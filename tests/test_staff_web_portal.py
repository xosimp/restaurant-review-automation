"""The browser staff portal (parity audit #100, owner 10/7/26: staff without
an iPhone, Android included, are not left out).

What this holds:
- /staff/r/<code> is the PIN pad again; /staff/home is the portal for a
  signed-in employee; the landing page offers the browser and the app.
- The portal is a shell over the SAME /staff/api/* routes the app calls:
  every path its script names is a real staff route, and each flow (time
  off, swap, claim, tick, photo, inbox reply, availability) works with the
  PIN cookie session — and still with the app's Bearer token.
- CSRF: a cookie-authenticated write must echo the csrf_js cookie
  (staff_routes.staff_csrf_check, wired app-wide in hosted_dashboard); a
  Bearer call and the pre-session sign-in are left alone; sign-out is a
  GET that asks and a POST that carries the token.
- Unauthenticated access redirects; a console login is not a staff session.

Clocks are pinned (October 2026, the Sunday before the published week).
"""
import datetime as dt
import io
import os
import re
import sys

import pytest
from flask import Flask

# Imported before any fixture patches models.get_conn (bound imports).
import shift_requests, staff_comms, staff_schedule, staff_settings, task_sheets, time_off  # noqa: E401,F401
import people, push, schedule_versions  # noqa: E401,F401

import auth
import models
import schedule_versions as sv
import shift_requests as srq
import task_sheets as ts
import time_utils
from auth import create_session, create_staff_session, create_user, get_or_create_staff_portal_token, \
    init_auth, set_membership_pin, upsert_membership
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
NOW = dt.datetime(2026, 10, 4, 9, 0)          # the Sunday before W1, 9am local
CSRF = "tok-for-the-web-portal"


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


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
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r=None, naive=False: NOW)
    init_auth(db_path=db_path)
    return db_path


@pytest.fixture
def told(monkeypatch):
    """Every notice to staff or managers, captured instead of sent."""
    box = []
    monkeypatch.setattr(srq, "_tell_staff", lambda *a, **k: box.append(("staff", a)) or [], raising=False)
    monkeypatch.setattr(srq, "_email_staff", lambda *a, **k: box.append(("email", a)) or 0)
    monkeypatch.setattr(srq, "_tell_managers", lambda *a, **k: box.append(("managers", a)))
    monkeypatch.setattr(people, "tell", lambda *a, **k: box.append(("tell", a)))
    monkeypatch.setattr(staff_comms, "_tell_deciders", lambda *a, **k: box.append(("deciders", a)) or 1)
    monkeypatch.setattr(time_off, "_tell_requester", lambda *a, **k: None)
    monkeypatch.setattr(staff_settings, "_tell_deciders", lambda *a, **k: 0)
    return box


def _app(csrf=True):
    """The staff blueprints as hosted_dashboard wires them: the app-wide
    staff CSRF check and the csrf cookie on every response."""
    from csrf import ensure_csrf_cookie
    from staff_knowledge_routes import staff_knowledge_bp
    from staff_routes import staff_bp, staff_csrf_check
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    if csrf:
        app.before_request(staff_csrf_check)
    app.register_blueprint(staff_bp)
    app.register_blueprint(staff_knowledge_bp)
    app.after_request(ensure_csrf_cookie)
    return app


def _restaurant(db_path, roster=(("Ana", "Server"), ("Ben", "Server"), ("Cara", "Server"))):
    rid = create_restaurant(Restaurant(name="Maple & Rye", owner_email="o@x.test"), db_path=db_path)
    for name, role in roster:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def _member(db_path, rid, name, pin="8317", job_role=None):
    uid = create_user(rid, name.lower().replace(" ", ""), f"{name.lower()}@x.test", "unused", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, job_role=job_role, db_path=db_path)
    if pin:
        set_membership_pin(m["id"], rid, pin, db_path=db_path)
    return uid, m["id"]


