"""통합 로그 — OCR / Arduino / 웹 / 원격 탭."""

from __future__ import annotations

import threading

import flet as ft

from ..components import LogConsole, outline_button, stream_log_panel
from ..log_buffers import get_log_store, reset_app_log
from ..state import (
    AppState,
    get_ocr_call_total,
    reset_ocr_log,
    reset_remote_log,
    reset_web_log,
)
from ..theme import (
    StreamMasterTheme as T,
    headline_sm,
    label_lg,
    label_md,
    title_md,
)

_prev_logs_ctrl: _LogsHubController | None = None

# Log 페이지 탭 선택 (세션 메모리만, 설정 저장 안 함)
_LOG_PAGE_SELECTED_TAB_INDEX: int = 0


class _LogsHubController:
    """네 종류 로그 버퍼를 한 스레드에서 폴링한다."""

    def __init__(self, state: AppState) -> None:
        self.state = state
        self.page: ft.Page | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.log_ocr: LogConsole | None = None
        self.log_arduino: LogConsole | None = None
        self.log_web: LogConsole | None = None
        self.log_remote: LogConsole | None = None
        self.log_app: LogConsole | None = None
        self.stats_ocr: ft.Text | None = None
        self.viewer_web: ft.Text | None = None
        self._mounted = False
        self._cur_ocr = 0
        self._cur_arduino = 0
        self._cur_web = 0
        self._cur_remote = 0
        self._cur_app = 0
        self._last_ocr_total: int = -1
        self._last_viewers: int = -1

    def prefill_all(self) -> None:
        store = get_log_store()
        if self.log_ocr:
            snap, c = store.ocr.attach()
            self._cur_ocr = c
            if snap:
                self.log_ocr.append_many(snap)
        if self.log_arduino:
            snap, c = store.arduino.attach()
            self._cur_arduino = c
            if snap:
                self.log_arduino.append_many(snap)
        if self.log_web:
            snap, c = store.web.attach()
            self._cur_web = c
            if snap:
                self.log_web.append_many(snap)
        if self.log_remote:
            snap, c = store.remote.attach()
            self._cur_remote = c
            if snap:
                self.log_remote.append_many(snap)
        if self.log_app:
            snap, c = store.app.attach()
            self._cur_app = c
            if snap:
                self.log_app.append_many(snap)

    def start(self, page: ft.Page) -> None:
        self.page = page
        self._mounted = True
        if self._thread is not None:
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                self._tick()
                if self._stop.wait(0.2):
                    return

        self._thread = threading.Thread(
            target=_loop, name="flet-logs-hub", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._mounted = False
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def _tick(self) -> None:
        if not self._mounted:
            return
        page = self.page
        if page is None:
            return
        store = get_log_store()

        lines_o, self._cur_ocr = store.ocr.read_since(self._cur_ocr)
        lines_a, self._cur_arduino = store.arduino.read_since(self._cur_arduino)
        lines_w, self._cur_web = store.web.read_since(self._cur_web)
        lines_r, self._cur_remote = store.remote.read_since(self._cur_remote)
        lines_app, self._cur_app = store.app.read_since(self._cur_app)

        total = get_ocr_call_total()
        ocr_stat_chg = total != self._last_ocr_total
        viewers = int(self.state.get_web_viewer_count())
        vw_chg = viewers != self._last_viewers

        if not (
            lines_o
            or lines_a
            or lines_w
            or lines_r
            or lines_app
            or ocr_stat_chg
            or vw_chg
        ):
            return

        self._last_ocr_total = total
        self._last_viewers = viewers

        lo, la, lw, lr, lapp = lines_o, lines_a, lines_w, lines_r, lines_app
        stats_val = f"OCR API 완료 호출: {total}회"

        async def _apply(
            _lo=lo,
            _la=la,
            _lw=lw,
            _lr=lr,
            _lapp=lapp,
            _stats=stats_val,
            _vw=viewers,
            _vw_ch=vw_chg,
            _ocr_sc=ocr_stat_chg,
        ) -> None:
            try:
                if _lo and self.log_ocr:
                    self.log_ocr.append_many(_lo)
                    self.log_ocr.flush(page)
                if _la and self.log_arduino:
                    self.log_arduino.append_many(_la)
                    self.log_arduino.flush(page)
                if _lw and self.log_web:
                    self.log_web.append_many(_lw)
                    self.log_web.flush(page)
                if _lr and self.log_remote:
                    self.log_remote.append_many(_lr)
                    self.log_remote.flush(page)
                if _lapp and self.log_app:
                    self.log_app.append_many(_lapp)
                    self.log_app.flush(page)
                if _ocr_sc and self.stats_ocr:
                    self.stats_ocr.value = _stats
                    try:
                        self.stats_ocr.update()
                    except Exception:
                        pass
                if _vw_ch and self.viewer_web:
                    self.viewer_web.value = str(_vw)
                    try:
                        self.viewer_web.update()
                    except Exception:
                        pass
                try:
                    page.update()
                except Exception:
                    pass
            except Exception:
                pass

        try:
            page.run_task(_apply)
        except Exception:
            self._mounted = False


def build_logs(state: AppState) -> ft.Control:
    global _prev_logs_ctrl

    if _prev_logs_ctrl is not None:
        _prev_logs_ctrl.shutdown()
        _prev_logs_ctrl = None

    ctrl = _LogsHubController(state)

    stats_ocr = ft.Text(
        f"OCR API 완료 호출: {get_ocr_call_total()}회",
        style=label_md(),
        color=T.ON_SURFACE_VARIANT,
    )
    autoscroll_ocr = ft.Checkbox(
        label="맨 아래 자동 스크롤",
        value=True,
        active_color=T.PRIMARY,
        label_style=label_md(),
    )
    btn_ocr_clear = outline_button("초기화", on_click=lambda _e: None)

    log_ocr, card_ocr = stream_log_panel(
        title="OCR 로그",
        placeholder="OCR 로그가 여기에 표시됩니다.",
        actions=[btn_ocr_clear],
        controls_above_console=[
            ft.Row(
                spacing=24,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[stats_ocr, autoscroll_ocr],
            ),
        ],
    )

    def _toggle_ocr_scroll(_e: ft.ControlEvent) -> None:
        log_ocr.set_autoscroll(autoscroll_ocr.value or False)

    autoscroll_ocr.on_change = _toggle_ocr_scroll

    def _clear_ocr(_e: ft.ControlEvent) -> None:
        reset_ocr_log()
        st = get_log_store()
        ctrl._cur_ocr = st.ocr.clear()
        log_ocr.clear()
        stats_ocr.value = "OCR API 완료 호출: 0회"
        if log_ocr.page is not None:
            log_ocr.update()
        if stats_ocr.page is not None:
            try:
                stats_ocr.update()
            except Exception:
                pass

    btn_ocr_clear.on_click = _clear_ocr

    autoscroll_ard = ft.Checkbox(
        label="맨 아래 자동 스크롤",
        value=True,
        active_color=T.PRIMARY,
        label_style=label_md(),
    )
    btn_ard_clear = outline_button("로그 비우기", on_click=lambda _e: None)

    log_arduino, card_arduino = stream_log_panel(
        title="아두이노 로그",
        icon=ft.Icons.TERMINAL,
        placeholder="아두이노 로그가 여기에 표시됩니다.",
        actions=[btn_ard_clear],
        description="[KB] PC 키 이벤트, [RX] 시리얼 수신, [상태] 알림.",
        controls_above_console=[autoscroll_ard],
    )

    autoscroll_ard.on_change = lambda _e: log_arduino.set_autoscroll(
        autoscroll_ard.value or False
    )

    def _clear_arduino(_e: ft.ControlEvent) -> None:
        st = get_log_store()
        ctrl._cur_arduino = st.arduino.clear()
        log_arduino.clear()
        if log_arduino.page is not None:
            log_arduino.update()

    btn_ard_clear.on_click = _clear_arduino

    viewer_web = ft.Text(
        "0", style=headline_sm(), color=T.PRIMARY, weight=ft.FontWeight.BOLD
    )
    autoscroll_web = ft.Checkbox(
        label="맨 아래 자동 스크롤",
        value=True,
        active_color=T.PRIMARY,
        label_style=label_md(),
    )
    btn_web_clear = outline_button("초기화", on_click=lambda _e: None)

    web_stats_row = ft.Container(
        padding=ft.padding.only(bottom=8),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(
                    spacing=8,
                    controls=[
                        ft.Icon(ft.Icons.GROUP, color=T.PRIMARY, size=20),
                        ft.Text(
                            "현재 시청 연결",
                            style=title_md(),
                            color=T.ON_SURFACE,
                        ),
                    ],
                ),
                viewer_web,
            ],
        ),
    )

    log_web, card_web = stream_log_panel(
        title="웹 로그",
        icon=ft.Icons.TERMINAL,
        placeholder="뷰어 접속·WebRTC 연결 이벤트가 여기에 표시됩니다.",
        actions=[autoscroll_web, btn_web_clear],
        controls_above_console=[web_stats_row],
    )

    autoscroll_web.on_change = lambda _e: log_web.set_autoscroll(
        autoscroll_web.value or False
    )

    def _clear_web(_e: ft.ControlEvent) -> None:
        reset_web_log()
        st = get_log_store()
        ctrl._cur_web = st.web.clear()
        log_web.clear()
        if log_web.page is not None:
            log_web.update()

    btn_web_clear.on_click = _clear_web

    autoscroll_rem = ft.Checkbox(
        label="맨 아래 자동 스크롤",
        value=True,
        active_color=T.PRIMARY,
        label_style=label_md(),
    )
    btn_rem_clear = outline_button("초기화", on_click=lambda _e: None)

    log_remote, card_remote = stream_log_panel(
        title="원격 로그",
        icon=ft.Icons.TERMINAL,
        placeholder="호스트·뷰어 연결, 캡처, WebRTC 상태가 여기에 표시됩니다.",
        actions=[autoscroll_rem, btn_rem_clear],
    )

    autoscroll_rem.on_change = lambda _e: log_remote.set_autoscroll(
        autoscroll_rem.value or False
    )

    def _clear_remote(_e: ft.ControlEvent) -> None:
        reset_remote_log()
        st = get_log_store()
        ctrl._cur_remote = st.remote.clear()
        log_remote.clear()
        if log_remote.page is not None:
            log_remote.update()

    btn_rem_clear.on_click = _clear_remote

    autoscroll_app = ft.Checkbox(
        label="맨 아래 자동 스크롤",
        value=True,
        active_color=T.PRIMARY,
        label_style=label_md(),
    )
    btn_app_clear = outline_button("초기화", on_click=lambda _e: None)

    log_app, card_app = stream_log_panel(
        title="앱 / 전역",
        icon=ft.Icons.BUG_REPORT_OUTLINED,
        placeholder=(
            "전역 예외·asyncio 경고·세그폴트 직전 스택은 "
            "logs/app_error-*.log · logs/python-faulthandler-*.log 에도 남습니다."
        ),
        actions=[autoscroll_app, btn_app_clear],
    )

    autoscroll_app.on_change = lambda _e: log_app.set_autoscroll(
        autoscroll_app.value or False
    )

    def _clear_app(_e: ft.ControlEvent) -> None:
        reset_app_log()
        st = get_log_store()
        ctrl._cur_app = st.app.clear()
        log_app.clear()
        if log_app.page is not None:
            log_app.update()

    btn_app_clear.on_click = _clear_app

    ctrl.log_ocr = log_ocr
    ctrl.log_arduino = log_arduino
    ctrl.log_web = log_web
    ctrl.log_remote = log_remote
    ctrl.log_app = log_app
    ctrl.stats_ocr = stats_ocr
    ctrl.viewer_web = viewer_web
    ctrl.prefill_all()

    tab_ocr = ft.Container(
        expand=True,
        padding=ft.padding.only(top=T.SPACE_SM),
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=T.SPACE_MD,
            controls=[card_ocr],
        ),
    )
    tab_arduino = ft.Container(
        expand=True,
        padding=ft.padding.only(top=T.SPACE_SM),
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=T.SPACE_MD,
            controls=[card_arduino],
        ),
    )
    tab_web = ft.Container(
        expand=True,
        padding=ft.padding.only(top=T.SPACE_SM),
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=T.SPACE_MD,
            controls=[card_web],
        ),
    )
    tab_remote = ft.Container(
        expand=True,
        padding=ft.padding.only(top=T.SPACE_SM),
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=T.SPACE_MD,
            controls=[card_remote],
        ),
    )
    tab_app = ft.Container(
        expand=True,
        padding=ft.padding.only(top=T.SPACE_SM),
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=T.SPACE_MD,
            controls=[card_app],
        ),
    )

    _tab_count = 5
    _sel = max(0, min(_tab_count - 1, _LOG_PAGE_SELECTED_TAB_INDEX))

    def _on_tabs_change(e: ft.ControlEvent) -> None:
        global _LOG_PAGE_SELECTED_TAB_INDEX
        try:
            idx = int(e.data)
        except (TypeError, ValueError):
            return
        if 0 <= idx < _tab_count:
            _LOG_PAGE_SELECTED_TAB_INDEX = idx

    tabs = ft.Tabs(
        length=_tab_count,
        selected_index=_sel,
        expand=False,
        height=620,
        on_change=_on_tabs_change,
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=[
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="OCR", icon=ft.Icons.VISIBILITY_OUTLINED),
                        ft.Tab(label="Arduino", icon=ft.Icons.MEMORY),
                        ft.Tab(
                            label="Web",
                            icon=ft.Icons.SETTINGS_INPUT_ANTENNA,
                        ),
                        ft.Tab(
                            label="Remote",
                            icon=ft.Icons.SCREEN_SHARE_OUTLINED,
                        ),
                        ft.Tab(
                            label="앱",
                            icon=ft.Icons.BUG_REPORT_OUTLINED,
                        ),
                    ],
                ),
                ft.TabBarView(
                    expand=True,
                    controls=[tab_ocr, tab_arduino, tab_web, tab_remote, tab_app],
                ),
            ],
        ),
    )

    page_root = ft.Column(
        spacing=T.GUTTER,
        expand=True,
        controls=[
            ft.Text("Log", style=headline_sm(), color=T.ON_SURFACE),
            ft.Text(
                "OCR·아두이노·웹 송출·원격·앱(전역 예외) 로그를 한 화면에서 확인합니다.",
                style=label_lg(),
                color=T.ON_SURFACE_VARIANT,
            ),
            tabs,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        scroll=ft.ScrollMode.AUTO,
    )

    def _on_mount(e: ft.ControlEvent) -> None:
        if e.page is not None:
            ctrl.start(e.page)

    page_root.on_mount = _on_mount  # type: ignore[attr-defined]

    _prev_logs_ctrl = ctrl
    page_obj = getattr(state, "page", None)
    if isinstance(page_obj, ft.Page):
        ctrl.start(page_obj)

    return page_root


def shutdown_logs_page_poller_if_any() -> None:
    global _prev_logs_ctrl

    if _prev_logs_ctrl is not None:
        _prev_logs_ctrl.shutdown()
        _prev_logs_ctrl = None


__all__ = ["build_logs", "shutdown_logs_page_poller_if_any"]
