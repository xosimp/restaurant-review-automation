"""Audit #13 — the consultant layer.

The finding this file exists for: the Reviews module measured honestly and
summarised carefully, and stopped at the sentence an owner already knew.
Every substantive claim in its AI output had been computed in Python before
the model was called, so the model's whole job was rephrasing four pre-formed
strings at twenty words each.

These tests hold the machinery that takes the next step — the entity
extraction that makes a dish-level pattern representable, the clusters, the
cross-module context, the confidence, the bounded money, and the root-cause
pass — to the same standard the module already held its counts to: a claim
only exists when the evidence for it does.
"""
import inspect
import json

import pytest

import analyser
import client_api
import models
import reporter
import review_intelligence as ri


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, ri):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ri, "DB_PATH", db_path)
    yield


def _restaurant(db_path, rid=1, **kw):
    conn = models.get_conn(db_path)
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test"}
    cols.update(kw)
    keys = ",".join(cols)
    marks = ",".join("?" for _ in cols)
    conn.execute(f"INSERT INTO restaurants ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _review(db_path, rid, *, ext, rating=2, days_ago=3, sentiment="negative",
            cats='["food_quality"]', entities=None, complaint=None,
            severity="operational", status="pending", processed=1):
    conn = models.get_conn(db_path)
    conn.execute(
        """INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
           review_date, fetched_at, sentiment, categories, summary, urgency, processed,
           response_status, entities, specific_complaint, severity)
           VALUES (?, 'google', ?, ?, ?, 'the entree arrived cold again',
                   date('now', ?), datetime('now'), ?, ?, 'a summary', 'normal', ?, ?, ?, ?, ?)""",
        (rid, ext, f"Guest{ext}", rating, f"-{days_ago} days", sentiment, cats, processed,
         status, json.dumps(entities) if entities else None, complaint, severity))
    conn.commit()
    conn.close()


# ── The analyser now stores the operational dimension ───────────────────────

def test_the_analyser_asks_for_a_dish_a_role_and_a_daypart():
    """Every downstream question an owner actually asks — which dish, which
    shift, which role — was unanswerable at any price, because the detail was
    discarded at the moment of analysis and the text was never shown to a
    model again."""
    p = analyser.ANALYSE_PROMPT
    for token in ("entities", "dishes", "staff_roles", "daypart",
                  "service_mode", "specific_complaint", "severity"):
        assert token in p, f"the analyser no longer asks for {token}"
    # And it is told, in the prompt, not to guess any of them.
    assert "EXTRACTION RULES" in p
    assert "never infer a dish from a category" in p.lower()


def test_an_invented_entity_is_dropped_not_stored():
    """Same discipline `categories` already had: an un-enumerated value is a
    cluster of one that no trend query will group with anything, and it would
    be rendered to the owner as if it were one of ours."""
    out = analyser._validate_entities({
        "dishes": ["Truffle Fries", "x", "a whole sentence that is clearly the model paraphrasing the review"],
        "staff_roles": ["server", "sous chef", "BARTENDER"],
        "daypart": "second breakfast",
        "service_mode": "dine_in",
    })
    assert out["dishes"] == ["truffle fries"], "short, long and sentence-shaped dishes must all be dropped"
    assert out["staff_roles"] == ["server", "bartender"], "'sous chef' is not in the vocabulary"
    assert "daypart" not in out, "an unknown daypart is absent, not stored"
    assert out["service_mode"] == "dine_in"


def test_entities_are_absent_rather_than_empty_when_nothing_was_named():
    """A review that named nothing is the common case and the correct one.
    An empty shape would read downstream like a measured result."""
    assert analyser._validate_entities({"dishes": [], "staff_roles": []}) is None
    assert analyser._validate_entities(None) is None
    assert analyser._validate_entities("not a dict") is None


