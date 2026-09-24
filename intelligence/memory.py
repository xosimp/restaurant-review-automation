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
    was measured after: {kind: stats}. Level 1 — no floor applies."""
    conn = get_conn(db_path)
    try:
        kinds = [r["rec_kind"] for r in conn.execute("SELECT DISTINCT rec_kind FROM intel_rec_events WHERE restaurant_id=?",
                                                       (restaurant_id,)).fetchall()]
    finally:
        conn.close()
    out = {}
    for k in kinds:
        out[k] = scoring.kind_stats(k, restaurant_id=restaurant_id, db_path=db_path)
    worked = sorted([k for k, s in out.items() if (s["improved"] or 0) > 0], key=lambda k: -(out[k]["success_rate"] or 0))
    ignored = sorted([k for k, s in out.items() if (s["declined"] or 0) + (s["hidden"] or 0) > (s["accepted"] or 0)],
                     key=lambda k: -((out[k]["declined"] or 0) + (out[k]["hidden"] or 0)))
    return {"by_kind": out, "worked": worked[:5], "ignored": ignored[:5]}


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
    if rec.get("worked"):
        out.append("Recommendation kinds that measurably improved things here: " + ", ".join(rec["worked"]) + ".")
    if rec.get("ignored"):
        out.append("Kinds this owner has declined or hidden more than acted on: " + ", ".join(rec["ignored"]) + " — do not re-propose without new evidence.")
    for k, s in (mem.get("slopes") or {}).items():
        if s["slope_per_week"] and abs(s["slope_per_week"]) >= 0.05:
            out.append(f"{k} is moving {'up' if s['slope_per_week'] > 0 else 'down'} about {abs(s['slope_per_week']):.2f} per week over {s['weeks']} weeks.")
    return out
