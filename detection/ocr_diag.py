"""OCR 호출·다운로드·캐시 등 로그 (감지 스레드 → 큐 → OCR 로그 창에서 소비)."""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, List, Optional, Sequence

# 한 줄 앞에 이 sentinel 이 붙어 있으면 LogConsole 이 그 줄을 알림 색(밝은 빨강) 으로 렌더링한다.
# 파일 로그/외부 소비자는 줄을 화면에 보여주기 직전에 strip 하면 된다 (``flet_ui.log_buffers``).
ALERT_LOG_LINE_PREFIX = "\x01ALERT\x01"

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
    # engine/operation 모두 *최소* 10자로 맞춰 등폭 글꼴에서 컬럼이 가지런히 정렬되게 한다.
    line = f"#{n:5d} {ts} {engine:10s} {operation:10s} 호출            —    {d}\n"
    _queue.put(line)
    return n


def _format_matched_keywords(
    matched: Optional[Sequence[str]],
) -> tuple[Optional[str], bool]:
    """``end_ocr_call`` 보조: ``매치 키워드 목록`` 을 화면 표기 + 알림여부로 환산.

    - ``matched is None`` → ``(None, False)`` : 알림 검사를 안 한 호출.
    - 비어 있음 → ``("알림:없음", False)``.
    - 1개 이상 → ``("알림: 가나,마바", True)``.
    """
    if matched is None:
        return None, False
    seen: set[str] = set()
    ordered: List[str] = []
    for k in matched:
        s = (k or "").strip()
        if s and s not in seen:
            seen.add(s)
            ordered.append(s)
    if not ordered:
        return "알림:없음", False
    return "알림: " + ",".join(ordered), True


def end_ocr_call(
    call_id: int,
    operation: str,
    engine: str,
    duration_sec: float,
    detail: str = "",
    *,
    keyword_alert_hit: Optional[bool] = None,
    matched_keywords: Optional[Sequence[str]] = None,
    count_completed: bool = True,
) -> None:
    """해당 call_id 요청이 끝났을 때 호출 (예외여도 finally에서 호출 권장).

    ``matched_keywords`` 가 우선 적용된다 (예: ``["가나", "마바"]``).
    호환을 위해 레거시 ``keyword_alert_hit`` 도 그대로 받는다.
    """
    global _completed_calls
    if count_completed:
        with _lock:
            _completed_calls += 1
    ms = duration_sec * 1000.0
    ts = time.strftime("%H:%M:%S")
    d = _fmt_detail(detail)

    is_alert = False
    alert_text: Optional[str] = None
    if matched_keywords is not None:
        alert_text, is_alert = _format_matched_keywords(matched_keywords)
    elif keyword_alert_hit is True:
        alert_text, is_alert = "알림:있음", True
    elif keyword_alert_hit is False:
        alert_text = "알림:없음"

    # 응답 줄: "호출" 라인과 동일하게 ``응답            —    {detail} {ms} ms [알림:...]``
    # 순서로 출력해 등폭 글꼴에서 detail 컬럼이 호출 라인과 같이 정렬되게 한다.
    rest_parts: list[str] = []
    if d:
        rest_parts.append(d)
    rest_parts.append(f"{ms:9.2f} ms")
    if alert_text:
        rest_parts.append(alert_text)
    rest = " ".join(rest_parts)

    line = f"#{call_id:5d} {ts} {engine:10s} {operation:10s} 응답            —    {rest}\n"
    if is_alert:
        line = f"{ALERT_LOG_LINE_PREFIX}{line}"
    _queue.put(line)
    if is_alert and _on_keyword_alert_sound is not None:
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
