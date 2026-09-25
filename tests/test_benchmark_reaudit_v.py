"""Benchmarking re-audit fix round, workstream V (validation & AI, 9/24/26):
one road for published figures to every model, the checker's comparability,
grammar, direction, metric binding and severity, recommendation priors by
organisation, no licence for general industry lore, numberless predictions,
the confirmed type, pattern membership, peer-band freshness, one market
comparison and the Reviews location comparison. Top-50 items 5, 6, 7, 8, 9,
14, 18, 19, 20, 21, 24, 30 and 42. (The phrasings of R3-1 / R3-2 / R3-3 /
R3-12 are golden cases in tests/fixtures/validation_corpus.)"""
import json
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant

# Imported before any fixture patches get_conn (CLAUDE.md's bound-import hazard).
import admin_routes  # noqa: E402
import ask_cavnar  # noqa: E402
import ask_cavnar_tools  # noqa: E402
import client_api  # noqa: E402
import competitor_intel_format as cif  # noqa: E402
import data_freshness  # noqa: E402
import emails  # noqa: E402
import labor  # noqa: E402
import rec_learning  # noqa: E402
import response_validation as rv  # noqa: E402
import review_intelligence as ri  # noqa: E402
import intelligence  # noqa: E402
from intelligence import (benchmarks, categories, confidence, engine, features, jobs, metrics_registry,  # noqa
                          patterns, privacy, scoring)

THIS_WEEK = features.iso_week(date.today())
LIVE = (date.today() - timedelta(days=120)).isoformat() + "T00:00:00"


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (features, patterns, benchmarks, confidence, intelligence, engine, scoring, jobs, ri):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    ask_cavnar._INTEL_FACTS.clear()
    yield
    ask_cavnar._INTEL_FACTS.clear()


def _rid(db_path, name, concept=None, service_model="full_service", created_at=LIVE, **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=cols.pop("owner_email", f"{abs(hash(name))}@x.test"),
                                       created_at=created_at, hourly_rate=cols.pop("hourly_rate", 18.0)),
                            db_path=db_path)
    sets = dict(cols)
    if concept:
        sets.update(concept=concept, category=concept, service_model=service_model, profile_source="set")
    if sets:
        conn = models.get_conn(db_path)
        conn.execute(f"UPDATE restaurants SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _feat(db_path, rid, vals, week=THIS_WEEK, completeness=1.0):
    conn = models.get_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, completeness) "
                 "VALUES (?,?,?,?)", (rid, week, json.dumps(vals), completeness))
    conn.commit()
    conn.close()


def _bench(key, value, unit="%", **src):
    """A peer-band fact in the contract shape (source.metric / better /
    standing / strength_pct — engine.facts once workstream A lands)."""
    base = {"source": "Cavnar anonymous cohort", "source_kind": "cohort", "engine_kind": "peers",
            "cohort_label": "full-service restaurants on Cavnar", "n": 12, "min_n": 8, "as_of": "9/20/26",
            "comparable": True, "strength_pct": 80}
    base.update(src)
    return {"key": key, "value": value, "unit": unit, "kind": "benchmark", "entity": "cohort", "as_of": "9/20/26",
            "source": base}


def _nra(**src):
    """The NRA labor median as the engine's industry kind serves it (the
    contract keys: comparable, definition_note, inferred, metric)."""
    base = {"source": "NRA 2025 Restaurant Operations Data Abstract", "year": 2025, "source_kind": "published",
            "cohort_label": "full-service restaurants", "n": None, "engine_kind": "industry", "inferred": False,
            "comparable": False, "metric": "labor_pct_28d",
            "definition_note": "The NRA median includes benefits; this restaurant's labor % is wages from shifts."}
    base.update(src)
    return {"key": "bench.labor_pct_28d.industry.labor_pct.full_service.median", "value": 34.2, "unit": "%",
            "kind": "benchmark", "entity": "industry", "as_of": "2024", "source": base}


def _v(text, facts, surface="ask", **kw):
    return rv.validate(text, rv.ValidationContext(surface=surface, facts=facts, confidence={"pct": 70}, **kw))


# ══ #5: one road for published figures to the AI ══════════════════════════

