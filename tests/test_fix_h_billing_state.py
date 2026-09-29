"""Fix round H — billing state: history, holds, the subscription mirror and
the Stripe lifecycle as the webhook applies it.

Findings pinned here: #7 (a failed billing write is retried, not
acknowledged), #11 (billing-status and module history), #15/#51 (the
subscription mirror: interval, amount, discount, trial end; converted_at),
#50 (one ledger row per event, attributed by metadata, refunds at the
refunded amount), #74/#145 (signature failures counted; no secret, no
entry), #106 (module flags follow Stripe only on a real plan change; a
disagreement is recorded), #114 (dispute and refund holds only an admin
lifts), #115 (lifecycle events matched by ids, never email; an ended
subscription is not "live"), #116 (state moves across the locations a
subscription covers) and #155 (one receipt per paid invoice; the
conversion alert on the first non-zero retainer invoice).

Stripe is a stand-in module: nothing here reaches Stripe, Resend or DocuSign.
"""
import json
import sqlite3
import sys
import types

import pytest
from flask import Flask

import admin_events
import auth
import billing_jobs
import emails
import models
import ops
import webhook_routes
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, webhook_routes, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    alerts = []
    monkeypatch.setattr(webhook_routes, "_send_alert", lambda subject, body: alerts.append((subject, body)))
    return alerts


