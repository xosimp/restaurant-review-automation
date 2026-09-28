"""Marketing audit fix round, slice A (9/28/26): guest TEXT consent, inbound
SMS, the review-link invites, the public join form and the guest jobs.

  MB-1 / #6      inbound: exact keywords only; a name is only an answer
  MB-2 / OPP-6   marketing consent apart from review-link consent, one rule
                 for every send and count
  MB-3 / #2      invites off until the owner turns them on and acknowledges;
                 never demo rows; unanswered invite contacts purged
  MB-4 / #3 / #9 double opt-in join, durable limits, append-only evidence,
                 a join is not a visit
  MB-20          review requests only about a visit inside 48 hours
  MB-18/19       invites and attribution bounded, resumable, restaurant-local
  CS-19 / #94    audience counts in SQL, phone index

send_sms is always a recorder; nothing reaches Twilio.
"""
import random
import sqlite3
import sys
import types
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import ai_utils
import auth
import auth_routes
import client_api
import guest_links
import guest_marketing as gm
import marketing_opportunities as mo
import mobile_api
import models
import notify
import ops
import pos
import scheduler
import time_utils
import webhook_routes
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_conn, get_restaurant, update_restaurant


@pytest.fixture(autouse=True)
def world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, gm, client_api, mobile_api, webhook_routes, notify, ops, scheduler, auth, auth_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    gm.init_guest_marketing(db_path)
    # Inside the 8am-9pm guest window unless a test says otherwise.
    monkeypatch.setattr(gm, "_sms_local_now", lambda rid: datetime.now().replace(hour=12, minute=0))
    return db_path


@pytest.fixture
def texts(monkeypatch):
    sent = []

    def rec(phone, msg, **kw):
        sent.append({"phone": phone, "msg": msg})
        return True
    monkeypatch.setattr(gm, "send_sms", rec)
    return sent


@pytest.fixture
def pos_guests(monkeypatch):
    """A POS sharing two identified guests for every restaurant id in the set;
    `calls` records (restaurant_id, day) per fetch."""
    on, calls = set(), []
    guests = [{"order_guid": "o-1", "name": "Guest One", "phone": "+15550000001"},
              {"order_guid": "o-2", "name": "Guest Two", "phone": "+15550000002"}]

    def fetch(r, day):
        calls.append((r, day))
        return [dict(g, order_guid=f"{g['order_guid']}-{r}-{day}") for g in guests]
    monkeypatch.setattr(pos, "PROVIDERS", {"toast": types.SimpleNamespace(
        is_connected=lambda r: r in on, fetch_order_customers=fetch,
        sync_to_db=lambda r: {}, build_shifts_csv=lambda r, days=60: None)})
    return types.SimpleNamespace(on=on, calls=calls)


def _rid(db_path, **kw):
    kw.setdefault("name", "Kimball Diner")
    kw.setdefault("owner_email", "k@x.test")
    kw.setdefault("module_marketing", 1)
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _row(db_path, rid, phone):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM guest_contacts WHERE restaurant_id=? AND phone=?",
                         (rid, notify._normalize_phone(phone))).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _sql(db_path, sql, args=()):
    conn = get_conn(db_path)
    try:
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _confirmed(db_path, rid, phone, name="G"):
    """A guest who joined and replied Y (the only marketing consent)."""
    return gm.add_guest_contact_public_optin(rid, phone, name=name, db_path=db_path)


def _review_only(db_path, rid, phone):
    gm.record_optin_invite(rid, phone, external_ref=f"ref-{rid}-{phone}", db_path=db_path)
    gm.handle_inbound_sms(phone, "YES", db_path=db_path)
    assert _row(db_path, rid, phone)["review_consent"] == 1


# ══ MB-1 · inbound: exact keywords, a name is only an answer ═══════════════

def test_mb1_a_complaint_that_names_the_restaurant_does_not_resubscribe(db_path):
    """The audit's probe: a STOPped guest writes "Why is Kimball Diner still
    texting me? I said stop" (10 words, not a STOP by _is_stop) and got
    consent=1, unsubscribed=0 and "Thanks! You're in"."""
    rid = _rid(db_path)
    _confirmed(db_path, rid, "+13125550100")
    gm.handle_inbound_sms("+13125550100", "STOP", db_path=db_path)
    reply = gm.handle_inbound_sms("+13125550100", "Why is Kimball Diner still texting me? I said stop",
                                  db_path=db_path)
    row = _row(db_path, rid, "+13125550100")
    assert reply is None                          # was "Thanks! You're in"
    assert row["unsubscribed"] == 1
    assert gm.phone_opted_out("+13125550100", db_path=db_path)


