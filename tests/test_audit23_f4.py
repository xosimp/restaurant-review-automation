"""Audit #23 fix round (10/21/26), group F4 — labor semantics.

A3-14 / A2-18 / A2-24 one business-date boundary; A3-02 / A3-09 the rate
chain; A3-03 station logins by behaviour; A3-04 RPOWER's own overtime pay;
A3-01 / A6-08 open punches; A3-05 restating a reported night's labor;
A3-06 punch edits after 14 days; A3-08 name disambiguation among current
staff; A3-10 nightly sub-steps on the ledger; A3-15 breaks in live labor;
A3-19 roles keyed on the POS job; A3-20 salaried out once; A6-13 a store
with no punches."""
import json
from datetime import date, datetime, timedelta

import pytest

import models
import pos
import pos_archive
import pos_io
import rpower
import time_utils


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(pos, "PROVIDERS", None)
    pos_io.reset_for_tests()
    rpower.clear_caches()
    yield
    pos_io.reset_for_tests()


def _restaurant(rid=1, **kw):
    conn = models.get_conn()
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test", "rpower_token": "tok",
            "rpower_cg": 1280, "rpower_store_mid": f"store-{rid}", "timezone": "America/Chicago"}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _q(sql, args=()):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


class _R:
    """A restaurant-shaped object with hours, for the pure time rules."""
    def __init__(self, opens=None, closes=None, tz="America/Chicago"):
        self.open_times_json = json.dumps(opens or {})
        self.close_times_json = json.dumps(closes or {})
        self.timezone = tz
        self.id = 1


# ── A3-14 / A2-24: one business-date boundary ────────────────────────────────

def test_the_business_date_turns_over_where_the_punches_do():
    """4:30am is the morning's for "today" exactly as for a punch (both
    turned over at different hours: 5am vs 4am)."""
    r = _R()
    assert time_utils.day_start_minutes(r) == 4 * 60
    assert time_utils.business_date(r, datetime(2026, 10, 21, 4, 30)) == date(2026, 10, 21)
    assert time_utils.business_date(r, datetime(2026, 10, 21, 3, 59)) == date(2026, 10, 20)
    assert rpower.punch_business_date(datetime(2026, 10, 21, 4, 30)) == date(2026, 10, 21)


def test_a_store_that_trades_past_4am_turns_its_day_over_at_its_own_close():
    days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    late = _R(opens={d: "4:00pm" for d in days}, closes={"Saturday": "5:00am", "Friday": "4:00am"})
    assert time_utils.day_start_minutes(late) == 5 * 60
    # A cafe's 3pm close is the same day's, never a boundary.
    cafe = _R(opens={d: "7:00am" for d in days}, closes={d: "3:00pm" for d in days})
    assert time_utils.day_start_minutes(cafe) == 4 * 60
    # A punch at 4:30 at the late bar is the night's.
    assert rpower.punch_business_date(datetime(2026, 10, 25, 4, 30), 5 * 60) == date(2026, 10, 24)
    assert time_utils.business_date(late, datetime(2026, 10, 22, 4, 30)) == date(2026, 10, 21)


def test_the_punch_window_is_the_stores_boundary():
    rid = _restaurant(close_times_json=json.dumps({"Tuesday": "4:30am"}),
                      open_times_json=json.dumps({"Tuesday": "6:00pm"}))
    start, end = rpower._punch_window(date(2026, 10, 20), rid)
    assert start == datetime(2026, 10, 20, 4, 30) and end == datetime(2026, 10, 21, 4, 30)
    assert rpower._punch_window(date(2026, 10, 20))[0] == datetime(2026, 10, 20, 4, 0)


def test_a_night_is_complete_at_the_same_boundary():
    r = _R()
    assert pos.complete_through(r, datetime(2026, 10, 21, 4, 30)) == date(2026, 10, 20)
    assert pos.complete_through(r, datetime(2026, 10, 21, 3, 30)) == date(2026, 10, 19)


def test_tip_windows_and_depletion_use_the_restaurants_own_date():
    import inspect
    import scheduler
    import tip_opportunity
    assert "date.today()" not in inspect.getsource(tip_opportunity._worked).split("except Exception")[0]
    assert "pos.local_complete_through(restaurant_id)" in inspect.getsource(tip_opportunity._worked)
    assert "end = pos.complete_through(r)" in inspect.getsource(scheduler.run_daily_depletion_sync)


# ── A3-02 / A3-09: the rate chain ───────────────────────────────────────────

