"""Audit #11 remediation: what the Intel module claims, and on what basis.

Every test here was written against a measured problem. The module was
rendering a Perplexity outage as "Not yet indexed by AI search" in red,
scoring AI visibility out of three non-deterministic questions so a single
flip moved it 33 points and fired an SMS, counting whether a Yelp ID had
been typed into Cavnar AI as part of an AI visibility score, and letting a
Claude insight name a restaurant that was never in the competitor list.
"""
import json
import types

import pytest

import ai_guard
import competitor
import models
import notify


# ── The visibility score is a range over a real sample ─────────────────────

def _payload(monkeypatch, db_path, *, answers, place_id="ChIJx", neighborhood="Geneva",
             name="Gia Mia"):
    """Run the real visibility path with Perplexity and Places stubbed."""
    import client_api
    real = models.get_conn
    for mod in (models, client_api, notify):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    conn = real(db_path)
    conn.execute("INSERT INTO restaurants (id,name,owner_email,google_place_id,neighborhood,"
                 "vibe,known_for) VALUES (1,?,?,?,?,'lively pizza bar','wood-fired pizza')",
                 (name, "o@x.test", place_id, neighborhood))
    conn.commit()
    conn.close()
    monkeypatch.setattr(client_api, "get_restaurant",
                        lambda rid: models.get_restaurant(rid, db_path))
    monkeypatch.setattr(client_api, "_city_from_place_id", lambda pid: "Geneva" if pid else "")
    monkeypatch.setattr(client_api, "get_review_stats",
                        lambda rid: {"total": 10, "response_rate": 50})
    client_api._aivis_cache.clear()

    seq = list(answers)

    class _Resp:
        status_code = 200
        def json(self):
            a = seq.pop(0) if seq else None
            if a is None:
                return {}
            return {"choices": [{"message": {"content": a}}], "citations": ["https://x.test"]}

    monkeypatch.setattr(client_api, "get_restaurant",
                        lambda rid: models.get_restaurant(rid, db_path))
    import requests as _rq
    monkeypatch.setattr(_rq, "post", lambda *a, **kw: _Resp())
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    monkeypatch.setattr(client_api, "ai_budget_exceeded", lambda rid: None, raising=False)
    payload, _ = client_api._do_ai_visibility_inner(1, force=True)
    return payload


def test_a_total_outage_reports_no_score_not_zero(db_path, monkeypatch):
    """Every query failing is an outage on our side. Scoring it zero and
    labelling it "Not yet indexed by AI search" states a conclusion about
    the restaurant that the data cannot support."""
    p = _payload(monkeypatch, db_path, answers=[None] * 12)
    assert p["ai_score"] is None
    assert p["answered_queries"] == 0
    assert p["partial"] is True


def test_a_complete_run_reports_a_range_not_a_point(db_path, monkeypatch):
    """Appearances over a handful of questions answered by a
    non-deterministic model is a proportion with error bars, not a figure."""
    hit = "Gia Mia in Geneva is excellent."
    miss = "Try Somewhere Else in Naperville."
    # Eight questions now: seven discovery/cuisine/occasion/practical, plus
    # one branded, which is scored apart.
    p = _payload(monkeypatch, db_path,
                 answers=[hit, miss, hit, miss, hit, miss, miss, hit])
    assert p["partial"] is False
    assert p["ai_score"] is not None
    assert p["ai_score_low"] is not None and p["ai_score_high"] is not None
    assert p["ai_score_low"] <= p["ai_score"] <= p["ai_score_high"]


def test_the_query_set_is_wide_enough_to_move_less_than_a_third(db_path, monkeypatch):
    """Three queries gave the score four possible values, so one question
    changing its mind moved it 33 points and cleared the alert threshold."""
    p = _payload(monkeypatch, db_path, answers=["x"] * 12)
    assert p["total_queries"] >= 6


def test_branded_recall_is_scored_apart_from_discovery(db_path, monkeypatch):
    """"Tell me about X" is not evidence that an open search would surface
    you. Both were counted in one number, which answered neither."""
    hit = "Gia Mia in Geneva is excellent."
    miss = "Try Somewhere Else in Naperville."
    p = _payload(monkeypatch, db_path, answers=[miss] * 7 + [hit])
    assert p["branded_queries"] == 1
    assert p["branded_score"] == 100
    assert p["ai_score"] == 0, "a branded hit must not lift the discovery score"