def test_mb1_a_hand_added_number_asking_a_question_gets_no_consent(db_path):
    """The audit's second probe: a hand-added (consent 0) number asking
    "what time does kimball diner close tonight" became consented."""
    rid = _rid(db_path)
    gm.add_guest_contact_manual(rid, "+13125550101", name="Walk-in", db_path=db_path)
    reply = gm.handle_inbound_sms("+13125550101", "what time does kimball diner close tonight", db_path=db_path)
    row = _row(db_path, rid, "+13125550101")
    assert reply is None
    assert (row["consent"], row["review_consent"], row["unsubscribed"]) == (0, 0, 0)
    assert gm.segment_counts(rid, db_path=db_path)["all"] == 0


@pytest.mark.parametrize("body", ["Kimball Diner", "kimball diner!", "YES", "y", "Yes please save a table",
                                  "START", "unstop"])
def test_mb1_nothing_without_a_pending_question_sets_consent_on_a_hand_added_contact(db_path, body):
    rid = _rid(db_path)
    gm.add_guest_contact_manual(rid, "+13125550102", db_path=db_path)
    gm.handle_inbound_sms("+13125550102", body, db_path=db_path)
    row = _row(db_path, rid, "+13125550102")
    assert (row["consent"], row["review_consent"]) == (0, 0)
    assert row["last_visit"] is None and row["visit_count"] == 0     # a YES is never a visit (#6)


def test_mb1_only_an_exact_start_undoes_a_stop_and_it_grants_no_consent(db_path):
    rid = _rid(db_path)
    gm.add_guest_contact_manual(rid, "+13125550103", db_path=db_path)
    gm.handle_inbound_sms("+13125550103", "stop", db_path=db_path)
    for body in ("start texting me about kimball diner", "Kimball Diner", "yes please"):
        gm.handle_inbound_sms("+13125550103", body, db_path=db_path)
        assert _row(db_path, rid, "+13125550103")["unsubscribed"] == 1, body
    reply = gm.handle_inbound_sms("+13125550103", "Start.", db_path=db_path)
    row = _row(db_path, rid, "+13125550103")
    assert row["unsubscribed"] == 0 and not gm.phone_opted_out("+13125550103", db_path=db_path)
    assert row["consent"] == 0                     # START undoes STOP; it is not an opt-in
    assert "resubscribed" in reply.lower()
    ev = [e["event"] for e in gm.consent_events(rid, "+13125550103", db_path=db_path)]
    assert "resubscribed" in ev and "opted_out" in ev


def test_mb1_start_does_not_override_an_owner_unsubscribe(db_path):
    a = _rid(db_path)
    b = _rid(db_path, name="Other Place", owner_email="o@x.test")
    _confirmed(db_path, a, "+13125550104")
    _confirmed(db_path, b, "+13125550104")
    gm.unsubscribe_guest(a, "+13125550104", db_path=db_path)      # the owner's own action
    gm.handle_inbound_sms("+13125550104", "STOP", db_path=db_path)
    gm.handle_inbound_sms("+13125550104", "START", db_path=db_path)
    assert _row(db_path, a, "+13125550104")["unsubscribed"] == 1
    assert _row(db_path, b, "+13125550104")["unsubscribed"] == 0


def test_mb1_a_name_answers_only_an_open_question_and_never_after_a_stop(db_path):
    a = _rid(db_path, name="Syrup Chicago")
    b = _rid(db_path, name="Kimball Diner", owner_email="o@x.test")
    for rid in (a, b):
        gm.record_optin_invite(rid, "+13125550105", external_ref=f"r{rid}", db_path=db_path)
    # a bare name before any question is asked: nothing
    gm.handle_inbound_sms("+13125550105", "Kimball Diner", db_path=db_path)
    assert _row(db_path, b, "+13125550105") is None
    assert "Which restaurant" in gm.handle_inbound_sms("+13125550105", "yes", db_path=db_path)
    gm.handle_inbound_sms("+13125550105", "stop", db_path=db_path)          # closes the question
    assert gm.handle_inbound_sms("+13125550105", "Kimball Diner", db_path=db_path) is None
    assert (_row(db_path, b, "+13125550105") or {}).get("review_consent", 0) == 0
    assert gm.phone_opted_out("+13125550105", db_path=db_path)


def test_mb1_yes_naming_a_pending_restaurant_resolves_directly(db_path):
    a = _rid(db_path, name="Syrup Chicago")
    b = _rid(db_path, name="Kimball Diner", owner_email="o@x.test")
    for rid in (a, b):
        gm.record_optin_invite(rid, "+13125550106", external_ref=f"r{rid}", db_path=db_path)
    reply = gm.handle_inbound_sms("+13125550106", "Yes, Kimball Diner", db_path=db_path)
    assert "Kimball Diner" in reply
    assert _row(db_path, b, "+13125550106")["review_consent"] == 1
    assert _row(db_path, a, "+13125550106") is None


@pytest.mark.parametrize("body", ["ＳＴＯＰ", "​STOP", "Stop.", "please stop"])
def test_mb1_stop_normalisation_is_intact(db_path, body):
    rid = _rid(db_path)
    _confirmed(db_path, rid, "+13125550107")
    gm.handle_inbound_sms("+13125550107", body, db_path=db_path)
    assert _row(db_path, rid, "+13125550107")["unsubscribed"] == 1


