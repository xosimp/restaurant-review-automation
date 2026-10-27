"""Release-safety fix round 10/26/26, WS2 — the app runs without its volume
when VOLUME_OPTIONAL=1 (P3 "retire the app volume", code side only).

With no volume the guards' files live in object storage (marker_store) and
the backup's local snapshot is scratch; production with its volume attached
and no new variable is unchanged ("volume")."""
import sqlite3

import pytest

import config
import marker_store
import models

PG_ENV = {"RAILWAY_ENVIRONMENT": "production", "VOLUME_OPTIONAL": "1",
          "DATABASE_URL": "postgresql://u@db.internal:5432/railway",
          "BACKUP_S3_ENDPOINT": "https://r2.example.com", "BACKUP_S3_BUCKET": "cavnar-backups",
          "BACKUP_S3_ACCESS_KEY_ID": "k", "BACKUP_S3_SECRET_ACCESS_KEY": "s"}


# ── the boot guard ────────────────────────────────────────────────────────────

def test_a_railway_boot_with_no_volume_is_still_refused_by_default():
    with pytest.raises(RuntimeError, match="no volume is attached"):
        models.require_volume({"RAILWAY_ENVIRONMENT": "production"})


def test_volume_optional_boots_only_when_nothing_durable_needs_the_volume():
    assert models.require_volume(dict(PG_ENV)) is None
    with pytest.raises(RuntimeError, match="not Postgres"):
        models.require_volume(dict(PG_ENV, DATABASE_URL=""))
    with pytest.raises(RuntimeError, match="not Postgres"):
        models.require_volume(dict(PG_ENV, DB_BACKEND="sqlite"))
    no_s3 = {k: v for k, v in PG_ENV.items() if not k.startswith("BACKUP_S3_")}
    with pytest.raises(RuntimeError, match="no object storage"):
        models.require_volume(no_s3)
    assert models.require_volume(dict(no_s3, BLOB_S3_ENDPOINT="https://b", BLOB_S3_BUCKET="blobs",
                                      BLOB_S3_ACCESS_KEY_ID="k", BLOB_S3_SECRET_ACCESS_KEY="s")) is None
    with pytest.raises(RuntimeError, match="BLOB_STORE=local"):
        models.require_volume(dict(PG_ENV, BLOB_STORE="local"))


def test_a_mounted_volume_is_used_even_with_volume_optional(tmp_path):
    env = dict(PG_ENV, RAILWAY_VOLUME_MOUNT_PATH=str(tmp_path))
    assert models.require_volume(env, db_path=str(tmp_path / "reviews.db")) == str(tmp_path)


# ── marker_store ─────────────────────────────────────────────────────────────

@pytest.fixture
def bucket(monkeypatch):
    """A volume-less Railway service whose object storage is a dict."""
    import offsite_backup
    store, calls = {}, []
    for k, v in PG_ENV.items():
        if k != "DATABASE_URL":          # never set for a test process (CLAUDE.md)
            monkeypatch.setenv(k, v)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    for v in ("BLOB_S3_ENDPOINT", "BLOB_S3_BUCKET", "MARKER_S3_PREFIX"):
        monkeypatch.delenv(v, raising=False)

    def put(key, data, cfg, content_type=None):
        calls.append(("put", cfg["prefix"] + key))
        store[cfg["prefix"] + key] = bytes(data)

    def get(key, cfg):
        calls.append(("get", cfg["prefix"] + key))
        if cfg["prefix"] + key not in store:
            raise offsite_backup.OffsiteError("object store refused the blob: HTTP 404")
        return store[cfg["prefix"] + key]

    def delete(key, cfg):
        store.pop(cfg["prefix"] + key, None)
        return True
    monkeypatch.setattr(offsite_backup, "put_bytes", put)
    monkeypatch.setattr(offsite_backup, "get_bytes", get)
    monkeypatch.setattr(offsite_backup, "delete_object", delete)
    marker_store._exists_cache.clear()
    yield store, calls
    marker_store._exists_cache.clear()


