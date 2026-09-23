"""Opening hours that run past midnight (re-audit A-1, A-11, A-20).

A close at or after midnight ("1:00am", or "00:00" from the web's time
input) was compared as a same-day time, so every intraday job read a
late-night restaurant as closed all day. The service belongs to the day it
opened — its business date — and every hours comparison goes through
time_utils.
"""
from datetime import date, datetime

import pytest

import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import intraday, issues, notify, push, auth, ops
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, intraday, issues, notify, push, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    claimed = set()

    def _claim(job, period):
        if (job, period) in claimed:
            return False
        claimed.add((job, period))
        return True
    monkeypatch.setattr(ops, "claim_period", _claim)


def _rid(db_path, opens=None, closes=None, **kw):
    import json
    kw.setdefault("name", "Late Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("timezone", "America/Chicago")
    rid = create_restaurant(Restaurant(**kw), db_path=db_path)
    models.update_restaurant(rid, {"open_times_json": json.dumps(opens) if opens else None,
                                   "close_times_json": json.dumps(closes) if closes else None},
                             db_path=db_path)
    return rid


# 2026-09-25 is a Friday.
FRI, SAT = date(2026, 9, 25), date(2026, 9, 26)


def _at(day, hh, mm=5):
    return datetime(day.year, day.month, day.day, hh, mm)


# ── A-1: one helper, and every caller uses it ────────────────────────────

def test_a_one_am_close_is_open_all_evening_and_until_one(db_path):
    import time_utils
    r = models.get_restaurant(_rid(db_path, {"Friday": "4:00pm"}, {"Friday": "1:00am"}), db_path)
    for hh in (16, 18, 20, 22, 23):
        assert time_utils.is_open_at(r, _at(FRI, hh)), hh
    assert time_utils.is_open_at(r, _at(SAT, 0, 30)), "still Friday's service"
    assert not time_utils.is_open_at(r, _at(SAT, 1, 0))
    assert not time_utils.is_open_at(r, _at(FRI, 15, 0))
    assert time_utils.business_date(r, _at(SAT, 0, 30)) == FRI


def test_a_midnight_close_from_the_web_time_input_is_not_closed_all_day(db_path):
    import time_utils
    r = models.get_restaurant(_rid(db_path, {"Friday": "17:00"}, {"Friday": "00:00"}), db_path)
    assert time_utils.is_open_at(r, _at(FRI, 21))
    assert time_utils.is_open_at(r, _at(FRI, 23, 59))
    assert not time_utils.is_open_at(r, _at(SAT, 0, 0))


def test_intraday_jobs_see_a_late_night_restaurant_as_open(db_path):
    import strategy_jobs
    r = models.get_restaurant(_rid(db_path, {"Friday": "4:00pm"}, {"Friday": "1:00am"}), db_path)
    assert all(strategy_jobs._open_now(r, _at(FRI, hh)) for hh in (16, 18, 20, 22, 23))
    assert strategy_jobs._open_now(r, _at(SAT, 0, 30))


def test_a_same_day_close_still_closes(db_path):
    import strategy_jobs
    r = models.get_restaurant(_rid(db_path, {"Friday": "11:00am"}, {"Friday": "10:00pm"}), db_path)
    assert strategy_jobs._open_now(r, _at(FRI, 21))
    assert not strategy_jobs._open_now(r, _at(FRI, 22, 0))
    assert not strategy_jobs._open_now(r, _at(FRI, 10))


def test_the_rush_hold_applies_to_a_late_night_dinner(db_path):
    import notify
    rid = _rid(db_path, {"Friday": "4:00pm"}, {"Friday": "1:00am"})
    assert notify.rush_release_at(rid, "2star", db_path, now_local=_at(FRI, 19, 0)) is not None
    assert notify.rush_release_at(rid, "2star", db_path, now_local=_at(FRI, 12, 15)) is None


def test_the_closing_hour_of_a_late_close_is_past_midnight(db_path):
    import strategy_jobs
    r = models.get_restaurant(_rid(db_path, {"Friday": "4:00pm"}, {"Friday": "1:00am"}), db_path)
    assert strategy_jobs._close_hour(r, _at(FRI, 20)) == 25


def test_a_capture_after_midnight_is_filed_under_the_night_it_belongs_to(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path, {"Friday": "4:00pm"}, {"Friday": "1:00am"})
    monkeypatch.setattr(pos, "fetch_sales_today", lambda rid_, d: (9000.0, "toast"))
    asked = []
    monkeypatch.setattr(pos, "fetch_sales_today", lambda rid_, d: (asked.append(d), (9000.0, "toast"))[1])
    intraday.capture(rid, now_local=_at(FRI, 23), db_path=db_path)
    intraday.capture(rid, now_local=_at(SAT, 0, 30), db_path=db_path)
    assert asked == [FRI, FRI]
    net, hour = intraday.day_total(rid, FRI, db_path)
    assert hour == 24, "the 12:30am reading sorts after 11pm"


# ── A-11: the closing summary belongs to the business date ───────────────

def _closing_run(db_path, monkeypatch, now):
    import strategy_jobs, time_utils, intraday
    sent = []
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: now)
    monkeypatch.setattr(intraday, "capture", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(strategy_jobs, "_reach",
                        lambda rid, t, title, body, data, db, **k: sent.append((title, data)) or 1)
    monkeypatch.setattr(intraday, "closing_summary",
                        lambda rid, day=None, **k: {"available": False, "net_sales": 5000.0,
                                                    "hour": 21, "day": day})
    strategy_jobs.run_closing_summary(db_path=db_path)
    return sent


def test_thursdays_summary_is_not_resent_at_one_am_and_friday_gets_its_own(db_path, monkeypatch):
    _rid(db_path, None, {"Thursday": "10:00pm", "Friday": "1:00am", "Saturday": "10:00pm"})
    thu = date(2026, 9, 24)
    assert len(_closing_run(db_path, monkeypatch, _at(thu, 22))) == 1
    assert _closing_run(db_path, monkeypatch, _at(FRI, 1)) == [], "Thursday already summarised"
    assert _closing_run(db_path, monkeypatch, _at(FRI, 22)) == [], "Friday isn't closed at 10pm"
    fri = _closing_run(db_path, monkeypatch, _at(SAT, 1))
    assert len(fri) == 1 and "Friday" in fri[0][1]["ask_prompt"]


# ── A-20: the night's total includes its last hour ───────────────────────

def test_the_closing_summary_takes_a_reading_at_close(db_path, monkeypatch):
    import strategy_jobs, time_utils, intraday
    rid = _rid(db_path, {"Friday": "11:00am"}, {"Friday": "10:00pm"})
    conn = get_conn(db_path)
    conn.execute("INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, "
                 "net_sales, provider) VALUES (?,?,?,?,?,'toast')", (rid, FRI.isoformat(), 21, "Friday", 4000.0))
    conn.commit(); conn.close()
    import pos
    monkeypatch.setattr(pos, "fetch_sales_today", lambda rid_, d: (5200.0, "toast"))
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: _at(FRI, 22))
    sent = []
    monkeypatch.setattr(strategy_jobs, "_reach",
                        lambda rid_, t, title, body, data, db, **k: sent.append(title) or 1)
    strategy_jobs.run_closing_summary(db_path=db_path)
    assert sent and sent[0].startswith("$5,200"), sent


