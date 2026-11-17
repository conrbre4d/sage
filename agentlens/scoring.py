"""Trust score, licence level and promotion rules. Pure functions on plain dicts.

  Per mistake:  MINOR -1 · MAJOR -2 · CRITICAL -10 (score kept between 0 and 100)
  Any CRITICAL -> instantly back to L-plates (a person checks every case).
  mistake_rate = weighted mistakes / cases handled (last 50 cases)
                 weights: MINOR 0.5 · MAJOR 1 · CRITICAL 2
     rate > 0.30 -> L (L-plates) · > 0.15 -> P1 (Red P) · > 0.05 -> P2 (Green P) · else FULL
  DEMOTION is automatic (the level only ever moves down on its own).
  PROMOTION is never automatic: a named person approves it, and only after
     30 / 20 / 15 cases at L / P1 / P2 (10 when fast-tracked after a verified fix)
     with no CRITICAL mistake since the last level change.

With these weights the demo script plays out as written: 7 correct cases then
3 identical MAJOR mistakes gives 3 / 10 = 0.30 -> P1.
"""

from __future__ import annotations

LEVELS = ["L", "P1", "P2", "FULL"]
LEVEL_NAME = {"L": "L-plates", "P1": "Red P", "P2": "Green P", "FULL": "Full licence"}
POINTS = {"MINOR": -1, "MAJOR": -2, "CRITICAL": -10}
WEIGHT = {"MINOR": 0.5, "MAJOR": 1.0, "CRITICAL": 2.0}
WINDOW = 50
PROMOTE_AFTER = {"L": 30, "P1": 20, "P2": 15}
FAST_TRACK_CASES = 10
# Share of an agent's cases a person must check at each level (shown on the dashboard)
HUMAN_REVIEW_RATE = {"L": 1.0, "P1": 0.5, "P2": 0.1, "FULL": 0.0}


def normalise_level(level) -> str:
    """Older data may still say SUSPENDED; that level no longer exists, so read it as L-plates."""
    return level if level in LEVELS else "L"


def new_licence(agent: str) -> dict:
    return {"agent_name": agent, "trust_score": 100, "level": "FULL", "cases_at_level": 0,
            "last_level_change": None, "risk_owner": "unassigned", "fast_track": False, "recent_cases": []}


def level_for_rate(rate: float) -> str:
    if rate > 0.30:
        return "L"
    if rate > 0.15:
        return "P1"
    if rate > 0.05:
        return "P2"
    return "FULL"


def record_case(lic: dict, case_id: str) -> None:
    """The agent handled a case (a fresh run of it) at its current level."""
    recent = [c for c in lic.get("recent_cases") or [] if c != case_id] + [case_id]
    lic["recent_cases"] = recent[-WINDOW:]
    lic["cases_at_level"] = int(lic.get("cases_at_level") or 0) + 1


def mistake_rate(lic: dict, mistakes: list[dict]) -> float:
    """Weighted open (unresolved) mistakes in the agent's last 50 cases / those cases."""
    recent = set(lic.get("recent_cases") or [])
    if not recent:
        return 0.0
    weight = sum(WEIGHT[m["mistake_type"]] for m in mistakes
                 if m["agent_name"] == lic["agent_name"] and m["case_id"] in recent and not m.get("resolved")
                 and not str(m.get("pattern_key", "")).startswith("repeat:"))
    return round(weight / len(recent), 4)


def _history(lic: dict, change: int, reason: str, case_id=None, event_id=None) -> dict:
    return {"agent_name": lic["agent_name"], "case_id": case_id, "event_id": event_id, "score_change": change,
            "reason": reason, "new_score": lic["trust_score"], "new_level": lic["level"]}


def _set_level(lic: dict, level: str, now: str) -> None:
    lic["level"] = level
    lic["cases_at_level"] = 0
    lic["last_level_change"] = now
    if level == "L":
        lic["fast_track"] = False


def apply_mistake(lic: dict, m: dict, now: str) -> list[dict]:
    """Score one mistake. Returns licence_history rows."""
    change = POINTS[m["mistake_type"]]
    before = lic["trust_score"]
    lic["trust_score"] = max(0, min(100, before + change))
    rows = [_history(lic, lic["trust_score"] - before,
                     f"{m['mistake_type']} mistake: {m['details'].get('title', m['rule'])}", m["case_id"], m.get("event_id"))]
    if m["mistake_type"] == "CRITICAL":
        old = lic["level"]
        _set_level(lic, "L", now)  # also restarts the case count if already on L-plates
        rows.append(_history(lic, 0, f"CRITICAL mistake -> instantly back to L-plates from {LEVEL_NAME[old]} "
                                     f"({m['details'].get('title')})", m["case_id"], m.get("event_id")))
    return rows


def demote_by_rate(lic: dict, rate: float, now: str, case_id=None) -> dict | None:
    """Automatic demotion only: move down if the mistake rate calls for a lower level."""
    target = level_for_rate(rate)
    if LEVELS.index(target) < LEVELS.index(lic["level"]):
        old = lic["level"]
        _set_level(lic, target, now)
        return _history(lic, 0, f"mistake rate {rate:.0%} -> demoted {LEVEL_NAME[old]} to {LEVEL_NAME[target]}",
                        case_id)
    return None


def cases_needed(lic: dict) -> int:
    if lic["level"] == "FULL":
        return 0
    return FAST_TRACK_CASES if lic.get("fast_track") else PROMOTE_AFTER[lic["level"]]


def promotion_check(lic: dict, criticals_since_change: int) -> tuple[bool, str]:
    if lic["level"] == "FULL":
        return False, "already on a full licence"
    if criticals_since_change:
        return False, f"{criticals_since_change} CRITICAL mistake(s) since the last level change"
    need = cases_needed(lic)
    if (lic.get("cases_at_level") or 0) < need:
        return False, f"needs {need} cases at {LEVEL_NAME[lic['level']]} (has {lic.get('cases_at_level') or 0})"
    return True, "ok"


def promote(lic: dict, approved_by: str, now: str) -> dict:
    old = lic["level"]
    new = LEVELS[LEVELS.index(old) + 1]
    _set_level(lic, new, now)
    lic["fast_track"] = False
    return _history(lic, 0, f"promoted {LEVEL_NAME[old]} to {LEVEL_NAME[new]}, approved by {approved_by}")


def demote_to(lic: dict, level: str, reason: str, now: str) -> dict:
    _set_level(lic, level, now)
    return _history(lic, 0, reason)
