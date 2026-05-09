"""
2·3단계 통합: 모니터·창 캡처 스레드 → 미리보기·OCR·감지가 동일 프레임 공유 + 알림음.
실행 시 창 제목·프로세스 표시 이름은 APP_NAME(기본 oddments).

실행 전:
  pip install -r requirements.txt

키워드 OCR: RapidOCR (pip install rapidocr-onnxruntime).
"""

from __future__ import annotations

import sys

# rthook 다음 방어. cv2→numpy 로드 전에 두어야 OpenMP 중복과 onnxruntime 충돌을 줄임.
if getattr(sys, "frozen", False):
    import os

    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    try:
        import bootstrap_onnx

        bootstrap_onnx.apply()
    except Exception:
        import traceback

        try:
            _base = os.path.dirname(os.path.abspath(sys.executable))
            with open(
                os.path.join(_base, "bootstrap_onnx_error.txt"),
                "w",
                encoding="utf-8",
            ) as _f:
                _f.write(traceback.format_exc())
        except OSError:
            pass
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        pass

import ipaddress
import json
import socket
import subprocess
import threading
import traceback
import urllib.error
import urllib.request
from typing import Optional
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox, scrolledtext

import cv2
from PIL import Image, ImageTk

from capture import CaptureThread
from detection.ocr_backends import ENGINE_RAPIDOCR

from app_platform import (
    ensure_pre_gui_init,
    enumerate_windows,
    play_alert_sound,
    stop_queued_alert_sounds,
    window_pick_supported,
)

from detection import (
    DetectionConfig,
    OCR_VARIANT_GROUPS_DISABLED,
    OCR_VARIANT_UI_CHOICES,
    get_overlay_store,
    normalize_ocr_engine,
    ocr_runtime_ok,
    run_detection_with_overlays,
)
from detection.ocr_backends import ENGINE_RAPIDOCR
from preview_render import frame_with_overlays

from detection.ocr_diag import log_ocr_activity
from web_log import log_web_event

if sys.platform == "win32":
    from arduino_serial_bridge import (
        ArduinoKeyBridge,
        bridge_supported,
        clear_arduino_notice_buffer,
        clear_key_bridge_debug_log,
        clear_received_serial_log,
        drain_received_serial_lines,
        drain_key_bridge_debug_lines,
        key_pick_choices,
        list_com_ports,
        log_arduino_notice,
        parse_key_filter_spec,
        set_key_bridge_debug_logging,
        take_arduino_notice_lines,
    )
else:

    class ArduinoKeyBridge:  # type: ignore[no-redef]
        def __init__(self) -> None:
            pass

        def last_error(self) -> None:
            return None

        def traffic_status_error(self) -> None:
            return None

        def is_active(self) -> bool:
            return False

        def start(self, _port: str, _baud: int, _allowed: set[int]) -> bool:
            return False

        def stop(self) -> None:
            pass

        def send_virtual_key(self, _vk: int, _down: bool) -> bool:
            return False

    def bridge_supported() -> bool:
        return False

    def parse_key_filter_spec(_spec: str) -> tuple[set[int], list[str]]:
        return set(), []

    def list_com_ports() -> list[tuple[str, str]]:
        return []

    def key_pick_choices() -> tuple[str, ...]:
        return ()

    def set_key_bridge_debug_logging(_enabled: bool) -> None:
        pass

    def drain_key_bridge_debug_lines(_max_n: int = 200) -> list[str]:
        return []

    def drain_received_serial_lines(_max_n: int = 200) -> list[str]:
        return []

    def clear_received_serial_log() -> None:
        pass

    def clear_key_bridge_debug_log() -> None:
        pass

    def log_arduino_notice(_msg: str) -> None:
        pass

    def take_arduino_notice_lines() -> list[str]:
        return []

    def clear_arduino_notice_buffer() -> None:
        pass


def _app_writable_dir() -> Path:
    """PyInstaller exe 일 때 설정 JSON 은 실행 파일과 같은 폴더에 둔다."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_SETTINGS_FILE = _app_writable_dir() / "oddments_settings.json"
_LEGACY_ALERT_SETTINGS_FILE = _app_writable_dir() / "alert_settings.json"


def _initial_ocr_engines() -> tuple[str, ...]:
    """키워드 OCR은 RapidOCR 만 동작. Tesseract/EasyOCR 체크는 설정 보관용."""
    return (ENGINE_RAPIDOCR,)


# 창 제목·프로세스 표시 이름 (setproctitle, Windows 콘솔 제목)
APP_NAME = "oddments"

# 상단 바(캡처 소스·FPS·송출 시작/중지)가 잘리지 않도록 최소 크기
_MAIN_WIN_MIN_W = 1000
_MAIN_WIN_MIN_H = 480

_TRAFFIC_LAMP_IDLE = "#9ca3af"
_TRAFFIC_LAMP_OK = "#22c55e"
_TRAFFIC_LAMP_ERR = "#dc2626"
_TRAFFIC_LAMP_OUTLINE_IDLE = "#6b7280"
_TRAFFIC_LAMP_OUTLINE_OK = "#15803d"
_TRAFFIC_LAMP_OUTLINE_ERR = "#991b1b"


def _parse_template_paths(raw: str) -> tuple[str, ...]:
    out: list[str] = []
    for part in raw.replace("\r", "").replace("\n", ";").split(";"):
        p = part.strip().strip('"').strip("'")
        if p:
            out.append(p)
    return tuple(out)

# Tesseract: Windows 기본 경로 / macOS Homebrew·Intel 경로 (없으면 PATH)
if sys.platform == "win32":
    _TESS_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    try:
        import os

        import pytesseract

        if os.path.isfile(_TESS_DEFAULT):
            pytesseract.pytesseract.tesseract_cmd = _TESS_DEFAULT
        from tesseract_win_console import apply_pytesseract_windows_no_console

        apply_pytesseract_windows_no_console()
    except ImportError:
        pass
elif sys.platform == "darwin":
    try:
        import os

        import pytesseract

        for _tp in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
            if os.path.isfile(_tp):
                pytesseract.pytesseract.tesseract_cmd = _tp
                break
    except ImportError:
        pass


def _load_json_settings() -> dict:
    for path in (_SETTINGS_FILE, _LEGACY_ALERT_SETTINGS_FILE):
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_json_settings(data: dict) -> None:
    with open(_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _read_window_geometry_str(root: tk.Tk) -> Optional[str]:
    try:
        g = root.winfo_geometry().strip()
        return g if g else None
    except tk.TclError:
        return None


def _set_process_display_name(name: str) -> None:
    try:
        import setproctitle

        setproctitle.setproctitle(name)
    except ImportError:
        pass
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(name)
        except Exception:
            pass


def _win32_foreground_hwnd() -> int | None:
    """Windows 현재 포그라운드 창 HWND를 반환. 실패 시 None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        return hwnd if hwnd > 0 else None
    except Exception:
        return None


class OddmentsApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1000x820")
        self.minsize(_MAIN_WIN_MIN_W, _MAIN_WIN_MIN_H)

        self._cfg = DetectionConfig(
            alert_keywords=("보스",),
            template_paths=(),
            template_threshold=0.80,
            ocr_engines=_initial_ocr_engines(),
            ocr_variant_groups=(),
        )
        self._alert_cooldown_sec = 3.0
        self._detect_every_ms = 1000
        self._preview_scale = 0.5
        # CPU: 미리보기 주기(ms), 동일 캡처 seq 이면 그리기 생략
        self._preview_interval_ms = 66
        self._preview_last_frame_seq = -1
        # CPU: 폴링에서 설정 동기화·엔진 상태 검사 간격
        self._ui_cfg_dirty = True
        self._last_cfg_poll_sync = 0.0

        self._thread: CaptureThread | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._running = True
        self._picked_hwnd: int | None = None
        self._picked_summary: str = ""
        self._was_triggered_last: bool = False
        self._bg_join_thread: threading.Thread | None = None

        self._det_lock = threading.Lock()
        self._det_stop = threading.Event()
        self._det_cfg_wake = threading.Event()
        self._det_kw_abort = threading.Event()
        self._det_thread: threading.Thread | None = None
        self._last_det_triggered = False
        self._last_det_reason = ""
        self._sound_armed = False
        self._stream_status_text = ""
        self._web_stream_enabled_var = tk.BooleanVar(value=False)
        self._web_stream_port_var = tk.StringVar(value="8787")
        self._web_stream_max_side_var = tk.StringVar(value="1080")
        self._web_stream_url_var = tk.StringVar(value="")
        self._web_viewer_count_var = tk.StringVar(value="현재 시청 연결: 0명")
        self._web_audio_status_var = tk.StringVar(value="")
        self._web_stream_audio_output_var = tk.StringVar(value="")
        self._web_stream_https_var = tk.BooleanVar(value=False)
        self._web_stream_ssl_cert_var = tk.StringVar(value="")
        self._web_stream_ssl_key_var = tk.StringVar(value="")
        self._web_audio_output_combo: ttk.Combobox | None = None
        self._web_audio_output_internal: list[str] = []
        self._web_streamer = None
        self._public_ip_cache: str | None = None
        self._public_ip_cache_ts = 0.0
        self._traffic_err_last: dict[str, str] = {}
        self._web_stream_audio_err: str | None = None

        self._ocr_settings_win: tk.Toplevel | None = None
        self._ocr_log_poll_after: str | None = None
        self._ocr_log_widget: scrolledtext.ScrolledText | None = None
        self._ocr_log_stats_var: tk.StringVar | None = None
        self._ocr_log_autoscroll_var = tk.BooleanVar(value=True)

        self._web_settings_win: tk.Toplevel | None = None
        self._web_log_poll_after: str | None = None
        self._web_log_widget: scrolledtext.ScrolledText | None = None
        self._web_log_autoscroll_var = tk.BooleanVar(value=True)

        self._init_detection_vars()
        self._ocr_tpl_disabled_widgets: list[tk.Widget] = []
        self._keyword_ocr_var.trace_add(
            "write", lambda *_a: self._apply_keyword_ocr_tpl_ui_state()
        )
        self._arduino_bridge = ArduinoKeyBridge()
        self._arduino_port_var = tk.StringVar(value="COM3")
        self._arduino_baud_var = tk.StringVar(value="115200")
        self._arduino_keys_var = tk.StringVar(value="F1,F2,F3")
        self._arduino_focus_event_enabled_var = tk.BooleanVar(value=False)
        self._arduino_focus_gain_key_var = tk.StringVar(value="F8")
        self._arduino_focus_loss_key_var = tk.StringVar(value="F8")
        self._arduino_status_var = tk.StringVar(value="")
        self._arduino_summary_var = tk.StringVar(value="")
        self._last_window_focus_state: bool | None = None

        self._build_ui()
        self._register_ui_cfg_dirty_traces()
        self._apply_settings_dict(_load_json_settings())
        self.after_idle(self._clamp_main_window_geometry_to_minimum)
        if sys.platform == "win32":
            self.after(100, self._sync_arduino_bridge)

        self._alert_sound_lock = threading.Lock()
        self._last_alert_sound_ts = 0.0

        def _on_ocr_kw_alert_sound() -> None:
            """워커에서 호출됨 — 쿨다운·재생은 UI 스레드에서만 처리."""
            try:
                self.after(0, self._try_alert_sound_from_ocr)
            except tk.TclError:
                pass

        from detection.ocr_diag import set_ocr_keyword_alert_sound_handler

        set_ocr_keyword_alert_sound_handler(_on_ocr_kw_alert_sound)

        self.title(APP_NAME)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(self._preview_interval_ms, self._tick_preview)
        self.after(50, self._poll_detection_ui)
        if sys.platform == "darwin":
            # 번들/터미널 실행 시 창이 뒤에 깔리는 경우가 많아 한 번 앞으로 올림
            self.after(80, self._mac_bring_to_front_once)

    def _mac_bring_to_front_once(self) -> None:
        try:
            self.update_idletasks()
            self.lift()
            self.attributes("-topmost", True)
            self.after(250, self._mac_clear_topmost)
        except tk.TclError:
            pass

    def _mac_clear_topmost(self) -> None:
        try:
            self.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _clamp_main_window_geometry_to_minimum(self) -> None:
        """저장된 geometry가 너무 작으면 최소 가로·세로로 맞춤 (상단 버튼이 사라지지 않게)."""
        try:
            self.update_idletasks()
            w = int(self.winfo_width())
            h = int(self.winfo_height())
        except (tk.TclError, ValueError):
            return
        if w <= 1 or h <= 1:
            return
        nw = max(w, _MAIN_WIN_MIN_W)
        nh = max(h, _MAIN_WIN_MIN_H)
        if nw == w and nh == h:
            return
        try:
            self.geometry(f"{nw}x{nh}")
        except tk.TclError:
            pass

    def _init_detection_vars(self) -> None:
        """OCR·감지 설정(별도 창)과 공유하는 Tk 변수."""
        self._kw_var = tk.StringVar(value="보스,레드")
        self._tpl_var = tk.StringVar(value="")
        self._th_var = tk.StringVar(value="0.80")
        self._cd_var = tk.StringVar(value="3")
        self._show_overlay_var = tk.BooleanVar(value=True)
        self._keyword_ocr_var = tk.BooleanVar(value=True)
        self._ocr_variant_group_vars = {
            vid: tk.BooleanVar(value=True) for vid, _ in OCR_VARIANT_UI_CHOICES
        }

    def _populate_ocr_settings(self, bot: tk.Widget) -> None:
        """OCR·감지 설정 창 본문."""
        r1 = ttk.Frame(bot)
        r1.pack(fill=tk.X, pady=2)
        ttk.Label(r1, text="알림 키워드(쉼표 구분)").pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self._kw_var, width=40).pack(
            side=tk.LEFT, padx=8, fill=tk.X, expand=True
        )

        r1b = ttk.Frame(bot)
        r1b.pack(fill=tk.X, pady=2)
        ttk.Checkbutton(
            r1b,
            text="키워드 OCR 사용 (RapidOCR)",
            variable=self._keyword_ocr_var,
            command=self._on_detection_cfg_changed,
        ).pack(side=tk.LEFT)

        r1c = ttk.LabelFrame(bot, text="OCR 전처리 변형 (선택한 것만 사용)", padding=(6, 4))
        r1c.pack(fill=tk.X, pady=4)
        ttk.Label(
            r1c,
            text="전부 체크이면 모든 변형을 사용합니다. "
            "체크가 하나도 없으면 키워드 OCR(전처리 변형)은 호출하지 않습니다.",
            foreground="gray",
            font=("", 8),
        ).pack(anchor=tk.W)
        vgrid = ttk.Frame(r1c)
        vgrid.pack(fill=tk.X, pady=(4, 0))
        _cols = 3
        for i, (vid, vlabel) in enumerate(OCR_VARIANT_UI_CHOICES):
            var = self._ocr_variant_group_vars[vid]
            rr, cc = divmod(i, _cols)
            ttk.Checkbutton(
                vgrid,
                text=vlabel,
                variable=var,
                command=self._on_detection_cfg_changed,
            ).grid(row=rr, column=cc, sticky=tk.W, padx=(0, 12), pady=2)

        self._ocr_tpl_disabled_widgets.clear()

        r3 = ttk.Frame(bot)
        r3.pack(fill=tk.X, pady=2)
        rl3 = ttk.Label(r3, text="템플릿 경로")
        rl3.pack(side=tk.LEFT, anchor=tk.N)
        self._ocr_tpl_disabled_widgets.append(rl3)
        tpl_col = ttk.Frame(r3)
        tpl_col.pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)
        tpl_ent = ttk.Entry(tpl_col, textvariable=self._tpl_var)
        tpl_ent.pack(side=tk.TOP, fill=tk.X, expand=True)
        self._ocr_tpl_disabled_widgets.append(tpl_ent)
        tpl_hint = ttk.Label(
            tpl_col,
            text="여러 장: 세미콜론(;)으로 구분 · 미리보기(캡처)와 같은 해상도로 잘라 저장",
            foreground="gray",
            font=("", 8),
        )
        tpl_hint.pack(side=tk.TOP, anchor=tk.W)
        self._ocr_tpl_disabled_widgets.append(tpl_hint)
        tpl_btns = ttk.Frame(r3)
        tpl_btns.pack(side=tk.LEFT)
        tpl_add_btn = ttk.Button(tpl_btns, text="추가…", command=self._browse_template)
        tpl_add_btn.pack(side=tk.TOP, pady=1)
        self._ocr_tpl_disabled_widgets.append(tpl_add_btn)
        tpl_clr_btn = ttk.Button(
            tpl_btns, text="비우기", command=lambda: self._tpl_var.set("")
        )
        tpl_clr_btn.pack(side=tk.TOP, pady=1)
        self._ocr_tpl_disabled_widgets.append(tpl_clr_btn)

        r4 = ttk.Frame(bot)
        r4.pack(fill=tk.X, pady=4)
        rl4 = ttk.Label(r4, text="매칭 임계값")
        rl4.pack(side=tk.LEFT)
        self._ocr_tpl_disabled_widgets.append(rl4)
        th_spin = ttk.Spinbox(
            r4, from_=0.5, to=0.99, increment=0.01, width=6, textvariable=self._th_var
        )
        th_spin.pack(side=tk.LEFT, padx=8)
        self._ocr_tpl_disabled_widgets.append(th_spin)
        ttk.Label(r4, text="알림 쿨다운(초)").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Spinbox(r4, from_=1, to=60, width=4, textvariable=self._cd_var).pack(
            side=tk.LEFT, padx=8
        )

        r5 = ttk.Frame(bot)
        r5.pack(fill=tk.X, pady=4)
        ttk.Checkbutton(
            r5,
            text="감지 영역 박스 표시 (영역마다 다른 색 테두리)",
            variable=self._show_overlay_var,
        ).pack(side=tk.LEFT)

        self._apply_keyword_ocr_tpl_ui_state()

    def _apply_keyword_ocr_tpl_ui_state(self) -> None:
        """키워드 OCR 미사용 시 템플릿 입력·임계값 위젯 비활성화."""
        if not self._ocr_tpl_disabled_widgets:
            return
        on = bool(self._keyword_ocr_var.get())
        st = tk.NORMAL if on else tk.DISABLED
        for w in self._ocr_tpl_disabled_widgets:
            try:
                w.configure(state=st)
            except tk.TclError:
                pass

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        src = ttk.LabelFrame(top, text="캡처·송출 소스", padding=(6, 4))
        src_main_row = ttk.Frame(src)
        src_main_row.pack(fill=tk.X, anchor=tk.W)

        self._src_mode = tk.StringVar(value="monitor")
        if window_pick_supported():
            ttk.Radiobutton(
                src_main_row,
                text="모니터 전체",
                variable=self._src_mode,
                value="monitor",
                command=self._on_src_mode_change,
            ).pack(side=tk.LEFT, padx=(0, 8))
            ttk.Radiobutton(
                src_main_row,
                text="프로세스(창) 지정",
                variable=self._src_mode,
                value="window",
                command=self._on_src_mode_change,
            ).pack(side=tk.LEFT, padx=(0, 12))
        else:
            ttk.Label(
                src_main_row,
                text="모니터만 지원 (창 선택은 Windows·macOS에서 가능)",
            ).pack(side=tk.LEFT)

        self._mon_label = ttk.Label(src_main_row, text="모니터 #")
        self._mon_label.pack(side=tk.LEFT)
        self._mon_var = tk.StringVar(value="1")
        self._mon_spin = ttk.Spinbox(
            src_main_row, from_=1, to=8, width=4, textvariable=self._mon_var
        )
        self._mon_spin.pack(side=tk.LEFT, padx=(4, 8))

        self._pick_btn = ttk.Button(
            src_main_row, text="창 선택…", command=self._open_window_picker, state=tk.DISABLED
        )
        self._pick_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._pick_info_var = tk.StringVar(value="")
        self._pick_info = ttk.Label(src_main_row, textvariable=self._pick_info_var, width=42)
        self._pick_info.pack(side=tk.LEFT, padx=(0, 0))

        # 오른쪽부터 pack: 맨 먼저 pack한 쪽이 화면 오른쪽 끝 → [소스…][캡처 FPS][송출 버튼]
        btn_fr = ttk.Frame(top, padding=(8, 0))
        self._btn_start = ttk.Button(btn_fr, text="송출 시작", command=self._start)
        self._btn_start.pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_fr, text="중지", command=self._stop).pack(side=tk.LEFT, padx=2)
        btn_fr.pack(side=tk.RIGHT)

        fps_fr = ttk.Frame(top, padding=(8, 0))
        ttk.Label(fps_fr, text="캡처 FPS").pack(side=tk.LEFT)
        self._fps_var = tk.StringVar(value="20")
        ttk.Spinbox(fps_fr, from_=5, to=60, width=4, textvariable=self._fps_var).pack(
            side=tk.LEFT, padx=(4, 12)
        )
        fps_fr.pack(side=tk.RIGHT)

        src.pack(side=tk.LEFT, fill=tk.X, expand=True)

        if window_pick_supported():
            self._on_src_mode_change()

        mid = ttk.Frame(self, padding=4)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self._canvas = tk.Canvas(mid, bg="#222", highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)

        set_fr = ttk.LabelFrame(self, text="설정", padding=(8, 6))
        set_fr.pack(fill=tk.X, padx=8, pady=(0, 2))
        ttk.Button(set_fr, text="OCR·감지 설정…", command=self._open_ocr_settings_window).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        if sys.platform == "win32":
            ttk.Button(
                set_fr, text="아두이노 설정…", command=self._open_arduino_settings_window
            ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            set_fr, text="웹 화면 송출 설정…", command=self._open_web_stream_settings_window
        ).pack(side=tk.LEFT, padx=(0, 8))

        self._status = ttk.Label(self, text="", anchor=tk.W, wraplength=900)
        self._build_traffic_bar()

    def _set_main_status_line(self, text: str, *, error: bool = False) -> None:
        """메인 상태 한 줄. 비우면 위젯을 pack 해제해 빈 줄을 두지 않음."""
        t = (text or "").strip()
        if not t:
            try:
                self._status.pack_forget()
            except tk.TclError:
                pass
            return
        fg = "#a63" if error else ""
        try:
            self._status.configure(text=t, foreground=fg)
        except tk.TclError:
            return
        try:
            if self._status.winfo_manager() == "":
                self._status.pack(
                    fill=tk.X, padx=10, pady=(0, 2), before=self._traffic_fr
                )
        except tk.TclError:
            pass

    def _build_traffic_bar(self) -> None:
        """하단 OCR / Arduino / Web 송출 상태 신호등."""
        self._traffic_fr = ttk.Frame(self)
        self._traffic_fr.pack(fill=tk.X, padx=10, pady=(4, 8))
        row = ttk.Frame(self._traffic_fr)
        row.pack(fill=tk.X)
        try:
            canvas_bg = self.cget("background")
        except tk.TclError:
            canvas_bg = "#f0f0f0"
        self._traffic_canvas_bg = canvas_bg
        self._traffic_lamp_items: dict[str, int] = {}
        self._traffic_lamp_canvases: dict[str, tk.Canvas] = {}

        def add_cell(title: str, key: str, pad_left: int) -> None:
            fr = ttk.Frame(row)
            fr.pack(side=tk.LEFT, padx=(pad_left, 18))
            try:
                fr.configure(cursor="hand2")
            except tk.TclError:
                pass
            lbl = ttk.Label(fr, text=title)
            lbl.pack(side=tk.LEFT, padx=(0, 4))
            try:
                lbl.configure(cursor="hand2")
            except tk.TclError:
                pass
            cv = tk.Canvas(
                fr,
                width=22,
                height=22,
                highlightthickness=0,
                bd=0,
                bg=canvas_bg,
                cursor="hand2",
            )
            cv.pack(side=tk.LEFT, padx=(0, 0))
            oid = cv.create_oval(
                5,
                5,
                17,
                17,
                fill=_TRAFFIC_LAMP_IDLE,
                outline=_TRAFFIC_LAMP_OUTLINE_IDLE,
            )
            self._traffic_lamp_items[key] = oid
            self._traffic_lamp_canvases[key] = cv

            def _click(_e: object, k: str = key) -> None:
                self._on_traffic_lamp_click(k)

            for w in (fr, lbl, cv):
                w.bind("<Button-1>", _click)

        add_cell("OCR:", "ocr", 0)
        add_cell("Arduino:", "arduino", 0)
        add_cell("Web송출:", "web", 0)

    def _traffic_apply_channel(self, key: str, state: str) -> None:
        cv = self._traffic_lamp_canvases[key]
        oid = self._traffic_lamp_items[key]
        if state == "ok":
            cv.itemconfig(oid, fill=_TRAFFIC_LAMP_OK, outline=_TRAFFIC_LAMP_OUTLINE_OK)
        elif state == "err":
            cv.itemconfig(oid, fill=_TRAFFIC_LAMP_ERR, outline=_TRAFFIC_LAMP_OUTLINE_ERR)
        else:
            cv.itemconfig(
                oid, fill=_TRAFFIC_LAMP_IDLE, outline=_TRAFFIC_LAMP_OUTLINE_IDLE
            )

    def _compute_traffic_states(
        self,
    ) -> tuple[tuple[str, str], tuple[str, str], tuple[str, str]]:
        """((ocr_st, o_err), (arduino_st, a_err), (web_st, w_err)) — st 는 idle|ok|err."""
        try:
            if self._keyword_ocr_var.get():
                o_ok, o_msg = ocr_runtime_ok(ENGINE_RAPIDOCR)
                o_st = "ok" if o_ok else "err"
                o_err = "" if o_ok else (o_msg or "RapidOCR 사용 불가")
            else:
                o_st, o_err = "idle", ""
        except tk.TclError:
            o_st, o_err = "idle", ""

        if sys.platform != "win32":
            a_st, a_err = "idle", ""
        elif not bridge_supported():
            a_st, a_err = "err", "pynput·pyserial 필요: pip install pynput pyserial"
        else:
            te = self._arduino_bridge.traffic_status_error()
            if self._arduino_bridge.is_active():
                if te:
                    a_st, a_err = "err", str(te)
                else:
                    a_st, a_err = "ok", ""
            elif te:
                a_st, a_err = "err", str(te)
            else:
                a_st, a_err = "idle", ""

        try:
            web_on = bool(self._web_stream_enabled_var.get())
        except tk.TclError:
            web_on = False
        streaming = self._thread is not None
        ws = self._web_streamer
        # 미사용: 웹 송출 체크만 꺼진 경우. 「사용」은 옵션 켜짐이면 송출 시작 전에도 초록(설정 반영).
        if not web_on:
            w_st, w_err = "idle", ""
        elif streaming and ws is not None:
            ae = ws.get_audio_error()
            if ae:
                w_st, w_err = "err", str(ae)
            else:
                w_st, w_err = "ok", ""
        else:
            w_st, w_err = "ok", ""

        return (o_st, o_err), (a_st, a_err), (w_st, w_err)

    def _on_traffic_lamp_click(self, channel: str) -> None:
        (o_st, _o_e), (a_st, _a_e), (w_st, _w_e) = self._compute_traffic_states()
        if channel == "ocr":
            if o_st == "err":
                self._open_ocr_settings_window()
            elif o_st == "idle":
                self._keyword_ocr_var.set(True)
                self._on_detection_cfg_changed()
                self._persist_app_settings(show_error_dialog=False)
            elif o_st == "ok":
                self._keyword_ocr_var.set(False)
                self._on_detection_cfg_changed()
                self._persist_app_settings(show_error_dialog=False)
        elif channel == "arduino":
            if sys.platform != "win32":
                return
            if a_st == "err":
                self._open_arduino_settings_window()
            elif a_st == "idle":
                self._arduino_apply_bridge_connection()
                self._persist_app_settings(show_error_dialog=False)
            elif a_st == "ok":
                self._arduino_bridge.stop()
                self._arduino_set_idle_disconnected()
                self._persist_app_settings(show_error_dialog=False)
        elif channel == "web":
            if w_st == "err":
                self._open_web_stream_settings_window()
            elif w_st == "idle":
                self._web_stream_enabled_var.set(True)
                self._persist_app_settings(show_error_dialog=False)
            elif w_st == "ok":
                self._web_stream_enabled_var.set(False)
                self._persist_app_settings(show_error_dialog=False)
        self._refresh_traffic_status()

    def _traffic_emit_error_log(self, channel: str, message: str) -> None:
        msg = (message or "").strip()
        if not msg:
            return
        prev = self._traffic_err_last.get(channel)
        if prev == msg:
            return
        self._traffic_err_last[channel] = msg
        if channel == "ocr":
            log_ocr_activity("상태", "traffic", msg, truncate_detail=False)
        elif channel == "arduino":
            log_arduino_notice(msg)
        elif channel == "web":
            log_web_event(f"[상태] {msg}")

    def _refresh_traffic_status(self) -> None:
        if not self._running:
            return
        (o_st, o_err), (a_st, a_err), (w_st, w_err) = self._compute_traffic_states()
        try:
            web_on = bool(self._web_stream_enabled_var.get())
        except tk.TclError:
            web_on = False
        streaming = self._thread is not None
        ws = self._web_streamer
        if not web_on or not streaming or ws is None:
            self._web_stream_audio_err = None
        else:
            ae = ws.get_audio_error()
            if ae:
                self._web_stream_audio_err = ae
            else:
                self._web_stream_audio_err = None

        for ch, st, msg in (
            ("ocr", o_st, o_err),
            ("arduino", a_st, a_err),
            ("web", w_st, w_err),
        ):
            if st == "err" and msg:
                self._traffic_emit_error_log(ch, msg)
            else:
                self._traffic_err_last.pop(ch, None)

        self._traffic_apply_channel("ocr", o_st)
        self._traffic_apply_channel("arduino", a_st)
        self._traffic_apply_channel("web", w_st)

    def _register_ui_cfg_dirty_traces(self) -> None:
        """키워드·템플릿·임계값·쿨다운 변경 시에만 감지 설정을 다시 맞추도록 표시."""

        def _mark(*_args: object) -> None:
            self._ui_cfg_dirty = True

        for var in (
            self._kw_var,
            self._tpl_var,
            self._th_var,
            self._cd_var,
            self._show_overlay_var,
        ):
            var.trace_add("write", _mark)

    def _effective_capture_fps(self) -> float:
        """
        사용자가 지정한 캡처 FPS를 그대로 사용한다.
        (감지 주기와 독립; 캡처 스레드는 입력 FPS 기준으로 동작)
        """
        try:
            u = float(self._fps_var.get())
        except ValueError:
            u = 20.0
        u = max(5.0, min(60.0, u))
        return u

    def _web_stream_url_scheme(self) -> str:
        """표시용 URL 스킴. 송출 중이면 실제 서버 TLS 여부, 아니면 저장된 HTTPS 설정을 반영한다."""
        ws = self._web_streamer
        if ws is not None:
            return "https" if ws.uses_tls else "http"
        if self._web_stream_https_var.get():
            c = self._web_stream_ssl_cert_var.get().strip()
            k = self._web_stream_ssl_key_var.get().strip()
            if c and k:
                try:
                    if Path(c).expanduser().is_file() and Path(k).expanduser().is_file():
                        return "https"
                except OSError:
                    pass
        return "http"

    # 공인 IP 조회가 실패했을 때 사용할 LAN URL을 계산한다.
    def _current_web_stream_url(self, port: int) -> str:
        scheme = self._web_stream_url_scheme()
        host = "127.0.0.1"
        try:
            host = socket.gethostbyname(socket.gethostname())
        except Exception:
            pass
        return f"{scheme}://{host}:{port}/"

    # 공인 IPv4만 허용: 응답/HTML 혼입·IPv6·사설대역 오탐을 줄인다.
    def _parse_public_ipv4_from_text(self, raw: str) -> str | None:
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

    # 여러 서비스에서 IPv4 공인 주소를 조회한다(연결이 IPv6로 나가면 ifconfig 등이 IPv6만 줄 수 있음).
    def _fetch_public_ipv4_wan(self) -> str | None:
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
                        ip = str(data.get("ip", "")).strip()
                        body = ip
                    except (json.JSONDecodeError, TypeError):
                        continue
                got = self._parse_public_ipv4_from_text(body)
                if got:
                    return got
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
        return None

    # 외부 접근 URL 표기를 위해 공인 IPv4를 조회하고 짧게 캐시한다.
    def _current_public_web_stream_url(self, port: int) -> str:
        scheme = self._web_stream_url_scheme()
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
        self._public_ip_cache = None
        self._public_ip_cache_ts = 0.0
        return self._current_web_stream_url(port)

    # 상단 버튼에서 웹 송출 URL을 클립보드로 복사한다.
    def _copy_web_stream_url(self) -> None:
        try:
            port = int(str(self._web_stream_port_var.get()).strip())
        except ValueError:
            messagebox.showerror("오류", "웹 송출 포트를 숫자로 입력하세요.")
            return
        if port < 1 or port > 65535:
            messagebox.showerror("오류", "웹 송출 포트는 1~65535 범위여야 합니다.")
            return
        url = self._web_stream_url_var.get().strip() or self._current_public_web_stream_url(
            port
        )
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.update_idletasks()
        except tk.TclError:
            messagebox.showerror("오류", "클립보드에 URL을 복사하지 못했습니다.")
            return
        self._set_main_status_line(f"URL 복사됨: {url}")

    def _on_src_mode_change(self) -> None:
        if not window_pick_supported():
            return
        if self._src_mode.get() == "monitor":
            self._mon_spin.configure(state="normal")
            self._pick_btn.configure(state="disabled")
            self._pick_info_var.set("")
            self._picked_hwnd = None
        else:
            self._mon_spin.configure(state="disabled")
            self._pick_btn.configure(state="normal")

    def _open_window_picker(self) -> None:
        if not window_pick_supported():
            return
        dlg = tk.Toplevel(self)
        dlg.title("캡처할 창 선택")
        dlg.geometry("760x520")
        dlg.transient(self)

        hint = ttk.Label(
            dlg,
            text="프로세스(실행 파일)과 창 제목으로 구분됩니다. 게임 창을 선택한 뒤 「확인」을 누르세요.",
            wraplength=720,
        )
        hint.pack(fill=tk.X, padx=8, pady=(8, 4))

        tree_frame = ttk.Frame(dlg, padding=6)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        cols = ("process", "title")
        tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", selectmode="browse"
        )
        tree.heading("process", text="프로세스")
        tree.heading("title", text="창 제목")
        tree.column("process", width=180, stretch=False)
        tree.column("title", width=520, stretch=True)
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        def refill() -> None:
            for iid in tree.get_children():
                tree.delete(iid)
            try:
                entries = enumerate_windows()
            except OSError as e:
                messagebox.showerror("오류", str(e), parent=dlg)
                return
            for ent in entries:
                tree.insert("", tk.END, iid=str(ent.hwnd), values=(ent.process_name, ent.title))

        refill()

        bar = ttk.Frame(dlg, padding=6)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="목록 새로고침", command=refill).pack(side=tk.LEFT, padx=2)

        def apply_selection() -> None:
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("선택", "목록에서 창을 한 줄 선택하세요.", parent=dlg)
                return
            hwnd = int(sel[0])
            item = tree.item(sel[0])
            vals = item.get("values") or []
            proc = str(vals[0]) if len(vals) > 0 else ""
            title = str(vals[1]) if len(vals) > 1 else ""
            self._picked_hwnd = hwnd
            summary = f"{proc} | {title}"
            if len(summary) > 45:
                summary = summary[:42] + "…"
            self._pick_info_var.set(summary)
            dlg.destroy()

        ttk.Button(bar, text="취소", command=dlg.destroy).pack(side=tk.RIGHT, padx=2)
        ttk.Button(bar, text="확인", command=apply_selection).pack(side=tk.RIGHT, padx=2)
        tree.bind("<Double-1>", lambda _e: apply_selection())

    def _apply_settings_dict(self, d: dict) -> None:
        if not d:
            return
        if "keywords" in d:
            self._kw_var.set(str(d["keywords"]))
        if "template_paths" in d and isinstance(d["template_paths"], list):
            self._tpl_var.set(";".join(str(p) for p in d["template_paths"] if p))
        elif "template_path" in d and d["template_path"]:
            self._tpl_var.set(str(d["template_path"]))
        if "template_threshold" in d:
            self._th_var.set(str(d["template_threshold"]))
        if "ocr_engines" in d and isinstance(d["ocr_engines"], list):
            lst = d["ocr_engines"]
            if not lst:
                self._keyword_ocr_var.set(False)
            else:
                want = {
                    n
                    for x in lst
                    if x and str(x).strip()
                    for n in (normalize_ocr_engine(str(x)),)
                    if n
                }
                legacy = any(
                    str(x).strip().lower()
                    in (
                        "tesseract",
                        "easyocr",
                        "tess",
                        "easy",
                        "rapidocr",
                        "rapid",
                    )
                    for x in lst
                    if x and str(x).strip()
                )
                self._keyword_ocr_var.set(bool(want) or legacy)
        elif "ocr_engine" in d and str(d["ocr_engine"]).strip():
            n = normalize_ocr_engine(str(d["ocr_engine"]))
            legacy = str(d["ocr_engine"]).strip().lower() in (
                "tesseract",
                "easyocr",
                "tess",
                "easy",
            )
            self._keyword_ocr_var.set(bool(n) or legacy)
        if "cooldown_sec" in d:
            self._cd_var.set(str(d["cooldown_sec"]))
        if "show_overlay" in d:
            self._show_overlay_var.set(bool(d["show_overlay"]))
        if "ocr_variant_groups" in d:
            v = d["ocr_variant_groups"]
            if not isinstance(v, list):
                v = []
            if not v:
                for vid, _ in OCR_VARIANT_UI_CHOICES:
                    self._ocr_variant_group_vars[vid].set(True)
            else:
                want = {str(x) for x in v}
                for vid, _ in OCR_VARIANT_UI_CHOICES:
                    self._ocr_variant_group_vars[vid].set(vid in want)
        if "capture_fps" in d:
            try:
                f = float(d["capture_fps"])
                f = max(5, min(60, int(round(f))))
                self._fps_var.set(str(f))
            except (TypeError, ValueError):
                pass
        if "web_stream_enabled" in d:
            self._web_stream_enabled_var.set(bool(d["web_stream_enabled"]))
        if "web_stream_port" in d:
            try:
                p = int(d["web_stream_port"])
                if 1 <= p <= 65535:
                    self._web_stream_port_var.set(str(p))
            except (TypeError, ValueError):
                pass
        if "web_stream_max_side" in d:
            try:
                ms = int(d["web_stream_max_side"])
                if 0 <= ms <= 4096:
                    self._web_stream_max_side_var.set(str(ms))
            except (TypeError, ValueError):
                pass
        if "web_stream_audio_output" in d and isinstance(
            d["web_stream_audio_output"], str
        ):
            self._web_stream_audio_output_var.set(str(d["web_stream_audio_output"]))
        if "web_stream_https" in d:
            self._web_stream_https_var.set(bool(d["web_stream_https"]))
        if "web_stream_ssl_cert" in d and isinstance(d["web_stream_ssl_cert"], str):
            self._web_stream_ssl_cert_var.set(str(d["web_stream_ssl_cert"]))
        if "web_stream_ssl_key" in d and isinstance(d["web_stream_ssl_key"], str):
            self._web_stream_ssl_key_var.set(str(d["web_stream_ssl_key"]))
        if "capture_source_mode" in d and str(d["capture_source_mode"]).strip():
            v = str(d["capture_source_mode"]).strip().lower()
            if v == "stream":
                v = "monitor"
            if v in ("monitor", "window"):
                if not window_pick_supported() and v == "window":
                    v = "monitor"
                self._src_mode.set(v)
        if window_pick_supported():
            self._on_src_mode_change()
        if sys.platform == "win32" and "arduino_serial" in d and isinstance(
            d["arduino_serial"], dict
        ):
            ad = d["arduino_serial"]
            if ad.get("port"):
                self._arduino_port_var.set(str(ad["port"]))
            if ad.get("baud") is not None:
                self._arduino_baud_var.set(str(ad["baud"]))
            if ad.get("keys"):
                self._arduino_keys_var.set(str(ad["keys"]))
            if ad.get("focus_event_enabled") is not None:
                self._arduino_focus_event_enabled_var.set(
                    bool(ad.get("focus_event_enabled"))
                )
            legacy_focus = ad.get("focus_event_key")
            gain_k = ad.get("focus_event_key_gain")
            loss_k = ad.get("focus_event_key_loss")
            if gain_k:
                self._arduino_focus_gain_key_var.set(str(gain_k))
            elif legacy_focus:
                self._arduino_focus_gain_key_var.set(str(legacy_focus))
            if loss_k:
                self._arduino_focus_loss_key_var.set(str(loss_k))
            elif legacy_focus:
                self._arduino_focus_loss_key_var.set(str(legacy_focus))
        if "window_geometry" in d:
            g = str(d["window_geometry"]).strip()
            if g:
                try:
                    self.geometry(g)
                except tk.TclError:
                    pass

    def _persist_app_settings(self, *, show_error_dialog: bool = True) -> bool:
        """감지·캡처·아두이노 설정을 JSON에 기록. 창 닫기 등에서 호출."""
        self._sync_cfg_from_ui()

        def _f(var: tk.StringVar, default: float) -> float:
            try:
                return float(var.get())
            except ValueError:
                return default

        og = self._ocr_variant_groups_for_cfg()
        cap_fps = max(5, min(60, int(round(_f(self._fps_var, 20.0)))))
        self._fps_var.set(str(cap_fps))
        try:
            web_port = int(str(self._web_stream_port_var.get()).strip())
        except ValueError:
            web_port = 8787
        web_port = max(1, min(65535, web_port))
        self._web_stream_port_var.set(str(web_port))
        try:
            web_ms = int(str(self._web_stream_max_side_var.get()).strip())
        except ValueError:
            web_ms = 1080
        web_ms = max(0, min(4096, web_ms))
        self._web_stream_max_side_var.set(str(web_ms))
        data = {
            "keywords": self._kw_var.get(),
            "template_paths": list(_parse_template_paths(self._tpl_var.get())),
            "template_threshold": _f(self._th_var, 0.80),
            "ocr_engines": list(self._ocr_engines_for_cfg()),
            "cooldown_sec": _f(self._cd_var, 3.0),
            "show_overlay": self._show_overlay_var.get(),
            "ocr_variant_groups": list(og),
            "capture_fps": cap_fps,
            "capture_source_mode": self._src_mode.get(),
            "web_stream_enabled": bool(self._web_stream_enabled_var.get()),
            "web_stream_port": web_port,
            "web_stream_max_side": web_ms,
            "web_stream_audio_output": str(
                self._web_stream_audio_output_var.get()
            ).strip(),
            "web_stream_https": bool(self._web_stream_https_var.get()),
            "web_stream_ssl_cert": str(self._web_stream_ssl_cert_var.get()).strip(),
            "web_stream_ssl_key": str(self._web_stream_ssl_key_var.get()).strip(),
        }
        wg = _read_window_geometry_str(self)
        if wg:
            data["window_geometry"] = wg
        if sys.platform == "win32":
            self._merge_arduino_into_settings_dict(data)
        try:
            _save_json_settings(data)
            self._ui_cfg_dirty = False
            self._last_cfg_poll_sync = time.monotonic()
        except Exception as e:
            if show_error_dialog:
                messagebox.showerror("저장 실패", str(e))
            return False
        if sys.platform == "win32":
            self._sync_arduino_bridge()
        return True

    def _merge_arduino_into_settings_dict(self, data: dict) -> None:
        try:
            baud = int(str(self._arduino_baud_var.get()).strip())
        except ValueError:
            baud = 115200
        self._arduino_baud_var.set(str(baud))
        data["arduino_serial"] = {
            "port": str(self._arduino_port_var.get()).strip(),
            "baud": baud,
            "keys": str(self._arduino_keys_var.get()).strip(),
            "focus_event_enabled": bool(self._arduino_focus_event_enabled_var.get()),
            "focus_event_key_gain": str(self._arduino_focus_gain_key_var.get()).strip(),
            "focus_event_key_loss": str(self._arduino_focus_loss_key_var.get()).strip(),
        }

    def _merge_arduino_into_settings_file_only(self) -> None:
        data = _load_json_settings()
        self._merge_arduino_into_settings_dict(data)
        _save_json_settings(data)

    def _arduino_tokens_from_keys_var(self) -> list[str]:
        raw = str(self._arduino_keys_var.get()).replace(";", ",")
        return [t.strip() for t in raw.split(",") if t.strip()]

    def _arduino_set_keys_var_from_tokens(self, tokens: list[str]) -> None:
        self._arduino_keys_var.set(",".join(tokens))

    def _arduino_apply_bridge_connection(self) -> bool:
        """현재 UI 설정으로 연결(또는 재연결). 성공 시 True."""
        if not bridge_supported():
            self._arduino_bridge.stop()
            self._arduino_status_var.set(
                "pynput·pyserial 필요: pip install pynput pyserial"
            )
            self._arduino_summary_var.set("Arduino: pynput/pyserial 없음")
            return False
        spec = self._arduino_keys_var.get()
        vks, bad = parse_key_filter_spec(spec)
        if bad:
            self._arduino_bridge.stop()
            self._arduino_status_var.set(
                "인식하지 못한 키: " + ", ".join(bad[:8])
                + (" …" if len(bad) > 8 else "")
            )
            self._arduino_summary_var.set("Arduino: 키 목록 오류")
            return False
        if not vks:
            self._arduino_bridge.stop()
            self._arduino_status_var.set("전송할 키를 한 개 이상 추가하세요.")
            self._arduino_summary_var.set("Arduino: 키 미지정")
            return False
        try:
            baud = int(str(self._arduino_baud_var.get()).strip())
        except ValueError:
            baud = 115200
        self._arduino_baud_var.set(str(baud))
        port = str(self._arduino_port_var.get()).strip()
        ok = self._arduino_bridge.start(port, baud, vks)
        if ok:
            self._arduino_status_var.set(
                f"Arduino 연결됨 {port} @ {baud} — 키 {len(vks)}개 감지 중"
            )
            self._arduino_summary_var.set(
                f"Arduino: {port} @ {baud} 연결 · {len(vks)}개 키 전송"
            )
        else:
            err = self._arduino_bridge.last_error() or "연결 실패"
            self._arduino_status_var.set(f"오류: {err}")
            short = err if len(err) <= 56 else err[:53] + "…"
            self._arduino_summary_var.set(f"Arduino: 오류 — {short}")
        return ok

    # 포커스 획득(now_focused=True)·해제(False) 각각의 키 토큰을 VK로 해석한다.
    def _focus_event_vk(self, now_focused: bool) -> int | None:
        token = str(
            self._arduino_focus_gain_key_var.get()
            if now_focused
            else self._arduino_focus_loss_key_var.get()
        ).strip()
        if not token:
            return None
        vks, bad = parse_key_filter_spec(token)
        if bad or len(vks) != 1:
            return None
        return next(iter(vks))

    # 송출 중 선택 창의 포커스 변화(획득/해제)마다 Arduino에 DOWN→UP 한 번씩 보낸다.
    def _emit_focus_transition_to_arduino_if_needed(self) -> None:
        if sys.platform != "win32":
            return
        if self._thread is None:
            self._last_window_focus_state = None
            return
        if self._src_mode.get() != "window" or self._picked_hwnd is None:
            self._last_window_focus_state = None
            return
        eff = int(self._picked_hwnd)
        try:
            from windows_capture import (
                is_league_capture_pair_hwnd,
                resolve_league_capture_hwnd,
            )

            if is_league_capture_pair_hwnd(eff):
                eff = int(resolve_league_capture_hwnd(eff))
        except Exception:
            pass
        now_focused = _win32_foreground_hwnd() == eff
        last = self._last_window_focus_state
        self._last_window_focus_state = now_focused
        if last is None or last == now_focused:
            return
        if not bool(self._arduino_focus_event_enabled_var.get()):
            return
        if not self._arduino_bridge.is_active():
            return
        vk = self._focus_event_vk(now_focused)
        if vk is None:
            self._arduino_status_var.set(
                "포커스 이벤트 키 설정 오류: 획득/해제 각각 셀렉트박스에서 키 1개를 선택하세요."
            )
            return
        ok_down = self._arduino_bridge.send_virtual_key(vk, down=True)
        ok_up = self._arduino_bridge.send_virtual_key(vk, down=False)
        if not (ok_down and ok_up):
            self._arduino_status_var.set("포커스 이벤트 전송 실패: Arduino 연결 상태를 확인하세요.")

    def _arduino_set_idle_disconnected(self) -> None:
        """연결되지 않은 상태 메시지(요약만 덮어쓸 때)."""
        self._arduino_summary_var.set("Arduino: 연결 안 됨")
        self._arduino_status_var.set(
            "「연결」을 누르면 선택한 COM으로 키 이벤트를 보냅니다 (프로토콜 D,<VK> / U,<VK>)."
        )

    def _open_arduino_settings_window(self) -> None:
        if sys.platform != "win32":
            return

        dlg = tk.Toplevel(self)
        dlg.title("아두이노 설정")
        dlg.geometry("600x760")
        dlg.minsize(520, 680)
        dlg.transient(self)
        port_desc_var = tk.StringVar(value="")

        # 하단 연결/닫기 줄을 먼저 붙여 두면 창 높이가 작아도 버튼이 잘리지 않는다.
        bar = ttk.Frame(dlg, padding=(10, 8, 10, 10))
        bar.pack(side=tk.BOTTOM, fill=tk.X)

        top = ttk.Frame(dlg, padding=10)
        top.pack(fill=tk.BOTH, expand=True)

        prow = ttk.Frame(top)
        prow.pack(fill=tk.X, pady=(0, 0))
        ttk.Label(prow, text="COM 포트").pack(side=tk.LEFT)
        combo = ttk.Combobox(prow, textvariable=self._arduino_port_var, width=16)
        combo.pack(side=tk.LEFT, padx=8)

        def update_port_desc() -> None:
            pairs = list_com_ports()
            dev = str(self._arduino_port_var.get()).strip()
            for d, desc in pairs:
                if d == dev:
                    port_desc_var.set(desc[:140] if desc else "")
                    return
            port_desc_var.set(
                "(선택한 포트가 목록에 없습니다. 직접 입력했거나 케이블을 확인하세요.)"
            )

        def refresh_ports() -> None:
            pairs = list_com_ports()
            devices = [d for d, _ in pairs]
            cur = str(self._arduino_port_var.get()).strip()
            if cur:
                ups = {x.upper() for x in devices}
                if cur.upper() not in ups:
                    devices.insert(0, cur)
            combo["values"] = devices
            update_port_desc()

        ttk.Button(prow, text="포트 새로고침", command=refresh_ports).pack(
            side=tk.LEFT, padx=4
        )
        ttk.Label(prow, text="보드레이트").pack(side=tk.LEFT, padx=(16, 0))
        ttk.Entry(prow, textvariable=self._arduino_baud_var, width=10).pack(
            side=tk.LEFT, padx=4
        )

        focus_chk_fr = ttk.Frame(top)
        focus_chk_fr.pack(fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(
            focus_chk_fr,
            text="선택 창 포커스 획득·해제 시 키 전송 (각각 다른 키 가능)",
            variable=self._arduino_focus_event_enabled_var,
        ).pack(side=tk.LEFT)

        focus_row = ttk.Frame(top)
        focus_row.pack(fill=tk.X, pady=(6, 0))
        _choices = key_pick_choices()
        _choice_set = set(_choices)
        ttk.Label(focus_row, text="포커스 획득 시").pack(side=tk.LEFT)
        gain_combo = ttk.Combobox(
            focus_row,
            textvariable=self._arduino_focus_gain_key_var,
            values=_choices,
            width=14,
            state="readonly",
        )
        gain_combo.pack(side=tk.LEFT, padx=(6, 16))
        ttk.Label(focus_row, text="포커스 해제 시").pack(side=tk.LEFT)
        loss_combo = ttk.Combobox(
            focus_row,
            textvariable=self._arduino_focus_loss_key_var,
            values=_choices,
            width=14,
            state="readonly",
        )
        loss_combo.pack(side=tk.LEFT, padx=(6, 0))
        if str(self._arduino_focus_gain_key_var.get()).strip() not in _choice_set:
            self._arduino_focus_gain_key_var.set("F8")
        if str(self._arduino_focus_loss_key_var.get()).strip() not in _choice_set:
            self._arduino_focus_loss_key_var.set("F8")

        ttk.Label(
            top,
            textvariable=port_desc_var,
            foreground="gray",
            font=("", 8),
            wraplength=520,
        ).pack(anchor=tk.W, pady=(4, 0))

        keys_lab = ttk.LabelFrame(top, text="전송할 키", padding=(6, 6))
        keys_lab.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        pick_fr = ttk.Frame(keys_lab)
        pick_fr.pack(fill=tk.X)
        ttk.Label(pick_fr, text="키 선택").pack(side=tk.LEFT)
        pick_placeholder = "(선택하면 목록에 추가)"
        key_pick = ttk.Combobox(
            pick_fr,
            values=(pick_placeholder, *key_pick_choices()),
            width=28,
            state="readonly",
        )
        key_pick.pack(side=tk.LEFT, padx=8)
        key_pick.set(pick_placeholder)

        keys_row = ttk.Frame(keys_lab)
        keys_row.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        lb_fr = ttk.Frame(keys_row)
        lb_fr.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lb_scroll = ttk.Scrollbar(lb_fr)
        lb_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        keys_lb = tk.Listbox(
            lb_fr,
            height=8,
            selectmode=tk.EXTENDED,
            yscrollcommand=lb_scroll.set,
            exportselection=False,
        )
        keys_lb.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lb_scroll.config(command=keys_lb.yview)

        def sync_var_from_listbox() -> None:
            items = [keys_lb.get(i) for i in range(keys_lb.size())]
            self._arduino_set_keys_var_from_tokens(items)

        def refill_listbox_from_var() -> None:
            keys_lb.delete(0, tk.END)
            for t in self._arduino_tokens_from_keys_var():
                keys_lb.insert(tk.END, t)

        def on_pick(_evt: object | None = None) -> None:
            sel = key_pick.get()
            if not sel or sel.startswith("("):
                return
            existing = {keys_lb.get(i) for i in range(keys_lb.size())}
            if sel not in existing:
                keys_lb.insert(tk.END, sel)
                sync_var_from_listbox()
            key_pick.set(pick_placeholder)

        key_pick.bind("<<ComboboxSelected>>", on_pick)

        btn_col = ttk.Frame(keys_row)
        btn_col.pack(side=tk.LEFT, padx=(8, 0), fill=tk.Y)

        def remove_selected() -> None:
            sel = list(keys_lb.curselection())
            if not sel:
                return
            for i in reversed(sel):
                keys_lb.delete(i)
            sync_var_from_listbox()

        ttk.Button(btn_col, text="선택 제거", command=remove_selected).pack(
            fill=tk.X, pady=2
        )

        def clear_all_keys() -> None:
            keys_lb.delete(0, tk.END)
            sync_var_from_listbox()

        ttk.Button(btn_col, text="전부 비우기", command=clear_all_keys).pack(
            fill=tk.X, pady=2
        )

        ttk.Label(
            keys_lab,
            text="PC→아두이노 프로토콜: D,<VK> 다운 · U,<VK> 업. 이 창을 닫으면 설정이 저장됩니다.",
            foreground="gray",
            font=("", 8),
            wraplength=520,
        ).pack(anchor=tk.W, pady=(6, 0))

        refill_listbox_from_var()

        log_fr = ttk.LabelFrame(top, text="아두이노 로그", padding=(6, 6))
        log_fr.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        ttk.Label(
            log_fr,
            text=(
                "[KB] 체크 시 PC 키 이벤트, [RX] 아두이노 Serial 수신, [상태] 연결·오류 안내가 한 창에 표시됩니다."
            ),
            foreground="gray",
            font=("", 8),
            wraplength=520,
        ).pack(anchor=tk.W)
        log_row = ttk.Frame(log_fr)
        log_row.pack(fill=tk.X, pady=(4, 0))
        kb_debug_var = tk.BooleanVar(value=False)

        def on_kb_debug_toggle() -> None:
            set_key_bridge_debug_logging(kb_debug_var.get())

        ttk.Checkbutton(
            log_row,
            text="키 이벤트 로그 남기기",
            variable=kb_debug_var,
            command=on_kb_debug_toggle,
        ).pack(side=tk.LEFT)
        arduino_log_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            log_row,
            text="맨 아래 자동 스크롤",
            variable=arduino_log_autoscroll,
        ).pack(side=tk.LEFT, padx=(12, 0))

        arduino_log = scrolledtext.ScrolledText(
            log_fr,
            height=14,
            wrap=tk.NONE,
            font=("Consolas", 9),
        )
        arduino_log.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        kb_poll_after: list[str | None] = [None]

        def log_status_line(msg: str) -> None:
            ts = time.strftime("%H:%M:%S")
            try:
                arduino_log.insert(tk.END, f"[상태] {ts} {msg}\n")
            except tk.TclError:
                pass

        def clear_arduino_log_view() -> None:
            clear_key_bridge_debug_log()
            clear_received_serial_log()
            clear_arduino_notice_buffer()
            try:
                arduino_log.delete("1.0", tk.END)
            except tk.TclError:
                pass

        ttk.Button(log_row, text="로그 비우기", command=clear_arduino_log_view).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        def cancel_kb_log_poll() -> None:
            aid = kb_poll_after[0]
            if aid is not None:
                try:
                    dlg.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
                kb_poll_after[0] = None

        def close_arduino_dlg_cleanup() -> None:
            cancel_kb_log_poll()
            set_key_bridge_debug_logging(False)
            kb_debug_var.set(False)

        def tick_arduino_log() -> None:
            kb_poll_after[0] = None
            try:
                if not dlg.winfo_exists():
                    return
            except tk.TclError:
                return
            for line in take_arduino_notice_lines():
                arduino_log.insert(tk.END, line)
            for line in drain_key_bridge_debug_lines(250):
                arduino_log.insert(tk.END, line)
            for line in drain_received_serial_lines(250):
                arduino_log.insert(tk.END, line)
            try:
                end_line = int(float(arduino_log.index("end-1c").split(".")[0]))
                if end_line > 900:
                    arduino_log.delete("1.0", "450.0")
            except tk.TclError:
                pass
            if arduino_log_autoscroll.get():
                try:
                    arduino_log.see(tk.END)
                except tk.TclError:
                    pass
            kb_poll_after[0] = dlg.after(120, tick_arduino_log)

        combo.bind("<<ComboboxSelected>>", lambda _e: update_port_desc())
        combo.bind("<FocusOut>", lambda _e: update_port_desc())

        refresh_ports()
        if not bridge_supported():
            self._arduino_status_var.set(
                "pynput·pyserial 필요: pip install pynput pyserial"
            )
        open_msg = (
            self._arduino_status_var.get().strip()
            or self._arduino_summary_var.get().strip()
            or "Arduino 설정 창을 열었습니다."
        )
        log_status_line(open_msg)

        toggle_btn = ttk.Button(bar)

        def refresh_toggle_label() -> None:
            if self._arduino_bridge.is_active():
                toggle_btn.configure(text="연결 해제")
            else:
                toggle_btn.configure(text="연결")

        def do_toggle() -> None:
            if self._arduino_bridge.is_active():
                self._arduino_bridge.stop()
                self._arduino_set_idle_disconnected()
            else:
                self._arduino_apply_bridge_connection()
            refresh_toggle_label()
            log_status_line(self._arduino_status_var.get())

        toggle_btn.configure(command=do_toggle)
        refresh_toggle_label()
        toggle_btn.pack(side=tk.LEFT, padx=2)

        def on_close_dlg() -> None:
            close_arduino_dlg_cleanup()
            self._merge_arduino_into_settings_file_only()
            self._sync_arduino_bridge()
            dlg.destroy()

        ttk.Button(bar, text="닫기", command=on_close_dlg).pack(side=tk.RIGHT, padx=2)

        dlg.protocol("WM_DELETE_WINDOW", on_close_dlg)

        dlg.after(80, tick_arduino_log)

    def _sync_arduino_bridge(self) -> None:
        """연결 중이면 설정으로 재연결, 아니면 대기 메시지만 갱신."""
        if sys.platform != "win32":
            return
        if not self._arduino_bridge.is_active():
            self._arduino_set_idle_disconnected()
            return
        self._arduino_apply_bridge_connection()

    def _browse_template(self) -> None:
        if not self._keyword_ocr_var.get():
            return
        paths = filedialog.askopenfilenames(
            title="템플릿 이미지 (여러 개 선택 가능)",
            filetypes=[
                ("이미지", "*.png;*.jpg;*.jpeg;*.bmp"),
                ("모든 파일", "*.*"),
            ],
        )
        if not paths:
            return
        cur = self._tpl_var.get().strip()
        add = ";".join(paths)
        self._tpl_var.set(f"{cur};{add}" if cur else add)

    def _ocr_engines_for_cfg(self) -> tuple[str, ...]:
        if not self._keyword_ocr_var.get():
            return ()
        return (ENGINE_RAPIDOCR,)

    def _ocr_variant_groups_for_cfg(self) -> tuple[str, ...]:
        checked = tuple(
            vid
            for vid, _ in OCR_VARIANT_UI_CHOICES
            if self._ocr_variant_group_vars[vid].get()
        )
        n_all = len(OCR_VARIANT_UI_CHOICES)
        if len(checked) == 0:
            return OCR_VARIANT_GROUPS_DISABLED
        if len(checked) == n_all:
            return ()
        return checked

    def _sync_cfg_from_ui(self) -> None:
        raw = self._kw_var.get()
        kws = tuple(s.strip() for s in raw.split(",") if s.strip())
        try:
            th = float(self._th_var.get())
        except ValueError:
            th = 0.80
        try:
            cd = float(self._cd_var.get())
        except ValueError:
            cd = 3.0
        tpls = (
            ()
            if not self._keyword_ocr_var.get()
            else _parse_template_paths(self._tpl_var.get())
        )
        new_cfg = DetectionConfig(
            alert_keywords=kws,
            template_paths=tpls,
            template_threshold=th,
            ocr_engines=self._ocr_engines_for_cfg(),
            ocr_variant_groups=self._ocr_variant_groups_for_cfg(),
        )
        with self._det_lock:
            self._cfg = new_cfg
        self._alert_cooldown_sec = max(1.0, cd)

    def _try_alert_sound_from_ocr(self) -> None:
        """OCR 키워드 알림(UI 스레드). 쿨타임 내 재요청·송출 중지 후 예약분은 무시."""
        self._try_alert_sound()

    def _try_alert_sound(self) -> None:
        if not self._sound_armed:
            return
        if self._thread is None or self._det_thread is None:
            return
        with self._alert_sound_lock:
            now = time.time()
            cd = max(1.0, self._alert_cooldown_sec)
            if now - self._last_alert_sound_ts < cd:
                return
            self._last_alert_sound_ts = now
        play_alert_sound()

    def _on_detection_cfg_changed(self) -> None:
        """OCR 엔진·전처리 변형 변경 시 설정 즉시 반영 + 진행 중 키워드 OCR 중단 + 워커 대기 깨우기."""
        self._sync_cfg_from_ui()
        self._apply_keyword_ocr_tpl_ui_state()
        self._ui_cfg_dirty = False
        self._last_cfg_poll_sync = time.monotonic()
        self._det_kw_abort.set()
        self._det_cfg_wake.set()

    def _start(self) -> None:
        self._stop()
        try:
            float(self._fps_var.get())
        except ValueError:
            messagebox.showerror("오류", "FPS를 숫자로 입력하세요.")
            return
        fps = self._effective_capture_fps()
        try:
            fps_ui = float(self._fps_var.get())
        except ValueError:
            fps_ui = fps
        jt = self._bg_join_thread
        if jt is not None and jt.is_alive():
            # ws.stop()·aiohttp 정리가 수 초 걸릴 수 있음. 짧게만 기다리면 다음 송출이
            # 같은 포트·화면 캡처와 겹쳐 뷰어가 검은 화면만 보는 레이스가 난다.
            jt.join(timeout=12.0)

        web_streamer = None
        web_port: int | None = None
        if self._web_stream_enabled_var.get():
            try:
                web_port = int(str(self._web_stream_port_var.get()).strip())
            except ValueError:
                messagebox.showerror("오류", "웹 송출 포트를 숫자로 입력하세요.")
                return
            if web_port < 1 or web_port > 65535:
                messagebox.showerror("오류", "웹 송출 포트는 1~65535 범위여야 합니다.")
                return
            try:
                from web_stream import WebStreamServer, build_web_stream_ssl_context
            except Exception as e:
                messagebox.showerror(
                    "웹 송출 초기화 실패",
                    "의존성 로드 실패입니다.\n"
                    "pip install -r requirements.txt\n\n"
                    f"상세: {e}",
                )
                return
            try:
                try:
                    max_side = int(str(self._web_stream_max_side_var.get()).strip())
                except ValueError:
                    max_side = 1080
                max_side = max(0, min(4096, max_side))
                self._web_stream_max_side_var.set(str(max_side))
                audio_sel = str(self._web_stream_audio_output_var.get()).strip()
                try:
                    ssl_ctx = build_web_stream_ssl_context(
                        enabled=bool(self._web_stream_https_var.get()),
                        certfile=self._web_stream_ssl_cert_var.get(),
                        keyfile=self._web_stream_ssl_key_var.get(),
                    )
                except ValueError as ve:
                    messagebox.showerror("HTTPS 설정", str(ve))
                    return
                time.sleep(0.25)
                web_streamer = WebStreamServer(
                    port=web_port,
                    fps=fps,
                    max_stream_side=max_side,
                    audio_output_name=audio_sel or None,
                    ssl_context=ssl_ctx,
                )
                web_streamer.start()
                aerr = web_streamer.get_audio_error()
                self._web_stream_audio_err = aerr
                if aerr:
                    messagebox.showwarning(
                        "웹 오디오 경고",
                        "시스템 오디오 loopback 초기화에 실패했습니다.\n"
                        "영상은 송출되지만 오디오는 없을 수 있습니다.\n\n"
                        f"상세: {aerr}",
                    )
            except Exception as e:
                messagebox.showerror("웹 송출 시작 실패", str(e))
                if web_streamer is not None:
                    try:
                        web_streamer.stop()
                    except Exception:
                        pass
                return

        hwnd: int | None = None
        mon = 1
        if window_pick_supported() and self._src_mode.get() == "window":
            if self._picked_hwnd is None:
                messagebox.showwarning(
                    "창 선택",
                    "「프로세스(창) 지정」을 쓰는 경우 먼저 「창 선택…」에서 창을 고르세요.",
                )
                return
            hwnd = self._picked_hwnd
        else:
            try:
                mon = int(self._mon_var.get())
            except ValueError:
                messagebox.showerror("오류", "모니터 번호를 숫자로 입력하세요.")
                return
        # 롤: 선택 창이 League of Legends.exe / LeagueClientUx.exe 일 때만 위 둘 사이 전환.
        dyn_resolver = None
        if sys.platform == "win32" and hwnd is not None:
            try:
                from windows_capture import (
                    is_league_capture_pair_hwnd,
                    resolve_league_capture_hwnd,
                )

                if is_league_capture_pair_hwnd(int(hwnd)):
                    _base = int(hwnd)

                    def dyn_resolver() -> int:
                        return int(resolve_league_capture_hwnd(_base))
            except Exception:
                dyn_resolver = None

        self._thread = CaptureThread(
            monitor_index=mon,
            target_fps=fps,
            window_hwnd=hwnd,
            dynamic_hwnd_resolver=dyn_resolver,
            on_frame=(
                (lambda frm: web_streamer.push_video_frame(frm))
                if web_streamer is not None
                else None
            ),
        )
        self._thread.start()
        self._web_streamer = web_streamer
        if web_streamer is None:
            self._web_stream_audio_err = None
        if web_streamer is not None and web_port is not None:
            self._web_stream_port_var.set(str(web_port))
            self._web_stream_url_var.set(self._current_public_web_stream_url(web_port))
        else:
            self._web_stream_url_var.set("")
        self._det_stop.clear()
        self._det_cfg_wake.clear()
        self._det_kw_abort.clear()
        self._det_thread = threading.Thread(
            target=self._detection_worker_loop,
            daemon=True,
            name="Oddments-Detect",
        )
        self._det_thread.start()
        if abs(fps_ui - fps) > 0.51:
            fps_txt = f"캡처 {fps:.0f} FPS (UI {fps_ui:.0f})"
        else:
            fps_txt = f"{fps:.0f} FPS"
        self._sound_armed = True
        self._last_window_focus_state = None
        if hwnd is not None:
            self._stream_status_text = f"송출 중 — 선택 창 (창 ID {hwnd}), {fps_txt}"
            self._set_main_status_line(self._stream_status_text)
        else:
            self._stream_status_text = f"송출 중 — 모니터 {mon}, {fps_txt}"
            self._set_main_status_line(self._stream_status_text)
        if web_streamer is not None and web_port is not None:
            self._set_main_status_line(
                f"{self._stream_status_text} · 웹: {self._current_public_web_stream_url(web_port)}"
            )

    def _detection_worker_loop(self) -> None:
        """OCR·템플릿 감지는 메인(UI) 스레드가 아닌 여기서만 실행."""
        while not self._det_stop.is_set():
            interval_sec = max(0.15, self._detect_every_ms / 1000.0)
            t0 = time.perf_counter()
            self._det_kw_abort.clear()
            thr = self._thread
            frame = thr.get_frame() if thr is not None else None
            with self._det_lock:
                cfg = self._cfg
            trig, reason = False, ""
            if frame is not None:
                try:
                    trig, reason, _ = run_detection_with_overlays(
                        frame,
                        cfg,
                        self._det_stop,
                        kw_abort=self._det_kw_abort,
                    )
                except Exception:
                    import traceback

                    traceback.print_exc()
            with self._det_lock:
                self._last_det_triggered = trig
                self._last_det_reason = reason
            elapsed = time.perf_counter() - t0
            remaining = max(0.0, interval_sec - elapsed)
            while remaining > 0 and not self._det_stop.is_set():
                if self._det_cfg_wake.is_set():
                    self._det_cfg_wake.clear()
                    break
                step = min(remaining, 0.05)
                if self._det_stop.wait(timeout=step):
                    break
                if self._det_cfg_wake.is_set():
                    self._det_cfg_wake.clear()
                    break
                remaining -= step

    def _stop(self) -> None:
        self._sound_armed = False
        stop_queued_alert_sounds()
        self._det_stop.set()
        t_det = self._det_thread
        t_cap = self._thread
        self._det_thread = None
        self._thread = None
        self._was_triggered_last = False
        get_overlay_store().clear()
        with self._det_lock:
            self._last_det_triggered = False
            self._last_det_reason = ""
        self._stream_status_text = ""
        self._web_stream_url_var.set("")
        self._last_window_focus_state = None
        self._web_stream_audio_err = None
        self._set_main_status_line("")
        ws = self._web_streamer
        self._web_streamer = None

        def join_bg() -> None:
            if t_det is not None and t_det.is_alive():
                t_det.join(timeout=0.4)
            if t_cap is not None:
                t_cap.stop()
                t_cap.join(timeout=2.5)
            # 웹 송출 aiohttp/WebRTC 종료는 초 단위 블로킹 가능 → UI 스레드가 아닌 여기서 처리
            if ws is not None:
                try:
                    ws.stop()
                except Exception:
                    pass

        self._bg_join_thread = threading.Thread(
            target=join_bg, daemon=True, name="Oddments-StopJoin"
        )
        self._bg_join_thread.start()

    def _cancel_ocr_log_poll(self) -> None:
        if self._ocr_log_poll_after is not None:
            try:
                self.after_cancel(self._ocr_log_poll_after)
            except (tk.TclError, ValueError):
                pass
            self._ocr_log_poll_after = None

    def _cancel_web_log_poll(self) -> None:
        if self._web_log_poll_after is not None:
            try:
                self.after_cancel(self._web_log_poll_after)
            except (tk.TclError, ValueError):
                pass
            self._web_log_poll_after = None

    def _open_ocr_settings_window(self) -> None:
        if self._ocr_settings_win is not None:
            try:
                if self._ocr_settings_win.winfo_exists():
                    self._ocr_settings_win.lift()
                    return
            except tk.TclError:
                pass

        win = tk.Toplevel(self)
        win.title("OCR·감지 설정")
        win.geometry("780x760")
        win.minsize(640, 600)
        win.transient(self)

        outer = ttk.Frame(win, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)
        body = ttk.LabelFrame(outer, text="감지·OCR", padding=8)
        body.pack(fill=tk.BOTH, expand=True)
        self._populate_ocr_settings(body)

        log_fr = ttk.LabelFrame(outer, text="OCR 로그", padding=(6, 6))
        log_fr.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        log_top = ttk.Frame(log_fr)
        log_top.pack(fill=tk.X)
        self._ocr_log_stats_var = tk.StringVar(value="OCR API 완료 호출: 0회")
        ttk.Label(log_top, textvariable=self._ocr_log_stats_var).pack(side=tk.LEFT)
        ttk.Checkbutton(
            log_top,
            text="맨 아래 자동 스크롤",
            variable=self._ocr_log_autoscroll_var,
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(
            log_top, text="통계·큐 초기화", command=self._ocr_log_reset_in_window
        ).pack(side=tk.RIGHT, padx=4)
        ttk.Button(
            log_top, text="창 비우기", command=self._ocr_log_clear_view_in_window
        ).pack(side=tk.RIGHT)

        st = scrolledtext.ScrolledText(
            log_fr, height=14, font=("Consolas", 9), wrap=tk.NONE
        )
        st.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        st.insert(
            tk.END,
            "# 번호 | 시각 | 엔진 | 작업 | 소요(ms) | …  (# 호출·응답, * 부가)\n"
            "# 키워드 인식은 RapidOCR만 사용.\n\n",
        )
        self._ocr_log_widget = st
        self._ocr_settings_win = win

        def on_close() -> None:
            self._cancel_ocr_log_poll()
            self._ocr_log_widget = None
            self._ocr_log_stats_var = None
            self._ocr_settings_win = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self._ocr_log_poll_after = self.after(100, self._tick_ocr_settings_log)

    def _tick_ocr_settings_log(self) -> None:
        self._ocr_log_poll_after = None
        if self._ocr_log_widget is None or self._ocr_settings_win is None:
            return
        try:
            if not self._ocr_settings_win.winfo_exists():
                self._ocr_log_widget = None
                self._ocr_settings_win = None
                return
        except tk.TclError:
            self._ocr_log_widget = None
            self._ocr_settings_win = None
            return

        from detection.ocr_diag import drain_ocr_log_lines, get_ocr_call_total

        if self._ocr_log_stats_var is not None:
            self._ocr_log_stats_var.set(
                f"OCR API 완료 호출: {get_ocr_call_total()}회"
            )
        for line in drain_ocr_log_lines(250):
            self._ocr_log_widget.insert(tk.END, line)
        try:
            end_line = int(float(self._ocr_log_widget.index("end-1c").split(".")[0]))
            if end_line > 4000:
                self._ocr_log_widget.delete("1.0", "1500.0")
        except (tk.TclError, ValueError):
            pass
        if self._ocr_log_autoscroll_var.get():
            self._ocr_log_widget.see(tk.END)
        self._ocr_log_poll_after = self.after(120, self._tick_ocr_settings_log)

    def _ocr_log_clear_view_in_window(self) -> None:
        if self._ocr_log_widget is None:
            return
        self._ocr_log_widget.delete("1.0", tk.END)
        self._ocr_log_widget.insert(
            tk.END, "# 화면만 비움. 「통계·큐 초기화」로 큐·카운터를 비웁니다.\n\n"
        )

    def _ocr_log_reset_in_window(self) -> None:
        from detection.ocr_diag import reset_ocr_log

        reset_ocr_log()
        if self._ocr_log_stats_var is not None:
            self._ocr_log_stats_var.set("OCR API 완료 호출: 0회 (초기화됨)")

    def _refresh_web_audio_output_combo(self) -> None:
        combo = self._web_audio_output_combo
        if combo is None:
            return
        try:
            from web_stream import list_web_stream_audio_outputs
        except ImportError:
            names = []
        else:
            names = list_web_stream_audio_outputs()
        labels = ["시스템 기본 출력"] + names
        self._web_audio_output_internal = [""] + names
        combo.configure(values=labels)
        saved = str(self._web_stream_audio_output_var.get()).strip()
        try:
            idx = self._web_audio_output_internal.index(saved)
        except ValueError:
            idx = 0
            if saved:
                self._web_stream_audio_output_var.set("")
        try:
            combo.current(idx)
        except tk.TclError:
            pass

    def _on_web_audio_output_selected(self, _event: object | None = None) -> None:
        combo = self._web_audio_output_combo
        if combo is None:
            return
        internals = self._web_audio_output_internal
        if not internals:
            return
        try:
            i = int(combo.current())
        except (tk.TclError, ValueError):
            return
        if i < 0 or i >= len(internals):
            return
        self._web_stream_audio_output_var.set(internals[i])
        self._persist_app_settings(show_error_dialog=False)

    def _browse_web_ssl_cert(self) -> None:
        path = filedialog.askopenfilename(
            title="TLS 인증서 파일",
            filetypes=(
                ("PEM / CRT", "*.pem *.crt"),
                ("모든 파일", "*.*"),
            ),
        )
        if path:
            self._web_stream_ssl_cert_var.set(path)
            self._persist_app_settings(show_error_dialog=False)

    def _browse_web_ssl_key(self) -> None:
        path = filedialog.askopenfilename(
            title="TLS 개인키 파일",
            filetypes=(
                ("PEM / KEY", "*.pem *.key"),
                ("모든 파일", "*.*"),
            ),
        )
        if path:
            self._web_stream_ssl_key_var.set(path)
            self._persist_app_settings(show_error_dialog=False)

    def _open_web_stream_settings_window(self) -> None:
        if self._web_settings_win is not None:
            try:
                if self._web_settings_win.winfo_exists():
                    self._web_settings_win.lift()
                    self._refresh_web_audio_output_combo()
                    return
            except tk.TclError:
                pass

        win = tk.Toplevel(self)
        win.title("웹 화면 송출 설정")
        win.geometry("640x620")
        win.minsize(520, 520)
        win.transient(self)

        outer = ttk.Frame(win, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)
        row = ttk.Frame(outer)
        row.pack(fill=tk.X)
        ttk.Checkbutton(
            row,
            text="웹 송출(WebRTC) 사용",
            variable=self._web_stream_enabled_var,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row, text="포트").pack(side=tk.LEFT)
        ttk.Entry(row, width=6, textvariable=self._web_stream_port_var).pack(
            side=tk.LEFT, padx=(4, 10)
        )
        ttk.Button(row, text="URL 복사", command=self._copy_web_stream_url).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Label(row, textvariable=self._web_stream_url_var).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        ttk.Label(
            outer,
            text="외부 접속: 공유기 포트포워딩·방화벽 필요. 송출은 메인에서 「송출 시작」 후 동작.",
            foreground="gray",
            font=("", 8),
            wraplength=600,
        ).pack(anchor=tk.W, pady=(8, 0))
        tls_row = ttk.Frame(outer)
        tls_row.pack(fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(
            tls_row,
            text="HTTPS(TLS)로 송출",
            variable=self._web_stream_https_var,
            command=lambda: self._persist_app_settings(show_error_dialog=False),
        ).pack(side=tk.LEFT)
        cert_row = ttk.Frame(outer)
        cert_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(cert_row, text="인증서").pack(side=tk.LEFT)
        ttk.Entry(
            cert_row,
            width=52,
            textvariable=self._web_stream_ssl_cert_var,
        ).pack(side=tk.LEFT, padx=(8, 6), fill=tk.X, expand=True)
        ttk.Button(cert_row, text="찾기…", command=self._browse_web_ssl_cert).pack(
            side=tk.RIGHT
        )
        key_row = ttk.Frame(outer)
        key_row.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(key_row, text="개인키").pack(side=tk.LEFT)
        ttk.Entry(
            key_row,
            width=52,
            textvariable=self._web_stream_ssl_key_var,
        ).pack(side=tk.LEFT, padx=(8, 6), fill=tk.X, expand=True)
        ttk.Button(key_row, text="찾기…", command=self._browse_web_ssl_key).pack(
            side=tk.RIGHT
        )
        ttk.Label(
            outer,
            text="Let's Encrypt 등 fullchain.pem·privkey.pem 또는 자체 서명 인증서를 지정할 수 있습니다. 변경 후 적용하려면 송출을 중지했다가 다시 시작하세요.",
            foreground="gray",
            font=("", 8),
            wraplength=600,
        ).pack(anchor=tk.W, pady=(4, 0))
        if sys.platform == "win32":
            arow = ttk.Frame(outer)
            arow.pack(fill=tk.X, pady=(6, 0))
            ttk.Label(arow, text="송출 오디오").pack(side=tk.LEFT)
            self._web_audio_output_combo = ttk.Combobox(
                arow,
                width=48,
                state="readonly",
            )
            self._web_audio_output_combo.pack(
                side=tk.LEFT, padx=(8, 6), fill=tk.X, expand=True
            )
            ttk.Button(
                arow,
                text="목록 새로고침",
                command=self._refresh_web_audio_output_combo,
            ).pack(side=tk.RIGHT)
            self._refresh_web_audio_output_combo()
            self._web_audio_output_combo.bind(
                "<<ComboboxSelected>>",
                self._on_web_audio_output_selected,
            )
            ttk.Label(
                outer,
                text="재생 소리가 나는 출력(스피커·헤드셋·HDMI 등)을 고르면 그 경로를 루프백으로 송출합니다. 적용은 송출 중지 후 다시 시작.",
                foreground="gray",
                font=("", 8),
                wraplength=600,
            ).pack(anchor=tk.W, pady=(4, 0))
        else:
            self._web_audio_output_combo = None
        ttk.Label(
            outer,
            textvariable=self._web_viewer_count_var,
            font=("", 9),
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            outer,
            textvariable=self._web_audio_status_var,
            foreground="gray",
            font=("", 9),
            wraplength=600,
        ).pack(anchor=tk.W, pady=(2, 0))

        qrow = ttk.Frame(outer)
        qrow.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(qrow, text="송출 화질(긴 변 최대 px)").pack(side=tk.LEFT)
        ttk.Spinbox(
            qrow,
            from_=0,
            to=3840,
            width=6,
            textvariable=self._web_stream_max_side_var,
            increment=120,
        ).pack(side=tk.LEFT, padx=(6, 10))
        ttk.Label(
            outer,
            text="긴 변 상한(px): 0 원본 · 720 HD · 1080 FHD · 1440 QHD · 2160 4K",
            foreground="gray",
            font=("", 8),
        ).pack(anchor=tk.W, pady=(4, 0))

        log_fr = ttk.LabelFrame(outer, text="웹 로그", padding=(6, 6))
        log_fr.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        log_top = ttk.Frame(log_fr)
        log_top.pack(fill=tk.X)
        ttk.Checkbutton(
            log_top,
            text="맨 아래 자동 스크롤",
            variable=self._web_log_autoscroll_var,
        ).pack(side=tk.LEFT)
        ttk.Button(
            log_top, text="큐 비우기", command=self._web_log_reset_in_window
        ).pack(side=tk.RIGHT, padx=4)
        ttk.Button(
            log_top, text="창 비우기", command=self._web_log_clear_view_in_window
        ).pack(side=tk.RIGHT)

        st = scrolledtext.ScrolledText(
            log_fr, height=16, font=("Consolas", 9), wrap=tk.NONE
        )
        st.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        st.insert(tk.END, "뷰어 접속·WebRTC 연결 이벤트가 여기에 표시됩니다.\n\n")
        self._web_log_widget = st
        self._web_settings_win = win

        def on_close() -> None:
            self._persist_app_settings(show_error_dialog=False)
            self._cancel_web_log_poll()
            self._web_log_widget = None
            self._web_settings_win = None
            self._web_audio_output_combo = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self._web_log_poll_after = self.after(100, self._tick_web_settings_log)

    def _tick_web_settings_log(self) -> None:
        self._web_log_poll_after = None
        if self._web_log_widget is None or self._web_settings_win is None:
            return
        try:
            if not self._web_settings_win.winfo_exists():
                self._web_log_widget = None
                self._web_settings_win = None
                self._web_audio_output_combo = None
                return
        except tk.TclError:
            self._web_log_widget = None
            self._web_settings_win = None
            self._web_audio_output_combo = None
            return

        from web_log import drain_web_log_lines

        for line in drain_web_log_lines(250):
            self._web_log_widget.insert(tk.END, line)
        try:
            end_line = int(float(self._web_log_widget.index("end-1c").split(".")[0]))
            if end_line > 3500:
                self._web_log_widget.delete("1.0", "1200.0")
        except (tk.TclError, ValueError):
            pass
        if self._web_log_autoscroll_var.get():
            self._web_log_widget.see(tk.END)
        ws = self._web_streamer
        if ws is not None:
            try:
                n = ws.get_connected_viewer_count()
            except Exception:
                n = 0
            self._web_viewer_count_var.set(f"현재 시청 연결: {n}명")
            try:
                self._web_audio_status_var.set(ws.get_audio_status_line())
            except Exception:
                self._web_audio_status_var.set("")
        else:
            self._web_viewer_count_var.set("현재 시청 연결: 0명")
            self._web_audio_status_var.set("")
        self._web_log_poll_after = self.after(120, self._tick_web_settings_log)

    def _web_log_clear_view_in_window(self) -> None:
        if self._web_log_widget is None:
            return
        self._web_log_widget.delete("1.0", tk.END)
        self._web_log_widget.insert(tk.END, "화면만 비움. 「큐 비우기」는 대기 중인 로그 줄을 삭제합니다.\n\n")

    def _web_log_reset_in_window(self) -> None:
        from web_log import reset_web_log

        reset_web_log()

    def _on_close(self) -> None:
        from detection.ocr_diag import set_ocr_keyword_alert_sound_handler

        self._persist_app_settings(show_error_dialog=True)
        set_ocr_keyword_alert_sound_handler(None)
        self._running = False
        self._cancel_ocr_log_poll()
        self._cancel_web_log_poll()
        for _w in (self._ocr_settings_win, self._web_settings_win):
            if _w is not None:
                try:
                    if _w.winfo_exists():
                        _w.destroy()
                except tk.TclError:
                    pass
        if sys.platform == "win32":
            self._arduino_bridge.stop()
        self._stop()
        self.destroy()

    def _tick_preview(self) -> None:
        if not self._running:
            return
        iv = self._preview_interval_ms
        if self._thread:
            seq = self._thread.get_frame_seq()
            if seq > 0 and seq == self._preview_last_frame_seq:
                self.after(iv, self._tick_preview)
                return
            self._preview_last_frame_seq = seq
            frame = self._thread.get_frame()
            if frame is not None:
                if self._show_overlay_var.get():
                    ovl = get_overlay_store().snapshot()
                    vis = frame_with_overlays(frame, ovl) if ovl else frame
                else:
                    vis = frame
                h, w = vis.shape[:2]
                cw_meas = self._canvas.winfo_width()
                ch_meas = self._canvas.winfo_height()
                # 레이아웃 전 1px 등이면 기본값(기존과 동일한 느낌으로 스케일만 계산)
                cw = cw_meas if cw_meas > 8 else 800
                ch = ch_meas if ch_meas > 8 else 600
                base = float(self._preview_scale)
                bw = max(1.0, w * base)
                bh = max(1.0, h * base)
                # 송출 해상도가 미리보기 영역보다 크면 비율 유지해 캔버스 안에 맞춤(세로·가로 잘림 방지)
                fit = min(1.0, cw / bw, ch / bh)
                total = base * fit
                nw, nh = max(1, int(w * total)), max(1, int(h * total))
                small = cv2.resize(vis, (nw, nh), interpolation=cv2.INTER_AREA)
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                pil = Image.fromarray(rgb)
                self._photo = ImageTk.PhotoImage(image=pil)
                self._canvas.delete("all")
                x = max(0, (cw - nw) // 2)
                y = max(0, (ch - nh) // 2)
                self._canvas.create_image(x, y, anchor=tk.NW, image=self._photo)
        else:
            self._preview_last_frame_seq = -1
        self.after(iv, self._tick_preview)

    def _poll_detection_ui(self) -> None:
        """무거운 감지는 워커 스레드 결과만 반영 (UI 멈춤 방지)."""
        if not self._running:
            return
        now = time.monotonic()
        cfg_resync_sec = 3.0
        if self._ui_cfg_dirty or now - self._last_cfg_poll_sync >= cfg_resync_sec:
            self._sync_cfg_from_ui()
            self._ui_cfg_dirty = False
            self._last_cfg_poll_sync = now

        if self._thread is not None:
            self._emit_focus_transition_to_arduino_if_needed()
            cap_err = self._thread.get_capture_error()
            with self._det_lock:
                triggered = self._last_det_triggered
                reason = self._last_det_reason
            if cap_err:
                short = cap_err if len(cap_err) <= 140 else cap_err[:137] + "…"
                self._set_main_status_line(f"캡처 실패 — {short}", error=True)
            elif triggered:
                self._set_main_status_line(
                    f"알림! ({reason}) — {time.strftime('%H:%M:%S')}"
                )
                self._try_alert_sound()
            elif self._stream_status_text:
                self._set_main_status_line(self._stream_status_text)
            self._was_triggered_last = triggered
        else:
            self._was_triggered_last = False
        self._refresh_traffic_status()
        self.after(50, self._poll_detection_ui)


def _frozen_exe_dir() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def _is_windows_elevated() -> bool:
    """Windows: 관리자(UAC 상승) 프로세스이면 True."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _windows_relaunch_as_admin() -> bool:
    """
    ShellExecuteW(..., \"runas\", ...) 로 동일 실행 파일을 관리자 권한으로 다시 띄운다.
    성공 시(>32) 새 프로세스가 시작되므로 호출 측에서 sys.exit(0) 할 것.
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
        script = str(Path(__file__).resolve())
        params = subprocess.list2cmdline([script, *sys.argv[1:]])

    cwd = os.getcwd()
    rc = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        exe,
        params if params else None,
        cwd,
        1,  # SW_SHOWNORMAL
    )
    try:
        return int(rc) > 32
    except (TypeError, ValueError):
        return False


def _maybe_prompt_windows_elevation() -> None:
    """관리자가 아니면 안내 후 확인 시 UAC로 재실행. 취소면 일반 권한으로 계속."""
    if sys.platform != "win32" or _is_windows_elevated():
        return
    ensure_pre_gui_init()
    root = tk.Tk()
    root.withdraw()
    try:
        if not messagebox.askokcancel(
            "관리자 권한",
            "지금은 일반 권한으로 실행 중입니다.\n\n"
            "「확인」을 누르면 UAC 창이 열린 뒤, 관리자 권한으로 다시 실행합니다.\n"
            "「취소」는 일반 권한으로 그대로 계속합니다.",
            parent=root,
        ):
            return
        if _windows_relaunch_as_admin():
            sys.exit(0)
        messagebox.showerror(
            "관리자 권한",
            "관리자 권한으로 다시 실행하지 못했습니다.\n"
            "(UAC에서 거부했거나 오류가 났을 수 있습니다.)",
            parent=root,
        )
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def _frozen_boot_log(msg: str) -> None:
    d = _frozen_exe_dir()
    if d is None:
        return
    try:
        p = d / "oddments_startup_log.txt"
        with p.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def main() -> None:
    d = _frozen_exe_dir()
    if d is not None:
        try:
            (d / "oddments_startup_log.txt").write_text(
                f"시작 {time.strftime('%H:%M:%S')}\n"
                "ONNX/OpenCV 로딩에 수십 초 걸릴 수 있습니다. Dock 아이콘만 보이면 잠시 기다려 주세요.\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    _set_process_display_name(APP_NAME)
    _maybe_prompt_windows_elevation()
    _frozen_boot_log("ensure_pre_gui_init …")
    ensure_pre_gui_init()
    _frozen_boot_log("Tk 앱 생성 …")
    app = OddmentsApp()
    _frozen_boot_log("mainloop 진입")
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        d = _frozen_exe_dir()
        if d is not None:
            try:
                (d / "oddments_fatal_error.txt").write_text(
                    traceback.format_exc(), encoding="utf-8"
                )
            except OSError:
                pass
        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            # console=False 빌드에서도 사용자가 터미널로 실행한 경우 메시지 표시
            try:
                print(
                    "치명적 오류가 발생했습니다. 실행 파일과 같은 폴더의 "
                    "oddments_fatal_error.txt 를 확인하세요.",
                    file=sys.stderr,
                )
            except OSError:
                pass
        raise
