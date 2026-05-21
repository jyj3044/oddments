"""플랫폼별 알림음 (Windows winsound/MCI · macOS afplay)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import List

_MCI_ALIAS = "oddments_alert"

_afplay_lock = threading.Lock()
_afplay_children: List[subprocess.Popen] = []


def resolve_alert_sound_path(path: str | None) -> str | None:
    """존재하는 알림음 파일 경로만 반환한다."""
    if not path or not str(path).strip():
        return None
    candidate = os.path.abspath(os.path.expanduser(str(path).strip()))
    return candidate if os.path.isfile(candidate) else None


def _win_mci_send(command: str) -> bool:
    import ctypes

    err = ctypes.create_unicode_buffer(256)
    rc = ctypes.windll.winmm.mciSendStringW(command, err, len(err), None)
    return int(rc) == 0


def _stop_windows_mci() -> None:
    try:
        _win_mci_send(f"close {_MCI_ALIAS}")
    except Exception:
        pass


def _play_custom_sound_windows(path: str) -> bool:
    """Windows: WAV 는 winsound, MP3 등은 winmm MCI."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        import winsound

        try:
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NOSTOP,
            )
            return True
        except Exception:
            return False

    _stop_windows_mci()
    open_cmds: list[str] = []
    if ext in (".mp3", ".mpeg", ".mpg"):
        open_cmds.append(f'open "{path}" type mpegvideo alias {_MCI_ALIAS}')
    if ext in (".aif", ".aiff"):
        open_cmds.append(f'open "{path}" type waveaudio alias {_MCI_ALIAS}')
    open_cmds.append(f'open "{path}" alias {_MCI_ALIAS}')
    for cmd in open_cmds:
        if _win_mci_send(cmd) and _win_mci_send(f"play {_MCI_ALIAS}"):
            return True
    return False


def _play_default_sound_windows() -> bool:
    import winsound

    try:
        flags = winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NOSTOP
        winsound.PlaySound("SystemExclamation", flags)
        return True
    except Exception:
        return False


def stop_queued_alert_sounds() -> None:
    """비동기로 예약된 알림 재생을 가능한 한 중단합니다."""
    if sys.platform == "win32":
        import winsound

        _stop_windows_mci()
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


def play_alert_sound(path: str | None = None) -> bool:
    """알림음 재생을 시작했으면 True. 실패 시 False (쿨다운에 반영하지 않음)."""
    resolved = resolve_alert_sound_path(path)

    if sys.platform == "win32":
        if resolved and _play_custom_sound_windows(resolved):
            return True
        return _play_default_sound_windows()

    if sys.platform == "darwin":
        try:
            sound_path = (
                resolved
                if resolved
                else "/System/Library/Sounds/Glass.aiff"
            )
            p = subprocess.Popen(
                ["afplay", sound_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with _afplay_lock:
                _afplay_children.append(p)
                _afplay_children[:] = [
                    x for x in _afplay_children if x.poll() is None
                ]
            return True
        except Exception:
            print("\a", end="", flush=True)
            return True

    try:
        print("\a", end="", flush=True)
        return True
    except Exception:
        return False
