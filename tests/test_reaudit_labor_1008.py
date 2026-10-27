"""Blind re-audit of Labor / the AI schedule (10/8/26) — the server halves.

#1   publish-check returns the server's own `one_tap_safe` (the unattended
     gate), and the publish route's `one_tap: true` runs that gate first —
     acknowledging nothing — and again when an undo window ends.
#6   covers carry the restaurant's business date.
#8   following a run a press was refused for promotes and watches it, and
     never starts anything.
#12  the automation reads say whether this login may switch them.
#14  a generation's watchers are released however the run ends.

Nothing is sent: the staff send and push.fire_push are stubbed; the model is
never called.
"""
from datetime import datetime

import pytest

import client_api
import schedule_publish
import delayed
import models
import ops
import schedule_engine as se
import strategy_jobs
import strategy_routes
import time_utils
from tests.test_ios_parity_labor import _rid, _week, db, pushed  # noqa: F401
from tests.test_ios_parity_labor import _client as _mobile_client


def _client(db, rid, monkeypatch):
    """The mobile app with the strategy routes (publish-check, covers) too."""
    c, h, uid = _mobile_client(db, rid, monkeypatch)
    if "strategy_mobile" not in c.application.blueprints:
        c.application.register_blueprint(strategy_routes.strategy_mobile_bp)
    return c, h, uid


# ── #1 publish-check carries the server's one-tap verdict ────────────────

def test_publish_check_answers_with_the_unattended_one_tap_verdict(db, monkeypatch):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, uid = _client(db, rid, monkeypatch)
    asked = []
    verdict = {"safe": True}
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe",
                        lambda r, s, can_publish=True, review=None: asked.append((r, s, can_publish)) or verdict["safe"])
    body = c.get(f"/mobile/api/labor/publish-check?schedule_id={hid}", headers=h).get_json()
    assert body["one_tap_safe"] is True and asked == [(rid, hid, True)]
    verdict["safe"] = False
    body = c.get(f"/mobile/api/labor/publish-check?schedule_id={hid}", headers=h).get_json()
    assert body["one_tap_safe"] is False


def test_publish_check_never_calls_a_sent_week_one_tap(db, monkeypatch):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _ = _client(db, rid, monkeypatch)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: True)
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    assert c.get(f"/mobile/api/labor/publish-check?schedule_id={hid}", headers=h).get_json()["one_tap_safe"] is False


def test_the_real_gate_holds_one_tap_on_a_note_the_check_shows(db, monkeypatch):
    # A week whose only finding is a note (never a blocker) is not one tap:
    # the phone used to read "no blockers" as safe.
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _ = _client(db, rid, monkeypatch)
    import people
    monkeypatch.setattr(people, "reach", lambda r, names, **k: {n: {"email": "a@x.test"} for n in names})
    monkeypatch.setattr(schedule_publish, "publish_review",
                        lambda r, s, **k: {"blockers": [], "soft": ["A weak week"], "hours": None,
                                           "notes": [{"key": "quality", "text": "A weak week"}]})
    body = c.get(f"/mobile/api/labor/publish-check?schedule_id={hid}", headers=h).get_json()
    assert body["blockers"] == [] and body["notes"] and body["one_tap_safe"] is False


@pytest.fixture
def publishes(monkeypatch):
    got = []
    stub = (lambda rid, sid, actor=None, acknowledge=False, **k: got.append(
        {"rid": rid, "sid": sid, "acknowledge": acknowledge})
        or ({"ok": True, "schedule_id": sid, "sent": [], "unreachable": [], "failed": []}, 200))
    # The route calls client_api's name; the delayed run the service's (ARCH-15).
    monkeypatch.setattr(client_api, "_publish_schedule", stub)
    monkeypatch.setattr(schedule_publish, "publish", stub)
    return got


