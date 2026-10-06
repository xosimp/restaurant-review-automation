"""Marketing audit fix slice E1 (9/28/26): the AI guards on everything the
public reads, and the Content tab's routing.

AUX-1 / #16 / #17  one public-copy guard set - one offer vocabulary for every
                   public surface, and on the marketing surfaces no price,
                   time, date, event, new/back/better/changed claim, link or
                   sign-off the owner's own words don't carry; a calendar
                   angle is never an offer source (AI-2).
AUX-2 / #15        Ask's guest-text and publish proposals run it before the
                   owner sees Send; the guest text names its audience.
AUX-4 / #19        the Content tab is social-only; texts and emails open in
                   the Campaign Studio (web) and never post (iOS too).
AUX-5 / #38        the quiet-night job drafts no dead-end guest text; an old
                   guest_sms draft opens the Studio and cannot be approved.
AUX-15 / #88       a food-cost figure never reaches a public topic.
AUX-16 / AUX-17    no unmeasured "popular" badge, no assumed happy-hour
                   hours, no stale "TWO versions" comment; the calendar CSV
                   is RFC 4180.

No model, SMS, email, post or push is reached: model calls are stubbed.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import types
from datetime import datetime

import pytest

import models
import response_validation as rv
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


@pytest.fixture
def db(db_path, monkeypatch):
    import guest_email
    import guest_marketing
    import client_api
    import marketing_drafts
    import marketing_publish
    real = models.get_conn
    # marketing_drafts / marketing_publish bind get_conn at import: whichever
    # test imported them first decided where they write, so each is pointed
    # here explicitly (CLAUDE.md, "Bound imports").
    for mod in (models, client_api, guest_email, guest_marketing, marketing_drafts, marketing_publish):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    guest_marketing.init_guest_marketing(db_path=db_path)
    return db_path


def _restaurant(db, **cols):
    rid = create_restaurant(Restaurant(name=cols.pop("name", "Gia Mia"), owner_email="o@x.test"), db_path=db)
    cols.setdefault("billing_status", "active")
    cols.setdefault("module_marketing", 1)
    update_restaurant(rid, cols, db_path=db)
    return rid


# The three offers the audit's scenario names, each with the words an owner
# would type to run it and a public line a model might write.
OFFERS = [
    ("half price", "Half-price apps Tuesday", "Half-price apps Tuesday! See you there."),
    ("dollars off", "$5 off pizzas tonight", "$5 off every pizza tonight."),
    ("2-for-1", "2-for-1 burgers", "2-for-1 burgers all night."),
]
IDS = [o[0] for o in OFFERS]


# ── AUX-1 / #16: one offer vocabulary, every public surface ──────────────────

@pytest.mark.parametrize("surface", ["social_post", "calendar_idea", "guest_sms", "reply_public"])
@pytest.mark.parametrize("_name,owner,copy", OFFERS, ids=IDS)
def test_every_public_surface_refuses_an_offer_only_the_owner_can_make(surface, _name, owner, copy):
    v = rv.validate(copy, rv.ValidationContext(surface=surface, offer_source="wood-fired pizza"))
    assert v.verdict == "refuse" and any(f["detail"] == rv.OFFER_LABEL for f in v.findings), v.findings
    ok = rv.validate(copy, rv.ValidationContext(surface=surface, offer_source=owner))
    assert not any(f["detail"] == rv.OFFER_LABEL for f in ok.findings), ok.findings
    if surface != "reply_public":
        assert ok.verdict == "pass", ok.findings


@pytest.mark.parametrize("copy", [
    "Half price wine all night", "half-off apps", "50% off desserts", "$5 off", "5 dollars off", "20 percent off",
    "2 for 1 tacos", "two-for-one tacos", "BOGO wings", "buy one, get one free", "a discount for teachers",
    "discounted drinks", "happy hour deals", "free desserts", "free dessert", "complimentary bread",
    "kids eat free", "dinner's on us", "it's on the house", "bottomless mimosas", "enter our giveaway",
    "use promo code FALL"])
def test_the_one_vocabulary_names_every_offer_shape_including_plurals(copy):
    assert rv.invented_offers(copy, ""), copy


@pytest.mark.parametrize("copy", ["Feel free to stop by", "gluten free pasta", "free-range eggs", "dinner for two",
                                  "table for 2 at 7", "free time"])
def test_words_that_only_look_like_an_offer_are_left_alone(copy):
    assert rv.invented_offers(copy, "") == []


def test_another_phrasing_of_the_owners_offer_is_the_same_offer_and_a_different_one_is_not():
    assert rv.invented_offers("50% off wine tonight", "half-price wine") == []
    assert rv.invented_offers("Two for one burgers", "BOGO burgers") == []
    assert rv.invented_offers("complimentary desserts", "free dessert on Tuesdays") == []
    assert rv.invented_offers("30% off apps", "20% off apps") == ["30% off"]
    assert rv.invented_offers("$6 off", "$5 off pizzas") == ["$6 off"]
    assert rv.invented_offers("free drinks", "free dessert on Tuesdays") == ["free drinks"]
    # A giveaway is its own thing for free, and nothing else (Copper Table, 10/6/26)
    assert rv.invented_offers("Free pumpkins while they last!", "bring guests in for a pumpkin giveaway") == []
    assert rv.invented_offers("Free drinks too", "bring guests in for a pumpkin giveaway") == ["Free drinks too"]
    assert rv.invented_offers("Everything is free", "bring guests in for a pumpkin giveaway") == ["is free"]


def test_guest_marketing_reads_the_shared_vocabulary():
    """The guest text's and the newsletter's invented_offers is the same list
    (it used to miss plurals; the social post's missed half price, $5 off and
    2-for-1)."""
    import guest_marketing as gm
    assert not hasattr(gm, "_EXTRA_OFFER_RE")
    for _n, owner, copy in OFFERS:
        assert gm.invented_offers(copy, "") and gm.invented_offers(copy, owner) == []
    assert gm.invented_offers("Free desserts this weekend!", "") == ["Free desserts"]


# ── AUX-1: the same refusal on every drafter that writes public copy ─────────

def _content(monkeypatch, text, seen=None):
    import marketing
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())

    def create(*a, **kw):
        if seen is not None:
            seen["prompt"] = kw["messages"][0]["content"]
        return _msg(text)
    monkeypatch.setattr(marketing, "create_with_retry", create)
    monkeypatch.setattr(marketing, "extract_text", lambda m: m.content[0].text)
    monkeypatch.setattr(marketing, "log_content", lambda *a, **k: None)


@pytest.mark.parametrize("_name,owner,copy", OFFERS, ids=IDS)
def test_the_content_generator_refuses_it_unless_the_owner_typed_it(db, monkeypatch, _name, owner, copy):
    import marketing
    rid = _restaurant(db)
    _content(monkeypatch, copy)
    with pytest.raises(ValueError, match="marketing copy rejected"):
        marketing.generate_content("instagram_post", "fall menu", restaurant_id=rid)
    assert marketing.generate_content("instagram_post", owner, restaurant_id=rid).startswith(copy[:6])
    # A calendar angle (or a job's topic) carrying the words is not the owner
    # saying so (AI-2).
    with pytest.raises(ValueError, match="marketing copy rejected"):
        marketing.generate_content("instagram_post", owner, restaurant_id=rid, topic_is_owner=False)


@pytest.mark.parametrize("_name,owner,copy", OFFERS, ids=IDS)
def test_the_newsletter_refuses_it_unless_the_owner_typed_it(db, monkeypatch, _name, owner, copy):
    import guest_email as ge
    rid = _restaurant(db)
    body = {"subject": "Pull up a chair", "preheader": "Your table is ready", "headline": "Come see us",
            "body": copy, "button": "Book a table"}
    monkeypatch.setattr(ge, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ge, "create_with_retry", lambda *a, **k: _msg(json.dumps(body)))
    with pytest.raises(ValueError, match="newsletter copy rejected"):
        ge.draft_newsletter(get_restaurant(rid, db_path=db), goal="Fill Tuesday lunch")
    assert ge.draft_newsletter(get_restaurant(rid, db_path=db), goal=owner)["body"] == copy


@pytest.mark.parametrize("_name,owner,copy", OFFERS, ids=IDS)
def test_the_guest_text_refuses_it_unless_the_owner_typed_it(db, monkeypatch, _name, owner, copy):
    import guest_marketing as gm
    rid = _restaurant(db)
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(gm, "create_with_retry", lambda *a, **k: _msg(copy))
    with pytest.raises(ValueError, match="campaign copy rejected"):
        gm.draft_campaign_message(get_restaurant(rid, db_path=db), campaign_type="general", goal="Fill Tuesday")
    assert gm.draft_campaign_message(get_restaurant(rid, db_path=db), campaign_type="general", goal=owner) == copy


@pytest.mark.parametrize("_name,owner,copy", OFFERS, ids=IDS)
def test_ask_proposals_refuse_it_unless_the_owner_said_it(db, monkeypatch, _name, owner, copy):
    import ask_cavnar_tools as t
    rid = _restaurant(db)
    text = {"message": copy, "segment": "all"}
    assert t.build_proposal("send_guest_campaign", text, restaurant_id=rid) is None
    why = t.proposal_refusal("send_guest_campaign", text, restaurant_id=rid)
    assert why and "can't go out as written" in why and "owner's own words" in why
    p = t.build_proposal("send_guest_campaign", text, restaurant_id=rid, owner_words=f"text everyone: {owner}")
    assert p and p["preview"] == copy
    cap = {"caption": copy}
    assert t.build_proposal("publish_instagram_post", cap, restaurant_id=rid) is None
    assert t.build_proposal("publish_facebook_post", cap, restaurant_id=rid) is None
    assert t.build_proposal("publish_instagram_post", cap, restaurant_id=rid, owner_words=f"post: {owner}")


@pytest.mark.parametrize("_name,owner,copy", OFFERS, ids=IDS)
def test_calendar_ideas_carrying_it_are_dropped_unless_the_menu_says_it(db, monkeypatch, _name, owner, copy):
    import marketing
    week = [{"day": d, "platform": "Instagram & FB", "angle": copy if d == "Monday" else f"{d}: the wood-fired oven",
             "type": "instagram_post"}
            for d in ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")]
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **k: _msg(json.dumps(week)))
    monkeypatch.setattr(marketing, "extract_text", lambda m: m.content[0].text)
    rid = _restaurant(db)
    assert copy not in [i["angle"] for i in marketing.get_content_calendar_ideas(rid, force=True)]
    rid2 = _restaurant(db, name="Other Co", menu_notes=owner)
    assert copy in [i["angle"] for i in marketing.get_content_calendar_ideas(rid2, force=True)]


def test_a_week_checked_under_an_older_vocabulary_is_checked_again_on_read(db):
    import marketing
    rid = _restaurant(db)
    stale = {"day": "Monday", "angle": "2-for-1 burgers all night.", "type": "instagram_post",
             "validation": {"verdict": "pass", "caveats": [], "controls": True, "codes": [], "version": rv.VERSION}}
    marketing._cache_calendar(rid, [stale, {"day": "Tuesday", "angle": "The wood-fired oven", "type": "instagram_post"}])
    out = marketing.get_cached_calendar(rid)
    assert [i["angle"] for i in out] == ["The wood-fired oven"]
    assert out[0]["validation"]["public"] == rv.PUBLIC_COPY_VERSION


# ── AUX-1 / #17: prices, times, dates, events, "new/back", links ─────────────

@pytest.mark.parametrize("copy,owner,label", [
    ("Short rib, $18 tonight.", "short rib $18", rv.LABEL_PRICE),
    ("Trivia night Thursday - bring your crew.", "trivia thursdays", rv.LABEL_EVENT),
    ("Come by at 7pm Thursday.", "open at 7pm", rv.LABEL_TIME),
    ("See you 10/30 for the chili cook-off.", "chili cook-off 10/30", rv.LABEL_DATE),
    ("Our new fall menu is here.", "the new fall menu", rv.LABEL_CHANGE),
    ("The lasagna is back this week.", "lasagna back this week", rv.LABEL_CHANGE),
    ("Better than ever: our carbonara.", "carbonara, better than ever", rv.LABEL_CHANGE),
    ("Live music Saturday night.", "live jazz saturdays", rv.LABEL_EVENT),
    ("Book at www.elsewhere.test/win", "book at www.elsewhere.test/win", rv.LABEL_CONTACT),
    ("Call 312-555-0199 to book.", "call 312-555-0199", rv.LABEL_CONTACT),
])
def test_specifics_come_only_from_the_owners_words(copy, owner, label):
    for surface in ("social_post", "calendar_idea", "guest_sms"):
        v = rv.validate(copy, rv.ValidationContext(surface=surface, offer_source="wood-fired pizza"))
        assert v.verdict == "refuse", (surface, copy, v.findings)
        if surface != "guest_sms" or label != rv.LABEL_CONTACT:      # a text refuses any link on its own
            assert any(f["detail"] == label for f in v.findings), (surface, v.findings)
    ok = rv.validate(copy, rv.ValidationContext(surface="social_post", offer_source=owner))
    assert ok.verdict == "pass", (copy, ok.findings)


def test_a_holiday_date_the_system_gave_may_be_said_and_never_backs_an_offer():
    ctx = rv.ValidationContext(surface="social_post", offer_source="wood-fired pizza",
                               policy={"given_text": "Upcoming holidays: Halloween (Oct 31)"})
    assert rv.validate("Pizza for Halloween, Oct 31.", ctx).verdict == "pass"
    assert rv.validate("Pizza for Halloween, 10/31.", ctx).verdict == "pass"
    assert rv.validate("Halloween, Oct 31: free candy with every pizza.", ctx).verdict == "refuse"


def test_the_restaurants_own_website_is_a_link_the_owner_gave(db, monkeypatch):
    import marketing
    rid = _restaurant(db, menu_url="https://giamia.test/menu")
    _content(monkeypatch, "Wood-fired pizza tonight. Menu: giamia.test/menu")
    assert "giamia.test" in marketing.generate_content("instagram_post", "pizza", restaurant_id=rid)
    _content(monkeypatch, "Wood-fired pizza tonight. Menu: elsewhere.test/menu")
    with pytest.raises(ValueError, match="link or phone number"):
        marketing.generate_content("instagram_post", "pizza", restaurant_id=rid)


def test_the_generator_holds_figures_to_typed_facts_from_the_owners_words(db):
    """generate_content runs the engine with typed facts (#17): the owner's
    prices are facts, so the figure rules have something to hold a draft to."""
    import marketing
    rid = _restaurant(db, menu_notes="Short rib $28. Margherita $16.")
    ctx = marketing.marketing_context(rid, "social_post", topic="20% off apps")
    assert {(f.unit, f.value, f.kind) for f in ctx.facts} >= {("$", 28.0, "price"), ("$", 16.0, "price"),
                                                              ("%", 20.0, "price")}
    assert ctx.policy.get("context_facts") and "Short rib $28" in ctx.context_text
    assert rv.validate("Short rib, $28, all week.", ctx).verdict == "pass"
    assert rv.validate("Short rib, $24, all week.", ctx).verdict == "refuse"
    # A topic the owner did not type is not in the source at all.
    calendar = marketing.marketing_context(rid, "social_post", topic="20% off apps", topic_is_owner=False)
    assert "20% off" not in calendar.offer_source


# ── AUX-1: the prompts ────────────────────────────────────────────────────────

def test_the_weekly_email_signs_off_as_the_restaurant_never_an_invented_name(db, monkeypatch):
    import marketing
    rid = _restaurant(db, sign_off_name="Chef Ana")
    seen = {}
    _content(monkeypatch, "SUBJECT LINE: Fall is here\nBODY: The squash ravioli is on.\n— Chef Ana", seen)
    marketing.generate_content("weekly_email", "fall menu", restaurant_id=rid)
    assert '"— Chef Ana"' in seen["prompt"] and "Sarah" not in seen["prompt"]
    assert "Sarah" not in marketing.PROMPTS["weekly_email"]
    _content(monkeypatch, "SUBJECT LINE: Fall is here\nBODY: The squash ravioli is on.\n— Sarah")
    with pytest.raises(ValueError, match="signs off as someone nobody named"):
        marketing.generate_content("weekly_email", "fall menu", restaurant_id=rid)


def test_every_prompt_carries_the_public_rules_and_no_offer_nudge(db, monkeypatch):
    import marketing
    rid = _restaurant(db)
    assert "incentive" not in marketing.PROMPTS["loyalty_nudge"].lower()
    for ct in marketing.PROMPTS:
        seen = {}
        _content(monkeypatch, "Wood-fired pizza tonight.", seen)
        marketing.generate_content(ct, "pizza", restaurant_id=rid)
        assert marketing.PUBLIC_COPY_RULES in seen["prompt"], ct
        assert marketing.SUGGESTED_TOPIC_RULE not in seen["prompt"], ct
    seen = {}
    _content(monkeypatch, "Wood-fired pizza tonight.", seen)
    marketing.generate_content("instagram_post", "pizza", restaurant_id=rid, topic_is_owner=False)
    assert marketing.SUGGESTED_TOPIC_RULE in seen["prompt"]


def test_the_holiday_list_names_holidays_and_offers_nothing():
    import marketing
    nov = marketing.get_upcoming_holidays(datetime(2026, 11, 1))
    oct_ = marketing.get_upcoming_holidays(datetime(2026, 10, 20))
    assert "Veterans Day (Nov 11)" in nov and "Halloween (Oct 31)" in oct_
    for words in (nov, oct_):
        assert not re.search(r"free|discount|offer|special", words, re.I), words


def test_happy_hour_assumes_no_hours_and_the_badge_that_measured_nothing_is_gone():
    import marketing
    hh = next(c for c in marketing.CONTENT_TYPES if c["id"] == "happy_hour")
    assert not re.search(r"Mon|Thu|4-6|pm|deals", hh["description"])
    src = open(DASHBOARD, encoding="utf-8").read()
    assert ">popular</span>" not in src


# ── AUX-2 / #15: Ask's guest text names its audience ─────────────────────────

def test_the_guest_text_tool_requires_a_known_segment(db):
    import ask_cavnar_tools as t
    import guest_marketing as gm
    spec = t._BY_NAME["send_guest_campaign"]["spec"]["input_schema"]
    assert "segment" in spec["required"]
    assert tuple(spec["properties"]["segment"]["enum"]) == t.GUEST_SEGMENTS == tuple(gm.SEGMENTS)
    rid = _restaurant(db)
    for bad in ({"message": "Patio's open tonight!"}, {"message": "Patio's open tonight!", "segment": "vips"}):
        assert t.build_proposal("send_guest_campaign", bad, restaurant_id=rid) is None
        assert "which guests" in t.proposal_refusal("send_guest_campaign", bad, restaurant_id=rid)
    p = t.build_proposal("send_guest_campaign", {"message": "Patio's open tonight!", "segment": "lapsed_30"},
                         restaurant_id=rid)
    rec = next(d["value"] for d in p["details"] if d["label"] == "Recipients")
    assert rec.endswith(gm.SEGMENTS["lapsed_30"]["label"])
    shown = {f["key"]: f["value"] for f in p["fields_shown"]}
    assert shown["segment"] == gm.SEGMENTS["lapsed_30"]["label"] and p["body"]["segment"] == "lapsed_30"


def test_a_guest_text_over_a_texts_budget_or_that_would_be_reworded_is_not_proposed(db):
    import ask_cavnar_tools as t
    rid = _restaurant(db)
    long = {"message": "Come see us. " * 30, "segment": "all"}
    assert "at most 300" in t.proposal_refusal("send_guest_campaign", long, restaurant_id=rid)
    award = {"message": "Our award-winning lasagna is on tonight!", "segment": "all"}
    assert "can't go out as written" in t.proposal_refusal("send_guest_campaign", award, restaurant_id=rid)


def test_ask_passes_the_owners_question_as_the_offer_source():
    src = open(os.path.join(ROOT, "ask_cavnar.py"), encoding="utf-8").read()
    assert src.count("owner_words=(question or \"\")[:_MAX_QUESTION_LENGTH]") == 2


# ── AUX-4 / #19: the Content tab is social-only ──────────────────────────────

def test_every_content_type_names_its_channel():
    import marketing
    assert {c["id"]: c["channel"] for c in marketing.CONTENT_TYPES} == {
        "instagram_post": "social", "weekly_email": "email", "google_promo": "social", "loyalty_nudge": "text",
        "happy_hour": "social", "event_announcement": "social"}
    assert marketing.content_channel("guest_sms") == "text" and marketing.is_social_type("")
    assert not marketing.is_social_type("loyalty_nudge") and not marketing.is_social_type("weekly_email")


def test_a_text_or_email_is_never_scheduled_to_a_social_account(db):
    import marketing_publish
    rid = _restaurant(db)
    for ct in ("loyalty_nudge", "weekly_email", "guest_sms"):
        out = marketing_publish.schedule_post(rid, "facebook", "SUBJECT LINE: Hi\nBODY: x", "2099-01-01T10:00",
                                              content_type=ct, db_path=db)
        assert out["ok"] is False and "Campaigns" in out["error"], ct


def test_a_guest_text_draft_is_never_approved_it_opens_in_campaigns(db):
    import marketing_drafts
    rid = _restaurant(db)
    sms = marketing_drafts.save_draft(rid, "Come in Tuesday!", content_type="guest_sms",
                                      topic="Tuesday night guest text", db_path=db)["id"]
    out = marketing_drafts.approve_draft(sms, rid, user_id=1, role="client", db_path=db)
    assert out["ok"] is False and out["code"] == "send_from_campaigns" and out["channel"] == "text"
    c = sqlite3.connect(db)          # not a bound get_conn: another module may have swapped models.get_conn
    assert c.execute("SELECT status FROM marketing_drafts WHERE id=?", (sms,)).fetchone()[0] == "draft"
    c.close()
    post = marketing_drafts.save_draft(rid, "Come in!", content_type="instagram_post", db_path=db)["id"]
    assert marketing_drafts.approve_draft(post, rid, user_id=1, role="client", db_path=db)["ok"] is True


def test_ios_never_offers_a_social_post_for_a_text_or_email():
    src = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Features/Marketing/MarketingViewModel.swift"),
               encoding="utf-8").read()
    body = src[src.index("var canPostSomewhere: Bool {"):]
    body = body[:body.index("\n    }\n")]
    assert '["loyalty_nudge", "weekly_email", "guest_sms"].contains(selectedType)' in body
    assert "return false" in body


# The web Content tab, run under node against a small stub.

def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _fn(name):
    """One top-level `function name(...) {...}` out of dashboard.html."""
    s = _src()
    i = s.index("function " + name + "(")
    j = s.index("{", i)
    depth, k = 0, j
    while True:
        ch = s[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[i:k + 1]
        k += 1


def _channels_line():
    import jinja2
    import marketing
    line = next(ln for ln in _src().splitlines() if ln.startswith("var _MKT_CHANNEL = "))
    return jinja2.Template(line).render(ctypes=marketing.CONTENT_TYPES)


STUB = r"""
var window = globalThis, _calls = [], _toasts = [], _els = {}, _fetches = [];
function El(id){ this.id = id; this.value = ''; this.style = {}; this.innerHTML = ''; this.textContent = ''; }
El.prototype.focus = function(){}; El.prototype.scrollIntoView = function(){};
El.prototype.removeAttribute = function(){}; El.prototype.setAttribute = function(){};
El.prototype.getAttribute = function(){ return null; };
var document = {getElementById: function(id){ return _els[id] || null; },
  querySelectorAll: function(){ return []; }, querySelector: function(){ return null; },
  createElement: function(){ return {click: function(){ _calls.push(['click', this.download]); }}; }};
function toast(m, t){ _toasts.push([m, t]); }
function fetch(u){ _fetches.push(u); return {then: function(){ return {then: function(){ return {catch: function(){}}; }}; }}; }
function mktOppDraft(o){ _calls.push(['studio', o.action.channels, o.action.prompt]); }
function switchMktTab(t){ _calls.push(['tab', t]); }
function cpUseEmailText(t){ _calls.push(['email', t]); }
function generateFromCalLegacy(){}
var _genInFlight = false, selCt = 'instagram_post';
_els['mktopic'] = new El('mktopic');
"""


def _node(body, fns):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = STUB + _channels_line() + "\n" + "\n".join(_fn(f) for f in fns) + "\n" + body
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


_ROUTING = ["_mktChannelOf", "_mktIsSocial", "_mktIdeaChannel", "mktToStudio", "mktCtToStudio"]


def test_text_and_email_types_and_calendar_ideas_open_the_studio_never_the_post_composer():
    out = _node(r"""
      var r = {};
      r.kinds = [_mktChannelOf('instagram_post'), _mktChannelOf('google_promo'), _mktChannelOf('loyalty_nudge'),
                 _mktChannelOf('weekly_email'), _mktChannelOf('guest_sms'), _mktChannelOf('')];
      r.ideas = [_mktIdeaChannel({platform: 'SMS', type: 'instagram_post'}), _mktIdeaChannel({platform: 'Email'}),
                 _mktIdeaChannel({platform: 'Instagram & FB', type: 'loyalty_nudge'}),
                 _mktIdeaChannel({platform: 'Google', type: 'google_promo'})];
      _els['mktopic'].value = 'Fill Thursday dinner';
      selCt = 'loyalty_nudge'; genContent(false);
      selCt = 'weekly_email'; openMktSchedule();
      selCt = 'loyalty_nudge'; postToFacebook(); postToInstagram();
      mktCtToStudio('weekly_email');
      window._calIdeas = [{day: 'Tuesday', platform: 'SMS', type: 'loyalty_nudge', angle: 'Text regulars about Tuesday'}];
      generateFromCalIdx(0);
      r.calls = _calls; r.fetches = _fetches;
      console.log(JSON.stringify(r));
    """, _ROUTING + ["genContent", "openMktSchedule", "postToFacebook", "postToInstagram", "generateFromCalIdx",
                     "generateFromCal"])
    assert out["kinds"] == ["social", "social", "text", "email", "text", "social"]
    assert out["ideas"] == ["text", "email", "text", "social"]
    assert out["fetches"] == [], "nothing is generated, previewed or posted for a text or an email"
    assert out["calls"] == [["studio", ["text"], "Fill Thursday dinner"], ["studio", ["email"], "Fill Thursday dinner"],
                            ["studio", ["text"], "Fill Thursday dinner"], ["studio", ["text"], "Fill Thursday dinner"],
                            ["studio", ["email"], "Fill Thursday dinner"],
                            ["studio", ["text"], "Text regulars about Tuesday"]]


def test_an_old_quiet_night_text_draft_opens_the_studio_with_its_night():
    out = _node(r"""
      window._mktDrafts = [{id: 7, content_type: 'guest_sms', topic: 'Tuesday night guest text', body: 'x'},
                           {id: 8, content_type: 'weekly_email', topic: 'Fall', body: 'SUBJECT LINE: Fall'},
                           {id: 9, content_type: 'loyalty_nudge', topic: 'We miss you', body: 'y'}];
      useMktDraft(7); useMktDraft(8); useMktDraft(9);
      console.log(JSON.stringify({calls: _calls}));
    """, _ROUTING + ["_mktDraftById", "_mktDraftToStudio", "useMktDraft"])
    assert out["calls"] == [["studio", ["text"], "Fill Tuesday dinner"], ["tab", "campaigns"],
                            ["email", "SUBJECT LINE: Fall"], ["studio", ["text"], "We miss you"]]


def test_the_drafts_shelf_offers_campaigns_not_approve_for_a_text():
    src = _fn("loadMktDrafts")
    assert "var toStudio = !_mktIsSocial(x.content_type);" in src
    assert "(approved || expired || toStudio) ? ''" in src and "Open in Campaigns" in src
    assert "Approving it sent nothing" in src
    approve = _fn("approveMktDraft")
    assert "d.code === 'send_from_campaigns'" in approve and "_mktDraftToStudio" in approve


def test_the_calendar_csv_is_rfc_4180():
    out = _node(r"""
      var _blob = null;
      var Blob = function(parts){ _blob = parts.join(''); };
      var URL = {createObjectURL: function(){ return 'blob:x'; }, revokeObjectURL: function(){}};
      window._calIdeas = [{day: 'Monday', platform: 'Instagram & FB',
                           angle: 'Feature the "Nonna" meatballs, with a side', type: 'instagram_post'}];
      downloadCal();
      console.log(JSON.stringify({csv: _blob}));
    """, ["downloadCal"])
    assert out["csv"] == ('"Day","Platform","Content Idea","Type"\r\n'
                          '"Monday","Instagram & FB","Feature the ""Nonna"" meatballs, with a side","instagram_post"')


# ── AUX-5 / #38: the quiet-night job drafts no dead-end text ─────────────────

def test_the_quiet_night_push_opens_marketing_and_promises_no_approvable_text(db, monkeypatch):
    import demand, strategy_jobs, push, morning_brief, time_utils, ops
    rid = create_restaurant(Restaurant(name="Quiet Co", owner_email="q@x.test", module_marketing=1), db_path=db)
    for mod in (strategy_jobs, push, ops):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: models.get_conn(db), raising=False)
    fired = []
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: fired.append((a, k)))
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 1}])
    monkeypatch.setattr(strategy_jobs, "_draft_quiet_night_fill", lambda *a, **k: {"post_draft_id": 3})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 22, 10, 30))
    monkeypatch.setattr(demand, "quiet_night_ahead", lambda *a, **k: {
        "available": True, "date": "2026-09-24", "weekday": "Thursday", "typical_sales": 2100.0,
        "samples": 6, "below_average_pct": 31.0})
    assert strategy_jobs.run_demand_opportunity(db_path=db)["sent"] == 1
    (args, kw), = fired
    body, data = args[3], kw["data"]
    assert data["nav"] == "marketing" and "ask_prompt" not in data and "sms_draft_id" not in data
    assert "approve" not in body.lower() and "guest text" in body and "Fill Thursday" in body


def test_slow_day_is_the_fill_a_night_prompt_not_general(db, monkeypatch):
    import guest_marketing as gm
    rid = _restaurant(db)
    seen = {}
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(gm, "create_with_retry",
                        lambda *a, **kw: seen.setdefault("p", kw["messages"][0]["content"]) and _msg("See you Tuesday!"))
    gm.draft_campaign_message(get_restaurant(rid, db_path=db), campaign_type="slow_day", topic="Tuesday")
    assert gm.CAMPAIGN_PROMPTS["event"] in seen["p"] and gm.CAMPAIGN_PROMPTS["general"] not in seen["p"]


# ── AUX-15 / #88: a food-cost figure never reaches a public topic ────────────

def test_the_margin_card_keeps_its_figure_and_the_post_never_sees_it(db, monkeypatch):
    import inventory_ledger
    import marketing
    rid = _restaurant(db, module_inventory=1)
    monkeypatch.setattr(inventory_ledger, "menu_profitability", lambda r: {"priced": [
        {"name": "Short Rib", "food_cost_pct": 22.4, "sell_price": 32},
        {"name": "Margherita", "food_cost_pct": 28.0, "sell_price": 16},
        {"name": "Carbonara", "food_cost_pct": 31.0, "sell_price": 21}]})
    ideas = marketing._with_margin_idea(rid, [], {"Thursday": "10/1"}, {"Thursday": "2026-10-01"})
    angle = ideas[0]["angle"]
    assert "22% food cost" in angle, "the owner's card keeps the figure that earned it"
    assert marketing.public_topic(angle) == "Feature Short Rib"
    seen = {}
    _content(monkeypatch, "Short rib, braised all day. #ShortRib", seen)
    marketing.generate_content("instagram_post", angle, restaurant_id=rid, topic_is_owner=False)
    assert "Feature Short Rib" in seen["prompt"]
    assert "food cost" not in seen["prompt"] and "best-margin" not in seen["prompt"] and "22%" not in seen["prompt"]


# ── AUX-16: the stale comment ────────────────────────────────────────────────

def test_the_preview_comment_no_longer_claims_the_prompts_ask_for_two_versions():
    src = open(os.path.join(ROOT, "marketing_publish.py"), encoding="utf-8").read()
    assert "ask\n    # Claude for TWO versions" not in src and "loyalty_nudge and event_announcement ask" not in src


def test_an_address_or_a_link_is_not_an_offer():
    """Found merging the fix round: "deals" joined the one offer vocabulary,
    and the matcher read it inside "deals@..." - a newsletter naming an email
    address was refused as an offer (and for the wrong reason). Addresses and
    links are masked before offers are read; the contact checks judge them."""
    import response_validation as rv
    assert rv.invented_offers("Write to deals@kitchen.test for a table.") == []
    assert rv.invented_offers("Menu at https://kitchen.test/free-parking-deals") == []
    assert rv.invented_offers("Grab our deals this week") == ["deals"]
    assert rv.invented_offers("Half-price apps, see www.kitchen.test") == ["Half-price"]
