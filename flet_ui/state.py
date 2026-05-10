"""Flet UI 와 기존 백엔드 모듈을 잇는 애플리케이션 상태.

기존 ``main.OddmentsApp`` 의 비-UI 책임(설정 로드/저장, 캡처·감지 스레드,
WebRTC 송출, Arduino 브리지 동기화)을 Tk 의존 없이 다시 구성한 객체.
Flet 페이지에서 이 상태를 의존성 주입처럼 받아 호출한다.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from app_platform import (
    enumerate_windows,
    play_alert_sound,
    stop_queued_alert_sounds,
    window_pick_supported,
)
from capture import CaptureThread, enumerate_monitors
from detection import (
    DetectionConfig,
    OCR_VARIANT_GROUPS_DISABLED,
    OCR_VARIANT_UI_CHOICES,
    get_overlay_store,
    ocr_runtime_ok,
    run_detection_with_overlays,
)
from detection.ocr_backends import ENGINE_RAPIDOCR
from detection.ocr_diag import (
    drain_ocr_log_lines,
    get_ocr_call_total,
    reset_ocr_log,
    set_ocr_keyword_alert_sound_handler,
)
from web_log import drain_web_log_lines, log_web_event, reset_web_log

if sys.platform == "win32":
    from arduino_serial_bridge import (
        ArduinoKeyBridge,
        bridge_supported,
        clear_arduino_notice_buffer,
        clear_key_bridge_debug_log,
        clear_received_serial_log,
        drain_key_bridge_debug_lines,
        drain_received_serial_lines,
        list_com_ports,
        log_arduino_notice,
        parse_key_filter_spec,
        set_key_bridge_debug_logging,
        take_arduino_notice_lines,
    )
else:  # 비 Windows 환경에서는 무동작 스텁으로 대체

    class ArduinoKeyBridge:  # type: ignore[no-redef]
        def __init__(self) -> None:
            pass

        def is_active(self) -> bool:
            return False

        def last_error(self) -> Optional[str]:
            return None

        def traffic_status_error(self) -> Optional[str]:
            return None

        def apply_filter_vks(self, vks: set[int]) -> None:
            pass

        def start(self, port: str, baud: int, allowed_vks: set[int]) -> bool:
            return False

        def stop(self) -> None:
            pass

        def send_virtual_key(self, vk: int, down: bool) -> bool:
            return False

    def bridge_supported() -> bool:  # type: ignore[no-redef]
        return False

    def list_com_ports() -> list[tuple[str, str]]:  # type: ignore[no-redef]
        return []

    def parse_key_filter_spec(spec: str) -> tuple[set[int], list[str]]:  # type: ignore[no-redef]
        return set(), []

    def take_arduino_notice_lines() -> list[str]:  # type: ignore[no-redef]
        return []

    def drain_received_serial_lines(max_n: int = 200) -> list[str]:  # type: ignore[no-redef]
        return []

    def drain_key_bridge_debug_lines(max_n: int = 200) -> list[str]:  # type: ignore[no-redef]
        return []

    def set_key_bridge_debug_logging(enabled: bool) -> None:  # type: ignore[no-redef]
        pass

    def clear_arduino_notice_buffer() -> None:  # type: ignore[no-redef]
        pass

    def clear_key_bridge_debug_log() -> None:  # type: ignore[no-redef]
        pass

    def clear_received_serial_log() -> None:  # type: ignore[no-redef]
        pass

    def log_arduino_notice(msg: str) -> None:  # type: ignore[no-redef]
        pass


from web_stream import (
    build_web_stream_ssl_context,
    list_web_stream_audio_outputs,
)

try:
    from web_stream import WebStreamServer
except ImportError:
    WebStreamServer = None  # type: ignore[assignment]

APP_NAME = "Oddments"
SETTINGS_FILENAME = "oddments_settings.json"
DEFAULT_KEYWORDS = "보스,레드"
ALERT_COOLDOWN_DEFAULT = 3.0
PREVIEW_INTERVAL_MS = 66
DETECTION_TICK_MS = 1000


def _win32_foreground_hwnd() -> int | None:
    """Windows 의 현재 포그라운드 창 HWND. 비-Windows 또는 실패 시 None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        return hwnd if hwnd > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _settings_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else Path.cwd()
    if base and base.exists():
        return base / SETTINGS_FILENAME
    return Path.cwd() / SETTINGS_FILENAME


