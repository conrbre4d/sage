"""Seeds the AgentLens demo by running a batch of synthetic loan applications through the
REAL pipeline (agents/main.py), one by one, with AgentLens watching. Synthetic data only.

    python demo_seed.py               # the batch (30 cases): stops with things for a person to do
    python demo_seed.py --count 60    # a bigger batch (more clean cases at the end)
    python demo_seed.py --all         # the batch, then the whole improvement loop automatically

The demo script, mapped onto our agents:
  1. verification_agent: 7 correct cases, then 3 identical "income doubled" mistakes
     (LOAN-0023 John, LOAN-0029, and LOAN-0035, who is self-employed)
     -> demoted FULL -> P1 (Red P), suggestion #1 "require pay_frequency (pay period)".
  2. Apply #1, verify -> the 2 employee cases pass, the self-employed one still fails
     -> iteration 2, suggestion #2 (self-employed income statements).
  3. Apply #2, verify -> all 3 pass -> resolved, verification agent fast-tracked
     (10 cases instead of 30); after 10 more cases a person promotes it.
  4. (on demand: --incident, or the dashboard's "Simulate a critical incident") decision_agent
     approves a loan despite a failed credit check (LOAN-0047) -> CRITICAL -> instantly back to L-plates.
After the batch alone: intake Full licence, verification Red P (P1), decision Green P (P2).
Also in the batch, for the review queue: debts left out (LOAN-0015), a borderline approval
(LOAN-0031), an old payslip (LOAN-0042), ignored credit checks (LOAN-0039), plus normal
declines, a referral for hardship and a customer the intake AI has to ask for the term.
"""

from __future__ import annotations

import argparse
import random
import sys
import time

import _paths  # noqa: F401
import db
import faults
import monitor
import store
import submissions
import tools
from test_cases import TEST_CASES

FIRST_NAMES = ["Olivia", "Noah", "Charlotte", "Jack", "Amelia", "William", "Isla", "Leo", "Mia", "Henry",
               "Ava", "Thomas", "Grace", "Lucas", "Zara", "Ethan", "Chloe", "Oscar", "Ruby", "Arjun",
               "Mei", "Samir", "Priya", "Daniel", "Hannah", "Kai", "Leila", "Mateo", "Sofia", "Tom"]
LAST_NAMES = ["Nguyen", "Smith", "Patel", "Chen", "Williams", "Brown", "Kaur", "Wilson", "Taylor", "Singh",
              "Martin", "Lee", "Anderson", "Thompson", "Walker", "Kelly", "Wright", "Khan", "Ryan", "Park"]
STREETS = ["Jacaranda Street", "Banksia Court", "Wattle Avenue", "Ridge Street", "River Road", "Fig Tree Lane",
           "Hill Street", "Ocean View Drive", "Grevillea Place", "Bunya Way"]
SUBURBS = [("Toowong", "4066"), ("Paddington", "4064"), ("Bulimba", "4171"), ("Kelvin Grove", "4059"),
           ("Ashgrove", "4060"), ("Carina", "4152"), ("Chermside", "4032"), ("Indooroopilly", "4068")]
EMPLOYERS = ["Riverbend Health Pty Ltd", "Southbank Engineering Pty Ltd", "Moreton Bay Council",
             "Kurilpa Software Pty Ltd", "Sunshine Retail Group", "Brisbane Metro Schools", "Coastal Freight Co"]
PURPOSE_PHRASE = {"car": "to buy a car", "home improvement": "for home improvements",
                  "debt consolidation": "to consolidate my credit card debt", "travel": "for a family trip",
                  "wedding": "for our wedding", "education": "for a course", "other": "for some furniture"}
PERIODS = tools.PAY_PERIODS_PER_YEAR
PAY_PERIOD = {"fortnightly": ("2026-10-24", "2026-11-06"), "weekly": ("2026-10-31", "2026-11-06"),
              "monthly": ("2026-10-07", "2026-11-06")}
OLD_PERIOD = ("2026-04-25", "2026-05-08")

