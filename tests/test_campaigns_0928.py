"""Campaigns, 9/28/26: the Guest Text Club rebuilt as Marketing's Campaigns
tab. One prompt plans the text (plan_campaign: tone, audience, the day it
fills); campaign_overview gives the page only measured figures, a rate only
once it rests on enough campaigns; the join link is the signed one; drafts
take the owner's goal as a goal and never state a cause; the send is a
two-press button that names the head count."""
import re
import types

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import guest_marketing as gm
import mobile_api
import models
from auth import create_user, init_auth
from guest_links import verify_join
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_conn, get_restaurant

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


# ── plan_campaign: the prompt's tone, audience and day ──────────────────────

@pytest.mark.parametrize("prompt,ctype,segment,day", [
    ("Bring back guests who haven't visited in 30 days", "win_back", "lapsed_30", None),
    ("Win back people we haven't seen in a couple of months", "win_back", "lapsed_60", None),
    ("Thank our regulars for coming back", "loyalty", "regulars", None),
    ("Welcome our first-time guests back", "loyalty", "new", None),
    ("Fill Thursday dinner", "event", "all", "Thursday"),
    ("Promote this week's special", "event", "all", None),
    ("Say hello", "general", "all", None),
])
def test_a_prompt_plans_the_tone_audience_and_day(prompt, ctype, segment, day):
    plan = gm.plan_campaign(prompt)
    assert (plan["type"], plan["segment"], plan["target_day"]) == (ctype, segment, day)
    assert plan["type"] in gm.CAMPAIGN_PROMPTS and plan["segment"] in gm.SEGMENTS and plan["goal"]


# ── campaign_overview: measured, or nothing ─────────────────────────────────

@pytest.fixture
def rid(db_path, monkeypatch):
    gm.init_guest_marketing(db_path=db_path)
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    return create_restaurant(Restaurant(name="Camp Co", owner_email="c@x.test", module_marketing=1), db_path=db_path)


def _campaign(db_path, rid, sent, *, clicks=None, back=None, failed=0, when="2026-09-20 18:00:00"):
    c = get_conn(db_path)
    tok = None
    if clicks is not None:
        tok = f"t{sent}{clicks}{when[-8:-6]}"
        c.execute("INSERT INTO marketing_links (restaurant_id, token, target_url, clicks) VALUES (?,?,?,?)",
                  (rid, tok, "https://x.test", clicks))
    c.execute("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, failed_count, created_at, segment, "
              "segment_label, link_token, visits_matched) VALUES (?,?,?,?,?,?,?,?,?)",
              (rid, "Hi", sent, failed, when, "all", "Everyone consented", tok, back))
    c.commit()
    c.close()


def test_an_empty_restaurant_has_counts_and_no_rates(rid, db_path):
    ov = gm.campaign_overview(rid, db_path=db_path)
    assert ov["subscribers"] == 0 and ov["today"] == 0 and ov["last_30"] == 0
    assert len(ov["weekly"]) == 12 and all(w["joined"] == 0 for w in ov["weekly"])
    assert ov["last_campaign"] is None and ov["tap_rate"] is None and ov["back_rate"] is None
    assert ov["delivered"] is None and ov["rate_min"] == gm.CAMPAIGN_RATE_MIN
    assert "open_rate" not in ov and "revenue" not in ov          # SMS reports no opens; nothing is money


def test_only_consented_guests_count_and_a_join_lands_this_week(rid, db_path):
    gm.add_guest_contact_public_optin(rid, "555-111-2222", name="A", db_path=db_path)
    gm.add_guest_contact_public_optin(rid, "555-111-3333", name="B", db_path=db_path)
    gm.add_guest_contact_manual(rid, "555-111-4444", name="Walk-in", db_path=db_path)
    ov = gm.campaign_overview(rid, db_path=db_path)
    assert ov["subscribers"] == 2 and ov["today"] == 2 and ov["last_30"] == 2
    assert ov["weekly"][-1]["joined"] == 2


