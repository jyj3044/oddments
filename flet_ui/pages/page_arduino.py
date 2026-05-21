"""Arduino Link 페이지 — COM 연결, 포커스 제어, 전송 키."""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

import flet as ft

from ..components import (
    dropdown,
    field_label,
    outline_button,
    primary_button,
    section_card,
    text_field,
)
from ..state import (
    AppState,
    list_com_ports,
    log_arduino_notice,
)
from ..theme import (
    StreamMasterTheme as T,
    body_md,
    label_md,
)

KEY_CHOICES_FALLBACK = (
    "(선택하면 목록에 추가)",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    "numpad0", "numpad1", "numpad2", "numpad3", "numpad4",
    "numpad5", "numpad6", "numpad7", "numpad8", "numpad9",
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l",
    "m", "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "space", "enter", "esc", "tab", "shift", "ctrl", "alt",
)

# 아두이노 탭 재진입 시 이전 폴링 스레드 정리. (Flet 0.85 Column 은 on_mount 가 거의 오지 않음)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_prev_arduino_log_ctrl: _ArduinoPageController | None = None
_runtime_status_processes_lock = threading.Lock()
_runtime_status_processes: list[subprocess.Popen[bytes]] = []


def _kill_status_subprocess(proc: subprocess.Popen[bytes]) -> None:
    """Flet 보조 창은 자식 프로세스까지 묶여 있어 Windows 에서 트리 단위로 종료한다."""
    if proc.poll() is not None:
        return
    pid = proc.pid
    if sys.platform == "win32" and pid:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def terminate_arduino_status_windows() -> None:
    with _runtime_status_processes_lock:
        entries = list(_runtime_status_processes)
        _runtime_status_processes.clear()
    for proc in entries:
        _kill_status_subprocess(proc)


atexit.register(terminate_arduino_status_windows)


def _page_loop_open(page: ft.Page) -> bool:
    try:
        loop = page.session.connection.loop
    except Exception:
        return False
    try:
        return not loop.is_closed()
    except Exception:
        return False


