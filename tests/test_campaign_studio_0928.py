"""Campaign Studio, 9/28/26: one goal drafts a text, an email and (when an
account is connected) a social post. The email is the restaurant's own - a
masthead, a photo from its media library, a headline, one button - and the
page previews it through the same render the send uses. An email reaches
the same audience a text does, its look is part of what it is, and its
opens come back per newsletter as a floor. Web only; the Email box left
Content."""
import json
import types

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import emails
import guest_email as ge
import guest_marketing as gm
import marketing_media
import mobile_api
import models
from auth import create_user, init_auth
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_conn, get_restaurant, update_restaurant

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


@pytest.fixture
def db(db_path, monkeypatch):
    gm.init_guest_marketing(db_path)
    real = models.get_conn
    for mod in (models, ge, gm, marketing_media):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path))
    return db_path


def _rid(db, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Studio Co"), owner_email="o@x.test", module_marketing=1, **kw),
                             db_path=db)


def _guest(db, rid, i, *, email=True, visits=1, days_ago=5):
    c = get_conn(db)
    c.execute("INSERT INTO guest_contacts (restaurant_id, phone, name, consent, consent_at, last_visit, visit_count, "
              "email, email_consent, email_unsubscribed) VALUES (?,?,?,1,datetime('now'),datetime('now', ?),?,?,?,0)",
              (rid, f"+1555030{i:04d}", f"Guest{i} Last", f"-{days_ago} days", visits,
               f"g{i}@example.test" if email else None, 1 if email else 0))
    c.commit()
    c.close()


def _media(db, rid):
    c = get_conn(db)
    mid = c.execute("INSERT INTO marketing_media (restaurant_id, token, mime, data) VALUES (?,?,?,?)",
                    (rid, f"tok{rid}", "image/jpeg", b"x")).lastrowid
    c.commit()
    c.close()
    return mid


# ── the design: what an email carries besides its text ─────────────────────

def test_a_design_keeps_only_what_the_frame_renders(db):
    rid, other = _rid(db), _rid(db, name="Other Co")
    mine, theirs = _media(db, rid), _media(db, other)
    d = ge.clean_design({"headline": "  Pull up   a chair ", "button_label": "Book", "button_url": "javascript:alert(1)",
                         "image_media_id": theirs, "image_url": "https://evil.test/pixel.gif", "extra": 1}, rid, db_path=db)
    assert d == {"headline": "Pull up a chair", "preheader": "", "button_label": "Book", "button_url": "",
                 "image_media_id": None}                             # another tenant's photo, a script link: gone
    d = ge.clean_design({"button_url": "https://ej.test/book", "image_media_id": str(mine)}, rid, db_path=db)
    assert d["button_url"] == "https://ej.test/book" and d["image_media_id"] == mine


# ── the drafter ─────────────────────────────────────────────────────────────

def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def _model(monkeypatch, reply, seen=None):
    monkeypatch.setattr(ge, "get_client", lambda *a, **k: object())
    def create(client, **kw):
        if seen is not None:
            seen["prompt"] = kw["messages"][0]["content"]
            seen["kw"] = kw
        return _msg(reply)
    monkeypatch.setattr(ge, "create_with_retry", create)


GOOD = {"subject": "Pull up a chair", "preheader": "Your table is ready this week", "headline": "Come see us",
        "body": "The kitchen is on.\n\n\n\nCome sit with us this week.", "button": "Book a table"}


