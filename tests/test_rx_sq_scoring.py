"""Schedule re-audit 10/4/26, lens SQ: the week score, the optimiser and the
money and hours figures behind the schedule (SQ-2..7, SQ-9, SQ-10, SQ-12).
Each test is the auditor's reproduction turned into an assertion."""
from datetime import date, timedelta

import schedule_optimizer as so
import schedule_rules as R
import shift_quality as sq

WEEK = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]
DAYS = [sq._day_name(d) for d in WEEK]


def _two_shift_week(gap_on=None):
    """14 shifts, a manager and two servers on each; `gap_on` dates have the
    dinner manager leave at 7pm, three hours before the 10pm close."""
    rows, names = [], set()
    for i, d in enumerate(WEEK):
        for s, e, tag in (("10:00am", "4:00pm", "a"), ("4:00pm", "10:00pm", "p")):
            m = f"Mgr{tag}{i}"
            rows.append({"employee": m, "role": "Manager", "date": d, "shift_start": s,
                         "shift_end": "7:00pm" if (tag == "p" and d in (gap_on or ())) else e})
            names.add(m)
            for k in range(2):
                n = f"Srv{tag}{i}{k}"
                rows.append({"employee": n, "role": "Server", "date": d, "shift_start": s, "shift_end": e})
                names.add(n)
    return rows, names


def _typical():
    return {(wd, p): {"Server": 2, "Manager": 1} for wd in DAYS for p in ("morning", "night")}


def _constraints(names):
    names = set(names) | {"Boss"}
    mgrs = {n.lower(): "Manager" for n in names if n.startswith("Mgr")}
    mgrs["boss"] = "Owner"
    return R.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS, roster_names=sorted(names),
                         active={n.lower() for n in names}, managers=mgrs)


# ── SQ-2: a broken hard rule holds the week, not only its shift ────────────

def test_sq2_manager_gap_holds_the_week_under_fair():
    rows, _n = _two_shift_week()
    clean = sq.score_rows(rows, typical_headcount=_typical())
    assert clean["score"] >= 90 and clean["held_by"] is None
    hb = [{"kind": "no_manager", "date": "2026-10-10", "hard": True,
           "label": "No manager on the floor 7:00 PM–10:00 PM", "gap_start": 19 * 60, "gap_end": 22 * 60}]
    q = sq.score_rows(rows, typical_headcount=_typical(), hard_breaches=hb)
    assert q["score"] <= sq.WEEK_HARD_BREACH_CEILING
    assert q["band"] == "weak"
    assert q["raw_score"] <= sq.WEEK_HARD_BREACH_CEILING
    assert q["held_by"]["key"] == sq.HARD_RULES_KEY
    assert "No manager on the floor" in q["held_by"]["text"]
    assert q["weaknesses"][0] == q["held_by"]["text"]


def test_sq2_more_breaches_score_lower_and_a_fix_is_worth_points():
    rows, _n = _two_shift_week()
    one = [{"kind": "no_manager", "date": "2026-10-10", "hard": True, "label": "No manager",
            "gap_start": 19 * 60, "gap_end": 22 * 60}]
    three = [dict(one[0], date=d) for d in ("2026-10-08", "2026-10-09", "2026-10-10")]
    q1 = sq.score_rows(rows, typical_headcount=_typical(), hard_breaches=one)
    q3 = sq.score_rows(rows, typical_headcount=_typical(), hard_breaches=three)
    # The hold scales rather than clips: fixing one breach of three still
    # moves the number a search chooses by.
    assert q3["raw_score"] < q1["raw_score"]
    rec = next(r for r in q1["recommendation_details"] if r["kind"] == "rules")
    clean = sq.score_rows(rows, typical_headcount=_typical())
    # Fixing the only breach lifts the week's hold too.
    assert rec["points"] >= clean["raw_score"] - q1["raw_score"] - 1


# ── SQ-3: the optimiser puts a hard breach right before anything else ──────

