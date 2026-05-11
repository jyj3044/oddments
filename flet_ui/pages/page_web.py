"""Web Stream 페이지 — WebRTC, HTTPS, 미디어 품질, 시청자."""

from __future__ import annotations

import threading

import flet as ft

from ..components import (
    dropdown,
    ensure_file_picker,
    field_label,
    outline_button,
    section_card,
    set_clipboard,
    show_snack,
    text_field,
)
from ..state import AppState
from ..theme import (
    StreamMasterTheme as T,
    headline_sm,
    label_lg,
    label_md,
    title_md,
)

_prev_web_viewer_ctrl: _WebViewerPoller | None = None


class _WebViewerPoller:
    """시청 연결 수 표시만 주기적으로 갱신한다."""

    def __init__(self, state: AppState) -> None:
        self.state = state
        self.page: ft.Page | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.viewer_count_text: ft.Text | None = None
        self._mounted = False
        self._last_viewer_count: int = -1

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
            target=_loop, name="flet-web-log", daemon=True
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
        if not self._mounted or self.viewer_count_text is None:
            return
        viewers = int(self.state.get_web_viewer_count())
        if viewers == self._last_viewer_count:
            return
        self._last_viewer_count = viewers
        page = self.page
        if page is None:
            return

        viewer_text = self.viewer_count_text

        async def _apply(_viewers=viewers) -> None:
            try:
                if viewer_text is not None:
                    viewer_text.value = str(_viewers)
                    try:
                        viewer_text.update()
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