def _rid(db_path, name="Bill Co", owner_email="o@x.test", **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email=owner_email), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _count(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def _rows(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


class _Obj(dict):
    __getattr__ = dict.get


@pytest.fixture
def stripe_mod(monkeypatch):
    """A stand-in stripe module: construct_event returns `holder.event`."""
    holder = types.SimpleNamespace(event=None, calls=[], subs={}, charges={})
    mod = types.ModuleType("stripe")
    mod.Webhook = type("W", (), {"construct_event": staticmethod(lambda *a, **k: holder.event)})

    class Subscription:
        @staticmethod
        def cancel(sid, **k):
            holder.calls.append(("Subscription.cancel", sid))

        @staticmethod
        def retrieve(sid, **k):
            holder.calls.append(("Subscription.retrieve", sid))
            return holder.subs.get(sid) or _Obj(id=sid, status="active")

    class Charge:
        @staticmethod
        def retrieve(cid, **k):
            holder.calls.append(("Charge.retrieve", cid))
            return holder.charges.get(cid)

    mod.Subscription, mod.Charge = Subscription, Charge
    mod.api_key = ""
    monkeypatch.setitem(sys.modules, "stripe", mod)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_fixture")
    holder.module = mod
    return holder


@pytest.fixture
def hook(stripe_mod):
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    client = app.test_client()

    def post(event):
        stripe_mod.event = event
        return client.post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    post.stripe = stripe_mod
    return post


def _evt(eid, etype, obj, created=1000, prev=None):
    data = {"object": obj}
    if prev is not None:
        data["previous_attributes"] = prev
    return {"id": eid, "type": etype, "created": created, "data": data}


def _sub(sid, rid=None, status="active", customer="cus_1", keys=None, **kw):
    meta = {}
    if rid is not None:
        meta["restaurant_id"] = str(rid)
    if keys:
        meta["module_keys"] = keys
    obj = {"id": sid, "customer": customer, "status": status, "metadata": meta,
           "items": {"data": [{"id": "si_1", "quantity": 1,
                               "price": {"id": "price_m", "unit_amount": 34900, "currency": "usd",
                                         "recurring": {"interval": "month", "interval_count": 1}}}]}}
    obj.update(kw)
    return obj


def _history(db_path, rid, field):
    return _rows(db_path, "SELECT * FROM billing_status_history WHERE restaurant_id=? AND field=? ORDER BY id",
                 rid, field)


# ── #11 history, inside update_restaurant ──────────────────────────────────

def test_a_billing_status_change_is_recorded_with_its_source_and_event(db_path):
    rid = _rid(db_path)
    with models.billing_context(source="stripe", actor="stripe", stripe_event_id="evt_9", reason="invoice.paid"):
        update_restaurant(rid, {"billing_status": "active"}, db_path=db_path)
    (h,) = _history(db_path, rid, "billing_status")
    assert (h["old_value"], h["new_value"], h["source"], h["actor"], h["stripe_event_id"], h["reason"]) == \
        ("trial", "active", "stripe", "stripe", "evt_9", "invoice.paid")


def test_module_flag_changes_are_history_and_an_unchanged_value_is_not(db_path):
    rid = _rid(db_path)
    update_restaurant(rid, {"module_labor": 0, "module_reviews": 1}, db_path=db_path)   # labor was 1
    assert [(h["old_value"], h["new_value"]) for h in _history(db_path, rid, "module_labor")] == [("1", "0")]
    assert _history(db_path, rid, "module_reviews") == []
    # Outside any request and any context: the source says so.
    assert _history(db_path, rid, "module_labor")[0]["source"] == "system"


def test_the_three_new_restaurant_columns_survive_the_four_touch_points(db_path):
    rid = _rid(db_path)
    update_restaurant(rid, {"pause_reason": "dispute", "converted_at": "2026-09-01 10:00:00",
                            "contract_signed_at": "2026-08-30 12:00:00"}, db_path=db_path)
    r = get_restaurant(rid, db_path)
    assert (r.pause_reason, r.converted_at, r.contract_signed_at) == \
        ("dispute", "2026-09-01 10:00:00", "2026-08-30 12:00:00")


@pytest.mark.parametrize("status,reason,until,lock", [
    ("paused", "self", "2026-10-20", None),
    ("paused", "dispute", None, "dispute"),
    ("paused", "refund", None, "refund"),
    ("paused", "admin", None, "admin"),
    ("paused", None, "2026-10-20", None),     # a pre-column self-serve pause (it had a date)
    ("paused", None, None, "admin"),          # a pre-column pause nobody can explain
    ("active", "dispute", None, None),        # not paused: no hold in force
])
def test_pause_lock_reads_old_and_new_rows(status, reason, until, lock):
    assert models.pause_lock({"billing_status": status, "pause_reason": reason, "paused_until": until}) == lock


def test_the_welcome_link_token_lasts_longer_than_a_reset_and_names_the_login(db_path):
    rid = _rid(db_path)
    uid = create_user(rid, "own", "own@x.test", "pw-owner-1", db_path=db_path)
    tok = models.create_reset_token("ignored@x.test", db_path=db_path, ttl_hours=72, user_id=uid)
    user = models.validate_reset_token(tok, db_path=db_path)
    assert user and user["id"] == uid
    from datetime import datetime, timezone, timedelta
    exp = datetime.fromisoformat(user["reset_token_expires"])
    assert exp - datetime.now(timezone.utc) > timedelta(hours=71)


# ── #145 / #74 the secret and signature failures ────────────────────────────

def test_no_signing_secret_refuses_every_stripe_request_and_says_so(db_path, stripe_mod, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    rid = _rid(db_path, billing_status="pending")
    stripe_mod.event = _evt("evt_x", "checkout.session.completed",
                            {"customer": "cus_1", "metadata": {"restaurant_id": str(rid)}})
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    resp = app.test_client().post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t"})
    assert resp.status_code == 401
    assert get_restaurant(rid, db_path).billing_status == "pending"
    (row,) = _rows(db_path, "SELECT * FROM webhook_verifications WHERE provider='stripe'")
    assert row["failures_since_verified"] == 1 and "not set" in row["last_failure_error"]
    assert _count(db_path, "SELECT COUNT(*) FROM job_failures WHERE job='stripe_webhook_signature'") == 1


def test_signature_failures_are_counted_and_captured_once_an_hour(db_path, monkeypatch):
    for _ in range(3):
        webhook_routes._webhook_seen("stripe", False, error="No signatures found")
    (row,) = _rows(db_path, "SELECT * FROM webhook_verifications WHERE provider='stripe'")
    assert row["failures_total"] == 3 and row["failures_since_verified"] == 3
    assert _count(db_path, "SELECT COUNT(*) FROM job_failures WHERE job='stripe_webhook_signature'") == 1
    webhook_routes._webhook_seen("stripe", True, event="invoice.paid evt_1")
    (row,) = _rows(db_path, "SELECT * FROM webhook_verifications WHERE provider='stripe'")
    assert row["failures_since_verified"] == 0 and row["last_verified_event"] == "invoice.paid evt_1"


def test_the_secret_is_read_per_request(monkeypatch):
    monkeypatch.setattr(webhook_routes, "STRIPE_WEBHOOK_SECRET", "")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_rotated")
    assert webhook_routes._stripe_webhook_secret() == "whsec_rotated"


# ── #50 the ledger ──────────────────────────────────────────────────────────

def test_a_redelivered_event_is_one_ledger_row_attributed_by_metadata(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    other = _rid(db_path, name="Owns the email", owner_email="payer@x.test")
    ev = _evt("evt_upd", "customer.subscription.updated", _sub("sub_1", rid=rid))
    ev["data"]["object"]["customer_email"] = "payer@x.test"
    for _ in range(3):
        assert hook(ev).status_code == 200
    rows = _rows(db_path, "SELECT * FROM admin_events WHERE source='stripe'")
    assert len(rows) == 1 and rows[0]["restaurant_id"] == rid and rows[0]["external_id"] == "evt_upd"
    assert other != rid


def test_a_retry_after_a_failed_dispatch_does_not_double_the_ledger(db_path, hook, monkeypatch):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    real = webhook_routes.update_restaurant
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)
    monkeypatch.setattr(webhook_routes, "update_restaurant", flaky)
    ev = _evt("evt_del", "customer.subscription.deleted", _sub("sub_1", rid=rid, status="canceled"))
    assert hook(ev).status_code == 500
    assert hook(ev).status_code == 200
    assert _count(db_path, "SELECT COUNT(*) FROM admin_events WHERE external_id='evt_del'") == 1


def test_a_partial_refund_is_recorded_at_the_refunded_amount(db_path):
    rid = _rid(db_path, stripe_customer_id="cus_1")
    admin_events.record_stripe(_evt("evt_ref", "charge.refunded",
                                    {"id": "ch_1", "customer": "cus_1", "amount": 75000, "amount_refunded": 500}),
                               db_path=db_path)
    (row,) = _rows(db_path, "SELECT * FROM admin_events WHERE external_id='evt_ref'")
    assert row["amount"] == 5.0 and row["restaurant_id"] == rid
    assert "$5.00 of $750.00" in row["summary"]


def test_invoice_rows_carry_setup_and_recurring_facts(db_path):
    inv = {"id": "in_1", "customer": "cus_1", "subscription": "sub_1", "amount_paid": 109900,
           "currency": "usd", "subtotal": 109900, "total": 109900, "billing_reason": "subscription_create",
           "lines": {"data": [{"amount": 75000, "type": "invoiceitem", "price": {"type": "one_time"}},
                              {"amount": 34900, "type": "subscription",
                               "price": {"recurring": {"interval": "month"}}}]}}
    admin_events.record_stripe(_evt("evt_in", "invoice.paid", inv), db_path=db_path)
    (row,) = _rows(db_path, "SELECT payload FROM admin_events WHERE external_id='evt_in'")
    p = json.loads(row["payload"])
    assert (p["kind"], p["setup_cents"], p["recurring_cents"], p["currency"]) == ("mixed", 75000, 34900, "usd")


# ── #7 a failed write is retried, and the alert never lies ──────────────────

def test_a_cancellation_whose_write_fails_is_redelivered_and_then_revokes(db_path, hook, monkeypatch, _world):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    monkeypatch.setattr(webhook_routes, "update_restaurant",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    ev = _evt("evt_cxl", "customer.subscription.deleted", _sub("sub_1", rid=rid, status="canceled"))
    assert hook(ev).status_code == 500
    assert _count(db_path, "SELECT COUNT(*) FROM stripe_events_seen WHERE event_id='evt_cxl'") == 0
    assert not [a for a in _world if "revoked" in a[1]], "no alert may claim a revocation that did not happen"
    monkeypatch.setattr(webhook_routes, "update_restaurant", models.update_restaurant)
    assert hook(ev).status_code == 200
    assert get_restaurant(rid, db_path).billing_status == "churned"
    assert [a for a in _world if "revoked" in a[1]]


# ── #115 match by ids, never email ──────────────────────────────────────────

def test_a_cancellation_is_matched_by_subscription_id_not_email(db_path, hook):
    paying = _rid(db_path, name="Paying", billing_status="active", stripe_customer_id="cus_pay")
    bystander = _rid(db_path, name="Bystander", owner_email="shared@x.test", billing_status="active")
    billing_jobs.upsert_subscription(paying, subscription_id="sub_pay", facts={"status": "active"})
    ev = _evt("evt_d", "customer.subscription.deleted",
              _sub("sub_pay", status="canceled", customer="cus_other", customer_email="shared@x.test"))
    hook(ev)
    assert get_restaurant(paying, db_path).billing_status == "churned"
    assert get_restaurant(bystander, db_path).billing_status == "active"


def test_an_invoice_matched_only_by_email_changes_nothing(db_path, hook):
    rid = _rid(db_path, owner_email="own@x.test", billing_status="past_due", stripe_customer_id="cus_real")
    hook(_evt("evt_p", "invoice.paid", {"id": "in_x", "customer": "cus_stranger", "customer_email": "own@x.test",
                                        "amount_paid": 34900, "billing_reason": "subscription_cycle"}))
    r = get_restaurant(rid, db_path)
    assert r.billing_status == "past_due" and r.stripe_customer_id == "cus_real"


def test_a_stored_customer_id_is_never_overwritten_by_an_invoice(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_real")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    hook(_evt("evt_p", "invoice.paid", {"id": "in_1", "customer": "cus_other", "subscription": "sub_1",
                                        "amount_paid": 34900, "billing_reason": "subscription_cycle"}))
    assert get_restaurant(rid, db_path).stripe_customer_id == "cus_real"


def test_an_unpaid_end_of_dunning_lets_a_later_checkout_through(db_path, hook):
    """COMMS-8: Stripe's final dunning action left the old row, and the
    returning client's new subscription was cancelled as a second checkout."""
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_old", facts={"status": "active"})
    hook(_evt("evt_u", "customer.subscription.updated", _sub("sub_old", rid=rid, status="unpaid"), created=1000))
    assert get_restaurant(rid, db_path).billing_status == "churned"
    hook(_evt("evt_co", "checkout.session.completed",
              {"id": "cs_new", "customer": "cus_2", "subscription": "sub_new",
               "metadata": {"restaurant_id": str(rid), "module_keys": "reviews"}}, created=2000))
    assert ("Subscription.cancel", "sub_new") not in hook.stripe.calls
    assert get_restaurant(rid, db_path).billing_status == "active"
    assert billing_jobs.mirror_row(rid)["subscription_id"] == "sub_new"


def test_an_old_subscription_ending_does_not_churn_the_current_one(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_now", facts={"status": "active"})
    hook(_evt("evt_d", "customer.subscription.deleted", _sub("sub_then", rid=rid, status="canceled")))
    assert get_restaurant(rid, db_path).billing_status == "active"


def test_the_duplicate_checkouts_refund_and_cancellation_touch_nothing(db_path, hook):
    """COMMS-19: the refund Will is told to issue paused the paying account."""
    rid = _rid(db_path, billing_status="pending")
    meta = {"restaurant_id": str(rid), "module_keys": "reviews"}
    hook(_evt("e1", "checkout.session.completed", {"id": "cs_1", "customer": "cus_1", "subscription": "sub_1",
                                                   "metadata": meta}, created=1000))
    hook(_evt("e2", "checkout.session.completed", {"id": "cs_2", "customer": "cus_dup", "subscription": "sub_dup",
                                                   "metadata": meta}, created=1001))
    assert ("Subscription.cancel", "sub_dup") in hook.stripe.calls
    hook(_evt("e3", "invoice.paid", {"id": "in_dup", "customer": "cus_dup", "subscription": "sub_dup",
                                     "amount_paid": 75000, "billing_reason": "subscription_create",
                                     "charge": "ch_dup"}, created=1002))
    hook(_evt("e4", "charge.refunded", {"id": "ch_dup", "customer": "cus_dup", "amount": 75000,
                                        "amount_refunded": 75000, "refunded": True, "invoice": "in_dup"},
              created=1003))
    hook(_evt("e5", "customer.subscription.deleted", _sub("sub_dup", status="canceled", customer="cus_dup"),
              created=1004))
    r = get_restaurant(rid, db_path)
    assert r.billing_status == "active" and r.pause_reason is None and r.stripe_customer_id == "cus_1"
    assert _count(db_path, "SELECT COUNT(*) FROM owed_sends WHERE kind='receipt'") == 0
    # The duplicate's events never moved the paying restaurant's clock: a real
    # failure created before them but delivered after is still applied.
    hook(_evt("e6", "invoice.payment_failed", {"id": "in_real", "customer": "cus_1", "subscription": "sub_1",
                                               "amount_due": 34900, "attempt_count": 1}, created=1001))
    assert get_restaurant(rid, db_path).billing_status == "past_due"


# ── #114 holds only an admin lifts ──────────────────────────────────────────

def _group(db_path, n=2):
    ids = [_rid(db_path, name=f"Loc {i}", owner_email="g@x.test", location_group="Grp",
                billing_status="active") for i in range(n)]
    update_restaurant(ids[0], {"stripe_customer_id": "cus_g"}, db_path=db_path)
    billing_jobs.upsert_subscription(ids[0], subscription_id="sub_g", facts={"status": "active"})
    return ids


def test_a_dispute_holds_the_whole_group_with_its_reason(db_path, hook):
    a, b = _group(db_path)
    hook(_evt("e_d", "charge.dispute.created", {"id": "dp_1", "charge": "ch_1", "customer": "cus_g",
                                                "amount": 34900}))
    for rid in (a, b):
        r = get_restaurant(rid, db_path)
        assert (r.billing_status, r.pause_reason) == ("paused", "dispute")


def test_a_full_refund_holds_only_the_location_that_paid(db_path, hook):
    a, b = _group(db_path)
    hook(_evt("e_r", "charge.refunded", {"id": "ch_1", "customer": "cus_g", "amount": 34900,
                                         "amount_refunded": 34900, "refunded": True}))
    assert (get_restaurant(a, db_path).billing_status, get_restaurant(a, db_path).pause_reason) == ("paused", "refund")
    assert get_restaurant(b, db_path).billing_status == "active"


def test_a_dispute_without_a_customer_is_resolved_through_its_charge(db_path, hook):
    """A real Dispute object carries only its charge id."""
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.record_invoice({"id": "in_1", "customer": "cus_1", "charge": "ch_9", "status": "paid"},
                                restaurant_id=rid)
    hook(_evt("e_d", "charge.dispute.created", {"id": "dp_1", "charge": "ch_9", "amount": 34900}))
    assert get_restaurant(rid, db_path).pause_reason == "dispute"


def test_stripe_saying_active_does_not_lift_a_dispute_hold(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    hook(_evt("e_d", "charge.dispute.created", {"id": "dp", "customer": "cus_1", "amount": 1}, created=1000))
    hook(_evt("e_u", "customer.subscription.updated", _sub("sub_1", rid=rid), created=1001))
    hook(_evt("e_p", "invoice.paid", {"id": "in_2", "customer": "cus_1", "subscription": "sub_1",
                                      "amount_paid": 34900, "billing_reason": "subscription_cycle"}, created=1002))
    r = get_restaurant(rid, db_path)
    assert (r.billing_status, r.pause_reason) == ("paused", "dispute")


def test_a_hold_survives_churn_and_meets_the_returning_payment(db_path, hook, _world):
    """A client who disputed a charge and then cancelled keeps the hold: a
    new checkout or a paid invoice does not turn the account back on."""
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    hook(_evt("e_d", "charge.dispute.created", {"id": "dp", "customer": "cus_1", "amount": 1}, created=1000))
    hook(_evt("e_x", "customer.subscription.deleted", _sub("sub_1", rid=rid, status="canceled"), created=1001))
    r = get_restaurant(rid, db_path)
    assert (r.billing_status, r.pause_reason) == ("churned", "dispute")
    assert models.pause_lock(r) is None and models.billing_hold(r) == "dispute"
    hook(_evt("e_co", "checkout.session.completed", {"id": "cs2", "customer": "cus_2", "subscription": "sub_2",
                                                     "metadata": {"restaurant_id": str(rid)}}, created=2000))
    hook(_evt("e_p", "invoice.paid", {"id": "in_2", "customer": "cus_2", "subscription": "sub_2",
                                      "amount_paid": 34900, "billing_reason": "subscription_create"}, created=2001))
    assert get_restaurant(rid, db_path).billing_status == "churned"
    assert [a for a in _world if "Lift hold" in a[1]]


def test_a_self_serve_pause_is_still_the_owners_to_end(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    hook(_evt("e1", "customer.subscription.updated",
              _sub("sub_1", rid=rid, pause_collection={"behavior": "void", "resumes_at": 1790000000}), created=1000))
    r = get_restaurant(rid, db_path)
    assert (r.billing_status, r.pause_reason) == ("paused", "self") and models.pause_lock(r) is None
    hook(_evt("e2", "customer.subscription.updated", _sub("sub_1", rid=rid, pause_collection=None), created=1001))
    assert get_restaurant(rid, db_path).billing_status == "active"


# ── #116 one subscription per group ─────────────────────────────────────────

def test_a_failed_card_moves_the_locations_it_covers_and_not_one_billed_on_its_own(db_path, hook):
    a, b = _group(db_path)
    c = _rid(db_path, name="Loc own", owner_email="g@x.test", location_group="Grp", billing_status="active")
    billing_jobs.upsert_subscription(c, subscription_id="sub_c", facts={"status": "active"})
    hook(_evt("e_f", "invoice.payment_failed", {"id": "in_f", "customer": "cus_g", "subscription": "sub_g",
                                                "amount_due": 34900, "attempt_count": 1}))
    assert get_restaurant(a, db_path).billing_status == "past_due"
    assert get_restaurant(b, db_path).billing_status == "past_due"
    assert get_restaurant(c, db_path).billing_status == "active"
    assert billing_jobs.billed_by(b) == a and billing_jobs.billed_by(c) == c


# ── #15 / #51 the mirror and converted_at ───────────────────────────────────

def test_the_mirror_holds_what_stripe_bills(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1", module_reviews=1)
    sub = _sub("sub_1", rid=rid, status="trialing", keys="reviews", trial_end=1790000000,
               current_period_end=1790000000, discount={"coupon": {"percent_off": 10}})
    sub["items"]["data"][0]["price"].update(unit_amount=349000, recurring={"interval": "year", "interval_count": 1})
    hook(_evt("e_c", "customer.subscription.created", sub))
    m = billing_jobs.mirror_row(rid)
    assert (m["status"], m["interval"], m["amount_cents"], m["discount_pct"], m["module_keys"]) == \
        ("trialing", "year", 349000, 10.0, "reviews")
    assert m["trial_end"] and m["current_period_end"]
    assert billing_jobs.billed_mrr(m) == 0.0                      # trialing: committed, not billed
    m["status"] = "active"
    assert billing_jobs.billed_mrr(m) == round(3490 * 0.9 / 12, 2)


def test_checkout_stamps_converted_at_once(db_path, hook):
    rid = _rid(db_path, billing_status="pending")
    hook(_evt("e1", "checkout.session.completed", {"id": "cs", "customer": "cus_1", "subscription": "sub_1",
                                                   "payment_status": "paid",
                                                   "metadata": {"restaurant_id": str(rid)}}))
    first = get_restaurant(rid, db_path).converted_at
    assert first
    hook(_evt("e2", "invoice.paid", {"id": "in_2", "customer": "cus_1", "subscription": "sub_1",
                                     "amount_paid": 34900, "billing_reason": "subscription_cycle"}, created=2000))
    assert get_restaurant(rid, db_path).converted_at == first


def test_cancellation_facts_are_kept_on_the_mirror(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    hook(_evt("e_d", "customer.subscription.deleted",
              _sub("sub_1", rid=rid, status="canceled", canceled_at=1790000000, ended_at=1790000100,
                   cancellation_details={"reason": "cancellation_requested", "feedback": "too_expensive"})))
    m = billing_jobs.mirror_row(rid)
    assert m["status"] == "canceled" and m["canceled_at"] and m["ended_at"]
    assert m["cancellation_reason"] == "cancellation_requested · too_expensive"
    assert not billing_jobs.is_live_status(m["status"])
    assert [h["new_value"] for h in _history(db_path, rid, "billing_status")][-1] == "churned"


# ── #106 modules follow Stripe only on a real plan change ───────────────────

def test_a_renewal_no_longer_reverts_a_module_added_in_the_console(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1",
               module_reviews=1, module_labor=1, module_inventory=0, module_marketing=0)
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    hook(_evt("e_renew", "customer.subscription.updated", _sub("sub_1", rid=rid, keys="reviews"),
              prev={"current_period_end": 1}))
    r = get_restaurant(rid, db_path)
    assert r.module_labor == 1, "Labor survived the renewal"
    mm = json.loads(billing_jobs.mirror_row(rid)["module_mismatch"])
    assert mm["stripe"] == ["reviews"] and mm["local"] == ["labor", "reviews"]


def test_a_plan_change_made_in_stripe_is_applied(db_path, hook):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1",
               module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    hook(_evt("e_plan", "customer.subscription.updated", _sub("sub_1", rid=rid, keys="reviews,labor"),
              prev={"metadata": {"module_keys": "reviews"}}))
    assert get_restaurant(rid, db_path).module_labor == 1
    assert billing_jobs.mirror_row(rid)["module_mismatch"] is None


# ── #155 receipts and the conversion alert ─────────────────────────────────

def _retainer_invoice(iid, amount=34900, reason="subscription_cycle", sub="sub_1"):
    return {"id": iid, "customer": "cus_1", "subscription": sub, "amount_paid": amount, "status": "paid",
            "billing_reason": reason, "lines": {"data": [{"amount": amount, "type": "subscription"}]}}


def test_one_receipt_per_paid_invoice_whatever_the_order(db_path, hook):
    rid = _rid(db_path, billing_status="pending")
    # invoice.paid for the first invoice arrives BEFORE the checkout event.
    setup = {"id": "in_setup", "customer": "cus_1", "subscription": "sub_1", "amount_paid": 75000, "status": "paid",
             "billing_reason": "subscription_create",
             "subscription_details": {"metadata": {"restaurant_id": str(rid)}},
             "lines": {"data": [{"amount": 75000, "type": "invoiceitem"}, {"amount": 0, "type": "subscription"}]}}
    hook(_evt("e1", "invoice.paid", setup, created=1000))
    hook(_evt("e2", "checkout.session.completed", {"id": "cs", "customer": "cus_1", "subscription": "sub_1",
                                                   "metadata": {"restaurant_id": str(rid)}}, created=1001))
    hook(_evt("e3", "invoice.paid", _retainer_invoice("in_ret"), created=2000))
    receipts = _rows(db_path, "SELECT dedupe_key FROM owed_sends WHERE kind='receipt' ORDER BY id")
    assert [r["dedupe_key"] for r in receipts] == ["receipt:in_setup", "receipt:in_ret"]
    assert get_restaurant(rid, db_path).billing_status == "active"


def test_the_conversion_alert_fires_on_the_first_retainer_invoice_only(db_path, hook, _world):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "trialing"})
    hook(_evt("e1", "invoice.paid", {"id": "in_s", "customer": "cus_1", "subscription": "sub_1",
                                     "amount_paid": 75000, "status": "paid", "billing_reason": "subscription_create",
                                     "lines": {"data": [{"amount": 75000, "type": "invoiceitem"}]}}, created=1000))
    assert not [a for a in _world if "New paying client" in a[0]], "a setup fee is not a retainer"
    hook(_evt("e2", "invoice.paid", _retainer_invoice("in_r1"), created=2000))
    hook(_evt("e3", "invoice.paid", _retainer_invoice("in_r2"), created=3000))
    assert len([a for a in _world if "New paying client" in a[0]]) == 1


# ── #25 dunning owed on attempts 1–3, stood down when paid ─────────────────

def test_dunning_is_owed_to_the_owner_and_every_principal_login_on_attempts_1_to_3(db_path, hook):
    rid = _rid(db_path, owner_email="own@x.test", billing_status="active", stripe_customer_id="cus_1")
    create_user(rid, "own", "OWN@x.test", "pw-owner-1", db_path=db_path)
    create_user(rid, "gm", "gm@x.test", "pw-gm-12345", db_path=db_path)
    for attempt in (1, 2, 3, 4):
        hook(_evt(f"e{attempt}", "invoice.payment_failed",
                  {"id": "in_1", "customer": "cus_1", "amount_due": 34900, "attempt_count": attempt,
                   "hosted_invoice_url": "https://invoice.stripe.com/i/1"}, created=1000 + attempt))
    rows = _rows(db_path, "SELECT dedupe_key, to_email, restaurant_id FROM owed_sends WHERE kind='dunning'")
    keys = sorted(r["dedupe_key"] for r in rows)
    assert keys == sorted(f"dunning:in_1:{a}:{e}" for a in (1, 2, 3) for e in ("own@x.test", "gm@x.test"))
    assert {r["restaurant_id"] for r in rows} == {rid}
    assert get_restaurant(rid, db_path).billing_status == "past_due"
    # Paid: the rest of the dunning stands down and the account recovers.
    hook(_evt("e_paid", "invoice.paid", {"id": "in_1", "customer": "cus_1", "amount_paid": 34900,
                                         "billing_reason": "subscription_cycle"}, created=2000))
    assert _count(db_path, "SELECT COUNT(*) FROM owed_sends WHERE kind='dunning' AND status='pending'") == 0


def test_no_dunning_is_owed_to_an_account_on_hold(db_path, hook):
    rid = _rid(db_path, billing_status="paused", pause_reason="dispute", stripe_customer_id="cus_1")
    hook(_evt("e1", "invoice.payment_failed", {"id": "in_1", "customer": "cus_1", "amount_due": 34900,
                                               "attempt_count": 1}))
    assert _count(db_path, "SELECT COUNT(*) FROM owed_sends WHERE kind='dunning'") == 0
    assert get_restaurant(rid, db_path).billing_status == "paused"
