"""
labor_live.py — labor while the day can still be changed (schedule audit
10/10/26 MARKET-8, MARKET-24, roadmap RM-16).

The intraday pulse compared sales only, and the one staffing move it ever
made was "let someone go at 8pm" — the same 8pm for a lunch-only cafe and a
2am bar. A manager at 6pm could not see "labor is 31% of sales so far,
projected 28% at close against your 24%".

read()      the live read, every 15 minutes while the restaurant is open:
            labor to date (each punch so far — clock-in to now, or to the
            clock-out — at the punch's own wage, else the person's, else the
            role's rate; salaried people out of the hourly figure, as the
            schedule's hourly budget is), the scheduled rest of the day
            (each published shift still ahead, or the rest of the shift of
            someone on the clock), against sales so far (the POS's live
            figure) and the re-forecast close: sales so far over the share of
            a typical same weekday done by now (this restaurant's own hourly
            history), else the day's forecast while too little of the day has
            traded to project from. The owner's labor % is all-in (the day's
            share of the salaries on top), judged against their target; the
            hourly % stands beside it on the hourly basis.
moves()     the legal, priced moves worth making now — a cut, an extension,
            a call-in — derived from the restaurant's OWN plan, curve and
            close, half hour by half hour: each role's crew on the published
            schedule against the crew the day's pace needs (the planned crew
            scaled by projected/typical sales, never under the role's cut
            floor, the curve's dead tail at the floor). A cut is a person
            whose leaving at the earliest slot from now+LEAD keeps every
            later slot at or above need; an extension is a person whose shift
            ends inside a run of short slots; a call-in the best legal
            standby for a short run nobody on can stay for. Every move is
            judged by the one legality engine (schedule_rules.judge_change on
            the whole week: a manager every minute, floors, closers, rest,
            hours, minors), never adds overtime while a same-role teammate
            has room, carries the trim guard for a cut, and is priced — wages
            plus what predictability pay a law makes it owe
            (predictability_pay, where it exists).
run_live_labor()  the 15-minute job: the read kept for the screens, the
            single best move offered to the managers in one tap — or, for an
            extension or a call-in only, sent to the staff member without the
            tap once AUTOPILOT is earned: the owner turned autopilot on AND
            the managers approved EARN_APPROVALS of that kind themselves in
            EARN_WINDOW_DAYS since the last reversal (automation_trust scope
            "live_move"); one reversal lapses it at once. A cut always waits
            for a manager's tap: it changes somebody's pay without asking
            them.
"""
import json
import math
from datetime import date, datetime, timedelta

import models as _models_mod

SLOT = 30
CADENCE_MINUTES = 15
LEAD_MINUTES = 15            # a move starts at least this far ahead, so the person can be told
MIN_MOVE_HOURS = 0.5         # a cut or extension shorter than this is not worth a tap
MIN_CALL_IN_HOURS = 2.0      # nobody is called in for less
MIN_SHARE_FOR_PACE = 0.15    # of a typical day's sales done before the close is projected from pace
PACE_BEHIND_PCT = 10         # behind by at least this before a cut is looked for
PACE_AHEAD_PCT = 15          # ahead by at least this before an extension or call-in is
MAX_MOVE_PUSHES_PER_DAY = 3
EARN_APPROVALS = 6
EARN_WINDOW_DAYS = 60
SCOPE = "live_move"
KINDS = ("cut", "extend", "call_in")
AUTO_KINDS = ("extend", "call_in")       # a cut never acts alone
AUTO_ACTOR = "Cavnar AI (autopilot)"
READ_FRESH_MINUTES = 15
# The pre-dinner pulse's cut time when a restaurant has neither hours nor a
# measured curve to derive its own from (cut_minute). Said, never silent.
FALLBACK_CUT_HOUR = 20


def get_conn(db_path=None):
    return _models_mod.get_conn(db_path) if db_path else _models_mod.get_conn()


