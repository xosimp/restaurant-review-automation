"""
schedule_memory.py — what a restaurant's scheduling has learned, as one
compact memory with a confidence, a decay and an enforcement level, and the
observation log it is built from (schedule audit 10/3/26 L-29).

Before this, only the manager's edit habits had a compact store
(schedule_standing_patterns); outcomes, attendance by weekday, drop and
claim preferences, the rotation and mentoring were recomputed raw on every
generation with no confidence, and what was learned reached only the
prompt, so the solver, the optimizer, the budget trim and the scorer could
undo it (L-3).

The contract the rest of the pipeline codes against (the bodies are filled
in by the learning workstream of the fix round):

  observe(...)            one atomic fact, append-only, with its origin
                          (manager, cavnar, staff, system), phase
                          (pre_publish, post_publish, as_run) and authority
                          (principal, delegate, system, admin)
  enforced_signals(...)   the active memories for a week, each
                          {kind, key, person, day, daypart, role, value,
                           confidence 0-1, enforcement 'prompt'|'soft'|'hard',
                           source} — schedule_engine._quality_signals passes
                          them to the scorer, the solver, the optimizer and
                          the trim as signals["learned"]
  prompt_lines(...)       the same memories as budgeted, relevance-ranked
                          prompt lines (one short line for a fact code
                          already enforces)
"""
from models import get_conn, DB_PATH  # noqa: F401  (the learning workstream's tables)


def observe(restaurant_id, kind, *, week_start=None, date=None, daypart=None, role=None, person=None,
            value=None, origin="manager", phase="pre_publish", authority="principal", editor=None,
            source=None, history_id=None, db_path=None):
    """Record one observation. Never raises into the caller's work."""
    return None


def enforced_signals(restaurant_id, week_dates, roster_names=None, db_path=None) -> list:
    """The active memories that bind this week's passes (see the module doc)."""
    return []


def prompt_lines(restaurant_id, week_dates, roster_names=None, budget_chars=1600, db_path=None) -> str:
    """The memories as prompt text, inside `budget_chars`."""
    return ""
