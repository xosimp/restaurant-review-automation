"""Who is on the roster, as what, and who has stopped working (schedule audit
10/3/26 D-17, E-3).

The roster role was the newest punch's — one host pickup made a bartender a
Host, and her manager status, leader rules and section caps went with it.
Everyone in three years of history stayed schedulable until deactivated, and
every fill pass sorted candidates fewest-hours-first, so the server who left
in June was the first one picked; the open-shift broadcast walked the roster
alphabetically, told the departed and never reached the end of the alphabet.
"""
import datetime as dt
import json

import pytest

import models
import schedule_engine as se
import schedule_rules as sr
import shift_requests
import staff_settings as ss

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
TODAY = dt.date(2026, 10, 3)
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ss, "get_conn", fake)
    monkeypatch.setattr(sr, "get_conn", fake)
    monkeypatch.setattr(ss, "_today", lambda rid: TODAY)
    import client_api
    monkeypatch.setattr(client_api, "invalidate_insight_cache", lambda *a, **k: None)
    yield


def _rid():
    return models.create_restaurant(models.Restaurant(name="Roster Co", owner_email="ro@x.test", module_labor=1))


def _days_back(n, start=TODAY):
    return [(start - dt.timedelta(days=i)).isoformat() for i in range(1, n + 1)]


def _shifts(rid, rows):
    lines = [HEADER] + [f"{d},{dt.date.fromisoformat(d).strftime('%A')},{n},{r},4:00pm,10:00pm,6" for d, n, r in rows]
    models.save_client_data(rid, "shifts", "\n".join(lines) + "\n")


# ── D-17: the role is the one worked most, not the newest punch ─────────────

def test_one_host_pickup_does_not_make_a_bartender_a_host():
    rid = _rid()
    rows = [(d, "Bea", "Bartender") for d in _days_back(20)[1:]] + [(_days_back(1)[0], "Bea", "Host")]
    _shifts(rid, rows)
    e = next(e for e in ss.roster(rid) if e["name"] == "Bea")
    assert e["role"] == "Bartender"
    assert e["recent_roles"][0] == "Bartender" and "Host" in e["recent_roles"]


def test_a_real_move_to_a_new_role_wins_once_it_is_most_of_their_weeks():
    rid = _rid()
    old = [(d, "Cal", "Server") for d in _days_back(70)[40:]]        # 6-10 weeks back
    new = [(d, "Cal", "Bartender") for d in _days_back(30)]          # the last month, every day
    _shifts(rid, old + new)
    assert next(e for e in ss.roster(rid) if e["name"] == "Cal")["role"] == "Bartender"


def test_the_owners_primary_role_still_wins():
    rid = _rid()
    _shifts(rid, [(d, "Dee", "Server") for d in _days_back(10)])
    import people
    people.add_role(rid, "Dee", "Bar Manager", primary=True, record=False)
    assert next(e for e in ss.roster(rid) if e["name"] == "Dee")["role"] == "Bar Manager"


# ── E-3: who has stopped working ────────────────────────────────────────────

def _world(rid):
    working = [(d, n, "Server") for d in _days_back(14) for n in ("Ana", "Ben")]
    gone = [(d, "June", "Server") for d in _days_back(120)[90:]]          # last shift ~3 months ago
    mgr = [(_days_back(100)[-1], "Max", "General Manager")]               # a manager who barely punches
    sal = [(_days_back(90)[-1], "Erik", "Server")]                        # salaried, barely punches
    _shifts(rid, working + gone + mgr + sal)
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Erik", "annual": 150000}])})
    models.add_manual_team_member(rid, "Nina", role="Server")              # a new hire, no shift yet


def test_departed_staff_are_dormant_and_never_chosen_by_a_fill():
    rid = _rid()
    _world(rid)
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert set(c.dormant) == {"june"}
    assert c.can_work("June", WEEK[2])[0]                 # still on the roster: the owner may write her in
    ok, why = c.fillable("June", WEEK[2])
    assert not ok and why.startswith("has not worked since ") and "/" in why and "-" not in why
    for n in ("Ana", "Max", "Erik", "Nina"):
        assert c.fillable(n, WEEK[2])[0], n


