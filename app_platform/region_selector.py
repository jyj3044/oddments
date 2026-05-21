"""화면 위에서 드래그로 사각형 영역을 선택하는 작은 오버레이."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ScreenRect:
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class ScreenPoint:
    x: int
    y: int


@dataclass(frozen=True)
class _OverlaySpec:
    window: ScreenRect
    active: ScreenRect


def _virtual_screen_bounds() -> ScreenRect:
    if sys.platform == "win32":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
            if width > 0 and height > 0:
                return ScreenRect(left, top, width, height)
        except Exception:
            pass

    import tkinter as tk

    root = tk.Tk()
    try:
        width = int(root.winfo_screenwidth())
        height = int(root.winfo_screenheight())
    finally:
        root.destroy()
    return ScreenRect(0, 0, max(1, width), max(1, height))


def _monitor_bounds() -> list[ScreenRect]:
    """실제 모니터별 화면 좌표 목록. 실패하면 가상 화면 1개로 폴백."""
    try:
        import mss

        out: list[ScreenRect] = []
        with mss.mss() as sct:
            for i, mon in enumerate(sct.monitors):
                if i == 0:
                    continue
                left = int(mon.get("left", 0))
                top = int(mon.get("top", 0))
                width = int(mon.get("width", 0))
                height = int(mon.get("height", 0))
                if width > 0 and height > 0:
                    out.append(ScreenRect(left, top, width, height))
        if out:
            return out
    except Exception:
        pass
    return [_virtual_screen_bounds()]


def _overlay_bounds() -> list[ScreenRect]:
    return _monitor_bounds()


def _intersect_rect(a: ScreenRect, b: ScreenRect) -> ScreenRect | None:
    x1 = max(a.left, b.left)
    y1 = max(a.top, b.top)
    x2 = min(a.left + a.width, b.left + b.width)
    y2 = min(a.top + a.height, b.top + b.height)
    if x2 <= x1 or y2 <= y1:
        return None
    return ScreenRect(x1, y1, x2 - x1, y2 - y1)


def _overlay_specs(bounds: Iterable[ScreenRect] | None) -> list[_OverlaySpec]:
    monitors = _monitor_bounds()
    if bounds is None:
        return [_OverlaySpec(window=m, active=m) for m in monitors]
    if sys.platform == "win32":
        return [
            _OverlaySpec(window=target, active=target)
            for target in bounds
            if target.width > 0 and target.height > 0
        ]
    specs: list[_OverlaySpec] = []
    for target in bounds:
        if target.width <= 0 or target.height <= 0:
            continue
        matched = False
        for mon in monitors:
            clipped = _intersect_rect(target, mon)
            if clipped is not None:
                specs.append(_OverlaySpec(window=mon, active=clipped))
                matched = True
        if not matched:
            specs.append(_OverlaySpec(window=target, active=target))
    return specs


def _selector_log(message: str, *, detail: str | None = None, level: str = "INFO") -> None:
    try:
        from flet_ui.crash_diagnostics import record_exception

        record_exception("screen_region_selector", message, detail=detail, level=level)
    except Exception:
        pass


def _ensure_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def _format_bounds(bounds: Iterable[ScreenRect]) -> str:
    return ", ".join(str(b) for b in bounds)


def _format_overlay_modes(specs: Iterable[_OverlaySpec]) -> str:
    return ", ".join(
        "full-window" if _same_rect(spec.window, spec.active) else "masked-active"
        for spec in specs
    )


def _point_monitor_index(point: ScreenPoint, bounds: list[ScreenRect]) -> int | None:
    for idx, b in enumerate(bounds, start=1):
        if b.left <= point.x < b.left + b.width and b.top <= point.y < b.top + b.height:
            return idx
    return None


def _rect_monitor_indexes(rect: ScreenRect, bounds: list[ScreenRect]) -> list[int]:
    out: list[int] = []
    rx2 = rect.left + rect.width
    ry2 = rect.top + rect.height
    for idx, b in enumerate(bounds, start=1):
        bx2 = b.left + b.width
        by2 = b.top + b.height
        if max(rect.left, b.left) < min(rx2, bx2) and max(rect.top, b.top) < min(ry2, by2):
            out.append(idx)
    return out


def _event_screen_xy(event, canvas) -> tuple[int, int]:
    try:
        return int(canvas.winfo_rootx()) + int(event.x), int(canvas.winfo_rooty()) + int(event.y)
    except Exception:
        return int(event.x_root), int(event.y_root)


def _contains_point(rect: ScreenRect, x: int, y: int) -> bool:
    return rect.left <= x < rect.left + rect.width and rect.top <= y < rect.top + rect.height


def _same_rect(a: ScreenRect, b: ScreenRect) -> bool:
    return (
        int(a.left) == int(b.left)
        and int(a.top) == int(b.top)
        and int(a.width) == int(b.width)
        and int(a.height) == int(b.height)
    )


def _place_overlay_window(root, bounds: ScreenRect) -> None:
    """Tk geometry의 음수 좌표 해석을 피해서 가상 화면 위치에 창을 배치."""
    root.geometry(f"{bounds.width}x{bounds.height}+0+0")
    if sys.platform != "win32":
        root.geometry(f"{bounds.width}x{bounds.height}+{bounds.left}+{bounds.top}")
        return
    try:
        import ctypes

        root.update_idletasks()
        root.update()
        raw_hwnd = int(root.winfo_id())
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_bool
        user32.MoveWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_bool,
        ]
        user32.MoveWindow.restype = ctypes.c_bool
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
        user32.GetWindowRect.restype = ctypes.c_bool
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        kernel32.GetLastError.restype = ctypes.c_ulong
        parent_hwnd = int(user32.GetParent(ctypes.c_void_p(raw_hwnd)) or 0)
        hwnd = int(user32.GetAncestor(ctypes.c_void_p(raw_hwnd), 2) or raw_hwnd)  # GA_ROOT
        root_width = int(root.winfo_width())
        root_height = int(root.winfo_height())
        user32.ShowWindow(ctypes.c_void_p(hwnd), 5)  # SW_SHOW
        user32.MoveWindow(
            ctypes.c_void_p(hwnd),
            int(bounds.left),
            int(bounds.top),
            int(bounds.width),
            int(bounds.height),
            True,
        )
        ok = user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            ctypes.c_void_p(-1),
            int(bounds.left),
            int(bounds.top),
            int(bounds.width),
            int(bounds.height),
            0x0040,  # SWP_SHOWWINDOW
        )
        if not ok:
            raise OSError(f"SetWindowPos failed, last_error={int(kernel32.GetLastError())}")

        def _rect_text(window_hwnd: int) -> str:
            if window_hwnd <= 0:
                return "none"
            actual = RECT()
            if not user32.GetWindowRect(ctypes.c_void_p(window_hwnd), ctypes.byref(actual)):
                return "unknown"
            return (
                f"({int(actual.left)},{int(actual.top)},"
                f"{int(actual.right - actual.left)}x{int(actual.bottom - actual.top)})"
            )

        _selector_log(
            "overlay window placed",
            detail=(
                f"bounds={bounds}\n"
                f"raw_hwnd={raw_hwnd}\n"
                f"parent_hwnd={parent_hwnd}\n"
                f"root_hwnd={hwnd}\n"
                f"tk_size_before={root_width}x{root_height}\n"
                f"tk_root_after=({root.winfo_rootx()},{root.winfo_rooty()})\n"
                f"raw_win32_rect_after={_rect_text(raw_hwnd)}\n"
                f"parent_win32_rect_after={_rect_text(parent_hwnd)}\n"
                f"root_win32_rect_after={_rect_text(hwnd)}"
            ),
            level="INFO",
        )
    except Exception as exc:
        _selector_log(
            "overlay window placement failed",
            detail=f"bounds={bounds}\nerror={type(exc).__name__}: {exc}",
            level="WARN",
        )
        if bounds.left >= 0 and bounds.top >= 0:
            root.geometry(f"{bounds.width}x{bounds.height}+{bounds.left}+{bounds.top}")
        else:
            root.geometry(f"{bounds.width}x{bounds.height}+0+0")


def select_screen_rect(bounds: Iterable[ScreenRect] | None = None) -> ScreenRect | None:
    """사용자가 화면 위에서 드래그한 사각형을 화면 절대 좌표로 반환."""
    import tkinter as tk

    _ensure_dpi_awareness()
    root = tk.Tk()
    root.withdraw()

    result: dict[str, ScreenRect | None] = {"rect": None}
    start: dict[str, int] = {"x": 0, "y": 0}
    selecting: dict[str, bool] = {"value": False}
    active_canvas: dict[str, tk.Canvas | None] = {"canvas": None}
    rect_id: dict[str, int | None] = {"id": None}
    windows: list[tk.Toplevel] = []

    def finish_none(_event=None) -> None:
        result["rect"] = None
        root.quit()

    def on_down(event, canvas=None) -> None:
        canvas = canvas or event.widget
        active_canvas["canvas"] = canvas
        sx, sy = _event_screen_xy(event, canvas)
        active = getattr(canvas, "_oddments_active_rect", None)
        if isinstance(active, ScreenRect) and not _contains_point(active, sx, sy):
            selecting["value"] = False
            return
        selecting["value"] = True
        start["x"] = sx
        start["y"] = sy
        if rect_id["id"] is not None:
            try:
                canvas.delete(rect_id["id"])
            except Exception:
                pass
        rect_id["id"] = canvas.create_rectangle(
            int(event.x),
            int(event.y),
            int(event.x),
            int(event.y),
            outline="#ff5252",
            width=3,
        )

    def on_move(event, canvas=None) -> None:
        rid = rect_id["id"]
        canvas = canvas or active_canvas["canvas"]
        if rid is None or canvas is None or not selecting["value"]:
            return
        try:
            wx = int(canvas.winfo_rootx())
            wy = int(canvas.winfo_rooty())
            canvas.coords(
                rid,
                start["x"] - wx,
                start["y"] - wy,
                _event_screen_xy(event, canvas)[0] - wx,
                _event_screen_xy(event, canvas)[1] - wy,
            )
        except Exception:
            pass

    def on_up(event) -> None:
        canvas = active_canvas["canvas"] or event.widget
        if not selecting["value"]:
            return
        ex, ey = _event_screen_xy(event, canvas)
        active = getattr(canvas, "_oddments_active_rect", None)
        if isinstance(active, ScreenRect):
            ex = max(active.left, min(active.left + active.width, ex))
            ey = max(active.top, min(active.top + active.height, ey))
        x1 = min(start["x"], ex)
        y1 = min(start["y"], ey)
        x2 = max(start["x"], ex)
        y2 = max(start["y"], ey)
        w = x2 - x1
        h = y2 - y1
        if w >= 3 and h >= 3:
            result["rect"] = ScreenRect(x1, y1, w, h)
            _selector_log(
                "rect selection finished",
                detail=(
                    f"rect={result['rect']}\n"
                    f"intersecting_monitors={_rect_monitor_indexes(result['rect'], monitor_bounds)}"
                ),
                level="INFO",
            )
        root.quit()

    monitor_bounds = _monitor_bounds()
    specs = _overlay_specs(bounds)
    if not specs:
        specs = _overlay_specs(None)
    _selector_log(
        "rect selection started",
        detail=(
            f"monitors={_format_bounds(monitor_bounds)}\n"
            f"overlay_bounds={_format_bounds([s.window for s in specs])}\n"
            f"active_bounds={_format_bounds([s.active for s in specs])}\n"
            f"overlay_modes={_format_overlay_modes(specs)}"
        ),
        level="INFO",
    )
    root.bind("<Escape>", finish_none)
    for spec in specs:
        bounds = spec.window
        active = spec.active
        full_window_overlay = _same_rect(bounds, active)
        win = tk.Toplevel(root)
        win.withdraw()
        windows.append(win)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        transparent_color = "#ff00ff"
        try:
            win.attributes("-alpha", 0.28)
        except tk.TclError:
            pass
        if full_window_overlay:
            win.configure(bg="black")
        else:
            try:
                win.attributes("-transparentcolor", transparent_color)
            except tk.TclError:
                pass
            win.configure(bg=transparent_color)
        _place_overlay_window(win, bounds)
        canvas_bg = "black" if full_window_overlay else transparent_color
        canvas = tk.Canvas(win, bg=canvas_bg, highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        ax1 = active.left - bounds.left
        ay1 = active.top - bounds.top
        ax2 = ax1 + active.width
        ay2 = ay1 + active.height
        if not full_window_overlay:
            canvas.create_rectangle(ax1, ay1, ax2, ay2, fill="black", outline="")
        canvas._oddments_active_rect = active  # type: ignore[attr-defined]
        canvas.bind("<ButtonPress-1>", on_down)
        canvas.bind("<B1-Motion>", on_move)
        canvas.bind("<ButtonRelease-1>", on_up)
        win.bind("<ButtonPress-1>", lambda e, c=canvas: on_down(e, c))
        win.bind("<B1-Motion>", lambda e, c=canvas: on_move(e, c))
        win.bind("<ButtonRelease-1>", on_up)
        win.bind("<Escape>", finish_none)
        win._oddments_overlay_bounds = bounds  # type: ignore[attr-defined]
        _place_overlay_window(win, bounds)

    try:
        for win in windows:
            try:
                overlay_bounds = getattr(win, "_oddments_overlay_bounds", None)
                if isinstance(overlay_bounds, ScreenRect):
                    _place_overlay_window(win, overlay_bounds)
                win.lift()
                win.focus_force()
                win.grab_set_global()
            except Exception:
                pass
        root.mainloop()
    finally:
        for win in windows:
            try:
                win.destroy()
            except Exception:
                pass
        root.destroy()

    return result["rect"]


def select_screen_point(bounds: Iterable[ScreenRect] | None = None) -> ScreenPoint | None:
    """사용자가 화면 위에서 클릭한 한 점을 화면 절대 좌표로 반환."""
    import tkinter as tk

    _ensure_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    result: dict[str, ScreenPoint | None] = {"point": None}
    windows: list[tk.Toplevel] = []

    def finish_none(_event=None) -> None:
        result["point"] = None
        root.quit()

    def on_click(event, canvas=None) -> None:
        canvas = canvas or event.widget
        point = ScreenPoint(*_event_screen_xy(event, canvas))
        active = getattr(canvas, "_oddments_active_rect", None)
        if isinstance(active, ScreenRect) and not _contains_point(active, point.x, point.y):
            return
        result["point"] = point
        _selector_log(
            "point selection finished",
            detail=(
                f"point={result['point']}\n"
                f"monitor={_point_monitor_index(result['point'], monitor_bounds)}"
            ),
            level="INFO",
        )
        root.quit()

    monitor_bounds = _monitor_bounds()
    specs = _overlay_specs(bounds)
    if not specs:
        specs = _overlay_specs(None)
    _selector_log(
        "point selection started",
        detail=(
            f"monitors={_format_bounds(monitor_bounds)}\n"
            f"overlay_bounds={_format_bounds([s.window for s in specs])}\n"
            f"active_bounds={_format_bounds([s.active for s in specs])}\n"
            f"overlay_modes={_format_overlay_modes(specs)}"
        ),
        level="INFO",
    )
    root.bind("<Escape>", finish_none)
    for spec in specs:
        bounds = spec.window
        active = spec.active
        full_window_overlay = _same_rect(bounds, active)
        win = tk.Toplevel(root)
        win.withdraw()
        windows.append(win)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        transparent_color = "#ff00ff"
        try:
            win.attributes("-alpha", 0.18)
        except tk.TclError:
            pass
        if full_window_overlay:
            win.configure(bg="black")
        else:
            try:
                win.attributes("-transparentcolor", transparent_color)
            except tk.TclError:
                pass
            win.configure(bg=transparent_color)
        _place_overlay_window(win, bounds)
        canvas_bg = "black" if full_window_overlay else transparent_color
        canvas = tk.Canvas(win, bg=canvas_bg, highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)
        ax1 = active.left - bounds.left
        ay1 = active.top - bounds.top
        ax2 = ax1 + active.width
        ay2 = ay1 + active.height
        if not full_window_overlay:
            canvas.create_rectangle(ax1, ay1, ax2, ay2, fill="black", outline="")
        canvas._oddments_active_rect = active  # type: ignore[attr-defined]
        canvas.bind("<ButtonRelease-1>", on_click)
        win.bind("<ButtonRelease-1>", lambda e, c=canvas: on_click(e, c))
        win.bind("<Escape>", finish_none)
        win._oddments_overlay_bounds = bounds  # type: ignore[attr-defined]
        _place_overlay_window(win, bounds)

    try:
        for win in windows:
            try:
                overlay_bounds = getattr(win, "_oddments_overlay_bounds", None)
                if isinstance(overlay_bounds, ScreenRect):
                    _place_overlay_window(win, overlay_bounds)
                win.lift()
                win.focus_force()
                win.grab_set_global()
            except Exception:
                pass
        root.mainloop()
    finally:
        for win in windows:
            try:
                win.destroy()
            except Exception:
                pass
        root.destroy()

    return result["point"]
