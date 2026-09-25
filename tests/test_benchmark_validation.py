"""Workstream V of the Benchmarking audit (9/24/26): every benchmark an AI
surface is handed becomes a fact the Response Validation Layer binds to,
every peer claim binds to one or is not said, and the context each model
reads carries who the peers are, how they were chosen, how many measured
the figure and how strong the comparison is — projected by what the login
may see. (The B1 / P2 golden cases themselves live in the corpus,
tests/fixtures/validation_corpus, built from production facts by
tests/benchmark_corpus_facts.py.)"""
import json
import os
import types
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant

# Imported before any fixture patches get_conn (CLAUDE.md's bound-import hazard).
import ask_cavnar  # noqa: E402
import ask_cavnar_tools  # noqa: E402
import benchmark_registry as br  # noqa: E402
import inventory  # noqa: E402
import labor  # noqa: E402
import response_validation as rv  # noqa: E402
import schedule_engine  # noqa: E402
import intelligence  # noqa: E402
from intelligence import (benchmarks, categories, confidence, engine, features, feedback, memory, patterns,  # noqa
                          scoring)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THIS_WEEK = features.iso_week(date.today())


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (features, patterns, benchmarks, confidence, intelligence, engine, memory, feedback, scoring):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    ask_cavnar._INTEL_FACTS.clear()
    yield
    ask_cavnar._INTEL_FACTS.clear()


def _rid(db_path, name, category=None, service_model="counter", **kw):
    # Live 17 weeks on a real labor cost basis, with an owner-CONFIRMED
    # profile when a type is given: the peer group is that confirmed
    # partition (Benchmarking audit #7, #14, #20, #39 — workstream P).
    kw.setdefault("created_at", (date.today() - timedelta(days=120)).isoformat() + "T00:00:00")
    kw.setdefault("hourly_rate", 18.0)
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.replace(' ', '').lower()}@x.test", **kw),
                            db_path=db_path)
    if category:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET category=?, concept=?, service_model=?, profile_source='set' "
                     "WHERE id=?", (category, category, service_model, rid))
        conn.commit()
        conn.close()
    return rid


def _feat(db_path, rid, vals, week=THIS_WEEK):
    conn = models.get_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, completeness) "
                 "VALUES (?,?,?,1.0)", (rid, week, json.dumps(vals)))
    conn.commit()
    conn.close()


def _pizza(db_path, n=12, **extra):
    """n pizza places whose type the owner set, plus a viewer among them."""
    ids = []
    for i in range(n):
        rid = _rid(db_path, f"Slice {i}", "pizza")
        _feat(db_path, rid, {"labor_pct_28d": 28.0 + i * 0.2, "reply_rate_30d": 0.5 + i * 0.02,
                             "food_cost_pct_28d": 30.0 + i * 0.2, **{k: v + i * 0.1 for k, v in extra.items()}})
        ids.append(rid)
    viewer = _rid(db_path, "Viewer Pizza", "pizza")
    _feat(db_path, viewer, {"labor_pct_28d": 27.0, "reply_rate_30d": 0.95, "food_cost_pct_28d": 29.0,
                            **{k: v - 0.5 for k, v in extra.items()}})
    benchmarks.compute(db_path=db_path)       # each one's confirmed partition
    return viewer, ids


# ── #1 / #2 / #4 / #5: the B1 contract ────────────────────────────────────

def test_the_cohort_floor_is_the_engines_and_a_ranking_needs_the_high_threshold():
    assert rv.COHORT_MIN_N == benchmarks.MIN_QUARTILE_N == 8
    assert rv.STRONG_CLAIM_PCT == 75
    assert "P2" in rv.RULES and "prediction" in rv.FACT_KINDS


def test_the_labor_reads_sourced_nra_sentence_survives_its_own_production_context():
    """BM3-2: the labor read's one sourced industry sentence was dropped as
    'a cohort benchmark under five restaurants' on every read."""
    entry = br.for_category("labor_pct", "italian", "set")
    ctx = labor.labor_read_context({"overall_labor_pct": 36.1}, "Labor ran 36.1% this period.",
                                   restaurant_id=None, industry=entry)
    s = "Labor ran 36.1%, above the industry median of 34.2% (NRA 2025 Restaurant Operations Data Abstract)."
    v = rv.validate(s, ctx)
    assert v.text == s and "B1" not in v.codes


