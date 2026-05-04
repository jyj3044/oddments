# -*- mode: python ; coding: utf-8 -*-
# 프로젝트 루트에서: pyinstaller --noconfirm build_exe.spec
#
# ONEFILE=False (기본): dist/cyj/ 폴더 + cyj.exe — onnxruntime·OpenCV DLL 로드가 안정적.
# ONEFILE=True: dist/cyj.exe 단일 파일 — _MEIPASS 압축 해제 경로에서 ONNX 초기화 실패가 잦음.
ONEFILE = False

# ocr_backends.py 안에 `import easyocr` 가 있어 PyInstaller가 easyocr→torch 까지 끌어올 수 있음.
# torch DLL 과 onnxruntime OpenMP 가 겹치면 exe 에서만 "onnxruntime_pybind11_state 초기화 실패" 가 난다.
# EasyOCR 을 exe 에 넣을 때만 True 로 두고, 아래 excludes 에서 torch 계열을 빼 준다.
INCLUDE_EASYOCR = False

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

ROOT = Path(SPEC).resolve().parent

block_cipher = None

# RapidOCR: ONNX·yaml·서브모듈 전부 (미포함 시 exe 에서 import 실패)
_rapid_datas, _rapid_bins, _rapid_hidden = collect_all("rapidocr_onnxruntime")

datas = list(_rapid_datas)
binaries = list(_rapid_bins)
# onnxruntime: collect_dynamic_libs 만으로는 일부 보조 DLL 누락 시 pyd 초기화 실패할 수 있음
try:
    _onnx_datas, _onnx_bins, _onnx_hidden = collect_all("onnxruntime")
    datas += list(_onnx_datas)
    binaries += list(_onnx_bins)
except Exception:
    _onnx_hidden = ()
try:
    binaries += collect_dynamic_libs("cv2")
except Exception:
    pass
try:
    binaries += collect_dynamic_libs("onnxruntime")
except Exception:
    pass

_base_hidden = [
    "PIL._tkinter_finder",
    "app_platform",
    "app_platform.audio",
    "app_platform.host",
    "app_platform.models",
    "detection",
    "detection.common",
    "detection.keywords",
    "detection.pipeline",
    "detection.ocr_backends",
    "detection.ocr_diag",
    "detection.templates",
    "detection.overlay_store",
    "bootstrap_onnx",
    "setproctitle",
    "mss",
    "cv2",
    "numpy",
    "pytesseract",
    "rapidocr_onnxruntime",
    "onnxruntime",
]
# windows_capture 는 mac 에서 import 시 즉시 실패하므로 플랫폼별로만 넣는다.
_platform_hidden: list[str] = []
if sys.platform == "win32":
    _platform_hidden = [
        "windows_capture",
        "tesseract_win_console",
        "arduino_serial_bridge",
        "serial",
        "serial.serialutil",
        "serial.tools",
        "serial.tools.list_ports",
        "pynput.keyboard._win32",
        "pynput.mouse._win32",
    ]
elif sys.platform == "darwin":
    _platform_hidden = ["darwin_capture"]

hiddenimports = list(
    dict.fromkeys(
        _base_hidden + _platform_hidden + list(_rapid_hidden) + list(_onnx_hidden)
    )
)
# EasyOCR 을 exe 에 넣으려면: INCLUDE_EASYOCR = True, venv 에 easyocr 설치 후 아래 줄 주석 해제
# hiddenimports.append("easyocr")

# 앱에서 PaddleOCR 미사용 — 전역에 설치돼 있어도 번들에 넣지 않음
_paddle_excludes = (
    "paddleocr",
    "paddle",
    "paddlepaddle",
    "paddlex",
    "paddlenlp",
)

_torch_easyocr_excludes = (
    "easyocr",
    "torch",
    "torchvision",
    "torchaudio",
    "functorch",
    "torchgen",
    "tensorboard",
    "tensorflow",
    "jax",
    "jaxlib",
)

_all_excludes = list(_paddle_excludes)
if not INCLUDE_EASYOCR:
    _all_excludes.extend(_torch_easyocr_excludes)
_all_excludes = list(dict.fromkeys(_all_excludes))

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[
        str(ROOT / "pyi_rthook_00_openmp.py"),
        str(ROOT / "pyi_rthook_onnx.py"),
    ],
    excludes=_all_excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# macOS: windowed(console=False)는 터미널에서 실행해도 출력이 없어 "무반응"으로 보이기 쉬움.
# 터미널 로그·stderr 로 디버깅 가능하도록 콘솔 사용 (배포용 .app 은 별도 BUNDLE/서명 검토).
_use_console = sys.platform == "darwin"

_exe_kw = dict(
    name="cyj",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=_use_console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        runtime_tmpdir=None,
        **_exe_kw,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **_exe_kw,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="cyj",
    )
