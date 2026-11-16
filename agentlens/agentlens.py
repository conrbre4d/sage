"""AgentLens: monitoring, ranking and self-improvement on top of the bank's 3 agents.

    cd agentlens
    uvicorn agentlens:app --reload        # dashboard: http://127.0.0.1:8000/
                                          # bank pipeline + loan form: http://127.0.0.1:8000/bank/form

AgentLens only READS event_log (and case status) and WRITES its own tables. It touches the
agents only through replay. Promotion always needs a named person; demotion never does.
"""

from __future__ import annotations

import json
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

import _paths  # noqa: F401
import access
import db
import demo_seed
import detect
import llm
import main as pipeline
import monitor
import scoring
import store

app = FastAPI(title="AgentLens", description="AI agents earn the right to work alone.")
app.mount("/bank", pipeline.app)  # the bank's pipeline API + loan form, same server

FINAL = access.FINAL_STATUSES
SEV_ORDER = ["CRITICAL", "MAJOR", "MINOR"]
STAGES = [("Intake", "ai.parse_request"), ("Documents", "ai.read_documents"), ("Income", "verify_income"),
          ("Credit file", "credit.pull_report"), ("Eligibility", "rules.eligibility"),
          ("Affordability", "calc.serviceability"), ("Policy", "rules.policy"), ("Decision", "ai.decide")]
AGENT_LABEL = {"intake_agent": "Intake agent", "verification_agent": "Verification agent",
               "decision_agent": "Decision agent"}


@app.on_event("startup")
def _startup():
    db.init_db()
    store.licences()
    monitor.sync_fix_levels()
    pipeline.MONITOR = monitor.after_agent  # every case the bank processes is watched (form, API, batch)


# ---------------------------------------------------------------------------
# Background jobs (one at a time): the batch, a verify, a review
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
JOB: dict = {"name": None, "running": False, "done": 0, "total": 0, "current": None, "message": "", "error": None}


def _start(name: str, fn, total: int = 0, message: str = "") -> dict:
    if not _job_lock.acquire(blocking=False):
        raise HTTPException(409, f"busy: {JOB['name']} is still running")
    JOB.update(name=name, running=True, done=0, total=total, current=None, message=message, error=None)

    def run():
        try:
            fn()
            JOB["message"] = JOB.get("final") or f"{name} finished"
        except Exception as e:  # shown on the dashboard
            JOB["error"] = f"{e.__class__.__name__}: {e}"
            traceback.print_exc()
        finally:
            JOB.update(running=False, current=None, final=None)
            _job_lock.release()

    threading.Thread(target=run, daemon=True).start()
    return dict(JOB)


def _progress(i, n, case_id):
    JOB.update(done=i, total=n, current=case_id,
               message=f"Agents working on {case_id} ({i + 1} of {n})" if case_id else f"All {n} applications processed")


# ---------------------------------------------------------------------------
# Spec API
# ---------------------------------------------------------------------------

class Approver(BaseModel):
    approved_by: str = Field(min_length=2)


class ApplyFix(BaseModel):
    approved_by: str = Field(min_length=2)
    fix_description: str = ""


class Replay(BaseModel):
    case_ids: list[str] | None = None


@app.get("/rank")
def rank():
    """All agents, worst to best."""
    return monitor.rank()


@app.get("/rank/{agent}")
def rank_agent(agent: str):
    row = next((r for r in monitor.rank() if r["agent_name"] == agent), None)
    if row is None:
        raise HTTPException(404, f"unknown agent {agent}")
    return {"agent_name": agent, "trust_score": row["trust_score"], "level": row["level"],
            "level_name": row["level_name"], "mistake_rate": row["mistake_rate"],
            "cases_at_level": row["cases_at_level"], "cases_needed_to_promote": row["cases_needed_to_promote"],
            "fast_track": row["fast_track"], "risk_owner": row["risk_owner"]}


@app.get("/mistakes/{agent}")
def mistakes(agent: str):
    """Full mistake log: what the agent said vs what should have happened."""
    return [{k: m.get(k) for k in ("id", "case_id", "event_id", "mistake_type", "mistake_reason",
                                   "agent_stated_reasoning", "what_should_have_happened", "rule", "pattern_key",
                                   "resolved", "timestamp")}
            for m in store.mistakes() if m["agent_name"] == agent]


@app.get("/improve/{agent}")
def improve_history(agent: str):
    """Every suggestion and iteration for this agent."""
    return [s for s in store.suggestions() if s["agent_name"] == agent]


@app.post("/licence/{agent}/promote")
def promote(agent: str, body: Approver):
    """Human-approved only."""
    try:
        return monitor.promote_agent(agent, body.approved_by)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/improve/{sid}/apply")
def apply_fix(sid: int, body: ApplyFix):
    try:
        return monitor.apply_suggestion(sid, body.approved_by, body.fix_description)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/improve/{sid}/verify")
