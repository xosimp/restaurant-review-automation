"""Blind re-audit of the push / native iOS work (10/8/26), the server half:
Live Activity tokens follow a location switch (#4) and are filed at the
restaurant the activity is about, by a login allowed to see it (#10); a
generation's pushes are scoped to its job's restaurant (#10); an
app-started "Tonight's service" hears its updates and its end (#9) and
only Labor's readers hear names (#10); a push-to-start asks for the
activity's own token (#15); a payload is sent as UTF-8 and trimmed to fit
whatever field is long (#13); reading the queue is not a write for a peek
or a view-as read (#11 / Home #4); a countdown carries its restaurant for
the tap's `?loc=` (#6)."""
import json
from datetime import datetime, timedelta

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


@pytest.fixture
def db(db_path, monkeypatch):
    init_auth(db_path=db_path)
    push.init_push(db_path)
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, push, mobile_api, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(live_activities, "DB_PATH", db_path)
    return db_path


@pytest.fixture
def apns(monkeypatch):
    posts = []

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    class _Client:
        def post(self, url, content=None, headers=None):
            posts.append({"url": url, "raw": content, "payload": json.loads(content),
                          "headers": dict(headers or {})})
            return _Resp()

    monkeypatch.setattr(push, "_client", lambda: _Client())
    monkeypatch.setattr(push, "_provider_jwt", lambda: "jwt")
    monkeypatch.setattr(push, "native_push_allowed", lambda: True)
    monkeypatch.setattr(push, "_submit_native", lambda fn, *args: (fn(*args), True)[1])
    return posts


@pytest.fixture
def mobile(db):
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _restaurant(db, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Live Co"), owner_email=kw.pop("owner_email", "o@x.test"),
                                        **kw), db_path=db)


def _login(db, rid, username="owner", role=None):
    uid = create_user(rid, username, f"{username}@x.test", "correct-horse-battery", db_path=db)
    if role:
        set_user_role(uid, role, db_path=db)
    token = create_session(uid, device_type="ios", db_path=db)
    return uid, token, hash_session_token(token)


def _file(mobile, bearer, **body):
    body.setdefault("environment", "production")
    return mobile.post("/mobile/api/live-activity-tokens", headers={"Authorization": f"Bearer {bearer}"}, json=body)


# ── #10: an update token is filed where its activity is, by a login allowed it ──

def test_an_update_token_is_filed_at_its_own_activitys_restaurant_and_only_there(db, mobile):
    import delayed
    import ops
    here = _restaurant(db, name="Here")
    other = _restaurant(db, name="Elsewhere", owner_email="else@x.test")
    _owner, token, _h = _login(db, here)
    mine = delayed.schedule(here, "schedule_publish", {"schedule_id": 1}, 60, db_path=db)
    theirs = delayed.schedule(other, "schedule_publish", {"schedule_id": 1}, 60, db_path=db)
    assert _file(mobile, token, activity_type="pending_send", kind="update", token="a1" * 32,
                 activity_key=str(mine["id"])).status_code == 200
    assert [t["restaurant_id"] for t in push.live_activity_tokens("pending_send", "update", db_path=db)] == [here]
    # Another tenant's send: refused, never filed.
    r = _file(mobile, token, activity_type="pending_send", kind="update", token="b2" * 32,
              activity_key=str(theirs["id"]))
    assert r.status_code == 403
    # Nothing by that id at all.
    assert _file(mobile, token, activity_type="pending_send", kind="update", token="c3" * 32,
                 activity_key="999999").status_code == 404
    # Another tenant's generation: refused.
    ops.start_async_job("job-other", "schedule", other)
    assert _file(mobile, token, activity_type="schedule_build", kind="update", token="d4" * 32,
                 activity_key="job-other").status_code == 403
    assert len(push.live_activity_tokens("pending_send", "update", db_path=db)) == 1
    assert push.live_activity_tokens("schedule_build", "update", db_path=db) == []