def init_labor_live(db_path=None):
    """Boot DDL (models.init_db) — never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS live_labor_moves (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            business_date TEXT NOT NULL,
            kind          TEXT NOT NULL,
            move_key      TEXT NOT NULL,
            employee      TEXT,
            role          TEXT,
            start_label   TEXT,
            end_label     TEXT,
            hours         REAL,
            dollars       REAL,
            premium       REAL,
            text          TEXT,
            payload_json  TEXT,
            status        TEXT NOT NULL DEFAULT 'suggested',
            how           TEXT,
            request_id    INTEGER,
            pushed_at     TEXT,
            decided_at    TEXT,
            decided_by    TEXT,
            reversed_at   TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (restaurant_id, move_key)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_moves_day ON live_labor_moves (restaurant_id, business_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_moves_date ON live_labor_moves (business_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS live_labor_reads (
            restaurant_id INTEGER NOT NULL,
            business_date TEXT NOT NULL,
            read_at       TEXT NOT NULL,
            body_json     TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, business_date)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_reads_date ON live_labor_reads (business_date)")
        conn.commit()
    finally:
        conn.close()


# ── small helpers ───────────────────────────────────────────────────────────

def _median(vals):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _local(ts, tz):
    """A POS stamp as the restaurant's naive local time: an offset stamp
    (Toast) is converted, a bare one (RPOWER: local wall clock) kept."""
    if not ts:
        return None
    raw = str(ts).replace("Z", "+00:00")
    if raw.endswith("+0000"):
        raw = raw[:-5] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw[:32])
    except ValueError:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None and tz is not None:
        dt = dt.astimezone(tz).replace(tzinfo=None)
    return dt.replace(tzinfo=None)


def _clock(dt) -> str:
    h = dt.hour % 12 or 12
    return f"{h}{':%02d' % dt.minute if dt.minute else ''}{'am' if dt.hour < 12 else 'pm'}"


def _slot_of(dt, day) -> int:
    """Minutes since the business date's midnight, floored to the slot."""
    m = int((dt - datetime.combine(day, datetime.min.time())).total_seconds() // 60)
    return m - (m % SLOT)


def _ceil_slot(dt, day) -> int:
    m = int(math.ceil((dt - datetime.combine(day, datetime.min.time())).total_seconds() / 60.0))
    return m if m % SLOT == 0 else m + (SLOT - m % SLOT)


def _at(day, minute) -> datetime:
    return datetime.combine(day, datetime.min.time()) + timedelta(minutes=minute)


def _key(name) -> str:
    import staff_settings as _ss
    return _ss.name_key(name)


# ── the day's inputs ────────────────────────────────────────────────────────

def _today_rows(restaurant_id, day, db_path=None) -> list:
    """Today's published shifts as they will really run (a dropped or covered
    holder's row left unheld — shift_requests.effective_rows), each with its
    wall-clock span. Salaried rows kept, marked."""
    import intraday
    import schedule_time as _st
    rows = intraday.published_rows(restaurant_id, day, **({"db_path": db_path} if db_path else {}))
    try:
        import shift_requests
        rows = shift_requests.effective_rows(restaurant_id, rows, **({"db_path": db_path} if db_path else {}))
    except Exception:
        pass
    out = []
    for r in rows:
        r = dict(r, date=day.isoformat())
        a, z = _st.shift_span(r)
        if a is None or not (r.get("employee") or "").strip():
            continue
        out.append(dict(r, _a=a, _z=z))
    return out


def _week_rows(restaurant_id, day, db_path=None) -> list:
    """The whole published week holding `day` — what the legality engine
    judges a move against (hours, rest and overtime are week rules)."""
    import schedule_shifts
    return schedule_shifts.live_week_rows(restaurant_id, day, db_path=db_path)


def _rates(restaurant_id, restaurant):
    import labor as _lab
    from models import get_role_rates
    rates = get_role_rates(restaurant_id) or {}
    try:
        ppl, typ = _lab.person_rate_book(restaurant_id)
    except Exception:
        ppl, typ = {}, {}
    fallback = float(rates.get("_default") or getattr(restaurant, "hourly_rate", None) or 0) or None
    return rates, ppl, typ, fallback


def _rate(rates_t, name, role, wage=None):
    import labor as _lab
    rates, ppl, typ, fallback = rates_t
    rate, src = _lab._shift_rate_source({"employee": name, "role": role, "pay_rate": wage}, rates, fallback or 0.0,
                                        ppl, typ)
    return (float(rate) if rate else None), src


def _done_at(pts, t):
    """A past day's sales done by business-hour `t` (fractional) from its
    hourly captures [(hour, net_sales)] in order: the last capture at or
    before `t`, carried toward the next one in proportion to the time
    between them. None before the day's first capture."""
    before = [(hh, v) for hh, v in pts if hh <= t]
    if not before:
        return None
    h0, v0 = before[-1]
    after = [(hh, v) for hh, v in pts if hh > t]
    if not after or t <= h0:
        return v0
    h1, v1 = after[0]
    return v0 + (v1 - v0) * (t - h0) / float(h1 - h0)


def _sales(restaurant, day, now_local, live=True, db_path=None) -> dict:
    """{so_far, basis, projected, projected_basis, typical, pace_pct, share,
    covers_booked}. `so_far` None when the POS cannot be read during
    service and nothing was captured today."""
    import intraday
    import pos
    rid = restaurant.id
    weekday = day.strftime("%A")
    hour = now_local.hour + 24 * max(0, (now_local.date() - day).days)
    out = {"so_far": None, "basis": None, "projected": None, "projected_basis": None, "typical": None,
           "pace_pct": None, "share": None, "covers_booked": None}
    # The moment `so_far` stands at, in business hours (blind re-audit
    # 10/20/26 R9-08): live sales are read to the minute, while each past
    # day's history is one capture an hour (intraday, filed under its hour).
    # The share done by then is read at that same moment — between a past
    # day's two captures, in proportion — not at the hour's HH:00 capture,
    # which at 7:50pm set 7:50's sales against 7:00's share and overstated
    # the night's pace by up to an hour's trade.
    at_t = float(hour)
    if live:
        try:
            net, provider = pos.fetch_sales_today(rid, day)
            out.update(so_far=round(float(net), 2), basis=f"live from {provider}")
            at_t = hour + now_local.minute / 60.0
        except Exception:
            pass
    conn = get_conn(db_path)
    try:
        if out["so_far"] is None:
            r = conn.execute("SELECT net_sales, captured_hour FROM pos_intraday WHERE restaurant_id=? AND "
                             "business_date=? AND captured_hour<=? ORDER BY captured_hour DESC LIMIT 1",
                             (rid, day.isoformat(), hour)).fetchone()
            if r:
                out.update(so_far=round(float(r["net_sales"]), 2), basis=f"the {r['captured_hour'] % 24}:00 reading")
                # A captured reading is that hour's, so is its share.
                at_t = float(int(r["captured_hour"]))
        # Same weekday's history: each past day's reading at this hour and its
        # last reading (the day's total), the share done by now per day.
        hist = conn.execute(
            "SELECT business_date, captured_hour, net_sales FROM pos_intraday WHERE restaurant_id=? AND weekday=? "
            "AND business_date<? AND business_date>=? ORDER BY business_date, captured_hour",
            (rid, weekday, day.isoformat(), (day - timedelta(weeks=8)).isoformat())).fetchall()
    finally:
        conn.close()
    by_day = {}
    for h in hist:
        by_day.setdefault(h["business_date"], []).append((int(h["captured_hour"]), float(h["net_sales"] or 0)))
    shares, closes = [], []
    for _d, pts in by_day.items():
        total = pts[-1][1]
        if total > 0:
            closes.append(total)
            done = _done_at(pts, at_t)
            if done is not None:
                shares.append(done / total)
    share = _median(shares) if len(shares) >= intraday.MIN_PROFILE_SAMPLES else None
    if share is None:
        # The restaurant's measured sales curve (tickets first) — the share of
        # a typical same weekday's sales done before this hour.
        try:
            import schedule_economics as _se
            import schedule_time as _st
            curve = _se.hourly_profile(rid).get(weekday) or {}
            if curve:
                bh = _st.business_hour(now_local.hour)
                frac = now_local.minute / 60.0
                share = sum(v for h, v in curve.items() if _st.business_hour(int(h)) < bh) + \
                    frac * float(curve.get(now_local.hour, curve.get(str(now_local.hour), 0)) or 0)
        except Exception:
            share = None
    out["share"] = round(share, 3) if share is not None else None
    fc = None
    try:
        import demand
        fc = demand.forecast_day(rid, day, **({"db_path": db_path} if db_path else {}))
    except Exception:
        fc = None
    typical = float(fc["sales"]) if fc and fc.get("available") and fc.get("sales") else _median(closes)
    out["typical"] = round(typical, 2) if typical else None
    if out["so_far"] is not None and share and share >= MIN_SHARE_FOR_PACE:
        out.update(projected=round(out["so_far"] / share, 2),
                   projected_basis=f"sales so far over the {round(share * 100)}% of a typical {weekday} done by now")
    elif typical:
        out.update(projected=round(max(typical, out["so_far"] or 0), 2),
                   projected_basis=f"the {weekday} forecast — too little of the day has traded to project from")
    # The weather nowcast (MARKET-24): today's NWS forecast from the copy on
    # file (never a fetch), and what rain does to THIS restaurant's sales —
    # measured from its own nights (schedule_economics._rain_effect), never
    # assumed. Applied only while the close comes from the forecast: once
    # the day's own pace projects it, today's weather is already in it.
    try:
        import weather
        import schedule_economics as _se_w
        fc_rows = weather.cached_forecast_for_week(restaurant, [day.isoformat()])
        wet = fc_rows and not fc_rows[0].get("stale") and (fc_rows[0].get("precip_pct") or 0) >= 50
        eff = _se_w._rain_effect(rid, **({"db_path": db_path} if db_path else {})) if wet else None
        if wet:
            out["weather"] = {"precip_pct": fc_rows[0].get("precip_pct"),
                              "rain_lift_pct": float(eff["median_lift_pct"]) if eff else None}
        if eff and out.get("projected") and "forecast" in (out.get("projected_basis") or ""):
            lift = float(eff["median_lift_pct"]) / 100.0
            rest = max(0.0, out["projected"] - (out["so_far"] or 0))
            out["projected"] = round((out["so_far"] or 0) + rest * (1 + lift), 2)
            out["projected_basis"] += (f", moved {eff['median_lift_pct']:+g}% for the rain forecast "
                                       f"(what rain does here, measured)")
    except Exception:
        pass
    if out["projected"] and out["typical"]:
        out["pace_pct"] = round((out["projected"] / out["typical"] - 1) * 100, 1)
    try:
        import demand_signals as _ds
        sig = _ds.by_date(rid, [day.isoformat()], **({"db_path": db_path} if db_path else {})).get(day.isoformat())
        if sig and sig.get("covers"):
            out["covers_booked"] = int(sig["covers"])
    except Exception:
        pass
    return out


def _labor(restaurant, day, now_local, rows, live=True, rates_t=None) -> dict:
    """Labor to date and the scheduled rest of the day, hourly (salaried out)."""
    import attendance
    import pos
    from models import salaried_name_key
    from time_utils import restaurant_tz
    rid = restaurant.id
    tz = restaurant_tz(restaurant)
    sal = attendance.salaried_keys(restaurant)
    rates_t = rates_t or _rates(rid, restaurant)
    punches, basis = None, "punches"
    if live:
        try:
            punches, _prov = pos.fetch_clock_ins_today(rid, day)
        except Exception:
            punches = None
    if punches is None:
        basis = "schedule"
    to_date = hours_to_date = rest = rest_hours = 0.0
    on_now, clocked_out, unscheduled = [], [], []
    by_person = {}
    for r in rows:
        if salaried_name_key(r["employee"]) in sal:
            continue
        by_person.setdefault(_key(r["employee"]), []).append(r)
    seen = set()
    if punches is not None:
        try:
            import people as _people
            canon = _people.canonical_names(rid, [p.get("employee") for p in punches if p.get("employee")])
        except Exception:
            canon = {}
        for p in punches:
            name = canon.get(p.get("employee"), p.get("employee")) or ""
            if not name or salaried_name_key(name) in sal:
                continue
            a = _local(p.get("clocked_in_at"), tz)
            if a is None:
                continue
            z = _local(p.get("clocked_out_at"), tz)
            end = min(z or now_local, now_local)
            hrs = max(0.0, (end - a).total_seconds() / 3600.0)
            # Payroll's own hours once the punch is closed, else the clock
            # less the unpaid break and meal minutes the POS has recorded —
            # the night's final figure never costs a break, so live labor
            # must not either (audit #23 A3-15).
            try:
                paid = float(p.get("paid_hours") or 0)
                brk = float(p.get("break_minutes") or 0)
            except (TypeError, ValueError):
                paid, brk = 0.0, 0.0
            if z is not None and z <= now_local and paid > 0:
                hrs = paid
            elif brk > 0:
                hrs = max(0.0, hrs - brk / 60.0)
            mine = by_person.get(_key(name)) or []
            role = (mine[0].get("role") if mine else None) or p.get("role") or ""
            rate, _src = _rate(rates_t, name, role, p.get("hourly_wage"))
            to_date += hrs * (rate or 0)
            hours_to_date += hrs
            if z is None:
                seen.add(_key(name))
                sched = max((x["_z"] for x in mine), default=None)
                if sched is None:
                    unscheduled.append({"employee": name, "role": role, "since": _clock(a)})
                elif sched > now_local:
                    rest_h = (sched - now_local).total_seconds() / 3600.0
                    rest += rest_h * (rate or 0)
                    rest_hours += rest_h
                on_now.append({"employee": name, "role": role, "since": _clock(a),
                               "until": _clock(sched) if sched else None})
            else:
                seen.add(_key(name) + "|out")
                clocked_out.append({"employee": name, "role": role, "out": _clock(z)})
    for k, mine in by_person.items():
        for x in mine:
            if x["_z"] <= now_local:
                if punches is None and x["_a"] < now_local:
                    hrs = (x["_z"] - x["_a"]).total_seconds() / 3600.0
                    rate, _s = _rate(rates_t, x["employee"], x.get("role"))
                    to_date += hrs * (rate or 0)
                    hours_to_date += hrs
                continue
            if k in seen:
                continue                     # on the clock: the rest of their shift is counted above
            if punches is not None and (k + "|out") in seen and x["_a"] <= now_local:
                continue                     # clocked out of this shift already
            start = max(x["_a"], now_local)
            rate, _s = _rate(rates_t, x["employee"], x.get("role"))
            if punches is None and x["_a"] < now_local:
                hrs = (now_local - x["_a"]).total_seconds() / 3600.0
                to_date += hrs * (rate or 0)
                hours_to_date += hrs
            h = (x["_z"] - start).total_seconds() / 3600.0
            rest += h * (rate or 0)
            rest_hours += h
    return {"to_date": round(to_date, 2), "hours_to_date": round(hours_to_date, 2), "rest": round(rest, 2),
            "rest_hours": round(rest_hours, 2), "projected": round(to_date + rest, 2), "basis": basis,
            "on_now": on_now, "clocked_out": clocked_out, "unscheduled": unscheduled}


def _pct(a, b):
    return round(a / b * 100.0, 1) if (a is not None and b) else None


def read(restaurant, now_local=None, live=True, db_path=None) -> dict:
    """The live labor read (see the module note). {available, reason?,
    business_date, as_of, sales, labor, pct: {hourly_so_far,
    hourly_projected, all_in_projected, target, over_by, target_label},
    close, cut_at, moves}."""
    import time_utils as _tu
    now_local = now_local or _tu.restaurant_now(restaurant, naive=True)
    day = _tu.business_date(restaurant, now_local)
    rows = _today_rows(restaurant.id, day, db_path)
    window = _tu.service_window(restaurant, day)
    out = {"available": True, "business_date": day.isoformat(), "as_of": f"{_tu.mdy(day)} {_clock(now_local)}",
           "close": _clock(window[1]) if window else None}
    if not rows:
        return dict(out, available=False, reason="no published schedule covers today")
    rates_t = _rates(restaurant.id, restaurant)
    sales = _sales(restaurant, day, now_local, live=live, db_path=db_path)
    lab = _labor(restaurant, day, now_local, rows, live=live, rates_t=rates_t)
    try:
        from models import salaried_day_share
        sal_day = float(salaried_day_share(restaurant) or 0)
    except Exception:
        sal_day = 0.0
    # The ONE target read (thresholds.target_for): the owner's goal first,
    # its label, and whether it may raise anything - on Cavnar AI's starting
    # target nothing is "over" enough to suggest a cut (pace still can).
    try:
        import thresholds
        tf = thresholds.target_for(restaurant, "labor") or {}
        target = float(tf["pct"]) if tf.get("pct") is not None else None
        target_label = tf.get("label") or "your target"
        target_alerts = bool(tf.get("alerts_allowed", True))
    except Exception:
        target, target_label, target_alerts = None, "your target", False
    proj = sales.get("projected")
    all_in = _pct(lab["projected"] + sal_day, proj)
    out.update(sales=sales, labor=dict(lab, salaried_day=round(sal_day, 2)),
               pct={"hourly_so_far": _pct(lab["to_date"], sales.get("so_far")),
                    "hourly_projected": _pct(lab["projected"], proj),
                    "all_in_projected": all_in, "target": target, "target_label": target_label,
                    "target_alerts": target_alerts,
                    "over_by": round(all_in - target, 1) if (all_in is not None and target is not None) else None,
                    "basis": "all-in (hourly plus the day's share of salaries) against your target; hourly beside it"})
    out["sentence"] = _sentence(out)
    return out


def _sentence(rd) -> str:
    p, s = rd.get("pct") or {}, rd.get("sales") or {}
    if p.get("hourly_so_far") is None and p.get("all_in_projected") is None:
        return "Labor so far can't be set against sales yet — the POS hasn't reported today's sales."
    bits = []
    if p.get("hourly_so_far") is not None:
        bits.append(f"Labor is {p['hourly_so_far']:g}% of sales so far")
    if p.get("all_in_projected") is not None:
        bits.append(f"projected {p['all_in_projected']:g}% all-in at close"
                    + (f" against {p['target_label']} of {p['target']:g}%" if p.get("target") is not None else ""))
    line = ", ".join(bits) + "."
    if s.get("pace_pct") is not None:
        line += f" Sales are running {abs(s['pace_pct']):g}% {'behind' if s['pace_pct'] < 0 else 'ahead of'} a typical day."
    return line


# ── the restaurant's own cut time and the slot-by-slot need ────────────────

def cut_minute(restaurant, day, rows=None, now_local=None) -> tuple:
    """(minute, basis) — when a slow night's first cut is due, from this
    restaurant's own curve and close, not a fixed 8pm: the first half hour
    after the evening's busiest that falls under half its sales rate
    (staffing_curve.half_hour_shares of the weekday's measured curve), held
    to at least an hour before the close; with no measured curve, two hours
    before the close; with neither, FALLBACK_CUT_HOUR (said in `basis`)."""
    import schedule_economics as _se
    import staffing_curve as _sc
    import time_utils as _tu
    window = _tu.service_window(restaurant, day)
    base = datetime.combine(day, datetime.min.time())
    close_m = int((window[1] - base).total_seconds() // 60) if window else None
    try:
        curve = _se.hourly_profile(restaurant.id).get(day.strftime("%A")) or {}
    except Exception:
        curve = {}
    shares = _sc.half_hour_shares(curve) if curve else {}
    evening = {m: v for m, v in shares.items() if m >= 15 * 60}
    pick = None
    if evening:
        peak_m = max(evening, key=lambda m: evening[m])
        peak = evening[peak_m]
        tail = [m for m in sorted(evening) if m > peak_m and evening[m] < 0.5 * peak]
        if tail:
            pick, basis = tail[0], "the first half hour after tonight's peak that runs under half its sales"
    if pick is None and close_m is not None:
        pick, basis = close_m - 120, "two hours before your close (no measured sales curve yet)"
    if pick is None:
        return FALLBACK_CUT_HOUR * 60, f"{FALLBACK_CUT_HOUR % 12}pm — no hours or measured sales curve on file"
    if close_m is not None:
        pick = min(pick, close_m - 60)
    if now_local is not None:
        pick = max(pick, _ceil_slot(now_local + timedelta(minutes=LEAD_MINUTES), day))
    return int(pick), basis


def _crew(rows, day, salaried):
    """{role: {slot: people on}} from today's rows (salaried and empty out)."""
    from models import salaried_name_key
    out = {}
    for r in rows:
        if not (r.get("employee") or "").strip() or salaried_name_key(r["employee"]) in salaried:
            continue
        a, z = _slot_of(r["_a"], day), _ceil_slot(r["_z"], day)
        role = (r.get("role") or "Staff").strip()
        for m in range(a, z, SLOT):
            out.setdefault(role, {})[m] = out.setdefault(role, {}).get(m, 0) + 1
    return out


def _need(restaurant, day, crew, factor, tail_slots):
    """{role: {slot: people needed}}: the planned crew scaled by the day's
    pace, never under the role's cut floor; the curve's dead tail (a half
    hour under staffing_curve.MIN_SLOT_RATIO of the peak) at the floor."""
    import schedule_rules as _sr
    import schedule_time as _st
    weekday = day.strftime("%A")
    floors = _sr.effective_role_floors(restaurant, day)
    minimums = _sr.role_minimums(restaurant)
    out = {}
    for role, slots in crew.items():
        for m, n in slots.items():
            part = "morning" if (m % (24 * 60)) < _st.daypart_split() else "night"
            fl = _sr.cut_floor(restaurant, role, weekday, part, floors=floors, minimums=minimums)
            want = fl if m in tail_slots else max(fl, int(math.ceil(n * factor - 0.05)))
            out.setdefault(role, {})[m] = want
    return out


def _tail_slots(restaurant, day) -> set:
    import schedule_economics as _se
    import staffing_curve as _sc
    try:
        curve = _se.hourly_profile(restaurant.id).get(day.strftime("%A")) or {}
    except Exception:
        return set()
    shares = _sc.half_hour_shares(curve) if curve else {}
    if not shares:
        return set()
    peak = max(shares.values()) or 0
    last = max(shares)
    # Only the run at the END of the day: a slow 3pm is not the night's tail.
    out = set()
    for m in sorted(shares, reverse=True):
        if peak and shares[m] / peak < _sc.MIN_SLOT_RATIO:
            out.add(m)
        else:
            break
    out |= {m for m in range(last + SLOT, last + 6 * 60, SLOT)}
    return out


# ── legality and price ──────────────────────────────────────────────────────

def _judge(restaurant, week, after, takers, c=None):
    import schedule_rules as _sr
    import shift_requests as _srq
    c = c or _srq._week_constraints(restaurant.id, week)
    return _sr.judge_change(week, after, c, takers=takers), c


def _find_row(week, row):
    for i, r in enumerate(week):
        if (r.get("date") or "")[:10] == row["date"] and _key(r.get("employee")) == _key(row.get("listed_as")
                                                                                         or row["employee"]) \
                and (r.get("shift_start") or "").strip() == (row.get("shift_start") or "").strip():
            return i
    return None


def _premium(restaurant_id, before, after, kind, row=None, hours=0.0, shift_hours=0.0):
    """Predictability pay the move would owe under a law covering the
    restaurant (J1's predictability_pay, when present): {amount, law,
    text} or None. Without the module, the owner's notice rule is said as
    a caveat, never summed."""
    try:
        import predictability_pay as _pp
    except ImportError:
        _pp = None
    if _pp is not None:
        try:
            if kind == "cut" and row is not None:
                got = _pp.cut_premium(restaurant_id, row["employee"], row.get("role") or "", row["date"], hours,
                                      shift_hours)
                return {"amount": float(got.get("amount") or 0), "law": got.get("law")}
            pv = _pp.preview(restaurant_id, before, after)
            if pv.get("covered"):
                return {"amount": float(pv.get("total") or 0), "law": pv.get("law"), "text": pv.get("text")}
        except Exception as e:
            print(f"[labor_live] predictability pay not priced rid={restaurant_id}: {e!r}")
        return None
    try:
        import schedule_rules as _sr
        if _sr.compliance(restaurant_id).get("notice_days"):
            return {"amount": None, "law": None,
                    "text": "a same-day change may owe predictability pay under your notice rule — check with counsel"}
    except Exception:
        pass
    return None


def moves(restaurant, rd=None, now_local=None, db_path=None, limit=3) -> list:
    """The legal, priced moves worth making now, best first (see the module
    note). Each {kind, key, employee, role, at, until, start_label,
    end_label, hours, dollars, premium, net, text, caution, payload}."""
    import time_utils as _tu
    import attendance
    import schedule_rules as _sr
    import schedule_time as _st
    now_local = now_local or _tu.restaurant_now(restaurant, naive=True)
    rd = rd or read(restaurant, now_local=now_local, db_path=db_path)
    if not rd.get("available"):
        return []
    s = rd.get("sales") or {}
    pace = s.get("pace_pct")
    over = (rd.get("pct") or {}).get("over_by")
    import floor_settings
    margin = float(floor_settings.get(restaurant).get("live_target_margin_pts") or 0)
    day = date.fromisoformat(rd["business_date"])
    rows = _today_rows(restaurant.id, day, db_path)
    sal = attendance.salaried_keys(restaurant)
    crew = _crew(rows, day, sal)
    if not crew or not s.get("projected") or not s.get("typical"):
        return []
    factor = s["projected"] / s["typical"]
    first = _ceil_slot(now_local + timedelta(minutes=LEAD_MINUTES), day)
    week = _week_rows(restaurant.id, day, db_path)
    out = []
    rates_t = _rates(restaurant.id, restaurant)
    behind = pace is not None and pace <= -PACE_BEHIND_PCT
    over_target = over is not None and over > margin and (rd.get("pct") or {}).get("target_alerts", True)
    if behind or over_target:
        out += _cuts(restaurant, day, now_local, rows, week, crew, min(factor, 1.0), first, rates_t, sal, db_path)
    if pace is not None and pace >= PACE_AHEAD_PCT:
        out += _adds(restaurant, day, now_local, rows, week, crew, factor, first, rates_t, sal, db_path)
    out.sort(key=lambda m: (-(m.get("net") or 0) if m["kind"] == "cut" else -(m.get("hours") or 0)))
    return out[:limit]


def _cuts(restaurant, day, now_local, rows, week, crew, factor, first, rates_t, sal, db_path):
    import schedule_time as _st
    from models import salaried_name_key
    need = _need(restaurant, day, crew, factor, _tail_slots(restaurant, day))
    weekday = day.strftime("%A")
    try:
        import strategy_jobs
        if strategy_jobs._pulse_suppressed(restaurant, day, **({"db_path": db_path} if db_path else {})):
            return []
    except Exception:
        pass
    found = []
    c = None
    for r in sorted(rows, key=lambda x: (x["_a"], x["_z"]), reverse=True):
        name = r["employee"]
        if salaried_name_key(name) in sal:
            continue
        role = (r.get("role") or "Staff").strip()
        end = _ceil_slot(r["_z"], day)
        lo = max(first, _slot_of(r["_a"], day))
        cut = None
        for m in range(lo, end - int(MIN_MOVE_HOURS * 60) + 1, SLOT):
            if all(crew.get(role, {}).get(t, 0) - 1 >= need.get(role, {}).get(t, 0) for t in range(m, end, SLOT)):
                cut = m
                break
        if cut is None:
            continue
        cut_dt = _at(day, cut)
        hours = round((r["_z"] - cut_dt).total_seconds() / 3600.0, 2)
        if hours < MIN_MOVE_HOURS:
            continue
        # Trim guard: a campaign filling tonight suppresses; a complaint
        # cluster on this weekday's daypart is said beside the cut.
        part = "morning" if (cut % (24 * 60)) < _st.daypart_split() else "night"
        try:
            import staffing_signals as _sig
            guard = _sig.trim_guard(restaurant.id, weekday, daypart=part, on_date=day)
        except Exception:
            guard = {}
        if guard.get("suppress"):
            return []
        i = _find_row(week, r)
        if i is None:
            continue
        after = [dict(x) for x in week]
        _st.retime(after[i], None, cut)
        problems, c = _judge(restaurant, week, after, [], c)
        if problems:
            continue
        rate, _src = _rate(rates_t, name, role)
        dollars = round(hours * (rate or 0), 2) if rate else None
        shift_h = round((r["_z"] - r["_a"]).total_seconds() / 3600.0, 2)
        prem = _premium(restaurant.id, week, after, "cut", row=dict(r, date=day.isoformat()), hours=hours,
                        shift_hours=shift_h)
        owed = float((prem or {}).get("amount") or 0)
        net = round((dollars or 0) - owed, 2) if dollars is not None else None
        text = (f"Let {name} ({role}) go at {_clock(cut_dt)} instead of {_clock(r['_z'])}: "
                f"saves {hours:g}h" + (f" (~${dollars:,.0f})" if dollars else "")
                + (f", less ${owed:,.0f} predictability pay under {prem['law']}" if owed and prem.get("law") else "")
                + (f" — {prem['text']}" if prem and prem.get("text") and not owed else "")
                + f". {role} stays at or above what tonight's pace needs until close.")
        if guard.get("caution"):
            text += " " + guard["caution"]
        found.append({"kind": "cut", "employee": name, "role": role, "date": day.isoformat(),
                      "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"),
                      "at": cut, "start_label": _clock(cut_dt), "end_label": _clock(r["_z"]), "hours": hours,
                      "dollars": dollars, "premium": owed or None, "net": net, "text": text,
                      "caution": guard.get("caution"),
                      "key": f"cut:{day.isoformat()}:{_key(name)}:{r.get('shift_start')}"})
        # One cut per role at a time: the next is judged after this one lands.
        crew = {k: dict(v) for k, v in crew.items()}
        for t in range(cut, end, SLOT):
            crew[role][t] = crew[role].get(t, 0) - 1
    return found


def _short_runs(crew, need, first):
    """{role: [(start, end)]} — contiguous slots from `first` where the pace
    needs more people than are on."""
    out = {}
    for role, slots in need.items():
        run = None
        for m in sorted(slots):
            if m < first:
                continue
            short = slots[m] > crew.get(role, {}).get(m, 0)
            if short and run is None:
                run = [m, m + SLOT]
            elif short and run[1] == m:
                run[1] = m + SLOT
            elif run is not None:
                out.setdefault(role, []).append(tuple(run))
                run = [m, m + SLOT] if short else None
        if run is not None:
            out.setdefault(role, []).append(tuple(run))
    return out


def _adds(restaurant, day, now_local, rows, week, crew, factor, first, rates_t, sal, db_path):
    """Extensions (someone whose shift ends inside a short run stays to its
    end) and call-ins (the best legal standby for a run nobody can stay
    for). Never overtime while a same-role teammate has room."""
    import schedule_rules as _sr
    import schedule_time as _st
    import shift_requests as _srq
    from models import salaried_name_key
    need = _need(restaurant, day, crew, factor, set())
    runs = _short_runs(crew, need, first)
    found, c = [], None
    for role, spans in runs.items():
        for a, b in spans:
            done = False
            for r in sorted(rows, key=lambda x: x["_z"], reverse=True):
                if (r.get("role") or "Staff").strip() != role or salaried_name_key(r["employee"]) in sal:
                    continue
                e = _ceil_slot(r["_z"], day)
                if not (a <= e < b):
                    continue
                hours = round((_at(day, b) - r["_z"]).total_seconds() / 3600.0, 2)
                if hours < MIN_MOVE_HOURS:
                    continue
                i = _find_row(week, r)
                if i is None:
                    continue
                after = [dict(x) for x in week]
                new = _st.retime(after[i], None, b)
                problems, c = _judge(restaurant, week, after, [(i, r["employee"])], c)
                if problems:
                    continue
                extra = _sr.overtime_added(c, r["employee"], new, [x for j, x in enumerate(after) if j != i])
                if extra > 0.05 and _srq.teammates_with_room(restaurant.id, week, i, c, exclude=(r["employee"],)):
                    continue                 # overtime while a teammate has room: never
                rate, _s = _rate(rates_t, r["employee"], role)
                cost = round(hours * (rate or 0), 2) if rate else None
                found.append({"kind": "extend", "employee": r["employee"], "role": role, "date": day.isoformat(),
                              "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"),
                              "at": e, "until": b, "start_label": _clock(r["_z"]), "end_label": _clock(_at(day, b)),
                              "hours": hours, "dollars": cost, "premium": None, "net": None,
                              "text": (f"Ask {r['employee']} ({role}) to stay until {_clock(_at(day, b))} instead of "
                                       f"{_clock(r['_z'])}: tonight is running ahead and {role} is short then — "
                                       f"{hours:g}h" + (f", about ${cost:,.0f}" if cost else "") + "."),
                              "key": f"extend:{day.isoformat()}:{_key(r['employee'])}:{b}"})
                done = True
                break
            if done or (b - a) < MIN_CALL_IN_HOURS * 60:
                continue
            on_today = {_key(x["employee"]) for x in rows}
            try:
                import labor_replacements as _lr
                cands = _lr.for_gap(restaurant.id, role, day.strftime("%A"),
                                    exclude=[x["employee"] for x in rows], on_date=day.isoformat(), limit=8)
            except Exception:
                cands = []
            start_label, end_label = _st.fmt_minutes(a % (24 * 60)), _st.fmt_minutes(b % (24 * 60))
            best = None
            for cand in cands:
                name = cand["name"]
                if _key(name) in on_today:
                    continue
                newrow = {"date": day.isoformat(), "day": day.strftime("%A"), "employee": name, "role": role,
                          "shift_start": start_label, "shift_end": end_label, "notes": ""}
                _st.retime(newrow, a % (24 * 60), b % (24 * 60))
                after = [dict(x) for x in week] + [newrow]
                problems, c = _judge(restaurant, week, after, [(len(after) - 1, name)], c)
                if problems:
                    continue
                ot = _sr.overtime_added(c, name, newrow, week)
                if best is None or (ot <= 0.05 < best[1]):
                    best = (name, ot, newrow)
                if ot <= 0.05:
                    break
            if best is None:
                continue
            name, ot, newrow = best
            hours = round((b - a) / 60.0, 2)
            rate, _s = _rate(rates_t, name, role)
            cost = round(hours * (rate or 0), 2) if rate else None
            found.append({"kind": "call_in", "employee": name, "role": role, "date": day.isoformat(),
                          "shift_start": start_label, "shift_end": end_label, "at": a, "until": b,
                          "start_label": _clock(_at(day, a)), "end_label": _clock(_at(day, b)), "hours": hours,
                          "dollars": cost, "premium": None, "net": None,
                          "text": (f"Call in {name} ({role}) {_clock(_at(day, a))}-{_clock(_at(day, b))}: tonight is "
                                   f"running ahead and nobody on can stay — {hours:g}h"
                                   + (f", about ${cost:,.0f}" if cost else "")
                                   + (f" ({ot:g}h of it overtime — nobody with room could)" if ot > 0.05 else "") + "."),
                          "key": f"call_in:{day.isoformat()}:{role.lower()}:{a}"})
    return found


