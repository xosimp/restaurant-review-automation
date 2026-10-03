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
CHUNK_ROSTER_THRESHOLD = 80     # kept for callers; the decision is now by expected rows
CHUNK_ROWS_PER_CALL = 160       # ~60 output tokens a row against a 16k ceiling, with room for the summary


class ScheduleGenerationError(ValueError):
    """A generation that must not be saved, with a sentence the owner can
    read. Anything else that fails a job is logged and shown as a plain
    "try again" — never its exception text or a traceback (DATA-46)."""


def _labor_ot_line() -> float:
    """The hours past which overtime pay starts (labor.OVERTIME_THRESHOLD_HOURS)."""
    from labor import OVERTIME_THRESHOLD_HOURS
    return float(OVERTIME_THRESHOLD_HOURS)


def _expected_rows(shifts, roster_pairs) -> int:
    """How many shift rows a week here usually has: the busiest of the last
    four full weeks in the history, else three and a half a head."""
    try:
        by_week = {}
        for sh in shifts or []:
            d = (sh.get("date") or "")[:10]
            if len(d) == 10:
                from datetime import datetime as _d
                dt = _d.strptime(d, "%Y-%m-%d")
                by_week[(dt.isocalendar()[0], dt.isocalendar()[1])] = by_week.get((dt.isocalendar()[0], dt.isocalendar()[1]), 0) + 1
        weeks = sorted(by_week.items())[-4:]
        if weeks:
            return max(n for _, n in weeks)
    except Exception:
        pass
    return int(len(roster_pairs or []) * 3.5)


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
    closed = _rules.closures(restaurant)
    closed_here = _rules.closed_in(restaurant, dates)
    # The same plan inputs the draft is given (_build_schedule_result): the
    # salaried staff's share of the week comes off the all-in target (D-1),
    # a closed date takes no hours (D-24), and the wage is the measured one
    # with what of it is assumed said (D-2, E-24). Salaries are the owner's:
    # anyone else sees the hourly budget without their dollars.
    try:
        from models import salaried_week_share, viewer_sees_salaries
        salaried = salaried_week_share(restaurant, dates, closed_here)
        show_salary = viewer_sees_salaries()
    except Exception as e:
        _soft_fail("forecast_preview salaries", e, restaurant_id)
        salaried, show_salary = None, False
    # Each date's one demand number (the owner's budget for the night, the
    # dated facts, measured effects, measured rain on the forecast already
    # on file) — the draft's own, so a day's hours here are the day's hours
    # there (D-24).
    date_demand = {}
    try:
        try:
            import weather as _wx
            _wx_rows = _wx.cached_forecast_for_week(restaurant, dates)
        except Exception:
            _wx_rows = []
        _h, date_demand = _merge_date_demand(restaurant_id, dates, _signals.by_date(restaurant_id, dates),
                                             weather=_wx_rows, closed=closed_here)
    except Exception as e:
        _soft_fail("forecast_preview date demand", e, restaurant_id)
        date_demand = {}
    plan = week_hours_plan(analysis, dates, target, rate,
                           yoy_context=get_yoy_schedule_context(restaurant_id, dates, today=restaurant_now(restaurant).date()),
                           projected_revenue_override=revenue.get("value"), monthly_revenue_target=monthly,
                           salaried=salaried, closed_dates=closed_here, date_demand=date_demand,
                           rate_basis=analysis.get("rate_basis"), show_salary=show_salary)
    days = []
    for d in dates:
        wd = _date_of(d).strftime("%A")
        row = {"date": d, "weekday": wd, "hours": plan["daily_target_hours"].get(d),
               "closed": d in closed["closed_dates"] or wd in closed["closed_weekdays"]}
        dd = date_demand.get(d) or {}
        if dd and not dd.get("closed"):
            # How far the date sits from a typical one, and why (D-23, D-30).
            row["demand"] = {"pct": dd.get("pct"), "reasons": list(dd.get("reasons") or []),
                             "projected_sales": dd.get("projected_sales"), "typical_sales": dd.get("typical_sales")}
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
           "hourly_rate": round(float(rate or 0), 2),
           # What the budget is (all-in target less the salaried share, or
           # the whole target), the wage's basis and its caveat, and why a
           # day's target moved (D-1, D-2, E-24, D-24).
           "budget_basis": plan.get("budget_basis"), "daily_target_basis": plan.get("daily_target_basis"),
           "daily_target_reasons": plan.get("daily_target_reasons") or {}}
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
        detail = f"its shifts CSV is {max(0, len([ln for ln in csv_text.splitlines() if ln.strip()]) - 1)} rows but could not be read"
    return (f"{name} (id {restaurant_id}) has no shift data to schedule from — {detail}. "
            "Upload a shifts CSV under Update shifts CSV. The figures already on this "
            "page can come from sample data, so a populated Labor tab does not mean "
            "this restaurant has its own shifts on file.")


def _prompt_experienced(marked, tenure, constraints, roster_pairs) -> list:
    """The names the prompt lists as experienced (schedule_requirements.
    experience_block): the owner's marks and — where experience is judged
    at all, by the scorer's own rule (shift_quality.experience_judged) —
    the managers, anyone standing in as one and the salaried people on the
    roster (schedule audit 10/3/26 D-6). Tenure is the punch count, so an
    owner who never clocks in (1 shift on file) was listed as "still
    developing — put each of them on with an experienced hand, never two of
    them together". A history too short to judge anybody stays unclaimed,
    as the scorer leaves it."""
    import shift_quality as _sq
    marked = sorted({n for n in (marked or []) if n})
    if constraints is None or not _sq.experience_judged(tenure, marked):
        return marked
    defaults = _sq.experienced_by_default(constraints.managers, constraints.acting_managers, constraints.salaried)
    return sorted(set(marked) | {n for n, _r in (roster_pairs or []) if _sq.name_key(n) in defaults})


def _quality_tuning(restaurant_id) -> dict:
    """The calibration the owner applied to the built-in profiles
    (models.get_quality_tuning, SQ-22), or {} — a failed read is said."""
    try:
        from models import get_quality_tuning
        return get_quality_tuning(restaurant_id) or {}
    except Exception as _tx:
        _soft_fail("quality tuning", _tx, restaurant_id)
        return {}


def _soft_fail(what, exc, restaurant_id):
    """An input the draft can do without failed to load: the draft still
    goes ahead, but the failure is said — a silent {} made "keep these two
    apart" vanish from both the prompt and the score with nobody told."""
    print(f"[schedule] {what} unavailable for restaurant {restaurant_id}: {exc}")
    try:
        _ops.capture(exc, job="schedule_inputs", context=f"restaurant_id={restaurant_id} input={what}")
    except Exception as _cx:
        print(f"[schedule] could not record that failure: {_cx}")


def _build_schedule_result(restaurant_id, week_start=None, focus=None, dates=None, prior_rows=None,
                           instruction=None):
    """Shared logic for both schedule endpoints.

    focus — named weaknesses of the previous draft (a list of strings), for
    a regeneration of chosen dates; rendered into the prompt as "THE
    PREVIOUS DRAFT OF THESE DAYS SCORED WEAK ON" (schedule_requirements
    .focus_block). None for an ordinary generation.
    dates, prior_rows — a redo of some days: the dates being rewritten and
    the rows of the days the owner keeps. The manager plan covers only
    those dates, with the kept rows' hours, rest and runs counted.
    instruction — what the owner asked for with this draft (Ask Cavnar's
    generate_schedule, the generate route's `instruction`), handed to every
    model call of the generation (schedule audit 10/3/26 PR-19)."""
    from labor import (analyse_shifts_for_restaurant, load_shifts_for_restaurant,
                       generate_optimized_schedule, get_hourly_rate,
                       build_demand_forecast)
    from models import get_restaurant, get_staff_notes, get_yoy_schedule_context
    from datetime import datetime as _dt, timedelta as _td
    import uuid as _uuid_gen
    _generation_id = _uuid_gen.uuid4().hex

    restaurant = get_restaurant(restaurant_id)
    shifts = load_shifts_for_restaurant(restaurant_id)
    if not shifts:
        raise ScheduleGenerationError(_no_shift_data_message(restaurant_id, restaurant))
    # Hourly: the schedule's hours budget is sales x target / the hourly rate.
    analysis = analyse_shifts_for_restaurant(restaurant_id, with_salaries=False)
    # The guard above can never fire: load_shifts_for_restaurant substitutes
    # a bundled fictional week when a restaurant has uploaded nothing, so
    # `shifts` is always non-empty. That let a brand-new restaurant generate
    # a full week's schedule staffed by eight people who do not exist, with a
    # PAR banner priced off a fictional restaurant's revenue. is_live is the
    # real signal and was already computed; only the two AI paths ignored it.
    if not analysis.get("is_live"):
        raise ScheduleGenerationError(_no_shift_data_message(restaurant_id, restaurant))
    # Sales that stopped weeks ago are not a forecast: the demand, the budget
    # and every day's target would read them as current while the forecast
    # quietly fell back. The generation refuses and says why (D-33).
    freshness = _demand_data_through(restaurant_id, restaurant=restaurant)
    if freshness.get("blocked"):
        raise ScheduleGenerationError(freshness["message"])
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
    # The staffing baseline the requirements, the score and the soft-ask
    # check all read (labor.staffing_baseline): the punches less the
    # salaried (D-4), the published weeks for the people who never punch
    # (L-1), the late segment by the close times (D-32).
    staffing = None
    try:
        from labor import staffing_baseline as _baseline
        staffing = _baseline(restaurant_id, shifts=shifts, before=next_week_dates[0],
                             close_times=constraints.close_times or {})
    except Exception as _sfx:
        _soft_fail('staffing_baseline', _sfx, restaurant_id)
        staffing = None

    # Revenue override from restaurant target (takes priority over YoY sum)
    monthly_rev_target = float(getattr(restaurant, 'monthly_revenue_target', 0) or 0)

    # The salaried staff's share of this week: the target counts salaries,
    # so the hourly hours budget is the target's dollars less it (schedule
    # audit 10/3/26 D-1). Computed in a background job, kept out of the
    # prompt and the stored result (salaries are the owner's alone).
    salaried_week = None
    try:
        from models import salaried_week_share
        salaried_week = salaried_week_share(restaurant, next_week_dates, constraints.closed_dates)
    except Exception as _sfx:
        _soft_fail('salaried_week', _sfx, restaurant_id)
        salaried_week = None

    # YoY context — last year's night for each date: a holiday's own night
    # last year, else the same weekday (never another one), moved by this
    # year's trend; each row says which (models.get_yoy_schedule_context:
    # schedule audit 10/3/26 E-8, D-28, D-29). The holiday flags come from
    # the same calendar the lift is measured on; they were set here from the
    # marketing calendar's 52-week date match, which labelled Christmas Eve
    # "matched to Christmas Eve last year" over last Christmas Day's figures.
    yoy_ctx = get_yoy_schedule_context(restaurant_id, next_week_dates, today=today.date())

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

    # Operational Score, its targets, and any shift leader rules. The score
    # targets judge nobody until somebody is rated. The leader rules load
    # whatever the ratings: a closing rule needs no score at all, and an
    # owner's rule was dropped from the prompt and the score in silence
    # whenever nobody (or only support, through view-as) had rated anyone
    # (schedule audit 10/3/26 P-18, D-10). Whether they can judge anybody
    # is said on the generate screen (leader_rules_status).
    from models import (get_operational_scores, get_role_strength_thresholds,
                        get_shift_leader_rules, get_shift_profiles)
    _op_scores = get_operational_scores(restaurant_id)
    _strength_thresholds = get_role_strength_thresholds(restaurant_id) if _op_scores else {}
    _leader_rules = get_shift_leader_rules(restaurant_id)

    # Shift profiles — what each shift is actually judged on. Demand levels
    # come from its OWN sales rather than from an assumption that every
    # restaurant's Friday is busy.
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
    # Always built, from the restaurant's own profiles or the built-ins,
    # each weekday's demand from its own sales and any calibration the
    # owner applied (schedule audit 10/3/26 SQ-15, SQ-22). They used to be
    # built only once somebody was rated or a profile was saved, so a
    # restaurant with neither was scored on the built-ins' guesses — a
    # Saturday 30% under an average day still "peak" with an 82 bar.
    _profiles = _sq.profiles_from_config(
        [_sq.profile_from_dict(p) for p in _stored_profiles] or None,
        default_strength=_strength_thresholds,
        default_leader_rules=_leader_rules,
        demand_by_day=_demand_by_day,
        tuning=_quality_tuning(restaurant_id),
    )
    # The prompt's profile block stays as it was for a restaurant that has
    # configured none and rated nobody — no claim about its Friday in the
    # model's words (labor.format_profile_block); the requirements table
    # settles every shift's demand through the same re-levelled set either
    # way (shift_quality.profile_set).
    _prompt_profiles = _profiles if (_stored_profiles or _op_scores) else []

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
    holiday, date_demand = {}, {}
    try:
        holiday, date_demand = _merge_date_demand(restaurant_id, next_week_dates, signals_by_date,
                                                  weather=weather_forecast, closed=constraints.closed_dates)
    except Exception as _hx:
        _soft_fail('date_demand', _hx, restaurant_id)
        holiday, date_demand = {}, {}
    # The events block states only a lift this restaurant measured; the
    # prompt used to assert "20-40% higher covers" for any holiday (SCHED-33).
    # Matched by the holiday's own name: the marketing calendar's carries a
    # hint ("Christmas Eve — holiday dining"), so the measured lift never
    # reached those holidays' lines (E-8). Each name reads its own night.
    for _ev in upcoming_events:
        _evn = str(_ev.get("name") or "").split(" — ", 1)[0].strip()
        for _d, _h in holiday.items():
            _one = (_h.get("by_name") or {}).get(_evn) or (_h if _h.get("name") == _evn else None)
            if _one and _one.get("lift_pct") is not None:
                _ev["lift_pct"], _ev["based_on"] = _one["lift_pct"], _one.get("based_on")
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
    learning = schedule_learning_inputs(restaurant_id, restaurant, roster_pairs, shifts, splh, target,
                                        own_typical=(staffing or {}).get("typical_headcount"))
    stated_prefs, learned_prefs = {}, {}
    # Read on their own: the learned slots now bind the scorer and the
    # solver too (L-19), and a failed stated read used to drop them silently.
    try:
        stated_prefs = _staff.stated_preferences(restaurant_id)
    except Exception as _sfx:
        _soft_fail('stated_preferences', _sfx, restaurant_id)
    try:
        learned_prefs = _intel.behaviour_preferences(restaurant_id)
    except Exception as _sfx:
        _soft_fail('learned_preferences', _sfx, restaurant_id)
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
    try:
        _people["experienced"] = _prompt_experienced(_people["experienced"], _people["tenure"], constraints,
                                                     roster_pairs)
    except Exception as _sfx:
        _soft_fail('people["experienced"] defaults', _sfx, restaurant_id)
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
    # The labor standards (labor_standards, D-25): measured guests per
    # server-hour and the like, with the owner's own figure over them; an
    # owner's standard resizes that role's requirement, with its reason.
    standards_for_week = {}
    try:
        import labor_standards as _ls
        standards_for_week = _ls.for_requirements(restaurant_id, next_week_dates,
                                                  (staffing or {}).get("typical_headcount") or {}, date_demand,
                                                  restaurant=restaurant, shifts=shifts)
    except Exception as _sfx:
        _soft_fail('labor_standards', _sfx, restaurant_id)
        standards_for_week = {}
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
    # The managers' shifts, planned before the model writes anything
    # (schedule audit 10/3/26 PR-1, P-8, D-4, D-5, PR-32). Erik's first week
    # had no manager on any day: managers barely punch, so the history the
    # draft copies never asked for one, and the backstop then patched gaps
    # with 4h blocks shaped to them. Each trading day is covered from the
    # first person on to the last one out with real opener and closer
    # shifts — standing shifts first, then availability and every legal
    # limit, the days each usually works (published weeks and punches),
    # then a fair split — handed to the model as fixed rows. A plan that
    # cannot be made costs the plan, never the draft: the backstop
    # (cover_manager_gaps) still runs, and PRIORITIES 1a still states the rule.
    import schedule_skeleton as _skeleton
    try:
        manager_plan = _skeleton.plan_for_generation(
            restaurant_id, constraints, next_week_dates, shifts=shifts,
            roster_roles={n: r for n, r in roster_pairs}, dates=dates, prior_rows=prior_rows)
    except Exception as _sfx:
        _soft_fail('manager_plan', _sfx, restaurant_id)
        manager_plan = _skeleton.failed_plan(constraints, _sfx)
    extra_blocks = (_rules.prompt_block(constraints, manager_plan=manager_plan)
                    + _signals.prompt_block(signals_by_date, next_week_dates)
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
        shift_profiles=_prompt_profiles,
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
        # The manager plan: fixed rows under PRIORITIES 1a, merged into the
        # answer by code (schedule_skeleton).
        pinned_rows=manager_plan.get("rows") or None,
        manager_plan=manager_plan,
        salaried_week=salaried_week,
        # Each date's demand, the sales-per-labor-hour hold, the asks with
        # their reasons and the baseline: the requirement numbers are made
        # here, in code (D-23, P-19, PR-7, L-1, D-32).
        date_demand=date_demand or None,
        splh_hold=(learning.get("splh_objective") or {}).get("hold") or None,
        requirement_adjustments=_asks_as_adjustments(soft_reqs) or None,
        staffing_patterns=staffing,
        labor_standards=standards_for_week or None,
        # The roles each person holds beyond their roster role: the output
        # schema lets their rows carry them (schedule audit 10/3/26 PR-12).
        held_roles={k: sorted(v) for k, v in (getattr(constraints, "held_roles", None) or {}).items()} or None,
        # One id for every model call this generation makes: each call's
        # full input and answer is stored under it and keyed to the saved
        # week (schedule_output.record_call / link_calls, PR-31).
        generation_id=_generation_id,
        instruction=instruction or None,
    )
    result = _generate_in_parts(analysis, shifts, roster_pairs, _gen_kwargs)
    result["generation_id"] = _generation_id
    result["manager_plan"] = manager_plan
    # The scorer, the solver and the optimizer judge every shift against
    # the full set, whatever the prompt was shown (SQ-15).
    result["shift_profiles"] = _profiles
    # What staff showed by what they drop and claim, for the scorer and the
    # solver as well as the prompt (L-19) — read once.
    result["learned_preferences"] = learned_prefs
    result["rotation_plan"] = learning["rotation"]
    result["splh_objective"] = learning["splh_objective"]
    # Each date's demand number and its reasons (D-23, D-24, D-30) — the
    # Studio and the review say why a day moved.
    result["date_demand"] = date_demand
    result["labor_standards"] = (standards_for_week or {}).get("standards") or {}
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
    # How current the sales behind this week are, for the banner and the
    # summary (D-33): the read the gate above made.
    result["demand_data_through"] = freshness
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
    # The soft asks are judged against the same baseline the requirements
    # were built from (staffing_baseline, L-1/D-4).
    result["_soft_typical"] = {f"{d}|{p}": v for (d, p), v in
                               ((staffing or {}).get("typical_headcount") or {}).items()}
    result["prior_published_rows"] = prior_published_rows
    result["weather_forecast"] = weather_forecast or []
    result["pending_time_off"] = {n: sorted(d) for n, d in constraints.pending_off.items()}
    # Whether the leader rules judge anybody, and ratings entered through
    # support waiting on the owner (D-10) — for the generate screen.
    try:
        import schedule_setup as _setup
        result["leader_rules_status"] = _setup.leader_rules_status(
            restaurant_id, [n for n, _r in roster_pairs], rules=_leader_rules)
    except Exception as _lsx:
        _soft_fail('leader_rules_status', _lsx, restaurant_id)
        result["leader_rules_status"] = None
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