def _publish(db_path, rid, rows, dates=W1):
    text = HEADER + "\n" + "\n".join(f"{d},{dt.date.fromisoformat(d).strftime('%A')},{n},{role},{s},{e},{h},"
                                     for d, n, role, s, e, h in rows)
    hid = models.save_schedule_history(rid, dates[0], dates[-1], 0, 0, 30, text, [], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    sv.append(rid, hid, "published", text, db_path=db_path)
    return hid


def _web(app, db_path, rid, uid):
    """A phone browser signed in with its PIN: the staff cookie and the
    page's csrf cookie."""
    c = app.test_client()
    c.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, path, body=None, **kw):
    """What the portal's fetch wrapper (_csrf_fetch.html) sends."""
    return c.post(path, json=body if body is not None else {}, headers={"X-CSRF": CSRF}, **kw)


# ── the pages ───────────────────────────────────────────────────────────────

def test_home_renders_the_portal_for_a_signed_in_employee(db):
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana Reyes")
    c = _app().test_client()
    c.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db))
    r = c.get("/staff/home")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "private, no-store" in r.headers.get("Cache-Control", "")
    assert "Maple &amp; Rye" in page and "Hi, Ana" in page
    for tab in ("Today", "Schedule", "Requests", "Tasks", "Inbox", "Me"):
        assert f"<span>{tab}</span>" in page, tab
    assert "X-CSRF" in page, "the double-submit fetch wrapper is on the page"
    assert any(h.startswith("csrf_js=") for h in r.headers.getlist("Set-Cookie")), "the page mints its csrf cookie"
    code = auth.get_join_code(rid, db_path=db)
    assert f'"/staff/r/{code}"' in page, "sign in again goes to this restaurant's PIN pad"


def test_the_ready_page_after_a_web_signup_offers_the_browser_and_the_app(db):
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana")
    c = _app().test_client()
    c.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db))
    page = c.get("/staff/home?ready=1").get_data(as_text=True)
    assert "You&rsquo;re all set" in page and 'href="/staff/home">Use it in your browser' in page


def test_unauthenticated_access_redirects_to_the_landing_page(db):
    rid = _restaurant(db)
    c = _app().test_client()
    r = c.get("/staff/home")
    assert r.status_code == 302 and r.headers["Location"].endswith("/staff/")
    # A console login is a different product: not a staff session.
    owner = create_user(rid, "erik", "erik@x.test", "owner-pw-123456", db_path=db)
    upsert_membership(owner, rid, "client", db_path=db)
    c.set_cookie("staff_session", create_session(owner, db_path=db))
    assert c.get("/staff/home").status_code == 302
    assert c.get("/staff/api/shifts").status_code in (401, 403)
    expired = _app().test_client()
    expired.set_cookie("staff_session", "not-a-session")
    assert expired.get("/staff/home").status_code == 302
    assert expired.get("/staff/api/shifts").get_json()["session_expired"] is True


def test_the_landing_page_offers_the_browser_first_and_the_app(db, monkeypatch):
    rid = _restaurant(db)
    c = _app().test_client()
    page = c.get("/staff/").get_data(as_text=True)
    assert "Use it in your browser" in page and 'id="code-in"' in page and "Android included" in page
    assert "TestFlight" in page
    monkeypatch.setenv("IOS_APP_STORE_URL", "https://apps.apple.com/app/id1")
    page = c.get("/staff/").get_data(as_text=True)
    assert 'href="https://apps.apple.com/app/id1"' in page and "Get the Cavnar AI app" in page
    # Signed in already: straight to the portal.
    uid, _ = _member(db, rid, "Ana")
    c.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db))
    assert 'href="/staff/home">Use it in your browser' in c.get("/staff/").get_data(as_text=True)


def test_the_pin_pad_signs_in_with_the_restaurant_code_and_lands_on_the_portal(db):
    rid = _restaurant(db)
    uid, mid = _member(db, rid, "Ana", pin="8317")
    code = auth.get_join_code(rid, db_path=db)
    c = _app().test_client()
    pad = c.get(f"/staff/r/{code}").get_data(as_text=True)
    assert 'data-id="%d"' % mid in pad and "Ana" in pad
    nonce = re.search(r'var NONCE = "([^"]+)"', pad).group(1)
    r = c.post(f"/staff/r/{code}/login", json={"membership_id": mid, "pin": "8317", "nonce": nonce})
    body = r.get_json()
    assert r.status_code == 200 and body["redirect"] == "/staff/home" and "token" not in body
    assert c.get("/staff/home").status_code == 200


