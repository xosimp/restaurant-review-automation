"""schedule_requirements.py and what the generation prompt is told with it.

The Shift Quality Engine scores every shift against a number per role, a
demand level, a leader requirement, an experience mix and each person's
usual pattern. The prompt used to carry those as prose and name nobody, so
the model was scored on facts it never had. These pin the pure blocks, and
the prompt they land in: one ranked priority list, no competing "highest
priority" claims, the daypart-presence rule worded exactly as the scorer
counts it, and every existing hard rule still present.
"""
import re
import types

import labor
import schedule_requirements as req
import shift_quality as sq


# ── the requirements table ─────────────────────────────────────────────────

FRI, SAT, MON = "2026-10-09", "2026-10-10", "2026-10-05"


def test_required_is_the_larger_of_floor_and_typical_and_the_floor_is_marked():
    rows = req.shift_requirements(
        [FRI],
        typical_headcount={("Friday", "night"): {"Server": 6, "line cook": 2}},
        role_floors={"Line Cook": {"morning": 1, "night": 3}},
    )
    night = next(r for r in rows if r["daypart"] == "night")
    by_role = {x["role"]: x for x in night["roles"]}
    # Case-insensitive merge; the owner's spelling wins.
    assert set(by_role) == {"Server", "Line Cook"}
    assert by_role["Server"] == {"role": "Server", "required": 6, "floor": 0, "typical": 6}
    assert by_role["Line Cook"]["required"] == 3 and by_role["Line Cook"]["floor"] == 3
    morning = next(r for r in rows if r["daypart"] == "morning")
    assert morning["roles"] == [{"role": "Line Cook", "required": 1, "floor": 1, "typical": 0}]


def test_a_per_day_floor_overrides_the_role_default_as_the_scorer_reads_it():
    floors = {"Cook": {"night": 2, "days": {"Saturday": {"night": 4}}}}
    assert req.floor_for(floors["Cook"], "Saturday", "night") == 4
    assert req.floor_for(floors["Cook"], "Friday", "night") == 2
    rows = req.shift_requirements([FRI, SAT], role_floors=floors)
    assert [(r["day"], r["roles"][0]["required"]) for r in rows] == [("Friday", 2), ("Saturday", 4)]


def test_closed_dates_and_roles_nobody_here_can_work_are_left_out():
    typical = {("Friday", "night"): {"Server": 4, "Line Cook": 2}}
    rows = req.shift_requirements([FRI, SAT], typical_headcount=typical, roles={"line cook"}, skip_dates=[SAT])
    assert len(rows) == 1
    assert [x["role"] for x in rows[0]["roles"]] == ["Line Cook"]
    assert req.shift_requirements([FRI], typical_headcount=typical, roles=set()) == []


def test_demand_and_leader_come_from_the_scorers_own_profile_resolution():
    rows = req.shift_requirements([FRI, MON], typical_headcount={("Friday", "night"): {"Server": 4},
                                                                 ("Monday", "night"): {"Server": 2}},
                                  daily_targets={FRI: 60.0}, leadership_known=True)
    fri = next(r for r in rows if r["date"] == FRI)
    expect = sq.profile_for_shift("Friday", "night", None)
    assert fri["demand"] == expect.demand
    assert bool(fri["leader"]) == expect.requires_leader
    assert fri["target_hours"] == 60.0
    mon = next(r for r in rows if r["date"] == MON)
    assert mon["target_hours"] is None


def test_a_recorded_lift_raises_demand_exactly_as_the_scorer_does():
    lifted = {MON: {"lift_pct": 30}}
    rows = req.shift_requirements([MON], typical_headcount={("Monday", "night"): {"Server": 2}},
                                  demand_by_date=lifted)
    assert rows[0]["demand"] == sq.profile_for_shift("Monday", "night", None, None, 30).demand == "peak"


def test_a_leader_requirement_is_not_asked_when_nobody_could_be_identified():
    """The scorer withdraws leadership when nobody is rated or authorised
    to close; the prompt must not demand what cannot be judged."""
    typical = {("Saturday", "night"): {"Server": 4}}
    blind = req.shift_requirements([SAT], typical_headcount=typical, leadership_known=False)
    assert blind[0]["leader"] == []
    known = req.shift_requirements([SAT], typical_headcount=typical, leadership_known=True)
    assert known[0]["leader"]


