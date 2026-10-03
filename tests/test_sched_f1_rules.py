"""Schedule fix round 10/3/26, workstream F1 — managers, closers and roles as
the rules read them. Each test is one of the audit's findings, failing
before its fix:

  P-7 / E-14 / E-15  who runs the floor is explicit per person (yes / no /
                     automatic from their role, a held role, a manager role
                     worked in the last 8 weeks, the floor manager
                     certificate); a department manager and a food-safety
                     card never make anyone one
  E-13 / D-5 / E-12  acting managers by date, standing shifts, an Owner role
                     salaried-style unless paid hourly, the salaried cap
  D-9 / L-9          closers per role, chosen roles, view-as flags excluded
  D-13               role families: section cap, certificates, stays after
                     close, cross-training
  D-14               the owner's rule "two servers Saturday night" parsed
                     against Server AM / Server PM
  D-15               held roles reach the rules and the solver; role_not_held
  D-16               trainees: not coverage, paired, never handed out
  D-43               one "stays until close + N" setting per role
"""
import sys
from datetime import date, timedelta

import pytest

import labor
import models
import people
import schedule_rules as sr
import staff_settings as ss
from models import Restaurant, create_restaurant

WEEK = [(date(2026, 10, 12) + timedelta(days=i)).isoformat() for i in range(7)]   # a Monday
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HISTORY = {}


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    HISTORY.clear()
    monkeypatch.setattr(models, "_cached_shifts", lambda r: HISTORY.get(r, []))
    yield


def _rid(**cols):
    rid = create_restaurant(Restaurant(name="F1 Grill", owner_email="f1@x.test", timezone="America/Chicago"))
    if cols:
        models.update_restaurant(rid, cols)
    return rid


def _punch(rid, name, role, d, start="4:00pm", end="10:00pm"):
    HISTORY.setdefault(rid, []).append({"date": d, "employee": name, "role": role, "shift_start": start,
                                        "shift_end": end, "scheduled_hours": "6"})


def _before(days):
    return (date.fromisoformat(WEEK[0]) - timedelta(days=days)).isoformat()


def _c(rid):
    return sr.build_constraints(rid, WEEK, DAYS)


