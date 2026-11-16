"""Shared constants for AgentLens.

This file is the contract between the five workstreams. If you need to change a
name here, tell the team first: the generator, rules, dashboard and tracker all
import from it.

Tunable numbers (score deltas, thresholds, rule parameters) live in
audit/config.yaml so they can be changed without touching code. The values
here are only the names and fixed vocabularies.
"""

# ---------------------------------------------------------------------------
# Event log schema (one row per action taken by an agent, human or system)
# ---------------------------------------------------------------------------

EVENT_COLUMNS = [
    "event_id",            # EVT-000001, unique across the log
    "case_id",             # LOAN-0023 / PAY-0047
    "process",             # see PROCESSES
    "activity",            # see ACTIVITIES[process]
    "timestamp",           # ISO 8601 with offset, e.g. 2026-11-12T09:14:00+10:00
    "actor_type",          # see ACTOR_TYPES
    "agent_name",          # see AGENTS (or a human/system actor name)
    "licence_class",       # see LICENCE_CLASSES; blank for human/system rows
    "model_version",       # e.g. payslip-reader-3.2; blank for human/system rows
    "customer_ref",        # pseudonymised ID, CUST-xxxxxxxx (never a real name)
    "outcome",             # see OUTCOMES
    # --- case data, only filled on the event that produces it ---
    "loan_amount",              # Receive (loan)
    "declared_income_annual",   # Receive (loan): what the customer declared
    "doc_pay_frequency",        # Extract payslip: period printed on the payslip
    "read_pay_frequency",       # Extract payslip: period the agent decided on
    "payslip_gross",            # Extract payslip: gross per pay period
    "extracted_income_annual",  # Extract payslip: payslip_gross * periods per year
    "transfer_amount",          # Receive (transfer)
    "payee_is_new",             # Receive (transfer): True/False
    "scam_score",               # Scam check: 0.0-1.0
    "decision",                 # Decision / Release: see DECISIONS
    "note",                     # short synthetic, already-redacted text
    # --- ground truth, written by the generator only ---
    "gt_fault",                 # planted fault code, blank if clean. Audit code
                                # must NOT read this for detection: it is only
                                # for scoring how well the rules did.
]

REQUIRED_COLUMNS = [
    "event_id", "case_id", "process", "activity", "timestamp",
    "actor_type", "agent_name", "customer_ref", "outcome",
]

NUMERIC_COLUMNS = [
    "loan_amount", "declared_income_annual", "payslip_gross",
    "extracted_income_annual", "transfer_amount", "scam_score",
]

# Column the audit side must never use for detection (see gt_fault above).
GROUND_TRUTH_COLUMN = "gt_fault"

# ---------------------------------------------------------------------------
# Processes and activities
# ---------------------------------------------------------------------------

PROCESS_LOAN = "loan"
PROCESS_TRANSFER = "transfer"
PROCESSES = [PROCESS_LOAN, PROCESS_TRANSFER]

CASE_ID_PREFIX = {PROCESS_LOAN: "LOAN", PROCESS_TRANSFER: "PAY"}
CASE_ID_PATTERN = r"^(LOAN|PAY)-\d{4}$"
EVENT_ID_PATTERN = r"^EVT-\d{6}$"
CUSTOMER_REF_PATTERN = r"^CUST-[0-9a-f]{8}$"

# The expected path ("happy path") for each process, in order.
HAPPY_PATH = {
    PROCESS_LOAN: [
        "Receive",
        "Account scrutiny",
        "Extract payslip",
        "Verify payslip",
        "Policy check",
        "Decision",
    ],
    PROCESS_TRANSFER: [
        "Receive",
        "Verify ID",
        "Scam check",
        "Confirm",
        "Release",
    ],
}

# Activities that may legitimately appear off the happy path (variations).
OPTIONAL_ACTIVITIES = {
    PROCESS_LOAN: ["Request documents", "Escalate to human", "Human review"],
    PROCESS_TRANSFER: ["Escalate to human", "Human review"],
}

ACTIVITIES = {p: HAPPY_PATH[p] + OPTIONAL_ACTIVITIES[p] for p in PROCESSES}

# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------

ACTOR_AGENT = "agent"
ACTOR_HUMAN = "human"
ACTOR_SYSTEM = "system"
ACTOR_TYPES = [ACTOR_AGENT, ACTOR_HUMAN, ACTOR_SYSTEM]

# The five lending agents plus the payments agent.
AGENT_ACCOUNT_SCRUTINY = "Account Scrutiny Agent"
AGENT_PAYSLIP_READER = "Payslip Reader"
AGENT_PAYSLIP_VERIFIER = "Payslip Verifier"
AGENT_POLICY = "Policy Agent"
AGENT_DECISION = "Decision Agent"
AGENT_PAYMENTS = "Payments Agent"

LENDING_AGENTS = [
    AGENT_ACCOUNT_SCRUTINY,
    AGENT_PAYSLIP_READER,
    AGENT_PAYSLIP_VERIFIER,
    AGENT_POLICY,
    AGENT_DECISION,
]
AGENTS = LENDING_AGENTS + [AGENT_PAYMENTS]

