"""AgentLens: monitoring, ranking and self-improvement on top of the bank's 3 agents.

    cd agentlens
    uvicorn agentlens:app --reload        # dashboard: http://127.0.0.1:8000/
                                          # bank pipeline + loan form: http://127.0.0.1:8000/bank/form

AgentLens only READS event_log (and case status) and WRITES its own tables. It touches the
agents only through replay. Promotion always needs a named person; demotion never does.
(The dashboard's "Application form" and "Documents" tabs call the BANK's endpoints under
/bank, which a human reviewer uses; AgentLens's own code never reads the vault.)
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

# Let a separate front end (e.g. a Lovable app, or one running on localhost) call this API.
# Override with AGENTLENS_CORS_ORIGINS="https://my-app.example,http://localhost:5173".
import os  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

_origins = [o.strip() for o in os.getenv("AGENTLENS_CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"],
    allow_origin_regex=None if _origins else
    r"^https://([a-z0-9-]+\.)*(lovable\.app|lovableproject\.com|lovable\.dev)$|^http://(localhost|127\.0\.0\.1)(:\d+)?$")


@app.middleware("http")
async def _private_network(request, call_next):
    """Chrome asks before an https site calls a server on this computer; say yes."""
    response = await call_next(request)
    if request.headers.get("access-control-request-private-network") == "true":
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

FINAL = access.FINAL_STATUSES
SEV_ORDER = ["CRITICAL", "MAJOR", "MINOR"]
STAGES = [("Intake", "ai.parse_request"), ("Documents", "ai.read_documents"), ("Income", "verify_income"),
          ("Credit file", "credit.pull_report"), ("Eligibility", "rules.eligibility"),
          ("Affordability", "calc.serviceability"), ("Policy", "rules.policy"), ("Decision", "ai.decide")]
AGENT_LABEL = {"intake_agent": "Intake agent", "verification_agent": "Verification agent",
               "decision_agent": "Decision agent", "notification_service": "Notification service",
               "orchestrator": "Orchestrator", "agentlens": "AgentLens", "access_layer": "Access layer",
               "human_reviewer": "Credit reviewer"}


@app.on_event("startup")
def _startup():
    db.init_db()
    store.licences()
    monitor.sync_fix_levels()
    pipeline.MONITOR = monitor.after_agent  # every case the bank processes is watched (form, API, batch)


# ---------------------------------------------------------------------------
# Background jobs (one at a time): the batch, a verify, a review, the incident
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
                                   "resolved", "timestamp")} | {"title": m.get("details", {}).get("title")}
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
# Readable event timeline (from event_log only)
# ---------------------------------------------------------------------------

def _money(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def _yes(x) -> str:
    return "Yes" if x else "No"


def _masked(counts: dict) -> str:
    return ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in (counts or {}).items()) or "nothing to hide"


SYSTEM_STEPS = {"access.issue_ticket", "case.write_handoff", "case.read_handoff", "crm.get_application"}


def describe(e: dict) -> dict:
    """One event as a plain-English line + key facts (no personal details)."""
    s, i, o = e["step_name"], e.get("input_data") or {}, e.get("output_data") or {}
    title, facts = s, []
    if s == "access.issue_ticket":
        title = f"Gave the {AGENT_LABEL.get(i.get('agent'), i.get('agent'))} a ticket for this case only"
        facts = [["Can read", ", ".join(o.get("read", []))], ["Can write", ", ".join(o.get("write", []))]]
    elif s == "crm.get_application":
        title = "Read the application form (no personal details)"
    elif s == "chat.get_message":
        title = "Read the customer's message with personal details hidden"
        facts = [["Hidden", _masked(o.get("masked"))]]
    elif s == "chat.ask_customer":
        title = f"Asked the customer: “{i.get('question')}”"
        facts = [["Customer replied", _yes(o.get("reply"))], ["Asked because", ", ".join(i.get("missing_fields", []))]]
    elif s == "ai.parse_request":
        loan = o.get("combined_with_form") or {}
        title = "AI read the request and pulled out the loan details"
        facts = [["Amount", _money(loan.get("loan_amount"))], ["Term", f"{loan.get('term_years') or '?'} years"],
                 ["Purpose", loan.get("purpose")],
                 ["Sensitive info", ", ".join(x["category"].replace("_", " ") for x in o.get("sensitive_info", [])) or "none"],
                 ["Done by", "AI" if o.get("parser") == "llm" else "rules (AI offline)"]]
    elif s == "case.write_handoff":
        title = "Handed the case to the next agent"
        facts = [["Passed on", ", ".join(i.get("fields", []))]]
    elif s == "case.read_handoff":
        title = "Picked up the case from the previous agent"
    elif s == "case.set_status":
        title = f"Moved the case to “{o.get('status')}”"
    elif s in ("docs.extract_payslip", "docs.extract_statements"):
        doc = "payslip" if s == "docs.extract_payslip" else "bank statement"
        title = f"Opened the {doc}" + ("" if o.get("redacted", True) else ": personal details NOT hidden")
        facts = [["Personal details hidden", _yes(o.get("redacted"))], ["Hidden", _masked(o.get("masked"))]]
    elif s == "docs.match_identity":
        title = "Checked the payslip belongs to the applicant"
        facts = [["Name matches", _yes(o.get("name_match"))]]
    elif s == "credit.pull_report":
        title = "Pulled the credit report"
        facts = [["Defaults", o.get("defaults")], ["Recent enquiries", o.get("enquiries")],
                 ["Credit score", o.get("credit_score") or "n/a"], ["Existing accounts", len(o.get("accounts", []))]]
    elif s == "ai.read_documents":
        ps = o.get("payslip") or {}
        title = "AI read the payslip and bank statement"
        facts = [["Pay frequency", ps.get("pay_frequency")], ["Gross pay", _money(ps.get("gross_pay"))],
                 ["Living expenses", f"{_money(o.get('monthly_living_expenses'))}/month"],
                 ["Debt repayments", f"{_money(o.get('monthly_debt_repayments'))}/month"],
                 ["Debts", o.get("liabilities_summary")],
                 ["Done by", "AI" if o.get("reader") == "llm" else "rules (AI offline)"]]
    elif s == "verify_income":
        title = "Checked the income"
        facts = [["Stated on the form", _money(i.get("stated_annual_income"))],
                 ["Payslip", f"{i.get('payslip_pay_frequency')} {_money(i.get('payslip_gross_pay'))}"],
                 ["Pay periods used", o.get("periods_per_year_used")],
                 ["Income used (a year)", _money(o.get("verified_annual_income"))]]
        if o.get("payslip_age_days") is not None:
            facts.append(["Payslip age", f"{o['payslip_age_days']} days"])
    elif s == "identity.verify":
        title = "Verified identity (FrankieOne mock)"
        facts = [["Identity verified", _yes(o.get("identity_verified"))], ["18 or over", _yes(o.get("age_over_18"))],
                 ["Checked against", ", ".join(o.get("sources_checked", []))]]
    elif s == "benchmarks.hem":
        title = "Looked up the living-cost benchmark (HEM)"
        facts = [["HEM", f"{_money(o.get('hem_monthly'))}/month"], ["Household", f"{o.get('household')}, {o.get('dependants')} dependant(s)"]]
    elif s == "rules.eligibility":
        title = "Checked eligibility"
        facts = [[k.replace("_", " ").capitalize(), _yes(v)] for k, v in (o.get("checks") or {}).items()]
    elif s == "calc.serviceability":
        title = "Worked out whether the loan is affordable"
        facts = [["Income used", f"{_money(o.get('monthly_income_used'))}/month"],
                 ["Living costs used", f"{_money(o.get('expenses_used'))} ({o.get('expenses_basis')})"],
                 ["Credit card", _money(o.get("credit_card_commitment"))], ["HECS-HELP", _money(o.get("hecs_commitment"))],
                 ["New repayment", f"{_money(o.get('new_monthly_repayment'))}/month"],
                 ["Debt-to-income", f"{(o.get('dti_ratio') or 0) * 100:.1f}% (limit 50%)"]]
    elif s == "rules.policy":
        title = "Checked lending policy"
        facts = [["Breaches", "; ".join(o.get("violations", [])) or "none"]]
    elif s == "ai.decide":
        title = f"AI decided: {str(o.get('final_decision', '')).replace('_', ' ')}"
        facts = [["Confidence", f"{o.get('confidence')}%" if o.get("confidence") is not None else "n/a"],
                 ["Borderline", _yes(o.get("borderline"))], ["Rules said", str(o.get("rules_decision", "")).replace("_", " ")],
                 ["Done by", "AI" if o.get("decided_by") == "llm" else "rules (AI offline)"]]
    elif s == "notify.send_template":
        title = "Told the customer (their name added by the notification service)"
        facts = [["Message", o.get("message_preview")]]
    elif s == "case.hold":
        title = "AgentLens held the case for a person"
        facts = [["Reason", i.get("reason")]]
    elif s == "case.review":
        title = f"Reviewed by {i.get('reviewer')}"
        facts = [["Outcome", o.get("status")], ["Note", i.get("note")]]
    elif s == "access.violation":
        title = "Access REFUSED: asked for data outside its ticket"
        facts = [["Asked for", i.get("requested_customer_token") or i.get("scope")],
                 ["Ticket was for", i.get("ticket_customer_token")]]
    elif s == "vault.view_identity":
        title = f"{i.get('viewer')} viewed the customer's {i.get('viewed')}"
    return {"event_id": e["event_id"], "agent": e["agent_name"], "agent_label": AGENT_LABEL.get(e["agent_name"], e["agent_name"]),
            "step": s, "title": title, "result": e["result"], "note": e.get("notes"), "time": str(e.get("timestamp")),
            "facts": [[k, v] for k, v in facts if v not in (None, "")], "system": s in SYSTEM_STEPS}


# ---------------------------------------------------------------------------
# Dashboard API
# ---------------------------------------------------------------------------

class BatchJob(BaseModel):
    count: int = Field(default=30, ge=22, le=80)


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


LIVE: dict[str, str] = {}  # case_id -> "queued" | "running" | "done" | error text (form submissions)


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

    def row(a):
        w = _worst(by_case.get(a["case_id"], []))
        return {"case_id": a["case_id"], "status": a["status"], "amount": a["requested_amount"],
                "purpose": a["purpose"], "term_years": a.get("term_years"), "decision": a.get("decision"),
                "submitted_at": str(a.get("submitted_at") or ""), "approval_probability": a.get("approval_probability"),
                "estimated_timeframe": a.get("estimated_timeframe"), "reviewed_by": a.get("reviewed_by"),
                "reviewed_at": str(a.get("reviewed_at") or ""), "flagged": a["case_id"] in by_case,
                "finding": (w or {}).get("details", {}).get("title"), "live": LIVE.get(a["case_id"])}

    ordered = sorted(apps, key=lambda a: (str(a.get("status_updated_at") or a.get("submitted_at") or ""), a["case_id"]),
                     reverse=True)
    reviewed = [row(a) for a in sorted((a for a in apps if a.get("reviewed_by")),
                                       key=lambda a: str(a.get("reviewed_at") or ""), reverse=True)]
    ranked = monitor.rank()
    return {
        "storage": db.backend_name(),
        "stats": {"total": len(apps), "processed": sum(1 for a in apps if a["status"] != "submitted"),
                  "cleared": len(cleared), "held": len(held), "reviewed": len(reviewed),
                  "held_by_severity": {s: sum(1 for q in queue if q["severity"] == s) for s in ("Critical", "High", "Medium")},
                  "waiting": sum(1 for a in apps if a["status"] == "submitted"),
                  "in_progress": sum(1 for a in apps if a["status"] in ("verifying", "deciding"))},
        "queue": queue,
        "agents": [{**r, "label": AGENT_LABEL.get(r["agent_name"], r["agent_name"])} for r in ranked],
        "suggestions": list(reversed(store.suggestions())),
        "history": db.select("licence_history", order="id", desc=True, limit=15),
        "job": dict(JOB),
        "statuses": {a["case_id"]: a["status"] for a in apps},
        "applications": [row(a) for a in ordered],
        "reviewed": reviewed,
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
    decide = steps.get("ai.decide")
    decision = None
    if decide:
        o = decide["output_data"]
        decision = {"decision": o.get("final_decision"), "reasoning": o.get("reasoning"), "confidence": o.get("confidence"),
                    "borderline": o.get("borderline"), "rules_decision": o.get("rules_decision"),
                    "decided_by": o.get("decided_by"), "note": decide.get("notes")}
    return {
        "case_id": case_id, "token": a["customer_token"], "status": a["status"], "amount": a["requested_amount"],
        "purpose": a["purpose"], "term_years": a["term_years"], "decision": a.get("decision"),
        "approval_probability": a.get("approval_probability"), "estimated_timeframe": a.get("estimated_timeframe"),
        "estimated_decision_by": str(a.get("estimated_decision_by") or ""), "submitted_at": str(a.get("submitted_at") or ""),
        "reviewed_by": a.get("reviewed_by"), "reviewed_at": str(a.get("reviewed_at") or ""),
        "primary": primary and {**primary, "agent_label": AGENT_LABEL.get(primary["agent_name"]),
                                "current_run": primary in current},
        "mistakes": [{**m, "agent_label": AGENT_LABEL.get(m["agent_name"]), "earlier_run": m["event_id"] not in ids,
                      "title": m.get("details", {}).get("title")} for m in ms],
        "chips": chips, "agent_decision": decision,
        "timeline": [describe(e) for e in events],
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


@app.post("/api/jobs/incident")
def job_incident():
    """Demo step 4: the decision agent approves despite a failed credit check -> CRITICAL -> back to L-plates."""
    def go():
        out = demo_seed.run_incident()
        JOB["final"] = f"{demo_seed.INCIDENT_CASE}: {out['status']}; the decision agent is back on L-plates"
    return _start("incident", go, 1, "Running the critical incident…")


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
