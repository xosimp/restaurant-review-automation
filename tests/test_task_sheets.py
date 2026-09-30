"""Task sheets by job code and shift (task_sheets.py, docs/plans/TASK_SHEETS_PLAN.md):
who owes which sheet comes from the PUBLISHED schedule, a line added later
never makes this morning's opener late, owners read and never tick, and
misses reach someone."""
import json
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import auth
import client_api
import issues
import mobile_api
import models
import task_sheets as ts
from auth import create_session, create_staff_session, create_user, init_auth
from models import Restaurant, create_restaurant

TODAY = date.today()
CSV_HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, auth, client_api, mobile_api, issues):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    # "Today" is the calendar day in these tests, whatever the clock says.
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: TODAY)


def _rid(db_path, name="Simple EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _publish(db_path, rid, rows, day=TODAY):
    """A published week covering `day`: rows are (employee, role, start, end)."""
    ws = day - timedelta(days=day.weekday())
    csv = CSV_HEAD + "".join(f"{day.isoformat()},{day.strftime('%A')},{e},{r},{a},{b},8,\n" for e, r, a, b in rows)
    conn = models.get_conn(db_path)
    models._ensure_history_columns(conn)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                 "VALUES (?,?,?,?,datetime('now'))", (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), csv))
    conn.commit()
    conn.close()


def _sheet(rid, db_path, job="Bartender", kind="opening", lines=("Stock the ice well", "Cut fruit")):
    s = ts.create_sheet(rid, job, kind, db_path=db_path)
    for l in lines:
        ts.add_line(rid, s["id"], {"label": l} if isinstance(l, str) else l, db_path=db_path)
    return ts.get_sheet(rid, s["id"], db_path=db_path)


def _at(hhmm, day=TODAY):
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())


# ── the owner's sheets ──────────────────────────────────────────────────────

def test_the_flat_lists_carry_over_as_all_day_sheets_once(db_path):
    rid = _rid(db_path)
    for label in ("Punch in", "Grab 4 rags"):
        models.add_task_template(rid, "Bartender AM", label, db_path=db_path)
    conn = models.get_conn(db_path)
    assert ts.migrate_templates(conn) == 1
    assert ts.migrate_templates(conn) == 0          # a restaurant with sheets is left alone
    conn.commit()
    conn.close()
    (sheet,) = ts.list_sheets(rid, db_path=db_path)
    assert (sheet["job_code"], sheet["shift_kind"], sheet["carried_over"]) == ("Bartender AM", "any", True)
    assert [l["label"] for l in sheet["lines"]] == ["Punch in", "Grab 4 rags"]


def test_lines_validate_reorder_and_flag_a_near_duplicate(db_path):
    rid = _rid(db_path)
    s = _sheet(rid, db_path, lines=("Wipe down the line", "Count the drawer"))
    with pytest.raises(ts.TaskSheetError):
        ts.add_line(rid, s["id"], {"label": "  "}, db_path=db_path)
    with pytest.raises(ts.TaskSheetError):
        ts.add_line(rid, s["id"], {"label": "Walk-in", "proof": "number", "min_value": 41, "max_value": 33}, db_path=db_path)
    out = ts.add_line(rid, s["id"], {"label": "Wipe the line down"}, db_path=db_path)
    assert [x["label"] for x in out["similar"]] == ["Wipe down the line"]
    ids = [l["id"] for l in out["sheet"]["lines"]]
    again = ts.reorder_lines(rid, s["id"], list(reversed(ids)), db_path=db_path)
    assert [l["id"] for l in again["lines"]] == list(reversed(ids))
    with pytest.raises(ts.TaskSheetError):
        ts.reorder_lines(rid, s["id"], ids[:1], db_path=db_path)
    assert again["version"] > s["version"]


