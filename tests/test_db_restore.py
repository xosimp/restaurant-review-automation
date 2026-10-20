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


# ── a restore prepared out of band (scale audit 10/19/26 OPS-3) ─────────────

def test_a_prepared_restore_is_checked_beforehand_and_only_renamed_at_boot(tmp_path, monkeypatch):
    import gzip
    import shutil
    live, snap = str(tmp_path / "reviews.db"), str(tmp_path / "snap.db")
    _db(live, restaurants=1)
    _db(snap, restaurants=4)
    models.checkpoint(snap)            # every commit in the file before it is gzipped
    gz = snap + ".gz"
    with open(snap, "rb") as src, gzip.open(gz, "wb") as out:
        shutil.copyfileobj(src, out)
    prepared = str(tmp_path / "restore_prepared.db")
    rec = db_restore.prepare(gz, prepared)
    assert rec["integrity"] == "ok" and rec["restaurants"] == 4 and os.path.exists(prepared + ".prepared.json")
    assert _count(live) == 1, "preparing never touches the live database"
    # The boot: no integrity check, no copy — a rename (the prepared file is gone).
    calls = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(db_restore.sqlite3, "connect",
                        lambda p, *a, **k: (calls.append(p), real_connect(p, *a, **k))[1])
    monkeypatch.setenv("RESTORE_FROM", prepared)
    out = db_restore.restore_if_requested(live)
    assert out["restored"] and out["prepared"] and _count(live) == 4
    assert not os.path.exists(prepared)
    # A later boot with the variable still set leaves the database alone.
    assert db_restore.restore_if_requested(live)["restored"] is False


def test_a_prepared_file_that_changed_since_is_refused(tmp_path, monkeypatch):
    live, snap = str(tmp_path / "reviews.db"), str(tmp_path / "snap.db")
    _db(live, restaurants=2)
    _db(snap, restaurants=3)
    prepared = str(tmp_path / "restore_prepared.db")
    db_restore.prepare(snap, prepared)
    with open(prepared, "ab") as f:
        f.write(b"\0" * 4096)
    monkeypatch.setenv("RESTORE_FROM", prepared)
    with pytest.raises(RuntimeError, match="changed after it was prepared"):
        db_restore.restore_if_requested(live)
    assert _count(live) == 2


def test_a_snapshot_that_fails_its_checks_is_never_prepared(tmp_path):
    snap = str(tmp_path / "empty.db")
    sqlite3.connect(snap).close()
    prepared = str(tmp_path / "restore_prepared.db")
    with pytest.raises(RuntimeError):
        db_restore.prepare(snap, prepared)
    assert not os.path.exists(prepared) and not os.path.exists(prepared + ".prepared.json")


def test_a_second_boot_does_not_restore_again_when_the_checkpoint_moved_the_snapshots_mtime(tmp_path, monkeypatch):
    # The restore checkpoints the snapshot before copying it; when that moved
    # its mtime past a second, the next boot looked for a marker nobody had
    # written and restored again over every write since (10/20/26).
    import time
    import models
    live, snap = str(tmp_path / "reviews.db"), str(tmp_path / "snap.db")
    _db(live, restaurants=1)
    _db(snap, restaurants=5)
    monkeypatch.setenv("RESTORE_FROM", snap)
    real = models.checkpoint

    def checkpoint(path=None, *a, **k):
        out = real(path, *a, **k)
        if path == snap:
            later = time.time() + 5
            os.utime(snap, (later, later))
        return out
    monkeypatch.setattr(models, "checkpoint", checkpoint)
    assert db_restore.restore_if_requested(live)["restored"]
    conn = sqlite3.connect(live)
    conn.execute("INSERT INTO restaurants (name, owner_email) VALUES ('New', 'n@x.test')")
    conn.commit()
    conn.close()
    assert db_restore.restore_if_requested(live)["restored"] is False
    assert _count(live) == 6
