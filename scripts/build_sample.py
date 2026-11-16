"""Build the hand-written sample data in data/sample/.

    python scripts/build_sample.py

This is a small, fixed dataset for building against before the real generator
(Agent 1) is ready. It contains both demo cases plus clean cases, legitimate
variations, and the history the L-Plates story needs:

  LOAN-0019, LOAN-0021  Payslip Reader misreads fortnightly as weekly; the
                        Verifier catches it and it is re-read (minor slips)
  LOAN-0023 (hero)      same misread, NOT caught; income doubled; approved.
                        Third time in 24h -> repeat-mistake demotion to Red P
  LOAN-0012             decision made without a policy check
  PAY-0047 (supporting) money released with no scam check -> suspension

Planted-failure rate here is higher than the generator's ~12% on purpose:
the sample exists to exercise every rule.

Outputs: data/sample/events.csv, agents.csv, customers.csv
"""

from __future__ import annotations

import csv
import hashlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared import constants as C  # noqa: E402

OUT = ROOT / "data" / "sample"
TZ = "+10:00"  # Brisbane, no daylight saving

MODEL_VERSION = {
    C.AGENT_ACCOUNT_SCRUTINY: "acct-scrutiny-1.4",
    C.AGENT_PAYSLIP_READER: "payslip-reader-3.2",
    C.AGENT_PAYSLIP_VERIFIER: "payslip-verifier-2.0",
    C.AGENT_POLICY: "policy-agent-2.1",
    C.AGENT_DECISION: "decision-agent-1.7",
    C.AGENT_PAYMENTS: "payments-agent-4.0",
}

# Synthetic customers. display_name is ONLY for the customer tracker; it never
# goes into the event log or to the LLM.
CUSTOMERS = {
    "LOAN-0001": "Mei Chen",
    "LOAN-0002": "Tom Walsh",
    "LOAN-0005": "Aroha Ngata",
    "LOAN-0008": "Sam Okafor",
    "LOAN-0012": "Lucy Park",
    "LOAN-0019": "Dev Raman",
    "LOAN-0021": "Hannah Ellis",
    "LOAN-0023": "Priya Sharma",
    "PAY-0001": "Ben Russo",
    "PAY-0002": "Grace Liu",
    "PAY-0003": "Oscar Nguyen",
    "PAY-0004": "Ella Brooks",
    "PAY-0047": "Jordan Taylor",
}

SALT = "agentlens-demo-salt"  # pseudonymisation salt (synthetic data only)


def customer_ref(name: str) -> str:
    return "CUST-" + hashlib.sha256((SALT + name).encode()).hexdigest()[:8]


def licence_for(agent: str, case: dict) -> str:
    if agent in (C.AGENT_PAYSLIP_READER, C.AGENT_PAYSLIP_VERIFIER):
        return case.get("licence_class", C.LICENCE_PAYSLIP_STANDARD)
    if agent == C.AGENT_PAYMENTS:
        return C.LICENCE_TRANSFER_STANDARD
    if agent in C.LENDING_AGENTS:
        return C.LICENCE_LENDING_GENERAL
    return ""


rows: list[dict] = []


def emit(case: dict, activity: str, minutes: float, outcome: str = "completed", **fields):
    """Append one event. `minutes` is the offset from the case start."""
    proc = case["process"]
    agent = fields.pop("agent", None) or C.ACTIVITY_OWNER[(proc, activity)]
    actor = C.NON_AGENT_ACTORS.get(agent, C.ACTOR_AGENT)
    ts = case["start"] + timedelta(minutes=minutes)
    row = {c: "" for c in C.EVENT_COLUMNS}
    row.update(
        case_id=case["case_id"],
        process=proc,
        activity=activity,
        timestamp=ts.strftime("%Y-%m-%dT%H:%M:%S") + TZ,
        actor_type=actor,
        agent_name=agent,
        licence_class=licence_for(agent, case) if actor == C.ACTOR_AGENT else "",
        model_version=MODEL_VERSION.get(agent, "") if actor == C.ACTOR_AGENT else "",
        customer_ref=customer_ref(CUSTOMERS[case["case_id"]]),
        outcome=outcome,
    )
    for k, v in fields.items():
        if k not in row:
            raise KeyError(f"unknown column {k}")
        row[k] = "" if v is None else str(v)
    rows.append(row)


def case(case_id: str, start: str, **extra) -> dict:
    proc = C.PROCESS_LOAN if case_id.startswith("LOAN") else C.PROCESS_TRANSFER
    return {"case_id": case_id, "process": proc,
            "start": datetime.fromisoformat(start), **extra}


