"""
2·3단계 통합: 모니터·창 캡처 스레드 → 미리보기·OCR·감지가 동일 프레임 공유 + 알림음.
실행 시 창 제목·프로세스 표시 이름은 APP_NAME(기본 cyj).

실행 전:
  pip install -r requirements.txt

키워드 OCR: Tesseract / EasyOCR / RapidOCR 중 복수 선택 (UI).
  · Tesseract: https://github.com/UB-Mannheim/tesseract/wiki
  · EasyOCR: pip install easyocr (용량 큼, PyTorch 포함)
  · RapidOCR: pip install rapidocr-onnxruntime
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

import json
import subprocess
import threading
import traceback
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
    ALL_OCR_ENGINES,
    DEFAULT_OCR_ENGINE,
    DetectionConfig,
    OCR_VARIANT_GROUPS_DISABLED,
    OCR_VARIANT_UI_CHOICES,
    get_overlay_store,
    normalize_ocr_engine,
    ocr_runtime_ok,
    run_detection_with_overlays,
)
from preview_render import frame_with_overlays

if sys.platform == "win32":
    from arduino_serial_bridge import (
        ArduinoKeyBridge,
        bridge_supported,
        clear_key_bridge_debug_log,
        clear_received_serial_log,
        drain_received_serial_lines,
        drain_key_bridge_debug_lines,
        key_pick_choices,
        list_com_ports,
        parse_key_filter_spec,
        set_key_bridge_debug_logging,
    )
else:

    class ArduinoKeyBridge:  # type: ignore[no-redef]
        def __init__(self) -> None:
            pass

        def last_error(self) -> None:
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


def _app_writable_dir() -> Path:
    """PyInstaller exe 일 때 설정 JSON 은 실행 파일과 같은 폴더에 둔다."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_SETTINGS_FILE = _app_writable_dir() / "alert_settings.json"


def _initial_ocr_engines() -> tuple[str, ...]:
    """exe 배포본은 RapidOCR 이 번들됨. Tesseract 는 subprocess 비용이 커서 기본 선택을 RapidOCR 로 둔다."""
    if getattr(sys, "frozen", False):
        return (ENGINE_RAPIDOCR,)
    return (DEFAULT_OCR_ENGINE,)


# 창 제목·프로세스 표시 이름 (setproctitle, Windows 콘솔 제목)
APP_NAME = "cyj"

