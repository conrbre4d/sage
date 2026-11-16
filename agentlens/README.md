# AgentLens: AI agents earn the right to work alone

AgentLens monitors, ranks and improves the bank's 3 AI agents (`intake_agent`,
`verification_agent`, `decision_agent` in [`../agents`](../agents)).

- It only **reads** the `event_log` table (plus each case's status).
- It only **writes** its own tables (and holds cases).
- It touches the agents only through **replay**.

> **Promotion always needs a named person. Demotion never does.**

## Run it

```bash
cd agentlens
uvicorn agentlens:app --reload
```

| Open | What |
|---|---|
| <http://127.0.0.1:8000/> | **Dashboard**: review queue, case details, agent licences, fixes |
| <http://127.0.0.1:8000/bank/form> | The customer's **loan form**. Submitting it runs the agents with AgentLens watching, then opens the case in the dashboard |
| <http://127.0.0.1:8000/docs> | API docs |

Storage is whatever `.env` says: Supabase, or `AGENTLENS_BACKEND=local` for an offline SQLite file.
On Supabase, run the **whole** [`../supabase/schema.sql`](../supabase/schema.sql) once. It's safe to re-run.

### The demo

On the dashboard, click **Run demo batch**. This **clears the demo data** and submits 23–60 synthetic
applications, and the agents process them one by one. Or run it from the terminal:

```bash
python demo_seed.py            # the batch (30 cases)
python demo_seed.py --all      # the batch + the whole improvement loop + a promotion
```

The demo script, mapped onto our agents:

1. **verification_agent**: 7 correct cases, then 3 identical "income doubled" mistakes
   (LOAN-0023 John, LOAN-0029, and LOAN-0035, who is self-employed).
   - It's demoted **Full → Red P (P1)**.
   - **Suggestion #1** appears: "Require pay_frequency (pay period) as an explicit structured field…".
2. **Apply #1, then verify.** The 2 employee cases pass; the self-employed one still fails.
   This starts **iteration 2**, with **suggestion #2**: "Add labelled examples for self-employed income statements…".
3. **Apply #2, then verify.** All 3 pass.
   - The suggestion is **resolved** and the agent is **fast-tracked**: 10 cases instead of 30 to the next level.
   - After 10 more cases, a person promotes it.
4. **decision_agent** approves a loan despite a failed credit check (LOAN-0047).
   That's **CRITICAL**, so the agent is **instantly SUSPENDED**.

Also in the batch, for the review queue:

| Case | Issue |
|---|---|
| LOAN-0015 | Debts left out |
| LOAN-0031 | Unsure, not referred |
| LOAN-0042 | Old payslip |
| LOAN-0039 | Many credit checks ignored |

The batch also has normal declines, a hardship referral, and a customer the intake AI asks for their loan term.
The privacy (LOAN-0026, DP3) and access (LOAN-0027, DP4) demos run separately, because they suspend the verification agent:
`python ../agents/main.py LOAN-0026 LOAN-0027` while AgentLens is running.

## How it works

```
loan form ─► bank pipeline: intake ─► verification ─► decision ─► notification
                                   │              │
                     AgentLens scans the new events after each of these, BEFORE the customer hears:
                     mistakes ─► trust score + licence ─► hold the case ─► pattern? ─► suggestion
```

### Step 1: detect mistakes ([`detect.py`](detect.py), pure functions)

| Rule | Mistake | Example |
|---|---|---|
| 1 Wrong conclusion | **MAJOR** | Recalculates from `input_data`: "Agent concluded income=$144,000 but the payslip shows fortnightly pay of $2,769 (= $72,000/yr) — treated fortnightly as weekly." Also debts left out, ignored enquiries, a stale payslip, a borderline approval |
| 2 Missing reasoning | MINOR | A decision step with empty `notes` |
| 3 Skipped a required step | MAJOR | e.g. affordability before income was verified |
| 4 Ignored a failed upstream check | **CRITICAL** | Approved although a check failed |
| 5 Same wrong conclusion on 4+ cases in 24h | **CRITICAL** | A pattern, not a one-off. *The spec says 3+, but its demo needs 3 repeats to give P1 and a suggestion, so the 4th repeat is critical* |
| DP3 | CRITICAL | Personal details in an AI step's input |
| DP4 | CRITICAL | An agent asked for data outside its ticket |

Every mistake goes in `mistake_log` with the agent's own reasoning (`agent_stated_reasoning`) side by side
with `what_should_have_happened`. MAJOR and CRITICAL mistakes **hold the case** for a person.

### Step 2: score and rank ([`scoring.py`](scoring.py))

- **Points:** MINOR −1 · MAJOR −2 · CRITICAL −10 (score kept between 0 and 100). Any CRITICAL means **instantly SUSPENDED**.
- **Mistake rate:** weighted mistakes ÷ the agent's last 50 cases. The weights are MINOR 0.5 · MAJOR 1 · CRITICAL 2,
  which makes the demo work as written: 3 / 10 = 0.30 gives P1.

  | Mistake rate | Level |
  |---|---|
  | > 0.30 | L-plates |
  | > 0.15 | P1 (Red P) |
  | > 0.05 | P2 (Green P) |
  | otherwise | Full licence |

- **Demotion is automatic.** Every change is logged in `licence_history`.
- **Promotion is never automatic.** It needs `POST /licence/{agent}/promote {"approved_by": "name"}` and:
  - 30 / 20 / 15 cases at L / P1 / P2 (10 when fast-tracked), and
  - no CRITICAL mistake since the last level change.

### Steps 3 and 4: patterns, fixes, replay ([`improve.py`](improve.py), [`monitor.py`](monitor.py))

**Patterns.** The same agent and the same cause (`pattern_key`) on **3+ cases** creates an `improvement_suggestion` from a template:

| Pattern | Suggested fix |
|---|---|
| misreads field X | Require X as an explicit structured field, reject output missing it |
| skips required step | Add a hard dependency check in the orchestrator |
| ignores failed check | Make this agent fail-closed on any upstream fail |
| inconsistent on edge case | Add labelled examples for this case to the agent's prompt |

**Apply** (a named person approves it):
- The status becomes `testing`.
- The agent goes back to **L-plates** and `cases_at_level` resets to 0.
- The agent's logic for that pattern is fixed one level. Faults live in [`../agents/faults.py`](../agents/faults.py).

**Verify** replays every case the pattern failed on through the real pipeline:
- **All pass:** the suggestion is `resolved`, the agent is fast-tracked, and the pattern's mistakes are marked resolved (they leave the mistake rate).
- **Some fail:** the suggestion becomes `applied`, `iteration + 1`, and a **new suggestion** is created automatically. This loop repeats until everything passes.

## API

| Endpoint | Returns |
|---|---|
| `GET /rank` | All agents, worst first: score, level, mistake rate, cases at level, cases needed to promote |
| `GET /rank/{agent}` | One agent's score, level, mistake rate |
| `GET /mistakes/{agent}` | The full mistake log, reasoning side by side |
| `GET /improve/{agent}` | Every suggestion and iteration for the agent |
| `POST /licence/{agent}/promote` `{"approved_by"}` | Promotes one level. **Human-approved only**; 400 with the reason if not allowed yet |
| `POST /improve/{id}/apply` `{"approved_by", "fix_description"}` | `testing`; agent back to L-plates |
| `POST /improve/{id}/verify` | Replays the failed cases. Returns `resolved` or `iterate` (with the next suggestion) |
| `POST /replay/{agent}` `{"case_ids"?}` | Re-runs cases (default: the agent's open mistakes) |
| `POST /scan` | Checks any new events now (normally automatic) |
| `POST /api/applications` | The loan form: stores it and runs the agents in the background |
| `GET /api/overview`, `GET /api/case/{id}`, `GET /api/case/{id}/explain` | What the dashboard shows |
| `POST /api/jobs/batch` · `/api/jobs/verify/{id}` · `/api/jobs/review/{case}` | Background jobs started from the dashboard |

## Tables (in `supabase/schema.sql`)

- **Spec tables:** `agent_licence`, `mistake_log`, `licence_history`, `improvement_suggestion`.
- **Columns I added:** `recent_cases`, `fast_track`, `pattern_key`, `details`, `resolved`, `case_ids`, `parent_id`, `verify_result`. These are what the dashboard and the loop need.
- **One extra table:** `agentlens_state`, which stores the scan cursor.

AgentLens never reads `customer_identity` (the vault); a test checks this.

## Files

| File | What |
|---|---|
| `agentlens.py` | FastAPI app: all endpoints + the dashboard + the bank pipeline mounted at `/bank` |
| `detect.py` | Mistake rules (pure functions) |
| `scoring.py` | Score, level and promotion rules (pure functions) |
| `improve.py` | Pattern detection + suggestion templates |
| `monitor.py` | The engine: scan → mistakes → scores → holds → suggestions; apply / verify / replay / promote / review |
| `store.py` | AgentLens's tables |
| `demo_seed.py` | The demo batch and the scripted improvement loop |
| `dashboard.html` | The dashboard |
