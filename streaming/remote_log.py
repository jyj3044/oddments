"""원격 제어(호스트/뷰어) WebRTC·캡처 이벤트 전용 로그 큐.

``flet_ui.log_buffers`` 가 드레인하여 ``logs/remote-YYYYMMDD.log`` 및
원격 설정 페이지 LogConsole 에 반영한다.
``error=True`` 이면 추가로 ``logs/remote_error-YYYYMMDD.log`` 에 한 줄을 남긴다.

``log_remote_diag(...)`` 는 ``logs/remote-YYYYMMDD.log`` 에 **즉시** 한 줄을 쓰고,
원격 로그 UI 링 버퍼에도 같은 줄을 넣는다(``log_remote_event`` 큐를 쓰지 않아
드레이너가 같은 줄을 파일에 두 번 쓰지 않는다). stderr 에도 찍는다.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List

_queue: queue.SimpleQueue[str] = queue.SimpleQueue()

_remote_immediate_lock = threading.Lock()


def _log_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _append_immediate_remote_file(message: str) -> None:
    """드레인 스레드와 무관하게 ``logs/remote-YYYYMMDD.log`` 에 즉시 한 줄 추가."""
    clean = (message or "").replace("\r", " ").replace("\n", " ").strip()
    if len(clean) > 2000:
        clean = clean[:1999] + "…"
    if not clean:
        return
    try:
        d = _log_base_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"remote-{datetime.now():%Y%m%d}.log"
        ts = datetime.now().strftime("%H:%M:%S")
        with _remote_immediate_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {clean}\n")
    except Exception:
        pass


def log_remote_diag(message: str, *, error: bool = False) -> None:
    """원격 로그 UI 버퍼·즉시 파일·stderr 에 기록.

    ``log_remote_event`` 를 호출하지 않는다. 그 경로는 큐 → 드레이너가
    ``remote-*.log`` 에 다시 쓰므로, 여기서까지 쓰면 **동일 진단이 두 줄**로
    남는다(타임스탬프만 다른 중복).
    """
    ts = time.strftime("%H:%M:%S")
    m = (message or "").replace("\r", " ").replace("\n", " ").strip()
    if len(m) > 280:
        m = m[:279] + "…"
    ui_line = f"{ts}  {m}\n"
    try:
        from flet_ui.log_buffers import get_log_store

        get_log_store().remote.push_many([ui_line])
    except Exception:
        pass
    _append_immediate_remote_file(message)
    if error:
        try:
            from flet_ui.log_buffers import append_sidecar_error_file

            append_sidecar_error_file("remote", m)
        except Exception:
            pass
    try:
        print(f"[remote-diag] {(message or '').strip()}", flush=True, file=sys.stderr)
    except Exception:
        pass


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


__all__ = [
    "log_remote_event",
    "log_remote_diag",
    "drain_remote_log_lines",
    "reset_remote_log",
]
