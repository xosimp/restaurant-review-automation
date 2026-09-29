"""event_memory — what each night teaches: how events, holidays, weather and
campaigns moved sales here (memory audit 9/29/26: event_memory,
public_history). SKELETON: workstream M5 owns this module and replaces these
bodies; the signatures are the contract.
"""


def measured_effect(restaurant_id, label, db_path=None):
    """This restaurant's measured effect of a recurring label ("football
    sunday", "rain", "1st of month") -> {"median_lift_pct": float, "n": int,
    "last": date} once it has a sample, else None."""
    return None


def night_facts(restaurant_id, day, db_path=None):
    """What is known about one date: [{"kind", "label", "measured_lift_pct",
    "n"}] (events, holidays, influence notes, weather, campaign targets)."""
    return []


def memory_lines(req):
    """memory_context provider: measured event effects for the dates in play."""
    return []
