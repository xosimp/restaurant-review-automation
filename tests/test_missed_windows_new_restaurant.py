"""Ops digest 10/17/26: the showcase (Halsted & Rye) was created at 5:55pm
Chicago and first seen by the scheduler at 6pm, after every window of its
first day had closed - five "windows missed" in the ops email for windows
that opened before it existed."""
from datetime import datetime

import ops
import scheduler
from models import Restaurant, create_restaurant, get_conn
from tests.test_fix_d_sweeps import db  # noqa: F401  (models.get_conn / DB_PATH on the test database)


def _rid(db_path, created_at, tz="America/Chicago"):
    return create_restaurant(Restaurant(name="New Co", owner_email="n@x.test", created_at=created_at, timezone=tz),
                             db_path=db_path)


def _rows(db_path, rid):
    c = get_conn(db_path)
    try:
        return [r[0] for r in c.execute("SELECT job FROM missed_windows WHERE restaurant_id=?", (rid,)).fetchall()]
    finally:
        c.close()


def test_a_window_that_opened_before_the_restaurant_existed_is_not_missed(db):
    rid = _rid(db, "2026-10-16T17:55:41")
    r = type("R", (), {"id": rid})()
    # The pulse's 4-6pm window, seen first at 6pm: opened before the restaurant existed.
    assert scheduler.local_due(r, 16, until=18, claim_key="pre_dinner_pulse",
                               now_local=datetime(2026, 10, 16, 18, 0)) is False
    assert _rows(db, rid) == []
    # The next day's window closed unclaimed is a real miss.
    scheduler.local_due(r, 16, until=18, claim_key="pre_dinner_pulse", now_local=datetime(2026, 10, 17, 18, 0))
    assert _rows(db, rid) == ["pre_dinner_pulse"]


def test_a_restaurant_made_before_the_window_opened_still_records_a_miss(db):
    rid = _rid(db, "2026-10-16T08:00:00")
    assert ops.record_missed_window("pre_dinner_pulse", rid, "2026-10-16", window_open_hour=16) is True
    # No opening hour: the first day is never a miss.
    assert ops.record_missed_window("morning_brief", rid, "2026-10-16") is False
    assert ops.record_missed_window("morning_brief", rid, "2026-10-17") is True


def test_created_at_is_read_in_the_restaurants_own_zone(db):
    # 5:55pm Chicago is 3:55pm in Los Angeles: a 4pm LA window opened after it.
    rid = _rid(db, "2026-10-16T17:55:41", tz="America/Los_Angeles")
    assert ops.record_missed_window("pre_dinner_pulse", rid, "2026-10-16", window_open_hour=16) is True


def test_the_digest_says_when_in_chicago_time():
    assert ops._since_ct("2026-10-16 13:00:43") == "10/16/26 8:00am CT"
