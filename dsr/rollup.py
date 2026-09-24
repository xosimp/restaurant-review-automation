"""
dsr.rollup — a week and a fiscal period, from the nightly reports.

The shape of Erik's weekly DSR sheet: his week (Wed → Tue at Simple EJ's),
labelled "Period 9 · Week 4"; one row per day with the six categories, gross
and net, the budget and last year beside them, and the day's weather, event
and "Influence/Result" note; a totals row; period to date.

Every figure is read, never computed from a guess:

  categories, gross, net, labor   dsr_metrics (the night's report)
  budget                          dsr_budgets (the owner enters it)
  last year                       the same weekday 52 weeks back: that
                                  night's DSR, else an imported row from
                                  his old workbooks (dsr_history_import),
                                  else the nightly POS sync's daily sales
  weather / event / influence     the night's report (intel, close-out)

A day with no report is in the week with its budget and last year and every
measured figure None — shown as a dash, never a zero. A total sums only the
days that were measured and says how many (`days_measured`, and `<col>_days`
per column: gross can be measured on fewer nights than net); a comparison is
made only between figures that were both measured on the same days. Each
night carries the gross basis it was built under, and a total says which
bases it added (`gross_bases`, `gross_mixed`).

Budget columns are the owner's: access.redact_grid drops them for a
manager, the same rule as the nightly report.
"""
import json
from datetime import date, timedelta

import dsr as _dsr
from dsr import fiscal, store

LAST_YEAR_DAYS = 364                 # same weekday, 52 weeks back
_NUMERIC = ("gross", "net", "budget_gross", "budget_net", "last_year_net", "labor_cost")


def _d(day):
    return day if isinstance(day, date) else date.fromisoformat(str(day)[:10])


def _pct_change(value, base):
    if value is None or base in (None, 0):
        return None
    return round((value - base) / abs(base) * 100, 1)


def _diff(value, base):
    return None if value is None or base is None else round(value - base, 2)


def period_bounds(restaurant, day):
    """(first, last) day of the fiscal period holding `day`, or None when
    the restaurant has no fiscal year start (then there are only weeks).
    The period's length is its own year's: a listed 53-week year's long
    period is five weeks even when the default scheme's is four."""
    return fiscal.period_span(restaurant, _d(day))


def _metrics(rid, start, end, db_path):
    """{date: {metric: value}} — measured values only."""
    conn = store.get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
                            "AND business_date BETWEEN ? AND ? AND value IS NOT NULL",
                            (rid, str(start), str(end))).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["business_date"], {})[r["metric"]] = float(r["value"])
    return out


def _reports(rid, start, end, db_path):
    """{date: the latest version's row} for the range."""
    conn = store.get_conn(db_path)
    try:
        rows = conn.execute("SELECT r.business_date, r.version, r.status, r.provisional, r.facts_json FROM dsr_reports r "
                            "JOIN (SELECT business_date, MAX(version) v FROM dsr_reports WHERE restaurant_id=? "
                            "AND business_date BETWEEN ? AND ? GROUP BY business_date) m "
                            "ON m.business_date=r.business_date AND m.v=r.version WHERE r.restaurant_id=?",
                            (rid, str(start), str(end), rid)).fetchall()
    finally:
        conn.close()
    return {r["business_date"]: r for r in rows}


