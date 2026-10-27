"""
schema_baseline.py — the boot schema's tables and indexes as production
already has them (release-safety audit 10/26/26, A05-04 / R10).

tests/test_boot_ddl_rules.py holds every boot to one rule: the boot may
create an index only on a table created in the same release. An index on a
table already in migrations/schema_baseline.json is built BEFORE the deploy,
CONCURRENTLY, with scripts/build_index.py, and named in
migrations/prebuilt_indexes.txt; the boot's own `CREATE INDEX IF NOT EXISTS`
then finds it and takes no lock (db_dialect answers it from the catalog).

    python scripts/schema_baseline.py            # what differs from the baseline
    python scripts/schema_baseline.py --update   # rewrite it from this code

Run --update after each production promotion, in a commit of its own, so the
tables a release added count as existing from then on. Reads and writes
files only; boots a scratch SQLite file under a scrubbed environment.
"""
import argparse
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
BASELINE = os.path.join(_ROOT, "migrations", "schema_baseline.json")
PREBUILT = os.path.join(_ROOT, "migrations", "prebuilt_indexes.txt")


def snapshot(path=None) -> dict:
    """{"tables": [...], "indexes": {name: table}} of a scratch database built
    by models.boot_schema (SQLite; the automatic indexes left out)."""
    import contextlib
    import io
    import sqlite3
    import models
    with tempfile.TemporaryDirectory() as d:
        db = path or os.path.join(d, "baseline.db")
        with contextlib.redirect_stdout(io.StringIO()):
            models.boot_schema(db)
        conn = sqlite3.connect(db)
        try:
            tables = sorted(r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            indexes = {r[0]: r[1] for r in conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
        finally:
            conn.close()
    return {"tables": tables, "indexes": dict(sorted(indexes.items()))}


def load_baseline(path=BASELINE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_prebuilt(path=PREBUILT) -> set:
    """Index names built before their deploy (one per line; # comments)."""
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.split("#", 1)[0].strip() for line in f if line.split("#", 1)[0].strip()}


def violations(current: dict, baseline: dict, prebuilt: set) -> list:
    """Indexes the boot would build on a table production already has,
    without a prebuild: [(index, table)]."""
    known_tables = set(baseline.get("tables") or [])
    known_indexes = set((baseline.get("indexes") or {}).keys())
    return sorted((name, table) for name, table in current["indexes"].items()
                  if name not in known_indexes and table in known_tables and name not in prebuilt)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--update", action="store_true", help="rewrite migrations/schema_baseline.json")
    args = ap.parse_args(argv)
    sys.path.insert(0, _ROOT)
    from scripts.build_reference_schema import scrub_environment
    scrub_environment()
    current = snapshot()
    if args.update:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"baseline: {len(current['tables'])} tables, {len(current['indexes'])} indexes -> {BASELINE}")
        return 0
    base = load_baseline()
    new_tables = sorted(set(current["tables"]) - set(base["tables"]))
    new_indexes = sorted(set(current["indexes"]) - set(base["indexes"]))
    bad = violations(current, base, load_prebuilt())
    print(f"new tables: {new_tables or 'none'}")
    print(f"new indexes: {new_indexes or 'none'}")
    for name, table in bad:
        print(f"NOT ALLOWED AT BOOT: {name} on existing table {table} — build it first with "
              f"scripts/build_index.py and add it to migrations/prebuilt_indexes.txt")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
