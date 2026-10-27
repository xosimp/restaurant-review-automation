"""
schema_baseline.py — the boot schema's tables and indexes as production
already has them (release-safety audit 10/26/26, A05-04 / R10).

tests/test_boot_ddl_rules.py holds every boot to one rule: the boot may
create an index only on a table created in the same release. An index on a
table already in migrations/schema_baseline.json is built BEFORE the deploy,
CONCURRENTLY, with scripts/build_index.py, and named in
migrations/prebuilt_indexes.txt; the boot's own `CREATE INDEX IF NOT EXISTS`
then finds it and takes no lock (db_dialect answers it from the catalog).

    python scripts/schema_baseline.py                        # what differs from the baseline
    python scripts/schema_baseline.py --update               # rewrite it from this code
    python scripts/schema_baseline.py --ref origin/production         # against a release itself
    python scripts/schema_baseline.py --update --ref origin/production  # rewrite it FROM that release

The committed file is refreshed by hand and drifts (blind re-audit 10/27/26
B5 P3): --ref reads the baseline from a release's own code instead (`git
archive` of the ref, booted in a scratch process), so `--update --ref
<the promoted SHA>` after a promotion — run by the promote workflow or by
hand, in a commit of its own — needs nobody to remember which tables a
release added. The deploy gate's upgrade boot check
(scripts/upgrade_boot_check.py) holds the same rule against the previous
release's booted database on every push, so the committed file is only the
fallback for a local run. Reads and writes files only; boots scratch SQLite
files under a scrubbed environment.
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


_REF_CHILD = r"""
import contextlib, io, json, os, sqlite3, sys
sys.path.insert(0, os.getcwd())
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
db = os.path.join(os.environ["RAILWAY_VOLUME_MOUNT_PATH"], "reviews.db")
import models
with contextlib.redirect_stdout(io.StringIO()):
    if hasattr(models, "boot_schema"):
        models.boot_schema(db)
    else:                                  # a release from before boot_schema: its whole web boot
        import hosted_dashboard
conn = sqlite3.connect(db)
tables = sorted(r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
indexes = {r[0]: r[1] for r in conn.execute(
    "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
conn.close()
print("SNAPSHOT " + json.dumps({"tables": tables, "indexes": dict(sorted(indexes.items()))}))
sys.stdout.flush()
os._exit(0)
"""


def snapshot_ref(ref, root=_ROOT) -> dict:
    """snapshot() of a git ref's code: archived into a temporary tree and
    booted there, in a process of its own with no provider key and SQLite
    pinned. Raises RuntimeError when the ref or its boot fails."""
    import shutil
    import subprocess
    sys.path.insert(0, _ROOT)
    from scripts.boot_check import child_env
    from scripts.upgrade_boot_check import extract_tree, resolve_base_ref
    work = tempfile.mkdtemp(prefix="cavnar-baseline-ref-")
    try:
        sha = resolve_base_ref(root, ref)
        tree = extract_tree(root, sha, os.path.join(work, "tree"))
        volume = os.path.join(work, "volume")
        os.makedirs(volume)
        open(os.path.join(volume, "reviews.db"), "a").close()
        env = dict(child_env(volume), DB_BACKEND="sqlite", RUN_SCHEDULER_IN_WEB="0")
        r = subprocess.run([sys.executable, "-c", _REF_CHILD], cwd=tree, env=env, capture_output=True, text=True,
                           timeout=300)
        line = next((x for x in r.stdout.splitlines() if x.startswith("SNAPSHOT ")), None)
        if r.returncode != 0 or not line:
            raise RuntimeError(f"the schema of {ref} ({sha[:12]}) could not be built: "
                               f"{(r.stdout[-800:] + r.stderr[-1500:]).strip()}")
        return json.loads(line[len("SNAPSHOT "):])
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    ap.add_argument("--ref", default=None, help="a release (git ref) to take the baseline from, instead of the "
                                                "committed file (with --update: write the file from it)")
    args = ap.parse_args(argv)
    sys.path.insert(0, _ROOT)
    if args.update and args.ref:
        released = snapshot_ref(args.ref)
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(released, f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"baseline from {args.ref}: {len(released['tables'])} tables, {len(released['indexes'])} indexes "
              f"-> {BASELINE}")
        return 0
    base = snapshot_ref(args.ref) if args.ref else None
    from scripts.build_reference_schema import scrub_environment
    scrub_environment()
    current = snapshot()
    if args.update:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"baseline: {len(current['tables'])} tables, {len(current['indexes'])} indexes -> {BASELINE}")
        return 0
    base = base or load_baseline()
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
