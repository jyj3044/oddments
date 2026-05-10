"""
GUI 시작 전·캡처 공통 진입점: OS 별 초기화, 창 캡처 가능 여부, 창 목록·캡처 팩토리.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

from app_platform.frame_source import BgrWindowCapture
from app_platform.models import WindowEntry

_MB_OK = 0x00000000
_MB_OKCANCEL = 0x00000001
_MB_ICONWARNING = 0x00000030
_MB_ICONERROR = 0x00000010
_IDOK = 1


def is_windows_elevated() -> bool:
    """Windows에서 UAC로 관리자 권한이 상승된 프로세스면 True. 비Windows는 검사 생략(True)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def windows_relaunch_as_admin(entry_script: str | Path | None = None) -> bool:
    """
    ShellExecuteW(runas)로 현재와 동일한 명령줄을 관리자 권한으로 다시 띄운다.
    성공 시 호출 측에서 sys.exit(0) 할 것. entry_script 는 비동결 시 실행할 .py 경로(보통 __file__).
    """
    if sys.platform != "win32":
        return False
    import ctypes
    import os

    if getattr(sys, "frozen", False):
        exe = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        exe = sys.executable
        if entry_script is not None:
            script = str(Path(entry_script).resolve())
        else:
            script = str(Path(sys.argv[0]).resolve())
        params = subprocess.list2cmdline([script, *sys.argv[1:]])

    cwd = os.getcwd()
    rc = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        exe,
        params if params else None,
        cwd,
        1,
    )
    try:
        return int(rc) > 32
    except (TypeError, ValueError):
        return False


def require_windows_admin_or_exit(entry_script: str | Path | None = None) -> None:
    """
    Windows에서 관리자가 아니면 안내 대화상자(확인/취소)를 띄운다.
    확인이면 UAC 후 재실행, 취소면 종료. 비Windows는 무시.
    """
    if sys.platform != "win32" or is_windows_elevated():
        return
    import ctypes

    title = "관리자 권한 필요"
    body = (
        "이 프로그램은 관리자 권한으로 실행해야 합니다.\n\n"
        "「확인」을 누르면 UAC 창이 열린 뒤 관리자 권한으로 다시 실행합니다.\n"
        "「취소」를 누르면 종료합니다."
    )
    try:
        answer = ctypes.windll.user32.MessageBoxW(
            0, body, title, _MB_OKCANCEL | _MB_ICONWARNING
        )
    except Exception:
        sys.exit(1)

    if answer != _IDOK:
        sys.exit(1)

    if windows_relaunch_as_admin(entry_script):
        sys.exit(0)

    err = (
        "관리자 권한으로 다시 실행하지 못했습니다.\n"
        "(UAC에서 거부했거나 오류가 났을 수 있습니다.)"
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, err, title, _MB_OK | _MB_ICONERROR)
    except Exception:
        pass
    sys.exit(1)


def window_pick_supported() -> bool:
    """창 선택·창 단위 캡처 UI/로직을 켤 수 있는지 (Windows·macOS)."""
    return sys.platform in ("win32", "darwin")


def ensure_pre_gui_init() -> None:
    """Tk() 전에 호출: DPI 등 OS 별 준비."""
    if sys.platform == "win32":
        from windows_capture import ensure_windows_dpi_awareness

        ensure_windows_dpi_awareness()


def enumerate_windows(min_width: int = 80, min_height: int = 80) -> List[WindowEntry]:
    if sys.platform == "win32":
        from windows_capture import enumerate_windows as _ew

        return _ew(min_width=min_width, min_height=min_height)
    if sys.platform == "darwin":
        from darwin_capture import enumerate_windows as _ew

        return _ew(min_width=min_width, min_height=min_height)
    return []


def make_window_capture(window_id: int) -> BgrWindowCapture:
    """
    WindowEntry.hwnd 와 동일한 식별자로 캡처 객체를 만듭니다.
    grab_bgr() · close() 를 제공합니다.
    """
    if sys.platform == "win32":
        from windows_capture import WindowCapture

        return WindowCapture(window_id)
    if sys.platform == "darwin":
        from darwin_capture import MacWindowCapture

        return MacWindowCapture(window_id)
    raise OSError("창 캡처는 이 운영체제에서 지원되지 않습니다.")
