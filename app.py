"""Vercel entry point: exposes the AgentLens FastAPI app as `app`.

  /dashboard      AgentLens dashboard
  /bank/form      loan application form (the bank pipeline is mounted at /bank)

Local development is unchanged:  cd agentlens  &&  uvicorn agentlens:app --reload
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for sub in ("agents", "agentlens"):
    path = str(ROOT / sub)
    if path not in sys.path:
        sys.path.insert(0, path)

if os.getenv("VERCEL"):
    # Vercel's filesystem is read-only except /tmp
    os.environ.setdefault("AGENTLENS_LLM_CACHE", "/tmp/llm_cache.json")
    os.environ.setdefault("AGENTLENS_DB", "/tmp/agentlens.db")

import agentlens as _agentlens  # noqa: E402  (agentlens/agentlens.py)

app = _agentlens.app

# Initialise now as well as on startup, in case the platform skips ASGI startup events (safe to run twice).
try:
    _agentlens._startup()
except Exception as e:  # pragma: no cover - logged, the app can still serve pages
    print(f"AgentLens startup deferred: {e}")
