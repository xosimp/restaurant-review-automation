"""The phone's silent pushes and Live Activities (iOS parity audit 10/7/26
#31, #38, #61, #94): push.py's background and liveactivity sending, the
token routes, and what live_activities.py sends for a queued send, a
schedule generation and tonight's service — including that the JSON it
sends matches the Swift ActivityAttributes it is decoded into."""
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import auth
import client_api
import live_activities
import mobile_api
import models
import push
from auth import create_user, create_session, hash_session_token, set_user_role, init_auth
from models import create_restaurant, Restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(db_path, monkeypatch):
    init_auth(db_path=db_path)
    push.init_push(db_path)
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, push, mobile_api, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    return db_path


@pytest.fixture
def apns(monkeypatch):
    """Every APNs post, captured and answered 200 — nothing leaves — and the
    deliveries run inline. Native pushes are allowed (as on Railway)."""
    posts = []

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    class _Client:
        def post(self, url, content=None, headers=None):
            posts.append({"url": url, "payload": json.loads(content), "headers": dict(headers or {})})
            return _Resp()

    monkeypatch.setattr(push, "_client", lambda: _Client())
    monkeypatch.setattr(push, "_provider_jwt", lambda: "jwt")
    monkeypatch.setattr(push, "native_push_allowed", lambda: True)
    monkeypatch.setattr(push, "_submit_native", lambda fn, *args: (fn(*args), True)[1])
    return posts


def _restaurant(db, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Live Co"), owner_email="o@x.test", **kw), db_path=db)


def _login(db, rid, username="owner", role=None):
    uid = create_user(rid, username, f"{username}@x.test", "correct-horse-battery", db_path=db)
    if role:
        set_user_role(uid, role, db_path=db)
    token = create_session(uid, device_type="ios", db_path=db)
    return uid, token, hash_session_token(token)


def _device(db, uid, rid, token="a" * 64, tier="owner"):
    push.register_device_token(uid, rid, token, "production", db_path=db, tier=tier)


def _swift(*parts):
    return open(os.path.join(ROOT, "ios", "CavnarAI", *parts), encoding="utf-8").read()


def _swift_fields(src, struct):
    """`var name` / `let name` declared directly in a Swift struct body."""
    start = src.index(f"struct {struct}")
    body = src[start:]
    depth, out, i = 0, [], body.index("{")
    for line in body[i:].splitlines():
        if depth == 1:
            m = re.match(r"\s*(?:var|let)\s+(\w+)\s*:", line)
            if m:
                out.append(m.group(1))
        depth += line.count("{") - line.count("}")
        if depth <= 0 and out:
            break
    return set(out)


# ── dates ────────────────────────────────────────────────────────────────────

def test_a_content_state_date_counts_from_2001():
    """ActivityKit decodes a Date in content-state with JSONDecoder's
    default strategy — seconds since 2001-01-01, not Unix time."""
    assert push.apple_date(datetime(2001, 1, 1, tzinfo=timezone.utc)) == 0
    at = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc)
    assert push.apple_date(at) == at.timestamp() - 978307200
    assert push.apple_date("2026-10-09T23:00:00Z") == at.timestamp() - 978307200
    assert push.apple_date("2026-10-09 23:00:00") == at.timestamp() - 978307200
    assert push.apple_date(None) is None and push.apple_date("not a date") is None


# ── silent pushes (#31) ──────────────────────────────────────────────────────

def test_a_silent_push_is_a_background_push_with_nothing_to_show(db, apns):
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid)
    assert push.fire_silent(rid, "dsr", db_path=db) == 1
    sent = apns[0]
    assert sent["headers"]["apns-push-type"] == "background"
    assert sent["headers"]["apns-priority"] == "5"
    assert sent["headers"]["apns-topic"] == push._bundle_id()
    # content-available and nothing else: Apple drops a background push
    # that carries an alert, a sound or a badge.
    assert sent["payload"]["aps"] == {"content-available": 1}
    assert sent["payload"]["cavnar"] == {"silent": "dsr", "restaurant_id": rid}


