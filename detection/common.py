"""감지 공통 타입·설정."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

import numpy as np

# UI에서 전처리를 하나도 선택하지 않았을 때 (키워드 OCR 변형 호출 안 함)
OCR_VARIANT_GROUPS_DISABLED: Tuple[str, ...] = ("__oddments_ocr_variants_disabled__",)


@dataclass
class OverlayRect:
    """미리보기에 그릴 테두리 박스 (프레임 전체 해상도 좌표)."""

    x: int
    y: int
    w: int
    h: int
    color_bgr: Tuple[int, int, int]
    label: str = ""


@dataclass(frozen=True)
class RegionRect:
    """프레임 전체 해상도 기준 특정 OCR 영역."""

    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class RegionDetectionConfig:
    """감지 루프에서 바로 쓸 수 있게 정규화한 특정영역 OCR 설정."""

    id: str
    name: str
    rect: RegionRect
    enabled: bool = True
    alert_keywords: Tuple[str, ...] = ()
    ocr_engines: Tuple[str, ...] = ("rapidocr",)
    ocr_variant_groups: Tuple[str, ...] = ()
    color_match_enabled: bool = False
    color_bgr: Tuple[int, int, int] = (48, 48, 255)
    color_tolerance: int = 24
    cooldown_sec: float = 3.0
    custom_sound_path: str = ""


@dataclass(frozen=True)
class DetectionHitEvent:
    """감지 루프가 알림음/쿨다운을 처리할 때 쓰는 논리적 히트 이벤트."""

    id: str
    name: str
    cooldown_sec: float
    custom_sound_path: str = ""


@dataclass(frozen=True)
class DetectionRunResult:
    triggered: bool
    reason: str
    overlays: Tuple[OverlayRect, ...] = ()
    events: Tuple[DetectionHitEvent, ...] = ()


@dataclass
class DetectionConfig:
    alert_keywords: Tuple[str, ...] = ()
    template_paths: Tuple[str, ...] = ()
    template_threshold: float = 0.80
    # 키워드 OCR에 쓸 엔진(순서대로 호출, 하나라도 키워드면 알림). 비어 있으면 키워드 OCR 안 함.
    ocr_engines: Tuple[str, ...] = ("rapidocr",)
    # 비어 있으면 전처리 변형 전부 사용. OCR_VARIANT_GROUPS_DISABLED 이면 변형 OCR 호출 안 함.
    ocr_variant_groups: Tuple[str, ...] = ()
    region_rules: Tuple[RegionDetectionConfig, ...] = ()


def stable_overlay_bgr(tag: str, x: int, y: int, w: int, h: int) -> Tuple[int, int, int]:
    """같은 영역은 프레임마다 같은 색(깜빡임 방지), 영역마다는 서로 다른 색."""
    u = hash((tag, x, y, w, h)) & 0xFFFFFFFF
    return (
        40 + (u & 0xFF) % 200,
        40 + ((u >> 8) & 0xFF) % 200,
        40 + ((u >> 16) & 0xFF) % 200,
    )


def clamp_region_rect(
    rect: RegionRect,
    *,
    frame_w: int,
    frame_h: int,
) -> Optional[RegionRect]:
    """영역을 프레임 안으로 자르고, 비어 있으면 None."""
    fw = max(0, int(frame_w))
    fh = max(0, int(frame_h))
    if fw <= 0 or fh <= 0:
        return None
    x1 = max(0, min(fw, int(rect.x)))
    y1 = max(0, min(fh, int(rect.y)))
    x2 = max(0, min(fw, int(rect.x) + max(0, int(rect.w))))
    y2 = max(0, min(fh, int(rect.y) + max(0, int(rect.h))))
    w = x2 - x1
    h = y2 - y1
    if w <= 0 or h <= 0:
        return None
    return RegionRect(x1, y1, w, h)


def offset_overlay_rects(
    overlays: Iterable[OverlayRect],
    *,
    dx: int,
    dy: int,
    label_prefix: str = "",
) -> list[OverlayRect]:
    """crop 좌표계 OverlayRect 목록을 전체 프레임 좌표계로 옮긴다."""
    prefix = (label_prefix or "").strip()
    out: list[OverlayRect] = []
    for ov in overlays:
        label = ov.label
        if prefix:
            label = f"{prefix} {label}".strip()
        out.append(
            OverlayRect(
                int(ov.x) + int(dx),
                int(ov.y) + int(dy),
                int(ov.w),
                int(ov.h),
                ov.color_bgr,
                label,
            )
        )
    return out


def color_match_ratio(
    frame_bgr: np.ndarray,
    *,
    color_bgr: Tuple[int, int, int],
    tolerance: int,
) -> float:
    """BGR 프레임에서 기준 색상과 허용 오차 안에 드는 픽셀 비율(%)을 반환."""
    if frame_bgr is None or frame_bgr.size == 0:
        return 0.0
    if frame_bgr.ndim < 3 or frame_bgr.shape[2] < 3:
        return 0.0
    tol_percent = max(0, min(100, int(tolerance)))
    tol = int(round(255 * (tol_percent / 100.0)))
    target = np.array(color_bgr, dtype=np.int16)
    work = frame_bgr[:, :, :3].astype(np.int16)
    mask = np.all(np.abs(work - target) <= tol, axis=2)
    return float(mask.mean() * 100.0)


def region_color_matches(
    frame_bgr: np.ndarray,
    cfg: RegionDetectionConfig,
) -> bool:
    if not cfg.color_match_enabled:
        return False
    ratio = color_match_ratio(
        frame_bgr,
        color_bgr=cfg.color_bgr,
        tolerance=cfg.color_tolerance,
    )
    return ratio > 0.0
