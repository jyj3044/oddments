"""감지 공통 타입·설정."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

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


@dataclass
class DetectionConfig:
    alert_keywords: Tuple[str, ...] = ()
    template_paths: Tuple[str, ...] = ()
    template_threshold: float = 0.80
    # 키워드 OCR에 쓸 엔진(순서대로 호출, 하나라도 키워드면 알림). 비어 있으면 키워드 OCR 안 함.
    ocr_engines: Tuple[str, ...] = ("rapidocr",)
    # 비어 있으면 전처리 변형 전부 사용. OCR_VARIANT_GROUPS_DISABLED 이면 변형 OCR 호출 안 함.
    ocr_variant_groups: Tuple[str, ...] = ()


def stable_overlay_bgr(tag: str, x: int, y: int, w: int, h: int) -> Tuple[int, int, int]:
    """같은 영역은 프레임마다 같은 색(깜빡임 방지), 영역마다는 서로 다른 색."""
    u = hash((tag, x, y, w, h)) & 0xFFFFFFFF
    return (
        40 + (u & 0xFF) % 200,
        40 + ((u >> 8) & 0xFF) % 200,
        40 + ((u >> 16) & 0xFF) % 200,
    )