def test_a_leader_rule_is_named_on_the_shift_it_applies_to():
    rules = [{"role": "Bartender", "days": ["Saturday"], "daypart": "night", "count": 1, "min_score": 4}]
    rows = req.shift_requirements([SAT], typical_headcount={("Saturday", "night"): {"Bartender": 2}},
                                  leader_rules=rules)
    assert rows[0]["leader"] == ["1 Bartender scoring 4+"]


def test_the_block_is_one_line_per_shift_and_never_looks_like_a_date_to_write():
    rows = req.shift_requirements([FRI], typical_headcount={("Friday", "night"): {"Server": 6}},
                                  role_floors={"Server": {"night": 3}}, daily_targets={FRI: 54.5})
    block = req.requirements_block(rows)
    assert block.startswith("\n\nSHIFT REQUIREMENTS — priority 2.")
    line = next(ln for ln in block.splitlines() if ln.startswith("  Fri 2026-10-09 night"))
    assert "Server 6 (floor 3)" in line and "day target 54.5h" in line and "demand" in line
    # The date list the model writes is "- YYYY-MM-DD: Day"; a table line
    # must never read as one (the chunk tests and the missing-day retry
    # count dates that way).
    assert not re.search(r"- \d{4}-\d{2}-\d{2}: ", block)
    assert req.requirements_block([]) == ""


def test_the_presence_rule_in_words_matches_what_the_scorer_counts():
    rule = req.presence_rule()
    lo, hi = sq.CORE_WINDOWS["night"]
    assert f"at least {sq.PRESENCE_MIN_OVERLAP} minutes" in rule
    assert "5:30pm-8:30pm" in rule and (lo, hi) == (17 * 60 + 30, 20 * 60 + 30)
    # The worked examples the rule gives are the scorer's own answers.
    assert "11:30am-7:00pm shift counts at lunch AND at dinner" in rule
    assert set(sq.present_dayparts({"shift_start": "11:30am", "shift_end": "7:00pm"})) == {"morning", "night"}
    assert "10:00am-5:00pm shift counts at lunch only" in rule
    assert sq.present_dayparts({"shift_start": "10:00am", "shift_end": "5:00pm"}) == ["morning"]


# ── people ────────────────────────────────────────────────────────────────

def test_experienced_and_developing_staff_are_named():
    out = req.experience_block({"Ana": 40, "Ben": 3, "Cy": 10}, ["Ana", "Ben", "Cy", "Dee"])
    assert f"{sq.EXPERIENCE_SHIFTS}+ shifts" in out and "Ana." in out
    assert "Still developing" in out and "Ben" in out.split("Still developing")[1]
    # Cy sits between the two bars and Dee has no history: neither listed.
    assert "Cy" not in out and "Dee" not in out


def test_a_history_too_short_for_anyone_says_nothing_about_experience():
    """Two weeks of history makes everybody look new. The scorer withdraws
    the dimension then; the prompt must not claim nobody is experienced."""
    assert req.experience_block({"Ana": 12, "Ben": 3}, ["Ana", "Ben"]) == ""


def test_the_owners_word_makes_somebody_experienced():
    out = req.experience_block({"Ana": 4}, ["Ana", "Ben"], experienced={"ana"})
    assert "EXPERIENCED STAFF" in out and "Ana" in out
    assert "Still developing" not in out     # marked experienced is not developing


def test_people_authorised_to_close_are_named_even_without_tenure():
    out = req.experience_block({}, ["Ana", "Ben"], leader_flags={"Ben": True})
    assert "EXPERIENCED STAFF" not in out
    assert "AUTHORISED TO CLOSE" in out and "Ben" in out


def test_usual_pattern_groups_people_and_caps_its_length():
    pattern = {"Ana": {"days": ["Friday", "Saturday"], "dayparts": ["night"]},
               "Ben": {"days": ["Saturday", "Friday"], "dayparts": ["night"]},
               "Cy": {"days": list(req._DAY_ORDER), "dayparts": ["morning", "night"]},
               "Dee": {"days": [], "dayparts": []}}
    out = req.usual_pattern_block(pattern, ["Ana", "Ben", "Cy", "Dee"])
    assert "Schedule stability is scored" in out
    assert "  Fri/Sat, nights: Ana, Ben" in out
    assert "  any day, day or night: Cy" in out
    assert "Dee" not in out
    many = {f"P{i:03d}": {"days": ["Monday"] if i % 2 else ["Tuesday"], "dayparts": ["night"]} for i in range(400)}
    capped = req.usual_pattern_block(many, sorted(many), cap=300)
    assert len(capped) < 800 and "more people not listed" in capped
    assert req.usual_pattern_block({}, ["Ana"]) == ""


