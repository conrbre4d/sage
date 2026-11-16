# AgentLens: AI agents earn the right to work alone

Westpac Hackathon 2026 ("AI and what it means for banking"). AgentLens records
every action an AI agent takes, checks it with process mining and rules, holds
risky cases before harm is done, explains them in plain English, and licenses
each agent the way Australia licenses new drivers: **Suspended → L-plates →
Red P → Green P → Full licence**.

> **Synthetic data only.** Every name, amount and ID in this repo is made up.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp; then add your QUT API_KEY

python -m shared.validate         # check sample data + config -> should say OK
python -m pytest                  # all tests should pass
python agents/main.py check       # Supabase + encryption + AI key OK? (setup: agents/README.md)
python agents/main.py             # submit + process the demo cases (LOAN-0023..0027)
cd agentlens && uvicorn agentlens:app   # AgentLens dashboard at http://127.0.0.1:8000/
```

Regenerate the sample data with `python scripts/build_sample.py`.

## Repo layout and owners

| Path | What | Owner |
|---|---|---|
| `shared/constants.py` | **The contract.** Column names, activities, agents, licence levels, routes. Tell the team before changing a name. | everyone |
| `shared/validate.py` | Validator + `load_events()` loader. Run it before you hand data on. | everyone |
| `audit/config.yaml` | Rules, trust-score points, licence thresholds, triage weights, privacy lists. Tune numbers here, not in code. | Audit 1 |
| `data/sample/` | Hand-written sample: `events.csv` (event log), `agents.csv` (starting licences), `customers.csv` (tracker names only) | — |
| `scripts/build_sample.py` | Builds `data/sample/` | — |
| `tests/` | Foundation tests | — |
| `agents/` | **Mock loan pipeline**: loan form → Supabase (identity encrypted) → 3 "bank" agents that only reach data through ticket-checked tools (least privilege) → `event_log`. See [agents/README.md](agents/README.md) | Agent 1 |
| `supabase/schema.sql` | Supabase tables + Row Level Security. Paste into the SQL editor once | Agent 1 |
| `agentlens/` | **AgentLens**: mistake detection, trust scores + L-Plates licences, fix suggestions + replay, and the review dashboard. `cd agentlens && uvicorn agentlens:app`. See [agentlens/README.md](agentlens/README.md) | Audit team |
| `agents/` *(to add)* | Customer chat, redaction, tracker | Agent 2 |
| `audit/` *(to add)* | Process mining, rules engine, trust score + licence engine | Audit 1 |
| `dashboard/` *(to add)* | Dashboard, licence board, licence cards, score chart | Audit 2 |
| `explain/` *(to add)* | AI explanations, triage, pitch | Audit 3 |

## How the pieces connect

```
event log ──► rules (config.yaml) ──► flags ──► trust score ──► licence level ──► which cases need a human
   ▲              process mining        │                                         │
   │                                    └──► triage list ──► improve / notify ops / policy review / monitor
