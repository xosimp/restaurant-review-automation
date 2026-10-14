"""
outcome_benchmark.py — the outcome benchmark: weeks Cavnar AI drafted
against weeks the manager built, on what actually happened (scheduler audit
10/10/26 MARKET-21, RM-22).

Cavnar AI's real differentiators (team strength, a manager every minute,
review-aware staffing) are invisible in a demo, where competitors show a
forecast-accuracy percentage. The proof is outcomes, measured the
value_delivered way: measured, caveated, never mixed with an opportunity
or a projection.

  week_outcomes()  the SAME as-run measures for any week, whoever
                   built it, from the same sources:
                     labor_pct          hourly labor % (labor_daily_history:
                                        the POS's labor cost ÷ sales; at
                                        least MIN_DAYS days of the week)
                     ot_hours           hours past 40 worked by hourly staff
                                        (the punches, shift_facts)
                     unmanaged_minutes  (published weeks only) minutes on the
                                        floor with no manager as run - G1's
                                        scorecard row for the week
                     cover_rate         of the no-shows and call-outs, the
                                        share somebody covered (attendance
                                        on watched nights; None with none)
                     service_complaints the share of the week's reviews that
                                        are 3 stars or under and name service
                                        (MIN_REVIEWS reviews, else None)
  week_class()     "cavnar" — a published week whose first version Cavnar
                   AI drafted (a 'generated' schedule version); "manager" —
                   a published week built without a draft, or a week before
                   the first Cavnar AI draft (the punches, no schedule here).
  restaurant_view() the owner's before/after: per measure, the mean of each
                   class and the SEASON-MATCHED difference (blind re-audit
                   10/11/26 LEARNING-29: weeks compared as if independent and
                   season-blind, a claim on any of five measures, 4 weeks a
                   side): each Cavnar AI week against the manager's weeks of
                   the same time of year (week of the year within
                   SEASON_BAND_WEEKS, any year on file — WEEKS back), the
                   mean of those paired differences with a 90% interval
                   from both sides' variances, each widened for serially
                   correlated weeks (season_matched), "better" or "worse"
                   only past Holm's step-
                   down at FAMILY_ALPHA over the five measures — stated only
                   with MIN_WEEKS matched Cavnar AI weeks and MIN_WEEKS
                   manager weeks behind them; the caveat that the weeks are
                   still different weeks (demand, staff), so a difference is
                   an association, not a controlled effect.
  platform_view()  the admin readout (RM-22): each eligible restaurant's
                   difference (restaurants that may teach — intelligence.
                   jobs.excluded_learning_ids), the restaurant-cluster mean
                   and 90% interval, and the PUBLISHED figure only when the
                   restaurants clear MIN_COHORT and the organisations behind
                   them clear the band floors (MIN_ORGS, no organisation over
                   a third — intelligence.privacy.orgs_ok), the payload
                   passing assert_anonymous. Below the floor: the counts and
                   "not published", never a figure. A claim is made only
                   when the interval excludes no difference.

Reads only; nothing calls a model.
"""
import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

WEEKS = 104                     # two years: last year's same season is a Cavnar AI week's match
MIN_DAYS = 5
MIN_REVIEWS = 3
MIN_WEEKS = 8
SEASON_BAND_WEEKS = 3           # a manager week matches a Cavnar AI week within this many weeks of the year
FAMILY_ALPHA = 0.10             # Holm over the five measures
MEASURES = {
    "labor_pct": {"label": "Hourly labor %", "better": "lower", "unit": "%"},
    "ot_hours": {"label": "Overtime hours", "better": "lower", "unit": "h"},
    "cover_rate": {"label": "No-shows covered", "better": "higher", "unit": "share"},
    "service_complaints": {"label": "Reviews complaining about service", "better": "lower", "unit": "share"},
    # From G1's Schedule Learning Scorecard (schedule_learning_scores), for
    # published weeks only - a week before Cavnar AI has no row, so this one
    # compares drafted weeks with weeks the manager built and published here.
    "unmanaged_minutes": {"label": "Minutes on the floor with no manager", "better": "lower", "unit": "min"},
}
UNIT_WORD = {"share": " points", "%": " points", "h": " hours", "min": " minutes"}
SERVICE_WORDS = ("service", "slow", "wait", "staff", "server", "rude", "attentive")


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _hours(r):
    for k in ("actual_hours", "scheduled_hours", "hours"):
        try:
            v = r.get(k)
            if v not in (None, ""):
                return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _week_starts(restaurant_id, today, db_path=None) -> list:
    import models
    import schedule_engine
    r = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
    sd = schedule_engine.week_start_day_of(r)
    last = models._week_start_of(today, sd) - timedelta(days=7)       # the last week that has ended
    return [last - timedelta(weeks=i) for i in range(WEEKS)]


