"""claims: no model call knew what it said last time or whether it was right
(memory audit 9/29/26, M4 "claims"). A diagnosis's claim is scored at its
horizon, the next prompt on the subject reads it back, and the verdicts feed
Historical Accuracy and the admin check of the model's own confidence."""
import json
from datetime import date, timedelta

import pytest

import ai_reads
import models
import rec_ledger
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


TODAY = date.today()


def _rid(name="Claims Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET module_reviews=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return rid


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur
    finally:
        conn.close()


def _rows(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


_n = [0]


def _review(rid, day, negative, cat="service"):
    _n[0] += 1
    _exec("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, review_date, fetched_at, "
          "sentiment, categories, processed) VALUES (?,?,?,?,?,?,?,?,?,1)",
          (rid, "google", f"x{_n[0]}", 2 if negative else 5, "text", day.isoformat(), day.isoformat(),
           "negative" if negative else "positive", json.dumps([cat] if negative else [])))


def _seed_complaints(rid, made_on, before_neg=5, after_neg=1, per_window=10, horizon=28):
    for i in range(per_window):
        _review(rid, made_on - timedelta(days=1 + i * 2), i < before_neg)
        _review(rid, made_on + timedelta(days=i * 2), i < after_neg)


def _claim(rid, made_on, cat="service"):
    c = ai_reads.review_diagnosis_claim(cat, {"cause": "Friday dinner runs a server short.",
                                              "recommended_action": "Add a Friday dinner server.",
                                              "expected_outcome": "If the cause is right, complaints fall "
                                                                  "within four weeks.",
                                              "model_confidence": "high", "confidence": "medium"})
    return ai_reads.record_claims(rid, "review_diagnosis", f"category:{cat}", [c], today=made_on)[0]


def _answer(rid, key, event, at_day, kind=None):
    rec_ledger.present(rid, key, "reviews", "reviews", title="Add a Friday dinner server.")
    _exec("UPDATE rec_instances SET created_at=? WHERE restaurant_id=? AND key=?",
          (f"{(at_day - timedelta(days=1)).isoformat()} 09:00:00", rid, key))
    if event:
        rec_ledger.record(rid, key, event, surface="reviews", at=f"{at_day.isoformat()} 10:00:00",
                          meta={"kind": kind} if kind else None)


# ── the horizon ──────────────────────────────────────────────────────────────

def test_the_horizon_comes_from_the_models_own_words_within_bounds():
    assert ai_reads.horizon_days("If right, waste falls within two weeks", "weekly_waste") == 14
    assert ai_reads.horizon_days("over the next month", "labor_pct") == 30
    assert ai_reads.horizon_days("within 3 days", "labor_pct") == ai_reads.HORIZON_MIN_DAYS
    assert ai_reads.horizon_days("in six months", "labor_pct") == ai_reads.HORIZON_MAX_DAYS
    # The complaint share needs a month of reviews behind it.
    assert ai_reads.horizon_days("within two weeks", "complaints:service") == 28
    assert ai_reads.horizon_days(None, "labor_pct") == 28


# ── scoring ──────────────────────────────────────────────────────────────────

def test_a_claim_whose_advice_was_taken_and_whose_number_fell_held():
    rid = _rid()
    made = TODAY - timedelta(days=40)
    _seed_complaints(rid, made)
    cid = _claim(rid, made)
    _answer(rid, "diag_review:service", "accepted", made + timedelta(days=2))
    out = ai_reads.score_due(rid, today=TODAY)
    assert out["scored"] == 1
    c = _rows("SELECT * FROM ai_claims WHERE id=?", (cid,))[0]
    assert (c["verdict"], c["action_state"], c["verdict_source"]) == ("held", "taken", "window")
    assert c["baseline_value"] > c["verdict_value"]
    assert "before and after, not proof" in c["verdict_basis"] and "Complaint share" in c["verdict_basis"]


def test_a_claim_whose_advice_was_never_taken_is_untested_not_wrong():
    rid = _rid()
    made = TODAY - timedelta(days=40)
    _seed_complaints(rid, made, before_neg=5, after_neg=5)
    cid = _claim(rid, made)
    _answer(rid, "diag_review:service", None, made)        # shown, never answered
    ai_reads.score_due(rid, today=TODAY)
    c = _rows("SELECT * FROM ai_claims WHERE id=?", (cid,))[0]
    assert c["verdict"] == "untested" and "says nothing about the cause" in c["verdict_basis"]


def test_taken_advice_with_no_change_did_not_hold():
    rid = _rid()
    made = TODAY - timedelta(days=40)
    _seed_complaints(rid, made, before_neg=5, after_neg=5)
    cid = _claim(rid, made)
    _answer(rid, "diag_review:service", "completed", made + timedelta(days=1))
    ai_reads.score_due(rid, today=TODAY)
    assert _rows("SELECT verdict FROM ai_claims WHERE id=?", (cid,))[0]["verdict"] == "not_held"


def test_an_unreadable_number_waits_and_is_given_up_after_the_grace():
    rid = _rid()
    made = TODAY - timedelta(days=40)
    cid = _claim(rid, made)                                  # no reviews at all
    assert ai_reads.score_due(rid, today=TODAY)["waiting"] == 1
    assert _rows("SELECT verdict FROM ai_claims WHERE id=?", (cid,))[0]["verdict"] is None
    later = TODAY + timedelta(days=ai_reads.UNSCORABLE_AFTER_DAYS + 5)
    assert ai_reads.score_due(rid, today=later)["unmeasurable"] == 1


def test_a_claim_before_its_horizon_is_not_scored():
    rid = _rid()
    _claim(rid, TODAY - timedelta(days=3))
    assert ai_reads.score_due(rid, today=TODAY) == {"scored": 0, "unmeasurable": 0, "waiting": 0}
    assert ai_reads.restaurants_due(today=TODAY) == []


def test_the_advices_own_tracker_decides_when_it_was_tracked(monkeypatch):
    rid = _rid()
    made = TODAY - timedelta(days=40)
    _seed_complaints(rid, made, before_neg=5, after_neg=5)     # the window alone reads "not held"
    cid = _claim(rid, made)
    _answer(rid, "diag_review:service", "accepted", made + timedelta(days=1))
    cur = _exec("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                "baseline_value, started_on, evaluate_on, status, verdict, concurrent) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, "recommendation", "diag_review:service", "t", "complaints:service", 50.0,
                 made.isoformat(), (made + timedelta(days=28)).isoformat(), "evaluated", "improved", "[]"))
    _exec("UPDATE rec_instances SET tracker_id=? WHERE restaurant_id=? AND key=?",
          (cur.lastrowid, rid, "diag_review:service"))
    ai_reads.score_due(rid, today=TODAY)
    c = _rows("SELECT * FROM ai_claims WHERE id=?", (cid,))[0]
    assert (c["verdict"], c["verdict_source"], c["tracker_id"]) == ("held", "tracker", cur.lastrowid)


