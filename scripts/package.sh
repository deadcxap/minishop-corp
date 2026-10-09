#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
if [[ "${1:-}" == "build" ]]; then
  bash scripts/check-minishop.sh
  bash scripts/node.sh npm run check
  bash scripts/node.sh npm run build
  set -- "$@" --verify-host
fi
.venv/bin/python scripts/build-package.py "$@"
