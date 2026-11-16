# Mock loan pipeline (the "Westpac agents")

A stand-in for the bank's own AI agents. Nine agents process a personal loan
one after another, and **every agent call writes one row to a shared
`event_log` table**. AgentLens reads that table to audit the agents.

Only `intake_agent` calls a real LLM (Claude, to parse the customer's chat
message). Every other agent is plain if/else, so the demo gives the same
result every time.

> Synthetic data only. No real customers.

## The 9 agents

| # | Agent | What it does | Result rule |
|---|---|---|---|
| 1 | `intake_agent` | Parses the chat message into name, age, loan amount, term and purpose (**Claude**) | missing field → `flag` |
| 2 | `identity_eligibility_agent` | Age 18+, existing customer, AU resident | under 18 → `fail` |
| 3 | `income_verification_agent` | Annualises the payslip and compares it with stated income | >10% apart → `flag` · **planted bug on LOAN-0023** |
| 4 | `expense_liability_agent` | Living expenses from the bank statement + 3.8% of card limit + HECS-HELP | commitments > income → `flag` |
| 5 | `credit_bureau_agent` | Reads the mock credit report | default → `fail`, >3 enquiries → `flag` |
| 6 | `serviceability_agent` | DTI = (commitments + new repayment) / monthly income, **using agent 3's income** | > 50% → `fail` |
| 7 | `policy_rules_agent` | Personal loans ≤ $30k; debt consolidation needs existing debts | any breach → `fail` |
| 8 | `decision_agent` | Any fail → `declined`, any flag → `refer_to_human`, else `approved` | — |
| 9 | `notification_agent` | Writes the customer message and updates `customer_tracker` | — |

The orchestrator (`run_case`) always runs all 9, even after a fail, so the
decision agent sees the full picture.

## Run it

```bash
pip install -r ../requirements.txt     # needs fastapi>=0.115 and anthropic>=0.75
cd agents

python main.py                         # runs LOAN-0023 and LOAN-0024, prints the log
uvicorn main:app --reload              # API on http://127.0.0.1:8000 (docs at /docs)
```

The intake agent uses Claude if `ANTHROPIC_API_KEY` is set (in the repo's
`.env`). If Claude can't be reached, or `AGENTLENS_OFFLINE=1` is set, it falls
back to a rule-based parser. `output_data.parser` records which was used.
**Set `AGENTLENS_OFFLINE=1` while filming** so the demo can't break on Wi-Fi.

The database is `agents/agentlens.db`. Point `AGENTLENS_DB` at another file to
keep runs separate. Re-submitting a case replaces that case's previous run.

## Submit a test case

PowerShell:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/submit_case `
  -ContentType "application/json" -Body '{"case_id": "LOAN-0023"}'
Invoke-RestMethod http://127.0.0.1:8000/event_log/LOAN-0023
Invoke-RestMethod http://127.0.0.1:8000/tracker/LOAN-0023
```

bash / curl:

```bash
curl -X POST http://127.0.0.1:8000/submit_case \
  -H "Content-Type: application/json" -d '{"case_id": "LOAN-0023"}'
```

For a test case, `customer_message` and `profile_json` are optional (the
hardcoded ones are used). For any other `case_id`, send both:

```json
{
  "case_id": "LOAN-0100",
  "customer_message": "Hi, I'm Mia, 31. Can I borrow $12,000 over 3 years for a car?",
  "profile_json": {"name": "Mia", "age": 31, "gross_income": 85000, "requested": 12000,
                   "term_years": 3, "purpose": "car", "monthly_expenses": 2100,
                   "credit_card_limit": 5000, "has_hecs": false, "defaults": 0,
                   "enquiries": 1, "existing_customer": true, "residency": "AU"}
}
```

| Endpoint | Returns |
|---|---|
| `POST /submit_case` | decision, summary, customer message, and the case's events |
| `GET /event_log/{case_id}` | that case's events, in order |
| `GET /event_log` | the whole log (what AgentLens consumes) |
| `GET /tracker/{case_id}` | customer-facing status |

## ⚠️ LOAN-0023 is designed to be wrongly approved

LOAN-0023 and LOAN-0024 are the **same customer with the same documents**.
John earns $72,000 and is paid **fortnightly** ($2,769.23 per payslip).

For LOAN-0023 only, `income_verification_agent` has a planted parsing bug:
when it builds the figure it hands downstream, it reads the payslip as
**weekly**. So it reports $144,000. Its own mismatch check uses the correct
figure, so it still says `pass`. `serviceability_agent` trusts that number:

| | Income used | DTI | Decision |
|---|---|---|---|
| LOAN-0024 (control) | $6,000/month | 52.1% | **declined** ✔ correct |
| LOAN-0023 (demo) | $12,000/month | 26.0% | **approved** ✘ wrong |

That's the bug AgentLens is supposed to catch. The evidence is all in the
event log:

- Agent 3's `output_data` shows `payslip_pay_frequency: "fortnightly"`, but
  `periods_per_year_used: 52` and `verified_annual_income: 144000`, against
  `stated_annual_income: 72000`. That's a `pass` that contradicts its own numbers.
- Agent 6's `input_data.upstream_event_ids.income_verification_agent` points to
  the exact event it read its income from, so the bad approval traces back to agent 3.

The bug lives in `PLANTED_BUG_CASES` in `main.py`.

## Event log schema (`event_log` table)

| Column | Type | Example |
|---|---|---|
| `event_id` | int, autoincrement | 3 |
| `case_id` | text | `LOAN-0023` |
| `agent_name` | text | `income_verification_agent` |
| `step_name` | text | `verify_income` |
| `timestamp` | text, ISO 8601 | `2026-11-16T12:26:04.376+10:00` |
| `input_data` | JSON text | what the agent was given, incl. `upstream_event_ids` |
| `output_data` | JSON text | what it produced |
| `result` | text | `pass` / `fail` / `flag` |
| `notes` | text | short plain-English reason |
