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
    from labor import shift_history
    out = {}
    try:
        # The one reader (SCALE-5): shift_facts once they are the record.
        for r in shift_history(rid, db_path=db_path):
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
    filled = failed = 0
    source = f"{provider}{SOURCE_SUFFIX}"
    import pos_io
    for d in todo:
        try:
            # History: the "bulk" lane of the POS request budget, never ahead
            # of a live or close read (blind re-audit 10/20/26 R3-01).
            with pos_io.lane("bulk"):
                got = _day_metrics(restaurant, d, mapping, hist, shift_rows)
        except Exception as e:
            failed += 1
            log.warning("dsr backfill rid=%s %s: %s", rid, d, e)
            continue
        if got is None:
            continue                    # the POS holds no tickets for it: not a $0 night
        m, prov = got
        _write(rid, d, m, f"{prov or provider}{SOURCE_SUFFIX}", db_path)
        filled += 1
    return {"days": len(todo), "filled": filled, "remaining": len(missing) - len(todo), "failed": failed,
            "source": source, "labor_added": labor_added}


def _day_metrics(restaurant, d, mapping, hist, shift_rows):
    """(metrics, provider) for one night from the POS's own day read, or
    None when the POS holds no tickets for it. Each department's net and,
    since 10/15/26, each POS item category inside one (`sales.depcat:`, as
    the report's Sales block stores them): the Bar maps item categories
    (bar.mapping), so a backfilled night is mapped at the same level as a
    reported one."""
    import pos
    from dsr.block_sales import _gross, categorize
    data, prov = pos.fetch_day_sales(restaurant.id, date.fromisoformat(d))
    if not data.get("transactions") and not data.get("gross"):
        return None
    net = data.get("net")
    gross, _basis, _missing = _gross(SimpleNamespace(restaurant=restaurant), data)
    m = {"sales.gross": gross, "sales.gross_items": data.get("gross"), "sales.net": net,
         "sales.transactions": data.get("transactions"), "sales.guests": data.get("guests"),
         "sales.discounts": data.get("discounts"), "sales.comps": data.get("comps"),
         "sales.voids": data.get("voids"), "sales.refunds": data.get("refunds"), "sales.tax": data.get("tax")}
    deps = {k: round(float(v or 0), 2) for k, v in (data.get("by_department") or {}).items()}
    for dep, v in deps.items():
        m[f"sales.dep:{dep}"] = v
    contents = data.get("by_department_category") or {}
    for dep, inside in contents.items():
        for sub, v in (inside or {}).items():
            m[f"sales.depcat:{store.sub_name(dep, sub)}"] = round(float(v or 0), 2)
    cats, unmapped = categorize(mapping, deps, net)
    for c in cats:
        m[f"sales.cat:{c['category']}"] = c["net"]
    if unmapped:
        m[f"sales.cat:{dsr.UNMAPPED}"] = round(sum(u["net"] for u in unmapped), 2)
    m.update(_labor_metrics(restaurant, hist.get(d), shift_rows.get(d) or [], net))
    return m, prov


def _write(rid, d, m, source, db_path, replace_source=None):
    """Insert one night's figures; with `replace_source`, that source's
    rows for the night (and only report-less rows) are replaced whole first."""
    conn = store.get_conn(db_path)
    try:
        if replace_source:
            conn.execute("DELETE FROM dsr_metrics WHERE restaurant_id=? AND business_date=? AND report_id IS NULL "
                         "AND source=?", (rid, d, replace_source))
        for key, value in m.items():
            if value is None:
                continue
            conn.execute(
                "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source, report_id, "
                "updated_at) VALUES (?,?,?,?,?,?,NULL,datetime('now')) ON CONFLICT(restaurant_id, business_date, "
                "metric) DO NOTHING", (rid, d, key, value, dsr.READY, source))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def restate(restaurant_id, dates, db_path=None) -> dict:
    """The POS restated these nights (pos_archive found a re-post and stored
    them again): each night this backfill filled — no report of its own,
    `source` `<provider>_backfill` — is read again and its figures replaced
    whole (bar audit 10/14/26 RPW-13 / R11). A night a report measured is
    the report's own (the report path re-checks late data itself).
    {restated, skipped, failed}."""
    import models
    db_path = db_path or models.DB_PATH
    r = models.get_restaurant(int(restaurant_id), db_path=db_path)
    out = {"restated": 0, "skipped": 0, "failed": 0}
    if r is None or not dates:
        return out
    rid = r.id
    days = sorted({str(d)[:10] for d in dates})
    conn = store.get_conn(db_path)
    try:
        marks = ",".join("?" for _ in days)
        filled = {str(x[0])[:10]: x[1] for x in conn.execute(
            f"SELECT business_date, source FROM dsr_metrics WHERE restaurant_id=? AND metric='sales.net' "
            f"AND report_id IS NULL AND business_date IN ({marks})", (rid, *days)).fetchall()}
        reported = {str(x[0])[:10] for x in conn.execute(
            f"SELECT DISTINCT business_date FROM dsr_reports WHERE restaurant_id=? AND business_date IN ({marks})",
            (rid, *days)).fetchall()}
    finally:
        conn.close()
    todo = [d for d in days if d in filled and str(filled[d] or "").endswith(SOURCE_SUFFIX) and d not in reported]
    out["skipped"] = len(days) - len(todo)
    if not todo:
        return out
    lo, hi = date.fromisoformat(todo[0]), date.fromisoformat(todo[-1])
    _missing, hist = _missing_days(rid, lo, hi, db_path)
    shift_rows = _labor_rows(rid, db_path)
    mapping = store.category_map(rid, db_path=db_path)
    for d in todo:
        try:
            got = _day_metrics(r, d, mapping, hist, shift_rows)
            if got is None:
                out["skipped"] += 1
                continue
            m, _prov = got
            _write(rid, d, m, filled[d], db_path, replace_source=filled[d])
            out["restated"] += 1
        except Exception as e:
            out["failed"] += 1
            log.warning("dsr backfill restate rid=%s %s: %s", rid, d, e)
    return out


