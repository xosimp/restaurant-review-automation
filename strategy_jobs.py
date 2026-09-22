"""
strategy_jobs.py — the scheduled half of the strategic-foundation modules.

Each function here is one scheduler job, called from scheduler.scheduler_loop
through ops.run_job and gated there by ops.claim_period. They are kept out of
scheduler.py only to keep that file's loop readable; the gating, the order
and the cadence all live in the loop.

  run_outcome_evaluations   daily   — close outcome trackers whose window ended,
                                      and mark goals whose target is met
  run_milestones            daily   — fire the once-ever moments (savings tiers,
                                      anniversaries, goals met, every review
                                      answered) and notify on the big ones
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
import config
import uuid

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)


def _restaurants(db_path=DB_PATH):
    from models import get_all_restaurants
    for r in get_all_restaurants(db_path):
        if (getattr(r, "billing_status", None) or "trial").lower() in ("churned", "cancelled", "canceled", "paused"):
            continue
        yield r


# A result worth interrupting for. Below this, "it moved a little" is a line
# in the monthly review, not a notification.
OUTCOME_WORTH_TELLING = 100.0   # dollars a month


def run_outcome_evaluations(db_path=DB_PATH):
    import outcomes, goals, ops
    results = outcomes.evaluate_due(db_path=db_path) or []
    closed = len(results) if isinstance(results, (list, tuple)) else int(results or 0)
    told = _tell_owners_what_worked(results if isinstance(results, list) else [], db_path)
    achieved = 0
    for r in _restaurants(db_path):
        try:
            achieved += len(goals.mark_achieved(r.id, db_path=db_path) or [])
        except Exception as e:
            ops.capture(e, job="goals_mark_achieved", context=f"restaurant_id={r.id}")
    return {"outcomes_closed": closed, "goals_achieved": achieved, "wins_told": told}


def _tell_owners_what_worked(results, db_path):
    """Tell an owner when a change they made paid off.

    This is the only notification in the product that is about money the
    owner ALREADY made rather than money they are losing, and it is the one
    that makes every other recommendation worth reading. evaluate_due has
    computed it daily since outcomes shipped and nobody was ever told.

    One per restaurant per pass, the biggest — five separate "this worked"
    pushes on the same morning is how a win becomes noise.
    """
    import ops, push
    best = {}
    for row in results:
        if not isinstance(row, dict) or row.get("verdict") != "improved":
            continue
        dollars = row.get("dollars_monthly")
        if not dollars or abs(float(dollars)) < OUTCOME_WORTH_TELLING:
            continue
        rid = row.get("restaurant_id")
        if rid is None:
            continue
        if abs(float(dollars)) > abs(float(best.get(rid, {}).get("dollars_monthly") or 0)):
            best[rid] = row
    told = 0
    for rid, row in best.items():
        try:
            import outcomes as _outcomes
            dollars = abs(float(row["dollars_monthly"]))
            body = (f"{row.get('title') or 'The change you made'}: "
                    f"{row.get('metric_label') or row.get('metric')} improved over the "
                    f"window you set. {_outcomes.CAUSATION_CAVEAT}")
            if _reach(rid, "outcome_achieved",
                      f"That one worked — about ${dollars:,.0f}/month", body,
                      {"ask_prompt": f"What did {row.get('title') or 'that change'} actually do?"},
                      db_path, subject="A change you made paid off"):
                told += 1
        except Exception as e:
            ops.capture(e, job="outcome_win_push", context=f"restaurant_id={rid}")
    return told


def run_milestones(db_path=DB_PATH):
    """Mark the moments worth marking, and tell the owner about the ones
    worth interrupting for.

    Every detector is idempotent by construction — milestones.fire() only
    ever returns a row the first time — so this is safe to run daily and
    produces nothing on almost every day, which is the point.

    NOT everything that fires gets a push. An anniversary and a goal met
    are worth a notification; a savings tier is worth one because it is the
    product proving its own case. A record is deliberately NOT pushed from
    here: good_news already puts one in the morning brief, and a record
    plus a brief line plus a milestone is the same news three times.
    """
    import milestones, ops
    fired = pushed = 0
    for r in _restaurants(db_path):
        try:
            for m in (milestones.check_all(r.id, restaurant=r, db_path=db_path) or []):
                fired += 1
                if m.get("kind") not in ("savings", "anniversary", "goal"):
                    continue
                if _reach(r.id, "milestone", m["title"], m.get("body") or "",
                          {"ask_prompt": f"Tell me more about this: {m['title']}"},
                          db_path, subject=m["title"], email_type="milestone"):
                    milestones.mark_notified(r.id, m["key"], db_path=db_path)
                    pushed += 1
        except Exception as e:
            ops.capture(e, job="milestones", context=f"restaurant_id={r.id}")
    return {"fired": fired, "notified": pushed}


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
            _loss_flags_to_issues(r, db_path)
        else:
            unsupported += 1
    return {"synced": synced, "not_supported": unsupported}


def _loss_flags_to_issues(r, db_path):
    """A comp/void concentration on one approver becomes an owner-facing
    issue — filed, not texted (notify=False): it names a manager, so it
    goes to the person who can review the tickets, never to the routed
    manager who may be its subject. One per approver per week (source_key)."""
    try:
        import loss_detection, issues
        sig = loss_detection.signals(r.id, db_path=db_path)
        if not sig.get("available"):
            return
        week = (sig.get("week") or ["?"])[0]
        for entry in sig.get("kinds") or []:
            for f in entry.get("flags") or []:
                if f.get("type") != "concentration":
                    continue
                who = _approver_name(r.id, f.get("approver"), db_path)
                headline = f["headline"]
                if who:
                    headline = headline.replace(f"One manager (POS id {f.get('approver')})", f"{who} (POS id {f.get('approver')})")
                issues.create_issue(
                    r.id, "loss", headline[:120],
                    detail=((f"POS approver id {f.get('approver')} is {who} on your staff list. " if who else
                             f"POS approver id {f.get('approver')} is not on your staff list — add the POS id under "
                             f"Labor → Staff contacts to name them next time. ")
                            + f"{f.get('alternative', '')} {sig.get('note', '')}").strip(),
                    severity="normal", source_key=f"loss:{week}:{entry['kind']}:{f.get('approver')}",
                    notify=False, db_path=db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="loss_issues", context=f"restaurant_id={r.id}")


def _approver_name(restaurant_id, pos_id, db_path):
    """The staff contact whose pos_id matches, or None. A name only from
    a mapping the owner entered — never guessed from a similar string."""
    if not pos_id or str(pos_id) == "unrecorded":
        return None
    try:
        from models import get_conn
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT employee_name FROM staff_contacts WHERE restaurant_id=? AND pos_id=?",
                               (restaurant_id, str(pos_id))).fetchone()
        finally:
            conn.close()
        return row["employee_name"] if row else None
    except Exception:
        return None


WEEKLY_PLAN_PROMPT = (
    "It is Monday morning. Using your tools, read this week's business snapshot, the review brief, "
    "open issues, goals, recent outcomes and any cross-module findings. Then return ONLY a JSON array "
    "of at most 3 objects, no prose, each: {\"title\": an imperative of at most 80 characters, "
    "\"why\": at most 200 characters citing a figure you actually read, "
    "\"owner\": one of \"owner\", \"manager\", \"kitchen\", \"due_days\": an integer 1-7}. "
    "Only actions the data supports; fewer is fine; an empty array if nothing is worth a week."
)


def _parse_plan(answer):
    import json, re
    text = (answer or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not str(it.get("title") or "").strip():
            continue
        try:
            due = max(1, min(7, int(it.get("due_days") or 7)))
        except (TypeError, ValueError):
            due = 7
        out.append({"title": str(it["title"]).strip()[:80], "why": str(it.get("why") or "").strip()[:200],
                    "owner": str(it.get("owner") or "owner").lower()[:20], "due_days": due})
    return out[:3]


def run_weekly_plan(db_path=DB_PATH):
    """Monday 7am local: the agent — not a script — reads the week and files
    up to three owned actions as issues (notify=False: they appear on Home,
    nobody is texted). The morning brief is deterministic by design; this is
    the one place the model is asked to hold the why across modules. Off
    unless weekly_plan_enabled; claimed per ISO week."""
    import ops, issues
    from time_utils import restaurant_now
    filed = 0
    for r in _restaurants(db_path):
        if not getattr(r, "weekly_plan_enabled", 0):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != 0 or not (7 <= local.hour < 11):
            continue
        week = local.strftime("%G-W%V")
        if not ops.claim_period(f"weekly_plan:{r.id}", week):
            continue
        try:
            from ask_cavnar import ask_with_tools
            answer, _trunc, _props, _meta = ask_with_tools(r, WEEKLY_PLAN_PROMPT, history=[], user=None)
            for i, item in enumerate(_parse_plan(answer)):
                issues.create_issue(
                    r.id, "plan", item["title"],
                    detail=f"{item['why']} Owner: {item['owner']}. Due in {item['due_days']} days.",
                    severity="normal", source_key=f"plan:{week}:{i}", notify=False, db_path=db_path)
                filed += 1
        except Exception as e:
            ops.capture(e, job="weekly_plan", context=f"restaurant_id={r.id}")
    return {"filed": filed}


def run_recipe_drafts(db_path=DB_PATH):
    """Tuesday 5am local, after the nightly depletion has created any new
    menu items: draft recipes for dishes that have none (recipes.py), a
    bounded number per restaurant per week. Only where Food Cost is on."""
    import ops, recipes
    from time_utils import restaurant_now
    drafted = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_inventory", 0):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != 1 or not (5 <= local.hour < 9):
            continue
        if not ops.claim_period(f"recipe_drafts:{r.id}", local.strftime("%G-W%V")):
            continue
        try:
            drafted += recipes.draft_missing(r.id, db_path=db_path).get("drafted", 0)
        except Exception as e:
            ops.capture(e, job="recipe_drafts", context=f"restaurant_id={r.id}")
    return {"drafted": drafted}


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


CALIBRATION_MIN_WEEKS = 8
CALIBRATION_STEP = 2
CALIBRATION_MAX_WEIGHT = 30


def run_quality_calibration(db_path=DB_PATH):
    """Weekly: let each restaurant's own record nudge how much a quality
    dimension counts. For the last published weeks with a stored quality
    verdict, a week is "clean" when no coverage or no-show issue was opened
    during it. A dimension whose score sits at least 15 points higher in
    clean weeks than in troubled ones, over at least CALIBRATION_MIN_WEEKS
    weeks with both kinds present, gains CALIBRATION_STEP weight (capped);
    nothing is ever lowered, and nothing moves before the sample exists.
    The change is recorded like a rating change, so it can be seen and
    undone."""
    import json as _j
    from models import get_restaurant, update_restaurant, get_quality_weights, record_capability_change
    import shift_quality as _sq
    changed = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_labor", 0):
            continue
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT week_start, week_end, quality_json FROM schedule_history WHERE restaurant_id=? "
                "AND published_at IS NOT NULL AND quality_json IS NOT NULL ORDER BY id DESC LIMIT 26", (r.id,)).fetchall()
            weeks = []
            for row in rows:
                try:
                    q = _j.loads(row["quality_json"] or "null") or {}
                except Exception:
                    continue
                if not q.get("checked") or not row["week_start"]:
                    continue
                try:
                    trouble = conn.execute(
                        "SELECT 1 FROM ops_issues WHERE restaurant_id=? AND kind IN ('coverage', 'no_show') "
                        "AND substr(created_at, 1, 10) BETWEEN ? AND ? LIMIT 1",
                        (r.id, row["week_start"], row["week_end"] or row["week_start"])).fetchone()
                except Exception:
                    trouble = None
                weeks.append((not trouble, {d["key"]: d["score"] for d in (q.get("dimensions") or [])}))
        finally:
            conn.close()
        if len(weeks) < CALIBRATION_MIN_WEEKS:
            continue
        clean = [d for ok, d in weeks if ok]
        troubled = [d for ok, d in weeks if not ok]
        if len(clean) < 3 or len(troubled) < 3:
            continue
        current = get_quality_weights(r.id) or {}
        merged = dict(_sq.DEFAULT_WEIGHTS)
        merged.update(current)
        bumped = []
        for key in _sq.DIMENSIONS:
            c = [d[key] for d in clean if key in d]
            t = [d[key] for d in troubled if key in d]
            if len(c) < 3 or len(t) < 3:
                continue
            gap = sum(c) / len(c) - sum(t) / len(t)
            if gap >= 15 and merged.get(key, 0) < CALIBRATION_MAX_WEIGHT:
                merged[key] = min(CALIBRATION_MAX_WEIGHT, merged.get(key, 0) + CALIBRATION_STEP)
                bumped.append(f"{key} +{CALIBRATION_STEP} (clean weeks score it {gap:.0f} higher)")
        if not bumped:
            continue
        update_restaurant(r.id, {"quality_weights_json": _j.dumps(merged)}, db_path=db_path)
        try:
            record_capability_change(r.id, "quality_weights_calibrated", subject="weights",
                                     before=_j.dumps(current), after=_j.dumps(merged),
                                     changed_by="Cavnar AI (calibration)")
        except Exception:
            pass
        changed += 1
    return {"restaurants_changed": changed}


def run_auto_draft_schedules(db_path=DB_PATH):
    """Draft next week's schedule for every opted-in restaurant that hasn't
    already made one. The draft lands in Schedule History exactly as a
    hand-generated one does; nothing reaches staff until the owner publishes.

    Skipped where an external scheduling tool is named (the owner schedules
    in 7shifts/HotSchedules/etc. and a Cavnar draft would be a second,
    conflicting source of truth), and where Labor isn't on the plan."""
    import ops
    import time as _time
    from schedule_engine import _run_schedule_job
    drafted, skipped = 0, 0
    # Bounded and resumable, like run_daily_fetch: a five-minute draft per
    # 70-person roster means one pass cannot cover every restaurant, so the
    # cursor makes the next pass start where this one stopped.
    started = _time.monotonic()
    rows = [r for r in _restaurants(db_path)
            if getattr(r, "auto_draft_schedule", 0) and getattr(r, "module_labor", 0)]
    order = sorted(rows, key=lambda r: r.id)
    cursor = _read_cursor(AUTO_DRAFT_CURSOR_KEY, db_path)
    order = [r for r in order if r.id > cursor] + [r for r in order if r.id <= cursor]
    last_done = None
    for r in order:
        if _time.monotonic() - started > AUTO_DRAFT_MAX_SECONDS:
            break
        last_done = r.id
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
            import notify, push
            # The owner's task, not the line cook's: a teammate with the app
            # was told to review and publish a schedule they cannot publish.
            # morning_brief.recipients is the same audience the brief uses.
            import morning_brief
            audience = {u["id"] for u in morning_brief.recipients(r.id, db_path)}
            if not notify.briefing_allowed(r.id, "schedule_drafted", db_path):
                continue
            notify.record_notification(r.id, "schedule_drafted", db_path=db_path)
            push.fire_push(r.id, "schedule_drafted", "Next week's schedule is drafted",
                           "Review it and publish when it looks right — nothing has gone to "
                           "your staff yet.", data={}, db_path=db_path,
                           user_ids=audience or None)
        except Exception as e:
            ops.capture(e, job="auto_draft_schedule_push", context=f"restaurant_id={r.id}")
    _write_cursor(AUTO_DRAFT_CURSOR_KEY, last_done if last_done is not None else 0, db_path)
    return {"drafted": drafted, "skipped": skipped}


