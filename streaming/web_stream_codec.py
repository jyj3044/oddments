"""Web Stream 전용 WebRTC 비디오 코덱 설정 (원격 호스트 송출과 분리).

- A: H.264 비트레이트(해상도·FPS 연동) — ``remote_h264_bitrate`` 재사용
- B: SDP 협상 시 H.264 우선
- C: ``WebSharedVideoTrack`` 송신에만 GPU H.264 시도 (전역 HW 패치 미사용)
- D: ``WebSharedVideoTrack`` 송신 REMB 하향만 제한
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from aiortc import RTCPeerConnection
from aiortc.codecs import get_capabilities, get_encoder
from aiortc.mediastreams import AudioFrame, Frame, MediaStreamError
from aiortc.rtcrtpparameters import RTCRtpCodecCapability
from aiortc.rtcrtpsender import RTCEncodedFrame, RTCRtpSender
from aiortc.rtp import compute_audio_level_dbov

logger = logging.getLogger(__name__)

_WEB_HW_REQUESTED = False
_SENDER_PATCHES_INSTALLED = False
_ORIG_NEXT_ENCODED_FRAME: Callable[..., Any] | None = None
_ORIG_HANDLE_RTCP: Callable[..., Any] | None = None


def is_web_stream_hw_requested() -> bool:
    return bool(_WEB_HW_REQUESTED)


def apply_web_stream_codec_patches(*, fps: float, h264_hardware: bool) -> list[str]:
    """Web Stream 서버 시작 시 호출. 원격 호스트 ``install_h264_hardware_encoder`` 는 건드리지 않는다."""
    global _WEB_HW_REQUESTED

    messages: list[str] = []
    try:
        from streaming.remote_h264_bitrate import install_screen_share_h264_bitrate_patch

        install_screen_share_h264_bitrate_patch(fps=fps)
        messages.append("H.264 비트레이트(해상도·FPS) 패치")
    except Exception as exc:
        logger.warning("Web Stream H.264 비트레이트 패치 실패: %s", exc)

    _WEB_HW_REQUESTED = bool(h264_hardware)
    if _WEB_HW_REQUESTED:
        from streaming.h264_hw_patch import choose_hardware_encoder_name

        chosen = choose_hardware_encoder_name()
        if chosen:
            messages.append(f"H.264 GPU 인코더(웹 전용): {chosen}")
        else:
            messages.append("H.264 GPU 없음 — libx264(웹)")
    else:
        messages.append("H.264 소프트웨어(libx264, 웹)")

    _install_web_sender_patches()
    return messages


def release_web_stream_codec_patches() -> None:
    """Web Stream 서버 종료 시 GPU 요청만 해제 (전역 HW 패치는 변경하지 않음)."""
    global _WEB_HW_REQUESTED
    _WEB_HW_REQUESTED = False


def prefer_h264_on_peer_connection(pc: RTCPeerConnection) -> None:
    """뷰어 offer 처리 시 비디오 m-line 에 H.264 를 VP8 보다 앞에 둔다."""
    caps = get_capabilities("video")
    h264: list[RTCRtpCodecCapability] = []
    vp8: list[RTCRtpCodecCapability] = []
    rest: list[RTCRtpCodecCapability] = []
    for c in caps.codecs:
        mime = (c.mimeType or "").lower()
        if mime == "video/h264":
            h264.append(c)
        elif mime == "video/vp8":
            vp8.append(c)
        else:
            rest.append(c)
    if not h264:
        return
    preferred = h264 + vp8 + rest
    for transceiver in pc.getTransceivers():
        if transceiver.kind == "video":
            transceiver.setCodecPreferences(preferred)


def _web_video_track(sender: Any) -> bool:
    from streaming.web_stream import WebSharedVideoTrack

    track = getattr(sender, "track", None)
    return isinstance(track, WebSharedVideoTrack)


def _maybe_web_hw_encoder(sender: RTCRtpSender, codec: Any) -> None:
    if not _WEB_HW_REQUESTED or not _web_video_track(sender):
        return
    mime = (getattr(codec, "mimeType", "") or "").lower()
    if mime != "video/h264":
        return
    enc = getattr(sender, "_RTCRtpSender__encoder", None)
    if enc is None:
        return
    from streaming.h264_hw_patch import H264HardwareEncoder, choose_hardware_encoder_name

    if isinstance(enc, H264HardwareEncoder):
        return
    chosen = choose_hardware_encoder_name()
    if not chosen:
        return
    sender._RTCRtpSender__encoder = H264HardwareEncoder(chosen)  # type: ignore[attr-defined]


async def _web_next_encoded_frame(
    self: RTCRtpSender, codec: Any
) -> RTCEncodedFrame | None:
    """웹 전용: 인코더 생성 직후 GPU 로 바꾼 뒤 인코딩 (원격 ``SharedVideoTrack`` 은 원본 경로)."""
    data = await self._RTCRtpSender__track.recv()  # type: ignore[attr-defined]
    if not self._enabled:
        return None

    audio_level = None
    if getattr(self, "_RTCRtpSender__encoder", None) is None:
        self._RTCRtpSender__encoder = get_encoder(codec)  # type: ignore[attr-defined]
        _maybe_web_hw_encoder(self, codec)

    if isinstance(data, Frame):
        if isinstance(data, AudioFrame):
            audio_level = compute_audio_level_dbov(data)
        force_keyframe = self._RTCRtpSender__force_keyframe  # type: ignore[attr-defined]
        self._RTCRtpSender__force_keyframe = False  # type: ignore[attr-defined]
        payloads, timestamp = await self._RTCRtpSender__loop.run_in_executor(  # type: ignore[attr-defined]
            None,
            self._RTCRtpSender__encoder.encode,  # type: ignore[attr-defined]
            data,
            force_keyframe,
        )
    else:
        payloads, timestamp = self._RTCRtpSender__encoder.pack(data)  # type: ignore[attr-defined]

    if not payloads:
        return None
    return RTCEncodedFrame(payloads, timestamp, audio_level)


def _install_web_sender_patches() -> None:
    global _SENDER_PATCHES_INSTALLED, _ORIG_NEXT_ENCODED_FRAME, _ORIG_HANDLE_RTCP
    if _SENDER_PATCHES_INSTALLED:
        return

    from aiortc.rtcrtpsender import RTCP_PSFB_APP, unpack_remb_fci
    from aiortc.rtp import RtcpPsfbPacket

    _ORIG_NEXT_ENCODED_FRAME = RTCRtpSender._next_encoded_frame
    _ORIG_HANDLE_RTCP = RTCRtpSender._handle_rtcp_packet

    async def _patched_next_encoded_frame(
        self: RTCRtpSender, codec: Any
    ) -> RTCEncodedFrame | None:
        if _web_video_track(self):
            return await _web_next_encoded_frame(self, codec)
        assert _ORIG_NEXT_ENCODED_FRAME is not None
        return await _ORIG_NEXT_ENCODED_FRAME(self, codec)

    async def _patched_handle_rtcp_packet(self: RTCRtpSender, packet: Any) -> None:
        if (
            isinstance(packet, RtcpPsfbPacket)
            and packet.fmt == RTCP_PSFB_APP
            and _web_video_track(self)
        ):
            try:
                bitrate, ssrcs = unpack_remb_fci(packet.fci)
                enc = getattr(self, "_RTCRtpSender__encoder", None)
                if self._ssrc in ssrcs and enc is not None and hasattr(
                    enc, "target_bitrate"
                ):
                    floor = int(getattr(enc, "_oddments_remb_floor", 0) or 0)
                    if floor > 0:
                        bitrate = max(int(bitrate), floor)
                    enc.target_bitrate = bitrate
                    return
            except ValueError:
                pass
        assert _ORIG_HANDLE_RTCP is not None
        await _ORIG_HANDLE_RTCP(self, packet)

    RTCRtpSender._next_encoded_frame = _patched_next_encoded_frame  # type: ignore[method-assign]
    RTCRtpSender._handle_rtcp_packet = _patched_handle_rtcp_packet  # type: ignore[method-assign]
    _SENDER_PATCHES_INSTALLED = True


__all__ = [
    "apply_web_stream_codec_patches",
    "is_web_stream_hw_requested",
    "prefer_h264_on_peer_connection",
    "release_web_stream_codec_patches",
]
