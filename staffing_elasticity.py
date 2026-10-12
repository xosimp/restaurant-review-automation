"""
staffing_elasticity.py — what one more (or one fewer) person of a role on a
meal is worth here, measured, with an interval: the first counterfactual
staffing estimate (scheduler audit 10/10/26 RM-20, LEARNING-6, MARKET-23),
made safe to act on (blind re-audit 10/11/26 LEARNING-4, -5, -6, -7, -24).

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
                       treated shifts. Needs NAT_MIN treated shifts. THE
                       DECISION ESTIMATOR: nobody chose the absence, so the
                       night's demand did not choose the headcount.
  fixed effects        every meal: the outcome residual on the family's
                       people on at the meal's busiest half hour, both
                       taken within the weekday (weekday fixed effects) and
                       with the meal's forecast partialled out, clustered by
                       weekday (learning_stats.fe_slope_ci — t on G - 1).
                       REPORTED ONLY (`panel` beside each outcome): managers
                       keep and call in staff on nights running hot beyond
                       the forecast, so the punched heads follow tonight's
                       sales — reverse causation that read as "one more
                       person pays" with no effect at all (LEARNING-5). It
                       used to be inverse-variance pooled with the natural
                       experiment, and its narrower interval won.

Outcomes: sales against the meal's forecast (the same weekday's median for
the meal over the 8 weeks before — a log ratio, said in dollars at the
meal's typical sales), the comp and void rates (of net), the median table
turn and the median kitchen fire-to-bump minutes (where the POS records
them) from the ticket archive, and the meal's attributed review rating
(schedule_outcomes' `daypart` attribution — sparse, so it speaks rarely).

decide() moves a requirement by one person only on the natural experiment
and only past a Holm step-down at FAMILY_ALPHA over EVERY test the run makes
for the restaurant (each family x meal's "pays", "costs" and each service
outcome — LEARNING-4: 5 outcomes x ~20 cells at 90% each put a person on
40% of cells with no effect): up when one more person's sales net of what
carrying their shift costs at the owner's labor target is above zero, or a
service outcome measurably improves AND that net is not clearly negative;
down when the net is below zero and no service outcome would measurably
worsen with one fewer (none measurably improves with one more, read at a
plain 90% — the guard errs toward keeping the person).
A move BINDS only when two consecutive weekly estimates agree (`streak`),
and only for STALE_DAYS after it was computed (LEARNING-24); it is a target
LEVEL — the controls' headcount plus the move — so a baseline that already
absorbed it is not raised again (LEARNING-6, like
schedule_learning.headcount_to_apply), and it applies to the weekdays at
the demand it was measured at (`weekdays`: up on the meals at least as busy
as the treated ones, down on those no busier — LEARNING-7: it was every
weekday's dinner). Never below the owner's floors (the requirement holds
them), never more than one person, and only behind the sample floors.
While a +1 binds, its window is kept (`windows`) and the baseline's trend
detector holds an upward move of the family's level inside it at the
earlier level (requirement_provenance.hold_down_trend): the drafts' own
+1 never becomes next month's "usual crew" to be raised again.

Weekly job (run_elasticity: one `learner` queue row per Labor restaurant
when the job queue is on — estimate_one — else a bounded, resumable sweep;
one row per restaurant x family x daypart in `staffing_elasticity`);
moves() reads the stored rows for the requirements (schedule_requirements'
`elasticity`); marginal_values() is the per-hour figure a solver objective
prices hours by (contract for the CP-SAT solver, workstream F). Both are
empty unless applies() (STAFFING_ELASTICITY_APPLY).
"""
import json
import math
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH

ESTIMATE_DAYS = 120
NAT_MIN = 4                      # uncovered absences of a family on a meal before the experiment speaks
NAT_WEEKS = 8
MATCH_BAND = 0.25                # a control meal's forecast within 25% of the treated one's
PANEL_MIN = 24                   # meals before the fixed-effects slope speaks
Z90 = 1.645
# The family-wise error of one restaurant's run: every test it makes, Holm
# step-down, one-sided (LEARNING-4).
FAMILY_ALPHA = 0.10
# A move binds once this many consecutive weekly estimates agree on it.
BIND_STREAK = 2
# A stored move older than this binds nothing (LEARNING-24): the weekly job
# missed it, and a +1 from months ago is not this week's evidence.
STALE_DAYS = 21
# How many past +1 windows a cell keeps for the baseline's guard.
WINDOWS_KEPT = 6
CURSOR_KEY = "staffing_elasticity_cursor"
RUN_MAX_SECONDS = 15 * 60
RUN_WORKERS = 3
PARTS = ("morning", "night")
OUTCOMES = ("sales", "comps", "voids", "turn", "kitchen", "review")
# The direction one more person should move each outcome: sales and the
# meal's attributed review up; comps, voids, table turns and kitchen
# fire-to-bump minutes down.
OUTCOME_GOOD = {"sales": 1, "comps": -1, "voids": -1, "turn": -1, "kitchen": -1, "review": 1}
SERVICE_OUTCOMES = ("comps", "voids", "turn", "kitchen", "review")
DEFAULT_SHIFT_HOURS = 6.0        # a shift's length where the punches give none
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


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
# absence of the family), "typical" (the meal's same-weekday median sales),
# "y": {outcome: value}}.

def natural_experiment(units, outcome) -> dict | None:
    """{est, lo, hi, se, df, n, method, level, treated_typical, weekdays} —
    the effect of ONE MORE person (the negative of an uncovered absence's
    effect) on `outcome` with its 90% t interval; `level` the matched
    controls' median headcount (the level one more was measured above),
    `treated_typical` the treated meals' median typical sales (the demand it
    was measured at), `weekdays` the treated meals' weekdays. None below
    NAT_MIN treated meals with controls."""
    diffs, levels, typicals, wds = [], [], [], []
    for t in units:
        if not t.get("absent") or (t.get("y") or {}).get(outcome) is None or t.get("fc") is None:
            continue
        try:
            td = date.fromisoformat(t["date"])
        except ValueError:
            continue
        ctrl = [c for c in units
                if not c.get("absent") and c["weekday"] == t["weekday"] and c is not t
                and (c.get("y") or {}).get(outcome) is not None and c.get("fc") is not None
                and abs(c["fc"] - t["fc"]) <= math.log(1 + MATCH_BAND)
                and abs((date.fromisoformat(c["date"]) - td).days) <= NAT_WEEKS * 7]
        if len(ctrl) >= 2:
            diffs.append(t["y"][outcome] - sum(c["y"][outcome] for c in ctrl) / len(ctrl))
            levels += [c["people"] for c in ctrl if c.get("people") is not None]
            if t.get("typical"):
                typicals.append(float(t["typical"]))
            wds.append(t["weekday"])
    n = len(diffs)
    if n < NAT_MIN:
        return None
    m = sum(diffs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in diffs) / (n - 1)) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    est = -m
    half = _t(n - 1) * se
    lvl = _median(levels)
    return {"est": round(est, 4), "lo": round(est - half, 4), "hi": round(est + half, 4), "se": round(se, 6),
            "df": n - 1, "n": n, "method": "natural_experiment",
            "level": int(round(lvl)) if lvl is not None else None,
            "treated_typical": round(_median(typicals), 2) if typicals else None,
            "weekdays": sorted(set(wds), key=lambda w: WEEKDAYS.index(w) if w in WEEKDAYS else 9)}


def panel_slope(units, outcome) -> dict | None:
    """{est, lo, hi, se, df, n, method} — the within-weekday slope of
    `outcome` on people at the rush with the forecast partialled out
    (Frisch-Waugh), clustered by weekday with t on G - 1; None below
    PANEL_MIN meals. Reported beside the decision, never decided on
    (module doc, LEARNING-5)."""
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
    return {"est": got["slope"], "lo": got["lo"], "hi": got["hi"], "se": got.get("se"), "df": got.get("df"),
            "n": got["n"], "method": "fixed_effects", "report_only": True}


def combine(a, b) -> dict | None:
    """Inverse-variance pool of two estimates (either may be None). Kept for
    reading; decide() never reads a pooled figure (LEARNING-5)."""
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


