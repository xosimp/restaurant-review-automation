"""Fix round H — the console's billing actions.

#6/#109: "Resend payment link" sends what moves the client forward (a
past-due client gets the card-update link), reports the real delivery and
writes no synthetic 'sent' row; "Send card-update link" likewise. #106:
Change plan updates the Stripe price and module_keys (with the chosen
proration) and then the local flags, audited. #78: Mark signed (offline)
and Attach Stripe customer, audited and guarded. #114: only an admin lifts
a dispute or refund hold. The detail and health reads the console needs.

Stripe and Resend are stand-ins; nothing leaves the process.
"""
import sys
import types

import pytest
from flask import Flask

import admin_events
import admin_routes
import auth
import billing_jobs
import config
import emails
import models
import ops
import webhook_routes
from auth import create_session, create_user, init_auth
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
    for mod in (models, auth, webhook_routes, admin_routes, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    monkeypatch.setattr(billing_jobs, "_sending_allowed", lambda: True)


def _rid(db_path, **kw):
    rid = create_restaurant(Restaurant(name=kw.pop("name", "Console Co"),
                                       owner_email=kw.pop("owner_email", "o@x.test")), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _rows(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


@pytest.fixture
def admin(db_path):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_routes.admin_bp)
    app.register_blueprint(webhook_routes.webhook_bp)
    home = _rid(db_path, name="Cavnar HQ", owner_email="will@x.test")
    uid = create_user(home, "will", "will@x.test", "admin-pass-1", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db_path))
    return c


@pytest.fixture
def mail(monkeypatch):
    box = types.SimpleNamespace(sent=[], next=None)

    def rec(kind):
        def _f(**k):
            box.sent.append((kind, k))
            return box.next if box.next is not None else emails.SendResult(True, message_id="m1")
        return _f
    monkeypatch.setattr(emails, "send_payment_email", rec("payment"))
    monkeypatch.setattr(emails, "send_card_update_email", rec("card"))
    monkeypatch.setattr(emails, "send_welcome_with_set_password_link", rec("welcome"))
    return box


# ── #6 / #109 resend payment and the card-update link ───────────────────────

def test_a_past_due_client_gets_the_card_update_link_not_the_setup_email(db_path, admin, mail):
    rid = _rid(db_path, billing_status="past_due", stripe_customer_id="cus_1")
    resp = admin.post(f"/admin/resend-payment/{rid}")
    assert resp.status_code == 200 and resp.get_json()["kind"] == "card_update"
    assert [k for k, _ in mail.sent] == ["card"] and mail.sent[0][1]["card_url"].endswith("/card")
    assert _rows(db_path, "SELECT * FROM email_log WHERE email_type='payment'") == []


def test_a_paying_or_covered_client_gets_nothing_and_is_told_why(db_path, admin, mail):
    paying = _rid(db_path, name="Paying", billing_status="active")
    payer = _rid(db_path, name="Main", owner_email="g@x.test", location_group="G", billing_status="active")
    covered = _rid(db_path, name="Elm", owner_email="g@x.test", location_group="G", billing_status="active")
    billing_jobs.upsert_subscription(payer, subscription_id="sub_g", facts={"status": "active"})
    r1 = admin.post(f"/admin/resend-payment/{paying}")
    r2 = admin.post(f"/admin/resend-payment/{covered}")
    assert r1.status_code == 409 and r1.get_json()["paying"] is True
    assert r2.status_code == 409 and r2.get_json()["covered"] is True
    assert mail.sent == []


def test_a_failed_send_is_a_502_with_a_sentence_and_no_fake_sent_row(db_path, admin, mail):
    rid = _rid(db_path, billing_status="trial", module_reviews=1)
    mail.next = emails.SendResult(False, error="Resend 422: invalid to address", status_code=422)
    resp = admin.post(f"/admin/resend-payment/{rid}")
    body = resp.get_json()
    assert resp.status_code == 502 and body["ok"] is False and "invalid to address" in body["error"]
    assert _rows(db_path, "SELECT * FROM email_log WHERE email_type='payment' AND status='sent'") == []
    (row,) = _rows(db_path, "SELECT status FROM owed_sends WHERE kind='payment_link'")
    assert row["status"] == "failed"


def test_a_local_backend_refuses_to_mail_a_client(db_path, admin, mail, monkeypatch):
    monkeypatch.setattr(billing_jobs, "_sending_allowed", lambda: False)
    rid = _rid(db_path, billing_status="trial", module_reviews=1)
    resp = admin.post(f"/admin/resend-payment/{rid}")
    assert resp.status_code == 409 and "not the production server" in resp.get_json()["error"]
    assert mail.sent == []


def test_send_card_update_link_needs_a_stripe_customer(db_path, admin, mail):
    rid = _rid(db_path, billing_status="past_due")
    assert admin.post(f"/admin/api/billing/{rid}/card-update-link").status_code == 409
    update_restaurant(rid, {"stripe_customer_id": "cus_1"}, db_path=db_path)
    resp = admin.post(f"/admin/api/billing/{rid}/card-update-link")
    assert resp.status_code == 200 and resp.get_json()["sent"] is True and mail.sent[0][0] == "card"


# ── #106 change plan ────────────────────────────────────────────────────────

class _Obj(dict):
    __getattr__ = dict.get


def _plan_stripe(interval="month"):
    calls = []
    sub = _Obj(id="sub_1", status="active", metadata={"restaurant_id": "x", "module_keys": "reviews"},
               items=_Obj(data=[_Obj(id="si_1", quantity=1, price=_Obj(
                   id="price_1m", unit_amount=34900, currency="usd", recurring={"interval": interval}))]))

    def modify(sid, **kw):
        calls.append(("modify", sid, kw))
        new = dict(sub)
        new["metadata"] = kw["metadata"]
        new["items"] = _Obj(data=[_Obj(id="si_1", quantity=1, price=_Obj(
            id=kw["items"][0]["price"], unit_amount=64900, currency="usd", recurring={"interval": interval}))])
        return _Obj(new)

    mod = types.SimpleNamespace(
        Subscription=types.SimpleNamespace(retrieve=lambda sid: sub, modify=modify),
        Product=types.SimpleNamespace(search=lambda query, limit: _Obj(data=[_Obj(id="prod_2m")]),
                                      create=lambda name: _Obj(id="prod_new")),
        Price=types.SimpleNamespace(list=lambda **k: _Obj(data=[_Obj(id="price_2m")]),
                                    create=lambda **k: _Obj(id="price_created")))
    mod.calls = calls
    return mod


def test_change_plan_updates_stripe_then_the_flags_and_is_audited(db_path, admin, monkeypatch):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1", module_reviews=1, module_labor=0,
               module_inventory=0, module_marketing=0)
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    fake = _plan_stripe()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(config, "stripe_api", lambda key=None: fake)
    resp = admin.post(f"/admin/api/billing/{rid}/change-plan",
                      json={"modules": ["reviews", "labor"], "proration": "none"})
    assert resp.status_code == 200 and resp.get_json()["stripe_updated"] is True
    (_, sid, kw), = fake.calls
    assert sid == "sub_1" and kw["items"] == [{"id": "si_1", "price": "price_2m"}]
    assert kw["proration_behavior"] == "none" and kw["metadata"]["module_keys"] == "labor,reviews"
    r = get_restaurant(rid, db_path)
    assert r.module_labor == 1 and r.module_reviews == 1
    m = billing_jobs.mirror_row(rid)
    assert m["module_keys"] == "labor,reviews" and m["amount_cents"] == 64900 and m["module_mismatch"] is None
    hist = [h for h in billing_jobs.billing_history(rid) if h["field"] == "module_labor"]
    assert hist[0]["source"] == "admin" and hist[0]["actor"] == "will"
    assert _rows(db_path, "SELECT * FROM admin_events WHERE event_type='billing.change_plan'")


