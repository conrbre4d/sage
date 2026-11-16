"""Every tool the agents can call, in one file. Synthetic data only.

LEAST PRIVILEGE: agents never touch the database. Every tool takes the agent's
Ticket (one case, one agent, issued by the orchestrator) and calls access.check()
before it reads or writes anything. The ticket decides:
  - which case   (a different customer_token is refused: rule DP4)
  - which fields (each agent has an allowlist in access.PERMISSIONS)
  - which stage  (an agent can only act while the case is at its status)
Refusals are logged to the event log as 'access.violation'.

PRIVACY: identity (name, DOB, address, account, employer) is encrypted in the
database. Tools that need it (identity.verify, docs.match_identity, the notification
service) decrypt it in plain code and return yes/no facts. Text that reaches an agent
(the message, customer replies, documents) is redacted INSIDE the tool first.

  Tool                     Hackathon version                        Production version
  crm.get_application      applications table (Supabase)            loan origination system
  chat.get_message         the form's free text, redacted           Westpac App / online application
  chat.ask_customer        scripted replies, redacted               Westpac App messaging
  case.read_handoff /      handoffs table                           case management / workflow
    case.write_handoff
  case.set_status          applications.status (stage-gated)        case management / workflow
  docs.extract_payslip     payslip text, encrypted -> redacted      Equifax Verification Exchange / upload portal
  docs.extract_statements  statement text, encrypted -> redacted    Open Banking (CDR) / uploaded statements
  docs.match_identity      compares names in plain code             Westpac verification portal
  identity.verify          yes/no from the encrypted identity       FrankieOne + gov checks, OCR Labs, ConnectID
  credit.pull_report       mock_external_data table                 Equifax / Experian credit report
  benchmarks.hem           small lookup table                       licensed HEM data
  rules.eligibility, calc.serviceability, rules.policy   Python     Westpac internal credit policy / decisioning
  notify.send_template     prints the message on screen             email / SMS / app notifications
  (internal) redact.mask   regex + known identity values            Westpac PII redaction service
"""

from __future__ import annotations

import os
import re
from copy import deepcopy
from datetime import date

import access
import db
import faults

TODAY = date(2026, 11, 16)  # fixed so the demo is reproducible

# Mock lending assumptions
PAY_PERIODS_PER_YEAR = {"weekly": 52, "fortnightly": 26, "monthly": 12}
CREDIT_CARD_COMMITMENT_RATE = 0.038  # monthly commitment = 3.8% of card limit
HECS_MONTHLY_ALLOWANCE = 150.0       # flat mock HECS-HELP repayment
INTEREST_LOADING = 1.08              # monthly repayment = loan / months x 1.08
MAX_DTI = 0.50
PERSONAL_LOAN_CAP = 30000
MAX_TERM_YEARS = 7

# Mock Household Expenditure Measure, $/month, by (household, dependants)
HEM_MONTHLY = {
    ("single", 0): 1950, ("single", 1): 2650, ("single", 2): 3200,
    ("couple", 0): 2900, ("couple", 1): 3500, ("couple", 2): 4000,
}

APPLICATION_FIELDS = [  # what crm.get_application returns: never identity
    "stated_annual_income", "requested_amount", "term_years", "purpose", "existing_customer",
    "residency", "household", "dependants",
]
HANDOFF_STAGE = {"intake_agent": "intake", "verification_agent": "verification", "decision_agent": "decision"}


# ---------------------------------------------------------------------------
# redact (internal: tools run it before text reaches an agent)
# ---------------------------------------------------------------------------

PII_PATTERNS = {  # also usable by AgentLens rule DP3 (scan any agent's input)
    "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "card_number": r"\b(?:\d{4}[ -]?){3}\d{4}\b",
    "phone": r"\b(?:\+?61[ -]?|0)4\d{2}[ -]?\d{3}[ -]?\d{3}\b",
    "account_number": r"\b\d{3}-\d{3}(?:\s+\d{2}\s?\d{4}|\s*\d{6,9})?\b",
    "tfn": r"\b\d{3}[ -]?\d{3}[ -]?\d{3}\b",
    "date_of_birth": r"(?i)\b(?:dob|date of birth|born)\b[:\s]*[\w /.-]{6,20}",
    "address": (r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Ct|Court|"
                r"Pl|Place|Tce|Terrace|Cres|Crescent|Ln|Lane|Way)\b"
                r"(?:,?\s+[A-Z][a-z]+)*(?:,?\s+(?:QLD|NSW|VIC|SA|WA|TAS|NT|ACT))?(?:\s+\d{4})?"),
    "name_intro": r"(?:(?<=my name is )|(?<=I'm )|(?<=I am )|(?<=this is ))[A-Z][a-z]+(?: [A-Z][a-z]+)?",
}
MASK_LABEL = {"email": "[EMAIL]", "card_number": "[CARD]", "phone": "[PHONE]", "account_number": "[ACCOUNT]",
              "tfn": "[TFN]", "date_of_birth": "[DOB]", "address": "[ADDRESS]", "name_intro": "[NAME]",
              "name": "[NAME]", "employer": "[EMPLOYER]"}