def test_a_login_without_the_module_cannot_follow_its_activity(db, mobile):
    import ops
    rid = _restaurant(db)
    _uid, token, _h = _login(db, rid, "viewer", role="support")
    ops.start_async_job("job-1", "schedule", rid)
    assert _file(mobile, token, activity_type="schedule_build", kind="update", token="e5" * 32,
                 activity_key="job-1").status_code == 403
    assert _file(mobile, token, activity_type="service", kind="update", token="f6" * 32,
                 activity_key="2026-10-09").status_code == 403
    assert push.live_activity_tokens("service", "update", db_path=db) == []


def test_a_service_token_is_filed_at_the_restaurant_its_activity_names(db, mobile):
    a = _restaurant(db, name="North", location_group="Grp")
    b = _restaurant(db, name="South", location_group="Grp")
    stranger = _restaurant(db, name="Stranger", owner_email="s@x.test")
    _uid, token, _h = _login(db, a, role="owner")
    assert _file(mobile, token, activity_type="service", kind="update", token="1a" * 32,
                 activity_key="2026-10-09", restaurant_id=b).status_code == 200
    assert [t["restaurant_id"] for t in push.live_activity_tokens("service", "update", db_path=db)] == [b]
    assert _file(mobile, token, activity_type="service", kind="update", token="2b" * 32,
                 activity_key="2026-10-09", restaurant_id=stranger).status_code == 403


# ── #4: a switch re-files the push-to-start tokens at the new location ──────

def test_switching_location_drops_the_sessions_start_tokens_and_keeps_running_ones(db, mobile):
    a = _restaurant(db, name="North", location_group="Grp")
    b = _restaurant(db, name="South", location_group="Grp")
    _uid, token, session_hash = _login(db, a, role="owner")
    assert _file(mobile, token, activity_type="pending_send", kind="start", token="3c" * 32).status_code == 200
    push.register_live_activity_token(_uid, a, session_hash, "service", "update", "4d" * 32,
                                      activity_key="2026-10-09", db_path=db)
    r = mobile.post("/mobile/api/switch-location", headers={"Authorization": f"Bearer {token}"},
                    json={"restaurant_id": b})
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    assert push.live_activity_tokens("pending_send", "start", db_path=db) == []
    # The running night is still North's and still reachable.
    assert [t["restaurant_id"] for t in push.live_activity_tokens("service", "update", db_path=db)] == [a]
    # The phone files its start token again: it lands at South.
    assert _file(mobile, token, activity_type="pending_send", kind="start", token="3c" * 32).status_code == 200
    assert [t["restaurant_id"] for t in push.live_activity_tokens("pending_send", "start", db_path=db)] == [b]


# ── #10: a generation's pushes reach only its own restaurant's tokens ───────

def test_a_generations_progress_reaches_only_tokens_at_its_restaurant(db, apns):
    import ops
    rid = _restaurant(db)
    other = _restaurant(db, name="Other", owner_email="x@x.test")
    owner, _t, h = _login(db, rid)
    stranger, _t2, h2 = _login(db, other, "stranger")
    ops.start_async_job("job-9", "schedule", rid)
    push.register_live_activity_token(owner, rid, h, "schedule_build", "update", "5e" * 32,
                                      activity_key="job-9", db_path=db)
    # A token filed under the same job id at another restaurant.
    push.register_live_activity_token(stranger, other, h2, "schedule_build", "update", "6f" * 32,
                                      activity_key="job-9", db_path=db)
    ops.set_async_job_progress("job-9", {"days_total": 7, "days_drafted": 2, "dates_drafted": []})
    assert [p["url"].rsplit("/", 1)[1] for p in apns] == ["5e" * 32]
    apns.clear()
    ops.finish_async_job("job-9", "done", {"ok": True})
    assert [p["url"].rsplit("/", 1)[1] for p in apns] == ["5e" * 32]
    # A job that doesn't exist pushes nothing.
    assert live_activities.job_progress("no-such-job", {"days_total": 7, "days_drafted": 1}, db_path=db) == 0


