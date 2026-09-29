"""Workstream F — /health (#3, #28, #94, #105, #108, #134).

health_snapshot is read-only, carries a keyword an uptime monitor can
assert, probes writes, judges disk against the database's size, reports
backup age, and fails when a volume that held client data has none — while
Railway's deploy check still gets its 200 for a degraded platform.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask, jsonify

import models
import ops
import status_manager as sm


@pytest.fixture
def db(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(sm, "DB_PATH", db_path)
    sm.seed_default_services()
    sm._schema_ok.clear()
    # No backup ledger in this worktree unless a test provides one.
    monkeypatch.setattr(ops, "backup_status", None, raising=False)
    return db_path


def _stamp_heartbeat(db_path, minutes_ago):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE service_status SET updated_at=datetime('now', ?) WHERE service_key='scheduler'",
                 (f"-{int(minutes_ago)} minutes",))
    conn.commit()
    conn.close()


def _body(payload):
    app = Flask(__name__)
    with app.app_context():
        return jsonify(**payload).get_data(as_text=True)


def _ok_disk(monkeypatch):
    monkeypatch.setattr(sm, "disk_state", lambda p=None: {"state": "ok", "free_mb": 9000, "pct_free": 80.0})


# ── read-only (#94, RELIABILITY-4) ───────────────────────────────────────────

def test_health_never_writes_or_pages_and_a_dead_scheduler_stays_stale(db, monkeypatch):
    _stamp_heartbeat(db, 60)
    _ok_disk(monkeypatch)
    monkeypatch.setattr(sm, "update_service_status", lambda *a, **k: pytest.fail("/health wrote the status page"))
    monkeypatch.setattr(ops, "alert_will", lambda *a, **k: pytest.fail("/health paged"))
    monkeypatch.setattr(ops, "claim_cooldown", lambda *a, **k: pytest.fail("/health claimed a cooldown"))
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    first, s1 = sm.health_snapshot(db)
    second, s2 = sm.health_snapshot(db)
    assert s1 == s2 == 200
    assert first["scheduler"] == second["scheduler"] == "stale", \
        "the check used to reset the heartbeat it read, so the second look said ok"
    assert second["scheduler_heartbeat_age_minutes"] >= 59
    assert "scheduler_stale" in second["problems"] and second["status"] == "degraded"


def test_an_unopenable_database_is_a_500_with_a_code_not_the_exception_text(db, monkeypatch):
    monkeypatch.setattr(models, "get_conn",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open /secret/vol/x.db")))
    payload, status = sm.health_snapshot(db)
    assert status == 500 and payload == {"status": "error", "error": "db_unavailable"}
    assert "/secret" not in json.dumps(payload)


# ── the keyword (#3) ─────────────────────────────────────────────────────────

def test_the_monitor_keyword_appears_exactly_when_the_platform_is_ok(db, monkeypatch):
    _stamp_heartbeat(db, 1)
    _ok_disk(monkeypatch)
    payload, status = sm.health_snapshot(db)
    assert status == 200 and payload["status"] == "ok" and payload["problems"] == []
    body = _body(payload)
    assert body.count('"status":"ok"') == 1

    monkeypatch.setattr(sm, "disk_state", lambda p=None: {"state": "low", "free_mb": 120, "pct_free": 4.0})
    payload, status = sm.health_snapshot(db)
    assert status == 200, "degraded stays a 200: the deploy check must not fail on it"
    assert payload["status"] == "degraded" and "disk_low" in payload["problems"]
    assert '"status":"ok"' not in _body(payload), "no nested key may carry the keyword"


def test_the_body_carries_backup_age_disk_and_the_heartbeat(db, monkeypatch):
    _stamp_heartbeat(db, 3)
    _ok_disk(monkeypatch)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(ops, "backup_status", lambda db_path=None: {
        "last_local_ok_at": (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S"),
        "last_offsite_ok_at": (now - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S"),
        "last_size_bytes": 7 * 1024 * 1024, "last_error": "secret detail", "offsite_configured": True},
        raising=False)
    payload, _ = sm.health_snapshot(db)
    assert payload["status"] == "ok"
    assert payload["backup"]["state"] == "ok" and 4.9 <= payload["backup"]["age_hours"] <= 5.1
    assert payload["backup"]["offsite"] == "ok"
    assert "secret detail" not in json.dumps(payload), "backup errors are for the admin card, not the public body"
    assert payload["disk"]["state"] == "ok"
    assert 2.5 <= payload["scheduler_heartbeat_age_minutes"] <= 3.5


def test_a_stale_backup_and_a_stale_offsite_copy_are_problems(db, monkeypatch):
    _stamp_heartbeat(db, 1)
    _ok_disk(monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(hours=40)).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(ops, "backup_status", lambda db_path=None: {
        "last_local_ok_at": old, "last_offsite_ok_at": old, "offsite_configured": True}, raising=False)
    payload, status = sm.health_snapshot(db)
    assert status == 200 and {"backup_stale", "offsite_backup_stale"} <= set(payload["problems"])
    # An unconfigured off-site copy is a configuration warning (the admin
    # card), not a problem that pages every minute.
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    monkeypatch.setattr(ops, "backup_status", lambda db_path=None: {
        "last_local_ok_at": fresh, "offsite_configured": False}, raising=False)
    payload, _ = sm.health_snapshot(db)
    assert payload["backup"]["offsite"] == "unconfigured" and payload["status"] == "ok"


def test_without_the_backup_ledger_the_backup_block_says_unknown(db, monkeypatch):
    _stamp_heartbeat(db, 1)
    _ok_disk(monkeypatch)
    payload, _ = sm.health_snapshot(db)
    assert payload["backup"]["state"] == "unknown" and payload["status"] == "ok"


# ── the write probe (#105) ───────────────────────────────────────────────────

def test_a_held_write_lock_reads_busy_and_degraded_not_down(db, monkeypatch):
    _stamp_heartbeat(db, 1)
    _ok_disk(monkeypatch)
    monkeypatch.setattr(sm, "WRITE_PROBE_TIMEOUT_MS", 100)
    holder = sqlite3.connect(db, timeout=1)
    holder.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        payload, status = sm.health_snapshot(db)
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    assert status == 200 and payload["database"]["write"] == "busy" and "db_busy" in payload["problems"]
    assert elapsed < 5, "the probe waits its own short timeout, never the 30-second one"


def test_the_write_probe_writes_nothing(db):
    before = sqlite3.connect(db).execute("PRAGMA data_version").fetchone()[0]
    watcher = sqlite3.connect(db)
    v0 = watcher.execute("PRAGMA data_version").fetchone()[0]
    assert sm.db_write_probe(db_path=db)["state"] == "ok"
    assert watcher.execute("PRAGMA data_version").fetchone()[0] == v0, "BEGIN IMMEDIATE + ROLLBACK committed nothing"
    watcher.close()
    assert before is not None


def test_a_database_that_cannot_take_a_write_is_a_500(db, monkeypatch):
    monkeypatch.setattr(sm, "db_write_probe",
                        lambda conn=None, db_path=None, timeout_ms=None: {"state": "failed", "ms": 1.0,
                                                                          "error": "attempt to write a readonly database"})
    payload, status = sm.health_snapshot(db)
    assert status == 500 and payload == {"status": "error", "error": "db_not_writable"}


def test_the_probe_classifies_readonly_as_failed_and_locked_as_busy():
    class _Conn:
        in_transaction = False

        def __init__(self, err):
            self.err = err

        def execute(self, sql):
            if sql.startswith("BEGIN"):
                raise sqlite3.OperationalError(self.err)

        def rollback(self):
            pass
    assert sm.db_write_probe(conn=_Conn("attempt to write a readonly database"))["state"] == "failed"
    assert sm.db_write_probe(conn=_Conn("database is locked"))["state"] == "busy"


def test_the_probe_never_touches_a_callers_open_transaction(db):
    conn = models.get_conn(db)
    try:
        conn.execute("INSERT INTO status_incidents (title) VALUES ('mine')")
        assert conn.in_transaction
        assert sm.db_write_probe(conn=conn)["state"] == "unknown"
        assert conn.in_transaction, "the caller's work must survive the probe"
        conn.rollback()
    finally:
        conn.close()


def test_a_database_not_in_wal_mode_is_degraded(db, monkeypatch):
    """get_conn asks for WAL on every connection, and SQLite answers with
    the mode it is actually in when it cannot switch (a read-only volume
    that cannot create the -wal file): that answer is what is reported."""
    _stamp_heartbeat(db, 1)
    _ok_disk(monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    def plain_conn(path=None, *a, **k):
        c = sqlite3.connect(path or db, timeout=30)
        c.row_factory = sqlite3.Row
        return c
    monkeypatch.setattr(models, "get_conn", plain_conn)
    payload, status = sm.health_snapshot(db)
    assert status == 200 and payload["database"]["journal"] == "delete" and "db_not_wal" in payload["problems"]


# ── disk against the database's size (#28) ──────────────────────────────────

def test_disk_thresholds_scale_with_the_database(db, monkeypatch):
    import shutil
    from collections import namedtuple
    Usage = namedtuple("Usage", "total used free")
    mb = 1024 * 1024
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(2_000 * mb, 1_700 * mb, 300 * mb))
    monkeypatch.setattr(sm, "_file_mb", lambda p: 7.0 if not p.endswith("-wal") else 0.5)
    small = sm.disk_state(db)
    assert small["state"] == "ok" and small["low_below_mb"] == 250, "a 7 MB database keeps the 250 MB floor"
    monkeypatch.setattr(sm, "_file_mb", lambda p: 100.0 if not p.endswith("-wal") else 0.0)
    big = sm.disk_state(db)
    assert big["state"] == "low" and big["low_below_mb"] == 400, "300 MB free cannot hold a 100 MB database's backup"
    monkeypatch.setattr(sm, "_file_mb", lambda p: 250.0 if not p.endswith("-wal") else 0.0)
    assert sm.disk_state(db)["state"] == "critical"
    assert {"db_mb", "wal_mb", "total_mb", "critical_below_mb"} <= set(big)


# ── schema check: cached, but never across a schema change (#134) ───────────

def test_a_clean_schema_is_not_rechecked_on_every_hit_but_a_change_is_seen(db, monkeypatch):
    calls = []
    real = sm._schema_gaps
    monkeypatch.setattr(sm, "_schema_gaps", lambda conn: calls.append(1) or real(conn))
    _stamp_heartbeat(db, 1)
    sm.health_snapshot(db)
    sm.health_snapshot(db)
    assert len(calls) == 1
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE forecast_log DROP COLUMN signed_error_pct")
    conn.commit()
    conn.close()
    payload, status = sm.health_snapshot(db)
    assert status == 500 and payload["error"] == "schema_mismatch" and payload["missing_count"] >= 1
    assert "forecast_log" not in json.dumps(payload), "names go to the log, not the public body"


def test_the_reference_schema_can_be_built_at_boot():
    assert sm.warm_reference_schema() >= 0
    assert "cols" in sm._reference_schema


# ── an emptied volume (#108) ─────────────────────────────────────────────────

def _client_restaurant(db_path):
    return models.create_restaurant(models.Restaurant(name="Real Client", owner_email="c@x.test"), db_path=db_path)


def test_the_volume_marker_turns_an_emptied_database_into_a_500(db, monkeypatch):
    vol = os.path.dirname(db)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", vol)
    _stamp_heartbeat(db, 1)
    _ok_disk(monkeypatch)
    assert sm.record_volume_marker(db) is None, "no client restaurant yet, nothing to record"
    rid = _client_restaurant(db)
    marker = sm.record_volume_marker(db)
    assert marker and marker["clients_seen"] == 1
    assert os.path.exists(os.path.join(vol, sm.VOLUME_MARKER))
    assert sm.health_snapshot(db)[1] == 200
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM restaurants WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    payload, status = sm.health_snapshot(db)
    assert status == 500 and payload["error"] == "data_missing"
    monkeypatch.setenv("ALLOW_EMPTY_DATABASE", "1")
    assert sm.health_snapshot(db)[1] == 200


def test_demo_and_internal_restaurants_do_not_count_as_client_data(db, monkeypatch):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(db))
    rid = models.create_restaurant(models.Restaurant(name="Demo", owner_email="d@x.test"), db_path=db)
    models.update_restaurant(rid, {"is_demo": 1}, db_path=db)
    rid2 = models.create_restaurant(models.Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db)
    models.update_restaurant(rid2, {"billing_status": "internal"}, db_path=db)
    assert sm.record_volume_marker(db) is None


def test_no_marker_is_written_off_the_volume(db, monkeypatch):
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    _client_restaurant(db)
    assert sm.record_volume_marker(db) is None
    assert not os.path.exists(os.path.join(os.path.dirname(db), sm.VOLUME_MARKER))


def test_boot_refuses_an_emptied_database_before_anything_is_built_over_it(tmp_path, monkeypatch):
    vol = tmp_path / "vol"
    vol.mkdir()
    db = str(vol / "reviews.db")
    monkeypatch.delenv("ALLOW_EMPTY_DATABASE", raising=False)
    sm.assert_platform_not_emptied(db)            # no marker: a brand-new volume boots
    (vol / sm.VOLUME_MARKER).write_text(json.dumps({"clients_seen": 3, "updated_at": "2026-09-28 10:00:00"}))
    with pytest.raises(RuntimeError, match="empty platform"):
        sm.assert_platform_not_emptied(db)
    assert not os.path.exists(db), "the check must not create the missing database"
    sqlite3.connect(db).execute("CREATE TABLE restaurants (id INTEGER PRIMARY KEY, is_demo INTEGER, "
                                "billing_status TEXT)").connection.close()
    with pytest.raises(RuntimeError, match="no client restaurants"):
        sm.assert_platform_not_emptied(db)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO restaurants (id, is_demo, billing_status) VALUES (5, 0, 'active')")
    c.commit()
    c.close()
    sm.assert_platform_not_emptied(db)
    monkeypatch.setenv("ALLOW_EMPTY_DATABASE", "1")
    os.remove(db)
    sm.assert_platform_not_emptied(db)


def test_railway_without_the_volume_refuses_to_boot(tmp_path):
    rw = {"RAILWAY_ENVIRONMENT": "production"}
    assert models.require_volume({}) is None, "a laptop or a test run is untouched"
    with pytest.raises(RuntimeError, match="no volume is attached"):
        models.require_volume(dict(rw))
    with pytest.raises(RuntimeError, match="does not exist"):
        models.require_volume(dict(rw, RAILWAY_VOLUME_MOUNT_PATH=str(tmp_path / "missing")))
    with pytest.raises(RuntimeError, match="not on the volume"):
        models.require_volume(dict(rw, RAILWAY_VOLUME_MOUNT_PATH=str(tmp_path)),
                              db_path=str(tmp_path / "elsewhere" / "reviews.db"))
    assert models.require_volume(dict(rw, RAILWAY_VOLUME_MOUNT_PATH=str(tmp_path)),
                                 db_path=str(tmp_path / "reviews.db")) == str(tmp_path)
    assert models.require_volume(dict(rw, ALLOW_NO_VOLUME="1")) is None
    for marker in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_NAME"):
        with pytest.raises(RuntimeError):
            models.require_volume({marker: "x"})


def test_the_web_boot_runs_the_guards_before_it_builds_anything():
    """Source order, because importing hosted_dashboard boots the app."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "hosted_dashboard.py"), encoding="utf-8").read()
    guard = src.index("_models_boot.require_volume()")
    emptied = src.index("_sm_boot.assert_platform_not_emptied()")
    first_init = src.index("    _init_db()")
    admin_seed = src.index("# ── Admin account seed")
    assert guard < emptied < first_init < admin_seed
    assert src.index("_logging_setup.configure()") < src.index("app = Flask(__name__)")
    assert "release=(os.getenv(\"RAILWAY_GIT_COMMIT_SHA\")" in src
