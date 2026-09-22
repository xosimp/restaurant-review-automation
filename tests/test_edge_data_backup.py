"""Backup, restore and the healthcheck that promotes a deploy (DATA audit:
DATA-2, DATA-18, DATA-33, DATA-34, DATA-44).

What this protects: the 2am snapshot, the quarterly restore drill, the
documented restore in docs/ops/RECOVERY.md, and /health — the one check that
decides whether Railway promotes a new deploy. A backup that is corrupt but
named as the newest, an off-site copy built in memory, a drill that quietly
migrates production, a healthcheck that passes on an empty database, and a
restore procedure that can be clobbered by the process it runs beside are
each a data-loss event waiting for the worst possible moment.

Every test here runs from a scratch working directory with the database
paths pointed into tmp_path: several of these code paths resolve the
def-time default ./reviews.db, which must never be touched.

Tests without a marker pin behaviour that works today; xfail(strict=True)
tests assert the correct behaviour for a defect the audit confirmed.
"""
import glob
import os
import shutil
import sqlite3
from datetime import datetime

import pytest

import models
import ops
import scheduler
import status_manager
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 9, 22, 2, 0))
    import emails
    monkeypatch.setattr(emails, "deliver", lambda **kw: True)
    return cwd


def _live_db(path, monkeypatch, restaurants=1):
    """A fully migrated database at `path`, made the app's DB_PATH."""
    models.init_db(path)
    models.ensure_columns(path)
    for i in range(restaurants):
        rid = create_restaurant(Restaurant(name=f"Backup Co {i}", owner_email=f"b{i}@x.test"), db_path=path)
        models.update_restaurant(rid, {"gmb_refresh_token": f"tok-{i}"}, db_path=path)
    monkeypatch.setattr(models, "DB_PATH", path)
    monkeypatch.setattr(status_manager, "DB_PATH", path)
    return path


def _count(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


# ── a snapshot that fails its integrity check (DATA-34) ────────────────────

class _FailsIntegrityCheck(sqlite3.Connection):
    """A real connection (so the backup API accepts it) whose integrity check
    reports corruption — stands in for a torn page or a full disk mid-copy."""
    def execute(self, sql, *a):
        if "integrity_check" in sql:
            class _Row:
                def fetchone(self):
                    return ("*** in database main ***\nPage 7 is never used",)
            return _Row()
        return super().execute(sql, *a)


def test_a_good_snapshot_is_written_under_today_s_name(tmp_path, monkeypatch):
    _live_db(str(tmp_path / "live.db"), monkeypatch)
    bdir = tmp_path / "backups"
    monkeypatch.setenv("BACKUP_DIR", str(bdir))
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    scheduler.backup_db()
    snap = bdir / "cavnar_ai_backup_2026-09-22.db"
    assert snap.exists() and _count(str(snap), "SELECT COUNT(*) FROM restaurants") == 1


@pytest.mark.xfail(strict=True, reason="DATA-34: a snapshot that fails integrity_check is left on disk under "
                                       "today's name, so 'the newest snapshot' is the corrupt one")
def test_a_failed_snapshot_leaves_no_file_behind(tmp_path, monkeypatch):
    _live_db(str(tmp_path / "live.db"), monkeypatch)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    good = bdir / "cavnar_ai_backup_2026-09-21.db"
    shutil.copyfile(models.DB_PATH, good)                         # yesterday's good snapshot
    monkeypatch.setenv("BACKUP_DIR", str(bdir))
    real_connect = sqlite3.connect

    def connect(p, *a, **k):
        if str(p).startswith(str(bdir)):
            k["factory"] = _FailsIntegrityCheck
        return real_connect(p, *a, **k)
    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        scheduler.backup_db()
    finally:
        monkeypatch.setattr(sqlite3, "connect", real_connect)
    left = sorted(os.path.basename(p) for p in glob.glob(str(bdir / "cavnar_ai_backup_*.db")))
    assert left == ["cavnar_ai_backup_2026-09-21.db"], \
        f"a restore would pick {left[-1]} as the newest snapshot"


# ── the off-site copy (DATA-18) ────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-18: the emailed copy is built with f.read() of the whole database, "
                                       "then encrypted and base64'd in the web process (3-4x the DB in RAM)")
def test_the_offsite_copy_is_streamed_not_read_whole(tmp_path, monkeypatch):
    import resend
    from cryptography.fernet import Fernet
    _live_db(str(tmp_path / "live.db"), monkeypatch)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "re_test_key")
    sent = []
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda payload: sent.append(payload) or {"id": "x"}))

    unsized = []

    class _Tracked:
        def __init__(self, f, path):
            self._f, self._path = f, path

        def read(self, n=-1):
            if n is None or n < 0:
                unsized.append(self._path)
            return self._f.read(n)

        def __getattr__(self, name):
            return getattr(self._f, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def __iter__(self):
            return iter(self._f)

    def tracked_open(path, mode="r", *a, **k):
        f = open(path, mode, *a, **k)
        return _Tracked(f, str(path)) if "b" in mode and "r" in mode else f
    monkeypatch.setattr(scheduler, "open", tracked_open, raising=False)
    scheduler.backup_db()
    assert sent, "the email copy was not attempted — the test setup is wrong"
    assert not unsized, f"backup_db read the whole database into memory: {unsized}"


# ── the restore drill's reach (DATA-44) ────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-44: the drill's init_db(scratch) calls ensure_columns() with no "
                                       "argument, so it migrates the live database the drill promises not to touch")
