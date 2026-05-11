"""macOS: 가상 디스플레이 원격 시 물리 NSScreen 위에 전체화면 봉인 오버레이.

봉인 창은 **별도 subprocess**(`darwin_seal_worker.py`)로 실행된다.
Flet/Flutter 메인 스레드와 완전히 분리되므로, CGCompleteDisplayConfiguration 등으로
메인 스레드가 일시 점유되더라도 봉인 창 생성에 영향이 없다.

subprocess 통신:
  stdin  → "quit\\n"       : 창 닫고 종료
  stdout ← "disconnect\\n" : 호스트가 '세션 종료' 버튼 누름
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

if sys.platform != "darwin":
    raise ImportError("darwin_remote_seal 은 macOS 전용입니다.")

_log = logging.getLogger(__name__)

_WORKER = Path(__file__).parent / "darwin_seal_worker.py"

_lock = threading.Lock()
_proc: subprocess.Popen | None = None  # type: ignore[type-arg]


def _kill_proc(proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
    try:
        proc.stdin.write(b"quit\n")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass


def _hide_sync() -> None:
    global _proc
    p = _proc
    _proc = None
    if p is not None:
        _kill_proc(p)


def schedule_seal_hide(
    *,
    ui_runner: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    with _lock:
        _hide_sync()


def schedule_seal_show(
    virtual_display_id: int,
    on_disconnect: Callable[[], None],
    *,
    ui_runner: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    vid = int(virtual_display_id)
    if vid <= 0:
        _log.warning("darwin_remote_seal: virtual_display_id=%s 무시", vid)
        return

    with _lock:
        # 이미 subprocess 가 실행 중이면 재시작하지 않는다 (_fire_retry 등 중복 호출 방어).
        if _proc is not None and _proc.poll() is None:
            return

        _hide_sync()

        try:
            proc = subprocess.Popen(
                [sys.executable, str(_WORKER), str(vid)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                close_fds=True,
            )
        except Exception:
            _log.exception("darwin_remote_seal: subprocess 실행 실패")
            return

        global _proc
        _proc = proc

    def _read_stdout() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip().decode("utf-8", errors="replace")
                if line == "disconnect":
                    try:
                        on_disconnect()
                    except Exception:
                        _log.exception("darwin_remote_seal: on_disconnect 콜백 실패")
        except Exception:
            pass

    threading.Thread(target=_read_stdout, daemon=True).start()

    try:
        from streaming.remote_log import log_remote_event

        log_remote_event(f"호스트: 봉인 subprocess 시작 (vid={vid}, pid={proc.pid})")
    except Exception:
        pass


__all__ = [
    "schedule_seal_hide",
    "schedule_seal_show",
]