AUTO_DRAFT_CURSOR_KEY = "auto_draft_schedule_cursor"
AUTO_DRAFT_MAX_SECONDS = 40 * 60


def _read_cursor(key, db_path) -> int:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row and str(row["value"]).isdigit() else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _write_cursor(key, value, db_path) -> None:
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (key, str(value)))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def run_schedule_outcomes(db_path=DB_PATH):
    """Monday: record what each published week actually did, by daypart
    (schedule_intel.record_outcomes), for every Labor restaurant."""
    import schedule_intel
    written = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_labor", 0):
            continue
        try:
            written += schedule_intel.record_outcomes(r.id, db_path=db_path).get("written", 0)
        except Exception as e:
            import ops
            ops.capture(e, job="schedule_outcomes", context=f"restaurant_id={r.id}")
    return {"rows": written}


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
            import notify
            if not notify.briefing_allowed(r.id, "intraday_pulse", db_path):
                continue
            notify.record_notification(r.id, "intraday_pulse", db_path=db_path)
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
            # Everyone on today's schedule is busy; the person missing is the
            # gap. Best fits come from the same engine as the replacements
            # screen, folded into the text the manager actually reads.
            on_today = {str(x.get("employee") or "") for x in (gaps.get("scheduled") or [])}
            for m in (gaps.get("missing") or []):
                fits_text = ""
                try:
                    import labor_replacements
                    fits = labor_replacements.for_gap(r.id, m.get("role"), local.strftime("%A"),
                                                      exclude=on_today | {m["employee"]}, db_path=db_path)
                    fits_text = labor_replacements.sentence(fits)
                except Exception as fe:
                    ops.capture(fe, job="coverage_replacements", context=f"restaurant_id={r.id}")
                issue, token = issues.create_issue(
                    r.id, "coverage",
                    f"{m['employee']} hasn't clocked in",
                    detail=f"Scheduled {m['shift_start']} as {m['role']} — "
                           f"{m['minutes_late']} minutes ago, with no clock-in on the POS." + fits_text,
                    severity="high",
                    source_key=f"coverage:{local.date().isoformat()}:{m['employee'].lower()}",
                    db_path=db_path)
                if token:
                    opened += 1
        except Exception as e:
            ops.capture(e, job="coverage_check", context=f"restaurant_id={r.id}")
    return {"opened": opened}


