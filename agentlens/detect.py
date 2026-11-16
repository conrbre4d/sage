"""Mistake detection: pure functions over event_log rows. No database, no network.

check_event(event, prior_events) -> list of mistakes for that event, where
prior_events are the same case's earlier events. Each mistake is a dict ready for
the mistake_log table:

  agent_name, case_id, event_id, mistake_type (MINOR | MAJOR | CRITICAL),
  mistake_reason, agent_stated_reasoning, what_should_have_happened,
  rule, pattern_key, details (what the dashboard shows)

Rules (from the AgentLens spec):
  1. wrong_conclusion      recalculate from input_data with the rule the agent should follow -> MAJOR
  2. missing_reasoning     a decision step with no notes -> MINOR
  3. skipped_step          a step ran before a step it depends on -> MAJOR
  4. ignored_failed_check  the case was approved although an earlier check failed -> CRITICAL
  5. repeated_mistake      the same wrong conclusion on 4+ cases in 24h (see monitor.py) -> CRITICAL
  plus the privacy/access rules:
  DP3 pii_to_ai            an AI step was sent personal details -> CRITICAL
  DP4 access_violation     an agent asked for data outside its ticket -> CRITICAL
"""

from __future__ import annotations

import json
import re

import _paths  # noqa: F401
import tools  # constants + PII patterns only (never the vault)

AUDITED_AGENTS = ["intake_agent", "verification_agent", "decision_agent"]
PERIODS = tools.PAY_PERIODS_PER_YEAR
MAX_ENQUIRIES = 3
MAX_PAYSLIP_AGE_DAYS = 60
BORDERLINE_POINTS = 1.0
MIN_APPROVE_CONFIDENCE = 70

DECISION_STEPS = {"ai.parse_request", "ai.read_documents", "verify_income", "credit.pull_report",
                  "rules.eligibility", "calc.serviceability", "rules.policy", "ai.decide",
                  "docs.match_identity", "identity.verify"}
REQUIRED_BEFORE = {
    "ai.read_documents": ["docs.extract_payslip", "docs.extract_statements"],
    "verify_income": ["ai.read_documents"],
    "rules.eligibility": ["identity.verify"],
    "calc.serviceability": ["verify_income", "credit.pull_report", "benchmarks.hem"],
    "ai.decide": ["verify_income", "credit.pull_report", "rules.eligibility", "calc.serviceability",
                  "rules.policy"],
}
STEP_LABEL = {
    "ai.parse_request": "Intake", "ai.read_documents": "Documents", "verify_income": "Income",
    "credit.pull_report": "Credit file", "rules.eligibility": "Eligibility",
    "calc.serviceability": "Affordability", "rules.policy": "Policy", "ai.decide": "Decision",
    "identity.verify": "Identity", "docs.match_identity": "Documents", "benchmarks.hem": "Expenses",
}
SEVERITY_LABEL = {"CRITICAL": "Critical", "MAJOR": "High", "MINOR": "Medium"}


def money(x) -> str:
    return f"${x:,.0f}"


def short_money(x) -> str:
    return f"${x / 1000:,.0f}k" if x >= 1000 else money(x)


def _mistake(event: dict, mistake_type: str, rule: str, pattern_key: str, reason: str, should: str,
             title: str, left: dict, right: dict, wrong_steps: list[str] | None = None, stated: str | None = None,
             **extra) -> dict:
    return {
        "agent_name": event["agent_name"], "case_id": event["case_id"], "event_id": event["event_id"],
        "mistake_type": mistake_type, "mistake_reason": reason,
        "agent_stated_reasoning": stated if stated is not None else (event.get("notes") or "(no reasoning given)"),
        "what_should_have_happened": should, "rule": rule, "pattern_key": pattern_key,
        "details": {"title": title, "severity": SEVERITY_LABEL[mistake_type], "step": event["step_name"],
                    "left": left, "right": right, "wrong_steps": wrong_steps or [event["step_name"]], **extra},
    }


def _last(prior: list[dict], step: str) -> dict | None:
    return next((e for e in reversed(prior) if e["step_name"] == step), None)


def _sent_text(event: dict) -> str:
    sent = (event.get("input_data") or {}).get("text_sent_to_llm")
    if isinstance(sent, list):
        return "\n".join(str(s) for s in sent)
    return str(sent or "")


def document_kind(prior: list[dict]) -> str:
    """What the verification AI was reading: a normal payslip or a self-employed income statement."""
    read = _last(prior, "ai.read_documents")
    text = _sent_text(read) if read else ""
    return "self-employed income statement" if "SELF-EMPLOYED" in text.upper() else "payslip"


