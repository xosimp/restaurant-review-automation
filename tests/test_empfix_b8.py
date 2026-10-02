"""Employee audit fix round B8 (10/2/26): availability by hours and dates
(M5 — MISS-8, WF-32, LG-35, PERF-05) and FOH floor sections per shift (V12).

Clocks are pinned: every save and read here passes `today`, and the weeks
are fixed dates in October-December 2026.
"""
import json
from datetime import date

import pytest
from flask import Flask

import models
import schedule_rules as sr
import staff_settings as ss
import time_off
from models import Restaurant, create_restaurant

TODAY = date(2026, 10, 2)                      # a Friday
WEEK_OCT5 = ["2026-10-0%d" % d for d in range(5, 10)] + ["2026-10-10", "2026-10-11"]   # Mon 10/5 .. Sun 10/11
WEEK_DEC21 = ["2026-12-%d" % d for d in range(21, 28)]                                 # Mon 12/21 .. Sun 12/27
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture
def env(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, ss, time_off):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    told = []
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: told.append((a, k)) or 1)
    rid = create_restaurant(Restaurant(name="Maple & Rye", owner_email="o@x.test"), db_path=db_path)
    return {"rid": rid, "db": db_path, "told": told}


def _row(env, name="Ben"):
    return models.staff_availability_for(env["rid"], name, db_path=env["db"])


def _save(env, name="Ben", **kw):
    kw.setdefault("today", TODAY)
    if "expected" in kw:
        exp = kw.pop("expected")
    else:
        exp = ss.own_availability(env["rid"], name, db_path=env["db"], today=TODAY)["updated_at"]
    return ss.save_own_availability(env["rid"], name, exp, db_path=env["db"], **kw)


def _week(**days):
    return [dict({"day": d}, **days.get(d, {"status": "any"})) for d in DAYS]


def _publish(env, rows, week_start="2026-10-05", week_end="2026-10-11"):
    conn = models.get_conn(env["db"])
    try:
        models._ensure_history_columns(conn)
        conn.execute("INSERT INTO schedule_history (restaurant_id, generated_at, week_start, week_end, schedule_csv, "
                     "published_at) VALUES (?, datetime('now'), ?, ?, ?, datetime('now'))",
                     (env["rid"], week_start, week_end, HEAD + "\n".join(rows)))
        conn.commit()
    finally:
        conn.close()


# ── M5: one representation, read as a hard constraint ──────────────────────

def test_a_window_with_an_end_date_is_the_owners_time_window_and_holds_only_until_then(env):
    models.add_manual_team_member(env["rid"], "Ben", "Server", db_path=env["db"])      # on the roster
    res = _save(env, week=_week(Tuesday={"status": "window", "earliest": "17:00", "until": "2026-12-15"}))
    assert res["ok"], res
    # The owner's roster reads the same window the employee set: one store.
    tw = ss.for_name(env["rid"], "ben", db_path=env["db"])["time_windows"]
    assert tw["Tuesday"]["earliest"] == "5:00pm" and tw["Tuesday"]["until"] == "2026-12-15"
    c = sr.build_constraints(env["rid"], WEEK_OCT5, DAYS, db_path=env["db"])
    ok, why = c.window_ok("Ben", "2026-10-06", "4:00pm", "10:00pm")
    assert not ok and "not before 5:00pm" in why
    assert c.window_ok("Ben", "2026-10-06", "5:00pm", "10:00pm")[0]
    after = sr.build_constraints(env["rid"], WEEK_DEC21, DAYS, db_path=env["db"])
    assert after.window_ok("Ben", "2026-12-22", "4:00pm", "10:00pm")[0]     # past 12/15: gone


