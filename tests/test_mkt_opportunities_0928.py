"""The Marketing Opportunity Feed, 9/28/26 (marketing_opportunities).

Opening Marketing shows what Cavnar AI found worth doing, from measured
signals: a reliably slow weekday ahead, last year's holiday night, a POS
category falling past its own swing, a high-margin dish nobody orders, a
dish guests praise, an opted-in list nobody has contacted, nothing posted
lately. Every figure is measured and says what it is — a gap, never an
expected return — and a card rests on a floor or doesn't appear. Answered
cards go; facts carry no confidence; nothing calls a model on load."""
import json
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import demand
import guest_marketing as gm
import insight_store
import marketing_opportunities as mo
import menu_intelligence
import mobile_api
import models
import rec_ledger
from auth import create_user, init_auth
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_conn, get_restaurant, update_restaurant

SRC = open("templates/dashboard.html", encoding="utf-8").read()
TODAY = date.today()
NOW = datetime.combine(TODAY, datetime.min.time()).replace(hour=12)


@pytest.fixture
def db(db_path, monkeypatch):
    gm.init_guest_marketing(db_path)
    insight_store.init_insight_store(db_path)
    rec_ledger.init_rec_ledger(db_path)
    from dsr import store as dstore
    dstore.init_dsr(db_path)
    real = models.get_conn
    for mod in (models, gm, menu_intelligence):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path))
    return db_path


def _rid(db, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Opp Co"), owner_email="o@x.test", module_marketing=1, **kw),
                             db_path=db)


def _labor(db, rid, slow_day="Tuesday", slow=3000.0, usual=7000.0, weeks=8):
    c = get_conn(db)
    for i in range(1, weeks * 7 + 1):
        d = TODAY - timedelta(days=i)
        name = d.strftime("%A")
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                  (rid, d.isoformat(), name, slow + (i % 3) * 10 if name == slow_day else usual + (i % 5) * 10))
    c.commit()
    c.close()


# ── slow nights ─────────────────────────────────────────────────────────────

def test_a_reliably_slow_weekday_is_a_card_with_its_measured_gap(db):
    rid = _rid(db)
    _labor(db, rid)
    cards = mo.slow_nights(rid, TODAY, db)
    assert len(cards) == 1
    c = cards[0]
    assert c["key"] == rec_ledger.rec_key("slow_day", "Tuesday") == "slow_day:Tuesday"
    assert c["title"].startswith("Fill Tuesday, ") and "8 of the last 8 did" in c["why"]
    typical = demand.slow_days(rid, db_path=db)["typical_day"]
    assert f"${typical:,.0f} a typical day" in c["facts"]
    assert c["stake"]["label"] == "a Tuesday night under a typical day" and c["stake"]["amount"] > 3000
    assert c["action"] == {"prompt": "Fill Tuesday dinner", "channels": ["text", "email", "social"]}
    assert gm.plan_campaign(c["action"]["prompt"])["target_day"] == "Tuesday"   # the send closes the card
    assert c["evidence"]["kind"] == "nights" and 1 <= c["days_away"] <= 7


def test_every_slow_card_quotes_one_typical_day_and_there_are_at_most_two(db):
    rid = _rid(db)
    c = get_conn(db)
    for i in range(1, 57):
        d = TODAY - timedelta(days=i)
        slow = d.strftime("%A") in ("Monday", "Tuesday", "Wednesday")
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                  (rid, d.isoformat(), d.strftime("%A"), 3000.0 if slow else 8000.0))
    c.commit()
    c.close()
    cards = mo.slow_nights(rid, TODAY, db)
    assert len(cards) == 2
    typical = {f for card in cards for f in card["facts"] if f.endswith("a typical day")}
    assert len(typical) == 1                                              # one figure, not three
    assert [x["days_away"] for x in cards] == sorted(x["days_away"] for x in cards)


def test_no_history_no_slow_card(db):
    assert mo.slow_nights(_rid(db), TODAY, db) == []


# ── holidays ────────────────────────────────────────────────────────────────

