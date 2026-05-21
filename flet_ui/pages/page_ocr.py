"""OCR Settings 페이지 — 메인 OCR과 특정영역 OCR 규칙 설정."""

from __future__ import annotations

import time
import threading

import cv2
import flet as ft
import numpy as np

from detection import RegionRect
from app_platform.region_selector import (
    ScreenPoint,
    ScreenRect,
    select_screen_point,
    select_screen_rect,
)
from capture import enumerate_monitors

from ..components import (
    ensure_file_picker,
    outline_button,
    primary_button,
    section_card,
    show_snack,
    text_field,
)
from ..state import (
    AppState,
    OCR_VARIANT_UI_CHOICES,
    RegionRuleSettings,
)
from ..theme import (
    StreamMasterTheme as T,
    body_md,
    label_lg,
    label_md,
)


def build_ocr_settings(state: AppState) -> ft.Control:
    det = state.settings.detection

    def _push_detection_cfg() -> None:
        state._sync_cfg_from_settings()

    keyword_field = text_field(
        value=det.keywords,
        expand=True,
        hint="키워드를 쉼표로 구분해 입력",
    )

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

    main_keyword_card = section_card(
        title="메인 - 키워드 설정",
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

    main_variant_grid, main_variant_checks = _variant_grid(
        selected=set(det.ocr_variant_groups),
        on_change=lambda groups: _set_main_variants(det, groups, _push_detection_cfg),
    )
    ocr_dependent_controls.extend(main_variant_checks)

    main_variant_card = section_card(
        title="메인 - 전처리 설정",
        description=(
            "체크한 전처리만 메인 OCR에 적용합니다. "
            "체크가 하나도 없으면 메인 키워드 OCR 전처리를 호출하지 않습니다."
        ),
        content=main_variant_grid,
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

    main_sound_field = text_field(
        value=det.custom_sound_path,
        expand=True,
        read_only=True,
    )

    def _set_main_sound_path(path: str) -> None:
        det.custom_sound_path = path
        _push_detection_cfg()

    pick_main_sound_btn = outline_button(
        "파일 선택",
        on_click=lambda _e: _pick_alert_sound(
            state,
            main_sound_field,
            dialog_title="메인 OCR 알림음 선택",
            log_tag="main",
            set_path=_set_main_sound_path,
        ),
    )
    reset_main_sound_btn = outline_button(
        "기본 알림음",
        on_click=lambda _e: _set_default_alert_sound(
            main_sound_field,
            lambda: _set_main_sound_path(""),
        ),
    )

    tpl_add_btn = outline_button(
        "추가...",
        on_click=lambda _e: _pick_template(state, tpl_field, det),
    )
    tpl_clr_btn = outline_button(
        "비우기",
        on_click=lambda _e: _clear_template(state, tpl_field, det),
    )

    main_template_card = section_card(
        title="메인 - 템플릿 매칭",
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
                            controls=[tpl_add_btn, tpl_clr_btn],
                        ),
                    ],
                    spacing=24,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                ft.Row(
                    spacing=24,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        _labeled_small_field("매칭 임계값", threshold_field),
                        _labeled_small_field("알림 쿨다운(초)", cooldown_field),
                    ],
                ),
                ft.Column(
                    spacing=T.SPACE_SM,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        ft.Text("알림음", style=label_lg(), color=T.ON_SURFACE),
                        ft.Row(
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                main_sound_field,
                                pick_main_sound_btn,
                                reset_main_sound_btn,
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

    regions_column = ft.Column(
        spacing=T.SPACE_SM,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    refresh_lock = threading.Lock()
    refresh_pending = {"value": False}
    last_refresh_ts = {"value": 0.0}

    def _refresh_regions_ui() -> None:
        regions_column.controls.clear()
        if not det.region_rules:
            regions_column.controls.append(
                ft.Container(
                    padding=T.SPACE_MD,
                    border=ft.border.all(1, T.OUTLINE_VARIANT),
                    border_radius=T.RADIUS_SM,
                    content=ft.Text(
                        "추가된 특정영역이 없습니다.",
                        style=body_md(),
                        color=T.ON_SURFACE_VARIANT,
                    ),
                )
            )
        else:
            for i, rule in enumerate(det.region_rules):
                regions_column.controls.append(
                    _build_region_rule_control(
                        state,
                        det,
                        rule,
                        i,
                        refresh=_refresh_regions_ui,
                        push_detection_cfg=_push_detection_cfg,
                    )
                )
        try:
            if regions_column.page is not None:
                regions_column.update()
        except Exception:
            pass

    async def _refresh_regions_ui_async() -> None:
        refresh_pending["value"] = False
        _refresh_regions_ui()

    def _on_frame_for_region_preview(_frame: np.ndarray) -> None:
        now = time.monotonic()
        with refresh_lock:
            if refresh_pending["value"] or now - last_refresh_ts["value"] < 0.7:
                return
            refresh_pending["value"] = True
            last_refresh_ts["value"] = now
        page = getattr(state, "page", None)
        if page is None:
            refresh_pending["value"] = False
            return
        try:
            page.run_task(_refresh_regions_ui_async)
        except Exception:
            refresh_pending["value"] = False

    def _add_region(_e: ft.ControlEvent) -> None:
        n = len(det.region_rules) + 1
        rid = f"region-{int(time.time() * 1000)}-{n}"
        det.region_rules = (
            *det.region_rules,
            RegionRuleSettings(id=rid, name=f"영역 {n}", expanded=True),
        )
        _push_detection_cfg()
        _refresh_regions_ui()

    region_card = section_card(
        content=ft.Column(
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.END,
                    controls=[
                        primary_button(
                            "특정영역 추가",
                            icon=ft.Icons.ADD,
                            on_click=_add_region,
                        )
                    ],
                ),
                regions_column,
            ],
        ),
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
    _refresh_regions_ui()
    old_listener = getattr(state, "_ocr_settings_frame_listener", None)
    if old_listener is not None:
        try:
            state.remove_frame_listener(old_listener)
        except Exception:
            pass
    state._ocr_settings_frame_listener = _on_frame_for_region_preview  # type: ignore[attr-defined]
    state.add_frame_listener(_on_frame_for_region_preview)
    main_card = _build_main_drawer(
        det,
        controls=[main_keyword_card, main_variant_card, main_template_card],
        keyword_enabled_cb=keyword_ocr_cb,
        push_detection_cfg=_push_detection_cfg,
        notify_state=state._notify_state,
        sync_dependent_ui=_sync_ocr_dependent_ui,
    )

    page_root = ft.Column(
        spacing=T.SPACE_LG,
        controls=[
            main_card,
            region_card,
        ],
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        scroll=ft.ScrollMode.AUTO,
    )

    return page_root


def _build_main_drawer(
    det,
    *,
    controls: list[ft.Control],
    keyword_enabled_cb: ft.Checkbox,
    push_detection_cfg,
    notify_state,
    sync_dependent_ui,
) -> ft.Control:
    expanded = {"value": bool(getattr(det, "main_expanded", False))}
    drawer = ft.Column(
        spacing=T.SPACE_MD,
        controls=controls,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        visible=expanded["value"],
    )
    icon_btn = ft.IconButton(
        icon=ft.Icons.EXPAND_MORE if expanded["value"] else ft.Icons.CHEVRON_RIGHT,
        tooltip="열기/닫기",
    )
    enabled_btn = _enabled_toggle_button(det.keyword_ocr_enabled)

    def _toggle_expand(_e: ft.ControlEvent) -> None:
        expanded["value"] = not expanded["value"]
        det.main_expanded = expanded["value"]
        drawer.visible = expanded["value"]
        icon_btn.icon = ft.Icons.EXPAND_MORE if expanded["value"] else ft.Icons.CHEVRON_RIGHT
        notify_state()
        if drawer.page is not None:
            drawer.update()
        if icon_btn.page is not None:
            icon_btn.update()

    def _toggle_enabled(_e: ft.ControlEvent) -> None:
        det.keyword_ocr_enabled = not bool(det.keyword_ocr_enabled)
        keyword_enabled_cb.value = det.keyword_ocr_enabled
        _apply_enabled_toggle_style(enabled_btn, det.keyword_ocr_enabled)
        push_detection_cfg()
        notify_state()
        sync_dependent_ui()
        for c in (keyword_enabled_cb, enabled_btn):
            if c.page is not None:
                c.update()

    icon_btn.on_click = _toggle_expand
    enabled_btn.on_click = _toggle_enabled

    return section_card(
        content=ft.Column(
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    border_radius=T.RADIUS_SM,
                    bgcolor=T.SURFACE_CONTAINER_LOW,
                    on_click=_toggle_expand,
                    content=ft.Row(
                        spacing=T.SPACE_SM,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            icon_btn,
                            ft.Text("메인", style=label_lg(), color=T.ON_SURFACE),
                            ft.Text(
                                _main_summary(det),
                                style=label_md(),
                                color=T.ON_SURFACE_VARIANT,
                                expand=True,
                            ),
                            enabled_btn,
                        ],
                    ),
                ),
                drawer,
            ],
        )
    )


