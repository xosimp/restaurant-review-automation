"""Fix round B2 — the admin support routes (#9, #21, #22, #78, #86, #109,
#118, #127, #153).

What these protect:
  * Admin sends refuse on a local backend unless ALLOW_LOCAL_SCHEDULER=1,
    and every send answers with what really happened (a 502 and a sentence
    on failure).
  * Resend welcome targets the principal login, never changes a password,
    sends a set-password link, holds its cooldown only after a send that
    worked, and refuses a churned account.
  * Admin password tools send reset links; no password travels in a
    response or an email.
  * Test digest / test urgent alert name their recipient and can go to the
    admin instead of the owner.
  * Seed sample reviews is demo-only and labels only its own rows.
  * Re-draft every reply works in place and never touches an owner's edit.
  * The Instagram/Facebook refresh says which token failed.
  * Marking a client as a demo is refused for paying accounts and needs
    the typed name; the before-state is recorded.
  * The review-account seed keeps Apple's password unless rotation is
    confirmed, and a new password is readable once.
  * Slow admin work runs on the bounded admin job pool.
"""
import json
import types

import pytest
from flask import Flask

import admin_events
import admin_routes
import auth
import auth_routes
import client_api
import emails
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant, Review, save_reviews

CSRF = "fix-b2-support-csrf"
# The real pool entry, kept before the autouse fixture swaps in an inline runner.
_REAL_SUBMIT_ADMIN_JOB = admin_routes._submit_admin_job


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    auth_routes._login_attempts.clear()
    # Admin jobs run inline here, so a test sees their result. (One admin
    # job pool now — admin_routes._submit_admin_job — for every admin job.)
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *a: fn(*a))


@pytest.fixture
def sending(monkeypatch):
    """This process may send (as on Railway), and every email is captured."""
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    # A configured key: the one welcome (emails.send_welcome_with_set_password_link)
    # mints its link only when it can send.
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    sent = []

    def fake_deliver(payload=None, restaurant_id=None, email_type=None, log_send=True):
        sent.append({"payload": payload, "restaurant_id": restaurant_id, "email_type": email_type})
        return emails.SendResult(True, message_id="m1")
    monkeypatch.setattr(emails, "deliver", fake_deliver)
    return sent


@pytest.fixture
def local_backend(monkeypatch):
    monkeypatch.delenv("ALLOW_LOCAL_SCHEDULER", raising=False)
    monkeypatch.delenv("RESTORE_FROM", raising=False)
    for var in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "fix-b2-support"
    for bp in (admin_routes.admin_bp, auth_routes.auth_bp, client_api.client_bp):
        flask_app.register_blueprint(bp)
    return flask_app


@pytest.fixture
def admin(app, db_path):
    home = create_restaurant(Restaurant(name="Cavnar HQ", owner_email="will@cavnar.test"), db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("X-CSRF", CSRF)
    return c.post(url, headers=headers, **kw)


def _raw(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _owner_account(db_path, name="Simple Grill", email="erik@grill.test", **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email=email, owner_name="Erik Smith", **kw), db_path=db_path)
    uid = create_user(rid, email.split("@")[0], email, "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return rid, uid


def _staff_identity(db_path, rid):
    uid = create_user(rid, "dana.%d" % rid, "dana.%d@staff.invalid" % rid, "Unused-random-pw-123", db_path=db_path)
    upsert_membership(uid, rid, "employee", employee_name="Dana", db_path=db_path)
    return uid


def _hash(db_path, uid):
    return _raw(db_path, "SELECT password_hash FROM users WHERE id=?", (uid,))[0]["password_hash"]


# ── the local-backend guard (#9) ─────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/admin/resend-welcome/{rid}", "/admin/test-digest/{rid}",
                                  "/admin/test-urgent/{rid}", "/admin/reset-password-by-restaurant/{rid}"])
def test_admin_sends_refuse_on_a_local_backend(admin, db_path, local_backend, monkeypatch, path):
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda **k: sent.append(k) or emails.SendResult(True))
    rid, uid = _owner_account(db_path)
    before = _hash(db_path, uid)
    r = _post(admin, path.format(rid=rid), json={})
    assert r.status_code == 409 and r.get_json()["local_backend"] is True
    assert "ALLOW_LOCAL_SCHEDULER=1" in r.get_json()["error"]
    assert sent == [] and _hash(db_path, uid) == before


