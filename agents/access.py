"""Least-privilege data access: the layer every tool goes through.

Four layers of restriction:
  1. No database access for agents. Agents have no connection or credentials; only
     tools (tools.py) call db.py, and every tool calls check() first.
  2. Case-scoped. The orchestrator issues a Ticket for ONE case/token to ONE agent.
     Agents can't make their own: a ticket is only valid while it's in _ISSUED.
  3. Field-scoped. Each agent has an allowlist of what it may read and write.
  4. Status-gated. An agent can only act while the case is at its stage, and a
     'held' case can only be changed by a human reviewer.

Anything outside that is refused, raised as AccessViolation, and logged to the
event log as step 'access.violation' (AgentLens rule DP4: critical -> back to L-plates for the
agent and hold the case).

Production version: an access role per agent, row-level security in the database,
and short-lived credentials issued per case. This is the hackathon version.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import db
import estimates

# Case status flow: submitted -> verifying -> deciding -> approved / declined / referred
FINAL_STATUSES = {"approved", "declined", "referred"}
STATUSES = ["submitted", "verifying", "deciding", *sorted(FINAL_STATUSES), "held", "error"]

PERMISSIONS = {
    "intake_agent": {
        "read": {"application", "customer_message", "customer_chat"},  # message arrives redacted
        "write": {"handoff_intake", "status"},
        "stage": {"submitted"}, "next_status": {"verifying"},
        "never": "vault, documents, credit file",
    },
    "verification_agent": {
        "read": {"application", "handoff_intake", "documents", "credit_report", "identity_check",
                 "benchmarks", "rules"},  # documents arrive redacted
        "write": {"handoff_verification", "status"},
        "stage": {"verifying"}, "next_status": {"deciding"},
        "never": "vault, other cases, decisions",
    },
    "decision_agent": {
        "read": {"handoff_verification"},
        "write": {"handoff_decision", "status"},
        "stage": {"deciding"}, "next_status": FINAL_STATUSES,
        "never": "vault, raw documents, other cases",
    },
    "notification_service": {  # plain code, not an AI
        "read": {"status", "contact_details", "handoff_decision"},
        "write": {"sent_message"},
        "stage": FINAL_STATUSES, "next_status": set(),
        "never": "financial figures it doesn't need",
    },
}
# AgentLens (built separately) reads: event_log, handoffs, statuses. Writes: flags,
# holds (place_hold below), trust scores. Never the vault.


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    case_id: str
    customer_token: str
    agent: str


class AccessViolation(PermissionError):
    pass


_ISSUED: dict[str, Ticket] = {}


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=10))).isoformat(timespec="milliseconds")


def issue_ticket(case_id: str, agent: str) -> Ticket:
    """Orchestrator only: a ticket for one agent on one case."""
    if agent not in PERMISSIONS:
        raise ValueError(f"unknown agent {agent}")
    app = db.get_application(case_id)
    if app is None:
        raise ValueError(f"no submitted application for {case_id}")
    t = Ticket(secrets.token_hex(6), case_id, app["customer_token"], agent)
    _ISSUED[t.ticket_id] = t
    p = PERMISSIONS[agent]
    db.insert_event(case_id, "orchestrator", "access.issue_ticket", now_iso(),
                    {"agent": agent, "customer_token": t.customer_token},
                    {"ticket_id": t.ticket_id, "read": sorted(p["read"]), "write": sorted(p["write"]),
                     "stage": sorted(p["stage"]), "never": p["never"]},
                    "pass", f"ticket for {agent} on {t.customer_token} only")
    return t


def revoke(ticket: Ticket) -> None:
    _ISSUED.pop(ticket.ticket_id, None)


def _violation(ticket, scope: str, action: str, problem: str, requested_token: str | None) -> None:
    agent = getattr(ticket, "agent", "unknown")
    case_id = getattr(ticket, "case_id", "unknown")
    db.insert_event(case_id, agent, "access.violation", now_iso(),
                    {"ticket_id": getattr(ticket, "ticket_id", None), "action": action, "scope": scope,
                     "requested_customer_token": requested_token,
                     "ticket_customer_token": getattr(ticket, "customer_token", None)},
                    {"refused": True, "rule": "DP4", "severity": "critical"},
                    "fail", f"DP4 refused: {agent} {problem}")


def check(ticket: Ticket, scope: str, action: str = "read", customer_token: str | None = None) -> dict:
    """Every tool calls this first. Returns the case's application row, or raises AccessViolation."""
    problem, app = None, None
    if not isinstance(ticket, Ticket) or _ISSUED.get(ticket.ticket_id) != ticket:
        problem = "used no valid ticket (agents can't make their own)"
    else:
        p = PERMISSIONS[ticket.agent]
        if customer_token is not None and customer_token != ticket.customer_token:
            problem = f"asked for {customer_token}'s {scope}, but its ticket is for {ticket.customer_token}"
        elif scope not in p[action]:
            problem = f"may not {action} '{scope}'"
        else:
            app = db.get_application(ticket.case_id)
            if app["status"] == "held":
                problem = "tried to act on a held case (only a human reviewer can change it)"
            elif app["status"] not in p["stage"]:
                problem = f"acted at status '{app['status']}', outside its stage ({'/'.join(sorted(p['stage']))})"
    if problem:
        _violation(ticket, scope, action, problem, customer_token)
        raise AccessViolation(f"DP4: {getattr(ticket, 'agent', 'unknown')} {problem}")
    return app


# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------

TRACKER_LABEL = {
    "submitted": ("Application received", "submitted"),
    "verifying": ("Checking your details", "in_progress"),
    "deciding": ("Making a decision", "in_progress"),
    "approved": ("Decision made", "approved"),
    "declined": ("Decision made", "declined"),
    "referred": ("Decision made", "with_a_person"),
    "held": ("A person is reviewing your application", "with_a_person"),
}


def set_status(ticket: Ticket, new_status: str) -> dict:
    """An agent moves its case to the next stage (and only the next stage)."""
    check(ticket, "status", "write")
    if new_status not in PERMISSIONS[ticket.agent]["next_status"]:
        problem = f"tried to set status '{new_status}' (allowed: {sorted(PERMISSIONS[ticket.agent]['next_status'])})"
        _violation(ticket, "status", "write", problem, None)
        raise AccessViolation(f"DP4: {ticket.agent} {problem}")
    db.update_application(ticket.case_id, status_fields(ticket.case_id, new_status))
    step, status = TRACKER_LABEL[new_status]
    db.upsert_tracker(ticket.case_id, step, status, now_iso())
    return {"status": new_status}


def status_fields(case_id: str, status: str) -> dict:
    """The new status plus the updated time estimate and approval probability (stored on the application)."""
    app = db.get_application(case_id) or {}
    verification = db.read_handoff(case_id, "verification") if status == "deciding" else None
    return {"status": status, **estimates.for_status(status, app, verification)}


def place_hold(case_id: str, reason: str, by: str = "agentlens") -> None:
    """AgentLens (or a person) holds a case. Agents can't touch it until a human reviewer releases it."""
    db.update_application(case_id, status_fields(case_id, "held"))
    step, status = TRACKER_LABEL["held"]
    db.upsert_tracker(case_id, step, status, now_iso())
    db.insert_event(case_id, by, "case.hold", now_iso(), {"reason": reason}, {"status": "held"},
                    "flag", f"case held: {reason}")
    db.flush_events()


def human_review(case_id: str, reviewer: str, new_status: str, note: str = "") -> None:
    """Only a human reviewer can change a held case."""
    if new_status not in STATUSES:
        raise ValueError(f"unknown status {new_status}")
    fields = status_fields(case_id, new_status)
    if reviewer != "AgentLens replay":  # a real person
        fields |= {"reviewed_by": reviewer, "reviewed_at": now_iso()}
    db.update_application(case_id, fields)
    if new_status in TRACKER_LABEL:
        step, status = TRACKER_LABEL[new_status]
        db.upsert_tracker(case_id, step, status, now_iso())
    db.insert_event(case_id, "human_reviewer", "case.review", now_iso(), {"reviewer": reviewer, "note": note},
                    {"status": new_status}, "pass", f"reviewed by a person: {new_status}")
    db.flush_events()


def reset_for_rerun(case_id: str) -> None:
    """Orchestrator/admin: put a (non-held) case back to 'submitted' so it can be processed again."""
    app = db.get_application(case_id)
    if app["status"] == "held":
        raise AccessViolation(f"{case_id} is held; only a human reviewer can release it")
    db.update_application(case_id, {**status_fields(case_id, "submitted"), "decision": None,
                                    "decision_confidence": None, "decided_at": None})