def _pos_sync_sales(rid, days, db_path):
    if not days:
        return {}
    conn = store.get_conn(db_path)
    try:
        marks = ",".join("?" for _ in days)
        rows = conn.execute(f"SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date IN ({marks}) "
                            "AND sales IS NOT NULL AND sales > 0", (rid, *days)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    return {r["date"]: float(r["sales"]) for r in rows}


def _last_year(rid, days, db_path):
    """{day_iso: (net, source)} for the same weekday 52 weeks before each day."""
    ly = {d: (_d(d) - timedelta(days=LAST_YEAR_DAYS)).isoformat() for d in days}
    lo, hi = min(ly.values()), max(ly.values())
    measured = _metrics(rid, lo, hi, db_path)
    imported = store.history_for(rid, lo, hi, db_path=db_path)
    synced = _pos_sync_sales(rid, list(ly.values()), db_path)
    out = {}
    for d, then in ly.items():
        if (measured.get(then) or {}).get("sales.net") is not None:
            out[d] = (measured[then]["sales.net"], "dsr")
        elif (imported.get(then) or {}).get("net") is not None:
            out[d] = (imported[then]["net"], "import")
        elif then in synced:
            out[d] = (synced[then], "pos_sync")
        else:
            out[d] = (None, None)
    return out


def _notes(facts):
    """(weather, event, influence) text from a night's facts."""
    blocks = (facts or {}).get("blocks") or {}
    intel = ((blocks.get("intel") or {}).get("detail") or {})
    weather = ((intel.get("weather") or {}).get("summary")) or None
    events = (intel.get("events") or {}).get("summary")
    event = events if events and events != "Nothing listed" else None
    fields = ((blocks.get("closeout") or {}).get("detail") or {}).get("fields") or {}
    return weather, event, fields.get("influence") or None


def _gross_basis(facts):
    """What that night's report called gross ("items" | "all"): the basis it
    was built under (dsr.block_sales.GROSS_BASES), read from the night itself
    because the owner can change the setting between nights. A report from
    before the setting existed was items only."""
    sales = ((facts or {}).get("blocks") or {}).get("sales") or {}
    basis = ((sales.get("detail") or {}).get("definition") or {}).get("gross_basis")
    return basis if isinstance(basis, str) and basis else "items"


def _categories(rows):
    seen = []
    for r in rows:
        for c in r["cats"]:
            if c not in seen:
                seen.append(c)
    order = [c for c in _dsr.DEFAULT_CATEGORIES]
    extra = sorted(c for c in seen if c not in order and c != _dsr.UNMAPPED)
    return order + extra + ([_dsr.UNMAPPED] if _dsr.UNMAPPED in seen else [])


def _totals(rows, cats):
    def total(key, pick=lambda r, k: r.get(k)):
        vals = [pick(r, key) for r in rows if pick(r, key) is not None]
        return (round(sum(vals), 2) if vals else None), len(vals)

    t = {}
    for k in _NUMERIC:
        t[k], t[f"{k}_days"] = total(k)
    t["cats"] = {c: total(c, lambda r, k: r["cats"].get(k))[0] for c in cats}
    t["days_measured"] = sum(1 for r in rows if r["net"] is not None)
    # Gross is summed over gross_days, which can be fewer than the nights
    # measured (an "everything rung" night whose tax or voids the POS didn't
    # report has a net and no gross); and a range the owner switched bases
    # inside adds two different kinds of gross - said, never hidden.
    t["gross_bases"] = sorted({r.get("gross_basis") or "items" for r in rows if r["gross"] is not None})
    t["gross_mixed"] = len(t["gross_bases"]) > 1
    # Comparisons only over the days that have both sides.
    both_b = [r for r in rows if r["net"] is not None and r["budget_net"] is not None]
    both_ly = [r for r in rows if r["net"] is not None and r["last_year_net"] is not None]
    both_labor = [r for r in rows if r["net"] and r["labor_cost"] is not None]
    nb, bb = sum(r["net"] for r in both_b), sum(r["budget_net"] for r in both_b)
    nl, ll = sum(r["net"] for r in both_ly), sum(r["last_year_net"] for r in both_ly)
    t["vs_budget_net"] = round(nb - bb, 2) if both_b else None
    t["vs_budget_net_pct"] = _pct_change(nb, bb) if both_b else None
    t["vs_last_year_net"] = round(nl - ll, 2) if both_ly else None
    t["vs_last_year_net_pct"] = _pct_change(nl, ll) if both_ly else None
    t["labor_pct"] = (round(sum(r["labor_cost"] for r in both_labor) / sum(r["net"] for r in both_labor) * 100, 1)
                      if both_labor else None)
    # A total row compares the SAME days on both sides: vs_X == net - X
    # (NS3 H6, R11). Budget and last year used to sum all seven days while
    # net summed the three measured, so mid-week read "Net $30,000, Budget
    # $66,500, vs Budget +$1,500" under a footnote saying totals cover only
    # measured days. Now the X total covers the measured days; where a
    # measured night has no X, the total and its comparison are withheld
    # rather than set beside a net they do not match. The whole range's
    # figure stays, named for what it is (X_full_range).
    measured = [r for r in rows if r["net"] is not None]
    for key, both, vs in (("budget_net", both_b, "vs_budget_net"), ("last_year_net", both_ly, "vs_last_year_net")):
        t[f"{key}_full_range"], t[f"{key}_full_range_days"] = t[key], t[f"{key}_days"]
        if both and len(both) == len(measured):
            t[key], t[f"{key}_days"] = round(sum(r[key] for r in both), 2), len(both)
        else:
            if both:
                t[f"{key}_note"] = (f"{len(measured) - len(both)} measured night(s) have no "
                                    f"{'budget' if key.startswith('budget') else 'last-year figure'}, "
                                    f"so the total and its comparison are not shown")
            t[key], t[f"{key}_days"] = None, 0
            t[vs], t[f"{vs}_pct"] = None, None
    bg = [r for r in measured if r.get("budget_gross") is not None]
    t["budget_gross_full_range"], t["budget_gross_full_range_days"] = t["budget_gross"], t["budget_gross_days"]
    t["budget_gross"] = round(sum(r["budget_gross"] for r in bg), 2) if bg and len(bg) == len(measured) else None
    t["budget_gross_days"] = len(bg) if t["budget_gross"] is not None else 0
    return t


def _rows(restaurant, start, end, db_path):
    rid = restaurant.id
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    metrics = _metrics(rid, start, end, db_path)
    reports = _reports(rid, start, end, db_path)
    budgets = store.budgets_for(rid, start, end, db_path=db_path)
    last_year = _last_year(rid, days, db_path)
    from time_utils import mdy
    rows = []
    for d in days:
        m = metrics.get(d) or {}
        rep = reports.get(d)
        facts = None
        if rep is not None and rep["facts_json"]:
            try:
                facts = json.loads(rep["facts_json"])
            except ValueError:
                facts = None
        weather, event, influence = _notes(facts)
        b = budgets.get(d) or {}
        ly, ly_src = last_year[d]
        net = m.get("sales.net")
        row = {
            "date": d, "weekday": _d(d).strftime("%a"), "label": f"{_d(d).strftime('%a')} {mdy(d)}",
            "status": rep["status"] if rep is not None else None,
            "provisional": bool(rep["provisional"]) if rep is not None else False,
            "version": rep["version"] if rep is not None else None,
            "cats": {k[len("sales.cat:"):]: v for k, v in m.items() if k.startswith("sales.cat:")},
            "gross": m.get("sales.gross"), "net": net,
            "gross_basis": _gross_basis(facts) if rep is not None else None,
            "transactions": m.get("sales.transactions"), "guests": m.get("sales.guests"),
            "budget_gross": b.get("gross"), "budget_net": b.get("net"),
            "last_year_net": ly, "last_year_source": ly_src,
            "labor_cost": m.get("labor.cost"), "labor_pct": m.get("labor.pct"),
            "weather": weather, "event": event, "influence": influence,
        }
        row["vs_budget_net"] = _diff(net, row["budget_net"])
        row["vs_budget_net_pct"] = _pct_change(net, row["budget_net"])
        row["vs_last_year_net"] = _diff(net, ly)
        row["vs_last_year_net_pct"] = _pct_change(net, ly)
        rows.append(row)
    return rows


def week(restaurant, day, db_path=None):
    """The restaurant's week holding `day`, with period to date."""
    import models
    db_path = db_path or models.DB_PATH
    start, end = fiscal.week_bounds(restaurant, day)
    rows = _rows(restaurant, start, end, db_path)
    cats = _categories(rows)
    out = {"kind": "week", "start": start.isoformat(), "end": end.isoformat(),
           "label": fiscal.label(restaurant, start), "fiscal": fiscal.position(restaurant, start),
           "categories": cats, "days": rows, "totals": _totals(rows, cats), "period_to_date": None}
    pb = period_bounds(restaurant, start)
    if pb:
        ptd = _rows(restaurant, pb[0], end, db_path)
        out["period_to_date"] = dict(_totals(ptd, cats), start=pb[0].isoformat(), end=end.isoformat())
    return out


def period(restaurant, day, db_path=None):
    """The fiscal period holding `day`, one row per week; None when the
    restaurant has no fiscal year start."""
    import models
    db_path = db_path or models.DB_PATH
    pb = period_bounds(restaurant, day)
    if pb is None:
        return None
    start, end = pb
    rows = _rows(restaurant, start, end, db_path)
    cats = _categories(rows)
    weeks = []
    for i in range(0, len(rows), 7):
        chunk = rows[i:i + 7]
        weeks.append({"start": chunk[0]["date"], "end": chunk[-1]["date"],
                      "label": fiscal.label(restaurant, chunk[0]["date"]), "totals": _totals(chunk, cats)})
    pos = fiscal.position(restaurant, start)
    return {"kind": "period", "start": start.isoformat(), "end": end.isoformat(),
            "label": f"Period {pos['period']}", "fiscal": pos, "categories": cats,
            "weeks": weeks, "totals": _totals(rows, cats)}
