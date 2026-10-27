"""Audit #25 A01-13 / A06-08 / A06-10: Stripe through the REAL SDK.

Ten test files put a stand-in `stripe` module in sys.modules, so the
installed SDK's signature check, its 300-second timestamp tolerance and its
StripeObject types never ran in the suite: a stripe upgrade that changed any
of them would have passed every test while billing state stopped updating.

Here nothing is faked but the network:

- events from tests/fixtures/providers/stripe/ are signed with the SDK's own
  scheme (stripe.WebhookSignature) and POSTed through /stripe-webhook, where
  the real stripe.Webhook.construct_event verifies them and hands
  _stripe_dispatch a stripe.Event — checkout.session.completed activates,
  invoice.payment_failed puts the account past due and owes the dunning,
  customer.subscription.deleted churns it, in both invoice API shapes;
- a signature older than the tolerance, a wrong secret or a changed body is
  refused and changes nothing;
- config.stripe_api pins STRIPE_API_VERSION and says (logs) when the bounded
  HTTP client could not be installed;
- Stripe answering 429 or not answering at all: the nightly reconcile counts
  the restaurant failed (never a clean run), and the owner's billing page
  answers inside the bound — at most two requests of 10 s each and the
  SDK's one short retry pause, never the SDK's own 80 s x 3.

The SDK's clock for the tolerance is pinned (NOW) so no test reads the wall
clock; requests.Session.request is the only thing stubbed, below the SDK.
"""
import json
import os
import re
import sys
import types

import pytest
import requests
import stripe

import auth
import billing_jobs
import client_api
import config
import models
import ops
import webhook_routes

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "providers", "stripe")
SECRET = "whsec_fixture_real_sdk_0123456789"
RID, CUS, SUB = 41, "cus_FIXTURE0001", "sub_FIXTURE0001"
NOW = 1795579400                     # the SDK's clock in these tests: just after the corpus's last event


def _event(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


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
    for mod in (models, auth, webhook_routes, ops, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    auth.init_auth(db_path=db_path)
    alerts = []
    monkeypatch.setattr(webhook_routes, "_send_alert", lambda subject, body: alerts.append(subject))
    return alerts


@pytest.fixture
def real_stripe(monkeypatch):
    """The installed SDK, its tolerance clock pinned to NOW, the webhook
    secret set — and every global the SDK keeps restored afterwards."""
    assert sys.modules.get("stripe") is stripe and hasattr(stripe, "StripeObject"), "a stand-in leaked"
    monkeypatch.setattr(stripe._webhook, "time", types.SimpleNamespace(time=lambda: float(NOW)))
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(webhook_routes, "STRIPE_WEBHOOK_SECRET", "")
    seen = []
    real_dispatch = webhook_routes._stripe_dispatch

    def spy(event):
        seen.append((type(event), type((event.get("data") or {}).get("object"))))
        return real_dispatch(event)
    monkeypatch.setattr(webhook_routes, "_stripe_dispatch", spy)
    return seen


@pytest.fixture
def hook():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    client = app.test_client()

    def post(event, signed_at=NOW - 5, secret=SECRET, tamper=None):
        body = json.dumps(event)
        sig = stripe.WebhookSignature._compute_signature(f"{signed_at}.{body}", secret)
        if tamper:
            body = tamper(body)
        return client.post("/stripe-webhook", data=body.encode(), content_type="application/json",
                           headers={"Stripe-Signature": f"t={signed_at},v1={sig}"})
    return post


@pytest.fixture
def pending(db_path):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, billing_status) VALUES (?,?,?,?)",
                 (RID, "Fixture Bistro", "owner@fixture.test", "pending"))
    conn.commit()
    conn.close()
    return RID


def _status(rid):
    return (models.get_restaurant(rid).billing_status or "").lower()


