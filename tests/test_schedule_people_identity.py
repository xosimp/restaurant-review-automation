"""One person, one key for the schedule's rules (schedule audit 10/3/26 D-8,
E-25, D-7).

Every per-person input — availability, time off, notes, ratings, station
skills, salaried entries — was keyed by the name as typed. A spelling the
people module knew for somebody (an alias, the name before a POS rename, an
owner's nickname) matched nobody: a legal time-off block stopped applying
with no error, ratings judged nobody, a salaried manager punching under a
nickname was held to overtime and spent from the hourly budget, and "Kim
T." and "Kim Tran" were checked as two people while the owner had not
yet said whether they are one.
"""
import json
from datetime import date

import pytest

import labor
import models
import people
import pos
import schedule_engine
import schedule_rules as sr
import staff_settings

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = list(sr.DAYS)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    monkeypatch.setattr(sr, "get_conn", fake)
    import time_off
    monkeypatch.setattr(time_off, "get_conn", fake)
    monkeypatch.setattr(pos, "_complete_through_for", lambda r: date.today())
    import client_api
    monkeypatch.setattr(client_api, "invalidate_insight_cache", lambda *a, **k: None)
    yield


HEADER = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate,"
          "employee_ext_id,employee_payroll_id,employee_named,schedule_known\n")


def _rid():
    return models.create_restaurant(models.Restaurant(name="Identity Rules Co", owner_email="ir@x.test",
                                                      module_labor=1))


def _sync(rid, people_rows, days=(1, 2, 3), month=9, role="Server"):
    lines = [HEADER]
    for d in days:
        day = date(2026, month, d)
        for name, ext in people_rows:
            lines.append(f"{day.isoformat()},{day.strftime('%A')},{name},{role},16:00,22:00,6,6,1000,,,{ext},,1,0\n")
    return pos.save_synced_shifts(rid, "".join(lines), "rpower")


def _row(name, start, end, d="2026-10-07", hours=None, role="Server"):
    s, e = sr.parse_minutes(start), sr.parse_minutes(end)
    return {"date": d, "day": "", "employee": name, "role": role, "shift_start": start, "shift_end": end,
            "scheduled_hours": str(hours if hours is not None else round(((e - s) % 1440) / 60, 1)), "notes": ""}


def _renamed(rid):
    """Mike Smith on the POS, then the POS spells him Michael Smith (one id)."""
    _sync(rid, [("Mike Smith", "7001"), ("Ana B.", "7002")])
    _sync(rid, [("Michael Smith", "7001"), ("Ana B.", "7002")], days=(8, 9))
    assert people.canonical_names(rid, ["Mike Smith"])["Mike Smith"] == "Michael Smith"


def _insert_time_off(rid, name, start, end, status="approved"):
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                     "VALUES (?,?,?,?,?)", (rid, name, start, end, status))
        conn.commit()
    finally:
        conn.close()


# ── D-8: every per-person input under the person's one key ──────────────────

def test_time_off_under_an_old_spelling_still_blocks_the_person():
    rid = _rid()
    _renamed(rid)
    # A request filed under the spelling the POS used to have (a store the
    # rename never reached — a staff login, a hand-typed entry).
    _insert_time_off(rid, "Mike Smith", "2026-10-07", "2026-10-07")
    c = sr.build_constraints(rid, WEEK, DAYS)
    ok, why = c.can_work("Michael Smith", "2026-10-07")
    assert not ok and why == sr.LABELS["approved_time_off"]
    assert any(v["kind"] == "approved_time_off" for v in sr.violations([_row("Michael Smith", "4:00pm", "10:00pm")], c))
    assert c.unmatched == []


