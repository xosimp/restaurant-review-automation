"""The delight audit's invariants (Sep 2026).

The audit found the product's best moments were computed and never
delivered: correlations() had no route, the web threw away the receipts it
calculated on every page load, and the button that starts an outcome tracker
existed only on the device owners do not use.

These tests pin the delivery — that each surface exists and is reachable —
and the restraint that keeps the good news worth reading.
"""
from datetime import date, timedelta

import pytest

import models
import morning_brief
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    # milestones.py does `from models import DB_PATH, get_conn` at module
    # scope — the house style here, and the bound-import hazard CLAUDE.md
    # documents. Patching models.get_conn does NOT reach that bound copy.
    #
    # It matters in this file specifically because these tests call ROUTE
    # BODIES, which take no db_path (production rightly uses the real one),
    # so the bound default is what actually runs. Without this the route
    # read an empty database and the permission assertion below passed for
    # the wrong reason — it saw no milestones at all.
    import milestones
    monkeypatch.setattr(milestones, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(milestones, "DB_PATH", db_path)


def _restaurant(db_path, **kw):
    kw.setdefault("module_reviews", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "Delight Co"),
                                        owner_email="d@x.com", **kw), db_path=db_path)


# ── the routes exist, web and mobile ─────────────────────────────────────────

# Booting hosted_dashboard from a test is not safe in this suite: other
# modules register the shared admin blueprint into their own Flask app
# first, and Flask then refuses hosted_dashboard's own before_request on
# it. So these assert against the table that DRIVES registration, plus the
# loop that turns each row into both halves — which is the actual twin
# guarantee. The live routes were verified separately against a running
# backend.

@pytest.mark.parametrize("path", ["/cross-module", "/good-news",
                                  "/milestones", "/milestones/seen"])
def test_every_new_surface_is_in_the_route_table(path):
    import strategy_routes
    assert path in [row[0] for row in strategy_routes._ROUTES]


def test_the_route_table_registers_both_halves_of_every_twin():
    """CLAUDE.md's web/mobile-twin rule, and the whole point of this
    audit's top three findings: a surface that reaches one client is a
    surface most owners never see. One loop registers both, so this pins
    the loop rather than each route."""
    import inspect

    import strategy_routes
    src = inspect.getsource(strategy_routes)
    loop = src.split("for _path, _methods, _body_fn, _ep in _ROUTES:")[1][:600]
    assert "strategy_bp.add_url_rule" in loop
    assert "strategy_mobile_bp.add_url_rule" in loop


def test_cross_module_did_not_collide_with_the_integrations_route():
    """/mobile/api/connections already meant POS integrations (Toast,
    Square, Clover, Google). A second meaning on the same path would have
    been a silent trap for whoever read it next."""
    import strategy_routes
    assert "/connections" not in [row[0] for row in strategy_routes._ROUTES]


# ── the brief's all-clear ────────────────────────────────────────────────────

def test_all_clear_is_never_claimed_for_an_unconnected_account(db_path):
    """The dangerous case. An account with nothing connected produces no
    brief lines for the same reason a perfect day does — and telling that
    owner "nothing needs you" is a lie, because nothing was checked."""
    rid = _restaurant(db_path)
    brief = morning_brief.build(rid, db_path=db_path)
    assert [l for l in brief["lines"] if l["key"] == "all_clear"] == []


def test_all_clear_appears_when_there_was_something_to_watch(db_path, monkeypatch):
    # The weather/holiday line is the one thing that can appear for an
    # otherwise silent restaurant, and whether it does depends on today's
    # forecast. Silence it so this asserts the all-clear rule rather than
    # the weather.
    monkeypatch.setattr(morning_brief, "_day_context", lambda *a, **k: "")
    # Reviews only: Labor's "next week isn't built yet" line fires from
    # Thursday on, which would make this assert the day of the week.
    rid = _restaurant(db_path, reviews_live=1, module_labor=0, module_inventory=0)
    from models import get_restaurant
    brief = morning_brief.build(rid, restaurant=get_restaurant(rid), db_path=db_path)
    lines = [l for l in brief["lines"] if l["key"] == "all_clear"]
    assert len(lines) == 1
    assert lines[0]["tone"] == "good"
    # It says WHAT was watched, so the reassurance is verifiable.
    assert "reviews" in lines[0]["text"]