class _ArduinoPageController:
    def __init__(self, state: AppState) -> None:
        self.state = state
        self.page: ft.Page | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status_text: ft.Text | None = None
        self.connect_btn: ft.FilledButton | None = None
        self.apply_baud_state = None  # type: ignore[assignment]
        self._last_active: bool | None = None
        self._mounted = False

    def launch_runtime_status_window(self) -> None:
        from arduino.serial_bridge import arduino_runtime_status_path

        parent_pid = os.getpid()
        status_path = str(arduino_runtime_status_path().resolve())
        if getattr(sys, "frozen", False):
            cmd = [
                sys.executable,
                "--arduino-status-window",
                "--parent-pid",
                str(parent_pid),
                "--status-path",
                status_path,
            ]
        else:
            main_py = _PROJECT_ROOT / "main.py"
            if not main_py.is_file():
                log_arduino_notice("[상태] Arduino 상태창 실행 실패: main.py 없음")
                return
            cmd = [
                sys.executable,
                str(main_py),
                "--arduino-status-window",
                "--parent-pid",
                str(parent_pid),
                "--status-path",
                status_path,
            ]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(_PROJECT_ROOT),
            )
            with _runtime_status_processes_lock:
                _runtime_status_processes[:] = [
                    p for p in _runtime_status_processes if p.poll() is None
                ]
                _runtime_status_processes.append(proc)
            log_arduino_notice("[상태] Arduino 상태창 실행")
        except OSError as exc:
            log_arduino_notice(f"[상태] Arduino 상태창 실행 실패: {exc}")

    def start(self, page: ft.Page) -> None:
        self.page = page
        self._mounted = True
        if self._thread is not None:
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.is_set():
                self._tick()
                if self._stop.wait(0.2):
                    return

        self._thread = threading.Thread(
            target=_loop, name="flet-arduino-log", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._mounted = False
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None

    def _tick(self) -> None:
        if not self._mounted:
            return
        active = self.state.arduino_active()
        active_changed = active != self._last_active
        if not active_changed:
            return
        page = self.page
        if page is None:
            return
        if not _page_loop_open(page):
            self._mounted = False
            return

        status = self.status_text
        btn = self.connect_btn
        apply_baud = self.apply_baud_state
        err_text = self.state.arduino_last_error() or "미연결"
        self._last_active = active

        async def _apply(
            _active=active,
            _changed=active_changed,
            _err=err_text,
        ) -> None:
            try:
                if _changed and status is not None:
                    if _active:
                        status.value = "● 연결됨"
                        status.color = T.SUCCESS
                    else:
                        status.value = f"● {_err}"
                        status.color = T.ON_SURFACE_VARIANT
                    try:
                        status.update()
                    except Exception:
                        pass
                if _changed and btn is not None:
                    desired = "해제" if _active else "연결"
                    btn.content = desired
                    try:
                        btn.update()
                    except Exception:
                        pass
                if _changed and apply_baud is not None:
                    try:
                        apply_baud(_active)
                    except Exception:
                        pass
                try:
                    page.update()
                except Exception:
                    pass
            except Exception:
                pass

        try:
            if not _page_loop_open(page):
                self._mounted = False
                return
            page.run_task(_apply)
        except Exception:
            self._mounted = False


def build_arduino_link(state: AppState) -> ft.Control:
    global _prev_arduino_log_ctrl

    if _prev_arduino_log_ctrl is not None:
        _prev_arduino_log_ctrl.shutdown()
        _prev_arduino_log_ctrl = None

    ard = state.settings.arduino
    ctrl = _ArduinoPageController(state)

    com_options = [p[0] for p in list_com_ports()] or [ard.port]
    if ard.port and ard.port not in com_options:
        com_options.insert(0, ard.port)

    com_dropdown = dropdown(
        label=None,
        value=ard.port,
        options=com_options,
        expand=True,
    )

    def _on_com_change(_e: ft.ControlEvent) -> None:
        if com_dropdown.value:
            ard.port = com_dropdown.value

    com_dropdown.on_select = _on_com_change

    def _refresh_ports() -> None:
        opts = [p[0] for p in list_com_ports()]
        if ard.port and ard.port not in opts:
            opts.insert(0, ard.port)
        com_dropdown.options = [ft.dropdown.Option(o) for o in opts]
        if com_dropdown.page is not None:
            try:
                com_dropdown.update()
            except Exception:
                pass

    def _on_com_focus(_e: ft.ControlEvent) -> None:
        _refresh_ports()

    try:
        com_dropdown.on_focus = _on_com_focus  # type: ignore[attr-defined]
    except Exception:
        pass

    baud_field = text_field(
        value=str(ard.baud),
        expand=True,
        keyboard_type=ft.KeyboardType.NUMBER,
    )

    def _apply_baud_state(active: bool) -> None:
        baud_field.read_only = active
        baud_field.bgcolor = T.SURFACE_CONTAINER_HIGH if active else T.SURFACE_CONTAINER_LOWEST
        try:
            if baud_field.page is not None:
                baud_field.update()
        except Exception:
            pass

    _apply_baud_state(state.arduino_active())

    def _on_baud_change(_e: ft.ControlEvent) -> None:
        try:
            ard.baud = max(1200, int(baud_field.value or "115200"))
        except ValueError:
            pass

    baud_field.on_change = _on_baud_change

    connect_btn = primary_button(
        "해제" if state.arduino_active() else "연결",
        on_click=lambda _e: _toggle_connect(state, connect_btn, _apply_baud_state),
    )

    status_text = ft.Text("● 미연결", style=label_md(), color=T.ON_SURFACE_VARIANT)

    connection_card = section_card(
        title="연결 설정",
        icon=ft.Icons.SETTINGS_INPUT_COMPONENT,
        actions=[
            outline_button(
                "상태창",
                icon=ft.Icons.INFO_OUTLINE,
                on_click=lambda _e: ctrl.launch_runtime_status_window(),
            )
        ],
        expand=True,
        content=ft.Column(
            spacing=T.SPACE_MD,
            expand=True,
            controls=[
                ft.Column(
                    spacing=8,
                    controls=[
                        field_label("COM 포트"),
                        com_dropdown,
                    ],
                ),
                ft.Column(
                    spacing=8,
                    controls=[
                        field_label("보드레이트"),
                        ft.Row(
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[baud_field, connect_btn],
                        ),
                    ],
                ),
                status_text,
            ],
        ),
    )

    focus_cb = ft.Checkbox(
        label="선택 창 포커스 획득·해제 시 키 전송",
        value=ard.focus_event_enabled,
        active_color=T.PRIMARY,
        on_change=lambda e: _set_focus_enabled(state, e.control.value),
        label_style=body_md(),
    )

    focus_keys = list(KEY_CHOICES_FALLBACK)
    gain_dd = dropdown(
        value=ard.focus_event_key_gain,
        options=focus_keys,
        expand=True,
    )
    loss_dd = dropdown(
        value=ard.focus_event_key_loss,
        options=focus_keys,
        expand=True,
    )

    def _on_gain_change(_e: ft.ControlEvent) -> None:
        ard.focus_event_key_gain = gain_dd.value or ard.focus_event_key_gain

    def _on_loss_change(_e: ft.ControlEvent) -> None:
        ard.focus_event_key_loss = loss_dd.value or ard.focus_event_key_loss

    gain_dd.on_select = _on_gain_change
    loss_dd.on_select = _on_loss_change

    focus_card = section_card(
        title="포커스 제어",
        icon=ft.Icons.HIGHLIGHT,
        expand=True,
        content=ft.Column(
            spacing=T.SPACE_MD,
            expand=True,
            controls=[
                focus_cb,
                ft.Row(
                    spacing=16,
                    controls=[
                        ft.Column(
                            expand=True,
                            spacing=8,
                            controls=[field_label("포커스 획득 시"), gain_dd],
                        ),
                        ft.Column(
                            expand=True,
                            spacing=8,
                            controls=[field_label("포커스 해제 시"), loss_dd],
                        ),
                    ],
                ),
            ],
        ),
    )

    PLACEHOLDER_OPTION = "(선택하면 목록에 추가)"
    add_dd = dropdown(
        value=PLACEHOLDER_OPTION, options=focus_keys, expand=True
    )

    keys_list = ft.ListView(spacing=0, expand=True)
    selected_keys: list[str] = [
        k.strip() for k in ard.keys.split(",") if k.strip()
    ]
    selection_state = {"idx": -1}

    def _refresh_keys_list() -> None:
        keys_list.controls = []
        sel = selection_state["idx"]
        for i, k in enumerate(selected_keys):
            picked = i == sel
            row = ft.Container(
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                bgcolor="#1f1a73e8" if picked else None,
                border=(
                    ft.border.all(1, T.PRIMARY)
                    if picked
                    else ft.border.all(1, "#00000000")
                ),
                border_radius=T.RADIUS_SM,
                margin=ft.margin.symmetric(horizontal=4, vertical=2),
                ink=True,
                on_click=lambda _e, idx=i: _select_key(idx),
                content=ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Icon(
                            ft.Icons.CHECK_CIRCLE if picked else ft.Icons.RADIO_BUTTON_UNCHECKED,
                            color=T.PRIMARY if picked else T.OUTLINE_VARIANT,
                            size=16,
                        ),
                        ft.Text(
                            k,
                            style=body_md(),
                            color=T.ON_SURFACE,
                            weight=ft.FontWeight.BOLD if picked else None,
                        ),
                    ],
                ),
            )
            keys_list.controls.append(row)

    def _select_key(idx: int) -> None:
        if idx < 0 or idx >= len(selected_keys):
            return
        selection_state["idx"] = -1 if selection_state["idx"] == idx else idx
        _refresh_keys_list()
        if keys_list.page is not None:
            try:
                keys_list.update()
            except Exception:
                pass

    _refresh_keys_list()

    def _on_add_key(_e: ft.ControlEvent) -> None:
        v = (add_dd.value or "").strip()
        if not v or v == PLACEHOLDER_OPTION or v.startswith("("):
            return
        if v not in selected_keys:
            selected_keys.append(v)
            ard.keys = ",".join(selected_keys)
            _refresh_keys_list()
            if keys_list.page is not None:
                try:
                    keys_list.update()
                except Exception:
                    pass
        # 같은 항목을 다시 고를 수 있도록 placeholder 로 되돌리기
        add_dd.value = PLACEHOLDER_OPTION
        if add_dd.page is not None:
            try:
                add_dd.update()
            except Exception:
                pass

    add_dd.on_select = _on_add_key

    def _on_clear_all(_e: ft.ControlEvent) -> None:
        selected_keys.clear()
        ard.keys = ""
        selection_state["idx"] = -1
        _refresh_keys_list()
        if keys_list.page is not None:
            keys_list.update()

    def _on_remove_selected(_e: ft.ControlEvent) -> None:
        idx = selection_state["idx"]
        if idx < 0 or idx >= len(selected_keys):
            return
        selected_keys.pop(idx)
        ard.keys = ",".join(selected_keys)
        if not selected_keys:
            selection_state["idx"] = -1
        else:
            selection_state["idx"] = min(idx, len(selected_keys) - 1)
        _refresh_keys_list()
        if keys_list.page is not None:
            keys_list.update()

    keys_card = section_card(
        title="전송할 키",
        icon=ft.Icons.KEYBOARD,
        content=ft.Column(
            spacing=T.SPACE_MD,
            controls=[
                ft.Row(
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Column(
                            expand=True,
                            spacing=12,
                            controls=[
                                add_dd,
                                ft.Container(
                                    height=200,
                                    border=ft.border.all(1, T.OUTLINE_VARIANT),
                                    border_radius=T.RADIUS_DEFAULT,
                                    bgcolor=T.SURFACE_BRIGHT,
                                    content=keys_list,
                                ),
                            ],
                        ),
                        ft.Column(
                            spacing=8,
                            width=120,
                            controls=[
                                outline_button(
                                    "선택 제거", on_click=_on_remove_selected, danger=True
                                ),
                                outline_button(
                                    "전부 비우기", on_click=_on_clear_all, danger=True
                                ),
                            ],
                        ),
                    ],
                ),
            ],
        ),
    )

    ctrl.status_text = status_text
    ctrl.connect_btn = connect_btn
    ctrl.apply_baud_state = _apply_baud_state  # type: ignore[assignment]
    top_row = ft.Row(
        controls=[
            ft.Container(content=connection_card, expand=True),
            ft.Container(content=focus_card, expand=True),
        ],
        spacing=T.GUTTER,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        intrinsic_height=True,
    )

    page_root = ft.Column(
        controls=[
            top_row,
            keys_card,
        ],
        spacing=T.GUTTER,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        scroll=ft.ScrollMode.AUTO,
    )

    def _on_mount(e: ft.ControlEvent) -> None:
        if e.page is not None:
            ctrl.start(e.page)

    page_root.on_mount = _on_mount  # type: ignore[attr-defined]

    _prev_arduino_log_ctrl = ctrl
    page_obj = getattr(state, "page", None)
    if isinstance(page_obj, ft.Page):
        ctrl.start(page_obj)

    return page_root


