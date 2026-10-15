"""Scheduler audit 10/10/26, workstream H1 — what a schedule version stores:
the headline inline and the whole Shift Quality evaluation once, compressed,
by its content (SCALE-4); a save no longer writes the evaluation twice
(SCALE-34)."""
import json

import pytest

import models
import ops
import schedule_versions as sv
from models import Restaurant, create_restaurant

HEAD = ",".join(sv.COLS) + "\n"
CSV = HEAD + "2026-10-05,Monday,Ana,Server,5:00pm,10:00pm,5,\n"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: {"sent": False})
    yield


def _quality(score=71, n=40):
    return {"checked": True, "score": score, "band": "good", "confidence": {"level": "medium", "why": "x" * 50},
            "recommendations": ["Fill the gap on Friday night: one more Server."],
            "week_dimensions": [{"key": "coverage", "score": 80, "detail": "y" * 300}],
            "shifts": [{"date": "2026-10-05", "daypart": "night", "employee": f"P{i}",
                        "dimensions": [{"k": "x" * 200}]} for i in range(n)]}


def _one(sql, args=()):
    c = models.get_conn()
    try:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _week(rid):
    return models.save_schedule_history(rid, "2026-10-05", "2026-10-11", 5, 100, 30, CSV, [])


def test_a_version_keeps_its_headline_inline_and_its_evaluation_once_by_reference():
    rid = create_restaurant(Restaurant(name="Store Co", owner_email="s@x.test"))
    hid = _week(rid)
    q = _quality()
    sv.append(rid, hid, "generated", CSV, quality=q, saved_by="Cavnar AI", saved_authority="system")
    sv.append(rid, hid, "edited", CSV, quality=q, saved_by="erik", saved_authority="principal")
    rows = [dict(r) for r in models.get_conn().execute(
        "SELECT quality_json, quality_ref FROM schedule_versions WHERE history_id=? ORDER BY version", (hid,))]
    raw = len(json.dumps(q))
    for r in rows:
        head = json.loads(r["quality_json"])
        assert head["score"] == 71 and head["recommendations"] and head["headline"] is True
        assert "shifts" not in head and len(r["quality_json"]) < raw / 10
    assert rows[0]["quality_ref"] == rows[1]["quality_ref"]          # the identical evaluation stored once
    blobs = _one("SELECT COUNT(*) AS n, SUM(LENGTH(body)) AS b, SUM(raw_bytes) AS r FROM quality_blobs")
    assert blobs["n"] == 1 and blobs["b"] < blobs["r"] / 5
    c = models.get_conn()
    try:
        assert sv.version_quality(c, rows[0]["quality_json"], rows[0]["quality_ref"]) == json.loads(json.dumps(q))
    finally:
        c.close()
    # The readers of versions read on as before.
    assert [v["score"] for v in sv.list_versions(rid, hid)] == [71, 71]
    c = models.get_conn()
    try:
        assert sv._open_recommendations(c, hid) == q["recommendations"]
    finally:
        c.close()


def test_a_save_writes_the_evaluation_once_not_twice():
    """SCALE-34: write_on rewrote the week's row AND appended a version with
    the same whole evaluation. The week keeps the current one; the version
    points at the stored copy."""
    rid = create_restaurant(Restaurant(name="Save Co", owner_email="v@x.test"))
    hid = _week(rid)
    q = _quality(score=64)
    conn = models.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        sv.write_on(conn, rid, hid, "edited", CSV, saved_by="erik", quality=q, saved_authority="principal")
        conn.commit()
    finally:
        conn.close()
    week = _one("SELECT LENGTH(quality_json) AS n FROM schedule_history WHERE id=?", (hid,))["n"]
    ver = _one("SELECT LENGTH(quality_json) AS n FROM schedule_versions WHERE history_id=?", (hid,))["n"]
    blob = _one("SELECT SUM(LENGTH(body)) AS n FROM quality_blobs")["n"]
    assert ver + blob < week / 4


def test_the_planned_quality_and_the_rec_outcome_read_the_whole_evaluation():
    import schedule_intel
    import schedule_scorecard
    rid = create_restaurant(Restaurant(name="Plan Co", owner_email="p@x.test"))
    hid = _week(rid)
    q = _quality(score=58)
    sv.append(rid, hid, "published", CSV, quality=q, saved_by="erik", saved_authority="principal")
    conn = models.get_conn()
    try:
        planned = schedule_scorecard._planned_quality(conn, hid)
    finally:
        conn.close()
    assert planned["score"] == 58 and len(planned["shifts"]) == 40


def test_old_intermediate_detail_goes_after_thirty_days_and_its_blob_with_it():
    rid = create_restaurant(Restaurant(name="Thin Co", owner_email="t@x.test"))
    hid = _week(rid)
    for v, (reason, score) in enumerate((("generated", 70), ("edited", 60), ("edited", 50)), start=1):
        sv.append(rid, hid, reason, CSV, quality=_quality(score=score), saved_by="x", saved_authority="principal")
    c = models.get_conn()
    c.execute("UPDATE schedule_versions SET created_at=datetime('now', '-40 days') WHERE history_id=?", (hid,))
    c.execute("UPDATE quality_blobs SET created_at=datetime('now', '-40 days')")
    c.commit()
    c.close()
    assert ops.VERSION_DETAIL_KEEP_DAYS == 30
    out = ops.prune_ledgers()
    assert out.get("schedule_versions_thinned") == 1 and out.get("quality_blobs") == 1
    refs = [r[0] for r in models.get_conn().execute(
        "SELECT quality_ref FROM schedule_versions WHERE history_id=? ORDER BY version", (hid,))]
    assert refs[0] and refs[1] is None and refs[2]                   # generated and newest kept whole
    assert _one("SELECT COUNT(*) AS n FROM quality_blobs")["n"] == 2
    assert [v["score"] for v in sv.list_versions(rid, hid)] == [70, 60, 50]
