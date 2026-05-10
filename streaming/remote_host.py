"""원격 호스트: 단일 모니터 캡처 → WebRTC 송출 + DataChannel 로 입력 수신."""

from __future__ import annotations

import asyncio
import hmac
import json
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
from .web_stream import SharedVideoBuffer, SharedVideoTrack, _even_dims_bgr

_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="remote-input")


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


def _inject_input_message(
    raw: str,
    rect: tuple[int, int, int, int],
) -> None:
    """JSON 한 줄을 파싱해 마우스·키보드를 주입한다."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(msg, dict):
        return
    t = str(msg.get("t", "")).lower()
    left, top, w, h = rect
    try:
        from pynput.keyboard import Key  # type: ignore[import-untyped]
        from pynput.keyboard import Controller as KbdCtrl
        from pynput.mouse import Button, Controller as MouseCtrl
    except ImportError:
        return

    mouse = MouseCtrl()
    kbd = KbdCtrl()

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
        if down:
            kbd.press(key_obj)
        else:
            kbd.release(key_obj)

    if t == "move":
        try:
            nx = float(msg.get("nx", 0.0))
            ny = float(msg.get("ny", 0.0))
        except (TypeError, ValueError):
            return
        nx = max(0.0, min(1.0, nx))
        ny = max(0.0, min(1.0, ny))
        ax = int(left + nx * float(w))
        ay = int(top + ny * float(h))
        mouse.position = (ax, ay)
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
        if down:
            mouse.press(b)
        else:
            mouse.release(b)
        return

    if t == "scroll":
        try:
            dx = int(msg.get("dx", 0))
            dy = int(msg.get("dy", 0))
        except (TypeError, ValueError):
            return
        mouse.scroll(dx, dy)
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
        _POOL.submit(_inject_input_message, raw, geom)
        return
    if not isinstance(msg, dict):
        _POOL.submit(_inject_input_message, raw, geom)
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
    _POOL.submit(_inject_input_message, raw, geom)


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
    ) -> None:
        self.host = str(host).strip() or "0.0.0.0"
        self.port = int(port)
        self.fps = float(max(5.0, min(60.0, fps)))
        self.monitor_index = int(monitor_index)
        self.capture_width = max(0, int(capture_width))
        self.capture_height = max(0, int(capture_height))
        self._rtc_configuration = rtc_configuration or RTCConfiguration()
        self._auth_token = (auth_token or "").strip()
        self._h264_hardware_encode = bool(h264_hardware_encode)
        self._h264_patch_applied = False

        self.video = SharedVideoBuffer()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pcs: set[RTCPeerConnection] = set()
        self._runner: web.AppRunner | None = None
        self._site: web.BaseSite | None = None

        self._capture: CaptureThread | None = None
        self._capture_lock = threading.Lock()
        self._geom: Optional[tuple[int, int, int, int]] = None
        self._meta_pending: set[object] = set()

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

    def _push_frame(self, frame: np.ndarray) -> None:
        try:
            out = _prepare_frame(
                frame,
                capture_width=self.capture_width,
                capture_height=self.capture_height,
            )
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

    def _ensure_geom(self) -> None:
        """첫 연결 시 모니터 기하만 확보(캡처와 분리)."""
        if self._geom is not None:
            return
        geom = _monitor_geometry(self.monitor_index)
        if geom is None:
            raise RuntimeError(
                f"모니터 #{self.monitor_index} 을(를) 찾을 수 없습니다."
            )
        self._geom = geom

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
            try:
                log_remote_event(
                    f"호스트: 화면 캡처 시작 (모니터 #{self.monitor_index})"
                )
            except Exception:
                pass
            self._capture = CaptureThread(
                monitor_index=self.monitor_index,
                target_fps=self.fps,
                on_frame=self._push_frame,
                window_hwnd=None,
            )
            self._capture.start()

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
        cap = self._capture
        self._capture = None
        if cap is not None:
            try:
                cap.stop()
                cap.join(timeout=2.0)
            except Exception:
                pass

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

        geom = self._geom

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
                if geom is None:
                    return
                raw = (
                    message.decode("utf-8", errors="ignore")
                    if isinstance(message, (bytes, bytearray))
                    else str(message)
                )
                _POOL.submit(_dispatch_dc_payload, srv, raw, geom, channel)

        pc.on("datachannel", _on_datachannel)

        await pc.setRemoteDescription(offer)
        pc.addTrack(
            SharedVideoTrack(
                self.video,
                fps=self.fps,
                max_stream_side=0,
            )
        )
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