def test_a_weekday_off_until_a_date_blocks_only_the_dates_inside_it(env):
    res = _save(env, week=_week(Friday={"status": "off", "until": "2026-10-31"}, Monday={"status": "off"}))
    assert res["ok"], res
    c = sr.build_constraints(env["rid"], WEEK_OCT5, DAYS, db_path=env["db"])
    assert not c.can_work("Ben", "2026-10-09")[0]          # Friday 10/9: inside
    assert not c.can_work("Ben", "2026-10-05")[0]          # Monday: for good
    nov = ["2026-11-0%d" % d for d in range(2, 9)]
    c2 = sr.build_constraints(env["rid"], nov, DAYS, db_path=env["db"])
    assert c2.can_work("Ben", "2026-11-06")[0]             # Friday 11/6: after it ended
    assert not c2.can_work("Ben", "2026-11-02")[0]
    # Every week-aware reader agrees: the swap index, the prompt, the top-up.
    assert models.get_unavailability_map(env["rid"], db_path=env["db"], week_dates=nov)["Ben"] == {"Monday"}
    assert models.get_unavailability_map(env["rid"], db_path=env["db"], week_dates=WEEK_OCT5)["Ben"] == {"Monday", "Friday"}


def test_the_get_answers_the_week_shape_and_the_version(env):
    empty = ss.own_availability(env["rid"], "Ben", db_path=env["db"], today=TODAY)
    assert empty["updated_at"] is None and [d["status"] for d in empty["week"]] == ["any"] * 7
    _save(env, week=_week(Tuesday={"status": "window", "earliest": "5:00pm", "latest": "11:00pm"},
                          Friday={"status": "off", "from": "2026-10-10", "until": "2026-12-15"}), notes="school")
    got = ss.own_availability(env["rid"], "Ben", db_path=env["db"], today=TODAY)
    tue = got["week"][1]
    fri = got["week"][4]
    assert tue == {"day": "Tuesday", "status": "window", "earliest": "17:00", "latest": "23:00", "from": None, "until": None}
    assert fri == {"day": "Friday", "status": "off", "earliest": None, "latest": None,
                   "from": "2026-10-10", "until": "2026-12-15"}
    assert got["unavailable_days"] == ["Friday"] and got["notes"] == "school" and got["updated_at"]
    # Once its dates have passed the entry reads as "any".
    later = ss.own_availability(env["rid"], "Ben", db_path=env["db"], today=date(2026, 12, 16))
    assert later["week"][4]["status"] == "any" and later["unavailable_days"] == []


# ── PERF-05: the version, and a failed load never saves an empty week ───────

def test_a_save_on_a_stale_version_is_a_409_with_the_record_as_it_is(env):
    first = _save(env, week=_week(Monday={"status": "off"}), notes="Mon/Tue school")
    assert first["ok"]
    # The app's load failed (nothing loaded: version None) and it saves Sunday.
    stale = _save(env, expected=None, week=_week(Sunday={"status": "off"}), notes="")
    assert stale["status"] == 409 and stale["stale"]
    assert stale["availability"]["unavailable_days"] == ["Monday"]
    assert _row(env)["notes"] == "Mon/Tue school"                      # untouched
    # The /s/ link saved in between: the app's older version is refused too.
    v1 = first["updated_at"]
    _save(env, unavailable_days=["Monday", "Tuesday"])
    again = _save(env, expected=v1, unavailable_days=["Wednesday"])
    assert again["status"] == 409


def test_no_version_at_all_is_refused_and_two_quick_saves_never_share_one(env):
    gone = _save(env, expected=ss.MISSING, unavailable_days=["Monday"])
    assert gone["status"] == 409 and "Reload" in gone["error"]
    a = _save(env, unavailable_days=["Monday"])
    b = _save(env, expected=a["updated_at"], unavailable_days=["Tuesday"])
    assert b["ok"] and b["updated_at"] != a["updated_at"]


