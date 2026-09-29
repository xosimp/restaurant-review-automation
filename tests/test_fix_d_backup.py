"""Fix round D — the backup (#1, #2, #28, #102, decision 7).

An off-site copy on object storage (SigV4-signed, checksummed) and the
encrypted email as a second path; the local snapshot stays unredacted; the
off-site copies are scrubbed by ONE registry; the run FAILS when the snapshot
fails, when there is no room, or when no off-site copy was made; a
backup_runs row either way, read by ops.backup_status; the restore drill
refuses a stale snapshot and proves the object-storage copy.
"""
import hashlib
import io
import os
import re
import shutil
import sqlite3
from datetime import datetime

import pytest

import models
import offsite_backup
import ops
import scheduler
import status_manager
from models import Restaurant, create_restaurant

AK, SK = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
EMPTY = hashlib.sha256(b"").hexdigest()


# ── SigV4 against AWS's own documented examples ─────────────────────────────
# (Signature Version 4 "single chunk" examples: examplebucket, 20130524)

def _sig(auth):
    return auth.rsplit("Signature=", 1)[1]


def test_sigv4_get_object_vector():
    auth = offsite_backup.sign("GET", "/test.txt", {
        "Host": "examplebucket.s3.amazonaws.com", "Range": "bytes=0-9",
        "x-amz-content-sha256": EMPTY, "x-amz-date": "20130524T000000Z"},
        EMPTY, AK, SK, "us-east-1", "20130524T000000Z")
    assert _sig(auth) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in auth


def test_sigv4_put_object_vector_encodes_the_path():
    body_hash = hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
    auth = offsite_backup.sign("PUT", "/test$file.text", {
        "Host": "examplebucket.s3.amazonaws.com", "Date": "Fri, 24 May 2013 00:00:00 GMT",
        "x-amz-date": "20130524T000000Z", "x-amz-storage-class": "REDUCED_REDUNDANCY",
        "x-amz-content-sha256": body_hash}, body_hash, AK, SK, "us-east-1", "20130524T000000Z")
    assert _sig(auth) == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"


@pytest.mark.parametrize("query,expected", [
    ({"lifecycle": ""}, "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"),
    ({"max-keys": "2", "prefix": "J"}, "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"),
])
def test_sigv4_query_string_vectors(query, expected):
    auth = offsite_backup.sign("GET", "/", {
        "Host": "examplebucket.s3.amazonaws.com", "x-amz-date": "20130524T000000Z",
        "x-amz-content-sha256": EMPTY}, EMPTY, AK, SK, "us-east-1", "20130524T000000Z", query=query)
    assert _sig(auth) == expected


# ── the uploader ────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, text="", headers=None, body=b""):
        self.status_code, self.text, self.headers, self._body = status, text, headers or {}, body

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _s3_env(monkeypatch):
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "cavnar-backups")
    monkeypatch.setenv("BACKUP_S3_ACCESS_KEY_ID", "AKTEST")
    monkeypatch.setenv("BACKUP_S3_SECRET_ACCESS_KEY", "SKTEST")
    monkeypatch.delenv("BACKUP_S3_REGION", raising=False)


def test_upload_streams_the_file_with_its_checksum_and_a_timeout(tmp_path, monkeypatch):
    import requests
    _s3_env(monkeypatch)
    f = tmp_path / "x.enc"
    f.write_bytes(b"ciphertext" * 1000)
    seen = {}

    def put(url, data=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout, body_is_file=hasattr(data, "read"))
        return _Resp(200)
    monkeypatch.setattr(requests, "put", put)
    out = offsite_backup.upload_file(str(f), "cavnar_ai_backup_2026-09-29.db.enc")
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    assert out["sha256"] == digest and out["bytes"] == f.stat().st_size
    assert seen["url"] == ("https://acct.r2.cloudflarestorage.com/cavnar-backups/cavnar-backups/"
                           "cavnar_ai_backup_2026-09-29.db.enc")
    assert seen["headers"]["x-amz-content-sha256"] == digest
    assert seen["headers"]["x-amz-meta-sha256"] == digest
    assert "/auto/s3/aws4_request" in seen["headers"]["Authorization"]      # R2's region
    assert seen["body_is_file"], "the snapshot must stream, never be read whole into memory"
    assert seen["timeout"] and all(seen["timeout"])


