"""Create a publisher key explicitly, or build a local publication tree."""

import argparse
import hashlib
from pathlib import Path

from package_support import (
    ROOT,
    archive,
    canonical,
    create_key,
    load_key,
    local_path,
    manifest,
    payload,
    public_key,
    version,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init-key", "build"))
    parser.add_argument("--key", type=Path, default=ROOT / ".local/publisher/minishop-corp.key")
    parser.add_argument("--publisher", default="minishop-corp")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release")
    args = parser.parse_args()
    if args.action == "init-key":
        create_key(args.key)
        _, fingerprint = public_key(load_key(args.key))
        print(f"Created private key: {local_path(args.key).relative_to(ROOT)}")
        print(f"Publisher fingerprint: {fingerprint}")
        return

    key = load_key(args.key)
    metadata = manifest(args.publisher)
    body = archive(key, metadata, payload())
    # Validate using the real host before making any output visible.
    from bot.plugins.packages import inspect_archive

    candidate = inspect_archive(ROOT / ".local/release-validation", body)
    assert candidate.digest == hashlib.sha256(body).hexdigest()
    output = local_path(args.output)
    artifact = f"packages/minishop-corp-{version()}.zip"
    archive_path = local_path(output / artifact)
    if archive_path.exists() and archive_path.read_bytes() != body:
        raise ValueError("Output version already has different bytes; use a fresh output directory")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(body)
    (output / "minishop-plugin.json").write_bytes(
        canonical(
            {
                "schema_version": 1,
                "artifact": artifact,
                "sha256": candidate.digest,
                "version": version(),
            }
        )
        + b"\n"
    )
    encoded, fingerprint = public_key(key)
    (output / "publisher.pub").write_text(encoded + "\n", encoding="ascii")
    (output / "SHA256SUMS").write_text(f"{candidate.digest}  {artifact}\n", encoding="ascii")
    print(f"Built and verified: {archive_path.relative_to(ROOT)}")
    print(f"SHA-256: {candidate.digest}")
    print(f"Publisher fingerprint: {fingerprint}")


if __name__ == "__main__":
    main()