def test_another_restaurants_sheet_is_refused(db_path):
    a, b = _rid(db_path, "A"), _rid(db_path, "B")
    s = _sheet(a, db_path)
    with pytest.raises(ts.TaskSheetError):
        ts.add_line(b, s["id"], {"label": "x y"}, db_path=db_path)
    with pytest.raises(ts.TaskSheetError):
        ts.update_line(b, s["lines"][0]["id"], {"label": "hijack"}, db_path=db_path)


# ── who owes it today ───────────────────────────────────────────────────────

def test_the_opener_and_the_closer_come_from_the_published_schedule(db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, "Bartender", "opening", [{"label": "Stock ice", "due_offset_min": 30}])
    _sheet(rid, db_path, "Bartender", "closing", [{"label": "Count the drawer", "due_offset_min": 15, "critical": True}])
    _publish(db_path, rid, [("Dana Reyes", "Bartender", "10:00 AM", "4:00 PM"),
                            ("Bo Park", "Bartender", "4:00 PM", "12:30 AM"),
                            ("Chidi O.", "Server", "11:00 AM", "5:00 PM")])
    assert ts.ensure_day(rid, TODAY, db_path=db_path) == 2
    assert ts.ensure_day(rid, TODAY, db_path=db_path) == 0          # idempotent
    view = ts.day_view(rid, TODAY, db_path=db_path)
    opening = next(a for a in view["sheets"] if a["shift_kind"] == "opening")
    closing = next(a for a in view["sheets"] if a["shift_kind"] == "closing")
    assert opening["assignees"] == ["Dana Reyes"] and opening["lines"][0]["due_at"] == f"{TODAY}T10:30:00"
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    assert closing["assignees"] == ["Bo Park"] and closing["lines"][0]["due_at"] == f"{tomorrow}T00:15:00"
    assert view["schedule_published"] is True and view["summary"]["lines"] == 2


def test_no_published_schedule_issues_the_sheet_unassigned(db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path)
    ts.ensure_day(rid, TODAY, db_path=db_path)
    (a,) = ts.day_view(rid, TODAY, db_path=db_path)["sheets"]
    assert a["unassigned"] and a["assignees"] == []
    mine = ts.staff_view(rid, "Anyone", ["bartender"], db_path=db_path)
    assert [s["id"] for s in mine["sheets"]] == [a["id"]]          # whoever works that code sees it
    assert ts.staff_view(rid, "Anyone", ["Server"], db_path=db_path)["sheets"] == []


def test_a_line_added_after_the_sheet_went_out_is_not_on_todays(db_path):
    rid = _rid(db_path)
    s = _sheet(rid, db_path)
    _publish(db_path, rid, [("Dana Reyes", "Bartender", "10:00 AM", "4:00 PM")])
    ts.ensure_day(rid, TODAY, db_path=db_path)
    ts.add_line(rid, s["id"], {"label": "Polish the taps"}, db_path=db_path)
    (a,) = ts.day_view(rid, TODAY, db_path=db_path)["sheets"]
    assert [l["label"] for l in a["lines"]] == ["Stock the ice well", "Cut fruit"]


# ── ticking ─────────────────────────────────────────────────────────────────

