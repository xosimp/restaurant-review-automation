"""Memory audit 9/29/26 (workstream M2, owner_layers): a preference resolves
login override → location setting → organisation default → product default.

  * a group owner applies a location's settings to every location at once
    (never-say "cheap" everywhere), and a location that joins the group
    takes the organisation's defaults on what it has not set itself;
  * each login's own notification choices (push off, a muted type, their
    own quiet hours) apply to their own phone only, and never take away
    health, safety or an assigned issue;
  * the group owner can turn a sibling location's brief off;
  * engagement is counted per login.
"""
import sqlite3
from datetime import datetime

import pytest
from flask import Flask

import auth
import models
import preferences
import push
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    import sys
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    yield


def _group():
    ids = []
    for name in ("Downtown", "North", "South"):
        rid = create_restaurant(Restaurant(name=f"EJ's {name}", owner_email="erik@x.test"))
        update_restaurant(rid, {"location_group": "Simple EJ's", "location_name": name})
        ids.append(rid)
    return ids


def _user(rid, username, role):
    uid = auth.create_user(rid, username, f"{username}@x.test", "pw")
    conn = models.get_conn()
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    conn.commit()
    conn.close()
    return uid


# ── the organisation layer ──────────────────────────────────────────────────

def test_a_group_owner_applies_never_say_to_every_location_and_each_says_where_it_came_from():
    a, b, c = _group()
    update_restaurant(a, {"never_say": "cheap, deal", "voice_notes": "Warm, short"})
    owner = {"id": 1, "role": "owner", "is_admin": 0}
    assert preferences.may_apply_to_all(owner, get_restaurant(a))
    out = preferences.apply_to_all_locations(get_restaurant(a), ["never_say", "voice_notes", "auto_approve_5star"],
                                             user=owner)
    assert out["keys"] == ["never_say", "voice_notes"] and out["skipped"] == ["auto_approve_5star"]
    assert {loc["id"] for loc in out["locations"]} == {a, b, c}
    assert get_restaurant(b).never_say == "cheap, deal" and get_restaurant(c).voice_notes == "Warm, short"
    assert preferences.resolve("never_say", get_restaurant(c))["source"] == "all locations"
    update_restaurant(c, {"never_say": "cheap"})
    assert preferences.resolve("never_say", get_restaurant(c))["source"] == "this location"
    manager = {"id": 2, "role": "manager", "is_admin": 0}
    assert not preferences.may_apply_to_all(manager, get_restaurant(a))


def test_a_location_joining_the_group_takes_the_defaults_it_has_not_set_itself():
    a, b, _c = _group()
    update_restaurant(a, {"never_say": "cheap", "briefing_level": "calm"})
    preferences.apply_to_all_locations(get_restaurant(a), ["never_say", "briefing_level"],
                                       user={"id": 1, "role": "owner"})
    d = create_restaurant(Restaurant(name="EJ's West", owner_email="erik@x.test"))
    update_restaurant(d, {"never_say": "fancy"})
    update_restaurant(d, {"location_group": "Simple EJ's"})
    r = get_restaurant(d)
    assert r.never_say == "fancy", "its own setting is its override, kept"
    assert r.briefing_level == "calm", "an unset one takes the group's"


# ── the login layer ─────────────────────────────────────────────────────────

class _Inline:
    def submit(self, fn, *a):
        fn(*a)


def _stub_apns(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "_push_executor", lambda: _Inline())

    def _deliver(token_row, alert_type, title, body, data, db_path=None):
        sent.append((token_row["user_id"], alert_type))
        return {"ok": True, "status": 200, "attempts": 1, "error": None}
    monkeypatch.setattr(push, "_deliver", _deliver)
    return sent


def _device(rid, uid, tok):
    conn = models.get_conn()
    conn.execute("INSERT INTO device_tokens (restaurant_id, user_id, apns_token) VALUES (?,?,?)", (rid, uid, tok * 20))
    conn.commit()
    conn.close()


def test_a_login_mutes_a_type_on_their_own_phone_only_and_never_health(monkeypatch):
    rid = create_restaurant(Restaurant(name="Solo", owner_email="solo@x.test"))
    owner, gm = _user(rid, "erik", "client"), _user(rid, "dana", "manager")
    _device(rid, owner, "a")
    _device(rid, gm, "b")
    sent = _stub_apns(monkeypatch)
    preferences.set_login_overrides(owner, rid, {"push_muted_types": ["1star", "health"]})
    assert preferences.login_overrides(owner, rid)["push_muted_types"] == ["1star"], "health cannot be muted"
    push.fire_push(rid, "1star", "t", "b")
    push.fire_push(rid, "health", "t", "b")
    assert (gm, "1star") in sent and (owner, "1star") not in sent
    assert (owner, "health") in sent and (gm, "health") in sent


