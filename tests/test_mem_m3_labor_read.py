"""The Labor read is stored like the other three reads (memory audit 9/29/26,
labor_read).

It lived only in labor._NOTE_CACHE, a process dict: every deploy paid for a
new read with new wording, new insight_labor keys (superseding every
unanswered line) and a reset "as of". It is now one insight_store read per
restaurant and prompt (kind "labor", the model's raw text beside it), and
the process cache is only a front cache.
"""
import types
from datetime import datetime

import pytest

import insight_store
import labor
import models
import response_validation as rv
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "other_tenant_names", lambda rid, db_path=None: set())
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    labor._NOTE_CACHE.clear()
    yield
    labor._NOTE_CACHE.clear()


def _rid():
    return create_restaurant(Restaurant(name="Labor Read Co", owner_email="l@x.test", module_labor=1))


def _analysis(**kw):
    a = {"is_live": True, "total_sales": 10000.0, "total_labor_cost": 3400.0, "overall_labor_pct": 34.0,
         "labor_target": 30, "period_days": 14, "potential_savings": 400.0,
         "potential_savings_weekly": 200.0, "potential_savings_monthly": 866.67,
         "dow_summary": {"Wednesday": 38.0, "Friday": 29.5},
         "overstaffed_days": [{"date": "9/16/26", "day": "Wednesday", "labor_pct": 38.0, "labor_cost": 760.0,
                               "sales": 2000.0, "over_target_dollars": 160.0}],
         "understaffed_days": [], "overtime_risk": [], "role_summary": {},
         "date_range": {"start": "2026-09-14", "end": "2026-09-27", "days": 14}}
    a.update(kw)
    return a


_TEXT = "Sam, labor ran 34% against a 30% target.\n\nRecommendations:\n1. Trim Wednesday by one server."


def _stub(monkeypatch, text=_TEXT):
    seen = {"calls": 0}

    def fake(*a, **kw):
        seen["calls"] += 1
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)],
                                     stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)
    return seen


def test_a_restart_serves_the_stored_read_with_its_words_and_its_time(monkeypatch):
    rid = _rid()
    seen = _stub(monkeypatch)
    first = labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    written = labor.note_generated_at(rid)
    assert seen["calls"] == 1 and "34%" in first
    labor._NOTE_CACHE.clear()                   # a deploy: the process cache is gone
    second = labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert seen["calls"] == 1, "the stored read is served; no second model call"
    assert second == first
    assert rv.validation_of(second) and rv.validation_of(second)["version"] == rv.VERSION
    # "as of" is when the read was written, not when this process met it.
    again = labor.note_generated_at(rid)
    assert again is not None and abs((again - written).total_seconds()) < 5


def test_new_figures_write_a_new_read(monkeypatch):
    rid = _rid()
    seen = _stub(monkeypatch)
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    labor._NOTE_CACHE.clear()
    labor.labor_note(rid, _analysis(overall_labor_pct=36.0, total_labor_cost=3600.0), restaurant_name="R",
                     owner_name="Sam")
    # A new read (not the stored one): the stub's fixed text still says 34%
    # against figures of 36%, so the validation layer sets the T1 attempt
    # aside and the labor_insight run climbs to T2 once (AI orchestration
    # design, 10/7/26) — two model calls for the second read, three in all.
    assert seen["calls"] == 3


def test_the_read_is_stored_with_the_models_own_text_for_revalidation(monkeypatch):
    rid = _rid()
    _stub(monkeypatch)
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    payload, created = insight_store.latest(rid, "labor")
    assert str(payload).startswith("Sam, labor ran 34%")
    conn = models.get_conn()
    try:
        row = conn.execute("SELECT payload FROM insight_cache WHERE restaurant_id=? AND kind='labor'",
                           (rid,)).fetchone()
    finally:
        conn.close()
    import json
    stored = json.loads(row["payload"])
    assert stored["raw"] == _TEXT and stored["_rv"] == rv.VERSION
    assert datetime.strptime(str(created)[:19], "%Y-%m-%d %H:%M:%S")


def test_a_new_validation_engine_revalidates_the_stored_read_without_a_model_call(monkeypatch):
    rid = _rid()
    seen = _stub(monkeypatch)
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    conn = models.get_conn()
    try:
        conn.execute("UPDATE insight_cache SET payload=json_set(payload, '$._rv', 'old') "
                     "WHERE restaurant_id=? AND kind='labor'", (rid,))
        conn.commit()
    finally:
        conn.close()
    labor._NOTE_CACHE.clear()
    out = labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert seen["calls"] == 1 and "34%" in out
    assert rv.validation_of(out)["version"] == rv.VERSION
