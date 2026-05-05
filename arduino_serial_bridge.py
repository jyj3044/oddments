"""
Windows: 지정한 키(가상 키 코드)가 눌리거나 떼질 때 Arduino(COM)로 한 줄씩 전송.

프로토콜 (ASCII, \\n 종료):
  D,<vk>   키 다운 (decimal Windows VK)
  U,<vk>   키 업
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import time
from typing import Optional

if sys.platform == "win32":
    try:
        from pynput import keyboard
    except ImportError:
        keyboard = None  # type: ignore[assignment]
    try:
        import serial  # type: ignore
    except ImportError:
        serial = None  # type: ignore[assignment]
else:
    keyboard = None  # type: ignore[assignment]
    serial = None  # type: ignore[assignment]


_FKEY_RE = re.compile(r"^f(\d{1,2})$", re.IGNORECASE)

_kb_debug_q: queue.SimpleQueue[str] = queue.SimpleQueue()
_kb_debug_on = False
_serial_rx_q: queue.SimpleQueue[str] = queue.SimpleQueue()


def set_key_bridge_debug_logging(enabled: bool) -> None:
    """Arduino 키 브리지 스레드에서 키 이벤트 진단 줄을 큐에 넣을지 여부."""
    global _kb_debug_on
    _kb_debug_on = bool(enabled)


def drain_key_bridge_debug_lines(max_n: int = 200) -> list[str]:
    """UI 스레드에서 소비. max_n 줄까지 한 번에 꺼냄."""
    out: list[str] = []
    for _ in range(max(1, max_n)):
        try:
            out.append(_kb_debug_q.get_nowait())
        except queue.Empty:
            break
    return out


def clear_key_bridge_debug_log() -> None:
    while True:
        try:
            _kb_debug_q.get_nowait()
        except queue.Empty:
            break


def drain_received_serial_lines(max_n: int = 200) -> list[str]:
    """Arduino -> PC 로 들어온 시리얼 텍스트 줄을 UI에서 소비."""
    out: list[str] = []
    for _ in range(max(1, max_n)):
        try:
            out.append(_serial_rx_q.get_nowait())
        except queue.Empty:
            break
    return out


def clear_received_serial_log() -> None:
    while True:
        try:
            _serial_rx_q.get_nowait()
        except queue.Empty:
            break


def _kb_debug_line(msg: str) -> None:
    if not _kb_debug_on:
        return
    ts = time.strftime("%H:%M:%S")
    _kb_debug_q.put(f"[KB] {ts} {msg}\n")


def _key_repr_for_log(key: object) -> str:
    try:
        s = str(key)
    except Exception:
        s = repr(key)
    s = s.replace("\n", " ")
    if len(s) > 96:
        return s[:93] + "…"
    return s


def _token_to_vk(token: str) -> Optional[int]:
    """사용자 입력 토큰(예: a, F1, space)을 Windows VK 정수로."""
    if sys.platform != "win32":
        return None
    t = token.strip()
    if not t:
        return None
    m = _FKEY_RE.match(t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 12:
            return 0x70 + (n - 1)
        if 13 <= n <= 24:
            return 0x7C + (n - 13)
    tl = t.lower()
    named: dict[str, int] = {
        "numpad0": 0x60,
        "numpad1": 0x61,
        "numpad2": 0x62,
        "numpad3": 0x63,
        "numpad4": 0x64,
        "numpad5": 0x65,
        "numpad6": 0x66,
        "numpad7": 0x67,
        "numpad8": 0x68,
        "numpad9": 0x69,
        "space": 0x20,
        "enter": 0x0D,
        "return": 0x0D,
        "tab": 0x09,
        "escape": 0x1B,
        "esc": 0x1B,
        "backspace": 0x08,
        "bs": 0x08,
        "up": 0x26,
        "down": 0x28,
        "left": 0x25,
        "right": 0x27,
        "shift": 0x10,
        "lshift": 0xA0,
        "rshift": 0xA1,
        "ctrl": 0x11,
        "lctrl": 0xA2,
        "rctrl": 0xA3,
        "alt": 0x12,
        "lalt": 0xA4,
        "ralt": 0xA5,
    }
    if tl in named:
        return named[tl]
    # 숫자·영문: VK 표 고정 (pynput from_char 가 vk=None 인 경우가 있어 테이블 우선)
    if len(t) == 1:
        ch = t[0]
        if ch in "0123456789":
            return 0x30 + (ord(ch) - ord("0"))
        low = ch.lower()
        if "a" <= low <= "z":
            return 0x41 + (ord(low) - ord("a"))
    if keyboard is None:
        return None
    if len(t) == 1:
        kc = keyboard.KeyCode.from_char(t)
        vk = getattr(kc, "vk", None)
        if vk is not None:
            return int(vk)
    return None


def parse_key_filter_spec(spec: str) -> tuple[set[int], list[str]]:
    """
    쉼표/세미콜론 구분 목록을 VK 집합으로 변환.
    반환: (vk_set, 인식 못한 토큰 목록)
    """
    vks: set[int] = set()
    bad: list[str] = []
    for part in spec.replace(";", ",").split(","):
        p = part.strip()
        if not p:
            continue
        vk = _token_to_vk(p)
        if vk is None:
            bad.append(p)
        else:
            vks.add(vk)
    return vks, bad


def _event_vk(key: object) -> Optional[int]:
    if keyboard is None:
        return None
    vk = getattr(key, "vk", None)
    if vk is not None:
        return int(vk)
    # 일부 Key 열거형
    if hasattr(keyboard.KeyCode, "from_vk") and hasattr(key, "value"):
        try:
            kc = keyboard.KeyCode.from_vk(int(key.value))  # type: ignore[arg-type]
            v2 = getattr(kc, "vk", None)
            if v2 is not None:
                return int(v2)
        except (TypeError, ValueError, AttributeError):
            pass
    return None


class ArduinoKeyBridge:
    """백그라운드에서 키 감지 → 시리얼 전송."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ser: Optional[object] = None
        self._listener: Optional[object] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._allowed: set[int] = set()
        self._port = ""
        self._baud = 115200
        self._running = False
        self._last_error: Optional[str] = None

    def last_error(self) -> Optional[str]:
        return self._last_error

    def apply_filter_vks(self, vks: set[int]) -> None:
        with self._lock:
            self._allowed = set(vks)

    def start(self, port: str, baud: int, allowed_vks: set[int]) -> bool:
        self.stop()
        self._last_error = None
        if sys.platform != "win32":
            self._last_error = "Arduino 키 전송은 Windows에서만 지원됩니다."
            return False
        if keyboard is None:
            self._last_error = "pynput 패키지가 없습니다. pip install pynput"
            return False
        if serial is None:
            self._last_error = "pyserial 패키지가 없습니다. pip install pyserial"
            return False
        port = (port or "").strip()
        if not port:
            self._last_error = "COM 포트를 입력하세요."
            return False
        if not allowed_vks:
            self._last_error = "전송할 키를 한 개 이상 지정하세요."
            return False
        try:
            ser = serial.Serial(port, baud, timeout=0.2, write_timeout=0.2)
        except Exception as e:
            self._last_error = str(e)
            return False
        with self._lock:
            self._ser = ser
            self._port = port
            self._baud = baud
            self._allowed = set(allowed_vks)
            self._running = True
            self._reader_stop.clear()

        def reader_loop() -> None:
            while not self._reader_stop.is_set():
                with self._lock:
                    s = self._ser
                    running = self._running
                if not running or s is None:
                    return
                try:
                    raw = s.readline()
                except Exception as e:
                    _serial_rx_q.put(f"[RX][ERR] {e}\n")
                    time.sleep(0.1)
                    continue
                if not raw:
                    continue
                try:
                    txt = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    txt = repr(raw)
                if txt:
                    ts = time.strftime("%H:%M:%S")
                    _serial_rx_q.put(f"[RX] {ts} {txt}\n")

        self._reader_thread = threading.Thread(
            target=reader_loop, daemon=True, name="Arduino-Serial-RX"
        )
        self._reader_thread.start()

        def on_press(key: object) -> None:
            self._send_key_event(key, down=True)

        def on_release(key: object) -> None:
            self._send_key_event(key, down=False)

        try:
            lst = keyboard.Listener(on_press=on_press, on_release=on_release)
            lst.start()
            self._listener = lst
        except Exception as e:
            self._last_error = str(e)
            with self._lock:
                self._running = False
                if self._ser is not None:
                    try:
                        self._ser.close()
                    except Exception:
                        pass
                    self._ser = None
            return False
        return True

    def _send_key_event(self, key: object, down: bool) -> None:
        phase = "DOWN" if down else "UP"
        kr = _key_repr_for_log(key)
        vk = _event_vk(key)
        if vk is None:
            _kb_debug_line(
                f"{phase} key={kr} vk=(없음) → 가상키 미해석, 시리얼 없음"
            )
            return
        hexvk = f"0x{vk:02X}"
        with self._lock:
            if not self._running:
                _kb_debug_line(
                    f"{phase} key={kr} vk={vk} ({hexvk}) → 브리지 중지됨, 시리얼 없음"
                )
                return
            if vk not in self._allowed:
                _kb_debug_line(
                    f"{phase} key={kr} vk={vk} ({hexvk}) → 전송 목록에 없음 (필터), 시리얼 없음"
                )
                return
            ser = self._ser
        if ser is None:
            _kb_debug_line(
                f"{phase} key={kr} vk={vk} ({hexvk}) → 시리얼 없음 (포트 닫힘)"
            )
            return
        line = (f"D,{vk}\n" if down else f"U,{vk}\n").encode("ascii", errors="ignore")
        try:
            ser.write(line)
            ser.flush()
            _kb_debug_line(
                f"{phase} key={kr} vk={vk} ({hexvk}) → 시리얼 전송 {line.decode('ascii', errors='replace').strip()}"
            )
        except Exception as e:
            _kb_debug_line(
                f"{phase} key={kr} vk={vk} ({hexvk}) → 시리얼 쓰기 실패: {e}"
            )

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._reader_stop.set()
            lst = self._listener
            self._listener = None
            ser = self._ser
            self._ser = None
            rd = self._reader_thread
            self._reader_thread = None
        if lst is not None:
            try:
                lst.stop()
            except Exception:
                pass
        if rd is not None and rd.is_alive():
            rd.join(timeout=0.4)
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def is_active(self) -> bool:
        """시리얼·키 리스너가 동작 중이면 True."""
        with self._lock:
            return bool(self._running and self._ser is not None and self._listener is not None)


