"""Release-safety fix round 10/26/26, WS2 — the boot's DDL on a real Postgres
(A05-04, A05-15, A05-16; R10).

Runs when CAVNAR_TEST_PG_URL names a SCRATCH server (never production's);
each test gets a database of its own, dropped after."""
import contextlib
import io
import os
import sqlite3
import threading
import time
import uuid

import pytest

import db_dialect
import models

PG = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()
pytestmark = pytest.mark.skipif(not PG, reason="CAVNAR_TEST_PG_URL names no scratch Postgres")


def _url(name):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(PG)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
def scratch_db():
    import psycopg
    name = f"ws2_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    yield _url(name)
    db_dialect.close_idle(_url(name))
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _raw(url):
    import psycopg
    return psycopg.connect(url, autocommit=True, connect_timeout=10)


def _seed_table(url, rows=10):
    with _raw(url) as c:
        c.execute("CREATE TABLE t_ws2 (id BIGSERIAL PRIMARY KEY, a INTEGER, b TEXT)")
        c.execute("INSERT INTO t_ws2 (a, b) SELECT g, 'x' || g FROM generate_series(1, %s) g", (rows,))
        c.execute("CREATE INDEX idx_t_ws2_a ON t_ws2(a)")


@contextlib.contextmanager
def _table_locked(url, mode="ACCESS EXCLUSIVE"):
    """Another session holding `mode` on t_ws2 until the block ends."""
    import psycopg
    holder = psycopg.connect(url, autocommit=False, connect_timeout=10)
    holder.execute(f"LOCK TABLE t_ws2 IN {mode} MODE")
    try:
        yield
    finally:
        holder.rollback()
        holder.close()


def test_an_existing_index_is_answered_from_the_catalog_without_a_lock(scratch_db):
    _seed_table(scratch_db)
    conn = db_dialect.connect(scratch_db)
    try:
        with _table_locked(scratch_db):
            conn.execute_native("SET lock_timeout = '500ms'")
            started = time.monotonic()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_t_ws2_a ON t_ws2(a)")
            assert time.monotonic() - started < 0.5, "waited for the table's lock"
            pid = conn._raw.info.backend_pid
            with _raw(scratch_db) as probe:
                held = probe.execute("SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                                     "WHERE l.pid = %s AND c.relname = 't_ws2'", (pid,)).fetchone()[0]
            assert held == 0
        conn.rollback()
    finally:
        conn.close()


def test_a_new_index_waits_for_its_lock_bounded(scratch_db, monkeypatch):
    _seed_table(scratch_db)
    monkeypatch.setattr(db_dialect, "DDL_LOCK_TIMEOUT_MS", 200)
    monkeypatch.setattr(db_dialect, "DDL_LOCK_TRIES", 2)
    conn = db_dialect.connect(scratch_db)
    try:
        with _table_locked(scratch_db):
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                conn.execute("CREATE INDEX IF NOT EXISTS idx_t_ws2_b ON t_ws2(b)")
            assert time.monotonic() - started < 5
        conn.rollback()
        conn.execute("CREATE INDEX IF NOT EXISTS idx_t_ws2_b ON t_ws2(b)")       # unlocked: built
        conn.commit()
        assert conn.execute_native("SELECT to_regclass('idx_t_ws2_b') IS NOT NULL").fetchone()[0]
    finally:
        conn.close()


def test_a_new_index_past_its_build_limit_is_cancelled_and_recorded(scratch_db, monkeypatch):
    _seed_table(scratch_db, rows=300000)
    monkeypatch.setenv("PG_BOOT_INDEX_TIMEOUT_MS", "1")
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})
    conn = db_dialect.connect(scratch_db)
    try:
        assert models._apply_migration(conn, "CREATE INDEX IF NOT EXISTS idx_t_ws2_ab ON t_ws2(a, b)") is False
        conn.commit()
        assert "idx_t_ws2_ab" in models.BOOT_MISSING_INDEXES
        assert not conn.execute_native("SELECT to_regclass('idx_t_ws2_ab') IS NOT NULL").fetchone()[0]
        timeout = conn.execute_native("SELECT current_setting('statement_timeout')").fetchone()[0]
        assert timeout != "1ms", "the build limit leaked into the session"
    finally:
        conn.close()


def test_a_unique_index_over_duplicates_is_recorded_on_postgres(scratch_db, monkeypatch):
    _seed_table(scratch_db)
    with _raw(scratch_db) as c:
        c.execute("INSERT INTO t_ws2 (a, b) VALUES (1, 'dup'), (1, 'dup')")
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})
    conn = db_dialect.connect(scratch_db)
    try:
        sql = "CREATE UNIQUE INDEX IF NOT EXISTS uq_t_ws2_a ON t_ws2(a)"
        assert models._apply_migration(conn, sql) is False
        assert models.create_unique_index(conn, sql) is False
        conn.commit()
        assert models.BOOT_MISSING_INDEXES["uq_t_ws2_a"]["unique"] is True
        conn.execute("SELECT 1").fetchone()            # the transaction is still usable
    finally:
        conn.close()


