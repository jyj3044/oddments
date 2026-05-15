"""프로세스 전역 크래시·행·미처리 예외 자동 기록.

개발자가 일일이 ``log_*`` 를 호출하지 않아도 다음을 파일에 남긴다.

* 메인/백그라운드 스레드 미처리 예외
* ``__del__`` 등 unraisable 예외
* asyncio 태스크 예외 (Flet 페이지 연결 후)
* Flet ``page.on_error``
* 세그폴트 등 (``faulthandler`` — ``log_buffers`` 경유)
* UI 이벤트 루프 무응답(Working… 의심) 시 전 스레드 스택 덤프
* 종료 신호·정상 종료(atexit)

주 기록 위치: ``logs/crash-YYYYMMDD.log``, ``logs/app_error-*.log``
"""

from __future__ import annotations

import atexit
import faulthandler
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

_HEARTBEAT_INTERVAL_SEC = 5.0
_HANG_THRESHOLD_SEC = 45.0
_WATCHDOG_POLL_SEC = 10.0

_install_lock = threading.Lock()
_process_installed = False
_ui_attached = False
_ui_sink: Optional[Callable[[str, Optional[str]], None]] = None

_last_ui_heartbeat = 0.0
_hang_episode_reported = False
_watchdog_stop = threading.Event()
_watchdog_thread: threading.Thread | None = None

_prev_excepthook: Any = None
_prev_thread_hook: Any = None
_prev_unraisable: Any = None
_prev_signal_handlers: dict[int, Any] = {}


def _crash_log_dir() -> Path:
    from .log_buffers import _log_dir_path

    return _log_dir_path()


def _runtime_context_lines() -> list[str]:
    lines = [
        f"pid={os.getpid()}",
        f"thread={threading.current_thread().name}",
        f"frozen={getattr(sys, 'frozen', False)}",
    ]
    try:
        lines.append(f"executable={sys.executable}")
    except Exception:
        pass
    try:
        lines.append(f"argv={sys.argv!r}")
    except Exception:
        pass
    return lines


def _append_crash_file(
    source: str,
    message: str,
    *,
    detail: Optional[str] = None,
    level: str = "ERROR",
) -> None:
    try:
        d = _crash_log_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"crash-{datetime.now():%Y%m%d}.log"
        ts = datetime.now().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 72}\n")
            f.write(f"[{ts}] {level} source={source}\n")
            for ln in _runtime_context_lines():
                f.write(f"  {ln}\n")
            f.write(f"message: {message}\n")
            if detail:
                f.write("detail:\n")
                for ln in detail.splitlines():
                    f.write(f"  {ln}\n")
    except OSError:
        pass


def record_exception(
    source: str,
    message: str,
    *,
    detail: Optional[str] = None,
    exc: BaseException | None = None,
    level: str = "ERROR",
) -> None:
    """미처리·포착 예외를 파일에 남긴다. UI 싱크가 연결돼 있으면 스낵도 띄운다."""
    if exc is not None and not detail:
        try:
            detail = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        except Exception:
            detail = repr(exc)
    head = f"[{source}] {message}"
    try:
        from .log_buffers import append_sidecar_error_file, get_log_store, log_app_event

        get_log_store()
        log_app_event(level, head, detail=detail)
    except Exception:
        try:
            from .log_buffers import append_sidecar_error_file

            line = head.replace("\r", " ").replace("\n", " ")
            if detail:
                d0 = str(detail).strip().splitlines()
                if d0:
                    line = f"{line} | {d0[0][:2000]}"
            append_sidecar_error_file("app", line)
        except Exception:
            pass
    _append_crash_file(source, message, detail=detail, level=level)
    sink = _ui_sink
    if sink is not None and level in ("ERROR", "CRITICAL"):
        try:
            sink(message, detail)
        except Exception:
            pass


def record_thread_dump(source: str, reason: str) -> None:
    """모든 스레드의 Python 스택을 crash 로그에 덤프한다 (행/ANR 의심)."""
    detail_parts = [reason, "", "thread stacks:"]
    try:
        import io

        buf = io.StringIO()
        faulthandler.dump_traceback(file=buf, all_threads=True)
        detail_parts.append(buf.getvalue())
    except Exception as exc:
        detail_parts.append(f"(dump failed: {exc})")
    record_exception(source, reason, detail="\n".join(detail_parts), level="CRITICAL")


