"""Run core imports without loading any operator environment or production store."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("MINISHOP_PLUGIN_STORE", str(ROOT / ".local" / "test-plugin-store"))
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

collect_ignore_glob = [] if os.environ.get("CORP_INTEGRATION") == "1" else ["integration/*"]
