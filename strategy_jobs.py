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
  run_intraday_capture      hourly  — net sales so far today, while a POS that
                                      can be read during service is open
  run_pre_dinner_pulse      daily   — one push before dinner when the day is
                                      materially off a typical same weekday
  run_coverage_check        service — scheduled staff who have not clocked in
"""
import logging
import os
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


def run_issue_scan(db_path=DB_PATH, local_hour=None):
    """Reviews hourly; the daily operational signals and the opening
    checklist once a day at `local_hour` in the restaurant's own timezone
    (None = no gate, for a direct call)."""
    import issues, ops
    from time_utils import restaurant_now
    opened = 0
    for r in _restaurants(db_path):
        if getattr(r, "module_reviews", 0):
            try:
                opened += len(issues.open_from_reviews(r.id, db_path=db_path) or [])
            except Exception as e:
                ops.capture(e, job="issue_scan", context=f"restaurant_id={r.id}")
        try:
            local = restaurant_now(r, naive=True)
            # The checklist is time-of-day sensitive, so it runs every pass —
            # its own grace period decides when it is late (issues.
            # open_from_checklists), and source_key keeps it to one a day.
            opened += len(issues.open_from_checklists(r.id, db_path=db_path, now_local=local) or [])
        except Exception as e:
            ops.capture(e, job="issue_checklists", context=f"restaurant_id={r.id}")
        if local_hour is not None:
            import scheduler
            if not scheduler.local_due(r, local_hour, claim_key="issue_signals"):
                continue
        try:
            opened += len(issues.open_from_signals(r.id, db_path=db_path) or [])
        except Exception as e:
            ops.capture(e, job="issue_signals", context=f"restaurant_id={r.id}")
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


# Used only when a restaurant hasn't set its hours: without a fallback the
# intraday features would silently never run for them, which reads exactly
# like the POS not being supported.
DEFAULT_SERVICE_HOURS = (10, 23)


def _open_now(r, local):
    """True when the restaurant is inside its opening hours, or inside a
    plain daytime window when it hasn't set any."""
    from notify import _open_window
    window = _open_window(r, local.strftime("%A"))
    if not window:
        return DEFAULT_SERVICE_HOURS[0] <= local.hour < DEFAULT_SERVICE_HOURS[1]
    opens, closes = window
    if opens and local.time() < __import__("datetime").time(*opens):
        return False
    if closes and local.time() >= __import__("datetime").time(*closes):
        return False
    return True


def run_intraday_capture(db_path=DB_PATH):
    """Snapshot net sales so far, once an hour, while the restaurant is open.
    Each snapshot is also the baseline for the same weekday in later weeks —
    no hour-level history existed to compare a running day against."""
    import intraday, ops
    from time_utils import restaurant_now
    captured = skipped = 0
    for r in _restaurants(db_path):
        local = restaurant_now(r, naive=True)
        if not _open_now(r, local):
            skipped += 1
            continue
        if not ops.claim_period(f"intraday:{r.id}", f"{local.date().isoformat()}-{local.hour}"):
            continue
        try:
            captured += 1 if intraday.capture(r.id, now_local=local, db_path=db_path,
                                              restaurant=r).get("ok") else 0
        except Exception as e:
            ops.capture(e, job="intraday_capture", context=f"restaurant_id={r.id}")
    return {"captured": captured, "closed": skipped}


# The one interruption of the working day this product allows itself: late
# enough that the lunch numbers are in, early enough to change tonight's
# staffing, prep or a text to the guest club.
PULSE_HOUR = 16