def test_the_queries_cover_more_than_one_intent(db_path, monkeypatch):
    """Every question used to be a superlative local-discovery question, so
    a model leaned on listicles and high-review-count venues and a small
    independent scored zero because of how the questions were written."""
    p = _payload(monkeypatch, db_path, answers=["x"] * 12)
    kinds = {q.get("kind") for q in p["queries"]}
    assert len(kinds) >= 4, f"only {kinds} intents covered"
    assert "branded" in kinds


def test_no_city_is_reported_rather_than_scored_zero(db_path, monkeypatch):
    """Without a city two locations of one brand are indistinguishable in an
    answer, so appearance cannot be judged at all."""
    import client_api
    monkeypatch.setattr(client_api, "_city_from_place_id", lambda pid: "")
    p = _payload(monkeypatch, db_path, answers=["x"] * 12, place_id="", neighborhood="")
    assert p["location_known"] is False


def test_the_city_comes_from_google_not_the_profile_text(db_path, monkeypatch):
    """neighborhood is free text. An owner who typed "West Loop" produced
    queries about a place called West Loop, and norm_city then gated every
    appearance match."""
    p = _payload(monkeypatch, db_path, answers=["x"] * 12, neighborhood="West Loop")
    assert p["city"] == "Geneva"
    assert p["city_source"] == "google"


def test_a_profile_derived_city_says_so(db_path, monkeypatch):
    import client_api
    monkeypatch.setattr(client_api, "_city_from_place_id", lambda pid: "")
    p = _payload(monkeypatch, db_path, answers=["x"] * 12, place_id="", neighborhood="Geneva")
    assert p["city_source"] == "profile"


# ── The score stops counting our own configuration ─────────────────────────

def test_presence_and_setup_are_scored_separately(db_path, monkeypatch):
    """gbp_score counted "is a Yelp ID typed into Cavnar AI" in the same
    percentage as "does your Google listing have opening hours", and called
    the result AI visibility."""
    p = _payload(monkeypatch, db_path, answers=["x"] * 12)
    assert "presence_score" in p and "setup_done" in p and "setup_total" in p
    assert p["setup_total"] > 0
    # Every item must be tagged. An untagged one silently falls out of both
    # scores, which is how a real signal disappears without anyone noticing.
    kinds = {i.get("kind") for i in p["checklist"]}
    assert kinds == {"presence", "setup"}, f"untagged: {[i['label'] for i in p['checklist'] if not i.get('kind')]}"
    assert len(p["checklist"]) == len(
        [i for i in p["checklist"] if i.get("kind") in ("presence", "setup")])


def test_typing_a_yelp_id_does_not_move_the_presence_score(db_path, monkeypatch):
    p1 = _payload(monkeypatch, db_path, answers=["x"] * 12)
    before = p1["presence_score"]
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET yelp_business_id='abc' WHERE id=1")
    conn.commit()
    conn.close()
    import client_api
    client_api._aivis_cache.clear()
    p2, _ = client_api._do_ai_visibility_inner(1, force=True)
    assert p2["presence_score"] == before
    assert p2["setup_done"] == p1["setup_done"] + 1


def test_the_checklist_makes_no_unsourced_claim_about_third_party_ai(db_path, monkeypatch):
    """"Perplexity indexes Yelp heavily" and "this is the #1 driver of AI
    search ranking" were stated as fact with nothing behind them."""
    p = _payload(monkeypatch, db_path, answers=["x"] * 12)
    blob = " ".join((i.get("action") or "") + " " + (i.get("label") or "") for i in p["checklist"])
    for claim in ("indexes Yelp heavily", "AI visibility threshold",
                  "#1 driver of AI search", "boosts AI ranking",
                  "AI tools reward", "feeds AI search results"):
        assert claim not in blob, claim


def test_no_branch_of_the_checklist_asserts_how_third_party_ai_works():
    """Checking the rendered payload only covers the branches one fixture
    happens to hit — a restaurant with no Yelp ID never renders the "done"
    string, so restoring "Perplexity indexes Yelp heavily" there left the
    suite green. Read the source instead, which covers every branch.

    None of these is sourced, dated or verifiable, and this module holds no
    evidence for any of them. They were rendered beside a genuine
    measurement, in the same weight.
    """
    import inspect
    import client_api
    src = inspect.getsource(client_api._do_ai_visibility_inner)
    banned = [
        "indexes Yelp heavily",
        "AI visibility threshold",
        "#1 driver of AI search",
        "boosts AI ranking",
        "AI tools reward",
        "feeds AI search results",
        "AI tools can index you",
        "AI tools can find your location",
        "AI tools can surface your menu",
    ]
    found = [b for b in banned if b in src]
    assert not found, f"unsourced claims about third-party AI: {found}"


