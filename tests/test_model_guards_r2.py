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