import labor
import schedule_objective as so


def _s(emp, role, day, pay="", **kw):
    return dict({"employee": emp, "role": role, "date": day, "pay_rate": pay, "actual_hours": 6}, **kw)


def test_a_zero_punch_is_priced_at_the_owners_role_rate_before_another_jobs_pay():
    """Amy: Barback AM paid $11, Bartender PM $0, the owner's Bartender PM
    rate $9 — the PM hours cost $9 (they cost $11, source "person")."""
    shifts = [_s("Amy", "Barback AM", "2026-10-01", 11), _s("Amy", "Bartender PM", "2026-10-02")]
    people, typical = so.rate_book(shifts, {})
    assert so._shift_rate_source(shifts[1], {"Bartender PM": 9.0}, 26.0, people, typical) == (9.0, "role_rate")
    # No owner rate for the role: her own pay in another job is still hers.
    assert so._shift_rate_source(shifts[1], {}, 26.0, people, typical) == (11.0, "person")


def test_the_owners_person_rate_beats_the_role_rate_and_a_plain_dict_is_the_owners():
    shifts = [_s("Amy", "Bartender PM", "2026-10-02")]
    people, typical = so.rate_book(shifts, {"amy": 14.0})
    assert so._shift_rate_source(shifts[0], {"Bartender PM": 9.0}, 26.0, people, typical) == (14.0, "person")
    assert so._shift_rate_source(shifts[0], {"Bartender PM": 9.0}, 26.0, {"amy": 14.0}, {}) == (14.0, "person")


def test_a_past_punch_is_priced_at_the_rate_in_force_then_and_the_same_job_first():
    shifts = [_s("Bo", "Cook", "2026-09-01", 18), _s("Bo", "Cook", "2026-10-01", 21),
              _s("Bo", "Prep", "2026-10-05", 30),
              _s("Bo", "Cook", "2026-09-15"), _s("Bo", "Cook", "2026-10-15")]
    people, typical = so.rate_book(shifts, {})
    assert people["bo"] == 30.0                             # latest any job: what pricing a draft reads
    assert so._shift_rate_source(shifts[3], {}, 26.0, people, typical) == (18.0, "person")
    assert so._shift_rate_source(shifts[4], {}, 26.0, people, typical) == (21.0, "person")


def test_rpowers_job_rate_prices_an_hour_nobody_else_does():
    shifts = [_s("Cy", "Host PM", "2026-10-02", job_rate="12.5")]
    people, typical = so.rate_book(shifts, {})
    assert so._shift_rate_source(shifts[0], {}, 26.0, people, typical) == (12.5, "job_rate")
    assert "job_rate" in so.RATE_SOURCES and "job_rate" in labor.RATE_SOURCE_WORDS


def test_the_labor_rows_carry_rpowers_job_rate_and_job_id(db_path, monkeypatch):
    rid = _restaurant()
    people = {"jobs": {"7777": "Kitchen"}, "employees": {"4444": "Dana Reyes"}, "job_rates": {"7777": 17.5}}
    rows = rpower.normalise_entries([{"in_dttm": "2026-09-10T08:00:00", "out_dttm": "2026-09-10T16:00:00",
                                      "emp_mid": "4444", "job_mid": "7777", "reg_hours": 8}], {}, people)
    assert rows[0]["job_ext_id"] == "7777" and rows[0]["job_rate"] == 17.5 and rows[0]["pay_rate"] == ""


def test_rpowers_salaried_flags_are_proposed_on_the_setup_step(db_path, monkeypatch):
    import onboarding
    rid = _restaurant()
    onboarding.start_setup(rid)
    assert labor.suggest_salaried(rid, ["Erik Baylis", "Jim Bo"]) == ["Erik Baylis", "Jim Bo"]
    got = onboarding.confirmations(rid)["salaried"]
    assert got["status"] == "pending" and got["suggested"] == {"names": ["Erik Baylis", "Jim Bo"]}
    entries = [{"emp_mid": "1", "job_mid": "9", "in_dttm": "2026-10-01T09:00:00", "out_dttm": "2026-10-01T17:00:00"},
               {"emp_mid": "2", "job_mid": "8", "in_dttm": "2026-10-01T09:00:00", "out_dttm": "2026-10-01T17:00:00"}]
    monkeypatch.setattr(rpower, "fetch_employee_jobs", lambda r: [{"external_id": "2", "salaried": True}])
    ppl = {"employees": {"1": "Erik Baylis", "2": "Jim Bo"}, "salaried_jobs": {"9"}, "current": {"1", "2"}}
    assert rpower._salaried_names(rid, entries, ppl) == ["Erik Baylis", "Jim Bo"]


