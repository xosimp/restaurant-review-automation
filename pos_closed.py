"""pos_closed — one shared answer to "has the POS closed this business day?"
and when to ask it (RPOWER audit #23 A5-04 / A1-24, F5).

Two pollers asked RPOWER's `closeday` for the same fact, each with its own
memory: the DSR pipeline every POLL_MINUTES (10) from close until the close
came — 15-19 polls a store-night at Simple EJ's, which closes its day ~2.5 h
after its stated close (MEASURED 10/20/26) — and the archive's per-process
cache (rpower.day_closed_cached) in every process that archives. At 5,000
stores the DSR polls alone were 8.3 req/s, over the fleet ceiling.

Now:
  * one `pos_closed_days` row per (restaurant, business date), shared by
    every process: a CLOSED answer is kept for good (a closed day stays
    closed — it is never asked again); an OPEN answer is shared for
    `fresh_seconds` (the asker's own cadence), so the DSR and the archive
    never ask twice for one moment;
  * each store's CLOSE LAG is learned — the median of (first seen closed −
    stated close) over its last LAG_SAMPLE_NIGHTS nights the DSR saw close —
    and the DSR asks first at that lag (less FIRST_EARLY_MINUTES), then
    POLL_STEPS_MINUTES apart (10, 20, 30, 30 …): about four polls a night
    instead of fifteen. With fewer than LAG_MIN_SAMPLES nights it asks from
    the stated close, on the same widening steps. The deadline still sends
    a provisional night (dsr.pipeline).
"""
import logging
import os
import statistics
import time

log = logging.getLogger("pos_closed")

_DDL = (
    """CREATE TABLE IF NOT EXISTS pos_closed_days (
        restaurant_id   INTEGER NOT NULL,
        business_date   TEXT NOT NULL,
        closed          INTEGER NOT NULL DEFAULT 0,
        closed_seen_at  REAL,
        checked_at      REAL,
        polls           INTEGER NOT NULL DEFAULT 0,
        close_epoch     REAL,
        PRIMARY KEY (restaurant_id, business_date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pos_closed_days_date ON pos_closed_days(business_date)",
)

LAG_SAMPLE_NIGHTS = 14
LAG_MIN_SAMPLES = 3
FIRST_EARLY_MINUTES = 5
POLL_STEPS_MINUTES = tuple(int(x) for x in (os.getenv("DSR_CLOSE_POLL_STEPS", "10,20,30") or "10,20,30").split(",")
                           if x.strip().isdigit()) or (10, 20, 30)
# A learned lag longer than this is not believed (a store that skipped a
# close and closed two days in one record): poll from the stated close.
MAX_LAG_MINUTES = 12 * 60


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def init_pos_closed(db_path=None):
    """The shared closed-day table (boot only, models.init_db)."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _row(restaurant_id, day, db_path=None):
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT closed, closed_seen_at, checked_at, polls, close_epoch FROM pos_closed_days "
                               "WHERE restaurant_id=? AND business_date=?",
                               (int(restaurant_id), str(day)[:10])).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.warning("pos_closed_days unreadable for %s %s: %s", restaurant_id, day, e)
        return None


def known(restaurant_id, day, db_path=None):
    """The shared row for (restaurant, day): {closed, closed_seen_at,
    checked_at, polls, close_epoch} or None."""
    return _row(restaurant_id, day, db_path)


def _record(restaurant_id, day, closed, close_epoch=None, db_path=None, now=None):
    now = float(now if now is not None else time.time())
    try:
        conn = get_conn(db_path)
        try:
            conn.execute(
                "INSERT INTO pos_closed_days (restaurant_id, business_date, closed, closed_seen_at, checked_at, polls, "
                "close_epoch) VALUES (?,?,?,?,?,1,?) ON CONFLICT(restaurant_id, business_date) DO UPDATE SET "
                "closed=CASE WHEN pos_closed_days.closed=1 THEN 1 ELSE excluded.closed END, "
                "closed_seen_at=COALESCE(pos_closed_days.closed_seen_at, excluded.closed_seen_at), "
                "checked_at=excluded.checked_at, polls=pos_closed_days.polls+1, "
                "close_epoch=COALESCE(excluded.close_epoch, pos_closed_days.close_epoch)",
                (int(restaurant_id), str(day)[:10], 1 if closed else 0, now if closed else None, now,
                 float(close_epoch) if close_epoch else None))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("pos_closed_days not written for %s %s: %s", restaurant_id, day, e)


