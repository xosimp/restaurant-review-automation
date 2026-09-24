"""Level 1: what this restaurant's own history says about it.

Everything here is `WHERE restaurant_id = ?`. It serves the restaurant it
reads and nobody else: Ask's context, Home's recommendation confidence and
the owner's own account page.
"""
from datetime import date, timedelta

import models as _models_mod
from models import DB_PATH
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
    own daily history. None when fewer than 4 weeks are on file."""
    floor = (date.today() - timedelta(days=days)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT date, sales, labor_pct FROM labor_daily_history WHERE restaurant_id=? AND date >= ? AND sales > 0",
                            (restaurant_id, floor)).fetchall()
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
    twelve distinct months, otherwise says so."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT substr(date,1,7) AS ym, SUM(sales) AS s, COUNT(*) AS n FROM labor_daily_history "
                            "WHERE restaurant_id=? AND sales > 0 GROUP BY ym ORDER BY ym", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    months = {r["ym"]: float(r["s"]) / max(1, int(r["n"])) for r in rows if int(r["n"]) >= 10}
    if len(months) < 12:
        return {"available": False, "reason": f"{len(months)} full months on file; twelve are needed for a seasonal read"}
    by_m = {}
    for ym, per_day in months.items():
        by_m.setdefault(int(ym[5:7]), []).append(per_day)
    idx = {m: mean(v) for m, v in by_m.items()}
    base = mean(idx.values())
    index = {_MONTHS[m - 1]: round(v / base, 2) for m, v in sorted(idx.items())}
    ranked = sorted(index.items(), key=lambda kv: kv[1], reverse=True)
    return {"available": True, "index": index, "peak": [m for m, _ in ranked[:2]], "trough": [m for m, _ in ranked[-2:]]}


def own_record(restaurant_id, db_path=DB_PATH) -> dict:
    """What this restaurant did with each kind of recommendation, and what
    was measured after: {kind: stats}. Level 1.

    "worked" follows rec_learning.most_effective's floors (CA2 finding 9,
    CA1 A10/E4): a kind is named only with MIN_MEASURED_FOR_RATE clear
    results, success at least even, ranked by the lower end of its 90%
    Wilson interval — one improvement beside four that got worse used to
    read "measurably improved things here". `worked_detail` carries each
    one's "k of n", and a kind's `success_rate` is None below the floor
    (its counts stay), so nothing downstream can quote 100% from one."""
    import rec_learning
    conn = get_conn(db_path)
    try:
        kinds = [r["rec_kind"] for r in conn.execute("SELECT DISTINCT rec_kind FROM intel_rec_events WHERE restaurant_id=?",
                                                       (restaurant_id,)).fetchall()]
    finally:
        conn.close()
    out = {}
    for k in kinds:
        s = dict(scoring.kind_stats(k, restaurant_id=restaurant_id, db_path=db_path))
        if (s.get("measured") or 0) < rec_learning.MIN_MEASURED_FOR_RATE:
            s["success_rate"] = None
        out[k] = s
    ranked = []
    for k, s in out.items():
        n, imp = int(s.get("measured") or 0), int(s.get("improved") or 0)
        if n < rec_learning.MIN_MEASURED_FOR_RATE or imp / n < 0.5:
            continue
        lo, _ = rec_learning.wilson(imp, n)
        ranked.append((lo, n, k, imp))
    ranked.sort(key=lambda x: (-x[0], -x[1], x[2]))
    worked_detail = [{"kind": k, "improved": imp, "measured": n} for _lo, n, k, imp in ranked[:5]]
    ignored = sorted([k for k, s in out.items() if (s["declined"] or 0) + (s["hidden"] or 0) > (s["accepted"] or 0)],
                     key=lambda k: -((out[k]["declined"] or 0) + (out[k]["hidden"] or 0)))
    return {"by_kind": out, "worked": [w["kind"] for w in worked_detail], "worked_detail": worked_detail,
            "ignored": ignored[:5], "min_measured": rec_learning.MIN_MEASURED_FOR_RATE}


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


def lines(mem: dict) -> list:
    """Short, dated, own-data-only lines for a prompt."""
    out = []
    bd = mem.get("busiest_days") or {}
    if bd.get("available"):
        out.append(f"Busiest days by sales: {', '.join(bd['busiest'])}; quietest: {', '.join(bd['quietest'])}.")
    se = mem.get("seasonality") or {}
    if se.get("available"):
        out.append(f"Seasonal peak months: {', '.join(se['peak'])}; trough: {', '.join(se['trough'])} (index vs own annual mean).")
    rec = mem.get("record") or {}
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
    if rec.get("ignored"):
        out.append("Kinds this owner has declined or hidden more than acted on: " + ", ".join(rec["ignored"]) + " — do not re-propose without new evidence.")
    for k, s in (mem.get("slopes") or {}).items():
        if s["slope_per_week"] and abs(s["slope_per_week"]) >= slope_threshold(k, s.get("weeks")):
            unit = SLOPE_UNITS.get(k, "")
            out.append(f"{k} is moving {'up' if s['slope_per_week'] > 0 else 'down'} about "
                       f"{abs(s['slope_per_week']):.2f}{unit} per week over {s['weeks']} weeks.")
    return out