# ── A3-03: station logins ───────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["Bar 1", "Patio Bar 2", "Online Orders", "Doordash", "Rooftop Bar", "Beer Garden"])
def test_numbered_and_channel_named_logins_are_stations(name):
    assert rpower.is_station_name(name)


@pytest.mark.parametrize("name", ["John Smith", "Amy Baylis", "7", "Bar Smith"])
def test_people_are_not_stations(name):
    assert not rpower.is_station_name(name)


def _pair(day, a_in="09:51", a_out="15:31", job="7"):
    return [{"emp_mid": "A", "job_mid": job, "reg_rate": 11, "in_dttm": f"{day}T{a_in}:00", "out_dttm": f"{day}T{a_out}:00"},
            {"emp_mid": "S", "job_mid": job, "in_dttm": f"{day}T{a_in}:00", "out_dttm": f"{day}T{a_out}:00"}]


def test_a_record_that_clocks_in_beside_someone_on_every_shift_is_a_station_candidate():
    entries = _pair("2026-10-01") + _pair("2026-10-02") + _pair("2026-10-03")
    people = {"employees": {"A": "Amy Baylis", "S": "Sunset Crew"}}
    got = rpower.station_candidates(entries, people)
    assert list(got) == ["S"] and got["S"]["beside"] == ["Amy Baylis"] and got["S"]["punches"] == 3
    # One shift of its own (a trainee working alone): a person.
    entries.append({"emp_mid": "S", "job_mid": "7", "in_dttm": "2026-10-04T09:00:00", "out_dttm": "2026-10-04T13:00:00"})
    assert rpower.station_candidates(entries, people) == {}
    # Behaviour only proposes: nothing is left out of labor until the owner says.
    assert rpower.station_logins(entries, people) == {}


def test_the_owners_station_list_decides(db_path):
    import onboarding
    rid = _restaurant()
    onboarding.start_setup(rid)
    rpower.set_station_verdict(rid, "S", "Sunset Crew", True, who="owner:t")
    rpower.set_station_verdict(rid, "X", "Day Bar", False, who="owner:t")
    owner = rpower.station_owner(rid)
    assert owner == {"stations": {"S": "Sunset Crew"}, "people": {"X": "Day Bar"}}
    people = {"station_names": {"X": "Day Bar"}, "station_owner": owner}
    # "Day Bar" is a station by name, but the owner said it is a person.
    assert rpower.station_logins([], people) == {"S": "Sunset Crew"}
    rows = rpower.normalise_entries(_pair("2026-10-01"), {}, dict(people, employees={"A": "Amy", "S": "Sunset Crew"}))
    assert [r["employee"] for r in rows] == ["Amy"]


def test_a_confirmed_suggestion_without_a_value_is_the_station_list(db_path):
    import onboarding
    rid = _restaurant()
    onboarding.start_setup(rid)
    labor.record_station_candidates(rid, {"S": {"name": "Sunset Crew", "punches": 3, "beside": ["Amy"]}})
    assert [c["ref"] for c in labor.flags(rid, "station_candidate")] == ["S"]
    onboarding.confirm(rid, "station_logins")
    assert rpower.station_owner(rid)["stations"] == {"S": "Sunset Crew"}
    notes = labor.data_health_notes(rid)
    assert notes[0]["action"]["kind"] == "station" and "Sunset Crew" in notes[0]["text"]


def test_the_station_answer_has_web_and_mobile_twins():
    import strategy_routes
    eps = {e for _p, _m, _f, e in strategy_routes._ROUTES}
    assert "data_health_station" in eps


# ── A3-08: names among current staff ────────────────────────────────────────

def test_a_former_employees_name_never_renames_the_current_one():
    recs = [{"mid": "1", "fname": "John", "lname": "Smith", "payroll_id": "OLD111", "term_date": "2024-03-01T00:00:00"},
            {"mid": "2", "fname": "John", "lname": "Smith", "payroll_id": "NEW222", "term_date": "2000-01-01T00:00:00"}]
    got = rpower.distinct_employee_names(recs, today=date(2026, 10, 21))
    assert got["2"] == "John Smith" and got["1"] == "John Smith (OLD111)"
    # Two current ones are still told apart.
    recs[0]["term_date"] = ""
    got = rpower.distinct_employee_names(recs, today=date(2026, 10, 21))
    assert got["1"] != got["2"] and "OLD111" in got["1"] and "NEW222" in got["2"]