def test_signs_of_still_being_here_keep_someone_off_the_dormant_list():
    rid = _rid()
    _world(rid)
    # June tells the app her availability this week.
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO staff_availability (restaurant_id, employee_name, available_days, updated_at) "
                     "VALUES (?,?,?,?)", (rid, "June", "[]", (TODAY - dt.timedelta(days=2)).isoformat() + " 10:00:00"))
        conn.commit()
    finally:
        conn.close()
    assert "june" not in ss.dormant_people(rid)
    rid2 = _rid()
    _world(rid2)
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                     "VALUES (?,?,?,?,?)", (rid2, "June", "2026-10-20", "2026-10-22", "approved"))
        conn.commit()
    finally:
        conn.close()
    assert "june" not in ss.dormant_people(rid2)
    rid3 = _rid()
    _world(rid3)
    ss.upsert(rid3, "June", active=True)                 # the owner marks her still here
    assert "june" not in ss.dormant_people(rid3)


def test_stale_uploads_do_not_make_the_whole_team_dormant():
    rid = _rid()
    long_ago = dt.date(2026, 5, 1)
    _shifts(rid, [(d, n, "Server") for d in _days_back(14, start=long_ago) for n in ("Ana", "Ben")])
    assert ss.dormant_people(rid) == {}


def test_the_model_is_not_offered_dormant_people(monkeypatch):
    rid = _rid()
    import labor
    import time_utils
    import weather
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda r: [{"date": "2026-09-01", "employee": "Ana"}])
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r, **k: {"is_live": True, "blended_rate": 20.0})
    monkeypatch.setattr(labor, "build_demand_forecast", lambda r: {"ok": False})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: dt.datetime(2026, 10, 1, 9, 0))
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    roster = [{"name": "Ana", "role": "Server"}, {"name": "June", "role": "Server"}]
    monkeypatch.setattr(ss, "roster", lambda *a, **k: roster)
    c = sr.Constraints(restaurant_id=rid, week_dates=WEEK, week_days=list(sr.DAYS))
    c.active, c.dormant = {"ana", "june"}, {"june": "2026-06-20"}
    monkeypatch.setattr(sr, "build_constraints", lambda *a, **k: c)
    captured = {}

    def fake_parts(analysis, shifts, roster_pairs, kwargs):
        captured["pairs"] = list(roster_pairs)
        captured["roster"] = list(kwargs.get("roster") or [])
        return {"schedule_csv": HEADER, "narrative": [], "summary": []}
    monkeypatch.setattr(se, "_generate_in_parts", fake_parts)
    result = se._build_schedule_result(rid)
    assert captured["pairs"] == [("Ana", "Server")] and captured["roster"] == [("Ana", "Server")]
    assert result["dormant"] == {"June": "2026-06-20"}


def test_the_team_page_asks_about_someone_who_stopped_working(monkeypatch):
    rid = _rid()
    _world(rid)
    import strategy_routes
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_rid", lambda u: rid)
    out, status = strategy_routes._do_roster_get({"id": 1, "role": "client"})
    assert status == 200
    june = next(e for e in out["roster"] if e["name"] == "June")
    assert june["dormant"] is True and june["dormant_text"].startswith("Not worked since ")
    assert june["dormant_text"].endswith("— deactivate?") and "-" not in june["last_worked_label"]
    ana = next(e for e in out["roster"] if e["name"] == "Ana")
    assert ana["dormant"] is False and ana["dormant_text"] is None


# ── E-3: the open-shift broadcast goes round, and skips the departed ────────

