"""
build_index.py — build one index on Postgres BEFORE the deploy that needs it,
CONCURRENTLY, with no statement timeout (release-safety audit 10/26/26,
A05-04 / R10).

A boot may create an index only on a table created in the same release
(ARCHITECTURE_MANIFEST.md → Release compatibility). For an index on a table
production already has: run this on production first (railway ssh, a quiet
hour), add the index name to migrations/prebuilt_indexes.txt, and ship the
same `CREATE INDEX IF NOT EXISTS` in the boot DDL — the boot finds the name
in the catalog and takes no lock.

    python scripts/build_index.py "CREATE INDEX IF NOT EXISTS idx_x ON t(a, b)"            # the plan
    python scripts/build_index.py "CREATE INDEX IF NOT EXISTS idx_x ON t(a, b)" --apply    # build it

The statement is the boot's own (SQLite dialect); it is translated exactly as
the boot would translate it, so the index built is the one the boot expects.
A monthly-partitioned table (db_dialect.PARTITIONED) has its index built on
each partition CONCURRENTLY, then created ON ONLY the parent and each
partition's attached — Postgres has no CONCURRENTLY on a partitioned parent.
A build that fails leaves an INVALID index behind: this says so, the boot
records it as missing (/health constraint_missing / index_missing — blind
re-audit 10/27/26 B5 P3), and --rebuild-invalid drops it CONCURRENTLY and
builds it again. Writes nothing but the index; sends nothing. Dry run unless
--apply.
"""
import argparse
import hashlib
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_STMT = re.compile(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_]\w*)\"?\s+ON\s+"
                   r"(?:ONLY\s+)?\"?([A-Za-z_]\w*)\"?\s*(.*)$", re.I | re.S)


def parse(sql):
    """(unique, name, table, rest after the table) of a CREATE INDEX."""
    m = _STMT.match(sql.strip().rstrip(";"))
    if not m:
        raise ValueError("not a CREATE INDEX statement")
    return bool(m.group(1)), m.group(2).lower(), m.group(3).lower(), m.group(4)


def _child_name(name, partition):
    full = f"{name}__{partition}"
    if len(full) <= 63:
        return full
    return f"{name[:40]}__{hashlib.blake2b(partition.encode(), digest_size=6).hexdigest()}"


def plan(sql, translate, partitions):
    """The statements to run, in order: [(sql, concurrent)]. `translate` maps
    the boot's statement to Postgres's; `partitions(table)` lists a
    partitioned table's partitions ([] for a plain table)."""
    unique, name, table, _rest = parse(sql)
    text = translate(sql).strip().rstrip(";")
    _u, _n, _t, rest = parse(text)
    head = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    parts = partitions(table)
    if not parts:
        return [(f"{head} CONCURRENTLY IF NOT EXISTS {name} ON {table} {rest}", True)]
    steps = [(f"{head} CONCURRENTLY IF NOT EXISTS {_child_name(name, p)} ON {p} {rest}", True) for p in parts]
    steps.append((f"{head} IF NOT EXISTS {name} ON ONLY {table} {rest}", False))
    steps += [(f"ALTER INDEX {name} ATTACH PARTITION {_child_name(name, p)}", False) for p in parts]
    return steps


def _invalid_drops(name, table, parts, index_valid):
    """The DROPs --rebuild-invalid runs first: the INVALID index itself on a
    plain table (CONCURRENTLY: no lock on writers); on a partitioned one each
    INVALID partition index — the parent's ON ONLY index is kept (dropping
    it would drop every attached partition index) and is valid again once
    every partition's index is attached."""
    if not parts:
        return [f"DROP INDEX CONCURRENTLY IF EXISTS {name}"]
    return [f"DROP INDEX CONCURRENTLY IF EXISTS {_child_name(name, p)}" for p in parts
            if index_valid(_child_name(name, p)) is False]


def run(sql, url, apply=False, out=print, rebuild_invalid=False):
    import psycopg
    sys.path.insert(0, _ROOT)
    import db_dialect
    unique, name, table, _ = parse(sql)
    pg = db_dialect.PgConnection(db_dialect._driver_connect(url))
    raw = psycopg.connect(url, autocommit=True, connect_timeout=10)
    try:
        def translate(s):
            return db_dialect._translate_cached(pg, s)[0]

        def partitions(t):
            rows = raw.execute("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                               "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = %s "
                               "AND p.relkind = 'p' ORDER BY c.relname", (t,)).fetchall()
            return [r[0] for r in rows]

        def index_valid(n):
            """True / False for an index called `n`, None when there is none."""
            row = raw.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)", (n,)).fetchone()
            return None if row is None else bool(row[0])

        drops = []
        exists = raw.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]
        if exists:
            valid = index_valid(name)
            if valid is None:
                out(f"{name} names a relation that is not an index — nothing built.")
                return 2
            if valid:
                out(f"{name} already exists (valid) — nothing to build.")
                return 0
            if not rebuild_invalid:
                out(f"{name} already exists (INVALID) — the boot records it as missing (/health "
                    "constraint_missing / index_missing). Run this again with --rebuild-invalid to replace it.")
                return 2
            drops = _invalid_drops(name, table, partitions(table), index_valid)
        steps = [(d, True) for d in drops] + plan(sql, translate, partitions)
        for stmt, concurrent in steps:
            out(("RUN  " if apply else "PLAN ") + stmt)
        if not apply:
            out("Dry run: add --apply to build it.")
            return 0
        # No statement timeout and no lock timeout: CONCURRENTLY waits for the
        # transactions before it and never blocks writers.
        raw.execute("SET statement_timeout = 0")
        raw.execute("SET lock_timeout = 0")
        for stmt, _concurrent in steps:
            raw.execute(stmt)
        valid = raw.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)",
                            (name,)).fetchone()
        if not (valid and valid[0]):
            out(f"{name} was built INVALID. Run this again with --rebuild-invalid.")
            return 2
        out(f"{name} built and valid. Add it to migrations/prebuilt_indexes.txt.")
        return 0
    finally:
        raw.close()
        try:
            pg._raw.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("statement", help="the boot's CREATE [UNIQUE] INDEX IF NOT EXISTS statement")
    ap.add_argument("--apply", action="store_true", help="build it (default: print the plan)")
    ap.add_argument("--database-url", default=None, help="default: DATABASE_URL")
    ap.add_argument("--rebuild-invalid", action="store_true",
                    help="an INVALID index of this name (a failed concurrent build) is dropped CONCURRENTLY and "
                         "built again")
    args = ap.parse_args(argv)
    url = args.database_url or os.getenv("DATABASE_URL") or ""
    if not url.startswith(("postgres://", "postgresql://")):
        print("No Postgres URL (DATABASE_URL or --database-url).")
        return 1
    return run(args.statement, url, apply=args.apply, rebuild_invalid=args.rebuild_invalid)


if __name__ == "__main__":
    sys.exit(main())
