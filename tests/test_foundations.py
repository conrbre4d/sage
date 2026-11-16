"""Smoke tests for the shared foundations. Run with:  python -m pytest"""

import pandas as pd
import pytest

from shared import constants as C
from shared.validate import (
    DEFAULT_CONFIG, DEFAULT_EVENTS, load_config, load_events,
    validate_config, validate_events,
)


def errors(issues):
    return [i for i in issues if i.level == "error"]


@pytest.fixture(scope="module")
def raw():
    return pd.read_csv(DEFAULT_EVENTS, dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def events():
    return load_events(DEFAULT_EVENTS)


@pytest.fixture(scope="module")
def cfg():
    return load_config(DEFAULT_CONFIG)


# --- the shipped files are valid -----------------------------------------

def test_sample_events_valid(raw):
    assert errors(validate_events(raw)) == []


def test_config_valid(cfg):
    assert errors(validate_config(cfg)) == []


def test_columns_match_schema(raw):
    assert list(raw.columns) == C.EVENT_COLUMNS


def test_load_events_types(events):
    assert str(events["timestamp"].dt.tz) == C.TIMEZONE
    assert pd.api.types.is_float_dtype(events["loan_amount"])
    assert events["timestamp"].notna().all()


def test_every_agent_row_has_licence_tags(events):
    agents = events[events["actor_type"] == C.ACTOR_AGENT]
    assert (agents["agent_name"] != "").all()
    assert (agents["licence_class"] != "").all()
    assert (agents["model_version"] != "").all()


# --- the demo story is in the data ---------------------------------------

def test_hero_case_income_doubled(events):
    ext = events[(events.case_id == C.HERO_CASE) & (events.activity == "Extract payslip")].iloc[-1]
    rec = events[(events.case_id == C.HERO_CASE) & (events.activity == "Receive")].iloc[0]
    assert ext.doc_pay_frequency == "fortnightly" and ext.read_pay_frequency == "weekly"
    assert ext.agent_name == C.AGENT_PAYSLIP_READER
    assert ext.licence_class == C.LICENCE_PAYSLIP_STANDARD
    assert ext.extracted_income_annual / rec.declared_income_annual == pytest.approx(2.0, rel=0.01)
    dec = events[(events.case_id == C.HERO_CASE) & (events.activity == "Decision")].iloc[0]
    assert dec.decision == "approved"


def test_hero_true_income_needs_human(events):
    """With the correct income, LOAN-0023 is above 6x and should have gone to a human."""
    g = events[events.case_id == C.HERO_CASE]
    loan = g.loan_amount.dropna().iloc[0]
    gross = g.payslip_gross.dropna().iloc[0]
    assert loan / (gross * C.PAY_FREQUENCY_PERIODS["fortnightly"]) > 6.0
    assert "Human review" not in set(g.activity)


def test_three_misreads_in_24h(events):
    ext = events[(events.activity == "Extract payslip")
                 & (events.read_pay_frequency != events.doc_pay_frequency)]
    assert set(ext.agent_name) == {C.AGENT_PAYSLIP_READER}
    assert len(ext) == 3
    assert ext.timestamp.max() - ext.timestamp.min() <= pd.Timedelta(hours=24)
    assert C.HERO_CASE in set(ext.case_id)


def test_supporting_case_skips_scam_check(events):
    acts = list(events[events.case_id == C.SUPPORTING_CASE].sort_values("timestamp").activity)
    assert "Scam check" not in acts and acts[-1] == "Release"


def test_clean_cases_follow_happy_path(events):
    for cid in ("LOAN-0001", "PAY-0001"):
        g = events[events.case_id == cid].sort_values("timestamp")
        assert list(g.activity) == C.HAPPY_PATH[g.process.iloc[0]]


def test_planted_faults(events):
    planted = set(events.loc[events.gt_fault != "", "case_id"])
    assert planted == {"LOAN-0012", "LOAN-0019", "LOAN-0021", "LOAN-0023", "PAY-0047"}


def test_no_real_names_in_log(raw):
    names = pd.read_csv(DEFAULT_EVENTS.parent / "customers.csv")["display_name"]
    blob = raw.to_csv()
    for n in names:
        assert n not in blob and n.split()[0] not in blob


# --- the validator catches mistakes ---------------------------------------

def test_catches_missing_column(raw):
    assert errors(validate_events(raw.drop(columns=["agent_name"])))


def test_catches_unknown_agent(raw):
    bad = raw.copy()
    i = bad.index[bad.actor_type == "agent"][0]
    bad.loc[i, "agent_name"] = "Mystery Bot"
    assert any("unknown agent_name" in i.message for i in errors(validate_events(bad)))


def test_catches_wrong_licence_class(raw):
    bad = raw.copy()
    i = bad.index[bad.agent_name == C.AGENT_PAYMENTS][0]
    bad.loc[i, "licence_class"] = C.LICENCE_PAYSLIP_STANDARD
    assert any("cannot hold" in i.message for i in errors(validate_events(bad)))


def test_catches_missing_licence_class(raw):
    bad = raw.copy()
    i = bad.index[bad.actor_type == "agent"][0]
    bad.loc[i, "licence_class"] = ""
    assert errors(validate_events(bad))


def test_catches_unpseudonymised_customer(raw):
    bad = raw.copy()
    bad.loc[0, "customer_ref"] = "Priya Sharma"
    assert any("pseudonymised" in i.message for i in errors(validate_events(bad)))


def test_catches_pii_in_note(raw):
    bad = raw.copy()
    bad.loc[0, "note"] = "call me on 0412 345 678 or priya@example.com"
    msgs = [i.message for i in errors(validate_events(bad))]
    assert any("email" in m for m in msgs) and any("phone" in m for m in msgs)


def test_catches_bad_config_activity(cfg):
    import copy
    bad = copy.deepcopy(cfg)
    bad["rules"][0]["check"]["requires_before"] = "Credit check"
    assert any("does not exist" in i.message for i in errors(validate_config(bad)))


def test_catches_llm_privacy_leak(cfg):
    import copy
    bad = copy.deepcopy(cfg)
    bad["privacy"]["llm_allowed_columns"].append("customer_ref")
    assert any("both allowed and forbidden" in i.message for i in errors(validate_config(bad)))