def test_sq3_optimizer_closes_a_manager_gap_and_never_stops_at_target_with_one_open():
    rows, names = _two_shift_week(gap_on=("2026-10-10",))
    c = _constraints(names)
    assert [v["kind"] for v in R.violations(rows, c) if v.get("hard")] == ["no_manager"]
    hard = [v for v in R.violations(rows, c) if v.get("hard")]
    q = sq.score_rows(rows, typical_headcount=_typical(), hard_breaches=hard)
    top = so._problems(q)[0]
    assert top[2]["key"] == sq.HARD_RULES_KEY and top[1]["date"] == "2026-10-10"
    # A target the held week already clears may not stop the search.
    out = so.optimize(rows, inputs={"constraints": c}, signals={"typical_headcount": _typical()},
                      constraints=c, max_seconds=20, target=10)
    assert not [v for v in R.violations(out["rows"], c) if v.get("hard")]
    assert out["after_score"] > out["before_score"]
    assert any("Saturday" in ch["reason"] for ch in out["changes"])


def test_sq3_a_rule_move_is_not_held_to_the_hours_budget():
    rows, names = _two_shift_week(gap_on=("2026-10-10",))
    c = _constraints(names)
    hours = R.hourly_hours(rows, c)
    out = so.optimize(rows, inputs={"constraints": c}, signals={"typical_headcount": _typical()},
                      constraints=c, max_seconds=20, hours_budget=hours)
    assert not [v for v in R.violations(out["rows"], c) if v.get("hard")]


# ── SQ-4: the labor % and the savings compare like with like ───────────────

def _salaried_week(monkeypatch, salaries=2600.0):
    import models
    monkeypatch.setattr(models, "salaried_staff", lambda r: [{"name": "Erik Baylis", "annual": 67600.0},
                                                             {"name": "Jim Mgr", "annual": 67600.0}])
    monkeypatch.setattr(models, "salaried_week_share",
                        lambda r, dates, closed=(): {"cost": salaries, "people": 2, "trading_days": 7})
    monkeypatch.setattr(R, "closed_in", lambda r, dates: set())


def test_sq4_owner_sees_all_in_labor_against_the_all_in_target(monkeypatch):
    import schedule_economics as E
    _salaried_week(monkeypatch)
    lv = E.labor_view(1, {"total": 4500.0}, 18000.0, labor_target=30, labor_budget_dollars=2800,
                      week_dates=WEEK, restaurant=object(), sees_salaries=True,
                      recent={"pct": 39.4, "days": 14, "includes_salaries": True})
    assert lv["basis"] == "all_in" and lv["cost"] == 7100 and lv["pct"] == 39.4
    assert lv["target_pct"] == 30 and lv["salaried_cost"] == 2600
    # The recent all-in % is this week's: no saving, never the salaries.
    assert lv["savings"] is None


def test_sq4_savings_need_the_recent_percent_on_the_same_basis(monkeypatch):
    import schedule_economics as E
    _salaried_week(monkeypatch)
    # An hourly recent % beside an all-in week is not compared at all.
    lv = E.labor_view(1, {"total": 4500.0}, 18000.0, labor_target=30, week_dates=WEEK, restaurant=object(),
                      sees_salaries=True, recent={"pct": 45.0, "days": 14, "includes_salaries": False})
    assert lv["recent_pct"] is None and lv["savings"] is None
    lv = E.labor_view(1, {"total": 4500.0}, 18000.0, labor_target=30, week_dates=WEEK, restaurant=object(),
                      sees_salaries=True, recent={"pct": 42.0, "days": 14, "includes_salaries": True})
    assert lv["savings"] == round((42.0 - 39.4) / 100 * 18000.0)


def test_sq4_a_manager_sees_hourly_labor_against_the_hourly_budget_and_no_salary(monkeypatch):
    import schedule_economics as E
    _salaried_week(monkeypatch)
    lv = E.labor_view(1, {"total": 4500.0}, 18000.0, labor_target=30, labor_budget_dollars=2800,
                      week_dates=WEEK, restaurant=object(), sees_salaries=False,
                      recent={"pct": 30.0, "days": 14, "includes_salaries": False})
    assert lv["basis"] == "hourly" and lv["pct"] == 25.0
    assert lv["target_pct"] == round(2800 / 18000 * 100, 1) and lv["target_basis"] == "hourly"
    assert "salaried_cost" not in lv and lv["savings"] == round((30.0 - 25.0) / 100 * 18000)


