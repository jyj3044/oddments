"""Flet 기반 Material Design 3 UI 패키지.

백엔드(`capture`, `detection`, `arduino`, `streaming`)는 그대로 재사용한다.

Flet 0.85 에서 사라진 헬퍼(padding.symmetric/only/all, border.all/only,
margin.only/symmetric, alignment.center)를 import 시점에 보강한다.
"""

from __future__ import annotations

import flet as ft

# ─── 호환 헬퍼: padding ─────────────────────────────────


def _padding_symmetric(*, horizontal: float = 0, vertical: float = 0) -> ft.Padding:
    return ft.Padding(left=horizontal, right=horizontal, top=vertical, bottom=vertical)


def _padding_only(*, top: float = 0, bottom: float = 0, left: float = 0, right: float = 0) -> ft.Padding:
    return ft.Padding(top=top, bottom=bottom, left=left, right=right)


def _padding_all(value: float) -> ft.Padding:
    return ft.Padding(top=value, bottom=value, left=value, right=value)


# ─── 호환 헬퍼: margin ──────────────────────────────────


def _margin_symmetric(*, horizontal: float = 0, vertical: float = 0) -> ft.Margin:
    return ft.Margin(left=horizontal, right=horizontal, top=vertical, bottom=vertical)


def _margin_only(*, top: float = 0, bottom: float = 0, left: float = 0, right: float = 0) -> ft.Margin:
    return ft.Margin(top=top, bottom=bottom, left=left, right=right)


def _margin_all(value: float) -> ft.Margin:
    return ft.Margin(top=value, bottom=value, left=value, right=value)


# ─── 호환 헬퍼: border ──────────────────────────────────


def _border_all(width: float, color: str) -> ft.Border:
    side = ft.BorderSide(width, color)
    return ft.Border(top=side, bottom=side, left=side, right=side)


def _border_only(
    *,
    top: ft.BorderSide | None = None,
    bottom: ft.BorderSide | None = None,
    left: ft.BorderSide | None = None,
    right: ft.BorderSide | None = None,
) -> ft.Border:
    return ft.Border(top=top, bottom=bottom, left=left, right=right)


# ─── 모듈에 부착(없는 경우에만) ─────────────────────────

if not hasattr(ft.padding, "symmetric"):
    ft.padding.symmetric = _padding_symmetric  # type: ignore[attr-defined]
if not hasattr(ft.padding, "only"):
    ft.padding.only = _padding_only  # type: ignore[attr-defined]
if not hasattr(ft.padding, "all"):
    ft.padding.all = _padding_all  # type: ignore[attr-defined]

if not hasattr(ft.margin, "symmetric"):
    ft.margin.symmetric = _margin_symmetric  # type: ignore[attr-defined]
if not hasattr(ft.margin, "only"):
    ft.margin.only = _margin_only  # type: ignore[attr-defined]
if not hasattr(ft.margin, "all"):
    ft.margin.all = _margin_all  # type: ignore[attr-defined]

if not hasattr(ft.border, "all"):
    ft.border.all = _border_all  # type: ignore[attr-defined]
if not hasattr(ft.border, "only"):
    ft.border.only = _border_only  # type: ignore[attr-defined]

if not hasattr(ft.alignment, "center"):
    ft.alignment.center = ft.Alignment(0, 0)  # type: ignore[attr-defined]
if not hasattr(ft.alignment, "top_left"):
    ft.alignment.top_left = ft.Alignment(-1, -1)  # type: ignore[attr-defined]
if not hasattr(ft.alignment, "top_right"):
    ft.alignment.top_right = ft.Alignment(1, -1)  # type: ignore[attr-defined]
if not hasattr(ft.alignment, "bottom_left"):
    ft.alignment.bottom_left = ft.Alignment(-1, 1)  # type: ignore[attr-defined]
if not hasattr(ft.alignment, "bottom_right"):
    ft.alignment.bottom_right = ft.Alignment(1, 1)  # type: ignore[attr-defined]


# ─── 호환 헬퍼: 버튼 (text= → content=) ────────────────


def _patch_button_text_kw(cls) -> None:
    """`text=` 키워드를 0.85 의 `content=` 로 자동 변환."""

    if getattr(cls, "_text_kw_patched", False):
        return
    original = cls.__init__

    def __init__(self, *args, **kwargs):  # type: ignore[no-redef]
        if "text" in kwargs and "content" not in kwargs:
            kwargs["content"] = kwargs.pop("text")
        elif "text" in kwargs:
            kwargs.pop("text")
        original(self, *args, **kwargs)

    cls.__init__ = __init__  # type: ignore[method-assign]
    cls._text_kw_patched = True  # type: ignore[attr-defined]


for _btn_name in (
    "ElevatedButton",
    "FilledButton",
    "FilledTonalButton",
    "OutlinedButton",
    "TextButton",
):
    _btn = getattr(ft, _btn_name, None)
    if _btn is not None:
        _patch_button_text_kw(_btn)


# ─── 호환 헬퍼: Tab (text= → label=) ───────────────────


# ─── 호환 헬퍼: 이름이 바뀐 enum/class 별칭 ─────────────

if not hasattr(ft, "ImageFit") and hasattr(ft, "BoxFit"):
    ft.ImageFit = ft.BoxFit  # type: ignore[attr-defined]


_TabCls = getattr(ft, "Tab", None)
if _TabCls is not None and not getattr(_TabCls, "_text_kw_patched", False):
    _orig_tab_init = _TabCls.__init__

    def _tab_init(self, *args, **kwargs):  # type: ignore[no-redef]
        if "text" in kwargs and "label" not in kwargs:
            kwargs["label"] = kwargs.pop("text")
        elif "text" in kwargs:
            kwargs.pop("text")
        _orig_tab_init(self, *args, **kwargs)

    _TabCls.__init__ = _tab_init  # type: ignore[method-assign]
    _TabCls._text_kw_patched = True  # type: ignore[attr-defined]


from .theme import StreamMasterTheme  # noqa: E402

__all__ = ["StreamMasterTheme"]