def test_the_pin_pad_keeps_the_lockout_and_the_one_shot_nonce(db):
    rid = _restaurant(db)
    _uid, mid = _member(db, rid, "Ana", pin="8317")
    code = auth.get_join_code(rid, db_path=db)
    c = _app().test_client()
    nonce = re.search(r'var NONCE = "([^"]+)"', c.get(f"/staff/r/{code}").get_data(as_text=True)).group(1)
    wrong = c.post(f"/staff/r/{code}/login", json={"membership_id": mid, "pin": "0000", "nonce": nonce})
    assert wrong.status_code == 401 and wrong.get_json()["login_nonce"] != nonce
    replay = c.post(f"/staff/r/{code}/login", json={"membership_id": mid, "pin": "8317", "nonce": nonce})
    assert replay.status_code == 409 and replay.get_json()["nonce_expired"] is True


def test_sign_out_asks_on_get_and_needs_the_token_on_post(db):
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana")
    app = _app()
    c = _web(app, db, rid, uid)
    token = c.get_cookie("staff_session").value
    page = c.get("/staff/logout").get_data(as_text=True)
    assert "Sign out?" in page and f"name='csrf_token' value='{CSRF}'" in page
    assert auth.get_session_user(token, db_path=db) is not None, "a GET only asks (SEC-34)"
    assert c.post("/staff/logout").status_code == 403, "no token, no sign-out"
    assert auth.get_session_user(token, db_path=db) is not None
    r = c.post("/staff/logout", data={"csrf_token": CSRF})
    assert r.status_code == 302 and r.headers["Location"].endswith("/staff/r/" + auth.get_join_code(rid, db_path=db))
    assert auth.get_session_user(token, db_path=db) is None


# ── CSRF ────────────────────────────────────────────────────────────────────

def test_a_cookie_write_without_the_csrf_token_is_refused(db, told):
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana")
    c = _web(_app(), db, rid, uid)
    body = {"start_date": "2026-10-12", "end_date": "2026-10-13"}
    assert c.post("/staff/api/time-off", json=body).status_code == 403
    assert c.post("/staff/api/time-off", json=body, headers={"X-CSRF": "another"}).status_code == 403
    assert c.post("/staff/api/language", json={"language": "es"}).status_code == 403, "staff_knowledge_bp too"
    assert c.post("/staff/api/messages", json={"body": "hi"}).status_code == 403
    assert time_off.mine(rid, "Ana") == [] and told == []
    assert _post(c, "/staff/api/time-off", body).status_code == 200
    # Reads are never held to it.
    assert c.get("/staff/api/time-off").status_code == 200


def test_the_apps_bearer_calls_and_the_pre_session_routes_are_left_alone(db, told):
    rid = _restaurant(db)
    uid, mid = _member(db, rid, "Ana", pin="8317")
    app = _app()
    token = create_staff_session(uid, rid, db_path=db)
    c = app.test_client()
    bearer = {"Authorization": f"Bearer {token}"}
    r = c.post("/staff/api/time-off", json={"start_date": "2026-10-12", "end_date": "2026-10-12"}, headers=bearer)
    assert r.status_code == 200, "the app sends no csrf cookie"
    # An app whose cookie jar still holds a staff cookie: the Bearer header
    # is what no cross-site page can add, so it is never the forged one.
    c.set_cookie("staff_session", token)
    assert c.post("/staff/api/language", json={"language": "es"}, headers=bearer).status_code == 200
    # Sign-in with a stale cookie in the jar: no session to ride, so no check.
    code = auth.get_join_code(rid, db_path=db)
    roster = c.get(f"/staff/api/roster/{code}").get_json()
    login = c.post(f"/staff/r/{code}/login", json={"membership_id": mid, "pin": "8317", "nonce": roster["login_nonce"]})
    assert login.status_code == 200


