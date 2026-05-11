"""macOS 접근성(Assistive) 권한 — 원격 호스트의 pynput 마우스·키보드 주입에 필요."""

from __future__ import annotations

import subprocess
import sys
from typing import Optional, Tuple

_HINT = (
    "시스템 설정 → 개인 정보 보호 및 보안 → 접근성에서 "
    "이 앱(또는 터미널/Cursor/실행 중인 Python)을 허용해야 "
    "원격에서 마우스·키보드를 제어할 수 있습니다."
)


def open_accessibility_settings_pane() -> None:
    """접근성 설정 화면을 연다(프롬프트 API 실패·미설치 시 폴백)."""
    if sys.platform != "darwin":
        return
    urls = (
        # macOS Ventura 이후
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
        # 이전 버전
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    )
    for url in urls:
        try:
            subprocess.run(
                ["/usr/bin/open", url],
                check=False,
                capture_output=True,
            )
            return
        except Exception:
            continue


def accessibility_trusted_after_prompt() -> Tuple[bool, Optional[str]]:
    """접근성 신뢰 여부를 확인하고, 미허용 시에만 시스템 권한 요청 대화를 시도한다.

    이미 허용된 경우 ``AXIsProcessTrusted`` 만 호출하고 프롬프트 API 는 쓰지 않는다.

    Returns:
        ``(True, None)`` — 이미 허용됨.
        ``(False, hint)`` — 아직 미허용. ``hint`` 를 스낵 등에 표시.
    """
    if sys.platform != "darwin":
        return True, None

    try:
        import HIServices  # type: ignore[import-untyped]
        from Foundation import NSDictionary  # type: ignore[import-untyped]
    except ImportError:
        open_accessibility_settings_pane()
        return False, _HINT

    # 이미 신뢰됨 → 시스템 권한 창을 띄우지 않음
    try:
        apt = getattr(HIServices, "AXIsProcessTrusted", None)
        if apt is not None and bool(apt()):
            return True, None
    except Exception:
        pass

    key = getattr(HIServices, "kAXTrustedCheckOptionPrompt", None)
    if key is None:
        open_accessibility_settings_pane()
        return False, _HINT

    opts = NSDictionary.dictionaryWithDictionary_({key: True})
    fn = getattr(HIServices, "AXIsProcessTrustedWithOptions_", None)
    if fn is None:
        open_accessibility_settings_pane()
        return False, _HINT

    try:
        trusted = bool(fn(opts))
    except Exception:
        open_accessibility_settings_pane()
        return False, _HINT

    if trusted:
        return True, None
    return False, _HINT
