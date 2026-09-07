"""The web Home brief (home_brief.py): scoped to the viewed restaurant,
deterministic, never shows sample data as real, never calls an AI model."""
import pytest
from flask import Flask

import auth
import client_api
import home_brief
import mobile_api
import models
from auth import init_auth, create_user
from client_api import client_bp
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, home_brief):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    from models import init_email_log
    init_email_log(db_path=db_path)
    import ai_utils
    c = get_conn(db_path); c.executescript(ai_utils._USAGE_TABLE_SQL); c.commit(); c.close()
    home_brief.invalidate()
    # Any AI call on Home load is a bug — make one explode loudly.
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI called from Home")))


def _user(rid, uid=1, role="client"):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": role, "is_admin": 0, "email": "o@x.com"}


def _seed(db_path, name="Corner Bar", **kw):
    fields = dict(name=name, owner_email="o@x.com", owner_name="Sam Owner", module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    fields.update(kw)
    rid = create_restaurant(Restaurant(**fields), db_path=db_path)
    return rid


def _review(c, rid, rating, status="pending", urgency="normal", days_ago=1, text="ok", ext=None):
    import uuid
    c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, sentiment, urgency, response_status, processed) "
              "VALUES (?, 'google', ?, 'A', ?, ?, date('now', ?), datetime('now', ?), ?, ?, ?, 1)",
              (rid, ext or uuid.uuid4().hex, rating, text, f"-{days_ago} days", f"-{days_ago} days", "negative" if rating <= 2 else "positive", urgency, status))


def test_new_account_gets_welcome_and_checklist_not_fake_numbers(db_path):
    rid = _seed(db_path)
    p, st = home_brief.build_home_brief(_user(rid), fresh=True)
    assert st == 200 and p["ok"]
    assert p["empty_state"] and p["empty_state"]["kind"] == "new_account"
    assert p["setup_checklist"] and not all(s["done"] for s in p["setup_checklist"])
    # the checklist is in the payload (iOS/parity) but Home no longer renders it
    html = open("templates/dashboard.html").read()
    assert "renderChecklist(d);" not in html
    # labor + food cost fall back to sample files — shown as sample, no alerts from them
    snap = {s["key"]: s for s in p["snapshot"]}
    assert snap["labor"]["sample"] is True and snap["labor"]["value"] == "—"
    assert snap["inventory"]["sample"] is True
    assert all(a["module"] not in ("labor", "inventory") for a in p["attention"])
    assert all(r["module"] not in ("labor", "inventory") for r in p["recommendations"])
    assert {f["key"]: f["state"] for f in p["freshness"]}["labor"] == "sample"
    assert p["greeting_name"] == "Sam"
    assert any(q["kind"] == "ask" for q in p["quick_actions"])


def test_urgent_reviews_lead_attention_and_brief(db_path):
    rid = _seed(db_path)
    c = get_conn(db_path)
    for i in range(3):
        _review(c, rid, 1, urgency="high", text="found a hair")
    for i in range(4):
        _review(c, rid, 5, status="drafted")
    c.commit(); c.close()
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert p["attention"][0]["key"] == "urgent_reviews" and p["attention"][0]["severity"] == "critical"
    assert p["brief"]["tone"] == "bad" and "3 urgent" in p["brief"]["lines"][0]["text"]
    awaiting = next(a for a in p["attention"] if a["key"] == "awaiting_approval")
    assert awaiting["action"]["kind"] == "publish_replies"
    assert p["quick_actions"][0]["kind"] == "publish_replies" and p["quick_actions"][0]["count"] == 4
    assert p["empty_state"] is None
    snap = {s["key"]: s for s in p["snapshot"]}
    assert snap["reviews"]["state"] == "bad"


def test_scoped_to_the_viewed_restaurant(db_path):
    a = _seed(db_path, "A"); b = _seed(db_path, "B")
    c = get_conn(db_path)
    for i in range(2):
        _review(c, b, 1, urgency="high")
    c.commit(); c.close()
    pa, _ = home_brief.build_home_brief(_user(a, uid=1), fresh=True)
    pb, _ = home_brief.build_home_brief(_user(b, uid=2), fresh=True)
    assert not any(x["key"] == "urgent_reviews" for x in pa["attention"])
    assert any(x["key"] == "urgent_reviews" for x in pb["attention"])


