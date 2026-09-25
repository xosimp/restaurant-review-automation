"""
dsr.block_labor — the Labor block: `collect(ctx) -> block`.

Reads what the product already syncs; calls no POS itself.

  labor $, hours    labor_daily_history — the per-day archive every POS sync
                    (pos.save_synced_shifts / rpower.sync_to_db) and every
                    shifts upload writes, costed by the Labor module (role
                    rates, the weekly overtime premium allocated to its days)
  labor %           labor $ over TONIGHT's net sales when the Sales block is
                    ready, so the report's own figures reconcile; otherwise
                    the archive's own percentage, and the detail says which
  target            notify.labor_target_for — the one target every surface uses
  overtime          RPOWER: the payroll engine's own OT/DT split carried on
                    each punch (rpower.normalise_entries' notes). Any other
                    source: the hours this day pushed someone past 40 in the
                    restaurant's payroll week (labor.OVERTIME_THRESHOLD_HOURS,
                    labor.get_week_start_day) — the Labor module's rule, per day
  after 6pm         the day's shifts clipped at 6pm, against the Sales block's
                    hourly curve after 6pm
  coverage          the day's "hasn't clocked in" issues (strategy_jobs
                    .run_coverage_check): closed by a clock-in = late, the
                    rest = no-shows. Measured only where that check could run
                    (Labor module, a routed manager, a POS with a live clock-in
                    feed, a published schedule for the day); elsewhere None
  shift quality     the published week's Shift Quality evaluation
                    (schedule_history.quality_json) for this date's shifts

Status:
  ready          labor $ and hours are on file for the day
  awaiting       a POS is connected and has not synced since the night closed
                 (the nightly sync is what writes the archive)
  unavailable    it has synced since and there is still nothing for the day
  not_connected  no POS and no shifts uploaded
"""
import json
import logging
import re
from datetime import datetime, time

import dsr
from dsr import store
from dsr.block_sales import EVENING_HOUR

log = logging.getLogger("dsr")

REASON_SYNC_PENDING = "Labor syncs from the POS overnight"
REASON_NOTHING = "No time punches recorded for this day"
REASON_NOT_CONNECTED = "Connect your POS or upload shifts to see labor"

_OT_NOTE = re.compile(r"\b(OT|DT)\s+([0-9.]+)h", re.I)


