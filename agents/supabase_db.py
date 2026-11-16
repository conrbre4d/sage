"""Supabase backend, over Supabase's REST API (PostgREST) with httpx.

Needs in .env:
  SUPABASE_URL              https://YOUR-PROJECT.supabase.co
  SUPABASE_SECRET_KEY       the secret key (sb_secret_...) or the legacy service_role key.
                            Server-side only: it bypasses Row Level Security. Never put it
                            in a browser or commit it.

Tables + the reserve_event_ids() function come from supabase/schema.sql.
Same functions as db._local.
"""

from __future__ import annotations

import os
import re

import httpx

TABLES = ["applications", "customer_identity", "documents", "mock_external_data", "handoffs",
          "event_log", "customer_tracker", "agent_licence", "mistake_log", "licence_history",
          "improvement_suggestion", "agentlens_state"]

_client: httpx.Client | None = None


class SupabaseError(RuntimeError):
    pass


def _http() -> httpx.Client:
    global _client
    if _client is None:
        url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
        url = re.sub(r"/rest/v1$", "", url)  # accept the URL with or without /rest/v1
        key = (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
        if not url or not key:
            raise SupabaseError("set SUPABASE_URL and SUPABASE_SECRET_KEY in .env")
        headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):  # legacy JWT keys also go in Authorization
            headers["Authorization"] = f"Bearer {key}"
        _client = httpx.Client(base_url=f"{url}/rest/v1", headers=headers, timeout=30)
    return _client


def _req(method: str, path: str, params: dict | None = None, json=None, prefer: str | None = None):
    headers = {"Prefer": prefer} if prefer else {}
    try:
        r = _http().request(method, f"/{path}", params=params, json=json, headers=headers)
    except httpx.HTTPError as e:
        raise SupabaseError(f"could not reach Supabase ({e.__class__.__name__}: {e})") from e
    if r.status_code >= 400:
        hint = ""
        if r.status_code in (401, 403):
            hint = " (check SUPABASE_SECRET_KEY: it must be the secret/service_role key, not the publishable/anon key)"
        elif r.status_code == 404 or "does not exist" in r.text or "schema cache" in r.text or "PGRST202" in r.text:
            hint = " (run supabase/schema.sql again in the Supabase SQL editor)"
        raise SupabaseError(f"{method} {path} -> {r.status_code}: {r.text[:300]}{hint}")
    return r.json() if r.content else None


def _eq(value) -> str:
    if isinstance(value, bool):
        value = str(value).lower()
    return f"eq.{value}"


def _in(values) -> str:
    return "in.(" + ",".join(f'"{v}"' for v in values) + ")"


def init() -> None:
    pass  # tables are created by supabase/schema.sql


def check() -> list[str]:
    problems = []
    for t in TABLES:
        try:
            _req("GET", t, params={"select": "*", "limit": 1})
        except SupabaseError as e:
            problems.append(str(e))
            if "could not reach" in str(e) or "SUPABASE_" in str(e):
                return problems
    try:
        reserve_event_ids(1)
    except SupabaseError as e:
        problems.append(str(e))
    return problems


# --- applications ----------------------------------------------------------

def save_applications(cases):
    ids = [c["case_id"] for c in cases]
    for i in range(0, len(ids), 50):  # cascades to the other tables
        _req("DELETE", "applications", params={"case_id": _in(ids[i:i + 50])})
    _req("POST", "applications", json=[{"case_id": c["case_id"], "customer_token": c["customer_token"],
                                        **c["application"]} for c in cases], prefer="return=minimal,missing=default")
    _req("POST", "customer_identity", json=[{"customer_token": c["customer_token"], "case_id": c["case_id"],
                                             "identity_encrypted": c["identity_encrypted"],
                                             "customer_message_encrypted": c["message_encrypted"]} for c in cases],
         prefer="return=minimal")
    _req("POST", "documents", json=[{"case_id": c["case_id"], "doc_type": k, "content_encrypted": v}
                                    for c in cases for k, v in c["documents_encrypted"].items()],
         prefer="return=minimal")
    _req("POST", "mock_external_data", json=[{"case_id": c["case_id"], "credit_report": c["credit_report"],
                                              "customer_replies": c["customer_replies"]} for c in cases],
         prefer="return=minimal")


def get_application(case_id):
    rows = _req("GET", "applications", params={"select": "*", "case_id": _eq(case_id)})
    return rows[0] if rows else None


def list_applications(status):
    params = {"select": "*", "order": "submitted_at.asc,case_id.asc"}
    if status:
        params["status"] = _eq(status)
    return _req("GET", "applications", params=params)