# ---------------------------------------------------------------------------
# Rule 1: wrong conclusion (recalculate from input_data)
# ---------------------------------------------------------------------------

def _verify_income(e: dict, prior: list[dict]) -> list[dict]:
    inp, out = e["input_data"], e["output_data"]
    out_found = []
    freq, gross, used_annual = inp.get("payslip_pay_frequency"), inp.get("payslip_gross_pay"), out.get("verified_annual_income")
    if freq in PERIODS and gross and used_annual is not None:
        expected = gross * PERIODS[freq]
        if abs(used_annual - expected) > 0.01 * expected:
            used = out.get("pay_frequency_used") or "?"
            kind = document_kind(prior)
            ratio = used_annual / expected if expected else 0
            out_found.append(_mistake(
                e, "MAJOR", "wrong_conclusion", "misreads_field:pay_frequency",
                f"Agent concluded income={money(used_annual)} but the {kind} shows {freq} pay of {money(gross)} "
                f"(= {money(expected)}/yr) — treated {freq} as {used}.",
                f"Annualise {freq} pay: {money(gross)} x {PERIODS[freq]} = {money(expected)} a year.",
                "Income doubled" if 1.9 < ratio < 2.1 else "Income misread",
                {"label": f"Income on the {kind}", "value": money(expected), "sub": f"{freq} pay of {money(gross)}"},
                {"label": "Income the agent used", "value": money(used_annual), "sub": f"read as {used} ({ratio:.1f}x)"},
                ["verify_income", "calc.serviceability"],
                stated=f"{e.get('notes')} — used {out.get('periods_per_year_used')} pay periods a year",
                document_kind=kind))
    age = out.get("payslip_age_days")
    if age is not None and age > MAX_PAYSLIP_AGE_DAYS and e["result"] == "pass":
        out_found.append(_mistake(
            e, "MAJOR", "wrong_conclusion", "accepts_stale:payslip",
            f"Agent accepted a payslip that ended {inp.get('payslip_period_end')}, {age} days before the assessment; "
            f"the limit is {MAX_PAYSLIP_AGE_DAYS} days.",
            f"Flag the case and ask for a payslip from the last {MAX_PAYSLIP_AGE_DAYS} days.",
            "Old payslip",
            {"label": "Payslip period ended", "value": f"{age} days ago", "sub": str(inp.get("payslip_period_end"))},
            {"label": "What the agent said", "value": "Pass", "sub": f"limit is {MAX_PAYSLIP_AGE_DAYS} days"}))
    return out_found


def _credit(e: dict, prior: list[dict]) -> list[dict]:
    out = e["output_data"]
    if "enquiries" not in out:
        return []
    expected = "fail" if out.get("defaults", 0) > 0 else "flag" if out["enquiries"] > MAX_ENQUIRIES else "pass"
    rank = {"pass": 0, "flag": 1, "fail": 2}
    if rank[e["result"]] >= rank[expected]:
        return []
    field = "defaults" if expected == "fail" else "enquiries"
    if field == "enquiries":
        return [_mistake(
            e, "MAJOR", "wrong_conclusion", "misreads_field:enquiries",
            f"Agent marked the credit check '{e['result']}' but the report shows {out['enquiries']} recent "
            f"enquiries (limit {MAX_ENQUIRIES}).",
            f"Mark the credit check 'flag' so a person looks at {out['enquiries']} recent credit applications.",
            "Many credit checks",
            {"label": "Recent credit enquiries", "value": str(out["enquiries"]), "sub": f"limit {MAX_ENQUIRIES}"},
            {"label": "What the agent reported", "value": e["result"].upper(), "sub": "should be FLAG"})]
    return [_mistake(
        e, "MAJOR", "wrong_conclusion", "misreads_field:defaults",
        f"Agent marked the credit check '{e['result']}' but the report shows {out['defaults']} default(s).",
        "Mark the credit check 'fail'.", "Default missed",
        {"label": "Defaults on file", "value": str(out["defaults"]), "sub": "any default fails"},
        {"label": "What the agent reported", "value": e["result"].upper(), "sub": "should be FAIL"})]


