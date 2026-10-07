#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
export CORP_UID="$(id -u)" CORP_GID="$(id -g)"
action="${1:-up}"
shift || true
case "$action" in
  up)
    expected="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["revision"])')"
    [[ "$(git -C "$MINISHOP_SOURCE" rev-parse HEAD)" == "$expected" ]]
    [[ -z "$(git -C "$MINISHOP_SOURCE" status --porcelain)" ]]
    bash scripts/node.sh npm run build
    .venv/bin/python scripts/build-dev-package.py
    mkdir -p .local/runtime/data .local/runtime/tmp .local/build-context
    cp "$MINISHOP_SOURCE/backend/requirements.txt" .local/build-context/requirements.txt
    for directory in backend locales; do
      [[ -L ".local/runtime/$directory" ]] || ln -s "/minishop/$directory" ".local/runtime/$directory"
    done
    # The native launcher reads the bundled archive at process startup.
    docker compose -f dev/compose.yaml up -d --build --force-recreate "$@" backend worker preview
    ;;
  check)
    docker compose -f dev/compose.yaml exec -T backend python /corp-dev/smoke.py
    ;;
  logs|ps|stop|down)
    docker compose -f dev/compose.yaml "$action" "$@"
    ;;
  *) echo "Usage: bash scripts/stand.sh {up|check|logs|ps|stop|down}" >&2; exit 2 ;;
esac