# ── A3-19: a job renamed in the POS keeps the name it is filed under ────────

def test_a_renamed_job_keeps_its_first_name(db_path):
    rid = _restaurant()
    assert rpower._stable_job_names(rid, {"7": "Server PM"}) == ({"7": "Server PM"}, {})
    names, renames = rpower._stable_job_names(rid, {"7": "Server Night", "8": "Host"})
    assert names == {"7": "Server PM", "8": "Host"} and renames == {"7": ("Server PM", "Server Night")}
    labor.record_job_renames(rid, renames)
    assert "Server Night" in labor.data_health_notes(rid)[0]["text"]


# ── A3-04: RPOWER's own overtime ────────────────────────────────────────────

def test_the_ot_premium_is_read_from_the_punch():
    assert rpower._ot_premium({"ot_hours": 0, "dt_hours": 0}) == 0.0
    assert rpower._ot_premium({"ot_hours": 2, "reg_rate": 20, "ot_rate": 30}) == 20.0
    assert rpower._ot_premium({"ot_hours": 2, "reg_rate": 20, "ot_pay": 60}) == 20.0     # whole OT pay
    assert rpower._ot_premium({"ot_hours": 2, "reg_rate": 20, "ot_pay": 20}) == 20.0     # premium only
    assert rpower._ot_premium({"ot_hours": 2, "reg_rate": 20, "ot_pay": 40}) is None     # can't tell
    assert rpower._ot_premium({"ot_hours": 2, "reg_rate": 0}) is None                    # a $0 punch
    assert rpower._ot_premium({"ot_hours": 1, "dt_hours": 1, "reg_rate": 10, "ot_pay": 35,
                               "_ot_pay_combined": True}) == 15.0


def test_labor_uses_the_poss_overtime_not_a_week_of_its_own():
    """Ana works 48h Mon-Sat on Cavnar's Monday week, but RPOWER's payroll
    week split it differently: 2 OT hours on Saturday's punch, $30 paid
    over straight. The premium is RPOWER's $30, on Saturday."""
    rows = []
    for i, d in enumerate(("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17")):
        last = i == 5
        rows.append({"date": d, "employee": "Ana", "role": "Cook", "actual_hours": 8, "scheduled_hours": 8,
                     "pay_rate": 20, "sales": 2000, "pos_ot_hours": "2" if last else "0", "pos_dt_hours": "0",
                     "pos_ot_premium": "30" if last else "0", "shift_start": "09:00", "shift_end": "17:00"})
    out = labor.analyse_shifts(rows, hourly_rate=20, labor_target=30, role_rates={"_default": 20})
    straight = 48 * 20
    assert round(out["total_labor_cost"], 2) == straight + 30
    # A punch that could not say what was paid: its OT hours at half the hour's rate.
    rows[-1]["pos_ot_premium"] = ""
    out = labor.analyse_shifts(rows, hourly_rate=20, labor_target=30, role_rates={"_default": 20})
    assert round(out["total_labor_cost"], 2) == straight + 20
    # Without the POS's word (an upload): derived on the 40-hour week, as before.
    for r in rows:
        for k in ("pos_ot_hours", "pos_dt_hours", "pos_ot_premium"):
            r.pop(k)
    out = labor.analyse_shifts(rows, hourly_rate=20, labor_target=30, role_rates={"_default": 20})
    assert abs(out["total_labor_cost"] - (straight + 8 * 20 * 0.5)) < 0.05


# ── A3-01 / A6-08: open punches ─────────────────────────────────────────────

def test_open_punches_are_found_and_never_a_station():
    entries = [{"rid": "p1", "emp_mid": "A", "job_mid": "7", "in_dttm": "2026-10-20T17:00:00",
                "out_dttm": "2000-01-01T00:00:00"},
               {"rid": "p2", "emp_mid": "S", "job_mid": "7", "in_dttm": "2026-10-20T17:00:00", "out_dttm": None},
               {"rid": "p3", "emp_mid": "A", "job_mid": "7", "in_dttm": "2026-10-19T17:00:00",
                "out_dttm": "2026-10-19T23:00:00"}]
    people = {"employees": {"A": "Amy"}, "jobs": {"7": "Bartender"}, "station_owner": {"stations": {"S": "Bar 1"}}}
    got = rpower.open_punches(entries, people)
    assert [(p["punch_id"], p["employee"], p["business_date"], p["role"]) for p in got] == \
        [("p1", "Amy", "2026-10-20", "Bartender")]


