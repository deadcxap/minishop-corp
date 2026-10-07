"""Build a signed, local-only fixture for the native Minishop package loader.

This is deliberately not the S10 publisher/release pipeline. Its disposable key
is generated inside .local and must never be trusted on a production instance.
"""

import base64
import hashlib
import json
import platform
import sys
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".local" / "package"


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    key_path = OUTPUT / "development.key"
    if key_path.exists():
        key = Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())
    else:
        key = Ed25519PrivateKey.generate()
        with key_path.open("xb") as stream:
            key_path.chmod(0o600)
            stream.write(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    encoded_public = base64.b64encode(public).decode("ascii")
    (OUTPUT / "development.pub").write_text(encoded_public, encoding="ascii")
    files: dict[str, bytes] = {}
    for directory, prefix, pattern in (
        (ROOT / "backend", "backend", "*.py"),
        (ROOT / "locales", "locales", "*.json"),
        (ROOT / "frontend" / "dist", "frontend", "*"),
    ):
        for path in sorted(directory.rglob(pattern)):
            if path.is_file():
                files[f"{prefix}/{path.relative_to(directory).as_posix()}"] = path.read_bytes()
    metadata = json.loads((ROOT / "dev" / "package.json").read_text())
    pin = json.loads((ROOT / "dev" / "minishop.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = {
        **metadata,
        "schema_version": 1,
        "version": project["version"],
        "publisher": "minishop-corp-local-dev",
        "publisher_public_key": encoded_public,
        "publisher_fingerprint": hashlib.sha256(public).hexdigest(),
        "plugin_api": pin["plugin_api"],
        "core_revision": pin["revision"],
        "frontend_host_api": pin["frontend_host_api"],
        "runtime": {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "system": platform.system().lower(),
            "machine": platform.machine().lower(),
        },
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
    }
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    archive_path = OUTPUT / "minishop-corp-dev.zip"
    members = {
        "plugin.json": canonical,
        "signatures/ed25519.sig": key.sign(canonical),
        **files,
    }
    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for name, body in sorted(members.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, body)
    # Use the pinned core's validator, including its frontend asset allowlists.
    from bot.plugins.packages import inspect_archive

    candidate = inspect_archive(ROOT / ".local" / "package-validation", archive_path.read_bytes())
    assert candidate.manifest["id"] == "minishop-corp"
    print(f"Validated development package: {archive_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