def test_all_clear_yields_to_any_real_line(db_path):
    """It is a last resort, not a greeting. One real thing to do must push
    it out entirely."""
    rid = _restaurant(db_path, reviews_live=1)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, "
        "sentiment, processed, response_status, review_date, fetched_at) "
        "VALUES (?,?,?,?,?,?,1,'pending',?,?)",
        (rid, "google", "d-1", 1, "bad", "negative",
         date.today().isoformat(), date.today().isoformat()))
    conn.commit()
    conn.close()
    from models import get_restaurant
    brief = morning_brief.build(rid, restaurant=get_restaurant(rid), db_path=db_path)
    keys = [l["key"] for l in brief["lines"]]
    assert "reviews" in keys
    assert "all_clear" not in keys


def test_watching_lists_only_modules_with_data(db_path):
    """A module switched on but never fed is not watching anything."""
    rid = _restaurant(db_path, module_labor=1, reviews_live=0)
    from models import get_restaurant
    assert morning_brief._watching(get_restaurant(rid), rid, frozenset(), db_path) == []


def test_watching_text_reads_as_a_sentence():
    assert morning_brief._watching_text(["reviews"]) == "I'm watching reviews"
    assert morning_brief._watching_text(["reviews", "labor"]) == "I'm watching reviews and labor"
    assert (morning_brief._watching_text(["reviews", "labor", "food cost"])
            == "I'm watching reviews, labor and food cost")


def test_an_all_clear_brief_is_delivered_at_most_once_a_week(db_path):
    """Reassurance every morning is how a brief becomes the notification
    people swipe away — which costs them the day it says something real."""
    import ops
    rid = _restaurant(db_path, reviews_live=1)
    brief = {"lines": [{"key": "all_clear", "tone": "good", "text": "…", "ask": "?"}]}
    assert morning_brief._only_all_clear(brief)
    # A fixed Wednesday. ISO weeks end on Sunday, so running this on a real
    # Sunday made "tomorrow" the next week and the test passed or failed by
    # the day it ran.
    today = date(2026, 9, 16)
    assert today.isoweekday() == 3
    first = morning_brief._claim_weekly_all_clear(rid, 1, today)
    second = morning_brief._claim_weekly_all_clear(rid, 1, today + timedelta(days=1))
    assert first is True
    assert second is False, "a second quiet day in the same week must stay silent"
    # A different person still gets their own.
    assert morning_brief._claim_weekly_all_clear(rid, 2, today) is True
    # And next week it comes round again.
    assert morning_brief._claim_weekly_all_clear(rid, 1, today + timedelta(days=7)) is True


def test_a_brief_with_real_news_is_never_held_back(db_path):
    """The weekly hold applies ONLY to a brief that says nothing else."""
    brief = {"lines": [{"key": "all_clear"}, {"key": "reviews"}]}
    assert morning_brief._only_all_clear(brief) is False
    assert morning_brief._only_all_clear({"lines": [{"key": "reviews"}]}) is False
    assert morning_brief._only_all_clear({"lines": []}) is False


# ── the brief carries at most one piece of good news ─────────────────────────

def test_the_brief_never_opens_with_three_congratulations(db_path, monkeypatch):
    """A brief that leads with a run of congratulations is one nobody reads
    to the end, and the bad news is at the end."""
    import good_news
    fake = [{"key": f"record:{i}", "headline": f"Record {i}", "summary": "s",
             "kind": "record"} for i in range(5)]
    monkeypatch.setattr(good_news, "all_good_news",
                        lambda rid, **kw: fake[:(kw.get("limit") or len(fake))])
    rid = _restaurant(db_path, reviews_live=1)
    from models import get_restaurant
    brief = morning_brief.build(rid, restaurant=get_restaurant(rid), db_path=db_path)
    good = [l for l in brief["lines"] if l["key"].startswith("record:")]
    assert len(good) <= 1


