"""Backup integrity/confidentiality and session-token storage.

Both come from the pre-launch audit:

* backup_db used shutil.copy2 on a live WAL database, which can miss commits
  still in the -wal file or capture a torn page — a backup that only reveals
  itself as broken during an emergency restore. It then base64'd that file
  into an email every night, carrying every restaurant's financials, guest
  phone numbers, and (because sessions were stored verbatim) a working bearer
  token for every logged-in owner.
* sessions.token held the raw bearer token, so any copy of the database was a
  ring of live master keys.
"""
import os
import sqlite3

import pytest

import auth
import models
from auth import create_session, create_user, get_session_user, hash_session_token
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)


def _restaurant(db_path, **kw):
    return create_restaurant(
        Restaurant(name=kw.pop("name", "Backup Co"), owner_email="b@x.test", **kw), db_path=db_path
    )


# ── session tokens are never stored in a replayable form ────────────────────

def test_session_token_is_not_stored_verbatim(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.test", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)

    conn = models.get_conn(db_path)
    stored = [r["token"] for r in conn.execute("SELECT token FROM sessions").fetchall()]
    conn.close()

    assert token not in stored, "the raw bearer token must never reach the database"
    assert hash_session_token(token) in stored
    # and the hash alone must not be usable as a bearer token
    assert get_session_user(hash_session_token(token), db_path=db_path) is None


def test_the_real_token_still_authenticates(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "bob", "bob@x.test", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    user = get_session_user(token, db_path=db_path)
    assert user and user["id"] == uid


def test_a_stolen_database_row_cannot_be_replayed(db_path):
    """The whole point: someone holding a dump of `sessions` has nothing to
    send in an Authorization header."""
    rid = _restaurant(db_path)
    uid = create_user(rid, "carol", "carol@x.test", "pw", db_path=db_path)
    create_session(uid, db_path=db_path)

    conn = models.get_conn(db_path)
    leaked = conn.execute("SELECT token FROM sessions").fetchone()["token"]
    conn.close()

    assert get_session_user(leaked, db_path=db_path) is None


# ── backup artifact ─────────────────────────────────────────────────────────

def test_snapshot_is_consistent_and_passes_integrity_check(tmp_path, db_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = _restaurant(db_path, name="Integrity Co")
    dest = str(tmp_path / "snap.db")
    scheduler._write_consistent_snapshot(dest)

    conn = sqlite3.connect(dest)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()[0] == "Integrity Co"
    conn.close()


def test_backup_carries_no_session_tokens_or_credentials(tmp_path, db_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = _restaurant(db_path, name="Redact Co")
    uid = create_user(rid, "dave", "dave@x.test", "pw", db_path=db_path)
    create_session(uid, db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET temp_password='plaintext-pw', gmb_refresh_token='goog-secret' WHERE id=?", (rid,))
    conn.commit()
    conn.close()

    dest = str(tmp_path / "snap.db")
    scheduler._write_consistent_snapshot(dest)
    scheduler._redact_snapshot(dest)

    bk = sqlite3.connect(dest)
    assert bk.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    row = bk.execute("SELECT temp_password, gmb_refresh_token, name FROM restaurants WHERE id=?", (rid,)).fetchone()
    assert row[0] is None and row[1] is None
    # business data itself must survive — this is still a usable backup
    assert row[2] == "Redact Co"
    bk.close()

    # and no credential string survives anywhere in the file's bytes
    with open(dest, "rb") as f:
        blob = f.read()
    assert b"plaintext-pw" not in blob
    assert b"goog-secret" not in blob


def test_email_copy_is_skipped_when_no_encryption_key(tmp_path, db_path, monkeypatch):
    """Fail closed: without a key the local snapshot still runs, but nothing
    leaves the server in the clear."""
    import scheduler
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_fake_key")
    _restaurant(db_path)

    sent = []
    import resend as _resend
    monkeypatch.setattr(_resend.Emails, "send", lambda *a, **k: sent.append(a) or {"id": "x"})

    scheduler.backup_db()

    assert sent == [], "no unencrypted backup may be emailed"
    assert os.listdir(str(tmp_path / "backups")), "local snapshot must still be written"
