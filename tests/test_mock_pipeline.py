"""Tests for the mock loan pipeline in agents/. Runs offline (no LLM call, no Supabase, $0).

Storage: local SQLite in a temp folder. One test runs the whole pipeline against a
fake Supabase REST server, to check supabase_db.py speaks PostgREST correctly.
The AI steps fall back to plain-Python rules offline; tests that need the AI's
behaviour (tool calling, guardrails) use fake LLM replies.
"""

import inspect
import json
import re
from urllib.parse import parse_qs

import pytest

ORCH = ("orchestrator", "access.issue_ticket")
STEP_ORDER = [
    ORCH,
    ("intake_agent", "crm.get_application"),
    ("intake_agent", "chat.get_message"),
    ("intake_agent", "ai.parse_request"),
    ("intake_agent", "case.write_handoff"),
    ("intake_agent", "case.set_status"),
    ORCH,
    ("verification_agent", "case.read_handoff"),
    ("verification_agent", "crm.get_application"),
    ("verification_agent", "docs.extract_payslip"),
    ("verification_agent", "docs.extract_statements"),
    ("verification_agent", "docs.match_identity"),
    ("verification_agent", "credit.pull_report"),
    ("verification_agent", "ai.read_documents"),
    ("verification_agent", "verify_income"),
    ("verification_agent", "identity.verify"),
    ("verification_agent", "benchmarks.hem"),
    ("verification_agent", "rules.eligibility"),
    ("verification_agent", "calc.serviceability"),
    ("verification_agent", "rules.policy"),
    ("verification_agent", "case.write_handoff"),
    ("verification_agent", "case.set_status"),
    ORCH,
    ("decision_agent", "case.read_handoff"),
    ("decision_agent", "ai.decide"),
    ("decision_agent", "case.write_handoff"),
    ("decision_agent", "case.set_status"),
    ORCH,
    ("notification_service", "notify.send_template"),
]