def _variant_grid(
    *,
    selected: set[str],
    on_change,
) -> tuple[ft.ResponsiveRow, list[ft.Checkbox]]:
    checks: dict[str, ft.Checkbox] = {}

    def _changed(_e: ft.ControlEvent, vid: str) -> None:
        cb = checks[vid]
        if cb.value:
            selected.add(vid)
        else:
            selected.discard(vid)
        ordered = tuple(vid for vid, _ in OCR_VARIANT_UI_CHOICES if vid in selected)
        on_change(ordered)

    grid = ft.ResponsiveRow(spacing=8, run_spacing=8, columns=12, controls=[])
    for vid, label in OCR_VARIANT_UI_CHOICES:
        cb = ft.Checkbox(
            label=label,
            value=(vid in selected),
            on_change=lambda e, _vid=vid: _changed(e, _vid),
            active_color=T.PRIMARY,
            label_style=body_md(),
        )
        checks[vid] = cb
        grid.controls.append(
            ft.Container(col={"xs": 12, "md": 6, "lg": 4}, content=cb)
        )
    return grid, list(checks.values())


def _enabled_toggle_button(enabled: bool) -> ft.FilledButton:
    btn = ft.FilledButton(content=ft.Text("사용" if enabled else "미사용"))
    _apply_enabled_toggle_style(btn, enabled)
    return btn


def _apply_enabled_toggle_style(btn: ft.FilledButton, enabled: bool) -> None:
    label = "사용" if enabled else "미사용"
    btn.text = None
    content = getattr(btn, "content", None)
    if isinstance(content, ft.Text):
        content.value = label
        content.color = T.ON_PRIMARY if enabled else T.ON_SURFACE
        content.style = label_lg()
    else:
        btn.content = ft.Text(
            label,
            color=T.ON_PRIMARY if enabled else T.ON_SURFACE,
            style=label_lg(),
        )
    btn.style = ft.ButtonStyle(
        bgcolor=T.PRIMARY if enabled else T.SURFACE_CONTAINER_LOWEST,
        color=T.ON_PRIMARY if enabled else T.ON_SURFACE,
        side=ft.BorderSide(1, T.PRIMARY if enabled else T.OUTLINE_VARIANT),
        padding=ft.padding.symmetric(horizontal=16, vertical=10),
        shape=ft.RoundedRectangleBorder(radius=T.RADIUS_DEFAULT),
        text_style=label_lg(),
    )