def test_only_the_assignee_ticks_and_proof_late_and_range_are_kept(db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, lines=[{"label": "Stock ice", "due_offset_min": 30},
                                {"label": "Walk-in temp", "proof": "number", "proof_label": "Walk-in °F",
                                 "min_value": 33, "max_value": 41},
                                {"label": "Note the 86 list", "proof": "note"}])
    _publish(db_path, rid, [("Dana Reyes", "Bartender", "10:00 AM", "4:00 PM"),
                            ("Bo Park", "Bartender", "4:00 PM", "11:00 PM")])
    ts.ensure_day(rid, TODAY, db_path=db_path)
    (a,) = ts.staff_view(rid, "Dana Reyes", ["Bartender"], db_path=db_path)["sheets"]
    stock, temp, note = (l["line_id"] for l in a["lines"])
    with pytest.raises(ts.TaskSheetError, match="isn't yours"):
        ts.complete_line(rid, a["id"], stock, employee_name="Bo Park", job_roles=["Bartender"], db_path=db_path)
    assert ts.complete_line(rid, a["id"], stock, employee_name="Dana Reyes", db_path=db_path,
                            now_local=_at("10:45")) == {"late": True, "flagged": False}
    with pytest.raises(ts.TaskSheetError, match="Walk-in"):
        ts.complete_line(rid, a["id"], temp, employee_name="Dana Reyes", db_path=db_path, now_local=_at("11:00"))
    assert ts.complete_line(rid, a["id"], temp, employee_name="Dana Reyes", value="46", db_path=db_path,
                            now_local=_at("11:00"))["flagged"] is True
    with pytest.raises(ts.TaskSheetError, match="note"):
        ts.complete_line(rid, a["id"], note, employee_name="Dana Reyes", value=" ", db_path=db_path)
    ts.complete_line(rid, a["id"], note, employee_name="Dana Reyes", value="Out of brisket", db_path=db_path)
    ts.complete_line(rid, a["id"], note, done=False, employee_name="Dana Reyes", db_path=db_path)
    (a,) = ts.day_view(rid, TODAY, db_path=db_path)["sheets"][:1]
    by = {l["label"]: l for l in a["lines"]}
    assert by["Stock ice"]["late"] and by["Stock ice"]["completed_by"] == "Dana Reyes"
    assert by["Walk-in temp"]["flagged"] and by["Walk-in temp"]["proof_value"] == "46"
    assert by["Note the 86 list"]["done"] is False                 # un-ticked: a new row, history kept
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM task_line_completions").fetchone()[0] == 4
    conn.close()


def test_a_manager_sees_the_floor_and_an_employee_only_their_own(db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, "Bartender", "opening")
    _sheet(rid, db_path, "Manager", "opening", ["Walk the floor", "Check the schedule"])
    _publish(db_path, rid, [("Dana Reyes", "Bartender", "10:00 AM", "4:00 PM"),
                            ("Erik M.", "Manager", "9:00 AM", "5:00 PM")])
    dana = ts.staff_view(rid, "Dana Reyes", ["Bartender"], db_path=db_path)
    erik = ts.staff_view(rid, "Erik M.", ["Manager"], db_path=db_path)
    assert (len(dana["sheets"]), dana["manager"], dana["floor"]) == (1, False, [])
    assert erik["manager"] and len(erik["sheets"]) == 1 and len(erik["floor"]) == 1
    out = ts.sign_off(rid, "opening", "Erik M.", db_path=db_path)
    assert out["summary"]["sheets"][0]["total"] == 2
    with pytest.raises(ts.TaskSheetError, match="already signed"):
        ts.sign_off(rid, "opening", "Erik M.", db_path=db_path)


# ── misses reach someone ────────────────────────────────────────────────────

def test_a_critical_line_past_due_and_an_unfinished_shift_open_issues(db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, "Bartender", "opening", [{"label": "Walk-in temps logged", "due_offset_min": 30, "critical": True},
                                                 {"label": "Cut fruit"}])
    _publish(db_path, rid, [("Dana Reyes", "Bartender", "10:00 AM", "4:00 PM")])
    assert ts.evaluate(rid, now_local=_at("10:20"), db_path=db_path)["critical"] == 0      # not yet due
    assert ts.evaluate(rid, now_local=_at("10:45"), db_path=db_path)["critical"] == 1
    assert ts.evaluate(rid, now_local=_at("11:00"), db_path=db_path)["critical"] == 0      # once a line a day
    assert ts.evaluate(rid, now_local=_at("16:40"), db_path=db_path)["closed"] == 1
    kinds = {i["kind"]: i for i in issues.list_issues(rid, db_path=db_path)}
    assert kinds["task_missed"]["severity"] == "high" and "Walk-in temps" in kinds["task_missed"]["title"]
    assert kinds["task_sheet"]["title"].endswith("0 of 2 done (Dana Reyes)")
    (a,) = ts.day_view(rid, TODAY, db_path=db_path)["sheets"]
    assert a["status"] == "missed"
    with pytest.raises(ts.TaskSheetError, match="closed"):
        ts.complete_line(rid, a["id"], a["lines"][1]["line_id"], employee_name="Dana Reyes", db_path=db_path)


