"""Schedule re-audit 10/4/26, lens RULES (+ SQ-1, SQ-8, PROMPT-6). Each test is
one finding's reproduction, failing before its fix:

  RULES-1   open hours with nobody on are judged by the manager rule
  RULES-2   a "Manager in Training" / trainee is never the floor manager
  RULES-3   SQ-1  one reader of a job code's half of the day, every spelling
  RULES-4   the repair passes hold the owner's soft rules as the engine does
  RULES-5   an unset salaried cap is no owner cap
  RULES-6   each role's closer is held to its own close
  RULES-7   the closer breach's severity is about the close only
  RULES-8   the owner's staffing rules: ranges, exceptions, or "couldn't read"
  RULES-9   an owner maximum above 40h is a ceiling, not the overtime line
  RULES-10  manager coverage on one timeline across midnight
  RULES-11  any minute in a blocked half of the day is in it
  RULES-12  SQ-8  one floor test, by family, for the sweep and the score
  RULES-13  owner-operator titles; no manager on the roster holds the publish
  RULES-14  a certificate expiring mid-week counts until it expires
  RULES-15  approving the only manager's drop says the floor goes unmanaged
  RULES-16  dormant people stay on the roster the rows are checked against
  PROMPT-6  a closing leader rule is printed only on the closing daypart
"""
import random
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import models
import schedule_engine as se
import schedule_requirements as req
import schedule_rules as sr
import shift_quality as sq

WEEK = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]   # a Monday
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def C(managers=None, roster=None, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.roster_names = list(roster or ["Ann", "Ben", "Andrew", "Erik"])
    c.active = {n.lower() for n in c.roster_names}
    c.managers = {"andrew": "Manager", "erik": "Owner"} if managers is None else managers
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def R(i, emp, start, end, role="Server", hours=None):
    r = {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role,
         "shift_start": start, "shift_end": end, "notes": ""}
    r["scheduled_hours"] = str(hours if hours is not None else sr.span_hours(r))
    return r


def kinds(rows, c):
    return [v["kind"] for v in sr.violations(rows, c)]


def same_as_sweep(rows, c):
    """IncrementalSweep must give violations()' own answer."""
    assert sr.IncrementalSweep(c).violations(rows) == sr.violations(rows, c)


# ── RULES-1 ───────────────────────────────────────────────────────────────

def test_open_hours_with_nobody_on_are_unmanaged_minutes():
    c = C(open_times={"Monday": "11:00am"}, close_times={"Monday": "10:00pm"})
    rows = [R(0, "Ann", "12:30pm", "10:00pm"), R(0, "Andrew", "12:30pm", "10:00pm", role="Manager")]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "no_manager"]
    assert [(x["gap_start"], x["gap_end"]) for x in v] == [(11 * 60, 12 * 60 + 30)]
    assert v[0]["hard"] and sr.BREACH_TIER["no_manager"] == sr.TIER_MANAGER and "nobody is on" in v[0]["detail"]
    same_as_sweep(rows, c)
    # Everyone gone at 8:30pm with a 10pm close: the manager tier, not only
    # the coverage tier's nobody_at_close.
    rows = [R(0, "Ann", "11:00am", "8:30pm"), R(0, "Andrew", "11:00am", "8:30pm", role="Manager")]
    gaps = sr.manager_gaps(rows, c)[WEEK[0]]
    assert [(s, e) for s, e, _i in gaps] == [(20 * 60 + 30, 22 * 60)]
    assert {"nobody_at_close", "no_manager"} <= set(kinds(rows, c))
    # A manager on from open to close: clean.
    rows = [R(0, "Ann", "12:30pm", "10:00pm"), R(0, "Andrew", "11:00am", "10:00pm", role="Manager")]
    assert "no_manager" not in kinds(rows, c)


def test_the_backstop_fills_an_unmanaged_opening():
    c = C(open_times={"Monday": "11:00am"}, close_times={"Monday": "10:00pm"}, salaried={"erik"})
    rows = [R(0, "Ann", "12:30pm", "10:00pm"), R(0, "Andrew", "12:30pm", "10:00pm", role="Manager")]
    out = sr.cover_manager_gaps(rows, c)
    assert not sr.manager_gaps(out["rows"], c), out


# ── RULES-2 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", ["Manager in Training", "Manager Trainee", "MIT", "Assistant Manager Trainee"])
def test_a_manager_in_training_is_never_counted_as_the_floor_manager(title):
    assert sr.manager_role_kind(title) == "trainee"
    assert sr.is_training_role(title)
    assert not sr.manager_basis("Tess", title)["counts"]
    # Not even on the owner's yes: they run the floor only beside a manager.
    b = sr.manager_basis("Tess", title, settings={"floor_manager": True})
    assert not b["counts"] and b["basis"] == "trainee" and "training" in b["why"]


