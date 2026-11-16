"""Storage for the mock pipeline and AgentLens. Two interchangeable backends:

  supabase  the real one (free tier is fine). Tables: supabase/schema.sql
  local     SQLite file, same tables. Used by the tests and as an offline fallback.

AGENTLENS_BACKEND picks one. Default: supabase if SUPABASE_URL is set, else local.

Identity (name, DOB, address, account number, employer), the customer's free-text
message and the documents are ENCRYPTED here, in Python, before they're stored.
The key (AGENTLENS_ENCRYPTION_KEY) never goes to the database.

Speed: every Supabase request is a network round trip, so this layer caches what
this process wrote or read (applications, handoffs, documents), and on Supabase it
reserves event IDs from the database in blocks and writes events in batches
(flush_events). IDs come from the database sequence, so they never clash.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import config  # noqa: F401  (loads .env)

DEFAULT_DB = Path(__file__).resolve().parent / "agentlens.db"
# Local/dev only. Supabase mode refuses to run without a real key.
_DEV_KEY = base64.urlsafe_b64encode(b"agentlens-local-dev-key-NOT-SECRET"[:32])


# ---------------------------------------------------------------------------
# Encryption (Fernet = AES + HMAC). Ciphertext is all the database ever sees.
# ---------------------------------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet

    key = (os.getenv("AGENTLENS_ENCRYPTION_KEY") or "").strip()
    if not key:
        if backend_name() == "supabase":
            raise RuntimeError("AGENTLENS_ENCRYPTION_KEY is not set in .env. Make one with: "
                               "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
        key = _DEV_KEY
    return Fernet(key)


def encrypt(value) -> str:
    return _fernet().encrypt(json.dumps(value).encode()).decode()


def decrypt(token: str):
    return json.loads(_fernet().decrypt(token.encode()))


# ---------------------------------------------------------------------------
# Backend choice
# ---------------------------------------------------------------------------

def backend_name() -> str:
    name = os.getenv("AGENTLENS_BACKEND") or ("supabase" if os.getenv("SUPABASE_URL") else "local")
    if name not in ("supabase", "local"):
        raise RuntimeError(f"AGENTLENS_BACKEND must be 'supabase' or 'local', not {name!r}")
    return name


def _backend():
    if backend_name() == "supabase":
        import supabase_db
        return supabase_db
    return _local


def _jsonable(x):
    return json.loads(json.dumps(x, default=str))


# ---------------------------------------------------------------------------
# Caches + event buffer
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_identity_cache: dict[str, dict] = {}
_app_cache: dict[str, dict] = {}
_handoff_cache: dict[tuple[str, str], dict] = {}
_doc_cache: dict[tuple[str, str], str] = {}
_mock_cache: dict[str, dict] = {}
_event_buffer: list[dict] = []
_reserved_ids: list[int] = []
_no_reserve = False
EVENT_ID_BLOCK = 40


def _forget(case_id: str | None = None) -> None:
    with _lock:
        if case_id is None:
            for c in (_identity_cache, _app_cache, _handoff_cache, _doc_cache, _mock_cache):
                c.clear()
            return
        _app_cache.pop(case_id, None)
        _mock_cache.pop(case_id, None)
        for c in (_handoff_cache, _doc_cache):
            for k in [k for k in c if k[0] == case_id]:
                c.pop(k, None)


def init_db() -> None:
    _backend().init()


# ---------------------------------------------------------------------------
# Applications (identity + documents encrypted)
# ---------------------------------------------------------------------------

def save_application(case_id: str, application: dict, customer_token: str, identity: dict,
                     customer_message: str, documents: dict[str, str], credit_report: dict,
                     customer_replies: list[str]) -> None:
    """Store a submitted application. Replaces any earlier submission with this case_id."""
    save_applications([{"case_id": case_id, "application": application, "customer_token": customer_token,
                        "identity": identity, "customer_message": customer_message, "documents": documents,
                        "credit_report": credit_report, "customer_replies": customer_replies}])


def save_applications(cases: list[dict]) -> None:
    """Store many submitted applications at once (one request per table on Supabase)."""
    enc = [{"case_id": c["case_id"], "application": c["application"], "customer_token": c["customer_token"],
            "identity_encrypted": encrypt(c["identity"]), "message_encrypted": encrypt(c["customer_message"]),
            "documents_encrypted": {k: encrypt(v) for k, v in c["documents"].items()},
            "credit_report": c["credit_report"], "customer_replies": c["customer_replies"]} for c in cases]
    _backend().save_applications(enc)
    for c in cases:
        _forget(c["case_id"])
    with _lock:
        _identity_cache.clear()


def get_application(case_id: str) -> dict | None:
    with _lock:
        if case_id in _app_cache:
            return dict(_app_cache[case_id])
    row = _backend().get_application(case_id)
    if row is not None:
        with _lock:
            _app_cache[case_id] = dict(row)
    return row


def list_applications(status: str | None = None) -> list[dict]:
    rows = _backend().list_applications(status)
    with _lock:
        for r in rows:
            _app_cache[r["case_id"]] = dict(r)
    return rows


def update_application(case_id: str, fields: dict) -> None:
    _backend().update_application(case_id, fields)
    with _lock:
        if case_id in _app_cache:
            _app_cache[case_id].update(fields)


def get_identity(customer_token: str) -> dict:
    """Decrypted identity. Only plain-code tools call this, never an AI step or AgentLens."""
    with _lock:
        hit = _identity_cache.get(customer_token)
    if hit is None:
        row = _backend().get_identity_row(customer_token)
        if row is None:
            raise KeyError(f"unknown customer token {customer_token}")
        hit = {"case_id": row["case_id"], **decrypt(row["identity_encrypted"]),
               "_message": decrypt(row["customer_message_encrypted"])}
        with _lock:
            _identity_cache[customer_token] = hit
    return {k: v for k, v in hit.items() if k != "_message"}


def get_customer_message(case_id: str) -> str:
    app = get_application(case_id)
    if app is None:
        raise KeyError(f"no submitted application for {case_id}")
    get_identity(app["customer_token"])
    with _lock:
        return _identity_cache[app["customer_token"]]["_message"]


def get_document_text(case_id: str, doc_type: str) -> str:
    key = (case_id, doc_type)
    with _lock:
        enc = _doc_cache.get(key)
    if enc is None:
        enc = _backend().get_document(case_id, doc_type)
        if enc is None:
            raise KeyError(f"no {doc_type} on file for {case_id}")
        with _lock:
            _doc_cache[key] = enc
    return decrypt(enc)


def get_mock_external(case_id: str) -> dict:
    with _lock:
        if case_id in _mock_cache:
            return _mock_cache[case_id]
    row = _backend().get_mock_external(case_id)
    if row is None:
        raise KeyError(f"no mock bureau data for {case_id}")
    with _lock:
        _mock_cache[case_id] = row
    return row


# ---------------------------------------------------------------------------
# Handoffs
# ---------------------------------------------------------------------------

def write_handoff(case_id: str, stage: str, data: dict) -> None:
    """What one agent passes to the next (token + facts only, never identity)."""
    data = _jsonable(data)
    _backend().write_handoff(case_id, stage, data)
    with _lock:
        _handoff_cache[(case_id, stage)] = data


def read_handoff(case_id: str, stage: str) -> dict | None:
    with _lock:
        if (case_id, stage) in _handoff_cache:
            return _handoff_cache[(case_id, stage)]
    data = _backend().read_handoff(case_id, stage)
    if data is not None:
        with _lock:
            _handoff_cache[(case_id, stage)] = data
    return data


# ---------------------------------------------------------------------------
# Event log
# ---------------------------------------------------------------------------

def insert_event(case_id: str, agent_name: str, step_name: str, timestamp: str,
                 input_data: dict, output_data: dict, result: str, notes: str) -> int:
    """Write one event and return its event_id (buffered on Supabase; see flush_events)."""
    row = {"case_id": case_id, "agent_name": agent_name, "step_name": step_name, "timestamp": timestamp,
           "input_data": _jsonable(input_data), "output_data": _jsonable(output_data),
           "result": result, "notes": notes}
    global _no_reserve
    backend = _backend()
    if backend is _local:
        return backend.insert_event(**row)
    with _lock:
        if not _reserved_ids and not _no_reserve:
            try:
                _reserved_ids.extend(backend.reserve_event_ids(EVENT_ID_BLOCK))
            except Exception as e:  # schema.sql's reserve_event_ids() not installed yet: slower, one by one
                if "reserve_event_ids" not in str(e):
                    raise
                _no_reserve = True
        if _no_reserve:
            flush_events()
            return backend.insert_event(**row)
        row["event_id"] = _reserved_ids.pop(0)
        _event_buffer.append(row)
        return row["event_id"]


def flush_events() -> None:
    """Write buffered events (Supabase). Safe to call any time."""
    with _lock:
        batch = list(_event_buffer)
        _event_buffer.clear()
    if batch:
        _backend().insert_events(batch)


def get_events(case_id: str | None = None) -> list[dict]:
    flush_events()
    return _backend().get_events(case_id)


def get_events_after(event_id: int) -> list[dict]:
    """Events with event_id > event_id, in order (what AgentLens scans)."""
    flush_events()
    return _backend().get_events_after(event_id)


def clear_case(case_id: str) -> None:
    """Remove a case's previous run (events, tracker, handoffs) so a re-run gives a clean log."""
    flush_events()
    _backend().clear_case(case_id)
    with _lock:
        for k in [k for k in _handoff_cache if k[0] == case_id]:
            _handoff_cache.pop(k, None)


