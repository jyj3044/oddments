"""
키워드 OCR 엔진 추상화: Tesseract / EasyOCR / RapidOCR.
각 백엔드는 RGB uint8 ndarray 입력을 가정한다 (OpenCV BGR 프레임은 호출부에서 변환).
"""

from __future__ import annotations

import math
import ssl
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union
from urllib.error import URLError
from urllib.request import Request, urlopen

import cv2
import numpy as np

from .ocr_diag import begin_ocr_call, end_ocr_call, log_ocr_activity


def _import_error_detail(exc: BaseException) -> str:
    """ImportError 등에 묻힌 DLL/원인 한 줄로 펼침 (exe 진단용)."""
    parts: list[str] = []
    cur: BaseException | None = exc
    for _ in range(6):
        if cur is None:
            break
        s = (str(cur) or "").strip() or type(cur).__name__
        parts.append(s)
        cur = cur.__cause__
    return " | ".join(parts)


_runtime_fail_logged: dict[str, str] = {}


def _log_ocr_runtime_failure_once(engine_label: str, ok: bool, msg: str) -> None:
    """OCR 런타임 점검 실패 시 메시지가 바뀔 때만 OCR 로그에 전체 기록 (상태줄은 짧게 유지)."""
    key = (engine_label or "?").strip() or "?"
    if ok:
        _runtime_fail_logged.pop(key, None)
        return
    m = (msg or "").strip()
    if not m:
        return
    if _runtime_fail_logged.get(key) == m:
        return
    _runtime_fail_logged[key] = m
    log_ocr_activity("오류", key, m, truncate_detail=False)


_tesseract_version_probe_logged = False
_tesseract_version_probe_lock = threading.Lock()


def _shape_prep_detail(
    rgb: np.ndarray, preprocess_label: str = "", extra: str = ""
) -> str:
    h, w = rgb.shape[:2]
    parts = [f"{w}x{h}"]
    p = (preprocess_label or "").strip()
    if p:
        parts.append(p)
    e = (extra or "").strip()
    if e:
        parts.append(e)
    return " ".join(parts)


def _keyword_alert_hit(
    text_blob: str, alert_keywords: Optional[Tuple[str, ...]]
) -> Optional[bool]:
    if not alert_keywords:
        return None
    kws = tuple((k or "").strip().lower() for k in alert_keywords if (k or "").strip())
    if not kws:
        return None
    b = (text_blob or "").lower()
    return any(k in b for k in kws)


def _boxes_text_blob(
    boxes: List[Tuple[str, Tuple[int, int, int, int]]],
) -> str:
    return " ".join((b[0] or "").lower() for b in boxes)

ENGINE_TESSERACT = "tesseract"
ENGINE_EASYOCR = "easyocr"
ENGINE_RAPIDOCR = "rapidocr"
DEFAULT_OCR_ENGINE = ENGINE_RAPIDOCR
ALL_OCR_ENGINES: Tuple[str, ...] = (ENGINE_RAPIDOCR,)

_OCR_ENGINE_ALIASES: Dict[str, str] = {
    "rapid": ENGINE_RAPIDOCR,
    "rapid_ocr": ENGINE_RAPIDOCR,
    "rapid-ocr": ENGINE_RAPIDOCR,
}

_TESS_LANGS: Tuple[str, ...] = ("kor+eng", "kor", "eng+kor", "eng")

_easy_reader = None
_rapid_ocr = None
_rapid_ocr_korean_active = False
_RAPID_INIT_LOCK = threading.Lock()
_RAPID_INFER_LOCK = threading.Lock()

