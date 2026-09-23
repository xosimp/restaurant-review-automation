"""Edge cases for email: the deliver() choke point, unsubscribe links, the
Resend webhook, third-party templates, the guest newsletter, and the send
sites that still call the Resend SDK directly.

Every test comes from the MOD edge-case audit (appendix A7, the Newsletter
part of A6, findings MOD-EML-1..9 and the email half of MOD-MKT-11). The
promises protected: mail that leaves under the restaurant's or Cavnar's
name cannot be turned into phishing by whatever text an owner typed; an
address that bounced or complained is never mailed again through a side
door; an unsubscribe happens only when a person asks for it; and a retry
never mails anybody twice.

Resend is never reached: requests.post and resend.Emails.send are recorders.
xfail(strict=True) tests assert the CORRECT behaviour for a confirmed defect
and flip to a failure the day it is fixed.
"""
import ast
import base64
import datetime as _dtmod
import email.utils
import hashlib
import hmac
import json
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests
from flask import Flask

import admin_routes
import auth
import client_api
import emails
import guest_email
import guest_marketing as gm
import mobile_api
import models
import scheduler
import webhook_routes
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, get_conn, suppress_email, update_restaurant

REPO = pathlib.Path(__file__).resolve().parent.parent


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
    for mod in (models, auth, client_api, mobile_api, admin_routes, webhook_routes, guest_email, gm, scheduler):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    gm.init_guest_marketing(db_path)


@pytest.fixture
def outbox(monkeypatch):
    """Everything that would have gone through emails.deliver."""
    box = []

    def rec(payload=None, restaurant_id=None, email_type=None, log_send=True):
        box.append({"payload": dict(payload or {}), "rid": restaurant_id, "type": email_type})
        return emails.SendResult(True, message_id=f"msg_{len(box)}", status_code=200, attempts=1)
    monkeypatch.setattr(emails, "deliver", rec)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    return box


@pytest.fixture
def sdk(monkeypatch):
    """Everything that would have gone through the Resend SDK directly."""
    import resend
    box = []
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda payload: box.append(dict(payload)) or {"id": "x"}),
                        raising=False)
    return box


def _rid(db_path, name="Mail Co", **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email=kw.pop("owner_email", "owner@x.test")), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _email_log(db_path):
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT email_type, to_email, status FROM email_log")]
    finally:
        conn.close()


EVIL = 'Joe\'s, Bar & Grill <a href="https://evil.example/pay">Your invoice is overdue</a>'


# ── deliver() ───────────────────────────────────────────────────────────────

class _R:
    def __init__(self, code, body='{"id":"msg_1"}'):
        self.status_code, self.text = code, body

    def json(self):
        return json.loads(self.text)


def _stub_post(monkeypatch, behaviours):
    """behaviours: list of _R or Exception instances, consumed in order."""
    calls = []
    seq = list(behaviours)

    def post(url, headers=None, json=None, timeout=None, **kw):
        calls.append({"headers": dict(headers or {}), "json": json, "timeout": timeout})
        b = seq.pop(0) if seq else _R(200)
        if isinstance(b, BaseException):
            raise b
        return b
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def test_a_retried_send_carries_the_same_idempotency_key(db_path, monkeypatch):
    """A7 deliver #10 / MOD-EML-8."""
    calls, _ = _stub_post(monkeypatch, [requests.Timeout("read timed out"), _R(200)])
    emails.deliver({"to": ["a@x.test"], "subject": "s", "html": "x"}, email_type="send_welcome_email")
    keys = [c["headers"].get("Idempotency-Key") for c in calls]
    assert len(keys) == 2 and keys[0] and keys[0] == keys[1], keys


@pytest.mark.xfail(strict=True, reason="MOD-EML-9: only to[0] is checked against suppression; the rest of a multi-recipient send goes out unchecked")
def test_every_recipient_of_a_multi_recipient_send_is_checked_against_suppression(db_path, monkeypatch):
    """A7 deliver #11 / MOD-EML-9 (latent: no caller passes two today)."""
    suppress_email("gone@x.test", "bounced", db_path=db_path)
    calls, _ = _stub_post(monkeypatch, [_R(200)])
    emails.deliver({"to": ["ok@x.test", "gone@x.test"], "subject": "s", "html": "x"},
                   email_type="send_welcome_email")
    assert all("gone@x.test" not in (c["json"] or {}).get("to", []) for c in calls)


