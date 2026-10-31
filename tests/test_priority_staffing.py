"""Priority on busy nights (owner, 10/30/26 — Simple EJ's: "Sloane, Emily
and Mia primarily for busy nights").

A person is marked Priority in a role family (staff settings'
`priority_roles`, ["host"]); on the week's busy shifts
(schedule_requirements.priority_busy_shifts — the top quartile of shift
demand, Friday and Saturday night unless the restaurant's own demand puts
them below the week's middle, and any night with a demand signal the
schedule plans on) the solvers fill the role from Priority people first: a
preference above every ordinary one (dollars, fairness, stability), never
above availability, the hours maximum or overtime.
"""
from datetime import timedelta

import pytest
from flask import Flask

import auth
import models
import schedule_cpsat as cs
import schedule_requirements as sreq
import schedule_rules as sr
import schedule_solver as ss
import shift_quality as sq
import staff_settings
from models import Restaurant, create_restaurant
from tests.clock import next_monday

WEEK = [(next_monday() + timedelta(days=i)).isoformat() for i in range(7)]
MON, TUE, WED, THU, FRI, SAT, SUN = WEEK


# ── which shifts are busy ────────────────────────────────────────────────

def _req(night=None, morning=None):
    """{(date, part): {"Server": n}} — `night`/`morning` {date: n}, every
    other date's shifts at 3."""
    out = {}
    for d in WEEK:
        out[(d, "night")] = {"Server": (night or {}).get(d, 3)}
        out[(d, "morning")] = {"Server": (morning or {}).get(d, 2)}
    return out


def test_the_top_quartile_of_the_weeks_demand_is_busy():
    req = _req(night={THU: 6, FRI: 8, SAT: 9, SUN: 5})
    busy = sreq.priority_busy_shifts(WEEK, req)
    # 14 shifts: the top 4 by people asked for.
    assert busy == {(THU, "night"), (FRI, "night"), (SAT, "night"), (SUN, "night")}


def test_friday_and_saturday_night_are_busy_unless_below_the_weeks_middle():
    # A flat week has no top quartile; the owner's Friday and Saturday stand.
    flat = {(d, p): {"Host": 2} for d in WEEK for p in ("morning", "night")}
    assert sreq.priority_busy_shifts(WEEK, flat) == {(FRI, "night"), (SAT, "night")}
    # Nothing measured at all: Friday and Saturday night.
    assert sreq.priority_busy_shifts(WEEK) == {(FRI, "night"), (SAT, "night")}
    # A lunch place whose Friday and Saturday nights are its quietest shifts.
    quiet = _req(night={FRI: 1, SAT: 1}, morning={d: 6 for d in WEEK})
    got = sreq.priority_busy_shifts(WEEK, quiet)
    assert (FRI, "night") not in got and (SAT, "night") not in got


def test_a_night_with_a_demand_signal_is_busy_a_catalog_game_alone_is_not():
    req = _req(night={FRI: 8, SAT: 9})
    signals = {TUE: {"lift_pct": 25, "labels": ["Bears game (measured 6 times here)"]},
               WED: {"lift_pct": None, "labels": ["Trivia night"], "assumed": True},
               THU: {"lift_pct": None, "labels": [], "context": ["Bulls game (no measured effect here yet)"]},
               MON: {"lift_pct": -20, "labels": ["Reservations (12 covers)"]}}
    busy = sreq.priority_busy_shifts(WEEK, req, demand_by_date=signals)
    assert {(TUE, "night"), (WED, "night")} <= busy
    assert (THU, "night") not in busy and (MON, "night") not in busy


def test_a_closed_date_is_never_busy_and_late_counts_with_the_night():
    req = {k: v for k, v in _req(night={FRI: 8, SAT: 9}).items() if k[0] != SAT}
    req[(THU, "late")] = {"Bartender": 5}
    busy = sreq.priority_busy_shifts(WEEK, req)
    assert not any(d == SAT for d, _p in busy)
    assert (THU, "night") in busy
    # "date|daypart" keys (a JSON round trip) read the same.
    assert sreq.priority_busy_shifts(WEEK, {f"{d}|{p}": v for (d, p), v in req.items()}) == busy


