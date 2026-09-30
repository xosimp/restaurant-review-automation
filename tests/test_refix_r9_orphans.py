"""Memory re-audit fix round (9/29/26), R9 — "orphans" (INVENTORY-12, FORGET-16).

engagement_monthly was written and never read: an owner who had not signed
in for 90 days showed no console sign-in at all once login_history aged
out. The console now falls back to the month it holds. change_log.
value_as_of and learning_tombstones are documented as intentional records
(no production reader yet), not claimed as read.
"""
import sqlite3

import pytest

import admin_ops
import auth
import change_log
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, admin_ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    yield


def test_an_owner_past_the_raw_window_keeps_their_last_sign_in_month(db_path):
    rid = create_restaurant(Restaurant(name="Quiet Owner Grill", owner_email="q@x.test"))
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO users (id, restaurant_id, username, email, password_hash, role) "
              "VALUES (71, ?, 'q', 'q@x.test', 'x', 'client')", (rid,))
    c.execute("INSERT INTO engagement_monthly (user_id, restaurant_id, month, logins, login_days) "
              "VALUES (71, ?, '2026-03', 4, 3)", (rid,))
    c.execute("INSERT INTO engagement_monthly (user_id, restaurant_id, month, logins, login_days) "
              "VALUES (71, ?, '2026-05', 2, 2)", (rid,))
    c.commit()
    conn = models.get_conn()
    try:
        c2 = conn
        d = admin_ops._load_with(c2)
    finally:
        conn.close()
    got = d["owner_logins"][rid]
    assert got["from_month"] == "2026-05" and got["last_at"].startswith("2026-05-31")


def test_the_records_nothing_reads_say_so():
    import inspect
    # value_as_of has had a reader since 9/29/26 (Ask's read_target_history).
    assert "read_target_history" in change_log.__doc__
    src = open("DATABASE_SCHEMA.md").read()
    assert "`learning_tombstones.category` has no reader" in src and "intentional record" in src