def test_a_date_with_an_open_punch_is_held_provisional(db_path):
    rid = _restaurant()
    by_day = {"2026-10-18": {"sales": 2000, "actual": 30, "labor_cost": 600},
              "2026-10-19": {"sales": 2000, "actual": 30, "labor_cost": 600}}
    models.save_labor_daily_history(rid, by_day, provenance={
        "source": "rpower", "provider": "rpower", "synced_at": "2026-10-20 08:00:00",
        "window": ("2026-10-01", "2026-10-20"), "complete_through": "2026-10-19", "held_dates": ["2026-10-18"]})
    final = {r["date"]: r["final"] for r in _q("SELECT date, final FROM labor_daily_history WHERE restaurant_id=?", (rid,))}
    assert final == {"2026-10-18": 0, "2026-10-19": 1}


def test_open_punches_are_kept_estimated_tasked_and_closed(db_path, monkeypatch):
    rid = _restaurant(open_times_json=json.dumps({"Sunday": "4:00pm"}),
                      close_times_json=json.dumps({"Sunday": "11:00pm"}), hourly_rate=20.0)
    punch = {"punch_id": "p1", "business_date": "2026-10-18", "employee": "Amy Baylis", "employee_id": "A",
             "role": "Bartender", "clock_in": "2026-10-18T17:00", "pay_rate": 15.0, "job_rate": None}
    now = datetime(2026, 10, 20, 3, 0)
    out = labor.record_open_punches(rid, [punch], window=("2026-08-21", "2026-10-20"), now_local=now)
    assert out["count"] == 1 and out["tasks_opened"] == 1
    row = labor.flags(rid, "open_punch")[0]
    assert (row["est_hours"], row["est_cost"], row["est_basis"]) == (6.0, 90.0, "to the store's close")
    issue = _q("SELECT kind, status, title FROM ops_issues WHERE restaurant_id=? AND source_key=?",
               (rid, "open_punch:p1"))[0]
    assert issue["kind"] == "labor" and issue["status"] == "open" and "Amy Baylis" in issue["title"]
    s = labor.open_punch_summary(rid)
    assert s["count"] == 1 and "Amy Baylis" in s["line"] and "10/18/26" in s["line"] and "$90" in s["line"]
    assert "Amy" not in labor.open_punch_summary(rid, with_names=False)["line"]
    # The punch is closed in the POS: the flag goes and the task closes.
    out = labor.record_open_punches(rid, [], window=("2026-08-21", "2026-10-20"), now_local=now)
    assert labor.flags(rid, "open_punch") == [] and out["tasks_closed"] == 1
    assert _q("SELECT status FROM ops_issues WHERE source_key='open_punch:p1'")[0]["status"] == "resolved"


def test_a_punch_open_under_sixteen_hours_is_no_task(db_path):
    rid = _restaurant()
    punch = {"punch_id": "p2", "business_date": "2026-10-20", "employee": "Bo", "clock_in": "2026-10-20T22:00"}
    out = labor.record_open_punches(rid, [punch], now_local=datetime(2026, 10, 21, 3, 0))
    assert out["tasks_opened"] == 0 and labor.flags(rid, "open_punch")[0]["est_hours"] is None


def test_the_labor_page_says_the_open_punches(db_path):
    import inspect
    src = open("templates/dashboard.html", encoding="utf-8").read()
    assert "labor.open_punches.line" in src and "ld.open_punches" in src and "dhLaborNotesHtml" in src
    assert "result['open_punches'] = open_punch_summary(restaurant_id)" in inspect.getsource(labor._analyse_for_restaurant)


# ── the DSR's Labor block: open punches, salaried out once, restating ───────

import sys

import dsr
from dsr import block_labor, store as dsr_store


DSR_DAY = date(2026, 9, 22)


def _dsr_ctx(rid, now=datetime(2026, 9, 23, 5, 0)):
    ctx = dsr.Context(models.get_restaurant(rid), DSR_DAY, now_utc=now)
    ctx.day_closed = "pos"
    return ctx


