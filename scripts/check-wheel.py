"""Import the built wheel from an extracted installation, not the editable source."""

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZipFile

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
wheel = root / ".local" / "dist" / f"minishop_corp-{version}-py3-none-any.whl"
with tempfile.TemporaryDirectory(dir=root / ".local", prefix="wheel-check-") as directory:
    with ZipFile(wheel) as archive:
        archive.extractall(directory)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pathlib; sys.path.insert(0, sys.argv[1]); "
            "import minishop_corp; "
            "assert pathlib.Path(minishop_corp.__file__).is_relative_to(sys.argv[1]); "
            "assert minishop_corp.__version__ == sys.argv[2]; "
            "assert all((minishop_corp.plugin.locales_dir() / (lang + '.json')).is_file() "
            "for lang in ('ru', 'en')); print('PASS: installed wheel and RU/EN resources')",
            directory,
            version,
        ],
        check=True,
    )
