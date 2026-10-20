"""Blind re-audit 10/20/26 R5-03: the Schedule Studio's heavy reads run in the
queue worker, the client polls a ticket, and the answer is exactly the
route's own. Nothing is sent; the model is never called."""
import json

import pytest

import job_queue
import models
import strategy_routes
import studio_offload
from tests.test_ios_parity_labor import _rid, db, pushed  # noqa: F401
from tests.test_ios_parity_labor import _client as _mobile_client

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
OFF = {studio_offload.OFFLOAD_HEADER: "1"}


@pytest.fixture(autouse=True)
def _before_the_week(monkeypatch):
    import datetime as _dt
    import schedule_publish as _sp_fx
    monkeypatch.setattr(_sp_fx, "_local_today", lambda rid: _dt.date(2026, 10, 9))
    monkeypatch.setattr(strategy_routes, "_local_today", lambda *a, **k: _dt.date(2026, 10, 9))


@pytest.fixture
def worker(monkeypatch):
    """A live queue worker (so the web process never drains the row itself);
    the test runs the row with run_one."""
    job_queue.reset_for_tests()
    monkeypatch.setattr(job_queue, "worker_alive", lambda db_path=None: True)
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    monkeypatch.delenv("QUEUE_WORKER", raising=False)
    monkeypatch.delenv("STUDIO_OFFLOAD", raising=False)
    studio_offload._COST.clear()
    yield
    studio_offload._COST.clear()


def _client(db, rid, monkeypatch):
    c, h, uid = _mobile_client(db, rid, monkeypatch)
    if "strategy_mobile" not in c.application.blueprints:
        c.application.register_blueprint(strategy_routes.strategy_mobile_bp)
    return c, h, uid


def _week(db, rid):
    for name, role in (("Erik", "Manager"), ("Ana", "Server")):
        models.add_manual_team_member(rid, name, role=role, db_path=db)
    csv = HEADER + "\n2026-10-12,Monday,Erik,Manager,10:00am,10:30pm,12.5,\n2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,"
    return models.save_schedule_history(rid, "2026-10-12", "2026-10-18", 10, 0, 30, csv, [], db_path=db)


def _run_studio_row():
    job = job_queue.claim("test-worker", groups=("studio",))
    assert job is not None and job["kind"] == studio_offload.KIND
    job_queue.run_one(job)
    conn = models.get_conn()
    try:
        row = conn.execute("SELECT state, result_json FROM jobs WHERE id=?", (job["id"],)).fetchone()
    finally:
        conn.close()
    assert row["state"] == "done" and json.loads(row["result_json"])["status"] in (200, 400, 404)
    return job


def _strip(body):
    return {k: v for k, v in body.items() if k not in ("checked_at",)}


def test_a_slow_restaurants_review_is_answered_with_a_ticket_and_then_the_routes_own_answer(db, monkeypatch, worker):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _uid = _client(db, rid, monkeypatch)
    url = f"/mobile/api/labor/publish-review?schedule_id={hid}"
    # The first call is measured here (no cost known yet): inline.
    direct = c.get(url, headers=dict(h, **OFF))
    assert direct.status_code == 200 and direct.get_json()["schedule_id"] == hid
    # A restaurant whose reviews are slow: the next call is a ticket.
    studio_offload.record("publish_review", rid, 6.0)
    first = c.get(url, headers=dict(h, **OFF))
    assert first.status_code == 202 and first.headers.get(studio_offload.TICKET_HEADER)
    t = first.get_json()
    assert t["pending"] and t["poll"] == f"/mobile/api/labor/offload/{t['ticket']}" and t["retry_after_ms"] > 0
    # Still waiting: 202 again.
    again = c.get(t["poll"], headers=h)
    assert again.status_code == 202 and again.get_json()["queue_ahead"] == 0
    _run_studio_row()
    done = c.get(t["poll"], headers=h)
    assert done.status_code == 200
    assert _strip(done.get_json()) == _strip(direct.get_json())
    # No header (an app build in the field): synchronous, as before.
    assert c.get(url, headers=h).status_code == 200
    # The kill switch.
    monkeypatch.setenv("STUDIO_OFFLOAD", "0")
    assert c.get(url, headers=dict(h, **OFF)).status_code == 200


