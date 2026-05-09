#!/usr/bin/env bash
# 프로젝트 루트에서 macOS .app 번들 빌드 (PyInstaller)
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/2] pip install (requirements-build.txt)..."
python3 -m pip install -U pip
python3 -m pip install -r requirements-build.txt

echo "[2/2] PyInstaller..."
python3 -m PyInstaller --noconfirm build_exe.spec

echo "완료: dist/oddments.app (또는 dist/oddments/) — dist 폴더를 확인하세요."
