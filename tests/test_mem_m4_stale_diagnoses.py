"""stale_diagnoses: stored diagnoses were served as current long after they
went stale, and anchored as the "likely" cause (memory audit 9/29/26, M4).
A food diagnosis whose lead driver is no longer ranked is retired and kept
as history; the food read, the Reviews read and Home lean on a stored cause
only as far as rec_trust.diagnosis_anchor_strength allows; stale and
retired diagnoses never become the one thing; a stale stored read offers no
controls."""
import json

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Stale Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


_DRV = {"drivers": [{"label": "Salmon waste above tolerance", "item": "Salmon", "kind": "waste",
                     "dollars_monthly": 220.0}]}


def _food_diag(rid, hours_ago=1):
    import food_cost_intelligence as fci
    fci._save_diagnosis(rid, _DRV, {"headline": "h", "cause": "Salmon is over-ordered.", "alternative_cause": "a",
                                    "what_would_confirm": "w", "operational_evidence": [], "confidence": "medium",
                                    "recommended_action": "Order less salmon.", "expected_outcome": "o"},
                        220.0, models.DB_PATH)
    _exec("UPDATE food_cost_diagnoses SET generated_at=datetime('now', ?) WHERE restaurant_id=?",
          (f"-{int(hours_ago)} hours", rid))


# ── retirement ───────────────────────────────────────────────────────────────

def test_no_driver_above_the_floor_retires_the_stored_diagnosis(monkeypatch):
    import food_cost_intelligence as fci
    rid = _rid()
    _food_diag(rid)
    monkeypatch.setattr(fci, "build_evidence", lambda r, db_path=None: {"drivers": {"drivers": [], "reason": "x"}})
    fci.diagnose(rid)
    assert fci.get_diagnosis(rid, include_stale=True) is None, "a retired diagnosis is not served"
    kept = fci.get_diagnosis(rid, include_stale=True, include_retired=True)
    assert kept["cause"] == "Salmon is over-ordered." and kept["retired_at"]
    assert "floor" in kept["retired_reason"]


def test_a_lead_driver_that_dropped_out_of_the_ranking_retires_it_before_a_new_read(monkeypatch):
    import ai_utils
    import food_cost_intelligence as fci
    rid = _rid()
    _food_diag(rid, hours_ago=30)
    ranked = {"drivers": [{"label": "Beef price up 12%", "item": "Beef", "kind": "price", "dollars_monthly": 300.0,
                           "confidence": "high", "difficulty": "medium", "evidence": "e", "if_ignored": "i"}]}
    monkeypatch.setattr(fci, "build_evidence", lambda r, db_path=None: {"drivers": ranked})

    def fail(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(ai_utils, "create_with_retry", fail)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    import data_health
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {"held": True, "reason": "x"})
    with pytest.raises(Exception):
        fci.diagnose(rid)
    assert fci.get_diagnosis(rid, include_stale=True) is None
    assert "Salmon" in fci.get_diagnosis(rid, include_stale=True, include_retired=True)["retired_reason"]


def test_a_new_diagnosis_un_retires_the_row():
    import food_cost_intelligence as fci
    rid = _rid()
    _food_diag(rid)
    assert fci.retire_diagnosis(rid, "test")
    _food_diag(rid)
    d = fci.get_diagnosis(rid)
    assert d is not None and d["retired_at"] is None


def test_lead_still_ranked_matches_by_item_or_label():
    import food_cost_intelligence as fci
    diag = {"drivers": _DRV["drivers"]}
    assert fci.lead_still_ranked(diag, [{"item": "salmon", "label": "x"}])
    assert fci.lead_still_ranked(diag, [{"label": "Salmon waste above tolerance"}])
    assert not fci.lead_still_ranked(diag, [{"item": "Beef", "label": "Beef price up 9%"}])


# ── the food read leans on it only as far as its age allows ─────────────────

