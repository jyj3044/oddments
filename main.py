"""Oddments 애플리케이션 진입점 (Flet UI).

캡처·OCR·웹 송출·Arduino 연동은 ``flet_ui`` 와 기존 백엔드 모듈을 사용합니다.

실행 전:
  pip install -r requirements.txt

실행:
  python main.py

키워드 OCR: RapidOCR (``pip install rapidocr-onnxruntime``, requirements 에 포함).
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

import cv2
import flet as ft
import numpy as np

from streaming.remote_client import run_session_in_thread
from streaming.remote_host import rtc_configuration_from_stun_turn
from streaming.remote_log import log_remote_event

from app_platform import ensure_pre_gui_init
from app_platform.host import require_windows_admin_or_exit
from flet_ui.components import (
    schedule_clipboard_read,
    set_clipboard,
    show_snack,
)
from flet_ui.shell import (
    ROUTE_APP_SETTINGS,
    ROUTE_ARDUINO,
    ROUTE_DASHBOARD,
    ROUTE_LOGS,
    ROUTE_OCR,
    ROUTE_REMOTE_SETTINGS,
    ROUTE_WEB,
    StreamMasterApp,
)
from flet_ui.log_buffers import get_log_store, shutdown_log_store
from flet_ui.pages import (
    build_app_settings,
    build_arduino_link,
    build_dashboard,
    build_logs,
    build_ocr_settings,
    build_remote_settings,
    build_web_stream,
)
from flet_ui.theme import (
    StreamMasterTheme as T,
    apply_theme_mode,
    body_md,
    button_style_click_cursor,
    label_lg,
    label_md,
)
from flet_ui.state import APP_NAME, AppState


def _resolve_assets_dir() -> str:
    """개발 시에는 프로젝트 `assets/`, PyInstaller 동결 시에는 ``_MEIPASS/assets``."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return os.path.join(base, "assets")
        return os.path.join(os.path.dirname(sys.executable), "assets")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


ASSETS_DIR = _resolve_assets_dir()

# 플레이스홀더: 회색 타일(data URI). 옛 1x1 PNG 는 확대 시 붉게 보일 수 있다.
# 원격 뷰어는 별도 프로세스라 ``src=JPEG bytes`` 가 불안정할 수 있어,
# 임시 파일 경로(문자열)로 넘기고 오래된 파일만 순차 삭제한다.
_REMOTE_VIEW_MAX_DISPLAY_SIDE = 1280
_REMOTE_VIEW_JPEG_QUALITY = 68
_REMOTE_VIEW_TEMP_KEEP = 6
_gray_ph = np.full((64, 64, 3), 45, dtype=np.uint8)
_ok_ph, _gray_buf = cv2.imencode(".png", _gray_ph)
if _ok_ph:
    _REMOTE_PLACEHOLDER_DATA_URI = (
        "data:image/png;base64,"
        + base64.b64encode(bytes(_gray_buf)).decode("ascii")
    )
else:
    _REMOTE_PLACEHOLDER_DATA_URI = (
        "data:image/png;base64,"
        + base64.b64encode(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
            )
        ).decode("ascii")
    )


def _raise_remote_viewer_window(page: ft.Page) -> None:
    """보조 원격 창을 다른 창 위로 올리고 포커스를 준다.

    Windows 에서 포커스 도난 방지로 ``to_front`` 만으로는 부족할 수 있어
    잠깐 ``always_on_top`` 을 켰다 끈다.
    """
    win = getattr(page, "window", None)
    if win is None:
        return

    async def _kick() -> None:
        await asyncio.sleep(0.04)
        try:
            win.focused = True
        except Exception:
            pass
        try:
            win.always_on_top = True
        except Exception:
            pass
        try:
            page.update()
        except Exception:
            pass
        await asyncio.sleep(0.06)
        try:
            await win.to_front()
        except Exception:
            pass
        await asyncio.sleep(0.08)
        try:
            win.focused = True
        except Exception:
            pass
        try:
            await win.to_front()
        except Exception:
            pass
        await asyncio.sleep(0.22)
        try:
            win.always_on_top = False
        except Exception:
            pass
        try:
            page.update()
        except Exception:
            pass
        await asyncio.sleep(0.05)
        try:
            await win.to_front()
        except Exception:
            pass

        if sys.platform == "win32":
            try:
                _win32_bring_own_window_to_foreground(page)
            except Exception:
                pass

    try:
        rt = getattr(page, "run_task", None)
        if callable(rt):
            rt(_kick)
    except Exception:
        pass


def _win32_bring_own_window_to_foreground(page: ft.Page) -> None:
    """포그라운드 허용 규칙을 우회하기 위한 보조(실패해도 무시)."""
    import ctypes

    title = getattr(page, "title", None) or ""
    if not title:
        return
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, str(title))
    if not hwnd:
        return
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)


def _set_process_display_name(name: str) -> None:
    try:
        import setproctitle

        setproctitle.setproctitle(name)
    except ImportError:
        pass
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(name)
        except Exception:
            pass


