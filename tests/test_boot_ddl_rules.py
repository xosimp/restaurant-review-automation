"""Release-safety fix round 10/26/26, WS2 — the boot DDL rules (A05-04,
A05-05, A05-12, A05-15; R9, R10).

1. The boot creates an index only on a table created in the same release:
   an index on a table already in migrations/schema_baseline.json is built
   before the deploy (scripts/build_index.py) and listed in
   migrations/prebuilt_indexes.txt.
2. Boot DDL is additive: no DROP TABLE / DROP COLUMN / RENAME outside the
   allow-list below (the SQLite-only table rebuilds, a no-op on Postgres).
3. A boot UNIQUE index over duplicate rows does not fail the boot: it is
   recorded and /health reports constraint_missing.
4. gunicorn's graceful window plus the queue worker's drain fit inside the
   deploy's documented drain."""
import ast
import contextlib
import importlib.util
import io
import os
import re
import sqlite3

import pytest

import models
import status_manager
from scripts import schema_baseline

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 1. indexes on existing tables are prebuilt ───────────────────────────────

def test_the_boot_builds_no_index_on_an_existing_table_unless_prebuilt():
    current = schema_baseline.snapshot()
    base = schema_baseline.load_baseline()
    bad = schema_baseline.violations(current, base, schema_baseline.load_prebuilt())
    assert not bad, (
        "the boot would build these indexes on tables production already has (a SHARE lock for the whole "
        f"build, inside the 120 s healthcheck): {bad}. Build each first with scripts/build_index.py "
        "(CONCURRENTLY) and add its name to migrations/prebuilt_indexes.txt.")


def test_the_rule_itself():
    base = {"tables": ["pos_tickets"], "indexes": {"idx_old": "pos_tickets"}}
    current = {"tables": ["pos_tickets", "brand_new"],
               "indexes": {"idx_old": "pos_tickets", "idx_new_on_old": "pos_tickets", "idx_on_new": "brand_new"}}
    assert schema_baseline.violations(current, base, set()) == [("idx_new_on_old", "pos_tickets")]
    assert schema_baseline.violations(current, base, {"idx_new_on_old"}) == []


def test_the_baseline_and_prebuilt_list_are_readable():
    base = schema_baseline.load_baseline()
    assert "restaurants" in base["tables"] and "users" in base["tables"]
    assert base["indexes"] and all(isinstance(t, str) for t in base["indexes"].values())
    assert isinstance(schema_baseline.load_prebuilt(), set)


# ── 2. boot DDL is additive ──────────────────────────────────────────────────

# Every DROP TABLE / DROP COLUMN / RENAME in application code, by file and the
# statement's start, with why it is allowed. Anything else fails: a dropped
# or renamed column breaks the build a rollback returns to (Release
# compatibility, ARCHITECTURE_MANIFEST.md) — drop one release after its last
# reader, from a runbook, never from the boot.
DESTRUCTIVE_ALLOWED = {
    ("models.py", "DROP TABLE IF EXISTS reviews_rekeyed"): "SQLite-only table rebuild (no-op on Postgres)",
    ("models.py", "DROP TABLE reviews"): "SQLite-only table rebuild, inside its one transaction",
    ("models.py", "ALTER TABLE reviews_rekeyed RENAME TO reviews"): "SQLite-only table rebuild",
    ("models.py", "DROP TABLE IF EXISTS reviews_platforms"): "SQLite-only table rebuild",
    ("models.py", "ALTER TABLE reviews_platforms RENAME TO reviews"): "SQLite-only table rebuild",
    ("models.py", "DROP TABLE IF EXISTS ask_memory_rekeyed"): "SQLite-only table rebuild",
    ("models.py", "DROP TABLE ask_memory"): "SQLite-only table rebuild, inside its one transaction",
    ("models.py", "ALTER TABLE ask_memory_rekeyed RENAME TO ask_memory"): "SQLite-only table rebuild",
    ("pg_partitions.py", "DROP TABLE"): "runbook only (scripts/run_pg_copy.py --partition-tables): "
                                        "the old table after the swap",
    ("pg_partitions.py", "ALTER TABLE"): "runbook only: the partition swap, in the write freeze",
    ("pg_partitions.py", "ALTER INDEX"): "runbook only: the partition swap",
}
_DESTRUCTIVE = re.compile(r"\b(DROP\s+TABLE|DROP\s+COLUMN|RENAME\s+(TO|COLUMN))\b", re.I)
_SKIP_DIRS = {"tests", "scripts", "ios", ".git", ".claude", "node_modules", "docs", "static", "templates", "public"}