def _row(i, emp, role, start="4:00pm", end="10:00pm", hours=6):
    return {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _kinds(rows, c):
    return [v["kind"] for v in sr.violations(rows, c)]


# ── P-7 / E-14 / E-15: who runs the floor ──────────────────────────────────

def test_floor_titles_count_and_department_managers_do_not():
    for role in ("Manager FOH", "General Manager", "GM", "AGM", "MOD", "Manager on Duty", "Shift Lead",
                 "Assistant Manager", "Owner", "Co-Owner", "Lead"):
        assert sr.is_manager_role(role), role
    for role in ("Kitchen Manager", "Bar Manager", "Bar Supervisor", "Assistant Kitchen Manager", "Lead Server",
                 "Server", "Bartender AM", "Line Cook", ""):
        assert not sr.is_manager_role(role), role
    assert sr.manager_role_kind("Kitchen Manager") == "department"


def test_manager_status_is_the_owners_word_else_role_held_role_recent_role_or_certificate():
    rid = _rid()
    for n, role in (("Erik", "Owner"), ("Andrew", "Manager FOH"), ("Kim", "Kitchen Manager"), ("Ana", "Server"),
                    ("Bob", "Manager FOH"), ("Pat", "Server"), ("Cook", "Line Cook"), ("Sue", "Host")):
        models.add_manual_team_member(rid, n, role=role)
    # Tom last punched as a bartender; he ran shifts three weeks ago.
    _punch(rid, "Tom", "Manager FOH", _before(21))
    _punch(rid, "Tom", "Bartender", _before(2))
    # Vic managed twelve weeks ago, not since.
    _punch(rid, "Vic", "Manager FOH", _before(84))
    _punch(rid, "Vic", "Server", _before(3))
    ss.upsert(rid, "Ana", floor_manager=True)
    ss.upsert(rid, "Bob", floor_manager=False)
    ss.upsert(rid, "Cook", certifications=["food_protection_manager"])
    ss.upsert(rid, "Sue", certifications=["manager"])
    people.add_role(rid, "Pat", "Shift Lead")
    c = _c(rid)
    assert set(c.managers) == {"erik", "andrew", "ana", "tom", "sue", "pat"}, c.manager_basis
    assert c.manager_basis["tom"]["basis"] == "recent_role" and c.managers["tom"] == "Manager FOH"
    assert c.manager_basis["pat"]["basis"] == "held_role"
    assert c.manager_basis["sue"]["basis"] == "certificate"
    assert c.manager_basis["ana"]["basis"] == "set"
    assert c.manager_basis["kim"]["basis"] == "department" and "kim" not in c.managers
    assert c.manager_basis["bob"]["basis"] == "set_not" and "bob" not in c.managers
    assert "vic" not in c.managers and "cook" not in c.managers


def test_floor_manager_back_to_automatic_is_recorded():
    rid = _rid()
    models.add_manual_team_member(rid, "Ana", role="Server")
    ss.upsert(rid, "Ana", floor_manager="yes")
    assert ss.for_name(rid, "Ana")["floor_manager"] is True
    ss.upsert(rid, "Ana", floor_manager="auto")
    assert ss.for_name(rid, "Ana")["floor_manager"] is None
    conn = models.get_conn()
    try:
        rows = conn.execute("SELECT old_value, new_value FROM change_log WHERE restaurant_id=? AND field='floor manager'",
                            (rid,)).fetchall()
    finally:
        conn.close()
    assert [(r["old_value"], r["new_value"]) for r in rows] == [('"automatic"', '"yes"'), ('"yes"', '"automatic"')]
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Ana", floor_manager="maybe")


def test_the_certificate_labels_say_floor_manager_and_food_safety_apart():
    assert ss.CERTIFICATION_LABELS["manager"] == "Floor manager (can run the shift)"
    assert "food_protection_manager" in ss.CERTIFICATIONS
    assert sr.manager_basis("Cook", "Line Cook", {}, (), (), ["food_protection_manager"])["counts"] is False


# ── E-13: acting managers by date ──────────────────────────────────────────

def test_an_acting_manager_counts_on_their_dates_only():
    rid = _rid()
    for n, role in (("Ann", "Server"), ("Maria", "Bartender"), ("Andrew", "Manager FOH")):
        models.add_manual_team_member(rid, n, role=role)
    ss.upsert(rid, "Maria", acting_manager=[{"from": "10/13/26", "until": "10/14/26", "note": "Andrew away"}])
    assert ss.for_name(rid, "Maria")["acting_manager"] == [{"from": "2026-10-13", "until": "2026-10-14",
                                                            "note": "Andrew away"}]
    c = _c(rid)
    assert c.acting_managers == {"maria": {WEEK[1], WEEK[2]}}
    on_tue = [_row(1, "Ann", "Server", "11:00am", "4:00pm", 5), _row(1, "Maria", "Bartender", "11:00am", "4:00pm", 5)]
    on_thu = [_row(3, "Ann", "Server", "11:00am", "4:00pm", 5), _row(3, "Maria", "Bartender", "11:00am", "4:00pm", 5)]
    assert "no_manager" not in _kinds(on_tue, c)
    assert "no_manager" in _kinds(on_thu, c)
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Maria", acting_manager=[{"from": "2026-10-20", "until": "2026-10-13"}])


# ── D-5: standing shifts ───────────────────────────────────────────────────

def test_standing_shifts_are_stored_validated_and_read_into_the_rules():
    rid = _rid()
    models.add_manual_team_member(rid, "Jim", role="Owner")
    ss.upsert(rid, "Jim", standing_shifts=[{"day": "tuesday", "start": "10:00am", "end": "6:00pm"},
                                           {"day": "Friday", "start": "4:00pm", "end": "1:00am", "role": "Owner"}])
    c = _c(rid)
    assert c.standing_shifts["jim"] == [
        {"day": "Tuesday", "start": "10:00am", "end": "6:00pm", "role": "Owner"},
        {"day": "Friday", "start": "4:00pm", "end": "1:00am", "role": "Owner"}]
    for bad in ([{"day": "Funday", "start": "1:00pm", "end": "5:00pm"}],
                [{"day": "Monday", "start": "1:00pm", "end": "1:30pm"}],
                [{"day": "Monday", "start": "9:00am", "end": "5:00pm"},
                 {"day": "Monday", "start": "4:00pm", "end": "10:00pm"}]):
        with pytest.raises(ss.StaffSettingsError):
            ss.upsert(rid, "Jim", standing_shifts=bad)


# ── E-12: an Owner role is salaried-style; the salaried cap ────────────────

def test_an_owner_role_is_salaried_style_unless_paid_hourly_and_the_cap_defaults_to_55():
    rid = _rid()
    for n, role in (("Erik", "Owner"), ("Jim", "Co-Owner"), ("Ann", "Server")):
        models.add_manual_team_member(rid, n, role=role)
    ss.upsert(rid, "Jim", paid_hourly=True)
    c = _c(rid)
    assert c.is_salaried("Erik") and not c.is_salaried("Jim") and not c.is_salaried("Ann")
    assert c.salaried_style == {"erik"} and c.salaried_cap == 55.0
    assert sr.overtime_line(c, "Erik") > 40, "no overtime line for the owner"
    models.update_restaurant(rid, {"salaried_cap": 50})
    assert _c(rid).salaried_cap == 50.0


# ── D-9 / L-9: closers per role ────────────────────────────────────────────

def _closer_roster(rid):
    for n, role in (("Sam", "Bartender PM"), ("Lee", "Bartender PM"), ("Dee", "Dishwasher"),
                    ("Andrew", "Manager FOH"), ("Ana", "Server PM")):
        models.add_manual_team_member(rid, n, role=role)
    for n in ("Sam", "Dee"):
        models.set_capability(rid, n, attribute="can_close", flag=True)


def test_closers_are_per_role_and_a_dishwasher_staying_late_does_not_close_the_bar():
    rid = _rid(close_times_json='{"Friday": "12:00am"}')
    _closer_roster(rid)
    c = _c(rid)
    assert c.closers_by_role == {"bartender": {"sam"}, "dishwasher": {"dee"}}
    rows = [_row(4, "Andrew", "Manager FOH", "3:00pm", "12:00am", 9), _row(4, "Sam", "Bartender PM", "4:00pm", "10:00pm"),
            _row(4, "Lee", "Bartender PM", "4:00pm", "10:30pm", 6.5), _row(4, "Dee", "Dishwasher", "5:00pm", "12:00am", 7)]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "keyholder_until_close"]
    assert len(v) == 1 and v[0]["close_role"] == "bartender" and v[0]["hard"] and v[0]["day_level"]
    assert "Sam, the Bartender closer Friday, leaves before close" in v[0]["detail"]


