"""Memory re-audit 9/29/26, workstream R3 — labor_cache (INVENTORY-5).

The labor read's front cache keyed on the data, the answered lines and the
local day — not the owner's memory its prompt carries — so "never cut the
Friday closer" told at 10am was missing from the labor read until the data,
the day or the process changed. Every memory write now bumps a per-restaurant
version the key includes.
"""
import pytest

import labor
import models
import owner_memory
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    labor._NOTE_CACHE.clear()
    yield
    labor._NOTE_CACHE.clear()


def test_a_memory_write_reaches_the_next_labor_read(monkeypatch):
    rid = create_restaurant(Restaurant(name="Cache Co", owner_email="cache@x.test"))
    calls = []
    monkeypatch.setattr(labor, "get_claude_insights", lambda analysis, **k: calls.append(1) or f"note {len(calls)}")
    analysis = {"period": "week", "overall_labor_pct": 31.0}
    assert labor.labor_note(rid, analysis) == "note 1"
    assert labor.labor_note(rid, analysis) == "note 1", "the same state is served from the cache"
    v = owner_memory.memory_version(rid)
    owner_memory.remember(rid, "Never cut the Friday closer", kind="constraint", modules=["labor"],
                          user={"id": 1, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"})
    assert owner_memory.memory_version(rid) > v
    assert labor.labor_note(rid, analysis) == "note 2"
    owner_memory.forget(rid, "Never cut the Friday closer")
    assert labor.labor_note(rid, analysis) == "note 3"


def test_a_memory_write_drops_the_reads_route_caches(monkeypatch):
    import client_api
    rid = create_restaurant(Restaurant(name="Cache Co", owner_email="cache@x.test"))
    client_api._insight_cache[f"labor-insight:{rid}"] = ("stale", 0)
    client_api._insight_cache[f"review-insight:{rid}"] = ("stale", 0)
    owner_memory.invalidate(rid)
    assert f"labor-insight:{rid}" not in client_api._insight_cache
    assert f"review-insight:{rid}" not in client_api._insight_cache