def _history_row(ctx):
    conn = store.get_conn(ctx.db_path)
    try:
        row = conn.execute("SELECT labor_pct, labor_cost, sales, total_hours FROM labor_daily_history "
                           "WHERE restaurant_id=? AND date=?", (ctx.restaurant_id, ctx.day)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _shifts(ctx):
    """(every shift row in the synced/uploaded file, whether a file exists)."""
    from models import get_client_data
    from labor import load_shifts
    data = get_client_data(ctx.restaurant_id, db_path=ctx.db_path) or {}
    raw = data.get("shifts_csv") or ""
    if not raw.strip():
        return [], False
    try:
        return load_shifts(csv_string=raw), True
    except Exception as e:
        log.warning("dsr labor: shifts unreadable rid=%s: %s", ctx.restaurant_id, e)
        return [], True


def _synced_since_close(ctx, provider):
    """Whether the connected POS has synced since this night closed."""
    from time_utils import parse_stored_dt
    from dsr import pipeline
    stamp = parse_stored_dt(getattr(ctx.restaurant, f"{provider}_last_synced", None), tz="UTC")
    if stamp is None:
        return False
    close = pipeline.close_at(ctx.restaurant, ctx.business_date) or \
        datetime.combine(ctx.business_date, time(*pipeline.DEFAULT_CLOSE))
    return stamp >= pipeline.to_utc(ctx.restaurant, close)


def _clock_minutes(value):
    from time_utils import parse_clock
    hm = parse_clock(value)
    return None if hm is None else hm[0] * 60 + hm[1]


def _span(row):
    """(start, end) minutes from the business day's midnight; a shift that
    starts before the business day begins (00:30) is tonight's, after
    midnight, and one that ends at or before its start ran past midnight."""
    from time_utils import BUSINESS_DAY_START_HOUR
    start, end = _clock_minutes(row.get("shift_start")), _clock_minutes(row.get("shift_end"))
    if start is None or end is None:
        return None
    if start < BUSINESS_DAY_START_HOUR * 60:
        start += 1440
        end += 1440
    if end <= start:
        end += 1440
    return start, end


def _hours_after(rows, hour=EVENING_HOUR):
    """(hours worked, of which at or after `hour`), each shift's recorded
    hours shared out over its clock span — so a break is not counted twice."""
    from labor import _shift_hours
    total = after = 0.0
    for row in rows:
        span = _span(row)
        hours = _shift_hours(row)
        if not span or hours <= 0:
            continue
        start, end = span
        late = max(0, end - max(start, hour * 60))
        total += hours
        after += hours * late / (end - start)
    return round(total, 2), round(after, 2)


def _overtime(ctx, provider, rows, all_rows):
    """(overtime hours for the day, source) — or (None, None)."""
    day_rows = rows
    if provider == "rpower":
        ot = 0.0
        for row in day_rows:
            for _kind, h in _OT_NOTE.findall(row.get("notes") or ""):
                try:
                    ot += float(h)
                except ValueError:
                    continue
        return round(ot, 2), "rpower"
    if not all_rows:
        return None, None
    import labor
    try:
        wsd = labor.get_week_start_day(ctx.restaurant_id)
    except Exception:
        wsd = 0
    week_start = labor._week_key(ctx.day, wsd)
    before, today = {}, {}
    for row in all_rows:
        d = str(row.get("date") or "")[:10]
        if not d or not (week_start <= d <= ctx.day):
            continue
        who = (row.get("employee") or "").strip().lower()
        if not who:
            continue
        bucket = today if d == ctx.day else before
        bucket[who] = bucket.get(who, 0.0) + labor._shift_hours(row)
    limit = labor.OVERTIME_THRESHOLD_HOURS
    ot = sum(max(0.0, before.get(w, 0.0) + h - limit) - max(0.0, before.get(w, 0.0) - limit)
             for w, h in today.items())
    return round(ot, 2), "cavnar"


def _coverage(ctx):
    """{"measured", "no_shows", "late", "reason"}."""
    import issues
    import intraday
    import pos
    conn = store.get_conn(ctx.db_path)
    try:
        rows = conn.execute("SELECT source_key, status, resolution_note, meta_json FROM ops_issues "
                            "WHERE restaurant_id=? AND kind='coverage' AND source_key LIKE ?",
                            (ctx.restaurant_id, f"coverage:{ctx.day}:%")).fetchall()
    finally:
        conn.close()
    no_shows, late = [], []
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "null") or {}
        except (TypeError, ValueError):
            meta = {}
        entry = {"employee": meta.get("missing") or r["source_key"].split(":", 2)[2],
                 "role": meta.get("role"), "shift_start": meta.get("shift_start")}
        if (r["resolution_note"] or "").startswith("Closed automatically: they clocked in"):
            late.append(entry)
        else:
            no_shows.append(entry)
    if rows:
        return {"measured": True, "no_shows": no_shows, "late": late, "reason": None}
    why = None
    if not getattr(ctx.restaurant, "module_labor", 0):
        why = "The Labor module isn't on"
    elif not pos.supports(ctx.restaurant_id, "fetch_clock_ins_today"):
        why = "The POS has no live clock-in feed"
    elif "manager" not in issues.get_routing(ctx.restaurant_id, db_path=ctx.db_path):
        why = "No manager is routed for coverage issues"
    elif not intraday.published_rows(ctx.restaurant_id, ctx.business_date, db_path=ctx.db_path):
        why = "No published schedule covered this day"
    if why:
        return {"measured": False, "no_shows": [], "late": [], "reason": why}
    return {"measured": True, "no_shows": [], "late": [], "reason": None}


def _scheduled(ctx):
    """Distinct people on the published schedule for the night, or None when
    no published schedule covered it (never 0 for "unknown")."""
    try:
        import intraday
        rows = intraday.published_rows(ctx.restaurant_id, ctx.business_date, db_path=ctx.db_path)
    except Exception:
        return None
    names = {" ".join(str(r.get("employee") or "").lower().split()) for r in rows or []}
    names.discard("")
    return len(names) if names else None


def _quality(ctx):
    """The published week's Shift Quality for this date, or None."""
    conn = store.get_conn(ctx.db_path)
    try:
        row = conn.execute(
            "SELECT id, quality_json FROM schedule_history h WHERE h.restaurant_id=? "
            "AND h.published_at IS NOT NULL AND h.superseded_by IS NULL "
            "AND h.week_start <= ? AND h.week_end >= ? "
            "AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=h.restaurant_id "
            "AND nw.week_start=h.week_start AND nw.published_at IS NOT NULL AND nw.id > h.id) "
            "ORDER BY h.id DESC LIMIT 1", (ctx.restaurant_id, ctx.day, ctx.day)).fetchone()
    except Exception as e:
        log.warning("dsr labor: schedule history unreadable rid=%s: %s", ctx.restaurant_id, e)
        row = None
    finally:
        conn.close()
    if not row:
        return None
    try:
        q = json.loads(row["quality_json"] or "null") or {}
    except (TypeError, ValueError):
        return None
    shifts = [s for s in (q.get("shifts") or [])
              if isinstance(s, dict) and str(s.get("date") or "")[:10] == ctx.day and s.get("scored")]
    if not shifts:
        return None
    return {"schedule_id": row["id"], "week_score": q.get("score"),
            "score": round(sum(float(s["score"]) for s in shifts) / len(shifts)),
            "shifts": [{"daypart": s.get("daypart"), "score": s.get("score"), "band": s.get("band"),
                        "meets_profile": s.get("meets_profile"), "headline": s.get("headline")} for s in shifts]}


