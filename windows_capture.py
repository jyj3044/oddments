"""
Windows 전용: 보이는 최상위 창 나열 + 창 HWND 기준 화면 영역 캡처.

HWND·창 테두리 좌표만 사용하며 게임 프로세스 메모리는 읽지 않습니다.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

import mss
import numpy as np

from app_platform.models import WindowEntry

if sys.platform != "win32":
    raise ImportError("windows_capture는 Windows에서만 사용할 수 있습니다.")

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

HWND = wintypes.HWND
LPARAM = wintypes.LPARAM
BOOL = wintypes.BOOL
DWORD = wintypes.DWORD

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020


def ensure_windows_dpi_awareness() -> None:
    """
    Tk·mss·GetWindowRect 좌표계를 맞추기 위해 가능한 한 먼저 호출하세요.
    (main()에서 Tk() 생성 전에 호출하는 것이 좋습니다.)
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def _exe_basename_from_pid(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, DWORD(pid))
    if not h:
        return f"pid:{pid}"
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = DWORD(1024)
        qfpn = kernel32.QueryFullProcessImageNameW
        qfpn.argtypes = [wintypes.HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)]
        qfpn.restype = BOOL
        if qfpn(h, DWORD(0), buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return f"pid:{pid}"


def enumerate_windows(min_width: int = 80, min_height: int = 80) -> List[WindowEntry]:
    """보이는 최상위 창 목록 (제목 비어 있음·너무 작은 창 제외)."""
    out: List[WindowEntry] = []

    @ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = (buf.value or "").strip()
        if not title:
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w < min_width or h < min_height:
            return True
        pid = DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe_basename_from_pid(pid.value)
        out.append(WindowEntry(hwnd=int(hwnd), title=title, process_name=exe))
        return True

    user32.EnumWindows(callback, LPARAM(0))
    out.sort(key=lambda e: (e.process_name.lower(), e.title.lower()))
    return out


class _RECT_L(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _window_rect_for_capture(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, width, height) — DWM 확장 프레임 우선, 실패 시 GetWindowRect."""
    h = HWND(hwnd)
    r = _RECT_L()
    hr = dwmapi.DwmGetWindowAttribute(
        h,
        DWORD(DWMWA_EXTENDED_FRAME_BOUNDS),
        ctypes.byref(r),
        DWORD(ctypes.sizeof(r)),
    )
    if hr == 0 and r.right > r.left and r.bottom > r.top:
        return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)
    wr = wintypes.RECT()
    if not user32.GetWindowRect(h, ctypes.byref(wr)):
        raise OSError("창 위치를 읽을 수 없습니다.")
    return (
        int(wr.left),
        int(wr.top),
        int(wr.right - wr.left),
        int(wr.bottom - wr.top),
    )


def _window_rect_for_printwindow(hwnd: int) -> tuple[int, int, int, int]:
    """
    (left, top, width, height) — PrintWindow용 기준.
    DWM 확장 프레임 대신 GetWindowRect를 사용해 GDI 윈도우 DC 좌표와 맞춘다.
    """
    h = HWND(hwnd)
    wr = wintypes.RECT()
    if not user32.GetWindowRect(h, ctypes.byref(wr)):
        raise OSError("창 위치를 읽을 수 없습니다.")
    return (
        int(wr.left),
        int(wr.top),
        int(wr.right - wr.left),
        int(wr.bottom - wr.top),
    )


def _client_rect_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    """클라이언트 영역을 화면 좌표로. (left, top, w, h)"""
    h = HWND(hwnd)
    cr = wintypes.RECT()
    if not user32.GetClientRect(h, ctypes.byref(cr)):
        return None
    w = int(cr.right - cr.left)
    hgt = int(cr.bottom - cr.top)
    if w <= 0 or hgt <= 0:
        return None
    pt = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(h, ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y), w, hgt


def _frame_looks_black(bgr: np.ndarray) -> bool:
    """캡처 실패(잘못된 영역·합성) 시 흔한 순수 검화면."""
    if bgr.size == 0:
        return True
    return float(np.mean(bgr)) < 1.2 and int(np.max(bgr)) < 10


def _grab_bgr_screen_bitblt(left: int, top: int, width: int, height: int) -> np.ndarray:
    """
    전체 화면 DC에서 (left,top)~(width,height) 영역을 GDI BitBlt로 복사.
    크롬·일반 창은 DXGI(mss)보다 이 경로가 잘 맞는 경우가 많습니다.
    """
    if width <= 0 or height <= 0:
        raise ValueError("캡처 크기가 올바르지 않습니다.")
    hdc_screen = user32.GetDC(HWND(0))
    if not hdc_screen:
        raise OSError("GetDC(화면) 실패")
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            raise OSError("CreateCompatibleDC 실패")
        try:
            hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
            if not hbmp:
                raise OSError("CreateCompatibleBitmap 실패")
            try:
                old = gdi32.SelectObject(hdc_mem, hbmp)
                ok = gdi32.BitBlt(
                    hdc_mem,
                    0,
                    0,
                    width,
                    height,
                    hdc_screen,
                    left,
                    top,
                    SRCCOPY,
                )
                gdi32.SelectObject(hdc_mem, old)
                if not ok:
                    raise OSError("BitBlt 실패")
                size = width * height * 4
                buf = ctypes.create_string_buffer(size)
                n = gdi32.GetBitmapBits(hbmp, size, buf)
                if n <= 0:
                    raise OSError("GetBitmapBits 실패")
                arr = np.frombuffer(buf, dtype=np.uint8, count=size).reshape(
                    (height, width, 4)
                )
                # CreateCompatibleBitmap + GetBitmapBits 는 이 경로에서 위→아래 순서로 옴
                return np.ascontiguousarray(arr[:, :, :3])
            finally:
                gdi32.DeleteObject(hbmp)
        finally:
            gdi32.DeleteDC(hdc_mem)
    finally:
        user32.ReleaseDC(HWND(0), hdc_screen)


def _crop_image_to_client_area(
    img: np.ndarray,
    hwnd: int,
    win_left: int,
    win_top: int,
) -> np.ndarray:
    """PrintWindow(전체 창) 결과에서 화면상 클라이언트에 해당하는 부분만 잘라 냄."""
    if img.size == 0:
        return img
    cc = _client_rect_screen(hwnd)
    if cc is None:
        return img
    cl, ct, cw, ch = cc
    ox = cl - win_left
    oy = ct - win_top
    ih, iw = img.shape[:2]
    if cw < 16 or ch < 16 or ox < 0 or oy < 0:
        return img
    cw = min(cw, iw - ox)
    ch = min(ch, ih - oy)
    if cw < 16 or ch < 16:
        return img
    return img[oy : oy + ch, ox : ox + cw].copy()


def _grab_bgr_printwindow(hwnd: int, width: int, height: int) -> np.ndarray | None:
    """PrintWindow + 비트맵 — 일부 D3D/창 합성에서 mss만 검은 경우에 유효."""
    if width <= 0 or height <= 0:
        return None
    h = HWND(hwnd)
    hdc_src = user32.GetWindowDC(h)
    if not hdc_src:
        return None
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_src)
        if not hdc_mem:
            return None
        try:
            hbmp = gdi32.CreateCompatibleBitmap(hdc_src, width, height)
            if not hbmp:
                return None
            try:
                old = gdi32.SelectObject(hdc_mem, hbmp)
                ok = user32.PrintWindow(h, hdc_mem, PW_RENDERFULLCONTENT)
                if not ok:
                    user32.PrintWindow(h, hdc_mem, 0)
                gdi32.SelectObject(hdc_mem, old)
                row_bytes = width * 4
                size = row_bytes * height
                buf = ctypes.create_string_buffer(size)
                n = gdi32.GetBitmapBits(hbmp, size, buf)
                if n <= 0:
                    return None
                arr = np.frombuffer(buf, dtype=np.uint8, count=size).reshape(
                    (height, width, 4)
                )
                return np.ascontiguousarray(arr[:, :, :3])
            finally:
                gdi32.DeleteObject(hbmp)
        finally:
            gdi32.DeleteDC(hdc_mem)
    finally:
        user32.ReleaseDC(h, hdc_src)


class WindowCapture:
    """
    HWND 기준 캡처.
    실제로 보이는 그리기 영역에 가깝도록 **클라이언트 영역(화면 좌표)** 을
    전체 창(DWM 테두리)보다 먼저 시도합니다. (게임·브라우저에서 바깥 프레임·
    안 보이는 영역이 섞이는 현상 완화)
    순서: PrintWindow(전체→클라 crop) → 클라 BitBlt → 클라 mss → 전체 창 BitBlt → 전체 mss
    (겹침 창이 함께 캡처되는 현상을 줄이기 위해 PrintWindow 경로를 우선 시도)
    """

    def __init__(self, hwnd: int):
        self.hwnd = int(hwnd)
        self._sct: Optional[object] = None

    def _mss(self):
        if self._sct is None:
            self._sct = mss.mss()
        return self._sct

    def grab_bgr(self) -> np.ndarray:
        if not user32.IsWindow(self.hwnd):
            raise OSError("창이 더 이상 존재하지 않습니다.")
        left, top, w, h = _window_rect_for_capture(self.hwnd)
        if w <= 0 or h <= 0:
            raise OSError("창 크기가 0입니다 (최소화됨?).")
        win_area = max(1, w * h)

        # 창 위에 다른 창이 겹쳐도 대상 창 자체를 우선 얻기 위해 PrintWindow 먼저 시도.
        # 일부 앱(D3D/보안/관리자 권한 차이)은 PrintWindow를 거부할 수 있어 실패 시 화면 복사 경로로 폴백.
        pw_left, pw_top, pw_w, pw_h = _window_rect_for_printwindow(self.hwnd)
        alt = _grab_bgr_printwindow(self.hwnd, pw_w, pw_h)
        if alt is not None:
            alt = _crop_image_to_client_area(alt, self.hwnd, pw_left, pw_top)
            if not _frame_looks_black(alt):
                return alt

        def grab_mss_region(l: int, t: int, ww: int, hh: int) -> np.ndarray:
            reg = {"left": l, "top": t, "width": ww, "height": hh}
            sh = self._mss().grab(reg)
            fr = np.asarray(sh, dtype=np.uint8)
            return fr[:, :, :3].copy()

        candidates: list[np.ndarray] = []

        def try_rect(cl: int, ct: int, cw: int, ch: int) -> np.ndarray | None:
            if cw <= 0 or ch <= 0:
                return None
            try:
                bb = _grab_bgr_screen_bitblt(cl, ct, cw, ch)
                candidates.append(bb)
                if not _frame_looks_black(bb):
                    return bb
            except OSError:
                pass
            try:
                ms = grab_mss_region(cl, ct, cw, ch)
                candidates.append(ms)
                if not _frame_looks_black(ms):
                    return ms
            except Exception:
                pass
            return None

        client = _client_rect_screen(self.hwnd)
        prefer_client = False
        if client is not None:
            cl, ct, cw, ch = client
            ca = cw * ch
            if ca >= 0.22 * win_area and cw >= 64 and ch >= 64:
                prefer_client = True
                got = try_rect(cl, ct, cw, ch)
                if got is not None:
                    return got

        got = try_rect(left, top, w, h)
        if got is not None:
            return got

        if client is not None and not prefer_client:
            cl, ct, cw, ch = client
            if cw > 0 and ch > 0:
                got = try_rect(cl, ct, cw, ch)
                if got is not None:
                    return got

        for c in reversed(candidates):
            if c.size:
                return c
        raise OSError("창 이미지를 가져오지 못했습니다.")

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None