def test_a_guessed_type_gets_no_published_figure_on_any_model_surface(db_path):
    """R3-8 / R1-17: "Joe's Steakhouse", never confirmed, got the NRA
    full-service labor figure in Ask, the labor read, the food facts, the
    onboarding email and the admin hints."""
    rid = _rid(db_path, "Joe's Steakhouse")
    r = models.get_restaurant(rid, db_path=db_path)
    assert categories.category_for(r) == ("steakhouse", "inferred")
    text, facts = ask_cavnar._intelligence_bundle(rid)
    assert "34.2%" not in text and not [f for f in facts if (f.get("source") or {}).get("engine_kind") == "industry"]
    assert labor.industry_band_for(restaurant=r) is None
    assert "Industry benchmark: none" in labor.industry_prompt_line(labor.industry_band_for(restaurant=r))
    assert "%" not in emails.benchmark_sentence("labor_pct", rid, "labor as a share of sales")
    assert intelligence.industry_read(r, "labor_pct_28d") is None


def test_a_confirmed_types_published_figure_is_context_only_everywhere(db_path):
    """R1-04 / R3-5 / R4-4: the NRA labor median includes benefits — every
    model surface quotes it as context, and its facts say comparable False."""
    rid = _rid(db_path, "Nonna", "italian")
    r = models.get_restaurant(rid, db_path=db_path)
    text, facts = ask_cavnar._intelligence_bundle(rid)
    assert "34.2%" in text and intelligence.CONTEXT_ONLY_NOTE in text
    pub = [f for f in facts if (f.get("source") or {}).get("engine_kind") == "industry"]
    assert pub and all(f["source"]["comparable"] is False and f["source"]["definition_note"] for f in pub)
    line = labor.industry_prompt_line(labor.industry_band_for(restaurant=r))
    assert "34.2%" in line and intelligence.CONTEXT_ONLY_NOTE in line
    assert "context, not a comparison" in emails.benchmark_sentence("labor_pct", rid, "labor")
    # And the labor read's facts carry it, so a comparison is dropped.
    ctx = labor.labor_read_context({"overall_labor_pct": 31.0}, "Labor ran 31.0%.", industry=labor.industry_band_for(
        restaurant=r))
    assert rv.validate("Labor ran 31.0%, under the 34.2% industry median.", ctx).verdict == "refuse"


def test_the_admin_hints_read_the_engine(db_path, monkeypatch):
    import inspect
    src = inspect.getsource(admin_routes.client_settings_page)
    assert "industry_read(" in src and "for_restaurant(" not in src


# ══ #6: comparability and guessed types in the checker ═════════════════════

def test_a_comparison_with_a_figure_measured_differently_is_dropped():
    for s in ("Your 33.4% labor is below the industry median of 34.2%.",
              "Your labor runs below the industry median, so labor is in good shape.",
              "At 33.4%, your labor sits near the 34.2% industry median."):
        v = _v(s, [_nra(), {"key": "labor.pct", "value": 33.4, "unit": "%"}])
        assert v.verdict == "refuse" and "B1" in v.codes, s
    quote = _v("The industry median for full-service restaurants is 34.2% labor.", [_nra()])
    assert quote.verdict == "caveat" and any("includes benefits" in c for c in quote.actions["caveats"])
    said = "The NRA median of 34.2% includes benefits, so it is context rather than a like-for-like figure."
    assert _v(said, [_nra()]).verdict == "pass"
    # Comparable (same definition): the comparison stands, cited.
    ok = _v("Your labor is below the industry median of 34.2%.", [_nra(comparable=True)])
    assert ok.verdict == "pass" and "NRA 2025" in ok.text


def test_a_claim_bound_to_a_guessed_type_is_dropped():
    v = _v("The industry median for full-service restaurants is 34.2% labor.", [_nra(inferred=True)])
    assert v.verdict == "refuse" and "B1" in v.codes


# ══ #7: grammar and direction ══════════════════════════════════════════════

