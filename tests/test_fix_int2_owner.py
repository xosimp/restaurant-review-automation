"""Integration wave, INT-2 — the owner-facing side and the smaller requests.

What these protect:
  * An owner's own POS connect — web and app, Toast, Square and Clover —
    refuses a store already bound to another live restaurant, without naming
    it (a demo is exempt), as the admin saves already did.
  * A dispute, refund or admin hold is shown as a hold — on the blocked page
    (no Resume) and in the message the phone shows.
  * Anything logged through an admin's view-as session names the admin.
  * Redraft-all's write never lands on a reply the owner edited meanwhile.
  * The dashboard draws a brand colour only when it is a six-digit hex, and
    the email history prints each send on the restaurant's own clock.
  * Operator mail is never suppressed; login_history's prune has an index;
    session expiries stay text the prune can compare.
  * The email personalisation's fixed-copy fallback is a quality finding.
  * The 2FA code is logged against the restaurant; the reactivation answer
    says whether the email really went.
  * Square and Clover syncs go through the shared manual-sync path.
  * Checkout's Stripe names and lookup keys are pricing's.
  * Provisioning and the app's staff add mint their random passwords as
    generated.
"""
import json
import os
import re
import types

import pytest
from flask import Flask, g

import auth
import auth_routes
import client_api
import clover_routes
import emails
import mobile_api
import models
import square_routes
import toast_routes
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    auth_routes._login_attempts.clear()


def _rid(db_path, name="Owner Grill", **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email=kw.pop("owner_email", "o@grill.test")),
                            db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _owner_login(db_path, rid, username="owner"):
    uid = create_user(rid, username, f"{username}@grill.test", "Owner-pass-2026", db_path=db_path)
    auth.upsert_membership(uid, rid, "client", db_path=db_path)
    return uid


# ── owner-side POS connects refuse a store bound elsewhere ──────────────────

@pytest.fixture
def web(db_path):
    app = Flask(__name__)
    for bp in (toast_routes.toast_bp, square_routes.square_bp, clover_routes.clover_bp):
        app.register_blueprint(bp)
    return app


def _web_owner(web, db_path, rid):
    c = web.test_client()
    c.set_cookie("session_token", create_session(_owner_login(db_path, rid), db_path=db_path))
    return c


@pytest.mark.parametrize("path,body,column,value", [
    ("/api/toast/save", {"client_id": "i", "client_secret": "s", "restaurant_guid": "GUID-9"},
     "toast_restaurant_guid", "guid-9"),
    ("/api/square/save", {"access_token": "t", "location_id": "LOC-9"}, "square_location_id", "loc-9"),
    ("/api/clover/save", {"merchant_id": "M-9", "api_token": "t"}, "clover_merchant_id", "m-9"),
])
def test_an_owners_web_connect_refuses_a_store_bound_elsewhere(web, db_path, monkeypatch, path, body, column, value):
    import toast, square, clover
    for mod in (toast, square, clover):
        monkeypatch.setattr(mod, "test_credentials", lambda *a: pytest.fail("the store was tested before the check"))
    _rid(db_path, "Secret Neighbor", **{column: value})
    mine = _rid(db_path, "My Grill")
    r = _web_owner(web, db_path, mine).post(path, json=body)
    out = r.get_json()
    assert r.status_code == 409 and out["ok"] is False and "another Cavnar AI account" in out["error"]
    assert "Secret Neighbor" not in out["error"], "another client's name never reaches an owner"
    assert not getattr(get_restaurant(mine, db_path=db_path), column)


def test_a_demo_may_mirror_a_live_store(web, db_path, monkeypatch):
    import toast
    monkeypatch.setattr(toast, "test_credentials", lambda *a: {"ok": True})
    _rid(db_path, "Live Grill", toast_restaurant_guid="guid-7")
    demo = _rid(db_path, "Demo Grill", is_demo=1)
    r = _web_owner(web, db_path, demo).post("/api/toast/save", json={"client_id": "i", "client_secret": "s",
                                                                    "restaurant_guid": "GUID-7"})
    assert r.get_json()["ok"] is True


@pytest.fixture
def phone(db_path):
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _phone_token(phone, db_path, rid):
    _owner_login(db_path, rid, "apper")
    return phone.post("/mobile/api/login", json={"username": "apper", "password": "Owner-pass-2026"}
                      ).get_json()["token"]


