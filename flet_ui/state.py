"""Flet UI 와 백엔드 모듈을 잇는 애플리케이션 상태.

설정 로드/저장, 캡처·감지 스레드, WebRTC 송출, Arduino 브리지 동기화 등 비-UI 책임을 담는 객체.
Flet 페이지에서 이 상태를 의존성 주입처럼 받아 호출한다.
"""

from __future__ import annotations

import ipaddress
import json
import os
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
    RegionDetectionConfig,
    RegionRect,
    get_overlay_store,
    ocr_runtime_ok,
    run_detection_detailed,
    run_detection_with_overlays,
)
from detection.ocr_backends import ENGINE_RAPIDOCR
from detection.ocr_diag import (
    drain_ocr_log_lines,
    get_ocr_call_total,
    reset_ocr_log,
    set_ocr_keyword_alert_sound_handler,
)
from streaming.remote_log import (
    drain_remote_log_lines,
    log_remote_diag,
    log_remote_event,
    reset_remote_log,
)

def _vd_test_log(msg: str, *, error: bool = False) -> None:
    """VD 테스트 생성·해제 진단(항상 remote 즉시 로그)."""
    try:
        log_remote_diag(msg, error=error)
    except Exception:
        pass



from streaming.web_log import drain_web_log_lines, log_web_event, reset_web_log

