"""Edge cases for Employees & Team: roster, ratings, settings, time off,
contacts, checklists and the staff portal.

Every test comes from the MOD edge-case audit (appendix A4, findings
MOD-EMP-1..10, plus MOD-LAB-5/-21 where they decide who is on the roster).
The recurring theme: staff tables are keyed by a free-text name, so one
person spelled two ways must still be one person, a person who does not
exist must never appear, and switching someone off in one place must switch
them off everywhere.

xfail(strict=True) tests assert the CORRECT behaviour for a defect the audit
confirmed; they flip to a failure when the defect is fixed.
"""
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
import schedule_engine
import shift_requests
import staff_settings as ss
import strategy_routes
import time_off
import time_utils
from auth import create_staff_session, create_user, init_auth, set_membership_pin, upsert_membership
from models import Restaurant, create_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import importlib
    for name in ("models", "auth", "client_api", "mobile_api", "staff_settings", "time_off",
                 "shift_requests", "staff_schedule", "schedule_engine", "schedule_rules",
                 "schedule_intel", "schedule_versions", "strategy_routes", "staff_roster",
                 "ask_cavnar_tools", "labor", "staff_routes", "permissions"):
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)


@pytest.fixture
def rid(db_path):
    return create_restaurant(Restaurant(name="Team Co", owner_email="t@x.test", module_labor=1,
                                        timezone="America/Chicago"), db_path=db_path)


def _shifts(monkeypatch, rows):
    monkeypatch.setattr(models, "_cached_shifts", lambda r: rows)


def _owner_u(rid):
    return {"id": 1, "restaurant_id": rid, "is_admin": 1, "username": "owner", "role": "owner"}


def _call(body_fn, u, json=None, *args):
    app = Flask(__name__)
    with app.test_request_context(json=json or {}):
        return body_fn(u, *args)


# ── Roster ─────────────────────────────────────────────────────────────────

def test_a_brand_new_restaurant_has_an_empty_roster(rid, db_path):
    """A4 #2 / MOD-EMP-1 — no shifts uploaded, no hand-added names: nobody.
    Deliberately no monkeypatch of _cached_shifts — that is what hid this."""
    assert ss.roster(rid, db_path=db_path) == []


def test_a_brand_new_restaurant_has_no_reliability_figures(rid, db_path):
    """A4 #2 / MOD-EMP-1."""
    assert ss.reliability(rid, db_path=db_path, min_shifts=1) == {}


def test_a_brand_new_restaurant_has_no_tenure(rid, db_path):
    """A4 #2 / MOD-EMP-1."""
    assert models.get_employee_tenure(rid, db_path=db_path) == {}


def test_case_and_whitespace_variants_of_one_name_are_one_person(rid, db_path, monkeypatch):
    """A4 #4 / MOD-EMP-2."""
    _shifts(monkeypatch, [{"employee": "Maria G.", "role": "Server", "date": "2026-09-01"},
                          {"employee": "maria g.", "role": "Server", "date": "2026-09-02"},
                          {"employee": "MARIA G. ", "role": "Server", "date": "2026-09-03"}])
    assert len(ss.roster(rid, db_path=db_path)) == 1


def test_deactivating_one_spelling_hides_every_spelling(rid, db_path, monkeypatch):
    """A4 #5 / MOD-EMP-2."""
    _shifts(monkeypatch, [{"employee": "Maria G.", "role": "Server", "date": "2026-09-01"},
                          {"employee": "maria g.", "role": "Server", "date": "2026-09-02"},
                          {"employee": "MARIA G. ", "role": "Server", "date": "2026-09-03"},
                          {"employee": "Ana", "role": "Host", "date": "2026-09-03"}])
    ss.upsert(rid, "Maria G.", active=False, db_path=db_path)
    assert [e["name"] for e in ss.roster(rid, db_path=db_path)] == ["Ana"]


def test_a_hand_added_name_and_a_shift_name_differing_in_case_are_one_person(rid, db_path, monkeypatch):
    """A4 #10 / MOD-EMP-2 — add_manual_team_member promises the two collapse."""
    _shifts(monkeypatch, [{"employee": "Maria G.", "role": "Server", "date": "2026-09-01"}])
    models.add_manual_team_member(rid, "maria g.", role="Server", db_path=db_path)
    assert len(ss.roster(rid, db_path=db_path)) == 1


