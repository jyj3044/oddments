"""템플릿(부분 이미지) 다중 매칭."""

from __future__ import annotations

import os
import time
from typing import List, Optional, Tuple

import cv2
import numpy as np

from .common import OverlayRect, stable_overlay_bgr

# 약간의 스케일·압축 차이를 흡수 (캡처와 템플릿 파일 해상도 불일치 완화)
_TEMPLATE_SCALE_FACTORS: Tuple[float, ...] = (0.92, 0.96, 1.0, 1.04, 1.08)


def load_template_bgr(path: str) -> Optional[np.ndarray]:
    """BGR uint8. PNG 알파는 흰 배경에 합성."""
    if not path or not os.path.isfile(path):
        return None
    im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if im is None or im.size == 0:
        return None
    if im.ndim == 2:
        return cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
    if im.shape[2] == 3:
        return np.ascontiguousarray(im)
    if im.shape[2] == 4:
        bgr = im[:, :, :3].astype(np.float32)
        a = im[:, :, 3:4].astype(np.float32) / 255.0
        white = np.full_like(bgr, 255.0)
        out = bgr * a + white * (1.0 - a)
        return np.ascontiguousarray(np.clip(out, 0, 255).astype(np.uint8))
    return None


def _match_at_scale(
    frame_bgr: np.ndarray, tpl_bgr: np.ndarray, threshold: float
) -> Tuple[Optional[Tuple[int, int, int, int]], float]:
    """(임계 이상일 때만 사각형, 항상 최대 상관값 TM_CCOEFF_NORMED)."""
    fh, fw = frame_bgr.shape[:2]
    th0, tw0 = tpl_bgr.shape[:2]
    if th0 < 2 or tw0 < 2:
        return None, 0.0
    if th0 > fh or tw0 > fw:
        return None, 0.0
    frame_bgr = np.ascontiguousarray(frame_bgr)
    tpl_bgr = np.ascontiguousarray(tpl_bgr)
    try:
        res = cv2.matchTemplate(frame_bgr, tpl_bgr, cv2.TM_CCOEFF_NORMED)
    except cv2.error:
        return None, 0.0
    _min_v, max_v, _min_loc, max_loc = cv2.minMaxLoc(res)
    mv = float(max_v)
    if mv < threshold:
        return None, mv
    return (int(max_loc[0]), int(max_loc[1]), tw0, th0), mv


def match_one_template(
    frame_bgr: np.ndarray, template_path: str, threshold: float
) -> Tuple[Optional[Tuple[OverlayRect, float]], float]:
    """(히트 시 (오버레이, 점수), 스케일 전체에서 관측한 최대 상관값)."""
    tpl0 = load_template_bgr(template_path)
    if tpl0 is None:
        return None, -1.0
    best: Optional[Tuple[int, int, int, int, float]] = None
    best_sc = threshold
    max_seen = 0.0
    for sf in _TEMPLATE_SCALE_FACTORS:
        tw = max(2, int(round(tpl0.shape[1] * sf)))
        th = max(2, int(round(tpl0.shape[0] * sf)))
        if tw != tpl0.shape[1] or th != tpl0.shape[0]:
            tpl = cv2.resize(
                tpl0,
                (tw, th),
                interpolation=cv2.INTER_AREA if sf < 1.0 else cv2.INTER_LINEAR,
            )
        else:
            tpl = tpl0
        rect, mv = _match_at_scale(frame_bgr, tpl, threshold)
        max_seen = max(max_seen, mv)
        if rect is not None and mv > best_sc:
            x, y, rtw, rth = rect
            best = (x, y, rtw, rth, mv)
            best_sc = mv
    if best is None:
        return None, max_seen
    x, y, tw, th, sc = best
    base = os.path.basename(template_path)
    label = f"템플릿:{base}"
    ov = OverlayRect(
        x, y, tw, th, stable_overlay_bgr("tpl", x, y, tw, th), label
    )
    return (ov, sc), max_seen


def match_all_templates(
    frame_bgr: np.ndarray,
    template_paths: Tuple[str, ...],
    threshold: float,
) -> List[OverlayRect]:
    from .ocr_diag import begin_ocr_call, end_ocr_call

    fh, fw = frame_bgr.shape[:2]
    shape_prefix = f"{fw}x{fh}"

    out: List[OverlayRect] = []
    seen_rect: set[Tuple[int, int, int, int]] = set()
    for p in template_paths:
        p = (p or "").strip()
        if not p:
            continue
        base = os.path.basename(p)
        detail_call = f"{shape_prefix} {base}"
        cid = begin_ocr_call("match", "template", detail_call)
        t0 = time.perf_counter()
        hit, max_seen = match_one_template(frame_bgr, p, threshold)
        elapsed = time.perf_counter() - t0
        if hit is None:
            if max_seen < 0.0:
                end_detail = f"{shape_prefix} {base} 로드 실패"
            else:
                end_detail = (
                    f"{shape_prefix} {base} 미일치 max={max_seen:.3f} "
                    f"임계={threshold:.2f}"
                )
            end_ocr_call(
                cid,
                "match",
                "template",
                elapsed,
                end_detail,
                count_completed=False,
            )
            continue
        ov, sc = hit
        end_ocr_call(
            cid,
            "match",
            "template",
            elapsed,
            f"{shape_prefix} {base} 일치 score={sc:.3f}",
            count_completed=False,
        )
        key = (ov.x, ov.y, ov.w, ov.h)
        if key in seen_rect:
            continue
        seen_rect.add(key)
        out.append(ov)
    return out