def test_a_refused_upload_raises(tmp_path, monkeypatch):
    import requests
    _s3_env(monkeypatch)
    f = tmp_path / "x.enc"
    f.write_bytes(b"x")
    monkeypatch.setattr(requests, "put", lambda *a, **k: _Resp(403, "AccessDenied"))
    with pytest.raises(offsite_backup.OffsiteError):
        offsite_backup.upload_file(str(f), "k")


def test_http_endpoints_are_refused(monkeypatch):
    _s3_env(monkeypatch)
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "http://insecure.example")
    assert offsite_backup.s3_config() is None


# ── the scrub registry (#102) ───────────────────────────────────────────────

def _full_schema(path):
    from auth import init_auth
    from webhooks import init_webhooks
    from guest_marketing import init_guest_marketing
    from push import init_push
    from sales_audits import init_sales_audits
    models.init_db(path)
    init_auth(path)
    models.ensure_columns(path)
    init_webhooks(path)
    init_guest_marketing(path)
    init_push(path)
    init_sales_audits(path)


def test_every_credential_looking_column_is_scrubbed_or_kept_on_purpose(tmp_path):
    """Fails when a new token / secret / api_key / password column appears
    that nobody decided about — add it to SCRUB_COLUMNS or KEEP_COLUMNS."""
    import credentials
    path = str(tmp_path / "schema.db")
    _full_schema(path)
    conn = sqlite3.connect(path)
    try:
        cols = offsite_backup.credential_columns(conn)
        _wipe, null, unclassified = offsite_backup.scrub_plan(conn)
    finally:
        conn.close()
    assert cols, "the credential pattern matched nothing — the test is broken"
    assert unclassified == [], f"classify these in offsite_backup: {unclassified}"
    for f in credentials.FIELDS:
        if any(t == "restaurants" and c == f for t, c in cols):
            assert ("restaurants", f) in null, f
    for must in (("restaurants", "square_access_token"), ("restaurants", "clover_api_token"),
                 ("restaurants", "rpower_token"), ("restaurants", "reservation_api_key"),
                 ("webhooks", "secret")):
        assert must in null, must
    for t in ("staff_portal_tokens", "app_secrets", "sessions", "login_reports"):
        assert t in offsite_backup.SCRUB_TABLES


def test_the_keep_list_names_real_columns_with_a_reason(tmp_path):
    path = str(tmp_path / "schema.db")
    _full_schema(path)
    conn = sqlite3.connect(path)
    try:
        have = set(offsite_backup.credential_columns(conn))
    finally:
        conn.close()
    stale = [k for k in offsite_backup.KEEP_COLUMNS if k not in have and k[0] != "users"]
    assert not stale, f"KEEP_COLUMNS names columns that no longer exist: {stale}"
    assert all(offsite_backup.KEEP_COLUMNS.values())


def test_no_credential_value_survives_in_the_scrubbed_copy(tmp_path):
    path = str(tmp_path / "live.db")
    _full_schema(path)
    rid = create_restaurant(Restaurant(name="Scrub Co", owner_email="s@x.test"), db_path=path)
    conn = sqlite3.connect(path)
    secrets = {"square_access_token": "SQ-SECRET-1", "clover_api_token": "CL-SECRET-2",
               "rpower_token": "RP-SECRET-3", "reservation_api_key": "RES-SECRET-4",
               "backoffice_api_key": "BO-SECRET-5", "toast_client_secret": "TOAST-SECRET-6"}
    for col, val in secrets.items():
        conn.execute(f"UPDATE restaurants SET {col}=? WHERE id=?", (val, rid))
    conn.execute("INSERT INTO webhooks (restaurant_id, url, secret) VALUES (?, 'https://h.example', 'WH-SECRET-7')",
                 (rid,))
    conn.execute("INSERT INTO staff_portal_tokens (restaurant_id, token) VALUES (?, 'STAFF-SECRET-8')", (rid,))
    conn.execute("INSERT INTO app_secrets (name, value) VALUES ('unsub', 'APP-SECRET-9')")
    conn.commit()
    conn.close()
    copy = str(tmp_path / "copy.db")
    # A consistent copy of the WAL database (what backup_db takes), not a
    # file copy that can miss the -wal.
    src, dst = sqlite3.connect(path), sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    assert b"SQ-SECRET-1" in open(copy, "rb").read(), "the test copy must carry the secrets first"
    scheduler._redact_snapshot(copy)
    blob = open(copy, "rb").read()
    for val in list(secrets.values()) + ["WH-SECRET-7", "STAFF-SECRET-8", "APP-SECRET-9"]:
        assert val.encode() not in blob, val
    c = sqlite3.connect(copy)
    try:
        assert c.execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()[0] == "Scrub Co"
    finally:
        c.close()
    # and the LOCAL database is untouched — the snapshot is the restore artifact
    c = sqlite3.connect(path)
    try:
        assert c.execute("SELECT square_access_token FROM restaurants WHERE id=?", (rid,)).fetchone()[0] == "SQ-SECRET-1"
    finally:
        c.close()


