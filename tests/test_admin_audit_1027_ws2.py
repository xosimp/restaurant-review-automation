"""Admin audit #26 (10/27/26), fix stream WS-2 — backend workflows and
security. One regression test (or a small group) per verified defect:

  S1   the three older job-status polls answer only their own kind of job
       (and a client's job only for that client), never a read-once value,
       and support's job list never names a review-account job;
  S2   Reinstate lifts a bounce only — a person's own unsubscribe or spam
       complaint is refused (409) and audited as refused; C7 the lift names
       the restaurant and the actor's id;
  S7   typed audit rows (actor id included) for the vendor cost, Run now, the
  F16  test text, fetch now, issue resolve/reopen, the experiment writes, the
       sales-audit create/status/delete and the POS Sync now routes;
  F2   the offboarding Stripe step reads the subscription mirror, not a
       hand-set billing_status;
  F4   guest thank-you texts, newsletter batches and the POS guest match keep
       to the one service rule;
  F8   a rollout needs `percent` and a feature rollout.py knows;
  F10  a create-client that fails after the login leaves nothing behind;
  F15  deactivate asks before switching off the last owner login, and the
       login is off before its sessions are ended;
  F17  the new-client duplicate check compares usernames and emails crosswise;
  S9   the intake store mapping checks the group by id (organisation, or name
       AND owner);
  W15  a double-clicked support note is written once.

Stripe, Twilio, Resend and the providers are never reached.
"""
import json
import os
from datetime import datetime

import pytest
from flask import Flask, g

import admin_events
import admin_ops
import admin_routes
import auth
import billing_jobs
import models
import offboarding
import ops
from auth import create_session, create_user
from models import get_restaurant, update_restaurant

from test_fix_int2_admin import CSRF, _admin, _hq, _owner, _post, _rid, _rows, _world, app  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _support(app, db_path, username="sup"):
    sup = create_user(_hq(db_path), username, f"{username}@cavnar.test", "Support-pass-2026", role="support",
                      db_path=db_path)
    s = app.test_client()
    s.set_cookie("session_token", create_session(sup, db_path=db_path))
    s.set_cookie("csrf_js", CSRF)
    return s


def _events(db_path, action):
    return _rows(db_path, "SELECT * FROM admin_events WHERE source='admin' AND event_type=? ORDER BY id", (action,))


# ── S1: the job-status polls ────────────────────────────────────────────────

def _review_account_job():
    ops.start_async_job("seed-1", "admin_review_account", 0)
    ops.finish_async_job("seed-1", "done", {"ok": True, "password_once": "abcde-FGHJK",
                                            "_scrub": ["password_once"]})


def test_the_job_polls_never_hand_out_another_kind_of_job_or_a_read_once_value(app, db_path):
    c, _ = _admin(app, db_path)
    s = _support(app, db_path)
    rid = _rid(db_path)
    _review_account_job()
    for client in (s, c):
        for url in ("/admin/api/menu-extract/seed-1", "/admin/api/create-client/seed-1",
                    f"/admin/api/client/{rid}/pos-export/seed-1"):
            r = client.get(url)
            assert r.status_code == 404, url
            assert "abcde-FGHJK" not in r.get_data(as_text=True), url
    # Still the admin's to read, once, through the one route that hands it over.
    assert c.get("/admin/api/admin-jobs/seed-1").get_json()["password_once"] == "abcde-FGHJK"


def test_a_job_of_the_right_kind_reads_with_its_scrubbed_keys_removed(app, db_path):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    ops.start_async_job("menu-1", "menu_extract", rid)
    ops.finish_async_job("menu-1", "done", {"ok": True, "menu_notes": "Burgers", "secret": "x",
                                            "_scrub": ["secret"]})
    body = c.get("/admin/api/menu-extract/menu-1").get_json()
    assert body["menu_notes"] == "Burgers" and body["status"] == "done"
    assert "secret" not in body and "_scrub" not in body
    # The same job is not a new client's setup.
    assert c.get("/admin/api/create-client/menu-1").status_code == 404


def test_a_pos_export_job_reads_only_through_its_own_restaurant(app, db_path):
    c, _ = _admin(app, db_path)
    mine, other = _rid(db_path, "Mine Grill"), _rid(db_path, "Other Grill")
    ops.start_async_job("exp-1", "pos_export", mine)
    ops.finish_async_job("exp-1", "done", {"ok": True, "file": f"pos-export-{mine}-x.zip", "bytes": 1, "tables": 1})
    assert c.get(f"/admin/api/client/{mine}/pos-export/exp-1").status_code == 200
    assert c.get(f"/admin/api/client/{other}/pos-export/exp-1").status_code == 404
    r = _post(c, f"/admin/api/client/{other}/pos-export/exp-1/download")
    assert r.status_code == 404