def test_mb1_source_has_no_substring_name_match():
    src = open(gm.__file__, encoding="utf-8").read()
    assert "_match_named_restaurant" not in src and "_inbound_candidates" not in src
    body = src.split("def handle_inbound_sms")[1].split("\ndef ")[0]
    assert "norm in START_KEYWORDS" in body and "word in START_KEYWORDS" not in body


# ══ MB-2 / OPP-6 · marketing consent apart from review-link consent ════════

def test_mb2_a_review_link_yes_is_never_in_a_campaign_audience(db_path, texts):
    """The audit's probe: a YES-for-review guest in segment_contacts(rid,
    "all") got promo blasts."""
    rid = _rid(db_path)
    _confirmed(db_path, rid, "+13125550200")
    _review_only(db_path, rid, "+13125550201")
    texts.clear()
    assert [c["phone"] for c in gm.segment_contacts(rid, "all", db_path=db_path)] == ["+13125550200"]
    assert gm.segment_counts(rid, db_path=db_path)["all"] == 1
    assert gm.audience_size(rid, db_path=db_path) == 1
    assert [c["phone"] for c in gm.get_guest_contacts(rid, consent_only=True, db_path=db_path)] == ["+13125550200"]
    led = gm.consent_ledger(rid, db_path=db_path)
    assert (led["textable"], led["review_only"]) == (1, 1)
    assert gm.campaign_overview(rid, db_path=db_path)["subscribers"] == 1
    out = gm.send_campaign(rid, "Half-price wine tonight", db_path=db_path)
    assert out["sent"] == 1 and [t["phone"] for t in texts] == ["+13125550200"]


def test_mb2_opp6_the_feed_the_studio_and_the_send_count_the_same_guests(db_path, texts):
    """OPP-6: the card said "Text your 48", the Studio "Text 60" and the send
    texted the 12 review-link YESes. One rule now."""
    rid = _rid(db_path)
    for i in range(27):
        _confirmed(db_path, rid, f"+1312555{3000 + i}")
    for i in range(6):
        _review_only(db_path, rid, f"+1312555{4000 + i}")
    texts.clear()
    card = next(c for c in mo.lists(rid, datetime.now(), db_path) if c["key"] == "list_idle:text")
    assert card["title"] == "Text your 27 opted-in guests"
    assert gm.segment_counts(rid, db_path=db_path)["all"] == 27 == gm.audience_size(rid, db_path=db_path)
    assert gm.send_campaign(rid, "Patio's open", db_path=db_path)["sent"] == 27 == len(texts)


def test_mb2_review_requests_keep_working_for_review_only_guests(db_path, texts):
    rid = _rid(db_path, google_place_id="ChIJreview")
    _review_only(db_path, rid, "+13125550202")
    texts.clear()
    cid = _row(db_path, rid, "+13125550202")["id"]
    visit = (time_utils.restaurant_now_by_id(rid, naive=True) - timedelta(hours=4)).isoformat()
    _sql(db_path, "UPDATE guest_contacts SET last_visit=?, last_review_requested_at=NULL WHERE id=?", (visit, cid))
    assert gm.run_review_request_followups(delay_hours=3, db_path=db_path)["sent"] == 1
    assert "ChIJreview" in texts[0]["msg"]


def test_mb2_the_invite_yes_gets_the_review_link_it_asked_for(db_path):
    rid = _rid(db_path, google_place_id="ChIJlink")
    gm.record_optin_invite(rid, "+13125550203", external_ref="o", db_path=db_path)
    reply = gm.handle_inbound_sms("+13125550203", "yes", db_path=db_path, message_sid="SMyes")
    assert "writereview?placeid=ChIJlink" in reply
    ev = gm.consent_events(rid, "+13125550203", db_path=db_path)[0]
    assert (ev["event"], ev["source"], ev["message_sid"]) == ("review_only", "sms_reply", "SMyes")
    assert _sql(db_path, "SELECT method FROM review_requests WHERE restaurant_id=?", (rid,))[0][0] == "sms_reply"


