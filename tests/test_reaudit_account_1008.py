"""Blind re-audit 10/8/26 — Account, auth, iPad and UI (the backend halves).

#1  a passkey added is said to the login's own address, with "This wasn't me".
#5  Delete my login ends the login everywhere: every membership (and its PIN),
    the second factor, extra permissions — and the console never revives it.
#6  a passkey is never removed in view-as.
#7  a co-owner deletes their own login while another owner remains.
#11 the app's passkey is named for the device it was made on.
#14 one "connected" rule for Instagram & Facebook.
#15 the app's health card never says "update your card".
#16 a measured change with no dollar figure reads "—", never $0.
"""
import os

import pytest

import account_health
import auth
import auth_routes
import client_api
import mobile_api
import models
from auth import create_user, upsert_membership, set_membership_pin, verify_membership_pin

from test_ios_account_parity import (_redirect_db, app, client, _restaurant, _login, _row,  # noqa: F401
                                     _app_device, _register_in_app)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _exec(db_path, sql, *args):
    c = models.get_conn(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


# ── #1 a passkey added is said ──────────────────────────────────────────────

def test_adding_a_passkey_emails_the_login_with_a_this_wasnt_me_link(client, db_path, monkeypatch):
    sent = []
    import emails
    monkeypatch.setattr(emails, "send_passkey_added_email", lambda *a, **k: sent.append((a, k)))
    rid = _restaurant(db_path)
    _login(client, db_path, rid)
    uid, h = _login(client, db_path, rid, "server.ana", role="manager")
    saved = _register_in_app(client, h, _app_device())
    assert len(sent) == 1
    (to, name, device), kw = sent[0][0][:3], sent[0][1]
    assert to == "server.ana@x.test"          # the login's own address, not the owner's
    assert name == "Parity Co" and device == saved["name"]
    assert "/auth/not-me/" in kw["report_url"] and kw["restaurant_id"] == rid
    # The link is a live "This wasn't me" for this login.
    token = kw["report_url"].rsplit("/", 1)[1]
    assert auth.consume_login_report(token, db_path=db_path)["id"] == uid
    assert _row(db_path, "SELECT COUNT(*) FROM user_passkeys WHERE user_id=?", uid)[0] == 0


def test_the_passkey_notice_never_blocks_the_save(client, db_path, monkeypatch):
    import emails

    def boom(*a, **k):
        raise RuntimeError("resend down")
    monkeypatch.setattr(emails, "send_passkey_added_email", boom)
    rid = _restaurant(db_path)
    _uid, h = _login(client, db_path, rid)
    assert _register_in_app(client, h, _app_device())["id"]


def test_the_passkey_notice_is_security_mail():
    import emails
    assert "send_passkey_added_email" in emails._SUPPRESSION_EXEMPT
    assert "send_passkey_added_email" in emails.FLOOD_LIMITS
    assert models.EMAIL_TYPE_LABELS["send_passkey_added_email"] == "Passkey added"
    result = emails.send_passkey_added_email("a@x.test", "R", "iPad", report_url="https://x/auth/not-me/t")
    assert isinstance(result, emails.SendResult) and result.ok is False


def test_the_passkey_email_says_the_device_and_carries_the_link(monkeypatch):
    import emails
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    got = {}
    monkeypatch.setattr(emails, "deliver", lambda **kw: got.update(kw) or emails.SendResult(True))
    assert emails.send_passkey_added_email("a@x.test", "Simple EJ's", "iPad",
                                           report_url="https://x/auth/not-me/tok", restaurant_id=5).ok
    html = got["payload"]["html"]
    assert "iPad" in html and "https://x/auth/not-me/tok" in html and "This wasn&rsquo;t me" in html
    assert got["email_type"] == "send_passkey_added_email" and got["restaurant_id"] == 5


# ── #6 never removed in view-as ─────────────────────────────────────────────

def test_view_as_cannot_remove_a_passkey(client, db_path):
    rid = _restaurant(db_path)
    uid, h = _login(client, db_path, rid)
    saved = _register_in_app(client, h, _app_device())
    viewer = dict(auth.get_user_by_id(uid), acting_admin_id=99)
    payload, status = auth_routes._do_passkey_remove(viewer, saved["id"])
    assert status == 403 and "view-as" in payload["error"]
    assert _row(db_path, "SELECT COUNT(*) FROM user_passkeys WHERE user_id=?", uid)[0] == 1


# ── #11 named for the device ────────────────────────────────────────────────

@pytest.mark.parametrize("device,name", [("iPad", "iPad"), ("iPhone", "iPhone"), ("Mac", "Mac"),
                                         (None, "iPhone"), ("Windows", "iPhone")])
def test_the_app_names_a_passkey_for_its_device(client, db_path, device, name):
    rid = _restaurant(db_path)
    _uid, h = _login(client, db_path, rid)
    phone = _app_device()
    opts = client.post("/mobile/api/passkeys/options", json={"password": "correct horse battery"},
                       headers=h).get_json()["options"]
    body = {"credential": phone.create(opts)}
    if device:
        body["device"] = device
    r = client.post("/mobile/api/passkeys", json=body, headers=h)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["passkey"]["name"] == name


# ── #5 Delete my login ends it everywhere ───────────────────────────────────

def _teammate_with_pins(client, db_path):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, name="Uptown Co")
    _login(client, db_path, rid)
    create_user(other, "owner.up", "owner.up@x.test", "correct horse battery", db_path=db_path)
    uid, h = _login(client, db_path, rid, "server.ana", role="manager")
    # A manager here who also holds a staff PIN at another location.
    here = upsert_membership(uid, rid, "manager", employee_name="Ana Ruiz", db_path=db_path)
    there = upsert_membership(uid, other, "employee", employee_name="Ana Ruiz", db_path=db_path)
    assert set_membership_pin(here["id"], rid, "4821", db_path=db_path)
    assert set_membership_pin(there["id"], other, "4821", db_path=db_path)
    assert verify_membership_pin(there["id"], other, "4821", db_path=db_path)["ok"]
    _exec(db_path, "INSERT INTO user_totp (user_id, secret) VALUES (?, 'enc')", uid)
    _exec(db_path, "INSERT INTO user_backup_codes (user_id, code_hash) VALUES (?, 'h')", uid)
    _exec(db_path, "INSERT INTO permission_grants (user_id, restaurant_id, permission) VALUES (?,?,?)",
          uid, rid, "reviews.reply")
    _exec(db_path, "INSERT INTO two_fa_challenges (restaurant_id, user_id, pending_hash, code_hash, expires_at) "
                   "VALUES (?,?,?,?,?)", rid, uid, "p", "c", "2099-01-01")
    return rid, other, uid, h, here, there


