"""Edge cases for guest SMS marketing: STOP handling, the public opt-in,
campaigns, and the two automated guest jobs (Toast opt-in invites and
post-visit review requests).

Every test comes from the MOD edge-case audit (appendix A6 "Guest marketing
(SMS)", findings MOD-MKT-6..12). What they protect is the TCPA line the
module is built around: consent comes from the guest, a revocation is
honoured however it is phrased and wherever it was sent, and no guest is
texted twice (or after 9pm) because of how the send loop happens to be
written.

send_sms is always a recorder here — nothing reaches Twilio. xfail(strict=True)
tests assert the CORRECT behaviour for a confirmed defect and flip to a
failure the day it is fixed.
"""
import sqlite3
import sys
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import client_api
import guest_links
import guest_marketing as gm
import mobile_api
import models
import notify
import ops
import scheduler
import webhook_routes
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_conn, update_restaurant


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
    for mod in (models, gm, client_api, mobile_api, webhook_routes, notify, ops, scheduler):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    gm.init_guest_marketing(db_path)


@pytest.fixture(autouse=True)
def _noon(monkeypatch):
    """Inside the 8am-9pm guest window unless a test says otherwise."""
    monkeypatch.setattr(gm, "_sms_local_now", lambda rid: datetime.now().replace(hour=12, minute=0))


@pytest.fixture
def texts(monkeypatch):
    sent = []

    def rec(phone, msg, **kw):
        sent.append({"phone": phone, "msg": msg, **kw})
        return True
    monkeypatch.setattr(gm, "send_sms", rec)
    return sent


def _rid(db_path, **kw):
    kw.setdefault("name", "Guest Co")
    kw.setdefault("owner_email", "g@x.test")
    kw.setdefault("module_marketing", 1)
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _unsubscribed(db_path, phone):
    conn = get_conn(db_path)
    try:
        return [r["unsubscribed"] for r in conn.execute(
            "SELECT unsubscribed FROM guest_contacts WHERE phone=?", (phone,))]
    finally:
        conn.close()


# ── STOP, however it is phrased ─────────────────────────────────────────────

@pytest.mark.parametrize("body", ["Stop.", "STOP!", "Please stop", "opt out", "Opt-out", "remove me",
                                  "Unsubscribe."])