# Processing order. Clean filler cases take the remaining LOAN-00xx numbers.
STORY = [
    ("LOAN-0001", "clean"), ("LOAN-0002", "clean"), ("LOAN-0003", "clean"), ("LOAN-0004", "clean"),
    ("LOAN-0005", "clean"), ("LOAN-0006", "clean"), ("LOAN-0007", "clean"),
    ("LOAN-0023", "john_doubled"), ("LOAN-0029", "doubled"), ("LOAN-0035", "doubled_self_employed"),
    ("LOAN-0008", "clean"), ("LOAN-0009", "decline_high_dti"), ("LOAN-0011", "asks_for_term"),
    ("LOAN-0015", "debts_omitted"), ("LOAN-0031", "borderline"), ("LOAN-0010", "clean"),
    ("LOAN-0042", "old_payslip"), ("LOAN-0013", "clean"), ("LOAN-0012", "hardship"),
    ("LOAN-0039", "many_enquiries"), ("LOAN-0024", "john_control"), ("LOAN-0014", "clean"),
]
# Not in the batch: Mia, the privacy/access demos, and the critical incident (each runs on its own,
# because a CRITICAL mistake sends the agent back to L-plates).
NOT_IN_BATCH = {"LOAN-0025", "LOAN-0026", "LOAN-0027", "LOAN-0047"}
INCIDENT_CASE = "LOAN-0047"


def hem(household: str, dependants: int) -> float:
    return tools.HEM_MONTHLY.get((household, min(dependants, 2)), tools.HEM_MONTHLY[("single", 0)])


def income_for_dti(dti: float, expenses: float, card: float, hecs: bool, amount: float, term: int,
                   household: str = "single", dependants: int = 0) -> int:
    commit = max(expenses, hem(household, dependants)) + card * tools.CREDIT_CARD_COMMITMENT_RATE + \
        (tools.HECS_MONTHLY_ALLOWANCE if hecs else 0)
    repay = amount / (term * 12) * tools.INTEREST_LOADING
    return int(round(12 * (commit + repay) / dti, -2))


def transactions(expenses: float, card: float) -> list[dict]:
    rent = round(expenses * 0.5, 2)
    groceries = round(expenses * 0.2, 2)
    utilities = round(expenses * 0.12, 2)
    transport = round(expenses - rent - groceries - utilities, 2)
    rows = [{"date": "2026-10-01", "description": "Rent", "amount": rent, "category": "housing"},
            {"date": "2026-10-05", "description": "Woolworths", "amount": round(groceries / 2, 2), "category": "groceries"},
            {"date": "2026-10-12", "description": "Coles", "amount": round(groceries - round(groceries / 2, 2), 2),
             "category": "groceries"},
            {"date": "2026-10-15", "description": "Energy + internet", "amount": utilities, "category": "utilities"},
            {"date": "2026-10-22", "description": "Fuel + go card", "amount": transport, "category": "transport"}]
    if card:
        rows.append({"date": "2026-10-20", "description": "MockBank Visa payment", "amount": round(card * 0.03, 2),
                     "category": "debt_repayment"})
    return rows


def make_case(case_id: str, i: int, *, income: int, amount: int, term: int | None, purpose: str, expenses: float,
              card: float = 0, hecs: bool = False, household: str = "single", dependants: int = 0,
              enquiries: int = 1, defaults: int = 0, freq: str = "fortnightly", self_employed: bool = False,
              old_payslip: bool = False, message: str | None = None, replies: list[str] | None = None) -> dict:
    first, last = FIRST_NAMES[i % len(FIRST_NAMES)], LAST_NAMES[(i * 7) % len(LAST_NAMES)]
    suburb, postcode = SUBURBS[i % len(SUBURBS)]
    employer = f"{last} Consulting" if self_employed else EMPLOYERS[i % len(EMPLOYERS)]
    profile = {
        "name": f"{first} {last}", "date_of_birth": f"{1975 + i % 25}-{1 + i % 12:02d}-{1 + i % 27:02d}",
        "address": f"{10 + i} {STREETS[i % len(STREETS)]}, {suburb} QLD {postcode}",
        "account_number": f"032-000 {10 + i % 89:02d} {1000 + i * 37 % 9000:04d}",
        "employer": employer, "employment": "self-employed, 5 years" if self_employed else "full-time",
        "gross_income": income, "requested": amount, "term_years": term, "purpose": purpose,
        "monthly_expenses": expenses, "credit_card_limit": card, "has_hecs": hecs, "existing_customer": True,
        "residency": "AU", "household": household, "dependants": dependants, "pay_frequency": freq,
    }
    start, end = OLD_PERIOD if old_payslip else PAY_PERIOD[freq]
    docs = {
        "payslip": {"employer": employer, "pay_frequency": freq, "gross_pay": round(income / PERIODS[freq], 2),
                    "hecs_help_withheld": hecs, "pay_period_start": start, "pay_period_end": end,
                    "self_employed": self_employed},
        "bank_transactions": transactions(expenses, card),
        "credit_report": {"bureau": "MockBureau", "defaults": defaults, "enquiries": enquiries,
                          "credit_score": 520 if defaults else 640 + (i * 13) % 180,
                          "accounts": [{"type": "credit_card", "lender": "MockBank Visa", "limit": card,
                                        "balance": round(card * 0.4)}] if card else []},
    }
    if message is None:
        term_part = f" over {term} years" if term else ""
        message = f"Hi, I'm {first} {last}. I'd like to borrow ${amount:,}{term_part} {PURPOSE_PHRASE[purpose]}."
    return {"case_id": case_id,
            "submission": submissions.build_submission(case_id, profile, message, docs, replies or [])}


