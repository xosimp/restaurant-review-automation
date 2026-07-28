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
    for mod in (models, auth, auth_routes, client_api, mobile_api):
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
    assert data["modules"]["reviews"] is True
    assert "reviews" in data["kpis"]
    assert isinstance(data["needs_attention"], list)


def test_home_surfaces_awaiting_approval_in_needs_attention(client, db_path):
    rid = _restaurant(db_path)
    _add_review(db_path, rid)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/home", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["kpis"]["reviews"]["awaiting_approval"] == 1
    types = [item["type"] for item in data["needs_attention"]]
    assert "reviews_awaiting_approval" in types


def test_reviews_endpoint_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    _add_review(db_path, rid_a, external_id="only-a")
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.get("/mobile/api/reviews", headers=_auth_headers(token_b))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["reviews"] == []  # restaurant B sees none of restaurant A's reviews
