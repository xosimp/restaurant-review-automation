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
import value_delivered
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
    for mod in (models, auth, auth_routes, client_api, mobile_api, notify, guest_marketing, value_delivered):
        monkeypatch.setattr(mod, "get_conn", redirect)


@pytest.fixture(autouse=True)
def _init_auth_tables(db_path):
    init_auth(db_path=db_path)
    from models import init_two_fa_backup_codes
    init_two_fa_backup_codes(db_path=db_path)


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


# ── /me ───────────────────────────────────────────────────────────────────

def test_me_resolves_bearer_token_to_user(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid, username="alice")
    resp = client.get("/mobile/api/me", headers=_auth_headers(token))
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["user"]["username"] == "alice"
    assert "password_hash" not in data["user"]


# ── /apple-signin ────────────────────────────────────────────────────────

def test_apple_signin_matches_by_email_and_backfills_apple_user_id(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    monkeypatch.setattr(
        "mobile_api._verify_apple_identity_token",
        lambda token, bundle_id: {"sub": "apple-stable-id-1", "email": "alice@x.com"},
    )
    resp = client.post("/mobile/api/apple-signin", json={"identity_token": "fake"})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["user"]["username"] == "alice"

    conn = get_conn(db_path)
    row = conn.execute("SELECT apple_user_id FROM users WHERE username='alice'").fetchone()
    conn.close()
    assert row["apple_user_id"] == "apple-stable-id-1"


def test_apple_signin_matches_by_apple_user_id_without_email(client, db_path, monkeypatch):
    """Apple only sends the email on a user's very first Sign In with Apple
    ever — every login after that must still work from apple_user_id alone."""
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET apple_user_id=? WHERE username='alice'", ("apple-stable-id-1",))
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "mobile_api._verify_apple_identity_token",
        lambda token, bundle_id: {"sub": "apple-stable-id-1"},
    )
    resp = client.post("/mobile/api/apple-signin", json={"identity_token": "fake"})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["user"]["username"] == "alice"


def test_apple_signin_no_matching_account_returns_401(client, db_path, monkeypatch):
    monkeypatch.setattr(
        "mobile_api._verify_apple_identity_token",
        lambda token, bundle_id: {"sub": "apple-stable-id-unknown", "email": "nobody@x.com"},
    )
    resp = client.post("/mobile/api/apple-signin", json={"identity_token": "fake"})
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_apple_signin_verification_failure_returns_401(client, db_path, monkeypatch):
    def _raise(token, bundle_id):
        raise ValueError("bad signature")
    monkeypatch.setattr("mobile_api._verify_apple_identity_token", _raise)
    resp = client.post("/mobile/api/apple-signin", json={"identity_token": "fake"})
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_apple_signin_missing_token_returns_400(client, db_path):
    resp = client.post("/mobile/api/apple-signin", json={})
    assert resp.status_code == 400


def test_me_rejects_missing_token(client, db_path):
    resp = client.get("/mobile/api/me")
    assert resp.status_code == 401


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


