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
  holiday_lift        — what a holiday did to THIS restaurant's sales last time
  stagger_same_starts — spread identical starts along the day's sales curve
  cost_delta          — what an edit moves in hours and dollars

Everything here is pure over the rows it is handed, except the readers that
say so. Nothing calls a model.
"""
from datetime import date, datetime, timedelta

from models import get_conn, DB_PATH

TRIM_TOLERANCE = 0.02            # a week within 2% of the budget is not trimmed
TRIM_MAX_REMOVALS = 60
TRIM_SCORE_CANDIDATES = 5       # how many equally-discretionary rows the score chooses between
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
                base_hours: dict = None, multiplier: float = 1.5, bucket=None, daily_ot_hours: float = None) -> dict:
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
    toward the weekly ceiling."""
    rates = {str(k).strip().lower(): float(v) for k, v in (role_rates or {}).items() if k and k != "_default"}
    blended = float(blended_rate or 0) or (sum(rates.values()) / len(rates) if rates else 0.0)
    try:
        daily = float(daily_ot_hours or 0)
    except (TypeError, ValueError):
        daily = 0.0
    per_person = {}
    for r in rows or []:
        n = (r.get("employee") or "").strip()
        if not n:
            continue
        rate = rates.get((r.get("role") or "").strip().lower(), blended)
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

def projected_weekly_revenue(restaurant_id, weeks: int = 8, db_path=DB_PATH) -> dict:
    """{"value", "source", "weeks"} — the median of the last `weeks` complete
    weeks of daily sales, so the budget follows how this restaurant
    actually earns rather than a twelfth of a monthly target. None when
    fewer than three complete weeks exist."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND sales IS NOT NULL AND sales > 0 "
            "AND date >= date('now', ?) ORDER BY date", (restaurant_id, f"-{int(weeks) * 7 + 7} days")).fetchall()
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
    return {"value": round(med, 0), "source": f"median of the last {len(complete)} complete weeks", "weeks": len(complete)}


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
            "AND sales IS NOT NULL AND sales > 0 AND date >= date('now', ?)", (restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
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
            part = _daypart(sh)
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
                day[part] = {"sales": round(s, 0), "hours": round(h, 1), "splh": round(s / h, 0)}
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
    """{date: {"name", "lift_pct", "based_on"}} for holidays in the week,
    with the lift THIS restaurant saw on that holiday last year against the
    median of the same weekday in the four weeks either side. A holiday
    with no sales on file last year is listed with lift None — a name the
    model can react to, never a number it did not measure."""
    if not week_dates:
        return {}
    years = {int(d[:4]) for d in week_dates}
    names = {}
    for y in years:
        names.update(_holiday_dates(y))
    hits = {d: names[d] for d in week_dates if d in names}
    if not hits:
        return {}
    conn = get_conn(db_path)
    out = {}
    try:
        for d, name in hits.items():
            last = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=364)).date()   # same weekday last year
            # the holiday's own date last year, whichever weekday it fell on
            prior_dates = [k for k, n in _holiday_dates(last.year).items() if n == name]
            hol_date = prior_dates[0] if prior_dates else last.isoformat()
            row = conn.execute("SELECT sales FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                               (restaurant_id, hol_date)).fetchone()
            entry = {"name": name, "lift_pct": None, "based_on": None}
            if row and row["sales"]:
                hd = datetime.strptime(hol_date, "%Y-%m-%d").date()
                same = conn.execute(
                    "SELECT sales FROM labor_daily_history WHERE restaurant_id=? AND date BETWEEN ? AND ? "
                    "AND date<>? AND day_of_week=? AND sales > 0",
                    (restaurant_id, (hd - timedelta(days=28)).isoformat(), (hd + timedelta(days=28)).isoformat(),
                     hol_date, hd.strftime("%A"))).fetchall()
                vals = sorted(float(r["sales"]) for r in same)
                if len(vals) >= 3:
                    med = vals[len(vals) // 2]
                    if med > 0:
                        entry["lift_pct"] = int(round((float(row["sales"]) / med - 1) * 100))
                        entry["based_on"] = f"{name} {hd.year}: ${float(row['sales']):,.0f} against a typical {hd.strftime('%A')} of ${med:,.0f}"
            out[d] = entry
    except Exception:
        return {d: {"name": n, "lift_pct": None, "based_on": None} for d, n in hits.items()}
    finally:
        conn.close()
    return out


# ── staggered starts along the day's curve ────────────────────────────────

def stagger_same_starts(rows: list, hourly_profile: dict, min_group: int = 3) -> tuple:
    """When three or more people in the same role start at the same minute
    on a day whose sales curve is still climbing, keep the first and move
    the others later in 30-minute steps (at most 90), ending where they
    did. Only with a measured curve for that weekday, and never past the
    hour the curve peaks. Returns (rows, changes:[...])."""
    if not hourly_profile:
        return rows, []
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
                   tolerance: float = TRIM_TOLERANCE, score_fn=None) -> tuple:
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
    rows = list(rows or [])
    budget = float(hours_budget or 0)
    total = sum(_hours(r) for r in rows)
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
        total = sum(_hours(r) for r in rows if id(r) not in no_show)
        if total <= budget * (1 + tolerance):
            return rows, [], 0.0

    def person_hours(name):
        low = (name or "").strip().lower()
        return sum(_hours(x) for x in rows if (x.get("employee") or "").strip().lower() == low and id(x) not in no_show)

    def removable(r):
        if r.get("needs_review") or id(r) in no_show:
            return False
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