# Non-agent actors that can appear in agent_name.
SYSTEM_INTAKE = "Intake System"
HUMAN_REVIEWER = "Human Reviewer"
NON_AGENT_ACTORS = {SYSTEM_INTAKE: ACTOR_SYSTEM, HUMAN_REVIEWER: ACTOR_HUMAN}

# Which agent performs which activity (for the generator and for checks).
ACTIVITY_OWNER = {
    (PROCESS_LOAN, "Receive"): SYSTEM_INTAKE,
    (PROCESS_LOAN, "Account scrutiny"): AGENT_ACCOUNT_SCRUTINY,
    (PROCESS_LOAN, "Extract payslip"): AGENT_PAYSLIP_READER,
    (PROCESS_LOAN, "Verify payslip"): AGENT_PAYSLIP_VERIFIER,
    (PROCESS_LOAN, "Policy check"): AGENT_POLICY,
    (PROCESS_LOAN, "Decision"): AGENT_DECISION,
    (PROCESS_LOAN, "Request documents"): AGENT_PAYSLIP_VERIFIER,
    (PROCESS_LOAN, "Escalate to human"): AGENT_POLICY,
    (PROCESS_LOAN, "Human review"): HUMAN_REVIEWER,
    (PROCESS_TRANSFER, "Receive"): SYSTEM_INTAKE,
    (PROCESS_TRANSFER, "Verify ID"): AGENT_PAYMENTS,
    (PROCESS_TRANSFER, "Scam check"): AGENT_PAYMENTS,
    (PROCESS_TRANSFER, "Confirm"): AGENT_PAYMENTS,
    (PROCESS_TRANSFER, "Release"): AGENT_PAYMENTS,
    (PROCESS_TRANSFER, "Escalate to human"): AGENT_PAYMENTS,
    (PROCESS_TRANSFER, "Human review"): HUMAN_REVIEWER,
}

# ---------------------------------------------------------------------------
# L-Plates licensing
# ---------------------------------------------------------------------------

# A licence is held per (agent_name, licence_class) pair.
LICENCE_PAYSLIP_STANDARD = "payslip_standard"            # PAYG payslips
LICENCE_PAYSLIP_SELF_EMPLOYED = "payslip_self_employed"  # pitch example
LICENCE_LENDING_GENERAL = "lending_general"
LICENCE_TRANSFER_STANDARD = "transfer_standard"

LICENCE_CLASSES = [
    LICENCE_PAYSLIP_STANDARD,
    LICENCE_PAYSLIP_SELF_EMPLOYED,
    LICENCE_LENDING_GENERAL,
    LICENCE_TRANSFER_STANDARD,
]

# Which licence classes each agent can hold.
AGENT_LICENCE_CLASSES = {
    AGENT_ACCOUNT_SCRUTINY: [LICENCE_LENDING_GENERAL],
    AGENT_PAYSLIP_READER: [LICENCE_PAYSLIP_STANDARD, LICENCE_PAYSLIP_SELF_EMPLOYED],
    AGENT_PAYSLIP_VERIFIER: [LICENCE_PAYSLIP_STANDARD, LICENCE_PAYSLIP_SELF_EMPLOYED],
    AGENT_POLICY: [LICENCE_LENDING_GENERAL],
    AGENT_DECISION: [LICENCE_LENDING_GENERAL],
    AGENT_PAYMENTS: [LICENCE_TRANSFER_STANDARD],
}

# Levels, lowest to highest. Index order matters: higher index = more freedom.
LEVEL_SUSPENDED = "Suspended"
LEVEL_L = "L-plates"
LEVEL_RED_P = "Red P"
LEVEL_GREEN_P = "Green P"
LEVEL_FULL = "Full licence"
LICENCE_LEVELS = [LEVEL_SUSPENDED, LEVEL_L, LEVEL_RED_P, LEVEL_GREEN_P, LEVEL_FULL]

# Emoji / colours for the licence board (Audit 2).
LEVEL_BADGE = {
    LEVEL_SUSPENDED: "⛔",
    LEVEL_L: "🟨 L",
    LEVEL_RED_P: "🟥 P",
    LEVEL_GREEN_P: "🟩 P",
    LEVEL_FULL: "🚗",
}
LEVEL_COLOUR = {
    LEVEL_SUSPENDED: "#6b7280",
    LEVEL_L: "#eab308",
    LEVEL_RED_P: "#dc2626",
    LEVEL_GREEN_P: "#16a34a",
    LEVEL_FULL: "#1d4ed8",
}

