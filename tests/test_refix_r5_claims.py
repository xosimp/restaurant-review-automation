"""Memory re-audit fix round 9/29/26 (R5):

  discounted_claims  LOOPS-9: a claim whose tracker the owner discounted ("I
                     didn't make the change") or whose result was confounded is
                     untested / not measurable — never re-read from the raw
                     window as held or not held.
  limits_first       INVENTORY-13: claim_lines chooses the viewer's surfaces and
                     the latest claim per subject in SQL, before its LIMIT.
"""
import json
from datetime import date, timedelta
from types import SimpleNamespace

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
    _exec("UPDATE restaurants SET module_reviews=1 WHERE id=?", (rid,))
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


def _tracked_claim(rid, tracker_status="evaluated", verdict="improved", concurrent="[]"):
    made = TODAY - timedelta(days=40)
    for i in range(10):                                  # the window alone reads "held"
        _review(rid, made - timedelta(days=1 + i * 2), i < 5)
        _review(rid, made + timedelta(days=i * 2), i < 1)
    c = ai_reads.review_diagnosis_claim("service", {"cause": "Friday dinner runs a server short.",
                                                    "recommended_action": "Add a Friday dinner server.",
                                                    "expected_outcome": "Complaints fall within four weeks.",
                                                    "model_confidence": "high", "confidence": "medium"})
    cid = ai_reads.record_claims(rid, "review_diagnosis", "category:service", [c], today=made)[0]
    key = "diag_review:service"
    rec_ledger.present(rid, key, "reviews", "reviews", title="Add a Friday dinner server.")
    _exec("UPDATE rec_instances SET created_at=? WHERE restaurant_id=? AND key=?",
          (f"{(made - timedelta(days=1)).isoformat()} 09:00:00", rid, key))
    rec_ledger.record(rid, key, "accepted", surface="reviews", at=f"{made.isoformat()} 10:00:00",
                      authority="principal")
    cur = _exec("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                "baseline_value, started_on, evaluate_on, status, verdict, concurrent) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, "recommendation", key, "t", "complaints:service", 50.0, made.isoformat(),
                 (made + timedelta(days=28)).isoformat(), tracker_status, verdict, concurrent))
    _exec("UPDATE rec_instances SET tracker_id=? WHERE restaurant_id=? AND key=?", (cur.lastrowid, rid, key))
    return cid, key, cur.lastrowid


def test_i_didnt_make_the_change_scores_the_claim_untested():
    rid = _rid()
    cid, key, tid = _tracked_claim(rid)
    rec_ledger.checkin(rid, key, "no", tracker_id=tid)
    ai_reads.score_due(rid, today=TODAY)
    c = _rows("SELECT * FROM ai_claims WHERE id=?", (cid,))[0]
    assert (c["verdict"], c["verdict_source"]) == ("untested", "tracker")
    assert "wasn't made" in c["verdict_basis"]


def test_a_confounded_result_is_not_measurable_never_held_from_the_window():
    rid = _rid()
    cid, _key, _tid = _tracked_claim(rid, concurrent=json.dumps([{"kind": "change", "key": "x"}]))
    ai_reads.score_due(rid, today=TODAY)
    c = _rows("SELECT * FROM ai_claims WHERE id=?", (cid,))[0]
    assert (c["verdict"], c["verdict_source"]) == ("unmeasurable", "tracker")


def test_a_clear_tracker_result_still_decides():
    rid = _rid()
    cid, _key, _tid = _tracked_claim(rid)
    ai_reads.score_due(rid, today=TODAY)
    c = _rows("SELECT * FROM ai_claims WHERE id=?", (cid,))[0]
    assert (c["verdict"], c["verdict_source"]) == ("held", "tracker")


# ── limits_first ────────────────────────────────────────────────────────────

def test_a_managers_last_read_survives_forty_newer_owner_level_claims():
    rid = _rid()
    _exec("INSERT INTO ai_claims (restaurant_id, surface, subject, claim_type, text, created_at, horizon_date) "
          "VALUES (?,?,?,?,?,datetime('now','-5 days'),?)",
          (rid, "review_diagnosis", "category:service", "cause", "Friday runs short.",
           (TODAY + timedelta(days=20)).isoformat()))
    for i in range(45):
        _exec("INSERT INTO ai_claims (restaurant_id, surface, subject, claim_type, text, created_at, horizon_date) "
              "VALUES (?,?,?,?,?,datetime('now','-1 days'),?)",
              (rid, "weekly_plan", f"plan:{i}", "action", "Owner-level plan.", (TODAY + timedelta(days=20)).isoformat()))
    manager = {"id": 9, "restaurant_id": rid, "role": "manager", "is_admin": 0}
    req = SimpleNamespace(restaurant_id=rid, surface="ask", subjects=(), db_path=None, viewer=manager)
    import memory_context
    if "ask" not in getattr(ai_reads, "CROSS_SURFACES", ()):
        req.surface = next(iter(ai_reads.CROSS_SURFACES))
    lines = ai_reads.claim_lines(req)
    said = [l["text"] for l in lines if l["text"].startswith("LAST READ")]
    assert said and all("Owner-level" not in t for t in said)
    assert any("Friday runs short" in t for t in said)
    assert memory_context       # the visible() rule this reads through
