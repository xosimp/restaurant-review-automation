"""
schedule_economics.py — the money side of a generated week, deterministically.

The budget used to be a ceiling in words only: the model could write 140
hours past it and nothing took them back, overtime was priced straight,
the weekly revenue behind the budget was a flat twelfth of a monthly
target, and the sales curve inside a day was a fact stated to the model
and never used. This module owns:

  trim_to_budget      — remove the most discretionary hours until the week
                        fits, never below a floor, every removal reported
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

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports): a
    bound copy kept whichever function was there at import, so a test that
    redirected models.get_conn never reached this module's reads. The
    module's own DB_PATH default means "whatever models uses now"."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

# Final days only (canonical_facts, memory audit 9/29/26): a half-night the
# POS had not closed is never a week's sales or a day's labor %.
from canonical_facts import FINAL_SQL

TRIM_TOLERANCE = 0.02            # a week within 2% of the budget is not trimmed
TRIM_MAX_REMOVALS = 60
TRIM_SCORE_CANDIDATES = 5       # how many equally-discretionary rows the score chooses between
TRIM_SCORE_SECONDS = 8.0        # past this the score stops choosing; the order alone decides (big rosters)
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


def _weekday_medians(restaurant_id, weeks: int = 8, db_path=DB_PATH, today=None) -> dict:
    """{weekday: median daily sales} over the last `weeks` weeks before the
    restaurant's own today (never the server's UTC date — D-33)."""
    today = _today_for(restaurant_id, today)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT day_of_week, sales FROM labor_daily_history WHERE restaurant_id=? AND sales IS NOT NULL "
            f"AND sales > 0 AND date >= ? AND date < ? AND {FINAL_SQL}",
            (restaurant_id, (today - timedelta(weeks=int(weeks))).isoformat(), today.isoformat())).fetchall()
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


# ── what each date of the week asks for ───────────────────────────────────
#
# The week's demand used to change a shift's LABEL and nothing else: the
# requirements table copied past headcount, the day targets spread a
# measured +40% holiday across all seven days, and the weather reached only
# the prompt (schedule audit 10/3/26 D-23, P-19, D-24, D-30). One number per
# date now — how far it sits from a typical night of its weekday — from, in
# order: the owner's own budget for the night (the DSR); else what the
# owner and the record say about the date (demand_signals.by_date: an
# owner's lift, booked covers, an event's measured lift, last year's
# holiday night — already the strongest of them, never a sum); else the
# forecast's measured effects (event_memory: the 1st of the month, a
# campaign night). Then the weather, only where it is measured: a fresh
# forecast of rain on a near-term date, at this restaurant's own measured
# rain effect — never an assumed one. Different kinds multiply (rain on a
# holiday still dampens it); the same kind never adds.

RAIN_FORECAST_PCT = 60           # the precipitation chance that makes a date rainy (the trim's own line)


def _rain_effect(restaurant_id, db_path=DB_PATH):
    """event_memory.measured_effect for rain when it is past the sample
    floor and moves sales past EFFECT_FLOOR_PCT, else None."""
    try:
        import event_memory as _em
        e = _em.measured_effect(restaurant_id, _em.RAIN_LABEL, db_path=None if db_path == DB_PATH else db_path)
    except Exception:
        return None
    try:
        from event_memory import EFFECT_FLOOR_PCT
    except Exception:
        EFFECT_FLOOR_PCT = 5
    if not e or not e.get("applies") or abs(float(e.get("median_lift_pct") or 0)) < EFFECT_FLOOR_PCT:
        return None
    return e


def date_demand(restaurant_id, week_dates, signals_by_date: dict = None, weather: list = None, closed_dates=(),
                db_path=DB_PATH, today=None) -> dict:
    """{date: {"typical_sales", "projected_sales", "ratio", "pct", "reasons",
    "labels", "sources", "closed"}} — each date's demand against a typical
    night of its weekday (the 8-week median the typical headcount was
    staffed for), by the order in the note above. `reasons` are the
    owner-facing words for why it moved; `labels` the ones to name on the
    date where the dated facts do not already (the budget, a measured
    forecast effect, the rain). A closed date is `closed` with no sales.
    Never raises; a date nothing moves is ratio 1.0."""
    from time_utils import mdy
    dates = [str(d)[:10] for d in (week_dates or []) if d]
    closed = {str(d)[:10] for d in (closed_dates or ())}
    signals = signals_by_date or {}
    try:
        typical = _weekday_medians(restaurant_id, weeks=8, db_path=db_path, today=today)
    except Exception:
        typical = {}
    try:
        from dsr import store as _dsr_store
        budgets = _dsr_store.budgets_for(restaurant_id, min(dates), max(dates), db_path=db_path) if dates else {}
    except Exception:
        budgets = {}
    rain = None
    wet = {}
    for w in weather or []:
        try:
            if not w.get("stale") and int(w.get("precip_pct") or 0) >= RAIN_FORECAST_PCT:
                wet[str(w.get("date"))[:10]] = int(w.get("precip_pct") or 0)
        except (TypeError, ValueError):
            continue
    if wet:
        rain = _rain_effect(restaurant_id, db_path=db_path)
    out = {}
    for d in dates:
        try:
            day = date.fromisoformat(d)
        except ValueError:
            continue
        wd = day.strftime("%A")
        base = typical.get(wd)
        entry = {"typical_sales": round(base, 0) if base else None, "projected_sales": None, "ratio": 1.0,
                 "pct": 0, "reasons": [], "labels": [], "sources": [], "closed": d in closed}
        out[d] = entry
        if d in closed:
            entry.update(projected_sales=0.0, ratio=0.0, pct=None)
            continue
        occasion = 1.0
        b = budgets.get(d) or {}
        bv = b.get("net") if b.get("net") not in (None, "") else b.get("gross")
        try:
            bv = float(bv) if bv not in (None, "") else None
        except (TypeError, ValueError):
            bv = None
        sig = signals.get(d) or {}
        if bv and bv > 0 and base:
            occasion = bv / base
            entry["sources"].append("budget")
            entry["reasons"].append(f"your budget for the night, ${bv:,.0f} against a typical {wd}'s ${base:,.0f}")
            entry["labels"].append(f"Your budget for the night (${bv:,.0f})")
        elif sig.get("lift_pct") is not None:
            occasion = 1.0 + float(sig["lift_pct"]) / 100.0
            entry["sources"].append("signals")
            what = "; ".join(str(x) for x in (sig.get("labels") or []) if x) or "a dated fact"
            entry["reasons"].append(f"{what}: {int(sig['lift_pct']):+d}%")
        else:
            try:
                import demand as _demand
                fc = _demand.forecast_day(restaurant_id, day, db_path=db_path, calibrate=False)
            except Exception:
                fc = {}
            if fc.get("available") and fc.get("effect_pct"):
                occasion = 1.0 + float(fc["effect_pct"]) / 100.0
                names = [e.get("display") or e.get("label") for e in (fc.get("effects") or []) if e]
                entry["sources"].append("measured_effects")
                entry["reasons"].append(f"{', '.join(n for n in names if n) or 'a measured effect'}: "
                                        f"{float(fc['effect_pct']):+.0f}% measured here")
                entry["labels"].append(", ".join(n for n in names if n) + f" ({float(fc['effect_pct']):+.0f}% measured here)")
        weather_f = 1.0
        if d in wet and rain:
            weather_f = 1.0 + float(rain["median_lift_pct"]) / 100.0
            entry["sources"].append("weather")
            entry["reasons"].append(f"rain forecast ({wet[d]}%): rain nights here ran "
                                    f"{abs(float(rain['median_lift_pct'])):.0f}% "
                                    f"{'below' if rain['median_lift_pct'] < 0 else 'above'} a typical one "
                                    f"(measured {rain['n']} times, last {mdy(rain['last'])})")
            entry["labels"].append(f"Rain forecast ({wet[d]}%)")
        ratio = max(0.0, occasion * weather_f)
        entry["ratio"] = round(ratio, 3)
        entry["pct"] = int(round((ratio - 1.0) * 100))
        if base:
            entry["projected_sales"] = round((bv * weather_f) if ("budget" in entry["sources"]) else base * ratio, 0)
    return out


# ── the measured sales curve: tickets, captures, nightly reports ──────────
#
# The hourly demand curve and every daypart split were read from the live
# intraday captures alone (three same-weekday days needed), while the POS's
# ticket archive (pos_tickets: every ticket's open time and net, 90 days
# back) sat unread — Simple EJ's, RPOWER-live since 9/28/26, had no curve
# and a 40/60 split assumed for weeks (schedule audit 10/3/26 D-26, L-25).
# One reader now: per business date, the archive's tickets by the hour they
# opened; for a date the archive does not hold, the intraday captures; then
# the nightly report's own hourly split. One source per date — never two
# added together. An hour after midnight that belongs to the night before
# is 24+ (1:30am → hour 25), as the captures already file it.

CURVE_WEEKS = 8
CURVE_MIN_DAYS = 3               # same-weekday measured days before a curve or a split is stated
MORNING_SPLIT_HOUR = 15          # shift_quality.DAYPART_CUTOVER: sales before 3pm are lunch's
LATE_START_HOUR = 22             # shift_quality.LATE_WINDOW_START: from 10pm is the late segment (D-32)


def _today_for(restaurant_id, today=None):
    """The restaurant's own calendar date (demand.local_today) — never the
    server's UTC date: after 7pm Central it was already tomorrow (D-33)."""
    if today is not None:
        return today
    try:
        import demand
        return demand.local_today(restaurant_id)
    except Exception:
        return date.today()


