"""Seeing a day while it happens (workflow audit #17, #18).

Everything else in the product reads yesterday. These two read today — and
must say plainly when they can't, because a POS that cannot be asked during
service is the normal case (RPOWER is month-at-a-time).
"""
from datetime import date, datetime, timedelta

import pytest

import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import intraday, issues, notify, push, auth, pos
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, intraday, issues, notify, push, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr("notify.send_sms", lambda *a, **k: True)


def _rid(db_path, **kw):
    kw.setdefault("name", "Service Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("module_labor", 1)
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _capture(db_path, rid, day, hour, net):
    conn = get_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO pos_intraday (restaurant_id, business_date, captured_hour, "
                 "weekday, net_sales, provider) VALUES (?,?,?,?,?, 'toast')",
                 (rid, day.isoformat(), hour, day.strftime("%A"), net))
    conn.commit(); conn.close()


# ── the pulse ─────────────────────────────────────────────────────────────

def test_a_pos_that_cannot_be_read_during_service_says_so(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    def _no(*a, **k):
        raise pos.POSCapabilityError("rpower cannot be read during service")
    monkeypatch.setattr(pos, "fetch_sales_today", _no)
    out = intraday.capture(rid, now_local=datetime(2026, 9, 21, 14, 0), db_path=db_path)
    assert out["ok"] is False and "service" in out["reason"]


def test_capture_stores_the_running_total_under_this_hour(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    monkeypatch.setattr(pos, "fetch_sales_today", lambda rid_, d: (2450.0, "toast"))
    out = intraday.capture(rid, now_local=datetime(2026, 9, 21, 14, 30), db_path=db_path)
    assert out["ok"] and out["net_sales"] == 2450.0 and out["hour"] == 14
    conn = get_conn(db_path)
    row = conn.execute("SELECT weekday, captured_hour FROM pos_intraday").fetchone()
    conn.close()
    assert row["weekday"] == "Monday" and row["captured_hour"] == 14


def test_no_comparison_until_there_is_a_profile(db_path):
    """One previous Monday is not a baseline — say so instead of a percentage."""
    import intraday
    rid = _rid(db_path)
    today = date(2026, 9, 21)
    _capture(db_path, rid, today, 16, 3000)
    _capture(db_path, rid, today - timedelta(days=7), 16, 4000)
    out = intraday.pulse(rid, now_local=datetime(2026, 9, 21, 16, 30), db_path=db_path)
    assert out["available"] is False and "only 1 past Mondays" in out["reason"]


def test_a_day_running_behind_its_own_weekday_is_flagged(db_path):
    import intraday
    rid = _rid(db_path)
    today = date(2026, 9, 21)
    for weeks in (1, 2, 3):
        _capture(db_path, rid, today - timedelta(days=7 * weeks), 16, 4000)
    _capture(db_path, rid, today, 16, 3000)
    out = intraday.pulse(rid, now_local=datetime(2026, 9, 21, 16, 30), db_path=db_path)
    assert out["available"] and out["pct"] == -25.0
    assert out["off"] is True and out["direction"] == "behind" and out["samples"] == 3


def test_a_normal_day_is_not_worth_interrupting_for(db_path):
    import intraday
    rid = _rid(db_path)
    today = date(2026, 9, 21)
    for weeks in (1, 2, 3):
        _capture(db_path, rid, today - timedelta(days=7 * weeks), 16, 4000)
    _capture(db_path, rid, today, 16, 4150)
    assert intraday.pulse(rid, now_local=datetime(2026, 9, 21, 16, 30), db_path=db_path)["off"] is False


def test_the_pulse_pushes_once_and_only_to_owners(db_path, monkeypatch):
    import intraday, push, strategy_jobs, morning_brief
    rid = _rid(db_path)
    models.update_restaurant(rid, {"open_times_json": '{"Monday": "11:00am"}',
                                   "close_times_json": '{"Monday": "10:00pm"}'}, db_path=db_path)
    monkeypatch.setattr(intraday, "pulse", lambda *a, **k: {
        "available": True, "off": True, "pct": -25.0, "direction": "behind", "weekday": "Monday",
        "net_sales": 3000, "typical": 4000, "samples": 4, "hour": 16})
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 7}])
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 21, 16, 20))
    fired = []
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: fired.append((a[1], k.get("user_ids"))))
    assert strategy_jobs.run_pre_dinner_pulse(db_path=db_path)["sent"] == 1
    assert fired == [("intraday_pulse", {7})]
    assert strategy_jobs.run_pre_dinner_pulse(db_path=db_path)["sent"] == 0, "once a day"


