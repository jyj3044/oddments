"""OCR Settings 페이지 — 키워드, 전처리 변형, 템플릿, 임계값, 로그."""

from __future__ import annotations

import threading

import flet as ft

from ..components import (
    LogConsole,
    ensure_file_picker,
    outline_button,
    section_card,
    show_snack,
    stream_log_panel,
    text_field,
)
from ..log_buffers import get_log_store
from ..state import (
    AppState,
    OCR_VARIANT_UI_CHOICES,
    get_ocr_call_total,
    reset_ocr_log,
)
from ..theme import (
    StreamMasterTheme as T,
    body_md,
    label_lg,
    label_md,
)

# OCR 탭 재진입 시 이전 폴링 스레드 정리. (Flet 0.85 Column 은 on_mount 가 거의 오지 않음)
_prev_ocr_log_ctrl: _OcrPageController | None = None

class _OcrPageController:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.page: ft.Page | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.log_console: LogConsole | None = None
        self.stats_text: ft.Text | None = None
        self._mounted = False
        self._last_total: int = -1
        # 중앙 로그 버퍼에서 마지막으로 읽은 절대 인덱스
        self._log_cursor: int = 0

    def prefill_log(self) -> None:
        """페이지 빌드 직후 호출. 중앙 버퍼에 누적된 라인을 즉시 LogConsole 에 반영."""
        log = self.log_console
        if log is None:
            return
        snapshot, cursor = get_log_store().ocr.attach()
        self._log_cursor = cursor
        if snapshot:
            log.append_many(snapshot)

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
            target=_loop, name="flet-ocr-log", daemon=True
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
        if not self._mounted or self.log_console is None:
            return
        lines, new_cursor = get_log_store().ocr.read_since(self._log_cursor)
        total = get_ocr_call_total()
        counter_changed = total != self._last_total
        if not lines and not counter_changed:
            return
        self._log_cursor = new_cursor
        self._last_total = total
        page = self.page
        if page is None:
            return

        log = self.log_console
        stats = self.stats_text
        stats_value = f"OCR API 완료 호출: {total}회"

        async def _apply(_lines=lines, _stats=stats_value) -> None:
            try:
                if _lines and log is not None:
                    log.append_many(_lines)
                    log.flush(page)
                if stats is not None:
                    stats.value = _stats
                    try:
                        stats.update()
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


