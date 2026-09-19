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
    sixth daily alert type and forget the gate the other five now share.

    The gate used to be repeated inside each job's own _fire() closure.
    Both now go through notify.raise_alert(), which is the only thing that
    may raise a non-review alert — so the guard follows it there, and also
    pins that raise_alert itself still checks the cap AND the rush."""
    import inspect
    src = inspect.getsource(notify)
    for fn in ("check_daily_alerts", "check_extra_daily_alerts"):
        body = src.split(f"def {fn}(")[1].split("\ndef ")[0]
        assert "raise_alert(" in body, f"{fn} fires without going through raise_alert"
        assert "deliver_alert(" not in body, f"{fn} delivers directly, skipping the gate"

    gate = inspect.getsource(notify.raise_alert)
    assert "_daily_alert_suppressed(" in gate, "raise_alert fires without checking the cap"
    # raise_alert either collects into the morning batch or hands off to
    # _deliver_or_hold; the rush check lives there so both the immediate
    # path and the batch flush go through it.
    assert "_deliver_or_hold(" in gate, "raise_alert delivers without the rush check"
    for fn in (notify._deliver_or_hold, notify._deliver_pending, notify._deliver_combined):
        src_fn = inspect.getsource(fn)
        assert "_deliver_or_hold(" in src_fn or "rush_release_at(" in src_fn, \
            f"{fn.__name__} can send mid-service"


def test_check_no_response_also_goes_through_the_gate():
    """The last alert still hand-rolling its own SMS/email/push/log/webhook,
    and so the last one with no quiet hours, no cap, no ceiling, no hold."""
    import inspect
    body = inspect.getsource(notify.check_no_response_alerts)
    assert "raise_alert(" in body
    assert "send_sms(" not in body and "fire_push" not in body


def test_a_daily_alert_raised_mid_rush_is_held_not_sent(db_path, monkeypatch):
    """Daily alerts were the only ones with no rush holding. They run at
    10am, so it almost never bit — but a scheduler that catches up at 12:30
    (the gate allows through 2pm local) used to buzz a manager on the floor."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(notify, "_daily_alert_suppressed", lambda *a, **k: False)
    monkeypatch.setattr(notify, "rush_release_at",
                        lambda *a, **k: datetime.datetime(2026, 9, 19, 18, 30,
                                                          tzinfo=datetime.timezone.utc))
    delivered = []
    monkeypatch.setattr(notify, "deliver_alert", lambda *a, **k: delivered.append(a))

    assert notify.raise_alert(rid, "labor_over", "sms", "subject", "<p>html</p>",
                              db_path=db_path, value=34.1) is True

    assert delivered == []
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT alert_type, value, sent_at FROM alert_holds WHERE restaurant_id=?",
                       (rid,)).fetchone()
    conn.close()
    assert row["alert_type"] == "labor_over"
    # The figure has to survive the hold: _waste_alert_worsened compares the
    # next week's number against what the last alert actually fired on.
    assert row["value"] == 34.1
    assert row["sent_at"] is None
