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
    user_ui = metadata["frontend"]["user"]
    assert [slot["target"] for slot in user_ui["slots"]] == ["user.settings.cards"]
    assert all(
        page["navigation"] == "hidden" and page["parent"] == "settings" for page in user_ui["pages"]
    )
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


@pytest.mark.parametrize("revision", ["f" * 40, "f" * 8, None])
def test_release_package_accepts_compatible_core_with_another_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str | None
) -> None:
    monkeypatch.setattr("bot.plugins.packages._running_core_revision", lambda: revision)
    key = Ed25519PrivateKey.generate()
    candidate = inspect_archive(tmp_path, archive(key, manifest("minishop-corp-test"), files()))
    tested_revision = json.loads((ROOT / "dev/minishop.json").read_text())["revision"]
    assert candidate.manifest["core_revision"] == tested_revision


@pytest.mark.parametrize("capability", ["user_ui", "ui_composition"])
@pytest.mark.parametrize("provided_version", [None, 2])
def test_release_package_rejects_missing_or_incompatible_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    provided_version: int | None,
) -> None:
    from bot.plugins.packages import CORE_PLUGIN_CAPABILITIES

    if provided_version is None:
        monkeypatch.delitem(CORE_PLUGIN_CAPABILITIES, capability)
    else:
        monkeypatch.setitem(CORE_PLUGIN_CAPABILITIES, capability, provided_version)
    key = Ed25519PrivateKey.generate()
    with pytest.raises(PluginPackageError, match="incompatible_core_capability"):
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


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["", "main"])
async def test_repository_importer_finds_generated_package_from_repository_url(
    standalone_project: Path, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    from aiohttp import ClientSession
    from bot.plugins import sources

    project = standalone_project
    key = project / "publisher.key"
    create_key(key)
    result = build_cli(project, "--key", str(key), "--output", ".")
    assert result.returncode == 0, result.stderr
    index = json.loads((project / "minishop-plugin.json").read_bytes())
    revision = "a" * 40
    repo = "https://github.com/example/minishop-corp"
    api = "https://api.github.com/repos/example/minishop-corp"
    raw = "https://raw.githubusercontent.com/example/minishop-corp"
    responses = {
        api: b'{"default_branch":"main"}',
        f"{api}/commits/main": json.dumps({"sha": revision}).encode(),
        f"{api}/commits/{revision}": json.dumps({"sha": revision}).encode(),
        f"{raw}/{revision}/minishop-plugin.json": (project / "minishop-plugin.json").read_bytes(),
        f"{raw}/main/minishop-plugin.json": (project / "minishop-plugin.json").read_bytes(),
        f"{raw}/{revision}/{index['artifact']}": (project / index["artifact"]).read_bytes(),
    }
    requested: list[str] = []

    async def download(session: ClientSession, url: str, limit: int) -> bytes:
        # Only transport is replaced; ref resolution, paths and hash checks belong to Core.
        requested.append(url)
        body = responses[url]
        assert len(body) <= limit
        return body

    monkeypatch.setattr(sources, "_download", download)
    body, source = await sources.fetch_ready_package(repo, ref)
    assert source["commit"] == revision
    assert source["requested_ref"] == ref
    assert source["artifact"] == index["artifact"]
    assert inspect_archive(project / "store", body).digest == index["sha256"]
    assert f"{raw}/{revision}/{index['artifact']}" in requested
    # Confirmation pins the previewed commit; later update checks need only the index.
    pinned_body, pinned_source = await sources.fetch_ready_package(repo, ref, commit=revision)
    assert pinned_body == body and pinned_source["commit"] == revision
    digest, release = await sources.check_ready_package_release(repo, ref)
    assert digest == index["sha256"]
    assert release == tuple(int(part) for part in index["version"].split("."))
    responses[f"{raw}/{revision}/{index['artifact']}"] = body + b"tampered"
    with pytest.raises(PluginPackageError, match="repository_artifact_hash_mismatch"):
        await sources.fetch_ready_package(repo, ref)


def test_repository_build_preserves_published_version_until_version_is_bumped(
    standalone_project: Path,
) -> None:
    project = standalone_project
    key = project / "publisher.key"
    create_key(key)
    args = ("--key", str(key), "--output", ".")
    first = build_cli(project, *args)
    assert first.returncode == 0, first.stderr
    index_bytes = (project / "minishop-plugin.json").read_bytes()
    index = json.loads(index_bytes)
    archive_bytes = (project / index["artifact"]).read_bytes()
    repeat = build_cli(project, *args)
    assert repeat.returncode == 0, repeat.stderr
    assert (project / index["artifact"]).read_bytes() == archive_bytes
    source = project / "backend/minishop_corp/__init__.py"
    source.write_text(source.read_text() + "\n# Changed package contents.\n")
    changed = build_cli(project, *args)
    assert changed.returncode != 0
    assert "Output version already has different bytes" in changed.stderr
    assert (project / "minishop-plugin.json").read_bytes() == index_bytes
    assert (project / index["artifact"]).read_bytes() == archive_bytes
    old_version = index["version"]
    major, minor, patch = (int(part) for part in old_version.split("."))
    new_version = f"{major}.{minor}.{patch + 1}"
    for file in (source, project / "pyproject.toml"):
        file.write_text(file.read_text().replace(f'"{old_version}"', f'"{new_version}"'))
    updated = build_cli(project, *args)
    assert updated.returncode == 0, updated.stderr
    new_index = json.loads((project / "minishop-plugin.json").read_bytes())
    assert new_index["version"] == new_version
    assert new_index["artifact"] != index["artifact"]
    assert (project / index["artifact"]).read_bytes() == archive_bytes
    assert inspect_archive(project / "store", (project / new_index["artifact"]).read_bytes())


def git(project: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=Publication Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(project),
            *args,
        ],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def publish(project: Path, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "scripts/publish-package.sh")],
        cwd=project,
        env={**os.environ, "CORP_PUBLISH_BRANCH": "main", "CORP_PUBLISH_SHA": source},
        text=True,
        capture_output=True,
        timeout=30,
    )


