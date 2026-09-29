"""learning_memory — the nightly pass that turns what Cavnar AI said and what
followed into what it knows (memory audit 9/29/26, workstream M4).

One restaurant at a time (scheduler.run_learning_memory sweeps them, bounded
and resumable), each step isolated so one failing never stops the others:

  claims       score every claim whose horizon has passed (ai_reads.score_due)
  summaries    write the quarterly "what we said, what was done, what
               happened" rows that outlive the raw reads (ai_reads)
  what_worked  this month's what-worked record by kind and tag
               (rec_learning.snapshot_what_worked; kept forever)
  implemented_trackers  start the tracker of every change made this week
               that has none yet (outcomes.autostart_due) — one recorded
               inside a caller's own transaction could not start it there
  thresholds   refit the trigger margins to the restaurant's own noise
               (restaurant_thresholds.refresh)
  scorecard    this month's learning scorecard and its flags
               (learning_scorecard.snapshot; kept forever)
  links        resolve the cross-module links the owner answered or that a
               later read no longer found (link_memory.settle)

Only restaurants that may teach a learner are passed in
(models.learning_eligible): a demo, a test account or an internal one never
scores a claim or writes a summary another reader would learn from.
"""
import logging

log = logging.getLogger(__name__)


def _claims(restaurant_id, today, db_path):
    import ai_reads
    return ai_reads.score_due(restaurant_id, today=today, db_path=db_path)


def _summaries(restaurant_id, today, db_path):
    import ai_reads
    return {"written": ai_reads.summarise_quarters(restaurant_id, today=today, db_path=db_path)}


def _what_worked(restaurant_id, today, db_path):
    import rec_learning
    kw = {"db_path": db_path} if db_path else {}
    return {"written": rec_learning.snapshot_what_worked(restaurant_id, **kw)}


# name -> fn(restaurant_id, today, db_path) -> dict, run in this order;
# each step reads only committed rows, so a failing step never corrupts the
# next.
def _implemented_trackers(restaurant_id, today, db_path):
    import outcomes
    kw = {"db_path": db_path} if db_path else {}
    return {"started": outcomes.autostart_due(restaurant_id, today=today, **kw)}


def _thresholds(restaurant_id, today, db_path):
    import restaurant_thresholds
    return {"written": restaurant_thresholds.refresh(restaurant_id, today=today, db_path=db_path)}


def _scorecard(restaurant_id, today, db_path):
    import learning_scorecard
    return learning_scorecard.snapshot(restaurant_id, today=today, db_path=db_path)


def _links(restaurant_id, today, db_path):
    import link_memory
    return link_memory.settle(restaurant_id, today=today, db_path=db_path)


STEPS = [
    ("claims", _claims),
    ("summaries", _summaries),
    ("what_worked", _what_worked),
    ("implemented_trackers", _implemented_trackers),
    ("thresholds", _thresholds),
    ("scorecard", _scorecard),
    ("links", _links),
]


def nightly(restaurant_id, today=None, db_path=None) -> dict:
    """Every step for one restaurant: {"ok": bool, "steps": {name: result},
    "failed": [names]}. A step that raises is recorded and the rest run."""
    out = {"ok": True, "steps": {}, "failed": []}
    for name, fn in STEPS:
        try:
            out["steps"][name] = fn(restaurant_id, today, db_path)
        except Exception as e:
            out["ok"] = False
            out["failed"].append(name)
            log.warning("learning_memory: %s failed for rid=%s: %s", name, restaurant_id, e)
            try:
                import ops
                ops.capture(e, job="learning_memory", context=f"restaurant_id={restaurant_id} step={name}")
            except Exception:
                pass
    return out


def eligible_ids(db_path=None) -> list:
    """The restaurants the nightly pass walks: in service and allowed to
    teach a learner (models.learning_eligible)."""
    import models
    rows = models.get_all_restaurants(db_path=db_path) if db_path else models.get_all_restaurants()
    out = []
    for r in rows or []:
        try:
            if models.in_service(r) and models.learning_eligible(r):
                out.append(r.id)
        except Exception:
            continue
    return out