def test_the_provisioning_poll_reads_only_provisioning_jobs(db_path):
    """The fourth caller of the shared poll (onboarding_routes) — the audit
    named three; a module-level grep of admin_routes alone misses it."""
    import onboarding_routes
    flask_app = Flask(__name__)
    flask_app.secret_key = "ws2"
    flask_app.register_blueprint(onboarding_routes.onboarding_bp)
    uid = create_user(_hq(db_path), "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = flask_app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db_path))
    _review_account_job()
    r = c.get("/admin/api/onboarding/provision/seed-1")
    assert r.status_code == 404 and "abcde-FGHJK" not in r.get_data(as_text=True)
    ops.start_async_job("batch-1", "bulk_provision", None)
    ops.finish_async_job("batch-1", "done", {"ok": True, "counts": {"created": 1}})
    assert c.get("/admin/api/onboarding/provision/batch-1").get_json()["counts"] == {"created": 1}


def test_every_caller_of_the_shared_job_poll_names_its_kind():
    import subprocess
    out = subprocess.run(["git", "grep", "-n", "_admin_job_status(", "--", "*.py"], cwd=ROOT,
                         capture_output=True, text=True, timeout=30).stdout
    calls = [ln for ln in out.splitlines() if "def _admin_job_status" not in ln and "/tests/" not in ln
             and not ln.startswith("tests/")]
    assert calls and all("_admin_job_status(job_id, (" in ln for ln in calls), calls


def test_support_is_never_shown_a_review_account_job_in_the_jobs_list(app, db_path):
    c, _ = _admin(app, db_path)
    s = _support(app, db_path)
    _review_account_job()
    assert "seed-1" not in s.get("/admin/api/jobs").get_data(as_text=True)
    assert any(j["id"] == "seed-1" for j in c.get("/admin/api/jobs").get_json()["inflight"])


# ── S2 / C7: Reinstate ──────────────────────────────────────────────────────

def test_a_bounce_is_reinstated_with_the_actor_id_and_the_restaurant(app, db_path):
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path, owner_email="own@bounce.test")
    models.suppress_email("own@bounce.test", "bounced", "550 full", db_path=db_path)
    r = _post(c, "/admin/api/suppressions/reinstate", {"email": "own@bounce.test", "reason": "mailbox emptied"})
    assert r.status_code == 200 and r.get_json()["lifted"]["reason"] == "bounced"
    assert not models.is_email_suppressed("own@bounce.test", db_path=db_path)
    (ev,) = _events(db_path, "email.unsuppressed")
    assert ev["actor_id"] == admin_uid and ev["restaurant_id"] == rid and ev["result"] == "ok"


@pytest.mark.parametrize("reason,scope", [("unsubscribed", "guest"), ("unsubscribed", "marketing"),
                                          ("complained", "all")])
def test_a_persons_own_opt_out_is_never_reinstated_by_an_admin(app, db_path, reason, scope):
    c, admin_uid = _admin(app, db_path)
    models.suppress_email("guest@optout.test", reason, "their own click", scope=scope, db_path=db_path)
    r = _post(c, "/admin/api/suppressions/reinstate", {"email": "guest@optout.test", "reason": "please"})
    assert r.status_code == 409 and r.get_json()["refused"] is True
    rows = _rows(db_path, "SELECT reason FROM email_suppressions WHERE email='guest@optout.test'")
    assert rows and rows[0]["reason"] == reason, "the opt-out row is the record of it"
    (ev,) = _events(db_path, "email.unsuppressed")
    assert ev["result"] == "refused" and ev["actor_id"] == admin_uid


def test_the_suppression_list_says_which_rows_can_be_reinstated(app, db_path):
    c, _ = _admin(app, db_path)
    models.suppress_email("a@bounce.test", "bounced", db_path=db_path)
    models.suppress_email("b@optout.test", "unsubscribed", scope="guest", db_path=db_path)
    rows = {r["email"]: r for r in c.get("/admin/api/suppressions").get_json()["suppressions"]}
    assert rows["a@bounce.test"]["reinstatable"] is True
    assert rows["b@optout.test"]["reinstatable"] is False


