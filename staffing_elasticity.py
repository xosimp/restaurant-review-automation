"""
staffing_elasticity.py — what one more (or one fewer) person of a role on a
meal is worth here, measured, with an interval: the first counterfactual
staffing estimate (scheduler audit 10/10/26 RM-20, LEARNING-6, MARKET-23).

Nothing in the scheduler ever learned what staffing level works. Outcomes
only vetoed trims; service performance (comps, turns) was read by no
schedule module; over-staffing was invisible and under-staffing seen only
when an issue was opened on a watched night. Labor-percentage targets treat
every hour alike — the 4th server at 7pm Friday and the 4th at 3pm.

Two estimators per role family x daypart, over ESTIMATE_DAYS of this
restaurant's own nights:

  natural experiments  a shift that lost one of the family to an UNCOVERED
                       no-show or call-out (attendance_events: nobody
                       covered it — an unplanned -1) against the same
                       weekday's meals with no absence in the family and a
                       forecast within MATCH_BAND of it (NAT_WEEKS either
                       side). Effect of -1 = the mean of (treated - its
                       matched controls); the interval a t interval over the
                       treated shifts. Needs NAT_MIN treated shifts.
  fixed effects        every meal: the outcome residual on the family's
                       people on at the meal's busiest half hour, both
                       taken within the weekday (weekday fixed effects) and
                       with the meal's forecast partialled out, clustered by
                       weekday (learning_stats.fe_slope_ci). Needs PANEL_MIN
                       meals. Managers staff up on nights they expect to be
                       busy beyond the forecast, which biases this one up;
                       the natural experiments do not share that bias.

Outcomes: sales against the meal's forecast (the same weekday's median for
the meal over the 8 weeks before — a log ratio, said in dollars at the
meal's typical sales), the comp rate (comps / net), the median table turn
(minutes), from the ticket archive.

The two are pooled by inverse variance. decide() moves a requirement by one
person only when an interval clears zero: up when one more person's sales
net of what carrying their shift costs at the owner's labor target clears
zero, or a service outcome measurably improves; down when that net is
clearly negative and no service outcome measurably worsens. Never below the
owner's floors (the requirement holds them), never more than one person,
and only behind the sample floors — below them nothing is said or moved.

Weekly job (run_elasticity: one row per restaurant x family x daypart in
`staffing_elasticity`, bounded and resumable); moves() reads the stored
rows for the requirements (schedule_requirements' `elasticity`);
marginal_values() is the per-hour figure a solver objective prices hours
by (contract for the CP-SAT solver, workstream F).
"""
import json
import math
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH

ESTIMATE_DAYS = 120
NAT_MIN = 4                      # uncovered absences of a family on a meal before the experiment speaks
NAT_WEEKS = 8
MATCH_BAND = 0.25                # a control meal's forecast within 25% of the treated one's
PANEL_MIN = 24                   # meals before the fixed-effects slope speaks
Z90 = 1.645
CURSOR_KEY = "staffing_elasticity_cursor"
RUN_MAX_SECONDS = 15 * 60
PARTS = ("morning", "night")
OUTCOMES = ("sales", "comps", "turn")
OUTCOME_GOOD = {"sales": 1, "comps": -1, "turn": -1}   # the direction one more person should move it
DEFAULT_SHIFT_HOURS = 6.0        # a shift's length where the punches give none


def get_conn(db_path=None):
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_staffing_elasticity(db_path=None):
    """Boot DDL (models.init_db): the weekly estimates."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS staffing_elasticity (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            family          TEXT    NOT NULL,
            daypart         TEXT    NOT NULL,
            move            INTEGER NOT NULL DEFAULT 0,
            estimate_json   TEXT    NOT NULL,
            computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, family, daypart)
        )""")
        conn.commit()
    finally:
        conn.close()


def _median(vals):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _t(df) -> float:
    """The 95th percentile of Student's t (a 90% two-sided interval)."""
    try:
        import metrics
        return float(metrics.t_ppf(0.95, max(1, int(df))))
    except Exception:
        return Z90


# ── the estimators (pure over units) ──────────────────────────────────────
#
# A unit is one meal of one role family: {"date", "weekday", "people" (on at
# the busiest half hour), "fc" (log forecast), "absent" (an uncovered
# absence of the family), "y": {outcome: value}}.