@pytest.mark.xfail(strict=True, reason="MOD-EML-8: one email can hold its caller ~46s (3 x 15s timeouts plus backoff) during a Resend brownout")
def test_one_email_cannot_hold_a_request_thread_for_most_of_a_minute(db_path, monkeypatch):
    """A7 deliver #12 / MOD-EML-8 — the worst case, added up from the
    timeouts deliver() asks for and the backoff it sleeps."""
    calls, sleeps = _stub_post(monkeypatch, [requests.Timeout("t")] * 5)
    emails.deliver({"to": ["a@x.test"], "subject": "s", "html": "x"}, email_type="send_welcome_email")
    worst = sum((c["timeout"] if isinstance(c["timeout"], (int, float)) else sum(c["timeout"])) for c in calls) + sum(sleeps)
    assert worst <= 20, f"worst case {worst:.1f}s per email"


@pytest.mark.xfail(strict=True, reason="MOD-EML-9: deliver() pops 'preheader' out of the caller's own dict")
def test_deliver_does_not_mutate_the_callers_payload(db_path, monkeypatch):
    """MOD-EML-9 — a caller that retries with the same dict loses its
    preheader."""
    _stub_post(monkeypatch, [_R(200)])
    payload = {"to": ["a@x.test"], "subject": "s", "html": "x", "preheader": "Your code is inside"}
    emails.deliver(payload, email_type="send_welcome_email")
    assert payload.get("preheader") == "Your code is inside"


# ── Unsubscribe ─────────────────────────────────────────────────────────────

@pytest.fixture
def web(db_path):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(admin_routes.admin_bp)
    app.register_blueprint(webhook_routes.webhook_bp)
    return app


def _guest_subscriber(db_path, rid, email_addr="guest@x.test", phone="5559990001"):
    cid = gm.add_guest_contact_public_optin(rid, phone, name="Gia Guest", db_path=db_path)
    guest_email.set_guest_email(cid, rid, email_addr, consent=True, db_path=db_path)
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT email_token FROM guest_contacts WHERE id=?", (cid,)).fetchone()[0], cid
    finally:
        conn.close()


def _guest_unsubscribed(db_path, cid):
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT email_unsubscribed FROM guest_contacts WHERE id=?", (cid,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="MOD-EML-5: GET /u/<token> unsubscribes, so link scanners and prefetchers opt owners out")
def test_fetching_the_owner_unsubscribe_link_does_not_unsubscribe(db_path, web):
    """A7 Unsubscribe #4 / MOD-EML-5 — GET shows a confirm button; only POST
    (RFC 8058 one-click) writes. tests/test_email_delivery.py pins the GET
    write today; both change together."""
    rid = _rid(db_path)
    web.test_client().get(f"/u/{models.unsubscribe_token(rid)}")
    assert not models.get_restaurant(rid, db_path).marketing_emails_opt_out


@pytest.mark.xfail(strict=True, reason="MOD-EML-5: GET /e/<token> unsubscribes a newsletter guest on a link prefetch")
def test_fetching_the_guest_unsubscribe_link_does_not_unsubscribe(db_path, web):
    """A6 Newsletter #8 / A7 Unsubscribe #4 / MOD-EML-5."""
    rid = _rid(db_path)
    token, cid = _guest_subscriber(db_path, rid)
    web.test_client().get(f"/e/{token}")
    assert _guest_unsubscribed(db_path, cid) == 0


def test_the_guest_unsubscribe_link_works_by_post_without_signing_in(db_path, web):
    """A7 Unsubscribe #6 — the /e/ route over HTTP, not just the function."""
    rid = _rid(db_path)
    token, cid = _guest_subscriber(db_path, rid)
    resp = web.test_client().post(f"/e/{token}")
    assert resp.status_code == 200
    assert _guest_unsubscribed(db_path, cid) == 1
    assert web.test_client().post("/e/not-a-real-token").status_code == 404


@pytest.mark.xfail(strict=True, reason="MOD-EML-9: rotating SECRET_KEY invalidates every owner unsubscribe link already in inboxes")
def test_an_old_unsubscribe_link_survives_a_secret_key_rotation(db_path, monkeypatch):
    """A7 Unsubscribe #5 / MOD-EML-9 — the docstring promises an old link in
    an old email never stops working."""
    rid = _rid(db_path)
    monkeypatch.setenv("SECRET_KEY", "the-old-secret")
    token = models.unsubscribe_token(rid)
    monkeypatch.setenv("SECRET_KEY", "the-new-secret")
    assert models.verify_unsubscribe_token(token) == rid


