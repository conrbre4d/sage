"""AgentLens's own tables (agent_licence, mistake_log, licence_history, improvement_suggestion,
agentlens_state), through the shared storage layer (local SQLite or Supabase).

AgentLens never reads the vault. It reads event_log, handoffs and application status,
and writes only these tables (plus holds, via access.place_hold).

Kept in memory after the first read (one AgentLens process per database).
"""

from __future__ import annotations

import threading

import _paths  # noqa: F401
import db
import scoring
from detect import AUDITED_AGENTS

_lock = threading.RLock()
_cache: dict = {}


def reset_cache() -> None:
    with _lock:
        _cache.clear()


# --- licences ------------------------------------------------------------------

def licences() -> dict[str, dict]:
    with _lock:
        if "lic" not in _cache:
            rows = {r["agent_name"]: r for r in db.select("agent_licence")}
            missing = [scoring.new_licence(a) for a in AUDITED_AGENTS if a not in rows]
            if missing:
                db.upsert("agent_licence", missing, "agent_name")
                rows.update({m["agent_name"]: m for m in missing})
            for r in rows.values():
                r["level"] = scoring.normalise_level(r.get("level"))
                r["recent_cases"] = list(r.get("recent_cases") or [])
                r["fast_track"] = bool(r.get("fast_track"))
            _cache["lic"] = rows
        return _cache["lic"]


def save_licences(agents: list[str]) -> None:
    lic = licences()
    rows = [lic[a] for a in agents if a in lic]
    db.upsert("agent_licence", rows, "agent_name")


def add_history(rows: list[dict]) -> None:
    rows = [r for r in rows if r]
    if rows:
        db.insert("licence_history", rows)


def history(agent: str | None = None) -> list[dict]:
    return db.select("licence_history", {"agent_name": agent} if agent else {}, order="id")


# --- mistakes ------------------------------------------------------------------

def mistakes() -> list[dict]:
    with _lock:
        if "mistakes" not in _cache:
            _cache["mistakes"] = db.select("mistake_log", order="id")
        return _cache["mistakes"]


def add_mistakes(rows: list[dict]) -> list[dict]:
    saved = db.insert("mistake_log", rows)
    with _lock:
        mistakes().extend(saved)
    return saved


def resolve_pattern(agent: str, pattern_key: str) -> int:
    db.update("mistake_log", {"agent_name": agent, "pattern_key": pattern_key}, {"resolved": True})
    n = 0
    with _lock:
        for m in mistakes():
            if m["agent_name"] == agent and m["pattern_key"] == pattern_key and not m.get("resolved"):
                m["resolved"] = True
                n += 1
    return n


# --- suggestions -----------------------------------------------------------------

def suggestions() -> list[dict]:
    with _lock:
        if "sug" not in _cache:
            _cache["sug"] = db.select("improvement_suggestion", order="id")
        return _cache["sug"]


def get_suggestion(sid: int) -> dict | None:
    return next((s for s in suggestions() if int(s["id"]) == int(sid)), None)


def add_suggestion(row: dict) -> dict:
    saved = db.insert("improvement_suggestion", [row])[0]
    with _lock:
        suggestions().append(saved)
    return saved


def update_suggestion(sid: int, fields: dict) -> dict:
    db.update("improvement_suggestion", {"id": int(sid)}, fields)
    s = get_suggestion(sid)
    s.update(fields)
    return s


# --- scan cursor ---------------------------------------------------------------

def cursor() -> int:
    with _lock:
        if "cursor" not in _cache:
            rows = db.select("agentlens_state", {"key": "cursor"})
            _cache["cursor"] = int(rows[0]["value"]) if rows else 0
        return _cache["cursor"]


def set_cursor(event_id: int) -> None:
    with _lock:
        _cache["cursor"] = int(event_id)
    db.upsert("agentlens_state", [{"key": "cursor", "value": int(event_id)}], "key")
