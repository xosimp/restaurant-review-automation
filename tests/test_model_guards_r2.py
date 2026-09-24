"""Group R (round 2) — model output guards, replaying the blind re-audit's
adversarial probes (B5 p01–p20) with FAKE model outputs.

Each test names the item it holds (R1–R14) and the probe it replays. No
model or network is reached: every model reply below is a literal.
"""
import json
import sqlite3
import types

import pytest

import ai_guard
import client_api
import models
from models import Restaurant, create_restaurant

# Imported at collection, never first inside a test (bound get_conn hazard).
import analyser, ask_cavnar, competitor, food_cost_intelligence, inventory  # noqa: E401,F401
import labor, notify, recipes, reporter, review_intelligence, strategy_jobs  # noqa: E401,F401
import rec_trust, confidence_engine  # noqa: E401,F401
from dsr import narrative as _narrative  # noqa: F401


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import review_intelligence as ri
    import food_cost_intelligence as fci
    monkeypatch.setattr(ri, "DB_PATH", db_path)
    monkeypatch.setattr(fci, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(fci, "DB_PATH", db_path, raising=False)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    yield


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Probe Bistro"), owner_email="o@x.test", **kw),
                             db_path=db_path)


# ── R1: padding, duplicates and template words never raise evidence ─────────

_OPCTX = {"labor": {"labor_pct": 31.4, "target_pct": 28, "understaffed_days": 0, "overstaffed_days": 3,
                    "period_days": 28, "covers_to": "9/20/26"},
          "reviews": {"mentions": 5, "category": "food_quality", "window_days": 90}}
_FOOD_BASE = {"headline": "Salmon waste leads food cost", "cause": "Salmon is over-ordered for weekday demand.",
              "recommended_action": "Cut the Tuesday salmon order.", "confidence": "high"}


def _food_evidence(opev):
    import food_cost_intelligence as fci
    op_lines = fci._operational_lines(_OPCTX)
    prompt = "Drivers: Salmon over par $640/month. " + "\n".join(op_lines.values())
    out = fci._validate_diagnosis(dict(_FOOD_BASE, operational_evidence=opev), ["Salmon"], prompt, 1,
                                  op_lines=op_lines)
    n_ev = rec_trust.verified_evidence_count(out)
    ev = confidence_engine.evidence(**rec_trust.diagnosis_evidence(out, n_ev, "evidence_items", "x"))
    return out, n_ev, ev["pct"]


REAL = {"module": "labor", "metric": "labor %", "value": "31.4%"}


def test_r1_p04_padding_never_raises_food_evidence():
    _, n_real, pct_real = _food_evidence([REAL])
    assert n_real == 1
    # D: a template word with no figure ("the data says 0 understaffed days")
    out, n, pct = _food_evidence([{"module": "labor", "metric": "staffing", "value": "understaffed"}])
    assert out["operational_evidence"] == [] and n == 0 and pct == 0
    # E: the same real entry four times is still one module's cross-check
    out, n, pct = _food_evidence([REAL] * 4)
    assert n == 1 and pct == pct_real
    # G: three template words
    out, n, pct = _food_evidence([{"module": "labor", "metric": "m", "value": "target"},
                                  {"module": "labor", "metric": "m", "value": "sales"},
                                  {"module": "reviews", "metric": "m", "value": "negative reviews"}])
    assert n == 0 and pct == 0
    # the same module under two metric names is one module
    out, n, _ = _food_evidence([REAL, {"module": "labor", "metric": "overstaffing", "value": "3 overstaffed days"}])
    assert n == 1 and len(out["operational_evidence"]) == 1
    assert out["operational_evidence"][0]["fields"] == ["labor_pct", "overstaffed_days"]
    # two distinct modules are two
    _, n, _ = _food_evidence([REAL, {"module": "reviews", "metric": "mentions", "value": "5 reviews"}])
    assert n == 2


def test_r1_p04_context_zero_counts_and_mislabels_are_not_evidence():
    # F: "overtime cost rose: 28%" — 28 is the owner's TARGET, not a measurement
    out, n, _ = _food_evidence([{"module": "labor", "metric": "overtime cost rose", "value": "28%"}])
    assert n == 0
    # a zero count ("0 understaffed days") corroborates nothing
    out, n, _ = _food_evidence([{"module": "labor", "metric": "understaffing", "value": "0 understaffed days"}])
    assert n == 0
    # C: an invented figure is dropped (held from round 1)
    out, n, _ = _food_evidence([{"module": "labor", "metric": "labor %", "value": "36%"}])
    assert n == 0
    # the metric and value the screen shows are code's, not the model's words
    out, _, _ = _food_evidence([{"module": "labor", "metric": "overtime exploded", "value": "31%"}])
    assert out["operational_evidence"][0]["metric"] == "labor % of sales"
    assert out["operational_evidence"][0]["value"] == "31.4%"


