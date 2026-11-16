"""Loads ../.env once. Settings already in the environment win (e.g. AGENTLENS_OFFLINE=1
set by tests), except API_KEY, where the .env value beats a stale system variable."""

import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

load_dotenv(ENV_FILE)
if _file_key := dotenv_values(ENV_FILE).get("API_KEY"):
    os.environ["API_KEY"] = _file_key