def week_class(restaurant_id, week_start, db_path=None):
    """('cavnar' | 'manager', history_id or None) — module doc."""
    ws = week_start.isoformat() if hasattr(week_start, "isoformat") else str(week_start)[:10]
    conn = get_conn(db_path)
    try:
        h = conn.execute("SELECT id FROM schedule_history h WHERE restaurant_id=? AND substr(week_start,1,10)=? AND "
                         "published_at IS NOT NULL AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
                         (restaurant_id, ws)).fetchone()
        if not h:
            return "manager", None
        gen = conn.execute("SELECT 1 FROM schedule_versions WHERE history_id=? AND reason='generated' LIMIT 1",
                           (h["id"],)).fetchone()
    finally:
        conn.close()
    return ("cavnar" if gen else "manager"), h["id"]


def week_outcomes(restaurant_id, week_start, data=None, db_path=None) -> dict:
    """The four as-run measures of one week (module doc); None where the
    week has no measurement of it."""
    d0 = week_start if hasattr(week_start, "isoformat") else date.fromisoformat(str(week_start)[:10])
    d1 = d0 + timedelta(days=6)
    a, b = d0.isoformat(), d1.isoformat()
    data = data or {}
    out = {"week_start": a}
    days = [x for x in data.get("labor", []) if a <= x[0] <= b]
    if len(days) >= MIN_DAYS and sum(x[2] for x in days) > 0:
        out["labor_pct"] = round(100.0 * sum(x[1] for x in days) / sum(x[2] for x in days), 2)
    else:
        out["labor_pct"] = None
    punches = [p for p in data.get("punches", []) if a <= str(p.get("date"))[:10] <= b]
    if punches:
        per = {}
        for p in punches:
            k = _key(p.get("employee"))
            if k and k not in data.get("salaried", set()):
                per[k] = per.get(k, 0.0) + _hours(p)
        out["ot_hours"] = round(sum(max(0.0, h - 40.0) for h in per.values()), 2)
    else:
        out["ot_hours"] = None
    att = [e for e in data.get("attendance", []) if a <= e[0] <= b]
    covered = sum(1 for _d, o, cov in att if o == "covered" or (o in ("no_show", "called_out") and cov))
    missed = sum(1 for _d, o, cov in att if o in ("no_show", "called_out") and not cov)
    out["cover_rate"] = round(covered / float(covered + missed), 3) if (covered + missed) else None
    revs = [r for r in data.get("reviews", []) if a <= r[0] <= b]
    if len(revs) >= MIN_REVIEWS:
        bad = sum(1 for _d, rating, text in revs if rating <= 3 and any(w in text for w in SERVICE_WORDS))
        out["service_complaints"] = round(bad / float(len(revs)), 3)
    else:
        out["service_complaints"] = None
    return out


def _load(restaurant_id, since, until, db_path=None) -> dict:
    data = {"labor": [], "punches": [], "attendance": [], "reviews": [], "salaried": set()}
    conn = get_conn(db_path)
    try:
        data["labor"] = [(str(r["date"])[:10], float(r["labor_cost"] or 0), float(r["sales"] or 0)) for r in conn.execute(
            "SELECT date, labor_cost, sales FROM labor_daily_history WHERE restaurant_id=? AND date>=? AND date<=? AND "
            "sales>0", (restaurant_id, since, until)).fetchall()]
        data["attendance"] = [(str(r["business_date"])[:10], r["outcome"], (r["covered_by"] or "").strip())
                              for r in conn.execute("SELECT business_date, outcome, covered_by FROM attendance_events "
                                                    "WHERE restaurant_id=? AND business_date>=? AND business_date<=?",
                                                    (restaurant_id, since, until)).fetchall()]
        for r in conn.execute("SELECT review_date, rating, text, categories FROM reviews WHERE restaurant_id=? AND "
                              "substr(review_date,1,10)>=? AND substr(review_date,1,10)<=?",
                              (restaurant_id, since, until)).fetchall():
            data["reviews"].append((str(r["review_date"])[:10], int(r["rating"] or 0),
                                    (str(r["text"] or "") + " " + str(r["categories"] or "")).lower()))
    except Exception as e:
        log.warning("benchmark: records unreadable rid=%s: %s", restaurant_id, e)
    finally:
        conn.close()
    try:
        import shift_facts
        data["punches"] = shift_facts.person_rows(restaurant_id, since=since, until=until, db_path=db_path)
    except Exception as e:
        log.warning("benchmark: punches unreadable rid=%s: %s", restaurant_id, e)
    try:
        import models
        data["salaried"] = {_key(n) for n in (models.salaried_keys(models.get_restaurant(restaurant_id, db_path or
                                                                                          models.DB_PATH)) or [])}
    except Exception:
        pass
    return data