def _reach(restaurant_id, alert_type, title, body, data, db_path, subject=None,
           lines=None, email_type=None):
    """Push to the people who have the app, email the ones who don't.

    morning_brief.deliver established this — push OR email, never both,
    because a brief that arrives twice is one people learn to ignore. The
    closing summary and the outcome win were written push-only, so an owner
    without the app simply never learned their change had paid off. Same
    audience either way (morning_brief.recipients).

    Returns the number of people reached.
    """
    import morning_brief, notify, push
    if not notify.briefing_allowed(restaurant_id, alert_type, db_path):
        return 0
    # A paused or lapsed account asked for quiet; the jobs' own iteration
    # filters most of these out, but a nudge fired directly (while-away,
    # connection lost) has to check for itself.
    from models import get_restaurant as _gr
    _r = _gr(restaurant_id, db_path=db_path)
    if _r and (_r.billing_status or "trial").lower() in ("paused", "churned", "cancelled", "canceled"):
        return 0
    people = morning_brief.recipients(restaurant_id, db_path)
    if not people:
        return 0
    devices = {}
    for token in (push.get_device_tokens(restaurant_id, db_path, for_delivery=True) or []):
        devices.setdefault(int(token.get("user_id") or 0), []).append(token)

    notify.record_notification(restaurant_id, alert_type, db_path=db_path)
    pushed = {u["id"] for u in people if devices.get(u["id"])}
    if pushed:
        push.fire_push(restaurant_id, alert_type, title, body, data=data,
                       db_path=db_path, user_ids=pushed)
    reached = len(pushed)

    emailed = [u for u in people if u["id"] not in pushed and u.get("email")]
    if emailed:
        import html as _h
        import emails as _emails
        from models import get_restaurant
        restaurant = get_restaurant(restaurant_id)
        name = (restaurant.location_name or restaurant.name) if restaurant else "your restaurant"
        body_html = _emails.report_shell(
            kicker=name,
            title=title,
            subtitle="",
            sections=[_emails.report_paragraph(_h.escape(line))
                      for line in (lines or [body])],
        )
        for user in emailed:
            result = _emails.deliver(
                email_type=email_type or alert_type, restaurant_id=restaurant_id, payload={
                    "from": _emails.sender("client"),
                    "to": [user["email"]],
                    "subject": f"{subject or title} — {name}",
                    "preheader": body[:120],
                    "html": body_html,
                })
            if getattr(result, "ok", False):
                reached += 1
    return reached