# ── backup_db ───────────────────────────────────────────────────────────────

@pytest.fixture
def live(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    path = str(tmp_path / "live.db")
    models.init_db(path)
    models.ensure_columns(path)
    rid = create_restaurant(Restaurant(name="Backup Co", owner_email="b@x.test"), db_path=path)
    models.update_restaurant(rid, {"gmb_refresh_token": "tok-live"}, db_path=path)
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn", lambda p=None, *a, **k: real(path if p in (None, default) else p))
    monkeypatch.setattr(models, "DB_PATH", path)
    monkeypatch.setattr(status_manager, "DB_PATH", path)
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 9, 29, 2, 0))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    for v in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY_ID", "BACKUP_S3_SECRET_ACCESS_KEY",
              "HEALTHCHECK_PING_URL", "WILL_PHONE"):
        monkeypatch.delenv(v, raising=False)
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, **k: pages.append((key, lines)) or {"sent": True})
    return {"path": path, "tmp": tmp_path, "pages": pages}


def _key(monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", key)
    return key


def _runs(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM backup_runs ORDER BY id")]
    finally:
        c.close()


def test_no_offsite_copy_fails_the_run_pages_and_is_recorded(live, monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    with pytest.raises(scheduler.BackupFailed):
        scheduler.backup_db()
    runs = _runs(live["path"])
    assert runs[-1]["local_ok"] == 1 and runs[-1]["offsite_ok"] == 0
    assert live["pages"] and live["pages"][0][0] == "backup_failed"
    st = ops.backup_status()
    assert st["state"] == "no_offsite" and st["last_local_ok_at"] and not st["offsite_configured"]


def test_a_run_through_run_job_is_recorded_failed_not_ok(live, monkeypatch):
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    ops.run_job("backup_db", scheduler.backup_db)
    c = sqlite3.connect(live["path"])
    try:
        ok = c.execute("SELECT ok FROM job_runs WHERE job='backup_db' ORDER BY id DESC LIMIT 1").fetchone()[0]
    finally:
        c.close()
    assert ok == 0, "a backup with no off-site copy used to be a green job run"


def test_object_storage_copy_is_encrypted_scrubbed_and_checksummed(live, monkeypatch):
    import requests
    from cryptography.fernet import Fernet
    key = _key(monkeypatch)
    _s3_env(monkeypatch)
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "")
    uploaded = {}

    def put(url, data=None, headers=None, timeout=None):
        uploaded["body"] = data.read()
        uploaded["headers"] = headers
        return _Resp(200)
    monkeypatch.setattr(requests, "put", put)
    pings = []
    monkeypatch.setattr(ops, "ping_healthcheck", lambda status="ok": pings.append(status))
    out = scheduler.backup_db()
    assert out["failed"] == 0 and out["offsite"] and out["offsite"][0].startswith("s3://cavnar-backups/")
    assert pings == ["ok"]
    body = uploaded["body"]
    assert hashlib.sha256(body).hexdigest() == uploaded["headers"]["x-amz-content-sha256"]
    plain = b"".join(Fernet(key.encode()).decrypt(line) for line in body.splitlines() if line.strip())
    assert b"SQLite format 3" in plain[:16] and b"tok-live" not in plain
    runs = _runs(live["path"])
    assert runs[-1]["offsite_ok"] == 1 and runs[-1]["sha256"] == uploaded["headers"]["x-amz-content-sha256"]
    assert runs[-1]["db_bytes"] and runs[-1]["free_bytes"]
    # The local snapshot keeps its credentials: it is the restore artifact.
    snap = live["tmp"] / "backups" / "cavnar_ai_backup_2026-09-29.db"
    assert b"tok-live" in snap.read_bytes()
    assert ops.backup_status()["state"] == "ok"


def test_email_is_the_second_path_when_object_storage_fails(live, monkeypatch):
    import requests
    import emails
    _key(monkeypatch)
    _s3_env(monkeypatch)
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "re_test")
    monkeypatch.setattr(requests, "put", lambda *a, **k: _Resp(500, "boom"))
    mailed = []
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: mailed.append(payload)
                        or emails.SendResult(True, attempts=1))
    out = scheduler.backup_db()
    assert out["offsite"] == ["email"] and out["failed"] == 1
    assert mailed and "stripped" in mailed[0]["html"]
    run = _runs(live["path"])[-1]
    assert run["offsite_ok"] == 1 and "object storage" in (run["offsite_error"] or "")


