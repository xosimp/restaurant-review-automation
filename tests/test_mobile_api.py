"""mobile_api.py — the iOS app's bearer-token auth flow (login/2FA/logout)
and cross-tenant (IDOR) scoping on the review/food-cost routes it reuses from
client_api.py via the shared _do_*() helpers."""
import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import mobile_api
import models
import notify
import guest_marketing
from auth import create_user, init_auth, set_user_role
from auth_routes import _login_attempts
from mobile_api import mobile_bp
from models import create_restaurant, Restaurant, Review, save_reviews, update_restaurant, get_conn


def _redirect_db(monkeypatch, db_path):
    """Every one of these modules does `from models import get_conn` (or binds
    its own get_conn reference) at module level — patching models.get_conn
    alone does not redirect their calls. See test_auth_routes.py's identical
    comment for the same gotcha."""
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, auth_routes, client_api, mobile_api, notify, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect)


@pytest.fixture(autouse=True)
def _init_auth_tables(db_path):
    init_auth(db_path=db_path)


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    """mobile_api.py's /login and /verify-2fa reuse auth_routes' own
    in-memory limiter (one shared brute-force counter, not two) — clear it
    between tests same as test_auth_routes.py does."""
    _login_attempts.clear()
    yield
    _login_attempts.clear()


@pytest.fixture
def app(db_path, monkeypatch):
    _redirect_db(monkeypatch, db_path)
    flask_app = Flask(__name__)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Mobile Test Co"), owner_email="m@x.com", **kw), db_path=db_path)


def _add_review(db_path, rid, external_id="rev1", draft_response="Thanks for coming!"):
    # save_reviews() only inserts the fetch-time columns (author/rating/text/
    # dates) — draft_response/response_status are set later by the drafting
    # pipeline, so this test fixture sets them directly to simulate "already
    # has an AI draft awaiting approval."
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=external_id,
                          author="Ann", rating=2, text="Slow service.")], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE reviews SET draft_response=?, response_status='drafted', processed=1 WHERE restaurant_id=? AND external_id=?",
        (draft_response, rid, external_id)
    )
    conn.commit()
    row = conn.execute("SELECT id FROM reviews WHERE restaurant_id=? AND external_id=?", (rid, external_id)).fetchone()
    conn.close()
    return row["id"]


