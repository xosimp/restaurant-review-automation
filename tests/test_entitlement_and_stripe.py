"""Subscription entitlement and Stripe reconciliation.

From the pre-launch audit:

* Nothing checked billing_status for access. customer.subscription.deleted
  only emailed Will asking him to deactivate the account by hand, so a
  cancelled customer kept the dashboard, the iOS app and every Claude-backed
  feature indefinitely, at Cavnar's API cost.
* invoice.paid matched a restaurant by users.email, so an owner who changed
  their email stopped reconciling entirely, and a multi-location owner only
  ever resolved to their base restaurant.
* Stripe retries any non-2xx and can deliver duplicates; nothing deduped by
  event id, so a retry re-sent the customer's receipt.
"""
import pytest
from flask import Flask

import auth
import client_api
import guest_marketing
import mobile_api
import models
import webhook_routes
from auth import create_user, init_auth
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_conn, subscription_allows_access, update_restaurant


def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, guest_marketing, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)


@pytest.fixture(autouse=True)
def _init(db_path, monkeypatch):
    _redirect_db(monkeypatch, db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def app(db_path, monkeypatch):
    flask_app = Flask(__name__)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    return create_restaurant(
        Restaurant(name=kw.pop("name", "Entitle Co"), owner_email=kw.pop("owner_email", "e@x.test"), **kw),
        db_path=db_path,
    )


def _login(client, db_path, rid, username="alice", password="correct-horse"):
    create_user(rid, username, f"{username}@x.test", password, db_path=db_path)
    return client.post("/mobile/api/login", json={"username": username, "password": password}).get_json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


# ── the entitlement predicate ───────────────────────────────────────────────

@pytest.mark.parametrize("status,allowed", [
    ("active", True), ("trial", True), ("internal", True), ("past_due", True),
    ("", True), ("pending", True),
    ("churned", False), ("paused", False), ("canceled", False), ("cancelled", False),
])
def test_billing_states_map_to_access(db_path, status, allowed):
    rid = _restaurant(db_path, billing_status=status)
    assert subscription_allows_access(rid, db_path=db_path) is allowed


def test_unknown_status_and_missing_row_fail_open(db_path):
    """A value nobody anticipated must not lock out a paying customer."""
    rid = _restaurant(db_path, billing_status="some_new_state")
    assert subscription_allows_access(rid, db_path=db_path) is True
    assert subscription_allows_access(999999, db_path=db_path) is True


def test_lookup_failure_fails_open(db_path, monkeypatch):
    rid = _restaurant(db_path, billing_status="churned")
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert subscription_allows_access(rid, db_path=db_path) is True


# ── enforcement on real routes ──────────────────────────────────────────────

def test_active_customer_reaches_data_routes(client, db_path):
    rid = _restaurant(db_path, billing_status="active", module_reviews=1)
    token = _login(client, db_path, rid)
    assert client.get("/mobile/api/home", headers=_hdr(token)).status_code == 200


def test_churned_customer_is_refused(client, db_path):
    rid = _restaurant(db_path, billing_status="active", module_reviews=1)
    token = _login(client, db_path, rid)
    assert client.get("/mobile/api/home", headers=_hdr(token)).status_code == 200

    update_restaurant(rid, {"billing_status": "churned"}, db_path=db_path)
    resp = client.get("/mobile/api/home", headers=_hdr(token))
    assert resp.status_code == 402
    body = resp.get_json()
    assert body["ok"] is False and body["billing_inactive"] is True


def test_churned_customer_can_still_reach_account_and_logout(client, db_path):
    """The block must not be a dead end — they need to see why, and get out."""
    rid = _restaurant(db_path, billing_status="churned", module_reviews=1)
    token = _login(client, db_path, rid)
    assert client.get("/mobile/api/account", headers=_hdr(token)).status_code == 200
    assert client.post("/mobile/api/logout", headers=_hdr(token)).status_code == 200


# ── Stripe reconciliation ───────────────────────────────────────────────────

def test_restaurant_resolves_by_stripe_customer_id_after_email_change(db_path):
    rid = _restaurant(db_path, owner_email="old@x.test")
    update_restaurant(rid, {"stripe_customer_id": "cus_123"}, db_path=db_path)
    create_user(rid, "owner", "changed@x.test", "pw", db_path=db_path)
    # Stripe still knows the ORIGINAL email; matching on it alone would miss.
    assert webhook_routes._restaurant_for_stripe("cus_123", "old@x.test") == rid
    assert webhook_routes._restaurant_for_stripe("cus_123", "nomatch@x.test") == rid


def test_restaurant_resolves_by_email_before_a_customer_id_exists(db_path):
    rid = _restaurant(db_path, owner_email="first@x.test")
    create_user(rid, "firstowner", "first@x.test", "pw", db_path=db_path)
    assert webhook_routes._restaurant_for_stripe("", "first@x.test") == rid
    assert webhook_routes._restaurant_for_stripe("cus_unknown", "first@x.test") == rid


def test_unmatched_stripe_customer_returns_none(db_path):
    assert webhook_routes._restaurant_for_stripe("cus_nobody", "nobody@x.test") is None


def test_billing_state_applies_to_every_location_in_the_group(db_path):
    a = _restaurant(db_path, name="Downtown", owner_email="g@x.test", location_group="Group A")
    b = _restaurant(db_path, name="Uptown", owner_email="g2@x.test", location_group="Group A")
    solo = _restaurant(db_path, name="Solo", owner_email="s@x.test")
    assert set(webhook_routes._sibling_restaurant_ids(a)) == {a, b}
    assert webhook_routes._sibling_restaurant_ids(solo) == [solo]


# ── webhook idempotency ─────────────────────────────────────────────────────

def test_the_same_stripe_event_is_only_claimed_once(db_path):
    assert webhook_routes._claim_stripe_event("evt_abc") is True
    assert webhook_routes._claim_stripe_event("evt_abc") is False, "a Stripe retry must not re-run the handler"
    assert webhook_routes._claim_stripe_event("evt_def") is True


def test_a_missing_event_id_does_not_block_processing(db_path):
    assert webhook_routes._claim_stripe_event("") is True
