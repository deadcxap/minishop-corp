#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
destination="$CORP_ROOT/.local/kiro-wheel"
repository="$(python3 -c 'import json; print(json.load(open("dev/kiro-wheel.json"))["repository"])')"
revision="$(python3 -c 'import json; print(json.load(open("dev/kiro-wheel.json"))["revision"])')"
if [[ -e "$destination" ]]; then
  echo "Checkout already exists: $destination. Verify its pinned revision before use." >&2
  exit 1
fi
git init "$destination"
git -C "$destination" remote add origin "$repository"
git -C "$destination" fetch --depth=1 origin "$revision"
git -C "$destination" checkout --detach FETCH_HEAD
