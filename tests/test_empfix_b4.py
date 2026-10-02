"""Employee audit fixes B4 — task sheets and the staff server's performance
(task_sheets.py, staff_routes.py /api/tasks*, http_layer, models):

- LG-15: a same-day cover or swap re-resolves today's open sheets, so the
  person now on the shift ticks it and a miss is filed against them.
- PERF-08: a sheet nobody works today settles the day (no CSV re-read);
  /tasks/complete answers with the sheet; photo tokens are joined.
- LG-31 / SEC-13: a photo is stored only after the sheet is the caller's,
  multipart is accepted, and a login has an hourly photo cap.
- LG-30: only a sheet for the business day ± 1 ticks; evaluate sweeps every
  open row, however old.
- COM-10 (H13): an out-of-range critical reading opens a high issue for the
  routed manager and tells the employee "Tell your manager now".
- COM-16 (M14): last night's closing note reaches today's opening manager.
- M13: /staff/api/ is no-store, proof photos private no-store, a narrow
  published-week reader, the flat `tasks` list only for older apps.

Clocks are pinned: the business day is TODAY and the restaurant's wall clock
is TODAY at noon unless a test passes its own."""
import base64
import io
import json
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import auth
import client_api
import http_layer
import issues
import mobile_api
import models
import task_sheets as ts
from auth import create_staff_session, create_user, init_auth
from models import Restaurant, create_restaurant

TODAY = date(2026, 10, 1)        # a Thursday
CSV_HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _at(hhmm, day=TODAY):
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, client_api, mobile_api, issues):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: TODAY)
    monkeypatch.setattr(ts, "local_now", lambda rid, restaurant=None: _at("12:00"))
    sent = []
    monkeypatch.setattr(issues, "_notify", lambda issue_id, token=None, db_path=None, now=None: sent.append(issue_id))
    return sent


def _rid(db_path, name="Simple EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _csv(rows, day=TODAY):
    return CSV_HEAD + "".join(f"{day.isoformat()},{day.strftime('%A')},{e},{r},{a},{b},8,\n" for e, r, a, b in rows)


def _publish(db_path, rid, rows, day=TODAY):
    ws = day - timedelta(days=day.weekday())
    conn = models.get_conn(db_path)
    models._ensure_history_columns(conn)
    cur = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                       "VALUES (?,?,?,?,datetime('now'))",
                       (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), _csv(rows, day)))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _cover(db_path, rid, hid, rows, day=TODAY):
    """What shift_requests._cover / _execute_swap write: the live week's CSV
    in place, plus a version row (schedule_versions.write_on)."""
    from schedule_versions import write_on
    conn = models.get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        write_on(conn, rid, hid, "swap", _csv(rows, day), saved_by="Ben")
        conn.commit()
    finally:
        conn.close()


def _sheet(rid, db_path, job="Bartender", kind="closing", lines=("Count the drawer", "Lock up")):
    s = ts.create_sheet(rid, job, kind, db_path=db_path)
    for l in lines:
        ts.add_line(rid, s["id"], {"label": l} if isinstance(l, str) else l, db_path=db_path)
    return ts.get_sheet(rid, s["id"], db_path=db_path)


@pytest.fixture
def client(db_path):
    from staff_routes import staff_bp
    app = Flask(__name__, template_folder="../templates")
    http_layer.register(app)
    for bp in (staff_bp, client_api.client_bp, mobile_api.mobile_bp):
        app.register_blueprint(bp)
    return app.test_client()


def _login(client, db_path, rid, name, job, username):
    uid = create_user(rid, username, f"{username}@x.test", "unused", db_path=db_path)
    auth.upsert_membership(uid, rid, "employee", employee_name=name, job_role=job, db_path=db_path)
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    return uid


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 50)).save(buf, format="PNG")
    return buf.getvalue()