def log_swallowed(
    source: str,
    exc: BaseException,
    *,
    context: str = "",
) -> None:
    """``except: pass`` 대신 호출 — 삼킨 예외도 crash/app 로그에 남긴다."""
    msg = f"{context}: {type(exc).__name__}: {exc}" if context else f"{type(exc).__name__}: {exc}"
    record_exception(source, msg, exc=exc, level="WARN")


def touch_ui_heartbeat() -> None:
    global _last_ui_heartbeat, _hang_episode_reported
    _last_ui_heartbeat = time.monotonic()
    _hang_episode_reported = False


def _route_to_ui(message: str, detail: Optional[str]) -> None:
    sink = _ui_sink
    if sink is None:
        return
    head = str(message).strip()
    if len(head) > 240:
        head = head[:237] + "…"
    try:
        sink(head, detail)
    except Exception:
        pass


def _on_sys_excepthook(exc_type, exc, tb) -> None:
    try:
        tb_text = "".join(traceback.format_exception(exc_type, exc, tb))
        record_exception(
            "main_thread",
            f"{getattr(exc_type, '__name__', exc_type)}: {exc}",
            detail=tb_text,
        )
    except Exception:
        pass
    if _prev_excepthook is not None:
        try:
            _prev_excepthook(exc_type, exc, tb)
        except Exception:
            pass


def _on_thread_excepthook(args: threading.ExceptHookArgs) -> None:
    try:
        tb_text = "".join(
            traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback
            )
        )
        tname = getattr(args.thread, "name", "?")
        record_exception(
            "background_thread",
            f"[{tname}] {args.exc_type.__name__}: {args.exc_value}",
            detail=tb_text,
        )
    except Exception:
        pass
    if _prev_thread_hook is not None:
        try:
            _prev_thread_hook(args)
        except Exception:
            pass


def _on_unraisablehook(unraisable: sys.UnraisableHookArgs) -> None:  # type: ignore[name-defined]
    try:
        et = unraisable.exc_type
        ev = unraisable.exc_value
        tb = unraisable.exc_traceback
        if et is not None:
            tb_text = "".join(traceback.format_exception(et, ev, tb))
            record_exception(f"unraisable:{unraisable.object!r}", f"{et.__name__}: {ev}", detail=tb_text)
        else:
            record_exception("unraisable", "(exc_type is None)", detail=repr(unraisable))
    except Exception:
        pass
    if _prev_unraisable is not None:
        try:
            _prev_unraisable(unraisable)
        except Exception:
            pass


def _on_signal(signum: int, frame) -> None:
    try:
        name = signal.Signals(signum).name
    except Exception:
        name = str(signum)
    try:
        tb_text = "".join(traceback.format_stack(frame))
    except Exception:
        tb_text = None
    record_exception(
        "signal",
        f"received {name}",
        detail=tb_text,
        level="CRITICAL",
    )
    prev = _prev_signal_handlers.get(signum)
    if callable(prev):
        try:
            prev(signum, frame)
            return
        except Exception:
            pass
    if signum in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        raise SystemExit(128 + signum)


def _on_atexit() -> None:
    try:
        _append_crash_file(
            "atexit",
            "process exiting",
            detail="\n".join(_runtime_context_lines()),
            level="INFO",
        )
    except Exception:
        pass


def _watchdog_loop() -> None:
    global _hang_episode_reported
    while not _watchdog_stop.is_set():
        if _watchdog_stop.wait(_WATCHDOG_POLL_SEC):
            return
        if not _ui_attached:
            continue
        stale = time.monotonic() - _last_ui_heartbeat
        if stale < _HANG_THRESHOLD_SEC:
            _hang_episode_reported = False
            continue
        if _hang_episode_reported:
            continue
        _hang_episode_reported = True
        record_thread_dump(
            "ui_hang_watchdog",
            f"UI heartbeat stale for {stale:.1f}s (threshold={_HANG_THRESHOLD_SEC}s); "
            "Flet may show Working…",
        )