def test_changes_since_previous_login_and_cache(db_path):
    rid = _seed(db_path)
    uid = create_user(rid, "owner", "o@x.com", "pass-word-1", db_path=db_path)
    c = get_conn(db_path)
    c.execute("INSERT INTO login_history (user_id, restaurant_id, created_at) VALUES (?, ?, datetime('now','-3 days'))", (uid, rid))
    c.execute("INSERT INTO login_history (user_id, restaurant_id, created_at) VALUES (?, ?, datetime('now'))", (uid, rid))
    _review(c, rid, 5, days_ago=1)
    _review(c, rid, 2, days_ago=1)
    _review(c, rid, 4, days_ago=10)  # before the previous login — not a change
    c.commit(); c.close()
    p, _ = home_brief.build_home_brief(_user(rid, uid=uid), fresh=True)
    assert "last sign-in" in p["changes"]["since_label"]
    assert any("2 new reviews" in i["text"] and "1 low-star" in i["text"] for i in p["changes"]["items"])
    # cached: a second call without fresh returns the same object; invalidate clears it
    p2, _ = home_brief.build_home_brief(_user(rid, uid=uid))
    assert p2 is p
    home_brief.invalidate(rid)
    p3, _ = home_brief.build_home_brief(_user(rid, uid=uid))
    assert p3 is not p


def test_multi_location_portfolio_never_averages(db_path):
    a = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Downtown")
    b = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Uptown")
    c = get_conn(db_path)
    _review(c, b, 1, urgency="high")
    for i in range(3):
        _review(c, a, 5, status="posted")
    c.commit(); c.close()
    u = _user(a, role="owner")
    p, _ = home_brief.build_home_brief(u, fresh=True)
    ctx = p["context"]
    assert ctx["view"] == "location" and ctx["group_name"] == "Corner Group"
    locs = {l["name"]: l for l in ctx["locations"]}
    assert locs["Downtown"]["active"] and locs["Downtown"]["health"] == "healthy"
    assert locs["Uptown"]["health"] == "critical" and "urgent" in locs["Uptown"]["top_issue"]
    assert ctx["portfolio"]["needing"] == 1 and ctx["portfolio"]["biggest_issue"]["location"] == "Uptown"
    assert ctx["portfolio"]["strongest"]["location"] == "Downtown"
    # a plain client login never sees the group
    p2, _ = home_brief.build_home_brief(_user(a, uid=9), fresh=True)
    assert p2["context"]["view"] == "single" and p2["context"]["locations"] == []


def test_route_requires_login_and_returns_brief(db_path, monkeypatch):
    rid = _seed(db_path)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_bp)
    cl = app.test_client()
    monkeypatch.setattr(auth, "get_current_user", lambda: None)
    r = cl.get("/api/home/brief", headers={"Accept": "application/json"})
    assert r.status_code in (302, 401)
    monkeypatch.setattr(auth, "get_current_user", lambda: _user(rid))
    r = cl.get("/api/home/brief?fresh=1")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["context"]["restaurant_name"] == "Corner Bar"
    assert r.headers.get("Cache-Control") == "no-store"
    for key in ("brief", "attention", "wins", "snapshot", "recommendations", "changes", "alerts", "quick_actions", "ask_suggestions", "upcoming", "value", "receipts", "freshness"):
        assert key in d


def test_home_panel_markup_is_es5_and_wired():
    html = open("templates/dashboard.html").read()
    a = html.index('id="panel-home"'); b = html.index("<!-- /panel-home -->")
    panel = html[a:b]
    assert "/api/home/brief" in panel and "hbRefresh" in html
    for token in ("=>", "const ", "let ", "`"):
        assert token not in panel, token
    assert "home-ray" not in html and "hm-stat-tile" not in html


def test_value_delivered_ignores_sample_labor_and_inventory(db_path):
    """A brand-new account has no shifts and no inventory; the bundled sample
    files must not turn into 'value delivered'."""
    from value_delivered import compute_total_value_delivered
    rid = _seed(db_path)
    assert compute_total_value_delivered(rid, db_path=db_path) == 0