def test_the_email_is_drafted_from_the_goal_with_the_hard_rules(db, monkeypatch):
    rid, seen = _rid(db), {}
    _model(monkeypatch, "Here you go:\n" + json.dumps(GOOD), seen)
    out = ge.draft_newsletter(get_restaurant(rid, db_path=db), goal="Fill Tuesday lunch")
    assert out == {"subject": "Pull up a chair", "preheader": "Your table is ready this week", "headline": "Come see us",
                   "body": "The kitchen is on.\n\nCome sit with us this week.", "button_label": "Book a table"}
    p = seen["prompt"]
    assert "What the owner wants this email to do, in their words: Fill Tuesday lunch." in p
    for rule in ("No offer", "No story: nothing anyone said, asked, noticed or did", "Nothing is new, back, started, better or changed",
                 "never say how long it has been since a guest's visit", "Give no reason or cause for anything"):
        assert rule in p, rule
    assert seen["kw"]["action"] == "guest_newsletter_draft"


def test_an_invented_offer_or_unreadable_reply_is_refused(db, monkeypatch):
    rid = _rid(db)
    _model(monkeypatch, json.dumps({**GOOD, "body": "Enjoy 20% off all week."}))
    with pytest.raises(ValueError, match="newsletter copy rejected: it offers"):
        ge.draft_newsletter(get_restaurant(rid, db_path=db), goal="Fill Tuesday lunch")
    _model(monkeypatch, json.dumps({**GOOD, "body": "Enjoy 20% off all week."}))
    assert "20% off" in ge.draft_newsletter(get_restaurant(rid, db_path=db), goal="20% off all week")["body"]
    _model(monkeypatch, "Sorry, I can't.")
    with pytest.raises(ValueError, match="unreadable"):
        ge.draft_newsletter(get_restaurant(rid, db_path=db), goal="Fill Tuesday lunch")


# ── the email itself, and its preview ──────────────────────────────────────

def test_the_preview_is_the_restaurants_email_as_sent(db):
    rid = _rid(db, name="Simple EJ's")
    update_restaurant(rid, {"mailing_address": "214 Main St"}, db_path=db)
    _guest(db, rid, 1)
    mid = _media(db, rid)
    out = ge.preview(rid, subject="Pull up a chair", body="The kitchen is on.\n\nCome by.",
                     design={"headline": "Come see us", "preheader": "Ready this week", "button_label": "Book a table",
                             "button_url": "https://ej.test/book", "image_media_id": mid}, base="https://dash.test", db_path=db)
    h = out["html"]
    assert out["ok"] and out["subject"] == "Pull up a chair" and out["body"] == "The kitchen is on.\n\nCome by."
    # The greeting is a stand-in, never a real guest's name (CS-19).
    assert "SIMPLE EJ" in h.upper() and "Come see us" in h and "Hi Alex —" in h and "Guest1" not in h
    assert 'href="https://ej.test/book"' in h and ">Book a table</a>" in h
    assert f'src="https://dash.test/m/tok{rid}.jpg"' in h
    assert "Unsubscribe" in h and "214 Main St" in h and "Ready this week" in h
    assert "wordmark" not in h                                         # the restaurant's email, not Cavnar AI's
    assert 'name="color-scheme" content="light"' in h                  # email is light only
    bare = ge.preview(rid, subject="S", body="Hi", design={"button_label": "Book"}, base="https://dash.test", db_path=db)["html"]
    assert "Book</a>" not in bare                                      # no link, no button


def test_the_frame_takes_its_colours_from_brand():
    src = open("emails.py", encoding="utf-8").read()
    frame = src[src.index("def guest_newsletter_email("):src.index("def _send_branded(")]
    assert "max-width:560px" in frame and 'B["ember"]' in frame and "#c84b2f" not in frame


# ── the send: audience, look, message ids ──────────────────────────────────

def _address(db, rid):
    update_restaurant(rid, {"mailing_address": "214 Main St, Springfield"}, db_path=db)


