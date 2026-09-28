"""Inbound SMS handling and Toast-sourced opt-in invites.

Two things are pinned down here. First, that STOP actually unsubscribes —
every outbound message this system sends promises it, and until the inbound
webhook existed nothing honoured that promise. Second, that a phone number
Toast captured at checkout never becomes consented on the guest's behalf;
only the guest replying YES can do that.
"""
import base64
import hashlib
import hmac
from datetime import date

import pytest
from flask import Flask

import guest_marketing as gm
import models
import notify
import webhook_routes
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, gm, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    gm.init_guest_marketing(db_path)
    monkeypatch.setattr(gm, "DB_PATH", db_path)


@pytest.fixture(autouse=True)
def _inside_texting_hours(monkeypatch):
    """Guest texts are held outside 8am-9pm local (see
    guest_marketing.guest_sms_allowed_now). Pin the clock the window reads so
    the suite means the same thing at 2am on CI as it does at noon."""
    from datetime import datetime as _dt
    monkeypatch.setattr(gm, "_sms_local_now",
                        lambda rid: _dt.now().replace(hour=12, minute=0))


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.register_blueprint(webhook_routes.webhook_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Text Club Co")
    kw.setdefault("owner_email", "w@x.com")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _sent_sms(monkeypatch):
    sent = []
    monkeypatch.setattr(gm, "send_sms", lambda phone, msg, **kw: sent.append((phone, msg)) or True)
    return sent


# ── Inbound: STOP ────────────────────────────────────────────────────────

def test_stop_actually_unsubscribes(db_path, monkeypatch):
    rid = _restaurant(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    reply = gm.handle_inbound_sms("+15551234567", "STOP", db_path=db_path)
    contact = gm.get_guest_contacts(rid, db_path=db_path)[0]
    assert contact["unsubscribed"] is True
    assert "unsubscribed" in reply.lower()


def test_stop_is_honoured_across_every_restaurant_with_that_number(db_path):
    """Someone saying stop means stop — not stop from whichever tenant we
    happened to guess they were replying to."""
    a = _restaurant(db_path, name="Place A")
    b = _restaurant(db_path, name="Place B")
    gm.add_guest_contact_public_optin(a, "5551234567", db_path=db_path)
    gm.add_guest_contact_public_optin(b, "5551234567", db_path=db_path)
    gm.handle_inbound_sms("+15551234567", "stop", db_path=db_path)
    assert gm.get_guest_contacts(a, db_path=db_path)[0]["unsubscribed"] is True
    assert gm.get_guest_contacts(b, db_path=db_path)[0]["unsubscribed"] is True


def test_an_unsubscribed_guest_stops_receiving_campaigns(db_path, monkeypatch):
    rid = _restaurant(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", db_path=db_path)
    sent = _sent_sms(monkeypatch)
    gm.handle_inbound_sms("+15551234567", "STOP", db_path=db_path)
    result = gm.send_campaign(rid, "Half price wine tonight", db_path=db_path)
    assert result["sent"] == 0 and sent == []


def test_stop_variants_all_work(db_path):
    for word in ("STOP", "stop", "Unsubscribe", "CANCEL", "quit", "end"):
        rid = _restaurant(db_path)
        gm.add_guest_contact_public_optin(rid, "5559990000", db_path=db_path)
        gm.handle_inbound_sms("+15559990000", word, db_path=db_path)
        assert gm.get_guest_contacts(rid, db_path=db_path)[0]["unsubscribed"] is True
        get_conn(db_path).execute("DELETE FROM guest_contacts").connection.commit()


# ── Inbound: YES ─────────────────────────────────────────────────────────

def test_yes_reply_grants_review_link_consent_only(db_path):
    """The invite asks "can we text you a quick link to leave a review" —
    a YES is consent to that and nothing more (MB-2, owner 9/28/26): it
    used to set marketing consent and a visit (#6)."""
    rid = _restaurant(db_path)
    gm.add_guest_contact_manual(rid, "5551234567", name="Ben", db_path=db_path)
    gm.record_optin_invite(rid, "5551234567", external_ref="order-1", db_path=db_path)

    before = gm.get_guest_contacts(rid, db_path=db_path)[0]
    assert before["consent"] is False        # Toast-sourced, not consented

    reply = gm.handle_inbound_sms("+15551234567", "YES", db_path=db_path)
    after = gm.get_guest_contacts(rid, db_path=db_path)[0]
    assert after["consent"] is False and after["review_only"] is True
    assert after["last_visit"] is None and after["visit_count"] == 0   # a YES is not a visit
    assert "Text Club Co" in reply


def test_an_inbound_from_a_stranger_is_ignored(db_path):
    """No invite, no contact row — we have no idea who they are, so we say
    nothing rather than guessing a restaurant."""
    _restaurant(db_path)
    assert gm.handle_inbound_sms("+15550009999", "YES", db_path=db_path) is None


def test_the_invite_records_the_response(db_path):
    rid = _restaurant(db_path)
    gm.record_optin_invite(rid, "5551234567", external_ref="order-1", db_path=db_path)
    gm.handle_inbound_sms("+15551234567", "yes", db_path=db_path)
    row = get_conn(db_path).execute("SELECT response, responded_at FROM sms_optin_invites").fetchone()
    assert row["response"] == "yes" and row["responded_at"] is not None


# ── Toast opt-in invites ─────────────────────────────────────────────────

_GUESTS = [{"order_guid": "order-1", "name": "Demo Guest One", "phone": "+15550000001"},
           {"order_guid": "order-2", "name": "Demo Guest Two", "phone": "+15550000002"}]


@pytest.fixture
def pos_guests(monkeypatch):
    """A POS that shares guest records for every restaurant id in `on`."""
    import pos
    import types
    on = set()
    monkeypatch.setattr(pos, "PROVIDERS", {"toast": types.SimpleNamespace(
        is_connected=lambda r: r in on, fetch_order_customers=lambda r, day: [dict(g) for g in _GUESTS],
        sync_to_db=lambda r: {}, build_shifts_csv=lambda r, days=60: None)})
    return on


def _toast_restaurant(db_path, pos_guests=None, enabled=True):
    """A real (not demo) restaurant whose owner turned invites on (MB-3)."""
    rid = _restaurant(db_path, module_marketing=1)
    if pos_guests is not None:
        pos_guests.add(rid)
    if enabled:
        gm.set_optin_invites(rid, True, user_id=1, acknowledged=True, db_path=db_path)
    return rid


def test_toast_guests_are_invited_but_never_auto_consented(db_path, monkeypatch, pos_guests):
    """The whole point: Toast handing us a number is not the guest agreeing
    to be texted marketing."""
    rid = _toast_restaurant(db_path, pos_guests)
    sent = _sent_sms(monkeypatch)
    result = gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)

    assert result["invited"] == 2
    contacts = gm.get_guest_contacts(rid, db_path=db_path)
    assert len(contacts) == 2
    assert all(c["consent"] is False for c in contacts)
    assert all("Reply YES" in msg for _, msg in sent)
    assert all("Reply STOP" in msg for _, msg in sent)


def test_a_toast_guest_cannot_be_texted_a_campaign_even_after_yes(db_path, monkeypatch, pos_guests):
    """A YES to the review-link invite is review-only consent (MB-2, owner
    9/28/26): it used to put the guest in every campaign. Only the join
    form's confirmed double opt-in makes them textable."""
    rid = _toast_restaurant(db_path, pos_guests)
    _sent_sms(monkeypatch)
    gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    assert gm.send_campaign(rid, "Come back!", db_path=db_path)["sent"] == 0

    gm.handle_inbound_sms("+15550000001", "YES", db_path=db_path)
    assert gm.send_campaign(rid, "Come back!", db_path=db_path)["sent"] == 0

    assert gm.request_public_optin(rid, "+15550000001", name="Guest One", db_path=db_path)["ok"]
    gm.handle_inbound_sms("+15550000001", "Y", db_path=db_path)
    assert gm.send_campaign(rid, "Come back!", db_path=db_path)["sent"] == 1


def test_rerunning_the_invite_job_does_not_re_text_anyone(db_path, monkeypatch, pos_guests):
    rid = _toast_restaurant(db_path, pos_guests)
    sent = _sent_sms(monkeypatch)
    gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    second = gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    assert second["invited"] == 0
    assert len(sent) == 2          # still just the original two


def test_an_unsubscribed_guest_is_never_re_invited(db_path, monkeypatch, pos_guests):
    rid = _toast_restaurant(db_path, pos_guests)
    gm.add_guest_contact_manual(rid, "+15550000001", db_path=db_path)
    gm.handle_inbound_sms("+15550000001", "STOP", db_path=db_path)
    sent = _sent_sms(monkeypatch)
    gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    assert all(phone != "+15550000001" for phone, _ in sent)


def test_a_restaurant_without_toast_is_skipped(db_path, monkeypatch, pos_guests):
    _toast_restaurant(db_path)                   # invites on, no POS sharing guests
    sent = _sent_sms(monkeypatch)
    assert gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)["invited"] == 0
    assert sent == []


# ── Webhook route ────────────────────────────────────────────────────────

def _signed(url, params, token="test-token"):
    payload = url + "".join(k + str(params[k]) for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def test_webhook_rejects_an_unsigned_request(client, db_path, monkeypatch):
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "test-token")
    resp = client.post("/webhooks/twilio/sms", data={"From": "+15551234567", "Body": "STOP"})
    assert resp.status_code == 403


def test_webhook_rejects_a_forged_signature(client, db_path, monkeypatch):
    """Without this, anyone could forge a YES and grant consent on a
    guest's behalf."""
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "test-token")
    resp = client.post("/webhooks/twilio/sms",
                       data={"From": "+15551234567", "Body": "YES"},
                       headers={"X-Twilio-Signature": "not-the-right-signature"})
    assert resp.status_code == 403


def test_webhook_accepts_a_correctly_signed_stop(client, db_path, monkeypatch):
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "test-token")
    rid = _restaurant(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", db_path=db_path)

    params = {"From": "+15551234567", "Body": "STOP"}
    url = "http://localhost/webhooks/twilio/sms"
    resp = client.post("/webhooks/twilio/sms", data=params,
                       headers={"X-Twilio-Signature": _signed(url, params)})
    assert resp.status_code == 200
    assert b"<Response>" in resp.data
    assert gm.get_guest_contacts(rid, db_path=db_path)[0]["unsubscribed"] is True