def test_a_pending_restore_blocks_sends_even_where_sending_is_allowed(admin, db_path, sending, monkeypatch):
    monkeypatch.setenv("RESTORE_FROM", "/data/snap.db")
    rid, _ = _owner_account(db_path)
    r = _post(admin, f"/admin/test-digest/{rid}", json={})
    assert r.status_code == 409 and "restore" in r.get_json()["error"] and sending == []


# ── resend welcome (#22) ─────────────────────────────────────────────────────

def test_resend_welcome_emails_the_principal_a_set_password_link_and_changes_nothing(admin, db_path, sending):
    rid, owner = _owner_account(db_path)
    staff = _staff_identity(db_path, rid)
    manager = create_user(rid, "mgr", "mgr@grill.test", "Manager-pass-2026", db_path=db_path)
    upsert_membership(manager, rid, "manager", db_path=db_path)
    session = create_session(owner, db_path=db_path)
    hashes = {u: _hash(db_path, u) for u in (owner, staff, manager)}
    r = _post(admin, f"/admin/resend-welcome/{rid}", json={})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True and body["sent_to"] == "erik@grill.test"
    assert {u: _hash(db_path, u) for u in (owner, staff, manager)} == hashes, "no password changed"
    assert auth.get_session_user(session, db_path=db_path), "the owner is still signed in"
    assert len(sending) == 1
    html = sending[0]["payload"]["html"]
    assert sending[0]["payload"]["to"] == ["erik@grill.test"] and sending[0]["restaurant_id"] == rid
    assert "/reset-password/" in html and "Owner-pass-2026" not in html and "erik" in html
    assert "Temporary password" not in html
    row = _raw(db_path, "SELECT after_json, target, result FROM admin_events WHERE event_type='welcome.resent'")[0]
    assert row["target"] == f"user:{owner}" and row["result"] == "ok"
    assert json.loads(row["after_json"])["password_changed"] is False


def test_a_double_clicked_resend_welcome_sends_once(admin, db_path, sending):
    rid, _ = _owner_account(db_path)
    first = _post(admin, f"/admin/resend-welcome/{rid}", json={}).get_json()
    second = _post(admin, f"/admin/resend-welcome/{rid}", json={}).get_json()
    assert first["ok"] and second["ok"] and second["already_sent"] is True
    assert len(sending) == 1


def test_a_failed_welcome_says_so_and_leaves_the_cooldown_free_for_the_retry(admin, db_path, monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    results = [emails.SendResult(False, error="Resend is down", status_code=503, attempts=3, reason="transient"),
               emails.SendResult(False, error="recipient suppressed (bounced/complained)", attempts=0,
                                 reason="suppressed"),
               emails.SendResult(True)]
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda **k: sent.append(k) or results.pop(0))
    rid, _ = _owner_account(db_path)
    r = _post(admin, f"/admin/resend-welcome/{rid}", json={})
    assert r.status_code == 502 and "didn't go out" in r.get_json()["error"]
    # A suppressed address is a refusal a retry won't change: 409, with where
    # to lift it (integration wave: resend-welcome answers 409 when suppressed).
    r = _post(admin, f"/admin/resend-welcome/{rid}", json={})
    assert r.status_code == 409 and r.get_json()["suppressed"] is True and "suppression list" in r.get_json()["error"]
    retry = _post(admin, f"/admin/resend-welcome/{rid}", json={})
    assert retry.status_code == 200 and retry.get_json().get("already_sent") is None and len(sent) == 3
    assert [r["result"] for r in _raw(db_path, "SELECT result FROM admin_events WHERE event_type='welcome.resent' ORDER BY id")] \
        == ["failed", "refused", "ok"]