def verify_fix(sid: int):
    """Replays every case this pattern failed on with the fixed agent (takes a while)."""
    with _job_lock:
        try:
            return monitor.verify_suggestion(sid)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))


@app.post("/replay/{agent}")
def replay(agent: str, body: Replay | None = None):
    case_ids = (body.case_ids if body else None) or sorted(
        {m["case_id"] for m in store.mistakes() if m["agent_name"] == agent and not m.get("resolved")})
    with _job_lock:
        results = monitor.replay(agent, case_ids)
    return {c: {"mistakes": len(ms), "passed": not ms} for c, ms in results.items()}


@app.post("/scan")
def scan():
    out = monitor.scan()
    return {"events": out["events"], "mistakes": len(out["mistakes"]), "held": out["held"]}


# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------

class BatchJob(BaseModel):
    count: int = Field(default=30, ge=23, le=80)


class ReviewJob(BaseModel):
    action: str = Field(pattern="^(recalculate|approve|decline)$")
    reviewer: str = "Credit reviewer"


def _worst(ms: list[dict]) -> dict | None:
    return sorted(ms, key=lambda m: (SEV_ORDER.index(m["mistake_type"]), -int(m["id"])))[0] if ms else None


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/overview")
def overview():
    apps = db.list_applications()
    ms = store.mistakes()
    by_case: dict[str, list] = {}
    for m in ms:
        by_case.setdefault(m["case_id"], []).append(m)
    held = [a for a in apps if a["status"] == "held"]
    queue = []
    for a in held:
        w = _worst(by_case.get(a["case_id"], []))
        queue.append({"case_id": a["case_id"], "amount": a["requested_amount"], "token": a["customer_token"],
                      "title": (w or {}).get("details", {}).get("title", "Held by the access layer"),
                      "severity": detect.SEVERITY_LABEL.get((w or {}).get("mistake_type"), "High"),
                      "agent": AGENT_LABEL.get((w or {}).get("agent_name"), "Access layer")})
    queue.sort(key=lambda q: (["Critical", "High", "Medium"].index(q["severity"]), q["case_id"]))
    final = [a for a in apps if a["status"] in FINAL]
    cleared = [a for a in final if a["case_id"] not in by_case]
    ranked = monitor.rank()
    hist = db.select("licence_history", order="id", desc=True, limit=12)
    return {
        "storage": db.backend_name(),
        "stats": {"total": len(apps), "processed": sum(1 for a in apps if a["status"] != "submitted"),
                  "cleared": len(cleared), "reviewed": len(final) - len(cleared), "held": len(held),
                  "held_by_severity": {s: sum(1 for q in queue if q["severity"] == s) for s in ("Critical", "High", "Medium")},
                  "waiting": sum(1 for a in apps if a["status"] == "submitted")},
        "queue": queue,
        "agents": [{**r, "label": AGENT_LABEL.get(r["agent_name"], r["agent_name"])} for r in ranked],
        "suggestions": list(reversed(store.suggestions())),
        "history": hist,
        "job": dict(JOB),
        "statuses": {a["case_id"]: a["status"] for a in apps},
        "applications": [{"case_id": a["case_id"], "status": a["status"], "amount": a["requested_amount"],
                          "purpose": a["purpose"], "decision": a.get("decision"),
                          "flagged": a["case_id"] in by_case, "live": LIVE.get(a["case_id"])}
                         for a in sorted(apps, key=lambda a: (str(a.get("submitted_at") or ""), a["case_id"]),
                                         reverse=True)][:120],
    }


@app.get("/api/case/{case_id}")
def case_detail(case_id: str):
    a = db.get_application(case_id)
    if a is None:
        raise HTTPException(404, f"no application {case_id}")
    events = db.get_events(case_id)
    ids = {e["event_id"] for e in events}
    ms = sorted([m for m in store.mistakes() if m["case_id"] == case_id], key=lambda m: -int(m["id"]))
    current = [m for m in ms if m["event_id"] in ids]
    primary = _worst(current) or _worst(ms)
    wrong = {s for m in current for s in m.get("details", {}).get("wrong_steps", [])}
    steps = {e["step_name"]: e for e in events}
    chips = [{"label": label, "state": "bad" if step in wrong else ("ok" if step in steps else "skip"),
              "result": steps[step]["result"] if step in steps else None} for label, step in STAGES]
    return {
        "case_id": case_id, "token": a["customer_token"], "status": a["status"], "amount": a["requested_amount"],
        "purpose": a["purpose"], "term_years": a["term_years"], "decision": a.get("decision"),
        "primary": primary and {**primary, "agent_label": AGENT_LABEL.get(primary["agent_name"]),
                                "current_run": primary in current},
        "mistakes": ms, "chips": chips,
        "events": [{k: e[k] for k in ("event_id", "agent_name", "step_name", "result", "notes")} for e in events],
    }


