#!/usr/bin/env bash
set -euo pipefail

if [[ ! -d ".venv" ]]; then
  echo "Missing .venv. Create it first (python -m venv .venv)."
  exit 1
fi

source .venv/bin/activate

python -m pip install -r requirements-dev.txt
python -m playwright install chromium
PYTHONPATH=. pytest -q -k datetime_box_playwright