def test_direction_is_read_against_the_metric_and_the_standing():
    rating = [_bench("bench.avg_rating_30d.peers.p50", 4.5, standing="bottom quarter", metric="avg_rating_30d",
                     better="higher", unit="★")]
    assert _v("Your rating is higher than most similar restaurants.", rating).verdict == "refuse"
    assert _v("Your rating is lower than similar restaurants.", rating).verdict == "pass"
    labor_top = [_bench("bench.labor_pct_28d.peers.p50", 29.0, standing="top quarter", metric="labor_pct_28d",
                        better="lower")]
    # Lower labor is the better side: "lower than" agrees, "higher than" contradicts.
    assert _v("Your labor % is lower than similar restaurants.", labor_top).verdict == "pass"
    assert _v("Your labor % is higher than similar restaurants.", labor_top).verdict == "refuse"
    # The group as the subject turns it round.
    assert _v("Similar restaurants run a higher labor % than you.", labor_top).verdict == "pass"
    # About the middle: a difference the comparison does not show is not said.
    mid = [_bench("bench.labor_pct_28d.peers.p50", 29.0, standing="about the middle", metric="labor_pct_28d",
                  better="lower")]
    assert _v("Your labor % is lower than similar restaurants.", mid).verdict == "refuse"


def test_the_new_ranking_forms_are_gated_on_strength():
    weak = [_bench("bench.labor_pct_28d.peers.p50", 29.0, standing="top quarter", metric="labor_pct_28d",
                   better="lower", strength_pct=60)]
    strong = [_bench("bench.labor_pct_28d.peers.p50", 29.0, standing="top quarter", metric="labor_pct_28d",
                     better="lower", strength_pct=80)]
    for s in ("Your labor % is better than most similar restaurants.",
              "Your labor % is one of the lowest among full-service restaurants on Cavnar.",
              "Your labor % beats the group."):
        assert _v(s, weak).verdict == "caveat", s
        assert _v(s, strong).verdict == "pass", s


def test_the_fallback_better_direction_agrees_with_the_registry():
    """The validator is layer 0 and cannot import metrics_registry; its
    fallback for a fact with no source.better must say what the registry
    says, for every metric."""
    for metric, meta in metrics_registry.METRICS.items():
        want = meta["better"]
        got = "lower" if rv._LOWER_BETTER_RE.search(metric) else "higher"
        assert got == want, metric


# ══ #8: the same metric, not the same topic ════════════════════════════════

def test_a_claim_binds_to_the_metric_it_names():
    facts = [_bench("bench.labor_hours_per_1k_28d.peers.p50", 4.1, standing="top quarter", unit="",
                    metric="labor_hours_per_1k_28d", better="lower"),
             _bench("bench.labor_pct_28d.peers.p50", 29.0, standing="bottom quarter", metric="labor_pct_28d",
                    better="lower")]
    assert _v("Your labor % is in the top quarter of similar restaurants.", facts).verdict == "refuse"
    assert _v("Your labor hours per thousand dollars of sales are lower than similar restaurants.",
              facts).verdict == "pass"
    amb = _v("Your labor is below similar restaurants.", facts)
    assert amb.verdict == "refuse" and "disagree" in amb.findings[0]["detail"]


# ══ #9: dropped on every surface ═══════════════════════════════════════════

def test_an_unbound_or_contradicted_peer_claim_is_dropped_on_an_interactive_surface():
    for surface in ("ask", "labor_insight", "review_insight", "intel"):
        v = _v("You're doing better than most restaurants like yours.", [], surface=surface)
        assert v.verdict == "refuse" and v.text == "", surface
    contra = [_bench("bench.labor_pct_28d.peers.p50", 29.0, standing="bottom quarter", metric="labor_pct_28d",
                     better="lower")]
    assert _v("Your labor % is in the top quartile of similar restaurants.", contra).verdict == "refuse"
    # Kept with a caveat only for a missing citation or staleness.
    stale = [_bench("bench.labor_pct_28d.peers.p50", 29.0, standing="top quarter", metric="labor_pct_28d",
                    better="lower", stale=True)]
    assert _v("Your labor % is lower than similar restaurants.", stale).verdict == "caveat"


def test_saying_there_is_no_comparison_is_not_a_peer_claim():
    s = "Cavnar can't compare your labor with other restaurants fairly yet — confirm your restaurant profile."
    v = _v(s, [])
    assert v.verdict == "pass" and v.text == s


# ══ #14: recommendation priors by organisation ══════════════════════════════

def _events(db_path, rid, kind, outcomes, cohort="pizza"):
    conn = models.get_conn(db_path)
    for i, o in enumerate(outcomes):
        at = (datetime.utcnow() - timedelta(days=10 + i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, "
                     "event_at) VALUES (?,?,?,?,?,?,?)", (rid, kind, f"{kind}:{rid}:{i}", cohort, "measured", o, at))
        conn.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, "
                     "event_at) VALUES (?,?,?,?,?,?,?)", (rid, kind, f"{kind}:{rid}:{i}", cohort, "accepted", None, at))
    conn.commit()
    conn.close()


