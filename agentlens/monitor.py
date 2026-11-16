"""The AgentLens engine: scan new events -> mistakes -> scores/licences -> holds -> suggestions.
Plus the human actions: apply / verify a fix (replay), promote an agent, review a held case.

AgentLens only READS event_log (and application status) and WRITES its own tables. It
touches the agents only through replay(), which re-runs cases through the normal pipeline.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta

import _paths  # noqa: F401
import access
import db
import detect
import faults
import improve
import main as pipeline
import scoring
import store

REPEAT_CRITICAL_AT = 4  # rule 5. The spec says 3+, but its demo needs 3 repeats -> P1 + a suggestion,
#                         so a 4th repeat (after the pattern was flagged) is what turns CRITICAL.
HOLD_SEVERITIES = {"MAJOR", "CRITICAL"}

_scan_lock = threading.RLock()
_case_events: dict[str, list[dict]] = {}

now_iso = access.now_iso


def _ts(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def reset_memory() -> None:
    with _scan_lock:
        _case_events.clear()
    store.reset_cache()


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan() -> dict:
    """Check every event written since the last scan. Returns what was found."""
    with _scan_lock:
        events = db.get_events_after(store.cursor())
        if not events:
            return {"events": 0, "mistakes": [], "held": []}
        lic = store.licences()
        touched: set[str] = set()
        found: list[dict] = []

        by_case: dict[str, list[dict]] = defaultdict(list)
        for e in events:
            by_case[e["case_id"]].append(e)
        for case_id, evs in by_case.items():
            history = _case_events.get(case_id)
            if history is None:
                history = [x for x in db.get_events(case_id) if x["event_id"] < evs[0]["event_id"]]
            for e in evs:
                if e["step_name"] == "access.issue_ticket":
                    agent = (e.get("input_data") or {}).get("agent")
                    if agent == "intake_agent":
                        history = []  # a new run of this case starts here
                    if agent in lic:
                        scoring.record_case(lic[agent], case_id)
                        touched.add(agent)
                for m in detect.check_event(e, history):
                    m["timestamp"] = now_iso()
                    found.append(m)
                history.append(e)
            _case_events[case_id] = history

        # Rule 5: the same wrong conclusion on 4+ different cases within 24 h
        cutoff = datetime.now().astimezone() - timedelta(hours=24)
        earlier = [m for m in store.mistakes() if not m.get("resolved") and (_ts(m.get("timestamp")) or cutoff) >= cutoff]
        extra = []
        for i, m in enumerate(found):
            same = {x["case_id"] for x in earlier + found[:i]
                    if x["agent_name"] == m["agent_name"] and x["pattern_key"] == m["pattern_key"]}
            r = detect.repeated_mistake(m, same, REPEAT_CRITICAL_AT)
            if r:
                extra.append(r)
        saved = store.add_mistakes(found + extra) if found or extra else []

        hist = []
        for m in saved:
            hist += scoring.apply_mistake(lic[m["agent_name"]], m, now_iso())
            touched.add(m["agent_name"])
        for agent in touched:
            rate = scoring.mistake_rate(lic[agent], store.mistakes())
            hist.append(scoring.demote_by_rate(lic[agent], rate, now_iso()))
        if touched:
            store.save_licences(sorted(touched))
        store.add_history(hist)

        held = []
        for case_id in sorted({m["case_id"] for m in saved if m["mistake_type"] in HOLD_SEVERITIES}):
            app = db.get_application(case_id)
            if app and app["status"] != "held":
                worst = sorted((m for m in saved if m["case_id"] == case_id),
                               key=lambda m: ["CRITICAL", "MAJOR", "MINOR"].index(m["mistake_type"]))[0]
                access.place_hold(case_id, f"AgentLens: {worst['details']['title']} ({worst['mistake_type']})")
                held.append(case_id)

        new_sugs = [store.add_suggestion(p) for p in improve.proposals(store.mistakes(), store.suggestions())]
        store.set_cursor(events[-1]["event_id"])
        return {"events": len(events), "mistakes": saved, "held": held, "suggestions": new_sugs}


def after_agent(case_id: str, agent_name: str) -> None:
    """Pipeline hook: check the case after the verification and decision agents (before the customer hears)."""
    if agent_name in ("verification_agent", "decision_agent"):
        scan()


def process(case_id: str, fresh: bool = False) -> dict:
    """Run one case through the pipeline with AgentLens watching."""
    out = pipeline.process_case(case_id, monitor=after_agent, fresh=fresh, with_events=False)
    scan()
    return out


# ---------------------------------------------------------------------------
# Improvement loop
# ---------------------------------------------------------------------------

def sync_fix_levels() -> None:
    """Restore the agents' fixed logic from applied suggestions (after a restart)."""
    levels: dict = {}
    for s in store.suggestions():
        if s["status"] in ("testing", "applied", "resolved"):
            k = (s["agent_name"], s["pattern_key"])
            levels[k] = levels.get(k, 0) + 1
    faults.set_fix_levels(levels)


def apply_suggestion(sid: int, approved_by: str, fix_description: str) -> dict:
    s = store.get_suggestion(sid)
    if s is None:
        raise KeyError(f"no suggestion {sid}")
    if s["status"] != "proposed":
        raise ValueError(f"suggestion {sid} is '{s['status']}', only a 'proposed' one can be applied")
    if not approved_by.strip():
        raise ValueError("approved_by is required")
    store.update_suggestion(sid, {"status": "testing", "applied_by": approved_by,
                                  "fix_description": fix_description or s["suggested_fix"]})
    lic = store.licences()[s["agent_name"]]
    h = scoring.demote_to(lic, "L", f"fix #{sid} applied (approved by {approved_by}): back to L-plates for re-testing",
                          now_iso())
    faults.apply_fix(s["agent_name"], s["pattern_key"])
    store.save_licences([s["agent_name"]])
    store.add_history([h])
    return store.get_suggestion(sid)


