"""StreamMaster Pro / Capture Studio 디자인 시스템 정의.

`professional_material_system` 의 Material Design 3 토큰을 Flet 에서 쓸 수
있는 형태(테마·색상·타이포그래피·간격)로 옮겨둔 모듈. UI 모듈들이 이 값을
공통 상수처럼 import 해서 사용한다.
"""

from __future__ import annotations

import flet as ft


_COLOR_KEYS: tuple[str, ...] = (
    "SURFACE",
    "SURFACE_DIM",
    "SURFACE_BRIGHT",
    "SURFACE_CONTAINER_LOWEST",
    "SURFACE_CONTAINER_LOW",
    "SURFACE_CONTAINER",
    "SURFACE_CONTAINER_HIGH",
    "SURFACE_CONTAINER_HIGHEST",
    "ON_SURFACE",
    "ON_SURFACE_VARIANT",
    "INVERSE_SURFACE",
    "INVERSE_ON_SURFACE",
    "OUTLINE",
    "OUTLINE_VARIANT",
    "SURFACE_TINT",
    "PRIMARY",
    "ON_PRIMARY",
    "PRIMARY_CONTAINER",
    "ON_PRIMARY_CONTAINER",
    "INVERSE_PRIMARY",
    "SECONDARY",
    "ON_SECONDARY",
    "SECONDARY_CONTAINER",
    "ON_SECONDARY_CONTAINER",
    "TERTIARY",
    "ON_TERTIARY",
    "TERTIARY_CONTAINER",
    "ON_TERTIARY_CONTAINER",
    "ERROR",
    "ON_ERROR",
    "ERROR_CONTAINER",
    "ON_ERROR_CONTAINER",
    "SUCCESS",
    "SUCCESS_DIM",
    "WARNING",
    "PRIMARY_FIXED",
    "PRIMARY_FIXED_DIM",
    "ON_PRIMARY_FIXED",
    "SECONDARY_FIXED",
    "SECONDARY_FIXED_DIM",
    "BACKGROUND",
)

# 라이트 테마 스냅샷은 클래스 정의 직후 한 번 채운다.
_LIGHT_THEME_SNAPSHOT: dict[str, str] | None = None

_DARK_THEME_OVERRIDES: dict[str, str] = {
    "SURFACE": "#1c1b1f",
    "SURFACE_DIM": "#141218",
    "SURFACE_BRIGHT": "#1c1b1f",
    "SURFACE_CONTAINER_LOWEST": "#1c1b1f",
    "SURFACE_CONTAINER_LOW": "#2b2930",
    "SURFACE_CONTAINER": "#36343b",
    "SURFACE_CONTAINER_HIGH": "#423f47",
    "SURFACE_CONTAINER_HIGHEST": "#4e4a53",
    "ON_SURFACE": "#e6e1e5",
    "ON_SURFACE_VARIANT": "#cac4d0",
    "INVERSE_SURFACE": "#e6e1e5",
    "INVERSE_ON_SURFACE": "#1c1b1f",
    "OUTLINE": "#938f99",
    "OUTLINE_VARIANT": "#49454f",
    "SURFACE_TINT": "#a8c7fa",
    "PRIMARY": "#a8c7fa",
    "ON_PRIMARY": "#003258",
    "PRIMARY_CONTAINER": "#004a77",
    "ON_PRIMARY_CONTAINER": "#d0e4ff",
    "INVERSE_PRIMARY": "#3f5d92",
    "SECONDARY": "#bcc7db",
    "ON_SECONDARY": "#283141",
    "SECONDARY_CONTAINER": "#3f4a5c",
    "ON_SECONDARY_CONTAINER": "#d9e3f8",
    "TERTIARY": "#d0c0d8",
    "ON_TERTIARY": "#382e42",
    "TERTIARY_CONTAINER": "#534660",
    "ON_TERTIARY_CONTAINER": "#eddff7",
    "ERROR": "#ffb4ab",
    "ON_ERROR": "#690005",
    "ERROR_CONTAINER": "#93000a",
    "ON_ERROR_CONTAINER": "#ffdad6",
    "SUCCESS": "#6ee7b7",
    "SUCCESS_DIM": "#34d399",
    "WARNING": "#fcd34d",
    "PRIMARY_FIXED": "#d0e4ff",
    "PRIMARY_FIXED_DIM": "#a8c7fa",
    "ON_PRIMARY_FIXED": "#041c35",
    "SECONDARY_FIXED": "#d9e3f8",
    "SECONDARY_FIXED_DIM": "#bcc7db",
    "BACKGROUND": "#121316",
}