def test_delete_my_login_ends_every_membership_pin_and_second_factor(client, db_path):
    rid, other, uid, h, here, there = _teammate_with_pins(client, db_path)
    r = client.post("/mobile/api/account/delete-login", headers=h)
    assert r.status_code == 200, r.get_json()
    for m in (here, there):
        row = _row(db_path, "SELECT is_active, pin_hash, deleted_at FROM memberships WHERE id=?", m["id"])
        assert row[0] == 0 and row[1] is None and row[2] is not None
    # The PIN no longer opens the staff app at either location.
    assert not verify_membership_pin(there["id"], other, "4821", db_path=db_path)["ok"]
    assert not verify_membership_pin(here["id"], rid, "4821", db_path=db_path)["ok"]
    for table in ("user_totp", "user_backup_codes", "permission_grants", "two_fa_challenges", "sessions"):
        assert _row(db_path, f"SELECT COUNT(*) FROM {table} WHERE user_id=?", uid)[0] == 0, table
    # The membership never comes back through the roster switch either.
    assert _row(db_path, "SELECT password_hash FROM users WHERE id=?", uid)[0].startswith(auth.DELETED_LOGIN_MARK)


def test_a_login_its_holder_deleted_carries_the_mark(client, db_path):
    """The console's reactivate reads this mark and refuses (tests/
    test_fix_a_login_actions.py drives the route)."""
    _rid, _other, uid, h, _here, _there = _teammate_with_pins(client, db_path)
    assert client.post("/mobile/api/account/delete-login", headers=h).status_code == 200
    assert auth.login_deleted_by_holder(_row(db_path, "SELECT password_hash FROM users WHERE id=?", uid))
    assert not auth.login_deleted_by_holder({"password_hash": "pbkdf2:abc"})
    assert not auth.login_deleted_by_holder(None)


def test_the_delete_copy_says_it_ends_everywhere():
    html = open(os.path.join(ROOT, "templates", "dashboard.html")).read()
    card = html[html.index('id="acct-delete-login-card"'):]
    card = card[:card.index("</div></div>")]
    assert "any other location you sign in to" in card and "staff PIN" in card
    assert "two-factor" in card
    js = html[html.index("function acctDeleteLogin(btn)"):]
    assert "every location" in js[:600]
    swift = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Account",
                              "AccountCloseAccountView.swift")).read()
    assert "It ends at every location you sign in to" in swift and "staff PIN" in swift


# ── #7 a co-owner deletes their own login while another owner remains ───────

def test_a_co_owner_deletes_their_own_login_while_another_owner_remains(client, db_path):
    rid = _restaurant(db_path)
    holder, hh = _login(client, db_path, rid)                       # role client: the account holder
    co, hc = _login(client, db_path, rid, "co.owner", role="owner")
    # Both are offered what applies: the holder closes; the co-owner may delete.
    assert client.get("/mobile/api/account", headers=hh).get_json()["account"]["can_delete_login"] is False
    assert client.get("/mobile/api/account", headers=hc).get_json()["account"]["can_delete_login"] is True
    r = client.post("/mobile/api/account/delete-login", headers=hh)
    assert r.status_code == 403 and "Close my account" in r.get_json()["error"]
    r = client.post("/mobile/api/account/delete-login", headers=hc)
    assert r.status_code == 200, r.get_json()
    assert _row(db_path, "SELECT is_active FROM users WHERE id=?", co)[0] == 0
    assert _row(db_path, "SELECT is_active FROM users WHERE id=?", holder)[0] == 1


