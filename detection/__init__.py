"""화면 감지: 키워드 OCR + 템플릿 매칭."""

from .common import (
    DetectionConfig,
    DetectionHitEvent,
    DetectionRunResult,
    OCR_VARIANT_GROUPS_DISABLED,
    OverlayRect,
    RegionDetectionConfig,
    RegionRect,
)
from .keywords import OCR_VARIANT_UI_CHOICES, check_plain_text, ocr_runtime_ok
from .ocr_backends import ALL_OCR_ENGINES, DEFAULT_OCR_ENGINE, normalize_ocr_engine
from .overlay_store import get_overlay_store
from .pipeline import run_detection, run_detection_detailed, run_detection_with_overlays

__all__ = [
    "ALL_OCR_ENGINES",
    "DEFAULT_OCR_ENGINE",
    "DetectionConfig",
    "DetectionHitEvent",
    "DetectionRunResult",
    "get_overlay_store",
    "OCR_VARIANT_GROUPS_DISABLED",
    "OCR_VARIANT_UI_CHOICES",
    "OverlayRect",
    "RegionDetectionConfig",
    "RegionRect",
    "check_plain_text",
    "normalize_ocr_engine",
    "ocr_runtime_ok",
    "run_detection",
    "run_detection_detailed",
    "run_detection_with_overlays",
]