def test_two_different_people_with_the_same_pos_display_name_stay_two_people():
    """A4 #11 / MOD-LAB-5 — Maria Garcia and Maria Gomez are two employees
    with two Toast guids; the roster must not merge them."""
    import toast
    entries = [
        {"employee": {"guid": "g-1", "firstName": "Maria", "lastName": "Garcia"},
         "jobReference": {"name": "Server"}, "inDate": "2026-09-14T16:00:00.000+0000",
         "outDate": "2026-09-14T22:00:00.000+0000"},
        {"employee": {"guid": "g-2", "firstName": "Maria", "lastName": "Gomez"},
         "jobReference": {"name": "Server"}, "inDate": "2026-09-14T16:00:00.000+0000",
         "outDate": "2026-09-14T22:00:00.000+0000"},
    ]
    rows = toast.normalise_entries(entries, {})
    assert len({r["employee"] for r in rows}) == 2


def test_a_five_hundred_person_roster_is_built_quickly(rid, db_path, monkeypatch):
    """A4 #12 — 500 employees x 20 shifts: the roster and reliability stay
    well inside a request budget."""
    rows = []
    for i in range(500):
        for d in range(20):
            rows.append({"employee": f"Person {i}", "role": "Server", "date": f"2026-08-{d + 1:02d}",
                         "scheduled_hours": "6", "actual_hours": "6"})
    _shifts(monkeypatch, rows)
    started = _time.perf_counter()
    roster = ss.roster(rid, db_path=db_path)
    rel = ss.reliability(rid, db_path=db_path)
    elapsed = _time.perf_counter() - started
    assert len(roster) == 500 and len(rel) == 500
    assert elapsed < 2.0, f"roster + reliability took {elapsed:.2f}s"


# ── Ratings / settings / reliability ───────────────────────────────────────

def test_a_rating_applies_whatever_case_the_name_is_spelled_in(rid, db_path):
    """A4 #18 / MOD-EMP-2."""
    models.set_capability(rid, "Maria G.", "overall", score=4, db_path=db_path)
    cov = models.capability_coverage(rid, ["maria g."], db_path=db_path)
    assert cov["rated"] == 1


class _ConnProxy:
    """A sqlite connection that can run a hook just before one statement —
    enough to interleave two callers deterministically."""
    def __init__(self, conn, hook):
        self._conn, self._hook = conn, hook

    def execute(self, sql, *a, **k):
        self._hook(sql)
        return self._conn.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_two_managers_editing_different_fields_at_once_both_persist(rid, db_path, monkeypatch):
    """A4 #22 / MOD-EMP-9 — manager A sets max hours while manager B marks
    the same person a minor; B's write lands between A's read and A's write."""
    ss.upsert(rid, "Ana", employment_type="part", db_path=db_path)
    real = models.get_conn
    fired = {"done": False}

    def hook(sql):
        if not fired["done"] and sql.lstrip().startswith("INSERT INTO staff_settings"):
            fired["done"] = True
            monkeypatch.setattr(ss, "get_conn", lambda *a, **k: real(db_path))
            ss.upsert(rid, "Ana", is_minor=True, db_path=db_path)   # manager B
    monkeypatch.setattr(ss, "get_conn", lambda *a, **k: _ConnProxy(real(db_path), hook))
    ss.upsert(rid, "Ana", max_hours=20, db_path=db_path)            # manager A
    row = ss.get_all(rid, db_path=db_path)["Ana"]
    assert row["max_hours"] == 20
    assert row["is_minor"] is True


def test_an_open_clock_in_is_not_counted_as_a_no_show(rid, db_path, monkeypatch):
    """A4 #25 / MOD-LAB-21 — the nightly sync runs while a closer is still
    clocked in; that is not a no-show."""
    import toast
    open_entry = {"employee": {"firstName": "Dana", "lastName": "Kim"}, "jobReference": {"name": "Bartender"},
                  "inDate": "2026-09-14T23:00:00.000+0000", "outDate": None,
                  "scheduledInDate": "2026-09-14T23:00:00.000+0000",
                  "scheduledOutDate": "2026-09-15T07:00:00.000+0000"}
    rows = toast.normalise_entries([open_entry] * 6, {})
    assert rows, "the open entry should still normalise"
    _shifts(monkeypatch, rows)
    rel = ss.reliability(rid, db_path=db_path, min_shifts=1).get("Dana K.")
    assert rel is None or rel["no_show_rate"] == 0


# ── Time off ───────────────────────────────────────────────────────────────

def test_a_time_off_request_longer_than_a_month_is_refused(rid, db_path):
    """A4 #27."""
    today = date(2026, 9, 22)
    row, err = time_off.request_time_off(rid, "Ana", "2026-10-01", "2026-11-15", db_path=db_path, today=today)
    assert row is None and "31" in err


