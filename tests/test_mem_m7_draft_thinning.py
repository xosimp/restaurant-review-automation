"""Memory fix round (9/29/26), workstream M7 — "draft_thinning".

Superseded schedule drafts were kept whole forever — a 55-person week's
Shift Quality evaluation is about 130 KB — and that growth is what would
push the backup past the emailed copy's 25 MB. Past 30 days a superseded,
never-published, never-shared draft of a week that is over keeps its
headline (score, band, confidence, the top reasons, the breach counts), its
CSV and its economics; its full evaluation, review and what-if go. Old
intermediate versions keep only their headline; the generated, published
and final versions are never thinned, and the 180-day version prune never
takes the generated draft the schedule learner diffs against.
"""
import json
import sqlite3

import pytest

import models
import ops
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: {"sent": False})
    c = models.get_conn()
    try:
        models._ensure_history_columns(c)
        c.commit()
    finally:
        c.close()
    yield


QUALITY = {"checked": True, "score": 71, "band": "good", "confidence": {"level": "medium", "why": "x" * 50},
           "weaknesses": ["Saturday close is thin", "Two openers overlap", "third"], "strengths": ["Fri covered"],
           "shifts": [{"employee": f"P{i}", "dimensions": [{"k": "x" * 200}]} for i in range(40)]}
REVIEW = {"hard": 1, "soft": 2, "lines": ["⚠ Maria over 40h"] * 20}


def _x(sql, args=()):
    c = models.get_conn()
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _one(sql, args=()):
    c = models.get_conn()
    try:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def _draft(rid, days_ago, week_days_ago, superseded=True, published=False):
    return _x("INSERT INTO schedule_history (restaurant_id, generated_at, week_start, week_end, schedule_csv, "
              "quality_json, quality_score, quality_band, quality_confidence, review_json, what_if_json, economics_json, "
              "superseded_by, published_at) VALUES (?, datetime('now', ?), date('now', ?), date('now', ?), ?,?,?,?,?,?,?,?,?,?)",
              (rid, f"-{days_ago} days", f"{-(week_days_ago + 6):+d} days", f"{-week_days_ago:+d} days", "date,day\n",
               json.dumps(QUALITY), 71, "good", "medium", json.dumps(REVIEW), json.dumps({"x": [1] * 100}),
               json.dumps({"sales": 1000}), 999 if superseded else None,
               None if not published else "2026-01-01 00:00:00"))


def test_an_old_superseded_draft_keeps_its_headline_csv_and_economics():
    rid = create_restaurant(Restaurant(name="Thin Co", owner_email="t@x.test"))
    hid = _draft(rid, 45, 40)
    before = _one("SELECT length(quality_json) AS q FROM schedule_history WHERE id=?", (hid,))["q"]
    out = ops.prune_ledgers()
    assert out.get("schedule_history_thinned") == 1
    row = _one("SELECT * FROM schedule_history WHERE id=?", (hid,))
    q = json.loads(row["quality_json"])
    assert q["score"] == 71 and q["band"] == "good" and q["confidence"] == {"level": "medium"}
    assert q["weaknesses"] == ["Saturday close is thin", "Two openers overlap"] and "shifts" not in q
    assert len(row["quality_json"]) < before / 10
    assert json.loads(row["review_json"])["hard"] == 1 and "lines" not in json.loads(row["review_json"])
    assert row["what_if_json"] is None and row["detail_thinned_at"]
    assert row["schedule_csv"] == "date,day\n" and json.loads(row["economics_json"]) == {"sales": 1000}
    assert (row["quality_score"], row["quality_band"], row["quality_confidence"]) == (71, "good", "medium")
    # The history list still says something honest about it.
    item = [h for h in models.get_schedule_history(rid) if h["id"] == hid][0]
    assert item["quality_score"] == 71 and item["summary_line"]


@pytest.mark.parametrize("kind", ["published", "current", "recent", "week_not_over", "shared", "has_outcome"])
def test_what_is_never_thinned(kind):
    rid = create_restaurant(Restaurant(name="Keep Co", owner_email="k@x.test"))
    if kind == "published":
        hid = _draft(rid, 45, 40, published=True)
    elif kind == "current":
        hid = _draft(rid, 45, 40, superseded=False)
    elif kind == "recent":
        hid = _draft(rid, 10, 5)
    elif kind == "week_not_over":
        hid = _draft(rid, 45, -3)
    else:
        hid = _draft(rid, 45, 40)
        if kind == "shared":
            _x("INSERT INTO schedule_shares (restaurant_id, schedule_id, token, employee_name) VALUES (?,?,?,?)",
               (rid, hid, "tok-1", "Maria"))
        else:
            _x("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart) VALUES (?,?,?,?)",
               (rid, hid, "2026-08-01", "dinner"))
    ops.prune_ledgers()
    row = _one("SELECT quality_json, detail_thinned_at FROM schedule_history WHERE id=?", (hid,))
    assert row["detail_thinned_at"] is None and "shifts" in json.loads(row["quality_json"])


def _version(rid, hid, version, reason, days_ago):
    return _x("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv, quality_json, "
              "created_at) VALUES (?,?,?,?,?,?, datetime('now', ?))",
              (rid, hid, version, reason, "date,day\n", json.dumps(QUALITY), f"-{days_ago} days"))


def test_old_intermediate_versions_are_thinned_and_the_rest_kept_whole():
    rid = create_restaurant(Restaurant(name="Ver Co", owner_email="v@x.test"))
    hid = _draft(rid, 100, 95, superseded=False, published=True)
    gen = _version(rid, hid, 1, "generated", 100)
    mid = _version(rid, hid, 2, "edited", 100)
    pub = _version(rid, hid, 3, "published", 100)
    last = _version(rid, hid, 4, "edited", 100)
    out = ops.prune_ledgers()
    assert out.get("schedule_versions_thinned") == 1
    for vid, whole in ((gen, True), (mid, False), (pub, True), (last, True)):
        q = json.loads(_one("SELECT quality_json FROM schedule_versions WHERE id=?", (vid,))["quality_json"])
        assert ("shifts" in q) is whole, vid
        assert q["score"] == 71
    import schedule_versions
    listed = schedule_versions.list_versions(rid, hid)
    assert [v["score"] for v in listed] == [71, 71, 71, 71], "the version list keeps its scores"


def test_the_180_day_version_prune_keeps_the_generated_draft():
    rid = create_restaurant(Restaurant(name="Gen Co", owner_email="g@x.test"))
    hid = _draft(rid, 300, 295, superseded=False, published=True)
    gen = _version(rid, hid, 1, "generated", 300)
    mid = _version(rid, hid, 2, "edited", 300)
    last = _version(rid, hid, 3, "edited", 300)
    ops.prune_ledgers()
    left = {r[0] for r in sqlite3.connect(models.DB_PATH).execute("SELECT id FROM schedule_versions").fetchall()}
    assert left == {gen, last}, "the diff baseline the learner reads is never pruned"
    assert mid not in left