# ── the ledger, earned autonomy and acting on a move ───────────────────────

def _store_move(restaurant_id, m, db_path=None):
    """The move's ledger row (one per key, ever). Returns (id, is_new)."""
    conn = get_conn(db_path)
    try:
        hit = conn.execute("SELECT id FROM live_labor_moves WHERE restaurant_id=? AND move_key=?",
                           (restaurant_id, m["key"])).fetchone()
        if hit:
            return hit["id"], False
        cur = conn.execute(
            "INSERT INTO live_labor_moves (restaurant_id, business_date, kind, move_key, employee, role, start_label, "
            "end_label, hours, dollars, premium, text, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (restaurant_id, m["date"], m["kind"], m["key"], m["employee"], m["role"], m["start_label"],
             m["end_label"], m["hours"], m.get("dollars"), m.get("premium"), m["text"],
             json.dumps({k: m.get(k) for k in ("date", "shift_start", "shift_end", "at", "until", "employee", "role",
                                                "kind")})))
        conn.commit()
        return cur.lastrowid, True
    finally:
        conn.close()


def get_move(restaurant_id, move_id, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM live_labor_moves WHERE id=? AND restaurant_id=?",
                         (int(move_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    d = dict(r)
    try:
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
    except (TypeError, ValueError):
        d["payload"] = {}
    return d


def todays_moves(restaurant_id, day, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, kind, employee, role, start_label, end_label, hours, dollars, premium, text, "
                            "status, how, decided_by FROM live_labor_moves WHERE restaurant_id=? AND business_date=? "
                            "ORDER BY id DESC", (restaurant_id, str(day)[:10])).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def manager_approvals(restaurant_id, kind, since=None, db_path=None) -> int:
    """Live moves of `kind` a manager approved themselves in the window since
    the last lapse — never an automatic one, never one later reversed."""
    floor = (datetime.utcnow() - timedelta(days=EARN_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    after = since is not None and str(since)[:19] >= floor
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT COUNT(*) FROM live_labor_moves WHERE restaurant_id=? AND kind=? AND how='manager' "
                         "AND status IN ('acted','offered') AND reversed_at IS NULL AND decided_at "
                         + ("> ?" if after else ">= ?"), (restaurant_id, kind, str(since)[:19] if after else floor)
                         ).fetchone()
    finally:
        conn.close()
    return int(r[0] or 0) if r else 0


def trust(restaurant_id, kind, db_path=None) -> dict:
    """{kind, earned, approvals, needed, autopilot_on, lapsed_at,
    lapse_reason}: autopilot acts alone for `kind` only when it is an
    AUTO_KIND, the owner's switch is on, and it is earned."""
    import automation_trust
    import floor_settings
    st = automation_trust.states(restaurant_id, SCOPE, **({"db_path": db_path} if db_path else {})).get(kind) or {}
    since = st.get("lapsed_at") if st.get("state") == "lapsed" else None
    n = manager_approvals(restaurant_id, kind, since=since, db_path=db_path)
    earned = kind in AUTO_KINDS and (st.get("state") == "earned" or n >= EARN_APPROVALS)
    if earned and st.get("state") != "earned":
        automation_trust.transition(restaurant_id, SCOPE, kind, True,
                                    basis={"approvals": n, "window_days": EARN_WINDOW_DAYS},
                                    **({"db_path": db_path} if db_path else {}))
    on = floor_settings.get_for(restaurant_id, db_path)["autopilot"]
    return {"kind": kind, "earned": bool(earned), "approvals": n, "needed": EARN_APPROVALS, "autopilot_on": on,
            "acts_alone": bool(on and earned), "lapsed_at": st.get("lapsed_at"), "lapse_reason": st.get("lapse_reason")}


def _decide(move_id, status, how, actor, request_id=None, db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE live_labor_moves SET status=?, how=?, decided_by=?, decided_at=datetime('now'), "
                     "request_id=COALESCE(?, request_id) WHERE id=?",
                     (status, how, (actor or "")[:120] or None, request_id, int(move_id)))
        conn.commit()
    finally:
        conn.close()


def _actor_name(user):
    if isinstance(user, dict):
        return user.get("username") or user.get("email") or "a manager"
    return user or "a manager"


def act(restaurant_id, move_id, user=None, how="manager", now_local=None, db_path=None) -> dict:
    """Carry out a move — judged again on the week as it is now:
      cut      the shift's end written through the Studio save path
               (shift_edits, Cavnar AI's origin "live_move") and the change
               sent to that person (Send changes — its gate, its record of
               predictability pay);
      extend   / call_in: the shift offered to the person in the app
               (shift_requests.post_open_shift with offer_to — the claim's
               own legality check; their Accept writes the week).
    Returns {ok, status, ...} or {ok: False, error}."""
    import shift_edits
    import shift_requests as _srq
    m = get_move(restaurant_id, move_id, db_path)
    if not m:
        return {"ok": False, "error": "That suggestion wasn't found."}
    if m["status"] not in ("suggested", "pushed"):
        return {"ok": False, "error": "That suggestion was already answered."}
    p = m["payload"]
    who = _actor_name(user) if how == "manager" else AUTO_ACTOR
    if m["kind"] == "cut":
        import schedule_time as _st
        plan_args = {"date": p["date"], "employee": p["employee"], "shift_start": p.get("shift_start"),
                     "new_end": _st.fmt_minutes(int(p["at"]) % (24 * 60))}
        pl = shift_edits.plan(restaurant_id, "retime", plan_args, db_path=db_path)
        if not pl.get("ok"):
            return {"ok": False, "error": pl.get("error")}
        if not pl["legal"]:
            return {"ok": False, "error": "That cut isn't legal on the week as it is now: " + "; ".join(pl["problems"][:2])}
        saved = shift_edits.save_rows(restaurant_id, pl["history_id"], pl["_rows"], pl["version"], user=user,
                                      origin="cavnar:live_move", db_path=db_path)
        if not saved.get("ok"):
            return saved
        sent = None
        if pl["published"]:
            # Only the cut goes out — never the manager's other unsent edits
            # of the week — through the owner's undo window like any Send,
            # its late-change premium acknowledged and on the record
            # (schedule re-audit 10/11/26 BUGSLIFE-4, -5).
            try:
                sent = _send_cut(restaurant_id, pl, p, saved, user, db_path=db_path)
            except Exception as e:
                sent = {"ok": False, "error": str(e)}
        _decide(move_id, "acted", how, who, db_path=db_path)
        _record(restaurant_id, m, "accepted", user, db_path=db_path)
        out = {"ok": True, "status": "acted", "saved": saved, "sent": sent}
        if sent is not None and not (sent or {}).get("ok"):
            out["note"] = ("Saved to the week; it hasn't reached them — Send changes from the Studio "
                           + (f"({sent.get('error')})" if sent and sent.get("error") else "") + ".")
        elif (sent or {}).get("queued"):
            out["note"] = (f"Saved — {p['employee']} is told when your undo window ends"
                           + (f" ({sent.get('undo_minutes')} min)" if sent.get("undo_minutes") else "") + ".")
        return out
    try:
        res = _srq.post_open_shift(restaurant_id, p["date"], m["start_label"] if m["kind"] == "extend" else
                                   p["shift_start"], shift_end=m["end_label"] if m["kind"] == "extend" else
                                   p["shift_end"], role=p["role"], actor=who, offer_to=p["employee"],
                                   note=("Tonight is running ahead — can you stay on?" if m["kind"] == "extend"
                                         else "Tonight is running ahead — can you come in?"),
                                   **({"db_path": db_path} if db_path else {}))
    except _srq.ShiftRequestError as e:
        return {"ok": False, "error": f"Couldn't offer {p['employee']} the shift: {e}."}
    req_id = (res.get("request") or {}).get("id")
    _decide(move_id, "offered", how, who, request_id=req_id, db_path=db_path)
    if how == "manager":
        trust(restaurant_id, m["kind"], db_path=db_path)
    return {"ok": True, "status": "offered", "request_id": req_id, "via": (res.get("offer") or {}).get("via")}


def _cut_shift_ids(rows, p) -> list:
    """The shift id of the cut row in the week as saved: the person's shift
    on the date that started at the planned start."""
    import schedule_time as _st
    want = _st.parse_minutes(p.get("shift_start") or "")
    for r in rows or []:
        if (r.get("date") or "")[:10] == p["date"] and _key(r.get("employee")) == _key(p["employee"]) \
                and (want is None or _st.parse_minutes(r.get("shift_start") or "") == want) and r.get("shift_id"):
            return [r["shift_id"]]
    return []


def _send_cut(restaurant_id, pl, p, saved, user, db_path=None) -> dict:
    """Tell the person their cut — that shift alone, the week staff have
    otherwise as it was (schedule_publish.send_live_change) — now, or when
    the owner's undo window ends (send_delay_minutes, queued like any Send:
    one Undo stops it). Returns the send's payload, or {queued, action_id,
    execute_at, undo_minutes}."""
    import schedule_publish as _sp
    import schedule_versions as _sv
    from models import get_restaurant
    conn = get_conn(db_path)
    try:
        ids = _cut_shift_ids(_sv.latest_rows(conn, int(pl["history_id"])), p)
    finally:
        conn.close()
    if not ids:
        return {"ok": False, "error": "the cut shift couldn't be found on the saved week"}
    actor = user if isinstance(user, dict) else None
    r = get_restaurant(restaurant_id, db_path) if db_path else get_restaurant(restaurant_id)
    delay = int(getattr(r, "send_delay_minutes", 0) or 0) if r else 0
    if delay > 0:
        import delayed
        from time_utils import mdy
        from permissions import answer_authority
        by = {"authority": answer_authority(actor), "queued_by": ((actor or {}).get("username")
                                                                  or (actor or {}).get("email") or "")[:120]} \
            if actor else {"automatic": True}
        act = delayed.queue_schedule_send(
            restaurant_id, "schedule_changes_send",
            {"schedule_id": int(pl["history_id"]), "only_shift_ids": ids, "version": saved.get("version"),
             "live": "cut", "manual": bool(actor), **by},
            delay, actor=actor,
            label=f"Telling {p['employee']} their {mdy(p['date'])} shift now ends earlier",
            **({"db_path": db_path} if db_path else {}))
        return {"ok": True, "queued": True, "action_id": act["id"], "execute_at": act["execute_at"],
                "undo_minutes": delay}
    body, _status = _sp.send_live_change(restaurant_id, int(pl["history_id"]), ids, actor=actor,
                                         version=saved.get("version"), why="live labor cut")
    return body


def dismiss(restaurant_id, move_id, user=None, db_path=None) -> dict:
    m = get_move(restaurant_id, move_id, db_path)
    if not m or m["status"] not in ("suggested", "pushed"):
        return {"ok": False, "error": "That suggestion was already answered."}
    _decide(move_id, "dismissed", "manager", _actor_name(user), db_path=db_path)
    _record(restaurant_id, m, "dismissed", user, db_path=db_path)
    return {"ok": True, "status": "dismissed"}


def reverse(restaurant_id, move_id, user=None, db_path=None) -> dict:
    """A manager's "that shouldn't have gone": an automatic offer withdrawn
    where nobody took it yet, and autopilot for that kind lapses at once
    (automation_trust), whatever could be undone."""
    import automation_trust
    import shift_requests as _srq
    m = get_move(restaurant_id, move_id, db_path)
    if not m or m.get("how") != "auto" or m.get("reversed_at"):
        return {"ok": False, "error": "That wasn't an automatic move, or it was already reversed."}
    undone = False
    if m.get("request_id"):
        try:
            undone = bool(_srq.cancel_open(restaurant_id, m["request_id"], actor=_actor_name(user),
                                           **({"db_path": db_path} if db_path else {})))
        except Exception:
            undone = False
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE live_labor_moves SET reversed_at=datetime('now'), status='reversed' WHERE id=?",
                     (int(move_id),))
        conn.commit()
    finally:
        conn.close()
    automation_trust.transition(restaurant_id, SCOPE, m["kind"], False,
                                reason=f"you reversed an automatic {m['kind'].replace('_', '-')}",
                                **({"db_path": db_path} if db_path else {}))
    return {"ok": True, "undone": undone,
            "note": None if undone else "Somebody already took it — change the week in the Studio if it should go."}


def _record(restaurant_id, m, event, user, db_path=None):
    """A cut's answer into the recommendation ledger under the pulse's own
    kind (pulse_cut), so the cut learners read the live ones too. No
    authority= on purpose: rec_ledger derives it (request_authority) - the
    floor route's login for a manager's answer, none (system) for the
    15-minute job's automatic cut."""
    if m["kind"] != "cut":
        return
    try:
        import rec_ledger
        rec_ledger.record(restaurant_id, f"pulse_cut:{m['business_date']}:{_key(m['employee'])}", event,
                          surface="labor", user_id=(user or {}).get("id") if isinstance(user, dict) else None,
                          meta={"live_move": m["id"], "dollars": m.get("dollars")},
                          source_ref=f"live_move:{m['id']}", **({"db_path": db_path} if db_path else {}))
    except Exception as e:
        print(f"[labor_live] rec ledger not written rid={restaurant_id}: {e!r}")


# ── the 15-minute job ───────────────────────────────────────────────────────

def _keep_read(restaurant_id, rd, db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO live_labor_reads (restaurant_id, business_date, read_at, body_json) "
                     "VALUES (?,?,datetime('now'),?) ON CONFLICT(restaurant_id, business_date) DO UPDATE SET "
                     "read_at=excluded.read_at, body_json=excluded.body_json",
                     (restaurant_id, rd["business_date"], json.dumps(rd, default=str)))
        conn.commit()
    finally:
        conn.close()


def kept_read(restaurant_id, day, db_path=None):
    """The newest kept read for the business date and its age in minutes."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT read_at, body_json, (julianday('now') - julianday(read_at)) * 1440 AS age FROM "
                         "live_labor_reads WHERE restaurant_id=? AND business_date=?",
                         (restaurant_id, str(day)[:10])).fetchone()
    finally:
        conn.close()
    if not r:
        return None, None
    try:
        return json.loads(r["body_json"]), float(r["age"] or 0)
    except (TypeError, ValueError):
        return None, None


def _pushes_today(restaurant_id, day, db_path=None) -> int:
    conn = get_conn(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM live_labor_moves WHERE restaurant_id=? AND business_date=? "
                                "AND pushed_at IS NOT NULL", (restaurant_id, str(day)[:10])).fetchone()[0])
    finally:
        conn.close()


def _offer_to_managers(restaurant, move_id, m, db_path=None) -> bool:
    """One push to the people who get the morning brief and whose phone can
    take it, with the move and its one-tap action; never more than
    MAX_MOVE_PUSHES_PER_DAY a day."""
    import morning_brief
    import notify
    import push
    import strategy_jobs
    dbp = db_path or _models_mod.DB_PATH
    if _pushes_today(restaurant.id, m["date"], db_path) >= MAX_MOVE_PUSHES_PER_DAY:
        return False
    if not notify.briefing_allowed(restaurant.id, "intraday_pulse", dbp):
        return False
    audience = strategy_jobs.deliverable_audience(
        restaurant.id, {u["id"] for u in morning_brief.recipients(restaurant.id, dbp)}, dbp,
        alert_type="intraday_pulse")
    if not audience:
        return False
    title = {"cut": "Tonight's running slow", "extend": "Tonight's running ahead",
             "call_in": "Tonight's running ahead"}[m["kind"]]
    queued = push.fire_push(restaurant.id, "intraday_pulse", title, m["text"],
                            data={"live_move_id": move_id, "live_move_kind": m["kind"], "surface": "alert_push",
                                  "answerable": False},
                            db_path=dbp, user_ids=audience)
    if not queued:
        return False
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE live_labor_moves SET pushed_at=datetime('now'), status=CASE WHEN status='suggested' "
                     "THEN 'pushed' ELSE status END WHERE id=?", (move_id,))
        conn.commit()
    finally:
        conn.close()
    return True


def run_for(restaurant, now_local=None, db_path=None) -> dict:
    """One restaurant's quarter hour: the read kept, the best move stored
    and offered (or, earned, acted on). {read, move, offered, acted}."""
    import floor_settings
    import time_utils as _tu
    now_local = now_local or _tu.restaurant_now(restaurant, naive=True)
    rd = read(restaurant, now_local=now_local, live=True, db_path=db_path)
    if not rd.get("available"):
        return {"read": rd, "move": None}
    st = floor_settings.get(restaurant)
    best = None
    if st["live_moves"]:
        try:
            ms = moves(restaurant, rd=rd, now_local=now_local, db_path=db_path, limit=1)
        except Exception as e:
            import ops
            ops.capture(e, job="live_labor", context=f"restaurant_id={restaurant.id} moves")
            ms = []
        best = ms[0] if ms else None
    rd["moves"] = [best] if best else []
    _keep_read(restaurant.id, rd, db_path)
    out = {"read": rd, "move": best, "offered": False, "acted": False}
    if not best:
        return out
    move_id, new = _store_move(restaurant.id, best, db_path)
    if not new:
        return out
    t = trust(restaurant.id, best["kind"], db_path=db_path)
    if t["acts_alone"]:
        res = act(restaurant.id, move_id, user=None, how="auto", now_local=now_local, db_path=db_path)
        out["acted"] = bool(res.get("ok"))
        if res.get("ok"):
            try:
                import strategy_jobs
                strategy_jobs._reach(restaurant.id, "shift_request", "Offered automatically",
                                     best["text"] + " Sent to them automatically (autopilot) — reverse it in "
                                     "Labor if it shouldn't have gone; autopilot then goes back to you.",
                                     {"tab": "labor"}, db_path or _models_mod.DB_PATH, lines=[best["text"]])
            except Exception:
                pass
            return out
    out["offered"] = _offer_to_managers(restaurant, move_id, best, db_path)
    return out


def run_live_labor(db_path=None, restaurants=None) -> dict:
    """The `live_labor` job, every 15 minutes: every open restaurant with
    labor on, bounded and resumable (strategy_jobs._slot_iter's cursor and
    wall-clock bound). Standard counts."""
    import threading
    import ops
    import strategy_jobs
    import time_utils as _tu
    dbp = db_path or _models_mod.DB_PATH
    st = {"attempted": 0, "failed": 0, "offered": 0, "acted": 0}
    lock = threading.Lock()

    def _one(r):
        if not getattr(r, "module_labor", 0):
            return
        local = _tu.restaurant_now(r, naive=True)
        if not strategy_jobs._open_now(r, local):
            return
        if not ops.claim_period(f"live_labor:{r.id}", f"{local.date().isoformat()}-{local.hour}-"
                                                      f"{local.minute // CADENCE_MINUTES}"):
            return
        with lock:
            st["attempted"] += 1
        try:
            out = run_for(r, now_local=local, db_path=db_path)
            with lock:
                st["offered"] += 1 if out.get("offered") else 0
                st["acted"] += 1 if out.get("acted") else 0
        except Exception as e:
            with lock:
                st["failed"] += 1
            ops.capture(e, job="live_labor", context=f"restaurant_id={r.id}")

    # A small pool, not a serial walk (scale audit 10/19/26 INF-14 / A3-03):
    # the serial 4-minute walk reached ~96 stores a run, so at 500 open
    # stores each was read every ~78 minutes, not every 15.
    st["hit_bound"] = strategy_jobs._slot_sweep("live_labor", restaurants, _one, dbp,
                                                workers=strategy_jobs.SLOT_WORKERS)
    return strategy_jobs._slot_counts(st, offered=st["offered"], acted=st["acted"])