def test_a_time_off_request_more_than_six_months_out_is_refused(rid, db_path):
    """A4 #27."""
    today = date(2026, 9, 22)
    row, err = time_off.request_time_off(rid, "Ana", "2027-06-01", "2027-06-02", db_path=db_path, today=today)
    assert row is None and "too far ahead" in err


def test_a_31_day_request_180_days_out_is_still_accepted(rid, db_path):
    """A4 #27 — the boundaries themselves are allowed."""
    today = date(2026, 9, 22)
    start = today + timedelta(days=180)
    row, err = time_off.request_time_off(rid, "Ana", start.isoformat(), (start + timedelta(days=30)).isoformat(),
                                         db_path=db_path, today=today)
    assert err is None and row["status"] == "pending"


def _frozen_utc(monkeypatch, module, utc_dt):
    """Freeze module.datetime.now() at one UTC instant (converted to
    whatever tz is asked for)."""
    real = datetime

    class _Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return utc_dt.astimezone(tz) if tz else utc_dt.astimezone().replace(tzinfo=None)
    monkeypatch.setattr(module, "datetime", _Frozen)


@pytest.fixture
def staff_app():
    from staff_routes import staff_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(staff_bp)
    return app


def _staff(db_path, rid, name="Jordan P.", username="jordan"):
    uid = create_user(rid, username, f"{username}@x.test", "unused", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    set_membership_pin(m["id"], rid, "8317", db_path=db_path)
    return uid, m["id"]


def test_a_date_that_has_passed_is_judged_on_the_restaurants_own_calendar(db_path, staff_app, monkeypatch):
    """A4 #28 — 10:30pm on 9/21 in Los Angeles is already 9/22 in UTC; a
    request for 'today' typed then is for today, not yesterday."""
    rid = create_restaurant(Restaurant(name="LA Co", owner_email="la@x.test",
                                       timezone="America/Los_Angeles"), db_path=db_path)
    uid, _ = _staff(db_path, rid)
    _frozen_utc(monkeypatch, time_utils, datetime(2026, 9, 22, 5, 30, tzinfo=timezone.utc))
    client = staff_app.test_client()
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    ok = client.post("/staff/api/time-off", json={"start_date": "2026-09-21", "end_date": "2026-09-21"})
    assert ok.status_code == 200, ok.get_json()
    late = client.post("/staff/api/time-off", json={"start_date": "2026-09-20", "end_date": "2026-09-20"})
    assert late.status_code == 400 and "passed" in late.get_json()["error"]


def test_an_employee_can_withdraw_a_pending_request_and_file_a_corrected_one(db_path, rid, staff_app):
    """A4 #29 / MOD-EMP-7."""
    uid, _ = _staff(db_path, rid)
    client = staff_app.test_client()
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    start = (date.today() + timedelta(days=10)).isoformat()
    first = client.post("/staff/api/time-off", json={"start_date": start, "end_date": start}).get_json()
    assert first["ok"] is True
    rid_req = first["request"]["id"]
    wd = client.post(f"/staff/api/time-off/{rid_req}/withdraw")
    assert wd.status_code == 200
    fixed_end = (date.today() + timedelta(days=11)).isoformat()
    again = client.post("/staff/api/time-off", json={"start_date": start, "end_date": fixed_end})
    assert again.status_code == 200


def _published_week(db_path, rid, rows_csv):
    conn = models.get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                       "published_at) VALUES (?,?,?,?,datetime('now'))",
                       (rid, "2026-10-05", "2026-10-11", rows_csv))
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


def test_approving_time_off_over_a_published_shift_names_the_conflict(db_path, rid):
    """A4 #30 / MOD-EMP-8."""
    _published_week(db_path, rid, HEADER +
                    "2026-10-06,Tuesday,Jordan P.,Server,11:00,17:00,6,,,\n"
                    "2026-10-07,Wednesday,Jordan P.,Server,11:00,17:00,6,,,\n")
    row, err = time_off.request_time_off(rid, "Jordan P.", "2026-10-06", "2026-10-06", db_path=db_path,
                                         today=date(2026, 9, 22))
    assert err is None
    body, status = _call(strategy_routes._do_time_off_decide, _owner_u(rid), {"decision": "approve"}, row["id"])
    assert status == 200
    conflicts = body.get("conflicts") or []
    assert [c.get("date") for c in conflicts] == ["2026-10-06"]


