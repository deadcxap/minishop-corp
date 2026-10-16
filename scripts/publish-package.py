"""Publish a generated package, then optionally its immutable tag and GitHub Release.

Uses only the standard library, Git and the GitHub CLI; no Minishop or signing key.
Run from the checkout that produced the downloaded Actions artifact.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, check=check)


def git(*args: str) -> str:
    return run("git", *args).stdout.decode().strip()


def remote_ref(ref: str) -> str | None:
    result = git("ls-remote", "origin", ref)
    return result.split()[0] if result else None


def object_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return cast(dict[str, object], value)


@dataclass(frozen=True)
class Package:
    version: str
    files: dict[str, bytes]

    @classmethod
    def load(cls) -> "Package":
        index = object_value(json.loads(Path("minishop-plugin.json").read_bytes()))
        version = index.get("version")
        artifact = index.get("artifact")
        if (
            index.get("schema_version") != 1
            or not isinstance(version, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            or artifact != f"packages/minishop-corp-{version}.zip"
        ):
            raise ValueError("Invalid generated package index")
        assert isinstance(artifact, str)
        files = {
            name: Path(name).read_bytes()
            for name in ("minishop-plugin.json", artifact, "publisher.pub", "SHA256SUMS")
        }
        digest = hashlib.sha256(files[artifact]).hexdigest()
        if (
            index.get("sha256") != digest
            or files["SHA256SUMS"] != f"{digest}  {artifact}\n".encode()
        ):
            raise ValueError("Generated package checksum mismatch")
        return cls(version, files)

    def matches(self, commit: str) -> bool:
        for name, body in self.files.items():
            result = run("git", "show", f"{commit}:{name}", check=False)
            if result.returncode or result.stdout != body:
                return False
        return True


def publish_package(package: Package, branch: str, source: str) -> str | None:
    ref = f"refs/heads/{branch}"
    git("check-ref-format", ref)
    if git("rev-parse", "HEAD") != source:
        raise ValueError("Checkout does not match the package source commit")
    remote = remote_ref(ref)
    if remote is None:
        raise ValueError("Destination branch does not exist")
    if remote != source:
        git("fetch", "--no-tags", "--depth=2", "origin", remote)
        # A retry may start from the original source after the package commit was pushed.
        # Only recover that direct child, with exactly this package and no source changes.
        changed = set(git("diff-tree", "--no-commit-id", "--name-only", "-r", remote).splitlines())
        if (
            git("show", "-s", "--format=%P", remote) == source
            and changed
            and changed <= package.files.keys()
            and package.matches(remote)
        ):
            print("Package commit already published; resuming its release.")
            return remote
        print("Branch has moved; the newer workflow run will publish its package.")
        return None

    git("add", "--", *package.files)
    diff = run("git", "diff", "--cached", "--quiet", "--", *package.files, check=False)
    if diff.returncode == 0:
        print("Published package is already up to date.")
        return source
    if diff.returncode != 1:
        diff.check_returncode()
    git(
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--only",
        "-m",
        "build: publish signed plugin package",
        "--",
        *package.files,
    )
    # Normal push rejects any branch change after the check; never force-push.
    git("push", "origin", f"HEAD:{ref}")
    return git("rev-parse", "HEAD")


def publish_tag(package: Package, commit: str) -> str:
    tag = f"v{package.version}"
    ref = f"refs/tags/{tag}"
    if remote_ref(ref) is None:
        git("push", "origin", f"{commit}:{ref}")
    else:
        git("fetch", "--no-tags", "--depth=1", "origin", ref)
        tagged = git("rev-parse", "FETCH_HEAD^{commit}")
        # Documentation-only pushes can reuse a version, but never move its tag.
        if not package.matches(tagged):
            raise ValueError(f"Tag {tag} already exists with different package files")
    return tag


def release_record(repository: str, tag: str) -> dict[str, object] | None:
    # Listing includes drafts. A failed API request must not be mistaken for a missing release.
    result = run(
        "gh",
        "api",
        "--paginate",
        f"repos/{repository}/releases?per_page=100",
        "--jq",
        f'.[] | select(.tag_name == "{tag}")',
    ).stdout.strip()
    return object_value(json.loads(result)) if result else None


def release_asset_names(record: dict[str, object]) -> set[str]:
    assets = record.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Expected GitHub release assets")
    names: set[str] = set()
    for asset in assets:
        name = object_value(asset).get("name")
        if not isinstance(name, str) or name in names:
            raise ValueError("Expected unique GitHub release asset names")
        names.add(name)
    return names


def wait_for_release(
    repository: str, tag: str, *, expected_assets: set[str] | None = None
) -> dict[str, object]:
    # GitHub's release listing can briefly lag successful create/upload requests.
    # Retry only reads of missing state, never a mutation or a failed API request.
    subject = "release metadata" if expected_assets is None else "uploaded assets"
    for delay in (0, 1, 2, 4, 8):
        if delay:
            print(f"Waiting {delay}s for GitHub {tag} {subject}.", flush=True)
            time.sleep(delay)
        record = release_record(repository, tag)
        if record is not None and (
            expected_assets is None or expected_assets <= release_asset_names(record)
        ):
            return record
    raise ValueError(
        f"GitHub {tag}: {subject} not yet visible after 5 checks; rerun the publish job."
    )


def missing_assets(
    repository: str, tag: str, package: Package, record: dict[str, object]
) -> list[str]:
    names = release_asset_names(record)
    expected = {Path(name).name for name in package.files}
    if not names <= expected:
        raise ValueError(f"Unexpected release assets on {tag}")
    missing = []
    for path, body in package.files.items():
        name = Path(path).name
        if name not in names:
            missing.append(path)
            continue
        downloaded = run(
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            name,
            "--output",
            "-",
        ).stdout
        if downloaded != body:
            raise ValueError(f"Release asset {name} differs; refusing to overwrite it")
    return missing


def publish_release(package: Package, commit: str, repository: str, notes: Path) -> None:
    tag = publish_tag(package, commit)
    record = release_record(repository, tag)
    if record is None:
        run(
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--draft",
            "--verify-tag",
            "--target",
            commit,
            "--title",
            f"minishop-corp {package.version}",
            "--notes-file",
            str(notes),
        )
        record = wait_for_release(repository, tag)
    if record.get("prerelease") is not False:
        raise ValueError(f"Expected a stable release for {tag}")
    missing = missing_assets(repository, tag, package, record)
    if record.get("draft") is False:
        if missing:
            raise ValueError(f"Published release {tag} is incomplete; refusing to modify it")
        print(f"Release {tag} already published and verified.")
    elif record.get("draft") is True:
        if missing:
            run("gh", "release", "upload", tag, "--repo", repository, *missing)
        record = wait_for_release(
            repository, tag, expected_assets={Path(name).name for name in package.files}
        )
        if record.get("draft") is not True or record.get("prerelease") is not False:
            raise ValueError(f"Draft release {tag} changed during upload; rerun the publish job")
        if missing_assets(repository, tag, package, record):
            raise ValueError(f"Release {tag} is missing assets after upload")
        run("gh", "release", "edit", tag, "--repo", repository, "--draft=false", "--latest")
        print(f"Published release {tag}.")
    else:
        raise ValueError("Expected GitHub release draft status")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as stream:
            stream.write(
                f"\nRelease: [{tag}](https://github.com/{repository}/releases/tag/{tag})\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release", action="store_true", help="Also publish tag and GitHub Release"
    )
    args = parser.parse_args()
    package = Package.load()
    notes = Path(f"docs/releases/{package.version}.md")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if args.release:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise ValueError("Expected GITHUB_REPOSITORY owner/name")
        if not notes.is_file() or not notes.read_text().strip():
            raise ValueError(f"Missing release notes: {notes}")
    commit = publish_package(
        package, os.environ["CORP_PUBLISH_BRANCH"], os.environ["CORP_PUBLISH_SHA"]
    )
    if commit is not None and args.release:
        publish_release(package, commit, repository, notes)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.decode(errors="replace"), file=sys.stderr)
        sys.exit(exc.returncode)
    except (KeyError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