def test_a_trainee_alone_with_a_server_leaves_the_floor_unmanaged():
    c = C(managers={"andrew": "Manager", "tess": "Manager in Training"}, roster=["Ann", "Andrew", "Tess"])
    rows = [R(0, "Ann", "11:00am", "4:00pm"), R(0, "Tess", "11:00am", "4:00pm", role="Manager in Training")]
    assert "no_manager" in kinds(rows, c)
    # A trainee into a manager role, while training lasts, on a manager row.
    c = C(managers={"andrew": "Manager", "tess": "Manager"}, roster=["Ann", "Andrew", "Tess"],
          trainees={"tess": {"target_role": "Manager", "trainer": "Andrew", "from": None, "until": WEEK[3]}})
    rows = [R(1, "Ann", "11:00am", "4:00pm"), R(1, "Tess", "11:00am", "4:00pm", role="Manager")]
    assert not c.manages("Tess", WEEK[1]) and "no_manager" in kinds(rows, c)
    rows = [R(5, "Ann", "11:00am", "4:00pm"), R(5, "Tess", "11:00am", "4:00pm", role="Manager")]
    assert c.manages("Tess", WEEK[5]) and "no_manager" not in kinds(rows, c)     # training over


# ── RULES-3 / SQ-1 ────────────────────────────────────────────────────────

SPELLINGS = {
    "morning": ["Server AM", "Server-AM", "Server (AM)", "Server/AM", "Server_AM", "AM Server", "Lunch Server",
                "Server - AM", "Server (A.M.)", "Server [AM]"],
    "night": ["Server PM", "Server-PM", "Server (PM)", "Server/PM", "Server_PM", "PM Server", "Dinner Server",
              "Server Dinner", "Server – PM"],
}


def test_every_spelling_role_family_accepts_keeps_its_half_of_the_day():
    runs = {"server": {("Saturday", "morning"), ("Saturday", "night")}}
    for part, names in SPELLINGS.items():
        other = "night" if part == "morning" else "morning"
        for role in names:
            assert sq.role_family(role) == "server", role
            assert sq.role_daypart(role) == part, role
            assert sq.job_code_daypart(role) == part, role
            assert sr.role_daypart(role) == part, role
            assert models.leader_rule_daypart(role) == part, role
            rule = {"role": role, "min_score": 5, "count": 1}
            assert sq.leader_rule_applies(rule, "Saturday", part, runs=runs), role
            assert not sq.leader_rule_applies(rule, "Saturday", other, runs=runs), role
    assert sq.role_daypart("Lunch/Dinner Server") is None and sq.role_daypart("Server") is None


def test_a_pm_leader_rule_does_not_reach_the_lunch_table_or_cap_the_lunch():
    mon = WEEK[0]
    rule = {"role": "Server (PM)", "min_score": 5, "count": 1}
    table = req.shift_requirements([mon], typical_headcount={("Monday", "morning"): {"Server (AM)": 2},
                                                             ("Monday", "night"): {"Server (PM)": 1}},
                                   leader_rules=[rule], leadership_known=True)
    said = {r["daypart"]: " ".join(r["leader"]) for r in table}
    assert "Server (PM)" not in said.get("morning", "") and "Server (PM)" in said.get("night", "")
    # The scorer's leadership check on the lunch: the PM rule is not asked.
    lunch = SimpleNamespace(day="Monday", daypart="morning", is_closing=False, role_runs=None, role_families=None)
    assert not sq._rule_applies(rule, lunch)


# ── PROMPT-6 ──────────────────────────────────────────────────────────────

def test_a_closing_leader_rule_is_printed_on_the_closing_daypart_only():
    rule = {"role": "Bartender", "attribute": "can_close", "count": 1, "closing": True}
    dates = WEEK[2:5]
    typical = {(d, p): {"Bartender": 1} for d in DAYS for p in ("morning", "night")}
    table = req.shift_requirements(dates, typical_headcount=typical, leader_rules=[rule], leadership_known=True)
    said = {(r["date"], r["daypart"]): " ".join(r["leader"]) for r in table}
    for d in dates:
        assert "authorized to close" not in said.get((d, "morning"), ""), said
        assert "authorized to close" in said.get((d, "night"), ""), said
    # A place that closes at 2:30pm closes at lunch.
    table = req.shift_requirements([WEEK[2]], typical_headcount=typical, leader_rules=[rule], leadership_known=True,
                                   close_times={"Wednesday": "2:30pm"})
    said = {r["daypart"]: " ".join(r["leader"]) for r in table}
    assert "authorized to close" in said.get("morning", "") and "authorized to close" not in said.get("night", "")