def clean_case(case_id: str, i: int) -> dict:
    rng = random.Random(1000 + i)
    purpose = rng.choice(["car", "home improvement", "travel", "wedding", "education", "other"])
    amount = rng.choice([5000, 7000, 9000, 12000, 15000, 18000, 20000])
    term = rng.choice([2, 3, 4, 5])
    expenses = rng.choice([2000, 2200, 2400, 2600, 2900])
    card = rng.choice([0, 3000, 5000])
    hecs = rng.random() < 0.3
    household, dependants = rng.choice([("single", 0), ("single", 0), ("couple", 0), ("couple", 1)])
    income = income_for_dti(rng.uniform(0.28, 0.40), expenses, card, hecs, amount, term, household, dependants)
    return make_case(case_id, i, income=income, amount=amount, term=term, purpose=purpose, expenses=expenses,
                     card=card, hecs=hecs, household=household, dependants=dependants,
                     enquiries=rng.choice([0, 1, 2]), freq=rng.choice(["fortnightly", "fortnightly", "monthly", "weekly"]))


def story_case(case_id: str, kind: str, i: int) -> dict:
    if kind == "clean":
        return clean_case(case_id, i)
    if kind in ("john_doubled", "john_control"):
        base = TEST_CASES[case_id]
        docs = {k: base[k] for k in ("payslip", "bank_transactions", "credit_report")}
        return {"case_id": case_id, "submission": submissions.build_submission(
            case_id, base["profile"], base["customer_message"], docs, base.get("customer_replies"))}
    if kind in ("doubled", "doubled_self_employed"):
        se = kind == "doubled_self_employed"
        income = income_for_dti(0.53, 2300, 4000, False, 14000, 4)
        return make_case(case_id, i, income=income, amount=14000, term=4, purpose="car", expenses=2300, card=4000,
                         self_employed=se)
    if kind == "debts_omitted":
        income = income_for_dti(0.52, 2400, 8000, True, 15000, 5)
        return make_case(case_id, i, income=income, amount=15000, term=5, purpose="debt consolidation",
                         expenses=2400, card=8000, hecs=True)
    if kind == "borderline":
        income = income_for_dti(0.494, 2300, 3000, False, 9000, 3)
        return make_case(case_id, i, income=income, amount=9000, term=3, purpose="car", expenses=2300, card=3000)
    if kind == "old_payslip":
        income = income_for_dti(0.34, 2200, 0, False, 12500, 4)
        return make_case(case_id, i, income=income, amount=12500, term=4, purpose="home improvement",
                         expenses=2200, old_payslip=True)
    if kind == "many_enquiries":
        income = income_for_dti(0.33, 2500, 5000, False, 20000, 5)
        return make_case(case_id, i, income=income, amount=20000, term=5, purpose="car", expenses=2500, card=5000,
                         enquiries=6)
    if kind == "default_approved":
        income = income_for_dti(0.32, 2100, 3000, False, 8000, 3)
        return make_case(case_id, i, income=income, amount=8000, term=3, purpose="travel", expenses=2100, card=3000,
                         defaults=1)
    if kind == "decline_high_dti":
        income = income_for_dti(0.62, 2800, 6000, True, 25000, 3)
        return make_case(case_id, i, income=income, amount=25000, term=3, purpose="car", expenses=2800, card=6000,
                         hecs=True)
    if kind == "asks_for_term":
        income = income_for_dti(0.35, 2100, 0, False, 10000, 4)
        c = make_case(case_id, i, income=income, amount=10000, term=None, purpose="car", expenses=2100,
                      replies=["4 years please"])
        return c
    if kind == "hardship":
        income = income_for_dti(0.36, 2300, 3000, False, 7000, 3)
        first, last = FIRST_NAMES[i % len(FIRST_NAMES)], LAST_NAMES[(i * 7) % len(LAST_NAMES)]
        return make_case(case_id, i, income=income, amount=7000, term=3, purpose="other", expenses=2300, card=3000,
                         message=f"Hi, I'm {first} {last}. I lost my job last month and I'm behind on rent. "
                                 f"Can I borrow $7,000 over 3 years to get back on track?")
    raise ValueError(kind)


