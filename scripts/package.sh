#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
if [[ "${1:-}" == "build" ]]; then
  expected="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["revision"])')"
  [[ "$(git -C "$MINISHOP_SOURCE" rev-parse HEAD)" == "$expected" ]]
  [[ -z "$(GIT_OPTIONAL_LOCKS=0 git -C "$MINISHOP_SOURCE" status --porcelain)" ]]
  bash scripts/node.sh npm run check
  bash scripts/node.sh npm run build
  set -- "$@" --verify-host
fi
.venv/bin/python scripts/build-package.py "$@"