def test_the_open_shift_notice_skips_the_dormant_and_rotates(monkeypatch):
    rid = 7
    people_ = [{"name": n, "active": True} for n in ("Abe", "Bo", "Cy", "Di", "June")]
    monkeypatch.setattr(ss, "roster", lambda *a, **k: people_)
    rows = [{"date": WEEK[2], "day": "Wednesday", "employee": "Zed", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""},
            {"date": WEEK[1], "day": "Tuesday", "employee": "Abe", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    c = sr.Constraints(restaurant_id=rid, week_dates=WEEK, week_days=list(sr.DAYS))
    c.active = {"abe", "bo", "cy", "di", "june", "zed"}
    c.dormant = {"june": "2026-06-20"}
    monkeypatch.setattr(sr, "build_constraints", lambda *a, **k: c)
    monkeypatch.setattr(shift_requests, "_live_hist", lambda *a, **k: {"schedule_csv": "x"})
    import schedule_versions
    monkeypatch.setattr(schedule_versions, "rows_from_csv", lambda text: [dict(r) for r in rows])
    monkeypatch.setattr(shift_requests, "_role_ok", lambda *a, **k: (True, ""))
    monkeypatch.setattr(se, "replacement_is_legal", lambda *a, **k: (True, ""))
    # Bo and Cy were told about the last two open shifts; Di about one.
    monkeypatch.setattr(shift_requests, "_recent_notices",
                        lambda *a, **k: {"bo": (2, "2026-10-01"), "cy": (2, "2026-09-30"), "di": (1, "2026-09-29")})
    monkeypatch.setattr(shift_requests, "OPEN_SHIFT_NOTICE_LIMIT", 3)
    req = {"employee_name": "Zed", "date": WEEK[2], "shift_start": "4:00pm", "role": "Server"}
    got = shift_requests._who_could_take(rid, req, None)
    assert "June" not in got
    # Abe has never been told but already works that week; still first:
    # the turn is notices first, hours second.
    assert got == ["Abe", "Di", "Cy"]


# ── the merge with the fix round (10/3/26): managers barely punch ──────────

def _published_week(rid, rows, start):
    """A live published week holding `rows` [(date, name, role)]."""
    csv_text = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "".join(
        f"{d},{dt.date.fromisoformat(d).strftime('%A')},{n},{r},4:00pm,10:00pm,6,\n" for d, n, r in rows)
    conn = models.get_conn()
    try:
        models._ensure_history_columns(conn)
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, summary_json, "
                     "published_at) VALUES (?,?,?,?,'[]',datetime('now'))",
                     (rid, start.isoformat(), (start + dt.timedelta(days=6)).isoformat(), csv_text))
        conn.commit()
    finally:
        conn.close()


def test_a_manager_of_any_kind_who_barely_punches_is_never_dormant():
    """A department's manager is not who runs the floor (is_manager_role says
    no to "Kitchen Manager"), but managers barely punch: a manager role of
    any kind keeps them on the team, and so does a manager's shift on a
    published week from before the six weeks. Anybody else's old published
    shift does not."""
    rid = _rid()
    working = [(d, n, "Server") for d in _days_back(14) for n in ("Ana", "Ben")]
    old = _days_back(100)[-1]
    _shifts(rid, working + [(old, "Kim", "Kitchen Manager"), (old, "Mo", "Server"), (old, "Sal", "Server")]
            + [(d, "June", "Server") for d in _days_back(120)[90:]])
    wk = TODAY - dt.timedelta(weeks=10)
    _published_week(rid, [(wk.isoformat(), "Mo", "MOD"), (wk.isoformat(), "Sal", "Server")], wk)
    assert sr.manager_role_kind("Kitchen Manager") == "department" and not sr.is_manager_role("Kitchen Manager")
    assert set(ss.dormant_people(rid)) == {"june", "sal"}
    c = sr.build_constraints(rid, WEEK, list(sr.DAYS))
    assert set(c.dormant) == {"june", "sal"}
    for n in ("Kim", "Mo", "Ana"):
        assert c.fillable(n, WEEK[2])[0], n


def test_a_gap_with_no_published_week_never_suggests_someone_dormant():
    """labor_replacements.for_gap with no published week to check the gap
    against (an Ask "who could cover Wednesday?") still leaves out somebody
    who has stopped working."""
    import labor_replacements
    rid = _rid()
    _world(rid)
    fits = labor_replacements.for_gap(rid, "Server", "Wednesday", on_date=WEEK[2], limit=10)
    names = [f["name"] for f in fits]
    assert "June" not in names and "Ana" in names, names