def _serviceability(e: dict, prior: list[dict]) -> list[dict]:
    inp, out = e["input_data"], e["output_data"]
    found = []
    credit = _last(prior, "credit.pull_report")
    read = _last(prior, "ai.read_documents")
    bureau_limit = sum(a.get("limit") or 0 for a in (credit or {}).get("output_data", {}).get("accounts", [])
                       if a.get("type") == "credit_card")
    hecs_on_doc = bool((read or {}).get("output_data", {}).get("payslip", {}).get("hecs_help_withheld"))
    used_limit, used_hecs = inp.get("credit_card_limit") or 0, bool(inp.get("has_hecs"))
    missing = []
    if bureau_limit > used_limit:
        missing.append(f"card {short_money(bureau_limit)}")
    if hecs_on_doc and not used_hecs:
        missing.append("HECS-HELP")
    if missing:
        n_file = (1 if bureau_limit else 0) + (1 if hecs_on_doc else 0)
        n_used = (1 if used_limit else 0) + (1 if used_hecs else 0)
        overstated = (bureau_limit - used_limit) * tools.CREDIT_CARD_COMMITMENT_RATE + \
            (tools.HECS_MONTHLY_ALLOWANCE if hecs_on_doc and not used_hecs else 0)
        debts_file = " · ".join(([f"Card {short_money(bureau_limit)}"] if bureau_limit else []) +
                                (["HECS-HELP"] if hecs_on_doc else []))
        found.append(_mistake(
            e, "MAJOR", "wrong_conclusion", "misreads_field:debts",
            f"Agent passed on {n_used} debt(s) but the credit file and payslip show {n_file}: "
            f"{', '.join(missing)} left out, so the surplus is overstated by {money(overstated)}/month.",
            "Include every debt from the credit file and payslip in the repayment maths, then recalculate.",
            "Debts left out of the repayment maths",
            {"label": "Debts in credit file", "value": str(n_file), "sub": debts_file},
            {"label": "Debts the agent used", "value": str(n_used), "sub": f"surplus overstated by {money(overstated)}/mo"},
            ["ai.read_documents", "calc.serviceability"],
            action_hint="Recalculate with all debts"))
    # recompute the agent's own maths from its inputs
    try:
        expenses = max(inp["living_expenses"], inp["hem_monthly"])
        repayment = inp["loan_amount"] / (inp["term_years"] * 12) * tools.INTEREST_LOADING
        commitments = expenses + used_limit * tools.CREDIT_CARD_COMMITMENT_RATE + \
            (tools.HECS_MONTHLY_ALLOWANCE if used_hecs else 0)
        dti = (commitments + repayment) / inp["monthly_income"]
        if (dti <= tools.MAX_DTI) != bool(out.get("serviceable")):
            found.append(_mistake(
                e, "MAJOR", "wrong_conclusion", "misreads_field:dti",
                f"Agent said serviceable={out.get('serviceable')} but its own inputs give a DTI of {dti:.1%}.",
                f"DTI {dti:.1%} {'is within' if dti <= tools.MAX_DTI else 'exceeds'} the 50% limit.",
                "Affordability miscalculated",
                {"label": "DTI from its inputs", "value": f"{dti:.1%}", "sub": "limit 50%"},
                {"label": "What the agent said", "value": "Serviceable" if out.get("serviceable") else "Not serviceable",
                 "sub": f"reported {out.get('dti_ratio')}"}))
    except (KeyError, TypeError, ZeroDivisionError):
        pass
    return found


def _eligibility(e: dict, prior: list[dict]) -> list[dict]:
    inp = e["input_data"]
    if "age_over_18" not in inp:
        return []
    expected = ("fail" if not inp.get("age_over_18") else
                "flag" if not (inp.get("identity_verified") and inp.get("existing_customer")
                               and inp.get("residency") == "AU") else "pass")
    rank = {"pass": 0, "flag": 1, "fail": 2}
    if rank[e["result"]] >= rank[expected]:
        return []
    return [_mistake(
        e, "MAJOR", "wrong_conclusion", "misreads_field:eligibility",
        f"Agent marked eligibility '{e['result']}' but its inputs require '{expected}'.",
        f"Mark eligibility '{expected}'.", "Eligibility missed",
        {"label": "Required result", "value": expected.upper(), "sub": "from the eligibility rules"},
        {"label": "What the agent reported", "value": e["result"].upper(), "sub": ""})]


