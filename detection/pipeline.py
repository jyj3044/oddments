"""키워드 + 템플릿 감지 파이프라인 (한 프레임 단위)."""

from __future__ import annotations

import threading
from typing import List, Optional, Tuple

import numpy as np

from .common import DetectionConfig, OverlayRect
from . import keywords as kw
from .overlay_store import get_overlay_store
from . import templates as tpl


def run_detection_with_overlays(
    frame_bgr: np.ndarray,
    cfg: DetectionConfig,
    stop_event: Optional[threading.Event] = None,
    kw_abort: Optional[threading.Event] = None,
) -> Tuple[bool, str, List[OverlayRect]]:
    if stop_event is not None and stop_event.is_set():
        return False, "", []
    plain_hits, kw_ovs = kw.run_keyword_detection(
        frame_bgr,
        cfg.alert_keywords,
        cfg.ocr_engines,
        stop_event,
        variant_groups=cfg.ocr_variant_groups,
        kw_abort=kw_abort,
    )
    if stop_event is not None and stop_event.is_set():
        return False, "", []
    # OCR 미사용(settings 에서 ocr_engines 비움) 시 키워드 OCR·템플릿 매칭 모두 생략
    tpl_ovs: List[OverlayRect] = []
    if cfg.ocr_engines:
        tpl_ovs = tpl.match_all_templates(
            frame_bgr, cfg.template_paths, cfg.template_threshold
        )
    overlays = kw_ovs + tpl_ovs
    get_overlay_store().touch(overlays)

    if plain_hits:
        return True, "키워드 텍스트", overlays
    if tpl_ovs:
        return True, "템플릿 이미지", overlays
    return False, "", overlays


def run_detection(
    frame_bgr: np.ndarray,
    cfg: DetectionConfig,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[bool, str]:
    trig, reason, _ = run_detection_with_overlays(frame_bgr, cfg, stop_event)
    return trig, reason