def _set_main_variants(det, groups: tuple[str, ...], push_detection_cfg) -> None:
    det.ocr_variant_groups = groups
    push_detection_cfg()


def _labeled_small_field(label: str, field: ft.Control) -> ft.Row:
    return ft.Row(
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Container(
                bgcolor=T.SURFACE_CONTAINER_LOWEST,
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                border_radius=T.RADIUS_SM,
                content=ft.Text(label, style=label_lg(), color=T.ON_SURFACE),
            ),
            field,
        ],
    )


def _build_region_rule_control(
    state: AppState,
    det,
    rule: RegionRuleSettings,
    index: int,
    *,
    refresh,
    push_detection_cfg,
) -> ft.Control:
    def _replace_rule(new_rule: RegionRuleSettings, *, do_refresh: bool = False) -> None:
        rules = list(det.region_rules)
        rules[index] = new_rule
        det.region_rules = tuple(rules)
        push_detection_cfg()
        try:
            state._notify_state()
        except Exception:
            pass
        if do_refresh:
            refresh()

    def _toggle_expand(_e: ft.ControlEvent) -> None:
        current = det.region_rules[index]
        _replace_rule(
            RegionRuleSettings(**{**current.__dict__, "expanded": not current.expanded}),
            do_refresh=True,
        )

    def _delete_rule(_e: ft.ControlEvent) -> None:
        rules = list(det.region_rules)
        del rules[index]
        det.region_rules = tuple(rules)
        push_detection_cfg()
        refresh()

    def _toggle_enabled_from_row(_e: ft.ControlEvent) -> None:
        current = det.region_rules[index]
        _replace_rule(
            RegionRuleSettings(**{**current.__dict__, "enabled": not current.enabled}),
            do_refresh=True,
        )

    icon = ft.Icons.EXPAND_MORE if rule.expanded else ft.Icons.CHEVRON_RIGHT
    summary = _region_summary(rule)
    enabled_btn = _enabled_toggle_button(rule.enabled)
    enabled_btn.on_click = _toggle_enabled_from_row
    header = ft.Container(
        padding=ft.padding.symmetric(horizontal=10, vertical=8),
        border_radius=T.RADIUS_SM,
        bgcolor=T.SURFACE_CONTAINER_LOW,
        on_click=_toggle_expand,
        content=ft.Row(
            spacing=T.SPACE_SM,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.IconButton(icon=icon, on_click=_toggle_expand, tooltip="열기/닫기"),
                ft.Text(rule.name or f"영역 {index + 1}", style=label_lg(), color=T.ON_SURFACE),
                ft.Text(summary, style=label_md(), color=T.ON_SURFACE_VARIANT, expand=True),
                enabled_btn,
                ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE,
                    tooltip="삭제",
                    on_click=_delete_rule,
                    icon_color=T.ERROR,
                ),
            ],
        ),
    )

    if not rule.expanded:
        return header

    name_field = text_field(value=rule.name, expand=True)
    enabled_cb = ft.Checkbox(
        label="사용",
        value=rule.enabled,
        active_color=T.PRIMARY,
        label_style=body_md(),
    )
    keyword_field = text_field(
        value=rule.keywords,
        expand=True,
        hint="키워드를 쉼표로 구분해 입력",
    )
    color_hex_field = text_field(value=rule.color_hex, width=120)
    color_preview = _color_preview_box(rule.color_hex)
    color_tol_field = _number_field(str(max(0, min(100, int(rule.color_tolerance)))))
    try:
        color_tol_field.input_filter = ft.NumbersOnlyInputFilter()
    except Exception:
        pass
    color_enabled_cb = ft.Checkbox(
        label="색상 매칭 사용",
        value=rule.color_match_enabled,
        active_color=T.PRIMARY,
        label_style=body_md(),
    )
    cooldown_field = _number_field(f"{rule.cooldown_sec:.1f}")
    sound_field = text_field(value=rule.custom_sound_path, expand=True, read_only=True)

    def _current_rule(**updates) -> RegionRuleSettings:
        current = det.region_rules[index]
        data = {**current.__dict__, **updates}
        return RegionRuleSettings(**data)

    def _update_basic(_e: ft.ControlEvent | None = None) -> None:
        _replace_rule(
            _current_rule(
                name=name_field.value or f"영역 {index + 1}",
                enabled=bool(enabled_cb.value),
                keywords=keyword_field.value or "",
            )
        )

    name_field.on_change = _update_basic
    enabled_cb.on_change = _update_basic
    keyword_field.on_change = _update_basic

    def _clear_rect(_e: ft.ControlEvent) -> None:
        _replace_rule(_current_rule(rect=None), do_refresh=True)

    def _start_region_pick(_e: ft.ControlEvent) -> None:
        page = getattr(state, "page", None)
        selection_bounds = _capture_selection_bounds(state)
        if not selection_bounds:
            if page is not None:
                show_snack(page, _region_mapping_error_message(state), error=True)
            return
        if page is not None:
            show_snack(page, "지정할 화면 영역을 드래그하세요. Esc를 누르면 취소됩니다.")
        picked = select_screen_rect(selection_bounds)
        if picked is None:
            return
        rect = _screen_rect_to_capture_rect(state, picked)
        if rect is None:
            _log_region_mapping_failure(state, picked)
            if page is not None:
                show_snack(page, _region_mapping_error_message(state), error=True)
            return
        _replace_rule(_current_rule(rect=rect), do_refresh=True)

    def _set_region_variants(groups: tuple[str, ...]) -> None:
        _replace_rule(_current_rule(ocr_variant_groups=groups))

    variant_grid, _ = _variant_grid(
        selected=set(rule.ocr_variant_groups),
        on_change=_set_region_variants,
    )

    def _update_color(_e: ft.ControlEvent | None = None) -> None:
        try:
            tol = max(0, min(100, int(float(color_tol_field.value or "24"))))
        except ValueError:
            return
        if color_tol_field.value != str(tol):
            color_tol_field.value = str(tol)
            if color_tol_field.page is not None:
                color_tol_field.update()
        _replace_rule(
            _current_rule(
                color_match_enabled=bool(color_enabled_cb.value),
                color_hex=color_hex_field.value or "#ff3030",
                color_tolerance=tol,
            )
        )
        color_preview.bgcolor = _safe_ui_hex_color(color_hex_field.value or "#ff3030")
        if color_preview.page is not None:
            color_preview.update()

    color_enabled_cb.on_change = _update_color
    color_hex_field.on_change = _update_color
    color_tol_field.on_change = _update_color

    def _pick_color(_e: ft.ControlEvent) -> None:
        page = getattr(state, "page", None)
        selection_bounds = _capture_selection_bounds(state)
        if not selection_bounds:
            if page is not None:
                show_snack(page, _region_mapping_error_message(state), error=True)
            return
        if page is not None:
            show_snack(page, "색상을 가져올 지점을 클릭하세요. Esc를 누르면 취소됩니다.")
        point = select_screen_point(selection_bounds)
        if point is None:
            return
        picked = _sample_color_from_screen_point(state, point)
        if picked is None:
            if page is not None:
                show_snack(page, "선택한 지점에서 색상을 가져오지 못했습니다.", error=True)
            return
        try:
            tol = max(0, min(100, int(float(color_tol_field.value or "24"))))
        except ValueError:
            tol = 24
        color_hex_field.value = picked
        if color_hex_field.page is not None:
            color_hex_field.update()
        color_preview.bgcolor = picked
        if color_preview.page is not None:
            color_preview.update()
        _replace_rule(
            _current_rule(
                color_match_enabled=True,
                color_hex=picked,
                color_tolerance=tol,
            )
        )

    def _update_cooldown(_e: ft.ControlEvent | None = None) -> None:
        try:
            cooldown = max(0.0, float(cooldown_field.value or "3.0"))
        except ValueError:
            return
        _replace_rule(_current_rule(cooldown_sec=cooldown))

    cooldown_field.on_change = _update_cooldown

    pick_sound_btn = outline_button(
        "파일 선택",
        on_click=lambda _e: _pick_alert_sound(
            state,
            sound_field,
            dialog_title="특정영역 알림음 선택",
            log_tag=f"region:{rule.id}",
            set_path=lambda new_path: _replace_rule(
                _current_rule(custom_sound_path=new_path),
                do_refresh=True,
            ),
        ),
    )
    reset_sound_btn = outline_button(
        "기본 알림음",
        on_click=lambda _e: _set_default_alert_sound(
            sound_field,
            lambda: _replace_rule(
                _current_rule(custom_sound_path=""),
                do_refresh=True,
            ),
        ),
    )

    drawer = ft.Container(
        padding=T.SPACE_MD,
        border=ft.border.all(1, T.OUTLINE_VARIANT),
        border_radius=T.RADIUS_SM,
        content=ft.Column(
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    spacing=T.SPACE_MD,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[name_field, enabled_cb],
                ),
                _setting_group("키워드 설정", keyword_field),
                _setting_group(
                    "특정영역",
                    ft.Column(
                        spacing=T.SPACE_SM,
                        controls=[
                            _region_preview_control(state, rule.rect),
                            ft.Row(
                                spacing=T.SPACE_SM,
                                controls=[
                                    outline_button("영역 지정 시작", on_click=_start_region_pick),
                                    outline_button("영역 비우기", on_click=_clear_rect),
                                ],
                            ),
                        ],
                    ),
                ),
                _setting_group("전처리 설정", variant_grid),
                _setting_group(
                    "색상 매칭 설정",
                    ft.Column(
                        spacing=T.SPACE_SM,
                        controls=[
                            color_enabled_cb,
                            ft.Row(
                                spacing=T.SPACE_SM,
                                vertical_alignment=ft.CrossAxisAlignment.END,
                                controls=[
                                    _field_with_label("기준 색상", color_hex_field),
                                    _field_with_label("미리보기", color_preview),
                                    _field_with_label(
                                        "색상 선택",
                                        outline_button(
                                            "스포이드",
                                            icon=ft.Icons.COLORIZE,
                                            on_click=_pick_color,
                                        ),
                                    ),
                                    _field_with_label("허용 오차(%)", color_tol_field),
                                ],
                            ),
                        ],
                    ),
                ),
                _setting_group(
                    "알림",
                    ft.Column(
                        spacing=T.SPACE_SM,
                        controls=[
                            _field_with_label("쿨다운(초)", cooldown_field),
                            ft.Row(
                                spacing=T.SPACE_SM,
                                controls=[sound_field, pick_sound_btn, reset_sound_btn],
                            ),
                        ],
                    ),
                ),
            ],
        ),
    )

    return ft.Column(
        spacing=4,
        controls=[header, drawer],
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


def _number_field(value: str) -> ft.TextField:
    return text_field(
        value=value,
        width=96,
        text_align=ft.TextAlign.RIGHT,
        keyboard_type=ft.KeyboardType.NUMBER,
    )


def _safe_ui_hex_color(value: str) -> str:
    raw = (value or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) != 6:
        return "#ff3030"
    try:
        int(raw, 16)
    except ValueError:
        return "#ff3030"
    return "#" + raw.lower()


def _color_preview_box(value: str) -> ft.Container:
    return ft.Container(
        width=40,
        height=40,
        bgcolor=_safe_ui_hex_color(value),
        border=ft.border.all(1, T.OUTLINE_VARIANT),
        border_radius=T.RADIUS_SM,
    )


def _main_summary(det) -> str:
    kw_count = len([k for k in det.keywords.replace("\n", ",").split(",") if k.strip()])
    sound = (
        det.custom_sound_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        if det.custom_sound_path
        else "기본음"
    )
    return f"키워드 {kw_count}개 · 쿨다운 {det.cooldown_sec:.1f}초 · {sound}"


def _region_preview_control(state: AppState, rect: RegionRect | None) -> ft.Control:
    if rect is None:
        return ft.Container(
            height=120,
            alignment=ft.alignment.center,
            border=ft.border.all(1, T.OUTLINE_VARIANT),
            border_radius=T.RADIUS_SM,
            bgcolor=T.SURFACE_CONTAINER_LOW,
            content=ft.Text("영역 미지정", style=body_md(), color=T.ON_SURFACE_VARIANT),
        )
    frame = state.get_latest_preview()
    if frame is None:
        return ft.Container(
            height=120,
            alignment=ft.alignment.center,
            border=ft.border.all(1, T.OUTLINE_VARIANT),
            border_radius=T.RADIUS_SM,
            bgcolor=T.SURFACE_CONTAINER_LOW,
            content=ft.Text("캡처 실행 중에 미리보기가 표시됩니다.", style=body_md(), color=T.ON_SURFACE_VARIANT),
        )
    h, w = frame.shape[:2]
    x1 = max(0, min(w, int(rect.x)))
    y1 = max(0, min(h, int(rect.y)))
    x2 = max(0, min(w, int(rect.x + rect.w)))
    y2 = max(0, min(h, int(rect.y + rect.h)))
    if x2 <= x1 or y2 <= y1:
        return ft.Container(
            height=120,
            alignment=ft.alignment.center,
            border=ft.border.all(1, T.OUTLINE_VARIANT),
            border_radius=T.RADIUS_SM,
            bgcolor=T.SURFACE_CONTAINER_LOW,
            content=ft.Text("현재 프레임 밖의 영역입니다.", style=body_md(), color=T.ON_SURFACE_VARIANT),
        )
    crop = frame[y1:y2, x1:x2]
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 86])
    if not ok:
        return ft.Container(
            height=120,
            alignment=ft.alignment.center,
            border=ft.border.all(1, T.OUTLINE_VARIANT),
            border_radius=T.RADIUS_SM,
            bgcolor=T.SURFACE_CONTAINER_LOW,
            content=ft.Text("영역 이미지를 만들지 못했습니다.", style=body_md(), color=T.ON_SURFACE_VARIANT),
        )
    return ft.Container(
        height=160,
        border=ft.border.all(1, T.OUTLINE_VARIANT),
        border_radius=T.RADIUS_SM,
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
        bgcolor=T.SURFACE_CONTAINER_LOW,
        content=ft.Image(
            src=bytes(buf.tobytes()),
            fit=ft.ImageFit.CONTAIN,
            repeat=ft.ImageRepeat.NO_REPEAT,
        ),
    )