def test_resend_welcome_refuses_a_churned_account_and_one_closing(admin, db_path, sending):
    rid, _ = _owner_account(db_path, billing_status="churned")
    assert _post(admin, f"/admin/resend-welcome/{rid}", json={}).status_code == 409
    rid2, _ = _owner_account(db_path, name="Closing Grill", email="c@grill.test")
    models.request_account_deletion(rid2, db_path=db_path)
    assert _post(admin, f"/admin/resend-welcome/{rid2}", json={}).status_code == 409
    assert sending == []


def test_resend_welcome_with_only_a_staff_identity_sends_nothing(admin, db_path, sending):
    rid = create_restaurant(Restaurant(name="Staff Only", owner_email="o@x.test"), db_path=db_path)
    _staff_identity(db_path, rid)
    assert _post(admin, f"/admin/resend-welcome/{rid}", json={}).status_code == 404
    assert sending == []


# ── reset links, never passwords (#86, #109) ─────────────────────────────────

def test_admin_reset_sends_a_link_and_never_sets_or_returns_a_password(admin, db_path, sending):
    rid, owner = _owner_account(db_path)
    before = _hash(db_path, owner)
    r = _post(admin, f"/admin/reset-password/{owner}", json={"password": "Chosen-by-admin-2026", "send_email": True})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["password_ignored"] is True
    assert "password" not in body and "Chosen-by-admin-2026" not in json.dumps(body)
    assert _hash(db_path, owner) == before
    assert len(sending) == 1 and "Chosen-by-admin-2026" not in sending[0]["payload"]["html"]
    assert "/reset-password/" in sending[0]["payload"]["html"]


def test_the_emailed_link_sets_the_password_ends_sessions_and_lifts_a_freeze(admin, db_path, sending):
    rid, owner = _owner_account(db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET must_reset_password=1 WHERE id=?", (owner,))
    conn.commit(); conn.close()
    web = create_session(owner, db_path=db_path)
    _post(admin, f"/admin/reset-password-by-restaurant/{rid}", json={})
    import re
    token = re.search(r"/reset-password/([A-Za-z0-9_\-]+)", sending[0]["payload"]["html"]).group(1)
    assert models.consume_reset_token(token, "Owner-chosen-2026!", db_path=db_path)
    assert auth.get_session_user(web, db_path=db_path) is None
    assert _raw(db_path, "SELECT must_reset_password FROM users WHERE id=?", (owner,))[0]["must_reset_password"] == 0
    from werkzeug.security import check_password_hash
    assert check_password_hash(_hash(db_path, owner), "Owner-chosen-2026!")


def test_a_reset_link_that_did_not_go_answers_502(admin, db_path, monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    monkeypatch.setattr(emails, "deliver", lambda **k: emails.SendResult(False, error="RESEND_API_KEY or recipient missing",
                                                                       attempts=0))
    _, owner = _owner_account(db_path)
    r = _post(admin, f"/admin/send-reset-link/{owner}", json={})
    assert r.status_code == 502 and "isn't configured" in r.get_json()["error"]
    assert _raw(db_path, "SELECT result FROM admin_events WHERE event_type='password.reset_link_sent'")[0]["result"] == "failed"


def test_reset_links_refuse_admin_logins_and_staff_identities(admin, db_path, sending):
    rid, _ = _owner_account(db_path)
    staff = _staff_identity(db_path, rid)
    admin_id = _raw(db_path, "SELECT id FROM users WHERE username='will'")[0]["id"]
    assert _post(admin, f"/admin/send-reset-link/{admin_id}", json={}).status_code == 403
    assert _post(admin, f"/admin/send-reset-link/{staff}", json={}).status_code == 409
    assert sending == []


# ── test sends (#109, #127) ──────────────────────────────────────────────────

@pytest.mark.parametrize("path,email_type", [("/admin/test-digest/{rid}", "digest_preview"),
                                             ("/admin/test-urgent/{rid}", "alert_test")])
def test_test_sends_name_the_recipient_and_can_go_to_the_admin(admin, db_path, sending, path, email_type):
    rid, _ = _owner_account(db_path)
    r = _post(admin, path.format(rid=rid), json={})
    assert r.get_json()["sent_to"] == "erik@grill.test" and r.get_json()["to"] == "owner"
    me = _post(admin, path.format(rid=rid), json={"to": "me"})
    assert me.get_json()["sent_to"] == "will@cavnar.test" and "(you)" in me.get_json()["message"]
    assert [s["payload"]["to"] for s in sending] == [["erik@grill.test"], ["will@cavnar.test"]]
    assert {s["email_type"] for s in sending} == {email_type}


def test_a_test_urgent_alert_uses_the_real_alert_template_and_reports_a_failed_send(admin, db_path, monkeypatch):
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    seen = []
    monkeypatch.setattr(emails, "deliver", lambda **k: seen.append(k) or emails.SendResult(False, status_code=422,
                                                                                         error="invalid"))
    rid, _ = _owner_account(db_path)
    r = _post(admin, f"/admin/test-urgent/{rid}", json={})
    assert r.status_code == 502 and "didn't go out" in r.get_json()["error"]
    html = seen[0]["payload"]["html"]
    assert "Test urgent review alert" in html and "{{CTA}}" not in html and "/?tab=reviews" in html


# ── seed sample reviews (#21) ────────────────────────────────────────────────

def test_seed_reviews_refuses_a_real_client(admin, db_path):
    rid, _ = _owner_account(db_path)
    r = _post(admin, f"/admin/seed-reviews/{rid}", json={})
    assert r.status_code == 400
    assert _raw(db_path, "SELECT COUNT(*) AS n FROM reviews WHERE restaurant_id=?", (rid,))[0]["n"] == 0


def test_seed_reviews_labels_only_its_own_rows_and_drafts_them_on_the_pool(admin, db_path, monkeypatch):
    import drafter
    rid, _ = _owner_account(db_path, is_demo=1)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="real-1", author="Real",
                         rating=2, text="A real pending review")], db_path=db_path)
    drafted = []

    def fake_draft(review_id, *a, **k):
        drafted.append(review_id)
        models.update_draft(review_id, "drafted reply", db_path=db_path)
        return "drafted reply"
    monkeypatch.setattr(drafter, "draft_response", fake_draft)
    r = _post(admin, f"/admin/seed-reviews/{rid}", json={})
    body = r.get_json()
    assert r.status_code == 200 and body["seeded"] == 12 and body["job_id"]
    real = _raw(db_path, "SELECT processed, sentiment FROM reviews WHERE external_id='real-1'")[0]
    assert real["processed"] == 0 and real["sentiment"] is None, "a real pending review is not labelled"
    assert len(drafted) == 12
    job = admin.get(f"/admin/api/admin-jobs/{body['job_id']}").get_json()
    assert job["ok"] and job["drafted"] == 12