def test_silent_pushes_are_throttled_per_restaurant_and_reason(db, apns):
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid)
    assert push.fire_silent(rid, "waiting", user_ids=[uid], db_path=db) == 1
    assert push.fire_silent(rid, "waiting", user_ids=[uid], db_path=db) == 0
    assert push.fire_silent(rid, "dsr", db_path=db) == 1
    assert push.fire_silent(rid, "made_up", db_path=db) == 0


def test_a_silent_push_never_reaches_a_staff_phone(db, apns):
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid, token="b" * 64, tier="staff")
    assert push.fire_silent(rid, "dsr", user_ids=[uid], db_path=db) == 0
    assert apns == []


def test_nothing_native_leaves_a_local_backend(db, monkeypatch):
    for v in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
              "ALLOW_LOCAL_SCHEDULER"):
        monkeypatch.delenv(v, raising=False)
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid)
    assert push.native_push_allowed() is False
    assert push.fire_silent(rid, "dsr", db_path=db) == 0
    assert push.fire_live_activity([{"id": 1, "apns_token": "c" * 64}], "service", "update", {}) == 0


def test_an_actionable_alert_also_wakes_the_widget(db, apns, monkeypatch):
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid)
    monkeypatch.setattr(push, "_push_executor", lambda: type("E", (), {"submit": lambda self, *a, **k: None})())
    push.fire_push(rid, "1star", "A 1-star review", "Slow service.", data={"review_id": 9}, db_path=db)
    assert [p["payload"]["cavnar"]["silent"] for p in apns] == ["waiting"]


def test_the_dsr_delivery_wakes_the_widgets(db, apns, monkeypatch):
    import dsr.deliver as deliver
    from dsr import store
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid)
    monkeypatch.setattr(deliver, "_allowed", lambda: True)
    monkeypatch.setattr(store, "get_report_by_id",
                        lambda *a, **k: {"status": "final", "business_date": "2026-10-06", "version": 1})
    out = deliver.on_terminal(models.get_restaurant(rid, db_path=db), 1, db_path=db)
    assert out.get("reason") == "delivery is off for this restaurant"
    assert [p["payload"]["cavnar"]["silent"] for p in apns] == ["dsr"]


def test_a_web_read_of_a_changed_queue_wakes_that_logins_phone(db, apns):
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid)
    assert live_activities.note_waiting_count(rid, uid, 3, from_web=True) == 0      # the first read only records
    assert live_activities.note_waiting_count(rid, uid, 3, from_web=True) == 0      # unchanged
    assert live_activities.note_waiting_count(rid, uid, 2, from_web=False) == 0     # the phone's own read
    assert live_activities.note_waiting_count(rid, uid, 4, from_web=True) == 1
    assert apns[-1]["payload"]["cavnar"]["silent"] == "waiting"


# ── the token routes ─────────────────────────────────────────────────────────

@pytest.fixture
def mobile(db):
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def test_a_phone_files_its_tokens_and_signing_out_stops_them(db, mobile):
    rid = _restaurant(db)
    uid, token, session_hash = _login(db, rid)
    h = {"Authorization": f"Bearer {token}"}
    r = mobile.post("/mobile/api/live-activity-tokens", headers=h,
                    json={"activity_type": "pending_send", "kind": "start", "token": "d" * 64,
                          "environment": "sandbox", "activity_key": ""})
    assert r.status_code == 200 and r.get_json()["ok"]
    rows = push.live_activity_tokens("pending_send", "start", restaurant_id=rid, db_path=db)
    assert [(x["user_id"], x["environment"], x["session_hash"]) for x in rows] == [(uid, "sandbox", session_hash)]
    # A rotated push-to-start token replaces the old one.
    mobile.post("/mobile/api/live-activity-tokens", headers=h,
                json={"activity_type": "pending_send", "kind": "start", "token": "e" * 64, "environment": "sandbox"})
    assert [x["apns_token"] for x in push.live_activity_tokens("pending_send", "start", db_path=db)] == ["e" * 64]
    # Malformed requests are refused.
    for bad in ({"activity_type": "nope", "kind": "start", "token": "f" * 64},
                {"activity_type": "service", "kind": "start", "token": "not hex"},
                {"activity_type": "schedule_build", "kind": "update", "token": "f" * 64}):
        assert mobile.post("/mobile/api/live-activity-tokens", headers=h, json=bad).status_code == 400
    # Signing out ends the session; no push may follow it to the phone.
    auth.delete_session(token, db_path=db)
    assert push.live_activity_tokens("pending_send", "start", db_path=db) == []


