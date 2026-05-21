"""키워드 + 템플릿 감지 파이프라인 (한 프레임 단위)."""

from __future__ import annotations

import threading
from typing import List, Optional, Tuple

import numpy as np

from .common import (
    DetectionConfig,
    DetectionHitEvent,
    DetectionRunResult,
    OverlayRect,
    clamp_region_rect,
    offset_overlay_rects,
    region_color_matches,
    stable_overlay_bgr,
)
from . import keywords as kw
from .ocr_diag import suppress_ocr_keyword_alert_sound
from .overlay_store import get_overlay_store
from . import templates as tpl


def run_detection_detailed(
    frame_bgr: np.ndarray,
    cfg: DetectionConfig,
    stop_event: Optional[threading.Event] = None,
    kw_abort: Optional[threading.Event] = None,
) -> DetectionRunResult:
    if stop_event is not None and stop_event.is_set():
        return DetectionRunResult(False, "")
    plain_hits = False
    kw_ovs: List[OverlayRect] = []
    if cfg.alert_keywords and cfg.ocr_engines:
        plain_hits, kw_ovs = kw.run_keyword_detection(
            frame_bgr,
            cfg.alert_keywords,
            cfg.ocr_engines,
            stop_event,
            variant_groups=cfg.ocr_variant_groups,
            kw_abort=kw_abort,
        )
    if stop_event is not None and stop_event.is_set():
        return DetectionRunResult(False, "")
    # OCR 미사용(settings 에서 ocr_engines 비움) 시 키워드 OCR·템플릿 매칭 모두 생략
    tpl_ovs: List[OverlayRect] = []
    if cfg.ocr_engines:
        tpl_ovs = tpl.match_all_templates(
            frame_bgr, cfg.template_paths, cfg.template_threshold
        )
    region_ovs: List[OverlayRect] = []
    events: List[DetectionHitEvent] = []
    h, w = frame_bgr.shape[:2]
    for rule in cfg.region_rules:
        if stop_event is not None and stop_event.is_set():
            return DetectionRunResult(False, "")
        if not rule.enabled:
            continue
        rect = clamp_region_rect(rule.rect, frame_w=w, frame_h=h)
        if rect is None:
            continue
        has_ocr = bool(rule.alert_keywords and rule.ocr_engines)
        has_color = bool(rule.color_match_enabled)
        if not has_ocr and not has_color:
            continue
        crop = frame_bgr[rect.y : rect.y + rect.h, rect.x : rect.x + rect.w]
        rule_hit = False
        rule_ovs: List[OverlayRect] = []
        if has_ocr:
            with suppress_ocr_keyword_alert_sound():
                rule_hit, crop_ovs = kw.run_keyword_detection(
                    crop,
                    rule.alert_keywords,
                    rule.ocr_engines,
                    stop_event,
                    variant_groups=rule.ocr_variant_groups,
                    kw_abort=kw_abort,
                )
            rule_ovs.extend(
                offset_overlay_rects(
                    crop_ovs,
                    dx=rect.x,
                    dy=rect.y,
                    label_prefix=rule.name,
                )
            )
        if has_color and region_color_matches(crop, rule):
            rule_hit = True
            rule_ovs.append(
                OverlayRect(
                    rect.x,
                    rect.y,
                    rect.w,
                    rect.h,
                    stable_overlay_bgr("region-color", rect.x, rect.y, rect.w, rect.h),
                    f"{rule.name} 색상",
                )
            )
        if rule_hit:
            region_ovs.extend(rule_ovs)
            events.append(
                DetectionHitEvent(
                    id=f"region:{rule.id}",
                    name=rule.name,
                    cooldown_sec=rule.cooldown_sec,
                    custom_sound_path=rule.custom_sound_path,
                )
            )
    overlays = kw_ovs + tpl_ovs + region_ovs
    get_overlay_store().touch(overlays)

    if plain_hits:
        events.insert(0, DetectionHitEvent("main", "메인", 0.0, ""))
        return DetectionRunResult(True, "키워드 텍스트", tuple(overlays), tuple(events))
    if tpl_ovs:
        events.insert(0, DetectionHitEvent("main", "메인", 0.0, ""))
        return DetectionRunResult(True, "템플릿 이미지", tuple(overlays), tuple(events))
    if events:
        first = events[0]
        return DetectionRunResult(
            True,
            f"특정영역: {first.name}",
            tuple(overlays),
            tuple(events),
        )
    return DetectionRunResult(False, "", tuple(overlays))


def run_detection_with_overlays(
    frame_bgr: np.ndarray,
    cfg: DetectionConfig,
    stop_event: Optional[threading.Event] = None,
    kw_abort: Optional[threading.Event] = None,
) -> Tuple[bool, str, List[OverlayRect]]:
    result = run_detection_detailed(frame_bgr, cfg, stop_event, kw_abort)
    return result.triggered, result.reason, list(result.overlays)


def run_detection(
    frame_bgr: np.ndarray,
    cfg: DetectionConfig,
    stop_event: Optional[threading.Event] = None,
) -> Tuple[bool, str]:
    trig, reason, _ = run_detection_with_overlays(frame_bgr, cfg, stop_event)
    return trig, reason