# ── what the next prompt reads ───────────────────────────────────────────────

def test_the_next_diagnosis_of_the_subject_reads_the_last_claim_and_what_followed():
    import ai_guard
    import memory_context
    rid = _rid()
    made = TODAY - timedelta(days=40)
    _seed_complaints(rid, made)
    _claim(rid, made)
    _answer(rid, "diag_review:service", "completed", made + timedelta(days=2))
    ai_reads.score_due(rid, today=TODAY)
    block = memory_context.memory_context(rid, "review_diagnosis",
                                          subjects=["category:service", "diag_review:service"])
    text = block.text
    assert "LAST CLAIM:" in text
    assert "LAST READ on category:service" in text and "Friday dinner runs a server short." in text
    assert ai_guard.UNTRUSTED_OPEN in text, "the model's own words are fenced"
    assert "Since that read: Answered Done" in text and "the expected change held" in text
    assert f"({made.month}/{made.day}/{made.year % 100:02d})" in text and made.isoformat() not in text


def test_an_open_claim_reads_so_far_and_when_it_is_checked():
    rid = _rid()
    made = TODAY - timedelta(days=10)
    _seed_complaints(rid, made)
    _claim(rid, made)
    _answer(rid, "diag_review:service", None, made)        # shown on the card, not answered
    lines = ai_reads.claim_lines(type("R", (), {"restaurant_id": rid, "surface": "review_diagnosis",
                                                "subjects": ("diag_review:service",), "db_path": None})())
    since = [ln for ln in lines if ln["trusted"]][0]["text"]
    assert "Not answered yet" in since or "not answered yet" in since
    assert "so far" in since or "to be checked" in since


def test_other_subjects_and_other_restaurants_never_leak():
    rid, other = _rid(), _rid("Other Co")
    _claim(rid, TODAY - timedelta(days=5), cat="service")
    _claim(other, TODAY - timedelta(days=5), cat="food_quality")
    req = type("R", (), {"restaurant_id": rid, "surface": "review_diagnosis",
                         "subjects": ("category:food_quality",), "db_path": None})()
    assert ai_reads.claim_lines(req) == []


# ── accuracy and the model's confidence ──────────────────────────────────────