def test_an_all_types_group_is_known_by_its_source_whatever_its_label():
    """BM3-4: 'All restaurants on Cavnar' was not in a label list, so 'like
    yours' survived on an all-types band."""
    f = {"key": "bench.reply_rate_30d.platform.p50", "value": 0.6, "unit": "", "kind": "benchmark",
         "as_of": "9/20/26", "source": {"source": "Cavnar anonymous cohort", "source_kind": "platform",
                                        "engine_kind": "platform", "cohort_label": "All restaurants on Cavnar",
                                        "n": 12, "min_n": 8, "as_of": "9/20/26", "restaurant_category": "platform",
                                        "strength_pct": 60, "standing": "top quarter", "comparable": True}}
    v = rv.validate("Your reply rate is higher than restaurants like yours.",
                    rv.ValidationContext(surface="ask", facts=[f]))
    assert "like yours" not in v.text and "12 other restaurants on Cavnar, all types" in v.text


# ── #3: every benchmark is a bindable fact ─────────────────────────────────

def test_facts_from_dict_walks_lists():
    fs = {f.key: f for f in rv.facts_from_dict({"bands": [{"p50": 31.0}, {"p50": 0.6}], "n": [1, 2]})}
    assert fs["bands.0.p50"].value == 31.0 and fs["bands.0.p50"].kind == "benchmark"
    assert fs["bands.1.p50"].value == 0.6 and "n.1" in fs


def test_asks_snapshot_registers_the_engines_bands_and_a_peer_claim_binds(db_path):
    viewer, _ids = _pizza(db_path)
    text, facts = ask_cavnar._intelligence_bundle(viewer)
    assert "12 other counter-service restaurants on Cavnar" in text and "comparison strength" in text
    peers = [f for f in facts if (f.get("source") or {}).get("engine_kind") == "peers"]
    assert peers and all(f["kind"] == "benchmark" and f["source"]["n"] == 12 for f in peers)
    assert ask_cavnar.snapshot_benchmark_facts(viewer) == facts
    ctx = ask_cavnar._validation_context([text], viewer, bench_facts=facts)
    v = rv.validate("Your labor is above restaurants similar to yours.", ctx)
    assert v.verdict == "pass" and "12 other counter-service restaurants on Cavnar" in v.text, v.findings
    # And without them the same claim has nothing to bind to.
    v0 = rv.validate("Your labor is above restaurants similar to yours.",
                     ask_cavnar._validation_context([text], viewer))
    assert v0.verdict == "caveat" and "B1" in v0.codes


def test_the_published_figure_in_asks_snapshot_is_a_fact(db_path):
    rid = _rid(db_path, "Nonna", "italian", service_model="full_service")  # re-audit #4: the NRA figure is full-service only
    text, facts = ask_cavnar._intelligence_bundle(rid)
    assert "PUBLISHED INDUSTRY BENCHMARKS" in text and "34.2%" in text
    pub = [f for f in facts if (f.get("source") or {}).get("source_kind") == "published"]
    assert any(f["value"] == 34.2 for f in pub)
    v = rv.validate("The industry median for full-service restaurants is 34.2% labor.",
                    ask_cavnar._validation_context([text], rid, bench_facts=facts))
    assert v.text and "NRA 2025" in v.text and v.verdict == "pass"


def test_the_platform_intelligence_tool_is_typed_as_engine_facts(db_path):
    viewer, _ids = _pizza(db_path)
    payload = ask_cavnar_tools.run_read_tool("read_platform_intelligence", viewer, {})
    body = json.loads(payload)
    assert body["comparisons"] and any("12 other counter-service restaurants on Cavnar" in ln for ln in body["lines"])
    facts = ask_cavnar._typed_facts(["snapshot", payload])
    bench = [f for f in facts if (f.get("kind") if isinstance(f, dict) else f.kind) == "benchmark"]
    srcs = [(f["source"] if isinstance(f, dict) else f.source) for f in bench]
    assert srcs and all(isinstance(s, dict) and s.get("engine_kind") for s in srcs)
    # An all-types band is never among them for labor or food cost.
    labor_rows = [c for c in body["comparisons"] if c["metric"] == "labor_pct_28d"]
    plat = next(c for c in labor_rows[0]["comparisons"] if c["kind"] == "platform")
    assert plat["available"] is False