def weeks_for(restaurant_id, today=None, db_path=None) -> list:
    """[{week_start, class, history_id, measures...}] for the last WEEKS
    ended weeks that carry any measurement."""
    today = today or date.today()
    starts = _week_starts(restaurant_id, today, db_path)
    data = _load(restaurant_id, min(starts).isoformat(), (max(starts) + timedelta(days=6)).isoformat(), db_path)
    scored = _scorecard(restaurant_id, db_path)
    out = []
    for ws in sorted(starts):
        o = week_outcomes(restaurant_id, ws, data=data, db_path=db_path)
        cls, hid = week_class(restaurant_id, ws, db_path)
        if all(o.get(m) is None for m in MEASURES if m != "unmanaged_minutes") and scored.get(hid) is None:
            continue
        o["unmanaged_minutes"] = scored.get(hid) if hid else None
        out.append(dict(o, **{"class": cls, "history_id": hid}))
    return out


def _scorecard(restaurant_id, db_path=None) -> dict:
    """{history_id: unmanaged minutes as run} from the scorecard's rows."""
    conn = get_conn(db_path)
    try:
        return {r["history_id"]: r["unmanaged_minutes"] for r in conn.execute(
            "SELECT history_id, unmanaged_minutes FROM schedule_learning_scores WHERE restaurant_id=? AND "
            "unmanaged_minutes IS NOT NULL", (restaurant_id,)).fetchall()}
    except Exception:
        return {}
    finally:
        conn.close()


def _woy(week_start):
    d = week_start if hasattr(week_start, "isocalendar") else date.fromisoformat(str(week_start)[:10])
    return d.isocalendar()[1]


def _ar1_inflation(vals):
    """(variance inflation, effective n) of a weekly series' mean for its
    lag-1 correlation (bias-corrected, held to [0, 0.9])."""
    n = len(vals)
    if n < 3:
        return 1.0, float(n)
    m = sum(vals) / n
    dev = [v - m for v in vals]
    ss = sum(d * d for d in dev)
    rho = 0.0
    if ss > 0:
        r = sum(dev[t] * dev[t - 1] for t in range(1, n)) / ss
        rho = max(0.0, min(0.9, r + (1.0 + 3.0 * r) / n))
    infl = (1.0 + rho) / (1.0 - rho)
    return infl, n / infl


def season_matched(weeks, m) -> dict | None:
    """The season-matched difference for measure `m` (module doc): each
    Cavnar AI week's value minus the mean of the manager weeks of the same
    time of year (week of the year within SEASON_BAND_WEEKS, circular); a
    Cavnar AI week with no such manager week is left out. Its mean equals
    the Cavnar AI weeks' mean minus a WEIGHTED mean of the manager weeks
    used (a manager week matched by several Cavnar AI weeks weighs more),
    so its variance is each side's — the manager side through its weights,
    since overlapping matches are not independent — each widened for its
    own serial correlation. {difference, lo, hi, p, cavnar_weeks,
    manager_weeks} or None when nothing matched."""
    import learning_stats as _ls
    man = sorted((w["week_start"], _woy(w["week_start"]), float(w[m])) for w in weeks
                 if w["class"] == "manager" and w.get(m) is not None)
    cav, weight = [], {}
    for w in sorted((w for w in weeks if w["class"] == "cavnar" and w.get(m) is not None),
                    key=lambda w: w["week_start"]):
        k = _woy(w["week_start"])
        near = [ws for ws, wk, _v in man if min(abs(wk - k), 52 - abs(wk - k)) <= SEASON_BAND_WEEKS]
        if not near:
            continue
        cav.append(float(w[m]))
        for ws in near:
            weight[ws] = weight.get(ws, 0.0) + 1.0 / len(near)
    if not cav:
        return None
    n = len(cav)
    mvals = [v for ws, _wk, v in man if ws in weight]
    wts = [weight[ws] / n for ws, _wk, _v in man if ws in weight]
    mc = sum(cav) / n
    mm = sum(w * v for w, v in zip(wts, mvals))
    diff = mc - mm
    out = {"difference": round(diff, 4), "lo": None, "hi": None, "p": None, "cavnar_weeks": n,
           "manager_weeks": len(mvals)}
    if n < 3 or len(mvals) < 3:
        return out
    vc = sum((v - mc) ** 2 for v in cav) / (n - 1)
    mbar = sum(mvals) / len(mvals)
    vm = sum((v - mbar) ** 2 for v in mvals) / (len(mvals) - 1)
    ic, nc = _ar1_inflation(cav)
    im, nm = _ar1_inflation(mvals)
    se = (vc / n * ic + vm * sum(w * w for w in wts) * im) ** 0.5
    df = max(1.0, min(nc, nm) - 1.0)
    q = _ls.t_ppf(0.95, df)
    out.update(lo=round(diff - q * se, 4), hi=round(diff + q * se, 4), p=_ls.t_pvalue(diff, se, df))
    return out


