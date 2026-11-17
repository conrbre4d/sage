# Building the AgentLens dashboard in Lovable

## How to use this (for you)

1. **Start the AgentLens API on your laptop** (it has to be running for Lovable to show real data):
   ```powershell
   cd agentlens
   uvicorn agentlens:app --reload
   ```
   The API is at `http://127.0.0.1:8000` (docs: `http://127.0.0.1:8000/docs`). CORS already allows Lovable's
   domains (`*.lovable.app`, `*.lovableproject.com`) and `localhost`.
2. **In Lovable, start a new project and paste everything in the "Prompt" section below** (from
   `Build a web app called AgentLens…` to the end) as the first message.
3. **Connect it to your API:**
   - In Chrome, Lovable's preview can usually call `http://127.0.0.1:8000` on your own laptop.
     If Chrome shows a "Allow access to devices on your local network?" prompt, click **Allow**.
   - If the preview still can't reach it, use either of these:
     - **Export to GitHub and run it locally** (`npm install`, `npm run dev`). It runs on `http://localhost:5173`, which
       the API already allows. Set `VITE_AGENTLENS_API_URL=http://127.0.0.1:8000` in its `.env`.
     - **A tunnel**, e.g. `cloudflared tunnel --url http://127.0.0.1:8000`, then set the API URL in the app's settings to
       the `https://…trycloudflare.com` address. This makes your (synthetic, unauthenticated) API public while it runs,
       so close it afterwards.
4. **Don't connect Lovable to Supabase directly.** The tables have Row Level Security with no policies, so the
   browser key can't read anything, and the secret key must never go in a web app. Everything goes through the API.
5. **Good follow-up messages for Lovable** (one at a time):
   - "Use the real API for every screen; remove any mock data."
   - "Make the review queue and case panel feel calmer: more white space, fewer borders."
   - "Add keyboard shortcuts: J/K to move through the queue, A approve, D decline, R recalculate (with confirm)."
   - "Check every screen at 1280px and 390px wide and fix anything that overflows."

---

## Prompt

Build a web app called **AgentLens**, a calm, bank-grade review dashboard for a credit reviewer. It monitors three AI
agents that process personal-loan applications. Its tagline is "AI agents earn the right to work alone": each agent holds
a licence like an Australian learner driver (**L-plates → Red P → Green P → Full licence**; there is no suspended level). It loses
the licence automatically when it makes mistakes, and earns it back only when a named person approves.

This is a front end only. **All data comes from an existing REST API** (described below). Don't use mock data or a
database of your own. Don't add authentication. The API base URL comes from `import.meta.env.VITE_AGENTLENS_API_URL`, with a
fallback of `http://127.0.0.1:8000`. Add a small "API" setting in the sidebar footer to change it, saved in localStorage.

### Visual design

- Look: clean and quiet, like a modern banking back office. White cards on a light grey background (`#F3F4F6`).
  Cards have 16px radius, a 1px `#E5E7EB` border and no heavy shadows. Use a lot of white space. Font: Inter. Body is 14px;
  page titles are 22px semibold; big numbers are 32–34px bold.
- Colours (use as tokens; also support a dark mode):
  - Ink `#111827` · muted `#6B7280` · primary/dark buttons `#0F172A`.
  - **High** severity: orange (bg `#FFF4E0`, text `#C2410C`, accent bar `#F5B14C`).
  - **Critical**: red (bg `#FEF2F2`, text `#B42318`).
  - **Medium**: blue (bg `#E8F0FE`, text `#1D4ED8`).
  - Success/approved: green (bg `#ECFDF3`, text `#15803D`).
  - AI content: violet (bg `#EEF2FF`, text `#4F46E5`), always marked with a ✦ icon.
- Licence badges look like Australian plates:
  - **L-plates**: yellow square with a black "L".
  - **Red P**: white plate with a red "P".
  - **Green P**: white plate with a green "P".
  - **Full licence**: green pill.

- Status pills:

  | Status | Shown as | Colour |
  |---|---|---|
  | `submitted` | Waiting | grey |
  | `verifying` | Verifying… | violet, gently pulsing |
  | `deciding` | Deciding… | violet, gently pulsing |
  | `approved` | Approved | green |
  | `declined` | Declined | grey |
  | `referred` | Referred to a person | blue |
  | `held` | Held for review | orange |
  | `error` | Error | red |

