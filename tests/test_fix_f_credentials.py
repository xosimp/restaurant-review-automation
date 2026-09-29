"""Workstream F — client credentials at rest (#102).

The Square, Clover and RPOWER tokens and the reservation key were never
encrypted, even with CREDENTIAL_KEY set, and nothing re-saved the plaintext
rows written before a key existed.
"""
import sqlite3

import pytest

import credentials
import models
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


@pytest.fixture
def key(monkeypatch):
    from cryptography.fernet import Fernet
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_KEY", k)
    credentials._fernet_cache.clear()
    return k


def _raw(db_path, rid, col):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT {col} FROM restaurants WHERE id=?", (rid,)).fetchone()[0]
    finally:
        conn.close()


def test_every_credential_column_on_restaurants_is_encrypted_or_excluded_with_a_reason(db_path):
    """A rule that must hold everywhere, asserted against the schema: a new
    *_token / *_secret / *_api_key column that is in neither list fails
    here, not in a volume snapshot."""
    conn = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in conn.execute('PRAGMA table_info("restaurants")')]
    finally:
        conn.close()
    looks = [c for c in cols if credentials.CREDENTIAL_NAME.search(c)]
    uncovered = [c for c in looks if c not in credentials.FIELDS and c not in credentials.EXCLUDED]
    assert uncovered == [], f"add to credentials.FIELDS or EXCLUDED (with why): {uncovered}"
    for col in ("square_access_token", "clover_api_token", "rpower_token", "reservation_api_key"):
        assert col in credentials.FIELDS


@pytest.mark.parametrize("col", ["square_access_token", "clover_api_token", "rpower_token",
                                 "reservation_api_key"])
def test_pos_and_reservation_tokens_are_ciphertext_on_disk_and_plaintext_to_the_app(db_path, key, col):
    rid = create_restaurant(Restaurant(name="Tokens", owner_email="t@x.test"), db_path=db_path)
    update_restaurant(rid, {col: "tok-plain-123"}, db_path=db_path)
    assert _raw(db_path, rid, col).startswith(credentials.PREFIX)
    assert getattr(get_restaurant(rid, db_path=db_path), col) == "tok-plain-123"


def test_existing_plaintext_is_re_saved_encrypted_once_a_key_is_set(db_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_KEY", "")
    credentials._fernet_cache.clear()
    rid = create_restaurant(Restaurant(name="Legacy", owner_email="l@x.test"), db_path=db_path)
    update_restaurant(rid, {"rpower_token": "live-rpower", "gmb_refresh_token": "g-refresh"}, db_path=db_path)
    assert _raw(db_path, rid, "rpower_token") == "live-rpower", "no key: stored as given"
    assert credentials.encrypt_existing(db_path)["key"] == "missing"
    assert credentials.status(db_path)["plaintext"] == 2

    from cryptography.fernet import Fernet
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    credentials._fernet_cache.clear()
    out = credentials.encrypt_existing(db_path)
    assert out["encrypted"] == 2 and out["failed"] == 0
    assert _raw(db_path, rid, "rpower_token").startswith(credentials.PREFIX)
    assert get_restaurant(rid, db_path=db_path).rpower_token == "live-rpower"
    assert credentials.encrypt_existing(db_path)["encrypted"] == 0, "idempotent"
    st = credentials.status(db_path)
    assert st["key"] == "ok" and st["plaintext"] == 0 and st["encrypted"] == 2


def test_the_re_save_never_overwrites_a_value_that_changed_under_it(db_path, key, monkeypatch):
    rid = create_restaurant(Restaurant(name="Race", owner_email="r@x.test"), db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE restaurants SET clover_api_token='old-plain' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    real_encrypt = credentials.encrypt

    def racing_encrypt(value):
        # A save lands between the read and the write.
        c = sqlite3.connect(db_path)
        c.execute("UPDATE restaurants SET clover_api_token='newer-plain' WHERE id=?", (rid,))
        c.commit()
        c.close()
        return real_encrypt(value)
    monkeypatch.setattr(credentials, "encrypt", racing_encrypt)
    out = credentials.encrypt_existing(db_path)
    assert out["encrypted"] == 0 and out["skipped"] == 1
    assert _raw(db_path, rid, "clover_api_token") == "newer-plain"


def test_an_invalid_key_is_reported_and_nothing_is_touched(db_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_KEY", "not-a-fernet-key")
    credentials._fernet_cache.clear()
    assert credentials.key_state() == "invalid"
    assert credentials.encrypt_existing(db_path) == {"encrypted": 0, "skipped": 0, "failed": 0, "key": "invalid"}


def test_one_registry_names_every_credential_column_and_no_counters(db_path):
    from auth import init_auth
    init_auth(db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS webhooks (id INTEGER PRIMARY KEY, url TEXT, secret TEXT)")
        cols = set(credentials.credential_columns(conn))
    finally:
        conn.close()
    for col in credentials.FIELDS:
        if col != "toast_refresh_token":          # a field no schema has a column for
            assert ("restaurants", col) in cols
    assert ("webhooks", "secret") in cols
    assert ("users", "reset_token") in cols
    assert not any(c.endswith("tokens") for _t, c in cols), "token COUNTS are not credentials"
    assert not any(c.endswith("_expires") for _t, c in cols)
    assert not any(c.endswith("_hash") for _t, c in cols)
    # Columns that only mention a credential: a stamp, a flag, a score.
    for flag in ("password_changed_at", "must_reset_password", "password_strength"):
        assert ("users", flag) not in cols
