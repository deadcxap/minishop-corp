"""Real Minishop verification, including signature and payload tampering."""

import io
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from bot.plugins.packages import PluginPackageError, inspect_archive, trust_publisher
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# The build scripts are deliberately not installed as application code.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from package_support import archive, create_key, load_key, manifest, public_key  # noqa: E402


def files() -> dict[str, bytes]:
    return {
        "backend/minishop_corp/__init__.py": b"plugin = None\n",
        "frontend/admin/index.js": b"export function mountView() {}\n",
        "frontend/admin/index.css": b".minishop-corp {}\n",
        "frontend/customer/index.js": b"export function mountView() {}\n",
        "frontend/customer/index.css": b".minishop-corp {}\n",
    }


def test_signature_reproducibility_and_trust(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    metadata = manifest("minishop-corp-test")
    assert "core_compatibility" not in metadata
    first = archive(key, metadata, files())
    assert first == archive(key, metadata, dict(reversed(list(files().items()))))
    candidate = inspect_archive(tmp_path, first)
    assert not candidate.trusted and candidate.reason == "publisher_not_trusted"
    encoded, fingerprint = public_key(key)
    trust_publisher(tmp_path, "minishop-corp-test", encoded, fingerprint)
    assert inspect_archive(tmp_path, first).trusted
    with ZipFile(io.BytesIO(first)) as bundle:
        assert set(bundle.namelist()) == set(files()) | {"plugin.json", "signatures/ed25519.sig"}
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in bundle.infolist())


@pytest.mark.parametrize("target", ["backend/minishop_corp/__init__.py", "signatures/ed25519.sig"])
def test_host_rejects_tampered_package(tmp_path: Path, target: str) -> None:
    key = Ed25519PrivateKey.generate()
    original = archive(key, manifest("minishop-corp-test"), files())
    changed = io.BytesIO()
    with ZipFile(io.BytesIO(original)) as source, ZipFile(changed, "w") as dest:
        for name in source.namelist():
            body = source.read(name)
            dest.writestr(name, bytes([body[0] ^ 1]) + body[1:] if name == target else body)
    with pytest.raises(
        PluginPackageError, match="package_file_hash_mismatch|invalid_package_signature"
    ):
        inspect_archive(tmp_path, changed.getvalue())


def test_signing_key_is_private_and_never_silently_replaced(tmp_path: Path) -> None:
    path = tmp_path / "publisher.key"
    create_key(path)
    before = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    load_key(path)
    with pytest.raises(FileExistsError):
        create_key(path)
    assert path.read_bytes() == before
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_key(path)


def test_release_package_rejects_another_core_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bot.plugins.packages._running_core_revision", lambda: "f" * 40)
    key = Ed25519PrivateKey.generate()
    with pytest.raises(PluginPackageError, match="incompatible_core_revision"):
        inspect_archive(tmp_path, archive(key, manifest("minishop-corp-test"), files()))
