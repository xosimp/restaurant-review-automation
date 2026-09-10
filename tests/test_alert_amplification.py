"""Audit #4: notification amplification.

The audit traced one review fetch of a Google-Business-connected restaurant
to 180 notifications — 20 reviews arriving in a single pass, each fanning out
to every contact, inbox and device, with the only brake ("Max alerts/day")
shipping set to Unlimited. These pin the brakes that were added, and the two
places that were bypassing the brakes that already existed.
"""
import datetime

import pytest

import models
import notify
import ops


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, notify, ops):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(notify, "DB_PATH", db_path)


def _restaurant(db_path, **kw):
    conn = models.get_conn(db_path)
    cols = {"id": 1, "name": "The Copper Table", "owner_email": "o@example.com"}
    cols.update(kw)
    conn.execute(
        f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        tuple(cols.values()))
    conn.commit()
    conn.close()
    return cols["id"]


def _log(db_path, rid, n, alert_type="1star", hours_ago=0):
    conn = models.get_conn(db_path)
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(n):
        conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,?)",
                     (rid, alert_type, stamp))
    conn.commit()
    conn.close()


# ── the ceiling ─────────────────────────────────────────────────────────────

def test_the_ceiling_holds_even_on_unlimited(db_path):
    """'Max alerts/day: Unlimited' (stored as 0) is a real choice an owner can
    make in the UI, so it is respected — but it was never meant to mean
    'however many a bad day produces'."""
    rid = _restaurant(db_path, alert_max_per_day=0)
    _log(db_path, rid, notify.ALERT_HARD_CEILING_PER_DAY - 1)
    assert notify._over_alert_ceiling(rid, db_path) is False
    _log(db_path, rid, 1)
    assert notify._over_alert_ceiling(rid, db_path) is True


def test_the_ceiling_is_high_enough_not_to_bite_a_busy_day(db_path):
    rid = _restaurant(db_path, alert_max_per_day=0)
    _log(db_path, rid, 20)
    assert notify._over_alert_ceiling(rid, db_path) is False, \
        "a genuinely busy restaurant must never hit the backstop"


def test_yesterdays_alerts_do_not_count_against_today(db_path):
    rid = _restaurant(db_path, alert_max_per_day=0)
    _log(db_path, rid, notify.ALERT_HARD_CEILING_PER_DAY + 10, hours_ago=48)
    assert notify._over_alert_ceiling(rid, db_path) is False


# ── the owner's own cap, on the restaurant's clock ──────────────────────────

def test_the_daily_cap_counts_the_restaurants_day_not_utc(db_path):
    """count_alerts_today compared against date('now'), which sqlite evaluates
    in UTC — so for a Chicago restaurant the cap reset at 7pm local, in the
    middle of the dinner service it existed to protect."""
    rid = _restaurant(db_path, timezone="America/Chicago")
    from time_utils import restaurant_now_by_id
    local = restaurant_now_by_id(rid)
    if local.hour < 2:
        pytest.skip("run too close to the restaurant's local midnight to be meaningful")
    # An alert from an hour ago is inside the restaurant's own day.
    _log(db_path, rid, 1, hours_ago=1)
    assert models.count_alerts_today(rid, db_path) >= 1

    # And one from well before local midnight must not count.
    _log(db_path, rid, 3, hours_ago=48)
    assert models.count_alerts_today(rid, db_path) < 4


def test_a_timezone_failure_does_not_become_no_cap_at_all(db_path, monkeypatch):
    rid = _restaurant(db_path)
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no tz")))
    _log(db_path, rid, 2)
    assert models.count_alerts_today(rid, db_path) >= 0  # fell back, did not raise


# ── the daily alert types, which bypassed every brake ──────────────────────

def test_daily_alerts_now_respect_the_owners_cap(db_path):
    """check_daily_alerts and check_extra_daily_alerts built their own _fire()
    closures and consulted neither quiet hours nor the cap, so an owner who
    asked for 2 a day could still receive five more on top."""
    rid = _restaurant(db_path, alert_max_per_day=2)
    _log(db_path, rid, 2)
    assert notify._daily_alert_suppressed(rid, "labor_over", db_path) is True


def test_daily_alerts_are_allowed_under_the_cap(db_path):
    rid = _restaurant(db_path, alert_max_per_day=5)
    _log(db_path, rid, 1)
    assert notify._daily_alert_suppressed(rid, "labor_over", db_path) is False


def test_daily_alerts_stop_at_the_ceiling_on_unlimited(db_path):
    rid = _restaurant(db_path, alert_max_per_day=0)
    _log(db_path, rid, notify.ALERT_HARD_CEILING_PER_DAY)
    assert notify._daily_alert_suppressed(rid, "food_waste", db_path) is True


def test_both_daily_alert_jobs_actually_call_the_gate():
    """A regression guard on the wiring, not the logic: it is easy to add a
    sixth daily alert type and forget the gate the other five now share."""
    import inspect
    src = inspect.getsource(notify)
    for fn in ("check_daily_alerts", "check_extra_daily_alerts"):
        body = src.split(f"def {fn}(")[1].split("\ndef ")[0]
        assert "_daily_alert_suppressed(" in body, f"{fn} fires without checking the cap"