def test_mb2_backfill_moves_legacy_review_yeses_to_review_only_once(db_path):
    rid = _rid(db_path)
    ins = ("INSERT INTO guest_contacts (restaurant_id, phone, consent, consent_at) VALUES (?,?,1,?)")
    inv = ("INSERT INTO sms_optin_invites (restaurant_id, phone, external_ref, responded_at, response) "
           "VALUES (?,?,?,?,'yes')")
    # YES-only: consent set by the YES itself
    _sql(db_path, ins, (rid, "+13125550210", "2026-09-01T12:00:00.100000"))
    _sql(db_path, inv, (rid, "+13125550210", "a", "2026-09-01T12:00:00.200000"))
    # marketing consent a day before the YES: it came from elsewhere
    _sql(db_path, ins, (rid, "+13125550211", "2026-08-01T12:00:00"))
    _sql(db_path, inv, (rid, "+13125550211", "b", "2026-08-02T12:00:00"))
    # a confirmed marketing opt-in on record
    _sql(db_path, ins, (rid, "+13125550212", "2026-09-01T12:00:00"))
    _sql(db_path, inv, (rid, "+13125550212", "c", "2026-09-01T12:00:00"))
    gm.record_consent_event(rid, "confirmed", "sms_reply", phone="+13125550212", db_path=db_path)

    gm.init_guest_marketing(db_path)
    gm.init_guest_marketing(db_path)          # boot twice: idempotent

    assert (_row(db_path, rid, "+13125550210")["consent"], _row(db_path, rid, "+13125550210")["review_consent"]) == (0, 1)
    assert _row(db_path, rid, "+13125550211")["consent"] == 1
    assert _row(db_path, rid, "+13125550212")["consent"] == 1
    ev = gm.consent_events(rid, "+13125550210", db_path=db_path)
    assert [(e["event"], e["source"]) for e in ev] == [("review_only", "backfill")]
    # the demoted guest later joins and confirms: a later boot leaves them be
    _confirmed(db_path, rid, "+13125550210")
    gm.init_guest_marketing(db_path)
    assert _row(db_path, rid, "+13125550210")["consent"] == 1


def test_mb2_a_stop_from_someone_else_leaves_every_audience_alone(db_path, texts):
    """The one rule checks guest_sms_optouts for THIS guest's number: an
    unqualified `phone` inside that subquery is the opt-out row's own column,
    and one STOP on file would have emptied every list."""
    rid = _rid(db_path)
    _confirmed(db_path, rid, "+13125550220")
    _confirmed(db_path, rid, "+13125550221")
    gm.handle_inbound_sms("+13125550221", "STOP", db_path=db_path)
    gm.handle_inbound_sms("+19995550000", "STOP", db_path=db_path)        # a stranger's STOP
    assert gm.segment_counts(rid, db_path=db_path)["all"] == 1 == gm.audience_size(rid, db_path=db_path)
    assert [c["phone"] for c in gm.marketing_audience(rid, db_path=db_path)] == ["+13125550220"]
    assert gm.consent_ledger(rid, db_path=db_path)["textable"] == 1
    assert gm.send_campaign(rid, "Tonight", db_path=db_path)["sent"] == 1


def test_mb2_every_text_audience_reads_the_one_rule():
    """Asserted against the source (a fixture only covers its own branches):
    no hand-rolled consent check is left in the module's queries."""
    src = open(gm.__file__, encoding="utf-8").read()
    assert "consent=1 AND unsubscribed=0" not in src
    for fn in ("def marketing_audience(", "def marketing_audience_count(", "def segment_counts(",
               "def consent_ledger(", "def get_guest_contacts("):
        assert "marketing_text_sql()" in src.split(fn)[1].split("\ndef ")[0], fn
    mo_src = open(mo.__file__, encoding="utf-8").read()
    lists_body = mo_src.split("def lists(")[1].split("\ndef ")[0]
    # The feed's list counts read the one rule; its own review-YES exclusion
    # is gone. (The cache fingerprint still COUNTS invite replies so a new one
    # rebuilds the feed - an input, not a consent check.)
    assert "marketing_text_sql" in lists_body and "response='yes'" not in lists_body


# ══ MB-4 · the public join: double opt-in ══════════════════════════════════

@pytest.fixture
def web():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _join(web, rid, phone, name="Guest", ua="TestBrowser/1.0", **extra):
    return web.post(f"/api/public/guest-optin/{guest_links.sign_join(rid)}",
                    json=dict({"name": name, "phone": phone, "consent": True}, **extra),
                    headers={"User-Agent": ua})


def test_mb4_a_join_is_pending_until_the_phone_replies_y(db_path, web, texts):
    """The audit's probe: anyone with the link could enrol any number."""
    rid = _rid(db_path)
    resp = _join(web, rid, "(312) 555-0300", name="Stranger")
    assert resp.status_code == 200 and resp.get_json() == {"ok": True, "pending": True}
    row = _row(db_path, rid, "+13125550300")
    assert row["consent"] == 0 and gm.segment_counts(rid, db_path=db_path)["all"] == 0
    assert [t["phone"] for t in texts] == ["+13125550300"]
    msg = texts[0]["msg"]
    assert msg.startswith("Kimball Diner: Reply Y to confirm") and len(msg) <= 160
    for part in ("Msg frequency varies", "Msg & data rates may apply", "STOP", "HELP"):
        assert part in msg
    req = gm.consent_events(rid, "+13125550300", db_path=db_path)[0]
    assert (req["event"], req["source"], req["ip"], req["user_agent"]) == \
        ("requested", "join_form", "127.0.0.1", "TestBrowser/1.0")
    assert (req["disclosure_version"], req["disclosure_hash"]) == \
        (gm.JOIN_DISCLOSURE_VERSION, gm.join_disclosure_hash())

    reply = gm.handle_inbound_sms("+13125550300", "Y", db_path=db_path, message_sid="SMconfirm1")
    assert "Kimball Diner" in reply and "STOP" in reply
    assert _row(db_path, rid, "+13125550300")["consent"] == 1
    conf = gm.consent_events(rid, "+13125550300", db_path=db_path)[0]
    assert (conf["event"], conf["message_sid"], conf["ip"], conf["disclosure_hash"]) == \
        ("confirmed", "SMconfirm1", "127.0.0.1", gm.join_disclosure_hash())


