"""One open labor issue, kept current (Simple EJ's, 10/5/26): two weeks over
target left "Labor 42.2% against a 32% target" and "Labor 41.7% against a 32%
target" open together, both on a target since changed to 35%, under Home's
own "Labor at 41.7% — 6.7 pts over the 35% target"."""
from pathlib import Path

import pytest

import issues
import models


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(issues, "_notify", lambda *a, **k: None, raising=False)
    return models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)


def _open_labor(db_path, rid):
    c = models.get_conn(db_path)
    try:
        return [dict(r) for r in c.execute("SELECT id, title, status, resolution_note FROM ops_issues "
                                           "WHERE restaurant_id=? AND kind='labor' ORDER BY id", (rid,))]
    finally:
        c.close()


def test_one_issue_is_kept_current_and_an_older_one_closes(db_path, rid):
    issues.create_issue(rid, "labor", "Labor 42.2% against a 32% target", source_key="labor:2026-W40",
                        notify=False, db_path=db_path)
    issues.create_issue(rid, "labor", "Labor 41.7% against a 32% target", source_key="labor:2026-W41",
                        notify=False, db_path=db_path)
    assert issues._sync_labor_issue(rid, 41.7, 35.0, True, "labor:2026-W41", db_path) is None
    rows = _open_labor(db_path, rid)
    assert rows[0]["status"] == "resolved" and "carried on" in rows[0]["resolution_note"]
    assert rows[1]["status"] != "resolved" and rows[1]["title"] == "Labor 41.7% against a 35% target"


def test_back_under_target_closes_it_and_a_new_run_opens_one(db_path, rid):
    issues._sync_labor_issue(rid, 41.7, 31.5, True, "labor:2026-W41", db_path)
    assert _open_labor(db_path, rid)[0]["title"] == "Labor 41.7% against a 31.5% target"
    issues._sync_labor_issue(rid, 33.0, 35.0, False, "labor:2026-W41", db_path)
    assert _open_labor(db_path, rid)[0]["status"] == "resolved"
    issues._sync_labor_issue(rid, 40.0, 35.0, True, "labor:2026-W42", db_path)
    still = [r for r in _open_labor(db_path, rid) if r["status"] != "resolved"]
    assert len(still) == 1 and still[0]["title"] == "Labor 40.0% against a 35% target"


def test_home_leaves_off_an_issue_needs_attention_already_says():
    src = Path(__file__).resolve().parents[1].joinpath("templates", "dashboard.html").read_text(encoding="utf-8")
    assert "labor_over:'labor',labor:'labor'" in src
    fn = src[src.index("  function hbIssuesNotShown(iss,items){"):]
    fn = fn[:fn.index("\n  }\n")]
    assert "said[hbSame(k)]" in fn and "iss[i].kind!=='coverage'" in fn
    assert "iss=hbIssuesNotShown(_hbIssues||[],items)" in src
    assert "var shown=hbIssuesNotShown(_hbIssues,_hbAttnItems);" in src
