"""Flet 메인 앱 셸: 사이드바, 상단바, 푸터, 라우팅."""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft

from .components import (
    STATUS_IDLE,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    _STATUS_DOT_COLOR,
    show_snack,
    status_dot,
)
from .state import AppState
from .theme import (
    StreamMasterTheme as T,
    headline_md,
    label_lg,
    label_md,
)

PageBuilder = Callable[[AppState], ft.Control]

ROUTE_DASHBOARD = "dashboard"
ROUTE_OCR = "ocr"
ROUTE_ARDUINO = "arduino"
ROUTE_WEB = "web"


def _nav_item(
    *,
    label: str,
    icon: str,
    active: bool,
    on_click: Callable[[ft.ControlEvent], None],
) -> ft.Container:
    if active:
        bg = T.SECONDARY_CONTAINER
        fg = T.ON_SECONDARY_CONTAINER
    else:
        bg = ft.Colors.TRANSPARENT
        fg = T.ON_SURFACE_VARIANT

    return ft.Container(
        content=ft.Row(
            controls=[
                ft.Icon(icon, color=fg, size=20),
                ft.Text(label, style=label_lg(), color=fg),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.padding.symmetric(horizontal=16, vertical=12),
        bgcolor=bg,
        border_radius=T.RADIUS_FULL,
        margin=ft.margin.symmetric(horizontal=8, vertical=2),
        on_click=on_click,
        ink=True,
        tooltip=label,
    )


class StreamMasterApp:
    def __init__(self, state: AppState, pages: dict[str, tuple[str, str, PageBuilder]]) -> None:
        self.state = state
        self.pages = pages
        self.current_route = ROUTE_DASHBOARD
        self.page: ft.Page | None = None

        self._page_container = ft.Container(
            padding=T.MARGIN_DESKTOP,
            content=ft.Container(),
            expand=True,
            bgcolor=T.SURFACE_BRIGHT,
        )

        self._sidebar = self._build_sidebar()
        self._topbar_btn_start: ft.FilledButton | None = None
        self._topbar_btn_stop: ft.OutlinedButton | None = None
        self._topbar = self._build_topbar()
        self._footer_ocr: ft.Row | None = None
        self._footer_arduino: ft.Row | None = None
        self._footer_web: ft.Row | None = None
        self._footer = self._build_footer()

    def _build_sidebar(self) -> ft.Container:
        self._nav_items = {
            ROUTE_DASHBOARD: ("Dashboard", ft.Icons.DASHBOARD_OUTLINED),
            ROUTE_OCR: ("OCR Settings", ft.Icons.VISIBILITY_OUTLINED),
            ROUTE_ARDUINO: ("Arduino Link", ft.Icons.MEMORY),
            ROUTE_WEB: ("Web Stream", ft.Icons.SETTINGS_INPUT_ANTENNA),
        }
        self._nav_column = ft.Column(spacing=4, expand=True)
        self._refresh_nav_buttons()

        return ft.Container(
            width=T.SIDEBAR_WIDTH,
            bgcolor=T.SURFACE_CONTAINER,
            border=ft.border.only(right=ft.BorderSide(1, T.OUTLINE_VARIANT)),
            padding=ft.padding.symmetric(vertical=24),
            content=ft.Column(
                controls=[
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=24),
                        margin=ft.margin.only(bottom=24),
                        content=ft.Text(
                            "Oddments",
                            style=headline_md(),
                            color=T.PRIMARY,
                        ),
                    ),
                    self._nav_column,
                ],
                spacing=0,
                expand=True,
            ),
        )

    def _refresh_nav_buttons(self) -> None:
        items: list[ft.Control] = []
        for key, (label, icon) in self._nav_items.items():
            items.append(
                _nav_item(
                    label=label,
                    icon=icon,
                    active=(key == self.current_route),
                    on_click=lambda e, k=key: self._goto(k),
                )
            )
        self._nav_column.controls = items

    def _goto(self, route: str) -> None:
        if route == self.current_route:
            return
        leaving = self.current_route
        self.current_route = route
        self._stop_route_log_pollers(leaving)
        self._refresh_nav_buttons()
        self._render_current_page()
        if self.page is not None:
            self.page.update()

    @staticmethod
    def _stop_route_log_pollers(leaving: str) -> None:
        """탭 전환 시 이전 페이지의 로그 폴링 스레드를 멈춘다 (고아 스레드·중복 소비 방지)."""
        if leaving == ROUTE_ARDUINO:
            from .pages.page_arduino import shutdown_arduino_log_poller_if_any

            shutdown_arduino_log_poller_if_any()
        elif leaving == ROUTE_WEB:
            from .pages.page_web import shutdown_web_log_poller_if_any

            shutdown_web_log_poller_if_any()
        elif leaving == ROUTE_OCR:
            from .pages.page_ocr import shutdown_ocr_log_poller_if_any

            shutdown_ocr_log_poller_if_any()

    def _render_current_page(self) -> None:
        builder = self.pages.get(self.current_route)
        if not builder:
            self._page_container.content = ft.Text("페이지가 없습니다.")
            return
        _, _, build_fn = builder
        try:
            self._page_container.content = build_fn(self.state)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            self._page_container.content = ft.Text(
                f"페이지 렌더 오류: {exc}", color=T.ERROR
            )

    def _build_topbar(self) -> ft.Container:
        self._topbar_btn_stop = ft.OutlinedButton(
            text="Stop",
            on_click=self._on_click_stop,
            style=ft.ButtonStyle(
                color=T.ON_SURFACE,
                bgcolor=T.SURFACE_CONTAINER_LOWEST,
                side=ft.BorderSide(1, T.OUTLINE),
                padding=ft.padding.symmetric(horizontal=16, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=T.RADIUS_DEFAULT),
                text_style=label_lg(),
            ),
        )
        self._topbar_btn_start = ft.FilledButton(
            text="Start",
            on_click=self._on_click_start,
            style=ft.ButtonStyle(
                bgcolor=T.PRIMARY,
                color=T.ON_PRIMARY,
                padding=ft.padding.symmetric(horizontal=16, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=T.RADIUS_DEFAULT),
                text_style=label_lg(),
            ),
        )
        return ft.Container(
            height=T.TOPBAR_HEIGHT,
            bgcolor=T.SURFACE_CONTAINER_LOWEST,
            border=ft.border.only(bottom=ft.BorderSide(1, T.OUTLINE_VARIANT)),
            padding=ft.padding.symmetric(horizontal=T.GUTTER),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.END,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[self._topbar_btn_stop, self._topbar_btn_start],
            ),
        )

    def _build_footer(self) -> ft.Container:
        self._footer_ocr = status_dot(status=STATUS_OFFLINE, label="OCR: Offline")
        self._footer_arduino = status_dot(status=STATUS_OFFLINE, label="Arduino: Offline")
        self._footer_web = status_dot(status=STATUS_OFFLINE, label="Web: Offline")
        return ft.Container(
            height=T.FOOTER_HEIGHT,
            bgcolor=T.SURFACE_CONTAINER_LOW,
            border=ft.border.only(top=ft.BorderSide(1, T.OUTLINE_VARIANT)),
            padding=ft.padding.symmetric(horizontal=T.GUTTER),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=24,
                controls=[
                    self._footer_ocr,
                    self._footer_arduino,
                    self._footer_web,
                ],
            ),
        )

    def _on_click_start(self, _e: ft.ControlEvent) -> None:
        ok, err = self.state.start_capture()
        if not ok and err:
            # 사용자 입력 누락 같은 케이스는 에러가 아니라 *경고* 라 주황 스낵바로
            # 가볍게 안내한다. 화면을 막는 모달 대신 하단에 띄우고 사용자가 바로
            # 다시 시도할 수 있게 한다.
            if self.page is not None:
                show_snack(self.page, err, severity="warning")
            else:
                self._show_snack(err, error=True)
        self._refresh_topbar_state()
        self._refresh_footer()

    def _on_click_stop(self, _e: ft.ControlEvent) -> None:
        self.state.stop_capture()
        self._refresh_topbar_state()
        self._refresh_footer()

    def _refresh_topbar_state(self) -> None:
        running = self.state.is_running()
        if self._topbar_btn_start is not None:
            self._topbar_btn_start.disabled = running
            self._topbar_btn_start.content = "Starting..." if running else "Start"
        if self._topbar_btn_stop is not None:
            self._topbar_btn_stop.disabled = not running

    def _refresh_footer(self) -> None:
        running = self.state.is_running()
        det = self.state.settings.detection
        web = self.state.settings.web

        # OCR: rapidocr 사용 안함 → offline / 사용 → idle / 사용 + 송출 → online
        if det.keyword_ocr_enabled:
            ocr_status = STATUS_ONLINE if running else STATUS_IDLE
        else:
            ocr_status = STATUS_OFFLINE
        self._update_status_row(
            self._footer_ocr,
            status=ocr_status,
            label=f"OCR: {ocr_status.capitalize()}",
        )

        # Arduino: 연결 안됨 → offline / 연결 → idle / 연결 + 송출 → online
        if self.state.arduino_active():
            ard_status = STATUS_ONLINE if running else STATUS_IDLE
        else:
            ard_status = STATUS_OFFLINE
        self._update_status_row(
            self._footer_arduino,
            status=ard_status,
            label=f"Arduino: {ard_status.capitalize()}",
        )

        # Web: 사용체크 안됨 → offline / 체크 → idle / 체크 + 송출 → online
        if web.enabled:
            web_status = STATUS_ONLINE if running else STATUS_IDLE
        else:
            web_status = STATUS_OFFLINE
        self._update_status_row(
            self._footer_web,
            status=web_status,
            label=f"Web: {web_status.capitalize()}",
        )

    @staticmethod
    def _update_status_row(row: Optional[ft.Row], *, status: str, label: str) -> None:
        if row is None or not row.controls:
            return
        dot_color, glow, accent = _STATUS_DOT_COLOR.get(
            status, _STATUS_DOT_COLOR[STATUS_OFFLINE]
        )
        dot, text = row.controls[0], row.controls[1]
        if isinstance(dot, ft.Container):
            dot.bgcolor = dot_color
            dot.shadow = ft.BoxShadow(
                blur_radius=8, color=glow, offset=ft.Offset(0, 0)
            )
        if isinstance(text, ft.Text):
            text.value = label
            text.color = T.ON_SURFACE if accent else T.ON_SURFACE_VARIANT
            text.weight = ft.FontWeight.BOLD if accent else None

    def _show_snack(self, message: str, *, error: bool = False) -> None:
        if self.page is None:
            return
        snack = ft.SnackBar(
            content=ft.Text(message, color=T.ON_PRIMARY if not error else T.ON_ERROR),
            bgcolor=T.PRIMARY if not error else T.ERROR,
        )
        self.page.snack_bar = snack
        snack.open = True
        self.page.update()

    def attach(self, page: ft.Page) -> None:
        self.page = page
        page.title = "Oddments"
        page.bgcolor = T.BACKGROUND
        page.padding = 0
        page.theme = T.theme()
        page.fonts = T.fonts()
        win = getattr(page, "window", None)
        if win is not None:
            try:
                # 폭/높이는 ``main._apply_window_settings`` 가 저장된 값(또는
                # 기본값)으로 이미 셋업했으므로 여기서는 최소 크기 가드만 둔다.
                # (예전엔 매번 1280×820 으로 강제 덮어써 사용자가 늘려둔 창 크기가
                # 다음 부팅에 사라지는 문제가 있었다.)
                win.min_width = 1024
                win.min_height = 720
            except Exception:
                pass

        def _on_state() -> None:
            self._refresh_topbar_state()
            self._refresh_footer()
            try:
                if self.page is not None:
                    self.page.update()
            except Exception:
                pass

        self.state.add_state_listener(_on_state)

        layout = ft.Row(
            controls=[
                self._sidebar,
                ft.Container(
                    expand=True,
                    bgcolor=T.SURFACE_BRIGHT,
                    content=ft.Column(
                        controls=[
                            self._topbar,
                            self._page_container,
                            self._footer,
                        ],
                        spacing=0,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )

        self._render_current_page()
        self._refresh_topbar_state()
        self._refresh_footer()

        page.add(layout)
        try:
            page.update()
        except Exception:
            pass


def ocr_runtime_ok_safe() -> tuple[bool, str]:
    try:
        from detection import ocr_runtime_ok

        return ocr_runtime_ok()
    except Exception as exc:
        return False, str(exc)


__all__ = [
    "StreamMasterApp",
    "ROUTE_DASHBOARD",
    "ROUTE_OCR",
    "ROUTE_ARDUINO",
    "ROUTE_WEB",
]
