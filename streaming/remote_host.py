"""원격 호스트: 단일 모니터 캡처 → WebRTC 송출 + DataChannel 로 입력 수신."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, TypeVar

_SealUiFn = Callable[[], None]
_SealUiRunner = Callable[[_SealUiFn], None]

import mss
import numpy as np
from aiohttp import web
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)

from capture.thread import CaptureThread, enumerate_monitors

from .pil_bgr import resize_bgr
from .remote_log import log_remote_diag, log_remote_event
from .remote_presets import normalize_preset_id, preset_dimensions
from .rtc_ice import rtc_configuration_from_stun_turn
from .web_stream import (
    SharedAudioBuffer,
    SharedAudioTrack,
    SharedVideoBuffer,
    SharedVideoTrack,
    _even_dims_bgr,
)

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="remote-input")

_T = TypeVar("_T")

# pynput 컨트롤러는 프로세스당 하나만 쓴다. 매 DataChannel 메시지마다 새로 만들면
# 특히 macOS Quartz 경로에서 호버 폭주 시 멈춤·지연을 유발하기 쉽다.
_pynput_singleton_lock = threading.Lock()
_mouse_controller: object | None = None
_kbd_controller: object | None = None
_move_dedup_lock = threading.Lock()
_last_move_pixel: list[tuple[int, int] | None] = [None]
_inject_failure_logged = False

# macOS 봉인 worker 가 CGEventTap 으로 사용자 하드웨어 키와 우리 합성 키를
# 구별하기 위한 magic 값. 호스트가 합성하는 이벤트의 kCGEventSourceUserData
# 필드에 이 값을 마킹하면, 봉인 worker tap 은 user_data == ODDM 인 이벤트는
# 통과시키고 나머지(=사용자 키)는 차단할 수 있다.
DARWIN_SYNTHETIC_USER_DATA: int = 0x4F44444D  # 'ODDM' (4 bytes ASCII)

# macOS virtual keycodes (HIToolbox/Events.h).
# 특수 토큰은 _DARWIN_TOKEN_VK, 알파·숫자·일부 부호는 US 물리 키(_darwin_us_layout_physical_key).
# 그 외 유니코드는 vk=0 + CGEventKeyboardSetUnicodeString.
# US QWERTY 물리 키 (HIToolbox kVK_ANSI_*). 한글 IME 는 유니코드 주입이 아니라
# 이 키코드로 보내야 2벌식처럼 r→ㄱ 이 된다.
_DARWIN_US_LETTER_VK_LOWER: dict[str, int] = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "o": 31,
    "u": 32,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "k": 40,
    "n": 45,
    "m": 46,
}
_DARWIN_US_DIGIT_VK: dict[str, int] = {
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "5": 23,
    "6": 22,
    "7": 26,
    "8": 28,
    "9": 25,
    "0": 29,
}
_DARWIN_US_PUNCT_VK: dict[str, tuple[int, bool]] = {
    ",": (43, False),
    ".": (47, False),
    "/": (44, False),
    ";": (41, False),
    "'": (39, False),
    "[": (33, False),
    "]": (30, False),
    "\\": (42, False),
    "-": (27, False),
    "=": (24, False),
    "`": (50, False),
}


def _darwin_us_layout_physical_key(ch: str) -> tuple[int, bool] | None:
    """한 글자에 대해 (가상 키코드, 해당 키 타이핑에 Shift 필요 여부). 없으면 None."""
    if len(ch) != 1:
        return None
    if ch == " ":
        return (49, False)
    if ch == "\t":
        return (48, False)
    if ch in "\n\r":
        return (36, False)
    if ch.islower() and ch in _DARWIN_US_LETTER_VK_LOWER:
        return (_DARWIN_US_LETTER_VK_LOWER[ch], False)
    if ch.isupper() and "A" <= ch <= "Z":
        lo = ch.lower()
        if lo in _DARWIN_US_LETTER_VK_LOWER:
            return (_DARWIN_US_LETTER_VK_LOWER[lo], True)
    if ch in _DARWIN_US_DIGIT_VK:
        return (_DARWIN_US_DIGIT_VK[ch], False)
    if ch in _DARWIN_US_PUNCT_VK:
        vk, sh = _DARWIN_US_PUNCT_VK[ch]
        return (vk, sh)
    return None


def _darwin_post_physical_key_tap(vk: int, extra_shift: bool) -> bool:
    """한 번의 키 다운+업 (수정자 마스크 + 선택적 Shift)."""
    if sys.platform != "darwin":
        return False
    try:
        import Quartz  # type: ignore[import-untyped]
    except Exception:
        return False
    try:
        with _darwin_synth_mod_lock:
            base = _darwin_synth_mod_mask
        shift_bit = int(Quartz.kCGEventFlagMaskShift)
        flags = base | (shift_bit if extra_shift else 0)
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, vk, down)
            if ev is None:
                return False
            try:
                Quartz.CGEventSetFlags(ev, flags)
            except Exception:
                pass
            _darwin_post_event(ev)
        return True
    except Exception as exc:
        _log_inject_failure_once(exc)
        return False


_DARWIN_TOKEN_VK: dict[str, int] = {
    "enter": 36, "return": 36,
    "tab": 48,
    "space": 49,
    " ": 49,
    "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53,
    "shift": 56, "shift_l": 56, "shift_r": 60,
    "cmd": 55, "cmd_r": 54, "meta": 55, "meta_l": 55, "meta_r": 54,
    "win": 55, "win_l": 55, "win_r": 54,
    "alt": 58, "alt_l": 58, "alt_r": 61, "option": 58,
    "ctrl": 59, "ctrl_l": 59, "ctrl_r": 62,
    "caps_lock": 57,
    "up": 126, "down": 125, "left": 123, "right": 124,
    "home": 115, "end": 119, "page_up": 116, "page_down": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

# macOS 합성 키: 시스템 단축키(⌃Space 한영 등)는 각 CGEvent 의 flag 필드에
# 현재 눌린 수정자가 반영되어야 인식된다. 클라이언트가 보내는 순서대로 mask 유지.
_darwin_synth_mod_lock = threading.Lock()
_darwin_synth_mod_mask: int = 0


def _darwin_reset_synth_modifiers() -> None:
    global _darwin_synth_mod_mask
    with _darwin_synth_mod_lock:
        _darwin_synth_mod_mask = 0


def _darwin_modifier_flag(quartz: object, tok_lower: str) -> int | None:
    """수정자 토큰이면 ``kCGEventFlagMask*`` 값. 아니면 None."""
    if tok_lower in ("shift", "shift_l", "shift_r"):
        return int(quartz.kCGEventFlagMaskShift)
    if tok_lower in ("ctrl", "ctrl_l", "ctrl_r", "control", "control_l", "control_r"):
        return int(quartz.kCGEventFlagMaskControl)
    if tok_lower in ("cmd", "cmd_r", "meta", "meta_l", "meta_r", "win", "win_l", "win_r"):
        return int(quartz.kCGEventFlagMaskCommand)
    if tok_lower in ("alt", "alt_l", "alt_r", "option", "option_l", "option_r"):
        return int(quartz.kCGEventFlagMaskAlternate)
    return None


def _darwin_post_event(ev: object) -> None:
    """합성 이벤트에 magic user_data 마킹 후 HID tap 위치로 post.

    Source 는 None (시스템 기본 결합 session state) 으로 만들어야 macOS 의 시스템
    단축키 디스패처(예: ⌃Space 한영 토글)와 IME 가 이를 정상 합성 키로 인식한다.
    Private source state 로 만들면 단축키·IME 단계에서 무시될 수 있다.
    """
    import Quartz  # type: ignore[import-untyped]

    try:
        Quartz.CGEventSetIntegerValueField(
            ev,
            Quartz.kCGEventSourceUserData,
            DARWIN_SYNTHETIC_USER_DATA,
        )
    except Exception:
        pass
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _darwin_press_token(token: str, down: bool) -> bool:
    """macOS 자체 키 합성. 특수 토큰·US 물리 키는 vk, 그 외 한 글자는 unicode.

    우리가 만든 모든 이벤트는 ``kCGEventSourceUserData = DARWIN_SYNTHETIC_USER_DATA``
    로 마킹된다 → 봉인 worker tap 에서 통과 식별 가능.
    Returns: 성공 시 True.
    """
    if sys.platform != "darwin":
        return False
    try:
        import Quartz  # type: ignore[import-untyped]
    except Exception:
        return False
    low = str(token).lower()
    if low == "space":
        low = " "
    vk = _DARWIN_TOKEN_VK.get(low)
    if vk is None and len(token) == 1 and token == " ":
        vk = 49
    extra_shift = False
    if vk is None and len(token) == 1:
        phys = _darwin_us_layout_physical_key(token)
        if phys is not None:
            vk, extra_shift = phys
    try:
        global _darwin_synth_mod_mask
        mbit = _darwin_modifier_flag(Quartz, low)
        with _darwin_synth_mod_lock:
            if mbit is not None:
                if down:
                    flags = _darwin_synth_mod_mask | mbit
                    _darwin_synth_mod_mask |= mbit
                else:
                    flags = _darwin_synth_mod_mask
                    _darwin_synth_mod_mask &= ~mbit
            else:
                flags = _darwin_synth_mod_mask
                if extra_shift:
                    flags |= int(Quartz.kCGEventFlagMaskShift)

            if vk is not None:
                ev = Quartz.CGEventCreateKeyboardEvent(None, vk, bool(down))
            elif token:
                ev = Quartz.CGEventCreateKeyboardEvent(None, 0, bool(down))
            else:
                return False
            if ev is None:
                return False
            try:
                Quartz.CGEventSetFlags(ev, flags)
            except Exception:
                pass
            if vk is None:
                Quartz.CGEventKeyboardSetUnicodeString(ev, len(token), token)
        _darwin_post_event(ev)
        return True
    except Exception as exc:
        _log_inject_failure_once(exc)
        return False


def _darwin_type_unicode(text: str) -> bool:
    """``text`` 를 한 글자씩 주입. US 레이아웃에 대응하는 ASCII 는 물리 키, 나머지는 unicode."""
    if sys.platform != "darwin" or not text:
        return False
    try:
        import Quartz  # type: ignore[import-untyped]
    except Exception:
        return False
    try:
        for ch in text:
            phys = _darwin_us_layout_physical_key(ch)
            if phys is not None:
                vk, ex = phys
                if not _darwin_post_physical_key_tap(vk, ex):
                    return False
                continue
            with _darwin_synth_mod_lock:
                flags = _darwin_synth_mod_mask
            ev_d = Quartz.CGEventCreateKeyboardEvent(None, 0, True)
            if ev_d is not None:
                try:
                    Quartz.CGEventSetFlags(ev_d, flags)
                except Exception:
                    pass
                Quartz.CGEventKeyboardSetUnicodeString(ev_d, len(ch), ch)
                _darwin_post_event(ev_d)
            ev_u = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
            if ev_u is not None:
                try:
                    Quartz.CGEventSetFlags(ev_u, flags)
                except Exception:
                    pass
                Quartz.CGEventKeyboardSetUnicodeString(ev_u, len(ch), ch)
                _darwin_post_event(ev_u)
        return True
    except Exception as exc:
        _log_inject_failure_once(exc)
        return False


def _pynput_mouse_keyboard() -> tuple[object, object]:
    global _mouse_controller, _kbd_controller
    with _pynput_singleton_lock:
        if _mouse_controller is None:
            from pynput.keyboard import Controller as KbdCtrl  # type: ignore[import-untyped]
            from pynput.mouse import Controller as MouseCtrl  # type: ignore[import-untyped]

            _mouse_controller = MouseCtrl()
            _kbd_controller = KbdCtrl()
        return _mouse_controller, _kbd_controller


def _log_inject_failure_once(exc: BaseException) -> None:
    global _inject_failure_logged
    if _inject_failure_logged:
        return
    _inject_failure_logged = True
    hint = ""
    if sys.platform == "darwin":
        hint = (
            " 시스템 설정 → 개인 정보 보호 및 보안 → 접근성에서 이 앱(또는 터미널/"
            "Python)을 허용했는지 확인하세요. 화면 녹화 권한도 원격 호스트에 필요합니다."
        )
    try:
        log_remote_event(
            f"원격 입력 주입 실패 ({type(exc).__name__}: {exc}).{hint}",
            error=True,
        )
    except Exception:
        pass


def _darwin_backing_scale() -> float:
    """메인 디스플레이 Retina 배율. mss 는 물리 픽셀, Quartz 마우스는 보통 논리 포인트."""
    if sys.platform != "darwin":
        return 1.0
    try:
        from AppKit import NSScreen  # type: ignore[import-untyped]

        scr = NSScreen.mainScreen()
        if scr is not None:
            return float(scr.backingScaleFactor())
    except Exception:
        pass
    return 1.0


def _darwin_backing_scale_for_geom(
    left: int, top: int, width: int, height: int,
) -> float:
    """캡처 모니터 영역 중심이 속한 NSScreen 의 backingScaleFactor.

    메인 화면만 보면 외장·비주류 모니터에서 Retina 배율이 틀어져 좌표가 밀린다.
    mss 좌표계와 NSScreen.frame 모두 글로벌 좌표(원점·축 동일)로 겹침 판별한다.
    """
    if sys.platform != "darwin":
        return 1.0
    try:
        from AppKit import NSScreen  # type: ignore[import-untyped]

        cx = float(left) + float(width) * 0.5
        cy = float(top) + float(height) * 0.5
        for scr in NSScreen.screens():
            r = scr.frame()
            ox = float(r.origin.x)
            oy = float(r.origin.y)
            rw = float(r.size.width)
            rh = float(r.size.height)
            if ox <= cx <= ox + rw and oy <= cy <= oy + rh:
                return float(scr.backingScaleFactor())
    except Exception:
        pass
    return _darwin_backing_scale()


def _resolve_pointer_scale(stored: float) -> float:
    """환경변수 MAPLE_REMOTE_POINTER_SCALE 로 호스트 배율 강제(실험·비표준 환경)."""
    raw = os.environ.get("MAPLE_REMOTE_POINTER_SCALE", "").strip()
    if not raw:
        return stored
    try:
        v = float(raw)
        return v if v > 0 else stored
    except ValueError:
        return stored


def default_rtc_configuration() -> RTCConfiguration:
    """STUN/TURN 없음 — 동일 LAN·포트포워딩 등 로컬 ICE 만 사용."""
    return RTCConfiguration(iceServers=[])


def _monitor_geometry(index: int) -> Optional[tuple[int, int, int, int]]:
    """(left, top, width, height) 또는 없음."""
    for m in enumerate_monitors():
        try:
            if int(m.get("index", -1)) == int(index):
                return (
                    int(m["left"]),
                    int(m["top"]),
                    int(m["width"]),
                    int(m["height"]),
                )
        except (TypeError, ValueError, KeyError):
            continue
    return None


def _darwin_main_pixel_size() -> tuple[int, int]:
    """메인 디스플레이 픽셀 크기 (가상 디스플레이 host_native 용).

    해상도 변경 작업은 ThreadPoolExecutor 등 **비 메인 스레드**에서 돌 수 있어
    AppKit ``NSScreen`` 은 쓰지 않고 Quartz 만 사용한다.
    """
    if sys.platform != "darwin":
        return 1920, 1080
    try:
        import Quartz  # type: ignore[import-untyped]

        mid = Quartz.CGMainDisplayID()
        w = int(Quartz.CGDisplayPixelsWide(mid))
        h = int(Quartz.CGDisplayPixelsHigh(mid))
        return max(320, w), max(240, h)
    except Exception:
        return 1920, 1080


def _mss_monitors_light() -> list[dict]:
    """mss 세션을 한 번만 열어 모니터 목록을 가져온다.

    ``enumerate_monitors()`` 는 호출마다 mss 컨텍스트를 새로 열어(Windows EDID 등 포함)
    가상 디스플레이 매칭 재시도 시 짧은 시간에 수십 번 호출되면 전체 프로세스가 멈춘
    것처럼 보일 수 있다(Flet UI ``Working…``).
    """
    out: list[dict] = []
    try:
        with mss.mss() as sct:
            for i, mon in enumerate(sct.monitors):
                if i == 0:
                    continue
                try:
                    out.append(
                        {
                            "index": int(i),
                            "left": int(mon.get("left", 0)),
                            "top": int(mon.get("top", 0)),
                            "width": int(mon.get("width", 0)),
                            "height": int(mon.get("height", 0)),
                            "name": None,
                        }
                    )
                except (TypeError, ValueError, KeyError):
                    continue
    except Exception:
        return []
    return out


def _mss_index_for_rect(
    left: int,
    top: int,
    width: int,
    height: int,
    *,
    tol: int = 16,
) -> Optional[int]:
    """mss 모니터 목록에서 좌표가 일치하는 인덱스."""
    for m in _mss_monitors_light():
        try:
            if (
                abs(int(m.get("left", 0)) - left) <= tol
                and abs(int(m.get("top", 0)) - top) <= tol
                and abs(int(m.get("width", 0)) - width) <= tol
                and abs(int(m.get("height", 0)) - height) <= tol
            ):
                return int(m["index"])
        except (TypeError, ValueError, KeyError):
            continue
    return None


def _prepare_frame(
    bgr: np.ndarray,
    *,
    capture_width: int,
    capture_height: int,
) -> np.ndarray:
    if bgr.size == 0:
        return bgr
    bgr = _even_dims_bgr(bgr)
    cw, ch = int(capture_width), int(capture_height)
    if cw > 0 and ch > 0:
        ew = max(2, cw - (cw % 2))
        eh = max(2, ch - (ch % 2))
        h, w = bgr.shape[:2]
        if (w, h) != (ew, eh):
            return resize_bgr(bgr, ew, eh)
    return bgr


def _norm_to_monitor_pixel_floats(
    nx: float,
    ny: float,
    rect: tuple[int, int, int, int],
) -> tuple[float, float]:
    """뷰어 정규 좌표(0..1, 송출 버퍼 기준) → mss 모니터 직사각형 좌표계 절대 위치.

    송출 해상도를 인코더에서 낮춰도 한 픽셀 열이 여전히 화면 가로 전체에 대응하므로,
    스팬은 반드시 ``rect`` 의 폭·높이(gw×gh)를 쓴다. ``frame_wh`` 로 스팬을 줄이면
    커서가 화면 안에서만 움직이는 것처럼 보인다.
    """
    left, top, gw, gh = rect
    ax_f = float(left) + float(nx) * float(gw)
    ay_f = float(top) + float(ny) * float(gh)
    return ax_f, ay_f


def _inject_input_message(
    raw: str,
    rect: tuple[int, int, int, int],
    *,
    pointer_scale: float = 1.0,
) -> None:
    """JSON 한 줄을 파싱해 마우스·키보드를 주입한다.

    pointer_scale: 맥 Retina 에서 mss/geom 이 물리 픽셀일 때 Quartz 포인터 좌표로 변환
    (보통 2.0 → 나눔).
    """
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(msg, dict):
        return
    t = str(msg.get("t", "")).lower()
    try:
        from pynput.keyboard import Key  # type: ignore[import-untyped]
        from pynput.mouse import Button  # type: ignore[import-untyped]
    except ImportError:
        return

    mouse, kbd = _pynput_mouse_keyboard()

    special = {
        "enter": Key.enter,
        "return": Key.enter,
        "space": Key.space,
        "tab": Key.tab,
        "esc": Key.esc,
        "escape": Key.esc,
        "backspace": Key.backspace,
        "delete": Key.delete,
        "shift": Key.shift,
        "shift_l": Key.shift_l,
        "shift_r": Key.shift_r,
        "ctrl": Key.ctrl,
        "ctrl_l": Key.ctrl_l,
        "ctrl_r": Key.ctrl_r,
        "alt": Key.alt,
        "alt_l": Key.alt_l,
        "alt_r": Key.alt_r,
        "cmd": Key.cmd,
        "cmd_r": Key.cmd_r,
        "meta": Key.cmd,
        "meta_l": Key.cmd,
        "meta_r": Key.cmd_r,
        "win": Key.cmd,
        "win_l": Key.cmd,
        "win_r": Key.cmd_r,
        "super_l": Key.cmd,
        "super_r": Key.cmd_r,
        "up": Key.up,
        "down": Key.down,
        "left": Key.left,
        "right": Key.right,
        "home": Key.home,
        "end": Key.end,
        "page_up": Key.page_up,
        "page_down": Key.page_down,
        "f1": Key.f1,
        "f2": Key.f2,
        "f3": Key.f3,
        "f4": Key.f4,
        "f5": Key.f5,
        "f6": Key.f6,
        "f7": Key.f7,
        "f8": Key.f8,
        "f9": Key.f9,
        "f10": Key.f10,
        "f11": Key.f11,
        "f12": Key.f12,
        # 맥 한영(캡스락 길게 누르기 등)은 합성 이벤트에서 항상 재현되지는 않음.
        "caps_lock": Key.caps_lock,
    }

    def _press_key(token: str, down: bool) -> None:
        tok = str(token)
        # macOS: 자체 합성으로 분기 (user_data 마킹 — 봉인 worker 키 차단과 통합).
        if sys.platform == "darwin":
            if _darwin_press_token(tok, bool(down)):
                return
            # 자체 합성 실패 시 pynput 폴백 — 합성 식별이 안 되어 호스트 키와
            # 함께 차단될 수 있으나 입력 자체는 동작한다.
        key_obj: object | None = None
        if len(tok) == 1:
            key_obj = tok
        else:
            key_obj = special.get(tok.lower())
        if key_obj is None:
            return
        try:
            if down:
                kbd.press(key_obj)
            else:
                kbd.release(key_obj)
        except Exception as exc:
            _log_inject_failure_once(exc)

    if t == "move":
        try:
            nx = float(msg.get("nx", 0.0))
            ny = float(msg.get("ny", 0.0))
        except (TypeError, ValueError):
            return
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        ax_f, ay_f = _norm_to_monitor_pixel_floats(nx, ny, rect)
        ps = max(float(_resolve_pointer_scale(pointer_scale)), 1e-6)
        if sys.platform == "darwin" and ps > 1.01:
            ax = int(ax_f / ps)
            ay = int(ay_f / ps)
        else:
            ax = int(ax_f)
            ay = int(ay_f)
        pix = (ax, ay)
        with _move_dedup_lock:
            if _last_move_pixel[0] == pix:
                return
            _last_move_pixel[0] = pix
        try:
            mouse.position = pix
        except Exception as exc:
            _log_inject_failure_once(exc)
        return

    if t == "btn":
        btn = str(msg.get("btn", "left")).lower()
        try:
            down = bool(msg.get("down", True))
        except Exception:
            down = True
        bmap = {
            "left": Button.left,
            "right": Button.right,
            "middle": Button.middle,
        }
        b = bmap.get(btn, Button.left)
        try:
            if down:
                mouse.press(b)
            else:
                mouse.release(b)
        except Exception as exc:
            _log_inject_failure_once(exc)
        return

    if t == "scroll":
        try:
            dx = int(msg.get("dx", 0))
            dy = int(msg.get("dy", 0))
        except (TypeError, ValueError):
            return
        try:
            mouse.scroll(dx, dy)
        except Exception as exc:
            _log_inject_failure_once(exc)
        return

    if t == "key":
        k = str(msg.get("k", ""))
        try:
            down = bool(msg.get("down", True))
        except Exception:
            down = True
        try:
            cps = [hex(ord(c)) for c in k]
            log_remote_event(
                f"호스트: 키 수신 k={k!r} cps={cps} down={down}"
            )
        except Exception:
            pass
        _press_key(k, down)
        return

    if t == "char":
        c = str(msg.get("c", ""))
        try:
            cps = [hex(ord(ch)) for ch in c]
            log_remote_event(f"호스트: 문자 수신 c={c!r} cps={cps}")
        except Exception:
            pass
        if not c:
            return
        if sys.platform == "darwin":
            _darwin_type_unicode(c)
        else:
            try:
                kbd.type(c)
            except Exception as exc:
                _log_inject_failure_once(exc)
        return


def _clipboard_read_text() -> str:
    try:
        import pyperclip

        return str(pyperclip.paste() or "")
    except Exception:
        return ""


def _clipboard_write_text(text: str) -> None:
    try:
        import pyperclip

        pyperclip.copy(text or "")
    except Exception:
        pass


def _host_warp_pointer_to_capture_center(srv: "RemoteHostServer") -> None:
    """가상 디스플레이 원격 시 커서를 캡처 화면 안으로 옮겨 키 입력이 메인 화면에 가지 않게 한다."""
    g = srv._geom
    if g is None:
        return
    try:
        raw = json.dumps({"t": "move", "nx": 0.5, "ny": 0.5})
        _inject_input_message(raw, g, pointer_scale=srv._pointer_scale)
    except Exception:
        pass


def _dispatch_dc_payload(
    srv: "RemoteHostServer",
    raw: str,
    geom: tuple[int, int, int, int],
    channel: object,
) -> None:
    """DataChannel 메시지: 클립보드 vs 입력 주입."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        _POOL.submit(
            _inject_input_message,
            raw,
            geom,
            pointer_scale=srv._pointer_scale,
        )
        return
    if not isinstance(msg, dict):
        _POOL.submit(
            _inject_input_message,
            raw,
            geom,
            pointer_scale=srv._pointer_scale,
        )
        return
    t = str(msg.get("t", "")).lower()
    if t == "clip_get":

        def _work() -> None:
            txt = _clipboard_read_text()
            payload = json.dumps({"t": "clip", "text": txt}, ensure_ascii=False)

            def _send() -> None:
                try:
                    channel.send(payload)
                except Exception:
                    pass

            loop = srv._loop
            if loop is not None:
                loop.call_soon_threadsafe(_send)

        _POOL.submit(_work)
        return
    if t == "clip_set":
        text = str(msg.get("text", ""))
        _POOL.submit(_clipboard_write_text, text)
        return
    _POOL.submit(
        _inject_input_message,
        raw,
        geom,
        pointer_scale=srv._pointer_scale,
    )


