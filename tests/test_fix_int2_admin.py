"""Integration wave, INT-2 — the admin side of the fix round's cross-workstream
requests.

What these protect:
  * The step-up (owner decision 4) is on every sensitive admin route the
    workstreams left for this wave — B2's reset / welcome / demo / delete /
    add-location routes, freeze, H's billing actions, B1's POS credential
    routes — and, as a helper that answers the same 403, on the settings save
    (billing, a module or the owner email), the offboarding steps that act,
    and the review-account seed's rotation.
  * resend-contract and the alert-contact test text refuse on a local
    backend, like every other admin send.
  * One audit call: A's, B1's and H's admin actions land in admin_events'
    typed columns (actor, actor_id, before, after, result) through
    admin_events.record_admin_action.
  * One admin job pool; one welcome email (409 when the address is
    suppressed); the create-client contract step passes the restaurant and
    records the envelope's terms, with no synthetic email_log row.
  * The legacy billing override is attributed in billing_status_history,
    and a pause it lifts takes its reason with it.
  * Offboarding's Stripe and DocuSign steps carry out their action; a
    covered location's subscription is never cancelled from here; nothing
    reaches Stripe or DocuSign from a local backend.
  * delete_restaurant never deletes an admin or support login.
  * /admin/audits/new creates nothing on a GET.
  * The console's JSON read of a client's settings carries the contract the
    save expects.
Stripe, DocuSign, Resend and Twilio are stand-ins; nothing leaves the process.
"""
import json
import os
import sys
import types

import pytest
from flask import Flask

import admin_events
import admin_routes
import auth
import auth_routes
import billing_jobs
import clover_routes
import docusign_helper
import emails
import models
import offboarding
import ops
import rpower_routes
import sales_audit_routes
import square_routes
import toast_routes
import webhook_routes
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSRF = "int2-admin-csrf"
# The real pool entry, kept before the autouse fixture swaps in an inline runner.
_REAL_SUBMIT = admin_routes._submit_admin_job


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, auth_routes, admin_routes, webhook_routes, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    models.init_staff_notes(db_path=db_path)
    auth_routes._login_attempts.clear()
    # Admin jobs run inline, so a test reads their result.
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *a: fn(*a))


@pytest.fixture
def app():
    # The audit blueprint gets its CSRF check before its first registration,
    # as hosted_dashboard and tests/test_sales_audit.py wire it: Flask refuses
    # a before_request added after a blueprint was registered anywhere.
    if not getattr(sales_audit_routes.audit_bp, "_csrf_wired", False):
        from csrf import csrf_protect
        csrf_protect(sales_audit_routes.audit_bp)
        sales_audit_routes.audit_bp._csrf_wired = True
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "int2-admin"
    for bp in (admin_routes.admin_bp, auth_routes.auth_bp, toast_routes.toast_bp, square_routes.square_bp,
               clover_routes.clover_bp, rpower_routes.rpower_bp, sales_audit_routes.audit_bp):
        flask_app.register_blueprint(bp)
    return flask_app


def _hq(db_path):
    return create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@cavnar.test"), db_path=db_path)