def test_the_last_owner_and_the_owner_of_record_cannot(client, db_path):
    rid = _restaurant(db_path)
    holder, _hh = _login(client, db_path, rid)
    co, hc = _login(client, db_path, rid, "co.owner", role="owner")
    # With the holder gone, the co-owner is the only owner left.
    _exec(db_path, "UPDATE users SET is_active=0 WHERE id=?", holder)
    _login(client, db_path, rid, "server.ana", role="manager")
    refusal = auth.own_login_deletion_refusal(auth.get_user_by_id(co))
    assert refusal and "only owner left" in refusal
    assert client.post("/mobile/api/account/delete-login", headers=hc).status_code == 403
    # A co-owner whose address is the restaurant's owner email is the holder.
    _exec(db_path, "UPDATE users SET is_active=1 WHERE id=?", holder)
    _exec(db_path, "UPDATE restaurants SET owner_email='co.owner@x.test' WHERE id=?", rid)
    assert "Close my account" in auth.own_login_deletion_refusal(auth.get_user_by_id(co))
    # View-as never.
    viewer = dict(auth.get_user_by_id(co), acting_admin_id=1)
    assert client_api._do_delete_own_login(viewer)[1] == 403


def test_the_web_draws_both_cards_for_a_co_owner():
    html = open(os.path.join(ROOT, "templates", "dashboard.html")).read()
    assert "{% if (can_delete_login if can_delete_login is defined else not _principal) %}" in html
    src = open(os.path.join(ROOT, "hosted_dashboard.py")).read()
    assert "can_delete_login=_can_delete_login" in src


# ── #14 one connected rule ──────────────────────────────────────────────────

def test_a_facebook_page_alone_is_connected_in_both_counts(client, db_path):
    rid = _restaurant(db_path, fb_page_token="tok", fb_page_id="1")
    _uid, h = _login(client, db_path, rid)
    conns = client.get("/mobile/api/account", headers=h).get_json()["connections"]
    health = account_health.connections(models.get_restaurant(rid))
    assert conns["instagram"]["connected"] is True and health["rows"]["instagram"] is True


# ── #15 the app's billing wording ───────────────────────────────────────────

def test_the_app_never_says_update_your_card(client, db_path):
    rid = _restaurant(db_path, billing_status="past_due")
    _uid, h = _login(client, db_path, rid)
    ios = client.get("/mobile/api/account/health", headers=h).get_json()
    sub = next(i for i in ios["items"] if i["key"] == "subscription")
    assert "card" not in sub["sub"].lower() and "service agreement" in sub["sub"]
    assert ios["fix"]["key"] in {i["key"] for i in ios["items"]}
    fixes = [ios["fix"]["label"]] if ios["fix"]["key"] == "subscription" else []
    assert all("payment" not in f.lower() for f in fixes)
    web = account_health.payload(auth.get_user_by_id(_uid))
    assert next(i for i in web["items"] if i["key"] == "subscription")["sub"] == "Payment past due — update your card"
    ios_payload = account_health.payload(auth.get_user_by_id(_uid), surface="ios")
    if ios_payload["fix"] and ios_payload["fix"]["key"] == "subscription":
        assert ios_payload["fix"]["label"] == account_health.IOS_SUBSCRIPTION_FIX


# ── #16 "—", never $0 ───────────────────────────────────────────────────────

def test_a_measured_change_with_no_figure_reads_a_dash(monkeypatch):
    import value_delivered
    monkeypatch.setattr(value_delivered, "viewer_scope", lambda rid, user: {})
    monkeypatch.setattr(value_delivered, "delivered",
                        lambda rid, scope=None: {"wins": 1, "evaluated": 1, "in_flight": 0,
                                                 "net_monthly": None, "monthly": None, "worsened": {}})
    out = account_health.measured_value({"restaurant_id": 1, "role": "client"})
    assert out["measured"] is True and out["net_monthly"] is None
    assert "$0" not in out["line"] and "Measured, net: —" in out["line"]
    monkeypatch.setattr(value_delivered, "delivered",
                        lambda rid, scope=None: {"wins": 2, "evaluated": 2, "in_flight": 0,
                                                 "net_monthly": 1240.4, "worsened": {}})
    out = account_health.measured_value({"restaurant_id": 1, "role": "client"})
    assert out["line"].startswith("Measured, net: $1,240/mo") and out["net_monthly"] == 1240.4