def test_severity_cannot_contradict_the_urgency_that_already_fired():
    """urgency='high' is defined by safety, injury, legal threat or
    misconduct. A 'minor' severity on the same row would make the priority
    ordering disagree with the alert that already went out."""
    assert analyser._severity_floor(1, "high", "minor") == "safety"
    assert analyser._severity_floor(1, "high", "legal") == "legal"
    # And a 5-star with no complaint is not an operational failure, whatever
    # prose the model produced.
    assert analyser._severity_floor(5, "normal", "operational") == "minor"
    assert analyser._severity_floor(2, "normal", "service") == "service"


def test_the_analysis_result_carries_the_new_fields_through():
    out = analyser._validate_analysis({
        "sentiment": "negative", "categories": ["food_quality"],
        "summary": "cold entree", "urgency": "normal",
        "severity": "operational", "specific_complaint": "entree served cold",
        "entities": {"dishes": ["ribeye"], "daypart": "dinner"},
    }, rating=2)
    assert out["severity"] == "operational"
    assert out["specific_complaint"] == "entree served cold"
    assert out["entities"]["dishes"] == ["ribeye"]


def test_update_analysis_persists_entities(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="a", entities=None, complaint=None, severity=None)
    conn = models.get_conn(db_path)
    rid = conn.execute("SELECT id FROM reviews").fetchone()["id"]
    conn.close()
    models.update_analysis(rid, "negative", ["food_quality"], "s", "normal",
                           db_path=db_path,
                           entities={"dishes": ["ribeye"], "daypart": "dinner"},
                           specific_complaint="cold entree", severity="operational")
    row = models.get_reviews_data(1)[0]
    assert row["entities"]["dishes"] == ["ribeye"]
    assert row["specific_complaint"] == "cold entree"
    assert row["severity"] == "operational"
    assert row["severity_label"] == "Operational failure"


# ── Clusters: the grain the module never had ────────────────────────────────

def test_a_cluster_needs_the_same_floor_every_other_trend_claim_has(db_path):
    _restaurant(db_path)
    for i in range(2):
        _review(db_path, 1, ext=f"f{i}")
    assert ri.complaint_clusters(1) == [], "two reviews is not a pattern"
    _review(db_path, 1, ext="f2")
    assert len(ri.complaint_clusters(1)) == 1


def test_a_concentration_is_only_reported_when_it_is_actually_dominant(db_path):
    """A 'concentration' holding 2 of 11 mentions is the shape of random
    scatter. Naming it would hand the model a pattern to explain that is not
    there."""
    _restaurant(db_path)
    # Six complaints, six different dishes — no dish concentration.
    for i in range(6):
        _review(db_path, 1, ext=f"s{i}", entities={"dishes": [f"dish{i}"]})
    c = ri.complaint_clusters(1)[0]
    assert c["dish"] is None
    # Now six complaints all naming the same dish.
    _restaurant(db_path, rid=2)
    for i in range(6):
        _review(db_path, 2, ext=f"t{i}", entities={"dishes": ["the ribeye"]})
    c2 = ri.complaint_clusters(2)[0]
    assert c2["dish"]["value"] == "the ribeye"
    assert c2["dish"]["count"] == 6


def test_clusters_rank_by_severity_before_volume(db_path):
    """Mention count is not severity. A safety report in a three-review
    cluster outranks a twelve-review service gripe."""
    _restaurant(db_path)
    for i in range(8):
        _review(db_path, 1, ext=f"v{i}", cats='["service"]', severity="service")
    for i in range(3):
        _review(db_path, 1, ext=f"h{i}", cats='["cleanliness"]', severity="safety")
    order = [c["category"] for c in ri.complaint_clusters(1)]
    assert order[0] == "cleanliness", order


def test_a_cluster_carries_the_review_ids_behind_it(db_path):
    _restaurant(db_path)
    for i in range(4):
        _review(db_path, 1, ext=f"e{i}", complaint="entree served cold")
    c = ri.complaint_clusters(1)[0]
    assert len(c["review_ids"]) == 4
    assert all(isinstance(i, int) for i in c["review_ids"])
    assert c["complaints"], "the guests' own words travel with the cluster"


# ── Trend confidence, reusing the scorer that already exists ───────────────