def _admin(app, db_path, stepped_up=True, username="will"):
    uid = create_user(_hq(db_path), username, f"{username}@cavnar.test", "Admin-pass-2026", is_admin=True,
                      db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True if stepped_up else None,
                                                 db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c, uid


def _post(c, url, body=None):
    return c.post(url, json=body if body is not None else {}, headers={"X-CSRF": CSRF})


def _rid(db_path, name="Client Grill", **kw):
    kw.setdefault("owner_email", f"{name.split()[0].lower()}@grill.test")
    rid = create_restaurant(Restaurant(name=name, owner_email=kw.pop("owner_email")), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _owner(db_path, rid, username="erik", email="erik@grill.test"):
    uid = create_user(rid, username, email, "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return uid


def _rows(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── the step-up on the routes the workstreams left for this wave ────────────

_STEPPED = [
    "/admin/send-reset-link/{uid}", "/admin/reset-password/{uid}", "/admin/reset-password-by-restaurant/{rid}",
    "/admin/resend-welcome/{rid}", "/admin/api/client/{rid}/demo", "/admin/api/client/{rid}/delete-demo",
    "/admin/api/client/{rid}/delete", "/admin/api/brand/add-location", "/admin/freeze/{rid}",
    "/admin/api/billing/{rid}/change-plan", "/admin/api/billing/{rid}/lift-hold",
    "/admin/api/billing/{rid}/mark-signed", "/admin/api/billing/{rid}/attach-stripe-customer",
    "/admin/toast/save/{rid}", "/admin/toast/disconnect/{rid}", "/admin/square/save/{rid}",
    "/admin/square/disconnect/{rid}", "/admin/clover/save/{rid}", "/admin/clover/disconnect/{rid}",
    "/admin/rpower/save/{rid}", "/admin/rpower/bootstrap/{rid}", "/admin/rpower/disconnect/{rid}",
]


@pytest.mark.parametrize("path", _STEPPED)
def test_a_sensitive_admin_route_asks_for_the_password_again(app, db_path, path):
    c, _ = _admin(app, db_path, stepped_up=False)
    rid = _rid(db_path, toast_client_secret="s", square_access_token="t", clover_api_token="t",
               rpower_token="t", is_demo=1)
    uid = _owner(db_path, rid)
    before = get_restaurant(rid, db_path=db_path)
    r = _post(c, path.format(rid=rid, uid=uid), {"confirm_name": before.name, "is_demo": 0})
    assert r.status_code == 403 and r.get_json()["reauth_required"] is True, path
    assert r.get_json()["reauth_url"] == "/admin/api/reauth"
    after = get_restaurant(rid, db_path=db_path)
    assert after is not None and after.is_demo == before.is_demo, "nothing ran before the password"
    assert after.toast_client_secret == "s" and after.rpower_token == "t"


def test_the_step_up_sits_directly_under_admin_required_on_each_of_them():
    for name, fn in (("admin_routes.py", ("send_reset_link", "reset_password", "reset_password_by_restaurant",
                                          "resend_welcome_email", "admin_api_set_demo", "admin_api_delete_demo",
                                          "admin_api_delete_restaurant", "admin_api_add_location", "freeze_account",
                                          "admin_api_billing_change_plan", "admin_api_billing_lift_hold",
                                          "admin_api_billing_mark_signed", "admin_api_billing_attach_customer")),
                     ("toast_routes.py", ("save_toast_credentials", "disconnect_toast")),
                     ("square_routes.py", ("admin_save_square", "admin_disconnect_square")),
                     ("clover_routes.py", ("admin_save_clover", "admin_disconnect_clover")),
                     ("rpower_routes.py", ("save_rpower_token", "bootstrap_rpower", "disconnect_rpower"))):
        src = open(os.path.join(ROOT, name), encoding="utf-8").read()
        for f in fn:
            assert f"@admin_required\n@recent_auth_required()\ndef {f}(" in src, (name, f)


def test_the_settings_save_asks_only_when_billing_a_module_or_the_owner_email_changes(app, db_path):
    c, _ = _admin(app, db_path, stepped_up=False)
    rid = _rid(db_path, module_labor=0, voice_notes="old")
    url = f"/admin/client-settings/{rid}"
    ok = _post(c, url, {"voice_notes": "warmer"})
    assert ok.status_code == 200 and get_restaurant(rid, db_path=db_path).voice_notes == "warmer"
    same = _post(c, url, {"module_labor": 0, "owner_email": "client@grill.test"})
    assert same.status_code == 200, "re-sending the stored values changes nothing and needs no password"
    for body in ({"module_labor": 1}, {"owner_email": "new@grill.test"},
                 {"billing_status": "active", "billing_status_reason": "paid by check"}):
        r = _post(c, url, body)
        assert r.status_code == 403 and r.get_json()["reauth_required"] is True, body
    r = get_restaurant(rid, db_path=db_path)
    assert (r.module_labor, r.owner_email, r.billing_status) == (0, "client@grill.test", "trial")
    # A refusal the admin can fix is said before the password is asked for.
    other = _rid(db_path, "Listing Owner", google_place_id="PLACE_1")
    clash = _post(c, url, {"google_place_id": "PLACE_1", "module_labor": 1})
    assert clash.status_code == 400 and "Listing Owner" in clash.get_json()["error"]
    assert other


def test_the_review_account_seed_asks_only_to_rotate(app, db_path, monkeypatch):
    c, _ = _admin(app, db_path, stepped_up=False)
    monkeypatch.setattr(admin_routes, "_seed_review_account_job", lambda script, rotate, actor: {"ok": True})
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    assert _post(c, "/admin/seed-review-account", {}).status_code == 200
    r = _post(c, "/admin/seed-review-account", {"rotate_password": True, "confirm": "ROTATE"})
    assert r.status_code == 403 and r.get_json()["reauth_required"] is True


def test_offboarding_steps_that_act_ask_for_the_password_and_the_rest_do_not(app, db_path):
    c, _ = _admin(app, db_path, stepped_up=False)
    rid = _rid(db_path)
    for step in offboarding.ACTING_STEPS:
        r = _post(c, f"/admin/api/client/{rid}/offboarding/{step}", {"status": "done"})
        assert r.status_code == 403 and r.get_json()["reauth_required"] is True, step
    assert _post(c, f"/admin/api/client/{rid}/offboarding/export", {"status": "done", "note": "emailed"}
                 ).status_code == 200
    assert _post(c, f"/admin/api/client/{rid}/offboarding/stripe", {"status": "skipped", "note": "none"}
                 ).status_code == 200


# ── local backends send nothing from these two either ───────────────────────

@pytest.fixture
def local_backend(monkeypatch):
    monkeypatch.delenv("ALLOW_LOCAL_SCHEDULER", raising=False)
    monkeypatch.delenv("RESTORE_FROM", raising=False)
    for var in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"):
        monkeypatch.delenv(var, raising=False)


def test_resend_contract_refuses_on_a_local_backend_before_reaching_docusign(app, db_path, local_backend,
                                                                             monkeypatch):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, contract_status="sent", docusign_envelope_id="env_1", module_reviews=1)
    monkeypatch.setattr(docusign_helper, "resend_envelope", lambda *a, **k: pytest.fail("reached DocuSign"))
    monkeypatch.setattr(docusign_helper, "send_contract", lambda **k: pytest.fail("reached DocuSign"))
    r = _post(c, f"/admin/resend-contract/{rid}")
    assert r.status_code == 409 and r.get_json()["local_backend"] is True
    assert "ALLOW_LOCAL_SCHEDULER=1" in r.get_json()["error"]


def test_the_alert_contact_test_text_refuses_on_a_local_backend(app, db_path, local_backend, monkeypatch):
    import notify
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    monkeypatch.setattr(notify, "send_test_sms", lambda *a, **k: pytest.fail("texted a real contact"))
    r = _post(c, f"/admin/alert-contacts/test/{rid}")
    assert r.status_code == 409 and r.get_json()["local_backend"] is True


def test_the_alert_contact_test_text_goes_where_sending_is_allowed(app, db_path, monkeypatch):
    import notify
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    monkeypatch.setattr(notify, "send_test_sms", lambda restaurant_id: {"ok": True, "results": []})
    assert _post(c, f"/admin/alert-contacts/test/{rid}").get_json()["ok"] is True


# ── one audit call ──────────────────────────────────────────────────────────

def test_an_admin_action_lands_in_the_typed_audit_columns(app, db_path):
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path)
    r = _post(c, f"/admin/freeze/{rid}", {"reason": "reported takeover"})
    assert r.status_code == 200
    (ev,) = _rows(db_path, "SELECT * FROM admin_events WHERE event_type='account_frozen'")
    assert ev["source"] == "admin" and ev["actor"] == "will" and ev["actor_id"] == admin_uid
    assert ev["restaurant_id"] == rid and ev["result"] == "ok" and ev["payload"] is None
    assert json.loads(ev["after_json"])["reason"] == "reported takeover"


def test_billing_actions_carry_the_admins_id(app, db_path):
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path, contract_status="sent")
    assert _post(c, f"/admin/api/billing/{rid}/mark-signed", {"note": "Signed on paper"}).status_code == 200
    (ev,) = _rows(db_path, "SELECT * FROM admin_events WHERE event_type='billing.contract.marked_signed'")
    assert ev["actor"] == "will" and ev["actor_id"] == admin_uid
    assert json.loads(ev["before_json"]) == {"contract_status": "sent"}


def test_the_legacy_audit_writers_are_gone_from_the_admin_routes():
    """Every admin action goes through admin_events.record_admin_action —
    the plain record() left payload-only rows the fleet audit's actor,
    action and result filters could not see."""
    src = open(os.path.join(ROOT, "admin_routes.py"), encoding="utf-8").read()
    assert 'admin_events.record("admin"' not in src and '_ae.record("admin"' not in src
    auth_src = open(os.path.join(ROOT, "auth.py"), encoding="utf-8").read()
    body = auth_src[auth_src.index("def record_view_as_write("):]
    body = body[:body.index("\ndef ")]
    assert "record_admin_action(" in body and 'admin_events.record(' not in body


def test_a_pos_save_is_audited_with_booleans_and_never_the_credential(app, db_path, monkeypatch):
    import square
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    monkeypatch.setattr(square, "test_credentials", lambda token, loc: {"ok": True})
    assert _post(c, f"/admin/square/save/{rid}", {"access_token": "sq-SECRET-9", "location_id": "L9"}
                 ).get_json()["ok"] is True
    (ev,) = _rows(db_path, "SELECT * FROM admin_events WHERE event_type='pos.square.saved'")
    assert json.loads(ev["after_json"]) == {"connected": True}
    assert "sq-SECRET-9" not in json.dumps(_rows(db_path, "SELECT * FROM admin_events"))


# ── one admin job pool ──────────────────────────────────────────────────────

def test_every_admin_job_goes_through_the_one_bounded_pool(monkeypatch):
    src = open(os.path.join(ROOT, "admin_routes.py"), encoding="utf-8").read()
    assert "class _AdminJobs" not in src and "_ADMIN_JOBS" not in src
    start = src[src.index("def _start_admin_job("):]
    start = start[:start.index("\ndef ")]
    assert "_submit_admin_job(job_id, _run)" in start
    assert src.count("ThreadPoolExecutor(") == 1


def test_a_full_pool_refuses_a_console_job(db_path, monkeypatch):
    monkeypatch.setattr(admin_routes, "_submit_admin_job", _REAL_SUBMIT)
    monkeypatch.setattr(admin_routes, "ADMIN_JOB_QUEUE_MAX", 0)
    assert admin_routes._start_admin_job("admin_redraft", 3, lambda: {"ok": True}) == (None, False)


# ── one welcome email ───────────────────────────────────────────────────────

def test_resend_welcome_sends_the_one_welcome(app, db_path, monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda payload=None, restaurant_id=None, email_type=None, log_send=True:
                        sent.append((payload, restaurant_id, email_type)) or emails.SendResult(True, "m1"))
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, owner_name="Erik Smith")
    _owner(db_path, rid)
    r = _post(c, f"/admin/resend-welcome/{rid}")
    assert r.status_code == 200 and "3 days" in r.get_json()["message"]
    ((payload, logged_rid, email_type),) = sent
    # The same email the post-signing outbox and provisioning send.
    assert email_type == "send_welcome_set_password_email" and logged_rid == rid
    assert "/reset-password/" in payload["html"] and "Temporary password" not in payload["html"]
    assert not hasattr(emails, "send_signed_welcome_email")
    assert "_send_welcome_link_email" not in open(os.path.join(ROOT, "admin_routes.py")).read()


