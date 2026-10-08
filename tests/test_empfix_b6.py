"""Employee audit fixes B6 (10/1/26): earnings, stats, recognition, the
post-shift pulse, the calendar feed, schedule links and the web staff pages.

What these protect:
- H11 (MISS-7, INV-07): /staff/api/earnings is the caller's OWN punches only
  — by POS id, never another person's tips — with `as_of` and the lag said;
  no POS is an empty answer with a reason, never zeros; two POS people with
  one name are never guessed.
- V2 (INV-08): /staff/api/stats — scheduled and worked hours this payroll
  week, an overtime heads-up in hours (never money), own attendance with
  "not tracked" said honestly, certifications with no invented expiry, and
  never a reliability score.
- V5 (MISS-12): only owner-CONFIRMED POSITIVE mentions of the caller, a safe
  excerpt (no guest identity, nothing about a colleague), and the confirm
  tells the person.
- V6 (MISS-13): one pulse per own shift; the owner's read is aggregate only
  and silent below the small-n floor; the close-out gets it as context.
- M11 (MISS-9): a stable per-person .ics link stored only as its hash,
  serving only the holder's published shifts, dead on deactivation.
- M9 (SEC-09, LG-18, UX-25/32/39/40): /s/ tokens stored hashed (old rows
  migrated), NULL expiries backfilled, a superseded week followed to the
  live one, links dead on any deactivation path, branded invalid/throttled
  pages, guests' own expired page, the app named on the page.
- C1 (INV-01, UX-08/26/33/38): the web staff page always says how to get the
  app and how a non-iPhone employee gets their week; the ready page shows
  the restaurant code; one name for the code; the logout page; a11y labels.

Clocks are pinned: staff_insights.local_now / business_today are patched to
Thursday 10/1/26 8pm at the restaurant.
"""
import json
import os
import re
from datetime import date, datetime

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
import people
import staff_insights
import staff_settings
import strategy_routes
from auth import create_staff_session, create_user, set_membership_pin, upsert_membership
from models import Restaurant, create_restaurant
from staff_routes import staff_bp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 10, 1)                      # a Thursday
NOW = datetime(2026, 10, 1, 20, 0)

WEEK_CSV = """date,day,employee,role,shift_start,shift_end,scheduled_hours,notes
2026-09-28,Monday,Sofia R.,Server,16:00,22:00,6.0,
2026-09-29,Tuesday,Sofia R.,Server,16:00,23:00,7.0,
2026-09-30,Wednesday,Sofia R.,Server,16:00,23:00,7.0,
2026-10-01,Thursday,Sofia R.,Server,16:00,22:00,6.0,
2026-10-02,Friday,Sofia R.,Bartender,15:00,23:00,8.0,
2026-09-28,Monday,Marcus T.,Cook,08:00,16:00,8.0,
"""


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _env(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, client_api, mobile_api, staff_settings):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    monkeypatch.setattr(staff_insights, "local_now", lambda rid, db_path=None: NOW)
    monkeypatch.setattr(staff_insights, "business_today", lambda rid, now_local=None, db_path=None: TODAY)
    monkeypatch.setattr(staff_insights, "_provider", lambda rid, db_path=None: "rpower")
    monkeypatch.delenv("IOS_APP_STORE_URL", raising=False)
    yield


def _app():
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(staff_bp)
    app.register_blueprint(client_api.client_bp)
    return app


def _restaurant(db_path, name="Maple & Rye"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test", module_labor=1), db_path=db_path)


def _staff(db_path, rid, name="Sofia R.", username="sofia"):
    uid = create_user(rid, username, f"{username}@x.test", "unused", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    set_membership_pin(m["id"], rid, "8317", db_path=db_path)
    return uid, m["id"]


def _staff_client(db_path, rid, uid):
    c = _app().test_client()
    c.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    return c


def _week(db_path, rid, csv=WEEK_CSV, published=True, week_start="2026-09-28", week_end="2026-10-04"):
    conn = models.get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
        "labor_target, schedule_csv, summary_json, published_at) VALUES (?,?,?,40,40,30,?,'[]',?)",
        (rid, week_start, week_end, csv, "2026-09-26 15:00:00" if published else None))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def _punch(db_path, rid, pid, day, emp_id, name, reg=6.0, ot=0.0, tips=0.0, net=0.0, grats=0.0,
           station=0, cin="16:00", cout="22:00"):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date, employee_id, "
                 "employee_name, role, clock_in, clock_out, reg_hours, ot_hours, tips, tips_net, grats, is_station, "
                 "pay, reg_rate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, "rpower", pid, day, emp_id, name, "Server", f"{day}T{cin}:00",
                  f"{day}T{cout}:00" if cout else None, reg, ot, tips, net, grats, station, 777.77, 13.13))
    conn.execute("INSERT OR IGNORE INTO pos_archive_days (restaurant_id, provider, business_date) VALUES (?,?,?)",
                 (rid, "rpower", day))
    conn.commit()
    conn.close()