def compare(weeks) -> dict:
    """{measure: {cavnar: {mean, n}, manager: {mean, n}, matched, difference,
    lo, hi, p, stated, verdict}} — the season-matched difference (module
    doc), stated only with MIN_WEEKS matched weeks on each side, a verdict
    only past Holm over the measures."""
    import learning_stats as _ls
    out, tests = {}, {}
    for m, spec in MEASURES.items():
        a = [w[m] for w in weeks if w["class"] == "cavnar" and w.get(m) is not None]
        b = [w[m] for w in weeks if w["class"] == "manager" and w.get(m) is not None]
        e = {"label": spec["label"], "better": spec["better"], "cavnar": {"n": len(a), "mean": round(sum(a) / len(a), 3) if a else None},
             "manager": {"n": len(b), "mean": round(sum(b) / len(b), 3) if b else None},
             "matched": {"cavnar": 0, "manager": 0}, "difference": None, "lo": None, "hi": None, "p": None,
             "stated": False, "verdict": None}
        got = season_matched(weeks, m) or {}
        e["matched"] = {"cavnar": got.get("cavnar_weeks", 0), "manager": got.get("manager_weeks", 0)}
        if e["matched"]["cavnar"] >= MIN_WEEKS and e["matched"]["manager"] >= MIN_WEEKS and got.get("p") is not None:
            e.update({"difference": got["difference"], "lo": got["lo"], "hi": got["hi"], "p": got["p"], "stated": True})
            tests[m] = got["p"]
        out[m] = e
    sure = _ls.holm(tests, FAMILY_ALPHA) if tests else set()
    for m, e in out.items():
        if not e["stated"]:
            continue
        lower = e["difference"] < 0
        good = lower if MEASURES[m]["better"] == "lower" else not lower
        e["verdict"] = ("better" if good else "worse") if m in sure else "no clear difference"
    return out


def _fmt(m, v):
    if v is None:
        return None
    unit = MEASURES[m]["unit"]
    if unit == "share":
        return f"{v * 100:.0f}%"
    if unit == "%":
        return f"{v:.1f}%"
    if unit == "min":
        return f"{v:.0f} min"
    return f"{v:.1f}h"


def _diff(m, v):
    """A difference in the measure's own unit, signed."""
    unit = MEASURES[m]["unit"]
    if unit == "share":
        return f"{v * 100:+.0f} pts"
    if unit == "%":
        return f"{v:+.1f} pts"
    if unit == "min":
        return f"{v:+.0f} min"
    return f"{v:+.1f}h"


def restaurant_view(restaurant_id, today=None, db_path=None) -> dict:
    """The owner's before/after (module doc), with plain lines."""
    weeks = weeks_for(restaurant_id, today=today, db_path=db_path)
    comp = compare(weeks)
    lines = []
    for m, e in comp.items():
        if not e["stated"]:
            continue
        word = {"better": "better", "worse": "worse"}.get(e["verdict"], "no clear difference yet")
        lines.append(f"{e['label']}: {_fmt(m, e['cavnar']['mean'])} in the {e['cavnar']['n']} weeks Cavnar AI drafted, "
                     f"{_fmt(m, e['manager']['mean'])} in the {e['manager']['n']} weeks built without it — {word} "
                     f"against the same time of year ({_diff(m, e['difference'])}, 90% range {_diff(m, e['lo'])} to "
                     f"{_diff(m, e['hi'])}, over {e['matched']['cavnar']} matched weeks).")
    n_c = sum(1 for w in weeks if w["class"] == "cavnar")
    n_m = sum(1 for w in weeks if w["class"] == "manager")
    return {"ok": True, "available": any(e["stated"] for e in comp.values()), "weeks": {"cavnar": n_c, "manager": n_m},
            "measures": comp, "lines": lines,
            "caveat": ("Measured on what each week actually did, each Cavnar AI week against the manager's weeks "
                       "from the same time of year. They are still different weeks — the demand and the team "
                       "change — so a difference is what went with Cavnar AI's weeks, not a controlled test. "
                       "Nothing here is a projection or an opportunity."),
            "floor": (f"Each comparison needs {MIN_WEEKS} Cavnar AI weeks with manager weeks from the same time of "
                      f"year (within {SEASON_BAND_WEEKS} weeks), and {MIN_WEEKS} such manager weeks.")}