def test_mb4_a_y_after_48_hours_confirms_nothing(db_path, texts):
    rid = _rid(db_path)
    assert gm.request_public_optin(rid, "+13125550301", name="Late", db_path=db_path)["ok"]
    _sql(db_path, "UPDATE guest_optin_requests SET requested_at=datetime('now','-49 hours')")
    assert gm.handle_inbound_sms("+13125550301", "yes", db_path=db_path) is None
    assert _row(db_path, rid, "+13125550301")["consent"] == 0


def test_mb4_a_join_is_not_a_visit_and_starts_no_review_request(db_path, web, texts):
    """MB-5's join half: an existing guest re-joining was a visit (last_visit,
    visit_count+1) and got "thanks for visiting" three hours later."""
    rid = _rid(db_path, google_place_id="ChIJjoin")
    cid = _confirmed(db_path, rid, "+13125550302", name="Ana")
    old = (time_utils.restaurant_now_by_id(rid, naive=True) - timedelta(days=10)).isoformat()
    _sql(db_path, "UPDATE guest_contacts SET last_visit=?, visit_count=2, last_review_requested_at=? WHERE id=?",
         (old, old, cid))
    for _ in range(3):
        _join(web, rid, "312-555-0302", name="Prankster")
    row = _row(db_path, rid, "+13125550302")
    assert (row["last_visit"], row["visit_count"], row["name"]) == (old, 2, "Ana")
    texts.clear()
    gm.run_review_request_followups(delay_hours=0, db_path=db_path)
    assert texts == []


def test_mb4_the_per_phone_limit_is_durable(db_path, web, texts):
    """Three submissions for one number a day, across restaurants — held in
    the database, so a restart (the in-memory limiter cleared) resets nothing."""
    rids = [_rid(db_path, name=f"Place {i}", owner_email=f"p{i}@x.test") for i in range(4)]
    codes = []
    for rid in rids:
        ai_utils._ai_call_log.clear()
        codes.append(_join(web, rid, "3125550303").status_code)
    assert codes == [200, 200, 200, 429]
    assert len(texts) == 3


def test_mb4_the_per_ip_limit_is_durable(db_path, texts):
    rids = [_rid(db_path, name=f"Place {i}", owner_email=f"q{i}@x.test") for i in range(3)]
    results = [gm.request_public_optin(rids[i % 3], f"+1312556{i:04d}", name="G", ip="203.0.113.7",
                                       db_path=db_path) for i in range(gm.JOIN_IP_HOURLY_LIMIT + 1)]
    assert all(r["ok"] for r in results[:-1]) and results[-1]["status"] == 429


def test_mb4_a_double_tap_sends_one_confirmation(db_path, web, texts):
    rid = _rid(db_path)
    _join(web, rid, "3125550304")
    _join(web, rid, "3125550304")
    assert len(texts) == 1


def test_mb4_stopped_and_already_joined_numbers_get_no_text_and_the_same_answer(db_path, web, texts):
    rid = _rid(db_path)
    _confirmed(db_path, rid, "+13125550305")
    gm.add_guest_contact_manual(rid, "+13125550306", db_path=db_path)
    gm.handle_inbound_sms("+13125550306", "STOP", db_path=db_path)
    texts.clear()
    answers = [_join(web, rid, p).get_json() for p in ("3125550305", "3125550306")]
    assert answers == [{"ok": True, "pending": True}] * 2
    assert texts == []
    assert _row(db_path, rid, "+13125550306")["unsubscribed"] == 1


def test_mb4_a_failed_confirmation_text_is_said_not_hidden(db_path, web, monkeypatch):
    monkeypatch.setattr(gm, "send_sms", lambda *a, **k: False)
    rid = _rid(db_path)
    resp = _join(web, rid, "3125550307")
    assert resp.status_code == 502 and "confirmation text" in resp.get_json()["error"]
    assert gm.handle_inbound_sms("+13125550307", "Y", db_path=db_path) is None


def test_mb4_evidence_outlives_the_contact_and_is_append_only(db_path, texts):
    rid = _rid(db_path)
    gm.request_public_optin(rid, "+13125550308", name="Ana", ip="198.51.100.2", db_path=db_path)
    gm.handle_inbound_sms("+13125550308", "yes", db_path=db_path, message_sid="SMx")
    cid = _row(db_path, rid, "+13125550308")["id"]
    gm.delete_guest_contact(cid, rid, db_path=db_path)
    assert [e["event"] for e in gm.consent_events(rid, "+13125550308", db_path=db_path)] == ["confirmed", "requested"]
    with pytest.raises(sqlite3.DatabaseError):
        _sql(db_path, "UPDATE guest_consent_events SET event='confirmed' WHERE phone=?", ("+13125550308",))