def _app_sources():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _string_parts(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        yield "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)


def test_no_destructive_ddl_outside_the_allow_list():
    found = []
    for path in _app_sources():
        rel = os.path.relpath(path, ROOT)
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            for text in _string_parts(node):
                if not _DESTRUCTIVE.search(text) or not re.match(r"^\s*(DROP|ALTER)\b", text, re.I):
                    continue
                stmt = " ".join(text.split())
                if not any(rel == f and stmt.startswith(p) for (f, p) in DESTRUCTIVE_ALLOWED):
                    found.append((rel, stmt[:100]))
    assert not found, f"destructive DDL outside the allow-list (additive only — Release compatibility): {found}"


# ── 3. a unique index over duplicate rows does not fail the boot ─────────────

# (index, table, the duplicate pair) — one per guarded path: init_db's list,
# an init_* module through models.create_unique_index, and models' own.
_DUP_CASES = [
    ("uq_milestones", "INSERT INTO milestones (restaurant_id, kind, key, title) VALUES ({rid}, 'k', 'first', 't')"),
    ("uq_sched_obs_fact", "INSERT INTO schedule_observations (restaurant_id, kind, origin, phase, authority, "
                          "fact_key) VALUES ({rid}, 'k', 'o', 'p', 'a', 'f1')"),
    ("idx_people_live_key", "INSERT INTO people (restaurant_id, display_name, name_key) VALUES ({rid}, 'Ann', 'ann')"),
    ("idx_ask_memory_fact_author", "INSERT INTO ask_memory (restaurant_id, fact) VALUES ({rid}, 'same fact')"),
]


def _columns_ok(conn, sql):
    try:
        conn.execute("SAVEPOINT probe")
        conn.execute(sql)
        conn.execute("ROLLBACK TO probe")
        conn.execute("RELEASE probe")
        return True
    except sqlite3.Error:
        conn.execute("ROLLBACK TO probe")
        conn.execute("RELEASE probe")
        return False


def test_boot_over_duplicate_rows_succeeds_and_reports_the_missing_constraint(db_path, monkeypatch):
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})
    rid = models.create_restaurant(models.Restaurant(name="Dup", owner_email="d@x.test"), db_path=db_path)
    conn = sqlite3.connect(db_path)
    seeded = []
    for index, insert in _DUP_CASES:
        sql = insert.format(rid=rid)
        conn.execute(f"DROP INDEX IF EXISTS {index}")
        if not _columns_ok(conn, sql):
            pytest.fail(f"fixture for {index} no longer fits its table: {sql}")
        conn.execute(sql)
        conn.execute(sql)
        seeded.append(index)
    conn.commit()
    conn.close()
    with contextlib.redirect_stdout(io.StringIO()):
        models.boot_schema(db_path)                      # must not raise
    assert set(seeded) <= set(models.BOOT_MISSING_INDEXES), models.BOOT_MISSING_INDEXES
    assert all(models.BOOT_MISSING_INDEXES[i]["unique"] for i in seeded)
    status_manager._schema_ok.clear()
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200 and "constraint_missing" in payload["problems"]
    assert set(seeded) <= set(payload["indexes_missing"])


def test_a_clean_boot_reports_no_missing_index(db_path, monkeypatch):
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})
    with contextlib.redirect_stdout(io.StringIO()):
        models.boot_schema(db_path)
    assert models.BOOT_MISSING_INDEXES == {}


def test_an_index_past_its_build_limit_is_recorded_not_fatal(db_path, monkeypatch):
    """The Postgres build limit's error (db_dialect.INDEX_BUILD_TIMED_OUT)
    through the boot's runner: recorded as index_missing, the boot goes on."""
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})

    class _Conn:
        def execute(self, sql, *a):
            raise sqlite3.OperationalError("index build timed out after 60000 ms: canceling statement")
    assert models._apply_migration(_Conn(), "CREATE INDEX IF NOT EXISTS idx_big ON pos_tickets(x)") is False
    assert models.BOOT_MISSING_INDEXES["idx_big"]["unique"] is False
    with pytest.raises(sqlite3.OperationalError):
        models._apply_migration(_Conn(), "ALTER TABLE pos_tickets ADD COLUMN y TEXT")


# ── 4. the shutdown fits the deploy's drain (A05-12) ─────────────────────────

def _gunicorn_conf():
    spec = importlib.util.spec_from_file_location("gunicorn_conf_ws2", os.path.join(ROOT, "gunicorn.conf.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_graceful_timeout_and_queue_drain_fit_inside_the_documented_drain():
    conf = _gunicorn_conf()
    assert isinstance(conf.graceful_timeout, int) and conf.graceful_timeout > 0
    assert conf.graceful_timeout + conf._DRAIN_SECONDS <= conf.DEPLOY_DRAIN_SECONDS
    recovery = open(os.path.join(ROOT, "docs", "ops", "RECOVERY.md"), encoding="utf-8").read()
    assert f"RAILWAY_DEPLOYMENT_DRAINING_SECONDS={conf.DEPLOY_DRAIN_SECONDS}" in recovery