def natural_experiment(units, outcome) -> dict | None:
    """{est, lo, hi, se, n, method} — the effect of ONE MORE person (the
    negative of an uncovered absence's effect) on `outcome`, or None below
    NAT_MIN treated meals with controls."""
    diffs = []
    for t in units:
        if not t.get("absent") or (t.get("y") or {}).get(outcome) is None or t.get("fc") is None:
            continue
        try:
            td = date.fromisoformat(t["date"])
        except ValueError:
            continue
        ctrl = [c["y"][outcome] for c in units
                if not c.get("absent") and c["weekday"] == t["weekday"] and c is not t
                and (c.get("y") or {}).get(outcome) is not None and c.get("fc") is not None
                and abs(c["fc"] - t["fc"]) <= math.log(1 + MATCH_BAND)
                and abs((date.fromisoformat(c["date"]) - td).days) <= NAT_WEEKS * 7]
        if len(ctrl) >= 2:
            diffs.append(t["y"][outcome] - sum(ctrl) / len(ctrl))
    n = len(diffs)
    if n < NAT_MIN:
        return None
    m = sum(diffs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in diffs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    est = -m
    half = _t(n - 1) * se
    return {"est": round(est, 4), "lo": round(est - half, 4), "hi": round(est + half, 4), "se": round(se, 4),
            "n": n, "method": "natural_experiment"}


def panel_slope(units, outcome) -> dict | None:
    """{est, lo, hi, se, n, method} — the within-weekday slope of `outcome`
    on people at the rush with the forecast partialled out (Frisch-Waugh),
    clustered by weekday; None below PANEL_MIN meals."""
    rows = [(u["weekday"], float(u["people"]), float(u["fc"]), float(u["y"][outcome])) for u in units
            if u.get("people") is not None and u.get("fc") is not None and (u.get("y") or {}).get(outcome) is not None]
    if len(rows) < PANEL_MIN:
        return None
    by = {}
    for g, x, f, y in rows:
        by.setdefault(g, []).append((x, f, y))
    dm = []
    for g, pts in by.items():
        mx = sum(p[0] for p in pts) / len(pts)
        mf = sum(p[1] for p in pts) / len(pts)
        my = sum(p[2] for p in pts) / len(pts)
        dm += [(g, x - mx, f - mf, y - my) for x, f, y in pts]
    sff = sum(f * f for _g, _x, f, _y in dm)
    if sff > 1e-12:
        bx = sum(x * f for _g, x, f, _y in dm) / sff
        by_ = sum(y * f for _g, _x, f, y in dm) / sff
        dm = [(g, x - bx * f, f, y - by_ * f) for g, x, f, y in dm]
    import learning_stats as _ls
    got = _ls.fe_slope_ci([(g, x, y) for g, x, _f, y in dm])
    if got.get("slope") is None:
        return None
    se = (got["hi"] - got["lo"]) / (2 * Z90) if got.get("hi") is not None else None
    return {"est": got["slope"], "lo": got["lo"], "hi": got["hi"], "se": round(se, 4) if se is not None else None,
            "n": got["n"], "method": "fixed_effects"}


def combine(a, b) -> dict | None:
    """Inverse-variance pool of two estimates (either may be None)."""
    have = [x for x in (a, b) if x and x.get("se")]
    if not have:
        return a or b
    if len(have) == 1:
        return have[0]
    w = [1.0 / (x["se"] ** 2) for x in have]
    est = sum(wi * x["est"] for wi, x in zip(w, have)) / sum(w)
    se = math.sqrt(1.0 / sum(w))
    return {"est": round(est, 4), "lo": round(est - Z90 * se, 4), "hi": round(est + Z90 * se, 4), "se": round(se, 4),
            "n": sum(x["n"] for x in have), "method": "pooled"}


def decide(est: dict, typical_sales, shift_cost, target_pct) -> dict:
    """{"move": +1 | -1 | 0, "net": {est, lo, hi} | None, "why"} for one
    family x daypart from its outcome estimates ({outcome: estimate}):
    sales in log ratio becomes dollars at the meal's typical sales; the
    person's shift cost becomes the sales it needs at the owner's labor
    target (cost / target). Up only when that net's interval clears zero or
    a service outcome measurably improves; down only when the net is clearly
    negative and no service outcome measurably worsens. Pure."""
    s = (est or {}).get("sales")
    need = (float(shift_cost) / (float(target_pct) / 100.0)) if shift_cost and target_pct else None
    net = None
    if s and typical_sales and need is not None:
        k = float(typical_sales)
        net = {"est": round(s["est"] * k - need, 0), "lo": round(s["lo"] * k - need, 0),
               "hi": round(s["hi"] * k - need, 0), "sales": round(s["est"] * k, 0),
               "sales_lo": round(s["lo"] * k, 0), "sales_hi": round(s["hi"] * k, 0), "needs": round(need, 0)}
    # A service outcome one more person measurably improves (its interval
    # clears zero in the good direction) — equally, one fewer measurably
    # worsens it.
    better = []
    for o in ("comps", "turn"):
        e = (est or {}).get(o)
        if e and (e["hi"] < 0 if OUTCOME_GOOD[o] < 0 else e["lo"] > 0):
            better.append(o)
    if net and net["lo"] > 0:
        return {"move": 1, "net": net, "why": "pays"}
    if better:
        return {"move": 1, "net": net, "why": "service:" + ",".join(better)}
    if net and net["hi"] < 0:
        return {"move": -1, "net": net, "why": "costs"}
    return {"move": 0, "net": net, "why": None}


# ── reading the restaurant's own nights ───────────────────────────────────

def _meals(restaurant_id, start, end, db_path=DB_PATH) -> dict:
    """{(date, part): {"sales", "comps_rate", "turn"}} from the ticket archive
    (each date's own split), comps from the ticket lines."""
    import demand_drivers as _dd
    import schedule_time as _stime
    sales = _dd.date_part_sales(restaurant_id, start, end, db_path=db_path)
    out = {}
    for d, e in sales.items():
        for part in PARTS:
            if e.get(part):
                out[(d, part)] = {"sales": float(e[part]), "comps_rate": None, "turn": None}
    conn = get_conn(db_path)
    try:
        tickets = conn.execute(
            "SELECT ticket_id, business_date, opened_at, closed_at, room_name, is_bar, net_sales FROM pos_tickets "
            "WHERE restaurant_id=? AND business_date>=? AND business_date<? AND COALESCE(cancelled,0)=0",
            (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()
        comps = {r[0]: float(r[1] or 0) for r in conn.execute(
            "SELECT ticket_id, SUM(COALESCE(loss_amount, 0)) FROM pos_ticket_lines WHERE restaurant_id=? AND "
            "business_date>=? AND business_date<? AND kind='comp' GROUP BY ticket_id",
            (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()}
    except Exception:
        tickets, comps = [], {}
    finally:
        conn.close()
    import service_performance as _sp
    agg = {}
    split = _stime.daypart_split()
    for t in tickets:
        try:
            hh, mm = int(str(t["opened_at"])[11:13]), int(str(t["opened_at"])[14:16])
        except (TypeError, ValueError):
            continue
        part = "morning" if hh * 60 + mm < split and hh >= 5 else "night"
        e = agg.setdefault((str(t["business_date"])[:10], part), {"net": 0.0, "comps": 0.0, "turns": []})
        e["net"] += float(t["net_sales"] or 0)
        e["comps"] += comps.get(t["ticket_id"], 0.0)
        m = _sp._turn(dict(t))
        if m is not None:
            e["turns"].append(m)
    for k, e in agg.items():
        if k in out:
            out[k]["comps_rate"] = (e["comps"] / e["net"]) if e["net"] > 0 else None
            out[k]["turn"] = _median(e["turns"]) if len(e["turns"]) >= 5 else None
    return out


def _absences(restaurant_id, since, db_path=DB_PATH) -> set:
    """{(date, part, family)} with an uncovered no-show or call-out."""
    import attendance
    import schedule_time as _stime
    from intelligence.staffing import family_of
    out = set()
    try:
        evs = attendance.events(restaurant_id, since=since, db_path=None if db_path == DB_PATH else db_path)
    except Exception:
        return out
    for e in evs or []:
        if e.get("outcome") not in ("no_show", "called_out") or (e.get("covered_by") or "").strip():
            continue
        fam = family_of(e.get("role"))
        m = _stime.parse_minutes(e.get("shift_start"))
        if not fam or m is None:
            continue
        part = "morning" if m < _stime.daypart_split() - 60 else "night"
        out.add((str(e.get("business_date"))[:10], part, fam))
    return out


def shift_hours(rows) -> dict:
    """{family: median hours of one person's shift} from shift rows."""
    from intelligence.staffing import family_of
    by = {}
    for r in rows or []:
        f = family_of(r.get("role"))
        try:
            h = float(r.get("actual_hours") or r.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            h = 0
        if f and h > 0:
            by.setdefault(f, []).append(h)
    return {f: round(_median(v), 2) for f, v in by.items()}


def units_for(restaurant_id, today=None, db_path=DB_PATH, days=ESTIMATE_DAYS, hours_out=None) -> dict:
    """{(family, part): [unit]} over the restaurant's own nights (see the
    unit note above), the forecast each meal's same-weekday median over the
    8 weeks before it. `hours_out` (a dict) receives shift_hours()."""
    import staffing_model as _sm
    import shift_facts
    today = today or date.today()
    start = today - timedelta(days=int(days))
    meals = _meals(restaurant_id, start - timedelta(weeks=8), today, db_path=db_path)
    rows = shift_facts.rows(restaurant_id, since=start.isoformat(), until=(today - timedelta(days=1)).isoformat(),
                            db_path=None if db_path == DB_PATH else db_path)
    try:
        import labor
        rows, _h = labor._without_salaried(restaurant_id, rows)
    except Exception:
        pass
    if hours_out is not None:
        hours_out.update(shift_hours(rows))
    people = _sm.people_slots(rows)
    work = _sm.work_slots(restaurant_id, start, today, db_path=db_path)
    absent = _absences(restaurant_id, start.isoformat(), db_path=db_path)
    out = {}
    for (d, part), m in meals.items():
        if d < start.isoformat():
            continue
        day = date.fromisoformat(d)
        base = _median([meals[((day - timedelta(weeks=k)).isoformat(), part)]["sales"]
                        for k in range(1, 9) if ((day - timedelta(weeks=k)).isoformat(), part) in meals])
        if not base or base <= 0 or not m.get("sales"):
            continue
        w = ((work.get(d) or {}).get("tickets") or {})
        lo, hi = _sm._part_range(part)
        inpart = {t: v for t, v in w.items() if lo <= t < hi}
        if not inpart:
            continue
        t_star = max(inpart, key=lambda t: (inpart[t], -t))
        for fam, slots in (people.get(d) or {}).items():
            on = slots.get(t_star)
            if not on:
                continue
            out.setdefault((fam, part), []).append({
                "date": d, "weekday": day.strftime("%A"), "people": on, "fc": math.log(base),
                "absent": (d, part, fam) in absent, "typical": base,
                "y": {"sales": math.log(m["sales"] / base), "comps": m.get("comps_rate"), "turn": m.get("turn")}})
    return out


def estimate_restaurant(restaurant_id, today=None, db_path=DB_PATH) -> dict:
    """Estimate every family x daypart and store it (staffing_elasticity).
    {(family, part): {"outcomes": {outcome: estimate}, "move", "net", "why",
    "typical_sales", "shift_cost"}}."""
    import models as _m
    r = _m.get_restaurant(restaurant_id) if db_path in (None, DB_PATH) else _m.get_restaurant(restaurant_id, db_path)
    target = getattr(r, "labor_target", None) or 30.0
    rate = getattr(r, "hourly_rate", None) or 15.0
    try:
        rates = _m.get_role_rates(restaurant_id) or {}
    except Exception:
        rates = {}
    from intelligence.staffing import family_of
    fam_rate = {}
    for role, v in rates.items():
        f = family_of(role)
        if f and v and role != "_default":
            fam_rate.setdefault(f, []).append(float(v))
    out = {}
    hours_by = {}
    for (fam, part), units in units_for(restaurant_id, today=today, db_path=db_path, hours_out=hours_by).items():
        outcomes = {}
        for o in OUTCOMES:
            got = combine(natural_experiment(units, o), panel_slope(units, o))
            if got:
                outcomes[o] = got
        if not outcomes:
            continue
        typical = _median([u["typical"] for u in units])
        hours = float(hours_by.get(fam) or DEFAULT_SHIFT_HOURS)
        cost = hours * (sum(fam_rate[fam]) / len(fam_rate[fam]) if fam in fam_rate else float(rate))
        d = decide(outcomes, typical, cost, target)
        out[(fam, part)] = {"outcomes": outcomes, "move": d["move"], "net": d["net"], "why": d["why"],
                            "typical_sales": round(typical, 0) if typical else None, "shift_cost": round(cost, 0),
                            "shift_hours": hours, "meals": len(units),
                            "absences": sum(1 for u in units if u["absent"])}
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM staffing_elasticity WHERE restaurant_id=?", (restaurant_id,))
        for (fam, part), e in out.items():
            conn.execute("INSERT INTO staffing_elasticity (restaurant_id, family, daypart, move, estimate_json) "
                         "VALUES (?,?,?,?,?)", (restaurant_id, fam, part, int(e["move"]), json.dumps(e)))
        conn.commit()
    finally:
        conn.close()
    return out


def stored(restaurant_id, db_path=DB_PATH) -> dict:
    """{(family, part): estimate} as last stored; {} on any failure."""
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT family, daypart, estimate_json FROM staffing_elasticity WHERE restaurant_id=?",
                                (restaurant_id,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            out[(r["family"], r["daypart"])] = json.loads(r["estimate_json"] or "{}") or {}
        except (TypeError, ValueError):
            continue
    return out


FAMILY_WORDS = {"server": "server", "bartender": "bartender", "line_cook": "cook", "dish": "dishwasher",
                "host": "host", "busser": "busser", "runner": "runner", "barback": "barback",
                "prep_cook": "prep cook", "cashier": "cashier", "barista": "barista"}


def reason(fam, part, e) -> str:
    """The owner's words for one stored estimate's move."""
    meal = "lunch" if part == "morning" else "dinner"
    who = FAMILY_WORDS.get(fam, fam.replace("_", " "))
    net = e.get("net") or {}
    oc = e.get("outcomes") or {}
    if e.get("move") == 1 and str(e.get("why") or "").startswith("service"):
        bits = []
        for o in str(e["why"]).split(":", 1)[1].split(","):
            x = oc.get(o) or {}
            if o == "turn":
                bits.append(f"tables turned {abs(x.get('est') or 0):.0f} min faster (90% CI {abs(x.get('hi') or 0):.0f}"
                            f"-{abs(x.get('lo') or 0):.0f})")
            elif o == "comps":
                bits.append(f"comps {abs((x.get('est') or 0) * 100):.1f} points lower")
        return (f"one more {who} at {meal} measured better service here: " + "; ".join(bits)
                + f" (over {oc.get(o, {}).get('n')} meals)")
    if net:
        word = "one more" if e.get("move") == 1 else "one fewer" if e.get("move") == -1 else "one more"
        return (f"{word} {who} at {meal}: one more measured {net.get('sales', 0):+,.0f} in sales a meal here "
                f"(90% CI {net.get('sales_lo', 0):+,.0f} to {net.get('sales_hi', 0):+,.0f}) against "
                f"${net.get('needs', 0):,.0f} of sales their shift needs at your labor target")
    return ""


def moves(restaurant_id, db_path=DB_PATH) -> dict:
    """{(family, part): {"move", "reason"}} — the stored estimates whose
    interval cleared zero (schedule_requirements' `elasticity`)."""
    return {k: {"move": int(e.get("move") or 0), "reason": reason(k[0], k[1], e)}
            for k, e in stored(restaurant_id, db_path=db_path).items() if int(e.get("move") or 0)}


def marginal_values(restaurant_id, db_path=DB_PATH) -> dict:
    """{(family, part): {"per_hour", "lo", "hi"}} — one more person's net
    sales value per hour of their shift (the solver objective's price of an
    hour there; contract for workstream F). Only estimates behind the floors."""
    out = {}
    for k, e in stored(restaurant_id, db_path=db_path).items():
        net = e.get("net") or {}
        if net and e.get("shift_cost"):
            hours = float(e.get("shift_hours") or DEFAULT_SHIFT_HOURS)
            out[k] = {"per_hour": round(net["est"] / hours, 2), "lo": round(net["lo"] / hours, 2),
                      "hi": round(net["hi"] / hours, 2)}
    return out


def run_elasticity(db_path=None, today=None) -> dict:
    """The weekly job: every Labor restaurant with a ticket archive,
    bounded by RUN_MAX_SECONDS and resumable from CURSOR_KEY (scheduler.
    resumable_sweep); the people's productivity too (staff_productivity —
    the same reads, the same cadence). Standard counts."""
    import models
    import scheduler as _sched
    ids = [r.id for r in (models.get_all_restaurants(db_path) if db_path else models.get_all_restaurants())
           if getattr(r, "module_labor", 0)]
    tally = {"attempted": 0, "failed": 0}

    def _one(rid):
        tally["attempted"] += 1
        try:
            estimate_restaurant(rid, today=today, db_path=db_path or DB_PATH)
            import staff_productivity as _prod
            _prod.estimate_restaurant(rid, today=today, db_path=db_path or DB_PATH)
        except Exception as e:
            tally["failed"] += 1
            import ops
            ops.capture(e, job="staffing_learning", context=f"restaurant_id={rid}", restaurant_id=rid)
    _done, ran_out = _sched.resumable_sweep(CURSOR_KEY, sorted(ids), _one, RUN_MAX_SECONDS, workers=1,
                                            job="staffing_learning")
    return {"attempted": tally["attempted"], "ok": tally["attempted"] - tally["failed"], "failed": tally["failed"],
            "skipped": 0, "hit_bound": bool(ran_out)}