def test_a_rate_waits_for_enough_campaigns_of_enough_texts(rid, db_path):
    _campaign(db_path, rid, 40, clicks=4, back=2)
    _campaign(db_path, rid, 5, clicks=5, back=5, when="2026-09-21 18:00:00")    # too small to count
    ov = gm.campaign_overview(rid, db_path=db_path)
    assert ov["tap_rate"] is None and ov["back_rate"] is None                      # one campaign is not a rate
    assert ov["last_campaign"]["sent"] == 5

    _campaign(db_path, rid, 60, clicks=12, back=3, when="2026-09-22 18:00:00")
    ov = gm.campaign_overview(rid, db_path=db_path)
    assert ov["tap_rate"] == {"pct": 16.0, "campaigns": 2}                         # (4+12)/(40+60)
    assert ov["back_rate"] == {"pct": 5.0, "campaigns": 2}                         # (2+3)/(40+60)


def test_a_campaign_without_a_link_is_not_a_zero_tap_rate(rid, db_path):
    _campaign(db_path, rid, 40, clicks=None, back=None)
    _campaign(db_path, rid, 40, clicks=None, back=None, when="2026-09-22 18:00:00")
    ov = gm.campaign_overview(rid, db_path=db_path)
    assert ov["tap_rate"] is None and ov["back_rate"] is None


# ── the routes: overview with the signed join link, the draft from a prompt ──

@pytest.fixture
def client(db_path, monkeypatch):
    init_auth(db_path=db_path)
    gm.init_guest_marketing(db_path)
    real = models.get_conn
    for mod in (models, auth, auth_routes, client_api, mobile_api, gm):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path))
    auth_routes._login_attempts.clear()
    app = Flask(__name__)
    app.register_blueprint(mobile_bp)
    return app.test_client()


def _token(client, db_path, module_marketing=1):
    r = create_restaurant(Restaurant(name="Route Co", owner_email="r@x.test", module_marketing=module_marketing),
                          db_path=db_path)
    create_user(r, "owner", "owner@x.test", "correct-horse", db_path=db_path)
    tok = client.post("/mobile/api/login", json={"username": "owner", "password": "correct-horse"}).get_json()["token"]
    return r, {"Authorization": f"Bearer {tok}"}


def test_the_overview_carries_a_join_link_that_verifies(client, db_path):
    r, h = _token(client, db_path)
    d = client.get("/mobile/api/guest-overview", headers=h).get_json()
    assert d["ok"] is True and d["subscribers"] == 0 and d["receipt_hint"]
    token = d["join_url"].rsplit("/join/", 1)[1]
    assert token != str(r) and verify_join(token) == r                 # the bare /join/<id> is refused


def test_the_overview_needs_the_marketing_module(client, db_path):
    _, h = _token(client, db_path, module_marketing=0)
    assert client.get("/mobile/api/guest-overview", headers=h).status_code == 403


def test_the_web_twin_delegates():
    src = open("client_api.py", encoding="utf-8").read()
    i = src.index('@client_bp.route("/api/guest-overview")')
    assert '_m("mobile_guest_overview")' in src[i:i + 300]


def test_a_prompt_drafts_as_a_goal_with_its_audience(client, db_path, monkeypatch):
    _, h = _token(client, db_path)
    seen = {}

    def fake(restaurant, campaign_type="general", topic="", goal=""):
        seen.update(type=campaign_type, topic=topic, goal=goal)
        return "See you Thursday."
    monkeypatch.setattr(gm, "draft_campaign_message", fake)
    d = client.post("/mobile/api/guest-campaign/draft", json={"prompt": "Fill Thursday dinner"}, headers=h).get_json()
    assert d["ok"] and d["message"] == "See you Thursday."
    assert (d["type"], d["segment"], d["target_day"], d["goal"]) == ("event", "all", "Thursday", "Fill Thursday")
    assert seen == {"type": "event", "topic": "", "goal": "Fill Thursday dinner"}

    # Rewrite sends the type it already has; that type wins over the plan's.
    d = client.post("/mobile/api/guest-campaign/draft", json={"prompt": "Fill Thursday dinner", "type": "loyalty"},
                    headers=h).get_json()
    assert d["type"] == "loyalty" and seen["type"] == "loyalty"


# ── the draft itself ────────────────────────────────────────────────────────

