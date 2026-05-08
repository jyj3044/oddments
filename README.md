# Alert (cyj)

Windows에서 **특정 창 화면을 캡처**하고, **키워드 OCR**과 **템플릿 이미지 매칭**으로 이벤트를 감지하면 **알림음**을 재생하는 데스크톱 앱입니다.  
미리보기·설정은 **Tkinter** GUI로 제공합니다.

- **OCR 엔진**: Tesseract, EasyOCR, RapidOCR 중 UI에서 선택(복수 선택 가능).  
  한글 인식 보조를 위해 RapidOCR 사용 시 ONNX·사전 파일을 최초에 자동으로 내려받을 수 있습니다.
- **실행 파일 이름·창 제목**: 기본 `cyj` (`main.py`의 `APP_NAME`).
- **설정 저장**: `alert_settings.json` (소스 실행 시 프로젝트 루트, **exe 실행 시 exe와 같은 폴더**).

---

## 필요 환경

- **OS**: Windows (캡처·알림은 Win32 기준으로 동작)
- **Python**: 3.10 이상 권장 (개발 시 3.12 등에서 검증)

---

## 저장소를 처음 받았을 때 (개발 실행)

### 1) 가상환경 만들기 (권장)

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
```

### 2) Python 패키지 설치

**일반 개발·실행용** (EasyOCR 포함, 용량 큼):

```powershell
pip install -r requirements.txt
```

`requirements.txt`에 포함되는 주요 항목은 다음과 같습니다.

- `mss`, `numpy`, `opencv-python`, `Pillow`, `setproctitle`, `pytesseract`
- `easyocr`, `rapidocr-onnxruntime` (OCR 엔진)

### 3) Tesseract 본체 (Tesseract 엔진을 쓸 때만)

`pytesseract`는 Python 래퍼이며, **Tesseract 실행 파일은 별도 설치**가 필요합니다.

- 설치 가이드: [UB-Mannheim Tesseract for Windows](https://github.com/UB-Mannheim/tesseract/wiki)
- 기본적으로 `C:\Program Files\Tesseract-OCR\tesseract.exe` 가 있으면 앱에서 자동으로 가리킵니다. 없으면 **PATH**에 `tesseract`가 잡혀 있어야 합니다.

### 4) 실행

```powershell
python main.py
```

#### macOS (Apple Silicon) 참고

- 터미널에서 보통은 `python3` / `python3 -m pip` 를 쓰는 것이 안전합니다 (`python` 이 없거나 다른 바이너리를 가리킬 수 있음).
- 터미널이 **Rosetta(x86_64)** 로 실행 중인데, 패키지는 **arm64** 휠로 설치된 경우 NumPy/OpenCV 로딩 시  
  `incompatible architecture (have 'arm64', need 'x86_64')` 가 납니다. **로제타를 끈 터미널**을 쓰거나 아래처럼 arm64 로 맞춥니다.

```zsh
arch -arm64 python3 -m pip install -r requirements.txt
arch -arm64 python3 main.py
```

- **「프로세스(창) 지정」**으로 송출할 때는 **화면 기록** 권한이 필요합니다.  
  시스템 설정 → 개인 정보 보호 및 보안 → **화면 및 시스템 오디오 녹음**(또는 화면 기록)에서 **Terminal·Cursor·Python** 등 실제로 앱을 실행한 프로그램을 켜 주세요. 권한이 없으면 창 목록은 보여도 송출 화면이 비거나 상태줄에 캡처 오류가 표시됩니다.
- **RapidOCR 한글 모델·사전 자동 다운로드**가 `SSL: CERTIFICATE_VERIFY_FAILED` 로 실패하면: `python3 -m pip install -U certifi` 후 다시 실행하세요(`requirements.txt`에 포함됨). Python.org 설치본이면 **`/Applications/Python 3.x/` 안의 `Install Certificates.command`** 를 한 번 실행하는 방법도 있습니다. 수동 설치 시에는 아래 두 파일을 `~/.cache/프로세스명/rapidocr_korean/` 에 넣습니다.  
  - [korean_mobile_v2.0_rec_infer.onnx](https://huggingface.co/SWHL/RapidOCR/resolve/main/PP-OCRv1/korean_mobile_v2.0_rec_infer.onnx)  
  - [korean_dict.txt](https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/ppocr/utils/dict/korean_dict.txt)

---

## 실행 파일(exe) 만들기

### 권장: 가벼운 exe (EasyOCR·PyTorch 제외)

전역 Python에 `easyocr`가 있으면 PyInstaller가 **torch까지 끌어와 exe가 수백 MB**가 될 수 있으므로, **전용 venv `.venv-build`**에서 빌드하는 것을 권장합니다.

**한 번에 실행 (권장)** — 프로젝트 루트에서 CMD 또는 탐색기에서 더블클릭:

```bat
build_exe_with_venv.bat
```

이 스크립트는 다음을 순서대로 수행합니다.

1. `python -m venv .venv-build` (폴더가 없을 때만 생성)
2. `pip` 업그레이드
3. `pip install -r requirements-build.txt`
4. `pyinstaller --noconfirm build_exe.spec`  
   (내부적으로 `.venv-build\Scripts\python.exe -m PyInstaller` 사용)

**수동으로 동일 작업을 하려면** (PowerShell/CMD, 프로젝트 루트 기준):

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements-build.txt
pyinstaller --noconfirm build_exe.spec
```

