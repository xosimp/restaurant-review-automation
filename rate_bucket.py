"""Shared token buckets in the database, for any budget every process must
share (scale audit 10/19/26, SEC-03).

One row per bucket in `rate_buckets` (bucket TEXT PK, credit REAL,
updated_at REAL — epoch seconds), taken with a compare-and-swap so two
processes — the web worker, the queue worker, the scheduler thread — cannot
both spend one slot. rpower's own request budget (`rpower_rate_bucket`) is
the same idea, older and keyed by a token hash; this is the general form.

`take(key, per_second, burst)` returns 0.0 (go now, the slot is taken), the
seconds to wait before asking again, or None when the table cannot be
reached — the caller then keeps its own local pacing, never none.
"""
from __future__ import annotations

import time

import models as _models

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_buckets (
    bucket      TEXT PRIMARY KEY,
    credit      REAL NOT NULL,
    updated_at  REAL NOT NULL
)
"""


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path is not None else _models.get_conn()


def init_rate_buckets(db_path=None):
    """Boot-time schema (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def take(key: str, per_second: float, burst: float = 1.0, db_path=None):
    """One slot from bucket `key`, refilled at `per_second` up to `burst`."""
    if per_second <= 0:
        return 0.0
    burst = max(1.0, float(burst))
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        for _ in range(8):                         # compare-and-swap retries
            now = time.time()
            row = conn.execute("SELECT credit, updated_at FROM rate_buckets WHERE bucket=?", (key,)).fetchone()
            if row is None:
                cur = conn.execute("INSERT INTO rate_buckets (bucket, credit, updated_at) VALUES (?,?,?) "
                                   "ON CONFLICT(bucket) DO NOTHING", (key, burst - 1.0, now))
                conn.commit()
                if cur.rowcount == 1:
                    return 0.0
                continue
            credit = min(burst, float(row[0]) + max(0.0, now - float(row[1])) * per_second)
            if credit < 1.0:
                return (1.0 - credit) / per_second
            cur = conn.execute("UPDATE rate_buckets SET credit=?, updated_at=? WHERE bucket=? AND updated_at=?",
                               (credit - 1.0, now, key, float(row[1])))
            conn.commit()
            if cur.rowcount == 1:
                return 0.0
        return 1.0 / per_second                      # contended: wait one slot and ask again
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        conn.close()


def wait_for(key: str, per_second: float, burst: float = 1.0, max_wait: float = 60.0, db_path=None,
             sleep=time.sleep) -> bool:
    """Block until a slot is taken (True), the table is unreachable (None
    from take: False, the caller paces locally), or `max_wait` passes
    (False)."""
    waited = 0.0
    while True:
        w = take(key, per_second, burst, db_path=db_path)
        if w is None:
            return False
        if w <= 0:
            return True
        if waited >= max_wait:
            return False
        step = min(w, 1.0)
        sleep(step)
        waited += step