def _with_punches(db_path, rid):
    people.link_pos_id(rid, "Sofia R.", "E1", source="rpower", db_path=db_path)
    people.link_pos_id(rid, "Marcus T.", "E2", source="rpower", db_path=db_path)
    _punch(db_path, rid, "p1", "2026-09-28", "E1", "Sofia R.", reg=6.0, tips=120.5, net=110.25, grats=15.0)
    _punch(db_path, rid, "p2", "2026-09-29", "E1", "Sofia R.", reg=7.0, ot=0.5, tips=88.0, net=80.0)
    _punch(db_path, rid, "p3", "2026-09-28", "E2", "Marcus T.", reg=8.0, tips=999.99, net=999.99)
    _punch(db_path, rid, "p4", "2026-09-29", "E1", "Sofia R.", reg=3.0, tips=5555.0, station=1)


# ── H11: my hours and tips ──────────────────────────────────────────────────

def test_earnings_are_only_my_own_punches_with_the_lag_said(db_path):
    rid = _restaurant(db_path)
    uid, _mid = _staff(db_path, rid)
    _staff(db_path, rid, "Marcus T.", "marcus")
    _with_punches(db_path, rid)
    r = _staff_client(db_path, rid, uid).get("/staff/api/earnings?days=14")
    d = r.get_json()
    assert r.status_code == 200 and d["ok"] and d["available"]
    assert [s["business_date"] for s in d["shifts"]] == ["2026-09-29", "2026-09-28"]
    first = d["shifts"][1]
    assert (first["tips_total"], first["tip_net"], first["grats"], first["hours"]) == (120.5, 110.25, 15.0, 6.0)
    assert first["clock_in_label"] == "4pm" and first["date_label"] == "9/28/26"
    assert d["shifts"][0]["overtime_hours"] == 0.5
    assert d["as_of"] == "2026-09-29" and d["as_of_label"] == "9/29/26" and "next morning" in d["lag_note"]
    body = r.get_data(as_text=True)
    # Never anyone else's figures, never a station login's, never pay.
    assert "999.99" not in body and "Marcus" not in body and "5555" not in body
    assert "777.77" not in body and "13.13" not in body and '"pay"' not in body


def test_earnings_by_name_never_guess_between_two_pos_people(db_path):
    rid = _restaurant(db_path)
    _punch(db_path, rid, "p1", "2026-09-28", "E1", "Sofia R.", tips=10)
    _punch(db_path, rid, "p2", "2026-09-29", "E9", "Sofia R.", tips=20)
    d = staff_insights.my_earnings(rid, "Sofia R.", db_path=db_path)
    assert d["available"] is False and d["reason"] == "ambiguous" and d["shifts"] == []


def test_earnings_by_name_when_one_unclaimed_pos_id_carries_it(db_path):
    rid = _restaurant(db_path)
    _punch(db_path, rid, "p1", "2026-09-28", "E1", "Sofia R.", tips=10)
    d = staff_insights.my_earnings(rid, "sofia r.", db_path=db_path)
    assert d["available"] and [s["tips_total"] for s in d["shifts"]] == [10.0]


def test_earnings_without_a_pos_say_why_instead_of_zeros(db_path, monkeypatch):
    monkeypatch.setattr(staff_insights, "_provider", lambda rid, db_path=None: None)
    rid = _restaurant(db_path)
    d = staff_insights.my_earnings(rid, "Sofia R.", db_path=db_path)
    assert d["available"] is False and d["reason"] == "pos_not_connected"
    assert d["shifts"] == [] and d["as_of"] is None and "isn't connected" in d["message"]


# ── V2: personal stats ──────────────────────────────────────────────────────

