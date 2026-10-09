"""Build a signed publication tree without Minishop; host validation is opt-in."""

import argparse
import hashlib
from pathlib import Path

from package_support import (
    ROOT,
    archive,
    canonical,
    create_key,
    load_key,
    load_key_env,
    local_path,
    manifest,
    payload,
    public_key,
    version,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init-key", "build"))
    signing = parser.add_mutually_exclusive_group()
    signing.add_argument("--key", type=Path, default=ROOT / ".local/publisher/minishop-corp.key")
    signing.add_argument("--key-env", metavar="NAME", help="Environment variable with a base64 key")
    parser.add_argument("--publisher", default="minishop-corp")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release")
    parser.add_argument("--verify-host", action="store_true", help="Validate with local Minishop")
    args = parser.parse_args()
    if args.action == "init-key":
        if args.key_env:
            parser.error("init-key requires --key, not --key-env")
        create_key(args.key)
        _, fingerprint = public_key(load_key(args.key))
        print(f"Created private key: {local_path(args.key).relative_to(ROOT)}")
        print(f"Publisher fingerprint: {fingerprint}")
        return

    try:
        key = load_key_env(args.key_env) if args.key_env else load_key(args.key)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    metadata = manifest(args.publisher)
    body = archive(key, metadata, payload())
    digest = hashlib.sha256(body).hexdigest()
    if args.verify_host:
        # Only local development uses the host validator. CI never imports Core.
        from bot.plugins.packages import inspect_archive

        candidate = inspect_archive(ROOT / ".local/release-validation", body)
        assert candidate.digest == digest
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
                "sha256": digest,
                "version": version(),
            }
        )
        + b"\n"
    )
    encoded, fingerprint = public_key(key)
    (output / "publisher.pub").write_text(encoded + "\n", encoding="ascii")
    (output / "SHA256SUMS").write_text(f"{digest}  {artifact}\n", encoding="ascii")
    print(f"Built and signed: {archive_path.relative_to(ROOT)}")
    if args.verify_host:
        print("Verified with local Minishop")
    print(f"SHA-256: {digest}")
    print(f"Publisher fingerprint: {fingerprint}")


if __name__ == "__main__":
    main()