def test_a_one_tap_send_the_gate_would_not_send_unread_is_refused(db, monkeypatch):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _ = _client(db, rid, monkeypatch)
    # Judged by the publish itself, on its one run of the gate under its
    # claim (scale audit 10/19/26 A5-F14: the route ran the gate for the
    # verdict and the publish ran it again).
    gates = []
    monkeypatch.setattr(schedule_publish, "publish_review", lambda r, s, **k: gates.append(s) or {
        "blockers": [], "soft": ["Ana owes a meal break"], "notes": [], "hours": None})
    r = c.post("/mobile/api/labor/publish-schedule", headers=h,
               json={"schedule_id": hid, "acknowledge": True, "one_tap": True})
    body = r.get_json()
    assert r.status_code == 409 and body["one_tap_refused"] is True and body["schedule_id"] == hid
    assert body["held"] == ["Ana owes a meal break"] and gates == [hid]
    conn = models.get_conn()
    try:
        row = conn.execute("SELECT published_at, publishing_at FROM schedule_history WHERE id=?", (hid,)).fetchone()
    finally:
        conn.close()
    assert row["published_at"] is None and row["publishing_at"] is None     # nothing sent, the claim let go


def test_a_safe_one_tap_send_goes_and_acknowledges_nothing(db, monkeypatch, publishes):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _ = _client(db, rid, monkeypatch)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: True)
    # Even a client that says acknowledge: true acknowledges nothing on one
    # tap — a blocker appearing between the check and the send holds it.
    r = c.post("/mobile/api/labor/publish-schedule", headers=h,
               json={"schedule_id": hid, "acknowledge": True, "one_tap": True})
    assert r.status_code == 200 and publishes == [{"rid": rid, "sid": hid, "acknowledge": []}]


def test_one_tap_on_a_week_staff_have_is_never_a_resend(db, monkeypatch, publishes):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _ = _client(db, rid, monkeypatch)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: True)
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    body = c.post("/mobile/api/labor/publish-schedule", headers=h,
                  json={"schedule_id": hid, "one_tap": True}).get_json()
    assert body["ok"] is True and body["already_published"] is True and publishes == []


def test_a_queued_one_tap_send_runs_the_unattended_gate_when_its_window_ends(db, monkeypatch, publishes):
    rid = _rid(db)
    hid = _week(db, rid)
    models.update_restaurant(rid, {"send_delay_minutes": 10}, db_path=db)
    c, h, _ = _client(db, rid, monkeypatch)
    monkeypatch.setattr(schedule_publish, "one_tap_ok", lambda *a, **k: True)
    queued = []
    # The week's send is queued through queue_schedule_send since BUGSLIFE-9
    # (one pending send per week); this test patched the old delayed.schedule.
    monkeypatch.setattr(delayed, "queue_schedule_send", lambda rid_, kind, payload, delay, **k: queued.append(payload)
                        or {"id": 1, "execute_at": "2026-10-08 12:00:00"})
    body = c.post("/mobile/api/labor/publish-schedule", headers=h,
                  json={"schedule_id": hid, "one_tap": True}).get_json()
    assert body["queued"] is True and queued[0]["one_tap"] is True and queued[0]["acknowledge"] == []
    # When the window ends a soft flag has come up: the publish's own
    # one-tap verdict (under its claim — A5-F14) holds it, the owner told.
    asked = []
    monkeypatch.setattr(schedule_publish, "publish", lambda rid_, sid, actor=None, **k: asked.append(k) or (
        schedule_publish.one_tap_refusal({"soft": ["Ana owes a meal break"]}, sid), 409))
    told = []
    monkeypatch.setattr(delayed, "_tell_owner_schedule_held",
                        lambda r, p, held, dbp, title=None, keys=None: told.append((held, keys)))
    out = delayed._run_schedule_publish(rid, queued[0], db)
    assert out["ok"] is False and "Ana owes a meal break" in out["error"]
    assert told == [(["Ana owes a meal break"], None)] and publishes == [] and asked[0]["one_tap"] is True


def test_a_sheet_send_is_unchanged_by_the_one_tap_gate(db, monkeypatch, publishes):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _ = _client(db, rid, monkeypatch)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe",
                        lambda *a, **k: pytest.fail("a read-and-acknowledged send is not one tap"))
    c.post("/mobile/api/labor/publish-schedule", headers=h, json={"schedule_id": hid, "acknowledge": ["k"]})
    assert publishes == [{"rid": rid, "sid": hid, "acknowledge": ["k"]}]


# ── #6 covers default to the business date ───────────────────────────────

