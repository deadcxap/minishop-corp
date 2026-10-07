#!/usr/bin/env bash
# Source from scripts executed within this repository.
set -euo pipefail
CORP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$CORP_ROOT"
export MINISHOP_SOURCE="${MINISHOP_SOURCE:-$CORP_ROOT/../remnawave-minishop}"
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR="$CORP_ROOT/.local/tmp"
export UV_CACHE_DIR="$CORP_ROOT/.local/uv"
export BUILDX_CONFIG="$CORP_ROOT/.local/buildx"
export RUFF_CACHE_DIR="$CORP_ROOT/.local/ruff"
export PYTHONPATH="$MINISHOP_SOURCE/backend:$CORP_ROOT/backend"
export MYPYPATH="$MINISHOP_SOURCE/backend"
mkdir -p "$TMPDIR" "$UV_CACHE_DIR"
