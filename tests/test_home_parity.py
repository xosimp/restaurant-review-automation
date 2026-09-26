import os
"""Web / iOS parity round (9/25/26): Home, the brief, notifications and
locations. Each test names the audit finding it pins.

#1  the phone's Home payload carries every top-level key web Home reads from
    /api/home/brief — a guard, so a key added to the brief can't drift again.
#2  every approve path drops the cached Home, the mobile approve-all too.
#7  the phone reads notifications the web's way: a row is read when opened
    (`mark=0`), `scope=group` covers every location, and the counts agree.
#8  the group brief's mobile twin, scoped to the owner's own locations only.
#9  the DSR list row carries what the phone's Home card needs in one call.
No model, network, email, SMS or push is reached.
"""
import sys
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import auth
import client_api
import home_brief
import mobile_api
import models
import pos
from auth import create_user, init_auth, set_user_role
from models import Restaurant, create_restaurant, get_conn, update_restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    init_auth(db_path=db_path)
    from models import init_two_fa_backup_codes
    init_two_fa_backup_codes(db_path=db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    import gmb
    monkeypatch.setattr(gmb, "is_connected", lambda rid: False)
    from auth_routes import _login_attempts
    _login_attempts.clear()
    home_brief.invalidate()
    yield db_path
    home_brief.invalidate()


@pytest.fixture
def client(db):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _rest(db, name="Parity Co", owner_email="p@x.com", **kw):
    kw.setdefault("module_reviews", 1)
    return create_restaurant(Restaurant(name=name, owner_email=owner_email, timezone="America/Chicago", **kw),
                             db_path=db)


def _login(client, db, rid, username="owner1", role=None):
    uid = create_user(rid, username, f"{username}@x.com", "parity-pass-1", db_path=db)
    if role:
        set_user_role(uid, role, db_path=db)
    r = client.post("/mobile/api/login", json={"username": username, "password": "parity-pass-1"})
    return uid, {"Authorization": "Bearer " + r.get_json()["token"]}


_n = [0]


def _draft(db, rid, days_ago=1):
    _n[0] += 1
    c = get_conn(db)
    rv = c.execute(
        "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
        "response_status, draft_response, urgency, draft_needs_review, review_date, fetched_at) "
        "VALUES (?, 'yelp', ?, 'Ann', 4, 'Nice night', 1, 'drafted', 'Thanks so much!', 'normal', 0, ?, ?)",
        (rid, f"p{_n[0]}", (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S"),
         datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))).lastrowid
    c.commit(); c.close()
    return rv


# ── #1 the phone's Home carries what web Home reads ─────────────────────────

def test_mobile_home_carries_every_key_the_web_brief_has(client, db):
    rid = _rest(db)
    uid, headers = _login(client, db, rid)
    user = {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "role": "client",
            "username": "owner1", "email": "owner1@x.com"}
    web, status = home_brief.build_home_brief(user, fresh=True)
    assert status == 200 and web["ok"]
    home_brief.invalidate()
    phone = client.get("/mobile/api/home", headers=headers).get_json()
    assert phone["ok"] is True
    missing = [k for k in web if k != "ok" and mobile_api.MOBILE_HOME_ALIASES.get(k, k) not in phone]
    assert not missing, f"web Home reads these and the phone payload lacks them: {missing}"
    # The drift the audit found, named: none of these reached the phone.
    for key in ("quick_actions", "changes", "charts", "dismissed", "snapshot", "upcoming", "alerts",
                "ask_suggestions", "ai_insight", "context", "wins"):
        assert key in phone, key
    assert "locations" in phone["context"]
    # Older builds' contract stays: the flat attention list and the brief's
    # headline and tone (now inside the whole brief object).
    assert isinstance(phone["needs_attention"], list)
    assert phone["brief"]["headline"] == web["brief"]["headline"]
    assert phone["brief"]["tone"] == web["brief"]["tone"]


def test_the_passthrough_never_overrides_the_phones_own_shapes():
    rest = mobile_api._brief_passthrough({"ok": True, "attention": [1], "receipts": [2], "charts": {"a": 1}})
    assert rest == {"charts": {"a": 1}}, "attention/receipts travel as needs_attention/weekly_receipts"


# ── #2 every approve path drops the cached Home ─────────────────────────────

def test_mobile_approve_all_drops_the_cached_home(client, db):
    rid = _rest(db)
    uid, headers = _login(client, db, rid)
    _draft(db, rid)
    home_brief._CACHE[(rid, uid)] = (datetime.now(timezone.utc), {"ok": True, "stale": True})
    home_brief._CACHE[("group", rid, uid)] = (datetime.now(timezone.utc), {"ok": True, "stale": True})
    out = client.post("/mobile/api/reviews/approve-all", headers=headers, json={"limit": 25}).get_json()
    assert out["ok"] and out["approved"] == 1
    assert (rid, uid) not in home_brief._CACHE, "the phone's publish left Home saying the reply still waits"
    assert ("group", rid, uid) not in home_brief._CACHE


def test_a_single_approve_drops_the_cached_home_too(db):
    rid = _rest(db)
    rv = _draft(db, rid)
    home_brief._CACHE[(rid, 1)] = (datetime.now(timezone.utc), {"ok": True})
    payload, status = client_api._do_approve(rv, rid)
    assert status == 200 and payload["ok"]
    assert (rid, 1) not in home_brief._CACHE


def test_the_web_route_no_longer_invalidates_before_the_write():
    import inspect
    src = inspect.getsource(client_api.approve_all_reviews_api)
    assert "invalidate" not in src.replace("# Home's cache is dropped inside _do_approve_all", "")
    body = inspect.getsource(client_api._do_approve_all)
    assert body.index("_invalidate_home(") > body.index("_do_approve(row[\"id\"]")


# ── #7 notifications: read when opened, group scope, one count ──────────────

def _alert(db, rid, kind="1star"):
    c = get_conn(db)
    aid = c.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) "
                    "VALUES (?, ?, datetime('now'))", (rid, kind)).lastrowid
    c.commit(); c.close()
    return aid