def _hist(rid, cost=400.0, hours=30.0, final=1, day="2026-09-22"):
    conn = models.get_conn()
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, "
                 "total_hours, final) VALUES (?,?,?,?,?,?,?,?)", (rid, day, "Tuesday", 20.0, cost, 2000.0, hours, final))
    conn.commit()
    conn.close()


def _open_flag(rid, day="2026-09-22", ref="p1"):
    labor._replace_flags(rid, "open_punch", [{"ref": ref, "business_date": day, "detail": {
        "employee": "Amy Baylis", "role": "Bartender", "clock_in": f"{day}T17:00", "est_hours": 6.0,
        "est_cost": 90.0, "est_basis": "to the store's close"}}])


def test_a_night_held_only_by_an_open_punch_is_ready_and_says_so(db_path, monkeypatch):
    rid = _restaurant(hourly_rate=18.0, hourly_rate_source="set")
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    _hist(rid, final=0)
    _open_flag(rid)
    # Past the night (5am CDT 9/23 = 10:00 UTC): the only gap is the punch.
    ctx = _dsr_ctx(rid, now=datetime(2026, 9, 23, 10, 0))
    ctx.blocks["sales"] = dsr.block(dsr.READY, metrics={"net": 2000.0})
    b = block_labor.collect(ctx)
    assert b["status"] == dsr.READY and b["metrics"]["open_punches"] == 1
    assert b["detail"]["open_punches"]["people"][0]["employee"] == "Amy Baylis"
    assert any(o["key"] == "open_punches" and "still open" in o["text"] for o in b["detail"]["observations"])
    # A held row with no open punch is still a night not yet whole.
    labor._replace_flags(rid, "open_punch", [])
    b = block_labor.collect(ctx)
    assert b["status"] == dsr.UNAVAILABLE and b["reason"] == block_labor.REASON_PARTIAL


def test_the_salaried_are_left_out_of_the_nights_rows_once(db_path, monkeypatch):
    rid = _restaurant(hourly_rate=18.0, hourly_rate_source="set",
                      salaried_staff_json=json.dumps([{"name": "Erik Baylis", "annual": 150000}]))
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    _hist(rid, hours=6.0)
    head = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate\n"
    body = ("2026-09-22,Tuesday,Ana B,Server,11:00,17:00,6,6,,,15\n"
            "2026-09-22,Tuesday,Erik Baylis,Manager FOH,18:00,23:00,5,5,,,\n")
    models.save_client_data(rid, "shifts", head + body, source="test")
    ctx = _dsr_ctx(rid)
    ctx.blocks["sales"] = dsr.block(dsr.READY, metrics={"net": 2000.0, "evening_share_pct": 60.0})
    b = block_labor.collect(ctx)
    # Erik's evening hours are not in the after-6pm share, and Ana's POS pay
    # is the whole night's (his $0 punch used to pull the share under half).
    assert b["metrics"]["hours_after_6pm"] == 0.0 and b["detail"]["pos_pay_share_pct"] == 100


def _report(rid, cost=400.0, hours=30.0, open_n=0):
    rep = dsr_store.create_report(rid, "2026-09-22")
    dsr_store.save_block(rep["id"], "sales", dsr.block(dsr.READY, metrics={"net": 2000.0}))
    dsr_store.save_block(rep["id"], "labor", dsr.block(
        dsr.READY, metrics={"cost": cost, "hours": hours, "pct": 20.0, "target_pct": 30.0, "vs_target_pts": -10.0,
                            "open_punches": open_n},
        detail={"departments": [{"department": "Kitchen", "cost": cost, "hours": hours, "pct_of_sales": 20.0}],
                "observations": []}, block_name="labor"))
    dsr_store.set_stage(rep["id"], "final")
    return rep["id"]