def _field_with_label(label: str, control: ft.Control) -> ft.Column:
    return ft.Column(
        spacing=4,
        tight=True,
        controls=[
            ft.Text(label, style=label_md(), color=T.ON_SURFACE_VARIANT),
            control,
        ],
    )


def _setting_group(title: str, content: ft.Control) -> ft.Container:
    return ft.Container(
        padding=T.SPACE_MD,
        border=ft.border.all(1, T.OUTLINE_VARIANT),
        border_radius=T.RADIUS_SM,
        content=ft.Column(
            spacing=T.SPACE_SM,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Text(title, style=label_lg(), color=T.ON_SURFACE),
                content,
            ],
        ),
    )


def _region_summary(rule: RegionRuleSettings) -> str:
    kw_count = len([k for k in rule.keywords.replace("\n", ",").split(",") if k.strip()])
    rect = "영역 지정됨" if rule.rect is not None else "영역 미지정"
    sound = rule.custom_sound_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if rule.custom_sound_path else "기본음"
    return f"키워드 {kw_count}개 · {rect} · 쿨다운 {rule.cooldown_sec:.1f}초 · {sound}"


def _rect_intersection_to_capture(
    *,
    source_left: int,
    source_top: int,
    source_w: int,
    source_h: int,
    frame_w: int,
    frame_h: int,
    rect: ScreenRect,
) -> RegionRect | None:
    if source_w <= 0 or source_h <= 0 or frame_w <= 0 or frame_h <= 0:
        return None
    x1 = max(source_left, min(source_left + source_w, rect.left))
    y1 = max(source_top, min(source_top + source_h, rect.top))
    x2 = max(source_left, min(source_left + source_w, rect.left + rect.width))
    y2 = max(source_top, min(source_top + source_h, rect.top + rect.height))
    if x2 <= x1 or y2 <= y1:
        return None
    sx = frame_w / float(source_w)
    sy = frame_h / float(source_h)
    fx1 = int(round((x1 - source_left) * sx))
    fy1 = int(round((y1 - source_top) * sy))
    fx2 = int(round((x2 - source_left) * sx))
    fy2 = int(round((y2 - source_top) * sy))
    fx1 = max(0, min(frame_w, fx1))
    fy1 = max(0, min(frame_h, fy1))
    fx2 = max(0, min(frame_w, fx2))
    fy2 = max(0, min(frame_h, fy2))
    if fx2 <= fx1 or fy2 <= fy1:
        return None
    return RegionRect(fx1, fy1, fx2 - fx1, fy2 - fy1)