def _known_strings(ident: dict) -> list[tuple[str, str]]:
    """Identity values from the vault, longest first, so 'John Davies' masks before 'John'."""
    out = []
    if ident.get("name"):
        out += [("name", ident["name"])] + [("name", part) for part in ident["name"].split() if len(part) > 1]
    for k in ("address", "account_number", "date_of_birth", "employer"):
        if ident.get(k):
            out.append((k, ident[k]))
    return sorted(out, key=lambda kv: -len(kv[1]))


def find_pii(text: str, identity: dict | None = None) -> list[dict]:
    """Every PII hit in `text` (pattern matches + known identity values). For tests / rule DP3."""
    hits = [{"type": t, "match": m.group(0)} for t, pat in PII_PATTERNS.items() for m in re.finditer(pat, text)]
    for t, value in _known_strings(identity or {}):
        if t != "employer" and re.search(rf"\b{re.escape(value)}\b", text):
            hits.append({"type": t, "match": value})
    return hits


def redact_mask(text: str, customer_token: str | None = None) -> dict:
    """Mask identity in `text` using the vault's known values plus regex patterns."""
    counts: dict[str, int] = {}
    ident = db.get_identity(customer_token) if customer_token else {}
    for t, value in _known_strings(ident):
        text, n = re.subn(rf"\b{re.escape(value)}\b", MASK_LABEL[t], text)
        if n:
            counts[t] = counts.get(t, 0) + n
    for t, pat in PII_PATTERNS.items():
        text, n = re.subn(pat, MASK_LABEL[t], text)
        if n:
            key = "name" if t == "name_intro" else t
            counts[key] = counts.get(key, 0) + n
    return {"text": text, "masked": counts}


# ---------------------------------------------------------------------------
# crm + chat (intake agent)
# ---------------------------------------------------------------------------

def crm_get_application(ticket) -> dict:
    """The application form's facts for the ticket's case. Never name, DOB, address or account."""
    access.check(ticket, "application")
    row = db.get_application(ticket.case_id)
    return {k: row.get(k) for k in APPLICATION_FIELDS}


def chat_get_message(ticket) -> dict:
    """The customer's free-text message from the form, already redacted."""
    access.check(ticket, "customer_message")
    m = redact_mask(db.get_customer_message(ticket.case_id), ticket.customer_token)
    return {"text": m["text"], "masked": m["masked"], "redacted": True}


_CUSTOMER_REPLIES: dict[str, list[str]] = {}


def register_customer_replies(case_id: str, replies: list[str] | None) -> None:
    """Hackathon: scripted customer answers, used in order by chat.ask_customer."""
    _CUSTOMER_REPLIES[case_id] = list(replies or [])


def chat_ask_customer(ticket, question: str) -> dict:
    """Ask the customer a question; their reply comes back redacted (None = no reply yet)."""
    access.check(ticket, "customer_chat")
    pending = _CUSTOMER_REPLIES.get(ticket.case_id, [])
    raw = pending.pop(0) if pending else None
    if os.environ.get("AGENTLENS_QUIET") != "1":
        print(f"[chat -> {ticket.case_id}] {question}\n[chat <- {ticket.case_id}] {raw or '(no reply yet)'}")
    m = redact_mask(raw, ticket.customer_token) if raw else {"text": None, "masked": {}}
    return {"delivered": True, "reply": m["text"], "masked": m["masked"]}


# ---------------------------------------------------------------------------
# case: handoffs and status (every agent)
# ---------------------------------------------------------------------------

def case_write_handoff(ticket, data: dict) -> dict:
    """Write this agent's handoff for the next stage (token + facts only)."""
    stage = HANDOFF_STAGE[ticket.agent] if ticket.agent in HANDOFF_STAGE else "none"
    access.check(ticket, f"handoff_{stage}", "write")
    db.write_handoff(ticket.case_id, stage, data)
    return {"stage": stage, "fields": sorted(data)}


def case_read_handoff(ticket, stage: str) -> dict:
    """Read an earlier agent's handoff, if this agent is allowed to."""
    access.check(ticket, f"handoff_{stage}")
    data = db.read_handoff(ticket.case_id, stage)
    if data is None:
        raise KeyError(f"no {stage} handoff for {ticket.case_id}")
    return data


