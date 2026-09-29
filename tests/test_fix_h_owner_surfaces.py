"""Fix round H — what the owner sees and can do about billing.

#6: a past-due owner has a working way to fix the card — the /pay link
goes to the open invoice or the Stripe portal (never "You're all set"),
and the Billing screen (web and app, one body) lists past-due
subscriptions with the fix link. #114/#138: a hold only Cavnar AI lifts
cannot be resumed by the owner, and says why. #116 (decision 1): a
location covered by its group's subscription is told so, and its pause
acts on the paying location's subscription. #25: the in-app past-due
banner.

Stripe is a stand-in; nothing leaves the process.
"""
import sys
import types

import pytest
from flask import Flask, render_template

import auth
import billing_jobs
import client_api
import config
import emails
import models
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
    for mod in (models, auth, webhook_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    monkeypatch.delenv("BILLING_PREVIEW_IDS", raising=False)


def _rid(db_path, **kw):
    rid = create_restaurant(Restaurant(name=kw.pop("name", "Owner Co"),
                                       owner_email=kw.pop("owner_email", "o@x.test")), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


class _Obj(dict):
    __getattr__ = dict.get


def _stripe(portal="https://billing.stripe.com/p/session_1", open_invoice=None, subs=None):
    calls = []

    def sub_list(customer, status, limit=5):
        calls.append(("Subscription.list", customer, status))
        return _Obj(data=[s for s in (subs or []) if s.status == status])

    def inv_list(customer, status=None, limit=6):
        calls.append(("Invoice.list", customer, status))
        if status == "open":
            return _Obj(data=[open_invoice] if open_invoice else [])
        return _Obj(data=[])

    mod = types.SimpleNamespace(
        Subscription=types.SimpleNamespace(list=sub_list),
        Invoice=types.SimpleNamespace(list=inv_list),
        Customer=types.SimpleNamespace(retrieve=lambda cid, expand=None: _Obj(invoice_settings=_Obj(
            default_payment_method=_Obj(card=_Obj(brand="visa", last4="4242"))))),
        billing_portal=types.SimpleNamespace(Session=types.SimpleNamespace(
            create=lambda customer, return_url: calls.append(("Portal", customer)) or _Obj(url=portal))))
    mod.calls = calls
    return mod


@pytest.fixture
def pay(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    client = app.test_client()

    def get(rid, period="monthly"):
        path = emails.pay_link(rid, period).split("://", 1)[1].split("/", 1)[1]
        return client.get("/" + path)
    return get


# ── #6 the /pay link ────────────────────────────────────────────────────────

def test_a_past_due_client_is_sent_to_pay_the_open_invoice_not_told_all_set(db_path, pay, monkeypatch):
    rid = _rid(db_path, billing_status="past_due", stripe_customer_id="cus_1")
    billing_jobs.record_invoice({"id": "in_1", "status": "open", "amount_due": 34900, "amount_remaining": 34900,
                                 "hosted_invoice_url": "https://invoice.stripe.com/i/in_1"}, restaurant_id=rid)
    monkeypatch.setattr(emails, "create_stripe_checkout", lambda *a, **k: pytest.fail("no new checkout"))
    r = pay(rid, "monthly")                       # the old payment email's button
    assert r.status_code == 302 and r.headers["Location"] == "https://invoice.stripe.com/i/in_1"


def test_the_card_link_opens_a_fresh_portal_session(db_path, pay, monkeypatch):
    rid = _rid(db_path, billing_status="active", stripe_customer_id="cus_1")
    fake = _stripe()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(config, "stripe_api", lambda key=None: fake)
    r = pay(rid, "card")
    assert r.status_code == 302 and r.headers["Location"] == "https://billing.stripe.com/p/session_1"
    assert ("Portal", "cus_1") in fake.calls


def test_a_covered_location_is_told_who_pays_for_it(db_path, pay, monkeypatch):
    payer = _rid(db_path, name="Main St", owner_email="g@x.test", location_group="G", billing_status="active")
    covered = _rid(db_path, name="Elm St", owner_email="g@x.test", location_group="G", billing_status="active")
    billing_jobs.upsert_subscription(payer, subscription_id="sub_g", facts={"status": "active"})
    monkeypatch.setattr(emails, "create_stripe_checkout", lambda *a, **k: pytest.fail("no second checkout"))
    r = pay(covered)
    body = r.get_data(as_text=True)
    assert r.status_code == 200 and "Already covered" in body and "Main St" in body


def test_a_held_account_is_not_offered_a_checkout(db_path, pay, monkeypatch):
    rid = _rid(db_path, billing_status="paused", pause_reason="dispute")
    monkeypatch.setattr(emails, "create_stripe_checkout", lambda *a, **k: pytest.fail("no checkout while held"))
    body = pay(rid).get_data(as_text=True)
    assert "on hold" in body


def test_a_self_paused_client_is_not_sent_to_a_new_checkout(db_path, pay, monkeypatch):
    rid = _rid(db_path, billing_status="paused", pause_reason="self", paused_until="2026-10-20")
    monkeypatch.setattr(emails, "create_stripe_checkout", lambda *a, **k: pytest.fail("no checkout while paused"))
    assert "Resume any time" in pay(rid).get_data(as_text=True)


def test_an_unpaid_trial_still_gets_its_checkout(db_path, pay, monkeypatch):
    rid = _rid(db_path, billing_status="trial", module_reviews=1, module_labor=0, module_inventory=0,
               module_marketing=0)
    monkeypatch.setattr(emails, "create_stripe_checkout", lambda n, *a, **k: "https://checkout.stripe.com/c/x")
    r = pay(rid, "annual")
    assert r.status_code == 302 and r.headers["Location"].startswith("https://checkout.stripe.com/")


# ── #6 the Billing screen (one body for web and app) ────────────────────────

def test_billing_info_lists_a_past_due_subscription_with_the_fix_link(db_path, monkeypatch):
    rid = _rid(db_path, billing_status="past_due", stripe_customer_id="cus_1")
    sub = _Obj(status="past_due", current_period_end=1790000000, trial_end=None,
               items=_Obj(data=[_Obj(price=_Obj(unit_amount=34900, recurring={"interval": "month"}))]))
    inv = _Obj(hosted_invoice_url="https://invoice.stripe.com/i/in_1", amount_due=34900, amount_remaining=34900,
               attempt_count=2, next_payment_attempt=1790000000)
    fake = _stripe(subs=[sub], open_invoice=inv)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(config, "stripe_api", lambda key=None: fake)
    payload, status = client_api._do_billing_info(rid)
    assert status == 200 and payload["ok"] and payload["status"] == "past_due"
    assert payload["portal_url"] == "https://billing.stripe.com/p/session_1"
    assert payload["fix_url"] == "https://invoice.stripe.com/i/in_1"
    assert payload["past_due"]["attempt_count"] == 2 and payload["past_due"]["amount_due"] == "$349.00"
    m, d, yy = payload["past_due"]["next_attempt"].split("/")
    assert len(yy) == 2 and not m.startswith("0")


def test_billing_info_offers_the_portal_even_with_no_live_subscription(db_path, monkeypatch):
    rid = _rid(db_path, billing_status="churned", stripe_customer_id="cus_1")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(config, "stripe_api", lambda key=None: _stripe())
    payload, _ = client_api._do_billing_info(rid)
    assert payload["status"] == "inactive" and payload["portal_url"]


def test_billing_info_for_a_covered_location_names_the_payer_and_shows_nothing_of_theirs(db_path):
    payer = _rid(db_path, name="Main St", owner_email="g@x.test", location_group="G",
                 stripe_customer_id="cus_g", billing_status="active")
    covered = _rid(db_path, name="Elm St", owner_email="g@x.test", location_group="G", billing_status="active")
    billing_jobs.upsert_subscription(payer, subscription_id="sub_g", facts={"status": "active"})
    payload, _ = client_api._do_billing_info(covered)
    assert payload["status"] == "covered" and payload["billed_by"] == {"restaurant_id": payer, "name": "Main St"}
    assert "portal_url" not in payload and payload["invoices"] == []


def test_billing_info_says_when_a_hold_is_not_the_owners_to_lift(db_path):
    rid = _rid(db_path, billing_status="paused", pause_reason="refund")
    payload, _ = client_api._do_billing_info(rid)
    assert payload["locked"] is True and payload["pause_reason"] == "refund"


# ── #114 / #138 pause and resume ────────────────────────────────────────────

def _owner(rid):
    return {"id": 1, "restaurant_id": rid, "role": "owner", "is_admin": False, "username": "o"}


@pytest.fixture
def sr(monkeypatch):
    import strategy_routes
    monkeypatch.setattr(strategy_routes, "_stripe_client", lambda: None)
    monkeypatch.setattr(emails, "deliver", lambda **kw: emails.SendResult(True))
    return strategy_routes


def test_the_owner_cannot_resume_a_dispute_hold(db_path, sr):
    rid = _rid(db_path, billing_status="paused", pause_reason="dispute")
    payload, status = sr._do_resume(_owner(rid))
    assert status == 409 and payload["locked"] is True and "dispute" in payload["error"]
    assert get_restaurant(rid, db_path).billing_status == "paused"
    st, _ = sr._do_pause_status(_owner(rid))
    assert st["paused"] and st["locked"] and st["can_resume"] is False and st["pause_reason"] == "dispute"


def test_a_self_pause_records_its_reason_and_a_trial_resumes_as_a_trial(db_path, sr, monkeypatch):
    rid = _rid(db_path)                                   # a trial that never paid
    monkeypatch.setattr(sr, "_body", lambda: {"days": 14})
    assert sr._do_pause(_owner(rid))[1] == 200
    r = get_restaurant(rid, db_path)
    assert (r.billing_status, r.pause_reason) == ("paused", "self")
    assert sr._do_pause_status(_owner(rid))[0]["can_resume"] is True
    assert sr._do_resume(_owner(rid))[1] == 200
    r = get_restaurant(rid, db_path)
    assert r.billing_status == "trial" and r.pause_reason is None, "resuming a trial must not make it 'active'"
    hist = [h["source"] for h in billing_jobs.billing_history(rid) if h["field"] == "billing_status"]
    assert hist == ["owner", "owner"]


def test_a_covered_location_pauses_the_paying_locations_subscription(db_path, sr, monkeypatch):
    payer = _rid(db_path, name="Main St", owner_email="g@x.test", location_group="G",
                 stripe_customer_id="cus_g", billing_status="active")
    covered = _rid(db_path, name="Elm St", owner_email="g@x.test", location_group="G", billing_status="active")
    billing_jobs.upsert_subscription(payer, subscription_id="sub_g", facts={"status": "active"})
    seen = []

    class _Sub:
        id = "sub_g"

    class _Subscription:
        @staticmethod
        def list(customer, status, limit):
            seen.append(customer)
            return type("R", (), {"data": [_Sub()] if status == "active" else []})()

        @staticmethod
        def modify(sub_id, **kw):
            seen.append(("modify", sub_id))
    monkeypatch.setattr(sr, "_stripe_client", lambda: type("S", (), {"Subscription": _Subscription}))
    monkeypatch.setattr(sr, "_body", lambda: {"days": 30})
    assert sr._do_pause(_owner(covered))[1] == 200
    assert "cus_g" in seen and ("modify", "sub_g") in seen
    assert get_restaurant(payer, db_path).billing_status == "paused"
    assert get_restaurant(covered, db_path).billing_status == "paused"


# ── the paused page and the past-due banner ─────────────────────────────────

def _render(name, **ctx):
    app = Flask(__name__, template_folder="../templates")
    with app.test_request_context("/"):
        return render_template(name, **ctx)


def test_the_paused_page_shows_a_hold_without_a_resume_button():
    html = _render("billing_paused.html", paused=True, paused_until=None, can_resume=True,
                   pause_lock="dispute", message="")
    assert "on hold" in html and "disputed" in html and 'id="resume-btn"' not in html
    html = _render("billing_paused.html", paused=True, paused_until="2026-10-20", can_resume=True, message="")
    assert 'id="resume-btn"' in html                       # an owner's own pause still resumes


def test_the_dashboard_carries_a_past_due_banner_with_one_fix_button():
    src = open("templates/dashboard.html", encoding="utf-8").read()
    block = src[src.index("{% if restaurant and (restaurant.billing_status or '')|lower == 'past_due' %}"):]
    block = block[:block.index("{% endif %}\n<div class=\"tabs\"")]
    assert 'id="pastdue-banner"' in block and "cbtn cbtn-primary" in block and "pastDueFix(this)" in block
    assert "{% if is_principal %}" in block and "/api/billing-info" in block and "fix_url" in block
    assert "var(--red" in block and "#" not in block.split("<style>")[1].split("</style>")[0].replace("#pastdue", "")