def test_a_refused_plan_change_changes_nothing(db_path, admin, monkeypatch):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1", module_reviews=1, module_labor=0,
               module_inventory=0, module_marketing=0)
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1", facts={"status": "active"})
    fake = _plan_stripe()
    fake.Subscription.modify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("card_declined"))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(config, "stripe_api", lambda key=None: fake)
    resp = admin.post(f"/admin/api/billing/{rid}/change-plan", json={"modules": ["reviews", "labor"]})
    assert resp.status_code == 502 and get_restaurant(rid, db_path).module_labor == 0


def test_change_plan_with_no_subscription_changes_the_flags_only(db_path, admin, monkeypatch):
    rid = _rid(db_path, billing_status="trial")
    monkeypatch.setattr(config, "stripe_api", lambda key=None: pytest.fail("no Stripe without a subscription"))
    resp = admin.post(f"/admin/api/billing/{rid}/change-plan", json={"modules": ["marketing"]})
    assert resp.status_code == 200 and resp.get_json()["stripe_updated"] is False
    r = get_restaurant(rid, db_path)
    assert (r.module_reviews, r.module_marketing) == (0, 1)


def test_retainer_price_reuses_checkouts_product_and_lookup_key():
    import pricing
    seen = {}
    mod = types.SimpleNamespace(
        Product=types.SimpleNamespace(search=lambda query, limit: seen.update(q=query) or _Obj(data=[_Obj(id="prod_7")])),
        Price=types.SimpleNamespace(list=lambda lookup_keys, active, limit: seen.update(k=lookup_keys) or _Obj(data=[]),
                                    create=lambda **k: seen.update(c=k) or _Obj(id="price_new")))
    assert pricing.retainer_price_id(mod, 2, "year") == "price_new"
    assert seen["q"] == 'name:"Cavnar AI Retainer Annual — 2 Modules"'
    assert seen["k"] == ["cavnar-prod_7-649000-year"]
    assert seen["c"]["recurring"] == {"interval": "year"} and seen["c"]["unit_amount"] == 649000