def _close_hour(r, local):
    """The hour this restaurant is done for the night, rounded up past any
    half hour, or 22 when it hasn't set hours."""
    from notify import _open_window
    window = _open_window(r, local.strftime("%A"))
    closes = window[1] if window else None
    if not closes:
        return DEFAULT_SERVICE_HOURS[1] - 1
    hour = closes[0] + (1 if closes[1] else 0)
    return min(hour, 23)


def run_closing_summary(db_path=DB_PATH):
    """How tonight went, sent once the doors are shut.

    The morning brief tells an owner how YESTERDAY went. Nothing told them
    how TODAY went, while they can still picture the room — which is the one
    moment the number means something specific instead of being a figure in
    a table. Pairs it with the close-out handoff, so whatever the closing
    manager wrote reaches the owner the same night rather than at 7am.

    P4: passive, no sound, no Focus break. It is a summary, not an alert.
    """
    import closeout, intraday, ops, push, scheduler
    from models import is_in_quiet_hours
    from time_utils import restaurant_now
    sent = 0
    for r in _restaurants(db_path):
        if not getattr(r, "morning_brief_enabled", 1):
            continue
        local = restaurant_now(r, naive=True)
        hour = _close_hour(r, local)
        if not scheduler.local_due(r, hour, until=min(hour + 2, 24),
                                   claim_key="closing_summary", now_local=local):
            continue
        # An owner who asked not to be disturbed at night meant this too.
        # It is in tomorrow's brief either way.
        if is_in_quiet_hours(r.id, db_path=db_path):
            continue
        try:
            day = closeout.business_date_for(r, now_local=local)
            summary = intraday.closing_summary(r.id, day=day, db_path=db_path, restaurant=r)
            note = closeout.get(r.id, day, db_path=db_path)
            title, body = _closing_text(summary, note)
            if not title:
                continue
            if _reach(r.id, "closing_summary", title, body,
                      {"ask_prompt": f"How did {day.strftime('%A')} actually go?"},
                      db_path, subject="How tonight went"):
                sent += 1
        except Exception as e:
            ops.capture(e, job="closing_summary", context=f"restaurant_id={r.id}")
    return {"sent": sent}