# ── RULES-12 / SQ-8 ───────────────────────────────────────────────────────

def test_the_sweep_and_the_score_give_one_verdict_on_a_floor():
    tue = WEEK[1]
    rows = [{"date": tue, "employee": "Ann", "role": "Server", "shift_start": "5:00pm", "shift_end": "8:00pm",
             "scheduled_hours": "3"},
            {"date": tue, "employee": "Bo", "role": "Server", "shift_start": "7:00pm", "shift_end": "11:00pm",
             "scheduled_hours": "4"},
            {"date": tue, "employee": "Mo", "role": "Manager", "shift_start": "4:00pm", "shift_end": "11:00pm",
             "scheduled_hours": "7"}]
    c = sr.Constraints(restaurant_id=1, week_dates=[tue], week_days=["Tuesday"], roster_names=["Ann", "Bo", "Mo"],
                       active={"ann", "bo", "mo"}, managers={"mo": "Manager"}, role_floors={"Server": {"night": 2}},
                       open_times={"Tuesday": "4:00pm"}, close_times={"Tuesday": "11:00pm"})
    v = [x for x in sr.violations(rows, c) if x["kind"] == "coverage_floor"]
    held = sq.floor_shortfall(rows, "Server", 2, "night", sr.parse_minutes("4:00pm"), sr.parse_minutes("11:00pm"))
    assert not held["held"] and v and v[0]["hard"]
    assert "your floor is 2" in v[0]["detail"] and v[0]["worst_at"] == held["worst_at"]
    assert v[0]["minutes"] == held["short_minutes"] == 120


def test_a_floor_on_a_job_code_counts_the_roles_am_and_pm_codes():
    c = C(managers={"erik": "Owner"}, roster=["Bo", "Cy", "Erik"],
          role_names={"bartender": "Bartender", "bartender pm": "Bartender PM", "owner": "Owner"},
          known_roles={"bo": {"bartender", "bartender pm"}, "cy": {"bartender pm"}, "erik": {"owner"}},
          role_floors={"Bartender": {"morning": 0, "night": 1, "days": {}}})
    sr._floors_on_job_codes(c)
    rows = [R(4, "Bo", "5:00pm", "1:00am", role="Bartender PM"), R(4, "Cy", "6:00pm", "1:00am", role="Bartender PM"),
            R(4, "Erik", "4:00pm", "1:00am", role="Owner")]
    assert "coverage_floor" not in kinds(rows, c)


def test_randomised_days_the_sweep_floor_and_floor_shortfall_agree():
    rng = random.Random(1004)
    starts = ["10:00am", "11:00am", "12:00pm", "3:00pm", "4:00pm", "5:00pm", "6:00pm", "7:00pm"]
    ends = ["2:00pm", "3:00pm", "6:00pm", "8:00pm", "9:00pm", "10:00pm", "11:00pm"]
    for _ in range(200):
        need = {"Server": {"morning": rng.choice([0, 1, 2]), "night": rng.choice([1, 2, 3])}}
        rows = [R(1, "Mo", "9:00am", "11:30pm", role="Manager")]
        for n in range(rng.randint(0, 5)):
            s, e = rng.choice(starts), rng.choice(ends)
            if sr.parse_minutes(e) <= sr.parse_minutes(s):
                continue
            rows.append(R(1, f"P{n}", s, e, role=rng.choice(["Server", "Server AM", "Server PM"])))
        c = C(managers={"mo": "Manager"}, roster=["Mo"] + [f"P{n}" for n in range(6)], role_floors=need,
              open_times={"Tuesday": "10:00am"}, close_times={"Tuesday": "11:30pm"})
        sweep = {x["daypart"] for x in sr.violations(rows, c) if x["kind"] == "coverage_floor"}
        for part in ("morning", "night"):
            held = sq.floor_shortfall([r for r in rows if r["role"] != "Manager"], "Server", need["Server"][part], part,
                                      sr.parse_minutes("10:00am"), sr.parse_minutes("11:30pm"))
            assert (part in sweep) == (not held["held"]), (rows, part, held)
        same_as_sweep(rows, c)