# ── S7 / F16: typed audit rows with the actor's id ──────────────────────────

def test_a_username_from_this_requests_actor_gets_their_id():
    flask_app = Flask(__name__)
    with flask_app.test_request_context("/admin/x", method="POST"):
        g._admin_audit = {"actor": {"id": 7, "username": "will"}, "request_id": "r1", "admin_only": False}
        assert admin_events._actor_parts("will") == ("will", 7)
        assert admin_events._actor_parts("someone-else") == ("someone-else", None)
    assert admin_events._actor_parts("will") == ("will", None)          # outside a request: no id


def test_a_vendor_cost_is_a_typed_row_with_before_and_after(app, db_path):
    c, admin_uid = _admin(app, db_path)
    vendor = admin_ops.VENDORS[0]
    assert _post(c, "/admin/api/vendor-costs", {"vendor": vendor, "month": "2026-09", "amount_usd": 20}
                 ).status_code == 200
    assert _post(c, "/admin/api/vendor-costs", {"vendor": vendor, "month": "2026-09", "amount_usd": 35}
                 ).status_code == 200
    evs = _events(db_path, "vendor_cost.set")
    assert len(evs) == 2 and all(e["actor_id"] == admin_uid for e in evs)
    assert json.loads(evs[-1]["before_json"]) == {"amount_usd": 20.0}
    assert json.loads(evs[-1]["after_json"])["amount_usd"] == 35.0


def test_run_now_records_the_admins_id(app, db_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    c, admin_uid = _admin(app, db_path)
    job = next(n for n, spec in admin_ops.RUNNABLE_JOBS.items() if spec["sends"])
    assert _post(c, f"/admin/api/jobs/{job}/run").status_code == 409
    (ev,) = _events(db_path, "job.run_now")
    assert ev["actor_id"] == admin_uid and ev["result"] == "refused"


def test_the_alert_contact_test_text_is_audited_refused_and_sent(app, db_path, monkeypatch):
    import notify
    import scheduler
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    assert _post(c, f"/admin/alert-contacts/test/{rid}").status_code == 409
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(notify, "send_test_sms",
                        lambda r: {"ok": True, "sent": 2, "errors": ["+13125550199"], "results": []})
    assert _post(c, f"/admin/alert-contacts/test/{rid}").get_json()["ok"] is True
    evs = _events(db_path, "alert_contact.test_texted")
    assert [e["result"] for e in evs] == ["refused", "ok"]
    assert all(e["actor_id"] == admin_uid and e["restaurant_id"] == rid for e in evs)
    assert json.loads(evs[-1]["after_json"])["sent"] == 2
    assert "5550199" not in json.dumps(evs), "counts, never numbers"


def test_fetch_now_is_audited(app, db_path, monkeypatch):
    import scheduler
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path, google_place_id="ChIJfetch", reviews_live=1)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "run_admin_task", lambda *a, **k: ("fetch-1", False))
    assert _post(c, f"/admin/fetch-reviews/{rid}").get_json()["job_id"] == "fetch-1"
    (ev,) = _events(db_path, "reviews.fetch_now")
    assert ev["actor_id"] == admin_uid and ev["restaurant_id"] == rid


def test_resolving_and_reopening_an_issue_are_typed_rows(app, db_path, monkeypatch):
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path)
    monkeypatch.setattr(admin_ops, "resolve_issue", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(admin_ops, "unresolve_issue", lambda *a, **k: {"ok": True})
    assert _post(c, "/admin/api/issues/resolve", {"key": f"{rid}:fetch_behind", "note": "fixed"}).status_code == 200
    assert _post(c, "/admin/api/issues/resolve", {"key": f"{rid}:fetch_behind", "undo": True}).status_code == 200
    assert _post(c, "/admin/api/issues/resolve", {"key": "platform:error_rate", "note": "ok"}).status_code == 200
    resolved, reopened = _events(db_path, "issue.resolved"), _events(db_path, "issue.reopened")
    assert [e["restaurant_id"] for e in resolved] == [rid, None] and reopened[0]["restaurant_id"] == rid
    assert all(e["actor_id"] == admin_uid for e in resolved + reopened)
    assert json.loads(resolved[0]["after_json"])["note"] == "fixed"


def test_the_schedule_experiment_writes_are_typed_rows(app, db_path):
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path)
    _post(c, "/admin/api/schedule-experiments/pin", {"restaurant_id": rid, "experiment": "nope", "arm": "a"})
    _post(c, "/admin/api/schedule-experiments/promote", {"experiment": "nope", "arm": "a"})
    _post(c, "/admin/api/schedule-experiments/revert", {"experiment": "nope"})
    for action in ("schedule_experiment.pinned", "schedule_experiment.promoted", "schedule_experiment.reverted"):
        (ev,) = _events(db_path, action)
        assert ev["actor_id"] == admin_uid and ev["result"] == "refused", action
    assert _events(db_path, "schedule_experiment.pinned")[0]["restaurant_id"] == rid