def test_covers_carry_the_restaurants_business_date(db, monkeypatch):
    rid = _rid(db)
    c, h, _ = _client(db, rid, monkeypatch)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda r, naive=True: datetime(2026, 10, 9, 1, 30))
    assert c.get("/mobile/api/labor/covers", headers=h).get_json()["business_date"] == "2026-10-08"
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda r, naive=True: datetime(2026, 10, 9, 18, 0))
    assert c.get("/mobile/api/labor/covers", headers=h).get_json()["business_date"] == "2026-10-09"


# ── #8 following a run in the way ────────────────────────────────────────

def test_following_the_run_in_the_way_promotes_and_watches_it(db, monkeypatch):
    rid = _rid(db)
    c, h, uid = _client(db, rid, monkeypatch)
    monkeypatch.setattr(se, "submit_generation", lambda job_id, r, **k: pytest.fail("follow never starts a run"))
    ops.claim_async_job("auto-run", "schedule", rid)
    promoted = []
    monkeypatch.setattr(se, "promote_generation", lambda j: promoted.append(j))
    r = c.post("/mobile/api/labor/generate-schedule/follow", headers=h, json={"job_id": "auto-run"})
    body = r.get_json()
    assert r.status_code == 200 and body["joined"] is True and body["job_id"] == "auto-run"
    assert body["wait_seconds"] > 0
    assert promoted == ["auto-run"] and se.generation_watchers("auto-run") == {uid: True}


def test_following_a_run_that_already_ended_only_polls_it(db, monkeypatch):
    rid = _rid(db)
    c, h, _ = _client(db, rid, monkeypatch)
    ops.claim_async_job("done-run", "schedule", rid)
    ops.finish_async_job("done-run", "done", {"ok": True})
    monkeypatch.setattr(se, "promote_generation", lambda j: pytest.fail("nothing to promote"))
    body = c.post("/mobile/api/labor/generate-schedule/follow", headers=h, json={"job_id": "done-run"}).get_json()
    assert body["ok"] is True and body["job_id"] == "done-run"
    # Never watched: nothing would ever release it.
    assert se.generation_watchers("done-run") == {}


def test_following_another_restaurants_run_or_none_is_not_found(db, monkeypatch):
    rid = _rid(db)
    c, h, _ = _client(db, rid, monkeypatch)
    other = _rid(db)
    ops.claim_async_job("theirs", "schedule", other)
    assert c.post("/mobile/api/labor/generate-schedule/follow", headers=h, json={"job_id": "theirs"}).status_code == 404
    assert c.post("/mobile/api/labor/generate-schedule/follow", headers=h, json={}).status_code == 404
    assert se.generation_watchers("theirs") == {}


def test_the_follow_route_has_a_web_twin():
    import inspect
    assert "mobile_follow_generation" in inspect.getsource(client_api.follow_generation_json)


# ── #12 the automation reads say who may switch them ─────────────────────

def test_automation_reads_say_whether_this_login_may_switch_them(db):
    rid = _rid(db)
    owner = {"restaurant_id": rid, "is_admin": True}
    member = {"restaurant_id": rid, "role": "member", "grants": []}
    assert strategy_routes._do_auto_draft_get(owner)[0]["can_edit"] is True
    assert strategy_routes._do_auto_publish_get(owner)[0]["can_edit"] is True
    # A teammate may draft, never publish — as the two saves check.
    assert strategy_routes._do_auto_draft_get(member)[0]["can_edit"] is True
    assert strategy_routes._do_auto_publish_get(member)[0]["can_edit"] is False
    from permissions import has_permission, SCHEDULE_PUBLISH
    assert has_permission(member, SCHEDULE_PUBLISH) is False


# ── #14 watchers are released however the run ends ──────────────────────

class _Timeout(Exception):
    pass


def _auto(db, rid, monkeypatch, run):
    class _SE:
        GenerationSlotTimeout = _Timeout
        _run_schedule_job = staticmethod(run)
        # How the job failed (schedule_engine.record_generation_failure):
        # nothing recorded here — the owner's own refusal, not a dead letter.
        take_generation_failure = staticmethod(lambda job_id: None)
    watched = {}

    real_claim = ops.claim_async_job

    def claim(job_id, kind, r, **k):
        out = real_claim(job_id, kind, r, **k)
        se.watch_generation(out[0], {"id": 77, "is_admin": True})
        watched["job"] = out[0]
        return out
    monkeypatch.setattr(ops, "claim_async_job", claim)
    return _SE, watched