# rapidocr_onnxruntime 기본값은 중국어 인식(rec)만 있어 한글이 거의 안 나옴 → PP-OCR 한국어 rec + 사전 사용
_RAPID_KO_ONNX_URLS: Tuple[str, ...] = (
    "https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv1/korean_mobile_v2.0_rec_infer.onnx",
    # 국내/기업망에서 huggingface.co 가 막힐 때 대안
    "https://hf-mirror.com/SWHL/RapidOCR/resolve/main/PP-OCRv1/korean_mobile_v2.0_rec_infer.onnx",
)
_RAPID_KO_KEYS_URLS: Tuple[str, ...] = (
    "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/ppocr/utils/dict/korean_dict.txt",
    "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/release/2.7/ppocr/utils/dict/korean_dict.txt",
)


def _rapid_korean_cache_dir() -> Path:
    p = Path.home() / ".cache" / "oddments" / "rapidocr_korean"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ssl_context_for_download() -> ssl.SSLContext:
    """
    macOS 등에서 python.org 빌드가 시스템 CA를 못 쓰면 urllib 가 SSL 검증 실패한다.
    certifi 번들 CA를 쓰면 Hugging Face / GitHub raw 다운로드가 안정된다.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _download_to_file(
    url: str,
    dest: Path,
    *,
    min_bytes: int,
    timeout_sec: float = 120.0,
    log_engine: str = "rapidocr",
    asset_name: str = "",
) -> bool:
    label = (asset_name or dest.name).strip() or dest.name
    tail = url.rstrip("/").split("/")[-1]
    if "?" in tail:
        tail = tail.split("?", 1)[0]
    tail = (tail or "url")[:72]

    if dest.exists() and dest.stat().st_size >= min_bytes:
        log_ocr_activity(
            "캐시",
            log_engine,
            f"{label} 기존 파일 사용 ({dest.stat().st_size:,} B)",
        )
        return True

    phase = "업데이트" if dest.exists() else "다운로드"
    log_ocr_activity(
        phase,
        log_engine,
        f"{label} 시작 ← {tail}",
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    ctx = _ssl_context_for_download()
    try:
        req = Request(url, headers={"User-Agent": "Oddments/1.0 (RapidOCR Korean assets)"})
        with urlopen(req, timeout=timeout_sec, context=ctx) as resp:
            data = resp.read()
        if len(data) < min_bytes:
            log_ocr_activity(
                "실패",
                log_engine,
                f"{label} 응답 크기 부족 ({len(data)} B)",
            )
            return False
        tmp.write_bytes(data)
        tmp.replace(dest)
        log_ocr_activity(
            phase,
            log_engine,
            f"{label} 완료 ({len(data):,} B) → {dest.name}",
        )
        return True
    except (OSError, URLError, ValueError) as e:
        log_ocr_activity(
            "실패",
            log_engine,
            f"{label} {phase} 중단: {e}",
        )
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False


def _download_first_url(
    urls: Union[str, Sequence[str]],
    dest: Path,
    *,
    min_bytes: int,
    timeout_sec: float = 120.0,
    log_engine: str = "rapidocr",
    asset_name: str = "",
) -> bool:
    seq: Tuple[str, ...] = (urls,) if isinstance(urls, str) else tuple(urls)
    for u in seq:
        if _download_to_file(
            u,
            dest,
            min_bytes=min_bytes,
            timeout_sec=timeout_sec,
            log_engine=log_engine,
            asset_name=asset_name,
        ):
            return True
    return False


def _ensure_rapid_korean_assets() -> Tuple[Optional[Path], Optional[Path]]:
    d = _rapid_korean_cache_dir()
    onnx_p = d / "korean_mobile_v2.0_rec_infer.onnx"
    keys_p = d / "korean_dict.txt"
    ok_o = _download_first_url(
        _RAPID_KO_ONNX_URLS,
        onnx_p,
        min_bytes=500_000,
        asset_name="한글 rec ONNX",
    )
    ok_k = _download_first_url(
        _RAPID_KO_KEYS_URLS,
        keys_p,
        min_bytes=10_000,
        asset_name="한글 사전",
    )
    if ok_o and ok_k:
        return onnx_p, keys_p
    return None, None


def normalize_ocr_engine(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return ""
    # 제거된 엔진(설정 JSON 호환): 무시
    if n in ("paddleocr", "paddle"):
        return ""
    if n in ("tesseract", "easyocr", "tess", "easy"):
        return ""
    if n in ALL_OCR_ENGINES:
        return n
    if n in _OCR_ENGINE_ALIASES:
        return _OCR_ENGINE_ALIASES[n]
    return DEFAULT_OCR_ENGINE


def _get_easy_reader():
    global _easy_reader
    if _easy_reader is None:
        log_ocr_activity(
            "정보",
            "easyocr",
            "Reader 초기화 — 최초 실행 시 모델 캐시 다운로드가 있을 수 있음",
        )
        import easyocr

        _easy_reader = easyocr.Reader(["ko", "en"], gpu=False, verbose=False)
    return _easy_reader


def _get_rapid():
    global _rapid_ocr, _rapid_ocr_korean_active
    if _rapid_ocr is not None:
        return _rapid_ocr
    with _RAPID_INIT_LOCK:
        if _rapid_ocr is not None:
            return _rapid_ocr
        from rapidocr_onnxruntime import RapidOCR

        inst = None
        korean_ok = False
        onnx_p, keys_p = _ensure_rapid_korean_assets()
        if onnx_p is not None and keys_p is not None:
            r_onnx = str(onnx_p.resolve())
            r_keys = str(keys_p.resolve())
            for rec_shape in ([3, 32, 320], [3, 48, 320]):
                try:
                    inst = RapidOCR(
                        rec_model_path=r_onnx,
                        rec_keys_path=r_keys,
                        rec_img_shape=rec_shape,
                    )
                    korean_ok = True
                    break
                except Exception:
                    inst = None
        if inst is None:
            inst = RapidOCR()
            korean_ok = False
        _rapid_ocr = inst
        _rapid_ocr_korean_active = korean_ok
        if korean_ok:
            log_ocr_activity(
                "정보",
                "rapidocr",
                "한글 rec·사전 적용된 RapidOCR 인스턴스 준비됨",
            )
        else:
            log_ocr_activity(
                "정보",
                "rapidocr",
                "한글 자원 미적용 — 패키지 기본(중국어 위주) 인식",
            )
        return _rapid_ocr


def _quad_to_xywh(box) -> Tuple[int, int, int, int]:
    arr = np.asarray(box, dtype=np.float64).reshape(-1, 2)
    x0, y0 = arr.min(axis=0)
    x1, y1 = arr.max(axis=0)
    w = max(1, int(round(x1 - x0)))
    h = max(1, int(round(y1 - y0)))
    return int(round(x0)), int(round(y0)), w, h


def _parse_tesseract_conf(raw) -> float:
    try:
        if raw is None or raw == "":
            return -1.0
        v = float(raw)
        if math.isnan(v):
            return -1.0
        return v
    except (ValueError, TypeError):
        return -1.0


def _boxes_from_tesseract_dict(
    data: Dict[str, List],
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    out: List[Tuple[str, Tuple[int, int, int, int]]] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        c = _parse_tesseract_conf(data.get("conf", [""])[i])
        has_hangul = any("\uac00" <= ch <= "\ud7a3" for ch in text)
        if c < 0:
            if not has_hangul and len(text) < 2:
                continue
        x, y, w, h = (
            int(data["left"][i]),
            int(data["top"][i]),
            int(data["width"][i]),
            int(data["height"][i]),
        )
        out.append((text, (x, y, w, h)))
    return out


def _tesseract_dict_text_blob(data: Optional[Dict[str, List]]) -> str:
    if not data:
        return ""
    parts = data.get("text") or []
    return " ".join((str(t) or "").lower() for t in parts)


def _tesseract_exe_display() -> str:
    try:
        import pytesseract.pytesseract as pt

        cmd = getattr(pt, "tesseract_cmd", None)
        if cmd:
            p = Path(str(cmd))
            return p.name if p.name else str(cmd)[:48]
    except Exception:
        pass
    return "tesseract"


def _log_tesseract_subprocess(operation: str, extra: str = "") -> None:
    exe = _tesseract_exe_display()
    msg = f"{exe} 자식 프로세스 실행 | {operation}"
    e = (extra or "").strip()
    if e:
        msg = f"{msg} | {e}"
    log_ocr_activity("프로세스", "tesseract", msg)


def _log_tesseract_version_probe_once() -> None:
    """UI 가 주기적으로 ocr_runtime_ok 를 호출하므로 버전 프로브는 최초 1회만 로그."""
    global _tesseract_version_probe_logged
    with _tesseract_version_probe_lock:
        if _tesseract_version_probe_logged:
            return
        _tesseract_version_probe_logged = True
    _log_tesseract_subprocess(
        "get_tesseract_version",
        "설치·PATH 확인 (동일 프로세스 내 이후 폴링은 프로세스 로그 생략)",
    )


def tesseract_image_to_data(
    rgb: np.ndarray,
    ocr_config: str,
    *,
    preprocess_label: str = "",
    alert_keywords: Optional[Tuple[str, ...]] = None,
) -> Optional[Dict[str, List]]:
    try:
        import pytesseract
    except ImportError:
        return None
    cfg_short = (ocr_config or "")[:40]
    for lang in _TESS_LANGS:
        try:
            d = _shape_prep_detail(rgb, preprocess_label, f"lang={lang} {cfg_short}")
            cid = begin_ocr_call("image_to_data", "tesseract", d)
            t0 = time.perf_counter()
            out: Optional[Dict[str, List]] = None
            try:
                _log_tesseract_subprocess(
                    "image_to_data",
                    f"lang={lang} {cfg_short}".strip(),
                )
                out = pytesseract.image_to_data(
                    rgb,
                    output_type=pytesseract.Output.DICT,
                    lang=lang,
                    config=ocr_config,
                )
            finally:
                end_ocr_call(
                    cid,
                    "image_to_data",
                    "tesseract",
                    time.perf_counter() - t0,
                    d,
                    keyword_alert_hit=_keyword_alert_hit(
                        _tesseract_dict_text_blob(out), alert_keywords
                    ),
                )
            return out
        except Exception:
            continue
    d2 = _shape_prep_detail(rgb, preprocess_label, f"lang=- {cfg_short}")
    cid2 = begin_ocr_call("image_to_data", "tesseract", d2)
    t0 = time.perf_counter()
    out_fb: Optional[Dict[str, List]] = None
    try:
        _log_tesseract_subprocess("image_to_data", f"lang=- {cfg_short}".strip())
        out_fb = pytesseract.image_to_data(
            rgb, output_type=pytesseract.Output.DICT, config=ocr_config
        )
    except Exception:
        pass
    finally:
        end_ocr_call(
            cid2,
            "image_to_data",
            "tesseract",
            time.perf_counter() - t0,
            d2,
            keyword_alert_hit=_keyword_alert_hit(
                _tesseract_dict_text_blob(out_fb), alert_keywords
            ),
        )
    return out_fb


def tesseract_call_image_to_string(
    rgb: np.ndarray,
    *,
    lang: Optional[str] = None,
    config: str = "",
    preprocess_label: str = "",
    alert_keywords: Optional[Tuple[str, ...]] = None,
) -> str:
    """keywords.py용: image_to_string + 진단 로그."""
    try:
        import pytesseract
    except ImportError:
        return ""
    d = _shape_prep_detail(
        rgb, preprocess_label, f"{lang or '-'} {(config or '')[:40]}"
    )
    cid = begin_ocr_call("image_to_string", "tesseract", d)
    t0 = time.perf_counter()
    out = ""
    try:
        _log_tesseract_subprocess(
            "image_to_string",
            f"{lang or '-'} {(config or '')[:40]}".strip(),
        )
        if lang:
            s = pytesseract.image_to_string(rgb, lang=lang, config=config)
        else:
            s = pytesseract.image_to_string(rgb, config=config)
        out = s if s else ""
    except Exception:
        pass
    finally:
        end_ocr_call(
            cid,
            "image_to_string",
            "tesseract",
            time.perf_counter() - t0,
            d,
            keyword_alert_hit=_keyword_alert_hit(out, alert_keywords),
        )
    return out


def boxes_from_tesseract_data(
    data: Dict[str, List],
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    return _boxes_from_tesseract_dict(data)


def ocr_word_boxes_tesseract(
    rgb: np.ndarray,
    tesseract_psm: str = "--oem 3 --psm 6",
    *,
    preprocess_label: str = "",
    alert_keywords: Optional[Tuple[str, ...]] = None,
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    data = tesseract_image_to_data(
        rgb,
        tesseract_psm,
        preprocess_label=preprocess_label,
        alert_keywords=alert_keywords,
    )
    if not data:
        return []
    return _boxes_from_tesseract_dict(data)


def ocr_word_boxes_easyocr(
    rgb: np.ndarray,
    *,
    preprocess_label: str = "",
    alert_keywords: Optional[Tuple[str, ...]] = None,
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    reader = _get_easy_reader()
    detail = _shape_prep_detail(rgb, preprocess_label)
    cid = begin_ocr_call("readtext", "easyocr", detail)
    t0 = time.perf_counter()
    rows = None
    # 탐지 임계값을 올려 배경·이진 노이즈에서 가짜 박스가 줄어듦. decoder=greedy 가 더 빠름.
    _rt_kw = dict(
        decoder="greedy",
        paragraph=False,
        text_threshold=0.65,
        low_text=0.48,
        link_threshold=0.35,
    )
    out: List[Tuple[str, Tuple[int, int, int, int]]] = []
    try:
        try:
            rows = reader.readtext(rgb, **_rt_kw)
        except TypeError:
            try:
                rows = reader.readtext(rgb, decoder="greedy", paragraph=False)
            except Exception:
                rows = None
        except Exception:
            rows = None
        if rows is not None:
            for row in rows:
                if len(row) < 2:
                    continue
                bbox, text = row[0], row[1]
                conf = float(row[2]) if len(row) > 2 else 1.0
                if conf < 0.22:
                    continue
                t = (text or "").strip()
                if not t:
                    continue
                x, y, w, h = _quad_to_xywh(bbox)
                out.append((t, (x, y, w, h)))
    finally:
        end_ocr_call(
            cid,
            "readtext",
            "easyocr",
            time.perf_counter() - t0,
            detail,
            keyword_alert_hit=_keyword_alert_hit(
                _boxes_text_blob(out), alert_keywords
            ),
        )
    return out


def ocr_word_boxes_rapidocr(
    rgb: np.ndarray,
    *,
    preprocess_label: str = "",
    alert_keywords: Optional[Tuple[str, ...]] = None,
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    engine = _get_rapid()
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    detail = _shape_prep_detail(rgb, preprocess_label)
    cid = begin_ocr_call("infer", "rapidocr", detail)
    t0 = time.perf_counter()
    raw = None
    out: List[Tuple[str, Tuple[int, int, int, int]]] = []
    try:
        try:
            with _RAPID_INFER_LOCK:
                raw = engine(bgr)
        except Exception:
            raw = None
        if raw is not None:
            if isinstance(raw, tuple) and len(raw) >= 1:
                result = raw[0]
            else:
                result = raw
            if result:
                for item in result:
                    if item is None:
                        continue
                    if hasattr(item, "text") and hasattr(item, "box"):
                        box, text = item.box, item.text
                        score = float(getattr(item, "score", 1.0))
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        box, text = item[0], item[1]
                        score = float(item[2]) if len(item) > 2 else 1.0
                    else:
                        continue
                    if score < 0.12:
                        continue
                    t = (str(text) or "").strip()
                    if not t:
                        continue
                    x, y, w, h = _quad_to_xywh(box)
                    out.append((t, (x, y, w, h)))
    finally:
        end_ocr_call(
            cid,
            "infer",
            "rapidocr",
            time.perf_counter() - t0,
            detail,
            keyword_alert_hit=_keyword_alert_hit(
                _boxes_text_blob(out), alert_keywords
            ),
        )
    return out


def ocr_word_boxes(
    rgb: np.ndarray,
    engine: str,
    *,
    tesseract_psm: str = "--oem 3 --psm 6",
    preprocess_label: str = "",
    alert_keywords: Optional[Tuple[str, ...]] = None,
) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    eng = normalize_ocr_engine(engine)
    if eng == ENGINE_RAPIDOCR:
        return ocr_word_boxes_rapidocr(
            rgb, preprocess_label=preprocess_label, alert_keywords=alert_keywords
        )
    return []


def joined_text_from_rgb(
    rgb: np.ndarray,
    engine: str,
    *,
    preprocess_label: str = "",
    alert_keywords: Optional[Tuple[str, ...]] = None,
) -> str:
    """단일 RGB 이미지에서 한 줄로 이은 전체 문자열 (뉴럴 엔진용)."""
    eng = normalize_ocr_engine(engine)
    if eng == ENGINE_RAPIDOCR:
        boxes = ocr_word_boxes_rapidocr(
            rgb, preprocess_label=preprocess_label, alert_keywords=alert_keywords
        )
        return " ".join(t for t, _ in boxes)
    return ""


def _ocr_engine_runtime_ok_core(eng: str) -> Tuple[bool, str]:
    if not eng:
        return False, "알 수 없는 OCR 엔진"
    if eng == ENGINE_RAPIDOCR:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError as e:
            detail = _import_error_detail(e)
            if getattr(sys, "frozen", False):
                tb = "".join(
                    traceback.format_exception(type(e), e, e.__traceback__)
                ).rstrip()
                return (
                    False,
                    "RapidOCR/ONNX 로드 실패. "
                    "① exe 빌드 venv 에서 requirements-runtime 의 onnxruntime==1.21.1 설치 후 재빌드 "
                    "(1.22+ 는 PyInstaller 번들과 pybind 초기화 충돌 보고 있음) "
                    "② INCLUDE_EASYOCR=False·_internal 에 torch 없음 ③ VC++ 재배포·pyi_rthook_onnx_error.txt 확인. "
                    f"상세: {detail}\n--- traceback ---\n{tb}",
                )
            return False, f"rapidocr-onnxruntime 미설치: {detail} (pip install rapidocr-onnxruntime)"
        try:
            _get_rapid()
        except ImportError as e:
            detail = _import_error_detail(e)
            if getattr(sys, "frozen", False):
                tb = "".join(
                    traceback.format_exception(type(e), e, e.__traceback__)
                ).rstrip()
                return (
                    False,
                    "ONNX Runtime DLL/모듈 로드 실패(PyInstaller). "
                    "VC++ 재배포·깨끗한 venv 재빌드·bootstrap_onnx_error.txt 를 확인하세요. "
                    f"상세: {detail}\n--- traceback ---\n{tb}",
                )
            return False, f"RapidOCR 초기화 import 실패: {e!s}"
        except Exception as e:
            return False, str(e)
        if _rapid_ocr_korean_active:
            return True, ""
        return (
            True,
            "한글 인식용 모델·사전을 받지 못했습니다. "
            "중국어 기본 인식만 사용되어 한글이 거의 안 나올 수 있습니다. "
            "네트워크 확인 후 "
            f"{_rapid_korean_cache_dir()} 에 ONNX·korean_dict.txt 가 생기는지 보세요.",
        )
    return False, "알 수 없는 OCR 엔진"


def ocr_engine_runtime_ok(engine: str) -> Tuple[bool, str]:
    eng = normalize_ocr_engine(engine)
    log_key = eng if eng else ((engine or "").strip() or "?")
    ok, msg = _ocr_engine_runtime_ok_core(eng)
    _log_ocr_runtime_failure_once(log_key, ok, msg)
    return ok, msg
