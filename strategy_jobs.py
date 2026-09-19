"""
strategy_jobs.py — the scheduled half of the strategic-foundation modules.

Each function here is one scheduler job, called from scheduler.scheduler_loop
through ops.run_job and gated there by ops.claim_period. They are kept out of
scheduler.py only to keep that file's loop readable; the gating, the order
and the cadence all live in the loop.

  run_outcome_evaluations   daily   — close outcome trackers whose window ended,
                                      and mark goals whose target is met
  run_loss_sync             daily   — pull comps/voids/refunds from POSes that
                                      report them (RPOWER today)
  run_issue_scan            hourly  — open an issue for a fresh bad review,
                                      where the owner has routed a manager
  run_auto_draft_schedules  weekly  — draft next week's schedule for owners who
                                      opted in; a DRAFT in Schedule History,
                                      never published to staff
"""
import logging
import uuid

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)


def _restaurants(db_path=DB_PATH):
    from models import get_all_restaurants
    for r in get_all_restaurants(db_path):
        if (getattr(r, "billing_status", None) or "trial").lower() in ("churned", "cancelled", "canceled"):
            continue
        yield r


def run_outcome_evaluations(db_path=DB_PATH):
    import outcomes, goals, ops
    closed = outcomes.evaluate_due(db_path=db_path)
    closed = len(closed) if isinstance(closed, (list, tuple)) else int(closed or 0)
    achieved = 0
    for r in _restaurants(db_path):
        try:
            achieved += len(goals.mark_achieved(r.id, db_path=db_path) or [])
        except Exception as e:
            ops.capture(e, job="goals_mark_achieved", context=f"restaurant_id={r.id}")
    return {"outcomes_closed": closed, "goals_achieved": achieved}


def run_loss_sync(db_path=DB_PATH):
    import loss_detection, ops
    synced = unsupported = 0
    for r in _restaurants(db_path):
        try:
            out = loss_detection.sync(r.id, db_path=db_path)
        except Exception as e:
            ops.capture(e, job="loss_sync", context=f"restaurant_id={r.id}")
            continue
        if out.get("ok"):
            synced += 1
        else:
            unsupported += 1
    return {"synced": synced, "not_supported": unsupported}


def run_issue_scan(db_path=DB_PATH):
    import issues, ops
    opened = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_reviews", 0):
            continue
        try:
            opened += len(issues.open_from_reviews(r.id, db_path=db_path) or [])
        except Exception as e:
            ops.capture(e, job="issue_scan", context=f"restaurant_id={r.id}")
    return {"opened": opened}


# A schedule generated this recently counts as "next week is handled" — the
# owner (or last week's auto-draft) already did it, and a second draft would
# only be noise in Schedule History.
AUTO_DRAFT_RECENT_DAYS = 5


def _recent_schedule(conn, restaurant_id):
    return conn.execute(
        "SELECT 1 FROM schedule_history WHERE restaurant_id=? AND "
        "generated_at >= datetime('now', ?)",
        (restaurant_id, f"-{AUTO_DRAFT_RECENT_DAYS} days")).fetchone() is not None


def run_auto_draft_schedules(db_path=DB_PATH):
    """Draft next week's schedule for every opted-in restaurant that hasn't
    already made one. The draft lands in Schedule History exactly as a
    hand-generated one does; nothing reaches staff until the owner publishes.

    Skipped where an external scheduling tool is named (the owner schedules
    in 7shifts/HotSchedules/etc. and a Cavnar draft would be a second,
    conflicting source of truth), and where Labor isn't on the plan."""
    import ops
    from client_api import _run_schedule_job
    drafted, skipped = 0, 0
    for r in _restaurants(db_path):
        if not getattr(r, "auto_draft_schedule", 0) or not getattr(r, "module_labor", 0):
            continue
        if (getattr(r, "external_scheduling_tool", None) or "").strip():
            skipped += 1
            continue
        conn = get_conn(db_path)
        try:
            if _recent_schedule(conn, r.id):
                skipped += 1
                continue
        finally:
            conn.close()
        job_id = f"auto-{uuid.uuid4().hex[:12]}"
        ops.start_async_job(job_id, "schedule", r.id)
        try:
            _run_schedule_job(job_id, r.id)
        except Exception as e:
            ops.capture(e, job="auto_draft_schedule", context=f"restaurant_id={r.id}")
            continue
        # _run_schedule_job reports its own failures into the job row rather
        # than raising, so the push below must wait on that verdict — telling
        # an owner a draft is waiting when none was saved is worse than silence.
        state = ops.read_async_job(job_id, restaurant_id=r.id) or {}
        if state.get("status") != "done":
            continue
        drafted += 1
        try:
            import push
            push.fire_push(r.id, "schedule_drafted", "Next week's schedule is drafted",
                           "Review it and publish when it looks right — nothing has gone to "
                           "your staff yet.", data={})
        except Exception as e:
            ops.capture(e, job="auto_draft_schedule_push", context=f"restaurant_id={r.id}")
    return {"drafted": drafted, "skipped": skipped}