def test_the_schedule_prompts_cohort_block_is_the_engines_and_registers_its_facts(db_path):
    viewer, _ids = _pizza(db_path, labor_hours_per_1k_28d=20.0)
    r = models.get_restaurant(viewer, db_path=db_path)
    block = schedule_engine._cohort_block(viewer, r)
    assert schedule_engine.COHORT_BLOCK_HEADER in block and "12 other counter-service restaurants on Cavnar" in block
    assert "peer group: counter-service restaurants — split by how they serve" in block
    assert "measured at 13 of 13" in block and "% comparison strength" in block
    ctx = labor.schedule_note_context("Build next week." + block, restaurant_id=viewer)
    assert ctx.facts and all(f.kind == "benchmark" for f in ctx.facts)
    assert not labor.schedule_note_context("Build next week.", restaurant_id=viewer).facts


def test_the_schedule_prompt_never_states_an_all_types_band_for_hours(db_path):
    """BM3-7: a lone sushi bar got the all-types hours band ("hold the line")."""
    _viewer, ids = _pizza(db_path, labor_hours_per_1k_28d=20.0)
    sushi = _rid(db_path, "Omakase Room", "sushi", service_model="full_service")
    _feat(db_path, sushi, {"labor_hours_per_1k_28d": 14.0})
    conn = models.get_conn(db_path)
    conn.execute("DELETE FROM intel_benchmarks")       # this week's bands again, with the sushi bar in them
    conn.commit()
    conn.close()
    benchmarks.compute(db_path=db_path)
    # The legacy read no longer falls back to the all-types band either
    # (Benchmarking audit #6, workstream P).
    b = benchmarks.benchmark(sushi, "labor_hours_per_1k_28d", cohort="sushi", db_path=db_path)
    assert b["available"] is False and "no like-for-like peers" in b["reason"]
    assert schedule_engine._cohort_block(sushi, models.get_restaurant(sushi, db_path=db_path)) == ""


def test_the_food_read_registers_the_published_food_cost_figure(db_path):
    rid = _rid(db_path, "Trattoria Uno", "italian", service_model="full_service")  # re-audit #4
    ctx = inventory.food_read_context(rid, "prompt", {}, [])
    pub = [f for f in ctx.facts if f.kind == "benchmark" and (f.source or {}).get("source_kind")]
    assert pub and all(f.key.startswith("benchmark.food_cost_pct") for f in pub)


# ── #12: projected by the viewer's module permissions ──────────────────────

def test_ask_projects_benchmarks_memory_and_tools_by_module_permission(db_path):
    viewer, _ids = _pizza(db_path)
    r = models.get_restaurant(viewer, db_path=db_path)
    denied = types.SimpleNamespace(id=viewer, _ask_denied=frozenset({"labor", "inventory"}))
    text, facts = ask_cavnar._intelligence_bundle(viewer, viewer=denied)
    assert "Labor %" not in text and "Food cost %" not in text and "Reviews answered" in text
    assert not any("labor" in f["key"] or "food" in f["key"] for f in facts)
    view = ask_cavnar_tools.viewer_restaurant(r, None)
    view._ask_denied = frozenset({"labor", "inventory"})
    mem = json.loads(ask_cavnar_tools.run_read_tool("read_restaurant_memory", viewer, {}, restaurant=view))
    assert "labor_pct_28d" not in (mem["features"] or {}) and "food_cost_pct_28d" not in (mem["features"] or {})
    assert "reply_rate_30d" in (mem["features"] or {})
    plat = json.loads(ask_cavnar_tools.run_read_tool("read_platform_intelligence", viewer, {}, restaurant=view))
    metrics = {c["metric"] for c in plat["comparisons"]}
    assert metrics and not any(m.startswith(("labor", "food", "waste")) for m in metrics)
    # The owner's own view still carries them.
    full = json.loads(ask_cavnar_tools.run_read_tool("read_platform_intelligence", viewer, {}))
    assert "labor_pct_28d" in {c["metric"] for c in full["comparisons"]}


# ── #16: patterns say "at the same time as"; pooled types never support economics ─