def _scorer_floor_short(rows, floors, c, day):
    """Whether the scorer's coverage by the hour finds the dinner floor short."""
    flagged = {((v.get("employee") or "").strip().lower(), v.get("date") or "", v.get("shift_start") or "")
               for v in sr.violations(rows, c) if v.get("no_show")}
    q = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="std", source="restaurant")], role_floors=floors,
                      open_times={day: "4:00pm"}, close_times={day: "11:00pm"}, flagged=flagged,
                      trainees=dict(c.trainees))
    night = [x for x in q["shifts"] if x.get("daypart") == "night"][0]
    dim = next(d for d in night["dimensions"] if d["key"] == "coverage_curve")
    return bool((dim.get("facts") or {}).get("gaps"))


def test_a_training_shift_and_a_shift_on_time_off_count_toward_neither_floor():
    tue = WEEK[1]
    floors = {"Server": {"night": 2}}
    base = [R(1, "Mo", "4:00pm", "11:00pm", role="Manager"), R(1, "Ann", "4:00pm", "11:00pm")]
    for extra, kw in ((R(1, "Tia", "4:00pm", "11:00pm", role="Server Trainee"), {}),
                      (R(1, "Tia", "4:00pm", "11:00pm"),
                       {"trainees": {"tia": {"target_role": "Server", "trainer": "Ann", "from": None, "until": None}}}),
                      (R(1, "Tia", "4:00pm", "11:00pm"), {"blocked_dates": {"tia": {tue: "on approved time off"}}})):
        c = C(managers={"mo": "Manager"}, roster=["Mo", "Ann", "Tia"], role_floors=floors,
              open_times={"Tuesday": "4:00pm"}, close_times={"Tuesday": "11:00pm"}, **kw)
        rows = base + [extra]
        sweep = any(v["kind"] == "coverage_floor" for v in sr.violations(rows, c))
        assert sweep and _scorer_floor_short(rows, floors, c, "Tuesday"), kw
    c = C(managers={"mo": "Manager"}, roster=["Mo", "Ann", "Tia"], role_floors=floors,
          open_times={"Tuesday": "4:00pm"}, close_times={"Tuesday": "11:00pm"})
    rows = base + [R(1, "Tia", "4:00pm", "11:00pm")]
    assert not any(v["kind"] == "coverage_floor" for v in sr.violations(rows, c))
    assert not _scorer_floor_short(rows, floors, c, "Tuesday")


# ── RULES-4 ───────────────────────────────────────────────────────────────

def test_the_rebalance_keeps_the_owners_rule_and_the_engine_keeps_the_stage():
    names = ["Ann", "Dan", "Ben", "Cara", "Erik"]
    c = C(managers={"erik": "Owner"}, roster=names)
    c.compliance.update(max_consecutive_days=7, weekly_hours_ceiling=50, max_shift_hours=14)
    c.known_roles = {"ann": {"server"}, "dan": {"server"}, "ben": {"server"}, "cara": {"server"}, "erik": {"owner"}}
    c.salaried = {"erik"}
    c.owner_rules = [dict(sr.parse_owner_rule("Always 2 servers on Saturday", ["Server"]), private=False)]
    c.unavailable_days = {"cara": {"Saturday"}}
    rows = [R(i, "Ann", "10:00am", "6:00pm") for i in range(6)]
    rows += [R(i, "Dan", "1:00pm", "10:00pm") for i in range(5)]
    rows.append(R(5, "Ben", "6:00pm", "11:00pm"))
    rows += [R(i, "Erik", "9:00am", "11:00pm", role="Owner") for i in range(7)]
    rr = {"Ann": "Server", "Dan": "Server", "Ben": "Server", "Cara": "Server", "Erik": "Owner"}
    out = sr.rebalance_overtime(rows, c, roster_roles=rr)
    moved = {(m["from"], out["rows"][m["index"]]["day"]) for m in out["moves"]}
    assert ("Ann", "Saturday") not in moved and {"Ann", "Dan"} <= {f for f, _d in moved}
    before = sr.breach_profile(rows, c)
    worse, _after = se._judge(sr.TIER_OVERTIME, rows, out["rows"], before, SimpleNamespace(editable=None, c=c))
    assert worse == []
    sat = [r for r in out["rows"] if r["day"] == "Saturday" and r["role"] == "Server"]
    assert len({r["employee"] for r in sat}) >= 2


# ── RULES-9 ───────────────────────────────────────────────────────────────

