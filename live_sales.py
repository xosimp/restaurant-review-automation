"""live_sales — RPOWER's sales so far today, read incrementally (RPOWER audit
#23 A1-01 / A5-06, F5).

Every live read (live labor, the coverage check's pace, the hourly capture)
used to page the WHOLE day's `ticketsales/getbybusinessdate` again: 6.2x the
night's lines at the hourly cadence (MEASURED 10/20/26, Simple EJ's: 6,081
rows for a 974-line night) and ~25x with live labor on. Now each store keeps
its day-so-far LINE SET — the counted sale lines keyed by RPOWER's own line
identity (`rid:atom`, rpower._row_key) with each line's net value — in one
`pos_live_snapshot` row (kind "sales_lines"), and a read:

  * fetches only the lines written since the last read, less OVERLAP
    (`ticketsales/getbytimestamp`, RPOWER's documented insert-or-update
    feed; its `time_stamp` is UTC, measured 10/21/26), merged by key: a line
    added counts, a line voided or re-rung under a non-revenue type drops
    out, a line edited takes its new value;
  * re-reads the whole day (`getbybusinessdate`) once FULL_EVERY_SECONDS as
    the safety net (a line deleted outright never appears in the change
    feed), on the first read of the day, and whenever the feed cannot be
    read with a time in its window (rpower's date-only fallback);
  * applies the same revenue rule as the night's total (rpower.sales_types +
    rpower._counts_as_revenue, F3's), so the hourly figure and the close
    agree.

Requests stay about the same (1 page a read either way at most stores); rows
downloaded fall ~95 %, and so does the JSON the queue worker parses.
LIVE_SALES_INCREMENTAL=0 reads the whole day every time (the rollback).
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("live_sales")

KIND = "sales_lines"
INCREMENTAL = (os.getenv("LIVE_SALES_INCREMENTAL", "1") or "1").strip().lower() not in ("0", "false", "off", "no")
FULL_EVERY_SECONDS = max(300.0, float(os.getenv("LIVE_SALES_FULL_EVERY_SECONDS", "3600") or 3600))
# How far before the last read's newest stamp the next delta starts (clock
# skew and rows committed late).
OVERLAP = timedelta(minutes=15)


def _stamp(row) -> str:
    return str(row.get("time_stamp") or "").replace("T", " ")[:19]


def _net(lines) -> float:
    return round(sum(float(v) for v in lines.values()), 2)


def _apply(lines, rows, day, types, counts, key_of, biz_date) -> str:
    """Merge `rows` into `lines` ({key: value}) for business date `day`;
    returns the newest stamp seen ("" for none)."""
    newest = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        st = _stamp(row)
        if st > newest:
            newest = st
        if biz_date(row.get("date")) != day:
            continue
        key = key_of(row)
        if counts(row, types):
            lines[key] = round(float(row.get("sales") or 0), 2)
        else:
            lines.pop(key, None)
    return newest


def _types_for(rpower, restaurant_id, rows, types) -> dict:
    """The sales-type list these rows are classified against: re-read when a
    line names a type the kept list does not hold (rpower._types_for, the
    archive's throttle — Audit #23 A2-08), so a type added during service
    counts from its first line, not after the next full re-read."""
    if not any(isinstance(r, dict) and r.get("slstype_mid") not in (None, "")
               and str(r.get("slstype_mid")) not in types for r in rows):
        return types
    try:
        return rpower._types_for(restaurant_id, rows)
    except Exception as e:
        if isinstance(e, getattr(rpower, "RPowerAuthError", ())):
            raise
        return types


def fetch_today(restaurant_id, business_date, now=None) -> dict:
    """{"net": net sales so far or None (nothing counted yet), "mode":
    "full" | "delta", "rows": rows downloaded, "lines": counted lines kept}
    for RPOWER store `restaurant_id` on `business_date` (see the module
    note). Raises what rpower raises (a read that failed keeps the last
    line set, so the next read starts from it)."""
    import pos_io
    import rpower
    day = rpower._d(business_date)
    now_ts = float(now if now is not None else time.time())
    token, base = rpower._ctx(restaurant_id)
    types = rpower.sales_types(restaurant_id)
    prev = pos_io.snapshot_get(restaurant_id, KIND, day) if INCREMENTAL else None
    state = (prev or {}).get("value") or {}
    lines = dict(state.get("lines") or {})
    full_at = float(state.get("full_at") or 0)
    since = state.get("stamp") or ""
    datetime_ok = getattr(rpower, "CHANGE_DATETIME", True) and \
        (getattr(rpower, "_change_datetime_refused", {}) or {}).get("at") is None
    full = (not INCREMENTAL or prev is None or not since or not datetime_ok
            or now_ts - full_at >= FULL_EVERY_SECONDS)
    if full:
        rows = [r for cs, ce in rpower._chunk_range(day, day)
                for r in rpower._paged(token, "ticketsales/getbybusinessdate",
                                       {**base, "startdate": rpower._d(cs), "enddate": rpower._d(ce),
                                        "sortorder": "date"})]
        lines = {}
        types = _types_for(rpower, restaurant_id, rows, types)
        newest = _apply(lines, rows, day, types, rpower._counts_as_revenue, rpower._row_key, rpower._biz_date)
        state = {"lines": lines, "stamp": newest or since, "full_at": now_ts}
        mode = "full"
    else:
        lo = datetime.strptime(since, "%Y-%m-%d %H:%M:%S") - OVERLAP
        hi = datetime.fromtimestamp(now_ts, timezone.utc).replace(tzinfo=None) + timedelta(minutes=1)
        rows = rpower._paged(token, "ticketsales/getbytimestamp", {
            **base, "startdate": lo.strftime("%Y-%m-%dT%H:%M:%S"), "enddate": hi.strftime("%Y-%m-%dT%H:%M:%S"),
            "sortorder": "time_stamp"})
        types = _types_for(rpower, restaurant_id, rows, types)
        newest = _apply(lines, rows, day, types, rpower._counts_as_revenue, rpower._row_key, rpower._biz_date)
        state = {"lines": lines, "stamp": max(since, newest), "full_at": full_at}
        mode = "delta"
    if INCREMENTAL:
        pos_io.snapshot_put(restaurant_id, KIND, day, state, provider="rpower")
    return {"net": _net(lines) if lines else None, "mode": mode, "rows": len(rows), "lines": len(lines)}