# ── #78 mark signed, attach customer ────────────────────────────────────────

def test_mark_signed_offline_needs_a_note_and_starts_the_chase(db_path, admin):
    rid = _rid(db_path, contract_status="sent")
    assert admin.post(f"/admin/api/billing/{rid}/mark-signed", json={}).status_code == 400
    resp = admin.post(f"/admin/api/billing/{rid}/mark-signed", json={"note": "Signed on paper 9/27",
                                                                      "signed_on": "9/27/26"})
    assert resp.status_code == 200
    r = get_restaurant(rid, db_path)
    assert r.contract_status == "signed" and r.contract_signed_at == "2026-09-27 12:00:00"
    assert admin.post(f"/admin/api/billing/{rid}/mark-signed", json={"note": "again"}).status_code == 409
    (ev,) = _rows(db_path, "SELECT * FROM admin_events WHERE event_type='billing.contract.marked_signed'")
    assert "paper" in ev["summary"] and ev["restaurant_id"] == rid


def test_attach_customer_verifies_and_guards(db_path, admin, monkeypatch):
    rid = _rid(db_path, billing_status="active", module_reviews=1, module_labor=0, module_inventory=0,
               module_marketing=0)
    stranger = _rid(db_path, name="Stranger", owner_email="s@x.test", stripe_customer_id="cus_taken")
    subs = _Obj(data=[_Obj(id="sub_9", status="active", customer="cus_9",
                           metadata={"restaurant_id": str(rid), "module_keys": "reviews"},
                           items=_Obj(data=[_Obj(id="si", quantity=1, price=_Obj(
                               id="p", unit_amount=34900, currency="usd", recurring={"interval": "month"}))]))])
    fake = types.SimpleNamespace(
        Customer=types.SimpleNamespace(retrieve=lambda cid: _Obj(id=cid, deleted=cid == "cus_dead")),
        Subscription=types.SimpleNamespace(list=lambda **k: subs))
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(config, "stripe_api", lambda key=None: fake)
    assert admin.post(f"/admin/api/billing/{rid}/attach-stripe-customer", json={"customer_id": "nope"}).status_code == 400
    assert admin.post(f"/admin/api/billing/{rid}/attach-stripe-customer",
                      json={"customer_id": "cus_taken"}).status_code == 409
    assert admin.post(f"/admin/api/billing/{rid}/attach-stripe-customer",
                      json={"customer_id": "cus_dead"}).status_code == 400
    resp = admin.post(f"/admin/api/billing/{rid}/attach-stripe-customer", json={"customer_id": "cus_9"})
    assert resp.status_code == 200
    assert get_restaurant(rid, db_path).stripe_customer_id == "cus_9"
    assert billing_jobs.mirror_row(rid)["subscription_id"] == "sub_9"
    # A stored customer is never silently replaced.
    r2 = admin.post(f"/admin/api/billing/{rid}/attach-stripe-customer", json={"customer_id": "cus_10"})
    assert r2.status_code == 409 and r2.get_json()["stored"] == "cus_9"
    assert stranger