def test_a_sales_audit_create_status_and_permanent_delete_are_typed_rows(app, db_path):
    c, admin_uid = _admin(app, db_path)
    aid = _post(c, "/admin/api/audits", {"restaurant_name": "Audit Grill"}).get_json()["id"]
    assert _post(c, f"/admin/api/audits/{aid}/status", {"status": "Archived"}).get_json()["ok"]
    assert c.delete(f"/admin/api/audits/{aid}", headers={"X-CSRF": CSRF}).get_json()["ok"]
    created, status, deleted = (_events(db_path, a) for a in ("sales_audit.created", "sales_audit.status_set",
                                                               "sales_audit.deleted"))
    assert created and status and deleted
    assert all(e[0]["actor_id"] == admin_uid for e in (created, status, deleted))
    assert json.loads(status[0]["after_json"]) == {"status": "Archived"}
    assert json.loads(deleted[0]["before_json"])["restaurant_name"] == "Audit Grill"


def test_pos_sync_now_is_a_typed_row_and_a_refused_one_says_refused(app, db_path, monkeypatch):
    import scheduler
    import square
    c, admin_uid = _admin(app, db_path)
    rid = _rid(db_path)
    monkeypatch.setattr(square, "is_connected", lambda r: True)
    monkeypatch.setattr(scheduler, "start_manual_pos_sync", lambda r, who: ("sync-1", False))
    assert _post(c, f"/admin/square/sync/{rid}").get_json()["job_id"] == "sync-1"
    _post(c, f"/admin/toast/sync/{rid}")                       # Toast isn't connected: ok false
    (sq,) = _events(db_path, "pos.square.sync_started")
    (to,) = _events(db_path, "pos.toast.sync_started")
    assert sq["actor_id"] == admin_uid and sq["restaurant_id"] == rid and sq["result"] == "ok"
    assert to["result"] == "refused"


# ── F2: the offboarding Stripe step reads the mirror ─────────────────────────

def _stripe_step(db_path, rid):
    return next(s for s in offboarding.checklist(rid, db_path=db_path)["steps"] if s["step"] == "stripe")


def test_a_hand_set_churned_status_does_not_skip_a_live_subscriptions_cancel(db_path):
    rid = _rid(db_path, stripe_customer_id="cus_X", billing_status="active")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"customer_id": "cus_X", "status": "active"},
                                     db_path=db_path)
    update_restaurant(rid, {"billing_status": "churned"}, db_path=db_path)
    step = _stripe_step(db_path, rid)
    assert step["status"] == "pending", step
    assert "ready_to_delete" in offboarding.checklist(rid, db_path=db_path)
    assert not offboarding.checklist(rid, db_path=db_path)["ready_to_delete"]
    # Stripe really ended it: the mirror says so, and the step has nothing to do.
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1",
                                     facts={"customer_id": "cus_X", "status": "canceled"}, db_path=db_path)
    assert _stripe_step(db_path, rid)["status"] == "not_needed"