def test_an_email_goes_to_the_audience_a_text_would(db, monkeypatch):
    rid = _rid(db)
    _address(db, rid)
    _guest(db, rid, 1, visits=4)                  # a regular
    _guest(db, rid, 2, visits=1)
    _guest(db, rid, 3, visits=5, email=False)     # a regular with no email
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda payload, **kw: sent.append(payload) or emails.SendResult(True, message_id=f"m{len(sent)}"))
    out = ge.send_newsletter(rid, "Thanks for coming in.", subject="Thank you", segment="regulars",
                             design={"headline": "The best part of our week", "button_label": "See the menu",
                                     "button_url": "https://ej.test/menu"}, db_path=db)
    assert out["ok"] and out["total"] == 1 and out["segment"] == "regulars"
    assert sent[0]["to"] == ["g1@example.test"] and "The best part of our week" in sent[0]["html"] and "See the menu</a>" in sent[0]["html"]
    c = get_conn(db)
    row = c.execute("SELECT design, segment, segment_label FROM guest_newsletters WHERE id=?", (out["newsletter_id"],)).fetchone()
    mid = c.execute("SELECT message_id FROM guest_newsletter_recipients WHERE newsletter_id=?", (out["newsletter_id"],)).fetchone()[0]
    c.close()
    assert json.loads(row["design"])["headline"] == "The best part of our week" and row["segment_label"] == "Regulars (3+ visits)"
    assert mid == "m1"                                                  # opens and clicks can find it
    again = ge.send_newsletter(rid, "Thanks for coming in.", subject="Thank you", segment="regulars",
                               design={"headline": "The best part of our week", "button_label": "See the menu",
                                       "button_url": "https://ej.test/menu"}, db_path=db)
    assert again["newsletter_id"] == out["newsletter_id"] and len(sent) == 1   # the same press resumes
    assert again["ok"] is False and again["already_sent"] is True               # and says it mailed nobody (CS-15)
    other = ge.send_newsletter(rid, "Thanks for coming in.", subject="Thank you", segment="all", db_path=db)
    assert other["newsletter_id"] != out["newsletter_id"]


def test_an_empty_audience_says_so(db):
    rid = _rid(db)
    _address(db, rid)
    _guest(db, rid, 1, visits=1)
    out = ge.send_newsletter(rid, "Hi", subject="S", segment="regulars", db_path=db)
    assert not out["ok"] and out["error"] == "Nobody in that audience is on your email list yet."


def test_opens_are_as_recorded_and_unknown_without_tracking(db, monkeypatch):
    rid = _rid(db)
    _address(db, rid)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    for i in range(4):
        _guest(db, rid, i)
    ids = iter(range(100))
    monkeypatch.setattr(emails, "deliver", lambda payload, **kw: emails.SendResult(True, message_id=f"msg{next(ids)}"))
    ge.send_newsletter(rid, "Hi", subject="S", db_path=db)
    ge.run_newsletter_sends(db_path=db)             # three go inline now (CS-18); the tick sends the rest
    item = ge.newsletter_history(rid, db_path=db)[0]
    assert item["sent"] == 4 and item["opened"] is None and item["clicked"] is None     # nothing reports opens yet
    c = get_conn(db)
    for i in range(4):
        c.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, message_id, opened_at, clicked_at) "
                  "VALUES (?,?,?,?,?,?)", (rid, "guest_newsletter", "x", f"msg{i}",
                                           "2026-09-28 10:00:00" if i < 3 else None, "2026-09-28 10:00:00" if i == 0 else None))
    c.commit()
    c.close()
    item = ge.newsletter_history(rid, db_path=db)[0]
    assert (item["opened"], item["clicked"]) == (3, 1)


# ── the page's figures ──────────────────────────────────────────────────────

def test_the_overview_carries_email_reach_and_measured_insights(db, monkeypatch):
    rid = _rid(db)
    for i in range(3):
        _guest(db, rid, i, email=i < 2)
    c = get_conn(db)
    for sent, back, ts in ((40, 4, "2026-09-01 18:00:00"), (60, 9, "2026-09-10 18:00:00")):
        c.execute("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, failed_count, created_at, segment, "
                  "segment_label, visits_matched) VALUES (?,?,?,0,?,?,?,?)", (rid, "Hi", sent, ts, "regulars", "Regulars (3+ visits)", back))
    c.commit()
    c.close()
    ov = gm.campaign_overview(rid, db_path=db)
    assert ov["email_subscribers"] == 2 and ov["mailing_address_set"] is False
    assert ov["insights"] == [{"kind": "back", "figure": "13%", "tone": "good",
                               "text": "came back after a text to regulars (3+ visits)",
                               "basis": "2 campaigns · a visit within 14 days, matched in your POS"}]
    assert ov["last_campaign"]["channel"] == "text"


