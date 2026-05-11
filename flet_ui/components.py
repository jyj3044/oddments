"""공용 UI 컴포넌트 모음.

`StreamMasterTheme` 의 디자인 토큰을 따르는 카드·버튼·필드·칩·로그 콘솔
등을 제공한다. 각 페이지에서 import 해서 일관된 룩 앤 필을 유지한다.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

import flet as ft

from .theme import (
    StreamMasterTheme as T,
    body_md,
    button_style_click_cursor,
    label_lg,
    label_md,
    title_lg,
    title_md,
)


def section_card(
    *,
    title: str | None = None,
    icon: str | None = None,
    actions: list[ft.Control] | None = None,
    description: str | None = None,
    content: ft.Control,
    expand: bool = False,
    height: int | None = None,
) -> ft.Container:
    """제목+아이콘+액션이 있는 흰색 둥근 카드.

    `content` 는 카드 본문이며, 외부에서 `Column`, `Row`, `GridView` 등 어떤
    컨트롤이라도 넣을 수 있다.
    """

    header_children: list[ft.Control] = []
    if title:
        header_left: list[ft.Control] = []
        if icon:
            header_left.append(
                ft.Icon(icon, color=T.PRIMARY, size=20)
            )
        header_left.append(ft.Text(title, style=title_md(), color=T.ON_SURFACE))
        header_children.append(
            ft.Row(header_left, spacing=T.SPACE_SM, expand=True)
        )
    if actions:
        header_children.append(
            ft.Row(actions, spacing=T.SPACE_SM, alignment=ft.MainAxisAlignment.END)
        )

    inner: list[ft.Control] = []
    if header_children:
        inner.append(
            ft.Row(
                header_children,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )
        )
    if description:
        inner.append(
            ft.Text(description, style=body_md(), color=T.ON_SURFACE_VARIANT)
        )
    inner.append(content)

    body = ft.Column(
        inner,
        spacing=T.SPACE_MD,
        tight=True,
    )

    return ft.Container(
        content=body,
        padding=T.SPACE_LG,
        bgcolor=T.SURFACE_CONTAINER_LOWEST,
        border=ft.border.all(1, T.OUTLINE_VARIANT),
        border_radius=T.RADIUS_MD,
        shadow=ft.BoxShadow(
            spread_radius=0,
            blur_radius=3,
            offset=ft.Offset(0, 1),
            color="#0d000000",
        ),
        expand=expand,
        height=height,
    )


def primary_button(
    text: str,
    on_click: Callable[[ft.ControlEvent], None] | None = None,
    *,
    icon: str | None = None,
    disabled: bool = False,
    tooltip: str | None = None,
) -> ft.FilledButton:
    return ft.FilledButton(
        text=text,
        icon=icon,
        on_click=on_click,
        disabled=disabled,
        tooltip=tooltip,
        style=button_style_click_cursor(
            ft.ButtonStyle(
                bgcolor=T.PRIMARY,
                color=T.ON_PRIMARY,
                padding=ft.padding.symmetric(horizontal=16, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=T.RADIUS_DEFAULT),
                text_style=label_lg(),
            )
        ),
    )


def outline_button(
    text: str,
    on_click: Callable[[ft.ControlEvent], None] | None = None,
    *,
    icon: str | None = None,
    disabled: bool = False,
    tooltip: str | None = None,
    danger: bool = False,
) -> ft.OutlinedButton:
    fg = T.ERROR if danger else T.ON_SURFACE
    border_color = T.ERROR if danger else T.OUTLINE_VARIANT
    return ft.OutlinedButton(
        text=text,
        icon=icon,
        on_click=on_click,
        disabled=disabled,
        tooltip=tooltip,
        style=button_style_click_cursor(
            ft.ButtonStyle(
                color=fg,
                bgcolor=T.SURFACE_CONTAINER_LOWEST,
                side=ft.BorderSide(1, border_color),
                padding=ft.padding.symmetric(horizontal=16, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=T.RADIUS_DEFAULT),
                text_style=label_lg(),
            )
        ),
    )


def text_field(
    *,
    label: str | None = None,
    value: str = "",
    hint: str | None = None,
    on_change: Callable[[ft.ControlEvent], None] | None = None,
    on_submit: Callable[[ft.ControlEvent], None] | None = None,
    width: int | None = None,
    height: int = 40,
    expand: bool | int = False,
    read_only: bool = False,
    password: bool = False,
    multiline: bool = False,
    text_align: ft.TextAlign = ft.TextAlign.LEFT,
    keyboard_type: ft.KeyboardType = ft.KeyboardType.TEXT,
    suffix_text: str | None = None,
) -> ft.TextField:
    return ft.TextField(
        label=label,
        value=value,
        hint_text=hint,
        on_change=on_change,
        on_submit=on_submit,
        width=width,
        height=None if multiline else height,
        expand=expand,
        read_only=read_only,
        password=password,
        multiline=multiline,
        text_align=text_align,
        keyboard_type=keyboard_type,
        suffix=suffix_text,
        text_style=body_md(),
        label_style=label_md(),
        content_padding=ft.padding.symmetric(horizontal=12, vertical=10),
        border_radius=T.RADIUS_DEFAULT,
        border_color=T.OUTLINE_VARIANT,
        focused_border_color=T.PRIMARY,
        focused_border_width=1.5,
        bgcolor=T.SURFACE_BRIGHT,
        color=T.ON_SURFACE,
        cursor_color=T.PRIMARY,
        filled=True,
        fill_color=T.SURFACE_BRIGHT,
    )


def dropdown(
    *,
    label: str | None = None,
    value: str | None = None,
    options: Iterable[str],
    on_change: Callable[[ft.ControlEvent], None] | None = None,
    width: int | None = None,
    expand: bool | int = False,
) -> ft.Dropdown:
    return ft.Dropdown(
        label=label,
        value=value,
        options=[ft.dropdown.Option(o) for o in options],
        on_select=on_change,
        width=width,
        expand=expand,
        text_style=body_md(),
        label_style=label_md(),
        content_padding=ft.padding.symmetric(horizontal=12, vertical=8),
        border_radius=T.RADIUS_DEFAULT,
        border_color=T.OUTLINE_VARIANT,
        focused_border_color=T.PRIMARY,
        bgcolor=T.SURFACE_BRIGHT,
        color=T.ON_SURFACE,
        height=44,
    )


def field_label(text: str) -> ft.Text:
    return ft.Text(
        text,
        style=label_md(),
        color=T.ON_SURFACE_VARIANT,
        spans=None,
    )


LOG_CONSOLE_BG = "#1e1e1e"
LOG_CONSOLE_FG = "#d4d4d4"
# 알림(키워드 매칭) 줄을 LogConsole 에 밝은 빨강으로 강조하기 위한 sentinel + 색상.
# 큐에 넣은 줄 앞에 ``LOG_CONSOLE_ALERT_PREFIX`` 가 붙어 있으면 LogConsole 이 자동으로
# strip 하고 해당 줄만 ``LOG_CONSOLE_ALERT_COLOR`` 로 칠한다.
LOG_CONSOLE_ALERT_PREFIX = "\x01ALERT\x01"
LOG_CONSOLE_ALERT_COLOR = "#ff5252"
# 어두운 로그 배경 위에서 스크롤바가 잘 보이도록 (ARGB)
_LOG_CONSOLE_SCROLLBAR_THEME = ft.ScrollbarTheme(
    thumb_color="#D9FFFFFF",
    track_color="#33FFFFFF",
    thickness=10,
    radius=6,
)


def _split_alert_line(line: str) -> tuple[str, Optional[str]]:
    """LogConsole 내부용: 알림 sentinel 이 있으면 떼어내고 색상을 함께 돌려준다."""
    if line.startswith(LOG_CONSOLE_ALERT_PREFIX):
        return line[len(LOG_CONSOLE_ALERT_PREFIX):], LOG_CONSOLE_ALERT_COLOR
    return line, None


def stat_chip(text: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, style=label_md(), color=T.ON_SURFACE_VARIANT),
        padding=ft.padding.symmetric(horizontal=8, vertical=4),
        bgcolor=T.SURFACE_CONTAINER,
        border=ft.border.all(1, T.OUTLINE_VARIANT),
        border_radius=T.RADIUS_SM,
    )


class LogConsole(ft.Container):
    """등폭 글꼴·검정 계열 배경의 스크롤 가능한 로그 패널.

    - ``append(line)`` / ``append_many(lines)`` 로 한 줄/여러 줄 추가, 일정 줄 수 초과
      시 앞쪽 자동 트림.
    - ``clear()`` 로 텍스트만 비움.
    - 가로는 부모에서 ``expand=True`` 로 영역을 채운다.
    - 줄 앞에 ``LOG_CONSOLE_ALERT_PREFIX`` sentinel 이 붙은 라인은 자동으로 strip 하고
      해당 줄만 ``LOG_CONSOLE_ALERT_COLOR`` (밝은 빨강) 으로 강조 표시한다.
    """

    def __init__(
        self,
        *,
        height: int = 256,
        # 메모리 보호를 위해 화면에 유지할 최대 줄 수.
        # 영속화된 전체 기록은 ``flet_ui.log_buffers`` 의 파일 로그를 참고할 것.
        max_lines: int = 500,
        autoscroll: bool = True,
        placeholder: str = "",
        expand: bool = True,
    ) -> None:
        # 각 항목: (텍스트, 강조색 또는 None)
        self._lines: list[tuple[str, Optional[str]]] = []
        self._max_lines = max_lines
        self._autoscroll = autoscroll
        self._placeholder = placeholder

        # spans 기반: 줄별로 다른 색상을 적용할 수 있도록 ft.TextSpan 컬렉션을 쓴다.
        # placeholder 는 spans 가 비었을 때 보여줄 단일 span 으로 갱신한다.
        self._text = ft.Text(
            spans=[ft.TextSpan(text=placeholder)] if placeholder else [],
            selectable=True,
            font_family="Consolas, 'Cascadia Mono', 'Courier New', monospace",
            size=12,
            color=LOG_CONSOLE_FG,
        )
        # ListView.auto_scroll 은 자식 컨트롤이 추가·삭제될 때만 동작하고,
        # 단일 Text 의 value 만 바뀌면 스크롤이 내려가지 않는다. Column + scroll_to 로 처리한다.
        # NOTE: ``Column.scroll`` 은 ``ScrollMode`` 만 받는다. 스크롤바 비주얼은 외곽
        # 컨테이너에 부여한 ``_LOG_CONSOLE_SCROLLBAR_THEME`` 에서 처리한다.
        self._scroll_host = ft.Column(
            controls=[self._text],
            spacing=0,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        scroll_body = ft.Container(
            content=self._scroll_host,
            padding=ft.padding.all(12),
            expand=True,
        )

        _log_console_theme = ft.Theme(scrollbar_theme=_LOG_CONSOLE_SCROLLBAR_THEME)
        super().__init__(
            content=scroll_body,
            bgcolor=LOG_CONSOLE_BG,
            border=ft.border.all(1, T.OUTLINE_VARIANT),
            border_radius=T.RADIUS_DEFAULT,
            height=height,
            padding=0,
            expand=expand,
            theme=_log_console_theme,
            dark_theme=_log_console_theme,
        )

    @staticmethod
    def _normalize(line: str) -> str:
        # 큐의 라인은 trailing ``\n`` 으로 끝나는 경우가 많아 spans 결합 시 빈 줄이
        # 생기지 않도록 정리한다.
        return line.rstrip("\r\n")

    def _rebuild_text(self) -> None:
        if not self._lines:
            self._text.spans = (
                [ft.TextSpan(text=self._placeholder)] if self._placeholder else []
            )
            return
        spans: list[ft.TextSpan] = []
        for i, (text, color) in enumerate(self._lines):
            if i > 0:
                spans.append(ft.TextSpan(text="\n"))
            if color is not None:
                spans.append(
                    ft.TextSpan(text=text, style=ft.TextStyle(color=color))
                )
            else:
                spans.append(ft.TextSpan(text=text))
        self._text.spans = spans

    def set_autoscroll(self, on: bool) -> None:
        self._autoscroll = bool(on)

    def append(self, line: str, *, color: Optional[str] = None) -> None:
        text = self._normalize(line)
        if color is None:
            text, color = _split_alert_line(text)
        self._lines.append((text, color))
        if len(self._lines) > self._max_lines:
            self._lines = self._lines[-self._max_lines:]
        self._rebuild_text()

    def append_many(self, lines: Iterable[str]) -> None:
        for raw in lines:
            text = self._normalize(raw)
            text, color = _split_alert_line(text)
            self._lines.append((text, color))
        if len(self._lines) > self._max_lines:
            self._lines = self._lines[-self._max_lines:]
        self._rebuild_text()

    def clear(self) -> None:
        self._lines = []
        self._rebuild_text()

    def flush(self, page: Optional[ft.Page] = None) -> None:
        """내부 ``_text`` 까지 갱신하고, 맨 아래 자동 스크롤이 켜져 있으면 스크롤을 맨 끝으로 보낸다.

        Flet 에서는 레이아웃 반영 후 스크롤을 걸어야 하므로 ``page`` 가 있을 때만
        ``scroll_to`` 를 예약한다. ``scroll_to`` 는 Flet 0.85 에서 동기 메서드라
        await 하면 ``TypeError`` 가 나는 변종이 있어, awaitable 인 경우에만 await 한다.
        """
        try:
            self._text.update()
        except Exception:
            pass
        try:
            self.update()
        except Exception:
            pass
        if page is None or not self._autoscroll:
            return

        async def _scroll_bottom() -> None:
            try:
                # 텍스트 갱신과 레이아웃 반영을 한 프레임 양보해 둔다.
                import asyncio
                try:
                    await asyncio.sleep(0)
                except Exception:
                    pass
                ret = self._scroll_host.scroll_to(offset=-1, duration=0)
                # Flet 변종에 따라 sync(None) / coroutine 모두 가능.
                if ret is not None and hasattr(ret, "__await__"):
                    await ret
            except Exception:
                pass

        try:
            page.run_task(_scroll_bottom)
        except Exception:
            # run_task 자체가 안되는 환경에서는 sync 시도 폴백.
            try:
                self._scroll_host.scroll_to(offset=-1, duration=0)
            except Exception:
                pass


def make_vertical_resize_handle(
    target: ft.Control,
    *,
    initial_height: int,
    min_height: int = 140,
    max_height: int = 900,
    on_height_change: Optional[Callable[[int], None]] = None,
) -> ft.GestureDetector:
    """``height`` 가 있는 임의의 컨트롤을 세로 드래그로 늘리고 줄이는 핸들.

    핸들 자체는 가운데 알약 모양 막대(60×4px)가 보이는 14px 높이 영역이며,
    ``RESIZE_ROW`` 커서로 호버 시 시각적 피드백을 준다.

    ``LogConsole`` 처럼 ``ft.Container`` 를 상속한 컨트롤이든, 일반
    ``ft.Container`` 든 ``height`` 속성과 ``update()`` 만 있으면 그대로 동작한다.

    ``on_height_change`` 가 주어지면 드래그로 높이가 실제로 변경될 때마다
    정수 픽셀 값으로 호출된다. 호출자는 이 콜백에서 메모리 상태나 설정을
    갱신할 수 있다(파일 I/O 는 종료 시점에 일괄 저장하는 패턴 권장).
    """
    state = {"h": float(initial_height)}

    def _on_drag(e: ft.DragUpdateEvent) -> None:
        # Flet 0.85: vertical drag 는 primary_delta(y축 단위) 사용.
        # 호환을 위해 local_delta.y, global_delta.y 도 fallback.
        delta = 0.0
        try:
            pd = getattr(e, "primary_delta", None)
            if pd is not None:
                delta = float(pd)
            else:
                ld = getattr(e, "local_delta", None)
                if ld is not None and hasattr(ld, "y"):
                    delta = float(ld.y)
                else:
                    gd = getattr(e, "global_delta", None)
                    if gd is not None and hasattr(gd, "y"):
                        delta = float(gd.y)
        except Exception:
            delta = 0.0
        if delta == 0.0:
            return
        new_h = max(float(min_height), min(float(max_height), state["h"] + delta))
        if int(new_h) == int(state["h"]):
            return
        state["h"] = new_h
        target.height = int(new_h)  # type: ignore[attr-defined]
        try:
            target.update()
        except Exception:
            pass
        if on_height_change is not None:
            try:
                on_height_change(int(new_h))
            except Exception:
                pass

    handle_pill = ft.Container(
        width=60,
        height=4,
        bgcolor=T.OUTLINE_VARIANT,
        border_radius=T.RADIUS_FULL,
    )
    handle_strip = ft.Container(
        height=14,
        alignment=ft.alignment.center,
        content=handle_pill,
        margin=ft.margin.only(top=4, bottom=2),
    )
    return ft.GestureDetector(
        mouse_cursor=ft.MouseCursor.RESIZE_ROW,
        on_vertical_drag_update=_on_drag,
        content=handle_strip,
    )


# 하위 호환: ``LogConsole`` 전용 별칭. 새 코드는 ``make_vertical_resize_handle`` 사용.
_make_log_resize_handle = make_vertical_resize_handle


def stream_log_panel(
    *,
    title: str,
    placeholder: str,
    actions: list[ft.Control],
    height: int = 240,
    icon: str | None = None,
    description: str | None = None,
    controls_above_console: list[ft.Control] | None = None,
    resizable: bool = True,
    min_height: int = 140,
    max_height: int = 900,
) -> tuple[LogConsole, ft.Container]:
    """OCR / 아두이노 / 웹 공통: 검정 로그 콘솔 + ``section_card`` 한 묶음.

    ``resizable=True`` (기본) 면 콘솔 아래에 세로 드래그 리사이즈 핸들을
    붙여 사용자가 로그 높이를 마우스로 늘릴 수 있다.
    """
    log_console = LogConsole(height=height, placeholder=placeholder, expand=False)
    body_children: list[ft.Control] = []
    if controls_above_console:
        body_children.extend(controls_above_console)
    body_children.append(log_console)
    if resizable:
        body_children.append(
            _make_log_resize_handle(
                log_console,
                initial_height=height,
                min_height=min_height,
                max_height=max_height,
            )
        )
    body = ft.Column(
        controls=body_children,
        spacing=T.SPACE_MD,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        expand=True,
    )
    card = section_card(
        title=title,
        icon=icon,
        actions=actions,
        description=description,
        content=body,
        expand=True,
    )
    return log_console, card


STATUS_OFFLINE = "offline"
STATUS_IDLE = "idle"
STATUS_ONLINE = "online"
STATUS_ERROR = "error"

# (도트색, 도트 글로우 색, 텍스트 강조 여부) 4-state 매핑
_STATUS_DOT_COLOR = {
    STATUS_OFFLINE: ("#9aa0a6", "#00000000", False),    # 회색
    STATUS_IDLE: ("#f59e0b", "#55f59e0b", False),       # 주황
    STATUS_ONLINE: ("#22c55e", "#5522c55e", True),      # 초록 + 강조
    STATUS_ERROR: ("#ef4444", "#55ef4444", False),      # 빨강
}


def status_dot(*, status: str = STATUS_OFFLINE, label: str) -> ft.Row:
    """4-state 도트 + 라벨. ``status`` 는 offline/idle/online/error."""
    dot_color, glow, accent = _STATUS_DOT_COLOR.get(status, _STATUS_DOT_COLOR[STATUS_OFFLINE])
    return ft.Row(
        controls=[
            ft.Container(
                width=10,
                height=10,
                bgcolor=dot_color,
                border_radius=T.RADIUS_FULL,
                shadow=ft.BoxShadow(
                    blur_radius=8,
                    color=glow,
                    offset=ft.Offset(0, 0),
                ),
            ),
            ft.Text(
                label,
                style=label_md(),
                color=T.ON_SURFACE if accent else T.ON_SURFACE_VARIANT,
                weight=ft.FontWeight.BOLD if accent else None,
            ),
        ],
        spacing=T.SPACE_SM,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def page_header(title: str) -> ft.Text:
    return ft.Text(title, style=title_lg(), color=T.ON_SURFACE)


# severity → (bgcolor, log_level_or_None) 매핑. 새 종류 추가는 여기에서만.
_SNACK_SEVERITY_PRESETS: dict[str, tuple[str, str | None]] = {
    "info": (T.PRIMARY, None),
    "warning": (T.WARNING, "WARN"),
    "error": (T.ERROR, "ERROR"),
}


def show_snack(
    page: ft.Page,
    message: str,
    *,
    bgcolor: str | None = None,
    error: bool = False,
    severity: str | None = None,
    duration_sec: int = 5,
) -> None:
    """Flet 0.85 호환 스낵바.

    Material 표준에 맞춰 항상 화면 하단(``SnackBarBehavior.FIXED``)에 표시되며,
    오른쪽 끝에 X 닫기 아이콘이 붙는다.

    severity:
      ``info`` (기본, 파랑) / ``warning`` (주황) / ``error`` (빨강).
      구버전 호환을 위해 ``error=True`` 도 받아 ``severity='error'`` 로 매핑한다.
      ``warning`` / ``error`` 일 때는 ``logs/app-*.log`` 에도 한 줄 남긴다.

    ``bgcolor`` 가 명시되면 severity 가 정한 색을 덮어쓴다.
    """
    sev = severity or ("error" if error else "info")
    sev_bg, log_level = _SNACK_SEVERITY_PRESETS.get(
        sev, _SNACK_SEVERITY_PRESETS["info"]
    )
    if log_level is not None:
        try:
            from .log_buffers import log_app_event

            log_app_event(log_level, message)
        except Exception:
            pass
    snack = ft.SnackBar(
        content=ft.Text(message, color=T.ON_PRIMARY),
        bgcolor=bgcolor or sev_bg,
        behavior=ft.SnackBarBehavior.FIXED,
        show_close_icon=True,
        close_icon_color=T.ON_PRIMARY,
        duration=ft.Duration(seconds=max(1, int(duration_sec))),
    )
    show = getattr(page, "show_dialog", None)
    if callable(show):
        try:
            show(snack)
            return
        except Exception:
            pass
    open_attr = getattr(page, "open", None)
    if callable(open_attr):
        try:
            open_attr(snack)
            return
        except Exception:
            pass
    try:
        page.snack_bar = snack  # type: ignore[attr-defined]
        snack.open = True
        page.update()
    except Exception:
        pass


def close_active_dialog(page: ft.Page) -> None:
    """현재 떠 있는 다이얼로그/스낵을 안전하게 닫는다.

    Flet 0.85 는 ``pop_dialog`` / ``close`` / 레거시 ``dialog.open=False`` 등
    여러 닫기 경로가 공존한다. 사용 가능한 것부터 시도한다.
    """
    pop = getattr(page, "pop_dialog", None)
    if callable(pop):
        try:
            pop()
            return
        except Exception:
            pass
    close = getattr(page, "close", None)
    if callable(close):
        try:
            dlg = getattr(page, "dialog", None)
            if dlg is not None:
                close(dlg)
                return
        except Exception:
            pass
    try:
        dlg = getattr(page, "dialog", None)
        if dlg is not None:
            dlg.open = False
            page.update()
    except Exception:
        pass


def ensure_file_picker(page: ft.Page) -> ft.FilePicker:
    """페이지에 ``ft.FilePicker`` 가 어태치되어 있는지 확인하고 없으면 추가한다.

    Flet 0.85 에서 FilePicker 는 일반 컨트롤이 아니라 ``Service`` 라
    ``page.overlay`` 에 추가하면 *unknown control: FilePicker* 오류가 난다.
    반드시 ``page.services`` 컬렉션에 등록해야 한다. 구버전 호환을 위해
    ``page.services`` 가 없으면 ``page.overlay`` 폴백한다.

    한 페이지당 하나의 picker 만 유지하기 위해 ``page._file_picker`` 어트리뷰트로
    캐싱한다.
    """
    fp: ft.FilePicker | None = getattr(page, "_file_picker", None)  # type: ignore[assignment]
    if fp is not None and getattr(fp, "page", None) is not None:
        return fp

    fp = ft.FilePicker()

    services = getattr(page, "services", None)
    attached = False
    if services is not None:
        try:
            services.append(fp)
            attached = True
        except Exception:
            pass
    if not attached:
        # 구버전 폴백: 일부 환경에서 services 가 없거나 append 가 안 되면
        # overlay 로 시도(예전 Flet 버전 호환).
        try:
            page.overlay.append(fp)
            attached = True
        except Exception:
            pass

    if attached:
        try:
            page.update()
        except Exception:
            pass
    try:
        page._file_picker = fp  # type: ignore[attr-defined]
    except Exception:
        pass
    return fp


def set_clipboard(page: ft.Page, text: str) -> bool:
    """Flet 0.85+ ``page.clipboard.set(text)`` (async 코루틴) + 구버전 호환.

    동기 핸들러에서 호출되므로 ``page.run_task`` 로 스케줄한다. 스케줄 자체가
    성공하면 True, 어떤 경로로도 호출이 안 되면 False. 비동기 호출 안에서
    실제 OS 클립보드 쓰기가 실패하면 ``logs/app-*.log`` 에 ERROR 한 줄이 남는다.
    """
    # Flet 0.85+: ``page.clipboard`` 가 Clipboard 인스턴스. ``set`` 은 코루틴.
    cb = getattr(page, "clipboard", None)
    if cb is not None and hasattr(cb, "set"):
        run_task = getattr(page, "run_task", None)
        if callable(run_task):
            async def _do() -> None:
                try:
                    await cb.set(text)
                except Exception as exc:  # noqa: BLE001
                    try:
                        import traceback as _tb
                        from .log_buffers import log_app_event

                        log_app_event(
                            "ERROR",
                            f"clipboard.set failed: {exc}",
                            detail=_tb.format_exc(),
                        )
                    except Exception:
                        pass

            try:
                run_task(_do)
                return True
            except Exception:
                pass

    # 레거시 폴백 (구버전 Flet): ``page.set_clipboard`` (sync) / ``set_clipboard_async``.
    setter = getattr(page, "set_clipboard", None)
    if callable(setter):
        try:
            setter(text)
            return True
        except Exception:
            pass
    setter_async = getattr(page, "set_clipboard_async", None)
    if callable(setter_async):
        try:
            page.run_task(setter_async, text)
            return True
        except Exception:
            pass
    return False


def schedule_clipboard_read(
    page: ft.Page, callback: Callable[[Optional[str]], None]
) -> bool:
    """OS 클립보드 텍스트를 비동기로 읽고 ``callback(text)`` 로 넘긴다.

    ``text`` 가 None 이면 읽기 실패 또는 미지원.
    """
    cb = getattr(page, "clipboard", None)
    if cb is not None and hasattr(cb, "get"):
        run_task = getattr(page, "run_task", None)
        if callable(run_task):

            async def _do() -> None:
                try:
                    t = await cb.get()
                    callback(t if isinstance(t, str) else None)
                except Exception:
                    callback(None)

            try:
                run_task(_do)
                return True
            except Exception:
                pass
    callback(None)
    return False


# 매우 긴 에러 메시지로 다이얼로그가 폭발하지 않게 자르는 컷오프 (트레이스백 포함).
_ALERT_DIALOG_MAX_CHARS = 4000

# 공용 알림 다이얼로그 크기 가이드 (모두 px).
ALERT_DIALOG_WIDTH = 520
ALERT_DIALOG_MIN_HEIGHT = 120
ALERT_DIALOG_MAX_HEIGHT = 480
ALERT_DETAIL_BOX_HEIGHT = 200

# severity → (icon, color) 매핑. 새 종류가 필요하면 여기에만 추가.
_ALERT_SEVERITY_PRESETS: dict[str, tuple[str, str]] = {
    "info": (ft.Icons.INFO_OUTLINE, T.PRIMARY),
    "warning": (ft.Icons.WARNING_AMBER_ROUNDED, T.WARNING),
    "error": (ft.Icons.ERROR_OUTLINE, T.ERROR),
}


def show_alert(
    page: ft.Page,
    message: str,
    *,
    title: str = "알림",
    detail: str | None = None,
    severity: str = "info",
    confirm_label: str = "확인",
    on_close: Callable[[], None] | None = None,
) -> None:
    """공용 알림 모달 다이얼로그 컴포넌트.

    페이지 전체를 덮는 Flet 기본 에러 화면 대신, 화면 중앙에 적당한 크기의
    카드를 띄운다. 본문 영역은 컨텐츠 길이에 따라 자연스럽게 늘어나되,
    ``ALERT_DIALOG_MIN_HEIGHT``/``ALERT_DIALOG_MAX_HEIGHT`` 사이로 묶여
    화면을 꽉 채우는 일이 없다. ``detail`` 이 주어지면 그 부분만 스크롤
    가능한 고정 높이 박스로 들어가 메인 메시지는 항상 한눈에 보인다.

    severity:
      ``info`` (기본) / ``warning`` / ``error``. 아이콘과 색만 달라진다.

    이 함수는 어떤 단계에서도 예외를 호출자에게 다시 던지지 않는다.
    """
    try:
        msg = (message or "").strip() or "(빈 메시지)"
        if len(msg) > _ALERT_DIALOG_MAX_CHARS:
            msg = msg[:_ALERT_DIALOG_MAX_CHARS] + "\n... (이하 생략)"

        icon, icon_color = _ALERT_SEVERITY_PRESETS.get(
            severity, _ALERT_SEVERITY_PRESETS["info"]
        )

        body_children: list[ft.Control] = [
            ft.Text(msg, selectable=True, color=T.ON_SURFACE),
        ]
        if detail:
            d = detail.strip()
            if len(d) > _ALERT_DIALOG_MAX_CHARS:
                d = d[:_ALERT_DIALOG_MAX_CHARS] + "\n... (이하 생략)"
            # detail 만 자체적으로 스크롤되는 고정 높이 박스. 외곽 Column 은
            # ``tight=True`` 라 메인 메시지 + 이 박스 합산 높이로 자연스럽게 사이즈가
            # 잡혀, 다이얼로그 전체가 화면을 덮는 일이 없다.
            body_children.append(
                ft.Container(
                    bgcolor=T.SURFACE_CONTAINER_LOW,
                    padding=ft.padding.all(8),
                    border_radius=T.RADIUS_SM,
                    height=ALERT_DETAIL_BOX_HEIGHT,
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                d,
                                selectable=True,
                                color=T.ON_SURFACE_VARIANT,
                                style=label_md(),
                            ),
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        tight=True,
                    ),
                )
            )

        body = ft.Container(
            width=ALERT_DIALOG_WIDTH,
            # ``height`` 를 명시하지 않아 컨텐츠에 맞게 사이즈가 잡히지만,
            # MIN/MAX 가이드는 ``Column`` 에서 ``tight=True`` 로 보장된다.
            content=ft.Column(
                controls=body_children,
                spacing=8,
                tight=True,
            ),
        )

        def _on_confirm(_e: ft.ControlEvent) -> None:
            close_active_dialog(page)
            if on_close is not None:
                try:
                    on_close()
                except Exception:
                    pass

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(icon, color=icon_color, size=22),
                    ft.Text(title, color=T.ON_SURFACE),
                ],
            ),
            content=body,
            actions=[
                ft.TextButton(
                    confirm_label,
                    on_click=_on_confirm,
                    style=button_style_click_cursor(ft.ButtonStyle()),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

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
            # 정말 마지막 수단: stderr 에라도 남긴다.
            try:
                import sys as _sys

                _sys.stderr.write(f"[show_alert] {title}: {msg}\n")
            except Exception:
                pass
    except Exception:
        # 알림창 자체가 예외를 던지면 절대 다시 던지지 않는다.
        pass


def show_error_dialog(
    page: ft.Page,
    message: str,
    *,
    title: str = "오류",
    detail: str | None = None,
) -> None:
    """``show_alert(severity='error')`` 의 얇은 래퍼 + 자동 파일 로깅.

    Flet 기본 동작은 핸들러에서 예외가 새어나가면 화면 전체를 빨간 스택트레이스
    창으로 덮는다. 본 헬퍼는 ``page.on_error``/``sys.excepthook``/스레드 예외를
    한 군데로 모아 사용자가 닫을 수 있는 알림으로 보여주기 위한 진입점이다.

    동시에 ``logs/app-*.log`` 에도 즉시 기록해 사용자가 다이얼로그를 닫더라도
    원인을 사후에 추적할 수 있게 한다.
    """
    try:
        from .log_buffers import log_app_event

        log_app_event("ERROR", f"{title}: {message}", detail=detail)
    except Exception:
        pass
    show_alert(page, message, title=title, detail=detail, severity="error")


__all__ = [
    "section_card",
    "primary_button",
    "outline_button",
    "text_field",
    "dropdown",
    "field_label",
    "stat_chip",
    "LogConsole",
    "LOG_CONSOLE_BG",
    "LOG_CONSOLE_FG",
    "LOG_CONSOLE_ALERT_PREFIX",
    "LOG_CONSOLE_ALERT_COLOR",
    "stream_log_panel",
    "make_vertical_resize_handle",
    "status_dot",
    "page_header",
    "show_snack",
    "show_alert",
    "show_error_dialog",
    "ensure_file_picker",
    "ALERT_DIALOG_WIDTH",
    "ALERT_DIALOG_MIN_HEIGHT",
    "ALERT_DIALOG_MAX_HEIGHT",
    "ALERT_DETAIL_BOX_HEIGHT",
    "close_active_dialog",
    "set_clipboard",
    "schedule_clipboard_read",
    "STATUS_OFFLINE",
    "STATUS_IDLE",
    "STATUS_ONLINE",
    "STATUS_ERROR",
]