def test_the_outbox_welcome_skips_a_login_that_signed_in_and_fails_a_refusal_for_good(db_path, monkeypatch):
    monkeypatch.setattr(billing_jobs, "_sending_allowed", lambda: True)
    rid = _rid(db_path, module_reviews=1)
    uid = _owner(db_path, rid)
    monkeypatch.setattr(emails, "send_welcome_with_set_password_link",
                        lambda **k: emails.not_sent("no_recipient", "that login does not exist or is inactive"))
    oid, _ = billing_jobs.enqueue("welcome", rid, f"welcome:{uid}", payload={"user_id": uid})
    billing_jobs.drain_owed_sends(ids=[oid])
    (row,) = _rows(db_path, "SELECT status, attempts FROM owed_sends WHERE id=?", (oid,))
    assert row["status"] == "failed" and row["attempts"] == 1, "a refusal is not retried six times"


# ── the create-client contract step ─────────────────────────────────────────

def test_create_client_sends_the_contract_for_the_restaurant_and_records_its_terms(app, db_path, monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    calls = []
    monkeypatch.setattr(docusign_helper, "send_contract",
                        lambda **k: calls.append(k) or {"ok": True, "envelope_id": "env_new"})
    c, _ = _admin(app, db_path)
    r = _post(c, "/admin/create-client", {"restaurant_name": "New Grill", "owner_email": "own@new.test",
                                          "username": "newgrill", "password": "Correct-Horse-2026",
                                          "module_reviews": 1, "module_labor": 1})
    body = r.get_json()
    assert body["ok"] is True
    rid = body["restaurant_id"]
    assert calls and calls[0]["restaurant_id"] == rid and calls[0]["modules_list"] == "Review Intelligence, Labor Optimizer"
    (env,) = _rows(db_path, "SELECT * FROM docusign_envelopes WHERE envelope_id='env_new'")
    assert env["module_count"] == 2 and env["status"] == "sent" and env["restaurant_id"] == rid
    assert _rows(db_path, "SELECT * FROM email_log WHERE email_type='contract'") == []
    hist = _rows(db_path, "SELECT source, actor FROM billing_status_history WHERE restaurant_id=? "
                          "AND field='contract_status'", (rid,))
    assert hist and hist[-1]["source"] == "admin" and hist[-1]["actor"] == "will"


# ── the legacy billing override ─────────────────────────────────────────────

def test_a_billing_override_is_attributed_and_lifting_a_pause_lifts_its_reason(app, db_path):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, billing_status="paused", pause_reason="dispute")
    r = _post(c, f"/admin/client-settings/{rid}", {"billing_status": "active",
                                                   "billing_status_reason": "Dispute won, 9/29"})
    assert r.status_code == 200
    row = get_restaurant(rid, db_path=db_path)
    assert row.billing_status == "active" and row.pause_reason is None and row.paused_until is None
    hist = _rows(db_path, "SELECT field, new_value, source, actor, reason FROM billing_status_history "
                          "WHERE restaurant_id=? ORDER BY id", (rid,))
    status = [h for h in hist if h["field"] == "billing_status"][-1]
    assert status["source"] == "admin_override" and status["actor"] == "will"
    assert status["reason"] == "Dispute won, 9/29" and status["new_value"] == "active"
    # An admin's pause is a hold only an admin lifts, said explicitly.
    _post(c, f"/admin/client-settings/{rid}", {"billing_status": "paused", "billing_status_reason": "unpaid"})
    assert get_restaurant(rid, db_path=db_path).pause_reason == "admin"
    assert models.billing_hold(get_restaurant(rid, db_path=db_path)) == "admin"