# ── SQ-5: the hours panel's figures are hourly ─────────────────────────────

def test_sq5_hours_view_leaves_salaried_hours_out_of_par_days_roles_and_the_40h_line():
    import schedule_economics as E
    rows = []
    for d in WEEK:
        for m in ("Erik Baylis", "Jim Mgr"):
            rows.append({"date": d, "employee": m, "role": "Manager", "shift_start": "11:00am",
                         "shift_end": "10:00pm", "scheduled_hours": "11"})
        rows.append({"date": d, "employee": "Ann", "role": "Server", "shift_start": "4:00pm",
                     "shift_end": "10:00pm", "scheduled_hours": "6"})
    c = R.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS, roster_names=["Erik Baylis", "Jim Mgr", "Ann"],
                      active={"erik baylis", "jim mgr", "ann"}, salaried={"erik baylis", "jim mgr"})
    c.roster_roles = {"Erik Baylis": "Manager", "Jim Mgr": "Manager", "Ann": "Server"}
    hv = E.hours_view(rows, c, ceiling=40.0)
    assert hv["hourly"] == 42.0 and hv["salaried"] == 154.0
    assert hv["by_date"][WEEK[0]] == 6.0 and "Manager" not in hv["by_role"]
    assert [o["name"] for o in hv["over"]] == ["Ann"]
    assert hv["role_people"] == {"Server": 1}


# ── SQ-6: the edit line prices as the generation prices ────────────────────

def test_sq6_a_salaried_gms_extra_shift_costs_nothing_and_published_hours_count():
    import schedule_economics as E
    dates = WEEK[:5]
    base = [{"date": d, "employee": "Gina GM", "role": "Manager", "shift_start": "10:00am",
             "shift_end": "9:00pm", "scheduled_hours": "11"} for d in dates]
    after = base + [{"date": WEEK[5], "employee": "Gina GM", "role": "Manager", "shift_start": "4:00pm",
                     "shift_end": "10:00pm", "scheduled_hours": "6"}]
    pricing = {"role_rates": {"_default": 16.0}, "blended_rate": 16.0, "person_rates": {}, "role_typical": {},
               "ceiling": 40.0, "salaried": {"gina gm"}, "base_hours": {}, "bucket": None, "daily_ot_hours": None}
    cd = E.cost_delta(base, after, pricing=pricing)
    assert cd["dollars_delta"] == 0 and cd["hours_delta"] == 0 and cd["salaried_hours_delta"] == 6.0
    assert cd["overtime_hours_after"] == 0 and cd["basis"] == "hourly"
    # An hourly person with 36h already published in the payroll week: a 6h
    # add is 2h of overtime, which the old line (no published hours) missed.
    row = {"date": WEEK[0], "employee": "Sam", "role": "Server", "shift_start": "4:00pm",
           "shift_end": "10:00pm", "scheduled_hours": "6"}
    pricing.update(salaried=set(), base_hours={"sam": {"": 36.0}}, bucket=lambda d: "")
    cd = E.cost_delta([], [row], pricing=pricing)
    assert cd["overtime_hours_after"] == 2.0 and cd["dollars_delta"] == round(6 * 16 + 2 * 16 * 0.5)


# ── SQ-7: the trim ranks days on the hourly basis of their targets ─────────