def test_one_campaign_per_audience_is_no_insight(db):
    rid = _rid(db)
    c = get_conn(db)
    c.execute("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, failed_count, segment, visits_matched) "
              "VALUES (?,?,40,0,'regulars',9)", (rid, "Hi"))
    c.commit()
    c.close()
    assert gm.campaign_insights(rid, db_path=db) == []


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(db, monkeypatch):
    init_auth(db_path=db)
    real = models.get_conn
    for mod in (auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db))
    auth_routes._login_attempts.clear()
    app = Flask(__name__)
    app.register_blueprint(mobile_bp)
    return app.test_client()


def _login(client, db):
    rid = _rid(db)
    create_user(rid, "owner", "owner@x.test", "correct-horse", db_path=db)
    tok = client.post("/mobile/api/login", json={"username": "owner", "password": "correct-horse"}).get_json()["token"]
    return rid, {"Authorization": f"Bearer {tok}"}


def test_the_draft_route_returns_the_email_and_the_plan(client, db, monkeypatch):
    _, h = _login(client, db)
    monkeypatch.setattr(ge, "draft_newsletter", lambda r, goal="", topic="": {
        "subject": "S", "preheader": "P", "headline": "H", "body": "B", "button_label": "Book"})
    d = client.post("/mobile/api/guest-newsletter/draft", json={"prompt": "Thank our regulars"}, headers=h).get_json()
    assert d["ok"] and d["subject"] == "S" and d["segment"] == "regulars" and d["goal"] == "Thank your regulars"
    assert client.post("/mobile/api/guest-newsletter/draft", json={}, headers=h).status_code == 400

    def refuse(r, goal="", topic=""):
        raise ValueError("newsletter copy rejected: award or ranking claim ('famous')")
    monkeypatch.setattr(ge, "draft_newsletter", refuse)
    r = client.post("/mobile/api/guest-newsletter/draft", json={"prompt": "Fill Tuesday"}, headers=h)
    assert r.status_code == 422 and "award or ranking claim" in r.get_json()["error"]


def test_the_send_route_passes_the_look_and_the_audience(client, db, monkeypatch):
    _, h = _login(client, db)
    got = {}
    monkeypatch.setattr(ge, "send_newsletter", lambda rid, body, **kw: got.update(kw, body=body) or {"ok": True})
    client.post("/mobile/api/guest-newsletter", json={"body": "Hi", "subject": "S", "segment": "lapsed_30",
                                                     "design": {"headline": "H"}}, headers=h)
    assert got["segment"] == "lapsed_30" and got["design"] == {"headline": "H"} and got["body"] == "Hi"


def test_segments_count_the_email_list_and_history_resolves_photos(client, db):
    rid, h = _login(client, db)
    _guest(db, rid, 1, visits=4)
    _guest(db, rid, 2, visits=4, email=False)
    segs = {s["key"]: s for s in client.get("/mobile/api/guest-segments", headers=h).get_json()["segments"]}
    assert (segs["regulars"]["count"], segs["regulars"]["email_count"]) == (2, 1)
    assert client.get("/mobile/api/guest-newsletters", headers=h).get_json() == {"ok": True, "newsletters": []}
    prev = client.post("/mobile/api/guest-newsletter/preview", json={"subject": "S", "body": "Hi"}, headers=h).get_json()
    assert prev["ok"] and "<html" in prev["html"]