# ── offboarding: the Stripe and DocuSign steps act ──────────────────────────

class _Sub(dict):
    def __getattr__(self, k):
        return self[k]


def _fake_stripe(cancelled, subs=()):
    sub_list = types.SimpleNamespace(data=list(subs))
    return types.SimpleNamespace(Subscription=types.SimpleNamespace(
        list=lambda **k: sub_list,
        cancel=lambda sid: cancelled.append(sid) or _Sub(id=sid, status="canceled", items={"data": []})))


def test_the_stripe_step_cancels_the_live_subscription(app, db_path, monkeypatch):
    import config
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_int2")
    cancelled = []
    monkeypatch.setattr(config, "stripe_api", lambda key: _fake_stripe(cancelled))
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_leaving")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_leaving", facts={"status": "active"})
    r = _post(c, f"/admin/api/client/{rid}/offboarding/stripe", {"status": "done", "note": "closing"})
    assert r.status_code == 200 and cancelled == ["sub_leaving"]
    step = next(s for s in r.get_json()["steps"] if s["step"] == "stripe")
    assert step["status"] == "done" and step["detail"]["cancelled"] == ["sub_leaving"]
    assert _rows(db_path, "SELECT * FROM admin_events WHERE event_type='billing.subscription_cancelled'")


def test_a_covered_location_is_never_cancelled_from_its_own_checklist(app, db_path, monkeypatch):
    import config
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    monkeypatch.setattr(config, "stripe_api", lambda key: pytest.fail("reached Stripe"))
    c, _ = _admin(app, db_path)
    payer = _rid(db_path, "Main St", owner_email="g@x.test", location_group="G", billing_status="active",
                 stripe_customer_id="cus_g")
    covered = _rid(db_path, "Elm St", owner_email="g@x.test", location_group="G", billing_status="active",
                   stripe_customer_id="cus_g")
    billing_jobs.upsert_subscription(payer, subscription_id="sub_g", facts={"status": "active"})
    r = _post(c, f"/admin/api/client/{covered}/offboarding/stripe", {"status": "done"})
    assert r.status_code == 409 and r.get_json()["covered"] is True and "Main St" in r.get_json()["error"]


