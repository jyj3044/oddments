"""OCR Settings 페이지 — 키워드, 전처리 변형, 템플릿, 임계값."""

from __future__ import annotations

import flet as ft

from ..components import (
    ensure_file_picker,
    outline_button,
    section_card,
    show_snack,
    text_field,
)
from ..state import (
    AppState,
    OCR_VARIANT_UI_CHOICES,
)
from ..theme import (
    StreamMasterTheme as T,
    body_md,
    label_lg,
    label_md,
)

def build_ocr_settings(state: AppState) -> ft.Control:
    det = state.settings.detection

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

    page_root = ft.Column(
        spacing=T.SPACE_LG,
        controls=[
            detection_card,
            variant_card,
            template_card,
        ],
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        scroll=ft.ScrollMode.AUTO,
    )

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


__all__ = ["build_ocr_settings"]
