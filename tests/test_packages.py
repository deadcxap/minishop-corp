"""Real Minishop verification, including signature and payload tampering."""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
from bot.plugins.packages import PluginPackageError, inspect_archive, trust_publisher
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# The build scripts are deliberately not installed as application code.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from package_support import ROOT, archive, create_key, load_key, manifest, public_key  # noqa: E402


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


@pytest.mark.parametrize("revision_length", [40, 28])
def test_release_package_accepts_stable_core_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision_length: int
) -> None:
    revision = json.loads((ROOT / "dev/minishop.json").read_text())["revision"]
    monkeypatch.setattr(
        "bot.plugins.packages._running_core_revision", lambda: revision[:revision_length]
    )
    key = Ed25519PrivateKey.generate()
    candidate = inspect_archive(tmp_path, archive(key, manifest("minishop-corp-test"), files()))
    assert candidate.manifest["core_revision"] == revision


def test_release_package_rejects_another_core_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("bot.plugins.packages._running_core_revision", lambda: "f" * 40)
    key = Ed25519PrivateKey.generate()
    with pytest.raises(PluginPackageError, match="incompatible_core_revision"):
        inspect_archive(tmp_path, archive(key, manifest("minishop-corp-test"), files()))


@pytest.fixture
def standalone_project(tmp_path: Path) -> Path:
    """Only our builder and package inputs; no Core, test tools or dev environment."""
    for name in (
        "scripts/build-package.py",
        "scripts/package_support.py",
        "dev/package.json",
        "dev/minishop.json",
        "pyproject.toml",
        "backend/minishop_corp/__init__.py",
        "locales/en.json",
        "locales/ru.json",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    for name, body in files().items():
        if name.startswith("frontend/"):
            target = tmp_path / name.replace("frontend/", "frontend/dist/", 1)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    return tmp_path


def build_cli(
    project: Path, *args: str, secret: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "MINISHOP_SOURCE": str(project / "no-core"),
        "KIRO_WHEEL_SOURCE": str(project / "no-wheel"),
    }
    env.pop("CORP_TEST_SIGNING_KEY", None)
    if secret is not None:
        env["CORP_TEST_SIGNING_KEY"] = secret
    return subprocess.run(
        [sys.executable, str(project / "scripts/build-package.py"), "build", *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_standalone_build_accepts_the_same_persistent_key_as_file_or_secret(
    standalone_project: Path,
) -> None:
    project = standalone_project
    key = project / "publisher.key"
    create_key(key)
    secret = base64.b64encode(key.read_bytes()).decode("ascii")
    builds = (
        build_cli(project, "--key", str(key), "--output", "release-file"),
        build_cli(
            project,
            "--key-env",
            "CORP_TEST_SIGNING_KEY",
            "--output",
            "release-env",
            secret=secret,
        ),
    )
    for result in builds:
        assert result.returncode == 0, result.stderr
        assert secret not in result.stdout + result.stderr
    file_output = project / "release-file"
    env_output = project / "release-env"
    index = json.loads((env_output / "minishop-plugin.json").read_bytes())
    body = (env_output / index["artifact"]).read_bytes()
    assert body == (file_output / index["artifact"]).read_bytes()
    # The real host must accept the output of the process with no Core imports.
    candidate = inspect_archive(project / "store", body)
    assert candidate.digest == index["sha256"] == hashlib.sha256(body).hexdigest()
    assert (env_output / "SHA256SUMS").read_text() == f"{candidate.digest}  {index['artifact']}\n"
    encoded, fingerprint = public_key(load_key(key))
    assert candidate.manifest["publisher_fingerprint"] == fingerprint
    assert (env_output / "publisher.pub").read_text() == encoded + "\n"
    with ZipFile(io.BytesIO(body)) as bundle:
        assert "publisher.key" not in bundle.namelist()
        assert all(key.read_bytes() not in bundle.read(name) for name in bundle.namelist())


@pytest.mark.parametrize(
    "secret",
    [None, "", "not-a-base64-private-key!", base64.b64encode(b"too-short-private-key").decode()],
)
def test_invalid_ci_signing_secret_fails_without_generating_or_exposing_key(
    standalone_project: Path, secret: str | None
) -> None:
    result = build_cli(
        standalone_project,
        "--key-env",
        "CORP_TEST_SIGNING_KEY",
        secret=secret,
    )
    assert result.returncode == 2
    assert "signing secret" in result.stderr.lower()
    assert "CORP_TEST_SIGNING_KEY" in result.stderr
    if secret:
        assert secret not in result.stdout + result.stderr
    assert not (standalone_project / "dist").exists()
    assert not (standalone_project / ".local/publisher").exists()
