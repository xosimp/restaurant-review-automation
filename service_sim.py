"""
service_sim.py — the counterfactual service simulator: replay a past night
with a different crew and say what the guest would have felt (scheduler
audit 10/10/26 MARKET-22).

Every WFM vendor stops at hours against sales. Cavnar AI holds, for an
RPOWER restaurant, every ticket's fired → bumped and opened → closed times,
its server, table and guests (pos_tickets) and every punch (pos_punches).
That is enough to fit a small queueing twin per restaurant and ask "what
would last Saturday have looked like with one fewer server?".

The twin, per stage (one per role family whose work the archive times):

  kitchen   the line (cross-restaurant family line_cook): a ticket's time
            is bumped − fired; it arrives when it was fired; the queue's
            servers are the cooks on the clock at that moment.
  floor     the dining room (family server): a table's time is closed −
            opened for a dining-room (not bar) ticket with guests; it
            arrives when it was opened; the queue's servers are the servers
            on the clock.

A REPLAY, not a formula: the night's own tickets arrive at their own
times, first come first served, each needing W minutes of one crew
member's attention, with as many crew members as the punches had on in
each quarter hour (± the change, never below zero; a quarter hour with
nobody of the family stays without them — the change is to who was on);
a ticket's time is P + its wait + W, P being the part no crew changes
(cooking, eating). A ticket that arrives when nobody can ever take it
before the night ends is "stranded" and counted, not averaged in.

P and W are FITTED, per stage, to every timed ticket of the last FIT_WEEKS:
for each W on a grid the nights are replayed as they ran and P is the
mean of what the replay does not explain (never below zero); the W with
the least squared error wins. The fit says how much of a ticket's time a
crew can change at all — a dining room whose P is most of its table time
is one where a server more or less barely moves it, and that is the
honest answer. `skill` is the fit's error against the plain mean.

  fit()         P, W, rmse, skill, nights per stage, and BOOT refits on
                nights resampled (the intervals). Under MIN_NIGHTS nights
                of timed tickets with punches a stage has no twin (said).
  replay()      one past night with a crew change ({family or stage: ±n}):
                the as-run mean, the predicted mean (the change's effect on
                the twin laid on the night as it ran), an 80% interval from
                the refits, the worst quarter hour, stranded tickets, the
                change in turns at a full room (floor), comps (the night's
                comp share moved by this restaurant's own comps-against-
                load slope, where MIN_NIGHTS nights measure one) and review
                risk (its own stars-per-minute slope of a night's service
                time, where MIN_REVIEW_NIGHTS reviewed nights measure it).
  what_if()     the Studio's "what this cut costs the guest": the last
                LOOKBACK same weekdays replayed with the change, averaged,
                with a sentence that states only what was measured
                (studio_bench.api_body 'what_if').

Integration (G2's staffing_elasticity): G2 MEASURES what one more person
did on the restaurant's own meals (uncovered no-shows as natural
experiments, fixed effects); this twin MODELS it. `what_if` lays G2's
measured kitchen and turn effect for dinner beside the model's
(`stages[...]["measured"]`, and a clause in the sentence) — side by side,
never blended. `marginal_minutes()` is the twin's derivative for any reader
that wants the model's view.

Reads only; nothing calls a model. RPOWER today (the only ticket archive);
another POS says why there is no twin (MARKET-9).
"""
import bisect
import logging
import math
import random
import threading
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

FIT_WEEKS = 10
BIN_MIN = 15
MIN_NIGHTS = 8                 # nights with timed tickets and punches before a stage has a twin
BOOT = 30                      # refits on resampled nights, for the intervals
LOOKBACK = 4                   # same weekdays a what-if replays
MIN_REVIEW_NIGHTS = 20         # reviewed nights before a star-per-minute slope is stated
W_GRID = (0.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 10, 12, 15, 18, 22, 27, 33, 40, 50)
MAX_TICKET_MINUTES = {"kitchen": 90, "floor": 240}
STAGES = {"kitchen": "line_cook", "floor": "server"}
STAGE_WORDS = {"kitchen": ("ticket time", "cook"), "floor": ("table time", "server")}
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 64


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def _ts(v):
    text = str(v or "").replace("T", " ")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return datetime.strptime(text[:n], fmt)
        except ValueError:
            continue
    return None


