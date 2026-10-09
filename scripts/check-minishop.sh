#!/usr/bin/env bash
# Verify the stable source before importing Core or starting any local workload.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
release="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["release"])')"
expected="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["revision"])')"
if ! actual="$(git -C "$MINISHOP_SOURCE" rev-parse HEAD 2>/dev/null)"; then
  echo "Minishop $release checkout missing: $MINISHOP_SOURCE. Run bash scripts/fetch-minishop.sh." >&2
  exit 1
fi
if [[ "$actual" != "$expected" ]]; then
  echo "Minishop $release requires $expected; found $actual at $MINISHOP_SOURCE." >&2
  exit 1
fi
state="$(git -C "$MINISHOP_SOURCE" status --porcelain)"
if [[ -n "$state" ]]; then
  echo "Minishop $release checkout must be clean: $MINISHOP_SOURCE." >&2
  exit 1
fi
