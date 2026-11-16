"""Validate AgentLens event logs and the audit config.

Run before you hand data to anyone else:

    python -m shared.validate                       # sample data + audit config
    python -m shared.validate path/to/events.csv    # your own log
    python -m shared.validate --config audit/config.yaml

Exit code is 1 if there are errors (warnings don't fail).

You can also import it:

    from shared.validate import load_events, validate_events
    df = load_events("data/sample/events.csv")
    issues = validate_events(df)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml

from shared import constants as C

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVENTS = ROOT / "data" / "sample" / "events.csv"
DEFAULT_CONFIG = ROOT / "audit" / "config.yaml"

# Simple patterns for personal data that must never be in the log.
PII_PATTERNS = {
    "email address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone number": re.compile(r"(?:\+?61|0)[2-478](?:[ -]?\d){8}"),
    "card number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "TFN-like number": re.compile(r"\b\d{3}[ -]?\d{3}[ -]?\d{3}\b"),
    "BSB/account": re.compile(r"\b\d{3}-\d{3}\b"),
}


@dataclass
class Issue:
    level: str      # "error" or "warning"
    where: str      # e.g. "row 12" or "rule DAT-01"
    message: str

    def __str__(self) -> str:
        return f"[{self.level.upper():7}] {self.where}: {self.message}"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_events(path: str | Path) -> pd.DataFrame:
    """Read an event log CSV with the right types.

    timestamp -> tz-aware datetime, numeric columns -> float,
    payee_is_new -> nullable boolean, everything else -> string ("" if blank).
    Missing optional columns are added as blanks so downstream code can rely on
    every column in EVENT_COLUMNS existing.
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return coerce_types(df)


def coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in C.EVENT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    for col in C.NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col].replace("", None), errors="coerce")
    df["payee_is_new"] = (
        df["payee_is_new"].astype(str).str.strip().str.lower()
        .map({"true": True, "false": False, "1": True, "0": False})
        .astype("boolean")
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True).dt.tz_convert(C.TIMEZONE)
    return df


# ---------------------------------------------------------------------------
# Event log checks
# ---------------------------------------------------------------------------

