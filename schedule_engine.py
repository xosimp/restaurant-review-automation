"""
schedule_engine.py — the deterministic half of schedule generation.

labor.generate_optimized_schedule builds the prompt and makes the one model
call; everything around it lives here: the inputs a generation needs, the
row repair and the rule backstops applied to what the model wrote, the
signals the Shift Quality Engine (shift_quality.py, pure) scores against,
and the async job that runs the whole pipeline and persists the week.

This used to sit inside client_api.py, a route module, and every job and
mobile twin reached into it lazily. It is the Labor domain's engine and is
imported by client_api, mobile_api, strategy_jobs and delayed alike.
"""
import contextlib
import contextvars
import json
import os
import re
import threading
import time

from ai_guard import safe_error as _safe_err
from models import get_restaurant

import schedule_rules as _rules
import schedule_versions as _versions
import staff_settings as _staff
import demand_signals as _signals

# ── How a week is cut into model calls (schedule audit 10/3/26 E-28, P-34) ──
#
# A call is sized by what it has to write. The schedule model thinks, and its
# thinking shares max_tokens (labor.SCHEDULE_MAX_TOKENS_THINKING) with the
# rows, so a call can carry
#     (SCHEDULE_TOKEN_CEILING - THINKING_TOKENS_RESERVED - SUMMARY_TOKENS)
#         / OUTPUT_TOKENS_PER_ROW  rows  = (64,000 - 40,000 - 1,500) / 70 = 321
# before it risks a truncated answer. OUTPUT_TOKENS_PER_ROW is the cost of
# one row under the output contract in force (labor.SCHEDULE_SCHEMA, eight
# keys a row as JSON; the old figure, 60, was the CSV row's and undercounted
# the JSON one); tests/test_schedule_b2_calls.py holds it to the schema, so a
# schema change re-sizes the calls. A call's minutes grow with its rows as
# well — and with its thinking — so the rows held per call
# (ROWS_PER_CALL_TIME_CAP) stay at the slice size that finished well inside
# the old 360-second timeout with the earlier, non-thinking model, rather
# than where the tokens alone would let a call run (321 rows: about twice
# the minutes). Every call logs its rows, characters written and seconds
# (`slices`), so both figures can be re-read from the first real Opus weeks.
# A wrong guess costs a split, not the week: a cut answer keeps its finished
# days and the rest is written again smaller.
SCHEDULE_TOKEN_CEILING = 64000          # labor.SCHEDULE_MAX_TOKENS_THINKING (a test holds them equal)
THINKING_TOKENS_RESERVED = 40000        # adaptive thinking at effort "high", left room before rows
SUMMARY_TOKENS = 1500                   # the three bullets and the JSON around the rows
OUTPUT_TOKENS_PER_ROW = 70              # one row of labor.SCHEDULE_SCHEMA
ROWS_PER_CALL_BY_TOKENS = (SCHEDULE_TOKEN_CEILING - THINKING_TOKENS_RESERVED - SUMMARY_TOKENS) // OUTPUT_TOKENS_PER_ROW
ROWS_PER_CALL_TIME_CAP = 160
CHUNK_ROWS_PER_CALL = min(ROWS_PER_CALL_BY_TOKENS, ROWS_PER_CALL_TIME_CAP)
# A cut or failed part is split again — its dates in halves, then a single
# date by department, then a department's people in halves — at most this
# many times, and a generation makes at most its planned calls plus
# MAX_EXTRA_CALLS (retries and splits included) before it stops.
MAX_SPLIT_DEPTH = 5
MAX_EXTRA_CALLS = 8

# ── One wall-clock limit per generation (schedule audit 10/3/26 P-22) ──────
#
# A slice could take ~18 minutes (a 360-second timeout, retried twice), the
# phone and the web stop waiting at 15, and the job store gave up at 45 — and
# the thread kept going and overwrote "didn't finish" with a draft. Now each
# generation has one deadline: its planned calls at SCHEDULE_CALL_SECONDS
# each, plus SCHEDULE_POST_MODEL_SECONDS for the repair, scoring and save,
# never under SCHEDULE_JOB_MIN_SECONDS (what an ordinary week is given — the
# clients' 15 minutes) nor over SCHEDULE_JOB_MAX_SECONDS (under the job
# store's ops.JOB_MAX_MINUTES). Model calls stop at the deadline less the
# post-model allowance; each call waits at most the time left; the job store
# reads the job dead past the deadline (ops.set_async_job_deadline), and a
# job that finds itself declared dead saves nothing.
SCHEDULE_CALL_SECONDS = 360
SCHEDULE_POST_MODEL_SECONDS = 240
SCHEDULE_JOB_MIN_SECONDS = 15 * 60
SCHEDULE_JOB_MAX_SECONDS = 40 * 60

# At most this many generations run at once in this process (P-39): each
# was a daemon thread of its own, and their CPU-bound passes held the GIL
# against every other restaurant's requests on a four-thread web process.
# Owners' presses queue on the pool; the Thursday auto-draft takes a slot
# the same way (generation_scope).
SCHEDULE_GEN_WORKERS = max(1, int(os.getenv("SCHEDULE_GEN_WORKERS", "2")))


class ScheduleGenerationError(ValueError):
    """A generation that must not be saved, with a sentence the owner can
    read. Anything else that fails a job is logged and shown as a plain
    "try again" — never its exception text or a traceback (DATA-46)."""


class GenerationClock:
    """One generation's wall clock (P-22). `deadline` is when the job must
    be finished or failed; `model_deadline` is when the last model call must
    have returned, leaving SCHEDULE_POST_MODEL_SECONDS for everything after.
    plan(calls) sizes the deadline once the calls are planned and records it
    in the job store, so a poll knows how long the job can still run."""

    def __init__(self, job_id=None, started=None):
        self.job_id = job_id
        self.started = float(started if started is not None else time.time())
        self.deadline = self.started + SCHEDULE_JOB_MIN_SECONDS
        self._record()

    def plan(self, calls: int) -> None:
        """Size the deadline for `calls` planned model calls. Never shortens
        it: the quality gate's rewrite re-plans inside the same job."""
        want = int(calls or 1) * SCHEDULE_CALL_SECONDS + SCHEDULE_POST_MODEL_SECONDS
        sized = self.started + max(SCHEDULE_JOB_MIN_SECONDS, min(SCHEDULE_JOB_MAX_SECONDS, want))
        if sized > self.deadline:
            self.deadline = sized
            self._record()

    def _record(self):
        if self.job_id:
            _ops.set_async_job_deadline(self.job_id, self.deadline)

    @property
    def model_deadline(self) -> float:
        return self.deadline - SCHEDULE_POST_MODEL_SECONDS

    def model_seconds_left(self) -> float:
        return self.model_deadline - time.time()

    def seconds_left(self) -> float:
        return self.deadline - time.time()

    def minutes(self) -> int:
        return int(round((self.deadline - self.started) / 60.0))


_CLOCK = contextvars.ContextVar("schedule_generation_clock", default=None)


def current_clock():
    """The GenerationClock of the generation running on this thread, or None
    (a direct call — a test, a script — runs unbounded as before)."""
    return _CLOCK.get()


_GEN_SLOTS = threading.BoundedSemaphore(SCHEDULE_GEN_WORKERS)
_gen_pool = None
_gen_pool_lock = threading.Lock()


@contextlib.contextmanager
def generation_scope(job_id):
    """One generation's slot and clock: waits for one of the
    SCHEDULE_GEN_WORKERS slots (P-39), then starts the clock (P-22) — a
    generation queued behind two others does not spend its time waiting.
    submit_generation runs an owner's job inside it; the auto-draft enters it
    itself (strategy_jobs._draft_one)."""
    with _GEN_SLOTS:
        clock = GenerationClock(job_id)
        token = _CLOCK.set(clock)
        try:
            yield clock
        finally:
            _CLOCK.reset(token)


def submit_generation(job_id, restaurant_id, **job_kwargs):
    """Queue an owner's generation on the bounded pool (P-39). The pool's
    thread has no request to read who asked from, so the owner's attribution
    is taken here, on the request thread (ai_utils.attributed, #148). Returns
    the Future."""
    global _gen_pool
    import concurrent.futures
    import ai_utils as _ai
    with _gen_pool_lock:
        if _gen_pool is None:
            _gen_pool = concurrent.futures.ThreadPoolExecutor(max_workers=SCHEDULE_GEN_WORKERS,
                                                              thread_name_prefix="schedule-gen")
    job = _run_schedule_job            # the job as it stands when the owner pressed, not when a slot frees

    def _run(*a, **k):
        try:
            with generation_scope(job_id):
                return job(*a, **k)
        except Exception as e:
            # The job reports its own failures; this is one around it (the
            # slot, the clock). Said, and the job closed, so the owner's poll
            # never waits on a job nothing will finish.
            _ops.capture(e, job="schedule_generate", context=f"restaurant_id={restaurant_id} job={job_id}")
            _ops.finish_async_job(job_id, "error", {"ok": False, "error": generation_error_message(e)})
    return _gen_pool.submit(_ai.attributed(_run), job_id, restaurant_id, **job_kwargs)


def job_wait_seconds(calls: int = None) -> int:
    """How long a client should wait for a generation planned at `calls`
    model calls (None: an ordinary week) — the deadline the job will hold
    itself to, plus the store's grace."""
    want = (int(calls) * SCHEDULE_CALL_SECONDS + SCHEDULE_POST_MODEL_SECONDS) if calls else 0
    return int(max(SCHEDULE_JOB_MIN_SECONDS, min(SCHEDULE_JOB_MAX_SECONDS, want))
               + _ops.ASYNC_DEADLINE_GRACE_SECONDS)


def _labor_ot_line() -> float:
    """The hours past which overtime pay starts (labor.OVERTIME_THRESHOLD_HOURS)."""
    from labor import OVERTIME_THRESHOLD_HOURS
    return float(OVERTIME_THRESHOLD_HOURS)


def _expected_rows(shifts, roster_pairs) -> int:
    """How many shift rows a week here usually has: the busiest of the last
    four weeks in the history, else three and a half a head. A busiest week
    with shifts on fewer than five days — a new POS feed's first, partial
    week — is never taken below three and a half a head: it planned calls
    too small for the week (schedule audit 10/3/26 E-28). The calls are
    re-split when an answer is cut anyway; this keeps that the exception."""
    from datetime import datetime as _d
    per_head = int(len(roster_pairs or []) * 3.5)
    try:
        by_week, days = {}, {}
        for sh in shifts or []:
            d = (sh.get("date") or "")[:10]
            if len(d) == 10:
                wk = _d.strptime(d, "%Y-%m-%d").isocalendar()[:2]
                by_week[wk] = by_week.get(wk, 0) + 1
                days.setdefault(wk, set()).add(d)
        weeks = sorted(by_week)[-4:]
        if weeks:
            busiest = max(weeks, key=lambda w: by_week[w])
            n = by_week[busiest]
            return max(n, per_head) if len(days[busiest]) < 5 else n
    except Exception as _ex:
        print(f"[schedule] expected rows unread, planning from the roster: {_ex}")
    return per_head


def _week_monday(today, week_start=None):
    """The Monday the week starts on: the owner's choice when given (any
    date in the wanted week works), else next Monday."""
    from datetime import datetime as _d, timedelta as _t
    if week_start:
        try:
            d = _d.strptime(str(week_start)[:10], "%Y-%m-%d")
            return d - _t(days=d.weekday())
        except ValueError:
            pass
    days_ahead = (7 - today.weekday()) % 7 or 7
    return today + _t(days=days_ahead)


def revenue_source_words(basis, override_source=None):
    """What set the week's projected sales, in words, from week_hours_plan's
    `revenue_basis` — never a guess at which branch ran (blind audit)."""
    return {"override": override_source or "your own weekly pattern",
            "monthly": "monthly target ÷ 4.33",
            "last_year": "the same week last year",
            "recent": "recent sales scaled to a week"}.get(basis, "no sales on file to project the week from")