def test_mb4_the_form_shows_the_recorded_disclosure_and_links(db_path, web):
    rid = _rid(db_path)
    from markupsafe import escape
    html = web.get(f"/join/{guest_links.sign_join(rid)}").get_data(as_text=True)
    assert str(escape(gm.join_disclosure("Kimball Diner"))) in html
    assert "Msg frequency varies" in html and 'href="/terms"' in html and 'href="/privacy"' in html
    assert "You're in!" not in html and "Reply <b>Y</b>" in html


def test_mb4_the_confirmation_is_one_gsm_segment_for_any_name():
    for name in ("Simple EJ’s", "A" * 90, ""):
        msg = gm.confirmation_text(name)
        assert len(msg) <= 160 and all(ord(ch) < 128 for ch in msg), msg
        assert msg.endswith("Reply STOP to cancel, HELP for help.")


def test_mb4_the_route_never_grants_consent_itself():
    src = open(client_api.__file__, encoding="utf-8").read()
    body = src.split("def guest_optin_submit")[1].split("\ndef ")[0]
    assert "request_public_optin" in body and "add_guest_contact_public_optin" not in body


# ══ MB-3 · invites off until the owner turns them on ═══════════════════════

def test_mb3_invites_are_off_by_default_even_with_marketing_on(db_path, texts, pos_guests):
    rid = _rid(db_path)
    pos_guests.on.add(rid)
    assert get_restaurant(rid, db_path=db_path).optin_invites_enabled == 0
    assert gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)["invited"] == 0
    update_restaurant(rid, {"optin_invites_enabled": 1}, db_path=db_path)      # no acknowledgement
    assert gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)["invited"] == 0
    assert texts == [] and pos_guests.calls == []


def test_mb3_on_needs_an_acknowledgement_which_is_stored(db_path, texts, pos_guests):
    rid = _rid(db_path)
    pos_guests.on.add(rid)
    assert not gm.set_optin_invites(rid, True, user_id=7, db_path=db_path)["ok"]
    out = gm.set_optin_invites(rid, True, user_id=7, acknowledged=True, db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert out["ok"] and r.optin_invites_enabled == 1 and r.optin_invites_ack_by == 7 and r.optin_invites_ack_at
    assert gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)["invited"] == 2
    gm.set_optin_invites(rid, False, db_path=db_path)
    assert get_restaurant(rid, db_path=db_path).optin_invites_ack_at == r.optin_invites_ack_at   # kept on record


def test_mb3_demo_rows_are_never_invited(db_path, texts, pos_guests):
    rid = _rid(db_path, is_demo=1)
    pos_guests.on.add(rid)
    gm.set_optin_invites(rid, True, user_id=1, acknowledged=True, db_path=db_path)
    assert gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)["invited"] == 0


@pytest.fixture
def app_client(db_path, monkeypatch):
    auth_routes._login_attempts.clear()
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(client_api.client_bp)
    return app.test_client()


def _login(c, db_path, rid, username, role=None):
    create_user(rid, username, f"{username}@x.test", "correct-horse-9", db_path=db_path, role=role)
    tok = c.post("/mobile/api/login", json={"username": username, "password": "correct-horse-9"}).get_json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_mb3_the_switch_route_is_the_owners(db_path, app_client):
    rid = _rid(db_path)
    owner = _login(app_client, db_path, rid, "owner1")
    d = app_client.get("/mobile/api/guest-optin-invites", headers=owner).get_json()
    assert d["ok"] and d["enabled"] is False and d["disclosure"] == gm.OPTIN_INVITES_DISCLOSURE
    assert app_client.post("/mobile/api/guest-optin-invites", json={"enabled": True},
                           headers=owner).status_code == 400                   # no acknowledgement
    assert app_client.post("/mobile/api/guest-optin-invites", json={"enabled": True, "acknowledged": True},
                           headers=owner).get_json()["enabled"] is True
    manager = _login(app_client, db_path, rid, "mgr1", role="manager")
    assert app_client.post("/mobile/api/guest-optin-invites", json={"enabled": False, "acknowledged": True},
                           headers=manager).status_code == 403
    assert get_restaurant(rid, db_path=db_path).optin_invites_enabled == 1
    src = open(client_api.__file__, encoding="utf-8").read()
    i = src.index('@client_bp.route("/api/guest-optin-invites"')
    assert '_m("mobile_guest_optin_invites")' in src[i:i + 300]


def test_mb3_the_settings_switch_is_on_the_campaigns_page():
    html = open("templates/dashboard.html", encoding="utf-8").read()
    assert 'id="cp-inv-row"' in html and 'onchange="cpInvitesToggle(this)"' in html
    row = html.split('id="cp-inv-row"')[1].split("</div>")[0]
    assert 'class="ac-switch"' in row and "cp-inv-say" in row
    assert "cpLoadInvites();" in html and "acknowledged: on" in html