def validate_events(df: pd.DataFrame) -> list[Issue]:
    """Check an event log against the shared schema. Accepts raw or coerced frames."""
    issues: list[Issue] = []

    def err(where, msg):
        issues.append(Issue("error", where, msg))

    def warn(where, msg):
        issues.append(Issue("warning", where, msg))

    missing = [c for c in C.REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        err("columns", f"missing required columns: {missing}")
        return issues
    extra = [c for c in df.columns if c not in C.EVENT_COLUMNS]
    if extra:
        warn("columns", f"columns not in shared schema (ignored by others): {extra}")
    for col in C.EVENT_COLUMNS:
        if col not in df.columns:
            warn("columns", f"optional column '{col}' is missing")

    raw = df.astype(str).replace({"nan": "", "<NA>": "", "NaT": "", "None": ""})

    def col(name):
        return raw[name] if name in raw.columns else pd.Series([""] * len(raw), index=raw.index)

    # IDs
    dup = raw["event_id"][raw["event_id"].duplicated()]
    for eid in dup.unique():
        err("event_id", f"duplicate event_id {eid}")
    for i, eid in raw["event_id"].items():
        if not re.match(C.EVENT_ID_PATTERN, eid):
            err(f"row {i}", f"event_id '{eid}' should look like EVT-000001")

    timestamps = pd.to_datetime(raw["timestamp"], errors="coerce", utc=True)

    for i, r in raw.iterrows():
        where = f"row {i} ({r['event_id']})"
        proc, act, actor, agent = r["process"], r["activity"], r["actor_type"], r["agent_name"]

        if not re.match(C.CASE_ID_PATTERN, r["case_id"]):
            err(where, f"case_id '{r['case_id']}' should look like LOAN-0023 or PAY-0047")
        if proc not in C.PROCESSES:
            err(where, f"unknown process '{proc}' (expected one of {C.PROCESSES})")
        else:
            if not r["case_id"].startswith(C.CASE_ID_PREFIX[proc] + "-"):
                err(where, f"case_id '{r['case_id']}' does not match process '{proc}'")
            if act not in C.ACTIVITIES[proc]:
                err(where, f"unknown activity '{act}' for process '{proc}'")

        if pd.isna(timestamps[i]):
            err(where, f"timestamp '{r['timestamp']}' is not ISO 8601")
        elif not re.search(r"(Z|[+-]\d{2}:?\d{2})$", r["timestamp"]):
            warn(where, "timestamp has no timezone offset; assuming UTC")

        if actor not in C.ACTOR_TYPES:
            err(where, f"unknown actor_type '{actor}'")
        if actor == C.ACTOR_AGENT:
            if agent not in C.AGENTS:
                err(where, f"unknown agent_name '{agent}' (expected one of {C.AGENTS})")
            lc = r.get("licence_class", "")
            if not lc:
                err(where, "agent rows must have a licence_class")
            elif lc not in C.LICENCE_CLASSES:
                err(where, f"unknown licence_class '{lc}'")
            elif agent in C.AGENT_LICENCE_CLASSES and lc not in C.AGENT_LICENCE_CLASSES[agent]:
                err(where, f"{agent} cannot hold licence_class '{lc}'")
            if not r.get("model_version", ""):
                warn(where, "agent row has no model_version")
        elif actor in (C.ACTOR_HUMAN, C.ACTOR_SYSTEM):
            expected = C.NON_AGENT_ACTORS.get(agent)
            if expected is None:
                warn(where, f"unrecognised {actor} actor '{agent}'")
            elif expected != actor:
                err(where, f"'{agent}' should have actor_type '{expected}'")
            if r.get("licence_class", ""):
                warn(where, "licence_class is only meaningful on agent rows")

        if not re.match(C.CUSTOMER_REF_PATTERN, r["customer_ref"]):
            err(where, f"customer_ref '{r['customer_ref']}' is not pseudonymised (CUST-xxxxxxxx)")
        if r["outcome"] not in C.OUTCOMES:
            err(where, f"unknown outcome '{r['outcome']}'")
        dec = r.get("decision", "")
        if dec and dec not in C.DECISIONS:
            err(where, f"unknown decision '{dec}'")
        for fcol in ("doc_pay_frequency", "read_pay_frequency"):
            v = r.get(fcol, "")
            if v and v not in C.PAY_FREQUENCY_PERIODS:
                err(where, f"{fcol} '{v}' not in {list(C.PAY_FREQUENCY_PERIODS)}")
        flt = r.get(C.GROUND_TRUTH_COLUMN, "")
        if flt and flt not in C.FAULTS:
            err(where, f"unknown gt_fault '{flt}'")
        pn = r.get("payee_is_new", "")
        if pn and pn.lower() not in ("true", "false", "1", "0"):
            err(where, f"payee_is_new '{pn}' must be True/False")

        for name, pat in PII_PATTERNS.items():
            for text_col in ("note",):
                if pat.search(r.get(text_col, "")):
                    err(where, f"{text_col} looks like it contains a {name}; redact it")

    # Numeric columns
    for ncol in C.NUMERIC_COLUMNS:
        if ncol not in raw.columns:
            continue
        vals = raw[ncol]
        nums = pd.to_numeric(vals.replace("", None), errors="coerce")
        for i in vals.index[(vals != "") & nums.isna()]:
            err(f"row {i}", f"{ncol} '{vals[i]}' is not a number")
        if ncol == "scam_score":
            for i in nums.index[(nums < 0) | (nums > 1)]:
                err(f"row {i}", f"scam_score {nums[i]} must be between 0 and 1")
        elif (nums < 0).any():
            for i in nums.index[nums < 0]:
                err(f"row {i}", f"{ncol} {nums[i]} must not be negative")

    # Per-case checks
    work = raw.assign(_ts=timestamps)
    for case_id, g in work.groupby("case_id", sort=False):
        where = f"case {case_id}"
        if g["process"].nunique() > 1:
            err(where, "case has events from more than one process")
        if g["customer_ref"].nunique() > 1:
            err(where, "case has more than one customer_ref")
        ordered = g.sort_values("_ts", kind="stable")
        if ordered.iloc[0]["activity"] != "Receive":
            warn(where, f"first event is '{ordered.iloc[0]['activity']}', expected 'Receive'")
        if not g["_ts"].is_monotonic_increasing:
            warn(where, "events are not in time order in the file (sort by case_id, timestamp)")

    return issues


# ---------------------------------------------------------------------------
# Config checks
# ---------------------------------------------------------------------------

def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _fields_in(obj) -> set[str]:
    """Collect every column name referenced inside a rule's check block."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("field", "equals_field", "gross", "frequency", "result") and isinstance(v, str):
                found.add(v)
            elif k == "ratio" and isinstance(v, list):
                found.update(x for x in v if isinstance(x, str))
            else:
                found |= _fields_in(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _fields_in(v)
    return found


def _activities_in(obj) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("activity", "requires_before", "target") and isinstance(v, str):
                found.add(v)
            else:
                found |= _activities_in(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _activities_in(v)
    return found


def validate_config(cfg: dict) -> list[Issue]:
    issues: list[Issue] = []

    def err(where, msg):
        issues.append(Issue("error", where, msg))

    rules = cfg.get("rules") or []
    if not rules:
        err("rules", "no rules defined")
    ids = [r.get("id") for r in rules]
    for rid in {x for x in ids if ids.count(x) > 1}:
        err("rules", f"duplicate rule id {rid}")

    for r in rules:
        where = f"rule {r.get('id', '?')}"
        for key in ("id", "mistake", "type", "process", "description", "check", "severity", "responsible", "route"):
            if key not in r:
                err(where, f"missing '{key}'")
        on = (r.get("check") or {}).get("on")
        if on is not None and on not in ("last", "earlier", "any"):
            err(where, f"check.on must be last, earlier or any (got '{on}')")
        if r.get("type") not in C.RULE_TYPES:
            err(where, f"unknown type '{r.get('type')}'")
        proc = r.get("process")
        if proc not in C.PROCESSES:
            err(where, f"unknown process '{proc}'")
            continue
        if r.get("severity") not in C.SEVERITIES:
            err(where, f"unknown severity '{r.get('severity')}'")
        if r.get("route") not in C.TRIAGE_ROUTES:
            err(where, f"unknown route '{r.get('route')}'")
        if r.get("responsible") not in C.AGENTS:
            err(where, f"unknown responsible agent '{r.get('responsible')}'")
        for act in _activities_in(r.get("check")):
            if act not in C.ACTIVITIES[proc]:
                err(where, f"activity '{act}' does not exist in process '{proc}'")
        for fld in _fields_in(r.get("check")):
            if fld not in C.EVENT_COLUMNS:
                err(where, f"field '{fld}' is not an event log column")

    ts = cfg.get("trust_score", {})
    deltas = ts.get("deltas", {})
    if set(deltas) != set(C.SCORE_EVENTS):
        err("trust_score.deltas", f"keys must be exactly {C.SCORE_EVENTS}")

    lic = cfg.get("licence", {})
    mins = lic.get("level_min_score", {})
    if list(mins) != C.LICENCE_LEVELS:
        err("licence.level_min_score", f"levels must be, in order, {C.LICENCE_LEVELS}")
    elif any(a >= b for a, b in zip(list(mins.values()), list(mins.values())[1:])):
        err("licence.level_min_score", "scores must increase with each level")
    for key in ("human_review_rate",):
        if set(lic.get(key, {})) != set(C.LICENCE_LEVELS):
            err(f"licence.{key}", f"must have every level: {C.LICENCE_LEVELS}")
    for lvl in lic.get("promotion", {}):
        if lvl not in C.LICENCE_LEVELS:
            err("licence.promotion", f"unknown level '{lvl}'")
    dem = lic.get("demotion", {})
    for key in ("on_critical", "on_model_update"):
        if dem.get(key) not in C.LICENCE_LEVELS:
            err(f"licence.demotion.{key}", f"'{dem.get(key)}' is not a level")
    if dem.get("repeat_mistake", {}).get("cap_level") not in C.LICENCE_LEVELS:
        err("licence.demotion.repeat_mistake", "cap_level is not a level")

    for sev in cfg.get("circuit_breaker", {}).get("hold_on_severity", []):
        if sev not in C.SEVERITIES:
            err("circuit_breaker", f"unknown severity '{sev}'")
    for route in cfg.get("triage", {}).get("routes", []):
        if route not in C.TRIAGE_ROUTES:
            err("triage.routes", f"unknown route '{route}'")
    priv = cfg.get("privacy", {})
    for key in ("llm_allowed_columns", "llm_bucketed_columns", "never_send_to_llm"):
        for c in priv.get(key, []):
            if c not in C.EVENT_COLUMNS:
                err(f"privacy.{key}", f"'{c}' is not an event log column")
    leak = set(priv.get("llm_allowed_columns", [])) & set(priv.get("never_send_to_llm", []))
    if leak:
        err("privacy", f"columns both allowed and forbidden for the LLM: {sorted(leak)}")

    return issues


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report(label: str, issues: list[Issue]) -> int:
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    status = "OK" if not errors else "FAILED"
    print(f"{label}: {status} ({len(errors)} errors, {len(warnings)} warnings)")
    for i in errors + warnings:
        print("  ", i)
    return len(errors)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Validate AgentLens event logs and config.")
    p.add_argument("events", nargs="*", help=f"event log CSV(s) (default: {DEFAULT_EVENTS.relative_to(ROOT)})")
    p.add_argument("--config", help=f"audit config YAML (default: {DEFAULT_CONFIG.relative_to(ROOT)})")
    args = p.parse_args(argv)

    if not args.events and not args.config:
        args.events = [str(DEFAULT_EVENTS)]
        args.config = str(DEFAULT_CONFIG)

    n_errors = 0
    for path in args.events:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        cases = df["case_id"].nunique() if "case_id" in df else 0
        n_errors += _report(f"{path} [{len(df)} events, {cases} cases]", validate_events(df))
    if args.config:
        n_errors += _report(args.config, validate_config(load_config(args.config)))
    return 1 if n_errors else 0


if __name__ == "__main__":
    sys.exit(main())
