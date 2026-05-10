"""Oddments 애플리케이션 진입점 (Flet UI).

캡처·OCR·웹 송출·Arduino 연동은 ``flet_ui`` 와 기존 백엔드 모듈을 사용합니다.

실행 전:
  pip install -r requirements.txt

실행:
  python main.py

키워드 OCR: RapidOCR (``pip install rapidocr-onnxruntime``, requirements 에 포함).
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path

import flet as ft

from app_platform import ensure_pre_gui_init
from app_platform.host import require_windows_admin_or_exit
from flet_ui.components import show_error_dialog
from flet_ui.shell import (
    ROUTE_ARDUINO,
    ROUTE_DASHBOARD,
    ROUTE_OCR,
    ROUTE_WEB,
    StreamMasterApp,
)
from flet_ui.log_buffers import get_log_store, shutdown_log_store
from flet_ui.pages import (
    build_arduino_link,
    build_dashboard,
    build_ocr_settings,
    build_web_stream,
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
    """페이지 전체를 덮는 기본 Flet 에러 화면 대신 모달 알림으로 라우팅.

    - ``page.on_error`` : Flet 이벤트 핸들러에서 새어나간 예외
    - ``sys.excepthook`` : 동기 코드의 미처리 예외
    - ``threading.excepthook`` : 백그라운드 스레드의 미처리 예외 (Py3.8+)

    UI 스레드에서 안전하게 다이얼로그를 띄우기 위해 ``page.run_task`` 를 통해
    Flet 이벤트 루프로 작업을 마샬링한다.
    """

    def _route(message: str, detail: str | None = None) -> None:
        try:
            run_task = getattr(page, "run_task", None)
            if callable(run_task):

                async def _runner() -> None:
                    show_error_dialog(page, message, detail=detail)

                run_task(_runner)
                return
        except Exception:
            pass
        show_error_dialog(page, message, detail=detail)

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

    pages = {
        ROUTE_DASHBOARD: ("Dashboard", "dashboard", build_dashboard),
        ROUTE_OCR: ("OCR Settings", "visibility", build_ocr_settings),
        ROUTE_ARDUINO: ("Arduino Link", "memory", build_arduino_link),
        ROUTE_WEB: ("Web Stream", "settings_input_antenna", build_web_stream),
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


def _frozen_exe_dir() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


if __name__ == "__main__":
    require_windows_admin_or_exit(__file__)
    os.makedirs(os.path.join(ASSETS_DIR, "preview"), exist_ok=True)
    try:
        if hasattr(ft, "run"):
            ft.run(main, assets_dir=ASSETS_DIR)
        else:
            ft.app(target=main, assets_dir=ASSETS_DIR)
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
