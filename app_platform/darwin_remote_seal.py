"""macOS: 가상 디스플레이 원격 시 물리 NSScreen 위에 전체화면 봉인 오버레이.

- 가상 디스플레이(CG ID)에 해당하는 NSScreen 은 제외한다.
- 마우스는 오버레이가 흡수한다(물리 화면에서 로컬 조작 최소화).
- '세션 끊기' 는 등록된 콜백으로 WebRTC 피어를 닫는 용도.

NSWindow 는 반드시 메인 스레드에서 생성·조작한다."""

from __future__ import annotations

import sys
from typing import Callable

if sys.platform != "darwin":
    raise ImportError("darwin_remote_seal 은 macOS 전용입니다.")

import objc
from AppKit import (
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
from Foundation import NSObject


# 메뉴바 위로 덮기 위해 높은 레벨(대략 스크린세이버 급).
_SEAL_WINDOW_LEVEL = 1000

_windows: list[NSWindow] = []
_handler_retainer: list[NSObject] = []


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


def schedule_seal_hide() -> None:
    """메인 큐에서 물리 화면 오버레이를 모두 내린다."""

    def _run() -> None:
        _hide_sync()

    try:
        NSOperationQueue.mainQueue().addOperationWithBlock_(_run)
    except Exception:
        _run()


def schedule_seal_show(
    virtual_display_id: int,
    on_disconnect: Callable[[], None],
) -> None:
    """물리 NSScreen 전체에 반투명 봉인 + 세션 끊기 버튼. 가상 디스플레이 화면은 제외."""

    vid = int(virtual_display_id)

    def _run() -> None:
        _hide_sync()
        if vid <= 0:
            return
        try:
            screens = list(NSScreen.screens() or [])
        except Exception:
            return
        for scr in screens:
            try:
                if _screen_number(scr) == vid:
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
                        0.08, 0.08, 0.12, 0.92
                    )
                )
                win.setLevel_(_SEAL_WINDOW_LEVEL)
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

                title = NSTextField.wrappingLabelWithString_(
                    "원격 제어 중입니다.\n이 화면에서는 조작할 수 없습니다.\n"
                    "작업은 가상 디스플레이(원격 클라이언트)에서 진행하세요."
                )
                title.setFont_(NSFont.systemFontOfSize_(22.0))
                title.setTextColor_(NSColor.whiteColor())
                title.setBackgroundColor_(NSColor.clearColor())
                try:
                    from AppKit import NSTextAlignmentCenter

                    title.setAlignment_(NSTextAlignmentCenter)
                except Exception:
                    title.setAlignment_(1)
                title.setFrame_(NSMakeRect(40, bh * 0.55, bw - 80, bh * 0.35))
                cv.addSubview_(title)

                handler = _DisconnectHandler.alloc().initWithCallback_(on_disconnect)
                _handler_retainer.append(handler)

                btn = NSButton.alloc().initWithFrame_(
                    NSMakeRect(max(40, (bw - 220) * 0.5), 80, 220, 44)
                )
                btn.setTitle_("세션 끊기")
                btn.setBezelStyle_(NSBezelStyleRounded)
                btn.setTarget_(handler)
                btn.setAction_("fire:")
                cv.addSubview_(btn)

                win.makeKeyAndOrderFront_(None)
                _windows.append(win)
            except Exception:
                continue

    try:
        NSOperationQueue.mainQueue().addOperationWithBlock_(_run)
    except Exception:
        _run()


__all__ = [
    "schedule_seal_hide",
    "schedule_seal_show",
]