def test_priors_floor_on_organisations_and_leave_the_viewers_organisation_out(db_path):
    """R1-02 / R4-23: a group owner with four locations and one outsider read
    "7 of 12 improved" — and, subtracting its own, the outsider's results."""
    group = [_rid(db_path, f"Slice Group {i}", "pizza", "counter", organization_id=77) for i in range(4)]
    outsider = [_rid(db_path, f"Outside Pie {i}", "pizza", "counter") for i in range(3)]
    for r in group + outsider:
        _events(db_path, r, "trim_day", ["improved", "worsened", "improved"])
    viewer = group[0]
    s = scoring.kind_stats("trim_day", cohort="pizza", db_path=db_path, exclude_restaurant_id=viewer)
    # The viewer's whole organisation is out: three outside restaurants, three organisations.
    assert s["measured_restaurants"] == 3 and s["measured_orgs"] == 3 and s["success_available"] is False
    # Without the group, seven restaurants but only four organisations: still below the floor.
    s2 = scoring.kind_stats("trim_day", cohort="pizza", db_path=db_path)
    assert s2["measured_restaurants"] == 7 and s2["measured_orgs"] == 4 and s2["success_available"] is False
    # Five organisations clear it, and the share cap is per organisation:
    more = [_rid(db_path, f"More Pie {i}", "pizza", "counter") for i in range(2)]
    for r in more:
        _events(db_path, r, "trim_day", ["improved", "worsened", "improved"])
    s3 = scoring.kind_stats("trim_day", cohort="pizza", db_path=db_path)
    assert s3["measured_orgs"] == 6 and s3["success_available"] is True
    # the four-location group counts as one organisation's share (≤ 1/3 of the capped total)
    assert s3["measured_capped"] < s3["measured"]


def test_the_owner_facing_prior_carries_no_raw_counts(db_path):
    rid = _rid(db_path, "Prior Pie", "pizza", "counter")
    rec = rec_learning.kind_record(rid, "trim_day", db_path=db_path, episodes=[])
    assert "prior_measured_raw" not in rec and "prior_improved_raw" not in rec


# ══ #18: no general-industry licence; why there is no comparison ═════════

def test_ask_says_why_there_is_no_fair_comparison(db_path):
    assert "general industry knowledge, not measured here" not in ask_cavnar._SYSTEM_STATIC
    rid = _rid(db_path, "Unconfirmed Cafe")
    lines = intelligence.context_lines(rid)
    labor_line = next(ln for ln in lines if "for labor" in ln)
    assert labor_line.startswith("No fair comparison") and "confirm" in labor_line.lower()
    assert "do not substitute an industry average" in labor_line
    text, _facts = ask_cavnar._intelligence_bundle(rid)
    assert "No fair comparison with other restaurants for labor" in text


# ══ #19: numberless predictions, and P2 binds metric and kind ═══════════════

def _prediction(metric="labor_pct_28d", kind="trim_day", n=7):
    return {"key": f"predict.{kind}.{metric}.effect_pct", "value": 6.0, "unit": "%", "kind": "prediction",
            "as_of": "9/20/26", "source": {"source_kind": "prediction", "metric": metric, "rec_kind": kind,
                                           "n_restaurants": n, "n_orgs": n, "n_results": 14, "min_n": 5,
                                           "interval": [2.0, 9.0], "similarity_pct": 80}}


def test_a_numberless_what_worked_for_peers_claim_needs_a_prediction_fact():
    s = "Restaurants like yours typically cut labor after tightening weekday prep."
    peers = [_bench("bench.labor_pct_28d.peers.p50", 29.0, standing="top quarter", metric="labor_pct_28d",
                    better="lower")]
    v = _v(s, peers)
    assert v.verdict == "refuse" and v.codes == ["P2"]          # a peer band is no prediction
    assert _v(s, [_prediction()]).verdict == "pass"
    assert _v(s, [_prediction(metric="waste_sales_pct_28d")]).verdict == "refuse"      # another measure
    assert _v(s, [_prediction()], policy={"rec_kind": "cut_waste"}).verdict == "refuse"   # another kind
    assert _v("Restaurants like yours reduced labor 6% after trimming Tuesdays.", [_prediction()],
              policy={"rec_kind": "trim_day"}).verdict == "pass"