# ── The Resend webhook ──────────────────────────────────────────────────────

_SECRET = "whsec_" + base64.b64encode(b"topsecret").decode()


def _svix(body: bytes, ts: int):
    key = base64.b64decode(_SECRET.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"msg_1.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": "msg_1", "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}",
            "Content-Type": "application/json"}


@pytest.mark.xfail(strict=True, reason="MOD-EML-9: the Resend webhook never checks svix-timestamp, so a captured event replays forever")
def test_a_replayed_resend_event_ten_minutes_old_is_rejected(db_path, web, monkeypatch):
    """A7 Webhooks #6 / MOD-EML-9. (The existing webhook tests sign with a
    2023 timestamp and expect 200; they change with the fix.)"""
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    body = json.dumps({"type": "email.complained", "data": {"email_id": "m1", "to": ["victim@x.test"]}}).encode()
    resp = web.test_client().post("/webhooks/resend", data=body, headers=_svix(body, int(time.time()) - 600))
    assert resp.status_code in (400, 401, 403)
    assert models.is_email_suppressed("victim@x.test", db_path=db_path) is False


@pytest.mark.xfail(strict=True, reason="MOD-EML-9: RESEND_WEBHOOK_SECRET is frozen at import, so setting it needs a restart")
def test_the_resend_webhook_reads_its_secret_when_the_request_arrives(db_path, web, monkeypatch):
    """MOD-EML-9 — the frozen-key pattern the repo already fixed for
    RESEND_API_KEY."""
    monkeypatch.setenv("RESEND_WEBHOOK_SECRET", _SECRET)
    body = json.dumps({"type": "email.delivered", "data": {"email_id": "m1", "to": ["a@x.test"]}}).encode()
    resp = web.test_client().post("/webhooks/resend", data=body, headers=_svix(body, int(time.time())))
    assert resp.status_code == 200


@pytest.mark.xfail(strict=True, reason="MOD-EML-7: one guest's newsletter complaint suppresses that address platform-wide, including staff schedules")
def test_a_newsletter_complaint_does_not_stop_that_persons_staff_schedule(db_path, web, monkeypatch):
    """A7 Webhooks #7 / MOD-EML-7 — the same person is a guest of A and on
    staff at B; complaining about A's newsletter must not silently stop B's
    schedules."""
    calls, _ = _stub_post(monkeypatch, [_R(200, '{"id":"msg_nl"}'), _R(200, '{"id":"msg_sched"}')])
    emails.deliver({"to": ["maria@x.test"], "subject": "News", "html": "x"}, restaurant_id=1,
                   email_type="guest_newsletter")
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    body = json.dumps({"type": "email.complained", "data": {"email_id": "msg_nl", "to": ["maria@x.test"]}}).encode()
    assert web.test_client().post("/webhooks/resend", data=body, headers=_svix(body, int(time.time()))).status_code == 200
    result = emails.send_staff_schedule_email("maria@x.test", "Maria G.", "Bravo Bistro", "Sep 21-27",
                                              "https://x.test/s/1", [])
    assert result.ok is True


# ── Third-party templates ───────────────────────────────────────────────────

def _render(kind, name):
    if kind == "supplier":
        emails.send_supplier_order_email("sup@x.test", f"{name} Produce", name, "PO-1",
                                         [{"item": f"<b>{name}</b> tomatoes", "qty": 3, "unit": "cs"}], 42.0)
    elif kind == "staff":
        emails.send_staff_schedule_email("staff@x.test", f"<img src=x onerror=alert(1)>{name}", name,
                                         "Sep 21-27", "https://x.test/s/1",
                                         [{"day": "Mon", "start": "09:00", "end": "17:00", "role": "Server"}])
    elif kind == "invite":
        emails.send_team_invite_email("mate@x.test", name, "mate", "Temp-pass-1", inviter_name=name)


@pytest.mark.parametrize("kind", ["supplier", "staff", "invite"])
@pytest.mark.xfail(strict=True, reason="MOD-EML-2: restaurant, supplier, employee and item names are interpolated unescaped into third-party email")
def test_names_in_third_party_templates_are_escaped(db_path, outbox, kind):
    """A7 Templates #5 / MOD-EML-2 — a restaurant name carrying an anchor
    must arrive as text, not a link Cavnar signed."""
    _render(kind, EVIL)
    html = outbox[-1]["payload"]["html"]
    assert '<a href="https://evil.example/pay">' not in html
    assert "&lt;a href=" in html