def extract(c, minutes, doc_freq, read_freq, gross, fault=""):
    emit(c, "Extract payslip", minutes,
         doc_pay_frequency=doc_freq, read_pay_frequency=read_freq,
         payslip_gross=gross,
         extracted_income_annual=round(gross * C.PAY_FREQUENCY_PERIODS[read_freq]),
         gt_fault=fault)


def loan_happy(c, loan, declared, doc_freq, gross, read_freq=None, fault="", decision="approved"):
    read_freq = read_freq or doc_freq
    emit(c, "Receive", 0, loan_amount=loan, declared_income_annual=declared)
    emit(c, "Account scrutiny", 4)
    extract(c, 9, doc_freq, read_freq, gross, fault)
    emit(c, "Verify payslip", 13, note="Employer and pay dates checked")
    emit(c, "Policy check", 18)
    emit(c, "Decision", 22, outcome=decision, decision=decision)


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------

# Clean, fortnightly PAYG
loan_happy(case("LOAN-0001", "2026-11-12T07:05:00"), 480000, 85000, "fortnightly", 3270)

# Clean, monthly
loan_happy(case("LOAN-0002", "2026-11-12T07:40:00"), 700000, 120000, "monthly", 10000)

# Legit variation: verifier asks for a clearer payslip, then re-extracts
c = case("LOAN-0005", "2026-11-12T08:15:00")
emit(c, "Receive", 0, loan_amount=350000, declared_income_annual=62400)
emit(c, "Account scrutiny", 3)
extract(c, 8, "weekly", "weekly", 1200)
emit(c, "Verify payslip", 12, outcome="failed", note="Payslip image blurred; YTD unreadable")
emit(c, "Request documents", 13, note="Asked customer for a clearer payslip")
extract(c, 190, "weekly", "weekly", 1200)
emit(c, "Verify payslip", 194)
emit(c, "Policy check", 198)
emit(c, "Decision", 202, outcome="approved", decision="approved")

# Legit variation: loan above 6x income, correctly escalated to a human
c = case("LOAN-0008", "2026-11-12T08:50:00")
emit(c, "Receive", 0, loan_amount=900000, declared_income_annual=130000)
emit(c, "Account scrutiny", 4)
extract(c, 9, "fortnightly", "fortnightly", 5000)
emit(c, "Verify payslip", 13)
emit(c, "Policy check", 17, note="Loan-to-income 6.9x is above 6x")
emit(c, "Escalate to human", 18, outcome="escalated")
emit(c, "Human review", 75, note="Strong savings history; approve")
emit(c, "Decision", 80, outcome="approved", decision="approved")

# Planted: decision with no policy check (ORD-01)
c = case("LOAN-0012", "2026-11-12T09:30:00")
emit(c, "Receive", 0, loan_amount=410000, declared_income_annual=91000)
emit(c, "Account scrutiny", 4)
extract(c, 9, "fortnightly", "fortnightly", 3500)
emit(c, "Verify payslip", 13)
emit(c, "Decision", 16, outcome="approved", decision="approved", gt_fault=C.FAULT_SKIPPED_POLICY_CHECK)

# Planted: misread 1 of 3 - caught by the Verifier, re-read (DAT-04, minor)
for cid, start, loan, declared, gross in [
    ("LOAN-0019", "2026-11-12T10:10:00", 520000, 98000, 3770),
    ("LOAN-0021", "2026-11-12T14:40:00", 610000, 104000, 4000),
]:
    c = case(cid, start)
    emit(c, "Receive", 0, loan_amount=loan, declared_income_annual=declared)
    emit(c, "Account scrutiny", 4)
    extract(c, 9, "fortnightly", "weekly", gross, fault=C.FAULT_PAY_FREQUENCY_MISREAD)
    emit(c, "Verify payslip", 13, outcome="failed", note="Annual income is double the YTD figure")
    emit(c, "Request documents", 14, note="Asked Payslip Reader to re-read the payslip")
    extract(c, 20, "fortnightly", "fortnightly", gross)
    emit(c, "Verify payslip", 24)
    emit(c, "Policy check", 28)
    emit(c, "Decision", 32, outcome="approved", decision="approved")

# HERO: Priya. Fortnightly read as weekly, not caught, income doubled, approved.
# True income $109,200 -> 6.6x income (should have gone to a human).
# Doubled income $218,400 -> 3.3x, so it sailed through.
loan_happy(case("LOAN-0023", "2026-11-13T08:30:00"),
           loan=720000, declared=109000, doc_freq="fortnightly", gross=4200,
           read_freq="weekly", fault=C.FAULT_PAY_FREQUENCY_MISREAD)

# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------

def transfer_happy(c, amount, new_payee, scam, fault=""):
    emit(c, "Receive", 0, transfer_amount=amount, payee_is_new=new_payee)
    emit(c, "Verify ID", 1)
    emit(c, "Scam check", 2, scam_score=scam)
    emit(c, "Confirm", 3)
    emit(c, "Release", 4, decision="released", gt_fault=fault)


# Clean
transfer_happy(case("PAY-0001", "2026-11-12T09:00:00"), 450, False, 0.05)

# Legit variation: ID check fails once, then passes
c = case("PAY-0002", "2026-11-12T11:20:00")
emit(c, "Receive", 0, transfer_amount=2300, payee_is_new=False)
emit(c, "Verify ID", 1, outcome="failed", note="One-time code expired")
emit(c, "Verify ID", 3)
emit(c, "Scam check", 4, scam_score=0.12)
emit(c, "Confirm", 5)
emit(c, "Release", 6, decision="released")

# Legit variation: high scam score, escalated, human blocks it
c = case("PAY-0003", "2026-11-12T13:05:00")
emit(c, "Receive", 0, transfer_amount=6400, payee_is_new=True)
emit(c, "Verify ID", 1)
emit(c, "Scam check", 2, scam_score=0.82, note="Payee account opened 2 days ago")
emit(c, "Escalate to human", 3, outcome="escalated")
emit(c, "Human review", 25, outcome="declined", decision="blocked",
     note="Customer confirmed a 'tax office' caller asked for the payment")

# Legit variation: large transfer to a new payee, human reviews, released
c = case("PAY-0004", "2026-11-12T15:30:00")
emit(c, "Receive", 0, transfer_amount=15000, payee_is_new=True)
emit(c, "Verify ID", 1)
emit(c, "Scam check", 2, scam_score=0.31)
emit(c, "Human review", 20, note="Invoice for building work matches payee")
emit(c, "Confirm", 22)
emit(c, "Release", 23, decision="released")

# SUPPORTING: Jordan. Scam check skipped, money released (ORD-03, critical).
c = case("PAY-0047", "2026-11-13T10:12:00")
emit(c, "Receive", 0, transfer_amount=9800, payee_is_new=True)
emit(c, "Verify ID", 1)
emit(c, "Confirm", 2)
emit(c, "Release", 3, decision="released", gt_fault=C.FAULT_SKIPPED_SCAM_CHECK)

# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

rows.sort(key=lambda r: (r["case_id"], r["timestamp"]))
for i, r in enumerate(rows, 1):
    r["event_id"] = f"EVT-{i:06d}"

OUT.mkdir(parents=True, exist_ok=True)
with open(OUT / "events.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=C.EVENT_COLUMNS)
    w.writeheader()
    w.writerows(rows)

# Starting licence state, the morning before the demo story.
SNAPSHOT = "2026-11-12T07:00:00" + TZ
licences = [
    (C.AGENT_ACCOUNT_SCRUTINY, C.LICENCE_LENDING_GENERAL, C.LEVEL_FULL, 93),
    (C.AGENT_PAYSLIP_READER, C.LICENCE_PAYSLIP_STANDARD, C.LEVEL_FULL, 96),
    (C.AGENT_PAYSLIP_READER, C.LICENCE_PAYSLIP_SELF_EMPLOYED, C.LEVEL_L, 42),
    (C.AGENT_PAYSLIP_VERIFIER, C.LICENCE_PAYSLIP_STANDARD, C.LEVEL_GREEN_P, 81),
    (C.AGENT_POLICY, C.LICENCE_LENDING_GENERAL, C.LEVEL_GREEN_P, 78),
    (C.AGENT_DECISION, C.LICENCE_LENDING_GENERAL, C.LEVEL_RED_P, 66),
    (C.AGENT_PAYMENTS, C.LICENCE_TRANSFER_STANDARD, C.LEVEL_FULL, 92),
]
with open(OUT / "agents.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=C.LICENCE_STATE_COLUMNS)
    w.writeheader()
    for agent, lc, level, score in licences:
        w.writerow(dict(agent_name=agent, licence_class=lc, level=level, trust_score=score,
                        model_version=MODEL_VERSION[agent], last_change_reason="initial",
                        updated_at=SNAPSHOT))

with open(OUT / "customers.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["customer_ref", "display_name", "case_id"])
    for cid, name in sorted(CUSTOMERS.items()):
        w.writerow([customer_ref(name), name, cid])

print(f"wrote {len(rows)} events, {len(CUSTOMERS)} cases, {len(licences)} licences to {OUT.relative_to(ROOT)}")