def forecast_preview(restaurant_id, week_start=None) -> dict:
    """The chosen week's forecast before anything is drafted — the Studio's
    Forecast tab. The same inputs _build_schedule_result hands the draft
    (the shift analysis, the labor target, the week's projected sales and
    its source, last year's same days) through the same arithmetic
    (labor.week_hours_plan), so the hours shown are the hours the draft is
    then given. Each day adds its own forecast sales (demand.forecast_day,
    the uncorrected figure the week's projection sums), the nights it rests
    on and the measured events on its date. No model call.

    {"ok": False, "reason"} when the restaurant has no live shift history,
    the same refusal Generate gives."""
    from datetime import timedelta as _td
    import demand
    import schedule_economics as _econ
    from labor import analyse_shifts_for_restaurant, get_hourly_rate, week_hours_plan
    from models import get_restaurant, get_yoy_schedule_context
    import thresholds as _thr
    from time_utils import restaurant_now
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return {"ok": False, "reason": "no such restaurant"}
    analysis = analyse_shifts_for_restaurant(restaurant_id, with_salaries=False)
    if not analysis.get("is_live"):
        return {"ok": False, "reason": _no_shift_data_message(restaurant_id, restaurant)}
    rate = analysis.get("blended_rate") or get_hourly_rate(restaurant_id)
    # thresholds.target_for: the number notify's reader hands the draft,
    # with the label that says whose target it is.
    tgt = _thr.target_for(restaurant, "labor")
    target = tgt["pct"]
    monday = _week_monday(restaurant_now(restaurant, naive=True), week_start)
    dates = [(monday + _td(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    monthly = float(getattr(restaurant, "monthly_revenue_target", 0) or 0)
    revenue = {"value": None, "source": None}
    try:
        revenue = _econ.projected_weekly_revenue(restaurant_id, week_dates=dates)
    except Exception as e:
        _soft_fail("forecast_preview revenue", e, restaurant_id)
    plan = week_hours_plan(analysis, dates, target, rate, yoy_context=get_yoy_schedule_context(restaurant_id, dates),
                           projected_revenue_override=revenue.get("value"), monthly_revenue_target=monthly)
    closed = _rules.closures(restaurant)
    days = []
    for d in dates:
        wd = _date_of(d).strftime("%A")
        row = {"date": d, "weekday": wd, "hours": plan["daily_target_hours"].get(d),
               "closed": d in closed["closed_dates"] or wd in closed["closed_weekdays"]}
        try:
            fc = demand.forecast_day(restaurant_id, _date_of(d), calibrate=False)
        except Exception:
            fc = {}
        if fc.get("available"):
            row.update({"sales": fc["typical_sales"], "samples": fc.get("samples"),
                        "low": fc.get("low"), "high": fc.get("high"),
                        "effects": [e.get("label") for e in (fc.get("effects") or []) if e.get("label")]})
        else:
            row["reason"] = fc.get("reason")
        days.append(row)
    source = revenue_source_words(plan.get("revenue_basis"), revenue.get("source"))
    out = {"ok": True, "week_start": dates[0], "days": days, "projected_revenue": plan["projected_revenue"],
           "projected_revenue_source": source, "hours_budget": plan["hours_budget"],
           "labor_budget_dollars": plan["labor_budget_dollars"], "labor_target": target,
           "hourly_rate": round(float(rate or 0), 2)}
    out["labor_target_label"] = tgt.get("label")
    try:
        out["demand_data_through"] = _demand_data_through(restaurant_id)
    except Exception:
        out["demand_data_through"] = None
    return out


def _date_of(iso):
    from datetime import date as _date
    return _date.fromisoformat(str(iso)[:10])


def check_week_start(restaurant_id, raw):
    """(week_start or None, error or None) for a Generate request. An
    unreadable date used to fall back silently to next Monday, and a week
    that had already happened generated and could be published (SCHED-37).
    Any date in the wanted week is accepted, as _week_monday reads it."""
    from datetime import date as _date, timedelta as _t
    raw = (raw or "").strip()[:10]
    if not raw:
        return None, None
    try:
        d = _date.fromisoformat(raw)
    except ValueError:
        return None, "That week isn't a date we can read — pick the week again."
    try:
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        today = _date.today()
    monday = d - _t(days=d.weekday())
    if monday + _t(days=6) < today:
        return None, "That week has already happened — pick this week or a later one."
    if monday > today + _t(days=7 * 12):
        return None, "That week is more than twelve weeks out — pick a nearer one."
    return raw, None


def _holiday_on_or_after(label, today):
    """'Jan 1' → the next such date on or after about a month ago. Parsed
    in today's year, a New Year's Day seen from late December was last
    January and fell out of the week it belongs to (SCHED-33)."""
    from datetime import datetime as _d, timedelta as _td
    base = today if isinstance(today, _d) else _d(today.year, today.month, today.day)
    d = _d.strptime(f"{label} {base.year}", "%b %d %Y")
    if d < base - _td(days=31):
        d = _d.strptime(f"{label} {base.year + 1}", "%b %d %Y")
    return d


def _no_shift_data_message(restaurant_id, restaurant=None, missing="history"):
    """What the owner reads when a week can't be drafted or forecast for want
    of data — by the restaurant's name and the screen to fix it on, never its
    database id or the tables behind it (schedule audit 10/3/26 E-30: "Edge
    Grill (id 5) has no shift data … it has no client data row at all").

    `missing`: "history" (no shifts of its own — what the Forecast tab
    needs), "unreadable" (a shifts file is on file but none of it could be
    read), "team" (no history and nobody on the team to schedule), "basis"
    (a team, but no history, Role floors or borrowed headcount to size the
    week by). The figures elsewhere on the Labor tab can come from the
    bundled sample while nothing is uploaded, so a populated tab proves
    nothing — still worth saying where it is the confusion."""
    name = getattr(restaurant, "name", None) or "This restaurant"
    if missing == "unreadable":
        return (f"{name}'s shifts file is on file, but none of its rows could be read, so there's nothing to "
                "schedule from. Upload it again with Update shifts CSV, then generate.")
    if missing == "team":
        return (f"{name} has no team and no shift history on file yet, so there's nobody to schedule. Add your "
                "people on the Schedule Studio's Team step, or upload past shifts with Upload shifts CSV, then "
                "generate.")
    if missing == "basis":
        return (f"{name} has no shift history yet, so there's nothing to size the week by. Set Role floors (how "
                "many of each role a morning and a night needs) on the Schedule Studio's Team step, or upload "
                "past shifts with Upload shifts CSV, then generate.")
    return (f"{name} has no shift history on file yet, so there's nothing to forecast the week from. Upload past "
            "shifts with Upload shifts CSV. Until you do, the figures on the Labor tab can be sample data, not "
            "your own.")


def _soft_fail(what, exc, restaurant_id):
    """An input the draft can do without failed to load: the draft still
    goes ahead, but the failure is said — a silent {} made "keep these two
    apart" vanish from both the prompt and the score with nobody told."""
    print(f"[schedule] {what} unavailable for restaurant {restaurant_id}: {exc}")
    try:
        _ops.capture(exc, job="schedule_inputs", context=f"restaurant_id={restaurant_id} input={what}")
    except Exception as _cx:
        print(f"[schedule] could not record that failure: {_cx}")


def _no_history_analysis() -> dict:
    """The analysis a restaurant with no shifts of its own is drafted against
    (E-30): no labor figures, no patterns, nothing from the bundled sample —
    labor.analyse_shifts_for_restaurant's preview for it is the fictional
    sample week, which must never shape a real draft."""
    return {"is_live": False, "no_history": True, "overall_labor_pct": None, "overstaffed_days": [],
            "understaffed_days": [], "dow_summary": {}, "period_days": 0, "total_sales": 0, "by_day": {},
            "role_rates": {}, "blended_rate": None, "date_range": {}}


def _build_schedule_result(restaurant_id, week_start=None, focus=None, dates=None, prior_rows=None,
                           owner_reason=None):
    """Shared logic for both schedule endpoints.

    focus — named weaknesses of the previous draft (a list of strings), for
    a regeneration of chosen dates; rendered into the prompt as "THE
    PREVIOUS DRAFT OF <those dates> SCORED WEAK ON" (schedule_requirements
    .focus_block). None for an ordinary generation.

    dates — a redo: only these dates are written (the owner's chosen days,
    or the quality gate's weakest); prior_rows — the rows of the days being
    kept, handed to every call as the rest of the week, never rewritten;
    owner_reason — {"chip", "text"} the owner gave for the redo, fenced in
    the prompt as their note (schedule audit 10/3/26 P-9, E-21, PR-18). The
    whole week used to be generated again blind to the kept days, and all but
    the chosen dates thrown away."""
    from labor import (analyse_shifts_for_restaurant, load_shifts_for_restaurant,
                       generate_optimized_schedule, get_hourly_rate,
                       build_demand_forecast)
    from models import get_restaurant, get_staff_notes, get_yoy_schedule_context, get_client_data
    from datetime import datetime as _dt, timedelta as _td

    restaurant = get_restaurant(restaurant_id)
    shifts = load_shifts_for_restaurant(restaurant_id)
    # Hourly: the schedule's hours budget is sales x target / the hourly rate.
    analysis = analyse_shifts_for_restaurant(restaurant_id, with_salaries=False) if shifts else {}
    # A restaurant with no shifts of its own (is_live is the real signal:
    # the loader once handed it a bundled fictional week) used to be refused
    # outright, before the borrowed-headcount path could run — so a new or
    # pre-opening client with a hand-built team could not draft at all (E-30).
    # It is drafted from its team now, sized by its Role floors and, where
    # similar restaurants are measured, their staffing (intelligence.
    # staffing), labelled a starting point. A shifts file that is on file but
    # unreadable is a broken upload, not a new restaurant: said as that.
    no_history = not shifts or not analysis.get("is_live")
    if no_history:
        try:
            _stored = ((get_client_data(restaurant_id) or {}).get("shifts_csv") or "").strip()
        except Exception:
            _stored = ""
        if _stored and len([ln for ln in _stored.splitlines() if ln.strip()]) > 1:
            raise ScheduleGenerationError(_no_shift_data_message(restaurant_id, restaurant, missing="unreadable"))
        shifts, analysis = [], _no_history_analysis()
    # Use blended rate from per-role rates if available, otherwise flat rate
    rate = analysis.get("blended_rate") or get_hourly_rate(restaurant_id)
    from notify import labor_target_for as _labor_target_for
    target   = _labor_target_for(restaurant)
    owner    = restaurant.owner_name if restaurant else None
    staff_notes = get_staff_notes(restaurant_id) or None

    # Employee availability
    # The table is created at boot (init_db); no DDL on a generation.
    from models import get_staff_availability as _gsa
    try:
        staff_availability = _gsa(restaurant_id) or []
    except Exception as _sfx:
        _soft_fail('staff_availability', _sfx, restaurant_id)
        staff_availability = []

    # Compute next week dates — the restaurant's week, not the server's
    from time_utils import restaurant_now
    today = restaurant_now(restaurant, naive=True)
    monday = _week_monday(today, week_start)
    next_week_dates = [(monday + _td(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    week_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    # Everything the rules need to know about who can work when, gathered
    # once and shared by the prompt, the backstops, the swap search, the
    # review panel and the publish gate.
    constraints = _rules.build_constraints(restaurant_id, next_week_dates, week_days, restaurant)
    roster_rows = _staff.roster(restaurant_id)
    roster_pairs = [(e["name"], e.get("role") or "") for e in roster_rows]
    if no_history and not roster_pairs:
        raise ScheduleGenerationError(_no_shift_data_message(restaurant_id, restaurant, missing="team"))
    # Days nobody on the roster can legally work (E-20): everyone on approved
    # time off, unavailable that weekday or off that day. They were asked of
    # the model like any day, came back empty twice, and failed the week with
    # "try again in a minute" after up to four paid calls — retrying never
    # helps and the cause was never named. They are worked out before any
    # call, left out of what the model is asked to write, accepted as empty,
    # and named for the owner.
    unstaffable = _nobody_can_work(constraints, [n for n, _r in roster_pairs],
                                   [d for d in next_week_dates if d not in constraints.closed_dates])

    # Revenue override from restaurant target (takes priority over YoY sum)
    monthly_rev_target = float(getattr(restaurant, 'monthly_revenue_target', 0) or 0)

    # YoY context — same day last year
    yoy_ctx = get_yoy_schedule_context(restaurant_id, next_week_dates)

    # Flag holiday matches in YoY context
    try:
        from marketing import get_upcoming_holidays as _guh_sched
        import re as _re_h
        _hol_str = _guh_sched(today)
        if _hol_str:
            _hol_this_week = {}
            for chunk in _hol_str.split(", "):
                m = _re_h.search(r'\((\w+ \d+)\)$', chunk)
                if m:
                    try:
                        hdate = _holiday_on_or_after(m.group(1), today)
                        for nd in next_week_dates:
                            if hdate.strftime("%Y-%m-%d") == nd:
                                _hol_this_week[nd] = chunk[:chunk.rfind("(")].strip()
                    except Exception:
                        pass
            for row in yoy_ctx:
                nd = row.get("next_week_date", "")
                if nd in _hol_this_week:
                    row["is_holiday"] = True
                    row["holiday_name"] = _hol_this_week[nd]
    except Exception:
        pass

    # Upcoming events for the schedule banner
    upcoming_events = []
    try:
        from marketing import get_upcoming_holidays as _guh2
        import re as _re_ev
        _ev_str = _guh2(today)
        if _ev_str:
            for chunk in _ev_str.split(", "):
                m = _re_ev.search(r'\((\w+ \d+)\)$', chunk)
                if m:
                    try:
                        edate = _holiday_on_or_after(m.group(1), today)
                        days_away = (edate - today).days
                        if 0 <= days_away <= 21:
                            upcoming_events.append({
                                "name": chunk[:chunk.rfind("(")].strip(),
                                "date_str": m.group(1),
                                "days_away": days_away
                            })
                    except Exception:
                        pass
    except Exception:
        pass

    # Weather forecast for the schedule week — never blocks generation if
    # geocoding/NWS is unavailable (see weather.get_forecast_for_week).
    try:
        from weather import get_forecast_for_week
        weather_forecast = get_forecast_for_week(restaurant, next_week_dates)
    except Exception:
        weather_forecast = []

    # The last PUBLISHED week before this one — never a discarded draft of
    # the same week — so "what changed" compares against the week that ran.
    prior_schedule_summary = None
    prior_published_rows = []
    try:
        _prior_csv = _last_published_csv(restaurant_id, next_week_dates[0])
        if _prior_csv:
            prior_schedule_summary = _summarize_schedule_csv_by_day_role(_prior_csv)
            prior_published_rows = _versions.rows_from_csv(_prior_csv)
    except Exception:
        prior_schedule_summary = None

    # Operational Score, its targets, and any shift leader rules. All three
    # are dormant when nobody has been rated, so an existing restaurant
    # schedules exactly as it did before this feature existed.
    from models import (get_operational_scores, get_role_strength_thresholds,
                        get_shift_leader_rules, get_shift_profiles)
    _op_scores = get_operational_scores(restaurant_id)
    _strength_thresholds = get_role_strength_thresholds(restaurant_id) if _op_scores else {}
    _leader_rules = get_shift_leader_rules(restaurant_id) if _op_scores else []

    # Shift profiles — what each shift is actually judged on. A restaurant
    # gets the engine's built-in set only once it has rated somebody, so
    # one that never touches any of this schedules exactly as before. Demand
    # levels come from its OWN sales rather than from an assumption that
    # every restaurant's Friday is busy.
    import shift_quality as _sq
    _stored_profiles = get_shift_profiles(restaurant_id)
    _demand_by_day = {}
    try:
        _forecast = build_demand_forecast(restaurant_id)
        if _forecast.get("ok"):
            # A weekday's level only on DEMAND_LEVEL_MIN_READINGS nights —
            # a median of two is one of them (CA1 L25).
            from thresholds import DEMAND_LEVEL_MIN_READINGS as _MINR
            _demand_by_day = {d["day"]: d["vs_average_pct"] for d in _forecast["days"]
                              if (d.get("samples") or 0) >= _MINR}
    except Exception:
        _demand_by_day = {}
    _profiles = []
    if _stored_profiles or _op_scores:
        _profiles = _sq.profiles_from_config(
            [_sq.profile_from_dict(p) for p in _stored_profiles] or None,
            default_strength=_strength_thresholds,
            default_leader_rules=_leader_rules,
            demand_by_day=_demand_by_day,
        )

    # Dated facts, pairings, reliability and what the manager keeps
    # changing — rendered here from the engine's own data and handed to the
    # prompt as text, so the same facts are what the code checks afterwards.
    signals_by_date = {}
    try:
        signals_by_date = _signals.by_date(restaurant_id, next_week_dates)
    except Exception as _sfx:
        _soft_fail('signals_by_date', _sfx, restaurant_id)
        signals_by_date = {}
    pairs = {}
    try:
        pairs = _staff.pair_sets(restaurant_id)
    except Exception as _sfx:
        _soft_fail('pairs', _sfx, restaurant_id)
        pairs = {}
    reliability = {}
    try:
        reliability = _staff.reliability(restaurant_id)
    except Exception as _sfx:
        _soft_fail('reliability', _sfx, restaurant_id)
        reliability = {}
    learned, pattern_conflicts = [], []
    try:
        # The live window's patterns and the standing ones the draft keeps
        # after the manager stopped having to correct them (memory audit
        # 9/29/26, standing_patterns), less the owner's dismissals and any
        # two editors pull opposite ways (those go to the owner).
        learned, pattern_conflicts = _versions.patterns_for_draft(restaurant_id)
    except Exception as _sfx:
        _soft_fail('learned', _sfx, restaurant_id)
        learned, pattern_conflicts = [], []
    # The money and the record: what a holiday did here last time, sales per
    # labor hour by daypart, what published weeks actually did, who has
    # carried the weekends, what staff want, who could hold a station.
    import schedule_economics as _econ
    import schedule_intel as _intel
    holiday = {}
    try:
        holiday = _merge_holiday_lift(restaurant_id, next_week_dates, signals_by_date)
    except Exception as _hx:
        print(f"[schedule] holiday lift unavailable: {_hx}")
        holiday = {}
    # The events block states only a lift this restaurant measured; the
    # prompt used to assert "20-40% higher covers" for any holiday (SCHED-33).
    for _ev in upcoming_events:
        for _d, _h in holiday.items():
            if _h.get("name") == _ev.get("name") and _h.get("lift_pct") is not None:
                _ev["lift_pct"], _ev["based_on"] = _h["lift_pct"], _h.get("based_on")
    splh = {}
    try:
        splh = _econ.splh_by_daypart(restaurant_id)
    except Exception as _sfx:
        _soft_fail('splh', _sfx, restaurant_id)
        splh = {}
    outcomes = {}
    try:
        outcomes = _intel.outcomes_by_daypart(restaurant_id)
    except Exception as _sfx:
        _soft_fail('outcomes', _sfx, restaurant_id)
        outcomes = {}
    ledger = {}
    try:
        ledger = _intel.fairness_ledger(restaurant_id)
    except Exception as _sfx:
        _soft_fail('ledger', _sfx, restaurant_id)
        ledger = {}
    # What schedule learning adds on top (schedule_learning_inputs): the
    # multi-week rotation, the sales-per-labor-hour objective, and for a
    # restaurant with no history of its own a borrowed starting headcount.
    learning = schedule_learning_inputs(restaurant_id, restaurant, roster_pairs, shifts, splh, target)
    stated_prefs, learned_prefs = {}, {}
    try:
        stated_prefs = _staff.stated_preferences(restaurant_id)
        learned_prefs = _intel.behaviour_preferences(restaurant_id)
    except Exception:
        pass
    could_hold = {}
    try:
        could_hold = _intel.could_hold(_intel.mentoring(restaurant_id))
    except Exception as _sfx:
        _soft_fail('could_hold', _sfx, restaurant_id)
        could_hold = {}
    revenue = {"value": None, "source": None}
    try:
        # The week's own DSR budget when the owner set one for most of its
        # nights (memory audit 9/29/26, owner_goals); the source says which.
        revenue = _econ.projected_weekly_revenue(restaurant_id, week_dates=next_week_dates)
    except Exception:
        pass
    # Who is experienced, who can run a shift and who usually works when —
    # the same reads the quality pass makes (_quality_signals), handed to
    # the prompt so the model is told the facts it is scored on. Any one of
    # them failing costs only its own block.
    from models import get_employee_tenure, get_leader_flags, get_prior_shift_pattern
    _people = {}
    for _key, _fn in (("tenure", get_employee_tenure), ("leader_flags", get_leader_flags),
                      ("prior_pattern", get_prior_shift_pattern)):
        try:
            _people[_key] = _fn(restaurant_id) or {}
        except Exception as _sfx:
            _soft_fail('people[_key]', _sfx, restaurant_id)
            _people[_key] = {}
    try:
        _people["experienced"] = sorted(_staff.experienced_names(restaurant_id))
    except Exception as _sfx:
        _soft_fail('people["experienced"]', _sfx, restaurant_id)
        _people["experienced"] = []
    # What the rest of the product knows about the nights being drafted
    # (memory audit 9/29/26): what the last four weeks of nightly reports
    # measured per weekday (dsr_to_schedule), the reviews diagnosis's and
    # the reports' open staffing asks as SOFT requirements
    # (reviews_to_labor), and the restaurant's memory for this surface —
    # the owner's constraints and goals, what was decided, what worked,
    # events, people (memory_context: fenced, M/D/YY, within a budget).
    import staffing_signals as _stsig
    last_nights_blk, soft_reqs = "", []
    try:
        last_nights_blk = _stsig.last_nights_block(restaurant_id, next_week_dates, today=today.date())
    except Exception as _sfx:
        _soft_fail('last_nights', _sfx, restaurant_id)
    try:
        soft_reqs = _stsig.soft_requirements(restaurant_id, next_week_dates, today=today.date())
    except Exception as _sfx:
        _soft_fail('soft_requirements', _sfx, restaurant_id)
    memory_blk = ""
    try:
        import memory_context as _mc
        from labor import TEAM_VIEWER as _team_viewer
        # As the team reads it: the draft is shared with every login that
        # can open the schedule, so an owner-only line never shapes it.
        _mem = _mc.memory_context(restaurant_id, "schedule", viewer=_team_viewer,
                                  subjects=["labor", "schedule"] + [f"labor:day:{d.lower()}" for d in week_days])
        if _mem.text:
            from ai_guard import MEMORY_FENCE_NOTE as _MFN
            memory_blk = ("\n\nWHAT CAVNAR AI REMEMBERS FOR THIS RESTAURANT (dated. " + _MFN + "):\n"
                          + _mem.text)
    except Exception as _sfx:
        _soft_fail('memory_context', _sfx, restaurant_id)
    # The rules block is kept apart from the rest so a department call can be
    # given the rules for its own people only (E-29, PR-17: a kitchen-only
    # call was told every manager's name and the whole roster's floors, then
    # told to schedule nobody off its list).
    rules_block = _rules.prompt_block(constraints)
    extra_rest = (_signals.prompt_block(signals_by_date, next_week_dates)
                    + _pairs_block(pairs, roster_pairs)
                    + _reliability_block(reliability)
                    + _versions.prompt_block(learned)
                    + _econ.splh_block(splh)
                    + _intel.outcome_block(outcomes, week_days)
                    + _intel.ledger_block(ledger)
                    + _intel.preferences_block(learned_prefs, stated_prefs)
                    + _could_hold_block(could_hold)
                    + _cohort_block(restaurant_id, restaurant)
                    + _intel.rotation_block(learning["rotation"])
                    + _econ.splh_objective_block(learning["splh_objective"], next_week_dates, signals_by_date)
                    + learning["starting_block"]
                    + last_nights_blk
                    + _stsig.soft_block(soft_reqs)
                    + memory_blk)
    extra_blocks = rules_block + extra_rest
    # No history of its own (E-30): the week is sized by the owner's Role
    # floors (or role minimums) and, where similar restaurants are measured,
    # their staffing. With neither, the model would invent every headcount —
    # refused, saying what to set, before any paid call.
    _role_mins = _parse_role_minimums(getattr(restaurant, "role_minimums_json", None))
    if no_history and not (constraints.role_floors or _role_mins or learning.get("borrowed_headcount")):
        raise ScheduleGenerationError(_no_shift_data_message(restaurant_id, restaurant, missing="basis"))

    _gen_kwargs = dict(
        restaurant_name=restaurant.name if restaurant else "Restaurant",
        hourly_rate=rate,
        owner_name=owner,
        staff_notes=staff_notes,
        labor_target=target,
        yoy_context=yoy_ctx,
        upcoming_events=upcoming_events if upcoming_events else None,
        monthly_revenue_target=monthly_rev_target,
        hours_notes=getattr(restaurant, 'hours_notes', None),
        role_rates=analysis.get("role_rates") or {},
        section_count=getattr(restaurant, 'section_count', None),
        daypart_split=getattr(restaurant, 'daypart_split', None),
        delivery_pct=getattr(restaurant, 'delivery_pct', None),
        role_minimums_json=getattr(restaurant, 'role_minimums_json', None),
        sched_notes=_sched_notes_with_findings(restaurant_id, getattr(restaurant, 'sched_notes', None)),
        staff_availability=staff_availability or None,
        tz_name=getattr(restaurant, 'timezone', None),
        restaurant_id=restaurant_id,
        weather_forecast=weather_forecast or None,
        prior_schedule_summary=prior_schedule_summary or None,
        operational_scores=_op_scores,
        strength_thresholds=_strength_thresholds,
        leader_rules=_leader_rules,
        shift_profiles=_profiles,
        roster=roster_pairs or None,
        extra_blocks=extra_blocks or None,
        projected_revenue_override=revenue.get("value"),
        week_start=monday.strftime("%Y-%m-%d"),
        closed_dates=sorted(constraints.closed_dates),
        max_consecutive_days=constraints.compliance.get("max_consecutive_days"),
        # The SHIFT REQUIREMENTS table and the people blocks: the same
        # floors, demand, tenure and patterns the quality pass scores with.
        role_floors=constraints.role_floors or {},
        demand_by_date=signals_by_date,
        demand_by_day=_demand_by_day,
        tenure=_people["tenure"],
        leader_flags=_people["leader_flags"],
        prior_pattern=_people["prior_pattern"],
        experienced=_people["experienced"],
        focus=list(focus) if focus else None,
        borrowed_headcount=learning["borrowed_headcount"],
        # Half-hour needs from the sales curve and the section cap over the
        # front-of-house roles, for the requirements table (staffing_curve).
        hourly_profile=_safe_hourly_profile(restaurant_id),
        section_cap_roles=sorted(constraints.foh_roles or {"server"}),
        open_times=constraints.open_times or {},
        close_times=constraints.close_times or {},
        # What each call is sent (schedule audit 10/3/26): the dates a redo
        # writes and the kept days it is written against (P-9), the owner's
        # reason for it (PR-18), the days nobody can work (E-20), and the
        # rules apart from the rest so a department call gets its own (E-29).
        redo_dates=sorted(set(dates)) if dates else None,
        prior_rows=[dict(r) for r in (prior_rows or [])] or None,
        redo_note=_redo_note(owner_reason),
        unstaffable_dates=unstaffable,
        rules_constraints=constraints,
        extra_after_rules=extra_rest,
    )
    result = _generate_in_parts(analysis, shifts, roster_pairs, _gen_kwargs)
    # What the generation itself has to tell the owner, ahead of the review
    # (the job puts these at the top of it): days nobody could work (E-20),
    # a first week drafted with no history of its own (E-30).
    notes = list(result.get("generation_notes") or [])
    asked = set(dates) if dates else set(next_week_dates)
    result["unstaffable_dates"] = [{"date": d, "day": _date_of(d).strftime("%A"), "reasons": unstaffable[d]}
                                   for d in sorted(unstaffable) if d in asked]
    if result["unstaffable_dates"]:
        notes.insert(0, _unstaffable_line(result["unstaffable_dates"]))
    if no_history:
        result["starting_point"] = {"no_history": True, "floors": bool(constraints.role_floors or _role_mins),
                                    "borrowed": bool(learning.get("borrowed_headcount"))}
        notes.append(_starting_point_line(restaurant, result["starting_point"], learning.get("starting_payload")))
    result["generation_notes"] = notes
    result["rotation_plan"] = learning["rotation"]
    result["splh_objective"] = learning["splh_objective"]
    result["starting_headcount"] = learning["starting_payload"]
    result["holiday_lift"] = holiday
    result["splh_by_daypart"] = splh
    result["outcomes_by_daypart"] = outcomes
    result["fairness_ledger"] = ledger
    result["could_hold"] = could_hold
    result["projected_revenue_source"] = revenue_source_words(result.get("revenue_basis"), revenue.get("source"))
    # Where a cut measurably went worse here, for the optimizer (memory
    # audit 9/29/26, "what_worked").
    result["learned_worse"] = learned_worse_levers(restaurant_id)
    # Which labor target the budget was built against — the owner's goal
    # ("your goal of 26% by 12/31/26") or the setting (memory audit 9/29/26,
    # owner_goals): both the revenue and the target name what applied.
    try:
        import thresholds as _thr_s
        _t = _thr_s.target_for(restaurant, "labor")
        result["labor_target_source"] = _t.get("source")
        result["labor_target_label"] = _t.get("label")
    except Exception:
        pass
    try:
        import reservation_feeds as _rf
        result["reservation_feed"] = _rf.status(restaurant)
    except Exception:
        result["reservation_feed"] = None
    try:
        result["demand_data_through"] = _demand_data_through(restaurant_id)
    except Exception:
        result["demand_data_through"] = None
    result["restaurant_name"] = restaurant.name if restaurant else "Restaurant"
    result["demand_by_day"] = _demand_by_day
    result["demand_by_date"] = signals_by_date
    result["role_minimums"] = _parse_role_minimums(getattr(restaurant, "role_minimums_json", None))
    result["constraints"] = constraints
    if roster_pairs:
        result["roster"] = sorted(n for n, _r in roster_pairs)
    result["roster_roles"] = {n: r for n, r in roster_pairs}
    result["pairs"] = pairs
    result["reliability"] = reliability
    result["learned_patterns"] = learned
    result["pattern_conflicts"] = pattern_conflicts
    result["soft_requirements"] = soft_reqs
    try:
        from labor import historical_patterns as _hp_soft
        result["_soft_typical"] = {f"{d}|{p}": v for (d, p), v in
                                   ((_hp_soft(shifts) or {}).get("typical_headcount") or {}).items()}
    except Exception:
        result["_soft_typical"] = {}
    result["prior_published_rows"] = prior_published_rows
    result["weather_forecast"] = weather_forecast or []
    result["pending_time_off"] = {n: sorted(d) for n, d in constraints.pending_off.items()}
    return result


# Days a restaurant commonly closes. When the model writes nothing for one
# of these, that is a closure, not a missed day.
_CLOSURE_HOLIDAYS = ("Thanksgiving", "Christmas Day")


def _trading_weekdays(shifts) -> set:
    """Weekdays this restaurant actually trades, from its own shift history:
    a weekday counts when it carried shifts in at least half the weeks on
    file. No history means no opinion (every weekday trades)."""
    from datetime import datetime as _d
    weeks, by_wd = set(), {}
    for sh in shifts or []:
        try:
            d = _d.strptime(str(sh.get("date") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        wk = d.isocalendar()[:2]
        weeks.add(wk)
        by_wd.setdefault(d.weekday(), set()).add(wk)
    if len(weeks) < 2:
        return set(range(7))
    return {wd for wd, w in by_wd.items() if len(w) * 2 >= len(weeks)}


def _nobody_can_work(c, names, dates) -> dict:
    """{date: {reason: people}} for each of `dates` that nobody in `names` can
    legally work at all — approved time off, an unavailable weekday, a day
    marked off (Constraints.can_work, no daypart) — schedule audit 10/3/26
    E-20. An empty roster has no opinion: nothing is excluded."""
    names = [n for n in (names or []) if n]
    if c is None or not names:
        return {}
    out = {}
    for d in dates or []:
        reasons = {}
        for n in names:
            ok, why = c.can_work(n, d)
            if ok:
                break
            reasons[why or "unavailable"] = reasons.get(why or "unavailable", 0) + 1
        else:
            out[d] = reasons
    return out


def _unstaffable_line(items) -> str:
    """The review's warning for days nobody can work (E-20) — a "⚠" line, so
    the publish gate holds the week until the owner has seen it."""
    from time_utils import mdy
    days = [f"{it['day']} {mdy(it['date'])}" for it in items]
    which = days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1]
    one = len(days) == 1
    return (f"⚠ Nobody on the team can work {which} (time off or availability), so {'it was' if one else 'they were'} "
            f"left empty — mark {'it' if one else 'them'} closed or fix availability, then redo "
            f"{'that day' if one else 'those days'}.")


def _starting_point_line(restaurant, start: dict, payload: dict = None) -> str:
    """What a first week with no history of its own was drafted from (E-30)."""
    name = getattr(restaurant, "name", None) or "This restaurant"
    basis = ["your team"]
    if start.get("floors"):
        basis.append("your Role floors")
    if start.get("borrowed"):
        n = (payload or {}).get("n")
        basis.append(f"the staffing of {n}+ similar restaurants" if n else "the staffing of similar restaurants")
    said = basis[0] if len(basis) == 1 else ", ".join(basis[:-1]) + " and " + basis[-1]
    return (f"A starting point: {name} has no shift history of its own yet, so this week is drafted from {said}. "
            "Check it closely before you publish — your own weeks replace it as they come in.")


# Why the owner is redoing some days (PR-18), as chips on the redo screen:
# the key is what the route accepts and what is recorded (H1, redo_days); the
# words are what the owner tapped, and what the prompt is told they said.
REDO_REASONS = {
    "too_few": "Too few people on",
    "too_many": "Too many people on",
    "wrong_people": "The wrong people on",
    "times": "Start or end times are off",
    "manager": "Manager coverage is wrong",
    "overtime": "Too much overtime",
    "fairness": "Shifts aren't shared fairly",
    "other": "Something else",
}
REDO_REASON_MAX_CHARS = 300


def redo_reason_from(body: dict):
    """({"chip", "text"} or None, error or None) from a redo request's body:
    `reason` one of REDO_REASONS, `reason_text` the owner's own words (at
    most REDO_REASON_MAX_CHARS). Both optional."""
    body = body or {}
    chip = str(body.get("reason") or "").strip().lower() or None
    text = " ".join(str(body.get("reason_text") or "").split())
    if chip and chip not in REDO_REASONS:
        return None, "That reason isn't one of the choices — pick one, or say what's wrong in your own words."
    if len(text) > REDO_REASON_MAX_CHARS:
        return None, f"Keep what's wrong to {REDO_REASON_MAX_CHARS} characters."
    if not chip and not text:
        return None, None
    return {"chip": chip, "text": text or None}, None


def _redo_note(owner_reason):
    """The owner's redo reason as the prompt carries it (labor's redo_note):
    the chip's words, and their own text — fenced there, never instructions."""
    if not owner_reason:
        return None
    chip = (owner_reason or {}).get("chip")
    text = (owner_reason or {}).get("text")
    if not chip and not text:
        return None
    return {"chip": REDO_REASONS.get(chip) if chip else None, "text": text}


def _acceptable_missing(missing, trading_dates, roster_pairs, max_days) -> bool:
    """Whether dates the model left empty are a legitimate week: a closure
    holiday, or the day off a roster too small to cover every trading day
    has to take. Anything else is a missed day and fails as before."""
    from schedule_economics import _holiday_dates
    rest = []
    for d in missing:
        try:
            name = _holiday_dates(int(d[:4])).get(d)
        except Exception:
            name = None
        if name not in _CLOSURE_HOLIDAYS:
            rest.append(d)
    people = len({n for n, _r in (roster_pairs or []) if n})
    if people:
        capacity = people * int(max_days or 6)
        if len(rest) <= max(0, len(trading_dates) - capacity):
            return True
    return not rest


def schedule_learning_inputs(restaurant_id, restaurant, roster_pairs, shifts, splh, labor_target) -> dict:
    """The learned inputs a draft is generated against, each failing alone:
    the rotation plan (schedule_intel.rotation_plan), the sales-per-labor-
    hour objective (schedule_economics.splh_objective), and — only for a
    restaurant with no history of its own — a starting headcount borrowed
    from similar restaurants (intelligence.staffing), labelled borrowed."""
    import schedule_intel as _intel
    import schedule_economics as _econ
    roles = {n: r for n, r in (roster_pairs or []) if n}
    out = {"rotation": {}, "splh_objective": {"available": False}, "borrowed_headcount": None,
           "starting_payload": {"available": False}, "starting_block": ""}
    try:
        out["rotation"] = _intel.rotation_plan(restaurant_id, roster_roles=roles or None) or {}
    except Exception as _sfx:
        _soft_fail('rotation_plan', _sfx, restaurant_id)
    try:
        out["splh_objective"] = _econ.splh_objective(restaurant_id, splh=splh, labor_target_pct=labor_target)
    except Exception as _sfx:
        _soft_fail('splh_objective', _sfx, restaurant_id)
    try:
        from intelligence import staffing as _staffing
        start = _staffing.starting_headcount(restaurant_id, restaurant=restaurant, roster_roles=roles, shifts=shifts)
        out["starting_payload"] = _staffing.payload(start)
        if start.get("available"):
            out["borrowed_headcount"] = start["headcount"]
            out["starting_block"] = _staffing.prompt_block(start)
    except Exception as _sfx:
        _soft_fail('starting_headcount', _sfx, restaurant_id)
    return out


def _generate_in_parts(analysis, shifts, roster_pairs, kwargs):
    """Write the week (or a redo's dates) in as few model calls as fit, and
    keep everything a call finished.

    The calls are planned from the rows the dates usually carry
    (CHUNK_ROWS_PER_CALL a call): one call; else date slices; past three
    slices' worth, by department (the kitchen, then the front of house with
    every floor manager) and, for a department too big for one call a day,
    by groups of people. Each call sees every row already on the week — the
    kept days of a redo, pinned rows, what earlier calls wrote — as the rest
    of the week, and only its own dates are taken from its answer.

    A call's answer is salvaged, never thrown away (schedule audit 10/3/26
    P-34, E-28): every date it finished is kept; when it was cut (out of
    tokens, or out of the job's time) the date it was writing is not trusted
    and the dates it did not reach are written again, split smaller — dates
    in halves, then a single date by department, then a department's people
    in halves — up to MAX_SPLIT_DEPTH; a call that timed out is split the
    same way (P-22). A call that left a trading day empty is asked once more
    for just those days, named. A one-call week used to be thrown away whole
    for one missing day, and a cut slice failed the job outright after the
    calls before it had been paid for.

    Only trading days must carry shifts: the owner's closed weekdays and
    dates, weekdays the restaurant's own history shows it never trades, a
    Thanksgiving or Christmas the model leaves empty, the day off a
    one-person roster must take — and the days nobody on the roster can
    work (`unstaffable_dates`, E-20), which are never asked for at all.

    What could not be written — a day still empty after its retry, the time
    or the call budget running out, the provider failing partway — comes
    back in `unwritten_dates` (with why) beside the rest of the week, and is
    closed to every later pass: no backstop staffs a day the model did not.
    Only an answer with nothing in it fails the generation. A redo
    (`redo_dates`) writes only those dates; the caller keeps the rest."""
    from labor import generate_optimized_schedule
    from time_utils import restaurant_now
    from datetime import timedelta as _t0, datetime as _dt0
    kwargs = dict(kwargs)
    closed = set(kwargs.pop("closed_dates", None) or [])
    max_days = kwargs.pop("max_consecutive_days", None) or 6
    redo = kwargs.pop("redo_dates", None)
    kept = [dict(r) for r in (kwargs.pop("prior_rows", None) or [])]
    pinned = [dict(r) for r in (kwargs.pop("pinned_rows", None) or [])]
    unstaffable = dict(kwargs.pop("unstaffable_dates", None) or {})
    rules_c = kwargs.pop("rules_constraints", None)
    extra_rest = kwargs.pop("extra_after_rules", None)
    if kwargs.get("redo_note") is None:
        kwargs.pop("redo_note", None)
    # The prompt leaves closed dates out of its SHIFT REQUIREMENTS table.
    kwargs["closed_dates"] = sorted(closed)
    clock = current_clock()
    today0 = restaurant_now(kwargs.get("tz_name"), naive=True)
    monday0 = _week_monday(today0, kwargs.get("week_start"))
    all_dates = [(monday0 + _t0(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    trading_wd = _trading_weekdays(shifts)
    open_dates = [d for d in all_dates if d not in closed]
    trading_dates = [d for d in open_dates if _dt0.strptime(d, "%Y-%m-%d").weekday() in trading_wd]
    asked = [d for d in open_dates if redo is None or d in set(redo)]
    want = [d for d in asked if d not in unstaffable]
    if not open_dates:
        raise ScheduleGenerationError("The restaurant is marked closed every day of this week, so there is nothing to schedule.")
    if redo is not None and not asked:
        raise ScheduleGenerationError("The days you picked are marked closed, so there is nothing to redo.")
    if not want:
        raise ScheduleGenerationError(_nobody_message(sorted(set(asked) & set(unstaffable))))

    managers = set((getattr(rules_c, "managers", None) or {}).keys())
    expected = _expected_rows(shifts, roster_pairs)
    plan = _plan_tasks(expected * len(want) / max(1, len(open_dates)), want, roster_pairs, managers)
    if clock is not None:
        clock.plan(len(plan))
    call_cap = len(plan) + MAX_EXTRA_CALLS
    no_one = sorted(set(unstaffable) & set(all_dates))

    def _required(dates):
        miss = [d for d in dates if d in trading_dates]
        return [] if miss and _acceptable_missing(miss, trading_dates, roster_pairs, max_days) else miss

    queue = list(plan)
    written = []            # (date, employee key, line, valid) kept from the answers
    written_rows = []       # the same rows as dicts: the next calls' view of the week
    unwritten = {}          # date -> why it was not written
    slices_log, notes_by_part = [], []
    seconds, calls = 0.0, 0
    merged, failure = None, None
    labels = []
    while queue:
        t = queue.pop(0)
        if calls >= call_cap or (clock is not None and clock.model_seconds_left() < MIN_CALL_SECONDS):
            why = "calls" if calls >= call_cap else "time"
            for task in [t] + queue:
                for d in _required(task["dates"]):
                    unwritten.setdefault(d, why)
            queue = []
            break
        if t["label"] and t["label"] not in labels:
            labels.append(t["label"])
        dk = _call_kwargs(t, kwargs, rules_c, extra_rest, managers, written_rows, kept, pinned, redo, no_one, clock)
        whole = redo is None and t["people"] is None and t["dates"] == open_dates and not (kept or pinned)
        entry = {"dates": list(t["dates"]), "part": t["what"], "retried": False}
        try:
            if whole:
                part = generate_optimized_schedule(analysis, shifts, **dk)
            else:
                part = generate_optimized_schedule(analysis, shifts, week_slice=list(t["dates"]),
                                                   prior_rows=kept + pinned + written_rows, **dk)
        except Exception as e:
            calls += 1
            kind = _failure_kind(e)
            entry.update({"error": kind, "rows_by_date": {}, "missing": list(t["dates"])})
            slices_log.append(entry)
            smaller = _split_task(t, roster_pairs, managers) if _can_split(t) else []
            if kind == "timeout" and smaller and calls < call_cap \
                    and (clock is None or clock.model_seconds_left() >= MIN_CALL_SECONDS):
                # A call that ran out of time is not sent again as it was —
                # it would time out again in less — but re-planned smaller.
                queue[0:0] = smaller
                continue
            failure = e
            for task in [t] + queue:
                for d in _required(task["dates"]):
                    unwritten.setdefault(d, kind)
            queue = []
            break
        calls += 1
        seconds += float(part.get("generation_seconds") or 0)
        merged = merged or part
        tdates = set(t["dates"])
        lines = _answer_lines(part.get("schedule_csv", ""))
        if not whole:
            lines = [x for x in lines if x[0] in tdates]
        names = None if t["people"] is None else {" ".join(str(n).lower().split()) for n, _r in t["people"]}
        off_list = [x for x in lines if names is not None and x[1] not in names]
        if off_list:
            # A department call that wrote someone off its own list (E-29) is
            # never kept: that person's shifts are another part's to write.
            lines = [x for x in lines if x not in off_list]
            _quality_note("off_list", kwargs.get("restaurant_id"), len(off_list),
                          f"{len(off_list)} row(s) for people outside the {t['what'].lower()} part dropped")
        clash = _pinned_clashes(lines, pinned)
        if clash:
            lines = [x for x in lines if x not in clash]
        cut = bool(part.get("truncated")) or part.get("stop_reason") in ("max_tokens", "deadline", "timeout")
        done = [d for d in _complete_dates([x for x in lines if x[3]], cut) if whole or d in tdates]
        keep = [x for x in lines if (x[0] in done) or (not x[0] and not cut)]
        written.extend(keep)
        written_rows.extend(_line_row(x[2]) for x in keep if x[3])
        left = [d for d in t["dates"] if d not in done]
        rows_kept = sum(1 for x in keep if x[3])
        entry.update({"rows_by_date": _rows_by_date("x\n" + "\n".join(x[2] for x in lines)),
                      "seconds": part.get("generation_seconds"), "stop_reason": part.get("stop_reason"),
                      "cut": cut, "rows": rows_kept,
                      # What one row cost to write, measured, for re-sizing
                      # OUTPUT_TOKENS_PER_ROW against the schema in force.
                      "chars_per_row": (round(float(part["text_chars"]) / rows_kept, 1)
                                        if part.get("text_chars") and rows_kept else None),
                      "off_list_dropped": len(off_list), "pinned_dropped": len(clash)})
        if rows_kept:
            notes_by_part.append((t, done, list(part.get("narrative") or [])))
        if cut and left:
            entry["missing"] = left
            smaller = _split_task(dict(t, dates=left), roster_pairs, managers) if _can_split(t) else []
            if smaller and calls < call_cap:
                queue[0:0] = smaller
            else:
                for d in _required(left):
                    unwritten.setdefault(d, "cut")
        elif not cut:
            missing = _required(left)
            entry["missing"] = missing
            if missing and not t["retry"]:
                # One retry, told exactly which days it skipped, asked for
                # just those days with everything else in view.
                entry["retried"] = True
                queue.insert(0, dict(t, dates=missing, retry=True, missed=missing))
            elif missing:
                for d in missing:
                    unwritten.setdefault(d, "skipped")
        slices_log.append(entry)

    # A day is written whole or not at all: what one part wrote for a date
    # another part could not finish is not kept as though it were the day.
    kept_lines = [x for x in written if not x[0] or x[0] not in unwritten]
    if not any(x[3] for x in kept_lines):
        if failure is not None:
            raise failure
        gaps = [{"date": d, "day": _date_of(d).strftime("%A"), "why": unwritten.get(d, "skipped")}
                for d in (sorted(unwritten) or want)]
        why = "; ".join(f"{days}: {words}" for days, words, _n in _gap_groups(gaps))
        raise ScheduleGenerationError(
            f"No shifts could be written ({why}), so nothing was saved. "
            + ("Check those days' hours and who can work them, then generate again."
               if set(unwritten.values()) <= {"skipped"} else
               "Generate again — if it happens again, redo a few days at a time."))
    merged = dict(merged)
    merged["schedule_csv"] = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + \
        "\n".join(x[2] for x in kept_lines if x[2].strip())
    if len(notes_by_part) == 1 and calls == 1:
        merged["narrative"] = notes_by_part[0][2][:3]
    else:
        # Two bullets from each part, labelled with the days it wrote (M/D/YY:
        # they read "10-05–10-07").
        merged["narrative"] = [(f"{_PART_WORDS.get(t['label'], str(t['label'] or '').capitalize())} "
                                if t["people"] is not None else "") + f"{_dates_label(done)}: {b}"
                               for t, done, bullets in notes_by_part for b in bullets[:2]][:6]
    merged["summary"] = list(merged["narrative"])
    merged["generation_seconds"] = round(seconds, 1)
    merged["truncated"] = False
    merged["chunked"] = calls
    merged["departments"] = labels
    merged["slices"] = slices_log
    merged["pinned_rows"] = pinned
    merged["generated_dates"] = sorted({x[0] for x in kept_lines if x[0] and x[3]})
    gaps = sorted(unwritten)
    merged["unwritten_dates"] = [{"date": d, "day": _date_of(d).strftime("%A"), "why": unwritten[d]} for d in gaps]
    merged["generation_notes"] = [_unwritten_line(merged["unwritten_dates"])] if (gaps and redo is None) else []
    # Dates the week leaves empty — closed, never traded, nobody able, not
    # written — are closed to every pass after this one, so no backstop
    # staffs a day the model did not. A redo's unwritten dates keep the
    # owner's rows (the caller puts them back), so they stay open.
    empty = set(_missing_dates(merged["schedule_csv"], asked)) | (set(unstaffable) & set(asked))
    if redo is not None:
        empty -= set(gaps)
    merged["closed_dates"] = sorted(closed | empty)
    return merged


# A call is not started with less of the job's model time left than this.
MIN_CALL_SECONDS = 60
# How a department part is named to the owner in the draft's note.
_PART_WORDS = {"KITCHEN": "Kitchen", "FRONT OF HOUSE": "Front of house", "STAFF": "Staff"}


def _plan_tasks(rows: float, dates: list, roster_pairs, managers=()) -> list:
    """The calls the dates are written in, each planned to fit
    CHUNK_ROWS_PER_CALL: one; else date slices; past three slices' worth,
    each department in date slices — and a department too big for one call
    a day in groups of people. At most three slices of two departments used
    to be the ceiling, so a 500-person roster planned ~290-row calls against
    a 160-row budget (SCHED-24)."""
    def task(ds, people=None, label=None, gi=0, gn=1):
        what = "STAFF" if label is None else label + (f" GROUP {gi + 1} OF {gn}" if gn > 1 else "")
        return {"dates": list(ds), "people": people, "label": label, "group": (gi, gn), "what": what,
                "depth": 0, "retry": False, "missed": None}
    if rows <= CHUNK_ROWS_PER_CALL:
        return [task(dates)]
    if rows <= CHUNK_ROWS_PER_CALL * 3 and len(dates) > 1:
        parts = min(len(dates), -(-int(rows) // CHUNK_ROWS_PER_CALL))
        size = -(-len(dates) // parts)
        return [task(dates[i:i + size]) for i in range(0, len(dates), size)]
    out = []
    per_head = rows / max(1, len(roster_pairs or []))
    for label, people in _departments(roster_pairs, managers).items():
        k, p = _plan_calls(per_head * len(people), len(dates), min_slices=2, max_groups=len(people))
        size_p = -(-len(people) // k)
        chunks = [people[i:i + size_p] for i in range(0, len(people), size_p)]
        size_d = -(-len(dates) // p)
        slices = [dates[i:i + size_d] for i in range(0, len(dates), size_d)]
        for ci, chunk in enumerate(chunks):
            for sl in slices:
                out.append(task(sl, chunk, label, ci, len(chunks)))
    return out or [task(dates)]


def _can_split(t) -> bool:
    if t["depth"] >= MAX_SPLIT_DEPTH:
        return False
    return len(t["dates"]) > 1 or t["people"] is None or len(t["people"]) > 1


def _split_task(t, roster_pairs, managers=()) -> list:
    """A cut or timed-out call's work, smaller (E-28): its dates in halves;
    a single date by department (the kitchen first, the managers' part last
    so it sees whom they cover); a department's people in halves, managers
    together in the later half."""
    nxt = dict(t, depth=t["depth"] + 1, retry=False, missed=None)
    ds = list(t["dates"])
    if len(ds) > 1:
        h = (len(ds) + 1) // 2
        return [dict(nxt, dates=ds[:h]), dict(nxt, dates=ds[h:])]
    people = t["people"]
    label = t["label"] or "STAFF"
    if people is None:
        depts = _departments(roster_pairs, managers)
        if len(depts) > 1:
            return [dict(nxt, people=list(ps), label=lb, group=(0, 1), what=lb) for lb, ps in depts.items()]
        people = list(roster_pairs or [])
    if len(people) <= 1:
        return []
    mgr = {str(m).strip().lower() for m in (managers or ())}
    ordered = [p for p in people if str(p[0]).strip().lower() not in mgr] + \
              [p for p in people if str(p[0]).strip().lower() in mgr]
    h = (len(ordered) + 1) // 2
    return [dict(nxt, people=half, label=label, group=(i, 2), what=f"{label} GROUP {i + 1} OF 2")
            for i, half in enumerate((ordered[:h], ordered[h:])) if half]


def _call_kwargs(t, kwargs, rules_c, extra_rest, managers, written_rows, kept, pinned, redo, no_one, clock) -> dict:
    """The keyword arguments one call is sent: its roster and rules when it
    is a department's, and what it is told about the rest of the week."""
    dk = dict(kwargs)
    blocks = kwargs.get("extra_blocks") or ""
    if t["people"] is not None:
        dk["roster"] = list(t["people"])
        if rules_c is not None:
            # Only this part's people: its managers (or where they are), its
            # floors, its per-person limits (E-29, PR-17).
            blocks = _chunk_rules_block(rules_c, t["people"]) + (extra_rest or "")
        blocks += _department_addendum(t, written_rows, managers)
    add = ""
    if redo is not None and kept:
        add += _redo_addendum(t["dates"], kept)
    pins = [r for r in pinned if r.get("date") in set(t["dates"])]
    if pins:
        add += _fixed_shifts_block(pins)
    if no_one:
        add += ("\n\nNOBODY ON THE ROSTER CAN WORK " + ", ".join(f"{_date_of(d).strftime('%A')} {d}" for d in no_one)
                + " (time off or availability): " + ("it stays" if len(no_one) == 1 else "they stay")
                + " empty and no other part writes " + ("it" if len(no_one) == 1 else "them") + ".")
    if t["retry"] and t.get("missed"):
        add += ("\n\nYOUR PREVIOUS ANSWER WROTE NO SHIFTS FOR " + ", ".join(t["missed"]) +
                ". Every date in this request must have a full day of shifts across every role that normally works it.")
    if blocks or add:
        dk["extra_blocks"] = blocks + add
    if clock is not None:
        dk["deadline"] = clock.model_deadline
    return dk


def _chunk_rules_block(c, people) -> str:
    """schedule_rules.prompt_block for one part's people only: its managers,
    minors, closers, salaried people and per-person limits, the floors,
    certificates and after-close stays of its own roles, the kitchen stations
    only for a kitchen part. A kitchen-only call used to be told every
    manager's name and the whole roster's floors, then forbidden to schedule
    anyone off its list (schedule audit 10/3/26 E-29, PR-17)."""
    import copy

    def key(n):
        return " ".join(str(n or "").lower().split())
    low = {key(n) for n, _r in people}
    roles = {(r or "").strip().lower() for _n, r in people if r}
    fams = {c.family(r) for r in roles}

    def mine(role):
        role = (role or "").strip().lower()
        return role in roles or c.family(role) in fams
    view = copy.copy(c)
    view.roster_names = [n for n in (c.roster_names or []) if key(n) in low] or [n for n, _r in people]
    view.managers = {k: v for k, v in (c.managers or {}).items() if key(k) in low}
    view.minors = {k for k in (c.minors or set()) if key(k) in low}
    view.keyholders = {k for k in (c.keyholders or set()) if key(k) in low}
    view.salaried = {k for k in (c.salaried or set()) if key(k) in low}
    view.role_floors = {r: s for r, s in (c.role_floors or {}).items() if mine(r)}
    view.close_mins = {r: m for r, m in (c.close_mins or {}).items() if mine(r)}
    view.role_requirements = {r: v for r, v in (c.role_requirements or {}).items() if mine(r)}
    if not any(w in r for r in roles for w in _BOH_WORDS):
        view.stations = {}
    return _rules.prompt_block(view)


def _department_addendum(t, written_rows, managers) -> str:
    """What a department call is told about the rest: that its list is all it
    writes, where the managers are, and — for the part that carries them —
    when the rest of the staff is on, so the manager rule covers them too
    (PR-17: hand the other department's rows over as context)."""
    mgr = {str(m).strip().lower() for m in (managers or ())}
    here = any(str(n).strip().lower() in mgr for n, _r in t["people"])
    out = (f"\n\nTHIS REQUEST COVERS ONLY THE {t['what']} ROSTER LISTED ABOVE. The rest of the staff is written "
           "separately; do not schedule anyone not on this list.")
    if mgr and not here:
        out += (" The managers are on another part's list, written after this one with these shifts in view — "
                "schedule no manager here.")
    if here:
        spans = _span_lines(written_rows, t["dates"], exclude={" ".join(str(n).lower().split()) for n, _r in t["people"]})
        if spans:
            out += ("\n\nTHE REST OF THE STAFF ON THESE DATES (already written by the other parts — at every minute "
                    "any of them is on, a manager on this list is on too):\n" + "\n".join(spans))
    return out


def _span_lines(rows, dates, exclude=()) -> list:
    """Per date, the first start and last end among `rows` of people not in
    `exclude`: when somebody else is on the floor that day."""
    out = []
    for d in dates:
        lo = hi = None
        for r in rows:
            if r.get("date") != d or " ".join(str(r.get("employee") or "").lower().split()) in exclude:
                continue
            s, e = _rules.parse_minutes(r.get("shift_start")), _rules.parse_minutes(r.get("shift_end"))
            if s is None or e is None:
                continue
            if e <= s:
                e += 24 * 60
            lo = s if lo is None else min(lo, s)
            hi = e if hi is None else max(hi, e)
        if lo is not None:
            out.append(f"  {_date_of(d).strftime('%A')} {d}: {_clock_words(lo)} to {_clock_words(hi)}")
    return out


def _redo_addendum(dates, kept) -> str:
    """For a redo: the rest of the week is kept and never written; the kept
    shifts right next to the redone dates bound rest between shifts and runs
    of days (P-9, E-21: the model never saw the kept days at all)."""
    out = ("\n\nTHE REST OF THIS WEEK IS KEPT AS THE OWNER HAS IT — only the dates above are being redone. Its "
           "shifts are counted under ALREADY WRITTEN; write nothing for any other date.")
    near = _neighbour_lines(kept, dates)
    if near:
        out += ("\nThe kept shifts right next to the dates being redone (rest between shifts and days in a row "
                "are checked across them):\n" + "\n".join(near))
    return out


def _neighbour_lines(kept, dates, cap: int = 60) -> list:
    """Per person, their kept shift the day before a redone date (when it
    ends) and the day after one (when it starts)."""
    from datetime import timedelta as _td
    redo = set(dates or [])
    before, after = set(), set()
    for d in redo:
        day = _date_of(d)
        prev, nxt = (day - _td(days=1)).isoformat(), (day + _td(days=1)).isoformat()
        if prev not in redo:
            before.add(prev)
        if nxt not in redo:
            after.add(nxt)
    by = {}
    for r in kept or []:
        d, name = r.get("date"), (r.get("employee") or "").strip()
        if not name or d not in before | after:
            continue
        s, e = _rules.parse_minutes(r.get("shift_start")), _rules.parse_minutes(r.get("shift_end"))
        slot = by.setdefault(name, {})
        if d in before and e is not None:
            if s is not None and e <= s:
                e += 24 * 60
            slot[d] = ("until", max(e, slot[d][1]) if d in slot else e)
        if d in after and s is not None:
            slot[d] = ("from", min(s, slot[d][1]) if d in slot else s)
    out = []
    for name in sorted(by):
        bits = [f"{_date_of(d).strftime('%A')} {d} {how} {_clock_words(m)}" for d, (how, m) in sorted(by[name].items())]
        out.append(f"  {name}: " + "; ".join(bits))
    return out[:cap]


def _fixed_shifts_block(rows) -> str:
    """Pinned rows on a call's own dates (the manager plan's), listed as
    shifts: kept exactly, counted, never written again."""
    lines = [f"  {_date_of(r['date']).strftime('%A')} {r['date']}: {r.get('employee')} ({r.get('role') or 'shift'}) "
             f"{r.get('shift_start')} to {r.get('shift_end')}"
             for r in sorted(rows, key=lambda r: (r.get("date") or "", r.get("shift_start") or "", r.get("employee") or ""))]
    return ("\n\nFIXED SHIFTS ON THESE DATES (already on the schedule and kept exactly as they are — count them toward "
            "coverage, the manager rule and each person's hours; never write them again):\n" + "\n".join(lines))


def _clock_words(m) -> str:
    m = int(m) % (24 * 60)
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def _answer_lines(csv_text: str) -> list:
    """[(date, employee key, line, valid)] for each data line of a call's
    answer, in the order it was written. `valid` is the job's parser keeping
    the row: a date, a name and both times (a row with no times, 3-5
    columns, used to count as the day written and then be dropped, SCHED-42)."""
    out = []
    for line in (csv_text or "").split("\n")[1:]:
        if not line.strip():
            continue
        cols = [c.strip().strip('"').strip() for c in line.split(",", 7)]
        date = cols[0] if re.match(r"^\d{4}-\d{2}-\d{2}$", cols[0]) else ""
        emp = " ".join(cols[2].lower().split()) if len(cols) > 2 else ""
        valid = len(cols) >= 6 and bool(date) and bool(emp)
        if valid and _rules.parse_minutes(cols[4]) is None and _rules.parse_minutes(cols[3]) is None:
            valid = False          # no start time where one belongs (nor one column left of it)
        if valid and _rules.parse_minutes(cols[5]) is None and _rules.parse_minutes(cols[4]) is None:
            valid = False
        out.append((date, emp, line, valid))
    return out


def _line_row(line: str) -> dict:
    cols = [c.strip() for c in line.split(",", 7)]
    cols += [""] * (8 - len(cols))
    return {"date": cols[0], "day": cols[1], "employee": cols[2], "role": cols[3],
            "shift_start": cols[4], "shift_end": cols[5], "scheduled_hours": cols[6]}


def _complete_dates(valid_lines, cut: bool) -> list:
    """The dates a call finished, in the order written: every date it wrote,
    less — when it was cut — the date it was writing when it stopped. An
    answer cut after going back to a date it had left was not writing day by
    day, so none of its dates can be trusted whole."""
    order, seen, last = [], set(), None
    for x in valid_lines:
        d = x[0]
        if d == last:
            continue
        if d in seen:
            if cut:
                return []
        else:
            seen.add(d)
            order.append(d)
        last = d
    return order[:-1] if cut else order


def _pinned_clashes(lines, pinned) -> list:
    """Answer lines that put a pinned row's person on at an overlapping time
    that day — the pinned row is the one kept."""
    if not pinned:
        return []
    held = {}
    for r in pinned:
        s, e = _rules.parse_minutes(r.get("shift_start")), _rules.parse_minutes(r.get("shift_end"))
        if s is None or e is None:
            continue
        held.setdefault((" ".join(str(r.get("employee") or "").lower().split()), r.get("date")), []).append(
            (s, e + 24 * 60 if e <= s else e))
    out = []
    for x in lines:
        spans = held.get((x[1], x[0]))
        if not spans or not x[3]:
            continue
        cols = [c.strip() for c in x[2].split(",", 7)]
        s, e = _rules.parse_minutes(cols[4]), _rules.parse_minutes(cols[5])
        if s is None or e is None:
            continue
        e = e + 24 * 60 if e <= s else e
        if any(s < pe and ps < e for ps, pe in spans):
            out.append(x)
    return out


def _quality_note(kind, restaurant_id, n, detail):
    """An AI-quality finding about an answer (fix round G #58), not a failing job."""
    import ai_utils as _ai_q
    _ai_q.record_quality_event("labor_schedule", kind, restaurant_id=restaurant_id, action="labor_schedule",
                               n=n, detail=detail)


def _dates_label(dates) -> str:
    """M/D/YY, or the first and last of a run."""
    from time_utils import mdy
    ds = sorted(dates or [])
    if not ds:
        return ""
    return mdy(ds[0]) if len(ds) == 1 else f"{mdy(ds[0])}–{mdy(ds[-1])}"


def _failure_kind(exc) -> str:
    """What stopped a call, as the job's owner sentences are keyed:
    budget, provider_down, data, refused, generation, timeout, provider,
    config or other."""
    import ai_utils as _ai
    if isinstance(exc, _ai.AIBudgetExceeded):
        return "budget"
    if isinstance(exc, _ai.AIProviderDown):
        return "provider_down"
    if isinstance(exc, _ai.DataNotReady):
        return "data"
    if isinstance(exc, _ai.AIRefused):
        return "refused"
    if isinstance(exc, ScheduleGenerationError):
        return "generation"
    reason = _ai.classify_error(exc)
    if reason == "timeout":
        return "timeout"
    if reason in ("connection", "rate_limit", "overloaded", "server"):
        return "provider"
    if reason in ("auth", "permission", "credit", "not_found", "too_large", "bad_request"):
        return "config"
    return "other"


_UNWRITTEN_WHY = {
    "skipped": "the model left them empty even when asked again",
    "cut": "the model ran out of room writing them, even in smaller parts",
    "calls": "the generation used every call it is allowed",
    "time": "the generation ran out of time",
    "timeout": "the model took too long on them",
    "budget": "AI is paused — this account has reached its AI limit",
    "provider_down": "Cavnar AI's model provider stopped responding partway",
    "provider": "Cavnar AI's model provider failed partway",
    "data": "the data behind them stopped being current partway",
    "refused": "the model declined to write them",
    "config": "Cavnar AI couldn't reach its model partway (we've been alerted)",
    "generation": "they couldn't be written",
    "other": "something failed partway (we've been alerted)",
}


def _gap_groups(items) -> list:
    """[(days words, why words)] for unwritten dates, grouped by why, in
    _UNWRITTEN_WHY's order: "Saturday 10/10/26 and Sunday 10/11/26" with
    "the model left them empty even when asked again"."""
    from time_utils import mdy
    by = {}
    for it in items:
        by.setdefault(it["why"], []).append(f"{it['day']} {mdy(it['date'])}")
    order = list(_UNWRITTEN_WHY)
    out = []
    for why in sorted(by, key=lambda w: order.index(w) if w in order else len(order)):
        days = by[why]
        words = _UNWRITTEN_WHY.get(why, _UNWRITTEN_WHY["other"])
        if len(days) == 1:
            words = words.replace("them", "it")
        out.append((days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1], words, len(days)))
    return out


def _unwritten_line(items) -> str:
    """The review's warning for days the generation could not write — a "⚠"
    line, so the publish gate holds the week until they are redone. The
    rest of the week is kept (P-34): the days already paid for are not
    thrown away for the ones that failed."""
    groups = _gap_groups(items)
    one = len(items) == 1
    tail = f" The rest of the week is here; redo {'that day' if one else 'those days'} before you publish."
    if len(groups) == 1:
        days, why, n = groups[0]
        return f"⚠ {days} {'was' if n == 1 else 'were'} not written — {why}.{tail}"
    return "⚠ Not written: " + "; ".join(f"{days} — {why}" for days, why, _n in groups) + "." + tail


def _not_redone_line(dates) -> str:
    """A redo day the model could not write keeps the owner's rows (P-34):
    said, so a redo that changed nothing on a day never reads as done."""
    from time_utils import mdy
    days = [f"{_date_of(d).strftime('%A')} {mdy(d)}" for d in dates]
    which = days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1]
    return (f"{which} couldn't be redone just now, so {'it is' if len(days) == 1 else 'they are'} as "
            f"{'it was' if len(days) == 1 else 'they were'} — redo {'it' if len(days) == 1 else 'them'} again.")


def _nobody_message(dates) -> str:
    """The refusal when nobody on the team can work any day asked for (E-20)."""
    from time_utils import mdy
    days = [f"{_date_of(d).strftime('%A')} {mdy(d)}" for d in dates]
    which = (days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1]) if days else "any of these days"
    return (f"Nobody on the team can work {which} (time off or availability), so there's nothing to draft. "
            "Check availability and time off, or mark the day closed, then generate again.")


def _plan_calls(rows: float, n_dates: int, min_slices: int = 1, max_groups: int = 200) -> tuple:
    """(people_groups, date_slices) with the fewest calls for which every
    call's expected rows — rows × (its share of people) × (its days / the
    dates) — fit CHUNK_ROWS_PER_CALL."""
    n = max(1, int(n_dates or 1))
    best = None
    for k in range(1, max(1, int(max_groups)) + 1):
        for p in range(max(1, min(min_slices, n)), n + 1):
            longest = -(-n // p)
            if rows / k * longest / n <= CHUNK_ROWS_PER_CALL:
                calls = k * p
                if best is None or calls < best[0]:
                    best = (calls, k, p)
                break
        if best is not None and best[0] <= k:
            break
    return (best[1], best[2]) if best else (max(1, int(max_groups)), n)


def _rows_by_date(csv_text: str) -> dict:
    """Rows per date — counting only rows the job's parser will keep
    (_answer_lines' `valid`)."""
    out = {}
    for date, _emp, _line, valid in _answer_lines(csv_text):
        if valid:
            out[date] = out.get(date, 0) + 1
    return out


def _missing_dates(csv_text: str, dates: list) -> list:
    by = _rows_by_date(csv_text)
    return [d for d in (dates or []) if not by.get(d)]


def _pretty_dates(dates: list) -> list:
    from datetime import datetime as _d
    out = []
    for d in dates or []:
        try:
            out.append(_d.strptime(d, "%Y-%m-%d").strftime("%A"))
        except ValueError:
            out.append(str(d))
    return out


_BOH_WORDS = ("cook", "prep", "dish", "chef", "line", "kitchen", "pantry", "saute", "sauté", "pizza", "grill",
              "fry", "expo", "baker", "pastry", "sous", "kds")


def _departments(roster_pairs, managers=None) -> dict:
    """{"KITCHEN": [(name, role)], "FRONT OF HOUSE": [...]} by role words.
    Every floor manager (Constraints.managers, by lower-case name) goes to the
    front of house, last on its list: one part carries the manager rule with
    all of them on it, written after the kitchen with its shifts in view
    (E-29: a kitchen call was told the manager rule and forbidden to
    schedule the managers)."""
    mgr = {str(m).strip().lower() for m in (managers or ())}
    out = {"KITCHEN": [], "FRONT OF HOUSE": []}
    held = []
    for name, role in roster_pairs or []:
        if str(name or "").strip().lower() in mgr:
            held.append((name, role))
            continue
        r = (role or "").lower()
        out["KITCHEN" if any(w in r for w in _BOH_WORDS) else "FRONT OF HOUSE"].append((name, role))
    if held:
        (out["FRONT OF HOUSE"] if (out["FRONT OF HOUSE"] or not out["KITCHEN"]) else out["KITCHEN"]).extend(held)
    return {k: v for k, v in out.items() if v}


def _last_published_csv(restaurant_id, before_date):
    """The newest published week that ended before `before_date`."""
    from models import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
            "AND week_end < ? AND week_end >= date(?, '-2 days') ORDER BY week_end DESC, id DESC LIMIT 1",
            (restaurant_id, before_date, before_date)).fetchone()
    finally:
        conn.close()
    return (row["schedule_csv"] if row else "") or ""


COHORT_BLOCK_HEADER = "HOW THIS RESTAURANT'S LABOR COMPARES"
_COHORT_METRICS = ("labor_hours_per_1k_28d", "labor_hours_per_1k_day_28d", "labor_hours_per_1k_night_28d")


def cohort_comparisons(restaurant_id, restaurant) -> list:
    """The Benchmark Engine's peer comparisons for hours per $1k of sales
    (whole day, lunch/day, dinner/night) that have a like-for-like band of
    the restaurant's own type. Hours per $1k is an economics metric, so an
    all-types band is never one of them (BM1-4, BM2-3, BM3-7). Never raises."""
    try:
        from intelligence import engine
        out = []
        for metric in _COHORT_METRICS:
            cm = engine.compare(restaurant_id, metric, kinds=("peers",), restaurant=restaurant)
            peers = next((c for c in cm.get("comparisons") or () if c.get("kind") == "peers"), {})
            if peers.get("available") and peers.get("value") is not None and \
                    peers.get("standing") not in (None, "unmeasured"):
                out.append(cm)
        return out
    except Exception:
        return []


def cohort_facts(restaurant_id, restaurant=None) -> list:
    """The response_validation benchmark facts behind the cohort block, so
    a schedule note that quotes the band binds to it (BM3-3)."""
    if restaurant is None:
        try:
            from models import get_restaurant
            restaurant = get_restaurant(restaurant_id)
        except Exception:
            restaurant = None
    facts = []
    for cm in cohort_comparisons(restaurant_id, restaurant):
        facts += cm.get("facts") or []
    return facts


def _cohort_block(restaurant_id, restaurant) -> str:
    """Hours per $1k of sales against the restaurant's own type on Cavnar —
    a ratio, no dollars, no names — in the engine's one wording
    (engine.prompt_lines: the peer group and how it was chosen, set or
    guessed, measured at k of m, as of when, comparison strength %), and
    only when a like-for-like band clears the engine's floors."""
    try:
        from intelligence import engine
        comps = cohort_comparisons(restaurant_id, restaurant)
        if not comps:
            return ""
        lines = [f"  {ln}" for ln in engine.prompt_lines(comps)]
        # This restaurant's own figure beside each band.
        for i, cm in enumerate(comps):
            peers = next(c for c in cm["comparisons"] if c.get("kind") == "peers")
            lines[i] += f" This restaurant runs {float(peers['value']):g} labor hours per $1k of sales."
        return (f"\n\n{COHORT_BLOCK_HEADER} (an anonymous peer-group ratio — a reason to hold the line on "
                "hours where it runs heavy, never a reason to add; no ranking word under 75% comparison "
                "strength):\n" + "\n".join(lines))
    except Exception:
        return ""


def _could_hold_block(could_hold: dict) -> str:
    if not could_hold:
        return ""
    lines = [f"  {n}: could hold {', '.join(roles)}" for n, roles in sorted(could_hold.items())]
    return ("\n\nTRAINED UP (people who have worked a station beside a closer often enough to hold it — "
            "a legitimate cross-training option before adding headcount):\n" + "\n".join(lines))


def _demand_data_through(restaurant_id) -> dict:
    """When the daily sales the demand forecast reads were last written,
    and whether that is recent enough to trust. Nothing on the screen said
    the forecast was three months stale."""
    from datetime import date as _date, datetime as _dt
    from models import get_conn as _gc
    conn = _gc()
    try:
        # A day with a real sales figure: NULL is a missing figure, and rows
        # written before that rule carry 0 for one. `sales IS NOT NULL`
        # alone let a feed that kept landing shifts with no sales read as
        # current — the forecast called itself fresh on 20 days of $0 (CA3 F3).
        row = conn.execute("SELECT MAX(date) AS d FROM labor_daily_history WHERE restaurant_id=? "
                           "AND sales IS NOT NULL AND sales > 0",
                           (restaurant_id,)).fetchone()
    finally:
        conn.close()
    last = row["d"] if row else None
    if not last:
        return {"date": None, "days_ago": None, "blind": True}
    try:
        days = (_date.today() - _dt.strptime(str(last)[:10], "%Y-%m-%d").date()).days
    except ValueError:
        return {"date": last, "days_ago": None, "blind": True}
    return {"date": str(last)[:10], "days_ago": days, "blind": days > 14}


def _pairs_block(pairs: dict, roster_pairs: list) -> str:
    if not pairs or not (pairs.get("prefer") or pairs.get("avoid")):
        return ""
    names = {n.lower(): n for n, _r in (roster_pairs or [])}
    def _show(p):
        a, b = sorted(names.get(x, x.title()) for x in p)
        return f"{a} and {b}"
    lines = []
    for p in sorted(pairs.get("avoid") or set(), key=sorted):
        lines.append(f"  keep apart: {_show(p)}")
    for p in sorted(pairs.get("prefer") or set(), key=sorted):
        lines.append(f"  work well together: {_show(p)}")
    return "\n\nPAIRINGS (the owner's own notes on who works with whom):\n" + "\n".join(lines)


def _reliability_block(reliability: dict) -> str:
    if not reliability:
        # Nobody's attendance has been watched here (no clock-in check, no
        # published week against the punches yet): said so, never implied
        # to be good (memory audit 9/29/26, attendance).
        return ("\n\nATTENDANCE: not watched yet at this restaurant — nobody's no-show record is known. Do "
                "not assume anyone is reliable or unreliable.")
    risky = sorted(((n, r) for n, r in (reliability or {}).items()
                    if float(r.get("no_show_rate") or 0) >= 0.2), key=lambda kv: -kv[1]["no_show_rate"])
    if not risky:
        return ""
    # Chosen on the smoothed rate (staff_settings.reliability); said with the
    # raw count, which is what actually happened.
    lines = [f"  {n}: missed {r['no_shows']} of {r['shifts']} watched shifts" if r.get("no_shows") is not None
             else f"  {n}: missed {int(round(r['no_show_rate'] * 100))}% of {r['shifts']} watched shifts"
             for n, r in risky[:8]]
    return ("\n\nATTENDANCE (from shifts somebody watched, recent ones counting most): the people below miss "
            "shifts often. Do not leave any of them alone in "
            "a role, and do not rely on them for the busiest shift of the week; a second body alongside them is the fix, "
            "not fewer hours:\n" + "\n".join(lines))


def _parse_role_minimums(raw):
    """{role: floor} from the settings textarea, or {} when it is junk.

    An owner typing malformed JSON into a settings box must never be the
    reason a schedule fails to generate.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k): int(v) for k, v in (parsed or {}).items() if int(v or 0) > 0}
    except Exception:
        return {}


# Async schedule generation is tracked in ops.async_jobs (a table), not a
# module dict — see ops.start_async_job for why. This alias is kept so the
# admin console and tests have one name to reach for.
import ops as _ops

# Matches a clock time like "8:00am"/"3:00 pm" — a real role name (Server,
# Prep Cook, Carry Out, ...) never looks like this, which is what makes it a
# reliable signal that a row's `day` field was dropped and every field after
# `date` shifted one position left. See _run_schedule_job's row-repair logic.
_TIME_FIELD_RE = re.compile(r'^\d{1,2}:\d{2}\s*(am|pm)$', re.IGNORECASE)
_WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def _reconcile_scheduled_hours(row):
    """Recompute scheduled_hours from the row's own shift times.

    The model writes this column itself, and nothing checked it: _row_is_sane
    only asked whether the value parsed as a float. A row reading
    "11:00am,7:00pm,12.0" is an 8-hour shift labelled 12, and that number is
    what the week's total, the labor-budget comparison and the projected
    labor cost are all summed from — so the owner's labor percentage drifts
    by whatever the model happened to write.

    Times are the source of truth: they are what a manager reads off the
    printed schedule and what the staff actually work. An overnight shift
    (end before start) is treated as crossing midnight. Returns the
    correction size in hours, or 0.0 when the row was already right or its
    times can't be parsed.
    """
    start = _parse_time_to_minutes(row.get("shift_start", ""))
    end = _parse_time_to_minutes(row.get("shift_end", ""))
    if start is None or end is None:
        return 0.0
    span = end - start
    if span < 0:
        span += 24 * 60          # closing shift running past midnight
    correct = round(span / 60, 1)
    raw_hours = (row.get("scheduled_hours") or "").strip() if isinstance(row.get("scheduled_hours"), str) \
        else row.get("scheduled_hours")
    if raw_hours in (None, ""):
        stated = None            # nothing was stated, so nothing was wrong
    else:
        try:
            stated = round(float(raw_hours), 1)
        except (ValueError, TypeError):
            stated = None
    if stated is None:
        row["scheduled_hours"] = str(correct)
        return 0.0
    drift = round(correct - stated, 1)
    if abs(drift) < 0.1:
        return 0.0          # already right — leave the row exactly as written
    row["scheduled_hours"] = str(correct)
    row["hours_corrected_from"] = str(stated)
    return drift


def _parse_time_to_minutes(t: str):
    """Parses a "9:00pm"/"9:30 am" style string (the exact format the AI is
    instructed to always produce) to minutes-since-midnight, or None if it
    doesn't match that format at all."""
    if not t or not _TIME_FIELD_RE.match(t.strip()):
        return None
    t_clean = t.strip().lower().replace(" ", "")
    period, hm = t_clean[-2:], t_clean[:-2]
    try:
        hour_str, minute_str = hm.split(":")
        hour, minute = int(hour_str), int(minute_str)
    except (ValueError, IndexError):
        return None
    if period == "pm" and hour != 12:
        hour += 12
    if period == "am" and hour == 12:
        hour = 0
    return hour * 60 + minute


def _format_minutes_to_time(total_minutes: int) -> str:
    """Inverse of _parse_time_to_minutes — "9:00pm" style."""
    total_minutes %= 24 * 60
    hour24, minute = divmod(total_minutes, 60)
    period = "am" if hour24 < 12 else "pm"
    hour12 = hour24 % 12 or 12
    return f"{hour12}:{minute:02d}{period}"


def _summarize_schedule_csv_by_day_role(csv_text: str) -> dict:
    """Compact day -> {role: {"count": N, "hours": H}} rollup of a past
    generation's schedule_csv — enough detail (headcount and hours per
    role per day) for the AI to describe a genuine week-over-week
    difference in its next "what changed and why" summary, without
    feeding it the full 100-200 row CSV again. Used to give
    generate_optimized_schedule something concrete to compare against —
    previously the summary only ever compared against historical shift
    patterns (TYPICAL HEADCOUNT), never the restaurant's own actual prior
    generated schedule, which is what "what changed" should mean to an
    owner reading it week to week.
    """
    import csv as _csv_mod, io as _io_mod
    summary: dict = {}
    try:
        for row in _csv_mod.DictReader(_io_mod.StringIO(csv_text or "")):
            day = row.get("day", "")
            role = row.get("role", "")
            if not day or not role:
                continue
            try:
                hrs = float(row.get("scheduled_hours") or 0)
            except (ValueError, TypeError):
                hrs = 0.0
            bucket = summary.setdefault(day, {}).setdefault(role, {"count": 0, "hours": 0.0})
            bucket["count"] += 1
            bucket["hours"] += hrs
    except Exception:
        return {}
    return summary


def _hourly_hours_sum(rows: list, constraints=None) -> float:
    """Hours the HOURLY budget pays for: a salaried person's shifts are left
    out. The hours budget is sales x the labor target / the hourly rate, so
    Erik's and Jim's weeks on the floor (owner, 9/30/26) are not spent from
    it and never crowd out an hourly shift."""
    sal = getattr(constraints, "is_salaried", None)
    return _safe_hours_sum([r for r in rows or [] if not (sal and sal(r.get("employee")))])


def _safe_hours_sum(rows: list) -> float:
    """Total scheduled_hours across rows, skipping any row whose value
    isn't actually numeric instead of raising — a live generation
    occasionally has one malformed row out of 100+ (see
    test_schedule_row_repair.py) where scheduled_hours ends up holding a
    stray time string from a column shift. The original per-row parsing
    loop already tolerates this per-row; the backstop passes' own
    hours_scheduled recomputes need the same tolerance, since a single bad
    row previously aborted the recompute (and by extension the CSV
    rebuild) via an uncaught ValueError from float() inside sum()."""
    total = 0.0
    for r in rows:
        try:
            total += float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            pass
    return round(total, 1)


def _normalise_row_times(row: dict) -> None:
    """Rewrite a 24-hour shift_start/shift_end ("17:00") as "5:00pm", the
    one form the pipeline's checks read. Anything unreadable is left alone
    for the sanity checks to flag."""
    for key in ("shift_start", "shift_end"):
        raw = (row.get(key) or "").strip()
        if not raw or _TIME_FIELD_RE.match(raw):
            continue
        m = _rules.parse_minutes(raw)
        if m is not None:
            row[key] = _format_minutes_to_time(m)


def _enforce_close_time(row: dict, real_day: str, close_times: dict, role_buffers: dict) -> None:
    """Hard-caps a row's shift_end at that day's actual close time (plus any
    role-specific after-close allowance, e.g. a bartender's stated "stay 1h
    after close") — mutates `row` in place. A no-op for any restaurant that
    hasn't configured close_times_json, so this only ever applies where an
    owner has actually set real hours.

    Telling the model "the close time is 9pm, never schedule past it" in
    prose plateaued below 100% compliance on live testing (captured
    servers scheduled to 9:30-10pm on a night that closes at 9pm, likely
    from over-generalizing a "staff this day like a busy Friday" volume
    note to Friday's later close time too) — this is the deterministic
    backstop for a rule that must never be violated, not one more request
    the model can occasionally miss.
    """
    if not real_day or real_day not in close_times:
        return
    close_minutes = _parse_time_to_minutes(close_times[real_day])
    if close_minutes is None:
        return
    # A close in the small hours is the next morning: "1:00am" is 25:00, not
    # one in the morning before the doors open (SCHED-13).
    if close_minutes < _rules._OVERNIGHT_LATEST_BEFORE:
        close_minutes += 24 * 60
    role = (row.get("role") or "").strip()
    ceiling = close_minutes + role_buffers.get(role, 0)

    start_minutes = _parse_time_to_minutes(row.get("shift_start", ""))
    end_minutes = _parse_time_to_minutes(row.get("shift_end", ""))
    if end_minutes is None:
        return
    # A shift that ends past midnight ends on the next day's clock; reading
    # 2:00am as 120 minutes let a 5pm-2am shift through a 10pm close.
    if start_minutes is not None and end_minutes <= start_minutes:
        end_minutes += 24 * 60
    if end_minutes <= ceiling:
        return

    if start_minutes is None or start_minutes >= ceiling:
        # Can't produce a sensible corrected shift (e.g. shift_start is
        # itself already past the ceiling) — don't fabricate a number,
        # flag it for a human instead.
        row["needs_review"] = True
        return

    row["shift_end"] = _format_minutes_to_time(ceiling)
    row["scheduled_hours"] = str(round((ceiling - start_minutes) / 60, 1))
    note = (row.get("notes") or "").strip()
    row["notes"] = f"{note} (auto-capped to close time)" if note else "auto-capped to close time"


def _row_fields_look_sane(row: dict) -> bool:
    """True when everything but `day` already looks individually
    well-formed — confirmed against two more live-captured failure shapes
    beyond the day-omitted-shift one: the model sometimes just misspells
    the day word itself ("Thursson" instead of "Thursday"), or duplicates
    the employee name into the day slot instead of writing a weekday
    ("Piper A.,Piper A.,Runner,..."). In both, employee/role/times/hours
    were all already in the right place — only the day label itself was
    wrong, which `date` already fixes. Flagging those for human review is
    just noise; this reserves the flag for rows where something beyond the
    day label can't be confirmed sane.
    """
    emp = (row.get("employee") or "").strip()
    role = (row.get("role") or "").strip()
    start = (row.get("shift_start") or "").strip()
    end = (row.get("shift_end") or "").strip()
    hours = (row.get("scheduled_hours") or "").strip()
    if not emp or emp in _WEEKDAYS or _TIME_FIELD_RE.match(emp):
        return False
    if not role or role in _WEEKDAYS or _TIME_FIELD_RE.match(role):
        return False
    if not _TIME_FIELD_RE.match(start) or not _TIME_FIELD_RE.match(end):
        return False
    try:
        float(hours)
    except (ValueError, TypeError):
        return False
    return True

def _top_up_hours_gap(preview_rows: list, daily_target_hours: dict, hours_budget: float,
                       hours_scheduled: float, restaurant_id: int,
                       close_times: dict, role_buffers: dict, constraints=None, scorer_for=None) -> tuple:
    """Deterministic post-generation pass that adds real shifts on the days
    furthest under their own per-day target when the AI's output lands well
    under the week's PAR hours budget.

    Prompt-only fixes for this (see par_block/_daily_targets in labor.py)
    plateaued around -240h under a ~1314h budget on live testing — asking
    the model to hit a number more insistently in prose has a ceiling. This
    is the same deterministic-backstop pattern as _enforce_close_time,
    applied to headcount instead of shift end times.

    Never invents an employee: only adds a shift for someone who already
    has other shifts this week in that exact role, on a day they aren't
    already working and haven't marked unavailable, picking whoever has
    the fewest hours so far to spread the addition fairly. Uses an existing
    same-day/role shift as a time template when one exists, else that
    role's typical time block anywhere in the week. Every added row is
    tagged in its notes so it's visible, never silent.

    Among the legal people for the thin role, the one whose shift costs the
    week the least Shift Quality is taken when `scorer_for` gives a scorer
    (the fewest hours first is the order and the tie-break).

    Returns (preview_rows, hours_added, added_dates) where added_dates is
    {date: shifts_added_count}.
    """
    # A coverage backstop, not an hours target. The labor budget is a
    # CEILING (the prompt says so and a test pins it); this pass used to
    # spend toward it, adding shifts whenever the week landed 5% under.
    # It now adds a shift only where a role on a day is genuinely thin
    # against what that role usually runs, and never past the budget.
    if not daily_target_hours:
        return preview_rows, 0.0, {}
    remaining_budget = (hours_budget - hours_scheduled) if hours_budget and hours_budget > 0 else float("inf")
    if remaining_budget <= 0:
        return preview_rows, 0.0, {}
    total_gap = remaining_budget
    scorer = scorer_for(preview_rows) if scorer_for else None

    import datetime as _dt_topup
    import json as _json_avail
    from models import get_staff_availability

    _avail_rows = get_staff_availability(restaurant_id) or []
    unavailable_by_emp: dict = {}
    available_by_emp: dict = {}
    # staff_availability only has whole-day granularity in its structured
    # fields — a client who writes "only mornings" or "no closes" has to
    # put it in the freeform notes field instead, which the AI prompt does
    # read (see labor.py's _avail_block) but this deterministic code has
    # no reliable way to parse. Rather than risk scheduling a body into a
    # time window their notes explicitly rule out, exclude anyone with any
    # notes at all from automatic day-level additions -- conservative, but
    # a missed top-up is a far smaller problem than deterministically
    # violating a constraint a human specifically wrote down.
    notes_restricted: set = set()
    # This week's reading of each row: a weekday blocked only between two
    # dates counts when this week's day is inside them (employee audit M5).
    from models import availability_blocked_days as _av_blocked
    _topup_dates = sorted({r.get("date") for r in preview_rows if r.get("date")})
    for a in _avail_rows:
        name = a.get("employee_name")
        if not name:
            continue
        try:
            unavailable_by_emp[name] = _av_blocked(a, _topup_dates or None)
        except Exception:
            unavailable_by_emp[name] = set()
        try:
            avail = _json_avail.loads(a.get("available_days") or "[]")
            if avail:
                available_by_emp[name] = set(_WEEKDAYS) - unavailable_by_emp[name]
        except Exception:
            pass
        if (a.get("notes") or "").strip():
            notes_restricted.add(name)

    # Roster + time templates from the AI's own output this week
    role_employees: dict = {}
    role_time_template: dict = {}
    date_role_template: dict = {}
    hours_by_employee: dict = {}
    working_on_date: dict = {}
    role_headcount_by_date: dict = {}
    by_date_hours: dict = {}

    for r in preview_rows:
        date, emp, role = r.get("date", ""), r.get("employee", ""), r.get("role", "")
        if not (date and emp and role):
            continue
        role_employees.setdefault(role, set()).add(emp)
        working_on_date.setdefault(date, set()).add(emp)
        try:
            hrs = float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            hrs = 0.0
        hours_by_employee[emp] = hours_by_employee.get(emp, 0.0) + hrs
        by_date_hours[date] = by_date_hours.get(date, 0.0) + hrs
        # Keyed by daypart: a Saturday with seven morning servers and no
        # dinner ones used to read as "seven servers", and the template it
        # cloned was the first morning row. (role, daypart) is the unit.
        part = _rules.daypart_of(r.get("shift_start", ""))
        role_headcount_by_date[(date, role, part)] = role_headcount_by_date.get((date, role, part), 0) + 1
        if r.get("shift_start") and r.get("shift_end"):
            date_role_template.setdefault((date, role, part), (r["shift_start"], r["shift_end"]))
            role_time_template.setdefault((role, part), (r["shift_start"], r["shift_end"]))
    # People on the roster in a role, even without a row this week.
    for _n, _role in (getattr(constraints, "roster_roles", None) or {}).items() if constraints is not None else []:
        if _role:
            role_employees.setdefault(_role.strip(), set()).add(_n)
    # A day the model wrote nothing for is not thin — it is missing, and
    # that is the generation's failure to report, never this pass's to fill.
    dates_with_rows = {d for d in by_date_hours if by_date_hours.get(d, 0) > 0}

    # Average headcount per (role, daypart) across the days it appears —
    # used to spot a thin daypart for a role that's normally better staffed.
    role_day_counts: dict = {}
    for (d, role, part), n in role_headcount_by_date.items():
        role_day_counts.setdefault((role, part), []).append(n)
    role_avg_headcount = {rp: sum(v) / len(v) for rp, v in role_day_counts.items() if v}
    added_by_date: dict = {}

    daily_target_hours = dict(daily_target_hours)  # local copy — we mutate to drop exhausted dates
    hours_added = 0.0
    added_dates: dict = {}
    remaining_gap = total_gap
    MAX_ADDS = 100  # hard safety ceiling regardless of gap size
    # Which (date, role) pairs are actually thin: at least one whole person
    # under that role's average headcount across the week. Nothing else is
    # a reason to add a shift.
    thin_dates = {d for (d, role, part), n in role_headcount_by_date.items()
                  if role_avg_headcount.get((role, part), 0) - n >= 1}
    thin_dates |= {d for d in daily_target_hours for (role, part) in role_avg_headcount
                   if (d, role, part) not in role_headcount_by_date and role_avg_headcount[(role, part)] >= 1}
    thin_dates &= dates_with_rows
    _blocked_dates = (constraints.blocked_dates if constraints is not None else {})

    for _pass in range(MAX_ADDS):
        if remaining_gap <= 0:
            break
        target_date = max(
            (d for d in daily_target_hours if d in thin_dates
             and daily_target_hours[d] - by_date_hours.get(d, 0.0) > 0),
            key=lambda d: daily_target_hours[d] - by_date_hours.get(d, 0.0),
            default=None,
        )
        if not target_date:
            break

        try:
            day_name = _dt_topup.datetime.strptime(target_date, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue

        # Pick the role furthest below its own weekly-average headcount for
        # this day, among roles that actually have an available candidate.
        # At most a quarter of the day's target can come from this pass;
        # past that the day is flagged, not written.
        _cap_day = 0.25 * float(daily_target_hours.get(target_date) or 0)
        if _cap_day and added_by_date.get(target_date, 0.0) >= _cap_day:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        candidates_role = None
        best_shortfall = 0.0
        for (role, part), avg_hc in role_avg_headcount.items():
            today_hc = role_headcount_by_date.get((target_date, role, part), 0)
            shortfall = avg_hc - today_hc
            if shortfall > best_shortfall:
                pool = role_employees.get(role, set()) - working_on_date.get(target_date, set())
                pool = {e for e in pool
                        if day_name not in unavailable_by_emp.get(e, set())
                        and (e not in available_by_emp or day_name in available_by_emp[e])
                        and (e not in notes_restricted or (constraints is not None and e.lower() in constraints.time_windows))
                        and (constraints is None or constraints.cert_ok(e, role)[0])
                        # Approved time off, daypart windows, a deactivated
                        # name: the same question the swap search asks.
                        and target_date not in (_blocked_dates.get(e.lower()) or {})
                        and (constraints is None or constraints.can_work(e, target_date, part)[0])
                        # "No employee over 40h for the week" was prompt text
                        # with nothing enforcing it, so this pass could push
                        # someone into overtime to consume an hours budget —
                        # and the cost model priced those hours straight.
                        and hours_by_employee.get(e, 0.0) + (sum((constraints.base_hours.get(e.lower()) or {}).values()) if constraints is not None else 0.0)
                            < (constraints.max_hours(e) if constraints is not None else _WEEKLY_HOURS_CEILING)}
                if pool:
                    best_shortfall = shortfall
                    candidates_role = (role, part, pool)

        if not candidates_role:
            # No role on this date has a real, available candidate — can't
            # responsibly add here. Drop the date and move to the
            # next-neediest one instead of forcing a bad pick.
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue

        role, part, pool = candidates_role
        start, end = (date_role_template.get((target_date, role, part)) or role_time_template.get((role, part))
                      or _daypart_fallback(constraints, day_name, part))
        if constraints is not None:
            pool = {e for e in pool if constraints.window_ok(e, target_date, start, end)[0]}
            if not pool:
                daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
                continue
        _rel = (getattr(constraints, "reliability", None) or {}) if constraints is not None else {}
        ordered = sorted(pool, key=lambda e: (hours_by_employee.get(e, 0.0),
                                              float((_rel.get(e) or {}).get("no_show_rate") or 0), e))

        probe = {
            "date": target_date, "day": day_name, "employee": "", "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": "0",
            "notes": "added — PAR hours top-up",
        }
        _enforce_close_time(probe, day_name, close_times, role_buffers)
        if probe.get("needs_review"):
            # Template (borrowed from another day) doesn't produce a sane
            # shift once capped to this day's close time — skip rather
            # than fabricate a number, same discipline _enforce_close_time
            # itself follows.
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        s_min, e_min = _parse_time_to_minutes(probe["shift_start"]), _parse_time_to_minutes(probe["shift_end"])
        if s_min is None or e_min is None or e_min <= s_min:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        probe["scheduled_hours"] = str(round((e_min - s_min) / 60, 1))
        hrs = float(probe["scheduled_hours"])
        if hrs <= 0 or hrs > remaining_gap + 0.01:
            # Nothing to add, or it would cross the ceiling; the ceiling wins.
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        legal = []
        for employee in ordered:
            new_row = dict(probe, employee=employee)
            _cap = constraints.max_hours(employee) if constraints is not None else _WEEKLY_HOURS_CEILING
            _base = sum((constraints.base_hours.get(employee.lower()) or {}).values()) if constraints is not None else 0.0
            if hours_by_employee.get(employee, 0.0) + _base + hrs > _cap:
                continue
            if constraints is not None and not constraints.rest_ok(
                    employee, new_row, [r for r in preview_rows if (r.get("employee") or "") == employee])[0]:
                continue
            new_row["notes"] = "added — coverage top-up"
            legal.append((employee, new_row))
            if len(legal) >= FILL_SCORE_CANDIDATES:
                break
        if not legal:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        employee, new_row = _fill_pick(scorer, preview_rows, legal)

        preview_rows.append(new_row)
        hours_added += hrs
        remaining_gap -= hrs
        added_dates[target_date] = added_dates.get(target_date, 0) + 1
        added_by_date[target_date] = added_by_date.get(target_date, 0.0) + hrs
        if role_headcount_by_date.get((target_date, role, part), 0) + 1 >= role_avg_headcount.get((role, part), 0):
            thin_dates.discard(target_date)

        by_date_hours[target_date] = by_date_hours.get(target_date, 0.0) + hrs
        working_on_date.setdefault(target_date, set()).add(employee)
        hours_by_employee[employee] = hours_by_employee.get(employee, 0.0) + hrs
        role_headcount_by_date[(target_date, role, part)] = role_headcount_by_date.get((target_date, role, part), 0) + 1

    return preview_rows, round(hours_added, 1), added_dates


# FLSA overtime starts past this many hours in the payroll week. The
# generated schedule must not create overtime on its own; the prompt said
# so and nothing enforced it.
_WEEKLY_HOURS_CEILING = 40.0

_SERVER_MAX_OVERLAP = 7


def _peak_server_overlap(day_rows: list) -> tuple:
    """Sweep-line peak concurrent Server headcount for one day's rows.
    Returns (peak_count, peak_time_minutes) — peak_time is None if there
    are no rows. Ends are processed before starts at the same instant, so
    a shift ending at 3pm and one starting at 3pm never count as
    overlapping at that exact minute."""
    events = []
    for r in day_rows:
        s = _parse_time_to_minutes(r.get("shift_start", ""))
        e = _parse_time_to_minutes(r.get("shift_end", ""))
        if s is None or e is None or e <= s:
            continue
        events.append((s, 1))
        events.append((e, -1))
    events.sort(key=lambda ev: (ev[0], ev[1]))  # -1 (end) before +1 (start) at same minute
    running = 0
    peak = 0
    peak_time = None
    for t, delta in events:
        running += delta
        if running > peak:
            peak = running
            peak_time = t
    return peak, peak_time


def _trim_server_overlap_cap(preview_rows: list, close_times: dict, role_buffers: dict,
                              max_overlap: int = None, roles=None, scorer_for=None) -> tuple:
    """Deterministic backstop for the "never more than N servers at once"
    hard cap already stated in hours_notes.

    N is the restaurant's own configured section count when it has one —
    one server per section is the rule the prompt states. This was a flat
    module constant of 7 applied to every restaurant, so a dining room
    with twelve sections had rows silently shortened or deleted against a
    ceiling its owner never set, while the prompt above told the model to
    use the real figure. The constant remains the fallback for a
    restaurant that hasn't configured sections. Live testing showed the AI
    missing this reliably at 190-220+ row scale, including via double
    shifts (a server working both morning and night) that were never
    subtracted from the night total before more closers got added on top —
    the exact reported bug (Monday: 6 correct night closers + 2 uncounted
    doubles = 8) plus worse cases found in verification (10 on one night).

    Only ever shortens a row's shift_end (never shift_start) — "cut early
    when overstaffed" is already this restaurant's own stated convention
    for servers (see hours_notes' SHIFT END / CLOSER RULES), not something
    invented here. Priority for which row absorbs the cut, most to least
    preferred: a row this file's own top-up/extension passes added (most
    discretionary), then the later leg of a double shift (the specific
    reported pattern), then whichever active row started latest (a
    "last in, first cut" tiebreak). Removes a row outright if trimming it
    below ~30min would leave a token sliver shift.

    Returns (preview_rows, rows_trimmed, trimmed_dates).

    With no section count on file there is no cap to enforce: the flat 7
    this used to fall back on deleted real shifts against a ceiling the
    owner never set, undid role floors above 7 servers, and reported the
    cut nowhere (SCHED-8). _SERVER_MAX_OVERLAP stays as the documented
    figure for callers that pass it explicitly.

    `roles` are the roles the cap counts together — the restaurant's front
    of house (schedule_rules.Constraints.foh_roles), the same set the rule
    sweep and the requirement use; servers alone when not given. Among the
    rows in the most discretionary tier at the peak, the cut that costs the
    week the least Shift Quality is taken when `scorer_for` gives a scorer.
    """
    try:
        _cap = int(max_overlap) if max_overlap and int(max_overlap) > 0 else 0
    except (TypeError, ValueError):
        _cap = 0
    if not _cap:
        return preview_rows, 0, {}

    counted = {str(x).strip().lower() for x in (roles or ()) if str(x).strip()} or {"server"}
    scorer = scorer_for(preview_rows) if scorer_for else None
    by_date: dict = {}
    for r in preview_rows:
        if (r.get("role") or "").strip().lower() in counted:
            by_date.setdefault(r.get("date"), []).append(r)

    trimmed_dates: dict = {}
    rows_trimmed = 0

    for date, day_rows in by_date.items():
        try:
            day_name = __import__("datetime").datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day_name = None

        # Which employees are working a double shift today (2+ rows) — the
        # later of their rows (by start time) is the "carryover" leg.
        rows_by_employee: dict = {}
        for r in day_rows:
            emp = r.get("employee")
            if emp:
                rows_by_employee.setdefault(emp, []).append(r)
        double_shift_second_legs = set()
        for emp, rows in rows_by_employee.items():
            if len(rows) > 1:
                rows_sorted = sorted(rows, key=lambda r: _parse_time_to_minutes(r.get("shift_start", "")) or 0)
                for r in rows_sorted[1:]:
                    double_shift_second_legs.add(id(r))

        for _pass in range(20):  # bounded — one trim per pass, per day
            peak, peak_time = _peak_server_overlap(day_rows)
            if peak <= _cap or peak_time is None:
                break

            active = []
            for r in day_rows:
                s = _parse_time_to_minutes(r.get("shift_start", ""))
                e = _parse_time_to_minutes(r.get("shift_end", ""))
                if s is not None and e is not None and s <= peak_time < e:
                    active.append(r)

            def _priority(r):
                is_topup = "PAR hours top-up" in (r.get("notes") or "")
                is_second_leg = id(r) in double_shift_second_legs
                start = _parse_time_to_minutes(r.get("shift_start", "")) or 0
                return (0 if is_topup else 1, 0 if is_second_leg else 1, -start)

            ranked = sorted(active, key=_priority)
            candidate = ranked[0]
            if scorer is not None and len(ranked) > 1:
                # The tier says which cuts are the discretionary ones; the
                # score says which of them the floor can best spare.
                tier = _priority(candidate)[:2]
                pool = [r for r in ranked if _priority(r)[:2] == tier][:FILL_SCORE_CANDIDATES]
                options = []
                for r in pool:
                    cut = _overlap_cut(r, peak_time, day_name, close_times, role_buffers, _cap)
                    if cut is False:
                        continue
                    trial = [x for x in preview_rows if x is not r] + ([cut] if cut is not None else [])
                    options.append((id(r), trial))
                import time as _time_cap
                if len(options) > 1 and _time_cap.monotonic() - scorer.started <= FILL_SCORE_SECONDS:
                    key, _val = scorer.best(options)
                    candidate = next((r for r in pool if id(r) == key), candidate)
            start_min = _parse_time_to_minutes(candidate.get("shift_start", ""))
            if start_min is None:
                break
            cut = _overlap_cut(candidate, peak_time, day_name, close_times, role_buffers, _cap)
            if cut is None:
                day_rows.remove(candidate)
                preview_rows.remove(candidate)
            elif cut is not False:
                candidate.update(cut)

            rows_trimmed += 1
            trimmed_dates[date] = trimmed_dates.get(date, 0) + 1

    return preview_rows, rows_trimmed, trimmed_dates


def _overlap_cut(row: dict, peak_time: int, day_name, close_times: dict, role_buffers: dict, cap: int):
    """The row cut to end at `peak_time` (a new dict), None when that would
    leave under half an hour (the row goes), False when it cannot be read."""
    start_min = _parse_time_to_minutes(row.get("shift_start", ""))
    if start_min is None:
        return False
    if peak_time - start_min < 30:
        return None
    out = dict(row)
    out["shift_end"] = _format_minutes_to_time(peak_time)
    if day_name:
        _enforce_close_time(out, day_name, close_times, role_buffers)
    final_end = _parse_time_to_minutes(out["shift_end"])
    out["scheduled_hours"] = str(round((final_end - start_min) / 60, 1))
    note = (out.get("notes") or "").strip()
    out["notes"] = f"{note} (trimmed — over the {cap}-server cap)" if note else f"trimmed — over the {cap}-server cap"
    return out


def _window_overlap(row: dict, window: tuple) -> bool:
    s_ = _parse_time_to_minutes(row.get("shift_start", ""))
    e_ = _parse_time_to_minutes(row.get("shift_end", ""))
    return s_ is not None and e_ is not None and s_ < window[1] and e_ > window[0]


_MORNING_WINDOW = (_parse_time_to_minutes("11:00am"), _parse_time_to_minutes("2:30pm"))
_NIGHT_WINDOW = (_parse_time_to_minutes("5:30pm"), _parse_time_to_minutes("8:30pm"))
_DAYPART_SPLIT = 15 * 60   # the same 3pm the rules and the quality engine use


def _daypart_windows(constraints, day_name: str) -> list:
    """[("morning", (start, end)), ("night", (start, end))] for one day, on
    the same 3pm split schedule_rules.daypart_of and shift_quality use, from
    the day's opening and closing times when they are known. A floor
    measured on a fixed 11am–2:30pm slot counted a 6am–10:30am cook as
    nobody and added a second one."""
    open_m = _parse_time_to_minutes((getattr(constraints, "open_times", None) or {}).get(day_name, "")) if constraints else None
    close_m = _parse_time_to_minutes((getattr(constraints, "close_times", None) or {}).get(day_name, "")) if constraints else None
    if open_m is None:
        open_m = 6 * 60
    if close_m is None or close_m <= _DAYPART_SPLIT:
        close_m = 24 * 60 - 1
    return [("morning", (open_m, _DAYPART_SPLIT)), ("night", (_DAYPART_SPLIT, close_m))]


# The fill-in and trim passes (#12): each chooses, among the legal options
# its own rules allow, the one that costs the week the least Shift Quality.
# The pass's order still says which options are on the table and breaks a
# tie; the score only chooses among them. At most this many options are
# scored per choice, and past FILL_SCORE_SECONDS a pass goes back to its
# own order, so a 250-person week never waits on it.
FILL_SCORE_CANDIDATES = 6
FILL_SCORE_SECONDS = 6.0


def _pass_scorer(restaurant_id, result, signals=None, weights=None):
    """scorer_for(rows) -> shift_quality.LocalScorer for the passes, built
    from the same signals the week is scored with (read once), or None when
    they cannot be read. Each scorer re-scores only the dates a move
    touches, so choosing between six options costs a fraction of one
    whole-week score."""
    import time as _time
    import shift_quality as _sqp
    try:
        if signals is None:
            signals, weights = _quality_signals(restaurant_id, result)
    except Exception as _sx:
        print(f"[schedule] score-aware passes unavailable: {_sx}")
        return None
    profiles = result.get("shift_profiles") or None
    t0 = _time.monotonic()

    def scorer_for(rows):
        if _time.monotonic() - t0 > FILL_SCORE_SECONDS * 4:
            return None                   # the passes as a whole are out of time
        try:
            sc = _sqp.LocalScorer(rows, profiles=profiles, weights=weights, **signals)
        except Exception as _lx:
            print(f"[schedule] local scorer failed: {_lx}")
            return None
        return sc
    return scorer_for


def _fill_pick(scorer, rows, legal):
    """(employee, row) from `legal` — [(employee, new_row)] in the pass's own
    order — whose added row costs the week the least score; the first when
    there is no scorer, one option, or the pass is out of time."""
    if scorer is None or len(legal) < 2:
        return legal[0]
    import time as _time
    if _time.monotonic() - scorer.started > FILL_SCORE_SECONDS:
        return legal[0]
    key, _val = scorer.best([(k, rows + [row]) for k, (_e, row) in enumerate(legal)])
    return legal[key] if key is not None else legal[0]


def _ensure_role_floors(preview_rows: list, week_dates: list, week_days: list, restaurant_id: int,
                        close_times: dict, role_buffers: dict, floors: dict = None,
                        constraints=None, scorer_for=None) -> tuple:
    """Deterministic backstop for the owner's per-role, per-daypart staffing
    floors (schedule_rules.role_floors): "at least 1 line cook on lunch and
    2 at dinner, 3 on Saturday night". A prompt-only floor plateaus below
    full compliance, the same way close times and the server cap did.

    This replaced a rule compiled in for one client (a pizza cook on every
    daypart, doubled on that restaurant's busy days, with a 15-hour
    fallback shift). Only ever adds a shift for somebody who already works
    that role (this week or on the roster), on a date and daypart every
    rule says they can work — availability, approved time off, daypart
    windows, hours ceiling, rest — and never invents a person. Times come
    from another shift of that role in the same daypart this week when one
    exists, else the daypart's slice of the opening hours.

    Among the legal people (the fewest hours first), the one whose shift
    costs the week the least Shift Quality is taken when `scorer_for` gives
    a scorer (shift_quality.LocalScorer, _pass_scorer).

    Returns (preview_rows, rows_added, added_dates).
    """
    floors = floors if floors is not None else {}
    if not floors:
        return preview_rows, 0, {}
    scorer = scorer_for(preview_rows) if scorer_for else None
    from models import get_staff_availability
    import json as _json_avail
    if constraints is None:
        constraints = _rules.build_constraints(restaurant_id, week_dates, week_days)
    _avail_rows = get_staff_availability(restaurant_id) or []
    notes_restricted = {a.get("employee_name") for a in _avail_rows
                        if a.get("employee_name") and (a.get("notes") or "").strip()
                        and (a.get("employee_name") or "").strip().lower() not in constraints.daypart_avail}

    role_people = {}
    working_on_date = {}
    hours_by_employee = {}
    templates = {}
    rows_by_person = {}
    for r in preview_rows:
        emp, role, date = (r.get("employee") or "").strip(), (r.get("role") or "").strip(), r.get("date")
        if not (emp and date):
            continue
        working_on_date.setdefault(date, set()).add(emp)
        rows_by_person.setdefault(emp.lower(), []).append(r)
        try:
            hours_by_employee[emp] = hours_by_employee.get(emp, 0.0) + float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            pass
        if role:
            role_people.setdefault(role.lower(), set()).add(emp)
            part = _rules.daypart_of(r.get("shift_start", ""))
            if part in ("morning", "night") and r.get("shift_start") and r.get("shift_end"):
                templates.setdefault((role.lower(), part), (r["shift_start"], r["shift_end"]))
    for name, role in (getattr(constraints, "roster_roles", None) or {}).items():
        if role:
            role_people.setdefault(role.strip().lower(), set()).add(name)

    rows_added = 0
    added_dates = {}
    # A closed date, or one the generation accepted as not trading, gets no
    # floor rows: a floor is a minimum for a day that trades, never a reason
    # to open on one that does not.
    _closed = set(getattr(constraints, "closed_dates", None) or ())
    for date, day_name in zip(week_dates, week_days):
        if date in _closed:
            continue
        for role_name, spec in floors.items():
            key = role_name.strip().lower()
            for part, window in _daypart_windows(constraints, day_name):
                need = _rules.floor_for(floors, role_name, day_name, part)
                if not need:
                    continue
                current = [r for r in preview_rows if r.get("date") == date
                           and (r.get("role") or "").strip().lower() == key and _window_overlap(r, window)]
                filled = len(current)
                tried = set()
                for _attempt in range(40):
                    if filled >= need:
                        break
                    pool = set(role_people.get(key, set())) - working_on_date.get(date, set()) - tried
                    pool = {e for e in pool if (e not in notes_restricted or e.lower() in constraints.time_windows)
                            and constraints.can_work(e, date, part)[0] and constraints.cert_ok(e, role_name)[0]}
                    if not pool:
                        break
                    start, end = templates.get((key, part)) or _daypart_fallback(constraints, day_name, part)
                    pool = {e for e in pool if constraints.window_ok(e, date, start, end)[0]}
                    if not pool:
                        break
                    # The fewest hours so far, and among those the most reliable,
                    # is the order; the score chooses among the first few
                    # legal ones (_fill_pick).
                    _rel = getattr(constraints, "reliability", None) or {}
                    ordered = sorted(pool, key=lambda e: (hours_by_employee.get(e, 0.0),
                                                          float((_rel.get(e) or {}).get("no_show_rate") or 0), e))
                    probe = {"date": date, "day": day_name, "employee": "", "role": role_name,
                             "shift_start": start, "shift_end": end, "scheduled_hours": "0",
                             "notes": f"added — {role_name} floor"}
                    _enforce_close_time(probe, day_name, close_times, role_buffers)
                    s_min = _parse_time_to_minutes(probe["shift_start"])
                    e_min = _parse_time_to_minutes(probe["shift_end"])
                    if probe.get("needs_review") or s_min is None or e_min is None or e_min <= s_min:
                        break
                    hrs = round((e_min - s_min) / 60, 1)
                    legal = []
                    for employee in ordered:
                        new_row = dict(probe, employee=employee)
                        base = sum((constraints.base_hours.get(employee.lower()) or {}).values())
                        if hours_by_employee.get(employee, 0.0) + base + hrs > constraints.max_hours(employee):
                            tried.add(employee)
                            continue
                        ok, _why = constraints.rest_ok(employee, new_row, rows_by_person.get(employee.lower(), []))
                        if not ok:
                            tried.add(employee)
                            continue
                        new_row["scheduled_hours"] = str(hrs)
                        legal.append((employee, new_row))
                        if len(legal) >= FILL_SCORE_CANDIDATES:
                            break
                    if not legal:
                        continue
                    employee, new_row = _fill_pick(scorer, preview_rows, legal)
                    filled += 1
                    preview_rows.append(new_row)
                    rows_by_person.setdefault(employee.lower(), []).append(new_row)
                    working_on_date.setdefault(date, set()).add(employee)
                    hours_by_employee[employee] = hours_by_employee.get(employee, 0.0) + hrs
                    rows_added += 1
                    added_dates[date] = added_dates.get(date, 0) + 1
    return preview_rows, rows_added, added_dates


def _ensure_station_coverage(preview_rows: list, week_dates: list, week_days: list,
                             close_times: dict, role_buffers: dict, constraints=None,
                             scorer_for=None) -> tuple:
    """Every kitchen station the owner requires on a daypart gets a cook
    trained on it (kitchen_stations; owner, 9/30/26: "just because a sauté
    cook works sauté doesn't mean the same cook can be on grill").

    The draft's kitchen cooks on each date and daypart are matched to the
    stations it needs (kitchen_stations.week). For a station none of them
    can take, a shift is added for a cook trained on it who is free that
    day, on the same legal checks the role floors use (availability, time
    off, the daypart window, the hours ceiling, rest) — never an invented
    person, never a cook who isn't trained. What can't be filled is left
    for the review to name. Returns (preview_rows, rows_added, unfilled)."""
    import kitchen_stations as _ks
    cfg = getattr(constraints, "stations", None) or {}
    if not cfg:
        return preview_rows, 0, []
    scorer = scorer_for(preview_rows) if scorer_for else None
    working_on_date, hours_by_employee, rows_by_person, templates = {}, {}, {}, {}
    for r in preview_rows:
        emp, date = (r.get("employee") or "").strip(), r.get("date")
        if not (emp and date):
            continue
        working_on_date.setdefault(date, set()).add(emp)
        rows_by_person.setdefault(emp.lower(), []).append(r)
        try:
            hours_by_employee[emp] = hours_by_employee.get(emp, 0.0) + float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            pass
        if _ks.is_kitchen(cfg, r.get("role")) and r.get("shift_start") and r.get("shift_end"):
            for part in _ks.parts_of(r):
                templates.setdefault(part, (r["shift_start"], r["shift_end"], r.get("role")))
    roster_roles = getattr(constraints, "roster_roles", None) or {}
    closed = set(getattr(constraints, "closed_dates", None) or ())
    added, unfilled = 0, []
    for gap in _ks.week(preview_rows, cfg, week_dates)["gaps"]:
        date, day, part, station = gap["date"], gap["day"], gap["daypart"], gap["station"]
        if date in closed:
            continue
        # Still a gap? An earlier addition this pass may have covered it.
        if not _ks.uncovered(preview_rows, cfg, date, part):
            continue
        pool = [name for name, have in (cfg.get("skills") or {}).items()
                if station in have and name not in working_on_date.get(date, set())]
        start, end, role = templates.get(part) or (_daypart_fallback(constraints, day, part) + (cfg["roles"][0],))
        legal = []
        for name in sorted(pool, key=lambda n: (hours_by_employee.get(n, 0.0), n)):
            person_role = roster_roles.get(name) if _ks.is_kitchen(cfg, roster_roles.get(name)) else role
            if not (constraints.can_work(name, date, part)[0] and constraints.cert_ok(name, person_role)[0]
                    and constraints.window_ok(name, date, start, end)[0]):
                continue
            row = {"date": date, "day": day, "employee": name, "role": person_role,
                   "shift_start": start, "shift_end": end, "scheduled_hours": "0",
                   "notes": f"added — {station} station"}
            _enforce_close_time(row, day, close_times, role_buffers)
            s_min, e_min = _parse_time_to_minutes(row["shift_start"]), _parse_time_to_minutes(row["shift_end"])
            if row.get("needs_review") or s_min is None or e_min is None or e_min <= s_min:
                continue
            hrs = round((e_min - s_min) / 60, 1)
            base = sum((constraints.base_hours.get(name.lower()) or {}).values())
            if hours_by_employee.get(name, 0.0) + base + hrs > constraints.max_hours(name):
                continue
            if not constraints.rest_ok(name, row, rows_by_person.get(name.lower(), []))[0]:
                continue
            row["scheduled_hours"] = str(hrs)
            legal.append((name, row))
            if len(legal) >= FILL_SCORE_CANDIDATES:
                break
        if not legal:
            unfilled.append(gap)
            continue
        name, row = _fill_pick(scorer, preview_rows, legal)
        preview_rows.append(row)
        rows_by_person.setdefault(name.lower(), []).append(row)
        working_on_date.setdefault(date, set()).add(name)
        hours_by_employee[name] = hours_by_employee.get(name, 0.0) + float(row["scheduled_hours"])
        added += 1
    return preview_rows, added, unfilled


def station_report(rows: list, constraints, week_dates: list) -> dict:
    """The week's station assignment as the review and the screens read it:
    {"stations": [...], "assigned": [{date, employee, shift_start, morning,
    night}], "gaps": [{date, day, daypart, station}]}, or {} when the
    restaurant has no stations set."""
    import kitchen_stations as _ks
    cfg = getattr(constraints, "stations", None) or {}
    if not cfg:
        return {}
    w = _ks.week(rows, cfg, week_dates)
    assigned = []
    for i, parts in sorted(w["stations"].items()):
        r = rows[i]
        assigned.append({"date": r.get("date"), "employee": r.get("employee"), "shift_start": r.get("shift_start"),
                         "role": r.get("role"), "morning": parts.get("morning"), "night": parts.get("night")})
    return {"stations": cfg["stations"], "assigned": assigned, "gaps": w["gaps"]}


def _daypart_fallback(constraints, day_name: str, part: str) -> tuple:
    """A daypart's slice of the opening hours, when no shift of that role
    exists this week to copy times from."""
    open_m = _parse_time_to_minutes((constraints.open_times or {}).get(day_name, "")) if constraints else None
    close_m = _parse_time_to_minutes((constraints.close_times or {}).get(day_name, "")) if constraints else None
    if part == "morning":
        start = open_m if open_m is not None else _parse_time_to_minutes("11:00am")
        end = min(close_m, 15 * 60) if close_m is not None else 15 * 60
    else:
        start = max(open_m, 15 * 60) if open_m is not None else 16 * 60
        end = close_m if close_m is not None else 22 * 60
    if end <= start:
        end = start + 4 * 60
    return _format_minutes_to_time(start), _format_minutes_to_time(end)


def stored_daily_targets(restaurant_id, history_id) -> dict:
    """{date: hours} the stored week was generated against: kept on its
    quality, or recovered from its labor-efficiency facts for a week saved
    before they were kept. {} when neither is on file."""
    import json as _json
    from models import get_conn
    try:
        hid = int(history_id)
    except (TypeError, ValueError):
        return {}
    conn = get_conn()
    try:
        row = conn.execute("SELECT quality_json FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (hid, restaurant_id)).fetchone()
    finally:
        conn.close()
    try:
        q = _json.loads(row["quality_json"] or "null") or {} if row else {}
    except Exception:
        return {}
    kept = q.get("daily_target_hours")
    if isinstance(kept, dict) and kept:
        return {str(k): float(v) for k, v in kept.items() if v}
    out = {}
    for s in q.get("shifts") or []:
        for d in s.get("dimensions") or []:
            t = (d.get("facts") or {}).get("target_hours") if d.get("key") == "labor_efficiency" else None
            if t and s.get("date"):
                out[s["date"]] = float(t)
    return out


def _merge_holiday_lift(restaurant_id, dates, signals_by_date: dict) -> dict:
    """Fold what each holiday this week did here last year into the dated
    demand signals, in place. One implementation for the generation and the
    live rescore — the rescore used to skip it, so a holiday week's score
    jumped on the first edit when "peak" fell back to "normal"."""
    import schedule_economics as _econ
    holiday = _econ.holiday_lift(restaurant_id, dates)
    for d, h in holiday.items():
        e = signals_by_date.setdefault(d, {"lift_pct": None, "covers": None, "labels": []})
        label = h["name"] + (f" ({h['lift_pct']:+d}% here last year)" if h.get("lift_pct") is not None else "")
        if label not in e.setdefault("labels", []):
            e["labels"].append(label)
        if h.get("lift_pct") is not None:
            e["lift_pct"] = h["lift_pct"] if e.get("lift_pct") is None else max(e["lift_pct"], h["lift_pct"])
    return holiday


def quality_inputs_from_db(restaurant_id, daily_target_hours=None, week_rows=None):
    """Rebuild the engine's inputs for a schedule nobody just generated.

    A manager editing a published week needs the score to move as they
    drag a shift, and there is no generation result sitting around to score
    it against. Everything the generator passed down is re-derivable from
    the database and the restaurant's own shift history — except the
    per-day hour targets, which belong to that specific generation and are
    passed back in by the caller that already holds them.
    """
    from models import (get_operational_scores, get_role_strength_thresholds,
                        get_shift_leader_rules, get_shift_profiles, get_restaurant)
    from labor import build_demand_forecast, historical_patterns
    from models import _cached_shifts
    import shift_quality as _sq

    scores = get_operational_scores(restaurant_id)
    thresholds = get_role_strength_thresholds(restaurant_id) if scores else {}
    leader_rules = get_shift_leader_rules(restaurant_id) if scores else []
    stored = get_shift_profiles(restaurant_id)

    demand_by_day = {}
    try:
        forecast = build_demand_forecast(restaurant_id)
        if forecast.get("ok"):
            from thresholds import DEMAND_LEVEL_MIN_READINGS as _MINR
            demand_by_day = {d["day"]: d["vs_average_pct"] for d in forecast["days"]
                             if (d.get("samples") or 0) >= _MINR}
    except Exception:
        demand_by_day = {}

    profiles = []
    if stored or scores:
        profiles = _sq.profiles_from_config(
            [_sq.profile_from_dict(p) for p in stored] or None,
            default_strength=thresholds, default_leader_rules=leader_rules,
            demand_by_day=demand_by_day)

    try:
        # Through the request-scoped cache, like every other reader. Parsing
        # the whole history here and again in _quality_signals meant a single
        # manager edit paid for two full passes over it.
        patterns = historical_patterns(_cached_shifts(restaurant_id))
        from labor import apply_learned_headcount
        patterns["typical_headcount"] = apply_learned_headcount(restaurant_id, patterns.get("typical_headcount"))
    except Exception:
        patterns = {"typical_headcount": {}, "cross_trained": {}}

    restaurant = get_restaurant(restaurant_id)
    out = {
        "elsewhere": {},
        "operational_scores": scores,
        "strength_thresholds": thresholds,
        "leader_rules": leader_rules,
        "shift_profiles": profiles,
        "demand_by_day": demand_by_day,
        "role_minimums": _parse_role_minimums(
            getattr(restaurant, "role_minimums_json", None) if restaurant else None),
        "daily_target_hours": daily_target_hours or {},
        **patterns,
    }
    # The same constraint set generation used, rebuilt for the week the
    # rows describe, so a manager's edit is judged by every rule the draft
    # was — and the caller's rows can be swept for violations too.
    try:
        rows = week_rows or []
        dates = sorted({r.get("date") for r in rows if r.get("date")})
        if dates:
            from datetime import datetime as _dt
            week_days = [_dt.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates]
            c = _rules.build_constraints(restaurant_id, dates, week_days, restaurant)
            roster_rows = _staff.roster(restaurant_id)
            c.roster_roles = {e["name"]: e.get("role") or "" for e in roster_rows}
            out["constraints"] = c
            out["roster"] = sorted(e["name"] for e in roster_rows)
            out["roster_roles"] = c.roster_roles
            out["pairs"] = _staff.pair_sets(restaurant_id)
            out["reliability"] = _staff.reliability(restaurant_id)
            out["demand_by_date"] = _signals.by_date(restaurant_id, dates)
            try:
                _merge_holiday_lift(restaurant_id, dates, out["demand_by_date"])
            except Exception as _hx:
                print(f"[schedule] live holiday lift unavailable: {_hx}")
            # The same person at a sibling location that day, as generation
            # scores it.
            try:
                from models import sibling_location_shifts as _sibs
                out["elsewhere"] = _sibs(restaurant_id, dates) or {}
            except Exception as _ex:
                print(f"[schedule] live sibling shifts unavailable: {_ex}")
            out["prior_week_assignments"] = _prior_week_assignments(restaurant_id, before=dates[0])
            out["pending_time_off"] = {n: sorted(d) for n, d in c.pending_off.items()}
    except Exception as _cx:
        print(f"[schedule] live constraints unavailable: {_cx}")
    out["learned_worse"] = learned_worse_levers(restaurant_id)
    return out


def learned_worse_levers(restaurant_id) -> dict:
    """{weekday: {worsened, measured, label}}: the days where cutting
    staffing was taken and measured worse here (rec_learning.
    worsened_levers) — the optimizer holds back a trim of the same day
    (memory audit 9/29/26, "what_worked"). {} on any failure."""
    try:
        import rec_learning
        return rec_learning.worsened_levers(restaurant_id) or {}
    except Exception as e:
        print(f"[schedule] learned levers unavailable for {restaurant_id}: {e}")
        return {}


def _prior_week_assignments(restaurant_id, days_back: int = 7, before: str = None) -> dict:
    """The tail of the last schedule, as fatigue and fairness assignments.

    A run of nine days reads as five when the engine can only see inside
    its own seven-day box, and the week boundary is exactly where that
    matters — somebody who worked Saturday and Sunday then Monday to Friday
    has worked nine straight and the schedule looked clean.
    """
    from models import get_schedule_history, get_schedule_history_detail
    import shift_quality as _sq
    from datetime import datetime as _dt
    try:
        csv_text = _last_published_csv(restaurant_id, before) if before else ""
        if not csv_text and before:
            # Nothing published before this week: the newest generation is
            # the only tail there is — but ONLY if it is the adjacent week.
            # A three-week-old draft seeded "7 days in a row" warnings for
            # people working three days; no seed is better than a stale one.
            from datetime import timedelta as _tdl
            floor = (_dt.strptime(before, "%Y-%m-%d") - _tdl(days=2)).strftime("%Y-%m-%d")
            entries = get_schedule_history(restaurant_id, limit=5)
            for e in entries:
                ws, we = (e.get("week_start") or ""), (e.get("week_end") or "")
                if ws >= before or not we or we < floor:
                    continue
                detail = get_schedule_history_detail(e["id"], restaurant_id)
                csv_text = (detail or {}).get("schedule_csv") or ""
                if csv_text:
                    break
        if not csv_text:
            return {}
        out = {}
        for line in csv_text.split("\n")[1:]:
            parts = [p.strip() for p in line.split(",", 7)]
            if len(parts) < 5:
                continue
            date, _day, name, _role, start = parts[0], parts[1], parts[2], parts[3], parts[4]
            if not (date and name):
                continue
            try:
                _dt.strptime(date, "%Y-%m-%d")
            except ValueError:
                continue
            part = _sq.daypart_of(start)
            entry = {"date": date, "daypart": part,
                     "day": _dt.strptime(date, "%Y-%m-%d").strftime("%A"),
                     "demand": "normal"}
            bucket = out.setdefault(name, [])
            if not any(e["date"] == date and e["daypart"] == part for e in bucket):
                bucket.append(entry)
        # Only the last few days matter; an entire prior week would let a
        # long-past pattern drag this week's fairness figures.
        for name, bucket in out.items():
            bucket.sort(key=lambda e: e["date"])
            out[name] = bucket[-days_back:]
        return out
    except Exception:
        return {}


def _quality_signals(restaurant_id, result, **extra):
    """Every signal the Shift Quality Engine reads, gathered in one place.

    Kept separate from the scoring call so the live-rescore route a manager
    hits after dragging a shift is judged against exactly the same inputs
    the generation was. Two code paths assembling these by hand is how the
    number on screen starts disagreeing with the number in the schedule.
    """
    from models import (get_employee_tenure, get_leader_flags,
                        get_prior_shift_pattern, get_unavailability_map,
                        get_quality_weights)
    signals = {
        "scores": result.get("operational_scores") or {},
        "leader_rules": result.get("leader_rules") or [],
        "daily_target_hours": result.get("daily_target_hours") or {},
        "demand_by_day": result.get("demand_by_day") or {},
        "role_minimums": result.get("role_minimums") or {},
        "typical_headcount": result.get("typical_headcount") or {},
        "cross_trained": result.get("cross_trained") or {},
        # The prompt calls staff constraints the highest-priority rule of
        # all. The engine cannot read free text, but handing it the names
        # lets the what-if pass refuse to move anybody who has one, instead
        # of recommending a swap that breaks a rule the generator obeyed.
        "constraints": result.get("staff_constraints") or {},
        # Rows the repair pass could not vouch for, so a double-booked
        # person is not counted as coverage twice.
        "flagged": result.get("flagged_rows") or set(),
        # The tail of the previous schedule, so a nine-day run does not read
        # as five just because the week boundary falls in the middle of it.
        "prior_week_assignments": result.get("prior_week_assignments") or {},
        # The same person on another site's schedule tonight.
        "elsewhere": result.get("elsewhere") or {},
        # Dated facts (events, reservations) that raise a day's demand.
        "demand_by_date": result.get("demand_by_date") or {},
        "reliability": result.get("reliability") or {},
        "pairs": result.get("pairs") or {},
        "roster": result.get("roster") or [],
        # {name: role} — who a fix or a repair may consider for a role,
        # beyond the people already scheduled in it this week.
        "roster_roles": result.get("roster_roles") or {},
    }
    c = result.get("constraints")
    if c is not None:
        signals["rules"] = _rules_for_swaps(c)
        signals["open_times"] = c.open_times or {}
        signals["close_times"] = c.close_times or {}
        signals["role_floors"] = c.role_floors or {}
        signals["max_shift_hours"] = c.compliance.get("max_shift_hours")
        signals["weekly_ceiling"] = c.compliance.get("weekly_hours_ceiling")
        # Keyed by display name, which is what the contexts carry; the
        # constraints key by lowercase. (An earlier version built this and
        # then overwrote it with an empty dict.)
        signals["hours_limits"] = {}
        for n in (result.get("roster") or []):
            lim = c.hours_limits.get(n.lower())
            if lim:
                signals["hours_limits"][n] = lim
        # One section cap for the requirement, the score, the backstop and
        # the repair loop: the section count over the front-of-house roles.
        signals["section_cap"] = int(getattr(c, "section_cap", 0) or 0)
        signals["cap_roles"] = sorted(getattr(c, "foh_roles", None) or {"server"})
    try:
        from models import get_restaurant as _gr_ct
        signals["cross_training_targets"] = _rules.role_cross_training(_gr_ct(restaurant_id))
    except Exception:
        signals["cross_training_targets"] = {}
    try:
        import schedule_intel as _si
        signals["ledger"] = result.get("fairness_ledger") if result.get("fairness_ledger") is not None else _si.fairness_ledger(restaurant_id)
        signals["history_weeks"] = _si.history_weeks(restaurant_id)
    except Exception:
        signals["ledger"], signals["history_weeks"] = {}, 0
    try:
        signals["demand_curve"] = _hourly_profile(restaurant_id)
    except Exception:
        signals["demand_curve"] = {}
    # The rotation the week is judged against, and the sales-per-labor-hour
    # objective — the generation's own when it carries them, so a live
    # re-score judges an edit by what the draft was written against.
    signals.update(_learning_signals(restaurant_id, result))
    # A rule nobody on the roster could satisfy is not the draft's fault,
    # and confidence should say so rather than the score silently failing.
    try:
        scores = signals["scores"]
        unsat = 0
        unmeetable = []
        roles = result.get("roster_roles") or {}
        try:
            from models import get_leader_flags as _glf
            _flags = _glf(restaurant_id) or {}
        except Exception:
            _flags = {}
        cross = result.get("cross_trained") or {}
        for rule in signals["leader_rules"]:
            ms = rule.get("min_score")
            role = (rule.get("role") or "").strip().lower()

            def _in_role(n):
                return (not roles or (roles.get(n) or "").strip().lower() == role
                        or role in {str(x).strip().lower() for x in (cross.get(n) or [])})
            if rule.get("attribute"):
                # "Authorized to close" nobody in the role holds is as
                # unmeetable as a score nobody reaches; checked only for
                # score rules, it scored every closing shift 0 (SCHED-30).
                able = [n for n, f in _flags.items() if f and _in_role(n)]
            elif ms is not None:
                able = [n for n, sc in scores.items() if sc is not None and float(sc) >= float(ms) and _in_role(n)]
            else:
                continue
            if len(able) < int(rule.get("count") or 1):
                unsat += 1
                unmeetable.append(rule)
        signals["unsatisfiable"] = unsat
        # The rules themselves, so the engine sets them aside instead of
        # capping every shift they cover (SCHED-30).
        signals["unmeetable_leader_rules"] = unmeetable
    except Exception:
        pass
    # Each of these is a separate read and any one of them can be empty for
    # a new restaurant. A failure to load one must cost that dimension, not
    # the whole evaluation — which is exactly what returning {} does, since
    # a dimension with no data withdraws instead of scoring zero.
    # Availability is read for the week being judged: a weekday blocked
    # only between two dates counts inside them (employee audit M5).
    _sig_week = list((result or {}).get("week_dates") or []) or None
    for key, fn in (("tenure", get_employee_tenure), ("leader_flags", get_leader_flags),
                    ("prior_pattern", get_prior_shift_pattern),
                    ("availability", lambda rid: get_unavailability_map(rid, week_dates=_sig_week))):
        try:
            signals[key] = fn(restaurant_id) or {}
        except Exception:
            signals[key] = {}
    try:
        signals["experienced"] = _staff.experienced_names(restaurant_id)
    except Exception:
        signals["experienced"] = set()
    try:
        signals["preferences"] = _staff.stated_preferences(restaurant_id)
    except Exception:
        signals["preferences"] = {}
    _reconcile_to_roster(signals)
    try:
        weights = get_quality_weights(restaurant_id)
    except Exception:
        weights = {}
    if not signals.get("constraints"):
        try:
            from models import get_staff_notes as _gsn
            signals["constraints"] = {n["employee_name"]: n["notes"]
                                      for n in (_gsn(restaurant_id) or [])
                                      if n.get("employee_name")}
        except Exception:
            signals["constraints"] = {}
    signals.update(extra)
    return signals, weights


def _learning_signals(restaurant_id, result) -> dict:
    """{rotation, splh_targets, daypart_sales} for the scorer. Either one
    failing costs only its own dimension's part (each withdraws on {})."""
    out = {"rotation": {}, "splh_targets": {}, "daypart_sales": {}}
    try:
        rot = result.get("rotation_plan")
        if rot is None:
            import schedule_intel as _si
            rot = _si.rotation_plan(restaurant_id, roster_roles=result.get("roster_roles") or None)
        out["rotation"] = rot or {}
    except Exception as _sfx:
        _soft_fail('rotation_plan', _sfx, restaurant_id)
    try:
        obj = result.get("splh_objective")
        if obj is None:
            import schedule_economics as _econ
            obj = _econ.splh_objective(restaurant_id)
        if obj and obj.get("available"):
            import schedule_economics as _econ_t
            out["splh_targets"] = {wd: {p: _econ_t.splh_target_for(obj, wd, p) for p in ("morning", "night")
                                        if _econ_t.splh_target_for(obj, wd, p)}
                                   for wd in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}
            out["daypart_sales"] = obj.get("daypart_sales") or {}
    except Exception as _sfx:
        _soft_fail('splh_objective', _sfx, restaurant_id)
    return out


def _reconcile_to_roster(signals: dict) -> None:
    """Ratings and closer flags for people who are not on the roster (seed
    leftovers, staff who left) must not judge this week: fourteen such
    ratings once put a fully staffed Saturday at 0 while the confidence
    panel said nobody was rated. With a roster on file, only its names'
    facts are scored; without one, everything stands."""
    roster = {str(n).strip().lower() for n in (signals.get("roster") or []) if n}
    if not roster:
        return
    for key in ("scores", "leader_flags"):
        d = signals.get(key) or {}
        if isinstance(d, dict):
            signals[key] = {n: v for n, v in d.items() if str(n).strip().lower() in roster}


def _score_schedule_quality(restaurant_id, rows, result, **extra):
    """Score the finished schedule, then see whether a better one existed.

    The what-if pass only ever trades two people between shifts of the same
    role, so headcount, hours and coverage cannot move. That restriction is
    what makes running it on every generation affordable and its answers
    explainable: exactly two names changed, and here is what it bought.
    """
    import shift_quality as _sq
    profiles = result.get("shift_profiles") or None
    signals, weights = _quality_signals(restaurant_id, result, **extra)
    quality = _sq.score_rows(rows, profiles=profiles, weights=weights, **signals)

    what_if = {"ran": False, "reason": "Nothing to compare."}
    if quality.get("checked"):
        try:
            what_if = _sq.compare_candidates(rows, profiles=profiles, weights=weights, **signals)
            # The engine reports what a better arrangement WOULD have been;
            # it does not silently rewrite the schedule the owner is about
            # to read. A swap the manager did not ask for, applied without
            # being told, is how trust in a generated schedule dies.
            what_if.pop("rows", None)
            what_if.pop("baseline", None)
            what_if.pop("best", None)
        except Exception as _wx:
            what_if = {"ran": False, "reason": f"comparison unavailable: {_wx}"}
    # Recommendation kinds this owner has been shown many times and never
    # once acted on stop being shown. Nothing is RECORDED here: this runs
    # wherever a schedule is scored — the nightly auto-draft with nobody
    # looking, a rescore, a build — and a build is not a showing (re-audit
    # C1). present_quality records what a response actually served.
    try:
        import schedule_intel as _si
        import rec_ledger as _rl
        schedule_rec_key = _si.schedule_rec_key
        hidden = _si.suppressed_kinds(restaurant_id)
        silenced = _rl.silenced_keys(restaurant_id)
        import insight_store as _ist_sq
        declined_sigs = None
        kept, items = [], []
        for rec in quality.get("recommendations") or []:
            kind = _sq.recommendation_kind(rec)
            key = schedule_rec_key(kind, rec)
            # The key is the sentence, with no week in it, so a "Not for us"
            # (silenced for years) or a "Did it" (two weeks) on "Fill the
            # gap on Friday night" hid the identical gap in every later
            # week. Coverage, leadership and fatigue say whether a shift is
            # safe to run: never hidden, by kind OR by key (re-audit A-9).
            if kind in hidden or (key in silenced and kind not in _sq.PROTECTED_REC_KINDS):
                continue
            # "Not for us" to the same advice on ANY surface (T2, B4 H6):
            # "Trim about 6h from Tuesday night" is Home's trim_day:Tuesday
            # (both labor:day:tuesday). The protected kinds still never hide.
            sig = _ist_sq.advice_signature(key, rec)
            if sig and kind not in _sq.PROTECTED_REC_KINDS:
                if declined_sigs is None:
                    declined_sigs = _ist_sq.declined_signatures(restaurant_id)
                if sig in declined_sigs:
                    continue
            kept.append(rec)
            # The kind travels with the text, so web and iOS never classify
            # a sentence themselves (their copies of the prefix list drifted).
            items.append({"text": rec, "kind": kind, "key": key, "rec_key": key, "advice_signature": sig})
        # Both clients show at most SHOWN_RECOMMENDATIONS (the web panel's
        # loop, iOS's block); the list is cut here so what is served is what
        # is shown, on every client.
        quality["recommendations"] = kept[:SHOWN_RECOMMENDATIONS]
        quality["recommendation_items"] = items[:SHOWN_RECOMMENDATIONS]
        # Each served item carries its K1 confidence (confidence audit):
        # evidence from this read's own measured completeness.
        try:
            import rec_trust as _rt_sq
            # Slots staffed from other restaurants' borrowed headcount cap
            # the read's Evidence until the restaurant's own weeks replace
            # them (BM3-13, Top-50 #33).
            quality["borrowed_slots"] = _rt_sq.borrowed_slots(result.get("starting_headcount"),
                                                              result.get("typical_headcount"))
            _rt_sq.attach_schedule_confidence(restaurant_id, quality, quality["recommendation_items"])
        except Exception as _rtx:
            print(f"[schedule] recommendation confidence failed: {_rtx}")
        if hidden:
            quality["suppressed_recommendation_kinds"] = sorted(hidden)
    except Exception as _rx:
        print(f"[schedule] recommendation filter failed: {_rx}")
    return quality, what_if


def mark_next_week_built(restaurant_id, history_id) -> int:
    """"Next week's schedule isn't built" (schedule:next-week — the brief's
    line and the queue's item) is discharged by building it: a whole week
    generated is that recommendation implemented (re-audit C7). Only an
    episode someone was shown is recorded (rec_ledger.implemented), once
    per generation. Never raises."""
    try:
        import rec_ledger
        return rec_ledger.implemented(restaurant_id, "schedule:next-week", "schedule_review",
                                      source_ref=f"generated:{history_id}", meta={"module": "labor"})
    except Exception as e:
        print(f"[schedule] next-week not marked implemented: {e}")
        return 0


# The most Shift Quality recommendations a client shows for one week.
SHOWN_RECOMMENDATIONS = 5


def schedule_item_metric(item):
    """The number a schedule recommendation is measured on (memory audit
    9/29/26, "positive_volume"): a trim of a weekday reads that weekday's
    labor %. Coverage, leadership, pairing, fatigue and ratings advice adds
    or moves people for service's sake and has no honest number: None."""
    if str((item or {}).get("kind") or "") != "hours":
        return None
    try:
        import outcomes
        return outcomes.expected_metric_for("schedule_hours:" + str(item.get("key") or ""), item.get("text"),
                                            module="schedule")
    except Exception:
        return None


def present_quality(restaurant_id, quality, user_id=None, authority=None):
    """The quality verdict's recommendations, recorded as shown on
    "schedule_review" by the response that SERVES them to a person — the
    generation's status poll, a rescore, apply-fixes, the optimizer, a
    stored week reopened (re-audit C1) — once a day per recommendation.
    Also schedule_intel's own "shown" (what decides which kinds go quiet),
    which was counted by the nightly auto-draft nobody read. Each item gains
    `rec_key`, `answerable` and `answered`. Mutates and returns `quality`;
    never raises."""
    if not isinstance(quality, dict):
        return quality
    items = [it for it in (quality.get("recommendation_items") or []) if isinstance(it, dict) and it.get("key")]
    if not items:
        return quality
    try:
        import rec_delivery
        import schedule_intel as _si
        ids = rec_delivery.present_now(
            restaurant_id, "schedule_review",
            [{"key": it["key"], "module": "schedule", "title": str(it.get("text") or "")[:200],
              "kind": "schedule_" + str(it.get("kind") or "other"), "position": i,
              "expected_metric": schedule_item_metric(it)} for i, it in enumerate(items)],
            user_id=user_id)
        for it in items:
            it["rec_key"] = it["key"]
            it["answered"] = bool(it["key"] in ids and ids[it["key"]] is None)
            it["answerable"] = rec_delivery.answerable(it["key"]) and not it["answered"]
            try:
                # Who it was shown to (memory audit 9/29/26, who_answered):
                # a manager's or support's showings never suppress a kind
                # for the owner.
                _si.record_recommendation(restaurant_id, it.get("kind") or "other",
                                          str(it.get("text") or "")[:200], "shown", authority=authority)
            except Exception as e:
                print(f"[schedule] showing not counted for {it.get('kind')}: {e}")
    except Exception as e:
        print(f"[schedule] quality recommendations not presented rid={restaurant_id}: {e}")
    return quality


def _sched_notes_with_findings(restaurant_id, sched_notes):
    """The owner's schedule notes plus what the cross-module engine found
    about staffing. "Friday dinner is one server short and it shows in the
    reviews" (business_intelligence reviews_x_labor) used to change nothing
    unless the owner read it and edited the draft; now the draft reads it.
    Only links that cleared their own floors exist, so this adds nothing
    when there is nothing to add."""
    try:
        import business_intelligence as bi
        # Only links the owner has NOT answered (executive_brief filters
        # them — re-audit 9/29/26, CROSSMODULE-2): a declined "complaints on
        # lean Fridays" used to push every weekly draft to add Friday staff.
        links = [l for l in (bi.executive_brief(restaurant_id).get("links") or [])
                 if l.get("kind") == "reviews_x_labor" and l.get("headline")]
    except Exception:
        links = []
    if not links:
        return sched_notes
    # A co-occurrence is a QUESTION for the draft, never a directive, and it
    # carries what it is not (its not_a_cause), as every other surface does.
    lines = ["Cavnar AI question (reviews x labor, two modules pointing at one day — not a proven cause): "
             + l["headline"] + ". Consider whether the draft should staff that day differently, and say in the "
             "summary what you decided and why."
             + (" What it is not: " + l["not_a_cause"] if l.get("not_a_cause") else "")
             + (" What would confirm it: " + l["confirm_by"] if l.get("confirm_by") else "")
             for l in links[:2]]
    from labor import SCHED_FINDINGS_HEADER
    return ((sched_notes or "").strip() + "\n\n" + SCHED_FINDINGS_HEADER + "\n" + "\n".join(lines)).strip()


def likely_edits(restaurant_id, rows: list, patterns: list = None, predict: bool = True) -> list:
    """Draft rows the manager is likely to change, before they see it.

    First, rows matching an edit the manager keeps making: a person on a
    weekday and daypart they have been taken off repeatedly, or a role's
    start the manager keeps moving. Then every other row the predictor
    (schedule_learning.predict_row_edits — smoothed edit rates from this
    restaurant's own finished drafts) puts at or above its threshold, with
    the likelihood and the rates behind it. [{kind, employee?, date, text,
    likelihood?, reason?, index?}]."""
    out = _pattern_likely_edits(restaurant_id, rows, patterns)
    if not predict:
        return out
    try:
        import schedule_learning as _sl
        flagged = {(o.get("employee") or "").strip().lower() + "|" + (o.get("date") or "") for o in out if o.get("employee")}
        for p in _sl.predict_row_edits(restaurant_id, rows):
            if f"{(p.get('employee') or '').strip().lower()}|{p.get('date') or ''}" in flagged:
                continue
            out.append(p)
    except Exception as _px:
        print(f"[schedule] edit prediction unavailable for {restaurant_id}: {_px}")
    return out


def _pattern_likely_edits(restaurant_id, rows: list, patterns: list = None) -> list:
    """The repeated-edit matches likely_edits starts from."""
    import shift_quality as _sq
    if patterns is None:
        try:
            patterns = _versions.patterns_for_draft(restaurant_id)[0]
        except Exception:
            patterns = _versions.learned_patterns(restaurant_id)
    try:
        import schedule_intel as _si
        gone = _si.dismissed_patterns(restaurant_id)
        patterns = [p for p in patterns if _si.pattern_key(p) not in gone]
    except Exception:
        pass
    out, seen = [], set()
    for r in rows or []:
        name = (r.get("employee") or "").strip()
        day = _sq._day_name(r.get("date") or "")
        part = _sq.daypart_of(r.get("shift_start", ""))
        for p in patterns:
            if p.get("day") != day or p.get("daypart") != part:
                continue
            if p.get("kind") == "moved_off" and (p.get("employee") or "").strip().lower() == name.lower():
                key = ("off", name, r.get("date"))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"kind": "moved_off", "employee": name, "date": r.get("date"),
                            "text": f"{name} is on {day} {'lunch' if part == 'morning' else 'dinner'} — you've taken "
                                    f"them off it in {p.get('times')} recent weeks."})
            elif p.get("kind") == "retime_start" and (p.get("role") or "").strip().lower() == (r.get("role") or "").strip().lower():
                want = (p.get("time") or p.get("to") or "").strip().lower().replace(" ", "")
                have = (r.get("shift_start") or "").strip().lower().replace(" ", "")
                key = ("retime", r.get("role"), r.get("date"))
                if want and have and want != have and key not in seen:
                    seen.add(key)
                    out.append({"kind": "retime_start", "date": r.get("date"), "role": r.get("role"),
                                "text": f"{r.get('role')} on {day} starts at {r.get('shift_start')} here — you usually "
                                        f"move it to {p.get('time') or p.get('to')}."})
    return out


# The quality gate: a draft whose busy shifts are still capped by a staffing
# hole after the repair loop has its weakest days written again, once, with
# what was wrong named in the prompt. Only coverage holes qualify — a missing
# leader or a weak team is the roster's limit and a rewrite cannot fix it.
GATE_BELOW = 60
GATE_MAX_DATES = 3
# The gate's rewrite is one more model call inside the same job: it starts
# only with at least this much of the job's model time left (P-22).
GATE_MIN_MODEL_SECONDS = 180


def _quality_gate(result: dict):
    """{dates, focus, reason} when the finished draft should have its weakest
    days regenerated, else None."""
    q = result.get("quality") or {}
    if not q.get("checked"):
        return None
    weak = {}
    for s in q.get("shifts") or []:
        if not s.get("scored") or (s.get("score") or 0) >= GATE_BELOW:
            continue
        if s.get("capped_by") not in ("coverage", "coverage_curve"):
            continue
        if sq_demand_rank(s) < 2:
            continue
        lines = []
        for d in s.get("dimensions") or []:
            if d["key"] == s["capped_by"]:
                lines = d.get("weaknesses") or []
        part = "lunch" if s["daypart"] == "morning" else "dinner"
        weak.setdefault(s["date"], []).extend(f"{s['day']} {s['date']} {part}: {w}" for w in lines[:2])
    if not weak:
        return None
    dates = sorted(weak, key=lambda d: -len(weak[d]))[:GATE_MAX_DATES]
    return {"dates": sorted(dates), "focus": [f for d in sorted(dates) for f in weak[d]][:12],
            "reason": f"{len(dates)} busy {'day' if len(dates) == 1 else 'days'} still had a staffing hole after repair"}


def _gate_local(quality: dict, dates) -> float:
    """Demand-weighted mean of the shift scores on `dates`, a daypart the
    other draft had but this one lacks counting as 0 — so a rewrite cannot
    "improve" a day by leaving its dinner out."""
    import shift_quality as _sq
    dates = set(dates or [])
    shifts = [s for s in (quality or {}).get("shifts") or [] if s.get("date") in dates]
    if not shifts:
        return 0.0
    num = den = 0.0
    for s in shifts:
        w = _sq.DEMAND_WEIGHT.get(((s.get("profile") or {}).get("demand") or "normal"), 1.0)
        num += (s.get("score") or 0) * w if s.get("scored") else 0.0
        den += w
    return num / (den or 1.0)


def _restore_draft(restaurant_id, keep_id, drop_id=None):
    """Make `keep_id` the week's current draft again: saving the rewrite
    superseded it. The rewrite (and any later unsent draft of the week from
    this job) is marked superseded by it, so auto-publish and the history
    read the original as the draft in force."""
    if not keep_id:
        return
    from models import get_conn
    conn = get_conn()
    try:
        row = conn.execute("SELECT week_start FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (keep_id, restaurant_id)).fetchone()
        if not row:
            return
        conn.execute("UPDATE schedule_history SET superseded_by=? WHERE restaurant_id=? AND week_start=? AND id>? "
                     "AND published_at IS NULL", (keep_id, restaurant_id, row["week_start"], keep_id))
        conn.execute("UPDATE schedule_history SET superseded_by=NULL WHERE id=? AND restaurant_id=?",
                     (keep_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()


def sq_demand_rank(shift: dict) -> int:
    import shift_quality as _sq
    return _sq.DEMAND_RANK.get(((shift.get("profile") or {}).get("demand") or "normal"), 1)


def _run_schedule_job(job_id, restaurant_id, week_start=None, dates=None, base_history_id=None,
                      focus=None, gate=True, _fallback=None, redo_reason=None):
    """week_start picks the week (any date in it); dates + base_history_id
    regenerate only those days of an existing draft, the rest pinned.
    focus names what was weak in those days for the prompt; gate allows one
    automatic regeneration of a draft's weakest days (_quality_gate) — never
    on a redo the owner asked for, which must touch only the days they chose.
    redo_reason is the owner's {"chip", "text"} for a redo (redo_reason_from).

    Run inside generation_scope (submit_generation, the auto-draft) it has
    one wall clock (P-22): its model calls stop in time for the rest of the
    job, and a job its poll has already declared dead saves nothing."""
    if dates and not focus:
        gate = False
    import csv as _csv_mod, traceback as _tb, datetime as _dt_sched
    clock = current_clock()
    try:
        _pinned, _base_rows = [], []
        redo = bool(dates and base_history_id)
        if redo:
            from models import get_schedule_history_detail as _gshd
            _base = _gshd(int(base_history_id), restaurant_id) or {}
            _base_rows = _versions.rows_from_csv(_base.get("schedule_csv") or "")
            _pinned = [r for r in _base_rows if r.get("date") not in set(dates)]
            week_start = week_start or _base.get("week_start")
        # A redo (the owner's, or the quality gate's) writes only its dates,
        # with the kept days handed to every call as the rest of the week
        # (schedule audit 10/3/26 P-9, E-21, PR-18). It used to generate the
        # whole week again — two or three calls for big rosters — blind to
        # the kept days, and keep only the chosen dates' lines.
        _bkw = {}
        if focus:
            _bkw["focus"] = list(focus)
        if redo:
            _bkw.update(dates=list(dates), prior_rows=_pinned)
            if redo_reason:
                _bkw["owner_reason"] = dict(redo_reason)
        result = _build_schedule_result(restaurant_id, week_start=week_start, **_bkw)
        # A partial redo rewrites only these days; the passes below that can
        # change rows (fixes, the repair loop, the budget trim) leave the
        # owner's kept days exactly as they were.
        _editable = set(dates) if redo else None
        if redo:
            # Only the asked-for days were written; the rest come from the
            # draft the owner is keeping — and so does any asked-for day the
            # model could not write, said in the review.
            _unwritten = {u["date"] for u in (result.get("unwritten_dates") or [])}
            keep = set(dates) - _unwritten
            lines = [ln for ln in result["schedule_csv"].split("\n")[1:] if ln.split(",", 1)[0].strip() in keep]
            for r in _pinned + [r for r in _base_rows if r.get("date") in _unwritten]:
                lines.append(",".join(str(r.get(c, "") or "").replace(",", ";") for c in _COLS_PINNED))
            result["schedule_csv"] = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(lines)
            result["regenerated_dates"] = sorted(keep)
            _editable = keep
            if _unwritten:
                result.setdefault("generation_notes", []).insert(0, _not_redone_line(sorted(_unwritten)))
        # Rows planned before the call (a manager plan's pinned rows) were
        # shown to every call as fixed and never written by the model; they
        # join the week here, once.
        _pins = [r for r in (result.get("pinned_rows") or []) if r.get("date")
                 and (not redo or r.get("date") in set(result.get("regenerated_dates") or []))]
        if _pins:
            _have = {tuple(ln.split(",")[:6]) for ln in result["schedule_csv"].split("\n")[1:]}
            _add = [",".join(str(r.get(c, "") or "").replace(",", ";") for c in _COLS_PINNED) for r in _pins]
            _add = [ln for ln in _add if tuple(ln.split(",")[:6]) not in _have]
            if _add:
                result["schedule_csv"] = result["schedule_csv"].rstrip("\n") + "\n" + "\n".join(_add)
        if redo:
            result["redo"] = {"dates": sorted(set(dates)), "base_history_id": int(base_history_id),
                              "reason": (redo_reason or {}).get("chip"), "reason_text": (redo_reason or {}).get("text"),
                              "by": "gate" if focus else "owner"}
        from models import get_staff_notes as _gsn_sched, get_close_times as _gct_sched, get_role_close_buffers as _grcb_sched
        _raw_notes = _gsn_sched(restaurant_id) or []
        staff_constraints = {n["employee_name"]: n["notes"] for n in _raw_notes if n.get("employee_name")}
        _close_times = _gct_sched(restaurant_id)
        _role_close_buffers = _grcb_sched(restaurant_id)
        _restaurant_for_sched = get_restaurant(restaurant_id)
        preview_rows = []
        hours_scheduled = 0.0
        hours_added = 0.0
        _hours_drift_total = 0.0
        _hours_drift_rows = 0
        added_dates = {}
        extended_dates = {}
        trimmed_dates = {}
        pizza_added_dates = {}
        try:
            _COLS = ["date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes"]
            _csv_lines = result["schedule_csv"].split("\n")
            print(f"[schedule] csv lines={len(_csv_lines)} first3={_csv_lines[:3]}")
            # A malformed row from the model used to be skipped in silence, so
            # a garbled response produced a SHORT schedule rather than an
            # error and nobody was told how much was missing. Counted now, and
            # surfaced below.
            _dropped_rows = []
            for _line in _csv_lines[1:]:  # skip header
                _line = _line.strip()
                if not _line:
                    continue
                _parts = _line.split(",", 7)  # max 7 splits — notes gets remainder
                if len(_parts) < 6:
                    _dropped_rows.append(_line[:120])
                    continue
                # Strip outer quotes Sonnet sometimes adds around field values
                _row = {_COLS[i]: _parts[i].strip().strip('"').strip() for i in range(min(len(_parts), 8))}
                # Times in 24-hour form ("17:00") are the same times; every
                # check after this reads "5:00pm", and a 24-hour row used to
                # skip the close cap and the hours reconciliation (SCHED-38).
                _normalise_row_times(_row)
                # Keep rows that have a non-empty employee name — skips header repeats and prose
                if not _row.get("employee", "").strip() or _row.get("employee", "").lower() == "employee":
                    continue

                # The model occasionally mis-writes one row per generation
                # (out of 60-100+) — always a non-routine addition (a food
                # runner, a "misfill check," an extra staff member) that
                # doesn't follow the same repeating pattern as the rest of
                # the week. `date` was correct in every malformed row
                # observed, so re-derive `day` from it instead of trusting
                # the model's own day text, and detect+repair the specific
                # failure shapes actually seen in captured live output
                # rather than guessing.
                try:
                    _real_day = _dt_sched.datetime.strptime(_row.get("date", ""), "%Y-%m-%d").strftime("%A")
                except (ValueError, TypeError):
                    _real_day = None
                if not _real_day:
                    # date itself didn't parse — can't verify or repair
                    # anything in this row, so flag it rather than let a
                    # possibly-bogus day value pass through unmarked.
                    _row["needs_review"] = True
                elif _row.get("day") != _real_day:
                    if _row.get("employee") == _real_day:
                        # Clean two-column swap: "...,Jamie L.,Friday,
                        # Server,..." instead of "...,Friday,Jamie L.,
                        # Server,...". Swap back for a full recovery.
                        _row["day"], _row["employee"] = _real_day, _row["day"]
                    elif _TIME_FIELD_RE.match(_row.get("role", "")):
                        # The far more common failure, confirmed against
                        # live captured output: the model drops the `day`
                        # field entirely for this one row (never reorders
                        # it — just omits it), which shifts every field
                        # after `date` one position left. The tell is that
                        # "role" ends up holding a clock time
                        # ("...,Farah A.,Prep Cook,8:00am,3:00pm,7,morning
                        # prep,,"), which a real role name never does —
                        # every field after `date` un-shifts one position
                        # right, recovering a fully sensible row instead of
                        # just flagging it.
                        _old_day, _old_employee, _old_role = _row.get("day", ""), _row.get("employee", ""), _row.get("role", "")
                        _old_start, _old_end, _old_hours = _row.get("shift_start", ""), _row.get("shift_end", ""), _row.get("scheduled_hours", "")
                        _row["day"] = _real_day
                        _row["employee"] = _old_day
                        _row["role"] = _old_employee
                        _row["shift_start"] = _old_role
                        _row["shift_end"] = _old_start
                        _row["scheduled_hours"] = _old_end
                        _row["notes"] = _old_hours
                    else:
                        _row["day"] = _real_day
                        if not _row_fields_look_sane(_row):
                            _row["needs_review"] = True

                # A row with no shift times, or a weekday where the role
                # belongs, is not a shift: the model once wrote
                # "2026-09-16,Wednesday,Bartender,Wednesday,,,," and the
                # role "Wednesday" then surfaced on the rules screen as if
                # it were a real role (owner, 9/26/26). It is dropped and
                # counted with the other unreadable lines, never saved.
                if (not (_row.get("shift_start") or "").strip() or not (_row.get("shift_end") or "").strip()
                        or (_row.get("role") or "").strip() in _WEEKDAYS):
                    _dropped_rows.append(_line[:120])
                    continue

                _enforce_close_time(_row, _real_day, _close_times, _role_close_buffers)

                # Times win over the model's own arithmetic — see
                # _reconcile_scheduled_hours.
                _drift = _reconcile_scheduled_hours(_row)
                if abs(_drift) >= 0.1:
                    _hours_drift_total += abs(_drift)
                    _hours_drift_rows += 1

                preview_rows.append(_row)
                try:
                    hours_scheduled += float(_row.get("scheduled_hours") or 0)
                except (ValueError, TypeError):
                    pass
            if _hours_drift_rows:
                print(f"[schedule] corrected scheduled_hours on {_hours_drift_rows} row(s), "
                      f"{round(_hours_drift_total, 1)}h total drift")
                # The model's hours disagreed with its own shift times and
                # were corrected: an AI-quality finding (fix round G #58),
                # not a failing job.
                import ai_utils as _ai_q
                _ai_q.record_quality_event(
                    "labor_schedule", "figures", restaurant_id=restaurant_id, action="labor_schedule",
                    n=_hours_drift_rows,
                    detail=(f"{_hours_drift_rows} schedule rows had scheduled_hours that disagreed "
                            f"with their shift times ({round(_hours_drift_total, 1)}h total)"))
            print(f"[schedule] parsed {len(preview_rows)} rows, first={preview_rows[0] if preview_rows else None}")
            if not preview_rows:
                # Every line was unreadable: saving it would publish an empty
                # week as if it were a schedule (SCHED-42).
                raise ScheduleGenerationError(
                    "The generated schedule came back in a form we couldn't read, so nothing was saved. "
                    "Try generating again.")

            _constraints = result.get("constraints")
            if _constraints is None:
                _constraints = _rules.build_constraints(restaurant_id, result.get("week_dates", []),
                                                        result.get("week_days", []), _restaurant_for_sched)
                result["constraints"] = _constraints
            if not getattr(_constraints, "roster_roles", None):
                _constraints.roster_roles = result.get("roster_roles") or {}
            if "roster" in result:
                # The staff list the prompt was built from is the list the
                # rows are checked against — one roster, one answer. An
                # explicitly empty list means "no roster on file", which is
                # no opinion (the same reading Constraints.can_work gives).
                _constraints.roster_names = list(result["roster"] or [])
                _constraints.active = {str(n).strip().lower() for n in (result["roster"] or []) if n}

            # Closed dates and days the generation accepted as not trading.
            _constraints.closed_dates = set(getattr(_constraints, "closed_dates", None) or ()) | set(result.get("closed_dates") or ())
            # Every fill-in and trim pass below chooses among its legal
            # options by what each costs the week's Shift Quality, scored
            # locally (only the dates a move touches). The signals are read
            # once here and shared.
            _pass_sig, _pass_w = None, None
            try:
                _pass_sig, _pass_w = _quality_signals(restaurant_id, result)
            except Exception as _psx:
                print(f"[schedule] pass signals unavailable: {_psx}")
            _scorer_for = _pass_scorer(restaurant_id, result, _pass_sig, _pass_w) if _pass_sig is not None else None
            preview_rows, pizza_rows_added, pizza_added_dates = _ensure_role_floors(
                preview_rows, result.get("week_dates", []), result.get("week_days", []),
                restaurant_id, _close_times, _role_close_buffers,
                floors=_constraints.role_floors, constraints=_constraints, scorer_for=_scorer_for,
            )
            if pizza_rows_added:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] role floors added {pizza_rows_added} row(s) across {pizza_added_dates}")
            # Kitchen stations (kitchen_stations): each required station on
            # each daypart gets a cook trained on it.
            preview_rows, _st_added, _st_unfilled = _ensure_station_coverage(
                preview_rows, result.get("week_dates", []), result.get("week_days", []),
                _close_times, _role_close_buffers, constraints=_constraints, scorer_for=_scorer_for)
            if _st_added:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] station coverage added {_st_added} row(s); {len(_st_unfilled)} left open")

            _hourly_so_far = _hourly_hours_sum(preview_rows, _constraints)
            preview_rows, hours_added, added_dates = _top_up_hours_gap(
                preview_rows, result.get("daily_target_hours", {}),
                result.get("hours_budget", 0), _hourly_so_far, restaurant_id,
                _close_times, _role_close_buffers, constraints=_constraints, scorer_for=_scorer_for,
            )
            if hours_added:
                hours_scheduled = round(hours_scheduled + hours_added, 1)
                print(f"[schedule] coverage top-up added {hours_added}h across {added_dates}")
            # Nothing extends shifts to spend the hours budget: the prompt calls
            # it a ceiling. The pass that pushed closers later to consume it
            # (_extend_shifts_to_close_gap) had no caller left and was removed
            # (schedule audit 10/3/26 P-46).

            preview_rows, rows_trimmed, trimmed_dates = _trim_server_overlap_cap(
                preview_rows, _close_times, _role_close_buffers,
                max_overlap=getattr(_restaurant_for_sched, 'section_count', None),
                roles=getattr(_constraints, "foh_roles", None), scorer_for=_scorer_for,
            )
            if rows_trimmed:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] trimmed {rows_trimmed} row(s) over the server cap across {trimmed_dates}")

            # Staggered starts along the day's sales curve and the trim back
            # to the budget, each reported in the review. A role's arrival
            # is a clock-in lead before each person's own shift
            # (attendance.clock_in_leads), never a reason to move a shift.
            import schedule_economics as _econ
            try:
                _curve = _hourly_profile(restaurant_id)
            except Exception:
                _curve = {}
            _stagger_scorer = _scorer_for(preview_rows) if (_scorer_for and _curve) else None
            preview_rows, _staggered = _econ.stagger_same_starts(
                preview_rows, _curve, score_fn=_stagger_scorer.score if _stagger_scorer else None)
            result["staggered"] = _staggered
            result["hourly_profile_ready"] = bool(_curve)
            result["trimmed"] = []
            result["hours_trimmed"] = 0.0
            if int(getattr(_restaurant_for_sched, "trim_to_budget", 1) or 0):
                _rainy = set()
                for _w in (result.get("weather_forecast") or []):
                    # A stale forecast copy never trims a shift for rain
                    # (re-audit B3#6).
                    if _w.get("stale"):
                        continue
                    try:
                        if int(_w.get("precip_pct") or 0) >= 60:
                            _rainy.add(_w.get("date"))
                    except (TypeError, ValueError):
                        pass
                # The same local scorer the fill-in passes use: a removal
                # re-scores only its own date (it used to re-score the week).
                _score_fn = None
                try:
                    _trim_scorer = _scorer_for(preview_rows) if _scorer_for else None
                    _score_fn = _trim_scorer.score if _trim_scorer is not None else None
                except Exception as _tx:
                    print(f"[schedule] score-aware trim unavailable: {_tx}")
                preview_rows, _trimmed, _hours_trimmed = _econ.trim_to_budget(
                    preview_rows, result.get("hours_budget", 0), result.get("daily_target_hours") or {},
                    constraints=_constraints, floors=_constraints.role_floors, splh=result.get("splh_by_daypart") or {},
                    rainy_dates=_rainy, patio_roles=_constraints.patio_roles, score_fn=_score_fn,
                    only_dates=_editable)
                result["trimmed"] = _trimmed
                result["hours_trimmed"] = _hours_trimmed
                if _trimmed:
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    print(f"[schedule] trimmed {_hours_trimmed}h to the budget ({len(_trimmed)} rows)")
            # Overtime first (schedule_rules.rebalance_overtime): a person
            # over the 40h line hands shifts to a same-role teammate with
            # room, or has the last shift trimmed where the role is covered,
            # before any other fix runs. Each move is a "Fixed by Cavnar AI"
            # line; what could not be moved is named in the review.
            _ot_fixes = []
            try:
                _ot = _rules.rebalance_overtime(preview_rows, _constraints,
                                                roster_roles=result.get("roster_roles") or {}, editable=_editable)
                if _ot["moves"] or _ot["trims"]:
                    preview_rows = _ot["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    for _m in _ot["moves"]:
                        _ot_fixes.append({"index": _m["index"], "from": _m["from"], "to": _m["to"],
                                          "kind": "overtime", "reason": _m["reason"]})
                    for _t in _ot["trims"]:
                        _ot_fixes.append({"index": _t["index"], "from": _t["employee"] + " " + _t["was"],
                                          "to": _t["now"], "kind": "overtime", "reason": _t["reason"]})
                # A night nobody who can lock up is on to close: a keyholder
                # already on runs on to it, inside the same overtime line.
                _co = _rules.close_out_gaps(preview_rows, _constraints, editable=_editable)
                if _co["extended"]:
                    preview_rows = _co["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    for _x in _co["extended"]:
                        _ot_fixes.append({"index": _x["index"], "from": _x["employee"] + " " + _x["from"],
                                          "to": _x["to"], "kind": "close", "reason": _x["reason"]})
                # A manager on the floor every minute anyone is (owner,
                # 10/2/26 — the highest rule): each stretch without one gets
                # a manager, by extending one already on or adding one.
                _mg = _rules.cover_manager_gaps(preview_rows, _constraints, editable=_editable)
                if _mg["extended"] or _mg["added"]:
                    preview_rows = _mg["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    for _x in _mg["extended"] + _mg["added"]:
                        _ot_fixes.append({"index": _x["index"], "from": _x["from"] or "no manager",
                                          "to": _x["employee"] + " " + _x["to"], "kind": "manager", "reason": _x["reason"]})
                result["manager_coverage"] = {"extended": len(_mg["extended"]), "added": len(_mg["added"]),
                                              "left": _mg["left"]}
                # A minor past their limit is cut to it; a run of days past
                # the rule hands one of its shifts to a teammate (10/2/26:
                # both stayed hard flags in Erik's first week).
                _pb = _rules.fix_person_breaches(preview_rows, _constraints,
                                                 roster_roles=result.get("roster_roles") or {}, editable=_editable)
                if _pb["fixes"]:
                    preview_rows = _pb["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    _ot_fixes.extend(_pb["fixes"])
                result["overtime_rebalance"] = {"over_before": _ot["over_before"], "moved": len(_ot["moves"]),
                                                "trimmed": len(_ot["trims"]), "left": _ot["left"]}
                print(f"[schedule] overtime pass: {len(_ot['moves'])} moved, {len(_ot['trims'])} trimmed, "
                      f"{len(_ot['left'])} still over ({_ot['sweeps']} checks)")
            except Exception as _otx:
                print(f"[schedule] overtime pass failed: {_otx}")
            def _price_week(_rows):
                try:
                    from models import get_role_rates as _grr
                    _rates = _grr(restaurant_id)
                    import labor as _lab_book
                    _ppl, _typ = _lab_book.person_rate_book(restaurant_id)
                    _priced = _econ.priced_cost(_rows, _rates, result.get("blended_rate") or (_rates or {}).get("_default"),
                                                person_rates=_ppl, role_typical=_typ,
                                                # Overtime is priced from the 40h line
                                                # (labor.OVERTIME_THRESHOLD_HOURS), never the
                                                # owner's hours ceiling: a 35h ceiling priced
                                                # $50 of "premium" on a week that owes none (NS3 H5).
                                                ceiling=_labor_ot_line(),
                                                salaried=getattr(_constraints, "salaried", None),
                                                base_hours={n: dict(v) for n, v in (_constraints.base_hours or {}).items()},
                                                bucket=_constraints.bucket,
                                                daily_ot_hours=_constraints.compliance.get("daily_ot_hours"))
                    result["projected_cost"] = _priced
                    _lbd = float(result.get("labor_budget_dollars") or 0)
                    result["over_budget_dollars"] = round(_priced["total"] - _lbd, 0) if _lbd else None
                except Exception as _px:
                    print(f"[schedule] pricing failed: {_px}")
            # Priced here and again on the final rows (below): the fix pass,
            # the solver and the optimizer all change rows after this point,
            # and a cost priced before them showed overtime the week no
            # longer had (9/26/26).
            _price_week(preview_rows)

            # Every rule the week is checked against, in one sweep
            # (schedule_rules.violations): the roster, the week, double
            # bookings, approved time off, availability by day and daypart,
            # the hours ceiling on the PAYROLL week including what is
            # already published, shift length, rest between shifts, minors,
            # days off, pending time off. Hard breaches that a legal
            # replacement can fix are fixed and tagged; the rest are flagged
            # for the owner and never counted as coverage.
            _viols = _rules.violations(preview_rows, _constraints)
            _fixes, _unfixed = list(_ot_fixes), []
            _hard = [v for v in _viols if v["hard"]
                     and (_editable is None or (preview_rows[v["index"]].get("date") in _editable))]
            # A missed run of days off is fixed here too (one of the person's
            # shifts to a legal teammate, least score cost); which rows may
            # move is limited to the editable days inside apply_fixes.
            _days_off = [v for v in _rules.fixable(_viols) if not v["hard"]]
            if _hard or _days_off:
                try:
                    _sig, _w = _quality_signals(restaurant_id, result)
                    _profiles_for_fix = result.get("shift_profiles") or None
                    import shift_quality as _sqf
                    # The candidate budget grows with the roster: sized for ~20
                    # people, a 55-person week ran out before its days-off
                    # fixes were tried (9/26/26).
                    _out = _sqf.apply_fixes(preview_rows, _hard + _days_off, profiles=_profiles_for_fix, weights=_w,
                                            rule_constraints=_constraints, only_dates=_editable,
                                            max_evaluations=max(_sqf.MAX_CANDIDATE_EVALUATIONS,
                                                                3 * len(result.get("roster") or [])), **_sig)
                    if _out.get("fixes"):
                        preview_rows = _out["rows"]
                        _fixes = _ot_fixes + _out["fixes"]
                        hours_scheduled = _safe_hours_sum(preview_rows)
                        _viols = _rules.violations(preview_rows, _constraints)
                    _unfixed = _out.get("unfixed") or []
                except Exception as _fx:
                    print(f"[schedule] fix pass failed: {_fx}")
            # The fix pass staffs rows the budget trim counted as nobody (a
            # person on time off): a week the trim had brought under budget
            # can be back over it. Trimmed again, by the same rules.
            try:
                _hb2 = float(result.get("hours_budget") or 0)
                if _fixes and _hb2 > 0 and int(getattr(_restaurant_for_sched, "trim_to_budget", 1) or 0) \
                        and _hourly_hours_sum(preview_rows, _constraints) > _hb2 * (1 + _econ.TRIM_TOLERANCE):
                    preview_rows, _t2, _h2 = _econ.trim_to_budget(
                        preview_rows, _hb2, result.get("daily_target_hours") or {}, constraints=_constraints,
                        floors=_constraints.role_floors, splh=result.get("splh_by_daypart") or {},
                        patio_roles=_constraints.patio_roles, only_dates=_editable)
                    if _t2:
                        result["trimmed"] = (result.get("trimmed") or []) + _t2
                        result["hours_trimmed"] = round((result.get("hours_trimmed") or 0) + _h2, 1)
                        hours_scheduled = _safe_hours_sum(preview_rows)
                        _viols = _rules.violations(preview_rows, _constraints)
            except Exception as _t2x:
                print(f"[schedule] post-fix trim failed: {_t2x}")
            # ── audit #47/#50 integration point: schedule_solver + schedule_experiments ──
            # The model decided which shifts exist; who works each is solved
            # (schedule_solver) over the hard rules and judged by Shift
            # Quality, kept only when the week scores higher and breaks no
            # rule the draft did not. Whether it runs is this week's arm of
            # the live experiment (schedule_experiments) — no model call.
            result["solver"] = {"ran": False}
            try:
                import schedule_experiments as _sx
                result["experiment_arms"] = _sx.arms_for(restaurant_id, (result.get("week_dates") or [None])[0])
                if _sx.flag(result["experiment_arms"], "solver"):
                    import schedule_solver as _solver
                    result["flagged_rows"] = {
                        ((_v.get("employee") or "").strip().lower(), _v.get("date") or "", _v.get("shift_start") or "")
                        for _v in _viols if _v.get("no_show")}
                    result["prior_week_assignments"] = _prior_week_assignments(
                        restaurant_id, before=(result.get("week_dates") or [None])[0])
                    result["staff_constraints"] = staff_constraints
                    _ssig, _sw = _quality_signals(restaurant_id, result)
                    _sres = _solver.improve(preview_rows, result, signals=_ssig, weights=_sw,
                                            constraints=_constraints, only_dates=_editable)
                    result["solver"] = _solver.summary(_sres)
                    if _sres.get("applied"):
                        preview_rows = _sres["rows"]
                        hours_scheduled = _safe_hours_sum(preview_rows)
                        _viols = _rules.violations(preview_rows, _constraints)
                    print(f"[schedule] solver {result['solver']['status']} {_sres['before_score']} -> "
                          f"{_sres['after_score']} kept={result['solver']['kept']} ({result['solver']['seconds']}s)")
            except Exception as _svx:
                print(f"[schedule] solver failed: {_svx}")
                try:
                    _ops.capture(_svx, job="schedule_solver", context=f"restaurant_id={restaurant_id}")
                except Exception:
                    pass
            # ── end #47/#50 integration point ──
            # The score as the objective (schedule_optimizer): legal adds,
            # stretches, replacements, swaps and trims aimed at the weakest
            # dimensions, each re-checked against the rule sweep, applied
            # before the owner sees the draft and listed with why. The draft
            # was never shown, so this is Cavnar finishing it, not rewriting
            # a week the owner has read.
            result["optimizer"] = {"ran": False}
            try:
                import schedule_optimizer as _opt
                result["flagged_rows"] = {
                    ((_v.get("employee") or "").strip().lower(), _v.get("date") or "", _v.get("shift_start") or "")
                    for _v in _viols if _v.get("no_show")}
                result["prior_week_assignments"] = _prior_week_assignments(
                    restaurant_id, before=(result.get("week_dates") or [None])[0])
                result["staff_constraints"] = staff_constraints
                _osig, _ow = _quality_signals(restaurant_id, result)
                result["pending_time_off"] = result.get("pending_time_off") or {
                    n: sorted(d) for n, d in (getattr(_constraints, "pending_off", None) or {}).items()}
                _ores = _opt.optimize(preview_rows, result, signals=_osig, weights=_ow, constraints=_constraints,
                                      hours_budget=(result.get("hours_budget") or 0)
                                      if int(getattr(_restaurant_for_sched, "trim_to_budget", 1) or 0) else None,
                                      max_server_overlap=getattr(_restaurant_for_sched, "section_count", None),
                                      only_dates=_editable)
                result["optimizer"] = _opt.summary(_ores, _osig)
                if _ores.get("changes"):
                    preview_rows = _ores["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    _viols = _rules.violations(preview_rows, _constraints)
                    print(f"[schedule] optimizer {_ores['before_score']} -> {_ores['after_score']} "
                          f"({len(_ores['changes'])} changes, {_ores['seconds']}s)")
            except Exception as _ox:
                print(f"[schedule] optimizer failed: {_ox}")
                try:
                    _ops.capture(_ox, job="schedule_optimizer", context=f"restaurant_id={restaurant_id}")
                except Exception:
                    pass
            # The manager rule once more on the finished rows: the fix pass,
            # the solver and the optimizer are refused a move that opens a
            # new gap, but a gap one of them could not close is closed here,
            # and the violations are read again from the rows the owner sees.
            try:
                _mg2 = _rules.cover_manager_gaps(preview_rows, _constraints, editable=_editable)
                if _mg2["extended"] or _mg2["added"]:
                    preview_rows = _mg2["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    for _x in _mg2["extended"] + _mg2["added"]:
                        _fixes.append({"index": _x["index"], "from": _x["from"] or "no manager",
                                       "to": _x["employee"] + " " + _x["to"], "kind": "manager", "reason": _x["reason"]})
                    _viols = _rules.violations(preview_rows, _constraints)
                    _price_week(preview_rows)
                _mc = result.setdefault("manager_coverage", {"extended": 0, "added": 0, "left": []})
                _mc["extended"] = _mc.get("extended", 0) + len(_mg2["extended"])
                _mc["added"] = _mc.get("added", 0) + len(_mg2["added"])
                _mc["left"] = _mg2["left"]
            except Exception as _mgx:
                print(f"[schedule] manager coverage pass failed: {_mgx}")
            # #47: the solver's changes lead "what Cavnar changed" (web and iOS read optimizer.changes).
            if (result.get("solver") or {}).get("ran"):
                import schedule_solver as _solver_m
                result["optimizer"] = _solver_m.merge_into_optimizer(result.get("optimizer"), result["solver"])
            for _v in _viols:
                if not _v["hard"]:
                    continue
                _r = preview_rows[_v["index"]]
                _r["needs_review"] = True
                _r["review_reason"] = _v["detail"] if _v["kind"] == "over_max_hours" else _v["label"]
            result["rule_violations"] = _viols
            result["review"] = _rules.summarize(_viols)
            result["review"]["fixes"] = _fixes
            result["review"]["unfixed"] = _unfixed
            # What the generation itself has to say leads the review: days it
            # could not write (P-34), days nobody could work (E-20), a first
            # week drafted with no history (E-30). A "⚠" line holds the
            # publish gate (client_api.publish_review reads the first six).
            for _gn in reversed(result.get("generation_notes") or []):
                result["review"]["lines"].insert(0, _gn)
            result["review"]["unwritten_dates"] = result.get("unwritten_dates") or []
            result["review"]["unstaffable_dates"] = result.get("unstaffable_dates") or []
            # A staffing rule the owner set that the code could not read is
            # named, so the owner checks the draft against it — the model was
            # asked to follow it, and nothing else checked (memory re-audit
            # 9/29/26, PROMPTS-1).
            _unchecked = list(getattr(_constraints, "owner_rules_unchecked", None) or [])
            result["review"]["owner_rules_unchecked"] = _unchecked
            for _txt in _unchecked[:3]:
                result["review"]["lines"].append(
                    f"Your rule \u201c{_txt[:120]}\u201d isn't one Cavnar AI can check automatically — check this "
                    f"draft against it")
            # Who on the roster got nothing, and ratings that name nobody on
            # it — both silent before, both the owner's to know.
            _on = {(_r.get("employee") or "").strip().lower() for _r in preview_rows}
            _not = [n for n in (result.get("roster") or []) if n and n.strip().lower() not in _on]
            result["not_scheduled"] = _not
            if _not:
                _full = [n for n in _not if (_constraints.employment.get(n.lower()) == "full")]
                result["review"]["lines"].append(
                    f"{len(_not)} on the roster have no shift this week: " + ", ".join(_not[:8]) + ("…" if len(_not) > 8 else "")
                    + (f" — {len(_full)} of them full-time" if _full else ""))
            _roster_low = {n.strip().lower() for n in (result.get("roster") or [])}
            _off = sorted(n for n in (result.get("operational_scores") or {}) if n.strip().lower() not in _roster_low) if _roster_low else []
            result["ratings_off_roster"] = _off
            # The section cap against what this restaurant's own history
            # runs: every requirement is held to the cap, and where the
            # history runs over it the owner is told the cap is probably
            # wrong, rather than every such shift quietly scoring short.
            try:
                import staffing_curve as _stc
                _cap_roles = getattr(_constraints, "foh_roles", None) or {"server"}
                _conf = _stc.cap_conflicts(result.get("typical_headcount") or {},
                                           getattr(_constraints, "section_cap", 0), _cap_roles)
                result["section_cap_conflicts"] = _conf
                _line = _stc.cap_conflict_line(
                    _conf, "servers" if set(_cap_roles) == {"server"} else "front-of-house staff")
                if _line:
                    result["review"]["lines"].append(_line)
            except Exception as _scx:
                print(f"[schedule] section cap check failed: {_scx}")
                result["section_cap_conflicts"] = []
            if _off:
                result["review"]["lines"].append(
                    f"{len(_off)} Operational Score{'s' if len(_off) != 1 else ''} belong to names not on the roster "
                    f"({', '.join(_off[:4])}{'…' if len(_off) > 4 else ''}) — they judge nobody until the names match")
            # The budget is a ceiling. The owner's trim (trim_to_budget, on by
            # default) has already taken the most discretionary hours back to
            # it, each removal reported; what is still over — the trim never
            # goes below a floor — is named at the top of the review (the
            # comment here used to say the week was never trimmed;
            # schedule audit 10/3/26 P-45).
            try:
                _hs = _hourly_hours_sum(preview_rows, _constraints)
                _hb = float(result.get("hours_budget") or 0)
            except (TypeError, ValueError):
                _hs = _hb = 0.0
            if _hb > 0 and _hs > _hb * 1.02:
                result["review"]["over_budget_hours"] = round(_hs - _hb, 1)
                result["review"]["lines"].insert(
                    0, f"⚠ {_hs:,.0f}h scheduled against a {_hb:,.0f}h budget — {_hs - _hb:,.0f}h over the ceiling")
            result["review"]["trimmed"] = result.get("trimmed") or []
            result["review"]["hours_trimmed"] = result.get("hours_trimmed") or 0.0
            result["review"]["staggered"] = result.get("staggered") or []
            for _line in _econ.trim_lines(result.get("trimmed") or [], result.get("hours_trimmed") or 0.0, _hb)[:3]:
                result["review"]["lines"].append(_line)
            # Who this draft puts past the ceiling in the payroll week (with
            # a same-role person who has room), and the days most likely to
            # lose somebody to a no-show (schedule_learning).
            try:
                import schedule_learning as _sl
                from time_utils import mdy as _mdy
                _ot = _sl.overtime_forecast(preview_rows, constraints=_constraints,
                                            roster_roles=result.get("roster_roles") or None)
                try:
                    from models import get_role_rates as _grr_ot, get_restaurant as _gr_ot
                    _sl.price_overtime_moves(_ot, _grr_ot(restaurant_id),
                                             getattr(_gr_ot(restaurant_id), "hourly_rate", None) or None)
                except Exception as _px:
                    print(f"[schedule] overtime moves unpriced: {_px}")
                result["overtime_forecast"] = _ot
                for _o in _ot[:3]:
                    result["review"]["lines"].append(_o["text"])
                _sb = _sl.standby_days(restaurant_id, result.get("week_dates") or [], rows=preview_rows)
                result["standby_days"] = _sb
                for _d in _sb:
                    _who = (_d.get("standby") or {})
                    result["review"]["lines"].append(
                        f"Worth a standby on {_d['day']} {_mdy(_d['date'])}: about a "
                        f"{int(round(_d['chance_of_a_no_show'] * 100))}% chance somebody scheduled doesn't show, "
                        f"from their own attendance."
                        + (f" {_who['employee']} ({_who['role'] or 'same role'}) is off and could be on call."
                           if _who.get("employee") else ""))
            except Exception as _slx:
                print(f"[schedule] overtime/standby read failed: {_slx}")
            # Rows the manager has repeatedly edited away and the draft put
            # back anyway: said before they have to do it again.
            # Then every other row the manager's own edit history says they
            # are likely to change (schedule_learning.predict_row_edits),
            # with the likelihood and why — listed in the review's Likely
            # edits block rather than as lines.
            try:
                result["likely_edits"] = likely_edits(restaurant_id, preview_rows)
                for _le in [x for x in result["likely_edits"] if x.get("kind") != "predicted"][:3]:
                    result["review"]["lines"].append(_le["text"])
            except Exception as _lex:
                print(f"[schedule] likely-edit read failed: {_lex}")
            # How far this restaurant's demand forecasts have been off (K8):
            # the draft is staffed to that forecast, so the review says it.
            try:
                import demand as _demand_acc
                result["demand_accuracy"] = _demand_acc.demand_accuracy(restaurant_id)
                result["week_projection_accuracy"] = _demand_acc.week_projection_accuracy(restaurant_id)
            except Exception as _dax:
                print(f"[schedule] demand accuracy read failed: {_dax}")
            # Sales per labor hour against the objective the draft was
            # written to, and the borrowed starting headcount, said.
            try:
                import schedule_economics as _econ_r
                _sr = _econ_r.splh_report(result.get("splh_objective") or {}, preview_rows,
                                          result.get("demand_by_date") or {})
                result["splh_report"] = _sr
                if _sr.get("line"):
                    result["review"]["lines"].append(_sr["line"])
            except Exception as _srx:
                print(f"[schedule] splh report failed: {_srx}")
            _start = result.get("starting_headcount") or {}
            if _start.get("available"):
                result["review"]["lines"].append(
                    f"Staffing numbers marked borrowed come from {_start.get('n')}+ "
                    f"{(_start.get('cohort_label') or 'Restaurants on Cavnar AI')[:1].lower()}"
                    f"{(_start.get('cohort_label') or 'Restaurants on Cavnar AI')[1:]} (people on the floor per $1k of "
                    f"sales, scaled to {_start.get('basis')})"
                    + (" — the type was inferred from the restaurant's name, not set" if _start.get("inferred") else "")
                    + " — this restaurant has no schedule history of its own yet.")
            _pc = result.get("projected_cost") or {}
            if _pc.get("overtime_hours"):
                result["review"]["lines"].append(
                    f"{_pc['overtime_hours']:g}h of overtime priced at {_pc['multiplier']}× — ${_pc['overtime_premium']:,.0f} of premium in a ${_pc['total']:,.0f} week")
            result["rows_needing_review"] = sum(1 for _r in preview_rows if _r.get("needs_review"))
            # Shift strength, checked against the finished schedule rather
            # than trusted to the prompt. The same discipline close times and
            # the server cap already get: state the rule to the model, then
            # verify what it actually produced.
            # The legacy strength banner is deliberately gone. It ran a second
            # leadership check with different semantics — silently dropping
            # any rule without a minimum score, which the quality engine
            # enforces — and both rendered, so an owner read "every target
            # met" a few centimetres above "needs 2 bartenders, found 1".
            # Contradiction on one screen costs more trust than either
            # message being wrong alone. dim_leadership and
            # dim_operational_strength cover everything it reported.
            result["strength"] = {"checked": False, "superseded_by": "quality"}

            # Shift Quality — the same discipline as the strength check, over
            # every dimension rather than one. Wrapped whole: a schedule that
            # generated fine must never be lost because a scoring dimension
            # raised, so a failure here degrades to "not scored" and the week
            # still ships.
            try:
                result["staff_constraints"] = staff_constraints
                # Only the flags that mean the person is not really on that
                # shift. A row flagged "over 40h for the week" still has a
                # real person on the floor — it is a cost problem, not a
                # coverage one — and excluding it made a fully staffed week
                # read as zero coverage on every shift.
                _NO_SHOW_REASONS = tuple(sorted(_rules.NO_SHOW))
                # schedule_rules.NO_SHOW, each on its own line so a reader can
                # grep for the phrase the owner sees:
                #   not on the staff list · no longer on the roster
                #   date is outside next week
                #   double-booked at the same start time · two shifts overlap
                #   on approved time off · marked unavailable that day
                #   not available for that daypart · already scheduled at another location
                # A row over the hours ceiling is NOT here: the person is
                # still on the floor.
                result["flagged_rows"] = {
                    ((_v.get("employee") or "").strip().lower(), _v.get("date") or "",
                     _v.get("shift_start") or "")
                    for _v in (result.get("rule_violations") or []) if _v.get("no_show")}
                result["prior_week_assignments"] = _prior_week_assignments(
                    restaurant_id, before=(result.get("week_dates") or [None])[0])
                from models import sibling_location_shifts as _sibs
                result["elsewhere"] = _sibs(
                    restaurant_id, sorted({(_r.get("date") or "") for _r in preview_rows}))
                _quality, _whatif = _score_schedule_quality(
                    restaurant_id, preview_rows, result,
                    rows_needing_review=result.get("rows_needing_review", 0),
                    dropped_rows=len(_dropped_rows),
                )
                if isinstance(_quality, dict):
                    _quality["optimizer"] = result.get("optimizer") or {"ran": False}
                    # The per-day hour targets belong to this generation and
                    # cannot be re-derived; kept with the stored verdict so a
                    # later rescore from a client that never held them (iOS)
                    # is judged on the same targets (stored_daily_targets).
                    _quality["daily_target_hours"] = result.get("daily_target_hours") or {}
                result["quality"] = _quality
                result["what_if"] = _whatif
                from models import capability_version as _capver
                result["capability_version"] = _capver(restaurant_id)
                if _quality.get("checked"):
                    print(f"[schedule] shift quality {_quality['score']}/100 "
                          f"({_quality['band']}), confidence {_quality['confidence']['level']}"
                          + (f", what-if {_whatif['improvement']:+d}" if _whatif.get("ran") else ""))
            except Exception as _qx:
                print(f"[schedule] quality engine failed: {_qx}")
                result["quality"] = {"checked": False, "error": _safe_err(_qx)}
                result["what_if"] = {"ran": False}

            _flagged = sum(1 for _r in preview_rows if _r.get("needs_review"))
            result["rows_needing_review"] = _flagged
            if _flagged:
                print(f"[schedule] {_flagged} row(s) flagged for review")

            # "What changed" is computed from the diff against the last
            # published week, never written by the model about a draft it
            # cannot see. The model's own bullets ride along as narrative.
            try:
                _prior_rows = result.get("prior_published_rows") or []
                if _prior_rows:
                    _d = _versions.diff(_prior_rows, preview_rows)
                    result["summary"] = _versions.diff_lines(_d)
                    result["diff_vs_published"] = {k: _d[k] for k in ("changes", "hours_before", "hours_after")}
                else:
                    _people = {(_r.get("employee") or "").strip() for _r in preview_rows if _r.get("employee")}
                    _by_day = {}
                    for _r in preview_rows:
                        try:
                            _by_day[_r.get("day")] = _by_day.get(_r.get("day"), 0.0) + float(_r.get("scheduled_hours") or 0)
                        except (TypeError, ValueError):
                            pass
                    _busiest = max(_by_day.items(), key=lambda kv: kv[1])[0] if _by_day else None
                    result["summary"] = [f"{len(preview_rows)} shifts, {round(hours_scheduled):,}h across {len(_people)} people."
                                         + (f" Busiest day {_busiest}." if _busiest else ""),
                                         "First published week on file — no prior week to compare against."]
            except Exception as _sx:
                print(f"[schedule] summary diff failed: {_sx}")

            # Rebuild schedule_csv from the (possibly repaired) rows so the
            # downloadable/shared CSV matches what the app displays instead
            # of shipping the pre-repair text out from under it.
            #
            # needs_review used to live only on preview_rows, so the flag was
            # lost the moment the CSV was rebuilt — a row generation could not
            # repair was saved to history and emailed to staff with nothing
            # marking it. It rides in the notes column now, which is the one
            # field a human actually reads on the printed schedule.
            if preview_rows:
                # Stations last: the fix pass, the solver and the optimizer
                # may have moved cooks since the first pass. Close what they
                # opened, then name each cook's station for the screens.
                try:
                    if getattr(_constraints, "stations", None):
                        preview_rows, _st2, _ = _ensure_station_coverage(
                            preview_rows, result.get("week_dates", []), result.get("week_days", []),
                            _close_times, _role_close_buffers, constraints=_constraints)
                        if _st2:
                            hours_scheduled = _safe_hours_sum(preview_rows)
                        _stations = station_report(preview_rows, _constraints, result.get("week_dates", []))
                        result["stations"] = _stations
                        if isinstance(result.get("review"), dict):
                            result["review"]["stations"] = _stations
                except Exception as _stx:
                    print(f"[schedule] station assignment skipped: {_stx}")
                _price_week(preview_rows)
                _lines_out = [",".join(_COLS)]
                for _r in preview_rows:
                    if _r.get("needs_review"):
                        _n = (_r.get("notes") or "").strip()
                        _why = _r.get("review_reason") or "could not be auto-checked"
                        _mark = f"NEEDS REVIEW: {_why}"
                        if "NEEDS REVIEW" not in _n:
                            _r["notes"] = f"{_n} — {_mark}" if _n else _mark
                    _lines_out.append(",".join(str(_r.get(c, "") or "").replace(",", ";") for c in _COLS))
                result["schedule_csv"] = "\n".join(_lines_out)
        except ScheduleGenerationError:
            raise                      # already a sentence the owner can read
        except Exception as _csv_ex:
            # Every safeguard lives inside that block. A crash there used to
            # fall through to save and ship the raw model text as a finished
            # week with "0 hard"; now it is the generation's failure.
            import ops as _ops_fail
            _ops_fail.capture(_csv_ex, job="schedule_checks", context=f"restaurant_id={restaurant_id}")
            raise ScheduleGenerationError("The draft was written but its checks failed, so nothing was saved. Try again.")

        # Which soft requirements the final rows honoured — read from the
        # rows, so the review names what the draft did, not what the model
        # says it did (memory audit 9/29/26, dsr_to_schedule).
        try:
            import staffing_signals as _stsig_ap
            _typ = {tuple(k.split("|", 1)): v for k, v in (result.pop("_soft_typical", None) or {}).items()}
            result["soft_requirements"] = _stsig_ap.applied(result.get("soft_requirements") or [], preview_rows, _typ)
            if isinstance(result.get("review"), dict) and result["soft_requirements"]:
                result["review"]["soft_requirements"] = result["soft_requirements"]
            # Each review "+1" is a recommendation the owner is shown here;
            # publishing a week that carries it records it as implemented
            # (staffing_signals.record_published, re-audit CROSSMODULE-10).
            _stsig_ap.present_requirements(restaurant_id, result["soft_requirements"])
        except Exception as _sax:
            print(f"[schedule] soft requirement check skipped: {_sax}")
            result.pop("_soft_typical", None)
        # A job past its own deadline, or one a poll has already called dead,
        # saves nothing (P-22): the owner was told it didn't finish, and a
        # draft turning up in history afterwards contradicted that. Checked
        # with half the store's grace still in hand, so the save and the
        # verdict can never cross.
        if (clock is not None and clock.seconds_left() < -_ops.ASYNC_DEADLINE_GRACE_SECONDS / 2) \
                or not _ops.job_still_pending(job_id):
            raise ScheduleGenerationError("The schedule didn't finish in the time a generation is given, so "
                                          "nothing was saved. Try again.")
        _history_id = None
        try:
            from models import save_schedule_history
            _wd = result.get("week_dates", [])
            _history_id = save_schedule_history(
                restaurant_id, _wd[0] if _wd else None, _wd[-1] if _wd else None,
                round(hours_scheduled, 1), result.get("hours_budget", 0), result.get("labor_target", 30),
                result["schedule_csv"], result.get("summary", []),
                quality=result.get("quality"), what_if=result.get("what_if"),
            )
            _annotate_history(_history_id, restaurant_id, review=result.get("review"),
                              seconds=result.get("generation_seconds"),
                              weather=result.get("weather_forecast"),
                              economics={"projected_revenue": result.get("projected_revenue"),
                                         "projected_revenue_source": result.get("projected_revenue_source"),
                                         "labor_budget_dollars": result.get("labor_budget_dollars"),
                                         "daily_target_hours": result.get("daily_target_hours") or {},
                                         "demand_data_through": result.get("demand_data_through")})
            # A whole week built discharges "next week's schedule isn't
            # built"; a partial redo of a few days does not, nor a week with
            # days the generation could not write (P-34).
            if _history_id and not _pinned and not result.get("unwritten_dates"):
                mark_next_week_built(restaurant_id, _history_id)
            # #50: this week's experiment arm, with the draft's score (internal only).
            try:
                import schedule_experiments as _sx_rec
                _sx_rec.record(restaurant_id, _history_id, result.get("experiment_arms") or [],
                               (result.get("quality") or {}).get("score"),
                               solver_applied=(result.get("solver") or {}).get("applied")
                               if (result.get("solver") or {}).get("ran") else None)
            except Exception as _arx:
                print(f"[schedule] experiment arm not recorded: {_arx}")
            try:
                _versions.append(restaurant_id, _history_id, "generated", result["schedule_csv"],
                                 quality=result.get("quality"), saved_by="Cavnar AI", saved_authority="system")
            except Exception as _vx:
                print(f"[schedule] version save failed: {_vx}")
            try:
                from client_api import log_account_event
                log_account_event(restaurant_id, "schedule_generated", None,
                                  detail=f"week of {_wd[0] if _wd else '?'} · {len(preview_rows)} shifts · "
                                         f"{result.get('rows_needing_review', 0)} flagged")
            except Exception:
                pass
        except Exception as _hist_ex:
            print(f"[schedule history] save error: {_hist_ex}")
            # The history row is the ONLY durable copy of this schedule — the
            # async result is deleted the first time it is polled. If the save
            # failed and the poll is then lost, a paid-for Claude call is gone.
            _ops.capture(_hist_ex, job="schedule_generate",
                         context=f"restaurant_id={restaurant_id} — history save failed, result is poll-only")

        if _dropped_rows:
            # Not fatal — the rest of the week is still usable — but the owner
            # must not be handed a short schedule that looks complete.
            print(f"[schedule] dropped {len(_dropped_rows)} malformed row(s): {_dropped_rows[:3]}")
            # Rows the model wrote that could not be read: an AI-quality
            # finding (fix round G #58), not a failing job.
            import ai_utils as _ai_q
            _ai_q.record_quality_event("labor_schedule", "line_dropped", restaurant_id=restaurant_id,
                                       action="labor_schedule", n=len(_dropped_rows),
                                       detail=f"{len(_dropped_rows)} malformed schedule row(s) dropped")

        _payload = dict(
            ok=True,
            history_id=_history_id,
            # Every rule breach, hard and soft, with the person and the
            # date; what the fix pass repaired; what it could not.
            rule_violations=result.get("rule_violations") or [],
            review=result.get("review") or {"hard": 0, "soft": 0, "lines": [], "fixes": [], "unfixed": []},
            pending_time_off=result.get("pending_time_off") or {},
            narrative=result.get("narrative") or [],
            generation_seconds=result.get("generation_seconds"),
            # Whether the week took more than one model call — a bool, as both
            # clients read it (the phone decodes Bool and failed on the count
            # this used to be); the count itself is `calls`.
            chunked=int(result.get("chunked") or 1) > 1,
            calls=int(result.get("chunked") or 1),
            roster=result.get("roster") or [],
            dropped_rows=len(_dropped_rows),
            dropped_row_note=(
                f"{len(_dropped_rows)} line(s) of the generated schedule could not be read and were "
                "left out. Check the week before you publish it."
            ) if _dropped_rows else None,
            schedule_csv=result["schedule_csv"],
            summary=result.get("summary", []),
            preview_rows=preview_rows,
            # How many rows failed a check the model cannot be trusted to
            # make for itself — a hallucinated name, a date outside the
            # generated week, a double booking, a week over 40 hours. The
            # owner must see this before publishing to staff.
            rows_needing_review=result.get("rows_needing_review", 0),
            # Which shifts met their Operational Score target, which fell
            # short and why, and any shift leader requirement that could not
            # be satisfied. Never a silent miss.
            # Retained as a key so an older build of either client keeps
            # decoding; the verdict itself now lives in `quality`.
            strength=result.get("strength") or {"checked": False},
            # The Shift Quality Engine's verdict: one score per shift across
            # every dimension that had data, the week's roll-up, why each
            # shift scored what it did, and how much the engine actually
            # knew when it said so.
            quality=result.get("quality") or {"checked": False},
            # Moves whenever a rating, target, profile or weighting moves, so
            # a client holding a cached schedule can tell that the score on
            # it was built against inputs that no longer exist.
            capability_version=result.get("capability_version"),
            # Alternative arrangements of the same people, and why this one
            # won. Never a second generation — same headcount, same hours,
            # same roles, only who works which shift.
            what_if=result.get("what_if") or {"ran": False},
            # What Cavnar changed to raise the score before the owner saw
            # the draft, each change with why, and what it could not fix.
            optimizer=result.get("optimizer") or {"ran": False},
            week_dates=result.get("week_dates", []),
            week_days=result.get("week_days", []),
            projected_revenue=result.get("projected_revenue", 0),
            hours_budget=result.get("hours_budget", 0),
            labor_budget_dollars=result.get("labor_budget_dollars", 0),
            hours_scheduled=round(hours_scheduled, 1),
            labor_target=result.get("labor_target", 30),
            # Needed by the live re-score a manager triggers by moving a
            # shift: the per-day hour targets belong to THIS generation and
            # cannot be re-derived afterwards, so they ride with the result.
            daily_target_hours=result.get("daily_target_hours") or {},
            staff_constraints=staff_constraints,
            hours_added_by_backstop=hours_added,
            backstop_added_dates=added_dates,
            backstop_extended_dates=extended_dates,
            backstop_trimmed_dates=trimmed_dates,
            backstop_pizza_added_dates=pizza_added_dates,
            trimmed=result.get("trimmed") or [],
            hours_trimmed=result.get("hours_trimmed") or 0.0,
            staggered=result.get("staggered") or [],
            projected_cost=result.get("projected_cost"),
            over_budget_dollars=result.get("over_budget_dollars"),
            projected_revenue_source=result.get("projected_revenue_source"),
            hourly_profile_ready=result.get("hourly_profile_ready", False),
            demand_data_through=result.get("demand_data_through"),
            reservation_feed=result.get("reservation_feed"),
            holiday_lift=result.get("holiday_lift") or {},
            could_hold=result.get("could_hold") or {},
            departments=result.get("departments") or [],
            regenerated_dates=result.get("regenerated_dates") or [],
            slices=result.get("slices") or [],
            not_scheduled=result.get("not_scheduled") or [],
            ratings_off_roster=result.get("ratings_off_roster") or [],
            # Shifts whose usual front-of-house crew is over the section
            # count (staffing_curve.cap_conflicts): the cap is probably wrong.
            section_cap_conflicts=result.get("section_cap_conflicts") or [],
            overtime_forecast=result.get("overtime_forecast") or [],
            standby_days=result.get("standby_days") or [],
            likely_edits=result.get("likely_edits") or [],
            demand_accuracy=result.get("demand_accuracy"),
            week_projection_accuracy=result.get("week_projection_accuracy"),
            # What schedule learning set the draft against: the rotation
            # plan, the sales-per-labor-hour objective and how the draft
            # landed on it, and a borrowed starting headcount (new accounts).
            rotation_plan=result.get("rotation_plan") or {},
            splh_objective=result.get("splh_objective") or {"available": False},
            splh_report=result.get("splh_report") or {},
            starting_headcount=result.get("starting_headcount") or {"available": False},
            gate=result.get("gate") or {"ran": False},
            # What the generation could not do, said (schedule audit 10/3/26):
            # days it could not write, the rest kept (P-34) — `partial`; days
            # nobody on the team can work (E-20); a first week drafted with no
            # history of its own, a starting point (E-30); the redo it was,
            # with the owner's reason (PR-18, recorded by schedule learning).
            partial=bool(result.get("unwritten_dates")) and not result.get("redo"),
            unwritten_dates=result.get("unwritten_dates") or [],
            unstaffable_dates=result.get("unstaffable_dates") or [],
            starting_point=result.get("starting_point") or {"no_history": False},
            redo=result.get("redo"),
        )
        _q_now = (result.get("quality") or {}).get("score")
        if _fallback:
            # Kept only if the days it was asked to fix got better, judged on
            # those days alone (a shift the rewrite dropped counts as 0), and
            # the week as a whole is not worse. The whole-week average alone
            # kept a rewrite with ONE Saturday server over one with five.
            before_local = _gate_local(_fallback["quality"], _fallback["dates"])
            after_local = _gate_local(result.get("quality") or {}, _fallback["dates"])
            better = (after_local is not None and before_local is not None and after_local > before_local
                      and (_q_now or 0) >= (_fallback["score"] or 0) - 1)
            if not better:
                _restore_draft(restaurant_id, _fallback["history_id"], _history_id)
                _fb = dict(_fallback["payload"])
                _fb["gate"] = {"ran": True, "kept": "original", "dates": _fallback["dates"],
                               "reason": (f"Cavnar AI rewrote the weakest days to fix them, but the rewrite was no better "
                                          f"on those days, so your original draft was kept.")}
                _ops.finish_async_job(job_id, "done", _fb)
                return
        _gate = _quality_gate(result) if gate else None
        if _gate and clock is not None and clock.model_seconds_left() < GATE_MIN_MODEL_SECONDS:
            # Not enough of the job's time left to rewrite the weak days and
            # check the rewrite (P-22): the draft stands as it is.
            print(f"[schedule] quality gate skipped: {int(clock.model_seconds_left())}s of model time left")
            _gate = None
        if _gate and _history_id:
            try:
                import inspect as _insp
                if "focus" in _insp.signature(_build_schedule_result).parameters:
                    print(f"[schedule] quality gate: regenerating {_gate['dates']} ({_gate['reason']})")
                    return _run_schedule_job(
                        job_id, restaurant_id, week_start=week_start, dates=_gate["dates"],
                        base_history_id=_history_id, focus=_gate["focus"], gate=False,
                        _fallback={"score": _q_now, "history_id": _history_id, "dates": _gate["dates"],
                                   "quality": result.get("quality") or {},
                                   "payload": dict(_payload, gate={"ran": True, **_gate})})
            except Exception as _gx:
                print(f"[schedule] quality gate failed: {_gx}")
        if focus:
            _payload["gate"] = {"ran": True, "kept": "regenerated", "focus": list(focus)[:12],
                                "dates": list(dates or []),
                                "reason": "The weakest days were regenerated with what was wrong with them."}
        _ops.finish_async_job(job_id, "done", _payload)
    except Exception as e:
        tb = _tb.format_exc()
        print(f"[schedule job] FAILED:\n{tb}")
        try:
            _ops.capture(e, job="schedule_generate", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        if _fallback:
            # The rewrite failed, but the draft it was improving is saved and
            # sound: the owner gets that draft, not a failure it did not have.
            try:
                _restore_draft(restaurant_id, _fallback["history_id"], locals().get("_history_id"))
            except Exception:
                pass
            _fb = dict(_fallback["payload"])
            _fb["gate"] = {"ran": True, "kept": "original", "dates": _fallback["dates"],
                           "reason": "Cavnar AI tried to rewrite the weakest days but couldn't just now, so your draft is as it was."}
            _ops.finish_async_job(job_id, "done", _fb)
            return
        # The owner gets a sentence that says what stopped it (P-44), never
        # the exception or the traceback.
        _ops.finish_async_job(job_id, "error", {"ok": False, "error": generation_error_message(e, clock)})


def generation_error_message(exc, clock=None) -> str:
    """The sentence an owner reads when a generation failed (schedule audit
    10/3/26 P-44): what stopped it and what to do about it. A paused AI
    budget, an open provider breaker and data that isn't current all read
    "try again in a minute", and the owner retried into the same refusal.
    Never the exception's own text (provider ids, raw bodies — DATA-46),
    except a ScheduleGenerationError's, which is written for the owner, and
    the budget stop's, which is (ai_utils.user_facing_error)."""
    if isinstance(exc, ScheduleGenerationError):
        return str(exc)
    kind = _failure_kind(exc)
    if kind == "budget":
        return "The schedule wasn't generated. " + str(exc)
    if kind == "provider_down":
        return ("Cavnar AI's model provider isn't responding right now, so the schedule wasn't generated and "
                "nothing was saved. Try again in a few minutes.")
    if kind == "data":
        why = str(((getattr(exc, "readiness", None) or {}).get("reason")) or "").strip().rstrip(".")
        return ("The schedule wasn't generated because the data it is built from isn't current"
                + (f": {why}" if why else "") + ". Nothing was saved — generate again once it has caught up.")
    if kind == "refused":
        return ("The model declined to write this schedule, so nothing was saved. If your notes or rules ask "
                "for something unusual, check them, then generate again.")
    if kind == "timeout":
        mins = clock.minutes() if clock is not None else None
        return ("The schedule took longer than " + (f"the {mins} minutes" if mins else "the time")
                + " a generation is given, so nothing was saved. Generate again — if it happens again, redo a "
                  "few days at a time.")
    if kind == "provider":
        return ("Cavnar AI's model provider is overloaded or unreachable right now, so the schedule wasn't "
                "generated and nothing was saved. Try again in a few minutes.")
    if kind == "config":
        return ("Cavnar AI couldn't reach its model — a problem on our side, and we've been alerted — so the "
                "schedule wasn't generated. Try again later.")
    return ("The schedule couldn't be generated because of a problem on our side — we've been alerted and "
            "nothing was saved. Try again in a few minutes.")


_COLS_PINNED = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")


def _annotate_history(history_id, restaurant_id, review=None, seconds=None, weather=None, economics=None):
    """The columns save_schedule_history predates: the review verdict, how
    long generation took, the weather forecast it was written against, and
    its economics (forecast sales and basis, labor budget, daily hour
    targets) so a reopened week can state its labor %."""
    if not history_id:
        return
    from models import get_conn, _ensure_history_columns
    conn = get_conn()
    try:
        _ensure_history_columns(conn)
        conn.execute("UPDATE schedule_history SET review_json=?, generation_seconds=?, weather_json=?, "
                     "economics_json=? WHERE id=? AND restaurant_id=?",
                     (json.dumps(review) if review else None, seconds,
                      json.dumps(weather) if weather else None,
                      json.dumps(economics, default=str) if economics else None, history_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()


def _rows_to_csv_text(rows: list) -> str:
    cols = ["date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes"]
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(str(r.get(c, "") or "").replace(",", ";") for c in cols))
    return "\n".join(lines)


# Hard flags about the shift's staffing rather than the person on it: one the
# week already had on a shift is not a newcomer's to answer for.
_STAFFING_GAPS = frozenset({"no_manager_on_duty", "coverage_floor", "keyholder_until_close", "nobody_at_close",
                            "no_manager"})


def replacement_is_legal(restaurant_id, rows: list, index: int, name: str, constraints=None):
    """(ok, reason): could `name` take rows[index] outright, by every rule
    the generation itself is checked against? One answer for the web and
    iOS replacement pickers and the open-shift claim.

    The reason is the violation sweep's own label wherever one applies: the
    move is tried on a copy of the week and any hard rule it would newly
    break for `name` — a minor past the latest end, a certification the
    role needs, the person's hours window, rest, the hours ceiling — is the
    answer (SCHED-6). A double that does not overlap is allowed (SCHED-34).
    A free-text note on file is not read as a refusal: the engine cannot
    read it, and treating every note as "never" locked anyone with a
    compliment on file out of every claim (SCHED-35). The automatic swap
    search still leaves noted people alone.

    `constraints` lets a caller that already built the week's
    schedule_rules.Constraints pass it in (the replacement picker checks
    every roster member against one set)."""
    import shift_quality as _sq
    from models import get_unavailability_map
    try:
        c = constraints
        if c is None:
            dates = sorted({r.get("date") for r in rows if r.get("date")})
            week_days = [__import__("datetime").datetime.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates]
            c = _rules.build_constraints(restaurant_id, dates, week_days)
        row = rows[index]
        ok, why = c.can_work(name, row.get("date", ""), _rules.daypart_of(row.get("shift_start", "")))
        if not ok:
            return False, why
        low = (name or "").strip().lower()
        trial = [dict(r) for r in rows]
        trial[index]["employee"] = name

        def _hard_for_name(rs, inherit=False):
            # `inherit`: a staffing gap the week already had ON this shift
            # (nobody until close, a floor short) is the week's, not the
            # newcomer's — it was charged to whoever held the shift, so it
            # read as new for anyone taking it, and a restaurant with any
            # standing gap could never have a cover claimed or offered
            # (employee audit B3, H2).
            return {(v["kind"], v["index"]): v for v in _rules.violations(rs, c)
                    if v["hard"] and ((v.get("employee") or "").strip().lower() == low
                                      or (inherit and v.get("index") == index and v["kind"] in _STAFFING_GAPS))}
        before, after = _hard_for_name(rows, inherit=True), _hard_for_name(trial)
        new = sorted((k for k in after if k not in before), key=lambda k: (k[1] != index, k[1]))
        if new:
            v = after[new[0]]
            label = _rules.LABELS.get(v["kind"], v["kind"])
            return False, label if v.get("detail") in (None, "", label) else f"{label} ({v['detail']})"
        availability = get_unavailability_map(restaurant_id, week_dates=list(c.week_dates or []) or None)
        idx = _sq._SwapIndex(rows, availability, {}, _rules_for_swaps(c))
        if not idx.replacement_legal(index, name, allow_double=True):
            return False, "would break a rule (hours, rest, or a shift that overlaps one they already have)"
        return True, ""
    except Exception as e:
        # The person asking is an employee or a manager, not a developer:
        # the exception goes to the log, a plain sentence to them (MOD-EMP-10).
        print(f"[schedule] replacement check failed rid={restaurant_id}: {e!r}")
        return False, "we couldn't check that shift just now — try again, or ask a manager"


def _rules_for_swaps(c) -> dict:
    """The constraint set as the plain dicts shift_quality's swap index
    reads — that module stays free of database code."""
    if c is None:
        return {}
    buckets = {c.bucket(d) for d in (c.week_dates or [])}
    base = {}
    for name, per in (c.base_hours or {}).items():
        base[name] = sum(h for b, h in per.items() if b in buckets)
    return {
        "blocked_dates": c.blocked_dates, "daypart_avail": c.daypart_avail,
        "hours_limits": c.hours_limits, "base_hours": base, "base_rows": c.base_rows,
        "weekly_ceiling": c.compliance.get("weekly_hours_ceiling"),
        "min_rest_hours": c.compliance.get("min_rest_hours"),
        "inactive": sorted(c.inactive),
        # the sweep's per-person rules, so a swap or fix can never hand a
        # minor a late close or a bar shift to someone uncertified (SCHED-6)
        "minors": sorted(c.minors),
        "minor_latest_end": _rules.parse_minutes(c.compliance.get("minor_latest_end") or ""),
        "minor_max_daily_hours": c.compliance.get("minor_max_daily_hours"),
        "certifications": {k: sorted(v) for k, v in (c.certifications or {}).items()},
        "role_requirements": {k: sorted(v) for k, v in (c.role_requirements or {}).items()},
        "time_windows": dict(c.time_windows or {}),
        "pending_off": {n: sorted(d) for n, d in (getattr(c, "pending_off", None) or {}).items()},
    }


def _safe_hourly_profile(restaurant_id) -> dict:
    """_hourly_profile, or {} (today's behaviour) when it cannot be read."""
    try:
        return _hourly_profile(restaurant_id) or {}
    except Exception as _hx:
        print(f"[schedule] hourly profile unavailable: {_hx}")
        return {}


def _hourly_profile(restaurant_id) -> dict:
    """{weekday: {hour: share of the day's sales}} from the intraday
    captures, when at least three same-weekday days were measured. Sales
    captured hourly are cumulative, so the hour's own share is the step."""
    from models import get_conn
    conn = get_conn()
    try:
        rows = conn.execute("SELECT weekday, business_date, captured_hour, net_sales FROM pos_intraday "
                            "WHERE restaurant_id=? AND business_date >= date('now', '-56 days') "
                            "ORDER BY business_date, captured_hour", (restaurant_id,)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    by_day = {}
    for r in rows:
        by_day.setdefault((r["weekday"], r["business_date"]), []).append((r["captured_hour"], float(r["net_sales"] or 0)))
    per_weekday = {}
    for (weekday, _date), pts in by_day.items():
        pts.sort()
        prev = 0.0
        steps = {}
        for h, cum in pts:
            steps[h] = max(0.0, cum - prev)
            prev = cum
        total = sum(steps.values())
        if total <= 0:
            continue
        per_weekday.setdefault(weekday, []).append({h: v / total for h, v in steps.items()})
    out = {}
    for weekday, days in per_weekday.items():
        if len(days) < 3:
            continue
        hours = sorted({h for d in days for h in d})
        out[weekday] = {h: round(sorted(d.get(h, 0.0) for d in days)[len(days) // 2], 4) for h in hours}
    return out