class RemoteHostServer:
    """HTTP `/offer` 신호 + 공유 비디오 버퍼 + 입력 DataChannel."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 49152,
        fps: float = 30.0,
        monitor_index: int = 1,
        auth_token: str = "",
        h264_hardware_encode: bool = False,
        virtual_display_enabled: bool = False,
        darwin_audio_device: str = "",
        seal_ui_runner: Optional[_SealUiRunner] = None,
    ) -> None:
        self.host = str(host).strip() or "0.0.0.0"
        self.port = int(port)
        self.fps = float(max(5.0, min(60.0, fps)))
        self.monitor_index = int(monitor_index)
        self._capture_monitor_index = int(monitor_index)
        # 송출 픽셀 크기는 캡처(클라이언트 요청 가상 해상도 등)에 맡김. 별도 축소 없음.
        self.capture_width = 0
        self.capture_height = 0
        self._rtc_configuration = default_rtc_configuration()
        self._auth_token = (auth_token or "").strip()
        self._h264_hardware_encode = bool(h264_hardware_encode)
        self._h264_patch_applied = False

        self._virtual_display_enabled = bool(virtual_display_enabled)
        # 클라이언트 /offer 의 preset 으로 갱신. 초기값은 host_native.
        self._resolution_preset_id = "host_native"
        self._darwin_audio_device = (darwin_audio_device or "").strip()
        self._seal_ui_runner: Optional[_SealUiRunner] = seal_ui_runner
        self._vd_obj: object | None = None
        self._vd_display_id: int = 0
        # 봉인 동안 물리 화면에 등장한 일반 앱 창을 VD 로 끌어오는 polling 스레드.
        self._corral_thread: threading.Thread | None = None
        self._corral_stop_event: threading.Event | None = None
        # 봉인 동안 호스트 사용자의 하드웨어 키만 차단하는 CGEventTap 스레드.
        self._keyblock_thread: threading.Thread | None = None
        self._keyblock_state: dict | None = None
        # 가상 디스플레이 PyObjC 객체는 워커·호스트 루프에서 동시에 건드리면 이중 release 로 SIGSEGV 가 난다.
        self._vd_session_lock = threading.Lock()
        # vd 해제 시 워커 스택/클로저가 PyObjC 래퍼를 붙잡으면 GC 가 idle 워커에서 object_dealloc 하며 SIGSEGV 난다.
        # 참조는 여기만 두고 해제·del 은 호스트 aiohttp 스레드 메서드에서만 한다.
        # 메인 루프에서 vd.release() 가 끝나기 전에 새 CGVirtualDisplay 를 만들면 초기화 실패가 난다.
        self._vd_release_complete_event = threading.Event()
        self._vd_release_complete_event.set()

        # _startup_async 성공·실패를 start() 에 알린다.
        # 성공 시 set(), 실패(OSError 등) 시 _startup_error 에 예외를 담고 set().
        self._startup_done = threading.Event()
        self._startup_error: Exception | None = None

        self.video = SharedVideoBuffer()
        self.audio = SharedAudioBuffer()
        self._audio_stop = threading.Event()
        self._audio_thread: threading.Thread | None = None
        self._audio_logged_fail = False

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pcs: set[RTCPeerConnection] = set()
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None
        # 피어 끊김 시 idle 정리(executor + finalize) 겹침 방지 — asyncio.Lock 은 루프 기동 후 설정.
        self._peer_disconnect_lock: asyncio.Lock | None = None

        self._capture: CaptureThread | None = None
        self._capture_lock = threading.Lock()
        self._geom: Optional[tuple[int, int, int, int]] = None
        self._meta_pending: set[object] = set()
        # 맥 Retina: mss/monitor geom 은 물리 픽셀 → pynput 는 논리 포인트일 때 나눔.
        self._pointer_scale: float = 1.0
        # _prepare_frame·짝수 맞춤 후 송출 버퍼 (뷰어 정규화와 주입 스팬 일치)
        self._last_frame_wh: tuple[int, int] = (0, 0)
        # /offer 용: 피어 종료 정리(최대 ~15s VD 해제 대기)와 같은 풀을 쓰면
        # 빠르게 들어갔다 나갔다 할 때 워커가 고갈되어 ``/offer 수신`` 만 찍히고 멈춘다.
        self._host_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="odd-remote-offer",
        )
        self._cleanup_executor = ThreadPoolExecutor(
            max_workers=3,
            thread_name_prefix="odd-remote-idle",
        )

    def _request_disconnect_from_seal(self) -> None:
        """물리 화면 오버레이 '세션 끊기' — AppKit 메인 스레드에서 호출될 수 있음."""
        loop = self._loop
        if loop is None:
            return

        async def _close_all() -> None:
            for pc in list(self._pcs):
                try:
                    await pc.close()
                except Exception:
                    pass

        try:
            log_remote_event("호스트: 물리 화면 봉인에서 세션 종료 요청")
        except Exception:
            pass
        try:
            asyncio.run_coroutine_threadsafe(_close_all(), loop)
        except Exception:
            pass

    def _try_start_physical_seal(self) -> None:
        """NSScreen 목록이 갱신된 뒤 봉인하도록 약간 지연한다."""
        if sys.platform != "darwin" or not self._virtual_display_enabled:
            return
        if int(self._vd_display_id) <= 0 or len(self._pcs) == 0:
            return
        loop = self._loop
        if loop is None:
            return
        vid = int(self._vd_display_id)
        cb = self._request_disconnect_from_seal
        try:
            log_remote_event(
                f"호스트: 물리 화면 봉인 예약 (vid={vid}, 0.48s 후)"
            )
        except Exception:
            pass

        def _fire() -> None:
            try:
                _darwin_reset_synth_modifiers()
            except Exception:
                pass
            try:
                from app_platform.darwin_remote_seal import schedule_seal_show

                schedule_seal_show(vid, cb)
            except Exception as exc:
                try:
                    log_remote_event(
                        f"호스트: 물리 화면 봉인 subprocess 시작 실패 — {exc}",
                        error=True,
                    )
                except Exception:
                    pass
            # 봉인 후 창 코랄(corral) 스레드 시작: 0.3s 간격 polling 으로
            # 물리 화면에 등장한 일반 앱 창을 VD 로 이동시킨다.
            self._start_window_corral_thread(vid)
            # 호스트 사용자의 하드웨어 키 입력을 차단한다 (마우스/트랙패드는 통과).
            self._start_key_block_thread()

        try:
            loop.call_later(0.48, _fire)
        except Exception:
            _fire()

    def _start_key_block_thread(self) -> None:
        """봉인 동안 호스트 하드웨어 키만 차단하는 CGEventTap 스레드.

        합성 이벤트는 ``kCGEventSourceUserData == DARWIN_SYNTHETIC_USER_DATA``
        로 마킹되어 통과. 마우스/트랙패드 이벤트는 mask 에 포함하지 않아 통과.
        호스트 메인 프로세스가 접근성 권한을 가지므로 worker 가 아닌 여기서
        설치한다. 별도 스레드의 CFRunLoop 에서 callback 이 동작한다.
        """
        if sys.platform != "darwin":
            return
        if getattr(self, "_keyblock_thread", None) is not None:
            return
        ready_ev = threading.Event()
        state: dict = {"tap": None, "src": None, "runloop": None, "ok": False}

        def _run() -> None:
            try:
                import Quartz  # type: ignore[import-untyped]
                from CoreFoundation import (  # type: ignore[import-untyped]
                    CFMachPortCreateRunLoopSource,
                    CFRunLoopAddSource,
                    CFRunLoopGetCurrent,
                    CFRunLoopRun,
                    kCFRunLoopCommonModes,
                )

                def _cb(_proxy, type_, event, _refcon):  # noqa: ANN001
                    try:
                        # tap 자체가 사용자 입력으로 인해 비활성화될 수 있음 → 재활성.
                        if int(type_) == int(
                            Quartz.kCGEventTapDisabledByTimeout
                        ) or int(type_) == int(
                            Quartz.kCGEventTapDisabledByUserInput
                        ):
                            try:
                                if state["tap"] is not None:
                                    Quartz.CGEventTapEnable(state["tap"], True)
                            except Exception:
                                pass
                            return event
                        ud = Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGEventSourceUserData
                        )
                        if int(ud) == DARWIN_SYNTHETIC_USER_DATA:
                            return event
                        return None  # 호스트 사용자의 키 차단
                    except Exception:
                        return event

                mask = (
                    (1 << Quartz.kCGEventKeyDown)
                    | (1 << Quartz.kCGEventKeyUp)
                    | (1 << Quartz.kCGEventFlagsChanged)
                )
                tap = Quartz.CGEventTapCreate(
                    Quartz.kCGHIDEventTap,
                    Quartz.kCGHeadInsertEventTap,
                    Quartz.kCGEventTapOptionDefault,
                    mask,
                    _cb,
                    None,
                )
                if tap is None:
                    try:
                        log_remote_event(
                            "호스트: 키 차단 tap 생성 실패 (접근성 권한 필요)",
                            error=True,
                        )
                    except Exception:
                        pass
                    return
                src = CFMachPortCreateRunLoopSource(None, tap, 0)
                rl = CFRunLoopGetCurrent()
                CFRunLoopAddSource(rl, src, kCFRunLoopCommonModes)
                Quartz.CGEventTapEnable(tap, True)
                state["tap"] = tap
                state["src"] = src
                state["runloop"] = rl
                state["ok"] = True
                try:
                    log_remote_event("호스트: 키 차단 tap 설치됨 (하드웨어 키만 차단)")
                except Exception:
                    pass
                ready_ev.set()
                # 봉인 해제 시 _stop_key_block_thread 가 CFRunLoopStop 으로 종료.
                CFRunLoopRun()
            except Exception as exc:
                try:
                    log_remote_event(
                        f"호스트: 키 차단 tap 스레드 예외 — {type(exc).__name__}: {exc}",
                        error=True,
                    )
                except Exception:
                    pass
            finally:
                ready_ev.set()

        t = threading.Thread(target=_run, name="vd-keyblock", daemon=True)
        self._keyblock_thread = t
        self._keyblock_state = state
        t.start()
        ready_ev.wait(timeout=2.0)

    def _stop_key_block_thread(self) -> None:
        st = getattr(self, "_keyblock_state", None)
        if st is None:
            return
        try:
            import Quartz  # type: ignore[import-untyped]
            from CoreFoundation import CFRunLoopStop  # type: ignore[import-untyped]

            tap = st.get("tap")
            if tap is not None:
                try:
                    Quartz.CGEventTapEnable(tap, False)
                except Exception:
                    pass
            rl = st.get("runloop")
            if rl is not None:
                try:
                    CFRunLoopStop(rl)
                except Exception:
                    pass
        except Exception:
            pass
        t = getattr(self, "_keyblock_thread", None)
        if t is not None:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
        self._keyblock_thread = None
        self._keyblock_state = None

    def _start_window_corral_thread(self, vid: int) -> None:
        """원격 세션 동안 0.3s 간격으로 물리 화면에 등장한 창을 VD 로 이동시킨다.

        VD 주 디스플레이 전환을 하지 않는 대신 이 polling 으로 새 창과
        활성화되는 기존 창을 자동으로 VD 쪽으로 끌어온다.
        """
        if sys.platform != "darwin":
            return
        if getattr(self, "_corral_thread", None) is not None:
            return
        stop_ev = threading.Event()
        self._corral_stop_event = stop_ev
        my_pid = os.getpid()

        def _run() -> None:
            try:
                from app_platform.darwin_virtual_display import move_windows_to_display
            except Exception:
                return
            total_moved = 0
            iters = 0
            while not stop_ev.is_set() and self._pcs and self._vd_display_id == vid:
                try:
                    moved = move_windows_to_display(vid, exclude_pid=my_pid)
                    total_moved += int(moved or 0)
                except Exception:
                    pass
                iters += 1
                # 60 회(약 18s)마다 1회 요약 로그
                if iters % 60 == 0:
                    try:
                        log_remote_event(
                            f"호스트: 창 코랄 polling 누적 — iters={iters} moved={total_moved}"
                        )
                    except Exception:
                        pass
                stop_ev.wait(0.3)

        t = threading.Thread(target=_run, name="vd-window-corral", daemon=True)
        self._corral_thread = t
        t.start()

    def _stop_window_corral_thread(self) -> None:
        ev = getattr(self, "_corral_stop_event", None)
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
        t = getattr(self, "_corral_thread", None)
        if t is not None:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
        self._corral_thread = None
        self._corral_stop_event = None

    def _try_stop_physical_seal(self) -> None:
        if sys.platform != "darwin":
            return
        self._stop_window_corral_thread()
        self._stop_key_block_thread()
        try:
            _darwin_reset_synth_modifiers()
        except Exception:
            pass
        try:
            log_remote_diag("호스트: 물리 화면 봉인 해제 — schedule_seal_hide 예약")
        except Exception:
            pass
        try:
            from app_platform.darwin_remote_seal import schedule_seal_hide

            schedule_seal_hide(ui_runner=self._seal_ui_runner)
        except Exception as exc:
            try:
                log_remote_diag(
                    f"호스트: schedule_seal_hide 실패 — {type(exc).__name__}: {exc}",
                    error=True,
                )
            except Exception:
                pass

    def _auth_accept(self, token_param: object | None) -> bool:
        exp = self._auth_token
        if not exp:
            return True
        got = token_param if isinstance(token_param, str) else ""
        got = got.strip()
        if len(exp) != len(got):
            return False
        try:
            return hmac.compare_digest(
                exp.encode("utf-8"),
                got.encode("utf-8"),
            )
        except Exception:
            return False

    def _invoke_on_host_loop(self, fn: Callable[[], _T], *, timeout: float = 120.0) -> _T:
        """호스트 aiohttp 이벤트 루프 스레드에서만 실행해야 하는 작업용.

        CGVirtualDisplay 생성 등은 이 스레드에서 직렬화한다. 해제는
        ``release_virtual_display_on_main_thread`` 로 메인 큐에서 처리한다.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return fn()
        host_th = self._thread
        if host_th is not None and threading.current_thread() is host_th:
            return fn()
        fut: concurrent.futures.Future[_T] = concurrent.futures.Future()

        def _wrapper() -> None:
            try:
                fut.set_result(fn())
            except BaseException as exc:
                fut.set_exception(exc)

        loop.call_soon_threadsafe(_wrapper)
        return fut.result(timeout=timeout)


    async def _release_virtual_display_session_async(self) -> bool:
        """가상 디스플레이 해제(피어 idle 정리 전용, asyncio 호스트 루프에서만 호출).

        PyObjC 는 alloc+init 반환값을 내부적으로 retain 하므로 명시적 vd.release() 를
        호출하면 retain count 가 2→1 이 될 뿐 dealloc 이 실행되지 않는다. 게다가
        ``Oddments-RemoteHost`` 스레드에서 로컬 변수 ``vd`` 가 스코프를 벗어나면 PyObjC 가
        한 번 더 [vd release] 를 호출해 SIGSEGV 가 난다(더블 릴리스).
        모든 Python 참조를 해제하고 gc.collect() 를 호출해 PyObjC 가 [vd release] 를
        단 한 번 자동 호출하도록 한다 → retain count 0 → dealloc → WindowServer 등록 해제.
        """
        if sys.platform != "darwin" or not self._virtual_display_enabled:
            return True
        with self._vd_session_lock:
            vd = self._vd_obj
            if vd is None:
                try:
                    log_remote_diag("호스트: VD 세션 해제(async) — 해제할 객체 없음(스킵)")
                except Exception:
                    pass
                return True
            old_id = int(self._vd_display_id)
            self._vd_obj = None
            self._vd_display_id = 0

        self._vd_release_complete_event.clear()
        try:
            import gc as _gc

            from app_platform.darwin_virtual_display import cg_display_id_still_online

            try:
                log_remote_diag(
                    f"호스트: VD 세션 해제(async) — Py 참조 해제 CG ID {old_id}"
                )
            except Exception:
                pass
            vd = None
            _gc.collect()
            try:
                log_remote_diag("호스트: VD 세션 해제(async) — Py 참조 해제·gc 완료")
            except Exception:
                pass

            if cg_display_id_still_online(old_id) is True:
                for _ in range(30):
                    await asyncio.sleep(0.1)
                    if cg_display_id_still_online(old_id) is not True:
                        break
            try:
                log_remote_diag(
                    f"호스트: VD 세션 해제(async) — 완료 "
                    f"cg_id={old_id} "
                    f"online={cg_display_id_still_online(old_id)!r}"
                )
            except Exception:
                pass
            return True
        except Exception as exc:
            try:
                log_remote_diag(
                    f"호스트: VD 세션 해제(async) 예외 — {type(exc).__name__}: {exc}",
                    error=True,
                )
            except Exception:
                pass
            return False
        finally:
            vd = None  # noqa: F841 — drop any remaining reference
            try:
                self._vd_release_complete_event.set()
            except Exception:
                pass

    async def _idle_finalize_darwin_vd_on_host_loop(self) -> None:
        """darwin+VD: executor 봉인 이후 geom·메타까지 마친다."""
        await self._release_virtual_display_session_async()
        self._geom = None
        try:
            log_remote_diag("호스트: idle finalize — self._geom = None 적용 (async VD 경로)")
        except Exception:
            pass
        try:
            n_meta = len(self._meta_pending)
            self._meta_pending.clear()
            try:
                log_remote_diag(
                    f"호스트: idle finalize — DataChannel 메타 대기 목록 비움 "
                    f"({n_meta}개 제거)"
                )
            except Exception:
                pass
        except Exception:
            pass

    def _release_virtual_display_session(self) -> None:
        """가상 디스플레이 세션을 한 번만 해제한다.

        ``_vd_obj`` 참조는 ``_vd_session_lock`` 으로 한 번만 떼어낸다.
        명시적 vd.release() 대신 Python 참조를 모두 해제하고 gc.collect() 로 PyObjC 의
        자동 [vd release] 를 유도한다(더블 릴리스로 인한 SIGSEGV 방지).
        """
        with self._vd_session_lock:
            vd = self._vd_obj
            if vd is None:
                try:
                    log_remote_diag("호스트: VD 세션 해제 — 해제할 객체 없음(스킵)")
                except Exception:
                    pass
                return
            old_id = int(self._vd_display_id)
            self._vd_obj = None
            self._vd_display_id = 0

        import gc as _gc

        try:
            log_remote_diag(
                f"호스트: VD 세션 해제 — Py 참조 해제 CG ID {old_id}"
            )
        except Exception:
            pass
        self._vd_release_complete_event.clear()
        try:
            vd = None
            _gc.collect()
            try:
                log_remote_diag("호스트: VD 세션 해제 — Py 참조 해제·gc 완료")
            except Exception:
                pass
        finally:
            vd = None  # noqa: F841
            try:
                self._vd_release_complete_event.set()
            except Exception:
                pass

    def _capture_join_timeout_sec(self) -> float:
        """가상 디스플레이는 mss 스레드가 끝나기 전에 CGDisplay 를 닫으면 오류가 나기 쉬워 여유를 둔다."""
        if sys.platform == "darwin" and self._virtual_display_enabled:
            return 20.0
        return 2.0

    def _join_capture_thread(self, cap: CaptureThread | None) -> None:
        if cap is None:
            return
        try:
            cap.join(timeout=self._capture_join_timeout_sec())
        except Exception:
            pass

    def _ensure_geom_virtual_display(self) -> None:
        from app_platform.darwin_virtual_display import (
            DarwinVirtualDisplayError,
            cg_display_bounds,
            create_virtual_display,
        )

        requested = normalize_preset_id(
            self._resolution_preset_id,
            fallback="host_native",
        )
        chain = [requested]
        if requested != "host_native":
            chain.append("host_native")

        last_err: BaseException | None = None
        for attempt, pid in enumerate(chain):
            self._resolution_preset_id = normalize_preset_id(
                pid, fallback="host_native"
            )
            w, h = preset_dimensions(
                self._resolution_preset_id,
                host_native=_darwin_main_pixel_size,
            )
            w = max(320, int(w))
            h = max(240, int(h))

            # Event.wait 는 호스트 aiohttp 스레드에서 실행되면 안 된다. 루프가 멈추면 피어 종료 후
            # ``_stop_capture_if_idle_finalize`` 가 실행되지 않아 VD 해제·completion set 과 교착하고,
            # 원격 재연결 시 /offer 가 영구히 막히며 호스트 중지도 되지 않는다.
            if self._thread is not None and threading.current_thread() is self._thread:
                raise RuntimeError(
                    "내부 오류: 가상 디스플레이 준비는 호스트 asyncio 스레드에서 호출할 수 없습니다."
                )
            if not self._vd_release_complete_event.wait(timeout=60.0):
                raise DarwinVirtualDisplayError(
                    "이전 가상 디스플레이 해제 대기 시간 초과"
                )

            def _create_vd(
                ww: int = w,
                hh: int = h,
            ) -> tuple[float, float, float, float]:
                vd, did = create_virtual_display(ww, hh, refresh_hz=60.0)
                with self._vd_session_lock:
                    self._vd_obj = vd
                    self._vd_display_id = int(did)
                return cg_display_bounds(int(did))

            try:
                if self._loop is not None:
                    bx, by, bw, bh = self._invoke_on_host_loop(_create_vd)
                else:
                    bx, by, bw, bh = _create_vd()
            except DarwinVirtualDisplayError as exc:
                last_err = exc
                if attempt == 0 and requested != "host_native":
                    try:
                        log_remote_event(
                            f"호스트: 요청 해상도({requested}) 가상 디스플레이 생성 실패 — "
                            f"호스트 해상도(host_native)로 재시도합니다. ({exc})"
                        )
                    except Exception:
                        pass
                continue

            left = int(round(bx))
            top = int(round(by))
            gw = max(1, int(round(bw)))
            gh = max(1, int(round(bh)))
            time.sleep(0.22)
            idx: int | None = None
            for mtry in range(10):
                idx = _mss_index_for_rect(left, top, gw, gh)
                if idx is not None:
                    break
                if mtry < 9:
                    time.sleep(0.14)
            if idx is None:
                self._release_virtual_display_session()
                err = RuntimeError(
                    "가상 디스플레이가 생성되었으나 mss 모니터 목록과 매칭되지 않았습니다."
                )
                last_err = err
                if attempt == 0 and requested != "host_native":
                    try:
                        log_remote_event(
                            "호스트: 요청 해상도 mss 매칭 실패 — host_native 로 재시도합니다.",
                            error=True,
                        )
                    except Exception:
                        pass
                    continue
                raise err

            self._capture_monitor_index = idx
            self._geom = (left, top, gw, gh)
            self._pointer_scale = 1.0
            try:
                did = int(self._vd_display_id)
                log_remote_event(
                    f"호스트: 가상 디스플레이 {gw}×{gh} (mss #{idx}, CG ID {did}, "
                    f"프리셋 «{self._resolution_preset_id}»)"
                )
            except Exception:
                pass

            return

        raise RuntimeError(
            str(last_err) if last_err else "가상 디스플레이를 만들 수 없습니다."
        ) from last_err

    def _ensure_geom(self) -> None:
        if self._geom is not None:
            return
        if sys.platform == "darwin" and self._virtual_display_enabled:
            self._ensure_geom_virtual_display()
            return
        geom = _monitor_geometry(self.monitor_index)
        if geom is None:
            raise RuntimeError(
                f"모니터 #{self.monitor_index} 을(를) 찾을 수 없습니다."
            )
        self._geom = geom
        self._capture_monitor_index = int(self.monitor_index)

    def _darwin_audio_worker(self) -> None:
        try:
            import soundcard as sc  # type: ignore[import-untyped]
        except ImportError:
            if not self._audio_logged_fail:
                self._audio_logged_fail = True
                try:
                    log_remote_event(
                        "호스트: soundcard 모듈 없음 — 원격 오디오 생략",
                        error=True,
                    )
                except Exception:
                    pass
            return
        mic = None
        needle = self._darwin_audio_device.lower()
        try:
            # 맥: soundcard 가 loopback=True 일 때 경고·불안정 CoreAudio 경로를 탄다.
            all_m = list(
                sc.all_microphones(include_loopback=(sys.platform != "darwin"))
            )
        except Exception as exc:
            if not self._audio_logged_fail:
                self._audio_logged_fail = True
                try:
                    log_remote_event(f"호스트: 마이크 열거 실패 — {exc}", error=True)
                except Exception:
                    pass
            return
        if needle:
            for m in all_m:
                if needle in str(getattr(m, "name", "")).lower():
                    mic = m
                    break
        if mic is None:
            for m in all_m:
                n = str(getattr(m, "name", "")).lower()
                if "blackhole" in n:
                    mic = m
                    break
        if mic is None:
            if not self._audio_logged_fail:
                self._audio_logged_fail = True
                try:
                    log_remote_event(
                        "호스트: BlackHole 등 가상 입력 장치를 찾지 못했습니다. "
                        "시스템 오디오를 원격으로 보내려면 BlackHole 2ch 설치 후 "
                        "멀티 출력 장치로 라우팅하세요.",
                        error=True,
                    )
                except Exception:
                    pass
            return
        try:
            try:
                log_remote_event(f"호스트: 원격 오디오 입력 «{mic.name}»")
            except Exception:
                pass
            with mic.recorder(samplerate=48000, channels=2) as rec:
                while not self._audio_stop.is_set():
                    buf = rec.record(numframes=960)
                    if buf is not None and getattr(buf, "size", 0):
                        self.audio.publish(buf)
        except Exception as exc:
            try:
                log_remote_event(f"호스트: 오디오 캡처 중단 — {exc}", error=True)
            except Exception:
                pass

    def _start_darwin_audio(self) -> None:
        if sys.platform != "darwin":
            return
        if self._audio_thread is not None and self._audio_thread.is_alive():
            return
        self._audio_stop.clear()
        self._audio_thread = threading.Thread(
            target=self._darwin_audio_worker,
            daemon=True,
            name="Oddments-RemoteAudio",
        )
        self._audio_thread.start()

    def _stop_darwin_audio(self) -> None:
        self._audio_stop.set()
        if self._audio_thread is not None and self._audio_thread.is_alive():
            try:
                self._audio_thread.join(timeout=2.0)
            except Exception:
                pass
        self._audio_thread = None

    def _push_frame(self, frame: np.ndarray) -> None:
        try:
            # CGVirtualDisplay(hiDPI=0) + mss 는 보통 1:1 픽셀. 여기서 Retina 보정을 켜면
            # 마우스 좌표가 밀려 물리 모니터로 주입되는 경우가 있다.
            _vd_active = (
                sys.platform == "darwin"
                and self._virtual_display_enabled
                and self._vd_display_id > 0
            )
            if (
                sys.platform == "darwin"
                and self._geom is not None
                and self._pointer_scale <= 1.01
                and not _vd_active
            ):
                try:
                    rh, rw = int(frame.shape[0]), int(frame.shape[1])
                    _gl, _gt, gw, gh = self._geom
                    if abs(rw - gw) <= 4 and abs(rh - gh) <= 4:
                        s = _darwin_backing_scale_for_geom(_gl, _gt, gw, gh)
                        if s > 1.01:
                            self._pointer_scale = s
                except Exception:
                    pass
            out = _prepare_frame(
                frame,
                capture_width=self.capture_width,
                capture_height=self.capture_height,
            )
            try:
                oh, ow = out.shape[:2]
                self._last_frame_wh = (int(ow), int(oh))
            except (TypeError, ValueError, IndexError):
                pass
            self.video.push(out)
            if self._meta_pending and self._loop is not None:
                h, w = out.shape[:2]
                mon_w = mon_h = 0
                if self._geom is not None:
                    mon_w, mon_h = int(self._geom[2]), int(self._geom[3])
                payload = json.dumps(
                    {
                        "t": "meta",
                        "stream_w": int(w),
                        "stream_h": int(h),
                        "mon_w": mon_w,
                        "mon_h": mon_h,
                        "virtual_display": bool(
                            self._virtual_display_enabled
                            and self._vd_display_id > 0
                        ),
                        "preset": self._resolution_preset_id,
                    },
                    ensure_ascii=False,
                )
                loop = self._loop
                for ch in list(self._meta_pending):

                    def _send_meta(c: object = ch, p: str = payload) -> None:
                        try:
                            c.send(p)
                        except Exception:
                            pass

                    try:
                        loop.call_soon_threadsafe(_send_meta)
                    except Exception:
                        pass
                self._meta_pending.clear()
        except Exception:
            pass

    def _ensure_capture(self) -> None:
        """인증된 클라이언트가 붙은 뒤에만 화면 캡처·인코더 패치를 적용한다."""
        self._ensure_geom()
        if not self._h264_patch_applied:
            self._h264_patch_applied = True
            try:
                from streaming.remote_h264_bitrate import (
                    install_screen_share_h264_bitrate_patch,
                )

                install_screen_share_h264_bitrate_patch(fps=self.fps)
            except Exception:
                pass
            try:
                from streaming.h264_hw_patch import install_h264_hardware_encoder

                install_h264_hardware_encoder(enabled=self._h264_hardware_encode)
            except Exception:
                pass
        with self._capture_lock:
            if self._capture is not None and self._capture.is_alive():
                return
            idx = int(self._capture_monitor_index)
            try:
                log_remote_event(
                    f"호스트: 화면 캡처 시작 (모니터 #{idx}"
                    + (
                        ", 가상 디스플레이"
                        if sys.platform == "darwin" and self._virtual_display_enabled
                        else ""
                    )
                    + ")",
                )
            except Exception:
                pass
            self._capture = CaptureThread(
                monitor_index=idx,
                target_fps=self.fps,
                on_frame=self._push_frame,
                window_hwnd=None,
            )
            self._capture.start()
        self._start_darwin_audio()

    def _stop_capture_if_idle_blocking(self) -> None:
        """워커 스레드 전용: 캡처 중지·조인·오디오 중지(블로킹).

        가상 디스플레이(PyObjC)는 여기서 건드리지 않는다. 해제는
        ``_stop_capture_if_idle_finalize`` 가 스레드 풀에서 호출된다.
        """
        try:
            with self._capture_lock:
                if len(self._pcs) > 0:
                    try:
                        log_remote_diag(
                            f"호스트: idle blocking 생략 — 재연결된 피어 있음 "
                            f"({len(self._pcs)}개)"
                        )
                    except Exception:
                        pass
                    return
                cap = self._capture
                self._capture = None
            if cap is None:
                try:
                    log_remote_diag(
                        "호스트: idle blocking — 활성 CaptureThread 없음, "
                        "stop/join 생략"
                    )
                except Exception:
                    pass
            if cap is not None:
                try:
                    log_remote_event("호스트: 뷰어 없음 — 캡처 중지")
                except Exception:
                    pass
                try:
                    log_remote_diag(
                        "호스트: idle blocking — CaptureThread.stop() 호출"
                    )
                except Exception:
                    pass
                try:
                    cap.stop()
                except Exception:
                    pass
                try:
                    log_remote_diag(
                        "호스트: idle blocking — CaptureThread.join 대기 "
                        f"(timeout {self._capture_join_timeout_sec():.0f}s)"
                    )
                except Exception:
                    pass
                self._join_capture_thread(cap)
                try:
                    log_remote_diag("호스트: idle blocking — CaptureThread.join 반환")
                except Exception:
                    pass
            try:
                log_remote_diag("호스트: idle blocking — _stop_darwin_audio")
            except Exception:
                pass
            self._stop_darwin_audio()
        except BaseException as exc:
            try:
                log_remote_event(
                    f"호스트: 뷰어 종료 후 캡처 정리 중 오류 — {type(exc).__name__}: {exc}",
                    error=True,
                )
            except Exception:
                pass

    def _stop_capture_if_idle_finalize(self, *, defer_darwin_vd: bool = False) -> None:
        """스레드 풀 워커에서 호출: VD·geom·봉인·메타.

        ``defer_darwin_vd=True`` 이면 맥 가상 디스플레이 모드에서 봉인 해제만 하고
        즉시 반환한다. VD 해제·geom·메타는 asyncio 루프에서
        :meth:`_idle_finalize_darwin_vd_on_host_loop` 로 이어서 처리한다(메인 스레드
        교착·타임아웃 방지).

        조인 도중 새 피어가 붙었을 수 있으므로 피어 수를 다시 확인한다.
        """
        try:
            with self._capture_lock:
                if len(self._pcs) > 0:
                    try:
                        log_remote_diag(
                            "호스트: idle finalize 생략 — 조인 중 새 피어 연결됨 "
                            f"({len(self._pcs)}개)"
                        )
                    except Exception:
                        pass
                    return
            if sys.platform == "darwin" and self._virtual_display_enabled:
                try:
                    log_remote_diag(
                        "호스트: idle finalize 시작 "
                        "(맥 가상 디스플레이: 봉인 해제 → VD 해제 → geom 초기화)"
                    )
                except Exception:
                    pass
                # 봉인 해제가 CG/스크린 상태를 건드릴 수 있어 VD release 전에 둔다.
                self._try_stop_physical_seal()
                if defer_darwin_vd:
                    try:
                        log_remote_diag(
                            "호스트: idle finalize — VD·geom·메타는 "
                            "호스트 asyncio 코루틴에서 처리"
                        )
                    except Exception:
                        pass
                    return
                try:
                    log_remote_diag(
                        "호스트: idle finalize — _release_virtual_display_session()"
                    )
                except Exception:
                    pass
                self._release_virtual_display_session()
                self._geom = None
                try:
                    log_remote_diag("호스트: idle finalize — self._geom = None 적용")
                except Exception:
                    pass
            else:
                try:
                    log_remote_diag(
                        "호스트: idle finalize — 비 VD 모드: 봉인 해제만"
                    )
                except Exception:
                    pass
                self._try_stop_physical_seal()
            try:
                n_meta = len(self._meta_pending)
                self._meta_pending.clear()
                try:
                    log_remote_diag(
                        f"호스트: idle finalize — DataChannel 메타 대기 목록 비움 "
                        f"({n_meta}개 제거)"
                    )
                except Exception:
                    pass
            except Exception:
                pass
        except BaseException as exc:
            try:
                log_remote_event(
                    f"호스트: 뷰어 종료 후 마무리 중 오류 — {type(exc).__name__}: {exc}",
                    error=True,
                )
            except Exception:
                pass

    def _stop_capture_if_idle(self) -> None:
        """활성 피어가 없으면 캡처를 멈춘다(호스트 스레드에서 호출할 때 동기 전체)."""
        self._stop_capture_if_idle_blocking()
        self._stop_capture_if_idle_finalize()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._startup_done.clear()
        self._startup_error = None
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="Oddments-RemoteHost",
        )
        self._thread.start()
        if not self._startup_done.wait(timeout=15.0):
            raise RuntimeError("원격 호스트 서버 시작 시간 초과.")
        if self._startup_error is not None:
            raise self._startup_error

    def stop(self) -> None:
        # 피어·트랙을 먼저 닫은 뒤 캡처를 멈추면(특히 가상 디스플레이) CG/mss 경합을 줄인다.
        loop = self._loop
        if loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._shutdown_async(), loop)
                fut.result(timeout=12.0)
            except Exception:
                pass

        self._stop_darwin_audio()

        with self._capture_lock:
            cap = self._capture
            self._capture = None
        if cap is not None:
            try:
                cap.stop()
            except Exception:
                pass
            self._join_capture_thread(cap)

        if sys.platform == "darwin" and self._virtual_display_enabled:
            try:
                log_remote_diag(
                    "호스트: RemoteHostServer.stop() — 가상 디스플레이 세션 해제 호출"
                )
            except Exception:
                pass
            self._release_virtual_display_session()
        try:
            log_remote_diag("호스트: RemoteHostServer.stop() — _geom 초기화·봉인 해제")
        except Exception:
            pass
        self._geom = None
        self._try_stop_physical_seal()

        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        self._loop = None
        self._startup_done.clear()
        self._startup_error = None
        self._pcs.clear()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._startup_async())
        except Exception as exc:
            try:
                print(f"[원격호스트] 시작 실패 {self.host}:{self.port}: {exc}", flush=True)
            except OSError:
                pass
            try:
                loop.run_until_complete(self._shutdown_async())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._startup_error = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
            self._startup_done.set()
            return
        self._startup_done.set()
        loop.run_forever()
        try:
            loop.run_until_complete(self._shutdown_async())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass

    async def _startup_async(self) -> None:
        app = web.Application()
        app.router.add_post("/offer", self._handle_offer)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner,
            host=self.host,
            port=self.port,
            reuse_address=True,
        )
        await self._site.start()
        self._peer_disconnect_lock = asyncio.Lock()
        try:
            log_remote_event(
                f"호스트: 신호 서버 대기 http://{self.host}:{self.port}/offer"
            )
        except Exception:
            pass

    async def _shutdown_async(self) -> None:
        pcs = list(self._pcs)
        try:
            log_remote_diag(
                f"호스트: _shutdown_async — 활성 피어 {len(pcs)}개 닫기 예약"
            )
        except Exception:
            pass
        self._pcs.clear()
        for pc in pcs:
            try:
                await pc.close()
            except Exception:
                pass
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                pass
        self._runner = None
        self._site = None
        for attr in ("_host_executor", "_cleanup_executor"):
            ex = getattr(self, attr, None)
            if ex is not None:
                setattr(self, attr, None)
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except TypeError:
                    try:
                        ex.shutdown(wait=False)
                    except Exception:
                        pass
                except Exception:
                    pass

    async def _handle_offer(self, req: web.Request) -> web.Response:
        t_offer = time.perf_counter()
        try:
            log_remote_diag("호스트: /offer 수신")
        except Exception:
            pass
        try:
            params = await req.json()
        except Exception as exc:
            try:
                log_remote_diag(
                    f"호스트: /offer JSON 파싱 실패 — {type(exc).__name__}: {exc}",
                    error=True,
                )
            except Exception:
                pass
            return web.Response(
                status=400,
                text=json.dumps({"error": "invalid json"}),
                content_type="application/json",
            )
        if not self._auth_accept(params.get("token")):
            try:
                log_remote_event("호스트: /offer 인증 실패", error=True)
            except Exception:
                pass
            return web.Response(
                status=401,
                text=json.dumps({"error": "unauthorized"}),
                content_type="application/json",
            )

        # 송출 해상도: 클라이언트 /offer 의 preset (없으면 호스트 사용 중 = host_native).
        raw_preset = params.get("preset")
        if raw_preset is None:
            raw_preset = params.get("resolution_preset")
        if isinstance(raw_preset, str) and raw_preset.strip():
            self._resolution_preset_id = normalize_preset_id(
                raw_preset.strip(),
                fallback="host_native",
            )
            try:
                log_remote_event(
                    f"호스트: 클라이언트 요청 해상도 «{self._resolution_preset_id}»"
                )
            except Exception:
                pass
        else:
            self._resolution_preset_id = "host_native"
            try:
                log_remote_event("호스트: preset 없음 — host_native(메인 화면) 로 송출")
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._host_executor, self._ensure_capture)
        except Exception as exc:
            try:
                log_remote_diag(f"호스트: 캡처 불가 — {exc}", error=True)
            except Exception:
                pass
            return web.Response(
                status=503,
                text=json.dumps({"error": str(exc)}, ensure_ascii=False),
                content_type="application/json",
            )

        try:
            log_remote_diag(
                f"호스트: /offer 캡처 준비 완료 ({time.perf_counter() - t_offer:.2f}s)"
            )
        except Exception:
            pass

        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        pc = RTCPeerConnection(configuration=self._rtc_configuration)
        self._pcs.add(pc)

        @pc.on("connectionstatechange")
        async def _on_state_change() -> None:
            st = pc.connectionState
            try:
                log_remote_diag(
                    f"호스트: WebRTC connectionstatechange → «{st}» "
                    f"(등록된 피어 {len(self._pcs)}개)"
                )
            except Exception:
                pass
            if st == "connected":
                # ICE 연결 직후 한 번 더 봉인(가상 디스플레이 + 물리 화면)을 건다.
                self._try_start_physical_seal()
            if st in ("failed", "closed", "disconnected"):
                # closed·disconnected 가 연달아 오면 idle 정리가 두 번 돌아 각각 ~15s 걸린다.
                if pc not in self._pcs:
                    try:
                        log_remote_diag(
                            "호스트: 원격 종료 처리 생략 — 이미 제거된 피어 "
                            f"(상태 «{st}», 중복 이벤트)"
                        )
                    except Exception:
                        pass
                    return
                self._pcs.discard(pc)
                try:
                    log_remote_event(
                        f"호스트: 피어 {st} (남은 연결 {len(self._pcs)})",
                        error=(st == "failed"),
                    )
                except Exception:
                    pass
                try:
                    log_remote_diag(
                        f"호스트: 원격 종료 — pc.close() 및 idle 정리 예약 "
                        f"(남은 피어 {len(self._pcs)}개)"
                    )
                except Exception:
                    pass
                try:
                    await pc.close()
                except Exception as exc:
                    try:
                        log_remote_diag(
                            f"호스트: pc.close() 예외 — {type(exc).__name__}: {exc}"
                        )
                    except Exception:
                        pass
                # cap.join() 은 워커로, PyObjC 해제는 이 루프에서. 피어 여러 개가 동시에
                # 끊기면 finalize 가 겹치지 않도록 락으로 직렬화한다.
                try:
                    lock = self._peer_disconnect_lock
                    if lock is None:
                        lock = asyncio.Lock()
                        self._peer_disconnect_lock = lock
                    async with lock:
                        t_idle = time.perf_counter()
                        try:
                            log_remote_diag(
                                "호스트: 피어 정리 시작 (idle) — "
                                "cleanup_executor 로 blocking → finalize"
                            )
                        except Exception:
                            pass
                        exe_loop = asyncio.get_running_loop()
                        try:
                            log_remote_diag(
                                "호스트: 런인 실행기 await "
                                "_stop_capture_if_idle_blocking …"
                            )
                        except Exception:
                            pass
                        await exe_loop.run_in_executor(
                            self._cleanup_executor,
                            self._stop_capture_if_idle_blocking,
                        )
                        try:
                            log_remote_diag(
                                "호스트: 피어 정리 blocking 완료 "
                                f"({time.perf_counter() - t_idle:.2f}s)"
                            )
                        except Exception:
                            pass
                        try:
                            log_remote_diag(
                                "호스트: 런인 실행기 await "
                                "_stop_capture_if_idle_finalize …"
                            )
                        except Exception:
                            pass
                        if sys.platform == "darwin" and self._virtual_display_enabled:
                            await exe_loop.run_in_executor(
                                self._cleanup_executor,
                                lambda: self._stop_capture_if_idle_finalize(
                                    defer_darwin_vd=True
                                ),
                            )
                            await self._idle_finalize_darwin_vd_on_host_loop()
                        else:
                            await exe_loop.run_in_executor(
                                self._cleanup_executor,
                                self._stop_capture_if_idle_finalize,
                            )
                        try:
                            log_remote_diag(
                                "호스트: 피어 정리 finalize 완료 "
                                f"({time.perf_counter() - t_idle:.2f}s)"
                            )
                        except Exception:
                            pass
                except Exception as exc:
                    try:
                        log_remote_diag(
                            f"호스트: 피어 정리 예외 — {type(exc).__name__}: {exc}",
                            error=True,
                        )
                    except Exception:
                        pass

        srv = self

        def _on_datachannel(channel) -> None:
            if getattr(channel, "label", "") != "input":
                return
            srv._meta_pending.add(channel)
            if (
                srv._virtual_display_enabled
                and srv._vd_display_id > 0
                and srv._geom is not None
            ):
                _POOL.submit(_host_warp_pointer_to_capture_center, srv)

            @channel.on("message")
            def _on_message(message: object) -> None:
                g = srv._geom
                if g is None:
                    return
                raw = (
                    message.decode("utf-8", errors="ignore")
                    if isinstance(message, (bytes, bytearray))
                    else str(message)
                )
                _POOL.submit(_dispatch_dc_payload, srv, raw, g, channel)

        pc.on("datachannel", _on_datachannel)

        await pc.setRemoteDescription(offer)
        pc.addTrack(
            SharedVideoTrack(
                self.video,
                fps=self.fps,
                max_stream_side=0,
            )
        )
        if sys.platform == "darwin":
            pc.addTrack(SharedAudioTrack(self.audio, sample_rate=48000))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        self._try_start_physical_seal()
        try:
            log_remote_event(
                f"호스트: SDP 답변 전송 (활성 피어 {len(self._pcs)})"
            )
            log_remote_diag(
                f"호스트: /offer 완료·SDP 전송 "
                f"(경과 {time.perf_counter() - t_offer:.2f}s)"
            )
        except Exception:
            pass
        return web.Response(
            text=json.dumps(
                {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
            ),
            content_type="application/json",
        )


__all__ = [
    "RemoteHostServer",
    "default_rtc_configuration",
    "rtc_configuration_from_stun_turn",
]
