"""Re-audit fix round R7 (9/29/26), CROSSMODULE-7: a scheduled post was
written as an "event" demand signal — never removed when the post was
cancelled or failed, flagged as an event night in event_memory (and left out
of every baseline), told the schedule the night was "ASSUMED busier" and
suppressed trims. A post is kind 'post' now."""
from datetime import date

import pytest

import demand
import demand_signals
import event_memory as em
# Imported here, not inside a test: marketing_publish/marketing_tags bind
# get_conn and DB_PATH at import, so a first import under this file's patch
# left them pointing at this file's temporary database for every later test
# (test_mem_m2_ask_reach read no posts after it, 9/29/26).
import marketing_publish
import marketing_signals
import marketing_tags
import models
import staffing_signals
from models import Restaurant, create_restaurant

DAY = date(2026, 10, 6)                   # a Tuesday


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(demand_signals, "get_conn", fake)
    monkeypatch.setattr(marketing_tags, "get_conn", fake)
    monkeypatch.setattr(marketing_publish, "get_conn", fake)
    yield


def _rid():
    rid = create_restaurant(Restaurant(name="Post Co", owner_email="p@x.test", module_labor=1, module_marketing=1))
    conn = models.get_conn()
    conn.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, "Brisket"))
    conn.commit()
    conn.close()
    return rid


def _q(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def test_a_post_is_a_post_never_an_event_night():
    rid = _rid()
    assert demand_signals.record_post(rid, DAY.isoformat(), "Brisket", platform="instagram", post_id=7) is True
    row = _q("SELECT kind, source, ref FROM demand_signals WHERE restaurant_id=?", (rid,))[0]
    assert row == {"kind": "post", "source": "post", "ref": "post:7"}
    # Not an event flag, so not left out of the baselines either.
    assert em.flags_for(rid, [DAY])[DAY.isoformat()] == []
    # No trim is suppressed by a post; the prep list still names its dish.
    assert staffing_signals.trim_guard(rid, "Tuesday", on_date=DAY)["suppress"] is False
    assert demand.promoted_dishes(rid, DAY)[0]["dish"] == "Brisket"
    # Unmeasured, it adds no lift and is never "ASSUMED busier".
    entry = demand_signals.by_date(rid, [DAY.isoformat()])[DAY.isoformat()]
    assert entry["lift_pct"] is None and not entry.get("assumed") and entry["labels"] == []
    block = demand_signals.prompt_block({DAY.isoformat(): entry}, [DAY.isoformat()])
    assert "ASSUMED" not in block and "no measured effect on sales here yet" in block and "Brisket" in block


def test_a_measured_post_kind_carries_marketings_own_lift(monkeypatch):
    import marketing_signals
    rid = _rid()
    demand_signals.record_post(rid, DAY.isoformat(), "Brisket", platform="instagram", post_id=8)
    monkeypatch.setattr(marketing_signals, "attribution_summary", lambda rid, db_path=None: {
        "ok": True, "by_dish": [{"group": "Brisket", "posts": 3, "median_lift_pct": 12.4, "verdict": "lifted"}],
        "by_occasion": []})
    entry = demand_signals.by_date(rid, [DAY.isoformat()])[DAY.isoformat()]
    assert entry["lift_pct"] == 12 and entry["lift_source"] == "post_measured"
    assert "posts like it" in demand_signals.prompt_block({DAY.isoformat(): entry}, [DAY.isoformat()])
    # A group whose posts did not clearly move sales adds nothing.
    monkeypatch.setattr(marketing_signals, "attribution_summary", lambda rid, db_path=None: {
        "ok": True, "by_dish": [{"group": "Brisket", "posts": 3, "median_lift_pct": 2.0,
                                 "verdict": "no_clear_change"}], "by_occasion": []})
    assert demand_signals.by_date(rid, [DAY.isoformat()])[DAY.isoformat()]["lift_pct"] is None


def test_a_cancelled_post_takes_its_signal_with_it(db_path):
    import marketing_publish
    rid = _rid()
    conn = models.get_conn()
    pid = conn.execute("INSERT INTO marketing_scheduled_posts (restaurant_id, platform, content_type, topic, body, "
                       "scheduled_for, status) VALUES (?,?,?,?,?,?, 'scheduled')",
                       (rid, "instagram", "post", "Brisket", "Brisket tonight", "2026-10-06T11:00:00")).lastrowid
    conn.commit()
    conn.close()
    demand_signals.record_post(rid, DAY.isoformat(), "Brisket", platform="instagram", post_id=pid)
    assert _q("SELECT 1 FROM demand_signals WHERE restaurant_id=?", (rid,))
    assert marketing_publish.cancel_scheduled(pid, rid, db_path=db_path)["ok"] is True
    assert _q("SELECT 1 FROM demand_signals WHERE restaurant_id=?", (rid,)) == []


def test_the_old_post_events_are_rekinded_and_dead_ones_removed_at_boot():
    rid = _rid()
    conn = models.get_conn()
    pid = conn.execute("INSERT INTO marketing_scheduled_posts (restaurant_id, platform, content_type, topic, body, "
                       "scheduled_for, status) VALUES (?,?,?,?,?,?, 'cancelled')",
                       (rid, "instagram", "post", "x", "x", "2026-10-07T11:00:00")).lastrowid
    for d, label, ref in (("2026-10-06", "Post: Brisket", "post:999"), ("2026-10-07", "Post: Wings", f"post:{pid}")):
        conn.execute("INSERT INTO demand_signals (restaurant_id, date, kind, label, source, ref) "
                     "VALUES (?,?, 'event', ?, 'post', ?)", (rid, d, label, ref))
    got = demand_signals.migrate_post_signals(conn)
    conn.commit()
    conn.close()
    assert got == {"rekinded": 2, "removed": 1}
    assert _q("SELECT date, kind FROM demand_signals WHERE restaurant_id=?", (rid,)) == [
        {"date": "2026-10-06", "kind": "post"}]
