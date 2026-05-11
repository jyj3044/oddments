"""macOS 전용: CGVirtualDisplay(비공개 API)로 가상 디스플레이 생성·해제.

직접 배포 빌드 전용. App Store 제출에는 적합하지 않음.
PyObjC 로 CoreGraphics 클래스를 동적 로드한다.
macOS 에서는 ``pyobjc-framework-libdispatch`` 로 **프로세스 공용 시리얼 큐** 하나를
descriptor 에 붙이고, ``CGVirtualDisplay`` 생성·모드 적용·``release`` 를
**항상 그 큐에서만** ``dispatch_sync`` 로 실행한다(스레드 분산·빈 applySettings
제거로 PyObjC/WindowServer 불일치를 줄인다).
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from typing import Optional

if sys.platform != "darwin":
    raise ImportError("darwin_virtual_display 는 macOS 에서만 사용할 수 있습니다.")


_cgvd_serial_lock = threading.Lock()
_cgvd_serial_queue_ref: object | None = None


class DarwinVirtualDisplayError(RuntimeError):
    pass


def _cgvd_create_verbose(msg: str) -> None:
    """``ODDMENTS_CGVD_TRACE=1`` 일 때만 단계 로그(기본은 조용히)."""
    try:
        v = os.environ.get("ODDMENTS_CGVD_TRACE", "").strip().lower()
        if v not in ("1", "true", "yes"):
            return
        from streaming.remote_log import log_remote_diag

        t = threading.current_thread()
        log_remote_diag(
            f"CGVD create | {msg} | pid={os.getpid()} thr={t.name!s} "
            f"ident={getattr(t, 'ident', None)}"
        )
    except Exception:
        pass


def _cgvd_create_fail(msg: str) -> None:
    """생성 실패·타임아웃 시 한 줄(즉시 파일·stderr)."""
    try:
        from streaming.remote_log import log_remote_diag

        t = threading.current_thread()
        log_remote_diag(
            f"CGVD create FAIL | {msg} | thr={t.name!s} ident={getattr(t, 'ident', None)}"
        )
    except Exception:
        pass


def _cgvd_create_brief(msg: str) -> None:
    """생성 과정 요약(회당 소수 줄). ``ODDMENTS_CGVD_TRACE`` 와 무관하게 항상 기록."""
    try:
        from streaming.remote_log import log_remote_diag

        t = threading.current_thread()
        log_remote_diag(f"CGVD create | {msg} | thr={t.name!s}")
    except Exception:
        pass


def _cgvd_ops_detail(msg: str) -> None:
    """CGVirtualDisplay 생성·해제 단계(VD 테스트 디버깅용, 항상 remote 로그)."""
    try:
        from streaming.remote_log import log_remote_diag

        t = threading.current_thread()
        log_remote_diag(
            f"CGVD ops | {msg} | pid={os.getpid()} thr={t.name!s} "
            f"ident={getattr(t, 'ident', None)}"
        )
    except Exception:
        pass


def _cgvd_serial_queue() -> object:
    """CGVirtualDisplay 관련 ObjC 호출을 한 스레드로 직렬화한다."""
    global _cgvd_serial_queue_ref
    if _cgvd_serial_queue_ref is not None:
        return _cgvd_serial_queue_ref
    with _cgvd_serial_lock:
        if _cgvd_serial_queue_ref is not None:
            return _cgvd_serial_queue_ref
        import libdispatch as ld  # type: ignore[import-untyped]

        _cgvd_serial_queue_ref = ld.dispatch_queue_create(
            b"com.oddments.cgvirtualdisplay",
            None,
        )
        return _cgvd_serial_queue_ref


def _attach_serial_queue_to_descriptor(desc: object) -> None:
    """descriptor.queue 에 공용 시리얼 큐를 붙인다."""
    try:
        desc.setQueue_(_cgvd_serial_queue())
    except (ImportError, Exception):
        return


def _release_vd_dispatch_on_shared_serial(vd: object) -> bool:
    """공용 시리얼 큐에서 ``vd.release()`` 한 번."""
    try:
        import libdispatch as ld  # type: ignore[import-untyped]
        import objc  # type: ignore[import-untyped]
    except ImportError:
        return False
    err: list[BaseException | None] = [None]

    def inner() -> None:
        try:
            _cgvd_ops_detail("releasevd inner vd.release CALL")
            with objc.autorelease_pool():
                vd.release()
            _cgvd_ops_detail("releasevd inner vd.release RET")
        except BaseException as exc:  # noqa: BLE001
            err[0] = exc
            _cgvd_ops_detail(f"releasevd inner EXC {type(exc).__name__}: {exc!r}")

    q = _cgvd_serial_queue()
    _cgvd_ops_detail(f"releasevd serial dispatch_sync ENTER queue={id(q)!r} vd={id(vd)!r}")
    t0 = time.monotonic()
    try:
        ld.dispatch_sync(q, inner)
    except BaseException as exc:  # noqa: BLE001
        err[0] = exc
        _cgvd_ops_detail(f"releasevd serial dispatch_sync outer EXC {exc!r}")
    _cgvd_ops_detail(
        "releasevd serial dispatch_sync LEAVE "
        f"ms={(time.monotonic() - t0) * 1000.0:.1f} ok={err[0] is None}"
    )
    return err[0] is None


def create_virtual_display(
    width: int,
    height: int,
    *,
    refresh_hz: float = 60.0,
    name: str = "Oddments Remote",
    descriptor_serial: int | None = None,
    descriptor_product_id: int | None = None,
) -> tuple[object, int]:
    """가상 디스플레이를 만들고 (객체, CGDirectDisplayID) 를 반환.

    ``descriptor_serial`` 은 디스크립터 ``setSerialNum_`` 에 쓴다. 직전 해제 직후
    재생성 시 동일 시리얼이면 WindowServer 가 막는 경우가 있어, VD 테스트 등에서는
    세션마다 증가시키는 값을 넘기는 것이 안전하다. ``None`` 이면 1 을 쓴다.

    ``descriptor_product_id`` 는 ``setProductID_`` (16bit) 에 쓴다. ``None`` 이면
    ``0x0001`` 을 쓴다. 시리얼만 바꿔도 막힐 때 제품 ID 조합을 바꾸면 재생성에 유리하다.

    ``pyobjc-framework-libdispatch`` 가 있으면 생성 전 과정을 공용 시리얼 큐에서
    실행한다(해제와 동일 큐 — OS 잔류·PyObjC 스레드 불일치 완화).

    호출측은 세션 종료 시 :func:`release_virtual_display` 로 해제해야 한다.
    """
    w = max(320, int(width))
    h = max(240, int(height))
    rr = float(refresh_hz)
    if rr < 30.0:
        rr = 60.0

    _cgvd_create_verbose(f"begin w={w} h={h} rr={rr} name={name!r}")
    _cgvd_create_brief(f"시작 w={w} h={h} rr={rr} name={name!r}")
    _cgvd_ops_detail(f"create_virtual_display ENTRY w={w} h={h} rr={rr} name={name!r}")

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

    out: list[tuple[object, int] | None] = [None]
    err: list[BaseException | None] = [None]

    def _impl() -> None:
        try:
            import objc  # type: ignore[import-untyped]

            with objc.autorelease_pool():
                _cgvd_create_verbose("_impl on serial queue")
                _cgvd_ops_detail("_impl ENTER")
                desc = CGVirtualDisplayDescriptor.alloc().init()
                _cgvd_ops_detail("descriptor alloc().init() OK")
                _attach_serial_queue_to_descriptor(desc)
                desc.setName_(name)
                desc.setVendorID_(0x0DD6)  # Oddments
                if descriptor_product_id is None:
                    prod = 0x0001
                else:
                    prod = max(1, int(descriptor_product_id)) & 0xFFFF
                desc.setProductID_(prod)
                if descriptor_serial is None:
                    sn = 1
                else:
                    sn = max(1, int(descriptor_serial)) & 0x7FFFFFFF
                desc.setSerialNum_(sn)
                _cgvd_ops_detail(f"descriptor productID={prod:#06x} serialNum={sn}")
                desc.setMaxPixelsWide_(w)
                desc.setMaxPixelsHigh_(h)
                mm_w = max(10.0, w * 25.4 / 96.0)
                mm_h = max(10.0, h * 25.4 / 96.0)
                desc.setSizeInMillimeters_(Quartz.CGSizeMake(float(mm_w), float(mm_h)))
                _cgvd_ops_detail(
                    f"descriptor configured w={w} h={h} name={name!r} queue_attached=1"
                )

                _cgvd_ops_detail("initWithDescriptor_ CALL")
                vd = CGVirtualDisplay.alloc().initWithDescriptor_(desc)
                _cgvd_ops_detail(f"initWithDescriptor_ RET vd_is_none={vd is None}")
                if vd is None:
                    raise DarwinVirtualDisplayError(
                        "CGVirtualDisplay 초기화 실패. "
                        "이전 가상 디스플레이가 시스템에 남았거나 동시에 여러 개를 만들 수 없을 때 "
                        "자주 발생합니다. 앱을 완전히 종료한 뒤 시스템 설정 → 디스플레이를 확인하고 "
                        "다시 시도하세요."
                    )

                _cgvd_ops_detail("CGVirtualDisplayMode + Settings alloc")
                mode = CGVirtualDisplayMode.alloc().initWithWidth_height_refreshRate_(
                    w, h, rr
                )
                settings = CGVirtualDisplaySettings.alloc().init()
                settings.setModes_([mode])
                settings.setHiDPI_(0)

                _cgvd_ops_detail("applySettings_ CALL")
                ok = bool(vd.applySettings_(settings))
                _cgvd_ops_detail(f"applySettings_ RET ok={ok}")
                if not ok:
                    try:
                        vd.release()
                    except Exception:
                        pass
                    raise DarwinVirtualDisplayError("가상 디스플레이 모드 적용 실패")

                try:
                    _cgvd_ops_detail("displayID CALL")
                    did = int(vd.displayID())
                    _cgvd_ops_detail(f"displayID RET did={did}")
                except Exception as exc:
                    try:
                        vd.release()
                    except Exception:
                        pass
                    raise DarwinVirtualDisplayError(
                        "displayID 를 읽지 못했습니다."
                    ) from exc

                if did <= 0:
                    try:
                        vd.release()
                    except Exception:
                        pass
                    raise DarwinVirtualDisplayError("유효하지 않은 displayID 입니다.")

                out[0] = (vd, did)
                _cgvd_create_verbose(f"ok did={did}")
                _cgvd_ops_detail(f"_impl EXIT ok cg_id={did}")
        except BaseException as exc:  # noqa: BLE001
            err[0] = exc
            _cgvd_ops_detail(f"_impl EXC {type(exc).__name__}: {exc!r}")
            _cgvd_create_fail(f"_impl: {type(exc).__name__}: {exc!r}")

    try:
        import libdispatch as ld  # type: ignore[import-untyped]
    except ImportError:
        _cgvd_create_verbose("_impl on caller thread (no libdispatch)")
        _cgvd_create_brief("경로: libdispatch 없음 → 호출 스레드에서 _impl")
        _cgvd_ops_detail("create path: no libdispatch → _impl on caller")
        _impl()
    else:
        # 메인 스레드에서 ``dispatch_sync(시리얼 큐)`` 만 하면 직전 해제 때 메인에 예약된
        # PyObjC 정리와 순서가 꼬일 수 있다. 메인이면 별도 스레드에서 sync 한다.
        #
        # 메인에서 백그라운드 완료를 기다릴 때 **CFRunLoop를 돌리면 안 된다**: 그동안
        # 메인 디스패치 큐에 쌓인 ``_clear_vd_ref`` 등이 실행되며 ``object_dealloc`` SIGSEGV
        # 가 난다(faulthandler). ``threading.Event.wait`` 만 사용한다.
        try:
            from Foundation import NSThread  # type: ignore[import-untyped]

            on_main = bool(NSThread.isMainThread())
        except Exception:
            on_main = False

        _cgvd_create_verbose(f"libdispatch on_main={on_main}")
        _cgvd_create_brief(
            "경로: 시리얼 큐 dispatch_sync "
            f"({'메인→백그라운드 스레드' if on_main else '비메인 직접'})"
        )

        if not on_main:
            # ``dispatch_sync`` 자체에는 timeout 이 없다. 두 번째 생성에서 OS 의
            # ``initWithDescriptor_`` 호출이 반환을 안 해 호출 스레드 전체가 영구
            # 무한 대기 한 사례가 있다(로그상 ``initWithDescriptor_ CALL`` 다음 줄
            # 부재). 별도 데몬 스레드에서 ``dispatch_sync`` 를 돌리고 ``Event.wait``
            # 로 감시해 일정 시간 내에 끝나지 않으면 명확히 ``timeout`` 예외로 빠진다.
            q = _cgvd_serial_queue()
            _cgvd_ops_detail(f"dispatch_sync ENTER queue={id(q)!r}")
            t_sync = time.monotonic()
            done = threading.Event()
            bg_err: list[BaseException | None] = [None]

            def _bg_off_main() -> None:
                try:
                    ld.dispatch_sync(q, _impl)
                except BaseException as exc:  # noqa: BLE001
                    bg_err[0] = exc
                finally:
                    done.set()

            th = threading.Thread(
                target=_bg_off_main,
                daemon=True,
                name="cgvd-create-dispatch",
            )
            th.start()
            create_timeout_s = 15.0
            if not done.wait(timeout=create_timeout_s):
                _cgvd_create_fail(
                    "dispatch_sync timeout "
                    f"{create_timeout_s:g}s bg_alive={th.is_alive()}"
                )
                _cgvd_ops_detail(
                    f"dispatch_sync TIMEOUT {create_timeout_s:g}s "
                    f"ms={(time.monotonic() - t_sync) * 1000.0:.1f} "
                    "(initWithDescriptor_ 무한 대기 가정)"
                )
                raise DarwinVirtualDisplayError(
                    f"CGVirtualDisplay 생성이 {create_timeout_s:g}초 내에 완료되지 "
                    "않았습니다. 이전 가상 디스플레이가 시스템에 남아 있거나 OS 가 "
                    "더 이상 신규 가상 디스플레이를 받지 못하는 상태로 보입니다. "
                    "앱을 종료한 뒤 다시 시도하거나, 시스템 설정 → 디스플레이에서 "
                    "잔여 항목을 확인하세요."
                )
            _cgvd_ops_detail(
                f"dispatch_sync LEAVE ms={(time.monotonic() - t_sync) * 1000.0:.1f}"
            )
            if bg_err[0] is not None:
                raise bg_err[0]
        else:
            done = threading.Event()

            def _bg() -> None:
                try:
                    q = _cgvd_serial_queue()
                    _cgvd_ops_detail(
                        f"dispatch_sync ENTER (off-main bg) queue={id(q)!r}"
                    )
                    t_sync = time.monotonic()
                    try:
                        ld.dispatch_sync(q, _impl)
                    finally:
                        _cgvd_ops_detail(
                            "dispatch_sync LEAVE (off-main bg) "
                            f"ms={(time.monotonic() - t_sync) * 1000.0:.1f}"
                        )
                finally:
                    done.set()

            th = threading.Thread(
                target=_bg,
                daemon=True,
                name="cgvd-create-off-main",
            )
            th.start()
            deadline = time.monotonic() + 120.0
            while True:
                if done.wait(0.05):
                    break
                if time.monotonic() > deadline:
                    _cgvd_create_fail(
                        f"timeout bg_alive={th.is_alive()}"
                    )
                    raise DarwinVirtualDisplayError(
                        "가상 디스플레이 생성이 시간 초과되었습니다. "
                        "잠시 후 다시 시도하세요."
                    )
            th.join(timeout=5.0)

    if err[0] is not None:
        e = err[0]
        _cgvd_create_fail(f"raise {type(e).__name__}: {e!r}")
        if isinstance(e, DarwinVirtualDisplayError):
            raise e
        raise DarwinVirtualDisplayError(str(e)) from e
    if out[0] is None:
        _cgvd_create_fail("out[0] is None")
        raise DarwinVirtualDisplayError("가상 디스플레이 생성 결과가 비었습니다.")
    _cgvd_create_verbose(f"return did={out[0][1]}")
    _cgvd_create_brief(f"완료 cg_id={out[0][1]}")
    _cgvd_ops_detail(f"create_virtual_display RETURN cg_id={out[0][1]}")
    return out[0]


def _drain_main_runloop_momentarily(seconds: float = 0.25) -> None:
    """해제 직후 WindowServer 쪽 정리가 돌도록 메인 런루프를 잠깐 돌린다."""
    try:
        import CoreFoundation as CF  # type: ignore[import-untyped]

        deadline = time.monotonic() + float(seconds)
        while time.monotonic() < deadline:
            CF.CFRunLoopRunInMode(CF.kCFRunLoopDefaultMode, 0.02, True)
    except Exception:
        pass


def _release_vd_once(vd: object) -> bool:
    """libdispatch 없을 때만: 현재 스레드에서 ``release`` (레거시 폴백)."""
    if vd is None:
        return True
    try:
        import objc  # type: ignore[import-untyped]

        with objc.autorelease_pool():
            vd.release()
        _drain_main_runloop_momentarily(0.25)
        return True
    except Exception:
        return False


def release_virtual_display(vd: object) -> None:
    """가상 디스플레이 객체를 해제하고 OS 에서 제거한다.

    호출 스레드에서 직접 ``release`` 한다. 원격 호스트(가상 디스플레이 모드)는
    :func:`release_virtual_display_on_main_thread` 를 쓴다
    (비메인에서 해제 시 PyObjC ``object_dealloc`` 크래시 방지).
    """
    if vd is None:
        return
    try:
        vd.release()
    except Exception:
        pass


def release_virtual_display_on_main_thread(
    vd: object,
    *,
    completion_event: Optional[threading.Event] = None,
    wait_timeout: float = 15.0,
    retry_wait: float = 10.0,
) -> bool:
    """``CGVirtualDisplay`` 를 해제한다.

    ``pyobjc-framework-libdispatch`` 가 있으면 **생성과 동일한 공용 시리얼 큐**에서
    ``release`` 한다(Flet 메인·``run_sync`` 와 분리 — 잔류·PyObjC 크래시 완화).

    패키지가 없으면 예전처럼 메인 런루프에 예약하는 경로를 쓴다.

    ``completion_event`` 가 있으면 해제 시도 종료 시 ``set()`` 한다.
    """
    import logging

    _log = logging.getLogger(__name__)

    def _notify_done() -> None:
        if completion_event is not None:
            completion_event.set()

    if vd is None:
        _notify_done()
        return True

    try:
        import libdispatch as ld  # noqa: F401  # type: ignore[import-untyped]
    except ImportError:
        ld = None

    if ld is not None:
        _cgvd_ops_detail(
            f"release_on_main_thread ENTRY path=libdispatch_serial vd={id(vd)!r}"
        )
        t_rel = time.monotonic()
        ok = bool(_release_vd_dispatch_on_shared_serial(vd))
        _cgvd_ops_detail(
            "release_on_main_thread serial branch "
            f"ok={ok} ms={(time.monotonic() - t_rel) * 1000.0:.1f}"
        )
        _cgvd_ops_detail("release_on_main_thread CFRunLoop drain 0.15s 시작")
        _drain_main_runloop_momentarily(0.15)
        _cgvd_ops_detail("release_on_main_thread CFRunLoop drain 끝")
        _notify_done()
        return ok

    try:
        from Foundation import NSThread  # type: ignore[import-untyped]

        if bool(NSThread.isMainThread()):
            ok = _release_vd_once(vd)
            _notify_done()
            return ok
    except Exception:
        pass

    done = threading.Event()
    released = {"ok": False}

    def _wrapped() -> None:
        # ``release()`` 가 성공한 뒤에만 ``released["ok"]`` 를 True 로 두어
        # 재스케줄된 블록은 성공 이후에만 스킵한다(첫 시도 실패 시 재시도 가능).
        if released["ok"]:
            done.set()
            _notify_done()
            return
        released["ok"] = bool(_release_vd_once(vd))
        try:
            from CoreFoundation import (  # type: ignore[import-untyped]
                CFRunLoopGetMain,
                CFRunLoopWakeUp,
            )

            CFRunLoopWakeUp(CFRunLoopGetMain())
        except Exception:
            pass
        finally:
            done.set()
            _notify_done()

    try:
        from app_platform.darwin_dispatch_main import run_sync_on_main_dispatch_queue

        if run_sync_on_main_dispatch_queue(_wrapped):
            return bool(released["ok"])
    except Exception as exc:
        try:
            _log.warning(
                "가상 디스플레이 메인 동기 해제 실패 — 비동기 폴백 시도 (%s)",
                exc,
            )
        except Exception:
            pass

    def _schedule_on_main_any() -> None:
        try:
            from app_platform.darwin_remote_seal import _schedule_on_main

            _schedule_on_main(_wrapped)
            return
        except Exception:
            pass
        try:
            from CoreFoundation import (  # type: ignore[import-untyped]
                CFRunLoopGetMain,
                CFRunLoopPerformBlock,
                CFRunLoopWakeUp,
                kCFRunLoopCommonModes,
            )

            CFRunLoopPerformBlock(CFRunLoopGetMain(), kCFRunLoopCommonModes, _wrapped)
            CFRunLoopWakeUp(CFRunLoopGetMain())
        except Exception:
            try:
                from AppKit import NSOperationQueue  # type: ignore[import-untyped]

                NSOperationQueue.mainQueue().addOperationWithBlock_(_wrapped)
            except Exception as exc:
                raise RuntimeError(f"메인 스케줄 불가: {exc}") from exc

    try:
        _schedule_on_main_any()
    except Exception as exc:
        try:
            _log.error("가상 디스플레이 메인 해제 스케줄 실패 — %s", exc)
        except Exception:
            pass
        done.set()
        _notify_done()
        return False

    if done.wait(timeout=float(wait_timeout)):
        return bool(released["ok"])

    if released["ok"]:
        return True

    try:
        _log.warning(
            "가상 디스플레이 메인 해제 대기 타임아웃(%ss) — 메인에 재스케줄",
            wait_timeout,
        )
    except Exception:
        pass

    try:
        _schedule_on_main_any()
    except Exception as exc:
        try:
            _log.error("가상 디스플레이 재스케줄 실패 — %s", exc)
        except Exception:
            pass

    if done.wait(timeout=float(retry_wait)):
        return bool(released["ok"])

    if released["ok"]:
        return True

    try:
        _log.error(
            "가상 디스플레이 해제를 메인에서 실행하지 못했습니다. "
            "디스플레이 설정에 잔류할 수 있으며, 프로세스 종료 시 정리될 수 있습니다."
        )
    except Exception:
        pass
    done.set()
    _notify_done()
    return False


def cg_display_id_still_online(display_id: int) -> bool | None:
    """``CGDirectDisplayID`` 가 여전히 온라인이면 True, 아니면 False.

    Quartz API 실패 시 None (검증 생략).
    """
    try:
        import Quartz  # type: ignore[import-untyped]

        did = int(display_id)
        if did <= 0:
            return False
        fn = getattr(Quartz, "CGDisplayIsOnline", None)
        if fn is None:
            return None
        return bool(fn(did))
    except Exception:
        return None


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


def _cg_load_display_config_fns() -> object:
    """CoreGraphics C 함수 중 PyObjC 가 래핑하지 않는 CGDisplayConfiguration API 를
    ctypes 로 직접 로드한다.

    PyObjC 의 Quartz 모듈은 ``CGBeginDisplayConfiguration`` 의 출력 파라미터를
    ctypes 방식으로 처리하지 않으므로, ctypes.CDLL 을 통해 직접 바인딩한다.
    """
    _CG_PATH = (
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    cg = ctypes.CDLL(_CG_PATH)

    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGMainDisplayID.argtypes = []

    # CGError CGBeginDisplayConfiguration(CGDisplayConfigRef *config)
    cg.CGBeginDisplayConfiguration.restype = ctypes.c_int32
    cg.CGBeginDisplayConfiguration.argtypes = [ctypes.POINTER(ctypes.c_void_p)]

    # CGError CGConfigureDisplayOrigin(CGDisplayConfigRef, CGDirectDisplayID, int32_t x, int32_t y)
    cg.CGConfigureDisplayOrigin.restype = ctypes.c_int32
    cg.CGConfigureDisplayOrigin.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.c_int32,
    ]

    # void CGCancelDisplayConfiguration(CGDisplayConfigRef)
    cg.CGCancelDisplayConfiguration.restype = None
    cg.CGCancelDisplayConfiguration.argtypes = [ctypes.c_void_p]

    # CGError CGCompleteDisplayConfiguration(CGDisplayConfigRef, CGConfigureOption)
    cg.CGCompleteDisplayConfiguration.restype = ctypes.c_int32
    cg.CGCompleteDisplayConfiguration.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    return cg


def set_virtual_display_as_primary(vd_display_id: int) -> int:
    """가상 디스플레이를 주 디스플레이(origin 0,0)로 설정한다.

    CGDisplayConfiguration 으로 가상 디스플레이를 (0,0) 위치로 옮기고
    기존 주 디스플레이를 그 오른쪽으로 이동시킨다. macOS 는 origin (0,0) 을
    가진 디스플레이를 주 디스플레이로 인식하므로, 이후 새 앱 창은
    가상 디스플레이에 열린다.

    반환값: 복원 시 사용할 이전 주 디스플레이 ID (성공), 0 (실패·변경 불필요).
    """
    vid = int(vd_display_id)
    if vid <= 0:
        return 0
    step = "init"
    try:
        import Quartz  # type: ignore[import-untyped]

        step = "load_cg"
        cg = _cg_load_display_config_fns()

        step = "main_id"
        old_main_id = int(cg.CGMainDisplayID())
        if old_main_id == vid:
            return 0  # 이미 주 디스플레이

        step = "bounds"
        new_bounds = Quartz.CGDisplayBounds(vid)
        new_main_w = max(1, int(new_bounds.size.width))

        step = "begin_config"
        config_ref = ctypes.c_void_p()
        err = cg.CGBeginDisplayConfiguration(ctypes.byref(config_ref))
        if err != 0:
            try:
                from streaming.remote_log import log_remote_event
                log_remote_event(
                    f"호스트: 가상 디스플레이 주 설정 실패 — CGBeginDisplayConfiguration err={err}",
                    error=True,
                )
            except Exception:
                pass
            return 0

        step = "origin_vd"
        err = cg.CGConfigureDisplayOrigin(
            config_ref, ctypes.c_uint32(vid), ctypes.c_int32(0), ctypes.c_int32(0)
        )
        if err != 0:
            cg.CGCancelDisplayConfiguration(config_ref)
            try:
                from streaming.remote_log import log_remote_event
                log_remote_event(
                    f"호스트: 가상 디스플레이 주 설정 실패 — CGConfigureDisplayOrigin(vd) err={err}",
                    error=True,
                )
            except Exception:
                pass
            return 0

        step = "origin_old"
        err = cg.CGConfigureDisplayOrigin(
            config_ref,
            ctypes.c_uint32(old_main_id),
            ctypes.c_int32(new_main_w),
            ctypes.c_int32(0),
        )
        if err != 0:
            cg.CGCancelDisplayConfiguration(config_ref)
            try:
                from streaming.remote_log import log_remote_event
                log_remote_event(
                    f"호스트: 가상 디스플레이 주 설정 실패 — CGConfigureDisplayOrigin(old) err={err}",
                    error=True,
                )
            except Exception:
                pass
            return 0

        step = "complete"
        # kCGConfigureForSession = 1
        err = cg.CGCompleteDisplayConfiguration(config_ref, ctypes.c_uint32(1))
        if err != 0:
            try:
                from streaming.remote_log import log_remote_event
                log_remote_event(
                    f"호스트: 가상 디스플레이 주 설정 실패 — CGCompleteDisplayConfiguration err={err}",
                    error=True,
                )
            except Exception:
                pass
            return 0

        try:
            from streaming.remote_log import log_remote_event
            log_remote_event(
                f"호스트: 가상 디스플레이(id={vid})를 주 디스플레이로 전환 완료 "
                f"(이전 주 디스플레이 id={old_main_id})"
            )
        except Exception:
            pass
        return old_main_id

    except Exception as exc:
        try:
            from streaming.remote_log import log_remote_event
            log_remote_event(
                f"호스트: 가상 디스플레이 주 설정 예외 (step={step}) — "
                f"{type(exc).__name__}: {exc}",
                error=True,
            )
        except Exception:
            print(
                f"[darwin_vd] set_virtual_display_as_primary 예외 step={step}: {exc}",
                flush=True,
            )
        return 0


def restore_primary_display(old_main_id: int, vd_display_id: int) -> bool:
    """세션 종료 후 주 디스플레이를 원래대로 복원한다.

    ``set_virtual_display_as_primary`` 의 반환값을 ``old_main_id`` 로 전달한다.
    0 이면 변경이 없었으므로 즉시 True 를 반환한다.
    """
    if old_main_id == 0:
        return True
    vid = int(vd_display_id)
    old_id = int(old_main_id)
    step = "init"
    try:
        import Quartz  # type: ignore[import-untyped]

        step = "load_cg"
        cg = _cg_load_display_config_fns()

        step = "bounds"
        vd_bounds = Quartz.CGDisplayBounds(vid)
        vd_w = max(1, int(vd_bounds.size.width))

        step = "begin_config"
        config_ref = ctypes.c_void_p()
        err = cg.CGBeginDisplayConfiguration(ctypes.byref(config_ref))
        if err != 0:
            return False

        step = "origin_old"
        err = cg.CGConfigureDisplayOrigin(
            config_ref, ctypes.c_uint32(old_id), ctypes.c_int32(0), ctypes.c_int32(0)
        )
        if err != 0:
            cg.CGCancelDisplayConfiguration(config_ref)
            return False

        step = "origin_vd"
        err = cg.CGConfigureDisplayOrigin(
            config_ref,
            ctypes.c_uint32(vid),
            ctypes.c_int32(vd_w),
            ctypes.c_int32(0),
        )
        if err != 0:
            cg.CGCancelDisplayConfiguration(config_ref)
            return False

        step = "complete"
        err = cg.CGCompleteDisplayConfiguration(config_ref, ctypes.c_uint32(1))
        if err != 0:
            return False

        try:
            from streaming.remote_log import log_remote_event
            log_remote_event(
                f"호스트: 주 디스플레이 복원 완료 (old_main={old_id}, vd={vid})"
            )
        except Exception:
            pass
        return True

    except Exception as exc:
        print(
            f"[darwin_vd] restore_primary_display 예외 step={step}: {exc}",
            flush=True,
        )
        return False


__all__ = [
    "DarwinVirtualDisplayError",
    "cg_display_bounds",
    "cg_display_id_still_online",
    "create_virtual_display",
    "release_virtual_display",
    "release_virtual_display_on_main_thread",
    "restore_primary_display",
    "set_virtual_display_as_primary",
]
