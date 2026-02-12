#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${BASE_URL:-}" ]]; then
  echo "Set BASE_URL, e.g. BASE_URL=https://tbm.example.com"
  exit 1
fi

echo "Checking health endpoint..."
curl -sSf "${BASE_URL%/}/health" >/dev/null
echo "OK: /health"
