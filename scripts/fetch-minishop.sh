#!/usr/bin/env bash
# Optional pinned checkout, created only inside the plugin repository.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/env.sh"
destination="$CORP_ROOT/.local/minishop"
repository="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["repository"])')"
revision="$(python3 -c 'import json; print(json.load(open("dev/minishop.json"))["revision"])')"
if [[ -e "$destination" ]]; then
  echo "Checkout already exists: $destination. Verify it with scripts/setup.sh." >&2
  exit 1
fi
git init "$destination"
git -C "$destination" remote add origin "$repository"
git -C "$destination" fetch --depth=1 origin "$revision"
git -C "$destination" checkout --detach FETCH_HEAD
