"""Edge cases for Billing & Contracts: Stripe and DocuSign webhooks, checkout
provisioning, payment links, and what a cancelled customer still costs us.

Every test comes from the MOD edge-case audit (appendix A10, findings
MOD-BIL-1..10 and the background-job half of MOD-REV-2). The promises being
protected: billing state only moves the way Stripe's current truth says it
should (never backwards on a late event, never to a lockout on a courtesy
refund), no payment or contract event is dropped by our own bookkeeping,
signing a contract never resets a working owner's password, and a churned
restaurant stops costing Places, Claude, Twilio and Resend money.

The stripe SDK is replaced by a stand-in module; Resend, Twilio, DocuSign and
Meta are stubbed. xfail(strict=True) tests assert the CORRECT behaviour for a
defect the audit confirmed and flip to a failure the day it is fixed.
"""
import sqlite3
import sys
import types
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import emails
import models
import notify
import ops
import provisioning
import push
import scheduler
import webhook_routes
import webhooks
from auth import create_user, init_auth
from models import Restaurant, Review, create_restaurant, get_conn, get_restaurant, save_reviews, update_restaurant


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
    for mod in (models, auth, webhook_routes, provisioning, notify, push, webhooks, ops, scheduler):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    _real_all = models.get_all_restaurants
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: _real_all(db_path=db_path))
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    webhooks.init_webhooks(db_path)
    monkeypatch.setattr(webhook_routes, "_resend_key", lambda: "")      # alerts to Will print, never send


def _restaurant(db_path, name="Billing Co", owner_email="o@x.test", **kw):
    """create_restaurant's INSERT is a fixed column list, so everything else
    (billing, Stripe/DocuSign ids, modules, group) goes through
    update_restaurant the way admin and the webhooks write it."""
    rid = create_restaurant(Restaurant(name=name, owner_email=owner_email), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _status(db_path, rid):
    return (get_restaurant(rid, db_path).billing_status or "").lower()


# ── Stripe through the real route ───────────────────────────────────────────

class _FakeStripe:
    """The route calls exactly one SDK function; everything else is here
    only for the checkout-creation tests further down."""

    def __init__(self):
        self.event = None
        self.calls = []
        mod = types.ModuleType("stripe")
        fake = self

        mod.Webhook = type("W", (), {"construct_event": staticmethod(lambda *a, **k: fake.event)})

        class _Obj(dict):
            __getattr__ = dict.get

        class Product:
            _made = {}

            @staticmethod
            def search(query=None, limit=1):
                fake.calls.append(("Product.search", query))
                name = (query or "").split('"')[1] if '"' in (query or "") else ""
                pid = Product._made.get(name)
                return _Obj(data=[_Obj(id=pid)] if pid else [])

            @staticmethod
            def create(name=None, **k):
                fake.calls.append(("Product.create", name))
                Product._made[name] = f"prod_{len(Product._made) + 1}"
                return _Obj(id=Product._made[name])

        class Price:
            @staticmethod
            def create(**k):
                fake.calls.append(("Price.create", k.get("lookup_key")))
                return _Obj(id=f"price_{len(fake.calls)}")

            @staticmethod
            def list(**k):
                fake.calls.append(("Price.list", k.get("lookup_keys")))
                return _Obj(data=[])

        class Session:
            @staticmethod
            def create(**k):
                fake.calls.append(("Session.create", k.get("customer_email")))
                return _Obj(url=f"https://checkout.stripe.com/c/pay/cs_{len(fake.calls)}", id=f"cs_{len(fake.calls)}")

            @staticmethod
            def expire(sid, **k):
                fake.calls.append(("Session.expire", sid))

        class Subscription:
            @staticmethod
            def cancel(sid, **k):
                fake.calls.append(("Subscription.cancel", sid))

            @staticmethod
            def delete(sid, **k):
                fake.calls.append(("Subscription.delete", sid))

            @staticmethod
            def retrieve(sid, **k):
                fake.calls.append(("Subscription.retrieve", sid))
                return _Obj(id=sid, status="canceled")

        mod.Product, mod.Price, mod.Subscription = Product, Price, Subscription
        mod.checkout = types.SimpleNamespace(Session=Session)
        mod.api_key = ""
        self.module = mod


@pytest.fixture
def stripe_fake(monkeypatch):
    fake = _FakeStripe()
    monkeypatch.setitem(sys.modules, "stripe", fake.module)
    return fake


@pytest.fixture
def hook(stripe_fake):
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    client = app.test_client()

    def post(event):
        stripe_fake.event = event
        return client.post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})
    post.client = client
    return post