def test_the_food_reads_root_cause_block_follows_the_anchor_strength():
    import inventory
    rid = _rid()
    _food_diag(rid, hours_ago=2)
    block, cause, alt = inventory.root_cause_block(rid)
    assert "Most likely: Salmon is over-ordered." in block and cause == ["Salmon is over-ordered."]
    _exec("UPDATE food_cost_diagnoses SET generated_at=datetime('now', '-3 days') WHERE restaurant_id=?", (rid,))
    block, cause, alt = inventory.root_cause_block(rid)
    assert "Most likely" not in block and "An earlier read suggested" in block
    assert cause == [] and "Salmon is over-ordered." in alt
    _exec("UPDATE food_cost_diagnoses SET generated_at=datetime('now', '-60 days') WHERE restaurant_id=?", (rid,))
    block, cause, alt = inventory.root_cause_block(rid)
    assert "too old to lean on" in block and "Salmon" not in block and cause == alt == []


# ── the Reviews read ─────────────────────────────────────────────────────────

def test_the_reviews_read_never_anchors_an_old_cause_as_likely():
    import client_api
    diag = {"category": "service", "cause": "Friday runs short.", "alternative_cause": "Expo",
            "stale": True, "age_hours": 24 * 40}
    ctx = client_api._review_insight_rv_context(
        1, "text", "prompt", rstats={}, top_issues=[], weekly_rows=[], this_week=None, last_week=None,
        trend={}, money={}, bench={}, diags=[diag], op_lines={}, urgent_rows=[], low_count=False)
    assert not any(getattr(a, "strength", None) == "likely" or (isinstance(a, dict) and a.get("strength") == "likely")
                   for a in ctx.cause_anchors)
    fresh = dict(diag, stale=False, age_hours=2)
    ctx2 = client_api._review_insight_rv_context(
        1, "text", "prompt", rstats={}, top_issues=[], weekly_rows=[], this_week=None, last_week=None,
        trend={}, money={}, bench={}, diags=[fresh], op_lines={}, urgent_rows=[], low_count=False)
    assert len(ctx2.cause_anchors) > len(ctx.cause_anchors)


def test_a_stale_stored_reviews_read_offers_no_controls():
    import client_api
    rid = _rid()
    payload = {"insight": "📊 This week: x.\n✅ Do today: Put a second server on Friday dinner.", "stale": True,
               "diagnoses": [{"category": "service", "cause": "c", "recommended_action": "Add a server.",
                              "stale": True, "age_hours": 30}]}
    out = client_api._review_insight_recs(rid, payload)
    assert out["recs"] == []
    assert out["diagnosis"]["answerable"] is False and out["diagnosis"]["controls_withheld"] == "stale"
    conn = models.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM rec_events WHERE restaurant_id=? AND event='shown'",
                            (rid,)).fetchone()[0] == 0, "nothing stale is logged as shown"
    finally:
        conn.close()


def test_a_stale_marketing_read_offers_no_controls():
    import client_api
    rid = _rid()
    out = client_api._mkt_insight_out(rid, "Posts are steady.\n1. Post the patio on Thursday.", False,
                                      {"stale": True})
    assert out["recs"] == []


# ── never the one thing ──────────────────────────────────────────────────────

def test_stale_and_retired_diagnoses_never_become_the_one_thing():
    import business_intelligence as bi
    import food_cost_intelligence as fci
    rid = _rid()
    _food_diag(rid, hours_ago=40)                       # stale
    data = {"food_cost": {"brief": {}}, "reviews": {"brief": {"fix_first": {"what": "service", "evidence": "5"}},
                                                    "diagnoses": [{"category": "service",
                                                                   "recommended_action": "Add a server.",
                                                                   "cause": "c", "stale": True}]}}
    keys = [c["key"] for c in bi.one_thing_candidates(rid, data, links=[])]
    assert not any(k.startswith("diag_food") for k in keys)
    assert not any(k.startswith("diag_review") for k in keys)
    _food_diag(rid, hours_ago=1)
    fci.retire_diagnosis(rid, "gone")
    keys = [c["key"] for c in bi.one_thing_candidates(rid, data, links=[])]
    assert not any(k.startswith("diag_food") for k in keys)
    _food_diag(rid, hours_ago=1)                          # current: it may
    keys = [c["key"] for c in bi.one_thing_candidates(rid, data, links=[])]
    assert any(k.startswith("diag_food") for k in keys)
