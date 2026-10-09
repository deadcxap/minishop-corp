#!/usr/bin/env bash
# Run in the checkout that produced the downloaded Actions artifact.
set -euo pipefail
: "${CORP_PUBLISH_BRANCH:?Expected destination branch}"
: "${CORP_PUBLISH_SHA:?Expected source commit}"
git check-ref-format "refs/heads/$CORP_PUBLISH_BRANCH"
[[ "$(git rev-parse HEAD)" == "$CORP_PUBLISH_SHA" ]] || {
  echo "Checkout does not match the package source commit." >&2
  exit 1
}
remote_sha="$(git ls-remote --exit-code origin "refs/heads/$CORP_PUBLISH_BRANCH" | cut -f1)"
if [[ "$remote_sha" != "$CORP_PUBLISH_SHA" ]]; then
  echo "Branch has moved; the newer workflow run will publish its package."
  exit 0
fi

artifact="$(python3 - <<'PY'
import hashlib
import json
import re
from pathlib import Path

index = json.loads(Path("minishop-plugin.json").read_text())
version = index["version"]
artifact = index["artifact"]
if (
    index["schema_version"] != 1
    or not isinstance(version, str)
    or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
    or artifact != f"packages/minishop-corp-{version}.zip"
):
    raise SystemExit("Invalid generated package index")
digest = hashlib.sha256(Path(artifact).read_bytes()).hexdigest()
if index["sha256"] != digest or Path("SHA256SUMS").read_text() != f"{digest}  {artifact}\n":
    raise SystemExit("Generated package checksum mismatch")
print(artifact)
PY
)"
git add -- minishop-plugin.json "$artifact" publisher.pub SHA256SUMS
if git diff --cached --quiet -- minishop-plugin.json "$artifact" publisher.pub SHA256SUMS; then
  echo "Published package is already up to date."
  exit 0
fi
git -c user.name='github-actions[bot]' \
  -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
  commit --only -m 'build: publish signed plugin package' \
  -- minishop-plugin.json "$artifact" publisher.pub SHA256SUMS
# A normal push also rejects a branch change after the check above. Never force-push.
git push origin "HEAD:refs/heads/$CORP_PUBLISH_BRANCH"
