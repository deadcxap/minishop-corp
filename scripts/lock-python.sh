#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
bash scripts/check-minishop.sh
uv pip compile requirements-dev.in --constraint "$MINISHOP_SOURCE/backend/requirements.txt" \
  --python .venv/bin/python --generate-hashes --no-annotate \
  --custom-compile-command "bash scripts/lock-python.sh" --output-file requirements-dev.lock