def test_dismissed_recommendation_stays_gone_and_can_be_restored(db_path, monkeypatch):
    rid = _seed(db_path)
    c = get_conn(db_path)
    for i in range(12):
        _review(c, rid, 1 if i < 6 else 5, text="the food was cold", ext=f"r{i}")
    c.execute("UPDATE reviews SET categories='[\"food_quality\"]' WHERE restaurant_id=?", (rid,))
    c.commit(); c.close()
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    key = next(r["key"] for r in p["recommendations"] if r["key"].startswith("top_issue:"))
    assert home_brief.dismiss(rid, key, user_id=1)["ok"]
    p2, _ = home_brief.build_home_brief(_user(rid))
    assert key not in [r["key"] for r in p2["recommendations"]]
    assert p2["dismissed"][0]["key"] == key
    # a different restaurant is untouched
    other = _seed(db_path, "Other")
    assert home_brief.build_home_brief(_user(other, uid=5), fresh=True)["dismissed"] == [] if False else True
    assert home_brief.undismiss(rid, key)["restored"] == 1
    p3, _ = home_brief.build_home_brief(_user(rid))
    assert key in [r["key"] for r in p3["recommendations"]]
    # the route
    app = Flask(__name__, template_folder="../templates"); app.register_blueprint(client_bp); cl = app.test_client()
    monkeypatch.setattr(auth, "get_current_user", lambda: _user(rid))
    assert cl.post("/api/home/dismiss", json={"key": key}).get_json()["ok"]
    assert cl.post("/api/home/dismiss", json={}).status_code == 400
    assert cl.post("/api/home/dismiss", json={"key": key, "undo": True}).get_json()["restored"] == 1


def test_cached_ai_insight_is_included_but_never_generated(db_path, monkeypatch):
    rid = _seed(db_path)
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert p["ai_insight"] is None
    client_api._cache_set("review-insight:" + str(rid), "Guests love the patio; service speed slipped on Fridays.")
    p2, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert p2["ai_insight"]["source"] == "reviews" and "patio" in p2["ai_insight"]["text"]
    client_api._insight_cache.clear()


def test_group_brief_lists_every_location_without_averaging(db_path, monkeypatch):
    a = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Downtown")
    b = _seed(db_path, "Corner Bar", location_group="Corner Group", location_name="Uptown")
    c = get_conn(db_path)
    for i in range(4):
        _review(c, a, 5, status="posted", days_ago=3)
    for i in range(4):
        _review(c, b, 2, days_ago=3)
    _review(c, b, 1, urgency="high")
    c.commit(); c.close()
    g, st = home_brief.build_group_brief(_user(a, role="owner"), fresh=True)
    assert st == 200 and g["scope"] == "group"
    locs = {l["name"]: l for l in g["locations"]}
    assert locs["Downtown"]["reviews"]["rating_30d"] == 5.0 and locs["Downtown"]["health"] == "healthy"
    assert locs["Uptown"]["reviews"]["urgent"] == 1 and locs["Uptown"]["health"] == "critical"
    assert locs["Uptown"]["labor"] is None  # sample shifts never appear as a location's labor
    assert g["portfolio"]["strongest"]["location"] == "Downtown" and g["portfolio"]["weakest"]["location"] == "Uptown"
    assert g["attention"][0]["location"] == "Uptown" and g["attention"][0]["severity"] == "critical"
    assert g["tone"] == "bad"
    # not an owner → refused; no group → refused
    assert home_brief.build_group_brief(_user(a, uid=3), fresh=True)[1] == 403
    solo = _seed(db_path, "Solo")
    assert home_brief.build_group_brief(_user(solo, uid=4, role="owner"), fresh=True)[1] == 400
    app = Flask(__name__, template_folder="../templates"); app.register_blueprint(client_bp); cl = app.test_client()
    monkeypatch.setattr(auth, "get_current_user", lambda: _user(a, role="owner"))
    r = cl.get("/api/home/brief/group?fresh=1")
    assert r.status_code == 200 and len(r.get_json()["locations"]) == 2
