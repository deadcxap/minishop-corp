#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
expected="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["revision"])')"
actual="$(git -C "$MINISHOP_SOURCE" rev-parse HEAD)"
if [[ "$actual" != "$expected" ]] || [[ -n "$(git -C "$MINISHOP_SOURCE" status --porcelain)" ]]; then
  echo "Minishop must be clean and checked out at $expected (see dev/minishop.json)." >&2
  exit 1
fi
uv venv --python python3.12 --allow-existing .venv
uv pip install --python .venv/bin/python \
  -r "$MINISHOP_SOURCE/backend/requirements.txt" -r requirements-dev.lock
uv pip install --python .venv/bin/python --no-build-isolation --no-deps -e .
bash scripts/node.sh npm ci --ignore-scripts