def _pattern(db_path, key, cohort, outcome, behaviour, rec_kinds):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO intel_patterns (key, cohort, hypothesis, n_with, n_without, effect, effect_unit, "
                 "cohen_d, p_value, q_value, confidence, sentence, evidence_json, status, last_confirmed) "
                 "VALUES (?,?,?,6,6,0.5,'%',0.6,0.01,0.05,0.7,'Across 12 restaurants, x.',?,'active',"
                 "datetime('now'))",
                 (key, cohort, key.split(":")[1], json.dumps({"n": 12, "outcome": outcome, "behaviour": behaviour,
                                                               "rec_kinds": rec_kinds,
                                                               # an owner is served a pattern only
                                                               # over 8 organisations a side (#11)
                                                               "orgs_with": 8, "orgs_without": 8})))
    conn.commit()
    conn.close()


def test_pattern_sentences_are_concurrent_not_following():
    for h in patterns.HYPOTHESES:
        assert "following" not in h["sentence"], h["key"]
    fast = next(h for h in patterns.HYPOTHESES if h["key"] == "reply_fast_rating")
    assert "at the same time as" in fast["sentence"]


def test_a_pooled_pattern_never_supports_an_economics_recommendation(db_path):
    _pattern(db_path, "platform:weekend_share_labor", "platform", "labor_pct_28d",
             ["weekend_sales_share_28d", ">=", 0.55], ["trim_day"])
    _pattern(db_path, "platform:reply_fast_rating", "platform", "avg_rating_delta",
             ["response_24h_rate_30d", ">=", 0.5], ["reply"])
    got = {p["key"]: p for p in patterns.active("pizza", db_path=db_path)}
    assert got["platform:weekend_share_labor"]["pooled_types"] is True
    assert patterns.pooled_on_economics(got["platform:weekend_share_labor"])
    assert not patterns.pooled_on_economics(got["platform:reply_fast_rating"])
    assert patterns.support_for("trim_day", cohort="pizza", db_path=db_path) is None
    assert patterns.support_for("reply", cohort="pizza", db_path=db_path)["key"] == "platform:reply_fast_rating"
    lines = intelligence.context_lines(_rid(db_path, "Lone Pie", "pizza"))
    assert not any("weekend" in ln.lower() or "x." in ln and "labor" in ln for ln in lines)
    assert sum(1 for ln in lines if ln.startswith("Pattern")) == 1


# ── #28: the prediction fact shape is documented ───────────────────────────

def test_the_prediction_fact_shape_is_documented_for_the_dna_layer():
    doc = rv.__doc__
    for word in ("P2", 'kind="prediction"', "n_restaurants", "interval", "PREDICTION_MIN_RESTAURANTS"):
        assert word in doc, word
    assert rv.PREDICTION_MIN_RESTAURANTS == 5
    assert rv.kind_of_key("predict.cut_waste.waste_sales_pct_28d.effect_pct") == "prediction"


# ── #40 / #41: one label end to end; the prompt line says how ──────────────

def test_asks_words_never_invite_like_yours_or_unsourced_benchmarks():
    spec = next(t for t in ask_cavnar_tools.TOOLS if t["spec"]["name"] == "read_platform_intelligence")["spec"]
    d = spec["description"]
    assert "LIKE IT" not in d and "five" not in d and "all types" in d and "8 other" in d
    doc = " ".join(ask_cavnar.__doc__.split())
    assert "industry benchmarks, or just conversation" not in doc and "NOT general expertise" in doc
    md = open(os.path.join(ROOT, "INTELLIGENCE_ENGINE.md"), encoding="utf-8").read()
    assert "restaurants like yours: X of Y" not in md


def test_prompt_lines_say_how_the_group_was_chosen_how_many_measured_and_how_strong(db_path):
    viewer, _ids = _pizza(db_path)
    cm = engine.compare(viewer, "labor_pct_28d", kinds=("peers",), db_path=db_path)
    line = engine.prompt_lines([cm])[0]
    assert "peer group: counter-service restaurants — split by how they serve" in line
    assert "from the profile the owner confirmed" in line
    assert "measured at 13 of 13 in the group" in line and "% comparison strength" in line
    assert "as of " in line and "12 other counter-service restaurants on Cavnar" in line