def build_ocr_settings(state: AppState) -> ft.Control:
    global _prev_ocr_log_ctrl

    if _prev_ocr_log_ctrl is not None:
        _prev_ocr_log_ctrl.shutdown()
        _prev_ocr_log_ctrl = None

    det = state.settings.detection
    ctrl = _OcrPageController(state)

    keyword_field = text_field(
        value=det.keywords,
        expand=True,
        hint="키워드를 쉼표로 구분해 입력",
    )

    def _push_detection_cfg() -> None:
        state._sync_cfg_from_settings()

    def _on_keyword_change(_e: ft.ControlEvent) -> None:
        det.keywords = keyword_field.value or ""
        _push_detection_cfg()

    keyword_field.on_change = _on_keyword_change

    keyword_ocr_cb = ft.Checkbox(
        label="OCR 사용 (rapidOCR)",
        value=det.keyword_ocr_enabled,
        on_change=lambda _e: None,
        active_color=T.PRIMARY,
        label_style=body_md(),
    )

    ocr_dependent_controls: list[ft.Control] = []

    detection_card = section_card(
        content=ft.Column(
            spacing=T.SPACE_MD,
            controls=[
                keyword_ocr_cb,
                ft.Column(
                    spacing=T.SPACE_SM,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        ft.Text(
                            "알림 키워드(쉼표구분)",
                            style=label_lg(),
                            color=T.ON_SURFACE,
                        ),
                        keyword_field,
                    ],
                ),
            ],
        ),
    )

    selected_variants = set(det.ocr_variant_groups)
    variant_checks: dict[str, ft.Checkbox] = {}

    def _on_variant_change(_e: ft.ControlEvent, vid: str) -> None:
        cb = variant_checks[vid]
        if cb.value:
            selected_variants.add(vid)
        else:
            selected_variants.discard(vid)
        det.ocr_variant_groups = tuple(
            vid for vid, _ in OCR_VARIANT_UI_CHOICES if vid in selected_variants
        )
        _push_detection_cfg()

    variant_grid = ft.ResponsiveRow(
        spacing=8, run_spacing=8, columns=12, controls=[]
    )
    for vid, label in OCR_VARIANT_UI_CHOICES:
        cb = ft.Checkbox(
            label=label,
            value=(vid in selected_variants),
            on_change=lambda e, _vid=vid: _on_variant_change(e, _vid),
            active_color=T.PRIMARY,
            label_style=body_md(),
        )
        variant_checks[vid] = cb
        variant_grid.controls.append(
            ft.Container(col={"xs": 12, "md": 6, "lg": 4}, content=cb)
        )

    ocr_dependent_controls.extend(variant_checks.values())

    variant_card = section_card(
        title="OCR 전처리 변형",
        description=(
            "전부 체크이면 모든 변형을 사용합니다. "
            "체크가 하나도 없으면 키워드 OCR(전처리 변형)은 호출하지 않습니다."
        ),
        content=variant_grid,
    )

    tpl_field = text_field(
        value=";".join(det.template_paths),
        expand=True,
        hint="C:/path/to/template1.png;C:/path/to/template2.png",
    )

    def _on_tpl_change(_e: ft.ControlEvent) -> None:
        raw = tpl_field.value or ""
        det.template_paths = tuple(
            p.strip() for p in raw.replace("\n", ";").split(";") if p.strip()
        )
        _push_detection_cfg()

    tpl_field.on_change = _on_tpl_change

    threshold_field = text_field(
        value=f"{det.template_threshold:.2f}",
        width=96,
        text_align=ft.TextAlign.RIGHT,
        keyboard_type=ft.KeyboardType.NUMBER,
    )

    def _on_threshold_change(_e: ft.ControlEvent) -> None:
        try:
            det.template_threshold = max(
                0.0, min(1.0, float(threshold_field.value or "0.8"))
            )
            _push_detection_cfg()
        except ValueError:
            pass

    threshold_field.on_change = _on_threshold_change

    cooldown_field = text_field(
        value=f"{det.cooldown_sec:.1f}",
        width=96,
        text_align=ft.TextAlign.RIGHT,
        keyboard_type=ft.KeyboardType.NUMBER,
    )

    def _on_cooldown_change(_e: ft.ControlEvent) -> None:
        try:
            det.cooldown_sec = max(0.0, float(cooldown_field.value or "3.0"))
        except ValueError:
            pass

    cooldown_field.on_change = _on_cooldown_change

    tpl_add_btn = outline_button(
        "추가...",
        on_click=lambda _e: _pick_template(state, tpl_field, det),
    )
    tpl_clr_btn = outline_button(
        "비우기",
        on_click=lambda _e: _clear_template(state, tpl_field, det),
    )

    template_card = section_card(
        title="템플릿 매칭",
        content=ft.Column(
            spacing=T.SPACE_MD,
            controls=[
                ft.Row(
                    controls=[
                        ft.Column(
                            spacing=4,
                            expand=True,
                            controls=[
                                ft.Text(
                                    "템플릿 경로",
                                    style=label_lg(),
                                    color=T.ON_SURFACE,
                                ),
                                tpl_field,
                                ft.Text(
                                    "여러 장: 세미콜론(;)으로 구분",
                                    style=label_md(),
                                    color=T.ON_SURFACE_VARIANT,
                                ),
                            ],
                        ),
                        ft.Column(
                            spacing=8,
                            width=120,
                            controls=[
                                tpl_add_btn,
                                tpl_clr_btn,
                            ],
                        ),
                    ],
                    spacing=24,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                ft.Row(
                    spacing=24,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Row(
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Container(
                                    bgcolor=T.SURFACE_CONTAINER_LOWEST,
                                    padding=ft.padding.symmetric(
                                        horizontal=8, vertical=4
                                    ),
                                    border_radius=T.RADIUS_SM,
                                    content=ft.Text(
                                        "매칭 임계값",
                                        style=label_lg(),
                                        color=T.ON_SURFACE,
                                    ),
                                ),
                                threshold_field,
                            ],
                        ),
                        ft.Row(
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Container(
                                    bgcolor=T.SURFACE_CONTAINER_LOWEST,
                                    padding=ft.padding.symmetric(
                                        horizontal=8, vertical=4
                                    ),
                                    border_radius=T.RADIUS_SM,
                                    content=ft.Text(
                                        "쿨다운(초)",
                                        style=label_lg(),
                                        color=T.ON_SURFACE,
                                    ),
                                ),
                                cooldown_field,
                            ],
                        ),
                    ],
                ),
            ],
        ),
    )

    ocr_dependent_controls.extend(
        [tpl_field, tpl_add_btn, tpl_clr_btn, threshold_field]
    )

    def _sync_ocr_dependent_ui() -> None:
        on = det.keyword_ocr_enabled
        for c in ocr_dependent_controls:
            c.disabled = not on
            try:
                c.update()
            except Exception:
                pass

    def _on_keyword_ocr_change(e: ft.ControlEvent) -> None:
        det.keyword_ocr_enabled = bool(e.control.value)
        state._sync_cfg_from_settings()
        state._notify_state()
        _sync_ocr_dependent_ui()

    keyword_ocr_cb.on_change = _on_keyword_ocr_change
    _sync_ocr_dependent_ui()

    stats_text = ft.Text(
        f"OCR API 완료 호출: {get_ocr_call_total()}회",
        style=body_md(),
        color=T.ON_SURFACE_VARIANT,
    )
    autoscroll_cb = ft.Checkbox(
        label="맨 아래 자동 스크롤",
        value=True,
        active_color=T.PRIMARY,
        label_style=label_md(),
    )

    btn_ocr_log_init = outline_button("초기화", on_click=lambda _e: None)

    log_console, log_card = stream_log_panel(
        title="OCR 로그",
        placeholder="OCR 로그가 여기에 표시됩니다.",
        actions=[btn_ocr_log_init],
        controls_above_console=[
            ft.Row(
                spacing=24,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[stats_text, autoscroll_cb],
            ),
        ],
    )

    def _toggle_autoscroll(_e: ft.ControlEvent) -> None:
        log_console.set_autoscroll(autoscroll_cb.value or False)

    autoscroll_cb.on_change = _toggle_autoscroll

    def _reset_ocr_log_panel(_e: ft.ControlEvent) -> None:
        reset_ocr_log()
        store = get_log_store()
        new_cursor = store.ocr.clear()
        ctrl._log_cursor = new_cursor
        log_console.clear()
        stats_text.value = "OCR API 완료 호출: 0회"
        if log_console.page is not None:
            log_console.update()
        if stats_text.page is not None:
            try:
                stats_text.update()
            except Exception:
                pass

    btn_ocr_log_init.on_click = _reset_ocr_log_panel

    ctrl.log_console = log_console
    ctrl.stats_text = stats_text
    ctrl.prefill_log()

    page_root = ft.Column(
        spacing=T.SPACE_LG,
        controls=[
            detection_card,
            variant_card,
            template_card,
            log_card,
        ],
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        scroll=ft.ScrollMode.AUTO,
    )

    def _on_mount(e: ft.ControlEvent) -> None:
        if e.page is not None:
            ctrl.start(e.page)

    page_root.on_mount = _on_mount  # type: ignore[attr-defined]

    _prev_ocr_log_ctrl = ctrl
    page_obj = getattr(state, "page", None)
    if isinstance(page_obj, ft.Page):
        ctrl.start(page_obj)

    return page_root