# ── the chunk seam ────────────────────────────────────────────────────────

def test_the_seam_carries_closes_weekends_and_busy_shifts():
    rows = [
        {"date": "2026-10-09", "day": "Friday", "employee": "Ana", "role": "Server",
         "shift_start": "4:00pm", "shift_end": "11:00pm", "scheduled_hours": "7"},
        {"date": "2026-10-09", "day": "Friday", "employee": "Ben", "role": "Server",
         "shift_start": "11:00am", "shift_end": "3:00pm", "scheduled_hours": "4"},
        {"date": "2026-10-10", "day": "Saturday", "employee": "Ana", "role": "Server",
         "shift_start": "5:00pm", "shift_end": "12:30am", "scheduled_hours": "7.5"},
        {"date": "2026-10-05", "day": "Monday", "employee": "Ben", "role": "Server",
         "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6"},
    ]
    lines = {ln.split(":")[0].strip(): ln for ln in req.seam_lines(rows)}
    # Ana closed Friday and Saturday (past midnight), both weekend, both busy
    # by default (Friday and Saturday nights).
    assert "14.5h so far" in lines["Ana"] and "2 closes, 2 weekend, 2 busy" in lines["Ana"]
    # Ben closed Monday (the only shift that day) and worked a Friday lunch.
    assert "1 close, 1 weekend, 0 busy" in lines["Ben"]
    # With the scorer's busy set, a busy Friday lunch counts.
    busy = {("2026-10-09", "morning")}
    lines = {ln.split(":")[0].strip(): ln for ln in req.seam_lines(rows, busy=busy)}
    assert lines["Ben"].endswith("1 busy") and lines["Ana"].endswith("0 busy")


def test_busy_shifts_are_the_ones_scored_high_or_above():
    busy = req.busy_shifts([MON, FRI, SAT])
    for d, day in ((MON, "Monday"), (FRI, "Friday"), (SAT, "Saturday")):
        for part in req.DAYPARTS:
            high = sq.DEMAND_RANK[sq.profile_for_shift(day, part, None).demand] >= sq.DEMAND_RANK[sq.HARD_DEMAND]
            assert ((d, part) in busy) == high


# ── focus for a regeneration ──────────────────────────────────────────────

def test_focus_names_each_weakness_once_and_is_silent_without_any():
    assert req.focus_block(None) == "" and req.focus_block(["  "]) == ""
    out = req.focus_block(["Friday dinner: 1 of 3 line cooks", "Friday dinner: 1 of 3 line cooks", "Ana\nhas 7 closes"])
    assert out.startswith("\n\nTHE PREVIOUS DRAFT OF THESE DAYS SCORED WEAK ON:")
    assert out.count("Friday dinner") == 1 and "Ana has 7 closes" in out
    assert len(req.focus_block([f"issue {i}" for i in range(40)]).splitlines()) <= req.FOCUS_MAX_ITEMS + 4


# ── the prompt ────────────────────────────────────────────────────────────