def test_three_misses_in_fourteen_days_is_a_pattern_seen_once(db_path, monkeypatch):
    rid = _rid(db_path)
    s = _sheet(rid, db_path, "Bartender", "closing", ["Count the safe", "Lock the back door"])
    for back in (6, 4, 2):
        day = TODAY - timedelta(days=back)
        monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None, d=day: d)
        _publish(db_path, rid, [("Jordan", "Bartender", "4:00 PM", "11:00 PM")], day=day)
        ts.ensure_day(rid, day, db_path=db_path)
        a = ts.day_view(rid, day, db_path=db_path)["sheets"][0]
        ts.complete_line(rid, a["id"], a["lines"][1]["line_id"], employee_name="Jordan", db_path=db_path,
                         now_local=_at("22:00", day))
        ts.evaluate(rid, now_local=_at("23:59", day) + timedelta(minutes=45), db_path=db_path)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: TODAY)
    pats = [i for i in issues.list_issues(rid, db_path=db_path) if i["kind"] == "task_pattern"]
    assert len(pats) == 1 and pats[0]["title"].startswith("Jordan has missed “Count the safe” 3 times")
    rep = ts.report(rid, days=14, db_path=db_path)
    jordan = next(p for p in rep["by_person"] if p["name"] == "Jordan")
    assert (jordan["sheets"], jordan["completion_pct"], jordan["enough"]) == (3, 50, True)
    assert ts.yesterday_line(rid, today=TODAY - timedelta(days=1), db_path=db_path)["text"].startswith(
        "Yesterday's sheets left unfinished")
    del s


def test_the_report_puts_the_managers_side_by_side(db_path, monkeypatch):
    rid = _rid(db_path)
    _sheet(rid, db_path, "Manager", "opening", ["Walk the floor", "Count the drawer"])
    _sheet(rid, db_path, "Manager", "closing", ["Lock up", "Drop the safe"])
    for back in (5, 4, 3):
        day = TODAY - timedelta(days=back)
        monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None, d=day: d)
        _publish(db_path, rid, [("Ana", "Manager", "9:00 AM", "3:00 PM"), ("Ben", "Manager", "3:00 PM", "11:00 PM")], day=day)
        ts.ensure_day(rid, day, db_path=db_path)
        for a in ts.day_view(rid, day, db_path=db_path)["sheets"]:
            who = a["assignees"][0]
            for l in a["lines"][: (2 if who == "Ana" else 1)]:
                ts.complete_line(rid, a["id"], l["line_id"], employee_name=who, db_path=db_path, now_local=_at("12:00", day))
        ts.evaluate(rid, now_local=_at("23:59", day) + timedelta(minutes=45), db_path=db_path)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: TODAY)
    rep = ts.report(rid, days=14, db_path=db_path)
    assert [(m["name"], m["completion_pct"]) for m in rep["managers"]] == [("Ben", 50), ("Ana", 100)]


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(db_path):
    from staff_routes import staff_bp
    app = Flask(__name__, template_folder="../templates")
    for bp in (staff_bp, client_api.client_bp, mobile_api.mobile_bp):
        app.register_blueprint(bp)
    return app.test_client()