def _count(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


# ── LG-15: a same-day cover moves the sheet to the person now on the shift ──

def test_a_same_day_cover_hands_the_sheet_to_the_cover_and_the_miss_to_them(db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, lines=[{"label": "Count the drawer", "due_offset_min": 15, "critical": True}, "Lock up"])
    hid = _publish(db_path, rid, [("Ana", "Bartender", "4:00 PM", "11:00 PM")])
    (a,) = ts.staff_view(rid, "Ana", ["Bartender"], db_path=db_path)["sheets"]      # 9am: issued to Ana
    assert a["assignees"] == ["Ana"]
    _cover(db_path, rid, hid, [("Ben", "Bartender", "4:00 PM", "11:00 PM")])           # 2pm: Ben claims it
    ben = ts.staff_view(rid, "Ben", ["Bartender"], db_path=db_path)["sheets"]
    assert [s["id"] for s in ben] == [a["id"]] and ben[0]["assignees"] == ["Ben"]
    assert ts.staff_view(rid, "Ana", ["Bartender"], db_path=db_path)["sheets"] == []
    ts.complete_line(rid, a["id"], ben[0]["lines"][1]["line_id"], employee_name="Ben", db_path=db_path,
                     now_local=_at("22:00"))
    with pytest.raises(ts.NotYourSheet):
        ts.complete_line(rid, a["id"], ben[0]["lines"][1]["line_id"], employee_name="Ana", db_path=db_path)
    ts.evaluate(rid, now_local=_at("23:59") + timedelta(minutes=45), db_path=db_path)
    found = {i["kind"]: i for i in issues.list_issues(rid, db_path=db_path)}
    assert "Ben" in found["task_missed"]["detail"] and "Ana" not in found["task_missed"]["detail"]
    assert found["task_sheet"]["title"].endswith("1 of 2 done (Ben)")


def test_the_cover_can_tick_before_anyone_reads_the_sheets_again(db_path):
    """The tick itself re-resolves when the sheet isn't the caller's — the
    cover's first action may be a tick from a stale screen."""
    rid = _rid(db_path)
    _sheet(rid, db_path)
    hid = _publish(db_path, rid, [("Ana", "Bartender", "4:00 PM", "11:00 PM")])
    (a,) = ts.staff_view(rid, "Ana", ["Bartender"], db_path=db_path)["sheets"]
    _cover(db_path, rid, hid, [("Ben", "Bartender", "4:00 PM", "11:00 PM")])
    out = ts.complete_line(rid, a["id"], a["lines"][0]["line_id"], employee_name="Ben", db_path=db_path,
                           now_local=_at("17:00"))
    assert out == {"late": False, "flagged": False}


def test_refresh_assignees_is_the_hook_for_a_cover_and_ignores_other_days(db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path)
    hid = _publish(db_path, rid, [("Ana", "Bartender", "4:00 PM", "11:00 PM")])
    ts.ensure_day(rid, TODAY, db_path=db_path)
    _cover(db_path, rid, hid, [("Ben", "Bartender", "4:00 PM", "11:00 PM")])
    assert ts.refresh_assignees(rid, TODAY + timedelta(days=2), db_path=db_path) == 0
    assert ts.refresh_assignees(rid, TODAY.isoformat(), db_path=db_path) == 1
    (a,) = ts.day_view(rid, TODAY, db_path=db_path)["sheets"]
    assert a["assignees"] == ["Ben"]
    assert ts.refresh_assignees(rid, TODAY, db_path=db_path) == 0                     # nothing left to move


def test_a_moved_shift_moves_its_due_times_but_not_its_lines(db_path):
    rid = _rid(db_path)
    s = _sheet(rid, db_path, kind="opening", lines=[{"label": "Stock ice", "due_offset_min": 30}])
    hid = _publish(db_path, rid, [("Ana", "Bartender", "10:00 AM", "4:00 PM")])
    ts.ensure_day(rid, TODAY, db_path=db_path)
    ts.add_line(rid, s["id"], {"label": "Polish the taps"}, db_path=db_path)          # not on today's
    _cover(db_path, rid, hid, [("Ben", "Bartender", "11:00 AM", "4:00 PM")])
    (a,) = ts.staff_view(rid, "Ben", ["Bartender"], db_path=db_path)["sheets"]
    assert [l["label"] for l in a["lines"]] == ["Stock ice"]
    assert a["lines"][0]["due_at"] == f"{TODAY}T11:30:00"


# ── PERF-08: a settled day reads no schedule; the tick answers with the sheet ──

def test_a_sheet_nobody_works_today_settles_the_day(db_path, monkeypatch):
    import labor
    rid = _rid(db_path)
    _sheet(rid, db_path, "Bartender", "opening", ["Stock ice"])
    _sheet(rid, db_path, "Server", "opening", ["Roll silverware"])
    hid = _publish(db_path, rid, [("Chidi", "Server", "11:00 AM", "5:00 PM")])       # no bartender today
    parsed = []
    real = labor.timed_shifts_from_csv
    monkeypatch.setattr(labor, "timed_shifts_from_csv", lambda text: parsed.append(1) or real(text))
    ts.staff_view(rid, "Chidi", ["Server"], db_path=db_path)
    first = len(parsed)
    for _ in range(3):
        ts.staff_view(rid, "Chidi", ["Server"], db_path=db_path)
    assert first == 1 and len(parsed) == first                                      # settled: no re-parse
    assert _count(db_path, "SELECT COUNT(*) FROM task_assignments WHERE status='none'") == 1
    view = ts.day_view(rid, TODAY, db_path=db_path)
    assert [a["job_code"] for a in view["sheets"]] == ["Server"]                    # 'none' is never shown
    # A bartender is added to today's live week: the bar sheet goes out.
    _cover(db_path, rid, hid, [("Chidi", "Server", "11:00 AM", "5:00 PM"), ("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    (bar,) = ts.staff_view(rid, "Dana", ["Bartender"], db_path=db_path)["sheets"]
    assert bar["assignees"] == ["Dana"] and bar["status"] == "open"


def test_the_tick_answers_with_the_sheet_and_photo_tokens_are_joined(client, db_path, monkeypatch):
    rid = _rid(db_path)
    _sheet(rid, db_path, kind="opening", lines=["Stock ice", {"label": "Photo of the bar", "proof": "photo"}])
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    _login(client, db_path, rid, "Dana", "Bartender", "dana")
    a = client.get("/staff/api/tasks").get_json()["sheets"][0]
    r = client.post("/staff/api/tasks/complete", json={"assignment_id": a["id"], "line_id": a["lines"][0]["line_id"],
                                                        "done": True}).get_json()
    assert r["ok"] and r["sheet"]["id"] == a["id"] and r["sheet"]["done"] == 1 and r["alert"] is None
    assert r["task_date"] == TODAY.isoformat() and r["sheet"]["lines"][0]["done"] is True
    p = client.post("/staff/api/tasks/photo", json={"assignment_id": a["id"], "line_id": a["lines"][1]["line_id"],
                                                    "image_b64": base64.b64encode(_png()).decode(), "mime": "image/png"})
    tok = p.get_json()["sheet"]["lines"][1]["photo"]
    assert p.status_code == 200 and tok
    # One read of the completions, the photo tokens joined in — no query per photo.
    seen = []
    real = models.get_conn

    def traced(*args, **kw):
        c = real(*args, **kw)
        c.set_trace_callback(seen.append)
        return c
    monkeypatch.setattr(models, "get_conn", traced)
    view = ts.day_view(rid, TODAY, db_path=db_path)
    assert view["sheets"][0]["lines"][1]["photo"] == tok
    assert not [q for q in seen if "FROM task_proof_media WHERE id" in q]


# ── LG-31 / SEC-13: authorise first, then store; multipart; a cap ──────────

def test_a_photo_against_someone_elses_sheet_stores_nothing(client, db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, kind="opening", lines=[{"label": "Photo of the bar", "proof": "photo"}])
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    a = ts.staff_view(rid, "Dana", ["Bartender"], db_path=db_path)["sheets"][0]
    _login(client, db_path, rid, "Chidi", "Server", "chidi")
    for _ in range(3):
        r = client.post("/staff/api/tasks/photo", json={"assignment_id": a["id"], "line_id": a["lines"][0]["line_id"],
                                                        "image_b64": base64.b64encode(_png()).decode()})
        assert r.status_code == 403
    assert _count(db_path, "SELECT COUNT(*) FROM task_proof_media") == 0


def test_a_photo_ticks_by_multipart_and_the_hourly_cap_holds(client, db_path, monkeypatch):
    rid = _rid(db_path)
    _sheet(rid, db_path, kind="opening", lines=[{"label": "Photo of the bar", "proof": "photo"},
                                                {"label": "Photo of the walk-in", "proof": "photo"}])
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    uid = _login(client, db_path, rid, "Dana", "Bartender", "dana")
    a = client.get("/staff/api/tasks").get_json()["sheets"][0]
    r = client.post("/staff/api/tasks/photo", content_type="multipart/form-data",
                    data={"assignment_id": str(a["id"]), "line_id": str(a["lines"][0]["line_id"]),
                          "file": (io.BytesIO(_png()), "bar.png", "image/png")})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["sheet"]["lines"][0]["photo"]
    assert _count(db_path, "SELECT COUNT(*) FROM task_proof_media WHERE uploaded_by_user_id=?", (uid,)) == 1
    monkeypatch.setattr(ts, "PHOTO_UPLOADS_PER_HOUR", 1)
    r = client.post("/staff/api/tasks/photo", content_type="multipart/form-data",
                    data={"assignment_id": str(a["id"]), "line_id": str(a["lines"][1]["line_id"]),
                          "photo": (io.BytesIO(_png()), "walkin.png", "image/png")})
    assert r.status_code == 400 and "photos in an hour" in r.get_json()["error"]
    assert _count(db_path, "SELECT COUNT(*) FROM task_proof_media") == 1
    empty = client.post("/staff/api/tasks/photo", json={"assignment_id": a["id"], "line_id": a["lines"][1]["line_id"]})
    assert empty.status_code == 400


# ── LG-30: only today's sheets tick; every open row is swept ───────────────

def test_an_old_open_sheet_cannot_be_ticked_and_is_swept_quietly(db_path, monkeypatch):
    rid = _rid(db_path)
    _sheet(rid, db_path, kind="opening", lines=["Stock ice", "Cut fruit"])
    old = TODAY - timedelta(days=5)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: old)
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")], day=old)
    ts.ensure_day(rid, old, db_path=db_path)
    (a,) = ts.day_view(rid, old, db_path=db_path)["sheets"]
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: TODAY)
    with pytest.raises(ts.TaskSheetError, match="another day"):
        ts.complete_line(rid, a["id"], a["lines"][0]["line_id"], employee_name="Dana", db_path=db_path)
    # A day either side still ticks (a closer after midnight).
    y = TODAY - timedelta(days=1)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: y)
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")], day=y)
    ts.ensure_day(rid, y, db_path=db_path)
    (b,) = ts.day_view(rid, y, db_path=db_path)["sheets"]
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: TODAY)
    ts.complete_line(rid, b["id"], b["lines"][0]["line_id"], employee_name="Dana", db_path=db_path)
    out = ts.evaluate(rid, now_local=_at("12:00"), db_path=db_path)
    assert out["closed"] == 2
    assert ts.day_view(rid, old, db_path=db_path)["sheets"][0]["status"] == "missed"
    titles = [i["title"] for i in issues.list_issues(rid, db_path=db_path) if i["kind"] == "task_sheet"]
    assert titles == ["Opening — Bartender: 1 of 2 done (Dana)"]                  # yesterday's; the old one is quiet