def _point_to_capture_xy(
    *,
    source_left: int,
    source_top: int,
    source_w: int,
    source_h: int,
    frame_w: int,
    frame_h: int,
    point: ScreenPoint,
) -> tuple[int, int] | None:
    if not (
        source_left <= point.x < source_left + source_w
        and source_top <= point.y < source_top + source_h
    ):
        return None
    sx = frame_w / float(source_w)
    sy = frame_h / float(source_h)
    x = max(0, min(frame_w - 1, int(round((point.x - source_left) * sx))))
    y = max(0, min(frame_h - 1, int(round((point.y - source_top) * sy))))
    return x, y


def _screen_rect_to_capture_rect(state: AppState, rect: ScreenRect) -> RegionRect | None:
    cap = state.settings.capture
    frame = state.get_latest_preview()
    frame_h = int(frame.shape[0]) if frame is not None else 0
    frame_w = int(frame.shape[1]) if frame is not None else 0
    if cap.source_mode == "monitor":
        mons = enumerate_monitors()
        mon = next((m for m in mons if int(m.get("index", 0)) == int(cap.monitor_index)), None)
        if mon is None:
            return None
        left = int(mon.get("left", 0))
        top = int(mon.get("top", 0))
        width = int(mon.get("width", 0))
        height = int(mon.get("height", 0))
        return _rect_intersection_to_capture(
            source_left=left,
            source_top=top,
            source_w=width,
            source_h=height,
            frame_w=frame_w or width,
            frame_h=frame_h or height,
            rect=rect,
        )

    hwnd = _effective_capture_hwnd_for_mapping(cap.picked_hwnd)
    if hwnd is None:
        return None
    candidates = _window_mapping_rect_candidates(int(hwnd), frame_w=frame_w, frame_h=frame_h)
    if frame_w <= 0 or frame_h <= 0:
        frame = state.get_latest_preview()
        frame_h = int(frame.shape[0]) if frame is not None else 0
        frame_w = int(frame.shape[1]) if frame is not None else 0
    for left, top, width, height in candidates:
        mapped = _rect_intersection_to_capture(
            source_left=left,
            source_top=top,
            source_w=width,
            source_h=height,
            frame_w=frame_w or width,
            frame_h=frame_h or height,
            rect=rect,
        )
        if mapped is not None:
            return mapped
    return None