def test_a_fact_under_a_name_nobody_goes_by_is_named_never_silent():
    rid = _rid()
    _sync(rid, [("Ana B.", "7002"), ("Kim T.", "7003")])
    _insert_time_off(rid, "Zed Nobody", "2026-10-07", "2026-10-08")
    models.save_staff_note(rid, "Kim Tram", "no Fridays")       # a typo of a roster name
    c = sr.build_constraints(rid, WEEK, DAYS)
    by = {(u["source"], u["name"]): u for u in c.unmatched}
    assert ("time off", "Zed Nobody") in by and "10/7/26" in by[("time off", "Zed Nobody")]["detail"]
    assert by[("scheduling notes", "Kim Tram")].get("suggestion") == "Kim T."
    # A legal block that holds for nobody reaches the publish gate too.
    assert any(p["source"] == "time off" and p["name"] == "Zed Nobody" for p in c.input_problems)


def test_availability_notes_holds_and_skills_follow_the_person():
    rid = _rid()
    _renamed(rid)
    conn = models.get_conn()
    try:
        conn.execute("INSERT INTO staff_availability (restaurant_id, employee_name, available_days, unavailable_days, "
                     "notes) VALUES (?,?,?,?,?)", (rid, "Mike Smith", "[]", json.dumps(["Tuesday"]), "class Tues"))
        conn.commit()
    finally:
        conn.close()
    import person_note_holds
    person_note_holds.add_hold(rid, "Mike Smith", "no Thursdays", days=["Thursday"])
    models.update_restaurant(rid, {"kitchen_stations_json": json.dumps(
        {"roles": ["Server"], "stations": ["Grill"], "skills": {"Mike Smith": ["Grill"]}})})
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert not c.can_work("Michael Smith", "2026-10-06")[0]                 # Tuesday, availability
    assert not c.can_work("Michael Smith", "2026-10-08")[0]                 # Thursday, the hold
    assert "class" in c.notes.get("michael smith", "")
    import kitchen_stations
    assert kitchen_stations.trained(c.stations, "Michael Smith") == {"Grill"}


def test_ratings_and_signals_under_an_old_spelling_score_the_roster_person():
    rid = _rid()
    _renamed(rid)
    models.set_capability(rid, "Mike Smith", "overall", score=5)   # the store the rename did not re-point
    c = sr.build_constraints(rid, WEEK, DAYS)
    signals = {"roster": ["Ana B.", "Michael Smith"], "scores": models.get_operational_scores(rid),
               "tenure": {"Mike Smith": 40, "Michael Smith": 3}, "experienced": {"Mike Smith"},
               "availability": {"Mike Smith": {"Tuesday"}}}
    schedule_engine._reconcile_to_roster(signals, c, rid)
    assert signals["scores"] == {"Michael Smith": 5}
    assert signals["tenure"]["Michael Smith"] == 40               # never two people's half-histories
    assert signals["experienced"] == {"Michael Smith"}
    assert signals["availability"] == {"Michael Smith": {"Tuesday"}}


# ── E-25: an open "same person?" question joins the two in the sweep ───────

def _kim(rid):
    _sync(rid, [("Kim T.", "7003"), ("Ana B.", "7002")])
    models.add_manual_team_member(rid, "Kim Tran", role="Server")
    people.stamp_person_ids(rid)
    assert any({q["a"]["name"], q["b"]["name"]} == {"Kim T.", "Kim Tran"} for q in people.open_questions(rid))


def test_two_roster_names_an_open_question_joins_are_one_week_in_the_sweep():
    rid = _rid()
    _kim(rid)
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert c.sweep_key("Kim T.") == c.sweep_key("Kim Tran")
    rows = [_row("Kim T.", "9:00am", "3:00pm"), _row("Kim Tran", "1:00pm", "9:00pm")]
    over = [v for v in sr.violations(rows, c) if v["kind"] == "overlap"]
    assert over and "Kim T." in over[0]["detail"] and "one person" in over[0]["detail"]
    # Hours too: 5 x 9h under one name and a 9h day under the other is 54h.
    week = [_row("Kim T.", "9:00am", "6:00pm", d=d) for d in WEEK[:5]]
    ok, why = c.can_add(_row("Kim Tran", "9:00am", "6:00pm", d=WEEK[5]), week)
    assert not ok
    # A fill can't hand Kim Tran a shift that overlaps Kim T.'s either.
    ok, _ = c.can_add(_row("Kim Tran", "1:00pm", "5:00pm"), [_row("Kim T.", "9:00am", "3:00pm")])
    assert not ok


