#!/usr/bin/env bash
# Stable release checkout, created only inside the plugin repository.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
destination="$CORP_ROOT/.local/minishop"
repository="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["repository"])')"
revision="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["revision"])')"
release="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["release"])')"
if [[ -e "$destination" ]]; then
  echo "Checkout already exists: $destination. Verify it with scripts/setup.sh." >&2
  exit 1
fi
git init "$destination"
git -C "$destination" remote add origin "$repository"
git -C "$destination" fetch --depth=1 origin "refs/tags/$release:refs/tags/$release"
if [[ "$(git -C "$destination" rev-parse 'FETCH_HEAD^{commit}')" != "$revision" ]]; then
  echo "Release $release no longer matches pinned revision $revision; checkout was not activated." >&2
  exit 1
fi
git -C "$destination" checkout --detach FETCH_HEAD
MINISHOP_SOURCE="$destination" bash scripts/check-minishop.sh
