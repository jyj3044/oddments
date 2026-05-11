"""Windows 전용: 원격 뷰어가 입력 포커스를 가질 때 Win/Ctrl/Alt 가 로컬 OS 로
전달되지 않도록 저수준 키보드 훅으로 가로채고, 동일 이벤트를 DataChannel 로 다시 보낸다.

추가로 ``WH_GETMESSAGE`` 훅으로 ``WM_CHAR``/``WM_UNICHAR`` 메시지를 가로채
한글 등 IME 가 합성한 유니코드 문자를 호스트로 전송한다 (raw 키 코드만으로는
Mac 호스트에서 한글 입력이 안 됨).

삼키면 Flutter 로는 해당 VK 가 전달되지 않으므로, 훅 안에서만 ``send_fn`` 을 호출한다.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Callable

WH_KEYBOARD_LL = 13
WH_GETMESSAGE = 3
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_CHAR = 0x0102
WM_SYSCHAR = 0x0106
WM_UNICHAR = 0x0109
WM_IME_CHAR = 0x0286
PM_NOREMOVE = 0x0000

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
_GETMESSAGE_PROC = ctypes.WINFUNCTYPE(
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


class _POINT(ctypes.Structure):
    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


class MSG(ctypes.Structure):
    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _POINT),
    )


class _Sink:
    __slots__ = (
        "hook",
        "msg_hook",
        "msg_thread_id",
        "thread",
        "stop",
        "proc_ref",
        "msg_proc_ref",
        "should_suppress",
        "send_key",
        "send_char",
        "window_title",
        "high_surrogate",
    )

    def __init__(self) -> None:
        self.hook: int = 0
        self.msg_hook: int = 0
        self.msg_thread_id: int = 0
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.proc_ref: object | None = None
        self.msg_proc_ref: object | None = None
        self.should_suppress: Callable[[], bool] = lambda: False
        self.send_key: Callable[[str, bool], None] = lambda _t, _d: None
        self.send_char: Callable[[str], None] = lambda _c: None
        self.window_title = ""
        self.high_surrogate: int = 0


_state = _Sink()


def _find_viewer_hwnd(window_title: str) -> int:
    """뷰어 최상위 HWND — 정확한 제목 실패 시 같은 프로세스·가시 창 제목 부분일치."""
    user32 = ctypes.windll.user32
    want = (window_title or "").strip()
    if not want:
        return 0
    hwnd = user32.FindWindowW(None, want)
    if hwnd:
        return int(hwnd)
    my_pid = int(os.getpid())
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(h: int, _lp: int) -> bool:
        if not user32.IsWindowVisible(h):
            return True
        cpid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(h, ctypes.byref(cpid))
        if int(cpid.value) != my_pid:
            return True
        buf = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(h, buf, 1024)
        got = buf.value.strip()
        if not got:
            return True
        a, b = want.lower(), got.lower()
        if a == b or a in b or b in a:
            found.append(int(h))
            return False
        return True

    user32.EnumWindows(_enum, 0)
    if found:
        return found[0]
    return 0


def _foreground_matches_title(title: str) -> bool:
    if not title:
        return False
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    if not fg:
        return False
    mine = user32.FindWindowW(None, str(title))
    return bool(mine and fg == mine)


# 진단용: hook 안에서 본 WM_* 메시지 카운트. 외부에서 polling 으로 읽어 로그.
_diag_seen_char = 0
_diag_seen_unichar = 0
_diag_seen_ime = 0
_diag_seen_syschar = 0
_diag_last_wp = 0


def _get_message_proc(
    n_code: int, w_param: wintypes.WPARAM, l_param: wintypes.LPARAM
) -> int:
    """WH_GETMESSAGE 훅: WM_CHAR/WM_SYSCHAR/WM_UNICHAR/WM_IME_CHAR 가로채기.

    ``should_suppress`` 가 True 일 때(원격 뷰어 키 캡처) 유니코드 문자를
    ``send_char`` 로 호스트에 전달. ASCII(WM_CHAR wp<0x80)는 KeyDown 경로와 중복되지
    않게 여기서는 제외.
    """
    global _diag_seen_char, _diag_seen_unichar, _diag_seen_ime, _diag_seen_syschar
    global _diag_last_wp
    if n_code == HC_ACTION:
        try:
            msg = ctypes.cast(l_param, ctypes.POINTER(MSG)).contents
            m = int(msg.message)
            wp = int(msg.wParam)
            is_char_msg = m in (WM_CHAR, WM_SYSCHAR, WM_UNICHAR, WM_IME_CHAR)
            if is_char_msg:
                # 디버그 카운터 — viewer focus 와 무관하게 hook 도달 여부 확인용.
                if m == WM_CHAR:
                    _diag_seen_char += 1
                elif m == WM_UNICHAR:
                    _diag_seen_unichar += 1
                elif m == WM_IME_CHAR:
                    _diag_seen_ime += 1
                elif m == WM_SYSCHAR:
                    _diag_seen_syschar += 1
                _diag_last_wp = wp
            if is_char_msg and _state.should_suppress():
                # 포커스 검사 생략: Flutter 뷰어에서 IME 가 보내는 WM_CHAR/WM_IME_CHAR 은
                # 훅이 걸린 UI 스레드 큐에서만 오며, FindWindowW 제목 불일치로
                # _foreground_matches_title 이 거짓이 되면 한글이 호스트에 안 간다.
                if m == WM_UNICHAR:
                    try:
                        if wp >= 0x80:
                            _state.send_char(chr(wp))
                    except (ValueError, OverflowError):
                        pass
                elif m in (WM_CHAR, WM_SYSCHAR, WM_IME_CHAR):
                    if 0xD800 <= wp <= 0xDBFF:
                        _state.high_surrogate = wp
                    elif 0xDC00 <= wp <= 0xDFFF:
                        hs = _state.high_surrogate
                        _state.high_surrogate = 0
                        if hs:
                            try:
                                cp = (
                                    0x10000
                                    + ((hs - 0xD800) << 10)
                                    + (wp - 0xDC00)
                                )
                                _state.send_char(chr(cp))
                            except (ValueError, OverflowError):
                                pass
                    else:
                        _state.high_surrogate = 0
                        if wp >= 0x80:
                            try:
                                _state.send_char(chr(wp))
                            except (ValueError, OverflowError):
                                pass
        except Exception:
            pass
    return int(
        ctypes.windll.user32.CallNextHookEx(
            _state.msg_hook, n_code, w_param, l_param
        )
    )


def get_diag_snapshot() -> dict:
    """진단용: hook 으로 본 WM_* 메시지 누적 카운트와 마지막 wParam.

    외부에서 주기적으로 호출하여 ``log_remote_event`` 등으로 기록한다.
    """
    return {
        "char": _diag_seen_char,
        "unichar": _diag_seen_unichar,
        "ime": _diag_seen_ime,
        "syschar": _diag_seen_syschar,
        "last_wp": _diag_last_wp,
        "msg_hook": _state.msg_hook,
        "msg_thread_id": _state.msg_thread_id,
        "ll_hook": _state.hook,
    }


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
    send_char: Callable[[str], None] | None = None,
) -> None:
    """별도 스레드에서 LL 훅 + 메시지 펌프를 돌린다.

    WH_GETMESSAGE 훅은 In-process 훅 — 같은 프로세스의 UI 스레드 메시지를
    가로챈다. 우리 메시지 펌프 스레드 자신을 ``thread_id`` 로 지정한다
    (Flutter UI 메시지가 이 hook 의 대상이 되려면 그 thread 에 hook 을 설치해야 하나,
    Python 프로세스 안에서 UI thread 가 별도로 돌 경우 thread id 가 다를 수 있다.
    여기서는 우리가 만든 메시지 펌프 thread 에서 캐치되는 메시지만 본다는 단점이 있으나,
    Flet/Flutter 자체가 별도 UI thread 를 갖지 않는 한 동일 thread).
    """
    stop_win_keyboard_sink()
    _state.window_title = window_title
    _state.should_suppress = should_suppress
    _state.send_key = send_key
    if send_char is not None:
        _state.send_char = send_char
    _state.high_surrogate = 0
    _state.stop.clear()
    _state.proc_ref = _LOWLEVEL_KEYBOARD_PROC(_low_level_proc)
    _state.msg_proc_ref = _GETMESSAGE_PROC(_get_message_proc)

    def _resolve_ui_tid() -> int:
        """뷰어 윈도우의 실제 UI thread id."""
        user32 = ctypes.windll.user32
        hwnd = _find_viewer_hwnd(window_title)
        if not hwnd:
            return 0
        pid = wintypes.DWORD(0)
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(tid or 0)

    def _run() -> None:
        user32 = ctypes.windll.user32
        hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            _state.proc_ref,
            None,
            0,
        )
        if not hook:
            try:
                from .remote_log import log_remote_event

                log_remote_event(
                    "클라이언트: WH_KEYBOARD_LL 설치 실패", error=True
                )
            except Exception:
                pass
            return
        _state.hook = hook

        # 뷰어 윈도우가 아직 생성되지 않았을 수 있으므로 잠시 polling.
        ui_tid = 0
        for _ in range(50):  # 50 * 100ms = 5s
            if _state.stop.is_set():
                break
            ui_tid = _resolve_ui_tid()
            if ui_tid > 0:
                break
            time.sleep(0.1)

        if ui_tid > 0:
            msg_hook = user32.SetWindowsHookExW(
                WH_GETMESSAGE,
                _state.msg_proc_ref,
                None,
                ui_tid,
            )
            _state.msg_hook = int(msg_hook or 0)
            _state.msg_thread_id = ui_tid
            try:
                from .remote_log import log_remote_event

                if _state.msg_hook:
                    log_remote_event(
                        f"클라이언트: WH_GETMESSAGE 설치 OK tid={ui_tid} "
                        f"hwnd={_find_viewer_hwnd(window_title)} title={window_title!r}"
                    )
                else:
                    err = ctypes.windll.kernel32.GetLastError()
                    log_remote_event(
                        f"클라이언트: WH_GETMESSAGE 설치 실패 tid={ui_tid} GLE={err}",
                        error=True,
                    )
            except Exception:
                pass
        else:
            try:
                from .remote_log import log_remote_event

                log_remote_event(
                    f"클라이언트: viewer HWND 미발견(title={window_title!r}) — "
                    f"WH_GETMESSAGE 미설치",
                    error=True,
                )
            except Exception:
                pass
        msg = MSG()
        PM_REMOVE = 0x0001
        while not _state.stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.004)
        if _state.msg_hook:
            user32.UnhookWindowsHookEx(_state.msg_hook)
            _state.msg_hook = 0
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
    _state.msg_proc_ref = None