# ---------------------------------------------------------------------------
# Customer tracker
# ---------------------------------------------------------------------------

def upsert_tracker(case_id: str, current_step: str, status: str, last_updated: str,
                   message: str | None = None) -> None:
    _backend().upsert_tracker(case_id, current_step, status, last_updated, message)


def get_tracker(case_id: str) -> dict | None:
    return _backend().get_tracker(case_id)


# ---------------------------------------------------------------------------
# Generic table access (AgentLens's own tables)
# ---------------------------------------------------------------------------

def select(table: str, where: dict | None = None, order: str | None = None, desc: bool = False,
           limit: int | None = None) -> list[dict]:
    """Rows where column == value for every item in `where`."""
    return _backend().select(table, where or {}, order, desc, limit)


def insert(table: str, rows: list[dict]) -> list[dict]:
    """Insert rows; returns them as stored (with generated ids)."""
    return _backend().insert(table, [_jsonable(r) for r in rows]) if rows else []


def upsert(table: str, rows: list[dict], key: str) -> None:
    if rows:
        _backend().upsert(table, [_jsonable(r) for r in rows], key)


def update(table: str, where: dict, fields: dict) -> None:
    _backend().update(table, where, _jsonable(fields))


def reset_demo() -> None:
    """Delete ALL demo data: applications, events, trackers and AgentLens tables."""
    with _lock:
        _event_buffer.clear()
    _backend().reset_demo()
    _forget()


