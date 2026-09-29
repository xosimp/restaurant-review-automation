"""Fix round E — the scheduled senders: the morning brief's per-person ledger
and email fallback, and stale held alerts folded into it (#82); the automatic
alert-storm cap (#92); onboarding that starts at the delivered welcome, per
owner, never for demos, and marks a step only when it went (#16, #20); the
step nudges (#41); monthly/quarterly counting and billing gates (#16, #155);
kept-secret join links (#133); and the admin messaging routes (#45, #83)."""
import sqlite3
from datetime import datetime, timedelta

import pytest
from flask import Flask

import auth
import emails
import models
import morning_brief
import notify
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import push, scheduler, admin_routes, ops, guest_marketing
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, morning_brief, push, notify, auth, admin_routes, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    guest_marketing.init_guest_marketing(db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Job Co")
    kw.setdefault("owner_email", "o@job.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _exec(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


class _Now:
    def submit(self, fn, *a, **k):
        fn(*a, **k)


# ── #82: the brief's ledger and email fallback ──────────────────────────────

def _brief_world(db_path, monkeypatch, device=True):
    rid = _rid(db_path)
    uid = auth.create_user(rid, "own1", "own@job.test", "pw-Very-Long-123!", db_path=db_path, role="owner")
    if device:
        _exec(db_path, "INSERT INTO device_tokens (restaurant_id, user_id, apns_token) VALUES (?,?,?)",
              (rid, uid, "tok" * 20))
    monkeypatch.setattr(morning_brief, "build", lambda *a, **k: {"date": "2026-09-29", "lines": [
        {"key": "x", "text": "Salmon is below par.", "tone": "action", "ask": "?"}]})
    return rid, uid


def test_a_push_no_phone_took_falls_back_to_email(db_path, monkeypatch):
    import push
    rid, uid = _brief_world(db_path, monkeypatch)
    monkeypatch.setattr(push, "_push_executor", lambda: _Now())
    monkeypatch.setattr(push, "_deliver", lambda *a, **k: {"ok": False, "error": "Unregistered"})
    mailed = []
    monkeypatch.setattr(emails, "deliver", lambda **kw: mailed.append(kw) or emails.SendResult(True))
    out = morning_brief.deliver(rid, db_path=db_path)
    assert out["push"] == 1 and len(mailed) == 1 and mailed[0]["email_type"] == "send_morning_brief"
    row = _q(db_path, "SELECT channel, status FROM morning_brief_deliveries")[0]
    assert row == {"channel": "email", "status": "sent"}


def test_a_delivered_push_is_recorded_and_not_sent_twice(db_path, monkeypatch):
    import push
    rid, uid = _brief_world(db_path, monkeypatch)
    sent = []
    monkeypatch.setattr(push, "_push_executor", lambda: _Now())
    monkeypatch.setattr(push, "_deliver", lambda *a, **k: sent.append(1) or {"ok": True})
    morning_brief.deliver(rid, db_path=db_path)
    assert _q(db_path, "SELECT status FROM morning_brief_deliveries")[0]["status"] == "sent"
    morning_brief.deliver(rid, db_path=db_path)          # a retried pass
    assert len(sent) == 1


def test_a_failed_email_brief_is_recorded_as_failed(db_path, monkeypatch):
    rid, uid = _brief_world(db_path, monkeypatch, device=False)
    monkeypatch.setattr(emails, "deliver", lambda **kw: emails.SendResult(False, error="503", status_code=503,
                                                                          attempts=3, reason="transient"))
    out = morning_brief.deliver(rid, db_path=db_path)
    assert out["failed"] == 1 and out["retry"] is True and out["sent"] == 0
    assert _q(db_path, "SELECT status FROM morning_brief_deliveries")[0]["status"] == "failed"


def test_past_due_keeps_the_brief_and_canceled_does_not(db_path, monkeypatch):
    import time_utils
    delivered = []
    monkeypatch.setattr(morning_brief, "deliver", lambda rid, **k: delivered.append(rid) or {"sent": 1})
    monkeypatch.setattr(time_utils, "restaurant_now",
                        lambda r=None, naive=False: datetime(2026, 9, 29, 8, 0))
    a = _rid(db_path, name="Past Due Co", billing_status="past_due")
    b = _rid(db_path, name="Canceled Co", billing_status="canceled")
    morning_brief.run_due(db_path=db_path)
    assert a in delivered and b not in delivered


def test_a_stale_held_alert_is_recorded_and_folded_into_the_next_brief(db_path, monkeypatch):
    import push
    rid, uid = _brief_world(db_path, monkeypatch, device=False)
    _exec(db_path, "INSERT INTO alert_holds (restaurant_id, alert_type, subject, sms_text, release_at) "
                   "VALUES (?, '1star', '1-star review from Ann', 'x', datetime('now','-20 hours'))", (rid,))
    out = notify.release_due_alerts(db_path=db_path)
    assert out["dropped_stale"] == 1
    assert _q(db_path, "SELECT outcome FROM alert_holds")[0]["outcome"] == "dropped_stale"
    assert any(r["job"] == "held_alerts_dropped" for r in _q(db_path, "SELECT job FROM job_failures"))
    mailed = []
    monkeypatch.setattr(emails, "deliver", lambda **kw: mailed.append(kw) or emails.SendResult(True))
    morning_brief.deliver(rid, db_path=db_path)
    assert "1-star review from Ann" in mailed[0]["payload"]["html"]
    assert _q(db_path, "SELECT folded_at FROM alert_holds")[0]["folded_at"]
    assert notify.dropped_holds(rid, db_path=db_path) == []


# ── #92: the automatic storm cap ────────────────────────────────────────────

def _alerts(db_path, rid, n, alert_type="1star"):
    c = sqlite3.connect(db_path)
    for _ in range(n):
        c.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,datetime('now'))",
                  (rid, alert_type))
    c.commit()
    c.close()


