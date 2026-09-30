"""Level 1: what this restaurant's own history says about it.

Everything here is `WHERE restaurant_id = ?`. It serves the restaurant it
reads and nobody else: Ask's context, Home's recommendation confidence and
the owner's own account page.
"""
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH
import canonical_facts as _cf
from . import features as _features, feedback, scoring
from .stats import slope, mean


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def busiest_days(restaurant_id, days=84, db_path=DB_PATH) -> dict:
    """Sales share by weekday over the last 12 weeks, from the restaurant's
    own daily history. None when fewer than 4 weeks are on file. The window
    ends on the restaurant's own day, not the server's (memory audit
    9/29/26, "outside": date.today() is UTC on Railway)."""
    from time_utils import restaurant_now_by_id
    floor = (restaurant_now_by_id(restaurant_id).date() - timedelta(days=days)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT date, sales, labor_pct FROM labor_daily_history WHERE restaurant_id=? AND date >= ? "
                            f"AND sales > 0 AND {_cf.FINAL_SQL}", (restaurant_id, floor)).fetchall()
    finally:
        conn.close()
    if len(rows) < 28:
        return {"available": False, "reason": "fewer than four weeks of daily sales on file"}
    by = {d: [] for d in _DAYS}
    lab = {d: [] for d in _DAYS}
    for r in rows:
        try:
            wd = _DAYS[date.fromisoformat(str(r["date"])[:10]).weekday()]
        except ValueError:
            continue
        by[wd].append(float(r["sales"]))
        if r["labor_pct"] is not None:
            lab[wd].append(float(r["labor_pct"]))
    avg = {d: mean(v) for d, v in by.items() if v}
    total = sum(avg.values()) or 1
    share = {d: round(v / total, 3) for d, v in avg.items()}
    ranked = sorted(share.items(), key=lambda kv: kv[1], reverse=True)
    return {"available": True, "share_by_day": share, "busiest": [d for d, _ in ranked[:2]],
            "quietest": [d for d, _ in ranked[-2:]],
            "labor_pct_by_day": {d: round(mean(v), 1) for d, v in lab.items() if v}}


def seasonality(restaurant_id, db_path=DB_PATH) -> dict:
    """Month index of sales against the restaurant's own annual mean. Needs
    twelve distinct months, otherwise says so.

    Reads the ONE sales history in the nightly report's baseline order
    (canonical_facts.sales_history: the night's report, the owner's imported
    DSR workbooks, the POS sync's final nights — memory audit 9/29/26,
    imported_year). It read the POS archive alone, so an owner who imported
    a year of DSR workbooks but had 60 days of POS history got "2 full months
    on file" with last year sitting in the database. Each month is read on
    one basis (the report's own net where the month has enough of it), and
    `sources` names what the index rests on."""
    series = _cf.sales_history(restaurant_id, "2000-01-01", (date.today() + timedelta(days=1)).isoformat(),
                               db_path=db_path)
    by_month = {}
    for d, x in series.items():
        by_month.setdefault(d[:7], {}).setdefault(x.get("basis") or _cf.BASIS_DSR, []).append(float(x["net"]))
    months = {}
    for ym, bases in by_month.items():
        dsr_days = bases.get(_cf.BASIS_DSR) or []
        vals = dsr_days if len(dsr_days) >= 10 else max(bases.values(), key=len)
        if len(vals) >= 10:
            months[ym] = sum(vals) / len(vals)
    if len(months) < 12:
        return {"available": False, "sources": _cf.sources_said(series),
                "reason": f"{len(months)} full months on file; twelve are needed for a seasonal read"}
    by_m = {}
    for ym, per_day in months.items():
        by_m.setdefault(int(ym[5:7]), []).append(per_day)
    idx = {m: mean(v) for m, v in by_m.items()}
    base = mean(idx.values())
    index = {_MONTHS[m - 1]: round(v / base, 2) for m, v in sorted(idx.items())}
    ranked = sorted(index.items(), key=lambda kv: kv[1], reverse=True)
    return {"available": True, "index": index, "peak": [m for m, _ in ranked[:2]], "trough": [m for m, _ in ranked[-2:]],
            "sources": _cf.sources_said(series)}