def test_an_owner_change_to_the_windows_moves_the_version_and_keeps_the_dates(env):
    res = _save(env, week=_week(Tuesday={"status": "window", "earliest": "17:00", "until": "2026-12-15"}))
    # The owner's iOS roster sends {earliest, latest} only.
    ss.upsert(env["rid"], "Ben", time_windows={"Tuesday": {"earliest": "5:00pm", "latest": ""},
                                               "Thursday": {"earliest": "10:00am", "latest": ""}},
              db_path=env["db"])
    tw = ss.for_name(env["rid"], "Ben", db_path=env["db"])["time_windows"]
    assert tw["Tuesday"]["until"] == "2026-12-15"                       # not turned into "for good"
    stale = _save(env, expected=res["updated_at"], unavailable_days=["Monday"])
    assert stale["status"] == 409                                       # the app can't undo the owner's edit


def test_the_link_and_older_apps_change_only_the_days_off(env):
    _save(env, week=_week(Tuesday={"status": "window", "earliest": "17:00"},
                          Friday={"status": "off", "until": "2026-12-15"}), notes="keep me")
    res = _save(env, unavailable_days=["Friday", "Sunday"])            # notes left out: kept
    assert res["ok"]
    got = res["availability"]
    assert got["week"][1]["status"] == "window"                        # window kept
    assert got["week"][4]["until"] == "2026-12-15"                     # Friday keeps its dates
    assert got["week"][6]["status"] == "off" and got["notes"] == "keep me"


def test_a_managers_save_keeps_an_employees_dates_on_days_still_blocked(env):
    _save(env, week=_week(Friday={"status": "off", "until": "2026-12-15"}, Monday={"status": "off"}))
    models.save_staff_availability(env["rid"], "ben", DAYS[1:4] + DAYS[5:], ["Monday", "Friday"], db_path=env["db"])
    assert models.availability_day_bounds(_row(env)) == {"Friday": {"from": None, "until": "2026-12-15"}}
    assert len(models.get_staff_availability(env["rid"], db_path=env["db"])) == 1     # "ben" is Ben's row
    models.save_staff_availability(env["rid"], "Ben", DAYS[:5] + DAYS[6:], ["Saturday"], db_path=env["db"])
    assert models.availability_day_bounds(_row(env)) == {}


# ── LG-35: published shifts the new availability rules out ─────────────────

def test_saving_names_the_published_shifts_still_on_and_tells_the_deciders_once(env):
    _publish(env, ["2026-10-09,Friday,Ben,Server,5:00pm,11:00pm,6,",
                   "2026-10-06,Tuesday,Ben,Server,11:00am,4:00pm,5,",
                   "2026-10-09,Friday,Ana,Server,5:00pm,11:00pm,6,"])
    res = _save(env, week=_week(Friday={"status": "off"}, Tuesday={"status": "window", "earliest": "17:00"}))
    assert res["ok"]
    assert [(c["date"], c["shift_start"]) for c in res["conflicts"]] == [("2026-10-06", "11:00am"), ("2026-10-09", "5:00pm")]
    assert res["conflicts_text"] == "You're still on Tue 10/6/26 11:00am and Fri 10/9/26 5:00pm — ask to drop them."
    assert len(env["told"]) == 1 and res["managers_told"] == 1
    args, kw = env["told"][0]
    assert kw["deciders"] is True and "Ben" in args[2] and "Fri 10/9/26 5:00pm" in args[3]
    # The same availability saved again is not news to the managers.
    again = _save(env, week=_week(Friday={"status": "off"}, Tuesday={"status": "window", "earliest": "17:00"}))
    assert len(again["conflicts"]) == 2 and len(env["told"]) == 1


def test_a_shift_outside_the_dates_is_not_a_conflict(env):
    _publish(env, ["2026-10-09,Friday,Ben,Server,5:00pm,11:00pm,6,"])
    res = _save(env, week=_week(Friday={"status": "off", "from": "2026-10-12"}))
    assert res["ok"] and res["conflicts"] == [] and env["told"] == []


# ── WF-32: dated absences go to time off ──────────────────────────────────

