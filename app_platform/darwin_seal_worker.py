#!/usr/bin/env python3
"""봉인 창 워커 프로세스 — 단독 실행 전용.

호출: python darwin_seal_worker.py <virtual_display_id>

stdin  "quit\\n"       → 창 닫고 종료
stdout "disconnect\\n" → '세션 종료' 버튼 누름
"""
from __future__ import annotations

import ctypes
import sys
import threading

if sys.platform != "darwin":
    raise SystemExit("macOS 전용")

from AppKit import (  # type: ignore[import-untyped]
    NSApplication,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSScreen,
    NSTextField,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSIntersectionRect, NSObject  # type: ignore[import-untyped]
import objc  # type: ignore[import-untyped]

_vid: int = int(sys.argv[1]) if len(sys.argv) > 1 else 0
_windows: list[NSWindow] = []
_handlers: list[NSObject] = []


# ── CoreGraphics display bounds (ctypes, no PyObjC 의존) ─────────────────────

def _cg_display_bounds(display_id: int) -> tuple[float, float, float, float] | None:
    try:
        _cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )

        class _CGPoint(ctypes.Structure):
            _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

        class _CGSize(ctypes.Structure):
            _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

        class _CGRect(ctypes.Structure):
            _fields_ = [("origin", _CGPoint), ("size", _CGSize)]

        _cg.CGDisplayBounds.restype = _CGRect
        _cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        r = _cg.CGDisplayBounds(ctypes.c_uint32(display_id))
        return (r.origin.x, r.origin.y, r.size.width, r.size.height)
    except Exception:
        return None


def _shielding_level() -> int:
    try:
        import Quartz  # type: ignore[import-untyped]

        return int(Quartz.CGShieldingWindowLevel())
    except Exception:
        return 2_147_483_630


def _screen_number(scr: object) -> int:
    try:
        d = scr.deviceDescription()
        n = d.get("NSScreenNumber") if d is not None else None
        return int(n) if n is not None else -1
    except Exception:
        return -1


def _screen_covers_virtual(scr: object) -> bool:
    """이 NSScreen 이 가상 디스플레이 영역과 실질적으로 겹치는지."""
    if _screen_number(scr) == _vid:
        return True
    vrect = _cg_display_bounds(_vid)
    if vrect is None:
        return False
    vx, vy, vw, vh = vrect
    try:
        sf = scr.frame()
        inter = NSIntersectionRect(
            sf, NSMakeRect(float(vx), float(vy), float(vw), float(vh))
        )
        iw, ih = float(inter.size.width), float(inter.size.height)
        if iw < 8 or ih < 8:
            return False
        va = max(float(vw) * float(vh), 1.0)
        sa = max(float(sf.size.width) * float(sf.size.height), 1.0)
        return (iw * ih) / min(va, sa) >= 0.45
    except Exception:
        return False


# ── disconnect handler ────────────────────────────────────────────────────────

class _DisconnectHandler(NSObject):
    def fire_(self, _sender: object) -> None:
        print("disconnect", flush=True)
        NSApplication.sharedApplication().terminate_(None)


# ── window factory ────────────────────────────────────────────────────────────

def _make_seal_window(frame: object, level: int) -> None:
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    win.setOpaque_(False)
    win.setBackgroundColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.08, 0.08, 0.12, 0.95)
    )
    win.setLevel_(level)
    win.setReleasedWhenClosed_(False)
    win.setIgnoresMouseEvents_(False)
    win.setCanHide_(False)
    win.setHidesOnDeactivate_(False)
    try:
        from AppKit import (  # type: ignore[import-untyped]
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary,
        )

        win.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
    except Exception:
        win.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 8))

    cv = win.contentView()
    bw = float(frame.size.width)
    bh = float(frame.size.height)

    try:
        from AppKit import NSTextAlignmentCenter  # type: ignore[import-untyped]

        _center = NSTextAlignmentCenter
    except Exception:
        _center = 1

    if cv is not None:
        badge = NSTextField.labelWithString_("원격 중")
        badge.setFont_(
            NSFont.boldSystemFontOfSize_(min(32.0, max(22.0, bw / 28.0)))
        )
        badge.setTextColor_(NSColor.whiteColor())
        badge.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.12, 0.08, 1.0)
        )
        badge.setDrawsBackground_(True)
        badge.setBezeled_(False)
        badge.setBordered_(False)
        badge.setEditable_(False)
        badge.setSelectable_(False)
        badge.setAlignment_(_center)
        badge_h = min(56.0, max(44.0, bh * 0.08))
        badge.setFrame_(NSMakeRect(40.0, bh - badge_h - 36.0, bw - 80.0, badge_h))
        cv.addSubview_(badge)

        title = NSTextField.wrappingLabelWithString_(
            "가상 디스플레이로 원격 호스트가 연결되었습니다.\n"
            "이 물리 화면에서는 마우스·키보드로 조작할 수 없습니다.\n"
            "작업은 원격 클라이언트(가상 디스플레이)에서 진행하세요."
        )
        title.setFont_(NSFont.systemFontOfSize_(20.0))
        title.setTextColor_(NSColor.whiteColor())
        title.setBackgroundColor_(NSColor.clearColor())
        title.setAlignment_(_center)
        title.setFrame_(NSMakeRect(40, bh * 0.42, bw - 80, bh * 0.38))
        cv.addSubview_(title)

        handler = _DisconnectHandler.alloc().init()
        _handlers.append(handler)

        btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(max(40.0, (bw - 240.0) * 0.5), 72.0, 240.0, 48.0)
        )
        btn.setTitle_("세션 종료")
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(handler)
        btn.setAction_("fire:")
        cv.addSubview_(btn)

    win.makeKeyWindow()
    win.orderFrontRegardless()
    _windows.append(win)