def _count(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def test_the_sdk_under_test_is_the_locked_one():
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        pinned = re.search(r"^stripe==([0-9.]+)", f.read(), re.M).group(1)
    assert stripe.VERSION == pinned


# ── /stripe-webhook, verified by the real SDK ───────────────────────────────

@pytest.mark.parametrize("shape", ["acacia", "basil"])
def test_a_signed_lifecycle_activates_then_past_dues_then_churns(db_path, real_stripe, hook, pending, shape):
    resp = hook(_event("checkout.session.completed.json"))
    assert resp.status_code == 200, resp.get_json()
    r = models.get_restaurant(RID)
    assert (_status(RID), r.stripe_customer_id) == ("active", CUS) and r.converted_at
    assert billing_jobs.mirror_row(RID)["subscription_id"] == SUB

    resp = hook(_event(f"invoice.payment_failed.{shape}.json"))
    assert resp.status_code == 200, resp.get_json()
    assert _status(RID) == "past_due"
    assert _count(db_path, "SELECT COUNT(*) FROM owed_sends WHERE kind='dunning' AND restaurant_id=?", RID) >= 1
    assert billing_jobs.invoice_row(f"in_FIXTURE_{shape.upper()}_FAILED")["subscription_id"] == SUB

    resp = hook(_event(f"customer.subscription.deleted.{shape}.json"))
    assert resp.status_code == 200, resp.get_json()
    assert _status(RID) == "churned"
    assert billing_jobs.mirror_row(RID)["status"] == "canceled"

    # The handlers were given the SDK's own objects, not plain dicts.
    assert [t for t, _ in real_stripe] == [stripe.Event] * 3
    assert real_stripe[0][1] is stripe.checkout.Session and real_stripe[1][1] is stripe.Invoice
    assert real_stripe[2][1] is stripe.Subscription
    assert _count(db_path, "SELECT COUNT(*) FROM stripe_events_seen") == 3


def test_a_replayed_event_is_acknowledged_once(real_stripe, hook, pending):
    event = _event("checkout.session.completed.json")
    assert hook(event).status_code == 200
    again = hook(event)
    assert again.status_code == 200 and again.get_json().get("duplicate") is True


def test_a_signature_just_inside_the_tolerance_is_accepted(real_stripe, hook, pending):
    assert hook(_event("checkout.session.completed.json"), signed_at=NOW - 299).status_code == 200
    assert _status(RID) == "active"


@pytest.mark.parametrize("case", ["too_old", "wrong_secret", "changed_body", "no_secret_configured"])
def test_a_bad_signature_is_refused_and_changes_nothing(db_path, real_stripe, hook, pending, monkeypatch, case):
    event = _event("checkout.session.completed.json")
    if case == "too_old":
        resp = hook(event, signed_at=NOW - 301)            # the SDK's DEFAULT_TOLERANCE is 300 s
    elif case == "wrong_secret":
        resp = hook(event, secret="whsec_someone_elses")
    elif case == "changed_body":
        resp = hook(event, tamper=lambda b: b.replace('"restaurant_id": "41"', '"restaurant_id": "42"'))
    else:
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
        resp = hook(event)
    assert resp.status_code in (400, 401)
    assert _status(RID) == "pending" and not models.get_restaurant(RID).stripe_customer_id
    assert _count(db_path, "SELECT COUNT(*) FROM stripe_events_seen") == 0
    assert real_stripe == [], "an unverified event reached the handlers"


# ── config.stripe_api ───────────────────────────────────────────────────────

@pytest.fixture
def sdk_globals(monkeypatch):
    """Every module-level setting config.stripe_api writes, restored after."""
    for name in ("api_key", "api_version", "default_http_client", "max_network_retries"):
        monkeypatch.setattr(stripe, name, getattr(stripe, name))
    monkeypatch.setattr(stripe, "_cavnar_bounded", False, raising=False)
    monkeypatch.setattr(config, "_stripe_unbounded_logged", False)
    return stripe


def test_stripe_api_version_pins_the_version_every_call_is_made_at(sdk_globals, monkeypatch):
    sdk_default = stripe.api_version
    monkeypatch.delenv("STRIPE_API_VERSION", raising=False)
    assert config.stripe_api("sk_test_fixture").api_version == sdk_default
    monkeypatch.setenv("STRIPE_API_VERSION", "2025-03-31.basil")
    mod = config.stripe_api("sk_test_fixture")
    assert mod.api_version == "2025-03-31.basil"
    assert isinstance(mod.default_http_client, stripe.RequestsClient)
    assert mod.default_http_client._timeout == config.STRIPE_TIMEOUT_SECONDS == 10
    assert mod.max_network_retries == config.STRIPE_MAX_NETWORK_RETRIES == 1


def test_a_failure_to_install_the_bounded_client_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(config, "_stripe_unbounded_logged", False)

    class _Broken:
        def __init__(self, **kw):
            raise TypeError("unexpected keyword argument 'timeout'")
    fake = types.ModuleType("stripe")
    fake.RequestsClient = _Broken
    monkeypatch.setitem(sys.modules, "stripe", fake)
    with caplog.at_level("ERROR", logger="config"):
        config.stripe_api("sk_test_fixture")
        config.stripe_api("sk_test_fixture")
    said = [r for r in caplog.records if "bounded HTTP client could not be installed" in r.getMessage()]
    assert len(said) == 1 and "timeout" in said[0].getMessage()
    assert not getattr(fake, "_cavnar_bounded", False), "a client that failed must not be marked installed"


def test_a_stand_in_module_with_no_http_client_is_left_alone(monkeypatch, caplog):
    fake = types.ModuleType("stripe")
    monkeypatch.setitem(sys.modules, "stripe", fake)
    with caplog.at_level("ERROR", logger="config"):
        assert config.stripe_api("sk_test_fixture") is fake
    assert not caplog.records


# ── Stripe refusing or not answering: reconcile and the billing page ────────

def _billed(db_path, rid, customer):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, billing_status, stripe_customer_id) "
                 "VALUES (?,?,?,?,?)", (rid, f"Fixture {rid}", f"o{rid}@fixture.test", "active", customer))
    conn.commit()
    conn.close()