def build_web_stream(state: AppState) -> ft.Control:
    global _prev_web_viewer_ctrl

    if _prev_web_viewer_ctrl is not None:
        _prev_web_viewer_ctrl.shutdown()
        _prev_web_viewer_ctrl = None

    web = state.settings.web
    ctrl = _WebViewerPoller(state)

    enable_cb = ft.Checkbox(
        label="웹 송출(WebRTC) 사용",
        value=web.enabled,
        active_color=T.PRIMARY,
        on_change=lambda e: _set_enable(state, e.control.value),
        label_style=label_lg(),
    )

    port_field = text_field(
        value=str(web.port),
        width=110,
        keyboard_type=ft.KeyboardType.NUMBER,
    )

    def _on_port_change(_e: ft.ControlEvent) -> None:
        try:
            web.port = max(1, min(65535, int(port_field.value or "8787")))
        except ValueError:
            pass

    port_field.on_change = _on_port_change

    def _copy_url(_e: ft.ControlEvent) -> None:
        page = getattr(state, "page", None)
        if page is None:
            return

        async def _do() -> None:
            import asyncio

            try:
                # 외부 echo 서비스 HTTP 호출은 최대 ~수 초 블로킹 가능 → 워커 스레드로.
                # 60초 캐시 덕분에 두 번째 호출부터는 즉시 반환된다.
                url = await asyncio.to_thread(state.get_public_web_url)
            except Exception as exc:  # noqa: BLE001
                show_snack(page, f"공인 URL 조회 실패: {exc}", severity="warning")
                return
            ok = set_clipboard(page, url)
            if ok:
                show_snack(page, f"URL 복사됨: {url}")
            else:
                show_snack(page, f"클립보드 복사 실패. URL: {url}", error=True)

        try:
            page.run_task(_do)
        except Exception as exc:  # noqa: BLE001
            show_snack(page, f"URL 복사 실패: {exc}", error=True)

    copy_url_btn = outline_button(
        "URL 복사", icon=ft.Icons.CONTENT_COPY, on_click=_copy_url
    )

    https_cb = ft.Checkbox(
        label="HTTPS(TLS)로 송출",
        value=web.https,
        active_color=T.PRIMARY,
        on_change=lambda e: _set_https(state, e.control.value),
        label_style=label_lg(),
    )

    cert_field = text_field(
        value=web.ssl_cert,
        hint="Path to fullchain.pem",
        expand=True,
    )
    key_field = text_field(
        value=web.ssl_key,
        hint="Path to privkey.pem",
        expand=True,
    )

    def _on_cert_change(_e: ft.ControlEvent) -> None:
        web.ssl_cert = cert_field.value or ""

    def _on_key_change(_e: ft.ControlEvent) -> None:
        web.ssl_key = key_field.value or ""

    cert_field.on_change = _on_cert_change
    key_field.on_change = _on_key_change

    cert_pick = outline_button(
        "찾기...", on_click=lambda _e: _pick_pem(state, cert_field, "ssl_cert")
    )
    key_pick = outline_button(
        "찾기...", on_click=lambda _e: _pick_pem(state, key_field, "ssl_key")
    )

    connection_card = section_card(
        title="연결",
        icon=ft.Icons.SENSORS,
        expand=True,
        content=ft.Column(
            spacing=T.SPACE_MD,
            expand=True,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        enable_cb,
                        ft.Row(
                            spacing=8,
                            controls=[
                                field_label("포트"),
                                port_field,
                                copy_url_btn,
                            ],
                        ),
                    ],
                ),
                https_cb,
                ft.Container(
                    padding=ft.padding.only(left=24),
                    content=ft.Column(
                        spacing=T.SPACE_MD,
                        controls=[
                            ft.Row(
                                spacing=12,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    ft.Container(
                                        width=80, content=field_label("인증서")
                                    ),
                                    cert_field,
                                    cert_pick,
                                ],
                            ),
                            ft.Row(
                                spacing=12,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                controls=[
                                    ft.Container(
                                        width=80, content=field_label("개인키")
                                    ),
                                    key_field,
                                    key_pick,
                                ],
                            ),
                        ],
                    ),
                ),
            ],
        ),
    )

    audio_options = state.list_audio_outputs()
    if not audio_options:
        audio_options = [web.audio_output] if web.audio_output else []
    audio_dd = dropdown(
        value=web.audio_output or (audio_options[0] if audio_options else None),
        options=audio_options or ["(없음)"],
        expand=True,
    )

    def _on_audio_change(_e: ft.ControlEvent) -> None:
        if audio_dd.value:
            web.audio_output = audio_dd.value

    audio_dd.on_select = _on_audio_change

    def _on_audio_focus(_e: ft.ControlEvent) -> None:
        _refresh_audio(state, audio_dd)

    try:
        audio_dd.on_focus = _on_audio_focus  # type: ignore[attr-defined]
    except Exception:
        pass

    max_side_field = text_field(
        value=str(web.max_side),
        width=120,
        keyboard_type=ft.KeyboardType.NUMBER,
    )

    def _on_max_side_change(_e: ft.ControlEvent) -> None:
        try:
            web.max_side = max(0, int(max_side_field.value or "0"))
        except ValueError:
            pass

    max_side_field.on_change = _on_max_side_change

    presets = [
        ("원본", 0),
        ("720 HD", 720),
        ("1080 FHD", 1080),
        ("1440 QHD", 1440),
        ("2160 4K", 2160),
    ]

    def _set_preset(value: int) -> None:
        web.max_side = value
        max_side_field.value = str(value)
        if max_side_field.page is not None:
            max_side_field.update()

    preset_chips: list[ft.Control] = []
    for label, value in presets:
        chip = ft.Container(
            on_click=lambda _e, v=value: _set_preset(v),
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            bgcolor=T.SURFACE_CONTAINER,
            border=ft.border.all(1, T.OUTLINE_VARIANT),
            border_radius=T.RADIUS_SM,
            content=ft.Text(label, style=label_md(), color=T.ON_SURFACE_VARIANT),
        )
        preset_chips.append(chip)

    media_card = section_card(
        title="미디어 품질",
        icon=ft.Icons.TUNE,
        content=ft.Column(
            spacing=T.SPACE_MD,
            controls=[
                ft.Column(
                    spacing=8,
                    controls=[
                        field_label("송출 오디오"),
                        audio_dd,
                    ],
                ),
                ft.Column(
                    spacing=8,
                    controls=[
                        field_label("송출 화질(긴 변 최대 px)"),
                        max_side_field,
                        ft.Row(spacing=8, wrap=True, controls=preset_chips),
                    ],
                ),
            ],
        ),
    )

    viewer_count_text = ft.Text(
        str(int(state.get_web_viewer_count())),
        style=headline_sm(),
        color=T.PRIMARY,
        weight=ft.FontWeight.BOLD,
    )
    viewer_card = ft.Container(
        padding=T.SPACE_MD,
        bgcolor="#1a1a73e8",
        border=ft.border.all(1, "#331a73e8"),
        border_radius=T.RADIUS_MD,
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
                viewer_count_text,
            ],
        ),
    )

    ctrl.viewer_count_text = viewer_count_text

    right_column = ft.Column(
        spacing=T.GUTTER,
        controls=[media_card, viewer_card],
        expand=True,
    )

    top_row = ft.Row(
        controls=[
            ft.Container(content=connection_card, expand=2),
            ft.Container(content=right_column, expand=1),
        ],
        spacing=T.GUTTER,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        intrinsic_height=True,
    )

    page_root = ft.Column(
        controls=[
            top_row,
        ],
        spacing=T.GUTTER,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        scroll=ft.ScrollMode.AUTO,
    )

    def _on_mount(e: ft.ControlEvent) -> None:
        # Flet 0.85 Column 은 Dart 쪽에서 mount 이벤트를 보내지 않는 경우가 많다.
        # 아래에서 state.page 로 start 하므로 여기서는 보조로만 시도한다.
        if e.page is not None:
            ctrl.start(e.page)

    page_root.on_mount = _on_mount  # type: ignore[attr-defined]

    _prev_web_viewer_ctrl = ctrl
    page_obj = getattr(state, "page", None)
    if isinstance(page_obj, ft.Page):
        ctrl.start(page_obj)

    return page_root