def test_the_stripe_step_refuses_on_a_local_backend(app, db_path, local_backend, monkeypatch):
    import config
    monkeypatch.setattr(config, "stripe_api", lambda key: pytest.fail("reached Stripe"))
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    r = _post(c, f"/admin/api/client/{rid}/offboarding/stripe", {"status": "done"})
    assert r.status_code == 409 and r.get_json()["local_backend"] is True


def test_the_docusign_step_voids_the_open_envelope(app, db_path, monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    voided = []
    monkeypatch.setattr(docusign_helper, "void_envelope",
                        lambda envelope_id, reason, restaurant_id=None: voided.append(envelope_id)
                        or {"ok": True, "status": "voided", "already": False, "voidable": True})
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, contract_status="sent", docusign_envelope_id="env_open")
    r = _post(c, f"/admin/api/client/{rid}/offboarding/docusign", {"status": "done", "note": "closing"})
    assert r.status_code == 200 and voided == ["env_open"]
    assert get_restaurant(rid, db_path=db_path).contract_status == "voided"
    signed = _rid(db_path, "Signed Grill", contract_status="sent", docusign_envelope_id="env_signed")
    monkeypatch.setattr(docusign_helper, "void_envelope",
                        lambda *a, **k: {"ok": False, "status": "completed", "voidable": False})
    r = _post(c, f"/admin/api/client/{signed}/offboarding/docusign", {"status": "done"})
    assert r.status_code == 409 and "already signed" in r.get_json()["error"]


def test_void_envelope_voids_only_what_is_waiting(monkeypatch):
    import requests
    monkeypatch.setattr(docusign_helper, "get_access_token", lambda: "tok")

    class _R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b
    puts = []
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None: _R(200, {"status": "sent"}))
    monkeypatch.setattr(requests, "put", lambda url, headers=None, json=None, timeout=None:
                        puts.append(json) or _R(200, {}))
    assert docusign_helper.void_envelope("env_1", "closing")["status"] == "voided"
    assert puts == [{"status": "voided", "voidedReason": "closing"}]
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None: _R(200, {"status": "completed"}))
    assert docusign_helper.void_envelope("env_1", "x") == {"ok": False, "status": "completed", "already": False,
                                                           "voidable": False}


# ── delete_restaurant never deletes an admin or support login ────────────────

def test_delete_restaurant_refuses_a_restaurant_an_internal_login_calls_home(db_path):
    rid = _rid(db_path)
    sup = create_user(rid, "sup", "sup@cavnar.test", "Support-pass-2026", role="support", db_path=db_path)
    assert offboarding.admin_homes(rid, db_path=db_path) == 1
    with pytest.raises(models.InternalLoginHome):
        models.delete_restaurant(rid, db_path=db_path)
    assert get_restaurant(rid, db_path=db_path) is not None
    assert _rows(db_path, "SELECT id FROM users WHERE id=?", (sup,))
    admin_home = _rid(db_path, "Admin Home")
    create_user(admin_home, "will2", "w2@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    with pytest.raises(models.InternalLoginHome):
        models.delete_restaurant(admin_home, db_path=db_path)
    plain = _rid(db_path, "Plain Grill")
    _owner(db_path, plain, "plain", "plain@grill.test")
    assert models.delete_restaurant(plain, db_path=db_path)["restaurants"] == 1


# ── /admin/audits/new only asks on a GET ────────────────────────────────────

def test_a_new_sales_audit_is_created_by_a_post_never_a_get(app, db_path):
    import sales_audits
    sales_audits.init_sales_audits(db_path=db_path)
    c, _ = _admin(app, db_path)
    before = len(_rows(db_path, "SELECT id FROM sales_audits"))
    asked = c.get("/admin/audits/new")
    assert asked.status_code == 200 and "Start the audit" in asked.get_data(as_text=True)
    assert len(_rows(db_path, "SELECT id FROM sales_audits")) == before
    made = c.post("/admin/audits/new", data={"csrf_token": CSRF})
    assert made.status_code == 302 and "/admin/audits/" in made.headers["Location"]
    assert len(_rows(db_path, "SELECT id FROM sales_audits")) == before + 1


# ── the console's JSON read of a client's settings (UI-1) ───────────────────

def test_the_settings_read_carries_the_save_contract(app, db_path):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, voice_notes="warm", timezone="America/Denver")
    body = c.get(f"/admin/api/client/{rid}/settings").get_json()
    assert body["ok"] is True and body["version"] == models.restaurant_version(rid, db_path=db_path)
    assert body["settings"]["voice_notes"] == "warm" and "weekly_revenue_target" not in body["settings"]
    assert set(body["settings"]) <= set(body["fields"]) and body["save_url"] == f"/admin/client-settings/{rid}"
    assert set(body["step_up_fields"]) == {"billing_status", "module_reviews", "module_labor", "module_inventory",
                                           "module_marketing", "owner_email"}
    assert any(o["value"] == "America/Denver" for o in body["choices"]["timezone"])
    # A save under the contract it describes goes through.
    r = _post(c, f"/admin/client-settings/{rid}", {"voice_notes": "warmer", "expected_version": body["version"],
                                                   "base": {"voice_notes": body["settings"]["voice_notes"]}})
    assert r.status_code == 200 and get_restaurant(rid, db_path=db_path).voice_notes == "warmer"