def test_claim_kinds_travel_with_the_numbers(db_path, monkeypatch):
    p = _payload(monkeypatch, db_path, answers=["x"] * 12)
    assert p["claim_kinds"]["presence_score"] == "measured"
    assert p["claim_kinds"]["setup_done"] == "configuration"


# ── The drop alert needs a real change, not model variance ─────────────────

def _runs(pairs):
    return [{"ai_score": s, "answered": n, "appeared": a} for s, n, a in pairs]


def test_one_question_flipping_is_not_a_drop():
    """The old bar was 15 points on a 3-question score whose only values
    were 0, 33, 67 and 100."""
    assert notify._ai_visibility_drop(_runs([(67, 6, 4), (83, 6, 5)])) is None


def test_a_real_collapse_still_alerts():
    got = notify._ai_visibility_drop(_runs([(17, 6, 1), (83, 6, 5)]))
    assert got is not None
    assert got[0] == 17 and got[1] == 83 and got[2] >= 2


def test_a_sample_too_small_to_judge_never_alerts():
    assert notify._ai_visibility_drop(_runs([(0, 3, 0), (100, 3, 3)])) is None


def test_runs_with_no_recorded_sample_never_alert():
    assert notify._ai_visibility_drop(
        [{"ai_score": 20, "answered": None, "appeared": None},
         {"ai_score": 90, "answered": None, "appeared": None}]) is None


def test_two_runs_over_different_sample_sizes_are_compared_as_rates():
    """5 of 10 then 3 of 6 is the same rate — not a drop."""
    assert notify._ai_visibility_drop(_runs([(50, 6, 3), (50, 10, 5)])) is None


# ── Competitor identity ────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("Simple EJ's", "Simple EJs Sports Bar & Grill"),
    ("Gia Mia", "Gia Mia Pizza Bar"),
    ("The Copper Table", "Copper Table Restaurant"),
])
def test_a_duplicate_listing_of_yourself_is_not_a_competitor(a, b):
    """Self-exclusion was an exact name match, so a duplicate Google listing
    of the same restaurant became its own competitor."""
    assert competitor._same_business(a, b) is True


@pytest.mark.parametrize("a,b", [("Lou's Diner", "Tony's Pizza"), ("Gia Mia", "Mia Bella")])
def test_two_different_restaurants_are_not_merged(a, b):
    assert competitor._same_business(a, b) is False


@pytest.mark.parametrize("a,b", [
    ("Bar", "Bar Louie"),
    ("Mia", "Gia Mia"),
    ("Lou's", "Lou's Diner"),
])
def test_a_short_name_does_not_swallow_a_real_competitor(a, b):
    """Containment was added so a duplicate listing of the restaurant itself
    stopped being its own competitor. Applied to a very short name it ran
    backwards: a restaurant genuinely called "Bar" excluded Bar Louie from
    its own competitor set. Containment now needs the shorter name to be
    distinctive enough to be a business name on its own."""
    assert competitor._same_business(a, b) is False


@pytest.mark.parametrize("name", [
    "Applebee's Grill + Bar", "Chili's", "Buffalo Wild Wings", "Texas Roadhouse",
    "Olive Garden Italian Restaurant", "IHOP", "Cracker Barrel", "McDonald's",
])
def test_casual_dining_chains_are_excluded_too(name):
    """The list was 16 fast-food brands, so every casual-dining chain passed
    straight through as a local competitor."""
    assert competitor._is_chain(name) is True


@pytest.mark.parametrize("name", ["Joe's Corner Tavern", "The Copper Table", "Gia Mia"])
def test_an_independent_is_not_called_a_chain(name):
    assert competitor._is_chain(name) is False


def test_distance_is_computed_for_provenance():
    d = competitor._distance_m(41.8781, -87.6298, 41.8881, -87.6298)
    assert 1000 < d < 1300


# ── The insight cannot invent a competitor or a figure ─────────────────────

def _comps():
    return [{"name": "Lou's Diner", "place_id": "p1", "rating": 4.2, "review_count": 300},
            {"name": "Tony's Pizza", "place_id": "p2", "rating": 4.5, "review_count": 120}]


def test_a_restaurant_that_was_never_in_the_list_is_flagged():
    text = "Lou's Diner is strong on service. Marco Polo Bistro is winning on price."
    assert "Marco Polo Bistro" in competitor._invented_competitors(text, _comps())


