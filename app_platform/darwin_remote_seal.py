"""macOS: 가상 디스플레이 원격 시 물리 NSScreen 위에 전체화면 봉인 오버레이.

가상 디스플레이는 ``CGDisplayBounds`` 와 ``NSScreen.frame`` 의 **겹침**으로 판별한다.
(``NSScreenNumber`` 만으로는 CGVirtualDisplay ID 와 불일치하는 경우가 있음.)

메인 스레드 실행은 ``CFRunLoopPerformBlock`` 우선 — Flet 등에서 ``NSOperationQueue`` 만으로는
블록이 실행되지 않는 경우가 있어 ``CFRunLoopWakeUp`` 으로 런루프를 깨운다."""

from __future__ import annotations

import logging
import sys
from typing import Callable

if sys.platform != "darwin":
    raise ImportError("darwin_remote_seal 은 macOS 전용입니다.")

import objc
from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSOperationQueue,
    NSScreen,
    NSTextField,
    NSWindow,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSIntersectionRect, NSObject

_log = logging.getLogger(__name__)

_windows: list[NSWindow] = []
_handler_retainer: list[NSObject] = []


def _window_level() -> int:
    try:
        import Quartz  # type: ignore[import-untyped]

        return int(Quartz.CGShieldingWindowLevel())
    except Exception:
        return 2147483630


def _schedule_on_main(fn: Callable[[], None]) -> None:
    """메인 스레드에서 실행.

    1) libdispatch 메인 큐 — Flet/Flutter 등에서 CFRunLoop 블록이 도지 않을 때 유효.
    2) 메인 CFRunLoop ``CFRunLoopPerformBlock`` + WakeUp
    3) ``NSOperationQueue.mainQueue`` 폴백
    """

    def _wrapped() -> None:
        try:
            fn()
        except Exception:
            _log.exception("darwin_remote_seal: 메인 블록 실패")

    try:
        from app_platform.darwin_dispatch_main import schedule_on_main_dispatch_queue

        if schedule_on_main_dispatch_queue(_wrapped):
            return
    except Exception:
        pass

    try:
        from CoreFoundation import (  # type: ignore[import-untyped]
            CFRunLoopGetMain,
            CFRunLoopPerformBlock,
            CFRunLoopWakeUp,
            kCFRunLoopCommonModes,
        )

        CFRunLoopPerformBlock(CFRunLoopGetMain(), kCFRunLoopCommonModes, _wrapped)
        CFRunLoopWakeUp(CFRunLoopGetMain())
    except Exception:
        try:
            NSOperationQueue.mainQueue().addOperationWithBlock_(_wrapped)
        except Exception:
            _log.exception("darwin_remote_seal: 메인 스케줄 실패")


def _screen_number(scr: object) -> int:
    try:
        d = scr.deviceDescription()
        if d is None:
            return -1
        n = d.get("NSScreenNumber")
        if n is None:
            return -1
        return int(n)
    except Exception:
        return -1


def _virtual_rect_from_cg(vid: int) -> tuple[float, float, float, float] | None:
    try:
        from app_platform.darwin_virtual_display import cg_display_bounds

        return cg_display_bounds(int(vid))
    except Exception:
        return None


def _screen_covers_virtual(
    scr: object,
    vid: int,
    vrect: tuple[float, float, float, float] | None,
) -> bool:
    """이 NSScreen 이 가상 디스플레이와 실질적으로 같은 면인지."""
    if _screen_number(scr) == int(vid):
        return True
    if vrect is None:
        return False
    vx, vy, vw, vh = vrect
    try:
        sf = scr.frame()
        inter = NSIntersectionRect(
            sf,
            NSMakeRect(float(vx), float(vy), float(vw), float(vh)),
        )
        iw = float(inter.size.width)
        ih = float(inter.size.height)
        if iw < 8 or ih < 8:
            return False
        va = max(float(vw) * float(vh), 1.0)
        sa = max(float(sf.size.width) * float(sf.size.height), 1.0)
        overlap = iw * ih
        # 가상 면적·화면 면적 중 작은 쪽 대비 겹침이 크면 같은 디스플레이로 본다.
        return overlap / min(va, sa) >= 0.45
    except Exception:
        return False