def test_good_news_lines_are_askable(db_path, monkeypatch):
    """Every brief line carries an `ask` so a tap opens Ask on it. A line
    that cannot be asked about is a dead end."""
    import good_news
    monkeypatch.setattr(good_news, "all_good_news",
                        lambda rid, **kw: [{"key": "record:sales", "kind": "record",
                                            "headline": "Best sales in 8 periods",
                                            "summary": "s"}])
    rid = _restaurant(db_path, reviews_live=1)
    from models import get_restaurant
    brief = morning_brief.build(rid, restaurant=get_restaurant(rid), db_path=db_path)
    for line in brief["lines"]:
        assert line.get("ask"), f"{line['key']} has no ask prompt"


# ── permissions on the new surfaces ──────────────────────────────────────────
#
# Each of these is the same class of bug the ROI audit found in /api/value,
# where the breakdown was filtered and the headline total was not: a manager
# could subtract their way back to the numbers they were not shown.

def _manager(rid):
    return {"id": 2, "restaurant_id": rid, "role": "manager", "is_admin": False}


def _owner(rid):
    return {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False}


def _denied_for(strategy_routes, monkeypatch, user, rid):
    """What /good-news would hide from this login."""
    seen = {}
    import good_news
    monkeypatch.setattr(good_news, "all_good_news",
                        lambda r, **kw: seen.update(
                            denied=set(kw.get("denied_modules") or ())) or [])
    monkeypatch.setattr(strategy_routes, "_local_today", lambda u: date.today())
    strategy_routes._do_good_news(user)
    return seen["denied"]


def test_good_news_denies_every_module_the_login_lacks_not_just_food_cost(db_path, monkeypatch):
    """The route must delegate to viewer_restaurant rather than hand-roll a
    food-cost-only check.

    No role in ROLE_PERMISSIONS denies Labor today, so this is hardening
    rather than a live leak — which is exactly why it needs a test that
    fails for the right reason. Denying LABOR_VIEW directly proves the
    route asks the permission system instead of assuming the answer.
    """
    import permissions
    import strategy_routes
    real = permissions.has_permission
    monkeypatch.setattr(
        permissions, "has_permission",
        lambda u, p: False if p == permissions.LABOR_VIEW else real(u, p))
    rid = _restaurant(db_path)
    denied = _denied_for(strategy_routes, monkeypatch, _owner(rid), rid)
    assert "labor" in denied, (
        "a login without LABOR_VIEW must not be shown sales or labor records")


def test_good_news_still_hides_margins_from_a_manager(db_path, monkeypatch):
    import strategy_routes
    rid = _restaurant(db_path)
    assert "inventory" in _denied_for(strategy_routes, monkeypatch, _manager(rid), rid)


def test_good_news_shows_the_owner_everything(db_path, monkeypatch):
    import strategy_routes
    seen = {}
    import good_news
    monkeypatch.setattr(good_news, "all_good_news",
                        lambda rid, **kw: seen.update(denied=set(kw.get("denied_modules") or ())) or [])
    rid = _restaurant(db_path)
    monkeypatch.setattr(strategy_routes, "_local_today", lambda u: date.today())
    strategy_routes._do_good_news(_owner(rid))
    assert seen["denied"] == set()


