"""Module entitlement is enforced, not just hidden.

From the pre-launch audit: modules are sold separately (pricing.py), and both
the dashboard and the app hide the tabs a client hasn't bought — but nothing
stopped a Reviews-only client from calling the Labor, Food Cost, Marketing or
Intel endpoints directly and getting the whole feature, Claude calls
included, at Cavnar's cost. Only a handful of routes checked for themselves.

The gate lives in login_required / mobile_login_required, keyed on a path
prefix table (auth._MODULE_PREFIXES), so a new route under an existing family
is covered the day it is written.
"""
import pytest
from flask import Flask

import auth
import client_api
import guest_marketing
import mobile_api
import models
from auth import create_user, init_auth
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, restaurant_has_module

# The deploy gate's contract slice (audit #25; TESTING.md, Tiers).
pytestmark = pytest.mark.contract


def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)


@pytest.fixture(autouse=True)
def _init(db_path, monkeypatch):
    _redirect_db(monkeypatch, db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def client():
    flask_app = Flask(__name__)
    flask_app.register_blueprint(mobile_bp)
    return flask_app.test_client()


def _restaurant(db_path, **kw):
    kw.setdefault("owner_email", "m@x.test")
    return create_restaurant(Restaurant(name=kw.pop("name", "Entitlement Co"), **kw), db_path=db_path)


def _login(client, db_path, rid, username="alice"):
    create_user(rid, username, f"{username}@x.test", "correct-horse", db_path=db_path)
    return client.post("/mobile/api/login", json={"username": username, "password": "correct-horse"}).get_json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


# ── the path → module table ─────────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("/mobile/api/labor", "labor"),
    ("/mobile/api/labor/generate-schedule", "labor"),
    ("/mobile/api/food-cost/analytics", "inventory"),
    ("/mobile/api/marketing/calendar", "marketing"),
    ("/mobile/api/guest-contacts", "marketing"),
    ("/mobile/api/intel/ai-visibility", "intel"),
    ("/mobile/api/reviews", "reviews"),
    ("/api/labor-gap", "labor"),
    ("/api/mkt-stats", "marketing"),
    ("/api/food-cost/order-draft", "inventory"),
    ("/api/intel/add-competitor", "intel"),
    ("/api/reviews/3/delete", "reviews"),
    ("/api/review-stats", "reviews"),
    # Deliberately ungated: these span modules or sit outside them entirely.
    ("/mobile/api/home", None),
    ("/mobile/api/account", None),
    ("/mobile/api/ask-cavnar", None),
    ("/api/home/brief", None),
    ("/api/notifications", None),
    ("/api/billing-info", None),
])
def test_paths_map_to_the_module_that_owns_them(path, expected):
    assert auth._required_module(path) == expected


# ── the predicate ───────────────────────────────────────────────────────────

def test_a_module_the_client_bought_is_allowed(db_path):
    rid = _restaurant(db_path, module_labor=1, module_inventory=0)
    assert restaurant_has_module(rid, "labor", db_path=db_path) is True
    assert restaurant_has_module(rid, "inventory", db_path=db_path) is False


def test_intel_follows_the_full_tier_entitlement_not_the_listing_connection(db_path):
    """A full-tier client who hasn't linked Google has bought Intel — telling
    them it isn't part of their plan would be false. The route's own "connect
    your listing" is the true answer."""
    full = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    assert restaurant_has_module(full, "intel", db_path=db_path) is True

    partial = _restaurant(db_path, name="Partial", module_reviews=1, module_labor=1,
                          module_inventory=0, module_marketing=1)
    assert restaurant_has_module(partial, "intel", db_path=db_path) is False