def test_a_logins_own_quiet_hours_hold_their_phone_only(monkeypatch):
    rid = create_restaurant(Restaurant(name="Solo", owner_email="solo@x.test", timezone="America/Chicago"))
    owner, gm = _user(rid, "erik", "client"), _user(rid, "dana", "manager")
    _device(rid, owner, "a")
    _device(rid, gm, "b")
    sent = _stub_apns(monkeypatch)
    preferences.set_login_overrides(owner, rid, {"quiet_start": "22:00", "quiet_end": "07:00"})
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, naive=False: datetime(2026, 9, 29, 23, 0))
    push.fire_push(rid, "1star", "t", "b")
    assert sent == [(gm, "1star")], "the closing GM still gets their 11pm push"
    with pytest.raises(ValueError):
        preferences.set_login_overrides(owner, rid, {"quiet_start": "25:99"})


def test_the_group_owner_can_turn_a_sibling_locations_brief_off(monkeypatch):
    a, b, _c = _group()
    owner = _user(a, "erik", "owner")
    auth.set_morning_brief_pref(b, owner, False)
    import morning_brief
    # morning_brief binds get_conn and DB_PATH at import, so whichever test imported it
    # first fixed its database; point it at this test's (tests/test_fix_d_sweeps.py does too).
    monkeypatch.setattr(morning_brief, "get_conn", models.get_conn)
    assert owner not in {u["id"] for u in morning_brief.recipients(b, db_path=models.DB_PATH)}
    stranger_rid = create_restaurant(Restaurant(name="Other", owner_email="other@x.test"))
    stranger = _user(stranger_rid, "sam", "owner")
    with pytest.raises(auth.TeamAccessError):
        auth.set_morning_brief_pref(b, stranger, False)


def test_engagement_is_counted_per_login():
    rid = create_restaurant(Restaurant(name="Solo", owner_email="solo@x.test"))
    owner, gm = _user(rid, "erik", "client"), _user(rid, "dana", "manager")
    _device(rid, owner, "a")
    _device(rid, gm, "b")
    conn = models.get_conn()
    toks = {r["user_id"]: r["id"] for r in conn.execute("SELECT id, user_id FROM device_tokens").fetchall()}
    for i in range(12):
        for uid in (owner, gm):
            conn.execute("INSERT INTO push_deliveries (device_token_id, restaurant_id, alert_type, status, ok, attempts, "
                         "created_at) VALUES (?,?,?,?,?,?, datetime('now', ?))",
                         (toks[uid], rid, "5star", 200, 1, 1, f"-{i} hours"))
        conn.execute("INSERT INTO notification_opens (restaurant_id, user_id, alert_type) VALUES (?,?,?)",
                     (rid, gm, "5star"))
    conn.commit()
    conn.close()
    assert [r["alert_type"] for r in preferences.never_opened_for_login(owner, rid)] == ["5star"]
    assert preferences.never_opened_for_login(gm, rid) == []


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(db_path):
    import strategy_routes
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    return app.test_client()


def test_the_preferences_routes_read_and_save_a_logins_own_and_apply_for_the_owner(client, db_path):
    a, b, _c = _group()
    owner = _user(a, "erik", "owner")
    gm = _user(a, "dana", "manager")
    h_owner = {"Authorization": f"Bearer {auth.create_session(owner)}"}
    h_gm = {"Authorization": f"Bearer {auth.create_session(gm)}"}
    body = client.get("/mobile/api/account/preferences", headers=h_owner).get_json()
    assert body["can_apply_to_all"] is True and len(body["locations"]) == 3
    assert body["mine"]["push_enabled"] is True and "health" in body["unmutable_types"]
    r = client.post("/mobile/api/account/preferences/mine", headers=h_gm,
                    json={"push_muted_types": ["5star"], "quiet_start": "23:30", "quiet_end": "06:00"}).get_json()
    assert r["mine"]["push_muted_types"] == ["5star"] and r["mine"]["quiet_start"] == "23:30"
    assert client.post("/mobile/api/account/preferences/apply-to-all", headers=h_gm,
                       json={"keys": ["never_say"]}).status_code == 403
    update_restaurant(a, {"never_say": "cheap"})
    ok = client.post("/mobile/api/account/preferences/apply-to-all", headers=h_owner,
                     json={"keys": ["never_say"]}).get_json()
    assert ok["ok"] and get_restaurant(b).never_say == "cheap"