def test_a_note_that_reads_like_dates_away_gets_the_time_off_hint(env):
    res = _save(env, unavailable_days=["Monday"], notes="away Oct 3-6")
    assert res["hint"]["kind"] == "time_off"
    assert _save(env, unavailable_days=["Monday"], notes="school on Mondays")["hint"] is None
    every = _save(env, unavailable_days=DAYS)
    assert every["status"] == 400 and every["hint"]["kind"] == "time_off"


def test_bad_entries_are_refused_plainly(env):
    assert _save(env, week=_week(Friday={"status": "off", "until": "2026-09-30"}))["status"] == 400
    assert _save(env, week=_week(Friday={"status": "window"}))["status"] == 400
    assert _save(env, week=_week(Friday={"status": "off", "from": "2026-12-01", "until": "2026-11-01"}))["status"] == 400
    assert _save(env, week=[{"day": "Funday", "status": "off"}])["status"] == 400


# ── the staff route ─────────────────────────────────────────────────────────

def _call(fn, user, body):
    import staff_routes
    app = Flask(__name__)
    app.register_blueprint(staff_routes.staff_bp, url_prefix="/staff")
    with app.test_request_context("/staff/api/availability", method="POST" if body is not None else "GET", json=body):
        out = getattr(fn, "__wrapped__", fn)(user)
    resp, status = (out if isinstance(out, tuple) else (out, 200))
    return resp.get_json(), status


def test_the_route_needs_the_version_and_answers_conflicts(env, monkeypatch):
    import staff_routes
    monkeypatch.setattr(ss, "_today", lambda rid: TODAY)
    user = {"id": 9, "restaurant_id": env["rid"], "employee_name": "Ben", "role": "employee"}
    got, st = _call(staff_routes.api_availability, user, None)
    assert st == 200 and got["updated_at"] is None and len(got["week"]) == 7
    body, st = _call(staff_routes.api_availability_save, user, {"unavailable_days": ["Monday"]})
    assert st == 409 and body["stale"]
    _publish(env, ["2026-10-05,Monday,Ben,Server,5:00pm,11:00pm,6,"])
    body, st = _call(staff_routes.api_availability_save, user,
                     {"updated_at": None, "week": _week(Monday={"status": "off"}), "notes": ""})
    assert st == 200 and body["ok"] and body["unavailable_days"] == ["Monday"] and body["updated_at"]
    assert body["conflicts_text"].startswith("You're still on Mon 10/5/26 5:00pm")
    body, st = _call(staff_routes.api_availability_save, user, ["not", "an", "object"])
    assert st == 400


# ── V12: floor sections per shift ─────────────────────────────────────────

CSV = HEAD + "2026-10-09,Friday,Ana,Server,5:00pm,11:00pm,6,\n2026-10-10,Saturday,Ana,Server,17:00,23:00,6,"


def test_a_section_rides_beside_the_shift_only_when_sections_are_named(env):
    from labor import employee_shifts_from_csv
    plain = employee_shifts_from_csv(CSV, "Ana", restaurant_id=env["rid"])
    assert all("section" not in s for s in plain)                      # nothing named: nothing changes
    with pytest.raises(ValueError):
        models.set_shift_section(env["rid"], "2026-10-09", "Ana", "5:00pm", "Patio", db_path=env["db"])
    assert models.set_foh_sections(env["rid"], [" Patio ", "Bar", "patio", ""], db_path=env["db"]) == ["Patio", "Bar"]
    assert models.set_shift_section(env["rid"], "2026-10-09", "ana", "17:00", "patio", db_path=env["db"]) == "Patio"
    shifts = employee_shifts_from_csv(CSV, "Ana", restaurant_id=env["rid"])
    assert shifts[0]["section"] == "Patio" and "section" not in shifts[1]
    with pytest.raises(ValueError):
        models.set_shift_section(env["rid"], "2026-10-09", "Ana", "5:00pm", "Roof", db_path=env["db"])
    # A section the owner stops naming disappears from every shift.
    models.set_foh_sections(env["rid"], ["Bar"], db_path=env["db"])
    assert "section" not in employee_shifts_from_csv(CSV, "Ana", restaurant_id=env["rid"])[0]
    models.set_foh_sections(env["rid"], ["Bar", "Patio"], db_path=env["db"])
    assert models.shift_sections_between(env["rid"], "2026-10-05", "2026-10-11", db_path=env["db"]) == [
        {"date": "2026-10-09", "employee": "ana", "shift_start": "17:00", "section": "Patio"}]
    assert models.set_shift_section(env["rid"], "2026-10-09", "Ana", "5:00pm", "", db_path=env["db"]) is None
    assert models.shift_sections_between(env["rid"], "2026-10-05", "2026-10-11", db_path=env["db"]) == []