def test_hosted_dashboard_wires_the_staff_csrf_check():
    src = _read("hosted_dashboard.py")
    assert "from staff_routes import staff_bp, staff_csrf_check" in src
    assert "app.before_request(staff_csrf_check)" in src
    import staff_routes
    assert staff_routes.STAFF_CSRF_BLUEPRINTS == {"staff", "staff_knowledge"}
    assert "staff.portal_authenticate" in staff_routes.STAFF_CSRF_EXEMPT_ENDPOINTS
    assert not any(e.startswith("staff_knowledge.") for e in staff_routes.STAFF_CSRF_EXEMPT_ENDPOINTS)


# ── each flow over the cookie session ───────────────────────────────────────

def test_time_off_create_withdraw_and_call_off_from_the_browser(db, told):
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana")
    c = _web(_app(), db, rid, uid)
    first = _post(c, "/staff/api/time-off", {"start_date": "2026-10-12", "end_date": "2026-10-13", "reason": "trip"})
    assert first.status_code == 200 and first.get_json()["request"]["status"] == "pending"
    assert _post(c, f"/staff/api/time-off/{first.get_json()['request']['id']}/withdraw").get_json()["ok"]
    part = _post(c, "/staff/api/time-off", {"start_date": "2026-10-20", "end_date": "2026-10-20", "end_time": "16:00"})
    assert part.status_code == 200, part.get_json()
    req = part.get_json()["request"]
    assert req["end_time"], "the portal's 15-minute select value is a time the server reads"
    time_off.decide(rid, req["id"], True, decided_by="erik")
    called = _post(c, f"/staff/api/time-off/{req['id']}/cancel")
    assert called.status_code == 200 and called.get_json()["request"]["status"] == "withdrawn"


def test_a_swap_is_asked_and_answered_from_two_browsers(db, told):
    rid = _restaurant(db)
    app = _app()
    ana, _ = _member(db, rid, "Ana")
    ben, _ = _member(db, rid, "Ben", pin="5063")
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5), (W1[2], "Ben", "Server", "5:00pm", "10:00pm", 5)])
    a = _web(app, db, rid, ana)
    picker = a.get(f"/staff/api/colleagues?shift_date={W1[1]}&shift_start=5:00pm").get_json()
    assert [p["name"] for p in picker["colleagues"]] == ["Ben"]
    asked = _post(a, "/staff/api/shift-requests", {"date": W1[1], "shift_start": "5:00pm", "kind": "swap",
                                                    "target_name": "Ben", "target_date": W1[2], "target_start": "5:00pm"})
    assert asked.status_code == 200, asked.get_json()
    b = _web(app, db, rid, ben)
    ask = b.get("/staff/api/shift-requests").get_json()["asks"][0]
    yes = _post(b, f"/staff/api/shift-requests/{ask['id']}/respond", {"accept": True})
    assert yes.status_code == 200 and yes.get_json()["ok"]


def test_an_open_shift_is_claimed_from_the_browser(db, told):
    rid = _restaurant(db)
    cara, _ = _member(db, rid, "Cara")
    _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4)])
    out = srq.post_open_shift(rid, W1[1], "5:00pm", shift_end="10:00pm", role="Server", actor="erik", now=NOW)
    c = _web(_app(), db, rid, cara)
    board = c.get("/staff/api/shift-requests").get_json()["open"]
    assert [o["id"] for o in board] == [out["request"]["id"]] and board[0]["can_take"] is True
    r = _post(c, f"/staff/api/open-shifts/{out['request']['id']}/claim")
    assert r.status_code == 200 and r.get_json()["ok"]
    shifts = c.get("/staff/api/shifts").get_json()
    tue = next(d for d in shifts["week"] if d["date"] == W1[1])
    assert tue["shift"]["start"] == "5:00pm"


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 50)).save(buf, format="PNG")
    return buf.getvalue()


