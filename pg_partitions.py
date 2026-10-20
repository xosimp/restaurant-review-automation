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
        have = {name for name, _upper in month_partitions(conn, table)}
        for ddl in pg_copy.partition_ddl(table, first, last):
            name = ddl.split()[5]                       # CREATE TABLE IF NOT EXISTS <name> PARTITION OF
            if name in have:
                continue                                # never a lock for a month that exists
            # A new month locks the parent (and scans DEFAULT): a bounded
            # wait, committed alone, so no lock is held while the next one
            # waits (blind re-audit 10/20/26 R2-09).
            db_dialect.run_ddl_bounded(conn, lambda ddl=ddl: conn.execute_native(ddl))
            conn.commit()
            out["created"] += 1
        col = db_dialect.PARTITIONED[table]
        # Below 1971 is the copy's NULL sentinel, which belongs in DEFAULT.
        n = conn.execute_native(f"SELECT COUNT(*) FROM {table}_default WHERE {col} >= '1971-01-01'").fetchone()[0]
        if n:
            out["default_rows"][table] = int(n)
    conn.commit()
    return out


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
            # Each waits for its lock a bounded time (db_dialect.run_ddl_bounded):
            # queued behind an open reader, a DETACH held every later reader of
            # the table behind it for the whole lock_timeout (R2-09).
            db_dialect.run_ddl_bounded(conn, lambda: conn.execute_native(f"ALTER TABLE {table} DETACH PARTITION {name}"))
            db_dialect.run_ddl_bounded(conn, lambda: conn.execute_native(f"DROP TABLE IF EXISTS {name}"))
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        dropped.append(name)
    return dropped


# ── an existing table made partitioned (blind re-audit 10/20/26 R2-11) ──────
#
# The copy partitioned the seven fact tables of 10/19/26. The growing
# per-store tables added to db_dialect.PARTITIONED since (pos_payments,
# pos_payouts, pos_item_daily, menu_item_sales, dsr_metrics,
# bar_sales_nights) are partitioned by any later copy; on the live database
# they stay plain tables (the dialect reads each table's real key, so both
# shapes work) until this conversion is run, by the operator, inside the
# cutover runbook's write freeze (docs/ops/POSTGRES_CUTOVER.md → *Partitioning
# a growing table*; scripts/run_pg_copy.py --partition-tables). Which keys
# gain the date and why that changes no rule: db_dialect.PARTITIONED.

def _q(conn, sql, params=()):
    return conn.execute_native(sql, params).fetchall()