def test_every_new_route_has_its_web_twin():
    src = open("client_api.py", encoding="utf-8").read()
    for path, body in (("/api/guest-newsletter/draft", "mobile_guest_newsletter_draft"),
                       ("/api/guest-newsletter/preview", "mobile_guest_newsletter_preview"),
                       ("/api/guest-newsletters", "mobile_guest_newsletters")):
        i = src.index(f'@client_bp.route("{path}"')
        assert f'_m("{body}")' in src[i:i + 400], path


# ── the page ────────────────────────────────────────────────────────────────

def test_the_email_box_left_content_for_the_studio():
    content = _between('<div id="mkt-tab-content"', "<!-- /mkt-tab-content -->")
    assert 'id="mkt-newsletter"' not in SRC and "guest-newsletter-btn" not in SRC and "function sendGuestNewsletter" not in SRC
    assert 'id="guest-newsletter-body"' not in content
    studio = _between('<div class="cp-builder" id="cp-builder" hidden>', '<section class="cp-sec" aria-labelledby="cp-aud-h">')
    for el in ('id="cp-ch-text"', 'id="cp-ch-email"', 'id="cp-ch-social"', 'id="guest-newsletter-subject"',
               'id="guest-newsletter-body"', 'id="cp-em-frame"', 'id="cp-step-photo"', 'id="guest-send-btn"',
               'data-nav="marketing/newsletter"'):
        assert el in studio, el
    assert 'sandbox="allow-same-origin"' in studio and "allow-scripts" not in studio
    nl = SRC[SRC.index("function mktSendAsNewsletter(){"):]
    nl = nl[:nl.index("\n}\n")]
    assert "cpUseEmailText(text)" in nl and "switchMktTab('campaigns')" in nl and "fetch(" not in nl


def test_one_goal_drafts_every_channel_that_is_on():
    create = _between("window.cpCreate = function(rewrite) {", "function cpCreateIdle()")
    assert "chans.forEach(function(c) { cpDraft(c, prompt, true); });" in create
    draft = _between("function cpDraft(k, prompt, plan) {", "window.cpRewrite = function(k) {")
    assert "'/api/guest-campaign/draft'" in draft and "'/api/guest-newsletter/draft'" in draft and "'/api/generate-content'" in draft
    assert "if (plan && !_cp.planned && d.segment)" in draft                  # the first plan sets the audience once
    pv = _between("function cpEmailPreview() {", "function cpFitFrame()")
    assert "'/api/guest-newsletter/preview'" in pv and "f.srcdoc = d.html" in pv and "seq !== _cp.pvSeq" in pv


def test_the_send_is_two_presses_and_names_what_goes():
    paint = _between("window.cpPaint = function", "function cpPaintAi")
    assert "label.push('Text ' + n)" in paint and "label.push('Email ' + m)" in paint and "label.push('Post to '" in paint
    assert "'Tap again to ' + (people ? 'reach ' + _cpPlural(people, 'guest') : '')" in paint
    send = _between("window.sendGuestCampaign = function(btn) {", "// A changed draft or audience is a new send")
    assert send.index("if (!_cp.armed) {") < send.index("fetch(")
    assert "'/api/guest-newsletter', body" in send and "segment: _cp.seg" in send and "design: cpDesign()" in send
    assert "'/api/post-to-instagram'" in send and "image_url: _cp.photo ? _cp.photo.url : ''" in send


def test_opens_show_as_recorded_and_history_holds_both_channels():
    hist = _between("function cpPaintHistory() {", "\n}\n")
    # Recorded, never "at least" (CS-7): Apple Mail auto-opens push opens up.
    assert "bar(es, 'Opens recorded', c.opened, 'var(--ember2)', 'not tracked')" in hist
    assert "≥" not in hist and "at least" not in hist
    assert "data-cp-reuse=" in hist and "data-cp-improve=" in hist
    assert "'/api/guest-newsletters'" in SRC
