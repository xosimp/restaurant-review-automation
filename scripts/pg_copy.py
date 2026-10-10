"""
scripts/pg_copy.py — copy the SQLite database into Postgres and prove it
(scheduler audit 10/10/26 RM-14; docs/ops/POSTGRES_CUTOVER.md is the
runbook this is a step of). Run by the operator, never by the app.

    python scripts/pg_copy.py --sqlite /data/reviews.db --database-url "$PG_URL" --report copy.json
    python scripts/pg_copy.py --sqlite snapshot.db --plan          # the schema it would build, no database
    python scripts/pg_copy.py --sqlite /data/reviews.db --database-url "$PG_URL" --verify-only
    python scripts/pg_copy.py --export out.db --database-url "$PG_URL"   # Postgres -> SQLite (rollback)

Copy a SNAPSHOT, never the live file: the nightly backup's newest local
snapshot (gunzipped), or `models.checkpoint()` then a copy, taken while
writes are paused (the runbook's maintenance window). The report says, per
table, the rows copied, the values that could not be stored as their
column's type (counted, never silently dropped) and the verification —
counts, and the integer primary key's min, max and sum — on both sides. Exit
status 0 only when every table verified.
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sqlite", help="the SQLite snapshot to copy from")
    ap.add_argument("--database-url", default=os.getenv("TARGET_DATABASE_URL") or os.getenv("DATABASE_URL"))
    ap.add_argument("--report", default="pg_copy_report.json")
    ap.add_argument("--plan", action="store_true", help="print the schema statements and stop")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--no-schema", action="store_true", help="the schema exists; copy rows only")
    ap.add_argument("--export", help="Postgres -> this SQLite file instead")
    a = ap.parse_args(argv)
    import pg_copy
    if a.export:
        rep = pg_copy.export_to_sqlite(a.export, url=a.database_url)
        print(json.dumps(rep, indent=2, default=str))
        return 0
    if not a.sqlite or not os.path.exists(a.sqlite):
        ap.error("--sqlite must name a snapshot file")
    if a.plan:
        src = sqlite3.connect(f"file:{a.sqlite}?mode=ro", uri=True)
        try:
            for s in pg_copy.schema_statements(src):
                print(s.rstrip().rstrip(";") + ";\n")
        finally:
            src.close()
        return 0
    if not a.database_url:
        ap.error("--database-url (or TARGET_DATABASE_URL) is required")
    import psycopg
    dst = psycopg.connect(a.database_url, autocommit=False, connect_timeout=10)
    try:
        if a.verify_only:
            src = sqlite3.connect(f"file:{a.sqlite}?mode=ro", uri=True)
            try:
                rep = {"verify": pg_copy.verify(src, dst)}
            finally:
                src.close()
        else:
            rep = pg_copy.copy_database(a.sqlite, dst, build_schema=not a.no_schema,
                                        on_table=lambda n, r: print(f"{n}: {r['rows']} rows"
                                                                    + (f", coerced {r['coerced']}" if r["coerced"]
                                                                       else "")))
    finally:
        dst.close()
    pg_copy.write_report(rep, a.report)
    ok = rep["verify"]["ok"]
    bad = [t for t, v in rep["verify"]["tables"].items() if not v["ok"]]
    print(f"verified: {ok}" + (f" — mismatched: {', '.join(bad)}" if bad else "") + f"; report {a.report}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
