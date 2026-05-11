"""원격 제어 설정 — 호스트/클라이언트는 한 페이지 안에서 탭으로 전환."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable

import flet as ft

from streaming.remote_presets import PRESET_LABELS

from ..components import (
    close_active_dialog,
    outline_button,
    section_card,
    show_snack,
    text_field,
)
from ..state import AppState
from ..theme import (
    StreamMasterTheme as T,
    body_md,
    button_style_click_cursor,
    headline_sm,
    label_md,
    title_lg,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _clamp_port_str(raw: str, default: int) -> int:
    try:
        p = int(str(raw).strip())
    except (TypeError, ValueError):
        p = default
    return max(1, min(65535, p))


def _remote_host_monitor_button_label(state: AppState) -> str:
    hp = state.settings.remote.host
    if sys.platform == "darwin" and bool(hp.use_virtual_display):
        return "가상 디스플레이"
    idx = int(hp.monitor_index or 1)
    cache = getattr(state, "_monitor_cache", []) or []
    for m in cache:
        try:
            if int(m.get("index", -1)) == idx:
                w = int(m.get("width", 0))
                h = int(m.get("height", 0))
                return f"모니터 {idx} ({w}×{h})"
        except (TypeError, ValueError):
            continue
    return f"모니터 {idx}"


def _set_remote_host_monitor(
    state: AppState,
    idx: int,
    on_picked: Callable[[], None] | None = None,
) -> None:
    if sys.platform == "darwin":
        state.settings.remote.host.use_virtual_display = False
    state.settings.remote.host.monitor_index = max(1, int(idx))
    state.save()
    page = getattr(state, "page", None)
    if page is None:
        return
    close_active_dialog(page)
    if on_picked is not None:
        try:
            on_picked()
        except Exception:
            pass


def _set_remote_host_virtual_display(
    state: AppState,
    on_picked: Callable[[], None] | None = None,
) -> None:
    state.settings.remote.host.use_virtual_display = True
    state.save()
    page = getattr(state, "page", None)
    if page is None:
        return
    close_active_dialog(page)
    if on_picked is not None:
        try:
            on_picked()
        except Exception:
            pass


def _open_remote_host_monitor_picker(
    state: AppState,
    on_picked: Callable[[], None] | None = None,
) -> None:
    """대시보드 전체화면 모니터 선택과 동일한 모달."""
    page = getattr(state, "page", None)
    if page is None:
        return
    monitors = state.list_monitors()
    try:
        state._monitor_cache = monitors  # type: ignore[attr-defined]
    except Exception:
        pass
    _darwin = sys.platform == "darwin"
    if not monitors and not _darwin:
        show_snack(
            page,
            "사용 가능한 모니터가 없습니다.",
            severity="warning",
        )
        return

    prev_keyboard = getattr(page, "on_keyboard_event", None)
    closed = {"v": False}

    def _restore_keyboard() -> None:
        try:
            page.on_keyboard_event = prev_keyboard  # type: ignore[attr-defined]
        except Exception:
            pass

    def _close_self() -> None:
        if closed["v"]:
            return
        closed["v"] = True
        _restore_keyboard()
        close_active_dialog(page)

    items: list[ft.Control] = []
    if _darwin:
        items.append(
            ft.ListTile(
                leading=ft.Icon(ft.Icons.CAST_CONNECTED, color=T.PRIMARY),
                title=ft.Text("가상 디스플레이"),
                subtitle=ft.Text(
                    "물리 모니터 미송출 · 연결 시 클라이언트 preset 해상도",
                    style=label_md(),
                    color=T.ON_SURFACE_VARIANT,
                ),
                on_click=lambda _e: (
                    _restore_keyboard(),
                    _set_remote_host_virtual_display(state, on_picked),
                ),
            )
        )
    for m in monitors:
        idx = int(m["index"])
        w = int(m["width"])
        h = int(m["height"])
        name = (m.get("name") or "").strip()
        sub_controls: list[ft.Control] = []
        if name:
            sub_controls.append(
                ft.Text(name, style=label_md(), color=T.ON_SURFACE_VARIANT)
            )
        sub_controls.append(
            ft.Text(f"{w}×{h}", style=label_md(), color=T.ON_SURFACE_VARIANT)
        )
        items.append(
            ft.ListTile(
                title=ft.Text(f"Monitor {idx}"),
                subtitle=ft.Column(
                    controls=sub_controls, spacing=2, tight=True
                ),
                on_click=lambda _e, i=idx: (
                    _restore_keyboard(),
                    _set_remote_host_monitor(state, i, on_picked),
                ),
            )
        )

    dialog = ft.AlertDialog(
        modal=False,
        title=ft.Text("모니터 선택", style=title_lg()),
        content=ft.Container(
            width=520,
            height=min(480, 80 + len(items) * 72),
            content=ft.ListView(controls=items, spacing=4),
        ),
        actions=[
            ft.TextButton(
                "닫기",
                on_click=lambda _e: _close_self(),
                style=button_style_click_cursor(ft.ButtonStyle()),
            ),
        ],
        on_dismiss=lambda _e=None: (closed.__setitem__("v", True), _restore_keyboard()),
    )

    def _on_key(e: ft.KeyboardEvent) -> None:
        try:
            k = str(getattr(e, "key", "")).lower()
        except Exception:
            k = ""
        if k in ("escape", "esc"):
            _close_self()
            return
        if callable(prev_keyboard):
            try:
                prev_keyboard(e)
            except Exception:
                pass

    try:
        page.on_keyboard_event = _on_key  # type: ignore[attr-defined]
    except Exception:
        pass

    show = getattr(page, "show_dialog", None)
    if callable(show):
        try:
            show(dialog)
            return
        except Exception:
            pass
    try:
        page.dialog = dialog  # type: ignore[attr-defined]
        dialog.open = True
        page.update()
    except Exception:
        _restore_keyboard()


def launch_remote_viewer_process() -> tuple[bool, str]:
    """별도 OS 창(Flet 프로세스)으로 원격 뷰어를 연다. 멀티윈도 미지원 분기."""

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--remote-viewer"]
    else:
        main_py = _PROJECT_ROOT / "main.py"
        if not main_py.is_file():
            return False, "main.py 를 찾을 수 없습니다."
        cmd = [sys.executable, str(main_py), "--remote-viewer"]
    try:
        subprocess.Popen(cmd, cwd=str(_PROJECT_ROOT))
    except OSError as exc:
        return False, str(exc)
    return True, ""


def build_remote_settings(state: AppState) -> ft.Control:
    rem = state.settings.remote
    hp = rem.host
    cp = rem.client
    try:
        state._monitor_cache = state.list_monitors()  # type: ignore[attr-defined]
    except Exception:
        pass

    _host_row_h = 48
    port_host = text_field(
        label="수신 포트",
        value=str(hp.listen_port),
        expand=True,
        height=_host_row_h,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: _persist_host_port(state, e.control.value),
    )
    def _refresh_remote_monitor_btn() -> None:
        lbl = _remote_host_monitor_button_label(state)
        try:
            monitor_btn.text = lbl
            monitor_btn.update()
        except Exception:
            try:
                monitor_btn.content = lbl
                monitor_btn.update()
            except Exception:
                pass

    monitor_btn = outline_button(
        _remote_host_monitor_button_label(state),
        icon=ft.Icons.MONITOR_OUTLINED,
        on_click=lambda _e: _open_remote_host_monitor_picker(
            state, _refresh_remote_monitor_btn
        ),
    )
    monitor_btn.height = int(_host_row_h)
    fps_field = text_field(
        label="FPS",
        value=str(hp.stream_fps),
        expand=True,
        height=_host_row_h,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: _persist_host_fps(state, e.control.value),
    )
    h264_hw = ft.Checkbox(
        label="H.264 GPU 인코딩 (NVENC / AMF / VideoToolbox)",
        value=bool(hp.h264_hardware_encode),
        on_change=lambda e: _persist_h264_hw(state, bool(e.control.value)),
    )
    host_auth = text_field(
        label="연결 비밀번호 (필수 · 맥 호스트)"
        if sys.platform == "darwin"
        else "연결 비밀번호 (선택)",
        value=hp.auth_token,
        password=True,
        hint="맥 호스트는 필수 · 클라이언트와 동일"
        if sys.platform == "darwin"
        else "비우면 인증 없음 · 클라이언트와 동일",
        expand=True,
        on_change=lambda e: _persist_host_auth(state, e.control.value),
    )

    vd_test_refresh_hooks: list[Callable[[], None]] = []
    mac_controls: list[ft.Control] = []
    if sys.platform == "darwin":
        vd_test_status = ft.Text(
            "",
            style=body_md(),
            color=T.ON_SURFACE_VARIANT,
        )
        vd_test_create_btn = ft.OutlinedButton(
            text="VD 테스트 생성",
            icon=ft.Icons.ADD_TO_QUEUE_ROUNDED,
            style=button_style_click_cursor(
                ft.ButtonStyle(
                    color=T.ON_SURFACE,
                    side=ft.BorderSide(1, T.OUTLINE),
                    padding=ft.padding.symmetric(horizontal=14, vertical=10),
                )
            ),
        )
        vd_test_release_btn = ft.OutlinedButton(
            text="VD 테스트 해제",
            icon=ft.Icons.REMOVE_FROM_QUEUE_ROUNDED,
            style=button_style_click_cursor(
                ft.ButtonStyle(
                    color=T.ON_SURFACE,
                    side=ft.BorderSide(1, T.OUTLINE),
                    padding=ft.padding.symmetric(horizontal=14, vertical=10),
                )
            ),
        )

        def _vd_ui_diag(where: str, exc: BaseException | None = None) -> None:
            """VD 테스트 버튼·async 흐름 예외를 ``remote`` 로그에 남겨 디버깅한다."""
            try:
                from streaming.remote_log import log_remote_diag

                if exc is None:
                    log_remote_diag(f"VD테스트 UI | {where}")
                    return
                log_remote_diag(
                    f"VD테스트 UI | {where} | {type(exc).__name__}: {exc!r} | "
                    f"{traceback.format_exc()}",
                    error=True,
                )
            except Exception:
                pass

        async def _vd_await_thread(
            target: Callable[[], tuple[bool, str]],
            *,
            name: str,
        ) -> tuple[bool, str]:
            """CG/PyObjC 블로킹은 ``asyncio.to_thread``(기본 풀 ``asyncio_N``)보다
            전용 ``threading.Thread`` 가 재생성 시 덜 꼬인다(``initWithDescriptor`` 정지 완화).
            """
            loop = asyncio.get_running_loop()
            fut = loop.create_future()

            def _runner() -> None:
                try:
                    r = target()
                except Exception as exc:
                    loop.call_soon_threadsafe(fut.set_exception, exc)
                else:
                    loop.call_soon_threadsafe(fut.set_result, r)

            threading.Thread(target=_runner, daemon=True, name=name).start()
            return await fut

        def _sync_vd_test_controls() -> None:
            host_on = state.remote_host_active()
            test_on = state.vd_test_display_active()
            releasing = state.vd_test_release_in_progress()
            creating = state.vd_test_create_in_progress()
            vd_opt = bool(hp.use_virtual_display)
            vd_test_create_btn.disabled = (
                host_on or test_on or not vd_opt or releasing or creating
            )
            vd_test_release_btn.disabled = (
                host_on or not test_on or not vd_opt or releasing
            )
            if releasing:
                vd_test_status.value = (
                    "VD 테스트: 해제 중… (잠시만 기다리세요. 완료 후 버튼이 갱신됩니다)"
                )
            elif creating:
                vd_test_status.value = (
                    "VD 테스트: 생성 중… (시스템이 준비될 때까지 걸릴 수 있습니다)"
                )
            elif test_on:
                vd_test_status.value = (
                    "VD 테스트: 켜짐 (1280×720) — 시스템 설정 → 디스플레이에서 확인"
                )
            elif host_on:
                vd_test_status.value = (
                    "VD 테스트: 원격 호스트 실행 중에는 사용할 수 없습니다."
                )
            elif not vd_opt:
                vd_test_status.value = (
                    "VD 테스트: 위 스위치를 켠 뒤에만 사용할 수 있습니다."
                )
            else:
                vd_test_status.value = (
                    "VD 테스트: 없음 — 호스트를 끈 상태에서 생성/해제로 "
                    "CGVirtualDisplay 경로를 점검합니다."
                )
            try:
                vd_test_status.update()
            except Exception:
                pass
            try:
                vd_test_create_btn.update()
                vd_test_release_btn.update()
            except Exception:
                pass

        def _on_vd_test_create(_e: ft.ControlEvent) -> None:
            # 블로킹은 메인 코루틴 밖으로: ``asyncio.to_thread`` 대신 전용 스레드(위
            # ``_vd_await_thread``) — 기본 스레드 풀에서 CG 재생성이 멈추는 경우가 있다.
            # UI 갱신·스낵만 메인 ``run_task`` 코루틴 안에서 한다.
            pg = getattr(state, "page", None)
            rt = getattr(pg, "run_task", None) if pg is not None else None

            async def _vd_create_flow() -> None:
                ok, msg = False, "내부 오류"
                try:
                    if pg is None:
                        return
                    await asyncio.sleep(0)
                    try:
                        _sync_vd_test_controls()
                    except Exception as exc:
                        _vd_ui_diag("생성 직전 _sync_vd_test_controls", exc)
                    ok, msg = await _vd_await_thread(
                        state.vd_test_create_display,
                        name="oddments-vd-test-create-ui",
                    )
                except Exception as exc:
                    _vd_ui_diag("생성 async 흐름(전용 스레드 대기 포함)", exc)
                    ok, msg = False, f"VD 테스트 생성 중 오류: {exc}"
                try:
                    if pg is not None:
                        show_snack(
                            pg,
                            msg,
                            severity="warning" if not ok else "info",
                        )
                except Exception as exc:
                    _vd_ui_diag("생성 show_snack", exc)
                try:
                    _sync_vd_test_controls()
                except Exception as exc:
                    _vd_ui_diag("생성 완료 후 _sync_vd_test_controls", exc)

            if callable(rt):
                try:
                    rt(_vd_create_flow)
                except Exception as exc:
                    _vd_ui_diag("생성 page.run_task 스케줄", exc)
                return

            def _work_fallback() -> None:
                ok, msg = False, "내부 오류"
                try:
                    ok, msg = state.vd_test_create_display()
                except Exception as exc:
                    _vd_ui_diag("생성 스레드 폴백 vd_test_create_display", exc)
                    ok, msg = False, f"VD 테스트 생성 중 오류: {exc}"
                if pg is not None:
                    try:
                        show_snack(
                            pg,
                            msg,
                            severity="warning" if not ok else "info",
                        )
                    except Exception as exc:
                        _vd_ui_diag("생성 폴백 show_snack", exc)
                try:
                    _sync_vd_test_controls()
                except Exception as exc:
                    _vd_ui_diag("생성 폴백 _sync_vd_test_controls", exc)

            threading.Thread(
                target=_work_fallback,
                daemon=True,
                name="oddments-vd-test-create-ui",
            ).start()

        def _on_vd_test_release(_e: ft.ControlEvent) -> None:
            # ``vd_test_release_display`` 는 내부에서 join 한다. ``asyncio.to_thread`` 풀보다
            # 전용 스레드로 돌리고, ``after_busy`` 는 ``call_soon_threadsafe`` 로만 메인에 넘긴다.
            pg = getattr(state, "page", None)
            rt = getattr(pg, "run_task", None) if pg is not None else None

            async def _vd_release_flow() -> None:
                ok, msg = False, "내부 오류"
                if pg is None:
                    return
                if not callable(rt):
                    try:
                        ok, msg = state.vd_test_release_display()
                    except Exception as exc:
                        _vd_ui_diag("해제 run_task 없음 vd_test_release_display", exc)
                        ok, msg = False, f"VD 테스트 해제 중 오류: {exc}"
                    try:
                        if pg is not None:
                            show_snack(
                                pg,
                                msg,
                                severity="warning" if not ok else "info",
                            )
                    except Exception as exc:
                        _vd_ui_diag("해제(run_task없음) show_snack", exc)
                    try:
                        _sync_vd_test_controls()
                    except Exception as exc:
                        _vd_ui_diag("해제(run_task없음) _sync_vd_test_controls", exc)
                    return

                loop = asyncio.get_running_loop()
                try:
                    await asyncio.sleep(0)
                except Exception as exc:
                    _vd_ui_diag("해제 async sleep(0)", exc)

                def after_busy() -> None:
                    def on_main() -> None:
                        async def _sync_only() -> None:
                            try:
                                _sync_vd_test_controls()
                            except Exception as exc:
                                _vd_ui_diag("해제 after_busy 코루틴 _sync", exc)

                        try:
                            rt(_sync_only)
                        except Exception as exc:
                            _vd_ui_diag("해제 after_busy page.run_task", exc)

                    try:
                        loop.call_soon_threadsafe(on_main)
                    except Exception as exc_outer:
                        _vd_ui_diag("해제 call_soon_threadsafe", exc_outer)
                        try:
                            on_main()
                        except Exception as exc_inner:
                            _vd_ui_diag("해제 after_busy on_main 폴백", exc_inner)

                try:
                    ok, msg = await _vd_await_thread(
                        lambda: state.vd_test_release_display(
                            after_busy=after_busy
                        ),
                        name="oddments-vd-test-release-ui",
                    )
                except Exception as exc:
                    _vd_ui_diag("해제 async 흐름 전용 스레드 대기", exc)
                    ok, msg = False, f"VD 테스트 해제 중 오류: {exc}"
                try:
                    if pg is not None:
                        show_snack(
                            pg,
                            msg,
                            severity="warning" if not ok else "info",
                        )
                except Exception as exc:
                    _vd_ui_diag("해제 show_snack", exc)
                try:
                    _sync_vd_test_controls()
                except Exception as exc:
                    _vd_ui_diag("해제 완료 후 _sync_vd_test_controls", exc)

            if callable(rt):
                try:
                    rt(_vd_release_flow)
                except Exception as exc:
                    _vd_ui_diag("해제 page.run_task 스케줄", exc)
                return

            def _work_fallback() -> None:
                def after_fb() -> None:
                    try:
                        _sync_vd_test_controls()
                    except Exception as exc:
                        _vd_ui_diag("해제 폴백 after_busy _sync", exc)

                ok, msg = False, "내부 오류"
                try:
                    ok, msg = state.vd_test_release_display(after_busy=after_fb)
                except Exception as exc:
                    _vd_ui_diag("해제 스레드 폴백 vd_test_release_display", exc)
                    ok, msg = False, f"VD 테스트 해제 중 오류: {exc}"
                if pg is not None:
                    try:
                        show_snack(
                            pg,
                            msg,
                            severity="warning" if not ok else "info",
                        )
                    except Exception as exc:
                        _vd_ui_diag("해제 폴백 show_snack", exc)
                try:
                    _sync_vd_test_controls()
                except Exception as exc:
                    _vd_ui_diag("해제 폴백 _sync_vd_test_controls", exc)

            threading.Thread(
                target=_work_fallback,
                daemon=True,
                name="oddments-vd-test-release-ui",
            ).start()

        vd_test_create_btn.on_click = _on_vd_test_create
        vd_test_release_btn.on_click = _on_vd_test_release

        vd_switch = ft.Switch(
            label="가상 디스플레이만 송출 (물리 모니터 미송출)",
            value=bool(hp.use_virtual_display),
            on_change=lambda e: (
                _persist_use_virtual_display(
                    state, bool(getattr(e.control, "value", False))
                ),
                _sync_vd_test_controls(),
            ),
        )

        vd_test_refresh_hooks.append(_sync_vd_test_controls)
        _sync_vd_test_controls()

        audio_vd_field = text_field(
            label="원격 오디오 입력 장치 (이름 일부)",
            value=hp.darwin_audio_input,
            hint="비우면 BlackHole 자동 탐색 · 시스템 소리는 멀티 출력으로 라우팅",
            expand=True,
            on_change=lambda e: _persist_darwin_audio_input(state, e.control.value),
        )
        mac_controls = [
            vd_switch,
            ft.Text(
                "1280×720 테스트용. 호스트가 꺼진 상태에서만 사용하세요.",
                style=label_md(),
                color=T.ON_SURFACE_VARIANT,
            ),
            ft.Row(
                spacing=T.SPACE_MD,
                wrap=False,
                controls=[
                    vd_test_create_btn,
                    vd_test_release_btn,
                ],
            ),
            vd_test_status,
            audio_vd_field,
        ]

    host_status = ft.Text(
        "호스트 실행 중" if state.remote_host_active() else "호스트 중지됨",
        style=body_md(),
        color=T.ON_SURFACE_VARIANT,
    )

    host_start_btn = ft.FilledButton(
        text="호스트 시작",
        icon=ft.Icons.PLAY_ARROW_ROUNDED,
        disabled=state.remote_host_active(),
        style=button_style_click_cursor(
            ft.ButtonStyle(
                bgcolor=T.PRIMARY,
                color=T.ON_PRIMARY,
                padding=ft.padding.symmetric(horizontal=18, vertical=12),
            )
        ),
    )
    host_stop_btn = ft.OutlinedButton(
        text="호스트 중지",
        icon=ft.Icons.STOP_ROUNDED,
        disabled=not state.remote_host_active(),
        style=button_style_click_cursor(
            ft.ButtonStyle(
                color=T.ON_SURFACE,
                side=ft.BorderSide(1, T.OUTLINE),
                padding=ft.padding.symmetric(horizontal=18, vertical=12),
            )
        ),
    )

    def _sync_host_row() -> None:
        running = state.remote_host_active()
        host_start_btn.disabled = running
        host_stop_btn.disabled = not running
        host_status.value = "호스트 실행 중 (WebRTC)" if running else "호스트 중지됨"
        for fn in vd_test_refresh_hooks:
            try:
                fn()
            except Exception:
                pass

    def _on_host_start(_e: ft.ControlEvent) -> None:
        ok, err, acc_hint = state.start_remote_host()
        pg = getattr(state, "page", None)
        if not ok and err and pg is not None:
            show_snack(pg, err, severity="warning")
        elif ok and acc_hint and pg is not None:
            show_snack(pg, acc_hint, severity="warning")
        _sync_host_row()
        if pg is not None:
            try:
                pg.update()
            except Exception:
                pass

    def _on_host_stop(_e: ft.ControlEvent) -> None:
        state.stop_remote_host()
        _sync_host_row()
        pg = getattr(state, "page", None)
        if pg is not None:
            try:
                pg.update()
            except Exception:
                pass

    host_start_btn.on_click = _on_host_start
    host_stop_btn.on_click = _on_host_stop

    host_card = section_card(
        title="호스트",
        icon=ft.Icons.CAST_CONNECTED,
        content=ft.Column(
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    spacing=T.SPACE_MD,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[port_host, monitor_btn, fps_field],
                ),
                h264_hw,
                *mac_controls,
                host_auth,
                ft.Row(
                    spacing=T.SPACE_MD,
                    controls=[host_start_btn, host_stop_btn],
                ),
                host_status,
            ],
        ),
    )

    client_host = text_field(
        label="호스트 주소",
        value=cp.host,
        hint="공인 IP 또는 DNS",
        expand=True,
        on_change=lambda e: _persist_client_host(state, e.control.value),
    )
    client_port = text_field(
        label="포트",
        value=str(cp.port),
        width=140,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: _persist_client_port(state, e.control.value),
    )
    client_auth = text_field(
        label="연결 비밀번호",
        value=cp.auth_token,
        password=True,
        hint="호스트와 동일",
        expand=True,
        on_change=lambda e: _persist_client_auth(state, e.control.value),
    )
    mac_mod_switch = ft.Switch(
        label="macOS 호스트용 수정자 매핑",
        value=bool(cp.mac_modifier_remap),
        tooltip=(
            "켜면 원격 뷰어에서 Ctrl→Control, ⊞ Win→⌥ Option, Alt→⌘ Command 로 보냅니다."
        ),
        on_change=lambda e: _persist_mac_modifier_remap(
            state, bool(getattr(e.control, "value", False))
        ),
    )
    _cl_preset_label_by_id = {k: lab for k, lab in PRESET_LABELS}
    client_res_dd = ft.Dropdown(
        label="연결 시 요청할 가상 해상도 (/offer preset)",
        value=_cl_preset_label_by_id.get(
            (cp.resolution_preset or "").strip(),
            PRESET_LABELS[0][1],
        ),
        expand=True,
        options=[ft.dropdown.Option(lab) for _, lab in PRESET_LABELS],
        on_select=lambda e: _persist_client_resolution_preset(
            state, str(getattr(e.control, "value", "") or "")
        ),
    )

    def _open_viewer(_e: ft.ControlEvent) -> None:
        ok, err = state.save()
        if not ok and err:
            pg = getattr(state, "page", None)
            if pg is not None:
                show_snack(pg, f"설정 저장 실패: {err}", severity="warning")
        launched, msg = launch_remote_viewer_process()
        pg = getattr(state, "page", None)
        if pg is None:
            return
        if launched:
            show_snack(pg, "원격 뷰어 창을 띄웠습니다.", severity="info")
        else:
            show_snack(pg, f"원격 창 실행 실패: {msg}", severity="warning")

    open_btn = ft.FilledButton(
        text="원격 창 열기",
        icon=ft.Icons.OPEN_IN_NEW,
        on_click=_open_viewer,
        style=button_style_click_cursor(
            ft.ButtonStyle(
                bgcolor=T.PRIMARY,
                color=T.ON_PRIMARY,
                padding=ft.padding.symmetric(horizontal=20, vertical=12),
            )
        ),
    )

    client_card = section_card(
        title="클라이언트 (상대 PC 화면 보기)",
        icon=ft.Icons.MONITOR_OUTLINED,
        description="연결 정보를 저장한 뒤 별도 창에서 뷰어를 엽니다.",
        content=ft.Column(
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(spacing=T.SPACE_MD, controls=[client_host]),
                ft.Row(
                    spacing=T.SPACE_MD,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[client_port, client_auth, open_btn],
                ),
                client_res_dd,
                mac_mod_switch,
            ],
        ),
    )

    # 페이지 루트에는 scroll 을 두지 않는다(탭 expand 깨짐 방지).
    # 탭 본문만 ScrollMode.AUTO: 할당된 높이 안에 카드가 들어가면 스크롤 없음,
    # 창이 줄어 넘치면 그 영역 안에서만 스크롤.
    host_body = ft.Container(
        padding=ft.padding.only(top=T.SPACE_SM),
        expand=True,
        alignment=ft.Alignment.TOP_CENTER,
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[host_card],
        ),
    )
    client_body = ft.Container(
        padding=ft.padding.only(top=T.SPACE_SM),
        expand=True,
        alignment=ft.Alignment.TOP_CENTER,
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[client_card],
        ),
    )

    tabs = ft.Tabs(
        length=2,
        expand=True,
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=[
                ft.TabBar(
                    tabs=[
                        ft.Tab(
                            label="호스트",
                            icon=ft.Icons.CAST_CONNECTED,
                        ),
                        ft.Tab(
                            label="클라이언트",
                            icon=ft.Icons.MONITOR_HEART_OUTLINED,
                        ),
                    ],
                ),
                ft.TabBarView(
                    expand=True,
                    controls=[host_body, client_body],
                ),
            ],
        ),
    )

    page_root = ft.Column(
        spacing=T.GUTTER,
        expand=True,
        controls=[
            ft.Text("Remote Desktop", style=headline_sm(), color=T.ON_SURFACE),
            tabs,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    return page_root


def _persist_host_fps(state: AppState, raw: str) -> None:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        v = 30
    state.settings.remote.host.stream_fps = max(5, min(60, v))
    state.save()


def _persist_h264_hw(state: AppState, enabled: bool) -> None:
    state.settings.remote.host.h264_hardware_encode = enabled
    state.save()


def _persist_host_port(state: AppState, raw: str) -> None:
    p = _clamp_port_str(raw, state.settings.remote.host.listen_port)
    state.settings.remote.host.listen_port = p
    state.save()


def _persist_host_auth(state: AppState, raw: str) -> None:
    state.settings.remote.host.auth_token = str(raw)
    state.save()


def _persist_darwin_audio_input(state: AppState, raw: str) -> None:
    state.settings.remote.host.darwin_audio_input = str(raw)
    state.save()


def _persist_client_auth(state: AppState, raw: str) -> None:
    state.settings.remote.client.auth_token = str(raw)
    state.save()


def _persist_client_host(state: AppState, raw: str) -> None:
    state.settings.remote.client.host = str(raw).strip()
    state.save()


def _persist_client_port(state: AppState, raw: str) -> None:
    p = _clamp_port_str(raw, state.settings.remote.client.port)
    state.settings.remote.client.port = p
    state.save()


def _persist_mac_modifier_remap(state: AppState, enabled: bool) -> None:
    state.settings.remote.client.mac_modifier_remap = enabled
    state.save()


def _persist_client_resolution_preset(state: AppState, label: str) -> None:
    for k, lab in PRESET_LABELS:
        if lab == label:
            state.settings.remote.client.resolution_preset = k
            state.save()
            return


__all__ = [
    "build_remote_settings",
    "launch_remote_viewer_process",
]