def apply_theme_mode(*, dark: bool) -> None:
    """StreamMasterTheme 의 색상 토큰을 라이트/다크로 전환한다.

    레이아웃 컨트롤은 각 속성을 빌드 시점에 읽으므로, 전환 후에는 UI 를 다시
    구성해야 한다.
    """
    global _LIGHT_THEME_SNAPSHOT
    if _LIGHT_THEME_SNAPSHOT is None:
        _LIGHT_THEME_SNAPSHOT = {
            k: getattr(StreamMasterTheme, k) for k in _COLOR_KEYS
        }
    src = _DARK_THEME_OVERRIDES if dark else _LIGHT_THEME_SNAPSHOT
    for k in _COLOR_KEYS:
        setattr(StreamMasterTheme, k, src[k])


class StreamMasterTheme:
    """Material Design 3 톤맵을 헥사 코드로 모은 정적 테이블."""

    SURFACE = "#f8f9fa"
    SURFACE_DIM = "#d9dadb"
    SURFACE_BRIGHT = "#f8f9fa"
    SURFACE_CONTAINER_LOWEST = "#ffffff"
    SURFACE_CONTAINER_LOW = "#f3f4f5"
    SURFACE_CONTAINER = "#edeeef"
    SURFACE_CONTAINER_HIGH = "#e7e8e9"
    SURFACE_CONTAINER_HIGHEST = "#e1e3e4"
    ON_SURFACE = "#191c1d"
    ON_SURFACE_VARIANT = "#414754"
    INVERSE_SURFACE = "#2e3132"
    INVERSE_ON_SURFACE = "#f0f1f2"
    OUTLINE = "#727785"
    OUTLINE_VARIANT = "#c1c6d6"
    SURFACE_TINT = "#005bc0"

    PRIMARY = "#005bbf"
    ON_PRIMARY = "#ffffff"
    PRIMARY_CONTAINER = "#1a73e8"
    ON_PRIMARY_CONTAINER = "#ffffff"
    INVERSE_PRIMARY = "#adc7ff"

    SECONDARY = "#005ac1"
    ON_SECONDARY = "#ffffff"
    SECONDARY_CONTAINER = "#4d8efe"
    ON_SECONDARY_CONTAINER = "#00285c"

    TERTIARY = "#48607b"
    ON_TERTIARY = "#ffffff"
    TERTIARY_CONTAINER = "#617995"
    ON_TERTIARY_CONTAINER = "#00050f"

    ERROR = "#ba1a1a"
    ON_ERROR = "#ffffff"
    ERROR_CONTAINER = "#ffdad6"
    ON_ERROR_CONTAINER = "#93000a"

    SUCCESS = "#22c55e"
    SUCCESS_DIM = "#15803d"
    WARNING = "#f59e0b"

    PRIMARY_FIXED = "#d8e2ff"
    PRIMARY_FIXED_DIM = "#adc7ff"
    ON_PRIMARY_FIXED = "#001a41"
    SECONDARY_FIXED = "#d8e2ff"
    SECONDARY_FIXED_DIM = "#adc6ff"

    BACKGROUND = "#f8f9fa"

    SPACE_XS = 4
    SPACE_SM = 8
    SPACE_MD = 16
    SPACE_LG = 24
    SPACE_XL = 32
    SPACE_XXL = 48
    GUTTER = 24
    MARGIN_DESKTOP = 32
    MARGIN_MOBILE = 16

    RADIUS_SM = 4
    RADIUS_DEFAULT = 8
    RADIUS_MD = 12
    RADIUS_LG = 16
    RADIUS_FULL = 9999

    SIDEBAR_WIDTH = 256
    TOPBAR_HEIGHT = 64
    FOOTER_HEIGHT = 40

    @classmethod
    def color_scheme(cls) -> ft.ColorScheme:
        """Flet `Theme` 에 사용하는 ColorScheme 인스턴스.

        Material 3 갱신으로 `background`/`on_background`/`surface_variant`
        토큰이 사라지고 `surface_container_*` 계열로 대체되었다.
        """
        return ft.ColorScheme(
            primary=cls.PRIMARY,
            on_primary=cls.ON_PRIMARY,
            primary_container=cls.PRIMARY_CONTAINER,
            on_primary_container=cls.ON_PRIMARY_CONTAINER,
            secondary=cls.SECONDARY,
            on_secondary=cls.ON_SECONDARY,
            secondary_container=cls.SECONDARY_CONTAINER,
            on_secondary_container=cls.ON_SECONDARY_CONTAINER,
            tertiary=cls.TERTIARY,
            on_tertiary=cls.ON_TERTIARY,
            tertiary_container=cls.TERTIARY_CONTAINER,
            on_tertiary_container=cls.ON_TERTIARY_CONTAINER,
            error=cls.ERROR,
            on_error=cls.ON_ERROR,
            error_container=cls.ERROR_CONTAINER,
            on_error_container=cls.ON_ERROR_CONTAINER,
            surface=cls.SURFACE,
            on_surface=cls.ON_SURFACE,
            on_surface_variant=cls.ON_SURFACE_VARIANT,
            surface_dim=cls.SURFACE_DIM,
            surface_bright=cls.SURFACE_BRIGHT,
            surface_container_lowest=cls.SURFACE_CONTAINER_LOWEST,
            surface_container_low=cls.SURFACE_CONTAINER_LOW,
            surface_container=cls.SURFACE_CONTAINER,
            surface_container_high=cls.SURFACE_CONTAINER_HIGH,
            surface_container_highest=cls.SURFACE_CONTAINER_HIGHEST,
            outline=cls.OUTLINE,
            outline_variant=cls.OUTLINE_VARIANT,
            inverse_surface=cls.INVERSE_SURFACE,
            on_inverse_surface=cls.INVERSE_ON_SURFACE,
            inverse_primary=cls.INVERSE_PRIMARY,
            surface_tint=cls.SURFACE_TINT,
            primary_fixed=cls.PRIMARY_FIXED,
            primary_fixed_dim=cls.PRIMARY_FIXED_DIM,
            on_primary_fixed=cls.ON_PRIMARY_FIXED,
            secondary_fixed=cls.SECONDARY_FIXED,
            secondary_fixed_dim=cls.SECONDARY_FIXED_DIM,
        )

    @classmethod
    def theme(cls) -> ft.Theme:
        return ft.Theme(
            color_scheme_seed=cls.PRIMARY,
            color_scheme=cls.color_scheme(),
            font_family="Inter",
            use_material3=True,
            visual_density=ft.VisualDensity.COMFORTABLE,
        )

    @staticmethod
    def fonts() -> dict[str, str]:
        """Inter 웹폰트(Google Fonts) 매핑."""
        return {
            "Inter": "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700&display=swap",
        }