def test_a_tick_and_a_photo_from_the_browser_and_a_repeat_changes_nothing(db, monkeypatch):
    day = dt.date(2026, 10, 5)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: day)
    monkeypatch.setattr(ts, "local_now", lambda rid, restaurant=None: dt.datetime(2026, 10, 5, 12, 0))
    rid = _restaurant(db)
    s = ts.create_sheet(rid, "Bartender", "opening", db_path=db)
    ts.add_line(rid, s["id"], {"label": "Stock ice"}, db_path=db)
    ts.add_line(rid, s["id"], {"label": "Photo of the bar", "proof": "photo"}, db_path=db)
    _publish(db, rid, [(W1[0], "Dana", "Bartender", "10:00am", "4:00pm", 6)])
    uid, _ = _member(db, rid, "Dana", job_role="Bartender")
    c = _web(_app(), db, rid, uid)
    view = c.get("/staff/api/tasks", headers={"X-Staff-Tasks-Version": "2"}).get_json()
    assert "tasks" not in view, "the portal reads sheets only, like the app"
    a = view["sheets"][0]
    body = {"assignment_id": a["id"], "line_id": a["lines"][0]["line_id"], "done": True}
    one = _post(c, "/staff/api/tasks/complete", body).get_json()
    two = _post(c, "/staff/api/tasks/complete", body).get_json()
    assert one["ok"] and two["ok"] and one["sheet"]["done"] == two["sheet"]["done"] == 1
    photo = c.post("/staff/api/tasks/photo", headers={"X-CSRF": CSRF},
                   data={"assignment_id": str(a["id"]), "line_id": str(a["lines"][1]["line_id"]),
                         "file": (io.BytesIO(_png()), "bar.png", "image/png")},
                   content_type="multipart/form-data")
    assert photo.status_code == 200, photo.get_json()
    tok = photo.get_json()["sheet"]["lines"][1]["photo"]
    img = c.get(f"/staff/api/tasks/photo/{tok}")
    assert img.status_code == 200 and img.headers["Cache-Control"] == "private, no-store"
    # The multipart upload is a write too.
    no = c.post("/staff/api/tasks/photo", data={"assignment_id": str(a["id"]), "line_id": "1",
                                                "file": (io.BytesIO(_png()), "x.png", "image/png")},
                content_type="multipart/form-data")
    assert no.status_code == 403


def test_the_inbox_reply_and_got_it_from_the_browser(db, told):
    rid = _restaurant(db)
    uid, mid = _member(db, rid, "Ana")
    c = _web(_app(), db, rid, uid)
    sent = _post(c, "/staff/api/messages", {"body": "Can I swap Friday?", "shift_date": W1[4]})
    assert sent.status_code == 200 and sent.get_json()["message"]["body"] == "Can I swap Friday?"
    thread = c.get("/staff/api/messages").get_json()
    assert [m["body"] for m in thread["messages"]] == ["Can I swap Friday?"]
    assert thread["messages"][0]["shift_date"] == W1[4]
    inbox = c.get("/staff/api/inbox").get_json()
    assert inbox["ok"] and inbox["announcements"] == []


def test_availability_save_from_the_browser_names_its_conflicts_and_refuses_a_stale_copy(db, told):
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana")
    _publish(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "10:00pm", 5)])
    c = _web(_app(), db, rid, uid)
    av = c.get("/staff/api/availability").get_json()
    week = av["week"]
    for w in week:
        if w["day"] == "Tuesday":
            w["status"] = "off"
        if w["day"] == "Monday":
            w.update(status="window", earliest="10:00", latest="16:00")
    r = _post(c, "/staff/api/availability", {"updated_at": av["updated_at"], "week": week, "notes": "bus"})
    out = r.get_json()
    assert r.status_code == 200 and "Tuesday" in out["unavailable_days"] and out["notes"] == "bus"
    assert [x["date"] for x in out["conflicts"]] == [W1[1]] and out["conflicts_text"]
    stale = _post(c, "/staff/api/availability", {"updated_at": av["updated_at"], "week": week, "notes": "x"})
    assert stale.status_code == 409 and stale.get_json()["availability"]["notes"] == "bus"