def test_the_rating_trend_reuses_waste_trends_confidence_scorer():
    """The old call was `all(ratings[i] <= ratings[i+1])` over three weekly
    means, stated with identical certainty whether it rested on 9 reviews or
    900. waste_trend already solved this exact problem for waste dollars."""
    src = inspect.getsource(ri.rating_trend)
    assert "from waste_trend import _confidence, _anomalies" in src
    assert "MIN_TREND_REVIEWS_PER_WEEK" in src


def test_a_trend_with_too_little_behind_it_says_so_instead_of_guessing(db_path):
    _restaurant(db_path)
    for i in range(3):
        _review(db_path, 1, ext=f"w{i}", days_ago=3 + i * 7)
    t = ri.rating_trend(1)
    assert t["direction"] is None
    assert t["confidence"] is None
    assert "not enough to call a direction" in (t["reason"] or "")


def test_a_real_decline_gets_a_direction_and_a_confidence(db_path):
    _restaurant(db_path)
    # Five weeks, four reviews each, ratings walking down as time moves
    # forward. week 0 is the OLDEST here (largest days_ago), so the oldest
    # week is the 5-star one and the most recent is the 1-star one.
    for week in range(5):
        for n in range(4):
            _review(db_path, 1, ext=f"d{week}-{n}", rating=max(1, 5 - week),
                    days_ago=3 + (4 - week) * 7)
    t = ri.rating_trend(1)
    assert t["direction"] == "declining"
    assert t["confidence"] in ("high", "medium", "low")
    assert t["weeks_above_floor"] >= 4


# ── Money: bounded, sourced, or absent ──────────────────────────────────────

def test_no_revenue_estimate_without_a_revenue_base(db_path):
    """An estimate with no sales behind it is arithmetic on nothing."""
    _restaurant(db_path)
    for week in range(4):
        for n in range(4):
            _review(db_path, 1, ext=f"m{week}-{n}", rating=5 - (week > 1),
                    days_ago=3 + week * 7)
    out = ri.revenue_at_risk(1)
    assert out["available"] is False
    assert "sales" in out["reason"] or "reviews" in out["reason"]


def test_no_revenue_estimate_when_the_rating_barely_moved(db_path):
    _restaurant(db_path)
    conn = models.get_conn(db_path)
    for d in range(60):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales) "
                     "VALUES (1, date('now', ?), 5000)", (f"-{d} days",))
    conn.commit()
    conn.close()
    for i in range(20):
        _review(db_path, 1, ext=f"q{i}", rating=4, days_ago=5 + i)
    for i in range(20):
        _review(db_path, 1, ext=f"r{i}", rating=4, days_ago=40 + i)
    out = ri.revenue_at_risk(1)
    assert out["available"] is False
    assert "noise" in out["reason"]


def test_a_revenue_estimate_is_a_range_that_shows_its_inputs(db_path):
    _restaurant(db_path)
    conn = models.get_conn(db_path)
    for d in range(90):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales) "
                     "VALUES (1, date('now', ?), 5000)", (f"-{d} days",))
    conn.commit()
    conn.close()
    for i in range(20):
        _review(db_path, 1, ext=f"lo{i}", rating=2, days_ago=5 + i)
    for i in range(20):
        _review(db_path, 1, ext=f"hi{i}", rating=5, days_ago=40 + i)
    out = ri.revenue_at_risk(1)
    assert out["available"] is True
    assert out["claim_kind"] == "forecast", "a projection must be labelled one"
    assert out["monthly_low"] < out["monthly_high"]
    assert out["direction"] == "at_risk"
    assert out["sales_source"], "the owner must be able to see what it was computed from"
    assert "not a measurement" in out["assumption"]


# ── Cross-module context ────────────────────────────────────────────────────

def test_sample_data_is_refused_rather_than_reported(db_path, monkeypatch):
    """Answering from the bundled placeholder pantry as if it were this
    restaurant's numbers is worse than saying nothing — ask_cavnar's own
    precedent."""
    _restaurant(db_path)
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant",
                        lambda rid: {"is_live": False, "overall_labor_pct": 31.0})
    ctx = ri.operational_context(1)
    assert ctx["labor"] is None
    assert any("sample data" in n for n in ctx["notes"])


