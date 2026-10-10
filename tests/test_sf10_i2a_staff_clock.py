"""The staff app reads each shift on the restaurant's clock (schedule audit
10/10/26 BUGSLIFE-21).

StaffTime.span added a shift's minutes as seconds to the phone's local
midnight, so on a 23- or 25-hour day every time was an hour off: on 11/1/26
(clocks back) a 5pm shift read as 4pm and "On now" and Running late opened an
hour early. The phone now builds each time from the day's components, in the
restaurant's zone — which /staff/api/shifts now names — on the row's business
date (the server's small-hours rule, schedule_rules._night_offset). The Swift
behaviour is pinned by StaffTodayTests.swift; here, the server half and the
constants the two sides must share.
"""
import datetime as dt
import os
import re
import sys

import pytest
from flask import Flask

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements, intraday, people, task_sheets  # noqa: E401,F401

import auth
import models
import time_utils
from auth import create_staff_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGIC = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Staff", "StaffTodayLogic.swift")
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r=None, naive=False: dt.datetime(2026, 10, 30, 9, 0))
    init_auth(db_path=db_path)
    return db_path


def _client(db_path, timezone=None):
    rid = create_restaurant(Restaurant(name="Clock Co", owner_email="c@x.test"), db_path=db_path)
    if timezone is not None:
        c = models.get_conn(db_path)
        c.execute("UPDATE restaurants SET timezone=? WHERE id=?", (timezone, rid))
        c.commit()
        c.close()
    models.add_manual_team_member(rid, "Ana", role="Server", db_path=db_path)
    text = HEADER + "\n2026-11-01,Sunday,Ana,Server,5:00pm,11:00pm,6,"
    hid = models.save_schedule_history(rid, "2026-10-26", "2026-11-01", 0, 0, 30, text, [], db_path=db_path)
    c = models.get_conn(db_path)
    c.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    c.commit()
    c.close()
    uid = create_user(rid, f"ana{rid}", f"ana{rid}@x.test", "unused", db_path=db_path)
    upsert_membership(uid, rid, "employee", employee_name="Ana", db_path=db_path)
    app = Flask(__name__, template_folder="../templates")
    from staff_routes import staff_bp
    app.register_blueprint(staff_bp)
    client = app.test_client()
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    return client


def test_the_shifts_payload_names_the_restaurants_zone(db):
    body = _client(db, timezone="America/Denver").get("/staff/api/shifts").get_json()
    assert body["ok"] and body["timezone"] == "America/Denver"
    assert any(d["date"] == "2026-11-01" and d["shifts"] for d in body["week"])


def test_a_restaurant_with_no_zone_is_read_on_operator_time_never_the_phones(db):
    body = _client(db).get("/staff/api/shifts").get_json()
    assert body["timezone"] == time_utils.OPERATOR_TZ
    # A name the server can't resolve is the clock every server reader uses.
    body = _client(db, timezone="Mars/Base").get("/staff/api/shifts").get_json()
    assert body["timezone"] == time_utils.restaurant_tz("Mars/Base").key == time_utils.OPERATOR_TZ


def test_the_phone_builds_times_from_the_days_components_not_seconds_from_midnight():
    src = open(LOGIC, encoding="utf-8").read()
    span = src[src.index("static func span("):src.index("static func nightOffsetDays(")]
    assert "addingTimeInterval" not in span, "minutes added as seconds drift an hour on a 23/25-hour day"
    assert "calendar.date(from: c)" in span and "byAdding: .day" in span
    view = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Staff", "StaffTodayView.swift"),
                encoding="utf-8").read()
    assert "hero(week: week, now: now, calendar: shifts.calendar)" in view
    assert "offersRunningLate(hero, now: now, calendar: shifts.calendar)" in view
    store = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Staff", "StaffPortalStore.swift"),
                 encoding="utf-8").read()
    assert "calendar: response.calendar" in store


def test_the_phones_small_hours_rule_is_the_servers():
    """A row's date is its business date: the phone's night offset uses the
    server's own hours (schedule_rules._night_offset, time_utils)."""
    src = open(LOGIC, encoding="utf-8").read()

    def minutes(name):
        m = re.search(rf"static let {name} = (\d+) \* 60\b", src)
        assert m, name
        return int(m.group(1)) * 60
    assert minutes("businessDayStartMinutes") == time_utils.BUSINESS_DAY_START_HOUR * 60
    assert minutes("earlyPrepFromMinutes") == schedule_rules._EARLY_PREP_FROM
    assert minutes("overnightLatestBeforeMinutes") == schedule_rules._OVERNIGHT_LATEST_BEFORE
    # The cases the Swift test pins, read by the server's rule.
    assert schedule_rules._night_offset(30, 240) == 1440           # 12:30am–4am porter: after the night's midnight
    assert schedule_rules._night_offset(270, 750) == 0             # 4:30am–12:30pm baker: the morning's
    assert schedule_rules._night_offset(120, None) == 1440         # 2am, no end
    assert schedule_rules._night_offset(23 * 60, 120) == 0         # an 11pm–2am close