def test_money_milestones_are_owner_level(db_path):
    """A savings milestone's body carries whole-business measured dollars,
    which include food-cost results."""
    import milestones
    import strategy_routes
    rid = _restaurant(db_path)
    milestones.fire(rid, "savings", "savings:5000", "$5,000 in measured results",
                    body="...$5,000 a year...", db_path=db_path)
    milestones.fire(rid, "anniversary", "anniversary:3", "3 months", db_path=db_path)

    payload, _ = strategy_routes._do_milestones(_manager(rid))
    kinds = {m["kind"] for m in payload["items"]}
    assert "savings" not in kinds, "a manager must not read whole-business dollars here"
    assert "anniversary" in kinds, "but they still get the moments that are theirs"

    payload, _ = strategy_routes._do_milestones(_owner(rid))
    assert {"savings", "anniversary"} <= {m["kind"] for m in payload["items"]}


def test_cross_module_is_built_from_what_this_login_may_see(db_path, monkeypatch):
    """viewer_restaurant zeroes the module flags for anything denied, and
    business_intelligence.gather only reads modules whose flag is on — so a
    denied module cannot contribute a side of a link."""
    import strategy_routes
    seen = {}
    import business_intelligence as bi
    monkeypatch.setattr(bi, "executive_brief",
                        lambda rid, restaurant=None, **kw: seen.update(r=restaurant) or {})
    rid = _restaurant(db_path, module_inventory=1, module_labor=1)
    strategy_routes._do_cross_module(_manager(rid))
    view = seen["r"]
    assert view.module_inventory == 0, "food cost must be off for a manager"
    assert "inventory" in getattr(view, "_ask_denied", frozenset())


# ── prose never carries a code identifier ────────────────────────────────────

def test_every_complaint_category_reads_as_words():
    """"takeout_delivery" reached the What Connects card on the dashboard
    looking like a variable name. Categories are snake_case identifiers —
    right for grouping, wrong for a sentence."""
    from analyser import CATEGORIES, category_label
    for category in CATEGORIES:
        label = category_label(category)
        assert "_" not in label, f"{category} still reads as an identifier"
        assert label and label == label.lower()


def test_an_unmapped_category_still_reads_as_words():
    """A category added to CATEGORIES without a label must degrade to words,
    not to code."""
    from analyser import category_label
    assert category_label("private_dining_rooms") == "private dining rooms"


def test_business_intelligence_never_interpolates_a_raw_category():
    """The guard is mechanical: nothing in the file may put c['category']
    straight into an f-string."""
    import inspect

    import business_intelligence as bi
    src = inspect.getsource(bi)
    assert "{c['category']}" not in src
    assert '{c["category"]}' not in src


def test_one_shared_category_vocabulary():
    """good_news carried its own copy, which is how "wait_time" becomes
    "wait times" on one screen and "wait time" on the next."""
    import good_news
    from analyser import category_label
    assert good_news.category_label is category_label


# ── the first look ───────────────────────────────────────────────────────────

def test_first_look_says_nothing_without_data():
    import first_look
    assert first_look.lines({}) == []
    assert first_look.lines(None) == []


def test_first_look_never_prints_a_zero_rating():
    """Google omits `rating` entirely for a listing with too few reviews.
    Rendering that as 0.0 stars would be the worst possible first thing
    this product ever says to an owner."""
    import first_look
    assert first_look.lines({"review_count": 4}) == []


def test_first_look_needs_enough_neighbours_to_average(db_path):
    import first_look
    out = first_look.lines({"rating": 4.5, "review_count": 100,
                            "neighbourhood": {"avg_rating": 4.1, "count": 5,
                                              "best": "Somewhere"}})
    assert any("4.1" in line for line in out)
    # With no neighbourhood block there is simply no comparison sentence.
    bare = first_look.lines({"rating": 4.5, "review_count": 100})
    assert len(bare) == 1


def test_first_look_states_the_gap_plainly_when_behind():
    import first_look
    out = first_look.lines({"rating": 3.9, "review_count": 100,
                            "neighbourhood": {"avg_rating": 4.4, "count": 4, "best": "X"}})
    assert any("4.4" in line for line in out)


# ── the welcome email ────────────────────────────────────────────────────────