def test_the_last_of_the_role_out_must_be_one_of_its_closers():
    rid = _rid(close_times_json='{"Friday": "11:00pm"}')
    _closer_roster(rid)
    c = _c(rid)
    rows = [_row(4, "Andrew", "Manager FOH", "3:00pm", "12:30am", 9.5), _row(4, "Sam", "Bartender PM", "4:00pm", "11:00pm", 7),
            _row(4, "Lee", "Bartender PM", "5:00pm", "12:30am", 7.5)]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "keyholder_until_close"]
    assert v and "Lee is the last Bartender out" in v[0]["detail"]


def test_the_owner_chooses_which_roles_have_closers():
    rid = _rid(closer_roles_json='["Bartender"]')
    _closer_roster(rid)
    c = _c(rid)
    assert c.closers_by_role == {"bartender": {"sam"}} and c.closer_flags == {"sam", "dee"}


def test_a_closer_flag_set_through_view_as_does_not_count_until_adopted():
    rid = _rid()
    _closer_roster(rid)
    admin = {"id": 99, "role": "admin", "is_admin": 1, "acting_admin_id": 99, "username": "will"}
    models.set_capability(rid, "Lee", attribute="can_close", flag=True, user=admin)
    conn = models.get_conn()
    conn.execute("UPDATE staff_capabilities SET authority='admin' WHERE employee_name='Lee'")
    conn.commit()
    conn.close()
    assert "Lee" not in models.get_leader_flags(rid) and "Lee" in models.get_leader_flags(rid, include_admin=True)
    assert models.admin_leader_flags(rid) == ["Lee"]
    assert "lee" not in _c(rid).closers_by_role.get("bartender", set())
    owner = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}
    assert models.adopt_admin_ratings(rid, owner) == 1
    assert "lee" in _c(rid).closers_by_role["bartender"]


