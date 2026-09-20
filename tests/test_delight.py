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
    assert "h+=renderReceipts(d);" in src


def test_the_web_renders_cross_module_links():
    src = _dashboard()
    assert "function renderConnections(" in src
    assert "/api/cross-module" in src


def test_the_web_renders_good_news():
    src = _dashboard()
    assert "function renderGoodNews(" in src
    assert "/api/good-news" in src


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