def _ticket_days(conn, restaurant_id, start, end) -> dict:
    """{business date: {hour: net}} from the ticket archive, by the hour each
    ticket opened on its business day's clock. Cancelled tickets are out."""
    out = {}
    try:
        # Summed by the hour in SQL: a busy restaurant's eight weeks are tens
        # of thousands of tickets, read on every live rescore.
        rows = conn.execute(
            "SELECT business_date, substr(replace(opened_at, ' ', 'T'), 1, 13) AS hk, SUM(net_sales) AS net "
            "FROM pos_tickets WHERE restaurant_id=? AND business_date>=? AND business_date<? "
            "AND COALESCE(cancelled,0)=0 AND opened_at IS NOT NULL GROUP BY business_date, hk",
            (restaurant_id, start, end)).fetchall()
    except Exception:
        return {}
    for r in rows:
        bd = str(r["business_date"])[:10]
        try:
            opened = datetime.strptime(str(r["hk"] or ""), "%Y-%m-%dT%H")
            hour = opened.hour + 24 * max(0, (opened.date() - date.fromisoformat(bd)).days)
            net = float(r["net"] or 0)
        except (TypeError, ValueError):
            continue
        day = out.setdefault(bd, {})
        day[hour] = day.get(hour, 0.0) + net
    return out


