"""Standalone Flet window for Arduino runtime status."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import flet as ft

from arduino.serial_bridge import arduino_runtime_status_path

from .components import (
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    field_label,
    status_dot,
)
from .theme import StreamMasterTheme as T, body_md, label_md, title_md

_STALE_AFTER_SECONDS = 15.0


def _arg_value(argv: list[str], flag: str) -> str | None:
    try:
        idx = argv.index(flag)
        return argv[idx + 1]
    except (ValueError, IndexError):
        return None


def _parent_pid_from_argv(argv: list[str] | None = None) -> int:
    args = list(sys.argv if argv is None else argv)
    raw = _arg_value(args, "--parent-pid")
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _status_path_from_argv(argv: list[str] | None = None) -> Path | None:
    args = list(sys.argv if argv is None else argv)
    raw = _arg_value(args, "--status-path")
    if not raw:
        return None
    return Path(raw)


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


def _page_loop_open(page: ft.Page) -> bool:
    try:
        loop = page.session.connection.loop
    except Exception:
        return False
    try:
        return not loop.is_closed()
    except Exception:
        return False


def _load_status_snapshot(status_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
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


def _apply_always_on_top(page: ft.Page, enabled: bool) -> None:
    try:
        page.window.always_on_top = bool(enabled)
        page.update()
    except Exception:
        pass


def _start_shutdown_watchdog(
    *,
    parent_pid: int,
) -> None:
    def _loop() -> None:
        while True:
            if _should_exit_for_parent(parent_pid):
                _exit_status_window()
            time.sleep(0.2)

    threading.Thread(
        target=_loop,
        name="arduino-status-shutdown-watchdog",
        daemon=True,
    ).start()


def _normalize_snapshot(
    snapshot: dict[str, Any] | None,
    now: float,
) -> tuple[dict[str, Any] | None, float, bool]:
    """스냅샷·수신 시각·만료 여부. 만료여도 마지막 값은 화면에 유지한다."""
    received_at = now
    stale = True
    if snapshot is not None:
        raw_received = snapshot.get("received_at")
        if raw_received is None:
            received_at = now
        else:
            try:
                received_at = float(raw_received)
            except (TypeError, ValueError):
                received_at = now
        stale = now - received_at > _STALE_AFTER_SECONDS
    return snapshot, received_at, stale


def _status_window_main(
    page: ft.Page,
    *,
    parent_pid: int = 0,
    status_path: str | Path | None = None,
) -> None:
    _start_shutdown_watchdog(parent_pid=parent_pid)

    page.title = "Arduino 상태"
    page.bgcolor = T.SURFACE_BRIGHT
    page.padding = 0
    page.window.width = 460
    page.window.height = 320
    page.window.min_width = 360
    page.window.min_height = 260

    always_on_top_cb = ft.Checkbox(
        label="항상위",
        value=False,
        active_color=T.PRIMARY,
        label_style=label_md(),
        on_change=lambda e: _apply_always_on_top(page, bool(e.control.value)),
    )

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
                            ft.Text(
                                "Arduino 상태",
                                style=title_md(),
                                color=T.ON_SURFACE,
                                expand=True,
                            ),
                            always_on_top_cb,
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

    status_file = Path(status_path) if status_path is not None else arduino_runtime_status_path()
    last_render = {"key": ""}

    def refresh_loop() -> None:
        while True:
            if not _page_loop_open(page):
                time.sleep(0.25)
                continue

            now = time.time()
            snapshot, received_at, stale = _normalize_snapshot(
                _load_status_snapshot(status_file),
                now,
            )
            age_sec = max(0, int(now - received_at))
            if snapshot is None:
                render_key = "none"
            else:
                render_key = (
                    f"{snapshot.get('t')}:{snapshot.get('r')}:{snapshot.get('n')}:"
                    f"{age_sec}:{int(stale)}"
                )
            if render_key == last_render["key"]:
                time.sleep(0.25)
                continue
            last_render["key"] = render_key

            async def _apply() -> None:
                if snapshot is None:
                    type_text.value = "-"
                    state_slot.content = _run_indicator("")
                    next_col.controls = []
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
                                        ft.Text(
                                            str(key),
                                            style=body_md(),
                                            color=T.ON_SURFACE,
                                            expand=True,
                                        ),
                                        ft.Text(
                                            _seconds_text(value),
                                            style=body_md(),
                                            color=T.ON_SURFACE,
                                        ),
                                    ],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                )
                            )
                    next_col.controls = controls
                    suffix = " (만료)" if stale else ""
                    last_seen_text.value = f"{age_sec}초 전{suffix}"

                for ctrl in (type_text, state_slot, next_col, last_seen_text):
                    try:
                        ctrl.update()
                    except Exception:
                        pass
                try:
                    page.update()
                except Exception:
                    _exit_status_window()

            try:
                page.run_task(_apply)
            except Exception:
                _exit_status_window()
            time.sleep(0.25)

    threading.Thread(
        target=refresh_loop,
        name="arduino-status-refresh",
        daemon=True,
    ).start()


def main(page: ft.Page) -> None:
    argv_path = _status_path_from_argv()
    _status_window_main(
        page,
        parent_pid=_parent_pid_from_argv(),
        status_path=argv_path if argv_path is not None else arduino_runtime_status_path(),
    )


if __name__ == "__main__":
    ft.app(target=main)