def check() -> list[str]:
    """Problems with the current setup (empty list = all good)."""
    problems = []
    try:
        _fernet()
    except Exception as e:
        problems.append(f"encryption key: {e}")
    try:
        problems += _backend().check()
    except Exception as e:
        problems.append(f"{backend_name()}: {e}")
    return problems


# ---------------------------------------------------------------------------
# Local SQLite backend
# ---------------------------------------------------------------------------

class _local:  # namespace
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS applications (
        case_id TEXT PRIMARY KEY, customer_token TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'submitted', submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        stated_annual_income REAL NOT NULL, requested_amount REAL NOT NULL, term_years INTEGER,
        purpose TEXT NOT NULL, employment TEXT, monthly_expenses REAL, credit_card_limit REAL,
        has_hecs INTEGER NOT NULL DEFAULT 0, existing_customer INTEGER NOT NULL DEFAULT 1,
        residency TEXT NOT NULL DEFAULT 'AU', household TEXT NOT NULL DEFAULT 'single',
        dependants INTEGER NOT NULL DEFAULT 0,
        decision TEXT, decision_confidence INTEGER, decided_at TEXT
    );
    CREATE TABLE IF NOT EXISTS customer_identity (
        customer_token TEXT PRIMARY KEY, case_id TEXT NOT NULL,
        identity_encrypted TEXT NOT NULL, customer_message_encrypted TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS documents (
        case_id TEXT NOT NULL, doc_type TEXT NOT NULL, content_encrypted TEXT NOT NULL,
        PRIMARY KEY (case_id, doc_type)
    );
    CREATE TABLE IF NOT EXISTS mock_external_data (
        case_id TEXT PRIMARY KEY, credit_report TEXT NOT NULL, customer_replies TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS event_log (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, agent_name TEXT NOT NULL,
        step_name TEXT NOT NULL, timestamp TEXT NOT NULL, input_data TEXT NOT NULL,
        output_data TEXT NOT NULL, result TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'flag')),
        notes TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_event_log_case ON event_log (case_id, event_id);
    CREATE TABLE IF NOT EXISTS customer_tracker (
        case_id TEXT PRIMARY KEY, current_step TEXT NOT NULL, status TEXT NOT NULL,
        last_updated TEXT NOT NULL, message TEXT
    );
    CREATE TABLE IF NOT EXISTS handoffs (
        case_id TEXT NOT NULL, stage TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (case_id, stage)
    );
    -- AgentLens
    CREATE TABLE IF NOT EXISTS agent_licence (
        agent_name TEXT PRIMARY KEY, trust_score INTEGER NOT NULL DEFAULT 100,
        level TEXT NOT NULL DEFAULT 'FULL', cases_at_level INTEGER NOT NULL DEFAULT 0,
        last_level_change TEXT, risk_owner TEXT NOT NULL DEFAULT 'unassigned',
        fast_track INTEGER NOT NULL DEFAULT 0, recent_cases TEXT NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS mistake_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT, case_id TEXT, event_id INTEGER,
        mistake_type TEXT, mistake_reason TEXT, agent_stated_reasoning TEXT,
        what_should_have_happened TEXT, timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
        rule TEXT, pattern_key TEXT, details TEXT NOT NULL DEFAULT '{}', resolved INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS licence_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT, case_id TEXT, event_id INTEGER,
        score_change INTEGER, reason TEXT, new_score INTEGER, new_level TEXT,
        timestamp TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS improvement_suggestion (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_name TEXT, pattern_description TEXT, root_cause TEXT,
        suggested_fix TEXT, status TEXT NOT NULL DEFAULT 'proposed', iteration INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, pattern_key TEXT, case_ids TEXT NOT NULL DEFAULT '[]',
        parent_id INTEGER, applied_by TEXT, fix_description TEXT, verify_result TEXT
    );
    CREATE TABLE IF NOT EXISTS agentlens_state (key TEXT PRIMARY KEY, value TEXT);
    """
    BOOL_COLS = {"has_hecs", "existing_customer", "fast_track", "resolved"}
    JSON_COLS = {"recent_cases", "details", "case_ids", "verify_result", "value", "credit_report",
                 "customer_replies", "data", "input_data", "output_data"}
    ALL_TABLES = ["applications", "customer_identity", "documents", "mock_external_data", "event_log",
                  "customer_tracker", "handoffs", "agent_licence", "mistake_log", "licence_history",
                  "improvement_suggestion", "agentlens_state"]

    @staticmethod
    @contextmanager
    def connect():
        conn = sqlite3.connect(os.environ.get("AGENTLENS_DB", DEFAULT_DB), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    _ready: set = set()

    @classmethod
    def init(cls):
        path = str(os.environ.get("AGENTLENS_DB", DEFAULT_DB))
        if path in cls._ready and os.path.exists(path):
            return
        with cls.connect() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "customer_tracker" in tables and "applications" not in tables:
                conn.executescript("DROP TABLE IF EXISTS event_log; DROP TABLE IF EXISTS customer_tracker;")
            conn.executescript(cls.SCHEMA)
        cls._ready.add(path)

    @classmethod
    def _row(cls, row):
        if row is None:
            return None
        d = dict(row)
        for k, v in d.items():
            if k in cls.BOOL_COLS and v is not None:
                d[k] = bool(v)
            elif k in cls.JSON_COLS and isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except ValueError:
                    pass
        return d

    @classmethod
    def _enc(cls, row: dict) -> dict:
        return {k: (json.dumps(v) if k in cls.JSON_COLS and not isinstance(v, str) else v) for k, v in row.items()}

    # --- applications ---
    @classmethod
    def save_applications(cls, cases):
        cls.init()
        with cls.connect() as conn:
            for c in cases:
                cid = c["case_id"]
                for t in ("handoffs", "documents", "mock_external_data", "customer_identity", "applications"):
                    conn.execute(f"DELETE FROM {t} WHERE case_id = ?", (cid,))
                cols = {"case_id": cid, "customer_token": c["customer_token"], **c["application"]}
                conn.execute(f"INSERT INTO applications ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             list(cols.values()))
                conn.execute("INSERT INTO customer_identity VALUES (?, ?, ?, ?)",
                             (c["customer_token"], cid, c["identity_encrypted"], c["message_encrypted"]))
                for doc_type, enc in c["documents_encrypted"].items():
                    conn.execute("INSERT INTO documents VALUES (?, ?, ?)", (cid, doc_type, enc))
                conn.execute("INSERT INTO mock_external_data VALUES (?, ?, ?)",
                             (cid, json.dumps(c["credit_report"]), json.dumps(c["customer_replies"])))

    @classmethod
    def get_application(cls, case_id):
        cls.init()
        with cls.connect() as conn:
            return cls._row(conn.execute("SELECT * FROM applications WHERE case_id = ?", (case_id,)).fetchone())

    @classmethod
    def list_applications(cls, status):
        cls.init()
        with cls.connect() as conn:
            q, args = "SELECT * FROM applications", ()
            if status:
                q, args = q + " WHERE status = ?", (status,)
            return [cls._row(r) for r in conn.execute(q + " ORDER BY submitted_at, rowid", args)]

    @classmethod
    def update_application(cls, case_id, fields):
        with cls.connect() as conn:
            conn.execute(f"UPDATE applications SET {', '.join(f'{k} = ?' for k in fields)} WHERE case_id = ?",
                         [*fields.values(), case_id])

    @classmethod
    def get_identity_row(cls, token):
        with cls.connect() as conn:
            row = conn.execute("SELECT * FROM customer_identity WHERE customer_token = ?", (token,)).fetchone()
        return dict(row) if row else None

    @classmethod
    def get_document(cls, case_id, doc_type):
        with cls.connect() as conn:
            row = conn.execute("SELECT content_encrypted FROM documents WHERE case_id = ? AND doc_type = ?",
                               (case_id, doc_type)).fetchone()
        return row[0] if row else None

    @classmethod
    def get_mock_external(cls, case_id):
        with cls.connect() as conn:
            row = conn.execute("SELECT * FROM mock_external_data WHERE case_id = ?", (case_id,)).fetchone()
        return {"credit_report": json.loads(row["credit_report"]),
                "customer_replies": json.loads(row["customer_replies"])} if row else None

    # --- handoffs ---
    @classmethod
    def write_handoff(cls, case_id, stage, data):
        with cls.connect() as conn:
            conn.execute("INSERT INTO handoffs VALUES (?, ?, ?) ON CONFLICT(case_id, stage) "
                         "DO UPDATE SET data = excluded.data", (case_id, stage, json.dumps(data)))

    @classmethod
    def read_handoff(cls, case_id, stage):
        with cls.connect() as conn:
            row = conn.execute("SELECT data FROM handoffs WHERE case_id = ? AND stage = ?",
                               (case_id, stage)).fetchone()
        return json.loads(row[0]) if row else None

    # --- events ---
    @classmethod
    def insert_event(cls, case_id, agent_name, step_name, timestamp, input_data, output_data, result, notes):
        cls.init()
        with cls.connect() as conn:
            cur = conn.execute(
                "INSERT INTO event_log (case_id, agent_name, step_name, timestamp, input_data, output_data, "
                "result, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (case_id, agent_name, step_name, timestamp, json.dumps(input_data), json.dumps(output_data),
                 result, notes))
            return cur.lastrowid

    @classmethod
    def _events(cls, where: str, args: tuple):
        cls.init()
        with cls.connect() as conn:
            rows = conn.execute(f"SELECT * FROM event_log {where} ORDER BY event_id", args).fetchall()
        return [cls._row(r) for r in rows]

    @classmethod
    def get_events(cls, case_id):
        return cls._events("", ()) if case_id is None else cls._events("WHERE case_id = ?", (case_id,))

    @classmethod
    def get_events_after(cls, event_id):
        return cls._events("WHERE event_id > ?", (event_id,))

    @classmethod
    def clear_case(cls, case_id):
        with cls.connect() as conn:
            for t in ("event_log", "customer_tracker", "handoffs"):
                conn.execute(f"DELETE FROM {t} WHERE case_id = ?", (case_id,))

    # --- tracker ---
    @classmethod
    def upsert_tracker(cls, case_id, current_step, status, last_updated, message):
        cls.init()
        with cls.connect() as conn:
            conn.execute(
                "INSERT INTO customer_tracker (case_id, current_step, status, last_updated, message) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(case_id) DO UPDATE SET "
                "current_step = excluded.current_step, status = excluded.status, "
                "last_updated = excluded.last_updated, "
                "message = COALESCE(excluded.message, customer_tracker.message)",
                (case_id, current_step, status, last_updated, message))

    @classmethod
    def get_tracker(cls, case_id):
        cls.init()
        with cls.connect() as conn:
            row = conn.execute("SELECT * FROM customer_tracker WHERE case_id = ?", (case_id,)).fetchone()
        return dict(row) if row else None

    # --- generic ---
    @classmethod
    def select(cls, table, where, order, desc, limit):
        cls.init()
        q = f"SELECT * FROM {table}"
        if where:
            q += " WHERE " + " AND ".join(f"{k} = ?" for k in where)
        if order:
            q += f" ORDER BY {order} {'DESC' if desc else 'ASC'}"
        if limit:
            q += f" LIMIT {int(limit)}"
        with cls.connect() as conn:
            vals = [int(v) if isinstance(v, bool) else v for v in where.values()]
            return [cls._row(r) for r in conn.execute(q, vals)]

    @classmethod
    def insert(cls, table, rows):
        cls.init()
        out = []
        with cls.connect() as conn:
            for r in rows:
                r = cls._enc(r)
                cur = conn.execute(f"INSERT INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                                   list(r.values()))
                out.append(cls._row(conn.execute(f"SELECT * FROM {table} WHERE rowid = ?",
                                                 (cur.lastrowid,)).fetchone()))
        return out

    @classmethod
    def upsert(cls, table, rows, key):
        cls.init()
        with cls.connect() as conn:
            for r in rows:
                r = cls._enc(r)
                sets = ", ".join(f"{k} = excluded.{k}" for k in r if k != key)
                conn.execute(f"INSERT INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))}) "
                             f"ON CONFLICT({key}) DO UPDATE SET {sets}", list(r.values()))

    @classmethod
    def update(cls, table, where, fields):
        cls.init()
        fields = cls._enc(fields)
        with cls.connect() as conn:
            conn.execute(f"UPDATE {table} SET {', '.join(f'{k} = ?' for k in fields)} WHERE "
                         + " AND ".join(f"{k} = ?" for k in where), [*fields.values(), *where.values()])

    @classmethod
    def reset_demo(cls):
        cls.init()
        with cls.connect() as conn:
            for t in cls.ALL_TABLES:
                conn.execute(f"DELETE FROM {t}")

    @classmethod
    def check(cls):
        cls.init()
        return []
