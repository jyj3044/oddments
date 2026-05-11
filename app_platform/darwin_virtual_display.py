"""macOS 전용: CGVirtualDisplay(비공개 API)로 가상 디스플레이 생성·해제.

직접 배포 빌드 전용. App Store 제출에는 적합하지 않음.
PyObjC 로 CoreGraphics 클래스를 동적 로드한다.
"""

from __future__ import annotations

import sys
from typing import Optional

if sys.platform != "darwin":
    raise ImportError("darwin_virtual_display 는 macOS 에서만 사용할 수 있습니다.")


class DarwinVirtualDisplayError(RuntimeError):
    pass


def create_virtual_display(
    width: int,
    height: int,
    *,
    refresh_hz: float = 60.0,
    name: str = "Oddments Remote",
) -> tuple[object, int]:
    """가상 디스플레이를 만들고 (객체, CGDirectDisplayID) 를 반환.

    호출측은 세션 종료 시 :func:`release_virtual_display` 로 해제해야 한다.
    """
    w = max(320, int(width))
    h = max(240, int(height))
    rr = float(refresh_hz)
    if rr < 30.0:
        rr = 60.0

    try:
        from objc import lookUpClass  # type: ignore[import-untyped]
        import Quartz  # type: ignore[import-untyped]
    except ImportError as e:
        raise DarwinVirtualDisplayError(
            "PyObjC(Quartz) 가 필요합니다. pip install pyobjc-framework-Quartz"
        ) from e

    CGVirtualDisplayDescriptor = lookUpClass("CGVirtualDisplayDescriptor")
    CGVirtualDisplay = lookUpClass("CGVirtualDisplay")
    CGVirtualDisplaySettings = lookUpClass("CGVirtualDisplaySettings")
    CGVirtualDisplayMode = lookUpClass("CGVirtualDisplayMode")

    if any(x is None for x in (
        CGVirtualDisplayDescriptor,
        CGVirtualDisplay,
        CGVirtualDisplaySettings,
        CGVirtualDisplayMode,
    )):
        raise DarwinVirtualDisplayError(
            "CGVirtualDisplay 클래스를 찾을 수 없습니다. "
            "macOS 버전이 너무 오래되었거나 CoreGraphics 가 제한되었습니다."
        )

    desc = CGVirtualDisplayDescriptor.alloc().init()
    desc.setName_(name)
    desc.setVendorID_(0x0DD6)  # Oddments
    desc.setProductID_(0x0001)
    desc.setSerialNum_(1)
    desc.setMaxPixelsWide_(w)
    desc.setMaxPixelsHigh_(h)
    # 대략 96 DPI 물리 크기(mm)
    mm_w = max(10.0, w * 25.4 / 96.0)
    mm_h = max(10.0, h * 25.4 / 96.0)
    # Cocoa/AppKit 은 메인 스레드 제약이 있을 수 있어 CGSize(Quartz)만 사용한다.
    desc.setSizeInMillimeters_(Quartz.CGSizeMake(float(mm_w), float(mm_h)))

    vd = CGVirtualDisplay.alloc().initWithDescriptor_(desc)
    if vd is None:
        raise DarwinVirtualDisplayError("CGVirtualDisplay 초기화 실패")

    mode = CGVirtualDisplayMode.alloc().initWithWidth_height_refreshRate_(
        w, h, rr
    )
    settings = CGVirtualDisplaySettings.alloc().init()
    settings.setModes_([mode])
    settings.setHiDPI_(0)

    ok = bool(vd.applySettings_(settings))
    if not ok:
        try:
            vd.release()
        except Exception:
            pass
        raise DarwinVirtualDisplayError("가상 디스플레이 모드 적용 실패")

    try:
        did = int(vd.displayID())
    except Exception as exc:
        try:
            vd.release()
        except Exception:
            pass
        raise DarwinVirtualDisplayError("displayID 를 읽지 못했습니다.") from exc

    if did <= 0:
        try:
            vd.release()
        except Exception:
            pass
        raise DarwinVirtualDisplayError("유효하지 않은 displayID 입니다.")

    return vd, did


def release_virtual_display(vd: object) -> None:
    """가상 디스플레이 객체를 해제하고 OS 에서 제거한다."""
    if vd is None:
        return
    try:
        vd.release()
    except Exception:
        pass


def cg_display_bounds(display_id: int) -> tuple[float, float, float, float]:
    """CGDisplayBounds → (x, y, width, height)."""
    import Quartz  # type: ignore[import-untyped]

    r = Quartz.CGDisplayBounds(int(display_id))
    return (
        float(r.origin.x),
        float(r.origin.y),
        float(r.size.width),
        float(r.size.height),
    )


__all__ = [
    "DarwinVirtualDisplayError",
    "cg_display_bounds",
    "create_virtual_display",
    "release_virtual_display",
]