def own_record(restaurant_id, db_path=DB_PATH) -> dict:
    """What this restaurant did with each kind of recommendation, and what
    was measured after: {kind: stats}. Level 1.

    "worked" follows rec_learning.most_effective's floors (CA2 finding 9,
    CA1 A10/E4): a kind is named only with MIN_MEASURED_FOR_RATE clear
    results, success at least even, ranked by the lower end of its 90%
    Wilson interval — one improvement beside four that got worse used to
    read "measurably improved things here". `worked_detail` carries each
    one's "k of n", and a kind's `success_rate` is None below the floor
    (its counts stay), so nothing downstream can quote 100% from one.

    One window rule (memory audit 9/29/26, PLATFORM-11): the same horizon
    the rankers and Historical Accuracy read (rec_learning.
    DECAY_HORIZON_DAYS — Ask counted all time while they read a year), and
    "worked" is ranked on the results weighed by their age (rec_learning.
    decay_weight); the "k of n" quoted stays the plain counts."""
    import rec_learning
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    since = (now - timedelta(days=rec_learning.DECAY_HORIZON_DAYS)).strftime("%Y-%m-%d")
    conn = get_conn(db_path)
    try:
        kinds = [r["rec_kind"] for r in conn.execute("SELECT DISTINCT rec_kind FROM intel_rec_events WHERE restaurant_id=? "
                                                       "AND event_at >= ?", (restaurant_id, since)).fetchall()]
        results = conn.execute("SELECT rec_kind, source_key, outcome, event_at FROM intel_rec_events WHERE restaurant_id=? "
                               "AND action='measured' AND outcome IN ('improved','worsened','no_clear_change') "
                               "AND event_at >= ?", (restaurant_id, since)).fetchall()
    finally:
        conn.close()
    weighted = {}
    for r in results:
        w = rec_learning.decay_weight(r["rec_kind"], r["event_at"], now, key=r["source_key"])
        n, k = weighted.get(r["rec_kind"], (0.0, 0.0))
        weighted[r["rec_kind"]] = (n + w, k + (w if r["outcome"] == "improved" else 0.0))
    out = {}
    for k in kinds:
        s = dict(scoring.kind_stats(k, restaurant_id=restaurant_id, db_path=db_path,
                                    window_days=rec_learning.DECAY_HORIZON_DAYS))
        if (s.get("measured") or 0) < rec_learning.MIN_MEASURED_FOR_RATE:
            s["success_rate"] = None
        out[k] = s
    ranked = []
    for k, s in out.items():
        n, imp = int(s.get("measured") or 0), int(s.get("improved") or 0)
        if n < rec_learning.MIN_MEASURED_FOR_RATE or imp / n < 0.5:
            continue
        nw, kw = weighted.get(k, (float(n), float(imp)))
        lo, _ = rec_learning.wilson(kw, nw) if nw else (None, None)
        if lo is None:
            continue
        ranked.append((lo, n, k, imp))
    ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
    worked_detail = [{"kind": k, "improved": imp, "measured": n} for _lo, n, k, imp in ranked[:5]]
    # What this owner has plainly declined, by SUBJECT (memory audit
    # 9/29/26, "one_hide"): declined + hidden > accepted over all history,
    # with no floor, put a whole kind on Ask's do-not-propose list after one
    # two-week hide. Now only "not for us", at least three in 180 days,
    # recency-weighted (decisions.declined_subjects).
    try:
        import decisions
        declined = decisions.declined_subjects(restaurant_id, db_path=db_path)
    except Exception as e:
        print(f"[intelligence.memory] declined subjects unavailable: {e}")
        declined = []
    return {"by_kind": out, "worked": [w["kind"] for w in worked_detail], "worked_detail": worked_detail,
            "ignored": [d["kind"] for d in declined[:5]], "declined_detail": declined[:5],
            "min_measured": rec_learning.MIN_MEASURED_FOR_RATE}


# A slope is worth a line when the series moved, over the weeks read, by
# more than the metric's own stated noise band (metrics._REGISTRY; CA1 E4):
# a flat 0.05 a week was 0.6★ of rating over twelve weeks (never said) and
# 0.6 of a labor point (said about noise). Units per week = band / span.
SLOPE_BANDS = {"avg_rating_30d": 0.1, "labor_pct_28d": 0.5, "food_cost_pct_28d": 1.0,
               "waste_sales_pct_28d": 0.25, "response_24h_rate_30d": 0.05}