def _policy(e: dict, prior: list[dict]) -> list[dict]:
    inp = e["input_data"]
    if "loan_amount" not in inp:
        return []
    breaches = (inp["loan_amount"] > tools.PERSONAL_LOAN_CAP or (inp.get("term_years") or 0) > tools.MAX_TERM_YEARS
                or (inp.get("purpose") == "debt consolidation" and not inp.get("has_existing_debts")))
    if breaches and e["result"] != "fail":
        return [_mistake(
            e, "MAJOR", "wrong_conclusion", "misreads_field:policy",
            "Agent passed the lending policy check but the loan breaches policy.",
            "Mark the policy check 'fail'.", "Policy breach missed",
            {"label": "Loan", "value": money(inp["loan_amount"]), "sub": f"{inp.get('term_years')} years, {inp.get('purpose')}"},
            {"label": "What the agent reported", "value": e["result"].upper(), "sub": "should be FAIL"})]
    return []


def _decide(e: dict, prior: list[dict]) -> list[dict]:
    out = e["output_data"]
    if out.get("final_decision") != "approved":
        return []
    try:
        facts = json.loads(e["input_data"].get("text_sent_to_llm") or "{}")
    except ValueError:
        facts = {}
    kf = facts.get("key_figures", {})
    points, conf = kf.get("dti_points_from_limit"), out.get("confidence")
    if (points is not None and abs(points) <= BORDERLINE_POINTS) or (conf is not None and conf < MIN_APPROVE_CONFIDENCE):
        why = (f"DTI {kf.get('dti_pct')}% is {abs(points):.1f} points from the 50% limit" if points is not None
               and abs(points) <= BORDERLINE_POINTS else f"its confidence was only {conf}")
        return [_mistake(
            e, "MAJOR", "wrong_conclusion", "edge_case:borderline",
            f"Agent approved a borderline case ({why}) instead of sending it to a person.",
            "Refer borderline or low-confidence approvals to a credit officer.",
            "Unsure, not referred",
            {"label": "Distance from the 50% limit", "value": f"{abs(points):.1f} pts" if points is not None else "n/a",
             "sub": f"DTI {kf.get('dti_pct')}% · AI confidence {conf}"},
            {"label": "What the agent did", "value": "Approved", "sub": "should go to a person"},
            ["ai.decide"])]
    return []


WRONG_CONCLUSION = {"verify_income": _verify_income, "credit.pull_report": _credit,
                    "calc.serviceability": _serviceability, "rules.eligibility": _eligibility,
                    "rules.policy": _policy, "ai.decide": _decide}


# ---------------------------------------------------------------------------
# Rules 2-4 + DP3/DP4
# ---------------------------------------------------------------------------

def missing_reasoning(e: dict, prior: list[dict]) -> list[dict]:
    if e["step_name"] in DECISION_STEPS and not (e.get("notes") or "").strip():
        return [_mistake(e, "MINOR", "missing_reasoning", f"missing_reasoning:{e['step_name']}",
                         f"The {STEP_LABEL.get(e['step_name'], e['step_name'])} step reached a result with no reasoning.",
                         "Every decision step records a plain-English reason.", "No reasoning given",
                         {"label": "Result", "value": e["result"].upper(), "sub": e["step_name"]},
                         {"label": "Reasoning recorded", "value": "None", "sub": "notes were empty"})]
    return []


def skipped_step(e: dict, prior: list[dict]) -> list[dict]:
    needed = REQUIRED_BEFORE.get(e["step_name"], [])
    done = {p["step_name"] for p in prior}
    missing = [s for s in needed if s not in done]
    if not missing:
        return []
    return [_mistake(e, "MAJOR", "skipped_step", f"skips_step:{missing[0]}",
                     f"The agent ran {e['step_name']} without first running {', '.join(missing)}.",
                     f"Run {', '.join(missing)} before {e['step_name']}.", "Skipped a required step",
                     {"label": "Required first", "value": ", ".join(STEP_LABEL.get(s, s) for s in missing), "sub": ""},
                     {"label": "What the agent did", "value": STEP_LABEL.get(e["step_name"], e["step_name"]),
                      "sub": "went ahead anyway"})]


def ignored_failed_check(e: dict, prior: list[dict]) -> list[dict]:
    if e["step_name"] != "ai.decide" or e["output_data"].get("final_decision") != "approved":
        return []
    failed = [p for p in prior if p["result"] == "fail" and p["step_name"] != "access.violation"
              and p["agent_name"] in AUDITED_AGENTS]
    if not failed:
        return []
    f = failed[0]
    return [_mistake(e, "CRITICAL", "ignored_failed_check", "ignores_failed_check",
                     f"Agent approved the loan although {STEP_LABEL.get(f['step_name'], f['step_name'])} failed: {f['notes']}.",
                     "Decline (or refer) any case with a failed check; never approve it.",
                     "Approved despite a failed check",
                     {"label": "Failed check", "value": STEP_LABEL.get(f["step_name"], f["step_name"]), "sub": f["notes"]},
                     {"label": "What the agent did", "value": "Approved", "sub": "should be declined"},
                     [f["step_name"], "ai.decide"])]