- Use shadcn/ui + Tailwind + lucide icons. Accessible: WCAG AA contrast, visible focus rings, full keyboard use,
  `aria-live` for progress, and respect `prefers-reduced-motion`.
- Responsive. The left sidebar collapses to a top bar under 1000px, and nothing scrolls sideways at 390px.

### Layout

**Left sidebar (250px):**
- At the top: a logo (a dark rounded square with a magnifying-glass icon), "AgentLens", and a small yellow
  "Prototype · synthetic data" badge.
- **Applications** group, each item with a count badge:
  - **Review queue** (count = held cases, orange badge)
  - **All applications** (total)
  - **Reviewed** (reviewed count)
- **Agents** group:
  - **Agents & licences** (3)
  - **Improvements** (count of suggestions that are `proposed` or `testing`)
- A demo box at the bottom:
  - A primary **+ New application** button.
  - A select for 22 / 30 / 45 / 60 applications, plus a **Run demo batch** button. It must confirm first: "This CLEARS the
    demo data and submits a fresh batch of synthetic applications."
  - A **Process waiting (n)** button, shown only when `stats.waiting > 0`.
- The footer: a "CR · Credit reviewer" avatar and a storage badge ("Supabase" or "Local" from `storage`).

**Main area:** a page title and one-line description, then a **job banner** under the title:
- While `job.running`: a pulsing dot, `job.message` and a progress bar (`job.done / job.total`).
- When it finishes: a green "✓ message".
- On error: red.
- If `stats.in_progress > 0`: "n application(s) being processed by the agents".

Poll `GET /api/overview` every 2.5 s. **Only re-render what changed**: never reset an open tab, a scroll position, an
open dialog or a half-typed input because of polling.

### Screens

**1. Review queue** (the default; this matches the existing design)

*Three stat cards in a row:*
- **"Cleared automatically"**: big `stats.cleared`, a small "of `stats.total`", and a green progress bar.
- **"Held for you"**: an orange 2px border, big `stats.held`, and severity chips such as "5 high", "1 critical" from
  `stats.held_by_severity`.
- **"Agents restricted"**: big = number of agents whose `level` isn't `FULL`. Under it, one row per agent: name,
  licence badge (with "↓" when not Full), and trust score.

*Below, two columns:*
- **Left (290px), "Held cases":** cards from `queue[]`. Each card has:
  - a coloured accent bar on the left by severity,
  - the bold `title`, e.g. "Debts left out of the repayment maths",
  - `case_id · $15k` in mono,
  - a severity pill on the right.

  The selected card has a 2px dark border. Select the first case automatically.