def _se_df(e):
    """(standard error, degrees of freedom) of an estimate: its own, else
    read back from its 90% interval."""
    df = e.get("df")
    if df is None and e.get("n"):
        df = max(1, int(e["n"]) - 1)
    se = e.get("se")
    if not se and e.get("lo") is not None and e.get("hi") is not None:
        import learning_stats as _ls
        se = (float(e["hi"]) - float(e["lo"])) / (2.0 * _ls.t90(df))
    return (float(se) if se else None), df


def _net(s, typical_sales, need):
    if not s or not typical_sales or need is None:
        return None
    k = float(typical_sales)
    return {"est": round(s["est"] * k - need, 0), "lo": round(s["lo"] * k - need, 0),
            "hi": round(s["hi"] * k - need, 0), "sales": round(s["est"] * k, 0),
            "sales_lo": round(s["lo"] * k, 0), "sales_hi": round(s["hi"] * k, 0), "needs": round(need, 0)}


def cell_tests(est: dict, typical_sales, shift_cost, target_pct) -> dict:
    """{"tests": {name: one-sided p}, "helps": [service outcomes one more
    person measurably improves at a plain 90% — what one fewer would
    worsen], "net"} for one family x daypart: "pays" (one
    more person's sales net of their shift's need is above zero), "costs"
    (below zero) and "service:<o>" (one more person measurably improves
    <o>). Pure."""
    import learning_stats as _ls
    s = (est or {}).get("sales")
    need = (float(shift_cost) / (float(target_pct) / 100.0)) if shift_cost and target_pct else None
    net = _net(s, typical_sales, need)
    tests, helps = {}, []
    if net is not None:
        se, df = _se_df(s)
        if se:
            k = float(typical_sales)
            tests["pays"] = _ls.t_pvalue(s["est"] * k - need, se * k, df, side=1)
            tests["costs"] = _ls.t_pvalue(s["est"] * k - need, se * k, df, side=-1)
    for o in SERVICE_OUTCOMES:
        e = (est or {}).get(o)
        if not e:
            continue
        se, df = _se_df(e)
        if not se:
            continue
        good = OUTCOME_GOOD[o]
        tests[f"service:{o}"] = _ls.t_pvalue(e["est"], se, df, side=good)
        if (e["lo"] > 0 if good > 0 else e["hi"] < 0):
            helps.append(o)
    return {"tests": tests, "helps": helps, "net": net}


def decide(est: dict, typical_sales, shift_cost, target_pct, passed=None) -> dict:
    """{"move": +1 | -1 | 0, "net": {est, lo, hi} | None, "why", "tests"}
    for one family x daypart from its outcome estimates ({outcome:
    estimate} — the natural experiments). `passed`: the tests (cell_tests'
    names) that survived the run's Holm step-down over every cell; None —
    this cell's own tests are the family (Holm at FAMILY_ALPHA). Up when
    "pays" passed, or a service test passed and the sales net is not
    clearly negative (its 90% interval reaches zero — LEARNING-4); down
    when "costs" passed and no service outcome would measurably worsen
    with one fewer (`helps`, unadjusted — the guard keeps the person). Pure."""
    import learning_stats as _ls
    got = cell_tests(est, typical_sales, shift_cost, target_pct)
    net, tests = got["net"], got["tests"]
    if passed is None:
        passed = _ls.holm(tests, FAMILY_ALPHA) if tests else set()
    better = [k.split(":", 1)[1] for k in sorted(tests) if k.startswith("service:") and k in passed]
    out = {"net": net, "tests": {k: round(v, 5) for k, v in tests.items()}}
    if "pays" in passed:
        return dict(out, move=1, why="pays")
    if better and (net is None or net["hi"] >= 0):
        return dict(out, move=1, why="service:" + ",".join(better))
    if "costs" in passed and not got["helps"]:
        return dict(out, move=-1, why="costs")
    return dict(out, move=0, why=None)