def test_the_restore_drill_migrates_only_the_scratch_copy(tmp_path, monkeypatch, _scratch):
    import inspect
    default = inspect.signature(models.ensure_columns).parameters["db_path"].default
    if os.path.isabs(default):
        pytest.skip("default DB_PATH is absolute in this environment; cannot observe it safely")
    prod = str(_scratch / os.path.basename(default))                # what ./reviews.db resolves to
    _live_db(prod, monkeypatch)
    bdir = tmp_path / "backups"
    bdir.mkdir()
    shutil.copyfile(prod, bdir / "cavnar_ai_backup_2026-09-21.db")
    conn = sqlite3.connect(prod)
    conn.execute("ALTER TABLE schedule_shares DROP COLUMN expires_at")  # production predates a new column
    conn.commit()
    conn.close()
    monkeypatch.setenv("BACKUP_DIR", str(bdir))

    scheduler.run_restore_drill()
    conn = sqlite3.connect(prod)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_shares)")}
    conn.close()
    assert "expires_at" not in cols, "the 'read-only against production' drill ran a migration on production"


# ── /health on a database that is not really there (DATA-33) ──────────────

def test_health_is_200_on_a_migrated_database(tmp_path, monkeypatch):
    path = _live_db(str(tmp_path / "live.db"), monkeypatch)
    payload, status = status_manager.health_snapshot(path)
    assert status == 200 and payload["db"] == "ok"


def test_health_fails_on_a_database_with_no_restaurants_table(tmp_path, monkeypatch):
    empty = str(tmp_path / "empty.db")
    sqlite3.connect(empty).close()
    monkeypatch.setattr(models, "DB_PATH", empty)
    monkeypatch.setattr(status_manager, "DB_PATH", empty)
    _payload, status = status_manager.health_snapshot(empty)
    assert status == 500


def test_health_fails_on_a_database_missing_a_column_the_code_expects(tmp_path, monkeypatch):
    path = _live_db(str(tmp_path / "live.db"), monkeypatch)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE forecast_log DROP COLUMN signed_error_pct")
    conn.commit()
    conn.close()
    _payload, status = status_manager.health_snapshot(path)
    assert status == 500


# ── the documented restore (DATA-2) ────────────────────────────────────────

def test_a_connection_opened_between_the_move_and_the_copy_empties_the_restored_database(tmp_path, monkeypatch):
    """Why the runbook cannot swap files under a live process: the app's own
    get_conn and one scheduler lease call, landing in the gap between step
    4's `mv` and `cp`, leave a WAL that replays an empty schema over the
    restored file — and integrity_check still says ok. This pins the hazard
    the restore procedure has to design around."""
    live = str(tmp_path / "data" / "reviews.db")
    os.makedirs(os.path.dirname(live))
    _live_db(live, monkeypatch, restaurants=5)
    snap = str(tmp_path / "snapshot.db")
    src, dst = sqlite3.connect(live), sqlite3.connect(snap)
    src.backup(dst)
    src.close()
    dst.close()

    os.rename(live, live + ".broken-1")                               # step 4, line 1
    for suffix in ("-wal", "-shm"):                                   # step 4, line 2
        if os.path.exists(live + suffix):
            os.remove(live + suffix)
    inflight = models.get_conn(live)                                  # a scheduler tick, still running
    inflight.execute(ops._LEASE_SQL)
    inflight.execute("INSERT OR IGNORE INTO scheduler_lease (id, owner, heartbeat_at) VALUES (1, NULL, NULL)")
    inflight.commit()
    shutil.copyfile(snap, live)                                       # step 4, line 3
    inflight.close()

    conn = sqlite3.connect(live)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    assert "restaurants" not in tables and integrity == "ok"


def test_the_documented_restore_stops_every_writer_before_swapping_files():
    text = open(os.path.join(ROOT, "docs", "ops", "RECOVERY.md"), encoding="utf-8").read()
    start = text.index("### 4. Restore")
    section = text[start:text.index("### 5.", start)]
    before_swap = section[:section.index("mv ")].lower()
    guards = ("restore_from", "maintenance", "stop the service", "scale the service to 0", "scale to zero",
              "before any connection")
    assert any(g in before_swap for g in guards), \
        "step 4 swaps the database files while the web process and scheduler can still open them"