def _start_watchdog() -> None:
    global _watchdog_thread
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, name="crash-ui-watchdog", daemon=True
    )
    _watchdog_thread.start()


def _stop_watchdog() -> None:
    _watchdog_stop.set()


def install_process_crash_handlers() -> None:
    """Flet 창이 뜨기 전에 호출. 프로세스 수명 동안 한 번만 설치된다."""
    global _process_installed, _prev_excepthook, _prev_thread_hook, _prev_unraisable
    with _install_lock:
        if _process_installed:
            return
        try:
            from .log_buffers import get_log_store

            get_log_store()
        except Exception:
            pass
        _prev_excepthook = sys.excepthook
        sys.excepthook = _on_sys_excepthook
        if hasattr(threading, "excepthook"):
            _prev_thread_hook = threading.excepthook
            threading.excepthook = _on_thread_excepthook  # type: ignore[assignment]
        _prev_unraisable = getattr(sys, "unraisablehook", None)
        try:
            sys.unraisablehook = _on_unraisablehook  # type: ignore[assignment]
        except Exception:
            pass
        for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                _prev_signal_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _on_signal)
            except (OSError, ValueError):
                pass
        atexit.register(_on_atexit)
        _process_installed = True


def attach_flet_page(
    page: Any,
    *,
    snack_fn: Callable[[str, Optional[str]], None],
) -> None:
    """Flet 페이지 연결 후 UI 오류 싱크·asyncio·heartbeat·행 감시를 켠다."""
    global _ui_sink, _ui_attached
    install_process_crash_handlers()
    _ui_sink = snack_fn
    _ui_attached = True
    touch_ui_heartbeat()
    _start_watchdog()

    def _on_page_error(e: Any) -> None:
        data = getattr(e, "data", None) or "알 수 없는 오류"
        first_line = str(data).strip().splitlines()[:1]
        head = first_line[0] if first_line else str(data)
        record_exception("flet", head, detail=str(data))

    try:
        page.on_error = _on_page_error  # type: ignore[attr-defined]
    except Exception:
        pass

    import asyncio

    async def _heartbeat_loop() -> None:
        while _ui_attached:
            touch_ui_heartbeat()
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)

    async def _arm_asyncio() -> None:
        loop = asyncio.get_running_loop()
        prev = loop.get_exception_handler()

        def _async_handler(loop_arg: asyncio.AbstractEventLoop, context: dict) -> None:
            try:
                exc = context.get("exception")
                msg = context.get("message", "")
                if exc is not None:
                    tb_text = "".join(
                        traceback.format_exception(
                            type(exc), exc, exc.__traceback__
                        )
                    )
                    record_exception(
                        "asyncio",
                        f"{type(exc).__name__}: {exc}",
                        detail=tb_text,
                    )
                else:
                    bits = [str(msg).strip()] if msg else []
                    for key in ("future", "task", "handle"):
                        obj = context.get(key)
                        if obj is not None:
                            bits.append(f"{key}={obj!r}")
                    record_exception(
                        "asyncio",
                        " ".join(bits) if bits else repr(context),
                    )
            except Exception:
                pass
            if prev is not None:
                try:
                    prev(loop_arg, context)
                except Exception:
                    pass
            else:
                try:
                    loop_arg.default_exception_handler(context)
                except Exception:
                    pass

        loop.set_exception_handler(_async_handler)
        await _heartbeat_loop()

    import asyncio

    try:
        run_task = getattr(page, "run_task", None)
        if callable(run_task):
            run_task(_arm_asyncio)
    except Exception:
        pass


def detach_flet_page() -> None:
    global _ui_attached, _ui_sink
    _ui_attached = False
    _ui_sink = None
    _stop_watchdog()


__all__ = [
    "attach_flet_page",
    "detach_flet_page",
    "install_process_crash_handlers",
    "log_swallowed",
    "record_exception",
    "record_thread_dump",
    "touch_ui_heartbeat",
]