def test_the_real_competitors_are_not_flagged():
    text = "Lou's Diner is strong on service. Tony's Pizza has slow waits."
    assert competitor._invented_competitors(text, _comps()) == []


def test_section_headers_are_not_mistaken_for_businesses():
    text = "WHAT COMPETITORS ARE DOING WELL:\n- Lou's Diner nails the breakfast rush."
    assert competitor._invented_competitors(text, _comps()) == []


def test_an_unsupported_figure_is_marked_on_the_insight(monkeypatch):
    """Every other AI insight in this codebase runs through verify_figures.
    The one whose prompt says "Always use $ signs before dollar amounts"
    did not."""
    monkeypatch.setattr(competitor, "create_with_retry", lambda *a, **kw: types.SimpleNamespace(
        content=[types.SimpleNamespace(text="Lou's Diner is beating you by $4,300 a week.")],
        stop_reason="end_turn"))
    monkeypatch.setattr(competitor, "ANTHROPIC_KEY", "k", raising=False)
    out = competitor.generate_competitor_insight("Mine", _comps(), restaurant_id=1)
    assert "UNVERIFIED" in out


def test_a_clean_insight_carries_no_warning(monkeypatch):
    monkeypatch.setattr(competitor, "create_with_retry", lambda *a, **kw: types.SimpleNamespace(
        content=[types.SimpleNamespace(text="Lou's Diner nails the breakfast rush. Tony's Pizza has slow waits.")],
        stop_reason="end_turn"))
    monkeypatch.setattr(competitor, "ANTHROPIC_KEY", "k", raising=False)
    out = competitor.generate_competitor_insight("Mine", _comps(), restaurant_id=1)
    assert "UNVERIFIED" not in out


# ── Credentials never reach a client ───────────────────────────────────────

def test_an_api_key_is_stripped_from_client_facing_error_text():
    """A requests exception carries the failing URL, and every Places URL in
    this codebase carries key= in its query string. Six handlers returned
    str(e) straight to the browser."""
    msg = ai_guard.safe_error(Exception(
        "403 Client Error for url: https://maps.googleapis.com/x?place_id=P&key=AIzaSyREAL123"))
    assert "AIzaSyREAL123" not in msg
    assert "[redacted]" in msg


def test_a_bearer_token_is_stripped_too():
    msg = ai_guard.safe_error(Exception("401 for Authorization: Bearer pplx-abc123def"))
    assert "pplx-abc123def" not in msg


def test_an_empty_exception_gets_a_usable_fallback():
    assert ai_guard.safe_error(Exception(""), "Nope.") == "Nope."


# ── Competitor history ─────────────────────────────────────────────────────