def _set_focus_enabled(state: AppState, value: bool | None) -> None:
    state.settings.arduino.focus_event_enabled = bool(value)


def _toggle_connect(
    state: AppState,
    btn: ft.FilledButton,
    apply_baud_state: "Callable[[bool], None] | None" = None,
) -> None:
    page = getattr(state, "page", None)
    if state.arduino_active():
        state.arduino_disconnect()
        log_arduino_notice("[상태] 연결 종료")
        btn.content = "연결"
        if apply_baud_state is not None:
            try:
                apply_baud_state(False)
            except Exception:
                pass
    else:
        ok, err = state.arduino_connect()
        if ok:
            log_arduino_notice("[상태] 연결 성공")
            btn.content = "해제"
            if apply_baud_state is not None:
                try:
                    apply_baud_state(True)
                except Exception:
                    pass
        else:
            log_arduino_notice(f"[상태] 연결 실패: {err}")
            if page is not None:
                show_snack = getattr(page, "show_snack_bar", None)
                snack = ft.SnackBar(
                    content=ft.Text(f"연결 실패: {err}"),
                    bgcolor=T.ERROR,
                )
                if callable(show_snack):
                    try:
                        show_snack(snack)
                    except Exception:
                        pass
                else:
                    try:
                        page.snack_bar = snack  # type: ignore[attr-defined]
                        snack.open = True
                    except Exception:
                        pass
    if btn.page is not None:
        try:
            btn.update()
        except Exception:
            pass
    if page is not None:
        try:
            page.update()
        except Exception:
            pass


def shutdown_arduino_log_poller_if_any() -> None:
    """다른 탭으로 나갈 때 호출 — 폴링 스레드를 멈추고 큐 중복 소비를 막는다."""
    global _prev_arduino_log_ctrl

    if _prev_arduino_log_ctrl is not None:
        _prev_arduino_log_ctrl.shutdown()
        _prev_arduino_log_ctrl = None


__all__ = ["build_arduino_link", "shutdown_arduino_log_poller_if_any"]
