"""Edge cases of the web Home brief (home_brief.py) that the MOD audit found
untested: what a manager's Home leaks from modules the role may not read,
restaurants configured oddly (every module off, a bad timezone), the
quiet-hours indicator's clock, the cost of a cold build on a year of shifts,
and the payload cache's growth and invalidation.

Tests that pin a confirmed defect are strict xfails naming the finding, so
they flip to failures the day the defect is fixed."""
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import auth
import client_api
import home_brief
import mobile_api
import models
from auth import init_auth
from models import Restaurant, create_restaurant, get_conn, save_client_data, update_restaurant


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, home_brief):
        monkeypatch.setattr(mod, "get_conn", redirect)
    # Any other repo module still holding a get_conn — including a stale
    # redirect an earlier test's lazy import bound (CLAUDE.md "Bound imports").
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if f.startswith(_REPO) and callable(getattr(mod, "get_conn", None)):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    from models import init_email_log
    init_email_log(db_path=db_path)
    import ai_utils
    c = get_conn(db_path); c.executescript(ai_utils._USAGE_TABLE_SQL); c.commit(); c.close()
    home_brief.invalidate()
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI called from Home")))


def _user(rid, uid=1, role="client"):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner",
            "role": role, "is_admin": 0, "email": "o@x.com"}


def _seed(db_path, name="Corner Bar", **kw):
    fields = dict(name=name, owner_email="o@x.com", owner_name="Sam Owner", module_reviews=1,
                  module_labor=1, module_inventory=1, module_marketing=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db_path)


def _alert(db_path, rid, alert_type, value=1.0):
    c = get_conn(db_path)
    c.execute("INSERT INTO alert_log (restaurant_id, alert_type, value) VALUES (?, ?, ?)",
              (rid, alert_type, value))
    c.commit(); c.close()


# ── A9 #13 / MOD-HOME-1: a manager's Home must not carry Food Cost alerts ────

def test_a_manager_home_lists_no_food_cost_alert(db_path):
    rid = _seed(db_path)
    _alert(db_path, rid, "food_waste", 420.0)
    _alert(db_path, rid, "labor_over", 38.0)
    mgr, st = home_brief.build_home_brief(_user(rid, uid=2, role="manager"), fresh=True)
    assert st == 200
    assert [a["module"] for a in mgr["alerts"]] == ["labor"]


def test_the_managers_notification_list_already_scopes_food_cost_out(db_path):
    """The reference Home should match: the notification list drops the
    inventory alert for the same manager (this half works today)."""
    rid = _seed(db_path)
    _alert(db_path, rid, "food_waste", 420.0)
    _alert(db_path, rid, "labor_over", 38.0)
    payload, _ = client_api._do_get_notifications(rid, viewer=_user(rid, uid=2, role="manager"))
    assert [n["type"] for n in payload["notifications"]] == ["labor_over"]


def test_a_managers_alerts_fired_change_counts_only_alerts_the_role_can_see(db_path):
    rid = _seed(db_path)
    _alert(db_path, rid, "food_waste", 420.0)
    _alert(db_path, rid, "labor_over", 38.0)
    mgr, _ = home_brief.build_home_brief(_user(rid, uid=2, role="manager"), fresh=True)
    texts = [x.get("text") for x in mgr["changes"]["items"] if x.get("module") == "alerts"]
    assert texts == ["1 alert fired"]


def test_an_owner_home_lists_every_alert(db_path):
    rid = _seed(db_path)
    _alert(db_path, rid, "food_waste", 420.0)
    _alert(db_path, rid, "labor_over", 38.0)
    owner, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert sorted(a["module"] for a in owner["alerts"]) == ["inventory", "labor"]


# ── A9 #14, #15: odd configurations still produce a Home ─────────────────────

def test_a_restaurant_with_every_module_off_still_gets_a_home(db_path):
    rid = _seed(db_path, module_reviews=0, module_labor=0, module_inventory=0, module_marketing=0)
    p, st = home_brief.build_home_brief(_user(rid), fresh=True)
    assert st == 200 and p["ok"]
    live = [s for s in p["snapshot"] if s.get("status") != "coming_soon"]
    assert live == []
    assert p["attention"] == [] and p["recommendations"] == []


def test_an_invalid_timezone_string_does_not_break_home(db_path):
    rid = _seed(db_path)
    update_restaurant(rid, {"timezone": "Mars/Olympus"}, db_path=db_path)
    p, st = home_brief.build_home_brief(_user(rid), fresh=True)
    assert st == 200 and p["ok"]


# ── A9 #16: quiet-hours indicator is the restaurant's clock ──────────────────

def _hhmm(dt):
    return f"{dt.hour:02d}:{dt.minute:02d}"


