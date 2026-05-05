@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM .venv-build 가상환경 생성 → pip 업그레이드 → requirements-build 설치 → PyInstaller
REM 기존 .venv-build 가 있으면 재사용합니다(삭제 후 다시 받으려면 폴더를 지우고 실행).
REM ---------------------------------------------------------------------------

if not exist ".venv-build\Scripts\python.exe" (
    echo [1/4] 가상환경 만들기: .venv-build
    python -m venv .venv-build
    if errorlevel 1 goto :fail
) else (
    echo [1/4] 기존 .venv-build 사용
)

set "VPY=.venv-build\Scripts\python.exe"

echo [2/4] pip 업그레이드
"%VPY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo [3/4] pip install -r requirements-build.txt
"%VPY%" -m pip install -r requirements-build.txt
if errorlevel 1 goto :fail

echo [4/4] PyInstaller build_exe.spec
"%VPY%" -m PyInstaller --noconfirm build_exe.spec
if errorlevel 1 goto :fail

echo.
echo 완료: dist\cyj\ 폴더 전체와 cyj.exe ^(onedir 배포^)
echo - 단일 exe: build_exe.spec 에서 ONEFILE = True 후 이 스크립트로 다시 빌드 ^(RapidOCR 깨질 수 있음^)
echo - Tesseract는 exe에 포함되지 않습니다. 사용 PC에 설치 또는 PATH 설정.
echo.
pause
goto :eof

:fail
echo.
echo [실패] 위 오류를 확인하세요. Python이 PATH에 있고, 3.10~3.12 권장입니다.
pause
exit /b 1