def test_an_empty_operational_block_says_there_is_no_data(db_path):
    """An empty block reads to a model as 'nothing notable happened', which
    is a different claim from 'we have no data' — and it is the second that
    has to pull the confidence down."""
    block = ri._operational_block({"labor": None, "food_cost": None,
                                   "waste": None, "marketing": None, "notes": []})
    assert "NO operational evidence" in block
    assert "medium" in block, "it must cap the confidence it allows"


def test_the_insight_prompt_carries_the_other_modules_and_the_diagnosis():
    src = inspect.getsource(client_api._do_review_insight)
    assert "import review_intelligence as _ri" in src
    assert "WHAT THE OTHER MODULES RECORDED OVER THE SAME PERIOD" in src
    assert "DIAGNOSIS (a stored root-cause pass" in src
    # And it is forbidden from inventing a cause when there is no diagnosis.
    assert "Never assert a cause that is not in the DIAGNOSIS block" in src


def test_the_insight_reads_diagnoses_rather_than_generating_them():
    """A Sonnet call per cluster on the critical path of a tab open would make
    the tab slow and the bill large for an answer that changes on the
    timescale of days."""
    src = inspect.getsource(client_api._do_review_insight)
    assert "_ri.get_diagnoses(rid" in src
    assert "_ri.diagnose(" not in src


# ── Guest names: the P0 ─────────────────────────────────────────────────────

def test_the_prompt_now_holds_the_guest_names_its_example_asks_for():
    """The 'Do today' example named a guest ("Amanda L.") while the urgent
    query selected `text` and not `author` — so the model was shown a
    demonstration of naming a person and handed no people to name."""
    src = inspect.getsource(client_api._do_review_insight)
    assert "SELECT id, author, rating, text FROM reviews" in src
    assert "Name a guest ONLY from the urgent-review list above" in src


def test_a_name_that_was_never_in_the_input_is_caught():
    prompt = "Urgent reviews awaiting a reply: #12 Marcus (1 star): \"cold food\""
    assert client_api._verify_named_entities("Respond to Marcus about the cold food", prompt) == []
    assert "Amanda" in client_api._verify_named_entities(
        "Respond to Amanda L. about her 1-star review", prompt)


def test_the_name_check_does_not_fire_on_ordinary_prose():
    prompt = "Restaurant: Gia Mia | Today: September 18, 2026"
    for line in ("Ratings held steady this week across every platform.",
                 "Respond to the Google review about parking.",
                 "Friday dinner carried most of the complaints."):
        assert client_api._verify_named_entities(line, prompt) == [], line


# ── The diagnosis itself ────────────────────────────────────────────────────

def test_a_diagnosis_citing_a_review_we_never_gave_it_is_rejected():
    """A root-cause paragraph is only worth more than a summary because the
    owner can click through to the reviews behind it."""
    with pytest.raises(ValueError, match="cited no review"):
        ri._validate_diagnosis(
            {"cause": "the pass is backing up", "evidence_review_ids": [999, 1000],
             "confidence": "high"},
            allowed_ids={1, 2, 3}, prompt="", restaurant_id=1)


def test_a_diagnosis_keeps_only_the_ids_it_was_actually_given():
    out = ri._validate_diagnosis(
        {"cause": "the pass is backing up", "evidence_review_ids": [1, 999, 3],
         "confidence": "medium", "alternative_cause": "hold times at the window",
         "what_would_confirm": "watch two Friday services",
         "recommended_action": "put a manager on the pass",
         "expected_outcome": "cold-food mentions fall within two weeks"},
        allowed_ids={1, 2, 3}, prompt="", restaurant_id=1)
    assert out["evidence_review_ids"] == [1, 3]
    assert out["confidence"] == "medium"


def test_a_diagnosis_stating_an_unsourced_figure_is_flagged_and_downgraded():
    out = ri._validate_diagnosis(
        {"cause": "labor fell 40% on the affected shifts", "evidence_review_ids": [1],
         "confidence": "high"},
        allowed_ids={1}, prompt="Labor: 31.4% of sales", restaurant_id=1)
    assert out["unsupported_figures"], "40% was never in the input"
    assert out["confidence"] == "low", "an unverified figure cannot stay high confidence"


