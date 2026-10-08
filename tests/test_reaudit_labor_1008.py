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
                        lambda r, s, can_publish=True: asked.append((r, s, can_publish)) or verdict["safe"])
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
    monkeypatch.setattr(client_api, "publish_review",
                        lambda r, s, **k: {"blockers": [], "soft": ["A weak week"], "hours": None,
                                           "notes": [{"key": "quality", "text": "A weak week"}]})
    body = c.get(f"/mobile/api/labor/publish-check?schedule_id={hid}", headers=h).get_json()
    assert body["blockers"] == [] and body["notes"] and body["one_tap_safe"] is False


@pytest.fixture
def publishes(monkeypatch):
    got = []
    monkeypatch.setattr(client_api, "_publish_schedule",
                        lambda rid, sid, actor=None, acknowledge=False: got.append(
                            {"rid": rid, "sid": sid, "acknowledge": acknowledge})
                        or ({"ok": True, "schedule_id": sid, "sent": [], "unreachable": [], "failed": []}, 200))
    return got


def test_a_one_tap_send_the_gate_would_not_send_unread_is_refused(db, monkeypatch, publishes):
    rid = _rid(db)
    hid = _week(db, rid)
    c, h, _ = _client(db, rid, monkeypatch)
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: False)
    r = c.post("/mobile/api/labor/publish-schedule", headers=h,
               json={"schedule_id": hid, "acknowledge": True, "one_tap": True})
    body = r.get_json()
    assert r.status_code == 409 and body["one_tap_refused"] is True and body["schedule_id"] == hid
    assert publishes == []


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
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: True)
    queued = []
    monkeypatch.setattr(delayed, "schedule", lambda rid_, kind, payload, delay, **k: queued.append(payload)
                        or {"id": 1, "execute_at": "2026-10-08 12:00:00"})
    body = c.post("/mobile/api/labor/publish-schedule", headers=h,
                  json={"schedule_id": hid, "one_tap": True}).get_json()
    assert body["queued"] is True and queued[0]["one_tap"] is True and queued[0]["acknowledge"] == []
    # When the window ends a soft flag has come up: held, the owner told.
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: False)
    monkeypatch.setattr(client_api, "publish_review",
                        lambda r, s, **k: {"blockers": [], "soft": ["Ana owes a meal break"], "notes": []})
    told = []
    monkeypatch.setattr(delayed, "_tell_owner_schedule_held",
                        lambda r, p, held, dbp, title=None, keys=None: told.append((held, keys)))
    out = delayed._run_schedule_publish(rid, queued[0], db)
    assert out["ok"] is False and "Ana owes a meal break" in out["error"]
    assert told == [(["Ana owes a meal break"], None)] and publishes == []
    # Still safe at the window's end: it goes.
    monkeypatch.setattr(strategy_routes, "draft_one_tap_safe", lambda *a, **k: True)
    delayed._run_schedule_publish(rid, queued[0], db)
    assert publishes == [{"rid": rid, "sid": hid, "acknowledge": []}]


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
    assert promoted == ["auto-run"] and se._GEN_WATCHERS["auto-run"] == {uid: True}


def test_following_a_run_that_already_ended_only_polls_it(db, monkeypatch):
    rid = _rid(db)
    c, h, _ = _client(db, rid, monkeypatch)
    ops.claim_async_job("done-run", "schedule", rid)
    ops.finish_async_job("done-run", "done", {"ok": True})
    monkeypatch.setattr(se, "promote_generation", lambda j: pytest.fail("nothing to promote"))
    body = c.post("/mobile/api/labor/generate-schedule/follow", headers=h, json={"job_id": "done-run"}).get_json()
    assert body["ok"] is True and body["job_id"] == "done-run"
    # Never watched: nothing would ever release it.
    assert "done-run" not in se._GEN_WATCHERS


def test_following_another_restaurants_run_or_none_is_not_found(db, monkeypatch):
    rid = _rid(db)
    c, h, _ = _client(db, rid, monkeypatch)
    other = _rid(db)
    ops.claim_async_job("theirs", "schedule", other)
    assert c.post("/mobile/api/labor/generate-schedule/follow", headers=h, json={"job_id": "theirs"}).status_code == 404
    assert c.post("/mobile/api/labor/generate-schedule/follow", headers=h, json={}).status_code == 404
    assert "theirs" not in se._GEN_WATCHERS


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
    watched = {}

    real_claim = ops.claim_async_job

    def claim(job_id, kind, r, **k):
        out = real_claim(job_id, kind, r, **k)
        se.watch_generation(out[0], {"id": 77, "is_admin": True})
        watched["job"] = out[0]
        return out
    monkeypatch.setattr(ops, "claim_async_job", claim)
    return _SE, watched


def test_a_slot_timeout_releases_the_watchers(db, monkeypatch, pushed):
    rid = _rid(db)
    r = models.get_restaurant(rid)

    def run(job_id, rid_):
        raise _Timeout()
    _SE, watched = _auto(db, rid, monkeypatch, run)
    strategy_jobs._draft_one(r, db, _SE, lambda k: None)
    assert watched["job"] not in se._GEN_WATCHERS and pushed == []


def test_an_exception_out_of_the_run_releases_the_watchers_and_closes_the_job(db, monkeypatch, pushed):
    rid = _rid(db)
    r = models.get_restaurant(rid)

    def run(job_id, rid_):
        raise RuntimeError("the generator fell over")
    _SE, watched = _auto(db, rid, monkeypatch, run)
    with pytest.raises(RuntimeError):
        strategy_jobs._draft_one(r, db, _SE, lambda k: None)
    assert watched["job"] not in se._GEN_WATCHERS and pushed == []
    assert ops.read_async_job(watched["job"], restaurant_id=rid)["status"] == "error"


def test_a_failed_job_releases_the_watchers(db, monkeypatch, pushed):
    rid = _rid(db)
    r = models.get_restaurant(rid)

    def run(job_id, rid_):
        ops.finish_async_job(job_id, "error", {"ok": False, "error": "No sales."})
    _SE, watched = _auto(db, rid, monkeypatch, run)
    strategy_jobs._draft_one(r, db, _SE, lambda k: None)
    assert watched["job"] not in se._GEN_WATCHERS and pushed == []


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
    assert se._GEN_WATCHERS == {}
    running, _ = ops.running_job("schedule", rid)
    assert running is None