def _asks_as_adjustments(soft_reqs) -> list:
    """The reviews' and the nightly reports' staffing asks
    (staffing_signals.soft_requirements) as requirement adjustments: +1 of
    the asked role on that date and daypart, with the ask's reason, never
    firm — folded into the number the model is given, never a shortfall the
    score marks (schedule audit 10/3/26 PR-7)."""
    from time_utils import mdy
    out = []
    for r in soft_reqs or []:
        if not r.get("date") or not (r.get("role") or "").strip():
            continue
        if r.get("source") == "dsr":
            why = (f"asked by the {mdy(r.get('report_date'))} nightly report" if r.get("report_date")
                   else "asked by the nightly reports")
        else:
            why = "asked by the reviews diagnosis"
        out.append({"date": str(r["date"])[:10], "daypart": r.get("daypart"), "role": r["role"],
                    "delta": 1, "reason": why, "firm": False})
    return out


def schedule_learning_inputs(restaurant_id, restaurant, roster_pairs, shifts, splh, labor_target,
                            own_typical=None) -> dict:
    """The learned inputs a draft is generated against, each failing alone:
    the rotation plan (schedule_intel.rotation_plan), the sales-per-labor-
    hour objective (schedule_economics.splh_objective), and — for each role
    family the restaurant's own history (`own_typical`, the staffing
    baseline) has no usual crew for — a starting headcount borrowed from
    similar restaurants (intelligence.staffing), labelled borrowed. It was
    all or nothing: a restaurant with any history borrowed nothing, so a
    family its punches never show (Simple EJ's managers) had no prior at
    all (schedule audit 10/3/26 L-2)."""
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
        start = _staffing.starting_headcount(restaurant_id, restaurant=restaurant, roster_roles=roles, shifts=shifts,
                                             own_typical=own_typical)
        out["starting_payload"] = _staffing.payload(start)
        if start.get("available"):
            out["borrowed_headcount"] = start["headcount"]
            out["starting_block"] = _staffing.prompt_block(start)
    except Exception as _sfx:
        _soft_fail('starting_headcount', _sfx, restaurant_id)
    return out


