"""Pattern detection, fix suggestions, and the apply -> verify -> iterate loop.

Step 3: group open mistakes by agent + pattern_key (same field / same cause). 3+ different
cases -> an improvement_suggestion from a template:
    misreads field X          -> Require X as an explicit structured field, reject output missing it
    skips required step       -> Add a hard dependency check in the orchestrator
    ignores failed check      -> Make this agent fail-closed on any upstream fail
    inconsistent on edge case -> Add labelled examples for this case to the agent's prompt

Step 4: apply (a named person) -> status 'testing', agent back to L-plates, cases_at_level 0.
verify -> replay every case the pattern failed on, with the fixed agent logic:
    all pass  -> 'resolved', agent fast-tracked (10 cases instead of 30), mistakes for
                 the pattern marked resolved (so they leave the mistake rate)
    some fail -> iteration + 1: the suggestion becomes 'applied' (it helped, but not
                 enough) and a NEW suggestion is created for the cases still failing.
"""

from __future__ import annotations

PATTERN_THRESHOLD = 3
ACTIVE = {"proposed", "testing"}

FIELD_LABEL = {"pay_frequency": "pay_frequency (pay period)", "debts": "debts", "enquiries": "credit enquiries",
               "defaults": "defaults", "dti": "DTI", "eligibility": "eligibility", "policy": "policy"}

TEMPLATES = {
    "misreads_field": ("misreads field {x}",
                       "The agent reads {x} from free text and sometimes gets it wrong, with nothing checking it.",
                       "Require {x} as an explicit structured field, reject output missing it"),
    "skips_step": ("skips required step {x}",
                   "Nothing stops the agent going ahead before {x} has run.",
                   "Add a hard dependency check in the orchestrator"),
    "ignores_failed_check": ("ignores failed upstream checks",
                             "The agent can reach 'approved' even when an earlier check failed.",
                             "Make this agent fail-closed on any upstream fail"),
    "edge_case": ("inconsistent on edge case: {x}",
                  "The agent handles {x} differently from the normal case.",
                  "Add labelled examples for {x} to the agent's prompt"),
}


def template_for(pattern_key: str, edge: str | None = None) -> dict | None:
    kind, _, x = pattern_key.partition(":")
    if edge:
        kind, x = "edge_case", edge
    if kind not in TEMPLATES:
        return None
    desc, cause, fix = TEMPLATES[kind]
    x = FIELD_LABEL.get(x, x.replace("_", " "))
    return {"pattern_description": desc.format(x=x), "root_cause": cause.format(x=x), "suggested_fix": fix.format(x=x)}


def proposals(mistakes: list[dict], suggestions: list[dict]) -> list[dict]:
    """New suggestions for patterns seen on 3+ different cases that have no active suggestion."""
    groups: dict[tuple[str, str], set[str]] = {}
    for m in mistakes:
        if m.get("resolved") or str(m.get("pattern_key", "")).startswith("repeat:"):
            continue
        groups.setdefault((m["agent_name"], m["pattern_key"]), set()).add(m["case_id"])
    out = []
    for (agent, key), cases in sorted(groups.items()):
        if len(cases) < PATTERN_THRESHOLD:
            continue
        if any(s["agent_name"] == agent and s["pattern_key"] == key and s["status"] in ACTIVE | {"applied"}
               for s in suggestions):
            continue
        t = template_for(key)
        if t is None:
            continue
        out.append({"agent_name": agent, "pattern_key": key, "case_ids": sorted(cases), "iteration": 1,
                    "status": "proposed", **t})
    return out


def edge_attribute(failed_mistakes: list[dict], all_mistakes: list[dict]) -> str | None:
    """What the still-failing cases have in common that the fixed ones didn't (e.g. self-employed)."""
    kinds = {m.get("details", {}).get("document_kind") for m in failed_mistakes}
    others = {m.get("details", {}).get("document_kind") for m in all_mistakes} - kinds
    if len(kinds) == 1 and None not in kinds and others:
        return f"{next(iter(kinds))}s"
    return None


def next_iteration(sug: dict, failed_cases: list[str], failed_mistakes: list[dict], all_mistakes: list[dict]) -> dict:
    edge = edge_attribute(failed_mistakes, all_mistakes)
    t = template_for(sug["pattern_key"], edge) or {k: sug[k] for k in ("pattern_description", "root_cause",
                                                                         "suggested_fix")}
    if not edge:
        t = {**t, "pattern_description": f"{t['pattern_description']} (still failing after iteration {sug['iteration']})"}
    return {"agent_name": sug["agent_name"], "pattern_key": sug["pattern_key"], "case_ids": sorted(failed_cases),
            "iteration": int(sug["iteration"]) + 1, "parent_id": sug["id"], "status": "proposed", **t}