def test_init_db_commits_each_ddl_so_no_lock_outlives_its_statement(scratch_db, tmp_path, monkeypatch):
    """A05-16: on Postgres a late statement in init_db's list finds no lock
    still held on restaurants by the boot's own session."""
    path = str(tmp_path / "pgboot.db")
    monkeypatch.setitem(models._PG_ROUTES, path, scratch_db)
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(path)                             # the schema exists
    seen = {}
    real = models._apply_migration

    def spy(conn, sql, also_tolerate=()):
        out = real(conn, sql, also_tolerate=also_tolerate)
        if "forecast_log ADD COLUMN explained_by" in sql and db_dialect.is_postgres(conn):
            pid = conn._raw.info.backend_pid
            with _raw(scratch_db) as probe:
                seen["locks"] = probe.execute(
                    "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                    "WHERE l.pid = %s AND c.relname = 'restaurants'", (pid,)).fetchone()[0]
        return out
    monkeypatch.setattr(models, "_apply_migration", spy)
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(path)
    assert seen.get("locks") == 0


def test_boot_schema_twice_on_postgres_and_the_second_rewrites_nothing(scratch_db, tmp_path, monkeypatch):
    path = str(tmp_path / "pgboot2.db")
    monkeypatch.setitem(models._PG_ROUTES, path, scratch_db)
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})
    with contextlib.redirect_stdout(io.StringIO()):
        models.boot_schema(path)
    names = {m["name"] for m in models.applied_migrations(path)}
    assert {"temp_password_scrub_a3", "recipe_pair_dedup", "missing_sales_null_ca3_f3"} <= names
    ran = []
    hook = lambda sql: ran.append(sql) if "temp_password=NULL" in sql or "DELETE FROM recipe_ingredients" in sql \
        else None
    db_dialect.WRITE_HOOKS.append(hook)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            models.boot_schema(path)
    finally:
        db_dialect.WRITE_HOOKS.remove(hook)
    assert ran == [] and models.BOOT_MISSING_INDEXES == {}


# ── scripts/build_index.py ───────────────────────────────────────────────────

def test_build_index_plans_then_builds_concurrently(scratch_db):
    from scripts import build_index
    _seed_table(scratch_db)
    out = []
    stmt = "CREATE INDEX IF NOT EXISTS idx_t_ws2_ba ON t_ws2(b, a)"
    assert build_index.run(stmt, scratch_db, apply=False, out=out.append) == 0
    assert any("CONCURRENTLY" in line for line in out)
    with _raw(scratch_db) as c:
        assert not c.execute("SELECT to_regclass('idx_t_ws2_ba') IS NOT NULL").fetchone()[0]
    out.clear()
    assert build_index.run(stmt, scratch_db, apply=True, out=out.append) == 0
    with _raw(scratch_db) as c:
        assert c.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_t_ws2_ba')"
                         ).fetchone()[0]
    assert build_index.run(stmt, scratch_db, apply=True, out=out.append) == 0          # already there


def test_build_index_on_a_partitioned_table_builds_each_partition_and_attaches(scratch_db):
    from scripts import build_index
    with _raw(scratch_db) as c:
        c.execute("CREATE TABLE p_ws2 (id BIGINT, business_date TEXT NOT NULL, v INTEGER) "
                  "PARTITION BY RANGE (business_date)")
        c.execute("CREATE TABLE p_ws2_2026_09 PARTITION OF p_ws2 FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')")
        c.execute("CREATE TABLE p_ws2_2026_10 PARTITION OF p_ws2 FOR VALUES FROM ('2026-10-01') TO ('2026-11-01')")
        c.execute("INSERT INTO p_ws2 VALUES (1, '2026-09-15', 1), (2, '2026-10-15', 2)")
    out = []
    assert build_index.run("CREATE INDEX IF NOT EXISTS idx_p_ws2_v ON p_ws2(v)", scratch_db, apply=True,
                           out=out.append) == 0
    with _raw(scratch_db) as c:
        assert c.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('idx_p_ws2_v')"
                         ).fetchone()[0]
        attached = c.execute("SELECT count(*) FROM pg_inherits WHERE inhparent = to_regclass('idx_p_ws2_v')"
                             ).fetchone()[0]
    assert attached == 2


def test_the_concurrent_build_never_blocks_writers(scratch_db):
    """While build_index runs, a writer's INSERT goes through."""
    from scripts import build_index
    _seed_table(scratch_db, rows=200000)
    done = {}

    def build():
        done["rc"] = build_index.run("CREATE INDEX IF NOT EXISTS idx_t_ws2_bb ON t_ws2(b)", scratch_db,
                                     apply=True, out=lambda *_: None)
    t = threading.Thread(target=build)
    t.start()
    time.sleep(0.05)
    with _raw(scratch_db) as w:
        w.execute("SET lock_timeout = '2s'")
        w.execute("INSERT INTO t_ws2 (a, b) VALUES (-1, 'during')")
    t.join(60)
    assert done.get("rc") == 0