def test_backend_is_volume_with_a_mount_object_without_one_local_off_railway(monkeypatch, tmp_path):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    monkeypatch.setenv("VOLUME_OPTIONAL", "1")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert marker_store.backend() == "volume"
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH")
    assert marker_store.backend() == "object" and marker_store.durable()
    monkeypatch.delenv("VOLUME_OPTIONAL")
    assert marker_store.backend() == "local"
    monkeypatch.setenv("VOLUME_OPTIONAL", "1")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT")
    assert marker_store.backend() == "local" and not marker_store.durable()


def test_object_markers_round_trip_under_their_own_prefix(bucket):
    store, calls = bucket
    assert marker_store.read("m.json") is None
    marker_store.write("m.json", {"a": 1})
    assert marker_store.read("m.json") == {"a": 1}
    assert "cavnar-markers/m.json" in store, "never under the backup prefix its lifecycle and checks read"
    assert marker_store.exists("m.json") is True
    n = len([c for c in calls if c[0] == "get"])
    assert marker_store.exists("m.json") is True and len([c for c in calls if c[0] == "get"]) == n, "cached"
    marker_store.remove("m.json")
    assert marker_store.exists("m.json") is False and marker_store.read("m.json") is None


# ── the guards on object storage ─────────────────────────────────────────────

def test_the_emptied_database_marker_lives_in_object_storage(bucket, db_path, monkeypatch):
    import status_manager
    store, _ = bucket
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = models.create_restaurant(models.Restaurant(name="Client", owner_email="c@x.test",
                                                     billing_status="active"), db_path=db_path)
    marker = status_manager.record_volume_marker(db_path)
    assert marker and marker["clients_seen"] >= 1 and "cavnar-markers/.cavnar-volume.json" in store
    assert status_manager.read_volume_marker(db_path)["clients_seen"] >= 1
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM restaurants WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    conn = models.get_conn(db_path)
    try:
        assert status_manager.platform_emptied(conn, db_path) is True
    finally:
        conn.close()


def test_the_restore_guard_freezes_and_confirms_through_object_storage(bucket, db_path, monkeypatch):
    import restore_guard
    store, _ = bucket
    monkeypatch.delenv("RESTORE_GUARD", raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    assert restore_guard.active() is True
    assert restore_guard.check_and_stamp(db_path)["state"] == "ok"
    assert restore_guard.check_and_stamp(db_path)["state"] == "ok"
    assert "cavnar-markers/.restore_epoch" in store
    # The database put back to an earlier tick (a point-in-time restore).
    conn = sqlite3.connect(db_path)
    db_id, seq = conn.execute("SELECT value FROM job_cursors WHERE key='restore_epoch'").fetchone()[0].rsplit(":", 1)
    conn.execute("UPDATE job_cursors SET value=? WHERE key='restore_epoch'", (f"{db_id}:{int(seq) - 1}",))
    conn.commit()
    conn.close()
    monkeypatch.setattr(restore_guard, "_page", lambda record: None)
    assert restore_guard.check_and_stamp(db_path)["state"] == "detected"
    assert "cavnar-markers/.restore_freeze" in store
    marker_store._exists_cache.clear()
    assert config.restore_frozen() is True and config.scheduling_allowed() is False
    restore_guard.confirm(actor="test", db_path=db_path)
    marker_store._exists_cache.clear()
    assert "cavnar-markers/.restore_freeze" not in store and config.restore_frozen() is False


def test_backups_use_scratch_space_without_a_volume(bucket, monkeypatch):
    import scheduler
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    path = scheduler.backup_dir_path()
    assert path.endswith("cavnar-backups") and "RAILWAY" not in path
    monkeypatch.setenv("BACKUP_DIR", "/explicit")
    assert scheduler.backup_dir_path() == "/explicit"
