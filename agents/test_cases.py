"""Hardcoded test cases for the mock loan pipeline. Synthetic data only.

Each case has:
  customer_message   free text the intake agent parses (with Claude)
  profile            what the bank already knows about the customer
  payslip            the mock payslip document
  bank_transactions  one month of mock bank statement debits
  credit_report      the mock credit bureau response

LOAN-0023 and LOAN-0024 are the SAME customer with the SAME documents.
The only difference is that income_verification_agent has a planted bug that
fires for LOAN-0023 (see PLANTED_BUG_CASES in main.py): it reads the
fortnightly payslip as weekly, so it reports $144,000 instead of $72,000.

  LOAN-0024 (control)  true income $72,000  -> DTI 52%  -> declined  (correct)
  LOAN-0023 (demo)     reported $144,000    -> DTI 26%  -> approved  (WRONG)
"""

from copy import deepcopy

_CUSTOMER_MESSAGE = (
    "Hi, I'm John, I'm 27 and I've been working full-time for 2 years. "
    "I'd like to borrow $15,000 over 5 years to consolidate my credit card debt."
)

_PROFILE = {
    "name": "John",
    "age": 27,
    "employment": "full-time, 2 years",
    "gross_income": 72000,          # stated annual gross income
    "requested": 15000,
    "term_years": 5,
    "purpose": "debt consolidation",
    "monthly_expenses": 2400,
    "credit_card_limit": 8000,
    "has_hecs": True,
    "defaults": 0,
    "enquiries": 2,
    "existing_customer": True,
    "residency": "AU",
}

# A fortnightly payslip: $72,000 / 26 = $2,769.23 per fortnight.
_PAYSLIP = {
    "employer": "Harbourside Logistics Pty Ltd",
    "pay_frequency": "fortnightly",
    "gross_pay": 2769.23,
    "pay_period_start": "2026-10-24",
    "pay_period_end": "2026-11-06",
}

# One month of debits, totalling the stated $2,400 monthly expenses.
_BANK_TRANSACTIONS = [
    {"date": "2026-10-01", "description": "Rent - Kelvin Grove", "amount": 1600.00, "category": "housing"},
    {"date": "2026-10-04", "description": "Woolworths", "amount": 210.40, "category": "groceries"},
    {"date": "2026-10-11", "description": "Coles", "amount": 239.60, "category": "groceries"},
    {"date": "2026-10-15", "description": "Energy bill", "amount": 120.00, "category": "utilities"},
    {"date": "2026-10-18", "description": "Phone + internet", "amount": 60.00, "category": "utilities"},
    {"date": "2026-10-22", "description": "go card top-up", "amount": 100.00, "category": "transport"},
    {"date": "2026-10-28", "description": "Fuel", "amount": 70.00, "category": "transport"},
]

_CREDIT_REPORT = {
    "bureau": "MockBureau",
    "defaults": 0,
    "enquiries": 2,
    "credit_score": 712,
}


def _case(**overrides):
    case = {
        "customer_message": _CUSTOMER_MESSAGE,
        "profile": deepcopy(_PROFILE),
        "payslip": deepcopy(_PAYSLIP),
        "bank_transactions": deepcopy(_BANK_TRANSACTIONS),
        "credit_report": deepcopy(_CREDIT_REPORT),
    }
    case.update(overrides)
    return case


TEST_CASES = {
    # Demo failure case. Documents are correct; the agent misreads them.
    "LOAN-0023": _case(),
    # Clean control case. Same profile, same documents, no bug.
    "LOAN-0024": _case(),
}


def build_documents_from_profile(profile: dict) -> dict:
    """Make mock documents for a case submitted with only a profile.

    Used when /submit_case gets a case_id that isn't in TEST_CASES.
    """
    gross_income = float(profile.get("gross_income", 0))
    return {
        "payslip": {
            "employer": "Mock Employer Pty Ltd",
            "pay_frequency": "fortnightly",
            "gross_pay": round(gross_income / 26, 2),
            "pay_period_start": "2026-10-24",
            "pay_period_end": "2026-11-06",
        },
        "bank_transactions": [
            {"date": "2026-10-01", "description": "Monthly living expenses",
             "amount": float(profile.get("monthly_expenses", 0)), "category": "living"},
        ],
        "credit_report": {
            "bureau": "MockBureau",
            "defaults": int(profile.get("defaults", 0)),
            "enquiries": int(profile.get("enquiries", 0)),
            "credit_score": None,
        },
    }