def test_a_reported_nights_labor_is_restated_when_the_archive_moves(db_path):
    rid = _restaurant()
    rep_id = _report(rid)
    _hist(rid, cost=470.0, hours=32.0)
    out = block_labor.restate(rid, ["2026-09-22"], now=datetime(2026, 10, 21, 8, 0))
    assert out["restated"] == ["2026-09-22"]
    rep = dsr_store.get_finished_report(rid, "2026-09-22")
    m = rep["facts"]["blocks"]["labor"]["metrics"]
    assert (m["cost"], m["hours"], m["pct"], m["vs_target_pts"]) == (470.0, 32.0, 23.5, -6.5)
    d = rep["facts"]["blocks"]["labor"]["detail"]
    assert d["restated"]["was"]["cost"] == 400.0 and "Updated 10/21/26" in d["restated"]["line"]
    assert d["departments"][0]["cost"] == 470.0
    assert rep["stages"]["restated"]["labor"]["was"]["hours"] == 30.0
    series = {r["metric"]: r["value"] for r in _q("SELECT metric, value FROM dsr_metrics WHERE restaurant_id=? "
                                                   "AND business_date='2026-09-22'", (rid,))}
    assert series["labor.cost"] == 470.0
    # Within the tolerance: left as reported.
    conn = models.get_conn()
    conn.execute("UPDATE labor_daily_history SET labor_cost=471.0 WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    assert block_labor.restate(rid, ["2026-09-22"])["restated"] == []


def test_a_held_night_is_never_restated_and_a_closed_open_punch_is(db_path):
    rid = _restaurant()
    _report(rid, open_n=1)
    _hist(rid, cost=400.0, hours=30.0, final=0)
    assert block_labor.restate(rid, ["2026-09-22"])["checked"] == 0
    conn = models.get_conn()
    conn.execute("UPDATE labor_daily_history SET final=1 WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    assert block_labor.restate(rid, ["2026-09-22"])["restated"] == ["2026-09-22"]
    m = dsr_store.get_finished_report(rid, "2026-09-22")["facts"]["blocks"]["labor"]["metrics"]
    assert m["open_punches"] == 0


def test_the_sync_restates_the_window_it_pulled():
    import inspect
    assert "_restate_labor(restaurant_id, dates)" in inspect.getsource(pos.save_synced_shifts)


# ── A3-15: live labor costs no break ────────────────────────────────────────

def test_live_labor_hours_leave_out_breaks(db_path, monkeypatch):
    import labor_live
    rid = _restaurant()
    r = models.get_restaurant(rid)
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda rid_, day: ([
        {"employee": "Ana", "role": "Cook", "clocked_in_at": "2026-10-21T09:00:00", "clocked_out_at": "2026-10-21T17:00:00",
         "hourly_wage": 20.0, "paid_hours": 7.5},
        {"employee": "Bo", "role": "Cook", "clocked_in_at": "2026-10-21T10:00:00", "clocked_out_at": None,
         "hourly_wage": 20.0, "break_minutes": 30}], "rpower"))
    out = labor_live._labor(r, date(2026, 10, 21), datetime(2026, 10, 21, 18, 0), [],
                            rates_t=({}, {}, {}, 20.0))
    # Ana: payroll's 7.5h, not the clock's 8; Bo: 8h on the clock less 30 min.
    assert out["hours_to_date"] == 15.0 and out["to_date"] == 300.0


def test_the_live_clock_ins_carry_payroll_hours_and_breaks(db_path, monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr(rpower, "fetch_time_entries", lambda r, s, e: [
        {"rid": "1", "emp_mid": "4", "job_mid": "7", "in_dttm": "2026-10-21T09:00:00", "out_dttm": "2026-10-21T17:00:00",
         "reg_hours": 7.5, "break_minutes": 30},
        {"rid": "2", "emp_mid": "5", "job_mid": "7", "in_dttm": "2026-10-21T10:00:00", "out_dttm": "2000-01-01T00:00:00",
         "meal1_minutes": 20}])
    monkeypatch.setattr(rpower, "_people_for", lambda r, e: {"employees": {"4": "Ana", "5": "Bo"}, "jobs": {"7": "Cook"}})
    rows = {r["employee"]: r for r in rpower.fetch_clock_ins_today(rid, date(2026, 10, 21))}
    assert rows["Ana"]["paid_hours"] == 7.5 and rows["Bo"]["paid_hours"] is None and rows["Bo"]["break_minutes"] == 20


# ── A3-10: the nightly sub-steps on the ledger ──────────────────────────────

def test_a_failed_nightly_sub_step_is_on_the_data_health_ledger(db_path, monkeypatch):
    import data_health
    import inventory_ledger
    rid = _restaurant()
    monkeypatch.setattr(rpower, "sweep_punch_edits", lambda r: {"due": False})
    monkeypatch.setattr(rpower, "_labor_read", lambda r, days=60: {"csv": (
        "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate\n"
        "2026-10-19,Monday,Ana B,Cook,09:00,17:00,8,8,2000,,20\n")})
    monkeypatch.setattr(pos, "save_synced_shifts", lambda *a, **k: 1)
    monkeypatch.setattr(inventory_ledger, "discover_menu_items", lambda r, days=2: (_ for _ in ()).throw(
        RuntimeError("menuitem/getbycg 500")))
    monkeypatch.setattr(inventory_ledger, "backfill_item_sales", lambda r, days=60: None)
    out = rpower.sync_to_db(rid)
    assert out["ok"] is True and out["partial"] == ["menu_items"]
    rows = data_health.health_rows(rid)
    assert rows["pos_menu_items"]["consecutive_failures"] == 1 and "500" in rows["pos_menu_items"]["last_error"]
    assert rows["pos_item_backfill"]["consecutive_failures"] == 0


def test_the_archives_sub_steps_record_their_own_source():
    import inspect
    src = inspect.getsource(pos_archive.run_for)
    for s in ("pos_catalog", "pos_roles", "pos_prices"):
        assert f'_step(restaurant_id, "{s}", False' in src and f'_step(restaurant_id, "{s}", True' in src
    assert '"partial": steps_failed' in src


# ── A6-13: a store with no timeclock punches ────────────────────────────────

def test_a_store_with_no_punches_is_a_state_and_its_sales_are_kept(db_path, monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr(rpower, "sweep_punch_edits", lambda r: {"due": False})
    monkeypatch.setattr(rpower, "_labor_read", lambda r, days=60: {
        "rows": [], "open": [], "sales": {"2026-10-18": 2500.0, "2026-10-19": 0}, "window": ("2026-08-20", "2026-10-19")})
    monkeypatch.setattr(pos, "_complete_through_for", lambda r: date(2026, 10, 19))
    out = rpower.sync_to_db(rid)
    assert out["ok"] is True and out["no_punches"] is True and out["sales_days"] == 1
    r = models.get_restaurant(rid)
    assert r.rpower_sync_error is None and r.rpower_last_synced
    row = _q("SELECT sales, labor_cost, labor_pct, total_hours, final FROM labor_daily_history WHERE restaurant_id=?",
             (rid,))
    assert row == [{"sales": 2500.0, "labor_cost": None, "labor_pct": None, "total_hours": None, "final": 1}]


# ── A3-06: punch edits after the live window ────────────────────────────────

def test_punch_edits_older_than_the_live_window_are_swept_weekly(db_path, monkeypatch):
    rid = _restaurant()
    today = date(2026, 10, 21)
    conn = models.get_conn()
    conn.execute("INSERT INTO pos_archive_days (restaurant_id, provider, business_date) VALUES (?, 'rpower', ?)",
                 (rid, "2026-09-30"))
    conn.execute("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date, employee_id, clock_in, "
                 "clock_out, reg_hours, edited_by, edited_at) VALUES (?, 'rpower', 'p9', '2026-09-30', 'E', "
                 "'2026-09-30T09:00:00', '2026-09-30T18:00:00', 9, 'M1', '2026-09-30T20:00:00')", (rid,))
    conn.commit()
    conn.close()
    asked = []

    def paged(token, path, params, max_pages=200):
        asked.append((path, params.get("editmgrmid")))
        return [{"rid": "p9", "in_dttm": "2026-09-30T09:00:00", "edit_dttm": "2026-10-16T11:00:00"},
                {"rid": "p8", "in_dttm": "2026-10-15T09:00:00", "edit_dttm": "2026-10-16T11:00:00"}]
    monkeypatch.setattr(rpower, "_paged", paged)
    monkeypatch.setattr(rpower, "_people_cached", lambda r: {"employees": {"E": "Eve"}, "jobs": {}})
    monkeypatch.setattr(rpower, "fetch_time_entries", lambda r, s, e: [
        {"rid": "p9", "emp_mid": "E", "in_dttm": "2026-09-30T09:00:00", "out_dttm": "2026-09-30T15:00:00",
         "reg_hours": 6, "editmgr_mid": "M1", "edit_dttm": "2026-10-16T11:00:00"}])
    out = rpower.sweep_punch_edits(rid, today=today)
    assert out["due"] and out["managers"] == 1 and out["dates"] == ["2026-09-30"] and out["stored"] == 1
    assert asked == [("timeclock/getbyeditmanager", "M1")]
    assert _q("SELECT reg_hours FROM pos_punches WHERE restaurant_id=? AND punch_id='p9'", (rid,))[0]["reg_hours"] == 6
    # Not due again for a week.
    assert rpower.sweep_punch_edits(rid, today=today + timedelta(days=3))["due"] is False
    assert rpower.sweep_punch_edits(rid, today=today + timedelta(days=7))["due"] is True