# ── the Meta refresh is the one refresh ─────────────────────────────────────

def test_the_console_meta_refresh_is_the_schedulers(app, db_path, monkeypatch):
    import scheduler
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, ig_token="ig-old")
    calls = []

    def one_refresh(r):
        calls.append(r.id)
        update_restaurant(r.id, {"ig_token": "ig-new", "ig_token_expires": "2026-11-28"}, db_path=db_path)
        return {"ok": True, "expires": "2026-11-28"}
    monkeypatch.setattr(scheduler, "refresh_ig_token", one_refresh)
    r = _post(c, f"/admin/refresh-ig-token/{rid}")
    job = c.get(f"/admin/api/admin-jobs/{r.get_json()['job_id']}").get_json()
    assert calls == [rid] and job["ok"] is True and job["expires"] == "2026-11-28" and job["error"] is None
    assert job["expires_on"] == "11/28/26" and job["refreshed"] == {"instagram": True}
    assert "_meta_exchange" not in open(os.path.join(ROOT, "admin_routes.py")).read()


# ── the Stripe and DocuSign verdicts reach the one webhook ledger ───────────

def test_stripe_and_docusign_verdicts_feed_the_inbound_webhook_ledger(db_path):
    webhook_routes._webhook_seen("stripe", True, event="invoice.paid evt_1")
    webhook_routes._webhook_seen("docusign", False, error="signature mismatch")
    health = models.inbound_webhook_health(db_path=db_path)
    assert health["stripe"]["last_verified_at"] and health["stripe"]["last_event_type"] == "invoice.paid"
    assert health["docusign"]["failed_count"] == 1 and health["docusign"]["failing"] is True
    # H's own table keeps its detail for the billing health panel.
    assert _rows(db_path, "SELECT provider FROM webhook_verifications ORDER BY provider") == \
        [{"provider": "docusign"}, {"provider": "stripe"}]


# ── the admin_events indexes: one definition each ───────────────────────────

def test_admin_events_has_one_index_per_purpose(db_path):
    admin_events.init_admin_events(db_path=db_path)
    ops._ensure_retention_indexes(models.get_conn(db_path))
    names = {r["name"] for r in _rows(db_path, "SELECT name FROM sqlite_master WHERE type='index' "
                                                "AND tbl_name='admin_events'")}
    assert {"idx_admin_events_rid", "idx_admin_events_created_at"} <= names
    assert not names & {"idx_admin_events_rest", "idx_admin_events_created"}


# ── the docs pass's code findings (integration wave, second round) ──────────

def test_the_task_poll_is_admin_only_scoped_and_never_hands_over_a_password(app, db_path):
    c, _ = _admin(app, db_path)
    ops.start_async_job("task-1", "pos_sync_one", 5)
    ops.finish_async_job("task-1", "done", {"ok": True, "state": "ok",
                                            "result": {"attempted": 1, "ok": 1, "failed": 0, "provider": "toast",
                                                       "secret_rows": [1, 2]}})
    ops.start_async_job("seed-1", "admin_review_account", 0)
    ops.finish_async_job("seed-1", "done", {"ok": True, "password_once": "abcde-FGHJK", "_scrub": ["password_once"]})
    got = c.get("/admin/api/tasks/task-1").get_json()
    assert got["status"] == "done" and got["result"]["state"] == "ok"
    assert got["result"]["result"] == {"attempted": 1, "ok": 1, "failed": 0, "provider": "toast"}
    # Not a task this route serves: the review account's job is read once,
    # through /admin/api/admin-jobs/<id>, and never here.
    r = c.get("/admin/api/tasks/seed-1")
    assert r.status_code == 404 and "abcde" not in r.get_data(as_text=True)
    sup = create_user(_hq(db_path), "sup", "sup@cavnar.test", "Support-pass-2026", role="support", db_path=db_path)
    s = app.test_client()
    s.set_cookie("session_token", create_session(sup, db_path=db_path))
    assert s.get("/admin/api/tasks/task-1").status_code == 403