def test_no_closer_of_the_role_able_to_work_is_a_notice_not_a_hard_breach():
    rid = _rid(close_times_json='{"Friday": "11:00pm"}')
    _closer_roster(rid)
    ss.upsert(rid, "Sam", daypart_availability={"Friday": "off"})
    c = _c(rid)
    rows = [_row(4, "Andrew", "Manager FOH", "3:00pm", "11:00pm", 8), _row(4, "Lee", "Bartender PM", "4:00pm", "10:00pm")]
    kinds = _kinds(rows, c)
    assert "closer_unavailable" in kinds and "keyholder_until_close" not in kinds


def test_close_out_runs_the_roles_closer_on_and_never_a_pinned_row():
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.close_times = {"Friday": "12:00am"}
    c.closers_by_role = {"bartender": {"sam"}}
    c.keyholders = {"sam"}
    c.close_mins = {"bartender": 30}
    rows = [_row(4, "Sam", "Bartender PM", "5:00pm", "11:00pm"), _row(4, "Lee", "Bartender PM", "5:00pm", "11:30pm", 6.5),
            _row(4, "Dee", "Dishwasher", "6:00pm", "12:30am", 6.5)]
    out = sr.close_out_gaps(rows, c)
    assert [x["employee"] for x in out["extended"]] == ["Sam"]
    assert out["rows"][0]["shift_end"] == "12:30am", "until close plus the bartenders' 30 minutes"
    assert "keyholder_until_close" not in _kinds(out["rows"], c)
    pinned = [dict(rows[0], _pinned="manager_plan")] + rows[1:]
    assert not sr.close_out_gaps(pinned, c)["extended"]


# ── D-13: role families ────────────────────────────────────────────────────

def test_the_section_cap_and_role_certificates_hold_for_every_job_code_of_the_role():
    rid = _rid(section_count=2, role_requirements_json='{"Bartender": ["alcohol"]}')
    for n, role in (("A", "Server AM"), ("B", "Server PM"), ("C", "Server PM"), ("D", "Bartender PM")):
        models.add_manual_team_member(rid, n, role=role)
    c = _c(rid)
    assert {"server am", "server pm"} <= c.foh_roles
    rows = [_row(4, n, "Server PM") for n in ("A", "B", "C")] + [_row(4, "D", "Bartender PM")]
    kinds = _kinds(rows, c)
    assert "over_section_cap" in kinds, "the default {server} cap matched no Server PM"
    assert "missing_cert" in kinds, "a Bartender PM without the alcohol certificate"


def test_the_owners_family_map_wins():
    rid = _rid(role_families_json='{"Barback PM": "Bartender"}')
    models.add_manual_team_member(rid, "Bo", role="Barback PM")
    c = _c(rid)
    assert c.family("Barback PM") == "bartender" and c.family("Server AM") == "server"


def test_cross_training_counts_roles_not_job_codes():
    shifts = [{"date": "2026-09-01", "employee": "Ana", "role": "Server AM", "shift_start": "10:00am"},
              {"date": "2026-09-02", "employee": "Ana", "role": "Server PM", "shift_start": "4:00pm"},
              {"date": "2026-09-01", "employee": "Bo", "role": "Server PM", "shift_start": "4:00pm"},
              {"date": "2026-09-02", "employee": "Bo", "role": "Bartender PM", "shift_start": "4:00pm"},
              {"date": "2026-09-03", "employee": "Cy", "role": "Training", "shift_start": "4:00pm"}]
    p = labor.historical_patterns(shifts)
    assert p["cross_trained"] == {"Bo": ["Bartender PM", "Server PM"]}
    assert not any("Training" in roles for roles in p["typical_headcount"].values())


def test_one_stays_after_close_setting_per_role_by_family():
    rid = _rid(close_times_json='{"Friday": "11:00pm"}', role_close_buffer_json='{"Bartender": 30}')
    models.add_manual_team_member(rid, "Lee", role="Bartender PM")
    c = _c(rid)
    assert c.close_mins == {"bartender": 30}
    early = [_row(4, "Lee", "Bartender PM", "4:00pm", "11:00pm", 7)]
    v = [x for x in sr.violations(early, c) if x["kind"] == "ends_before_role_close"]
    assert v and "11:30pm" in v[0]["detail"], "the allowance is the stay too (D-43)"
    caps = models.get_role_close_buffers(rid)
    assert sr.role_minutes(caps, "Bartender PM") == 30
    import schedule_engine
    row = {"role": "Bartender PM", "shift_start": "5:00pm", "shift_end": "11:30pm", "scheduled_hours": "6.5"}
    schedule_engine._enforce_close_time(row, "Friday", {"Friday": "11:00pm"}, caps)
    assert row["shift_end"] == "11:30pm", "never clamped at close while told to stay 30 min after it"


