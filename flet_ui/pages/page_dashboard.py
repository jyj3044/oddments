"""Dashboard 페이지 — 소스 모드, 캡처 FPS, 미리보기, 설정 카드 진입점."""

from __future__ import annotations

import threading
from typing import Callable

import cv2
import flet as ft
import numpy as np

from ..components import (
    close_active_dialog,
    make_vertical_resize_handle,
    outline_button,
    show_snack,
    text_field,
)
from ..state import AppState
from ..theme import (
    StreamMasterTheme as T,
    body_md,
    label_md,
    title_lg,
)


def _preview_encode_params(effective_fps: int) -> tuple[int, int]:
    """미리보기 FPS 에 맞춰 ``(max_side, quality)`` 를 결정한다.

    한 프레임당 CPU/IPC 비용이 거의 일정하도록 FPS 가 높을수록 사이즈와
    품질을 깎는다. Web 송출은 별도 파이프라인이라 영향 없음.
    """
    f = max(1, int(effective_fps))
    if f <= 5:
        return 960, 82
    if f <= 10:
        return 800, 78
    if f <= 15:
        return 720, 75
    if f <= 20:
        return 640, 72
    if f <= 25:
        return 560, 70
    return 480, 68


def _frame_to_jpeg_bytes(
    frame_bgr: np.ndarray, *, max_side: int, quality: int
) -> bytes:
    """프레임을 ``max_side`` 로 축소 + ``quality`` 로 JPEG 인코딩.

    Flet 0.85 의 ``Image.src`` 는 bytes 를 그대로 받아 base64 페이로드로
    전송한다. 매 프레임마다 IPC 가 발생하므로 인코딩 비용을 미리보기 FPS 에
    맞춰 가변으로 결정한다 (``_preview_encode_params`` 참고).
    """
    h, w = frame_bgr.shape[:2]
    if max(h, w) > max_side:
        scale = max_side / max(h, w)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        frame_bgr = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        return b""
    return bytes(buf.tobytes())