def test_an_owner_maximum_above_forty_is_a_ceiling_not_the_overtime_line():
    c = C(managers={"erik": "Owner"}, roster=["Kim", "Lou", "Erik"], hours_limits={"kim": (None, 45)},
          known_roles={"kim": {"line cook"}, "lou": {"line cook"}, "erik": {"owner"}}, salaried={"erik"})
    c.compliance["max_consecutive_days"] = 7
    assert c.max_hours("Kim") == 45 and sr.overtime_line(c, "Kim") == 40
    rows = [R(i, "Kim", "2:00pm", "11:00pm", role="Line Cook") for i in range(5)]
    rows += [R(i, "Lou", "11:00am", "4:00pm", role="Line Cook") for i in range(4)]
    out = sr.rebalance_overtime(rows, c, roster_roles={"Kim": "Line Cook", "Lou": "Line Cook", "Erik": "Owner"})
    assert [(m["from"], m["to"]) for m in out["moves"]] == [("Kim", "Lou")]
    kim = sum(sr.row_hours(r) for r in out["rows"] if r["employee"] == "Kim")
    assert kim <= 40 and "over_max_hours" not in kinds(out["rows"], c)
    # Nobody with room: the overtime stays, up to the owner's maximum, and is said.
    out = sr.rebalance_overtime(rows[:5], c, roster_roles={"Kim": "Line Cook", "Erik": "Owner"})
    assert not out["moves"] and any(x["employee"] == "Kim" for x in out["left"])


# ── RULES-5 ───────────────────────────────────────────────────────────────

@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _restaurant(**cols):
    rid = models.create_restaurant(models.Restaurant(name="Re-audit Grill", owner_email="ra@x.test",
                                                     timezone="America/Chicago"))
    if cols:
        models.update_restaurant(rid, cols)
    return rid


def test_an_unset_salaried_cap_is_no_owner_cap(db):
    rid = _restaurant()
    for n, role in (("Erik", "Owner"), ("Ann", "Manager"), ("Ben", "Manager")):
        models.add_manual_team_member(rid, n, role=role)
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert c.is_salaried("Erik") and c.salaried_cap is None
    assert c.max_hours("Erik") == sr.SALARIED_HOURS_MAX and c.salaried_limit("Erik") == sr.SALARIED_CAP_DEFAULT
    rows = [R(i, "Erik", "10:00am", "10:00pm", role="Owner") for i in range(5)]          # 60h of his own
    assert "over_max_hours" not in kinds(rows, c)
    out = sr.rebalance_overtime(rows, c, roster_roles={"Erik": "Owner", "Ann": "Manager", "Ben": "Manager"})
    assert not [m for m in out["moves"] if m["from"] == "Erik"]
    # A cap the owner set is theirs, and said so.
    models.update_restaurant(rid, {"salaried_cap": 50})
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert c.salaried_cap == 50 and c.max_hours("Erik") == 50 and c.salaried_cap_set("Erik")
    assert "over_max_hours" in kinds(rows, c)


# ── RULES-6 / RULES-7 ─────────────────────────────────────────────────────

def test_with_no_close_on_file_a_closer_is_the_last_of_their_role_out():
    c = C(managers={"erik": "Owner"}, roster=["Ann", "Sam", "Dee", "Erik"], closers_by_role={"server": {"ann"}},
          keyholders={"ann"}, close_times={})
    c.compliance["max_shift_hours"] = 14
    rows = [R(0, "Ann", "4:00pm", "10:00pm"), R(0, "Sam", "5:00pm", "10:00pm"),
            R(0, "Dee", "6:00pm", "1:00am", role="Dishwasher"), R(0, "Erik", "3:00pm", "1:00am", role="Owner")]
    assert "keyholder_until_close" not in kinds(rows, c)
    assert sr.close_out_gaps(rows, c)["extended"] == []


def test_a_kitchen_that_closes_before_the_bar_is_held_to_its_own_close():
    c = C(managers={"erik": "Owner"}, roster=["Kim", "Lou", "Bo", "Erik"],
          closers_by_role={"line cook": {"kim"}, "bartender": {"bo"}}, keyholders={"kim", "bo"},
          close_times={"Friday": "2:00am"})
    c.compliance["max_shift_hours"] = 14
    rows = [R(4, "Kim", "3:00pm", "11:00pm", role="Line Cook"), R(4, "Lou", "4:00pm", "10:30pm", role="Line Cook"),
            R(4, "Bo", "6:00pm", "2:00am", role="Bartender"), R(4, "Erik", "2:00pm", "2:00am", role="Owner")]
    # Its own punches show the kitchen ending before the close ...
    c.early_close_roles = {"line cook"}
    assert "keyholder_until_close" not in kinds(rows, c)
    assert sr.close_out_gaps(rows, c)["extended"] == []
    # ... or the owner's own end-time rule for the role's night says so.
    c.early_close_roles = set()
    c.role_times = {("line cook", "Friday", "night"): {"end": sr.parse_minutes("11:00pm"), "source": {}}}
    assert "keyholder_until_close" not in kinds(rows, c)
    # The bar, which does stay to the close, is still held to it.
    rows[2] = R(4, "Bo", "6:00pm", "12:30am", role="Bartender")
    v = [x for x in sr.violations(rows, c) if x["kind"] == "keyholder_until_close"]
    assert [x["close_role"] for x in v] == ["bartender"] and "leaves before close" in v[0]["detail"]