def _capture_selection_bounds(state: AppState) -> list[ScreenRect]:
    cap = state.settings.capture
    if cap.source_mode == "monitor":
        try:
            mons = enumerate_monitors()
        except Exception:
            return []
        mon = next((m for m in mons if int(m.get("index", 0)) == int(cap.monitor_index)), None)
        if mon is None:
            return []
        bounds = [
            ScreenRect(
                int(mon.get("left", 0)),
                int(mon.get("top", 0)),
                int(mon.get("width", 0)),
                int(mon.get("height", 0)),
            )
        ]
        try:
            from ..crash_diagnostics import record_exception

            record_exception(
                "ocr_region_selection_bounds",
                "monitor selection bounds resolved",
                detail=(
                    f"capture_source_mode={cap.source_mode}\n"
                    f"capture_monitor_index={cap.monitor_index}\n"
                    f"selected_monitor={mon!r}\n"
                    f"bounds={bounds!r}\n"
                    f"monitors={mons!r}"
                ),
                level="INFO",
            )
        except Exception:
            pass
        return bounds

    hwnd = _effective_capture_hwnd_for_mapping(cap.picked_hwnd)
    if hwnd is None:
        return []
    rect = _window_display_rect_for_selection(int(hwnd))
    return [rect] if rect is not None else []


def _effective_capture_hwnd_for_mapping(hwnd: int | None) -> int | None:
    if hwnd is None:
        return None
    target = int(hwnd)
    try:
        from app_platform.windows_capture import (
            is_league_capture_pair_hwnd,
            resolve_league_capture_hwnd,
        )

        if is_league_capture_pair_hwnd(target):
            return int(resolve_league_capture_hwnd(target))
    except Exception:
        pass
    return target


def _window_display_rect_for_selection(hwnd: int) -> ScreenRect | None:
    candidates = _window_selection_rect_candidates(hwnd)
    if not candidates:
        return None
    base = candidates[0]
    base_area = max(1, base.width * base.height)
    smaller = [
        r
        for r in candidates[1:]
        if r.width >= 64
        and r.height >= 48
        and r.width * r.height >= int(base_area * 0.05)
        and r.width * r.height <= int(base_area * 0.92)
    ]
    nested = [
        r
        for r in smaller
        if _rect_inside(r, base)
        and (r.width <= int(base.width * 0.85) or r.height <= int(base.height * 0.85))
    ]
    if nested:
        return max(nested, key=lambda r: r.width * r.height)
    if smaller and _rect_looks_like_monitor(base):
        return max(smaller, key=lambda r: r.width * r.height)
    return base


