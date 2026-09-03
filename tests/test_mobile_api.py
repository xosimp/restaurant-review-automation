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
