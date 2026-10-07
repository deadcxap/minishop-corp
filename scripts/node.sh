#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
image="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["node_image"])')"
docker run --rm --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$CORP_ROOT,dst=/workspace" --workdir /workspace \
  --env npm_config_cache=/workspace/.local/npm --env TMPDIR=/workspace/.local/tmp \
  "$image" "$@"