def _set_enable(state: AppState, value: bool | None) -> None:
    state.settings.web.enabled = bool(value)
    state._notify_state()


def _set_https(state: AppState, value: bool | None) -> None:
    state.settings.web.https = bool(value)


def _refresh_audio(state: AppState, dd: ft.Dropdown) -> None:
    opts = state.list_audio_outputs()
    if not opts:
        opts = (
            [state.settings.web.audio_output]
            if state.settings.web.audio_output
            else []
        )
    dd.options = [ft.dropdown.Option(o) for o in opts]
    if dd.page is not None:
        dd.update()


def _pick_pem(state: AppState, target: ft.TextField, attr: str) -> None:
    """Flet 0.85+ : ``FilePicker.pick_files`` 가 async 이고 결과를 직접 반환한다.

    동기 버튼 핸들러에서 호출되므로 ``page.run_task`` 로 코루틴을 띄우고,
    각 단계의 실패는 ``logs/app-*.log`` 에 ERROR 한 줄로 남긴다.
    """
    page = getattr(state, "page", None)
    if page is None:
        return

    async def _async_pick() -> None:
        from ..log_buffers import log_app_event

        try:
            # FilePicker 는 Flet 0.85 의 Service 라 page.services 에 등록해야 한다.
            fp = ensure_file_picker(page)
            log_app_event("INFO", f"pick_files start (attr={attr})")
            files = await fp.pick_files(
                dialog_title="인증서/개인키 선택",
                allow_multiple=False,
                allowed_extensions=["pem", "crt", "key"],
            )
            log_app_event(
                "INFO",
                f"pick_files done (attr={attr}, count={len(files) if files else 0})",
            )
            if not files:
                return
            f = files[0]
            path = getattr(f, "path", None)
            if not path:
                show_snack(
                    page,
                    "이 환경에서는 파일 경로를 가져올 수 없습니다.",
                    error=True,
                )
                return
            target.value = path
            try:
                setattr(state.settings.web, attr, path)
            except Exception as exc:  # noqa: BLE001
                import traceback as _tb

                log_app_event(
                    "ERROR",
                    f"setattr web.{attr} failed: {exc}",
                    detail=_tb.format_exc(),
                )
            try:
                if target.page is not None:
                    target.update()
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            import traceback as _tb

            log_app_event(
                "ERROR",
                f"pick_files failed (attr={attr}): {exc}",
                detail=_tb.format_exc(),
            )
            show_snack(page, f"파일 선택기 호출 실패: {exc}", error=True)

    try:
        page.run_task(_async_pick)
    except Exception as exc:  # noqa: BLE001
        import traceback as _tb
        from ..log_buffers import log_app_event

        log_app_event(
            "ERROR",
            f"run_task(pick_files) 실패: {exc}",
            detail=_tb.format_exc(),
        )
        show_snack(page, f"파일 선택기 호출 실패: {exc}", error=True)


def shutdown_web_viewer_poller_if_any() -> None:
    global _prev_web_viewer_ctrl

    if _prev_web_viewer_ctrl is not None:
        _prev_web_viewer_ctrl.shutdown()
        _prev_web_viewer_ctrl = None


__all__ = ["build_web_stream", "shutdown_web_viewer_poller_if_any"]
