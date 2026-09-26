"""Account → Billing and Close my account on the web, as twins of the app.

The web's /api/billing-info had no invoice history (the app's did) and the
web's close-account was a mailto only (the app records a request). Both are
now one shared body in client_api, called by both routes, so the two
answers are the same shape and scoped the same way: to the caller's own
restaurant, never to an id in the request.
"""
import types

import pytest
from flask import Flask

import auth
import client_api
import guest_marketing
import mobile_api
import models
from auth import create_user, init_auth
from client_api import client_bp
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_deletion_requested_at


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api, mobile_api, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, name="Twin Co", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.test",
                                        owner_name="Erik", **kw), db_path=db_path)


def _web_as(monkeypatch, rid, uid=7, email="o@x.test"):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client",
                                 "username": "owner", "email": email})


def _mobile_token(client, db_path, rid, username="alice"):
    create_user(rid, username, f"{username}@x.test", "correct-horse", db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": username, "password": "correct-horse"})
    return {"Authorization": f"Bearer {resp.get_json()['token']}"}


# ── Billing: one body, invoice history on both ─────────────────────────────

class _Obj(dict):
    __getattr__ = dict.get


def _fake_stripe(seen):
    """Answers only for the customer it is asked about, and records it."""
    item = _Obj(price=_Obj(unit_amount=34900, recurring={"interval": "month", "interval_count": 1}))

    def sub_list(customer, status, limit):
        seen.append(customer)
        return _Obj(data=[_Obj(status="active", current_period_end=1790000000, trial_end=None,
                               items=_Obj(data=[item]))] if status == "active" else [])

    def inv_list(customer, limit):
        seen.append(customer)
        return _Obj(data=[_Obj(created=1788000000, amount_paid=34900, amount_due=34900,
                               status="paid", invoice_pdf="https://pay.stripe.test/inv_1.pdf")])

    return types.SimpleNamespace(
        Subscription=types.SimpleNamespace(list=sub_list),
        Invoice=types.SimpleNamespace(list=inv_list),
        Customer=types.SimpleNamespace(retrieve=lambda cid, expand=None: _Obj(
            invoice_settings=_Obj(default_payment_method=_Obj(card=_Obj(brand="visa", last4="4242"))))),
        billing_portal=types.SimpleNamespace(Session=types.SimpleNamespace(
            create=lambda customer, return_url: _Obj(url="https://billing.stripe.test/p"))),
    )


def test_web_billing_has_the_apps_invoice_history_in_the_same_shape(client, db_path, monkeypatch):
    mine = _restaurant(db_path, stripe_customer_id="cus_mine")
    _restaurant(db_path, name="Other Co", stripe_customer_id="cus_theirs")
    seen = []
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("BILLING_PREVIEW_IDS", raising=False)
    monkeypatch.setattr(client_api.config, "stripe_api", lambda key=None: _fake_stripe(seen))
    _web_as(monkeypatch, mine)
    web = client.get("/api/billing-info").get_json()
    h = _mobile_token(client, db_path, mine)
    app_ = client.get("/mobile/api/account/billing", headers=h).get_json()
    assert web == app_
    assert web["ok"] and web["amount"] == "$349/mo" and web["payment_method"] == "Visa ending 4242"
    assert web["invoices"] == [{"date": web["invoices"][0]["date"], "amount": "$349", "status": "paid",
                                "pdf_url": "https://pay.stripe.test/inv_1.pdf"}]
    # M/D/YY, never a four-digit year.
    for d in (web["next_date"], web["invoices"][0]["date"]):
        m, day, yy = d.split("/")
        assert len(yy) == 2 and not m.startswith("0") and not day.startswith("0")
    # Only the caller's own Stripe customer was ever read.
    assert set(seen) == {"cus_mine"}


def test_billing_with_no_customer_reads_the_same_on_both(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.delenv("BILLING_PREVIEW_IDS", raising=False)
    _web_as(monkeypatch, rid)
    h = _mobile_token(client, db_path, rid)
    assert client.get("/api/billing-info").get_json() == {"ok": False, "reason": "no_customer"}
    assert client.get("/mobile/api/account/billing", headers=h).get_json() == {"ok": False, "reason": "no_customer"}


def test_a_stripe_failure_is_not_echoed_raw(client, db_path, monkeypatch):
    rid = _restaurant(db_path, stripe_customer_id="cus_mine")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("BILLING_PREVIEW_IDS", raising=False)

    def boom(key=None):
        # The app's copy used to answer str(e); the shared body redacts
        # credentials the way the web always did (ai_guard.safe_error).
        raise RuntimeError("GET https://api.stripe.com/v1/subscriptions?key=sk_test_x failed")
    monkeypatch.setattr(client_api.config, "stripe_api", boom)
    h = _mobile_token(client, db_path, rid)
    body = client.get("/mobile/api/account/billing", headers=h).get_json()
    assert body["ok"] is False and body["reason"] == "stripe_error"
    assert "sk_test_x" not in (body.get("error") or "")


# ── Close my account: the app's recorded request, on the web ──────────────

def test_web_close_account_records_the_same_request_as_the_app(client, db_path, monkeypatch):
    mine, theirs = _restaurant(db_path), _restaurant(db_path, name="Other Co")
    sent = []
    monkeypatch.setattr("emails.send_account_deletion_request_email", lambda *a: sent.append(a) or True)
    _web_as(monkeypatch, mine, email="owner@x.test")
    first = client.post("/api/account/request-deletion", json={"restaurant_id": theirs})
    assert first.status_code == 200
    body = first.get_json()
    # requested_on is the date to show, on the restaurant's clock (M/D/YY).
    assert set(body) == {"ok", "requested_at", "requested_on"} and body["ok"] is True
    assert get_deletion_requested_at(mine, db_path=db_path) == body["requested_at"]
    # The caller's restaurant, whatever the body names.
    assert get_deletion_requested_at(theirs, db_path=db_path) is None
    assert sent and sent[0][0] == "Twin Co" and sent[0][2] == "owner@x.test"
    # The app's twin answers with the first request, and Will hears once.
    h = _mobile_token(client, db_path, mine)
    again = client.post("/mobile/api/account/request-deletion", headers=h).get_json()
    assert again == body and len(sent) == 1
    # And the web Account settings carry it, so the card says it was sent.
    assert client.get("/api/account-settings").get_json()["deletion_requested_at"] == body["requested_at"]


def test_the_web_close_card_posts_the_request_and_keeps_the_email_line():
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "templates", "dashboard.html")).read()
    card = src[src.index('id="acct-close-card"'):]
    card = card[:card.index("</div>\n        </div>")]
    assert 'onclick="acctRequestClose(this)"' in card
    assert "mailto:will@cavnar.ai" in card
    fn = src[src.index("function acctRequestClose(btn)"):]
    fn = fn[:fn.index("\n}\n")]
    assert "'/api/account/request-deletion'" in fn and "confirm(" in fn
    inv = src[src.index("function _billingInvoices(list)"):]
    inv = inv[:inv.index("\n}\n")]
    assert "pdf_url" in inv and 'id="billing-invoice-rows"' in src