def test_turning_tonights_service_off_takes_the_token_back(db, mobile):
    rid = _restaurant(db)
    _uid, token, _h = _login(db, rid)
    h = {"Authorization": f"Bearer {token}"}
    mobile.post("/mobile/api/live-activity-tokens", headers=h,
                json={"activity_type": "service", "kind": "start", "token": "1" * 64, "environment": "production"})
    assert len(push.live_activity_tokens("service", "start", db_path=db)) == 1
    r = mobile.delete("/mobile/api/live-activity-tokens", headers=h, json={"activity_type": "service", "kind": "start"})
    assert r.get_json() == {"ok": True, "removed": 1}
    assert push.live_activity_tokens("service", "start", db_path=db) == []
    assert mobile.delete("/mobile/api/live-activity-tokens", headers=h,
                         json={"activity_type": "nope"}).status_code == 400


# ── #61: the queued-send countdown ──────────────────────────────────────────

def _start_token(db, uid, rid, session_hash, token):
    push.register_live_activity_token(uid, rid, session_hash, "pending_send", "start", token, db_path=db)


def test_a_queued_send_starts_the_countdown_on_phones_that_may_undo_it(db, apns):
    import delayed
    rid = _restaurant(db)
    owner, _t1, h1 = _login(db, rid, "owner")
    manager, _t2, h2 = _login(db, rid, "mgr", role="manager")
    _start_token(db, owner, rid, h1, "a1" * 32)
    _start_token(db, manager, rid, h2, "b2" * 32)
    # A supplier order: food cost's — the manager's login can't undo it.
    row = delayed.schedule(rid, "order_send", {"supplier_email": "s@x.test"}, 60,
                           label="Sending the Sysco order ($1,234, 5 items)", db_path=db)
    starts = [p for p in apns if p["payload"]["aps"]["event"] == "start"]
    assert [p["url"].rsplit("/", 1)[1] for p in starts] == ["a1" * 32]
    aps = starts[0]["payload"]["aps"]
    assert starts[0]["headers"]["apns-push-type"] == "liveactivity"
    assert starts[0]["headers"]["apns-topic"] == push._bundle_id() + ".push-type.liveactivity"
    assert starts[0]["headers"]["apns-priority"] == "10"
    assert aps["attributes-type"] == "PendingSendAttributes"
    assert aps["attributes"] == {"actionId": row["id"], "kind": "order_send",
                                 "title": "Sending the Sysco order ($1,234, 5 items)"}
    assert aps["content-state"]["status"] == "pending"
    assert aps["content-state"]["fireAt"] == push.apple_date(row["execute_at"])
    assert aps["alert"]["title"] == "Supplier order goes out"
    # Started once, however many passes see it.
    assert live_activities.pending_send_queued(row, db_path=db) == 0
    # A schedule publish reaches both: the manager may publish.
    apns.clear()
    delayed.schedule(rid, "schedule_publish", {"schedule_id": 1}, 60, db_path=db)
    assert sorted(p["url"].rsplit("/", 1)[1] for p in apns) == sorted(["a1" * 32, "b2" * 32])


def test_undo_or_the_send_itself_ends_the_countdown_everywhere(db, apns):
    import delayed
    rid = _restaurant(db)
    owner, _t, h = _login(db, rid)
    row = delayed.schedule(rid, "schedule_publish", {"schedule_id": 1}, 60, db_path=db)
    push.register_live_activity_token(owner, rid, h, "pending_send", "update", "c3" * 32,
                                      activity_key=str(row["id"]), db_path=db)
    apns.clear()
    assert delayed.cancel(rid, row["id"], actor={"username": "owner"}, db_path=db)
    ends = [p["payload"]["aps"] for p in apns]
    assert [a["event"] for a in ends] == ["end"] and ends[0]["content-state"]["status"] == "stopped"
    assert "dismissal-date" in ends[0]
    # The ended activity's token is forgotten; a second end sends nothing.
    assert push.live_activity_tokens("pending_send", "update", db_path=db) == []
    assert live_activities.pending_send_finished(rid, row["id"], "stopped", db_path=db) == 0


