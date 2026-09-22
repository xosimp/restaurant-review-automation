"""Edge cases on public and malformed input: the guest SMS opt-in page, the
public status page, and JSON bodies that are not objects.

From the SEC edge audit (SEC-17, SEC-32, SEC-38). These surfaces take input
from anyone: a printed join link, a status URL, a scanner posting "[1]" to
every route. The correct behaviour is that a stranger can never re-enrol a
guest who texted STOP, that a throttle keys on the address the proxy vouches
for rather than a header the client wrote, that a public GET does not write,
and that a body of the wrong JSON type is a 4xx, never a 500 traceback.

xfail(strict=True) marks a confirmed defect; the marker comes off with the fix.
"""
import collections
import os
import re
import socket
import threading

import pytest
from flask import Flask, got_request_exception

import auth
import client_api
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
CSRF = {"X-CSRF": "edge-csrf"}


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    import guest_marketing
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    guest_marketing.init_guest_marketing(db_path=db_path)


# ── public guest opt-in (SEC-17) ─────────────────────────────────────────────

def _marketing_restaurant(db_path):
    return create_restaurant(Restaurant(name="Text Club Co", owner_email="o@x.test", module_marketing=1), db_path=db_path)


def _contact(db_path, rid, phone):
    import guest_marketing
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT consent, unsubscribed FROM guest_contacts WHERE restaurant_id=? AND phone=?",
                       (rid, guest_marketing._normalize_phone(phone))).fetchone()
    conn.close()
    return dict(row) if row else None


def _unsubscribe(db_path, rid, phone):
    import guest_marketing
    conn = models.get_conn(db_path)
    conn.execute("UPDATE guest_contacts SET unsubscribed=1 WHERE restaurant_id=? AND phone=?",
                 (rid, guest_marketing._normalize_phone(phone)))
    conn.commit(); conn.close()


def test_a_first_public_opt_in_records_consent(db_path):
    import guest_marketing
    rid = _marketing_restaurant(db_path)
    guest_marketing.add_guest_contact_public_optin(rid, "5125550100", name="Ana", db_path=db_path)
    assert _contact(db_path, rid, "5125550100")["consent"] == 1


@pytest.mark.xfail(strict=True, reason="SEC-17: the public opt-in sets unsubscribed=0, re-enrolling a guest who texted STOP")
def test_the_public_opt_in_never_clears_an_unsubscribe(db_path):
    import guest_marketing
    rid = _marketing_restaurant(db_path)
    guest_marketing.add_guest_contact_public_optin(rid, "5125550100", name="Ana", db_path=db_path)
    _unsubscribe(db_path, rid, "5125550100")
    guest_marketing.add_guest_contact_public_optin(rid, "5125550100", name="Someone Else", db_path=db_path)
    assert _contact(db_path, rid, "5125550100")["unsubscribed"] == 1


def _optin_client():
    app = Flask(__name__, template_folder=TEMPLATES)
    app.register_blueprint(client_api.client_bp)
    auth.install_proxy_fix(app)
    c = app.test_client()
    c.set_cookie("csrf_js", CSRF["X-CSRF"])
    return c


def _optin(c, token, i, xff):
    return c.post(f"/api/public/guest-optin/{token}", headers=dict(CSRF, **{"X-Forwarded-For": xff}),
                  json={"name": f"Guest {i}", "phone": f"51255501{i:02d}", "consent": True})


def test_the_public_opt_in_throttles_one_address(db_path):
    import guest_links
    rid = _marketing_restaurant(db_path)
    c = _optin_client()
    token = guest_links.sign_join(rid)
    statuses = [_optin(c, token, i, "203.0.113.9").status_code for i in range(8)]
    assert statuses[0] == 200 and 429 in statuses


@pytest.mark.xfail(strict=True, reason="SEC-17: the opt-in throttle keys on the client-sent first X-Forwarded-For value")
def test_a_rotating_forwarded_for_does_not_reset_the_opt_in_throttle(db_path):
    import guest_links
    rid = _marketing_restaurant(db_path)
    c = _optin_client()
    token = guest_links.sign_join(rid)
    statuses = [_optin(c, token, i, f"10.6.0.{i}, 203.0.113.9").status_code for i in range(8)]
    assert 429 in statuses


@pytest.mark.xfail(strict=True, reason="SEC-17: a stranger with the join link re-subscribes an unsubscribed number through the route")
def test_the_public_opt_in_page_cannot_resubscribe_a_number_that_opted_out(db_path):
    import guest_links, guest_marketing
    rid = _marketing_restaurant(db_path)
    guest_marketing.add_guest_contact_public_optin(rid, "5125550142", name="Ana", db_path=db_path)
    _unsubscribe(db_path, rid, "5125550142")
    c = _optin_client()
    c.post(f"/api/public/guest-optin/{guest_links.sign_join(rid)}", headers=CSRF,
           json={"name": "Prankster", "phone": "5125550142", "consent": True})
    assert _contact(db_path, rid, "5125550142")["unsubscribed"] == 1


