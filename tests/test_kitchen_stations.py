"""Kitchen stations (owner, 9/30/26, from Jim Heflin at Simple EJ's): "just
because a sauté cook works sauté doesn't mean the same cook can be on grill";
"Kitchen is the umbrella term and all jobs back there aren't equal".

The POS has one job, Kitchen. The owner lists stations, marks who is trained
on each and which stations each daypart needs; the scheduler matches cooks to
stations, adds a trained cook where a station is uncovered, and never trims
the only trained cover."""
import json
import re

import pytest

import kitchen_stations as ks
import schedule_economics as econ
import schedule_rules as sr

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

CFG = {"roles": ["Kitchen"], "stations": ["Sauté", "Grill", "Fry", "Prep"],
       "needs": [{"station": "Sauté", "daypart": "night"}, {"station": "Grill", "daypart": "night"},
                 {"station": "Fry", "daypart": "night", "days": ["Friday", "Saturday"]},
                 {"station": "Prep", "daypart": "morning"}],
       "skills": {"Ana Ruiz": ["Sauté", "Grill"], "Bo Chan": ["Grill"], "Cy Diaz": ["Fry", "Grill"],
                  "Dee Park": ["Prep"]}}


def _row(i, emp, start="4:00pm", end="10:00pm", role="Kitchen"):
    s, e = sr.parse_minutes(start), sr.parse_minutes(end)
    return {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(round(((e - s) % 1440) / 60, 1)), "notes": ""}


def test_the_config_keeps_only_what_can_be_met():
    cfg = ks.normalise(dict(CFG, needs=CFG["needs"] + [{"station": "Wok", "daypart": "night"}],
                            skills=dict(CFG["skills"], **{"Eve": ["Wok"]})))
    assert [n["station"] for n in cfg["needs"]] == ["Sauté", "Grill", "Fry", "Prep"]
    assert "Eve" not in cfg["skills"]
    assert ks.normalise({"stations": ["Grill"]}) == {}, "no kitchen role, nothing to match"
    assert ks.required(ks.normalise(CFG), "Friday", "night") == ["Sauté", "Grill", "Fry"]
    assert ks.required(ks.normalise(CFG), "Monday", "night") == ["Sauté", "Grill"]


def test_each_cook_takes_one_station_they_are_trained_on():
    cfg = ks.normalise(CFG)
    placed, unmet = ks.assign(["Bo Chan", "Ana Ruiz"], ["Sauté", "Grill"], cfg)
    assert placed == {"Ana Ruiz": "Sauté", "Bo Chan": "Grill"} and unmet == []
    # Two grill cooks cannot cover sauté: a gap, never a guess.
    placed, unmet = ks.assign(["Bo Chan", "Cy Diaz"], ["Sauté", "Grill"], cfg)
    assert unmet == ["Sauté"] and list(placed.values()) == ["Grill"]


def test_a_double_is_matched_in_each_daypart():
    assert ks.parts_of({"shift_start": "10:00am", "shift_end": "10:00pm"}) == {"morning", "night"}
    assert ks.parts_of({"shift_start": "4:00pm", "shift_end": "1:00am"}) == {"night"}
    assert ks.parts_of({"shift_start": "6:00am", "shift_end": "2:00pm"}) == {"morning"}


def test_the_week_names_each_cooks_station_and_every_gap():
    cfg = ks.normalise(CFG)
    rows = [_row(4, "Ana Ruiz"), _row(4, "Bo Chan"), _row(4, "Dee Park", "8:00am", "2:00pm"),
            _row(0, "Ana Ruiz"), _row(0, "Bo Chan"), _row(4, "Sam Server", role="Server")]
    w = ks.week(rows, cfg, WEEK)
    assert w["stations"][0] == {"night": "Sauté"} and w["stations"][1] == {"night": "Grill"}
    assert w["stations"][2] == {"morning": "Prep"} and 5 not in w["stations"]
    gaps = {(g["date"], g["daypart"], g["station"]) for g in w["gaps"]}
    assert ("2026-10-09", "night", "Fry") in gaps, "Friday's fry station has nobody trained"
    assert ("2026-10-05", "morning", "Prep") in gaps


def test_the_trim_never_removes_the_only_trained_cover():
    cfg = ks.normalise(CFG)
    rows = [_row(0, "Ana Ruiz"), _row(0, "Bo Chan"), _row(0, "Cy Diaz")]
    # Cy can grill too, so Bo is spare; Ana is the only sauté cook.
    assert ks.protects(rows, rows[0], cfg) and not ks.protects(rows, rows[1], cfg)
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.stations = cfg
    kept, trimmed, _ = econ.trim_to_budget(rows, 6, {}, constraints=c)
    assert any(r["employee"] == "Ana Ruiz" for r in kept)
    assert all(t.get("employee") != "Ana Ruiz" for t in trimmed)


def test_the_engine_adds_a_trained_cook_for_an_open_station():
    import schedule_engine as se
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.active = {"ana ruiz", "bo chan", "cy diaz", "dee park"}
    c.stations = ks.normalise(dict(CFG, needs=[{"station": "Sauté", "daypart": "night", "days": ["Monday"]},
                                               {"station": "Grill", "daypart": "night", "days": ["Monday"]}]))
    rows = [_row(0, "Bo Chan"), _row(0, "Cy Diaz")]          # two grill cooks, no sauté
    rows, added, unfilled = se._ensure_station_coverage(rows, WEEK, DAYS, {}, {}, constraints=c)
    assert added == 1 and unfilled == []
    new = rows[-1]
    assert new["employee"] == "Ana Ruiz" and new["role"] == "Kitchen" and "Sauté station" in new["notes"]
    assert (new["shift_start"], new["shift_end"]) == ("4:00pm", "10:00pm")
    report = se.station_report(rows, c, WEEK)
    assert report["gaps"] == []
    nights = {a["employee"]: a["night"] for a in report["assigned"]}
    assert nights["Ana Ruiz"] == "Sauté" and "Grill" in nights.values()


