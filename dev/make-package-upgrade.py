"""Synthetic successor for package lifecycle testing, never a published release."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from package_support import archive, load_key, local_path, manifest, payload, version  # noqa: E402


def main() -> None:
    key = load_key(Path(sys.argv[1]))
    output = local_path(Path(sys.argv[2]))
    metadata = manifest("minishop-corp")
    major, minor, patch = version().split(".")
    successor = f"{major}.{minor}.{int(patch) + 1}"
    metadata["version"] = successor
    files = payload()
    entry = "backend/minishop_corp/__init__.py"
    files[entry] = files[entry].replace(
        f'__version__ = "{version()}"'.encode(), f'__version__ = "{successor}"'.encode()
    )
    files["metadata/acceptance.txt"] = b"Synthetic upgrade fixture. Do not publish.\n"
    output.write_bytes(archive(key, metadata, files))


if __name__ == "__main__":
    main()