def test_an_unreadable_mirror_leaves_the_stripe_step_to_do(db_path, monkeypatch):
    rid = _rid(db_path, stripe_customer_id="cus_Y", billing_status="churned")
    monkeypatch.setattr(billing_jobs, "live_subscription", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _stripe_step(db_path, rid)["status"] == "pending"


# ── F4: guest sends keep to the service rule ────────────────────────────────

def _followups_on(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        conn.execute("INSERT INTO guest_followup_settings (restaurant_id, enabled) VALUES (?, 1)", (rid,))
        conn.commit()
    finally:
        conn.close()


def test_a_churned_restaurant_thanks_no_guest(db_path, monkeypatch):
    import guest_hub
    import guest_marketing as gm
    monkeypatch.setattr(gm, "guest_sms_allowed_now", lambda rid: True)
    monkeypatch.setattr(gm, "send_sms", lambda *a, **k: pytest.fail("texted a churned restaurant's guest"))
    live, gone = _rid(db_path, "Live Grill", billing_status="active"), _rid(db_path, "Gone Grill",
                                                                            billing_status="churned")
    for rid in (live, gone):
        _followups_on(db_path, rid)
    noon = datetime(2026, 10, 27, 12, 0)
    assert guest_hub.followup_one(gone, now=noon, db_path=db_path)["skipped"] == 1
    assert guest_hub.followup_one(live, now=noon, db_path=db_path)["skipped"] == 0
    seen = []
    monkeypatch.setattr(guest_hub, "followup_one", lambda rid, **k: seen.append(rid) or {})
    guest_hub.run_followups(db_path=db_path, now=noon)
    assert seen == [live]


def test_a_newsletter_stops_when_its_restaurant_leaves_service(db_path, monkeypatch):
    import guest_email
    monkeypatch.setattr(guest_email, "_sending_configured", lambda: True)
    rid = _rid(db_path, billing_status="paused")
    conn = models.get_conn(db_path)
    try:
        nid = conn.execute("INSERT INTO guest_newsletters (restaurant_id, subject, body, content_hash, total) "
                           "VALUES (?,?,?,?,?)", (rid, "News", "Body", "h", 1)).lastrowid
        conn.commit()
    finally:
        conn.close()
    out = guest_email._send_batch(nid, db_path=db_path)
    assert out.get("out_of_service") is True and out["sent"] == 0 and out.get("paused") is True


def test_the_pos_guest_match_reads_no_store_out_of_service(db_path, monkeypatch):
    import guest_hub
    rids = {}
    for i, status in enumerate(("active", "churned", "paused", "canceled", "cancelled", "internal")):
        rid = _rid(db_path, f"Store{i} Grill", billing_status=status)
        conn = models.get_conn(db_path)
        try:
            conn.execute("UPDATE restaurants SET pos_guest_match=1, rpower_token='t', rpower_store_mid=? WHERE id=?",
                         (f"M{i}", rid))
            conn.commit()
        finally:
            conn.close()
        rids[status] = rid
    seen = []
    monkeypatch.setattr(guest_hub, "match_pos_visits", lambda rid, day, **k: seen.append(rid) or {"tickets": 0})
    guest_hub.run_pos_guest_match(db_path=db_path, day=datetime(2026, 10, 26).date())
    assert seen == [rids["active"]]


# ── F8: rollout validation ──────────────────────────────────────────────────

def test_a_rollout_needs_a_percent_and_a_known_feature(app, db_path):
    import rollout
    c, _ = _admin(app, db_path)
    r = _post(c, "/admin/api/rollout", {"feature": "staff_docs_plan_gate"})
    assert r.status_code == 400 and "percent" in r.get_json()["error"]
    assert rollout.state("staff_docs_plan_gate") is None, "nothing stored: the gate's default (on) holds"
    r = _post(c, "/admin/api/rollout", {"feature": "client_min_bulid", "percent": 50})
    assert r.status_code == 400
    assert _post(c, "/admin/api/rollout", {"feature": "stale_tab_reload", "percent": 100}).status_code == 200
    assert [e["result"] for e in _events(db_path, "rollout.set")] == ["refused", "refused", "ok"]


# ── F10: create-client undoes a half-made client ─────────────────────────────

def _new_client(**kw):
    body = {"restaurant_name": "Half Made Grill", "owner_email": "half@made.test", "username": "halfmade",
            "password": "Temp-pass-2026!", "module_reviews": 1}
    body.update(kw)
    return body


def test_a_failure_after_the_login_leaves_no_restaurant_or_login(app, db_path, monkeypatch):
    c, _ = _admin(app, db_path)
    real = models.update_restaurant

    def flaky(rid, fields, *a, **k):
        if "module_reviews" in fields:
            raise RuntimeError("database is locked")
        return real(rid, fields, *a, **k)
    monkeypatch.setattr(models, "update_restaurant", flaky)
    r = _post(c, "/admin/create-client", _new_client())
    assert r.get_json()["ok"] is False
    assert not _rows(db_path, "SELECT id FROM restaurants WHERE name='Half Made Grill'")
    assert not _rows(db_path, "SELECT id FROM users WHERE username='halfmade'")
    monkeypatch.setattr(models, "update_restaurant", real)
    again = _post(c, "/admin/create-client", _new_client()).get_json()
    assert again["ok"] is True, again
    assert _events(db_path, "client.created")


# ── F15: deactivate and the last owner ──────────────────────────────────────

def test_deactivating_the_only_owner_asks_first(app, db_path):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    owner = _owner(db_path, rid)
    r = _post(c, f"/admin/deactivate-client/{owner}")
    assert r.status_code == 409 and r.get_json()["last_owner"] is True
    assert _rows(db_path, "SELECT is_active FROM users WHERE id=?", (owner,))[0]["is_active"] == 1
    assert _post(c, f"/admin/deactivate-client/{owner}", {"force": True}).status_code == 200
    assert _rows(db_path, "SELECT is_active FROM users WHERE id=?", (owner,))[0]["is_active"] == 0


def test_a_second_owner_is_deactivated_without_asking_and_the_login_is_off_first(app, db_path, monkeypatch):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    _owner(db_path, rid)
    second = _owner(db_path, rid, username="second", email="second@grill.test")
    seen = []

    def ended(uid, *a, **k):
        seen.append(_rows(db_path, "SELECT is_active FROM users WHERE id=?", (uid,))[0]["is_active"])
        return {"sessions": 0, "devices": 0}
    monkeypatch.setattr(auth, "end_login_access", ended)
    assert _post(c, f"/admin/deactivate-client/{second}").status_code == 200
    assert seen == [0], "the login was still on while its sessions were ended"


# ── F17: the duplicate check, crosswise ─────────────────────────────────────

def test_a_new_username_equal_to_another_logins_email_is_refused(app, db_path):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    create_user(rid, "taken", "taken@other.test", "Owner-pass-2026", db_path=db_path)
    create_user(rid, "boss@q.test", "boss2@other.test", "Owner-pass-2026", db_path=db_path)
    for body in (_new_client(username="taken@other.test"), _new_client(owner_email="boss@q.test")):
        r = _post(c, "/admin/create-client", body).get_json()
        assert r["ok"] is False and "already exists" in r["error"], body
    assert not _rows(db_path, "SELECT id FROM restaurants WHERE name='Half Made Grill'")


# ── S9: the intake store mapping checks the group by id ─────────────────────

def test_a_store_maps_only_to_a_location_of_the_same_owners_group(app, db_path):
    c, _ = _admin(app, db_path)
    mine = _rid(db_path, "Syrup One", owner_email="a@syrup.test", location_group="Syrup")
    sibling = _rid(db_path, "Syrup Two", owner_email="a@syrup.test", location_group="Syrup")
    stranger = _rid(db_path, "Syrup Elsewhere", owner_email="b@other.test", location_group="Syrup")
    r = _post(c, f"/admin/api/client/{mine}/intake/store", {"location_id": "12", "target_restaurant_id": stranger})
    assert r.status_code == 400
    r = _post(c, f"/admin/api/client/{mine}/intake/store", {"location_id": "12", "target_restaurant_id": sibling})
    assert r.status_code == 200 and r.get_json()["restaurant_id"] == sibling


# ── W15-f: a double-clicked support note ────────────────────────────────────

def test_a_double_clicked_support_note_is_written_once(app, db_path):
    c, _ = _admin(app, db_path)
    rid = _rid(db_path)
    first = _post(c, f"/admin/api/client/{rid}/notes", {"body": "Called Erik about the bounce."}).get_json()
    second = _post(c, f"/admin/api/client/{rid}/notes", {"body": "Called Erik about the bounce."}).get_json()
    assert first["ok"] and second["ok"] and second.get("duplicate") is True
    assert second["note"]["id"] == first["note"]["id"]
    assert len(_rows(db_path, "SELECT id FROM support_notes WHERE restaurant_id=?", (rid,))) == 1
    assert len(_events(db_path, "support_note.added")) == 1
    _post(c, f"/admin/api/client/{rid}/notes", {"body": "A different note."})
    assert len(_rows(db_path, "SELECT id FROM support_notes WHERE restaurant_id=?", (rid,))) == 2


def test_one_reinstate_rule_and_an_unknown_reason_is_not_liftable():
    # Lead merge (admin audit 10/27/26): the console's Reinstate offer and the
    # route's lift read one rule, and it fails closed — only a bounce lifts.
    import admin_ops
    import models
    for reason in ("bounced", "bounce", "hard_bounce", "", None, " Bounced "):
        assert models.suppression_liftable(reason) and admin_ops._suppression_reinstatable(reason), reason
    for reason in ("unsubscribed", "complained", "manual", "spam_trap"):
        assert not models.suppression_liftable(reason) and not admin_ops._suppression_reinstatable(reason), reason
