"""원격 뷰어: WebRTC 수신 + DataChannel 입력 송신 (asyncio)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable, Optional

import cv2
import numpy as np
from aiohttp import ClientSession, ClientTimeout
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError

from .remote_log import log_remote_event

logger = logging.getLogger(__name__)

OnFrame = Callable[[np.ndarray], None]
OnState = Callable[[str], None]
OnDcJson = Callable[[dict], None]


def _video_frame_to_rgb(frame: object) -> Optional[np.ndarray]:
    """수신 VideoFrame → RGB uint8 (H.264 디코더·포맷 차이 대비)."""
    vf = frame
    to_nd = getattr(vf, "to_ndarray", None)
    if not callable(to_nd):
        return None
    for fmt in ("rgb24", "bgr24"):
        try:
            arr = to_nd(format=fmt)
        except Exception:
            continue
        if arr is None or not getattr(arr, "size", 0):
            continue
        try:
            if arr.ndim == 2:
                return np.stack([arr, arr, arr], axis=2)
            if arr.ndim != 3 or arr.shape[2] != 3:
                continue
            if fmt == "bgr24":
                return np.ascontiguousarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
            return np.ascontiguousarray(arr)
        except Exception:
            continue
    reformat = getattr(vf, "reformat", None)
    if callable(reformat):
        try:
            w = int(getattr(vf, "width", 0) or 0)
            h = int(getattr(vf, "height", 0) or 0)
            if w > 0 and h > 0:
                rf = reformat(width=w, height=h, format="rgb24")
                arr = rf.to_ndarray()
                if arr is not None and getattr(arr, "size", 0) and arr.ndim == 3:
                    return np.ascontiguousarray(arr)
        except Exception:
            pass
    return None


class RemoteViewerSession:
    def __init__(
        self,
        *,
        signal_host: str,
        signal_port: int,
        rtc_configuration: RTCConfiguration,
        on_frame: OnFrame,
        on_state: OnState | None = None,
        on_dc_json: OnDcJson | None = None,
        auth_token: str = "",
    ) -> None:
        self._host = (signal_host or "").strip() or "127.0.0.1"
        self._port = int(signal_port)
        self._rtc_configuration = rtc_configuration
        self._on_frame = on_frame
        self._on_state = on_state
        self._on_dc_json = on_dc_json
        self._auth_token = (auth_token or "").strip()

        self._pc: Optional[RTCPeerConnection] = None
        self._dc = None
        self._video_task: Optional[asyncio.Task[None]] = None
        self._stop = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._logged_ndarray_error = False

    def _emit(self, msg: str, *, error: bool = False) -> None:
        try:
            log_remote_event(f"뷰어: {msg}", error=error)
        except Exception:
            pass
        if self._on_state:
            try:
                self._on_state(msg)
            except Exception:
                pass

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._emit("연결 중…")
        offer_url = f"http://{self._host}:{self._port}/offer"

        self._pc = RTCPeerConnection(configuration=self._rtc_configuration)
        self._dc = self._pc.createDataChannel("input", ordered=True)
        self._pc.addTransceiver("video", direction="recvonly")

        @self._dc.on("message")
        def _on_dc_message(message: object) -> None:
            if self._on_dc_json is None:
                return
            raw = (
                message.decode("utf-8", errors="ignore")
                if isinstance(message, (bytes, bytearray))
                else str(message)
            )
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    self._on_dc_json(data)
            except (json.JSONDecodeError, TypeError):
                pass

        @self._pc.on("connectionstatechange")
        async def _on_conn_state() -> None:
            if self._pc is None:
                return
            st = self._pc.connectionState
            self._emit(f"피어: {st}", error=(st == "failed"))
            if st in ("failed", "closed", "disconnected"):
                self._stop.set()

        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)

        timeout = ClientTimeout(total=15)
        post_body: dict = {
            "sdp": self._pc.localDescription.sdp,
            "type": self._pc.localDescription.type,
        }
        if self._auth_token:
            post_body["token"] = self._auth_token

        try:
            async with ClientSession(timeout=timeout) as session:
                async with session.post(
                    offer_url,
                    json=post_body,
                ) as resp:
                    if resp.status == 401:
                        raise RuntimeError(
                            "연결 거부: 비밀번호가 호스트와 일치하지 않습니다."
                        )
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(
                            f"offer HTTP {resp.status}: {body[:200]}"
                        )
                    data = await resp.json()
        except Exception as exc:
            try:
                log_remote_event(f"뷰어: 신호(offer) 요청 실패 — {exc}", error=True)
            except Exception:
                pass
            self._emit(f"신호 실패: {exc}", error=True)
            await self._cleanup()
            return

        # setRemoteDescription(answer) 시점에 수신 트랙이 붙으므로, 반드시 그 전에 등록해야 한다.
        # 나중에 붙이면 track 이벤트를 놓쳐 영상이 영원히 안 온다.
        @self._pc.on("track")
        def _on_track(track) -> None:
            if track.kind != "video":
                return
            try:
                log_remote_event(f"뷰어: 비디오 트랙 수신 ({getattr(track, 'id', '?')})")
            except Exception:
                pass
            self._video_task = asyncio.create_task(
                self._drain_video(track),
                name="remote-video",
            )

        answer = RTCSessionDescription(sdp=data["sdp"], type=data["type"])
        await self._pc.setRemoteDescription(answer)
        self._emit("신호 완료, 미디어 대기…")

        await self._stop.wait()
        await self._cleanup()

    async def _drain_video(self, track) -> None:
        self._emit("영상 수신 중")
        while not self._stop.is_set():
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                continue
            except MediaStreamError:
                break
            except Exception:
                break
            rgb = _video_frame_to_rgb(frame)
            if rgb is not None and rgb.size:
                self._on_frame(rgb)
            elif not self._logged_ndarray_error:
                self._logged_ndarray_error = True
                try:
                    log_remote_event(
                        "뷰어: 영상 프레임을 RGB 로 변환하지 못했습니다. 코덱/디코더를 확인하세요.",
                        error=True,
                    )
                except Exception:
                    pass
                logger.warning(
                    "원격 영상 프레임 RGB 변환 실패 (포맷 시도 후 빈 결과)",
                )
        self._emit("영상 종료")

    async def _cleanup(self) -> None:
        self._stop.set()
        vt = self._video_task
        self._video_task = None
        if vt is not None and not vt.done():
            vt.cancel()
            try:
                await vt
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        pc = self._pc
        self._pc = None
        self._dc = None
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                pass

    async def close(self) -> None:
        self._stop.set()
        await self._cleanup()

    def request_close(self) -> None:
        """다른 스레드에서 세션을 종료할 때 (이벤트 루프로 마샬링)."""
        loop = self._loop
        if loop is None:
            return

        def _kick() -> None:
            self._stop.set()

        try:
            loop.call_soon_threadsafe(_kick)
        except Exception:
            pass

    def send_json(self, payload: dict) -> None:
        loop = self._loop
        dc = self._dc
        if loop is None or dc is None:
            return

        def _send() -> None:
            try:
                if getattr(dc, "readyState", "") != "open":
                    return
                dc.send(json.dumps(payload, separators=(",", ":")))
            except Exception:
                pass

        try:
            loop.call_soon_threadsafe(_send)
        except Exception:
            pass


def run_session_in_thread(
    *,
    signal_host: str,
    signal_port: int,
    rtc_configuration: RTCConfiguration,
    on_frame: OnFrame,
    on_state: OnState | None = None,
    on_dc_json: OnDcJson | None = None,
    auth_token: str = "",
) -> tuple[threading.Thread, Optional[RemoteViewerSession]]:
    holder: list[Optional[RemoteViewerSession]] = [None]

    def _runner() -> None:
        sess = RemoteViewerSession(
            signal_host=signal_host,
            signal_port=signal_port,
            rtc_configuration=rtc_configuration,
            on_frame=on_frame,
            on_state=on_state,
            on_dc_json=on_dc_json,
            auth_token=auth_token,
        )
        holder[0] = sess

        async def _go() -> None:
            await sess.run()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_go())
        finally:
            try:
                pend = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pend:
                    task.cancel()
                if pend:
                    loop.run_until_complete(
                        asyncio.gather(*pend, return_exceptions=True)
                    )
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

    t = threading.Thread(target=_runner, daemon=True, name="Oddments-RemoteViewer")
    t.start()
    deadline = time.time() + 5.0
    while holder[0] is None and time.time() < deadline:
        time.sleep(0.02)
    return t, holder[0]


__all__ = ["RemoteViewerSession", "run_session_in_thread", "OnDcJson"]