@pytest.mark.parametrize("kind", ["supplier", "staff"])
@pytest.mark.xfail(strict=True, reason="MOD-EML-2: the raw restaurant name is used as the From display name")
def test_the_from_header_is_one_mailbox_whatever_the_restaurant_is_called(db_path, outbox, kind):
    """A7 Templates #6 / MOD-EML-2 — comma, quote and angle brackets."""
    _render(kind, EVIL)
    parsed = email.utils.getaddresses([outbox[-1]["payload"]["from"]])
    assert len(parsed) == 1 and parsed[0][1] == emails._from_email(), parsed


def test_a_unicode_restaurant_name_survives_into_the_subject_and_from(db_path, outbox):
    """A7 Templates #7 — accents and emoji are ordinary restaurant names."""
    _render("supplier", "Café Olé 🌮")
    p = outbox[-1]["payload"]
    assert "Café Olé 🌮" in p["subject"]
    assert email.utils.getaddresses([p["from"]])[0][1] == emails._from_email()


def test_a_very_long_restaurant_name_still_produces_a_sendable_email(db_path, outbox):
    """A7 Templates #7 — a 300-character name is rendered, not a crash."""
    _render("supplier", "Long " * 60)
    assert outbox and outbox[-1]["payload"]["subject"].startswith("Order PO-1")


def _freeze_now(monkeypatch, utc_dt):
    real = _dtmod.datetime

    class _Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return utc_dt.astimezone(tz) if tz else utc_dt.replace(tzinfo=None)
    monkeypatch.setattr(_dtmod, "datetime", _Frozen)


@pytest.mark.parametrize("which", ["login", "password_changed", "email_changed"])
@pytest.mark.xfail(strict=True, reason="MOD-EML-9: security emails print 'Sep 02, 2026 at 03:04 PM CT' instead of M/D/YY, always in Central time")
def test_security_emails_print_dates_as_m_d_yy(db_path, outbox, monkeypatch, which):
    """A7 Templates #8 / MOD-EML-9 — CLAUDE.md: an owner-facing date reads
    9/2/26, no leading zeros."""
    from zoneinfo import ZoneInfo
    _freeze_now(monkeypatch, datetime(2026, 9, 2, 15, 4, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc))
    if which == "login":
        emails.send_login_notification("o@x.test", "Mail Co", ip="1.2.3.4", user_agent="iPhone Safari/1")
    elif which == "password_changed":
        emails.send_password_changed_email("o@x.test", "Mail Co")
    else:
        emails.send_email_changed_email("o@x.test", "Mail Co", "new@x.test")
    html = outbox[-1]["payload"]["html"]
    assert "9/2/26" in html
    assert "Sep 02" not in html


def test_an_order_with_nothing_to_order_is_refused_not_emailed(db_path, web, outbox):
    """A7 Templates #9 — a restaurant with no stock data gets 'nothing to
    order', never an empty purchase order in a supplier's inbox."""
    rid = _rid(db_path, module_inventory=1)
    create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    c = web.test_client()
    tok = c.post("/mobile/api/login", json={"username": "owner", "password": "pw-owner-1"}).get_json()["token"]
    resp = c.post("/mobile/api/food-cost/send-order", json={}, headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 400
    assert [m for m in outbox if m["type"] == "send_supplier_order_email"] == []


def test_a_digest_for_a_restaurant_with_no_owner_email_is_skipped_and_logged(db_path, outbox, monkeypatch):
    """A7 Templates #10 — the digest half: skipped, and it says so in the
    log rather than failing silently. (The failed-post alert half is
    tests/test_edge_mod_b_mkt_queue.py.)"""
    rid = _rid(db_path, owner_email="", digest_enabled=1, digest_day="monday", module_reviews=1)
    from auth import set_user_role
    mgr = create_user(rid, "mgr", "mgr@x.test", "pw-mgr-123", db_path=db_path)
    set_user_role(mgr, "manager", db_path=db_path)
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 9, 21, 9, 30))   # a Monday
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    warned = []
    monkeypatch.setattr(scheduler.log, "warning", lambda msg, *a, **k: warned.append(str(msg)))
    scheduler.run_weekly_digests()
    assert any("No email" in w and "Mail Co" in w for w in warned), warned
    assert outbox == []