def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def test_the_draft_prompt_carries_the_goal_and_asks_for_no_cause_and_plain_punctuation(rid, db_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(gm, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"]) and _msg("See you soon."))
    gm.draft_campaign_message(get_restaurant(rid), campaign_type="loyalty", goal="Thank our regulars")
    p = seen["p"]
    assert "What the owner wants this text to do, in their words: Thank our regulars." in p
    assert "Topic/specifics to include" not in p
    assert "Give no reason or cause for anything" in p and "no em dashes, curly quotes" in p


def test_an_offer_in_the_owners_goal_is_theirs(rid, db_path, monkeypatch):
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(gm, "create_with_retry", lambda client, **kw: _msg("Half-price apps till 6 tonight"))
    out = gm.draft_campaign_message(get_restaurant(rid), goal="half-price apps till 6 on Tuesdays")
    assert out.startswith("Half-price")


def test_the_win_back_copy_fits_one_plain_text():
    msg = gm._winback_message("Simple EJ's")
    assert "—" not in msg and "’" not in msg
    assert len(msg + "\n\nReply STOP to unsubscribe.") <= 160


# ── the page ────────────────────────────────────────────────────────────────

def test_campaigns_is_its_own_marketing_tab():
    tabs = _between('id="mkt-tab-content-btn"', '</div>')
    assert tabs.index('id="mkt-tab-campaigns-btn"') < tabs.index('id="mkt-tab-queue-btn"')
    panel = _between('<div id="mkt-tab-campaigns"', '<!-- /mkt-tab-campaigns -->')
    assert 'data-nav="marketing/guests"' in panel and 'id="mkt-guests"' in panel
    content = _between('<div id="mkt-tab-content"', '<!-- /mkt-tab-content -->')
    assert "mkt-guests" not in content and "Guest Text Club" not in SRC
    sw = SRC[SRC.index("window.switchMktTab = function(tab) {"):]
    sw = sw[:sw.index("\n}\n")]
    assert "'campaigns'" in sw and "loadGuestOverview()" in sw


def test_the_old_composer_is_gone():
    for gone in ("generateGuestCampaign", 'id="guest-campaign-type"', 'id="guest-segment"',
                 "confirm('Text ' + who"):
        assert gone not in SRC, gone


def test_the_page_has_its_four_sections_and_the_kpis():
    panel = _between('<div id="mkt-tab-campaigns"', '<!-- /mkt-tab-campaigns -->')
    for sec in ("Create", "Audience", "Performance", "Settings"):
        assert f'<div class="k">{sec}</div>' in panel or f'>{sec}<' in panel, sec
    for k in ("cp-k-subs", "cp-k-growth", "cp-k-last", "cp-k-tap", "cp-k-back"):
        assert f'id="{k}"' in panel, k
    assert 'id="cp-prompt"' in panel and 'id="guest-send-btn"' in panel and 'id="guest-join-link"' in panel
    # SMS reports no opens and no campaign is tied to money: neither is a figure here.
    shown = re.sub(r'title="[^"]*"', "", panel).lower()
    assert "open rate" not in shown and "revenue" not in shown


def test_the_copy_link_is_the_signed_one():
    # It copied '/join/' + the restaurant id, which verify_join refuses: every
    # copied link was a 404. It copies what the overview signed.
    fn = SRC[SRC.index("window.copyGuestJoinLink = function() {"):]
    fn = fn[:fn.index("\n};\n")]
    assert "getElementById('guest-join-link')" in fn and "'/join/'" not in fn
    ov = SRC[SRC.index("function loadGuestOverview"):]
    ov = ov[:ov.index("\n}\n")]
    assert "d.join_url" in ov and "fetch('/api/guest-overview'" in ov


def test_a_forecast_is_measured_or_a_dash():
    paint = _between("window.cpPaint = function", "function cpPaintAi")
    assert "if (!rate) { v.textContent = '—';" in paint
    assert "'after ' + (ov.rate_min || 2) + ' campaigns'" in paint


def test_the_counter_knows_a_unicode_text_is_shorter():
    paint = _between("window.cpPaint = function", "function cpPaintAi")
    assert "var one = uni ? 70 : 160, per = uni ? 67 : 153;" in paint
    assert "+ (link ? 26 : 0) + 29" in paint                            # the link and the STOP line count


def test_nav_to_the_section_opens_its_sub_tab():
    nav = SRC[SRC.index("function cavNavSection"):]
    nav = nav[:nav.index("\n}\n")]
    assert "[id^=\"mkt-tab-\"]" in nav or "[id^='mkt-tab-']" in nav


def test_layout_details():
    assert ".cp-step.done .n{font-size:0;" in SRC                       # a done step shows its tick alone
    assert ".cp-kpis>:last-child{grid-column:1/-1}" in SRC               # no orphan KPI on a phone
    assert ".cp-kpis>:nth-child(n+4){grid-column:span 3}" in SRC