def test_the_two_close_settings_merge_without_losing_either():
    r = Restaurant(name="x", owner_email="x@x", role_close_buffer_json='{"Bartender": 60, "Barback": 20}',
                   role_close_min_json='{"bartender": 30}')
    assert sr.role_close_stays(r) == {"Barback": 20, "bartender": 30}
    assert sr.role_close_caps(r) == {"Bartender": 60, "Barback": 20}
    assert sr.role_close_conflicts(r) == [{"role": "bartender", "stays": 30, "may_run": 60}]


# ── D-14: the owner's staffing rules against AM/PM job codes ───────────────

def test_a_rule_naming_servers_is_checked_on_the_server_pm_job_code():
    import owner_memory
    rid = _rid()
    for n, role in (("A", "Server AM"), ("B", "Server PM"), ("C", "Bartender AM"), ("D", "Bartender PM")):
        models.add_manual_team_member(rid, n, role=role)
    owner = {"id": 11, "role": "client", "is_admin": 0, "username": "erik"}
    owner_memory.remember(rid, "Always two servers on Saturday night", kind="constraint", modules=["schedule"],
                          user=owner)
    owner_memory.remember(rid, "At least 1 bartender every day", kind="constraint", modules=["schedule"],
                          user=owner)
    c = _c(rid)
    assert not c.owner_rules_unchecked
    assert sr.floor_for(c.role_floors, "Server PM", "Saturday", "night") == 2
    reads = {r["text"]: r["reads_as"] for r in c.owner_rules}
    assert reads["Always two servers on Saturday night"] == "at least 2 Server PM on Sat at dinner/night"
    assert reads["At least 1 bartender every day"] == "at least 1 Bartender on every trading day"
    rows = [_row(5, "B", "Server PM"), _row(5, "C", "Bartender AM", "10:00am", "4:00pm")]
    kinds = _kinds(rows, c)
    assert "coverage_floor" in kinds and "owner_rule" not in kinds, "a Bartender AM is a bartender"


def test_a_studio_note_naming_servers_is_a_floor_on_the_dinner_code():
    import schedule_note_rules as snr
    rid = _rid()
    for n, role in (("A", "Server AM"), ("B", "Server PM"), ("C", "Barback PM")):
        models.add_manual_team_member(rid, n, role=role)
    item = snr.read_sentence("Keep two servers at dinner", snr.restaurant_roles(rid))
    assert item["kind"] == "rule" and item["rule"]["role"] == "Server" and item["rule"]["dayparts"] == ["night"]
    assert snr.read_sentence("Always 2 barbacks on Friday night", snr.restaurant_roles(rid))["rule"]["role"] == "Barback"
    snr.add_rule(rid, "Server", 2, ["night"], source_text="Keep two servers at dinner")
    c = _c(rid)
    assert sr.floor_for(c.role_floors, "Server PM", "Friday", "night") == 2
    assert not c.owner_rules_unchecked and not [r for r in snr.note_rules(rid) if r.get("stale")]
    assert sr.floor_for(sr.effective_role_floors(models.get_restaurant(rid), WEEK[0]), "Server PM", "Friday",
                        "night") == 2, "the cut surfaces read the same floor"


# ── D-15: held roles ──────────────────────────────────────────────────────