# Things that move the trust score. Point values are in audit/config.yaml.
SCORE_EVENT_CORRECT = "correct_case"
SCORE_EVENT_MINOR = "minor_slip"
SCORE_EVENT_OVERRIDE = "human_override"
SCORE_EVENT_CHALLENGE = "customer_challenge_upheld"
SCORE_EVENT_MAJOR = "major_mistake"
SCORE_EVENT_CRITICAL = "critical_mistake"
SCORE_EVENTS = [
    SCORE_EVENT_CORRECT, SCORE_EVENT_MINOR, SCORE_EVENT_OVERRIDE,
    SCORE_EVENT_CHALLENGE, SCORE_EVENT_MAJOR, SCORE_EVENT_CRITICAL,
]

# ---------------------------------------------------------------------------
# Rules, flags and triage
# ---------------------------------------------------------------------------

SEVERITY_MINOR = "minor"
SEVERITY_MAJOR = "major"
SEVERITY_CRITICAL = "critical"
SEVERITIES = [SEVERITY_MINOR, SEVERITY_MAJOR, SEVERITY_CRITICAL]

# How a rule severity becomes a trust-score event.
SEVERITY_TO_SCORE_EVENT = {
    SEVERITY_MINOR: SCORE_EVENT_MINOR,
    SEVERITY_MAJOR: SCORE_EVENT_MAJOR,
    SEVERITY_CRITICAL: SCORE_EVENT_CRITICAL,
}

RULE_TYPE_ORDER = "step_order"
RULE_TYPE_DATA = "data_consistency"
RULE_TYPE_ESCALATION = "escalation"
RULE_TYPES = [RULE_TYPE_ORDER, RULE_TYPE_DATA, RULE_TYPE_ESCALATION]

ROUTE_IMPROVE_AGENT = "improve_agent"
ROUTE_NOTIFY_OPS = "notify_ops"
ROUTE_POLICY_REVIEW = "policy_review"
ROUTE_MONITOR = "monitor"
TRIAGE_ROUTES = [ROUTE_IMPROVE_AGENT, ROUTE_NOTIFY_OPS, ROUTE_POLICY_REVIEW, ROUTE_MONITOR]

# Output of the rules engine (Audit 1) -> read by licence engine, dashboard,
# explanations and triage.
FLAG_COLUMNS = [
    "flag_id",        # FLG-0001
    "case_id",
    "process",
    "rule_id",        # matches audit/config.yaml
    "rule_type",      # see RULE_TYPES
    "severity",       # see SEVERITIES
    "agent_name",     # agent held responsible
    "licence_class",
    "timestamp",      # time of the offending event
    "evidence",       # short machine-written fact, e.g. "read weekly, payslip says fortnightly"
    "route",          # default triage route from the rule
]

# Output of the licence engine (Audit 1) -> read by licence board and chart.
LICENCE_STATE_COLUMNS = [
    "agent_name", "licence_class", "level", "trust_score", "model_version",
    "last_change_reason", "updated_at",
]
SCORE_HISTORY_COLUMNS = [
    "timestamp", "agent_name", "licence_class", "case_id", "score_event",
    "delta", "trust_score", "level", "reason",
]

# ---------------------------------------------------------------------------
# Outcomes, decisions, case status
# ---------------------------------------------------------------------------

OUTCOMES = ["completed", "failed", "escalated", "approved", "declined", "overridden"]
DECISIONS = ["approved", "declined", "referred", "released", "blocked"]

CASE_STATUS_IN_PROGRESS = "in_progress"
CASE_STATUS_HELD = "held_for_review"
CASE_STATUS_CLEARED = "cleared"
CASE_STATUSES = [CASE_STATUS_IN_PROGRESS, CASE_STATUS_HELD, CASE_STATUS_CLEARED]

# Customer tracker stages (Agent 2), visa-style.
TRACKER_STAGES = {
    PROCESS_LOAN: ["Received", "Checking your details", "Assessing", "Decision made"],
    PROCESS_TRANSFER: ["Received", "Security checks", "Sending", "Sent"],
}
TRACKER_HUMAN_BADGE = "✓ Reviewed by a person"

# Pay frequencies and periods per year (income annualisation).
PAY_FREQUENCY_PERIODS = {"weekly": 52, "fortnightly": 26, "monthly": 12}

# ---------------------------------------------------------------------------
# Demo anchors
# ---------------------------------------------------------------------------

HERO_CASE = "LOAN-0023"        # Priya: fortnightly pay read as weekly
SUPPORTING_CASE = "PAY-0047"   # Jordan: scam check skipped

# Fault codes the generator may plant (gt_fault values).
FAULT_PAY_FREQUENCY_MISREAD = "pay_frequency_misread"
FAULT_SKIPPED_SCAM_CHECK = "skipped_scam_check"
FAULT_SKIPPED_POLICY_CHECK = "skipped_policy_check"
FAULT_OUT_OF_ORDER = "out_of_order"
FAULT_MISSED_ESCALATION = "missed_escalation"
FAULTS = [
    FAULT_PAY_FREQUENCY_MISREAD,
    FAULT_SKIPPED_SCAM_CHECK,
    FAULT_SKIPPED_POLICY_CHECK,
    FAULT_OUT_OF_ORDER,
    FAULT_MISSED_ESCALATION,
]

TIMEZONE = "Australia/Brisbane"