# ── The guest newsletter ────────────────────────────────────────────────────

def _newsletter_world(db_path, n=5, name="Mama's, Kitchen"):
    rid = _rid(db_path, name=name, module_marketing=1)
    for i in range(n):
        _guest_subscriber(db_path, rid, email_addr=f"g{i}@x.test", phone=f"55588800{i:02d}")
    return rid


@pytest.mark.xfail(strict=True, reason="MOD-EML-2: a comma or quote in the restaurant name splits the newsletter From header into two mailboxes")
def test_a_comma_in_the_restaurant_name_still_makes_one_from_mailbox(db_path, outbox):
    """A6 Newsletter #5 / MOD-EML-2."""
    rid = _newsletter_world(db_path, n=1)
    guest_email.send_newsletter(rid, "BODY:\nTruffle season starts Friday.", subject="News", db_path=db_path)
    parsed = email.utils.getaddresses([outbox[0]["payload"]["from"]])
    assert len(parsed) == 1 and parsed[0][1] == emails._from_email(), parsed


@pytest.mark.xfail(strict=True, reason="MOD-EML-3: the newsletter is one synchronous Resend call per subscriber inside the request")
def test_a_newsletter_is_not_mailed_one_by_one_on_the_request_thread(db_path, outbox):
    """A6 Newsletter #6 / MOD-EML-3 — the 5,000-subscriber case scaled to
    60: the send is handed to a batch/background sender, not looped inline
    (Resend's per-team rate limit makes the inline loop tens of minutes)."""
    rid = _newsletter_world(db_path, n=60, name="Big List Co")
    guest_email.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db_path)
    assert len(outbox) < 60, f"{len(outbox)} synchronous sends"


class _Killed(BaseException):
    pass


@pytest.mark.xfail(strict=True, reason="MOD-EML-3: a newsletter interrupted mid-send keeps no record, so the re-send mails the first recipients again")
def test_a_newsletter_interrupted_mid_send_mails_only_the_rest_when_resent(db_path, monkeypatch):
    """A6 Newsletter #7 / MOD-EML-3."""
    rid = _newsletter_world(db_path, n=5, name="Resume Co")
    got = []

    def dies_after_two(payload=None, **k):
        if len(got) == 2:
            raise _Killed()
        got.append(payload["to"][0])
        return emails.SendResult(True, attempts=1)
    monkeypatch.setattr(emails, "deliver", dies_after_two)
    try:
        guest_email.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db_path)
    except _Killed:
        pass
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: got.append(payload["to"][0]) or emails.SendResult(True, attempts=1))
    guest_email.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db_path)
    assert sorted(got) == sorted(set(got)) and len(got) == 5, got


@pytest.mark.xfail(strict=True, reason="MOD-EML-7: suppressed (bounced/complained) addresses still count as newsletter subscribers")
def test_a_suppressed_address_is_not_counted_as_a_subscriber(db_path):
    """A6 Newsletter #9 / MOD-EML-7."""
    rid = _newsletter_world(db_path, n=3, name="Count Co")
    suppress_email("g0@x.test", "bounced", db_path=db_path)
    assert guest_email.subscriber_count(rid, db_path=db_path) == 2


@pytest.mark.xfail(strict=True, reason="MOD-EML-6: the guest newsletter carries no physical postal address (CAN-SPAM)")
def test_the_newsletter_carries_a_postal_address_and_a_list_unsubscribe_header(db_path, outbox):
    """A6 Newsletter #10 / MOD-EML-6."""
    rid = _newsletter_world(db_path, n=1, name="Postal Co")
    # No postal-address field exists yet; whichever name the fix picks, it
    # is set here (update_restaurant ignores keys it does not know).
    update_restaurant(rid, {k: "123 Main St, Geneva, IL 60134" for k in
                            ("address", "mailing_address", "postal_address", "street_address")},
                      db_path=db_path)
    guest_email.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db_path)
    p = outbox[0]["payload"]
    assert "List-Unsubscribe" in p.get("headers", {})
    assert "123 Main St" in p["html"]


# ── Send sites that bypass deliver() ────────────────────────────────────────