def _build_seal_windows() -> None:
    app = NSApplication.sharedApplication()
    app.activateIgnoringOtherOptions_(True)

    level = _shielding_level()
    print(f"build_seal: vid={_vid} level={level}", flush=True)

    vd_rect = _cg_display_bounds(_vid)
    print(f"build_seal: vd_rect={vd_rect}", flush=True)

    try:
        screens = list(NSScreen.screens() or [])
    except Exception as e:
        print(f"build_seal: NSScreen.screens 실패 {e}", flush=True)
        screens = []

    print(f"build_seal: screen_count={len(screens)}", flush=True)
    covered = 0
    for scr in screens:
        sn = _screen_number(scr)
        sf = scr.frame()
        is_vd = _screen_covers_virtual(scr)
        print(f"build_seal: screen#={sn} frame={sf} is_vd={is_vd}", flush=True)
        if is_vd:
            continue
        _make_seal_window(sf, level)
        covered += 1

    if covered == 0:
        ms = NSScreen.mainScreen()
        if ms is not None and not _screen_covers_virtual(ms):
            print("build_seal: fallback mainScreen", flush=True)
            _make_seal_window(ms.frame(), level)

    print(f"build_seal: done covered={covered} windows={len(_windows)}", flush=True)


# ── NSApplicationDidChangeScreenParametersNotification 처리 ──────────────────
# 디스플레이 재배치(주 디스플레이 전환 등) 후 봉인 창을 재생성한다.

class _ScreenChangeObserver(NSObject):
    def screenParametersChanged_(self, _notification: object) -> None:
        print("screen_change: NSApplicationDidChangeScreenParametersNotification", flush=True)
        for w in list(_windows):
            try:
                w.orderOut_(None)
                w.close()
            except Exception:
                pass
        _windows.clear()
        _handlers.clear()
        _build_seal_windows()


_screen_observer: _ScreenChangeObserver | None = None


class _Launcher(NSObject):
    def launch_(self, _: object) -> None:
        print("launcher: start", flush=True)
        _register_screen_change_observer()
        _build_seal_windows()
        print("launcher: done", flush=True)


def _register_screen_change_observer() -> None:
    global _screen_observer
    try:
        from Foundation import NSNotificationCenter  # type: ignore[import-untyped]
        from AppKit import NSApplicationDidChangeScreenParametersNotification  # type: ignore[import-untyped]

        _screen_observer = _ScreenChangeObserver.alloc().init()
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            _screen_observer,
            "screenParametersChanged:",
            NSApplicationDidChangeScreenParametersNotification,
            None,
        )
    except Exception:
        pass


# ── stdin 감시 스레드 ─────────────────────────────────────────────────────────

def _watch_stdin() -> None:
    """stdin 이 닫히거나 'quit' 수신 시 앱 종료."""
    try:
        for line in sys.stdin:
            if line.strip() in ("quit", "exit", "stop"):
                break
    except Exception:
        pass
    try:
        NSApplication.sharedApplication().terminate_(None)
    except Exception:
        import os as _os
        _os._exit(0)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _app = NSApplication.sharedApplication()
    _app.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory (Dock 숨김)

    threading.Thread(target=_watch_stdin, daemon=True).start()

    _launcher = _Launcher.alloc().init()
    _launcher.performSelector_withObject_afterDelay_("launch:", None, 0.05)

    _app.run()
