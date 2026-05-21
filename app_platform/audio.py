"""플랫폼별 알림음 (Windows winsound · macOS afplay)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import List


_afplay_lock = threading.Lock()
_afplay_children: List[subprocess.Popen] = []


def stop_queued_alert_sounds() -> None:
    """비동기로 예약된 알림 재생을 가능한 한 중단합니다."""
    if sys.platform == "win32":
        import winsound

        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass
        return

    if sys.platform == "darwin":
        with _afplay_lock:
            for p in list(_afplay_children):
                if p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
            _afplay_children.clear()


def play_alert_sound(path: str | None = None) -> None:
    if sys.platform == "win32":
        import winsound

        if path:
            try:
                winsound.PlaySound(
                    path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NOSTOP,
                )
                return
            except Exception:
                pass
        flags = (
            winsound.SND_ALIAS
            | winsound.SND_ASYNC
            | winsound.SND_NOSTOP
        )
        winsound.PlaySound("SystemExclamation", flags)
        return

    if sys.platform == "darwin":
        try:
            sound_path = path if path and os.path.isfile(path) else "/System/Library/Sounds/Glass.aiff"
            p = subprocess.Popen(
                [
                    "afplay",
                    sound_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with _afplay_lock:
                _afplay_children.append(p)
                _afplay_children[:] = [x for x in _afplay_children if x.poll() is None]
        except Exception:
            print("\a", end="", flush=True)
        return

    print("\a", end="", flush=True)