# ── #114 only an admin lifts a hold ─────────────────────────────────────────

def test_lifting_a_dispute_hold_releases_the_group_back_to_what_stripe_says(db_path, admin):
    a = _rid(db_path, name="A", owner_email="g@x.test", location_group="G", billing_status="paused",
             pause_reason="dispute")
    b = _rid(db_path, name="B", owner_email="g@x.test", location_group="G", billing_status="paused",
             pause_reason="dispute")
    billing_jobs.upsert_subscription(a, subscription_id="sub_g", facts={"status": "past_due"})
    assert admin.post(f"/admin/api/billing/{a}/lift-hold", json={}).status_code == 400
    resp = admin.post(f"/admin/api/billing/{a}/lift-hold", json={"note": "Dispute won"})
    assert resp.status_code == 200 and sorted(resp.get_json()["lifted"]) == sorted([a, b])
    for rid in (a, b):
        r = get_restaurant(rid, db_path)
        assert (r.billing_status, r.pause_reason) == ("past_due", None)
    assert admin.post(f"/admin/api/billing/{a}/lift-hold", json={"note": "again"}).status_code == 409


def test_lifting_a_hold_on_a_client_who_paid_again_restores_service(db_path, admin):
    rid = _rid(db_path, billing_status="churned", pause_reason="dispute", stripe_customer_id="cus_2")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_2", facts={"status": "trialing"})
    resp = admin.post(f"/admin/api/billing/{rid}/lift-hold", json={"note": "Reviewed; welcome back"})
    assert resp.status_code == 200
    r = get_restaurant(rid, db_path)
    assert (r.billing_status, r.pause_reason) == ("active", None)
    lone = _rid(db_path, name="Gone", billing_status="churned", pause_reason="refund")
    admin.post(f"/admin/api/billing/{lone}/lift-hold", json={"note": "Refund was goodwill"})
    r = get_restaurant(lone, db_path)
    assert (r.billing_status, r.pause_reason) == ("churned", None)


# ── the reads the console needs ─────────────────────────────────────────────

def test_the_billing_detail_and_health_reads(db_path, admin):
    rid = _rid(db_path, billing_status="past_due", stripe_customer_id="cus_1", contract_status="signed")
    billing_jobs.upsert_subscription(rid, subscription_id="sub_1",
                                     facts={"status": "past_due", "amount_cents": 34900, "interval": "month",
                                            "interval_count": 1})
    billing_jobs.record_invoice({"id": "in_1", "status": "open", "amount_due": 34900, "amount_remaining": 34900,
                                 "attempt_count": 1, "hosted_invoice_url": "https://invoice.stripe.com/i/1"},
                                restaurant_id=rid)
    d = admin.get(f"/admin/api/billing/{rid}").get_json()
    assert d["ok"] and d["billed_by"]["restaurant_id"] == rid and d["covers"] == [rid]
    assert d["subscription"]["billed_mrr"] == 349.0
    assert d["open_invoice"]["hosted_invoice_url"] == "https://invoice.stripe.com/i/1"
    assert d["invoices"][0]["invoice_id"] == "in_1"
    webhook_routes._webhook_seen("stripe", True, event="invoice.paid evt_1")
    h = admin.get("/admin/api/billing/health").get_json()
    assert h["ok"] and h["webhooks"][0]["provider"] == "stripe" and "pipeline" in h and "reconcile" in h