def test_held_roles_reach_the_rules_and_a_role_nobody_holds_is_flagged():
    rid = _rid()
    for n, role in (("Ana", "Server PM"), ("Bo", "Server PM"), ("Mgr", "Manager FOH")):
        models.add_manual_team_member(rid, n, role=role)
    people.add_role(rid, "Ana", "Bartender PM")
    c = _c(rid)
    assert c.held_roles == {"ana": {"bartender pm"}}
    assert c.known_roles["ana"] == {"server pm", "bartender pm"}
    rows = [_row(0, "Mgr", "Manager FOH", "3:00pm", "11:00pm", 8), _row(0, "Ana", "Bartender PM"),
            _row(0, "Bo", "Server AM", "10:00am", "4:00pm"), _row(1, "Bo", "Bartender AM", "10:00am", "4:00pm")]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "role_not_held"]
    assert [(x["employee"], x["role"]) for x in v] == [("Bo", "Bartender AM")], "Server AM is Bo's role; bar isn't"
    assert v[0]["hard"] and sr.BREACH_TIER["role_not_held"] == sr.TIER_PERSON
    ok, why = c.can_add(_row(2, "Bo", "Bartender PM"), rows)
    assert not ok and "hold" in why


def test_the_solver_takes_roles_from_what_people_hold_never_from_the_drafts_rows():
    import schedule_solver
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.roster_names = ["Ana", "Bo"]
    c.active = {"ana", "bo"}
    c.known_roles = {"ana": {"server pm"}, "bo": {"bartender pm"}}
    rows = [_row(0, "Ana", "Bartender PM"), _row(1, "Bo", "Bartender AM", "10:00am", "4:00pm"),
            _row(2, "Ana", "Server AM", "10:00am", "4:00pm")]
    signals = {"roster": ["Ana", "Bo"], "roster_roles": {"Ana": "Server PM", "Bo": "Bartender PM"}}
    prob = schedule_solver.Problem(rows, c, signals=signals)
    ana, bo = prob.pidx["ana"], prob.pidx["bo"]
    assert "bartender pm" not in prob.roles[ana], "the model's row is not a role Ana holds"
    assert {"server am", "server pm"} <= prob.roles[ana], "Server AM and Server PM are one role"
    assert {"bartender am", "bartender pm"} <= prob.roles[bo]


# ── D-16: trainees ────────────────────────────────────────────────────────

def test_a_trainee_is_not_coverage_is_paired_and_is_never_handed_a_shift():
    rid = _rid()
    for n, role in (("Ana", "Server PM"), ("Marcus", "Bartender PM"), ("Mgr", "Manager FOH")):
        models.add_manual_team_member(rid, n, role=role)
    ss.upsert(rid, "Ana", trainee={"target_role": "Bartender", "trainer": "Marcus", "until": "11/15/26"})
    sr.save_role_floors(rid, {"Bartender PM": {"night": 1}})
    c = _c(rid)
    assert c.trainees["ana"]["target_role"] == "Bartender"
    alone = [_row(0, "Mgr", "Manager FOH", "3:00pm", "11:00pm", 8), _row(0, "Ana", "Bartender PM")]
    kinds = _kinds(alone, c)
    assert "coverage_floor" in kinds, "a training shift is not a bartender on the floor"
    assert "trainee_unpaired" in kinds and "role_not_held" not in kinds
    paired = alone + [_row(0, "Marcus", "Bartender PM", "4:00pm", "11:00pm", 7)]
    kinds = _kinds(paired, c)
    assert "trainee_unpaired" not in kinds and "coverage_floor" not in kinds
    ok, why = c.can_add(_row(1, "Ana", "Bartender PM"), paired)
    assert not ok and "training" in why
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Ana", trainee={"target_role": "Bartender"})
    ss.upsert(rid, "Ana", trainee={})
    assert ss.for_name(rid, "Ana")["trainee"] is None


def test_a_training_job_code_is_never_a_role():
    assert sr.is_training_role("Training") and sr.is_training_role("Server Trainee")
    assert not sr.is_training_role("Trainer") and not sr.is_training_role("Server")


# ── the prompt says it ────────────────────────────────────────────────────

def test_the_model_is_told_closers_per_role_acting_managers_and_trainees():
    rid = _rid(close_times_json='{"Friday": "12:00am"}')
    _closer_roster(rid)
    ss.upsert(rid, "Ana", acting_manager=[{"from": WEEK[1], "until": WEEK[1]}],
              trainee={"target_role": "Bartender", "trainer": "Sam", "until": "12/1/26"})
    block = sr.prompt_block(_c(rid))
    assert "Closers, by role" in block and "Bartender: Sam" in block and "Dishwasher: Dee" in block
    assert "Standing in as the manager" in block and "10/13/26" in block
    assert "Ana: training as Bartender until 12/1/26 with Sam" in block