def test_r1_p04_review_diagnosis_band_not_lifted_by_a_template_word():
    import review_intelligence as ri
    rlines = ri._operational_lines({"labor": dict(_OPCTX["labor"], age_days=None)})
    raw = {"cause": "Friday dinner is short a cook.", "evidence_review_ids": [11, 12], "confidence": "high",
           "operational_evidence": [{"module": "labor", "metric": "x", "value": "overstaffed"}]}
    out = ri._validate_diagnosis(raw, {11, 12}, "11 12 " + "\n".join(rlines.values()), 1, op_lines=rlines)
    assert out["operational_evidence"] == [] and out["confidence"] == "medium"


def test_r1_a_stored_row_with_padding_serves_one_entry_per_module(db_path):
    import review_intelligence as ri
    rid = _rid(db_path)
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO review_diagnoses (restaurant_id, category, window_days, mention_count, cause, "
              "operational_evidence, confidence, generated_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
              (rid, "service", 90, 3, "short on Fridays",
               json.dumps([{"module": "labor", "metric": "m", "value": "31.4%", "verified": True}] * 3
                          + [{"module": "labor", "metric": "m", "value": "understaffed", "verified": True}]),
               "high"))
    c.commit()
    c.close()
    d = ri.get_diagnoses(rid, db_path=db_path, include_stale=True, include_retired=True)[0]
    assert len(d["operational_evidence"]) == 1
    assert rec_trust.verified_evidence_count(d) == 1


# ── R9 (part): the review diagnosis % reads the CAPPED band (p20) ────────────

def test_r9_p20_review_diagnosis_k1_reads_the_capped_band(db_path):
    import review_intelligence as ri
    rid = _rid(db_path)
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO review_diagnoses (restaurant_id, category, window_days, mention_count, cause, "
              "operational_evidence, confidence, model_confidence, generated_at) "
              "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
              (rid, "service", 90, 8, "Friday dinner is short a cook.", "[]", "medium", "high"))
    c.commit()
    c.close()
    d = ri.get_diagnoses(rid, db_path=db_path, include_stale=True, include_retired=True)[0]
    assert d["confidence"] == "medium" and d["model_confidence"] == "high"
    ev = d["confidence_detail"]["dimensions"]["evidence"]
    # the medium cap (65) holds, not the high one
    assert ev["pct"] is not None and ev["pct"] <= confidence_engine.MODEL_CAPS["medium"]


# ── R2: DSR urgency and action confidence read only supporting cites (p07) ──

def _dsr_facts():
    import dsr
    from dsr import narrative as N
    R = dsr.READY
    return N.Facts({"business_date": "2026-09-22", "blocks": {
        "sales": {"status": R, "metrics": {"net": 19850.4, "net_last_week": 17210.15, "forecast_net": 20500.0}},
        "labor": {"status": R, "metrics": {"pct": 27.1, "pct_last_week": 26.8, "hours": 212.0, "cost": 5379.0}},
        "reviews": {"status": R, "metrics": {"count": 6, "urgent_count": 1, "avg_rating": 4.3}},
        "food": {"status": R, "metrics": {"est_food_cost_pct": 31.2}}}})


def test_r2_p07_unrelated_cites_never_raise_urgency_or_evidence(db_path):
    from dsr import narrative as N
    F = _dsr_facts()
    rid = _rid(db_path)
    ctx = types.SimpleNamespace(restaurant_id=rid, db_path=db_path)
    base = {"text": "Trim one bar shift on Tuesday.", "why": "Labor ran 27.1% of sales.", "dollars_monthly": None,
            "urgency": "before_service", "effort": "low", "kind": "adjust_staffing", "subject": None}
    res = {}
    for label, cites in [("honest", ["labor.pct"]),
                         ("sales pair", ["labor.pct", "sales.net", "sales.net_last_week"]),
                         ("urgent count", ["labor.pct", "reviews.urgent_count"]),
                         ("padding", ["labor.pct", "labor.hours", "labor.cost", "reviews.count"])]:
        a = dict(base, cites=cites)
        assert N.check_item(a, F, action=True) is None
        a2 = N.check_urgency(a, F)
        conf = N.action_confidence(dict(a2, key="dsr_action:adjust_staffing:labor"), F, ctx)
        res[label] = (a2["urgency"], conf["dimensions"]["evidence"]["pct"], conf["pct"])
    assert all(r == res["honest"] for r in res.values()), res
    assert res["honest"][0] == "this_week"