def test_a_measured_holiday_says_last_year_and_an_unmeasured_one_claims_nothing(db, monkeypatch):
    rid = _rid(db)
    soon = (TODAY + timedelta(days=5)).isoformat()
    later = (TODAY + timedelta(days=18)).isoformat()
    far_unmeasured = (TODAY + timedelta(days=16)).isoformat()
    monkeypatch.setattr(demand, "upcoming_holidays", lambda *a, **k: [
        {"name": "Halloween", "date": soon, "days_away": 5, "lift_pct": 33, "claim_kind": "measured",
         "based_on": "last year's night against 4 Saturdays around it"},
        {"name": "Veterans Day", "date": later, "days_away": 18, "lift_pct": 4, "claim_kind": "measured",
         "based_on": "x"},                                                  # within normal: no card
        {"name": "Thanksgiving", "date": far_unmeasured, "days_away": 16, "lift_pct": None, "claim_kind": None},
        {"name": "Today Day", "date": TODAY.isoformat(), "days_away": 0, "lift_pct": 50, "claim_kind": "measured"}])
    cards = mo.holidays(rid, NOW, db)
    assert [c["key"] for c in cards] == [f"holiday_promo:{soon}"]
    c = cards[0]
    assert c["title"].startswith("Get ready for Halloween") and "ran 33% above a typical" in c["why"]
    assert c["stake"] is None and c["evidence"]["n"] == 1                  # one night last year: low evidence, honestly
    monkeypatch.setattr(demand, "upcoming_holidays", lambda *a, **k: [
        {"name": "Thanksgiving", "date": (TODAY + timedelta(days=9)).isoformat(), "days_away": 9,
         "lift_pct": None, "claim_kind": None}])
    c = mo.holidays(rid, NOW, db)[0]
    assert c["evidence"] is None and "no Thanksgiving of your own on file" in c["why"]


# ── category dips ───────────────────────────────────────────────────────────

def _cat(db, rid, name, prior, last, skip_last=0):
    c = get_conn(db)
    end = TODAY - timedelta(days=1)
    for i in range(56):
        d = end - timedelta(days=i)
        if i < 28 and i < skip_last:
            continue
        c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
                  (rid, d.isoformat(), f"sales.cat:{name}", last if i < 28 else prior, "final"))
    c.commit()
    c.close()


def test_a_category_down_past_its_swing_is_a_card_and_a_steady_one_is_not(db):
    rid = _rid(db)
    _cat(db, rid, "Wine", prior=90.0, last=70.0)
    _cat(db, rid, "Beer", prior=80.0, last=78.0)
    cards = mo.category_dips(rid, TODAY, db)
    assert [c["key"] for c in cards] == ["category_dip:Wine"]
    c = cards[0]
    assert c["title"] == "Win back wine sales"
    assert c["why"] == "Wine is down 22%: $1,960 over the last 28 nights vs $2,520 the 28 before."
    assert c["stake"] == {"amount": 560.0, "label": "less than the 28 nights before"}
    assert c["action"]["prompt"] == "Promote our wine"


def test_a_thin_window_or_a_small_category_says_nothing(db):
    rid = _rid(db)
    _cat(db, rid, "Wine", prior=90.0, last=40.0, skip_last=10)              # 18 nights measured
    _cat(db, rid, "Cider", prior=10.0, last=2.0)                            # $280 base
    assert mo.category_dips(rid, TODAY, db) == []


# ── dishes ──────────────────────────────────────────────────────────────────

def test_a_praised_dish_is_a_card_and_a_criticised_one_is_not(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(menu_intelligence, "dish_praise", lambda *a, **k: [
        {"name": "Carbonara", "positive_mentions": 6, "negative_mentions": 0},
        {"name": "Ribeye", "positive_mentions": 5, "negative_mentions": 1},
        {"name": "Soup", "positive_mentions": 2, "negative_mentions": 0}])
    monkeypatch.setattr(menu_intelligence, "dish_scorecard", lambda *a, **k: {"available": False})
    cards = mo.dishes(rid, db)
    assert [c["key"] for c in cards] == ["dish_praise:Carbonara"]
    assert cards[0]["why"] == "Named positively in 6 reviews in the last 90 days, never negatively."
    assert cards[0]["stake"] is None


def test_a_high_margin_low_selling_dish_is_promoted_against_the_menu_median(db, monkeypatch):
    rid = _rid(db)
    dishes = [{"name": n, "units_sold": u, "margin": m, "action": a, "positive_mentions": p}
              for n, u, m, a, p in (("Short Rib", 22, 14.2, "promote", 3), ("Burger", 60, 6.0, "reprice", 0),
                                    ("Salad", 41, 9.1, None, 0), ("Wings", 50, 11.0, None, 0))]
    monkeypatch.setattr(menu_intelligence, "dish_scorecard",
                        lambda *a, **k: {"available": True, "has_sales_data": True, "dishes": dishes})
    monkeypatch.setattr(menu_intelligence, "dish_praise", lambda *a, **k: [
        {"name": "Short Rib", "positive_mentions": 3, "negative_mentions": 0}])
    cards = mo.dishes(rid, db)
    assert [c["key"] for c in cards] == ["dish_promote:Short Rib"]          # praised too: one card, not two
    c = cards[0]
    assert c["why"] == "It earns $14.20 a plate (menu median $10.05) but sold 22 in the last 28 days (median 46)."
    assert c["facts"] == ["Named positively in 3 reviews"]


def test_dish_praise_counts_only_unambiguous_mentions(db):
    rid = _rid(db)
    c = get_conn(db)
    for n in ("Carbonara", "Chicken Parm", "Chicken Wings"):
        c.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, n))
    for i, (dish, sent) in enumerate((("carbonara", "positive"), ("the carbonara", "positive"),
                                       ("chicken", "positive"), ("carbonara", "negative"))):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, sentiment, "
                  "entities, processed, review_date, fetched_at) VALUES (?,?,?,?,?,?,?,?,1,?,datetime('now'))",
                  (rid, "google", f"r{i}", "A", 5, "x", sent, json.dumps({"dishes": [dish]}),
                   (TODAY - timedelta(days=3)).isoformat()))
    c.commit()
    c.close()
    out = {it["name"]: (it["positive_mentions"], it["negative_mentions"]) for it in menu_intelligence.dish_praise(rid, db)}
    assert out == {"Carbonara": (2, 1)}                                     # "chicken" names two dishes: neither