def platform_view(today=None, db_path=None, restaurant_ids=None) -> dict:
    """The admin readout and the published figure (module doc)."""
    import models
    from intelligence import privacy
    try:
        from intelligence.jobs import excluded_learning_ids
        excluded = set(excluded_learning_ids(db_path=db_path) if db_path else excluded_learning_ids())
    except Exception as e:
        log.warning("benchmark: eligibility unreadable: %s", e)
        return {"ok": False, "error": "Which restaurants may teach could not be read."}
    if restaurant_ids is None:
        rs = models.get_all_restaurants(db_path) if db_path else models.get_all_restaurants()
        restaurant_ids = [r.id for r in rs if getattr(r, "module_labor", 0)]
    ids = [r for r in restaurant_ids if r not in excluded]
    per = {}
    for rid in ids:
        try:
            v = restaurant_view(rid, today=today, db_path=db_path)
        except Exception as e:
            log.warning("benchmark rid=%s failed: %s", rid, e)
            continue
        per[rid] = v
    try:
        orgs = privacy.org_map(db_path)
    except Exception:
        orgs = {}
    measures = {}
    for m, spec in MEASURES.items():
        rows = [(rid, v["measures"][m]["difference"]) for rid, v in per.items() if v["measures"][m]["stated"]]
        import schedule_experiments as sx
        ci = sx.cluster_mean_ci(rows) if rows else {"n": 0, "mean": None, "ci90": None, "restaurants": 0}
        members = [orgs.get(rid, f"r{rid}") for rid, _v in rows]
        n_orgs, share = privacy.org_counts(members)
        ok = privacy.cohort_ok(len(rows)) and privacy.orgs_ok(n_orgs, share)
        e = {"label": spec["label"], "better": spec["better"], "restaurants": len(rows), "organisations": n_orgs,
             "mean": ci.get("mean"), "ci90": ci.get("ci90"), "published": None,
             "floor": (f"{len(rows)} of {privacy.MIN_COHORT} restaurants, {n_orgs} of {privacy.MIN_ORGS} organisations"
                       if not ok else None)}
        if ok and ci.get("ci90"):
            lo, hi = ci["ci90"]
            clear = (hi < 0) if spec["better"] == "lower" else (lo > 0)
            claim = None
            if clear:
                claim = (f"Across {privacy.MIN_COHORT}+ restaurants, weeks Cavnar AI drafted ran "
                         f"{abs(ci['mean']) * (100 if spec['unit'] == 'share' else 1):.1f}"
                         f"{UNIT_WORD[spec['unit']]} "
                         f"{'lower' if ci['mean'] < 0 else 'higher'} on {spec['label'].lower()} than the weeks "
                         "their managers built.")
            pub = {"measure": spec["label"], "difference": privacy.round_effect(ci["mean"]),
                   "ci90": [privacy.round_effect(lo), privacy.round_effect(hi)], "cohort": "5+ restaurants",
                   "claim": claim}
            e["published"] = privacy.assert_anonymous(pub)
        measures[m] = e
    return {"ok": True, "restaurants": len(per), "measures": measures,
            "per_restaurant": [{"restaurant_id": rid, "weeks": v["weeks"],
                                "differences": {m: {k: v["measures"][m][k] for k in ("difference", "lo", "hi", "verdict")}
                                                for m in MEASURES if v["measures"][m]["stated"]}} for rid, v in per.items()],
            "method": ("Per restaurant: the season-matched difference (each Cavnar AI week minus the manager weeks "
                       "of the same time of year) with a 90% interval widened for serially correlated weeks, Holm "
                       "over the five measures; across restaurants: the restaurant-cluster mean "
                       "of those differences and its 90% interval. Published only above the privacy floor "
                       f"({privacy.MIN_COHORT} restaurants, {privacy.MIN_ORGS} organisations, none over a third) and "
                       "claimed only when the interval excludes no difference.")}