def day_closed(restaurant_id, day, ask, fresh_seconds=600, close_epoch=None, db_path=None, now=None) -> bool:
    """Whether the POS has closed `day`: the shared row when it says closed
    (for good) or holds an open answer younger than `fresh_seconds`, else
    `ask()` (the POS read; what it raises propagates and nothing is kept)
    with its answer kept for every process. `close_epoch` (the stated close,
    epoch) is kept beside it for the close-lag learning."""
    row = _row(restaurant_id, day, db_path)
    if row and int(row.get("closed") or 0):
        return True
    if row and row.get("checked_at") and (time.time() if now is None else float(now)) - float(row["checked_at"]) < float(fresh_seconds):
        if close_epoch and not row.get("close_epoch"):
            _note_close(restaurant_id, day, close_epoch, db_path)
        return False
    closed = bool(ask())
    _record(restaurant_id, day, closed, close_epoch=close_epoch, db_path=db_path, now=now)
    return closed


def _note_close(restaurant_id, day, close_epoch, db_path=None):
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE pos_closed_days SET close_epoch=? WHERE restaurant_id=? AND business_date=? "
                         "AND close_epoch IS NULL", (float(close_epoch), int(restaurant_id), str(day)[:10]))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("close time not kept for %s %s: %s", restaurant_id, day, e)


def close_lag_minutes(restaurant_id, db_path=None):
    """The store's learned close lag (minutes from the stated close to the
    POS's close being first seen), the median of its last LAG_SAMPLE_NIGHTS
    nights with both — or None with fewer than LAG_MIN_SAMPLES."""
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT closed_seen_at, close_epoch FROM pos_closed_days WHERE restaurant_id=? AND closed=1 "
                "AND closed_seen_at IS NOT NULL AND close_epoch IS NOT NULL ORDER BY business_date DESC LIMIT ?",
                (int(restaurant_id), LAG_SAMPLE_NIGHTS)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning("close lag unreadable for %s: %s", restaurant_id, e)
        return None
    lags = [(float(r[0]) - float(r[1])) / 60.0 for r in rows]
    lags = [x for x in lags if -60 <= x <= MAX_LAG_MINUTES]
    if len(lags) < LAG_MIN_SAMPLES:
        return None
    return max(0.0, statistics.median(lags))


def first_poll_at(restaurant_id, close_epoch, db_path=None) -> float:
    """The first moment worth asking (epoch): the stated close plus the
    learned lag less FIRST_EARLY_MINUTES; the stated close with no lag."""
    lag = close_lag_minutes(restaurant_id, db_path)
    return float(close_epoch) + max(0.0, (lag or 0.0) - FIRST_EARLY_MINUTES) * 60.0


def next_poll_at(restaurant_id, day, close_epoch, now, db_path=None) -> float:
    """When the DSR should next ask whether `day` is closed (epoch): not
    before the stated close plus the learned lag (less FIRST_EARLY_MINUTES);
    from there POLL_STEPS_MINUTES apart, widening with each poll already made
    at or after that first moment (the shared row's count)."""
    first = first_poll_at(restaurant_id, close_epoch, db_path)
    now = float(now)
    if now < first:
        return first
    row = _row(restaurant_id, day, db_path) or {}
    polls = int(row.get("polls") or 0)
    step = POLL_STEPS_MINUTES[min(max(0, polls - 1), len(POLL_STEPS_MINUTES) - 1)]
    return now + step * 60.0
