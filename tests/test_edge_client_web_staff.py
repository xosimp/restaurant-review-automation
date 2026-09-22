"""The staff-facing web pages under the edge cases the CLIENT audit found.

Staff reach Cavnar three ways on the web: the PIN pad (staff_login.html),
the portal (staff_portal.html) and the per-person schedule link in their
weekly email (`/s/<token>`, staff_schedule.html). What these protect:

- CLIENT-3: the server accepts a 4–8 digit PIN, the portal lets you choose
  one, and the pad auto-submits at 4 digits — so a 6-digit PIN can never be
  typed, and every truncated attempt counts toward the 15-minute lock-out.
- CLIENT-11: re-saving availability from the email link erases the saved
  note (the field is never pre-filled and a blank one overwrites), accepts
  "all 7 days blocked" that the portal refuses, and can leave a day both
  available and unavailable in the same row.
- CLIENT-45: the link page and the portal print ISO dates to staff.
- CLIENT-46: time-off "Request" is check-then-insert with a button that is
  never disabled, so a double tap stores duplicate rows.
- CLIENT-47 / CLIENT-61 / CLIENT-48: an expired 14-hour shift session is a
  dead end, the task date is the browser's, and errors come as alert().

Routes are exercised through the Flask test client with the blueprints
registered the way tests/test_staff_portal.py does; the inline ES5 is read
as source, since the suite has no JS engine.
"""
import json
import os
import re
import threading
from datetime import date, timedelta

import pytest
from flask import Flask
from werkzeug.datastructures import MultiDict

import auth
import client_api
import mobile_api
import models
import time_off
from auth import (create_staff_session, create_user, get_or_create_staff_portal_token,
                  init_auth, set_membership_pin, upsert_membership)