def test_an_unknown_confidence_falls_to_low_not_to_high():
    out = ri._validate_diagnosis(
        {"cause": "x", "evidence_review_ids": [1], "confidence": "certain"},
        allowed_ids={1}, prompt="", restaurant_id=1)
    assert out["confidence"] == "low"


def test_the_diagnose_prompt_requires_an_alternative_and_a_tiebreaker():
    """A single confident cause with nothing to weigh it against is exactly
    the shape of a plausible guess."""
    p = ri.DIAGNOSE_PROMPT
    assert "alternative_cause" in p
    assert "what_would_confirm" in p
    assert "Never write an id that is not on this page" in p
    assert "Correlation in a 90-day window is not proof" in p
    assert "A stated uncertainty is worth more than a confident guess" in p


def test_a_stored_diagnosis_round_trips_with_its_age(db_path):
    _restaurant(db_path)
    for i in range(4):
        _review(db_path, 1, ext=f"z{i}")
    cluster = ri.complaint_clusters(1)[0]
    result = {"cause": "the pass is backing up at peak",
              "alternative_cause": "hold times at the window",
              "what_would_confirm": "watch two Friday services",
              "evidence_review_ids": cluster["review_ids"][:2],
              "operational_evidence": [{"module": "labor", "metric": "labor %", "value": "31.4%"}],
              "confidence": "medium", "recommended_action": "put a manager on the pass",
              "expected_outcome": "cold-food mentions fall within two weeks"}
    ri._save_diagnosis(1, cluster, result, {"available": False}, db_path)
    stored = ri.get_diagnoses(1, db_path=db_path)
    assert len(stored) == 1
    d = stored[0]
    assert d["cause"] == result["cause"]
    assert d["evidence_review_ids"] == cluster["review_ids"][:2]
    assert d["operational_evidence"][0]["module"] == "labor"
    assert d["age_hours"] is not None
    assert d["stale"] is False


# ── The weekly digest ───────────────────────────────────────────────────────

def test_the_digest_no_longer_orders_a_line_for_a_module_with_no_data():
    """This instruction said, three times, that the model must write a line
    for every active module 'even if the data section above is thin or missing
    for it' — an instruction to manufacture prose out of a missing-data
    condition, in the one AI artefact that reaches the owner unattended."""
    src = inspect.getsource(reporter.generate_ai_digest_summary)
    assert "always write something specific and useful for it" not in src
    assert "even if the data section above is thin or missing" not in src
    assert "regardless of how much data is available" not in src
    # And the honest replacement is deterministic copy, not a generation.
    assert "_MODULE_GAP_COPY" in src
    assert "module_gap_lines" in src


def test_the_digest_drops_a_module_line_it_never_asked_for():
    """Enforced as well as asked for: a prompt rule is a request, and this
    email goes out with nobody reading it first."""
    src = inspect.getsource(reporter.generate_ai_digest_summary)
    assert "_allowed = {k.lower() for k in required_lines}" in src
    assert "that module reported no data this week" in src


def test_the_digest_correlations_compare_numbers_not_prose():
    """`"UP" in inventory_context` was true of a waste item called SOUP BASE,
    and `(pos + neg) > 3` was the entire evidentiary bar for telling an owner
    their kitchen is understaffed."""
    src = inspect.getsource(reporter.generate_ai_digest_summary)
    # Comments stripped: the docstring above and the code's own comments name
    # the old expressions on purpose, and matching those would make this test
    # pass on a file that still ran them.
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    assert '"UP" in inventory_context' not in code
    assert '"trending UP" in labor_context' not in code
    assert '"trending DOWN" in labor_context' not in code
    assert "(pos + neg) > 3" not in code
    assert "_facts[\"labor\"]" in code or "_facts['labor']" in code
    assert "MIN_TREND_REVIEWS_PER_WEEK as _MIN_WK_RPT" in code
    # And it may no longer assert causation — in the block it hands the
    # model, and in the rule it gives the model about that block.
    assert "these are CO-MOVEMENTS, not established" in code
    assert "caused the other" in code
    assert "Never say one caused the other." in code