# ── #9 / #10: tonight's service, started by the app ─────────────────────────

def _open_restaurant(db):
    hours = {d: "4:00pm" for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")}
    closes = {d: "11:00pm" for d in hours}
    rid = _restaurant(db, name="Tonight Co")
    models.update_restaurant(rid, {"open_times_json": json.dumps(hours), "close_times_json": json.dumps(closes),
                                   "module_labor": 1})
    return rid


def test_an_app_started_night_hears_its_updates_and_its_end(db, apns, monkeypatch):
    """iOS 17.0/17.1: no push-to-start token, only the running activity's
    own. The server never claimed a start, and used to say nothing."""
    import intraday
    rid = _open_restaurant(db)
    r = models.get_restaurant(rid)
    owner, _t, h = _login(db, rid)
    monkeypatch.setattr(intraday, "pulse", lambda *a, **k: {"available": True, "pct": 8.0, "weekday": "Friday"})
    push.register_live_activity_token(owner, rid, h, "service", "update", "7a" * 32,
                                      activity_key="2026-10-09", db_path=db)
    evening = datetime(2026, 10, 9, 18, 0)
    live_activities.note_coverage(r, evening, {"available": True, "business_date": "2026-10-09",
                                               "missing": [{"employee": "Dana", "role": "Server"}]}, db_path=db)
    assert [p["payload"]["aps"]["event"] for p in apns] == ["update"]
    assert apns[0]["payload"]["aps"]["content-state"]["missing"] == ["Dana"]
    apns.clear()
    live_activities.service_tick(r, datetime(2026, 10, 9, 23, 30), db_path=db)
    assert [p["payload"]["aps"]["event"] for p in apns] == ["end"]
    assert push.live_activity_tokens("service", "update", db_path=db) == []


def test_a_login_that_lost_labor_gets_an_end_with_no_names(db, apns, monkeypatch):
    import intraday
    rid = _open_restaurant(db)
    r = models.get_restaurant(rid)
    owner, _t, h = _login(db, rid)
    viewer, _t2, h2 = _login(db, rid, "viewer", role="support")
    monkeypatch.setattr(intraday, "pulse", lambda *a, **k: {"available": True, "pct": 8.0, "weekday": "Friday"})
    push.register_live_activity_token(owner, rid, h, "service", "update", "8b" * 32,
                                      activity_key="2026-10-09", db_path=db)
    # Filed before the login lost Labor (straight into the table).
    push.register_live_activity_token(viewer, rid, h2, "service", "update", "9c" * 32,
                                      activity_key="2026-10-09", db_path=db)
    live_activities.note_coverage(r, datetime(2026, 10, 9, 18, 0),
                                  {"available": True, "business_date": "2026-10-09",
                                   "missing": [{"employee": "Dana", "role": "Server"}]}, db_path=db)
    by_token = {p["url"].rsplit("/", 1)[1]: p["payload"]["aps"] for p in apns}
    assert by_token["8b" * 32]["event"] == "update" and by_token["8b" * 32]["content-state"]["missing"] == ["Dana"]
    assert by_token["9c" * 32]["event"] == "end" and by_token["9c" * 32]["content-state"]["missing"] == []
    assert "Dana" not in json.dumps(by_token["9c" * 32])
    assert [t["apns_token"] for t in push.live_activity_tokens("service", "update", db_path=db)] == ["8b" * 32]


# ── #15: a push-to-start asks for the started activity's token ──────────────

def test_a_push_to_start_asks_for_the_activitys_own_token():
    start = live_activities.push.live_activity_payload("service", "start", {"status": "open"},
                                                       attributes={"restaurantId": 1}, now=0)["aps"]
    assert start["input-push-token"] == 1 and start["event"] == "start"
    update = push.live_activity_payload("service", "update", {"status": "open"}, now=0)["aps"]
    assert "input-push-token" not in update


# ── #13: the payload is UTF-8 and any long prose is trimmed ─────────────────

def test_a_payload_is_sent_as_utf8_not_escaped():
    out = push._fit_payload({"aps": {"alert": {"title": "Café", "body": "Señor José"}}, "cavnar": {}})
    assert "Café".encode("utf-8") in out and b"\\u00e9" not in out


def test_a_platform_page_too_big_for_apns_is_trimmed_keeping_its_address():
    import ops
    data = ops.platform_alert_payload("Scheduler stopped " + "é" * 150, ["Ünïcode line " + "ß" * 190] * 6)
    payload = {"aps": {"alert": {"title": "Scheduler stopped", "body": "ø" * 900}, "category": "X"},
               "cavnar": {"alert_type": "platform_alert", "priority": 1, "restaurant_id": 3, **data}}
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > push.APNS_MAX_PAYLOAD_BYTES
    out = push._fit_payload(payload)
    assert len(out) <= push.APNS_MAX_PAYLOAD_BYTES
    sent = json.loads(out)
    assert sent["cavnar"]["nav"] == "admin/platform" and sent["cavnar"]["alert_at"] == data["alert_at"]
    assert sent["cavnar"]["restaurant_id"] == 3 and sent["cavnar"]["alert_type"] == "platform_alert"
    assert len(sent["cavnar"]["lines"]) == 6
    assert any(x.endswith("…") for x in sent["cavnar"]["lines"] + [sent["aps"]["alert"]["body"]])


# ── #11 / Home #4: reading the queue is not a write ─────────────────────────

def test_a_peek_or_a_view_as_read_of_the_queue_writes_nothing(monkeypatch):
    import action_queue
    import strategy_routes
    noted = []
    monkeypatch.setattr(action_queue, "items", lambda *a, **k: {"items": [{"key": "x"}]})
    monkeypatch.setattr(live_activities, "note_waiting_count", lambda *a, **k: noted.append(a) or 0)
    monkeypatch.setattr(strategy_routes, "_local_today", lambda u: None)
    app = Flask(__name__)
    owner = {"id": 5, "restaurant_id": 2, "role": "client"}
    with app.test_request_context("/api/actions?peek=1"):
        assert strategy_routes._do_actions(owner)[1] == 200
    with app.test_request_context("/api/actions"):
        strategy_routes._do_actions(dict(owner, acting_admin="will", acting_admin_id=1, acting_admin_role="admin"))
    assert noted == []
    with app.test_request_context("/api/actions"):
        strategy_routes._do_actions(owner)
    assert len(noted) == 1


# ── #6: a countdown names its restaurant ────────────────────────────────────

def test_a_countdowns_attributes_carry_its_restaurant():
    attrs = live_activities.pending_send_attributes({"id": 4, "kind": "order_send", "label": "", "restaurant_id": 9})
    assert attrs == {"actionId": 4, "kind": "order_send", "title": "Supplier order", "restaurantId": 9}


# ── #14: a platform page's address has a destination on the web ─────────────

def test_the_web_router_takes_the_admin_head_for_admins_only():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8").read()
    nav = html[html.index('<script id="cav-nav">'):]
    nav = nav[:nav.index("</script>")]
    assert "cavNavRegister('admin'" in nav and "/admin#operations" in nav
    assert "if (!window.CAV_IS_ADMIN) { cavNavOpenModule('home'); return true; }" in nav
    # The flag is Jinja, so it lives outside the router's own (node-run) block.
    assert "{{" not in nav
    assert "<script>window.CAV_IS_ADMIN={{ 'true' if current_user and current_user.is_admin else 'false' }};</script>" \
        in html


def test_the_phone_sends_the_service_activitys_restaurant():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "ios", "CavnarAI", "CavnarAI", "Core", "LiveActivitySync.swift"),
               encoding="utf-8").read()
    assert 'case restaurantId = "restaurant_id"' in src
    assert "restaurantId: a.attributes.restaurantId" in src
