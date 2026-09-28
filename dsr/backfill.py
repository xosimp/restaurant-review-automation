"""
dsr.backfill — past nights' figures for the days before the Daily Sales
Report started, straight from the POS (owner, 9/28/26).

The report starts from the night it is switched on, so the week and period
grids had one measured day while the restaurant's POS held a month (Simple
EJ's: RPOWER from 8/26, the first report 9/27). This fills dsr_metrics for
each business day the archive shows trading (labor_daily_history sales > 0)
that has neither a report nor figures of its own:

  sales.gross / net / transactions / guests / discounts / comps / voids /
  refunds / tax   the POS's own day read (pos.fetch_day_sales), with the
                  restaurant's gross basis — the Sales block's exact math
  sales.dep:<department>   each department's net; the grids categorise them
                  under the map as it stands when shown (rollup)
  sales.cat:<category>     today's categories, for readers of a single day
  labor.cost / pct / hours from the archive, only where the night's labor
                  is costed (the Labor block's rule: POS wages or the
                  owner's own rates — never the assumed $26/hr)

No report is written, nobody is emailed, no model is called: a filled day is
figures, not a night's report (source "<provider>_backfill", report_id
NULL). A report for that night later replaces them (store.sync_metrics).
Bounded per run (MAX_DAYS_PER_RUN POS reads) and resumable: what is missing
is re-derived every run.
"""
import logging
from datetime import date, timedelta
from types import SimpleNamespace

import dsr
from dsr import store

log = logging.getLogger("dsr")

MAX_DAYS_PER_RUN = 45
SOURCE_SUFFIX = "_backfill"


def _missing_days(rid, start, end, db_path):
    conn = store.get_conn(db_path)
    try:
        traded = [str(r[0])[:10] for r in conn.execute(
            "SELECT date FROM labor_daily_history WHERE restaurant_id=? AND date BETWEEN ? AND ? AND sales > 0 "
            "ORDER BY date", (rid, start.isoformat(), end.isoformat())).fetchall()]
        have = {str(r[0])[:10] for r in conn.execute(
            "SELECT DISTINCT business_date FROM dsr_metrics WHERE restaurant_id=? AND metric='sales.net' "
            "AND business_date BETWEEN ? AND ?", (rid, start.isoformat(), end.isoformat())).fetchall()}
        reported = {str(r[0])[:10] for r in conn.execute(
            "SELECT DISTINCT business_date FROM dsr_reports WHERE restaurant_id=? AND business_date BETWEEN ? AND ?",
            (rid, start.isoformat(), end.isoformat())).fetchall()}
        hist = {str(r["date"])[:10]: dict(r) for r in conn.execute(
            "SELECT date, labor_cost, total_hours, COALESCE(final, 1) AS final FROM labor_daily_history "
            "WHERE restaurant_id=? AND date BETWEEN ? AND ?", (rid, start.isoformat(), end.isoformat())).fetchall()}
    finally:
        conn.close()
    return [d for d in traded if d not in have and d not in reported], hist


def _labor_rows(rid, db_path):
    """{date: [shift rows]} from the stored shifts file."""
    from labor import load_shifts
    from models import get_client_data
    raw = ((get_client_data(rid, db_path=db_path) if db_path else get_client_data(rid)) or {}).get("shifts_csv") or ""
    out = {}
    if not raw.strip():
        return out
    try:
        for r in load_shifts(csv_string=raw):
            out.setdefault(str(r.get("date") or "")[:10], []).append(r)
    except Exception as e:
        log.warning("dsr backfill: shifts unreadable rid=%s: %s", rid, e)
    return out


def _labor_metrics(restaurant, hist_row, rows, net):
    """labor.* for one filled day, under the Labor block's costing rule."""
    import thresholds
    from dsr.block_labor import POS_PAY_MIN_SHARE, _pos_pay_share
    if not hist_row or not int(hist_row.get("final") or 0):
        return {}
    out = {}
    hours = hist_row.get("total_hours")
    if hours:
        out["labor.hours"] = round(float(hours), 2)
    basis = thresholds.labor_cost_basis(restaurant)
    share = _pos_pay_share(rows)
    costed = basis != "default" or (share is not None and share >= POS_PAY_MIN_SHARE)
    cost = hist_row.get("labor_cost")
    if costed and cost:
        out["labor.cost"] = round(float(cost), 2)
        if net and net > 0:
            out["labor.pct"] = round(float(cost) / float(net) * 100.0, 1)
    return out


