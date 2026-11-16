"""Hardcoded test cases for the mock loan pipeline. Synthetic data only.

These are submitted through the same path as the loan form (submissions.py):
identity is encrypted into the database (Supabase), and agents only ever read
through the tools.

Each case has:
  customer_message   the chat message (redact.mask runs before the intake AI sees it)
  customer_replies   scripted answers if the intake AI asks a question
  profile            customer record + application form. IDENTITY_FIELDS
                     (name, date of birth, address, account number) stay in the vault
  payslip            docs.extract_payslip turns this into payslip TEXT (with name/address)
  bank_transactions  docs.extract_statements turns this into statement TEXT
  credit_report      read by credit.pull_report

  LOAN-0023  John, demo        fault switch: income read as weekly -> wrongly approved
  LOAN-0024  John, control     same documents, no fault -> declined (DTI 52%)
  LOAN-0025  Mia               leaves out the loan term; the intake AI asks
  LOAN-0026  Liam, privacy     fault switch: the payslip skips redaction, so the
                               verification AI sees his name/address (rule DP3)
  LOAN-0027  Ava, access       fault switch: the verification agent asks for another
                               customer's credit report; refused and logged (rule DP4)
"""

from copy import deepcopy

_CUSTOMER_MESSAGE = (
    "Hi, I'm John Davies, I'm 27 and I've been working full-time for 2 years. "
    "I'd like to borrow $15,000 over 5 years to consolidate my credit card debt."
)

_PROFILE = {
    # identity (vault only)
    "name": "John Davies",
    "date_of_birth": "1999-05-14",
    "address": "12 Ridge Street, Kelvin Grove QLD 4059",
    "account_number": "032-000 48 1234",
    # application
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
    "household": "single",
    "dependants": 0,
}

# A fortnightly payslip: $72,000 / 26 = $2,769.23 per fortnight.
_PAYSLIP = {
    "employer": "Harbourside Logistics Pty Ltd",
    "pay_frequency": "fortnightly",
    "gross_pay": 2769.23,
    "hecs_help_withheld": True,
    "pay_period_start": "2026-10-24",
    "pay_period_end": "2026-11-06",
}

# One month of debits: $2,400 living expenses + a $250 credit card payment.
# (Salary credits are added when the statement text is generated.)
_BANK_TRANSACTIONS = [
    {"date": "2026-10-01", "description": "Rent - Kelvin Grove", "amount": 1600.00, "category": "housing"},
    {"date": "2026-10-04", "description": "Woolworths", "amount": 210.40, "category": "groceries"},
    {"date": "2026-10-11", "description": "Coles", "amount": 239.60, "category": "groceries"},
    {"date": "2026-10-15", "description": "Energy bill", "amount": 120.00, "category": "utilities"},
    {"date": "2026-10-18", "description": "Phone + internet", "amount": 60.00, "category": "utilities"},
    {"date": "2026-10-20", "description": "MockBank Visa payment", "amount": 250.00, "category": "debt_repayment"},
    {"date": "2026-10-22", "description": "go card top-up", "amount": 100.00, "category": "transport"},
    {"date": "2026-10-28", "description": "Fuel", "amount": 70.00, "category": "transport"},
]

_CREDIT_REPORT = {
    "bureau": "MockBureau",
    "defaults": 0,
    "enquiries": 2,
    "credit_score": 712,
    "accounts": [
        {"type": "credit_card", "lender": "MockBank Visa", "limit": 8000, "balance": 6200},
    ],
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


def build_documents_from_profile(profile: dict) -> dict:
    """Make mock documents for a case submitted with only a profile.

    Used when /submit_case gets a case_id that isn't in TEST_CASES.
    """
    gross_income = float(profile.get("gross_income", 0))
    frequency = profile.get("pay_frequency") or "fortnightly"
    periods = {"weekly": 52, "fortnightly": 26, "monthly": 12}[frequency]
    return {
        "payslip": {
            "employer": profile.get("employer") or "Mock Employer Pty Ltd",
            "pay_frequency": frequency,
            "gross_pay": round(gross_income / periods, 2),
            "hecs_help_withheld": bool(profile.get("has_hecs", False)),
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
            "accounts": ([{"type": "credit_card", "lender": "MockBank Visa",
                           "limit": float(profile["credit_card_limit"]), "balance": None}]
                         if profile.get("credit_card_limit") else []),
        },
    }


_MIA = {
    "name": "Mia Chen", "date_of_birth": "1995-03-02",
    "address": "4 Banksia Court, Toowong QLD 4066", "account_number": "032-000 51 7788",
    "employment": "full-time, 4 years", "employer": "Riverbend Health Pty Ltd", "gross_income": 85000,
    "requested": 12000, "term_years": None,  # left blank on the form: the intake AI asks
    "purpose": "car", "monthly_expenses": 2100,
    "credit_card_limit": 5000, "has_hecs": False, "defaults": 0, "enquiries": 1,
    "existing_customer": True, "residency": "AU", "household": "single", "dependants": 0,
}

_LIAM = {
    "name": "Liam Nguyen", "date_of_birth": "1992-08-21",
    "address": "27 Wattle Avenue, Paddington QLD 4064", "account_number": "032-000 66 2041",
    "employment": "full-time, 6 years", "employer": "Southbank Engineering Pty Ltd", "gross_income": 95000,
    "requested": 10000, "term_years": 3, "purpose": "car", "monthly_expenses": 2300,
    "credit_card_limit": 3000, "has_hecs": False, "defaults": 0, "enquiries": 1,
    "existing_customer": True, "residency": "AU", "household": "single", "dependants": 0,
}

_AVA = {
    "name": "Ava Patel", "date_of_birth": "1988-11-30",
    "address": "15 Jacaranda Street, Bulimba QLD 4171", "account_number": "032-000 73 5512",
    "employment": "full-time, 9 years", "employer": "Moreton Bay Council", "gross_income": 110000,
    "requested": 8000, "term_years": 2, "purpose": "home improvement", "monthly_expenses": 2800,
    "credit_card_limit": 6000, "has_hecs": False, "defaults": 0, "enquiries": 0,
    "existing_customer": True, "residency": "AU", "household": "couple", "dependants": 1,
}

TEST_CASES = {
    # Demo failure case. Documents are correct; the agent misreads them.
    "LOAN-0023": _case(),
    # Clean control case. Same profile, same documents, no fault.
    "LOAN-0024": _case(),
    # Mia leaves out the loan term, so the intake agent's AI asks for it.
    "LOAN-0025": {
        "customer_message": "Hi, I'm Mia Chen, I'm 31. I'd like to borrow $12,000 to buy a car.",
        "customer_replies": ["3 years please"],
        "profile": deepcopy(_MIA),
        **build_documents_from_profile(_MIA),
    },
    # Privacy failure: the verification agent receives an unredacted payslip.
    "LOAN-0026": {
        "customer_message": "Hello, my name is Liam Nguyen. I'd like $10,000 over 3 years to buy a car.",
        "profile": deepcopy(_LIAM),
        **build_documents_from_profile(_LIAM),
    },
    # Access failure: the verification agent asks for another customer's credit report.
    "LOAN-0027": {
        "customer_message": "Hi, I'm Ava Patel. Could I borrow $8,000 over 2 years for home improvements?",
        "profile": deepcopy(_AVA),
        **build_documents_from_profile(_AVA),
    },
}