def test_closing_split_reads_which_roles_close_from_the_punches():
    c = C(close_times={"Friday": "2:00am", "Saturday": "2:00am"})
    hist = []
    for wk in range(3):
        for i in (4, 5):
            d = (date.fromisoformat(WEEK[i]) - timedelta(weeks=wk + 1)).isoformat()
            hist += [{"date": d, "employee": "Kim", "role": "Line Cook", "shift_start": "3:00pm", "shift_end": "11:00pm"},
                     {"date": d, "employee": "Bo", "role": "Bartender", "shift_start": "6:00pm", "shift_end": "2:00am"}]
    closing, early = sr.closing_split(c, hist)
    assert closing == {"bartender"} and early == {"line cook"}


def test_the_closer_breach_does_not_get_worse_when_a_lunch_shift_is_added():
    c = C(managers={"erik": "Owner"}, roster=["Ann", "Sam", "Tia", "Erik"], closers_by_role={"server": {"ann"}},
          keyholders={"ann"}, close_times={"Monday": "10:00pm"},
          role_floors={"Server": {"morning": 1, "night": 0, "days": {}}})
    base = [R(0, "Sam", "4:00pm", "10:00pm"), R(0, "Erik", "10:00am", "10:00pm", role="Owner")]
    trial = base + [R(0, "Tia", "11:00am", "3:00pm")]
    b, a = sr.breach_profile(base, c), sr.breach_profile(trial, c)
    bid = ("keyholder_until_close", WEEK[0], "server")
    assert b["by_id"][bid] == a["by_id"][bid]
    assert sr.regressions(b, a) == []
    # Any closer on is better than none, even one leaving early.
    early = base + [R(0, "Ann", "4:00pm", "8:00pm")]
    assert sr.breach_profile(early, c)["by_id"][bid] < b["by_id"][bid]


# ── RULES-8 ───────────────────────────────────────────────────────────────

ROLES = ["Server", "Bartender", "Host", "Line Cook"]


@pytest.mark.parametrize("text,days,part", [
    ("Always 2 servers except Mondays", tuple(DAYS[1:]), None),
    ("At least 2 servers Mon-Fri", tuple(DAYS[:5]), None),
    ("At least 2 servers Monday through Friday", tuple(DAYS[:5]), None),
    ("Always 2 servers, no exceptions, every day but Sunday", tuple(DAYS[:6]), None),
    ("At least 2 servers weekdays except Friday", tuple(DAYS[:4]), None),
    ("At least 2 servers Fri-Sun", ("Friday", "Saturday", "Sunday"), None),
    ("At least 3 servers on Friday and Saturday nights", ("Friday", "Saturday"), "night"),
])
def test_ranges_and_exceptions_are_read(text, days, part):
    r = sr.parse_owner_rule(text, ROLES)
    assert not r.get("unclear") and r["days"] == days and r["daypart"] == part


@pytest.mark.parametrize("text", ["Never below 2 servers at dinner, 1 at lunch", "Always 1 bartender, 2 on weekends",
                                  "Always 2 servers except holidays", "At least 2 servers at lunch and dinner",
                                  "Always 2 servers on Saturday but never on a holiday"])
def test_a_rule_that_cant_be_read_confidently_goes_to_the_owner(text):
    r = sr.parse_owner_rule(text, ROLES)
    assert r and r.get("unclear") and "min" not in r


def test_an_unclear_rule_is_never_a_floor_and_is_said_back(db, monkeypatch):
    import owner_memory
    import schedule_setup
    rid = _restaurant()
    models.add_manual_team_member(rid, "Ann", role="Server")
    fact = {"id": 1, "fact": "Never below 2 servers at dinner, 1 at lunch", "modules": "", "kind": "constraint"}
    monkeypatch.setattr(owner_memory, "facts_for", lambda *a, **k: [fact])
    monkeypatch.setattr(owner_memory, "is_owner_rule", lambda f: True)
    c = sr.build_constraints(rid, WEEK, DAYS)
    sr.apply_owner_rules(c, rid)
    assert not c.owner_rules and not c.role_floors
    assert fact["fact"] in (c.owner_rules_unchecked + c.owner_rules_unchecked_private)
    said = schedule_setup.owner_rule_preview(rid, fact["fact"], c=c)
    assert not said["checked"] and said["unclear"] and "couldn't read this rule" in said["text"]