# ── coverage ──────────────────────────────────────────────────────────────

def _schedule(db_path, rid, day, rows, published=True):
    """A week containing `day`. Coverage reads only a PUBLISHED week (#13),
    so the helper publishes unless told not to."""
    from models import save_schedule_history
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "\n".join(
        f"{day.isoformat()},{day.strftime('%A')},{e},{r},{start},10:00pm,8," for e, r, start in rows)
    hid = save_schedule_history(rid, day.isoformat(), day.isoformat(), 40, 40, 30, csv, [], db_path=db_path)
    if published:
        conn = get_conn(db_path)
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        conn.commit(); conn.close()
    return hid


def test_someone_scheduled_and_not_clocked_in_is_named(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    day = date(2026, 9, 21)
    _schedule(db_path, rid, day, [("Dana K", "Server", "11:00am"), ("Jordan P", "Cook", "10:00am")])
    monkeypatch.setattr(pos, "fetch_clock_ins_today",
                        lambda rid_, d: ([{"employee": "Jordan P", "role": "Cook",
                                           "clocked_in_at": "2026-09-21T15:00:00Z"}], "toast"))
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 11, 30), db_path=db_path)
    assert [m["employee"] for m in out["missing"]] == ["Dana K"]
    assert out["missing"][0]["minutes_late"] == 30


def test_a_shift_that_just_started_is_given_grace(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    day = date(2026, 9, 21)
    _schedule(db_path, rid, day, [("Dana K", "Server", "11:00am")])
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda rid_, d: ([], "toast"))
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 11, 5), db_path=db_path)
    assert out["missing"] == []


def test_no_schedule_or_no_live_feed_means_no_claim(db_path, monkeypatch):
    import intraday, pos
    rid = _rid(db_path)
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 12, 0), db_path=db_path)
    assert out["available"] is False and "schedule" in out["reason"]
    _schedule(db_path, rid, date(2026, 9, 21), [("Dana K", "Server", "11:00am")])
    def _no(*a, **k):
        raise pos.POSCapabilityError("rpower has no live clock-in feed")
    monkeypatch.setattr(pos, "fetch_clock_ins_today", _no)
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 12, 0), db_path=db_path)
    assert out["available"] is False and "clock-in" in out["reason"]


def test_a_no_show_becomes_the_managers_issue_once(db_path, monkeypatch):
    import intraday, issues, strategy_jobs, time_utils
    rid = _rid(db_path)
    models.update_restaurant(rid, {"open_times_json": '{"Monday": "10:00am"}',
                                   "close_times_json": '{"Monday": "10:00pm"}'}, db_path=db_path)
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'GM', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit(); conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=False: datetime(2026, 9, 21, 11, 30))
    monkeypatch.setattr(intraday, "coverage_gaps", lambda *a, **k: {
        "available": True, "missing": [{"employee": "Dana K", "role": "Server",
                                        "shift_start": "11:00am", "minutes_late": 30}]})
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 1
    assert strategy_jobs.run_coverage_check(db_path=db_path)["opened"] == 0, "one per person per day"
    opened = issues.list_issues(rid, db_path=db_path)[0]
    assert opened["severity"] == "high" and "Dana K" in opened["title"]


def test_coverage_reads_the_schedule_that_covers_today_not_the_newest(db_path, monkeypatch):
    """From Thursday the newest schedule is next week's auto-draft. Reading
    only that left coverage blind for the rest of the week."""
    import intraday, pos
    rid = _rid(db_path)
    today = date(2026, 9, 21)
    _schedule(db_path, rid, today, [("Dana K", "Server", "11:00am")])
    _schedule(db_path, rid, today + timedelta(days=7), [("Jordan P", "Cook", "10:00am")])
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda rid_, d: ([], "toast"))
    out = intraday.coverage_gaps(rid, now_local=datetime(2026, 9, 21, 11, 30), db_path=db_path)
    assert [m["employee"] for m in out["missing"]] == ["Dana K"]
