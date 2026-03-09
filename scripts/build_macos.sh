#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
APP_NAME="MultiUserGoogleMeetJoiner"

echo "Using Python: $PYTHON_BIN"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements-packaging.txt

"$PYTHON_BIN" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "$APP_NAME" \
  --collect-all selenium \
  --collect-all webdriver_manager \
  main.py

if [[ -d "dist/${APP_NAME}.app" ]]; then
  echo "Build successful: dist/${APP_NAME}.app"
else
  echo "Build output created under dist/."
fi