def case_set_status(ticket, status: str) -> dict:
    """Move the case to the next stage (only the next stage, only from this agent's stage)."""
    return access.set_status(ticket, status)


# ---------------------------------------------------------------------------
# docs (verification agent). Documents are stored encrypted; the agent gets
# redacted text. The raw text never leaves this file.
# ---------------------------------------------------------------------------

MOCK_TAX_RATE = 0.18
MOCK_HECS_RATE = 0.035


def _d(iso: str) -> str:
    """2026-10-24 -> 24 Oct 2026"""
    return date.fromisoformat(iso).strftime("%d %b %Y")


def _net_pay(p: dict) -> tuple[float, float, float]:
    tax = round(p["gross_pay"] * MOCK_TAX_RATE, 2)
    hecs = round(p["gross_pay"] * MOCK_HECS_RATE, 2) if p.get("hecs_help_withheld") else 0.0
    return tax, hecs, round(p["gross_pay"] - tax - hecs, 2)


def payslip_text(p: dict, profile: dict) -> str:
    """Synthetic payslip text (made at submission time, then stored encrypted)."""
    tax, hecs, net = _net_pay(p)
    title = "SELF-EMPLOYED INCOME STATEMENT" if p.get("self_employed") else "PAYSLIP"
    lines = [
        f"{title} - {p['employer']}",
        f"Employee: {profile.get('name', '')}   Employee ID: E-20417",
        f"Address: {profile.get('address', '')}",
        f"Pay period: {_d(p['pay_period_start'])} to {_d(p['pay_period_end'])}",
        f"Pay frequency: {p['pay_frequency'].title()}",
        f"Gross pay: ${p['gross_pay']:,.2f}",
        f"Tax withheld: ${tax:,.2f}",
    ]
    if hecs:
        lines.append(f"HECS-HELP withheld: ${hecs:,.2f}")
    lines.append(f"Net pay: ${net:,.2f}")
    return "\n".join(lines)


def statement_text(case: dict) -> str:
    """Synthetic bank statement text (made at submission time, then stored encrypted)."""
    p, profile = case["payslip"], case["profile"]
    _, _, net = _net_pay(p)
    rows = [(t["date"], t["description"], -t["amount"]) for t in case["bank_transactions"]]
    rows += [("2026-10-09", f"Salary {p['employer']}", net), ("2026-10-23", f"Salary {p['employer']}", net)]
    lines = ["EVERYDAY ACCOUNT STATEMENT  01 Oct 2026 - 31 Oct 2026",
             f"Account holder: {profile.get('name', '')}   BSB/Account: {profile.get('account_number', '')}",
             "Date    Description                          Amount"]
    for d, desc, amt in sorted(rows):
        lines.append(f"{_d(d)[:6]}  {desc:<36} {amt:+,.2f}")
    return "\n".join(lines)


def _redacted_document(ticket, doc_type: str) -> dict:
    access.check(ticket, "documents")
    raw = db.get_document_text(ticket.case_id, doc_type)
    if doc_type == "payslip" and faults.fault_for(ticket.case_id) == "unredacted_payslip":
        # FAULT SWITCH (demo): redaction is skipped, so the agent and its AI get the
        # payslip with name, address and employer. AgentLens rule DP3 should catch it.
        return {"document_type": doc_type, "text": raw, "redacted": False, "masked": {}}
    m = redact_mask(raw, ticket.customer_token)
    return {"document_type": doc_type, "text": m["text"], "redacted": True, "masked": m["masked"]}


def docs_extract_payslip(ticket) -> dict:
    """The customer's latest payslip, redacted."""
    return _redacted_document(ticket, "payslip")


def docs_extract_statements(ticket) -> dict:
    """One month of the customer's bank statement, redacted."""
    return _redacted_document(ticket, "bank_statement")


def docs_match_identity(ticket, document: str = "payslip") -> dict:
    """Does the document belong to the applicant? Compared in plain code; returns yes/no only."""
    access.check(ticket, "identity_check")
    raw = db.get_document_text(ticket.case_id, document)
    ident = db.get_identity(ticket.customer_token)
    m = re.search(r"^(?:Employee|Account holder):\s*(.+?)(?:\s{2,}|$)", raw, re.M)
    on_doc = (m.group(1).strip().lower() if m else "")
    return {"document": document, "name_match": bool(on_doc) and on_doc == (ident.get("name") or "").lower(),
            "name_found_on_document": bool(on_doc)}