def test_sq7_trim_cuts_the_day_over_its_hourly_target_not_the_salaried_managers_day():
    import schedule_economics as E
    A, B = "2026-10-06", "2026-10-07"
    rows = []

    def add(d, n, role, s, e, h):
        rows.append({"date": d, "day": "", "employee": n, "role": role, "shift_start": s, "shift_end": e,
                     "scheduled_hours": str(h), "notes": ""})
    for d in (A, B):
        for k in range(4):
            add(d, f"S{d[-1]}{k}", "Server", "4:00pm", "11:00pm", 7)
    add(A, "Gina GM", "Manager", "12:00pm", "11:00pm", 11)
    add(B, f"S{B[-1]}x", "Server", "4:00pm", "10:00pm", 6)
    names = sorted({r["employee"] for r in rows})
    c = R.Constraints(restaurant_id=1, week_dates=[A, B], week_days=["Tuesday", "Wednesday"], roster_names=names,
                      active={n.lower() for n in names}, salaried={"gina gm"}, managers={"gina gm": "Manager"})
    _new, trimmed, _h = E.trim_to_budget([dict(r) for r in rows], 60.0, {A: 30.0, B: 30.0}, constraints=c)
    assert trimmed and all(t["date"] == B for t in trimmed)


# ── SQ-9: the late SPLH is on the basis of its target ──────────────────────

def test_sq9_late_hours_leave_the_salaried_owner_out():
    FRI = "2026-10-09"
    rows = [{"date": FRI, "employee": "Erik Owner", "role": "Manager", "shift_start": "6:00pm", "shift_end": "2:00am",
             "scheduled_hours": "8"},
            {"date": FRI, "employee": "Bart", "role": "Bartender", "shift_start": "6:00pm", "shift_end": "2:00am",
             "scheduled_hours": "8"},
            {"date": FRI, "employee": "Sue", "role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm",
             "scheduled_hours": "5"}]
    q = sq.score_rows(rows, profiles=[sq.ShiftProfile(key="std", source="restaurant")],
                      typical_headcount={("Friday", "night"): {"Bartender": 1, "Server": 1, "Manager": 1}},
                      close_times={"Friday": "2:00am"}, salaried=["Erik Owner"],
                      daypart_sales={"Friday": {"night": 1300.0, "late": 400.0}},
                      splh_targets={"Friday": {"night": 100.0, "late": 100.0}})
    night = [x for x in q["shifts"] if x["daypart"] == "night"][0]
    splh = [d for d in night["dimensions"] if d["key"] == "splh"][0]
    assert splh["facts"]["late"]["hours"] == 4.0 and splh["facts"]["late"]["splh"] == 100.0
    assert not any("Late night" in w for w in splh["weaknesses"])


# ── SQ-10: a soft ask held to the section cap never lowers the firm crew ───

def test_sq10_capped_soft_ask_keeps_the_firm_requirement():
    import schedule_requirements as req
    d = "2026-10-09"
    typical = {("Friday", "night"): {"Server": 4, "Cook": 2}}
    asked = req.shift_requirements([d], typical_headcount=typical, section_cap=4, cap_roles={"server"},
                                   adjustments=[{"date": d, "daypart": "night", "role": "server", "delta": 1,
                                                 "reason": "reviews say slow service", "firm": False}])
    plain = req.shift_requirements([d], typical_headcount=typical, section_cap=4, cap_roles={"server"})
    assert req.requirements_map(asked) == req.requirements_map(plain)
    # Under the cap the ask stays an ask on top of the firm crew.
    roomy = req.shift_requirements([d], typical_headcount=typical, section_cap=6, cap_roles={"server"},
                                   adjustments=[{"date": d, "daypart": "night", "role": "server", "delta": 1,
                                                 "reason": "reviews say slow service", "firm": False}])
    server = [r for r in roomy[0]["roles"] if r["role"] == "Server"][0] if roomy[0]["daypart"] == "night" else \
        [r for r in roomy[1]["roles"] if r["role"] == "Server"][0]
    assert server["required"] == 5 and server["firm"] == 4 and server["asked"] == 1


# ── SQ-12: the owner's standard is split across job codes exactly ──────────