@pytest.fixture
def publication_project(standalone_project: Path) -> tuple[Path, Path, str]:
    project = standalone_project
    remote = project / ".local/remote.git"
    remote.parent.mkdir()
    (project / ".gitignore").write_text(".local/\npublisher.key\n")
    git(project, "init", "--bare", str(remote))
    git(project, "init", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-m", "Initial source")
    git(project, "remote", "add", "origin", str(remote))
    git(project, "push", "origin", "HEAD:refs/heads/main")
    source = git(project, "rev-parse", "HEAD")
    key = project / "publisher.key"
    create_key(key)
    built = build_cli(project, "--key", str(key), "--output", ".")
    assert built.returncode == 0, built.stderr
    return project, remote, source


def test_publication_commits_index_and_zip_together_and_skips_unchanged_package(
    publication_project: tuple[Path, Path, str],
) -> None:
    project, remote, source = publication_project
    (project / "unrelated.txt").write_text("Do not publish unrelated work.\n")
    git(project, "add", "unrelated.txt")
    result = publish(project, source)
    assert result.returncode == 0, result.stderr
    published = git(remote, "rev-parse", "refs/heads/main")
    assert published != source
    index = json.loads(git(remote, "show", "refs/heads/main:minishop-plugin.json"))
    paths = set(
        git(project, "diff-tree", "--no-commit-id", "--name-only", "-r", published).splitlines()
    )
    assert paths == {"minishop-plugin.json", index["artifact"], "publisher.pub", "SHA256SUMS"}
    assert git(project, "diff", "--cached", "--name-only") == "unrelated.txt"
    repeated = publish(project, published)
    assert repeated.returncode == 0, repeated.stderr
    assert "already up to date" in repeated.stdout
    assert git(remote, "rev-parse", "refs/heads/main") == published


def test_publication_skips_stale_build_and_rejects_wrong_checkout(
    publication_project: tuple[Path, Path, str],
) -> None:
    project, remote, source = publication_project
    git(project, "commit", "--allow-empty", "-m", "Newer source")
    newer = git(project, "rev-parse", "HEAD")
    git(project, "push", "origin", "HEAD:refs/heads/main")
    wrong = publish(project, source)
    assert wrong.returncode != 0
    assert "does not match" in wrong.stderr
    git(project, "checkout", "--detach", source)
    stale = publish(project, source)
    assert stale.returncode == 0, stale.stderr
    assert "Branch has moved" in stale.stdout
    assert git(remote, "rev-parse", "refs/heads/main") == newer


def test_publication_rejects_damaged_archive(
    publication_project: tuple[Path, Path, str],
) -> None:
    project, remote, source = publication_project
    index = json.loads((project / "minishop-plugin.json").read_bytes())
    (project / index["artifact"]).write_bytes(b"damaged")
    result = publish(project, source)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert git(remote, "rev-parse", "refs/heads/main") == source
