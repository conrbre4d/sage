"""Tests for AgentLens (agentlens/). Offline: local SQLite, no AI calls, no Supabase."""

import pytest


@pytest.fixture()
def lens(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLENS_BACKEND", "local")
    monkeypatch.setenv("AGENTLENS_DB", str(tmp_path / "lens.db"))
    monkeypatch.setenv("AGENTLENS_LLM_CACHE", str(tmp_path / "llm_cache.json"))
    monkeypatch.setenv("AGENTLENS_OFFLINE", "1")
    monkeypatch.setenv("AGENTLENS_QUIET", "1")
    monkeypatch.delenv("AGENTLENS_ENCRYPTION_KEY", raising=False)
    import db
    import demo_seed
    import faults
    import monitor
    db._forget()
    faults.reset()
    monitor.reset_memory()
    return demo_seed


def ev(step, agent="verification_agent", result="pass", notes="ok", inp=None, out=None, eid=10, case="LOAN-9001"):
    return {"event_id": eid, "case_id": case, "agent_name": agent, "step_name": step, "result": result,
            "notes": notes, "input_data": inp or {}, "output_data": out or {}}


# --- detect.py (pure) ------------------------------------------------------------

def test_doubled_income_is_a_major_mistake_in_plain_english():
    import detect
    e = ev("verify_income", inp={"payslip_gross_pay": 2769.23, "payslip_pay_frequency": "fortnightly"},
           out={"verified_annual_income": 143999.96, "pay_frequency_used": "weekly", "periods_per_year_used": 52})
    [m] = detect.check_event(e, [ev("ai.read_documents", eid=9), ev("docs.extract_payslip", eid=1),
                                 ev("docs.extract_statements", eid=2)])
    assert m["mistake_type"] == "MAJOR" and m["pattern_key"] == "misreads_field:pay_frequency"
    assert m["mistake_reason"] == ("Agent concluded income=$144,000 but the payslip shows fortnightly pay of $2,769 "
                                   "(= $72,000/yr) — treated fortnightly as weekly.")
    assert m["details"]["title"] == "Income doubled"


def test_missing_reasoning_is_minor():
    import detect
    prior = [ev(s, eid=i) for i, s in enumerate(["docs.extract_payslip", "docs.extract_statements"])]
    found = detect.check_event(ev("ai.read_documents", notes=""), prior)
    assert [m["mistake_type"] for m in found] == ["MINOR"]


def test_skipped_step_is_major():
    import detect
    found = detect.check_event(ev("verify_income", inp={}, out={}), [])  # no ai.read_documents before it
    assert [(m["mistake_type"], m["pattern_key"]) for m in found] == [("MAJOR", "skips_step:ai.read_documents")]


def test_approving_after_a_failed_check_is_critical():
    import detect
    prior = [ev(s, eid=i, result="fail" if s == "credit.pull_report" else "pass", notes="1 default(s)")
             for i, s in enumerate(["verify_income", "credit.pull_report", "rules.eligibility",
                                    "calc.serviceability", "rules.policy"])]
    found = detect.check_event(ev("ai.decide", agent="decision_agent", out={"final_decision": "approved"},
                                  inp={"text_sent_to_llm": "{}"}), prior)
    assert [(m["mistake_type"], m["rule"]) for m in found] == [("CRITICAL", "ignored_failed_check")]


def test_privacy_and_access_rules_are_critical():
    import detect
    dp3 = detect.check_event(ev("ai.read_documents", inp={"text_sent_to_llm": "Address: 27 Wattle Avenue, Paddington QLD 4064"}),
                             [ev("docs.extract_payslip", eid=1), ev("docs.extract_statements", eid=2)])
    assert [(m["mistake_type"], m["rule"]) for m in dp3] == [("CRITICAL", "DP3")]
    dp4 = detect.check_event(ev("access.violation", result="fail", notes="DP4 refused",
                                inp={"requested_customer_token": "CUST-9B21"}), [])
    assert [(m["mistake_type"], m["rule"]) for m in dp4] == [("CRITICAL", "DP4")]


def test_repeat_on_four_cases_becomes_critical():
    import detect
    m = {"case_id": "D", "mistake_type": "MAJOR", "pattern_key": "misreads_field:x", "details": {"title": "t"}}
    assert detect.repeated_mistake(m, {"A", "B"}, 4) is None
    assert detect.repeated_mistake(m, {"A", "B", "C"}, 4)["mistake_type"] == "CRITICAL"


# --- scoring.py (pure) -------------------------------------------------------------

def test_seven_correct_then_three_majors_gives_p1():
    import scoring
    lic = scoring.new_licence("verification_agent")
    ms = []
    for i in range(10):
        scoring.record_case(lic, f"C{i}")
        if i >= 7:
            m = {"agent_name": "verification_agent", "case_id": f"C{i}", "mistake_type": "MAJOR",
                 "pattern_key": "p", "details": {"title": "t"}, "rule": "r"}
            ms.append(m)
            scoring.apply_mistake(lic, m, "now")
        scoring.demote_by_rate(lic, scoring.mistake_rate(lic, ms), "now")
    assert lic["level"] == "P1" and lic["trust_score"] == 94


def test_critical_sends_back_to_l_plates_and_promotion_is_never_automatic():
    import scoring
    assert "SUSPENDED" not in scoring.LEVELS and scoring.normalise_level("SUSPENDED") == "L"
    lic = scoring.new_licence("decision_agent")
    scoring.apply_mistake(lic, {"agent_name": "decision_agent", "case_id": "C", "mistake_type": "CRITICAL",
                                "details": {"title": "t"}, "rule": "r"}, "now")
    assert lic["level"] == "L"  # straight from Full licence to L-plates
    assert scoring.promotion_check(lic, 0)[0] is False  # L needs 30 cases first
    for i in range(100):  # lots of clean cases: still on L-plates until a person promotes
        scoring.record_case(lic, f"X{i}")
        scoring.demote_by_rate(lic, 0.0, "now")
    assert lic["level"] == "L"
    assert scoring.promotion_check(lic, criticals_since_change=0) == (True, "ok")
    scoring.promote(lic, "Sam", "now")
    assert lic["level"] == "P1"


# --- the demo script, end to end on the real pipeline ------------------------------

def test_demo_script(lens):
    import monitor
    import store
    import db

    lens.run_batch(23)
    held = {a["case_id"] for a in db.list_applications("held")}
    assert held == {"LOAN-0023", "LOAN-0029", "LOAN-0035", "LOAN-0015", "LOAN-0031", "LOAN-0042", "LOAN-0039"}
    lic = store.licences()
    # 1. verification: 7 correct, 3 doubled incomes -> P1 (and stays P1), suggestion #1
    assert lic["verification_agent"]["level"] == "P1"
    [s1] = store.suggestions()
    assert s1["pattern_key"] == "misreads_field:pay_frequency" and s1["case_ids"] == ["LOAN-0023", "LOAN-0029", "LOAN-0035"]
    assert "pay_frequency" in s1["suggested_fix"]
    # after the batch: decision is on a P plate, intake on a full licence
    assert lic["decision_agent"]["level"] == "P2"
    assert lic["intake_agent"]["level"] == "FULL"
    assert [r["agent_name"] for r in monitor.rank()][0] == "verification_agent"

    # 2. apply #1, verify -> 2 of 3 pass, the self-employed one doesn't -> iteration 2
    monitor.apply_suggestion(s1["id"], "Sam", s1["suggested_fix"])
    assert lic["verification_agent"]["level"] == "L"
    r1 = monitor.verify_suggestion(s1["id"])
    assert r1["result"] == "iterate" and r1["failed"] == ["LOAN-0035"]
    s2 = r1["next_suggestion"]
    assert s2["iteration"] == 2 and "self-employed" in s2["suggested_fix"]

    # 3. apply #2, verify -> all 3 pass -> resolved, fast-tracked
    monitor.apply_suggestion(s2["id"], "Sam", s2["suggested_fix"])
    r2 = monitor.verify_suggestion(s2["id"])
    assert r2["result"] == "resolved" and r2["failed"] == [] and len(r2["passed"]) == 3
    assert lic["verification_agent"]["fast_track"] is True
    assert all(m["resolved"] for m in store.mistakes() if m["pattern_key"] == "misreads_field:pay_frequency")

    # promotion needs a person AND the (fast-tracked) case count
    with pytest.raises(ValueError, match="needs 10 cases"):
        monitor.promote_agent("verification_agent", "Sam")
    lens.run_cases(lens.extra_clean_cases(10))
    monitor.promote_agent("verification_agent", "Sam")
    assert lic["verification_agent"]["level"] == "P1"
    # promotion can't happen without a name
    with pytest.raises(ValueError, match="named person"):
        monitor.promote_agent("decision_agent", " ")

    # 4. the critical incident, on demand: approved despite a failed credit check -> back to L-plates
    out = lens.run_incident()
    assert out["status"] == "held"
    assert lic["decision_agent"]["level"] == "L"
    assert any(m["case_id"] == "LOAN-0047" and m["mistake_type"] == "CRITICAL" for m in store.mistakes())


def test_privacy_and_access_demos_are_caught(lens):
    import db
    import main
    import monitor
    import store
    import submissions

    lens.reset_all()
    for cid in ("LOAN-0026", "LOAN-0027"):
        submissions.submit_test_case(cid)
        monitor.process(cid)
    rules = {(m["case_id"], m["rule"]) for m in store.mistakes()}
    assert ("LOAN-0026", "DP3") in rules and ("LOAN-0027", "DP4") in rules
    assert store.licences()["verification_agent"]["level"] == "L"
    assert {a["case_id"] for a in db.list_applications("held")} == {"LOAN-0026", "LOAN-0027"}
    assert main.db.get_tracker("LOAN-0026")["message"] is None  # held before the customer was told anything


def test_reviewer_actions(lens):
    import db
    import monitor

    lens.run_batch(23)
    held = db.get_application("LOAN-0031")
    assert held["estimated_timeframe"].startswith("Within 1 business day")
    monitor.review_case("LOAN-0031", "decline", "Sam")
    a = db.get_application("LOAN-0031")
    assert a["status"] == "declined" and a["reviewed_by"] == "Sam" and a["approval_probability"] == 0.0
    out = monitor.review_case("LOAN-0015", "recalculate", "Sam")  # fault fixed, re-run with all debts
    assert out["status"] == "declined"  # true DTI is over 50% once the debts are counted
    assert db.get_application("LOAN-0015")["reviewed_by"] == "Sam"
    with pytest.raises(ValueError, match="not held"):
        monitor.review_case("LOAN-0001", "approve", "Sam")


def test_estimates_are_stored_and_updated(lens):
    import db
    import estimates
    lens.run_batch(22)
    apps = {a["case_id"]: a for a in db.list_applications()}
    assert apps["LOAN-0024"]["approval_probability"] == 0.0 and apps["LOAN-0024"]["estimated_timeframe"] == "Decided"
    approved = [a for a in apps.values() if a["status"] == "approved"]
    assert approved and all(a["approval_probability"] == 1.0 for a in approved)
    # John's held case: estimated from his own form (DTI 52%), not the agent's doubled income
    assert apps["LOAN-0023"]["approval_probability"] == estimates.probability(estimates.dti_from_form(apps["LOAN-0023"]))
    assert apps["LOAN-0023"]["approval_probability"] <= 0.2
    assert estimates.probability(0.25) > estimates.probability(0.45) > estimates.probability(0.6)
    assert estimates.probability(0.25, defaults=1) == 0.05


def test_reviewer_sees_form_and_documents_masked_unless_revealed(lens):
    import db
    import main
    lens.run_batch(22)
    f = main.application_form("LOAN-0023")
    about = dict(f["sections"][0]["fields"])
    assert about["Full name"] == "J••• D•••••" and "Davies" not in str(f)
    assert "[NAME]" in f["message"]
    docs = main.application_documents("LOAN-0023")["documents"]
    assert set(docs) == {"payslip", "bank_statement", "credit_report"}
    assert "John" not in docs["payslip"]["text"] and "Fortnightly" in docs["payslip"]["text"]
    assert [e for e in db.get_events("LOAN-0023") if e["step_name"] == "vault.view_identity"] == []
    shown = main.application_form("LOAN-0023", reveal=True, viewer="Sam")
    assert dict(shown["sections"][0]["fields"])["Full name"] == "John Davies"
    assert "John Davies" in main.application_documents("LOAN-0023", reveal=True, viewer="Sam")["documents"]["payslip"]["text"]
    views = [e for e in db.get_events("LOAN-0023") if e["step_name"] == "vault.view_identity"]
    assert len(views) == 2 and views[0]["input_data"]["viewer"] == "Sam"


def test_timeline_is_plain_english(lens):
    import agentlens
    lens.run_batch(22)
    d = agentlens.case_detail("LOAN-0023")
    titles = [t["title"] for t in d["timeline"]]
    assert "Checked the income" in titles and "AgentLens held the case for a person" in titles
    income = next(t for t in d["timeline"] if t["step"] == "verify_income")
    assert dict(income["facts"])["Income used (a year)"] == "$144,000"
    assert d["primary"]["details"]["title"] == "Income doubled"


def test_agentlens_never_reads_the_vault():
    import inspect
    import detect
    import improve
    import monitor
    import scoring
    import store
    for mod in (detect, improve, monitor, scoring, store):
        src = inspect.getsource(mod)
        assert "get_identity" not in src and "customer_identity" not in src, mod.__name__