def repair_department_metrics(restaurant_id, dates=None, db_path=None, archive=False, dry_run=False) -> dict:
    """Nights that hold net sales but no department dollars (`sales.dep:*`) —
    Simple EJ's first report, 9/27/26, built before the departments were
    mapped, kept only `sales.cat:Unmapped` = $11,617.28 — given their
    department and item-category dollars from the ticket archive (bar audit
    10/14/26 FIN-3 / RPW-2 / V5). Idempotent: a night that has department
    dollars is left alone, and a night is written only when its archived
    departments add up to its stored net (within $1) and none of them is
    unresolved ("Unassigned" over $1) — otherwise it is listed and nothing
    is written. `archive=True` stores each night from the POS again first
    (pos_archive.archive_day: RPOWER reads), for a night archived before
    lines carried their department and before the catalog was kept.
    Writes `sales.dep:` / `sales.depcat:` with source `archive_repair`;
    a later version of the night's report replaces them. `dry_run` checks
    and lists without writing (the archive's own derived nights are still
    refreshed — they are mapping-independent figures, not the report's).
    {repaired: [..], skipped: [{date, why}], checked}."""
    import models
    db_path = db_path or models.DB_PATH
    rid = int(restaurant_id)
    conn = store.get_conn(db_path)
    try:
        nets = {str(x[0])[:10]: float(x[1]) for x in conn.execute(
            "SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? AND metric='sales.net' "
            "AND value IS NOT NULL", (rid,)).fetchall()}
        with_deps = {str(x[0])[:10] for x in conn.execute(
            "SELECT DISTINCT business_date FROM dsr_metrics WHERE restaurant_id=? AND metric >= 'sales.dep:' "
            "AND metric < 'sales.dep;'", (rid,)).fetchall()}
    finally:
        conn.close()
    want = sorted({str(d)[:10] for d in dates}) if dates else sorted(nets)
    todo = [d for d in want if d in nets and d not in with_deps and nets[d] > 0]
    out = {"repaired": [], "skipped": [], "checked": len(want)}
    from bar.sales import derive_nights
    for d in todo:
        if archive and not dry_run:
            try:
                import pos_archive
                pos_archive.archive_day(rid, date.fromisoformat(d), db_path=db_path)
            except Exception as e:
                out["skipped"].append({"date": d, "why": f"could not be archived again: {type(e).__name__}"})
                continue
        derive_nights(rid, [d], db_path=db_path)
        conn = store.get_conn(db_path)
        try:
            cells = conn.execute("SELECT department, sales_category, sales, discount_lines FROM bar_sales_nights "
                                 "WHERE restaurant_id=? AND business_date=?", (rid, d)).fetchall()
        finally:
            conn.close()
        deps, subs = {}, {}
        for c in cells:
            dep = str(c["department"] or "").strip()
            if not dep:
                continue
            v = float(c["sales"] or 0) + float(c["discount_lines"] or 0)
            deps[dep] = round(deps.get(dep, 0.0) + v, 2)
            if str(c["sales_category"] or "").strip():
                key = store.sub_name(dep, c["sales_category"])
                subs[key] = round(subs.get(key, 0.0) + v, 2)
        if not deps:
            out["skipped"].append({"date": d, "why": "not in the ticket archive"})
            continue
        if abs(deps.get("Unassigned", 0.0)) > 1.0:
            out["skipped"].append({"date": d, "why": f"${deps['Unassigned']:,.2f} of lines with no department "
                                                     "(archive the night again: archive=True)"})
            continue
        total = round(sum(deps.values()), 2)
        if abs(total - nets[d]) > 1.0:
            out["skipped"].append({"date": d, "why": f"archived departments add to ${total:,.2f}, "
                                                     f"the night's net is ${nets[d]:,.2f}"})
            continue
        m = {f"sales.dep:{k}": v for k, v in deps.items()}
        m.update({f"sales.depcat:{k}": v for k, v in subs.items()})
        if not dry_run:
            _write(rid, d, m, "archive_repair", db_path)
        out["repaired"].append({"date": d, "departments": deps, "net": nets[d]} if dry_run else d)
    return out


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
