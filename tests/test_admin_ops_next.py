"""The four follow-ups to the admin console: AI failures in ai_usage, run-now,
admin_events from webhooks, and the alert-cap toggle."""
import time
import pytest

import admin_ops
import admin_routes
import auth
import client_api
import mobile_api
import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, admin_routes, client_api, mobile_api, admin_ops):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    from models import init_two_fa_backup_codes, init_email_log
    init_two_fa_backup_codes(db_path=db_path)
    init_email_log(db_path=db_path)
    try:
        import guest_marketing
        guest_marketing.init_guest_marketing(db_path)
    except Exception:
        pass
    import ai_utils, ops
    conn = get_conn(db_path)
    conn.executescript(ai_utils._USAGE_TABLE_SQL)
    conn.executescript(ops._RUNS_SQL)
    conn.commit(); conn.close()


@pytest.fixture
def rid(db_path):
    return create_restaurant(Restaurant(name="Cap Test Grill", owner_email="cap@example.com", module_reviews=1), db_path=db_path)


class _Boom(Exception):
    pass


def test_failed_ai_call_lands_in_ai_usage_with_status(db_path, rid):
    import ai_utils

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                raise _Boom("model exploded")

    with pytest.raises(_Boom):
        ai_utils.create_with_retry(FakeClient(), retries=0, restaurant_id=rid, action="draft_reply", model="claude-x")
    c = get_conn(db_path)
    rows = c.execute("SELECT action, status, error, cost_usd FROM ai_usage").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "error" and "model exploded" in rows[0]["error"]
    assert rows[0]["cost_usd"] == 0
    # the migration is idempotent on a table that predates the columns
    c.execute("DROP TABLE ai_usage")
    c.execute("CREATE TABLE ai_usage (id INTEGER PRIMARY KEY, restaurant_id INTEGER, action TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, created_at TEXT DEFAULT (datetime('now')))")
    c.commit(); c.close()
    ai_utils.log_ai_usage(rid, "x", "claude-x", 1, 1, db_path=db_path)
    ai_utils.log_ai_usage(rid, "x", "claude-x", 0, 0, db_path=db_path, status="error", error="boom")
    c = get_conn(db_path)
    assert [r["status"] for r in c.execute("SELECT status FROM ai_usage ORDER BY id")] == ["ok", "error"]
    c.close()


def test_ai_ops_and_issue_count_failures(db_path, rid):
    import ai_utils
    for i in range(4):
        ai_utils.log_ai_usage(rid, "insights", "claude-x", 0, 0, db_path=db_path, status="error", error=f"fail {i}")
    ai_utils.log_ai_usage(rid, "insights", "claude-x", 10, 10, db_path=db_path)
    a = admin_ops.ai_ops(30)
    assert a["failed"]["n"] == 4 and a["failures"][0]["n"] == 4
    assert {r["status"] for r in a["recent"]} == {"ok", "error"}
    assert admin_ops.overview()["kpis"]["ai_failures_24h"] == 4
    rec = next(r for r in admin_ops.clients()["clients"] if r["id"] == rid)
    assert rec["ai"]["failed_7d"] == 4
    assert any(i["key"].endswith(":ai_failures") for i in rec["issues"])
    d = admin_ops.client_detail(rid)
    assert len(d["ai"]["failed"]) == 4 and any(e["kind"] == "ai" for e in d["errors"])


def test_run_now_records_a_manual_run(db_path, rid, monkeypatch):
    import scheduler
    calls = []
    monkeypatch.setattr(scheduler, "run_daily_fetch", lambda: calls.append("ran"))
    out = admin_ops.run_job_now("review_fetch", "will")
    assert out["ok"] and out["context"] == "manual by will"
    row = None
    for _ in range(100):
        c = get_conn(db_path)
        row = c.execute("SELECT job, context, ok, finished_at FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
        c.close()
        if row and row["finished_at"]:
            break
        time.sleep(0.05)
    assert calls == ["ran"]
    assert row["job"] == "review_fetch" and row["context"] == "manual by will" and row["ok"] == 1
    assert admin_ops.run_job_now("nope", "will") == {"ok": False, "error": "Unknown job"}
    sched = admin_ops.jobs()["schedule"]
    assert {s["job"] for s in sched} >= set(admin_ops.RUNNABLE_JOBS)
    assert next(s for s in sched if s["job"] == "weekly_digests")["sends"] is True


def test_run_now_refuses_a_job_already_running(db_path, rid, monkeypatch):
    import ops, scheduler
    c = get_conn(db_path)
    c.executescript(ops._RUNS_SQL)
    c.execute("INSERT INTO job_runs (job, context) VALUES ('pos_sync', 'scheduled')")
    c.commit(); c.close()
    monkeypatch.setattr(scheduler, "run_toast_sync", lambda: None)
    out = admin_ops.run_job_now("pos_sync", "will")
    assert out["ok"] is False and "already running" in out["error"]


def test_run_now_route_is_admin_only(monkeypatch, rid):
    from flask import Flask
    from admin_routes import admin_bp
    from auth_routes import auth_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp); app.register_blueprint(auth_bp)
    cl = app.test_client()
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0, "username": "client", "email": "x@x.com", "role": "client"})
    assert cl.post("/admin/api/jobs/review_fetch/run").status_code in (302, 401, 403)
    assert cl.post(f"/admin/api/client/{rid}/alert-cap", json={"max_per_day": 5}).status_code in (302, 401, 403)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": None, "is_admin": 1, "username": "will", "email": "x@x.com", "role": "admin"})
    assert cl.post("/admin/api/jobs/not-a-job/run").status_code == 404
    r = cl.post(f"/admin/api/client/{rid}/alert-cap", json={"max_per_day": 5})
    assert r.status_code == 200 and r.get_json()["max_per_day"] == 5
    assert cl.post(f"/admin/api/client/{rid}/alert-cap", json={"max_per_day": -2}).status_code == 400
    ev = cl.get(f"/admin/api/events?restaurant_id={rid}").get_json()["events"]
    assert ev and ev[0]["event_type"] == "alert_cap.set"