def test_a_double_tapped_time_off_request_files_only_one(db_path, rid, monkeypatch):
    """A4 #31 / MOD-EMP-9 — both taps pass the overlap check before either
    insert commits."""
    real = models.get_conn
    gate = threading.Barrier(2)

    def hook(sql):
        if sql.lstrip().startswith("INSERT INTO staff_time_off"):
            try:
                gate.wait(timeout=1)
            except threading.BrokenBarrierError:
                pass
    monkeypatch.setattr(time_off, "get_conn", lambda *a, **k: _ConnProxy(real(db_path), hook))
    start = (date.today() + timedelta(days=5)).isoformat()
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        time_off.request_time_off(rid, "Ana", start, start, db_path=db_path))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    conn = real(db_path)
    n = conn.execute("SELECT COUNT(*) FROM staff_time_off WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 1


# ── Contacts ───────────────────────────────────────────────────────────────

@pytest.fixture
def mobile_client(db_path, rid):
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    client = app.test_client()
    create_user(rid, "alice", "alice@x.test", "correct-horse", db_path=db_path)
    token = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse"}).get_json()["token"]
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    return client


def test_an_address_with_no_at_sign_is_refused(mobile_client):
    """A4 #35."""
    resp = mobile_client.post("/mobile/api/labor/staff-contacts",
                              json={"employee_name": "Maria G.", "email": "maria.example.com"})
    assert resp.status_code == 400


def test_saving_only_a_phone_number_keeps_the_email(db_path, rid):
    """A4 #36 / MOD-EMP-4 — the store layer."""
    models.set_staff_contact(rid, "Maria G.", "maria@x.test", None, db_path=db_path)
    models.set_staff_contact(rid, "Maria G.", None, "+15555550123", db_path=db_path)
    c = models.get_staff_contacts(rid, db_path=db_path)[0]
    assert c["email"] == "maria@x.test" and c["phone"] == "+15555550123"


def test_ask_cavnar_adding_a_phone_keeps_the_email(db_path, rid):
    """A4 #36 / MOD-EMP-4 — the tool is documented 'direct, not proposed',
    so nothing else catches it."""
    import ask_cavnar_tools
    models.set_staff_contact(rid, "Maria G.", "maria@x.test", None, db_path=db_path)
    out = ask_cavnar_tools._set_staff_contact(rid, employee_name="Maria G.", phone="+15555550123")
    assert out["ok"] is True
    assert models.get_staff_contacts(rid, db_path=db_path)[0]["email"] == "maria@x.test"


def test_a_phone_that_is_not_a_phone_number_is_refused(mobile_client, db_path, rid):
    """A4 #37 / MOD-EMP-4."""
    resp = mobile_client.post("/mobile/api/labor/staff-contacts",
                              json={"employee_name": "Maria G.", "email": "maria@x.test",
                                    "phone": "<script>call me maybe</script>"})
    assert resp.status_code == 400
    assert all("<script>" not in c["phone"] for c in models.get_staff_contacts(rid, db_path=db_path))


def test_a_contact_saved_under_a_different_case_updates_the_same_person(db_path, rid):
    """A4 #38 / MOD-EMP-2."""
    models.set_staff_contact(rid, "Maria G.", "maria@x.test", None, db_path=db_path)
    models.set_staff_contact(rid, "maria g.", "maria@x.test", "+15555550123", db_path=db_path)
    assert len(models.get_staff_contacts(rid, db_path=db_path)) == 1


# ── Checklists ─────────────────────────────────────────────────────────────

class _FakeDate(date):
    fixed = date(2026, 9, 22)

    @classmethod
    def today(cls):
        return cls.fixed


def test_the_checklist_with_no_date_uses_the_restaurants_local_date(db_path, rid, staff_app, monkeypatch):
    """A4 #44 / MOD-EMP-5 — 8pm in Chicago is 01:00 UTC tomorrow. A task the
    iPhone ticked for tonight must still read as done on the refetch."""
    import datetime as _dtmod
    uid, _ = _staff(db_path, rid)
    models.add_manual_team_member(rid, "Jordan P.", role="Server", db_path=db_path)
    t = models.add_task_template(rid, "Server", "Roll silverware", db_path=db_path)
    models.set_task_completion(rid, t["id"], "2026-09-21", True, completed_by="Jordan P.", db_path=db_path)
    monkeypatch.setattr(_dtmod, "date", _FakeDate)                     # the server's date: 9/22 (UTC)
    _frozen_utc(monkeypatch, time_utils, datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc))
    client = staff_app.test_client()
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    body = client.get("/staff/api/tasks").get_json()
    assert [x["done"] for x in body["tasks"]] == [True]


