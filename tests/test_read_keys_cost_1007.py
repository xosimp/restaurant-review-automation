"""AI cost audit 10/7/26 — the module reads and diagnoses keyed on their data.

  #11  the Labor read's stored copy was keyed on its full prompt, which
       carries the read's OWN last claim (and its live state): every read
       changed its own key, so the next load paid for another (Simple EJ's:
       eight labor reads on 9/30/26)
  #26  the Reviews read's key carried "Today: <date>" — a new Sonnet call
       every morning over the same reviews
  #27  the Food read's key carried "Today's date" and "N days ago"
  #12  review diagnoses had a 24-hour TTL on a daily job: rewritten daily
  #28  the food diagnosis, the same
  #48  a refused Marketing read was held five minutes in memory, so every
       later open resent the identical prompt

No test calls a model or the network: every model call is stubbed.
"""
import types
from datetime import datetime

import pytest

import insight_store
import models


# ── the key helper ──────────────────────────────────────────────────────────

def test_the_key_drops_the_clock_and_keeps_the_data():
    rd = {"prompt_block": "DATA STATE (...):\n- Sales: through 10/6/26, 1 day ago — current",
          "decision": "proceed", "data_state": {"stale_sources": [], "data_age_days": 1}}
    rd2 = dict(rd, prompt_block=rd["prompt_block"].replace("1 day ago", "2 days ago"),
               data_state={"stale_sources": [], "data_age_days": 2})
    p1 = "Today: October 06, 2026\nWaste $120. oldest count 10/1/26, 5 days ago.\n\n" + rd["prompt_block"]
    p2 = "Today: October 07, 2026\nWaste $120. oldest count 10/1/26, 6 days ago.\n\n" + rd2["prompt_block"]
    k1 = insight_store.read_fingerprint(p1, readiness=rd, today="Today: October 06, 2026", week="2026-W41")
    k2 = insight_store.read_fingerprint(p2, readiness=rd2, today="Today: October 07, 2026", week="2026-W41")
    assert k1 == k2, "the same data on the next morning is the same read"
    # A figure, a data date, the week or a source's state each make a new read.
    assert insight_store.read_fingerprint(p2.replace("$120", "$140"), readiness=rd2,
                                          today="Today: October 07, 2026", week="2026-W41") != k1
    assert insight_store.read_fingerprint(p2.replace("10/1/26", "10/2/26"), readiness=rd2,
                                          today="Today: October 07, 2026", week="2026-W41") != k1
    assert insight_store.read_fingerprint(p2, readiness=rd2, today="Today: October 07, 2026",
                                          week="2026-W42") != k1
    rd3 = dict(rd2, decision="caveat", data_state={"stale_sources": ["Sales: 10/1/26"], "data_age_days": 6})
    assert insight_store.read_fingerprint(p2, readiness=rd3, today="Today: October 07, 2026",
                                          week="2026-W41") != k1


# ── #11 the Labor read ─────────────────────────────────────────────────────

from tests.test_mem_m3_labor_read import _analysis, _redirect, _rid  # noqa: E402,F401  (autouse fixture)


def _labor_model(monkeypatch):
    import labor
    seen = {"calls": 0, "prompts": []}

    def fake(*a, **kw):
        seen["calls"] += 1
        seen["prompts"].append(kw["messages"][0]["content"])
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            type="text", text="Sam, labor ran 34% against a 30% target.\n\nRecommendations:\n"
                              "1. Trim Wednesday by one server.")], stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)
    return seen


def _line(text, subject="labor"):
    return {"text": text, "date": "2026-10-01", "source": "model", "subject": subject, "weight": 9.0,
            "trusted": False, "audience": "team"}