def test_a_reading_taken_before_close_is_labelled_as_of_its_hour(db_path):
    import strategy_jobs
    title, body = strategy_jobs._closing_text(
        {"available": True, "net_sales": 4000.0, "typical": 5000.0, "pct": -20.0, "direction": "behind",
         "weekday": "Friday", "samples": 4, "hour": 21, "as_of": "9pm"}, None)
    assert title == "$4,000 by 9pm" and "typical" not in title


def test_a_shift_after_midnight_is_checked_against_tonights_schedule(db_path, monkeypatch):
    """The coverage check reads the business date's published week: at
    12:50am during a 2am close, Friday's schedule is the one being worked,
    and a 12:30am shift on it is 20 minutes late — not ignored."""
    import intraday, pos
    rid = _rid(db_path, opens={"Friday": "4:00pm"}, closes={"Friday": "2:00am"})
    csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
           f"{FRI},Friday,Dana K,Barback,12:30am,2:00am,1.5,\n")
    hid = models.save_schedule_history(rid, FRI.isoformat(), FRI.isoformat(), 2, 2, 30, csv, [], db_path=db_path)
    c = get_conn(db_path)
    c.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    c.commit(); c.close()
    seen = []
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda r, d: (seen.append(d) or [], "toast"))
    gaps = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 26, 0, 50), db_path=db_path)
    assert seen == [FRI]
    assert [(m["employee"], m["minutes_late"]) for m in gaps["missing"]] == [("Dana K", 20)]
