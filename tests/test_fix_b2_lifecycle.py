"""Fix round B2 — account lifecycle: deletion requests, offboarding, bug
reports, support notes and Add location (#34, #42, #55, #138).

What these protect:
  * An open "Close my account" request has a checklist with a 30-day due
    date; steps with nothing to do read 'not needed'; a skip needs a reason;
    the integrations step clears every stored credential and turns webhooks
    off, with an audit row that never holds a credential.
  * A request can be withdrawn (it was write-once), and only the request
    that is open now.
  * A real restaurant can be deleted — only behind its exact name, a
    finished checklist, and never while an admin login lives on it — and the
    audit trail and checklist survive the delete.
  * The deletion notice's send result is checked, and nothing is emailed
    from a local backend.
  * In-app bug reports are stored before anything is emailed.
  * Support notes are authored, dated and append-only.
  * Add location creates the restaurant under the brand's owner and group
    (and organization) with no new login.
"""
import json

import pytest
from flask import Flask

import admin_events
import admin_routes
import auth
import auth_routes
import client_api
import mobile_api
import models
import offboarding
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

CSRF = "fix-b2-life-csrf"


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    auth_routes._login_attempts.clear()


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "fix-b2-life"
    for bp in (admin_routes.admin_bp, auth_routes.auth_bp, client_api.client_bp, mobile_api.mobile_bp):
        flask_app.register_blueprint(bp)
    return flask_app


