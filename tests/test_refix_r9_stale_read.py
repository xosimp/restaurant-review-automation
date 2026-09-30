"""Memory re-audit fix round (9/29/26), R9 — "stale_read" (FORGET-18).

A stored module read was handed to Ask whatever its age, with only a date:
a months-old "this week waste doubled" read as the current Food Cost read.
Past insight_store.MAX_AGE_HOURS it is marked stale, as the competitor read
beside it already was, and the tool's note says what stale means.
"""
import json
from datetime import datetime, timedelta

import pytest

import ask_cavnar_tools as tools
import insight_store
import models
from models import Restaurant, create_restaurant, get_restaurant

OWNER = {"id": 11, "role": "client", "is_admin": 0, "username": "erik"}


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _stamp(days_ago):
    return (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def test_an_old_stored_read_reaches_ask_marked_stale(monkeypatch):
    rid = create_restaurant(Restaurant(name="Stale Co", owner_email="s@x.test"))
    ages = {"food": 90, "reviews": 1}
    monkeypatch.setattr(insight_store, "latest",
                        lambda r, kind, db_path=None: ({"insight": f"{kind}: waste doubled this week"},
                                                       _stamp(ages[kind])) if kind in ages else (None, None))
    view = tools.viewer_restaurant(get_restaurant(rid), OWNER)
    out = json.loads(tools.run_read_tool("read_recent_reads", rid, {"days": 30}, restaurant=view))
    by = {r["what"]: r for r in out["reads"]}
    assert by["the Food Cost read"]["stale"] is True
    assert by["the Reviews read"]["stale"] is False
    assert "stale" in out["note"]


def test_is_stale_matches_the_age_get_stops_serving_at():
    assert insight_store.is_stale(_stamp(8)) and not insight_store.is_stale(_stamp(6))
    assert insight_store.is_stale(None) and insight_store.is_stale("garbage")
    assert not insight_store.is_stale(_stamp(1).replace(" ", "T"))