def _writable_settings_path() -> Path:
    return Path.cwd() / SETTINGS_FILENAME


@dataclass
class ArduinoSettings:
    port: str = "COM3"
    baud: int = 115200
    keys: str = "F1,F2,F3"
    focus_event_enabled: bool = False
    focus_event_key_gain: str = "F8"
    focus_event_key_loss: str = "F8"


@dataclass
class WebStreamSettings:
    enabled: bool = False
    port: int = 8787
    max_side: int = 0
    audio_output: str = ""
    https: bool = False
    ssl_cert: str = ""
    ssl_key: str = ""


@dataclass
class CaptureSettings:
    fps: int = 5
    source_mode: str = "monitor"  # "monitor" | "window"
    monitor_index: int = 1
    picked_hwnd: Optional[int] = None
    picked_summary: str = ""


@dataclass
class DetectionSettings:
    keywords: str = DEFAULT_KEYWORDS
    template_paths: tuple[str, ...] = ()
    template_threshold: float = 0.80
    cooldown_sec: float = ALERT_COOLDOWN_DEFAULT
    keyword_ocr_enabled: bool = True
    ocr_variant_groups: tuple[str, ...] = ()


@dataclass
class WindowSettings:
    """앱 윈도우 위치/크기 + 대시보드 내부 분할 높이.

    None 인 항목은 "마지막에 사용자가 정한 값이 없음" 을 의미하며 부팅 시
    Flet 의 기본값 또는 페이지 코드의 초기값을 그대로 쓴다.
    """

    width: Optional[int] = None
    height: Optional[int] = None
    left: Optional[int] = None
    top: Optional[int] = None
    maximized: bool = False
    dashboard_preview_height: Optional[int] = None


@dataclass
class AppSettings:
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    web: WebStreamSettings = field(default_factory=WebStreamSettings)
    arduino: ArduinoSettings = field(default_factory=ArduinoSettings)
    window: WindowSettings = field(default_factory=WindowSettings)