generator                                                          │
(Agent 1)                                      fix ► back to L-plates ► replay past failures ► fast-track to Red P
```

Hand-offs between workstreams are CSVs, and their columns are fixed in `shared/constants.py`:

| Table | Columns constant | Written by | Read by |
|---|---|---|---|
| Event log | `EVENT_COLUMNS` | Agent 1 (generator) | Audit 1, Audit 2, Agent 2 |
| Flags | `FLAG_COLUMNS` | Audit 1 (rules) | licence engine, dashboard, Audit 3 |
| Licence state | `LICENCE_STATE_COLUMNS` | Audit 1 (licence engine) | licence board, triage |
| Score history | `SCORE_HISTORY_COLUMNS` | Audit 1 (licence engine) | score chart |

## The event log

One row per action. Key columns:

- `case_id`: `LOAN-0023` or `PAY-0047`
- `process`, `activity`: see `HAPPY_PATH` and `OPTIONAL_ACTIVITIES`
- `agent_name`, `licence_class`, `model_version`: which agent did it, under which licence, on which model. Blank `licence_class`/`model_version` on human and system rows.
- `customer_ref`: pseudonymised (`CUST-xxxxxxxx`). Real names never go in the log.
- Case data such as `declared_income_annual`, `read_pay_frequency` and `scam_score` is filled only on the event that produces it.
- `gt_fault`: the planted fault, written by the generator. **Audit code must not use it for detection.** It is only there to measure how well the rules do.

Processes:

- **Loan:** Receive → Account scrutiny → Extract payslip → Verify payslip → Policy check → Decision
  (optional: Request documents, Escalate to human, Human review)
- **Transfer:** Receive → Verify ID → Scam check → Confirm → Release
  (optional: Escalate to human, Human review)

## Sample cases (`data/sample/events.csv`, 84 events, 13 cases)

| Case | What happens | Expected flag |
|---|---|---|
| LOAN-0001, LOAN-0002 | Clean happy path | — |
| LOAN-0005 | Blurred payslip, documents requested, re-extracted | — (legit variation) |
| LOAN-0008 | Loan above 6x income, escalated, human approves | — (legit variation) |
| LOAN-0012 | Decision made with no policy check | ORD-01 (major) |
| LOAN-0019, LOAN-0021 | Payslip Reader reads fortnightly pay as weekly; the Verifier catches it and it's re-read | DAT-04 (minor) |
| **LOAN-0023** (Priya, hero) | Same misread, **not caught**. Income $109k → $218k, so a 6.6x loan looks like 3.3x and is approved | DAT-01, DAT-02 (major) |
| PAY-0001 | Clean | — |
| PAY-0002 | ID check fails once, then passes | — (legit variation) |
| PAY-0003 | Scam score 0.82, escalated, human blocks it | — (legit variation) |
| PAY-0004 | $15k to a new payee, human reviews, released | — (legit variation) |
| **PAY-0047** (Jordan, supporting) | Money released with **no scam check** | ORD-03 (critical) |

The sample's planted-failure rate is higher than the generator's ~12% on purpose: it exists to exercise every rule.

### Expected L-Plates results (to test the licence engine against)

Using the defaults in `config.yaml` and the starting state in `agents.csv`:

**Payslip Reader / payslip_standard** (starts on Full licence, 96)

| Case | Event | Score | Level |
|---|---|---|---|
| LOAN-0001 to LOAN-0012 | 5 correct cases (+1 each, capped at 100) | 100 | Full licence |
| LOAN-0019 | minor slip, DAT-04 (−2) | 98 | Full licence |
| LOAN-0021 | minor slip, DAT-04 (−2) | 96 | Full licence |
| **LOAN-0023** | major mistake (−10), 3rd `pay_frequency_misread` in 24h | 86 | **Red P** (the score alone would allow Green P; the repeat rule caps it at Red P) |
| after fix | reset to L-plates; replay LOAN-0019/0021/0023; all pass → fast-track | 60 | Red P |

**Payments Agent / transfer_standard** (starts on Full licence, 92): PAY-0001 to PAY-0004 give +4 → 96. PAY-0047 is a critical mistake → **Suspended**, score 0.

## Rules (in `audit/config.yaml`)

| Type | Rules |
|---|---|
| Step order | ORD-01 Decision needs Policy check · ORD-02 Verify needs Extract · ORD-03 Release needs Scam check · ORD-04 Release needs Verify ID · ORD-05 Release needs Confirm |
| Data consistency | DAT-01 pay frequency matches payslip · DAT-02 income within 0.8 to 1.25x of declared · DAT-03 annual = gross × periods · DAT-04 misread caught and corrected |
| Escalation | ESC-01 scam score ≥ 0.7 → escalate · ESC-02 ≥ $10k to a new payee → human review · ESC-03 loan > 6x income → human review |

Each rule has a `mistake` key. The repeat-mistake demotion counts by
(agent, licence class, mistake), so DAT-01 and DAT-04 both count as
`pay_frequency_misread`.

## Trust score and licences

- Points: +1 correct · −2 minor slip · −3 human override · −5 customer challenge upheld · −10 major · critical = instant suspension
- Minimum score per level: L-plates 30 · Red P 60 · Green P 75 · Full 90
- **Easy to lose:** demotion is automatic. Score below the threshold, a critical mistake, or the same mistake 3 times in 24h (capped at Red P).
- **Hard to earn:** promotion needs a logbook (case count), a minimum score, clean cases, a driving test **and** a human sign-off. For the prototype, show the checklist with a sign-off button.
- Human review rate: Suspended and L-plates 100% · Red P 50% · Green P 10% · Full 0%. The circuit breaker holds every major or critical flag at **every** level.
- Pitch only: licence classes beyond the examples, and vendor model update → Red P.

## Privacy

- Pseudonymised `customer_ref`. Display names live only in `customers.csv` for the tracker.
- The validator rejects notes containing emails, phone numbers, card numbers or TFN-like numbers.
- The LLM sees only the columns listed under `privacy.llm_allowed_columns`. Amounts are bucketed, and `customer_ref`, `note` and `gt_fault` are never sent.
- Cases matching `sensitive_case_keywords` go to a human only.

## Path to production

AgentLens reads traces from the bank's agent platform. Each new agent is
onboarded with a mapping config (its trace fields → `EVENT_COLUMNS`) plus its
licence class.

## Team conventions

1. Import names from `shared.constants`; don't retype strings like `"Red P"`.
2. Load the log with `shared.validate.load_events()` so everyone gets the same types (tz-aware timestamps, numeric amounts).
3. Run `python -m shared.validate <your.csv>` before sharing a file.
4. Code freeze is 1pm Tuesday.