- **Right:** the **Case panel** (see 4) for the selected case.
- *Empty state:* "Nothing held. Every case cleared." (or "No applications yet: run the demo batch or submit a new
  application").

**2. All applications**
- Filter pills: All · In progress · Approved · Declined · Referred · Held, each with a count. Plus a "Search case ID" box.
- A table from `applications[]`, newest first:

  | Column | What it shows |
  |---|---|
  | Case | mono, bold |
  | Loan | `$12,000 car · 3 yrs` |
  | Status | pill |
  | Chance of approval | a small bar + %, from `approval_probability`: green ≥70%, amber ≥40%, red below; "n/a" if null |
  | Estimated result | `estimated_timeframe`, muted |
  | AgentLens | an orange "⚑ {finding}" if `flagged`; "watching…" while in progress; otherwise a green "✓ no problems" |

- Clicking a row opens the **Case page** (full width, with "← All applications" back).

**3. Reviewed**
- A table from `reviewed[]`: Case · Loan · What AgentLens found (`finding` as an orange chip) · Outcome (pill) ·
  Reviewed by · When (`reviewed_at`, e.g. "17 Nov, 3:42 pm").
- Clicking opens the Case page.
- *Empty state:* "Nothing reviewed yet."

**4. Case panel / Case page** (`GET /api/case/{id}`; reload it only when that case's status changes)

*Header:*
- `case_id · token · agent_label` (mono, muted), the status pill and the severity pill.
- Four small fact tiles:
  - **Loan:** `$15,000 · debt consolidation`, `5 years`.
  - **Chance of approval:** % and a bar.
  - **Estimated result:** `estimated_timeframe` plus "by `estimated_decision_by`" (hide "by" when decided).
  - **Submitted:** the date, plus "Reviewed by X" if any.

*Then tabs:* **Summary · Application form · Documents · Activity (n)**.

- **Summary**
  - **If `primary` exists:**
    - Big title `primary.details.title`.
    - Two compare cards side by side with an arrow between. Left (neutral): `details.left.label/value/sub`.
      Right (red-tinted): `details.right.label/value/sub`. Big value, small label and sub. Example: "Debts in credit
      file **2** · Card $8k · HECS-HELP" → "Debts the agent used **0** · surplus overstated by $454/mo".
    - A violet AI box: ✦, the `explanation` from `GET /api/case/{id}/explain`, then a bold "→ {action}" line and a tiny
      "AI explanation" or "rule-based" tag (from `by`). Show a skeleton while it loads.
  - **If no `primary` and the status is waiting, verifying or deciding:** a violet "The agents are working on this case"
    box, with the stage in plain words.
  - **Otherwise:** a green box: "✓ AgentLens checked every step and found nothing wrong. Outcome: Approved."
  - **"Where it went wrong" (or "Steps"):** step chips from `chips[]`, joined by "→":
    - `ok`: green ✓
    - `bad`: red ✕
    - `skip`: grey ·
    - Then a final chip: dark "■ Held", grey "… In progress", or green "✓ Approved/Declined/Referred".
  - **If the status is `held`:** three big buttons in a row:
    - **Send back to recalculate** (dark)
    - **Approve anyway** (outline)
    - **Decline** (red outline)

    Each opens a confirm dialog, then `POST /api/jobs/review/{id}` with `{action: "recalculate"|"approve"|"decline", reviewer: "Credit reviewer"}`.
  - **"Decision agent's reasoning" card** (if `agent_decision`):
    - Decision pill, "confidence 95%" (or "rules (AI offline)" if null), and a "borderline" chip if true.
    - The `reasoning` paragraph.
    - A muted line: "Hard rules said: {rules_decision}".
  - **"What AgentLens found (n)"**, one card per item in `mistakes[]`:
    - Header: a type pill (CRITICAL red, MAJOR orange, MINOR blue), the `title`, and a muted
      "{agent_label} · rule {rule} · earlier run · fixed" tag.
    - Two boxes side by side: a red-tinted **"What the agent concluded"** (`agent_stated_reasoning`) and a green-tinted
      **"What should have happened"** (`what_should_have_happened`).
    - Then "**Why it's wrong:** {mistake_reason}".
- **Application form** (`GET {API}/bank/applications/{id}/form?reveal=false`)
  - A privacy bar at the top: "🔒 Personal details are hidden. The AI agents never see them either." and a **Show personal
    details** button.
  - Clicking it opens a confirm dialog ("This is logged in the audit trail"), then refetches with `reveal=true` and the bar
    turns to "🔓 Personal details are showing. This view was logged." with a **Hide** button.
  - Never cache or prefetch the revealed version.
  - Render `sections[]` as read-only form cards in a 2-column grid: section title (🔒 if `private`), then label/value rows
    (skip null values).
  - Then "In the customer's own words": `message` in a quote block with a violet left border.
- **Documents** (`GET {API}/bank/applications/{id}/documents?reveal=false`, same privacy bar)
  - Three tiles for `documents.payslip`, `documents.bank_statement` and `documents.credit_report`. Each tile is a
    scaled-down "paper" preview: white sheet, subtle shadow, monospace, the top of the text visible, fading out at the
    bottom. Under it: a 📄 icon, the `title`, and an **Enlarge ⤢** button.
    - `kind: "text"`: show `text` with whitespace preserved.
    - `kind: "table"`: show `title`, `source`, and `rows[]` as a two-column table.
  - Clicking the preview or Enlarge opens a large lightbox at full size. It has the title, "case · personal details
    hidden/showing", and a Close button. Esc and clicking outside also close it.
  - A muted note: "The verification agent's AI only ever reads these with personal details hidden."
- **Activity** (from `timeline[]`)
  - A readable vertical timeline grouped into cards by consecutive `agent_label`: Orchestrator, Intake agent,
    Verification agent, Decision agent, Notification service, AgentLens, Credit reviewer.
  - Each event row has:
    - a small round icon by `result`: pass green ✓, flag amber !, fail red ✕;
    - the bold `title`;
    - a muted `note` (if different from the title);
    - `facts[]` as a two-column key/value list;
    - on the right, "#event_id" and the time.
  - If a mistake in `mistakes[]` (not `earlier_run`) has the same `event_id`, tint that row red and add
    "⚑ AgentLens: {title} (MAJOR)".
  - A checkbox at the top: "Show system steps (tickets, handoffs)". Hide events with `system: true` by default.

**5. Agents & licences** (from `agents[]` in the overview)
- *Three cards.* Each card has:
  - Name and licence badge.
  - A big trust score (0–100) with the label "trust score".
  - A 4-segment "licence ladder" (L → Red P → Green P → Full), filled up to the current level: green when Full or
    Green P, amber at Red P, red at L-plates.
  - Key/values: Mistake rate (last N cases) as %; "A person checks X% of its cases" (`human_review_rate`); Cases at this
    level "x of y needed" (`cases_needed_to_promote`); Open mistakes; "Fast-tracked after a fix" if true.
  - A **Promote…** button when not Full. It opens a dialog with a required "Your name" field and the line "Promotion
    always needs a named person", then calls `POST /licence/{agent}/promote` with `{approved_by}`. If the API refuses,
    show its `detail` text, e.g. "needs 30 cases at L-plates (has 3)".
- *Below, two columns:*
  - **Mistakes** for the selected agent (click a card to select it), from `GET /api/agent/{agent}` → `mistakes[]`.
    Show the same "What the agent concluded / What should have happened" cards; clicking the case ID opens it.
  - **Licence history** from `history[]`: a score change (red "−2", grey "0"), agent name, `reason`, and
    "score 88 · Red P · LOAN-0023".

**6. Improvements** (from `suggestions[]`)
- A 4-step explainer strip:
  1. Pattern (same mistake on 3+ cases → a suggested fix)
  2. Apply (a named person approves; the agent goes back to L-plates)
  3. Verify (replay every failed case with the fix)
  4. Resolve or iterate
- One card per suggestion. Header: "#id", a status pill (proposed blue · testing yellow · applied orange · resolved
  green), and "Verification agent · iteration 2 · follows #1". Then a definition list:
  - Pattern (`pattern_description`)
  - Root cause
  - Fix (`fix_description` or `suggested_fix`, bold, "approved by X")
  - Cases (clickable case IDs)
  - Replay result: "2 of 3 now pass; still failing: LOAN-0035" from `verify_result`
- Buttons:
  - **proposed → "Apply fix…"**: a dialog with "Approved by" (required) and "Fix description" (prefilled with
    `suggested_fix`), then `POST /improve/{id}/apply` with `{approved_by, fix_description}`.
  - **testing → "Verify: replay n cases"**: `POST /api/jobs/verify/{id}`; progress shows in the job banner.
- A red-outline **Simulate a critical incident** button at the top right. It confirms ("the decision agent approves a
  loan despite a failed credit check; AgentLens should send it straight back to L-plates"), then `POST /api/jobs/incident`.

**7. New application**
- A friendly multi-step form (About you → Work & income → The loan → Your finances → Your message → Review & submit).
- It posts `POST /api/applications` with this JSON:
  ```json
  {"name": "Mia Chen", "date_of_birth": "1995-03-02", "address": "4 Banksia Court, Toowong QLD 4066",
   "account_number": "032-000 51 7788", "employer": "Riverbend Health Pty Ltd", "employment": "full-time, 4 years",
   "gross_income": 85000, "pay_frequency": "fortnightly", "requested": 12000, "term_years": null,
   "purpose": "car", "monthly_expenses": 2100, "credit_card_limit": 5000, "has_hecs": false,
   "existing_customer": true, "residency": "AU", "household": "single", "dependants": 0,
   "customer_message": "Hi, I'm Mia Chen, I'm 31. I'd like to borrow $12,000 to buy a car.",
   "customer_replies": ["3 years please"]}
  ```
- Field rules:
  - `pay_frequency` is weekly, fortnightly or monthly.
  - `purpose` is one of: debt consolidation, car, home improvement, travel, wedding, education, other.
  - `household` is single or couple.
  - `term_years` is optional ("leave blank and our assistant will ask"); `customer_replies` is a "demo only" field.
- Prefill it with the example above. Show a banner: "Synthetic demo: don't enter real details. Personal details are
  encrypted and never shown to the AI."
- The response is `{case_id, dashboard}`. Then open that case's Case page so the reviewer can watch it go Waiting →
  Verifying… → Deciding… → a result, live.

### API reference (all JSON)

**Reads**
- `GET /api/overview` returns:
  - `storage`
  - `stats {total, processed, cleared, held, reviewed, waiting, in_progress, held_by_severity {Critical, High, Medium}}`
  - `queue[] {case_id, amount, token, title, severity, agent}`
  - `agents[] {agent_name, label, level (L|P1|P2|FULL), level_name, trust_score, mistake_rate (0–1),
    human_review_rate (0–1), cases_at_level, cases_needed_to_promote, fast_track, open_mistakes, recent_cases[]}`
  - `suggestions[] {id, agent_name, status, iteration, parent_id, pattern_description, root_cause, suggested_fix,
    fix_description, applied_by, case_ids[], verify_result {passed[], failed[], replayed}}`
  - `history[] {agent_name, case_id, score_change, reason, new_score, new_level}`
  - `job {name, running, done, total, current, message, error}`
  - `statuses {case_id: status}`
  - `applications[] {case_id, status, amount, purpose, term_years, decision, submitted_at, approval_probability,
    estimated_timeframe, reviewed_by, reviewed_at, flagged, finding, live}`
  - `reviewed[]` (same shape as `applications[]`)
- `GET /api/case/{id}` returns:
  - `{case_id, token, status, amount, purpose, term_years, decision, approval_probability, estimated_timeframe,
    estimated_decision_by, submitted_at, reviewed_by, reviewed_at}`
  - `primary {mistake_type, agent_label, details {title, severity, left {label, value, sub}, right {label, value, sub}}}`
    or `null`
  - `mistakes[] {mistake_type, title, agent_label, rule, agent_stated_reasoning, what_should_have_happened,
    mistake_reason, event_id, earlier_run, resolved}`
  - `chips[] {label, state (ok|bad|skip), result}`
  - `agent_decision {decision, reasoning, confidence, borderline, rules_decision, decided_by, note}` or `null`
  - `timeline[] {event_id, agent_label, step, title, result (pass|flag|fail), note, time, facts [[label, value]], system}`
- `GET /api/case/{id}/explain` returns `{explanation, action, by ("ai"|"rules")}`.
- `GET /api/agent/{agent}` returns `{history[], mistakes[], suggestions[]}`.
- `GET /bank/applications/{id}/form?reveal=false` returns `{sections[{title, private?, fields[[label, value]]}], message,
  revealed}`.
- `GET /bank/applications/{id}/documents?reveal=false` returns `{documents {payslip, bank_statement:
  {title, kind:"text", text}, credit_report: {title, kind:"table", source, rows[[label, value]]}}}`.

**Writes**
- `POST /api/applications` with the form JSON returns `{case_id, dashboard}`.
- `POST /api/jobs/batch {count}`, `POST /api/jobs/process_waiting`, `POST /api/jobs/incident`,
  `POST /api/jobs/verify/{id}` and `POST /api/jobs/review/{case_id} {action, reviewer}` start background jobs; watch
  `job` in the overview. A 409 means another job is still running: show "Busy: wait for the current job to finish".
- `POST /improve/{id}/apply {approved_by, fix_description}` and `POST /licence/{agent}/promote {approved_by}`.

**Errors:** the API returns `{detail: "…"}` with 4xx. Show `detail` in a toast; never a raw stack trace.

### Behaviour and wording rules

- Plain English everywhere. No internal jargon such as "DTI" on its own. Write "debt-to-income (limit 50%)".
- Money is shown as "$15,000", or "$15k" in compact lists.
- Never show personal details unless the reviewer clicks **Show personal details**, and say clearly that it's logged.
- Use dialogs (not browser `alert`/`prompt`) for confirmations and names. Use toasts for results.
- Skeletons while loading. Friendly empty states with a next action. Disable action buttons while `job.running`.
- Keep it fast: no layout jumps when data refreshes. Animate only small things (pulses, progress bars).
