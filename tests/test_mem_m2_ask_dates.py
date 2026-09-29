"""Memory audit 9/29/26 (workstream M2): the dates Ask's memory reads carry,
and the age of the competitor read it presents.

  iso_dates       — the model echoes the dates it is handed, so every date
                    in Ask's memory builders is M/D/YY, never ISO.
  competitor_age  — a competitor read past 14 days (or with no date) is
                    said to be stale and its recommendations are dropped,
                    in the snapshot and in read_competitors alike.
"""
import json
import re
from datetime import datetime, timedelta

import pytest

import ask_cavnar
import ask_cavnar_tools as tools
import models
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ISO = re.compile(r"\b20\d\d-\d\d-\d\d\b")


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(**kw):
    kw.setdefault("name", "Date Co")
    kw.setdefault("owner_email", "dates@x.test")
    return create_restaurant(Restaurant(**kw))


# ── iso_dates ───────────────────────────────────────────────────────────────

def test_client_since_is_mdy_not_iso():
    rid = _rid()
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET created_at='2026-09-01 14:00:00' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    text = ask_cavnar._profile_context(get_restaurant(rid))
    assert "Client since: 9/1/26" in text
    assert not ISO.search(text)


def test_proposals_are_dated_mdy_on_the_restaurants_own_day():
    rid = _rid(timezone="America/Chicago")
    pid = models.log_ask_action(rid, "send_supplier_order", summary="Order from Fresh Co", outcome="proposed")
    conn = models.get_conn()
    # 3am UTC on 9/28 is still 9/27 in Chicago.
    conn.execute("UPDATE ask_cavnar_actions SET created_at='2026-09-28 03:00:00' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    text = ask_cavnar._commitments_context(rid)
    assert "proposed 9/27/26" in text
    assert not ISO.search(text)


def test_remembered_facts_are_dated_mdy():
    rid = _rid()
    models.remember_ask_fact(rid, "We close Mondays in January", kind="context")
    conn = models.get_conn()
    conn.execute("UPDATE ask_memory SET created_at='2026-09-20 18:00:00' WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    text = ask_cavnar.build_context(get_restaurant(rid))
    assert "9/20/26" in text
    assert "2026-09-20" not in text


# ── competitor_age ──────────────────────────────────────────────────────────

def _intel(rid, updated_at):
    blob = json.dumps({"competitors": [{"name": "Mio Modo", "rating": 4.5, "review_count": 300}],
                       "insight": "Recommendations:\n1. Push the patio on Fridays\n2. Post more photos\n"})
    fields = {"competitor_intel": blob}
    if updated_at is not None:
        fields["competitor_updated_at"] = updated_at
    update_restaurant(rid, fields)


def test_a_fresh_competitor_read_keeps_its_recommendations_and_says_its_age():
    rid = _rid()
    _intel(rid, (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"))
    text = ask_cavnar._intel_context(rid)
    assert "Push the patio on Fridays" in text
    assert "3 days ago" in text and not ISO.search(text)


def test_a_competitor_read_past_14_days_is_stale_and_drops_its_recommendations():
    rid = _rid()
    when = datetime.utcnow() - timedelta(days=42)
    _intel(rid, when.strftime("%Y-%m-%d %H:%M:%S"))
    text = ask_cavnar._intel_context(rid)
    assert "stale" in text
    assert f"from {when.month}/{when.day}/{when.year % 100:02d}" in text
    assert "Push the patio" not in text
    assert not ISO.search(text)
    out = json.loads(tools.run_read_tool("read_competitors", rid, {}))
    assert out["stale"] is True and out["recommendations"] == []
    assert "stale" in out["_freshness_note"]


def test_an_undated_competitor_read_is_never_presented_as_current():
    rid = _rid()
    _intel(rid, None)
    text = ask_cavnar._intel_context(rid)
    assert "date unknown, stale" in text and "Push the patio" not in text


def test_read_competitors_leaves_out_a_line_the_owner_answered():
    """ask_reach: a line answered "Not for us" on Intel is not suggested
    again through the tool (the snapshot's own intel_open_recs rule)."""
    import insight_store
    import rec_ledger
    rid = _rid()
    _intel(rid, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    key = insight_store.line_key("insight_intel", "Push the patio on Fridays")
    rec_ledger.present(rid, key, "intel", "intel", title="Push the patio on Fridays")
    rec_ledger.record(rid, key, "dismissed", surface="intel", meta={"kind": "not_for_us"})
    out = json.loads(tools.run_read_tool("read_competitors", rid, {}))
    assert out["recommendations"] == ["Post more photos"]
