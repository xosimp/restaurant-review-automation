"""Marketing fix round, slice C (9/28/26): the guest TEXT send path, who may
send, and the Campaign Studio's send and state.

- CS-1 / MB-14 / #20  text, win-back and newsletter sends need MARKETING_APPROVE
                      (web and mobile twins); only the account owner sets the
                      mailing address printed on every email
- CS-12 / MB-15 / #7 / #65  every campaign text opens with the restaurant's
                      name and the counter counts it; the phone preview shows
                      a number; HELP carries a contact; the owner-alert sender
                      honours a STOP; the STOP reply promises only what happens
- MB-13 / #60         a guest text carries Twilio's ValidityPeriod (seconds to
                      9 PM, at most four hours) and submissions are paced
- MB-10 / #10 / #11   the length (name and link counted) is refused BEFORE
                      anything is queued; a durable per-recipient queue the
                      scheduler resumes; texts cut off at 9 PM wait for 8 AM
                      and go; a cancel
- MB-11 / CS-5 / #61  nobody eligible: no campaign row, no tracker; the tracker
                      only once something was sent; "Text N" is the eligible N
- CS-13 / #35         failures shown; "accepted by carrier", not "delivered"
- CS-9 / #52          (tests/test_campaigns_0928.py) + cpReuse resets the day
- CS-17               an unknown audience is a 400, never "all"
- CS-4/16/14/6/22/23  the Studio: a sending flag, the armed snapshot, per-
                      channel draft sequence, resets per entry point, the real
                      link length, aria-live status, _escHtml quotes

send_sms is always a recorder here; nothing reaches Twilio.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest
from flask import Flask

import ai_utils
import auth_routes
import client_api
import guest_marketing as gm
import mobile_api
import models
import notify
import ops
import scheduler
from auth import create_user, init_auth, set_user_role
from models import Restaurant, create_restaurant, get_conn, update_restaurant

SRC = open("templates/dashboard.html", encoding="utf-8").read()
STUDIO = SRC[SRC.index("// ── Campaign Studio (owner, 9/28/26)"):SRC.index("window.copyGuestJoinLink = function")]
# The Studio's drafts go through the page's owner-AI-job helper (AI cost audit
# 10/7/26 #57: a draft route asked with async answers a job to poll); it is a
# page-wide global defined above the Studio, so the harness carries it too.
STUDIO = SRC[SRC.index("function aiJobAwait("):SRC.index("window.aiJobAwait = aiJobAwait;")] + "\n" + STUDIO


def _between(a, b, src=SRC):
    i = src.index(a)
    return src[i:src.index(b, i)]


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
    for mod in (models, gm, client_api, mobile_api, notify, ops, scheduler):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    # Nothing reaches Twilio, whatever a .env loaded elsewhere put in notify.
    monkeypatch.setattr(notify, "TWILIO_SID", "")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "")
    init_auth(db_path=db_path)
    gm.init_guest_marketing(db_path)
    ai_utils._ai_call_log.clear()
    auth_routes._login_attempts.clear()
    yield
    ai_utils._ai_call_log.clear()


class Clock:
    """The restaurant's wall clock for the 8am-9pm window (gm._sms_local_now)."""
    def __init__(self, hour=12, minute=0):
        self.t = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)

    def set(self, hour, minute=0, days=0):
        self.t = (self.t + timedelta(days=days)).replace(hour=hour, minute=minute)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(gm, "_sms_local_now", lambda rid: c.t)
    return c


@pytest.fixture
def texts(monkeypatch):
    sent = []

    def rec(phone, msg, **kw):
        sent.append({"phone": phone, "msg": msg, **kw})
        return True
    monkeypatch.setattr(gm, "send_sms", rec)
    return sent


@pytest.fixture
def no_thread(monkeypatch):
    """A deploy that kills the send thread before it runs: start_campaign's
    background drain never starts."""
    started = []

    class _Dead:
        def __init__(self, target=None, name=None, daemon=None):
            started.append(name)

        def start(self):
            pass
    monkeypatch.setattr(threading, "Thread", _Dead)
    return started


def _rid(db_path, **kw):
    kw.setdefault("name", "Mama Rosa's")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("module_marketing", 1)
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _guests(db_path, rid, n, start=0):
    for i in range(start, start + n):
        gm.add_guest_contact_public_optin(rid, f"55520000{i:02d}", name=f"G{i}", db_path=db_path)