def test_the_digest_escapes_the_ai_written_draft():
    """It was the one field on the card that wasn't — author, text, sentiment
    and platform all went through _html.escape and the AI's reply went in
    raw."""
    src = inspect.getsource(reporter._review_card)
    assert "_html.escape(r.draft_response)" in src


# ── Claim kinds and caching ────────────────────────────────────────────────

def test_the_insight_labels_what_kind_of_claim_each_part_is():
    """ai_guard.CLAIM_KINDS was written for exactly this and shipped by Intel
    and Food Cost; Reviews sent a measured figure, an inference and a
    projection as three identical lines of prose."""
    src = inspect.getsource(client_api._do_review_insight)
    assert "CLAIM_KINDS" in src
    assert '"claim_kinds": _kinds' in src
    assert '"this_week": "measured"' in src
    assert '_kinds["next_week"] = "forecast"' in src


def test_the_insight_caches_the_whole_payload_not_just_the_sentence():
    """Caching the string meant every caveat was computed, rendered once, and
    then dropped for five minutes while the text it qualified kept showing."""
    src = inspect.getsource(client_api._do_review_insight)
    assert '_cache_set("review-insight:" + str(rid), payload)' in src
    assert '_cache_set("review-insight:" + str(rid), insight)' not in src


def test_the_stale_fallback_says_how_old_it_is():
    """That path deliberately bypasses the TTL — a stale read beats no read —
    so it has to carry a date. ai_guard.freshness exists for this."""
    src = inspect.getsource(client_api._do_review_insight)
    assert "from ai_guard import freshness as _fresh_ri" in src
    assert 'out["stale"] = True' in src


# ── Severity reaches the surfaces ───────────────────────────────────────────

def test_severity_orders_the_inbox_ahead_of_sentiment(db_path):
    """A guest-safety report and a parking gripe were both simply 'negative'
    and sorted identically."""
    _restaurant(db_path)
    _review(db_path, 1, ext="minor", severity="minor", days_ago=1)
    _review(db_path, 1, ext="safety", severity="safety", days_ago=9)
    rows = models.get_reviews_data(1)
    assert rows[0]["severity"] == "safety", "the newer minor review must not outrank it"


def test_the_severity_breakdown_reports_what_it_cannot_classify(db_path):
    """Reviews analysed before the column existed. Reported rather than
    folded into 'minor', so a thin breakdown reads as thin coverage and not
    as a calm restaurant."""
    _restaurant(db_path)
    _review(db_path, 1, ext="a", severity="safety")
    _review(db_path, 1, ext="b", severity=None)
    out = ri.severity_breakdown(1)
    assert out["unclassified"] == 1
    safety = next(t for t in out["tiers"] if t["key"] == "safety")
    assert safety["open"] == 1


# ── Dayparts and weekdays ───────────────────────────────────────────────────

def test_a_thin_bucket_reports_its_counts_and_withholds_its_rate(db_path):
    """A 100% negative rate on one review is a number that will be read as a
    finding."""
    _restaurant(db_path)
    _review(db_path, 1, ext="one", entities={"daypart": "brunch"})
    out = ri.daypart_breakdown(1)
    brunch = next(d for d in out["by_daypart"] if d["daypart"] == "brunch")
    assert brunch["total"] == 1
    assert brunch["negative_pct"] is None
    assert brunch["below_floor"] is True


def test_a_daypart_with_enough_behind_it_gets_a_rate(db_path):
    _restaurant(db_path)
    for i in range(4):
        _review(db_path, 1, ext=f"dn{i}", entities={"daypart": "dinner"})
    out = ri.daypart_breakdown(1)
    dinner = next(d for d in out["by_daypart"] if d["daypart"] == "dinner")
    assert dinner["negative_pct"] == 100
    assert dinner["below_floor"] is False