def _closing_text(summary, note):
    """(title, body), or (None, None) when there is nothing worth sending.

    Nothing worth sending is a real outcome: a night with no POS reading and
    no close-out is a night Cavnar has nothing to say about, and saying it
    anyway is how a summary becomes something people turn off.
    """
    lines = []
    if summary.get("available"):
        pct = summary.get("pct") or 0
        word = "behind" if summary["direction"] == "behind" else "ahead of"
        if abs(pct) < 5:
            title = f"${summary['net_sales']:,.0f} — about a normal {summary['weekday']}"
        else:
            title = (f"${summary['net_sales']:,.0f} — {abs(pct):.0f}% {word} "
                     f"a typical {summary['weekday']}")
        lines.append(f"Against about ${summary['typical']:,.0f} on the last "
                     f"{summary['samples']} {summary['weekday']}s.")
    elif summary.get("net_sales") is not None:
        title = f"${summary['net_sales']:,.0f} tonight"
        lines.append(summary.get("reason") or "")
    else:
        title = None
    if note:
        for field, label in (("went_wrong", "Went wrong"), ("eighty_sixed", "86'd"),
                             ("callouts", "Call-outs")):
            value = (note.get(field) or "").strip()
            if value:
                lines.append(f"{label}: {value}")
        if title is None and any((note.get(f) or "").strip() for f in
                                 ("went_well", "went_wrong", "eighty_sixed", "callouts")):
            title = "Tonight's handover is in"
    if title is None:
        return None, None
    return title, " ".join(l for l in lines if l)[:300] or "Open Cavnar AI for the detail."


