"""
macOS (Quartz): 화면에 올라온 창 나열 + CGWindowID 기준 창 픽셀 캡처.

화면 녹화 권한이 없으면 CGWindowListCreateImage 가 실패할 수 있습니다.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, List, Optional

import cv2
import numpy as np

from app_platform.models import WindowEntry

if sys.platform != "darwin":
    raise ImportError("darwin_capture는 macOS에서만 사용할 수 있습니다.")

try:
    import Quartz  # type: ignore
except ImportError as e:
    raise ImportError(
        "macOS 창 캡처에는 pyobjc-framework-Quartz 가 필요합니다. "
        "`pip install pyobjc-framework-Quartz` 후 다시 실행하세요."
    ) from e

try:
    import objc  # type: ignore
except ImportError:
    objc = None  # type: ignore


def _safe_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return None


def _window_dict_plain(win: Any) -> dict[str, Any]:
    """
    CGWindowListCopyWindowInfo 의 각 요소를 str 키 → 값 dict 로 만든다.
    PyObjC NSDictionary 는 `for k in d` 가 비어 있거나 키 매칭이 실패하는 경우가 있어
    allKeys + objectForKey_ 를 우선 사용한다.
    """
    m: dict[str, Any] = {}
    try:
        keys = win.allKeys()
        for key in keys:
            m[str(key)] = win.objectForKey_(key)
        if m:
            return m
    except Exception:
        pass
    try:
        for k, v in win.items():
            m[str(k)] = v
    except Exception:
        pass
    return m


def _cg_field(window_dict: Any, name: str) -> Any:
    """
    CGWindowListCopyWindowInfo 가 돌려주는 NSDictionary 는 환경에 따라
    Quartz 상수·str 키·CFString 등으로 섞여 올 수 있어, 여러 방식으로 조회한다.
    (키 매칭 실패 시 layer/title 을 못 읽어 목록이 비는 문제 방지)
    """
    const = getattr(Quartz, name, None)
    keys_to_try: list[Any] = []
    if const is not None:
        keys_to_try.append(const)
    keys_to_try.append(name)

    for key in keys_to_try:
        if key is None:
            continue
        try:
            if hasattr(window_dict, "objectForKey_"):
                v = window_dict.objectForKey_(key)
                if v is not None:
                    return v
        except Exception:
            pass
        try:
            if key in window_dict:
                return window_dict[key]
        except Exception:
            pass
        try:
            v = window_dict.get(key)
            if v is not None:
                return v
        except Exception:
            pass

    try:
        for k, v in window_dict.items():
            if const is not None and k == const:
                return v
            try:
                if str(k) == name:
                    return v
            except Exception:
                continue
    except Exception:
        pass
    return None


def _bounds_width_height(bounds: Any) -> tuple[int, int]:
    if bounds is None:
        return 0, 0
    if isinstance(bounds, dict):
        for kw, kh in (("Width", "Height"), ("width", "height")):
            if kw in bounds and kh in bounds:
                w = _safe_int(bounds.get(kw))
                h = _safe_int(bounds.get(kh))
                if w is not None and h is not None:
                    return w, h
    try:
        return int(bounds.size.width), int(bounds.size.height)  # NSRect/CGRect
    except Exception:
        pass
    try:
        b = _window_dict_plain(bounds)
        for kw, kh in (("Width", "Height"), ("width", "height")):
            if kw in b and kh in b:
                w = _safe_int(b.get(kw))
                h = _safe_int(b.get(kh))
                if w is not None and h is not None:
                    return w, h
    except Exception:
        pass
    return 0, 0


def _cgimage_to_bgr(cg_image) -> np.ndarray:
    """
    CGImage → BGR.
    PyObjC 래퍼가 Core Graphics 객체의 retain/release 를 맡는다.
    CGContextRelease / CGColorSpaceRelease 등을 직접 호출하면 이중 해제로 크래시할 수 있다.
    """
    w = int(Quartz.CGImageGetWidth(cg_image))
    h = int(Quartz.CGImageGetHeight(cg_image))
    if w <= 0 or h <= 0:
        raise OSError("캡처 크기가 0입니다.")
    color_space = Quartz.CGColorSpaceCreateDeviceRGB()
    if not color_space:
        raise OSError("CGColorSpaceCreateDeviceRGB 실패")
    bytes_per_row = w * 4
    nbytes = bytes_per_row * h
    buf = (ctypes.c_ubyte * nbytes)()
    ctx = Quartz.CGBitmapContextCreate(
        buf,
        w,
        h,
        8,
        bytes_per_row,
        color_space,
        Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big,
    )
    if not ctx:
        raise OSError("CGBitmapContextCreate 실패")
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), cg_image)

    arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
    return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)


def enumerate_windows(min_width: int = 80, min_height: int = 80) -> List[WindowEntry]:
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
    )
    if not raw:
        return []
    out: List[WindowEntry] = []
    for win in raw:
        layer = _cg_field(win, "kCGWindowLayer")
        # layer 를 못 읽으면 일반 앱 창으로 간주 (전부 걸러지는 것 방지)
        if layer is not None and int(layer) != 0:
            continue
        title_obj = _cg_field(win, "kCGWindowName")
        title = (str(title_obj).strip() if title_obj else "") or ""
        bounds = _cg_field(win, "kCGWindowBounds")
        bw, bh = _bounds_width_height(bounds)
        if bw < min_width or bh < min_height:
            continue
        wid_obj = _cg_field(win, "kCGWindowNumber")
        if wid_obj is None:
            continue
        wid = int(wid_obj)
        owner = _cg_field(win, "kCGWindowOwnerName")
        process_name = (
            str(owner).strip()
            if owner
            else f"pid:{_cg_field(win, 'kCGWindowOwnerPID') or '?'}"
        )
        on_screen = _cg_field(win, "kCGWindowIsOnScreen")
        if on_screen is not None and not bool(on_screen):
            continue
        # macOS 는 kCGWindowName 이 비어 있는 창이 많음 (게임·일부 네이티브 UI 등)
        if not title:
            title = f"(제목 없음) #{wid}"
        out.append(WindowEntry(hwnd=wid, title=title, process_name=process_name))
    out.sort(key=lambda e: (e.process_name.lower(), e.title.lower()))
    return out


def _win_map(win: Any) -> dict[str, Any]:
    if isinstance(win, dict):
        return {str(k): v for k, v in win.items()}
    return _window_dict_plain(win)


def _bounds_to_xywh(bounds: Any) -> Optional[tuple[int, int, int, int]]:
    if bounds is None:
        return None
    if isinstance(bounds, dict):
        b = {str(k): v for k, v in bounds.items()}
    else:
        b = _window_dict_plain(bounds)
    x = _safe_int(b.get("X") or b.get("x"))
    y = _safe_int(b.get("Y") or b.get("y"))
    w = _safe_int(b.get("Width") or b.get("width"))
    h = _safe_int(b.get("Height") or b.get("height"))
    if x is None or y is None or w is None or h is None or w < 1 or h < 1:
        return None
    return (x, y, w, h)


def _bounds_for_window_id(window_id: int) -> Optional[tuple[int, int, int, int]]:
    raw = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID,
    )
    if not raw:
        return None
    for win in raw:
        m = _win_map(win)
        wid = _safe_int(m.get("kCGWindowNumber"))
        if wid != int(window_id):
            continue
        bounds = m.get("kCGWindowBounds") or _cg_field(win, "kCGWindowBounds")
        return _bounds_to_xywh(bounds)
    return None


class MacWindowCapture:
    """CGWindowID 기준 단일 창 이미지 (BGR)."""

    def __init__(self, window_id: int):
        self._wid = int(window_id)
        self._mss: Any = None

    def grab_bgr(self) -> np.ndarray:
        def _body() -> np.ndarray:
            import mss

            # mss 는 PyObjC CGContext 변환을 거치지 않아 백그라운드 스레드에서 더 안전하다.
            xywh = _bounds_for_window_id(self._wid)
            if xywh is not None:
                x, y, w, h = xywh
                if self._mss is None:
                    self._mss = mss.mss()
                try:
                    shot = self._mss.grab({"left": x, "top": y, "width": w, "height": h})
                    arr = np.asarray(shot, dtype=np.uint8)
                    if arr.size > 0 and arr.shape[0] >= 1 and arr.shape[1] >= 1:
                        return arr[:, :, :3].copy()
                except Exception:
                    pass

            rect = Quartz.CGRectInfinite
            opt = Quartz.kCGWindowListOptionIncludingWindow
            d = Quartz.kCGWindowImageDefault
            img_opts_list: list[int] = []
            if hasattr(Quartz, "kCGWindowImageNominalResolution"):
                img_opts_list.append(d | Quartz.kCGWindowImageNominalResolution)
            img_opts_list.append(d)

            for img_opts in img_opts_list:
                img = Quartz.CGWindowListCreateImage(rect, opt, self._wid, img_opts)
                if img is None:
                    continue
                return _cgimage_to_bgr(img)

            raise OSError(
                "창 이미지를 가져오지 못했습니다. "
                "시스템 설정 > 개인 정보 보호 및 보안 > 화면 기록에서 "
                "이 앱을 실행한 프로그램(예: Terminal, Cursor, Python)을 허용한 뒤 "
                "다시 「송출 시작」하세요."
            )

        if objc is not None:
            with objc.autorelease_pool():
                return _body()
        return _body()

    def close(self) -> None:
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None