def pii_to_ai(e: dict, prior: list[dict]) -> list[dict]:
    if not e["step_name"].startswith("ai."):
        return []
    hits = tools.find_pii(_sent_text(e))
    unredacted = [p["output_data"].get("document_type") for p in prior
                  if p["step_name"].startswith("docs.extract") and p["output_data"].get("redacted") is False]
    if not hits and not unredacted:
        return []
    kinds = sorted({h["type"].replace("_", " ") for h in hits} | ({"name"} if unredacted else set()))
    return [_mistake(e, "CRITICAL", "DP3", "privacy:pii_to_ai",
                     f"The AI step {e['step_name']} was sent personal details ({', '.join(kinds)})"
                     + (f"; the {', '.join(unredacted)} was not redacted." if unredacted else "."),
                     "Redact every document and message before an AI sees it.",
                     "Personal details sent to the AI",
                     {"label": "Redacted before the AI?", "value": "No" if unredacted else "Partly",
                      "sub": ", ".join(unredacted) or "message"},
                     {"label": "Personal details the AI saw", "value": str(len(kinds)), "sub": ", ".join(kinds)},
                     ["ai.read_documents"] if e["step_name"] == "ai.read_documents" else [e["step_name"]])]


def access_violation(e: dict, prior: list[dict]) -> list[dict]:
    if e["step_name"] != "access.violation":
        return []
    inp = e["input_data"]
    return [_mistake(e, "CRITICAL", "DP4", "access:out_of_scope",
                     e.get("notes") or "Agent accessed data outside its permissions.",
                     "Only use the ticket's own case, fields and stage.",
                     "Asked for data outside its ticket",
                     {"label": "Ticket was for", "value": str(inp.get("ticket_customer_token")), "sub": "one case only"},
                     {"label": "Agent asked for", "value": str(inp.get("requested_customer_token") or inp.get("scope")),
                      "sub": "refused and logged"},
                     ["credit.pull_report"])]


RULES = [missing_reasoning, skipped_step, ignored_failed_check, pii_to_ai, access_violation]


def check_event(event: dict, prior: list[dict]) -> list[dict]:
    """Every mistake in one event, given the same case's earlier events."""
    if event["agent_name"] not in AUDITED_AGENTS:
        return []
    found = []
    rule1 = WRONG_CONCLUSION.get(event["step_name"])
    if rule1:
        found += rule1(event, prior)
    for rule in RULES:
        found += rule(event, prior)
    return found


def repeated_mistake(new: dict, earlier_same_pattern_cases: set[str], threshold: int) -> dict | None:
    """Rule 5: the same wrong conclusion on `threshold`+ different cases within 24 h -> CRITICAL."""
    cases = earlier_same_pattern_cases | {new["case_id"]}
    if new["mistake_type"] == "CRITICAL" or len(cases) < threshold:
        return None
    return {**new, "mistake_type": "CRITICAL", "rule": "repeated_mistake",
            "pattern_key": "repeat:" + new["pattern_key"],
            "mistake_reason": f"Same mistake on {len(cases)} different cases within 24 hours "
                              f"({new['pattern_key']}): a pattern, not a one-off.",
            "what_should_have_happened": "Stop and fix the agent before it handles more cases.",
            "details": {**new["details"], "title": "Same mistake again", "severity": "Critical"}}


def explain_fallback(m: dict) -> dict:
    """Plain-English explanation + action label when the AI isn't available."""
    action = (m.get("details") or {}).get("action_hint") or {
        "misreads_field:pay_frequency": "Recalculate with the right pay frequency",
        "accepts_stale:payslip": "Ask for a recent payslip",
        "misreads_field:enquiries": "Send to a credit officer",
        "edge_case:borderline": "Send to a credit officer",
        "ignores_failed_check": "Decline and review the agent",
        "privacy:pii_to_ai": "Re-run with redaction",
        "access:out_of_scope": "Review the agent's access",
    }.get(m.get("pattern_key"), "Review the case")
    return {"explanation": re.sub(r"\s+", " ", m["mistake_reason"]), "action": action}