@pytest.mark.xfail(strict=True, reason="MOD-MKT-8: punctuated or phrase-form STOP revocations leave the guest subscribed")
def test_a_punctuated_or_phrased_stop_unsubscribes(db_path, body):
    """A6 SMS #12 / MOD-MKT-8 — the FCC's 'reasonable means' revocation."""
    rid = _rid(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    reply = gm.handle_inbound_sms("+15551234567", body, db_path=db_path)
    assert _unsubscribed(db_path, "+15551234567") == [1]
    assert reply and "unsubscribed" in reply.lower()


@pytest.mark.parametrize("body", ["\u200bSTOP", "\uff33\uff34\uff2f\uff30", "STOP\u200d"])
@pytest.mark.xfail(strict=True, reason="MOD-MKT-8: zero-width and full-width STOP are not normalised, so the guest stays subscribed")
def test_a_unicode_disguised_stop_unsubscribes(db_path, body):
    """A6 SMS #13 / MOD-MKT-8 — NFKC and zero-width characters."""
    rid = _rid(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    gm.handle_inbound_sms("+15551234567", body, db_path=db_path)
    assert _unsubscribed(db_path, "+15551234567") == [1]


@pytest.mark.parametrize("body", ["stop texting me", "stop please.", "\u00a0stop", "  STOP  ", "Unsubscribe me"])
def test_a_stop_that_leads_the_message_still_works(db_path, body):
    """A6 SMS #12/#13 — the forms that work today keep working: STOP as the
    first word (trailing words and punctuation after it are fine), and
    ordinary leading whitespace, including a no-break space."""
    rid = _rid(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    gm.handle_inbound_sms("+15551234567", body, db_path=db_path)
    assert _unsubscribed(db_path, "+15551234567") == [1]


@pytest.mark.xfail(strict=True, reason="MOD-A6-optin-18: the HELP reply names no program or restaurant")
def test_the_help_reply_names_the_restaurant_it_is_for(db_path):
    """A6 SMS #18 — a HELP reply has to identify the program (the carrier
    requirement for a HELP response is program name plus how to stop)."""
    rid = _rid(db_path, name="Mama Rosa's")
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    reply = gm.handle_inbound_sms("+15551234567", "HELP", db_path=db_path)
    assert reply and "Mama Rosa" in reply and "STOP" in reply


# ── The public opt-in ───────────────────────────────────────────────────────

@pytest.fixture
def web(db_path):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _optin(web, token, phone, xff=None, name="Guest"):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return web.post(f"/api/public/guest-optin/{token}", json={"name": name, "phone": phone, "consent": True},
                    headers=headers)


@pytest.mark.xfail(strict=True, reason="MOD-MKT-9: anyone submitting the public join form re-subscribes a guest who texted STOP")
def test_the_public_form_never_re_subscribes_a_guest_who_texted_stop(db_path, web):
    """A6 SMS #14 / MOD-MKT-9 — only an inbound START from that phone may
    undo a STOP."""
    rid = _rid(db_path)
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    gm.handle_inbound_sms("+15551234567", "STOP", db_path=db_path)
    assert _optin(web, guest_links.sign_join(rid), "555-123-4567", name="Someone Else").status_code == 200
    assert _unsubscribed(db_path, "+15551234567") == [1]


@pytest.mark.xfail(strict=True, reason="MOD-MKT-9: a bare restaurant id is still accepted as a join token (ALLOW_LEGACY_JOIN_LINKS defaults on)")
def test_a_bare_restaurant_id_is_not_a_join_token(db_path, web, monkeypatch):
    """A6 SMS #15 / MOD-MKT-9 — /api/public/guest-optin/1, /2, ... reaches
    every marketing restaurant with no printed link at all. (tests/
    test_security.py pins the legacy acceptance; both change together.)"""
    monkeypatch.delenv("ALLOW_LEGACY_JOIN_LINKS", raising=False)
    rid = _rid(db_path)
    assert _optin(web, str(rid), "5551234567").status_code == 404


def test_a_signed_join_token_is_accepted(db_path, web):
    """A6 SMS #15 — the printed, signed form keeps working."""
    rid = _rid(db_path)
    assert _optin(web, guest_links.sign_join(rid), "5551234567").status_code == 200


@pytest.mark.xfail(strict=True, reason="MOD-MKT-9: the opt-in rate limit keys on the client-supplied left-most X-Forwarded-For")
def test_a_spoofed_forwarded_for_header_does_not_bypass_the_opt_in_limit(db_path, web):
    """A6 SMS #16 / MOD-MKT-9 — one client, a fresh fake XFF each time."""
    rid = _rid(db_path)
    token = guest_links.sign_join(rid)
    codes = [_optin(web, token, f"55512300{i:02d}", xff=f"10.0.0.{i}").status_code for i in range(8)]
    assert 429 in codes, codes


def test_a_seven_digit_number_is_refused(db_path, web):
    """A6 SMS #17."""
    rid = _rid(db_path)
    assert _optin(web, guest_links.sign_join(rid), "555-0123").status_code == 400


def test_an_international_number_without_a_plus_keeps_its_country_code(db_path, web):
    """A6 SMS #17 — 44 20 7946 0958 is a London number."""
    rid = _rid(db_path)
    assert _optin(web, guest_links.sign_join(rid), "44 20 7946 0958").status_code == 200
    assert [c["phone"] for c in gm.get_guest_contacts(rid, db_path=db_path)] == ["+442079460958"]


@pytest.mark.xfail(strict=True, reason="MOD-A6-optin-17: a US number typed with an extension becomes a different (foreign) number")
def test_a_us_number_with_an_extension_is_stored_as_that_us_number_or_refused(db_path, web):
    """A6 SMS #17 — '(630) 555-0123 x45' must not become +630555012345."""
    rid = _rid(db_path)
    resp = _optin(web, guest_links.sign_join(rid), "(630) 555-0123 x45")
    phones = [c["phone"] for c in gm.get_guest_contacts(rid, db_path=db_path)]
    assert resp.status_code == 400 or phones == ["+16305550123"], (resp.status_code, phones)


# ── Campaigns ───────────────────────────────────────────────────────────────

def _consented(db_path, rid, n):
    for i in range(n):
        gm.add_guest_contact_public_optin(rid, f"55520000{i:02d}", name=f"G{i}", db_path=db_path)


def _per_phone(texts):
    counts = {}
    for t in texts:
        counts[t["phone"]] = counts.get(t["phone"], 0) + 1
    return counts


def test_two_overlapping_campaign_sends_text_each_guest_once(db_path, monkeypatch):
    """A6 Campaigns #9 / MOD-MKT-6 — a double-tap: the second send starts
    while the first is still in its loop."""
    rid = _rid(db_path)
    _consented(db_path, rid, 10)
    sent = []
    state = {"nested": False}

    def rec(phone, msg, **kw):
        sent.append({"phone": phone})
        if not state["nested"]:
            state["nested"] = True
            gm.send_campaign(rid, "Half-price pasta tonight", db_path=db_path)
        return True
    monkeypatch.setattr(gm, "send_sms", rec)
    gm.send_campaign(rid, "Half-price pasta tonight", db_path=db_path)
    assert max(_per_phone(sent).values()) == 1, _per_phone(sent)


class _Killed(BaseException):
    """A deploy/restart mid-loop — not an Exception, so nothing swallows it."""


def test_a_campaign_interrupted_mid_send_does_not_re_text_on_retry(db_path, monkeypatch):
    """A6 Campaigns #10 / MOD-MKT-6."""
    rid = _rid(db_path)
    _consented(db_path, rid, 10)
    sent = []

    def dies_after_four(phone, msg, **kw):
        if len(sent) == 4:
            raise _Killed()
        sent.append({"phone": phone})
        return True
    monkeypatch.setattr(gm, "send_sms", dies_after_four)
    with pytest.raises(_Killed):
        gm.send_campaign(rid, "Half-price pasta tonight", db_path=db_path)
    monkeypatch.setattr(gm, "send_sms", lambda phone, msg, **kw: sent.append({"phone": phone}) or True)
    gm.send_campaign(rid, "Half-price pasta tonight", db_path=db_path)
    counts = _per_phone(sent)
    assert len(counts) == 10 and max(counts.values()) == 1, counts


@pytest.mark.xfail(strict=True, reason="MOD-MKT-7: the campaign fan-out runs synchronously inside the HTTP request")
def test_the_campaign_route_does_not_text_the_whole_list_inside_the_request(db_path, monkeypatch):
    """A6 Campaigns #11 / MOD-MKT-7 — the 50,000-guest case, scaled down:
    the route must hand the list to a background sender and return, not
    make one Twilio call per guest on a request thread."""
    rid = _rid(db_path)
    _consented(db_path, rid, 30)
    inline = []
    monkeypatch.setattr(gm, "send_sms", lambda phone, msg, **kw: inline.append(phone) or True)
    create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    c = app.test_client()
    tok = c.post("/mobile/api/login", json={"username": "owner", "password": "pw-owner-1"}).get_json()["token"]
    resp = c.post("/mobile/api/guest-campaign/send", json={"message": "Pasta night"},
                  headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code in (200, 202)
    assert inline == [], f"{len(inline)} texts sent on the request thread"


@pytest.mark.xfail(strict=True, reason="MOD-MKT-7: quiet hours are checked once, so a send started at 8:57pm keeps texting past 9pm")
def test_a_campaign_that_runs_past_nine_pm_stops_texting_at_nine(db_path, monkeypatch):
    """A6 Campaigns #12 / MOD-MKT-7 — each text takes a minute of (fake)
    wall clock; texts after 21:00 local must be deferred."""
    rid = _rid(db_path)
    _consented(db_path, rid, 10)
    clock = {"t": datetime(2026, 9, 21, 20, 57)}
    monkeypatch.setattr(gm, "_sms_local_now", lambda r: clock["t"])
    at = []

    def rec(phone, msg, **kw):
        at.append(clock["t"])
        clock["t"] += timedelta(minutes=1)
        return True
    monkeypatch.setattr(gm, "send_sms", rec)
    gm.send_campaign(rid, "Last call", db_path=db_path)
    assert all(t.hour < 21 for t in at), [t.strftime("%H:%M") for t in at]


def test_a_campaign_longer_than_an_sms_can_carry_is_refused_before_anyone_is_texted(db_path, texts):
    """A6 Campaigns #13 — Twilio's hard ceiling is 1,600 characters; each
    160 is a billed segment. A 2,000-character body is refused up front."""
    rid = _rid(db_path)
    _consented(db_path, rid, 3)
    out = gm.send_campaign(rid, "x" * 2000, db_path=db_path)
    assert out["ok"] is False and texts == []


@pytest.mark.xfail(strict=True, reason="MOD-MKT-11: guest marketing texts go out on the owner-alert A2P messaging service")
def test_a_guest_campaign_does_not_ride_the_owner_alert_messaging_service(db_path, texts):
    """A6 Campaigns #14 / MOD-MKT-11."""
    rid = _rid(db_path)
    _consented(db_path, rid, 2)
    gm.send_campaign(rid, "Pasta night", db_path=db_path)
    assert texts and all(t.get("use_case") not in (None, "alert") for t in texts), texts


@pytest.mark.xfail(strict=True, reason="MOD-MKT-11: the manual review-request SMS ignores a guest's STOP")
def test_a_manual_review_request_is_not_texted_to_a_guest_who_said_stop(db_path, monkeypatch):
    """MOD-MKT-11 — _do_send_review_request texts whatever number is typed."""
    rid = _rid(db_path, google_place_id="ChIJreview")
    gm.add_guest_contact_public_optin(rid, "5551234567", name="Ana", db_path=db_path)
    gm.handle_inbound_sms("+15551234567", "STOP", db_path=db_path)
    sent = []
    monkeypatch.setattr(notify, "send_sms", lambda phone, msg, **kw: sent.append(phone) or True)
    monkeypatch.setattr(client_api, "get_restaurant", lambda r: models.get_restaurant(r, db_path))
    client_api._do_send_review_request(rid, {"name": "Ana", "phone": "555-123-4567", "sms_consent": True})
    assert sent == []


@pytest.mark.xfail(strict=True, reason="MOD-MKT-11: the manual review-request SMS ignores the 8am-9pm guest window")
def test_a_manual_review_request_is_not_texted_at_midnight(db_path, monkeypatch):
    """MOD-MKT-11."""
    rid = _rid(db_path, google_place_id="ChIJreview")
    monkeypatch.setattr(gm, "_sms_local_now", lambda r: datetime(2026, 9, 21, 0, 30))
    sent = []
    monkeypatch.setattr(notify, "send_sms", lambda phone, msg, **kw: sent.append(phone) or True)
    monkeypatch.setattr(client_api, "get_restaurant", lambda r: models.get_restaurant(r, db_path))
    client_api._do_send_review_request(rid, {"name": "Ana", "phone": "555-123-4567", "sms_consent": True})
    assert sent == []


# ── The automated jobs ──────────────────────────────────────────────────────

def _due_followups(db_path, rid, n):
    for i in range(n):
        cid = gm.add_guest_contact_public_optin(rid, f"55530000{i:02d}", name=f"Guest{i}", db_path=db_path)
        conn = get_conn(db_path)
        conn.execute("UPDATE guest_contacts SET last_visit=? WHERE id=?",
                     ((datetime.now() - timedelta(days=1)).isoformat(), cid))
        conn.commit()
        conn.close()


@pytest.mark.xfail(strict=True, reason="MOD-MKT-10: the review-request job holds the SQLite write lock across every Twilio call")
def test_other_writers_are_not_locked_out_while_review_requests_go_out(db_path, monkeypatch):
    """A6 Jobs #9 / MOD-MKT-10 — a settings save during the job must not
    fail with 'database is locked'."""
    rid = _rid(db_path, google_place_id="ChIJreview")
    _due_followups(db_path, rid, 3)
    outcomes = []

    def send_while_someone_writes(phone, msg, **kw):
        other = sqlite3.connect(db_path, timeout=0.2)
        try:
            other.execute("UPDATE restaurants SET name=name WHERE id=?", (rid,))
            other.commit()
            outcomes.append("ok")
        except sqlite3.OperationalError as e:
            outcomes.append(str(e))
        finally:
            other.close()
        return True
    monkeypatch.setattr(gm, "send_sms", send_while_someone_writes)
    gm.run_review_request_followups(delay_hours=1, db_path=db_path)
    assert outcomes and all(o == "ok" for o in outcomes), outcomes


@pytest.mark.xfail(strict=True, reason="MOD-MKT-10: a review-request run interrupted mid-loop commits nothing, so the next run re-texts everyone")
def test_an_interrupted_review_request_run_does_not_re_text_guests(db_path, monkeypatch):
    """A6 Jobs #10 / MOD-MKT-10."""
    rid = _rid(db_path, google_place_id="ChIJreview")
    _due_followups(db_path, rid, 4)
    sent = []

    def dies_after_two(phone, msg, **kw):
        if len(sent) == 2:
            raise _Killed()
        sent.append(phone)
        return True
    monkeypatch.setattr(gm, "send_sms", dies_after_two)
    try:
        gm.run_review_request_followups(delay_hours=1, db_path=db_path)
    except _Killed:
        pass
    import gc
    gc.collect()      # the dead process's connection is gone with it
    monkeypatch.setattr(gm, "send_sms", lambda phone, msg, **kw: sent.append(phone) or True)
    gm.run_review_request_followups(delay_hours=1, db_path=db_path)
    assert len(sent) == 4 and len(set(sent)) == 4, sent


def _toast_restaurant(db_path, name, **kw):
    rid = _rid(db_path, name=name, **kw)
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET toast_client_id='demo', toast_client_secret='demo', "
                 "toast_restaurant_guid='demo' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return rid


@pytest.mark.xfail(strict=True, reason="MOD-MKT-12: a STOP sent to one restaurant does not stop another restaurant's opt-in invite")
def test_a_stop_to_one_restaurant_stops_another_restaurants_invite(db_path, texts):
    """A6 Jobs #11 / MOD-MKT-12 — the reply promised 'no more texts from us',
    and 'us' is one shared number."""
    a = _toast_restaurant(db_path, "Alpha")
    gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    invited = {t["phone"] for t in texts}
    assert invited
    victim = sorted(invited)[0]
    gm.handle_inbound_sms(victim, "STOP", db_path=db_path)
    texts.clear()
    _toast_restaurant(db_path, "Bravo")
    gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    assert victim not in {t["phone"] for t in texts}


@pytest.mark.xfail(strict=True, reason="MOD-MKT-12: a churned restaurant's opt-in invites keep going out")
def test_a_churned_restaurant_sends_no_opt_in_invites(db_path, texts):
    """A6 Jobs #13 / MOD-MKT-12."""
    rid = _toast_restaurant(db_path, "Gone Co")
    update_restaurant(rid, {"billing_status": "churned"}, db_path=db_path)
    gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    assert texts == []


@pytest.mark.xfail(strict=True, reason="MOD-MKT-12: a churned restaurant's post-visit review requests keep going out")
def test_a_churned_restaurant_sends_no_review_requests(db_path, texts):
    """A6 Jobs #13 / MOD-MKT-12."""
    rid = _rid(db_path, google_place_id="ChIJreview")
    update_restaurant(rid, {"billing_status": "churned"}, db_path=db_path)
    _due_followups(db_path, rid, 2)
    gm.run_review_request_followups(delay_hours=1, db_path=db_path)
    assert texts == []


class _StopLoop(BaseException):
    pass


@pytest.mark.xfail(strict=True, reason="MOD-MKT-12: a restaurant deferred by quiet hours at the 11am invite slot is never retried that day")
def test_a_restaurant_deferred_at_the_invite_slot_is_retried_later_that_day(monkeypatch):
    """A6 Jobs #12 / MOD-MKT-12 — drive two real scheduler ticks (11:05 and
    15:00 server time). The first pass deferred a restaurant (Hawaii is at
    6am); the second tick must run the invite job again."""
    ticks = iter([datetime(2026, 9, 21, 11, 5), datetime(2026, 9, 21, 15, 0)])
    monkeypatch.setattr(scheduler, "_chi_now", lambda: next(ticks))
    claimed = set()

    def claim(job, period):
        if job != "optin_invite":
            return False                         # every other job stays asleep
        if (job, period) in claimed:
            return False
        claimed.add((job, period))
        return True
    monkeypatch.setattr(scheduler._ops, "claim_period", claim)
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    runs = []

    def invite_job(business_date=None, db_path=None):
        runs.append(business_date)
        return {"invited": 0, "skipped": 0, "failed": 0, "deferred_quiet_hours": 1}
    monkeypatch.setattr(gm, "run_toast_optin_invites", invite_job)
    monkeypatch.setattr(scheduler._ops, "run_job", lambda name, fn, *a, **k: fn() if name == "toast_optin_invites" else None)
    import marketing_publish
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None, raising=False)
    sleeps = {"n": 0}

    def sleep(_s):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise _StopLoop()
    monkeypatch.setattr(scheduler.time, "sleep", sleep)
    with pytest.raises(_StopLoop):
        scheduler.scheduler_loop()
    assert len(runs) == 2, runs
