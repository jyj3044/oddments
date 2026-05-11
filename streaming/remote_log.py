"""원격 제어(호스트/뷰어) WebRTC·캡처 이벤트 전용 로그 큐.

``flet_ui.log_buffers`` 가 드레인하여 ``logs/remote-YYYYMMDD.log`` 및
원격 설정 페이지 LogConsole 에 반영한다.
``error=True`` 이면 추가로 ``logs/remote_error-YYYYMMDD.log`` 에 한 줄을 남긴다.
"""

from __future__ import annotations

import queue
import time
from typing import List

_queue: queue.SimpleQueue[str] = queue.SimpleQueue()


def log_remote_event(message: str, *, error: bool = False) -> None:
    ts = time.strftime("%H:%M:%S")
    m = (message or "").replace("\r", " ").replace("\n", " ").strip()
    if len(m) > 280:
        m = m[:279] + "…"
    _queue.put(f"{ts}  {m}\n")
    if error:
        try:
            from flet_ui.log_buffers import append_sidecar_error_file

            append_sidecar_error_file("remote", m)
        except Exception:
            pass


def drain_remote_log_lines(max_n: int = 200) -> List[str]:
    out: List[str] = []
    for _ in range(max_n):
        try:
            out.append(_queue.get_nowait())
        except queue.Empty:
            break
    return out


def reset_remote_log() -> None:
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break


__all__ = ["log_remote_event", "drain_remote_log_lines", "reset_remote_log"]