# ── the public status page (SEC-38) ──────────────────────────────────────────

@pytest.fixture
def status_client(db_path, monkeypatch):
    import status_manager
    import status_routes
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    status_manager.seed_default_services()          # boot-time state
    app = Flask(__name__, template_folder=TEMPLATES)
    app.register_blueprint(status_routes.status_bp)
    return app.test_client(), status_routes


def test_the_public_status_page_renders(status_client):
    c, _ = status_client
    assert c.get("/status").status_code == 200


@pytest.mark.xfail(strict=True, reason="SEC-38: the public /status page runs seed_default_services (a write) on every GET")
def test_the_public_status_page_does_not_write_on_a_get(status_client, monkeypatch):
    c, status_routes = status_client
    writes = []
    monkeypatch.setattr(status_routes, "seed_default_services", lambda *a, **k: writes.append(1))
    assert c.get("/status").status_code == 200
    assert writes == []


# ── JSON bodies that are not objects (SEC-32) ────────────────────────────────

# Routes whose handlers send, generate, sync or spend — skipped so a
# malformed body can never reach a real side effect. The same list the
# audit's fuzz probe used.
_SKIP = re.compile(r"send-test|report-bug|referral|request-deletion|export-data|guest-campaign/send|send-order|"
                   r"publish|post-to|google-post|send-review-request|test-digest|test-push|webhook/test|sync|"
                   r"refresh|generate|ask-cavnar|regenerate|redraft|seed|fetch|resend|freeze|reset|logout|"
                   r"stop-viewing|view-as|invite")


@pytest.fixture
def fuzz_app(db_path, monkeypatch):
    import admin_routes
    import auth_routes
    import mobile_api
    import social_routes
    import strategy_routes
    import push
    import webhooks
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (auth_routes, mobile_api, admin_routes, strategy_routes, social_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for init in (models.init_two_fa_backup_codes, models.init_email_log, models.init_staff_notes,
                 models.init_staff_availability, models.init_staff_capabilities, models.init_shift_profiles,
                 models.init_capability_changes, models.init_ask_memory, models.init_competitor_snapshots,
                 models.init_ai_visibility_queries, webhooks.init_webhooks, push.init_push):
        init(db_path=db_path)

    def _blocked(*a, **k):
        raise OSError("network blocked in the SEC-32 fuzz")
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", lambda self, *a, **k: _blocked())
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    for k in ("ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "GOOGLE_PLACES_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(k, "")

    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "test-secret"
    app.config["PROPAGATE_EXCEPTIONS"] = False
    for bp in (client_api.client_bp, mobile_api.mobile_bp, strategy_routes.strategy_bp,
               strategy_routes.strategy_mobile_bp, auth_routes.auth_bp):
        app.register_blueprint(bp)
    return app


@pytest.mark.xfail(strict=True, reason="SEC-32: ~170 POST routes answer 500 to a JSON body that is a string or an array")
def test_every_post_route_answers_4xx_never_500_to_a_json_body_that_is_not_an_object(fuzz_app, db_path):
    crashes = collections.OrderedDict()

    def _record(sender, exception, **extra):
        from flask import request
        rule = request.url_rule.rule if request.url_rule else request.path
        crashes.setdefault(rule, type(exception).__name__ + ": " + str(exception)[:80])
    got_request_exception.connect(_record, fuzz_app)

    rid = create_restaurant(Restaurant(name="Fuzz Co", owner_email="o@x.test"), db_path=db_path)
    uid = create_user(rid, "owner", "o@x.test", "ownerpass1", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    tok = create_session(uid, device_type="ios", db_path=db_path)
    c = fuzz_app.test_client()
    c.set_cookie("session_token", tok)
    c.set_cookie("csrf_js", CSRF["X-CSRF"])
    headers = dict(CSRF, Authorization=f"Bearer {tok}")
    tried = 0
    for rule in fuzz_app.url_map.iter_rules():
        methods = [m for m in rule.methods if m in ("POST", "PATCH", "PUT", "DELETE")]
        if not methods or _SKIP.search(rule.rule):
            continue
        path = re.sub(r"<(?:int:)?[^>]+>", "1", rule.rule)
        for m in methods:
            for body in ('"x"', "[1]"):
                tried += 1
                c.open(path, method=m, data=body, content_type="application/json", headers=headers)
    assert tried > 100                                     # the sweep really ran
    assert dict(crashes) == {}