def test_mb3_unanswered_invite_contacts_are_purged_and_stops_are_kept(db_path, texts, pos_guests):
    rid = _rid(db_path)
    pos_guests.on.add(rid)
    gm.set_optin_invites(rid, True, user_id=1, acknowledged=True, db_path=db_path)
    gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    gm.handle_inbound_sms("+15550000002", "STOP", db_path=db_path)          # an answer, and a STOP
    gm.add_guest_contact_manual(rid, "+15550000009", db_path=db_path)        # hand-added: never purged
    assert _row(db_path, rid, "+15550000001")["source"] == "toast_invite"
    assert gm.purge_unanswered_contacts(db_path=db_path) == 0                # nothing 30 days old yet
    _sql(db_path, "UPDATE guest_contacts SET created_at=datetime('now','-31 days')")
    _sql(db_path, "UPDATE sms_optin_invites SET sent_at=datetime('now','-31 days')")
    out = gm.run_toast_optin_invites(business_date=date(2026, 9, 4), db_path=db_path)
    assert out["purged"] == 1
    assert _row(db_path, rid, "+15550000001") is None
    assert _row(db_path, rid, "+15550000002")["unsubscribed"] == 1
    assert _row(db_path, rid, "+15550000009") is not None
    assert gm.phone_opted_out("+15550000002", db_path=db_path)
    # never invited again: the invite record stays
    assert all(t["phone"] != "+15550000001" for t in texts[2:])


def test_mb3_the_purge_is_bounded(db_path):
    rid = _rid(db_path)
    for i in range(5):
        gm.add_guest_contact_manual(rid, f"+1312557{i:04d}", db_path=db_path, source="toast_invite")
    _sql(db_path, "UPDATE guest_contacts SET created_at=datetime('now','-40 days')")
    assert gm.purge_unanswered_contacts(db_path=db_path, limit=2) == 2
    assert len(gm.get_guest_contacts(rid, db_path=db_path)) == 3


# ══ MB-20 · review requests only about a recent visit ══════════════════════

@pytest.mark.parametrize("hours_ago,sent", [(4, 1), (47, 1), (50, 0), (24 * 90, 0)])
def test_mb20_review_requests_only_within_48_hours(db_path, texts, hours_ago, sent):
    rid = _rid(db_path, google_place_id="ChIJfresh")
    cid = _confirmed(db_path, rid, "+13125550400")
    visit = (time_utils.restaurant_now_by_id(rid, naive=True) - timedelta(hours=hours_ago)).isoformat()
    _sql(db_path, "UPDATE guest_contacts SET last_visit=? WHERE id=?", (visit, cid))
    assert gm.run_review_request_followups(delay_hours=3, db_path=db_path)["sent"] == sent


# ══ MB-18 / MB-19 · bounded, resumable, restaurant-local ═══════════════════

def test_mb19_the_last_closed_business_day_is_the_restaurants_own():
    r = Restaurant(name="X", owner_email="x@x.test")
    assert gm._last_closed_business_date(r, datetime(2026, 9, 28, 11, 0)) == date(2026, 9, 27)
    # 1:30am is still last night's service: its day is not closed
    assert gm._last_closed_business_date(r, datetime(2026, 9, 28, 1, 30)) == date(2026, 9, 26)


def test_mb19_the_invite_pass_reads_each_restaurants_closed_day(db_path, texts, pos_guests, monkeypatch):
    rid = _rid(db_path, timezone="Pacific/Honolulu")
    pos_guests.on.add(rid)
    gm.set_optin_invites(rid, True, user_id=1, acknowledged=True, db_path=db_path)
    # 1:30am in Honolulu on 9/28: 9/27's service is still open, 9/26 is the closed day
    monkeypatch.setattr(time_utils, "restaurant_now",
                        lambda r=None, naive=False: datetime(2026, 9, 28, 1, 30))
    gm.run_toast_optin_invites(db_path=db_path)
    assert pos_guests.calls == [(rid, date(2026, 9, 26))]
    assert _sql(db_path, "SELECT business_date FROM optin_invite_runs WHERE restaurant_id=?", (rid,))[0][0] \
        == "2026-09-26"


def test_mb18_an_invite_pass_cut_off_resumes_without_losing_a_restaurant(db_path, texts, pos_guests):
    a = _rid(db_path, name="Alpha")
    b = _rid(db_path, name="Bravo", owner_email="b@x.test")
    for rid in (a, b):
        pos_guests.on.add(rid)
        gm.set_optin_invites(rid, True, user_id=1, acknowledged=True, db_path=db_path)
    first = gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path, max_seconds=0)
    assert first["hit_bound"] and first["invited"] == 0
    assert _sql(db_path, "SELECT COUNT(*) FROM optin_invite_runs")[0][0] == 0      # the cut-off one is not done
    pos_guests.calls.clear()
    second = gm.run_toast_optin_invites(business_date=date(2026, 9, 3), db_path=db_path)
    assert [c[0] for c in pos_guests.calls] == [b, a]            # the cursor: the one not reached leads
    assert second["invited"] == 4 and not second["hit_bound"]    # two guests at each restaurant
    assert _sql(db_path, "SELECT COUNT(*) FROM optin_invite_runs")[0][0] == 2


