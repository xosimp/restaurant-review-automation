"""ai_reads: Cavnar AI keeps a history of its own reads (memory audit 9/29/26,
M4 "ai_reads") — every stored read, both diagnoses and the monthly review,
kept as append-only rows beside the "current" pointers the screens read."""
import json

import pytest

import ai_reads
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Reads Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _rows(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_a_read_is_kept_and_a_reserved_read_is_not_a_new_row():
    rid = _rid()
    a = ai_reads.record_read(rid, "food_read", "1. Cut salmon par by 20%.", meta={"fingerprint": "f1"})
    b = ai_reads.record_read(rid, "food_read", "1. Cut salmon par by 20%.", meta={"fingerprint": "f1"})
    assert a and a == b
    c = ai_reads.record_read(rid, "food_read", "1. Recount the walk-in on Friday.", meta={"fingerprint": "f2"})
    assert c != a
    rows = _rows("SELECT id, superseded_by, superseded_at FROM ai_reads WHERE restaurant_id=? ORDER BY id", (rid,))
    assert [r["id"] for r in rows] == [a, c]
    assert rows[0]["superseded_by"] == c and rows[0]["superseded_at"] and rows[1]["superseded_at"] is None


def test_recent_reads_are_newest_first_with_owner_dates():
    rid = _rid()
    ai_reads.record_read(rid, "review_read", "This week: service complaints rose.")
    ai_reads.record_read(rid, "food_diagnosis", "Cause: over-portioned wings", subject="driver:wings")
    got = ai_reads.recent_reads(rid)
    assert [g["surface"] for g in got] == ["food_diagnosis", "review_read"]
    assert got[0]["label"] == "the food cost diagnosis" and got[0]["current"] is True
    assert "/" in got[0]["as_of"] and "-" not in got[0]["as_of"]       # M/D/YY, never ISO
    assert ai_reads.recent_reads(rid, surfaces=["review_read"])[0]["summary"].startswith("This week")
    assert ai_reads.recent_reads(rid + 999) == []


def test_insight_store_put_keeps_the_read_as_history_with_its_lines():
    import insight_store
    rid = _rid()
    text = "Waste ran high.\n1. Cut salmon par by 20%.\n2. Recount the walk-in."
    assert insight_store.put(rid, "food", "fp-1", text, raw=text + "\n(raw)")
    row = _rows("SELECT * FROM ai_reads WHERE restaurant_id=?", (rid,))[0]
    assert row["surface"] == "food_read" and row["kind"] == "food" and row["fingerprint"] == "fp-1"
    assert row["raw_text"].endswith("(raw)") and row["shown_text"] == text
    keys = json.loads(row["rec_keys"])
    assert keys == [insight_store.line_key("insight_food", "Cut salmon par by 20%."),
                    insight_store.line_key("insight_food", "Recount the walk-in.")]
    # The current pointer is still the one row insight_cache holds.
    assert insight_store.latest(rid, "food")[0] == text


def test_a_review_payload_is_filed_under_its_surface_with_its_verdict():
    import insight_store
    rid = _rid()
    payload = {"insight": "📊 This week: 4 service complaints.\n✅ Do today: Put a second server on Friday dinner.",
               "validation": {"verdict": "caveat"}}
    insight_store.put(rid, "reviews", "fp-r", payload, raw="raw text")
    row = _rows("SELECT * FROM ai_reads WHERE restaurant_id=?", (rid,))[0]
    assert row["surface"] == "review_read" and row["verdict"] == "caveat"
    assert any(k.startswith("insight_review:") for k in json.loads(row["rec_keys"]))


def _cluster(cat="service"):
    return {"category": cat, "mentions": 7, "window_days": 90, "review_ids": [1, 2, 3]}


def _diag(cause="Friday dinner is short a server on the floor."):
    return {"cause": cause, "alternative_cause": "A slow expo.", "what_would_confirm": "Time the pass.",
            "evidence_review_ids": [1, 2], "operational_evidence": [], "confidence": "medium",
            "model_confidence": "high", "recommended_action": "Add a server to Friday dinner.",
            "expected_outcome": "If the cause is right, service complaints fall within two weeks."}


def test_the_review_diagnosis_saver_keeps_history_and_one_open_claim():
    import review_intelligence as ri
    rid = _rid()
    ri._save_diagnosis(rid, _cluster(), _diag(), {}, models.DB_PATH)
    ri._save_diagnosis(rid, _cluster(), _diag("Friday dinner runs one server short."), {}, models.DB_PATH)
    reads = _rows("SELECT * FROM ai_reads WHERE restaurant_id=? AND surface='review_diagnosis' ORDER BY id", (rid,))
    assert len(reads) == 2 and reads[0]["superseded_by"] == reads[1]["id"]
    assert reads[1]["subject"] == "category:service"
    assert json.loads(reads[1]["rec_keys"]) == ["diag_review:service"]
    assert json.loads(reads[1]["cited_ids"]) == [1, 2]
    claims = _rows("SELECT * FROM ai_claims WHERE restaurant_id=?", (rid,))
    assert len(claims) == 1, "a restated diagnosis is one claim until its horizon"
    c = claims[0]
    assert (c["metric"], c["direction"], c["rec_key"]) == ("complaints:service", "down", "diag_review:service")
    assert c["restated_n"] == 1 and c["last_text"] == "Friday dinner runs one server short."
    assert (c["model_band"], c["capped_band"]) == ("high", "medium")
    # "within two weeks" is held to the complaint share's own floor (28 days).
    from datetime import date
    assert (date.fromisoformat(c["horizon_date"]) - date.fromisoformat(c["after_start"])).days == 28
    # The current pointer is still the one upserted row.
    assert len(_rows("SELECT * FROM review_diagnoses WHERE restaurant_id=?", (rid,))) == 1


def test_the_food_diagnosis_saver_keys_its_claim_like_the_card():
    import food_cost_intelligence as fci
    from client_api import diagnosis_rec_key
    rid = _rid()
    drv = {"drivers": [{"label": "Salmon waste above tolerance", "item": "Salmon", "kind": "waste",
                        "dollars_monthly": 220.0}]}
    result = dict(_diag("Salmon is over-ordered for its shelf life."), headline="Salmon waste is the leak",
                  operational_evidence=[])
    fci._save_diagnosis(rid, drv, result, 220.0, models.DB_PATH)
    read = _rows("SELECT * FROM ai_reads WHERE restaurant_id=? AND surface='food_diagnosis'", (rid,))[0]
    assert read["subject"] == "driver:salmon" and read["shown_text"].startswith("Salmon waste is the leak")
    claim = _rows("SELECT * FROM ai_claims WHERE restaurant_id=?", (rid,))[0]
    assert claim["rec_key"] == diagnosis_rec_key("diag_food", {"drivers": drv["drivers"]})
    assert claim["metric"] in ("item_waste:Salmon", "weekly_waste")


def test_the_monthly_review_that_went_out_is_kept():
    import emails
    rid = _rid()
    review = {"month": "8/1/26 – 8/31/26", "metrics": [], "fix_first": {"what": "Trim Tuesday dinner",
                                                                        "key": "trim_day:Tuesday"}}
    emails._record_monthly_read(rid, "August at Reads Co", "Labor fell two points.", "A calmer August.",
                                review, None, None)
    row = _rows("SELECT * FROM ai_reads WHERE restaurant_id=? AND surface='monthly_review'", (rid,))[0]
    assert "Labor fell two points." in row["shown_text"] and "Trim Tuesday dinner" in row["shown_text"]
    assert json.loads(row["rec_keys"]) == ["trim_day:Tuesday"]


def test_record_read_never_raises_and_refuses_nothing_to_keep(monkeypatch):
    assert ai_reads.record_read(None, "food_read", "x") is None
    assert ai_reads.record_read(1, "food_read", "   ") is None

    def broken(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(models, "get_conn", broken)
    assert ai_reads.record_read(1, "food_read", "x") is None
    assert ai_reads.recent_reads(1) == [] and ai_reads.record_claims(1, "s", "x", [{"text": "y"}]) == []


def test_the_raw_reads_are_registered_for_retention_and_summaries_are_not():
    import ops
    assert ops._RETENTION_DAYS["ai_reads"] == ops._RETENTION_DAYS["ai_claims"] == ai_reads.RAW_KEEP_DAYS
    assert "ai_read_summaries" not in ops._RETENTION_DAYS