# ---------------------------------------------------------------------------
# identity + credit + benchmarks (verification agent)
# ---------------------------------------------------------------------------

def identity_verify(ticket) -> dict:
    """Check the applicant's identity (mock FrankieOne + government sources).

    Plain code: reads name, DOB and address from the encrypted vault itself. The
    agent only gets yes/no facts back, never the values.
    """
    access.check(ticket, "identity_check")
    ident = db.get_identity(ticket.customer_token)
    if ident.get("date_of_birth"):
        dob = date.fromisoformat(ident["date_of_birth"])
        age = TODAY.year - dob.year - ((TODAY.month, TODAY.day) < (dob.month, dob.day))
    else:
        age = ident.get("age")
    have = {k: bool(ident.get(k)) for k in ("name", "address")} | {"date_of_birth": age is not None}
    return {
        "identity_verified": all(have.values()),
        "age_over_18": age is not None and age >= 18,
        "details_checked": sorted(k for k, ok in have.items() if ok),
        "sources_checked": ["Medicare", "Australian Passport Office", "Electoral Roll"] if all(have.values()) else [],
    }


def credit_pull_report(ticket, customer_token: str | None = None) -> dict:
    """Mock credit bureau response for the ticket's customer (any other customer is refused)."""
    access.check(ticket, "credit_report", customer_token=customer_token)
    return deepcopy(db.get_mock_external(ticket.case_id)["credit_report"])


def benchmarks_hem(ticket, household: str = "single", dependants: int = 0) -> dict:
    """Minimum monthly living expenses the bank assumes for this household."""
    access.check(ticket, "benchmarks")
    key = (household, min(int(dependants), 2))
    return {"household": household, "dependants": int(dependants),
            "hem_monthly": HEM_MONTHLY.get(key, HEM_MONTHLY[("single", 0)])}


# ---------------------------------------------------------------------------
# rules + calc (verification agent)
# ---------------------------------------------------------------------------

def rules_eligibility(ticket, identity_verified: bool, age_over_18: bool, existing_customer: bool,
                      residency: str) -> dict:
    """Hard eligibility: identity verified, 18+, existing customer, Australian resident."""
    access.check(ticket, "rules")
    checks = {
        "identity_verified": bool(identity_verified),
        "age_18_plus": bool(age_over_18),
        "existing_customer": bool(existing_customer),
        "residency_au": residency == "AU",
    }
    return {"eligible": all(checks.values()), "checks": checks}


def calc_serviceability(ticket, monthly_income: float, living_expenses: float, hem_monthly: float,
                        credit_card_limit: float, has_hecs: bool, loan_amount: float, term_years: int) -> dict:
    """Can the customer afford the repayments? DTI must be 50% or less."""
    access.check(ticket, "rules")
    expenses_used = max(living_expenses, hem_monthly)  # never assume less than HEM
    card = round(credit_card_limit * CREDIT_CARD_COMMITMENT_RATE, 2)
    hecs = HECS_MONTHLY_ALLOWANCE if has_hecs else 0.0
    repayment = round(loan_amount / (term_years * 12) * INTEREST_LOADING, 2)
    commitments = round(expenses_used + card + hecs, 2)
    dti = (commitments + repayment) / monthly_income if monthly_income else float("inf")
    return {
        "monthly_income_used": monthly_income,
        "expenses_used": expenses_used,
        "expenses_basis": "HEM" if hem_monthly > living_expenses else "declared",
        "credit_card_commitment": card,
        "hecs_commitment": hecs,
        "monthly_commitments": commitments,
        "new_monthly_repayment": repayment,
        "monthly_surplus": round(monthly_income - commitments - repayment, 2),
        "dti_ratio": round(dti, 4),
        "max_dti_ratio": MAX_DTI,
        "serviceable": dti <= MAX_DTI,
    }


def rules_policy(ticket, loan_amount: float, term_years: int, purpose: str | None,
                 has_existing_debts: bool) -> dict:
    """Lending policy for unsecured personal loans."""
    access.check(ticket, "rules")
    violations = []
    if loan_amount > PERSONAL_LOAN_CAP:
        violations.append(f"personal loans are capped at ${PERSONAL_LOAN_CAP:,} (requested ${loan_amount:,.0f})")
    if term_years > MAX_TERM_YEARS:
        violations.append(f"maximum term is {MAX_TERM_YEARS} years (requested {term_years})")
    if purpose == "debt consolidation" and not has_existing_debts:
        violations.append("debt consolidation requires existing debts")
    return {"rules_checked": ["personal_loan_cap", "max_term", "debt_consolidation_requires_debt"],
            "violations": violations}