@pytest.fixture()
def pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLENS_BACKEND", "local")
    monkeypatch.setenv("AGENTLENS_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("AGENTLENS_LLM_CACHE", str(tmp_path / "llm_cache.json"))
    monkeypatch.setenv("AGENTLENS_OFFLINE", "1")
    monkeypatch.setenv("AGENTLENS_QUIET", "1")
    monkeypatch.delenv("AGENTLENS_ENCRYPTION_KEY", raising=False)
    import main
    main.db._forget()
    main.faults.reset()
    return main


def by_step(events):
    return {e["step_name"]: e for e in events}  # last event per step


def identity_of(pipeline, case_id):
    return pipeline.db.get_identity(pipeline.db.get_application(case_id)["customer_token"])


def pii_hits(pipeline, event, case_id):
    blob = json.dumps(event["input_data"]) + json.dumps(event["output_data"])
    return pipeline.tools.find_pii(blob, identity_of(pipeline, case_id))


def fake_chat_json(pipeline, monkeypatch, **answers):
    """Answer only the named chat_json schemas; everything else falls back to rules."""
    def fake(system, user, schema, name, max_tokens=200):
        if name in answers:
            a = answers[name]
            return (a(user) if callable(a) else a), {"cached": False}
        raise pipeline.llm.LLMError("offline")
    monkeypatch.setattr(pipeline.llm, "chat_json", fake)


def decision(**kw):
    return {"reasoning": "r", "confidence": 90, "borderline": False, "customer_reason": "c", **kw}


# --- the story -----------------------------------------------------------

def test_steps_in_order(pipeline):
    for cid in ("LOAN-0023", "LOAN-0024"):
        events = pipeline.run_case(cid)["events"]
        assert [(e["agent_name"], e["step_name"]) for e in events] == STEP_ORDER
        assert all(e["result"] in ("pass", "fail", "flag") for e in events)
        assert all(e["timestamp"] and e["notes"] for e in events)


def test_every_tool_step_is_a_real_tool(pipeline):
    tool_steps = {s for a, s in STEP_ORDER if a != "orchestrator" and not s.startswith("ai.") and s != "verify_income"}
    assert tool_steps <= set(pipeline.tools.TOOLS)
    assert set(pipeline.tools.TOOLS) <= set(pipeline.tools.TOOL_INFO)  # every tool has a real-service mapping


def test_demo_case_is_wrongly_approved(pipeline):
    out = pipeline.run_case("LOAN-0023")
    inc = by_step(out["events"])["verify_income"]
    assert out["decision"] == "approved" and out["status"] == "approved"
    assert inc["output_data"]["verified_annual_income"] == pytest.approx(144000, rel=0.001)
    assert inc["input_data"]["payslip_pay_frequency"] == "fortnightly"
    assert inc["output_data"]["periods_per_year_used"] == 52
    assert inc["result"] == "pass"  # the fault is silent


def test_control_case_is_declined(pipeline):
    out = pipeline.run_case("LOAN-0024")
    ev = by_step(out["events"])
    assert out["decision"] == "declined" and out["status"] == "declined"
    assert ev["verify_income"]["output_data"]["verified_annual_income"] == pytest.approx(72000, rel=0.001)
    assert ev["calc.serviceability"]["result"] == "fail"
    assert pipeline.db.get_application("LOAN-0024")["decision"] == "declined"


def test_serviceability_reads_income_event(pipeline):
    ev = by_step(pipeline.run_case("LOAN-0023")["events"])
    inp = ev["calc.serviceability"]["input_data"]
    assert inp["upstream_event_ids"]["verify_income"] == ev["verify_income"]["event_id"]
    assert inp["monthly_income"] == ev["verify_income"]["output_data"]["verified_monthly_income"]


# --- least privilege -------------------------------------------------------

AGENT_FUNCS = ["intake_agent", "verification_agent", "decision_agent", "notification_service"]


def test_agents_never_touch_the_database(pipeline):
    for name in AGENT_FUNCS + ["intake_with_llm"]:
        src = inspect.getsource(getattr(pipeline, name))
        assert not re.search(r"\bdb\.", src), f"{name} calls the database directly"
    for name in AGENT_FUNCS:
        assert list(inspect.signature(getattr(pipeline, name)).parameters) == ["run", "ticket"]


def test_each_agent_gets_its_own_ticket(pipeline):
    events = pipeline.run_case("LOAN-0023")["events"]
    tickets = [e for e in events if e["step_name"] == "access.issue_ticket"]
    assert [t["input_data"]["agent"] for t in tickets] == AGENT_FUNCS
    assert len({t["output_data"]["ticket_id"] for t in tickets}) == 4
    token = pipeline.db.get_application("LOAN-0023")["customer_token"]
    assert all(t["input_data"]["customer_token"] == token for t in tickets)


def test_cross_case_access_is_refused_and_logged(pipeline):
    events = pipeline.run_case("LOAN-0027")["events"]
    v = [e for e in events if e["step_name"] == "access.violation"]
    assert len(v) == 1
    assert v[0]["agent_name"] == "verification_agent" and v[0]["result"] == "fail"
    assert v[0]["input_data"]["requested_customer_token"] == "CUST-9B21"
    assert v[0]["output_data"] == {"refused": True, "rule": "DP4", "severity": "critical"}
    # it never got the other report; its own pull still worked
    assert by_step(events)["credit.pull_report"]["result"] == "pass"


def test_forged_ticket_is_refused(pipeline):
    pipeline.submissions.submit_test_case("LOAN-0023")
    token = pipeline.db.get_application("LOAN-0023")["customer_token"]
    forged = pipeline.Ticket("deadbeef", "LOAN-0023", token, "verification_agent")
    with pytest.raises(pipeline.AccessViolation, match="no valid ticket"):
        pipeline.tools.credit_pull_report(forged)
    assert by_step(pipeline.db.get_events("LOAN-0023"))["access.violation"]["result"] == "fail"


def _ticket_at(pipeline, case_id, agent, status):
    pipeline.submissions.submit_test_case(case_id)
    pipeline.db.update_application(case_id, {"status": status})
    return pipeline.access.issue_ticket(case_id, agent)


def test_field_scope_decision_agent_cannot_read_documents(pipeline):
    t = _ticket_at(pipeline, "LOAN-0023", "decision_agent", "deciding")
    with pytest.raises(pipeline.AccessViolation, match="may not read 'documents'"):
        pipeline.tools.docs_extract_payslip(t)
    with pytest.raises(pipeline.AccessViolation, match="may not read 'application'"):
        pipeline.tools.crm_get_application(t)


def test_field_scope_notification_service_cannot_read_finances(pipeline):
    t = _ticket_at(pipeline, "LOAN-0023", "notification_service", "approved")
    with pytest.raises(pipeline.AccessViolation):
        pipeline.tools.credit_pull_report(t)
    with pytest.raises(pipeline.AccessViolation):
        pipeline.tools.crm_get_application(t)


def test_stage_gate_decision_agent_cannot_act_while_verifying(pipeline):
    t = _ticket_at(pipeline, "LOAN-0023", "decision_agent", "verifying")
    with pytest.raises(pipeline.AccessViolation, match="outside its stage"):
        pipeline.tools.case_set_status(t, "approved")
    assert pipeline.db.get_application("LOAN-0023")["status"] == "verifying"


def test_agent_can_only_move_to_its_next_status(pipeline):
    t = _ticket_at(pipeline, "LOAN-0023", "intake_agent", "submitted")
    with pytest.raises(pipeline.AccessViolation, match="tried to set status 'approved'"):
        pipeline.tools.case_set_status(t, "approved")


def test_held_case_only_a_human_can_change(pipeline):
    pipeline.run_case("LOAN-0023")
    pipeline.access.place_hold("LOAN-0023", "DP4 test")
    with pytest.raises(pipeline.AccessViolation, match="held"):
        pipeline.process_case("LOAN-0023")
    t = pipeline.access.issue_ticket("LOAN-0023", "decision_agent")
    with pytest.raises(pipeline.AccessViolation, match="held case"):
        pipeline.tools.case_set_status(t, "approved")
    pipeline.access.human_review("LOAN-0023", "Sam (credit officer)", "declined", "income misread")
    assert pipeline.db.get_application("LOAN-0023")["status"] == "declined"


def test_a_violation_that_isnt_handled_holds_the_case(pipeline, monkeypatch):
    def rogue(run, ticket):  # a decision agent that tries to read the raw payslip
        run.tool(ticket, "docs.extract_payslip", lambda o: ("pass", "x"))
    monkeypatch.setattr(pipeline, "PIPELINE", [(n, rogue if n == "decision_agent" else f)
                                                for n, f in pipeline.PIPELINE])
    out = pipeline.run_case("LOAN-0024")
    assert out["status"] == "held"
    assert by_step(out["events"])["case.hold"]["agent_name"] == "access_layer"


# --- privacy: no AI sees identity ----------------------------------------

@pytest.mark.parametrize("cid", ["LOAN-0023", "LOAN-0024", "LOAN-0025", "LOAN-0027"])
def test_no_pii_anywhere_in_the_event_log(pipeline, cid):
    events = pipeline.run_case(cid)["events"]
    assert [e["step_name"] for e in events if pii_hits(pipeline, e, cid)] == []


def test_ai_steps_only_see_redacted_text(pipeline):
    ev = by_step(pipeline.run_case("LOAN-0023")["events"])
    sent = ev["ai.read_documents"]["input_data"]["text_sent_to_llm"]
    assert "[NAME]" in sent and "[ADDRESS]" in sent and "[ACCOUNT]" in sent and "[EMPLOYER]" in sent
    assert "Gross pay: $2,769.23" in sent and "Pay frequency: Fortnightly" in sent
    assert "CUSTOMER MESSAGE:\nHi, I'm [NAME], I'm 27" in ev["ai.parse_request"]["input_data"]["text_sent_to_llm"][0]
    assert ev["docs.extract_payslip"]["output_data"]["redacted"] is True


def test_identity_and_documents_are_encrypted_at_rest(pipeline, tmp_path):
    pipeline.run_case("LOAN-0023")
    raw = (tmp_path / "test.db").read_bytes()
    for s in (b"John Davies", b"Ridge Street", b"1999-05-14", b"032-000 48 1234", b"Harbourside"):
        assert s not in raw
    assert pipeline.tools.find_pii(pipeline.db.get_document_text("LOAN-0023", "payslip"),
                                   identity_of(pipeline, "LOAN-0023"))  # decrypts fine


def test_planted_privacy_failure_unredacted_payslip(pipeline):
    events = pipeline.run_case("LOAN-0026")["events"]
    flagged = [e["step_name"] for e in events if pii_hits(pipeline, e, "LOAN-0026")]
    assert flagged == ["ai.read_documents"]  # exactly where rule DP3 should fire
    assert by_step(events)["docs.extract_payslip"]["output_data"]["redacted"] is False
    sent = by_step(events)["ai.read_documents"]["input_data"]["text_sent_to_llm"]
    assert "Liam Nguyen" in sent and "27 Wattle Avenue" in sent


def test_redact_mask_patterns(pipeline):
    t = ("Hi, my name is Sam Lee, email sam@example.com, phone 0412 345 678, TFN 123 456 782, "
         "card 4111 1111 1111 1111, BSB 062-000 12345678, DOB: 01/02/1990, I live at 9 Gum Tree Road Ashgrove.")
    out = pipeline.tools.redact_mask(t)
    for raw in ("Sam Lee", "sam@example.com", "0412 345 678", "123 456 782", "4111 1111 1111 1111",
                "062-000", "01/02/1990", "9 Gum Tree Road"):
        assert raw not in out["text"], raw
    assert pipeline.tools.find_pii(out["text"]) == []


def test_match_identity_is_yes_no_only(pipeline):
    t = _ticket_at(pipeline, "LOAN-0023", "verification_agent", "verifying")
    assert pipeline.tools.docs_match_identity(t) == {"document": "payslip", "name_match": True,
                                                     "name_found_on_document": True}


def test_customer_reply_is_redacted_before_the_ai(pipeline, monkeypatch):
    seen = []
    def fake_chat(messages, tools=None, tool_choice=None, **kw):
        if not tools:
            raise pipeline.llm.LLMError("offline")
        seen.append(messages)
        if len(seen) == 1:
            return {"content": None, "tool_calls": [{"id": "c1", "name": "ask_customer", "arguments_raw": "{}",
                    "arguments": {"question": "How many years?", "missing_fields": ["term_years"]}}]}, {"cached": False}
        return {"content": None, "tool_calls": [{"id": "c2", "name": "submit_application", "arguments_raw": "{}",
                "arguments": {"loan_amount": 12000, "term_years": 3, "purpose": "car", "sensitive_info": []}}]}, \
            {"cached": False}
    monkeypatch.setattr(pipeline.llm, "chat", fake_chat)
    out = pipeline.run_case("LOAN-0025", customer_replies=["It's Mia Chen from 4 Banksia Court, 3 years"])
    tool_reply = json.loads(seen[1][-1]["content"])["customer_reply"]
    assert "Mia" not in tool_reply and "Banksia" not in tool_reply and "3 years" in tool_reply
    assert "chat.ask_customer" in [e["step_name"] for e in out["events"]]


def test_name_only_added_by_the_notification_service(pipeline):
    out = pipeline.run_case("LOAN-0023")
    assert out["customer_message"].startswith("Good news, John!")
    note = by_step(out["events"])["notify.send_template"]
    assert note["agent_name"] == "notification_service" and "[NAME]" in note["output_data"]["message_preview"]
    assert pipeline.db.get_tracker("LOAN-0023")["message"] == out["customer_message"]


def test_decision_ai_gets_no_identity_or_income(pipeline, monkeypatch):
    seen = {}
    def decide(user):
        seen["user"] = user
        return decision(decision="approved", confidence=95)
    fake_chat_json(pipeline, monkeypatch, loan_decision=decide)
    pipeline.run_case("LOAN-0023")
    for s in ("John", "Davies", "Ridge", "144000", "72000", "CUST-"):
        assert s not in seen["user"]
    assert json.loads(seen["user"])["key_figures"]["dti_points_from_limit"] == pytest.approx(-24.0)


# --- verification agent reads text ---------------------------------------

def test_fault_switch_fires_even_when_ai_reads_correctly(pipeline, monkeypatch):
    real_read = pipeline.read_documents_with_rules
    def perfect_read(user):
        payslip, rest = user.split("\n\nBANK STATEMENT:\n")
        statement = rest.split("\n\nCREDIT REPORT")[0]
        return real_read(payslip, statement, pipeline.TEST_CASES["LOAN-0023"]["credit_report"])
    fake_chat_json(pipeline, monkeypatch, document_read=perfect_read)
    ev = by_step(pipeline.run_case("LOAN-0023")["events"])
    assert ev["ai.read_documents"]["output_data"]["payslip"]["pay_frequency"] == "fortnightly"
    assert ev["verify_income"]["output_data"]["verified_annual_income"] == pytest.approx(144000, rel=0.001)


def test_reader_living_expenses_exclude_income_and_debt(pipeline):
    out = by_step(pipeline.run_case("LOAN-0024")["events"])["ai.read_documents"]["output_data"]
    assert out["monthly_living_expenses"] == pytest.approx(2400)
    assert out["monthly_debt_repayments"] == pytest.approx(250)


def test_reader_guards_against_bad_ai_reads(pipeline, monkeypatch):
    bad = {"payslip": {"pay_frequency": "fortnightly", "gross_pay": 2769.23, "pay_period_start": None,
                       "pay_period_end": None, "hecs_help_withheld": True},
           "statement_items": [{"description": "Rent", "amount": -1600, "category": "housing"}],
           "liabilities": [], "liabilities_summary": "none"}
    fake_chat_json(pipeline, monkeypatch, document_read=bad)
    read = by_step(pipeline.run_case("LOAN-0024")["events"])["ai.read_documents"]
    assert read["output_data"]["monthly_living_expenses"] == 1600  # sign ignored
    assert read["result"] == "flag" and "statement rows" in read["notes"]


# --- intake agent ----------------------------------------------------------

def test_intake_question_limit_forces_submit(pipeline, monkeypatch):
    choices = []
    def fake_chat(messages, tools=None, tool_choice=None, **kw):
        if not tools:
            raise pipeline.llm.LLMError("offline")
        choices.append(tool_choice)
        if tool_choice == pipeline.SUBMIT_ONLY:
            return {"content": None, "tool_calls": [{"id": "s", "name": "submit_application", "arguments_raw": "{}",
                    "arguments": {"loan_amount": 12000, "term_years": None, "purpose": "car",
                                  "sensitive_info": []}}]}, {"cached": False}
        return {"content": None, "tool_calls": [{"id": f"a{len(choices)}", "name": "ask_customer", "arguments_raw": "{}",
                "arguments": {"question": "Term?", "missing_fields": ["term_years"]}}]}, {"cached": False}
    monkeypatch.setattr(pipeline.llm, "chat", fake_chat)
    out = pipeline.run_case("LOAN-0025", customer_replies=[])
    assert len(choices) == pipeline.MAX_CUSTOMER_QUESTIONS + 1
    assert by_step(out["events"])["ai.parse_request"]["result"] == "flag"  # term still missing


def test_detail_on_the_form_is_not_missing(pipeline):
    out = pipeline.run_case("LOAN-0024", customer_message="Hi, I'm John. I need $15,000 please.")
    ev = by_step(out["events"])["ai.parse_request"]
    assert ev["result"] == "pass"  # term + purpose come from the form
    assert ev["output_data"]["sources"] == {"loan_amount": "customer", "term_years": "application_form",
                                            "purpose": "application_form"}


def test_detail_missing_from_form_and_message_flags(pipeline):
    out = pipeline.run_case("LOAN-0025", customer_replies=[])  # Mia left the term blank and doesn't reply
    ev = by_step(out["events"])
    assert ev["ai.parse_request"]["result"] == "flag" and "missing field: term_years" in ev["ai.parse_request"]["notes"]
    assert out["status"] == "referred"  # a person sorts it out


def test_sensitive_info_goes_to_a_person(pipeline):
    msg = "I'm John, 27. I lost my job and I'm behind on rent. Can I get $15,000 over 5 years for a car?"
    out = pipeline.run_case("LOAN-0024", customer_message=msg)
    ev = by_step(out["events"])
    assert ev["ai.parse_request"]["output_data"]["sensitive_info"][0]["category"] == "financial_hardship"
    assert out["decision"] == "refer_to_human" and out["status"] == "referred"


def test_id_number_in_message_is_masked_and_flagged(pipeline):
    msg = "I'm John, my TFN is 123 456 782. Can I get $15,000 over 5 years for a car?"
    ev = by_step(pipeline.run_case("LOAN-0024", customer_message=msg)["events"])
    assert "123 456 782" not in json.dumps(ev["ai.parse_request"]["input_data"])
    assert "identity_number" in [s["category"] for s in ev["ai.parse_request"]["output_data"]["sensitive_info"]]


# --- eligibility via identity.verify -------------------------------------

def test_identity_verify_returns_facts_not_values(pipeline):
    out = by_step(pipeline.run_case("LOAN-0023")["events"])["identity.verify"]["output_data"]
    assert out["identity_verified"] is True and out["age_over_18"] is True
    assert "1999" not in json.dumps(out) and "Davies" not in json.dumps(out)


def test_under_18_fails(pipeline):
    profile = dict(pipeline.TEST_CASES["LOAN-0024"]["profile"], name="Sam Young",
                   date_of_birth="2010-01-01", gross_income=150000)
    out = pipeline.run_case("LOAN-0099", customer_message="I'm Sam Young. I want $5,000 over 2 years for a car.",
                            profile=profile)
    assert by_step(out["events"])["rules.eligibility"]["result"] == "fail"
    assert out["decision"] == "declined"


# --- decision agent: guardrails ------------------------------------------

def test_guardrail_blocks_ai_approving_a_failed_case(pipeline, monkeypatch):
    fake_chat_json(pipeline, monkeypatch, loan_decision=decision(decision="approved"))
    out = pipeline.run_case("LOAN-0024")
    assert out["decision"] == "declined" and by_step(out["events"])["ai.decide"]["result"] == "flag"


def test_ai_may_refer_a_borderline_decline(pipeline, monkeypatch):
    fake_chat_json(pipeline, monkeypatch, loan_decision=decision(decision="refer_to_human", borderline=True))
    out = pipeline.run_case("LOAN-0024")
    ai = by_step(out["events"])["ai.decide"]
    assert out["decision"] == "refer_to_human" and ai["result"] == "pass" and "borderline" in ai["notes"]


def test_low_confidence_approval_goes_to_a_person(pipeline, monkeypatch):
    fake_chat_json(pipeline, monkeypatch, loan_decision=decision(decision="approved", confidence=55))
    out = pipeline.run_case("LOAN-0023")
    assert out["decision"] == "refer_to_human" and "low confidence" in by_step(out["events"])["ai.decide"]["notes"]


# --- the loan form flow ------------------------------------------------------

FORM = {"name": "Noah Brown", "date_of_birth": "1990-06-01", "address": "8 River Road, Milton QLD 4064",
        "account_number": "032-000 11 2233", "employer": "Acme Pty Ltd", "employment": "full-time",
        "gross_income": 90000, "pay_frequency": "monthly", "requested": 9000, "term_years": 3, "purpose": "car",
        "monthly_expenses": 2000, "credit_card_limit": 4000, "has_hecs": False, "existing_customer": True,
        "residency": "AU", "household": "single", "dependants": 0}


def test_form_submission_then_process_next(pipeline):
    cid = pipeline.submissions.submit_application(dict(FORM), "Hi, I'm Noah Brown. $9,000 over 3 years for a car.")
    assert cid == "LOAN-1001" and pipeline.db.get_application(cid)["status"] == "submitted"
    assert pipeline.submissions.next_case_id() == "LOAN-1002"
    out = pipeline.process_next()
    assert out["case_id"] == cid and out["status"] == "approved"
    assert by_step(out["events"])["ai.read_documents"]["output_data"]["payslip"]["pay_frequency"] == "monthly"
    assert [e["step_name"] for e in out["events"] if pii_hits(pipeline, e, cid)] == []
    assert pipeline.process_next() is None  # nothing left waiting


# --- Supabase backend, against a fake PostgREST server ------------------------

class FakePostgREST:
    """Just enough of Supabase's REST API for supabase_db.py."""

    CHILDREN = ["customer_identity", "documents", "mock_external_data", "handoffs"]
    IDENTITY = {"mistake_log", "licence_history", "improvement_suggestion"}

    def __init__(self):
        import supabase_db
        self.tables = {t: [] for t in supabase_db.TABLES}
        self.next_event_id = 1
        self.next_id = 1
        self.auth_headers = []

    @staticmethod
    def _ok(value, op):
        sv = "true" if value is True else "false" if value is False else str(value)
        if op.startswith("eq."):
            return sv == op[3:]
        if op.startswith("neq."):
            return sv != op[4:]
        if op.startswith("gt."):
            return value is not None and float(value) > float(op[3:])
        if op.startswith("in.("):
            return sv in [x.strip('"') for x in op[4:-1].split(",")]
        if op == "not.is.null":
            return value is not None
        raise AssertionError(f"filter {op} not supported by the fake")

    FILTER_OPS = ("eq.", "neq.", "gt.", "in.(", "not.is.null")

    def _match(self, row, filters):
        return all(self._ok(row.get(k), v) for k, v in filters.items())

    def __call__(self, request):
        import httpx
        self.auth_headers.append(request.headers.get("apikey"))
        path = request.url.path.split("/rest/v1/", 1)[1]
        if path == "rpc/reserve_event_ids":
            n = json.loads(request.content)["n"]
            ids = list(range(self.next_event_id, self.next_event_id + n))
            self.next_event_id += n
            return httpx.Response(200, json=ids)
        table = path
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        filters = {k: v for k, v in q.items() if k not in ("select", "order", "limit", "on_conflict")
                   and v.startswith(self.FILTER_OPS)}
        rows = self.tables[table]
        if request.method == "GET":
            out = [r for r in rows if self._match(r, filters)]
            if "order" in q:
                for part in reversed(q["order"].split(",")):
                    col, _, direction = part.partition(".")
                    out.sort(key=lambda r: (r.get(col) is None, str(r.get(col)).zfill(12)),
                             reverse=direction == "desc")
            if "limit" in q:
                out = out[: int(q["limit"])]
            if q.get("select", "*") != "*":
                cols = q["select"].split(",")
                out = [{c: r.get(c) for c in cols} for r in out]
            return httpx.Response(200, json=out)
        if request.method == "DELETE":
            gone = [r for r in rows if self._match(r, filters)]
            self.tables[table] = [r for r in rows if not self._match(r, filters)]
            if table == "applications":  # ON DELETE CASCADE
                for child in self.CHILDREN:
                    ids = {g["case_id"] for g in gone}
                    self.tables[child] = [r for r in self.tables[child] if r["case_id"] not in ids]
            return httpx.Response(204)
        if request.method == "PATCH":
            for r in rows:
                if self._match(r, filters):
                    r.update(json.loads(request.content))
            return httpx.Response(204)
        if request.method == "POST":
            body = json.loads(request.content)
            new = body if isinstance(body, list) else [body]
            keys = q.get("on_conflict", "").split(",") if "on_conflict" in q else None
            for n in new:
                if table in self.IDENTITY:
                    n = {"id": self.next_id, **n}
                    self.next_id += 1
                if table == "applications":
                    n = {"status": "submitted", "submitted_at": f"2026-11-16T00:00:{len(rows):02d}Z", **n}
                existing = next((r for r in rows if keys and all(r.get(k) == n.get(k) for k in keys)), None)
                if existing is not None and "merge-duplicates" in request.headers.get("prefer", ""):
                    existing.update(n)
                else:
                    rows.append(n)
                last = n
            if "return=representation" in request.headers.get("prefer", ""):
                return httpx.Response(201, json=[last])
            return httpx.Response(201)
        return httpx.Response(405)


def test_full_pipeline_on_supabase_backend(pipeline, monkeypatch):
    import httpx
    from cryptography.fernet import Fernet

    import supabase_db

    fake = FakePostgREST()
    monkeypatch.setenv("AGENTLENS_BACKEND", "supabase")
    monkeypatch.setenv("AGENTLENS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(supabase_db, "_client", httpx.Client(
        transport=httpx.MockTransport(fake), base_url="https://fake.supabase.co/rest/v1",
        headers={"apikey": "sb_secret_test"}))
    pipeline.db._identity_cache.clear()

    assert pipeline.db.check() == []
    out = pipeline.run_case("LOAN-0023")
    assert out["status"] == "approved" and len(out["events"]) == len(STEP_ORDER)
    assert [e["event_id"] for e in out["events"]] == sorted(e["event_id"] for e in out["events"])
    stored = json.dumps(fake.tables["customer_identity"]) + json.dumps(fake.tables["documents"])
    assert "John" not in stored and "Ridge" not in stored  # only ciphertext reaches Supabase
    assert fake.tables["applications"][0]["decision"] == "approved"
    assert {h["stage"] for h in fake.tables["handoffs"]} == {"intake", "verification", "decision"}
    assert set(fake.auth_headers) == {"sb_secret_test"}