# ── redraft in place (#78, #153) ─────────────────────────────────────────────

def test_redraft_all_rewrites_in_place_and_never_touches_an_owners_edit(admin, db_path, monkeypatch):
    import drafter
    rid, _ = _owner_account(db_path)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=f"r{i}", author="A", rating=4,
                         text=f"Review {i}") for i in range(3)], db_path=db_path)
    ids = [r["id"] for r in _raw(db_path, "SELECT id FROM reviews WHERE restaurant_id=? ORDER BY id", (rid,))]
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET sentiment='positive', processed=1, response_status='drafted', "
                 "draft_response='old draft' WHERE restaurant_id=?", (rid,))
    conn.execute("UPDATE reviews SET draft_response='the owner''s words', draft_edited=1 WHERE id=?", (ids[0],))
    conn.execute("UPDATE reviews SET response_status='posted', draft_response='live reply' WHERE id=?", (ids[2],))
    conn.commit(); conn.close()
    calls = []

    def fake_draft(review_id, *a, **k):
        calls.append(review_id)
        models.update_draft(review_id, f"new draft {review_id}", db_path=db_path)
        return "x"
    monkeypatch.setattr(drafter, "draft_response", fake_draft)
    r = _post(admin, f"/admin/redraft-all/{rid}", json={})
    assert r.status_code == 200 and r.get_json()["job_id"]
    rows = {x["id"]: x for x in _raw(db_path, "SELECT id, draft_response, response_status FROM reviews WHERE restaurant_id=?", (rid,))}
    assert rows[ids[0]]["draft_response"] == "the owner's words", "an owner's edit is never touched"
    assert rows[ids[1]]["draft_response"] == f"new draft {ids[1]}"
    assert rows[ids[2]]["draft_response"] == "live reply" and rows[ids[2]]["response_status"] == "posted"
    assert calls == [ids[1]]
    job = admin.get(f"/admin/api/admin-jobs/{r.get_json()['job_id']}").get_json()
    assert job["redrafted"] == 1 and job["kept_owner_edits"] == 1 and job["remaining"] == 0


