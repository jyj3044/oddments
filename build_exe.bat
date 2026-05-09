@echo off
REM UTF-8 console (Korean messages avoided here for reliable cmd parsing)
chcp 65001 >nul
cd /d "%~dp0"

echo Tip: use a venv ^(python -m venv .venv ^& .venv\Scripts\activate^) for a smaller exe.
echo.

echo [1/2] pip install ^(requirements-build.txt^)...
python -m pip install -r requirements-build.txt
if errorlevel 1 goto :fail

echo [2/2] PyInstaller...
pyinstaller --noconfirm build_exe.spec
if errorlevel 1 goto :fail

echo.
echo Done: dist\oddments\oddments.exe  ^(whole dist\oddments folder — ONNX needs this layout^)
echo - Single-file exe: set ONEFILE = True in build_exe.spec then rebuild ^(may break RapidOCR^).
echo - Install Tesseract on the PC ^(PATH or default install path^).
echo - EasyOCR is not in requirements-runtime. For EasyOCR: pip install easyocr, then uncomment easyocr in build_exe.spec.
echo.
pause
goto :eof

:fail
echo.
echo [FAIL] Check errors above. Ensure python and pip are on PATH.
pause
exit /b 1