def _session_client(web, db_path, rid, username="owner"):
    uid = create_user(rid, username, f"{username}@x.test", "pw-owner-1", db_path=db_path)
    c = web.test_client()
    c.set_cookie("session_token", create_session(uid))
    return c


def test_the_referral_route_refuses_the_eleventh_referral_in_an_hour(db_path, web, sdk):
    """A7 Direct SDK #1 / MOD-EML-1."""
    rid = _rid(db_path)
    c = _session_client(web, db_path, rid)
    codes = [c.post("/api/send-referral", json={"name": f"R{i}", "email": f"r{i}@x.test"}).get_json().get("ok")
             for i in range(11)]
    assert codes[-1] is False, codes


def test_the_referral_note_arrives_escaped(db_path, web, sdk):
    """A7 Direct SDK #1 / MOD-EML-1."""
    rid = _rid(db_path)
    c = _session_client(web, db_path, rid)
    c.post("/api/send-referral", json={"name": "R", "email": "r@x.test",
                                       "note": '<a href="https://evil.example">claim your refund</a>'})
    to_referee = [m for m in sdk if m["to"] == ["r@x.test"]]
    assert to_referee and '<a href="https://evil.example">' not in to_referee[0]["html"]


def test_a_referral_to_a_suppressed_address_is_not_sent(db_path, web, sdk):
    """A7 Direct SDK #1 / MOD-EML-1."""
    rid = _rid(db_path)
    suppress_email("r@x.test", "complained", db_path=db_path)
    c = _session_client(web, db_path, rid)
    c.post("/api/send-referral", json={"name": "R", "email": "r@x.test"})
    assert [m for m in sdk if m["to"] == ["r@x.test"]] == []


def _review_request(db_path, monkeypatch, rid, email_addr="ana@x.test"):
    monkeypatch.setenv("RESEND_API_KEY", "k")
    monkeypatch.setattr(client_api, "get_restaurant", lambda r: models.get_restaurant(r, db_path))
    return client_api._do_send_review_request(rid, {"name": "Ana", "email": email_addr})


@pytest.mark.xfail(strict=True, reason="MOD-EML-4: the guest review-request email skips the suppression list")
def test_a_review_request_is_not_emailed_to_a_suppressed_guest(db_path, sdk, monkeypatch):
    """A7 Direct SDK #2 / MOD-EML-4."""
    rid = _rid(db_path, google_place_id="ChIJreview")
    suppress_email("ana@x.test", "bounced", db_path=db_path)
    _review_request(db_path, monkeypatch, rid)
    assert sdk == []


@pytest.mark.xfail(strict=True, reason="MOD-EML-6: the guest review-request email has no unsubscribe link at all")
def test_a_review_request_email_carries_an_unsubscribe(db_path, sdk, monkeypatch):
    """A7 Direct SDK #2 / MOD-EML-6."""
    rid = _rid(db_path, google_place_id="ChIJreview")
    _review_request(db_path, monkeypatch, rid)
    assert sdk and ("unsubscribe" in sdk[0]["html"].lower() or "List-Unsubscribe" in (sdk[0].get("headers") or {}))


@pytest.mark.xfail(strict=True, reason="MOD-EML-4: the guest review-request email is not written to email_log")
def test_a_review_request_email_is_in_the_owners_email_history(db_path, sdk, monkeypatch):
    """A7 Direct SDK #2 / MOD-EML-4."""
    rid = _rid(db_path, google_place_id="ChIJreview")
    _review_request(db_path, monkeypatch, rid)
    assert [r for r in _email_log(db_path) if r["to_email"] == "ana@x.test"]


@pytest.mark.xfail(strict=True, reason="MOD-EML-2: the restaurant name is interpolated unescaped into the guest review-request email")
def test_the_review_request_escapes_the_restaurant_name(db_path, sdk, monkeypatch):
    """A7 Templates #5 / MOD-EML-2 — the review-request template."""
    rid = _rid(db_path, name=EVIL, google_place_id="ChIJreview")
    _review_request(db_path, monkeypatch, rid)
    assert sdk and '<a href="https://evil.example/pay">' not in sdk[0]["html"]


def _urgent(monkeypatch, owner="owner@x.test"):
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "k")
    scheduler.send_urgent_alert("Mail Co", owner, [{"id": None, "rating": 1, "author": "A B",
                                                     "platform": "google", "text": "awful"}])