@pytest.mark.parametrize("path,body,column,value", [
    ("/mobile/api/connections/toast", {"toast_client_id": "i", "toast_client_secret": "s",
                                        "toast_restaurant_guid": "guid-5"}, "toast_restaurant_guid", "GUID-5"),
    ("/mobile/api/connections/square", {"square_access_token": "t", "square_location_id": "loc-5"},
     "square_location_id", "LOC-5"),
    ("/mobile/api/connections/clover", {"clover_merchant_id": "m-5", "clover_api_token": "t"},
     "clover_merchant_id", "M-5"),
])
def test_an_owners_app_connect_refuses_a_store_bound_elsewhere(phone, db_path, monkeypatch, path, body, column,
                                                               value):
    import toast, square, clover
    for mod in (toast, square, clover):
        monkeypatch.setattr(mod, "test_credentials", lambda *a: pytest.fail("the store was tested before the check"))
    _rid(db_path, "Secret Neighbor", **{column: value})
    mine = _rid(db_path, "My Grill")
    tok = _phone_token(phone, db_path, mine)
    r = phone.post(path, json=body, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 409 and "Secret Neighbor" not in r.get_json()["error"]
    assert not getattr(get_restaurant(mine, db_path=db_path), column)


# ── a hold is shown as a hold ───────────────────────────────────────────────

def _blocked_app():
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    return app


@pytest.mark.parametrize("reason", ["dispute", "refund", "admin"])
def test_the_blocked_page_shows_a_hold_with_no_resume(db_path, reason):
    rid = _rid(db_path, billing_status="paused", pause_reason=reason)
    user = {"id": _owner_login(db_path, rid), "restaurant_id": rid, "role": "client", "is_admin": 0}
    with _blocked_app().test_request_context("/"):
        body, code = auth._billing_blocked_page(user)
    html = body if isinstance(body, str) else body.get_data(as_text=True)
    assert code == 402 and "on hold" in html and 'id="resume-btn"' not in html
    msg, extra = auth._billing_blocked_message(user)
    assert "on hold" in msg and "contact Will" in msg and "Resume" not in msg
    assert extra["locked"] is True and extra["pause_reason"] == reason


def test_the_owners_own_pause_keeps_its_resume(db_path):
    rid = _rid(db_path, billing_status="paused", pause_reason="self", paused_until="2026-10-15")
    user = {"id": _owner_login(db_path, rid), "restaurant_id": rid, "role": "client", "is_admin": 0}
    with _blocked_app().test_request_context("/"):
        body, code = auth._billing_blocked_page(user)
    html = body if isinstance(body, str) else body.get_data(as_text=True)
    assert 'id="resume-btn"' in html and "on hold" not in html
    msg, extra = auth._billing_blocked_message(user)
    assert "10/15/26" in msg and "Resume" in msg and extra["locked"] is False


# ── a write through view-as names the admin ─────────────────────────────────

def test_an_event_logged_through_view_as_names_the_admin(db_path):
    rid = _rid(db_path)
    app = Flask(__name__)
    with app.test_request_context("/api/x", method="POST"):
        g.view_as = {"acting_admin_id": 7, "acting_admin": "will", "as_username": "erik", "restaurant_id": rid}
        client_api.log_account_event(rid, "hours_changed", {"username": "erik"}, detail="Mon 9-5")
    with app.test_request_context("/api/x", method="POST"):
        client_api.log_account_event(rid, "hours_changed", {"username": "erik"}, detail="Tue 9-5")
    rows = models.get_account_activity(rid, db_path=db_path)
    by_detail = {r["detail"]: r for r in rows}
    assert by_detail["Mon 9-5"]["actor"] == "will (Cavnar AI, viewing as erik)"
    assert by_detail["Tue 9-5"]["actor"] == "erik"
    raw = models.get_conn(db_path).execute("SELECT event_data FROM activity_log WHERE event_data LIKE '%Mon 9-5%'"
                                           ).fetchone()["event_data"]
    assert json.loads(raw)["acting_admin_id"] == 7


# ── redraft-all never lands on an owner's edit ──────────────────────────────

def _review(db_path, rid, edited=0, status="drafted"):
    conn = models.get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                           "fetched_at, response_status, draft_response, draft_edited) "
                           "VALUES (?, 'google', ?, 'Ann', 2, 'cold food', datetime('now'), ?, 'owner words', ?)",
                           (rid, f"x-{edited}-{status}", status, edited))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_an_unedited_only_write_leaves_an_owner_edit(db_path):
    rid = _rid(db_path)
    edited, plain = _review(db_path, rid, edited=1), _review(db_path, rid, edited=0, status="pending")
    assert models.update_draft(edited, "model text", unedited_only=True) is False
    assert models.update_draft(plain, "model text", unedited_only=True) is True
    assert models.update_draft(edited, "model text") is True, "a plain write keeps its old rule"