# ── COM-10 (H13): an out-of-range critical reading reaches the manager now ──

def _routed_manager(db_path, rid):
    conn = models.get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) "
                       "VALUES (?, 'Erik', '+15555550100', 1)", (rid,)).lastrowid
    conn.commit()
    conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)


def test_a_critical_reading_out_of_range_opens_a_high_issue_and_says_tell_your_manager(client, db_path, _redirect):
    rid = _rid(db_path)
    _routed_manager(db_path, rid)
    _sheet(rid, db_path, kind="opening", lines=[
        {"label": "Walk-in temperature", "proof": "number", "proof_label": "Walk-in °F", "min_value": 33,
         "max_value": 41, "critical": True},
        {"label": "Bar cooler", "proof": "number", "min_value": 33, "max_value": 41}])
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    _login(client, db_path, rid, "Dana", "Bartender", "dana")
    a = client.get("/staff/api/tasks").get_json()["sheets"][0]
    walkin, cooler = (l["line_id"] for l in a["lines"])
    r = client.post("/staff/api/tasks/complete", json={"assignment_id": a["id"], "line_id": walkin, "value": "46"}).get_json()
    assert r["flagged"] is True and r["alert"]["title"] == "Tell your manager now"
    assert r["alert"]["message"].startswith("Tell your manager now: Walk-in °F read 46, outside 33–41")
    assert r["alert"]["manager_alerted"] is True
    (issue,) = [i for i in issues.list_issues(rid, db_path=db_path) if i["kind"] == "task_flag"]
    assert issue["severity"] == "high" and issue["source_key"] == f"taskflag:{a['id']}:{walkin}"
    assert issue["assignee_name"] == "Erik" and _redirect == [issue["id"]]          # texted once
    # Read again, still out: the same issue, no second text.
    client.post("/staff/api/tasks/complete", json={"assignment_id": a["id"], "line_id": walkin, "value": "47"})
    assert len([i for i in issues.list_issues(rid, db_path=db_path) if i["kind"] == "task_flag"]) == 1
    assert _redirect == [issue["id"]]
    # A flagged reading on a line that isn't critical stays a flag.
    r = client.post("/staff/api/tasks/complete", json={"assignment_id": a["id"], "line_id": cooler, "value": "50"}).get_json()
    assert r["flagged"] is True and r["alert"] is None


