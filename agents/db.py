"""SQLite storage for the mock pipeline: one event_log table + customer_tracker.

The event_log table is what AgentLens reads. Keep its schema stable.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "agentlens.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT NOT NULL,
    agent_name   TEXT NOT NULL,
    step_name    TEXT NOT NULL,
    timestamp    TEXT NOT NULL,              -- ISO 8601
    input_data   TEXT NOT NULL,              -- JSON
    output_data  TEXT NOT NULL,              -- JSON
    result       TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'flag')),
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_log_case ON event_log (case_id, event_id);

CREATE TABLE IF NOT EXISTS customer_tracker (
    case_id       TEXT PRIMARY KEY,
    current_step  TEXT NOT NULL,
    status        TEXT NOT NULL,
    last_updated  TEXT NOT NULL              -- ISO 8601
);
"""


def db_path() -> Path:
    return Path(os.environ.get("AGENTLENS_DB", DEFAULT_DB))


@contextmanager
def connect(path: str | Path | None = None):
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: str | Path | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# event_log
# ---------------------------------------------------------------------------

def insert_event(case_id: str, agent_name: str, step_name: str, timestamp: str,
                 input_data: dict, output_data: dict, result: str, notes: str,
                 path: str | Path | None = None) -> int:
    """Write one event and return its event_id."""
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO event_log (case_id, agent_name, step_name, timestamp, "
            "input_data, output_data, result, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, agent_name, step_name, timestamp,
             json.dumps(input_data, default=str), json.dumps(output_data, default=str),
             result, notes),
        )
        return cur.lastrowid


def _row_to_event(row: sqlite3.Row) -> dict:
    event = dict(row)
    event["input_data"] = json.loads(event["input_data"])
    event["output_data"] = json.loads(event["output_data"])
    return event


def get_events(case_id: str | None = None, path: str | Path | None = None) -> list[dict]:
    """All events (or one case's events) in the order they were written."""
    with connect(path) as conn:
        if case_id is None:
            rows = conn.execute("SELECT * FROM event_log ORDER BY event_id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM event_log WHERE case_id = ? ORDER BY event_id", (case_id,)
            ).fetchall()
    return [_row_to_event(r) for r in rows]


def clear_case(case_id: str, path: str | Path | None = None) -> None:
    """Remove a case's previous run so a re-run gives a clean log."""
    with connect(path) as conn:
        conn.execute("DELETE FROM event_log WHERE case_id = ?", (case_id,))
        conn.execute("DELETE FROM customer_tracker WHERE case_id = ?", (case_id,))


# ---------------------------------------------------------------------------
# customer_tracker
# ---------------------------------------------------------------------------

def upsert_tracker(case_id: str, current_step: str, status: str, last_updated: str,
                   path: str | Path | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO customer_tracker (case_id, current_step, status, last_updated) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(case_id) DO UPDATE SET "
            "current_step = excluded.current_step, status = excluded.status, "
            "last_updated = excluded.last_updated",
            (case_id, current_step, status, last_updated),
        )


def get_tracker(case_id: str, path: str | Path | None = None) -> dict | None:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM customer_tracker WHERE case_id = ?", (case_id,)
        ).fetchone()
    return dict(row) if row else None