def collect(ctx):
    import pos
    from notify import labor_target_for
    provider, _mod = pos.connected_provider(ctx.restaurant_id)
    hist = _history_row(ctx)
    all_rows, has_file = _shifts(ctx)
    day_rows = [r for r in all_rows if str(r.get("date") or "")[:10] == ctx.day]

    if hist is None or not (hist.get("labor_cost") or hist.get("total_hours")):
        if not provider and not has_file:
            return dsr.block(dsr.NOT_CONNECTED, reason=REASON_NOT_CONNECTED, block_name="labor")
        if provider and not _synced_since_close(ctx, provider):
            return dsr.block(dsr.AWAITING, source=provider, reason=REASON_SYNC_PENDING, block_name="labor",
                             detail={"waiting_for": "pos_sync"})
        return dsr.block(dsr.UNAVAILABLE, source=provider or "upload", reason=REASON_NOTHING, block_name="labor")

    source = provider or "upload"
    cost = round(float(hist.get("labor_cost") or 0), 2)
    worked, after = _hours_after(day_rows)
    hours = round(float(hist.get("total_hours") or 0), 2) or (worked or None)

    sales = ctx.blocks.get("sales") or {}
    net = (sales.get("metrics") or {}).get("net") if sales.get("status") == dsr.READY else None
    if net:
        pct, basis = round(cost / net * 100.0, 1), "dsr_net"
    elif hist.get("labor_pct") is not None:
        pct, basis = round(float(hist["labor_pct"]), 1), "labor_history"
    else:
        pct, basis = None, None
    target = labor_target_for(ctx.restaurant)
    ot, ot_source = _overtime(ctx, provider, day_rows, all_rows)

    after_share = round(after / worked * 100.0, 1) if worked else None
    sales_after = (sales.get("metrics") or {}).get("evening_share_pct") if net else None
    cov = _coverage(ctx)
    quality = _quality(ctx)
    scheduled = _scheduled(ctx)

    metrics = {
        "cost": cost, "pct": pct, "hours": hours, "overtime_hours": ot,
        "target_pct": target, "vs_target_pts": round(pct - target, 1) if pct is not None else None,
        "hours_after_6pm": after if worked else None, "hours_after_6pm_share_pct": after_share,
        "sales_after_6pm_share_pct": sales_after,
        "no_shows": len(cov["no_shows"]) if cov["measured"] else None,
        "late_arrivals": len(cov["late"]) if cov["measured"] else None,
        "shift_quality": quality["score"] if quality else None,
        # People on the published schedule for the night (the Manager DSR's
        # "Employees scheduled"); None without a published schedule.
        "scheduled": scheduled,
    }

    observations = []
    if pct is not None:
        side = "over" if pct > target else "at or under"
        observations.append({"key": "vs_target", "facts": ["labor.pct", "labor.target_pct"],
                             "text": f"Labor was {pct:.1f}% of sales, {side} the {target:g}% target."})
    if after_share is not None and sales_after is not None:
        observations.append({"key": "evening_mix",
                             "facts": ["labor.hours_after_6pm_share_pct", "labor.sales_after_6pm_share_pct"],
                             "text": f"{after_share:.0f}% of labor hours were after 6pm, against "
                                     f"{sales_after:.0f}% of sales."})
    if ot:
        observations.append({"key": "overtime", "facts": ["labor.overtime_hours"],
                             "text": f"{ot:g} overtime hours worked."})
    if cov["measured"] and cov["no_shows"]:
        n = len(cov["no_shows"])
        observations.append({"key": "no_shows", "facts": ["labor.no_shows"],
                             "text": f"{n} scheduled {'person' if n == 1 else 'people'} never clocked in."})

    detail = {
        "pct_basis": basis,
        "overtime_source": ot_source,
        "coverage": cov,
        "shift_quality": quality,
        "observations": observations,
    }
    return dsr.block(dsr.READY, source=source, metrics=metrics, detail=detail, block_name="labor")