# ---------------------------------------------------------------------------
# notify (the notification service: plain code, the only place the name is put back)
# ---------------------------------------------------------------------------

TEMPLATES = {
    "approved": "Good news, {first_name}! Your {amount} personal loan has been approved. {reason}",
    "declined": "Hi {first_name}, thanks for applying. We can't approve your {amount} loan right now. {reason}",
    "refer_to_human": ("Hi {first_name}, thanks for applying. A member of our lending team is reviewing "
                       "your {amount} application and will be in touch within 1 business day. {reason}"),
}
TRACKER_STATUS = {"approved": "approved", "declined": "declined", "refer_to_human": "with_a_person"}


def notify_send_template(ticket, channel: str = "screen") -> dict:
    """Send the decision: reads the decision handoff + contact details, inserts the first name.

    Returns only a preview with [NAME].
    """
    access.check(ticket, "handoff_decision")
    access.check(ticket, "contact_details")
    d = db.read_handoff(ticket.case_id, "decision")
    filled = TEMPLATES[d["decision"]].format(first_name="{first_name}", amount=f"${d['loan_amount']:,.0f}",
                                             reason=d.get("customer_reason") or "").strip()
    name = db.get_identity(ticket.customer_token).get("name")
    message = filled.format(first_name=name.split()[0] if name else "there")
    if os.environ.get("AGENTLENS_QUIET") != "1":
        print(f"[notify -> {ticket.case_id} via {channel}] {message}")
    db.upsert_tracker(ticket.case_id, "Decision made", TRACKER_STATUS[d["decision"]], access.now_iso(), message)
    return {"template_id": d["decision"], "channel": channel, "delivered": True,
            "message_preview": filled.format(first_name="[NAME]")}


def datetime_now_iso() -> str:
    return access.now_iso()


# Tool names as the agents (and the event log) refer to them
TOOLS = {
    "crm.get_application": crm_get_application,
    "chat.get_message": chat_get_message,
    "chat.ask_customer": chat_ask_customer,
    "case.read_handoff": case_read_handoff,
    "case.write_handoff": case_write_handoff,
    "case.set_status": case_set_status,
    "docs.extract_payslip": docs_extract_payslip,
    "docs.extract_statements": docs_extract_statements,
    "docs.match_identity": docs_match_identity,
    "identity.verify": identity_verify,
    "credit.pull_report": credit_pull_report,
    "benchmarks.hem": benchmarks_hem,
    "rules.eligibility": rules_eligibility,
    "calc.serviceability": calc_serviceability,
    "rules.policy": rules_policy,
    "notify.send_template": notify_send_template,
}


# What each tool is called on screen, and the real service in Westpac's lending
# process it stands in for. (These are services Westpac uses in its lending
# process; which ones its AI agents call is not public.)
TOOL_INFO = {
    "crm.get_application": ("Loan application (Supabase)", "Westpac loan origination system"),
    "chat.get_message": ("Customer message (from the loan form)", "Westpac App / online application"),
    "chat.ask_customer": ("Customer chat (mock)", "Westpac App messaging"),
    "case.read_handoff": ("Case handoff (Supabase)", "Westpac case management / workflow"),
    "case.write_handoff": ("Case handoff (Supabase)", "Westpac case management / workflow"),
    "case.set_status": ("Case status (Supabase)", "Westpac case management / workflow"),
    "docs.extract_payslip": ("Income verification (Equifax mock)",
                             "Equifax Verification Exchange, or payslip upload via the verification portal"),
    "docs.extract_statements": ("Open Banking statements (CDR mock)",
                                "Consumer Data Right transaction data, or uploaded statements"),
    "docs.match_identity": ("Document ownership check (mock)", "Westpac verification portal"),
    "identity.verify": ("Identity check (FrankieOne mock)",
                        "FrankieOne + government sources (Medicare, Passport Office, Electoral Roll), OCR Labs, ConnectID"),
    "credit.pull_report": ("Credit bureau (Equifax mock)", "Equifax / Experian credit report"),
    "benchmarks.hem": ("HEM benchmark (mock)", "Licensed HEM benchmark data"),
    "rules.eligibility": ("Eligibility rules", "Westpac internal credit policy"),
    "calc.serviceability": ("Serviceability calculator", "Westpac internal decisioning"),
    "rules.policy": ("Lending policy rules", "Westpac internal credit policy"),
    "notify.send_template": ("Customer notification (mock)", "Westpac email / SMS / app notifications"),
    "redact.mask": ("PII redaction (internal)", "Westpac PII redaction service"),
}
AI_PLATFORM = "Amazon Bedrock AgentCore (hackathon: QUT Azure OpenAI)"