def test_staff_tick_their_own_line_through_the_portal_and_an_owner_reads_the_day(client, db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path)
    _publish(db_path, rid, [("Dana Reyes", "Bartender", "10:00 AM", "4:00 PM")])
    uid = create_user(rid, "dana", "dana@x.test", "unused", db_path=db_path)
    auth.upsert_membership(uid, rid, "employee", employee_name="Dana Reyes", db_path=db_path)
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    d = client.get("/staff/api/tasks").get_json()
    assert d["ok"] and len(d["sheets"]) == 1 and len(d["tasks"]) == 2      # the old flat list, still there
    a = d["sheets"][0]
    r = client.post("/staff/api/tasks/complete", json={"assignment_id": a["id"], "line_id": a["lines"][0]["line_id"], "done": True})
    assert r.status_code == 200
    # An older app ticks by the line alone (template_id).
    assert client.post("/staff/api/tasks/complete", json={"template_id": a["lines"][1]["line_id"], "done": True}).status_code == 200
    owner = create_user(rid, "erik", "erik@x.test", "owner-pw", db_path=db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    day = client.get("/api/task-sheets/day").get_json()
    assert day["ok"] and day["summary"]["done"] == 2 and day["sheets"][0]["lines"][0]["completed_by"] == "Dana Reyes"
    listed = client.get("/api/task-sheets").get_json()
    assert listed["ok"] and listed["sheets"][0]["job_code"] == "Bartender"


# ── proof photos, sign-off, the starter draft, Ask, the brief, the person ──

def _png():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 50)).save(buf, format="PNG")
    return buf.getvalue()


def _staff_client(client, db_path, rid, name="Dana Reyes", job="Bartender", username="dana"):
    uid = create_user(rid, username, f"{username}@x.test", "unused", db_path=db_path)
    auth.upsert_membership(uid, rid, "employee", employee_name=name, job_role=job, db_path=db_path)
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))
    return uid


def test_a_photo_line_ticks_with_its_photo_and_only_this_restaurant_sees_it(client, db_path):
    import base64
    rid = _rid(db_path)
    _sheet(rid, db_path, lines=[{"label": "Photo of the clean bar top", "proof": "photo"}])
    _publish(db_path, rid, [("Dana Reyes", "Bartender", "10:00 AM", "4:00 PM")])
    _staff_client(client, db_path, rid)
    a = client.get("/staff/api/tasks").get_json()["sheets"][0]
    line = a["lines"][0]["line_id"]
    no = client.post("/staff/api/tasks/complete", json={"assignment_id": a["id"], "line_id": line, "done": True})
    assert no.status_code == 400 and "photo" in no.get_json()["error"]
    r = client.post("/staff/api/tasks/photo", json={"assignment_id": a["id"], "line_id": line,
                                                    "image_b64": base64.b64encode(_png()).decode(), "mime": "image/png"})
    assert r.status_code == 200, r.get_json()
    tok = ts.day_view(rid, TODAY, db_path=db_path)["sheets"][0]["lines"][0]["photo"]
    assert tok and client.get(f"/staff/api/tasks/photo/{tok}").status_code == 200
    other = _rid(db_path, "Elsewhere")
    assert ts.get_photo(other, tok, db_path=db_path) is None


def test_only_a_manager_signs_a_shift_off_through_the_portal(client, db_path):
    rid = _rid(db_path)
    _sheet(rid, db_path, "Bartender", "closing")
    _sheet(rid, db_path, "Manager", "closing", ["Drop the safe", "Lock up"])
    _publish(db_path, rid, [("Dana Reyes", "Bartender", "4:00 PM", "11:00 PM"), ("Erik M.", "Manager", "4:00 PM", "11:00 PM")])
    _staff_client(client, db_path, rid)
    assert client.post("/staff/api/tasks/signoff", json={"shift_kind": "closing"}).status_code == 403
    _staff_client(client, db_path, rid, name="Erik M.", job="Manager", username="erik_m")
    d = client.get("/staff/api/tasks").get_json()
    assert d["manager"] and [s["job_code"] for s in d["floor"]] == ["Bartender"]
    r = client.post("/staff/api/tasks/signoff", json={"shift_kind": "closing", "note": "All good"})
    assert r.status_code == 200 and ts.day_view(rid, TODAY, db_path=db_path)["signoffs"][0]["signed_by"] == "Erik M."


