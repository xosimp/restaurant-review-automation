"""Audit #5: signup, entitlement and the Stripe events that were missing.

The two P0s were that anyone could self-register into permanent full-tier
access, and that spend on non-paying accounts drew down the same global AI
ceiling paying clients depend on — so a handful of stale demo accounts could
take Ask Cavnar away from someone who pays for it.

The P1 the whole thing hinged on: checkout put a module COUNT in metadata and
nothing read it, so what a client paid for and what they could open were two
unconnected facts.
"""
import json

import pytest
from flask import Flask

import ai_utils
import emails
import models
import webhook_routes
from models import get_conn


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, webhook_routes, ai_utils):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _restaurant(db_path, rid=1, **kw):
    cols = {"id": rid, "name": f"Restaurant {rid}", "owner_email": f"o{rid}@example.com",
            "billing_status": "trial"}
    cols.update(kw)
    conn = models.get_conn(db_path)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _status(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        r = conn.execute("SELECT billing_status, module_reviews, module_labor, "
                         "module_inventory, module_marketing FROM restaurants WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ── P0: self-serve signup is closed ────────────────────────────────────────

def _app():
    from mobile_api import mobile_bp
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mobile_bp, url_prefix="/mobile/api")
    return app


def test_registration_is_refused_by_default(monkeypatch):
    monkeypatch.delenv("ALLOW_PUBLIC_SIGNUP", raising=False)
    c = _app().test_client()
    r = c.post("/mobile/api/register", json={
        "restaurant_name": "Free Rider", "email": "free@rider.test",
        "username": "freerider", "password": "hunter2hunter2"})
    assert r.status_code == 403
    assert "will@cavnar.ai" in r.get_json()["error"]


def test_no_account_is_created_when_signup_is_closed(db_path, monkeypatch):
    monkeypatch.delenv("ALLOW_PUBLIC_SIGNUP", raising=False)
    _app().test_client().post("/mobile/api/register", json={
        "restaurant_name": "Free Rider", "email": "free@rider.test",
        "username": "freerider", "password": "hunter2hunter2"})
    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) c FROM restaurants WHERE name='Free Rider'").fetchone()["c"]
    conn.close()
    assert n == 0


def test_the_flag_still_turns_it_back_on(monkeypatch):
    """The code is kept, not deleted — it works again the day it is wanted."""
    import mobile_api
    monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "1")
    assert mobile_api.public_signup_open() is True
    monkeypatch.setenv("ALLOW_PUBLIC_SIGNUP", "0")
    assert mobile_api.public_signup_open() is False


# ── P0: unpaid spend cannot starve a paying client ─────────────────────────

def _burn(db_path, rid, dollars, n=1):
    conn = models.get_conn(db_path)
    conn.execute(ai_utils._USAGE_TABLE_SQL)
    for _ in range(n):
        conn.execute("INSERT INTO ai_usage (restaurant_id, cost_usd, created_at) "
                     "VALUES (?,?,datetime('now'))", (rid, dollars))
    conn.commit()
    conn.close()
    ai_utils._budget_cache.clear()


def test_an_unpaid_account_has_its_own_smaller_ceiling(db_path):
    rid = _restaurant(db_path, 1, billing_status="trial")
    st = ai_utils.ai_budget_status(rid, db_path)
    assert st["day"]["budget"] == ai_utils.AI_UNPAID_DAILY_BUDGET_USD
    assert st["month"]["budget"] == ai_utils.AI_UNPAID_MONTHLY_BUDGET_USD
    assert st["paid"] is False


def test_a_paying_account_keeps_the_full_ceiling(db_path):
    rid = _restaurant(db_path, 1, billing_status="active")
    st = ai_utils.ai_budget_status(rid, db_path)
    assert st["day"]["budget"] == ai_utils.AI_DAILY_BUDGET_USD
    assert st["paid"] is True