def backfill(restaurant, days=60, db_path=None, max_days=MAX_DAYS_PER_RUN) -> dict:
    """Fill the missing nights (module doc). {days, filled, remaining, failed}."""
    import models
    import pos
    from dsr.block_sales import _gross, categorize
    db_path = db_path or models.DB_PATH
    rid = restaurant.id
    provider, _mod = pos.connected_provider(rid)
    if not provider:
        return {"days": 0, "filled": 0, "remaining": 0, "failed": 0, "reason": "no POS"}
    from inventory_ledger import local_today
    end = local_today(rid) - timedelta(days=1)
    start = end - timedelta(days=max(1, int(days)) - 1)
    missing, hist = _missing_days(rid, start, end, db_path)
    shift_rows = _labor_rows(rid, db_path)
    labor_added = _labor_for_reported(restaurant, start, end, hist, shift_rows, db_path)
    todo = missing[:max(0, int(max_days))]
    if not todo:
        return {"days": 0, "filled": 0, "remaining": 0, "failed": 0, "labor_added": labor_added}
    mapping = store.category_map(rid, db_path=db_path)
    ctx = SimpleNamespace(restaurant=restaurant)
    filled = failed = 0
    source = f"{provider}{SOURCE_SUFFIX}"
    for d in todo:
        try:
            data, prov = pos.fetch_day_sales(rid, date.fromisoformat(d))
        except Exception as e:
            failed += 1
            log.warning("dsr backfill rid=%s %s: %s", rid, d, e)
            continue
        if not data.get("transactions") and not data.get("gross"):
            continue                    # the POS holds no tickets for it: not a $0 night
        net = data.get("net")
        gross, _basis, _missing = _gross(ctx, data)
        m = {"sales.gross": gross, "sales.gross_items": data.get("gross"), "sales.net": net,
             "sales.transactions": data.get("transactions"), "sales.guests": data.get("guests"),
             "sales.discounts": data.get("discounts"), "sales.comps": data.get("comps"),
             "sales.voids": data.get("voids"), "sales.refunds": data.get("refunds"), "sales.tax": data.get("tax")}
        deps = {k: round(float(v or 0), 2) for k, v in (data.get("by_department") or {}).items()}
        for dep, v in deps.items():
            m[f"sales.dep:{dep}"] = v
        cats, unmapped = categorize(mapping, deps, net)
        for c in cats:
            m[f"sales.cat:{c['category']}"] = c["net"]
        if unmapped:
            m[f"sales.cat:{dsr.UNMAPPED}"] = round(sum(u["net"] for u in unmapped), 2)
        m.update(_labor_metrics(restaurant, hist.get(d), shift_rows.get(d) or [], net))
        conn = store.get_conn(db_path)
        try:
            for key, value in m.items():
                if value is None:
                    continue
                conn.execute(
                    "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source, report_id, "
                    "updated_at) VALUES (?,?,?,?,?,?,NULL,datetime('now')) ON CONFLICT(restaurant_id, business_date, "
                    "metric) DO NOTHING", (rid, d, key, value, dsr.READY, f"{prov or provider}{SOURCE_SUFFIX}"))
            conn.commit()
        finally:
            conn.close()
        filled += 1
    return {"days": len(todo), "filled": filled, "remaining": len(missing) - len(todo), "failed": failed,
            "source": source, "labor_added": labor_added}


def _labor_for_reported(restaurant, start, end, hist, shift_rows, db_path):
    """labor.* for nights a report measured but whose Labor block withheld
    its dollars (no wage entered) — now costed at the POS's pay where it
    prices the night, as the report view does (access._live_labor). Never
    over a figure the report stored. Returns nights given labor."""
    rid = restaurant.id
    conn = store.get_conn(db_path)
    try:
        nets = {str(r[0])[:10]: r[1] for r in conn.execute(
            "SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? AND metric='sales.net' "
            "AND report_id IS NOT NULL AND business_date BETWEEN ? AND ?",
            (rid, start.isoformat(), end.isoformat())).fetchall()}
        costed = {str(r[0])[:10] for r in conn.execute(
            "SELECT business_date FROM dsr_metrics WHERE restaurant_id=? AND metric='labor.cost' "
            "AND business_date BETWEEN ? AND ?", (rid, start.isoformat(), end.isoformat())).fetchall()}
    finally:
        conn.close()
    added = 0
    for d, net in nets.items():
        if d in costed:
            continue
        m = {k: v for k, v in _labor_metrics(restaurant, hist.get(d), shift_rows.get(d) or [], net).items()
             if k in ("labor.cost", "labor.pct")}
        if not m:
            continue
        conn = store.get_conn(db_path)
        try:
            for key, value in m.items():
                conn.execute(
                    "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source, report_id, "
                    "updated_at) VALUES (?,?,?,?,?,?,NULL,datetime('now')) ON CONFLICT(restaurant_id, business_date, "
                    "metric) DO NOTHING", (rid, d, key, value, dsr.READY, "pos_wages_after_report"))
            conn.commit()
        finally:
            conn.close()
        added += 1
    return added
