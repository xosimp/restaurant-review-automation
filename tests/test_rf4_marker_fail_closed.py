"""marker_store reads fail CLOSED (re-audit B7 P3, 10/27/26).

With VOLUME_OPTIONAL=1 and no volume, the restore guard's epoch and freeze
live in object storage. An object-storage 5xx or timeout read as "no
marker": the freeze as lifted (sends resumed over a rewound database), the
epoch as missing (the tick wrote a fresh one over the evidence of a
restore). Now a read that fails is not "none":

  .restore_freeze   as last read in this process; never read -> frozen
  .restore_epoch    the tick neither compares nor writes it
  freeze record     unreadable -> the tick is held ("frozen")
"""
import sqlite3

import pytest

import config
import marker_store
import models

ENV = {"RAILWAY_ENVIRONMENT": "production", "VOLUME_OPTIONAL": "1",
       "BACKUP_S3_ENDPOINT": "https://r2.example.com", "BACKUP_S3_BUCKET": "cavnar-backups",
       "BACKUP_S3_ACCESS_KEY_ID": "k", "BACKUP_S3_SECRET_ACCESS_KEY": "s"}


@pytest.fixture
def bucket(monkeypatch):
    """A volume-less Railway service whose object storage is a dict that
    can be told to fail (state["down"]: every read answers HTTP 500)."""
    import offsite_backup
    store, state = {}, {"down": False}
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    for v in ("BLOB_S3_ENDPOINT", "BLOB_S3_BUCKET", "MARKER_S3_PREFIX", "DATABASE_URL"):
        monkeypatch.delenv(v, raising=False)

    def get(key, cfg):
        if state["down"]:
            raise offsite_backup.OffsiteError("object store refused the blob: HTTP 500")
        if cfg["prefix"] + key not in store:
            raise offsite_backup.OffsiteError("object store refused the blob: HTTP 404")
        return store[cfg["prefix"] + key]

    def put(key, data, cfg, content_type=None):
        store[cfg["prefix"] + key] = bytes(data)

    monkeypatch.setattr(offsite_backup, "get_bytes", get)
    monkeypatch.setattr(offsite_backup, "put_bytes", put)
    monkeypatch.setattr(offsite_backup, "delete_object", lambda key, cfg: store.pop(cfg["prefix"] + key, None))
    marker_store._exists_cache.clear()
    marker_store._last_known.clear()
    yield store, state
    marker_store._exists_cache.clear()
    marker_store._last_known.clear()


def test_a_storage_error_keeps_the_freeze_as_last_read(bucket):
    store, state = bucket
    marker_store.write(".restore_freeze", {"detected_at": "2026-10-27 08:00:00"})
    assert config.restore_frozen() is True
    marker_store._exists_cache.clear()
    state["down"] = True
    assert config.restore_frozen() is True, "an S3 blip lifted the restore freeze"
    # Lifted and read cleanly, then the store fails: still lifted (as last read).
    state["down"] = False
    store.clear()
    marker_store._exists_cache.clear()
    assert config.restore_frozen() is False
    state["down"] = True
    marker_store._exists_cache.clear()
    assert config.restore_frozen() is False


def test_a_freeze_never_read_and_unreadable_is_frozen(bucket):
    _store, state = bucket
    state["down"] = True
    assert config.restore_frozen() is True
    assert config.scheduling_allowed() is False


def test_a_strict_read_tells_unreadable_from_absent(bucket):
    _store, state = bucket
    assert marker_store.read(".restore_epoch", strict=True) is None, "a 404 is no marker"
    state["down"] = True
    assert marker_store.read(".restore_epoch") is None, "the plain read keeps its old answer"
    with pytest.raises(marker_store.MarkerUnreadable):
        marker_store.read(".restore_epoch", strict=True)


def test_the_restore_guard_never_writes_over_an_epoch_it_could_not_read(bucket, db_path, monkeypatch):
    import restore_guard
    store, state = bucket
    monkeypatch.delenv("RESTORE_GUARD", raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    assert restore_guard.check_and_stamp(db_path)["state"] == "ok"
    assert restore_guard.check_and_stamp(db_path)["state"] == "ok"
    before = store["cavnar-markers/.restore_epoch"]
    # The database is rewound AND the store blips while the guard reads.
    conn = sqlite3.connect(db_path)
    db_id, seq = conn.execute("SELECT value FROM job_cursors WHERE key='restore_epoch'").fetchone()[0].rsplit(":", 1)
    conn.execute("UPDATE job_cursors SET value=? WHERE key='restore_epoch'", (f"{db_id}:{int(seq) - 1}",))
    conn.commit()
    conn.close()
    state["down"] = True
    res = restore_guard.check_and_stamp(db_path)
    assert res["state"] == "frozen" and res.get("unreadable"), res
    assert store["cavnar-markers/.restore_epoch"] == before, "the epoch was overwritten unread"
    # The store back: the rewind is still caught.
    state["down"] = False
    monkeypatch.setattr(restore_guard, "_page", lambda record: None)
    assert restore_guard.check_and_stamp(db_path)["state"] == "detected"


def test_an_unreadable_epoch_alone_skips_the_stamp(bucket, db_path, monkeypatch):
    import restore_guard
    store, _state = bucket
    monkeypatch.delenv("RESTORE_GUARD", raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    assert restore_guard.check_and_stamp(db_path)["state"] == "ok"
    before = store["cavnar-markers/.restore_epoch"]
    import offsite_backup
    real_get = offsite_backup.get_bytes

    def epoch_down(key, cfg):
        if key == ".restore_epoch":
            raise offsite_backup.OffsiteError("timed out")
        return real_get(key, cfg)
    monkeypatch.setattr(offsite_backup, "get_bytes", epoch_down)
    res = restore_guard.check_and_stamp(db_path)
    assert res["state"] == "error" and store["cavnar-markers/.restore_epoch"] == before