def test_a_ticket_is_answered_only_to_the_login_that_started_it(db, monkeypatch, worker):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _uid = _client(db, rid, monkeypatch)
    studio_offload.record("publish_check", rid, 6.0)
    t = c.get(f"/mobile/api/labor/publish-check?schedule_id={hid}", headers=dict(h, **OFF)).get_json()
    _run_studio_row()
    row = models.get_conn().execute("SELECT payload_json FROM jobs WHERE id=?", (t["ticket"],)).fetchone()
    stored = json.loads(row["payload_json"])
    assert not any(k for k in stored["user"] if "password" in k.lower() or "hash" in k.lower())
    # Another login of the restaurant: not found.
    conn = models.get_conn()
    conn.execute("UPDATE jobs SET payload_json=? WHERE id=?",
                 (json.dumps(dict(stored, user=dict(stored["user"], id=-7))), t["ticket"]))
    conn.commit()
    conn.close()
    assert c.get(t["poll"], headers=h).status_code == 404


def test_a_row_that_failed_is_answered_by_the_poll_itself(db, monkeypatch, worker):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _uid = _client(db, rid, monkeypatch)
    studio_offload.record("schedule_history_detail", rid, 6.0)
    first = c.get(f"/mobile/api/labor/schedule-history/{hid}", headers=dict(h, **OFF))
    assert first.status_code == 202
    t = first.get_json()
    conn = models.get_conn()
    conn.execute("UPDATE jobs SET state='expired' WHERE id=?", (t["ticket"],))
    conn.commit()
    conn.close()
    done = c.get(t["poll"], headers=h)
    assert done.status_code == 200 and done.get_json()["id"] == hid and done.get_json()["preview_rows"]


def test_the_rows_on_screen_check_runs_in_the_worker_with_its_posted_rows(db, monkeypatch, worker):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _uid = _client(db, rid, monkeypatch)
    rows = [{"date": "2026-10-12", "day": "Monday", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    body = {"rows": rows, "history_id": hid}
    direct = c.post("/mobile/api/labor/schedule/violations", headers=h, json=body)
    studio_offload.record("schedule_violations", rid, 6.0)
    first = c.post("/mobile/api/labor/schedule/violations", headers=dict(h, **OFF), json=body)
    assert first.status_code == 202
    _run_studio_row()
    done = c.get(first.get_json()["poll"], headers=h)
    assert done.status_code == direct.status_code
    assert done.get_json()["violations"] == direct.get_json()["violations"]


def test_a_pending_generation_poll_says_where_it_stands_and_when_to_ask_again(db, monkeypatch, worker):
    rid = _rid(db)
    for n in range(3):
        job_queue.enqueue("schedule_generate", {}, restaurant_id=rid, external_id=f"ahead-{n}", wake=False)
    job_queue.enqueue("schedule_generate", {}, restaurant_id=rid, external_id="mine", wake=False)
    state, ahead = job_queue.queue_position("mine")
    assert (state, ahead) == ("queued", 3)
    assert job_queue.poll_after_ms(state, ahead) == 2300
    assert job_queue.poll_after_ms("running", 0) == 2000
    assert job_queue.poll_after_ms("queued", 5000) == 15000
    assert job_queue.queue_position("nobody") == (None, None)


def test_the_clients_poll_the_ticket_and_the_servers_wait():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    page = open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "'X-Cavnar-Offload', '1'" in page and "X-Cavnar-Ticket" in page
    assert "_schedNext(pollData.retry_after_ms)" in page
    api = open(os.path.join(root, "ios", "CavnarAI", "CavnarAI", "Core", "APIClient.swift"), encoding="utf-8").read()
    assert 'offloadHeader = "X-Cavnar-Offload"' in api and "settleOffloaded" in api
    vm = open(os.path.join(root, "ios", "CavnarAI", "CavnarAI", "Features", "Labor", "LaborViewModel.swift"),
              encoding="utf-8").read()
    assert "result.retryAfterMs" in vm


def test_finished_studio_rows_are_pruned_after_their_polls(db, monkeypatch, worker):
    import time
    rid = _rid(db)
    qid, _c = job_queue.enqueue(studio_offload.KIND, {"endpoint": "publish_check"}, restaurant_id=rid, wake=False)
    other, _c = job_queue.enqueue("learner", {}, restaurant_id=rid, wake=False)
    conn = models.get_conn()
    conn.execute("UPDATE jobs SET state='done', finished_at=? WHERE id IN (?, ?)",
                 (time.time() - studio_offload.RESULT_KEEP_SECONDS - 5, qid, other))
    conn.commit()
    conn.close()
    assert studio_offload._prune() == 1
    left = {r[0] for r in models.get_conn().execute("SELECT id FROM jobs").fetchall()}
    assert qid not in left and other in left