def test_the_labor_reads_own_last_claim_does_not_change_its_key(monkeypatch):
    """Same data twice across a process restart: one model call, though the
    read's own last claim — what it said, its live state — moved between."""
    import ai_reads
    import labor
    rid = _rid()
    seen = _labor_model(monkeypatch)
    n = {"i": 0}

    def claims(req):
        n["i"] += 1
        return [_line(f"LAST READ on labor (Labor): cause - Wednesday runs heavy (state {n['i']})")]
    monkeypatch.setattr(ai_reads, "claim_lines", claims)
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert seen["calls"] == 1
    assert "Wednesday runs heavy (state" in seen["prompts"][0], "the claim must be in the prompt for this test"
    labor._NOTE_CACHE.clear()                     # a deploy
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert seen["calls"] == 1, "the read's own last claim changed its key and paid for a new read"


def test_a_new_owner_rule_still_writes_a_new_labor_read(monkeypatch):
    import owner_memory
    import labor
    rid = _rid()
    seen = _labor_model(monkeypatch)
    rules = {"text": "Never cut the Friday closer."}
    monkeypatch.setattr(owner_memory, "rule_lines",
                        lambda req: [dict(_line(rules["text"]), source="owner", trusted=False)])
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert "Never cut the Friday closer." in seen["prompts"][0]
    labor._NOTE_CACHE.clear()
    rules["text"] = "Keep two servers on Wednesday."
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert seen["calls"] == 2, "real memory is still part of the read's key"


# ── #26 the Reviews read, #48 the Marketing refusal ─────────────────────────

from tests.test_rv_adoption_client import (_REVIEW_READ, _msg, _redirect_db,  # noqa: E402,F401
                                           _reviews_restaurant, _rid as _client_rid)


def _on(monkeypatch, day):
    import time_utils
    from zoneinfo import ZoneInfo
    fixed = datetime(2026, 10, day, 9, 0, tzinfo=ZoneInfo("America/Chicago"))
    monkeypatch.setattr(time_utils, "restaurant_now",
                        lambda r=None, naive=False: fixed.replace(tzinfo=None) if naive else fixed)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda rid, naive=False: fixed.replace(tzinfo=None) if naive else fixed)


def test_the_reviews_read_is_not_rewritten_because_the_date_moved(db_path, monkeypatch):
    import ai_utils
    import client_api
    rid = _reviews_restaurant(db_path)
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda client, **kw: (calls.append(kw), _msg(_REVIEW_READ))[1])
    _on(monkeypatch, 6)                           # Tuesday 10/6/26, ISO week 41
    client_api._do_review_insight(rid)
    assert len(calls) == 1
    assert "Today: October 06, 2026" in calls[0]["messages"][0]["content"], "the model still sees the date"
    client_api._insight_cache.clear()
    _on(monkeypatch, 8)                           # Thursday, the same week
    client_api._do_review_insight(rid)
    assert len(calls) == 1, "the same reviews on another day of the week paid for a new read"
    client_api._insight_cache.clear()
    _on(monkeypatch, 12)                          # the next Monday: "This week" is another week
    client_api._do_review_insight(rid)
    assert len(calls) == 2


def test_a_refused_marketing_read_is_held_against_its_data(db_path, monkeypatch):
    import ai_utils
    import client_api
    rid = _client_rid(db_path, module_marketing=1)
    calls = []

    def refuse(client, **kw):
        calls.append(kw)
        m = _msg("I can't help with that.")
        m.stop_reason = "refusal"
        return m
    monkeypatch.setattr(ai_utils, "create_with_retry", refuse)
    out, _ = client_api._do_mkt_insight(rid, raw=True)
    assert len(calls) == 1 and "held this week's marketing brief back" in out["insight"]
    for _ in range(3):
        client_api._insight_cache.clear()         # the five-minute cache is gone; the hold is not
        again, status = client_api._do_mkt_insight(rid, raw=True)
        assert status == 200 and "held this week's marketing brief back" in again["insight"]
    assert len(calls) == 1, "the identical prompt was sent again"
    # Held, not kept as the read: no history row, and the last good read untouched.
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM ai_reads WHERE restaurant_id=? AND surface='marketing_read'",
                        (rid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM insight_cache WHERE restaurant_id=? AND kind='marketing'",
                        (rid,)).fetchone()[0] == 0
    # Past the hold it tries again.
    conn.execute("UPDATE insight_cache SET created_at=datetime('now', '-7 hours') WHERE restaurant_id=? "
                 "AND kind='marketing:refused'", (rid,))
    conn.commit()
    conn.close()
    client_api._insight_cache.clear()
    client_api._do_mkt_insight(rid, raw=True)
    assert len(calls) == 2