def _swift_staff_portal():
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent / "ios" / "CavnarAI" / "CavnarAI" / "Features"
            / "Staff" / "StaffPortalView.swift").read_text()


def test_the_ios_checklist_refetch_names_the_date_it_completed_on():
    """A4 #44 / MOD-EMP-5 — the iOS half: every GET of /staff/api/tasks
    carries the same local date the completion was filed under."""
    import re
    src = _swift_staff_portal()
    gets = re.findall(r'authed\("(/staff/api/tasks[^"]*)"\)', src)
    assert gets, "no task fetch found"
    assert all("date=" in g for g in gets), gets


def test_the_ios_task_date_formatter_is_pinned_to_a_gregorian_posix_locale():
    """A4 #44 / MOD-EMP-5 — on a non-Gregorian device calendar the year the
    server receives is not the Gregorian one, and the 400 is swallowed."""
    src = _swift_staff_portal()
    assert "en_US_POSIX" in src


def test_a_server_role_spelled_in_lower_case_still_sees_the_server_checklist(db_path, rid, staff_app):
    """A4 #45 / MOD-EMP-6 — the job says 'server ', the template says 'Server'."""
    uid, _ = _staff(db_path, rid)
    models.add_manual_team_member(rid, "Jordan P.", role="server ", db_path=db_path)
    models.add_task_template(rid, "Server", "Roll silverware", db_path=db_path)
    client = staff_app.test_client()
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    body = client.get(f"/staff/api/tasks?date={date.today().isoformat()}").get_json()
    assert [x["label"] for x in body["tasks"]] == ["Roll silverware"]


# ── Deactivation reaches every surface ─────────────────────────────────────

def test_deactivating_an_employee_on_the_roster_ends_their_portal_access(db_path, rid, staff_app):
    """A4 #6 / MOD-EMP-3."""
    uid, _ = _staff(db_path, rid)
    _published_week(db_path, rid, HEADER + "2026-10-06,Tuesday,Ana B.,Server,11:00,17:00,6,,,\n")
    client = staff_app.test_client()
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    assert client.get("/staff/api/colleagues").status_code == 200
    body, status = _call(strategy_routes._do_staff_settings_set, _owner_u(rid),
                         {"employee_name": "Jordan P.", "active": False})
    assert status == 200
    assert client.get("/staff/api/colleagues").status_code == 401


def test_the_team_rating_list_leaves_out_someone_deactivated_on_the_roster(db_path, rid, mobile_client):
    """A4 #8 / MOD-EMP-3 — /labor/roster hides them; /labor/team must too."""
    models.save_client_data(rid, "shifts", HEADER +
                            "2026-09-01,Tuesday,Ana B.,Server,11:00,17:00,6,6,4000,\n"
                            "2026-09-01,Tuesday,Left Guy,Server,11:00,17:00,6,6,4000,\n", db_path=db_path)
    ss.upsert(rid, "Left Guy", active=False, db_path=db_path)
    body = mobile_client.get("/mobile/api/labor/team").get_json()
    names = [t["name"] for t in body["team"]]
    assert "Ana B." in names
    assert "Left Guy" not in names


# ── Open shifts ────────────────────────────────────────────────────────────

def test_an_open_shift_claim_that_cannot_be_checked_gives_a_plain_message(db_path, rid, monkeypatch):
    """A4 #50 / MOD-EMP-10."""
    hid = _published_week(db_path, rid, HEADER + "2026-10-06,Tuesday,Ana B.,Server,11:00,17:00,6,,,\n")
    conn = models.get_conn(db_path)
    cur = conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, "
                       "shift_start, shift_end, role, status) VALUES (?,?,?,?,?,?,?,'open')",
                       (rid, hid, "Ana B.", "2026-10-06", "11:00", "17:00", "Server"))
    conn.commit()
    req_id = cur.lastrowid
    conn.close()

    def broken(*a, **k):
        raise RuntimeError("sqlite3.OperationalError: no such column: secret_internal_col")
    monkeypatch.setattr(schedule_engine._rules, "build_constraints", broken)
    with pytest.raises(shift_requests.ShiftRequestError) as exc:
        shift_requests.claim(rid, req_id, "Jordan P.", db_path=db_path)
    msg = str(exc.value)
    assert "secret_internal_col" not in msg and "OperationalError" not in msg
