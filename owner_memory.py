"""owner_memory — what the owner tells Cavnar AI, typed and served to every
model call (memory audit 9/29/26: owner_reach, owner_lanes, owner_goals,
owner_layers, conversations). SKELETON: workstream M2 owns this module and
replaces these bodies; the signatures are the contract other modules call.
"""


def constraint_lines(req):
    """memory_context provider: hard constraints and time-bound owner facts
    for req.surface, as memory lines (see memory_context)."""
    return []


def goal_lines(req):
    """memory_context provider: the active goals for the metrics in play."""
    return []


def conversation_lines(req):
    """memory_context provider (Ask only): the rolling conversation summary."""
    return []


def target_for(restaurant_id, metric, db_path=None):
    """The target a module judges `metric` against when the owner set one
    through a goal — ("labor_pct" | "food_cost_pct" | "prime_cost_pct" |
    "nightly_sales" | ...) -> {"value": float, "source": "goal", "goal_id": int,
    "until": date|None} — or None, and the module keeps its own setting."""
    return None