def key_pick_choices() -> tuple[str, ...]:
    """
    UI 셀렉트용 키 이름 목록(parse_key_filter_spec와 동일 토큰).
    """
    out: list[str] = []
    for i in range(1, 25):
        out.append(f"F{i}")
    for i in range(0, 10):
        out.append(f"numpad{i}")
    for c in "abcdefghijklmnopqrstuvwxyz0123456789":
        out.append(c)
    for name in (
        "space",
        "enter",
        "return",
        "tab",
        "escape",
        "esc",
        "backspace",
        "bs",
        "up",
        "down",
        "left",
        "right",
        "shift",
        "lshift",
        "rshift",
        "ctrl",
        "lctrl",
        "rctrl",
        "alt",
        "lalt",
        "ralt",
    ):
        out.append(name)
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return tuple(uniq)


def list_com_ports() -> list[tuple[str, str]]:
    """
    PC에 잡힌 시리얼 포트 목록 (device, description).
    예: ("COM3", "USB-SERIAL CH340"). pyserial 미설치·오류 시 [].
    """
    if serial is None:
        return []
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    try:
        ports = list_ports.comports()
        out = [(p.device, (p.description or "").strip()) for p in ports]
        out.sort(key=lambda x: x[0].upper())
        return out
    except Exception:
        return []


def bridge_supported() -> bool:
    return sys.platform == "win32" and keyboard is not None and serial is not None