# ── RULES-10 ──────────────────────────────────────────────────────────────

def test_an_overnight_manager_covers_across_midnight():
    c = C()
    rows = [R(5, "Andrew", "10:00pm", "6:00am", role="Manager"), R(5, "Ann", "11:00pm", "7:30am"),
            R(6, "Erik", "6:00am", "2:00pm", role="Owner"), R(6, "Ben", "6:00am", "2:00pm", role="Line Cook")]
    assert sr.manager_gaps(rows, c) == {} and "no_manager" not in kinds(rows, c)
    same_as_sweep(rows, c)
    rows2 = [R(5, "Ann", "5:00pm", "1:00am"), R(5, "Andrew", "4:00pm", "12:00am", role="Manager"),
             R(6, "Erik", "12:00am", "8:00am", role="Owner")]
    assert sr.manager_gaps(rows2, c) == {}
    same_as_sweep(rows2, c)
    # Without the Sunday owner the 90 minutes are a real gap (past what a
    # closing manager covers by staying).
    assert sr.manager_gaps(rows[:2], c)[WEEK[5]][0][:2] == (30 * 60, 31 * 60 + 30)


# ── RULES-11 ──────────────────────────────────────────────────────────────

def test_any_minute_in_a_blocked_half_of_the_day_is_in_it():
    c = C(managers={"erik": "Owner"}, roster=["Ann", "Erik"],
          blocked_parts={"ann": {WEEK[4]: [{"from": None, "until": None, "daypart": "night",
                                            "reason": "on approved time off (dinner)"}]}})
    rows = [R(4, "Ann", "10:30am", "6:25pm"), R(4, "Erik", "10:00am", "7:00pm", role="Owner")]
    assert "approved_time_off" in kinds(rows, c)
    assert not c.can_add(R(4, "Ann", "10:30am", "6:25pm"), [rows[1]])[0]
    c = C(managers={"erik": "Owner"}, roster=["Ann", "Erik"], daypart_avail={"ann": {"Friday": "night"}})
    rows = [R(4, "Ann", "1:35pm", "10:00pm"), R(4, "Erik", "9:00am", "10:00pm", role="Owner")]
    assert "unavailable_daypart" in kinds(rows, c)
    assert not sq.works_daypart_ok(rows[0], "night")
    import time_off
    assert time_off.shift_hits_part(R(4, "Ann", "10:30am", "6:25pm"), {"daypart": "night"}) or \
        time_off.part_minutes({"daypart": "night"})[2] is None


# ── RULES-13 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,kind", [("Managing Partner", "owner"), ("Operating Partner", "owner"),
                                        ("Franchisee", "owner"), ("Operator", "owner"),
                                        ("Director of Operations", "manager"), ("Partner", None),
                                        ("Dish Machine Operator", None)])
def test_owner_operator_titles_are_recognised(title, kind):
    assert sr.manager_role_kind(title) == kind


def test_a_roster_with_nobody_managing_holds_the_publish(db):
    import client_api
    rid = _restaurant()
    for n in ("Ana", "Bob"):
        models.add_manual_team_member(rid, n, role="Server")
    conn = models.get_conn(db)
    models._ensure_history_columns(conn)
    csv_text = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6.0,\n")
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, "
                       "hours_budget, labor_target, schedule_csv, summary_json) VALUES (?,?,?,?,?,?,?,'[]')",
                       (rid, WEEK[0], WEEK[6], 6, 40, 30, csv_text)).lastrowid
    conn.commit()
    conn.close()
    assert any("floor manager" in b for b in client_api.publish_blockers(rid, hid))
    import staff_settings as ss
    ss.upsert(rid, "Ana", floor_manager=True)
    assert not any("floor manager" in b for b in client_api.publish_blockers(rid, hid))


# ── RULES-14 ──────────────────────────────────────────────────────────────

def test_a_certificate_that_expires_mid_week_counts_until_it_expires():
    c = C(managers={"erik": "Owner"}, roster=["Cook", "Erik"], certifications={"cook": {"food_handler"}},
          cert_expiry={"cook": {"food_handler": WEEK[2]}}, role_requirements={"line cook": {"food_handler"}})
    assert c.cert_ok("Cook", "Line Cook", WEEK[2])[0]
    assert not c.cert_ok("Cook", "Line Cook", WEEK[3])[0]
    rows = [R(i, "Cook", "4:00pm", "10:00pm", role="Line Cook") for i in (1, 3)]
    v = [x["date"] for x in sr.violations(rows, c) if x["kind"] == "missing_cert"]
    assert v == [WEEK[3]]
    assert not c.can_add(R(4, "Cook", "4:00pm", "10:00pm", role="Line Cook"), rows)[0]