def _login(client, db_path, rid, username="alice", password="correct-horse", role=None):
    uid = create_user(rid, username, f"{username}@x.com", password, db_path=db_path)
    if role:
        set_user_role(uid, role, db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": username, "password": password})
    return resp.get_json()["token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _stored_2fa_code(db_path, rid):
    conn = get_conn(db_path)
    row = conn.execute("SELECT two_fa_code FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    return row["two_fa_code"]


# ── /login ────────────────────────────────────────────────────────────────

def test_login_happy_path_returns_bearer_token(client, db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["requires_2fa"] is False
    assert data["token"]
    assert data["user"]["username"] == "alice"
    assert "password_hash" not in data["user"]


def test_login_wrong_password_rejected(client, db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": "alice", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_login_rate_limited_after_max_failed_attempts(client, db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    for _ in range(5):
        client.post("/mobile/api/login", json={"username": "alice", "password": "wrong"})
    resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    assert resp.status_code == 429


def test_login_with_2fa_enabled_returns_pending_token_not_session(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["requires_2fa"] is True
    assert data.get("pending_token")
    assert "token" not in data


# ── /verify-2fa ──────────────────────────────────────────────────────────

def test_verify_2fa_correct_code_returns_ios_tagged_session(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    login_resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    pending_token = login_resp.get_json()["pending_token"]
    code = _stored_2fa_code(db_path, rid)

    resp = client.post("/mobile/api/verify-2fa", json={"pending_token": pending_token, "code": code})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["token"]

    conn = get_conn(db_path)
    row = conn.execute("SELECT device_type FROM sessions WHERE token=?", (data["token"],)).fetchone()
    conn.close()
    assert row["device_type"] == "ios"


def test_verify_2fa_wrong_code_rejected(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    login_resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    pending_token = login_resp.get_json()["pending_token"]

    resp = client.post("/mobile/api/verify-2fa", json={"pending_token": pending_token, "code": "000000"})
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_verify_2fa_rate_limited_after_max_attempts(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    login_resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    pending_token = login_resp.get_json()["pending_token"]
    for _ in range(5):
        client.post("/mobile/api/verify-2fa", json={"pending_token": pending_token, "code": "000000"})

    code = _stored_2fa_code(db_path, rid)
    resp = client.post("/mobile/api/verify-2fa", json={"pending_token": pending_token, "code": code})
    assert resp.status_code == 429


# ── mobile_login_required (missing/expired/garbage token) ──────────────────

def test_protected_route_without_token_401s(client):
    resp = client.get("/mobile/api/review-stats")
    assert resp.status_code == 401
    assert resp.get_json()["session_expired"] is True


def test_protected_route_with_garbage_token_401s(client):
    resp = client.get("/mobile/api/review-stats", headers=_auth_headers("not-a-real-token"))
    assert resp.status_code == 401


# ── logout / revoke-others ─────────────────────────────────────────────────

def test_logout_deletes_the_session(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/logout", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True
    conn = get_conn(db_path)
    row = conn.execute("SELECT 1 FROM sessions WHERE token=?", (token,)).fetchone()
    conn.close()
    assert row is None


# ── cross-tenant scoping (IDOR) on reused review/food-cost routes ─────────

def test_approve_does_not_affect_another_restaurants_review(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    review_id = _add_review(db_path, rid_a)
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.post(f"/mobile/api/reviews/{review_id}/approve", headers=_auth_headers(token_b))
    assert resp.status_code == 200  # approve() has no existence check — just scopes the UPDATE

    conn = get_conn(db_path)
    row = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["response_status"] == "drafted"  # unchanged: restaurant B couldn't touch A's review


def test_save_draft_rejects_cross_tenant_review(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    review_id = _add_review(db_path, rid_a)
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.post(f"/mobile/api/reviews/{review_id}/save-draft",
                        json={"draft": "hacked"}, headers=_auth_headers(token_b))
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == "Review not found"


def test_food_cost_quickcount_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    token_a = _login(client, db_path, rid_a, username="alice")

    resp = client.post("/mobile/api/food-cost/quickcount",
                        json={"items": [{"name": "Cheese", "price": 10, "usage": 5}]},
                        headers=_auth_headers(token_a))
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row_a = conn.execute("SELECT food_cost_json FROM client_data WHERE restaurant_id=?", (rid_a,)).fetchone()
    row_b = conn.execute("SELECT food_cost_json FROM client_data WHERE restaurant_id=?", (rid_b,)).fetchone()
    conn.close()
    assert row_a is not None
    assert row_b is None


# ── /mobile/api/food-cost/analytics ────────────────────────────────────────

def test_food_cost_analytics_requires_auth(client):
    resp = client.get("/mobile/api/food-cost/analytics")
    assert resp.status_code == 401


def test_food_cost_analytics_returns_ok_for_fresh_restaurant(client, db_path, monkeypatch):
    # A restaurant with no real inventory uploads still gets sample-fallback
    # data from load_inventory_for_restaurant (same convention labor/reviews
    # already use) — this just checks the response shape, not real content.
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("inventory.get_claude_insights", lambda *a, **kw: "Waste is under control.")

    resp = client.get("/mobile/api/food-cost/analytics", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert isinstance(data["waste_items"], list)
    assert isinstance(data["overstock"], list)
    assert data["insight"] == "Waste is under control."


# ── switch-location owner/group checks ─────────────────────────────────────

def test_switch_location_rejects_non_owner(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)  # role defaults to 'client'
    resp = client.post("/mobile/api/switch-location", json={"restaurant_id": rid}, headers=_auth_headers(token))
    assert resp.status_code == 403


def test_switch_location_rejects_out_of_group_id(client, db_path):
    rid = _restaurant(db_path, name="Home Base", location_group="Group1")
    rid_other = _restaurant(db_path, name="Unrelated", location_group="Group2")
    token = _login(client, db_path, rid, username="owner1", role="owner")

    resp = client.post("/mobile/api/switch-location", json={"restaurant_id": rid_other}, headers=_auth_headers(token))
    assert resp.status_code == 403


def test_switch_location_allows_owner_within_group(client, db_path):
    rid = _restaurant(db_path, name="Home Base", location_group="GroupX")
    rid_sibling = _restaurant(db_path, name="Sibling", location_group="GroupX")
    token = _login(client, db_path, rid, username="owner1", role="owner")

    resp = client.post("/mobile/api/switch-location", json={"restaurant_id": rid_sibling}, headers=_auth_headers(token))
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["restaurant_id"] == rid_sibling


# ── /home and /reviews — new mobile-only endpoints ─────────────────────────

def test_home_returns_kpis_and_needs_attention_for_fresh_restaurant(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/home", headers=_auth_headers(token))
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    module_keys = [m["key"] for m in data["modules"]]
    assert "reviews" in module_keys
    assert isinstance(data["needs_attention"], list)


def test_home_omits_modules_the_restaurant_does_not_have(client, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/home", headers=_auth_headers(token))
    data = resp.get_json()
    module_keys = [m["key"] for m in data["modules"]]
    assert "marketing" not in module_keys
    assert "reviews" in module_keys  # default-on modules still show


def test_home_surfaces_awaiting_approval_in_needs_attention(client, db_path):
    rid = _restaurant(db_path)
    _add_review(db_path, rid)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/home", headers=_auth_headers(token))
    data = resp.get_json()
    reviews_module = next(m for m in data["modules"] if m["key"] == "reviews")
    assert reviews_module["kpi"]["value"] == "0/1"
    types = [item["type"] for item in data["needs_attention"]]
    assert "reviews_awaiting_approval" in types
    modules_hit = [item["module"] for item in data["needs_attention"]]
    assert "reviews" in modules_hit


def test_reviews_endpoint_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    _add_review(db_path, rid_a, external_id="only-a")
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.get("/mobile/api/reviews", headers=_auth_headers(token_b))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["reviews"] == []  # restaurant B sees none of restaurant A's reviews


# ── /mobile/api/labor ───────────────────────────────────────────────────────

def test_labor_endpoint_requires_auth(client):
    resp = client.get("/mobile/api/labor")
    assert resp.status_code == 401


def test_labor_endpoint_reflects_own_restaurants_target_only(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A", labor_target_pct=25.0)
    rid_b = _restaurant(db_path, name="Restaurant B", labor_target_pct=35.0)
    token_a = _login(client, db_path, rid_a, username="alice")

    resp = client.get("/mobile/api/labor", headers=_auth_headers(token_a))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["target"] == 25.0
    assert data["target"] != 35.0  # never leaks restaurant B's target


def test_generate_schedule_requires_auth(client):
    resp = client.post("/mobile/api/labor/generate-schedule")
    assert resp.status_code == 401


def test_schedule_status_unknown_job_returns_404(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/labor/schedule-status/not-a-real-job-id", headers=_auth_headers(token))
    assert resp.status_code == 404


# ── /mobile/api/labor/trend, /gap, /insight ────────────────────────────────

def test_labor_trend_requires_auth(client):
    resp = client.get("/mobile/api/labor/trend")
    assert resp.status_code == 401


def test_labor_trend_returns_empty_weeks_for_fresh_restaurant(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/labor/trend", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["weeks"] == []


def test_labor_gap_requires_auth(client):
    resp = client.get("/mobile/api/labor/gap")
    assert resp.status_code == 401


def test_labor_gap_returns_ok(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/labor/gap", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert "over_target" in data


def test_labor_insight_requires_auth(client):
    resp = client.get("/mobile/api/labor/insight")
    assert resp.status_code == 401


def test_labor_insight_returns_cached_value(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("client_api._cache_get", lambda key: "Cached labor insight.")

    resp = client.get("/mobile/api/labor/insight", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["insight"] == "Cached labor insight."


# ── /mobile/api/marketing ────────────────────────────────────────────────────

def test_marketing_endpoint_requires_auth(client):
    resp = client.get("/mobile/api/marketing")
    assert resp.status_code == 401


def test_marketing_stats_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
        content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
        created_at TEXT DEFAULT (datetime('now')))""")
    conn.execute(
        "INSERT INTO marketing_content_log (restaurant_id, content_type, topic) VALUES (?, 'instagram_post', 'test')",
        (rid_a,)
    )
    conn.commit()
    conn.close()
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.get("/mobile/api/marketing", headers=_auth_headers(token_b))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["stats"]["generated"] == 0  # restaurant B sees none of A's generated content


def test_generate_content_requires_auth(client):
    resp = client.post("/mobile/api/marketing/generate-content", json={"type": "instagram_post", "topic": "tacos"})
    assert resp.status_code == 401


def test_generate_content_returns_generated_text_on_success(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("ai_utils.ai_rate_limited", lambda *a, **kw: False)
    monkeypatch.setattr("marketing.generate_content", lambda *a, **kw: "Fresh pasta, fresher vibes. 🍝")

    resp = client.post(
        "/mobile/api/marketing/generate-content", json={"type": "instagram_post", "topic": "pasta"},
        headers=_auth_headers(token)
    )
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["content"] == "Fresh pasta, fresher vibes. 🍝"


def test_generate_content_rate_limited_returns_429(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("ai_utils.ai_rate_limited", lambda *a, **kw: True)

    resp = client.post(
        "/mobile/api/marketing/generate-content", json={"type": "instagram_post", "topic": "pasta"},
        headers=_auth_headers(token)
    )
    assert resp.status_code == 429
    assert resp.get_json()["ok"] is False


# ── /mobile/api/marketing/performance, insight, post-to-* ──────────────────

def test_marketing_performance_requires_auth(client):
    resp = client.get("/mobile/api/marketing/performance")
    assert resp.status_code == 401


def test_marketing_performance_returns_ok_for_fresh_restaurant(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/marketing/performance", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["published"] == 0
    assert data["has_data"] is False


def test_marketing_insight_requires_auth(client):
    resp = client.get("/mobile/api/marketing/insight")
    assert resp.status_code == 401


def test_marketing_insight_returns_cached_value(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("client_api._cache_get", lambda key: "Cached marketing brief.")

    resp = client.get("/mobile/api/marketing/insight", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["insight"] == "Cached marketing brief."


def test_post_to_instagram_requires_auth(client):
    resp = client.post("/mobile/api/marketing/post-to-instagram", json={})
    assert resp.status_code == 401


def test_post_to_instagram_reports_not_connected(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post(
        "/mobile/api/marketing/post-to-instagram",
        json={"caption": "Hello", "image_url": "https://example.com/x.jpg"},
        headers=_auth_headers(token),
    )
    data = resp.get_json()
    assert data["ok"] is False
    assert "not connected" in data["error"].lower()


def test_post_to_facebook_requires_auth(client):
    resp = client.post("/mobile/api/marketing/post-to-facebook", json={})
    assert resp.status_code == 401


def test_post_to_facebook_reports_not_connected(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post(
        "/mobile/api/marketing/post-to-facebook", json={"caption": "Hello"}, headers=_auth_headers(token)
    )
    data = resp.get_json()
    assert data["ok"] is False
    assert "not connected" in data["error"].lower()


# ── /mobile/api/guest-contacts + guest-campaign ────────────────────────────

def test_guest_contacts_requires_auth(client):
    resp = client.get("/mobile/api/guest-contacts")
    assert resp.status_code == 401


def test_guest_contacts_requires_marketing_module(client, db_path):
    rid = _restaurant(db_path, module_marketing=0)  # module_marketing defaults ON — turn it off
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/guest-contacts", headers=_auth_headers(token))
    assert resp.status_code == 403


def test_guest_contacts_add_list_delete_roundtrip(client, db_path):
    from guest_marketing import init_guest_marketing
    init_guest_marketing(db_path)
    rid = _restaurant(db_path, module_marketing=1)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/guest-contacts", json={"name": "Jamie", "phone": "312-555-0100"},
        headers=_auth_headers(token),
    )
    data = resp.get_json()
    assert data["ok"] is True
    contact_id = data["id"]

    resp2 = client.get("/mobile/api/guest-contacts", headers=_auth_headers(token))
    contacts = resp2.get_json()["contacts"]
    assert len(contacts) == 1
    assert contacts[0]["name"] == "Jamie"

    resp3 = client.post(
        f"/mobile/api/guest-contacts/{contact_id}/mark-visit", headers=_auth_headers(token)
    )
    assert resp3.get_json()["ok"] is True

    resp4 = client.delete(f"/mobile/api/guest-contacts/{contact_id}", headers=_auth_headers(token))
    assert resp4.get_json()["ok"] is True
    resp5 = client.get("/mobile/api/guest-contacts", headers=_auth_headers(token))
    assert resp5.get_json()["contacts"] == []


def test_guest_contacts_add_requires_name_and_phone(client, db_path):
    rid = _restaurant(db_path, module_marketing=1)
    token = _login(client, db_path, rid)
    resp = client.post(
        "/mobile/api/guest-contacts", json={"name": "", "phone": ""}, headers=_auth_headers(token)
    )
    assert resp.status_code == 400


def test_guest_contacts_scoped_to_own_restaurant(client, db_path):
    from guest_marketing import init_guest_marketing
    init_guest_marketing(db_path)
    rid_a = _restaurant(db_path, name="Restaurant A", module_marketing=1)
    rid_b = _restaurant(db_path, name="Restaurant B", module_marketing=1)
    token_a = _login(client, db_path, rid_a, username="alice")
    token_b = _login(client, db_path, rid_b, username="bob")

    client.post(
        "/mobile/api/guest-contacts", json={"name": "A's guest", "phone": "312-555-0101"},
        headers=_auth_headers(token_a),
    )
    resp = client.get("/mobile/api/guest-contacts", headers=_auth_headers(token_b))
    assert resp.get_json()["contacts"] == []


def test_guest_campaign_draft_requires_marketing_module(client, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/guest-campaign/draft", json={}, headers=_auth_headers(token))
    assert resp.status_code == 403


def test_guest_campaign_draft_returns_message(client, db_path, monkeypatch):
    rid = _restaurant(db_path, module_marketing=1)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("guest_marketing.draft_campaign_message", lambda *a, **kw: "Come back soon!")

    resp = client.post(
        "/mobile/api/guest-campaign/draft", json={"type": "general"}, headers=_auth_headers(token)
    )
    data = resp.get_json()
    assert data["ok"] is True
    assert data["message"] == "Come back soon!"


def test_guest_campaign_send_requires_message(client, db_path):
    rid = _restaurant(db_path, module_marketing=1)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/guest-campaign/send", json={}, headers=_auth_headers(token))
    assert resp.status_code == 400


def test_guest_campaign_send_returns_ok(client, db_path, monkeypatch):
    rid = _restaurant(db_path, module_marketing=1)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("guest_marketing.send_campaign", lambda *a, **kw: {"sent": 0})

    resp = client.post(
        "/mobile/api/guest-campaign/send", json={"message": "Hi there"}, headers=_auth_headers(token)
    )
    data = resp.get_json()
    assert data["ok"] is True
    assert data["sent"] == 0


def test_guest_join_link_requires_marketing_module(client, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/guest-join-link", headers=_auth_headers(token))
    assert resp.status_code == 403


def test_guest_join_link_includes_restaurant_id(client, db_path):
    rid = _restaurant(db_path, module_marketing=1)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/guest-join-link", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert f"/join/{rid}" in data["join_url"]


# ── /mobile/api/intel ─────────────────────────────────────────────────────

def test_intel_endpoint_requires_auth(client):
    resp = client.get("/mobile/api/intel")
    assert resp.status_code == 401


def test_intel_endpoint_reports_no_data_when_competitor_intel_empty(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/intel", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["has_data"] is False


def test_intel_endpoint_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    update_restaurant(rid_a, {
        "competitor_intel": "WHAT COMPETITORS DO WELL:\n- Fast service\n\nRecommendations:\n- Speed up service"
    }, db_path=db_path)
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.get("/mobile/api/intel", headers=_auth_headers(token_b))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["has_data"] is False  # restaurant B has no competitor_intel of its own


# ── /mobile/api/changelog ────────────────────────────────────────────────────

def test_changelog_requires_auth(client):
    resp = client.get("/mobile/api/changelog")
    assert resp.status_code == 401


def test_changelog_returns_entries_and_marks_seen(client, db_path):
    from models import save_changelog_entry
    save_changelog_entry("New feature", "Some body text", db_path=db_path)
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/changelog", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["entries"]) == 1

    conn = get_conn(db_path)
    row = conn.execute("SELECT changelog_seen_at FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["changelog_seen_at"] is not None


def test_changelog_unread_count_requires_auth(client):
    resp = client.get("/mobile/api/changelog/unread-count")
    assert resp.status_code == 401


def test_changelog_unread_count_reflects_new_entries_since_last_seen(client, db_path):
    from models import save_changelog_entry
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    # Seen timestamp is set explicitly in the past (rather than via the route,
    # which stamps "now") so the new entry's published_at is unambiguously
    # after it — sqlite's datetime('now') only has 1-second resolution, so
    # stamping both within the same test run is a real flakiness risk.
    update_restaurant(rid, {"changelog_seen_at": "2000-01-01T00:00:00"}, db_path=db_path)
    resp1 = client.get("/mobile/api/changelog/unread-count", headers=_auth_headers(token))
    assert resp1.get_json()["count"] == 0

    save_changelog_entry("Another feature", "Body", db_path=db_path)
    resp2 = client.get("/mobile/api/changelog/unread-count", headers=_auth_headers(token))
    assert resp2.get_json()["count"] == 1


# ── /mobile/api/intel/ai-visibility ─────────────────────────────────────────

def test_ai_visibility_requires_auth(client):
    resp = client.get("/mobile/api/intel/ai-visibility")
    assert resp.status_code == 401


def test_ai_visibility_reports_not_found_for_missing_restaurant(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("client_api.get_restaurant", lambda *a, **kw: None)

    resp = client.get("/mobile/api/intel/ai-visibility", headers=_auth_headers(token))
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_ai_visibility_rate_limited(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("ai_utils.ai_rate_limited", lambda *a, **kw: True)

    resp = client.get("/mobile/api/intel/ai-visibility", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is False
    assert "too many" in data["error"].lower()


# ── /mobile/api/account ───────────────────────────────────────────────────

def test_account_endpoint_requires_auth(client):
    resp = client.get("/mobile/api/account")
    assert resp.status_code == 401


def test_account_endpoint_returns_profile_and_connections(client, db_path):
    rid = _restaurant(db_path, owner_name="Gia Mia", neighborhood="River North")
    token = _login(client, db_path, rid)
    update_restaurant(rid, {"gmb_refresh_token": "tok123", "two_fa_enabled": 1}, db_path=db_path)

    resp = client.get("/mobile/api/account", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["profile"]["owner_name"] == "Gia Mia"
    assert data["profile"]["neighborhood"] == "River North"
    assert data["account"]["two_fa_enabled"] is True
    assert data["connections"]["google_business"]["connected"] is True
    assert data["connections"]["instagram"]["connected"] is False


def test_account_endpoint_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A", owner_name="Owner A")
    rid_b = _restaurant(db_path, name="Restaurant B", owner_name="Owner B")
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.get("/mobile/api/account", headers=_auth_headers(token_b))
    data = resp.get_json()
    assert data["profile"]["owner_name"] == "Owner B"


# ── /mobile/api/account/sessions ──────────────────────────────────────────

def test_account_sessions_requires_auth(client):
    resp = client.get("/mobile/api/account/sessions")
    assert resp.status_code == 401


def test_account_sessions_marks_current_session_and_device_type(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/account/sessions", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["is_current"] is True
    assert data["sessions"][0]["device_type"] == "ios"
    assert data["sessions"][0]["label"] == "iPhone (Cavnar AI app)"


# ── /mobile/api/account/change-password ───────────────────────────────────

def test_change_password_requires_auth(client):
    resp = client.post("/mobile/api/account/change-password", json={})
    assert resp.status_code == 401


def test_change_password_rejects_wrong_current_password(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/change-password",
        json={"current": "wrong-password", "new_password": "brandnewpass123"},
        headers=_auth_headers(token),
    )
    data = resp.get_json()
    assert resp.status_code == 400
    assert data["ok"] is False


def test_change_password_rejects_short_new_password(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/change-password",
        json={"current": "correct-horse", "new_password": "short"},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 400


def test_change_password_succeeds_and_new_password_works_next_login(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/change-password",
        json={"current": "correct-horse", "new_password": "brandnewpass123"},
        headers=_auth_headers(token),
    )
    assert resp.get_json()["ok"] is True

    resp2 = client.post("/mobile/api/login", json={"username": "alice", "password": "brandnewpass123"})
    assert resp2.get_json()["ok"] is True


# ── /mobile/api/account/2fa ────────────────────────────────────────────────

def test_2fa_send_test_requires_owner_email(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"owner_email": ""}, db_path=db_path)
    token = _login(client, db_path, rid)

    resp = client.post("/mobile/api/account/2fa/send-test", headers=_auth_headers(token))
    assert resp.status_code == 400


def test_2fa_send_test_and_verify_enables_2fa(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"owner_email": "owner@x.com"}, db_path=db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("emails.send_2fa_code", lambda *a, **kw: None)

    resp = client.post("/mobile/api/account/2fa/send-test", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True

    code = _stored_2fa_code(db_path, rid)
    resp2 = client.post(
        "/mobile/api/account/2fa/verify", json={"code": code}, headers=_auth_headers(token)
    )
    assert resp2.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute("SELECT two_fa_enabled FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["two_fa_enabled"] == 1


def test_2fa_verify_rejects_wrong_code(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_code": "111111", "two_fa_expires": "2099-01-01 00:00:00"}, db_path=db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/2fa/verify", json={"code": "999999"}, headers=_auth_headers(token)
    )
    assert resp.status_code == 400


def test_2fa_disable_turns_off_2fa(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)

    resp = client.post("/mobile/api/account/2fa/disable", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute("SELECT two_fa_enabled FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["two_fa_enabled"] == 0


# ── /mobile/api/account/login-notify ──────────────────────────────────────

def test_toggle_login_notify(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/login-notify", json={"enabled": True}, headers=_auth_headers(token)
    )
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute("SELECT login_notify FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["login_notify"] == 1


# ── /mobile/api/account/alert-settings ────────────────────────────────────

def test_save_alert_settings_requires_auth(client):
    resp = client.post("/mobile/api/account/alert-settings", json={})
    assert resp.status_code == 401


def test_save_alert_settings_updates_flags_and_replaces_contacts(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/alert-settings",
        json={
            "alert_1star": True,
            "alert_labor_over": True,
            "digest_day": "friday",
            "digest_enabled": True,
            "contacts": [{"name": "Will", "phone": "312-555-0100"}],
        },
        headers=_auth_headers(token),
    )
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT alert_1star, alert_labor_over, digest_day FROM restaurants WHERE id=?", (rid,)
    ).fetchone()
    conn.close()
    assert row["alert_1star"] == 1
    assert row["alert_labor_over"] == 1
    assert row["digest_day"] == "friday"

    from notify import get_alert_contacts
    contacts = get_alert_contacts(rid, db_path=db_path)
    assert len(contacts) == 1
    assert contacts[0]["name"] == "Will"


def test_save_alert_settings_downgrades_sms_without_consent(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/alert-settings",
        json={"urgent_via_sms": True, "sms_consent": False},
        headers=_auth_headers(token),
    )
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute("SELECT urgent_via_sms FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["urgent_via_sms"] == 0


# ── /mobile/api/account/digest-day ─────────────────────────────────────────

def test_update_digest_day_rejects_invalid_day(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/digest-day", json={"day": "someday"}, headers=_auth_headers(token)
    )
    assert resp.status_code == 400


def test_update_digest_day_succeeds(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/account/digest-day", json={"day": "sunday"}, headers=_auth_headers(token)
    )
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute("SELECT digest_day FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["digest_day"] == "sunday"


# ── /mobile/api/account/billing ────────────────────────────────────────────

def test_billing_requires_auth(client):
    resp = client.get("/mobile/api/account/billing")
    assert resp.status_code == 401


def test_billing_reports_no_customer_when_unset(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/account/billing", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "no_customer"


# ── /mobile/api/reviews/<id>/delete ────────────────────────────────────────

def test_delete_review_requires_auth(client):
    resp = client.post("/mobile/api/reviews/1/delete")
    assert resp.status_code == 401


def test_delete_review_marks_deleted_at_and_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    review_id = _add_review(db_path, rid_a)
    token_b = _login(client, db_path, rid_b, username="bob")

    # Restaurant B can't delete Restaurant A's review — scoped UPDATE is a no-op.
    resp = client.post(f"/mobile/api/reviews/{review_id}/delete", headers=_auth_headers(token_b))
    assert resp.get_json()["ok"] is True
    conn = get_conn(db_path)
    row = conn.execute("SELECT deleted_at FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["deleted_at"] is None

    token_a = _login(client, db_path, rid_a, username="alice")
    resp2 = client.post(f"/mobile/api/reviews/{review_id}/delete", headers=_auth_headers(token_a))
    assert resp2.get_json()["ok"] is True
    conn = get_conn(db_path)
    row = conn.execute("SELECT deleted_at FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["deleted_at"] is not None


# ── /mobile/api/reviews/response-performance, topic-heatmap, sentiment-trend

def test_response_performance_requires_auth(client):
    resp = client.get("/mobile/api/reviews/response-performance")
    assert resp.status_code == 401


def test_response_performance_returns_ok(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/reviews/response-performance", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True


def test_topic_heatmap_requires_auth(client):
    resp = client.get("/mobile/api/reviews/topic-heatmap")
    assert resp.status_code == 401


def test_topic_heatmap_returns_ok(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/reviews/topic-heatmap", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True


def test_sentiment_trend_requires_auth(client):
    resp = client.get("/mobile/api/reviews/sentiment-trend")
    assert resp.status_code == 401


def test_sentiment_trend_returns_ok(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/reviews/sentiment-trend", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["weeks"] == []


def test_review_insight_requires_auth(client):
    resp = client.get("/mobile/api/reviews/insight")
    assert resp.status_code == 401


def test_review_insight_returns_cached_value(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("client_api._cache_get", lambda key: "Cached insight text.")

    resp = client.get("/mobile/api/reviews/insight", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["insight"] == "Cached insight text."


# ── /mobile/api/templates ──────────────────────────────────────────────────

def test_list_templates_requires_auth(client):
    resp = client.get("/mobile/api/templates")
    assert resp.status_code == 401


def test_create_list_delete_template_roundtrip(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.post(
        "/mobile/api/templates",
        json={"title": "Thanks!", "body": "Thanks for the kind words!", "category": "positive"},
        headers=_auth_headers(token),
    )
    data = resp.get_json()
    assert data["ok"] is True
    tid = data["id"]

    resp2 = client.get("/mobile/api/templates", headers=_auth_headers(token))
    templates = resp2.get_json()["templates"]
    assert len(templates) == 1
    assert templates[0]["title"] == "Thanks!"

    resp3 = client.post(f"/mobile/api/templates/{tid}/use", headers=_auth_headers(token))
    assert resp3.get_json()["ok"] is True

    resp4 = client.delete(f"/mobile/api/templates/{tid}", headers=_auth_headers(token))
    assert resp4.get_json()["ok"] is True
    resp5 = client.get("/mobile/api/templates", headers=_auth_headers(token))
    assert resp5.get_json()["templates"] == []


def test_create_template_rejects_missing_title(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post(
        "/mobile/api/templates", json={"title": "", "body": "Body"}, headers=_auth_headers(token)
    )
    assert resp.status_code == 400


def test_templates_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    token_a = _login(client, db_path, rid_a, username="alice")
    token_b = _login(client, db_path, rid_b, username="bob")

    client.post(
        "/mobile/api/templates", json={"title": "A's template", "body": "Body"},
        headers=_auth_headers(token_a),
    )
    resp = client.get("/mobile/api/templates", headers=_auth_headers(token_b))
    assert resp.get_json()["templates"] == []


# ── /mobile/api/send-review-request ────────────────────────────────────────

def test_send_review_request_requires_auth(client):
    resp = client.post("/mobile/api/send-review-request", json={})
    assert resp.status_code == 401


def test_send_review_request_requires_email_or_phone(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post(
        "/mobile/api/send-review-request", json={"name": "Jamie"}, headers=_auth_headers(token)
    )
    assert resp.status_code == 400


def test_send_review_request_sms_only_logs_request(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("notify.send_sms", lambda *a, **kw: True)

    resp = client.post(
        "/mobile/api/send-review-request",
        json={"name": "Jamie", "phone": "312-555-0100"},
        headers=_auth_headers(token),
    )
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT customer_phone, method FROM review_requests WHERE restaurant_id=?", (rid,)
    ).fetchone()
    conn.close()
    assert row["method"] == "sms"


def test_review_request_stats_requires_auth(client):
    resp = client.get("/mobile/api/review-request-stats")
    assert resp.status_code == 401


def test_review_request_stats_returns_zero_for_fresh_restaurant(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/review-request-stats", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["total_sent"] == 0
