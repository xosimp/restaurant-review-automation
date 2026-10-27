"""Blind re-audit 10/27/26 (RF6) — the boot on a real Postgres 16.

- One booter at a time: two boot_schema calls on one database at once both
  succeed, the second after the first (models.boot_ddl_lock); a boot that
  cannot get the lock in time fails instead of racing.
- An INVALID index (a failed CREATE INDEX CONCURRENTLY) is not counted as
  built: /health says constraint_missing, and scripts/build_index.py
  --rebuild-invalid replaces it.
- The emptied-database guard reads Postgres, not the volume's SQLite file.

Runs when CAVNAR_TEST_PG_URL names a SCRATCH server (never production's)."""
import contextlib
import io
import os
import threading
import time
import uuid

import pytest

import db_dialect
import models
import status_manager

PG = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()
pytestmark = pytest.mark.skipif(not PG, reason="CAVNAR_TEST_PG_URL names no scratch Postgres")


def _url(name):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(PG)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


def _raw(url, autocommit=True):
    import psycopg
    return psycopg.connect(url, autocommit=autocommit, connect_timeout=10)


@pytest.fixture
def scratch_db():
    name = f"rf6_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with _raw(PG) as c:
        c.execute(f"CREATE DATABASE {name}")
    yield _url(name)
    db_dialect.close_idle(_url(name))
    with _raw(PG) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


# ── one booter at a time ─────────────────────────────────────────────────────

def test_two_boots_at_once_both_succeed_one_after_the_other(scratch_db, tmp_path, monkeypatch):
    paths = [str(tmp_path / "web.db"), str(tmp_path / "scheduler.db")]   # two processes, one database
    for p in paths:
        monkeypatch.setitem(models._PG_ROUTES, p, scratch_db)
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})
    spans, errors = {}, []
    real = models._boot_schema_steps

    def timed(db_path=None, on_db_ready=None):
        start = time.monotonic()
        try:
            return real(db_path, on_db_ready)
        finally:
            spans[db_path] = (start, time.monotonic())

    monkeypatch.setattr(models, "_boot_schema_steps", timed)

    def boot(p):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                models.boot_schema(p)
        except Exception as e:                      # collected and asserted below
            errors.append((p, repr(e)))
    threads = [threading.Thread(target=boot, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    for t in threads:
        t.join(240)
    assert not errors, errors
    (a0, a1), (b0, b1) = sorted(spans.values())
    assert b0 >= a1 - 0.05, "the second boot's DDL overlapped the first's"
    assert models.BOOT_MISSING_INDEXES == {}


def test_a_boot_that_cannot_get_the_lock_in_time_fails(scratch_db, tmp_path, monkeypatch):
    path = str(tmp_path / "late.db")
    monkeypatch.setitem(models._PG_ROUTES, path, scratch_db)
    holder = _raw(scratch_db, autocommit=False)
    try:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (models.BOOT_LOCK_KEY,))
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="boot DDL lock"):
            with models.boot_ddl_lock(path, wait_seconds=1):
                pass
        assert time.monotonic() - started < 10
    finally:
        holder.rollback()
        holder.close()
    with models.boot_ddl_lock(path, wait_seconds=1) as held:      # released: taken at once
        assert held is True


def test_sqlite_takes_no_boot_lock(tmp_path):
    with models.boot_ddl_lock(str(tmp_path / "plain.db")) as held:
        assert held is False


# ── an INVALID index is not built ────────────────────────────────────────────

def _invalid_unique_index(url):
    with _raw(url) as c:
        c.execute("CREATE TABLE t_rf6 (id BIGSERIAL PRIMARY KEY, a INTEGER, b TEXT)")
        c.execute("INSERT INTO t_rf6 (a, b) VALUES (1, 'x'), (1, 'y'), (2, 'z')")
        import psycopg
        with pytest.raises(psycopg.errors.UniqueViolation):     # the duplicates leave it INVALID
            c.execute("CREATE UNIQUE INDEX CONCURRENTLY uq_t_rf6_a ON t_rf6(a)")
        assert c.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('uq_t_rf6_a')"
                         ).fetchone()[0] is False


def test_an_invalid_unique_index_is_recorded_not_counted_as_built(scratch_db, monkeypatch):
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})
    _invalid_unique_index(scratch_db)
    conn = db_dialect.connect(scratch_db)
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_t_rf6_a ON t_rf6(a)")      # the boot's statement
        conn.execute("CREATE INDEX IF NOT EXISTS t_rf6_pkey ON t_rf6(id)")            # a valid one: untouched
        conn.commit()
    finally:
        conn.close()
    assert set(models.BOOT_MISSING_INDEXES) == {"uq_t_rf6_a"}
    assert models.BOOT_MISSING_INDEXES["uq_t_rf6_a"]["unique"] is True
    assert "INVALID" in models.BOOT_MISSING_INDEXES["uq_t_rf6_a"]["error"]


def test_health_says_constraint_missing_for_an_invalid_index(db_path, monkeypatch):
    monkeypatch.setattr(models, "BOOT_MISSING_INDEXES", {})
    url = models._PG_ROUTES[db_path]
    _invalid_unique_index(url)
    conn = models.get_conn(db_path)
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_t_rf6_a ON t_rf6(a)")
        conn.commit()
    finally:
        conn.close()
    status_manager._schema_ok.clear()
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200 and "constraint_missing" in payload["problems"]
    assert "uq_t_rf6_a" in payload["indexes_missing"]


def test_build_index_rebuilds_an_invalid_index(scratch_db):
    from scripts import build_index
    _invalid_unique_index(scratch_db)
    stmt = "CREATE UNIQUE INDEX IF NOT EXISTS uq_t_rf6_a ON t_rf6(a)"
    out = []
    assert build_index.run(stmt, scratch_db, apply=True, out=out.append) == 2
    assert any("INVALID" in line and "--rebuild-invalid" in line for line in out)
    with _raw(scratch_db) as c:
        c.execute("DELETE FROM t_rf6 WHERE b = 'y'")                   # the dedup that ships with it
    out.clear()
    assert build_index.run(stmt, scratch_db, apply=False, out=out.append, rebuild_invalid=True) == 0
    assert out[0].startswith("PLAN DROP INDEX CONCURRENTLY IF EXISTS uq_t_rf6_a")
    assert build_index.run(stmt, scratch_db, apply=True, out=out.append, rebuild_invalid=True) == 0
    with _raw(scratch_db) as c:
        assert c.execute("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('uq_t_rf6_a')"
                         ).fetchone()[0] is True


# ── the emptied-database guard reads Postgres ───────────────────────────────

def test_the_emptied_guard_reads_postgres_not_the_volume_file(db_path, monkeypatch):
    vol = os.path.dirname(db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", vol)
    monkeypatch.delenv("ALLOW_EMPTY_DATABASE", raising=False)
    rid = models.create_restaurant(models.Restaurant(name="Client", owner_email="c@x.test",
                                                     billing_status="active"), db_path=db_path)
    marker = status_manager.record_volume_marker()
    assert marker and marker["backend"] == "postgres" and marker["clients_seen"] >= 1
    os.remove(db_path)                            # the volume's SQLite file is gone, or frozen and stale
    status_manager.assert_platform_not_emptied()  # Postgres holds the clients: boots
    conn = models.get_conn(db_path)
    try:
        conn.execute("DELETE FROM restaurants WHERE id=?", (rid,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="no client restaurants"):
        status_manager.assert_platform_not_emptied()