def test_build_constraints_reads_a_mid_week_expiry(db):
    import staff_knowledge
    import staff_settings as ss
    rid = _restaurant()
    models.add_manual_team_member(rid, "Cook", role="Line Cook")
    ss.upsert(rid, "Cook", certifications=["food_handler"])
    # A week still ahead: once a week has begun, a card is judged as of today
    # (_expired_certs), so a fixed past week made this test expire with it.
    start = date.today() + timedelta(days=7 - date.today().weekday() + 7)
    week = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    staff_knowledge.save_cert(rid, None, "Cook", "Food handler", expires_on=week[2])
    c = sr.build_constraints(rid, week, DAYS)
    assert c.cert_expiry.get("cook", {}).get("food_handler") == week[2]
    assert "food_handler" in c.certifications.get("cook", set())


# ── RULES-15 ──────────────────────────────────────────────────────────────

def test_approving_the_only_managers_drop_says_the_floor_goes_unmanaged(db, monkeypatch):
    import schedule_versions as sv
    import shift_requests as srq
    import staff_settings as ss
    monkeypatch.setattr(srq, "_tell_managers", lambda *a, **k: told.append(a))
    monkeypatch.setattr(srq, "_email_staff", lambda *a, **k: None)
    monkeypatch.setattr(srq, "_broadcast_open", lambda *a, **k: None)
    told = []
    rid = _restaurant()
    models.add_manual_team_member(rid, "Ana", role="Server")
    models.add_manual_team_member(rid, "Max", role="Manager")
    ss.upsert(rid, "Max", floor_manager=True)
    csv_text = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                f"{WEEK[5]},Saturday,Ana,Server,4:00pm,11:00pm,7.0,\n"
                f"{WEEK[5]},Saturday,Max,Manager,4:00pm,11:00pm,7.0,\n")
    conn = models.get_conn(db)
    models._ensure_history_columns(conn)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, "
                       "hours_budget, labor_target, schedule_csv, summary_json, published_at) "
                       "VALUES (?,?,?,?,?,?,?,'[]','2026-10-01 10:00:00')",
                       (rid, WEEK[0], WEEK[6], 14, 40, 30, csv_text)).lastrowid
    conn.commit()
    conn.close()
    sv.append(rid, hid, "published", csv_text, db_path=db)
    today = date(2026, 10, 1)
    ask = srq.request_drop(rid, "Max", WEEK[5], "4:00pm", reason="wedding", db_path=db, today=today)
    assert any("no manager on" in str(a[2]) for a in told), told
    out = srq.decide(rid, ask["id"], True, decided_by="will", db_path=db, today=today)
    assert out["status"] == "open" and "no manager on" in out["manager_gap"] and "10/10/26" in out["manager_gap"]
    # A server's drop leaves the manager on: nothing to say.
    ask = srq.request_drop(rid, "Ana", WEEK[5], "4:00pm", db_path=db, today=today)
    assert not srq.decide(rid, ask["id"], True, db_path=db, today=today).get("manager_gap")


# ── RULES-16 ──────────────────────────────────────────────────────────────

def test_dormant_people_stay_on_the_roster_the_rows_are_checked_against(db):
    rid = _restaurant()
    for n in ("Ana", "Dana"):
        models.add_manual_team_member(rid, n, role="Server")
    c = sr.build_constraints(rid, WEEK, DAYS)
    result = {"constraints": c, "roster": ["Ana"], "dormant": {"Dana": "2026-07-01"}, "week_dates": WEEK,
              "week_days": DAYS}
    c2, _new = se._rules_after_the_model(rid, result)
    assert "dana" in c2.active and "Dana" in c2.roster_names
    rows = [R(2, "Dana", "4:00pm", "10:00pm")]
    assert "off_roster" not in kinds(rows, c2)
    c2.dormant = {"dana": "2026-07-01"}
    assert not c2.fillable("Dana", WEEK[2])[0]


# ── docs ──────────────────────────────────────────────────────────────────

def test_prompt_closer_lines_say_each_roles_own_close():
    c = C(closers_by_role={"line cook": {"kim"}, "bartender": {"bo"}}, close_times={"Friday": "2:00am"},
          early_close_roles={"line cook"}, roster=["Kim", "Bo"])
    text = " ".join(sr._closer_prompt_lines(c))
    assert "Bartender: Bo (on until close)" in text and "Line cook: Kim (the last of the role to leave)" in text