def test_home_quiet_hours_indicator_uses_the_restaurants_own_timezone(db_path):
    """A one-hour window around the current Los Angeles time: quiet in LA,
    two hours clear of it in Chicago, whatever the wall clock says."""
    la_now = datetime.now(ZoneInfo("America/Los_Angeles"))
    rid = _seed(db_path)
    update_restaurant(rid, {"timezone": "America/Los_Angeles",
                            "alert_quiet_start": _hhmm(la_now - timedelta(minutes=30)),
                            "alert_quiet_end": _hhmm(la_now + timedelta(minutes=30))}, db_path=db_path)
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert p["quiet_hours_active"] is True


# ── A9 #17 / MOD-HOME-2: a year of shifts ────────────────────────────────────

def _year_of_shifts(rid, db_path, staff=120):
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    d0 = date.today() - timedelta(days=365)
    for i in range(365):
        d = d0 + timedelta(days=i)
        for e in range(staff):
            rows.append(f"{d.isoformat()},{d.strftime('%A')},Emp {e},Server,11:00,17:00,6,6.1,9000,")
    save_client_data(rid, "shifts", "\n".join(rows), db_path=db_path)
    return len(rows) - 1


def test_a_cold_home_build_on_a_year_of_shifts_stays_inside_a_generous_budget(db_path):
    rid = _seed(db_path)
    assert _year_of_shifts(rid, db_path) == 43800
    t = time.time()
    p, st = home_brief.build_home_brief(_user(rid), fresh=True)
    assert st == 200 and p["ok"]
    assert time.time() - t < 10.0


def test_a_cold_home_build_reads_the_shifts_blob_once(db_path, monkeypatch):
    rid = _seed(db_path)
    _year_of_shifts(rid, db_path, staff=5)
    calls = []
    real = models.get_client_data

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)
    monkeypatch.setattr(models, "get_client_data", counting)
    import labor
    if hasattr(labor, "get_client_data"):
        monkeypatch.setattr(labor, "get_client_data", counting)
    home_brief.build_home_brief(_user(rid), fresh=True)
    assert len(calls) == 1


# ── A9 #18, #19 / MOD-HOME-3: the payload cache ──────────────────────────────

def test_expired_home_cache_entries_are_evicted(db_path):
    rid = _seed(db_path)
    for uid in range(10, 30):
        home_brief.build_home_brief(_user(rid, uid=uid), fresh=True)
    assert len(home_brief._CACHE) == 20
    # Age every entry past the TTL, then one more build writes to the cache.
    old = datetime.now(ZoneInfo("UTC")) - timedelta(seconds=home_brief._CACHE_TTL * 10)
    for k, (_, payload) in list(home_brief._CACHE.items()):
        home_brief._CACHE[k] = (old, payload)
    home_brief.build_home_brief(_user(rid, uid=99), fresh=True)
    assert len(home_brief._CACHE) == 1


def test_an_expired_entry_is_not_served(db_path):
    rid = _seed(db_path)
    first, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    key = (rid, 1)
    old = datetime.now(ZoneInfo("UTC")) - timedelta(seconds=home_brief._CACHE_TTL + 5)
    sentinel = dict(first, sentinel=True)
    home_brief._CACHE[key] = (old, sentinel)
    again, _ = home_brief.build_home_brief(_user(rid))
    assert "sentinel" not in again


def test_a_location_switch_leaves_other_tenants_cached_homes_alone(db_path, monkeypatch):
    a = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Downtown")
    b = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Uptown")
    other = _seed(db_path, "Elsewhere", owner_email="z@x.com")
    home_brief.build_home_brief(_user(other, uid=50), fresh=True)
    assert (other, 50) in home_brief._CACHE
    monkeypatch.setattr(auth, "switch_active_restaurant", lambda token, rid: None)
    payload, st = client_api._do_switch_location(_user(a, role="owner"), b, "tok")
    assert st == 200, payload
    assert (other, 50) in home_brief._CACHE


def test_a_location_switch_drops_the_owners_own_cached_home(db_path, monkeypatch):
    a = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Downtown")
    b = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Uptown")
    home_brief.build_home_brief(_user(a, role="owner"), fresh=True)
    monkeypatch.setattr(auth, "switch_active_restaurant", lambda token, rid: None)
    _, st = client_api._do_switch_location(_user(a, role="owner"), b, "tok")
    assert st == 200
    assert (a, 1) not in home_brief._CACHE


# ── A9 #20: group brief refused for a non-owner role ─────────────────────────

def test_the_group_brief_is_refused_for_a_manager(db_path):
    a = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Downtown")
    _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Uptown")
    payload, st = home_brief.build_group_brief(_user(a, uid=7, role="manager"), fresh=True)
    assert st == 403 and payload["ok"] is False