def test_both_paths_failing_fails_the_run(live, monkeypatch):
    import requests
    import emails
    _key(monkeypatch)
    _s3_env(monkeypatch)
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "re_test")
    monkeypatch.setattr(requests, "put", lambda *a, **k: _Resp(500, "boom"))
    monkeypatch.setattr(emails, "deliver", lambda **k: emails.SendResult(False, error="down", attempts=2))
    with pytest.raises(scheduler.BackupFailed):
        scheduler.backup_db()
    assert ops.backup_status()["state"] == "no_offsite"


def test_not_enough_free_space_fails_before_writing(live, monkeypatch):
    import shutil as _sh
    _key(monkeypatch)
    monkeypatch.setattr(_sh, "disk_usage", lambda p: type("U", (), {"free": 10, "total": 10 ** 9, "used": 0})())
    with pytest.raises(scheduler.BackupFailed, match="free space"):
        scheduler.backup_db()
    assert not (live["tmp"] / "backups" / "cavnar_ai_backup_2026-09-29.db").exists()
    assert _runs(live["path"])[-1]["local_ok"] == 0


def test_backup_status_reads_stale_and_failed(live):
    ops.record_backup_run({"started_at": "2026-09-20 02:00:00", "finished_at": "2026-09-20 02:01:00",
                           "local_ok": 1, "offsite_ok": 1, "size_bytes": 10})
    assert ops.backup_status()["state"] == "stale"
    ops.record_backup_run({"local_ok": 0, "detail_json": '{"error": "disk I/O error"}'})
    st = ops.backup_status()
    assert st["state"] == "failed" and "disk I/O error" in st["summary"]


# ── the restore drill (#2) ──────────────────────────────────────────────────

def test_the_drill_fails_on_a_stale_snapshot(live, monkeypatch):
    import emails
    monkeypatch.setattr(emails, "deliver", lambda **k: True)
    bdir = live["tmp"] / "backups"
    bdir.mkdir(exist_ok=True)
    snap = bdir / "cavnar_ai_backup_2026-09-01.db"
    shutil.copyfile(live["path"], snap)
    old = datetime(2026, 9, 1).timestamp()
    os.utime(snap, (old, old))
    with pytest.raises(RuntimeError, match="restore drill failed"):
        scheduler.run_restore_drill()


def test_the_drill_proves_the_object_storage_copy(live, monkeypatch):
    import requests
    import emails
    key = _key(monkeypatch)
    _s3_env(monkeypatch)
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "")
    stored = {}

    def put(url, data=None, headers=None, timeout=None):
        stored["body"], stored["headers"] = data.read(), headers
        return _Resp(200)
    monkeypatch.setattr(requests, "put", put)
    scheduler.backup_db()
    monkeypatch.setattr(requests, "get", lambda url, headers=None, stream=None, timeout=None: _Resp(
        200, body=stored["body"], headers={"x-amz-meta-sha256": stored["headers"]["x-amz-meta-sha256"]}))
    monkeypatch.setattr(emails, "deliver", lambda **k: True)
    report = scheduler.run_restore_drill()
    assert report["ok"] and report["offsite"]["checked"] and report["offsite"]["ok"]
    assert "checksum ok" in report["offsite"]["summary"]
    # a copy that does not match its checksum fails the drill
    monkeypatch.setattr(requests, "get", lambda url, headers=None, stream=None, timeout=None: _Resp(
        200, body=stored["body"] + b"tampered\n"))
    with pytest.raises(RuntimeError):
        scheduler.run_restore_drill()