def test_redraft_all_never_blanks_a_draft_when_drafting_fails(admin, db_path, monkeypatch):
    import drafter
    rid, _ = _owner_account(db_path)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="r1", author="A", rating=4,
                         text="Fine")], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET sentiment='positive', processed=1, response_status='drafted', "
                 "draft_response='old draft' WHERE restaurant_id=?", (rid,))
    conn.commit(); conn.close()
    monkeypatch.setattr(drafter, "draft_response", lambda *a, **k: (_ for _ in ()).throw(ValueError("model down")))
    r = _post(admin, f"/admin/redraft-all/{rid}", json={})
    job = admin.get(f"/admin/api/admin-jobs/{r.get_json()['job_id']}")
    assert job.status_code == 502 and job.get_json()["failed"] == 1
    assert _raw(db_path, "SELECT draft_response FROM reviews WHERE restaurant_id=?", (rid,))[0]["draft_response"] == "old draft"


# ── Instagram / Facebook refresh (#109, #153) ────────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def test_a_failed_facebook_refresh_is_reported_not_hidden(admin, db_path, monkeypatch):
    # Through scheduler.refresh_ig_token now — the one refresh (integration
    # wave, D #65) — which needs the Meta app's id and secret.
    import requests
    monkeypatch.setenv("META_APP_ID", "app-1")
    monkeypatch.setenv("META_APP_SECRET", "app-secret")
    rid, _ = _owner_account(db_path)
    update_restaurant(rid, {"ig_token": "ig-old", "fb_page_token": "fb-old"}, db_path=db_path)
    replies = {"ig-old": _Resp(200, {"access_token": "ig-new", "expires_in": 5184000}),
               "fb-old": _Resp(400, {"error": {"message": "Session has expired"}})}
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None: replies[params["fb_exchange_token"]])
    r = _post(admin, f"/admin/refresh-ig-token/{rid}", json={})
    assert r.status_code == 200 and r.get_json()["job_id"]
    job = admin.get(f"/admin/api/admin-jobs/{r.get_json()['job_id']}")
    body = job.get_json()
    assert job.status_code == 502 and body["refreshed"] == {"instagram": True, "facebook": False}
    assert body["expires"] and body["ok"] is False
    assert "Facebook page token NOT refreshed" in body["error"] and "Instagram token refreshed" in body["error"]
    rest = get_restaurant(rid, db_path=db_path)
    assert rest.ig_token == "ig-new" and rest.fb_page_token == "fb-old"
    assert _raw(db_path, "SELECT result FROM admin_events WHERE event_type='meta_tokens.refreshed'")[0]["result"] == "partial"


# ── demo flag (#118) and delete-demo (#126) ──────────────────────────────────

def test_a_paying_account_cannot_be_marked_as_a_demo(admin, db_path):
    rid, _ = _owner_account(db_path, billing_status="active")
    r = _post(admin, f"/admin/api/client/{rid}/demo", json={"is_demo": 1, "confirm_name": "Simple Grill"})
    assert r.status_code == 409 and "billing status is active" in r.get_json()["error"]
    assert get_restaurant(rid, db_path=db_path).is_demo == 0
    rid2, _ = _owner_account(db_path, name="Stripe Grill", email="s@grill.test", stripe_customer_id="cus_9")
    assert _post(admin, f"/admin/api/client/{rid2}/demo",
                 json={"is_demo": 1, "confirm_name": "Stripe Grill"}).status_code == 409