def test_scored_claims_of_taken_advice_feed_historical_accuracy():
    import rec_learning
    rid = _rid()
    for i in range(3):
        made = TODAY - timedelta(days=40 + 60 * i)
        cat = ("service", "wait_time", "cleanliness")[i]
        _seed_complaints(rid, made)
        cid = ai_reads.record_claims(rid, "review_diagnosis", f"category:{cat}", [
            ai_reads.review_diagnosis_claim(cat, {"cause": "c", "recommended_action": "a",
                                                  "expected_outcome": "within four weeks"})], today=made)[0]
        _answer(rid, f"diag_review:{cat}", "accepted", made + timedelta(days=1))
    for d in (TODAY, TODAY):
        ai_reads.score_due(rid, today=d)
    rec = rec_learning.kind_record(rid, "diag_review")
    assert rec["claims"]["measured"] >= 1
    assert rec["measured"] >= rec["claims"]["measured"]
    # A claim whose advice has a tracker is the tracker's result, never counted twice.
    _exec("UPDATE rec_instances SET tracker_id=999 WHERE restaurant_id=?", (rid,))
    assert ai_reads.claims_record(rid, "diag_review")["measured"] == 0


def test_the_admin_check_compares_the_models_band_with_what_held():
    rid = _rid()
    for i, (band, verdict) in enumerate([("high", "held")] * 5 + [("low", "not_held")] * 5):
        _exec("INSERT INTO ai_claims (restaurant_id, surface, subject, claim_type, text, model_band, capped_band, "
              "verdict) VALUES (?,?,?,?,?,?,?,?)", (rid, "review_diagnosis", f"s{i}", "cause", "t", band, "low", verdict))
    cal = ai_reads.confidence_calibration(rid)
    by = {b["band"]: b for b in cal["by_model_band"]}
    assert by["high"]["rate"] == 1.0 and by["low"]["rate"] == 0.0 and cal["ordered"] is True
    import admin_ops
    assert admin_ops._model_confidence_check(rid)["scored"] == 10


# ── the quarter that outlives the rows ───────────────────────────────────────

def test_a_closed_quarter_is_summarised_and_kept():
    rid = _rid()
    rid_read = ai_reads.record_read(rid, "review_diagnosis", "Cause: short Friday", subject="category:service",
                                    meta={"rec_keys": ["diag_review:service"]})
    old = TODAY - timedelta(days=120)
    _exec("UPDATE ai_reads SET created_at=? WHERE id=?", (f"{old.isoformat()} 08:00:00", rid_read))
    assert ai_reads.summarise_quarters(rid, today=TODAY) >= 1
    rows = ai_reads.summaries(rid)
    q = f"{old.year}-Q{(old.month - 1) // 3 + 1}"
    assert rows[0]["quarter"] == q and rows[0]["said"].startswith("Cause: short Friday")
    import ops
    assert "ai_read_summaries" not in ops._RETENTION_DAYS


# ── the nightly job ──────────────────────────────────────────────────────────

def test_the_nightly_pass_scores_and_summarises_each_eligible_restaurant():
    import learning_memory
    import scheduler
    rid = _rid()
    made = TODAY - timedelta(days=40)
    _seed_complaints(rid, made)
    _claim(rid, made)
    assert rid in learning_memory.eligible_ids()
    out = learning_memory.nightly(rid, today=TODAY)
    assert out["ok"] and out["steps"]["claims"]["scored"] == 1
    c = scheduler.run_learning_memory()
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(c)


def test_a_demo_account_never_teaches_the_learner():
    import learning_memory
    rid = _rid()
    _exec("UPDATE restaurants SET is_demo=1 WHERE id=?", (rid,))
    assert rid not in learning_memory.eligible_ids()


def test_a_nightly_report_action_with_an_honest_number_is_a_claim():
    rid = _rid()
    narrative = {"actions_tomorrow": [
        {"key": "dsr_action:control_hours:labor", "kind": "control_hours", "text": "Send one server home early.",
         "why": "labor.pct ran 34%"},
        {"key": "dsr_action:reorder:food/salmon", "kind": "reorder", "text": "Order salmon."}]}
    ai_reads.record_read(rid, "dsr_narrative", "Labor ran hot.", meta={"narrative": narrative})
    claims = _rows("SELECT * FROM ai_claims WHERE restaurant_id=?", (rid,))
    assert [(c["rec_key"], c["metric"], c["direction"]) for c in claims] == [
        ("dsr_action:control_hours:labor", "labor_pct", "down")]


