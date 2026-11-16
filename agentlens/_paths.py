"""Lets AgentLens import the shared storage layer (agents/db.py) and the tool constants."""

import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))