def test_r2_a_cited_figure_the_words_state_still_counts():
    from dsr import narrative as N
    F = _dsr_facts()
    a = {"text": "Push the patio special on Thursday.", "why": "Sales were $19,850, up 15.3% on last week.",
         "kind": "push_sales", "cites": ["sales.net", "sales.net_last_week", "reviews.urgent_count"],
         "urgency": "before_service", "effort": "low", "dollars_monthly": None, "subject": None}
    assert set(N.traced_cites(a, F)) == {"sales.net", "sales.net_last_week"}
    assert "reviews.urgent_count" not in N.supporting_cites(a, F)
    assert N.check_urgency(a, F)["urgency"] == "before_service"      # the stated 15.3% move is real


# ── R3: Ask evidence, counts, owner memory, model-stated confidence ─────────

def test_r3_p06_owner_memory_never_verifies_a_figure(db_path):
    rid = _rid(db_path)
    models.remember_ask_fact(rid, "Our rent is $8,200 a month and the landlord wants 12% more.", db_path=db_path)
    mem = ask_cavnar._memory_context(rid)
    assert ai_guard.UNTRUSTED_OPEN in mem and "$8,200" in mem
    snapshot = "RESTAURANT: Probe Bistro\nLABOR: 31.4% of sales, target 28%\n" + mem
    m = ask_cavnar._meta("Rent is $8,200 a month and a 12% raise is coming.", [snapshot], [], [], "standard", rid)
    assert "$8,200" in m["unverified_figures"] and "12%" in m["unverified_figures"]


def test_r3_p06_invented_counts_are_checked_and_basis_counts_only_checked_kinds(db_path):
    rid = _rid(db_path)
    snapshot = "LABOR: 31.4% of sales, target 28%"
    m = ask_cavnar._meta("You have 47 open complaints and 9 unhappy regulars this month.", [snapshot], [], [],
                         "standard", rid)
    assert any("47" in u for u in m["unverified_figures"])
    basis = m["confidence_detail"]["dimensions"]["evidence"]["basis"]
    assert "figures checked" not in basis or "0 of" in basis
    # a bare "top 3" is not a claim, so it is never reported as checked
    m2 = ask_cavnar._meta("Labor ran 31.4%, one of the top 3 things to fix.", [snapshot], [], [], "standard", rid)
    assert "1 of 1 figures checked" in m2["confidence_detail"]["dimensions"]["evidence"]["basis"]


def test_r3_p06_repeats_and_snapshots_do_not_raise_evidence(db_path):
    rid = _rid(db_path)
    snapshot = "RESTAURANT: Probe Bistro\nLABOR: 31.4% of sales, target 28%"
    tool = json.dumps({"is_live": True, "labor_pct": 31.4})
    snap_tool = json.dumps({"modules_consulted": ["reviews", "labor", "food", "marketing"], "x": 1})
    ev = {}
    for label, corpus, tools_ in (("1 relevant tool", [snapshot, tool], ["read_labor"]),
                                  ("3 same tools", [snapshot, tool, tool, tool], ["read_labor"] * 3),
                                  ("1 empty snapshot", [snapshot, snap_tool], ["read_business_snapshot"]),
                                  ("empty + real", [snapshot, tool, snap_tool],
                                   ["read_labor", "read_business_snapshot"])):
        m = ask_cavnar._meta("Labor ran 31.4% against a 28% target.", corpus, tools_, [], "standard", rid)
        ev[label] = m["confidence_detail"]["dimensions"]["evidence"]["pct"]
    assert ev["3 same tools"] == ev["1 relevant tool"] == ev["empty + real"]
    assert ev["1 empty snapshot"] == 0
    # the snapshot counts only modules it read live data for
    assert ask_cavnar.live_snapshot_modules(json.dumps(
        {"modules_consulted": ["labor", "reviews"], "labor": {"is_live": False}, "reviews": {"n": 3}})) == ["reviews"]


def test_r3_p18_model_stated_confidence_is_rewritten_to_the_computed_one(db_path, monkeypatch):
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    monkeypatch.setattr(ask_cavnar, "build_context", lambda rest: "LABOR: 31.4% of sales. COVERS: 85 covers Friday.")
    for said in ("Trim the Tuesday bar shift. I'm about 85% sure this pays off.",
                 "Trim the Tuesday bar shift. High confidence — this is clear-cut."):
        monkeypatch.setattr(ask_cavnar, "create_with_retry", lambda client, _s=said, **kw: types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=_s)], stop_reason="end_turn"))
        answer, _t, _p, meta = ask_cavnar.ask_with_tools(r, "should I trim tuesday?")
        pct = meta["confidence_detail"]["pct"]
        assert "85%" not in answer and "High confidence" not in answer
        assert meta.get("confidence_rewritten") == 1
        if "High" in said:
            assert f"{pct}% confidence" in answer
        assert answer.startswith("Trim the Tuesday bar shift.")