def display_lg() -> ft.TextStyle:
    return ft.TextStyle(
        size=57, weight=ft.FontWeight.W_400, height=64 / 57, letter_spacing=-0.25
    )


def headline_lg() -> ft.TextStyle:
    return ft.TextStyle(size=32, weight=ft.FontWeight.W_400, height=40 / 32)


def headline_md() -> ft.TextStyle:
    return ft.TextStyle(size=28, weight=ft.FontWeight.W_400, height=36 / 28)


def headline_sm() -> ft.TextStyle:
    return ft.TextStyle(size=24, weight=ft.FontWeight.W_400, height=32 / 24)


def title_lg() -> ft.TextStyle:
    return ft.TextStyle(size=22, weight=ft.FontWeight.W_500, height=28 / 22)


def title_md() -> ft.TextStyle:
    return ft.TextStyle(
        size=16, weight=ft.FontWeight.W_500, height=24 / 16, letter_spacing=0.15
    )


def body_lg() -> ft.TextStyle:
    return ft.TextStyle(
        size=16, weight=ft.FontWeight.W_400, height=24 / 16, letter_spacing=0.5
    )


def body_md() -> ft.TextStyle:
    return ft.TextStyle(
        size=14, weight=ft.FontWeight.W_400, height=20 / 14, letter_spacing=0.25
    )


def label_lg() -> ft.TextStyle:
    return ft.TextStyle(
        size=14, weight=ft.FontWeight.W_500, height=20 / 14, letter_spacing=0.1
    )


def label_md() -> ft.TextStyle:
    return ft.TextStyle(
        size=12, weight=ft.FontWeight.W_500, height=16 / 12, letter_spacing=0.5
    )


__all__ = [
    "apply_theme_mode",
    "StreamMasterTheme",
    "display_lg",
    "headline_lg",
    "headline_md",
    "headline_sm",
    "title_lg",
    "title_md",
    "body_lg",
    "body_md",
    "label_lg",
    "label_md",
]