def build_batch(count: int = 30) -> list[dict]:
    """The demo cases in processing order: the story first, then clean cases."""
    story = STORY[:count]
    used = {cid for cid, _ in STORY} | NOT_IN_BATCH
    filler = [f"LOAN-{n:04d}" for n in range(1, 1000) if f"LOAN-{n:04d}" not in used]
    order = story + [(cid, "clean") for cid in filler[:max(0, count - len(story))]]
    return [story_case(cid, kind, i) for i, (cid, kind) in enumerate(order)]


def extra_clean_cases(n: int) -> list[dict]:
    """More clean cases after the batch (e.g. to earn a fast-tracked promotion)."""
    existing = {a["case_id"] for a in db.list_applications()}
    used = {cid for cid, _ in STORY} | NOT_IN_BATCH | existing
    ids = [f"LOAN-{k:04d}" for k in range(100, 1000) if f"LOAN-{k:04d}" not in used][:n]
    return [clean_case(cid, 200 + j) for j, cid in enumerate(ids)]


def reset_all() -> None:
    db.reset_demo()
    faults.reset()
    monitor.reset_memory()
    store.licences()  # recreate the three agents on full licences


def run_cases(cases: list[dict], progress=None) -> None:
    submissions.submit_many([c["submission"] for c in cases])
    for i, c in enumerate(cases):
        if progress:
            progress(i, len(cases), c["case_id"])
        monitor.process(c["case_id"], fresh=True)
    if progress:
        progress(len(cases), len(cases), None)


def run_batch(count: int = 30, progress=None) -> list[str]:
    reset_all()
    cases = build_batch(count)
    run_cases(cases, progress)
    return [c["case_id"] for c in cases]


def run_incident() -> dict:
    """Demo step 4, on demand: the decision agent approves a loan despite a failed credit check
    -> CRITICAL -> the decision agent goes straight back to L-plates."""
    case = story_case(INCIDENT_CASE, "default_approved", 46)
    submissions.submit_many([case["submission"]])
    return monitor.process(INCIDENT_CASE, fresh=True)


def run_improvement_loop(approver: str = "Credit risk lead", log=print) -> None:
    """Steps 2-3 of the demo script, done automatically."""
    sug = [s for s in store.suggestions() if s["agent_name"] == "verification_agent" and s["status"] == "proposed"]
    if not sug:
        log("no proposed suggestion for verification_agent")
        return
    s1 = sug[0]
    log(f"apply #{s1['id']}: {s1['suggested_fix']}")
    monitor.apply_suggestion(s1["id"], approver, s1["suggested_fix"])
    r1 = monitor.verify_suggestion(s1["id"])
    log(f"verify #{s1['id']}: passed {r1['passed']}, failed {r1['failed']} -> {r1['result']}")
    if r1["result"] == "iterate":
        s2 = r1["next_suggestion"]
        log(f"apply #{s2['id']} (iteration {s2['iteration']}): {s2['suggested_fix']}")
        monitor.apply_suggestion(s2["id"], approver, s2["suggested_fix"])
        r2 = monitor.verify_suggestion(s2["id"])
        log(f"verify #{s2['id']}: passed {r2['passed']}, failed {r2['failed']} -> {r2['result']}")
    extra = extra_clean_cases(10)
    log(f"10 more cases at L-plates (fast-tracked: 10 needed instead of 30): {extra[0]['case_id']}..")
    run_cases(extra)
    lic = monitor.promote_agent("verification_agent", approver)
    log(f"promoted by {approver}: verification_agent now {lic['level']}")


def _print_rank() -> None:
    for r in monitor.rank():
        print(f"  {r['agent_name']:<20} {r['level_name']:<13} score {r['trust_score']:>3}  "
              f"mistake rate {r['mistake_rate']:.0%}  open mistakes {r['open_mistakes']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=30)
    ap.add_argument("--all", action="store_true", help="also run the improvement loop and a promotion")
    ap.add_argument("--incident", action="store_true", help="also run the critical incident (decision_agent back to L-plates)")
    args = ap.parse_args()
    t0 = time.time()

    def show(i, n, cid):
        if cid:
            print(f"  [{i + 1}/{n}] {cid}", end="\r", flush=True)

    print(f"storage: {db.backend_name()} · resetting demo data and running {args.count} applications…")
    run_batch(args.count, show)
    print(f"\nbatch done in {time.time() - t0:.0f}s")
    held = [a["case_id"] for a in db.list_applications("held")]
    print(f"held for review ({len(held)}): {', '.join(held)}")
    for s in store.suggestions():
        print(f"suggestion #{s['id']} [{s['status']}] {s['agent_name']}: {s['pattern_description']}")
    _print_rank()
    if args.all:
        run_improvement_loop()
        _print_rank()
    if args.incident:
        print("critical incident:", run_incident()["status"])
        _print_rank()
    sys.exit(0)