def test_mobile_list_with_mark_0_reads_without_marking_and_an_open_reads_one_row(client, db):
    rid = _rest(db)
    _uid, headers = _login(client, db, rid)
    a1, a2 = _alert(db, rid), _alert(db, rid, "labor_over")
    rows = client.get("/mobile/api/notifications?mark=0", headers=headers).get_json()["notifications"]
    assert {r["id"] for r in rows if r["unread"]} == {a1, a2}
    count = client.get("/mobile/api/notifications/unread-count", headers=headers).get_json()["count"]
    assert count == 2, "opening the list no longer reads every row"
    client.post("/mobile/api/notifications/opened", headers=headers, json={"type": "1star", "alert_id": a1})
    rows = {r["id"]: r for r in client.get("/mobile/api/notifications?mark=0", headers=headers)
            .get_json()["notifications"]}
    assert rows[a1]["unread"] is False and rows[a2]["unread"] is True
    assert client.get("/mobile/api/notifications/unread-count", headers=headers).get_json()["count"] == 1
    # "Mark all read" (and every older build) moves the mark over the rest.
    client.get("/mobile/api/notifications", headers=headers)
    assert client.get("/mobile/api/notifications/unread-count", headers=headers).get_json()["count"] == 0


def test_mobile_group_scope_lists_every_location_and_counts_them(client, db):
    a = _rest(db, name="Loc A", owner_email="o@g.com", location_group="Group G", location_name="Downtown")
    b = _rest(db, name="Loc B", owner_email="o@g.com", location_group="Group G", location_name="Uptown")
    _uid, headers = _login(client, db, a, role="owner")
    _alert(db, a)
    ab = _alert(db, b)
    one = client.get("/mobile/api/notifications?mark=0", headers=headers).get_json()["notifications"]
    assert ab not in {r["id"] for r in one}
    group = client.get("/mobile/api/notifications?mark=0&scope=group", headers=headers).get_json()["notifications"]
    assert ab in {r["id"] for r in group}
    web_style = client.get("/mobile/api/notifications/unread-count?scope=group", headers=headers).get_json()
    assert web_style["count"] == 2
    assert client.get("/mobile/api/notifications/unread-count", headers=headers).get_json()["count"] == 1


def test_group_scope_is_one_location_for_a_login_that_cannot_switch(client, db):
    a = _rest(db, name="Loc A", owner_email="o@g.com", location_group="Group G")
    b = _rest(db, name="Loc B", owner_email="o@g.com", location_group="Group G")
    _uid, headers = _login(client, db, a, username="mgr", role="manager")
    ab = _alert(db, b)
    rows = client.get("/mobile/api/notifications?mark=0&scope=group", headers=headers).get_json()["notifications"]
    assert ab not in {r["id"] for r in rows}


