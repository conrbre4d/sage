"""Tests for the mock loan pipeline in agents/. Runs offline (no Claude call)."""

import pytest

AGENT_ORDER = [
    "intake_agent", "identity_eligibility_agent", "income_verification_agent",
    "expense_liability_agent", "credit_bureau_agent", "serviceability_agent",
    "policy_rules_agent", "decision_agent", "notification_agent",
]


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLENS_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("AGENTLENS_OFFLINE", "1")
    import main
    return main


def by_agent(events):
    return {e["agent_name"]: e for e in events}


def test_nine_events_in_order(pipeline):
    for cid in ("LOAN-0023", "LOAN-0024"):
        events = pipeline.run_case(cid)["events"]
        assert [e["agent_name"] for e in events] == AGENT_ORDER
        assert all(e["result"] in ("pass", "fail", "flag") for e in events)
        assert all(e["timestamp"] and e["notes"] for e in events)


def test_demo_case_is_wrongly_approved(pipeline):
    out = pipeline.run_case("LOAN-0023")
    ev = by_agent(out["events"])
    assert out["decision"] == "approved"
    inc = ev["income_verification_agent"]["output_data"]
    assert inc["verified_annual_income"] == pytest.approx(144000, rel=0.001)
    assert inc["payslip_pay_frequency"] == "fortnightly"
    assert inc["periods_per_year_used"] == 52
    assert ev["income_verification_agent"]["result"] == "pass"  # the bug is silent


def test_control_case_is_declined(pipeline):
    out = pipeline.run_case("LOAN-0024")
    ev = by_agent(out["events"])
    assert out["decision"] == "declined"
    assert ev["income_verification_agent"]["output_data"]["verified_annual_income"] == pytest.approx(72000, rel=0.001)
    assert ev["serviceability_agent"]["result"] == "fail"
    assert "DTI ratio exceeds 50%" in ev["serviceability_agent"]["notes"]


def test_serviceability_reads_income_event(pipeline):
    ev = by_agent(pipeline.run_case("LOAN-0023")["events"])
    inp = ev["serviceability_agent"]["input_data"]
    assert inp["upstream_event_ids"]["income_verification_agent"] == ev["income_verification_agent"]["event_id"]
    assert inp["verified_monthly_income"] == ev["income_verification_agent"]["output_data"]["verified_monthly_income"]


def test_rerun_replaces_previous_run(pipeline):
    pipeline.run_case("LOAN-0023")
    pipeline.run_case("LOAN-0023")
    assert len(pipeline.db.get_events("LOAN-0023")) == 9


def test_missing_field_flags_and_refers(pipeline):
    out = pipeline.run_case("LOAN-0024", customer_message="Hi, I'm John. I need $15,000 please.")
    ev = by_agent(out["events"])
    assert ev["intake_agent"]["result"] == "flag"
    assert "missing field" in ev["intake_agent"]["notes"]
    assert out["decision"] == "declined"  # serviceability still fails on true income


def test_under_18_fails(pipeline):
    profile = dict(pipeline.TEST_CASES["LOAN-0024"]["profile"], age=17, gross_income=150000)
    msg = "I'm Sam, I'm 17. I want $5,000 over 2 years for a car."
    out = pipeline.run_case("LOAN-0099", customer_message=msg, profile=profile)
    assert by_agent(out["events"])["identity_eligibility_agent"]["result"] == "fail"
    assert out["decision"] == "declined"


def test_rule_parser_reads_test_message(pipeline):
    msg = pipeline.TEST_CASES["LOAN-0023"]["customer_message"]
    assert pipeline.parse_with_rules(msg) == {
        "name": "John", "age": 27, "loan_amount": 15000.0, "term_years": 5,
        "purpose": "debt consolidation",
    }


def test_api_endpoints(pipeline):
    from fastapi.testclient import TestClient

    try:
        test_client = TestClient(pipeline.app)
    except TypeError as e:  # old starlette + httpx>=0.28
        pytest.skip(f"TestClient incompatible with installed httpx ({e}); pip install -U fastapi")

    with test_client as client:
        r = client.post("/submit_case", json={"case_id": "LOAN-0023"})
        assert r.status_code == 200 and r.json()["decision"] == "approved"
        client.post("/submit_case", json={"case_id": "LOAN-0024"})

        assert len(client.get("/event_log/LOAN-0023").json()) == 9
        assert len(client.get("/event_log").json()) == 18
        assert client.get("/tracker/LOAN-0023").json()["status"] == "approved"
        assert client.get("/tracker/LOAN-0024").json()["status"] == "declined"
        assert client.get("/event_log/LOAN-9999").status_code == 404
        assert client.post("/submit_case", json={"case_id": "LOAN-0500"}).status_code == 400
        assert client.post("/submit_case", json={"case_id": "bad"}).status_code == 422