def test_redraft_all_writes_unedited_only(db_path, monkeypatch):
    """The race #78 left open: the job read draft_edited=0, the owner edited
    while the model was writing, and the new draft landed on their words."""
    import drafter
    rid = _rid(db_path)
    rv = _review(db_path, rid, edited=1)
    msg = types.SimpleNamespace(stop_reason="end_turn", content=[])
    monkeypatch.setattr(drafter, "get_client", lambda: None)
    monkeypatch.setattr(drafter, "create_with_retry", lambda *a, **k: msg)
    monkeypatch.setattr(drafter, "is_refusal", lambda m: False)
    monkeypatch.setattr(drafter, "extract_text", lambda m: "A fresh model reply.")
    monkeypatch.setattr(drafter, "check_reply", lambda draft, **k: (None, draft))
    with pytest.raises(drafter.DraftNotReplaced):
        drafter.draft_response(rv, 2, "cold food", "negative", "Owner Grill", restaurant_id=rid,
                               unedited_only=True)
    row = models.get_conn(db_path).execute("SELECT draft_response FROM reviews WHERE id=?", (rv,)).fetchone()
    assert row["draft_response"] == "owner words"
    src = open(os.path.join(ROOT, "admin_routes.py"), encoding="utf-8").read()
    job = src[src.index("def _redraft_job("):]
    job = job[:job.index("\ndef ")]
    assert "unedited_only=True" in job


# ── dashboard.html ──────────────────────────────────────────────────────────

def _brand_block():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    lines = [ln for ln in src.split("\n") if "_brand_hex" in ln]
    return "\n".join(lines)


@pytest.mark.parametrize("stored,drawn", [
    ("#c84b2f", "#c84b2f"), ("#C84B2F", "#c84b2f"), ("red;}body{display:none", None), ("#abc", None),
    ("", None), (None, None), ("#c84b2fcc", None),
])
def test_the_dashboard_draws_a_brand_colour_only_when_it_is_six_digit_hex(stored, drawn):
    from jinja2 import Environment
    out = Environment(autoescape=True).from_string(_brand_block()).render(
        restaurant=types.SimpleNamespace(brand_color=stored))
    if drawn:
        assert f"--ember:{drawn};" in out and f"--ember2:{drawn}cc" in out
    else:
        assert "--ember" not in out
    assert "restaurant.brand_color | e }}" not in open(os.path.join(ROOT, "templates", "dashboard.html")).read()


def test_the_email_history_prints_the_restaurants_own_clock():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    fn = src[src.index("function loadEmailHistory("):]
    fn = fn[:fn.index("\nfunction ")]
    assert "_ehFmtDateTime(e.sent_at_local || e.sent_at)" in fn


# ── operator mail, login_history, session expiries (D's requests) ───────────

@pytest.mark.parametrize("email_type", ["ops_platform_alert", "ops_failure_digest", "ops_weekly_digest",
                                        "ops_backup", "ops_stale_inventory", "ops_inactive_clients",
                                        "restore_drill"])
def test_operator_mail_is_never_suppressed(email_type, monkeypatch):
    assert emails.is_operator_mail(email_type, ["someone@x.test"])
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    monkeypatch.setattr(models, "is_email_suppressed", lambda *a, **k: True)
    import requests
    sent = []

    class _R:
        status_code = 200

        def json(self):
            return {"id": "m1"}
    monkeypatch.setattr(requests, "post", lambda url, **k: sent.append(url) or _R())
    assert emails.deliver({"from": "ops@x.test", "to": ["will@x.test"], "subject": "s", "html": "h"},
                          email_type=email_type, log_send=False).ok
    assert sent


def test_login_history_has_an_index_for_its_prune(db_path):
    rows = models.get_conn(db_path).execute("SELECT name FROM sqlite_master WHERE type='index' AND "
                                            "tbl_name='login_history'").fetchall()
    assert "idx_login_history_created" in {r["name"] for r in rows}


def test_session_expiries_stay_text_the_prune_compares(db_path):
    rid = _rid(db_path)
    uid = _owner_login(db_path, rid)
    create_session(uid, db_path=db_path)
    (row,) = models.get_conn(db_path).execute("SELECT expires_at, typeof(expires_at) AS t FROM sessions").fetchall()
    assert row["t"] == "text" and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", row["expires_at"])


# ── G: the personalisation fallback is a quality finding ────────────────────