def _intraday_days(conn, restaurant_id, start, end) -> dict:
    """{business date: {hour: net}} from the cumulative intraday captures:
    each hour's own sales are the step from the reading before it."""
    try:
        rows = conn.execute(
            "SELECT business_date, captured_hour, net_sales FROM pos_intraday WHERE restaurant_id=? "
            "AND business_date>=? AND business_date<? ORDER BY business_date, captured_hour",
            (restaurant_id, start, end)).fetchall()
    except Exception:
        return {}
    caps = {}
    for r in rows:
        caps.setdefault(str(r["business_date"])[:10], []).append((int(r["captured_hour"]), float(r["net_sales"] or 0)))
    out = {}
    for bd, pts in caps.items():
        pts.sort()
        prev, steps = 0.0, {}
        for h, cum in pts:
            steps[h] = max(0.0, cum - prev)
            prev = cum
        out[bd] = steps
    return out


def _dsr_days(conn, restaurant_id, start, end) -> dict:
    """{business date: {hour: net}} from each finished nightly report's
    hourly split (blocks.sales.detail.hourly), the latest version of each
    night. An hour before the business day starts belongs to the night."""
    import json as _json
    from time_utils import BUSINESS_DAY_START_HOUR
    try:
        rows = conn.execute(
            "SELECT business_date, version, facts_json FROM dsr_reports WHERE restaurant_id=? AND business_date>=? "
            "AND business_date<? AND status IN ('final','provisional') ORDER BY business_date, version",
            (restaurant_id, start, end)).fetchall()
    except Exception:
        return {}
    out = {}
    for r in rows:                              # later versions overwrite earlier ones
        try:
            blk = ((_json.loads(r["facts_json"] or "{}") or {}).get("blocks") or {}).get("sales") or {}
        except (TypeError, ValueError):
            continue
        if blk.get("status") != "ready":
            continue
        day = {}
        for h in ((blk.get("detail") or {}).get("hourly") or []):
            try:
                hour, net = int(h.get("hour")), float(h.get("net") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if hour < BUSINESS_DAY_START_HOUR:
                hour += 24
            day[hour] = day.get(hour, 0.0) + net
        if day:
            out[str(r["business_date"])[:10]] = day
    return out


def _median(vals):
    s = sorted(vals)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def measured_sales_curve(restaurant_id, weeks: int = CURVE_WEEKS, db_path=DB_PATH, today=None) -> dict:
    """{weekday: {"hours": {hour: share of the day's sales}, "days", "sources":
    {tickets, intraday, dsr}, "morning_share", "late_share"}} over the
    `weeks` weeks before the restaurant's today, from the measured nights
    only (see the note above). Each hour's share and each split is the
    median across that weekday's days; a weekday with fewer than
    CURVE_MIN_DAYS measured days is still returned with its `days`, and
    every caller holds it to the floor. {} when nothing is measured."""
    today = _today_for(restaurant_id, today)
    start = (today - timedelta(weeks=int(weeks))).isoformat()
    end = today.isoformat()
    conn = get_conn(db_path)
    try:
        sources = (("tickets", _ticket_days(conn, restaurant_id, start, end)),
                   ("intraday", _intraday_days(conn, restaurant_id, start, end)),
                   ("dsr", _dsr_days(conn, restaurant_id, start, end)))
    finally:
        conn.close()
    days = {}
    for name, by_date in sources:
        for bd, hours in by_date.items():
            if bd in days:
                continue
            positive = {h: v for h, v in hours.items() if v > 0}
            total = sum(positive.values())
            if total <= 0:
                continue
            days[bd] = (name, {h: v / total for h, v in positive.items()})
    per_wd = {}
    for bd, (name, shares) in days.items():
        try:
            wd = date.fromisoformat(bd).strftime("%A")
        except ValueError:
            continue
        per_wd.setdefault(wd, []).append((name, shares))
    out = {}
    for wd, entries in per_wd.items():
        hours = sorted({h for _n, s in entries for h in s})
        n = len(entries)
        out[wd] = {
            "hours": {h: round(sorted(s.get(h, 0.0) for _n, s in entries)[n // 2], 4) for h in hours},
            "days": n,
            "sources": {k: sum(1 for nm, _s in entries if nm == k) for k in ("tickets", "intraday", "dsr")},
            "morning_share": round(_median([sum(v for h, v in s.items() if h < MORNING_SPLIT_HOUR)
                                            for _n, s in entries]), 4),
            "late_share": round(_median([sum(v for h, v in s.items() if h >= LATE_START_HOUR)
                                         for _n, s in entries]), 4),
        }
    return out


def hourly_profile(restaurant_id, weeks: int = CURVE_WEEKS, db_path=DB_PATH, today=None) -> dict:
    """{weekday: {hour: share}} for the weekdays measured on at least
    CURVE_MIN_DAYS days — the curve the requirements, the scorer and the
    stagger read (schedule_engine._hourly_profile)."""
    return {wd: v["hours"] for wd, v in measured_sales_curve(restaurant_id, weeks, db_path, today).items()
            if v["days"] >= CURVE_MIN_DAYS and v["hours"]}


# ── sales per labor hour by daypart ───────────────────────────────────────

# A weekday's late segment (from 10pm, D-32) is stated only past this share
# of its measured sales: a stray 10:30pm ticket is not a late service.
LATE_MIN_SHARE = 0.03


def _late_minutes(row) -> float:
    """Minutes a shift is on the floor from 10pm (LATE_START_HOUR) to its
    end, on its own date's clock (an end at or before the start crosses
    midnight)."""
    s, e = _minutes(row.get("shift_start", "")), _minutes(row.get("shift_end", ""))
    if s is None or e is None:
        return 0.0
    if e <= s:
        e += 24 * 60
    return float(max(0, e - max(s, LATE_START_HOUR * 60)))


def splh_by_daypart(restaurant_id, weeks: int = 8, db_path=DB_PATH, curve: dict = None, today=None) -> dict:
    """{weekday: {"morning": {sales, hours, splh, sales_split}, "night": {...},
    "late": {...}}}; a weekday whose sales split was never measured is {}.

    Daily sales and hours come from labor_daily_history (hourly labor — the
    salaried are left out of it). The split of each day's sales into
    dayparts is MEASURED: the share before 3pm (and, for the late segment,
    from 10pm) from measured_sales_curve — the ticket archive, the intraday
    captures, the nightly report's hourly split — on at least
    CURVE_MIN_DAYS days of that weekday. A weekday never measured has no
    daypart figures: it used to be split 40/60 by assumption, and the
    assumption became the objective the prompt, the scorer's splh targets
    and the trim all read (schedule audit 10/3/26 L-25). The split of hours
    comes from the same `weeks` weeks of hourly shifts (the salaried out,
    as the daily hours leave them), each row filed under the daypart it is
    mostly on the floor for — it read the whole shift history. The late
    segment's hours are each shift's minutes from 10pm (D-32). A weekday
    with no sales or no hours is absent — never 0."""
    from models import _cached_shifts
    today = _today_for(restaurant_id, today)
    since = (today - timedelta(weeks=int(weeks))).isoformat()
    conn = get_conn(db_path)
    try:
        days = conn.execute(
            "SELECT date, day_of_week, sales, total_hours FROM labor_daily_history WHERE restaurant_id=? "
            f"AND sales IS NOT NULL AND sales > 0 AND date >= ? AND date < ? AND {FINAL_SQL}",
            (restaurant_id, since, today.isoformat())).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    if curve is None:
        try:
            curve = measured_sales_curve(restaurant_id, weeks=weeks, db_path=db_path, today=today)
        except Exception as e:
            print(f"[splh] sales curve unreadable for {restaurant_id}: {e}")
            curve = {}
    measured = {wd: v for wd, v in (curve or {}).items() if int(v.get("days") or 0) >= CURVE_MIN_DAYS}
    # hours split by daypart from the window's hourly shifts, per weekday
    hrs = {}
    try:
        from labor import _without_salaried
        shifts, _sal_h = _without_salaried(restaurant_id, list(_cached_shifts(restaurant_id) or []))
        from shift_quality import present_dayparts
        for sh in shifts:
            d = str(sh.get("date") or "")[:10]
            if not d or d < since or d >= today.isoformat():
                continue
            try:
                wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
            except ValueError:
                continue
            # Filed under the daypart the row is mostly on the floor for
            # (shift_quality.present_dayparts), as the scorer's `splh`
            # dimension files a draft's hours — a 2pm-11pm cook is dinner.
            part = present_dayparts(sh)[0]
            if part == "unknown":
                continue
            h = 0.0
            try:
                h = float(sh.get("actual_hours") or sh.get("scheduled_hours") or sh.get("hours") or 0)
            except (TypeError, ValueError):
                pass
            e = hrs.setdefault(wd, {"morning": 0.0, "night": 0.0, "late": 0.0})
            e[part] += h
            e["late"] += min(h, _late_minutes(sh) / 60.0)
    except Exception as e:
        print(f"[splh] shift hours unreadable for {restaurant_id}: {e}")
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
        total_hours = e["hours"] if e["hours"] > 0 else None
        if not total_hours:
            continue
        cv = measured.get(wd)
        hsplit = hrs.get(wd)
        if not cv or not hsplit or (hsplit["morning"] + hsplit["night"]) <= 0:
            out[wd] = {}           # sales and hours, but no measured split: no daypart figures
            continue
        whole_h = hsplit["morning"] + hsplit["night"]
        m_share = float(cv.get("morning_share") or 0.0)
        parts = [("morning", m_share, hsplit["morning"] / whole_h), ("night", 1 - m_share, hsplit["night"] / whole_h)]
        late_share = float(cv.get("late_share") or 0.0)
        if late_share >= LATE_MIN_SHARE and hsplit["late"] > 0:
            parts.append(("late", late_share, hsplit["late"] / whole_h))
        day = {}
        for part, s_share, h_share in parts:
            s = e["sales"] / e["n"] * s_share
            h = total_hours / e["n"] * h_share
            if h > 0 and s > 0:
                day[part] = {"sales": round(s, 0), "hours": round(h, 1), "splh": round(s / h, 0),
                             "sales_split": "measured"}
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
# The target is never raised past this multiple of the restaurant's own
# record in one step: a salaried share that leaves an hourly target of a
# few points would otherwise ask for triple the productivity it ever ran.
SPLH_SCALE_MAX = 2.0
SPLH_PARTS = ("morning", "night", "late")


def _dp_label(part):
    return {"morning": "lunch", "night": "dinner", "late": "late night"}.get(part, part)


def _hourly_target_pct(restaurant_id, labor_target_pct, days_with_sales, sales_total, db_path=DB_PATH):
    """(hourly target %, salaried?) — the labor target the HOURLY record is
    judged against: the owner's target counts salaries (9/30/26), and
    labor_daily_history is hourly, so the salaried staff's share of the
    window's sales comes off the target first (schedule audit 10/3/26 D-1:
    the same all-in-against-hourly mix-up as the hours budget). (target,
    False) with nobody salaried."""
    try:
        from models import get_restaurant, salaried_day_share
        share = float(salaried_day_share(get_restaurant(restaurant_id) if db_path == DB_PATH
                                         else get_restaurant(restaurant_id, db_path)) or 0)
    except Exception:
        share = 0.0
    if share <= 0 or not sales_total or not days_with_sales:
        return float(labor_target_pct), False
    return float(labor_target_pct) - share * days_with_sales / float(sales_total) * 100.0, True


def splh_objective(restaurant_id, splh: dict = None, labor_target_pct=None, weeks: int = 8, db_path=DB_PATH,
                   today=None) -> dict:
    """{available, by_day: {weekday: {daypart: target}}, targets: {daypart:
    {target, history, source}} (the week's figure per daypart, for display),
    daypart_sales: {weekday: {daypart: sales}}, hold: {weekday: {daypart:
    history ÷ target}}, labor_target_pct, history_labor_pct, scale, basis,
    split_unmeasured} or {available: False, reason}.

    Each weekday's daypart target is its own recent sales per labor hour.
    When the restaurant has been running over its labor % target, every
    target is raised by the same factor — history labor % ÷ target labor % —
    which is exactly the productivity the target needs at the wages it
    actually paid (labor % = wage × hours ÷ sales). Under target, history
    stands: the objective never asks for more hours than the record ran.

    The record is hourly (labor_daily_history) and the owner's target counts
    salaries, so the comparison is against the target less the salaried
    staff's share of the same window's sales (_hourly_target_pct, D-1) — the
    whole target against the hourly record read Simple EJ's 28% hourly as
    inside its 35% while it ran 41-45% with salaries. `hold` is how far the
    usual crew must come in to meet the target on each daypart: the
    requirements table holds the usual headcount to it (P-19)."""
    today = _today_for(restaurant_id, today)
    splh = splh_by_daypart(restaurant_id, weeks=weeks, db_path=db_path, today=today) if splh is None else splh
    if not splh:
        return {"available": False, "reason": "No daily sales and hours on file yet, so there is no sales-per-labor-hour target."}
    unmeasured = sorted(wd for wd, parts in splh.items() if not parts)
    tot = {p: [0.0, 0.0, 0] for p in SPLH_PARTS}
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
        reason = f"Sales per labor hour needs at least {SPLH_MIN_WEEKDAYS} weekdays of sales and hours per daypart"
        if unmeasured:
            reason += (f", with lunch and dinner sales measured apart (the ticket archive, the intraday readings or the "
                       f"nightly reports) — {len(unmeasured)} weekday{'s' if len(unmeasured) != 1 else ''} have none yet")
        return {"available": False, "reason": reason + ".", "split_unmeasured": unmeasured}
    hist_pct, days_n, sales_total = None, 0, 0.0
    since = (today - timedelta(weeks=int(weeks))).isoformat()
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT SUM(labor_pct * sales) AS w, SUM(sales) AS s, COUNT(*) AS n FROM labor_daily_history "
                           "WHERE restaurant_id=? AND sales > 0 AND labor_pct IS NOT NULL AND date >= ? AND date < ? "
                           f"AND {FINAL_SQL}", (restaurant_id, since, today.isoformat())).fetchone()
        if row and row["s"]:
            hist_pct = float(row["w"]) / float(row["s"])
            days_n, sales_total = int(row["n"] or 0), float(row["s"])
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
    hourly_target, with_salaries = (None, False)
    if labor_target_pct:
        hourly_target, with_salaries = _hourly_target_pct(restaurant_id, labor_target_pct, days_n, sales_total,
                                                          db_path=db_path)
    scale, capped = 1.0, False
    if hist_pct and hourly_target is not None:
        if hourly_target <= 0:
            scale, capped = SPLH_SCALE_MAX, True
        elif hist_pct > hourly_target:
            scale = hist_pct / hourly_target
            if scale > SPLH_SCALE_MAX:
                scale, capped = SPLH_SCALE_MAX, True
    by_day = {wd: {p: round(v * scale, 0) for p, v in parts.items() if p in have} for wd, parts in hist_by_day.items()}
    by_day = {wd: parts for wd, parts in by_day.items() if parts}
    hold = {wd: {p: round(1.0 / scale, 3) for p in parts} for wd, parts in by_day.items()} if scale > 1 else {}
    targets = {}
    target_words = (f"your {float(labor_target_pct):g}% labor target with the salaried staff's pay counted"
                    if with_salaries else f"your {float(labor_target_pct or 0):g}% labor target")
    for part, (s, h, _n) in have.items():
        hist = s / h
        if scale > 1:
            src = (f"your own recent pace on each day's {_dp_label(part)}, raised {int(round((scale - 1) * 100))}% to meet "
                   f"{target_words} ({'your hourly labor has' if with_salaries else 'you have'} run {hist_pct:.1f}%"
                   + ("; the raise is capped at double your own pace" if capped else "") + ")")
        elif hist_pct and labor_target_pct:
            src = f"your own recent pace on each day's {_dp_label(part)} — already inside {target_words}"
        else:
            src = f"your own recent pace on each day's {_dp_label(part)} over the last {weeks} weeks"
        targets[part] = {"target": round(hist * scale, 0), "history": round(hist, 0), "source": src}
    basis = "; ".join(f"{_dp_label(p).capitalize()} ${v['target']:,.0f} per labor hour across the week — {v['source']}"
                      for p, v in sorted(targets.items(), key=lambda kv: SPLH_PARTS.index(kv[0])))
    if unmeasured:
        basis += (f". {len(unmeasured)} weekday{'s' if len(unmeasured) != 1 else ''} "
                  f"({', '.join(wd[:3] for wd in unmeasured)}) have no measured lunch/dinner sales split yet, so "
                  "they carry no daypart target")
    return {"available": True, "by_day": by_day, "targets": targets, "daypart_sales": daypart_sales, "hold": hold,
            "labor_target_pct": float(labor_target_pct) if labor_target_pct else None,
            "history_labor_pct": round(hist_pct, 1) if hist_pct else None, "scale": round(scale, 3),
            "with_salaries": with_salaries, "basis": basis, "split_unmeasured": unmeasured,
            # The split is never assumed any more (L-25); kept False for readers of the old field.
            "split_assumed": False}


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
        for part in SPLH_PARTS:
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

# A holiday the calendar only approximates (its date moves with a league's
# calendar — demand.APPROXIMATE_HOLIDAYS) never takes a date's name from a
# holiday that falls on its own date every year.
_APPROXIMATE = ("Super Bowl Sunday",)


def holiday_names(year: int) -> dict:
    """{iso date: [names]} — every dining holiday on each date of `year`
    (the ones marketing.get_upcoming_holidays already knows), the date's own
    fixed holiday first, then the floating ones, then the approximate.

    Two can fall on one night: Super Bowl Sunday on 2/14/27 is Valentine's
    Day, and the one-name calendar let whichever was written last overwrite
    the other (schedule audit 10/3/26 E-8)."""
    from datetime import date as _date
    fixed = {(1, 1): "New Year's Day", (2, 14): "Valentine's Day", (3, 17): "St. Patrick's Day",
             (5, 5): "Cinco de Mayo", (7, 4): "Fourth of July", (10, 31): "Halloween",
             (11, 11): "Veterans Day", (12, 24): "Christmas Eve", (12, 25): "Christmas Day",
             (12, 31): "New Year's Eve"}
    out = {}

    def add(d, name):
        names = out.setdefault(d.isoformat() if hasattr(d, "isoformat") else d, [])
        if name not in names:
            names.append(name)
    for (m, d), n in fixed.items():
        add(_date(year, m, d), n)

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
    add(nth_weekday(5, 6, 2), "Mother's Day")
    add(nth_weekday(6, 6, 3), "Father's Day")
    add(nth_weekday(11, 3, 4), "Thanksgiving")
    add(last_weekday(5, 0), "Memorial Day")
    add(nth_weekday(9, 0, 1), "Labor Day")
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
    add(_date(year, month, day), "Easter")
    add(nth_weekday(2, 6, 2), "Super Bowl Sunday")
    for names in out.values():
        names.sort(key=lambda n: n in _APPROXIMATE)       # stable: fixed, floating, then approximate
    return out


def _holiday_dates(year: int) -> dict:
    """{iso date: name} — each date's first holiday (holiday_names): the
    fixed-date one wherever two fall on one night."""
    return {d: names[0] for d, names in holiday_names(year).items()}


def last_years_holiday_night(name: str, day) -> str:
    """The ISO date `name` fell on in the year before `day` — the night a
    holiday this year is read against (Christmas Eve 12/24/26 reads
    12/24/25, a Wednesday; Thanksgiving reads last Thanksgiving) — or None
    when the calendar has no such holiday that year."""
    d = day if hasattr(day, "year") else datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
    hits = [k for k, names in holiday_names(d.year - 1).items() if name in names]
    return hits[0] if hits else None


def holiday_lift(restaurant_id, week_dates: list, db_path=DB_PATH) -> dict:
    """{date: {"name", "names", "lift_pct", "based_on", "date", "source",
    "by_name"}} for holidays in the week, with the lift THIS restaurant saw
    on that holiday last year against the median of the same weekday in the
    four weeks either side. A holiday with no sales on file last year is
    listed with lift None — a name the model can react to, never a number
    it did not measure. A date carrying two holidays (Valentine's Day and
    Super Bowl Sunday on 2/14/27) reads each one's own night last year;
    `by_name` holds each, `lift_pct` the strongest measured — one number
    for the date (E-8, PR-8), `name` the one it came from.

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
        names.update(holiday_names(y))
    hits = {d: names[d] for d in week_dates if d in names}
    if not hits:
        return {}

    def _one(d, name):
        # the holiday's own date last year, whichever weekday it fell on
        hol_date = last_years_holiday_night(name, d) or \
            (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=364)).date().isoformat()
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
        return entry
    out = {}
    try:
        for d, day_names in hits.items():
            by_name = {n: _one(d, n) for n in day_names}
            measured = [e for e in by_name.values() if e["lift_pct"] is not None]
            lead = max(measured, key=lambda e: e["lift_pct"]) if measured else by_name[day_names[0]]
            out[d] = dict(lead, names=list(day_names), by_name=by_name)
    except Exception:
        return {d: {"name": ns[0], "names": list(ns), "lift_pct": None, "based_on": None} for d, ns in hits.items()}
    return out


# ── staggered starts along the day's curve ────────────────────────────────

def stagger_same_starts(rows: list, hourly_profile: dict, min_group: int = 3, score_fn=None,
                        score_seconds: float = TRIM_SCORE_SECONDS) -> tuple:
    """When three or more people in the same role start at the same minute
    on a day whose sales curve is still climbing, keep the first and move
    the others later in 30-minute steps (at most 90), ending where they
    did. Only with a measured curve for that weekday, and never past the
    hour the curve peaks. Returns (rows, changes:[...]).

    score_fn(rows) -> float, when given, chooses each move's size among the
    legal ones (the planned step, each shorter step, or none): the one that
    costs the week the least Shift Quality, the planned step on a tie. A
    stagger that would leave a half hour the sales curve needs short backs
    off instead of opening the gap."""
    if not hourly_profile:
        return rows, []
    import time as _time
    _t0 = _time.monotonic()
    groups = {}
    for i, r in enumerate(rows):
        key = (r.get("date"), (r.get("role") or "").strip().lower(), r.get("shift_start"))
        if all(key):
            groups.setdefault(key, []).append(i)
    changes = []
    for (date, role, start), idxs in groups.items():
        if len(idxs) < min_group:
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
            e = _minutes(r.get("shift_end", ""))
            if e is None or e <= s + off + 120:
                continue      # would leave under two hours: not worth it
            if score_fn is not None and _time.monotonic() - _t0 > score_seconds:
                score_fn = None
            if score_fn is not None:
                best = None
                for step in range(off, -1, -STAGGER_STEP_MIN):
                    trial = list(rows)
                    trial[i] = dict(r, shift_start=_fmt(s + step),
                                    scheduled_hours=str(round((e - (s + step)) / 60, 1)))
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
            r["scheduled_hours"] = str(round((e - (s + off)) / 60, 1))
            note = (r.get("notes") or "").strip()
            r["notes"] = f"{note}; staggered start" if note else "staggered start"
            changes.append({"date": date, "employee": r.get("employee"), "role": r.get("role"),
                            "from": start, "to": r["shift_start"], "reason": f"{wd}'s sales climb until {peak_hour}:00"})
    return rows, changes


# ── trim to budget ─────────────────────────────────────────────────────────

def trim_to_budget(rows: list, hours_budget: float, daily_targets: dict, constraints=None, floors: dict = None,
                   splh: dict = None, rainy_dates: set = None, patio_roles: set = None,
                   tolerance: float = TRIM_TOLERANCE, score_fn=None,
                   score_seconds: float = TRIM_SCORE_SECONDS, only_dates=None) -> tuple:
    """Remove the most discretionary hours until the week is within the
    budget. Order: rows the top-up added, then the later leg of a double,
    then the latest starter of a role on the day furthest over its own
    target — preferring the daypart with the lowest sales per labor hour
    and, on a rainy day, a patio role. Never below a role floor for that
    daypart, never the last person in a role on a daypart, never a row
    already marked for review. Every removal is reported.

    score_fn(rows) -> float, when given, chooses among the few most
    discretionary candidates the one whose removal costs the week the least
    Shift Quality: the order above says which hours are optional, the score
    says which of them the floor can best spare.

    Returns (rows, trimmed:[{...}], hours_removed).
    """
    import time as _time
    _t0 = _time.monotonic()
    rows = list(rows or [])
    budget = float(hours_budget or 0)
    # A salaried person's hours are not paid from the hourly budget and are
    # never trimmed to fit it (models.salaried_staff).
    _sal = getattr(constraints, "is_salaried", None)

    def _paid(r):
        return not (_sal and _sal(r.get("employee")))
    total = sum(_hours(r) for r in rows if _paid(r))
    if budget <= 0 or total <= budget * (1 + tolerance):
        return rows, [], 0.0
    from schedule_rules import floor_for
    floors = floors or {}
    rainy = rainy_dates or set()
    patio = {p.strip().lower() for p in (patio_roles or set())}
    trimmed = []
    removed = 0.0

    def day_over(d):
        tgt = float((daily_targets or {}).get(d) or 0)
        have = sum(_hours(r) for r in rows if r.get("date") == d and id(r) not in no_show)
        return (have - tgt) if tgt else have

    def role_count(d, role, part):
        return sum(1 for r in rows if r.get("date") == d and (r.get("role") or "").strip().lower() == role
                   and _daypart(r) == part and id(r) not in no_show)

    # Rows that will not stand — a person on time off, off the roster,
    # double-booked — are not coverage and not hours anyone works; they are
    # judged before the trim so it never removes a legal row to pay for one
    # (SCHED-23). The sweep flags them afterwards.
    no_show = set()
    if constraints is not None:
        try:
            from schedule_rules import violations as _viol
            no_show = {id(rows[v["index"]]) for v in _viol(rows, constraints) if v.get("no_show")}
        except Exception:
            no_show = set()
    if no_show:
        total = sum(_hours(r) for r in rows if id(r) not in no_show and _paid(r))
        if total <= budget * (1 + tolerance):
            return rows, [], 0.0

    def person_hours(name):
        low = (name or "").strip().lower()
        return sum(_hours(x) for x in rows if (x.get("employee") or "").strip().lower() == low and id(x) not in no_show)

    def removable(r):
        if r.get("needs_review") or id(r) in no_show or not _paid(r):
            return False
        if only_dates and r.get("date") not in only_dates:
            return False          # a day the owner kept in a partial redo
        d, role, part = r.get("date"), (r.get("role") or "").strip().lower(), _daypart(r)
        if not (d and role):
            return False
        # Never somebody's only shift of the week, and never under the
        # minimum hours they asked for (SCHED-23). A row the top-up added is
        # the pipeline's own addition, so taking it back is always allowed.
        name = r.get("employee")
        low = (name or "").strip().lower()
        added = "top-up" in (r.get("notes") or "").lower()
        if not added and sum(1 for x in rows if (x.get("employee") or "").strip().lower() == low and id(x) not in no_show) <= 1:
            return False
        if constraints is not None:
            try:
                mn = constraints.min_hours(name)
            except Exception:
                mn = None
            if mn and person_hours(name) - _hours(r) < float(mn) - 0.05:
                return False
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day = r.get("day") or ""
        n = role_count(d, role, part)
        if n <= 1:
            return False
        if n - 1 < floor_for(floors, r.get("role"), day, part):
            return False
        # Never the only trained cover for a kitchen station the owner
        # requires on that daypart (kitchen_stations, 9/30/26).
        _st = getattr(constraints, "stations", None) if constraints is not None else None
        if _st:
            import kitchen_stations as _ks
            if _ks.protects([x for x in rows if id(x) not in no_show], r, _st):
                return False
        return True

    def splh_for(r):
        try:
            day = datetime.strptime(r.get("date"), "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            return None
        return ((splh or {}).get(day) or {}).get(_daypart(r), {}).get("splh")

    for _ in range(TRIM_MAX_REMOVALS):
        if total <= budget:
            break
        cands = [r for r in rows if removable(r)]
        if not cands:
            break
        by_person_day = {}
        for r in rows:
            by_person_day.setdefault((r.get("employee"), r.get("date")), []).append(r)

        def second_leg(r):
            legs = sorted(by_person_day.get((r.get("employee"), r.get("date")), []), key=lambda x: _minutes(x.get("shift_start", "")) or 0)
            return len(legs) > 1 and legs[-1] is r

        def priority(r):
            note = (r.get("notes") or "").lower()
            tier = 0 if "top-up" in note else (1 if second_leg(r) else 2)
            over = day_over(r.get("date"))
            rain = 0 if (r.get("date") in rainy and (r.get("role") or "").strip().lower() in patio) else 1
            s = splh_for(r)
            splh_rank = s if s is not None else 10 ** 9
            start = _minutes(r.get("shift_start", "")) or 0
            return (tier, rain, -over, splh_rank, -start)
        ranked = sorted(cands, key=priority)
        victim = ranked[0]
        if score_fn is not None and _time.monotonic() - _t0 > score_seconds:
            score_fn = None
        if score_fn is not None:
            tier = priority(victim)[0]
            pool = [r for r in ranked if priority(r)[0] == tier][:TRIM_SCORE_CANDIDATES]
            best = None
            for cand in pool:
                try:
                    val = score_fn([x for x in rows if x is not cand])
                except Exception:
                    continue
                if best is None or val > best[0] + 1e-9:
                    best = (val, cand)
            if best is not None:
                victim = best[1]
        h = _hours(victim)
        rows.remove(victim)
        total -= h
        removed += h
        why = ("added by the top-up" if "top-up" in (victim.get("notes") or "").lower()
               else "second leg of a double" if second_leg(victim)
               else f"latest {victim.get('role')} on a day {day_over(victim.get('date')) + h:.0f}h over its target")
        if victim.get("date") in rainy and (victim.get("role") or "").strip().lower() in patio:
            why += ", rain forecast on a patio role"
        trimmed.append({"date": victim.get("date"), "day": victim.get("day"), "employee": victim.get("employee"),
                        "role": victim.get("role"), "shift_start": victim.get("shift_start"),
                        "shift_end": victim.get("shift_end"), "hours": h, "reason": why})
    return rows, trimmed, round(removed, 1)


def trim_lines(trimmed: list, hours_removed: float, budget: float, limit: int = 6) -> list:
    if not trimmed:
        return []
    lines = [f"Trimmed {hours_removed:g}h to fit the {budget:,.0f}h budget: {len(trimmed)} shift{'s' if len(trimmed) != 1 else ''} removed."]
    for t in trimmed[:limit]:
        lines.append(f"{t.get('day') or t.get('date')} {t.get('role')} {t.get('shift_start')}–{t.get('shift_end')}: {t.get('employee')} — {t['reason']}.")
    if len(trimmed) > limit:
        lines.append(f"…and {len(trimmed) - limit} more, all listed in the review.")
    return lines