class AppState:
    """전역 앱 상태. Flet 페이지들이 이 인스턴스를 공유한다.

    - 설정 로드/저장(`load`, `save`)
    - 캡처 시작/중지(`start_capture`, `stop_capture`)
    - 감지 스레드 관리(`_detection_loop`)
    - WebRTC 스트리머 시작/중지
    - Arduino 브리지 동기화
    """

    def __init__(self) -> None:
        self.settings = AppSettings()
        self._cfg = DetectionConfig()
        self._cfg_lock = threading.Lock()

        self._capture: CaptureThread | None = None
        self._capture_error: str | None = None

        self._det_stop = threading.Event()
        self._det_cfg_wake = threading.Event()
        self._det_kw_abort = threading.Event()
        self._det_thread: threading.Thread | None = None
        self._last_triggered = False
        self._last_reason = ""

        self._streamer = None
        self._stream_audio_error: str | None = None

        # 공인 IPv4 조회 결과 캐시(60초). URL 복사 같은 빈번한 호출에서 매번
        # 외부 HTTP 요청을 날리지 않게 한다.
        self._public_ip_cache: str | None = None
        self._public_ip_cache_ts: float = 0.0

        self._arduino = ArduinoKeyBridge()
        self._arduino_supported = bridge_supported()
        self._last_focus_state: bool | None = None
        # 포커스 변화 폴링 전용 스레드 (디텍션 루프와 분리. 200ms 간격으로 가볍게 돈다)
        self._focus_stop = threading.Event()
        self._focus_thread: threading.Thread | None = None

        self._sound_armed = False
        self._sound_lock = threading.Lock()
        self._last_sound_ts = 0.0

        self._on_frame_listeners: list[Callable[[np.ndarray], None]] = []
        self._on_state_listeners: list[Callable[[], None]] = []

        set_ocr_keyword_alert_sound_handler(self._handle_keyword_sound)

    # ─── 설정 ──────────────────────────────────────────────

    def load(self) -> None:
        path = _writable_settings_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._apply_settings_dict(data)
        self._sync_cfg_from_settings()

    def save(self) -> tuple[bool, str | None]:
        data = self._serialize_settings_dict()
        try:
            _writable_settings_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            return False, str(exc)
        if self._arduino_supported and self._arduino.is_active():
            self._sync_arduino_filter_only()
        return True, None

    def _apply_settings_dict(self, d: dict) -> None:
        det = self.settings.detection
        cap = self.settings.capture
        web = self.settings.web
        ard = self.settings.arduino

        det.keywords = str(d.get("keywords", det.keywords))
        det.template_paths = tuple(d.get("template_paths", []))
        det.template_threshold = float(d.get("template_threshold", det.template_threshold))
        det.cooldown_sec = float(d.get("cooldown_sec", det.cooldown_sec))
        engines = d.get("ocr_engines")
        if isinstance(engines, list):
            det.keyword_ocr_enabled = bool(engines)
        det.ocr_variant_groups = tuple(d.get("ocr_variant_groups", []))

        cap.fps = int(d.get("capture_fps", cap.fps) or cap.fps)
        cap.source_mode = str(d.get("capture_source_mode", cap.source_mode))

        web.enabled = bool(d.get("web_stream_enabled", web.enabled))
        web.port = int(d.get("web_stream_port", web.port) or web.port)
        web.max_side = int(d.get("web_stream_max_side", web.max_side) or 0)
        web.audio_output = str(d.get("web_stream_audio_output", web.audio_output) or "")
        web.https = bool(d.get("web_stream_https", web.https))
        web.ssl_cert = str(d.get("web_stream_ssl_cert", web.ssl_cert) or "")
        web.ssl_key = str(d.get("web_stream_ssl_key", web.ssl_key) or "")

        a = d.get("arduino_serial") or {}
        if isinstance(a, dict):
            ard.port = str(a.get("port", ard.port))
            ard.baud = int(a.get("baud", ard.baud) or ard.baud)
            ard.keys = str(a.get("keys", ard.keys))
            ard.focus_event_enabled = bool(a.get("focus_event_enabled", ard.focus_event_enabled))
            ard.focus_event_key_gain = str(a.get("focus_event_key_gain", ard.focus_event_key_gain))
            ard.focus_event_key_loss = str(a.get("focus_event_key_loss", ard.focus_event_key_loss))

        win_d = d.get("window") or {}
        if isinstance(win_d, dict):
            win = self.settings.window

            def _maybe_int(v: object) -> Optional[int]:
                try:
                    if v is None:
                        return None
                    iv = int(v)
                    return iv if iv > 0 else None
                except (TypeError, ValueError):
                    return None

            win.width = _maybe_int(win_d.get("width", win.width))
            win.height = _maybe_int(win_d.get("height", win.height))
            win.left = _maybe_int(win_d.get("left", win.left))
            win.top = _maybe_int(win_d.get("top", win.top))
            win.maximized = bool(win_d.get("maximized", win.maximized))
            win.dashboard_preview_height = _maybe_int(
                win_d.get("dashboard_preview_height", win.dashboard_preview_height)
            )

    def _serialize_settings_dict(self) -> dict:
        det = self.settings.detection
        cap = self.settings.capture
        web = self.settings.web
        ard = self.settings.arduino
        return {
            "keywords": det.keywords,
            "template_paths": list(det.template_paths),
            "template_threshold": det.template_threshold,
            "ocr_engines": [ENGINE_RAPIDOCR] if det.keyword_ocr_enabled else [],
            "cooldown_sec": det.cooldown_sec,
            "ocr_variant_groups": list(det.ocr_variant_groups),
            "capture_fps": cap.fps,
            "capture_source_mode": cap.source_mode,
            "web_stream_enabled": web.enabled,
            "web_stream_port": web.port,
            "web_stream_max_side": web.max_side,
            "web_stream_audio_output": web.audio_output,
            "web_stream_https": web.https,
            "web_stream_ssl_cert": web.ssl_cert,
            "web_stream_ssl_key": web.ssl_key,
            "arduino_serial": {
                "port": ard.port,
                "baud": ard.baud,
                "keys": ard.keys,
                "focus_event_enabled": ard.focus_event_enabled,
                "focus_event_key_gain": ard.focus_event_key_gain,
                "focus_event_key_loss": ard.focus_event_key_loss,
            },
            "window": {
                "width": self.settings.window.width,
                "height": self.settings.window.height,
                "left": self.settings.window.left,
                "top": self.settings.window.top,
                "maximized": bool(self.settings.window.maximized),
                "dashboard_preview_height": self.settings.window.dashboard_preview_height,
            },
        }

    # ─── 설정 → DetectionConfig 동기화 ────────────────────

    def _sync_cfg_from_settings(self) -> None:
        det = self.settings.detection
        keywords = tuple(
            tok.strip() for tok in det.keywords.replace("\n", ",").split(",") if tok.strip()
        )
        engines: tuple[str, ...] = (ENGINE_RAPIDOCR,) if det.keyword_ocr_enabled else ()
        if det.keyword_ocr_enabled:
            groups = det.ocr_variant_groups
            if not groups:
                groups = OCR_VARIANT_GROUPS_DISABLED
        else:
            groups = ()
        with self._cfg_lock:
            self._cfg = DetectionConfig(
                alert_keywords=keywords,
                template_paths=det.template_paths,
                template_threshold=det.template_threshold,
                ocr_engines=engines,
                ocr_variant_groups=groups,
            )
        self._det_cfg_wake.set()
        self._det_kw_abort.set()

    def get_cfg(self) -> DetectionConfig:
        with self._cfg_lock:
            return self._cfg

    # ─── 캡처·감지 ────────────────────────────────────────

    def start_capture(self) -> tuple[bool, str | None]:
        cap = self.settings.capture
        # 창모드에서 대상 창이 선택되지 않은 상태로 시작하면 캡처 대상이 모호해
        # 디텍션·송출이 의미 없는 화면을 잡거나 빈 프레임을 양산하게 된다. 미리 막는다.
        if cap.source_mode == "window" and not cap.picked_hwnd:
            return False, "창모드에서 캡쳐할 창을 먼저 선택해주세요."
        # 전체화면 모드에서는 mss 가 인식한 모니터 인덱스 안에 들어와야 한다.
        # 인덱스가 범위 밖이면 ``mss.monitors[idx]`` 가 IndexError 를 내며 캡처 스레드가
        # 그 자리에서 죽기 때문에 미리 차단한다.
        if cap.source_mode == "monitor":
            mons = enumerate_monitors()
            if not mons:
                return False, "사용 가능한 모니터를 찾지 못했습니다."
            valid = {m["index"] for m in mons}
            if int(cap.monitor_index or 0) not in valid:
                avail = ", ".join(f"#{m['index']}" for m in mons)
                return (
                    False,
                    f"선택한 모니터 #{cap.monitor_index} 가 존재하지 않습니다. "
                    f"사용 가능한 모니터: {avail}",
                )

        self.stop_capture(blocking=True)
        self._sync_cfg_from_settings()

        fps = max(1, min(60, int(cap.fps or 5)))
        hwnd = cap.picked_hwnd if cap.source_mode == "window" else None

        web_streamer = None
        if self.settings.web.enabled and WebStreamServer is not None:
            try:
                web = self.settings.web
                ssl_ctx = build_web_stream_ssl_context(
                    enabled=web.https, certfile=web.ssl_cert, keyfile=web.ssl_key
                )
                web_streamer = WebStreamServer(
                    host="0.0.0.0",
                    port=web.port,
                    fps=float(fps),
                    max_stream_side=int(web.max_side or 1080),
                    audio_output_name=web.audio_output or None,
                    ssl_context=ssl_ctx,
                )
                web_streamer.start()
                self._streamer = web_streamer
                self._stream_audio_error = web_streamer.get_audio_error()
                log_web_event("WebRTC 송출 시작 요청")
            except Exception as exc:  # noqa: BLE001
                log_web_event(f"WebRTC 시작 실패: {exc}")
                return False, f"웹 송출 시작 실패: {exc}"

        def _on_frame(frame: np.ndarray) -> None:
            if self._streamer is not None:
                try:
                    self._streamer.push_video_frame(frame)
                except Exception:  # noqa: BLE001
                    pass
            for cb in list(self._on_frame_listeners):
                try:
                    cb(frame)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()

        try:
            self._capture = CaptureThread(
                monitor_index=int(cap.monitor_index or 1),
                target_fps=float(fps),
                on_frame=_on_frame,
                window_hwnd=hwnd,
            )
            self._capture.start()
        except Exception as exc:  # noqa: BLE001
            return False, f"캡처 스레드 시작 실패: {exc}"

        self._det_stop.clear()
        self._det_thread = threading.Thread(
            target=self._detection_loop, name="oddments-detect", daemon=True
        )
        self._det_thread.start()
        self._sound_armed = True
        # Start 직후: 송출 중 상태로 진입했으므로 사용자 키 필터를 시리얼에 반영하고,
        # 포커스 변화 first-edge 가 잘못 발화하지 않도록 마지막 상태를 초기화.
        self._last_focus_state = None
        self._apply_effective_arduino_filter()
        self._start_focus_polling_thread()
        self._notify_state()
        return True, None

    def _start_focus_polling_thread(self) -> None:
        """포커스 변화 감지를 디텍션 루프와 분리된 200ms 폴링 스레드에서 처리한다.

        디텍션 루프는 OCR 호출이 길어지면 1초 이상 블록될 수 있어 포커스 토글이
        놓치기 쉽다. 가벼운 전용 루프를 두면 항상 빠르게 first-edge 를 잡는다.
        """
        if self._focus_thread is not None and self._focus_thread.is_alive():
            return
        self._focus_stop.clear()

        def _loop() -> None:
            while not self._focus_stop.is_set():
                try:
                    self._emit_focus_transition_to_arduino_if_needed()
                except Exception:  # noqa: BLE001
                    pass
                if self._focus_stop.wait(0.2):
                    return

        self._focus_thread = threading.Thread(
            target=_loop, name="oddments-focus", daemon=True
        )
        self._focus_thread.start()

    def stop_capture(self, *, blocking: bool = False) -> None:
        self._sound_armed = False
        try:
            stop_queued_alert_sounds()
        except Exception:  # noqa: BLE001
            pass

        det = self._det_thread
        self._det_stop.set()
        self._det_cfg_wake.set()
        self._det_kw_abort.set()
        cap = self._capture
        self._capture = None
        self._det_thread = None

        # 포커스 폴링 스레드 정지 (이후 _shutdown 안에서 join).
        self._focus_stop.set()
        focus_thread = self._focus_thread
        self._focus_thread = None

        streamer = self._streamer
        self._streamer = None

        def _shutdown() -> None:
            if cap is not None:
                try:
                    cap.stop()
                    cap.join(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
            if det is not None:
                try:
                    det.join(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
            if focus_thread is not None and focus_thread.is_alive():
                try:
                    focus_thread.join(timeout=1.0)
                except Exception:  # noqa: BLE001
                    pass
            if streamer is not None:
                try:
                    streamer.stop()
                except Exception:  # noqa: BLE001
                    pass
            try:
                get_overlay_store().clear()
            except Exception:  # noqa: BLE001
                pass
            # 송출 종료: 시리얼에 빈 필터를 적용해 어떤 키도 통과하지 못하게 하고,
            # 포커스 first-edge 도 초기화한다.
            self._last_focus_state = None
            self._apply_effective_arduino_filter()
            self._notify_state()

        if blocking:
            _shutdown()
        else:
            threading.Thread(target=_shutdown, name="oddments-stop", daemon=True).start()

    def is_running(self) -> bool:
        return self._capture is not None and self._capture.is_alive()

    def _detection_loop(self) -> None:
        while not self._det_stop.is_set():
            cap = self._capture
            if cap is None:
                break
            frame = cap.get_frame()
            if frame is None:
                if self._det_stop.wait(0.1):
                    return
                continue
            self._det_kw_abort.clear()
            try:
                triggered, reason, _ = run_detection_with_overlays(
                    frame, self.get_cfg(), self._det_stop, self._det_kw_abort
                )
            except Exception as exc:  # noqa: BLE001
                triggered, reason = False, f"감지 오류: {exc}"
            self._last_triggered = triggered
            self._last_reason = reason
            if triggered:
                self._maybe_play_sound()
            # 포커스 변화 감지는 별도 ``oddments-focus`` 스레드가 200ms 주기로 처리.
            if self._det_cfg_wake.wait(timeout=DETECTION_TICK_MS / 1000.0):
                self._det_cfg_wake.clear()

    # ─── 알림음 ───────────────────────────────────────────

    def _handle_keyword_sound(self) -> None:
        self._maybe_play_sound()

    def _maybe_play_sound(self) -> None:
        if not self._sound_armed:
            return
        with self._sound_lock:
            now = time.monotonic()
            if now - self._last_sound_ts < self.settings.detection.cooldown_sec:
                return
            self._last_sound_ts = now
        try:
            play_alert_sound()
        except Exception:  # noqa: BLE001
            pass

    # ─── 캡처 프레임 구독 ────────────────────────────────

    def add_frame_listener(self, cb: Callable[[np.ndarray], None]) -> None:
        if cb not in self._on_frame_listeners:
            self._on_frame_listeners.append(cb)

    def remove_frame_listener(self, cb: Callable[[np.ndarray], None]) -> None:
        if cb in self._on_frame_listeners:
            self._on_frame_listeners.remove(cb)

    def get_latest_preview(self) -> np.ndarray | None:
        cap = self._capture
        if cap is None:
            return None
        frame = cap.get_frame()
        if frame is None:
            return None
        return frame

    def get_capture_frame_seq(self) -> int:
        cap = self._capture
        if cap is None:
            return 0
        try:
            return int(cap.get_frame_seq())
        except Exception:  # noqa: BLE001
            return 0

    def get_capture_error(self) -> str | None:
        cap = self._capture
        if cap is None:
            return None
        return cap.get_capture_error()

    # ─── 상태 알림 ────────────────────────────────────────

    def add_state_listener(self, cb: Callable[[], None]) -> None:
        if cb not in self._on_state_listeners:
            self._on_state_listeners.append(cb)

    def _notify_state(self) -> None:
        for cb in list(self._on_state_listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    # ─── Arduino ─────────────────────────────────────────

    def arduino_supported(self) -> bool:
        return self._arduino_supported

    def arduino_active(self) -> bool:
        return self._arduino_supported and self._arduino.is_active()

    def arduino_connect(self) -> tuple[bool, str | None]:
        if not self._arduino_supported:
            return False, "Arduino 브리지는 Windows 에서만 동작합니다."
        ard = self.settings.arduino
        vks, _bad = parse_key_filter_spec(ard.keys)
        if not vks:
            return False, "전송할 키를 한 개 이상 지정하세요."
        # 브리지 자체는 사용자 키 목록으로 정상 시작하되, 실효 필터는 송출 상태에 따라
        # 이어서 다시 덮어쓰므로, Start 중이 아니면 시리얼로 키가 나가지 않는다.
        ok = self._arduino.start(ard.port, ard.baud, vks)
        if not ok:
            return False, self._arduino.last_error() or "연결 실패"
        self._apply_effective_arduino_filter()
        self._notify_state()
        return True, None

    def arduino_disconnect(self) -> None:
        if self._arduino_supported:
            self._arduino.stop()
            self._last_focus_state = None
            self._notify_state()

    def _effective_filter_vks(self) -> set[int]:
        """전송할 키의 *실효* 필터.

        Start(캡처 송출) 중일 때만 사용자가 지정한 키 목록을 그대로 적용하고,
        그 외에는 빈 집합을 반환해 어떤 키도 시리얼로 전송되지 않게 한다.
        """
        if not self.is_running():
            return set()
        ard = self.settings.arduino
        vks, _bad = parse_key_filter_spec(ard.keys)
        return vks

    def _apply_effective_arduino_filter(self) -> None:
        """Arduino 브리지가 활성 상태일 때만 실효 필터를 즉시 반영한다."""
        if not self._arduino_supported:
            return
        try:
            if not self._arduino.is_active():
                return
            self._arduino.apply_filter_vks(self._effective_filter_vks())
        except Exception:  # noqa: BLE001
            pass

    def _sync_arduino_filter_only(self) -> None:
        # save() 등 외부에서 호출. 실효 필터(=is_running 게이트)를 사용한다.
        self._apply_effective_arduino_filter()

    def arduino_last_error(self) -> str | None:
        if not self._arduino_supported:
            return None
        return self._arduino.last_error()

    # ─── 포커스 이벤트 → Arduino ────────────────────────

    def _focus_event_vk(self, now_focused: bool) -> int | None:
        """포커스 획득(``now_focused=True``)/해제(False) 시 보낼 단일 VK.

        획득/해제 셀렉트박스에 정확히 1개의 키 토큰이 있어야 하며, 그렇지 않으면
        ``None`` 을 반환해 호출자가 전송을 건너뛰도록 한다.
        """
        ard = self.settings.arduino
        token = (
            ard.focus_event_key_gain if now_focused else ard.focus_event_key_loss
        ).strip()
        if not token:
            return None
        vks, bad = parse_key_filter_spec(token)
        if bad or len(vks) != 1:
            return None
        return next(iter(vks))

    def _emit_focus_transition_to_arduino_if_needed(self) -> None:
        """선택 창의 포커스 변화를 감지해 Arduino 로 DOWN→UP 을 한 번씩 전송한다.

        포커스 *추적* 자체는 다음 4가지가 모두 만족되어야 하며 그 외에는 추적 상태
        (``_last_focus_state``) 를 ``None`` 으로 초기화해 다음 활성화 시 first-edge
        가 잘못 발화하지 않게 한다:

        - Windows 플랫폼
        - Start (``is_running()``) 중
        - 소스 모드가 ``window`` 이고 ``picked_hwnd`` 가 지정됨
        - 사용자가 포커스 이벤트 사용 체크를 켬

        포커스 변화가 감지되면 Arduino 연결 상태/키 설정을 점검해 다음 4가지 결과
        중 하나를 *반드시* ``[FOCUS]`` 라인으로 로그에 남긴다:

        - 정상 전송 → ``전송`` 메시지
        - Arduino 미연결 → ``Arduino 미연결로 전송 생략``
        - 키 토큰이 비어있거나 1개가 아님 → ``키 설정 오류``
        - 시리얼 쓰기 실패 → ``전송 실패``
        """
        if sys.platform != "win32":
            return

        cap = self.settings.capture
        ard = self.settings.arduino
        active_tracking = (
            self.is_running()
            and ard.focus_event_enabled
            and cap.source_mode == "window"
            and cap.picked_hwnd is not None
        )
        if not active_tracking:
            # 추적 비활성화 — 다음 활성화 시 first-edge 가 잘못 발화하지 않도록 초기화.
            self._last_focus_state = None
            return

        target_hwnd = int(cap.picked_hwnd)
        try:
            from windows_capture import (  # type: ignore[import-not-found]
                is_league_capture_pair_hwnd,
                resolve_league_capture_hwnd,
            )

            if is_league_capture_pair_hwnd(target_hwnd):
                target_hwnd = int(resolve_league_capture_hwnd(target_hwnd))
        except Exception:  # noqa: BLE001
            pass
        foreground = _win32_foreground_hwnd()
        now_focused = foreground is not None and foreground == target_hwnd
        last = self._last_focus_state
        self._last_focus_state = now_focused
        if last is None or last == now_focused:
            return

        edge = "획득" if now_focused else "해제"
        if not self._arduino_supported or not self._arduino.is_active():
            try:
                log_arduino_notice(
                    f"[FOCUS] {edge} 감지 — Arduino 미연결로 전송 생략"
                )
            except Exception:  # noqa: BLE001
                pass
            return
        vk = self._focus_event_vk(now_focused)
        if vk is None:
            try:
                log_arduino_notice(
                    f"[FOCUS] {edge} — 키 설정 오류 (획득/해제 각 1개 키만 선택)"
                )
            except Exception:  # noqa: BLE001
                pass
            return
        ok_down = self._arduino.send_virtual_key(vk, down=True)
        ok_up = self._arduino.send_virtual_key(vk, down=False)
        if ok_down and ok_up:
            try:
                log_arduino_notice(
                    f"[FOCUS] {edge} → vk={vk} (0x{vk:02X}) DOWN+UP 전송 완료"
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                log_arduino_notice(
                    f"[FOCUS] {edge} — 전송 실패 (Arduino 연결 상태 확인 필요)"
                )
            except Exception:  # noqa: BLE001
                pass

    # ─── 창 선택 ──────────────────────────────────────────

    def list_windows(self) -> list:
        if not window_pick_supported():
            return []
        try:
            return enumerate_windows()
        except Exception:  # noqa: BLE001
            return []

    def list_monitors(self) -> list[dict]:
        """현재 시스템에 연결된 물리 모니터 목록을 dict 리스트로 반환."""
        try:
            return enumerate_monitors()
        except Exception:  # noqa: BLE001
            return []

    # ─── 웹 송출 URL ──────────────────────────────────────

    def web_stream_url_scheme(self) -> str:
        return "https" if self.settings.web.https else "http"

    def get_local_web_url(self) -> str:
        port = self.settings.web.port
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip = "127.0.0.1"
        return f"{self.web_stream_url_scheme()}://{ip}:{port}/"

    # ─── 공인 IPv4 (외부 시청용 URL) ─────────────────────────────────
    @staticmethod
    def _parse_public_ipv4(raw: str) -> str | None:
        """문자열에서 사설/루프백/링크로컬을 제외한 IPv4 한 개를 골라낸다."""
        for line in raw.replace("\r", "\n").split("\n"):
            chunk = line.strip()
            if not chunk:
                continue
            for token in chunk.replace(",", " ").split():
                token = token.strip().strip('"').strip("'")
                try:
                    addr = ipaddress.ip_address(token)
                except ValueError:
                    continue
                if not isinstance(addr, ipaddress.IPv4Address):
                    continue
                if (
                    addr.is_private
                    or addr.is_loopback
                    or addr.is_link_local
                    or addr.is_multicast
                    or addr.is_reserved
                ):
                    continue
                return str(addr)
        return None

    def _fetch_public_ipv4_wan(self) -> str | None:
        """여러 echo 서비스에서 공인 IPv4 를 조회. 한 곳이라도 성공하면 반환."""
        endpoints = (
            "https://ipv4.icanhazip.com",
            "https://checkip.amazonaws.com",
            "https://api.ipify.org?format=json",
        )
        opener = urllib.request.build_opener()
        for url in endpoints:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Oddments/1.0"},
                    method="GET",
                )
                with opener.open(req, timeout=2.5) as resp:
                    body = resp.read().decode("utf-8", errors="ignore")
                if "ipify" in url and "json" in url:
                    try:
                        data = json.loads(body)
                        body = str(data.get("ip", "")).strip()
                    except (json.JSONDecodeError, TypeError):
                        continue
                got = self._parse_public_ipv4(body)
                if got:
                    return got
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
        return None

    def get_public_web_url(self) -> str:
        """공인 IPv4 기반 외부 시청 URL. 조회 실패 시 LAN URL 로 폴백.

        결과를 60초 캐시해 같은 IP 가 짧은 시간에 반복 요청되더라도 외부
        endpoint 를 매번 호출하지 않는다. 네트워크 호출이 포함되므로 호출자는
        UI 스레드에서 직접 부르지 말고 ``asyncio.to_thread`` 같은 워커로 위임할 것.
        """
        port = self.settings.web.port
        scheme = self.web_stream_url_scheme()
        now = time.monotonic()
        if (
            self._public_ip_cache
            and (now - self._public_ip_cache_ts) < 60.0
        ):
            return f"{scheme}://{self._public_ip_cache}:{port}/"
        wan = self._fetch_public_ipv4_wan()
        if wan:
            self._public_ip_cache = wan
            self._public_ip_cache_ts = now
            return f"{scheme}://{wan}:{port}/"
        # 조회 실패: 캐시 비우고 LAN URL 로 폴백.
        self._public_ip_cache = None
        self._public_ip_cache_ts = 0.0
        return self.get_local_web_url()

    def get_web_viewer_count(self) -> int:
        if self._streamer is None:
            return 0
        try:
            return self._streamer.get_connected_viewer_count()
        except Exception:  # noqa: BLE001
            return 0

    def get_web_audio_status(self) -> str:
        if self._streamer is None:
            return ""
        try:
            return self._streamer.get_audio_status_line() or ""
        except Exception:  # noqa: BLE001
            return ""

    def list_audio_outputs(self) -> list[str]:
        try:
            return list_web_stream_audio_outputs()
        except Exception:  # noqa: BLE001
            return []

    # ─── 종료 ─────────────────────────────────────────────

    def shutdown(self) -> None:
        try:
            self.save()
        except Exception:  # noqa: BLE001
            pass
        self.stop_capture(blocking=True)
        try:
            self.arduino_disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            set_ocr_keyword_alert_sound_handler(None)
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "AppState",
    "AppSettings",
    "ArduinoSettings",
    "WebStreamSettings",
    "CaptureSettings",
    "DetectionSettings",
    "OCR_VARIANT_UI_CHOICES",
    "drain_ocr_log_lines",
    "get_ocr_call_total",
    "reset_ocr_log",
    "drain_web_log_lines",
    "log_web_event",
    "reset_web_log",
    "list_com_ports",
    "drain_received_serial_lines",
    "drain_key_bridge_debug_lines",
    "set_key_bridge_debug_logging",
    "clear_arduino_notice_buffer",
    "clear_key_bridge_debug_log",
    "clear_received_serial_log",
    "take_arduino_notice_lines",
    "log_arduino_notice",
    "ocr_runtime_ok",
]