def _rect_looks_like_monitor(rect: ScreenRect) -> bool:
    try:
        mons = enumerate_monitors()
    except Exception:
        mons = []
    for mon in mons:
        mw = int(mon.get("width", 0))
        mh = int(mon.get("height", 0))
        ml = int(mon.get("left", 0))
        mt = int(mon.get("top", 0))
        if mw <= 0 or mh <= 0:
            continue
        size_close = rect.width >= int(mw * 0.90) and rect.height >= int(mh * 0.90)
        pos_close = abs(rect.left - ml) <= 12 and abs(rect.top - mt) <= 12
        if size_close and pos_close:
            return True
    return False


def _window_selection_rect_candidates(hwnd: int) -> list[ScreenRect]:
    out: list[ScreenRect] = []

    def add_rect(got: tuple[int, int, int, int] | None) -> None:
        if got is None:
            return
        left, top, width, height = got
        rect = ScreenRect(int(left), int(top), int(width), int(height))
        if rect.width > 0 and rect.height > 0 and rect not in out:
            out.append(rect)

    for getter in (_window_capture_rect_for_mapping, _window_client_rect_for_mapping):
        add_rect(getter(int(hwnd)))
    try:
        from app_platform.windows_capture import _enum_descendant_hwnds

        child_hwnds = _enum_descendant_hwnds(int(hwnd))
    except Exception:
        child_hwnds = []
    for child in child_hwnds:
        for getter in (_window_capture_rect_for_mapping, _window_client_rect_for_mapping):
            add_rect(getter(int(child)))
    try:
        from app_platform.windows_capture import enumerate_windows, hwnd_exe_basename_lower

        base_exe = hwnd_exe_basename_lower(int(hwnd))
        base_rect = out[0] if out else None
        for entry in enumerate_windows(1, 1):
            other_hwnd = int(getattr(entry, "hwnd", 0) or 0)
            if other_hwnd <= 0 or other_hwnd == int(hwnd):
                continue
            if hwnd_exe_basename_lower(other_hwnd) != base_exe:
                continue
            for getter in (_window_capture_rect_for_mapping, _window_client_rect_for_mapping):
                got = getter(other_hwnd)
                if got is None:
                    continue
                left, top, width, height = got
                rect = ScreenRect(int(left), int(top), int(width), int(height))
                if rect.width <= 0 or rect.height <= 0:
                    continue
                if base_rect is None or _rect_inside(rect, base_rect):
                    add_rect(got)
    except Exception:
        pass
    return out


def _rect_inside(inner: ScreenRect, outer: ScreenRect) -> bool:
    margin = 12
    return (
        inner.left >= outer.left - margin
        and inner.top >= outer.top - margin
        and inner.left + inner.width <= outer.left + outer.width + margin
        and inner.top + inner.height <= outer.top + outer.height + margin
    )


def _region_mapping_error_message(state: AppState) -> str:
    cap = state.settings.capture
    if cap.source_mode == "window" and cap.picked_hwnd is None:
        return "창모드 캡처 대상이 없습니다. 대시보드에서 캡처할 창을 먼저 선택해 주세요."
    if state.get_latest_preview() is None:
        return "현재 캡처 프레임이 없습니다. 캡처 시작 후 다시 영역을 지정해 주세요."
    return "선택한 영역을 현재 캡처 기준 좌표로 변환하지 못했습니다."


def _log_region_mapping_failure(state: AppState, rect: ScreenRect) -> None:
    """영역 선택 좌표 변환 실패 원인을 crash/app 로그에 남긴다."""
    cap = state.settings.capture
    frame = state.get_latest_preview()
    frame_shape = None if frame is None else tuple(int(v) for v in frame.shape[:2])
    detail_lines: list[str] = [
        f"picked_screen_rect={rect}",
        f"capture_source_mode={cap.source_mode}",
        f"capture_monitor_index={cap.monitor_index}",
        f"picked_hwnd={cap.picked_hwnd}",
        f"latest_frame_shape_hw={frame_shape}",
    ]
    try:
        mons = enumerate_monitors()
        detail_lines.append(f"monitors={mons!r}")
    except Exception as exc:
        detail_lines.append(f"enumerate_monitors_error={exc!r}")
    effective_hwnd = _effective_capture_hwnd_for_mapping(cap.picked_hwnd)
    detail_lines.append(f"effective_hwnd={effective_hwnd}")
    if effective_hwnd is not None:
        try:
            detail_lines.append(
                f"selection_rect_candidates={_window_selection_rect_candidates(int(effective_hwnd))!r}"
            )
            detail_lines.append(
                f"selection_display_rect={_window_display_rect_for_selection(int(effective_hwnd))!r}"
            )
        except Exception as exc:
            detail_lines.append(f"selection_rect_candidates_error={exc!r}")
        for name, getter in (
            ("window_capture_rect", _window_capture_rect_for_mapping),
            ("window_client_rect", _window_client_rect_for_mapping),
        ):
            try:
                detail_lines.append(f"{name}={getter(int(effective_hwnd))!r}")
            except Exception as exc:
                detail_lines.append(f"{name}_error={exc!r}")
    try:
        from ..crash_diagnostics import record_exception

        record_exception(
            "ocr_region_mapping",
            _region_mapping_error_message(state),
            detail="\n".join(detail_lines),
            level="WARN",
        )
    except Exception:
        try:
            from ..log_buffers import log_app_event

            log_app_event(
                "WARN",
                _region_mapping_error_message(state),
                detail="\n".join(detail_lines),
            )
        except Exception:
            pass


