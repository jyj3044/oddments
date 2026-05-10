"""웹 송출 접속·WebRTC 이벤트 전용 로그 큐 (UI 웹 설정 창에서 소비)."""

from __future__ import annotations

import queue
import time
from typing import List

_queue: queue.SimpleQueue[str] = queue.SimpleQueue()


def log_web_event(message: str) -> None:
    ts = time.strftime("%H:%M:%S")
    m = (message or "").replace("\r", " ").replace("\n", " ").strip()
    if len(m) > 280:
        m = m[:279] + "…"
    _queue.put(f"{ts}  {m}\n")


def drain_web_log_lines(max_n: int = 200) -> List[str]:
    out: List[str] = []
    for _ in range(max_n):
        try:
            out.append(_queue.get_nowait())
        except queue.Empty:
            break
    return out


def reset_web_log() -> None:
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
