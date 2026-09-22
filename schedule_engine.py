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
import json
import re

from ai_guard import safe_error as _safe_err
from models import get_restaurant

import schedule_rules as _rules
import schedule_versions as _versions
import staff_settings as _staff
import demand_signals as _signals

# When one call cannot carry the week, it is written in this many parts.
CHUNK_ROSTER_THRESHOLD = 80


def _no_shift_data_message(restaurant_id, restaurant=None):
    """Say which restaurant is missing shifts, and what is actually missing.

    "No shift data available — upload shifts CSV first" was shown beside a
    Labor tab full of numbers, which reads as a contradiction and gives
    nobody anywhere to start. The numbers on that page can come from a
    bundled sample when a restaurant has uploaded nothing, so the page
    looking populated proves nothing — and that is exactly the confusion
    worth naming.
    """
    from models import get_client_data
    name = getattr(restaurant, "name", None) or f"restaurant {restaurant_id}"
    try:
        data = get_client_data(restaurant_id) or {}
    except Exception:
        data = {}
    csv_text = (data.get("shifts_csv") or "").strip()
    if not data:
        detail = "it has no client data row at all"
    elif not csv_text:
        detail = "its client data row has no shifts CSV"
    else:
        detail = f"its shifts CSV is {len(csv_text.splitlines()) - 1} rows but could not be read"
    return (f"{name} (id {restaurant_id}) has no shift data to schedule from — {detail}. "
            "Upload a shifts CSV under Update shifts CSV. The figures already on this "
            "page can come from sample data, so a populated Labor tab does not mean "
            "this restaurant has its own shifts on file.")