if sys.platform == "win32":
    from arduino.serial_bridge import (
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


from streaming.web_stream import (
    build_web_stream_ssl_context,
    list_web_stream_audio_outputs,
)

try:
    from streaming.web_stream import WebStreamServer
except ImportError:
    WebStreamServer = None  # type: ignore[assignment]

from streaming.remote_host import RemoteHostServer

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


def _settings_base_dir() -> Path:
    """설정 JSON 기준 폴더. PyInstaller 등 동결 빌드는 exe 위치(시작 메뉴 실행 시 cwd 와 무관)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _settings_path() -> Path:
    return _settings_base_dir() / SETTINGS_FILENAME


def _writable_settings_path() -> Path:
    return _settings_base_dir() / SETTINGS_FILENAME


def _safe_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_hex_color(value: object, default: str = "#ff3030") -> str:
    raw = str(value or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) != 6:
        raw = default.lstrip("#")
    try:
        int(raw, 16)
    except ValueError:
        raw = default.lstrip("#")
    return "#" + raw.lower()


def _hex_to_bgr(value: object) -> tuple[int, int, int]:
    h = _normalize_hex_color(value).lstrip("#")
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return b, g, r


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
    # Web Stream 전용. 원격 호스트 h264_hardware_encode 와 별도.
    h264_hardware_encode: bool = True


@dataclass
class CaptureSettings:
    fps: int = 5
    source_mode: str = "monitor"  # "monitor" | "window"
    monitor_index: int = 1
    picked_hwnd: Optional[int] = None
    picked_summary: str = ""


@dataclass
class RegionRuleSettings:
    id: str
    name: str
    enabled: bool = True
    keywords: str = ""
    rect: RegionRect | None = None
    ocr_variant_groups: tuple[str, ...] = ()
    color_match_enabled: bool = False
    color_hex: str = "#ff3030"
    color_tolerance: int = 24
    cooldown_sec: float = ALERT_COOLDOWN_DEFAULT
    custom_sound_path: str = ""
    expanded: bool = False


def _region_rect_from_obj(value: object) -> RegionRect | None:
    if not isinstance(value, dict):
        return None
    x = _safe_int(value.get("x"), 0)
    y = _safe_int(value.get("y"), 0)
    w = _safe_int(value.get("w"), 0)
    h = _safe_int(value.get("h"), 0)
    if w <= 0 or h <= 0:
        return None
    return RegionRect(max(0, x), max(0, y), w, h)


def _region_rect_to_dict(rect: RegionRect | None) -> dict | None:
    if rect is None:
        return None
    return {"x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h}


def _region_rule_from_dict(value: object, index: int) -> RegionRuleSettings | None:
    if not isinstance(value, dict):
        return None
    rid = str(value.get("id") or f"region-{index + 1}")
    name = str(value.get("name") or f"영역 {index + 1}")
    raw_groups = value.get("ocr_variant_groups", [])
    groups = tuple(str(v) for v in raw_groups) if isinstance(raw_groups, list) else ()
    return RegionRuleSettings(
        id=rid,
        name=name,
        enabled=bool(value.get("enabled", True)),
        keywords=str(value.get("keywords", "")),
        rect=_region_rect_from_obj(value.get("rect")),
        ocr_variant_groups=groups,
        color_match_enabled=bool(value.get("color_match_enabled", False)),
        color_hex=_normalize_hex_color(value.get("color_hex", "#ff3030")),
        color_tolerance=max(0, min(100, _safe_int(value.get("color_tolerance"), 24))),
        cooldown_sec=max(0.0, _safe_float(value.get("cooldown_sec"), ALERT_COOLDOWN_DEFAULT)),
        custom_sound_path=str(value.get("custom_sound_path") or ""),
        expanded=bool(value.get("expanded", False)),
    )


def _region_rule_to_dict(rule: RegionRuleSettings) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": bool(rule.enabled),
        "keywords": rule.keywords,
        "rect": _region_rect_to_dict(rule.rect),
        "ocr_variant_groups": list(rule.ocr_variant_groups),
        "color_match_enabled": bool(rule.color_match_enabled),
        "color_hex": _normalize_hex_color(rule.color_hex),
        "color_tolerance": max(0, min(100, int(rule.color_tolerance))),
        "cooldown_sec": max(0.0, float(rule.cooldown_sec)),
        "custom_sound_path": rule.custom_sound_path,
        "expanded": bool(rule.expanded),
    }


@dataclass
class DetectionSettings:
    keywords: str = DEFAULT_KEYWORDS
    template_paths: tuple[str, ...] = ()
    template_threshold: float = 0.80
    cooldown_sec: float = ALERT_COOLDOWN_DEFAULT
    keyword_ocr_enabled: bool = True
    ocr_variant_groups: tuple[str, ...] = ()
    region_rules: tuple[RegionRuleSettings, ...] = ()
    main_expanded: bool = False


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
    # 메인 창 왼쪽 네비게이션 패널 폭(px). None 이면 테마 기본 SIDEBAR_WIDTH.
    sidebar_width: Optional[int] = None
    # 원격 뷰어 보조 창 왼쪽 패널 펼침 폭(px). None 이면 코드 초기값.
    remote_viewer_sidebar_width: Optional[int] = None


@dataclass
class RemoteHostProfile:
    """원격 제어 호스트(공유 측) 옵션. 캡처·인코딩·리슨은 네이티브 모듈에서 소비."""

    listen_port: int = 49152
    monitor_index: int = 1
    stream_fps: int = 30
    # 비어 있으면 연결 시 비밀번호를 요구하지 않음. 설정 시 클라이언트와 동일해야 한다.
    auth_token: str = ""
    # True 이면 호스트 시작 시 NVENC/AMF/VideoToolbox 등을 시도하고, 없으면 libx264 유지.
    h264_hardware_encode: bool = False
    # macOS 호스트: CGVirtualDisplay 로 가상 모니터만 송출 (물리 모니터 미송출).
    use_virtual_display: bool = True
    # macOS: 원격용 가상 입력 장치 이름 부분 문자열 (예: BlackHole). 비우면 BlackHole 자동 탐색.
    darwin_audio_input: str = ""


@dataclass
class RemoteClientProfile:
    """원격 제어 클라이언트(뷰어) 연결 정보."""

    host: str = ""
    port: int = 49152
    auth_token: str = ""
    # 연결 시 /offer 에 실림. 가상 디스플레이 호스트가 이 프리셋으로 모니터를 연다.
    resolution_preset: str = "host_native"
    # True 이면 Windows 뷰어→macOS 호스트 시 Ctrl=Control, Win=⌥, Alt=⌘ 로 전송.
    mac_modifier_remap: bool = False


@dataclass
class RemoteControlSettings:
    host: RemoteHostProfile = field(default_factory=RemoteHostProfile)
    client: RemoteClientProfile = field(default_factory=RemoteClientProfile)


@dataclass
class AppSettings:
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    web: WebStreamSettings = field(default_factory=WebStreamSettings)
    arduino: ArduinoSettings = field(default_factory=ArduinoSettings)
    window: WindowSettings = field(default_factory=WindowSettings)
    remote: RemoteControlSettings = field(default_factory=RemoteControlSettings)
    dark_mode: bool = False


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

        self._remote_host = None
        # 호스트 시작 실패 시에만 설정. 성공·중지 시 None.
        self._remote_host_last_error: str | None = None
        # macOS: 원격 설정 화면에서 CGVirtualDisplay 생성·해제만 따로 시험할 때 사용.
        self._vd_test_display: tuple[object, int] | None = None
        self._vd_test_busy = False
        # ``create_virtual_display`` 는 락을 오래 잡으면 안 된다. UI ``run_task`` 가
        # ``vd_test_display_active`` 등으로 같은 락을 기다리며 Flet 이 Working… 처럼 멈춘다.
        self._vd_test_creating = False
        # 직전 VD 테스트 해제의 CGDirectDisplayID(로그·선택 안정화에만 사용).
        self._vd_test_last_released_cg_id: int | None = None
        # ``CGVirtualDisplayDescriptor`` 시리얼 — 해제 직후 재생성 시 WindowServer 가
        # 동일 디스크립터를 거부하는 것을 줄이기 위해 생성할 때마다 증가.
        self._vd_test_descriptor_serial = 0
        self._vd_test_lock = threading.Lock()

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
        self._last_sound_ts_by_event: dict[str, float] = {}

        self._on_frame_listeners: list[Callable[[np.ndarray], None]] = []
        self._on_state_listeners: list[Callable[[], None]] = []
        self._theme_listeners: list[Callable[[], None]] = []

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
        det.main_expanded = bool(d.get("ocr_main_expanded", det.main_expanded))
        raw_region_rules = d.get("region_rules", [])
        if isinstance(raw_region_rules, list):
            parsed_rules: list[RegionRuleSettings] = []
            for i, raw_rule in enumerate(raw_region_rules):
                rule = _region_rule_from_dict(raw_rule, i)
                if rule is not None:
                    parsed_rules.append(rule)
            det.region_rules = tuple(parsed_rules)

        cap.fps = int(d.get("capture_fps", cap.fps) or cap.fps)
        cap.source_mode = str(d.get("capture_source_mode", cap.source_mode))
        try:
            cap.monitor_index = max(1, int(d.get("capture_monitor_index", cap.monitor_index) or 1))
        except (TypeError, ValueError):
            cap.monitor_index = 1
        try:
            raw_hwnd = d.get("capture_picked_hwnd", cap.picked_hwnd)
            cap.picked_hwnd = int(raw_hwnd) if raw_hwnd not in (None, "") else None
        except (TypeError, ValueError):
            cap.picked_hwnd = None
        cap.picked_summary = str(d.get("capture_picked_summary", cap.picked_summary) or "")

        web.enabled = bool(d.get("web_stream_enabled", web.enabled))
        web.port = int(d.get("web_stream_port", web.port) or web.port)
        web.max_side = int(d.get("web_stream_max_side", web.max_side) or 0)
        web.audio_output = str(d.get("web_stream_audio_output", web.audio_output) or "")
        web.https = bool(d.get("web_stream_https", web.https))
        web.ssl_cert = str(d.get("web_stream_ssl_cert", web.ssl_cert) or "")
        web.ssl_key = str(d.get("web_stream_ssl_key", web.ssl_key) or "")
        web.h264_hardware_encode = bool(
            d.get("web_stream_h264_hardware_encode", web.h264_hardware_encode)
        )

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
            raw_sw = win_d.get("sidebar_width", win.sidebar_width)
            if raw_sw is not None:
                try:
                    swi = int(raw_sw)
                except (TypeError, ValueError):
                    swi = 0
                if swi > 0:
                    win.sidebar_width = max(180, min(560, swi))
            raw_rv = win_d.get(
                "remote_viewer_sidebar_width", win.remote_viewer_sidebar_width
            )
            if raw_rv is not None:
                try:
                    rvi = int(raw_rv)
                except (TypeError, ValueError):
                    rvi = 0
                if rvi > 0:
                    win.remote_viewer_sidebar_width = max(180, min(560, rvi))

        self.settings.dark_mode = bool(d.get("dark_mode", self.settings.dark_mode))

        r = d.get("remote_control")
        if isinstance(r, dict):
            rh = r.get("host") or {}
            rc = r.get("client") or {}

            def _safe_u16_port(v: object, default: int) -> int:
                try:
                    p = int(v)
                except (TypeError, ValueError):
                    p = int(default)
                return max(1, min(65535, p))

            if isinstance(rh, dict):
                h = self.settings.remote.host
                h.listen_port = _safe_u16_port(
                    rh.get("listen_port", h.listen_port), h.listen_port
                )
                try:
                    h.monitor_index = max(
                        1, int(rh.get("monitor_index", h.monitor_index) or 1)
                    )
                except (TypeError, ValueError):
                    h.monitor_index = 1
                try:
                    h.stream_fps = max(
                        5, min(60, int(rh.get("stream_fps", h.stream_fps) or 30))
                    )
                except (TypeError, ValueError):
                    h.stream_fps = 30
                h.auth_token = str(rh.get("auth_token", h.auth_token) or "")
                h.h264_hardware_encode = bool(
                    rh.get("h264_hardware_encode", h.h264_hardware_encode)
                )
                h.use_virtual_display = bool(
                    rh.get("use_virtual_display", h.use_virtual_display)
                )
                h.darwin_audio_input = str(
                    rh.get("darwin_audio_input", h.darwin_audio_input) or ""
                )
            if isinstance(rc, dict):
                c = self.settings.remote.client
                c.host = str(rc.get("host", c.host) or "")
                c.port = _safe_u16_port(rc.get("port", c.port), c.port)
                c.auth_token = str(rc.get("auth_token", c.auth_token) or "")
                c.resolution_preset = str(
                    rc.get("resolution_preset", c.resolution_preset) or "host_native"
                )
                c.mac_modifier_remap = bool(
                    rc.get("mac_modifier_remap", c.mac_modifier_remap)
                )

    def _serialize_settings_dict(self) -> dict:
        det = self.settings.detection
        cap = self.settings.capture
        web = self.settings.web
        ard = self.settings.arduino
        rem = self.settings.remote
        rh = rem.host
        rc = rem.client
        return {
            "keywords": det.keywords,
            "template_paths": list(det.template_paths),
            "template_threshold": det.template_threshold,
            "ocr_engines": [ENGINE_RAPIDOCR] if det.keyword_ocr_enabled else [],
            "cooldown_sec": det.cooldown_sec,
            "ocr_variant_groups": list(det.ocr_variant_groups),
            "ocr_main_expanded": bool(det.main_expanded),
            "region_rules": [_region_rule_to_dict(r) for r in det.region_rules],
            "capture_fps": cap.fps,
            "capture_source_mode": cap.source_mode,
            "capture_monitor_index": cap.monitor_index,
            "capture_picked_hwnd": cap.picked_hwnd,
            "capture_picked_summary": cap.picked_summary,
            "web_stream_enabled": web.enabled,
            "web_stream_port": web.port,
            "web_stream_max_side": web.max_side,
            "web_stream_audio_output": web.audio_output,
            "web_stream_https": web.https,
            "web_stream_ssl_cert": web.ssl_cert,
            "web_stream_ssl_key": web.ssl_key,
            "web_stream_h264_hardware_encode": web.h264_hardware_encode,
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
                "sidebar_width": self.settings.window.sidebar_width,
                "remote_viewer_sidebar_width": self.settings.window.remote_viewer_sidebar_width,
            },
            "dark_mode": bool(self.settings.dark_mode),
            "remote_control": {
                "host": {
                    "listen_port": rh.listen_port,
                    "monitor_index": rh.monitor_index,
                    "stream_fps": rh.stream_fps,
                    "auth_token": rh.auth_token,
                    "h264_hardware_encode": rh.h264_hardware_encode,
                    "use_virtual_display": rh.use_virtual_display,
                    "darwin_audio_input": rh.darwin_audio_input,
                },
                "client": {
                    "host": rc.host,
                    "port": rc.port,
                    "auth_token": rc.auth_token,
                    "resolution_preset": rc.resolution_preset,
                    "mac_modifier_remap": rc.mac_modifier_remap,
                },
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
        region_rules: list[RegionDetectionConfig] = []
        for rule in det.region_rules:
            if rule.rect is None:
                continue
            region_keywords = tuple(
                tok.strip()
                for tok in rule.keywords.replace("\n", ",").split(",")
                if tok.strip()
            )
            region_groups = rule.ocr_variant_groups
            if not region_groups:
                region_groups = OCR_VARIANT_GROUPS_DISABLED
            region_rules.append(
                RegionDetectionConfig(
                    id=rule.id,
                    name=rule.name,
                    enabled=bool(rule.enabled),
                    alert_keywords=region_keywords,
                    rect=rule.rect,
                    ocr_engines=engines,
                    ocr_variant_groups=region_groups,
                    color_match_enabled=bool(rule.color_match_enabled),
                    color_bgr=_hex_to_bgr(rule.color_hex),
                    color_tolerance=max(0, min(100, int(rule.color_tolerance))),
                    cooldown_sec=max(0.0, float(rule.cooldown_sec)),
                    custom_sound_path=rule.custom_sound_path,
                )
            )
        with self._cfg_lock:
            self._cfg = DetectionConfig(
                alert_keywords=keywords,
                template_paths=det.template_paths,
                template_threshold=det.template_threshold,
                ocr_engines=engines,
                ocr_variant_groups=groups,
                region_rules=tuple(region_rules),
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
                    # 0 = 원본(리사이즈 없음). ``0 or 1080`` 이면 원본이 1080으로 깨짐.
                    max_stream_side=int(web.max_side),
                    audio_output_name=web.audio_output or None,
                    ssl_context=ssl_ctx,
                    h264_hardware_encode=web.h264_hardware_encode,
                )
                web_streamer.start()
                self._streamer = web_streamer
                self._stream_audio_error = web_streamer.get_audio_error()
                log_web_event("WebRTC 송출 시작 요청")
            except Exception as exc:  # noqa: BLE001
                log_web_event(f"WebRTC 시작 실패: {exc}", error=True)
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

        dyn_resolver: Callable[[], int] | None = None
        if sys.platform == "win32" and hwnd is not None:
            try:
                from app_platform.windows_capture import (  # type: ignore[import-not-found]
                    is_league_capture_pair_hwnd,
                    resolve_league_capture_hwnd,
                )

                if is_league_capture_pair_hwnd(int(hwnd)):
                    _base_hwnd = int(hwnd)

                    def dyn_resolver() -> int:
                        return int(resolve_league_capture_hwnd(_base_hwnd))

            except Exception:  # noqa: BLE001
                dyn_resolver = None

        try:
            self._capture = CaptureThread(
                monitor_index=int(cap.monitor_index or 1),
                target_fps=float(fps),
                on_frame=_on_frame,
                window_hwnd=hwnd,
                dynamic_hwnd_resolver=dyn_resolver,
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
                result = run_detection_detailed(
                    frame, self.get_cfg(), self._det_stop, self._det_kw_abort
                )
                triggered, reason = result.triggered, result.reason
            except Exception as exc:  # noqa: BLE001
                result = None
                triggered, reason = False, f"감지 오류: {exc}"
                try:
                    from flet_ui.crash_diagnostics import record_exception

                    record_exception(
                        "detection",
                        f"OCR/감지 루프 오류: {exc}",
                        exc=exc,
                    )
                except Exception:
                    pass
            self._last_triggered = triggered
            self._last_reason = reason
            if triggered:
                if result is not None and result.events:
                    for event in result.events:
                        cooldown = (
                            self.settings.detection.cooldown_sec
                            if event.id == "main"
                            else event.cooldown_sec
                        )
                        self._maybe_play_sound(
                            event_id=event.id,
                            cooldown_sec=cooldown,
                            custom_sound_path=event.custom_sound_path,
                        )
                else:
                    self._maybe_play_sound()
            # 포커스 변화 감지는 별도 ``oddments-focus`` 스레드가 200ms 주기로 처리.
            if self._det_cfg_wake.wait(timeout=DETECTION_TICK_MS / 1000.0):
                self._det_cfg_wake.clear()

    # ─── 알림음 ───────────────────────────────────────────

    def _handle_keyword_sound(self) -> None:
        self._maybe_play_sound()

    def _maybe_play_sound(
        self,
        *,
        event_id: str = "main",
        cooldown_sec: float | None = None,
        custom_sound_path: str = "",
    ) -> None:
        if not self._sound_armed:
            return
        key = event_id or "main"
        cooldown = (
            self.settings.detection.cooldown_sec
            if cooldown_sec is None
            else max(0.0, float(cooldown_sec))
        )
        with self._sound_lock:
            now = time.monotonic()
            last = self._last_sound_ts_by_event.get(key, 0.0)
            if now - last < cooldown:
                return
            self._last_sound_ts_by_event[key] = now
            if key == "main":
                self._last_sound_ts = now
        try:
            play_alert_sound(custom_sound_path or None)
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

    def add_theme_listener(self, cb: Callable[[], None]) -> None:
        if cb not in self._theme_listeners:
            self._theme_listeners.append(cb)

    def notify_theme_changed(self) -> None:
        for cb in list(self._theme_listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                traceback.print_exc()

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
            from app_platform.windows_capture import (  # type: ignore[import-not-found]
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
        if ok_down:
            try:
                log_arduino_notice(
                    f"[FOCUS] {edge} → vk={vk} (0x{vk:02X}) 전송 완료"
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

    # ─── 원격 호스트 (WebRTC) ────────────────────────────

    def remote_host_active(self) -> bool:
        return self._remote_host is not None

    def vd_test_display_active(self) -> bool:
        with self._vd_test_lock:
            return self._vd_test_display is not None

    def vd_test_release_in_progress(self) -> bool:
        with self._vd_test_lock:
            return self._vd_test_busy

    def vd_test_create_in_progress(self) -> bool:
        with self._vd_test_lock:
            return self._vd_test_creating

    def vd_test_create_display(self) -> tuple[bool, str]:
        """CGVirtualDisplay(1280×720) 테스트 생성. 호스트 미가동·VD 설정 ON 일 때만.

        Flet ``on_click`` 은 Cocoa 메인에서 돈다. ``create_virtual_display`` 가 메인에서
        런루프를 돌리며 대기하면 해제 시 예약된 PyObjC 정리와 겹쳐 SIGSEGV 가 날 수 있어,
        UI에서는 이 메서드를 **백그라운드 스레드**에서 호출한다(``page_remote``).

        CG ``create_virtual_display`` 호출은 ``_vd_test_lock`` 밖에서 한다. 락을 쥔 채
        WindowServer 가 막히면 UI 쪽 ``run_task`` 가 같은 락에서 대기해 Flet 이
        Working… 처럼 멈춘다.
        """
        if sys.platform != "darwin":
            _vd_test_log("VD테스트 생성 | 거부: macOS 아님", error=True)
            return False, "macOS에서만 사용할 수 있습니다."
        if not self.settings.remote.host.use_virtual_display:
            _vd_test_log(
                "VD테스트 생성 | 거부: 가상 디스플레이만 송출 설정 OFF",
                error=True,
            )
            return (
                False,
                "「가상 디스플레이만 송출」을 켠 뒤에만 테스트할 수 있습니다.",
            )
        if self._remote_host is not None:
            _vd_test_log(
                "VD테스트 생성 | 거부: 원격 호스트 동작 중",
                error=True,
            )
            return False, "원격 호스트를 먼저 중지한 뒤 테스트하세요."
        with self._vd_test_lock:
            if self._vd_test_busy:
                _vd_test_log(
                    "VD테스트 생성 | 거부: _vd_test_busy=True (해제 진행 중)",
                    error=True,
                )
                return (
                    False,
                    "VD 테스트 해제가 진행 중입니다. 완료된 뒤에 다시 시도하세요.",
                )
            if self._vd_test_display is not None:
                _vd_test_log(
                    "VD테스트 생성 | 거부: 이미 테스트 VD 있음 (_vd_test_display 비어 있지 않음)",
                    error=True,
                )
                return False, "이미 테스트 디스플레이가 있습니다. 먼저 해제하세요."
            if self._vd_test_creating:
                _vd_test_log(
                    "VD테스트 생성 | 거부: _vd_test_creating=True (다른 생성 진행 중)",
                    error=True,
                )
                return (
                    False,
                    "VD 테스트 생성이 이미 진행 중입니다. 완료될 때까지 기다려 주세요.",
                )
            self._vd_test_creating = True
            self._vd_test_descriptor_serial += 1
            desc_serial = self._vd_test_descriptor_serial

        _vd_test_log(
            "VD테스트 생성 | 락 해제 후 create_virtual_display 호출 "
            f"serial={desc_serial} thr={threading.current_thread().name!s}"
        )
        vd: object | None = None
        did = 0
        try:
            from app_platform.darwin_virtual_display import (
                create_virtual_display,
            )

            t0 = time.monotonic()
            try:
                vd, did = create_virtual_display(
                    1280,
                    720,
                    refresh_hz=60.0,
                    name=f"Oddments VD Test ({desc_serial})",
                    descriptor_serial=desc_serial,
                    descriptor_product_id=desc_serial,
                )
            except Exception:
                _vd_test_log(
                    "VD테스트 생성 | create_virtual_display 예외 "
                    f"ms={(time.monotonic() - t0) * 1000.0:.1f}",
                    error=True,
                )
                raise
            _vd_test_log(
                "VD테스트 생성 | create_virtual_display 정상 반환 "
                f"cg_id={did} ms={(time.monotonic() - t0) * 1000.0:.1f}"
            )
        except Exception as exc:  # noqa: BLE001
            with self._vd_test_lock:
                self._vd_test_creating = False
            try:
                log_remote_diag(f"VD테스트 생성 실패: {exc!r}", error=True)
            except Exception:
                pass
            return False, f"생성 실패: {exc}"

        with self._vd_test_lock:
            self._vd_test_creating = False
            if self._vd_test_display is not None:
                _vd_test_log(
                    "VD테스트 생성 | 경합: 저장 전에 이미 display 있음 → 신규 vd Py 참조 해제",
                    error=True,
                )
                try:
                    import gc as _gc
                    vd = None
                    _gc.collect()
                except Exception as drop_exc:
                    _vd_test_log(
                        f"VD테스트 생성 | 경합 vd 해제 예외 {drop_exc!r}",
                        error=True,
                    )
                return (
                    False,
                    "다른 생성이 먼저 반영되었습니다. 필요하면 다시 시도하세요.",
                )
            self._vd_test_display = (vd, did)
            self._vd_test_last_released_cg_id = None
        try:
            log_remote_diag(f"VD테스트 생성 | 상태 저장 완료 cg_id={did}")
        except Exception:
            pass
        try:
            log_remote_event(f"VD 테스트: 생성됨 (CG ID {did})")
        except Exception:
            pass
        return (
            True,
            f"가상 디스플레이가 추가되었습니다 (CG ID {did}). "
            "시스템 설정 → 디스플레이에서 확인하세요.",
        )

    def vd_test_release_display(
        self,
        *,
        after_busy: Callable[[], None] | None = None,
    ) -> tuple[bool, str]:
        """테스트용 CGVirtualDisplay 해제.

        Flet ``on_click`` 은 UI(메인) 스레드의 asyncio 콜백에서 돈다. 이 상태에서
        ``run_sync_on_main_dispatch_queue`` 를 호출하면 ``NSThread.isMainThread()`` 가
        참이라 **같은 스택에서 즉시** ``release()`` 가 재진입 실행되어 PyObjC
        ``object_dealloc`` SIGSEGV 가 난다. 해제 본문은 **짧은 전용 스레드**에서 호출해
        ``dispatch_sync`` / NSOperation 메인 경로로만 ``release()`` 를 돌린다.

        호출 스레드가 Cocoa 메인인 채 이 함수 안의 ``Thread.join`` 까지 기다리면,
        메인이 막혀 메인 큐에 예약한 해제 블록이 실행되지 않아 **교착·시간 초과**가 난다.
        UI에서는 이 메서드 호출 전체를 백그라운드 스레드에서 돌리고 완료 후
        ``page.run_task`` 로 스낵·컨트롤을 갱신한다(``page_remote`` 의 VD 테스트 해제).

        ``_vd_test_display`` 는 실제 ``release()`` 가 성공한 뒤에만 비운다. 해제 직전에
        비우면 UI·상태가 OS보다 앞서가거나, CG 온라인 검증 지연만으로 실패로 처리되어
        재생성이 꼬일 수 있다. ``after_busy`` 는 ``_vd_test_busy`` 가 켜진 직후
        (내부 워커가 막히기 전) UI에 「해제 중」을 반영하기 위해 선택적으로 호출한다.

        해제 본체는 전용 워커 스레드에서 실행된다. PyObjC 는 ``alloc().initWithDescriptor_()``
        반환값을 자체적으로 retain 하므로 명시적 ``vd.release()`` 는 retain count 를 2→1 로만
        줄여 dealloc 이 실행되지 않는다. 대신 Python 참조(``_vd_test_display``, ``box``)를
        모두 해제한 뒤 ``gc.collect()`` 를 호출해 PyObjC 가 ``[vd release]`` 를 자동으로
        호출하도록 한다 → retain count 0 → dealloc → WindowServer 등록 해제.
        """
        if sys.platform != "darwin":
            _vd_test_log("VD테스트 해제 | 거부: macOS 아님", error=True)
            return False, "macOS에서만 사용할 수 있습니다."
        if self._remote_host is not None:
            _vd_test_log(
                "VD테스트 해제 | 거부: 원격 호스트 동작 중",
                error=True,
            )
            return False, "원격 호스트를 먼저 중지한 뒤 테스트 해제하세요."
        with self._vd_test_lock:
            if self._vd_test_busy:
                _vd_test_log(
                    "VD테스트 해제 | 거부: _vd_test_busy=True",
                    error=True,
                )
                return False, "이전 해제가 진행 중입니다. 잠시 후 다시 시도하세요."
            t = self._vd_test_display
            if t is None:
                _vd_test_log(
                    "VD테스트 해제 | 거부: _vd_test_display 없음",
                    error=True,
                )
                return False, "해제할 테스트 디스플레이가 없습니다."
            self._vd_test_busy = True

        if after_busy is not None:
            try:
                after_busy()
            except Exception:
                pass

        did = int(t[1])
        box: list[object | None] = [t[0]]
        del t
        _vd_test_log(
            "VD테스트 해제 | 인입(락 밖) "
            f"cg_id={did} thr={threading.current_thread().name!s}"
        )

        outcome: list[tuple[bool, bool, str, str] | None] = [None]

        def _worker() -> None:
            err_note = [""]
            ran_ok = False
            verify_note = ""
            try:
                try:
                    log_remote_diag(
                        "VD테스트 해제 | worker 시작 "
                        f"cg_id={did} thr={threading.current_thread().name!s}"
                    )
                except Exception:
                    pass
                import gc

                from app_platform.darwin_virtual_display import (
                    cg_display_id_still_online,
                )

                # PyObjC 가 alloc+init 반환값을 retain 하고 있으므로 명시적 vd.release()
                # 를 호출하면 retain count 가 2→1 이 될 뿐 dealloc 이 실행되지 않는다.
                # Python 참조를 모두 해제하면 PyObjC 가 자동으로 [vd release] 를 호출해
                # retain count 가 0 이 되어 dealloc → WindowServer 등록 해제가 실행된다.
                _vd_test_log("VD테스트 해제 | Py 참조 해제 시작")
                with self._vd_test_lock:
                    self._vd_test_display = None
                box[0] = None
                gc.collect()
                ran_ok = True
                _vd_test_log("VD테스트 해제 | Py 참조 해제 완료 (gc.collect)")

                st = cg_display_id_still_online(did)
                _vd_test_log(
                    f"VD테스트 해제 | cg_display_id_still_online 즉시 "
                    f"cg_id={did} st={st!r}"
                )
                if st is True:
                    poll_n = 0
                    for _ in range(30):
                        time.sleep(0.1)
                        poll_n += 1
                        s = cg_display_id_still_online(did)
                        if s is not True:
                            _vd_test_log(
                                "VD테스트 해제 | cg_online 폴링 종료 "
                                f"cg_id={did} iters={poll_n} st={s!r}"
                            )
                            break
                    else:
                        _vd_test_log(
                            "VD테스트 해제 | cg_online 폴링 끝까지 온라인 "
                            f"cg_id={did} iters={poll_n}"
                        )
                        verify_note = (
                            "release 후에도 CG 디스플레이가 온라인입니다. "
                            "잠시 후에도 남으면 앱을 종료한 뒤 시스템 설정을 확인하세요."
                        )
                elif st is not True:
                    _vd_test_log(
                        "VD테스트 해제 | cg_online 즉시 오프라인/알수없음 "
                        f"cg_id={did} st={st!r}"
                    )
            except Exception as exc:  # noqa: BLE001
                err_note[0] = str(exc)
                _vd_test_log(f"VD테스트 해제 | worker try 블록 예외 {exc!r}", error=True)

            success_state = bool(ran_ok and not err_note[0])

            finalize_err = ""
            finalize_ok = True
            if ran_ok and success_state:
                try:
                    with self._vd_test_lock:
                        self._vd_test_display = None
                        self._vd_test_busy = False
                        self._vd_test_last_released_cg_id = did
                except Exception as exc:
                    finalize_ok = False
                    finalize_err = (
                        f"상태 정리 실패: {exc!r}. "
                        "잠시 후 「VD 테스트 해제」를 다시 시도하세요."
                    )
                    try:
                        log_remote_diag(f"VD테스트 해제 | worker finalize 예외 {exc!r}")
                    except Exception:
                        pass
                    try:
                        with self._vd_test_lock:
                            self._vd_test_busy = False
                    except Exception:
                        pass
                try:
                    time.sleep(0.18)
                except Exception:
                    pass
            else:
                with self._vd_test_lock:
                    self._vd_test_busy = False
                _vd_test_log(
                    f"VD테스트 해제 | finalize 스킵(ran_ok={ran_ok} success_state="
                    f"{success_state}) err_note={err_note[0]!r}",
                    error=bool(err_note[0]),
                )

            ok_user = bool(success_state and finalize_ok)
            _vd_test_log(
                "VD테스트 해제 | worker 요약 "
                f"ok_user={ok_user} ran_ok={ran_ok} success_state={success_state} "
                f"finalize_ok={finalize_ok} err_exc={err_note[0]!r} "
                f"finalize_err={finalize_err!r} verify_note={verify_note!r}"
            )
            outcome[0] = (
                ran_ok,
                ok_user,
                err_note[0] or finalize_err,
                verify_note,
            )

        th = threading.Thread(
            target=_worker,
            daemon=True,
            name="oddments-vd-test-release",
        )
        th.start()
        th.join(timeout=90.0)
        if th.is_alive():
            _vd_test_log(
                "VD테스트 해제 | oddments-vd-test-release 스레드 join 90초 타임아웃",
                error=True,
            )
            return (
                False,
                "해제 작업이 시간 초과되었습니다. 잠시 후 다시 시도하세요.",
            )
        pack = outcome[0]
        if pack is None:
            return False, "내부 오류: 해제 스레드가 결과를 남기지 않았습니다."
        ran, ok, err_exc, ver_hint = pack

        if not ran:
            return (
                False,
                err_exc or "가상 디스플레이 해제를 완료하지 못했습니다.",
            )

        if ok:
            try:
                log_remote_event("VD 테스트: 해제 완료")
            except Exception:
                pass
            msg = "가상 디스플레이를 해제했습니다."
            if ver_hint:
                msg += " 참고: " + ver_hint
            return True, msg
        parts = [p for p in (err_exc, ver_hint) if p]
        return (
            False,
            " · ".join(parts)
            if parts
            else "release() 실패 또는 알 수 없는 오류입니다.",
        )

    def remote_host_has_start_error(self) -> bool:
        """송출 중이 아니고 마지막 호스트 시작이 실패한 경우(푸터 오류 표시)."""
        return self._remote_host is None and self._remote_host_last_error is not None

    def start_remote_host(self) -> tuple[bool, str | None, str | None]:
        """성공 시 세 번째 값은 macOS 접근성 미허용 안내(있을 때만)."""
        if self._remote_host is not None:
            return True, None, None
        hp = self.settings.remote.host
        acc_hint: str | None = None
        try:
            import sys as _sys

            if _sys.platform == "darwin":
                from app_platform.darwin_accessibility import (
                    accessibility_trusted_after_prompt,
                )

                _trusted, acc_hint = accessibility_trusted_after_prompt()
                if _trusted:
                    acc_hint = None
                else:
                    try:
                        msg = (
                            "원격 호스트: 접근성이 아직 허용되지 않았습니다. "
                            "원격 마우스·키보드 주입은 설정 후에 동작합니다."
                        )
                        if hp.use_virtual_display:
                            msg += (
                                " 가상 디스플레이 모드에서는 물리 화면 봉인(전체 오버레이)도 "
                                "접근성이 허용된 뒤에 표시되는 경우가 많습니다."
                            )
                        log_remote_event(msg)
                    except Exception:
                        pass
                    if hp.use_virtual_display and acc_hint:
                        acc_hint = (
                            acc_hint
                            + " (가상 디스플레이) 물리 화면 봉인은 접근성 허용 후 표시됩니다."
                        )
        except Exception:
            acc_hint = None
        try:
            if _sys.platform == "darwin":
                if self.vd_test_release_in_progress():
                    raise RuntimeError(
                        "VD 테스트 해제가 진행 중입니다. "
                        "완료된 뒤 원격 설정에서 호스트를 시작하세요."
                    )
                if self.vd_test_display_active():
                    raise RuntimeError(
                        "VD 테스트 디스플레이가 켜져 있습니다. "
                        "원격 설정에서 「VD 테스트 해제」를 누른 뒤 호스트를 시작하세요."
                    )
                from app_platform.darwin_compat import remote_host_macos_version_ok

                ok_ver, ver_msg = remote_host_macos_version_ok()
                if not ok_ver:
                    raise RuntimeError(ver_msg)
                if not (hp.auth_token or "").strip():
                    if os.environ.get("MAPLE_REMOTE_ALLOW_NO_PASSWORD") != "1":
                        raise RuntimeError(
                            "맥 호스트는 연결 비밀번호가 필요합니다. "
                            "설정에 입력하거나 개발용으로 환경변수 "
                            "MAPLE_REMOTE_ALLOW_NO_PASSWORD=1 을 사용하세요."
                        )
            vd = bool(hp.use_virtual_display) if _sys.platform == "darwin" else False

            seal_ui_runner = None
            if _sys.platform == "darwin" and vd:

                def seal_ui_runner(fn: Callable[[], None]) -> None:
                    # NSWindow 는 AppKit 메인 스레드에서만 안전. Flet ``run_task`` 코루틴은
                    # 그 보장이 없어 봉인이 안 뜨거나 무시될 수 있음 → 항상 CF 메인 루프로 보냄.
                    from app_platform.darwin_remote_seal import _schedule_on_main

                    _schedule_on_main(fn)

            srv = RemoteHostServer(
                host="0.0.0.0",
                port=hp.listen_port,
                fps=float(hp.stream_fps),
                monitor_index=hp.monitor_index,
                auth_token=hp.auth_token,
                h264_hardware_encode=hp.h264_hardware_encode,
                virtual_display_enabled=vd,
                darwin_audio_device=hp.darwin_audio_input,
                seal_ui_runner=seal_ui_runner,
            )
            srv.start()
            self._remote_host = srv
            self._remote_host_last_error = None
            try:
                log_remote_event(
                    f"원격 호스트: 시작됨 (포트 {hp.listen_port}, 첫 연결 시 캡처)"
                )
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            try:
                log_remote_event(f"원격 호스트: 시작 실패 — {exc}", error=True)
            except Exception:
                pass
            self._remote_host_last_error = str(exc)
            self._notify_state()
            return False, str(exc), None
        self._notify_state()
        return True, None, acc_hint

    def stop_remote_host(self) -> None:
        srv = self._remote_host
        self._remote_host = None
        self._remote_host_last_error = None
        if srv is not None:
            try:
                log_remote_event("원격 호스트: 송출 종료")
            except Exception:
                pass
            try:
                srv.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            from streaming.h264_hw_patch import install_h264_hardware_encoder

            install_h264_hardware_encoder(
                enabled=self.settings.remote.host.h264_hardware_encode
            )
        except Exception:  # noqa: BLE001
            pass
        self._notify_state()

    # ─── 종료 ─────────────────────────────────────────────

    def shutdown(self) -> None:
        try:
            self.save()
        except Exception:  # noqa: BLE001
            pass
        self.stop_remote_host()
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
    "RemoteControlSettings",
    "RemoteHostProfile",
    "RemoteClientProfile",
    "ArduinoSettings",
    "WebStreamSettings",
    "CaptureSettings",
    "DetectionSettings",
    "RegionRuleSettings",
    "OCR_VARIANT_UI_CHOICES",
    "drain_ocr_log_lines",
    "get_ocr_call_total",
    "reset_ocr_log",
    "drain_remote_log_lines",
    "drain_web_log_lines",
    "log_remote_event",
    "log_web_event",
    "reset_remote_log",
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
