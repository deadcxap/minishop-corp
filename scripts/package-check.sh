#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
export CORP_UID="$(id -u)" CORP_GID="$(id -g)"
export CORP_PACKAGE_RUN="$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
target="$CORP_ROOT/.local/package-check/$CORP_PACKAGE_RUN"
key="$CORP_ROOT/.local/publisher/minishop-corp.key"
[[ -f "$key" ]] || { echo "Run bash scripts/package.sh init-key first." >&2; exit 1; }
mkdir -p "$target/runtime/data" "$target/runtime/tmp" .local/build-context
bash scripts/package.sh build --key "$key" --output "$target/release"
.venv/bin/python dev/make-package-upgrade.py "$key" "$target/upgrade.zip"
cp "$MINISHOP_SOURCE/backend/requirements.txt" .local/build-context/requirements.txt
for directory in backend locales; do
  ln -s "/minishop/$directory" "$target/runtime/$directory"
done
compose=(docker compose -f dev/package-compose.yaml)
cleanup() {
  "${compose[@]}" logs --no-color > "$target/host.log" 2>&1 || true
  "${compose[@]}" down
}
trap cleanup EXIT
"${compose[@]}" up -d --build backend worker
"${compose[@]}" exec -T backend python /corp-dev/package_lifecycle.py
echo "Package lifecycle evidence: ${target#"$CORP_ROOT/"}"
