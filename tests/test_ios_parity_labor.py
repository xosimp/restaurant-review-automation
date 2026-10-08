"""iOS parity, Labor & the AI schedule (10/7/26).

#16  A generation the owner pressed for pushes `schedule_drafted` to that
     login alone when the draft lands — with the week (`schedule_id`, so the
     push opens it in the editor, #5) and `one_tap_safe`, the one rule that
     lets the lock screen offer Send to staff (Waiting on you's: unsent, not
     replaced, nothing for the publish gate, somebody to reach, a login that
     may publish). The auto-draft's push carries the same.
#15  A press refused because another week is being built (409) names that
     run's job, so the phone can show that week and follow it.

Nothing is sent: push.fire_push is captured; the model is never called.
"""
import sys

import pytest
from flask import Flask

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import schedule_economics, schedule_intel, schedule_versions, shift_requests, staff_schedule  # noqa: E401,F401
import staff_settings, time_off  # noqa: E401,F401
import ai_utils
import auth
import client_api
import mobile_api
import models
import ops
import people
import push
import schedule_engine as se
import strategy_jobs
import strategy_routes
from models import Restaurant, create_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    se._GEN_WATCHERS.clear()
    return db_path


@pytest.fixture
def pushed(monkeypatch):
    got = []
    monkeypatch.setattr(push, "fire_push", lambda rid, t, title, body, data=None, **k: got.append(
        {"rid": rid, "type": t, "title": title, "body": body, "data": dict(data or {}), "user_ids": k.get("user_ids")})
        or 1)
    return got


def _rid(db):
    return create_restaurant(Restaurant(name="Parity Grill", owner_email="p@x.test", module_labor=1), db_path=db)


def _week(db, rid, csv=None):
    return models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 10, 0, 30,
                                        csv or (HEADER + "\n2026-10-12,Monday,Ana,Server,5:00pm,10:00pm,5,"), [],
                                        db_path=db)


def _finish(job_id, rid, **result):
    ops.claim_async_job(job_id, "schedule", rid)
    ops.finish_async_job(job_id, "done", dict({"ok": True, "week_dates": ["2026-10-12"]}, **result))


# ── push.py: the category and where it opens ─────────────────────────────

def test_a_drafted_week_gets_review_and_send_and_opens_the_week():
    # Send to staff only rides a push the server marked one-tap safe; a
    # category's buttons are fixed on the phone, so the other gets Review only.
    assert push._category("schedule_drafted", {"schedule_id": 41, "one_tap_safe": True}) \
        == push.CATEGORY_SCHEDULE == "CAVNAR_SCHEDULE"
    assert push._category("schedule_drafted", {"schedule_id": 41, "one_tap_safe": False}) \
        == push.CATEGORY_SCHEDULE_REVIEW == "CAVNAR_SCHEDULE_REVIEW"
    assert push._category("schedule_drafted", {"schedule_id": 41}) == push.CATEGORY_SCHEDULE_REVIEW
    assert push._category("schedule_drafted", {}) == ""
    assert push.nav_for("schedule_drafted", {"schedule_id": 41}) == "schedule/41"
    assert push.nav_for("schedule_drafted", {}) == "labor/schedule"


# ── who is told, and with what ───────────────────────────────────────────

def test_only_the_login_that_pressed_is_pushed_with_the_week(db, pushed, monkeypatch):
    rid = _rid(db)
    hid = _week(db, rid)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda r, s, can_publish=True: can_publish)
    se.watch_generation("job-a", {"id": 7, "is_admin": True})
    _finish("job-a", rid, history_id=hid)
    assert se.notify_generation_watchers("job-a", rid) == 1
    assert len(pushed) == 1
    p = pushed[0]
    assert p["type"] == "schedule_drafted" and p["user_ids"] == {7}
    # job_id: the phone watching this generation keeps the banner down.
    assert p["data"] == {"schedule_id": hid, "one_tap_safe": True, "job_id": "job-a"}
    assert p["title"] == "The week of 10/12/26 is drafted"
    # Told once: the watcher is spent.
    assert se.notify_generation_watchers("job-a", rid) == 0 and len(pushed) == 1


def test_a_login_that_cannot_publish_is_never_offered_send(db, pushed, monkeypatch):
    rid = _rid(db)
    hid = _week(db, rid)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda r, s, can_publish=True: can_publish)
    se.watch_generation("job-b", {"id": 8, "role": "member", "grants": []})
    _finish("job-b", rid, history_id=hid)
    se.notify_generation_watchers("job-b", rid)
    assert pushed[0]["data"]["one_tap_safe"] is False


def test_a_week_with_days_not_written_is_said_and_never_one_tap(db, pushed, monkeypatch):
    rid = _rid(db)
    hid = _week(db, rid)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: True)
    se.watch_generation("job-c", {"id": 9, "is_admin": True})
    _finish("job-c", rid, history_id=hid, unwritten_dates=[{"date": "2026-10-14"}])
    se.notify_generation_watchers("job-c", rid)
    assert pushed[0]["data"]["one_tap_safe"] is False
    assert "partly drafted" in pushed[0]["title"] and "1 day couldn't be written" in pushed[0]["body"]