from models import Restaurant, create_restaurant
from staff_routes import staff_bp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _read(name):
    with open(os.path.join(ROOT, "templates", name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def _redirect(db_path, monkeypatch):
    """Every module that bound get_conn reads the test database. Not
    autouse: the source-scan tests below need no database at all."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, time_off):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def client(_redirect):
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(staff_bp)
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _restaurant(db_path):
    return create_restaurant(Restaurant(name="Maple & Rye", owner_email="o@x.test"), db_path=db_path)


def _staff(db_path, rid, name="Sofia R.", pin="8317"):
    uid = create_user(rid, "sofia", "sofia@x.test", "unused", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    set_membership_pin(m["id"], rid, pin, db_path=db_path)
    return uid, m["id"]


def _login(client, token, membership_id, pin):
    roster = client.get(f"/staff/api/roster/{token}").get_json() or {}
    return client.post(f"/staff/r/{token}/login",
                       json={"membership_id": membership_id, "pin": pin,
                             "nonce": roster.get("login_nonce", "")})


SCHEDULE_CSV = """date,day,employee,role,shift_start,shift_end,scheduled_hours,notes
2026-09-28,Monday,Sofia R.,Server,16:00,22:00,6.0,
"""


def _share(db_path, rid, name="Sofia R."):
    conn = models.get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled,
                                      hours_budget, labor_target, schedule_csv, summary_json)
        VALUES (?, '2026-09-28', '2026-10-04', 6, 24, 30, ?, '[]')""", (rid, SCHEDULE_CSV))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return models.create_schedule_share(rid, sid, name, db_path=db_path)


def _submit_from_link(client, token, days, note=""):
    """What the link page's own form sends, CSRF pair included."""
    client.set_cookie("csrf_js", "tok-for-test", domain="localhost")
    items = [("unavailable", d) for d in days] + [("note", note), ("csrf_token", "tok-for-test")]
    return client.post(f"/s/{token}/availability", data=MultiDict(items))


def _row(db_path, rid, name="Sofia R."):
    for r in models.get_staff_availability(rid, db_path=db_path):
        if r["employee_name"] == name:
            return r
    return None


# ── CLIENT-3: PIN length ────────────────────────────────────────────────────

def _auto_submit_lengths(page, counter):
    return [int(n) for n in re.findall(r"if\s*\(\s*%s\.length\s*===\s*(\d+)\s*\)" % counter, page)]


def test_the_server_accepts_pins_of_four_to_eight_digits():
    assert (auth.PIN_MIN_LENGTH, auth.PIN_MAX_LENGTH) == (4, 8)


def test_a_six_digit_pin_signs_in_when_all_six_digits_reach_the_server(client, db_path):
    """The server half already works — the pad is the only thing in the way."""
    rid = _restaurant(db_path)
    uid, mid = _staff(db_path, rid)
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    changed = client.post("/staff/api/pin", json={"current_pin": "8317", "new_pin": "835172"})
    assert changed.status_code == 200 and changed.get_json()["ok"]
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    fresh = client.application.test_client()
    assert _login(fresh, token, mid, "8351").status_code == 401     # what the pad sends today
    ok = _login(fresh, token, mid, "835172")
    assert ok.status_code == 200 and ok.get_json()["ok"]


def test_both_pin_pads_are_still_on_the_page():
    page = _read("staff_login.html")
    assert "'.pad button[data-d]'" in page and "'.pad button[data-sd]'" in page
    assert "function submit()" in page or "submit()" in page


@pytest.mark.xfail(strict=True, reason="CLIENT-3: the sign-in pad auto-submits after 4 digits, so a 5-8 digit PIN the server accepts can never be entered")
def test_the_sign_in_pad_can_enter_every_pin_length_the_server_accepts():
    for n in _auto_submit_lengths(_read("staff_login.html"), "pin"):
        assert n >= auth.PIN_MAX_LENGTH, "auto-submits at %d digits" % n


@pytest.mark.xfail(strict=True, reason="CLIENT-3: the self-signup pad also claims the account after 4 digits")
def test_the_self_signup_pad_can_enter_every_pin_length_the_server_accepts():
    for n in _auto_submit_lengths(_read("staff_login.html"), "suPin"):
        assert n >= auth.PIN_MAX_LENGTH, "auto-submits at %d digits" % n


# ── CLIENT-11: the email-link availability form ─────────────────────────────

def test_the_portal_refuses_all_seven_days_blocked(client, db_path):
    rid = _restaurant(db_path)
    uid, _ = _staff(db_path, rid)
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    r = client.post("/staff/api/availability", json={"unavailable_days": DAYS})
    assert r.status_code == 400


def test_the_portal_keeps_available_and_unavailable_days_disjoint(client, db_path):
    rid = _restaurant(db_path)
    uid, _ = _staff(db_path, rid)
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    client.post("/staff/api/availability", json={"unavailable_days": ["Monday"], "notes": "not before 10am"})
    row = _row(db_path, rid)
    assert not set(json.loads(row["available_days"])) & set(json.loads(row["unavailable_days"]))
    assert row["notes"] == "not before 10am"


@pytest.mark.xfail(strict=True, reason="CLIENT-11: the /s/ link's note input has no value, so the saved note is never shown")
def test_the_link_form_shows_the_note_already_saved(client, db_path):
    rid = _restaurant(db_path)
    token = _share(db_path, rid)
    models.save_staff_availability(rid, "Sofia R.", DAYS[1:], ["Monday"], notes="away Oct 3-6", db_path=db_path)
    page = client.get(f"/s/{token}").get_data(as_text=True)
    field = re.search(r'<input[^>]*name="note"[^>]*>', page).group(0)
    assert 'value="away Oct 3-6"' in field, field


@pytest.mark.xfail(strict=True, reason="CLIENT-11: re-saving from the email link with the (never pre-filled) note blank erases the saved note")
def test_resaving_from_the_link_keeps_the_existing_note(client, db_path):
    rid = _restaurant(db_path)
    token = _share(db_path, rid)
    models.save_staff_availability(rid, "Sofia R.", DAYS[1:], ["Monday"], notes="not before 10am", db_path=db_path)
    r = _submit_from_link(client, token, ["Monday", "Friday"], note="")
    assert r.status_code == 302
    assert _row(db_path, rid)["notes"] == "not before 10am"


@pytest.mark.xfail(strict=True, reason="CLIENT-11: the email link stores 'all 7 days blocked', which the portal refuses")
def test_the_link_refuses_all_seven_days_blocked_like_the_portal(client, db_path):
    rid = _restaurant(db_path)
    token = _share(db_path, rid)
    models.save_staff_availability(rid, "Sofia R.", DAYS[1:], ["Monday"], db_path=db_path)
    _submit_from_link(client, token, DAYS)
    assert sorted(json.loads(_row(db_path, rid)["unavailable_days"])) != sorted(DAYS)


@pytest.mark.xfail(strict=True, reason="CLIENT-11: the link keeps the old available_days, so a newly blocked day is stored as both available and unavailable")
def test_a_link_save_never_leaves_a_day_both_available_and_unavailable(client, db_path):
    rid = _restaurant(db_path)
    token = _share(db_path, rid)
    models.save_staff_availability(rid, "Sofia R.", DAYS[1:], ["Monday"], db_path=db_path)
    _submit_from_link(client, token, ["Tuesday"])
    row = _row(db_path, rid)
    both = set(json.loads(row["available_days"] or "[]")) & set(json.loads(row["unavailable_days"] or "[]"))
    assert not both, both


# ── CLIENT-45: dates staff see ──────────────────────────────────────────────

MDY = r"\d{1,2}/\d{1,2}/\d{2}"


def test_the_link_page_renders_the_week_and_shift_dates(client, db_path):
    rid = _restaurant(db_path)
    token = _share(db_path, rid)
    page = client.get(f"/s/{token}").get_data(as_text=True)
    assert re.search(r'class="sub">Week of', page)
    assert re.search(r'class="date">[^<]+<', page)


@pytest.mark.xfail(strict=True, reason="CLIENT-45: the /s/ page prints 'Week of 2026-09-28 – 2026-10-04' and ISO shift dates")
def test_the_link_page_dates_are_mdy(client, db_path):
    rid = _restaurant(db_path)
    token = _share(db_path, rid)
    page = client.get(f"/s/{token}").get_data(as_text=True)
    sub = re.search(r'class="sub">([^<]*)<', page).group(1)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", sub) and re.search(MDY, sub), sub
    for d in re.findall(r'class="date">([^<]*)<', page):
        assert re.fullmatch(MDY, d.strip()), d


def test_the_portal_has_an_mdy_formatter():
    assert "function mdyShort(iso)" in _read("staff_portal.html")


@pytest.mark.xfail(strict=True, reason="CLIENT-45: the portal's 'Last saved' prints String(updated_at).slice(0, 10), an ISO date")
def test_the_portal_last_saved_line_is_mdy():
    assert "'Last saved ' + esc(String(d.updated_at).slice(0, 10))" not in _read("staff_portal.html")


@pytest.mark.xfail(strict=True, reason="CLIENT-45: portal time-off rows print q.start_date/q.end_date as ISO")
def test_the_portal_time_off_rows_are_mdy():
    page = _read("staff_portal.html")
    render = page[page.index("function toRender(d)"):page.index("function loadTimeOff()")]
    when = re.search(r"var when = ([^;]*);", render).group(1)
    assert "mdyShort(" in when or "fmtShort(" in when, when


# ── CLIENT-46: time-off double submit ───────────────────────────────────────

class _GatedConn:
    """A connection that holds every caller at the overlap check until the
    other caller has also passed it — the interleaving a double tap on a
    slow phone produces, made deterministic. If a fix serialises the
    callers before the check (BEGIN IMMEDIATE), the gate times out and
    lets the waiting one through."""
    def __init__(self, conn, gate):
        self._conn, self._gate = conn, gate

    def execute(self, sql, *a):
        cur = self._conn.execute(sql, *a)
        if sql.lstrip().upper().startswith("SELECT ID FROM STAFF_TIME_OFF"):
            try:
                self._gate.wait()
            except threading.BrokenBarrierError:
                pass
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_one_time_off_request_stores_one_row(db_path, _redirect):
    rid = _restaurant(db_path)
    start = (date.today() + timedelta(days=10)).isoformat()
    row, err = time_off.request_time_off(rid, "Sofia R.", start, start, db_path=db_path)
    assert row and not err
    again, err2 = time_off.request_time_off(rid, "Sofia R.", start, start, db_path=db_path)
    assert again is None and "already" in err2
    assert len(time_off.mine(rid, "Sofia R.", db_path=db_path)) == 1


@pytest.mark.xfail(strict=True, reason="CLIENT-46: time-off is check-then-insert in a deferred transaction, so two overlapping requests both pass the check")
def test_two_overlapping_time_off_requests_store_one_row(db_path, _redirect, monkeypatch):
    rid = _restaurant(db_path)
    start = (date.today() + timedelta(days=10)).isoformat()
    gate = threading.Barrier(2, timeout=2)
    real = models.get_conn
    monkeypatch.setattr(time_off, "get_conn", lambda *a, **k: _GatedConn(real(db_path), gate))

    def tap():
        try:
            time_off.request_time_off(rid, "Sofia R.", start, start, db_path=db_path)
        except Exception:
            pass     # a unique-index refusal is a correct outcome too

    threads = [threading.Thread(target=tap) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    monkeypatch.setattr(time_off, "get_conn", lambda *a, **k: real(db_path))
    assert len(time_off.mine(rid, "Sofia R.", db_path=db_path)) == 1


@pytest.mark.xfail(strict=True, reason="CLIENT-46: the portal's Request button is never disabled while the request is in flight")
def test_the_time_off_button_is_disabled_while_sending():
    page = _read("staff_portal.html")
    handler = page[page.index("$('to-send').onclick"):page.index("function loadTimeOff()")]
    assert ".disabled = true" in handler or ".disabled=true" in handler, handler


# ── CLIENT-47: an expired shift session ─────────────────────────────────────

def test_an_expired_shift_session_is_told_so_in_json(client, db_path):
    client.set_cookie("staff_session", "no-such-session")
    r = client.get("/staff/api/shifts")
    assert r.status_code == 401
    assert r.get_json()["session_expired"] is True


@pytest.mark.xfail(strict=True, reason="CLIENT-47: the portal never reads session_expired, so an ended shift session shows 'Could not load your shifts.' with no way back")
def test_the_portal_sends_an_expired_session_back_to_sign_in():
    assert "session_expired" in _read("staff_portal.html")


# ── the rest of the portal ──────────────────────────────────────────────────

def test_the_task_checkbox_handler_still_posts():
    page = _read("staff_portal.html")
    assert "/staff/api/tasks/complete" in page


@pytest.mark.xfail(strict=True, reason="CLIENT audit staff edge 15: a refused or failed task toggle just reloads the list; the employee is never told it didn't save")
def test_a_failed_task_toggle_is_shown_to_the_employee():
    page = _read("staff_portal.html")
    i = page.index("'/staff/api/tasks/complete'")
    chain = page[i:page.index("});", page.index(".catch(", i)) + 3]
    assert "d.ok" in chain or "!r.ok" in chain, chain


@pytest.mark.xfail(strict=True, reason="CLIENT-61: the portal's task date is the browser's local date, not the restaurant's")
def test_the_task_date_does_not_come_from_the_browser_clock():
    page = _read("staff_portal.html")
    today_fn = page[page.index("function today()"):page.index("}", page.index("function today()")) + 1]
    assert "new Date()" not in today_fn, today_fn


@pytest.mark.xfail(strict=True, reason="CLIENT-48: the portal reports shift-request errors with window.alert()")
def test_the_portal_reports_errors_without_alert():
    assert "window.alert(" not in _read("staff_portal.html")