def test_the_predicate_fails_open(db_path, monkeypatch):
    rid = _restaurant(db_path, module_labor=0)
    monkeypatch.setattr(models, "get_restaurant",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert restaurant_has_module(rid, "labor", db_path=db_path) is True


def test_a_missing_restaurant_fails_open(db_path):
    assert restaurant_has_module(999999, "labor", db_path=db_path) is True


# ── enforcement over HTTP ───────────────────────────────────────────────────

def test_a_reviews_only_client_cannot_reach_labor(client, db_path):
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/labor", headers=_hdr(token))
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["module_locked"] is True and body["module"] == "Labor"
    assert body["ok"] is False


def test_a_reviews_only_client_cannot_burn_claude_credit_on_a_schedule(client, db_path):
    """The expensive one: schedule generation is a multi-second Claude call
    Cavnar pays for."""
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    token = _login(client, db_path, rid)
    assert client.post("/mobile/api/labor/generate-schedule", headers=_hdr(token)).status_code == 403


def test_a_reviews_only_client_cannot_reach_food_cost_marketing_or_intel(client, db_path):
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    token = _login(client, db_path, rid)
    for path in ("/mobile/api/food-cost/analytics", "/mobile/api/marketing",
                 "/mobile/api/guest-contacts", "/mobile/api/intel"):
        assert client.get(path, headers=_hdr(token)).status_code == 403, path


def test_the_module_they_did_buy_still_works(client, db_path):
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    token = _login(client, db_path, rid)
    assert client.get("/mobile/api/reviews", headers=_hdr(token)).status_code == 200
    assert client.get("/mobile/api/review-stats", headers=_hdr(token)).status_code == 200


def test_home_account_and_ask_stay_reachable_on_any_plan(client, db_path):
    """The gate must not lock a client out of the parts of the app that
    aren't modules — otherwise a single-module plan looks broken."""
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    token = _login(client, db_path, rid)
    for path in ("/mobile/api/home", "/mobile/api/account", "/mobile/api/notifications",
                 "/mobile/api/changelog", "/mobile/api/me"):
        assert client.get(path, headers=_hdr(token)).status_code == 200, path


def test_a_full_tier_client_is_never_refused_by_the_gate(client, db_path):
    rid = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    token = _login(client, db_path, rid)
    # Not-403 rather than 200: some of these need optional tables this bare
    # fixture doesn't populate, and the claim under test is that the
    # entitlement gate lets a full-tier client through, not that every
    # handler is happy with an empty database.
    for path in ("/mobile/api/labor", "/mobile/api/food-cost/analytics",
                 "/mobile/api/marketing", "/mobile/api/intel", "/mobile/api/reviews"):
        assert client.get(path, headers=_hdr(token)).status_code != 403, path
    for path in ("/mobile/api/labor", "/mobile/api/marketing", "/mobile/api/intel", "/mobile/api/reviews"):
        assert client.get(path, headers=_hdr(token)).status_code == 200, path


# ── the team's side of Labor, and the gates that fail open (audit #25) ─────

@pytest.mark.parametrize("path", [
    "/api/staff-brief", "/api/staff-brief/draft", "/api/staff-brief/approve", "/api/house-rules",
    "/api/staff-docs", "/api/staff-docs/3/remove", "/api/staff-certs", "/api/staff-certs/3/remove",
    "/mobile/api/staff-brief", "/mobile/api/staff-brief/focus", "/mobile/api/house-rules",
    "/mobile/api/staff-docs", "/mobile/api/staff-certs/9/remove",
])
def test_the_staff_brief_rules_docs_and_certs_belong_to_labor(path):
    """They checked the role inside but not the plan, and no sweep saw them
    (A04-06): a Reviews-only owner could draft staff briefs, a model call."""
    assert auth._required_module(path) == "labor"


def test_the_public_status_json_is_deliberately_ungated():
    assert auth._required_module("/api/status") is None
    assert any("/api/status".startswith(p) for p in auth._UNGATED_PREFIXES)


@pytest.fixture
def knowledge_client():
    import staff_knowledge_routes
    flask_app = Flask(__name__)
    flask_app.register_blueprint(mobile_bp)
    flask_app.register_blueprint(staff_knowledge_routes.knowledge_mobile_bp)
    return flask_app.test_client()


def test_a_reviews_only_client_cannot_draft_a_staff_brief_or_read_house_rules(knowledge_client, db_path):
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    token = _login(knowledge_client, db_path, rid)
    for method, path in (("post", "/mobile/api/staff-brief/draft"), ("get", "/mobile/api/staff-brief"),
                         ("get", "/mobile/api/house-rules"), ("get", "/mobile/api/staff-docs"),
                         ("get", "/mobile/api/staff-certs")):
        resp = getattr(knowledge_client, method)(path, headers=_hdr(token), json={} if method == "post" else None)
        assert resp.status_code == 403, path
        assert resp.get_json()["module_locked"] is True, path


def test_a_labor_client_still_reads_house_rules(knowledge_client, db_path):
    rid = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=0, module_marketing=0)
    token = _login(knowledge_client, db_path, rid)
    resp = knowledge_client.get("/mobile/api/house-rules", headers=_hdr(token))
    assert resp.status_code == 200 and resp.get_json()["ok"] is True


@pytest.fixture
def captured(monkeypatch):
    import ops
    seen = []
    monkeypatch.setattr(ops, "capture", lambda exc, job="unknown", context="", **k: seen.append((job, type(exc).__name__)))
    models._gate_open_reported.clear()
    yield seen
    models._gate_open_reported.clear()


def test_the_module_gate_still_fails_open_but_reports_it_once_a_minute(client, db_path, captured, monkeypatch):
    """A03-12: a broken restaurants read lets the request through (revenue,
    not data) — and the console hears about it, once a minute, not per
    request."""
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    token = _login(client, db_path, rid)
    real = models.get_active_modules
    monkeypatch.setattr(models, "get_active_modules", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert client.get("/mobile/api/labor", headers=_hdr(token)).status_code != 403
    assert client.get("/mobile/api/labor", headers=_hdr(token)).status_code != 403
    assert captured == [("module_gate_open", "RuntimeError")]
    monkeypatch.setattr(models, "get_active_modules", real)


def test_the_billing_predicate_reports_a_failed_read(db_path, captured, monkeypatch):
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert models.subscription_allows_access(1) is True
    assert captured == [("billing_gate_open", "RuntimeError")]


def test_the_reporter_reports_again_after_its_window(captured, monkeypatch):
    clock = [1000.0]
    import time
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    assert models.report_gate_open("module", RuntimeError("x"), restaurant_id=1) is True
    assert models.report_gate_open("module", RuntimeError("x"), restaurant_id=1) is False
    assert models.report_gate_open("module", ValueError("y"), restaurant_id=1) is True
    clock[0] += models._GATE_OPEN_REPORT_SECONDS + 1
    assert models.report_gate_open("module", RuntimeError("x"), restaurant_id=1) is True
    assert [j for j, _ in captured] == ["module_gate_open"] * 3


def test_the_auth_gates_report_through_the_same_reporter(captured):
    import flask
    app = flask.Flask(__name__)
    with app.test_request_context("/api/labor"):
        user = {"restaurant_id": 7, "is_admin": False}
        assert auth._gate_failed_open("pos_hold", KeyError("restaurant_id"), user) is True
    assert captured == [("pos_hold_gate_open", "KeyError")]
