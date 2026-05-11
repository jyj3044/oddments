"""aiortc H.264 송출을 GPU 인코더(NVENC / AMF 등)로 우회하기 위한 런타임 패치.

PyAV(FFmpeg) 빌드에 해당 인코더가 포함되어 있어야 한다.
패치는 프로세스당 한 번만 적용된다.
"""

from __future__ import annotations

import fractions
import logging
import sys
from fractions import Fraction
from typing import Callable, Iterator, cast

import av
from av.video.codeccontext import VideoCodecContext

from aiortc.codecs.h264 import H264Encoder, MAX_FRAME_RATE

logger = logging.getLogger(__name__)

_PATCH_LOCK = False
_ORIGINAL_GET_ENCODER: Callable[..., object] | None = None
_LAST_STATUS: str = ""


def _apply_codec_options(
    codec: VideoCodecContext,
    encoder_name: str,
    *,
    for_probe: bool = False,
) -> None:
    if encoder_name == "h264_nvenc":
        codec.options = {
            "preset": "p4",
            "tune": "ull",
            "delay": "0",
            "zerolatency": "1",
            "rc": "cbr",
        }
        if not for_probe:
            codec.profile = "baseline"
    elif encoder_name == "h264_amf":
        codec.options = {
            "usage": "lowlatency",
            "quality": "speed",
        }
        if not for_probe:
            try:
                codec.profile = "baseline"
            except Exception:
                pass
    elif encoder_name == "h264_videotoolbox":
        codec.options = {"realtime": "1"}
        if not for_probe:
            try:
                codec.profile = "baseline"
            except Exception:
                pass
    else:
        codec.options = {
            "level": "31",
            "tune": "zerolatency",
        }
        codec.profile = "baseline"


def _try_open_encoder(name: str) -> bool:
    try:
        c = av.CodecContext.create(name, "w")
        c.width = 256
        c.height = 256
        c.pix_fmt = "yuv420p"
        c.bit_rate = 800_000
        c.framerate = Fraction(MAX_FRAME_RATE, 1)
        c.time_base = Fraction(1, MAX_FRAME_RATE)
        _apply_codec_options(c, name, for_probe=True)
        c.open()
        return True
    except Exception:
        return False


def choose_hardware_encoder_name() -> str | None:
    """사용할 하드웨어 인코더 이름. 없으면 None (libx264 유지)."""
    if sys.platform == "darwin":
        order = ("h264_videotoolbox", "h264_nvenc", "h264_amf")
    else:
        order = ("h264_nvenc", "h264_amf")
    for name in order:
        if _try_open_encoder(name):
            return name
    return None


class H264HardwareEncoder(H264Encoder):
    """libx264 대신 NVENC/AMF 로 인코딩하는 송출 전용 인코더."""

    def __init__(self, encoder_name: str) -> None:
        super().__init__()
        self._encoder_name = encoder_name

    def _encode_frame(
        self,
        frame: av.VideoFrame,
        force_keyframe: bool,
    ) -> Iterator[bytes]:
        if self.codec and (
            frame.width != self.codec.width
            or frame.height != self.codec.height
            or abs(self.target_bitrate - self.codec.bit_rate) / max(self.codec.bit_rate, 1)
            > 0.1
        ):
            self.buffer_data = b""
            self.buffer_pts = None
            self.codec = None

        if force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I
        else:
            frame.pict_type = av.video.frame.PictureType.NONE

        if self.codec is None:
            self.codec = cast(
                VideoCodecContext,
                av.CodecContext.create(self._encoder_name, "w"),
            )
            self.codec.width = frame.width
            self.codec.height = frame.height
            self.codec.bit_rate = self.target_bitrate
            self.codec.pix_fmt = "yuv420p"
            self.codec.framerate = fractions.Fraction(MAX_FRAME_RATE, 1)
            self.codec.time_base = fractions.Fraction(1, MAX_FRAME_RATE)
            _apply_codec_options(self.codec, self._encoder_name, for_probe=False)

        data_to_send = b""
        for package in self.codec.encode(frame):
            data_to_send += bytes(package)

        if data_to_send:
            yield from self._split_bitstream(data_to_send)


def install_h264_hardware_encoder(*, enabled: bool) -> str:
    """설정에 따라 aiortc 의 video/H264 송출 인코더를 GPU 경로로 교체한다."""
    global _PATCH_LOCK, _ORIGINAL_GET_ENCODER, _LAST_STATUS

    import aiortc.codecs as codecs_mod

    if not enabled:
        if _ORIGINAL_GET_ENCODER is not None:
            codecs_mod.get_encoder = _ORIGINAL_GET_ENCODER  # type: ignore[assignment]
            _ORIGINAL_GET_ENCODER = None
        _PATCH_LOCK = False
        _LAST_STATUS = "H.264 하드웨어: 설정으로 비활성화 (libx264)"
        return _LAST_STATUS

    if _PATCH_LOCK:
        return _LAST_STATUS or "H.264 하드웨어: 이미 적용됨"

    chosen = choose_hardware_encoder_name()
    if chosen is None:
        _LAST_STATUS = "H.264 하드웨어: 사용 가능한 GPU 인코더 없음 (libx264)"
        return _LAST_STATUS

    _orig = codecs_mod.get_encoder
    _ORIGINAL_GET_ENCODER = _orig

    def _patched_get_encoder(codec) -> object:
        mime = getattr(codec, "mimeType", "") or ""
        if str(mime).lower() == "video/h264":
            return H264HardwareEncoder(chosen)
        return _orig(codec)

    codecs_mod.get_encoder = _patched_get_encoder  # type: ignore[assignment]
    _PATCH_LOCK = True
    _LAST_STATUS = f"H.264 하드웨어: {chosen}"
    try:
        logger.info("WebRTC H.264 송출 — GPU 인코더 %s", chosen)
    except Exception:
        pass
    return _LAST_STATUS


def last_install_message() -> str:
    return _LAST_STATUS


__all__ = [
    "H264HardwareEncoder",
    "choose_hardware_encoder_name",
    "install_h264_hardware_encoder",
    "last_install_message",
]
