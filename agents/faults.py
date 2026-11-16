"""Planted failures for the demo: case_id -> fault. Deterministic, independent of the LLM.

  pay_frequency_misread   verification reads fortnightly pay as weekly -> income doubled
  debts_omitted           verification leaves the customer's debts out of the affordability maths
  old_payslip             the payslip is months old and verification skips its recency check
  credit_checks_ignored   verification ignores a high number of recent credit enquiries
  unsure_not_referred     decision approves a borderline case instead of sending it to a person
  approved_despite_fail   decision approves even though a check failed (AgentLens: CRITICAL)
  unredacted_payslip      the payslip reaches the verification AI without redaction (rule DP3)
  cross_case_access       verification asks for ANOTHER customer's credit report (rule DP4)

How faults get fixed:
  - AgentLens improvement loop: applying a suggestion raises the agent's fix level for that
    pattern (apply_fix). A case's fault stops once the level reaches FIX_NEEDED for it, so
    suggestion #1 fixes the employee payslips and suggestion #2 the self-employed one.
  - A reviewer "sends back to recalculate" one held case: mark_fixed(case_id).
"""

FAULT_SWITCH = {
    # demo batch (agentlens/demo_seed.py)
    "LOAN-0023": "pay_frequency_misread",   # John (employee)
    "LOAN-0029": "pay_frequency_misread",   # employee
    "LOAN-0035": "pay_frequency_misread",   # self-employed: fix #1 misses it
    "LOAN-0015": "debts_omitted",
    "LOAN-0031": "unsure_not_referred",
    "LOAN-0039": "credit_checks_ignored",
    "LOAN-0042": "old_payslip",
    "LOAN-0047": "approved_despite_fail",
    # privacy / access demos (run on their own: they suspend the verification agent)
    "LOAN-0026": "unredacted_payslip",
    "LOAN-0027": "cross_case_access",
}

# The fault each AgentLens pattern corresponds to, and the fix level each case needs.
FAULT_PATTERN = {"pay_frequency_misread": ("verification_agent", "misreads_field:pay_frequency")}
FIX_NEEDED = {"LOAN-0035": 2}  # everything else: 1

OTHER_CUSTOMER_TOKEN = "CUST-9B21"  # what the cross_case_access fault asks for

_FIXED: set[str] = set()
_fix_level: dict[tuple[str, str], int] = {}


def fault_for(case_id: str) -> str | None:
    """The active fault for a case (None once it has been fixed)."""
    fault = FAULT_SWITCH.get(case_id)
    if fault is None or case_id in _FIXED:
        return None
    pattern = FAULT_PATTERN.get(fault)
    if pattern and _fix_level.get(pattern, 0) >= FIX_NEEDED.get(case_id, 1):
        return None
    return fault


def mark_fixed(case_id: str) -> None:
    _FIXED.add(case_id)


def apply_fix(agent: str, pattern_key: str) -> int:
    """An improvement was applied to this agent for this pattern. Returns the new fix level."""
    _fix_level[(agent, pattern_key)] = _fix_level.get((agent, pattern_key), 0) + 1
    return _fix_level[(agent, pattern_key)]


def set_fix_levels(levels: dict[tuple[str, str], int]) -> None:
    """AgentLens restores the levels from its improvement_suggestion table on start-up."""
    _fix_level.clear()
    _fix_level.update(levels)


def reset() -> None:
    _FIXED.clear()
    _fix_level.clear()