def _capture(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    return captured


_ANALYSIS = {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
             "total_sales": 60000, "period_days": 21, "by_day": {}}


def _history():
    out = []
    for d in ("2026-09-18", "2026-09-25", "2026-10-02"):
        for i in range(4):
            out.append({"date": d, "employee": f"S{i}", "role": "Server", "shift_start": "16:00",
                        "shift_end": "22:00", "scheduled_hours": 6})
    return out


def _prompt(monkeypatch, **kw):
    captured = _capture(monkeypatch)
    kw.setdefault("roster", [("S0", "Server"), ("S1", "Server"), ("S2", "Server"), ("S3", "Server")])
    labor.generate_optimized_schedule(_ANALYSIS, _history(), restaurant_name="T", hourly_rate=20.0,
                                      labor_target=30.0, week_start="2026-10-05", **kw)
    return captured["messages"][0]["content"]


def test_one_ranked_priority_list_and_no_competing_claims(monkeypatch):
    prompt = _prompt(monkeypatch, staff_notes=[{"employee_name": "S0", "notes": "no Fridays"}],
                     staff_availability=[{"employee_name": "S1", "available_days": "[]",
                                          "unavailable_days": '["Monday"]', "notes": ""}],
                     hours_notes="Open 11am-10pm")
    assert prompt.count("PRIORITIES —") == 1
    assert prompt.index("PRIORITIES —") < prompt.index("CONTEXT:")
    for n, label in ((1, "Hard constraints"), (2, "SHIFT REQUIREMENTS"), (3, "Leadership"),
                     (4, "The weekly hours ceiling"), (5, "Quality preferences")):
        assert f"  {n}. {label}" in prompt
    for gone in ("HIGHEST PRIORITY", "same priority as STAFF CONSTRAINTS", "follow exactly —",
                 "PRIMARY scheduling basis", "— in that order", "must be satisfied, not merely aimed at"):
        assert gone not in prompt, gone
    # Every hard rule is still stated.
    assert "STAFF CONSTRAINTS — priority 1" in prompt and "- S0: no Fridays" in prompt
    assert "the constraint wins" in prompt
    avail = prompt[prompt.index("EMPLOYEE AVAILABILITY"):]
    assert "hard constraint" in avail.split("\n\n")[0].lower() and "S1: NOT available: Monday" in avail
    assert "RESTAURANT HOURS & SHIFT RULES (priority 1" in prompt and "Open 11am-10pm" in prompt


def test_the_requirements_table_and_people_reach_the_prompt(monkeypatch):
    prompt = _prompt(monkeypatch, role_floors={"Server": {"night": 5}},
                     tenure={"S0": 30, "S1": 2}, prior_pattern={"S0": {"days": ["Friday"], "dayparts": ["night"]}})
    assert "SHIFT REQUIREMENTS — priority 2." in prompt
    fri = next(ln for ln in prompt.splitlines() if ln.startswith("  Fri 2026-10-09 night"))
    assert "Server 5 (floor 5)" in fri
    mon = next(ln for ln in prompt.splitlines() if ln.startswith("  Mon 2026-10-05 night"))
    assert "Server 5 (floor 5)" in mon
    assert "EXPERIENCED STAFF" in prompt and "S0." in prompt
    assert "Still developing" in prompt and "S1" in prompt.split("Still developing")[1].split("\n")[0]
    assert "USUAL PATTERN" in prompt and "  Fri, nights: S0" in prompt


def test_the_straight_through_paragraph_states_the_presence_rule(monkeypatch):
    prompt = _prompt(monkeypatch)
    para = next(ln for ln in prompt.splitlines() if ln.startswith("- Server shift length:"))
    assert req.presence_rule() in para
    assert "counts toward the 6, it does not add to it" in para
    # The old wording — any shift extending past 3pm counts at night — is gone.
    assert "any shift that extends into the night daypart" not in prompt


def test_the_ceiling_is_still_a_ceiling_and_a_days_target_is_the_aim_only_when_needed(monkeypatch):
    prompt = _prompt(monkeypatch)
    assert "This is a ceiling, not a quota" in prompt
    assert "Coming in under it is a good outcome when every shift meets its SHIFT REQUIREMENTS" in prompt
    assert "use up to it when the day's shifts need the hours" in prompt
    assert "it never adds them" in prompt


def test_focus_reaches_the_prompt_only_when_given(monkeypatch):
    assert "SCORED WEAK ON" not in _prompt(monkeypatch)
    prompt = _prompt(monkeypatch, focus=["Saturday dinner: no leader on", "S2 has 4 closes against a share of 2"])
    assert "THE PREVIOUS DRAFT OF THESE DAYS SCORED WEAK ON:" in prompt
    assert "  * Saturday dinner: no leader on" in prompt


def test_a_slice_is_told_closes_weekends_and_busy_shifts_so_far(monkeypatch):
    prior = [{"date": "2026-10-09", "day": "Friday", "employee": "S0", "role": "Server",
              "shift_start": "5:00pm", "shift_end": "11:00pm", "scheduled_hours": "6"}]
    prompt = _prompt(monkeypatch, week_slice=["2026-10-10", "2026-10-11"], prior_rows=prior,
                     role_floors={"Server": {"night": 2}})
    block = prompt[prompt.index("ALREADY WRITTEN FOR THE OTHER DAYS"):]
    assert "S0: 6h so far on Fri, last shift Friday until 11:00pm; 1 close, 1 weekend, 1 busy" in block
    assert "closes, weekend shifts and busy shifts" in block
    # The table covers only the slice being written.
    assert "  Fri 2026-10-09" not in prompt and "  Sat 2026-10-10" in prompt