# ── #38: building next week ─────────────────────────────────────────────────

def test_a_generations_days_reach_the_phone_and_its_end_closes_it(db, apns, monkeypatch):
    import ops
    import schedule_engine
    rid = _restaurant(db)
    owner, _t, h = _login(db, rid)
    ops.start_async_job("job-1", "schedule", rid)
    push.register_live_activity_token(owner, rid, h, "schedule_build", "update", "d4" * 32,
                                      activity_key="job-1", db_path=db)
    monkeypatch.setattr(schedule_engine, "typical_generation_seconds",
                        lambda rid, db_path=None: {"seconds": 1200, "n": 4, "basis": "yours"})
    ops.set_async_job_progress("job-1", {"days_total": 7, "days_drafted": 3, "dates_drafted": []})
    upd = apns[-1]
    assert upd["headers"]["apns-priority"] == "5"
    state = upd["payload"]["aps"]["content-state"]
    assert upd["payload"]["aps"]["event"] == "update"
    assert state["daysDrafted"] == 3 and state["daysTotal"] == 7 and state["status"] == "building"
    assert isinstance(state["estimatedEnd"], float)
    # The same progress again is no news.
    n = len(apns)
    ops.set_async_job_progress("job-1", {"days_total": 7, "days_drafted": 3, "dates_drafted": []})
    assert len(apns) == n
    ops.finish_async_job("job-1", "done", {"ok": True})
    end = apns[-1]["payload"]["aps"]
    assert end["event"] == "end" and end["content-state"]["status"] == "done"
    assert end["content-state"]["daysDrafted"] == 7
    assert push.live_activity_tokens("schedule_build", "update", db_path=db) == []


def test_without_a_measured_typical_no_estimate_is_sent(db):
    state = live_activities.schedule_build_state({"days_total": 7, "days_drafted": 0})
    assert "estimatedEnd" not in state and state["daysDrafted"] == 0


# ── #94: tonight's service ──────────────────────────────────────────────────

def test_the_pulse_line_is_a_percent_against_a_typical_weekday():
    assert live_activities.pulse_line({"available": True, "pct": 8.4, "weekday": "Friday"}) == \
        {"line": "▲ 8% vs a typical Fri", "up": True}
    assert live_activities.pulse_line({"available": True, "pct": -12.0, "weekday": "Friday"})["line"] == \
        "▼ 12% vs a typical Fri"
    assert live_activities.pulse_line({"available": True, "pct": 0.3, "weekday": "Friday"})["line"] == \
        "Even with a typical Fri"
    # Not measured is never 0%.
    assert live_activities.pulse_line({"available": False, "reason": "only 2 past Fridays measured at this hour"}) \
        == {"note": "Only 2 past Fridays measured at this hour"}


def _open_restaurant(db):
    hours = {d: "4:00pm" for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}
    closes = {d: "11:00pm" for d in hours}
    rid = _restaurant(db, name="Tonight Co")
    models.update_restaurant(rid, {"open_times_json": json.dumps(hours), "close_times_json": json.dumps(closes),
                                   "module_labor": 1})
    return rid


def test_tonights_service_starts_at_open_updates_on_change_and_ends_at_close(db, apns, monkeypatch):
    import intraday
    rid = _open_restaurant(db)
    r = models.get_restaurant(rid)
    owner, _t, h = _login(db, rid)
    push.register_live_activity_token(owner, rid, h, "service", "start", "e5" * 32, db_path=db)
    monkeypatch.setattr(intraday, "pulse", lambda *a, **k: {"available": True, "pct": 8.0, "weekday": "Friday"})
    evening = datetime(2026, 10, 9, 18, 0)
    live_activities.note_coverage(r, evening, {"available": True, "business_date": "2026-10-09",
                                               "missing": [{"employee": "Dana", "role": "Server"}]}, db_path=db)
    start = [p["payload"]["aps"] for p in apns if p["payload"]["aps"]["event"] == "start"]
    assert len(start) == 1
    assert start[0]["attributes-type"] == "ServiceAttributes"
    assert start[0]["attributes"]["businessDate"] == "2026-10-09"
    assert start[0]["attributes"]["dayLabel"] == "Fri 10/9/26"
    cs = start[0]["content-state"]
    assert cs["status"] == "open" and cs["pulseLine"] == "▲ 8% vs a typical Fri" and cs["pulseUp"] is True
    assert cs["missing"] == ["Dana"] and cs["missingCount"] == 1
    # The app files the running activity's own token; a change reaches it.
    push.register_live_activity_token(owner, rid, h, "service", "update", "f6" * 32,
                                      activity_key="2026-10-09", db_path=db)
    apns.clear()
    live_activities.service_tick(r, evening + timedelta(minutes=20), db_path=db)
    assert apns == []                                  # nothing changed
    live_activities.note_coverage(r, evening + timedelta(minutes=40),
                                  {"available": True, "business_date": "2026-10-09", "missing": []}, db_path=db)
    assert [p["payload"]["aps"]["event"] for p in apns] == ["update"]
    assert apns[0]["payload"]["aps"]["content-state"]["missing"] == []
    # Closed: it ends on the phone.
    apns.clear()
    live_activities.service_tick(r, datetime(2026, 10, 9, 23, 30), db_path=db)
    ends = [p["payload"]["aps"] for p in apns]
    assert [e["event"] for e in ends] == ["end"] and ends[0]["content-state"]["status"] == "closed"