# ══ #20: one accessor for the confirmed type ═══════════════════════════════

def test_a_guessed_type_reads_no_group(db_path, monkeypatch):
    guessed = _rid(db_path, "Tony's Pizzeria")
    r = models.get_restaurant(guessed, db_path=db_path)
    assert categories.category_for(r) == ("pizza", "inferred") and categories.confirmed_type(r) is None
    confirmed = models.get_restaurant(_rid(db_path, "Set Pie", "pizza", "counter"), db_path=db_path)
    assert categories.confirmed_type(confirmed) == "pizza"
    calls = []
    monkeypatch.setattr(intelligence, "recommendation_success", lambda *a, **k: calls.append(k) or {})
    rec_learning.kind_record(guessed, "trim_day", db_path=db_path, restaurant=r, episodes=[])
    assert calls == []                                  # no cohort record read for a guess
    m = rec_learning.effectiveness(guessed, db_path=db_path, restaurant=r)
    assert m.cohort is None
    assert confidence.score(guessed, "trim_day", restaurant=r, db_path=db_path) is not None
    # No confirmed group: only the all-types patterns are served.
    conn = models.get_conn(db_path)
    for key, cohort in (("pizza:reply_rate_rating", "pizza"), ("platform:reply_rate_rating", "platform")):
        conn.execute("INSERT INTO intel_patterns (key, cohort, hypothesis, n_with, n_without, effect, effect_unit, "
                     "cohen_d, p_value, q_value, confidence, sentence, evidence_json, status, last_confirmed) "
                     "VALUES (?,?,'reply_rate_rating',9,9,0.2,'★',0.5,0.01,0.05,0.6,'Across 18 x.',"
                     "'{\"orgs_with\": 9, \"orgs_without\": 9}','active',datetime('now'))", (key, cohort))
    conn.commit()
    conn.close()
    assert [p["cohort"] for p in patterns.active(None, db_path=db_path)] == ["platform"]
    assert [p["cohort"] for p in patterns.active(patterns.viewer_cohorts(r), db_path=db_path)] == ["platform"]


# ══ #21: patterns use band membership and the confirmed partition ══════════

def test_patterns_use_the_bands_membership_and_the_confirmed_partition(db_path):
    """R1-09: discovery ran over every latest row by TYPE — trial accounts,
    half-connected ones and the $26/hr default wage included."""
    ids = []
    for i in range(12):
        default_wage = i >= 9                        # three on the assumed wage
        rid = _rid(db_path, f"Part {i}", "pizza", "counter", hourly_rate=26.0 if default_wage else 18.0)
        _feat(db_path, rid, {"schedule_adjust_rate": 0.7 if i % 2 else 0.2, "labor_pct_28d": 28.0 + i,
                             "response_24h_rate_30d": 0.8 if i % 2 else 0.1, "avg_rating_delta": 0.1})
        ids.append(rid)
    new = _rid(db_path, "Brand New Pie", "pizza", "counter", created_at=date.today().isoformat() + "T00:00:00")
    _feat(db_path, new, {"schedule_adjust_rate": 0.7, "labor_pct_28d": 40.0})
    members = jobs.member_info(db_path=db_path)
    assert members[ids[-1]]["cost_basis"] == "default"
    elig, skipped = jobs.eligible_members(features.latest_by_restaurant(db_path=db_path), members)
    assert new not in elig and ids[0] in elig and any("weeks live" in k for k in skipped)
    seen = []
    real = patterns.test_hypothesis

    def spy(rows, h, shuffles=patterns.SHUFFLES):
        seen.append((h["key"], sorted(r["_rid"] for r in rows)))
        return real(rows, h, shuffles=shuffles)
    import unittest.mock as _mock
    with _mock.patch.object(patterns, "test_hypothesis", spy):
        out = patterns.discover(db_path=db_path, shuffles=50)
    assert "sm:counter" in out["cohorts_tested"] and "pizza" not in out["cohorts_tested"]
    labor_runs = [rids for key, rids in seen if key == "adjust_schedule_labor"]
    assert labor_runs and all(new not in rids and not set(ids[9:]) & set(rids) for rids in labor_runs)
    reply_runs = [rids for key, rids in seen if key == "reply_fast_rating"]
    assert reply_runs and any(set(ids[9:]) <= set(rids) for rids in reply_runs)     # wage is no bar there


