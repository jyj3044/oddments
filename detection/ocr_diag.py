"""OCR 호출·다운로드·캐시 등 로그 (감지 스레드 → 큐 → OCR 로그 창에서 소비)."""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, List, Optional

_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
_lock = threading.Lock()
_next_call_id: int = 0
_completed_calls: int = 0
_on_keyword_alert_sound: Optional[Callable[[], None]] = None


def set_ocr_keyword_alert_sound_handler(
    fn: Optional[Callable[[], None]],
) -> None:
    """OCR 응답에 알림 키워드가 있을 때 호출할 콜백(보통 UI 스레드에서 소리). None 이면 비활성."""
    global _on_keyword_alert_sound
    _on_keyword_alert_sound = fn


def _fmt_detail(detail: str, *, max_len: int = 120) -> str:
    d = (detail or "").replace("\n", " ")
    if max_len > 0 and len(d) > max_len:
        d = d[: max_len - 1] + "…"
    return d


def log_ocr_activity(
    kind: str,
    engine: str,
    detail: str = "",
    *,
    truncate_detail: bool = True,
) -> None:
    """
    API 호출 한 쌍이 아닌 한 줄 로그 (다운로드·업데이트·캐시·실패 등).
    줄 앞머리는 * 로 번호형(#) 호출 로그와 구분.
    truncate_detail=False 이면 긴 진단 메시지(예: exe ONNX 로드 실패)를 OCR 로그에 그대로 남김.
    """
    ts = time.strftime("%H:%M:%S")
    k = (kind or "정보")[:10].ljust(10)
    eng = (engine or "—")[:10].ljust(10)
    raw = (detail or "").replace("\r\n", "\n").replace("\r", "\n")
    if truncate_detail:
        d = _fmt_detail(raw, max_len=120)
    else:
        d = raw.rstrip()
        if len(d) > 12000:
            d = d[:11999] + "…"
        d = d.replace("\n", "\n    ")
    line = f"* {ts} {eng} {k} — {d}\n"
    _queue.put(line)


def begin_ocr_call(
    operation: str,
    engine: str,
    detail: str = "",
) -> int:
    """OCR API 진입 직전 호출. 반환 id는 end_ocr_call과 짝을 맞춘다."""
    global _next_call_id
    with _lock:
        _next_call_id += 1
        n = _next_call_id
    ts = time.strftime("%H:%M:%S")
    d = _fmt_detail(detail)
    # 응답 줄의 ms 자리(약 9칸)와 맞추기 위해 호출 줄은 고정 폭
    line = f"#{n:5d} {ts} {engine:10s} {operation:18s} 호출            —    {d}\n"
    _queue.put(line)
    return n


def end_ocr_call(
    call_id: int,
    operation: str,
    engine: str,
    duration_sec: float,
    detail: str = "",
    *,
    keyword_alert_hit: Optional[bool] = None,
) -> None:
    """해당 call_id 요청이 끝났을 때 호출 (예외여도 finally에서 호출 권장)."""
    global _completed_calls
    with _lock:
        _completed_calls += 1
    ms = duration_sec * 1000.0
    ts = time.strftime("%H:%M:%S")
    d = _fmt_detail(detail)
    if keyword_alert_hit is True:
        d = f"{d} 알림:있음" if d else "알림:있음"
    elif keyword_alert_hit is False:
        d = f"{d} 알림:없음" if d else "알림:없음"
    line = f"#{call_id:5d} {ts} {engine:10s} {operation:18s} {ms:9.2f} ms 응답 {d}\n"
    _queue.put(line)
    if keyword_alert_hit is True and _on_keyword_alert_sound is not None:
        try:
            _on_keyword_alert_sound()
        except Exception:
            pass


def record_ocr_call(
    operation: str,
    engine: str,
    duration_sec: float,
    detail: str = "",
) -> None:
    """한 줄만 남기던 구식 API. 내부적으로 호출→응답 두 줄로 기록한다."""
    cid = begin_ocr_call(operation, engine, detail)
    end_ocr_call(cid, operation, engine, duration_sec, detail)


def drain_ocr_log_lines(max_n: int = 200) -> List[str]:
    out: List[str] = []
    for _ in range(max_n):
        try:
            out.append(_queue.get_nowait())
        except queue.Empty:
            break
    return out


def get_ocr_call_total() -> int:
    """완료(end)된 OCR API 호출 수."""
    with _lock:
        return _completed_calls


def reset_ocr_log() -> None:
    global _next_call_id, _completed_calls
    with _lock:
        _next_call_id = 0
        _completed_calls = 0
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
