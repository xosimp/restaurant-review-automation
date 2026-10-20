"""
Monthly partitions of the partitioned fact tables on Postgres (scale audit
10/19/26, DB-04).

pg_copy builds each db_dialect.PARTITIONED table PARTITION BY RANGE with one
partition a month from its oldest row to MONTHS_AHEAD past the copy, plus a
DEFAULT partition. Nothing made the months after that: three months after the
cutover every new row would have landed in DEFAULT, which then grows without
bound and blocks creating that month's partition later (Postgres refuses a
partition whose range DEFAULT already holds rows).

ensure() keeps MONTHS_AHEAD months ahead of today on every partitioned table
(CREATE TABLE IF NOT EXISTS ... PARTITION OF, idempotent), and counts the
dated rows sitting in each DEFAULT partition — a row the copy's NULL sentinel
did not put there means a month was missed, and the job reports it failed.
Daily from the scheduler (jobs_registry "pg_partitions"); a no-op on SQLite.

Retention (scale audit 10/19/26, DB-03): ops.prune_ledgers drops each month
wholly past a partitioned table's window (drop_expired — DETACH then DROP:
one catalog change instead of a month of row deletes under the write lock),
then deletes in chunks what is left past the window (the partial month, the
DEFAULT partition). At most PARTITION_DROPS_PER_PASS months a table a night,
so a wrong clock or a shortened window cannot take a year in one pass — the
same guard the chunked delete's per-pass cap is.
"""
import os
import re
from datetime import date

import pg_copy

PARTITION_DROPS_PER_PASS = int(os.getenv("PARTITION_DROPS_PER_PASS", "1"))
_MONTH_PART = re.compile(r"^(?P<table>.+)_p(?P<y>\d{4})(?P<m>\d{2})$")


def _partitioned_tables(conn) -> list:
    """The partitioned parents that exist on this Postgres, of the ones
    db_dialect partitions (a table the copy never saw is not one)."""
    import db_dialect
    rows = conn.execute_native(
        "SELECT c.relname FROM pg_partitioned_table p JOIN pg_class c ON c.oid = p.partrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema()").fetchall()
    have = {(r[0] if not hasattr(r, "keys") else r["relname"]) for r in rows}
    return sorted(t for t in db_dialect.PARTITIONED if t in have)


def ensure(conn, today=None, months_ahead=None) -> dict:
    """Create this month's partition and the next `months_ahead` on every
    partitioned table; count dated rows in each DEFAULT. Commits."""
    import db_dialect
    today = today or date.today()
    ahead = pg_copy.MONTHS_AHEAD if months_ahead is None else months_ahead
    first = date(today.year, today.month, 1)
    last = pg_copy._add_months(today, ahead)
    out = {"tables": 0, "created": 0, "default_rows": {}}
    for table in _partitioned_tables(conn):
        out["tables"] += 1
        before = _partition_count(conn, table)
        for ddl in pg_copy.partition_ddl(table, first, last):
            conn.execute_native(ddl)
        out["created"] += _partition_count(conn, table) - before
        col = db_dialect.PARTITIONED[table]
        # Below 1971 is the copy's NULL sentinel, which belongs in DEFAULT.
        n = conn.execute_native(f"SELECT COUNT(*) FROM {table}_default WHERE {col} >= '1971-01-01'").fetchone()[0]
        if n:
            out["default_rows"][table] = int(n)
    conn.commit()
    return out


def _partition_count(conn, table) -> int:
    return int(conn.execute_native(
        # `table` is one of db_dialect.PARTITIONED's own names, never input.
        f"SELECT COUNT(*) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhparent WHERE c.relname = '{table}'"
    ).fetchone()[0])


def month_partitions(conn, table) -> list:
    """[(name, 'YYYY-MM-01' upper bound, exclusive)] of `table`'s month
    partitions (pg_copy's `<table>_pYYYYMM`), oldest first. DEFAULT and any
    partition not named that way are not listed."""
    rows = conn.execute_native(
        # `table` is one of db_dialect.PARTITIONED's own names, never input.
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        f"JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = '{table}'").fetchall()
    out = []
    for r in rows:
        name = r[0] if not hasattr(r, "keys") else r["relname"]
        m = _MONTH_PART.match(name or "")
        if not m or m.group("table") != table:
            continue
        y, mo = int(m.group("y")), int(m.group("m"))
        upper = pg_copy._add_months(date(y, mo, 1), 1)
        out.append((name, upper.isoformat()))
    return sorted(out, key=lambda x: x[1])


def drop_expired(conn, table, cutoff, max_drops=None) -> list:
    """Detach and drop `table`'s month partitions whose every row is older
    than `cutoff` (a 'YYYY-MM-DD[ HH:MM:SS]' stamp: a month goes when its
    upper bound is at or before it), oldest first, at most `max_drops`
    (PARTITION_DROPS_PER_PASS). Each in its own transaction, committed.
    Returns the names dropped. Postgres only — the caller checks."""
    import db_dialect
    if table not in db_dialect.PARTITIONED:
        return []
    limit = PARTITION_DROPS_PER_PASS if max_drops is None else int(max_drops)
    cutoff = str(cutoff)
    dropped = []
    for name, upper in month_partitions(conn, table):
        if len(dropped) >= limit or upper > cutoff:
            break
        try:
            conn.execute_native(f"ALTER TABLE {table} DETACH PARTITION {name}")
            conn.execute_native(f"DROP TABLE IF EXISTS {name}")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        dropped.append(name)
    return dropped


def run() -> dict:
    """The scheduled job: standard counts. Skipped on SQLite."""
    import db_dialect
    if db_dialect.backend() != "postgres":
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1, "hit_bound": False}
    import models
    conn = models.get_conn()
    try:
        res = ensure(conn)
    finally:
        conn.close()
    bad = len(res["default_rows"])
    return {"attempted": res["tables"], "ok": res["tables"] - bad, "failed": bad, "skipped": 0,
            "hit_bound": False, "created": res["created"], "default_rows": res["default_rows"]}