def test_the_tonight_route_has_a_web_and_a_phone_twin(db, monkeypatch):
    import intraday
    from strategy_routes import strategy_bp, strategy_mobile_bp
    rid = _open_restaurant(db)
    owner, token, _h = _login(db, rid)
    monkeypatch.setattr(intraday, "pulse", lambda *a, **k: {"available": False, "reason": "nothing captured"})
    app = Flask(__name__)
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    c = app.test_client()
    body = c.get("/mobile/api/intraday/tonight", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert body["ok"] and set(body) >= {"in_service", "business_date", "attributes", "content_state"}
    assert body["content_state"]["pulseNote"] == "Nothing captured"
    assert "pulseLine" not in body["content_state"]
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": owner, "restaurant_id": rid, "is_admin": 0, "role": "client",
                                 "username": "owner", "email": "o@x.test"})
    web = c.get("/api/intraday/tonight").get_json()
    assert web["ok"] and web["attributes"] == body["attributes"]


def test_a_login_without_labor_cannot_read_tonight():
    assert live_activities._do_intraday_tonight({"id": 1, "restaurant_id": 1, "role": "support"})[1] == 403


# ── the Swift side reads exactly these keys ─────────────────────────────────

def test_the_pushed_json_matches_the_swift_attributes():
    pending = _swift("Shared", "PendingSendActivity.swift")
    assert _swift_fields(pending, "ContentState") >= {"fireAt", "status", "note"}
    assert {"actionId", "kind", "title"} <= _swift_fields(pending, "PendingSendAttributes")
    assert set(live_activities.pending_send_attributes({"id": 1, "kind": "order_send", "label": ""})) == \
        {"actionId", "kind", "title"}
    assert set(live_activities.pending_send_state("2026-10-09T23:00:00Z", note="x")) <= \
        _swift_fields(pending, "ContentState")

    build = _swift("Shared", "ScheduleBuildActivity.swift")
    state = live_activities.schedule_build_state({"days_total": 7, "days_drafted": 2},
                                                 datetime(2026, 10, 9, tzinfo=timezone.utc), "failed", "x")
    assert set(state) <= _swift_fields(build, "ContentState")
    assert {"daysDrafted", "daysTotal", "status"} <= set(state)

    service = _swift("Shared", "ServiceActivity.swift")
    assert {"status", "pulseLine", "pulseUp", "pulseNote", "missing", "missingCount", "coverageNote",
            "closesAt", "updatedAt"} <= _swift_fields(service, "ContentState")
    assert {"restaurantId", "restaurantName", "businessDate", "dayLabel"} <= \
        _swift_fields(service, "ServiceAttributes")
    assert set(push.LIVE_ACTIVITY_ATTRIBUTES.values()) == {"PendingSendAttributes", "ScheduleBuildAttributes",
                                                           "ServiceAttributes"}


def test_the_phone_files_every_key_the_token_route_reads():
    src = _swift("CavnarAI", "Core", "LiveActivitySync.swift")
    for key in ('"activity_type"', '"activity_key"', "case kind, token, environment"):
        assert key in src