EXPLAIN_SYSTEM = ("You explain an AI agent's mistake to a bank credit reviewer in plain English. "
                  "explanation: ONE short sentence on what the agent did wrong and why it matters. "
                  "action: a short button label (max 6 words) for the fix. No names or IDs.")
EXPLAIN_SCHEMA = {"type": "object", "properties": {"explanation": {"type": "string"}, "action": {"type": "string"}},
                  "required": ["explanation", "action"], "additionalProperties": False}


@app.get("/api/case/{case_id}/explain")
def explain(case_id: str):
    ms = [m for m in store.mistakes() if m["case_id"] == case_id]
    m = _worst(ms)
    if m is None:
        return {"explanation": "AgentLens found nothing wrong with this case.", "action": None, "by": "rules"}
    facts = {"title": m["details"].get("title"), "agent": m["agent_name"], "reason": m["mistake_reason"],
             "should": m["what_should_have_happened"]}
    try:
        out, _ = llm.chat_json(EXPLAIN_SYSTEM, json.dumps(facts), EXPLAIN_SCHEMA, "mistake_explanation", max_tokens=120)
        return {**out, "by": "ai"}
    except llm.LLMError:
        return {**detect.explain_fallback(m), "by": "rules"}


@app.get("/api/agent/{agent}")
def agent_detail(agent: str):
    return {"history": [h for h in store.history(agent)][-30:],
            "mistakes": mistakes(agent)[-30:], "suggestions": improve_history(agent)}


LIVE: dict[str, str] = {}  # case_id -> "queued" | "running" | "done" | error text (form submissions)


@app.post("/api/applications")
def submit_form(body: pipeline.LoanForm):
    """The loan form: store the application (identity encrypted), then run the agents in the
    background with AgentLens watching. The dashboard shows it live."""
    data = body.model_dump()
    message, replies = data.pop("customer_message"), data.pop("customer_replies")
    case_id = pipeline.submissions.submit_application(data, message, customer_replies=replies)
    LIVE[case_id] = "queued"

    def go():
        LIVE[case_id] = "running"
        try:
            monitor.process(case_id)  # waits its turn if a batch is running
            LIVE[case_id] = "done"
        except Exception as e:
            LIVE[case_id] = f"error: {e}"
            traceback.print_exc()
    threading.Thread(target=go, daemon=True).start()
    return {"case_id": case_id, "status": "submitted", "dashboard": f"/dashboard?case={case_id}"}


@app.post("/api/jobs/process_waiting")
def process_waiting():
    """Run the agents on every application still 'submitted' (e.g. sent to the bank API without processing)."""
    waiting = [a["case_id"] for a in db.list_applications("submitted")]
    if not waiting:
        raise HTTPException(404, "no applications waiting")

    def go():
        for i, cid in enumerate(waiting):
            _progress(i, len(waiting), cid)
            monitor.process(cid)
        JOB["final"] = f"Processed {len(waiting)} waiting application(s)"
    return _start("process", go, len(waiting), f"Processing {len(waiting)} waiting application(s)…")


@app.post("/api/jobs/batch")
def job_batch(body: BatchJob):
    def go():
        ids = demo_seed.run_batch(body.count, _progress)
        JOB["final"] = f"All {len(ids)} applications processed"
    return _start("batch", go, body.count, f"Submitting {body.count} applications…")


@app.post("/api/jobs/verify/{sid}")
def job_verify(sid: int):
    s = store.get_suggestion(sid)
    if s is None:
        raise HTTPException(404, f"no suggestion {sid}")
    if s["status"] != "testing":
        raise HTTPException(400, f"suggestion {sid} is '{s['status']}': apply it first")

    def go():
        r = monitor.verify_suggestion(sid)
        JOB["final"] = (f"Fix #{sid}: all {len(r['passed'])} replayed cases pass, resolved" if r["result"] == "resolved"
                        else f"Fix #{sid}: {len(r['passed'])} passed, {len(r['failed'])} still failing "
                             f"({', '.join(r['failed'])}), so suggestion #{r['next_suggestion']['id']} was created")
    return _start("verify", go, len(s["case_ids"]), f"Replaying {len(s['case_ids'])} cases with fix #{sid}…")


@app.post("/api/jobs/review/{case_id}")
def job_review(case_id: str, body: ReviewJob):
    a = db.get_application(case_id)
    if a is None or a["status"] != "held":
        raise HTTPException(400, f"{case_id} is not held")

    def go():
        out = monitor.review_case(case_id, body.action, body.reviewer)
        JOB["final"] = f"{case_id}: {body.action} -> {out['status']}"
    return _start("review", go, 1, f"{body.action.capitalize()} {case_id}…")


@app.get("/api/levels")
def levels():
    return {"levels": scoring.LEVELS, "names": scoring.LEVEL_NAME, "promote_after": scoring.PROMOTE_AFTER,
            "fast_track": scoring.FAST_TRACK_CASES, "review_rate": scoring.HUMAN_REVIEW_RATE}
