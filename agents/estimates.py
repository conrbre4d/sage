"""Estimated time to a decision and probability of approval, stored on each application.

Updated at every status change:
  submitted        estimate from the form's own figures (stated income, expenses, debts)
  verifying        "less than a minute"
  deciding         re-estimated from the VERIFIED figures (DTI, credit report, flags)
  held             "within 1 business day"; probability back to the form's own figures
                   (AgentLens held it because the agent's figures are suspect)
  referred         "within 1 business day": a credit officer is reviewing
  approved         100% · declined 0%

This is a simple rules-based guide for the customer and the reviewer, not a credit model.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

BRISBANE = timezone(timedelta(hours=10))


def _now() -> datetime:
    return datetime.now(BRISBANE)


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def next_business_day_5pm(now: datetime) -> datetime:
    d = now + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.replace(hour=17, minute=0, second=0, microsecond=0)


def probability(dti: float | None, defaults: int = 0, enquiries: int = 0, flagged: bool = False) -> float | None:
    """Rough chance of approval from affordability and credit history."""
    if dti is None:
        return None
    if defaults:
        return 0.05
    p = (0.95 if dti <= 0.30 else 0.85 if dti <= 0.38 else 0.70 if dti <= 0.45 else
         0.50 if dti <= 0.50 else 0.20 if dti <= 0.55 else 0.05)
    if enquiries > 3:
        p *= 0.7
    if flagged:
        p *= 0.8
    return round(p, 2)


def dti_from_form(app: dict) -> float | None:
    """DTI from what the customer typed in (before anything is verified)."""
    import tools  # here, not at the top: tools imports access, which imports this module
    try:
        income = float(app["stated_annual_income"]) / 12
        hem = tools.HEM_MONTHLY.get((app.get("household") or "single", min(int(app.get("dependants") or 0), 2)), 1950)
        expenses = max(float(app.get("monthly_expenses") or 0), hem)
        card = float(app.get("credit_card_limit") or 0) * tools.CREDIT_CARD_COMMITMENT_RATE
        hecs = tools.HECS_MONTHLY_ALLOWANCE if app.get("has_hecs") else 0
        term = int(app.get("term_years") or 5)
        repay = float(app["requested_amount"]) / (term * 12) * tools.INTEREST_LOADING
        return (expenses + card + hecs + repay) / income if income else None
    except (KeyError, TypeError, ValueError):
        return None


def for_submission(app: dict) -> dict:
    now = _now()
    return {"approval_probability": probability(dti_from_form(app)),
            "estimated_timeframe": "About 2 minutes · automated assessment",
            "estimated_decision_by": _iso(now + timedelta(minutes=2)), "status_updated_at": _iso(now)}


def for_status(status: str, app: dict, verification: dict | None = None) -> dict:
    """The estimate fields to store when a case moves to `status`."""
    now = _now()
    out = {"status_updated_at": _iso(now)}
    if status == "submitted":
        return {**out, **for_submission(app)}
    if status == "verifying":
        return {**out, "estimated_timeframe": "Less than a minute · checking documents",
                "estimated_decision_by": _iso(now + timedelta(minutes=1))}
    if status == "deciding":
        p = app.get("approval_probability")
        if verification:
            kf = verification.get("key_figures", {})
            flagged = any(c.get("result") in ("flag", "fail") for c in verification.get("checks", []))
            dti = kf.get("dti_pct")
            p = probability(dti / 100 if dti is not None else None, kf.get("defaults") or 0,
                            kf.get("enquiries") or 0, flagged) if dti is not None else p
        return {**out, "approval_probability": p, "estimated_timeframe": "Less than a minute · making a decision",
                "estimated_decision_by": _iso(now + timedelta(minutes=1))}
    if status == "held":
        # the agent's figures are suspect, so go back to the customer's own figures
        return {**out, "approval_probability": probability(dti_from_form(app)),
                "estimated_timeframe": "Within 1 business day · a credit officer is reviewing",
                "estimated_decision_by": _iso(next_business_day_5pm(now))}
    if status == "referred":
        return {**out, "estimated_timeframe": "Within 1 business day · a credit officer is reviewing",
                "estimated_decision_by": _iso(next_business_day_5pm(now))}
    if status == "approved":
        return {**out, "approval_probability": 1.0, "estimated_timeframe": "Decided",
                "estimated_decision_by": _iso(now)}
    if status == "declined":
        return {**out, "approval_probability": 0.0, "estimated_timeframe": "Decided",
                "estimated_decision_by": _iso(now)}
    if status == "error":
        return {**out, "estimated_timeframe": "Delayed · we'll be in touch"}
    return out