# ── #8 the group brief's mobile twin, the owner's locations only ────────────

def test_mobile_group_brief_is_scoped_to_the_owners_own_group(client, db):
    a = _rest(db, name="Loc A", owner_email="o@g.com", location_group="Syrup", location_name="Downtown")
    b = _rest(db, name="Loc B", owner_email="o@g.com", location_group="Syrup", location_name="Uptown")
    stranger = _rest(db, name="Someone Else", owner_email="other@z.com", location_group="Syrup")
    _uid, headers = _login(client, db, a, role="owner")
    g = client.get("/mobile/api/home/brief/group", headers=headers).get_json()
    assert g["ok"] and g["scope"] == "group"
    ids = {l["id"] for l in g["locations"]}
    assert ids == {a, b}, "another owner's location typed into the same group must never appear"
    assert stranger not in {x.get("restaurant_id") for x in g["attention"]}
    for l in g["locations"]:
        assert {"health", "attention", "last_night", "name"} <= set(l)
    assert {"total", "healthy", "needing"} <= set(g["portfolio"])


def test_mobile_group_brief_refuses_a_login_that_cannot_switch(client, db):
    a = _rest(db, name="Loc A", owner_email="o@g.com", location_group="Syrup")
    _rest(db, name="Loc B", owner_email="o@g.com", location_group="Syrup")
    _uid, headers = _login(client, db, a, username="mgr", role="manager")
    r = client.get("/mobile/api/home/brief/group", headers=headers)
    assert r.status_code == 403 and r.get_json()["ok"] is False


def test_mobile_group_brief_requires_auth(client):
    assert client.get("/mobile/api/home/brief/group").status_code == 401


# ── #9 one call for the phone's Last night card ─────────────────────────────

def test_dsr_list_row_carries_net_vs_yesterday(db):
    import dsr
    from datetime import date
    from dsr import access, store
    from models import get_restaurant
    rid = _rest(db)
    day = date(2026, 9, 19)
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower",
                                                 metrics={"net": 2000.0, "vs_yesterday_pct": 12.54}), db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    row = access.summary(store.get_report(rid, day, db_path=db), {"role": "owner"}, get_restaurant(rid))
    assert row["vs_yesterday_pct"] == 12.5
    assert row["net"] == 2000.0


# ── #4 #5 web Home: readiness after the recommendations, one range set ──────

def _dash():
    import os
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "templates", "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def test_readiness_leads_only_a_fresh_account_and_never_follows_the_recommendations():
    """Owner's call, 9/26/26: no readiness pills under Cavnar AI recommends,
    on web or iOS; a brand-new account still leads with it."""
    src = _dash()
    body = src[src.index("  function render(d){"):]
    body = body[:body.index("hbFollow();")]
    assert "if(fresh)h+=renderReadiness(d);" in body
    assert "if(!fresh)h+=renderReadiness(d);" not in body
    ios = open(os.path.join(os.path.dirname(__file__), "..", "ios", "CavnarAI", "CavnarAI", "Features", "Home", "HomeView.swift"), encoding="utf-8").read()
    assert "if !summary.isFresh {\n                                freshStart(summary)" not in ios


def test_web_and_phone_value_ranges_are_one_set():
    import os
    src = _dash()
    assert "['1M','3M','6M','1Y']" in src
    swift = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ios", "CavnarAI",
                              "CavnarAI", "Features", "Home", "ValueChartCard.swift"), encoding="utf-8").read()
    assert 'case oneMonth = "1M", threeMonths = "3M", sixMonths = "6M", oneYear = "1Y"\n' in swift


def test_a_generic_open_module_item_does_not_hide_every_module_shortcut():
    """hbQuickUnsaid drops a quick action a Needs-attention item already
    carries. "open_module" is generic, so it matches by nav only — an
    attention row opening the order draft must not hide "Build next week"."""
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "templates", "dashboard.html"), encoding="utf-8").read()
    body = src[src.index("function hbQuickUnsaid"):src.index("function renderQuick")]
    assert "a.kind!=='open_module'" in body