def test_the_sales_level_breaks_a_tie_and_ranks_alone_without_requirements():
    levels = {(d, p): 1.0 for d in WEEK for p in ("morning", "night")}
    levels[(SUN, "morning")] = 2.0
    got = sreq.priority_busy_shifts(WEEK, levels=levels)
    assert (SUN, "morning") in got and {(FRI, "night"), (SAT, "night")} <= got


# ── the solvers ──────────────────────────────────────────────────────────

NAMES = ["Ann", "Bea"]


def _cons(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=list(NAMES), active={n.lower() for n in NAMES})
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {n.lower(): {"host"} for n in NAMES}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _slot(d, start, end, who=""):
    r = {"date": d, "day": sq._day_name(d), "employee": who, "role": "Host", "shift_start": start,
         "shift_end": end, "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    return r


# Ann is dearer than Bea: without Priority the cheaper Bea takes both.
SIG = {"roster": list(NAMES), "roster_roles": {n: "Host" for n in NAMES},
       "requirements_by_date": {(d, p): {"Host": 1} for d in WEEK for p in ("morning", "night")},
       "overtime": {"person_rates": {"Ann": 16.0, "Bea": 10.0}, "default_rate": 12.0, "line": 40.0}}
SLOTS = [_slot(FRI, "5:00pm", "10:00pm"), _slot(MON, "11:00am", "3:00pm")]


def _who(rows):
    return {r["date"]: r["employee"] for r in rows}


needs_ortools = pytest.mark.skipif(not cs.available(), reason="OR-Tools not installed")


@needs_ortools
def test_cpsat_fills_a_busy_shift_from_priority_people_and_not_a_quiet_one():
    plain = cs.assign(SLOTS, _cons(), signals=SIG, max_seconds=3)
    assert _who(plain["rows"]) == {FRI: "Bea", MON: "Bea"}
    got = cs.assign(SLOTS, _cons(priority_roles={"ann": {"host"}}), signals=SIG, max_seconds=3)
    assert got["status"] in ("optimal", "time_limit")
    # Friday night is busy: Ann, though she costs more. Monday lunch is not:
    # the cheaper Bea, Priority or not.
    assert _who(got["rows"]) == {FRI: "Ann", MON: "Bea"}
    assert got["problem"].busy_parts >= {(FRI, "night")} and (MON, "morning") not in got["problem"].busy_parts


def test_the_backtracking_solver_does_the_same():
    rows = [dict(r, employee="Bea") for r in SLOTS]
    plain = ss.solve(rows, _cons(), signals=SIG)
    assert _who(plain["rows"]) == {FRI: "Bea", MON: "Bea"}
    got = ss.solve(rows, _cons(priority_roles={"ann": {"host"}}), signals=SIG)
    assert _who(got["rows"]) == {FRI: "Ann", MON: "Bea"}


@needs_ortools
@pytest.mark.parametrize("hold", [{"unavailable_days": {"ann": {"Friday"}}},
                                  {"blocked_dates": {"ann": {FRI: "on approved time off"}}},
                                  {"hours_limits": {"ann": (None, 3.0)}}])
def test_priority_never_overrides_availability_time_off_or_the_hours_maximum(hold):
    c = _cons(priority_roles={"ann": {"host"}}, **hold)
    got = cs.assign(SLOTS, c, signals=SIG, max_seconds=3)
    assert _who(got["rows"])[FRI] == "Bea"
    assert not [v for v in sr.violations(got["rows"], c) if v.get("hard")]
    bt = ss.solve([dict(r, employee="Bea") for r in SLOTS], c, signals=SIG)
    assert _who(bt["rows"])[FRI] == "Bea"


@needs_ortools
def test_priority_never_buys_overtime():
    # Ann sits at 38h on the payroll week already: Friday's 5h would put her
    # 3h into overtime, which outranks Priority — Bea takes it.
    c = _cons(priority_roles={"ann": {"host"}})
    c.base_rows = {"ann": [_slot(TUE, "9:00am", "5:00pm", "Ann"), _slot(WED, "9:00am", "5:00pm", "Ann"),
                           _slot(THU, "9:00am", "5:00pm", "Ann"), _slot(SAT, "9:00am", "5:00pm", "Ann"),
                           _slot(SUN, "9:00am", "3:00pm", "Ann")]}
    fixed = [dict(r, _pinned=True) for r in c.base_rows["ann"]]
    got = cs.assign(fixed + [SLOTS[0]], c, signals=SIG, max_seconds=3, only_dates=[FRI])
    assert got["status"] in ("optimal", "time_limit")
    assert _who([r for r in got["rows"] if r["date"] == FRI])[FRI] == "Bea"


@needs_ortools
def test_priority_in_another_role_does_not_count():
    c = _cons(priority_roles={"ann": {"server"}})
    got = cs.assign(SLOTS, c, signals=SIG, max_seconds=3)
    assert _who(got["rows"]) == {FRI: "Bea", MON: "Bea"}


# ── stored and saved through the route, web and phone ────────────────────

@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    import client_api
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    return db_path


@pytest.fixture
def client(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid):
    user = {"id": 5, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "owner",
            "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda token: user)
    return user


def test_priority_roles_round_trip_through_the_web_and_phone_route(client, db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Priority Grill", owner_email="p@x.test", module_labor=1),
                            db_path=db)
    _as(monkeypatch, rid)
    r = client.post("/api/labor/staff-settings", json={"employee_name": "Slaone Arado",
                                                         "priority_roles": ["Host AM", "host"]})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["settings"]["priority_roles"] == ["host"]
    r = client.post("/mobile/api/labor/staff-settings", json={"employee_name": "Emily Meredith",
                                                                "priority_roles": ["Host"]},
                    headers={"Authorization": "Bearer t"})
    assert r.status_code == 200, r.get_json()
    got = staff_settings.get_all(rid, db_path=db)
    assert got["Slaone Arado"]["priority_roles"] == ["host"]
    assert got["Emily Meredith"]["priority_roles"] == ["host"]
    # Another save leaves it as it was; [] clears it; a non-list is refused.
    assert client.post("/api/labor/staff-settings", json={"employee_name": "Slaone Arado",
                                                           "max_hours": 30}).status_code == 200
    assert staff_settings.get_all(rid, db_path=db)["Slaone Arado"]["priority_roles"] == ["host"]
    assert client.post("/api/labor/staff-settings", json={"employee_name": "Emily Meredith",
                                                           "priority_roles": []}).status_code == 200
    assert staff_settings.get_all(rid, db_path=db)["Emily Meredith"]["priority_roles"] == []
    bad = client.post("/api/labor/staff-settings", json={"employee_name": "Mia Martin", "priority_roles": "host"})
    assert bad.status_code == 400


def test_the_roster_read_and_the_constraints_carry_priority_roles(client, db, monkeypatch):
    rid = create_restaurant(Restaurant(name="Priority Grill", owner_email="p@x.test", module_labor=1),
                            db_path=db)
    _as(monkeypatch, rid)
    models.add_manual_team_member(rid, "Mia Martin", "Host", db_path=db)
    staff_settings.upsert(rid, "Mia Martin", priority_roles=["Host PM"], db_path=db)
    # The roster read (web Team screen; the field iOS reads later).
    ro = client.get("/api/labor/roster").get_json()
    mia = [e for e in ro["roster"] if e["name"] == "Mia Martin"][0]
    assert mia["settings"]["priority_roles"] == ["host"]
    # What every solver reads.
    c = sr.build_constraints(rid, list(WEEK), [sq._day_name(d) for d in WEEK], db_path=db)
    assert c.priority_roles.get(c.key("Mia Martin")) == {"host"}