SLOPE_UNITS = {"avg_rating_30d": "★", "labor_pct_28d": " pts", "food_cost_pct_28d": " pts",
               "waste_sales_pct_28d": " pts", "response_24h_rate_30d": ""}


def slope_threshold(key, weeks) -> float:
    """The smallest per-week slope of `key` worth saying over `weeks`."""
    return SLOPE_BANDS.get(key, 0.05) / max(1, int(weeks or 1) - 1)


def metric_slopes(restaurant_id, weeks=12, db_path=DB_PATH) -> dict:
    """Per-week slope of this restaurant's own feature series."""
    rows = _features.series(restaurant_id, weeks=weeks, db_path=db_path)
    if len(rows) < 4:
        return {}
    out = {}
    for key in ("avg_rating_30d", "labor_pct_28d", "food_cost_pct_28d", "waste_sales_pct_28d", "response_24h_rate_30d"):
        ys = [r["features"].get(key) for r in rows]
        if sum(1 for y in ys if y is not None) >= 4:
            out[key] = {"slope_per_week": round(slope(ys), 4), "latest": ys[-1], "weeks": len(rows)}
    return out


def restaurant_memory(restaurant_id, db_path=DB_PATH) -> dict:
    latest = _features.latest(restaurant_id, db_path=db_path)
    return {
        "restaurant_id": restaurant_id,
        "features": latest,
        "busiest_days": busiest_days(restaurant_id, db_path=db_path),
        "seasonality": seasonality(restaurant_id, db_path=db_path),
        "record": own_record(restaurant_id, db_path=db_path),
        "slopes": metric_slopes(restaurant_id, db_path=db_path),
        "recent_events": feedback.history(restaurant_id, limit=20, db_path=db_path),
    }


def lines(mem: dict, record=True) -> list:
    """Short, dated, own-data-only lines for a prompt. `record=False` leaves
    out the advice record (what worked, what was declined): Ask reads that
    from memory_context's "what_worked" section instead, gated by the
    viewer's modules, loss view and owner-only rules — this copy named food
    kinds and declined subjects to every login (memory re-audit PEOPLE-11 /
    QUALITY-16). own_record stays the engine's."""
    out = []
    bd = mem.get("busiest_days") or {}
    if bd.get("available"):
        out.append(f"Busiest days by sales: {', '.join(bd['busiest'])}; quietest: {', '.join(bd['quietest'])}.")
    se = mem.get("seasonality") or {}
    if se.get("available"):
        src = f"; from {se['sources']}" if se.get("sources") else ""
        out.append(f"Seasonal peak months: {', '.join(se['peak'])}; trough: {', '.join(se['trough'])} "
                   f"(index vs own annual mean{src}).")
    rec = (mem.get("record") or {}) if record else {}
    detail = rec.get("worked_detail")
    if detail:
        out.append("Recommendation kinds most often followed by a measured improvement here (before and after, "
                   "not proven cause): "
                   + ", ".join(f"{w['kind']} ({w['improved']} of {w['measured']} measured results improved)"
                               for w in detail) + ".")
    elif rec.get("worked"):
        # A record built before worked_detail: names only, no rate claimed.
        out.append("Recommendation kinds most often followed by a measured improvement here: "
                   + ", ".join(rec["worked"]) + ".")
    if rec.get("declined_detail"):
        out.append("Advice this owner has said 'not for us' to at least 3 times in 180 days — do not re-propose "
                   "these subjects without new evidence: "
                   + "; ".join(f"{d['label']} ({', '.join(d['subjects']) or 'any subject'}; {d['n']} times since "
                               f"{d['since']})" for d in rec["declined_detail"]) + ".")
    elif rec.get("ignored"):
        # A record built before declined_detail: the kinds, no subjects.
        out.append("Kinds this owner has said 'not for us' to repeatedly: " + ", ".join(rec["ignored"])
                   + " — do not re-propose without new evidence.")
    for k, s in (mem.get("slopes") or {}).items():
        if s["slope_per_week"] and abs(s["slope_per_week"]) >= slope_threshold(k, s.get("weeks")):
            unit = SLOPE_UNITS.get(k, "")
            out.append(f"{k} is moving {'up' if s['slope_per_week'] > 0 else 'down'} about "
                       f"{abs(s['slope_per_week']):.2f}{unit} per week over {s['weeks']} weeks.")
    return out
