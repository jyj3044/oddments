"""원격 호스트 화면 송출: H.264 목표 비트레이트를 해상도·FPS에 맞게 올린다.

aiortc 기본 H264Encoder는 대략 1Mbps 전후에서 시작하고, ``target_bitrate`` 세터가
약 3Mbps 상한으로 묶여 큰 캡처(1440p·4K 등)에서 블록·번짐이 심해진다.
호스트가 캡처를 시작할 때 한 번 패치해 완화한다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_StreamFpsHint = [30.0]
_ORIG_BASE_ENCODE: Callable[..., Any] | None = None
_ORIG_HW_ENCODE: Callable[..., Any] | None = None
_PATCH_INSTALLED = False


def set_stream_fps_hint(fps: float) -> None:
    """SharedVideoTrack 의 송출 FPS 와 동기."""
    _StreamFpsHint[0] = float(max(5.0, min(60.0, fps)))


def recommend_h264_bitrate_bps(width: int, height: int, fps: float) -> int:
    """텍스트·UI 위주 화면 공유에 맞춘 목표 비트레이트 (bps).

    대략 1080p30 → 5~6Mbps, 4K30 → 22~24Mbps 수준. 상한은 aiortc 모듈
    ``MAX_BITRATE`` 를 올린 뒤 세터로 적용된다.
    """
    w = max(16, int(width))
    h = max(16, int(height))
    f = float(max(5.0, min(60.0, fps)))
    pixels = float(w) * float(h)
    bps = int(pixels * f * 0.088)
    return int(max(800_000, min(bps, 48_000_000)))


def _raise_h264_caps() -> None:
    import aiortc.codecs.h264 as h264_mod

    h264_mod.MAX_BITRATE = max(int(getattr(h264_mod, "MAX_BITRATE", 0)), 48_000_000)


def _apply_target_from_frame(encoder: object, frame: object) -> None:
    try:
        w = int(getattr(frame, "width", 0) or 0)
        h = int(getattr(frame, "height", 0) or 0)
        if w < 32 or h < 32:
            return
        tgt = recommend_h264_bitrate_bps(w, h, _StreamFpsHint[0])
        enc = encoder  # H264Encoder / H264HardwareEncoder
        enc.target_bitrate = tgt  # type: ignore[attr-defined]
        # Web Stream REMB 하한(웹 송신 RTCP 패치에서 WebSharedVideoTrack 만 사용).
        enc._oddments_remb_floor = int(max(800_000, tgt * 85 // 100))  # type: ignore[attr-defined]
    except Exception:
        pass


def install_screen_share_h264_bitrate_patch(*, fps: float) -> None:
    """프로세스당 한 번. RemoteHostServer._ensure_capture 에서 호출."""
    global _PATCH_INSTALLED, _ORIG_BASE_ENCODE, _ORIG_HW_ENCODE

    set_stream_fps_hint(fps)
    _raise_h264_caps()

    if _PATCH_INSTALLED:
        return

    import aiortc.codecs.h264 as h264_mod

    _ORIG_BASE_ENCODE = h264_mod.H264Encoder._encode_frame

    def _patched_base_encode(self: Any, frame: Any, force_keyframe: bool = False) -> Any:
        _apply_target_from_frame(self, frame)
        assert _ORIG_BASE_ENCODE is not None
        return _ORIG_BASE_ENCODE(self, frame, force_keyframe)

    h264_mod.H264Encoder._encode_frame = _patched_base_encode  # type: ignore[method-assign]

    try:
        from streaming.h264_hw_patch import H264HardwareEncoder

        _ORIG_HW_ENCODE = H264HardwareEncoder._encode_frame

        def _patched_hw_encode(self: Any, frame: Any, force_keyframe: bool = False) -> Any:
            _apply_target_from_frame(self, frame)
            assert _ORIG_HW_ENCODE is not None
            return _ORIG_HW_ENCODE(self, frame, force_keyframe)

        H264HardwareEncoder._encode_frame = _patched_hw_encode  # type: ignore[method-assign]
    except Exception as exc:
        try:
            logger.warning("H264HardwareEncoder 비트레이트 래핑 생략: %s", exc)
        except Exception:
            pass

    _PATCH_INSTALLED = True
    try:
        from .remote_log import log_remote_event

        log_remote_event(
            "호스트: 화면 송출 H.264 비트레이트(해상도·FPS 연동) 패치 적용"
        )
    except Exception:
        pass


__all__ = [
    "install_screen_share_h264_bitrate_patch",
    "recommend_h264_bitrate_bps",
    "set_stream_fps_hint",
]
