#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
bash scripts/check-minishop.sh
uv venv --python python3.12 --allow-existing .venv
uv pip install --python .venv/bin/python \
  -r "$MINISHOP_SOURCE/backend/requirements.txt" -r requirements-dev.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps -e .
bash scripts/node.sh npm ci --ignore-scripts
