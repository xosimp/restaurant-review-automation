"""
schedule_economics.py — the money side of a generated week, deterministically.

The budget used to be a ceiling in words only: the model could write 140
hours past it and nothing took them back, overtime was priced straight,
the weekly revenue behind the budget was a flat twelfth of a monthly
target, and the sales curve inside a day was a fact stated to the model
and never used. This module owns:

  trim_to_budget      — take the most discretionary hours out until the week
                        fits: tails first (the close stagger), then whole
                        shifts; never a rule breach, never under a shift's
                        requirement, never a day cuts measured worse on;
                        every change reported, a conflict said once
  priced_cost         — a week's dollars with hours over the ceiling at the
                        overtime multiplier
  projected_weekly_revenue — the restaurant's own weekly pattern, not a
                        twelfth of a month
  splh_by_daypart     — sales per labor hour by weekday and daypart
  splh_objective      — the SPLH target per weekday and daypart the draft
                        aims for and the scorer's `splh` dimension judges
  holiday_lift        — what a holiday did to THIS restaurant's sales last time
  stagger_same_starts — spread identical starts along the day's sales curve
  cost_delta          — what an edit moves in hours and dollars

Everything here is pure over the rows it is handed, except the readers that
say so. Nothing calls a model.
"""
from datetime import date, datetime, timedelta

from models import get_conn, DB_PATH
# Final days only (canonical_facts, memory audit 9/29/26): a half-night the
# POS had not closed is never a week's sales or a day's labor %.
from canonical_facts import FINAL_SQL

TRIM_TOLERANCE = 0.02            # a week within 2% of the budget is not trimmed
TRIM_MAX_REMOVALS = 60
TRIM_SCORE_CANDIDATES = 5       # how many equally-discretionary rows the score chooses between
TRIM_SCORE_SECONDS = 8.0        # past this the score stops choosing; the order alone decides (big rosters)
# The close stagger (schedule audit 10/3/26 P-27): the end of a shift goes
# before a whole shift does — at most this much off one shift, in half hours,
# never leaving it shorter than a four-hour shift (the overtime trim's floor).
TRIM_TAIL_MAX_HOURS = 2.0
TRIM_TAIL_STEP_MIN = 30
TRIM_MIN_SHIFT_HOURS = 4.0
TRIM_MAX_TAIL_CUTS = 120
TRIM_MAX_SECONDS = 20.0         # past this the trim stops where it is and says what is left over
STAGGER_STEP_MIN = 30
STAGGER_MAX_MIN = 90

_COLS = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")


def _minutes(t):
    from schedule_rules import parse_minutes
    return parse_minutes(t)


def _fmt(m):
    from schedule_engine import _format_minutes_to_time
    return _format_minutes_to_time(int(m))


def _hours(r):
    try:
        return float(r.get("scheduled_hours") or 0)
    except (TypeError, ValueError):
        return 0.0


def _daypart(r):
    from schedule_rules import daypart_of
    return daypart_of(r.get("shift_start", ""))


# ── overtime-priced cost ───────────────────────────────────────────────────

def priced_cost(rows: list, role_rates: dict, blended_rate: float, ceiling: float = 40.0,
                base_hours: dict = None, multiplier: float = 1.5, bucket=None, daily_ot_hours: float = None,
                salaried=None, person_rates=None, role_typical=None) -> dict:
    """Dollars for the week with every overtime hour at the multiplier.

    Weekly overtime is counted per PAYROLL week: `bucket(date)` names the
    payroll week a date falls in (schedule_rules.Constraints.bucket), and
    base_hours is {name: {bucket: hours already published}} — or a plain
    {name: hours} when there is one bucket. A Monday-Sunday draft over a
    Wednesday payroll week is two payroll weeks, and pricing it as one read
    26h of overtime where there were 2 (SCHED-7). Those base hours are not
    priced here but they push this week's hours into overtime sooner.

    daily_ot_hours: where daily overtime applies, the hours past it in one
    day are overtime too — flagged by the sweep and, until now, never priced
    (SCHED-7). An hour already paid as daily overtime does not also count
    toward the weekly ceiling.

    salaried: names (models.salaried_name_key) paid the same whatever the
    hours — their shifts add no hourly dollars and no overtime.

    person_rates / role_typical (labor.person_rate_book): what each person
    is paid an hour, and a role's typical rate. A drafted cook's shift costs
    his own $22, not the $26 blended rate a role with no rate set fell to;
    the owner's role rate still beats the typical one."""
    sal = {" ".join(str(n or "").lower().split()) for n in (salaried or ())}
    rates = {str(k).strip().lower(): float(v) for k, v in (role_rates or {}).items() if k and k != "_default"}
    blended = float(blended_rate or 0) or (sum(rates.values()) / len(rates) if rates else 0.0)
    try:
        daily = float(daily_ot_hours or 0)
    except (TypeError, ValueError):
        daily = 0.0
    per_person = {}
    for r in rows or []:
        n = (r.get("employee") or "").strip()
        if not n or " ".join(n.lower().split()) in sal:
            continue
        role = (r.get("role") or "").strip().lower()
        rate = ((person_rates or {}).get(" ".join(n.lower().split())) or rates.get(role)
                or (role_typical or {}).get(role) or blended)
        per_person.setdefault(n, []).append((r.get("date") or "", _minutes(r.get("shift_start", "")) or 0, _hours(r), rate))
    straight = premium = ot_hours = 0.0
    for n, items in per_person.items():
        items.sort()
        base = (base_hours or {}).get(n.lower(), 0) or 0
        if isinstance(base, dict):
            if bucket is None:
                so_far = {"": float(sum(float(v or 0) for v in base.values()))}
            else:
                so_far = {k: float(v or 0) for k, v in base.items()}
        else:
            so_far = {"": float(base)}
        day_used = {}
        for d, _s, h, rate in items:
            daily_ot = 0.0
            if daily > 0:
                used = day_used.get(d, 0.0)
                daily_ot = max(0.0, h - max(0.0, daily - used))
                day_used[d] = used + h
            weekly_part = h - daily_ot
            key = ""
            if bucket is not None and d:
                try:
                    key = bucket(d) or ""
                except Exception:
                    key = ""
            have = so_far.get(key, 0.0)
            room = max(0.0, float(ceiling or 0) - have) if ceiling else weekly_part
            reg = min(weekly_part, room)
            ot = daily_ot + (weekly_part - reg)
            straight += h * rate
            premium += ot * rate * (multiplier - 1.0)
            ot_hours += ot
            so_far[key] = have + weekly_part
    return {"straight": round(straight, 0), "overtime_premium": round(premium, 0), "overtime_hours": round(ot_hours, 1),
            "total": round(straight + premium, 0), "multiplier": multiplier}


def cost_delta(before_rows: list, after_rows: list, role_rates: dict, blended_rate: float, ceiling: float = 40.0) -> dict:
    """What an edit moves: hours and overtime-priced dollars, before → after."""
    a = priced_cost(before_rows, role_rates, blended_rate, ceiling)
    b = priced_cost(after_rows, role_rates, blended_rate, ceiling)
    hb = round(sum(_hours(r) for r in before_rows or []), 1)
    ha = round(sum(_hours(r) for r in after_rows or []), 1)
    return {"hours_before": hb, "hours_after": ha, "hours_delta": round(ha - hb, 1),
            "dollars_before": a["total"], "dollars_after": b["total"], "dollars_delta": round(b["total"] - a["total"], 0),
            "overtime_hours_after": b["overtime_hours"]}


# ── weekly revenue from the restaurant's own pattern ──────────────────────

# The week's own budget is the projection once the owner has budgeted this
# many of its nights in the DSR (memory audit 9/29/26, owner_goals).
BUDGET_MIN_NIGHTS = 5