def test_a_station_nobody_trained_can_take_is_left_for_the_review():
    import schedule_engine as se
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.active = {"bo chan"}
    c.stations = ks.normalise({"roles": ["Kitchen"], "stations": ["Wok", "Grill"],
                               "needs": [{"station": "Wok", "daypart": "night", "days": ["Monday"]}],
                               "skills": {"Bo Chan": ["Grill"]}})
    rows, added, unfilled = se._ensure_station_coverage([_row(0, "Bo Chan")], WEEK, DAYS, {}, {}, constraints=c)
    assert added == 0 and unfilled == [{"date": "2026-10-05", "day": "Monday", "daypart": "night", "station": "Wok"}]


def test_the_prompt_states_the_stations_and_who_is_trained():
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.stations = ks.normalise(CFG)
    block = sr.prompt_block(c)
    assert "KITCHEN STATIONS (Kitchen shifts)" in block and "Ana Ruiz is trained on: Sauté, Grill" in block
    assert "Fry: 1 trained cook after 3pm, Friday, Saturday" in block


def test_one_edit_changes_one_thing():
    cfg = ks.apply_edit(None, {"op": "roles", "roles": ["Kitchen"]})
    for name in ("Sauté", "Grill"):
        cfg = ks.apply_edit(cfg, {"op": "add_station", "name": name})
    with pytest.raises(ValueError):
        ks.apply_edit(cfg, {"op": "add_station", "name": "grill"})
    cfg = ks.apply_edit(cfg, {"op": "add_need", "station": "Grill", "daypart": "night", "days": ["Saturday", "Friday"]})
    assert cfg["needs"] == [{"station": "Grill", "daypart": "night", "days": ["Friday", "Saturday"], "count": 1}]
    cfg = ks.apply_edit(cfg, {"op": "skill", "person": "Ana Ruiz", "station": "Grill", "on": True})
    cfg = ks.apply_edit(cfg, {"op": "skill", "person": "ana  ruiz", "station": "Sauté", "on": True})
    assert cfg["skills"] == {"Ana Ruiz": ["Grill", "Sauté"]}
    cfg = ks.apply_edit(cfg, {"op": "remove_station", "name": "Grill"})
    assert cfg["needs"] == [] and cfg["skills"] == {"Ana Ruiz": ["Sauté"]}
    with pytest.raises(ValueError):
        ks.apply_edit(cfg, {"op": "remove_need", "station": "Sauté", "daypart": "night"})


def test_the_owner_sets_stations_through_the_rules_endpoint(db_path, monkeypatch):
    import models
    import strategy_routes as srt
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr("client_api.log_account_event", lambda *a, **k: None)
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test", module_labor=1), db_path=db_path)
    owner = {"id": 1, "restaurant_id": rid, "role": "client"}
    manager = {"id": 2, "restaurant_id": rid, "role": "manager"}

    def post(u, edit):
        monkeypatch.setattr(srt, "_body", lambda: {"station_edit": edit})
        return srt._do_compliance_set(u)
    assert post(manager, {"op": "roles", "roles": ["Kitchen"]})[1] == 403
    for edit in ({"op": "roles", "roles": ["Kitchen"]}, {"op": "add_station", "name": "Grill"},
                 {"op": "add_need", "station": "Grill", "daypart": "night"},
                 {"op": "skill", "person": "Bo Chan", "station": "Grill", "on": True}):
        out, code = post(owner, edit)
        assert code == 200, out
    ks_out = out["kitchen_stations"]
    assert ks_out["active"] and ks_out["skills"] == {"Bo Chan": ["Grill"]}
    out, code = post(owner, {"op": "add_station", "name": "Grill"})
    assert code == 400 and "already" in out["error"]
    stored = json.loads(models.get_restaurant(rid, db_path=db_path).kitchen_stations_json)
    assert stored["needs"][0]["station"] == "Grill"
    c = sr.build_constraints(rid, WEEK, DAYS, db_path=db_path)
    assert c.stations and c.stations["stations"] == ["Grill"]


def test_a_cook_sees_their_station_on_their_shift(db_path, monkeypatch):
    """The staff app says "On Grill" (owner, 9/30/26). The station rides in
    the published week's review (schedule_engine.station_report)."""
    import models
    import staff_schedule
    from datetime import date
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test", module_labor=1), db_path=db_path)
    csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
           "2026-10-05,Monday,Bo Chan,Kitchen,4:00pm,10:00pm,6,\n")
    hid = models.save_schedule_history(rid, "2026-10-05", "2026-10-11", 6, 40, 30, csv, [], db_path=db_path)
    review = {"stations": {"stations": ["Grill"], "gaps": [],
                           "assigned": [{"date": "2026-10-05", "employee": "Bo Chan", "shift_start": "4:00pm",
                                         "role": "Kitchen", "morning": None, "night": "Grill"}]}}
    c = real(db_path)
    c.execute("UPDATE schedule_history SET published_at=datetime('now'), review_json=? WHERE id=?",
              (json.dumps(review), hid))
    c.commit()
    c.close()
    out = staff_schedule.shifts_for_employee(rid, "Bo Chan", today=date(2026, 10, 5))
    assert out["today"]["station"] == "Grill" and out["today"]["start"] == "4:00pm"
    swift = open("ios/CavnarAI/CavnarAI/Features/Staff/StaffModels.swift").read()
    assert re.search(r"\?\? \(?(try\? )?c\.decodeIfPresent\(String\.self, forKey: \.start\)", swift), \
        "the app reads the API's start/end"