def _radio(value: str, label: str, group_value: str) -> ft.Container:
    selected = value == group_value
    return ft.Container(
        padding=4,
        content=ft.Row(
            controls=[
                ft.Container(
                    width=18,
                    height=18,
                    border=ft.border.all(2, T.PRIMARY if selected else T.OUTLINE),
                    border_radius=T.RADIUS_FULL,
                    content=ft.Container(
                        width=10,
                        height=10,
                        bgcolor=T.PRIMARY if selected else ft.Colors.TRANSPARENT,
                        border_radius=T.RADIUS_FULL,
                    ),
                    alignment=ft.alignment.center,
                ),
                ft.Text(label, style=body_md(), color=T.ON_SURFACE),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


# 임베드 미리보기 화면 갱신의 상한 FPS.
# 캡쳐 FPS 가 이보다 높아도 메인 윈도우 미리보기는 이 값으로 캡됨
# (CPU/IPC 보호). Web 송출(WebRTC)은 캡쳐 FPS 를 그대로 따라간다.
_PREVIEW_MAX_FPS = 15


class _DashboardController:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.page: ft.Page | None = None
        self._preview_image: ft.Image | None = None
        self._stop_event = threading.Event()
        self._preview_thread: threading.Thread | None = None
        self._mounted = False
        self._last_seq: int = -1

    def attach_preview(self, image: ft.Image) -> None:
        self._preview_image = image

    def start_preview_loop(self, page: ft.Page) -> None:
        if self._preview_thread is not None:
            return
        self.page = page
        self._stop_event.clear()
        self._mounted = True

        def _loop() -> None:
            while not self._stop_event.is_set():
                self._tick()
                # 캡쳐 FPS 와 _PREVIEW_MAX_FPS 중 작은 값 기준.
                # 캡쳐가 60이라도 미리보기 인코딩/IPC 는 30번까지만.
                cap_fps = max(
                    1,
                    int(getattr(self.state.settings.capture, "fps", 5) or 5),
                )
                fps = min(cap_fps, _PREVIEW_MAX_FPS)
                interval = 1.0 / float(fps)
                if self._stop_event.wait(interval):
                    return

        self._preview_thread = threading.Thread(
            target=_loop, name="flet-preview", daemon=True
        )
        self._preview_thread.start()

    def _schedule_ui(self, coro_factory: Callable[[], "object"]) -> None:
        """이벤트 루프에서 UI 변경을 실행하도록 스케줄링.

        Flet 0.85 에서 백그라운드 스레드의 ``control.update()`` 는 다음 이벤트
        가 들어와야 flush 되므로, 화면이 액션 직후에만 갱신되는 현상을 만든다.
        ``page.run_task`` 로 직접 루프에 던지면 즉시 반영된다.
        """
        page = self.page
        if page is None:
            return
        try:
            page.run_task(coro_factory)  # type: ignore[arg-type]
        except Exception:
            self._mounted = False

    def _tick(self) -> None:
        img = self._preview_image
        if not self._mounted or img is None or self.page is None:
            return
        running = self.state.is_running()
        if not running:
            if img.visible:
                img.visible = False

                async def _hide(_img=img) -> None:
                    try:
                        _img.update()
                    except Exception:
                        pass

                self._schedule_ui(_hide)
            self._last_seq = -1
            return

        seq = self.state.get_capture_frame_seq()
        if seq == self._last_seq:
            return
        frame = self.state.get_latest_preview()
        if frame is None:
            return
        cap_fps = max(
            1, int(getattr(self.state.settings.capture, "fps", 5) or 5)
        )
        eff_fps = min(cap_fps, _PREVIEW_MAX_FPS)
        max_side, quality = _preview_encode_params(eff_fps)
        jpg = _frame_to_jpeg_bytes(frame, max_side=max_side, quality=quality)
        if not jpg:
            return
        self._last_seq = seq

        async def _apply(_img=img, _data=jpg) -> None:
            try:
                _img.src = _data
                _img.visible = True
                _img.update()
            except Exception:
                pass

        self._schedule_ui(_apply)


def build_dashboard(state: AppState) -> ft.Control:
    ctrl = _DashboardController(state)
    cap = state.settings.capture

    radio_full = _radio("monitor", "전체화면", cap.source_mode)
    radio_window = _radio("window", "창모드", cap.source_mode)

    # 페이지가 빌드될 때 한 번 캐시. 픽커를 열 때마다 다시 갱신해 hot-plug 도 반영.
    try:
        state._monitor_cache = state.list_monitors()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        state._monitor_cache = []  # type: ignore[attr-defined]

    def _monitor_button_label() -> str:
        idx = int(cap.monitor_index or 1)
        cache = getattr(state, "_monitor_cache", []) or []
        for m in cache:
            if m["index"] == idx:
                name = (m.get("name") or "").strip()
                if name:
                    return f"Monitor {idx} — {name}"
                w = int(m.get("width") or 0)
                h = int(m.get("height") or 0)
                if w and h:
                    return f"Monitor {idx}  ({w}×{h})"
                return f"Monitor {idx}"
        # 캐시에 없으면 인덱스만 표시. 시작 시 검증에서 막혀 사용자가 다시 선택하게 된다.
        return f"Monitor {idx}"

    monitor_button = outline_button(
        _monitor_button_label(),
        icon=ft.Icons.MONITOR,
        on_click=lambda _e: _open_monitor_picker(state, _refresh_monitor_button),
    )
    monitor_button.height = 48
    monitor_button.visible = cap.source_mode == "monitor"

    def _refresh_monitor_button() -> None:
        monitor_button.content = _monitor_button_label()
        try:
            monitor_button.update()
        except Exception:
            pass

    def _pick_button_label() -> str:
        title = (cap.picked_summary or "").strip()
        if not title:
            return "Select Window..."
        if len(title) > 28:
            return title[:27] + "…"
        return title

    pick_button = outline_button(
        _pick_button_label(),
        icon=ft.Icons.WINDOW,
        on_click=lambda _e: _open_window_picker(state, _refresh_pick_button),
    )
    pick_button.height = 48
    pick_button.visible = cap.source_mode == "window"
    if cap.picked_summary:
        pick_button.tooltip = cap.picked_summary

    def _refresh_pick_button() -> None:
        pick_button.content = _pick_button_label()
        if cap.picked_summary:
            pick_button.tooltip = cap.picked_summary
        try:
            pick_button.update()
        except Exception:
            pass

    def _set_mode(mode: str) -> None:
        cap.source_mode = mode
        radio_full.content.controls[0].border = ft.border.all(
            2, T.PRIMARY if mode == "monitor" else T.OUTLINE
        )
        radio_full.content.controls[0].content.bgcolor = (
            T.PRIMARY if mode == "monitor" else ft.Colors.TRANSPARENT
        )
        radio_window.content.controls[0].border = ft.border.all(
            2, T.PRIMARY if mode == "window" else T.OUTLINE
        )
        radio_window.content.controls[0].content.bgcolor = (
            T.PRIMARY if mode == "window" else ft.Colors.TRANSPARENT
        )
        monitor_button.visible = mode == "monitor"
        pick_button.visible = mode == "window"
        page = radio_full.page
        if page is not None:
            page.update()

    radio_full.on_click = lambda _e: _set_mode("monitor")
    radio_window.on_click = lambda _e: _set_mode("window")

    source_card = ft.Container(
        padding=T.SPACE_LG,
        bgcolor=T.SURFACE_CONTAINER_LOW,
        border=ft.border.all(1, T.OUTLINE_VARIANT),
        border_radius=T.RADIUS_MD,
        expand=True,
        content=ft.Row(
            controls=[
                ft.Column(
                    controls=[
                        ft.Text(
                            "SOURCE MODE",
                            style=label_md(),
                            color=T.ON_SURFACE_VARIANT,
                        ),
                        ft.Row(
                            controls=[radio_full, radio_window],
                            spacing=12,
                        ),
                    ],
                    spacing=8,
                    tight=True,
                ),
                ft.Container(width=1, height=48, bgcolor=T.OUTLINE_VARIANT),
                ft.Column(
                    controls=[
                        ft.Text(
                            "DISPLAY TARGET",
                            style=label_md(),
                            color=T.ON_SURFACE_VARIANT,
                        ),
                        ft.Row(
                            controls=[monitor_button, pick_button],
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                    spacing=8,
                    tight=True,
                ),
            ],
            spacing=32,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )

    fps_input = text_field(
        value=str(cap.fps or 5),
        width=72,
        text_align=ft.TextAlign.CENTER,
        keyboard_type=ft.KeyboardType.NUMBER,
    )

    def _on_fps_change(_e: ft.ControlEvent) -> None:
        try:
            cap.fps = max(1, min(60, int(fps_input.value or 5)))
        except ValueError:
            pass

    fps_input.on_change = _on_fps_change

    fps_card = ft.Container(
        padding=T.SPACE_LG,
        bgcolor=T.SURFACE_CONTAINER_LOW,
        border=ft.border.all(1, T.OUTLINE_VARIANT),
        border_radius=T.RADIUS_MD,
        width=150,
        content=ft.Column(
            controls=[
                ft.Text(
                    "CAPTURE FPS",
                    style=label_md(),
                    color=T.ON_SURFACE_VARIANT,
                ),
                ft.Row(
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                    controls=[
                        ft.Icon(ft.Icons.SPEED, color=T.ON_SURFACE_VARIANT, size=20),
                        fps_input,
                    ],
                ),
            ],
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            tight=True,
        ),
    )

    source_row = ft.Row(
        controls=[source_card, fps_card],
        spacing=T.GUTTER,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        intrinsic_height=True,
    )

    preview_image = ft.Image(
        src="",
        # CONTAIN: 송출 프레임 전체를 보여 주고(잘림 없음) 비율 유지.
        # 프리뷰 박스 안에서 긴 쪽이 가로 또는 세로 한 축을 꽉 채우고,
        # 짧은 쪽에만 상하 또는 좌우 여백이 생긴다(COVER 는 잘라내기 때문에 사용 안 함).
        fit=ft.ImageFit.CONTAIN,
        expand=True,
        visible=False,
        error_content=ft.Container(),
        gapless_playback=True,
    )

    # JPEG 고유 픽셀(~480)만 쓰이면 박스 대비 작게 보이므로 부모를 expand 로 채운 뒤 CONTAIN 적용.
    _PREVIEW_DEFAULT_H = 420
    _PREVIEW_MIN_H = 240
    _PREVIEW_MAX_H = 1400
    saved_preview_h = state.settings.window.dashboard_preview_height
    if saved_preview_h is None:
        initial_preview_h = _PREVIEW_DEFAULT_H
    else:
        # 과거에 더 작은/큰 한계로 저장된 값을 막기 위해 범위 클램프.
        initial_preview_h = max(_PREVIEW_MIN_H, min(_PREVIEW_MAX_H, int(saved_preview_h)))
    preview_box = ft.Container(
        height=initial_preview_h,
        border=ft.border.all(4, T.SURFACE_CONTAINER_HIGH),
        border_radius=T.RADIUS_MD,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
        bgcolor=T.INVERSE_SURFACE,
        content=ft.Container(expand=True, content=preview_image),
        shadow=ft.BoxShadow(blur_radius=12, color="#1a000000", offset=ft.Offset(0, 4)),
    )

    def _on_preview_height_change(h: int) -> None:
        # 메모리에만 반영 — 디스크 저장은 종료 시점 ``state.save()`` 에서 일괄.
        state.settings.window.dashboard_preview_height = int(h)

    preview_resize_handle = make_vertical_resize_handle(
        preview_box,
        initial_height=initial_preview_h,
        min_height=_PREVIEW_MIN_H,
        max_height=_PREVIEW_MAX_H,
        on_height_change=_on_preview_height_change,
    )

    ctrl.attach_preview(preview_image)

    page_root = ft.Column(
        controls=[
            source_row,
            preview_box,
            preview_resize_handle,
        ],
        spacing=T.GUTTER,
        expand=True,
        scroll=ft.ScrollMode.AUTO,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    page_obj = getattr(state, "page", None)
    if page_obj is not None:
        ctrl.start_preview_loop(page_obj)

    return page_root


def _open_window_picker(
    state: AppState,
    on_picked: Callable[[], None] | None = None,
) -> None:
    page = getattr(state, "page", None)
    if page is None:
        return
    windows = state.list_windows()
    if not windows:
        show_snack(
            page,
            "열려 있는 창이 없거나 이 플랫폼에서는 지원되지 않습니다.",
            bgcolor=T.SURFACE_CONTAINER_HIGH,
        )
        return

    # ESC / 바깥 클릭으로 닫히도록 ``modal=False`` 로 띄운다.
    # Flet 0.85 의 ``modal=True`` AlertDialog 는 키보드 이벤트를 자체 오버레이가
    # 캡처해 ``page.on_keyboard_event`` 가 발화하지 않는다. ``modal=False`` 로 두면
    # Flutter 의 ``Navigator`` 가 ESC 를 native 로 처리해 다이얼로그를 자동으로 닫는다.
    # 폴백으로 ``page.on_keyboard_event`` 도 함께 단다 (환경에 따라 발화하기도 함).
    prev_keyboard = getattr(page, "on_keyboard_event", None)
    closed = {"v": False}

    def _restore_keyboard() -> None:
        try:
            page.on_keyboard_event = prev_keyboard  # type: ignore[attr-defined]
        except Exception:
            pass

    def _close_self() -> None:
        if closed["v"]:
            return
        closed["v"] = True
        _restore_keyboard()
        close_active_dialog(page)

    items: list[ft.Control] = []
    for w in windows:
        title = getattr(w, "title", "(제목 없음)")
        hwnd = getattr(w, "hwnd", 0)
        items.append(
            ft.ListTile(
                title=ft.Text(title),
                subtitle=ft.Text(f"hwnd: {hwnd}", style=label_md()),
                # 항목 클릭 시: 키보드 핸들러 복원 → 픽 처리(거기서 다이얼로그 닫음).
                on_click=lambda _e, h=hwnd, t=title: (
                    _restore_keyboard(),
                    _set_picked(state, h, t, on_picked),
                ),
            )
        )

    dialog = ft.AlertDialog(
        modal=False,
        title=ft.Text("창 선택", style=title_lg()),
        content=ft.Container(
            width=520,
            height=420,
            content=ft.ListView(controls=items, spacing=4),
        ),
        actions=[
            ft.TextButton("닫기", on_click=lambda _e: _close_self()),
        ],
        on_dismiss=lambda _e=None: (closed.__setitem__("v", True), _restore_keyboard()),
    )

    def _on_key(e: ft.KeyboardEvent) -> None:
        try:
            k = str(getattr(e, "key", "")).lower()
        except Exception:
            k = ""
        if k in ("escape", "esc"):
            _close_self()
            return
        if callable(prev_keyboard):
            try:
                prev_keyboard(e)
            except Exception:
                pass

    try:
        page.on_keyboard_event = _on_key  # type: ignore[attr-defined]
    except Exception:
        pass

    show = getattr(page, "show_dialog", None)
    if callable(show):
        try:
            show(dialog)
            return
        except Exception:
            pass
    try:
        page.dialog = dialog  # type: ignore[attr-defined]
        dialog.open = True
        page.update()
    except Exception:
        _restore_keyboard()


def _set_picked(
    state: AppState,
    hwnd: int,
    title: str,
    on_picked: Callable[[], None] | None = None,
) -> None:
    state.settings.capture.picked_hwnd = int(hwnd)
    state.settings.capture.picked_summary = title
    state.settings.capture.source_mode = "window"
    page = getattr(state, "page", None)
    if page is None:
        return
    close_active_dialog(page)
    if on_picked is not None:
        try:
            on_picked()
        except Exception:
            pass


def _open_monitor_picker(
    state: AppState,
    on_picked: Callable[[], None] | None = None,
) -> None:
    """모니터 선택 모달. 창 선택 픽커와 동일한 ESC/외부클릭 처리 적용."""
    page = getattr(state, "page", None)
    if page is None:
        return
    monitors = state.list_monitors()
    # 외부에서도 같은 캐시를 라벨링에 쓰므로 매번 갱신.
    try:
        state._monitor_cache = monitors  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    if not monitors:
        show_snack(
            page,
            "사용 가능한 모니터가 없습니다.",
            severity="warning",
        )
        return

    prev_keyboard = getattr(page, "on_keyboard_event", None)
    closed = {"v": False}

    def _restore_keyboard() -> None:
        try:
            page.on_keyboard_event = prev_keyboard  # type: ignore[attr-defined]
        except Exception:
            pass

    def _close_self() -> None:
        if closed["v"]:
            return
        closed["v"] = True
        _restore_keyboard()
        close_active_dialog(page)

    items: list[ft.Control] = []
    for m in monitors:
        idx = int(m["index"])
        w = int(m["width"])
        h = int(m["height"])
        name = (m.get("name") or "").strip()
        sub_controls: list[ft.Control] = []
        if name:
            sub_controls.append(
                ft.Text(name, style=label_md(), color=T.ON_SURFACE_VARIANT)
            )
        sub_controls.append(
            ft.Text(f"{w}×{h}", style=label_md(), color=T.ON_SURFACE_VARIANT)
        )
        items.append(
            ft.ListTile(
                title=ft.Text(f"Monitor {idx}"),
                subtitle=ft.Column(
                    controls=sub_controls, spacing=2, tight=True
                ),
                on_click=lambda _e, i=idx: (
                    _restore_keyboard(),
                    _set_picked_monitor(state, i, on_picked),
                ),
            )
        )

    dialog = ft.AlertDialog(
        modal=False,
        title=ft.Text("모니터 선택", style=title_lg()),
        content=ft.Container(
            width=520,
            height=min(420, 80 + len(items) * 72),
            content=ft.ListView(controls=items, spacing=4),
        ),
        actions=[
            ft.TextButton("닫기", on_click=lambda _e: _close_self()),
        ],
        on_dismiss=lambda _e=None: (closed.__setitem__("v", True), _restore_keyboard()),
    )

    def _on_key(e: ft.KeyboardEvent) -> None:
        try:
            k = str(getattr(e, "key", "")).lower()
        except Exception:
            k = ""
        if k in ("escape", "esc"):
            _close_self()
            return
        if callable(prev_keyboard):
            try:
                prev_keyboard(e)
            except Exception:
                pass

    try:
        page.on_keyboard_event = _on_key  # type: ignore[attr-defined]
    except Exception:
        pass

    show = getattr(page, "show_dialog", None)
    if callable(show):
        try:
            show(dialog)
            return
        except Exception:
            pass
    try:
        page.dialog = dialog  # type: ignore[attr-defined]
        dialog.open = True
        page.update()
    except Exception:
        _restore_keyboard()


def _set_picked_monitor(
    state: AppState,
    idx: int,
    on_picked: Callable[[], None] | None = None,
) -> None:
    state.settings.capture.monitor_index = int(idx)
    state.settings.capture.source_mode = "monitor"
    page = getattr(state, "page", None)
    if page is None:
        return
    close_active_dialog(page)
    if on_picked is not None:
        try:
            on_picked()
        except Exception:
            pass


__all__ = ["build_dashboard"]