# ── #27 the Food read ──────────────────────────────────────────────────────

def _food_model(monkeypatch):
    import inventory
    seen = {"calls": 0}

    def fake(*a, **kw):
        seen["calls"] += 1
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            type="text", text="Waste ran $160 this week.\n1. Trim the Salmon par — $96 a week, low effort")],
            stop_reason="end_turn")
    monkeypatch.setattr(inventory, "create_with_retry", fake)
    monkeypatch.setattr(inventory, "get_client", lambda *a, **k: object())
    return seen


def test_the_food_read_is_not_rewritten_because_the_date_moved(db_path, monkeypatch):
    import inventory
    from tests.test_rv_adoption_insights import _food_analysis
    rid = models.create_restaurant(models.Restaurant(name="Food Key Co", owner_email="f@x.test"),
                                   db_path=db_path)
    seen = _food_model(monkeypatch)

    def analysis(age):
        return _food_analysis(count_freshness={"last_count_at": "2026-10-01", "age_days": age})
    _on(monkeypatch, 8)
    inventory.get_claude_insights(analysis(5), restaurant_id=rid, is_live=True)
    assert seen["calls"] == 1
    _on(monkeypatch, 9)                           # a day later: the count is a day older, nothing else moved
    inventory.get_claude_insights(analysis(6), restaurant_id=rid, is_live=True)
    assert seen["calls"] == 1, "the date and the count's age paid for a new read"
    _on(monkeypatch, 9)
    inventory.get_claude_insights(dict(analysis(6), total_waste_cost_week=180.0), restaurant_id=rid,
                                  is_live=True)
    assert seen["calls"] == 2, "a new figure is a new read"


# ── #12 / #28 diagnoses reused while their evidence is unchanged ───────────

def test_the_reuse_rule():
    import review_intelligence as ri
    now = datetime(2026, 10, 7, 12, 0, 0)
    two_days = {"evidence_hash": "h1", "generated_at": "2026-10-05 12:00:00"}
    assert ri.diagnosis_reusable(two_days, "h1", False, now=now) == (True, False)
    assert ri.diagnosis_reusable(two_days, "h2", True, now=now) == (False, False), "new evidence past the TTL"
    week = {"evidence_hash": "h1", "generated_at": "2026-09-30 11:00:00"}
    assert ri.diagnosis_reusable(week, "h1", True, now=now) == (False, False), "rewritten after 7 days"
    legacy = {"evidence_hash": None, "generated_at": "2026-10-07 02:00:00"}
    assert ri.diagnosis_reusable(legacy, "h1", True, now=now) == (True, True), "stamped on its first reuse"
    assert ri.diagnosis_reusable(None, "h1", True, now=now) == (False, False)