class _DisconnectHandler(NSObject):
    """NSButton target — PyObjC 가 인스턴스를 해제하지 않도록 리스트에 보관."""

    _cb: object

    def initWithCallback_(self, callback: object) -> object:
        self = objc.super(_DisconnectHandler, self).init()
        if self is None:
            return None
        self._cb = callback
        return self

    def fire_(self, sender: object) -> None:
        cb = self._cb
        if callable(cb):
            try:
                cb()
            except Exception:
                pass


def _hide_sync() -> None:
    global _windows
    for w in list(_windows):
        try:
            w.orderOut_(None)
            w.close()
        except Exception:
            pass
    _windows.clear()
    _handler_retainer.clear()


def schedule_seal_hide(
    *,
    ui_runner: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    def _go() -> None:
        _hide_sync()

    if ui_runner is not None:
        ui_runner(_go)
    else:
        _schedule_on_main(_go)


def _build_seal_windows(virtual_display_id: int, on_disconnect: Callable[[], None]) -> None:
    _hide_sync()
    vid = int(virtual_display_id)
    if vid <= 0:
        _log.warning("darwin_remote_seal: virtual_display_id=%s 무시", vid)
        return

    try:
        app = NSApplication.sharedApplication()
        app.activateIgnoringOtherOptions_(True)
    except Exception:
        pass

    vrect = _virtual_rect_from_cg(vid)
    try:
        screens = list(NSScreen.screens() or [])
    except Exception as exc:
        _log.error("darwin_remote_seal: NSScreen.screens 실패: %s", exc)
        return

    _log.info(
        "darwin_remote_seal: vid=%s vrect=%s screens=%s",
        vid,
        vrect,
        [(_screen_number(s), str(s.frame())) for s in screens],
    )

    covered = 0
    for scr in screens:
        try:
            if _screen_covers_virtual(scr, vid, vrect):
                _log.info("darwin_remote_seal: 스킵(가상) screen#=%s", _screen_number(scr))
                continue
            frame = scr.frame()
            win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame,
                NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered,
                False,
            )
            win.setOpaque_(False)
            win.setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    0.08, 0.08, 0.12, 0.94
                )
            )
            win.setLevel_(_window_level())
            win.setReleasedWhenClosed_(False)
            try:
                from AppKit import (
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
                try:
                    win.setCollectionBehavior_((1 << 0) | (1 << 3) | (1 << 8))
                except Exception:
                    pass

            cv = win.contentView()
            if cv is None:
                continue
            bw = float(frame.size.width)
            bh = float(frame.size.height)

            try:
                from AppKit import NSTextAlignmentCenter

                _align_center = NSTextAlignmentCenter
            except Exception:
                _align_center = 1

            badge = NSTextField.labelWithString_("원격 중")
            badge.setFont_(NSFont.boldSystemFontOfSize_(min(32.0, max(22.0, bw / 28.0))))
            badge.setTextColor_(NSColor.whiteColor())
            badge.setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.12, 0.08, 1.0)
            )
            try:
                badge.setDrawsBackground_(True)
                badge.setBezeled_(False)
                badge.setBordered_(False)
                badge.setEditable_(False)
                badge.setSelectable_(False)
            except Exception:
                pass
            badge.setAlignment_(_align_center)
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
            title.setAlignment_(_align_center)
            title.setFrame_(NSMakeRect(40, bh * 0.42, bw - 80, bh * 0.38))
            cv.addSubview_(title)

            handler = _DisconnectHandler.alloc().initWithCallback_(on_disconnect)
            _handler_retainer.append(handler)

            btn = NSButton.alloc().initWithFrame_(
                NSMakeRect(max(40.0, (bw - 240.0) * 0.5), 72.0, 240.0, 48.0)
            )
            btn.setTitle_("세션 종료")
            btn.setBezelStyle_(NSBezelStyleRounded)
            btn.setTarget_(handler)
            btn.setAction_("fire:")
            cv.addSubview_(btn)

            win.setIgnoresMouseEvents_(False)
            win.setCanHide_(False)
            win.setHidesOnDeactivate_(False)
            # orderFrontRegardless: 앱이 active 상태가 아니어도 창을 최전면으로 올린다.
            win.orderFrontRegardless()
            _windows.append(win)
            covered += 1
            _log.info(
                "darwin_remote_seal: 봉인 창 추가 screen#=%s frame=%s",
                _screen_number(scr),
                frame,
            )
        except Exception:
            import traceback as _tb
            _log.exception("darwin_remote_seal: 창 생성 실패")
            print(
                f"[darwin_remote_seal] 창 생성 실패: {_tb.format_exc()}",
                flush=True,
            )

    if covered == 0 and screens:
        ms = NSScreen.mainScreen()
        if ms is not None and not _screen_covers_virtual(ms, vid, vrect):
            try:
                frame = ms.frame()
                win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                    frame,
                    NSWindowStyleMaskBorderless,
                    NSBackingStoreBuffered,
                    False,
                )
                win.setOpaque_(False)
                win.setBackgroundColor_(
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.0, 0.0, 0.9)
                )
                win.setLevel_(_window_level())
                win.setReleasedWhenClosed_(False)
                cv = win.contentView()
                if cv is not None:
                    fw = float(frame.size.width)
                    fh = float(frame.size.height)
                    fbadge = NSTextField.labelWithString_("원격 중")
                    fbadge.setFont_(NSFont.boldSystemFontOfSize_(26.0))
                    fbadge.setTextColor_(NSColor.whiteColor())
                    fbadge.setBackgroundColor_(
                        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.55, 0.12, 0.08, 1.0)
                    )
                    try:
                        fbadge.setDrawsBackground_(True)
                        fbadge.setBezeled_(False)
                        fbadge.setBordered_(False)
                        fbadge.setEditable_(False)
                        fbadge.setSelectable_(False)
                    except Exception:
                        pass
                    try:
                        from AppKit import NSTextAlignmentCenter

                        fbadge.setAlignment_(NSTextAlignmentCenter)
                    except Exception:
                        fbadge.setAlignment_(1)
                    fbadge.setFrame_(NSMakeRect(40, fh - 92, fw - 80, 48))
                    cv.addSubview_(fbadge)
                    lab = NSTextField.wrappingLabelWithString_(
                        "원격 봉인(폴백)\n이 물리 화면은 조작할 수 없습니다.\n"
                        "아래 버튼으로 원격 세션을 종료하세요."
                    )
                    lab.setTextColor_(NSColor.whiteColor())
                    lab.setFrame_(NSMakeRect(40, fh * 0.38, fw - 80, fh * 0.35))
                    cv.addSubview_(lab)
                    handler = _DisconnectHandler.alloc().initWithCallback_(on_disconnect)
                    _handler_retainer.append(handler)
                    btn = NSButton.alloc().initWithFrame_(
                        NSMakeRect(max(40, (fw - 240) * 0.5), 56, 240, 48)
                    )
                    btn.setTitle_("세션 종료")
                    btn.setBezelStyle_(NSBezelStyleRounded)
                    btn.setTarget_(handler)
                    btn.setAction_("fire:")
                    cv.addSubview_(btn)
                    win.setCanHide_(False)
                    win.setHidesOnDeactivate_(False)
                    win.orderFrontRegardless()
                    _windows.append(win)
                    _log.warning("darwin_remote_seal: 폴백으로 mainScreen 만 봉인")
            except Exception:
                _log.exception("darwin_remote_seal: mainScreen 폴백 실패")


def schedule_seal_show(
    virtual_display_id: int,
    on_disconnect: Callable[[], None],
    *,
    ui_runner: Callable[[Callable[[], None]], None] | None = None,
) -> None:
    vid = int(virtual_display_id)

    def _go() -> None:
        try:
            from app_platform.darwin_accessibility import accessibility_trusted

            if not accessibility_trusted():
                _log.warning(
                    "darwin_remote_seal: 접근성 미허용 — 물리 화면 봉인이 표시되지 않거나 "
                    "다른 창에 가릴 수 있습니다. 시스템 설정 → 접근성에서 이 앱을 허용하세요."
                )
        except Exception:
            pass
        try:
            _build_seal_windows(vid, on_disconnect)
        except Exception:
            import traceback as _tb
            _log.exception("darwin_remote_seal: _build_seal_windows 실패")
            print(
                f"[darwin_remote_seal] _build_seal_windows 실패 vid={vid}: {_tb.format_exc()}",
                flush=True,
            )

    if ui_runner is not None:
        ui_runner(_go)
    else:
        _schedule_on_main(_go)


__all__ = [
    "schedule_seal_hide",
    "schedule_seal_show",
]
