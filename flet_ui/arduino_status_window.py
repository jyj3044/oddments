"""Standalone Flet window for Arduino runtime status."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import TimeoutError
from pathlib import Path
from typing import Any

import flet as ft

from arduino.serial_bridge import ARDUINO_RUNTIME_STATUS_PATH

from .components import (
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    field_label,
    status_dot,
)
from .theme import StreamMasterTheme as T, body_md, label_md, title_md

_STALE_AFTER_SECONDS = 3.0


def _parent_pid_from_argv(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    try:
        idx = args.index("--parent-pid")
        return max(0, int(args[idx + 1]))
    except (ValueError, IndexError, TypeError):
        return 0


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))
            if not handle:
                return False
            try:
                return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x00000102
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _should_exit_for_parent(parent_pid: int) -> bool:
    return parent_pid > 0 and not _process_alive(parent_pid)


def _exit_status_window() -> None:
    os._exit(0)


def _parent_watchdog_iteration(
    parent_pid: int,
    *,
    exit_func: Any = _exit_status_window,
) -> None:
    if _should_exit_for_parent(parent_pid):
        exit_func()


def _status_path() -> Path:
    return ARDUINO_RUNTIME_STATUS_PATH


def _load_status_snapshot() -> dict[str, Any] | None:
    try:
        data = json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _run_indicator(run_state: str) -> ft.Row:
    status = {
        "idle": STATUS_IDLE,
        "running": STATUS_ONLINE,
        "paused": STATUS_ERROR,
    }.get(run_state, STATUS_OFFLINE)
    return status_dot(status=status, label=run_state or "수신 대기")


def _seconds_text(ms: object) -> str:
    try:
        seconds = max(0, int(ms)) // 1000
    except (TypeError, ValueError):
        seconds = 0
    return f"{seconds}초"


def _field(label: str, value: ft.Control) -> ft.Row:
    return ft.Row(
        controls=[
            ft.Container(width=92, content=field_label(label)),
            ft.Container(expand=True, content=value),
        ],
        spacing=12,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def _page_loop(page: ft.Page) -> asyncio.AbstractEventLoop | None:
    try:
        loop = page.session.connection.loop
    except Exception:
        return None
    try:
        if loop.is_closed():
            return None
    except Exception:
        return None
    return loop


def _request_window_destroy(page: ft.Page) -> None:
    loop = _page_loop(page)
    if loop is None:
        _exit_status_window()
    try:
        fut = asyncio.run_coroutine_threadsafe(page.window.destroy(), loop)
        fut.result(timeout=1.5)
    except (TimeoutError, Exception):
        pass
    _exit_status_window()


def _start_shutdown_watchdog(
    *,
    page: ft.Page,
    parent_pid: int,
    shutdown_event: Any | None,
) -> None:
    def _loop() -> None:
        while True:
            if shutdown_event is not None:
                try:
                    if shutdown_event.is_set():
                        _request_window_destroy(page)
                except Exception:
                    _exit_status_window()
            if _should_exit_for_parent(parent_pid):
                _request_window_destroy(page)
            time.sleep(0.2)

    threading.Thread(
        target=_loop,
        name="arduino-status-shutdown-watchdog",
        daemon=True,
    ).start()


def _normalize_snapshot(snapshot: dict[str, Any] | None, now: float) -> tuple[dict[str, Any] | None, float]:
    received_at = now
    if snapshot is not None:
        try:
            received_at = float(snapshot.get("received_at") or now)
        except (TypeError, ValueError):
            received_at = now
        if now - received_at > _STALE_AFTER_SECONDS:
            snapshot = None
    return snapshot, received_at


def _status_window_main(
    page: ft.Page,
    *,
    parent_pid: int = 0,
    shutdown_event: Any | None = None,
) -> None:
    _start_shutdown_watchdog(
        page=page,
        parent_pid=parent_pid,
        shutdown_event=shutdown_event,
    )

    page.title = "Arduino 상태"
    page.bgcolor = T.SURFACE_BRIGHT
    page.padding = 0
    page.window.width = 460
    page.window.height = 320
    page.window.min_width = 360
    page.window.min_height = 260

    type_text = ft.Text("-", style=body_md(), color=T.ON_SURFACE)
    state_slot = ft.Container(content=_run_indicator(""))
    next_col = ft.Column(controls=[], spacing=6, tight=True)
    last_seen_text = ft.Text("-", style=label_md(), color=T.ON_SURFACE_VARIANT)

    page.add(
        ft.Container(
            expand=True,
            padding=20,
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.MEMORY, size=20, color=T.PRIMARY),
                            ft.Text("Arduino 상태", style=title_md(), color=T.ON_SURFACE),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    _field("타입", type_text),
                    _field("상태", state_slot),
                    ft.Divider(height=1, color=T.OUTLINE_VARIANT),
                    ft.Text("다음 실행", style=label_md(), color=T.ON_SURFACE_VARIANT),
                    ft.Container(
                        padding=ft.padding.all(12),
                        bgcolor=T.SURFACE_CONTAINER_LOW,
                        border=ft.border.all(1, T.OUTLINE_VARIANT),
                        border_radius=T.RADIUS_SM,
                        content=next_col,
                    ),
                    _field("마지막 수신", last_seen_text),
                ],
                spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )
    )

    last_render = {"key": ""}

    def refresh_loop() -> None:
        while True:
            snapshot, received_at = _normalize_snapshot(_load_status_snapshot(), time.time())
            now = time.time()
            if snapshot is None:
                render_key = "none"
            else:
                render_key = (
                    f"{snapshot.get('t')}:{snapshot.get('r')}:{snapshot.get('n')}:"
                    f"{max(0, int(now - received_at))}"
                )
            if render_key != last_render["key"]:
                last_render["key"] = render_key
                if snapshot is None:
                    type_text.value = "-"
                    state_slot.content = _run_indicator("")
                    next_col.controls = [
                        ft.Text("수신된 상태값이 없습니다.", style=body_md(), color=T.ON_SURFACE_VARIANT)
                    ]
                    last_seen_text.value = "-"
                else:
                    run_state = str(snapshot.get("r") or "")
                    type_text.value = str(snapshot.get("t") or "-")
                    state_slot.content = _run_indicator(run_state)
                    raw_next = snapshot.get("n")
                    controls: list[ft.Control] = []
                    if isinstance(raw_next, dict):
                        for key, value in raw_next.items():
                            controls.append(
                                ft.Row(
                                    controls=[
                                        ft.Text(str(key), style=body_md(), color=T.ON_SURFACE, expand=True),
                                        ft.Text(_seconds_text(value), style=body_md(), color=T.ON_SURFACE),
                                    ],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                )
                            )
                    next_col.controls = controls or [
                        ft.Text("-", style=body_md(), color=T.ON_SURFACE_VARIANT)
                    ]
                    last_seen_text.value = f"{max(0, int(now - received_at))}초 전"
                try:
                    page.update()
                except Exception:
                    _exit_status_window()
            time.sleep(0.25)

    threading.Thread(
        target=refresh_loop,
        name="arduino-status-refresh",
        daemon=True,
    ).start()


def run_status_window(parent_pid: int = 0, shutdown_event: Any | None = None) -> None:
    ft.app(
        target=lambda page: _status_window_main(
            page,
            parent_pid=parent_pid,
            shutdown_event=shutdown_event,
        )
    )


def main(page: ft.Page) -> None:
    _status_window_main(page, parent_pid=_parent_pid_from_argv())


if __name__ == "__main__":
    run_status_window(parent_pid=_parent_pid_from_argv())