def test_mb19_attribution_starts_the_day_after_the_local_send_day(db_path, monkeypatch):
    rid = _rid(db_path)
    fetched = []
    monkeypatch.setattr(pos, "PROVIDERS", {"toast": types.SimpleNamespace(
        is_connected=lambda r: r == rid, sync_to_db=lambda r: {}, build_shifts_csv=lambda r, days=60: None,
        fetch_order_customers=lambda r, day: fetched.append(day.isoformat()) or [])})
    # 8:30pm Central on 9/15 is 01:30 UTC on 9/16
    _sql(db_path, "INSERT INTO guest_campaigns (restaurant_id, message, sent_count, created_at) "
                  "VALUES (?,?,?,?)", (rid, "Patio", 3, "2026-09-16 01:30:00"))
    camp_id = _sql(db_path, "SELECT MAX(id) FROM guest_campaigns")[0][0]
    _sql(db_path, "INSERT INTO guest_campaign_recipients (campaign_id, restaurant_id, contact_id, phone) "
                  "VALUES (?,?,?,?)", (camp_id, rid, 1, "+13125550500"))
    gm.run_campaign_attribution(db_path=db_path, today=date(2026, 9, 19))
    assert fetched == ["2026-09-16", "2026-09-17", "2026-09-18"]      # the UTC reading skipped 9/16


def test_mb18_attribution_is_a_resumable_sweep():
    src = open(gm.__file__, encoding="utf-8").read()
    body = src.split("def run_campaign_attribution")[1].split("\ndef ")[0]
    assert "resumable_sweep(ATTRIBUTION_CURSOR_KEY" in body and "local_iso(" in body
    body = src.split("def run_toast_optin_invites")[1].split("\ndef ")[0]
    assert "resumable_sweep(OPTIN_INVITE_CURSOR_KEY" in body and "_last_closed_business_date" in body
    sched = open(scheduler.__file__, encoding="utf-8").read()
    assert "run_toast_optin_invites(business_date=_d.today()" not in sched


# ══ CS-19 / #94 / MB-21 · counts in SQL, phone index ═══════════════════════

def test_cs19_sql_segments_match_the_python_filter_everywhere(db_path):
    """The SQL twin (_segment_where / _not_recent_where) against
    filter_segment / _too_soon over randomised stamps, boundaries included."""
    rid = _rid(db_path)
    now = time_utils.restaurant_now_by_id(rid, naive=True)
    rnd = random.Random(94)
    stamps = [None, "", "garbage", (now - timedelta(days=30)).isoformat(),
              (now - timedelta(days=30, seconds=-2)).isoformat(),
              (now - timedelta(days=60, hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
              (now - timedelta(days=45)).strftime("%Y-%m-%d"),
              (now - timedelta(days=3, minutes=1)).isoformat(), (now - timedelta(days=2)).isoformat()]
    for i in range(80):
        cid = _confirmed(db_path, rid, f"+1312558{i:04d}")
        _sql(db_path, "UPDATE guest_contacts SET last_visit=?, visit_count=?, last_campaign_at=? WHERE id=?",
             (rnd.choice(stamps + [(now - timedelta(days=rnd.randint(0, 90), minutes=rnd.randint(0, 999))).isoformat()]),
              rnd.choice([0, 1, 2, 3, 7, None]), rnd.choice(stamps), cid))
    everyone = gm.get_guest_contacts(rid, consent_only=True, db_path=db_path)
    counts = gm.segment_counts(rid, db_path=db_path)
    for seg in gm.SEGMENTS:
        py = sorted(c["id"] for c in gm.filter_segment(rid, everyone, seg))
        assert sorted(c["id"] for c in gm.marketing_audience(rid, seg, db_path=db_path)) == py, seg
        assert counts[seg] == len(py), seg
        not_recent = [c for c in gm.filter_segment(rid, everyone, seg) if not gm._too_soon(c, now)]
        assert gm.audience_size(rid, seg, db_path=db_path) == len(not_recent), seg


def test_mb21_phone_lookups_use_an_index(db_path):
    conn = get_conn(db_path)
    try:
        names = {r[1] for r in conn.execute("PRAGMA index_list(guest_contacts)")}
        plan = " ".join(str(tuple(r)) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM guest_contacts WHERE phone=?", ("+1",)))
    finally:
        conn.close()
    assert "idx_guest_contacts_phone" in names and "idx_guest_contacts_phone" in plan


def test_cs19_counting_does_not_load_the_list():
    src = open(gm.__file__, encoding="utf-8").read()
    for fn in ("def segment_counts(", "def audience_size(", "def consent_ledger("):
        body = src.split(fn)[1].split("\ndef ")[0]
        assert "get_guest_contacts(" not in body and "segment_contacts(" not in body, fn