# test_a_slot_timeout_releases_the_watchers: removed with GenerationSlots (blind re-audit 10/11/26 ARCH-10 — an auto-draft no longer waits for an in-process slot).


def test_an_exception_out_of_the_run_releases_the_watchers_and_closes_the_job(db, monkeypatch, pushed):
    rid = _rid(db)
    r = models.get_restaurant(rid)

    def run(job_id, rid_, **_kw):
        raise RuntimeError("the generator fell over")
    _SE, watched = _auto(db, rid, monkeypatch, run)
    with pytest.raises(RuntimeError):
        strategy_jobs._draft_one(r, db, _SE, lambda k: None)
    assert se.generation_watchers(watched["job"]) == {} and pushed == []
    assert ops.read_async_job(watched["job"], restaurant_id=rid)["status"] == "error"


def test_a_failed_job_releases_the_watchers(db, monkeypatch, pushed):
    rid = _rid(db)
    r = models.get_restaurant(rid)

    def run(job_id, rid_, **_kw):
        ops.finish_async_job(job_id, "error", {"ok": False, "error": "No sales."})
    _SE, watched = _auto(db, rid, monkeypatch, run)
    strategy_jobs._draft_one(r, db, _SE, lambda k: None)
    assert se.generation_watchers(watched["job"]) == {} and pushed == []


def test_a_press_whose_job_could_not_be_queued_is_not_left_watched(db, monkeypatch):
    rid = _rid(db)
    c, h, uid = _client(db, rid, monkeypatch)

    def boom(job_id, r, **k):
        raise RuntimeError("pool is shut down")
    monkeypatch.setattr(se, "submit_generation", boom)
    try:
        r = c.post("/mobile/api/labor/generate-schedule", headers=h, json={})
        assert r.status_code == 500
    except RuntimeError:
        pass                                 # an app that propagates it
    # Nobody is left watching a job that was never queued (the watchers are
    # rows since scheduler audit 10/10/26 SCALE-24).
    conn = models.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM generation_watchers").fetchone()[0] == 0
    finally:
        conn.close()
    running, _ = ops.running_job("schedule", rid)
    assert running is None


# ── Stop generating (owner, 10/9/26) ────────────────────────────────────────

def test_stopping_a_running_generation_ends_it_saves_nothing_and_frees_the_restaurant(db, monkeypatch):
    rid = _rid(db)
    c, h, _ = _client(db, rid, monkeypatch)
    ops.claim_async_job("oops-run", "schedule", rid)
    r = c.post("/mobile/api/labor/generate-schedule/stop", headers=h, json={"job_id": "oops-run"})
    assert r.status_code == 200 and r.get_json()["stopped"] is True
    job = ops.read_async_job("oops-run", restaurant_id=rid)
    assert job["status"] == "error" and job["result"]["stopped"] is True
    assert job["result"]["error"] == se.STOPPED_MESSAGE
    assert not ops.job_still_pending("oops-run")
    assert ops.running_job("schedule", rid) == (None, None)            # another Generate may start
    # A second press finds it already over and changes nothing.
    again = c.post("/mobile/api/labor/generate-schedule/stop", headers=h, json={"job_id": "oops-run"}).get_json()
    assert again["ok"] is True and again["stopped"] is False


def test_another_restaurants_run_cannot_be_stopped(db, monkeypatch):
    rid = _rid(db)
    c, h, _ = _client(db, rid, monkeypatch)
    other = _rid(db)
    ops.claim_async_job("their-run", "schedule", other)
    assert c.post("/mobile/api/labor/generate-schedule/stop", headers=h, json={"job_id": "their-run"}).status_code == 404
    assert ops.job_still_pending("their-run")