def test_the_starter_draft_is_validated_deduplicated_and_never_saved(db_path, monkeypatch):
    rid = _rid(db_path)
    s = _sheet(rid, db_path, lines=("Cut fruit",))

    class _Msg:
        stop_reason = "end_turn"
    import ai_utils
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: _Msg())
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "extract_text", lambda m: json.dumps([
        {"label": "Cut fruit", "proof": "none"},
        {"label": "Log the bar cooler temperature", "proof": "number", "proof_label": "Cooler °F", "critical": True},
        {"label": "", "proof": "none"},
        {"label": "Restock straws", "proof": "sparkles"}]))
    lines = ts.starter_lines(rid, s["job_code"], s["shift_kind"], existing=["Cut fruit"])
    assert [l["label"] for l in lines] == ["Log the bar cooler temperature"]
    assert lines[0]["critical"] is True and lines[0]["proof"] == "number"
    assert len(ts.get_sheet(rid, s["id"], db_path=db_path)["lines"]) == 1      # nothing saved


def test_ask_reads_the_day_and_the_brief_and_person_record_carry_misses(db_path, monkeypatch):
    import ask_cavnar_tools as tools
    import people
    rid = _rid(db_path)
    _sheet(rid, db_path, "Bartender", "closing", ["Count the safe", "Lock the back door"])
    y = TODAY - timedelta(days=1)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: y)
    _publish(db_path, rid, [("Jordan", "Bartender", "4:00 PM", "11:00 PM")], day=y)
    ts.ensure_day(rid, y, db_path=db_path)
    a = ts.day_view(rid, y, db_path=db_path)["sheets"][0]
    ts.complete_line(rid, a["id"], a["lines"][1]["line_id"], employee_name="Jordan", db_path=db_path, now_local=_at("22:00", y))
    ts.evaluate(rid, now_local=_at("23:59", y) + timedelta(minutes=45), db_path=db_path)
    monkeypatch.setattr(ts, "business_day", lambda rid, restaurant=None, now_local=None: TODAY)
    out = json.loads(tools.run_read_tool("read_task_sheets", rid, {"date": y.isoformat()}, restaurant=models.get_restaurant(rid)))
    assert out["sheets"][0]["skipped"] == ["Count the safe"] and out["sheets"][0]["assignees"] == ["Jordan"]
    assert "Count the safe" not in ts.yesterday_line(rid, today=TODAY, db_path=db_path)["text"]
    assert ts.yesterday_line(rid, today=TODAY, db_path=db_path)["text"].endswith("1 of 2 done (Jordan).")
    rec = ts.person_record(rid, "Jordan", db_path=db_path)
    assert rec["recent_misses"][0]["label"] == "Count the safe"
    del people


def test_an_untouched_unassigned_sheet_closes_without_a_nightly_issue(db_path):
    """No schedule published: the sheet goes out unassigned and nobody owned
    it. Left untouched it closes as missed on the day view, not as an issue
    on Home every night."""
    rid = _rid(db_path)
    _sheet(rid, db_path, "Server AM", "any", ["Roll silverware", "Wipe menus"])
    ts.ensure_day(rid, TODAY, db_path=db_path)
    (a,) = ts.day_view(rid, TODAY, db_path=db_path)["sheets"]
    assert a["unassigned"] and a["shift_start"] is None        # no hours set either
    assert ts.evaluate(rid, now_local=_at("23:59") + timedelta(minutes=45), db_path=db_path)["closed"] == 1
    assert [i for i in issues.list_issues(rid, db_path=db_path) if i["kind"] == "task_sheet"] == []
    assert ts.day_view(rid, TODAY, db_path=db_path)["sheets"][0]["status"] == "missed"
