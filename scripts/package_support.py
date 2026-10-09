"""Shared, deterministic ZIP format for development and publisher packages."""

import ast
import base64
import binascii
import hashlib
import io
import json
import os
import re
import tomllib
from pathlib import Path
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

ROOT = Path(__file__).resolve().parents[1]
type JSON = None | bool | int | float | str | list[JSON] | dict[str, JSON]


def local_path(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT):
        raise ValueError("Package outputs and signing keys must stay inside this repository")
    return resolved


def canonical(value: dict[str, JSON]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def json_object(path: Path) -> dict[str, JSON]:
    value: object = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    # JSON decoding is the integration boundary; values cannot contain Python objects.
    return cast(dict[str, JSON], value)


def create_key(path: Path) -> None:
    path = local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    # O_EXCL prevents silent rotation, and permissions are private from the first write.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))


def load_key(path: Path) -> Ed25519PrivateKey:
    path = local_path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError("Signing key must have permissions 0600")
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def load_key_env(name: str) -> Ed25519PrivateKey:
    """Read a base64-encoded private seed without writing it to disk or logging it."""
    encoded = os.environ.pop(name, "").strip()
    if not encoded:
        raise ValueError(f"Missing signing secret {name}; configure it in GitHub Actions secrets")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"Signing secret {name} must be base64 of a 32-byte Ed25519 key") from None
    if len(raw) != 32:
        raise ValueError(f"Signing secret {name} must be base64 of a 32-byte Ed25519 key")
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key(key: Ed25519PrivateKey) -> tuple[str, str]:
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest()


def version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    value: object = project["version"]
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        raise ValueError("Expected a stable X.Y.Z project version")
    module = ast.parse((ROOT / "backend/minishop_corp/__init__.py").read_text())
    declared = [
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    if declared != [value]:
        raise ValueError("pyproject.toml and plugin __version__ differ")
    return value


def payload() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    sources = [
        (path, path.relative_to(ROOT).as_posix()) for path in (ROOT / "backend").rglob("*.py")
    ]
    sources += [(ROOT / f"locales/{lang}.json", f"locales/{lang}.json") for lang in ("en", "ru")]
    sources += [
        (ROOT / f"frontend/dist/{audience}/index.{kind}", f"frontend/{audience}/index.{kind}")
        for audience in ("admin", "customer")
        for kind in ("js", "css")
    ]
    for path, name in sorted(sources):
        if path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents if parent != ROOT
        ):
            raise ValueError(f"Symlink payload is not allowed: {name}")
        files[name] = path.read_bytes()
    if not files.get("backend/minishop_corp/__init__.py"):
        raise ValueError("Plugin entry point is missing")
    return files


def archive(key: Ed25519PrivateKey, manifest: dict[str, JSON], files: dict[str, bytes]) -> bytes:
    """Sign all payload bytes; fixed ZIP order, permissions and timestamps."""
    encoded, fingerprint = public_key(key)
    manifest = {
        **manifest,
        "publisher_public_key": encoded,
        "publisher_fingerprint": fingerprint,
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in files.items()},
    }
    body = canonical(manifest)
    members = {"plugin.json": body, "signatures/ed25519.sig": key.sign(body), **files}
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
        for name, data in sorted(members.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data, compresslevel=9)
    return output.getvalue()


def manifest(publisher: str) -> dict[str, JSON]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", publisher):
        raise ValueError("Invalid publisher identifier")
    metadata = json_object(ROOT / "dev/package.json")
    pin = json_object(ROOT / "dev/minishop.json")
    return {
        **metadata,
        "schema_version": 1,
        "version": version(),
        "publisher": publisher,
        "plugin_api": pin["plugin_api"],
        "core_revision": pin["revision"],
        "frontend_host_api": pin["frontend_host_api"],
        "runtime": {"python": "3.12", "system": "linux", "machine": "x86_64"},
    }