# ══ #24: peer freshness reads the bands actually used, one age limit ═══════

def test_peer_freshness_dates_the_confirmed_partition_band(db_path):
    assert data_freshness.SOURCES["cohort"]["horizon"] == benchmarks.MAX_BAND_AGE_WEEKS * 7
    rid = _rid(db_path, "Fresh Pie", "pizza", "counter")
    r = models.get_restaurant(rid, db_path=db_path)
    old = (date.today() - timedelta(days=40)).isoformat() + " 03:00:00"
    new = (date.today() - timedelta(days=1)).isoformat() + " 03:00:00"
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean, computed_at) "
                 "VALUES ('platform','reply_rate_30d','2026-W38',9,0.5,0.6,0.7,0.6,?)", (new,))
    conn.execute("INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean, computed_at) "
                 "VALUES ('sm:counter','labor_pct_28d','2026-W33',9,27,29,31,29,?)", (old,))
    conn.commit()
    conn.close()
    st = data_freshness.source_state(r, "cohort", db_path=db_path)
    assert st["state"] != "current"                  # dated by its 40-day-old partition band, not the platform one
    unconfirmed = models.get_restaurant(_rid(db_path, "Loose Diner"), db_path=db_path)
    assert data_freshness.source_state(unconfirmed, "cohort", db_path=db_path)["state"] == "current"


# ══ #30: one market comparison ═════════════════════════════════════════════

_M = "same cuisine type and similar price level"


def test_the_engines_market_kind_is_intels_standing(db_path):
    blob = {"competitors": [{"name": n, "rating": r, "review_count": 200, "match_basis": _M}
                            for n, r in (("A", 4.5), ("B", 4.6), ("C", 4.5), ("D", 4.4))]
                           + [{"name": "Far", "rating": 2.0, "review_count": 900, "match_basis": "widened search"}]}
    rid = _rid(db_path, "Market Bistro", competitor_intel=json.dumps(blob), gbp_rating=4.4, gbp_review_count=300,
               competitor_updated_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    cm = engine.compare(rid, "avg_rating_30d", kinds=("market",), db_path=db_path)
    c = cm["comparisons"][0]
    st = cif.market_standing(cif.own_rating(4.4, 300), cif.market_rating(blob["competitors"]))
    assert c["available"] and c["standing"] == st["standing"] == "level" and c["n"] == 4
    assert engine.compare(rid, "labor_pct_28d", kinds=("market",), db_path=db_path)["comparisons"][0][
        "available"] is False
    # Intel calls it level, so "you trail nearby competitors" is not said.
    facts = cif.market_facts(c)
    assert _v("You trail nearby competitors on rating.", facts, surface="review_insight").verdict == "refuse"
    ok = _v("Your Google rating is about level with nearby competitors.", facts, surface="review_insight")
    assert ok.verdict == "pass"


def test_the_reviews_read_quotes_the_market_kind_not_its_own_median():
    import inspect
    src = inspect.getsource(client_api._do_review_insight)
    assert "kinds=(\"market\",)" in src and "competitor_median" not in src and "gap_vs_median" not in src
    ctx_src = inspect.getsource(client_api._review_insight_rv_context)
    assert "market_facts(" in ctx_src and "competitors.median_rating" not in ctx_src


# ══ #42: the Reviews location comparison ═══════════════════════════════════

def test_the_reviews_location_comparison_is_by_organisation_and_gated(db_path):
    a = _rid(db_path, "Org Loc A", organization_id=9, location_name="North")
    _rid(db_path, "Org Loc B", organization_id=9, location_name="South")
    _rid(db_path, "Stranger", location_group="", owner_email="z@x.test")
    assert [s["label"] for s in ri.location_siblings(a, db_path=db_path)] == ["North", "South"]
    # Closed with no viewer, and for a login that may not switch locations.
    assert ri.location_comparison(a, db_path=db_path)["available"] is False
    manager = {"restaurant_id": a, "role": "manager", "permissions": []}
    assert ri.location_comparison(a, db_path=db_path, viewer=manager)["available"] is False
    assert ri.location_comparison(a, db_path=db_path, viewer={"is_admin": True})["available"] is True
    import inspect
    src = inspect.getsource(client_api._do_review_insight)
    assert "of this location's reviews" in src and "of this location's complaints" not in src
    assert "viewer=viewer" in src