def test_a_competitor_rating_change_becomes_visible(db_path):
    """competitor_intel is one JSON blob overwritten every Monday, so a
    rating sliding from 4.6 to 4.1 over two months was thrown away each
    time — the most useful thing this module could say."""
    models.record_competitor_snapshot(1, [
        {"place_id": "p1", "name": "Lou's", "rating": 4.6, "review_count": 300}], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE competitor_snapshots SET captured_at = datetime('now','-30 days')")
    conn.commit()
    conn.close()
    models.record_competitor_snapshot(1, [
        {"place_id": "p1", "name": "Lou's", "rating": 4.1, "review_count": 360}], db_path=db_path)
    moves = models.competitor_movement(1, days=60, db_path=db_path)
    assert len(moves) == 1
    assert moves[0]["rating_change"] == pytest.approx(-0.5)
    assert moves[0]["reviews_added"] == 60


def test_a_single_snapshot_is_not_a_movement(db_path):
    models.record_competitor_snapshot(1, [
        {"place_id": "p1", "name": "Lou's", "rating": 4.6, "review_count": 300}], db_path=db_path)
    assert models.competitor_movement(1, days=60, db_path=db_path) == []


def test_a_deleted_restaurant_is_not_served_from_the_visibility_cache(db_path, monkeypatch):
    """The cache lives six hours in process memory. A restaurant that was
    deleted or deactivated kept being served a full 200 payload of its own
    intelligence until the entry aged out — whether a restaurant exists is
    not something a cache of its results can answer."""
    import client_api
    p = _payload(monkeypatch, db_path, answers=["x"] * 12)
    assert p["ok"] is True
    conn = models.get_conn(db_path)
    conn.execute("DELETE FROM restaurants WHERE id=1")
    conn.commit()
    conn.close()
    payload, status = client_api._do_ai_visibility_inner(1)
    assert status == 404
    assert payload["ok"] is False


# ── Audit #12: what the module says it measured, and what it kept ──────────

def test_the_payload_names_the_system_that_was_actually_asked(db_path, monkeypatch):
    """One vendor is sampled. The interface called the result "AI search"
    and the roadmap named three platforms that are never queried."""
    p = _payload(monkeypatch, db_path, answers=["x"] * 12)
    assert p["platform"] == "Perplexity"
    assert p["model"]


def test_only_one_ai_endpoint_exists_in_the_visibility_path():
    """A second platform would need a second endpoint. This pins the claim
    the interface is allowed to make."""
    import inspect
    import client_api
    src = inspect.getsource(client_api._do_ai_visibility_inner)
    for other in ("api.openai.com", "generativelanguage.googleapis",
                  "api.anthropic.com", "bing.com"):
        assert other not in src, f"{other} is queried but the copy may not say so"
    assert "api.perplexity.ai" in src


def test_no_surface_claims_a_platform_it_does_not_query():
    """Four roadmap strings named ChatGPT and Google AI and asserted how
    each sources its answers. None was sourced; the module queries neither.

    Reads the files rather than a rendered payload, because a fixture only
    exercises the branches it happens to hit."""
    import pathlib as _pl
    root = _pl.Path(__file__).resolve().parent.parent
    targets = [
        root / "ios/CavnarAI/CavnarAI/Features/Intel/AIVisibilitySection.swift",
        root / "templates/dashboard.html",
    ]
    banned = [
        "Perplexity, ChatGPT, and Google AI",
        "Perplexity, ChatGPT and Google AI",
        "AI search tools rank restaurants",
        "more likely to be cited by AI tools",
        "the online footprint AI needs to find you",
        "indexable by AI search",
        "Not yet indexed by AI search",
    ]
    for t in targets:
        if not t.exists():
            continue
        # Only what a user can read. A comment quoting the removed string —
        # which is exactly how these fixes are documented — is not a claim
        # the product makes, and scanning raw text cannot tell the two
        # apart.
        live = []
        in_html_comment = False
        for line in t.read_text().split("\n"):
            stripped = line.strip()
            if t.suffix == ".swift":
                if stripped.startswith("//"):
                    continue
                line = line.split("//")[0] if '"' not in line.split("//")[0] else line
            else:
                if "<!--" in line:
                    in_html_comment = "-->" not in line
                    line = line.split("<!--")[0]
                elif in_html_comment:
                    if "-->" in line:
                        in_html_comment = False
                        line = line.split("-->", 1)[1]
                    else:
                        continue
            live.append(line)
        body = "\n".join(live)
        found = [b for b in banned if b in body]
        assert not found, f"{t.name}: {found}"


def test_every_checklist_item_states_its_effort(db_path, monkeypatch):
    """Each item was worth an identical share, so "add a phone number" and
    "build to 50+ reviews" read as equally weighted — one is two minutes and
    the other is a year."""
    p = _payload(monkeypatch, db_path, answers=["x"] * 12)
    missing = [i["label"] for i in p["checklist"] if not i.get("effort")]
    assert not missing, missing
    assert all(i.get("why_it_matters") for i in p["checklist"])


def test_a_run_writes_down_what_it_asked(db_path, monkeypatch):
    """ai_visibility_runs stored a score and nothing else, so a change could
    never be explained — while the drop alert told the owner to open Intel
    and see which questions changed. The questions were never written down."""
    hit = "Gia Mia in Geneva is excellent."
    _payload(monkeypatch, db_path, answers=[hit] * 8)
    conn = models.get_conn(db_path)
    rows = conn.execute("SELECT query, appeared, sources, query_kind "
                        "FROM ai_visibility_query_runs").fetchall()
    conn.close()
    assert len(rows) == 8
    assert any(r["appeared"] for r in rows)
    assert any(json.loads(r["sources"] or "[]") for r in rows), "citations were not kept"
    assert {r["query_kind"] for r in rows} >= {"discovery", "branded"}


def test_the_diff_names_the_questions_that_stopped_mentioning_you(db_path, monkeypatch):
    hit = "Gia Mia in Geneva is excellent."
    miss = "Try Somewhere Else in Naperville."
    _payload(monkeypatch, db_path, answers=[hit] * 8)
    import client_api
    client_api._aivis_cache.clear()
    _payload2 = _payload  # same helper, second run on the same restaurant
    conn = models.get_conn(db_path)
    conn.execute("DELETE FROM restaurants WHERE id=1")
    conn.commit()
    conn.close()
    # Re-seed and run again with misses so the diff has something to find.
    p2 = _payload(monkeypatch, db_path, answers=[miss] * 7 + [hit])
    diff = models.ai_visibility_query_diff(1, db_path=db_path)
    assert diff["ok"] is True
    assert diff["lost"], "nothing recorded as lost between two runs"


def test_a_single_run_is_not_a_comparison(db_path, monkeypatch):
    _payload(monkeypatch, db_path, answers=["x"] * 8)
    diff = models.ai_visibility_query_diff(1, db_path=db_path)
    assert diff["ok"] is False


def test_the_citations_behind_a_run_can_be_read_back(db_path, monkeypatch):
    hit = "Gia Mia in Geneva is excellent."
    _payload(monkeypatch, db_path, answers=[hit] * 8)
    assert models.ai_visibility_sources(1, db_path=db_path) == ["https://x.test"]


def test_competitor_appearances_come_from_the_same_answers(db_path, monkeypatch):
    """The answers are ranked restaurant lists and the competitor set is
    already on file with Place IDs. Nothing cross-referenced them."""
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id,name,owner_email,google_place_id,neighborhood,"
                 "vibe,known_for,competitor_intel) VALUES (2,'Mine','o2@x.test','ChIJy','Geneva',"
                 "'lively','pizza',?)",
                 (json.dumps({"competitors": [{"name": "Lou's Diner", "place_id": "p1"},
                                              {"name": "Tony's Pizza", "place_id": "p2"}]}),))
    conn.commit()
    conn.close()
    import client_api
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(client_api, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(client_api, "get_restaurant", lambda rid: models.get_restaurant(rid, db_path))
    monkeypatch.setattr(client_api, "_city_from_place_id", lambda pid: "Geneva")
    monkeypatch.setattr(client_api, "get_review_stats", lambda rid: {"total": 10, "response_rate": 50})
    monkeypatch.setattr(client_api, "ai_budget_exceeded", lambda rid: None, raising=False)
    client_api._aivis_cache.clear()

    class _Resp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {
                "content": "Try Lou's Diner in Geneva, or Tony's Pizza in Geneva."}}],
                "citations": []}
    import requests as _rq
    monkeypatch.setattr(_rq, "post", lambda *a, **kw: _Resp())
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    p, _ = client_api._do_ai_visibility_inner(2, force=True)
    names = {c["name"] for c in p["competitor_appearances"]}
    assert names == {"Lou's Diner", "Tony's Pizza"}
    assert all(c["queries"] > 0 for c in p["competitor_appearances"])