def test_preferences_language_and_change_pin_from_the_browser(db, told):
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana", pin="8317")
    c = _web(_app(), db, rid, uid)
    p = _post(c, "/staff/api/preferences", {"preferred_dayparts": ["night"], "desired_hours": 30}).get_json()
    assert p["ok"] and p["preferred_dayparts"] == ["night"]
    assert _post(c, "/staff/api/language", {"language": "es"}).get_json()["language"] == "es"
    changed = _post(c, "/staff/api/pin", {"current_pin": "8317", "new_pin": "835172"}).get_json()
    assert changed["ok"] and changed["signed_out"] is True
    assert c.get("/staff/api/me").status_code == 401, "a new PIN ends the session; the portal signs in again"


def test_the_texts_switch_follows_the_hold_exactly_as_the_api_says(db, monkeypatch):
    """The portal shows the "Schedule texts" switch only when the API's
    sms_available is true — false while STAFF_SMS_HOLD is on."""
    import notify
    rid = _restaurant(db)
    uid, _ = _member(db, rid, "Ana")
    monkeypatch.setattr(notify, "STAFF_SMS_HOLD", True)
    c = _web(_app(), db, rid, uid)
    assert c.get("/staff/api/preferences").get_json()["sms_available"] is False
    page = _read("templates", "staff_portal.html")
    assert "else if (p.sms_available) {" in page


# ── the page holds no rule of its own ──────────────────────────────────────

def test_every_api_path_the_portal_calls_is_a_staff_route():
    """One implementation of each rule: the page only calls the app's own
    /staff/api routes (a typo would 404 in a phone nobody is watching)."""
    app = _app()
    adapter = app.url_map.bind("localhost")
    page = _read("templates", "staff_portal.html")
    # '/staff/api/x' or '/staff/api/x/' + id + '/tail'
    found = re.findall(r"'(/staff/api/[a-z\-/]+)'(?:\s*\+\s*[^+'\n]+\s*\+\s*'(/[a-z\-]+)')?", page)
    paths = {p + ("1" + tail if p.endswith("/") else "") for p, tail in found}
    assert len(paths) >= 20, paths
    for probe in paths:
        p = probe
        for method in ("GET", "POST"):
            try:
                adapter.match(probe, method=method)
                break
            except Exception as e:  # noqa: BLE001
                last = e
        else:
            raise AssertionError(f"{p} is not a staff route ({last!r})")
    for route in ("/staff/api/time-off", "/staff/api/shift-requests", "/staff/api/tasks/complete",
                  "/staff/api/tasks/photo", "/staff/api/messages", "/staff/api/availability",
                  "/staff/api/running-late", "/staff/api/pulse", "/staff/api/ask", "/staff/api/docs",
                  "/staff/api/language", "/staff/api/calendar-link", "/staff/api/pin", "/staff/api/preshift",
                  "/staff/api/colleagues", "/staff/api/inbox", "/staff/api/preferences", "/staff/api/tasks/signoff"):
        assert "'" + route in page, route


def test_the_portal_follows_the_house_rules():
    page = _read("templates", "staff_portal.html")
    assert 'data-theme="dark"' in page, "dark only"
    assert not re.search(r"type=\\?[\"']time[\"']", page), "never a type=time input (DS 7)"
    assert 'accept="image/*" capture="environment"' in page, "the camera opens for a photo line"
    assert '{% include "_csrf_fetch.html" %}' in page
    assert 'class="dr-pulse"' in page and "'Loading…'" not in page and ">Loading…<" not in page
    assert "function mdy(x)" in page and "toISOString().slice(0, 10)" not in page
    assert "Cavnar AI" in page and not re.search(r"Cavnar(?! AI)\b", page.replace("cavnar", ""))
    css = re.sub(r":root\{[^}]*\}|/\*.*?\*/|\{#.*?#\}", "", page.split("</style>")[0], flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", css), "colours come from the tokens"
    # Outward actions go through the confirm sheet, never one tap from a list.
    for act in ("'ask-yes'", "'ask-no'", "'offer-yes'", "'offer-no'", "claim:", "'to-withdraw'", "'to-cancel'",
                "'sr-withdraw'", "signoff:", "'cal-reset'", "'cal-revoke'"):
        i = page.index(act)
        assert "askConfirm(" in page[i:i + 700], act
