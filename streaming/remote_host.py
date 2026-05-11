"""원격 호스트: 단일 모니터 캡처 → WebRTC 송출 + DataChannel 로 입력 수신."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import cv2
import numpy as np
from aiohttp import web
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

from capture.thread import CaptureThread, enumerate_monitors

from .remote_log import log_remote_event
from .remote_presets import preset_dimensions
from .web_stream import (
    SharedAudioBuffer,
    SharedAudioTrack,
    SharedVideoBuffer,
    SharedVideoTrack,
    _even_dims_bgr,
)

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="remote-input")

# pynput 컨트롤러는 프로세스당 하나만 쓴다. 매 DataChannel 메시지마다 새로 만들면
# 특히 macOS Quartz 경로에서 호버 폭주 시 멈춤·지연을 유발하기 쉽다.
_pynput_singleton_lock = threading.Lock()
_mouse_controller: object | None = None
_kbd_controller: object | None = None
_move_dedup_lock = threading.Lock()
_last_move_pixel: list[tuple[int, int] | None] = [None]
_inject_failure_logged = False


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


def rtc_configuration_from_stun_turn(
    *,
    stun_urls: str,
    turn_uri: str,
    turn_username: str,
    turn_password: str,
) -> RTCConfiguration:
    servers: list[RTCIceServer] = []
    raw = (stun_urls or "").replace(",", "\n")
    for line in raw.splitlines():
        u = line.strip()
        if u:
            servers.append(RTCIceServer(urls=[u]))
    tu = (turn_uri or "").strip()
    if tu:
        servers.append(
            RTCIceServer(
                urls=[tu],
                username=(turn_username or None),
                credential=(turn_password or None),
            )
        )
    return RTCConfiguration(iceServers=servers)


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
    """메인 디스플레이 픽셀 크기 (가상 디스플레이 host_native 용)."""
    if sys.platform != "darwin":
        return 1920, 1080
    try:
        from AppKit import NSScreen  # type: ignore[import-untyped]

        scr = NSScreen.mainScreen()
        if scr is None:
            return 1920, 1080
        f = scr.frame()
        scale = float(scr.backingScaleFactor())
        w = max(320, int(round(float(f.size.width) * scale)))
        h = max(240, int(round(float(f.size.height) * scale)))
        return w, h
    except Exception:
        return 1920, 1080


def _mss_index_for_rect(
    left: int,
    top: int,
    width: int,
    height: int,
    *,
    tol: int = 16,
) -> Optional[int]:
    """mss 모니터 목록에서 좌표가 일치하는 인덱스."""
    for m in enumerate_monitors():
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
            return cv2.resize(bgr, (ew, eh), interpolation=cv2.INTER_AREA)
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
        _press_key(k, down)


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
    if t == "resolution":
        preset = str(msg.get("preset", "")).strip()
        if preset:
            srv._schedule_resolution_change(preset)
        return
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
        capture_width: int = 0,
        capture_height: int = 0,
        rtc_configuration: RTCConfiguration | None = None,
        auth_token: str = "",
        h264_hardware_encode: bool = False,
        virtual_display_enabled: bool = False,
        resolution_preset: str = "host_native",
        darwin_audio_device: str = "",
    ) -> None:
        self.host = str(host).strip() or "0.0.0.0"
        self.port = int(port)
        self.fps = float(max(5.0, min(60.0, fps)))
        self.monitor_index = int(monitor_index)
        self._capture_monitor_index = int(monitor_index)
        self.capture_width = max(0, int(capture_width))
        self.capture_height = max(0, int(capture_height))
        self._rtc_configuration = rtc_configuration or RTCConfiguration()
        self._auth_token = (auth_token or "").strip()
        self._h264_hardware_encode = bool(h264_hardware_encode)
        self._h264_patch_applied = False

        self._virtual_display_enabled = bool(virtual_display_enabled)
        self._resolution_preset_id = (resolution_preset or "host_native").strip()
        self._darwin_audio_device = (darwin_audio_device or "").strip()
        self._vd_obj: object | None = None
        self._vd_display_id: int = 0

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

        self._capture: CaptureThread | None = None
        self._capture_lock = threading.Lock()
        self._geom: Optional[tuple[int, int, int, int]] = None
        self._meta_pending: set[object] = set()
        # 맥 Retina: mss/monitor geom 은 물리 픽셀 → pynput 는 논리 포인트일 때 나눔.
        self._pointer_scale: float = 1.0
        # _prepare_frame·짝수 맞춤 후 송출 버퍼 (뷰어 정규화와 주입 스팬 일치)
        self._last_frame_wh: tuple[int, int] = (0, 0)

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

    def _release_virtual_display_session(self) -> None:
        if self._vd_obj is None:
            return
        try:
            from app_platform.darwin_virtual_display import release_virtual_display

            release_virtual_display(self._vd_obj)
        except Exception:
            pass
        self._vd_obj = None
        self._vd_display_id = 0

    def _ensure_geom_virtual_display(self) -> None:
        from app_platform.darwin_virtual_display import (
            DarwinVirtualDisplayError,
            cg_display_bounds,
            create_virtual_display,
        )

        w, h = preset_dimensions(
            self._resolution_preset_id,
            host_native=_darwin_main_pixel_size,
        )
        w = max(320, int(w))
        h = max(240, int(h))
        try:
            vd, did = create_virtual_display(w, h, refresh_hz=60.0)
        except DarwinVirtualDisplayError as exc:
            raise RuntimeError(str(exc)) from exc
        self._vd_obj = vd
        self._vd_display_id = int(did)
        bx, by, bw, bh = cg_display_bounds(int(did))
        left = int(round(bx))
        top = int(round(by))
        gw = max(1, int(round(bw)))
        gh = max(1, int(round(bh)))
        idx = _mss_index_for_rect(left, top, gw, gh)
        if idx is None:
            self._release_virtual_display_session()
            raise RuntimeError(
                "가상 디스플레이가 생성되었으나 mss 모니터 목록과 매칭되지 않았습니다."
            )
        self._capture_monitor_index = idx
        self._geom = (left, top, gw, gh)
        self._pointer_scale = 1.0
        try:
            log_remote_event(
                f"호스트: 가상 디스플레이 {gw}×{gh} (mss #{idx}, CG ID {did})"
            )
        except Exception:
            pass

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

    def _blocking_restart_resolution(self, preset_id: str) -> None:
        """세션 유지 상태에서 해상도(프리셋)만 교체. 짧은 끊김 허용."""
        pid = (preset_id or "").strip() or "host_native"
        self._resolution_preset_id = pid
        with self._capture_lock:
            cap = self._capture
            self._capture = None
        if cap is not None:
            try:
                cap.stop()
                cap.join(timeout=3.0)
            except Exception:
                pass
        self._stop_darwin_audio()
        if sys.platform == "darwin" and self._virtual_display_enabled:
            self._release_virtual_display_session()
        self._geom = None
        self._pointer_scale = 1.0
        try:
            self._ensure_geom()
            self._ensure_capture()
        except Exception as exc:
            try:
                log_remote_event(f"호스트: 해상도 변경 실패 — {exc}", error=True)
            except Exception:
                pass

    def _schedule_resolution_change(self, preset_id: str) -> None:
        loop = self._loop
        if loop is None:
            return

        async def _coro() -> None:
            exe_loop = asyncio.get_running_loop()
            await exe_loop.run_in_executor(
                _POOL,
                self._blocking_restart_resolution,
                preset_id,
            )

        try:
            asyncio.run_coroutine_threadsafe(_coro(), loop)
        except Exception:
            pass

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
            all_m = list(sc.all_microphones(include_loopback=True))
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
            if (
                sys.platform == "darwin"
                and self._geom is not None
                and self._pointer_scale <= 1.01
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

    def _stop_capture_if_idle(self) -> None:
        """활성 피어가 없으면 캡처를 멈춰 CPU 를 돌려준다."""
        with self._capture_lock:
            if len(self._pcs) > 0:
                return
            cap = self._capture
            self._capture = None
        if cap is not None:
            try:
                log_remote_event("호스트: 뷰어 없음 — 캡처 중지")
            except Exception:
                pass
            try:
                cap.stop()
                cap.join(timeout=2.0)
            except Exception:
                pass
        self._stop_darwin_audio()
        if sys.platform == "darwin" and self._virtual_display_enabled:
            self._release_virtual_display_session()
            self._geom = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="Oddments-RemoteHost",
        )
        self._thread.start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self._loop is not None:
                return
            time.sleep(0.03)
        raise RuntimeError("원격 호스트 서버 시작 시간 초과.")

    def stop(self) -> None:
        self._stop_darwin_audio()
        cap = self._capture
        self._capture = None
        if cap is not None:
            try:
                cap.stop()
                cap.join(timeout=2.0)
            except Exception:
                pass
        if sys.platform == "darwin" and self._virtual_display_enabled:
            self._release_virtual_display_session()
        self._geom = None

        loop = self._loop
        if loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self._shutdown_async(), loop)
            try:
                fut.result(timeout=5.0)
            except Exception:
                pass
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        self._loop = None
        self._pcs.clear()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._startup_async())
        except OSError as exc:
            try:
                print(f"[원격호스트] 바인드 실패 {self.host}:{self.port}: {exc}", flush=True)
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
            return
        self._loop = loop
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
        try:
            log_remote_event(
                f"호스트: 신호 서버 대기 http://{self.host}:{self.port}/offer"
            )
        except Exception:
            pass

    async def _shutdown_async(self) -> None:
        pcs = list(self._pcs)
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

    async def _handle_offer(self, req: web.Request) -> web.Response:
        params = await req.json()
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

        try:
            self._ensure_capture()
        except RuntimeError as exc:
            try:
                log_remote_event(f"호스트: 캡처 불가 — {exc}", error=True)
            except Exception:
                pass
            return web.Response(
                status=503,
                text=json.dumps({"error": str(exc)}, ensure_ascii=False),
                content_type="application/json",
            )

        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        pc = RTCPeerConnection(configuration=self._rtc_configuration)
        self._pcs.add(pc)

        @pc.on("connectionstatechange")
        async def _on_state_change() -> None:
            st = pc.connectionState
            if st in ("failed", "closed", "disconnected"):
                self._pcs.discard(pc)
                try:
                    log_remote_event(
                        f"호스트: 피어 {st} (남은 연결 {len(self._pcs)})",
                        error=(st == "failed"),
                    )
                except Exception:
                    pass
                try:
                    await pc.close()
                except Exception:
                    pass
                self._stop_capture_if_idle()

        srv = self

        def _on_datachannel(channel) -> None:
            if getattr(channel, "label", "") != "input":
                return
            srv._meta_pending.add(channel)

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
        try:
            log_remote_event(
                f"호스트: SDP 답변 전송 (활성 피어 {len(self._pcs)})"
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
    "rtc_configuration_from_stun_turn",
]