def _pick_template(state: AppState, tpl_field: ft.TextField, det) -> None:
    """Flet 0.85+ ``FilePicker.pick_files`` async API 에 맞춰 코루틴으로 실행."""
    page = getattr(state, "page", None)
    if page is None:
        return

    async def _async_pick() -> None:
        from ..log_buffers import log_app_event

        try:
            # FilePicker 는 Flet 0.85 의 Service 라 page.services 에 등록해야 한다.
            fp = ensure_file_picker(page)
            log_app_event("INFO", "pick_files start (template)")
            files = await fp.pick_files(
                dialog_title="템플릿 이미지 선택",
                allow_multiple=True,
                allowed_extensions=["png", "jpg", "jpeg", "bmp"],
            )
            log_app_event(
                "INFO",
                f"pick_files done (template, count={len(files) if files else 0})",
            )
            if not files:
                return
            existing = list(det.template_paths) if det.template_paths else []
            for f in files:
                path = getattr(f, "path", None)
                if path and path not in existing:
                    existing.append(path)
            det.template_paths = tuple(existing)
            tpl_field.value = ";".join(existing)
            try:
                if tpl_field.page is not None:
                    tpl_field.update()
            except Exception:
                pass
            try:
                state._sync_cfg_from_settings()
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            import traceback as _tb

            log_app_event(
                "ERROR",
                f"pick_files failed (template): {exc}",
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
            f"run_task(pick_files template) 실패: {exc}",
            detail=_tb.format_exc(),
        )
        show_snack(page, f"파일 선택기 호출 실패: {exc}", error=True)


def _clear_template(
    state: AppState, tpl_field: ft.TextField, det
) -> None:
    det.template_paths = ()
    tpl_field.value = ""
    if tpl_field.page is not None:
        tpl_field.update()
    state._sync_cfg_from_settings()


def shutdown_ocr_log_poller_if_any() -> None:
    global _prev_ocr_log_ctrl

    if _prev_ocr_log_ctrl is not None:
        _prev_ocr_log_ctrl.shutdown()
        _prev_ocr_log_ctrl = None


__all__ = ["build_ocr_settings", "shutdown_ocr_log_poller_if_any"]
