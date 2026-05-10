"""앱 설정 — 외형(다크 모드) 등 전역 UI 옵션."""

from __future__ import annotations

import flet as ft

from ..components import section_card
from ..state import AppState
from ..theme import (
    StreamMasterTheme as T,
    body_md,
    headline_sm,
    label_lg,
)


def build_app_settings(state: AppState) -> ft.Control:
    dark_switch = ft.Switch(
        label="다크 모드",
        value=state.settings.dark_mode,
        label_text_style=body_md(),
        active_color=T.PRIMARY,
    )

    def _on_dark_mode(e: ft.ControlEvent) -> None:
        state.settings.dark_mode = bool(e.control.value)
        state.save()
        state.notify_theme_changed()

    dark_switch.on_change = _on_dark_mode

    appearance_card = section_card(
        content=ft.Column(
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Text("외형", style=label_lg(), color=T.ON_SURFACE),
                dark_switch,
                ft.Text(
                    "켜면 메뉴·본문·입력 필드 등 앱 전체에 어두운 배경이 적용됩니다.",
                    style=body_md(),
                    color=T.ON_SURFACE_VARIANT,
                ),
            ],
        ),
    )

    return ft.Column(
        spacing=T.GUTTER,
        controls=[
            ft.Text("앱 설정", style=headline_sm(), color=T.ON_SURFACE),
            appearance_card,
        ],
    )