def test_a_report_action_is_claimed_on_its_own_slice_in_the_shape_the_report_keeps():
    """dsr.narrative._record_read (M5) passes the night's actions as {key,
    text, urgency}; an action carrying its finer number is scored on it."""
    rid = _rid()
    ai_reads.record_read(rid, "dsr_narrative", "Tuesday ran heavy.", subject="dsr:2026-09-22", meta={
        "actions": [{"key": "dsr_action:control_hours:labor", "text": "Cut one server Tuesday lunch.",
                     "urgency": "next_schedule", "expected_metric": "labor_pct_day:tuesday"},
                    {"key": "dsr_action:adjust_staffing:labor/server", "text": "Add a Friday server.",
                     "urgency": "next_schedule"}]})
    got = {c["rec_key"]: c["metric"] for c in _rows("SELECT * FROM ai_claims WHERE restaurant_id=?", (rid,))}
    assert got["dsr_action:control_hours:labor"] == "labor_pct_day:Tuesday"


# ── both diagnosis prompts read the last claim ───────────────────────────────

def _msg(text):
    import types
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def _stub_model(monkeypatch, reply, seen):
    import ai_utils
    import data_health

    def fake(client, **kw):
        seen.setdefault("prompts", []).append(kw["messages"][0]["content"])
        return _msg(reply)
    monkeypatch.setattr(ai_utils, "create_with_retry", fake)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "is_held", lambda r: False)
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {})


def test_the_food_diagnosis_prompt_carries_the_last_read_of_its_driver(monkeypatch):
    import ai_guard
    import food_cost_intelligence as fci
    rid = _rid()
    ai_reads.record_claims(rid, "food_diagnosis", "driver:salmon", [
        {"claim_type": "cause", "text": "Salmon is ordered past its shelf life.", "action": "Order twice a week.",
         "rec_key": "diag_food:salmon", "metric": "weekly_waste"}], today=TODAY - timedelta(days=9))
    drv = {"available": True, "drivers": [
        {"kind": "waste", "label": "Salmon waste above tolerance", "item": "Salmon", "dollars_monthly": 400.0,
         "confidence": "high", "difficulty": "low", "evidence": "e", "if_ignored": "i"}],
        "total_monthly": 400.0, "total_monthly_deduplicated": 400.0, "degraded_sources": []}
    ev = {"drivers": drv, "food_cost": {"ok": False, "missing": []}, "coverage": {}, "waste_sources": {},
          "weekday": {}, "seasonal": {}, "operational": {}, "profitability": {"available": False}}
    monkeypatch.setattr(fci, "build_evidence", lambda r, db_path=None: ev)
    for name in ("_position_block", "_profit_block", "_trust_block", "_pattern_block", "_operational_block"):
        monkeypatch.setattr(fci, name, lambda *a, **k: "")
    seen = {}
    _stub_model(monkeypatch, json.dumps({
        "headline": "h", "cause": "Salmon waste above tolerance again", "alternative_cause": "a",
        "what_would_confirm": "w", "operational_evidence": [], "confidence": "medium",
        "recommended_action": "Order less salmon", "expected_outcome": "If the cause is right, waste falls "
                                                                      "within four weeks."}), seen)
    fci.diagnose(rid, force=True)
    p = seen["prompts"][0]
    assert "WHAT CAVNAR AI REMEMBERS ABOUT THIS" in p and "LAST READ on driver:salmon" in p
    assert "Salmon is ordered past its shelf life." in p and ai_guard.UNTRUSTED_OPEN in p
    assert "did not hold" in p                    # the rule that uses it


def test_the_review_diagnosis_prompt_says_so_when_nothing_is_remembered(monkeypatch):
    import review_intelligence as ri
    rid = _rid()
    cluster = {"category": "service", "mentions": 5, "review_ids": [], "avg_rating": 2.0, "dish": None,
               "role": None, "daypart": None, "weekday": None, "weekday_pair": None, "complaints": [],
               "worst_severity": "service", "severity_counts": {}, "unclassified": 0, "first_seen": None,
               "last_seen": None, "window_days": 90}
    monkeypatch.setattr(ri, "complaint_clusters", lambda *a, **k: [cluster])
    monkeypatch.setattr(ri, "operational_context", lambda *a, **k: {"notes": []})
    seen = {}
    _stub_model(monkeypatch, json.dumps({"cause": "c", "alternative_cause": "a", "what_would_confirm": "w",
                                         "evidence_review_ids": [], "operational_evidence": [],
                                         "confidence": "low", "recommended_action": "r",
                                         "expected_outcome": "If the cause is right, x."}), seen)
    ri.diagnose(rid, force=True)
    assert ri.NO_MEMORY_LINE in seen["prompts"][0]