def run_pre_dinner_pulse(db_path=DB_PATH):
    """One push before dinner, and only when today is materially off a
    typical same weekday at this hour. A pulse that fires every day is a
    notification people turn off."""
    import intraday, ops, push, scheduler
    from time_utils import restaurant_now
    sent = 0
    for r in _restaurants(db_path):
        local = restaurant_now(r, naive=True)
        if not scheduler.local_due(r, PULSE_HOUR, until=PULSE_HOUR + 2,
                                   claim_key="pre_dinner_pulse", now_local=local):
            continue
        try:
            p = intraday.pulse(r.id, now_local=local, db_path=db_path, restaurant=r)
            if not p.get("available") or not p.get("off"):
                continue
            # The people who already get the morning brief — owners, and
            # managers the owner put on it. Same audience, same day's numbers.
            import morning_brief
            audience = {u["id"] for u in morning_brief.recipients(r.id, db_path)}
            if not audience:
                continue
            word = "behind" if p["direction"] == "behind" else "ahead of"
            push.fire_push(
                r.id, "intraday_pulse",
                f"{abs(p['pct']):.0f}% {word} a typical {p['weekday']}",
                f"${p['net_sales']:,.0f} by {p['hour']}:00 against about ${p['typical']:,.0f} "
                f"on the last {p['samples']} {p['weekday']}s.",
                data={"ask_prompt": f"Why is today running {word} a normal {p['weekday']}?"},
                db_path=db_path, user_ids=audience)
            sent += 1
        except Exception as e:
            ops.capture(e, job="pre_dinner_pulse", context=f"restaurant_id={r.id}")
    return {"sent": sent}


def run_coverage_check(db_path=DB_PATH):
    """A scheduled person who hasn't clocked in becomes the routed manager's
    issue — the one staffing problem that is still fixable while it matters.
    One issue per person per day (source_key)."""
    import intraday, issues, ops
    from time_utils import restaurant_now
    opened = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_labor", 0):
            continue
        local = restaurant_now(r, naive=True)
        if not _open_now(r, local):
            continue
        if "manager" not in issues.get_routing(r.id, db_path):
            continue
        try:
            gaps = intraday.coverage_gaps(r.id, now_local=local, db_path=db_path, restaurant=r)
            for m in (gaps.get("missing") or []):
                issue, token = issues.create_issue(
                    r.id, "coverage",
                    f"{m['employee']} hasn't clocked in",
                    detail=f"Scheduled {m['shift_start']} as {m['role']} — "
                           f"{m['minutes_late']} minutes ago, with no clock-in on the POS.",
                    severity="high",
                    source_key=f"coverage:{local.date().isoformat()}:{m['employee'].lower()}",
                    db_path=db_path)
                if token:
                    opened += 1
        except Exception as e:
            ops.capture(e, job="coverage_check", context=f"restaurant_id={r.id}")
    return {"opened": opened}


def run_preshift_nudge(db_path=DB_PATH):
    """Text the routed manager that tonight's lineup notes are ready, at the
    hour the owner chose (restaurants.preshift_nudge_hour; 0 = off).

    It goes to the MANAGER, not to staff: the pre-shift briefing is on the
    staff portal, and staff phone numbers carry no SMS consent — the one
    consented, routed number is the manager's. Nothing is sent when the
    briefing has nothing to say.
    """
    import issues, ops, preshift
    from time_utils import restaurant_now
    import scheduler
    sent = 0
    for r in _restaurants(db_path):
        hour = int(getattr(r, "preshift_nudge_hour", 0) or 0)
        if not hour:
            continue
        local = restaurant_now(r, naive=True)
        if not scheduler.local_due(r, hour, until=hour + 2, claim_key="preshift_nudge",
                                   now_local=local):
            continue
        try:
            brief = preshift.build(r.id, day=local.date(), db_path=db_path)
            items = brief.get("items") or []
            if not items:
                continue
            routing = issues.get_routing(r.id, db_path)
            manager = routing.get("manager")
            if not manager or not manager.get("phone"):
                continue
            from models import is_in_quiet_hours
            if is_in_quiet_hours(r.id, db_path=db_path):
                continue
            from auth import get_or_create_staff_portal_token
            from notify import send_sms
            token = get_or_create_staff_portal_token(r.id, db_path=db_path)
            base = (os.getenv("BASE_URL") or "https://dashboard.cavnar.ai").rstrip("/")
            lead = items[0]["text"]
            msg = (f"Cavnar AI · tonight's lineup notes are ready ({len(items)} point"
                   f"{'' if len(items) == 1 else 's'}): {lead} Read them with the team: "
                   f"{base}/staff/r/{token}")
            if send_sms(manager["phone"], msg[:320], use_case="alert"):
                sent += 1
        except Exception as e:
            ops.capture(e, job="preshift_nudge", context=f"restaurant_id={r.id}")
    return {"sent": sent}
