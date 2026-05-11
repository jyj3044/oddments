"""원격 호스트 해상도 프리셋(비율·픽셀). macOS 가상 디스플레이 모드에서 사용."""

from __future__ import annotations

from typing import Callable

# preset_id -> (width, height) 물리 픽셀. host_native 는 별도 해석.
RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    "host_native": (0, 0),  # 호스트 메인 화면과 동일(동적)
    "xga_4_3": (1024, 768),
    "uxga_4_3": (1600, 1200),
    "fhd_16_9": (1920, 1080),
    "qhd_16_9": (2560, 1440),
    "uwfhd_21_9": (2560, 1080),
    "uwqhd_21_9": (3440, 1440),
}

PRESET_LABELS: list[tuple[str, str]] = [
    ("host_native", "호스트와 동일 (메인 디스플레이)"),
    ("xga_4_3", "4:3 · 1024×768"),
    ("uxga_4_3", "4:3 · 1600×1200"),
    ("fhd_16_9", "16:9 · FHD 1920×1080"),
    ("qhd_16_9", "16:9 · QHD 2560×1440"),
    ("uwfhd_21_9", "21:9 · 2560×1080"),
    ("uwqhd_21_9", "21:9 · 3440×1440"),
]

VALID_PRESET_IDS: frozenset[str] = frozenset(RESOLUTION_PRESETS.keys())


def normalize_preset_id(raw: object, *, fallback: str = "host_native") -> str:
    """알려진 preset id 만 허용."""
    if not isinstance(raw, str):
        return fallback
    s = raw.strip()
    if s in VALID_PRESET_IDS:
        return s
    return fallback


def preset_dimensions(
    preset_id: str,
    *,
    host_native: Callable[[], tuple[int, int]],
) -> tuple[int, int]:
    """프리셋 ID 에 해당하는 가로·세로를 반환."""
    pid = (preset_id or "").strip() or "host_native"
    if pid == "host_native":
        w, h = host_native()
        return max(320, int(w)), max(240, int(h))
    pair = RESOLUTION_PRESETS.get(pid)
    if pair is None or pair == (0, 0):
        w, h = host_native()
        return max(320, int(w)), max(240, int(h))
    w, h = pair
    return max(320, int(w)), max(240, int(h))


__all__ = [
    "PRESET_LABELS",
    "RESOLUTION_PRESETS",
    "VALID_PRESET_IDS",
    "normalize_preset_id",
    "preset_dimensions",
]
