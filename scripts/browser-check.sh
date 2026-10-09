#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
bash scripts/check-minishop.sh
image="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["browser_image"])')"
mkdir -p .local/screenshots
docker run --rm --user "$(id -u):$(id -g)" --network minishop-corp-dev_default \
  --mount "type=bind,src=$CORP_ROOT,dst=/workspace" --workdir /workspace \
  --env TMPDIR=/workspace/.local/tmp --env CORP_PREVIEW_URL=http://preview:8082 \
  "$image" node tests/browser/smoke.mjs