def _failing_stripe(fail_for):
    """A stripe module whose Subscription.list raises the REAL SDK's error for
    the customers in `fail_for` (customer -> exception) and lists one active
    subscription for anyone else."""
    mod = types.SimpleNamespace()
    mod.Customer = types.SimpleNamespace(retrieve=lambda cid, **k: {"id": cid, "deleted": False})

    def listing(customer=None, **k):
        if customer in fail_for:
            raise fail_for[customer]
        return {"data": [{"id": f"sub_{customer}", "status": "active", "metadata": {}, "items": {"data": []}}]}
    mod.Subscription = types.SimpleNamespace(list=listing)
    return mod


@pytest.mark.parametrize("error", [
    stripe.RateLimitError("Request rate limit exceeded.", http_status=429),
    stripe.APIConnectionError("Unexpected error communicating with Stripe."),
])
def test_reconcile_under_stripe_failure_is_never_a_clean_run(db_path, monkeypatch, error):
    _billed(db_path, 51, "cus_FIXTURE_A")
    _billed(db_path, 52, "cus_FIXTURE_B")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(config, "stripe_api", lambda key=None: _failing_stripe(
        {"cus_FIXTURE_A": error, "cus_FIXTURE_B": error}))
    out = billing_jobs.reconcile_stripe()
    assert (out["attempted"], out["failed"], out["ok"]) == (2, 2, 0)
    assert ops.run_outcome(out)[0] == ops.RUN_FAILED
    assert _count(db_path, "SELECT COUNT(*) FROM billing_reconcile") == 0, "a failed check wrote a clean result"

    # One answering, one refused: partial — never clean.
    monkeypatch.setattr(config, "stripe_api", lambda key=None: _failing_stripe({"cus_FIXTURE_B": error}))
    out = billing_jobs.reconcile_stripe()
    assert (out["attempted"], out["failed"], out["ok"]) == (2, 1, 1)
    assert ops.run_outcome(out)[0] == ops.RUN_PARTIAL


@pytest.fixture
def stripe_wire(monkeypatch, sdk_globals):
    """The real SDK with the bounded client config.stripe_api installs; the
    wire answers from a script. Records each request's timeout and every
    pause the SDK asks for."""
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.delenv("STRIPE_API_VERSION", raising=False)
    state = {"answers": [], "requests": [], "slept": []}

    def request(self, method, url, headers=None, data=None, timeout=None, **kw):
        assert "api.stripe.com" in url
        state["requests"].append(timeout)
        answer = state["answers"].pop(0) if state["answers"] else requests.exceptions.ReadTimeout("read timed out")
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr("time.sleep", lambda s: state["slept"].append(s))
    return state


def _http(status, body):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.headers["Request-Id"] = "req_fixture"
    r.headers["Content-Type"] = "application/json"
    return r


def test_the_billing_page_answers_inside_the_bound_when_stripe_does_not_answer(db_path, stripe_wire):
    _billed(db_path, 61, "cus_FIXTURE_SLOW")
    body, status = client_api._do_billing_info(61)
    assert status == 200 and body["ok"] is False and body["reason"] == "stripe_error"
    # one try and the SDK's single retry, each held to 10 s — not 3 x 80 s
    assert stripe_wire["requests"] == [10, 10]
    worst_case = sum(stripe_wire["requests"]) + sum(stripe_wire["slept"])
    assert worst_case <= 2 * config.STRIPE_TIMEOUT_SECONDS + 1.0, worst_case


def test_the_billing_page_on_a_429_is_one_request_and_an_honest_error(db_path, stripe_wire):
    _billed(db_path, 62, "cus_FIXTURE_BUSY")
    stripe_wire["answers"].append(_http(429, {"error": {"type": "invalid_request_error", "code": "rate_limit",
                                                        "message": "Too many requests hit the API too quickly."}}))
    body, status = client_api._do_billing_info(62)
    assert status == 200 and body["ok"] is False and body["reason"] == "stripe_error"
    assert stripe_wire["requests"] == [10] and stripe_wire["slept"] == []
