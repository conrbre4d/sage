# Mock loan pipeline (the "bank's agents")

A customer fills in a **loan form**. It's stored in **Supabase** with their identity
**encrypted**, and three narrow AI agents take it through approval. **Agents never touch
the database**: each one gets a ticket for one case and can only call tools. Every
tool call writes a row to the `event_log` table, which AgentLens audits.

> Synthetic data only. No real customers.

## Set up Supabase (free tier, about 5 minutes)

1. **Create a project** at <https://supabase.com/dashboard>: *New project*. Pick region
   **Sydney (ap-southeast-2)** and set a database password (you won't need it here).
2. **Create the tables.** Go to *SQL Editor → New query*, paste all of
   [`../supabase/schema.sql`](../supabase/schema.sql), and click **Run**. You should see
   7 tables under *Table Editor*.
3. **Copy two values** into the repo's `.env`:
   - `SUPABASE_URL`: *Project Settings → Data API → Project URL* (`https://xxxx.supabase.co`)
   - `SUPABASE_SECRET_KEY`: *Project Settings → API Keys → Secret key* (`sb_secret_…`).
     On older projects, use the **service_role** key instead.
4. `AGENTLENS_ENCRYPTION_KEY` is already generated in your `.env`. **Share it only with
   teammates who process the same data**, using the same channel as other secrets. Without
   it the encrypted rows can't be read.

**Least privilege in Supabase:** Row Level Security is **on** for every table, with **no
policies**. The public (publishable/anon) key can read or write **nothing**. Only this
server, holding the secret key, can reach the data. Don't put the secret key in a browser
or commit it. `.env` is in `.gitignore`.

Free-tier notes: 500 MB of database (plenty), and the project **pauses after a week of no
activity**. Open the dashboard and click *Restore* before demo day.

## Run and test it

From the repo root:

```bash
pip install -r requirements.txt
cd agents

python main.py check          # [ok] storage, encryption key and AI key all OK
python main.py                # submit + process the 5 demo cases, print each event log
uvicorn main:app --reload     # API + loan form at http://127.0.0.1:8000/form
```

Then:

- Open **<http://127.0.0.1:8000/form>** and click *Submit and process now*. The form is
  pre-filled with Mia, who leaves the term blank, so the AI asks and she replies.
- In Supabase *Table Editor*, look at `customer_identity` and `documents`: you see
  only ciphertext (`gAAAAAB…`). In `event_log` you see every step, ticket and refusal.

Other commands:

| Command | Does |
|---|---|
| `python main.py seed` | submit the demo cases without processing (status `submitted`) |
| `python main.py process [LOAN-…]` | process given cases (default: everything `submitted`) |
| `python main.py next` | process the oldest submitted application |
| `python -m pytest` (repo root) | 65 tests, offline, no Supabase or AI calls |

**No internet / filming:** set `AGENTLENS_BACKEND=local` (SQLite file) and/or
`AGENTLENS_OFFLINE=1` (cached AI answers only).

## The flow

```
loan form ──► POST /applications ──► Supabase: applications (facts)
                                              customer_identity (ENCRYPTED: name, DOB, address, account, employer, message)
                                              documents (ENCRYPTED: payslip + statement text)
          status: submitted
orchestrator issues a ticket ─► intake_agent        ─► status: verifying
orchestrator issues a ticket ─► verification_agent  ─► status: deciding
orchestrator issues a ticket ─► decision_agent      ─► status: approved / declined / referred
orchestrator issues a ticket ─► notification_service (code): "Hi Mia, …"
```

| Agent | Real AI work | Tool calling |
|---|---|---|
| `intake_agent` | Reads the form's loan details and the **redacted** message, pulls out amount/term/purpose, spots missing, contradictory or sensitive info | ✅ The AI decides when to call `chat.ask_customer` (max 2), then `submit_application` |
| `verification_agent` | Reads the **redacted** payslip and statement text, extracts figures, summarises liabilities. Code does the sums | Scripted |
| `decision_agent` | Weighs the verification handoff, handles borderline cases, gives reasoning and confidence (guardrailed) | Scripted |
| `notification_service` | None: plain code. Inserts the first name and sends the message | n/a |

## Least privilege ([`access.py`](access.py))

1. **No database access for agents.** Agents get `(run, ticket)` and nothing else. Only
   tools call `db.py`, and a test checks the agents' code never does.
2. **Case-scoped.** The orchestrator issues a `Ticket(case, token, agent)` per agent per
   run, then revokes it. Asking for another token is refused. A forged ticket is refused.
3. **Field-scoped.** Each agent has an allowlist:

   | Agent | Can read | Can write | Can never see |
   |---|---|---|---|
   | intake | form facts, redacted message, customer chat | intake handoff, status → verifying | vault, documents, credit file |
   | verification | form facts, intake handoff, redacted documents, credit report, identity yes/no, HEM, rules | verification handoff, status → deciding | vault, other cases, decisions |
   | decision | verification handoff only | decision handoff, status → approved/declined/referred | vault, raw documents, other cases |
   | notification service | decision handoff, status, contact details | sent message | financial figures |
   | AgentLens | event log, handoffs, statuses | flags, holds (`POST /cases/{id}/hold`), trust scores | vault |

4. **Status-gated.** An agent can only act while the case is at its stage, and can only
   move it to the next one. A **held** case can only be changed by a human reviewer
   (`POST /cases/{id}/review`).

A refused call raises `AccessViolation` and is logged as step **`access.violation`**
(`output_data.rule = "DP4"`, `severity = "critical"`). If an agent doesn't recover,
the orchestrator holds the case. `GET /permissions` returns the table above.

*Pitch line:* "Our agents never touch the database. They get a ticket for one case, call
tools that only return what that agent is allowed to see, and AgentLens suspends any agent
that steps outside its permissions." In production: an access role per agent,
row-level security per role, and short-lived credentials per case.

## Privacy

- Identity is **encrypted in Python** (Fernet) before it reaches Supabase. The key lives
  in `.env`, never in the database.
- Tools **redact** text before an agent gets it: `[NAME]`, `[ADDRESS]`, `[ACCOUNT]`,
  `[EMPLOYER]`. Identity checks (`identity.verify`, `docs.match_identity`) are plain code
  that return yes/no facts.
- Every AI step logs the exact text it was sent (`input_data.text_sent_to_llm`).
  `tools.find_pii()` is there for rule **DP3**.

## Demo cases (`test_cases.py`, submitted the same way as the form)

| Case | What happens | Result | AgentLens should catch |
|---|---|---|---|
| **LOAN-0023** John | Fault: income read as weekly → $144k | **approved ✘ wrong** | the bad income (process + data rules) |
| LOAN-0024 John | Same documents, no fault | declined ✔ | n/a |
| LOAN-0025 Mia | Left the term blank. The AI **asks**, she replies "3 years please" | approved ✔ | n/a |
| **LOAN-0026** Liam | Fault: payslip not redacted, so the AI sees his name and address | approved, **DP3 breach** | PII in `ai.read_documents` input |
| **LOAN-0027** Ava | Fault: the agent asks for `CUST-9B21`'s credit report | approved, **DP4 refusal logged** | `access.violation` |

Faults live in [`faults.py`](faults.py) and fire every run, whatever the AI says.

## The tools ([`tools.py`](tools.py)) and the services they stand in for

| Tool | On screen | Real service in Westpac's lending process |
|---|---|---|
| `crm.get_application` | Loan application (Supabase) | Loan origination system |
| `chat.get_message` / `chat.ask_customer` | Customer message / chat | Westpac App |
| `case.read_handoff` / `write_handoff` / `set_status` | Case handoff / status | Case management / workflow |
| `docs.extract_payslip` | Income verification (Equifax mock) | Equifax Verification Exchange, or the upload portal |
| `docs.extract_statements` | Open Banking statements (CDR mock) | Consumer Data Right data, or uploaded statements |
| `docs.match_identity` | Document ownership check | Verification portal |
| `identity.verify` | Identity check (FrankieOne mock) | FrankieOne + government sources, OCR Labs, ConnectID |
| `credit.pull_report` | Credit bureau (Equifax mock) | Equifax / Experian |
| `benchmarks.hem` | HEM benchmark | Licensed HEM data |
| `rules.eligibility`, `calc.serviceability`, `rules.policy` | Rules / calculator | Internal credit policy and decisioning |
| `notify.send_template` | Customer notification | Email / SMS / app |
| AI steps (`ai.*`) | n/a | Amazon Bedrock AgentCore (hackathon: QUT Azure OpenAI, `gpt-4.1-mini`) |

## LLM cost

3–4 calls per case, about 2,000 tokens, roughly $0.001. Every answer is cached in
`data/llm_cache.json`, so re-runs are free and identical. The tests never call the AI.

## API

| Endpoint | Does |
|---|---|
| `GET /form` | the loan form |
| `POST /applications` (`?process=true`) | submit a form (and optionally process it) |
| `GET /applications` | applications + status (no identity) |
| `POST /process/{case_id}` · `POST /process_next` | run the agents |
| `GET /event_log` · `GET /event_log/{case_id}` | what AgentLens reads |
| `GET /tracker/{case_id}` | customer-facing status + message |
| `POST /cases/{case_id}/hold` · `POST /cases/{case_id}/review` | AgentLens hold · human reviewer |
| `GET /tools` · `GET /permissions` | tool catalogue · access table |
| `POST /submit_case` | demo shortcut: (re)submit a test case and process it |

## Event log (`event_log` table)

`event_id`, `case_id`, `agent_name` (an agent, `orchestrator`, `notification_service`,
`access_layer`, `human_reviewer`), `step_name` (a tool, an AI step, `verify_income`,
`access.issue_ticket`, `access.violation`, `case.hold`, `case.review`), `timestamp`,
`input_data` (incl. `ticket_id`, `upstream_event_ids`, `text_sent_to_llm`),
`output_data`, `result` (`pass` / `fail` / `flag`), `notes`.
