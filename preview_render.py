"""송출 미리보기: 캡처 프레임에 감지 박스만 그리기 (OpenCV, UI 프레임워크와 분리)."""

from __future__ import annotations

import cv2
import numpy as np

from detection.common import OverlayRect


def frame_with_overlays(
    frame_bgr: np.ndarray, overlays: list[OverlayRect]
) -> np.ndarray:
    if not overlays:
        return frame_bgr
    vis = frame_bgr.copy()
    for ov in overlays:
        b, g, r = ov.color_bgr
        cv2.rectangle(
            vis,
            (ov.x, ov.y),
            (ov.x + ov.w, ov.y + ov.h),
            (b, g, r),
            2,
        )
    return vis
