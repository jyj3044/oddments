"""
1단계: 모니터 또는 지정 창에서 BGR 프레임 획득.

미리보기와 OCR·템플릿 감지는 CaptureThread.get_frame() 으로 **같은 최신 프레임**을
읽습니다 (별도 경로 없음).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable, Optional, Union

# (송출 스레드) 동적으로 바뀌는 HWND — 롤: League of Legends.exe 우선 → UX → 선택 창
DynamicHwndResolver = Callable[[], int]

import mss
import numpy as np

from app_platform.host import make_window_capture, window_pick_supported


def _parse_edid_monitor_name(edid: bytes) -> Optional[str]:
    """EDID 128바이트에서 0xFC (Monitor Name) 디스크립터를 ASCII 로 디코드.

    EDID 1.x 의 4개 디스크립터(오프셋 54/72/90/108, 각 18바이트) 를 순회한다.
    Monitor 디스크립터는 첫 3바이트가 0x00 0x00 0x00 이고 4번째 바이트가 타입,
    type=0xFC 이면 5–17 바이트가 ASCII 모델명이다(0x0A 종료, 0x20 패딩).
    """
    if not edid or len(edid) < 128:
        return None
    for off in (54, 72, 90, 108):
        block = edid[off : off + 18]
        if len(block) < 18:
            continue
        if block[0] == 0 and block[1] == 0 and block[2] == 0 and block[3] == 0xFC:
            try:
                text = bytes(block[5:18]).decode("ascii", errors="ignore")
            except Exception:  # noqa: BLE001
                continue
            if "\n" in text:
                text = text.split("\n", 1)[0]
            text = text.strip()
            if text:
                return text
    return None


def _read_edid_name_from_registry(device_id: str) -> Optional[str]:
    """``EnumDisplayDevicesW`` 가 돌려준 DeviceID 에서 EDID 모니터 이름 파싱.

    DeviceID 형식 예: ``MONITOR\\MSI4D02\\{4d36e96e-...}\\0001``.
    여기서 ``MSI4D02`` 가 hardware id 라 레지스트리 경로
    ``HKLM\\SYSTEM\\CurrentControlSet\\Enum\\DISPLAY\\MSI4D02\\<instance>``
    아래의 ``Device Parameters\\EDID`` 바이너리를 읽어 파싱한다.
    """
    if sys.platform != "win32":
        return None
    if not device_id:
        return None
    parts = device_id.split("\\")
    if len(parts) < 2 or parts[0].upper() != "MONITOR":
        return None
    hwid = parts[1]
    try:
        import winreg
    except Exception:  # noqa: BLE001
        return None

    base = f"SYSTEM\\CurrentControlSet\\Enum\\DISPLAY\\{hwid}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as base_key:
            i = 0
            while True:
                try:
                    instance = winreg.EnumKey(base_key, i)
                except OSError:
                    break
                i += 1
                try:
                    sub = f"{base}\\{instance}\\Device Parameters"
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                        try:
                            edid, _ = winreg.QueryValueEx(k, "EDID")
                        except FileNotFoundError:
                            continue
                        name = _parse_edid_monitor_name(bytes(edid or b""))
                        if name:
                            return name
                except OSError:
                    continue
    except OSError:
        return None
    return None


def _win32_monitor_friendly_names() -> list[dict]:
    """Win32 EnumDisplayMonitors + EnumDisplayDevices + EDID 로 모니터 이름 수집.

    이름 우선순위:

    1. ``EDID`` 의 0xFC Monitor Name (예: "MSI MAG274QRF", "DELL U2415")
       — 모니터가 자기 자신을 보고하는 가장 정확한 이름.
    2. ``EnumDisplayDevicesW`` 의 ``DeviceString`` (예: "LG ULTRAGEAR")
       — 드라이버 INF 가 보고하는 이름. 전용 드라이버 없는 경우
       대부분 "Generic PnP Monitor" 가 나오기 때문에 폴백으로만 사용.
    3. 둘 다 못 얻으면 ``None``.

    Returns:
        ``[{"left", "top", "right", "bottom", "name"}, ...]``
    """
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return []

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("DeviceName", wintypes.WCHAR * 32),
            ("DeviceString", wintypes.WCHAR * 128),
            ("StateFlags", wintypes.DWORD),
            ("DeviceID", wintypes.WCHAR * 128),
            ("DeviceKey", wintypes.WCHAR * 128),
        ]

    MonitorEnumProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )

    out: list[dict] = []

    def _callback(hmon, _hdc, _lprect, _lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return 1
        adapter = info.szDevice
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        name: Optional[str] = None
        idx = 0
        while user32.EnumDisplayDevicesW(adapter, idx, ctypes.byref(dd), 0):
            device_id = (dd.DeviceID or "")
            edid_name = _read_edid_name_from_registry(device_id)
            if edid_name:
                name = edid_name
                break
            cand = (dd.DeviceString or "").strip()
            if cand and not name:
                # 드라이버 보고 이름은 일단 보관해 두고 다음 모니터 디바이스에 EDID 가
                # 있는지 마저 시도. 전부 EDID 가 비어 있으면 이걸 폴백으로 사용.
                name = cand
            idx += 1
        out.append(
            {
                "left": int(info.rcMonitor.left),
                "top": int(info.rcMonitor.top),
                "right": int(info.rcMonitor.right),
                "bottom": int(info.rcMonitor.bottom),
                "name": name,
            }
        )
        return 1

    try:
        if not user32.EnumDisplayMonitors(
            None, None, MonitorEnumProc(_callback), 0
        ):
            return []
    except Exception:  # noqa: BLE001
        return []
    return out


def enumerate_monitors() -> list[dict]:
    """현재 시스템에 연결된 물리 모니터 목록.

    Returns:
        ``[{"index": 1, "left": 0, "top": 0, "width": 1920, "height": 1080,
            "name": "LG ULTRAGEAR" | None}, ...]``
        ``mss`` 규칙상 ``monitors[0]`` 은 모든 모니터를 합친 *가상 화면* 이므로
        실제 물리 모니터인 1번부터만 노출한다. ``name`` 은 Windows 에서만
        EDID 기반으로 채워지며(가능한 경우), 그 외 OS 나 조회 실패 시 ``None``.
        호출 실패 시 빈 리스트.
    """
    out: list[dict] = []
    try:
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors):
                if i == 0:
                    continue
                try:
                    out.append(
                        {
                            "index": int(i),
                            "left": int(m.get("left", 0)),
                            "top": int(m.get("top", 0)),
                            "width": int(m.get("width", 0)),
                            "height": int(m.get("height", 0)),
                            "name": None,
                        }
                    )
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        return out

    # Windows: ctypes 로 얻은 친근한 이름을 좌표로 매칭해 채워 넣는다. 실패해도
    # name 은 None 으로 남고 UI 는 인덱스/해상도만으로 동작한다.
    win_info = _win32_monitor_friendly_names()
    for entry in out:
        l = entry["left"]
        t = entry["top"]
        r = l + entry["width"]
        b = t + entry["height"]
        for w in win_info:
            if (
                w["left"] == l
                and w["top"] == t
                and w["right"] == r
                and w["bottom"] == b
            ):
                entry["name"] = w.get("name")
                break
    return out


class ScreenCapture:
    """단일 모니터 화면을 BGR uint8 numpy 배열로 캡처."""

    def __init__(self, monitor_index: int = 1):
        """
        monitor_index: mss 규칙 — 0은 모든 모니터 합친 가상 화면, 1부터 각 모니터.
        """
        self.monitor_index = monitor_index
        self._sct: Optional[object] = None

    def _mss(self):
        if self._sct is None:
            self._sct = mss.mss()
        return self._sct

    def grab_bgr(self) -> np.ndarray:
        """현재 프레임을 BGR (H, W, 3) 로 반환."""
        sct = self._mss()
        mon = sct.monitors[self.monitor_index]
        shot = sct.grab(mon)
        # BGRA -> BGR
        frame = np.asarray(shot, dtype=np.uint8)
        return frame[:, :, :3].copy()

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None


def _make_capture(
    *,
    monitor_index: int,
    window_hwnd: Optional[int],
) -> Union[ScreenCapture, object]:
    if window_hwnd is not None:
        if not window_pick_supported():
            raise OSError("창 캡처는 이 운영체제에서 지원되지 않습니다.")
        return make_window_capture(window_hwnd)
    return ScreenCapture(monitor_index=monitor_index)


class CaptureThread(threading.Thread):
    """
    백그라운드에서 주기적으로 캡처하여 최신 프레임만 보관.
    GUI 미리보기와 감지 스레드는 get_frame() 으로 동일 버퍼를 읽습니다.
    """

    def __init__(
        self,
        monitor_index: int = 1,
        target_fps: float = 30.0,
        on_frame: Optional[Callable[[np.ndarray], None]] = None,
        window_hwnd: Optional[int] = None,
        dynamic_hwnd_resolver: Optional[DynamicHwndResolver] = None,
    ):
        super().__init__(daemon=True)
        self._monitor_index = monitor_index
        self._window_hwnd = window_hwnd
        self._dynamic_hwnd_resolver = dynamic_hwnd_resolver
        self._interval = 1.0 / max(target_fps, 1.0)
        self._on_frame = on_frame
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._frame_seq: int = 0
        self._capture_error: Optional[str] = None

    def get_capture_error(self) -> Optional[str]:
        """창·화면 캡처가 연속 실패할 때 마지막 예외 메시지 (성공 시 None)."""
        with self._lock:
            return self._capture_error

    def get_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._latest is None:
                return None
            return self._latest.copy()

    def get_frame_seq(self) -> int:
        """grab 성공 시마다 증가. 미리보기에서 동일 프레임이면 그리기 생략용."""
        with self._lock:
            return self._frame_seq

    def stop(self) -> None:
        self._running.clear()

    def run(self) -> None:
        self._running.set()
        cap: Optional[Union[ScreenCapture, object]] = None
        current_hwnd: Optional[int] = None
        next_resolve_t = 0.0
        resolve_every_s = 0.15
        try:
            while self._running.is_set():
                t0 = time.perf_counter()
                sleep_for = self._interval
                try:
                    if self._window_hwnd is None:
                        if cap is None:
                            cap = _make_capture(
                                monitor_index=self._monitor_index,
                                window_hwnd=None,
                            )
                        frame = cap.grab_bgr()
                    else:
                        if self._dynamic_hwnd_resolver is not None:
                            if t0 >= next_resolve_t:
                                try:
                                    target_hwnd = int(self._dynamic_hwnd_resolver())
                                except Exception:
                                    target_hwnd = int(self._window_hwnd)
                                next_resolve_t = t0 + resolve_every_s
                            else:
                                target_hwnd = (
                                    current_hwnd
                                    if current_hwnd is not None
                                    else int(self._window_hwnd)
                                )
                        else:
                            target_hwnd = int(self._window_hwnd)

                        if cap is None or current_hwnd != target_hwnd:
                            if cap is not None:
                                cap.close()
                            cap = _make_capture(
                                monitor_index=self._monitor_index,
                                window_hwnd=target_hwnd,
                            )
                            current_hwnd = target_hwnd

                        frame = cap.grab_bgr()

                    with self._lock:
                        self._latest = frame
                        self._frame_seq += 1
                        self._capture_error = None
                    if self._on_frame:
                        self._on_frame(frame)
                except Exception as e:
                    with self._lock:
                        if self._capture_error is None:
                            self._capture_error = f"{type(e).__name__}: {e}"
                    sleep_for = max(self._interval, 0.25)
                elapsed = time.perf_counter() - t0
                wait = sleep_for - elapsed
                if wait > 0:
                    time.sleep(wait)
        finally:
            if cap is not None:
                cap.close()


if __name__ == "__main__":
    import cv2

    cap = ScreenCapture(monitor_index=1)
    img = cap.grab_bgr()
    cap.close()

    cv2.imwrite("capture_test.png", img)
    print("저장됨: capture_test.png", img.shape)
