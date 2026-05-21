"""설정에 저장하는 파일 경로 — 실행 파일 옆 ``assets/`` 기준 상대 경로."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_install_dir() -> Path:
    """앱이 설치·실행되는 루트 (동결 exe 디렉터리 또는 프로젝트 루트)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def user_assets_dir() -> Path:
    """사용자 템플릿·알림음 등을 두는 ``assets`` (Flet 번들 ``_MEIPASS`` 와 별도)."""
    return app_install_dir() / "assets"


def to_settings_storage_path(path: str | Path | None) -> str:
    """설정 JSON 에 넣을 경로. ``assets`` 아래 파일은 상대 경로로 축약한다."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    if not candidate.is_absolute():
        return candidate.as_posix().lstrip("./")
    try:
        rel = candidate.resolve().relative_to(user_assets_dir().resolve())
    except ValueError:
        return str(candidate.resolve())
    return rel.as_posix()


def resolve_settings_path(path: str | Path | None) -> str:
    """저장된 경로를 실제 파일 I/O 용 절대 경로로 변환한다."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.is_absolute():
        return str(candidate.resolve())
    return str((user_assets_dir() / candidate).resolve())


def resolve_settings_path_if_exists(path: str | Path | None) -> str | None:
    resolved = resolve_settings_path(path)
    if not resolved or not os.path.isfile(resolved):
        return None
    return resolved
