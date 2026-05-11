"""Windows 전용: 원격 뷰어가 입력 포커스를 가질 때 Win/Ctrl/Alt 가 로컬 OS 로
전달되지 않도록 저수준 키보드 훅으로 가로채고, 동일 이벤트를 DataChannel 로 다시 보낸다.

삼키면 Flutter 로는 해당 VK 가 전달되지 않으므로, 훅 안에서만 ``send_fn`` 을 호출한다.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_CAPITAL = 0x14

_SUPPRESS_VKS = frozenset(
    {
        VK_LWIN,
        VK_RWIN,
        VK_LCONTROL,
        VK_RCONTROL,
        VK_LMENU,
        VK_RMENU,
        VK_CAPITAL,
    }
)

_VK_TO_TOKEN = {
    VK_LWIN: "cmd",
    VK_RWIN: "cmd_r",
    VK_LCONTROL: "ctrl_l",
    VK_RCONTROL: "ctrl_r",
    VK_LMENU: "alt_l",
    VK_RMENU: "alt_r",
    VK_CAPITAL: "caps_lock",
}

# SetWindowsHookEx 는 호출마다 새로 만든 WINFUNCTYPE 과 호환되지 않는다.
# 프로토타입은 모듈 로드 시 한 번만 정의한다 (Python ctypes 문서 / Win32 LL 훅 관행).
_LOWLEVEL_KEYBOARD_PROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = (
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


class _Sink:
    __slots__ = (
        "hook",
        "thread",
        "stop",
        "proc_ref",
        "should_suppress",
        "send_key",
        "window_title",
    )

    def __init__(self) -> None:
        self.hook: int = 0
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.proc_ref: object | None = None
        self.should_suppress: Callable[[], bool] = lambda: False
        self.send_key: Callable[[str, bool], None] = lambda _t, _d: None
        self.window_title = ""


_state = _Sink()


def _foreground_matches_title(title: str) -> bool:
    if not title:
        return False
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    if not fg:
        return False
    mine = user32.FindWindowW(None, str(title))
    return bool(mine and fg == mine)


def _low_level_proc(n_code: int, w_param: wintypes.WPARAM, l_param: wintypes.LPARAM) -> int:
    if n_code == HC_ACTION:
        try:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            vk = int(kb.vkCode)
            if vk in _SUPPRESS_VKS and _state.should_suppress():
                if not _foreground_matches_title(_state.window_title):
                    return int(
                        ctypes.windll.user32.CallNextHookEx(
                            _state.hook, n_code, w_param, l_param
                        )
                    )
                wp = int(w_param)
                if wp in (
                    WM_KEYDOWN,
                    WM_KEYUP,
                    WM_SYSKEYDOWN,
                    WM_SYSKEYUP,
                ):
                    down = wp in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    tok = _VK_TO_TOKEN.get(vk)
                    if tok:
                        _state.send_key(tok, down)
                    return 1
        except Exception:
            pass
    return int(
        ctypes.windll.user32.CallNextHookEx(_state.hook, n_code, w_param, l_param)
    )


def start_win_keyboard_sink(
    *,
    window_title: str,
    should_suppress: Callable[[], bool],
    send_key: Callable[[str, bool], None],
) -> None:
    """별도 스레드에서 LL 훅 + 메시지 펌프를 돌린다."""
    if threading.current_thread() is not threading.main_thread():
        pass
    stop_win_keyboard_sink()
    _state.window_title = window_title
    _state.should_suppress = should_suppress
    _state.send_key = send_key
    _state.stop.clear()
    _state.proc_ref = _LOWLEVEL_KEYBOARD_PROC(_low_level_proc)

    def _run() -> None:
        user32 = ctypes.windll.user32
        # WH_KEYBOARD_LL + 전역 훅(dwThreadId==0) 일 때 hMod 는 NULL 이어야 한다 (MSDN).
        hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            _state.proc_ref,
            None,
            0,
        )
        if not hook:
            return
        _state.hook = hook
        msg = wintypes.MSG()
        PM_REMOVE = 0x0001
        while not _state.stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.004)
        user32.UnhookWindowsHookEx(hook)
        _state.hook = 0

    th = threading.Thread(target=_run, name="win-kbd-sink", daemon=True)
    _state.thread = th
    th.start()


def stop_win_keyboard_sink() -> None:
    _state.stop.set()
    if _state.thread is not None and _state.thread.is_alive():
        _state.thread.join(timeout=2.0)
    _state.thread = None
    _state.stop = threading.Event()
    _state.proc_ref = None
