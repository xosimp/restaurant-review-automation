"""Audit #23 fix round (10/21/26), group F4 — labor semantics.

A3-14 / A2-18 / A2-24 one business-date boundary; A3-02 / A3-09 the rate
chain; A3-03 station logins by behaviour; A3-04 RPOWER's own overtime pay;
A3-01 / A6-08 open punches; A3-05 restating a reported night's labor;
A3-06 punch edits after 14 days; A3-08 name disambiguation among current
staff; A3-10 nightly sub-steps on the ledger; A3-15 breaks in live labor;
A3-19 roles keyed on the POS job; A3-20 salaried out once; A6-13 a store
with no punches."""
import json
from datetime import date, datetime, timedelta

import pytest

import models
import pos
import pos_archive
import pos_io
import rpower
import time_utils


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    pos_io.reset_for_tests()
    rpower.clear_caches()
    yield
    pos_io.reset_for_tests()


def _restaurant(rid=1, **kw):
    conn = models.get_conn()
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test", "rpower_token": "tok",
            "rpower_cg": 1280, "rpower_store_mid": f"store-{rid}", "timezone": "America/Chicago"}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _q(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


class _R:
    """A restaurant-shaped object with hours, for the pure time rules."""
    def __init__(self, opens=None, closes=None, tz="America/Chicago"):
        self.open_times_json = json.dumps(opens or {})
        self.close_times_json = json.dumps(closes or {})
        self.timezone = tz
        self.id = 1


# ── A3-14 / A2-24: one business-date boundary ────────────────────────────────

def test_the_business_date_turns_over_where_the_punches_do():
    """4:30am is the morning's for "today" exactly as for a punch (both
    turned over at different hours: 5am vs 4am)."""
    r = _R()
    assert time_utils.day_start_minutes(r) == 4 * 60
    assert time_utils.business_date(r, datetime(2026, 10, 21, 4, 30)) == date(2026, 10, 21)
    assert time_utils.business_date(r, datetime(2026, 10, 21, 3, 59)) == date(2026, 10, 20)
    assert rpower.punch_business_date(datetime(2026, 10, 21, 4, 30)) == date(2026, 10, 21)


def test_a_store_that_trades_past_4am_turns_its_day_over_at_its_own_close():
    days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    late = _R(opens={d: "4:00pm" for d in days}, closes={"Saturday": "5:00am", "Friday": "4:00am"})
    assert time_utils.day_start_minutes(late) == 5 * 60
    # A cafe's 3pm close is the same day's, never a boundary.
    cafe = _R(opens={d: "7:00am" for d in days}, closes={d: "3:00pm" for d in days})
    assert time_utils.day_start_minutes(cafe) == 4 * 60
    # A punch at 4:30 at the late bar is the night's.
    assert rpower.punch_business_date(datetime(2026, 10, 25, 4, 30), 5 * 60) == date(2026, 10, 24)
    assert time_utils.business_date(late, datetime(2026, 10, 22, 4, 30)) == date(2026, 10, 21)


def test_the_punch_window_is_the_stores_boundary():
    rid = _restaurant(close_times_json=json.dumps({"Tuesday": "4:30am"}),
                      open_times_json=json.dumps({"Tuesday": "6:00pm"}))
    start, end = rpower._punch_window(date(2026, 10, 20), rid)
    assert start == datetime(2026, 10, 20, 4, 30) and end == datetime(2026, 10, 21, 4, 30)
    assert rpower._punch_window(date(2026, 10, 20))[0] == datetime(2026, 10, 20, 4, 0)


def test_a_night_is_complete_at_the_same_boundary():
    r = _R()
    assert pos.complete_through(r, datetime(2026, 10, 21, 4, 30)) == date(2026, 10, 20)
    assert pos.complete_through(r, datetime(2026, 10, 21, 3, 30)) == date(2026, 10, 19)


def test_tip_windows_and_depletion_use_the_restaurants_own_date():
    import inspect
    import scheduler
    import tip_opportunity
    assert "date.today()" not in inspect.getsource(tip_opportunity._worked).split("except Exception")[0]
    assert "pos.local_complete_through(restaurant_id)" in inspect.getsource(tip_opportunity._worked)
    assert "end = pos.complete_through(r)" in inspect.getsource(scheduler.run_daily_depletion_sync)
