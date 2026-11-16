"""What happens when a customer submits the loan form.

The form is split before anything is stored:
  identity  (name, DOB, address, account number, employer) -> encrypted, customer_identity
  message   (their free text)                               -> encrypted, customer_identity
  application (amounts, term, purpose, household...)       -> applications (plain)
  documents (payslip + statement text)                      -> encrypted, documents
  mock bureau data + scripted chat replies                  -> mock_external_data

The application is then 'submitted', and the agents pick it up (main.process_case).
Hackathon: the "uploaded" payslip and statement are synthetic text generated from
the form, and the credit report is a mock bureau response.
"""

from __future__ import annotations

import re
import secrets

import db
import tools
from test_cases import TEST_CASES, build_documents_from_profile

IDENTITY_FIELDS = ["name", "date_of_birth", "address", "account_number"]
APPLICATION_COLUMNS = {  # profile key -> applications column
    "gross_income": "stated_annual_income", "requested": "requested_amount", "term_years": "term_years",
    "purpose": "purpose", "employment": "employment", "monthly_expenses": "monthly_expenses",
    "credit_card_limit": "credit_card_limit", "has_hecs": "has_hecs",
    "existing_customer": "existing_customer", "residency": "residency", "household": "household",
    "dependants": "dependants",
}
DEFAULTS = {"existing_customer": True, "residency": "AU", "household": "single", "dependants": 0,
            "has_hecs": False}


def new_token() -> str:
    return "CUST-" + secrets.token_hex(4).upper()


def next_case_id() -> str:
    nums = [int(m.group(1)) for a in db.list_applications()
            if (m := re.fullmatch(r"LOAN-(\d{4})", a["case_id"]))]
    return f"LOAN-{max([1000, *nums]) + 1:04d}"


def build_submission(case_id: str, profile: dict, customer_message: str, documents: dict | None = None,
                     customer_replies: list[str] | None = None) -> dict:
    """Split a loan form into what gets stored (identity separate, to be encrypted)."""
    profile = {**DEFAULTS, **profile}
    docs = documents or build_documents_from_profile(profile)
    identity = {k: profile.get(k) for k in IDENTITY_FIELDS if profile.get(k) not in (None, "")}
    if "date_of_birth" not in identity and profile.get("age") is not None:
        identity["age"] = profile["age"]
    identity["employer"] = docs["payslip"].get("employer")
    application = {col: profile.get(key) for key, col in APPLICATION_COLUMNS.items()}
    case = {**docs, "profile": profile}
    return {"case_id": case_id, "application": application, "customer_token": new_token(), "identity": identity,
            "customer_message": customer_message,
            "documents": {"payslip": tools.payslip_text(docs["payslip"], profile),
                          "bank_statement": tools.statement_text(case)},
            "credit_report": docs["credit_report"], "customer_replies": list(customer_replies or [])}


def submit_application(profile: dict, customer_message: str, case_id: str | None = None,
                       documents: dict | None = None, customer_replies: list[str] | None = None) -> str:
    """Store a submitted loan form (identity encrypted). Returns the case_id."""
    case_id = case_id or next_case_id()
    db.save_applications([build_submission(case_id, profile, customer_message, documents, customer_replies)])
    db.upsert_tracker(case_id, "Application received", "submitted", tools.datetime_now_iso())
    return case_id


def submit_many(submissions: list[dict]) -> list[str]:
    """Store many built submissions at once (a few requests in total on Supabase)."""
    db.save_applications(submissions)
    return [s["case_id"] for s in submissions]


def submit_test_case(case_id: str, customer_message: str | None = None, profile: dict | None = None,
                     customer_replies: list[str] | None = None) -> str:
    """Submit a hardcoded test case (optionally with overrides), the same way the form does."""
    base = TEST_CASES.get(case_id)
    if base is None and profile is None:
        raise ValueError(f"{case_id} is not a test case, so a profile is required")
    profile = profile or base["profile"]
    message = customer_message or (base["customer_message"] if base else None)
    if not message:
        raise ValueError("customer_message is required")
    replies = customer_replies if customer_replies is not None else (base or {}).get("customer_replies", [])
    docs = ({k: base[k] for k in ("payslip", "bank_transactions", "credit_report")} if base else None)
    return submit_application(profile, message, case_id=case_id, documents=docs, customer_replies=replies)


def seed_demo_cases() -> list[str]:
    """Submit all the test cases (LOAN-0023..0026). They wait as 'submitted'."""
    return [submit_test_case(cid) for cid in TEST_CASES]
