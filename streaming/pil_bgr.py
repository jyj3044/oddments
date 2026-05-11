"""BGR ndarray 리사이즈 — OpenCV 대신 Pillow."""

from __future__ import annotations

import numpy as np
from PIL import Image

_LANCZOS = Image.Resampling.LANCZOS


def resize_bgr(bgr: np.ndarray, width: int, height: int) -> np.ndarray:
    w, h = max(1, int(width)), max(1, int(height))
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    im = Image.fromarray(rgb)
    im = im.resize((w, h), _LANCZOS)
    out = np.asarray(im)
    return np.ascontiguousarray(out[:, :, ::-1])