def test_marking_a_demo_needs_the_typed_name_and_records_the_before_state(admin, db_path):
    rid, _ = _owner_account(db_path)
    r = _post(admin, f"/admin/api/client/{rid}/demo", json={"is_demo": 1})
    assert r.status_code == 400 and r.get_json()["confirm_required"] is True
    r = _post(admin, f"/admin/api/client/{rid}/demo", json={"is_demo": 1, "confirm_name": "Simple Grill"})
    assert r.status_code == 200 and get_restaurant(rid, db_path=db_path).is_demo == 1
    row = _raw(db_path, "SELECT before_json, after_json FROM admin_events WHERE event_type='demo_flag.set' "
                        "AND result='ok'")[0]
    assert json.loads(row["before_json"])["is_demo"] == 0 and json.loads(row["after_json"])["is_demo"] == 1
    # Back to a real client needs no name.
    assert _post(admin, f"/admin/api/client/{rid}/demo", json={"is_demo": 0}).status_code == 200


def test_deleting_a_demo_keeps_its_audit_row(admin, db_path):
    rid, _ = _owner_account(db_path, is_demo=1)
    r = _post(admin, f"/admin/api/client/{rid}/delete-demo", json={"confirm_name": "Simple Grill"})
    assert r.status_code == 200 and get_restaurant(rid, db_path=db_path) is None
    assert _raw(db_path, "SELECT event_type FROM admin_events WHERE restaurant_id=? AND source='admin'", (rid,)) == \
        [{"event_type": "demo.deleted"}]


# ── the review account (#127, #153) ──────────────────────────────────────────

def test_the_review_account_keeps_its_password_unless_rotation_is_confirmed(admin, db_path, monkeypatch):
    import subprocess
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        pw = "(unchanged — pass without --keep-password to rotate)" if "--keep-password" in args else "abcde-FGHJK-23456"
        return types.SimpleNamespace(returncode=0, stderr="",
                                     stdout=f"Review account already exists (user 5, restaurant 9).\n  Username: appreview\n  Password: {pw}\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    r = _post(admin, "/admin/seed-review-account", json={})
    assert r.status_code == 200 and r.get_json()["rotate_password"] is False
    assert "--keep-password" in calls[-1]
    job = admin.get(f"/admin/api/admin-jobs/{r.get_json()['job_id']}").get_json()
    assert job["ok"] and "password_once" not in job
    assert _post(admin, "/admin/seed-review-account", json={"rotate_password": True}).status_code == 400
    r = _post(admin, "/admin/seed-review-account", json={"rotate_password": True, "confirm": "rotate"})
    assert "--keep-password" not in calls[-1]
    first = admin.get(f"/admin/api/admin-jobs/{r.get_json()['job_id']}").get_json()
    assert first["password_once"] == "abcde-FGHJK-23456" and "abcde-FGHJK-23456" not in first["output"]
    again = admin.get(f"/admin/api/admin-jobs/{r.get_json()['job_id']}").get_json()
    assert "password_once" not in again, "the new password is readable once"
    stored = _raw(db_path, "SELECT result_json FROM async_jobs WHERE job_id=?", (r.get_json()["job_id"],))[0]
    assert "abcde-FGHJK-23456" not in stored["result_json"]


# ── the admin job pool (#153) ────────────────────────────────────────────────

def test_the_admin_pool_is_bounded_and_a_full_queue_refuses(db_path, monkeypatch):
    # One bounded pool for every admin job (integration wave): B2's jobs go
    # through B1's executor, and a full pool refuses rather than queueing.
    monkeypatch.setattr(admin_routes, "_submit_admin_job", _REAL_SUBMIT_ADMIN_JOB)
    monkeypatch.setattr(admin_routes, "ADMIN_JOB_QUEUE_MAX", 0)
    job_id, joined = admin_routes._start_admin_job("admin_redraft", 7, lambda: {"ok": True})
    assert (job_id, joined) == (None, False)
    src = open(admin_routes.__file__).read()
    assert "class _AdminJobs" not in src and "_submit_admin_job(job_id, _run)" in src


def test_a_second_press_joins_the_running_job(db_path, monkeypatch):
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *a: None)     # stays pending
    a, joined_a = admin_routes._start_admin_job("admin_redraft", 5, lambda: {"ok": True})
    b, joined_b = admin_routes._start_admin_job("admin_redraft", 5, lambda: {"ok": True})
    assert a == b and not joined_a and joined_b