def test_stats_hours_and_overtime_in_hours_never_money(db_path):
    rid = _restaurant(db_path)
    uid, _ = _staff(db_path, rid)
    _week(db_path, rid)
    _with_punches(db_path, rid)
    r = _staff_client(db_path, rid, uid).get("/staff/api/stats")
    d = r.get_json()
    assert d["week"]["start"] == "2026-09-28" and d["week"]["label"] == "9/28/26 – 10/4/26"
    assert d["scheduled"] == {"hours": 34.0, "shifts": 5}
    assert d["actual"]["hours"] == 13.5 and d["actual"]["as_of"] == "2026-09-29"
    # 13.5 worked through 9/29 + 21 scheduled after it = 34.5 → 5.5h of room.
    ot = d["overtime"]
    assert ot["projected_hours"] == 34.5 and ot["headroom_hours"] == 5.5
    assert ot["message"] == "A pickup longer than 5.5h takes you past 40 hours this week."
    body = r.get_data(as_text=True)
    assert "$" not in body and "reliability" not in body.lower() and "Marcus" not in body


def test_a_pickup_says_when_it_crosses_forty(db_path):
    rid = _restaurant(db_path)
    _week(db_path, rid)
    assert staff_insights.pickup_overtime_note(rid, "Sofia R.", "2026-10-03", 4, db_path=db_path) is None
    assert staff_insights.pickup_overtime_note(rid, "Sofia R.", "2026-10-03", 7, db_path=db_path) == \
        "This 7h pickup takes you past 40 hours that week (to 41h)."


def test_attendance_not_tracked_is_said_not_a_clean_record(db_path):
    rid = _restaurant(db_path)
    a = staff_insights.my_attendance(rid, "Sofia R.", db_path=db_path)
    assert a["tracked"] is False and a["recent"] == [] and "isn't tracked" in a["message"]


def test_attendance_is_my_own_outcomes_only(db_path):
    import attendance
    rid = _restaurant(db_path)
    attendance.record(rid, "Sofia R.", "2026-09-29", "late", "coverage_check", shift_start="16:00",
                      minutes_late=12, covered_by="Dana K.", note="talked to her", db_path=db_path)
    attendance.record(rid, "Marcus T.", "2026-09-29", "no_show", "coverage_check", shift_start="08:00",
                      db_path=db_path)
    a = staff_insights.my_attendance(rid, "Sofia R.", db_path=db_path)
    assert a["tracked"] and a["counts"] == {"late": 1}
    assert a["recent"] == [{"date": "2026-09-29", "date_label": "9/29/26", "outcome": "late", "label": "Late",
                            "minutes_late": 12}]
    blob = json.dumps(a)
    assert "Marcus" not in blob and "Dana" not in blob and "talked" not in blob


def test_certifications_never_invent_an_expiry(db_path):
    rid = _restaurant(db_path)
    staff_settings.upsert(rid, "Sofia R.", certifications=["alcohol", "food_handler"], db_path=db_path)
    c = staff_insights.my_certifications(rid, "Sofia R.", db_path=db_path)
    assert [i["label"] for i in c["items"]] == ["Alcohol service", "Food handler"]
    assert all(i["expires_on"] is None for i in c["items"]) and c["expiry_tracked"] is False


# ── V5: a guest named you ───────────────────────────────────────────────────

def _review(db_path, rid, text, rating=5, author="Jane Guestperson"):
    conn = models.get_conn(db_path)
    cur = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                       "fetched_at, processed, sentiment) VALUES (?,?,?,?,?,?,?,?,1,'positive')",
                       (rid, "google", f"x{text[:8]}{rating}", author, rating, text, "2026-09-20", "2026-09-20"))
    conn.commit()
    rv = cur.lastrowid
    conn.close()
    return rv


def _mention(db_path, rid, name, review_id, status="confirmed", polarity=1):
    people.record_signal(rid, name, "review_mention", "2026-09-20", ref=f"review:{review_id}", polarity=polarity,
                         status=status, detail="snippet", db_path=db_path)
    conn = models.get_conn(db_path)
    sid = conn.execute("SELECT id FROM person_signals WHERE ref=? AND employee_key=?",
                       (f"review:{review_id}", staff_settings.name_key(name))).fetchone()["id"]
    conn.close()
    return sid