def _minute(t: datetime, night: date) -> float:
    """Minutes since the business night's midnight (past midnight goes on)."""
    return (t - datetime.combine(night, datetime.min.time())).total_seconds() / 60.0


# ── the replay ─────────────────────────────────────────────────────────────

def simulate(arrivals, people, W, delta=0):
    """Replay one night: `arrivals` sorted minutes, `people` {quarter hour:
    crew on}, W minutes of one crew member per ticket, `delta` crew changed
    in every quarter hour the family had anyone. [wait minutes or None
    (stranded)] in arrival order. First come first served."""
    cap = {}
    for k, c in people.items():
        cap[k] = max(0, c + delta) if c > 0 else 0
    if not cap:
        return [None] * len(arrivals)
    last_k = max(cap)

    def crew_at(t):
        return cap.get(int(t // BIN_MIN), 0)

    ends = []            # sorted end times of tickets in service
    out = []
    prev_start = 0.0
    for t in arrivals:
        s = max(t, prev_start)
        while True:
            if s // BIN_MIN > last_k:
                s = None
                break
            c = crew_at(s)
            if c <= 0:
                s = (int(s // BIN_MIN) + 1) * BIN_MIN
                continue
            i = bisect.bisect_right(ends, s)
            busy = len(ends) - i
            if busy < c:
                break
            # the (busy − c + 1)-th next finish frees a place
            s = ends[i + busy - c]
        if s is None:
            out.append(None)
            continue
        e = s + W
        bisect.insort(ends, e)
        prev_start = s
        out.append(s - t)
    return out


# ── the data ───────────────────────────────────────────────────────────────

def _families(restaurant_id, db_path=None):
    try:
        import models
        import schedule_rules
        return schedule_rules.role_families(models.get_restaurant(restaurant_id, db_path or models.DB_PATH)) or {}
    except Exception:
        return {}


def nights(restaurant_id, start, end, db_path=None) -> dict:
    """{night iso: {stage: {"arr": [minutes], "obs": [minutes or None],
    "comp": [bool], "people": {quarter hour: crew}, "tickets", "comped",
    "person_hours"}}} for business nights in [start, end) that have both
    timed tickets and punches of the stage's family."""
    from shift_quality import role_family
    from intelligence.staffing import family_of
    conn = get_conn(db_path)
    try:
        tickets = conn.execute(
            "SELECT business_date, opened_at, closed_at, fired_at, bumped_at, is_bar, guest_count, "
            "COALESCE(type_comp, 0) AS comp FROM pos_tickets WHERE restaurant_id=? AND business_date>=? AND "
            "business_date<? AND COALESCE(cancelled,0)=0", (restaurant_id, start, end)).fetchall()
        punches = conn.execute(
            "SELECT business_date, role, clock_in, clock_out FROM pos_punches WHERE restaurant_id=? AND "
            "business_date>=? AND business_date<? AND COALESCE(is_station,0)=0 AND clock_in IS NOT NULL",
            (restaurant_id, start, end)).fetchall()
    except Exception as e:
        log.warning("service twin: archive unreadable rid=%s: %s", restaurant_id, e)
        return {}
    finally:
        conn.close()
    if not tickets or not punches:
        return {}
    fams = _families(restaurant_id, db_path)
    on = {}
    for p in punches:
        try:
            night = date.fromisoformat(str(p["business_date"])[:10])
        except (TypeError, ValueError):
            continue
        # The restaurant's own family map first, then the cross-restaurant
        # family (labor_standards' unit: "Grill" and "Cook" are the line).
        fam = family_of(role_family(p["role"] or "", fams) or p["role"] or "")
        stage = next((s for s, f in STAGES.items() if f == fam), None)
        a, b = _ts(p["clock_in"]), _ts(p["clock_out"])
        if stage is None or a is None or b is None or b <= a:
            continue
        d = on.setdefault((night.isoformat(), stage), {})
        # On for a quarter hour when on for at least half of it.
        lo = int((_minute(a, night) + BIN_MIN / 2.0) // BIN_MIN)
        hi = int((_minute(b, night) - BIN_MIN / 2.0) // BIN_MIN)
        for k in range(lo, hi + 1):
            d[k] = d.get(k, 0) + 1
    raw = {}
    for t in tickets:
        try:
            night = date.fromisoformat(str(t["business_date"])[:10])
        except (TypeError, ValueError):
            continue
        for stage in STAGES:
            if stage == "kitchen":
                a, b = _ts(t["fired_at"]), _ts(t["bumped_at"])
            else:
                if t["is_bar"] or not t["guest_count"]:
                    continue
                a, b = _ts(t["opened_at"]), _ts(t["closed_at"])
            if a is None:
                continue
            m = (b - a).total_seconds() / 60.0 if (b is not None and b > a) else None
            if m is not None and m > MAX_TICKET_MINUTES[stage]:
                m = None
            raw.setdefault((night.isoformat(), stage), []).append(
                (_minute(a, night), m, stage == "floor" and float(t["comp"] or 0) > 0))
    out = {}
    for (night, stage), rows in raw.items():
        people = on.get((night, stage)) or {}
        if not people or not any(m is not None for _a, m, _c in rows):
            continue
        rows.sort(key=lambda r: r[0])
        out.setdefault(night, {})[stage] = {
            "arr": [r[0] for r in rows], "obs": [r[1] for r in rows], "comp": [r[2] for r in rows],
            "people": people, "tickets": len(rows), "comped": sum(1 for r in rows if r[2]),
            "person_hours": sum(people.values()) * BIN_MIN / 60.0}
    return out


# ── the fit ────────────────────────────────────────────────────────────────

def _terms(per_night):
    """{W: [(num, n, sq) per night]} — each night replayed once per W; P is
    then closed form, and a refit on resampled nights reuses the replays."""
    out = {}
    for W in W_GRID:
        rows = []
        for night in per_night:
            waits = simulate(night["arr"], night["people"], W)
            res = []
            for w, obs in zip(waits, night["obs"]):
                if w is None or obs is None:
                    continue
                res.append(obs - W - w)
            rows.append(res)
        out[W] = rows
    return out


def _best(terms, idx):
    """(P, W, rmse) over the nights at `idx` (a resample)."""
    best = None
    for W, rows in terms.items():
        res = [x for i in idx for x in rows[i]]
        if not res:
            continue
        P = max(0.0, sum(res) / len(res))
        sse = sum((x - P) ** 2 for x in res) / len(res)
        if best is None or sse < best[2]:
            best = (P, W, sse)
    return (best[0], best[1], math.sqrt(best[2])) if best else None


def fit(restaurant_id, today=None, db_path=None, weeks=FIT_WEEKS, data=None) -> dict:
    """{stage: {available, P, W, rmse, naive_rmse, skill, nights, boot:
    [(P, W)]} or {available: False, reason}} — module doc. Cached per
    restaurant and day (a per-process cache: safe per worker)."""
    today = today or date.today()
    key = (restaurant_id, today.isoformat(), weeks, db_path or "")
    with _CACHE_LOCK:
        if key in _CACHE:
            return _CACHE[key]
    if data is None:
        start = (today - timedelta(weeks=int(weeks))).isoformat()
        data = nights(restaurant_id, start, today.isoformat(), db_path)
    out = {}
    for stage in STAGES:
        per_night = [s[stage] for _n, s in sorted(data.items()) if stage in s]
        if len(per_night) < MIN_NIGHTS:
            out[stage] = {"available": False, "nights": len(per_night),
                          "reason": (f"Needs {MIN_NIGHTS} nights of timed tickets with punches; "
                                     f"{len(per_night)} so far." if per_night else
                                     "No timed tickets with punches yet: the simulator needs the POS ticket archive "
                                     "(RPOWER today).")}
            continue
        terms = _terms(per_night)
        idx = list(range(len(per_night)))
        best = _best(terms, idx)
        if best is None:
            out[stage] = {"available": False, "nights": len(per_night), "reason": "No ticket could be replayed."}
            continue
        obs = [o for n in per_night for o in n["obs"] if o is not None]
        mean = sum(obs) / len(obs)
        naive = math.sqrt(sum((o - mean) ** 2 for o in obs) / len(obs))
        rng = random.Random(f"{restaurant_id}:{stage}:{today.isoformat()}")
        boot = []
        for _ in range(BOOT):
            b = _best(terms, [rng.randrange(len(idx)) for _ in idx])
            if b:
                boot.append((round(b[0], 2), b[1]))
        out[stage] = {"available": True, "P": round(best[0], 2), "W": best[1], "rmse": round(best[2], 2),
                      "naive_rmse": round(naive, 2), "skill": round(1 - best[2] / naive, 3) if naive else None,
                      "nights": len(per_night), "boot": boot}
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = out
    return out


def marginal_minutes(restaurant_id, stage, per_hour, people, today=None, db_path=None, reps=40):
    """Minutes per ticket one more person saves over an hour of `per_hour`
    tickets arriving at random (Poisson, seeded — the same arrivals for both
    crews) with `people` on: the twin's derivative, the read G2's staffing
    elasticity can share. None without a twin."""
    f = (fit(restaurant_id, today=today, db_path=db_path) or {}).get(stage) or {}
    if not f.get("available") or per_hour <= 0 or people <= 0:
        return None
    rng = random.Random(f"{restaurant_id}:{stage}:{per_hour}:{people}")
    crew = {k: int(people) for k in range(0, 4 * 4)}
    now_w, more_w = [], []
    for _ in range(int(reps)):
        t, arr = 0.0, []
        while True:
            t += rng.expovariate(per_hour / 60.0)
            if t >= 60.0:
                break
            arr.append(t)
        now_w += [w for w in simulate(arr, crew, f["W"]) if w is not None]
        more_w += [w for w in simulate(arr, crew, f["W"], delta=1) if w is not None]
    if not now_w or not more_w:
        return None
    return round(sum(now_w) / len(now_w) - sum(more_w) / len(more_w), 2)


# ── the replay of a past night ─────────────────────────────────────────────

def _mean_time(night, P, W, delta):
    """(mean predicted minutes, worst quarter hour's mean, stranded)."""
    waits = simulate(night["arr"], night["people"], W, delta)
    got = [(a, P + W + w) for a, w in zip(night["arr"], waits) if w is not None]
    stranded = sum(1 for w in waits if w is None)
    if not got:
        return None, None, stranded
    by_q = {}
    for a, x in got:
        by_q.setdefault(int(a // BIN_MIN), []).append(x)
    worst = max(sum(v) / len(v) for v in by_q.values())
    return sum(x for _a, x in got) / len(got), worst, stranded


def _q(vals, q):
    s = sorted(v for v in vals if v is not None)
    if not s:
        return None
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _slope(pts):
    if len(pts) < 2:
        return None
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    vx = sum((x - mx) ** 2 for x, _ in pts)
    return (sum((x - mx) * (y - my) for x, y in pts) / vx) if vx > 0 else None


def _comp_slope(data):
    """The least-squares slope of a night's comp share on its tables per
    server-hour, across nights; None under MIN_NIGHTS or with no comp."""
    pts = [(e["tickets"] / e["person_hours"], e["comped"] / float(e["tickets"]))
           for s in data.values() for e in [s.get("floor")] if e and e["tickets"] and e["person_hours"] > 0]
    if len(pts) < MIN_NIGHTS or not any(y > 0 for _x, y in pts):
        return None
    return _slope(pts)


def _review_slope(restaurant_id, data, stage, db_path=None):
    """Stars per minute of a night's mean time, from the restaurant's own
    reviews dated that night (least squares; None under MIN_REVIEW_NIGHTS)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT substr(COALESCE(review_date, fetched_at),1,10) AS d, AVG(rating) AS r FROM reviews "
                            "WHERE restaurant_id=? AND rating IS NOT NULL GROUP BY d", (restaurant_id,)).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    stars = {str(r["d"]): float(r["r"]) for r in rows if r["d"]}
    pts = []
    for n, s in data.items():
        e = s.get(stage)
        obs = [o for o in (e or {}).get("obs") or [] if o is not None]
        if obs and n in stars:
            pts.append((sum(obs) / len(obs), stars[n]))
    if len(pts) < MIN_REVIEW_NIGHTS:
        return None
    return _slope(pts)


def _want(change) -> dict:
    want = {}
    for k, v in (change or {}).items():
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        k = str(k).strip().lower()
        stage = k if k in STAGES else next((s for s, f in STAGES.items() if f == k), None)
        if stage is None:
            try:
                from intelligence.staffing import family_of
                stage = next((s for s, f in STAGES.items() if f == family_of(k)), None)
            except Exception:
                stage = None
        if stage:
            want[stage] = want.get(stage, 0) + v
    return want


def replay(restaurant_id, night, change: dict, today=None, db_path=None, data=None, fits=None) -> dict:
    """One past night replayed with `change` ({family, role or stage: ±n}).
    {ok, night, stages: {stage: {as_run, predicted, moved, moved_lo,
    moved_hi, worst_quarter, stranded, turns_pct?}}, comps?, review_risk}."""
    today = today or date.today()
    night = str(night)[:10]
    if data is None:
        data = nights(restaurant_id, (today - timedelta(weeks=FIT_WEEKS)).isoformat(), today.isoformat(), db_path)
    fits = fit(restaurant_id, today=today, db_path=db_path, data=data) if fits is None else fits
    n = data.get(night)
    if not n:
        return {"ok": False, "night": night, "error": "No timed tickets with punches for that night."}
    want = _want(change)
    stages = {}
    for stage, e in n.items():
        f = fits.get(stage) or {}
        if not f.get("available"):
            stages[stage] = {"available": False, "reason": f.get("reason")}
            continue
        delta = want.get(stage, 0)
        base, _w0, strand0 = _mean_time(e, f["P"], f["W"], 0)
        pred, worst, stranded = _mean_time(e, f["P"], f["W"], delta)
        obs = [o for o in e["obs"] if o is not None]
        as_run = sum(obs) / len(obs)
        # The change measured on the twin and laid on the night as it ran:
        # the twin's own error on this night is never read as the change.
        moved = (pred - base) if (pred is not None and base is not None) else None
        moves = []
        for P, W in f["boot"]:
            b0 = _mean_time(e, P, W, 0)[0]
            b1 = _mean_time(e, P, W, delta)[0]
            if b0 is not None and b1 is not None:
                moves.append(b1 - b0)
        st = {"available": True, "as_run": round(as_run, 1), "change": delta,
              "predicted": round(as_run + moved, 1) if moved is not None else None,
              "moved": round(moved, 1) if moved is not None else None,
              "moved_lo": round(_q(moves, 0.1), 1) if moves else None,
              "moved_hi": round(_q(moves, 0.9), 1) if moves else None,
              "worst_quarter": round(worst, 1) if worst is not None else None,
              "stranded": max(0, stranded - strand0), "fit_skill": f.get("skill"), "nights_fitted": f.get("nights")}
        if stage == "floor" and moved is not None and base:
            # At a full room the tables a night seats scale with table time.
            st["turns_pct"] = round(100.0 * (base / (base + moved) - 1.0), 1) if base + moved > 0 else None
        stages[stage] = st
    res = {"ok": True, "night": night, "stages": stages}
    e = n.get("floor")
    if (stages.get("floor") or {}).get("available") and want.get("floor") and e and e["person_hours"] > 0:
        slope = _comp_slope(data)
        if slope is not None:
            hrs2 = sum(max(0, c + want["floor"]) for c in e["people"].values() if c > 0) * BIN_MIN / 60.0
            if hrs2 > 0:
                share = e["comped"] / float(e["tickets"]) + slope * (e["tickets"] / hrs2 - e["tickets"] / e["person_hours"])
                res["comps"] = {"as_run": e["comped"], "predicted": round(max(0.0, share) * e["tickets"], 1),
                                "basis": "the night's comp share moved by this restaurant's own comps against tables "
                                         "per server-hour, across its nights"}
    res["review_risk"] = {"stars": None, "basis": f"Not stated: fewer than {MIN_REVIEW_NIGHTS} reviewed nights with "
                                                  "timed tickets."}
    for stage in ("floor", "kitchen"):
        st = stages.get(stage) or {}
        if st.get("available") and st.get("moved"):
            slope = _review_slope(restaurant_id, data, stage, db_path)
            if slope is not None:
                res["review_risk"] = {
                    "stage": stage, "stars": round(slope * st["moved"], 3),
                    "stars_lo": round(slope * st["moved_lo"], 3) if st.get("moved_lo") is not None else None,
                    "stars_hi": round(slope * st["moved_hi"], 3) if st.get("moved_hi") is not None else None,
                    "basis": f"this restaurant's reviews against its nights' {STAGE_WORDS[stage][0]}"}
                break
    return res


def what_if(restaurant_id, weekday, change: dict, today=None, db_path=None) -> dict:
    """The last LOOKBACK same-weekday nights replayed with `change`,
    averaged per stage: {ok, available, weekday, change, nights: [M/D/YY],
    stages, comps?, review_risk?, sentence, basis}."""
    from time_utils import mdy
    today = today or date.today()
    data = nights(restaurant_id, (today - timedelta(weeks=FIT_WEEKS)).isoformat(), today.isoformat(), db_path)
    fits = fit(restaurant_id, today=today, db_path=db_path, data=data)
    wd = str(weekday or "").strip().capitalize()
    picks = sorted((n for n in data if date.fromisoformat(n).strftime("%A") == wd), reverse=True)[:LOOKBACK]
    unavailable = {s: f.get("reason") for s, f in fits.items() if not f.get("available")}
    if not picks or len(unavailable) == len(STAGES):
        return {"ok": True, "available": False, "weekday": wd, "change": change,
                "reason": next(iter(unavailable.values()), None) or f"No {wd} nights with timed tickets yet."}
    runs = [replay(restaurant_id, n, change, today=today, db_path=db_path, data=data, fits=fits) for n in picks]
    stages = {}
    for stage in STAGES:
        got = [r["stages"][stage] for r in runs if r.get("ok") and (r["stages"].get(stage) or {}).get("available")]
        if not got:
            if stage in unavailable:
                stages[stage] = {"available": False, "reason": unavailable[stage]}
            continue

        def avg(k, got=got):
            vals = [g[k] for g in got if g.get(k) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None
        stages[stage] = {"available": True, "nights": len(got), "as_run": avg("as_run"), "predicted": avg("predicted"),
                         "moved": avg("moved"), "moved_lo": avg("moved_lo"), "moved_hi": avg("moved_hi"),
                         "turns_pct": avg("turns_pct"), "stranded": sum(g.get("stranded") or 0 for g in got),
                         "change": got[0]["change"], "fit_skill": got[0]["fit_skill"]}
    # The measured check beside the model (G2's staffing_elasticity: what one
    # more of the family on a dinner did to the kitchen's fire-to-bump
    # minutes and the table turn, from the restaurant's own short nights and
    # meals, with its interval) — the twin's answer and the measured one side
    # by side, never blended.
    try:
        import staffing_elasticity as _sel
        import models as _m
        measured = _sel.stored(restaurant_id, db_path=db_path or _m.DB_PATH) or {}
    except Exception:
        measured = {}
    for stage, outcome in (("kitchen", "kitchen"), ("floor", "turn")):
        e = (((measured.get((STAGES[stage], "night")) or {}).get("outcomes")) or {}).get(outcome)
        st_ = stages.get(stage) or {}
        if e and e.get("est") is not None and st_.get("available") and st_.get("change"):
            st_["measured"] = {"per_person": e["est"], "lo": e.get("lo"), "hi": e.get("hi"), "n": e.get("n"),
                               "method": e.get("method"), "implied": round(st_["change"] * e["est"], 1),
                               "basis": "dinners at this restaurant that ran one short against the rest "
                                        "(staffing_elasticity), per person"}
    comps = [r["comps"] for r in runs if r.get("comps")]
    stars = [r["review_risk"] for r in runs if (r.get("review_risk") or {}).get("stars") is not None]
    out = {"ok": True, "available": any(s.get("available") for s in stages.values()), "weekday": wd,
           "change": change, "nights": [mdy(n) for n in picks], "stages": stages}
    if comps:
        out["comps"] = {"as_run": round(sum(c["as_run"] for c in comps) / len(comps), 1),
                        "predicted": round(sum(c["predicted"] for c in comps) / len(comps), 1),
                        "basis": comps[0]["basis"]}
    if stars:
        out["review_risk"] = {"stars": round(sum(s["stars"] for s in stars) / len(stars), 3),
                              "stars_lo": _q([s.get("stars_lo") for s in stars], 0.5),
                              "stars_hi": _q([s.get("stars_hi") for s in stars], 0.5), "basis": stars[0]["basis"]}
    out["sentence"] = _sentence(out)
    out["basis"] = (f"A queueing model fitted to this restaurant's own ticket times and punches over the last "
                    f"{FIT_WEEKS} weeks, replaying the last {len(picks)} {wd}s with the change; the range is 80% of "
                    "refits on resampled nights. It is what a crew change does to the guest, not to sales.")
    return out


def _sentence(w) -> str:
    bits = []
    for stage in ("floor", "kitchen"):
        s = (w.get("stages") or {}).get(stage) or {}
        if not s.get("available") or s.get("moved") is None or not s.get("change"):
            continue
        what, who = STAGE_WORDS[stage]
        n = abs(int(s["change"]))
        crew = f"{n} {'fewer' if s['change'] < 0 else 'more'} {who}{'s' if n != 1 else ''}"
        rng = (f" (between {s['moved_lo']:+g} and {s['moved_hi']:+g} minutes)"
               if s.get("moved_lo") is not None and s.get("moved_hi") is not None else "")
        if abs(s["moved"]) < 0.5:
            bits.append(f"With {crew}, {w['weekday']}'s {what} would have stayed about the same{rng}")
        else:
            bits.append(f"With {crew}, {w['weekday']}'s {what} would have run about {abs(s['moved']):g} minutes "
                        f"{'longer' if s['moved'] > 0 else 'shorter'}{rng}")
        m = s.get("measured") or {}
        if m.get("implied") is not None:
            bits.append(f"your own short dinners measured about {abs(m['implied']):g} minutes "
                        f"{'longer' if m['implied'] > 0 else 'shorter'}")
        if s.get("stranded"):
            bits.append(f"{s['stranded']} ticket{'s' if s['stranded'] != 1 else ''} would have had nobody to take "
                        f"{'them' if s['stranded'] != 1 else 'it'}")
    if not bits:
        return ""
    rr = w.get("review_risk") or {}
    tail = (f"; on this restaurant's own reviews that is about {abs(rr['stars']):.2f} stars"
            if rr.get("stars") else "")
    return "; ".join(bits) + tail + "."
