# 개발 실행 및 빌드

## 저장소를 처음 받았을 때 (개발 실행)

### 1) 가상환경 만들기 (권장)

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip
```

### 2) Python 패키지 설치

프로젝트 루트에서:

```powershell
pip install -r requirements.txt
```

`requirements.txt`에는 캡처·GUI·**RapidOCR(onnxruntime)**·웹 송출 등 실행에 필요한 패키지가 정리되어 있습니다.

### 3) 실행

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

### 권장: 전용 빌드 가상환경 (`.venv-build`)

전역 Python에 불필요한 패키지가 깔려 있으면 PyInstaller가 **의존성을 과하게 끌어와** exe가 비대해지거나 DLL 충돌이 날 수 있습니다. **`requirements-build.txt`** 만 설치한 **깨끗한 venv**에서 빌드하는 것을 권장합니다.

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

- **`dist\oddments\` 폴더 전체** + 그 안의 `oddments.exe` (기본). 단일 exe가 아니라 **폴더째** 배포·실행합니다. RapidOCR/ONNX는 이 방식에서 훨씬 잘 동작합니다.
- 예전처럼 **파일 하나만** 쓰려면 `build_exe.spec` 맨 위 `ONEFILE = True` 로 바꿔 빌드하세요 (같은 PC에서 `python main.py`는 되는데 exe만 깨질 때는 `False` 권장).

**번들에 넣지 않는 것:** `build_exe.spec` 의 `excludes`에 **pytesseract / easyocr / torch** 등이 들어 있어, OCR은 **RapidOCR·onnxruntime** 만 exe에 포함되는 구성입니다. 빌드 후 `dist\oddments\_internal` 안에 **`torch` 폴더가 생겼다면** 빌드 환경에 torch 계열이 끌려온 것이므로 venv를 비우고 다시 빌드해 보세요.

### macOS에서 앱 번들 만들기

Windows의 `exe`와 같이 **PyInstaller**로 묶습니다. 산출물은 보통 **`dist/oddments/`** 폴더 안의 **`oddments`** 실행 파일(PyInstaller 버전에 따라 **`dist/oddments.app`** 인 경우도 있으니 `dist/` 를 확인하세요).

mac 빌드는 **`console=True`** 로 두어, 터미널에서 `./dist/oddments/oddments` 를 실행할 때 **로그·오류가 터미널에 보이게** 합니다. Dock에만 아이콘이 있고 창이 늦게 뜨면 **첫 실행 시 ONNX/OpenCV 로딩에 수십 초** 걸릴 수 있습니다. 문제가 있으면 같은 폴더의 **`oddments_startup_log.txt`**, **`oddments_fatal_error.txt`**, **`pyi_rthook_onnx_error.txt`** 를 확인하세요.

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
- **한글 RapidOCR 자원**: 최초 실행 시 `~/.cache/프로그램명/rapidocr_korean/` 로 내려받습니다(네트워크·SSL 참고는 위 macOS 절).

### exe 배포 시 참고

- **Flet UI**: `requirements-build.txt` → `flet`·**`flet-desktop`**(동일 메이저 버전)이 포함됩니다. PyInstaller spec 에서 `collect_all("flet")`·`collect_all("flet_desktop")` 와 프로젝트 **`assets/`** 트리를 번들합니다.
- **Flet 데스크톱 클라이언트**: 설치된 wheel 에 `flet-windows.zip` 등이 없으면 **최초 실행 시** GitHub 에서 Flet 뷰어를 받아 `~/.flet/client/` 에 풉니다(방화벽·오프라인이면 실패할 수 있음).
- **OCR**: exe는 **RapidOCR** 관련 파일을 spec에서 번들합니다.
- **RapidOCR / `onnxruntime_pybind11_state` DLL 초기화 실패 (`python main.py` 는 되는데 exe만 안 될 때)**:
  - `_internal` 안 **DLL이 있는 모든 폴더**를 `add_dll_directory`에 등록(`app_platform.bootstrap_onnx`), cv2 전 `onnxruntime` 선로드, `collect_all("onnxruntime")`, **onedir** 배포.
  - 여전히 실패 시 `dist\oddments\` 옆에 생기는 **`pyi_rthook_onnx_error.txt`**(rthook 단계 예외) 내용을 확인하세요.
  - **`onnxruntime` 버전**: **Python 3.12 이하(Windows exe 권장)** 에서는 `requirements-runtime.txt` 가 **1.20.1** 을 고정합니다(1.22+ 는 Windows 번들·VC 조합에서 초기화 실패 보고). **Python 3.13+**(맥·3.14 등)에서는 1.20.1 휠이 없어 **1.24.x** 를 쓰도록 분기해 두었습니다. 재현 가능한 Windows 빌드는 **3.12 venv** 권장입니다.

---

## 관련 파일

| 파일 | 설명 |
|------|------|
| `requirements.txt` | 개발 실행용 의존성 |
| `requirements-runtime.txt` | exe 번들용 최소 의존성 (RapidOCR·웹 송출 등) |
| `requirements-build.txt` | exe 빌드 시 pip 설치 목록 |
| `build_exe.spec` | PyInstaller 설정 |
| `build_exe_with_venv.bat` | Windows: venv 생성·pip·PyInstaller까지 한 번에 |
| `build_exe.bat` | Windows: 현재 환경에서 pip 설치 후 PyInstaller만 |
| `build_exe.sh` | macOS 빌드 스크립트 |