def test_recognition_is_only_confirmed_positive_mentions_of_me(db_path):
    rid = _restaurant(db_path)
    uid, _ = _staff(db_path, rid)
    staff_settings.upsert(rid, "Sofia R.", db_path=db_path)
    staff_settings.upsert(rid, "Marcus T.", db_path=db_path)
    good = _review(db_path, rid, "Lovely night. Sofia was wonderful, call me at 312-555-0199 or jane@x.test! Great.")
    _mention(db_path, rid, "Sofia R.", good)
    _mention(db_path, rid, "Sofia R.", _review(db_path, rid, "Sofia was fine I guess.", rating=4), status="proposed")
    _mention(db_path, rid, "Sofia R.", _review(db_path, rid, "Sofia was rude.", rating=1), polarity=-1)
    _mention(db_path, rid, "Sofia R.", _review(db_path, rid, "Sofia and Marcus were both great.", rating=5))
    _mention(db_path, rid, "Marcus T.", _review(db_path, rid, "Marcus cooked a perfect steak.", rating=5))
    r = _staff_client(db_path, rid, uid).get("/staff/api/recognition")
    d = r.get_json()
    assert d["count"] == 2
    excerpts = [i["excerpt"] for i in d["items"]]
    # The colleague's sentence is not hers to read; the guest is never named.
    assert None in excerpts and "Sofia was wonderful, call me at or" in excerpts
    body = r.get_data(as_text=True)
    assert "Jane" not in body and "jane@" not in body and "312" not in body and "steak" not in body


def test_safe_excerpt_cuts_contact_details_and_long_text():
    out = staff_insights.safe_excerpt("Ask for Sofia at www.x.com or @sofia_fan " + "really " * 40, "Sofia R.")
    assert "www" not in out and "@" not in out and len(out) <= staff_insights.EXCERPT_MAX + 1
    assert staff_insights.safe_excerpt("Great service.", "Sofia R.") is None


def test_confirming_a_mention_tells_the_person(db_path, monkeypatch):
    rid = _restaurant(db_path)
    rv = _review(db_path, rid, "Sofia made our anniversary.")
    sid = _mention(db_path, rid, "Sofia R.", rv, status="proposed")
    told = []
    monkeypatch.setattr(people, "tell", lambda r, name, title, lines, **kw: told.append((name, title, lines)) or "push")
    user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "owner",
            "is_admin": 0, "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    r = app.test_client().post(f"/api/people/mentions/{sid}", json={"confirm": True},
                               headers={"X-CSRF": "t"}, environ_base={"HTTP_COOKIE": "csrf_js=t"})
    assert r.status_code == 200 and r.get_json()["told"] == "push"
    assert told == [("Sofia R.", "A guest named you",
                     ["A guest named you in a review on 9/20/26.", "“Sofia made our anniversary.”"])]


def test_a_negative_mention_is_never_sent(db_path, monkeypatch):
    rid = _restaurant(db_path)
    sid = _mention(db_path, rid, "Sofia R.", _review(db_path, rid, "Sofia was slow.", rating=2), polarity=-1)
    monkeypatch.setattr(people, "tell", lambda *a, **k: pytest.fail("told about a negative mention"))
    assert staff_insights.tell_recognition(rid, sid, db_path=db_path) is None


# ── V6: the post-shift pulse ────────────────────────────────────────────────

def test_pulse_once_per_own_published_shift(db_path):
    rid = _restaurant(db_path)
    uid, _ = _staff(db_path, rid)
    _week(db_path, rid)
    c = _staff_client(db_path, rid, uid)
    assert c.get("/staff/api/pulse").get_json()["due"]["date"] == "2026-10-01"
    ok = c.post("/staff/api/pulse", json={"date": "2026-09-30", "rating": 4, "note": "  Grill was down  "})
    assert ok.status_code == 200 and ok.get_json()["pulse"]["note"] == "Grill was down"
    assert c.post("/staff/api/pulse", json={"date": "2026-09-30", "rating": 5}).status_code == 409   # once
    assert c.post("/staff/api/pulse", json={"date": "2026-10-02", "rating": 5}).status_code == 409   # future
    assert c.post("/staff/api/pulse", json={"date": "2026-10-03", "rating": 5}).status_code == 409   # not on
    assert c.post("/staff/api/pulse", json={"date": "2026-09-26", "rating": 5}).status_code == 409   # too old
    assert c.post("/staff/api/pulse", json={"date": "2026-09-29", "rating": 6}).status_code == 400
    assert c.post("/staff/api/pulse", json={"date": "2026-09-29", "rating": True}).status_code == 400
    assert c.post("/staff/api/pulse", json=[1]).status_code == 400