def update_application(case_id, fields):
    _req("PATCH", "applications", params={"case_id": _eq(case_id)}, json=fields, prefer="return=minimal")


def get_identity_row(token):
    rows = _req("GET", "customer_identity", params={"select": "*", "customer_token": _eq(token)})
    return rows[0] if rows else None


def get_document(case_id, doc_type):
    rows = _req("GET", "documents", params={"select": "content_encrypted", "case_id": _eq(case_id),
                                            "doc_type": _eq(doc_type)})
    return rows[0]["content_encrypted"] if rows else None


def get_mock_external(case_id):
    rows = _req("GET", "mock_external_data", params={"select": "credit_report,customer_replies",
                                                     "case_id": _eq(case_id)})
    return rows[0] if rows else None


# --- event_log ---------------------------------------------------------------

def reserve_event_ids(n: int) -> list[int]:
    """Take n IDs from event_log's own sequence (never reused, safe across processes)."""
    out = _req("POST", "rpc/reserve_event_ids", json={"n": n})
    if isinstance(out, list):
        return [int(v["reserve_event_ids"] if isinstance(v, dict) else v) for v in out]
    raise SupabaseError(f"unexpected reserve_event_ids response: {str(out)[:100]}")


def insert_events(rows):
    _req("POST", "event_log", json=rows, prefer="return=minimal")


def insert_event(case_id, agent_name, step_name, timestamp, input_data, output_data, result, notes):
    """One event, ID from the database (the slow path if reserve_event_ids() isn't installed)."""
    rows = _req("POST", "event_log", params={"select": "event_id"},
                json={"case_id": case_id, "agent_name": agent_name, "step_name": step_name,
                      "timestamp": timestamp, "input_data": input_data, "output_data": output_data,
                      "result": result, "notes": notes},
                prefer="return=representation")
    return rows[0]["event_id"]


def get_events(case_id):
    params = {"select": "*", "order": "event_id.asc"}
    if case_id is not None:
        params["case_id"] = _eq(case_id)
    return _req("GET", "event_log", params=params)


def get_events_after(event_id):
    return _req("GET", "event_log", params={"select": "*", "order": "event_id.asc", "event_id": f"gt.{event_id}"})


def clear_case(case_id):
    for t in ("event_log", "customer_tracker", "handoffs"):
        _req("DELETE", t, params={"case_id": _eq(case_id)})


# --- handoffs ------------------------------------------------------------------

def write_handoff(case_id, stage, data):
    _req("POST", "handoffs", params={"on_conflict": "case_id,stage"},
         json={"case_id": case_id, "stage": stage, "data": data},
         prefer="resolution=merge-duplicates,return=minimal")


def read_handoff(case_id, stage):
    rows = _req("GET", "handoffs", params={"select": "data", "case_id": _eq(case_id), "stage": _eq(stage)})
    return rows[0]["data"] if rows else None


# --- customer_tracker ----------------------------------------------------------

def upsert_tracker(case_id, current_step, status, last_updated, message):
    row = {"case_id": case_id, "current_step": current_step, "status": status, "last_updated": last_updated}
    if message is not None:  # keep the last message when a step update has none
        row["message"] = message
    _req("POST", "customer_tracker", params={"on_conflict": "case_id"}, json=row,
         prefer="resolution=merge-duplicates,return=minimal")


def get_tracker(case_id):
    rows = _req("GET", "customer_tracker", params={"select": "*", "case_id": _eq(case_id)})
    return rows[0] if rows else None


# --- generic (AgentLens tables) -----------------------------------------------

def select(table, where, order, desc, limit):
    params = {"select": "*", **{k: _eq(v) for k, v in where.items()}}
    if order:
        params["order"] = f"{order}.{'desc' if desc else 'asc'}"
    if limit:
        params["limit"] = int(limit)
    return _req("GET", table, params=params)


def insert(table, rows):
    return _req("POST", table, json=rows, prefer="return=representation,missing=default")


def upsert(table, rows, key):
    _req("POST", table, params={"on_conflict": key}, json=rows,
         prefer="resolution=merge-duplicates,return=minimal,missing=default")


def update(table, where, fields):
    _req("PATCH", table, params={k: _eq(v) for k, v in where.items()}, json=fields, prefer="return=minimal")


def reset_demo():
    _req("DELETE", "applications", params={"case_id": "neq.__none__"})  # cascades
    for t, col in [("event_log", "event_id"), ("customer_tracker", "case_id"), ("mistake_log", "id"),
                   ("licence_history", "id"), ("improvement_suggestion", "id"),
                   ("agent_licence", "agent_name"), ("agentlens_state", "key")]:
        _req("DELETE", t, params={col: "not.is.null"})