@pytest.mark.parametrize("status,result", [
    ("error", {"ok": False, "error": "The model refused."}),
    ("done", {"ok": False, "error": "No sales."}),
    ("done", {"ok": True}),                          # nothing saved: no week to open
])
def test_a_job_that_saved_no_week_pushes_nothing(db, pushed, status, result):
    rid = _rid(db)
    se.watch_generation("job-d", {"id": 7, "is_admin": True})
    ops.claim_async_job("job-d", "schedule", rid)
    ops.finish_async_job("job-d", status, result)
    assert se.notify_generation_watchers("job-d", rid) == 0 and pushed == []
    assert "job-d" not in se._GEN_WATCHERS


def test_the_pool_tells_the_watchers_when_its_job_ends(db, pushed, monkeypatch):
    rid = _rid(db)
    hid = _week(db, rid)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: False)
    ops.claim_async_job("job-e", "schedule", rid)

    def fake_job(job_id, r, **k):
        ops.finish_async_job(job_id, "done", {"ok": True, "history_id": hid, "week_dates": ["2026-10-12"]})
    monkeypatch.setattr(se, "_run_schedule_job", fake_job)
    se.watch_generation("job-e", {"id": 5, "is_admin": True})
    se.submit_generation("job-e", rid).result(10)
    assert [p["user_ids"] for p in pushed] == [{5}]
    assert pushed[0]["data"] == {"schedule_id": hid, "one_tap_safe": False, "job_id": "job-e"}


# ── the rule itself ──────────────────────────────────────────────────────

def test_one_tap_safe_is_waiting_on_yous_rule(db, monkeypatch):
    rid = _rid(db)
    hid = _week(db, rid)
    blockers = []
    monkeypatch.setattr(client_api, "publish_review", lambda r, s, **k: {"blockers": list(blockers)})
    monkeypatch.setattr(people, "reach", lambda r, names, **k: {n: {"email": "a@x.test"} for n in names})
    assert strategy_routes.draft_one_tap_safe(rid, hid) is True
    # A login that may not publish, a blocker to read, nobody on the week.
    assert strategy_routes.draft_one_tap_safe(rid, hid, can_publish=False) is False
    blockers.append({"key": "hard", "text": "Ana is past 40 hours"})
    assert strategy_routes.draft_one_tap_safe(rid, hid) is False
    blockers.clear()
    empty = _week(db, rid, csv=HEADER + "\n")
    assert strategy_routes.draft_one_tap_safe(rid, empty) is False
    # A week already sent, or another restaurant's.
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    assert strategy_routes.draft_one_tap_safe(rid, hid) is False
    other = _rid(db)
    assert strategy_routes.draft_one_tap_safe(other, _week(db, rid)) is False


# ── the route: watched on start and on a join; a 409 names the run ──────

def _client(db, rid, monkeypatch):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    uid = auth.create_user(rid, "owner", "owner@x.test", "pw", db_path=db)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    return app.test_client(), {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}, uid


def test_a_press_is_watched_by_the_login_that_pressed(db, monkeypatch):
    rid = _rid(db)
    c, h, uid = _client(db, rid, monkeypatch)
    started = []
    monkeypatch.setattr(se, "submit_generation", lambda job_id, r, **k: started.append(job_id))
    r = c.post("/mobile/api/labor/generate-schedule", headers=h, json={})
    assert r.status_code == 200
    job = r.get_json()["job_id"]
    assert started == [job] and se._GEN_WATCHERS[job] == {uid: True}


def test_a_press_for_another_week_names_the_run_in_the_way(db, monkeypatch):
    rid = _rid(db)
    c, h, uid = _client(db, rid, monkeypatch)
    monkeypatch.setattr(se, "submit_generation", lambda job_id, r, **k: None)
    first = c.post("/mobile/api/labor/generate-schedule", headers=h, json={}).get_json()
    other = c.post("/mobile/api/labor/generate-schedule", headers=h, json={"instruction": "Keep Ana off"})
    body = other.get_json()
    assert other.status_code == 409 and body["busy"] is True
    assert body["job_id"] == first["job_id"] and body["running"]["week_start"]
    assert body["wait_seconds"] > 0 and "Nothing was started" in body["error"]


# ── the auto-draft's push carries the week too ───────────────────────────

def test_the_auto_drafts_push_opens_the_week(db, pushed, monkeypatch):
    import morning_brief
    import notify
    rid = _rid(db)
    hid = _week(db, rid)
    r = models.get_restaurant(rid)
    monkeypatch.setattr(ops, "read_async_job", lambda *a, **k: {"status": "done",
                                                                "result": {"ok": True, "history_id": hid}})
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 3, "is_admin": True}])
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(notify, "record_notification", lambda *a, **k: None)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: True)

    class _SE:
        @staticmethod
        def _run_schedule_job(job_id, rid_):
            return None
    strategy_jobs._draft_one(r, db, _SE, lambda k: None)
    assert pushed and pushed[0]["data"] == {"schedule_id": hid, "one_tap_safe": True}
    assert pushed[0]["user_ids"] == {3}