def budgeted_week_revenue(restaurant_id, week_dates, db_path=DB_PATH) -> dict:
    """{"value", "source", "nights"} from the owner's own nightly budgets
    (dsr_budgets, net, else gross) for the week being scheduled, when at
    least BUDGET_MIN_NIGHTS of its nights are budgeted: the owner who
    budgets a record festival week in the DSR had the schedule's hours
    budget built from last month's median week. A night not budgeted is
    filled from the restaurant's own median for that weekday (said in the
    source); None value when too few nights are budgeted."""
    dates = [str(d)[:10] for d in week_dates or () if d]
    if not dates:
        return {"value": None, "source": None, "nights": 0}
    try:
        from dsr import store as _dsr_store
        got = _dsr_store.budgets_for(restaurant_id, min(dates), max(dates), db_path=db_path)
    except Exception:
        return {"value": None, "source": None, "nights": 0}
    per_night = {}
    for d in dates:
        b = got.get(d) or {}
        v = b.get("net") if b.get("net") not in (None, "") else b.get("gross")
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            per_night[d] = v
    if len(per_night) < BUDGET_MIN_NIGHTS:
        return {"value": None, "source": None, "nights": len(per_night)}
    total = sum(per_night.values())
    missing = [d for d in dates if d not in per_night]
    filled = 0
    if missing:
        medians = _weekday_medians(restaurant_id, db_path=db_path)
        for d in missing:
            try:
                wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
            except ValueError:
                continue
            if medians.get(wd):
                total += medians[wd]
                filled += 1
    src = f"your budget ({len(per_night)} of {len(dates)} nights budgeted"
    src += (f"; {filled} filled from your usual {'night' if filled == 1 else 'nights'})" if filled else ")")
    return {"value": round(total, 0), "source": src, "nights": len(per_night)}