(`activate` 대신 `activate.bat` — PowerShell에서 실행 정책 경고를 피하려면 CMD에서 실행하거나 `activate.bat` 사용.)

**이미 같은 venv를 쓰는 경우** — venv를 활성화한 뒤 설치·빌드만:

```powershell
build_exe.bat
```

또는:

```powershell
pyinstaller --noconfirm build_exe.spec
```

완료 후 산출물:

- **`dist\cyj\` 폴더 전체** + 그 안의 `cyj.exe` (기본). 단일 exe가 아니라 **폴더째** 배포·실행합니다. RapidOCR/ONNX는 이 방식에서 훨씬 잘 동작합니다.
- 예전처럼 **파일 하나만** 쓰려면 `build_exe.spec` 맨 위 `ONEFILE = True` 로 바꿔 빌드하세요 (같은 PC에서 `python main.py`는 되는데 exe만 깨질 때는 `False` 권장).

### macOS에서 앱 번들(.app) 만들기

Windows의 `exe`와 같이 **PyInstaller**로 묶습니다. 산출물은 보통 **`dist/cyj/`** 폴더 안의 **`cyj`** 실행 파일(PyInstaller 버전에 따라 **`dist/cyj.app`** 인 경우도 있으니 `dist/` 를 확인하세요).

mac 빌드는 **`console=True`** 로 두어, 터미널에서 `./dist/cyj/cyj` 를 실행할 때 **로그·오류가 터미널에 보이게** 합니다. Dock에만 아이콘이 있고 창이 늦게 뜨면 **첫 실행 시 ONNX/OpenCV 로딩에 수십 초** 걸릴 수 있습니다. 문제가 있으면 같은 폴더의 **`cyj_startup_log.txt`**, **`cyj_fatal_error.txt`**, **`pyi_rthook_onnx_error.txt`** 를 확인하세요.

```zsh
python3 -m venv .venv-build
source .venv-build/bin/activate
python3 -m pip install --upgrade pip
pip install -r requirements-build.txt
./build_exe.sh
# 또는: python3 -m PyInstaller --noconfirm build_exe.spec
```

- **Apple Silicon**: Rosetta 터미널이면 x86_64 휠이 깔릴 수 있으므로, 가능하면 **arm64 터미널**에서 `arch -arm64 python3 -m venv .venv-build` 등으로 맞춥니다.
- **코드 서명 없음**: 다른 Mac에 복사했을 때 게이트키퍼가 막으면, **시스템 설정 → 개인 정보 보호 및 보안**에서 “확인 없이 열기”, 또는 **우클릭 → 열기**로 첫 실행을 허용합니다. 배포용이면 Apple Developer 로 **notarize** 하는 것이 정석입니다.
- **Tesseract**: 번들에 포함되지 않습니다. `brew install tesseract` 등으로 본체를 두거나 PATH에 잡아 두어야 합니다.
- **한글 RapidOCR 자원**: 최초 실행 시 `~/.cache/프로그램명/rapidocr_korean/` 로 내려받습니다(네트워크·SSL 참고는 위 macOS 절).

### exe 배포 시 참고

- **Tesseract**: exe에 포함되지 않습니다. 사용 PC에 Tesseract 설치 또는 PATH 설정이 필요합니다.
- **기본 OCR**: exe는 기본으로 **RapidOCR**을 쓰도록 되어 있으며, RapidOCR 관련 파일은 spec에서 번들됩니다.
- **EasyOCR를 exe에 넣으려면**: `build_exe.spec` 에서 `INCLUDE_EASYOCR = True`, venv에 `pip install easyocr`, `hiddenimports.append("easyocr")` 주석 해제 후 빌드 (용량·빌드 시간 크게 증가, RapidOCR과 DLL 충돌 가능).
- **RapidOCR / `onnxruntime_pybind11_state` DLL 초기화 실패 (`python main.py` 는 되는데 exe만 안 될 때)**:
  - PyInstaller가 **`import easyocr` 를 따라가며 torch 전체를 번들**에 넣으면, torch·onnxruntime OpenMP DLL이 겹쳐 exe에서만 깨질 수 있습니다. 기본 spec은 **`INCLUDE_EASYOCR = False`** 로 **easyocr·torch를 제외**합니다. **최신 spec으로 다시 빌드**한 뒤 `dist\cyj\_internal` 안에 **`torch` 폴더가 없는지** 확인하세요.
  - 그 외: `KMP_DUPLICATE_LIB_OK`, `_internal` 안 **DLL이 있는 모든 폴더**를 `add_dll_directory`에 등록(`bootstrap_onnx`), cv2 전 `onnxruntime` 선로드, `collect_all("onnxruntime")`, **onedir** 배포.
  - 여전히 실패 시 `dist\cyj\` 옆에 생기는 **`pyi_rthook_onnx_error.txt`**(rthook 단계 예외) 내용을 확인하세요.
  - **`onnxruntime` 버전**: **Python 3.12 이하(Windows exe 권장)** 에서는 `requirements-runtime.txt` 가 **1.20.1** 을 고정합니다(1.22+ 는 Windows 번들·VC 조합에서 초기화 실패 보고). **Python 3.13+**(맥·3.14 등)에서는 1.20.1 휠이 없어 **1.24.x** 를 쓰도록 분기해 두었습니다. 재현 가능한 Windows 빌드는 **3.12 venv** 권장입니다.

---

## 관련 파일

| 파일 | 설명 |
|------|------|
| `requirements.txt` | 개발 실행용 전체 의존성 |
| `requirements-runtime.txt` | exe 번들용 최소 의존성 (EasyOCR 제외) |
| `requirements-build.txt` | exe 빌드 시 pip 설치 목록 |
| `build_exe.spec` | PyInstaller 설정 |
| `build_exe_with_venv.bat` | Windows: venv 생성·pip·PyInstaller까지 한 번에 |
| `build_exe.bat` | Windows: 현재 환경에서 pip 설치 후 PyInstaller만 |
| `build_exe.sh` | macOS 빌드 스크립트 |

---

## 라이선스

저장소에 별도 라이선스 파일이 없다면, 프로젝트 관리자에게 문의하세요.
