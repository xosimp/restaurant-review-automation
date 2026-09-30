"""Re-audit fix round R7 (9/29/26), INVENTORY-6: the public history (market
events, the own-rating trajectory) was kept forever and read by one screen.
It now reaches the competitor read, the review read, the weekly plan,
marketing and Ask (memory_context "market"), and Ask can read it
(read_market_history)."""
from datetime import date, datetime

import pytest

import ai_guard
import event_memory as em
import memory_context as mc
import models
from models import Restaurant, create_restaurant

NOW = datetime(2026, 9, 29, 9, 0)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    rid = create_restaurant(Restaurant(name="Market Co", owner_email="m@x.test", google_place_id="own"))
    conn = models.get_conn()
    for place, name, kind, frm, to, count, seen in (
            ("p1", "Bella's", "arrived", None, 4.6, 12, "2026-05-10"),
            ("p2", "Old Grill", "rating_down", 4.4, 4.1, 300, "2026-07-02"),
            ("p3", "Ancient Diner", "arrived", None, 4.0, 5, "2025-01-05"),         # past the window
            ("p4", "Tracked Tavern", em.TRACKED, None, 4.2, 90, "2026-08-01")):     # never listed
        conn.execute("INSERT INTO market_events (restaurant_id, place_id, name, kind, from_rating, to_rating, "
                     "review_count, observed_on) VALUES (?,?,?,?,?,?,?,?)", (rid, place, name, kind, frm, to,
                                                                             count, seen))
    conn.commit()
    conn.close()
    em.record_own_rating(rid, 4.5, 200, source="places", at=date(2026, 2, 2))
    em.record_own_rating(rid, 4.4, 230, source="places", at=date(2026, 3, 2))
    em.record_own_rating(rid, 4.2, 260, source="places", at=date(2026, 9, 28))
    return rid


def test_the_market_summary_names_the_last_180_days_and_the_own_rating_change():
    rid = _rid()
    s = em.market_summary(rid, today=NOW.date())
    assert s["n_events"] == 2 and [e["name"] for e in s["events"]] == ["Old Grill", "Bella's"]
    assert "Bella's opened nearby (first seen 5/10/26, 12 Google reviews then)." in s["text"]
    assert "Old Grill's Google rating fell from 4.4 to 4.1 (seen 7/2/26)." in s["text"]
    # From the last reading on or before the window's start (4.4, the week of
    # 3/2/26) to now (4.2).
    assert s["own"]["change"] == -0.2 and s["own"]["from"] == 4.4
    assert "fell from 4.4 to 4.2 between the weeks of 3/2/26 and 9/28/26" in s["own"]["text"]
    assert not any("2026-" in t for t in s["text"])


@pytest.mark.parametrize("surface", ["competitor_read", "review_read", "weekly_plan", "marketing", "ask"])
def test_the_market_reaches_every_surface_that_reasons_about_it(surface):
    rid = _rid()
    block = mc.memory_context(rid, surface, now=NOW)
    assert "market" in block.sections, surface
    assert "Bella's opened nearby" in block.text and "fell from 4.4 to 4.2" in block.text
    assert ai_guard.UNTRUSTED_OPEN in block.text              # Google's listing text is fenced


def test_a_login_without_intel_never_reads_the_market():
    rid = _rid()
    lines = em.market_lines(mc.MemoryRequest(restaurant_id=rid, surface="ask", now=NOW))
    assert lines and all(l["module"] == "intel" for l in lines)
    assert mc.visible(lines[0], {"id": 9, "role": "staff", "is_admin": 0}) is False


def test_ask_reads_the_market_history_behind_intel_view(monkeypatch):
    import ask_cavnar_tools as t
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, **k: NOW)
    rid = _rid()
    out = t._read_market_history(rid, days=180)
    assert out["n_events"] == 2 and out["own_rating"]["change"] == -0.2
    assert out["own_rating"]["from_week_of"] and "-" not in out["own_rating"]["from_week_of"]
    assert "read_market_history" in t._UNTRUSTED_CONTENT_TOOLS and "read_market_history" in t._INTEL_TOOLS
    r = models.get_restaurant(rid)
    staff_view = t.viewer_restaurant(r, {"id": 9, "role": "staff", "is_admin": 0})
    if "intel" in staff_view._ask_denied:
        assert t.tool_allowed("read_market_history", staff_view) is False
    assert t.tool_allowed("read_market_history", r) is True