def test_sq12_standard_split_sums_to_the_need():
    import labor_standards as L
    roles = {"Server": 1, "Server Patio": 1, "Server Banquet": 1}
    std = {"families": {"server": {"night": {"per_hour": 10.0, "unit": "guests", "source": "yours"}}},
           "slots": {("Friday", "night"): {"work": {"guests": 100}, "hours_per_person": {"server": 5.0}}}}
    out = L.needs_for_week(["2026-10-09"], {("Friday", "night"): roles}, std)
    assert sum(v["people"] for v in out[("2026-10-09", "night")].values()) == 2
    for need, split in ((5, {"A": 3, "B": 2, "C": 2}), (1, {"A": 5, "B": 1}), (7, {"A": 1, "B": 1, "C": 1})):
        got = L.split_people(need, split)
        assert sum(got.values()) == need and set(got) == set(split)


# ── siblings: overtime forecast and the top-up ─────────────────────────────

def test_overtime_forecast_never_names_a_salaried_person():
    import schedule_learning as SL
    rows = [{"date": d, "employee": "Gina GM", "role": "Manager", "shift_start": "10:00am",
             "shift_end": "9:00pm", "scheduled_hours": "11"} for d in WEEK[:5]]
    c = R.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS, roster_names=["Gina GM"],
                      active={"gina gm"}, salaried={"gina gm"})
    assert SL.overtime_forecast(rows, constraints=c) == []
    c.salaried = set()
    assert [f["employee"] for f in SL.overtime_forecast(rows, constraints=c)] == ["Gina GM"]


# ── the screens read the server's figures (web + iOS) ──────────────────────

import os as _os

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _read(*parts):
    with open(_os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _js_fn(src, name):
    i = src.index("function " + name + "(")
    return src[i:src.index("\n}\n", i)]


def test_web_studio_reads_labor_view_and_the_hourly_split():
    s = _read("templates", "dashboard.html")
    summary = _js_fn(s, "ssRenderSummary")
    assert "d.labor_view" in summary and "lv.target_pct" in summary and "lv.savings" in summary
    assert "window._SS_RECENT" not in summary and "pc.total / rev" not in s
    live = _js_fn(s, "swLive")
    assert "_swHours" in live and "hv.by_date" in live and "hv.by_role" in live and "hv.over" in live
    assert "Salaried hours (not paid from PAR)" in live and "lv.basis === 'all_in'" in live
    recheck = _js_fn(s, "_schedRecheck")
    assert "d.hours" in recheck and "history_id: _schedHistoryId" in recheck
    assert "salaried_hours_delta" in _js_fn(s, "_schedRenderCost")
    assert "labor_view: d.labor_view" in s
    assert "quality.held_by" in _js_fn(s, "renderQualityWarnings")


def test_server_sends_what_the_screens_read():
    sr = _read("strategy_routes.py")
    assert 'out["hours"] = _econ_hv.hours_view(rows, c)' in sr
    assert "pricing=_econ.week_pricing(" in sr
    m = _read("mobile_api.py")
    assert "labor_view=labor_view" in m and "_capi.attach_labor_view(" in m
    assert "attach_labor_view(current_user[\"restaurant_id\"], result)" in _read("client_api.py")


def test_ios_decodes_the_new_keys_and_rescores_with_the_weeks_targets():
    vm = _read("ios", "CavnarAI", "CavnarAI", "Features", "Labor", "LaborViewModel.swift")
    models = _read("ios", "CavnarAI", "CavnarAI", "Features", "Labor", "ScheduleFixModels.swift")
    for key in ('"labor_view"', '"daily_target_hours"', '"held_by"', '"salaried_hours_delta"'):
        assert key in vm, key
    for key in ('"target_pct"', '"recent_pct"', '"target_basis"'):
        assert key in models, key
    # UI-5: no re-score sends empty targets with no week id any more.
    assert "dailyTargetHours: [:]" not in vm
    assert "historyId: nil, version: nil" not in vm
    panel = _read("ios", "CavnarAI", "CavnarAI", "Features", "Labor", "ShiftQualityPanel.swift")
    assert "quality.heldBy?.text" in panel and "base?.quality.score" in panel
    notes = _read("ios", "CavnarAI", "CavnarAI", "Features", "Labor", "ScheduleWeekNotes.swift")
    assert "result.laborView" in notes and 'stat("Hourly pay"' in notes