def _generate_in_parts(analysis, shifts, roster_pairs, kwargs):
    """One call for the week; if the response ran out of room, the week is
    written again in parts and merged. A big roster starts in parts.

    Only trading days are required to carry shifts: the owner's closed
    weekdays and dates, weekdays the restaurant's own history shows it never
    trades, a Thanksgiving or Christmas the model leaves empty, and the day
    off a one-person roster must take. Every date used to be required, so a
    restaurant closed Mondays failed every week after three paid calls."""
    from labor import generate_optimized_schedule
    from time_utils import restaurant_now
    from datetime import timedelta as _t0, datetime as _dt0
    kwargs = dict(kwargs)
    closed = set(kwargs.pop("closed_dates", None) or [])
    max_days = kwargs.pop("max_consecutive_days", None) or 6
    # The prompt leaves closed dates out of its SHIFT REQUIREMENTS table.
    kwargs["closed_dates"] = sorted(closed)
    parts = 1
    expected = _expected_rows(shifts, roster_pairs)
    if expected > CHUNK_ROWS_PER_CALL:
        parts = 2 if expected <= CHUNK_ROWS_PER_CALL * 2 else 3
    wasted = 0.0
    slices_log = []
    today0 = restaurant_now(kwargs.get("tz_name"), naive=True)
    monday0 = _week_monday(today0, kwargs.get("week_start"))
    all_dates = [(monday0 + _t0(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    trading_wd = _trading_weekdays(shifts)
    open_dates = [d for d in all_dates if d not in closed]
    trading_dates = [d for d in open_dates if _dt0.strptime(d, "%Y-%m-%d").weekday() in trading_wd]

    def _real_missing(csv_text, dates):
        miss = _missing_dates(csv_text, [d for d in dates if d in trading_dates])
        return [] if miss and _acceptable_missing(miss, trading_dates, roster_pairs, max_days) else miss

    if parts == 1:
        result = generate_optimized_schedule(analysis, shifts, **kwargs)
        missing = _real_missing(result.get("schedule_csv", ""), all_dates)
        slices_log.append({"dates": all_dates, "rows_by_date": _rows_by_date(result.get("schedule_csv", "")),
                           "seconds": result.get("generation_seconds"), "stop_reason": result.get("stop_reason"),
                           "missing": missing})
        if not result.get("truncated") and not missing:
            result["chunked"] = 1
            result["slices"] = slices_log
            result["closed_dates"] = sorted(closed | set(_missing_dates(result.get("schedule_csv", ""), all_dates)))
            return result
        # Ran out of room, or wrote nothing for a day: the week is written in
        # parts instead. A day with no draft must never be filled in by a
        # backstop as though the model had staffed it.
        wasted = float(result.get("generation_seconds") or 0)
        parts = 2
    from datetime import datetime as _d, timedelta as _t
    # Same week the single call would have used (the prompt builder computes
    # it from the restaurant's own clock, or from the owner's week_start).
    from time_utils import restaurant_now
    today = restaurant_now(kwargs.get("tz_name"), naive=True)
    monday = _week_monday(today, kwargs.get("week_start"))
    week_dates = [d for d in ((monday + _t(days=i)).strftime("%Y-%m-%d") for i in range(7)) if d not in closed]
    # Past three date slices the roster itself is split by department
    # (kitchen and front of house), each department generated in date
    # slices with the other's rows in view. That is what a 250-person
    # restaurant needs: the rule sweep afterwards arbitrates the merge.
    if not week_dates:
        raise ScheduleGenerationError("The restaurant is marked closed every day of this week, so there is nothing to schedule.")
    # Every call is planned to fit CHUNK_ROWS_PER_CALL: past three date
    # slices the roster is split by department (kitchen and front of house),
    # and a department too big for one call a day is split again into groups
    # of people, each written in as many date slices as it needs. At most
    # three slices of two departments used to be the ceiling, so a 500-person
    # roster planned ~290-row calls against a 160-row budget (SCHED-24).
    plan = []
    if expected > CHUNK_ROWS_PER_CALL * 3:
        per_head = expected / max(1, len(roster_pairs or []))
        for label, people in _departments(roster_pairs).items():
            if not people:
                continue
            k, p = _plan_calls(per_head * len(people), len(week_dates), min_slices=2, max_groups=len(people))
            size_p = -(-len(people) // k)
            chunks = [people[i:i + size_p] for i in range(0, len(people), size_p)]
            size_d = -(-len(week_dates) // p)
            slices = [week_dates[i:i + size_d] for i in range(0, len(week_dates), size_d)]
            for ci, chunk in enumerate(chunks):
                plan.append((label, chunk, ci, len(chunks), slices))
    if not plan:
        size = -(-len(week_dates) // parts)
        plan = [(None, None, 0, 1, [week_dates[i:i + size] for i in range(0, len(week_dates), size)])]
    merged = None
    rows, narrative, seconds = [], [], wasted
    prior_rows = []
    calls = 0
    for label, chunk, ci, n_chunks, slices in plan:
        dept = (label or "STAFF", chunk) if chunk is not None else None
        dkwargs = dict(kwargs)
        if dept:
            dkwargs["roster"] = chunk
            what = dept[0] + (f" GROUP {ci + 1} OF {n_chunks}" if n_chunks > 1 else "")
            dkwargs["extra_blocks"] = (kwargs.get("extra_blocks") or "") + (
                f"\n\nTHIS REQUEST COVERS ONLY THE {what} ROSTER LISTED ABOVE. The rest of the staff is "
                "written separately; do not schedule anyone not on this list.")
        for sl in slices:
            # Each slice sees what the earlier ones wrote — hours so far, days
            # worked, last shift end — so the ceiling, rest and days-off rules
            # can be honoured across the boundary rather than only checked after.
            part = generate_optimized_schedule(analysis, shifts, week_slice=sl, prior_rows=list(prior_rows), **dkwargs)
            calls += 1
            if part.get("truncated"):
                raise ScheduleGenerationError("The week is too long to generate even in parts — trim the roster or split the "
                                 "restaurant into departments, then try again.")
            missing = _real_missing(part.get("schedule_csv", ""), sl)
            if missing:
                # One retry, told exactly which days it skipped. A second
                # miss fails the generation: a week with no Saturday draft
                # that a backstop then fills is worse than no week.
                seconds += float(part.get("generation_seconds") or 0)
                slices_log.append({"dates": sl, "rows_by_date": _rows_by_date(part.get("schedule_csv", "")),
                                   "seconds": part.get("generation_seconds"), "stop_reason": part.get("stop_reason"),
                                   "missing": missing, "retried": True})
                rkwargs = dict(dkwargs)
                rkwargs["extra_blocks"] = (dkwargs.get("extra_blocks") or "") + (
                    "\n\nYOUR PREVIOUS ANSWER WROTE NO SHIFTS FOR " + ", ".join(missing) +
                    ". Every date in this request must have a full day of shifts across every role that normally works it.")
                part = generate_optimized_schedule(analysis, shifts, week_slice=sl, prior_rows=list(prior_rows), **rkwargs)
                calls += 1
                missing = _real_missing(part.get("schedule_csv", ""), sl)
                if missing or part.get("truncated"):
                    raise ScheduleGenerationError("The model wrote no shifts for " + ", ".join(_pretty_dates(missing or sl)) +
                                     " twice; the week was not saved. Try again in a minute.")
            slices_log.append({"dates": sl, "rows_by_date": _rows_by_date(part.get("schedule_csv", "")),
                               "seconds": part.get("generation_seconds"), "stop_reason": part.get("stop_reason"),
                               "missing": []})
            merged = merged or part
            rows.extend(part["schedule_csv"].split("\n")[1:])
            for line in part["schedule_csv"].split("\n")[1:]:
                cols = [c.strip() for c in line.split(",", 7)]
                if len(cols) >= 7 and cols[2]:
                    prior_rows.append({"date": cols[0], "day": cols[1], "employee": cols[2], "role": cols[3],
                                       "shift_start": cols[4], "shift_end": cols[5], "scheduled_hours": cols[6]})
            # Two bullets from each part, labelled with the days they cover,
            # so the note is not slice one's alone.
            label = f"{sl[0][5:]}–{sl[-1][5:]}" if len(sl) > 1 else sl[0][5:]
            for b in (part.get("narrative") or [])[:2]:
                narrative.append(f"{dept[0] + ' ' if dept else ''}{label}: {b}")
            seconds += float(part.get("generation_seconds") or 0)
    merged["schedule_csv"] = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(r for r in rows if r.strip())
    merged["narrative"] = narrative[:6]
    merged["summary"] = narrative[:6]
    merged["generation_seconds"] = round(seconds, 1)
    merged["truncated"] = False
    merged["chunked"] = calls
    merged["departments"] = list(dict.fromkeys(label for label, *_rest in plan if label))
    merged["slices"] = slices_log
    merged["closed_dates"] = sorted(closed | set(_missing_dates(merged["schedule_csv"], all_dates)))
    return merged


def _plan_calls(rows: float, n_dates: int, min_slices: int = 1, max_groups: int = 200) -> tuple:
    """(people_groups, date_slices) with the fewest calls for which every
    call's expected rows — rows × (its share of people) × (its days / the
    week) — fit CHUNK_ROWS_PER_CALL."""
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
    """Rows per date — counting only rows the job's parser will keep. A row
    with no times (3-5 columns) used to count as the day written, then be
    dropped by the parser, so a day could go missing past the retry and a
    week of such rows was saved empty (SCHED-42). A planned manager row
    (schedule_skeleton) is not the model writing the day: a day carrying
    only those still counts as missing."""
    import schedule_skeleton as _skel
    out = {}
    for line in (csv_text or "").split("\n")[1:]:
        cols = [c.strip().strip('"').strip() for c in line.split(",", 7)]
        if len(cols) < 6 or not cols[0] or not cols[2]:
            continue
        if len(cols) == 8 and _skel.is_plan_note(cols[7]):
            continue
        if _rules.parse_minutes(cols[4]) is None and _rules.parse_minutes(cols[3]) is None:
            continue          # no start time where one belongs (nor one column left of it)
        if _rules.parse_minutes(cols[5]) is None and _rules.parse_minutes(cols[4]) is None:
            continue
        out[cols[0]] = out.get(cols[0], 0) + 1
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


def _departments(roster_pairs) -> dict:
    """{"KITCHEN": [(name, role)], "FRONT OF HOUSE": [...]} by role words."""
    out = {"KITCHEN": [], "FRONT OF HOUSE": []}
    for name, role in roster_pairs or []:
        r = (role or "").lower()
        out["KITCHEN" if any(w in r for w in _BOH_WORDS) else "FRONT OF HOUSE"].append((name, role))
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


# Sales older than this many TRADING days stop a generation (schedule audit
# 10/3/26 D-33): the demand, the hours budget and every day's target would
# be built on them as if current. demand.STALE_SAMPLE_DAYS is the same
# horizon for a single forecast.
DEMAND_STALE_BLOCK_DAYS = 14


def _demand_data_through(restaurant_id, today=None, restaurant=None) -> dict:
    """When the daily sales the demand forecast reads were last written,
    and whether that is recent enough to trust: {"date", "days_ago",
    "trading_days_ago", "blind", "stale", "blocked", "line", "message"}.

    Read against the restaurant's own date — it was the server's UTC date,
    a day ahead every evening (D-33). `stale`: sales stopped more than
    DEMAND_STALE_BLOCK_DAYS trading days ago (a closed date or a closed
    weekday the owner marked is not a missing day: a restaurant shut three
    weeks for a refit is not stale the week it reopens). `blocked` is the
    same — generation refuses with `message` rather than drafting a week on
    a forecast that quietly fell back. `blind`: no sales on file at all, or
    stale — the Studio and the draft say so (`line`). A restaurant that has
    never had sales is blind but never blocked: there is nothing stale to
    mislead it, and the draft says it has no demand to plan by."""
    from datetime import date as _date, datetime as _dt, timedelta as _tdd
    from models import get_conn as _gc
    from time_utils import mdy
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
    if today is None:
        try:
            from time_utils import restaurant_now_by_id
            today = restaurant_now_by_id(restaurant_id).date()
        except Exception:
            today = _date.today()
    last = row["d"] if row else None
    if not last:
        return {"date": None, "days_ago": None, "trading_days_ago": None, "blind": True, "stale": False,
                "blocked": False, "line": "No daily sales on file — this week has no demand forecast to plan by.",
                "message": None}
    try:
        last_d = _dt.strptime(str(last)[:10], "%Y-%m-%d").date()
    except ValueError:
        return {"date": last, "days_ago": None, "trading_days_ago": None, "blind": True, "stale": False,
                "blocked": False, "line": None, "message": None}
    days = (today - last_d).days
    # Days the restaurant trades between the last sales and yesterday.
    closed_wd, closed_dates = set(), set()
    try:
        if restaurant is None:
            from models import get_restaurant
            restaurant = get_restaurant(restaurant_id)
        cl = _rules.closures(restaurant) if restaurant is not None else {}
        closed_wd = set(cl.get("closed_weekdays") or [])
        closed_dates = set(cl.get("closed_dates") or [])
    except Exception as _cx:
        print(f"[schedule] closures unreadable for freshness, every day counts: {_cx}")
    trading = 0
    d = last_d + _tdd(days=1)
    while d < today:
        if d.strftime("%A") not in closed_wd and d.isoformat() not in closed_dates:
            trading += 1
        d += _tdd(days=1)
    stale = trading > DEMAND_STALE_BLOCK_DAYS
    out = {"date": last_d.isoformat(), "days_ago": days, "trading_days_ago": trading, "blind": stale,
           "stale": stale, "blocked": stale,
           "line": f"Sales on file through {mdy(last_d)} ({days} day{'s' if days != 1 else ''} ago).",
           "message": None}
    if stale:
        out["line"] = (f"Sales on file end {mdy(last_d)}, {trading} trading days ago — the demand forecast, the "
                       f"hours budget and each day's target would rest on them as if current.")
        out["message"] = (f"Sales on file end {mdy(last_d)} — {trading} trading days with no sales since, so this "
                          f"week's demand, hours budget and day targets can't be built honestly. Reconnect the POS "
                          f"or upload recent sales, then generate. If the restaurant was closed, mark those dates "
                          f"closed under Hours and try again.")
    return out


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
    # Lateness (schedule audit 10/3/26 D-44): who is late to a share of
    # their clocked shifts past staff_settings.LATE_RISK_RATE, once enough
    # of them exist (`late_risk`) — they should not be the one who opens or
    # closes for their role. Said with the raw count.
    late = sorted(((n, r) for n, r in (reliability or {}).items() if r.get("late_risk")),
                  key=lambda kv: -float(kv[1].get("late_rate") or 0))
    if not risky and not late:
        return ""
    out = ""
    if risky:
        # Chosen on the smoothed rate (staff_settings.reliability); said with
        # the raw count, which is what actually happened.
        lines = [f"  {n}: missed {r['no_shows']} of {r['shifts']} watched shifts" if r.get("no_shows") is not None
                 else f"  {n}: missed {int(round(r['no_show_rate'] * 100))}% of {r['shifts']} watched shifts"
                 for n, r in risky[:8]]
        out += ("\n\nATTENDANCE (from shifts somebody watched, recent ones counting most): the people below miss "
                "shifts often. Do not leave any of them alone in "
                "a role, and do not rely on them for the busiest shift of the week; a second body alongside them is "
                "the fix, not fewer hours:\n" + "\n".join(lines))
    if late:
        lines = [f"  {n}: late to {r.get('late', 0)} of {r.get('late_shifts', 0)} clocked shifts" for n, r in late[:8]]
        out += ("\n\nLATENESS (from clocked shifts, recent ones counting most): the people below are often late. "
                "Do not make any of them the only person of their role who opens or closes a day:\n" + "\n".join(lines))
    return out


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


def _reconcile_scheduled_hours(row, tz=None):
    """Recompute scheduled_hours from the row's own shift times.

    The model writes this column itself, and nothing checked it: _row_is_sane
    only asked whether the value parsed as a float. A row reading
    "11:00am,7:00pm,12.0" is an 8-hour shift labelled 12, and that number is
    what the week's total, the labor-budget comparison and the projected
    labor cost are all summed from — so the owner's labor percentage drifts
    by whatever the model happened to write.

    Times are the source of truth: they are what a manager reads off the
    printed schedule and what the staff actually work. An overnight shift
    (end before start) is treated as crossing midnight. With `tz` (the
    restaurant's zone) the hours are the real ones: a 5pm-2am close on the
    night the clocks go back is 10 hours worked, not the 9 the wall clock
    reads, and the hours, the overtime line and the cost all count it
    (schedule audit 10/3/26 E-23 — Halloween 10/31/26, Simple EJ's 2am
    close); the row says so in its notes and carries `dst_hours`. Returns
    the model's own arithmetic error in hours (wall-clock against what it
    wrote, never the clock change), or 0.0 when the row was already right or
    its times can't be parsed.
    """
    start = _parse_time_to_minutes(row.get("shift_start", ""))
    end = _parse_time_to_minutes(row.get("shift_end", ""))
    if start is None or end is None:
        return 0.0
    span = end - start
    if span < 0:
        span += 24 * 60          # closing shift running past midnight
    wall = round(span / 60, 1)
    correct = wall
    clock = 0.0
    if tz:
        real = _rules.span_hours(row, tz) if row.get("date") else 0.0
        if real and abs(real - wall) >= 0.1:
            correct, clock = round(real, 1), round(real - wall, 1)
    raw_hours = (row.get("scheduled_hours") or "").strip() if isinstance(row.get("scheduled_hours"), str) \
        else row.get("scheduled_hours")
    if raw_hours in (None, ""):
        stated = None            # nothing was stated, so nothing was wrong
    else:
        try:
            stated = round(float(raw_hours), 1)
        except (ValueError, TypeError):
            stated = None
    if clock:
        row["dst_hours"] = clock
        note = (row.get("notes") or "").strip()
        text = ("includes the extra hour when the clocks go back" if clock > 0
                else "an hour shorter: the clocks go forward")
        if text not in note:
            row["notes"] = f"{note} ({text})".strip() if note else f"({text})"
    if stated is None:
        row["scheduled_hours"] = str(correct)
        return 0.0
    drift = round(wall - stated, 1)
    if abs(drift) < 0.1 and not clock:
        return 0.0          # already right — leave the row exactly as written
    row["scheduled_hours"] = str(correct)
    if abs(drift) < 0.1:
        return 0.0          # the model counted the wall clock right; the clock change is ours to add
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
    # By role family: "Bartender": 30 holds for a "Bartender PM" row, which
    # was clamped at close while the stays-after-close rule wanted it on
    # (schedule audit 10/3/26 D-13, D-43).
    ceiling = close_minutes + int(_rules.role_minutes(role_buffers, role) or 0)

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


# ── what every pass that changes rows asks first ───────────────────────────
#
# The fill-in passes used to ask only can_work, a certificate, a window, the
# hours ceiling and rest, so a cloned closing shift could land on a 16-17
# year old, on somebody's seventh day in a row, or past the hour the night's
# manager went home (schedule audit 10/3/26 P-2, E-1). The trims asked no
# rule at all: the budget trim took away the only closer or the last manager
# on a night (E-2, P-13), and the section-cap trim undid the owner's floors
# (P-29). Every pass that changes rows now asks the same questions:
#
#   * a person it adds: Constraints.fillable (code never chooses somebody
#     dormant, or on a day their unconfirmed note covers) and
#     Constraints.can_add (their own week swept with the row in — minors,
#     rest, the run of days, hours, the overtime line);
#   * anything it adds, cuts or removes: whether a breach about that day or
#     the people touched is new or worse, compared by what the breach is
#     about (schedule_rules.breach_profile / regressions), never by the row
#     it is pinned to, at every rank above the pass's own;
#   * a cut or a removal: whether a shift falls further under what it needs
#     (the SHIFT REQUIREMENTS the model was given, requirement_index).
#
# A row carrying "_pinned" (a day the owner kept in a redo, a planned manager
# shift) is never removed, re-timed or handed to anybody; it still counts.

_SLOT_MIN = 30


def _pinned(row) -> bool:
    return bool((row or {}).get("_pinned"))


def _changeable(row, only_dates=None) -> bool:
    """Whether a pass may change `row`: not pinned, and on a date this run
    may touch (`only_dates`, the days a partial redo rewrites; None is every
    day)."""
    return not _pinned(row) and (only_dates is None or row.get("date") in only_dates)


def _role_key(role, c=None) -> str:
    """A role's family (shift_quality.role_family, the restaurant's own map
    first): "Server AM" and "Server PM" are one role to every count the
    passes make (D-13)."""
    from shift_quality import role_family
    return role_family(role, getattr(c, "role_families", None) if c is not None else None)


def _name_key(name) -> str:
    return (name or "").strip().lower()


def _weekday_name(iso) -> str:
    try:
        return _date_of(iso).strftime("%A")
    except (TypeError, ValueError):
        return ""


def _present(row) -> list:
    """The dayparts a row is on the floor for — the rule sweep's and the
    score's own count (shift_quality.present_dayparts)."""
    from shift_quality import present_dayparts
    return present_dayparts(row)


def _span_minutes(row):
    """(start, end) minutes past the row's own midnight, an end past
    midnight on the next day's clock (24:30 for 12:30am); None when
    unreadable."""
    s, e = _rules.start_minutes(row), _rules.end_minutes(row)
    if s is None or e is None or e <= s:
        return None
    return s, e


def change_regressions(before_rows, after_rows, c, date, people=(), upto=None, hard_only=True) -> list:
    """schedule_rules.regressions between two versions of the week that
    differ only on `date` and in `people`'s rows, swept over what such a
    change can touch: every row of that date (floors, a closer, a manager on
    the floor and somebody at close are rules about the day) and each named
    person's whole week (their hours, rest, run of days). Nothing else in the
    week moved, so this finds exactly what the change made new or worse, at
    a fraction of a whole-week sweep. `upto` is the lowest rank still
    protected (schedule_rules.TIER_COVERAGE by default: no new hard breach);
    [] when nothing got worse, or with no Constraints to judge by."""
    if c is None:
        return []
    keys = {_name_key(p) for p in people or () if _name_key(p)}

    def _sub(rs):
        return [r for r in rs or [] if r.get("date") == date or _name_key(r.get("employee")) in keys]
    return _rules.regressions(_rules.breach_profile(_sub(before_rows), c),
                              _rules.breach_profile(_sub(after_rows), c),
                              upto=_rules.TIER_COVERAGE if upto is None else upto, hard_only=hard_only)


def _will_not_stand(rows, c) -> set:
    """id() of each row the sweep says will not happen — time off, a double
    booking, off the roster (schedule_rules.NO_SHOW): not coverage, and not
    hours anybody works."""
    if c is None:
        return set()
    try:
        return {id(rows[v["index"]]) for v in _rules.violations(rows, c)
                if v.get("no_show") and v.get("index") is not None and v["index"] < len(rows)}
    except Exception as exc:
        print(f"[schedule] could not read which rows will not stand: {exc}")
        return set()


def _ramp_end(day, part, c=None):
    """Where a daypart's half-hour ramp stops: the 3pm changeover for lunch,
    the close for dinner (a close in the small hours is the next morning),
    midnight with no close on file — the service window the requirements
    table builds the ramp over. None for any other daypart: its last step
    holds for one half hour."""
    split = 15 * 60
    if part == "morning":
        return split
    if part == "night":
        closes = (getattr(c, "close_times", None) or {}) if c is not None else {}
        m = _rules.parse_minutes(closes.get(day, "") or "")
        if m is not None and m < _rules._OVERNIGHT_LATEST_BEFORE:
            m += 24 * 60
        return m if (m is not None and m > split) else 24 * 60
    return None


def _ramp_slots(points, end) -> dict:
    """{minute: people} for every half hour of a ramp given as the points
    where it changes ([(minute, people)], staffing_curve.ramp_runs): each
    step holds until the next, the last until `end`."""
    pts = []
    for p in points or []:
        try:
            pts.append((int(p[0]), int(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
    pts.sort()
    out = {}
    for k, (m, n) in enumerate(pts):
        stop = pts[k + 1][0] if k + 1 < len(pts) else (end if end is not None and end > m else m + _SLOT_MIN)
        for t in range(m, stop, _SLOT_MIN):
            out[t] = n
    return out


def requirement_index(requirements, c=None) -> dict:
    """{date: {daypart: {role family: {"role", "required", "typical", "half":
    {minute: people}}}}} from the SHIFT REQUIREMENTS rows (schedule_requirements
    .shift_requirements, week_requirements): the people each role needs on
    each shift — the number the model was given and the coverage score
    judges, demand-scaled where it is — and the half-hour ramp where the
    sales curve is measured. {} without requirements."""
    out = {}
    for row in requirements or []:
        d, part = row.get("date"), row.get("daypart")
        if not (d and part):
            continue
        slot = out.setdefault(d, {}).setdefault(part, {})
        for x in row.get("roles") or []:
            key = _role_key(x.get("role"), c)
            try:
                n = int(x.get("required") or 0)
            except (TypeError, ValueError):
                n = 0
            if not key or n <= 0:
                continue
            spec = slot.setdefault(key, {"role": (x.get("role") or "").strip(), "required": 0, "half": {}})
            spec["required"] = max(spec["required"], n)
            try:
                spec["typical"] = max(int(spec.get("typical") or 0), int(x.get("typical") or 0))
            except (TypeError, ValueError):
                pass
        end = _ramp_end(row.get("day") or _weekday_name(d), part, c)
        for role, pts in (row.get("half_hours") or {}).items():
            key = _role_key(role, c)
            if not key:
                continue
            spec = slot.setdefault(key, {"role": str(role).strip(), "required": 0, "half": {}})
            for t, n in _ramp_slots(pts, end).items():
                spec["half"][t] = max(spec["half"].get(t, 0), n)
    return out


def requirement_shortfall(rows, index, date, c=None, skip=frozenset()) -> dict:
    """{(daypart, role family, minute or None): people short} on `date`
    against requirement_index. None is the shift's whole crew (people on the
    floor for the daypart, the score's own count); a minute is one half hour
    of its ramp. `skip` holds id() of rows that will not stand — not
    coverage."""
    parts = (index or {}).get(date) or {}
    if not parts:
        return {}
    by_key = {}
    for r in rows or []:
        if r.get("date") != date or id(r) in skip or not _name_key(r.get("employee")):
            continue
        by_key.setdefault(_role_key(r.get("role"), c), []).append(r)
    out = {}
    for part, roles in parts.items():
        for key, spec in roles.items():
            mine = by_key.get(key) or []
            if spec.get("required"):
                on = {_name_key(r.get("employee")) for r in mine if part in _present(r)}
                if len(on) < spec["required"]:
                    out[(part, key, None)] = spec["required"] - len(on)
            if spec.get("half"):
                spans = [(_name_key(r.get("employee")), sp) for r in mine for sp in [_span_minutes(r)] if sp]
                for t, need in spec["half"].items():
                    on = len({n for n, (s, e) in spans if s <= t < e})
                    if on < need:
                        out[(part, key, t)] = need - on
    return out


def requirement_regressions(before_rows, after_rows, index, date, c=None, skip=frozenset()) -> list:
    """[(daypart, role family, minute or None, short before, short after)]
    wherever `after_rows` leaves `date` further under its requirement than
    `before_rows` did — the whole crew first. [] when nothing is."""
    if not index or date not in index:
        return []
    b = requirement_shortfall(before_rows, index, date, c, skip)
    a = requirement_shortfall(after_rows, index, date, c, skip)
    return sorted(((k[0], k[1], k[2], b.get(k, 0), n) for k, n in a.items() if n > b.get(k, 0)),
                  key=lambda x: (x[2] is not None, x[0], x[1], x[2] or 0))


def week_requirements(restaurant_id, result, constraints=None, curve=None) -> list:
    """The SHIFT REQUIREMENTS rows for the week being built: schedule_
    requirements.shift_requirements over the inputs labor.generate_optimized_
    schedule gave the model — the usual headcount (with what was learned),
    the owner's floors and role minimums, the profiles' critical positions,
    demand, the measured sales curve, the section cap — so the passes judge
    "thin" and "needed" by the numbers the draft was written to and is
    scored against (schedule audit 10/3/26 P-20, SQ-7). Closed dates have
    none. [] when they cannot be built."""
    import schedule_requirements as _req
    c = constraints if constraints is not None else result.get("constraints")
    try:
        return _req.shift_requirements(
            list(result.get("week_dates") or []),
            typical_headcount=result.get("typical_headcount") or {},
            role_floors=(getattr(c, "role_floors", None) or {}) if c is not None else {},
            daily_targets=result.get("daily_target_hours") or {},
            profiles=result.get("shift_profiles") or None,
            demand_by_day=result.get("demand_by_day") or {},
            demand_by_date=result.get("demand_by_date") or {},
            leader_rules=result.get("leader_rules") or [],
            role_minimums=result.get("role_minimums") or {},
            skip_dates=sorted(set(getattr(c, "closed_dates", None) or ()) | set(result.get("closed_dates") or ())),
            demand_curve=(curve if curve is not None else _safe_hourly_profile(restaurant_id)) or None,
            open_times=(getattr(c, "open_times", None) or None) if c is not None else None,
            close_times=(getattr(c, "close_times", None) or None) if c is not None else None,
            section_cap=int(getattr(c, "section_cap", 0) or 0) if c is not None else 0,
            cap_roles=sorted(getattr(c, "foh_roles", None) or {"server"}) if c is not None else None)
    except Exception as exc:
        print(f"[schedule] shift requirements unavailable: {exc}")
        return []


def _legal_fills(c, rows, options, date, coverage=True, limit=None) -> tuple:
    """([(employee, row)], refused) — of `options` ([(employee, row)] in the
    pass's own order, each row already timed), the ones a pass may add: the
    code may choose the person (Constraints.fillable), the row keeps their
    own week legal (Constraints.can_add) and makes no breach about the day
    new or worse (change_regressions). First only people who stay under
    their overtime line with no daily overtime; a coverage pass (a floor, a
    station — they outrank overtime in the ranked priorities, P-47) that
    finds nobody then takes somebody past their overtime line, never past
    their hours maximum or any hard rule. A quality pass (the top-up) never
    does. `refused` names everybody the last round turned down."""
    limit = limit or FILL_SCORE_CANDIDATES
    refused = set()
    for relaxed in ((False, True) if coverage else (False,)):
        out, refused = [], set()
        for e, row in options:
            if not c.fillable(e, date)[0] or not c.can_add(row, rows, overtime=not relaxed)[0]:
                refused.add(e)
                continue
            worse = change_regressions(rows, list(rows) + [row], c, date, [e],
                                       upto=_rules.TIER_COVERAGE if relaxed else _rules.TIER_OVERTIME,
                                       hard_only=relaxed)
            if worse:
                refused.add(e)
                continue
            out.append((e, row))
            if len(out) >= limit:
                break
        if out:
            return out, set()
    return [], refused


def _top_up_hours_gap(preview_rows: list, daily_target_hours: dict, hours_budget: float,
                       hours_scheduled: float, restaurant_id: int,
                       close_times: dict, role_buffers: dict, constraints=None, scorer_for=None,
                       requirements=None, only_dates=None) -> tuple:
    """Deterministic post-generation pass that adds a real shift where a
    role on a daypart is genuinely short of what that shift needs — never
    past the week's hours budget, which is a ceiling, not a quota.

    Prompt-only fixes for this (see par_block/_daily_targets in labor.py)
    plateaued around -240h under a ~1314h budget on live testing — asking
    the model to hit a number more insistently in prose has a ceiling. This
    is the same deterministic-backstop pattern as _enforce_close_time,
    applied to headcount instead of shift end times.

    "Short" is measured against the SHIFT REQUIREMENTS the model was given
    (`requirements` — schedule_requirements.shift_requirements rows, built
    by week_requirements): the people each role needs on each daypart, and
    the half-hour ramp where the sales curve is measured. It used to be one
    or more people under the role's own average across this draft, so a
    quiet Monday read thin against a Friday-heavy week while a busy day the
    model under-staffed evenly read as fine (schedule audit 10/3/26 P-20).
    With no requirements nothing is short and nothing is added. The most
    short shift goes first.

    Never invents an employee: only adds a shift for somebody in that role
    (this week, on the roster, or holding it), on a day they aren't already
    working and haven't marked unavailable, whom the code may choose
    (Constraints.fillable) and whose week the row keeps legal — minors,
    rest, the run of days, hours, the overtime line (Constraints.can_add,
    P-2) — and that opens no manager gap or other breach on the day and no
    daily overtime (E-1). The fewest hours first. Times come from a shift of
    that role and daypart (this date's, else the week's; the one covering
    the short half hours when only the ramp is short), else the daypart's
    slice of the opening hours. Every added row is tagged in its notes so
    it's visible, never silent. A day the model wrote nothing for is never
    filled, at most a quarter of a day's target comes from this pass, and
    `only_dates` keeps it to the days a partial redo rewrites (P-10).

    Among the legal people, the one whose shift costs the week the least
    Shift Quality is taken when `scorer_for` gives a scorer.

    Returns (preview_rows, hours_added, added_dates) where added_dates is
    {date: shifts_added_count}.
    """
    if not daily_target_hours or not requirements:
        return preview_rows, 0.0, {}
    remaining_gap = (hours_budget - hours_scheduled) if hours_budget and hours_budget > 0 else float("inf")
    if remaining_gap <= 0:
        return preview_rows, 0.0, {}

    import json as _json_avail
    from models import get_staff_availability
    from models import availability_blocked_days as _av_blocked

    _dates_here = sorted({r.get("date") for r in preview_rows if r.get("date")})
    c = constraints
    if c is None:
        c = _rules.build_constraints(restaurant_id, _dates_here, [_weekday_name(d) for d in _dates_here])
    index = requirement_index(requirements, c)
    if not index:
        return preview_rows, 0.0, {}
    scorer = scorer_for(preview_rows) if scorer_for else None

    # staff_availability only has whole-day granularity in its structured
    # fields — a client who writes "only mornings" or "no closes" has to
    # put it in the freeform notes field instead, which the AI prompt does
    # read (see labor.py's _avail_block) but this deterministic code has
    # no reliable way to parse. Rather than risk scheduling a body into a
    # time window their notes explicitly rule out, anyone with any notes at
    # all is left out of automatic additions unless their note is held as a
    # time window -- conservative, but a missed top-up is a far smaller
    # problem than deterministically violating a constraint a human wrote.
    unavailable_by_emp, available_by_emp, notes_restricted = {}, {}, set()
    for a in get_staff_availability(restaurant_id) or []:
        name = a.get("employee_name")
        if not name:
            continue
        # This week's reading of each row: a weekday blocked only between two
        # dates counts when this week's day is inside them (employee audit M5).
        try:
            unavailable_by_emp[name] = _av_blocked(a, _dates_here or None)
        except Exception:
            unavailable_by_emp[name] = set()
        try:
            if _json_avail.loads(a.get("available_days") or "[]"):
                available_by_emp[name] = set(_WEEKDAYS) - unavailable_by_emp[name]
        except Exception:
            pass
        if (a.get("notes") or "").strip():
            notes_restricted.add(name)

    # Who works each role, the times each role and daypart run this week,
    # and the hours so far — from the model's own output.
    role_people, tpl_by_date, tpl_by_part = {}, {}, {}
    hours_by_employee, working_on_date, by_date_hours = {}, {}, {}
    for r in preview_rows:
        date, emp, role = r.get("date", ""), (r.get("employee") or "").strip(), (r.get("role") or "").strip()
        if not (date and emp and role):
            continue
        key = _role_key(role, c)
        role_people.setdefault(key, set()).add(emp)
        working_on_date.setdefault(date, set()).add(emp)
        hrs = _row_hours_value(r)
        hours_by_employee[emp] = hours_by_employee.get(emp, 0.0) + hrs
        by_date_hours[date] = by_date_hours.get(date, 0.0) + hrs
        # Keyed by daypart: a Saturday with seven morning servers and no
        # dinner ones used to read as "seven servers", and the template it
        # cloned was the first morning row. (role, daypart) is the unit.
        part = _rules.daypart_of(r.get("shift_start", ""))
        if r.get("shift_start") and r.get("shift_end"):
            tpl = (r["shift_start"], r["shift_end"], role)
            for book, k in ((tpl_by_date, (date, key, part)), (tpl_by_part, (key, part))):
                if tpl not in book.setdefault(k, []):
                    book[k].append(tpl)
    # People on the roster in a role, or holding it, even without a row.
    for _n, _role in (getattr(c, "roster_roles", None) or {}).items():
        if _n and _role:
            role_people.setdefault(_role_key(_role, c), set()).add(_n)
    _display = {_name_key(n): n for n in (getattr(c, "roster_names", None) or [])}
    for _low, _held in (getattr(c, "held_roles", None) or {}).items():
        if _display.get(_low):
            for _hr in _held or ():
                role_people.setdefault(_role_key(_hr, c), set()).add(_display[_low])
    # A day the model wrote nothing for is not thin — it is missing, and
    # that is the generation's failure to report, never this pass's to fill.
    dates_with_rows = {d for d, h in by_date_hours.items() if h > 0}
    closed = set(getattr(c, "closed_dates", None) or ())
    skip = _will_not_stand(preview_rows, c)
    _rel = getattr(c, "reliability", None) or {}

    hours_added = 0.0
    added_dates, added_by_date = {}, {}
    exhausted = set()
    MAX_ADDS = 100  # hard safety ceiling regardless of gap size
    for _pass in range(MAX_ADDS):
        if remaining_gap <= 0:
            break
        slots = {}
        for d in sorted(index):
            if d not in dates_with_rows or d in closed or (only_dates is not None and d not in only_dates):
                continue
            # At most a quarter of the day's target can come from this pass;
            # past that the day is flagged, not written.
            cap_day = 0.25 * float(daily_target_hours.get(d) or 0)
            if cap_day and added_by_date.get(d, 0.0) >= cap_day:
                continue
            for (part, key, minute), n in requirement_shortfall(preview_rows, index, d, c, skip).items():
                if (d, part, key) in exhausted:
                    continue
                s = slots.setdefault((d, part, key), {"count": 0, "half": 0, "minutes": []})
                if minute is None:
                    s["count"] = n
                else:
                    s["half"] = max(s["half"], n)
                    s["minutes"].append(minute)
        if not slots:
            break

        def _most_short(k):
            d, part, key = k
            under = float(daily_target_hours.get(d) or 0) - by_date_hours.get(d, 0.0)
            return (-slots[k]["count"], -slots[k]["half"], -under, d, part, key)
        target = min(slots, key=_most_short)
        target_date, part, key = target
        short = slots[target]
        day_name = _weekday_name(target_date)
        spec = index[target_date][part][key]

        # The times to copy: for a crew short of people, that role's shift in
        # that daypart (this date's, else the week's); for a ramp short only
        # at some half hours, the one of them covering the most of those.
        tpls = list(tpl_by_date.get((target_date, key, part)) or []) + \
            [t for t in (tpl_by_part.get((key, part)) or []) if t not in (tpl_by_date.get((target_date, key, part)) or [])]
        if short["count"] <= 0 and short["minutes"]:
            def _covers(t):
                sp = _span_minutes({"shift_start": t[0], "shift_end": t[1]})
                return sum(1 for m in short["minutes"] if sp and sp[0] <= m < sp[1])
            tpls.sort(key=lambda t: -_covers(t))
            if not tpls or not _covers(tpls[0]):
                # No shift of that role runs over the short half hours: one
                # that does, ending with them, four hours at the least.
                hi = max(short["minutes"]) + _SLOT_MIN
                lo = min(min(short["minutes"]), hi - 4 * 60)
                tpls = [(_format_minutes_to_time(lo), _format_minutes_to_time(hi),
                         tpls[0][2] if tpls else (spec.get("role") or key))]
        if tpls:
            start, end, row_role = tpls[0]
        else:
            start, end = _daypart_fallback(c, day_name, part)
            row_role = spec.get("role") or key
        probe = {"date": target_date, "day": day_name, "employee": "", "role": row_role,
                 "shift_start": start, "shift_end": end, "scheduled_hours": "0",
                 "notes": "added — coverage top-up"}
        _enforce_close_time(probe, day_name, close_times, role_buffers)
        sp = _span_minutes(probe)
        # A template (borrowed from another day) that does not produce a sane
        # shift once capped to this day's close, that would not count where
        # the shift is short, or that would cross the hours ceiling: this
        # shift is left for the review, never fabricated.
        counts = (part in _present(probe)) if short["count"] > 0 else \
            bool(sp and any(sp[0] <= m < sp[1] for m in short["minutes"]))
        if probe.get("needs_review") or not sp or not counts:
            exhausted.add(target)
            continue
        hrs = round((sp[1] - sp[0]) / 60, 1)
        probe["scheduled_hours"] = str(hrs)
        if hrs <= 0 or hrs > remaining_gap + 0.01:
            exhausted.add(target)
            continue

        pool = {e for e in role_people.get(key, set()) - working_on_date.get(target_date, set())
                if day_name not in unavailable_by_emp.get(e, set())
                and (e not in available_by_emp or day_name in available_by_emp[e])
                and (e not in notes_restricted or _name_key(e) in (c.time_windows or {}))}
        ordered = sorted(pool, key=lambda e: (hours_by_employee.get(e, 0.0),
                                              float((_rel.get(e) or {}).get("no_show_rate") or 0), e))
        legal, _refused = _legal_fills(c, preview_rows, [(e, dict(probe, employee=e)) for e in ordered],
                                       target_date, coverage=False)
        if not legal:
            # Nobody legal for this shift: it stays short and the review
            # names it — never a bad pick forced in.
            exhausted.add(target)
            continue
        employee, new_row = _fill_pick(scorer, preview_rows, legal)

        preview_rows.append(new_row)
        hours_added += hrs
        remaining_gap -= hrs
        added_dates[target_date] = added_dates.get(target_date, 0) + 1
        added_by_date[target_date] = added_by_date.get(target_date, 0.0) + hrs
        by_date_hours[target_date] = by_date_hours.get(target_date, 0.0) + hrs
        working_on_date.setdefault(target_date, set()).add(employee)
        hours_by_employee[employee] = hours_by_employee.get(employee, 0.0) + hrs

    return preview_rows, round(hours_added, 1), added_dates


def _row_hours_value(row) -> float:
    try:
        return float(row.get("scheduled_hours") or 0)
    except (ValueError, TypeError):
        return 0.0


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
    """Sweep-line peak concurrent headcount for one day's rows (the caller
    hands it the rows the section cap counts). Returns (peak_count,
    peak_time_minutes) — peak_time is None if there are no rows. Ends are
    processed before starts at the same instant, so a shift ending at 3pm
    and one starting at 3pm never count as overlapping at that exact minute.

    A shift past midnight ends on the next day's clock (12:30am is 24:30,
    schedule_rules.end_minutes): every row whose end read as before its
    start used to be skipped, so Friday and Saturday at a 2am-close bar were
    exactly the nights the cap went unenforced (schedule audit 10/3/26
    E-19). peak_time can therefore be 24:00 or later."""
    events = []
    for r in day_rows:
        sp = _span_minutes(r)
        if not sp:
            continue
        events.append((sp[0], 1))
        events.append((sp[1], -1))
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
                              max_overlap: int = None, roles=None, scorer_for=None, constraints=None,
                              floors: dict = None, only_dates=None, report: dict = None,
                              requirements=None) -> tuple:
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
    sweep and the requirement use, by role family ("Server AM" is a server);
    servers alone when not given. Among the rows in the most discretionary
    tier at the peak, the cut that costs the week the least Shift Quality
    is taken when `scorer_for` gives a scorer.

    A cut never takes a role under the owner's floor (`floors`, else the
    Constraints' role floors) for a daypart the row was on the floor for,
    and with `constraints` never makes a rule breach new or worse — the
    night's closer, a manager on the floor, somebody at close, a standing
    rule, a minimum-hours promise (change_regressions, ranked above the
    cap). It never touches a row the owner kept ("_pinned") or a day outside
    `only_dates` (a partial redo; schedule audit 10/3/26 P-29, P-10). When
    nothing at the peak may be cut the night stays over the cap and is
    named once in `report["conflicts"]` ([{date, day, at, on, cap, held_by:
    [{kind: "floor", role, daypart, floor} | {kind: "rule", label}]}]) — a
    floor above the section count is the owner's to resolve in settings
    (floor_cap_conflicts). The sweep runs past midnight (E-19). Among the
    legal cuts, one that leaves no role further under its shift requirement
    (`requirements`, already held under the section count) goes first.
    """
    try:
        _cap = int(max_overlap) if max_overlap and int(max_overlap) > 0 else 0
    except (TypeError, ValueError):
        _cap = 0
    if report is not None:
        report["conflicts"] = []
    if not _cap:
        return preview_rows, 0, {}

    c = constraints
    from shift_quality import role_family as _family
    fams = getattr(c, "role_families", None) if c is not None else None
    counted = {_family(x, fams) for x in (roles or ()) if str(x).strip()} or {"server"}
    floors = floors if floors is not None else ((getattr(c, "role_floors", None) or {}) if c is not None else {})
    index = requirement_index(requirements, c) if requirements else {}
    scorer = scorer_for(preview_rows) if scorer_for else None
    by_date: dict = {}
    for r in preview_rows:
        if r.get("date") and _family(r.get("role"), fams) in counted:
            by_date.setdefault(r.get("date"), []).append(r)

    trimmed_dates: dict = {}
    rows_trimmed = 0
    conflicts = []

    def _on(rs, date, key, part):
        return len({_name_key(x.get("employee")) for x in rs if x.get("date") == date
                    and _family(x.get("role"), fams) == key and part in _present(x)})

    def _cut_ok(r, cut, date, day_name):
        """(ok, why) for `r` becoming `cut` (None: removed)."""
        after = [x for x in preview_rows if x is not r] + ([cut] if cut is not None else [])
        key = _family(r.get("role"), fams)
        for part in _present(r):
            floor = max((_rules.floor_for(floors, role, day_name, part) for role in floors
                         if _family(role, fams) == key), default=0)
            if floor and _on(after, date, key, part) < min(floor, _on(preview_rows, date, key, part)):
                return False, {"kind": "floor", "role": r.get("role"), "daypart": part, "floor": floor}
        worse = change_regressions(preview_rows, after, c, date, [r.get("employee")],
                                   upto=_rules.TIER_MIN_HOURS, hard_only=False)
        if worse:
            return False, {"kind": "rule", "label": worse[0]["label"]}
        return True, None

    for date, day_rows in by_date.items():
        if only_dates is not None and date not in only_dates:
            continue          # a day the owner kept in a partial redo is never re-cut (P-10)
        day_name = _weekday_name(date) or None

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
                rows_sorted = sorted(rows, key=lambda r: _rules.parse_minutes(r.get("shift_start", "")) or 0)
                for r in rows_sorted[1:]:
                    double_shift_second_legs.add(id(r))

        def _priority(r):
            # The top-up tags its rows "coverage top-up" (the older
            # "PAR hours top-up" wording too): the pipeline's own additions.
            is_topup = "top-up" in (r.get("notes") or "").lower()
            is_second_leg = id(r) in double_shift_second_legs
            start = _rules.parse_minutes(r.get("shift_start", "")) or 0
            return (0 if is_topup else 1, 0 if is_second_leg else 1, -start)

        for _pass in range(20):  # bounded — one trim per pass, per day
            peak, peak_time = _peak_server_overlap(day_rows)
            if peak <= _cap or peak_time is None:
                break

            active = []
            for r in day_rows:
                sp = _span_minutes(r)
                if sp and sp[0] <= peak_time < sp[1]:
                    active.append(r)

            def _keeps_requirement(r, cut):
                after = [x for x in preview_rows if x is not r] + ([cut] if cut is not None else [])
                return not requirement_regressions(preview_rows, after, index, date, c)
            ranked = sorted((r for r in active if not _pinned(r)), key=_priority)
            want = FILL_SCORE_CANDIDATES if scorer is not None else 1
            legal, spare, held = [], [], []
            for r in ranked:
                if legal and (_priority(r)[:2] != _priority(legal[0][0])[:2] or len(legal) >= want):
                    break
                cut = _overlap_cut(r, peak_time, day_name, close_times, role_buffers, _cap)
                if cut is False:
                    continue
                ok, why = _cut_ok(r, cut, date, day_name)
                if not ok:
                    if why not in held:
                        held.append(why)
                elif index and not _keeps_requirement(r, cut):
                    spare.append((r, cut))     # legal, but leaves a role short: only if nothing else is
                else:
                    legal.append((r, cut))
            legal = legal or spare[:want]
            if not legal:
                # Nothing at the peak may be cut: the night stays over the
                # cap, and the owner is told what holds it there.
                conflicts.append({"date": date, "day": day_name, "at": _format_minutes_to_time(peak_time),
                                  "on": peak, "cap": _cap, "held_by": held})
                break
            candidate, cut = legal[0]
            if scorer is not None and len(legal) > 1:
                # The tier says which cuts are the discretionary ones; the
                # score says which of them the floor can best spare.
                import time as _time_cap
                if _time_cap.monotonic() - scorer.started <= FILL_SCORE_SECONDS:
                    options = [(id(r), [x for x in preview_rows if x is not r] + ([ct] if ct is not None else []))
                               for r, ct in legal]
                    key, _val = scorer.best(options)
                    candidate, cut = next(((r, ct) for r, ct in legal if id(r) == key), (candidate, cut))
            if cut is None:
                day_rows.remove(candidate)
                preview_rows.remove(candidate)
            else:
                candidate.update(cut)

            rows_trimmed += 1
            trimmed_dates[date] = trimmed_dates.get(date, 0) + 1

    if report is not None:
        report["conflicts"] = conflicts
    return preview_rows, rows_trimmed, trimmed_dates


def _overlap_cut(row: dict, peak_time: int, day_name, close_times: dict, role_buffers: dict, cap: int):
    """The row cut to end at `peak_time` (a new dict), None when that would
    leave under half an hour (the row goes), False when it cannot be read.
    `peak_time` past midnight (24:30) is the next morning's 12:30am."""
    start_min = _rules.parse_minutes(row.get("shift_start", ""))
    if start_min is None:
        return False
    if peak_time - start_min < 30:
        return None
    out = dict(row)
    out["shift_end"] = _format_minutes_to_time(peak_time)
    if day_name:
        _enforce_close_time(out, day_name, close_times, role_buffers)
    sp = _span_minutes(out)
    if not sp:
        return False
    out["scheduled_hours"] = str(round((sp[1] - sp[0]) / 60, 1))
    note = (out.get("notes") or "").strip()
    out["notes"] = f"{note} (trimmed — over the {cap}-server cap)" if note else f"trimmed — over the {cap}-server cap"
    return out


def floor_cap_conflicts(floors: dict, section_cap, cap_roles=None, families=None) -> list:
    """[{day, daypart, floor, cap, roles}] wherever the owner's staffing
    floors for the roles the section count caps add up to more people than
    there are sections — no draft can hold both, so the section-cap trim
    stops at the floor and the night stays over the cap (schedule audit
    10/3/26 P-29). The rules screen shows it as a settings conflict to
    resolve; [] when there is no cap or nothing conflicts."""
    try:
        cap = int(section_cap or 0)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0 or not floors:
        return []
    from shift_quality import role_family as _family
    counted = {_family(x, families) for x in (cap_roles or ()) if str(x).strip()} or {"server"}
    out = []
    for day in _rules.DAYS:
        for part in ("morning", "night"):
            roles = sorted(r for r in floors if _family(r, families) in counted and _rules.floor_for(floors, r, day, part))
            total = sum(_rules.floor_for(floors, r, day, part) for r in roles)
            if total > cap:
                out.append({"day": day, "daypart": part, "floor": total, "cap": cap, "roles": roles})
    return out


def cap_floor_conflict_line(conflicts: list) -> str:
    """One sentence for the review naming the nights the section-cap trim
    left over the cap and what held them there — the owner's floor above
    the section count, or a rule a cut would have broken (schedule audit
    10/3/26 P-29). "" when there are none."""
    if not conflicts:
        return ""
    from time_utils import mdy
    from shift_quality import role_words
    top = conflicts[0]
    held = top.get("held_by") or []
    floor = next((h for h in held if h.get("kind") == "floor"), None)
    rule = next((h for h in held if h.get("kind") == "rule"), None)
    more = len(conflicts) - 1
    head = (f"{top.get('day') or ''} {mdy(top.get('date'))} at {top.get('at')}".strip()
            + f": {top.get('on')} on the floor against {top.get('cap')} sections"
            + (f" (and {more} other night{'s' if more != 1 else ''})" if more else ""))
    if floor:
        part = {"morning": "lunch", "night": "dinner"}.get(floor.get("daypart"), floor.get("daypart") or "")
        return (head + f" — left over the section count, because cutting anyone would take "
                f"{role_words(floor.get('role'), 2, 'that role')} under your floor of {floor.get('floor')} for {part}. "
                "Your floors and your section count disagree: change one of them in the schedule rules.")
    if rule:
        return head + f" — left over the section count, because every cut would break a rule ({rule.get('label')})."
    return head + " — left over the section count: nothing on at the peak could be cut."


def _window_overlap(row: dict, window: tuple) -> bool:
    """Whether a row's span overlaps a window (minutes past midnight); a row
    past midnight ends on the next day's clock."""
    sp = _span_minutes(row)
    return bool(sp) and sp[0] < window[1] and sp[1] > window[0]


_MORNING_WINDOW = (_parse_time_to_minutes("11:00am"), _parse_time_to_minutes("2:30pm"))
_NIGHT_WINDOW = (_parse_time_to_minutes("5:30pm"), _parse_time_to_minutes("8:30pm"))
_DAYPART_SPLIT = 15 * 60   # the same 3pm the rules and the quality engine use


def _daypart_windows(constraints, day_name: str) -> list:
    """[("morning", (start, end)), ("night", (start, end))] for one day, on
    the same 3pm split schedule_rules.daypart_of and shift_quality use, from
    the day's opening and closing times when they are known. A floor
    measured on a fixed 11am–2:30pm slot counted a 6am–10:30am cook as
    nobody and added a second one. A close in the small hours is the next
    morning (a 2am close runs dinner to 26:00)."""
    open_m = _parse_time_to_minutes((getattr(constraints, "open_times", None) or {}).get(day_name, "")) if constraints else None
    close_m = _parse_time_to_minutes((getattr(constraints, "close_times", None) or {}).get(day_name, "")) if constraints else None
    if open_m is None:
        open_m = 6 * 60
    if close_m is not None and close_m < _rules._OVERNIGHT_LATEST_BEFORE:
        close_m += 24 * 60
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
                        constraints=None, scorer_for=None, only_dates=None) -> tuple:
    """Deterministic backstop for the owner's per-role, per-daypart staffing
    floors (schedule_rules.role_floors): "at least 1 line cook on lunch and
    2 at dinner, 3 on Saturday night". A prompt-only floor plateaus below
    full compliance, the same way close times and the server cap did.

    This replaced a rule compiled in for one client (a pizza cook on every
    daypart, doubled on that restaurant's busy days, with a 15-hour
    fallback shift). Only ever adds a shift for somebody who already works
    that role (this week, on the roster, or holding it), never invents a
    person. Times come from another shift of that role in the same daypart
    this week when one exists, else the daypart's slice of the opening hours.

    Who counts toward a floor is who is on the floor for that daypart
    (shift_quality.present_dayparts — the rule sweep's and the score's own
    count), by role family, never somebody whose row will not stand (time
    off, a double booking). Each person added is one the code may choose
    (Constraints.fillable) whose own week the row keeps legal — a minor's
    hours, rest including last week's published tail, the run of days, the
    hours maximum (Constraints.can_add) — and the row may not open a manager
    gap or make any other breach on the day new or worse (change_regressions;
    schedule audit 10/3/26 P-2, E-1). Somebody who stays under their overtime
    line is always taken first; only when nobody can is the floor — which
    outranks overtime — filled past it, never past an hours maximum.
    `only_dates` keeps it to the days a partial redo rewrites (P-10).

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
    if constraints is None:
        constraints = _rules.build_constraints(restaurant_id, week_dates, week_days)
    c = constraints
    _avail_rows = get_staff_availability(restaurant_id) or []
    notes_restricted = {a.get("employee_name") for a in _avail_rows
                        if a.get("employee_name") and (a.get("notes") or "").strip()
                        and _name_key(a.get("employee_name")) not in c.daypart_avail}

    role_people = {}
    working_on_date = {}
    hours_by_employee = {}
    templates = {}
    for r in preview_rows:
        emp, role, date = (r.get("employee") or "").strip(), (r.get("role") or "").strip(), r.get("date")
        if not (emp and date):
            continue
        working_on_date.setdefault(date, set()).add(emp)
        hours_by_employee[emp] = hours_by_employee.get(emp, 0.0) + _row_hours_value(r)
        if role:
            key = _role_key(role, c)
            role_people.setdefault(key, set()).add(emp)
            part = _rules.daypart_of(r.get("shift_start", ""))
            if part in ("morning", "night") and r.get("shift_start") and r.get("shift_end"):
                templates.setdefault((key, part), (r["shift_start"], r["shift_end"]))
    for name, role in (getattr(c, "roster_roles", None) or {}).items():
        if name and role:
            role_people.setdefault(_role_key(role, c), set()).add(name)
    _display = {_name_key(n): n for n in (getattr(c, "roster_names", None) or [])}
    for _low, _held in (getattr(c, "held_roles", None) or {}).items():
        if _display.get(_low):
            for _hr in _held or ():
                role_people.setdefault(_role_key(_hr, c), set()).add(_display[_low])
    skip = _will_not_stand(preview_rows, c)
    _rel = getattr(c, "reliability", None) or {}

    rows_added = 0
    added_dates = {}
    # A closed date, or one the generation accepted as not trading, gets no
    # floor rows: a floor is a minimum for a day that trades, never a reason
    # to open on one that does not.
    _closed = set(getattr(c, "closed_dates", None) or ())
    for date, day_name in zip(week_dates, week_days):
        if date in _closed or (only_dates is not None and date not in only_dates):
            continue
        for role_name in floors:
            key = _role_key(role_name, c)
            for part, _window in _daypart_windows(c, day_name):
                need = _rules.floor_for(floors, role_name, day_name, part)
                if not need:
                    continue
                tried = set()
                for _attempt in range(40):
                    on = {_name_key(r.get("employee")) for r in preview_rows
                          if r.get("date") == date and id(r) not in skip and _name_key(r.get("employee"))
                          and _role_key(r.get("role"), c) == key and part in _present(r)}
                    if len(on) >= need:
                        break
                    pool = set(role_people.get(key, set())) - working_on_date.get(date, set()) - tried
                    pool = {e for e in pool if e not in notes_restricted or _name_key(e) in c.time_windows}
                    if not pool:
                        break
                    start, end = templates.get((key, part)) or _daypart_fallback(c, day_name, part)
                    probe = {"date": date, "day": day_name, "employee": "", "role": role_name,
                             "shift_start": start, "shift_end": end, "scheduled_hours": "0",
                             "notes": f"added — {role_name} floor"}
                    _enforce_close_time(probe, day_name, close_times, role_buffers)
                    sp = _span_minutes(probe)
                    # A template that does not produce a sane shift, or one
                    # that would not count toward this daypart's floor.
                    if probe.get("needs_review") or not sp or part not in _present(probe):
                        break
                    probe["scheduled_hours"] = str(round((sp[1] - sp[0]) / 60, 1))
                    # The fewest hours so far, and among those the most reliable,
                    # is the order; the score chooses among the first few
                    # legal ones (_fill_pick).
                    ordered = sorted(pool, key=lambda e: (hours_by_employee.get(e, 0.0),
                                                          float((_rel.get(e) or {}).get("no_show_rate") or 0), e))
                    legal, refused = _legal_fills(c, preview_rows, [(e, dict(probe, employee=e)) for e in ordered],
                                                  date, coverage=True)
                    tried |= refused
                    if not legal:
                        continue
                    employee, new_row = _fill_pick(scorer, preview_rows, legal)
                    preview_rows.append(new_row)
                    working_on_date.setdefault(date, set()).add(employee)
                    hours_by_employee[employee] = hours_by_employee.get(employee, 0.0) + _row_hours_value(new_row)
                    rows_added += 1
                    added_dates[date] = added_dates.get(date, 0) + 1
    return preview_rows, rows_added, added_dates


def _ensure_station_coverage(preview_rows: list, week_dates: list, week_days: list,
                             close_times: dict, role_buffers: dict, constraints=None,
                             scorer_for=None, only_dates=None) -> tuple:
    """Every kitchen station the owner requires on a daypart gets a cook
    trained on it (kitchen_stations; owner, 9/30/26: "just because a sauté
    cook works sauté doesn't mean the same cook can be on grill").

    The draft's kitchen cooks on each date and daypart are matched to the
    stations it needs (kitchen_stations.week). For a station none of them
    can take, a shift is added for a cook trained on it who is free that
    day — one the code may choose, whose own week the row keeps legal and
    who opens no manager gap or other breach on the day, overtime only when
    no trained cook stays under their line (the same questions the role
    floors ask, _legal_fills; schedule audit 10/3/26 P-2, E-1) — never an
    invented person, never a cook who isn't trained. What can't be filled
    is left for the review to name. `only_dates` keeps it to the days a
    partial redo rewrites (P-10). Returns (preview_rows, rows_added,
    unfilled)."""
    import kitchen_stations as _ks
    cfg = getattr(constraints, "stations", None) or {}
    if not cfg:
        return preview_rows, 0, []
    c = constraints
    scorer = scorer_for(preview_rows) if scorer_for else None
    working_on_date, hours_by_employee, templates = {}, {}, {}
    for r in preview_rows:
        emp, date = (r.get("employee") or "").strip(), r.get("date")
        if not (emp and date):
            continue
        working_on_date.setdefault(date, set()).add(emp)
        hours_by_employee[emp] = hours_by_employee.get(emp, 0.0) + _row_hours_value(r)
        if _ks.is_kitchen(cfg, r.get("role")) and r.get("shift_start") and r.get("shift_end"):
            for part in _ks.parts_of(r):
                templates.setdefault(part, (r["shift_start"], r["shift_end"], r.get("role")))
    roster_roles = getattr(c, "roster_roles", None) or {}
    closed = set(getattr(c, "closed_dates", None) or ())
    added, unfilled = 0, []
    for gap in _ks.week(preview_rows, cfg, week_dates)["gaps"]:
        date, day, part, station = gap["date"], gap["day"], gap["daypart"], gap["station"]
        if date in closed or (only_dates is not None and date not in only_dates):
            continue
        # Still a gap? An earlier addition this pass may have covered it.
        if not _ks.uncovered(preview_rows, cfg, date, part):
            continue
        pool = [name for name, have in (cfg.get("skills") or {}).items()
                if station in have and name not in working_on_date.get(date, set())]
        start, end, role = templates.get(part) or (_daypart_fallback(c, day, part) + (cfg["roles"][0],))
        options = []
        for name in sorted(pool, key=lambda n: (hours_by_employee.get(n, 0.0), n)):
            person_role = roster_roles.get(name) if _ks.is_kitchen(cfg, roster_roles.get(name)) else role
            row = {"date": date, "day": day, "employee": name, "role": person_role,
                   "shift_start": start, "shift_end": end, "scheduled_hours": "0",
                   "notes": f"added — {station} station"}
            _enforce_close_time(row, day, close_times, role_buffers)
            sp = _span_minutes(row)
            if row.get("needs_review") or not sp or part not in _ks.parts_of(row):
                continue
            row["scheduled_hours"] = str(round((sp[1] - sp[0]) / 60, 1))
            options.append((name, row))
        legal, _refused = _legal_fills(c, preview_rows, options, date, coverage=True)
        if not legal:
            unfilled.append(gap)
            continue
        name, row = _fill_pick(scorer, preview_rows, legal)
        preview_rows.append(row)
        working_on_date.setdefault(date, set()).add(name)
        hours_by_employee[name] = hours_by_employee.get(name, 0.0) + _row_hours_value(row)
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
    exists this week to copy times from. A close in the small hours is the
    next morning: a 2am close gives dinner 6pm–2am (at most eight hours to
    the close), not the four-hour 3–7pm stub it read as when 2am was taken
    for 2 in the morning before the doors opened."""
    open_m = _parse_time_to_minutes((constraints.open_times or {}).get(day_name, "")) if constraints else None
    close_m = _parse_time_to_minutes((constraints.close_times or {}).get(day_name, "")) if constraints else None
    late = close_m is not None and close_m < _rules._OVERNIGHT_LATEST_BEFORE
    if part == "morning":
        start = open_m if open_m is not None else _parse_time_to_minutes("11:00am")
        end = min(close_m, 15 * 60) if (close_m is not None and not late) else 15 * 60
    elif late:
        end = close_m + 24 * 60
        start = max(open_m if open_m is not None else 0, 15 * 60, end - 8 * 60)
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
        # Every holiday on the date is named, each with its own night last
        # year; the date's lift is the strongest measured one — one number,
        # never two holidays added (schedule audit 10/3/26 E-8, PR-8).
        for name in (h.get("names") or [h["name"]]):
            one = (h.get("by_name") or {}).get(name) or (h if name == h.get("name") else {})
            label = name + (f" ({one['lift_pct']:+d}% here last year)" if one.get("lift_pct") is not None else "")
            if label not in e.setdefault("labels", []):
                e["labels"].append(label)
        if h.get("lift_pct") is not None:
            e["lift_pct"] = h["lift_pct"] if e.get("lift_pct") is None else max(e["lift_pct"], h["lift_pct"])
    return holiday


def _merge_date_demand(restaurant_id, dates, signals_by_date: dict, weather=None, closed=()) -> tuple:
    """(holiday, date_demand): the holiday lift folded into the dated
    signals (_merge_holiday_lift), then each date's ONE demand number
    (schedule_economics.date_demand: the owner's budget for the night, the
    dated facts, the forecast's measured effects, measured rain) written
    back as that date's `lift_pct` — so the requirements table, the day
    targets, the shift's demand level and its expected sales all read the
    same figure, never a lift stated in one place and missing in another
    (schedule audit 10/3/26 D-23, D-24, D-30, PR-8). The signal's own lift
    is kept as `signal_lift_pct`, the reasons under `demand`. One
    implementation for the generation and the live rescore."""
    import schedule_economics as _econ
    holiday = _merge_holiday_lift(restaurant_id, dates, signals_by_date)
    dd = _econ.date_demand(restaurant_id, dates, signals_by_date, weather=weather, closed_dates=closed)
    for d, x in dd.items():
        if x.get("closed") or x.get("pct") is None:
            continue
        had = signals_by_date.get(d)
        if not had and not x.get("pct"):
            continue
        e = signals_by_date.setdefault(d, {"lift_pct": None, "covers": None, "labels": []})
        e["signal_lift_pct"] = e.get("lift_pct")
        e["lift_pct"] = int(x["pct"]) if (x.get("pct") or e.get("lift_pct") is not None) else e.get("lift_pct")
        for lab in x.get("labels") or []:
            if lab and lab not in e.setdefault("labels", []):
                e["labels"].append(lab)
        e["demand"] = {"pct": x.get("pct"), "ratio": x.get("ratio"), "reasons": list(x.get("reasons") or []),
                       "typical_sales": x.get("typical_sales"), "projected_sales": x.get("projected_sales"),
                       "sources": list(x.get("sources") or [])}
    return holiday, dd


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
    # Leader rules whatever the ratings (P-18, D-10), as the generation reads them.
    leader_rules = get_shift_leader_rules(restaurant_id)
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

    # Always built, as the generation builds them (SQ-15, SQ-22): a manager's
    # edit is judged against the same re-levelled, tuned set.
    profiles = _sq.profiles_from_config(
        [_sq.profile_from_dict(p) for p in stored] or None,
        default_strength=thresholds, default_leader_rules=leader_rules,
        demand_by_day=demand_by_day, tuning=_quality_tuning(restaurant_id))

    try:
        # Through the request-scoped cache, like every other reader. Parsing
        # the whole history here and again in _quality_signals meant a single
        # manager edit paid for two full passes over it. The same baseline
        # the draft was built from (labor.staffing_baseline: the salaried
        # out, the published weeks for those who never punch, the late
        # segment — L-1, D-4, D-32).
        from labor import staffing_baseline
        _first = min((r.get("date") for r in (week_rows or []) if r.get("date")), default=None)
        patterns = staffing_baseline(restaurant_id, shifts=_cached_shifts(restaurant_id), before=_first)
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
                # The same per-date demand the draft was written against: the
                # weather is the copy already on file — a manager's edit never
                # waits on the forecast service.
                try:
                    import weather as _wx
                    _cached_wx = _wx.cached_forecast_for_week(restaurant, dates)
                except Exception:
                    _cached_wx = []
                _h, out["date_demand"] = _merge_date_demand(restaurant_id, dates, out["demand_by_date"],
                                                            weather=_cached_wx, closed=c.closed_dates)
            except Exception as _hx:
                print(f"[schedule] live date demand unavailable: {_hx}")
            # The same person at a sibling location that day, as generation
            # scores it.
            try:
                from models import sibling_location_shifts as _sibs
                out["elsewhere"] = _sibs(restaurant_id, dates) or {}
            except Exception as _ex:
                print(f"[schedule] live sibling shifts unavailable: {_ex}")
            out["prior_week_assignments"] = _prior_week_assignments(restaurant_id, before=dates[0])
            out["pending_time_off"] = {n: sorted(d) for n, d in c.pending_off.items()}
            # The week's demand-scaled requirements, rebuilt from the same
            # inputs through the same function the draft's table came from,
            # so an edit is judged on the numbers the draft was written to
            # (P-19). The sales-per-labor-hour objective is kept for the
            # scorer's own targets (_learning_signals reads it).
            try:
                import schedule_economics as _econ_live
                out["splh_objective"] = _econ_live.splh_objective(restaurant_id)
            except Exception as _ox:
                print(f"[schedule] live sales-per-labor-hour objective unavailable: {_ox}")
                out["splh_objective"] = {"available": False}
            try:
                import labor_standards as _ls_live
                _std_live = _ls_live.for_requirements(restaurant_id, dates, patterns.get("typical_headcount") or {},
                                                      out.get("date_demand") or {}, restaurant=restaurant,
                                                      needs_only=True)
                out["requirements"] = _live_requirements(
                    dates, patterns, c, profiles, demand_by_day, out.get("demand_by_date") or {},
                    out.get("role_minimums") or {}, out.get("date_demand") or {},
                    (out.get("splh_objective") or {}).get("hold") or {}, leader_rules,
                    bool(scores) or bool(out.get("leader_flags")), (_std_live or {}).get("needs") or {})
                import schedule_requirements as _req_live
                out["requirements_by_date"] = _req_live.requirements_map(out["requirements"])
            except Exception as _rqx:
                print(f"[schedule] live requirements unavailable: {_rqx}")
    except Exception as _cx:
        print(f"[schedule] live constraints unavailable: {_cx}")
    out["learned_worse"] = learned_worse_levers(restaurant_id)
    return out


def _live_requirements(dates, patterns, c, profiles, demand_by_day, demand_by_date, role_minimums,
                       date_demand, splh_hold, leader_rules, leadership_known, standard_needs=None) -> list:
    """SHIFT REQUIREMENTS for a stored week, every role, from the inputs a
    live rescore holds — the draft's own inputs, through the one function
    (schedule_requirements.shift_requirements)."""
    import schedule_requirements as _req
    return _req.shift_requirements(
        sorted(dates), typical_headcount=(patterns or {}).get("typical_headcount") or {},
        role_floors=c.role_floors or {}, profiles=profiles or None, demand_by_day=demand_by_day or None,
        demand_by_date=demand_by_date or None, leader_rules=leader_rules or None,
        leadership_known=leadership_known, role_minimums=role_minimums or None,
        skip_dates=sorted(c.closed_dates or ()), open_times=c.open_times or None,
        close_times=c.close_times or None, section_cap=int(getattr(c, "section_cap", 0) or 0),
        cap_roles=sorted(getattr(c, "foh_roles", None) or {"server"}), date_demand=date_demand or None,
        splh_hold=splh_hold or None, late_headcount=(patterns or {}).get("late_headcount") or None,
        standard_needs=standard_needs or None)


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

    No demand level is written: every tail day used to say "normal", so a
    busy Saturday before the week began read as quiet (schedule audit
    10/3/26 SQ-27). The scorer reads each one with the restaurant's own
    profiles, as it reads this week's shifts (shift_quality.build_contexts).
    How the weeks before this one were loaded is load_ledger's.
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
                     "day": _dt.strptime(date, "%Y-%m-%d").strftime("%A")}
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


# How many published weeks fatigue and fairness read back across (SQ-27) —
# the same eight the rotation and the fairness ledger read.
LOAD_LEDGER_WEEKS = 8


def load_ledger(restaurant_id, before: str, weeks: int = LOAD_LEDGER_WEEKS) -> dict:
    """{name: [{"week", "hours", "slots": [[weekday, daypart]], "role"}]},
    oldest week first: what each person carried in the published weeks of
    the `weeks` before `before` (schedule audit 10/3/26 SQ-27). Fatigue
    judged one week and the rotation covers weekends and closes only, so
    somebody at their hours ceiling or on every Friday and Saturday night
    week after week never read as strained. The scorer reads which slots
    were busy with the restaurant's own profiles. The published version of
    each week only (the one staff received). {} on any failure — the
    measures that read it withdraw."""
    if not before:
        return {}
    from datetime import datetime as _dt, timedelta as _td
    from models import get_conn
    from schedule_versions import rows_from_csv
    import shift_quality as _sq
    try:
        since = (_dt.strptime(str(before)[:10], "%Y-%m-%d") - _td(weeks=int(weeks))).strftime("%Y-%m-%d")
        conn = get_conn()
        try:
            found = conn.execute(
                "SELECT week_start, schedule_csv FROM schedule_history WHERE restaurant_id=? "
                "AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM "
                "schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND "
                "nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND "
                "nw.id > schedule_history.id) AND week_start < ? AND week_start >= ? "
                "ORDER BY week_start DESC LIMIT ?", (restaurant_id, str(before)[:10], since, int(weeks))).fetchall()
        finally:
            conn.close()
    except Exception as _lx:
        _soft_fail("load_ledger", _lx, restaurant_id)
        return {}
    out = {}
    for w in reversed(found):
        per = {}
        for r in rows_from_csv(w["schedule_csv"] or ""):
            n, d = (r.get("employee") or "").strip(), (r.get("date") or "").strip()
            try:
                day = _dt.strptime(d, "%Y-%m-%d").strftime("%A")
            except ValueError:
                continue
            if not n:
                continue
            e = per.setdefault(n, {"week": w["week_start"], "hours": 0.0, "slots": [], "roles": {}})
            h = _sq._row_hours(r)
            e["hours"] += h
            e["slots"].append([day, _sq.present_dayparts(r)[0]])
            role = (r.get("role") or "").strip()
            if role:
                e["roles"][role] = e["roles"].get(role, 0.0) + h
        for n, e in per.items():
            roles = e.pop("roles")
            e["role"] = max(roles.items(), key=lambda kv: (kv[1], kv[0]))[0] if roles else ""
            e["hours"] = round(e["hours"], 2)
            out.setdefault(n, []).append(e)
    return out


def _breach_dayparts(v: dict, row: dict = None) -> list:
    """The dayparts a hard breach is about: a floor's own daypart, the
    stretch an unmanaged gap covers, else the row's."""
    import shift_quality as _sq
    if v.get("daypart") in ("morning", "night"):
        return [v["daypart"]]
    if v.get("gap_start") is not None and v.get("gap_end") is not None:
        out = []
        if int(v["gap_start"]) < _sq.DAYPART_CUTOVER:
            out.append("morning")
        if int(v["gap_end"]) > _sq.DAYPART_CUTOVER:
            out.append("night")
        return out
    if row is not None:
        parts = [p for p in _sq.present_dayparts(row) if p != "unknown"]
        if parts:
            return parts
    part = _rules.daypart_of(v.get("shift_start") or "")
    return [part] if part != "unknown" else ["morning", "night"]


def hard_breach_map(viols, rows: list = None) -> dict:
    """The rule sweep's hard breaches for the rows it swept, as the scorer
    reads them (schedule audit 10/3/26 SQ-14 — the cap is D1a's):
    {"by_date": {date: [breach]}, "week": [breach], "rows_sig": {date:
    signature}}. A breach: {"id" (schedule_rules.breach_id), "kind", "tier"
    (BREACH_TIER), "label", "detail", "employee" (None for a day-level
    one), "dayparts", "day_level", "no_show"}, plus "minutes" for an
    unmanaged gap. "week" holds the ones about no date (no manager on the
    roster at all). rows_sig is each date's LocalScorer signature when the
    rows are given, so a pass can tell its rows have moved on: scored on
    changed rows, the scorer keeps a day's breach on that day (a what-if
    swap on a capped night must not claim to lift the cap) and drops a
    person's breach whose person is no longer on the date
    (shift_quality.place_breaches). A pass that wants the breaches of ITS
    rows recomputes with this function over a fresh sweep."""
    import shift_quality as _sq
    by_date, week = {}, []
    for v in viols or []:
        if not v.get("hard"):
            continue
        kind = v.get("kind")
        idx = v.get("index")
        row = rows[idx] if (rows is not None and isinstance(idx, int) and 0 <= idx < len(rows)) else None
        entry = {"id": list(_rules.breach_id(v)), "kind": kind, "tier": _rules.BREACH_TIER.get(kind),
                 "label": v.get("label"), "detail": v.get("detail"),
                 "employee": None if v.get("day_level") else v.get("employee"),
                 "dayparts": _breach_dayparts(v, row), "day_level": bool(v.get("day_level")),
                 "no_show": bool(v.get("no_show"))}
        if v.get("minutes") is not None:
            entry["minutes"] = v.get("minutes")
        d = v.get("date") or ""
        if kind in ("no_manager_roster", "manager_rule_unusable") or not d:
            week.append(entry)
        else:
            by_date.setdefault(d, []).append(entry)
    sig = {}
    if rows is not None:
        for d in by_date:
            sig[d] = _sq.LocalScorer._signature([r for r in rows if (r.get("date") or "").strip() == d
                                                 and (r.get("employee") or "").strip()])
    return {"by_date": by_date, "week": week, "rows_sig": sig}


def _viols_for(rows: list, result: dict):
    """The rule sweep of exactly `rows`: the generation's own when it is of
    these rows (every breach points at the row it names), else a fresh one
    with the week's constraints; None without them."""
    viols = result.get("rule_violations")
    if viols is not None:
        for v in viols:
            idx = v.get("index")
            if not isinstance(idx, int) or not (0 <= idx < len(rows or [])):
                viols = None
                break
            r = rows[idx]
            if ((r.get("employee") or "") != (v.get("employee") or "") or (r.get("date") or "") != (v.get("date") or "")
                    or (r.get("shift_start") or "") != (v.get("shift_start") or "")):
                viols = None
                break
    if viols is None:
        c = result.get("constraints")
        if c is None:
            return None
        viols = _rules.violations(rows or [], c)
    return viols


def _hard_breaches_for(rows: list, result: dict, viols=None) -> dict:
    """hard_breach_map for the rows being scored (_viols_for); {} without
    the week's constraints."""
    viols = _viols_for(rows, result) if viols is None else viols
    if viols is None:
        return {}
    return hard_breach_map(viols, rows)


def _flagged_for(rows: list, result: dict, viols=None) -> set:
    """(person, date, start) of the rows of `rows` the sweep says will not
    stand (off roster, on time off, double-booked …) — never counted as
    somebody on the floor (P-28); empty without the week's constraints."""
    viols = _viols_for(rows, result) if viols is None else viols
    return {((v.get("employee") or "").strip().lower(), v.get("date") or "", v.get("shift_start") or "")
            for v in (viols or []) if v.get("no_show")}


def _learned_preferences_signal(raw: dict) -> dict:
    """schedule_intel.behaviour_preferences as the scorer and the solver read
    it (schedule audit 10/3/26 L-19): {name: {"avoid": [[weekday,
    daypart]], "prefer": [...], "weight", "source", "drops", "claims"}}.
    Each slot is one the person dropped (or picked up) at least twice in
    the last twelve weeks — that function's own bar for a pattern; a slot
    they both dropped and claimed says nothing either way and is left out.
    Counted at shift_quality.LEARNED_PREFERENCE_WEIGHT of a stated one."""
    import shift_quality as _sq

    def _slot(text):
        bits = str(text or "").split()
        if len(bits) != 2 or bits[0] not in _rules.DAYS:
            return None
        part = {"night": "night", "day": "morning", "morning": "morning"}.get(bits[1].lower())
        return [bits[0], part] if part else None
    out = {}
    for name, p in (raw or {}).items():
        if not isinstance(p, dict) or not str(name or "").strip():
            continue
        avoid = [s for s in (_slot(x) for x in p.get("avoids") or []) if s]
        prefer = [s for s in (_slot(x) for x in p.get("prefers") or []) if s]
        both = {tuple(s) for s in avoid} & {tuple(s) for s in prefer}
        avoid = [s for s in avoid if tuple(s) not in both]
        prefer = [s for s in prefer if tuple(s) not in both]
        if avoid or prefer:
            out[name] = {"avoid": avoid, "prefer": prefer, "weight": _sq.LEARNED_PREFERENCE_WEIGHT,
                         "source": "what they drop and pick up", "drops": p.get("drops"), "claims": p.get("claims")}
    return out


def _overtime_inputs(restaurant_id, c, rules=None) -> dict:
    """What the scorer's overtime measure reads (schedule audit 10/3/26
    SQ-25) — the overtime forecast's own inputs: the line overtime pay
    starts past (labor.OVERTIME_THRESHOLD_HOURS), the daily line where the
    rules set daily overtime, each date's payroll week, the hours already
    published in those payroll weeks, and the rates the forecast prices its
    moves at (models.get_role_rates, the restaurant's hourly rate). {}
    without the week's constraints."""
    if c is None:
        return {}
    rates, default = {}, None
    try:
        from models import get_role_rates, get_restaurant
        rates = get_role_rates(restaurant_id) or {}
        default = getattr(get_restaurant(restaurant_id), "hourly_rate", None) or rates.get("_default")
    except Exception as _rx:
        _soft_fail("overtime rates", _rx, restaurant_id)
    return {"line": _labor_ot_line(),
            "daily_line": (c.compliance or {}).get("daily_ot_hours") or None,
            "bucket_of": {d: c.bucket(d) for d in (c.week_dates or [])},
            "published": {k: dict(v or {}) for k, v in (c.base_hours or {}).items()},
            "rates": rates, "default_rate": default, "rules": rules or {}}


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
        # The week's demand-scaled requirements the model was given, firm
        # numbers only, as {(date, daypart): {role: people}} — coverage is
        # judged against them, not the weekday's usual crew (schedule audit
        # 10/3/26 P-19); "late" keys carry the late segment (D-32).
        "requirements_by_date": _requirements_signal(result.get("requirements_by_date")),
        "requirement_reasons": _reasons_signal(result.get("requirements")),
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
        stated = _staff.stated_preferences(restaurant_id)
    except Exception:
        stated = {}
    _people_signals(restaurant_id, result, signals, stated)
    _reconcile_to_roster(signals)
    # Leader rules against who on the roster can meet them (schedule audit
    # 10/3/26 SQ-17), after the ratings and closer flags are reconciled to
    # the roster: nobody able sets a rule aside (named in confidence, never
    # failing every shift it covers — SCHED-30); fewer able than it asks for
    # keeps it, capped at the people able — "2 bartenders scoring 5" with
    # one such bartender used to be set aside whole, so the one who could be
    # scheduled was never asked for. People count in a rule's role by
    # family, through their roster role, the roles their history shows and
    # the roles they hold.
    try:
        _cap_leader_rules(signals)
    except Exception as _lx:
        _soft_fail("leader rule check", _lx, restaurant_id)
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
    # The hard breaches of the rows being scored (SQ-14, the cap is D1a's):
    # only a caller holding those rows can say (_score_schedule_quality
    # passes them); without it the scorer is told nothing, never a stale
    # sweep of other rows.
    signals.setdefault("hard_breaches", {})
    signals.update(extra)
    return signals, weights


def _people_signals(restaurant_id, result, signals: dict, stated: dict = None) -> None:
    """The people facts the scorer, the solver and the optimizer read beyond
    ratings, from the week's constraints (schedule audit 10/3/26 — the D1b
    signals contract), in place:

      salaried            name keys (models.salaried_name_key) paid the same
                          whatever the hours: out of the hourly day and SPLH
                          hours, held to their own ceiling (D-3)
      salaried_cap        their weekly cap when they set none of their own
      hours_ceilings      {name key: Constraints.max_hours, or
                          salaried_limit for a salaried person} — the
                          ceiling each roster person's week is judged by
      managers            {lower: role} (Constraints.managers)
      acting_managers     {lower: set(iso)} — standing in on those dates
      experienced_default managers, acting managers and salaried people:
                          experienced whatever their punch count (D-6)
      role_families, held_roles, closers_by_role — as Constraints holds them
      stations            the kitchen stations config (SQ-26)
      overtime            the overtime forecast's inputs (SQ-25)
      preferences         stated preferences, each person's learned slots
                          merged in as "learned" (L-19)
      learned_preferences {name: {"avoid", "prefer", "weight", ...}} — the
                          same learned slots on their own

    Each read failing costs only its own measure."""
    import shift_quality as _sq
    c = result.get("constraints")
    roster = [n for n in (result.get("roster") or []) if n]
    if c is not None:
        salaried = set(getattr(c, "salaried", None) or ())
        cap = getattr(c, "salaried_cap", None)
        signals["salaried_cap"] = float(cap) if cap else float(_rules.SALARIED_HOURS_CAP)
        signals["managers"] = dict(getattr(c, "managers", None) or {})
        signals["acting_managers"] = {k: set(v or ()) for k, v in (getattr(c, "acting_managers", None) or {}).items()}
        signals["role_families"] = dict(getattr(c, "role_families", None) or {})
        signals["held_roles"] = {k: set(v or ()) for k, v in (getattr(c, "held_roles", None) or {}).items()}
        signals["closers_by_role"] = {k: set(v or ()) for k, v in (getattr(c, "closers_by_role", None) or {}).items()}
        signals["stations"] = dict(getattr(c, "stations", None) or {})
        ceilings = {}
        for n in (roster or list(getattr(c, "roster_names", None) or [])):
            try:
                # A salaried person's week is judged against the most code
                # would schedule them for (their own maximum, else the
                # salaried cap — 55h by default), not the 84h hard line the
                # sweep keeps for a week they write themselves (E-17, D-3).
                mx = c.salaried_limit(n) if c.is_salaried(n) else c.max_hours(n)
            except Exception:
                continue
            if mx:
                ceilings[_sq.name_key(n)] = float(mx)
        signals["hours_ceilings"] = ceilings
        signals["overtime"] = _overtime_inputs(restaurant_id, c, signals.get("rules"))
        # What each person and each role is paid (labor.person_rate_book):
        # the solver and the optimizer weigh a move's labor dollars by them
        # (schedule audit 10/3/26 P-32). Read once per result — the passes
        # gather these signals several times a generation.
        book = result.get("_rate_book")
        if book is None:
            try:
                import labor as _lab_rates
                book = _lab_rates.person_rate_book(restaurant_id)
            except Exception as _bx:
                _soft_fail("person rates", _bx, restaurant_id)
                book = ({}, {})
            result["_rate_book"] = book
        if signals["overtime"]:
            signals["overtime"]["person_rates"] = dict(book[0] or {})
            signals["overtime"]["role_typical"] = dict(book[1] or {})
    else:
        # No week's constraints (a bare re-score): who is salaried still
        # comes from the restaurant, so their hours never read as hourly.
        try:
            from models import salaried_staff, salaried_name_key
            salaried = {salaried_name_key(s["name"]) for s in salaried_staff(get_restaurant(restaurant_id))}
        except Exception as _sx:
            _soft_fail("salaried staff", _sx, restaurant_id)
            salaried = set()
    signals["salaried"] = salaried
    signals["experienced_default"] = _sq.experienced_by_default(signals.get("managers"),
                                                                signals.get("acting_managers"), salaried)
    try:
        raw = result.get("learned_preferences")
        if raw is None:
            import schedule_intel as _si
            raw = _si.behaviour_preferences(restaurant_id)
        learned = _learned_preferences_signal(raw)
    except Exception as _px:
        _soft_fail("learned preferences", _px, restaurant_id)
        learned = {}
    prefs = {n: dict(p) for n, p in (stated or {}).items() if isinstance(p, dict)}
    for n, lp in learned.items():
        prefs.setdefault(n, {"preferred_dayparts": [], "desired_hours": None})["learned"] = lp
    signals["preferences"] = prefs
    signals["learned_preferences"] = learned


def _cap_leader_rules(signals: dict) -> None:
    """signals["leader_rules"] held to who on the roster can meet them
    (shift_quality.roster_leader_rules, SQ-17), in place: each rule nobody
    can meet stays listed and is named in unmeetable_leader_rules (the
    scorer sets it aside); each one fewer can meet than it asks for is
    replaced by a copy capped at the people able (capped_leader_rules)."""
    import shift_quality as _sq
    rules = list(signals.get("leader_rules") or [])
    roster_roles = signals.get("roster_roles") or {}
    people_roles = None
    if roster_roles:
        cross = signals.get("cross_trained") or {}
        held = signals.get("held_roles") or {}
        people_roles = {}
        for n, role in roster_roles.items():
            roles = {role.strip()} if (role or "").strip() else set()
            roles |= {str(x).strip() for x in (cross.get(n) or []) if str(x).strip()}
            roles |= {str(x).strip() for x in (held.get((n or "").strip().lower()) or ()) if str(x).strip()}
            people_roles[n] = roles
    kept, aside, capped = _sq.roster_leader_rules(rules, signals.get("scores"), signals.get("leader_flags"),
                                                  people_roles, signals.get("role_families"))
    out, k = [], iter(kept)
    for rule in rules:
        out.append(rule if any(rule is a for a in aside) else next(k))
    signals["leader_rules"] = out
    signals["unmeetable_leader_rules"] = aside
    signals["capped_leader_rules"] = capped
    signals["unsatisfiable"] = len(aside)


def _requirements_signal(raw) -> dict:
    """{(date, daypart): {role: people}} from a result's
    "date|daypart"-keyed requirements (JSON-safe) or already tuple-keyed."""
    out = {}
    for k, v in (raw or {}).items():
        key = tuple(k.split("|", 1)) if isinstance(k, str) else tuple(k)
        if len(key) == 2 and isinstance(v, dict):
            out[key] = dict(v)
    return out


def _reasons_signal(rows) -> dict:
    """{(date, daypart): [reasons]} from the requirements rows."""
    try:
        import schedule_requirements as _req
        return _req.requirement_reasons(rows or [])
    except Exception:
        return {}


def _learning_signals(restaurant_id, result) -> dict:
    """{rotation, splh_targets, daypart_sales, learned, load_ledger} for the
    scorer. Each failing costs only its own part (each withdraws on {})."""
    out = {"rotation": {}, "splh_targets": {}, "daypart_sales": {}, "learned": [], "load_ledger": {}}
    c = result.get("constraints")
    week = list(result.get("week_dates") or (getattr(c, "week_dates", None) if c is not None else None) or [])
    # What the restaurant's scheduling memory holds that binds this week
    # (schedule audit 10/3/26 L-3: learned manager patterns reached only
    # the prompt): its active memories, for the scorer, the solver and the
    # optimizer — empty until the memory has any. Read lazily; a memory
    # that cannot be read costs only itself.
    try:
        import schedule_memory as _smem
        learned = _smem.enforced_signals(restaurant_id, week, roster_names=list(result.get("roster") or []) or None)
        out["learned"] = [dict(x) for x in (learned or []) if isinstance(x, dict)]
    except Exception as _mx:
        _soft_fail("schedule_memory", _mx, restaurant_id)
    # The published weeks before this one: what fatigue and fairness read
    # across weeks (SQ-27).
    if week:
        out["load_ledger"] = load_ledger(restaurant_id, sorted(week)[0])
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
            out["splh_targets"] = {wd: {p: _econ_t.splh_target_for(obj, wd, p) for p in _econ_t.SPLH_PARTS
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
    facts are scored; without one, everything stands. The weeks before
    this one are read the same way (schedule audit 10/3/26 SQ-27): somebody
    who has left is no longer a colleague a share is measured against."""
    roster = {str(n).strip().lower() for n in (signals.get("roster") or []) if n}
    if not roster:
        return
    for key in ("scores", "leader_flags", "ledger", "load_ledger"):
        d = signals.get(key) or {}
        if isinstance(d, dict):
            signals[key] = {n: v for n, v in d.items() if str(n).strip().lower() in roster}


# ── the Studio's re-score as a manager edits (schedule audit 10/3/26 P-25) ─
#
# Every drag in the Studio re-scored the week from nothing: all of the shift
# history for "is this restaurant live", every input the generation read
# rebuilt from the database, the whole week scored, the what-if's sixty
# whole-week scores and the rule sweep — several seconds on one of four
# gunicorn threads, and two editors could hold the process. Now the
# inputs, the signals, the restaurant's live check, a kept sweep and an
# exact local scorer are built once per week being edited and reused for
# STUDIO_CACHE_SECONDS; each drag re-sweeps and re-scores only the people
# and dates it moved (the answer is score_rows' and violations' exactly);
# the what-if runs only when the editor asks for it. A save always rebuilds
# from the database, so what is stored is never judged on minutes-old inputs.

STUDIO_CACHE_SECONDS = 120.0
STUDIO_CACHE_SIZE = 8
# The dates (in their edited variants) one week's kept scorer holds.
STUDIO_SCORER_DATES = 96
_studio_cache = {}
_studio_live = {}


def studio_invalidate(restaurant_id=None) -> None:
    """Forget the kept Studio inputs and live checks — one restaurant's, or
    every one (the test suite's fresh database per test reuses ids)."""
    for store in (_studio_cache, _studio_live):
        for k in [k for k in list(store) if restaurant_id is None
                  or (k[0] if isinstance(k, tuple) else k) == restaurant_id]:
            store.pop(k, None)


def studio_is_live(restaurant_id) -> bool:
    """Whether the restaurant's own shifts are on file (labor.
    analyse_shifts_for_restaurant's is_live), remembered for
    STUDIO_CACHE_SECONDS — it reads every shift the restaurant has."""
    import time as _time
    now = _time.monotonic()
    hit = _studio_live.get(restaurant_id)
    if hit is not None and now - hit[1] < STUDIO_CACHE_SECONDS:
        return hit[0]
    from labor import analyse_shifts_for_restaurant
    live = bool((analyse_shifts_for_restaurant(restaurant_id) or {}).get("is_live"))
    if len(_studio_live) > 256:
        _studio_live.clear()
    _studio_live[restaurant_id] = (live, now)
    return live


def studio_prepared(restaurant_id, rows, daily_target_hours=None, history_id=None, fresh: bool = False) -> dict:
    """What a Studio re-score of `rows` reads, built once per week being
    edited ({inputs, signals, weights, sweep, scorer, built}): the inputs
    the generation would rebuild (quality_inputs_from_db), the scorer's
    signals and weights (_quality_signals), the week's Constraints with the
    roster on them, a kept rule sweep (schedule_rules.IncrementalSweep) and
    an exact local scorer (shift_quality.LocalScorer). Kept for
    STUDIO_CACHE_SECONDS per restaurant, week, hour targets and
    capability_version (a rating, profile, leader rule or weight changed);
    `fresh` rebuilds it (a save)."""
    import time as _time
    from models import capability_version
    dates = tuple(sorted({r.get("date") for r in rows or [] if r.get("date")}))
    targets = tuple(sorted((str(k), float(v)) for k, v in (daily_target_hours or {}).items()))
    try:
        cap = capability_version(restaurant_id)
    except Exception:
        cap = None
    key = (restaurant_id, history_id or None, dates, targets, cap)
    now = _time.monotonic()
    hit = _studio_cache.get(key)
    if hit is not None and not fresh and now - hit["built"] < STUDIO_CACHE_SECONDS:
        return hit
    inputs = quality_inputs_from_db(restaurant_id, daily_target_hours=dict(targets), week_rows=rows)
    c = inputs.get("constraints")
    if c is not None and inputs.get("roster"):
        c.active = {n.lower() for n in inputs["roster"]}
        c.roster_names = list(inputs["roster"])
    signals, weights = _quality_signals(restaurant_id, inputs)
    import threading as _threading
    # One editor's drag at a time per week: the kept scorer re-points its
    # kept shifts at each call's week, so two at once would read each
    # other's (the web process runs four threads).
    entry = {"inputs": inputs, "signals": signals, "weights": weights, "built": now,
             "sweep": _rules.IncrementalSweep(c) if c is not None else None, "scorer": None,
             "lock": _threading.Lock()}
    for k in [k for k, v in _studio_cache.items() if now - v["built"] >= STUDIO_CACHE_SECONDS or k[:3] == key[:3]]:
        _studio_cache.pop(k, None)
    while len(_studio_cache) >= STUDIO_CACHE_SIZE:
        _studio_cache.pop(next(iter(_studio_cache)))
    _studio_cache[key] = entry
    return entry


def _score_schedule_quality(restaurant_id, rows, result, what_if=True, prepared=None, **extra):
    """Score the finished schedule, then see whether a better one existed.

    The what-if pass only ever trades two people between shifts of the same
    role, so headcount, hours and coverage cannot move. That restriction is
    what makes running it on every generation affordable and its answers
    explainable: exactly two names changed, and here is what it bought.
    Every swap it offers is held to every rule the week is (the week's
    Constraints, schedule audit 10/3/26 P-33). `what_if` False skips it —
    the Studio's re-score on every drag asks for it only on demand (P-25).
    """
    import shift_quality as _sq
    profiles = result.get("shift_profiles") or None
    if prepared is not None:
        # The Studio's kept inputs (studio_prepared): the sweep and the score
        # of exactly these rows, re-done only where the edit moved them.
        lock = prepared.get("lock")
        if lock is not None:
            lock.acquire()
        try:
            quality, signals, weights = _score_prepared(restaurant_id, rows, result, prepared, extra, profiles)
        finally:
            if lock is not None:
                lock.release()
    else:
        quality, signals, weights = _score_fresh(restaurant_id, rows, result, extra, profiles)

    if not what_if:
        what_if = {"ran": False, "on_demand": True,
                   "reason": "Ask for a better arrangement to look for one."}
    else:
        what_if = {"ran": False, "reason": "Nothing to compare."}
    if quality.get("checked") and not what_if.get("on_demand"):
        try:
            what_if = _sq.compare_candidates(rows, profiles=profiles, weights=weights,
                                             rule_constraints=result.get("constraints"), **signals)
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
            # a sentence themselves (their copies of the prefix list drifted),
            # and so do the points the week gains when it is done (schedule
            # audit 10/3/26 SQ-28: shift_quality.recommendation_details).
            items.append({"text": rec, "kind": kind, "key": key, "rec_key": key, "advice_signature": sig,
                          "points": (quality.get("recommendation_points") or {}).get(rec)})
        # Both clients show at most SHOWN_RECOMMENDATIONS (the web panel's
        # loop, iOS's block); the list is cut here so what is served is what
        # is shown, on every client.
        quality["recommendations"] = kept[:SHOWN_RECOMMENDATIONS]
        quality["recommendation_items"] = items[:SHOWN_RECOMMENDATIONS]
        if quality.get("recommendation_details") is not None:
            shown = set(quality["recommendations"])
            quality["recommendation_details"] = [d for d in quality["recommendation_details"]
                                                 if d.get("text") in shown]
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


def _score_fresh(restaurant_id, rows, result, extra, profiles) -> tuple:
    """(quality, signals, weights) of `rows`, every input read now."""
    import shift_quality as _sq
    if "hard_breaches" not in extra or ("flagged" not in extra and result.get("constraints") is not None):
        # The hard breaches of exactly these rows, for the scorer's cap
        # (SQ-14), and the rows of them that will not stand (P-28): the set
        # read before the solver and the optimizer moved them (flagged_rows)
        # judged the finished week by rows it no longer had. The
        # generation's own sweep when it is of these rows, else a fresh one.
        try:
            _sweep = _viols_for(rows, result)
            if "hard_breaches" not in extra:
                extra = dict(extra, hard_breaches=_hard_breaches_for(rows, result, viols=_sweep) if _sweep is not None else {})
            if "flagged" not in extra and _sweep is not None:
                extra = dict(extra, flagged=_flagged_for(rows, result, viols=_sweep))
        except Exception as _hx:
            _soft_fail("hard breaches", _hx, restaurant_id)
    signals, weights = _quality_signals(restaurant_id, result, **extra)
    quality = _sq.score_rows(rows, profiles=profiles, weights=weights, **signals)
    return quality, signals, weights


def _score_prepared(restaurant_id, rows, result, prepared, extra, profiles) -> tuple:
    """(quality, signals, weights) of `rows` over the Studio's kept inputs:
    the kept sweep, and — the week's rules being there — the kept exact
    scorer (shift_quality.LocalScorer.evaluate: score_rows' answer,
    re-scoring only the dates the edit moved)."""
    import shift_quality as _sq
    viols = prepared["sweep"].violations(rows) if prepared.get("sweep") is not None else None
    if "hard_breaches" not in extra:
        extra = dict(extra, hard_breaches=hard_breach_map(viols, rows) if viols is not None else {})
    if "flagged" not in extra and viols is not None:
        extra = dict(extra, flagged=_flagged_for(rows, result, viols=viols))
    signals = dict(prepared["signals"])
    signals.update(extra)
    weights = prepared["weights"]
    prepared["violations"] = viols
    if viols is None:
        # No rules to keep a sweep of (the week's constraints could not be
        # built): nothing to gain from keeping the scorer either.
        return _sq.score_rows(rows, profiles=profiles, weights=weights, **signals), signals, weights
    if prepared.get("scorer") is None:
        prepared["scorer"] = _sq.LocalScorer(rows, profiles=profiles, weights=weights, exact=True,
                                             cache_limit=STUDIO_SCORER_DATES,
                                             **{k: v for k, v in signals.items()
                                                if k not in ("flagged", "hard_breaches")})
    quality = prepared["scorer"].evaluate(rows, flagged=signals.get("flagged") or set(),
                                          hard_breaches=signals.get("hard_breaches") or {})
    return quality, signals, weights




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
                      focus=None, gate=True, _fallback=None, instruction=None):
    """week_start picks the week (any date in it); dates + base_history_id
    regenerate only those days of an existing draft, the rest pinned.
    focus names what was weak in those days for the prompt; gate allows one
    automatic regeneration of a draft's weakest days (_quality_gate) — never
    on a redo the owner asked for, which must touch only the days they chose.
    instruction is what the owner asked for with this draft (PR-19)."""
    if dates and not focus:
        gate = False
    import csv as _csv_mod, traceback as _tb, datetime as _dt_sched
    try:
        _pinned = []
        if dates and base_history_id:
            from models import get_schedule_history_detail as _gshd
            _base = _gshd(int(base_history_id), restaurant_id) or {}
            _pinned = [r for r in _versions.rows_from_csv(_base.get("schedule_csv") or "") if r.get("date") not in set(dates)]
            week_start = week_start or _base.get("week_start")
        _build_kw = {"week_start": week_start}
        if focus:
            _build_kw["focus"] = list(focus)
        if instruction:
            _build_kw["instruction"] = instruction
        if _pinned:
            # The manager plan of a redo covers only its days, with the kept
            # days' rows counted for hours, rest and runs (schedule_skeleton).
            _build_kw.update(dates=sorted(set(dates)), prior_rows=_pinned)
        result = _build_schedule_result(restaurant_id, **_build_kw)
        # A partial redo rewrites only these days; the passes below that can
        # change rows (fixes, the repair loop, the budget trim) leave the
        # owner's kept days exactly as they were.
        _editable = set(dates) if (dates and base_history_id) else None
        if _pinned:
            # Only the asked-for days were written; the rest come from the
            # draft the owner is keeping.
            keep = set(dates)
            lines = [ln for ln in result["schedule_csv"].split("\n")[1:] if ln.split(",", 1)[0].strip() in keep]
            for r in _pinned:
                lines.append(",".join(str(r.get(c, "") or "").replace(",", ";") for c in _COLS_PINNED))
            result["schedule_csv"] = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(lines)
            result["regenerated_dates"] = sorted(keep)
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
            # The planned manager rows of the dates this run planned come
            # from the plan itself (restore_pinned, below), never through
            # this parser: its close cap would re-time a manager staying
            # until the last role's after-close stay, and a planned row must
            # not make an unreadable answer look like a week (SCHED-42).
            import schedule_skeleton as _skeleton
            _mplan = result.get("manager_plan") or {}
            _plan_dates = set(_mplan.get("dates") or ())
            if _plan_dates:
                _csv_lines = _csv_lines[:1] + [ln for ln in _csv_lines[1:]
                                               if not (ln.split(",", 1)[0].strip() in _plan_dates
                                                       and _skeleton.is_plan_line(ln))]
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
                # _reconcile_scheduled_hours — in real hours, so the night
                # the clocks go back counts its extra hour (E-23).
                _drift = _reconcile_scheduled_hours(
                    _row, tz=(getattr(_restaurant_for_sched, "timezone", None) or "").strip() or None)
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
            # Every planned manager row, exactly as planned and carrying
            # "_pinned" (schedule audit 10/3/26 PR-1, PR-32): no pass after
            # this removes, re-times or re-assigns it (the pinned-row
            # contract), and none lands on a day the generation accepted as
            # closed. A redo restores only the days it rewrote.
            _plan_rows = [r for r in (_mplan.get("rows") or [])
                          if _editable is None or r.get("date") in _editable]
            if _plan_rows:
                preview_rows = _skeleton.restore_pinned(preview_rows, _plan_rows,
                                                        closed=result.get("closed_dates") or ())
                hours_scheduled = _safe_hours_sum(preview_rows)

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
            # The SHIFT REQUIREMENTS the draft was written to, built once from
            # the generation's own inputs and shared: the top-up fills only
            # what is short of them and no trim cuts under them (schedule
            # audit 10/3/26 P-20, SQ-7). Every pass below keeps to the days a
            # partial redo rewrites (`only_dates`, P-10).
            import schedule_economics as _econ
            _curve = _safe_hourly_profile(restaurant_id)
            _reqs = week_requirements(restaurant_id, result, _constraints, curve=_curve)
            result["shift_requirements"] = _reqs
            preview_rows, pizza_rows_added, pizza_added_dates = _ensure_role_floors(
                preview_rows, result.get("week_dates", []), result.get("week_days", []),
                restaurant_id, _close_times, _role_close_buffers,
                floors=_constraints.role_floors, constraints=_constraints, scorer_for=_scorer_for,
                only_dates=_editable,
            )
            if pizza_rows_added:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] role floors added {pizza_rows_added} row(s) across {pizza_added_dates}")
            # Kitchen stations (kitchen_stations): each required station on
            # each daypart gets a cook trained on it.
            preview_rows, _st_added, _st_unfilled = _ensure_station_coverage(
                preview_rows, result.get("week_dates", []), result.get("week_days", []),
                _close_times, _role_close_buffers, constraints=_constraints, scorer_for=_scorer_for,
                only_dates=_editable)
            if _st_added:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] station coverage added {_st_added} row(s); {len(_st_unfilled)} left open")

            _hourly_so_far = _hourly_hours_sum(preview_rows, _constraints)
            preview_rows, hours_added, added_dates = _top_up_hours_gap(
                preview_rows, result.get("daily_target_hours", {}),
                result.get("hours_budget", 0), _hourly_so_far, restaurant_id,
                _close_times, _role_close_buffers, constraints=_constraints, scorer_for=_scorer_for,
                requirements=_reqs, only_dates=_editable,
            )
            if hours_added:
                hours_scheduled = round(hours_scheduled + hours_added, 1)
                print(f"[schedule] coverage top-up added {hours_added}h across {added_dates}")
            # _extend_shifts_to_close_gap is deliberately no longer run: it
            # pushed closers later to consume an hours budget the prompt
            # calls a ceiling. Kept for a caller that wants it explicitly.

            _cap_report = {}
            preview_rows, rows_trimmed, trimmed_dates = _trim_server_overlap_cap(
                preview_rows, _close_times, _role_close_buffers,
                max_overlap=getattr(_restaurant_for_sched, 'section_count', None),
                roles=getattr(_constraints, "foh_roles", None), scorer_for=_scorer_for,
                constraints=_constraints, floors=_constraints.role_floors, only_dates=_editable,
                report=_cap_report, requirements=_reqs,
            )
            # Nights the cap could not be met without going under a floor or
            # breaking a rule: named once in the review (P-29).
            result["cap_floor_conflicts"] = _cap_report.get("conflicts") or []
            if rows_trimmed:
                hours_scheduled = _safe_hours_sum(preview_rows)
                print(f"[schedule] trimmed {rows_trimmed} row(s) over the server cap across {trimmed_dates}")

            # Staggered starts along the day's sales curve and the trim back
            # to the budget, each reported in the review. A role's arrival
            # is a clock-in lead before each person's own shift
            # (attendance.clock_in_leads), never a reason to move a shift.
            _stagger_scorer = _scorer_for(preview_rows) if (_scorer_for and _curve) else None
            preview_rows, _staggered = _econ.stagger_same_starts(
                preview_rows, _curve, score_fn=_stagger_scorer.score if _stagger_scorer else None,
                constraints=_constraints, only_dates=_editable, requirements=_reqs)
            result["staggered"] = _staggered
            result["hourly_profile_ready"] = bool(_curve)
            result["trimmed"] = []
            result["hours_trimmed"] = 0.0
            result["budget_conflict"] = None
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
            if int(getattr(_restaurant_for_sched, "trim_to_budget", 1) or 0):
                # The same local scorer the fill-in passes use: a removal
                # re-scores only its own date (it used to re-score the week).
                _score_fn = None
                try:
                    _trim_scorer = _scorer_for(preview_rows) if _scorer_for else None
                    _score_fn = _trim_scorer.score if _trim_scorer is not None else None
                except Exception as _tx:
                    print(f"[schedule] score-aware trim unavailable: {_tx}")
                # Never a cut under the requirements, on a day cuts measured
                # worse, or in a daypart that has gone wrong before (L-24);
                # what it could not trim is said once in the review (SQ-7).
                _trim_report = {}
                preview_rows, _trimmed, _hours_trimmed = _econ.trim_to_budget(
                    preview_rows, result.get("hours_budget", 0), result.get("daily_target_hours") or {},
                    constraints=_constraints, floors=_constraints.role_floors, splh=result.get("splh_by_daypart") or {},
                    rainy_dates=_rainy, patio_roles=_constraints.patio_roles, score_fn=_score_fn,
                    only_dates=_editable, requirements=_reqs, learned_worse=result.get("learned_worse"),
                    outcomes=result.get("outcomes_by_daypart"), soft_asks=result.get("soft_requirements"),
                    report=_trim_report)
                result["trimmed"] = _trimmed
                result["hours_trimmed"] = _hours_trimmed
                result["budget_conflict"] = _trim_report.get("conflict")
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
                                          "kind": _m.get("kind") or "overtime", "reason": _m["reason"]})
                    for _t in _ot["trims"]:
                        _ot_fixes.append({"index": _t["index"], "from": _t["employee"] + " " + _t["was"],
                                          "to": _t["now"], "kind": _t.get("kind") or "overtime", "reason": _t["reason"]})
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
                                              "left": _mg["left"], "shortfall": _mg.get("shortfall")}
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
            # Somebody under the minimum hours the owner set gets legal shifts
            # in their roles — from a teammate above their own target, or
            # added inside the hours budget — after overtime, the manager rule
            # and the person repairs, never undoing any of them (schedule
            # audit 10/3/26 P-4: a full-time cook set to 40-45h got 7h).
            try:
                _mh = _rules.fill_min_hours(preview_rows, _constraints,
                                            roster_roles=result.get("roster_roles") or {}, editable=_editable,
                                            hours_budget=float(result.get("hours_budget") or 0) or None)
                if _mh["moves"] or _mh["added"]:
                    preview_rows = _mh["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    for _x in _mh["moves"]:
                        _ot_fixes.append({"index": _x["index"], "from": _x["from"], "to": _x["to"],
                                          "kind": "min_hours", "reason": _x["reason"]})
                    for _x in _mh["added"]:
                        _ot_fixes.append({"index": _x["index"], "from": "", "to": _x["employee"],
                                          "kind": "min_hours", "reason": _x["reason"]})
                result["min_hours"] = {"moved": len(_mh["moves"]), "added": len(_mh["added"]), "left": _mh["left"]}
            except Exception as _mhx:
                print(f"[schedule] minimum-hours pass failed: {_mhx}")
                try:
                    _ops.capture(_mhx, job="schedule_min_hours", context=f"restaurant_id={restaurant_id}")
                except Exception:
                    pass
            # The owner's start/end rules for a role (schedule audit 10/3/26
            # L-33 — a retime the manager kept making, made a rule): every row
            # of that role on that daypart is retimed to it where that is
            # legal and makes nothing above quality worse. Last, so no fill,
            # stagger or trim moves it again.
            try:
                _rt = _rules.apply_role_times(preview_rows, _constraints, editable=_editable)
                if _rt["retimed"]:
                    preview_rows = _rt["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    for _x in _rt["retimed"]:
                        _ot_fixes.append({"index": _x["index"], "from": _x["employee"] + " " + _x["from"],
                                          "to": _x["to"], "kind": "role_time", "reason": _x["reason"]})
                result["role_times"] = {"retimed": len(_rt["retimed"]), "left": _rt["left"]}
            except Exception as _rtx:
                print(f"[schedule] role time rules not applied: {_rtx}")
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
            # A breach about a day (no manager on, a floor short) is no row's
            # to fix by changing its person: it stays a day-level item in the
            # review, never an "unfixed" line on whoever's row it is pinned to
            # (schedule audit 10/3/26 E-13).
            _hard = [v for v in _viols if v["hard"] and not v.get("day_level")
                     and (_editable is None or (preview_rows[v["index"]].get("date") in _editable))]
            # A missed run of days off is fixed here too (one of the person's
            # shifts to a legal teammate, least score cost); which rows may
            # move is limited to the editable days inside apply_fixes. The
            # other soft fixable breach, under_min_hours, has its own pass
            # (fill_min_hours, above).
            _days_off = [v for v in _rules.fixable(_viols) if not v["hard"] and v["kind"] == "days_off"]
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
                    # The same trim as the first, with its scorer and the rain
                    # it was given — it used to run with neither — and the
                    # same refusals: nothing it removes can take away the
                    # night's closer or a manager after close-out ran (P-13).
                    _t2_score = None
                    try:
                        _t2_scorer = _scorer_for(preview_rows) if _scorer_for else None
                        _t2_score = _t2_scorer.score if _t2_scorer is not None else None
                    except Exception as _t2sx:
                        print(f"[schedule] score-aware post-fix trim unavailable: {_t2sx}")
                    _t2_report = {}
                    preview_rows, _t2, _h2 = _econ.trim_to_budget(
                        preview_rows, _hb2, result.get("daily_target_hours") or {}, constraints=_constraints,
                        floors=_constraints.role_floors, splh=result.get("splh_by_daypart") or {},
                        rainy_dates=_rainy, patio_roles=_constraints.patio_roles, score_fn=_t2_score,
                        only_dates=_editable, requirements=_reqs, learned_worse=result.get("learned_worse"),
                        outcomes=result.get("outcomes_by_daypart"), soft_asks=result.get("soft_requirements"),
                        report=_t2_report)
                    result["budget_conflict"] = _t2_report.get("conflict")
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
                _mc["shortfall"] = _mg2.get("shortfall")
            except Exception as _mgx:
                print(f"[schedule] manager coverage pass failed: {_mgx}")
            # The owner's start/end rules once more on the finished rows
            # (schedule audit 10/3/26 L-33): the optimizer may stretch a shift
            # after the first pass; a rule is the owner's word, so it has the
            # last say on any start or end nothing ranked above it needs.
            try:
                _rt2 = _rules.apply_role_times(preview_rows, _constraints, editable=_editable)
                if _rt2["retimed"]:
                    preview_rows = _rt2["rows"]
                    hours_scheduled = _safe_hours_sum(preview_rows)
                    for _x in _rt2["retimed"]:
                        _fixes.append({"index": _x["index"], "from": _x["employee"] + " " + _x["from"],
                                       "to": _x["to"], "kind": "role_time", "reason": _x["reason"]})
                    _viols = _rules.violations(preview_rows, _constraints)
                    _price_week(preview_rows)
            except Exception as _rtx2:
                print(f"[schedule] role time rules not re-applied: {_rtx2}")
            # #47: the solver's changes lead "what Cavnar changed" (web and iOS read optimizer.changes).
            if (result.get("solver") or {}).get("ran"):
                import schedule_solver as _solver_m
                result["optimizer"] = _solver_m.merge_into_optimizer(result.get("optimizer"), result["solver"])
            for _v in _viols:
                # A breach about a DAY (no manager on, a floor short) is shown
                # on the day (review.hard_days), never as "needs review" on
                # whichever row it is pinned to (schedule audit 10/3/26 E-13).
                if not _v["hard"] or _v.get("day_level"):
                    continue
                _r = preview_rows[_v["index"]]
                _r["needs_review"] = True
                _r["review_reason"] = _v["detail"] if _v["kind"] == "over_max_hours" else _v["label"]
            result["rule_violations"] = _viols
            result["review"] = _rules.summarize(_viols)
            result["review"]["fixes"] = _fixes
            result["review"]["unfixed"] = _unfixed
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
            # The manager plan, said (schedule audit 10/3/26 D-5, PR-1): why
            # a stretch has no manager, the owner's question when the
            # managers' days are unknown, a standing shift that could not be
            # used. Kept with the review so a reopened week still asks.
            result["review"]["lines"].extend(
                _skeleton.review_lines(_mplan, gaps=_rules.manager_gaps(preview_rows, _constraints)))
            result["review"]["manager_plan"] = {
                "question": _mplan.get("question"), "unknown_pattern": list(_mplan.get("unknown_pattern") or []),
                "uncovered": _skeleton.payload(_mplan)["uncovered"]}
            # Why the manager rule could not be met, once for the week (each
            # day's stretch is already its own day-level line), and who is
            # still under the minimum hours set for them, with why (E-12,
            # E-13, P-4). Not "⚠" lines: the breaches themselves are.
            _short = ((result.get("manager_coverage") or {}).get("shortfall") or {}).get("text")
            if _short:
                result["review"]["lines"].append(_short)
            for _x in ((result.get("min_hours") or {}).get("left") or [])[:4]:
                result["review"]["lines"].append(
                    f"{_x['employee']} is {_x['short_by']:g}h under the {_x['min']:g}h minimum you set — {_x['reason']}")
            # The setup the week was drafted against, for the owner to
            # confirm (schedule audit 10/3/26 F1): who counts as the
            # manager, who stands in, the closers, trading days with no
            # close, leader rules that judge nobody, each staffing rule as
            # it is checked, who is training, who is salaried-style.
            try:
                _setup_r = __import__("schedule_setup").setup_review(
                    _constraints, leader_status=result.get("leader_rules_status"),
                    # the manager plan already asks the managers' days (D-5)
                    omit=("standing_missing",))
                result["review"]["setup"] = _setup_r["items"]
                result["review"]["lines"].extend(_setup_r["lines"])
            except Exception as _stx:
                print(f"[schedule] setup review failed: {_stx}")
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
            # The budget is a ceiling. A week written past it is named at
            # the top of the review, never trimmed into a thinner week.
            try:
                _hs = _hourly_hours_sum(preview_rows, _constraints)
                _hb = float(result.get("hours_budget") or 0)
            except (TypeError, ValueError):
                _hs = _hb = 0.0
            if _hb > 0 and _hs > _hb * 1.02:
                result["review"]["over_budget_hours"] = round(_hs - _hb, 1)
                result["review"]["lines"].insert(
                    0, f"⚠ {_hs:,.0f}h scheduled against a {_hb:,.0f}h budget — {_hs - _hb:,.0f}h over the ceiling")
                # Why the trim left it there — the shifts' requirement, days
                # kept from cuts, the rules — said once, beside it (SQ-7).
                _bc = result.get("budget_conflict")
                if _bc:
                    result["review"]["budget_conflict"] = _bc
                    _bcl = _econ.budget_conflict_line(_bc, over_by=round(_hs - _hb, 1), budget=_hb)
                    if _bcl:
                        result["review"]["lines"].insert(1, _bcl)
            # Nights left over the section count because a cut would have
            # gone under a floor or broken a rule (P-29), named once.
            result["review"]["cap_floor_conflicts"] = result.get("cap_floor_conflicts") or []
            _cfl = cap_floor_conflict_line(result.get("cap_floor_conflicts") or [])
            if _cfl:
                result["review"]["lines"].append(_cfl)
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
                            _close_times, _role_close_buffers, constraints=_constraints, only_dates=_editable)
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
        # Everything the finished week does not meet — a manager minute, a
        # floor, a closer, a target or leader rule, an ask, a minimum, the
        # ceiling — read from the rows the owner sees (schedule audit
        # 10/3/26 PR-11). Eleven prompt instructions used to ask the model
        # to say these in a summary of three ten-word bullets, two per
        # slice, about its own draft before any repair; what was promised
        # was silently dropped. Computed from the week's final sweep and
        # score (rule_violations, quality), so it is saved with the review.
        try:
            import schedule_output as _so_unmet
            if isinstance(result.get("review"), dict):
                result["review"]["unmet"] = _so_unmet.unmet_items(
                    preview_rows, constraints=result.get("constraints"),
                    violations=result.get("rule_violations"), quality=result.get("quality"),
                    soft_requirements=result.get("soft_requirements"), hours_budget=result.get("hours_budget"),
                    station_gaps=(result.get("stations") or {}).get("gaps"),
                    owner_rules_unchecked=result["review"].get("owner_rules_unchecked"))
        except Exception as _ux:
            print(f"[schedule] unmet list failed: {_ux}")
            _ops.capture(_ux, job="schedule_unmet", context=f"restaurant_id={restaurant_id}")
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
            # Every model call this generation made — its full input and
            # answer — keyed to the week it produced, so the week can be
            # replayed against another model, effort or prompt (schedule
            # audit 10/3/26 PR-31, scripts/schedule_model_eval.py).
            try:
                import schedule_output as _so_link
                _so_link.link_calls(restaurant_id, _history_id, result.get("generation_id"))
            except Exception as _lx:
                print(f"[schedule] model calls not linked to history {_history_id}: {_lx}")
                _ops.capture(_lx, job="schedule_model_calls", context=f"restaurant_id={restaurant_id}")
            # A whole week built discharges "next week's schedule isn't
            # built"; a partial redo of a few days does not.
            if _history_id and not _pinned:
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
            # The managers' shifts planned before the draft (schedule_skeleton):
            # each with why, each date's manager window, the stretches nobody
            # could legally cover, and the owner's question about managers
            # whose days are unknown.
            manager_plan=_skeleton.payload(result.get("manager_plan")),
            # The manager rule's backstop: what it covered, each stretch it
            # could not with why each manager could not, and the week's
            # shortfall (E-12, E-13); and who is still under their minimum
            # hours (P-4).
            manager_coverage=result.get("manager_coverage") or {"extended": 0, "added": 0, "left": []},
            min_hours=result.get("min_hours") or {"moved": 0, "added": 0, "left": []},
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
                                   "payload": dict(_payload, gate={"ran": True, **_gate})},
                        instruction=instruction)
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
        # The owner gets a sentence, never the exception or the traceback.
        msg = str(e) if isinstance(e, ScheduleGenerationError) else \
            "The schedule couldn't be generated just now — please try again in a minute."
        _ops.finish_async_job(job_id, "error", {"ok": False, "error": msg})


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


def prepare_replacements(restaurant_id, rows: list, constraints=None) -> dict:
    """What every replacement check against one week of rows shares, built
    once: the Constraints, the week's breach profile as it stands, and the
    swap index over the availability map. The replacement picker asks the
    question for every roster member, and each check used to re-sweep the
    unchanged week and re-read the availability table — about 128 sweeps
    and 64 reads per tap for a 64-person roster (schedule audit 10/3/26
    P-40). Pass the result to replacement_is_legal(..., prepared=)."""
    import shift_quality as _sq
    from models import get_unavailability_map
    c = constraints
    if c is None:
        dates = sorted({r.get("date") for r in rows if r.get("date")})
        c = _rules.build_constraints(restaurant_id, dates, [_weekday_name(d) for d in dates])
    availability = get_unavailability_map(restaurant_id, week_dates=list(c.week_dates or []) or None)
    return {"rows": rows, "constraints": c, "before": _rules.breach_profile(rows, c),
            "swap": _sq._SwapIndex(rows, availability, {}, _rules_for_swaps(c))}


def _breach_reason(worse: dict) -> str:
    """A regression (schedule_rules.regressions) as the sweep's own words:
    the rule's label, with the breach's detail when it says more."""
    kind = (worse.get("id") or ("",))[0]
    label = _rules.LABELS.get(kind, kind)
    detail = worse.get("label")
    return label if detail in (None, "", label) else f"{label} ({detail})"


def replacement_is_legal(restaurant_id, rows: list, index: int, name: str, constraints=None, prepared=None):
    """(ok, reason): could `name` take rows[index] outright, by every rule
    the generation itself is checked against? One answer for the web and
    iOS replacement pickers and the open-shift claim.

    The reason is the violation sweep's own label wherever one applies: the
    move is tried on a copy of the week and any hard rule it would newly
    break or make worse — a minor past the latest end, a certification the
    role needs, the person's hours window, rest, the hours ceiling, and the
    day's own rules: a stretch with no manager on the floor, no closer until
    close — is the answer (SCHED-6). Breaches are compared by what they are
    about (schedule_rules.breach_profile / regressions), never by the row
    they are pinned to: a staffing gap the week already had on the shift
    stays the week's, not the newcomer's (employee audit B3, H2), and a new
    gap pinned to a row that already carried one is no longer invisible —
    handing a manager's shift to somebody who does not manage, on a day that
    already had a short gap, used to pass (schedule audit 10/3/26 E-1). A
    double that does not overlap is allowed (SCHED-34). A free-text note on
    file is not read as a refusal: the engine cannot read it, and treating
    every note as "never" locked anyone with a compliment on file out of
    every claim (SCHED-35). The automatic swap search still leaves noted
    people alone.

    `constraints` lets a caller that already built the week's
    schedule_rules.Constraints pass it in; `prepared` (prepare_replacements,
    for these same rows) lets a caller checking many people against one week
    sweep it and read availability once (P-40)."""
    try:
        if prepared is None or prepared.get("rows") is not rows:
            prepared = prepare_replacements(restaurant_id, rows, constraints)
        c = prepared["constraints"]
        row = rows[index]
        ok, why = c.can_work(name, row.get("date", ""), _rules.daypart_of(row.get("shift_start", "")))
        if not ok:
            return False, why
        trial = [dict(r) for r in rows]
        trial[index]["employee"] = name
        worse = _rules.regressions(prepared["before"], _rules.breach_profile(trial, c), upto=_rules.TIER_COVERAGE)
        if worse:
            return False, _breach_reason(worse[0])
        if not prepared["swap"].replacement_legal(index, name, allow_double=True):
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
    # Each person's weekly maximum as the rules compute it (max_hours: their
    # own maximum above the ceiling allows them the hours, P-12), and for a
    # salaried person the weekly cap code holds them to (salaried_limit,
    # E-17) — one answer for the sweep and every swap.
    names = set(c.roster_names or []) | set(c.hours_limits or {}) | set(c.salaried or ())
    caps = {}
    for n in names:
        try:
            caps[str(n).strip().lower()] = float(c.salaried_limit(n) if c.is_salaried(n) else c.max_hours(n))
        except (TypeError, ValueError):
            continue
    return {
        "blocked_dates": c.blocked_dates, "daypart_avail": c.daypart_avail, "caps": caps,
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
    """{weekday: {hour: share of the day's sales}}, measured on at least
    three same-weekday days — schedule_economics.hourly_profile: the POS's
    ticket archive by business date (the hour each ticket opened; an hour
    after midnight is 24+), with the intraday captures and the nightly
    report's hourly split for the dates the archive does not hold. It read
    the intraday captures alone while the 90-day archive sat unread, so a
    restaurant like Simple EJ's had no half-hour curve for weeks (schedule
    audit 10/3/26 D-26)."""
    import schedule_economics as _econ
    return _econ.hourly_profile(restaurant_id)