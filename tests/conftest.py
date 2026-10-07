"""Run core imports without loading any operator environment or production store."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["MINISHOP_PLUGIN_STORE"] = str(ROOT / ".local" / "test-plugin-store")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