# ── COM-16 (M14): last night's note for the opener ─────────────────────────

def test_last_nights_closing_note_reaches_todays_opening_manager(client, db_path, monkeypatch):
    rid = _rid(db_path)
    _sheet(rid, db_path, "Manager", "closing", ["Drop the safe"])
    _sheet(rid, db_path, "Manager", "opening", ["Walk the floor"])
    _sheet(rid, db_path, "Bartender", "opening", ["Stock ice"])
    y = TODAY - timedelta(days=1)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: y)
    _publish(db_path, rid, [("Ben", "Manager", "4:00 PM", "11:00 PM")], day=y)
    ts.ensure_day(rid, y, db_path=db_path)
    ts.sign_off(rid, "closing", "Ben", note="Walk-in door sticks — call the repair line", db_path=db_path)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: TODAY)
    _publish(db_path, rid, [("Erik", "Manager", "9:00 AM", "5:00 PM"), ("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    _login(client, db_path, rid, "Erik", "Manager", "erik_m")
    note = client.get("/staff/api/tasks").get_json()["last_night_note"]
    assert note["note"] == "Walk-in door sticks — call the repair line" and note["signed_by"] == "Ben"
    assert note["task_date"] == y.isoformat() and note["date_label"] == "9/30/26"
    assert ts.staff_view(rid, "Dana", ["Bartender"], db_path=db_path)["last_night_note"] is None


# ── M13: server hygiene ────────────────────────────────────────────────────

def test_staff_json_is_no_store_and_proof_photos_private_no_store(client, db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, kind="opening", lines=[{"label": "Photo of the bar", "proof": "photo"}])
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    _login(client, db_path, rid, "Dana", "Bartender", "dana")
    got = client.get("/staff/api/tasks")
    assert got.headers.get("Cache-Control") == "no-store"
    a = got.get_json()["sheets"][0]
    tok = client.post("/staff/api/tasks/photo", json={"assignment_id": a["id"], "line_id": a["lines"][0]["line_id"],
                                                      "image_b64": base64.b64encode(_png()).decode()}
                      ).get_json()["sheet"]["lines"][0]["photo"]
    assert client.get(f"/staff/api/tasks/photo/{tok}").headers.get("Cache-Control") == "private, no-store"


def test_the_flat_tasks_list_is_left_out_only_for_an_app_that_says_it_reads_sheets(client, db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, kind="opening", lines=["Stock ice"])
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    _login(client, db_path, rid, "Dana", "Bartender", "dana")
    assert [t["label"] for t in client.get("/staff/api/tasks").get_json()["tasks"]] == ["Stock ice"]
    new = client.get("/staff/api/tasks", headers={"X-Staff-Tasks-Version": "2"}).get_json()
    assert "tasks" not in new and new["sheets"][0]["lines"][0]["label"] == "Stock ice"
    assert "tasks" in client.get("/staff/api/tasks", headers={"X-Staff-Tasks-Version": "x"}).get_json()


def test_task_paths_never_load_the_whole_schedule_row(db_path, monkeypatch):
    def whole_row(*a, **k):
        raise AssertionError("task sheets read the whole schedule row")
    monkeypatch.setattr(models, "get_schedule_history_detail", whole_row)
    rid = _rid(db_path)
    _sheet(rid, db_path, kind="opening", lines=["Stock ice"])
    _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    (a,) = ts.staff_view(rid, "Dana", ["Bartender"], db_path=db_path)["sheets"]
    assert a["assignees"] == ["Dana"]


def test_the_published_week_reader_reads_three_fields_for_one_tenant(db_path):
    rid, other = _rid(db_path), _rid(db_path, "Elsewhere")
    hid = _publish(db_path, rid, [("Dana", "Bartender", "10:00 AM", "4:00 PM")])
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET review_json=?, summary_json='not json' WHERE id=?",
                 (json.dumps({"stations": {"assigned": [{"employee": "Dana"}]}, "other": 1}), hid))
    conn.commit()
    conn.close()
    week = models.get_published_week_csv(hid, rid)
    assert set(week) == {"id", "week_start", "week_end", "schedule_csv", "published_at", "edited_at"}
    assert "Dana" in week["schedule_csv"]
    assert models.get_published_week_csv(hid, rid, with_stations=True)["stations"] == {"assigned": [{"employee": "Dana"}]}
    assert models.get_published_week_csv(hid, other) is None