def test_undo_reverts_a_skipped_review_to_drafted(client, db_path):
    rid = _restaurant(db_path)
    review_id = _add_review(db_path, rid)
    token = _login(client, db_path, rid)
    client.post(f"/mobile/api/reviews/{review_id}/skip", headers=_auth_headers(token))

    resp = client.post(f"/mobile/api/reviews/{review_id}/undo", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["response_status"] == "drafted"


def test_undo_reverts_an_unposted_approval_to_drafted(client, db_path):
    # is_connected() is False in this test DB (no GMB token configured), so
    # approve() here lands on response_status='approved', auto_posted=False —
    # exactly the "approved but never actually posted" case undo should cover.
    rid = _restaurant(db_path)
    review_id = _add_review(db_path, rid)
    token = _login(client, db_path, rid)
    approve_resp = client.post(f"/mobile/api/reviews/{review_id}/approve", headers=_auth_headers(token))
    assert approve_resp.get_json()["auto_posted"] is False

    resp = client.post(f"/mobile/api/reviews/{review_id}/undo", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["response_status"] == "drafted"


def test_undo_rejects_a_posted_review(client, db_path):
    rid = _restaurant(db_path)
    review_id = _add_review(db_path, rid)
    token = _login(client, db_path, rid)
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET response_status='posted' WHERE id=?", (review_id,))
    conn.commit(); conn.close()

    resp = client.post(f"/mobile/api/reviews/{review_id}/undo", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is False
    conn = get_conn(db_path)
    row = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["response_status"] == "posted"  # untouched


def test_undo_does_not_affect_another_restaurants_review(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    review_id = _add_review(db_path, rid_a)
    client_id_conn = get_conn(db_path)
    client_id_conn.execute("UPDATE reviews SET response_status='skipped' WHERE id=?", (review_id,))
    client_id_conn.commit(); client_id_conn.close()
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.post(f"/mobile/api/reviews/{review_id}/undo", headers=_auth_headers(token_b))
    assert resp.get_json()["ok"] is False  # scoped query finds nothing for restaurant B

    conn = get_conn(db_path)
    row = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["response_status"] == "skipped"  # unchanged: restaurant B couldn't touch A's review


def test_retract_rejects_a_review_that_was_never_posted(client, db_path):
    rid = _restaurant(db_path)
    review_id = _add_review(db_path, rid)
    token = _login(client, db_path, rid)

    resp = client.post(f"/mobile/api/reviews/{review_id}/retract", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is False


def test_retract_deletes_the_live_reply_then_reverts_to_drafted(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    review_id = _add_review(db_path, rid)
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE reviews SET response_status='posted', review_name=? WHERE id=?",
        ("accounts/1/locations/2/reviews/3", review_id)
    )
    conn.commit(); conn.close()
    token = _login(client, db_path, rid)

    import gmb
    monkeypatch.setattr(gmb, "delete_reply", lambda restaurant_id, review_name: {"ok": True})

    resp = client.post(f"/mobile/api/reviews/{review_id}/retract", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True

    conn = get_conn(db_path)
    row = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["response_status"] == "drafted"


def test_retract_surfaces_the_google_api_error_and_leaves_status_unchanged(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    review_id = _add_review(db_path, rid)
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE reviews SET response_status='posted', review_name=? WHERE id=?",
        ("accounts/1/locations/2/reviews/3", review_id)
    )
    conn.commit(); conn.close()
    token = _login(client, db_path, rid)

    import gmb
    monkeypatch.setattr(gmb, "delete_reply", lambda restaurant_id, review_name: {"ok": False, "error": "token expired"})

    resp = client.post(f"/mobile/api/reviews/{review_id}/retract", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is False
    assert data["error"] == "token expired"

    conn = get_conn(db_path)
    row = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    assert row["response_status"] == "posted"  # unchanged — the delete never actually succeeded


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
    # Analytics tab redesign fields — computed by analyse_inventory() all
    # along, previously just never forwarded to mobile.
    for key in (
        "critical_low", "reorder_soon", "order_reduction",
        "annual_recoverable", "total_waste_cost_week", "monthly_waste_projection",
        "annual_waste_projection", "waste_rate_pct", "benchmark_label",
        "benchmark_detail", "total_stock_value", "total_items",
        "week_start", "week_end", "last_updated",
    ):
        assert key in data, f"missing {key}"


# ── /mobile/api/food-cost/trend ─────────────────────────────────────────────

def test_food_cost_trend_requires_auth(client):
    resp = client.get("/mobile/api/food-cost/trend")
    assert resp.status_code == 401


def test_food_cost_trend_returns_empty_weeks_with_no_history(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/food-cost/trend", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["weeks"] == []


def test_food_cost_trend_only_returns_this_restaurants_history(client, db_path):
    # IDOR check — a second restaurant's inventory_history rows must never
    # leak into this restaurant's trend chart. Explicit get_conn(db_path),
    # not the bare module-level get_conn imported at the top of this file —
    # that reference was bound before _redirect_db's monkeypatch runs (it
    # captures the original, un-patched function at file-import time), so
    # calling it with no args here would hit the real default DB_PATH
    # instead of this test's isolated database.
    import json
    rid = _restaurant(db_path)
    other_rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    conn = get_conn(db_path)
    # inventory_history isn't part of init_db()'s base schema — it's
    # created lazily by inventory.py's own get_claude_insights() on first
    # real use, which this test never triggers, so it's created here
    # matching that exact schema.
    conn.execute("""CREATE TABLE IF NOT EXISTS inventory_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        waste_json TEXT,
        week_end    TEXT,
        items_json  TEXT,
        saved_at    TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute(
        "INSERT INTO inventory_history (restaurant_id, week_end, waste_json) VALUES (?,?,?)",
        (rid, "2026-08-13", json.dumps({"total_waste_cost": 210.5})),
    )
    conn.execute(
        "INSERT INTO inventory_history (restaurant_id, week_end, waste_json) VALUES (?,?,?)",
        (other_rid, "2026-08-13", json.dumps({"total_waste_cost": 999.0})),
    )
    conn.commit()
    conn.close()

    resp = client.get("/mobile/api/food-cost/trend", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["weeks"]) == 1
    assert data["weeks"][0]["waste"] == 210.5
    assert data["weeks"][0]["label"] == "8/13"


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
    assert data["reviews_awaiting_approval"] == 1


def test_home_returns_the_signed_in_username_for_the_hero_greeting(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid, username="jamie")
    resp = client.get("/mobile/api/home", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["username"] == "jamie"


def test_home_includes_total_value_delivered_and_records_a_snapshot(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/home", headers=_auth_headers(token))
    data = resp.get_json()
    assert isinstance(data["total_value_delivered"], int)
    assert isinstance(data["value_history"], list)

    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT total_value FROM value_snapshots WHERE restaurant_id=? AND snapshot_date = date('now')", (rid,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["total_value"] == data["total_value_delivered"]


def test_home_value_snapshot_upserts_not_duplicates_same_day(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    client.get("/mobile/api/home", headers=_auth_headers(token))
    client.get("/mobile/api/home", headers=_auth_headers(token))

    conn = get_conn(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM value_snapshots WHERE restaurant_id=? AND snapshot_date = date('now')", (rid,)
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_reviews_endpoint_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    _add_review(db_path, rid_a, external_id="only-a")
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.get("/mobile/api/reviews", headers=_auth_headers(token_b))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["reviews"] == []  # restaurant B sees none of restaurant A's reviews


def test_reviews_category_filter_returns_only_matching_reviews(client, db_path):
    rid = _restaurant(db_path)
    id_service = _add_review(db_path, rid, external_id="rev-service")
    id_food = _add_review(db_path, rid, external_id="rev-food")
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET categories=? WHERE id=?", ('["service"]', id_service))
    conn.execute("UPDATE reviews SET categories=? WHERE id=?", ('["food_quality"]', id_food))
    conn.commit()
    conn.close()
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/reviews", query_string={"category": "service"}, headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert [r["id"] for r in data["reviews"]] == [id_service]


def test_reviews_platform_filter_returns_only_matching_reviews(client, db_path):
    rid = _restaurant(db_path)
    id_google = _add_review(db_path, rid, external_id="rev-google")
    conn = get_conn(db_path)
    save_reviews([Review(restaurant_id=rid, platform="yelp", external_id="rev-yelp",
                          author="Bob", rating=4, text="Pretty good.")], db_path=db_path)
    conn.execute("UPDATE reviews SET processed=1 WHERE restaurant_id=? AND external_id='rev-yelp'", (rid,))
    conn.commit()
    conn.close()
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/reviews", query_string={"platform": "yelp"}, headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["reviews"]) == 1
    assert data["reviews"][0]["platform"] == "yelp"
    assert id_google not in [r["id"] for r in data["reviews"]]


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


def test_schedule_history_requires_auth(client):
    resp = client.get("/mobile/api/labor/schedule-history")
    assert resp.status_code == 401


def test_schedule_history_detail_requires_auth(client):
    resp = client.get("/mobile/api/labor/schedule-history/1")
    assert resp.status_code == 401


def test_schedule_history_lists_own_generations_newest_first(client, db_path):
    from models import save_schedule_history
    rid = _restaurant(db_path)
    save_schedule_history(rid, "2026-08-10", "2026-08-16", 1200.0, 1300.0, 23.0,
                           "date,day,employee\n2026-08-10,Monday,Jamie", ["Older week"], db_path=db_path)
    save_schedule_history(rid, "2026-08-17", "2026-08-23", 1250.0, 1300.0, 23.0,
                           "date,day,employee\n2026-08-17,Monday,Jamie", ["Newer week"], db_path=db_path)
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/labor/schedule-history", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["history"]) == 2
    assert data["history"][0]["week_start"] == "2026-08-17"  # newest first
    assert "schedule_csv" not in data["history"][0]  # list is summary-only


def test_schedule_history_detail_returns_parsed_preview_rows(client, db_path):
    from models import save_schedule_history
    rid = _restaurant(db_path)
    history_id = save_schedule_history(
        rid, "2026-08-17", "2026-08-23", 1250.0, 1300.0, 23.0,
        "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
        "2026-08-17,Monday,Jamie L.,Server,10:00am,4:00pm,6,opener",
        ["Balanced week"], db_path=db_path,
    )
    token = _login(client, db_path, rid)

    resp = client.get(f"/mobile/api/labor/schedule-history/{history_id}", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["summary"] == ["Balanced week"]
    assert data["preview_rows"][0]["employee"] == "Jamie L."
    assert data["preview_rows"][0]["role"] == "Server"


def test_schedule_history_detail_rejects_cross_tenant_id(client, db_path):
    from models import save_schedule_history
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    history_id = save_schedule_history(rid_a, "2026-08-17", "2026-08-23", 1250.0, 1300.0, 23.0,
                                        "date,day,employee\n2026-08-17,Monday,Jamie", [], db_path=db_path)
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.get(f"/mobile/api/labor/schedule-history/{history_id}", headers=_auth_headers(token_b))
    assert resp.status_code == 404


def test_schedule_history_delete_requires_auth(client):
    resp = client.delete("/mobile/api/labor/schedule-history/1")
    assert resp.status_code == 401


def test_schedule_history_delete_removes_own_entry(client, db_path):
    from models import save_schedule_history, get_schedule_history
    rid = _restaurant(db_path)
    history_id = save_schedule_history(rid, "2026-08-17", "2026-08-23", 1250.0, 1300.0, 23.0,
                                        "date,day,employee\n2026-08-17,Monday,Jamie", [], db_path=db_path)
    token = _login(client, db_path, rid)

    resp = client.delete(f"/mobile/api/labor/schedule-history/{history_id}", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True
    assert get_schedule_history(rid, db_path=db_path) == []


def test_schedule_history_delete_rejects_cross_tenant_id(client, db_path):
    from models import save_schedule_history, get_schedule_history
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    history_id = save_schedule_history(rid_a, "2026-08-17", "2026-08-23", 1250.0, 1300.0, 23.0,
                                        "date,day,employee\n2026-08-17,Monday,Jamie", [], db_path=db_path)
    token_b = _login(client, db_path, rid_b, username="bob")

    resp = client.delete(f"/mobile/api/labor/schedule-history/{history_id}", headers=_auth_headers(token_b))
    assert resp.status_code == 404
    # Restaurant A's entry must survive an attempted cross-tenant delete.
    assert len(get_schedule_history(rid_a, db_path=db_path)) == 1


def test_labor_endpoint_includes_new_analytics_fields(client, db_path):
    """Overview/Analytics tab parity fields — date_range, overstaffed/
    understaffed day lists, dow_summary (By Day chart), savings_breakdown
    (savings tiles), labor_upcoming (holiday forecast card) — all forwarded
    from analyse_shifts_for_restaurant()'s existing output instead of being
    silently dropped like before."""
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/labor", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["date_range"]["start"] and data["date_range"]["end"]
    assert isinstance(data["overstaffed_days"], list)
    assert isinstance(data["understaffed_days"], list)
    assert isinstance(data["dow_summary"], dict) and data["dow_summary"]
    assert isinstance(data["labor_upcoming"], list)
    breakdown = data["savings_breakdown"]
    for key in ("labor_monthly", "labor_annual", "labor_overtime",
                "labor_vs_industry_monthly", "labor_vs_industry_annual"):
        assert key in breakdown


def test_labor_overtime_entries_include_total_hours_and_default_not_ot_allowed(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/labor", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["overtime_risk"], "sample shift data should produce at least one overtime-risk entry"
    for entry in data["overtime_risk"]:
        assert "total_hours" in entry
        assert entry["ot_allowed"] is False  # no staff constraint saved yet


def test_labor_overtime_ot_allowed_true_when_staff_constraint_mentions_overtime(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/labor", headers=_auth_headers(token))
    flagged = next(e for e in resp.get_json()["overtime_risk"] if e["status"] == "overtime")

    from models import save_staff_note, init_staff_notes
    init_staff_notes(db_path=db_path)
    save_staff_note(rid, flagged["employee"], "Happy to pick up overtime shifts.", db_path=db_path)

    resp2 = client.get("/mobile/api/labor", headers=_auth_headers(token))
    re_flagged = next(e for e in resp2.get_json()["overtime_risk"] if e["employee"] == flagged["employee"])
    assert re_flagged["ot_allowed"] is True
    # An employee with no constraint note is unaffected by another's note.
    others = [e for e in resp2.get_json()["overtime_risk"] if e["employee"] != flagged["employee"]]
    assert all(e["ot_allowed"] is False for e in others)


# ── /mobile/api/labor/availability ──────────────────────────────────────────

def test_labor_availability_list_requires_auth(client):
    resp = client.get("/mobile/api/labor/availability")
    assert resp.status_code == 401


def test_labor_availability_save_requires_auth(client):
    resp = client.post("/mobile/api/labor/availability", json={"employee_name": "Sam"})
    assert resp.status_code == 401


def test_labor_availability_delete_requires_auth(client):
    resp = client.post("/mobile/api/labor/availability/delete", json={"employee_name": "Sam"})
    assert resp.status_code == 401


def test_labor_availability_save_list_delete_roundtrip(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    save_resp = client.post(
        "/mobile/api/labor/availability", headers=_auth_headers(token),
        json={"employee_name": "Jake M.", "available_days": ["Monday", "Tuesday"],
              "unavailable_days": ["Saturday"], "notes": "Student, no mornings"},
    )
    assert save_resp.get_json()["ok"] is True

    list_resp = client.get("/mobile/api/labor/availability", headers=_auth_headers(token))
    entries = list_resp.get_json()["availability"]
    assert len(entries) == 1
    assert entries[0]["employee_name"] == "Jake M."
    assert entries[0]["available_days"] == ["Monday", "Tuesday"]
    assert entries[0]["unavailable_days"] == ["Saturday"]
    assert entries[0]["notes"] == "Student, no mornings"

    del_resp = client.post(
        "/mobile/api/labor/availability/delete", headers=_auth_headers(token),
        json={"employee_name": "Jake M."},
    )
    assert del_resp.get_json()["ok"] is True
    assert client.get("/mobile/api/labor/availability", headers=_auth_headers(token)).get_json()["availability"] == []


def test_labor_availability_save_requires_employee_name(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/labor/availability", headers=_auth_headers(token), json={"employee_name": "  "})
    assert resp.get_json()["ok"] is False


def test_labor_availability_scoped_to_own_restaurant(client, db_path):
    rid_a = _restaurant(db_path, name="Restaurant A")
    rid_b = _restaurant(db_path, name="Restaurant B")
    token_a = _login(client, db_path, rid_a, username="alice")
    token_b = _login(client, db_path, rid_b, username="bob")

    client.post("/mobile/api/labor/availability", headers=_auth_headers(token_a),
                json={"employee_name": "Alice's Server", "available_days": ["Monday"]})

    resp_b = client.get("/mobile/api/labor/availability", headers=_auth_headers(token_b))
    assert resp_b.get_json()["availability"] == []  # never sees restaurant A's entry


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


def test_labor_insight_returns_structured_fields_for_ios_rendering(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    cached = (
        "Labor is running high this week.\n\n"
        "Recommendations:\n"
        "1. Trim Sunday's closing shift.\n"
        "2. Add a busser Saturday lunch.\n\n"
        "FORECAST: Should settle back to target next week."
    )
    monkeypatch.setattr("client_api._cache_get", lambda key: cached)

    resp = client.get("/mobile/api/labor/insight", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["insight_intro"] == "Labor is running high this week."
    assert data["insight_recommendations"] == [
        "Trim Sunday's closing shift.",
        "Add a busser Saturday lunch.",
    ]
    assert data["insight_forecast"] == "Should settle back to target next week."


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


def test_intel_endpoint_parses_real_json_blob_shape(client, db_path):
    """competitor_intel is stored as a JSON blob (run_competitor_analysis's
    {"competitors": [...], "insight": <narrative text>, ...}), not the raw
    narrative text on its own — passing the raw column straight into the
    text parser (the old bug) fed it an escaped JSON fragment and produced
    garbage intro/sections/recommendations for every restaurant with real
    data. This pins the fix: intro/sections/recommendations must reflect
    the *decoded* insight text, and the raw competitors list must pass
    through untouched."""
    import json
    rid = _restaurant(db_path)
    insight = (
        "Solid ambiance, slow on weekends.\n\n"
        "WHAT COMPETITORS ARE DOING WELL:\n- Fast table turnover\n\n"
        "WHAT COMPETITORS ARE DOING POORLY:\n- Inconsistent hours\n\n"
        "Recommendations:\n1. Speed up weekend service\n2. Post updated hours\n"
    )
    blob = json.dumps({
        "competitors": [{
            "name": "Mio Modo", "rating": 4.5, "review_count": 270,
            "vicinity": "123 Main St", "reviews": [{"author": "Sam", "rating": 5, "text": "Great!", "time": "a week ago"}],
        }],
        "insight": insight,
        "generated_at": "2026-08-01",
    })
    update_restaurant(rid, {"competitor_intel": blob, "competitor_updated_at": "2026-08-01 12:00:00"}, db_path=db_path)
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/intel", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["has_data"] is True
    assert data["intro"] == "Solid ambiance, slow on weekends."
    assert data["sections"] == [
        {"name": "What competitors are doing well", "bullets": ["Fast table turnover"]},
        {"name": "What competitors are doing poorly", "bullets": ["Inconsistent hours"]},
    ]
    assert data["recommendations"] == ["Speed up weekend service", "Post updated hours"]
    # place_id/custom weren't in this fixture's blob — asserting the
    # endpoint's own documented defaults ("" / False) for a competitor
    # entry that predates those two fields, same as a restaurant whose
    # competitor_intel was generated before this change would have.
    assert data["competitors"] == [{
        "name": "Mio Modo", "rating": 4.5, "review_count": 270, "vicinity": "123 Main St",
        "reviews": [{"author": "Sam", "rating": 5, "text": "Great!", "time": "a week ago"}],
        "place_id": "", "custom": False,
    }]
    assert data["updated_at"] == "2026-08-01 12:00:00"


def test_refresh_competitors_requires_full_tier(client, db_path):
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, module_inventory=1, module_marketing=1)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/intel/refresh-competitors", headers=_auth_headers(token))
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_refresh_competitors_starts_job_and_status_reports_pending_then_done(client, db_path, monkeypatch):
    rid = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    token = _login(client, db_path, rid)

    # _run_competitor_job does `from competitor import run_competitor_analysis`
    # lazily inside its own body (same lazy-import-for-testability convention
    # as models.get_conn elsewhere), so the patch target is competitor's own
    # module, not admin_routes's namespace.
    import competitor
    monkeypatch.setattr(competitor, "run_competitor_analysis",
                         lambda restaurant_id: {"ok": True, "competitors": [], "insight": "done", "generated_at": "2026-08-01"})

    resp = client.post("/mobile/api/intel/refresh-competitors", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    job_id = data["job_id"]

    # _run_competitor_job runs on a background thread — poll until it lands
    # rather than assuming it's already done the instant the route returns.
    import time
    status_data = None
    for _ in range(50):
        status_resp = client.get(f"/mobile/api/intel/refresh-status/{job_id}", headers=_auth_headers(token))
        status_data = status_resp.get_json()
        if status_data.get("status") != "pending":
            break
        time.sleep(0.05)
    assert status_data["status"] == "done"
    assert status_data["ok"] is True
    assert status_data["insight"] == "done"


def test_refresh_status_reports_not_found_for_unknown_job(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/intel/refresh-status/nonexistent-job-id", headers=_auth_headers(token))
    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


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


# ── /mobile/api/notifications ────────────────────────────────────────────

def test_notifications_returns_module_and_review_id_and_marks_seen(client, db_path):
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO alert_log (restaurant_id, alert_type, review_id) VALUES (?,?,?)",
        (rid, "1star", 42),
    )
    conn.execute(
        "INSERT INTO alert_log (restaurant_id, alert_type) VALUES (?,?)",
        (rid, "labor_over"),
    )
    conn.commit()
    conn.close()
    token = _login(client, db_path, rid)

    resp = client.get("/mobile/api/notifications", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    by_type = {item["type"]: item for item in data["notifications"]}
    assert by_type["1star"]["review_id"] == 42
    assert by_type["1star"]["module"] == "reviews"
    assert by_type["labor_over"]["review_id"] is None
    assert by_type["labor_over"]["module"] == "labor"

    conn = get_conn(db_path)
    row = conn.execute("SELECT notifications_seen_at FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["notifications_seen_at"] is not None


def test_notifications_unread_count_requires_auth(client):
    resp = client.get("/mobile/api/notifications/unread-count")
    assert resp.status_code == 401


def test_notifications_unread_count_reflects_new_alerts_since_last_seen(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)

    # Explicit past timestamp, not the route's own "now" stamp — sqlite's
    # datetime('now') only has 1-second resolution, so stamping both the
    # seen-at and the new alert_log row within the same test run risks a
    # tie (see the analogous changelog test above).
    update_restaurant(rid, {"notifications_seen_at": "2000-01-01T00:00:00"}, db_path=db_path)
    resp1 = client.get("/mobile/api/notifications/unread-count", headers=_auth_headers(token))
    assert resp1.get_json()["count"] == 0

    conn = get_conn(db_path)
    conn.execute("INSERT INTO alert_log (restaurant_id, alert_type) VALUES (?,?)", (rid, "5star"))
    conn.commit()
    conn.close()
    resp2 = client.get("/mobile/api/notifications/unread-count", headers=_auth_headers(token))
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


def test_2fa_send_test_surfaces_a_failed_email_send(client, db_path, monkeypatch):
    # send_2fa_code swallows its own failures (missing RESEND_API_KEY, a
    # non-200 from Resend) and just returns False rather than raising — the
    # route must check that return value, not assume ok=True the moment
    # send_2fa_code was merely CALLED without an exception.
    rid = _restaurant(db_path)
    update_restaurant(rid, {"owner_email": "owner@x.com"}, db_path=db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("emails.send_2fa_code", lambda *a, **kw: False)

    resp = client.post("/mobile/api/account/2fa/send-test", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is False
    assert resp.status_code != 200


def test_2fa_send_test_and_verify_enables_2fa(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"owner_email": "owner@x.com"}, db_path=db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("emails.send_2fa_code", lambda *a, **kw: True)

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

def test_platform_breakdown_requires_auth(client):
    resp = client.get("/mobile/api/reviews/platform-breakdown")
    assert resp.status_code == 401


def test_platform_breakdown_returns_ok(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/reviews/platform-breakdown", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["data"] == []


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


def test_send_review_request_includes_guest_note_in_sms(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    sent = {}
    monkeypatch.setattr(
        "notify.send_sms",
        lambda phone, text: sent.update(phone=phone, text=text) or True
    )

    resp = client.post(
        "/mobile/api/send-review-request",
        json={"name": "Jamie", "phone": "312-555-0100", "message": "Loved having you for the anniversary!"},
        headers=_auth_headers(token),
    )
    assert resp.get_json()["ok"] is True
    assert "Loved having you for the anniversary!" in sent["text"]


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


# ── /mobile/api/account timezone + marketing opt-out ───────────────────────

def test_account_profile_includes_timezone(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/account", headers=_auth_headers(token))
    assert resp.get_json()["profile"]["timezone"] == "America/Chicago"


def test_update_profile_accepts_valid_timezone(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post(
        "/mobile/api/account/update-profile", json={"timezone": "America/Los_Angeles"},
        headers=_auth_headers(token),
    )
    assert resp.get_json()["ok"] is True
    conn = get_conn(db_path)
    row = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["timezone"] == "America/Los_Angeles"


def test_update_profile_rejects_unknown_timezone(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post(
        "/mobile/api/account/update-profile", json={"timezone": "Mars/Colony1"},
        headers=_auth_headers(token),
    )
    assert resp.get_json()["ok"] is True  # route never fails outright — just ignores the bad value
    conn = get_conn(db_path)
    row = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["timezone"] == "America/Chicago"  # unchanged from the default


def test_marketing_opt_out_toggle(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post(
        "/mobile/api/account/marketing-opt-out", json={"opted_out": True}, headers=_auth_headers(token)
    )
    assert resp.get_json()["ok"] is True
    conn = get_conn(db_path)
    row = conn.execute("SELECT marketing_emails_opt_out FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["marketing_emails_opt_out"] == 1


# ── /mobile/api/account/login-history ───────────────────────────────────────

def test_login_history_route_requires_auth(client):
    resp = client.get("/mobile/api/account/login-history")
    assert resp.status_code == 401


def test_login_history_route_returns_history(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/account/login-history", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["history"]) == 1
    assert data["history"][0]["event"] == "login"
    assert data["history"][0]["label"]


# ── /mobile/api/account/export-data ─────────────────────────────────────────

def test_export_data_route_emails_four_column_csv(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    _add_review(db_path, rid)
    sent = {}

    class FakeEmails:
        @staticmethod
        def send(payload):
            sent.update(payload)
            return {"id": "fake"}

    monkeypatch.setattr("resend.Emails", FakeEmails)
    resp = client.post("/mobile/api/account/export-data", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["email"] == "alice@x.com"
    assert sent["attachments"][0]["filename"].endswith("_reviews.csv")
    import base64
    csv_text = base64.b64decode(sent["attachments"][0]["content"]).decode("utf-8")
    assert csv_text.splitlines()[0] == "Date,Rating,Review,Response Status"
    assert "Slow service." in csv_text


def test_export_data_route_requires_auth(client):
    resp = client.post("/mobile/api/account/export-data")
    assert resp.status_code == 401


# ── /mobile/api/account/send-test-digest ────────────────────────────────────

def test_send_test_digest_scoped_to_own_restaurant(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    sent = {}

    class FakeEmails:
        @staticmethod
        def send(payload):
            sent.update(payload)
            return {"id": "fake"}

    monkeypatch.setattr("resend.Emails", FakeEmails)
    resp = client.post("/mobile/api/account/send-test-digest", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    assert data["email"] == "alice@x.com"
    assert sent["to"] == ["alice@x.com"]


# ── 2FA backup codes ─────────────────────────────────────────────────────────

def test_2fa_setup_returns_backup_codes_once(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"owner_email": "owner@x.com"}, db_path=db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("emails.send_2fa_code", lambda *a, **kw: True)

    client.post("/mobile/api/account/2fa/send-test", headers=_auth_headers(token))
    code = _stored_2fa_code(db_path, rid)
    resp = client.post(
        "/mobile/api/account/2fa/verify", json={"code": code}, headers=_auth_headers(token)
    )
    data = resp.get_json()
    assert data["ok"] is True
    assert len(data["backup_codes"]) == 10


def test_backup_codes_status_route(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    from models import generate_backup_codes
    generate_backup_codes(rid, count=7, db_path=db_path)
    resp = client.get("/mobile/api/account/2fa/backup-codes", headers=_auth_headers(token))
    assert resp.get_json()["remaining"] == 7


def test_regenerate_backup_codes_route_invalidates_old_ones(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    from models import generate_backup_codes
    old_codes = generate_backup_codes(rid, count=5, db_path=db_path)
    resp = client.post("/mobile/api/account/2fa/backup-codes", headers=_auth_headers(token))
    data = resp.get_json()
    assert data["ok"] is True
    new_codes = data["backup_codes"]
    assert len(new_codes) == 10
    assert set(new_codes).isdisjoint(old_codes)


def test_2fa_login_falls_back_to_backup_code_when_otp_wrong(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    from models import generate_backup_codes
    codes = generate_backup_codes(rid, count=5, db_path=db_path)

    login_resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    pending_token = login_resp.get_json()["pending_token"]

    resp = client.post("/mobile/api/verify-2fa", json={"pending_token": pending_token, "code": codes[0]})
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["token"]


def test_2fa_login_backup_code_is_single_use(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    from models import generate_backup_codes
    codes = generate_backup_codes(rid, count=5, db_path=db_path)

    login_resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    pending_token = login_resp.get_json()["pending_token"]
    client.post("/mobile/api/verify-2fa", json={"pending_token": pending_token, "code": codes[0]})

    login_resp2 = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    pending_token2 = login_resp2.get_json()["pending_token"]
    resp2 = client.post("/mobile/api/verify-2fa", json={"pending_token": pending_token2, "code": codes[0]})
    assert resp2.get_json()["ok"] is False


# ── team invite / manage access routes ──────────────────────────────────────

def test_team_invite_route_rejects_invited_member(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid, role="member")
    resp = client.post(
        "/mobile/api/account/team/invite", json={"name": "New", "email": "new@x.com"},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False


def test_team_invite_route_allows_default_client_login(client, db_path, monkeypatch):
    # The real-world case: every restaurant's primary login has the default
    # role 'client' (nobody is role='owner' — that's Will's multi-restaurant
    # login). Gating on 'owner' hid the whole feature from every real account.
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)  # role left at the column default
    monkeypatch.setattr("emails.send_team_invite_email", lambda *a, **kw: None)
    resp = client.post(
        "/mobile/api/account/team/invite", json={"name": "New", "email": "new@x.com"},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    members = client.get("/mobile/api/account/team", headers=_auth_headers(token)).get_json()["members"]
    invited = next(m for m in members if m["email"] == "new@x.com")
    assert invited["role"] == "member"


def test_invited_member_cannot_invite_others(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    owner_token = _login(client, db_path, rid)
    monkeypatch.setattr("emails.send_team_invite_email", lambda *a, **kw: None)
    from auth import invite_team_member
    result = invite_team_member(rid, "Team Mate", "mate@x.com", db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": result["username"], "password": result["temp_password"]})
    member_token = resp.get_json()["token"]
    resp = client.post(
        "/mobile/api/account/team/invite", json={"name": "Another", "email": "another@x.com"},
        headers=_auth_headers(member_token),
    )
    assert resp.status_code == 403
    # ...while the login that invited them still can.
    resp = client.post(
        "/mobile/api/account/team/invite", json={"name": "Another", "email": "another@x.com"},
        headers=_auth_headers(owner_token),
    )
    assert resp.status_code == 200


def test_team_invite_route_creates_user(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid, role="owner")
    monkeypatch.setattr("emails.send_team_invite_email", lambda *a, **kw: None)

    resp = client.post(
        "/mobile/api/account/team/invite", json={"name": "New Teammate", "email": "teammate@x.com"},
        headers=_auth_headers(token),
    )
    data = resp.get_json()
    assert data["ok"] is True
    assert data["username"]

    resp2 = client.get("/mobile/api/account/team", headers=_auth_headers(token))
    members = resp2.get_json()["members"]
    assert len(members) == 2
    assert any(m["email"] == "teammate@x.com" for m in members)


def test_team_invite_route_rejects_duplicate_email(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid, role="owner")
    monkeypatch.setattr("emails.send_team_invite_email", lambda *a, **kw: None)
    client.post(
        "/mobile/api/account/team/invite", json={"name": "First", "email": "dup@x.com"},
        headers=_auth_headers(token),
    )
    resp = client.post(
        "/mobile/api/account/team/invite", json={"name": "Second", "email": "dup@x.com"},
        headers=_auth_headers(token),
    )
    assert resp.get_json()["ok"] is False
    assert resp.status_code == 400


def test_team_revoke_route_rejects_invited_member(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid, role="member")
    resp = client.post("/mobile/api/account/team/999/revoke", headers=_auth_headers(token))
    assert resp.status_code == 403


def test_team_revoke_route_blocks_self_revoke(client, db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    set_user_role(uid, "owner", db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    token = resp.get_json()["token"]

    resp2 = client.post(f"/mobile/api/account/team/{uid}/revoke", headers=_auth_headers(token))
    assert resp2.get_json()["ok"] is False


def test_team_revoke_route_removes_teammate(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid, role="owner")
    monkeypatch.setattr("emails.send_team_invite_email", lambda *a, **kw: None)
    invite_resp = client.post(
        "/mobile/api/account/team/invite", json={"name": "New", "email": "teammate@x.com"},
        headers=_auth_headers(token),
    )
    member_id = invite_resp.get_json()["user_id"]

    resp = client.post(f"/mobile/api/account/team/{member_id}/revoke", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True

    members = client.get("/mobile/api/account/team", headers=_auth_headers(token)).get_json()["members"]
    assert len(members) == 1


# ── settings audit additions ────────────────────────────────────────────────

def _set_status(db_path, review_id, status):
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET response_status=? WHERE id=?", (status, review_id))
    conn.commit(); conn.close()


def test_account_payload_includes_new_blocks(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    data = client.get("/mobile/api/account", headers=_auth_headers(token)).get_json()
    assert data["reviews"] == {"auto_approve_5star": False, "auto_approve_daily_cap": 5,
                               "auto_approve_paused": False, "auto_approved_today": 0}
    assert data["data"] == {"data_retention_months": 0}
    s = data["alerts"]["settings"]
    assert s["alert_health_bypass_quiet"] is False and s["alert_food_waste"] is False
    assert s["alert_ai_visibility_drop"] is False and s["push_sound"] is True and s["alert_extra_emails"] == ""
    assert data["profile"]["sign_off_name"] is None and data["account"]["recovery_email"] is None


def test_alert_settings_persist_new_fields_and_clean_extra_emails(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/account/alert-settings", headers=_auth_headers(token), json={
        "alert_health": True, "alert_health_bypass_quiet": True, "alert_food_waste": True,
        "alert_ai_visibility_drop": True, "push_sound": False,
        "alert_extra_emails": "Chef@x.com, chef@x.com not-an-email  gm@x.com\nextra@x.com fourth@x.com",
        "digest_day": "friday",
    })
    assert resp.get_json()["ok"] is True
    s = client.get("/mobile/api/account", headers=_auth_headers(token)).get_json()["alerts"]["settings"]
    assert s["alert_health_bypass_quiet"] is True and s["alert_food_waste"] is True
    assert s["alert_ai_visibility_drop"] is True and s["push_sound"] is False
    assert s["alert_extra_emails"] == "chef@x.com,gm@x.com,extra@x.com"   # de-duped, capped at 3
    from notify import alert_recipients, health_bypasses_quiet_hours
    assert alert_recipients("m@x.com", rid, db_path=db_path) == ["m@x.com", "chef@x.com", "gm@x.com", "extra@x.com"]
    assert health_bypasses_quiet_hours(rid, db_path=db_path) is True


def test_auto_approve_route_and_scheduler_rule(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/account/auto-approve", headers=_auth_headers(token),
                       json={"enabled": True, "daily_cap": 1, "paused": False})
    assert resp.get_json()["ok"] is True
    # Two drafted 5-stars, one drafted 4-star, one pending 5-star.
    r1 = _add_review(db_path, rid, external_id="a", draft_response="Thank you!")
    r2 = _add_review(db_path, rid, external_id="b", draft_response="Thank you!")
    for r in (r1, r2):
        _set_status(db_path, r, "drafted")
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET rating=5 WHERE id IN (?,?)", (r1, r2))
    conn.commit(); conn.close()
    import scheduler
    from models import get_restaurant
    approved = scheduler.auto_approve_five_stars(rid, get_restaurant(rid, db_path))
    assert approved == 1    # cap of 1
    conn = get_conn(db_path)
    statuses = {row["id"]: row["response_status"] for row in conn.execute("SELECT id, response_status FROM reviews WHERE restaurant_id=?", (rid,))}
    conn.close()
    assert sorted(statuses.values()) == ["approved", "drafted"]
    data = client.get("/mobile/api/account", headers=_auth_headers(token)).get_json()
    assert data["reviews"]["auto_approved_today"] == 1
    # Kill switch
    client.post("/mobile/api/account/auto-approve", headers=_auth_headers(token),
                json={"enabled": True, "daily_cap": 10, "paused": True})
    assert scheduler.auto_approve_five_stars(rid, get_restaurant(rid, db_path)) == 0


def test_hours_route_persists_and_ignores_junk(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/account/hours", headers=_auth_headers(token), json={
        "open": {"Monday": "11:00am", "Funday": "9am", "Tuesday": ""},
        "close": {"Monday": "10:00pm"},
        "closures": ["2026-12-25", "2026-12-25", "  ", "2026-01-01"],
    })
    assert resp.get_json()["ok"] is True
    p = client.get("/mobile/api/account", headers=_auth_headers(token)).get_json()["profile"]
    assert p["open_times_json"] == '{"Monday": "11:00am"}'
    assert p["close_times_json"] == '{"Monday": "10:00pm"}'
    assert p["skip_holidays"] == "2026-01-01,2026-12-25"


def test_data_retention_route_and_purge(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    assert client.post("/mobile/api/account/data-retention", headers=_auth_headers(token), json={"months": 7}).status_code == 400
    assert client.post("/mobile/api/account/data-retention", headers=_auth_headers(token), json={"months": 6}).get_json()["ok"] is True
    old = _add_review(db_path, rid, external_id="old")
    new = _add_review(db_path, rid, external_id="new")
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET review_date='2020-01-01', fetched_at='2020-01-01' WHERE id=?", (old,))
    conn.commit(); conn.close()
    from models import purge_expired_reviews
    assert purge_expired_reviews(db_path=db_path) == 1
    conn = get_conn(db_path)
    rows = {r["id"]: r["deleted_at"] for r in conn.execute("SELECT id, deleted_at FROM reviews WHERE restaurant_id=?", (rid,))}
    conn.close()
    assert rows[old] is not None and rows[new] is None
    assert client.get("/mobile/api/account", headers=_auth_headers(token)).get_json()["data"]["data_retention_months"] == 6


def test_report_bug_route(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    sent = {}
    monkeypatch.setattr("emails.send_bug_report_email", lambda name, frm, msg, meta: sent.update(name=name, msg=msg, meta=meta) or True)
    assert client.post("/mobile/api/account/report-bug", headers=_auth_headers(token), json={"message": "hi"}).status_code == 400
    resp = client.post("/mobile/api/account/report-bug", headers=_auth_headers(token),
                       json={"message": "The chart is blank on Labor", "build": "abc123+", "device": "iPhone 15"})
    assert resp.get_json()["ok"] is True
    assert sent["msg"] == "The chart is blank on Labor" and sent["meta"]["build"] == "abc123+" and sent["meta"]["username"] == "alice"


def test_recovery_email_flow(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    captured = {}
    monkeypatch.setattr("emails.send_recovery_email_code", lambda email, code: captured.update(email=email, code=code) or True)
    assert client.post("/mobile/api/account/recovery-email", headers=_auth_headers(token), json={"email": "alice@x.com"}).status_code == 400
    resp = client.post("/mobile/api/account/recovery-email", headers=_auth_headers(token), json={"email": "Backup@X.com"})
    assert resp.get_json()["pending"] == "backup@x.com" and captured["email"] == "backup@x.com"
    acct = client.get("/mobile/api/account", headers=_auth_headers(token)).get_json()["account"]
    assert acct["recovery_email"] is None and acct["recovery_email_pending"] == "backup@x.com"
    assert client.post("/mobile/api/account/recovery-email/verify", headers=_auth_headers(token), json={"code": "000000"}).status_code == 400
    ok = client.post("/mobile/api/account/recovery-email/verify", headers=_auth_headers(token), json={"code": captured["code"]}).get_json()
    assert ok["recovery_email"] == "backup@x.com"
    acct = client.get("/mobile/api/account", headers=_auth_headers(token)).get_json()["account"]
    assert acct["recovery_email"] == "backup@x.com" and acct["recovery_email_pending"] is None
    assert client.post("/mobile/api/account/recovery-email/remove", headers=_auth_headers(token)).get_json()["ok"] is True
    assert client.get("/mobile/api/account", headers=_auth_headers(token)).get_json()["account"]["recovery_email"] is None
    events = client.get("/mobile/api/account/activity", headers=_auth_headers(token)).get_json()["events"]
    assert [e["type"] for e in events][:2] == ["recovery_email_removed", "recovery_email_set"]


def test_trusted_devices_list_revoke_and_login_honours_table(client, db_path):
    from auth import create_trusted_device, trusted_device_ok, get_trusted_devices
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    tok_a = create_trusted_device(rid, None, "iPhone · app", db_path=db_path)
    tok_b = create_trusted_device(rid, None, "Mac · web", db_path=db_path)
    assert trusted_device_ok(rid, tok_a, db_path=db_path) and trusted_device_ok(rid, tok_b, db_path=db_path)
    assert not trusted_device_ok(rid, "nope", db_path=db_path)
    devices = client.get("/mobile/api/account/2fa/trusted-devices", headers=_auth_headers(token)).get_json()["devices"]
    assert [d["label"] for d in devices] == ["Mac · web", "iPhone · app"]
    assert client.post(f"/mobile/api/account/2fa/trusted-devices/{devices[1]['id']}/revoke", headers=_auth_headers(token)).get_json()["ok"] is True
    assert not trusted_device_ok(rid, tok_a, db_path=db_path) and trusted_device_ok(rid, tok_b, db_path=db_path)
    # Legacy single-slot token still honoured until revoke-all clears it.
    update_restaurant(rid, {"two_fa_device_token": "legacy-token"}, db_path=db_path)
    assert trusted_device_ok(rid, "legacy-token", db_path=db_path)
    assert client.post("/mobile/api/account/2fa/trusted-devices/revoke-all", headers=_auth_headers(token)).get_json()["ok"] is True
    assert get_trusted_devices(rid, db_path=db_path) == []
    assert not trusted_device_ok(rid, tok_b, db_path=db_path) and not trusted_device_ok(rid, "legacy-token", db_path=db_path)


def test_login_report_revokes_everything_and_blocks_login(client, db_path):
    from auth import create_login_report, consume_login_report, create_trusted_device, get_trusted_devices, clear_must_reset_password
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    uid = client.get("/mobile/api/me", headers=_auth_headers(token)).get_json()["user"]["id"]
    create_trusted_device(rid, uid, "iPhone · app", db_path=db_path)
    report = create_login_report(uid, token, db_path=db_path)
    user = consume_login_report(report, db_path=db_path)
    assert user["id"] == uid
    assert consume_login_report(report, db_path=db_path) is None          # one use
    assert client.get("/mobile/api/account", headers=_auth_headers(token)).status_code == 401   # session gone
    assert get_trusted_devices(rid, db_path=db_path) == []
    resp = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"})
    assert resp.status_code == 403 and resp.get_json()["password_reset_required"] is True
    clear_must_reset_password(uid, db_path=db_path)
    assert client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"}).get_json()["ok"] is True


def test_export_scopes(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    _add_review(db_path, rid)
    sent = {}
    class _Emails:
        @staticmethod
        def send(payload): sent.update(payload)
    import resend
    monkeypatch.setattr(resend, "Emails", _Emails)
    monkeypatch.setattr(mobile_api, "_resend_key", lambda: "k")
    assert client.post("/mobile/api/account/export-data", headers=_auth_headers(token), json={"scopes": ["bogus"]}).status_code == 400
    resp = client.post("/mobile/api/account/export-data", headers=_auth_headers(token), json={"scopes": ["reviews", "settings", "labor", "food_cost"]})
    assert resp.get_json()["scopes"] == ["reviews", "settings", "labor", "food_cost"]
    names = [a["filename"] for a in sent["attachments"]]
    assert names == ["Mobile Test Co_reviews.csv", "Mobile Test Co_settings.json", "Mobile Test Co_labor.csv", "Mobile Test Co_food_cost.csv"]
    import base64, json as _json
    settings = _json.loads(base64.b64decode(sent["attachments"][1]["content"]))
    assert settings["name"] == "Mobile Test Co" and "two_fa_code" not in settings
    events = client.get("/mobile/api/account/activity", headers=_auth_headers(token)).get_json()["events"]
    assert events[0]["type"] == "data_exported" and events[0]["detail"] == "reviews, settings, labor, food_cost"


def test_activity_log_records_account_events(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    client.post("/mobile/api/account/login-notify", headers=_auth_headers(token), json={"enabled": True})
    client.post("/mobile/api/account/change-password", headers=_auth_headers(token), json={"current": "correct-horse", "new_password": "new-horse-99"})
    client.post("/mobile/api/sessions/revoke-others", headers=_auth_headers(token))
    events = client.get("/mobile/api/account/activity", headers=_auth_headers(token)).get_json()["events"]
    assert [e["type"] for e in events][:3] == ["sessions_revoked_others", "password_changed", "login_notify_changed"]
    assert events[2]["detail"] == "on" and events[0]["label"] == "Signed out of other devices" and events[0]["actor"] == "alice"


def test_ai_visibility_drop_alert(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    client.post("/mobile/api/account/alert-settings", headers=_auth_headers(token),
                json={"alert_ai_visibility_drop": True, "urgent_via_email": True, "digest_day": "monday"})
    from models import record_ai_visibility_run
    emails_sent = []
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **kw: emails_sent.append(a[1]) or True)
    monkeypatch.setattr("push.fire_push", lambda *a, **kw: None)
    record_ai_visibility_run(rid, 80, db_path=db_path)
    record_ai_visibility_run(rid, 75, db_path=db_path)
    notify.check_extra_daily_alerts(db_path=db_path)
    assert emails_sent == []                       # a 5-point dip isn't a drop
    record_ai_visibility_run(rid, 40, db_path=db_path)
    notify.check_extra_daily_alerts(db_path=db_path)
    assert emails_sent == ["AI visibility dropped — Mobile Test Co"]
    notify.check_extra_daily_alerts(db_path=db_path)
    assert len(emails_sent) == 1                   # 7-day repeat window



# ── analytics chart feeds ───────────────────────────────────────────────────

def test_topic_weeks_buckets_by_week_and_category(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    rows = [
        ("a", "positive", '["service","food_quality"]', 0),
        ("b", "negative", '["service"]', 0),
        ("c", "positive", '["food_quality"]', 10),
        ("d", "positive", '["ambience"]', 200),   # outside the 8-week window
    ]
    for ext, sent, cats, age in rows:
        save_reviews([Review(restaurant_id=rid, platform="google", external_id=ext, author="A", rating=4, text="x")], db_path=db_path)
        conn = get_conn(db_path)   # a fresh connection per write — holding one across save_reviews() locks the file
        conn.execute("""UPDATE reviews SET processed=1, sentiment=?, categories=?,
                        review_date=date('now', ?) WHERE restaurant_id=? AND external_id=?""",
                     (sent, cats, f"-{age} days", rid, ext))
        conn.commit(); conn.close()
    data = client.get("/mobile/api/reviews/topic-weeks", headers=_auth_headers(token)).get_json()["data"]
    assert len(data["week_labels"]) == 8 and all("/" in l for l in data["week_labels"])   # "8/26"-style Monday dates
    by = {t["category"]: t for t in data["topics"]}
    assert set(by) == {"service", "food_quality"}          # ambience is out of window
    assert by["service"]["total"] == 2 and by["food_quality"]["total"] == 2
    assert by["service"]["weeks"][-1] == {"positive": 1, "negative": 1, "total": 2}
    assert sum(w["total"] for w in by["food_quality"]["weeks"]) == 2


def test_labor_daily_and_visibility_history_feeds(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    from models import save_labor_daily_history, record_ai_visibility_run
    save_labor_daily_history(rid, {
        "2026-08-31": {"sales": 1000, "actual": 40, "labor_cost": 280, "labor_pct": 28.0},
        "2026-09-01": {"sales": 1200, "actual": 44, "labor_cost": 396, "labor_pct": 33.0},
    }, db_path=db_path)
    days = client.get("/mobile/api/labor/daily", headers=_auth_headers(token)).get_json()["days"]
    assert [d["date"] for d in days] == ["2026-08-31", "2026-09-01"]
    assert days[1]["labor_pct"] == 33.0 and days[0]["day_of_week"] == "Monday"
    for sc in (42, 51, 70):
        record_ai_visibility_run(rid, sc, 60, db_path=db_path)
    runs = client.get("/mobile/api/intel/ai-visibility/history", headers=_auth_headers(token)).get_json()["runs"]
    assert [r["ai_score"] for r in runs] == [42, 51, 70]


# ── Home rebuild: overnight line, pulse strip, action-deck CTAs, weekly receipts, one-tap publish ──

def test_home_pulse_overnight_and_publish_cta(client, db_path):
    rid = _restaurant(db_path)
    _add_review(db_path, rid)
    token = _login(client, db_path, rid)
    data = client.get("/mobile/api/home", headers=_auth_headers(token)).get_json()
    reviews_module = next(m for m in data["modules"] if m["key"] == "reviews")
    assert reviews_module["pulse"] == {"value": "0/1", "label": "replies · 0%", "tone": "warn"}
    # The draft was written just now, so it counts as "answered" in the 24h window.
    assert data["overnight"] == {"answered": 1, "flagged": 0, "window_hours": 24}
    item = next(i for i in data["needs_attention"] if i["type"] == "reviews_awaiting_approval")
    assert item["cta"] == "Publish 1 reply"
    assert item["secondary"] == "Read them first"
    assert item["action"] == "publish_replies"


def test_home_needs_attention_leads_with_urgent_unanswered_reviews(client, db_path):
    rid = _restaurant(db_path)
    review_id = _add_review(db_path, rid)
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET urgency='high' WHERE id=?", (review_id,))
    conn.commit()
    conn.close()
    token = _login(client, db_path, rid)
    data = client.get("/mobile/api/home", headers=_auth_headers(token)).get_json()
    first = data["needs_attention"][0]
    assert first["type"] == "urgent_reviews"
    assert first["title"] == "1 urgent review unanswered"
    assert first["cta"] == "Reply now" and first["action"] == "open_module"


def test_home_weekly_receipts_count_replies_posted_this_week(client, db_path):
    rid = _restaurant(db_path)
    review_id = _add_review(db_path, rid)
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE reviews SET response_status='posted', approved_at=datetime('now'), posted_at=datetime('now'), "
        "review_date=datetime('now','-3 hours') WHERE id=?", (review_id,)
    )
    conn.commit()
    conn.close()
    token = _login(client, db_path, rid)
    data = client.get("/mobile/api/home", headers=_auth_headers(token)).get_json()
    receipt = next(r for r in data["weekly_receipts"] if r["module"] == "reviews")
    assert receipt["emphasis"] == "1 reply"
    assert receipt["text"] == "published to Google — 100% within 24h"


def test_home_weekly_receipts_empty_for_a_quiet_week(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    data = client.get("/mobile/api/home", headers=_auth_headers(token)).get_json()
    assert data["weekly_receipts"] == []
    assert data["overnight"] == {"answered": 0, "flagged": 0, "window_hours": 24}


def test_bulk_approve_publishes_every_drafted_reply_for_this_restaurant_only(client, db_path):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, name="Other Co")
    _add_review(db_path, rid, external_id="r1")
    _add_review(db_path, rid, external_id="r2")
    _add_review(db_path, other, external_id="r3")
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/reviews/approve-all", headers=_auth_headers(token))
    data = resp.get_json()
    assert resp.status_code == 200 and data["ok"] is True
    assert (data["approved"], data["failed"], data["remaining"]) == (2, 0, 0)
    conn = get_conn(db_path)
    mine = conn.execute("SELECT response_status FROM reviews WHERE restaurant_id=?", (rid,)).fetchall()
    theirs = conn.execute("SELECT response_status FROM reviews WHERE restaurant_id=?", (other,)).fetchone()
    conn.close()
    assert {r["response_status"] for r in mine} == {"approved"}
    assert theirs["response_status"] == "drafted"


def test_bulk_approve_respects_limit_and_reports_remaining(client, db_path):
    rid = _restaurant(db_path)
    _add_review(db_path, rid, external_id="r1")
    _add_review(db_path, rid, external_id="r2")
    token = _login(client, db_path, rid)
    data = client.post("/mobile/api/reviews/approve-all", json={"limit": 1},
                       headers=_auth_headers(token)).get_json()
    assert data["approved"] == 1 and data["remaining"] == 1


# ── Square / Clover self-serve connect (mobile) ────────────────────────────

def test_connect_square_saves_credentials_after_verifying_them(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    import square as _square
    monkeypatch.setattr(_square, "test_credentials",
                        lambda t, l: {"ok": True, "location_name": "Gia Mia Downtown"})
    resp = client.post("/mobile/api/connections/square",
                       json={"square_access_token": "tok_abc", "square_location_id": "loc_1"},
                       headers=_auth_headers(token))
    data = resp.get_json()
    assert resp.status_code == 200 and data["ok"] is True
    assert data["location_name"] == "Gia Mia Downtown"
    conn = get_conn(db_path)
    row = conn.execute("SELECT square_access_token, square_location_id, pos_system FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert (row["square_access_token"], row["square_location_id"], row["pos_system"]) == ("tok_abc", "loc_1", "Square")


def test_connect_square_rejects_bad_credentials_without_storing_them(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    import square as _square
    monkeypatch.setattr(_square, "test_credentials",
                        lambda t, l: {"ok": False, "error": "Square returned 401"})
    data = client.post("/mobile/api/connections/square",
                       json={"square_access_token": "bad", "square_location_id": "loc_1"},
                       headers=_auth_headers(token)).get_json()
    assert data["ok"] is False and "401" in data["error"]
    conn = get_conn(db_path)
    row = conn.execute("SELECT square_access_token FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["square_access_token"] is None


def test_connect_square_requires_both_fields(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/connections/square",
                       json={"square_access_token": "tok_abc"}, headers=_auth_headers(token))
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_disconnect_square_clears_every_stored_field(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    import square as _square
    monkeypatch.setattr(_square, "test_credentials", lambda t, l: {"ok": True, "location_name": "X"})
    client.post("/mobile/api/connections/square",
                json={"square_access_token": "tok_abc", "square_location_id": "loc_1"},
                headers=_auth_headers(token))
    assert client.delete("/mobile/api/connections/square", headers=_auth_headers(token)).get_json()["ok"] is True
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT square_access_token, square_location_id, square_last_synced, square_sync_error "
        "FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert not any(row[k] for k in row.keys())


def test_connect_clover_saves_credentials_after_verifying_them(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    import clover as _clover
    monkeypatch.setattr(_clover, "test_credentials",
                        lambda m, t: {"ok": True, "merchant_name": "Gia Mia"})
    data = client.post("/mobile/api/connections/clover",
                       json={"clover_merchant_id": "m_1", "clover_api_token": "tok_x"},
                       headers=_auth_headers(token)).get_json()
    assert data["ok"] is True and data["merchant_name"] == "Gia Mia"
    conn = get_conn(db_path)
    row = conn.execute("SELECT clover_merchant_id, clover_api_token, pos_system FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert (row["clover_merchant_id"], row["clover_api_token"], row["pos_system"]) == ("m_1", "tok_x", "Clover")


def test_connect_clover_rejects_bad_credentials(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    import clover as _clover
    monkeypatch.setattr(_clover, "test_credentials", lambda m, t: {"ok": False, "error": "Clover returned 403"})
    data = client.post("/mobile/api/connections/clover",
                       json={"clover_merchant_id": "m_1", "clover_api_token": "bad"},
                       headers=_auth_headers(token)).get_json()
    assert data["ok"] is False
    conn = get_conn(db_path)
    row = conn.execute("SELECT clover_merchant_id FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    assert row["clover_merchant_id"] is None


def test_pos_connect_is_recorded_in_the_account_activity_log(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    import square as _square
    monkeypatch.setattr(_square, "test_credentials", lambda t, l: {"ok": True, "location_name": "X"})
    client.post("/mobile/api/connections/square",
                json={"square_access_token": "tok_abc", "square_location_id": "loc_1"},
                headers=_auth_headers(token))
    events = client.get("/mobile/api/account/activity", headers=_auth_headers(token)).get_json()["events"]
    connected = [e for e in events if e["type"] == "pos_connected"]
    assert connected and connected[0]["detail"] == "Square"
    assert connected[0]["label"] == "POS connected"


def test_pos_connect_routes_require_authentication(client, db_path):
    for path in ("/mobile/api/connections/square", "/mobile/api/connections/clover"):
        assert client.post(path, json={}).status_code == 401
        assert client.delete(path).status_code == 401


# ── Supplier orders (food cost: order list → actually sent) ────────────────

def _ingredient(db_path, rid, name, *, par=10, stock=0, usage=3, cost=4.0, case=1,
                supplier_name=None, supplier_email=None):
    """A live ingredients-table row — the path load_inventory_for_restaurant
    prefers, so analyse_inventory sees real data rather than the sample set."""
    conn = get_conn(db_path)
    conn.execute("""
        INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost,
                                 case_size, current_stock, avg_daily_usage, last_order_qty,
                                 waste_last_week, is_active, supplier_name, supplier_email)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)
    """, (rid, name, "Produce", "lb", par, cost, case, stock, usage, 0, 0,
          supplier_name, supplier_email))
    conn.commit()
    conn.close()


def test_order_draft_groups_items_by_supplier_and_flags_unassigned(client, db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    _ingredient(db_path, rid, "Tomatoes", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    _ingredient(db_path, rid, "Salmon", supplier_name="Sea Co", supplier_email="orders@sea.test")
    _ingredient(db_path, rid, "Napkins")  # no supplier assigned
    token = _login(client, db_path, rid)
    data = client.get("/mobile/api/food-cost/order-draft", headers=_auth_headers(token)).get_json()
    assert data["ok"] is True
    by_email = {g["supplier_email"]: g for g in data["groups"]}
    assert set(by_email) == {"orders@fresh.test", "orders@sea.test"}
    assert {i["item"] for i in by_email["orders@fresh.test"]["items"]} == {"Romaine", "Tomatoes"}
    assert [i["item"] for i in data["unassigned"]] == ["Napkins"]
    # Every line carries a real quantity, and the group total is their sum.
    fresh = by_email["orders@fresh.test"]
    assert all(i["qty"] > 0 for i in fresh["items"])
    assert fresh["total_cost"] == round(sum(i["line_cost"] for i in fresh["items"]), 2)


def test_send_order_emails_each_supplier_and_records_one_po_each(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    _ingredient(db_path, rid, "Salmon", supplier_name="Sea Co", supplier_email="orders@sea.test")
    token = _login(client, db_path, rid)

    sends = []
    import emails as _emails
    monkeypatch.setattr(_emails, "send_supplier_order_email",
                        lambda **kw: sends.append(kw) or {"id": "email_1"})

    data = client.post("/mobile/api/food-cost/send-order", headers=_auth_headers(token)).get_json()
    assert data["ok"] is True and data["failed"] == []
    assert len(data["sent"]) == 2
    assert {s["po_number"] for s in data["sent"]} == {"PO-0001", "PO-0002"}
    assert {kw["to_email"] for kw in sends} == {"orders@fresh.test", "orders@sea.test"}
    # The supplier replies to the restaurant, not to Cavnar.
    assert all(kw["reply_to"] == "m@x.com" for kw in sends)

    orders = client.get("/mobile/api/food-cost/purchase-orders", headers=_auth_headers(token)).get_json()["orders"]
    assert len(orders) == 2
    assert all(o["status"] == "sent" and o["items"] for o in orders)


def test_send_order_to_one_supplier_only(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    _ingredient(db_path, rid, "Salmon", supplier_name="Sea Co", supplier_email="orders@sea.test")
    token = _login(client, db_path, rid)
    import emails as _emails
    monkeypatch.setattr(_emails, "send_supplier_order_email", lambda **kw: {"id": "e"})
    data = client.post("/mobile/api/food-cost/send-order",
                       json={"supplier_email": "orders@sea.test"},
                       headers=_auth_headers(token)).get_json()
    assert [s["supplier_email"] for s in data["sent"]] == ["orders@sea.test"]


def test_a_failing_supplier_send_does_not_block_the_others_or_record_a_po(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    _ingredient(db_path, rid, "Salmon", supplier_name="Sea Co", supplier_email="bad@sea.test")
    token = _login(client, db_path, rid)

    import emails as _emails
    def _send(**kw):
        if kw["to_email"] == "bad@sea.test":
            raise RuntimeError("550 rejected")
        return {"id": "e"}
    monkeypatch.setattr(_emails, "send_supplier_order_email", _send)

    data = client.post("/mobile/api/food-cost/send-order", headers=_auth_headers(token)).get_json()
    assert [s["supplier_email"] for s in data["sent"]] == ["orders@fresh.test"]
    assert [f["supplier_email"] for f in data["failed"]] == ["bad@sea.test"]
    orders = client.get("/mobile/api/food-cost/purchase-orders", headers=_auth_headers(token)).get_json()["orders"]
    assert [o["supplier_email"] for o in orders] == ["orders@fresh.test"]


def test_send_order_with_no_supplier_assigned_is_a_clear_refusal(client, db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Napkins")
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/food-cost/send-order", headers=_auth_headers(token))
    assert resp.status_code == 400
    assert "no items with a supplier" in resp.get_json()["error"].lower()


def test_setting_a_supplier_moves_an_item_out_of_unassigned(client, db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Napkins")
    token = _login(client, db_path, rid)
    assert client.post("/mobile/api/food-cost/ingredient-supplier",
                       json={"name": "Napkins", "supplier_name": "Dry Goods",
                             "supplier_email": "orders@dry.test"},
                       headers=_auth_headers(token)).get_json()["ok"] is True
    data = client.get("/mobile/api/food-cost/order-draft", headers=_auth_headers(token)).get_json()
    assert data["unassigned"] == []
    assert data["groups"][0]["supplier_email"] == "orders@dry.test"


def test_setting_a_supplier_validates_the_address_and_the_ingredient(client, db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Napkins")
    token = _login(client, db_path, rid)
    bad = client.post("/mobile/api/food-cost/ingredient-supplier",
                      json={"name": "Napkins", "supplier_email": "not-an-email"},
                      headers=_auth_headers(token))
    assert bad.status_code == 400
    missing = client.post("/mobile/api/food-cost/ingredient-supplier",
                          json={"name": "Nonexistent", "supplier_email": "a@b.test"},
                          headers=_auth_headers(token))
    assert missing.status_code == 404


def test_supplier_assignment_cannot_reach_another_restaurants_ingredient(client, db_path):
    mine = _restaurant(db_path)
    theirs = _restaurant(db_path, name="Other Co")
    _ingredient(db_path, theirs, "TheirItem")
    token = _login(client, db_path, mine)
    resp = client.post("/mobile/api/food-cost/ingredient-supplier",
                       json={"name": "TheirItem", "supplier_email": "a@b.test"},
                       headers=_auth_headers(token))
    assert resp.status_code == 404
    conn = get_conn(db_path)
    row = conn.execute("SELECT supplier_email FROM ingredients WHERE restaurant_id=?", (theirs,)).fetchone()
    conn.close()
    assert row["supplier_email"] is None


def test_receiving_a_po_closes_it_and_is_scoped_to_the_restaurant(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, name="Other Co")
    _ingredient(db_path, rid, "Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test")
    token = _login(client, db_path, rid)
    import emails as _emails
    monkeypatch.setattr(_emails, "send_supplier_order_email", lambda **kw: {"id": "e"})
    client.post("/mobile/api/food-cost/send-order", headers=_auth_headers(token))
    po = client.get("/mobile/api/food-cost/purchase-orders", headers=_auth_headers(token)).get_json()["orders"][0]

    other_token = _login(client, db_path, other, username="otheruser")
    assert client.post(f"/mobile/api/food-cost/purchase-orders/{po['id']}/received",
                       headers=_auth_headers(other_token)).status_code == 404

    assert client.post(f"/mobile/api/food-cost/purchase-orders/{po['id']}/received",
                       headers=_auth_headers(token)).get_json()["ok"] is True
    received = client.get("/mobile/api/food-cost/purchase-orders?status=received",
                          headers=_auth_headers(token)).get_json()["orders"]
    assert [o["po_number"] for o in received] == [po["po_number"]]
    # Closing twice is refused rather than silently re-closing.
    assert client.post(f"/mobile/api/food-cost/purchase-orders/{po['id']}/received",
                       headers=_auth_headers(token)).status_code == 404


def test_supplier_order_routes_require_authentication(client, db_path):
    assert client.get("/mobile/api/food-cost/order-draft").status_code == 401
    assert client.post("/mobile/api/food-cost/send-order").status_code == 401
    assert client.get("/mobile/api/food-cost/purchase-orders").status_code == 401
    assert client.post("/mobile/api/food-cost/ingredient-supplier", json={}).status_code == 401


# ── Google Business Profile posts ──────────────────────────────────────────

def _connect_gmb(db_path, rid):
    """The three fields gmb.is_connected / create_local_post require."""
    from models import update_restaurant as _ur
    _ur(rid, {"gmb_refresh_token": "refresh_x", "gmb_account_id": "accounts/123",
              "gmb_location_id": "locations/456"}, db_path=db_path)


def test_google_post_requires_a_connected_listing(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/marketing/google-post",
                       json={"summary": "Fresh menu today"}, headers=_auth_headers(token))
    assert resp.status_code == 400
    assert "connect google business" in resp.get_json()["error"].lower()


def test_google_post_publishes_and_logs_the_piece(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _connect_gmb(db_path, rid)
    token = _login(client, db_path, rid)
    import gmb as _gmb
    monkeypatch.setattr(_gmb, "is_connected", lambda r: True)
    seen = {}
    monkeypatch.setattr(_gmb, "create_local_post",
                        lambda r, summary, cta_type=None, cta_url=None: seen.update(
                            summary=summary, cta_type=cta_type, cta_url=cta_url)
                        or {"ok": True, "name": "accounts/123/locations/456/localPosts/9"})
    data = client.post("/mobile/api/marketing/google-post",
                       json={"summary": "Half-price oysters all week",
                             "cta_type": "LEARN_MORE", "cta_url": "https://gia.test"},
                       headers=_auth_headers(token)).get_json()
    assert data["ok"] is True
    assert data["name"].endswith("/localPosts/9")
    assert seen["summary"] == "Half-price oysters all week"
    assert seen["cta_type"] == "LEARN_MORE"

    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT content_type, post_platform FROM marketing_content_log WHERE restaurant_id=?", (rid,)
    ).fetchone()
    conn.close()
    assert (row["content_type"], row["post_platform"]) == ("google_promo", "google")


def test_google_post_surfaces_the_api_error_and_logs_nothing(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _connect_gmb(db_path, rid)
    token = _login(client, db_path, rid)
    import gmb as _gmb
    monkeypatch.setattr(_gmb, "is_connected", lambda r: True)
    monkeypatch.setattr(_gmb, "create_local_post",
                        lambda *a, **k: {"ok": False, "error": "GBP API 403: not authorized"})
    data = client.post("/mobile/api/marketing/google-post",
                       json={"summary": "Anything"}, headers=_auth_headers(token)).get_json()
    assert data["ok"] is False and "403" in data["error"]
    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 0


def test_google_post_requires_text(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _connect_gmb(db_path, rid)
    token = _login(client, db_path, rid)
    import gmb as _gmb
    monkeypatch.setattr(_gmb, "is_connected", lambda r: True)
    assert client.post("/mobile/api/marketing/google-post", json={"summary": "  "},
                       headers=_auth_headers(token)).status_code == 400


def test_google_post_route_requires_authentication(client, db_path):
    assert client.post("/mobile/api/marketing/google-post", json={}).status_code == 401


# ── Menu profitability ─────────────────────────────────────────────────────

def _menu_item(db_path, rid, name, price=None):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO menu_items (restaurant_id, name, sell_price, is_active) VALUES (?,?,?,1)",
                       (rid, name, price))
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return mid


def _recipe(db_path, rid, menu_item_id, pairs):
    """pairs: [(ingredient_name, unit_cost, qty_per_unit)]"""
    conn = get_conn(db_path)
    for name, cost, qty in pairs:
        cur = conn.execute("""INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active)
                              VALUES (?,?,?,?,1)""", (rid, name, "lb", cost))
        conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                     (menu_item_id, cur.lastrowid, qty))
    conn.commit()
    conn.close()


def test_menu_profitability_costs_the_plate_and_computes_food_cost_pct(client, db_path):
    rid = _restaurant(db_path)
    burger = _menu_item(db_path, rid, "Burger", price=18.0)
    _recipe(db_path, rid, burger, [("Beef", 6.0, 0.5), ("Bun", 1.0, 1.0)])  # 3.00 + 1.00 = 4.00
    token = _login(client, db_path, rid)
    data = client.get("/mobile/api/food-cost/menu-profitability", headers=_auth_headers(token)).get_json()
    assert data["ok"] is True
    item = data["priced"][0]
    assert item["plate_cost"] == 4.0
    assert item["margin"] == 14.0
    assert item["food_cost_pct"] == round(4 / 18 * 100, 1)
    assert data["average_food_cost_pct"] == item["food_cost_pct"]


def test_items_are_grouped_by_what_can_honestly_be_said_about_them(client, db_path):
    rid = _restaurant(db_path)
    priced = _menu_item(db_path, rid, "Priced Dish", price=20.0)
    _recipe(db_path, rid, priced, [("A", 2.0, 1.0)])
    unpriced = _menu_item(db_path, rid, "No Price Yet")
    _recipe(db_path, rid, unpriced, [("B", 3.0, 1.0)])
    _menu_item(db_path, rid, "No Recipe", price=12.0)
    token = _login(client, db_path, rid)
    d = client.get("/mobile/api/food-cost/menu-profitability", headers=_auth_headers(token)).get_json()
    assert [i["name"] for i in d["priced"]] == ["Priced Dish"]
    assert [i["name"] for i in d["unpriced"]] == ["No Price Yet"]
    assert d["unpriced"][0]["plate_cost"] == 3.0        # cost is known, margin isn't
    assert "margin" not in d["unpriced"][0]
    assert [i["name"] for i in d["unmapped"]] == ["No Recipe"]


def test_worst_margin_is_listed_first(client, db_path):
    rid = _restaurant(db_path)
    good = _menu_item(db_path, rid, "Good Margin", price=20.0)
    _recipe(db_path, rid, good, [("A", 2.0, 1.0)])       # 10%
    bad = _menu_item(db_path, rid, "Bad Margin", price=10.0)
    _recipe(db_path, rid, bad, [("B", 6.0, 1.0)])        # 60%
    token = _login(client, db_path, rid)
    d = client.get("/mobile/api/food-cost/menu-profitability", headers=_auth_headers(token)).get_json()
    assert [i["name"] for i in d["priced"]] == ["Bad Margin", "Good Margin"]
    assert d["worst"]["name"] == "Bad Margin"
    assert d["best"]["name"] == "Good Margin"


def test_setting_and_clearing_a_price(client, db_path):
    rid = _restaurant(db_path)
    mid = _menu_item(db_path, rid, "Dish")
    _recipe(db_path, rid, mid, [("A", 5.0, 1.0)])
    token = _login(client, db_path, rid)
    assert client.post("/mobile/api/food-cost/menu-item-price",
                       json={"menu_item_id": mid, "sell_price": 15.0},
                       headers=_auth_headers(token)).get_json()["ok"] is True
    d = client.get("/mobile/api/food-cost/menu-profitability", headers=_auth_headers(token)).get_json()
    assert d["priced"][0]["food_cost_pct"] == round(5 / 15 * 100, 1)

    client.post("/mobile/api/food-cost/menu-item-price",
                json={"menu_item_id": mid, "sell_price": 0}, headers=_auth_headers(token))
    d = client.get("/mobile/api/food-cost/menu-profitability", headers=_auth_headers(token)).get_json()
    assert d["priced"] == [] and [i["name"] for i in d["unpriced"]] == ["Dish"]


def test_pricing_cannot_reach_another_restaurants_menu(client, db_path):
    mine = _restaurant(db_path)
    theirs = _restaurant(db_path, name="Other Co")
    their_item = _menu_item(db_path, theirs, "Their Dish", price=10.0)
    token = _login(client, db_path, mine)
    resp = client.post("/mobile/api/food-cost/menu-item-price",
                       json={"menu_item_id": their_item, "sell_price": 99.0},
                       headers=_auth_headers(token))
    assert resp.status_code == 400
    conn = get_conn(db_path)
    price = conn.execute("SELECT sell_price FROM menu_items WHERE id=?", (their_item,)).fetchone()["sell_price"]
    conn.close()
    assert price == 10.0


def test_menu_profitability_routes_require_authentication(client, db_path):
    assert client.get("/mobile/api/food-cost/menu-profitability").status_code == 401
    assert client.post("/mobile/api/food-cost/menu-item-price", json={}).status_code == 401
