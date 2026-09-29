"""memory_context reaches M4's generators (memory audit 9/29/26): both
diagnoses (tests/test_mem_m4_claims.py), the Reviews and Food reads and the
Monday plan read what Cavnar AI said before and what followed, fenced and
dated M/D/YY."""
import inspect
import json
from datetime import date, datetime, timedelta

import pytest

import ai_guard
import ai_reads
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Wired Co", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test", **kw))


def _claim(rid, surface, subject, text, rec_key, metric):
    ai_reads.record_claims(rid, surface, subject, [{"claim_type": "cause", "text": text, "rec_key": rec_key,
                                                     "metric": metric, "action": "Try the fix."}],
                           today=date.today() - timedelta(days=3))


def test_the_food_read_carries_its_memory_section():
    import inventory
    rid = _rid()
    _claim(rid, "food_diagnosis", "driver:salmon", "Salmon is ordered past its shelf life.", "diag_food:salmon",
           "weekly_waste")
    drivers = [{"label": "Salmon waste above tolerance", "item": "Salmon", "kind": "waste"}]
    sec = inventory.food_read_memory(rid, drivers)
    assert sec.startswith("\n\nWHAT CAVNAR AI REMEMBERS") and "Salmon is ordered past its shelf life." in sec
    assert ai_guard.UNTRUSTED_OPEN in sec
    assert inventory.food_read_memory(rid, []) == ""           # nothing on the subject, nothing said
    src = inspect.getsource(inventory.get_claude_insights)
    assert "food_read_memory(restaurant_id, ranked_drivers)" in src and "{memory_section}" in src


def test_the_reviews_read_carries_its_memory_section():
    import client_api
    rid = _rid()
    _claim(rid, "review_diagnosis", "category:service", "Friday runs a server short.", "diag_review:service",
           "complaints:service")
    sec = client_api.review_read_memory(rid, "service")
    assert "WHAT CAVNAR AI REMEMBERS" in sec and "Friday runs a server short." in sec and sec.endswith("\n\n")
    assert client_api.review_read_memory(rid, "food_quality") == ""
    assert "review_read_memory(rid" in inspect.getsource(client_api)


def test_the_monday_plan_reads_the_latest_claims_across_surfaces(db_path, monkeypatch):
    import ask_cavnar
    import strategy_jobs
    import time_utils
    rid = _rid()
    models.update_restaurant(rid, {"weekly_plan_enabled": 1})
    _claim(rid, "review_diagnosis", "category:service", "Friday runs a server short.", "diag_review:service",
           "complaints:service")
    sec = strategy_jobs.plan_memory(rid)
    assert "Friday runs a server short." in sec and ai_guard.UNTRUSTED_OPEN in sec
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: datetime(2026, 9, 21, 8, 0))
    asked = []
    monkeypatch.setattr(ask_cavnar, "ask_with_tools",
                        lambda r, q, **k: asked.append(q) or ("[]", False, [], {"confidence": "low"}))
    strategy_jobs.run_weekly_plan()
    assert asked and "Friday runs a server short." in asked[0]