def test_answered_different_people_are_two_people_again():
    rid = _rid()
    _kim(rid)
    q = next(q for q in people.open_questions(rid) if {q["a"]["name"], q["b"]["name"]} == {"Kim T.", "Kim Tran"})
    people.answer_question(rid, q["id"], False)
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert c.sweep_key("Kim T.") != c.sweep_key("Kim Tran")
    rows = [_row("Kim T.", "9:00am", "3:00pm"), _row("Kim Tran", "1:00pm", "9:00pm")]
    assert not [v for v in sr.violations(rows, c) if v["kind"] == "overlap"]


# ── D-7: salaried entries linked to people ──────────────────────────────────

def _gabe(rid):
    """Gabe Huerta punches on the POS; the owner typed "Gabriel Huerta" as salaried."""
    _sync(rid, [("Gabe Huerta", "7010"), ("Ana B.", "7002")], role="Manager FOH")


def test_an_unlinked_salaried_name_warns_with_the_roster_name_to_check():
    rid = _rid()
    _gabe(rid)
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Gabriel Huerta", "annual": 90000}])})
    got = models.salaried_matches(models.get_restaurant(rid))
    assert got[0]["matched"] is None and got[0]["suggestion"] == "Gabe Huerta"
    assert "matches nobody on your roster" in got[0]["warning"] and "Gabe Huerta" in got[0]["warning"]
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert not c.is_salaried("Gabe Huerta")             # never guessed
    assert any(u["source"] == "salaried staff" for u in c.unmatched)
    assert "Gabriel" not in sr.prompt_block(c)


def test_a_salaried_name_that_is_an_alias_holds_for_every_spelling():
    rid = _rid()
    _gabe(rid)
    # The owner says "Gabriel Huerta" is Gabe (an alias on the person).
    conn = people._conn(None)
    try:
        idx = people._Index(conn, rid)
        pid = next(iter(idx.for_key(people._nk("Gabe Huerta"))))
        people._alias(conn, idx, pid, "manual", "Gabriel Huerta")
        conn.commit()
    finally:
        conn.close()
    models.update_restaurant(rid, {"salaried_staff_json": json.dumps([{"name": "Gabriel Huerta", "annual": 90000}])})
    r = models.get_restaurant(rid)
    assert "gabe huerta" in models.salaried_keys(r)
    assert models.salaried_matches(r)[0]["matched"] == "Gabe Huerta"
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert c.is_salaried("Gabe Huerta") and sr.overtime_line(c, "Gabe Huerta") == sr.SALARIED_HOURS_CAP
    assert "Salaried (the same pay whatever the hours): Gabe Huerta" in sr.prompt_block(c)
    # His punches leave the hourly analysis.
    shifts = [{"employee": "Gabe Huerta", "date": "2026-09-01", "scheduled_hours": "6", "shift_start": "16:00",
               "shift_end": "22:00"}, {"employee": "Ana B.", "date": "2026-09-01", "scheduled_hours": "6",
                                       "shift_start": "16:00", "shift_end": "22:00"}]
    kept, hours = labor._without_salaried(rid, shifts)
    assert [s["employee"] for s in kept] == ["Ana B."] and hours == 6.0


def test_saving_a_salaried_name_links_it_to_the_person(monkeypatch):
    rid = _rid()
    _renamed(rid)
    import strategy_routes
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_rid", lambda u: rid)
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"salaried_add": {"name": "Mike Smith", "annual": 80000}})
    import client_api
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    monkeypatch.setattr(models, "recost_labor_history", lambda *a, **k: 0)
    out, status = strategy_routes._do_targets_set({"id": 1, "role": "client"})
    assert status == 200
    staff = models.salaried_staff(models.get_restaurant(rid))
    assert staff[0]["name"] == "Michael Smith" and staff[0].get("person_id")
    assert out["targets"]["salaried_warnings"] == []