def _weekday_medians(restaurant_id, weeks: int = 8, db_path=DB_PATH) -> dict:
    """{weekday: median daily sales} over the last `weeks` weeks."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT day_of_week, sales FROM labor_daily_history WHERE restaurant_id=? AND sales IS NOT NULL "
            f"AND sales > 0 AND date >= date('now', ?) AND {FINAL_SQL}",
            (restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    by = {}
    for r in rows:
        by.setdefault(str(r["day_of_week"] or "").capitalize(), []).append(float(r["sales"] or 0))
    out = {}
    for wd, vals in by.items():
        vals.sort()
        n = len(vals)
        out[wd] = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return out


def _corrected_by_record(restaurant_id, out, raw, db_path=DB_PATH):
    """Apply the published weeks' own record (forecast_log kind
    revenue_week) to `raw` — ONLY for the estimator that record scores:
    demand.week_projection, frozen at publish (demand.freeze_week_projection).
    When those projections have leaned one way the figure is corrected by
    the same factor and the source says so; when the record reads often
    wide it is said, since the budget still needs a figure. The frozen
    projection stays raw, so the correction never feeds on itself."""
    try:
        import forecast_log
        rec = forecast_log.shown(restaurant_id, "revenue_week", raw, db_path=db_path)
        if rec.get("corrected") and rec.get("shown") is not None:
            out.update(value=round(float(rec["shown"]), 0),
                       calibration={"factor": rec["factor"], "bias_pct": rec.get("bias_pct"),
                                    "reading": rec.get("reading")})
            out["source"] += (f", corrected {'down' if rec['factor'] < 1 else 'up'} "
                              f"{abs(round((1 - rec['factor']) * 100))}% because the published weeks' "
                              f"projections here {rec.get('reading')}")
        elif rec.get("withheld"):
            out["source"] += "; the published weeks' projections here have often been wide, so treat it as rough"
    except Exception as e:
        print(f"[schedule_economics] revenue record unreadable for {restaurant_id}: {e}")
    return out


def projected_weekly_revenue(restaurant_id, weeks: int = 8, db_path=DB_PATH, week_dates=None) -> dict:
    """{"value", "source", "weeks", "estimator"} — the week's own budget
    when the owner has budgeted at least BUDGET_MIN_NIGHTS of `week_dates`
    in the DSR (budgeted_week_revenue, source "your budget …"); else, for a
    named week every day of which has a forecast, the week's day-by-day
    projection (demand.week_projection: each weekday's median with the
    measured events on its dates — the figure frozen at publish and scored
    when the week closes), corrected by that record when it leans; else the
    median of the last `weeks` complete weeks of daily sales, raw, so the
    budget follows how this restaurant actually earns rather than a twelfth
    of a monthly target. None when none exists (fewer than three complete
    weeks).

    The revenue_week record corrects only the estimator it measured (PRED-4,
    memory fix round integration 9/29/26): it used to be applied to the
    median-week figure, a different estimate whose own lean nobody scored."""
    if week_dates:
        own = budgeted_week_revenue(restaurant_id, week_dates, db_path=db_path)
        if own.get("value"):
            return {"value": own["value"], "source": own["source"], "weeks": 0, "budget_nights": own["nights"],
                    "estimator": "budget"}
        try:
            import demand
            proj = demand.week_projection(restaurant_id, week_dates, db_path=db_path)
        except Exception as e:
            print(f"[schedule_economics] week projection unavailable for {restaurant_id}: {e}")
            proj = {}
        if proj.get("total") and not proj.get("missing") and len(proj.get("days") or []) == len(week_dates):
            total = float(proj["total"])
            out = {"value": round(total, 0), "raw_value": round(total, 0), "calibration": None,
                   "source": ("the week's day-by-day projection (each weekday's typical night"
                              + (", with the measured events on its dates" if proj.get("modelled") else "") + ")"),
                   "weeks": 0, "estimator": "week_projection"}
            return _corrected_by_record(restaurant_id, out, total, db_path=db_path)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0 "
            f"AND date >= date('now', ?) AND {FINAL_SQL} ORDER BY date",
            (restaurant_id, f"-{int(weeks) * 7 + 7} days")).fetchall()
    except Exception:
        return {"value": None, "source": "no history", "weeks": 0}
    finally:
        conn.close()
    by_week = {}
    for r in rows:
        try:
            d = datetime.strptime(str(r["date"])[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        key = (d - timedelta(days=d.weekday())).isoformat()
        e = by_week.setdefault(key, {"sales": 0.0, "days": set()})
        e["sales"] += float(r["sales"] or 0)
        e["days"].add(d)
    complete = sorted((k, v["sales"]) for k, v in by_week.items() if len(v["days"]) >= 5)
    complete = complete[-weeks:]
    if len(complete) < 3:
        return {"value": None, "source": "fewer than three complete weeks on file", "weeks": len(complete)}
    vals = sorted(s for _, s in complete)
    med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    # Raw: the published weeks' record (revenue_week) scores the day-by-day
    # projection, not this median, so its lean is never applied here.
    return {"value": round(med, 0), "raw_value": round(med, 0), "calibration": None,
            "source": f"median of the last {len(complete)} complete weeks", "weeks": len(complete),
            "estimator": "median_weeks"}


# ── sales per labor hour by daypart ───────────────────────────────────────

def splh_by_daypart(restaurant_id, weeks: int = 8, db_path=DB_PATH) -> dict:
    """{weekday: {"morning": {sales, hours, splh}, "night": {...}}}.

    Daily sales and hours come from labor_daily_history; the split of each
    day's sales into dayparts comes from the intraday captures' share of
    the day before 3pm (when at least three same-weekday days were
    captured, else 40/60); the split of hours comes from the shift history
    for that weekday. A weekday with no sales or no hours is absent —
    never 0."""
    from models import _cached_shifts
    conn = get_conn(db_path)
    try:
        days = conn.execute(
            "SELECT date, day_of_week, sales, total_hours FROM labor_daily_history WHERE restaurant_id=? "
            f"AND sales IS NOT NULL AND sales > 0 AND date >= date('now', ?) AND {FINAL_SQL}",
            (restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
        intra = conn.execute(
            "SELECT weekday, business_date, captured_hour, net_sales FROM pos_intraday WHERE restaurant_id=? "
            "AND business_date >= date('now', ?) ORDER BY business_date, captured_hour",
            (restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    # morning share of the day's sales from cumulative intraday captures
    share = {}
    by_day = {}
    for r in intra:
        by_day.setdefault((r["weekday"], r["business_date"]), []).append((int(r["captured_hour"]), float(r["net_sales"] or 0)))
    tmp = {}
    for (wd, _bd), caps in by_day.items():
        caps.sort()
        total = caps[-1][1]
        if total <= 0:
            continue
        at3 = max((s for h, s in caps if h <= 15), default=None)
        if at3 is None:
            continue
        tmp.setdefault(wd, []).append(min(1.0, at3 / total))
    for wd, vals in tmp.items():
        if len(vals) >= 3:
            vals.sort()
            share[wd] = vals[len(vals) // 2]
    # hours split by daypart from the shift history, per weekday
    hrs = {}
    try:
        for sh in _cached_shifts(restaurant_id):
            wd = (sh.get("day") or "").strip().capitalize()
            if not wd:
                try:
                    wd = datetime.strptime(sh.get("date", ""), "%Y-%m-%d").strftime("%A")
                except ValueError:
                    continue
            # Filed under the daypart the row is mostly on the floor for
            # (shift_quality.present_dayparts), as the scorer's `splh`
            # dimension files a draft's hours — a 2pm-11pm cook is dinner.
            from shift_quality import present_dayparts
            part = present_dayparts(sh)[0]
            if part == "unknown":
                continue
            h = 0.0
            try:
                h = float(sh.get("scheduled_hours") or sh.get("hours") or 0)
            except (TypeError, ValueError):
                pass
            e = hrs.setdefault(wd, {"morning": 0.0, "night": 0.0})
            e[part] += h
    except Exception:
        pass
    out = {}
    sales_by_wd = {}
    for r in days:
        wd = (r["day_of_week"] or "").strip().capitalize()
        if not wd:
            continue
        e = sales_by_wd.setdefault(wd, {"sales": 0.0, "hours": 0.0, "n": 0})
        e["sales"] += float(r["sales"] or 0)
        e["hours"] += float(r["total_hours"] or 0)
        e["n"] += 1
    for wd, e in sales_by_wd.items():
        if e["n"] < 2 or e["sales"] <= 0:
            continue
        m_share = share.get(wd, 0.4)
        hsplit = hrs.get(wd)
        if hsplit and (hsplit["morning"] + hsplit["night"]) > 0:
            h_m = hsplit["morning"] / (hsplit["morning"] + hsplit["night"])
        else:
            h_m = 0.4
        total_hours = e["hours"] if e["hours"] > 0 else None
        if not total_hours:
            continue
        day = {}
        for part, s_share, h_share in (("morning", m_share, h_m), ("night", 1 - m_share, 1 - h_m)):
            s = e["sales"] / e["n"] * s_share
            h = total_hours / e["n"] * h_share
            if h > 0:
                day[part] = {"sales": round(s, 0), "hours": round(h, 1), "splh": round(s / h, 0),
                             # whether the day's sales were split by measured
                             # intraday readings or the 40/60 default
                             "sales_split": "measured" if wd in share else "assumed"}
        if day:
            out[wd] = day
    return out


def splh_block(splh: dict) -> str:
    if not splh:
        return ""
    lines = []
    for wd in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        d = splh.get(wd)
        if not d:
            continue
        bits = [f"{p} ${v['splh']:,.0f}/labor-hour" for p, v in d.items()]
        lines.append(f"  {wd}: " + ", ".join(bits))
    if not lines:
        return ""
    return ("\n\nSALES PER LABOR HOUR BY DAYPART (this restaurant's own recent record — a daypart well "
            "below the others is where hours are being spent for the least return; add there last and "
            "trim there first):\n" + "\n".join(lines))


# ── sales per labor hour as the objective ─────────────────────────────────
#
# splh_by_daypart used to steer only which rows the budget trim cut first.
# The objective states a target for every weekday's lunch and dinner, from
# the restaurant's own record, raised to meet its labor target when it runs
# over it; the prompt is told the hours each shift's usual sales carry at
# that target, the scorer's `splh` dimension judges the draft against it,
# and the review says how the draft landed. Per weekday, not one figure per
# daypart: a Monday's minimum crew can never reach a Saturday's sales per
# hour, and a pooled target marked every quiet shift down for existing. No
# sales record: no target, and the dimension withdraws.

SPLH_MIN_WEEKDAYS = 3            # weekdays with a measured SPLH before a target is set


def _dp_label(part):
    return "lunch" if part == "morning" else "dinner"


def splh_objective(restaurant_id, splh: dict = None, labor_target_pct=None, weeks: int = 8, db_path=DB_PATH) -> dict:
    """{available, by_day: {weekday: {daypart: target}}, targets: {daypart:
    {target, history, source}} (the week's figure per daypart, for display),
    daypart_sales: {weekday: {daypart: sales}}, labor_target_pct,
    history_labor_pct, scale, basis, split_assumed} or {available: False,
    reason}.

    Each weekday's daypart target is its own recent sales per labor hour.
    When the restaurant has been running over its labor % target, every
    target is raised by the same factor — history labor % ÷ target labor % —
    which is exactly the productivity the target needs at the wages it
    actually paid (labor % = wage × hours ÷ sales). Under target, history
    stands: the objective never asks for more hours than the record ran."""
    splh = splh_by_daypart(restaurant_id, weeks=weeks, db_path=db_path) if splh is None else splh
    if not splh:
        return {"available": False, "reason": "No daily sales and hours on file yet, so there is no sales-per-labor-hour target."}
    tot = {"morning": [0.0, 0.0, 0], "night": [0.0, 0.0, 0]}
    daypart_sales, hist_by_day = {}, {}
    for wd, parts in splh.items():
        for part, v in (parts or {}).items():
            if part not in tot or not v or not v.get("hours") or not v.get("sales"):
                continue
            tot[part][0] += float(v["sales"])
            tot[part][1] += float(v["hours"])
            tot[part][2] += 1
            daypart_sales.setdefault(wd, {})[part] = float(v["sales"])
            hist_by_day.setdefault(wd, {})[part] = float(v["sales"]) / float(v["hours"])
    have = {p: t for p, t in tot.items() if t[2] >= SPLH_MIN_WEEKDAYS and t[1] > 0}
    if not have:
        return {"available": False,
                "reason": f"Sales per labor hour needs at least {SPLH_MIN_WEEKDAYS} weekdays of sales and hours per daypart."}
    hist_pct = None
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT SUM(labor_pct * sales) AS w, SUM(sales) AS s FROM labor_daily_history WHERE restaurant_id=? "
                           f"AND sales > 0 AND labor_pct IS NOT NULL AND date >= date('now', ?) AND {FINAL_SQL}",
                           (restaurant_id, f"-{int(weeks) * 7} days")).fetchone()
        if row and row["s"]:
            hist_pct = float(row["w"]) / float(row["s"])
    except Exception as e:
        print(f"[splh] labor % history unavailable for {restaurant_id}: {e}")
    finally:
        conn.close()
    if labor_target_pct is None:
        try:
            from models import get_restaurant
            from notify import labor_target_for
            labor_target_pct = labor_target_for(get_restaurant(restaurant_id))
        except Exception:
            labor_target_pct = None
    scale = 1.0
    if hist_pct and labor_target_pct and float(labor_target_pct) > 0 and hist_pct > float(labor_target_pct):
        scale = hist_pct / float(labor_target_pct)
    by_day = {wd: {p: round(v * scale, 0) for p, v in parts.items() if p in have} for wd, parts in hist_by_day.items()}
    by_day = {wd: parts for wd, parts in by_day.items() if parts}
    targets = {}
    for part, (s, h, _n) in have.items():
        hist = s / h
        if scale > 1:
            src = (f"your own recent pace on each day's {_dp_label(part)}, raised {int(round((scale - 1) * 100))}% to meet "
                   f"your {float(labor_target_pct):g}% labor target (you have run {hist_pct:.1f}%)")
        elif hist_pct and labor_target_pct:
            src = (f"your own recent pace on each day's {_dp_label(part)} — already inside your "
                   f"{float(labor_target_pct):g}% labor target")
        else:
            src = f"your own recent pace on each day's {_dp_label(part)} over the last {weeks} weeks"
        targets[part] = {"target": round(hist * scale, 0), "history": round(hist, 0), "source": src}
    basis = "; ".join(f"{_dp_label(p).capitalize()} ${v['target']:,.0f} per labor hour across the week — {v['source']}"
                      for p, v in sorted(targets.items()))
    assumed = sorted({wd for wd, parts in splh.items() for v in (parts or {}).values()
                      if (v or {}).get("sales_split") == "assumed"})
    if assumed:
        basis += (f". Lunch and dinner sales are split 40/60 on {len(assumed)} weekday{'s' if len(assumed) != 1 else ''} "
                  "with no intraday sales readings yet")
    return {"available": True, "by_day": by_day, "targets": targets, "daypart_sales": daypart_sales,
            "labor_target_pct": float(labor_target_pct) if labor_target_pct else None,
            "history_labor_pct": round(hist_pct, 1) if hist_pct else None, "scale": round(scale, 3),
            "basis": basis, "split_assumed": bool(assumed)}


def splh_target_for(objective: dict, weekday: str, part: str):
    """The target for one weekday's daypart: its own, else the week's."""
    t = ((objective.get("by_day") or {}).get(weekday) or {}).get(part)
    return t or ((objective.get("targets") or {}).get(part) or {}).get("target")


def _shift_sales(objective, weekday, part, date, demand_by_date):
    s = ((objective.get("daypart_sales") or {}).get(weekday) or {}).get(part)
    if not s:
        return None
    lift = ((demand_by_date or {}).get(date) or {}).get("lift_pct")
    try:
        return float(s) * (1 + float(lift) / 100.0) if lift else float(s)
    except (TypeError, ValueError):
        return float(s)


def splh_objective_block(objective: dict, dates: list, demand_by_date: dict = None) -> str:
    """The prompt's objective: for each shift of the week, the sales per
    labor hour it aims for and the labor hours its usual sales carry there."""
    if not objective or not objective.get("available"):
        return ""
    from time_utils import mdy
    lines = []
    for d in dates or []:
        try:
            wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except ValueError:
            continue
        bits = []
        for part in ("morning", "night"):
            t = splh_target_for(objective, wd, part)
            s = _shift_sales(objective, wd, part, d, demand_by_date)
            if not t or not s:
                continue
            bits.append(f"{_dp_label(part)} ${t:,.0f}/labor-hour ≈ {s / float(t):.0f}h")
        if bits:
            lines.append(f"  {wd[:3]} {mdy(d)}: " + ", ".join(bits))
    if not lines:
        return ""
    return ("\n\nSALES PER LABOR HOUR — the productivity objective, ranked below the shift requirements ("
            + objective.get("basis", "") + "). Each shift's target, and the labor hours its usual sales carry at that "
            "target (hours counted under the daypart each shift is mostly on the floor for). Staff to the requirements "
            "table first; past it, add hours where they carry the most sales and hold the rest near these figures:\n"
            + "\n".join(lines))


def splh_report(objective: dict, rows: list, demand_by_date: dict = None) -> dict:
    """How a draft lands against the objective, per shift and per daypart
    for the week — what the review panel reports. {} without a target."""
    if not objective or not objective.get("available") or not rows:
        return {}
    from shift_quality import present_dayparts
    from time_utils import mdy
    hours = {}
    for r in rows:
        d = (r.get("date") or "").strip()
        if not d or not (r.get("employee") or "").strip():
            continue
        part = present_dayparts(r)[0]
        hours[(d, part)] = hours.get((d, part), 0.0) + _hours(r)
    shifts, week = [], {}
    for (d, part), h in sorted(hours.items()):
        try:
            wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except ValueError:
            continue
        t = splh_target_for(objective, wd, part)
        s = _shift_sales(objective, wd, part, d, demand_by_date)
        if not t or not s or h <= 0:
            continue
        v = s / h
        shifts.append({"date": d, "day": wd, "daypart": part, "hours": round(h, 1), "expected_sales": round(s, 0),
                       "splh": round(v, 0), "target": t, "hours_at_target": round(s / t, 1),
                       "under": v < t * 0.95})
        w = week.setdefault(part, [0.0, 0.0, 0.0])
        w[0] += s
        w[1] += h
        w[2] += s / t
    if not shifts:
        return {}
    by_part = {p: {"splh": round(s / h, 0), "target": round(s / at, 0), "hours": round(h, 1), "hours_at_target": round(at, 1)}
               for p, (s, h, at) in week.items() if h > 0 and at > 0}
    worst = min(shifts, key=lambda x: x["splh"] / float(x["target"]))
    parts = [f"{_dp_label(p)} ${v['splh']:,.0f} against ${v['target']:,.0f}" for p, v in sorted(by_part.items())]
    line = "Sales per labor hour across the week: " + ", ".join(parts) + "."
    if worst["under"]:
        line += (f" Furthest under: {worst['day'][:3]} {mdy(worst['date'])} {_dp_label(worst['daypart'])} at "
                 f"${worst['splh']:,.0f} against ${worst['target']:,.0f}, about "
                 f"{worst['hours'] - worst['hours_at_target']:.0f}h more than its usual sales carry.")
    return {"by_daypart": by_part, "shifts": shifts, "under": [x for x in shifts if x["under"]],
            "line": line, "basis": objective.get("basis")}


# ── holidays: what they did here last time ────────────────────────────────

def _holiday_dates(year: int) -> dict:
    """{iso date: name} — the dining holidays marketing.get_upcoming_holidays
    already knows, resolved to one year."""
    from datetime import date as _date
    fixed = {(1, 1): "New Year's Day", (2, 14): "Valentine's Day", (3, 17): "St. Patrick's Day",
             (5, 5): "Cinco de Mayo", (7, 4): "Fourth of July", (10, 31): "Halloween",
             (11, 11): "Veterans Day", (12, 24): "Christmas Eve", (12, 25): "Christmas Day",
             (12, 31): "New Year's Eve"}
    out = {_date(year, m, d).isoformat(): n for (m, d), n in fixed.items()}

    def nth_weekday(month, weekday, n):
        first = _date(year, month, 1)
        off = (weekday - first.weekday()) % 7
        return first + timedelta(days=off + 7 * (n - 1))

    def last_weekday(month, weekday):
        nxt = _date(year + (month == 12), (month % 12) + 1, 1)
        d = nxt - timedelta(days=1)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    out[nth_weekday(5, 6, 2).isoformat()] = "Mother's Day"
    out[nth_weekday(6, 6, 3).isoformat()] = "Father's Day"
    out[nth_weekday(11, 3, 4).isoformat()] = "Thanksgiving"
    out[last_weekday(5, 0).isoformat()] = "Memorial Day"
    out[nth_weekday(9, 0, 1).isoformat()] = "Labor Day"
    out[nth_weekday(2, 6, 2).isoformat()] = "Super Bowl Sunday"
    # Easter (Anonymous Gregorian algorithm)
    a, b, c = year % 19, year // 100, year % 100
    d_, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d_ - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    out[_date(year, month, day).isoformat()] = "Easter"
    return out


def holiday_lift(restaurant_id, week_dates: list, db_path=DB_PATH) -> dict:
    """{date: {"name", "lift_pct", "based_on", "date", "source"}} for holidays
    in the week, with the lift THIS restaurant saw on that holiday last year
    against the median of the same weekday in the four weeks either side. A
    holiday with no sales on file last year is listed with lift None — a
    name the model can react to, never a number it did not measure.

    Last year is read through the ONE last-year reader
    (canonical_facts.sales_history: the night's report, the owner's imported
    DSR workbooks, the POS sync's final nights — memory audit 9/29/26,
    imported_year), so an owner who imported a year of workbooks has a
    holiday lift from day one. The holiday night and the nights it is
    compared with are on ONE basis (canonical_facts.one_basis), and
    `source` / `based_on` say where last year came from."""
    import canonical_facts as _cf
    if not week_dates:
        return {}
    years = {int(d[:4]) for d in week_dates}
    names = {}
    for y in years:
        names.update(_holiday_dates(y))
    hits = {d: names[d] for d in week_dates if d in names}
    if not hits:
        return {}
    out = {}
    try:
        for d, name in hits.items():
            last = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=364)).date()   # same weekday last year
            # the holiday's own date last year, whichever weekday it fell on
            prior_dates = [k for k, n in _holiday_dates(last.year).items() if n == name]
            hol_date = prior_dates[0] if prior_dates else last.isoformat()
            # `date`: the night last year's figure is read from, so a reader
            # can name ITS weekday (re-audit OPP-2).
            entry = {"name": name, "lift_pct": None, "based_on": None, "date": hol_date, "source": None}
            hd = datetime.strptime(hol_date, "%Y-%m-%d").date()
            series = _cf.sales_history(restaurant_id, (hd - timedelta(days=28)).isoformat(),
                                       (hd + timedelta(days=28)).isoformat(), db_path=db_path)
            night = series.get(hol_date)
            if night and night["net"]:
                same = {k: x for k, x in series.items() if k != hol_date and x.get("basis") == night.get("basis")
                        and datetime.strptime(k, "%Y-%m-%d").date().weekday() == hd.weekday()}
                vals = sorted(float(x["net"]) for x in same.values() if x["net"] and x["net"] > 0)
                if len(vals) >= 3:
                    med = vals[len(vals) // 2]
                    if med > 0:
                        entry["lift_pct"] = int(round((float(night["net"]) / med - 1) * 100))
                        entry["source"] = night.get("source")
                        entry["based_on"] = (f"{name} {hd.year}: ${float(night['net']):,.0f} against a typical "
                                             f"{hd.strftime('%A')} of ${med:,.0f}")
                        # The POS archive is the ordinary source; an imported
                        # workbook or a nightly report is named.
                        if night.get("source") in ("import", "dsr"):
                            entry["based_on"] += f", from {_cf.SOURCE_LABELS[night['source']]}"
            out[d] = entry
    except Exception:
        return {d: {"name": n, "lift_pct": None, "based_on": None} for d, n in hits.items()}
    return out


# ── staggered starts along the day's curve ────────────────────────────────

def stagger_same_starts(rows: list, hourly_profile: dict, min_group: int = 3, score_fn=None,
                        score_seconds: float = TRIM_SCORE_SECONDS, constraints=None, only_dates=None,
                        requirements=None) -> tuple:
    """When three or more people in the same role start at the same minute
    on a day whose sales curve is still climbing, keep the first and move
    the others later in 30-minute steps (at most 90), ending where they
    did. Only with a measured curve for that weekday, and never past the
    hour the curve peaks. Returns (rows, changes:[...]).

    score_fn(rows) -> float, when given, chooses each move's size among the
    legal ones (the planned step, each shorter step, or none): the one that
    costs the week the least Shift Quality, the planned step on a tie. A
    stagger that would leave a half hour the sales curve needs short backs
    off instead of opening the gap.

    A role is its family ("Server AM" starts with "Server"). A move is
    legal only when it leaves no shift further under its requirement
    (`requirements`, the SHIFT REQUIREMENTS rows with their half-hour ramp)
    and, with `constraints`, makes no rule breach about the day new or
    worse — a manager's later start opening a stretch with no manager on, a
    floor short at the open (schedule audit 10/3/26 E-1). A row the owner
    kept ("_pinned") is never moved, nor is any row on a day outside
    `only_dates` (a partial redo; P-10)."""
    if not hourly_profile:
        return rows, []
    import time as _time
    import schedule_engine as _se
    _t0 = _time.monotonic()
    c = constraints
    only = set(only_dates) if only_dates else None
    index = _se.requirement_index(requirements, c) if requirements else {}
    skip = _se._will_not_stand(rows, c) if (c is not None and index) else set()
    groups = {}
    for i, r in enumerate(rows):
        key = (r.get("date"), _se._role_key(r.get("role"), c), r.get("shift_start"))
        if all(key):
            groups.setdefault(key, []).append(i)
    changes = []
    for (date, role, start), idxs in groups.items():
        if len(idxs) < min_group:
            continue
        if only is not None and date not in only:
            continue
        try:
            wd = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue
        curve = hourly_profile.get(wd) or {}
        if not curve:
            continue
        s = _minutes(start)
        if s is None:
            continue
        peak_hour = max(curve.items(), key=lambda kv: kv[1])[0]
        try:
            peak_hour = int(peak_hour)
        except (TypeError, ValueError):
            continue
        if peak_hour * 60 <= s + STAGGER_STEP_MIN:
            continue          # already at or past the peak: everyone is needed now
        for n, i in enumerate(idxs[1:], start=1):
            off = min(n * STAGGER_STEP_MIN, STAGGER_MAX_MIN, peak_hour * 60 - s)
            if off <= 0:
                break
            r = rows[i]
            if not _se._changeable(r, only):
                continue      # kept by the owner: it still counts, it never moves
            e = _minutes(r.get("shift_end", ""))
            sp = _se._span_minutes(r)
            if e is None or not sp or sp[1] <= s + off + 120:
                continue      # would leave under two hours: not worth it
            end_m = sp[1]

            def _moved(step):
                return dict(r, shift_start=_fmt(s + step), scheduled_hours=str(round((end_m - (s + step)) / 60, 1)))

            def _legal(step):
                trial = list(rows)
                trial[i] = _moved(step)
                if index and _se.requirement_regressions(
                        [x for x in rows if id(x) not in skip], [x for x in trial if id(x) not in skip],
                        index, date, c):
                    return None
                if c is not None and _se.change_regressions(rows, trial, c, date, [r.get("employee")],
                                                            upto=_se._rules.TIER_COVERAGE, hard_only=False):
                    return None
                return trial
            steps = [st for st in range(off, 0, -STAGGER_STEP_MIN)]
            legal = []
            for st in steps:
                trial = _legal(st)
                if trial is not None:
                    legal.append((st, trial))
            if not legal:
                continue      # every stagger of this row breaks something: it stays
            off = legal[0][0]
            if score_fn is not None and _time.monotonic() - _t0 > score_seconds:
                score_fn = None
            if score_fn is not None:
                best = None
                for step, trial in legal + [(0, list(rows))]:
                    try:
                        val = score_fn(trial)
                    except Exception:
                        continue
                    if best is None or val > best[0] + 1e-9:
                        best = (val, step)
                if best is not None:
                    off = best[1]
                if off <= 0:
                    continue  # every stagger of this row costs the week score
            r["shift_start"] = _fmt(s + off)
            r["scheduled_hours"] = str(round((end_m - (s + off)) / 60, 1))
            note = (r.get("notes") or "").strip()
            r["notes"] = f"{note}; staggered start" if note else "staggered start"
            changes.append({"date": date, "employee": r.get("employee"), "role": r.get("role"),
                            "from": start, "to": r["shift_start"], "reason": f"{wd}'s sales climb until {peak_hour}:00"})
    return rows, changes


# ── trim to budget ─────────────────────────────────────────────────────────

def trim_to_budget(rows: list, hours_budget: float, daily_targets: dict, constraints=None, floors: dict = None,
                   splh: dict = None, rainy_dates: set = None, patio_roles: set = None,
                   tolerance: float = TRIM_TOLERANCE, score_fn=None,
                   score_seconds: float = TRIM_SCORE_SECONDS, only_dates=None, requirements=None,
                   learned_worse: dict = None, outcomes: dict = None, report: dict = None) -> tuple:
    """Take the most discretionary hours out until the week is within the
    budget — the tails of shifts first, then whole shifts — every change
    reported.

    1. Tails, the close stagger (schedule audit 10/3/26 P-27). Where another
       person in the role stays on through it, the end of a shift goes first
       — the first in is the first cut, on the day furthest over its own
       target — at most TRIM_TAIL_MAX_HOURS off one shift, never leaving it
       under TRIM_MIN_SHIFT_HOURS, never under a role floor through the hours
       cut. A 2h overage costs 2h, not a 7h shift.
    2. Whole shifts: rows the top-up added, then the later leg of a double,
       then the latest starter of a role on the day furthest over its own
       target — preferring the daypart with the lowest sales per labor hour
       and, on a rainy day, a patio role. Never somebody's only shift of the
       week, never under their minimum hours, never the last of a role on a
       daypart they were on the floor for, never below a role floor, never
       the only trained cover for a kitchen station.

    Neither ever (E-2, P-13, SQ-7, L-24, P-10):
      * makes a rule breach new or worse at any rank above the budget's —
        the night's closer, a manager on the floor every minute, somebody
        at close, a floor or a standing rule, a minimum-hours promise
        (schedule_rules.breach_profile / regressions with `constraints`,
        compared by what each breach is about, never by row);
      * leaves a shift further under its requirement (`requirements`, the
        SHIFT REQUIREMENTS rows: the usual headcount, floors and demand, and
        the half-hour ramp) — the coverage score judges the draft by them,
        and a trim that cut under them made the shortfalls it then scored;
      * touches a weekday where staffing cuts measured worse here
        (`learned_worse`, rec_learning.worsened_levers through schedule_
        engine.learned_worse_levers) or a daypart that has gone wrong before
        (`outcomes`, schedule_intel.outcomes_by_daypart's "troubled");
      * touches a row the owner kept ("_pinned"), a day outside
        `only_dates` (a partial redo), or a row already marked for review.
    A row that will not stand (time off, a double booking) is not hours
    anybody works: it is judged before the trim, never paid for with a legal
    row (SCHED-23). A salaried person's hours are not paid from the hourly
    budget and are never trimmed to fit it.

    score_fn(rows) -> float, when given, chooses among the few most
    discretionary candidates the one whose change costs the week the least
    Shift Quality: the order says which hours are optional, the score which
    of them the floor can best spare. Past `score_seconds` the order alone
    decides.

    `report`, a dict when given, says why a week is still over the budget —
    once, for the review: report["conflict"] = {"over_by", "budget",
    "scheduled", "held": {reason: shifts}, "examples": [...]} when the trim
    stops above the budget (budget_conflict_line words it), else no key.

    Returns (rows, trimmed:[{...}], hours_removed). Each trimmed entry names
    the shift as it was (date, day, employee, role, shift_start, shift_end),
    the hours taken and why; "kind" is "cut" (ended early, "to" the new end)
    or "removed". A shift cut and later removed is one "removed" entry.
    """
    import math
    import time as _time
    _t0 = _time.monotonic()
    rows = list(rows or [])
    budget = float(hours_budget or 0)
    c = constraints
    if report is not None:
        report.pop("conflict", None)
    # A salaried person's hours are not paid from the hourly budget and are
    # never trimmed to fit it (models.salaried_staff).
    _sal = getattr(c, "is_salaried", None)

    def _paid(r):
        return not (_sal and _sal(r.get("employee")))
    if budget <= 0:
        return rows, [], 0.0
    total = sum(_hours(r) for r in rows if _paid(r))
    if total <= budget * (1 + tolerance):
        return rows, [], 0.0

    # Rows that will not stand — a person on time off, off the roster,
    # double-booked — are not coverage and not hours anyone works; they are
    # judged before the trim so it never removes a legal row to pay for one
    # (SCHED-23). The sweep flags them afterwards.
    no_show = set()
    if c is not None:
        try:
            from schedule_rules import violations as _viol
            no_show = {id(rows[v["index"]]) for v in _viol(rows, c) if v.get("no_show")}
        except Exception:
            no_show = set()
    if no_show:
        total = sum(_hours(r) for r in rows if id(r) not in no_show and _paid(r))
        if total <= budget * (1 + tolerance):
            return rows, [], 0.0

    import schedule_engine as _se
    from schedule_rules import floor_for, TIER_MIN_HOURS
    from shift_quality import role_words as _role_words
    if not floors and c is not None:
        floors = getattr(c, "role_floors", None) or {}
    floors = floors or {}
    rainy = rainy_dates or set()
    patio = {p.strip().lower() for p in (patio_roles or set())}
    only = set(only_dates) if only_dates else None
    index = _se.requirement_index(requirements, c) if requirements else {}
    worse_days = {str(k).strip().lower() for k, v in (learned_worse or {}).items()
                  if isinstance(v, dict) and int(v.get("worsened") or 0) > 0}
    troubled = {(str(wd).strip().lower(), part) for wd, parts in (outcomes or {}).items() if isinstance(parts, dict)
                for part, e in parts.items() if isinstance(e, dict) and e.get("troubled")}
    trimmed = []
    removed = 0.0
    entry_of = {}       # id(row now in the week) -> its trimmed entry: a cut row later removed is one entry
    cut_rows = set()    # id() of rows already ended early — one cut per shift
    refusals = {}       # id(row) -> (reason, detail) from the last search
    timed_out = False

    def _out_of_time():
        return _time.monotonic() - _t0 > TRIM_MAX_SECONDS

    def _day(d):
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            return ""

    def _live(rs):
        return [x for x in rs if id(x) not in no_show]

    def _floor(k, day, part):
        return max((floor_for(floors, role, day, part) for role in floors if _se._role_key(role, c) == k), default=0)

    def _held_back(r):
        """Why `r` may not be changed at all, or None."""
        if id(r) in no_show:
            return ("not_working", None)
        if not _paid(r):
            return ("salaried", None)
        if r.get("needs_review"):
            return ("review", None)
        if not _se._changeable(r, only):
            return ("kept", None)
        d = r.get("date")
        if not (d and (r.get("role") or "").strip()):
            return ("unreadable", None)
        wd = _day(d)
        if wd.lower() in worse_days:
            return ("learned", wd)
        for part in _se._present(r):
            if (wd.lower(), part) in troubled:
                return ("troubled", (wd, part))
        return None

    def _guard(r, new):
        """(ok, why) for `r` becoming `new` (None: removed): no shift further
        under its requirement, no rule breach new or worse above the budget."""
        d = r.get("date")
        after = [x for x in rows if x is not r] + ([new] if new is not None else [])
        if index:
            worse = _se.requirement_regressions(_live(rows), _live(after), index, d, c)
            if worse:
                part, key, minute, _b, _a = worse[0]
                spec = ((index.get(d) or {}).get(part) or {}).get(key) or {}
                return False, ("requirement", {"date": d, "day": _day(d), "daypart": part,
                                               "role": spec.get("role") or key,
                                               "required": ((spec.get("half") or {}).get(minute) if minute is not None
                                                            else spec.get("required")),
                                               "at": _fmt(minute) if minute is not None else None})
        if c is not None:
            worse = _se.change_regressions(rows, after, c, d, [r.get("employee")],
                                           upto=TIER_MIN_HOURS, hard_only=False)
            if worse:
                return False, ("rule", {"date": d, "day": _day(d), "label": worse[0]["label"]})
        return True, None

    def splh_for(r):
        try:
            day = datetime.strptime(r.get("date"), "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            return None
        return ((splh or {}).get(day) or {}).get(_daypart(r), {}).get("splh")

    def _scoring():
        return score_fn is not None and _time.monotonic() - _t0 <= score_seconds

    def _choose(options):
        """The option (row, new, hours) whose change costs the week the least
        score, among the first few; the first when nothing scores them."""
        if len(options) < 2 or not _scoring():
            return options[0]
        best = None
        for opt in options:
            r, new, _h = opt
            try:
                val = score_fn([x for x in rows if x is not r] + ([new] if new is not None else []))
            except Exception:
                continue
            if best is None or val > best[0] + 1e-9:
                best = (val, opt)
        return best[1] if best is not None else options[0]

    # ── 1. tails: the close stagger ────────────────────────────────────
    def _tail(r, spans, keys, groups):
        """(new row, hours) — `r` ended as early as the overage asks and the
        rules allow, or None."""
        sp = spans.get(id(r))
        if not sp:
            return None
        s, e = sp
        room = min(TRIM_TAIL_MAX_HOURS, (e - s) / 60.0 - TRIM_MIN_SHIFT_HOURS)
        steps = int(math.floor(room * 60 / TRIM_TAIL_STEP_MIN + 1e-9))
        if steps <= 0:
            return None
        steps = min(steps, max(1, int(math.ceil((total - budget) * 60 / TRIM_TAIL_STEP_MIN - 1e-9))))
        d, k, day = r.get("date"), keys.get(id(r)), _day(r.get("date"))
        me = (r.get("employee") or "").strip().lower()
        mates = [(x, spans[id(x)]) for x in groups.get((d, k), []) if x is not r and spans.get(id(x))
                 and (x.get("employee") or "").strip().lower() != me]
        st = getattr(c, "stations", None) if c is not None else None
        for n in range(steps, 0, -1):
            new_end = e - n * TRIM_TAIL_STEP_MIN
            # Somebody else in the role stays through every hour cut: the
            # last of a role out is never the one sent home early.
            if not any(xs <= new_end and xe >= e for _x, (xs, xe) in mates):
                continue
            # The owner's floor holds through the hours cut, half hour by
            # half hour, as the coverage score holds it.
            if any(_floor(k, day, "night" if t >= 15 * 60 else "morning")
                   > sum(1 for _x, (xs, xe) in mates if xs <= t < xe)
                   for t in range(new_end, e, TRIM_TAIL_STEP_MIN)):
                continue
            if st:
                import kitchen_stations as _ks
                if _ks.is_kitchen(st, r.get("role")):
                    # The cooks on through the hours cut still cover every
                    # station the daypart needs, as the whole crew did.
                    stretch = [x for x in _live(rows) if x is not r and x.get("date") == d
                               and spans.get(id(x)) and spans[id(x)][0] <= new_end and spans[id(x)][1] >= e]
                    if any(_ks.uncovered(stretch, st, d, p) > _ks.uncovered(_live(rows), st, d, p)
                           for p in _ks.parts_of(r)):
                        continue
            new = dict(r)
            new["shift_end"] = _fmt(new_end)
            new["scheduled_hours"] = str(round((new_end - s) / 60.0, 2)).rstrip("0").rstrip(".")
            note = (r.get("notes") or "").strip()
            new["notes"] = f"{note} (ends early — hours budget)" if note else "ends early — hours budget"
            ok, why = _guard(r, new)
            if not ok:
                refusals[id(r)] = why
                continue
            return new, round(n * TRIM_TAIL_STEP_MIN / 60.0, 2)
        return None

    for _ in range(TRIM_MAX_TAIL_CUTS):
        if total <= budget:
            break
        if _out_of_time():
            timed_out = True
            break
        live = _live(rows)
        spans = {id(x): _se._span_minutes(x) for x in rows}
        keys = {id(x): _se._role_key(x.get("role"), c) for x in rows}
        groups, day_hours = {}, {}
        for x in live:
            groups.setdefault((x.get("date"), keys[id(x)]), []).append(x)
            day_hours[x.get("date")] = day_hours.get(x.get("date"), 0.0) + _hours(x)

        def _tail_order(r):
            d = r.get("date")
            tgt = float((daily_targets or {}).get(d) or 0)
            over = (day_hours.get(d, 0.0) - tgt) if tgt else day_hours.get(d, 0.0)
            rain = 0 if (d in rainy and (r.get("role") or "").strip().lower() in patio) else 1
            s = splh_for(r)
            sp = spans.get(id(r)) or (0, 0)
            return (rain, -over, s if s is not None else 10 ** 9, sp[0], -sp[1], (r.get("employee") or ""))

        refusals.clear()
        want = TRIM_SCORE_CANDIDATES if _scoring() else 1
        options = []
        for r in sorted((x for x in rows if id(x) not in cut_rows), key=_tail_order):
            held = _held_back(r)
            if held is not None:
                refusals[id(r)] = held
                continue
            got = _tail(r, spans, keys, groups)
            if got:
                options.append((r, got[0], got[1]))
                if len(options) >= want:
                    break
        if not options:
            break
        r, new, h = _choose(options)
        rows[rows.index(r)] = new
        cut_rows.add(id(new))
        total -= h
        removed += h
        entry = {"date": r.get("date"), "day": r.get("day"), "employee": r.get("employee"), "role": r.get("role"),
                 "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"), "hours": h,
                 "kind": "cut", "to": new.get("shift_end"),
                 "reason": f"ends {new.get('shift_end')}, {h:g}h early — another "
                           f"{_role_words(r.get('role'), 1, 'person in the role')} stays on until {r.get('shift_end')}"}
        trimmed.append(entry)
        entry_of[id(new)] = entry

    # ── 2. whole shifts ────────────────────────────────────────────────
    for _ in range(TRIM_MAX_REMOVALS):
        if total <= budget:
            break
        if _out_of_time():
            timed_out = True
            break
        live = _live(rows)
        keys = {id(x): _se._role_key(x.get("role"), c) for x in rows}
        parts_of = {id(x): _se._present(x) for x in rows}
        day_hours, person_rows, person_hours, crew, by_person_day = {}, {}, {}, {}, {}
        for x in live:
            low = (x.get("employee") or "").strip().lower()
            day_hours[x.get("date")] = day_hours.get(x.get("date"), 0.0) + _hours(x)
            person_rows[low] = person_rows.get(low, 0) + 1
            person_hours[low] = person_hours.get(low, 0.0) + _hours(x)
            for part in parts_of[id(x)]:
                crew.setdefault((x.get("date"), keys[id(x)], part), set()).add(low)
        for x in rows:
            by_person_day.setdefault((x.get("employee"), x.get("date")), []).append(x)

        def second_leg(r):
            legs = sorted(by_person_day.get((r.get("employee"), r.get("date")), []),
                          key=lambda x: _minutes(x.get("shift_start", "")) or 0)
            return len(legs) > 1 and legs[-1] is r

        def day_over(d):
            tgt = float((daily_targets or {}).get(d) or 0)
            return (day_hours.get(d, 0.0) - tgt) if tgt else day_hours.get(d, 0.0)

        def removable(r):
            """(ok, why) by the trim's own rules, before the guard."""
            held = _held_back(r)
            if held is not None:
                return False, held
            d, name = r.get("date"), r.get("employee")
            low = (name or "").strip().lower()
            # Never somebody's only shift of the week, and never under the
            # minimum hours they asked for (SCHED-23). A row the top-up added
            # is the pipeline's own addition, so taking it back is allowed.
            if "top-up" not in (r.get("notes") or "").lower() and person_rows.get(low, 0) <= 1:
                return False, ("only_shift", None)
            if c is not None:
                try:
                    mn = c.min_hours(name)
                except Exception:
                    mn = None
                if mn and person_hours.get(low, 0.0) - _hours(r) < float(mn) - 0.05:
                    return False, ("min_hours", None)
            day, k = _day(d), keys[id(r)]
            for part in parts_of[id(r)]:
                n = len(crew.get((d, k, part)) or ())
                if n <= 1:
                    return False, ("last_of_role", None)
                if n - 1 < _floor(k, day, part):
                    return False, ("floor", None)
            # Never the only trained cover for a kitchen station the owner
            # requires on that daypart (kitchen_stations, 9/30/26).
            _st = getattr(c, "stations", None) if c is not None else None
            if _st:
                import kitchen_stations as _ks
                if _ks.protects(live, r, _st):
                    return False, ("station", None)
            return True, None

        def priority(r):
            note = (r.get("notes") or "").lower()
            tier = 0 if "top-up" in note else (1 if second_leg(r) else 2)
            rain = 0 if (r.get("date") in rainy and (r.get("role") or "").strip().lower() in patio) else 1
            s = splh_for(r)
            splh_rank = s if s is not None else 10 ** 9
            start = _minutes(r.get("shift_start", "")) or 0
            return (tier, rain, -day_over(r.get("date")), splh_rank, -start)

        refusals.clear()
        cands = []
        for r in rows:
            ok, why = removable(r)
            if ok:
                cands.append(r)
            else:
                refusals[id(r)] = why
        want = TRIM_SCORE_CANDIDATES if _scoring() else 1
        options = []
        for r in sorted(cands, key=priority):
            if options and (priority(r)[0] != priority(options[0][0])[0] or len(options) >= want):
                break
            ok, why = _guard(r, None)
            if ok:
                options.append((r, None, _hours(r)))
            else:
                refusals[id(r)] = why
        if not options:
            break
        victim, _none, h = _choose(options)
        rows.remove(victim)
        total -= h
        removed += h
        why = ("added by the top-up" if "top-up" in (victim.get("notes") or "").lower()
               else "second leg of a double" if second_leg(victim)
               else f"latest {victim.get('role')} on a day {day_over(victim.get('date')):.0f}h over its target")
        if victim.get("date") in rainy and (victim.get("role") or "").strip().lower() in patio:
            why += ", rain forecast on a patio role"
        earlier = entry_of.pop(id(victim), None)
        if earlier is not None:
            # Ended early first, then taken out: one entry, the whole shift.
            earlier.update({"kind": "removed", "hours": round(earlier["hours"] + h, 2), "reason": why})
            earlier.pop("to", None)
        else:
            trimmed.append({"date": victim.get("date"), "day": victim.get("day"), "employee": victim.get("employee"),
                            "role": victim.get("role"), "shift_start": victim.get("shift_start"),
                            "shift_end": victim.get("shift_end"), "hours": h, "kind": "removed", "reason": why})

    if report is not None and total > budget * (1 + tolerance):
        report["conflict"] = _conflict(total, budget, refusals, timed_out)
    return rows, trimmed, round(removed, 1)


# Why a trim stopped short, in the order an owner can act on it. Rows that
# will not stand, salaried hours and unreadable rows are not the budget's.
_HELD_REASONS = ("requirement", "learned", "troubled", "rule", "floor", "last_of_role", "station",
                 "min_hours", "only_shift", "kept", "review")


def _conflict(total, budget, refusals, timed_out=False) -> dict:
    """The budget conflict the trim could not resolve, said once: how far
    over, and what held the rest — counted by reason, with a few examples."""
    held, examples, seen = {}, [], set()
    for reason, detail in refusals.values():
        if reason not in _HELD_REASONS:
            continue
        held[reason] = held.get(reason, 0) + 1
        if detail is None:
            continue
        ex = {"reason": reason}
        if isinstance(detail, dict):
            ex.update(detail)
        elif reason == "learned":
            ex["day"] = detail
        elif reason == "troubled":
            ex["day"], ex["daypart"] = detail
        sig = tuple(sorted((k, str(v)) for k, v in ex.items()))
        if sig in seen:
            continue
        seen.add(sig)
        examples.append(ex)
    examples.sort(key=lambda x: (_HELD_REASONS.index(x["reason"]), x.get("date") or "", x.get("day") or ""))
    out = {"over_by": round(total - budget, 1), "budget": round(budget, 1), "scheduled": round(total, 1),
           "held": {k: held[k] for k in _HELD_REASONS if k in held}, "examples": examples[:6]}
    if timed_out:
        out["timed_out"] = True
    return out


def budget_conflict_line(conflict: dict, over_by: float = None, budget: float = None) -> str:
    """One sentence for the review when the trim stopped above the budget:
    what holds the remaining hours — the shifts' own requirement, days and
    dayparts kept from cuts, the rules — and what the owner can change
    (schedule audit 10/3/26 SQ-7: the requirement and the budget disagree,
    said once). "" when there is no conflict."""
    if not conflict:
        return ""
    over = conflict.get("over_by") if over_by is None else over_by
    b = conflict.get("budget") if budget is None else budget
    held = conflict.get("held") or {}
    ex = conflict.get("examples") or []

    def _first(reason):
        return next((x for x in ex if x.get("reason") == reason), None)

    def _part(p):
        return {"morning": "lunch", "night": "dinner"}.get(p, p or "")
    bits = []
    if held.get("requirement"):
        x = _first("requirement")
        what = ""
        if x and x.get("required"):
            what = (f" — {x['day']} {_part(x.get('daypart'))} needs {x['required']} {x['role']}"
                    + (f" at {x['at']}" if x.get("at") else ""))
        bits.append("what the shifts need (your usual staffing, floors and demand" + what + ")")
    if held.get("learned"):
        x = _first("learned")
        bits.append(f"{x['day']}s, where staffing cuts measured worse here" if x and x.get("day")
                    else "days where staffing cuts measured worse here")
    if held.get("troubled"):
        x = _first("troubled")
        bits.append(f"{x['day']} {_part(x.get('daypart'))}, which has gone wrong before" if x and x.get("day")
                    else "dayparts that have gone wrong before")
    if held.get("rule"):
        x = _first("rule")
        bits.append("the rules" + (f" ({x['label']})" if x and x.get("label") else ""))
    if held.get("floor") or held.get("last_of_role") or held.get("station"):
        bits.append("your floors and the last of each role")
    if held.get("min_hours") or held.get("only_shift"):
        bits.append("minimum hours and people's only shifts")
    if not bits:
        return ""
    return (f"The trim stopped {over:,.0f}h over the {b:,.0f}h budget: the rest is held by "
            + "; ".join(bits[:3]) + ". Raise the hours budget, or lower the floors or staffing you set, to close it.")


def trim_lines(trimmed: list, hours_removed: float, budget: float, limit: int = 6) -> list:
    if not trimmed:
        return []
    cut = sum(1 for t in trimmed if t.get("kind") == "cut")
    gone = len(trimmed) - cut
    parts = ([f"{gone} shift{'s' if gone != 1 else ''} removed"] if gone else []) + \
            ([f"{cut} ended early" if gone else f"{cut} shift{'s' if cut != 1 else ''} ended early"] if cut else [])
    lines = [f"Trimmed {hours_removed:g}h to fit the {budget:,.0f}h budget: {', '.join(parts)}."]
    for t in trimmed[:limit]:
        lines.append(f"{t.get('day') or t.get('date')} {t.get('role')} {t.get('shift_start')}–{t.get('shift_end')}: {t.get('employee')} — {t['reason']}.")
    if len(trimmed) > limit:
        lines.append(f"…and {len(trimmed) - limit} more, all listed in the review.")
    return lines