def test_supabase_mode_refuses_without_an_encryption_key(pipeline, monkeypatch):
    monkeypatch.setenv("AGENTLENS_BACKEND", "supabase")
    monkeypatch.delenv("AGENTLENS_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AGENTLENS_ENCRYPTION_KEY"):
        pipeline.db.encrypt("x")


# --- misc ----------------------------------------------------------------

def test_rerun_replaces_previous_run(pipeline):
    pipeline.run_case("LOAN-0023")
    pipeline.run_case("LOAN-0023")
    assert len(pipeline.db.get_events("LOAN-0023")) == len(STEP_ORDER)


def test_rule_parser_reads_redacted_message(pipeline):
    assert pipeline.parse_with_rules("Hi, I'm [NAME], I'm 27. I'd like to borrow $15,000 over 5 years "
                                     "to consolidate my credit card debt.") == {
        "loan_amount": 15000.0, "term_years": 5, "purpose": "debt consolidation", "sensitive_info": [],
    }


def test_api_endpoints(pipeline):
    from fastapi.testclient import TestClient

    try:
        test_client = TestClient(pipeline.app)
    except TypeError as e:  # old starlette + httpx>=0.28
        pytest.skip(f"TestClient incompatible with installed httpx ({e}); pip install -U fastapi")

    with test_client as client:
        assert client.get("/form").status_code == 200
        r = client.post("/applications?process=true", json={**FORM, "customer_message": "I'm Noah. $9,000 over 3 years for a car."})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        assert client.post("/submit_case", json={"case_id": "LOAN-0023"}).json()["decision"] == "approved"
        assert len(client.get("/event_log/LOAN-0023").json()) == len(STEP_ORDER)
        assert client.get("/tracker/LOAN-0023").json()["status"] == "approved"
        assert "Equifax" in client.get("/tools").json()["tools"]["credit.pull_report"]["display_name"]
        assert "documents" not in client.get("/permissions").json()["decision_agent"]["read"]
        assert client.get("/event_log/LOAN-9999").status_code == 404
        assert client.post("/process/LOAN-0500").status_code == 400