def test_billing_and_close_account_are_the_owners_alone(client, monkeypatch, db_path):
    """A manager login may not read the invoices or ask to end the service:
    both are the account holder's (permissions.is_principal)."""
    rid = _restaurant(db_path, name="Owner Only Co")
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 9, "restaurant_id": rid, "is_admin": 0, "role": "manager",
                                 "username": "gm", "email": "gm@x.test"})
    r = client.get("/api/billing-info")
    assert r.status_code == 403 and r.get_json().get("owner_only") is True
    r = client.post("/api/account/request-deletion")
    assert r.status_code == 403 and r.get_json().get("owner_only") is True
    assert not get_deletion_requested_at(rid)


def test_the_close_request_date_is_the_restaurants_not_utcs(client, db_path, monkeypatch):
    """A request at 10pm in Chicago is stored as the next day in UTC; the
    card read the stamp's date part and said "Requested" a day late."""
    rid = _restaurant(db_path, name="Evening Co", timezone="America/Chicago")
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET deletion_requested_at='2026-09-25 03:00:00' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    _web_as(monkeypatch, rid)
    body = client.get("/api/account-settings").get_json()
    assert body["deletion_requested_at"] == "2026-09-25 03:00:00"
    assert body["deletion_requested_on"] == "9/24/26"
    again = client.post("/api/account/request-deletion").get_json()
    assert again["requested_at"] == "2026-09-25 03:00:00" and again["requested_on"] == "9/24/26"
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "templates", "dashboard.html")).read()
    assert "_acctShowClosed(d.deletion_requested_at, d.deletion_requested_on)" in src
    assert "_acctShowClosed(d.requested_at,d.requested_on)" in src