def apply_weekdays(move, decision_est, weekday_typical) -> list:
    """The weekdays a move applies to (LEARNING-7): one more person measured
    at the treated meals' demand applies on the meals at least that busy;
    one fewer on those no busier. Every weekday with a typical figure when
    the demand it was measured at is unknown."""
    t = (decision_est or {}).get("treated_typical")
    days = [d for d in WEEKDAYS if d in (weekday_typical or {})]
    if not t or not move:
        return days
    if move > 0:
        return [d for d in days if float(weekday_typical[d]) >= float(t)]
    return [d for d in days if float(weekday_typical[d]) <= float(t)]


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
                out[(d, part)] = {"sales": float(e[part]), "comps_rate": None, "voids_rate": None, "turn": None,
                                  "kitchen": None, "review": None}
    conn = get_conn(db_path)
    try:
        tickets = conn.execute(
            "SELECT ticket_id, business_date, opened_at, closed_at, fired_at, bumped_at, room_name, is_bar, "
            "net_sales FROM pos_tickets WHERE restaurant_id=? AND business_date>=? AND business_date<? "
            "AND COALESCE(cancelled,0)=0", (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()
        losses = {}
        for r in conn.execute(
                "SELECT ticket_id, kind, SUM(COALESCE(loss_amount, 0)) AS amt FROM pos_ticket_lines WHERE "
                "restaurant_id=? AND business_date>=? AND business_date<? AND kind IN ('comp','void') "
                "GROUP BY ticket_id, kind", (restaurant_id, str(start)[:10], str(end)[:10])).fetchall():
            losses.setdefault(r["kind"], {})[r["ticket_id"]] = float(r["amt"] or 0)
        # The meal's reviews put on the shift they were about (schedule_intel
        # .record_outcomes' `daypart` attribution only).
        reviews = {(str(r["date"])[:10], r["daypart"]): float(r["rating"]) for r in conn.execute(
            "SELECT date, daypart, review_rating_attributed AS rating FROM schedule_outcomes WHERE restaurant_id=? "
            "AND date>=? AND date<? AND review_rating_attributed IS NOT NULL",
            (restaurant_id, str(start)[:10], str(end)[:10])).fetchall()}
    except Exception:
        tickets, losses, reviews = [], {}, {}
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
        e = agg.setdefault((str(t["business_date"])[:10], part),
                           {"net": 0.0, "comps": 0.0, "voids": 0.0, "turns": [], "kitchen": []})
        e["net"] += float(t["net_sales"] or 0)
        e["comps"] += (losses.get("comp") or {}).get(t["ticket_id"], 0.0)
        e["voids"] += (losses.get("void") or {}).get(t["ticket_id"], 0.0)
        m = _sp._turn(dict(t))
        if m is not None:
            e["turns"].append(m)
        if t["fired_at"] and t["bumped_at"]:
            km = _sp._minutes(t["fired_at"], t["bumped_at"])
            if km is not None and 0 < km <= 120:
                e["kitchen"].append(km)
    for k, e in agg.items():
        if k in out:
            out[k]["comps_rate"] = (e["comps"] / e["net"]) if e["net"] > 0 else None
            out[k]["voids_rate"] = (e["voids"] / e["net"]) if e["net"] > 0 else None
            out[k]["turn"] = _median(e["turns"]) if len(e["turns"]) >= 5 else None
            out[k]["kitchen"] = _median(e["kitchen"]) if len(e["kitchen"]) >= 5 else None
    for k, v in reviews.items():
        if k in out:
            out[k]["review"] = v
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
                "y": {"sales": math.log(m["sales"] / base), "comps": m.get("comps_rate"),
                      "voids": m.get("voids_rate"), "turn": m.get("turn"), "kitchen": m.get("kitchen"),
                      "review": m.get("review")}})
    return out


def _previous(restaurant_id, db_path=DB_PATH) -> dict:
    """{(family, part): estimate} as stored before this run."""
    return stored(restaurant_id, db_path=db_path)


def _streak(old, move, today) -> int:
    """How many consecutive weekly estimates agree on `move` (0 for none):
    a rerun in the same week neither adds nor resets."""
    if not move:
        return 0
    if not old or int(old.get("move") or 0) != int(move):
        return 1
    n = int(old.get("streak") or 1)
    try:
        prev = date.fromisoformat(str(old.get("estimated_on"))[:10])
    except (TypeError, ValueError):
        return n + 1
    return n if (today - prev).days < 5 else n + 1