def test_a_stopped_run_starts_no_further_model_call(db, monkeypatch):
    """Behavioural (scheduler fix round 10/10/26, C2): the owner presses Stop
    while the first model call of a run is out. No further paid call starts
    — not the week's next slice, not the quality gate's rewrite, not the
    finished week's note — nothing is saved, and the job says "Stopped"."""
    import labor
    import schedule_job
    import schedule_narrative
    import owner_rules
    # The clock reads the owner's Stop and a poll's verdict from the job row.
    clock = se.GenerationClock(job_id="x")
    with monkeypatch.context() as m:
        m.setattr(ops, "job_still_pending", lambda j: False)
        assert clock.stopped() is True
        m.setattr(ops, "job_still_pending", lambda j: True)
        assert clock.stopped() is False
    assert se.GenerationClock(job_id=None).stopped() is False

    WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]

    class _Clock(se.GenerationClock):
        def __init__(self):
            super().__init__(job_id=None)
            self.stop = False

        def stopped(self):
            return self.stop

        def stop_requested(self):
            return self.stop

    def _rows(dates):
        return [{"date": d, "day": se._date_of(d).strftime("%A"), "employee": "Ana", "role": "Server",
                 "shift_start": "11:00am", "shift_end": "3:00pm", "scheduled_hours": "4", "notes": ""}
                for d in dates]

    # 1. Inside one draft: a week planned as two calls, one at a time. The
    #    Stop lands during the first; the second is never sent.
    monkeypatch.setenv("SCHEDULE_PARALLEL_CALLS", "1")
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 2 * se.CHUNK_ROWS_PER_CALL - 10)
    c1, sent = _Clock(), []

    def gen(analysis, shifts, week_slice=None, prior_rows=None, **kw):
        sent.append(list(week_slice or WEEK))
        c1.stop = True                                   # the owner presses Stop while this call is out
        dates = list(week_slice or WEEK)
        return {"schedule_csv": "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                + "\n".join(",".join(r[k] for k in se._ANSWER_COLS) for r in _rows(dates)),
                "narrative": [], "complete_dates": dates, "generation_seconds": 0.1}
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    token = se._CLOCK.set(c1)
    try:
        with pytest.raises(se.ScheduleStopped):
            se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None,
                                                                "roster": [("Ana", "Server")]})
    finally:
        se._CLOCK.reset(token)
    assert len(sent) == 1, "a slice was sent after the Stop"

    # 2. Before the draft: the owner's words are a model call (owner_rules),
    #    and a run already stopped never makes it.
    read = []
    monkeypatch.setattr(owner_rules, "prepare", lambda *a, **k: read.append(1) or {"instruction": []})
    c2 = _Clock()
    c2.stop = True
    token = se._CLOCK.set(c2)
    try:
        with pytest.raises(se.ScheduleStopped):
            se._build_schedule_result(_rid(db), week_start=WEEK[0], instruction="Maria closes Friday")
    finally:
        se._CLOCK.reset(token)
    assert read == []

    # 3. The whole job: the Stop lands during the week's model call, with the
    #    quality gate wanting a rewrite. Neither the rewrite nor the note is
    #    paid for, and nothing is saved.
    c3 = _Clock()
    calls, notes = [], []

    def week(r, **kw):
        calls.append(kw)
        c3.stop = True                                   # pressed while the week's call was out
        return {"ok": True, "rows": _rows(WEEK), "week_dates": WEEK,
                "schedule_csv": "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                + "\n".join(",".join(x[k] for k in se._ANSWER_COLS) for x in _rows(WEEK)),
                "week_days": [se._date_of(d).strftime("%A") for d in WEEK], "summary": [], "hours_budget": 0,
                "daily_target_hours": {}, "labor_target": 30, "unwritten_dates": [], "closed_dates": [],
                "narrative": []}
    monkeypatch.setattr(se, "_build_schedule_result", week)
    monkeypatch.setattr(se, "_quality_gate", lambda result: {"dates": [WEEK[5]], "focus": ["short"],
                                                             "reason": "a hole", "kinds": ["coverage"]})
    monkeypatch.setattr(schedule_narrative, "write", lambda *a, **k: notes.append(1))
    saved, finished = [], {}
    monkeypatch.setattr(se, "_save_durably", lambda *a, **k: saved.append(1) or 1)
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    monkeypatch.setattr(se._ops, "job_still_pending", lambda j: True)
    monkeypatch.setattr(se._ops, "job_stop_state", lambda j: "stopped" if c3.stop else "running")
    token = se._CLOCK.set(c3)
    try:
        se._run_schedule_job("stop-job", _rid(db))
    finally:
        se._CLOCK.reset(token)
    assert len(calls) == 1 and notes == [] and saved == []
    assert finished["status"] == "error" and finished["result"]["error"] == se.STOPPED_MESSAGE
    # And the job never re-enters itself to get there (ARCH-16).
    assert schedule_job.GATE_ATTEMPTS == ("fill", "rewrite")
