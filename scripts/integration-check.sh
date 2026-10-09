#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
export CORP_UID="$(id -u)" CORP_GID="$(id -g)"
bash scripts/check-minishop.sh
mkdir -p .local/build-context .local/integration/tmp
cp "$MINISHOP_SOURCE/backend/requirements.txt" .local/build-context/requirements.txt
git -C "$MINISHOP_SOURCE" rev-parse HEAD > .local/build-context/core-revision
cp requirements-dev.lock .local/build-context/requirements-dev.lock
docker compose -f dev/compose.yaml up -d --wait postgres
docker compose -f dev/compose.yaml build integration
docker compose -f dev/compose.yaml run --rm --no-deps integration \
  python -m pytest tests/integration -q "$@"
