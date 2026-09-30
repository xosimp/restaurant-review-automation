"""Memory re-audit fix round (9/29/26), R9 — "prune_backup" (FORGET-10).

The nightly prune ran straight after the backup whether or not the backup
wrote its snapshot: with the volume full, rows past their windows were
deleted with no copy anywhere. Now it runs only after a good local snapshot.
"""
import sqlite3

import pytest

import models
import ops
import scheduler


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


@pytest.fixture
def paged(monkeypatch):
    got = []
    monkeypatch.setattr(ops, "page_operator", lambda key, *a, **k: got.append(key) or {"sent": True})
    return got


def _backup(db_path, local_ok, hours_ago=1):
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO backup_runs (started_at, finished_at, local_ok, offsite_ok) "
                  "VALUES (datetime('now', ?), datetime('now', ?), ?, 0)",
                  (f"-{hours_ago} hours", f"-{hours_ago} hours", local_ok))
        c.commit()
    finally:
        c.close()


def _old_alert(db_path):
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (1, 'x', datetime('now','-400 days'))")
        c.commit()
        return lambda: sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
    finally:
        c.close()


def test_no_backup_holds_the_prune_and_pages(db_path, paged, monkeypatch):
    called = []
    monkeypatch.setattr(ops, "prune_ledgers", lambda *a, **k: called.append(1) or {})
    out = scheduler.run_nightly_retention()
    assert not called and out["failed"] == 1 and "no backup" in out["held"]
    assert "retention_held" in paged
    assert ops.run_outcome(out)[0] != ops.RUN_OK


def test_a_failed_snapshot_tonight_holds_the_prune(db_path, paged):
    count = _old_alert(db_path)
    _backup(db_path, 1, hours_ago=25)
    _backup(db_path, 0, hours_ago=0)            # tonight's failed
    out = scheduler.run_nightly_retention()
    assert "did not write its snapshot" in out["held"] and count() == 1


def test_a_stale_snapshot_holds_the_prune(db_path, paged):
    _backup(db_path, 1, hours_ago=50)
    assert ops.prune_backup_gate()[0] is False


def test_a_good_snapshot_tonight_lets_the_prune_run(db_path, paged):
    count = _old_alert(db_path)
    _backup(db_path, 1, hours_ago=0)
    out = scheduler.run_nightly_retention()
    assert "held" not in out and count() == 0 and not paged
