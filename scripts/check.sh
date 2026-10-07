#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
.venv/bin/ruff check backend tests scripts dev
.venv/bin/ruff format --check backend tests scripts dev
.venv/bin/mypy
.venv/bin/pytest
bash scripts/node.sh npm run check
bash scripts/node.sh npm run build
bash scripts/node.sh npm test
.venv/bin/python -m build --wheel --no-isolation --outdir .local/dist
.venv/bin/python scripts/check-wheel.py
git diff --check
