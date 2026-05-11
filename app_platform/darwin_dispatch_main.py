"""macOS: libdispatch 메인 큐에 작업을 넣는다.

Flet·Flutter 기반 앱은 ``CFRunLoopPerformBlock(CFRunLoopGetMain(), ...)`` 로 예약한
블록이 오래(또는 영원히) 실행되지 않는 경우가 있다. 메인 **디스패치 큐**
(``dispatch_get_main_queue``)는 UI 스레드와 동일하게 동작하는 경우가 많아
가상 디스플레이 ``release()`` 같이 AppKit/PyObjC 제약이 있는 코드를 여기에 두면
해제가 실제로 실행된다.

일부 macOS·Python 조합에서는 ``dispatch_get_main_queue`` 가 ctypes/dlsym 으로
잡히지 않는다. 그때는 ``NSOperationQueue.mainQueue()`` 로 스케줄한다."""
from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
from typing import Callable

if sys.platform != "darwin":
    raise ImportError("darwin_dispatch_main 은 macOS 전용입니다.")

_dispatch_lib: ctypes.CDLL | None = None
_dispatch_get_main_queue = None
_dispatch_async_f = None
_dispatch_sync_f = None
_DISPATCH_FUNCTION = None


def _dlsym_rtld_default(name: bytes) -> int | None:
    """단일 dylib 핸들로는 ``dispatch_get_main_queue`` 등이 export 되지 않는 경우가 있어
    프로세스 기본 검색 경로(RTLD_DEFAULT)로 주소를 구한다."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    except OSError:
        return None
    dlsym = libc.dlsym
    dlsym.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    dlsym.restype = ctypes.c_void_p
    rtld_default = ctypes.c_void_p(-2)
    addr = dlsym(rtld_default, name)
    if not addr:
        return None
    v = ctypes.cast(addr, ctypes.c_void_p).value
    if v is None or v == 0:
        return None
    return int(v)


def _bind_dispatch_via_rtld_default() -> bool:
    global _dispatch_get_main_queue, _dispatch_async_f, _dispatch_sync_f, _DISPATCH_FUNCTION
    gmq = _dlsym_rtld_default(b"dispatch_get_main_queue")
    daf = _dlsym_rtld_default(b"dispatch_async_f")
    dsf = _dlsym_rtld_default(b"dispatch_sync_f")
    if not gmq or not daf or not dsf:
        return False
    _dispatch_get_main_queue = ctypes.CFUNCTYPE(ctypes.c_void_p)(gmq)
    _dispatch_async_f = ctypes.CFUNCTYPE(
        None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )(daf)
    _dispatch_sync_f = ctypes.CFUNCTYPE(
        None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
    )(dsf)
    _DISPATCH_FUNCTION = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
    return True


def _bind_dispatch_from_cdll(lib: ctypes.CDLL) -> bool:
    global _dispatch_lib, _dispatch_get_main_queue, _dispatch_async_f
    global _dispatch_sync_f, _DISPATCH_FUNCTION
    try:
        dq = lib.dispatch_get_main_queue
        daf = lib.dispatch_async_f
        dsf = lib.dispatch_sync_f
    except AttributeError:
        return False
    dq.argtypes = []
    dq.restype = ctypes.c_void_p

    daf.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    daf.restype = None

    dsf.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    dsf.restype = None

    _dispatch_lib = lib
    _dispatch_get_main_queue = dq
    _dispatch_async_f = daf
    _dispatch_sync_f = dsf
    _DISPATCH_FUNCTION = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
    return True


def _ensure_dispatch() -> bool:
    global _dispatch_lib, _dispatch_get_main_queue, _dispatch_async_f
    global _dispatch_sync_f, _DISPATCH_FUNCTION
    if _dispatch_get_main_queue is not None:
        return True

    candidates: list[str] = []
    try:
        fl = ctypes.util.find_library("dispatch")
        if fl:
            candidates.append(fl)
    except Exception:
        pass
    candidates.extend(
        [
            "/usr/lib/system/libdispatch.dylib",
            "/usr/lib/libSystem.B.dylib",
        ]
    )
    seen: set[str] = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            continue
        if _bind_dispatch_from_cdll(lib):
            return True
    if _bind_dispatch_via_rtld_default():
        return True
    return False


def _schedule_on_main_nsoperation_queue(fn: Callable[[], None]) -> bool:
    """``dispatch_get_main_queue`` 를 ctypes 로 잡지 못하는 macOS(예: 일부 Python 3.14 빌드)용."""
    try:
        from AppKit import NSOperationQueue  # type: ignore[import-untyped]
    except Exception:
        return False

    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
    return True


def _run_sync_on_main_nsoperation_queue(fn: Callable[[], None]) -> bool:
    """메인 큐 동기 실행 폴백(``NSOperationQueue`` + 세마포)."""
    try:
        from AppKit import NSOperationQueue  # type: ignore[import-untyped]
    except Exception:
        return False

    sem = threading.Semaphore(0)
    err: list[BaseException] = []

    def _block() -> None:
        try:
            fn()
        except BaseException as exc:
            err.append(exc)
        finally:
            sem.release()

    NSOperationQueue.mainQueue().addOperationWithBlock_(_block)
    sem.acquire()
    if err:
        raise err[0]
    return True


# CFUNCTYPE 인스턴스가 GC 되면 C 쪽에서 크래시할 수 있어 실행 전까지 보관한다.
_trampoline_keepalive: list = []


def schedule_on_main_dispatch_queue(fn: Callable[[], None]) -> bool:
    """``dispatch_async(main_queue, ^{ fn(); })`` 에 해당. 성공 시 True."""
    if not _ensure_dispatch():
        return _schedule_on_main_nsoperation_queue(fn)
    assert _dispatch_get_main_queue is not None
    assert _dispatch_async_f is not None
    assert _DISPATCH_FUNCTION is not None

    tramp_ref: list[object] = []

    def _runner(_ctx: ctypes.c_void_p) -> None:
        try:
            fn()
        finally:
            t = tramp_ref[0] if tramp_ref else None
            if t is not None:
                try:
                    _trampoline_keepalive.remove(t)
                except ValueError:
                    pass

    tramp = _DISPATCH_FUNCTION(_runner)
    tramp_ref.append(tramp)
    _trampoline_keepalive.append(tramp)

    queue = _dispatch_get_main_queue()
    _dispatch_async_f(queue, None, tramp)
    return True


def run_sync_on_main_dispatch_queue(fn: Callable[[], None]) -> bool:
    """메인 디스패치 큐에서 ``fn`` 을 **동기** 실행한다.

    백그라운드 스레드에서 호출하면 메인이 블록을 실행할 때까지 여기서 대기한다.
    ``dispatch_async`` 만으로는 메인 런루프가 블록을 집어먹지 않아 해제가 안 되는
    경우가 있어, 가상 디스플레이 ``release()`` 에는 이 경로를 우선한다.

    Cocoa 메인 스레드에서는 ``NSThread.isMainThread()`` 일 때 직접 ``fn()`` 호출.
    메인 큐에 대한 ``dispatch_sync`` 는 **같은 스레드에서 호출하면 데드락**이므로 금지된다.
    """
    try:
        from Foundation import NSThread  # type: ignore[import-untyped]

        if bool(NSThread.isMainThread()):
            fn()
            return True
    except Exception:
        pass

    if not _ensure_dispatch():
        return _run_sync_on_main_nsoperation_queue(fn)
    assert _dispatch_get_main_queue is not None
    assert _dispatch_sync_f is not None
    assert _DISPATCH_FUNCTION is not None

    def _tramp(_ctx: ctypes.c_void_p) -> None:
        fn()

    tramp = _DISPATCH_FUNCTION(_tramp)
    _trampoline_keepalive.append(tramp)
    try:
        queue = _dispatch_get_main_queue()
        _dispatch_sync_f(queue, None, tramp)
        return True
    finally:
        try:
            _trampoline_keepalive.remove(tramp)
        except ValueError:
            pass


__all__ = [
    "run_sync_on_main_dispatch_queue",
    "schedule_on_main_dispatch_queue",
]