def _evt(eid, etype, obj, created):
    return {"id": eid, "type": etype, "created": created, "data": {"object": obj}}


@pytest.mark.xfail(strict=True, reason="MOD-BIL-1: a delayed invoice.paid re-activates a cancelled customer")
def test_a_delayed_invoice_paid_does_not_reactivate_a_cancelled_customer(db_path, hook):
    """A10 #13 / MOD-BIL-1 — Stripe does not guarantee order; an invoice
    created before the cancellation arrives after it."""
    rid = _restaurant(db_path, billing_status="active", stripe_customer_id="cus_1")
    hook(_evt("evt_del", "customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1"}, 2000))
    assert _status(db_path, rid) == "churned"
    hook(_evt("evt_inv", "invoice.paid", {"customer": "cus_1", "customer_email": "o@x.test", "amount_paid": 34900,
                                         "billing_reason": "subscription_cycle", "subscription": "sub_1"}, 1000))
    assert _status(db_path, rid) == "churned"


@pytest.mark.xfail(strict=True, reason="MOD-BIL-1: a stale subscription.updated(active) re-activates a cancelled customer and restores all modules")
def test_a_stale_subscription_update_does_not_reactivate_a_cancelled_customer(db_path, hook):
    """A10 #14 / MOD-BIL-1."""
    rid = _restaurant(db_path, billing_status="active", stripe_customer_id="cus_1", module_reviews=1)
    hook(_evt("evt_del", "customer.subscription.deleted", {"id": "sub_1", "customer": "cus_1"}, 2000))
    hook(_evt("evt_upd", "customer.subscription.updated",
              {"id": "sub_1", "customer": "cus_1", "status": "active",
               "metadata": {"restaurant_id": str(rid), "module_keys": "reviews,labor,inventory,marketing"}}, 1000))
    assert _status(db_path, rid) == "churned"


@pytest.mark.xfail(strict=True, reason="MOD-BIL-2: any refund, even a $5 partial credit, pauses every location billed to the customer")
def test_a_small_partial_refund_does_not_lock_a_paying_customer_out(db_path, hook):
    """A10 #15 / MOD-BIL-2 — a $5 goodwill credit on a $750 charge."""
    a = _restaurant(db_path, name="Group A", billing_status="active", stripe_customer_id="cus_1",
                    location_group="Group")
    b = _restaurant(db_path, name="Group B", billing_status="active", location_group="Group")
    hook(_evt("evt_ref", "charge.refunded", {"customer": "cus_1", "amount": 75000, "amount_refunded": 500,
                                             "refunded": False}, 1000))
    assert _status(db_path, a) == "active" and _status(db_path, b) == "active"


def test_a_chargeback_pauses_the_account(db_path, hook):
    """A10 #16 — the half that works: a dispute is a lockout."""
    rid = _restaurant(db_path, billing_status="active", stripe_customer_id="cus_1")
    hook(_evt("evt_dsp", "charge.dispute.created", {"customer": "cus_1", "amount": 75000}, 1000))
    assert _status(db_path, rid) == "paused"


