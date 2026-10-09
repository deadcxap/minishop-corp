#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
export CORP_UID="$(id -u)" CORP_GID="$(id -g)"
bash scripts/check-minishop.sh
wheel_revision="$(python3 -c 'import json; print(json.load(open("dev/kiro-wheel.json"))["revision"])')"
[[ "$(git -C "$KIRO_WHEEL_SOURCE" rev-parse HEAD)" == "$wheel_revision" ]]
[[ -z "$(GIT_OPTIONAL_LOCKS=0 git -C "$KIRO_WHEEL_SOURCE" status --porcelain)" ]]
mkdir -p .local/build-context .local/integration/tmp
cp "$MINISHOP_SOURCE/backend/requirements.txt" .local/build-context/requirements.txt
git -C "$MINISHOP_SOURCE" rev-parse HEAD > .local/build-context/core-revision
cp requirements-dev.lock .local/build-context/requirements-dev.lock
docker compose -f dev/compose.yaml up -d --wait postgres
docker compose -f dev/compose.yaml build integration
docker compose -f dev/compose.yaml run --rm --no-deps integration \
  python -m pytest tests/integration -q "$@"