def test_welcome_email_survives_a_places_outage(monkeypatch):
    """The first look is a nicety. Losing it must never cost the owner
    their credentials."""
    import emails
    import first_look
    monkeypatch.setattr(first_look, "build",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("places down")))
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda **kw: sent.update(kw) or True)
    emails.send_welcome_email("o@x.com", "Testaurant", "user", "pw",
                              module_reviews=1, google_place_id="abc")
    assert sent["payload"]["to"] == ["o@x.com"]
    assert "pw" in sent["payload"]["html"]


def test_welcome_email_includes_the_first_look_when_it_is_available(monkeypatch):
    import emails
    import first_look
    monkeypatch.setattr(first_look, "build",
                        lambda *a, **k: {"rating": 4.5, "review_count": 801})
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda **kw: sent.update(kw) or True)
    emails.send_welcome_email("o@x.com", "Testaurant", "user", "pw",
                              module_reviews=1, google_place_id="abc")
    html = sent["payload"]["html"]
    assert "What I can already see" in html
    assert "4.5 stars" in html and "801" in html


def test_welcome_email_without_a_place_id_is_unchanged(monkeypatch):
    import emails
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda **kw: sent.update(kw) or True)
    emails.send_welcome_email("o@x.com", "Testaurant", "user", "pw", module_reviews=1)
    assert "What I can already see" not in sent["payload"]["html"]


# ── the web surfaces actually render what the server computes ────────────────

def _dashboard():
    with open("templates/dashboard.html", encoding="utf-8") as fh:
        return fh.read()


def test_the_web_renders_the_receipts_it_computes():
    """home_brief has computed `receipts` and `overnight` on every page load
    since Home shipped and the web rendered neither — only iOS did. A
    payload nobody renders is the most expensive kind of dead code, because
    it looks shipped."""
    src = _dashboard()
    assert "function renderReceipts(" in src
    # Rendered inside THE DAY's slot (Home order, DESIGN_SYSTEM 11b): the
    # receipts lead the day on Monday and follow the brief otherwise.
    assert "var receipts=renderReceipts(d);" in src
    assert "if(monday)day+=receipts;" in src and "(monday?'':receipts)" in src


def test_the_web_renders_cross_module_links():
    src = _dashboard()
    assert "function renderConnections(" in src
    assert "/api/cross-module" in src


def test_the_web_renders_good_news():
    src = _dashboard()
    assert "function renderGoodNews(" in src
    assert "/api/good-news" in src


def test_no_home_button_builds_a_js_string_literal_from_server_text():
    """The Track button silently did nothing on any recommendation whose
    title contained an apostrophe — "Look into service — it's the
    most-mentioned complaint".

    esc() turns ' into &#39;, the HTML parser turns it back into ' when it
    reads the attribute, and the JS that reaches the engine is a syntax
    error. The .replace(/'/g,...) written to guard this could never fire,
    because after esc() there is no literal apostrophe left to find.

    The fix is structural: values ride on data attributes and a delegated
    listener reads them off the element, so an apostrophe is just a
    character. This pins that no Home handler goes back to the old shape.

    The rule is about FREE TEXT, not about onclick as such. hbOpen still
    appears in onclick attributes and is fine there: its argument is a
    module key from a closed vocabulary (TAB / MODLABEL), where an
    apostrophe cannot occur. hbTrack and hbDismiss take a recommendation's
    title and key, which are prose the server composes.
    """
    src = _dashboard()
    for handler in ("hbTrack(", "hbDismiss("):
        bad = f'onclick="{handler}'
        assert bad not in src, (
            f"{handler} is back in an onclick attribute — it carries server "
            f"prose, which breaks the JS string literal on an apostrophe")
    assert "data-track-key" in src and "data-dismiss-key" in src


def test_the_value_banner_detail_link_has_somewhere_to_go():
    """hbOpen('home') fell through to the "isn't active on this account"
    toast, because Home is not in TAB — it is the page the link lives on."""
    src = _dashboard()
    assert "if(module==='home')" in src