def test_pulse_waits_for_the_shift_to_start(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _, mid = _staff(db_path, rid)
    _week(db_path, rid)
    early = datetime(2026, 10, 1, 12, 0)
    with pytest.raises(staff_insights.PulseError):
        staff_insights.record_pulse(rid, mid, "Sofia R.", "2026-10-01", 4, now_local=early, db_path=db_path)
    assert staff_insights.record_pulse(rid, mid, "Sofia R.", "2026-10-01", 4, now_local=NOW,
                                       db_path=db_path)["rating"] == 4


def _pulses(db_path, rid, rows):
    conn = models.get_conn(db_path)
    for i, (day, rating, note) in enumerate(rows):
        conn.execute("INSERT INTO staff_shift_pulse (restaurant_id, membership_id, business_date, rating, note) "
                     "VALUES (?,?,?,?,?)", (rid, 100 + i, day, rating, note))
    conn.commit()
    conn.close()


def test_the_owner_read_is_silent_below_the_floor(db_path):
    rid = _restaurant(db_path)
    _pulses(db_path, rid, [("2026-09-30", 2, "Short on the line"), ("2026-09-30", 5, None)])
    s = staff_insights.pulse_summary(rid, today=TODAY, db_path=db_path)
    assert s["enough"] is False and s["responses"] == 2
    assert s["average"] is None and s["notes"] == [] and s["by_day"] == []
    assert staff_insights.pulse_for_closeout(rid, "2026-09-30", db_path=db_path) is None


def test_the_owner_read_never_says_who(db_path):
    rid = _restaurant(db_path)
    _pulses(db_path, rid, [("2026-09-30", 2, "Short on the line"), ("2026-09-30", 4, None),
                           ("2026-09-30", 3, None), ("2026-09-29", 5, "Fun night")])
    s = staff_insights.pulse_summary(rid, today=TODAY, db_path=db_path)
    assert s["enough"] and s["average"] == 3.5 and s["distribution"]["5"] == 1
    assert s["by_day"] == [{"date": "2026-09-30", "date_label": "9/30/26", "responses": 3, "average": 3.0}]
    assert s["other_days_responses"] == 1
    # A note keeps its date only when its day cleared the floor.
    assert {"text": "Fun night", "date_label": None} in s["notes"]
    assert {"text": "Short on the line", "date_label": "9/30/26"} in s["notes"]
    assert "membership" not in json.dumps(s) and "10" not in json.dumps([n for n in s["notes"]])
    assert staff_insights.pulse_for_closeout(rid, "2026-09-30", db_path=db_path)["line"] == \
        "Staff rated tonight 3 out of 5 (3 answers)."


def test_the_owner_pulse_route_needs_labor(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _pulses(db_path, rid, [("2026-09-30", 4, None)] * 3)

    def client(role):
        user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "username": role, "role": role,
                "is_admin": 0, "email": "o@x.test"}
        monkeypatch.setattr(auth, "get_current_user", lambda: user)
        monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
        monkeypatch.setattr(strategy_routes, "_local_today", lambda u: TODAY)
        app = Flask(__name__)
        app.register_blueprint(strategy_routes.strategy_bp)
        app.register_blueprint(strategy_routes.strategy_mobile_bp)
        return app.test_client()
    owner = client("owner")
    for path, headers in (("/api/labor/staff-pulse", {}), ("/mobile/api/labor/staff-pulse",
                                                           {"Authorization": "Bearer t"})):
        d = owner.get(path, headers=headers).get_json()
        assert d["ok"] and d["enough"] and d["average"] == 4.0, path


# ── M11: the calendar feed ──────────────────────────────────────────────────

def test_the_calendar_link_is_stable_hashed_and_shows_only_my_shifts(db_path):
    rid = _restaurant(db_path)
    uid, mid = _staff(db_path, rid)
    _week(db_path, rid)
    _week(db_path, rid, csv=WEEK_CSV.replace("Sofia R.,Server,16:00,22:00", "Sofia R.,Server,09:00,11:00"),
          published=False, week_start="2026-10-05", week_end="2026-10-11")    # a draft never shows
    c = _staff_client(db_path, rid, uid)
    a = c.get("/staff/api/calendar-link").get_json()
    b = c.get("/staff/api/calendar-link").get_json()
    assert a["url"] == b["url"] and a["url"].endswith(".ics") and a["webcal_url"].startswith("webcal://")
    token = a["url"].rsplit("/", 1)[1][:-4]
    conn = models.get_conn(db_path)
    stored = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM staff_calendar_links")]
    conn.close()
    assert stored == [staff_insights._cal_hash(token)] and token not in stored[0]
    feed = _app().test_client().get(f"/staff/cal/{token}.ics")
    assert feed.status_code == 200 and feed.headers["Content-Type"].startswith("text/calendar")
    ics = feed.get_data(as_text=True)
    assert ics.count("BEGIN:VEVENT") == 5 and "Marcus" not in ics and "Cook" not in ics
    # 4pm Chicago on 9/28 is 21:00 UTC; Friday's bartender shift ends 11pm.
    assert "DTSTART:20260928T210000Z" in ics and "SUMMARY:Bartender at Maple & Rye" in ics
    assert "T140000Z" not in ics                                           # the draft's 9am
    assert "\r\n" in ics and all(len(x.encode()) <= 75 for x in ics.split("\r\n"))


def test_the_calendar_feed_dies_on_deactivation_and_reset(db_path):
    rid = _restaurant(db_path)
    uid, mid = _staff(db_path, rid)
    _week(db_path, rid)
    c = _staff_client(db_path, rid, uid)
    old = c.get("/staff/api/calendar-link").get_json()["url"].rsplit("/", 1)[1][:-4]
    new = c.post("/staff/api/calendar-link/reset").get_json()["url"].rsplit("/", 1)[1][:-4]
    feeds = _app().test_client()
    assert new != old and feeds.get(f"/staff/cal/{old}.ics").status_code == 410
    assert feeds.get(f"/staff/cal/{new}.ics").status_code == 200
    assert feeds.get("/staff/cal/not-a-real-token.ics").status_code == 404
    auth.set_membership_active(mid, rid, False, db_path=db_path)        # any deactivation path
    assert feeds.get(f"/staff/cal/{new}.ics").status_code == 410


def test_ics_text_is_escaped_and_folded():
    from zoneinfo import ZoneInfo
    ics = staff_insights.build_ics(1, 2, "Bar; Grill, & Co", ZoneInfo("America/Chicago"),
                                   [{"date": "2026-10-03", "start": "9:00pm", "end": "2:00am", "role": "Server",
                                     "notes": "Close; count the drawer, " + "x" * 120}],
                                   now_utc=datetime(2026, 10, 1, 12, 0))
    assert "Bar\\; Grill\\, & Co" in ics
    assert "DTSTART:20261004T020000Z" in ics and "DTEND:20261004T070000Z" in ics     # past midnight
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


# ── M9: schedule links ──────────────────────────────────────────────────────

def test_share_tokens_are_stored_as_hashes_and_old_rows_migrate(db_path):
    rid = _restaurant(db_path)
    sid = _week(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    conn = models.get_conn(db_path)
    stored = conn.execute("SELECT token FROM schedule_shares").fetchone()["token"]
    assert stored.startswith("sha256:") and token not in stored
    conn.execute("INSERT INTO schedule_shares (restaurant_id, schedule_id, employee_name, token, sent_at) "
                 "VALUES (?,?,?,?,datetime('now','-70 days'))", (rid, sid, "Sofia R.", "plain-old-token"))
    conn.execute("INSERT INTO schedule_shares (restaurant_id, schedule_id, employee_name, token) "
                 "VALUES (?,?,?,?)", (rid, sid, "Sofia R.", "plain-new-token"))
    conn.commit()
    conn.close()
    assert models.migrate_schedule_shares(db_path) == {"hashed": 2, "expiry_set": 2}
    assert models.migrate_schedule_shares(db_path) == {"hashed": 0, "expiry_set": 0}      # once
    conn = models.get_conn(db_path)
    left = [r["token"] for r in conn.execute("SELECT token FROM schedule_shares")]
    conn.close()
    assert all(t.startswith("sha256:") for t in left)
    assert models.get_schedule_share("plain-new-token", db_path=db_path)["expired"] is False   # still works
    assert models.get_schedule_share("plain-old-token", db_path=db_path)["expired"] is True    # aged out


def test_a_link_to_a_replaced_week_shows_the_live_copy(db_path):
    rid = _restaurant(db_path)
    v1 = _week(db_path, rid)
    token = models.create_schedule_share(rid, v1, "Sofia R.", db_path=db_path)
    v2 = _week(db_path, rid, csv=WEEK_CSV.replace("2026-09-28,Monday,Sofia R.,Server,16:00,22:00",
                                                  "2026-09-28,Monday,Sofia R.,Server,11:00,15:00"))
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET superseded_by=?, published_at='2026-09-26 15:00:00' WHERE id=?",
                 (v2, v1))
    conn.execute("UPDATE schedule_history SET published_at='2026-09-30 10:00:00' WHERE id=?", (v2,))
    conn.commit()
    conn.close()
    page = _app().test_client().get(f"/s/{token}").get_data(as_text=True)
    assert "11:00 – 15:00" in page and page.count("16:00 – 22:00") == 1     # Monday moved; Thursday stays
    assert "schedule changed" in page and "9/30/26" in page


def test_a_link_dies_on_any_deactivation_path(db_path):
    rid = _restaurant(db_path)
    sid = _week(db_path, rid)
    _uid, mid = _staff(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    c = _app().test_client()
    assert c.get(f"/s/{token}").status_code == 200
    auth.set_membership_active(mid, rid, False, db_path=db_path)      # Account → Staff, not the roster
    r = c.get(f"/s/{token}")
    assert r.status_code == 410 and "16:00" not in r.get_data(as_text=True)
    # The end is durable (auth calls staff_insights.expire_links_for on the
    # deactivation — this file's handoff to B1): reactivating does not revive
    # the old link; the next send mints a new one.
    auth.set_membership_active(mid, rid, True, db_path=db_path)
    assert c.get(f"/s/{token}").status_code == 410
    fresh = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    assert c.get(f"/s/{fresh}").status_code == 200
    staff_settings.upsert(rid, "Sofia R.", active=False, db_path=db_path)  # the roster switch
    assert c.get(f"/s/{fresh}").status_code == 410


def test_expire_links_for_ends_schedule_and_calendar_links(db_path):
    rid = _restaurant(db_path)
    sid = _week(db_path, rid)
    _uid, mid = _staff(db_path, rid)
    token = models.create_schedule_share(rid, sid, "sofia r.", db_path=db_path)
    staff_insights.calendar_link(rid, mid, db_path=db_path)
    assert staff_insights.expire_links_for(rid, "Sofia R.", db_path=db_path) == \
        {"schedule_links": 1, "calendar_links": 1}
    assert models.get_schedule_share(token, db_path=db_path)["expired"] is True


def test_refusals_are_branded_pages_not_bare_text(db_path, monkeypatch):
    rid = _restaurant(db_path)
    sid = _week(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    c = _app().test_client()
    c.set_cookie("csrf_js", "t", domain="localhost")
    bad = c.post("/s/nope/availability", data={"csrf_token": "t", "unavailable": "Monday"})
    assert bad.status_code == 404 and "<html" in bad.get_data(as_text=True)
    assert "didn&rsquo;t work" in bad.get_data(as_text=True)
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: True)
    slow = c.post(f"/s/{token}/availability", data={"csrf_token": "t", "unavailable": "Monday"})
    assert slow.status_code == 302 and slow.headers["Location"].endswith("?error=throttled")
    assert "Too many updates" in c.get(f"/s/{token}?error=throttled").get_data(as_text=True)
    monkeypatch.setattr(auth, "portal_attempts_exceeded", lambda ip, **k: True)
    many = c.get("/s/whatever")
    assert many.status_code == 429 and "Too many tries" in many.get_data(as_text=True)


def test_guessing_links_is_throttled_per_address(db_path):
    rid = _restaurant(db_path)
    c = _app().test_client()
    codes = [c.get(f"/s/guess-{i}").status_code for i in range(auth.PORTAL_MAX_ATTEMPTS + 1)]
    assert codes[0] == 404 and codes[-1] == 429


def test_guests_get_their_own_expired_page(db_path):
    r = _app().test_client().get("/g/not-a-link")
    body = r.get_data(as_text=True)
    assert r.status_code == 404 and "This link has expired" in body
    assert "manager" not in body and "schedule" not in body.lower()


def test_the_link_page_points_to_the_app_with_big_chips(db_path, monkeypatch):
    rid = _restaurant(db_path)
    sid = _week(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    page = _app().test_client().get(f"/s/{token}").get_data(as_text=True)
    assert "Swaps, time off and today&rsquo;s tasks are in the Cavnar AI app" in page
    assert "ask your manager for the invite" in page
    monkeypatch.setenv("IOS_APP_STORE_URL", "https://apps.apple.com/app/id1")
    page = _app().test_client().get(f"/s/{token}").get_data(as_text=True)
    assert 'href="https://apps.apple.com/app/id1"' in page
    tpl = _read("templates", "staff_schedule.html")
    assert "min-height:44px" in tpl and "cavnar-fonts.css" in tpl
    for name in ("staff_schedule.html", "staff_schedule_expired.html", "staff_schedule_invalid.html",
                 "guest_link_expired.html"):
        css = re.sub(r":root\{[^}]*\}|/\*.*?\*/", "", _read("templates", name).split("</style>")[0], flags=re.S)
        assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css), name            # tokens, not hexes
    expired = _read("templates", "staff_schedule_expired.html")
    assert "always in the Cavnar AI app" in expired


# ── C1: the web staff page ──────────────────────────────────────────────────

def test_the_web_page_always_says_how_to_get_the_app(db_path, monkeypatch):
    c = _app().test_client()
    page = c.get("/staff/").get_data(as_text=True)
    assert "TestFlight" in page and "Cavnar AI app" in page and "Ask your manager for the invite" in page
    assert "No iPhone?" in page and "opens in any browser" in page
    monkeypatch.setenv("IOS_APP_STORE_URL", "https://apps.apple.com/app/id1")
    page = c.get("/staff/").get_data(as_text=True)
    assert 'href="https://apps.apple.com/app/id1"' in page and "No iPhone?" in page


def test_the_ready_page_shows_the_restaurant_code(db_path):
    rid = _restaurant(db_path)
    uid, _ = _staff(db_path, rid)
    code = auth.get_join_code(rid, db_path=db_path)
    page = _staff_client(db_path, rid, uid).get("/staff/home?ready=1").get_data(as_text=True)
    assert "You&rsquo;re all set" in page and "Your restaurant code" in page and code in page
    assert "The app asks for it once." in page
    assert "window.location = '/staff/home?ready=1'" in _read("templates", "staff_login.html")


def test_the_logout_page_offers_to_stay_in_the_portal(db_path):
    """The browser portal is back (owner, 10/7/26): "Stay signed in" goes
    to it, and the sign-out POST carries the double-submit token."""
    rid = _restaurant(db_path)
    uid, _ = _staff(db_path, rid)
    body = _staff_client(db_path, rid, uid).get("/staff/logout").get_data(as_text=True)
    assert "Back to my shifts" not in body and "href='/staff/home'" in body
    assert "Stay signed in" in body and "cbtn" in body and "#D4583A" not in body
    assert "name='csrf_token' value='" in body and "__CSRF__" not in body


def test_one_name_for_the_code_and_no_stale_copy():
    page = _read("templates", "staff_login.html")
    assert "restaurant&rsquo;s code" not in page and "restaurant's code" not in page
    assert "Enter your restaurant’s code" not in page and "Your restaurant code" in page
    assert "tablet" not in page and "staff sign in" not in page


def test_the_signup_page_is_labelled_for_screen_readers():
    page = _read("templates", "staff_login.html")
    assert 'onclick="signupBack()" aria-label="Back"' in page and "min-height:44px" in page
    assert 'id="su-err" role="status" aria-live="polite"' in page
    assert 'aria-label="Delete last digit"' in page and 'aria-label="Clear PIN"' in page
    assert 'id="su-dots" role="img" aria-label="PIN: no digits entered"' in page
    assert "box.setAttribute('aria-label'" in page


def test_the_link_uses_the_one_versioned_save_when_the_build_has_it(db_path, monkeypatch):
    """With staff_settings.save_own_availability (employee audit B8) the link
    sends back the version it showed, and a save over a newer change comes
    back to the form with the latest and a line saying so."""
    rid = _restaurant(db_path)
    sid = _week(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    calls = []
    monkeypatch.setattr(staff_settings, "KEEP", object(), raising=False)
    monkeypatch.setattr(staff_settings, "own_availability",
                        lambda rid_, name, **k: {"unavailable_days": ["Tuesday"], "notes": "class",
                                                 "updated_at": "2026-09-30 10:00:00"}, raising=False)

    def save(rid_, name, expected_updated_at=None, unavailable_days=None, notes=None, source=None, **k):
        calls.append((name, expected_updated_at, unavailable_days, notes, source))
        return {"ok": False, "status": 409, "stale": True} if len(calls) == 1 else {"ok": True, "status": 200}
    monkeypatch.setattr(staff_settings, "save_own_availability", save, raising=False)
    c = _app().test_client()
    page = c.get(f"/s/{token}").get_data(as_text=True)
    assert 'name="updated_at" value="2026-09-30 10:00:00"' in page and 'value="Tuesday" checked' in page
    c.set_cookie("csrf_js", "t", domain="localhost")
    form = {"csrf_token": "t", "unavailable": "Monday", "note": "", "note_prefilled": "1",
            "updated_at": "2026-09-29 09:00:00"}
    first = c.post(f"/s/{token}/availability", data=form)
    assert first.headers["Location"].endswith("?error=stale")
    assert "Someone changed this since you opened it" in c.get(f"/s/{token}?error=stale").get_data(as_text=True)
    assert c.post(f"/s/{token}/availability", data=form).headers["Location"].endswith("?saved=1")
    assert calls[0] == ("Sofia R.", "2026-09-29 09:00:00", ["Monday"], "", "link")
