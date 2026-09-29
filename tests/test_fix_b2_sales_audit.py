"""Fix round B2 — the sales-audit tool (#66, #162).

What these protect:
  * Share tokens are stored hashed with an expiry; the link still resolves
    by its hash until it expires or is revoked; links created before the
    change are migrated in place and keep working.
  * The admin can re-copy a live link only when the token can be decrypted
    (CREDENTIAL_KEY set) — the database never holds it in plaintext.
  * Linking an audit to the client it became is recorded, and the client's
    linked audits are readable; the audit tool has the picker.
  * audit_list.html quotes names with jsq, so an apostrophe can't break
    Delete.
"""
import json
import os

import pytest

import auth
import models
import sales_audits as store
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(store, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    store.init_sales_audits(db_path=db_path)


def _rows(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _generated_audit():
    aid = store.create_audit(answers={"restaurant_name": "Prospect Grill"})
    store.store_results(aid, {"health": {"score": 70}}, mark_generated=True)
    return aid


def test_a_share_token_is_stored_hashed_with_an_expiry_and_still_resolves(db_path, monkeypatch):
    monkeypatch.delenv("CREDENTIAL_KEY", raising=False)
    aid = _generated_audit()
    token = store.create_share(aid)
    row = _rows(db_path, "SELECT token, token_enc, token_hint, expires_at FROM sales_audit_shares")[0]
    assert token not in json.dumps(row), "the token is nowhere in the row"
    assert row["token"].startswith("sha256:") and row["token_enc"] is None and row["token_hint"] == token[-4:]
    assert row["expires_at"] > store._now()
    assert store.resolve_share(token)["id"] == aid
    assert store.resolve_share(row["token"]) is None, "the stored hash is not itself a link"
    share = store.active_share(aid)
    assert share["token"] is None and share["token_hint"] == token[-4:], "shown once, at creation, without a key"


def test_an_expired_or_revoked_link_resolves_to_nothing(db_path):
    aid = _generated_audit()
    token = store.create_share(aid)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE sales_audit_shares SET expires_at='2020-01-01 00:00:00'")
    conn.commit(); conn.close()
    assert store.resolve_share(token) is None and store.active_share(aid) is None
    token2 = store.create_share(aid)
    store.revoke_shares(aid)
    assert store.resolve_share(token2) is None


def test_with_a_credential_key_the_admin_can_copy_a_live_link_again(db_path, monkeypatch):
    from cryptography.fernet import Fernet
    import credentials
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    credentials._fernet_cache.clear()
    aid = _generated_audit()
    token = store.create_share(aid)
    row = _rows(db_path, "SELECT token, token_enc FROM sales_audit_shares")[0]
    assert token not in row["token"] + (row["token_enc"] or "")
    assert store.active_share(aid)["token"] == token


def test_links_stored_in_plaintext_are_migrated_and_keep_working(db_path):
    aid = _generated_audit()
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO sales_audit_shares (audit_id, token, created_at) VALUES (?, 'legacy-token-abcd', "
                 "datetime('now', '-200 days'))", (aid,))
    conn.commit(); conn.close()
    store.init_sales_audits(db_path=db_path)          # the boot migration
    row = _rows(db_path, "SELECT token, expires_at, token_hint FROM sales_audit_shares")[0]
    assert row["token"].startswith("sha256:") and row["token_hint"] == "abcd"
    assert row["expires_at"] >= store._stamp_plus_days(store.LEGACY_SHARE_GRACE_DAYS - 1), \
        "an old link gets the grace period, not an instant expiry"
    assert store.resolve_share("legacy-token-abcd")["id"] == aid
    store.init_sales_audits(db_path=db_path)          # idempotent: not hashed twice
    assert store.resolve_share("legacy-token-abcd")["id"] == aid


def test_linking_an_audit_is_recorded_and_the_client_lists_its_audits(db_path, monkeypatch):
    from flask import Flask
    import admin_routes
    from csrf import csrf_protect
    from sales_audit_routes import audit_bp
    if not getattr(audit_bp, "_csrf_wired", False):        # as tests/test_sales_audit.py wires it
        csrf_protect(audit_bp)
        audit_bp._csrf_wired = True
    monkeypatch.setattr(admin_routes, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 9, "username": "will", "is_admin": 1,
                                                           "restaurant_id": 1})
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(audit_bp)
    app.register_blueprint(admin_routes.admin_bp)
    rid = create_restaurant(Restaurant(name="Won Grill", owner_email="w@x.test"), db_path=db_path)
    aid = _generated_audit()
    c = app.test_client()
    c.set_cookie("csrf_js", "t")
    r = c.post(f"/admin/api/audits/{aid}/link", json={"restaurant_id": rid}, headers={"X-CSRF": "t"})
    assert r.get_json() == {"ok": True, "restaurant_id": rid, "restaurant_name": "Won Grill"}
    ev = _rows(db_path, "SELECT event_type, restaurant_id, target, before_json, after_json FROM admin_events "
                        "WHERE source='admin'")
    assert ev[0]["event_type"] == "sales_audit.linked" and ev[0]["restaurant_id"] == rid
    assert ev[0]["target"] == f"sales_audit:{aid}" and json.loads(ev[0]["after_json"]) == {"linked_restaurant_id": rid}
    audits = c.get(f"/admin/api/client/{rid}/audits").get_json()["audits"]
    assert [a["id"] for a in audits] == [aid]
    got = c.get(f"/admin/api/audits/{aid}").get_json()
    assert got["linked"] == {"restaurant_id": rid, "name": "Won Grill"}


def test_the_audit_tool_has_a_link_picker_and_the_list_quotes_names_with_jsq():
    tool = open(os.path.join(ROOT, "templates", "audit_tool.html"), encoding="utf-8").read()
    assert "/admin/api/audits/'+AUDIT_ID+'/link'" in tool and "function linkSearch" in tool
    listing = open(os.path.join(ROOT, "templates", "audit_list.html"), encoding="utf-8").read()
    assert "const jsq" in listing and "del(${a.id},'${jsq(a.restaurant_name)}')" in listing
    assert "'${esc(a.restaurant_name)}'" not in listing