def test_a_storm_caps_the_restaurant_until_its_midnight_and_tells_the_operator(db_path, monkeypatch):
    import ops, scheduler
    rid = _rid(db_path)
    told = []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: told.append(subject) or True)
    _alerts(db_path, rid, notify.ALERT_STORM_PER_HOUR - 1)
    assert notify._over_alert_ceiling(rid, db_path, "1star") is False
    _alerts(db_path, rid, 1)
    assert notify._over_alert_ceiling(rid, db_path, "1star") is True
    assert notify._over_alert_ceiling(rid, db_path, "2star") is True
    assert notify._over_alert_ceiling(rid, db_path, "health") is False, "health and safety always go"
    assert len(told) == 1, "the operator is told once"
    cap = notify.storm_cap_active(rid, db_path=db_path)
    assert cap and cap["suppressed"] == 2 and cap["until_at"] > datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def test_a_storm_cap_can_be_lifted_and_expires_on_its_own(db_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    rid = _rid(db_path)
    _alerts(db_path, rid, notify.ALERT_STORM_PER_HOUR)
    assert notify._over_alert_ceiling(rid, db_path, "1star") is True
    _exec(db_path, "UPDATE alert_storm_caps SET until_at=datetime('now','-1 minute')")
    assert notify.storm_cap_active(rid, db_path=db_path) is None
    _exec(db_path, "UPDATE alert_storm_caps SET until_at=datetime('now','+1 hour')")
    assert notify.lift_storm_cap(rid, "will", db_path=db_path) is True
    assert notify._over_alert_ceiling(rid, db_path, "1star") is False, "lifted for the rest of the day"


# ── #20 / #16: onboarding ───────────────────────────────────────────────────

def _onboarded(db_path, days, **kw):
    kw.setdefault("contract_status", "signed")
    kw.setdefault("owner_email", "o@job.test")
    rid = _rid(db_path, **kw)
    models.update_restaurant(rid, {"contract_status": kw["contract_status"]}, db_path=db_path)
    at = (datetime.utcnow() - timedelta(days=days, minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    _exec(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, sent_at, status) "
                   "VALUES (?, 'send_welcome_set_password_email', ?, ?, 'sent')", (rid, kw["owner_email"], at))
    return rid


def test_onboarding_waits_for_the_signed_contract_and_the_welcome(db_path, monkeypatch):
    import scheduler
    sent = []
    monkeypatch.setattr(emails, "send_onboarding_day2", lambda **kw: sent.append(kw["restaurant_id"])
                        or emails.SendResult(True))
    prospect = _rid(db_path, name="Prospect Co", owner_email="p@job.test",
                    created_at=(datetime.utcnow() - timedelta(days=3)).isoformat())
    unsigned = _onboarded(db_path, 3, name="Unsigned", owner_email="u@job.test", contract_status="pending")
    demo = _onboarded(db_path, 3, name="Demo", owner_email="d@job.test", is_demo=1)
    real = _onboarded(db_path, 3, name="Real", owner_email="r@job.test")
    scheduler.run_onboarding_sequence()
    assert sent == [real]
    assert scheduler.onboarding_eligible(models.get_restaurant(prospect, db_path))[1] == "contract not signed"


def test_onboarding_goes_to_an_owner_once_across_their_locations(db_path, monkeypatch):
    import scheduler
    sent = []
    monkeypatch.setattr(emails, "send_onboarding_day2", lambda **kw: sent.append(kw["restaurant_id"])
                        or emails.SendResult(True))
    a = _onboarded(db_path, 3, name="Loc A", owner_email="group@job.test", location_group="G")
    b = _onboarded(db_path, 3, name="Loc B", owner_email="group@job.test", location_group="G")
    scheduler.run_onboarding_sequence()
    assert sent == [a]
    status = {r["restaurant_id"]: r["status"] for r in _q(db_path, "SELECT restaurant_id, status FROM onboarding_emails")}
    assert status == {a: "sent", b: "covered"}


def test_a_failed_onboarding_email_is_not_marked_sent(db_path, monkeypatch):
    import scheduler, ops
    rid = _onboarded(db_path, 3)
    released = []
    monkeypatch.setattr(ops, "release_period", lambda job, period: released.append(job))
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr(emails, "send_onboarding_day2", lambda **kw: emails.SendResult(
        False, error="429", status_code=429, attempts=3, reason="transient"))
    scheduler.run_onboarding_sequence(local_hour=10)
    assert _q(db_path, "SELECT * FROM onboarding_emails") == []
    assert released == [f"onboarding:{rid}"], "a transient failure gives the day's claim back"
    monkeypatch.setattr(emails, "send_onboarding_day2", lambda **kw: emails.SendResult(
        False, error="suppressed", attempts=0, reason="suppressed"))
    scheduler.run_onboarding_sequence()
    row = _q(db_path, "SELECT status FROM onboarding_emails")[0]
    assert row["status"] == "failed"
    assert any(r["job"] == "onboarding_email" for r in _q(db_path, "SELECT job FROM job_failures"))


# ── #41: step nudges ────────────────────────────────────────────────────────

def test_a_missing_step_is_nudged_once_and_a_done_step_never(db_path, monkeypatch):
    import scheduler
    rid = _onboarded(db_path, 5, module_reviews=1)
    nudged = []
    monkeypatch.setattr(emails, "send_onboarding_nudge", lambda step, **kw: nudged.append(step["key"])
                        or emails.SendResult(True))
    scheduler.run_onboarding_nudges()
    scheduler.run_onboarding_nudges()
    assert nudged == ["reviews", "voice"], "one per pass, each once"
    models.update_restaurant(rid, {"voice_notes": "Warm, brief."}, db_path=db_path)
    scheduler.run_onboarding_nudges()
    assert nudged == ["reviews", "voice"]


def test_the_nudge_email_carries_the_can_spam_footer(db_path, monkeypatch):
    rid = _rid(db_path)
    sent = []
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    import types
    monkeypatch.setattr("requests.post", lambda url, json=None, **kw: sent.append(json) or types.SimpleNamespace(
        status_code=200, text='{"id":"m"}', json=lambda: {"id": "m"}))
    assert emails.send_onboarding_nudge({"key": "voice"}, "o@job.test", "Job Co", restaurant_id=rid).ok
    assert "/u/" in sent[0]["html"] and "100 Test St" in sent[0]["html"]


# ── #16 / #155: monthly and quarterly ───────────────────────────────────────

def test_a_failed_monthly_is_not_counted_and_no_month_ready_push_goes(db_path, monkeypatch):
    import scheduler, ops
    _rid(db_path, name="Monthly Co", owner_email="m@job.test", billing_status="active")
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    released = []
    monkeypatch.setattr(ops, "release_period", lambda job, period: released.append(job))
    monkeypatch.setattr(emails, "send_monthly_summary_email", lambda **kw: emails.SendResult(
        False, error="503", status_code=503, attempts=3, reason="transient"))
    pushed = []
    monkeypatch.setattr(scheduler, "_push_month_ready", lambda r: pushed.append(r.id))
    out = scheduler.run_monthly_summaries()
    assert out["sent"] == 0 and out["failed"] == 1 and pushed == []
    assert released and released[0].startswith("monthly_summary:")


def test_canceled_accounts_get_no_summaries(db_path, monkeypatch):
    import scheduler
    _rid(db_path, name="Gone Co", owner_email="g@job.test", billing_status="canceled")
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    called = []
    monkeypatch.setattr(emails, "send_monthly_summary_email", lambda **kw: called.append(1) or emails.SendResult(True))
    monkeypatch.setattr(emails, "send_quarterly_summary_email", lambda **kw: called.append(1) or emails.SendResult(True))
    scheduler.run_monthly_summaries()
    scheduler.run_quarterly_summaries()
    assert called == []


def test_is_paying_is_active_or_past_due():
    assert models.is_paying({"billing_status": "past_due"}) and models.is_paying({"billing_status": "active"})
    assert not models.is_paying({"billing_status": "trial"}) and not models.is_paying({"billing_status": "canceled"})


# ── #133: join links survive a SECRET_KEY rotation ──────────────────────────

def test_join_links_are_signed_with_a_kept_secret(db_path, monkeypatch):
    import guest_links
    token = guest_links.sign_join(42)
    assert token.startswith("42-v") and guest_links.verify_join(token) == 42
    monkeypatch.setenv("SECRET_KEY", "rotated-" + "y" * 20)
    assert guest_links.verify_join(token) == 42, "rotating SECRET_KEY broke a printed QR code"
    assert guest_links.verify_join("43-" + token.split("-", 1)[1]) is None


def test_a_printed_secret_key_join_link_still_works_until_the_key_changes(db_path, monkeypatch):
    import guest_links
    legacy = f"42-{guest_links._legacy_sig(42)}"
    assert guest_links.verify_join(legacy) == 42
    monkeypatch.setenv("SECRET_KEY", "another-key-" + "z" * 20)
    assert guest_links.verify_join(legacy) is None


# ── #45 / #83: admin routes ─────────────────────────────────────────────────

@pytest.fixture
def admin(monkeypatch, db_path):
    import admin_routes
    user = {"id": 1, "restaurant_id": 1, "is_admin": 1, "username": "will", "role": "owner",
            "email": "will@cavnar.ai"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "_admin_two_factor_missing", lambda u: False, raising=False)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_routes.admin_bp)
    return app.test_client()


def test_the_reinstate_route_lifts_and_audits(db_path, admin):
    models.suppress_email("lifted@job.test", "bounced", db_path=db_path)
    r = admin.post("/admin/api/suppressions/reinstate", json={"email": "Lifted@Job.test", "reason": "fixed"})
    assert r.status_code == 200 and r.get_json()["lifted"]["reason"] == "bounced"
    assert not models.is_email_suppressed("lifted@job.test", db_path=db_path)
    assert admin.post("/admin/api/suppressions/reinstate", json={"email": "lifted@job.test"}).status_code == 404
    listed = admin.get("/admin/api/suppressions?q=lifted").get_json()
    assert listed["suppressions"] == []


def test_the_value_recap_route_answers_with_the_real_result(db_path, admin, monkeypatch):
    import scheduler
    rid = _rid(db_path)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(emails, "send_value_recap_email", lambda r: emails.SendResult(
        False, error="503", status_code=503, attempts=3, reason="transient"))
    assert admin.post(f"/admin/api/client/{rid}/value-recap").status_code == 502
    monkeypatch.setattr(emails, "send_value_recap_email", lambda r: emails.SendResult(True, message_id="m"))
    assert admin.post(f"/admin/api/client/{rid}/value-recap").get_json()["ok"] is True
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    assert admin.post(f"/admin/api/client/{rid}/value-recap").status_code == 409


def test_the_messaging_health_route_reads(db_path, admin):
    body = admin.get("/admin/api/messaging/health").get_json()
    assert body["ok"] is True
    assert {"inbound_webhooks", "email", "sms", "push_outbox", "webhook_outbox", "storm_caps"} <= set(body)
