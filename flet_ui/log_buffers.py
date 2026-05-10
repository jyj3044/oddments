"""중앙 집중식 로그 버퍼 + 파일 영속화 모듈.

세 종류(OCR/Arduino/Web)의 소스 큐를 단일 데몬 스레드에서 지속적으로
드레인하여 다음을 수행한다.

* 메모리 ``deque`` (최대 500줄) 에 보관 — 페이지 재방문 시 그대로 표시.
* 일자별 파일 ``logs/{kind}-YYYYMMDD.log`` 에 추가 기록.
* 에러 전용으로 같은 날짜의 ``logs/{kind}_error-YYYYMMDD.log`` 에 한 줄을 즉시 추가할 수 있다.
* 앱 진단 ``log_app_event(..., ERROR|CRITICAL)`` 는 ``app_error-YYYYMMDD.log`` 에도 동일 본문을 남긴다.
* 1시간마다 ``LOG_RETENTION_DAYS`` (기본 2일) 보다 오래된 로그 파일 자동 삭제.

페이지(``page_ocr``, ``page_arduino``, ``page_web``) 들은 더 이상 소스 큐를
직접 드레인하지 않고, 이 모듈이 노출하는 ``Buffer.attach()`` 와
``Buffer.read_since(cursor)`` 만 사용한다. 이렇게 해서:

* 사용자가 다른 탭으로 이동했다가 돌아와도 로그가 사라지지 않는다.
* 두 곳에서 같은 큐를 동시에 드레인하다 발생하는 경합/누락이 없다.
* 메모리 사용은 큐 크기와 무관하게 500줄로 일정하게 제한된다.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from detection.ocr_diag import ALERT_LOG_LINE_PREFIX, drain_ocr_log_lines
from streaming.remote_log import drain_remote_log_lines
from streaming.web_log import drain_web_log_lines

if sys.platform == "win32":
    from arduino.serial_bridge import (
        drain_key_bridge_debug_lines,
        drain_received_serial_lines,
        take_arduino_notice_lines,
    )
else:  # pragma: no cover - 비-Windows 스텁

    def take_arduino_notice_lines() -> list[str]:  # type: ignore[no-redef]
        return []

    def drain_received_serial_lines(_max_n: int = 200) -> list[str]:  # type: ignore[no-redef]
        return []

    def drain_key_bridge_debug_lines(_max_n: int = 200) -> list[str]:  # type: ignore[no-redef]
        return []


MAX_LOG_LINES = 500
LOG_RETENTION_DAYS = 2
LOG_DIR_NAME = "logs"
DRAIN_INTERVAL_SEC = 0.1
CLEANUP_INTERVAL_SEC = 3600.0


def _log_dir_path() -> Path:
    """로그 파일을 둘 디렉토리. PyInstaller exe 일 경우 실행파일과 같은 폴더 옆,
    아니면 작업 디렉토리(``cwd``) 아래 ``logs/`` 를 사용한다.
    """
    base: Path
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path.cwd()
    return base / LOG_DIR_NAME


_sidecar_err_lock = threading.Lock()


def append_sidecar_error_file(kind: str, message: str) -> None:
    """에러 한 줄을 ``logs/{kind}_error-YYYYMMDD.log`` 에 즉시 추가한다.

    드레인 버퍼와 별도로 남기므로, UI 링 버퍼를 거치지 않는 진단용 부가 기록이다.
    ``kind`` 는 ``remote`` / ``web`` / ``ocr`` 등 기존 로그 종류 이름과 동일하게 둔다.
    """
    clean = (message or "").replace("\r", " ").replace("\n", " ").strip()
    if not clean:
        return
    if len(clean) > 4000:
        clean = clean[:3999] + "…"
    try:
        d = _log_dir_path()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{kind}_error-{datetime.now():%Y%m%d}.log"
    except Exception:
        return
    ts = datetime.now().strftime("%H:%M:%S")
    with _sidecar_err_lock:
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {clean}\n")
        except OSError:
            pass


class LogBuffer:
    """단일 종류의 로그 라인을 보관하는 스레드-안전 ring buffer."""

    def __init__(self, name: str, *, max_lines: int = MAX_LOG_LINES) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._lines: deque[str] = deque(maxlen=max_lines)
        # 모든 push 누적 카운터(절대 인덱스). 페이지가 cursor 로 사용한다.
        self._counter: int = 0

    @property
    def name(self) -> str:
        return self._name

    def push_many(self, lines: Iterable[str]) -> int:
        """라인을 버퍼에 추가하고 새 ``counter`` 를 반환한다."""
        added = 0
        with self._lock:
            for ln in lines:
                self._lines.append(ln)
                self._counter += 1
                added += 1
            return self._counter if added else self._counter

    def attach(self) -> tuple[list[str], int]:
        """페이지가 처음 마운트될 때 호출. 현재 버퍼의 전체 스냅샷과 그 시점
        ``cursor`` 를 함께 반환해 페이지가 LogConsole 에 한 번에 prefill 할 수 있게
        한다.
        """
        with self._lock:
            return list(self._lines), self._counter

    def read_since(self, cursor: int) -> tuple[list[str], int]:
        """``cursor`` 이후 추가된 라인만 반환. ``cursor`` 가 너무 오래되어
        링버퍼에서 이미 덮어쓰여진 부분이 있으면 잃어버린 라인은 자연스럽게
        스킵된다(현재 버퍼 안에 있는 가장 오래된 라인부터 보낸다).
        """
        with self._lock:
            new_total = self._counter
            buf_size = len(self._lines)
            if cursor >= new_total:
                return [], new_total
            oldest_abs = new_total - buf_size  # 버퍼 첫 라인의 절대 인덱스
            start_abs = max(cursor, oldest_abs)
            offset = start_abs - oldest_abs
            return list(self._lines)[offset:], new_total

    def clear(self) -> int:
        """메모리 버퍼를 비운다. ``counter`` 는 유지하므로 이미 진행 중이던 페이지의
        ``cursor`` 가 새 라인을 정상적으로 받는다.
        """
        with self._lock:
            self._lines.clear()
            return self._counter


class LogStore:
    """OCR/Arduino/Web/Remote 네 버퍼를 묶고 백그라운드 드레인 + 파일 영속화를 담당.

    에러 부가 파일(``{kind}_error-*.log``, ``app_error-*.log``)은 각 로깅 API 가 직접 쓴다.
    """

    def __init__(self) -> None:
        self.ocr = LogBuffer("ocr")
        self.arduino = LogBuffer("arduino")
        self.web = LogBuffer("web")
        self.remote = LogBuffer("remote")

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._file_lock = threading.Lock()
        self._last_cleanup_ts: float = 0.0
        self._dir_ensured = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="oddments-log-store", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        self._thread = None

    def _loop(self) -> None:
        try:
            self._cleanup_old_files()
        except Exception:
            pass
        self._last_cleanup_ts = time.time()

        while not self._stop.is_set():
            try:
                self._drain_once()
            except Exception:
                pass
            now = time.time()
            if now - self._last_cleanup_ts > CLEANUP_INTERVAL_SEC:
                try:
                    self._cleanup_old_files()
                except Exception:
                    pass
                self._last_cleanup_ts = now
            if self._stop.wait(DRAIN_INTERVAL_SEC):
                return

    def _drain_once(self) -> None:
        ocr_lines = drain_ocr_log_lines(500)
        if ocr_lines:
            self.ocr.push_many(ocr_lines)
            self._write_lines("ocr", ocr_lines)

        web_lines = drain_web_log_lines(500)
        if web_lines:
            self.web.push_many(web_lines)
            self._write_lines("web", web_lines)

        remote_lines = drain_remote_log_lines(500)
        if remote_lines:
            self.remote.push_many(remote_lines)
            self._write_lines("remote", remote_lines)

        ard_lines: list[str] = []
        try:
            ard_lines.extend(take_arduino_notice_lines())
            ard_lines.extend(drain_received_serial_lines(500))
            ard_lines.extend(drain_key_bridge_debug_lines(500))
        except Exception:
            pass
        if ard_lines:
            self.arduino.push_many(ard_lines)
            self._write_lines("arduino", ard_lines)

    def _ensure_dir(self) -> Path:
        d = _log_dir_path()
        if not self._dir_ensured:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            self._dir_ensured = True
        return d

    def _today_path(self, kind: str) -> Path:
        return self._ensure_dir() / f"{kind}-{datetime.now():%Y%m%d}.log"

    def _write_lines(self, kind: str, lines: Iterable[str]) -> None:
        try:
            path = self._today_path(kind)
        except Exception:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        with self._file_lock:
            try:
                with path.open("a", encoding="utf-8") as f:
                    for ln in lines:
                        try:
                            # 알림 sentinel 과 trailing newline 은 파일에서 제거.
                            clean = ln
                            if clean.startswith(ALERT_LOG_LINE_PREFIX):
                                clean = clean[len(ALERT_LOG_LINE_PREFIX):]
                            clean = clean.rstrip("\r\n")
                            f.write(f"[{ts}] {clean}\n")
                        except Exception:
                            continue
            except OSError:
                pass

    def _cleanup_old_files(self) -> None:
        d = self._ensure_dir()
        if not d.exists():
            return
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        try:
            entries = list(d.glob("*.log"))
        except OSError:
            return
        for f in entries:
            try:
                if f.stat().st_mtime < cutoff:
                    try:
                        f.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
            except OSError:
                continue

    # ─── 앱 레벨 진단 로그 (UI 버퍼와 무관, 파일에만 기록) ──────────────
    def log_app_event(
        self,
        level: str,
        message: str,
        *,
        detail: Optional[str] = None,
    ) -> None:
        """``logs/app-YYYYMMDD.log`` 에 한 줄(또는 여러 줄) 추가.

        ``ERROR``/``CRITICAL`` 이면 동일 내용을 ``logs/app_error-YYYYMMDD.log`` 에도 남긴다.

        UI 의 LogConsole 에는 표시하지 않는다(별도 화면이 없음). 단순 파일 진단용.
        ``level`` 은 ``INFO``/``WARN``/``ERROR`` 등 자유 문자열. ``detail`` 이
        주어지면 다음 줄들에 ``    | `` prefix 로 들여쓰기해 traceback 같은 멀티라인을
        읽기 쉽게 정렬한다.
        """
        try:
            path = self._ensure_dir() / f"app-{datetime.now():%Y%m%d}.log"
        except Exception:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lvl = (level or "INFO").upper()
        head = (message or "").rstrip("\r\n")
        with self._file_lock:
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"[{ts}] {lvl} {head}\n")
                    if detail:
                        for ln in detail.splitlines():
                            f.write(f"    | {ln}\n")
                if lvl in ("ERROR", "CRITICAL"):
                    try:
                        err_path = (
                            self._ensure_dir()
                            / f"app_error-{datetime.now():%Y%m%d}.log"
                        )
                        with err_path.open("a", encoding="utf-8") as ef:
                            ef.write(f"[{ts}] {lvl} {head}\n")
                            if detail:
                                for ln in detail.splitlines():
                                    ef.write(f"    | {ln}\n")
                    except OSError:
                        pass
            except OSError:
                pass


_store: Optional[LogStore] = None
_store_lock = threading.Lock()


def get_log_store() -> LogStore:
    """프로세스 전역 ``LogStore`` 인스턴스를 가져오고, 시작되어 있지 않으면
    백그라운드 드레인 스레드를 기동한다.
    """
    global _store
    with _store_lock:
        if _store is None:
            _store = LogStore()
            _store.start()
        elif _store._thread is None or not _store._thread.is_alive():
            _store.start()
        return _store


def shutdown_log_store() -> None:
    """앱 종료 시 호출. 드레인 스레드를 안전하게 멈춘다."""
    global _store
    with _store_lock:
        s = _store
        if s is not None:
            try:
                s.stop()
            except Exception:
                pass


def log_app_event(
    level: str,
    message: str,
    *,
    detail: Optional[str] = None,
) -> None:
    """모듈 레벨 편의 래퍼. 어디서든 한 줄로 ``logs/app-*.log`` 에 기록한다.

    ``LogStore`` 가 아직 만들어지지 않았다면 자동으로 생성/시작한다(파일 쓰기만
    필요하므로 드레인 스레드도 함께 시작되지만 별 부담은 없다).
    """
    try:
        get_log_store().log_app_event(level, message, detail=detail)
    except Exception:
        pass


__all__ = [
    "LogBuffer",
    "LogStore",
    "append_sidecar_error_file",
    "get_log_store",
    "shutdown_log_store",
    "log_app_event",
    "MAX_LOG_LINES",
    "LOG_RETENTION_DAYS",
]