@pytest.mark.xfail(strict=True, reason="MOD-BIL-2: a later invoice.payment_failed silently lifts a chargeback pause to past_due")
def test_a_failed_payment_does_not_lift_a_chargeback_pause(db_path, hook):
    """A10 #16 / MOD-BIL-2 — past_due is an allowed state; a disputing
    customer must not regain access because their card then failed."""
    rid = _restaurant(db_path, billing_status="active", stripe_customer_id="cus_1")
    hook(_evt("evt_dsp", "charge.dispute.created", {"customer": "cus_1", "amount": 75000}, 1000))
    hook(_evt("evt_fail", "invoice.payment_failed", {"customer": "cus_1", "customer_email": "o@x.test",
                                                    "amount_due": 34900, "attempt_count": 1}, 2000))
    assert _status(db_path, rid) == "paused"


class _FailingInsert:
    def __init__(self, conn, table):
        self._c, self._t = conn, table

    def execute(self, sql, *a, **k):
        if sql.strip().startswith(f"INSERT INTO {self._t}"):
            raise sqlite3.OperationalError("database is locked")
        return self._c.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


def test_a_bookkeeping_failure_on_the_stripe_claim_is_not_a_duplicate(db_path, monkeypatch):
    """A10 #11 / MOD-BIL-3 — the claim must fail open (or the route 500 so
    Stripe retries), never answer 'already handled' for a new event."""
    real = models.get_conn
    monkeypatch.setattr(webhook_routes, "get_conn", lambda *a, **k: _FailingInsert(real(db_path), "stripe_events_seen"))
    assert webhook_routes._claim_stripe_event("evt_never_seen") is True


def test_a_bookkeeping_failure_on_the_docusign_claim_is_not_a_duplicate(db_path, monkeypatch):
    """A10 #11 / MOD-BIL-3."""
    real = models.get_conn
    monkeypatch.setattr(webhook_routes, "get_conn", lambda *a, **k: _FailingInsert(real(db_path), "docusign_events_seen"))
    assert webhook_routes._claim_docusign_event("env_never_seen", "completed") is True


def test_a_genuine_duplicate_stripe_event_is_still_refused(db_path):
    """A10 #11 — the dedupe the claim exists for keeps working."""
    assert webhook_routes._claim_stripe_event("evt_dup") is True
    assert webhook_routes._claim_stripe_event("evt_dup") is False


def test_a_checkout_whose_activation_failed_is_activated_on_stripes_retry(db_path, hook, monkeypatch):
    """A10 #12 / MOD-BIL-4 — update_restaurant raises once (a locked DB);
    the redelivered event must still activate the customer."""
    rid = _restaurant(db_path, billing_status="pending")
    real_update = webhook_routes.update_restaurant
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_update(*a, **k)
    monkeypatch.setattr(webhook_routes, "update_restaurant", flaky)
    ev = _evt("evt_co", "checkout.session.completed",
              {"customer": "cus_9", "subscription": "sub_9", "customer_email": "o@x.test",
               "metadata": {"restaurant_id": str(rid), "module_keys": "reviews"}}, 1000)
    hook(ev)
    hook(ev)
    assert _status(db_path, rid) == "active"
    assert get_restaurant(rid, db_path).stripe_customer_id == "cus_9"


# ── Provisioning from checkout ──────────────────────────────────────────────

def _session(email="new@owner.test", name="New Place", keys="reviews"):
    return {"customer": "cus_new", "customer_details": {"email": email, "name": "Pat"},
            "metadata": {"restaurant": name, "module_keys": keys}}


def _count(db_path, sql, *args):
    conn = get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="MOD-BIL-9: a failed user insert during provisioning leaves an orphan restaurant")
