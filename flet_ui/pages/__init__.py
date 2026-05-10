"""Flet 페이지 모듈 모음."""

from .page_dashboard import build_dashboard
from .page_ocr import build_ocr_settings
from .page_arduino import build_arduino_link
from .page_web import build_web_stream

__all__ = [
    "build_dashboard",
    "build_ocr_settings",
    "build_arduino_link",
    "build_web_stream",
]