def test_a_swapped_shift_keeps_its_section(env):
    models.set_foh_sections(env["rid"], ["Patio"], db_path=env["db"])
    models.set_shift_section(env["rid"], "2026-10-09", "Ana", "5:00pm", "Patio", db_path=env["db"])
    assert models.move_shift_section(env["rid"], "2026-10-09", "5:00pm", "Ana", "Ben", db_path=env["db"])
    assert models.sections_for_employee(env["rid"], "Ben", db_path=env["db"]) == {("2026-10-09", "17:00"): "Patio"}
    assert models.sections_for_employee(env["rid"], "Ana", db_path=env["db"]) == {}


def test_the_studio_route_names_sections_and_assigns_them(env, monkeypatch):
    import mobile_api
    monkeypatch.setattr(mobile_api, "get_conn", lambda *a, **k: models.get_conn(env["db"]), raising=False)
    app = Flask(__name__)
    owner = {"id": 1, "restaurant_id": env["rid"], "is_admin": True, "username": "o"}

    def post(body):
        with app.test_request_context("/mobile/api/labor/schedule/sections", method="POST", json=body):
            out = getattr(mobile_api.mobile_schedule_sections_save, "__wrapped__",
                          mobile_api.mobile_schedule_sections_save)(owner)
        return out[0].get_json(), out[1]

    assert post({"sections": ["Patio", "Bar"]}) == ({"ok": True, "sections": ["Patio", "Bar"]}, 200)
    body, st = post({"date": "2026-10-09", "employee": "Ana", "shift_start": "5:00pm", "section": "Bar"})
    assert st == 200 and body["section"] == "Bar"
    assert post({"date": "2026-10-09", "employee": "Ana", "shift_start": "5:00pm", "section": "Roof"})[1] == 400
    assert post("x")[1] == 400
    with app.test_request_context("/mobile/api/labor/schedule/sections?start=2026-10-05&end=2026-10-11"):
        out = getattr(mobile_api.mobile_schedule_sections, "__wrapped__", mobile_api.mobile_schedule_sections)(owner)
    got = out[0].get_json()
    assert got["sections"] == ["Patio", "Bar"] and got["assigned"][0]["section"] == "Bar" and got["foh_roles"] == ["server"]
    manager = {"id": 2, "restaurant_id": env["rid"], "role": "employee"}
    with app.test_request_context("/mobile/api/labor/schedule/sections", method="POST", json={"sections": []}):
        out = getattr(mobile_api.mobile_schedule_sections_save, "__wrapped__",
                      mobile_api.mobile_schedule_sections_save)(manager)
    assert out[1] == 403


def test_the_studio_offers_the_section_picker_in_the_shift_pane():
    import os
    page = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates",
                             "dashboard.html"), encoding="utf-8").read()
    pane = page[page.index("function ssRenderShift()"):page.index("function ssFindCover(")]
    assert "_ssSecHtml(i)" in pane
    assert "/api/labor/schedule/sections" in page and "function ssSecPick(" in page