# Once a week at most. A "quiet night coming up" that arrives every day is
# a calendar, not an opportunity.
DEMAND_OPPORTUNITY_HOUR = 10


def run_demand_opportunity(db_path=DB_PATH):
    """A quiet night two days out, while there is still time to fill it.

    Marketing and demand were the one area of the product that produced no
    notification at all — an owner had to go and look, and the whole point
    of a slow Tuesday is that it is knowable in advance. P5: informational,
    never buzzes, and folded into nothing because it is a genuine one-a-week
    thing rather than part of the morning battery.
    """
    import demand, ops, push
    from time_utils import restaurant_now
    sent = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_marketing", 0):
            continue
        local = restaurant_now(r, naive=True)
        if not (DEMAND_OPPORTUNITY_HOUR <= local.hour < DEMAND_OPPORTUNITY_HOUR + 4):
            continue
        # Claimed on the ISO WEEK, not the date — scheduler.local_due claims
        # per day, which for a weekly job would mean one every morning.
        if not ops.claim_period(f"demand_opportunity:{r.id}", local.strftime("%G-W%V")):
            continue
        try:
            out = demand.quiet_night_ahead(r.id, today=local.date(), db_path=db_path)
            if not out.get("available"):
                continue
            import morning_brief, notify
            audience = {u["id"] for u in morning_brief.recipients(r.id, db_path)}
            if not audience:
                continue
            if not notify.briefing_allowed(r.id, "demand_opportunity", db_path):
                continue
            notify.record_notification(r.id, "demand_opportunity", db_path=db_path,
                                       value=float(out["typical_sales"]))
            # The fill, drafted: a post and a guest text for that night,
            # saved as drafts behind the same approval as any other. The
            # push used to ask "what could fill it?" — now it says "here's
            # what I wrote; approve it". Drafting can fail (budget, model)
            # without costing the owner the heads-up.
            drafted = _draft_quiet_night_fill(r, out, db_path)
            body = (f"About ${out['typical_sales']:,.0f}, {out['below_average_pct']:.0f}% under a "
                    f"typical day across {out['samples']} of them. ")
            body += ("A post and a guest text are drafted — approve them from Marketing."
                     if drafted else "Two days to do something about it.")
            push.fire_push(
                r.id, "demand_opportunity",
                f"{out['weekday']} is usually your quietest night", body,
                data={"ask_prompt": f"What could fill {out['weekday']} night?", **drafted},
                db_path=db_path, user_ids=audience)
            sent += 1
        except Exception as e:
            ops.capture(e, job="demand_opportunity", context=f"restaurant_id={r.id}")
    return {"sent": sent}