def _one(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


def _campaigns(db_path, rid):
    return _one(db_path, "SELECT COUNT(*) FROM guest_campaigns WHERE restaurant_id=?", (rid,))[0]


# ── the app, and a login per role ───────────────────────────────────────────

@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _login(client, db_path, rid, role=None, name="owner"):
    uid = create_user(rid, name, f"{name}@x.test", "correct-horse-1", db_path=db_path)
    if role:
        set_user_role(uid, role, db_path=db_path)
    tok = client.post("/mobile/api/login", json={"username": name, "password": "correct-horse-1"}).get_json()["token"]
    return {"Authorization": f"Bearer {tok}"}


# ── CS-1 / MB-14: who may send ──────────────────────────────────────────────

def test_a_teammate_without_marketing_approve_cannot_send_any_guest_campaign(db_path, client, clock, texts):
    rid = _rid(db_path)
    _guests(db_path, rid, 3)
    h = _login(client, db_path, rid, role="member", name="teammate")
    c = get_conn(db_path)
    draft = c.execute("INSERT INTO guest_campaign_drafts (restaurant_id, kind, segment, segment_size, message, rec_key) "
                      "VALUES (?,?,?,?,?,?)", (rid, "winback", "all", 3, "Come back", "winback:all")).lastrowid
    c.commit()
    c.close()
    for path, body in (("/mobile/api/guest-campaign/send", {"message": "Pasta night"}),
                       (f"/mobile/api/guest-winback/{draft}/send", {}),
                       ("/mobile/api/guest-newsletter", {"body": "Hi", "subject": "S"}),
                       ("/mobile/api/guest-campaign/1/cancel", {})):
        r = client.post(path, json=body, headers=h)
        assert r.status_code == 403, path
        assert r.get_json()["error"] == "Ask the main account to approve this before it goes out."
    assert texts == [] and _campaigns(db_path, rid) == 0
    # Reading is not sending: the list and the newsletter count stay open.
    assert client.get("/mobile/api/guest-newsletter", headers=h).status_code == 200


def test_the_account_owner_can_send(db_path, client, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 2)
    h = _login(client, db_path, rid)
    r = client.post("/mobile/api/guest-campaign/send", json={"message": "Pasta night"}, headers=h)
    assert r.status_code == 202 and r.get_json()["total"] == 2


def test_only_the_account_owner_sets_the_mailing_address(db_path, client, monkeypatch):
    import guest_email
    rid = _rid(db_path)
    update_restaurant(rid, {"mailing_address": "1 Main St, Springfield"}, db_path=db_path)
    got = []
    monkeypatch.setattr(guest_email, "send_newsletter", lambda r, body, **kw: got.append(kw) or {"ok": True})
    mgr = _login(client, db_path, rid, role="manager", name="manager")
    r = client.post("/mobile/api/guest-newsletter", json={"body": "Hi", "subject": "S",
                                                         "mailing_address": "99 Elsewhere Rd"}, headers=mgr)
    assert r.status_code == 403 and r.get_json()["owner_only"] is True and got == []
    # The address already on file is not "setting" it.
    r = client.post("/mobile/api/guest-newsletter", json={"body": "Hi", "subject": "S",
                                                         "mailing_address": "1 Main St,  Springfield"}, headers=mgr)
    assert r.status_code == 200 and len(got) == 1
    ai_utils._ai_call_log.clear()
    owner = _login(client, db_path, rid, name="owner")
    r = client.post("/mobile/api/guest-newsletter", json={"body": "Hi", "subject": "S",
                                                         "mailing_address": "99 Elsewhere Rd"}, headers=owner)
    assert r.status_code == 200 and got[-1]["mailing_address"] == "99 Elsewhere Rd"


def test_every_send_route_checks_the_permission_on_both_twins():
    mob = open("mobile_api.py", encoding="utf-8").read()
    for fn in ("def mobile_guest_campaign_send(", "def mobile_guest_winback_send(", "def mobile_guest_newsletter(",
               "def mobile_guest_campaign_cancel("):
        body = mob[mob.index(fn):]
        body = body[:body.index("\n@mobile_bp.route")]
        assert "may_publish(current_user)" in body, fn
    web = open("client_api.py", encoding="utf-8").read()
    for path, fn in (("/api/guest-campaign/send", "mobile_guest_campaign_send"),
                     ("/api/guest-winback/<int:draft_id>/send", "mobile_guest_winback_send"),
                     ("/api/guest-newsletter", "mobile_guest_newsletter"),
                     ("/api/guest-campaign/<int:campaign_id>/cancel", "mobile_guest_campaign_cancel")):
        i = web.index(f'@client_bp.route("{path}"')
        assert f'_m("{fn}")' in web[i:i + 400], path


# ── CS-12 / MB-15: whose text it is ─────────────────────────────────────────

def test_every_campaign_text_opens_with_the_restaurants_name(db_path, clock, texts):
    rid = _rid(db_path, name="Mama Rosa’s")                  # a curly apostrophe in the name
    _guests(db_path, rid, 2)
    gm.send_campaign(rid, "Pasta night tonight.", db_path=db_path)
    assert texts and all(t["msg"] == "Mama Rosa's: Pasta night tonight.\n\nReply STOP to unsubscribe." for t in texts)
    # A message that already opens with the name keeps it once.
    assert gm.campaign_text("Mama Rosa’s", "Mama Rosa's here! Pasta.") == "Mama Rosa's here! Pasta."
    assert gm.campaign_text("Mama Rosa's", "Pasta.", "tok1234567").endswith("\n" + gm._short_link("tok1234567"))


def test_the_win_back_send_carries_the_name_once(db_path, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 6)
    c = get_conn(db_path)
    c.execute("UPDATE guest_contacts SET last_visit=? WHERE restaurant_id=?",
              ((datetime.now() - timedelta(days=40)).isoformat(), rid))
    c.commit()
    c.close()
    d = gm.winback_suggestion(rid, db_path=db_path)["draft"]
    assert d["message"].startswith("Mama Rosa's: ")
    out = gm.send_winback(rid, d["id"], db_path=db_path)
    assert out["ok"] and out["queued"]
    gm.run_campaign_sends(db_path=db_path)
    assert texts and all(t["msg"].startswith("Mama Rosa's: It's been") and t["msg"].count("Mama Rosa") == 1 for t in texts)


def test_the_help_reply_names_the_program_a_contact_and_how_to_stop(db_path, monkeypatch):
    import config
    monkeypatch.delenv("SUPPORT_EMAIL", raising=False)
    rid = _rid(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    reply = gm.handle_inbound_sms("+15551234567", "HELP", db_path=db_path)
    assert reply.startswith("Mama Rosa's: ") and "Cavnar AI" in reply
    assert f"Help: {config.will_email()}." in reply and "Msg frequency varies" in reply and "Reply STOP" in reply


def test_the_stop_reply_promises_only_what_the_system_does(db_path):
    rid = _rid(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    reply = gm.handle_inbound_sms("+15551234567", "STOP", db_path=db_path)
    assert reply == gm.STOP_REPLY
    assert "every restaurant" in reply and "this number" in reply and "START" in reply
    assert "any more texts from us" not in reply


def test_an_alert_contact_who_texted_stop_is_not_texted_an_alert(db_path, monkeypatch):
    rid = _rid(db_path)
    cid = notify.add_alert_contact(rid, "Ana GM", "(555) 123-4567", sms_consent=True, db_path=db_path)
    notify.add_alert_contact(rid, "Ben", "555-999-0000", sms_consent=True, db_path=db_path)
    assert len(notify.get_alert_contacts(rid, sms_consent_only=True, db_path=db_path)) == 2
    gm.handle_inbound_sms("+15551234567", "Stop", db_path=db_path)
    sendable = notify.get_alert_contacts(rid, sms_consent_only=True, db_path=db_path)
    assert [c["name"] for c in sendable] == ["Ben"]
    # The owner still sees the contact; only texting stops.
    assert cid in [c["id"] for c in notify.get_alert_contacts(rid, db_path=db_path)]
    sent = []
    monkeypatch.setattr(notify, "send_sms", lambda phone, msg, *a, **k: sent.append(phone) or True)
    notify.send_test_sms(rid)
    assert sent == ["555-999-0000"]
    # Their own START lifts it (they are on no guest list, so nothing else would).
    assert gm.handle_inbound_sms("+15551234567", "START", db_path=db_path) == gm.START_REPLY
    assert len(notify.get_alert_contacts(rid, sms_consent_only=True, db_path=db_path)) == 2
    assert gm.handle_inbound_sms("+15557777777", "START", db_path=db_path) is None     # a stranger: silence


def test_the_phone_preview_shows_a_number_not_the_restaurant():
    phone = _between('<div class="cp-phone"', '<label for="guest-campaign-message"')
    top = _between('<div class="top">', '<div class="thread">', phone)
    assert "restaurant.name" not in top and "|upper" not in top            # no name, no initials avatar
    assert 'id="cp-bub-from"' in top and 'class="av num"' in top
    assert 'data-rname="{{ restaurant.name }}"' in phone                   # the QR print still has the name
    qr = _between("window.cpPrintQr = function", "window.cpShareJoin")
    assert "getAttribute('data-rname')" in qr and ".cp-phone .top b" not in qr
    ov = _between("function loadGuestOverview", "function cpPaintGrowth")
    assert "d.sms.sender" in ov


def test_the_sender_number_is_formatted_or_unknown(monkeypatch):
    monkeypatch.setenv("TWILIO_GUEST_FROM_NUMBER", "+16305550123")
    assert notify.guest_sender_display() == "(630) 555-0123"
    monkeypatch.delenv("TWILIO_GUEST_FROM_NUMBER")
    monkeypatch.setattr(notify, "TWILIO_GUEST_MESSAGING_SERVICE_SID", "MGguest")
    assert notify.guest_sender_display() == ""


# ── MB-13 / #60: nothing lands after 9 PM ───────────────────────────────────

@pytest.mark.parametrize("hour,minute,expect", [(20, 40, 1200), (12, 0, 14400), (18, 30, 9000), (21, 0, 0), (7, 59, 0)])
def test_validity_is_the_seconds_to_nine_pm_at_most_four_hours(clock, hour, minute, expect):
    clock.set(hour, minute)
    assert gm.guest_sms_validity_seconds(1) == expect


def test_every_campaign_text_carries_its_validity_period(db_path, clock, texts):
    clock.set(20, 40)
    rid = _rid(db_path)
    _guests(db_path, rid, 2)
    gm.send_campaign(rid, "Last call", db_path=db_path)
    assert [t["validity_seconds"] for t in texts] == [1200, 1200]
    assert all(t["use_case"] == "guest" for t in texts)


def test_send_sms_hands_twilio_the_validity_period_clamped(monkeypatch):
    posted = []

    class _R:
        status_code = 201
        text = ""
    monkeypatch.setattr(notify, "TWILIO_SID", "AC1")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "t")
    monkeypatch.setattr(notify, "TWILIO_FROM", "+15550000000")
    monkeypatch.setattr(notify, "TWILIO_GUEST_MESSAGING_SERVICE_SID", "MGguest")
    monkeypatch.setattr(notify.requests, "post", lambda url, **kw: posted.append(kw["data"]) or _R())
    assert notify.send_sms("5551234567", "hi", use_case="guest", validity_seconds=1200)
    assert notify.send_sms("5551234567", "hi", use_case="guest", validity_seconds=99999)
    assert notify.send_sms("5551234567", "hi", use_case="guest")
    assert [p.get("ValidityPeriod") for p in posted] == ["1200", "14400", None]
    assert posted[0]["MessagingServiceSid"] == "MGguest"


def test_submissions_are_paced_when_they_reach_a_carrier(db_path, clock, texts, monkeypatch):
    assert gm._submit_interval() == 0.0                         # nothing to pace in tests / local runs
    monkeypatch.setattr(notify, "TWILIO_SID", "AC1")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "t")
    monkeypatch.delenv("GUEST_SMS_PER_SECOND", raising=False)
    assert gm._submit_interval() == pytest.approx(0.5)
    monkeypatch.setenv("GUEST_SMS_PER_SECOND", "4")
    assert gm._submit_interval() == pytest.approx(0.25)
    naps, now = [], [1000.0]

    def _sleep(s):
        # The clock moves only when the sender sleeps, so a slow runner can't
        # spend the interval for it and leave nothing to wait for (CI, 10/5/26).
        naps.append(s)
        now[0] += s

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", _sleep)
    rid = _rid(db_path)
    _guests(db_path, rid, 4)
    gm.send_campaign(rid, "Pasta night", db_path=db_path)
    assert len(texts) == 4 and naps == [pytest.approx(0.25)] * 3


# ── MB-10 / #10 / #11: refused before queueing; a durable queue ─────────────

def test_a_400_character_text_is_refused_before_anything_is_queued(db_path, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 3)
    out = gm.start_campaign(rid, "x" * 400, db_path=db_path)
    assert out["ok"] is False and out["blocked"] == "too_long" and "queued" not in out
    assert "Shorten it by" in out["error"] and _campaigns(db_path, rid) == 0 and no_thread == [] and texts == []


def test_the_limit_counts_the_name_and_the_link(db_path, clock, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 1)
    room = gm.CAMPAIGN_MAX_CHARS - len("Mama Rosa's: ")
    assert gm.start_campaign(rid, "y" * (room + 1), db_path=db_path)["blocked"] == "too_long"
    assert gm.start_campaign(rid, "y" * room, db_path=db_path)["ok"] is True
    with_link = room - gm.link_chars()
    out = gm.start_campaign(rid, "z" * (with_link + 1), link_token="abcdefghij", db_path=db_path)
    assert out["blocked"] == "too_long" and "the link" in out["error"]
    assert gm.message_budget("Mama Rosa's", with_link=True) == with_link


def test_the_route_refuses_before_minting_a_link(db_path, client, clock, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 1)
    h = _login(client, db_path, rid)
    msg = "w" * (gm.CAMPAIGN_MAX_CHARS - len("Mama Rosa's: ") - 10)      # fits alone, not with the link
    r = client.post("/mobile/api/guest-campaign/send", json={"message": msg, "link_url": "https://mamarosa.test/menu"},
                    headers=h)
    assert r.status_code == 400 and r.get_json()["blocked"] == "too_long"
    assert _one(db_path, "SELECT COUNT(*) FROM marketing_links WHERE restaurant_id=?", (rid,))[0] == 0
    assert _campaigns(db_path, rid) == 0


def test_a_campaign_whose_thread_died_is_finished_by_the_scheduler(db_path, clock, texts, no_thread):
    """The deploy case: every recipient was queued, the drain never ran."""
    rid = _rid(db_path)
    _guests(db_path, rid, 5)
    out = gm.start_campaign(rid, "Wine dinner Friday", db_path=db_path)
    assert out["ok"] and out["queued"] and out["total"] == 5 and no_thread
    st = gm.campaign_status(out["campaign_id"], db_path=db_path)
    assert (st["status"], st["total"], st["sent"], st["pending"]) == ("sending", 5, 0, 5)
    res = gm.run_campaign_sends(db_path=db_path)
    assert res["sent"] == 5 and len(texts) == 5
    st = gm.campaign_status(out["campaign_id"], db_path=db_path)
    assert (st["status"], st["sent"], st["failed"], st["pending"]) == ("done", 5, 0, 0)
    assert gm.run_campaign_sends(db_path=db_path)["sent"] == 0 and len(texts) == 5      # nobody twice


def test_texts_cut_off_at_nine_wait_for_eight_and_then_go(db_path, clock, monkeypatch):
    rid = _rid(db_path)
    _guests(db_path, rid, 5)
    clock.set(20, 58)
    at = []

    def rec(phone, msg, **kw):
        at.append((clock.t.hour, clock.t.minute))
        clock.t += timedelta(minutes=1)
        return True
    monkeypatch.setattr(gm, "send_sms", rec)
    out = gm.send_campaign(rid, "Last call", db_path=db_path)
    assert out["sent"] == 2 and out["deferred_quiet_hours"] == 3 and out["status"] == "waiting"
    hist = gm.campaign_history(rid, db_path=db_path)[0]
    assert (hist["status"], hist["pending"], hist["waiting_until"]) == ("waiting", 3, "8:00 AM")
    clock.set(23, 30)
    assert gm.run_campaign_sends(db_path=db_path)["sent"] == 0                 # still night
    clock.set(8, 0, days=1)
    assert gm.run_campaign_sends(db_path=db_path)["sent"] == 3
    assert all(h < 21 for h, _ in at) and len(at) == 5
    assert gm.campaign_status(out["campaign_id"], db_path=db_path)["status"] == "done"


def test_a_held_send_outside_the_window_is_queued_for_eight_not_refused(db_path, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 3)
    clock.set(22, 15)
    refused = gm.start_campaign(rid, "Brunch tomorrow", db_path=db_path)
    assert refused["blocked"] == "quiet_hours" and _campaigns(db_path, rid) == 0
    out = gm.start_campaign(rid, "Brunch tomorrow", hold=True, db_path=db_path)
    assert out["ok"] and out["waiting"] is True and out["waiting_until"] == "8:00 AM" and no_thread == []
    assert gm.run_campaign_sends(db_path=db_path)["sent"] == 0 and texts == []
    clock.set(8, 5, days=1)
    assert gm.run_campaign_sends(db_path=db_path)["sent"] == 3


def test_the_route_holds_for_eight_only_when_asked(db_path, client, clock, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 2)
    h = _login(client, db_path, rid)
    clock.set(23, 0)
    r = client.post("/mobile/api/guest-campaign/send", json={"message": "Brunch"}, headers=h)
    assert r.get_json()["blocked"] == "quiet_hours"                      # the phone's old contract
    r = client.post("/mobile/api/guest-campaign/send", json={"message": "Brunch", "hold": True}, headers=h)
    assert r.status_code == 202 and r.get_json()["waiting"] is True


def test_a_cancel_stops_the_pending_texts(db_path, client, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 4)
    clock.set(22, 0)
    cid = gm.start_campaign(rid, "Brunch", hold=True, db_path=db_path)["campaign_id"]
    h = _login(client, db_path, rid)
    r = client.post(f"/mobile/api/guest-campaign/{cid}/cancel", headers=h)
    assert r.status_code == 200 and r.get_json()["cancelled"] == 4 and r.get_json()["status"] == "cancelled"
    assert client.post(f"/mobile/api/guest-campaign/{cid}/cancel", headers=h).status_code == 409
    assert client.post("/mobile/api/guest-campaign/99999/cancel", headers=h).status_code == 404
    clock.set(9, 0, days=1)
    gm.run_campaign_sends(db_path=db_path)
    assert texts == []
    # Another restaurant's campaign is not this login's to cancel.
    other = _rid(db_path, name="Other Co", owner_email="x@y.test")
    _guests(db_path, other, 1, start=50)
    ocid = gm.start_campaign(other, "Hi", hold=True, db_path=db_path)["campaign_id"]
    assert client.post(f"/mobile/api/guest-campaign/{ocid}/cancel", headers=h).status_code == 404


def test_a_text_interrupted_mid_send_is_failed_not_sent_twice(db_path, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 2)
    cid = gm.start_campaign(rid, "Pasta", db_path=db_path)["campaign_id"]
    c = get_conn(db_path)
    c.execute("UPDATE guest_campaign_queue SET status='sending', claimed_at=datetime('now','-40 minutes') "
              "WHERE id=(SELECT MIN(id) FROM guest_campaign_queue WHERE campaign_id=?)", (cid,))
    c.commit()
    c.close()
    gm.run_campaign_sends(db_path=db_path)
    st = gm.campaign_status(cid, db_path=db_path)
    assert (st["sent"], st["failed"], st["status"]) == (1, 1, "done") and len(texts) == 1


def test_a_pending_text_expires_rather_than_arriving_a_day_late(db_path, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 2)
    clock.set(22, 0)
    cid = gm.start_campaign(rid, "Tonight only", hold=True, db_path=db_path)["campaign_id"]
    c = get_conn(db_path)
    c.execute("UPDATE guest_campaigns SET created_at=datetime('now','-30 hours') WHERE id=?", (cid,))
    c.commit()
    c.close()
    clock.set(9, 0, days=1)
    gm.run_campaign_sends(db_path=db_path)
    assert texts == [] and gm.campaign_status(cid, db_path=db_path)["status"] == "done"
    assert _one(db_path, "SELECT COUNT(*) FROM guest_campaign_queue WHERE campaign_id=? AND status='expired'",
                (cid,))[0] == 2


def test_a_stop_that_arrives_mid_campaign_stops_that_campaign(db_path, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 3)
    cid = gm.start_campaign(rid, "Pasta", db_path=db_path)["campaign_id"]
    gm.handle_inbound_sms("+15552000001", "STOP", db_path=db_path)
    gm.run_campaign_sends(db_path=db_path)
    assert "+15552000001" not in [t["phone"] for t in texts] and len(texts) == 2
    assert gm.campaign_status(cid, db_path=db_path)["skipped_recent"] == 1


def test_the_drain_is_a_minute_duty():
    duties = _between("def _minute_duties():", "def _pulse_interval():", open("scheduler.py", encoding="utf-8").read())
    assert "run_campaign_sends()" in duties and 'job="guest_campaign_sends"' in duties


# ── MB-11 / CS-5 / #61: nobody eligible, nothing sent ───────────────────────

def test_nobody_eligible_writes_no_campaign_and_starts_no_tracker(db_path, clock, texts):
    import outcomes
    rid = _rid(db_path)
    _guests(db_path, rid, 2)
    gm.send_campaign(rid, "First", db_path=db_path)
    assert len(texts) == 2
    out = gm.start_campaign(rid, "Fill Tuesday!", target_day="Tuesday", db_path=db_path)
    assert out["ok"] is False and out["blocked"] == "no_audience" and out["skipped_recent"] == 2
    assert "texted in the last 3 days" in out["error"]
    assert _campaigns(db_path, rid) == 1 and outcomes.list_outcomes(rid) == []


def test_a_campaign_that_sent_nothing_starts_no_tracker(db_path, clock, monkeypatch, no_thread):
    import outcomes
    rid = _rid(db_path)
    _guests(db_path, rid, 2)
    monkeypatch.setattr(gm, "send_sms", lambda *a, **k: False)            # every text refused by the carrier
    cid = gm.start_campaign(rid, "Fill Tuesday!", target_day="Tuesday", db_path=db_path)["campaign_id"]
    gm.run_campaign_sends(db_path=db_path)
    st = gm.campaign_status(cid, db_path=db_path)
    assert (st["sent"], st["failed"], st["status"]) == (0, 2, "done")
    assert outcomes.list_outcomes(rid) == []


def test_a_fill_a_night_campaign_that_sent_starts_its_tracker_once(db_path, clock, texts, no_thread):
    import outcomes
    rid = _rid(db_path)
    _guests(db_path, rid, 3)
    gm.start_campaign(rid, "Fill Tuesday!", target_day="tuesday", user_id=7, db_path=db_path)
    assert outcomes.list_outcomes(rid) == []                               # nothing sent yet
    gm.run_campaign_sends(db_path=db_path)
    gm.run_campaign_sends(db_path=db_path)
    rows = outcomes.list_outcomes(rid)
    assert len(rows) == 1 and rows[0]["metric"] == "weekday_sales:Tuesday"
    # A bogus weekday is not stored, so nothing is tracked for it.
    assert gm.track_campaign_outcome(rid, "Funday", {"ok": True, "sent": 3}) is None


def test_text_n_is_the_eligible_count(db_path, client, clock):
    rid = _rid(db_path)
    _guests(db_path, rid, 3)
    c = get_conn(db_path)
    c.execute("UPDATE guest_contacts SET last_campaign_at=? WHERE phone='+15552000000'",
              ((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),))
    c.commit()
    c.close()
    assert gm.segment_counts(rid, db_path=db_path)["all"] == 3
    assert gm.eligible_counts(rid, db_path=db_path)["all"] == 2 == gm.audience_size(rid, "all", db_path=db_path)
    h = _login(client, db_path, rid)
    segs = {s["key"]: s for s in client.get("/mobile/api/guest-segments", headers=h).get_json()["segments"]}
    assert (segs["all"]["count"], segs["all"]["eligible"]) == (3, 2)
    paint = _between("window.cpPaint = function", "function cpPaintAi")
    assert "n = seg.eligible != null ? +seg.eligible : nAll" in paint


# ── CS-13 / #35: failures shown, "accepted" not "delivered" ─────────────────

def test_failures_are_on_the_campaign_and_accepted_is_not_called_delivered(db_path, clock, monkeypatch):
    rid = _rid(db_path)
    _guests(db_path, rid, 3)
    n = {"i": 0}

    def flaky(phone, msg, **kw):
        n["i"] += 1
        return n["i"] != 2
    monkeypatch.setattr(gm, "send_sms", flaky)
    gm.send_campaign(rid, "Pasta", db_path=db_path)
    hist = gm.campaign_history(rid, db_path=db_path)[0]
    assert (hist["sent_count"], hist["failed_count"], hist["status"]) == (2, 1, "done")
    ov = gm.campaign_overview(rid, db_path=db_path)
    assert ov["accepted"] == 66.7 and ov["texts_failed_this_month"] == 1 and "delivered" not in ov
    perf = _between("function cpPaintPerfSum()", "function loadGuestContactsSummary")
    assert "accepted by carrier" in perf and "texts delivered" not in perf and "texts failed" in perf
    card = _between("    if (it.kind === 'text') {", "    } else {")
    assert "bar(sent + failedN, 'Failed', failedN, 'var(--red)', '0')" in card
    assert "data-cp-cancel=" in card and "' wait until '" in card
    send = _between("window.sendGuestCampaign = function(btn) {", "// A changed draft or audience is a new send")
    assert "Performance shows any that fail" in send


# ── CS-17: an unknown audience ──────────────────────────────────────────────

def test_an_unknown_audience_is_a_400_never_everyone(db_path, client, clock, texts, no_thread):
    rid = _rid(db_path)
    _guests(db_path, rid, 2)
    h = _login(client, db_path, rid)
    r = client.post("/mobile/api/guest-campaign/send", json={"message": "Hi", "segment": "vip_lapsed"}, headers=h)
    assert r.status_code == 400 and r.get_json()["blocked"] == "unknown_segment"
    assert _campaigns(db_path, rid) == 0
    assert gm.known_segment(None) == "all" and gm.known_segment(" Regulars ") == "regulars"
    assert gm.known_segment("nope") is None and gm.known_segment(5) is None
    assert gm.filter_segment(rid, [{"id": 1}], "nope") == []


# ── The Studio (CS-4 / CS-16 / CS-14 / CS-6 / CS-22 / CS-23, CS-9 reuse) ────

def test_the_studio_source_keeps_its_invariants():
    assert "sending: false, snap: null, seq: {text: 0, email: 0, social: 0}, sent: {}" in STUDIO
    draft = _between("function cpDraft(k, prompt, plan) {", "window.cpRewrite = function(k) {")
    assert "var my = ++_cp.seq[k];" in draft and draft.count("if (my !== _cp.seq[k]) return;") == 2
    # The tag is Cavnar AI's only once a plan actually arrived.
    assert "tag.textContent = 'Picked by Cavnar AI'" in draft
    create = _between("window.cpCreate = function(rewrite) {", "function cpCreateIdle()")
    assert "if (cpBusy() || _cp.sending) return;" in create and "cpResetDraft();" in create
    assert "'Picked by Cavnar AI'" not in create
    for entry in ("function cpUseWinback() {", "function cpReuse(kind, id, improve) {", "window.cpUseEmailText = function(text) {"):
        body = _between(entry, "\n}")
        assert "cpResetDraft();" in body and "_cp.sending" in body, entry
    reuse = _between("function cpReuse(kind, id, improve) {", "\n}")
    assert "_cp.type = 'general'; _cp.targetDay = null;" in reuse
    reset = _between("function cpResetDraft() {", "\n}")
    for part in ("_cp.targetDay = null", "_cp.photo = null", "'cp-em-headline'", "'cp-em-btn'", "'guest-campaign-link'",
                 "tag.textContent = ''", "_cp.seq[k]++"):
        assert part in reset, part
    assert "if (cpBusy() || _cp.sending) return;" in _between("if (t.hasAttribute('data-cp-idea'))", "\n")
    assert 'id="guest-campaign-status" role="status" aria-live="polite"' in SRC
    esc = _between("function _escHtml(s) {", "\n}")
    assert ".replace(/\"/g,'&quot;').replace(/'/g,'&#39;')" in esc


# ── the Studio, run: a small DOM in node ────────────────────────────────────

_HARNESS = r"""
var listeners = {}, fetches = [], pending = [];
function El(id) {
  var cl = {}; this.id = id; this.value = ''; this.textContent = ''; this.innerHTML = ''; this.hidden = false;
  this.disabled = false; this.checked = false; this.maxLength = 300; this.style = {}; this.attrs = {}; this.className = '';
  this.classList = {add: function(c) { cl[c] = 1; }, remove: function(c) { delete cl[c]; },
    contains: function(c) { return !!cl[c]; }, toggle: function(c, on) { if (on === undefined) on = !cl[c]; if (on) cl[c] = 1; else delete cl[c]; return on; }};
}
El.prototype.setAttribute = function(k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function(k) { return this.attrs.hasOwnProperty(k) ? this.attrs[k] : null; };
El.prototype.hasAttribute = function(k) { return this.attrs.hasOwnProperty(k); };
El.prototype.removeAttribute = function(k) { delete this.attrs[k]; };
El.prototype.contains = function() { return true; };
El.prototype.closest = function() { return this; };
El.prototype.focus = El.prototype.select = El.prototype.scrollIntoView = function() {};
var els = {};
var document = {getElementById: function(id) { return els[id] || (els[id] = new El(id)); },
  addEventListener: function(t, f) { (listeners[t] = listeners[t] || []).push(f); },
  querySelectorAll: function() { return []; }, querySelector: function() { return null; }};
var window = globalThis;
function setTimeout() { return 1; } function clearTimeout() {}
function fetch(url, opts) {
  var rec = {url: url, body: opts && opts.body ? JSON.parse(opts.body) : null};
  fetches.push(rec);
  return new Promise(function(res) { rec.resolve = res; pending.push(rec); });
}
function apiJson(r) { return r; }
function mdy(s) { return s; } function toast() {} function loadMktOpps() {} function recEsc(s) { return s; }
function recReasonPicker() {} function _mktShrinkPhoto() {} function loadMarketingDiagnosis() {} function loadFailed() {}
// Typing in a field: its inline oninput="cpPaint()" runs first, then the
// document's input listener (bubbling), as in the page.
function typed(id) { cpPaint(); listeners.input[0]({target: document.getElementById(id)}); }
""" + "\n"


def _node(script):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    esc = _between("function _escHtml(s) {", "\n}\n") + "\n}\n"
    js = _HARNESS + esc + STUDIO + "\n(async function(){\nvar tick = function() { return new Promise(function(r) { setImmediate(r); }); };\n" + script + "\n})().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });"
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


_SETUP = """
_cp.ov = {sending_now: true, window: '8:00 AM and 9:00 PM', min_days_between: 3, mailing_address_set: true,
          sms: {prefix: "Mama Rosa's: ", link_chars: 41, stop_chars: 28, max: 300, link_example: 'https://dashboard.cavnar.ai/g/…'},
          back_by_segment: {}, tap_by_segment: {}};
_cp.segs = [{key: 'all', label: 'Everyone consented', count: 5, eligible: 3, email_count: 0},
            {key: 'regulars', label: 'Regulars', count: 2, eligible: 2, email_count: 0}];
_cp.chan = {text: true, email: false, social: false}; _cp.chanSet = true;
document.getElementById('cp-builder').hidden = false;
document.getElementById('guest-campaign-message').value = 'Pasta night';
var btn = document.getElementById('guest-send-btn');
cpPaint();
"""


def test_the_armed_snapshot_is_what_goes_and_nothing_re_arms_mid_send():
    got = _node(_SETUP + r"""
var r = {idle: btn.textContent};
sendGuestCampaign(btn);                                   // arm
r.armed = btn.textContent;
document.getElementById('guest-campaign-message').value = 'EDITED WITHOUT AN EVENT';
sendGuestCampaign(btn);                                   // send what was armed
r.sent_body = fetches[0].body; r.sending = _cp.sending; r.label_mid = btn.textContent;
cpPickSeg('regulars'); cpPaint();                         // mid-send: nothing re-labels or re-arms
r.label_after_paint = btn.textContent; r.seg_mid = _cp.seg; r.disabled_mid = btn.disabled;
pending[0].resolve({ok: true, queued: true, total: 3}); await tick(); await tick();
r.sending_after = _cp.sending; r.sent_class = btn.classList.contains('sent');
r.status = document.getElementById('guest-campaign-status').innerHTML;
console.log(JSON.stringify(r));
""")
    assert got["idle"] == "Text 3 →"                                  # the eligible 3, not the 5 on the list
    assert got["armed"] == "Tap again to text 3 guests"
    assert got["sent_body"]["message"] == "Pasta night" and got["sent_body"]["segment"] == "all"
    assert got["sent_body"]["hold"] is False
    assert got["sending"] is True and got["label_mid"] == got["label_after_paint"] == "Sending…"
    assert got["seg_mid"] == "all" and got["disabled_mid"] is True
    assert got["sending_after"] is False and got["sent_class"] is True
    assert "Texting 3 guests" in got["status"] and "Performance shows any that fail" in got["status"]


def test_an_edit_after_arming_disarms_and_only_a_changed_channel_is_offered_again():
    got = _node(_SETUP + r"""
var r = {};
sendGuestCampaign(btn);                                   // arm
typed('guest-campaign-message');
r.after_edit = btn.textContent; r.snap = _cp.snap;
sendGuestCampaign(btn);                                   // arms again, sends nothing
r.fetches_after_rearm = fetches.length;
sendGuestCampaign(btn); pending[0].resolve({ok: true, queued: true, total: 3}); await tick(); await tick();
// The same text again is not offered; the button stays "Sent" until a change.
typed('guest-campaign-message');
r.unchanged = btn.textContent; r.unchanged_disabled = btn.disabled;
r.checks_unchanged = document.getElementById('cp-checks').innerHTML;
document.getElementById('guest-campaign-message').value = 'Pasta night, fixed';
typed('guest-campaign-message');
r.changed = btn.textContent; r.checks_changed = document.getElementById('cp-checks').innerHTML;
console.log(JSON.stringify(r));
""")
    assert got["after_edit"] == "Text 3 →" and got["snap"] is None
    assert got["fetches_after_rearm"] == 0
    assert got["unchanged"] == "Send" and got["unchanged_disabled"] is True
    assert "This text is on its way" in got["checks_unchanged"]
    assert got["changed"] == "Text 3 →" and "You texted this audience 1 minute ago" in got["checks_changed"]


def test_a_late_draft_never_overwrites_a_newer_one_and_ideas_wait():
    got = _node(_SETUP + r"""
var r = {};
_cp.prompt = 'Fill Tuesday';
cpDraft('text', 'Fill Tuesday', false); cpDraft('text', 'Fill Tuesday', false);
pending[1].resolve({ok: true, message: 'NEWER'}); await tick();
pending[0].resolve({ok: true, message: 'OLDER'}); await tick();
r.message = document.getElementById('guest-campaign-message').value; r.busy = !!_cp.busy.text;
_cp.busy.email = true;
var chip = new El('chip'); chip.setAttribute('data-cp-idea', 'Thank our regulars');
var before = fetches.length; listeners.click[0]({target: chip}); r.idea_fetches = fetches.length - before;
console.log(JSON.stringify(r));
""")
    assert got["message"] == "NEWER" and got["busy"] is False
    assert got["idea_fetches"] == 0


def test_use_again_starts_clean_and_the_plan_alone_tags_the_audience():
    got = _node(_SETUP + r"""
var r = {};
_cp.type = 'event'; _cp.targetDay = 'Tuesday'; _cp.photo = {id: 4, url: 'x'};
document.getElementById('cp-em-headline').value = 'Old headline';
_cp.hist = [{id: 9, message: 'Trivia tonight', segment: 'regulars'}];
cpReuse('text', 9, false);
r.type = _cp.type; r.day = _cp.targetDay; r.photo = _cp.photo; r.headline = document.getElementById('cp-em-headline').value;
r.msg = document.getElementById('guest-campaign-message').value; r.seg = _cp.seg;
document.getElementById('cp-prompt').value = 'Fill Thursday dinner';
cpCreate();
r.tag_before = document.getElementById('cp-aud-k').textContent; r.seg_before = _cp.seg;
var p = pending[pending.length - 1];
p.resolve({ok: true, message: 'See you Thursday', type: 'event', segment: 'all', goal: 'Fill Thursday', target_day: 'Thursday'});
await tick();
r.tag_after = document.getElementById('cp-aud-k').textContent; r.day_after = _cp.targetDay;
console.log(JSON.stringify(r));
""")
    assert (got["type"], got["day"], got["photo"], got["headline"]) == ("general", None, None, "")
    assert got["msg"] == "Trivia tonight" and got["seg"] == "regulars"
    assert got["tag_before"] == "" and got["seg_before"] == "all"
    assert got["tag_after"] == "Picked by Cavnar AI" and got["day_after"] == "Thursday"


def test_the_counter_counts_the_name_and_the_real_link():
    got = _node(_SETUP + r"""
var r = {};
document.getElementById('guest-campaign-message').value = new Array(251).join('x');     // 250
document.getElementById('guest-campaign-link').value = 'https://mamarosa.test/menu';
cpPaint();
r.chars = document.getElementById('cp-chars').textContent; r.btn = btn.textContent;
r.checks = document.getElementById('cp-checks').innerHTML; r.max = document.getElementById('guest-campaign-message').maxLength;
r.bubble = document.getElementById('cp-bub-t').textContent.slice(0, 14);
r.link = document.getElementById('cp-bub-link').textContent;
console.log(JSON.stringify(r));
""")
    assert got["chars"] == "304 / 300"                                   # 13 + 250 + 41
    assert got["btn"] == "Send" and "Shorten the text by 4 to send it" in got["checks"]
    assert got["max"] == 300 - 13 - 41
    assert got["bubble"] == "Mama Rosa's: x" and got["link"].startswith("https://dashboard.cavnar.ai/g/")


def test_outside_the_window_the_text_is_offered_for_eight_and_held():
    got = _node(_SETUP + r"""
_cp.ov.sending_now = false; cpPaint();
var r = {idle: btn.textContent, checks: document.getElementById('cp-checks').innerHTML};
sendGuestCampaign(btn); sendGuestCampaign(btn);
r.body = fetches[0].body;
pending[0].resolve({ok: true, queued: true, waiting: true, waiting_until: '8:00 AM', total: 3}); await tick(); await tick();
r.status = document.getElementById('guest-campaign-status').innerHTML;
console.log(JSON.stringify(r));
""")
    assert got["idle"] == "Text 3 at 8:00 AM →" and "Texts wait until 8:00 AM" in got["checks"]
    assert got["body"]["hold"] is True
    assert "3 texts wait until 8:00 AM, then go" in got["status"]


def test_esc_html_escapes_quotes():
    got = _node("console.log(JSON.stringify(_escHtml('a\"b\\'c<d>&')));")
    assert got == "a&quot;b&#39;c&lt;d&gt;&amp;"