# ── Benchmark and locations ────────────────────────────────────────────────

def test_the_benchmark_refuses_on_fewer_than_two_rated_competitors(db_path):
    _restaurant(db_path, competitor_intel=json.dumps(
        {"competitors": [{"name": "A", "rating": 4.5}]}))
    out = ri.competitor_benchmark(1)
    assert out["available"] is False


def test_the_benchmark_puts_our_rating_beside_the_median(db_path):
    _restaurant(db_path, competitor_intel=json.dumps({"competitors": [
        {"name": "A", "rating": 4.6}, {"name": "B", "rating": 4.2},
        {"name": "C", "rating": 4.4}]}))
    for i in range(5):
        _review(db_path, 1, ext=f"b{i}", rating=4, sentiment="neutral")
    out = ri.competitor_benchmark(1)
    assert out["available"] is True
    assert out["competitor_median"] == 4.4
    assert out["our_rating_90d"] == 4.0
    # The gap is like for like — our Google rating against theirs — or none
    # at all (re-audit M-18): the 90-day sample is reported, never compared.
    assert out["gap_vs_median"] is None
    c = models.get_conn(db_path)
    c.execute("UPDATE restaurants SET gbp_rating=4.0 WHERE id=1")
    c.commit(); c.close()
    out = ri.competitor_benchmark(1)
    assert out["gap_vs_median"] == pytest.approx(-0.4, abs=0.01)


def test_locations_compare_shares_not_raw_counts(db_path):
    """A location with three times the volume carries three times the
    complaints at identical quality."""
    _restaurant(db_path, rid=1, location_group="grp", owner_email="o@x.test")
    _restaurant(db_path, rid=2, location_group="grp", owner_email="o@x.test")
    for i in range(4):
        _review(db_path, 1, ext=f"l1{i}", cats='["wait_time"]')
    for i in range(12):
        _review(db_path, 2, ext=f"l2{i}", cats='["service"]')
    out = ri.location_comparison(1)
    assert out["available"] is True
    mine = next(l for l in out["locations"] if l["is_this_one"])
    assert mine["complaint_share"]["wait_time"] == 1.0
    assert any(o["category"] == "wait_time" for o in out["outlier_themes"])


def test_a_single_location_has_nothing_to_compare(db_path):
    _restaurant(db_path)
    assert ri.location_comparison(1)["available"] is False


# ── The executive questions ─────────────────────────────────────────────────

def test_the_executive_brief_answers_the_six_questions(db_path):
    _restaurant(db_path)
    for i in range(5):
        _review(db_path, 1, ext=f"x{i}", entities={"daypart": "dinner"},
                complaint="entree served cold")
    out = ri.executive_brief(1)
    assert out["biggest_problems"], "top problems must be answerable"
    assert out["fix_first"]["what"] == "food_quality"
    assert out["fix_first"]["evidence"], "a priority with no evidence is an opinion"
    assert out["hurting_reviews_most"]["category"] == "food_quality"
    assert "costing_money" in out
    assert out["discuss_tomorrow"]


def test_the_executive_brief_says_why_it_cannot_answer_rather_than_filling_in(db_path):
    _restaurant(db_path)
    out = ri.executive_brief(1)
    assert out["biggest_problems"] == []
    assert out["fix_first"] is None
    assert out["hurting_reviews_most"] is None
    assert out["costing_money"]["available"] is False
    assert out["costing_money"]["reason"], "an absent answer must say why"


# ── The scheduler runs the pass ─────────────────────────────────────────────

def test_the_diagnosis_pass_is_scheduled_and_survives_one_restaurant_failing():
    import scheduler
    src = inspect.getsource(scheduler.run_review_diagnoses)
    assert "for row in rows:" in src
    assert "except Exception as e:" in src
    assert "invalidate_insight_cache" in src, "a fresh cause must expire the cached read"
    loop = inspect.getsource(scheduler.run_scheduler) if hasattr(scheduler, "run_scheduler") else ""
    whole = inspect.getsource(scheduler)
    assert 'claim_period("review_diagnoses"' in whole