def test_mark_signed_checks_for_production_before_it_writes(app, db_path, local_backend):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, contract_status="sent")
    r = _post(c, f"/admin/api/billing/{rid}/mark-signed", {"note": "Signed on paper", "send_welcome": True})
    assert r.status_code == 409
    assert get_restaurant(rid, db_path=db_path).contract_status == "sent", "nothing is written before the gate"
    ok = _post(c, f"/admin/api/billing/{rid}/mark-signed", {"note": "Signed on paper"})
    assert ok.status_code == 200 and get_restaurant(rid, db_path=db_path).contract_status == "signed"


def test_the_alert_contact_test_names_failed_numbers_by_their_last_four(app, db_path, monkeypatch):
    import notify
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    monkeypatch.setattr(notify, "send_test_sms", lambda restaurant_id: {
        "ok": False, "sent": 0, "errors": ["+13125550123"],
        "results": [{"to_last4": "0123", "ok": False, "status": "failed"}]})
    out = _post(c, f"/admin/alert-contacts/test/{rid}").get_json()
    assert out["errors"] == ["…0123"] and "5550123" not in json.dumps(out)


def test_support_reads_of_the_audit_tool_are_masked(app, db_path, monkeypatch):
    import admin_ops
    monkeypatch.setattr(admin_ops, "viewer_role", lambda: "support")
    monkeypatch.setattr(admin_ops, "redact_for_support", lambda data: {"masked": True})
    import sales_audit_routes as sar
    fake = Flask(__name__)

    @fake.route("/admin/api/audits/probe")
    def _probe():
        from flask import jsonify
        return jsonify(owner_email="prospect@example.com")
    with fake.test_request_context("/admin/api/audits/probe"):
        resp = sar._support_redaction(_probe())
    assert resp.get_json() == {"masked": True} and resp.headers["X-Redacted"] == "support"
    assert sar._support_redaction in sar.audit_bp.after_request_funcs.get(None, [])