@pytest.mark.xfail(strict=True, reason="MOD-EML-4: the urgent review alert skips the suppression list")
def test_the_urgent_review_alert_respects_suppression(db_path, sdk, monkeypatch):
    """A7 Direct SDK #3 / MOD-EML-4."""
    suppress_email("owner@x.test", "bounced", db_path=db_path)
    _urgent(monkeypatch)
    assert sdk == []


@pytest.mark.xfail(strict=True, reason="MOD-EML-4: the urgent review alert is not written to email_log")
def test_the_urgent_review_alert_is_in_email_history(db_path, sdk, monkeypatch):
    """A7 Direct SDK #3 / MOD-EML-4."""
    _urgent(monkeypatch)
    assert sdk, "the alert should have been sent"
    assert [r for r in _email_log(db_path) if r["to_email"] == "owner@x.test"]


def _paid_invoice(db_path, monkeypatch, web, email_addr="pay@x.test"):
    import types
    rid = _rid(db_path, owner_email=email_addr, billing_status="trial", stripe_customer_id="cus_pay")
    create_user(rid, "payer", email_addr, "pw-payer-1", db_path=db_path)
    fake = types.ModuleType("stripe")
    event = {"id": "evt_paid", "type": "invoice.paid", "data": {"object": {
        "customer": "cus_pay", "customer_email": email_addr, "amount_paid": 75000,
        "billing_reason": "subscription_create"}}}
    fake.Webhook = type("W", (), {"construct_event": staticmethod(lambda *a, **k: event)})
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setattr(webhook_routes, "_resend_key", lambda: "k")
    web.test_client().post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t"})
    return rid


def test_the_payment_receipt_is_logged(db_path, web, sdk, monkeypatch):
    """A7 Direct SDK #4 — the receipt reaches email_log (it does today)."""
    _paid_invoice(db_path, monkeypatch, web)
    assert [m for m in sdk if m["to"] == ["pay@x.test"]]
    assert [r for r in _email_log(db_path) if r["to_email"] == "pay@x.test"]


@pytest.mark.xfail(strict=True, reason="MOD-EML-4: the payment receipt skips the suppression list")
def test_the_payment_receipt_respects_suppression(db_path, web, sdk, monkeypatch):
    """A7 Direct SDK #4 / MOD-EML-4."""
    suppress_email("pay@x.test", "bounced", db_path=db_path)
    _paid_invoice(db_path, monkeypatch, web)
    assert [m for m in sdk if m["to"] == ["pay@x.test"]] == []


def _export(db_path, web):
    rid = _rid(db_path, module_reviews=1)
    create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    c = web.test_client()
    tok = c.post("/mobile/api/login", json={"username": "owner", "password": "pw-owner-1"}).get_json()["token"]
    return c.post("/mobile/api/account/export-data", json={"scopes": ["reviews"]},
                  headers={"Authorization": f"Bearer {tok}"})


def test_the_data_export_email_is_logged(db_path, web, sdk):
    """A7 Direct SDK #5."""
    assert _export(db_path, web).status_code == 200
    assert [r for r in _email_log(db_path) if r["to_email"] == "owner@x.test"]


@pytest.mark.xfail(strict=True, reason="MOD-EML-4: the data export email skips the suppression list")
def test_the_data_export_email_respects_suppression(db_path, web, sdk):
    """A7 Direct SDK #5 / MOD-EML-4."""
    suppress_email("owner@x.test", "bounced", db_path=db_path)
    _export(db_path, web)
    assert sdk == []


def _direct_sdk_sends():
    hits = []
    for path in REPO.glob("*.py"):
        if path.name == "emails.py":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "send":
                v = node.func.value
                if isinstance(v, ast.Attribute) and v.attr == "Emails":
                    hits.append(f"{path.name}:{node.lineno}")
    return hits


def test_the_direct_sdk_scan_finds_the_known_sites():
    """A7 Direct SDK #6 — the lint below is only worth anything if it can
    see a call; this pins that it does."""
    assert any(h.startswith("admin_routes.py:") for h in _direct_sdk_sends())


@pytest.mark.xfail(strict=True, reason="MOD-EML-4: ~17 send sites call resend.Emails.send directly, bypassing suppression, email_log, flood guard and retry")
def test_no_module_but_emails_calls_the_resend_sdk_directly():
    """A7 Direct SDK #6 / MOD-EML-4 — the lint the finding asks for."""
    assert _direct_sdk_sends() == []