# ── R5: the cause check (p02), and on the DSR and Ask ───────────────────────

_ANCHORS = ["The kitchen is understaffed on Friday dinner, so tickets back up during the rush.",
            "Tickets slow when the line runs a cook short", "slow service"]


@pytest.mark.parametrize("sentence", [
    "Ratings fell because of the new menu prices.",
    "Ratings fell after the new menu prices went up.",
    "The new menu prices hurt your ratings.",
    "Prices went up, which is why ratings fell.",
    "Complaints are stemming from the price increase.",
    "The drop is attributable to the price increase.",
    "The price increase contributed to the drop.",
    "Prices rose, resulting in lower ratings.",
    "Prices rose 8%, so guests rated you lower.",
    "Slow service complaints rose because a new POS system is dropping tickets.",
    "Ratings fell because the kitchen tickets were lost by the new printer.",
])
def test_r5_p02_every_causal_construction_needs_the_anchor_in_its_cause_clause(sentence):
    assert ai_guard.unsupported_causes(sentence, _ANCHORS) == [sentence]


@pytest.mark.parametrize("sentence", [
    "Ratings fell because the kitchen is understaffed on Friday dinner.",
    "Slow service drove the drop in ratings.",
    "Check again after Friday.",
    "Labor ran 31%, so trim Tuesday.",
])
def test_r5_anchored_or_non_causal_sentences_pass(sentence):
    assert ai_guard.unsupported_causes(sentence, _ANCHORS) == []


def test_r5_p07_dsr_drops_a_cause_its_cites_do_not_hold():
    from dsr import narrative as N
    F = _dsr_facts()
    bad = {"text": "Sales were $19,850 because the patio reopened.", "cites": ["sales.net"]}
    assert "cause" in (N.check_item(bad, F) or "")
    ok = {"text": "Labor ran 27.1% of sales, up 0.3 points because labor hours ran 212.",
          "cites": ["labor.pct", "labor.pct_last_week", "labor.hours"]}
    assert N.check_item(ok, F) is None


def test_r5_ask_flags_a_cause_nothing_it_read_states(db_path):
    rid = _rid(db_path)
    snap = "LABOR: 31.4% of sales. REVIEWS: likely cause: Friday dinner is short a line cook."
    m = ask_cavnar._meta("Ratings fell after the patio closed.", [snap], [], [], "standard", rid)
    assert m["unsupported_causes"]
    assert "cause" in m["confidence_detail"]["dimensions"]["evidence"]["basis"]
    ok = ask_cavnar._meta("Ratings fell because Friday dinner is short a line cook.", [snap], [], [], "standard", rid)
    assert ok["unsupported_causes"] == []


# ── R6: the weekly plan reads the FULL unverified list (p08) ────────────────

def test_r6_p08_the_sixth_invented_figure_is_not_filed(db_path):
    rid = _rid(db_path)
    snapshot = "LABOR: 31.4% of sales against a 28% target. WASTE: $2,400 this month."
    items = [{"title": "Cut Friday prep", "why": "Waste hit $1,100, $1,200, $1,300, $1,400, $1,500 in five weeks."},
             {"title": "Trim Tuesday bar shift", "why": "Tuesday labor cost is $6,600 a week over plan."},
             {"title": "Retrain host stand", "why": "Labor ran 31.4%.", "owner": "Marco Ruiz"}]
    brunch = {"title": "Promote brunch", "why": "Labor ran 31.4% while brunch sales fell after the menu change.",
              "owner": "owner"}
    answer = json.dumps(items)
    meta = ask_cavnar._meta(answer, [snapshot], [], [], "standard", rid)
    assert len(meta["unverified_figures"]) == 5 and len(meta["unverified_all"]) == 6
    anchors = ["Friday dinner is short a cook"]
    got = {it["title"]: strategy_jobs._plan_item_problem(it, meta["unverified_all"], frozenset(), anchors)
           for it in strategy_jobs._parse_plan(answer)}
    assert got["Trim Tuesday bar shift"] == "it states a figure nothing it read supports"
    assert got["Cut Friday prep"] == "it states a figure nothing it read supports"
    assert strategy_jobs._plan_item_problem(brunch, meta["unverified_all"], frozenset(), anchors) == \
        "it states a cause no stored diagnosis supports"
    assert got["Retrain host stand"] is None
    # the owner field is a closed list: a name the model wrote never lands
    assert [it["owner"] for it in strategy_jobs._parse_plan(answer)][-1] == "owner"