def convert_to_partitioned(conn, table, today=None, drop_old=False) -> dict:
    """Rebuild the plain table `table` (one of db_dialect.PARTITIONED) as a
    monthly-partitioned one with the same columns, defaults, keys (each
    unique key carrying the partition column), checks, foreign keys and
    indexes, and the same rows; swap it in under the old name in one
    transaction, the old table kept as `<table>__old` (dropped with
    `drop_old`). Identity columns become sequence defaults (no identity on a
    partitioned table), set past the highest value. Refuses a table with
    triggers or views depending on it, and one whose row count moved during
    the copy (run it in the write freeze). {"table", "rows", "partitions",
    "old"}; raises on any refusal, the database unchanged."""
    import db_dialect
    part = db_dialect.PARTITIONED.get(table)
    if not part:
        raise ValueError(f"{table} is not one of db_dialect.PARTITIONED")
    if not db_dialect.is_postgres(conn):
        raise ValueError("Postgres only")
    reg = _q(conn, "SELECT c.oid, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                   "WHERE n.nspname = current_schema() AND c.relname = %s", (table,))
    if not reg:
        raise ValueError(f"{table} does not exist")
    oid, kind = reg[0][0], reg[0][1]
    if kind == "p":
        return {"table": table, "rows": None, "partitions": 0, "old": None, "already": True}
    if _q(conn, "SELECT 1 FROM pg_trigger WHERE tgrelid = %s AND NOT tgisinternal", (oid,)):
        raise ValueError(f"{table} has triggers: convert it by hand")
    if _q(conn, "SELECT 1 FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid "
                "WHERE d.refobjid = %s AND d.classid = 'pg_rewrite'::regclass", (oid,)):
        raise ValueError(f"a view depends on {table}: convert it by hand")
    if _q(conn, "SELECT 1 FROM pg_constraint WHERE confrelid = %s AND conrelid <> %s", (oid, oid)):
        raise ValueError(f"another table's foreign key points at {table}: convert it by hand")
    new, old = f"{table}__part", f"{table}__old"
    if _q(conn, "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relname IN (%s, %s)", (new, old)):
        raise ValueError(f"{new} or {old} exists: a conversion was left half-done — look before rerunning")
    cols = _q(conn, "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                    "pg_get_expr(d.adbin, d.adrelid), a.attidentity FROM pg_attribute a "
                    "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                    "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum", (oid,))
    if part not in [c[0] for c in cols]:
        raise ValueError(f"{table} has no {part} column")
    today = today or date.today()
    try:
        defs, seqs = [], []
        for name, typ, notnull, dflt, ident in cols:
            d = f'"{name}" {typ}'
            if ident:
                seq = f"{table}_{name}_pseq"
                seqs.append((seq, name))
                conn.execute_native(f"CREATE SEQUENCE {seq}")
                d += f" DEFAULT nextval('{seq}')"
            elif dflt is not None:
                d += f" DEFAULT {dflt}"
            if notnull or ident:
                d += " NOT NULL"
            defs.append(d)
        conn.execute_native(f"CREATE TABLE {new} ({', '.join(defs)}) PARTITION BY RANGE ({part})")
        first = conn.execute_native(f"SELECT MIN({part}) FROM {table} WHERE {part} >= '1971'").fetchone()[0]
        first = date.fromisoformat(str(first)[:10]) if first else today
        bounds = pg_copy.month_bounds(date(first.year, first.month, 1), pg_copy._add_months(today, pg_copy.MONTHS_AHEAD))
        for a, b in bounds:
            conn.execute_native(f"CREATE TABLE {table}_p{a[:4]}{a[5:7]} PARTITION OF {new} "
                                f"FOR VALUES FROM ('{a}') TO ('{b}')")
        conn.execute_native(f"CREATE TABLE {table}_default PARTITION OF {new} DEFAULT")

        def _widen(defn):
            m = re.match(r"^(PRIMARY KEY|UNIQUE)\s*\(([^)]*)\)(.*)$", defn, re.S)
            if not m:
                return defn
            names = [c.strip() for c in m.group(2).split(",")]
            if part not in [c.strip('"') for c in names]:
                names.append(part)
            return f"{m.group(1)} ({', '.join(names)}){m.group(3)}"
        renames = []                    # (kind, final name) for the swap
        cons = _q(conn, "SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conrelid = %s AND contype IN ('p', 'u', 'c', 'f') ORDER BY contype DESC", (oid,))
        for cname, ctype, cdef in cons:
            conn.execute_native(f'ALTER TABLE {new} ADD CONSTRAINT "{cname}__p" {_widen(cdef)}')
            renames.append(("constraint", cname))
        idx = _q(conn, "SELECT c.relname, pg_get_indexdef(i.indexrelid), i.indisunique FROM pg_index i "
                       "JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = %s AND NOT EXISTS "
                       "(SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid AND k.conrelid = %s)",
                 (oid, oid))
        for iname, idef, unique in idx:
            m = re.match(r"^CREATE (UNIQUE )?INDEX (\S+) ON (?:\S+\.)?\S+ (USING .*)$", idef, re.S)
            if not m:
                raise ValueError(f"index {iname} unreadable: {idef}")
            body = m.group(3)
            if unique:
                mm = re.match(r"^(USING \w+ )\(([^)]*)\)(.*)$", body, re.S)
                if mm and part not in [c.strip().strip('"').split()[0] for c in mm.group(2).split(",")]:
                    body = f"{mm.group(1)}({mm.group(2)}, {part}){mm.group(3)}"
            conn.execute_native(f'CREATE {m.group(1) or ""}INDEX "{iname}__p" ON {new} {body}')
            renames.append(("index", iname))
        names = ", ".join(f'"{c[0]}"' for c in cols)
        conn.execute_native(f"INSERT INTO {new} ({names}) SELECT {names} FROM {table}")
        for seq, col in seqs:
            conn.execute_native(f"SELECT setval('{seq}', COALESCE((SELECT MAX(\"{col}\") FROM {new}), 0) + 1, false)")
        # The swap: no write lands between the count and the rename.
        conn.execute_native(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
        n_old = conn.execute_native(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        n_new = conn.execute_native(f"SELECT COUNT(*) FROM {new}").fetchone()[0]
        if int(n_old) != int(n_new):
            raise RuntimeError(f"{table}: {n_old} rows, {n_new} copied — writes during the copy (run it in the freeze)")
        conn.execute_native(f"ALTER TABLE {table} RENAME TO {old}")
        for kind_, name in renames:
            if kind_ == "constraint":
                conn.execute_native(f'ALTER TABLE {old} RENAME CONSTRAINT "{name}" TO "{name}__old"')
            else:
                conn.execute_native(f'ALTER INDEX "{name}" RENAME TO "{name}__old"')
        conn.execute_native(f"ALTER TABLE {new} RENAME TO {table}")
        for kind_, name in renames:
            if kind_ == "constraint":
                conn.execute_native(f'ALTER TABLE {table} RENAME CONSTRAINT "{name}__p" TO "{name}"')
            else:
                conn.execute_native(f'ALTER INDEX "{name}__p" RENAME TO "{name}"')
        for seq, col in seqs:
            conn.execute_native(f'ALTER SEQUENCE {seq} OWNED BY {table}."{col}"')
        if drop_old:
            conn.execute_native(f"DROP TABLE {old}")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        db_dialect.Catalog.forget()
        db_dialect._forget_translations()
    return {"table": table, "rows": int(n_new), "partitions": len(bounds) + 1, "old": None if drop_old else old}


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
