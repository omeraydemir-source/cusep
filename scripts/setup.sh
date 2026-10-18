#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir -r requirements.lock
.venv/bin/python scripts/prepare_ocr.py
.venv/bin/python -m cusep init