# ── lists and posting ───────────────────────────────────────────────────────

def _contacts(db, rid, n, email=0, invite_yes=0):
    c = get_conn(db)
    for i in range(n):
        phone = f"+1555040{i:04d}"
        c.execute("INSERT INTO guest_contacts (restaurant_id, phone, name, consent, email, email_consent) "
                  "VALUES (?,?,?,1,?,?)", (rid, phone, f"G{i}", f"g{i}@x.test" if i < email else None, 1 if i < email else 0))
        if i < invite_yes:
            c.execute("INSERT INTO sms_optin_invites (restaurant_id, phone, external_ref, response) VALUES (?,?,?,?)",
                      (rid, phone, f"ref{i}", "yes"))
    c.commit()
    c.close()


def test_idle_lists_are_counted_without_review_link_yeses(db):
    rid = _rid(db)
    _contacts(db, rid, 30, email=12, invite_yes=6)
    cards = {c["key"]: c for c in mo.lists(rid, NOW, db)}
    assert set(cards) == {"list_idle:email"}                                # 24 marketing-consented texts: under 25
    assert cards["list_idle:email"]["title"] == "Email the 12 guests on your list"
    assert cards["list_idle:email"]["why"] == "They've never been emailed."
    assert cards["list_idle:email"]["evidence"] is None and cards["list_idle:email"]["action"]["channels"] == ["email"]
    _contacts(db, rid, 0)
    c = get_conn(db)
    c.execute("INSERT INTO guest_newsletters (restaurant_id, subject, body, content_hash, total, created_at) "
              "VALUES (?,?,?,?,?,?)", (rid, "S", "B", "h", 12, (NOW - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    c.close()
    assert mo.lists(rid, NOW, db) == []                                     # emailed 10 days ago


def test_posting_card_needs_a_connection_and_a_gap(db):
    rid = _rid(db)
    assert mo.posting(rid, NOW, get_restaurant(rid, db_path=db), db) == []  # nothing connected
    update_restaurant(rid, {"ig_token": "tok"}, db_path=db)
    cards = mo.posting(rid, NOW, get_restaurant(rid, db_path=db), db)
    assert cards[0]["key"] == "post_this_week" and cards[0]["why"] == "Nothing has been posted yet."
    c = get_conn(db)
    c.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, created_at) "
              "VALUES (?,?,?,?,?)", (rid, "instagram_post", "t", "p1", (NOW - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    c.close()
    assert mo.posting(rid, NOW, get_restaurant(rid, db_path=db), db) == []


# ── the feed ────────────────────────────────────────────────────────────────

def test_the_feed_ranks_drops_answered_cards_and_gives_facts_no_confidence(db, monkeypatch):
    rid = _rid(db)
    _labor(db, rid)
    _contacts(db, rid, 12, email=12)
    items = mo.feed(rid, db_path=db)["items"]
    keys = [i["key"] for i in items]
    assert keys[0] == "slow_day:Tuesday" and "list_idle:email" in keys
    by = {i["key"]: i for i in items}
    assert isinstance(by["slow_day:Tuesday"]["confidence"], dict)
    assert by["list_idle:email"]["confidence"] is None
    rec_ledger.record(rid, "slow_day:Tuesday", "dismissed", surface="marketing", meta={"kind": "not_for_us"},
                      db_path=db)
    assert "slow_day:Tuesday" not in [i["key"] for i in mo.feed(rid, db_path=db)["items"]]


def test_the_feed_is_stored_and_rebuilt_only_when_its_inputs_move(db, monkeypatch):
    rid = _rid(db)
    calls = []
    real = mo.build
    monkeypatch.setattr(mo, "build", lambda *a, **k: calls.append(1) or real(*a, **k))
    mo.cached_build(rid, db_path=db, now=NOW)
    mo.cached_build(rid, db_path=db, now=NOW)
    assert len(calls) == 1
    _contacts(db, rid, 1)
    mo.cached_build(rid, db_path=db, now=NOW)
    assert len(calls) == 2


def test_one_failing_source_never_empties_the_feed(db, monkeypatch):
    rid = _rid(db)
    _labor(db, rid)
    monkeypatch.setattr(mo, "category_dips", lambda *a, **k: 1 / 0)
    assert [c["key"] for c in mo.build(rid, db_path=db, now=NOW)] == ["slow_day:Tuesday"]


def test_no_model_call_on_build():
    src = open("marketing_opportunities.py", encoding="utf-8").read()
    for banned in ("create_with_retry", "get_client", "requests.", "urlopen"):
        assert banned not in src, banned


# ── the route ───────────────────────────────────────────────────────────────

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


def _login(client, db, module=1):
    rid = create_restaurant(Restaurant(name="Route Co", owner_email="r@x.test", module_marketing=module), db_path=db)
    create_user(rid, "owner", "owner@x.test", "correct-horse", db_path=db)
    tok = client.post("/mobile/api/login", json={"username": "owner", "password": "correct-horse"}).get_json()["token"]
    return rid, {"Authorization": f"Bearer {tok}"}


def test_the_route_serves_the_feed_behind_the_module(client, db):
    rid, h = _login(client, db)
    _labor(db, rid)
    d = client.get("/mobile/api/marketing/opportunities", headers=h).get_json()
    assert d["ok"] and d["items"][0]["key"] == "slow_day:Tuesday" and d["checked"]
    src = open("client_api.py", encoding="utf-8").read()
    i = src.index('@client_bp.route("/api/marketing/opportunities")')
    assert '_m("mobile_marketing_opportunities")' in src[i:i + 300]


def test_the_route_needs_marketing(client, db):
    _, h = _login(client, db, module=0)
    assert client.get("/mobile/api/marketing/opportunities", headers=h).status_code == 403


# ── the drafters never tell guests the owner's aim ─────────────────────────

def test_every_drafter_forbids_announcing_a_slow_night():
    assert "never say or hint that a night is slow" in open("guest_marketing.py", encoding="utf-8").read()
    assert "never say or hint that a night is slow" in open("guest_email.py", encoding="utf-8").read()
    assert "Never say or hint that a night is slow" in open("marketing.py", encoding="utf-8").read()
    assert "Nothing is new, back, better or changed unless the owner's words say so." in \
        open("guest_marketing.py", encoding="utf-8").read()


def test_the_new_kinds_have_topics_and_dish_kinds_tag_the_dish():
    for k in ("holiday_promo", "category_dip", "dish_promote", "dish_praise", "list_idle"):
        assert k in rec_ledger.KIND_TOPIC, k
    assert "dish:short rib" in rec_ledger.tags_for("dish_promote:Short Rib", module="marketing")


# ── the page ────────────────────────────────────────────────────────────────

def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


def test_the_feed_leads_marketing_above_the_sub_tabs():
    panel = _between('id="panel-marketing"', 'id="mkt-tab-content-btn"')
    assert '<section class="mkt-opps" id="mkt-opps" data-nav="marketing/opportunities"' in panel
    assert panel.index('id="mkt-opps"') > panel.index('class="hb-top"')


def test_cards_reuse_home_anatomy_one_primary_and_answer_controls():
    card = _between("function mktOppCard(o, i) {", "function mktPaintOpps()")
    assert "hb-card hb-rec hb-rise mkt-opp" in card
    assert "recControlsHtml(o.key, 'marketing', 'marketing', {noTrack: 1})" in card
    assert "(i === 0 ? 'cbtn-primary' : 'cbtn-secondary')" in card
    assert "cavConfLine(o.confidence" in card and "o.stake.label" in card


def test_draft_it_opens_the_studio_with_the_goal_and_channels():
    fn = _between("window.mktOppDraft = function(o) {", "document.addEventListener('click', function(e) {\n  var t = e.target && e.target.closest ? e.target.closest('[data-opp-draft]')")
    assert "switchMktTab('campaigns')" in fn and "cpCreate()" in fn and "_cp.chanSet = true" in fn
    assert "cpSocialPlats().length > 0" in fn
    assert "event: 'opened'" in SRC                                         # opened, not answered
    assert "loadMktOpps();\n      loadGuestOverview();" in SRC
    assert "recOnAnswered(function(key) {" in SRC


def test_an_empty_feed_says_what_was_checked():
    assert "Nothing stands out right now. Cavnar AI checked " in SRC
    assert "dish margins and sales" in open("marketing_opportunities.py", encoding="utf-8").read()