def test_unpaid_spend_is_excluded_from_the_global_pool(db_path):
    """The finding with a named victim: a stale demo account burning the
    global ceiling used to block every paying client."""
    paying = _restaurant(db_path, 1, billing_status="active")
    demo   = _restaurant(db_path, 2, billing_status="trial")
    _burn(db_path, demo, ai_utils.AI_GLOBAL_MONTHLY_BUDGET_USD * 2)

    assert ai_utils.ai_budget_status(paying, db_path)["global_month"]["spend"] == 0.0
    assert ai_utils.ai_budget_exceeded(paying, db_path) is None, \
        "a paying client was blocked by spend on an account that isn't paying"
    # and the demo account is stopped by its own ceiling
    assert ai_utils.ai_budget_exceeded(demo, db_path) is not None


def test_paying_spend_still_counts_toward_the_global_pool(db_path):
    a = _restaurant(db_path, 1, billing_status="active")
    b = _restaurant(db_path, 2, billing_status="internal")
    _burn(db_path, b, ai_utils.AI_GLOBAL_MONTHLY_BUDGET_USD + 1)
    assert ai_utils.ai_budget_exceeded(a, db_path) == "monthly budget across all clients"


# ── P1: paying for modules grants modules ──────────────────────────────────

def test_checkout_metadata_carries_identity_and_entitlement():
    meta = emails._checkout_metadata("Simple EJ's", 2, restaurant_id=4,
                                     modules=["labor", "reviews"])
    assert meta["restaurant_id"] == "4"
    assert meta["module_keys"] == "labor,reviews"
    assert all(isinstance(v, str) for v in meta.values()), "Stripe metadata must be strings"


def test_metadata_is_omitted_rather_than_faked_when_unknown():
    meta = emails._checkout_metadata("Simple EJ's", 4)
    assert "restaurant_id" not in meta and "module_keys" not in meta


def test_entitlement_is_set_to_exactly_what_was_bought(db_path):
    rid = _restaurant(db_path, 1, module_reviews=1, module_labor=1,
                      module_inventory=1, module_marketing=1)
    webhook_routes._apply_module_entitlement(rid, "reviews,labor")
    row = _status(db_path, rid)
    assert (row["module_reviews"], row["module_labor"]) == (1, 1)
    assert (row["module_inventory"], row["module_marketing"]) == (0, 0), \
        "modules that were not paid for stayed on"


def test_empty_metadata_never_revokes_everything(db_path):
    """A missing key and 'they bought nothing' look identical; treating them
    the same would strip a paying client on the next event."""
    rid = _restaurant(db_path, 1, module_reviews=1, module_labor=1)
    assert webhook_routes._apply_module_entitlement(rid, "") == {}
    row = _status(db_path, rid)
    assert (row["module_reviews"], row["module_labor"]) == (1, 1)


def test_unknown_module_keys_are_ignored(db_path):
    rid = _restaurant(db_path, 1)
    assert webhook_routes._apply_module_entitlement(rid, "reviews,wat,../etc") == {
        "module_reviews": 1, "module_labor": 0, "module_inventory": 0, "module_marketing": 0}


def test_intel_cannot_be_granted_directly(db_path):
    """It is derived from full tier, not a column — granting it would be a lie."""
    rid = _restaurant(db_path, 1)
    assert webhook_routes._apply_module_entitlement(rid, "intel") == {}


# ── P1: identity resolves from metadata ────────────────────────────────────

def test_metadata_restaurant_id_is_validated_not_trusted(db_path):
    _restaurant(db_path, 7)
    assert webhook_routes._restaurant_from_metadata({"restaurant_id": "7"}) == 7
    assert webhook_routes._restaurant_from_metadata({"restaurant_id": "999999"}) is None
    assert webhook_routes._restaurant_from_metadata({"restaurant_id": "not-a-number"}) is None
    assert webhook_routes._restaurant_from_metadata({}) is None
    assert webhook_routes._restaurant_from_metadata(None) is None


# ── P1: dunning ────────────────────────────────────────────────────────────

def test_past_due_still_allows_access(db_path):
    rid = _restaurant(db_path, 1, billing_status="past_due")
    assert models.subscription_allows_access(rid, db_path) is True, \
        "a warning state must not lock out a client Stripe is still retrying"


def test_paused_and_churned_block_access(db_path):
    for i, status in enumerate(("paused", "churned"), start=1):
        rid = _restaurant(db_path, i, billing_status=status)
        assert models.subscription_allows_access(rid, db_path) is False
