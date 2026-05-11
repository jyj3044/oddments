"""원격 제어 설정 — 호스트/클라이언트는 한 페이지 안에서 탭으로 전환."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import flet as ft

from streaming.remote_presets import PRESET_LABELS

from ..components import (
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
    label_lg,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _clamp_port_str(raw: str, default: int) -> int:
    try:
        p = int(str(raw).strip())
    except (TypeError, ValueError):
        p = default
    return max(1, min(65535, p))


def _clamp_dim_str(raw: str) -> int:
    """0 이거나 빈 값은 네이티브; 그 외는 양수 상한만 둔다."""
    s = str(raw).strip()
    if not s:
        return 0
    try:
        v = int(s)
    except ValueError:
        return 0
    if v <= 0:
        return 0
    return min(v, 16384)


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

    port_host = text_field(
        label="수신 포트",
        value=str(hp.listen_port),
        expand=True,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: _persist_host_port(state, e.control.value),
    )
    mon_field = text_field(
        label="모니터 인덱스",
        value=str(hp.monitor_index),
        hint="mss 1부터",
        expand=True,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: _persist_monitor(state, e.control.value),
    )
    fps_field = text_field(
        label="FPS",
        value=str(hp.stream_fps),
        expand=True,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: _persist_host_fps(state, e.control.value),
    )
    h264_hw = ft.Checkbox(
        label="H.264 GPU 인코딩 (NVENC / AMF / VideoToolbox)",
        value=bool(hp.h264_hardware_encode),
        on_change=lambda e: _persist_h264_hw(state, bool(e.control.value)),
    )
    w_field = text_field(
        label="송출 가로(px)",
        value="" if hp.capture_width <= 0 else str(hp.capture_width),
        hint="비우거나 0 = 원격 PC 해상도",
        expand=True,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: _persist_host_dim_w(state, e.control.value),
    )
    h_field = text_field(
        label="송출 세로(px)",
        value="" if hp.capture_height <= 0 else str(hp.capture_height),
        hint="비우거나 0 = 원격 PC 해상도",
        expand=True,
        keyboard_type=ft.KeyboardType.NUMBER,
        on_change=lambda e: _persist_host_dim_h(state, e.control.value),
    )
    stun_field = text_field(
        label="STUN (줄마다 하나)",
        value=hp.stun_urls,
        multiline=True,
        height=120,
        on_change=lambda e: _persist_host_stun(state, e.control.value),
    )
    turn_uri = text_field(
        label="TURN URI (선택)",
        value=hp.turn_uri,
        hint="turn:… 또는 turns:…",
        on_change=lambda e: _persist_turn(state, uri=e.control.value),
    )
    turn_user = text_field(
        label="TURN 사용자",
        value=hp.turn_username,
        on_change=lambda e: _persist_turn(state, username=e.control.value),
    )
    turn_pass = text_field(
        label="TURN 비밀번호",
        value=hp.turn_password,
        password=True,
        on_change=lambda e: _persist_turn(state, password=e.control.value),
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

    mac_controls: list[ft.Control] = []
    if sys.platform == "darwin":
        vd_switch = ft.Switch(
            label="가상 디스플레이만 송출 (물리 모니터 미송출)",
            value=bool(hp.use_virtual_display),
            on_change=lambda e: _persist_use_virtual_display(
                state, bool(getattr(e.control, "value", False))
            ),
        )
        _preset_label_by_id = {k: lab for k, lab in PRESET_LABELS}
        _preset_row_labels = [lab for _, lab in PRESET_LABELS]
        _cur_preset_label = _preset_label_by_id.get(
            hp.resolution_preset, PRESET_LABELS[0][1]
        )
        preset_dd = ft.Dropdown(
            label="해상도 프리셋",
            value=_cur_preset_label,
            width=420,
            options=[ft.dropdown.Option(lab) for lab in _preset_row_labels],
            on_select=lambda e: _persist_resolution_preset_label(
                state, str(getattr(e.control, "value", "") or "")
            ),
        )
        audio_vd_field = text_field(
            label="원격 오디오 입력 장치 (이름 일부)",
            value=hp.darwin_audio_input,
            hint="비우면 BlackHole 자동 탐색 · 시스템 소리는 멀티 출력으로 라우팅",
            expand=True,
            on_change=lambda e: _persist_darwin_audio_input(state, e.control.value),
        )
        mac_controls = [
            vd_switch,
            ft.Row(
                spacing=T.SPACE_MD,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[preset_dd],
            ),
            audio_vd_field,
            ft.Text(
                "가상 디스플레이는 CGVirtualDisplay 비공개 API를 사용합니다. "
                "오디오는 BlackHole 등 가상 입력으로 캡처합니다.",
                style=body_md(),
                color=T.ON_SURFACE_VARIANT,
            ),
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
        title="호스트 (이 PC에서 화면 공유)",
        icon=ft.Icons.CAST_CONNECTED,
        description="WebRTC(aiortc)로 영상을 송출하고 DataChannel 로 마우스·키보드를 받습니다. "
        "외부망은 포트포워딩 또는 STUN/TURN 설정이 필요합니다.",
        content=ft.Column(
            spacing=T.SPACE_MD,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    spacing=T.SPACE_MD,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[port_host, mon_field, fps_field],
                ),
                h264_hw,
                *mac_controls,
                ft.Text(
                    "PyAV 에 해당 인코더가 포함되어 있어야 합니다. 없거나 실패 시 libx264 로 송출합니다.",
                    style=body_md(),
                    color=T.ON_SURFACE_VARIANT,
                ),
                ft.Row(
                    spacing=T.SPACE_MD,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[w_field, h_field],
                ),
                stun_field,
                turn_uri,
                ft.Row(
                    spacing=T.SPACE_MD,
                    controls=[turn_user, turn_pass],
                ),
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
                mac_mod_switch,
                ft.Text(
                    "Flet 단일 프로세스는 다중 OS 창을 지원하지 않아, 뷰어는 두 번째 프로세스로 띄웁니다.",
                    style=body_md(),
                    color=T.ON_SURFACE_VARIANT,
                ),
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
            ft.Text(
                "맥 호스트는 가상 디스플레이 모드에서 물리 모니터 대신 지정 해상도만 송출합니다. "
                "그 외 OS 는 단일 모니터 인덱스 기준입니다.",
                style=label_lg(),
                color=T.ON_SURFACE_VARIANT,
            ),
            tabs,
        ],
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    return page_root


def _persist_monitor(state: AppState, raw: str) -> None:
    try:
        v = max(1, int(str(raw).strip()))
    except (TypeError, ValueError):
        v = 1
    state.settings.remote.host.monitor_index = v
    state.save()


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


def _persist_host_dim_w(state: AppState, raw: str) -> None:
    state.settings.remote.host.capture_width = _clamp_dim_str(raw)
    state.save()


def _persist_host_dim_h(state: AppState, raw: str) -> None:
    state.settings.remote.host.capture_height = _clamp_dim_str(raw)
    state.save()


def _persist_host_stun(state: AppState, raw: str) -> None:
    state.settings.remote.host.stun_urls = str(raw)
    state.save()


def _persist_turn(
    state: AppState,
    *,
    uri: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> None:
    h = state.settings.remote.host
    if uri is not None:
        h.turn_uri = str(uri)
    if username is not None:
        h.turn_username = str(username)
    if password is not None:
        h.turn_password = str(password)
    state.save()


def _persist_host_auth(state: AppState, raw: str) -> None:
    state.settings.remote.host.auth_token = str(raw)
    state.save()


def _persist_use_virtual_display(state: AppState, enabled: bool) -> None:
    state.settings.remote.host.use_virtual_display = enabled
    state.save()


def _persist_resolution_preset_label(state: AppState, label: str) -> None:
    for k, lab in PRESET_LABELS:
        if lab == label:
            state.settings.remote.host.resolution_preset = k
            state.save()
            return


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


__all__ = [
    "build_remote_settings",
    "launch_remote_viewer_process",
]
