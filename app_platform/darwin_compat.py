"""macOS 버전·기능 가드(원격 호스트 등)."""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Tuple

# 원격 호스트: 최소 macOS 26.4 (Tahoe 계열) — 런타임에서만 검사.
_REMOTE_HOST_MIN = (26, 4)


def get_macos_version_tuple() -> Tuple[int, ...]:
    """(26, 4, 1) 형태. 실패 시 (0,)."""
    if sys.platform != "darwin":
        return (0,)
    try:
        out = subprocess.check_output(
            ["/usr/bin/sw_vers", "-productVersion"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        s = out.decode("utf-8", errors="ignore").strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return (0,)
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", s)
    if not m:
        return (0,)
    parts = [int(m.group(1))]
    if m.group(2) is not None:
        parts.append(int(m.group(2)))
    if m.group(3) is not None:
        parts.append(int(m.group(3)))
    return tuple(parts)


def _tuple_ge(a: Tuple[int, ...], b: Tuple[int, ...]) -> bool:
    """버전 튜플 비교 (짧은 쪽은 0 패딩)."""
    n = max(len(a), len(b))
    for i in range(n):
        ai = a[i] if i < len(a) else 0
        bi = b[i] if i < len(b) else 0
        if ai > bi:
            return True
        if ai < bi:
            return False
    return True


def remote_host_macos_version_ok() -> tuple[bool, str]:
    """원격 호스트 실행 가능 여부 (맥 전용)."""
    if sys.platform != "darwin":
        return True, ""
    ver = get_macos_version_tuple()
    if not ver or ver[0] == 0:
        return False, "macOS 버전을 확인할 수 없습니다."
    need = _REMOTE_HOST_MIN
    if not _tuple_ge(ver, need):
        return (
            False,
            f"이 빌드는 macOS {need[0]}.{need[1]}+(Tahoe) 가 필요합니다. "
            f"현재: {'.'.join(str(x) for x in ver)}.",
        )
    return True, ""


__all__ = [
    "get_macos_version_tuple",
    "remote_host_macos_version_ok",
]