# 상단 바(캡처 소스·FPS·송출 시작/중지)가 잘리지 않도록 최소 크기
_MAIN_WIN_MIN_W = 1000
_MAIN_WIN_MIN_H = 480


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
    if not _SETTINGS_FILE.is_file():
        return {}
    try:
        with open(_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
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


class MapleAlertApp(tk.Tk):
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
        self._last_ocr_status_check = 0.0
        self._first_ocr_poll = True
        self._ocr_status_stale = True

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

        self._ocr_log_win: tk.Toplevel | None = None
        self._ocr_log_text: scrolledtext.ScrolledText | None = None
        self._ocr_log_stats_var: tk.StringVar | None = None
        self._ocr_log_after_id: str | None = None
        self._ocr_log_autoscroll_var = tk.BooleanVar(value=False)

        self._arduino_bridge = ArduinoKeyBridge()
        self._arduino_port_var = tk.StringVar(value="COM3")
        self._arduino_baud_var = tk.StringVar(value="115200")
        self._arduino_keys_var = tk.StringVar(value="F1,F2,F3")
        self._arduino_focus_event_enabled_var = tk.BooleanVar(value=False)
        self._arduino_focus_key_var = tk.StringVar(value="F8")
        self._arduino_status_var = tk.StringVar(value="")
        self._arduino_summary_var = tk.StringVar(value="")
        self._last_window_focus_state: bool | None = None

        self._build_ui()
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

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)

        src = ttk.LabelFrame(top, text="캡처·송출 소스", padding=(6, 4))

        self._src_mode = tk.StringVar(value="monitor")
        if window_pick_supported():
            ttk.Radiobutton(
                src,
                text="모니터 전체",
                variable=self._src_mode,
                value="monitor",
                command=self._on_src_mode_change,
            ).pack(side=tk.LEFT, padx=(0, 8))
            ttk.Radiobutton(
                src,
                text="프로세스(창) 지정",
                variable=self._src_mode,
                value="window",
                command=self._on_src_mode_change,
            ).pack(side=tk.LEFT, padx=(0, 12))
        else:
            ttk.Label(
                src,
                text="모니터만 지원 (창 선택은 Windows·macOS에서 가능)",
            ).pack(side=tk.LEFT)

        self._mon_label = ttk.Label(src, text="모니터 #")
        self._mon_label.pack(side=tk.LEFT)
        self._mon_var = tk.StringVar(value="1")
        self._mon_spin = ttk.Spinbox(
            src, from_=1, to=8, width=4, textvariable=self._mon_var
        )
        self._mon_spin.pack(side=tk.LEFT, padx=(4, 8))

        self._pick_btn = ttk.Button(
            src, text="창 선택…", command=self._open_window_picker, state=tk.DISABLED
        )
        self._pick_btn.pack(side=tk.LEFT, padx=(0, 8))
        self._pick_info_var = tk.StringVar(value="")
        self._pick_info = ttk.Label(src, textvariable=self._pick_info_var, width=42)
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

        mid = ttk.LabelFrame(self, text="송출 화면 (미리보기·OCR 동일)", padding=4)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self._canvas = tk.Canvas(mid, bg="#222", highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)

        bot = ttk.LabelFrame(self, text="감지 설정", padding=8)
        bot.pack(fill=tk.X, padx=8, pady=8)

        r1 = ttk.Frame(bot)
        r1.pack(fill=tk.X, pady=2)
        ttk.Label(r1, text="알림 키워드(쉼표 구분)").pack(side=tk.LEFT)
        self._kw_var = tk.StringVar(value="보스,레드")
        ttk.Entry(r1, textvariable=self._kw_var, width=40).pack(
            side=tk.LEFT, padx=8, fill=tk.X, expand=True
        )

        r1b = ttk.Frame(bot)
        r1b.pack(fill=tk.X, pady=2)
        ttk.Label(r1b, text="키워드 OCR 엔진 (복수 선택)").pack(side=tk.LEFT, anchor=tk.N)
        eng_row = ttk.Frame(r1b)
        eng_row.pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)
        self._ocr_engine_vars: dict[str, tk.BooleanVar] = {}
        for i, eng in enumerate(ALL_OCR_ENGINES):
            var = tk.BooleanVar(value=(eng == DEFAULT_OCR_ENGINE))
            self._ocr_engine_vars[eng] = var
            ttk.Checkbutton(
                eng_row,
                text=eng,
                variable=var,
                command=self._on_detection_cfg_changed,
            ).grid(row=i // 4, column=i % 4, sticky=tk.W, padx=(0, 10), pady=1)

        r1c = ttk.LabelFrame(bot, text="OCR 전처리 변형 (선택한 것만 사용)", padding=(6, 4))
        r1c.pack(fill=tk.X, pady=4)
        ttk.Label(
            r1c,
            text="전부 체크이면 모든 변형을 사용합니다. "
            "체크가 하나도 없으면 키워드 OCR(전처리 변형)은 호출하지 않습니다. "
            "일부만 쓰려면 해당 항목만 체크하세요.",
            foreground="gray",
            font=("", 8),
        ).pack(anchor=tk.W)
        self._ocr_variant_group_vars: dict[str, tk.BooleanVar] = {}
        vgrid = ttk.Frame(r1c)
        vgrid.pack(fill=tk.X, pady=(4, 0))
        _cols = 3
        for i, (vid, vlabel) in enumerate(OCR_VARIANT_UI_CHOICES):
            var = tk.BooleanVar(value=True)
            self._ocr_variant_group_vars[vid] = var
            rr, cc = divmod(i, _cols)
            ttk.Checkbutton(
                vgrid,
                text=vlabel,
                variable=var,
                command=self._on_detection_cfg_changed,
            ).grid(row=rr, column=cc, sticky=tk.W, padx=(0, 12), pady=2)

        r3 = ttk.Frame(bot)
        r3.pack(fill=tk.X, pady=2)
        ttk.Label(r3, text="템플릿 경로").pack(side=tk.LEFT, anchor=tk.N)
        tpl_col = ttk.Frame(r3)
        tpl_col.pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)
        self._tpl_var = tk.StringVar(value="")
        ttk.Entry(tpl_col, textvariable=self._tpl_var).pack(
            side=tk.TOP, fill=tk.X, expand=True
        )
        ttk.Label(
            tpl_col,
            text="여러 장: 세미콜론(;)으로 구분 · 미리보기(캡처)와 같은 해상도로 잘라 저장",
            foreground="gray",
            font=("", 8),
        ).pack(side=tk.TOP, anchor=tk.W)
        tpl_btns = ttk.Frame(r3)
        tpl_btns.pack(side=tk.LEFT)
        ttk.Button(tpl_btns, text="추가…", command=self._browse_template).pack(
            side=tk.TOP, pady=1
        )
        ttk.Button(tpl_btns, text="비우기", command=lambda: self._tpl_var.set("")).pack(
            side=tk.TOP, pady=1
        )

        r4 = ttk.Frame(bot)
        r4.pack(fill=tk.X, pady=4)
        ttk.Label(r4, text="매칭 임계값").pack(side=tk.LEFT)
        self._th_var = tk.StringVar(value="0.80")
        ttk.Spinbox(
            r4, from_=0.5, to=0.99, increment=0.01, width=6, textvariable=self._th_var
        ).pack(side=tk.LEFT, padx=8)
        ttk.Label(r4, text="알림 쿨다운(초)").pack(side=tk.LEFT, padx=(16, 0))
        self._cd_var = tk.StringVar(value="3")
        ttk.Spinbox(r4, from_=1, to=60, width=4, textvariable=self._cd_var).pack(
            side=tk.LEFT, padx=8
        )

        r5 = ttk.Frame(bot)
        r5.pack(fill=tk.X, pady=4)
        self._show_overlay_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            r5,
            text="감지 영역 박스 표시 (영역마다 다른 색 테두리)",
            variable=self._show_overlay_var,
        ).pack(side=tk.LEFT)
        if sys.platform == "win32":
            ttk.Button(
                r5,
                text="Arduino 키 전송…",
                command=self._open_arduino_settings_window,
            ).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(r5, text="OCR 로그…", command=self._open_ocr_log_window).pack(
            side=tk.RIGHT, padx=(4, 0)
        )
        ttk.Label(r5, text=f"저장 위치: {_SETTINGS_FILE.name}", foreground="gray").pack(
            side=tk.RIGHT, padx=8
        )

        self._status = ttk.Label(
            self,
            text="대기 중 — 「송출 시작」을 누르세요.",
            anchor=tk.W,
            wraplength=900,
        )
        self._status.pack(fill=tk.X, padx=10, pady=(0, 2))
        self._ocr_status = ttk.Label(self, text="", anchor=tk.W, wraplength=900)
        _ocr_bottom = 2 if sys.platform == "win32" else 8
        self._ocr_status.pack(fill=tk.X, padx=10, pady=(0, _ocr_bottom))
        if sys.platform == "win32":
            self._arduino_main_line = ttk.Label(
                self,
                textvariable=self._arduino_summary_var,
                foreground="gray",
                font=("", 8),
                anchor=tk.W,
                wraplength=900,
            )
            self._arduino_main_line.pack(fill=tk.X, padx=10, pady=(0, 8))

        self._register_ui_cfg_dirty_traces()

    def _register_ui_cfg_dirty_traces(self) -> None:
        """키워드·템플릿·임계값·쿨다운 변경 시에만 감지 설정을 다시 맞추도록 표시."""

        def _mark(*_args: object) -> None:
            self._ui_cfg_dirty = True

        for var in (
            self._kw_var,
            self._tpl_var,
            self._th_var,
            self._cd_var,
        ):
            var.trace_add("write", _mark)

    def _effective_capture_fps(self) -> float:
        """
        사용자 캡처 FPS 와 감지 주기를 맞춰 불필요하게 빠른 grab 을 줄인다.
        min(사용자 FPS, max(10, 2 * (1000/감지주기ms))) — 미리보기는 최소 ~10fps 유지.
        """
        try:
            u = float(self._fps_var.get())
        except ValueError:
            u = 20.0
        u = max(5.0, min(60.0, u))
        d = max(150, int(self._detect_every_ms))
        detect_hz_budget = 2.0 * (1000.0 / float(d))
        cap = max(10.0, detect_hz_budget)
        return min(u, cap)

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
        dlg.grab_set()

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
        if "ocr_engines" in d and isinstance(d["ocr_engines"], list) and d["ocr_engines"]:
            want = {
                n
                for x in d["ocr_engines"]
                if x and str(x).strip()
                for n in (normalize_ocr_engine(str(x)),)
                if n
            }
            for eng in ALL_OCR_ENGINES:
                self._ocr_engine_vars[eng].set(eng in want)
        elif "ocr_engine" in d:
            v = normalize_ocr_engine(str(d["ocr_engine"]))
            for eng in ALL_OCR_ENGINES:
                self._ocr_engine_vars[eng].set(eng == v)
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
            if ad.get("focus_event_key"):
                self._arduino_focus_key_var.set(str(ad.get("focus_event_key")))
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
            self._ocr_status_stale = True
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
            "focus_event_key": str(self._arduino_focus_key_var.get()).strip(),
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

    # 포커스 이벤트용 키 토큰을 VK로 해석하고 유효성을 점검한다.
    def _focus_event_vk(self) -> int | None:
        token = str(self._arduino_focus_key_var.get()).strip()
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
        now_focused = _win32_foreground_hwnd() == int(self._picked_hwnd)
        last = self._last_window_focus_state
        self._last_window_focus_state = now_focused
        if last is None or last == now_focused:
            return
        if not bool(self._arduino_focus_event_enabled_var.get()):
            return
        if not self._arduino_bridge.is_active():
            return
        vk = self._focus_event_vk()
        if vk is None:
            self._arduino_status_var.set(
                "포커스 이벤트 키 설정 오류: 셀렉트박스에서 키 1개를 선택하세요."
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
        dlg.title("Arduino 키 전송 설정")
        dlg.geometry("580x640")
        dlg.minsize(500, 520)
        dlg.transient(self)
        dlg.grab_set()
        port_desc_var = tk.StringVar(value="")

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

        focus_row = ttk.Frame(top)
        focus_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Checkbutton(
            focus_row,
            text="선택 창 포커스 획득/해제 시 키 전송",
            variable=self._arduino_focus_event_enabled_var,
        ).pack(side=tk.LEFT)
        ttk.Label(focus_row, text="전송 키").pack(side=tk.LEFT, padx=(12, 4))
        focus_key_combo = ttk.Combobox(
            focus_row,
            textvariable=self._arduino_focus_key_var,
            values=key_pick_choices(),
            width=16,
            state="readonly",
        )
        focus_key_combo.pack(side=tk.LEFT)
        if str(self._arduino_focus_key_var.get()).strip() not in set(key_pick_choices()):
            self._arduino_focus_key_var.set("F8")

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

        bar = ttk.Frame(dlg, padding=(10, 0, 10, 10))
        bar.pack(fill=tk.X, side=tk.BOTTOM)

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
        return tuple(
            normalize_ocr_engine(eng)
            for eng in ALL_OCR_ENGINES
            if self._ocr_engine_vars[eng].get()
        )

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
        tpls = _parse_template_paths(self._tpl_var.get())
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
        self._ui_cfg_dirty = False
        self._last_cfg_poll_sync = time.monotonic()
        self._ocr_status_stale = True
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
            jt.join(timeout=0.8)

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
        self._thread = CaptureThread(
            monitor_index=mon,
            target_fps=fps,
            window_hwnd=hwnd,
        )
        self._thread.start()
        self._det_stop.clear()
        self._det_cfg_wake.clear()
        self._det_kw_abort.clear()
        self._det_thread = threading.Thread(
            target=self._detection_worker_loop,
            daemon=True,
            name="MapleAlert-Detect",
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
            self._status.config(text=self._stream_status_text)
        else:
            self._stream_status_text = f"송출 중 — 모니터 {mon}, {fps_txt}"
            self._status.config(text=self._stream_status_text)

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
        self._last_window_focus_state = None
        self._status.config(text="중지됨")

        def join_bg() -> None:
            if t_det is not None and t_det.is_alive():
                t_det.join(timeout=0.4)
            if t_cap is not None:
                t_cap.stop()
                t_cap.join(timeout=0.4)

        self._bg_join_thread = threading.Thread(
            target=join_bg, daemon=True, name="MapleAlert-StopJoin"
        )
        self._bg_join_thread.start()

    def _cancel_ocr_log_polling(self) -> None:
        if self._ocr_log_after_id is not None:
            try:
                self.after_cancel(self._ocr_log_after_id)
            except (tk.TclError, ValueError):
                pass
            self._ocr_log_after_id = None

    def _open_ocr_log_window(self) -> None:
        if self._ocr_log_win is not None:
            try:
                if self._ocr_log_win.winfo_exists():
                    self._ocr_log_win.lift()
                    self._ocr_log_win.focus_force()
                    return
            except tk.TclError:
                pass

        win = tk.Toplevel(self)
        win.title("OCR 로그")
        win.geometry("920x440")
        win.transient(self)

        top = ttk.Frame(win, padding=6)
        top.pack(fill=tk.X)
        self._ocr_log_stats_var = tk.StringVar(
            value="누적 OCR API 호출: 0회 (감지 스레드에서 기록)"
        )
        ttk.Label(top, textvariable=self._ocr_log_stats_var).pack(side=tk.LEFT)
        ttk.Checkbutton(
            top,
            text="맨 아래 자동 스크롤",
            variable=self._ocr_log_autoscroll_var,
        ).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Button(top, text="통계 초기화", command=self._ocr_log_reset).pack(
            side=tk.RIGHT, padx=4
        )
        ttk.Button(top, text="창 비우기", command=self._ocr_log_clear_view).pack(
            side=tk.RIGHT
        )

        body = ttk.Frame(win, padding=(6, 0, 6, 6))
        body.pack(fill=tk.BOTH, expand=True)
        st = scrolledtext.ScrolledText(
            body,
            height=18,
            font=("Consolas", 9),
            wrap=tk.NONE,
        )
        st.pack(fill=tk.BOTH, expand=True)
        st.insert(
            tk.END,
            "# 번호 | 시각 | 엔진 | 작업 | 소요(ms) | …  (OCR API 호출·응답)\n"
            "# * 로 시작: 다운로드·업데이트·캐시·실패·안내·프로세스 (예: tesseract.exe 자식 실행)\n"
            "# Tesseract: image_to_data / image_to_string 각 호출 1줄 (언어 폴백 시 여러 줄).\n"
            "# EasyOCR: readtext, RapidOCR: infer.\n\n",
        )
        self._ocr_log_text = st
        self._ocr_log_win = win

        def on_close() -> None:
            self._cancel_ocr_log_polling()
            self._ocr_log_win = None
            self._ocr_log_text = None
            self._ocr_log_stats_var = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self._ocr_log_after_id = self.after(100, self._tick_ocr_log_ui)

    def _tick_ocr_log_ui(self) -> None:
        self._ocr_log_after_id = None
        if self._ocr_log_text is None or self._ocr_log_win is None:
            return
        try:
            if not self._ocr_log_win.winfo_exists():
                self._ocr_log_text = None
                self._ocr_log_win = None
                return
        except tk.TclError:
            self._ocr_log_text = None
            self._ocr_log_win = None
            return

        from detection.ocr_diag import drain_ocr_log_lines, get_ocr_call_total

        if self._ocr_log_stats_var is not None:
            self._ocr_log_stats_var.set(
                f"누적 OCR API 호출: {get_ocr_call_total()}회"
            )

        for line in drain_ocr_log_lines(250):
            self._ocr_log_text.insert(tk.END, line)

        try:
            end_line = int(float(self._ocr_log_text.index("end-1c").split(".")[0]))
            if end_line > 4000:
                self._ocr_log_text.delete("1.0", "1500.0")
        except (tk.TclError, ValueError):
            pass

        if self._ocr_log_autoscroll_var.get():
            self._ocr_log_text.see(tk.END)
        self._ocr_log_after_id = self.after(120, self._tick_ocr_log_ui)

    def _ocr_log_clear_view(self) -> None:
        if self._ocr_log_text is None:
            return
        self._ocr_log_text.delete("1.0", tk.END)
        self._ocr_log_text.insert(
            tk.END,
            "# 화면만 비움. 카운터·큐 초기화는 「통계 초기화」.\n\n",
        )

    def _ocr_log_reset(self) -> None:
        from detection.ocr_diag import reset_ocr_log

        reset_ocr_log()
        if self._ocr_log_stats_var is not None:
            self._ocr_log_stats_var.set("누적 OCR API 호출: 0회 (초기화됨)")

    def _on_close(self) -> None:
        from detection.ocr_diag import set_ocr_keyword_alert_sound_handler

        self._persist_app_settings(show_error_dialog=True)
        set_ocr_keyword_alert_sound_handler(None)
        self._running = False
        self._cancel_ocr_log_polling()
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
        ocr_status_resync_sec = 3.0
        if self._ui_cfg_dirty or now - self._last_cfg_poll_sync >= cfg_resync_sec:
            self._sync_cfg_from_ui()
            self._ui_cfg_dirty = False
            self._last_cfg_poll_sync = now

        refresh_ocr = (
            self._first_ocr_poll
            or self._ocr_status_stale
            or (now - self._last_ocr_status_check >= ocr_status_resync_sec)
        )
        if refresh_ocr:
            self._first_ocr_poll = False
            self._ocr_status_stale = False
            self._last_ocr_status_check = now
            selected = self._ocr_engines_for_cfg()
            if not selected:
                self._ocr_status.config(
                    text="키워드 OCR — 엔진 미선택 (키워드 감지 안 함)",
                    foreground="#a63",
                )
            else:
                parts: list[str] = []
                all_ok = True
                for eng in selected:
                    o_ok, o_msg = ocr_runtime_ok(eng)
                    if not o_ok:
                        all_ok = False
                    if o_ok:
                        parts.append(f"{eng}: 사용 가능")
                    else:
                        hint = " (전문은 OCR 로그)"
                        room = max(24, 72 - len(hint))
                        short = (
                            o_msg
                            if len(o_msg) <= room
                            else o_msg[: max(1, room - 1)] + "…" + hint
                        )
                        parts.append(f"{eng}: {short}")
                self._ocr_status.config(
                    text="키워드 OCR — " + " · ".join(parts),
                    foreground="gray" if all_ok else "#a63",
                )
        if self._thread is not None:
            self._emit_focus_transition_to_arduino_if_needed()
            cap_err = self._thread.get_capture_error()
            with self._det_lock:
                triggered = self._last_det_triggered
                reason = self._last_det_reason
            if cap_err:
                short = cap_err if len(cap_err) <= 140 else cap_err[:137] + "…"
                self._status.config(text=f"캡처 실패 — {short}", foreground="#a63")
            elif triggered:
                self._status.config(
                    text=f"알림! ({reason}) — {time.strftime('%H:%M:%S')}"
                )
                self._try_alert_sound()
            elif self._stream_status_text:
                self._status.config(text=self._stream_status_text)
            self._was_triggered_last = triggered
        else:
            self._was_triggered_last = False
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
        p = d / "cyj_startup_log.txt"
        with p.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def main() -> None:
    d = _frozen_exe_dir()
    if d is not None:
        try:
            (d / "cyj_startup_log.txt").write_text(
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
    app = MapleAlertApp()
    _frozen_boot_log("mainloop 진입")
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        d = _frozen_exe_dir()
        if d is not None:
            try:
                (d / "cyj_fatal_error.txt").write_text(
                    traceback.format_exc(), encoding="utf-8"
                )
            except OSError:
                pass
        if getattr(sys, "frozen", False) and sys.platform == "darwin":
            # console=False 빌드에서도 사용자가 터미널로 실행한 경우 메시지 표시
            try:
                print(
                    "치명적 오류가 발생했습니다. 실행 파일과 같은 폴더의 "
                    "cyj_fatal_error.txt 를 확인하세요.",
                    file=sys.stderr,
                )
            except OSError:
                pass
        raise