def _build_schedule_result(restaurant_id):
    """Shared logic for both schedule endpoints."""
    from labor import (analyse_shifts_for_restaurant, load_shifts_for_restaurant,
                       generate_optimized_schedule, get_hourly_rate,
                       build_demand_forecast)
    from models import get_restaurant, get_staff_notes, get_yoy_schedule_context
    from datetime import datetime as _dt, timedelta as _td

    restaurant = get_restaurant(restaurant_id)
    shifts = load_shifts_for_restaurant(restaurant_id)
    if not shifts:
        raise ValueError(_no_shift_data_message(restaurant_id, restaurant))
    analysis = analyse_shifts_for_restaurant(restaurant_id)
    # The guard above can never fire: load_shifts_for_restaurant substitutes
    # a bundled fictional week when a restaurant has uploaded nothing, so
    # `shifts` is always non-empty. That let a brand-new restaurant generate
    # a full week's schedule staffed by eight people who do not exist, with a
    # PAR banner priced off a fictional restaurant's revenue. is_live is the
    # real signal and was already computed; only the two AI paths ignored it.
    if not analysis.get("is_live"):
        raise ValueError(_no_shift_data_message(restaurant_id, restaurant))
    # Use blended rate from per-role rates if available, otherwise flat rate
    rate = analysis.get("blended_rate") or get_hourly_rate(restaurant_id)
    target   = float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0
    owner    = restaurant.owner_name if restaurant else None
    staff_notes = get_staff_notes(restaurant_id) or None

    # Employee availability
    from models import get_staff_availability as _gsa, init_staff_availability as _isa
    try:
        _isa()
        staff_availability = _gsa(restaurant_id) or []
    except Exception:
        staff_availability = []

    # Compute next week dates — the restaurant's week, not the server's
    from time_utils import restaurant_now
    today = restaurant_now(restaurant, naive=True)
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + _td(days=days_ahead)
    next_week_dates = [(monday + _td(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    week_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    # Everything the rules need to know about who can work when, gathered
    # once and shared by the prompt, the backstops, the swap search, the
    # review panel and the publish gate.
    constraints = _rules.build_constraints(restaurant_id, next_week_dates, week_days, restaurant)
    roster_rows = _staff.roster(restaurant_id)
    roster_pairs = [(e["name"], e.get("role") or "") for e in roster_rows]

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
                        hdate = _dt.strptime(m.group(1) + " " + str(today.year), "%b %d %Y")
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
                        edate = _dt.strptime(m.group(1) + " " + str(today.year), "%b %d %Y")
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
            _demand_by_day = {d["day"]: d["vs_average_pct"] for d in _forecast["days"]}
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
    except Exception:
        signals_by_date = {}
    pairs = {}
    try:
        pairs = _staff.pair_sets(restaurant_id)
    except Exception:
        pairs = {}
    reliability = {}
    try:
        reliability = _staff.reliability(restaurant_id)
    except Exception:
        reliability = {}
    learned = []
    try:
        learned = _versions.learned_patterns(restaurant_id)
    except Exception:
        learned = []
    extra_blocks = (_rules.prompt_block(constraints)
                    + _signals.prompt_block(signals_by_date, next_week_dates)
                    + _pairs_block(pairs, roster_pairs)
                    + _reliability_block(reliability)
                    + _versions.prompt_block(learned))

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
    )
    result = _generate_in_parts(analysis, shifts, roster_pairs, _gen_kwargs)
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
    result["prior_published_rows"] = prior_published_rows
    result["weather_forecast"] = weather_forecast or []
    result["pending_time_off"] = {n: sorted(d) for n, d in constraints.pending_off.items()}
    return result


def _generate_in_parts(analysis, shifts, roster_pairs, kwargs):
    """One call for the week; if the response ran out of room, the week is
    written again in parts and merged. A big roster starts in parts."""
    from labor import generate_optimized_schedule
    parts = 1
    if len(roster_pairs or []) > CHUNK_ROSTER_THRESHOLD:
        parts = 2 if len(roster_pairs) <= CHUNK_ROSTER_THRESHOLD * 2 else 3
    if parts == 1:
        result = generate_optimized_schedule(analysis, shifts, **kwargs)
        if not result.get("truncated"):
            result["chunked"] = 1
            return result
        parts = 2
    from datetime import datetime as _d, timedelta as _t
    # Same week the single call would have used (the prompt builder computes
    # it from the restaurant's own clock).
    probe = generate_optimized_schedule.__globals__  # noqa: F841 — keeps a reference for readers
    from time_utils import restaurant_now
    today = restaurant_now(kwargs.get("tz_name"), naive=True)
    days_ahead = (7 - today.weekday()) % 7 or 7
    monday = today + _t(days=days_ahead)
    week_dates = [(monday + _t(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    size = -(-len(week_dates) // parts)
    slices = [week_dates[i:i + size] for i in range(0, len(week_dates), size)]
    merged = None
    rows, narrative, seconds = [], [], 0.0
    for sl in slices:
        part = generate_optimized_schedule(analysis, shifts, week_slice=sl, **kwargs)
        if part.get("truncated"):
            raise ValueError("The week is too long to generate even in parts — trim the roster or split the "
                             "restaurant into departments, then try again.")
        merged = merged or part
        rows.extend(part["schedule_csv"].split("\n")[1:])
        narrative.extend(part.get("narrative") or [])
        seconds += float(part.get("generation_seconds") or 0)
    merged["schedule_csv"] = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(r for r in rows if r.strip())
    merged["narrative"] = narrative[:3]
    merged["summary"] = narrative[:3]
    merged["generation_seconds"] = round(seconds, 1)
    merged["truncated"] = False
    merged["chunked"] = len(slices)
    return merged


def _last_published_csv(restaurant_id, before_date):
    """The newest published week that ended before `before_date`."""
    from models import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL "
            "AND week_end < ? ORDER BY week_end DESC, id DESC LIMIT 1", (restaurant_id, before_date)).fetchone()
    finally:
        conn.close()
    return (row["schedule_csv"] if row else "") or ""


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
    risky = sorted(((n, r) for n, r in (reliability or {}).items()
                    if float(r.get("no_show_rate") or 0) >= 0.2), key=lambda kv: -kv[1]["no_show_rate"])
    if not risky:
        return ""
    lines = [f"  {n}: missed {int(round(r['no_show_rate'] * 100))}% of {r['shifts']} clocked shifts" for n, r in risky[:8]]
    return ("\n\nATTENDANCE (from clock-ins): the people below miss shifts often. Do not leave any of them alone in "
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
    role = (row.get("role") or "").strip()
    ceiling = close_minutes + role_buffers.get(role, 0)

    end_minutes = _parse_time_to_minutes(row.get("shift_end", ""))
    if end_minutes is None or end_minutes <= ceiling:
        return

    start_minutes = _parse_time_to_minutes(row.get("shift_start", ""))
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
                       close_times: dict, role_buffers: dict, constraints=None) -> tuple:
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
    for a in _avail_rows:
        name = a.get("employee_name")
        if not name:
            continue
        try:
            unavailable_by_emp[name] = set(_json_avail.loads(a.get("unavailable_days") or "[]"))
        except Exception:
            unavailable_by_emp[name] = set()
        try:
            avail = _json_avail.loads(a.get("available_days") or "[]")
            if avail:
                available_by_emp[name] = set(avail)
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
        role_headcount_by_date[(date, role)] = role_headcount_by_date.get((date, role), 0) + 1
        if r.get("shift_start") and r.get("shift_end"):
            date_role_template.setdefault((date, role), (r["shift_start"], r["shift_end"]))
            role_time_template.setdefault(role, (r["shift_start"], r["shift_end"]))

    # Average headcount per role across the days it appears — used to spot
    # a thin day for a role that's normally better staffed, the same lens
    # the PAR prompt instruction already asks the model to apply, just
    # applied mechanically here instead of trusted to prose compliance.
    role_day_counts: dict = {}
    for (d, role), n in role_headcount_by_date.items():
        role_day_counts.setdefault(role, []).append(n)
    role_avg_headcount = {role: sum(v) / len(v) for role, v in role_day_counts.items() if v}

    daily_target_hours = dict(daily_target_hours)  # local copy — we mutate to drop exhausted dates
    hours_added = 0.0
    added_dates: dict = {}
    remaining_gap = total_gap
    MAX_ADDS = 100  # hard safety ceiling regardless of gap size
    # Which (date, role) pairs are actually thin: at least one whole person
    # under that role's average headcount across the week. Nothing else is
    # a reason to add a shift.
    thin_dates = {d for (d, role), n in role_headcount_by_date.items()
                  if role_avg_headcount.get(role, 0) - n >= 1}
    thin_dates |= {d for d in daily_target_hours for role in role_avg_headcount
                   if (d, role) not in role_headcount_by_date and role_avg_headcount[role] >= 1}
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
        candidates_role = None
        best_shortfall = 0.0
        for role, avg_hc in role_avg_headcount.items():
            today_hc = role_headcount_by_date.get((target_date, role), 0)
            shortfall = avg_hc - today_hc
            if shortfall > best_shortfall:
                pool = role_employees.get(role, set()) - working_on_date.get(target_date, set())
                pool = {e for e in pool
                        if day_name not in unavailable_by_emp.get(e, set())
                        and (e not in available_by_emp or day_name in available_by_emp[e])
                        and e not in notes_restricted
                        # Approved time off, daypart windows, a deactivated
                        # name: the same question the swap search asks.
                        and target_date not in (_blocked_dates.get(e.lower()) or {})
                        and (constraints is None or constraints.can_work(e, target_date, role_time_template.get(role) and _rules.daypart_of(role_time_template[role][0]))[0])
                        # "No employee over 40h for the week" was prompt text
                        # with nothing enforcing it, so this pass could push
                        # someone into overtime to consume an hours budget —
                        # and the cost model priced those hours straight.
                        and hours_by_employee.get(e, 0.0) + (sum((constraints.base_hours.get(e.lower()) or {}).values()) if constraints is not None else 0.0)
                            < (constraints.max_hours(e) if constraints is not None else _WEEKLY_HOURS_CEILING)}
                if pool:
                    best_shortfall = shortfall
                    candidates_role = (role, pool)

        if not candidates_role:
            # No role on this date has a real, available candidate — can't
            # responsibly add here. Drop the date and move to the
            # next-neediest one instead of forcing a bad pick.
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue

        role, pool = candidates_role
        employee = min(pool, key=lambda e: hours_by_employee.get(e, 0.0))
        start, end = date_role_template.get((target_date, role)) or role_time_template.get(role, ("11:00am", "5:00pm"))

        new_row = {
            "date": target_date, "day": day_name, "employee": employee, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": "0",
            "notes": "added — PAR hours top-up",
        }
        _enforce_close_time(new_row, day_name, close_times, role_buffers)
        if new_row.get("needs_review"):
            # Template (borrowed from another day) doesn't produce a sane
            # shift once capped to this day's close time — skip rather
            # than fabricate a number, same discipline _enforce_close_time
            # itself follows.
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        s_min, e_min = _parse_time_to_minutes(new_row["shift_start"]), _parse_time_to_minutes(new_row["shift_end"])
        if s_min is None or e_min is None or e_min <= s_min:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        new_row["scheduled_hours"] = str(round((e_min - s_min) / 60, 1))
        hrs = float(new_row["scheduled_hours"])
        if hrs <= 0:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        _cap = constraints.max_hours(employee) if constraints is not None else _WEEKLY_HOURS_CEILING
        _base = sum((constraints.base_hours.get(employee.lower()) or {}).values()) if constraints is not None else 0.0
        if hours_by_employee.get(employee, 0.0) + _base + hrs > _cap:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        if constraints is not None and not constraints.rest_ok(
                employee, new_row, [r for r in preview_rows if (r.get("employee") or "") == employee])[0]:
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        if hrs > remaining_gap + 0.01:
            # Would cross the ceiling; the ceiling wins.
            daily_target_hours[target_date] = by_date_hours.get(target_date, 0.0)
            continue
        new_row["notes"] = "added — coverage top-up"

        preview_rows.append(new_row)
        hours_added += hrs
        remaining_gap -= hrs
        added_dates[target_date] = added_dates.get(target_date, 0) + 1
        thin_dates.discard(target_date) if role_headcount_by_date.get((target_date, role), 0) + 1 >= role_avg_headcount.get(role, 0) else None

        by_date_hours[target_date] = by_date_hours.get(target_date, 0.0) + hrs
        working_on_date.setdefault(target_date, set()).add(employee)
        hours_by_employee[employee] = hours_by_employee.get(employee, 0.0) + hrs
        role_headcount_by_date[(target_date, role)] = role_headcount_by_date.get((target_date, role), 0) + 1

    return preview_rows, round(hours_added, 1), added_dates


def _extend_shifts_to_close_gap(preview_rows: list, daily_target_hours: dict, hours_budget: float,
                                 hours_scheduled: float, restaurant_id: int,
                                 close_times: dict, role_buffers: dict) -> tuple:
    """Second-line deterministic top-up, run after _top_up_hours_gap. That
    pass stops adding to a day once every role on it has run out of a real,
    available, not-already-scheduled candidate — which is correct (it
    should never invent a person), but it means some days still end up
    under target purely because the roster is thin, not because the day
    doesn't need the hours. This closes more of that gap the only way left
    that doesn't compromise on "never invent a person": push an existing
    closer's shift_end a bit later, up to that day's own close-time
    ceiling — the exact same ceiling _enforce_close_time already caps
    shift_end at, just applied as a bounded increase instead of a decrease.

    Only ever extends one of that day's later finishers for its role (a
    closer staying a bit longer is plausible; turning a lunch-only opener
    into a closer isn't), and caps each row's extension at 2h so no single
    shift balloons into something unrealistic. Never invents a new row —
    every hour added here belongs to someone already scheduled that day.

    Returns (preview_rows, hours_added, extended_dates) where
    extended_dates is {date: rows_extended_count}.
    """
    total_gap = hours_budget - hours_scheduled
    if not daily_target_hours or total_gap <= 0:
        return preview_rows, 0.0, {}

    import datetime as _dt_ext
    from models import get_staff_availability as _gsa_ext
    MAX_EXTENSION_MINUTES = 120

    # Same conservative rule as the other passes: staff_availability's
    # structured fields can't express "only mornings"/"no closes" — that
    # only ever lives in freeform notes, which this code can't reliably
    # parse. A shift extension is a smaller violation than scheduling a
    # brand-new one, but still real (pushing someone's shift 2h later
    # could turn a stated "mornings only" into an afternoon), so anyone
    # with any notes at all is left alone here too.
    notes_restricted_ext: set = {
        a.get("employee_name") for a in (_gsa_ext(restaurant_id) or [])
        if a.get("employee_name") and (a.get("notes") or "").strip()
    }

    by_date_hours: dict = {}
    rows_by_date: dict = {}
    week_hours_by_emp: dict = {}
    for r in preview_rows:
        d = r.get("date")
        if not d:
            continue
        try:
            hrs = float(r.get("scheduled_hours") or 0)
        except (ValueError, TypeError):
            hrs = 0.0
        by_date_hours[d] = by_date_hours.get(d, 0.0) + hrs
        rows_by_date.setdefault(d, []).append(r)
        emp = r.get("employee")
        if emp:
            week_hours_by_emp[emp] = week_hours_by_emp.get(emp, 0.0) + hrs

    hours_added = 0.0
    extended_dates: dict = {}
    remaining_gap = total_gap

    dates_by_need = sorted(
        (d for d in daily_target_hours if daily_target_hours[d] - by_date_hours.get(d, 0.0) > 0),
        key=lambda d: daily_target_hours[d] - by_date_hours.get(d, 0.0),
        reverse=True,
    )

    for target_date in dates_by_need:
        if remaining_gap <= 0:
            break
        day_gap = daily_target_hours[target_date] - by_date_hours.get(target_date, 0.0)
        if day_gap <= 0:
            continue
        try:
            day_name = _dt_ext.datetime.strptime(target_date, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue

        # Latest finishers first — the realistic "closer stays a little
        # longer" candidates, not openers or lunch-only shifts.
        day_rows_sorted = sorted(
            rows_by_date.get(target_date, []),
            key=lambda r: _parse_time_to_minutes(r.get("shift_end", "")) or -1,
            reverse=True,
        )

        for row in day_rows_sorted:
            if day_gap <= 0 or remaining_gap <= 0:
                break
            if row.get("employee") in notes_restricted_ext:
                continue
            # Never extend somebody into overtime to consume an hours
            # budget. The prompt's "no employee over 40h" rule had no
            # enforcement, and the cost model priced overtime straight.
            if week_hours_by_emp.get(row.get("employee"), 0.0) >= _WEEKLY_HOURS_CEILING:
                continue
            end_min = _parse_time_to_minutes(row.get("shift_end", ""))
            start_min = _parse_time_to_minutes(row.get("shift_start", ""))
            if end_min is None or start_min is None or end_min <= start_min:
                continue

            _emp_room = _WEEKLY_HOURS_CEILING - week_hours_by_emp.get(row.get("employee"), 0.0)
            extend_by = min(MAX_EXTENSION_MINUTES, int(day_gap * 60), int(max(0.0, _emp_room) * 60))
            if extend_by <= 0:
                continue
            original_end = row["shift_end"]
            row["shift_end"] = _format_minutes_to_time(end_min + extend_by)
            _enforce_close_time(row, day_name, close_times, role_buffers)  # clamps back down if past close
            new_end_min = _parse_time_to_minutes(row["shift_end"])
            if new_end_min is None or new_end_min <= end_min:
                row["shift_end"] = original_end  # at/past the close-time ceiling already — nothing gained
                continue

            added_hours = round((new_end_min - end_min) / 60, 1)
            if added_hours <= 0:
                row["shift_end"] = original_end
                continue
            row["scheduled_hours"] = str(round((new_end_min - start_min) / 60, 1))
            note = (row.get("notes") or "").strip()
            row["notes"] = f"{note} (extended — PAR hours top-up)" if note else "extended — PAR hours top-up"

            hours_added += added_hours
            remaining_gap -= added_hours
            day_gap -= added_hours
            by_date_hours[target_date] = by_date_hours.get(target_date, 0.0) + added_hours
            if row.get("employee"):
                week_hours_by_emp[row["employee"]] = week_hours_by_emp.get(row["employee"], 0.0) + added_hours
            extended_dates[target_date] = extended_dates.get(target_date, 0) + 1

    return preview_rows, round(hours_added, 1), extended_dates


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
                              max_overlap: int = None) -> tuple:
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
    """
    _cap = int(max_overlap) if max_overlap and int(max_overlap) > 0 else _SERVER_MAX_OVERLAP

    by_date: dict = {}
    for r in preview_rows:
        if (r.get("role") or "").strip().lower() == "server":
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

            candidate = min(active, key=_priority)
            start_min = _parse_time_to_minutes(candidate.get("shift_start", ""))
            if start_min is None:
                break
            new_end = peak_time
            if new_end - start_min < 30:
                day_rows.remove(candidate)
                preview_rows.remove(candidate)
            else:
                candidate["shift_end"] = _format_minutes_to_time(new_end)
                if day_name:
                    _enforce_close_time(candidate, day_name, close_times, role_buffers)
                final_end = _parse_time_to_minutes(candidate["shift_end"])
                candidate["scheduled_hours"] = str(round((final_end - start_min) / 60, 1))
                note = (candidate.get("notes") or "").strip()
                candidate["notes"] = f"{note} (trimmed — over the {_cap}-server cap)" if note else f"trimmed — over the {_cap}-server cap"

            rows_trimmed += 1
            trimmed_dates[date] = trimmed_dates.get(date, 0) + 1

    return preview_rows, rows_trimmed, trimmed_dates


def _window_overlap(row: dict, window: tuple) -> bool:
    s_ = _parse_time_to_minutes(row.get("shift_start", ""))
    e_ = _parse_time_to_minutes(row.get("shift_end", ""))
    return s_ is not None and e_ is not None and s_ < window[1] and e_ > window[0]


_MORNING_WINDOW = (_parse_time_to_minutes("11:00am"), _parse_time_to_minutes("2:30pm"))
_NIGHT_WINDOW = (_parse_time_to_minutes("5:30pm"), _parse_time_to_minutes("8:30pm"))


def _ensure_role_floors(preview_rows: list, week_dates: list, week_days: list, restaurant_id: int,
                        close_times: dict, role_buffers: dict, floors: dict = None,
                        constraints=None) -> tuple:
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

    Returns (preview_rows, rows_added, added_dates).
    """
    floors = floors if floors is not None else {}
    if not floors:
        return preview_rows, 0, {}
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
    for date, day_name in zip(week_dates, week_days):
        for role_name, spec in floors.items():
            key = role_name.strip().lower()
            for part, window in (("morning", _MORNING_WINDOW), ("night", _NIGHT_WINDOW)):
                need = _rules.floor_for(floors, role_name, day_name, part)
                if not need:
                    continue
                current = [r for r in preview_rows if r.get("date") == date
                           and (r.get("role") or "").strip().lower() == key and _window_overlap(r, window)]
                for _ in range(max(0, need - len(current))):
                    pool = set(role_people.get(key, set())) - working_on_date.get(date, set())
                    pool = {e for e in pool if e not in notes_restricted and constraints.can_work(e, date, part)[0]}
                    if not pool:
                        break
                    employee = min(pool, key=lambda e: hours_by_employee.get(e, 0.0))
                    start, end = templates.get((key, part)) or _daypart_fallback(constraints, day_name, part)
                    new_row = {"date": date, "day": day_name, "employee": employee, "role": role_name,
                               "shift_start": start, "shift_end": end, "scheduled_hours": "0",
                               "notes": f"added — {role_name} floor"}
                    _enforce_close_time(new_row, day_name, close_times, role_buffers)
                    s_min = _parse_time_to_minutes(new_row["shift_start"])
                    e_min = _parse_time_to_minutes(new_row["shift_end"])
                    if new_row.get("needs_review") or s_min is None or e_min is None or e_min <= s_min:
                        break
                    hrs = round((e_min - s_min) / 60, 1)
                    base = sum((constraints.base_hours.get(employee.lower()) or {}).values())
                    if hours_by_employee.get(employee, 0.0) + base + hrs > constraints.max_hours(employee):
                        role_people[key].discard(employee)
                        continue
                    ok, _why = constraints.rest_ok(employee, new_row, rows_by_person.get(employee.lower(), []))
                    if not ok:
                        role_people[key].discard(employee)
                        continue
                    new_row["scheduled_hours"] = str(hrs)
                    preview_rows.append(new_row)
                    rows_by_person.setdefault(employee.lower(), []).append(new_row)
                    working_on_date.setdefault(date, set()).add(employee)
                    hours_by_employee[employee] = hours_by_employee.get(employee, 0.0) + hrs
                    rows_added += 1
                    added_dates[date] = added_dates.get(date, 0) + 1
    return preview_rows, rows_added, added_dates


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
            demand_by_day = {d["day"]: d["vs_average_pct"] for d in forecast["days"]}
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
            out["prior_week_assignments"] = _prior_week_assignments(restaurant_id, before=dates[0])
            out["pending_time_off"] = {n: sorted(d) for n, d in c.pending_off.items()}
    except Exception as _cx:
        print(f"[schedule] live constraints unavailable: {_cx}")
    return out


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
        if not csv_text:
            # Nothing published before this week: the newest generation is
            # the only tail there is, as long as it is not this same week.
            entries = get_schedule_history(restaurant_id, limit=3)
            for e in entries:
                if before and (e.get("week_start") or "") >= before:
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
    }
    c = result.get("constraints")
    if c is not None:
        signals["rules"] = _rules_for_swaps(c)
        signals["open_times"] = c.open_times or {}
        signals["close_times"] = c.close_times or {}
        signals["role_floors"] = c.role_floors or {}
        signals["max_shift_hours"] = c.compliance.get("max_shift_hours")
        signals["weekly_ceiling"] = c.compliance.get("weekly_hours_ceiling")
        signals["hours_limits"] = {n: v for n, v in (c.hours_limits or {}).items()}
        signals["hours_limits"] = {}
        for n in (result.get("roster") or []):
            lim = c.hours_limits.get(n.lower())
            if lim:
                signals["hours_limits"][n] = lim
    try:
        signals["demand_curve"] = _hourly_profile(restaurant_id)
    except Exception:
        signals["demand_curve"] = {}
    # A rule nobody on the roster could satisfy is not the draft's fault,
    # and confidence should say so rather than the score silently failing.
    try:
        scores = signals["scores"]
        unsat = 0
        for rule in signals["leader_rules"]:
            ms = rule.get("min_score")
            if ms is None or rule.get("attribute"):
                continue
            role = (rule.get("role") or "").strip().lower()
            roles = result.get("roster_roles") or {}
            able = [n for n, sc in scores.items() if sc is not None and float(sc) >= float(ms)
                    and (not roles or (roles.get(n) or "").strip().lower() == role)]
            if len(able) < int(rule.get("count") or 1):
                unsat += 1
        signals["unsatisfiable"] = unsat
    except Exception:
        pass
    # Each of these is a separate read and any one of them can be empty for
    # a new restaurant. A failure to load one must cost that dimension, not
    # the whole evaluation — which is exactly what returning {} does, since
    # a dimension with no data withdraws instead of scoring zero.
    for key, fn in (("tenure", get_employee_tenure), ("leader_flags", get_leader_flags),
                    ("prior_pattern", get_prior_shift_pattern),
                    ("availability", get_unavailability_map)):
        try:
            signals[key] = fn(restaurant_id) or {}
        except Exception:
            signals[key] = {}
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
    return quality, what_if


def _sched_notes_with_findings(restaurant_id, sched_notes):
    """The owner's schedule notes plus what the cross-module engine found
    about staffing. "Friday dinner is one server short and it shows in the
    reviews" (business_intelligence reviews_x_labor) used to change nothing
    unless the owner read it and edited the draft; now the draft reads it.
    Only links that cleared their own floors exist, so this adds nothing
    when there is nothing to add."""
    try:
        import business_intelligence as bi
        links = [l for l in (bi.executive_brief(restaurant_id).get("links") or [])
                 if l.get("kind") == "reviews_x_labor" and l.get("headline")]
    except Exception:
        links = []
    if not links:
        return sched_notes
    lines = ["Cavnar AI finding (reviews x labor) — reflect this in the draft and say so in the summary: "
             + l["headline"] + (" Confirm by: " + l["confirm_by"] if l.get("confirm_by") else "")
             for l in links[:2]]
    return ((sched_notes or "").strip() + "\n" + "\n".join(lines)).strip()


def _run_schedule_job(job_id, restaurant_id):
    import csv as _csv_mod, traceback as _tb, datetime as _dt_sched
    try:
        result = _build_schedule_result(restaurant_id)
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
                try:
                    import ops
                    ops.capture(RuntimeError(
                        f"{_hours_drift_rows} schedule rows had scheduled_hours that disagreed "
                        f"with their shift times ({round(_hours_drift_total, 1)}h total)"),
                        job="labor_schedule", context=f"restaurant_id={restaurant_id}")
                except Exception:
                    pass
            print(f"[schedule] parsed {len(preview_rows)} rows, first={preview_rows[0] if preview_rows else None}")

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

            preview_rows, pizza_rows_added, pizza_added_dates = _ensure_role_floors(
                preview_rows, result.get("week_dates", []), result.get("week_days", []),
                restaurant_id, _close_times, _role_close_buffers,
                floors=_constraints.role_floors, constraints=_constraints,
            )
            if pizza_rows_added:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] role floors added {pizza_rows_added} row(s) across {pizza_added_dates}")

            preview_rows, hours_added, added_dates = _top_up_hours_gap(
                preview_rows, result.get("daily_target_hours", {}),
                result.get("hours_budget", 0), hours_scheduled, restaurant_id,
                _close_times, _role_close_buffers, constraints=_constraints,
            )
            if hours_added:
                hours_scheduled = round(hours_scheduled + hours_added, 1)
                print(f"[schedule] coverage top-up added {hours_added}h across {added_dates}")
            # _extend_shifts_to_close_gap is deliberately no longer run: it
            # pushed closers later to consume an hours budget the prompt
            # calls a ceiling. Kept for a caller that wants it explicitly.

            preview_rows, rows_trimmed, trimmed_dates = _trim_server_overlap_cap(
                preview_rows, _close_times, _role_close_buffers,
                max_overlap=getattr(_restaurant_for_sched, 'section_count', None),
            )
            if rows_trimmed:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] trimmed {rows_trimmed} row(s) over the server cap across {trimmed_dates}")

            # Every rule the week is checked against, in one sweep
            # (schedule_rules.violations): the roster, the week, double
            # bookings, approved time off, availability by day and daypart,
            # the hours ceiling on the PAYROLL week including what is
            # already published, shift length, rest between shifts, minors,
            # days off, pending time off. Hard breaches that a legal
            # replacement can fix are fixed and tagged; the rest are flagged
            # for the owner and never counted as coverage.
            _viols = _rules.violations(preview_rows, _constraints)
            _fixes, _unfixed = [], []
            _hard = [v for v in _viols if v["hard"]]
            if _hard:
                try:
                    _sig, _w = _quality_signals(restaurant_id, result)
                    _profiles_for_fix = result.get("shift_profiles") or None
                    import shift_quality as _sqf
                    _out = _sqf.apply_fixes(preview_rows, _hard, profiles=_profiles_for_fix, weights=_w, **_sig)
                    if _out.get("fixes"):
                        preview_rows = _out["rows"]
                        _fixes = _out["fixes"]
                        hours_scheduled = _safe_hours_sum(preview_rows)
                        _viols = _rules.violations(preview_rows, _constraints)
                    _unfixed = _out.get("unfixed") or []
                except Exception as _fx:
                    print(f"[schedule] fix pass failed: {_fx}")
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
            # The budget is a ceiling. A week written past it is named at
            # the top of the review, never trimmed into a thinner week.
            try:
                _hs = _safe_hours_sum(preview_rows)
                _hb = float(result.get("hours_budget") or 0)
            except (TypeError, ValueError):
                _hs = _hb = 0.0
            if _hb > 0 and _hs > _hb * 1.02:
                result["review"]["over_budget_hours"] = round(_hs - _hb, 1)
                result["review"]["lines"].insert(
                    0, f"⚠ {_hs:,.0f}h scheduled against a {_hb:,.0f}h budget — {_hs - _hb:,.0f}h over the ceiling")
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
        except Exception as _csv_ex:
            print(f"[schedule] csv parse error: {_csv_ex}")
            pass

        _history_id = None
        try:
            from models import save_schedule_history
            _wd = result.get("week_dates", [])
            _history_id = save_schedule_history(
                restaurant_id, _wd[0] if _wd else None, _wd[-1] if _wd else None,
                round(hours_scheduled, 1), result.get("hours_budget", 0), result.get("labor_target", 30),
                result["schedule_csv"], result.get("summary", []),
                quality=result.get("quality"),
            )
            _annotate_history(_history_id, restaurant_id, review=result.get("review"),
                              seconds=result.get("generation_seconds"),
                              weather=result.get("weather_forecast"))
            try:
                _versions.append(restaurant_id, _history_id, "generated", result["schedule_csv"],
                                 quality=result.get("quality"), saved_by="Cavnar AI")
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
            _ops.capture(RuntimeError(f"{len(_dropped_rows)} malformed schedule row(s) dropped"),
                         job="schedule_generate",
                         context=f"restaurant_id={restaurant_id} · examples={_dropped_rows[:3]}")

        _ops.finish_async_job(job_id, "done", dict(
            ok=True,
            history_id=_history_id,
            # Every rule breach, hard and soft, with the person and the
            # date; what the fix pass repaired; what it could not.
            rule_violations=result.get("rule_violations") or [],
            review=result.get("review") or {"hard": 0, "soft": 0, "lines": [], "fixes": [], "unfixed": []},
            pending_time_off=result.get("pending_time_off") or {},
            narrative=result.get("narrative") or [],
            generation_seconds=result.get("generation_seconds"),
            chunked=result.get("chunked") or 1,
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
        ))
    except Exception as e:
        tb = _tb.format_exc()
        print(f"[schedule job] FAILED:\n{tb}")
        _ops.finish_async_job(job_id, "error", {"ok": False, "error": str(e), "traceback": tb})


def _annotate_history(history_id, restaurant_id, review=None, seconds=None, weather=None):
    """The columns save_schedule_history predates: the review verdict, how
    long generation took, and the forecast it was written against."""
    if not history_id:
        return
    from models import get_conn, _ensure_history_columns
    conn = get_conn()
    try:
        _ensure_history_columns(conn)
        conn.execute("UPDATE schedule_history SET review_json=?, generation_seconds=?, weather_json=? "
                     "WHERE id=? AND restaurant_id=?",
                     (json.dumps(review) if review else None, seconds,
                      json.dumps(weather) if weather else None, history_id, restaurant_id))
        conn.commit()
    finally:
        conn.close()


def _rows_to_csv_text(rows: list) -> str:
    cols = ["date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes"]
    lines = [",".join(cols)]
    for r in rows:
        lines.append(",".join(str(r.get(c, "") or "").replace(",", ";") for c in cols))
    return "\n".join(lines)


def replacement_is_legal(restaurant_id, rows: list, index: int, name: str):
    """(ok, reason): could `name` take rows[index] outright, by every rule
    the generation itself is checked against? One answer for the web and
    iOS replacement pickers, the open-shift claim and the fix pass."""
    import shift_quality as _sq
    from models import get_unavailability_map, get_staff_notes
    try:
        dates = sorted({r.get("date") for r in rows if r.get("date")})
        week_days = [__import__("datetime").datetime.strptime(d, "%Y-%m-%d").strftime("%A") for d in dates]
        c = _rules.build_constraints(restaurant_id, dates, week_days)
        ok, why = c.can_work(name, rows[index].get("date", ""), _rules.daypart_of(rows[index].get("shift_start", "")))
        if not ok:
            return False, why
        availability = get_unavailability_map(restaurant_id)
        constraints = {n["employee_name"]: n["notes"] for n in (get_staff_notes(restaurant_id) or []) if n.get("employee_name")}
        idx = _sq._SwapIndex(rows, availability, constraints, _rules_for_swaps(c))
        if not idx.replacement_legal(index, name):
            return False, "would break a rule (hours, rest, a note on file, or already working that day)"
        return True, ""
    except Exception as e:
        return False, f"could not check: {e}"


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
    }


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
