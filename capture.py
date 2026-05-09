"""
1단계: 모니터 또는 지정 창에서 BGR 프레임 획득.

미리보기와 OCR·템플릿 감지는 CaptureThread.get_frame() 으로 **같은 최신 프레임**을
읽습니다 (별도 경로 없음).
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Union

# (송출 스레드) 동적으로 바뀌는 HWND — 롤: League of Legends.exe 우선 → UX → 선택 창
DynamicHwndResolver = Callable[[], int]

import mss
import numpy as np

from app_platform.host import make_window_capture, window_pick_supported


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