def _admin_client(app, db_path):
    home = create_restaurant(Restaurant(name="Cavnar HQ", owner_email="will@cavnar.test"), db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c, home


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


def _client_account(db_path, name="Leaving Grill", email="owner@leaving.test", **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email=email, **kw), db_path=db_path)
    uid = create_user(rid, name.split()[0].lower(), email, "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return rid, uid


# ── the checklist ────────────────────────────────────────────────────────────

def test_a_request_has_a_30_day_due_date_and_steps_with_nothing_to_do_read_not_needed(db_path):
    rid, _ = _client_account(db_path)
    requested = models.request_account_deletion(rid, db_path=db_path)
    state = offboarding.checklist(rid, db_path=db_path)
    assert state["request"]["deletion_requested_at"] == requested
    assert state["request"]["days_left"] in (29, 30) and not state["request"]["overdue"]
    assert state["request"]["due_on"].count("/") == 2, "M/D/YY"
    status = {s["step"]: s["status"] for s in state["steps"]}
    assert status == {"stripe": "not_needed", "docusign": "not_needed", "integrations": "not_needed",
                      "export": "pending"}
    assert state["outstanding"] == ["export"] and not state["ready_to_delete"]


def test_steps_with_work_stay_pending_until_marked(db_path):
    rid, _ = _client_account(db_path, stripe_customer_id="cus_1", billing_status="active")
    update_restaurant(rid, {"rpower_token": "tok-123", "contract_status": "sent"}, db_path=db_path)
    state = offboarding.checklist(rid, db_path=db_path)
    assert [s["step"] for s in state["steps"] if s["status"] == "pending"] == ["stripe", "docusign", "integrations",
                                                                               "export"]


def test_a_skip_needs_a_reason_and_every_mark_is_audited(db_path):
    rid, _ = _client_account(db_path)
    payload, code = offboarding.set_step(rid, "export", "skipped", {"id": 1, "username": "will"}, db_path=db_path)
    assert code == 400
    payload, code = offboarding.set_step(rid, "export", "skipped", {"id": 1, "username": "will"},
                                         note="Owner declined an export", db_path=db_path)
    assert code == 200 and payload["ready_to_delete"] is True
    row = _raw(db_path, "SELECT event_type, before_json, after_json, actor FROM admin_events WHERE restaurant_id=?",
               (rid,))[-1]
    assert row["event_type"] == "offboarding.export" and row["actor"] == "will"
    assert json.loads(row["before_json"]) == {"status": "pending"}
    assert json.loads(row["after_json"])["status"] == "skipped"


def test_revoke_integrations_clears_every_credential_and_webhook_and_audits_without_values(db_path):
    import webhooks
    webhooks.init_webhooks(db_path=db_path)
    rid, _ = _client_account(db_path)
    update_restaurant(rid, {"gmb_refresh_token": "g-refresh", "gmb_account_id": "acc", "ig_token": "ig-tok",
                            "fb_page_token": "fb-tok", "toast_client_secret": "t-secret", "toast_client_id": "t-id",
                            "square_access_token": "sq", "clover_api_token": "cl", "rpower_token": "rp",
                            "backoffice_api_key": "bo"}, db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO webhooks (restaurant_id, url, secret, events, is_active) VALUES (?, 'https://h.test', 's', '*', 1)",
                 (rid,))
    conn.commit(); conn.close()
    out = offboarding.revoke_integrations(rid, {"id": 1, "username": "will"}, db_path=db_path)
    assert out["ok"] and out["webhooks_disabled"]
    assert set(out["cleared"]) == {"google", "meta", "toast", "square", "clover", "rpower", "backoffice"}
    raw = _raw(db_path, "SELECT * FROM restaurants WHERE id=?", (rid,))[0]
    for cols in offboarding.INTEGRATION_FIELDS.values():
        for c in cols:
            assert raw.get(c) in (None, ""), c
    assert _raw(db_path, "SELECT is_active FROM webhooks WHERE restaurant_id=?", (rid,))[0]["is_active"] == 0
    row = _raw(db_path, "SELECT before_json, after_json FROM admin_events WHERE event_type='integrations.revoked'")[0]
    blob = row["before_json"] + row["after_json"]
    for secret in ("g-refresh", "ig-tok", "fb-tok", "t-secret", "sq", "rp", "bo"):
        assert f'"{secret}"' not in blob
    assert json.loads(row["before_json"])["connected"]["google"] is True
    state = offboarding.checklist(rid, db_path=db_path)
    assert {s["step"]: s["status"] for s in state["steps"]}["integrations"] == "not_needed"


def test_marking_the_integrations_step_done_runs_the_revoke(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid, _ = _client_account(db_path)
    update_restaurant(rid, {"ig_token": "ig-tok"}, db_path=db_path)
    r = _post(c, f"/admin/api/client/{rid}/offboarding/integrations", json={"status": "done"})
    body = r.get_json()
    assert r.status_code == 200 and body["revoked"]["cleared"] == {"meta": ["ig_token"]}
    assert get_restaurant(rid, db_path=db_path).ig_token in (None, "")
    assert {s["step"]: s["status"] for s in body["steps"]}["integrations"] == "done"


# ── withdraw ─────────────────────────────────────────────────────────────────

def test_withdrawing_clears_the_request_and_is_recorded(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid, _ = _client_account(db_path)
    requested = models.request_account_deletion(rid, db_path=db_path)
    r = _post(c, f"/admin/api/client/{rid}/deletion/withdraw", json={"note": "Owner called: staying"})
    assert r.status_code == 200 and r.get_json()["withdrawn"] == requested
    assert models.get_deletion_requested_at(rid, db_path=db_path) is None
    row = _raw(db_path, "SELECT before_json, after_json FROM admin_events WHERE event_type='deletion_request.withdrawn'")[0]
    assert json.loads(row["before_json"])["deletion_requested_at"] == requested
    assert _post(c, f"/admin/api/client/{rid}/deletion/withdraw", json={}).status_code == 409
    # A later request starts a fresh checklist: the withdrawn one's ticks don't carry over.
    offboarding._upsert_step(rid, requested, "export", "done", "will", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET deletion_requested_at='2026-12-01 09:00:00' WHERE id=?", (rid,))
    conn.commit(); conn.close()
    assert {s["step"]: s["status"] for s in offboarding.checklist(rid, db_path=db_path)["steps"]}["export"] == "pending"


def test_a_withdraw_never_clears_a_newer_request(db_path, monkeypatch):
    rid, _ = _client_account(db_path)
    models.request_account_deletion(rid, db_path=db_path)
    real = offboarding._raw_restaurant

    def stale(restaurant_id, db_path=None):
        row = real(restaurant_id, db_path=db_path)
        row["deletion_requested_at"] = "2020-01-01 00:00:00"      # what the page was loaded with
        return row
    monkeypatch.setattr(offboarding, "_raw_restaurant", stale)
    payload, code = offboarding.withdraw_deletion_request(rid, "will", db_path=db_path)
    assert code == 409 and models.get_deletion_requested_at(rid, db_path=db_path)


# ── deleting a real restaurant ───────────────────────────────────────────────

def test_a_real_restaurant_is_deleted_only_behind_its_name_and_a_finished_checklist(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid, _ = _client_account(db_path)
    models.request_account_deletion(rid, db_path=db_path)
    r = _post(c, f"/admin/api/client/{rid}/delete", json={"confirm_name": "leaving grill"})
    assert r.status_code == 400 and get_restaurant(rid, db_path=db_path)
    r = _post(c, f"/admin/api/client/{rid}/delete", json={"confirm_name": "Leaving Grill"})
    assert r.status_code == 409 and r.get_json()["outstanding"] == ["export"]
    _post(c, f"/admin/api/client/{rid}/offboarding/export", json={"status": "done", "note": "emailed CSVs"})
    r = _post(c, f"/admin/api/client/{rid}/delete", json={"confirm_name": "Leaving Grill"})
    assert r.status_code == 200 and r.get_json()["rows"] >= 2
    assert get_restaurant(rid, db_path=db_path) is None
    kept = _raw(db_path, "SELECT event_type FROM admin_events WHERE restaurant_id=? ORDER BY id", (rid,))
    assert "restaurant.deleted" in [k["event_type"] for k in kept] and "offboarding.export" in [k["event_type"] for k in kept]
    steps = _raw(db_path, "SELECT step, status FROM offboarding_steps WHERE restaurant_id=?", (rid,))
    assert {"step": "delete", "status": "done"} in steps
    audit = c.get(f"/admin/api/client/{rid}/audit").get_json()["events"]
    assert audit[0]["action"] == "restaurant.deleted", "the audit view still answers for a deleted client"


def test_the_admins_home_restaurant_cannot_be_deleted(app, db_path):
    c, home = _admin_client(app, db_path)
    offboarding.set_step(home, "export", "skipped", "will", note="internal", db_path=db_path)
    r = _post(c, f"/admin/api/client/{home}/delete", json={"confirm_name": "Cavnar HQ"})
    assert r.status_code == 409 and get_restaurant(home, db_path=db_path)


def test_support_cannot_delete(app, db_path):
    home = create_restaurant(Restaurant(name="HQ", owner_email="s@c.test"), db_path=db_path)
    sup = create_user(home, "sam", "sam@c.test", "Support-pass-2026", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET role='support' WHERE id=?", (sup,))
    conn.execute("DELETE FROM memberships WHERE user_id=?", (sup,))
    conn.commit(); conn.close()
    rid, _ = _client_account(db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(sup, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    assert _post(c, f"/admin/api/client/{rid}/delete", json={"confirm_name": "Leaving Grill"}).status_code == 403


def test_the_open_requests_list_is_soonest_due_first(app, db_path):
    c, _ = _admin_client(app, db_path)
    a, _ = _client_account(db_path, name="Alpha Grill", email="a@x.test")
    b, _ = _client_account(db_path, name="Bravo Grill", email="b@x.test")
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET deletion_requested_at='2026-09-01 10:00:00' WHERE id=?", (b,))
    conn.execute("UPDATE restaurants SET deletion_requested_at='2026-09-20 10:00:00' WHERE id=?", (a,))
    conn.commit(); conn.close()
    reqs = c.get("/admin/api/deletion-requests").get_json()["requests"]
    assert [r["restaurant_id"] for r in reqs] == [b, a]
    assert reqs[0]["due_on"] == "10/1/26"


# ── the deletion notice ──────────────────────────────────────────────────────

def _mobile_login(app, db_path, rid, username="closer", password="Owner-pass-2026"):
    create_user(rid, username, f"{username}@x.test", password, db_path=db_path)
    return app.test_client().post("/mobile/api/login", json={"username": username, "password": password}).get_json()["token"]


def test_a_new_request_is_recorded_for_the_console_and_a_failed_notice_is_captured(app, db_path, monkeypatch):
    import ops
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append((str(e), k)))
    import emails
    monkeypatch.setattr(emails, "send_account_deletion_request_email",
                        lambda *a, **k: emails.SendResult(False, error="recipient suppressed (bounced/complained)"))
    rid = create_restaurant(Restaurant(name="Closing Co", owner_email="c@x.test"), db_path=db_path)
    token = _mobile_login(app, db_path, rid)
    r = app.test_client().post("/mobile/api/account/request-deletion", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert _raw(db_path, "SELECT source, event_type FROM admin_events WHERE restaurant_id=?", (rid,)) == \
        [{"source": "account", "event_type": "deletion.requested"}]
    assert captured and captured[0][1]["job"] == "account_deletion_notice"
    assert f"restaurant_id={rid}" in captured[0][1]["context"]


def test_a_local_backend_records_the_request_but_emails_nothing(app, db_path, monkeypatch):
    monkeypatch.delenv("ALLOW_LOCAL_SCHEDULER", raising=False)
    for var in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"):
        monkeypatch.delenv(var, raising=False)
    import emails
    sent = []
    monkeypatch.setattr(emails, "send_account_deletion_request_email", lambda *a, **k: sent.append(a) or True)
    rid = create_restaurant(Restaurant(name="Local Co", owner_email="l@x.test"), db_path=db_path)
    token = _mobile_login(app, db_path, rid)
    r = app.test_client().post("/mobile/api/account/request-deletion", headers={"Authorization": f"Bearer {token}"})
    assert r.get_json()["ok"] is True and models.get_deletion_requested_at(rid, db_path=db_path)
    assert sent == []


# ── bug reports ──────────────────────────────────────────────────────────────

def test_a_bug_report_is_stored_before_it_is_emailed_and_survives_a_failed_email(app, db_path, monkeypatch):
    import ops
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)

    def boom(*a, **k):
        raise RuntimeError("resend is down")
    monkeypatch.setattr("emails.send_bug_report_email", boom)
    rid = create_restaurant(Restaurant(name="Buggy Co", owner_email="b@x.test"), db_path=db_path)
    token = _mobile_login(app, db_path, rid, username="reporter")
    r = app.test_client().post("/mobile/api/account/report-bug", headers={"Authorization": f"Bearer {token}"},
                               json={"message": "The labor chart is blank", "build": "b42"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    rows = _raw(db_path, "SELECT * FROM bug_reports")
    assert len(rows) == 1 and rows[0]["message"] == "The labor chart is blank"
    assert rows[0]["restaurant_id"] == rid and rows[0]["notified"] == 0 and "resend is down" in rows[0]["notify_error"]
    assert json.loads(rows[0]["meta_json"])["build"] == "b42"


def test_the_console_lists_and_closes_bug_reports(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = create_restaurant(Restaurant(name="Buggy Co", owner_email="b@x.test"), db_path=db_path)
    rep = admin_events.store_bug_report(rid, {"id": 3, "username": "erik", "email": "e@x.test"}, "It crashed",
                                        {"device": "iPhone"}, db_path=db_path)
    body = c.get("/admin/api/bug-reports?status=open").get_json()
    assert body["open"] == 1 and body["reports"][0]["restaurant"] == "Buggy Co"
    assert body["reports"][0]["meta"] == {"device": "iPhone"}
    r = _post(c, f"/admin/api/bug-reports/{rep}/status", json={"status": "closed", "note": "fixed in 2.4"})
    assert r.get_json()["report"]["status"] == "closed" and r.get_json()["report"]["closed_by"] == "will"
    assert c.get("/admin/api/bug-reports?status=open").get_json()["reports"] == []


# ── support notes ────────────────────────────────────────────────────────────

def test_support_notes_are_authored_dated_append_only_beside_the_old_field(app, db_path):
    c, _ = _admin_client(app, db_path)
    rid = create_restaurant(Restaurant(name="Noted Co", owner_email="n@x.test"), db_path=db_path)
    update_restaurant(rid, {"internal_notes": "Signed May 2026"}, db_path=db_path)
    assert _post(c, f"/admin/api/client/{rid}/notes", json={"body": "  "}).status_code == 400
    for text in ("Called about Toast", "Wants weekly digest on Tuesdays"):
        r = _post(c, f"/admin/api/client/{rid}/notes", json={"body": text})
        assert r.status_code == 200 and r.get_json()["note"]["author"] == "will"
    body = c.get(f"/admin/api/client/{rid}/notes").get_json()
    assert [n["body"] for n in body["notes"]] == ["Wants weekly digest on Tuesdays", "Called about Toast"]
    assert all(n["created_at"] for n in body["notes"])
    assert body["legacy_internal_notes"] == "Signed May 2026"
    rules = {r.rule: r.methods for r in app.url_map.iter_rules() if "/notes" in r.rule}
    assert all(not ({"DELETE", "PUT", "PATCH"} & set(m)) for m in rules.values()), "no edit or delete path"


# ── Add location ─────────────────────────────────────────────────────────────

def test_add_location_uses_the_brands_owner_and_group_with_no_new_login(app, db_path):
    c, _ = _admin_client(app, db_path)
    first, owner = _client_account(db_path, name="Syrup Lakeview", email="erik@syrup.test",
                                   billing_status="active", timezone="America/Denver")
    users_before = _raw(db_path, "SELECT COUNT(*) AS n FROM users")[0]["n"]
    r = _post(c, "/admin/api/brand/add-location", json={"from_restaurant_id": first,
                                                         "restaurant_name": "Syrup Wicker Park",
                                                         "location_name": "Wicker Park"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"], body
    new = body["restaurant_id"]
    assert _raw(db_path, "SELECT COUNT(*) AS n FROM users")[0]["n"] == users_before, "no new login"
    a, b = get_restaurant(first, db_path=db_path), get_restaurant(new, db_path=db_path)
    assert b.owner_email == "erik@syrup.test" and a.location_group == b.location_group == "Syrup Lakeview"
    assert b.billing_status == "active", "covered by the brand's subscription (decision 1)"
    assert b.timezone == "America/Denver"
    orgs = _raw(db_path, "SELECT id, organization_id FROM restaurants WHERE id IN (?,?)", (first, new))
    assert orgs[0]["organization_id"] and orgs[0]["organization_id"] == orgs[1]["organization_id"]
    login = _raw(db_path, "SELECT role FROM users WHERE id=?", (owner,))[0]
    assert login["role"] == "owner" and body["promoted_to_owner"] is True
    roles = {m["restaurant_id"]: m["role"] for m in
             _raw(db_path, "SELECT restaurant_id, role FROM memberships WHERE user_id=?", (owner,))}
    assert roles == {first: "owner", new: "owner"}
    ev = _raw(db_path, "SELECT event_type, target FROM admin_events WHERE source='admin' AND restaurant_id=?", (new,))
    assert ev == [{"event_type": "location.added", "target": f"restaurant:{first}"}]
    # The owner can now switch to it (auth._still_in_group + LOCATION_SWITCH).
    token = create_session(owner, db_path=db_path)
    owner_c = app.test_client()
    owner_c.set_cookie("session_token", token)
    owner_c.set_cookie("csrf_js", CSRF)
    locs = owner_c.get("/api/group-locations").get_json()["locations"]
    assert {l["id"] for l in locs} == {first, new}


def test_add_location_refuses_a_group_name_another_owner_uses(app, db_path):
    c, _ = _admin_client(app, db_path)
    first, _ = _client_account(db_path, name="Taco Uno", email="one@taco.test", location_group="Taco")
    create_restaurant(Restaurant(name="Other Taco", owner_email="two@taco.test", location_group="Taco"), db_path=db_path)
    r = _post(c, "/admin/api/brand/add-location", json={"from_restaurant_id": first, "restaurant_name": "Taco Dos"})
    assert r.status_code == 409


def test_add_location_needs_a_name_and_a_real_brand(app, db_path):
    c, _ = _admin_client(app, db_path)
    first, _ = _client_account(db_path)
    assert _post(c, "/admin/api/brand/add-location", json={"from_restaurant_id": first}).status_code == 400
    assert _post(c, "/admin/api/brand/add-location", json={"from_restaurant_id": 99999,
                                                            "restaurant_name": "X"}).status_code == 400


def test_create_restaurant_in_a_group_gets_its_organization(db_path):
    a = create_restaurant(Restaurant(name="Syrup North", owner_email="G@x.test", location_group="Syrup"), db_path=db_path)
    b = create_restaurant(Restaurant(name="Syrup South", owner_email="g@x.test", location_group="Syrup"), db_path=db_path)
    solo = create_restaurant(Restaurant(name="Solo", owner_email="s@x.test"), db_path=db_path)
    rows = {r["id"]: r["organization_id"] for r in
            _raw(db_path, "SELECT id, organization_id FROM restaurants WHERE id IN (?,?,?)", (a, b, solo))}
    assert rows[a] and rows[a] == rows[b] and rows[solo] is None