def _draft_quiet_night_fill(r, out, db_path):
    """Draft the post and the text that would fill the quiet night. Returns
    {"post_draft_id", "sms_draft_id"} for whatever was saved, {} if neither."""
    import ops
    saved = {}
    weekday = out.get("weekday") or "the quiet night"
    topic = f"{weekday} night — a reason to come in this week"
    try:
        import marketing, marketing_drafts
        body = marketing.generate_content("instagram_post", topic, restaurant_id=r.id)
        if body and body.strip():
            res = marketing_drafts.save_draft(r.id, body.strip(), content_type="instagram_post", topic=topic)
            if res.get("ok"):
                saved["post_draft_id"] = res["id"]
    except Exception as e:
        ops.capture(e, job="quiet_night_post", context=f"restaurant_id={r.id}")
    try:
        import guest_marketing, marketing_drafts
        msg = guest_marketing.draft_campaign_message(r, campaign_type="slow_day", topic=topic)
        if msg and msg.strip():
            res = marketing_drafts.save_draft(r.id, msg.strip(), content_type="guest_sms",
                                              topic=f"{weekday} night guest text")
            if res.get("ok"):
                saved["sms_draft_id"] = res["id"]
    except Exception as e:
        ops.capture(e, job="quiet_night_sms", context=f"restaurant_id={r.id}")
    return saved


def run_trusted_orders(db_path=DB_PATH):
    """Monday 8am local: queue the supplier orders that can go on their own
    (ordering.py — a supplier with a record, a total in the usual band, no
    order this week), with an hour to undo, and tell the owner. Off unless
    auto_order_trusted is on for the restaurant."""
    import ops, ordering
    from time_utils import restaurant_now
    import scheduler
    queued = 0
    for r in _restaurants(db_path):
        if not getattr(r, "module_inventory", 0) or not getattr(r, "auto_order_trusted", 0):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != 0 or not scheduler.local_due(r, 8, claim_key="trusted_orders"):
            continue
        try:
            rows = ordering.queue_trusted_orders(r.id, restaurant=r, db_path=db_path)
            for row in rows:
                _reach(r.id, "order_send_pending",
                       "A supplier order goes out in an hour",
                       f"{row.get('label')}. Undo from Home if you'd rather look first.",
                       {"delayed_action_id": row["id"]}, db_path,
                       subject=f"Supplier order going out at {row['execute_at'][11:16]} UTC — {r.name}")
                queued += 1
        except Exception as e:
            ops.capture(e, job="trusted_orders", context=f"restaurant_id={r.id}")
    return {"queued": queued}


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
            base = config.base_url()
            lead = items[0]["text"]
            msg = (f"Cavnar AI · tonight's lineup notes are ready ({len(items)} point"
                   f"{'' if len(items) == 1 else 's'}): {lead} Read them with the team: "
                   f"{base}/staff/r/{token}")
            if send_sms(manager["phone"], msg[:320], use_case="alert"):
                sent += 1
        except Exception as e:
            ops.capture(e, job="preshift_nudge", context=f"restaurant_id={r.id}")
    return {"sent": sent}