def _install_error_routing(page: ft.Page) -> None:
    """전역 예외를 모달이 아닌 스낵바로 알리고, 상세는 로그 파일에 남긴다.

    - ``page.on_error`` : Flet 이벤트 핸들러에서 새어나간 예외
    - ``sys.excepthook`` : 동기 코드의 미처리 예외
    - ``threading.excepthook`` : 백그라운드 스레드의 미처리 예외 (Py3.8+)

    모달을 쓰지 않아 화면이 막히지 않고 사이드바 등으로 이동할 수 있다.
    """

    def _route(message: str, detail: str | None = None) -> None:
        try:
            from flet_ui.log_buffers import log_app_event

            log_app_event("ERROR", message, detail=detail)
        except Exception:
            pass
        head = str(message).strip()
        if len(head) > 240:
            head = head[:237] + "…"

        def _show() -> None:
            try:
                show_snack(page, head, severity="error", duration_sec=14)
            except Exception:
                pass

        try:
            run_task = getattr(page, "run_task", None)
            if callable(run_task):

                async def _runner() -> None:
                    _show()

                run_task(_runner)
                return
        except Exception:
            pass
        _show()

    def _on_page_error(e: ft.ControlEvent) -> None:
        data = getattr(e, "data", None) or "알 수 없는 오류"
        # Flet 의 ``e.data`` 는 보통 traceback 문자열 한 덩어리.
        first_line = str(data).strip().splitlines()[:1]
        head = first_line[0] if first_line else str(data)
        _route(head, detail=str(data))

    try:
        page.on_error = _on_page_error  # type: ignore[attr-defined]
    except Exception:
        pass

    prev_excepthook = sys.excepthook

    def _sys_excepthook(exc_type, exc, tb) -> None:
        try:
            tb_text = "".join(traceback.format_exception(exc_type, exc, tb))
            _route(f"{exc_type.__name__}: {exc}", detail=tb_text)
        except Exception:
            pass
        try:
            prev_excepthook(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _sys_excepthook

    if hasattr(threading, "excepthook"):
        prev_thread_hook = threading.excepthook

        def _thread_excepthook(args: "threading.ExceptHookArgs") -> None:
            try:
                tb_text = "".join(
                    traceback.format_exception(
                        args.exc_type, args.exc_value, args.exc_traceback
                    )
                )
                tname = getattr(args.thread, "name", "?")
                _route(
                    f"[스레드:{tname}] {args.exc_type.__name__}: {args.exc_value}",
                    detail=tb_text,
                )
            except Exception:
                pass
            try:
                prev_thread_hook(args)
            except Exception:
                pass

        threading.excepthook = _thread_excepthook  # type: ignore[assignment]


DEFAULT_WINDOW_WIDTH = 1280
DEFAULT_WINDOW_HEIGHT = 820


def _apply_window_settings(page: ft.Page, state: AppState) -> None:
    """저장된 윈도우 설정을 페이지에 적용한다.

    Flet 0.85 에서는 ``page.window`` 가 별도 Window 컨트롤이므로 폭/높이/위치/
    최대화 상태를 직접 설정한 뒤 ``page.update()`` 로 반영한다. 저장된 값이
    없으면 기본 1280×820 을 사용한다(앱 첫 실행).
    """

    win_cfg = state.settings.window
    win = getattr(page, "window", None)
    if win is None:
        return
    try:
        w = win_cfg.width if (win_cfg.width and win_cfg.width > 0) else DEFAULT_WINDOW_WIDTH
        h = win_cfg.height if (win_cfg.height and win_cfg.height > 0) else DEFAULT_WINDOW_HEIGHT
        win.width = float(w)
        win.height = float(h)
        if win_cfg.left is not None:
            win.left = float(win_cfg.left)
        if win_cfg.top is not None:
            win.top = float(win_cfg.top)
        if win_cfg.maximized:
            win.maximized = True
    except Exception:
        # 어떤 속성이 없거나 잘못된 값이어도 첫 실행을 막지 않는다.
        pass
    try:
        page.update()
    except Exception:
        pass


def _install_window_tracking(page: ft.Page, state: AppState) -> None:
    """창 크기/위치 변경을 메모리 상태에 반영한다.

    실제 디스크 저장은 종료 시점에 한 번에 수행하므로(드래그 도중 매번 파일을
    쓰지 않음) 여기서는 ``state.settings.window`` 만 갱신한다.
    """

    win = getattr(page, "window", None)
    if win is None:
        return

    def _capture() -> None:
        try:
            w = getattr(win, "width", None)
            h = getattr(win, "height", None)
            left = getattr(win, "left", None)
            top = getattr(win, "top", None)
            maxed = bool(getattr(win, "maximized", False))
        except Exception:
            return
        cfg = state.settings.window
        # 최대화 상태에서는 일반 width/height 가 화면 전체값이라 다음 부팅 시
        # 평상 크기를 잃기 쉽다. 따라서 maximized 일 때는 크기/위치를 덮어쓰지
        # 않고 플래그만 갱신한다.
        cfg.maximized = maxed
        if not maxed:
            try:
                if w is not None:
                    cfg.width = max(1, int(w))
                if h is not None:
                    cfg.height = max(1, int(h))
                if left is not None:
                    cfg.left = int(left)
                if top is not None:
                    cfg.top = int(top)
            except (TypeError, ValueError):
                pass

    def _on_window_event(_e: ft.ControlEvent) -> None:
        _capture()

    try:
        win.on_event = _on_window_event  # type: ignore[attr-defined]
    except Exception:
        pass

    # page.on_resize 는 콘텐츠 리사이즈 이벤트로 더 자주 들어와 안전망이 된다.
    def _on_page_resize(_e: ft.ControlEvent) -> None:
        _capture()

    try:
        page.on_resize = _on_page_resize  # type: ignore[attr-defined]
    except Exception:
        pass


def main(page: ft.Page) -> None:
    _set_process_display_name(APP_NAME)
    ensure_pre_gui_init()

    # 페이지가 마운트되기 전에 시작해야, 사용자가 어떤 페이지로 처음 들어가더라도
    # 큐에 쌓인 초기 로그를 잃지 않는다.
    get_log_store()

    state = AppState()
    state.load()
    state.assets_dir = ASSETS_DIR  # type: ignore[attr-defined]
    apply_theme_mode(dark=state.settings.dark_mode)

    pages = {
        ROUTE_DASHBOARD: ("Dashboard", "dashboard", build_dashboard),
        ROUTE_OCR: ("OCR Settings", "visibility", build_ocr_settings),
        ROUTE_ARDUINO: ("Arduino Link", "memory", build_arduino_link),
        ROUTE_WEB: ("Web Stream", "settings_input_antenna", build_web_stream),
        ROUTE_REMOTE_SETTINGS: ("Remote Desktop", "desktop_access_disabled", build_remote_settings),
        ROUTE_LOGS: ("Log", "terminal", build_logs),
        ROUTE_APP_SETTINGS: ("앱 설정", "settings", build_app_settings),
    }

    app = StreamMasterApp(state, pages)

    state.go = app._goto  # type: ignore[attr-defined]
    state.page = page  # type: ignore[attr-defined]

    # 기본 Flet 에러 페이지 대신 모달로 띄우도록 가장 먼저 설치한다.
    _install_error_routing(page)

    # 저장된 창 크기 적용은 attach 보다 먼저 해야 사용자가 보는 첫 프레임에서
    # 깜빡임 없이 원하는 크기로 떠오른다.
    _apply_window_settings(page, state)
    _install_window_tracking(page, state)

    def _on_disconnect(_e: ft.ControlEvent | None = None) -> None:
        # 창을 닫는 그 순간의 width/height 를 한 번 더 잡아둔다. on_event 가
        # 호출되기 전에 종료 이벤트가 먼저 도착하는 경우를 대비한 안전망.
        try:
            win = getattr(page, "window", None)
            if win is not None:
                cfg = state.settings.window
                if not bool(getattr(win, "maximized", False)):
                    w = getattr(win, "width", None)
                    h = getattr(win, "height", None)
                    if w:
                        cfg.width = max(1, int(w))
                    if h:
                        cfg.height = max(1, int(h))
                cfg.maximized = bool(getattr(win, "maximized", False))
        except Exception:
            pass
        try:
            state.save()
        except Exception:
            pass
        try:
            state.shutdown()
        finally:
            shutdown_log_store()

    page.on_disconnect = _on_disconnect
    page.on_close = _on_disconnect

    app.attach(page)


def remote_viewer_main(page: ft.Page) -> None:
    """원격 뷰어 전용 보조 창. 메인과 별도 프로세스로 실행된다."""

    _set_process_display_name(f"{APP_NAME} Remote")
    ensure_pre_gui_init()

    state = AppState()
    state.load()
    get_log_store()
    apply_theme_mode(dark=state.settings.dark_mode)

    page.title = f"{APP_NAME} — 원격"
    page.theme_mode = (
        ft.ThemeMode.DARK
        if state.settings.dark_mode
        else ft.ThemeMode.LIGHT
    )
    page.padding = 0
    # 본문(viewport) 과 동일 톤으로 맞춰야 레이아웃 틈·디바이더 옆에 BACKGROUND(#121316)
    # 만 깔려 검은 세로 띠처럼 보이지 않는다.
    page.bgcolor = T.SURFACE_BRIGHT
    page.theme = T.theme()
    page.fonts = T.fonts()

    win = getattr(page, "window", None)
    if win is not None:
        try:
            win.width = 1280
            win.height = 720
            win.min_width = 800
            win.min_height = 450
        except Exception:
            pass

    _install_error_routing(page)

    rc = state.settings.remote.client
    hp = state.settings.remote.host

    try:
        _vh = (rc.host or "").strip() or "127.0.0.1"
        log_remote_event(f"원격 뷰어: 창 시작 (대상 {_vh}:{int(rc.port)})")
    except Exception:
        pass

    session_ref: dict[str, object | None] = {"s": None}
    # 사이드바 클릭 시 False → 로컬 OS 로 Win/Ctrl/Alt 전달 허용.
    # 영상 영역 클릭·부팅 지연 포커스 시 True → 저수준 훅으로 로컬 차단 + 원격 전송.
    viewer_kb_capture = [False]
    kl_ref: dict[str, ft.KeyboardListener | None] = {"k": None}
    win_kbd_sink_stop: Callable[[], None] | None = None
    meta_from_host = [False]
    decode_dims_shown = [False]
    jpeg_temp_paths: list[str] = []
    first_video_logged = [False]

    conn_status = ft.Text("", style=body_md(), color=T.ON_SURFACE_VARIANT)
    res_line = ft.Text(
        "—",
        style=body_md(),
        color=T.ON_SURFACE_VARIANT,
    )

    # 원격 스트림 종횡비(호스트 메타 또는 첫 프레임). 입력 좌표 정규화에 사용.
    stream_wh = [1280.0, 720.0]
    # 뷰포트(네비 제외 영역) 크기 — on_size_change 로 갱신. 픽셀 박스 레이아웃에 사용.
    view_size = [1280.0, 720.0]

    def _contain_fit_disp_xy() -> tuple[float, float, float, float]:
        """BoxFit.contain 과 동일: 표시 영역 (w,h) 및 좌상단 오프셋 (ox,oy)."""
        cw = max(1.0, view_size[0])
        ch = max(1.0, view_size[1])
        sw = max(1.0, stream_wh[0])
        sh = max(1.0, stream_wh[1])
        ar_s = sw / sh
        ar_c = cw / ch
        if ar_c > ar_s:
            disp_h = ch
            disp_w = disp_h * ar_s
            ox = (cw - disp_w) * 0.5
            oy = 0.0
        else:
            disp_w = cw
            disp_h = disp_w / ar_s
            ox = 0.0
            oy = (ch - disp_h) * 0.5
        return max(1.0, disp_w), max(1.0, disp_h), ox, oy

    img_view = ft.Image(
        src=_REMOTE_PLACEHOLDER_DATA_URI,
        expand=True,
        fit=ft.BoxFit.CONTAIN,
        gapless_playback=True,
        error_content=ft.Container(
            expand=True,
            bgcolor=T.SURFACE_CONTAINER_LOW,
            alignment=ft.Alignment.CENTER,
            content=ft.Text(
                "영상 디코딩 실패. logs/app-날짜.log 및 호스트 송출을 확인하세요.",
                style=body_md(),
                color=T.ON_SURFACE_VARIANT,
                text_align=ft.TextAlign.CENTER,
            ),
        ),
    )
    # 픽셀 박스 + margin(ox,oy) = BoxFit.contain 과 동일 배치(세로·가로 중앙).
    _dw, _dh, _ox, _oy = _contain_fit_disp_xy()
    remote_frame = ft.Container(
        expand=False,
        width=float(_dw),
        height=float(_dh),
        margin=ft.margin.only(left=float(_ox), top=float(_oy)),
        alignment=ft.Alignment.CENTER,
        bgcolor=T.SURFACE_BRIGHT,
        clip_behavior=ft.ClipBehavior.NONE,
        content=img_view,
    )

    def _layout_remote_frame_pixels() -> None:
        dw, dh, ox, oy = _contain_fit_disp_xy()
        try:
            remote_frame.width = float(dw)
            remote_frame.height = float(dh)
            remote_frame.margin = ft.margin.only(left=float(ox), top=float(oy))
        except Exception:
            pass

    def _schedule_remote_frame_aspect() -> None:
        async def _run() -> None:
            _layout_remote_frame_pixels()
            try:
                remote_frame.update()
            except Exception:
                pass

        try:
            rt = getattr(page, "run_task", None)
            if callable(rt):
                rt(_run)
        except Exception:
            pass

    last_emit = [0.0]
    last_hover = [0.0]

    def _norm_xy(local_pos: object | None) -> tuple[float, float]:
        """뷰포트 로컬 좌표 → 호스트 기준 0..1. BoxFit.contain 레터박스 보정."""
        disp_w, disp_h, ox, oy = _contain_fit_disp_xy()
        if local_pos is None:
            return 0.5, 0.5
        try:
            lx = float(getattr(local_pos, "x", 0.0))
            ly = float(getattr(local_pos, "y", 0.0))
        except (TypeError, ValueError):
            return 0.5, 0.5
        nx = (lx - ox) / max(disp_w, 1.0)
        ny = (ly - oy) / max(disp_h, 1.0)
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        return nx, ny

    def _emit_state(msg: str) -> None:
        async def _apply() -> None:
            conn_status.value = msg
            try:
                page.update()
            except Exception:
                pass

        try:
            page.run_task(_apply)
        except Exception:
            pass

    def _send_json(payload: dict) -> None:
        sess = session_ref["s"]
        if sess is not None:
            try:
                sess.send_json(payload)  # type: ignore[attr-defined]
            except Exception:
                pass

    def _remap_key_token_for_mac_host(tok: str) -> str:
        """Windows → macOS 호스트 시 수정자 토큰 치환 (⊞→⌥, Alt→⌘, Ctrl 유지)."""
        if not getattr(rc, "mac_modifier_remap", False):
            return tok
        if tok == "cmd":
            return "alt_l"
        if tok == "cmd_r":
            return "alt_r"
        if tok == "alt_l":
            return "cmd"
        if tok == "alt_r":
            return "cmd_r"
        return tok

    def _send_remote_key(tok: str, down: bool) -> None:
        if not tok:
            return
        _send_json(
            {
                "t": "key",
                "k": _remap_key_token_for_mac_host(tok),
                "down": down,
            }
        )

    def _send_mod_from_hook(tok: str, down: bool) -> None:
        """Windows LL 훅 스레드에서 호출 — RemoteViewerSession.send_json 이 thread-safe."""
        _send_remote_key(tok, down)

    async def _focus_remote_viewport() -> None:
        viewer_kb_capture[0] = True
        k = kl_ref["k"]
        if k is not None:
            await k.focus()

    async def _delayed_boot_keyboard_focus() -> None:
        await asyncio.sleep(0.85)
        viewer_kb_capture[0] = True
        k = kl_ref["k"]
        if k is not None:
            await k.focus()

    def _on_sidebar_pointer(_e: ft.ControlEvent) -> None:
        viewer_kb_capture[0] = False

    def _pull_host_clip(_e: ft.ControlEvent | None = None) -> None:
        """호스트 클립보드 요청(DataChannel). 추후 다른 UI 에서 재사용."""
        _send_json({"t": "clip_get"})

    def _push_local_clip(_e: ft.ControlEvent | None = None) -> None:
        """이 PC 클립보드를 호스트로 전송. 추후 다른 UI 에서 재사용."""

        def _got(text: str | None) -> None:
            if text:
                _send_json({"t": "clip_set", "text": text})
                _emit_state("이 PC 클립보드를 호스트에 반영했습니다.")
            else:
                _emit_state("클립보드를 읽지 못했습니다.")

        schedule_clipboard_read(page, _got)

    def _on_dc_json(d: dict) -> None:
        async def _apply() -> None:
            t = d.get("t")
            if t == "meta":
                meta_from_host[0] = True
                sw = d.get("stream_w")
                sh = d.get("stream_h")
                mw = d.get("mon_w")
                mh = d.get("mon_h")
                try:
                    if sw and sh:
                        stream_wh[0] = float(max(1, int(sw)))
                        stream_wh[1] = float(max(1, int(sh)))
                except (TypeError, ValueError):
                    pass
                _layout_remote_frame_pixels()
                try:
                    if mw and mh:
                        res_line.value = f"{int(mw)}×{int(mh)}"
                except (TypeError, ValueError):
                    pass
            elif t == "clip":
                txt = d.get("text")
                if isinstance(txt, str):
                    set_clipboard(page, txt)
                    conn_status.value = "호스트 클립보드를 이 PC에 복사했습니다."
            try:
                page.update()
            except Exception:
                pass

        try:
            page.run_task(_apply)
        except Exception:
            pass

    rail_expanded = [True]
    SIDEBAR_W_COLLAPSED = 52
    _RV_NAV_WIDTH_MIN = 180.0
    _RV_NAV_WIDTH_MAX = 560.0
    rw_cfg = getattr(state.settings.window, "remote_viewer_sidebar_width", None)
    try:
        if rw_cfg is not None and int(rw_cfg) > 0:
            rail_width_user = [
                float(
                    max(
                        _RV_NAV_WIDTH_MIN,
                        min(_RV_NAV_WIDTH_MAX, float(rw_cfg)),
                    )
                )
            ]
        else:
            rail_width_user = [300.0]
    except (TypeError, ValueError):
        rail_width_user = [300.0]

    sb_title = ft.Text("원격 뷰어", style=label_lg(), color=T.ON_SURFACE)
    sb_target = ft.Text(
        f"대상 {rc.host or '127.0.0.1'}:{rc.port}",
        style=body_md(),
        color=T.ON_SURFACE_VARIANT,
    )
    expanded_block = ft.Column(
        spacing=10,
        controls=[
            sb_title,
            sb_target,
            conn_status,
            res_line,
        ],
    )

    toggle_rail_btn = ft.IconButton(
        icon=ft.Icons.CHEVRON_LEFT,
        tooltip="패널 접기",
        style=button_style_click_cursor(ft.ButtonStyle(color=T.ON_SURFACE)),
    )

    sidebar_container = ft.Container(
        width=rail_width_user[0],
        bgcolor=T.SURFACE_CONTAINER_LOW,
        padding=ft.padding.symmetric(horizontal=10, vertical=12),
        on_click=_on_sidebar_pointer,
        content=ft.Column(
            expand=True,
            spacing=12,
            controls=[
                toggle_rail_btn,
                expanded_block,
            ],
        ),
    )

    def _rail_drag_delta(e: ft.DragUpdateEvent) -> float:
        if e.primary_delta is not None:
            return float(e.primary_delta)
        ld = e.local_delta
        if ld is not None:
            return float(ld.x)
        gd = e.global_delta
        if gd is not None:
            return float(gd.x)
        return 0.0

    def _on_rail_drag_update(e: ft.DragUpdateEvent) -> None:
        if not rail_expanded[0]:
            return
        delta = _rail_drag_delta(e)
        if delta == 0.0:
            return
        max_w = _RV_NAV_WIDTH_MAX
        try:
            pw = float(page.width or 1280.0)
            max_w = min(max_w, max(_RV_NAV_WIDTH_MIN + 48.0, pw * 0.58))
        except Exception:
            pass
        w = max(_RV_NAV_WIDTH_MIN, min(max_w, rail_width_user[0] + delta))
        if abs(w - rail_width_user[0]) < 0.25:
            return
        rail_width_user[0] = w
        sidebar_container.width = w
        try:
            page.update()
        except Exception:
            pass
        _sync_view_layout()

    def _persist_remote_rail_width() -> None:
        try:
            state.settings.window.remote_viewer_sidebar_width = int(
                round(rail_width_user[0])
            )
            state.save()
        except Exception:
            pass

    def _on_rail_drag_end(_e: ft.DragEndEvent) -> None:
        _persist_remote_rail_width()

    rail_split_hit = ft.Container(expand=True, bgcolor=T.SURFACE_CONTAINER_LOW)
    rail_splitter = ft.Container(
        width=6,
        bgcolor=T.SURFACE_CONTAINER_LOW,
        content=ft.GestureDetector(
            mouse_cursor=ft.MouseCursor.RESIZE_LEFT_RIGHT,
            on_horizontal_drag_update=_on_rail_drag_update,
            on_horizontal_drag_end=_on_rail_drag_end,
            content=rail_split_hit,
        ),
    )

    def _sync_rail_layout() -> None:
        ex = rail_expanded[0]
        sidebar_container.width = (
            rail_width_user[0] if ex else float(SIDEBAR_W_COLLAPSED)
        )
        expanded_block.visible = ex
        rail_splitter.visible = ex
        toggle_rail_btn.icon = (
            ft.Icons.CHEVRON_LEFT if ex else ft.Icons.CHEVRON_RIGHT
        )
        toggle_rail_btn.tooltip = "패널 접기" if ex else "패널 펼치기"

    def _on_toggle_rail(_e: ft.ControlEvent) -> None:
        viewer_kb_capture[0] = False
        rail_expanded[0] = not rail_expanded[0]
        _sync_rail_layout()
        try:
            page.update()
        except Exception:
            pass
        _sync_view_layout()

    toggle_rail_btn.on_click = _on_toggle_rail
    _sync_rail_layout()

    def _remote_pane_chrome_w() -> float:
        """사이드바 + 스플리터 (접힘 시 스플리터 숨김 → 폭 0)."""
        try:
            sw = float(sidebar_container.width or 0)
        except Exception:
            sw = float(rail_width_user[0])
        split_w = 6.0 if rail_expanded[0] else 0.0
        return max(0.0, sw + split_w)

    def _viewport_size_from_window() -> tuple[float, float]:
        """원격 영역만의 논리 크기. inner on_size_change 만으로는 창 확대 시 갱신이 누락될 수 있음."""
        pw = ph = 0.0
        try:
            pw = float(page.width or 0)
            ph = float(page.height or 0)
        except Exception:
            pass
        if pw < 2 or ph < 2:
            try:
                win = getattr(page, "window", None)
                if win is not None:
                    pw = float(getattr(win, "width", 0) or 0)
                    ph = float(getattr(win, "height", 0) or 0)
            except Exception:
                pass
        if pw < 2 or ph < 2:
            return max(1.0, view_size[0]), max(1.0, view_size[1])
        chrome = _remote_pane_chrome_w()
        cw = max(1.0, pw - chrome)
        ch = max(1.0, ph)
        return cw, ch

    def _sync_view_layout() -> None:
        cw, ch = _viewport_size_from_window()
        view_size[0] = cw
        view_size[1] = ch
        _layout_remote_frame_pixels()
        try:
            remote_frame.update()
        except Exception:
            pass

    def _on_frame(rgb: np.ndarray) -> None:
        now = time.monotonic()
        if now - last_emit[0] < 0.030:
            return
        last_emit[0] = now
        try:
            rgb = np.ascontiguousarray(rgb)
            if rgb.ndim != 3 or rgb.shape[2] != 3:
                return
            # 표시만 축소(입력 좌표용 stream_wh 는 원본 해상도 유지).
            disp = rgb
            h0, w0 = rgb.shape[:2]
            mx = max(h0, w0)
            if mx > _REMOTE_VIEW_MAX_DISPLAY_SIDE:
                sc = _REMOTE_VIEW_MAX_DISPLAY_SIDE / mx
                disp = cv2.resize(
                    rgb,
                    (max(1, int(w0 * sc)), max(1, int(h0 * sc))),
                    interpolation=cv2.INTER_AREA,
                )
            bgr = cv2.cvtColor(disp, cv2.COLOR_RGB2BGR)
            ok, buf = cv2.imencode(
                ".jpg",
                bgr,
                [int(cv2.IMWRITE_JPEG_QUALITY), _REMOTE_VIEW_JPEG_QUALITY],
            )
            if not ok:
                return
            jpeg_bytes = bytes(buf)
            if len(jpeg_bytes) < 4 or jpeg_bytes[:2] != b"\xff\xd8":
                return
        except Exception:
            return

        if not meta_from_host[0] and not decode_dims_shown[0]:
            decode_dims_shown[0] = True
            try:
                h, w = rgb.shape[:2]
                stream_wh[0] = float(max(1, w))
                stream_wh[1] = float(max(1, h))
            except Exception:
                pass
            _schedule_remote_frame_aspect()

        if not first_video_logged[0]:
            first_video_logged[0] = True
            try:
                log_remote_event(
                    f"원격 뷰어: 첫 영상 프레임 표시 준비 ({w0}×{h0})"
                )
            except Exception:
                pass

        blob = jpeg_bytes

        async def _apply_img(_data: bytes = blob) -> None:
            """데스크톱 Flet 에서 raw bytes src 가 빈 화면으로 남는 경우가 있어
            data:image/jpeg;base64 로 넘긴다. 실패 시 임시 파일 URI.
            """
            tmp_path: str | None = None
            data_uri = (
                "data:image/jpeg;base64,"
                + base64.b64encode(_data).decode("ascii")
            )
            try:
                img_view.src = data_uri
            except Exception:
                try:
                    fd, tmp_path = tempfile.mkstemp(
                        prefix="odd_rv_",
                        suffix=".jpg",
                        dir=tempfile.gettempdir(),
                    )
                    try:
                        os.write(fd, _data)
                    finally:
                        os.close(fd)
                    img_view.src = (
                        Path(tmp_path).resolve().as_uri()
                        + f"#t={time.time_ns()}"
                    )
                    jpeg_temp_paths.append(tmp_path)
                    while len(jpeg_temp_paths) > _REMOTE_VIEW_TEMP_KEEP:
                        old = jpeg_temp_paths.pop(0)
                        try:
                            os.unlink(old)
                        except OSError:
                            pass
                except Exception:
                    if tmp_path:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                    return
            try:
                img_view.update()
            except Exception:
                pass
            try:
                page.update()
            except Exception:
                pass

        try:
            page.run_task(_apply_img)
        except Exception:
            pass

    def _norm_key_token(raw: str) -> str:
        if not raw:
            return ""
        if len(raw) == 1:
            return raw
        table = {
            "Enter": "enter",
            "Escape": "esc",
            "Backspace": "backspace",
            "Delete": "delete",
            "Tab": "tab",
            "Caps Lock": "caps_lock",
            " ": "space",
            "Arrow Left": "left",
            "Arrow Right": "right",
            "Arrow Up": "up",
            "Arrow Down": "down",
            "Shift Left": "shift_l",
            "Shift Right": "shift_r",
            "Control Left": "ctrl_l",
            "Control Right": "ctrl_r",
            "Alt Left": "alt_l",
            "Alt Right": "alt_r",
            "Meta Left": "cmd",
            "Meta Right": "cmd_r",
            "Super Left": "cmd",
            "Super Right": "cmd_r",
            "Os Left": "cmd",
            "Os Right": "cmd_r",
        }
        if raw in table:
            return table[raw]
        low = raw.lower().strip().replace(" ", "_")
        aliases = {
            "shift_left": "shift_l",
            "shift_right": "shift_r",
            "control_left": "ctrl_l",
            "control_right": "ctrl_r",
            "alt_left": "alt_l",
            "alt_right": "alt_r",
            "meta_left": "cmd",
            "meta_right": "cmd_r",
            "caps_lock": "caps_lock",
        }
        if low in aliases:
            return aliases[low]
        return low

    def _on_vp_size(_e: ft.LayoutSizeChangeEvent) -> None:
        # 이벤트 값만 쓰면 창을 다시 키울 때 크기가 예전 최소값에 묶일 수 있음 → 창 기준 동기화.
        _sync_view_layout()

    def _hover(_e: ft.PointerEvent) -> None:
        now = time.monotonic()
        if now - last_hover[0] < 0.045:
            return
        last_hover[0] = now
        nx, ny = _norm_xy(getattr(_e, "local_position", None))
        _send_json({"t": "move", "nx": nx, "ny": ny})

    def _tap_dn(e: ft.TapEvent, btn: str, down: bool) -> None:
        if down:
            try:
                page.run_task(_focus_remote_viewport)
            except Exception:
                pass
        nx, ny = _norm_xy(getattr(e, "local_position", None))
        _send_json({"t": "move", "nx": nx, "ny": ny})
        _send_json({"t": "btn", "btn": btn, "down": down})

    def _scroll_ev(e: ft.ScrollEvent) -> None:
        sd = getattr(e, "scroll_delta", None)
        dy = float(getattr(sd, "y", 0.0) or 0.0)
        dx = float(getattr(sd, "x", 0.0) or 0.0)
        _send_json({"t": "scroll", "dx": int(dx), "dy": int(-dy)})

    def _kd(e: ft.KeyDownEvent) -> None:
        tok = _norm_key_token(e.key)
        if tok:
            _send_remote_key(tok, True)

    def _ku(e: ft.KeyUpEvent) -> None:
        tok = _norm_key_token(e.key)
        if tok:
            _send_remote_key(tok, False)

    # 레터박스 오프셋은 remote_frame.margin(ox,oy) 로 직접 반영 (contain 과 좌표 일치).
    gd = ft.GestureDetector(
        expand=True,
        mouse_cursor=ft.MouseCursor.PRECISE,
        content=ft.Container(
            expand=True,
            clip_behavior=ft.ClipBehavior.NONE,
            content=remote_frame,
        ),
        on_hover=_hover,
        on_tap_down=lambda ev: _tap_dn(ev, "left", True),
        on_tap_up=lambda ev: _tap_dn(ev, "left", False),
        on_secondary_tap_down=lambda ev: _tap_dn(ev, "right", True),
        on_secondary_tap_up=lambda ev: _tap_dn(ev, "right", False),
        on_scroll=_scroll_ev,
    )

    # expand 는 직계 부모가 Row/Column 등일 때만 먹는다. KL 의 부모를 Row 바로 아래
    # Column 으로 두고, 안쪽은 Column→GestureDetector→Image 로 채운다.
    _kl = ft.KeyboardListener(
        expand=True,
        autofocus=True,
        content=ft.Container(
            expand=True,
            bgcolor=T.SURFACE_BRIGHT,
            clip_behavior=ft.ClipBehavior.NONE,
            on_size_change=_on_vp_size,
            padding=0,
            margin=0,
            content=ft.Column(
                expand=True,
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[gd],
            ),
        ),
        on_key_down=_kd,
        on_key_up=_ku,
    )
    kl_ref["k"] = _kl
    viewport = ft.Column(
        expand=True,
        spacing=0,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[_kl],
    )

    page.add(
        ft.Row(
            expand=True,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                sidebar_container,
                rail_splitter,
                viewport,
            ],
        )
    )
    try:
        page.update()
    except Exception:
        pass

    def _on_page_resize_remote(_e: object) -> None:
        _sync_view_layout()

    try:
        page.on_resize = _on_page_resize_remote  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        _sync_view_layout()
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            from streaming.win_keyboard_sink import (
                start_win_keyboard_sink,
                stop_win_keyboard_sink,
            )

            start_win_keyboard_sink(
                window_title=str(page.title),
                should_suppress=lambda: viewer_kb_capture[0],
                send_key=_send_mod_from_hook,
            )
            win_kbd_sink_stop = stop_win_keyboard_sink
        except Exception:
            win_kbd_sink_stop = None

    try:
        rd = getattr(page, "run_task", None)
        if callable(rd):
            rd(_delayed_boot_keyboard_focus)
    except Exception:
        pass

    _raise_remote_viewer_window(page)

    async def _raise_remote_delayed() -> None:
        await asyncio.sleep(0.55)
        _raise_remote_viewer_window(page)

    try:
        rd = getattr(page, "run_task", None)
        if callable(rd):
            rd(_raise_remote_delayed)
    except Exception:
        pass

    rtc_cfg = rtc_configuration_from_stun_turn(
        stun_urls=hp.stun_urls,
        turn_uri=hp.turn_uri,
        turn_username=hp.turn_username,
        turn_password=hp.turn_password,
    )
    host_addr = (rc.host or "").strip() or "127.0.0.1"
    _, sess = run_session_in_thread(
        signal_host=host_addr,
        signal_port=int(rc.port),
        rtc_configuration=rtc_cfg,
        on_frame=_on_frame,
        on_state=_emit_state,
        on_dc_json=_on_dc_json,
        auth_token=(rc.auth_token or "").strip(),
    )
    session_ref["s"] = sess
    if sess is None:
        try:
            log_remote_event("원격 뷰어: 백그라운드 세션 시작 실패", error=True)
        except Exception:
            pass
        conn_status.value = "원격 세션을 시작하지 못했습니다."
        try:
            page.update()
        except Exception:
            pass

    def _on_disconnect(_e: ft.ControlEvent | None = None) -> None:
        stop_sink = win_kbd_sink_stop
        if stop_sink is not None:
            try:
                stop_sink()
            except Exception:
                pass
        try:
            log_remote_event("원격 뷰어: 창 종료")
        except Exception:
            pass
        for p in list(jpeg_temp_paths):
            try:
                os.unlink(p)
            except OSError:
                pass
        jpeg_temp_paths.clear()
        s = session_ref["s"]
        if s is not None:
            try:
                s.request_close()  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            state.settings.window.remote_viewer_sidebar_width = int(
                round(rail_width_user[0])
            )
        except Exception:
            pass
        try:
            state.save()
        except Exception:
            pass
        try:
            shutdown_log_store()
        except Exception:
            pass

    page.on_disconnect = _on_disconnect
    page.on_close = _on_disconnect


def _frozen_exe_dir() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


if __name__ == "__main__":
    os.makedirs(os.path.join(ASSETS_DIR, "preview"), exist_ok=True)
    _target = main
    if "--remote-viewer" in sys.argv:
        _target = remote_viewer_main
    else:
        require_windows_admin_or_exit(__file__)
    try:
        if hasattr(ft, "run"):
            ft.run(_target, assets_dir=ASSETS_DIR)
        else:
            ft.app(target=_target, assets_dir=ASSETS_DIR)
    except BaseException:
        d = _frozen_exe_dir()
        if d is not None:
            try:
                (d / "oddments_fatal_error.txt").write_text(
                    traceback.format_exc(), encoding="utf-8"
                )
            except OSError:
                pass
        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            try:
                print(
                    "치명적 오류가 발생했습니다. 실행 파일과 같은 폴더의 "
                    "oddments_fatal_error.txt 를 확인하세요.",
                    file=sys.stderr,
                )
            except OSError:
                pass
        raise
