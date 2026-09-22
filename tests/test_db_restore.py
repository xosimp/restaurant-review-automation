"""Boot-time restore (DATA-2): the snapshot is swapped in before anything
opens the database, once, and never over a file that fails its checks."""
import os
import sqlite3

import pytest

import db_restore
import models
import scheduler


def _db(path, restaurants=3):
    models.init_db(path)
    conn = sqlite3.connect(path)
    for i in range(restaurants):
        conn.execute("INSERT INTO restaurants (name, owner_email) VALUES (?,?)", (f"R{i}", f"r{i}@x.test"))
    conn.commit()
    conn.close()


def _count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
    finally:
        conn.close()


def test_a_requested_restore_swaps_the_snapshot_in_once(tmp_path, monkeypatch):
    live, snap = str(tmp_path / "reviews.db"), str(tmp_path / "snap.db")
    _db(live, restaurants=1)
    _db(snap, restaurants=5)
    monkeypatch.setenv("RESTORE_FROM", snap)
    out = db_restore.restore_if_requested(live)
    assert out["restored"] and _count(live) == 5
    assert any(n.startswith("reviews.db.broken-") for n in os.listdir(tmp_path))
    # A second boot with the variable still set leaves newer writes alone.
    conn = sqlite3.connect(live)
    conn.execute("INSERT INTO restaurants (name, owner_email) VALUES ('New', 'n@x.test')")
    conn.commit()
    conn.close()
    assert db_restore.restore_if_requested(live)["restored"] is False
    assert _count(live) == 6


def test_a_snapshot_without_restaurants_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    live, snap = str(tmp_path / "reviews.db"), str(tmp_path / "empty.db")
    _db(live, restaurants=2)
    sqlite3.connect(snap).close()
    monkeypatch.setenv("RESTORE_FROM", snap)
    with pytest.raises(RuntimeError):
        db_restore.restore_if_requested(live)
    assert _count(live) == 2


def test_the_scheduler_is_held_while_a_restore_is_requested(monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    monkeypatch.setenv("RESTORE_FROM", "/app/data/backups/x.db")
    assert scheduler.scheduling_allowed() is False
    monkeypatch.delenv("RESTORE_FROM")
    assert scheduler.scheduling_allowed() is True