def _screen_point_to_capture_xy(state: AppState, point: ScreenPoint) -> tuple[int, int] | None:
    cap = state.settings.capture
    frame = state.get_latest_preview()
    if frame is None:
        return None
    frame_h, frame_w = frame.shape[:2]
    if cap.source_mode == "monitor":
        mons = enumerate_monitors()
        mon = next((m for m in mons if int(m.get("index", 0)) == int(cap.monitor_index)), None)
        if mon is None:
            return None
        return _point_to_capture_xy(
            source_left=int(mon.get("left", 0)),
            source_top=int(mon.get("top", 0)),
            source_w=int(mon.get("width", frame_w)),
            source_h=int(mon.get("height", frame_h)),
            frame_w=frame_w,
            frame_h=frame_h,
            point=point,
        )
    hwnd = _effective_capture_hwnd_for_mapping(cap.picked_hwnd)
    if hwnd is None:
        return None
    candidates = _window_mapping_rect_candidates(int(hwnd), frame_w=frame_w, frame_h=frame_h)
    for left, top, width, height in candidates:
        mapped = _point_to_capture_xy(
            source_left=left,
            source_top=top,
            source_w=width,
            source_h=height,
            frame_w=frame_w,
            frame_h=frame_h,
            point=point,
        )
        if mapped is not None:
            return mapped
    return None


def _window_mapping_rect_candidates(
    hwnd: int,
    *,
    frame_w: int,
    frame_h: int,
) -> list[tuple[int, int, int, int]]:
    candidates: list[tuple[int, int, int, int]] = []
    for getter in (_window_capture_rect_for_mapping, _window_client_rect_for_mapping):
        got = getter(int(hwnd))
        if got is not None and got not in candidates:
            candidates.append(got)
    if frame_w <= 0 or frame_h <= 0:
        return candidates

    def score(candidate: tuple[int, int, int, int]) -> tuple[int, int]:
        _left, _top, width, height = candidate
        size_delta = abs(int(width) - int(frame_w)) + abs(int(height) - int(frame_h))
        area_delta = abs(int(width) * int(height) - int(frame_w) * int(frame_h))
        return size_delta, area_delta

    return sorted(candidates, key=score)


def _sample_color_from_screen_point(state: AppState, point: ScreenPoint) -> str | None:
    frame = state.get_latest_preview()
    xy = _screen_point_to_capture_xy(state, point) if frame is not None else None
    if frame is not None and xy is not None:
        x, y = xy
        b, g, r = [int(v) for v in frame[y, x, :3]]
        return f"#{r:02x}{g:02x}{b:02x}"
    sampled = _sample_screen_pixel_bgr(point)
    if sampled is None:
        return None
    b, g, r = sampled
    return f"#{r:02x}{g:02x}{b:02x}"


def _sample_screen_pixel_bgr(point: ScreenPoint) -> tuple[int, int, int] | None:
    try:
        import mss

        with mss.mss() as sct:
            shot = sct.grab(
                {
                    "left": int(point.x),
                    "top": int(point.y),
                    "width": 1,
                    "height": 1,
                }
            )
            arr = np.asarray(shot, dtype=np.uint8)
            if arr.size == 0:
                return None
            b, g, r = [int(v) for v in arr[0, 0, :3]]
            return b, g, r
    except Exception:
        return None


def _window_client_rect_for_mapping(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        from app_platform.windows_capture import _client_rect_screen

        return _client_rect_screen(int(hwnd))
    except Exception:
        return None


def _window_capture_rect_for_mapping(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        from app_platform.windows_capture import _window_rect_for_capture

        return _window_rect_for_capture(int(hwnd))
    except Exception:
        return None


def _pick_alert_sound(
    state: AppState,
    sound_field: ft.TextField,
    *,
    dialog_title: str,
    log_tag: str,
    set_path,
) -> None:
    page = getattr(state, "page", None)
    if page is None:
        return

    async def _async_pick() -> None:
        from ..log_buffers import log_app_event

        try:
            fp = ensure_file_picker(page)
            log_app_event("INFO", f"pick_files start (alert sound, {log_tag})")
            files = await fp.pick_files(
                dialog_title=dialog_title,
                allow_multiple=False,
                allowed_extensions=["wav", "mp3", "aiff", "aif"],
            )
            if not files:
                return
            path = getattr(files[0], "path", None)
            if not path:
                return
            sound_field.value = path
            if sound_field.page is not None:
                sound_field.update()
            set_path(path)
        except Exception as exc:  # noqa: BLE001
            show_snack(page, f"알림음 파일 선택 실패: {exc}", error=True)

    try:
        page.run_task(_async_pick)
    except Exception as exc:  # noqa: BLE001
        show_snack(page, f"알림음 파일 선택 실패: {exc}", error=True)


def _set_default_alert_sound(sound_field: ft.TextField, apply_default) -> None:
    sound_field.value = ""
    if sound_field.page is not None:
        sound_field.update()
    apply_default()


def _pick_template(state: AppState, tpl_field: ft.TextField, det) -> None:
    """Flet 0.85+ ``FilePicker.pick_files`` async API 에 맞춰 코루틴으로 실행."""
    page = getattr(state, "page", None)
    if page is None:
        return

    async def _async_pick() -> None:
        from ..log_buffers import log_app_event

        try:
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