def test_an_unknown_break_glass_name_is_said_not_claimed(db_path, monkeypatch, capsys):
    import security
    monkeypatch.setattr(security, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    create_user(_hq(db_path), "realadmin", "real@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    assert security.apply_boot_unlocks(env={"LOGIN_UNLOCK_USERNAMES": "realadmin, typo-admin"},
                                       db_path=db_path) == ["realadmin"]
    printed = capsys.readouterr().out
    assert "NO login is named 'typo-admin'" in printed
    rows = _rows(db_path, "SELECT summary FROM admin_events WHERE event_type='lockout_cleared_break_glass'")
    assert any("which is no login" in r["summary"] for r in rows)


# ── no admin action reaches a sender from a local backend ───────────────────
# A worktree's test server can hold production's keys (Flask's .env walk-up),
# so these gates are what stop a real send (coordinator, UI-2 round). Routes
# whose PURPOSE is the send refuse with the one 409; routes whose send is a
# side effect (reactivate, a support login's reset link, create-client's
# contract) do their work and skip the send, saying so.

_SENDING_ROUTES = [
    ("/admin/send-reset-link/{uid}", {}), ("/admin/reset-password/{uid}", {}),
    ("/admin/reset-password-by-restaurant/{rid}", {}), ("/admin/resend-welcome/{rid}", {}),
    ("/admin/test-digest/{rid}", {}), ("/admin/test-urgent/{rid}", {}),
    ("/admin/resend-contract/{rid}", {}), ("/admin/alert-contacts/test/{rid}", {}),
    ("/admin/resend-payment/{rid}", {}), ("/admin/api/billing/{rid}/card-update-link", {}),
    ("/admin/api/billing/{rid}/mark-signed", {"note": "paper", "send_payment_link": True}),
    ("/admin/api/client/{rid}/value-recap", {}),
]


@pytest.mark.parametrize("path,body", _SENDING_ROUTES)
def test_a_sending_admin_route_refuses_on_a_local_backend(app, db_path, local_backend, monkeypatch, path, body):
    import notify
    import requests
    for name in [n for n in dir(emails) if n.startswith("send_")] + ["deliver", "deliver_or_raise"]:
        monkeypatch.setattr(emails, name, lambda *a, **k: pytest.fail("reached a sender"), raising=False)
    monkeypatch.setattr(notify, "send_test_sms", lambda *a, **k: pytest.fail("texted"))
    monkeypatch.setattr(docusign_helper, "send_contract", lambda *a, **k: pytest.fail("reached DocuSign"))
    monkeypatch.setattr(docusign_helper, "resend_envelope", lambda *a, **k: pytest.fail("reached DocuSign"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("reached the network"))
    c, _ = _admin(app, db_path)
    rid = _rid(db_path, contract_status="sent", docusign_envelope_id="env_1", module_reviews=1,
               billing_status="past_due", stripe_customer_id="cus_1", owner_name="Erik Smith")
    uid = _owner(db_path, rid)
    r = _post(c, path.format(rid=rid, uid=uid), body)
    assert r.status_code == 409, (path, r.status_code, r.get_json())


def test_side_effect_sends_are_skipped_not_refused_on_a_local_backend(app, db_path, local_backend, monkeypatch):
    monkeypatch.setattr(emails, "send_password_reset_email", lambda *a, **k: pytest.fail("emailed"))
    monkeypatch.setattr(emails, "send_reactivation_email", lambda *a, **k: pytest.fail("emailed"))
    c, _ = _admin(app, db_path)
    made = _post(c, "/admin/api/support-logins", {"username": "helper2", "email": "helper2@cavnar.test"})
    assert made.status_code == 200 and made.get_json()["reset_link_sent"] is False
    assert "does not send" in made.get_json()["note"]
    rid = _rid(db_path, billing_status="active")
    uid = _owner(db_path, rid)
    _post(c, f"/admin/deactivate-client/{uid}")
    back = _post(c, f"/admin/reactivate-client/{uid}").get_json()
    assert back["ok"] is True and back["emailed"] is False and "local" in back["email_note"]


# ── lockouts name their login (#135, UI-2's request) ────────────────────────

def test_each_lockout_row_names_the_login_it_holds(app, db_path, monkeypatch):
    import security
    monkeypatch.setattr(security, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    uid = _owner(db_path, rid)
    for i in range(security.ACCOUNT_MAX):
        security.record_login_failure(f"10.9.0.{i}", "erik")
        security.record_login_failure(f"10.8.0.{i}", "nobody-here")
    rows = {r["username"]: r for r in c.get("/admin/api/lockouts").get_json()["lockouts"]}
    assert rows["erik"]["user_id"] == uid and rows["erik"]["restaurant_id"] == rid
    assert rows["nobody-here"]["user_id"] is None and rows["nobody-here"]["restaurant_id"] is None


# ── attribution on the pool and in the logs ─────────────────────────────────

def test_an_admin_jobs_ai_calls_are_the_admins(monkeypatch):
    import ai_utils
    monkeypatch.setattr(ai_utils, "_request_attribution", lambda: ("admin", 42, None))
    seen = {}
    with admin_routes._admin_job_attribution("job-9")():
        seen.update(ai_utils._CTX.get() or {})
    assert seen["trigger"] == "admin" and seen["actor_user_id"] == 42 and seen["correlation_id"] == "admin_job:job-9"
    monkeypatch.setattr(ai_utils, "_request_attribution", lambda: (None, None, None))
    with admin_routes._admin_job_attribution("job-10")():
        assert (ai_utils._CTX.get() or {})["trigger"] == "admin", "off a request it is still console work"


def test_a_signed_in_request_binds_the_login_to_its_log_lines(app, db_path):
    import logging_setup
    rid = _rid(db_path)
    uid = _owner(db_path, rid)
    seen = {}
    probe = Flask(__name__)

    @probe.route("/api/probe")
    @auth.login_required
    def _probe(current_user):
        seen.update(logging_setup.current())
        return "ok"
    c = probe.test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    assert c.get("/api/probe").status_code == 200
    assert seen["user_id"] == uid and seen["restaurant_id"] == rid
    logging_setup.clear()


def test_support_logins_read_the_console_not_the_legacy_pages(app, db_path):
    sup = create_user(_hq(db_path), "sup3", "sup3@cavnar.test", "Support-pass-2026", role="support", db_path=db_path)
    s = app.test_client()
    s.set_cookie("session_token", create_session(sup, db_path=db_path))
    rid = _rid(db_path)
    for path in (f"/admin/client-settings/{rid}", f"/admin/client-data/{rid}"):
        r = s.get(path)
        assert r.status_code == 403 and "Use the admin console" in r.get_data(as_text=True), path
    c, _ = _admin(app, db_path)
    assert c.get(f"/admin/client-data/{rid}").status_code == 200


def test_an_expired_console_read_answers_401_json_and_a_page_still_redirects(app, db_path):
    # The console's api() signs in again on a 401. A 302 to the login PAGE
    # was followed by fetch() and failed to parse, so an expired read showed
    # "HTTP 200" and never went back to sign-in (docs pass, §3 A).
    anon = app.test_client()
    r = anon.get("/admin/api/lockouts")
    assert r.status_code == 401 and r.get_json()["session_expired"] is True
    page = anon.get("/admin")
    assert page.status_code == 302 and "/login" in page.headers["Location"]
    rid = _rid(db_path)
    owner = app.test_client()
    owner.set_cookie("session_token", create_session(_owner(db_path, rid), db_path=db_path))
    assert owner.get("/admin/api/lockouts").status_code == 401


def test_the_legacy_alert_contact_writes_leave_the_same_typed_rows(app, db_path, monkeypatch):
    import notify
    monkeypatch.setattr(notify, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    monkeypatch.setattr(notify, "DB_PATH", db_path, raising=False)
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path)
    added = _post(c, f"/admin/alert-contacts/{rid}", {"name": "Erik", "phone": "+15125550123"}).get_json()
    assert added["ok"] is True
    assert _post(c, f"/admin/alert-contacts/delete/{added['id']}").get_json()["ok"] is True
    rows = {r["event_type"]: r for r in _rows(db_path, "SELECT * FROM admin_events WHERE restaurant_id=?", (rid,))}
    assert rows["alert_contact.added"]["actor_id"] == admin_uid
    assert json.loads(rows["alert_contact.added"]["after_json"])["phone_last4"] == "0123"
    assert json.loads(rows["alert_contact.removed"]["before_json"])["name"] == "Erik"
    typed = [rows["alert_contact.added"], rows["alert_contact.removed"]]
    assert "5125550123" not in json.dumps(typed), "a typed row keeps the number as its last four"