def test_stripe_event_is_kept_and_matched(db_path, rid):
    import admin_events
    models.update_restaurant(rid, {"stripe_customer_id": "cus_123"}, db_path=db_path)
    assert admin_events.record_stripe({"id": "evt_1", "type": "invoice.paid", "data": {"object": {"customer": "cus_123", "amount_paid": 34900, "customer_email": "cap@example.com"}}}, db_path=db_path)
    assert admin_events.record_stripe({"id": "evt_2", "type": "invoice.payment_failed", "data": {"object": {"customer": "cus_nobody", "amount_due": 100, "attempt_count": 2}}}, db_path=db_path)
    ev = admin_events.recent(db_path=db_path)
    assert ev[0]["event_type"] == "invoice.payment_failed" and ev[0]["restaurant_id"] is None
    assert ev[1]["restaurant_id"] == rid and ev[1]["amount"] == 349.0 and "$349.00" in ev[1]["summary"]
    assert "payload" not in ev[0]
    assert [e["event_type"] for e in admin_ops.billing()["events"]][:2] == ["invoice.payment_failed", "invoice.paid"]
    assert [e["event_type"] for e in admin_ops.client_detail(rid)["events"]] == ["invoice.paid"]
    assert any(e["kind"] == "stripe" for e in admin_ops.activity()["events"])
    # matched by owner email when there is no customer id yet
    assert admin_events.record("docusign", "contract.signed", email="cap@example.com", summary="Contract signed", db_path=db_path)
    assert admin_events.recent(restaurant_id=rid, db_path=db_path)[0]["event_type"] == "contract.signed"


def test_alert_cap_toggle(db_path, rid):
    assert admin_ops.set_alert_cap(rid, 12, "will")["ok"]
    assert models.get_restaurant(rid, db_path).alert_max_per_day == 12
    rec = next(r for r in admin_ops.clients()["clients"] if r["id"] == rid)
    assert rec["alert_cap"] == 12
    assert admin_ops.notifications()["caps"][0]["cap"] == 12
    assert admin_ops.set_alert_cap(rid, -1, "will")["ok"] is False
    assert admin_ops.set_alert_cap(rid, "x", "will")["ok"] is False
    assert admin_ops.set_alert_cap(999999, 3, "will")["ok"] is False
    assert admin_ops.set_alert_cap(rid, 0, "will")["ok"]
    assert admin_ops.notifications()["caps"] == []
    assert any(e["event_type"] == "alert_cap.set" for e in admin_ops.client_detail(rid)["events"])


def test_demo_flag_route_and_console_default(monkeypatch, rid):
    """Gia Mia vanished from Clients because it was flagged demo and the page
    hid demo rows by default. Demo rows are now shown (chipped), and the flag
    can be flipped from the console."""
    from flask import Flask
    from admin_routes import admin_bp
    from auth_routes import auth_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp); app.register_blueprint(auth_bp)
    cl = app.test_client()
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": None, "is_admin": 1, "username": "will", "email": "x@x.com", "role": "admin"})
    r = cl.post(f"/admin/api/client/{rid}/demo", json={"is_demo": 1})
    assert r.status_code == 200 and r.get_json()["is_demo"] == 1
    assert next(c for c in admin_ops.clients()["clients"] if c["id"] == rid)["is_demo"] is True
    r = cl.post(f"/admin/api/client/{rid}/demo", json={"is_demo": 0})
    assert r.get_json()["is_demo"] == 0
    assert next(c for c in admin_ops.clients()["clients"] if c["id"] == rid)["is_demo"] is False
    assert cl.post("/admin/api/client/999999/demo", json={"is_demo": 1}).status_code == 404
    html = open("templates/admin.html").read()
    assert "_showDemo = true" in html
    src = open("hosted_dashboard.py").read()
    assert "UPDATE restaurants SET is_demo=1" not in src