def _windows(old, binds_up, today) -> list:
    """The +1's binding windows [[from, to | None]], newest last: opened
    when a +1 starts binding (with the switch on), closed when it stops."""
    wins = [list(w) for w in ((old or {}).get("windows") or []) if isinstance(w, (list, tuple)) and len(w) == 2]
    open_ = bool(wins) and wins[-1][1] is None
    if binds_up and not open_:
        wins.append([today.isoformat(), None])
    elif not binds_up and open_:
        wins[-1][1] = today.isoformat()
    return wins[-WINDOWS_KEPT:]


def estimate_restaurant(restaurant_id, today=None, db_path=DB_PATH) -> dict:
    """Estimate every family x daypart and store it (staffing_elasticity).
    {(family, part): {"outcomes": {outcome: natural experiment}, "panel":
    {outcome: fixed effects, reported only}, "move", "net", "why", "tests",
    "streak", "binds", "target", "level", "weekdays", "typical_sales",
    "shift_cost", "estimated_on", "windows", ...}} — the moves decided
    together, one Holm step-down over every test of the run (module doc)."""
    import learning_stats as _ls
    import models as _m
    today = today if isinstance(today, date) else (date.fromisoformat(str(today)[:10]) if today else date.today())
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
    prev = _previous(restaurant_id, db_path=db_path)
    cells = {}
    hours_by = {}
    for (fam, part), units in units_for(restaurant_id, today=today, db_path=db_path, hours_out=hours_by).items():
        outcomes, panel = {}, {}
        for o in OUTCOMES:
            ne = natural_experiment(units, o)
            pa = panel_slope(units, o)
            if ne:
                outcomes[o] = ne
            if pa:
                panel[o] = pa
        if not outcomes and not panel:
            continue
        typical = _median([u["typical"] for u in units])
        hours = float(hours_by.get(fam) or DEFAULT_SHIFT_HOURS)
        cost = hours * (sum(fam_rate[fam]) / len(fam_rate[fam]) if fam in fam_rate else float(rate))
        wd_typ = {}
        for u in units:
            wd_typ.setdefault(u["weekday"], []).append(float(u["typical"]))
        cells[(fam, part)] = {"outcomes": outcomes, "panel": panel, "typical": typical, "cost": cost,
                              "hours": hours, "units": units,
                              "weekday_typical": {w: round(_median(v), 0) for w, v in wd_typ.items()},
                              "tests": cell_tests(outcomes, typical, cost, target)["tests"]}
    # One family of tests for the whole run (LEARNING-4).
    family = {(k, name): p for k, c in cells.items() for name, p in c["tests"].items()}
    survivors = _ls.holm(family, FAMILY_ALPHA) if family else set()
    applying = applies()
    out = {}
    for key, c in cells.items():
        fam, part = key
        passed = {name for (k, name) in survivors if k == key}
        d = decide(c["outcomes"], c["typical"], c["cost"], target, passed=passed)
        drive = c["outcomes"].get("sales") if d["why"] in ("pays", "costs") else \
            c["outcomes"].get(str(d["why"] or "").split(":", 1)[-1].split(",")[0]) if d["move"] else None
        level = (drive or {}).get("level")
        old = prev.get(key)
        streak = _streak(old, d["move"], today)
        binds = bool(d["move"]) and streak >= BIND_STREAK and level is not None
        out[key] = {"outcomes": c["outcomes"], "panel": c["panel"], "move": d["move"], "net": d["net"],
                    "why": d["why"], "tests": d["tests"], "family_alpha": FAMILY_ALPHA, "family_tests": len(family),
                    "typical_sales": round(c["typical"], 0) if c["typical"] else None, "shift_cost": round(c["cost"], 0),
                    "shift_hours": c["hours"], "meals": len(c["units"]),
                    "absences": sum(1 for u in c["units"] if u["absent"]),
                    "level": level, "target": (level + d["move"]) if (level is not None and d["move"]) else None,
                    "weekdays": apply_weekdays(d["move"], drive, c["weekday_typical"]) if d["move"] else [],
                    "weekday_typical": c["weekday_typical"], "streak": streak, "binds": binds,
                    "estimated_on": today.isoformat(),
                    "windows": _windows(old, applying and binds and d["move"] > 0, today)}
    # A cell that no longer has an estimate closes its window too.
    for key, old in prev.items():
        if key not in out and (old.get("windows") or []) and old["windows"][-1][1] is None:
            out[key] = {"outcomes": {}, "panel": {}, "move": 0, "net": None, "why": None, "tests": {},
                        "streak": 0, "binds": False, "estimated_on": today.isoformat(),
                        "windows": _windows(old, False, today), "meals": 0, "absences": 0}
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
    """{(family, part): estimate} as last stored (with `computed_at`); {} on
    any failure."""
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT family, daypart, estimate_json, computed_at FROM staffing_elasticity "
                                "WHERE restaurant_id=?", (restaurant_id,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            e = json.loads(r["estimate_json"] or "{}") or {}
        except (TypeError, ValueError):
            continue
        e.setdefault("computed_at", r["computed_at"])
        out[(r["family"], r["daypart"])] = e
    return out


FAMILY_WORDS = {"server": "server", "bartender": "bartender", "line_cook": "cook", "dish": "dishwasher",
                "host": "host", "busser": "busser", "runner": "runner", "barback": "barback",
                "prep_cook": "prep cook", "cashier": "cashier", "barista": "barista"}


def _days_words(days) -> str:
    days = [d for d in WEEKDAYS if d in set(days or ())]
    if not days or len(days) == 7:
        return ""
    return " on " + ", ".join(d[:3] for d in days)


def reason(fam, part, e) -> str:
    """The owner's words for one stored estimate's move."""
    meal = "lunch" if part == "morning" else "dinner"
    who = FAMILY_WORDS.get(fam, fam.replace("_", " "))
    net = e.get("net") or {}
    oc = e.get("outcomes") or {}
    when = _days_words(e.get("weekdays"))
    if e.get("move") == 1 and str(e.get("why") or "").startswith("service"):
        bits, n = [], 0
        for o in str(e["why"]).split(":", 1)[1].split(","):
            x = oc.get(o) or {}
            n = max(n, int(x.get("n") or 0))
            lo, hi = abs(x.get("hi") or 0), abs(x.get("lo") or 0)
            if o == "turn":
                bits.append(f"tables turned {abs(x.get('est') or 0):.0f} min faster (90% CI {lo:.0f}-{hi:.0f})")
            elif o == "kitchen":
                bits.append(f"kitchen tickets out {abs(x.get('est') or 0):.1f} min faster (90% CI {lo:.1f}-{hi:.1f})")
            elif o in ("comps", "voids"):
                bits.append(f"{o} {abs((x.get('est') or 0) * 100):.1f} points of sales lower")
            elif o == "review":
                bits.append(f"the meal's reviews {abs(x.get('est') or 0):.1f} stars higher")
        return (f"one more {who} at {meal}{when} measured better service here: " + "; ".join(bits)
                + f" (over {n} meals that ran one short)")
    if net:
        word = "one more" if e.get("move") == 1 else "one fewer" if e.get("move") == -1 else "one more"
        return (f"{word} {who} at {meal}{when}: one more measured {net.get('sales', 0):+,.0f} in sales a meal here "
                f"(90% CI {net.get('sales_lo', 0):+,.0f} to {net.get('sales_hi', 0):+,.0f}, from meals that ran one "
                f"short) against ${net.get('needs', 0):,.0f} of sales their shift needs at your labor target")
    return ""


def applies() -> bool:
    """Whether the measured value of one more person may move a requirement
    or price the solver's objective: OFF unless STAFFING_ELASTICITY_APPLY=1
    (blind re-audit 10/11/26). The estimator it was held off for is fixed
    (the natural experiment only, a Holm family over the run, two agreeing
    weeks, a target level, the weekdays it was measured at); the switch
    stays the lead's decision. The weekly job measures and stores either
    way."""
    import os
    return (os.environ.get("STAFFING_ELASTICITY_APPLY") or "0").strip().lower() in ("1", "on", "true", "yes")


def _fresh(e, today=None) -> bool:
    """A stored estimate recent enough to bind (LEARNING-24)."""
    today = today or date.today()
    when = e.get("estimated_on") or str(e.get("computed_at") or "")[:10]
    try:
        return (today - date.fromisoformat(str(when)[:10])).days <= STALE_DAYS
    except (TypeError, ValueError):
        return False


def moves(restaurant_id, db_path=DB_PATH, today=None) -> dict:
    """{(family, part): {"move", "target", "weekdays", "reason"}} — the
    stored moves that BIND: decided past the run's Holm step-down, agreed
    by BIND_STREAK consecutive weeks, computed within STALE_DAYS, with the
    level they were measured above (schedule_requirements' `elasticity`
    applies the move only up to `target` and only on `weekdays`). Empty
    unless applies()."""
    if not applies():
        return {}
    return {k: {"move": int(e.get("move") or 0), "target": int(e["target"]), "weekdays": list(e.get("weekdays") or []),
                "reason": reason(k[0], k[1], e)}
            for k, e in stored(restaurant_id, db_path=db_path).items()
            if int(e.get("move") or 0) and e.get("binds") and e.get("target") is not None and _fresh(e, today)}


def marginal_values(restaurant_id, db_path=DB_PATH, today=None) -> dict:
    """{(family, part): {"per_hour", "lo", "hi"}} — one more person's net
    sales value per hour of their shift, from the natural experiment (the
    solver objective's price of an hour there; contract for workstream F).
    Only fresh estimates whose sales net was read. Empty unless applies()."""
    out = {}
    if not applies():
        return out
    for k, e in stored(restaurant_id, db_path=db_path).items():
        net = e.get("net") or {}
        if net and e.get("shift_cost") and _fresh(e, today):
            hours = float(e.get("shift_hours") or DEFAULT_SHIFT_HOURS)
            out[k] = {"per_hour": round(net["est"] / hours, 2), "lo": round(net["lo"] / hours, 2),
                      "hi": round(net["hi"] / hours, 2)}
    return out


def upward_windows(restaurant_id, db_path=DB_PATH) -> dict:
    """{(family, part): [[from, to | None]]} — when a +1 bound the drafts
    (requirement_provenance.hold_down_trend's upward guard, LEARNING-6)."""
    return {k: [list(w) for w in (e.get("windows") or [])] for k, e in stored(restaurant_id, db_path=db_path).items()
            if e.get("windows")}


def estimate_one(restaurant_id, db_path=DB_PATH, today=None) -> dict:
    """One restaurant's weekly pass: the elasticity and the people's
    productivity (the same reads). The `learner` queue row's body
    (strategy_jobs.LEARNERS "staffing_learning")."""
    estimate_restaurant(restaurant_id, today=today, db_path=db_path)
    import staff_productivity as _prod
    _prod.estimate_restaurant(restaurant_id, today=today, db_path=db_path)
    return {"restaurant_id": restaurant_id, "ok": True}


def run_elasticity(db_path=None, today=None) -> dict:
    """The weekly job, inline: every Labor restaurant with a ticket archive,
    RUN_WORKERS at a time, bounded by RUN_MAX_SECONDS and resumable from
    CURSOR_KEY (scheduler.resumable_sweep); a pass that hits its bound is
    captured (the queue's fan-out — strategy_jobs.learner_pass — is the
    default when the job queue is on: one row per restaurant, LEARNING-24).
    Standard counts."""
    import models
    import scheduler as _sched
    import threading
    ids = [r.id for r in (models.get_all_restaurants(db_path) if db_path else models.get_all_restaurants())
           if getattr(r, "module_labor", 0)]
    tally = {"attempted": 0, "failed": 0}
    lock = threading.Lock()

    def _one(rid):
        with lock:
            tally["attempted"] += 1
        try:
            estimate_one(rid, today=today, db_path=db_path or DB_PATH)
        except Exception as e:
            with lock:
                tally["failed"] += 1
            import ops
            ops.capture(e, job="staffing_learning", context=f"restaurant_id={rid}", restaurant_id=rid)
    _done, ran_out = _sched.resumable_sweep(CURSOR_KEY, sorted(ids), _one, RUN_MAX_SECONDS, workers=RUN_WORKERS,
                                            job="staffing_learning")
    if ran_out:
        import ops
        ops.capture(RuntimeError(f"staffing_learning stopped at its {RUN_MAX_SECONDS}s bound; the rest lead the "
                                 "next pass"), job="staffing_learning", context="time_bound")
    return {"attempted": tally["attempted"], "ok": tally["attempted"] - tally["failed"], "failed": tally["failed"],
            "skipped": 0, "hit_bound": bool(ran_out)}