def test_the_city_cache_expires(monkeypatch):
    """It never did, so a restaurant that relocated kept the old city — and
    the city gates every appearance match."""
    import client_api
    assert client_api._CITY_CACHE_SECS > 0
    client_api._city_cache.clear()
    monkeypatch.setattr(client_api, "_CITY_CACHE_SECS", 0)
    calls = []

    class _R:
        status_code = 200
        def json(self):
            calls.append(1)
            return {"status": "OK", "result": {"address_components": [
                {"types": ["locality"], "long_name": "Geneva"}]}}
    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda *a, **kw: _R())
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    client_api._city_from_place_id("P1")
    client_api._city_from_place_id("P1")
    assert len(calls) == 2, "the cache never expired"


def test_hedges_that_change_certainty_are_not_stripped():
    """_clean_ai_answer removed "The results suggest" and "I found that",
    which turns a tentative answer into a confident one."""
    import inspect
    import client_api
    src = inspect.getsource(client_api._do_ai_visibility_inner)
    assert "results? (?:show|indicate|suggest|reveal)" not in src
    assert "I (?:found|can see|notice|see) that" not in src


def test_no_route_hands_a_client_a_raw_exception():
    """A requests error carries the failing URL, and a Places URL carries
    key= in its query string. Forty-eight sites returned str(e) directly."""
    import pathlib as _pl
    root = _pl.Path(__file__).resolve().parent.parent
    offenders = []
    for name in ("mobile_api.py", "client_api.py", "admin_routes.py",
                 "social_routes.py", "webhook_routes.py", "audit_app.py"):
        f = root / name
        if f.exists() and "error=str(e)" in f.read_text():
            offenders.append(name)
    assert not offenders, offenders