def test_the_personalisation_fallback_is_recorded(monkeypatch):
    import ai_utils
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    events, marks = [], []
    monkeypatch.setattr(ai_utils, "get_client", lambda: None)
    msg = types.SimpleNamespace(stop_reason="end_turn")
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: msg)
    monkeypatch.setattr(ai_utils, "extract_text", lambda m: "   ")
    monkeypatch.setattr(ai_utils, "record_quality_event", lambda *a, **k: events.append((a, k)) or True)
    monkeypatch.setattr(ai_utils, "mark_outcome", lambda m, outcome, reason=None, **k: marks.append(outcome))
    out = emails.generate_email_personalization("context", "FIXED COPY", restaurant_id=4)
    assert out == "FIXED COPY" and marks == ["unparseable"]
    (args, kw), = events
    assert args[:2] == ("email_personalization", "fallback") and kw["restaurant_id"] == 4
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert emails.generate_email_personalization("context", "FIXED COPY", restaurant_id=4) == "FIXED COPY"
    assert len(events) == 2 and "RuntimeError" in events[-1][1]["detail"]


# ── senders take the restaurant ─────────────────────────────────────────────

def test_the_2fa_code_is_logged_against_the_restaurant(db_path, monkeypatch):
    got = []
    monkeypatch.setattr(emails, "send_2fa_code", lambda to, name, code, owner=None, **k: got.append(k) or True)
    rid = _rid(db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert auth.send_two_fa_code({"kind": "email", "to": "o@grill.test", "name": None}, r, "123456")
    assert got == [{"restaurant_id": rid}]
    admin_label = types.SimpleNamespace(name=auth.ADMIN_CONSOLE_NAME)
    auth.send_two_fa_code({"kind": "email", "to": "w@x.test", "name": None}, admin_label, "654321")
    assert got[-1] == {"restaurant_id": None}, "an admin's own code belongs to no restaurant"


def test_reactivation_says_whether_the_email_really_went(db_path, monkeypatch):
    import admin_routes
    import scheduler
    monkeypatch.setattr(admin_routes, "get_conn", lambda *a, **k: models.get_conn(db_path))
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    got = []
    monkeypatch.setattr(emails, "send_reactivation_email",
                        lambda **k: got.append(k) or emails.SendResult(False, error="recipient suppressed",
                                                                       reason="suppressed"))
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)
    hq = _rid(db_path, "HQ", owner_email="will@x.test")
    admin = create_user(hq, "will", "will@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    rid = _rid(db_path, billing_status="active")
    uid = _owner_login(db_path, rid)
    c = app.test_client()
    c.set_cookie("session_token", create_session(admin, password_verified_at=True, db_path=db_path))
    c.post(f"/admin/deactivate-client/{uid}")
    out = c.post(f"/admin/reactivate-client/{uid}").get_json()
    assert out["emailed"] is False and "suppression list" in out["email_note"]
    assert got and got[0]["restaurant_id"] == rid


# ── Square and Clover syncs take the shared path ────────────────────────────

@pytest.mark.parametrize("provider,cols", [("square", {"square_access_token": "t", "square_location_id": "L"}),
                                           ("clover", {"clover_merchant_id": "M", "clover_api_token": "t"})])
def test_square_and_clover_syncs_go_through_the_shared_manual_sync(db_path, monkeypatch, provider, cols):
    import scheduler
    calls = []
    monkeypatch.setattr(scheduler, "start_manual_pos_sync", lambda rid, actor="admin": calls.append((rid, actor))
                        or ("job-1", False))
    rid = _rid(db_path, **cols)
    app = Flask(__name__)
    app.register_blueprint(square_routes.square_bp if provider == "square" else clover_routes.clover_bp)
    hq = _rid(db_path, "HQ", owner_email="will@x.test")
    admin = create_user(hq, "will", "will@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(admin, db_path=db_path))
    r = c.post(f"/admin/{provider}/sync/{rid}").get_json()
    assert r["ok"] is True and r["job_id"] == "job-1" and calls == [(rid, "will")]
    owner = app.test_client()
    owner.set_cookie("session_token", create_session(_owner_login(db_path, rid), db_path=db_path))
    assert owner.post(f"/api/{provider}/sync").get_json()["job_id"] == "job-1"
    assert calls[-1] == (rid, "owner")
    src = open(os.path.join(ROOT, f"{provider}_routes.py"), encoding="utf-8").read()
    assert "threading.Thread" not in src and "sync_to_db(" not in src


# ── checkout's Stripe names are pricing's ───────────────────────────────────

def test_checkout_names_its_stripe_products_and_prices_as_pricing_does(monkeypatch):
    import config
    import pricing
    names, keys = [], []

    class _Obj(types.SimpleNamespace):
        pass
    fake = types.SimpleNamespace(
        Product=types.SimpleNamespace(search=lambda query, limit: names.append(query) or _Obj(data=[_Obj(id="prod_1")]),
                                      create=lambda name: _Obj(id="prod_1")),
        Price=types.SimpleNamespace(list=lambda lookup_keys, active, limit: keys.append(lookup_keys[0])
                                    or _Obj(data=[_Obj(id="price_1")]),
                                    create=lambda **k: _Obj(id="price_1")),
        checkout=types.SimpleNamespace(Session=types.SimpleNamespace(create=lambda **k: _Obj(url="https://co"))))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_int2")
    monkeypatch.setattr(config, "stripe_api", lambda key: fake)
    import sys
    monkeypatch.setitem(sys.modules, "stripe", types.ModuleType("stripe"))   # the SDK need not be installed here
    monkeypatch.setattr(emails, "_PRICE_IDS", {})
    assert emails.create_stripe_checkout(2, "o@x.test", "Grill", "annual", restaurant_id=3) == "https://co"
    assert names == [f'name:"{pricing.setup_product_name(2)}"', f'name:"{pricing.retainer_product_name(2, "annual")}"']
    plan = pricing.plan_for(2)
    assert keys == [pricing.price_lookup_key("prod_1", plan["setup"] * 100),
                    pricing.price_lookup_key("prod_1", plan["annual"] * 100, "year")]


# ── random passwords are minted as generated ────────────────────────────────

def test_provisioning_mints_the_owners_password_as_generated(db_path, monkeypatch):
    import provisioning
    import billing_jobs
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", True)
    monkeypatch.setattr(auth, "password_policy_error", lambda pw: "refused: a typed password would be checked")
    monkeypatch.setattr(billing_jobs, "_sending_allowed", lambda: False)
    rid = provisioning.provision_from_checkout({"id": "cs_int2", "customer": "cus_int2",
                                                "customer_details": {"email": "new@owner.test", "name": "New"},
                                                "metadata": {"restaurant": "New Place", "module_keys": "reviews"}},
                                               db_path=db_path)
    assert rid and get_restaurant(rid, db_path=db_path).name == "New Place"
    src = open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8").read()
    assert 'create_user(rid, username, f"{username}@staff.invalid",\n                              _sec.token_urlsafe(32), generated=True)' in src


# ── the docs pass's findings on the owner side ──────────────────────────────

def test_the_web_2fa_test_code_says_when_it_did_not_go(db_path, monkeypatch):
    import permissions
    monkeypatch.setattr(emails, "send_2fa_code", lambda *a, **k: emails.not_sent("not_configured", "no key"))
    app = Flask(__name__)
    app.register_blueprint(auth_routes.auth_bp)
    rid = _rid(db_path)
    uid = _owner_login(db_path, rid)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    c.set_cookie("csrf_js", "t")
    r = c.post("/api/send-2fa-test", json={"method": "email"}, headers={"X-CSRF": "t"})
    assert r.status_code == 502 and r.get_json()["ok"] is False and "delivery failed" in r.get_json()["error"]
    left = models.get_conn(db_path).execute("SELECT COUNT(*) FROM two_fa_challenges WHERE user_id=?",
                                            (uid,)).fetchone()[0]
    assert left == 0, "an unsent code leaves no challenge to rate-limit the retry"
    monkeypatch.setattr(emails, "send_2fa_code", lambda *a, **k: emails.SendResult(True, "m1"))
    ok = c.post("/api/send-2fa-test", json={"method": "email"}, headers={"X-CSRF": "t"})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True


def test_a_deletion_notice_skipped_on_a_local_backend_is_recorded(db_path, monkeypatch):
    for v in ("ALLOW_LOCAL_SCHEDULER", "RESTORE_FROM", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID",
              "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(emails, "send_account_deletion_request_email", lambda *a, **k: pytest.fail("emailed"))
    rid = _rid(db_path)
    client_api._notify_deletion_request(rid, get_restaurant(rid, db_path=db_path), {"username": "owner"},
                                        "2026-09-29 12:00:00")
    rows = models.get_conn(db_path).execute("SELECT source, event_type, summary FROM admin_events "
                                            "WHERE restaurant_id=? ORDER BY id", (rid,)).fetchall()
    assert [(r["source"], r["event_type"]) for r in rows] == [("account", "deletion.requested"),
                                                              ("account", "deletion.notice_skipped")]
    assert "not the production server" in rows[-1]["summary"]