def replay(agent: str, case_ids: list[str]) -> dict[str, list[dict]]:
    """Re-run cases through the pipeline (with the agent's current, fixed logic).
    Returns the new mistakes this agent made on each case."""
    before = max([int(m["id"]) for m in store.mistakes()] or [0])
    for case_id in case_ids:
        app = db.get_application(case_id)
        if app is None:
            continue
        if app["status"] == "held":
            access.human_review(case_id, "AgentLens replay", "submitted", "re-run with the fixed agent")
        process(case_id)
    scan()
    new = [m for m in store.mistakes() if int(m["id"]) > before and m["agent_name"] == agent]
    return {c: [m for m in new if m["case_id"] == c] for c in case_ids}


def verify_suggestion(sid: int) -> dict:
    s = store.get_suggestion(sid)
    if s is None:
        raise KeyError(f"no suggestion {sid}")
    if s["status"] != "testing":
        raise ValueError(f"suggestion {sid} is '{s['status']}': apply it first")
    results = replay(s["agent_name"], list(s["case_ids"]))
    failed = [c for c, ms in results.items() if any(m["pattern_key"] == s["pattern_key"] for m in ms)]
    passed = [c for c in results if c not in failed]
    verdict = {"passed": passed, "failed": failed, "replayed": len(results), "at": now_iso()}
    lic = store.licences()[s["agent_name"]]
    if not failed:
        store.update_suggestion(sid, {"status": "resolved", "verify_result": verdict})
        n = store.resolve_pattern(s["agent_name"], s["pattern_key"])
        lic["fast_track"] = True
        store.save_licences([s["agent_name"]])
        store.add_history([{"agent_name": s["agent_name"], "case_id": None, "event_id": None, "score_change": 0,
                            "reason": f"fix #{sid} verified: all {len(passed)} replayed cases pass; {n} mistakes "
                                      f"resolved; fast-tracked ({scoring.FAST_TRACK_CASES} cases to the next level)",
                            "new_score": lic["trust_score"], "new_level": lic["level"]}])
        return {"suggestion": store.get_suggestion(sid), "result": "resolved", **verdict}
    store.update_suggestion(sid, {"status": "applied", "verify_result": verdict})
    pattern_mistakes = [m for m in store.mistakes() if m["agent_name"] == s["agent_name"]
                        and m["pattern_key"] == s["pattern_key"]]
    failed_mistakes = [m for c in failed for m in results[c] if m["pattern_key"] == s["pattern_key"]]
    nxt = improve.next_iteration(s, s["case_ids"], failed_mistakes, pattern_mistakes)
    nxt["root_cause"] = f"{nxt['root_cause']} Still failing after fix #{sid}: {', '.join(failed)}."
    new = store.add_suggestion(nxt)
    return {"suggestion": store.get_suggestion(sid), "result": "iterate", "next_suggestion": new, **verdict}


# ---------------------------------------------------------------------------
# Licences
# ---------------------------------------------------------------------------

def criticals_since_change(agent: str) -> int:
    lic = store.licences()[agent]
    since = _ts(lic.get("last_level_change")) if lic.get("last_level_change") else None
    return sum(1 for m in store.mistakes() if m["agent_name"] == agent and m["mistake_type"] == "CRITICAL"
               and (since is None or (_ts(m.get("timestamp")) or since) > since))


def promote_agent(agent: str, approved_by: str) -> dict:
    lic = store.licences().get(agent)
    if lic is None:
        raise KeyError(f"unknown agent {agent}")
    if not (approved_by or "").strip():
        raise ValueError("promotion needs a named person: approved_by")
    ok, reason = scoring.promotion_check(lic, criticals_since_change(agent))
    if not ok:
        raise ValueError(f"can't promote {agent}: {reason}")
    h = scoring.promote(lic, approved_by, now_iso())
    store.save_licences([agent])
    store.add_history([h])
    return lic


def rank() -> list[dict]:
    lic = store.licences()
    ms = store.mistakes()
    rows = []
    for a, l in lic.items():
        rows.append({**l, "level_name": scoring.LEVEL_NAME[l["level"]],
                     "mistake_rate": scoring.mistake_rate(l, ms),
                     "human_review_rate": scoring.HUMAN_REVIEW_RATE[l["level"]],
                     "cases_needed_to_promote": scoring.cases_needed(l),
                     "open_mistakes": sum(1 for m in ms if m["agent_name"] == a and not m.get("resolved"))})
    return sorted(rows, key=lambda r: (scoring.LEVELS.index(r["level"]), r["trust_score"]))


# ---------------------------------------------------------------------------
# A person reviews a held case
# ---------------------------------------------------------------------------

def review_case(case_id: str, action: str, reviewer: str) -> dict:
    app = db.get_application(case_id)
    if app is None:
        raise KeyError(f"no application {case_id}")
    if app["status"] != "held":
        raise ValueError(f"{case_id} is '{app['status']}', not held")
    if action == "recalculate":
        faults.mark_fixed(case_id)  # the agent redoes it properly
        access.human_review(case_id, reviewer, "submitted", "sent back to recalculate")
        return process(case_id)
    status = {"approve": "approved", "decline": "declined"}.get(action)
    if status is None:
        raise ValueError("action must be recalculate, approve or decline")
    access.human_review(case_id, reviewer, status, f"{action} after AgentLens hold")
    db.update_application(case_id, {"decision": "approved" if status == "approved" else "declined",
                                    "decided_at": now_iso()})
    return {"case_id": case_id, "status": status}