def _review_cluster_setup(db_path, monkeypatch):
    """A stored review diagnosis written two days ago from cluster `c`."""
    import review_intelligence as ri
    rid = models.create_restaurant(models.Restaurant(name="Diag Co", owner_email="d@x.test"), db_path=db_path)
    cluster = {"category": "service", "mentions": 4, "window_days": ri.DIAGNOSIS_WINDOW_DAYS,
               "review_ids": [11, 12, 13, 14], "worst_severity": "high", "first_seen": "9/1/26",
               "last_seen": "10/5/26", "avg_rating": 2.0}
    result = {"cause": "Friday dinner runs one server short.", "alternative_cause": "a",
              "evidence_review_ids": [11, 12], "operational_evidence": [], "confidence": "medium",
              "what_would_confirm": "w", "recommended_action": "Add a server Friday.",
              "expected_outcome": "o", "model_confidence": "medium"}
    ri._save_diagnosis(rid, cluster, result, {}, db_path, evidence_hash=ri.cluster_evidence_hash(cluster))
    conn = models.get_conn(db_path)
    conn.execute("UPDATE review_diagnoses SET generated_at=datetime('now','-2 days'), "
                 "confirmed_at=datetime('now','-2 days') WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    return rid, cluster


def test_a_review_diagnosis_on_the_same_reviews_is_reused_not_rewritten(db_path, monkeypatch):
    import review_intelligence as ri
    rid, cluster = _review_cluster_setup(db_path, monkeypatch)
    monkeypatch.setattr(ri, "complaint_clusters", lambda *a, **k: [dict(cluster)])
    monkeypatch.setattr(ri, "operational_context", lambda *a, **k: {})
    monkeypatch.setattr(ri, "revalidate_stored_diagnosis", lambda *a, **k: None, raising=False)
    import data_health
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {"decision": "proceed",
                                                                            "prompt_block": ""})
    import ai_utils
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: calls.append(1))
    before = ri.get_diagnoses(rid, db_path=db_path, include_stale=True)
    assert before and before[0]["stale"] is True                  # two days since its last check
    out = ri.diagnose(rid, db_path=db_path)
    assert calls == [], "the same reviews paid for a new diagnosis"
    assert out and out[0]["cause"] == "Friday dinner runs one server short." and out[0]["stale"] is False
    after = ri.get_diagnoses(rid, db_path=db_path)
    assert after and after[0]["stale"] is False, "a checked diagnosis is current"
    assert after[0]["as_of"] == before[0]["as_of"], "its words keep the date they were written"


def test_a_new_complaint_rewrites_the_review_diagnosis(db_path, monkeypatch):
    import review_intelligence as ri
    rid, cluster = _review_cluster_setup(db_path, monkeypatch)
    moved = dict(cluster, mentions=5, review_ids=[11, 12, 13, 14, 15])
    assert ri.cluster_evidence_hash(moved) != ri.cluster_evidence_hash(cluster)
    keys = ri._diagnosis_keys(rid, db_path)
    assert ri.diagnosis_reusable(keys["service"], ri.cluster_evidence_hash(moved), False) == (False, False)


def test_a_food_diagnosis_on_the_same_drivers_is_reused_not_rewritten(db_path, monkeypatch):
    import food_cost_intelligence as fci
    rid = models.create_restaurant(models.Restaurant(name="Food Diag Co", owner_email="fd@x.test"),
                                   db_path=db_path)
    drv = {"drivers": [{"kind": "waste", "label": "Salmon waste above tolerance", "item": "Salmon",
                        "dollars_monthly": 412.0, "evidence": "Salmon wasted 12% of what was ordered"}],
           "total_monthly": 412.0}
    result = {"headline": "h", "cause": "Salmon is over-ordered.", "alternative_cause": "a",
              "what_would_confirm": "w", "operational_evidence": [], "confidence": "medium",
              "recommended_action": "Order less salmon.", "expected_outcome": "o"}
    fci._save_diagnosis(rid, drv, result, 412.0, db_path, evidence_hash=fci.driver_evidence_hash(drv))
    conn = models.get_conn(db_path)
    conn.execute("UPDATE food_cost_diagnoses SET generated_at=datetime('now','-3 days'), "
                 "confirmed_at=datetime('now','-3 days') WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    assert fci.get_diagnosis(rid, db_path=db_path, include_stale=True)["stale"] is True
    monkeypatch.setattr(fci, "build_evidence", lambda *a, **k: {"drivers": drv})
    monkeypatch.setattr(fci, "get_restaurant", lambda *a, **k: object(), raising=False)
    import models as _m
    monkeypatch.setattr(_m, "get_restaurant", lambda *a, **k: types.SimpleNamespace(name="Food Diag Co"))
    import ai_utils
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: calls.append(1))
    out = fci.diagnose(rid, db_path=db_path)
    assert calls == [] and out["cause"] == "Salmon is over-ordered." and out["stale"] is False
    assert fci.get_diagnosis(rid, db_path=db_path)["stale"] is False
    moved = {"drivers": [dict(drv["drivers"][0], dollars_monthly=530.0)], "total_monthly": 530.0}
    assert fci.driver_evidence_hash(moved) != fci.driver_evidence_hash(drv)
