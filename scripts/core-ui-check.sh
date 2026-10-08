#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
export CORP_UID="$(id -u)" CORP_GID="$(id -g)"
# Copy tracked build inputs, never install dependencies or generate assets in the source repository.
.venv/bin/python - <<'PY'
import os
import shutil
import subprocess
from pathlib import Path

source = Path(os.environ['MINISHOP_SOURCE']).resolve()
target = Path('.local/core-ui').resolve()
names = subprocess.check_output(
    ['git', '-C', str(source), 'ls-files', '-z', 'frontend', 'backend/bot/app/web/templates'],
    env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
).decode().split('\0')
for name in filter(None, names):
    destination = target / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / name, destination)
PY
node_image="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["node_image"])')"
browser_image="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["browser_image"])')"
docker run --rm --user "$CORP_UID:$CORP_GID" \
  --mount "type=bind,src=$CORP_ROOT,dst=/workspace" --workdir /workspace/.local/core-ui/frontend \
  --env npm_config_cache=/workspace/.local/npm-core-ui --env TMPDIR=/workspace/.local/tmp \
  "$node_image" bash -c 'npm ci --no-audit --no-fund && npm run build'
# Ensure the plugin stand and its synthetic identities exist before loading the full shell.
bash scripts/stand.sh up
docker compose -f dev/compose.yaml -f dev/core-ui.yaml up -d --force-recreate backend
bash scripts/stand.sh check
docker compose -f dev/compose.yaml exec -T backend python /corp-dev/core_ui_sessions.py
docker run --rm --user "$CORP_UID:$CORP_GID" --network minishop-corp-dev_default \
  --mount "type=bind,src=$CORP_ROOT,dst=/workspace" --workdir /workspace \
  --env TMPDIR=/workspace/.local/tmp --env CORP_CORE_URL=http://backend:8081 \
  "$browser_image" node tests/browser/core-ui.mjs