def test_plain_english_is_not_set_in_the_number_face():
    """Space Grotesk is the NUMBER face (DESIGN_SYSTEM.md §2)."""
    src = _dashboard()
    idx = src.find("Nothing measured yet</div>")
    assert idx > 0
    line_start = src.rfind("<div", 0, idx)
    assert "Space Grotesk" not in src[line_start:idx]


def test_the_closeout_uses_the_pages_own_date_format():
    """M/D/YY everywhere. This card printed the raw ISO business_date."""
    src = _dashboard()
    # The card was rebuilt in the Home redesign; the rule it pins is the
    # same - the business date goes through mdy(), never straight out.
    start = src.find("function renderCloseout(")
    body = src[start:src.find("window.hbSaveCloseout", start)]
    assert "mdy(c.business_date)" in body
    assert "esc(c.business_date)" not in body


def test_the_celebration_is_no_longer_gated_on_localstorage():
    """The modal promised "this won't appear again" and was gated on
    localStorage, which meant a new browser or a cleared cache re-fired it
    and the phone could not see it at all."""
    src = _dashboard()
    # The NAME may survive in the comment that explains why it went — that
    # history is worth keeping. What must not survive is a read or a write.
    assert "localStorage.getItem('cavnar_congrats_shown')" not in src
    assert "localStorage.setItem('cavnar_congrats_shown'" not in src
    assert "/api/milestones/seen" in src


def test_the_response_rate_research_stays_with_the_response_rate_milestone():
    """Quoting return-rate research under an anniversary would be a figure
    with nothing to do with what was achieved."""
    src = _dashboard()
    assert "congrats-stats" in src
    assert "m.kind==='response_rate'" in src


# ── iOS parity for the loop that starts a tracker ────────────────────────────

def _swift(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_ios_can_start_an_outcome_tracker():
    """The audit's third structural finding: outcomes.record is what
    eventually produces "that one worked, about $420/month", and the only
    button that calls it lived on the web."""
    src = _swift("ios/CavnarAI/CavnarAI/Features/Home/HomeFollowThrough.swift")
    assert '"/mobile/api/outcomes", method: .post' in src
    rec = _swift("ios/CavnarAI/CavnarAI/Features/Home/HomeRecommendations.swift")
    assert "Track this" in rec


def test_ios_only_offers_to_track_what_can_be_measured():
    """A recommendation with no metric cannot be measured before and after,
    and a button that silently measures nothing is worse than no button."""
    rec = _swift("ios/CavnarAI/CavnarAI/Features/Home/HomeRecommendations.swift")
    assert "if rec.metric != nil" in rec


def test_ios_decodes_the_promise_comparison():
    src = _swift("ios/CavnarAI/CavnarAI/Features/Home/HomeFollowThrough.swift")
    assert "struct Promise: Decodable" in src
    assert 'case auditDate = "audit_date"' in src


def test_ios_renders_cross_module_and_good_news():
    src = _swift("ios/CavnarAI/CavnarAI/Features/Home/HomeFollowThrough.swift")
    assert "/mobile/api/cross-module" in src
    assert "/mobile/api/good-news" in src
    assert "private var connectionsCard" in src
    assert "private var goodNewsCard" in src


def test_ios_has_a_celebration_and_it_respects_reduced_motion():
    src = _swift("ios/CavnarAI/CavnarAI/Features/Home/MilestoneMoment.swift")
    assert "accessibilityReduceMotion" in src
    # DESIGN_SYSTEM.md §11: every looping/entrance animation needs a still
    # fallback, and ember is the only accent that moves.
    assert "guard !reduceMotion else { return }" in src


def test_ios_decodes_the_recommendations_the_server_already_sends():
    src = _swift("ios/CavnarAI/CavnarAI/Models/HomeSummary.swift")
    assert "struct HomeRecommendation" in src
    assert "let recommendations: [HomeRecommendation]?" in src