def test_provisioning_that_fails_to_create_the_login_leaves_no_restaurant_behind(db_path, monkeypatch):
    """A10 #26 / MOD-BIL-9."""
    monkeypatch.setattr(emails, "send_welcome_email", lambda **k: None)

    def boom(*a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: users.email")
    monkeypatch.setattr(auth, "create_user", boom)
    before = _count(db_path, "SELECT COUNT(*) FROM restaurants")
    with pytest.raises(Exception):
        provisioning.provision_from_checkout(_session(), db_path=db_path)
    assert _count(db_path, "SELECT COUNT(*) FROM restaurants") == before


@pytest.mark.xfail(strict=True, reason="MOD-BIL-9: two concurrent deliveries of one checkout both pass the email check and orphan a restaurant")
def test_a_checkout_replayed_concurrently_provisions_one_restaurant(db_path, monkeypatch):
    """A10 #26 / MOD-BIL-9 — the second delivery arrives between the first
    one's email check and its user insert."""
    monkeypatch.setattr(emails, "send_welcome_email", lambda **k: None)
    real_create = models.create_restaurant
    state = {"nested": False}

    def interleaved(*a, **k):
        rid = real_create(*a, **k)
        if not state["nested"]:
            state["nested"] = True
            try:
                provisioning.provision_from_checkout(_session(), db_path=db_path)
            except Exception:
                pass
        return rid
    monkeypatch.setattr(models, "create_restaurant", interleaved)
    try:
        provisioning.provision_from_checkout(_session(), db_path=db_path)
    except Exception:
        pass
    assert _count(db_path, "SELECT COUNT(*) FROM restaurants WHERE owner_email='new@owner.test'") == 1


@pytest.mark.xfail(strict=True, reason="MOD-BIL-9: an owner who already has a login is never provisioned a second location from checkout")
def test_an_existing_owner_buying_a_second_location_gets_it_provisioned(db_path, monkeypatch):
    """A10 #27 / MOD-BIL-9."""
    monkeypatch.setattr(emails, "send_welcome_email", lambda **k: None)
    first = _restaurant(db_path, name="First Place", owner_email="own@x.test")
    create_user(first, "own", "own@x.test", "pw-owner-1", db_path=db_path)
    new_rid = provisioning.provision_from_checkout(_session(email="own@x.test", name="Second Place"),
                                                   db_path=db_path)
    assert new_rid and new_rid != first
    assert get_restaurant(new_rid, db_path).name == "Second Place"


# ── DocuSign ────────────────────────────────────────────────────────────────

@pytest.fixture
def ds(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    client = app.test_client()
    mail = {"payment": [], "welcome": []}
    monkeypatch.setattr(webhook_routes, "_resend_key", lambda: "k")
    monkeypatch.setattr(webhook_routes, "send_payment_email", lambda **k: mail["payment"].append(k["to_email"]))
    monkeypatch.setattr(webhook_routes, "send_welcome_email", lambda **k: mail["welcome"].append(k["to_email"]))
    monkeypatch.delenv("DOCUSIGN_WEBHOOK_SECRET", raising=False)

    def post(envelope_id, status="completed", headers=None):
        return client.post("/docusign/webhook", json={"envelopeId": envelope_id, "status": status},
                           headers=headers or {})
    post.mail = mail
    return post


def _password_hash(db_path, uid):
    return _count(db_path, "SELECT password_hash FROM users WHERE id=?", uid)


@pytest.mark.xfail(strict=True, reason="MOD-BIL-7: with DOCUSIGN_WEBHOOK_SECRET unset the DocuSign webhook processes unsigned bodies")
def test_the_docusign_webhook_refuses_an_unsigned_body_when_no_secret_is_configured(db_path, ds):
    """A10 #30 / MOD-BIL-7 — docs/ops/SECURITY.md says webhooks are rejected
    without their secret; an unsigned 'completed' must not mark a contract
    signed or reset a password."""
    rid = _restaurant(db_path, docusign_envelope_id="env_1", contract_status="sent")
    uid = create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    before = _password_hash(db_path, uid)
    resp = ds("env_1")
    assert resp.status_code in (401, 403, 503)
    assert get_restaurant(rid, db_path).contract_status == "sent"
    assert _password_hash(db_path, uid) == before


def test_a_signed_contract_sends_the_payment_link_and_the_welcome_once(db_path, ds, monkeypatch):
    """A10 #31 — the whole completion through the real route: contract
    marked signed, one payment email, one welcome with a fresh password, and
    a repeat delivery changes nothing."""
    monkeypatch.setenv("DOCUSIGN_WEBHOOK_SECRET", "s3cret")
    import base64
    import hashlib
    import hmac
    import json
    rid = _restaurant(db_path, docusign_envelope_id="env_1", contract_status="sent", module_reviews=1)
    uid = create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    before = _password_hash(db_path, uid)
    body = json.dumps({"envelopeId": "env_1", "status": "completed"}).encode()
    sig = base64.b64encode(hmac.new(b"s3cret", body, hashlib.sha256).digest()).decode()
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    client = app.test_client()
    for _ in range(2):
        resp = client.post("/docusign/webhook", data=body, content_type="application/json",
                           headers={"X-DocuSign-Signature-1": sig})
        assert resp.status_code == 200
    assert get_restaurant(rid, db_path).contract_status == "signed"
    assert ds.mail["payment"] == ["o@x.test"]
    assert ds.mail["welcome"] == ["o@x.test"]
    assert _password_hash(db_path, uid) != before


@pytest.mark.xfail(strict=True, reason="MOD-BIL-6: signing a re-sent contract resets an active owner's password and re-sends the setup-fee payment link")
def test_a_second_envelope_for_an_active_client_neither_resets_the_password_nor_rebills(db_path, ds):
    """A10 #32 / MOD-BIL-6."""
    rid = _restaurant(db_path, docusign_envelope_id="env_2", contract_status="sent", billing_status="active",
                      module_reviews=1)
    uid = create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    before = _password_hash(db_path, uid)
    ds("env_2")
    assert _password_hash(db_path, uid) == before
    assert ds.mail["payment"] == [] and ds.mail["welcome"] == []


@pytest.mark.xfail(strict=True, reason="MOD-BIL-6: a client who signs the superseded envelope hits a dead end — nothing matches it")
def test_signing_the_superseded_envelope_still_counts(db_path, ds):
    """A10 #33 / MOD-BIL-6 — the client signed the first email's envelope
    after Will re-sent the contract."""
    rid = _restaurant(db_path, docusign_envelope_id="env_new", contract_status="sent", module_reviews=1)
    create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    ds("env_old")
    assert get_restaurant(rid, db_path).contract_status == "signed"


# ── Payment links ───────────────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-BIL-5: payment emails embed raw Stripe Checkout Session URLs that expire and cannot be regenerated")
def test_the_payment_email_links_to_a_cavnar_pay_route_not_a_raw_checkout_session(db_path, monkeypatch):
    """A10 #35 / MOD-BIL-5 — an owner opening onboarding mail days later
    must land somewhere that mints a fresh session."""
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(emails, "create_stripe_checkout",
                        lambda n, email, name, period="monthly", **k: f"https://checkout.stripe.com/c/pay/cs_{period}")
    captured = []
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: captured.append(payload) or True)
    emails.send_payment_email("o@x.test", "Billing Co", module_count=1, restaurant_id=1, modules=["reviews"])
    html = captured[0]["html"]
    assert "checkout.stripe.com" not in html


@pytest.mark.xfail(strict=True, reason="MOD-BIL-5: completing both the monthly and the annual checkout creates two subscriptions and two setup charges")
def test_completing_both_plans_does_not_leave_two_live_subscriptions(db_path, hook, stripe_fake):
    """A10 #36 / MOD-BIL-5 — the owner opened both tabs and paid in both.
    The second subscription must be cancelled (or never allowed to form)."""
    rid = _restaurant(db_path, billing_status="pending")
    meta = {"restaurant_id": str(rid), "module_keys": "reviews"}
    hook(_evt("evt_m", "checkout.session.completed", {"id": "cs_m", "customer": "cus_1", "subscription": "sub_m",
                                                      "customer_email": "o@x.test", "metadata": meta}, 1000))
    hook(_evt("evt_a", "checkout.session.completed", {"id": "cs_a", "customer": "cus_1", "subscription": "sub_a",
                                                      "customer_email": "o@x.test", "metadata": meta}, 1001))
    stopped = [c for c in stripe_fake.calls if c[0] in ("Subscription.cancel", "Subscription.delete",
                                                         "Session.expire")]
    assert stopped, "both subscriptions stay live and both setup fees are charged"


@pytest.mark.xfail(strict=True, reason="MOD-BIL-10: every checkout creates new Stripe Prices instead of reusing one per tier")
def test_checkout_creation_reuses_prices_instead_of_creating_new_ones(db_path, stripe_fake, monkeypatch):
    """MOD-BIL-10 — two payment emails for the same tier create the setup
    and retainer Prices once, not twice."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    emails.create_stripe_checkout(1, "a@x.test", "A", "monthly", restaurant_id=1, modules=["reviews"])
    emails.create_stripe_checkout(1, "b@x.test", "B", "monthly", restaurant_id=2, modules=["reviews"])
    assert len([c for c in stripe_fake.calls if c[0] == "Price.create"]) <= 2


# ── A churned restaurant is inert in the background ─────────────────────────

@pytest.fixture
def sent(monkeypatch):
    out = {"email": [], "push": [], "sms": []}
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": out["sms"].append(to) or True)
    monkeypatch.setattr(notify, "_email_alert", lambda rid, *a, **k: out["email"].append(rid))
    monkeypatch.setattr(push, "fire_push", lambda rid, *a, **k: out["push"].append(rid))
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    return out


def test_a_churned_restaurant_gets_no_review_alerts(db_path, sent):
    """A10 #34 / MOD-REV-2 — no SMS/email/push spend on a cancelled customer."""
    rid = _restaurant(db_path, billing_status="churned")
    update_restaurant(rid, {"urgent_via_email": 1, "alert_1star": 1}, db_path=db_path)
    _n, saved = save_reviews([Review(restaurant_id=rid, platform="google", external_id="c1", author="A",
                                     rating=1, text="bad")], db_path=db_path)
    notify.fire_review_alerts(rid, "Billing Co", saved, db_path=db_path)
    assert sent["email"] == [] and sent["push"] == []


def test_a_churned_restaurant_gets_no_daily_alerts(db_path, sent):
    """A10 #34 / MOD-REV-2."""
    from models import save_labor_snapshot
    rid = _restaurant(db_path, billing_status="churned")
    update_restaurant(rid, {"urgent_via_email": 1, "alert_labor_over": 1, "labor_target_pct": 30.0}, db_path=db_path)
    end = date.today() - timedelta(days=2)
    save_labor_snapshot(rid, (end - timedelta(days=6)).isoformat(), end.isoformat(), 38.0, 3800, 10000,
                        db_path=db_path)
    notify.check_daily_alerts(db_path=db_path)
    assert sent["email"] == [] and sent["push"] == []


def test_a_churned_restaurant_is_skipped_by_the_nightly_metrics_sync(db_path, monkeypatch):
    """A10 #34 / MOD-REV-2."""
    import social_routes
    live = _restaurant(db_path, name="Live Co", billing_status="active", module_marketing=1)
    gone = _restaurant(db_path, name="Gone Co", billing_status="churned", module_marketing=1)
    for rid in (live, gone):
        update_restaurant(rid, {"ig_token": "t", "ig_user_id": "u"}, db_path=db_path)
    seen = []
    monkeypatch.setattr(social_routes, "refresh_post_metrics", lambda rid, *a, **k: seen.append(rid) or {"ok": True})
    scheduler.run_marketing_metrics_sync()
    assert live in seen and gone not in seen


def test_a_churned_restaurant_is_refused_by_the_access_predicate(db_path):
    """A10 #34 — the request side already holds; the background side above
    is what is missing."""
    rid = _restaurant(db_path, billing_status="churned")
    assert models.subscription_allows_access(rid, db_path=db_path) is False
