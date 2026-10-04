"""The schedule prompt as one coherent request (schedule audit 10/3/26,
workstream C1 — PR-2 … PR-33, P-23, E-9, D-19, L-31, SQ-30).

One user message in three blocks: standing instructions first (the same for
every restaurant and every call, a worked example inside, PRIORITIES last),
THIS RESTAURANT'S WEEK next (the same for every call of one generation: the
owner's standing rules, the managers' fixed shifts, the rules marked the way
the code checks them, one ROSTER line per person, the context), THIS REQUEST
last (the dates, their requirements, the seam). Two cache breakpoints, so a
generation's slices, retries and rewrites read the first two blocks from the
cache. One date format, one hours anchor, one rank per owner channel."""
import datetime as dt
import json
import re
import types

import pytest

import ai_guard
import labor
import schedule_engine as se
import schedule_output as so
import schedule_prompt as sp
import schedule_requirements as req
import schedule_rules as sr
import shift_quality as sq

WEEK = [(dt.date(2026, 10, 5) + dt.timedelta(days=i)).isoformat() for i in range(7)]   # a Monday
DAYS = list(sr.DAYS)
NOTE_WORDS = ", ".join(v for v in so.NOTE_VALUES if v)
ANALYSIS = {"overall_labor_pct": 28.0, "total_sales": 60000, "period_days": 21, "by_day": {},
            "overstaffed_days": [{"day": "Monday", "date": "2026-09-28", "labor_pct": 40.2}],
            "understaffed_days": [{"day": "Sunday", "date": "2026-09-27"}],
            "dow_summary": {"Monday": 40.2, "Friday": 22.5},
            "date_range": {"start": "2026-09-14", "end": "2026-10-02", "days": 19}}


def _history(names=("S0", "S1", "S2", "S3"), role="Server"):
    out = []
    for d in ("2026-09-18", "2026-09-25", "2026-10-02"):
        for n in names:
            out.append({"date": d, "employee": n, "role": role, "shift_start": "4:00pm", "shift_end": "10:00pm",
                        "scheduled_hours": 6})
    return out


def _every_day_history(names=("S0", "S1", "S2", "S3")):
    """Two weeks with shifts every day: every weekday is a trading day."""
    return [{"date": (dt.date(2026, 9, 21) + dt.timedelta(days=k)).isoformat(), "employee": n, "role": "Server",
             "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": 6} for k in range(14) for n in names]


def _model(monkeypatch, answer=None, model="claude-opus-5-5"):
    """The schedule call captured; `answer(kwargs)` writes the JSON reply."""
    seen = []

    def fake(client, **kw):
        seen.append(kw)
        body = answer(kw) if callable(answer) else (answer or {"days": [], "summary": ["ok"]})
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(body))],
                                     stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "model_for", lambda k: model)
    return seen


def _call(monkeypatch, analysis=None, history=None, answer=None, **kw):
    seen = _model(monkeypatch, answer)
    kw.setdefault("restaurant_name", "T")
    kw.setdefault("hourly_rate", 20.0)
    kw.setdefault("labor_target", 30.0)
    kw.setdefault("week_start", WEEK[0])
    kw.setdefault("roster", [(f"S{i}", "Server") for i in range(4)])
    out = labor.generate_optimized_schedule(analysis or ANALYSIS, _history() if history is None else history, **kw)
    return seen[-1], out


def _blocks(kw):
    return [b["text"] for b in kw["messages"][0]["content"]]


def _prompt(kw):
    return sp.prompt_text(kw["messages"][0]["content"])


def _outside_fences(text):
    for o, c in ((ai_guard.UNTRUSTED_OPEN, ai_guard.UNTRUSTED_CLOSE),
                 (ai_guard.OWNER_RULE_OPEN, ai_guard.OWNER_RULE_CLOSE)):
        text = re.sub(re.escape(o) + r".*?" + re.escape(c), "<FENCE>", text, flags=re.S)
    return text


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    if c.roster_names and not c.active:
        c.active = {c.key(n) for n in c.roster_names}
    return c


# ── PR-26, P-23: three blocks, two cache breakpoints ──────────────────────

def test_pr26_three_blocks_the_first_two_cached(monkeypatch):
    kw, _out = _call(monkeypatch)
    content = kw["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "text", "text"]
    assert [b.get("cache_control") for b in content] == [{"type": "ephemeral"}, {"type": "ephemeral"}, None]
    static, week, request = _blocks(kw)
    assert static == sp.static_block(True, NOTE_WORDS)
    assert week.startswith("THIS RESTAURANT'S WEEK — T: Mon 2026-10-05 to Sun 2026-10-11.")
    assert request.startswith("THIS REQUEST — write shifts for these dates only:\n- Mon 2026-10-05")
    assert sp.request_dates(content) == WEEK
    # The order the model reads: PRIORITIES closes the standing part; the
    # restaurant's name and data window come only after it.
    prompt = _prompt(kw)
    assert (prompt.index("PRIORITIES —") < prompt.index("\n\nTHIS RESTAURANT'S WEEK — T:")
            < prompt.index("- Data window:"))
    assert (prompt.index("CONTEXT:") < prompt.index("THIS REQUEST — write")
            < prompt.index("SHIFT REQUIREMENTS — priority 2."))


def test_pr26_the_standing_instructions_are_the_same_for_every_restaurant_and_week(monkeypatch):
    a, _ = _call(monkeypatch, restaurant_name="Alpha", roster=[("Zora", "Server")], hours_notes="Open 11am-10pm")
    b, _ = _call(monkeypatch, restaurant_name="Beta", roster=[("Quill", "Line Cook"), ("Yu", "Host")],
                 week_start="2026-11-02", sched_notes="Fridays are the week", history=[])
    assert _blocks(a)[0] == _blocks(b)[0]
    assert a["system"] == b["system"]
    # Nothing about a restaurant or a week sits in it.
    for word in ("Alpha", "Beta", "Zora", "Quill", "2026-11-02", "2026-10-05", "Open 11am"):
        assert word not in _blocks(a)[0], word


def test_p23_every_call_of_a_generation_reads_the_same_first_two_blocks(monkeypatch):
    """Two slices and a retry for a day the second slice skipped: the week
    part is byte-identical on all three calls, so only THIS REQUEST is new."""
    calls = []

    def answer(kw):
        calls.append(kw)
        dates = sp.request_dates(kw["messages"][0]["content"])
        if len(calls) == 2:
            dates = dates[:-1]                       # the second slice leaves Sunday out
        return {"days": [{"date": d, "shifts": [{"employee": "S0", "role": "Server", "start": "4:00pm",
                                                  "end": "10:00pm"}]} for d in dates], "summary": ["ok"]}
    _model(monkeypatch, answer)
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: se.CHUNK_ROWS_PER_CALL + 40)
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: dt.datetime(2026, 10, 5))
    roster = [(f"S{i}", "Server") for i in range(4)]
    out = se._generate_in_parts(ANALYSIS, _every_day_history(), roster,
                                {"tz_name": None, "week_start": WEEK[0], "closed_dates": [], "roster": roster,
                                 "restaurant_name": "T", "hourly_rate": 20.0, "labor_target": 30.0})
    assert len(calls) == 3 and se._missing_dates(out["schedule_csv"], WEEK) == []
    static, week = {_blocks(k)[0] for k in calls}, {_blocks(k)[1] for k in calls}
    assert len(static) == 1 and len(week) == 1
    requests = [_blocks(k)[2] for k in calls]
    assert len(set(requests)) == 3
    assert sp.request_dates(calls[2]["messages"][0]["content"]) == [WEEK[6]]
    assert "YOUR PREVIOUS ANSWER WROTE NO SHIFTS FOR Sun 2026-10-11" in requests[2]
    # Each slice names its own dates; the week part names them all.
    assert [sp.request_dates(k["messages"][0]["content"]) for k in calls[:2]] == [WEEK[:4], WEEK[4:]]


# ── PR-2: the owner's words, one block, every channel ranked ──────────────

def test_pr2_every_owner_channel_sits_in_one_block_at_its_rank(monkeypatch):
    rule = ai_guard.wrap_owner_rule("Two servers on Friday dinner")
    kw, _ = _call(monkeypatch, hours_notes="Open 11am-10pm daily", owner_rules_text=rule,
                  staff_notes=[{"employee_name": "S0", "notes": "no Fridays"}],
                  sched_notes="Monday is where somebody new learns", instruction="Give S2 a weekend off")
    static, week, _request = _blocks(kw)
    at = week.index("THE OWNER'S STANDING RULES — everything the owner and the managers have told the schedule")
    assert week.index("THIS RESTAURANT'S WEEK") < at < week.index("ROSTER — ")
    block = week[at:week.index("ROSTER — ")]
    for head in ("RESTAURANT HOURS & SHIFT RULES (priority 1b for the opening, closing and arrival times",
                 "THE OWNER'S STANDING RULES (OWNER_RULE) — priority 2, beside the staffing floors",
                 "STAFF CONSTRAINTS — priority 1b (hard constraints)",
                 "ADDITIONAL SCHEDULING NOTES — priority 5",
                 "THE OWNER'S REQUEST FOR THIS DRAFT — priority 5"):
        assert head in block, head
    # PRIORITIES ranks every channel in one line, and the system prompt
    # defers to it rather than ranking OWNER_RULE on its own.
    assert ("Where the owner's words rank: RESTAURANT HOURS & SHIFT RULES 1b for opening, closing and arrival "
            "times") in static
    assert "the owner's standing rules (OWNER_RULE) 2; ADDITIONAL SCHEDULING NOTES and THE OWNER'S REQUEST" in static
    assert "it ranks where the request's PRIORITIES put the owner's standing rules" in kw["system"]
    assert "follow it unless" not in kw["system"]
    # Each of the owner's words is said once.
    prompt = _prompt(kw)
    for words in ("Two servers on Friday dinner", "Open 11am-10pm daily", "no Fridays",
                  "Monday is where somebody new learns", "Give S2 a weekend off"):
        assert prompt.count(words) == 1, words


# ── PR-3, PR-4: one weekly limit per person, minimums ranked and counted ──

def test_pr3_the_roster_max_is_the_codes_max_and_no_literal_40_is_left(monkeypatch):
    c = _c(roster_names=["Andre", "Bea"], hours_limits={"andre": (30.0, 45.0)})
    facts = sr.person_facts(c, ["Andre", "Bea"])
    people = {p["name"]: p for p in labor._roster_people([("Andre", "Line Cook"), ("Bea", "Line Cook")],
                                                         facts=facts)}
    assert c.max_hours("Andre") == 45.0
    assert people["Andre"]["hours"] == "30-45h, overtime past 40h allowed for them"
    assert people["Bea"]["hours"] == ""                       # the restaurant's default, said once in the head
    # The code agrees with the line: 45h passes, 46h is the hard breach.
    def week(hours):
        return [{"date": d, "day": DAYS[i], "employee": "Andre", "role": "Line Cook", "shift_start": "8:00am",
                 "shift_end": "5:00pm", "scheduled_hours": str(h)} for i, (d, h) in enumerate(zip(WEEK, hours))]
    kinds = lambda rows: {v["kind"] for v in sr.violations(rows, c, person_only=True) if v["hard"]}
    assert "over_max_hours" not in kinds(week([9, 9, 9, 9, 9]))
    assert "over_max_hours" in kinds(week([9, 9, 9, 9, 10]))
    # The standing instructions name no hour figure of their own.
    static = sp.static_block(True, NOTE_WORDS)
    assert not re.search(r"\b40\s?h\b|\b40 hours\b", static)
    assert "overtime" not in labor.STAFF_CONSTRAINTS_RULE.split("hard limits", 1)[1].split(")")[0]


def test_pr4_minimum_hours_are_ranked_counted_and_carried_across_the_seam():
    static = sp.static_block(True, NOTE_WORDS)
    assert ("Fill them from the people still under their minimum hours first (MIN in the ROSTER; a full-timer's "
            "is the full-time line)") in static
    assert "[SOFT] minimum, [HARD] maximum" in sp.ROSTER_HEAD
    prior = [{"date": WEEK[4], "day": "Friday", "employee": "S0", "role": "Server", "shift_start": "5:00pm",
              "shift_end": "11:00pm", "scheduled_hours": "6"}]
    lines = req.seam_lines(prior, limits={"S0": {"min": 30.0, "ot": 40.0, "carried": {}}},
                           payroll_weeks={d: WEEK[0] for d in WEEK})
    assert lines == ["  S0: 6h so far on Fri, last shift Fri 2026-10-09 until 11:00pm; 1 close, 1 weekend, 1 busy; "
                     "needs 24h more for their 30h minimum; 34h left before overtime"]


# ── PR-5, PR-27: every rule marked as the code checks it, hard ones with why ──

def _busy_constraints():
    c = _c(roster_names=["Ann", "Kid", "Sal", "Tia", "Bo"], managers={"ann": "Manager"},
           minors={"kid"}, salaried={"sal"}, closers_by_role={"server": {"bo"}},
           trainees={"tia": {"target_role": "Bartender", "trainer": "Bo", "until": "2026-12-01"}},
           role_floors={"Server": {"night": 2}}, role_requirements={"bartender": {"bassett"}})
    c.compliance.update({"daily_ot_hours": 8, "meal_break_after_hours": 6, "max_consecutive_days": 6})
    return c


def test_pr5_every_rule_line_carries_the_tag_the_code_checks_it_with(monkeypatch):
    kinds = []
    real = sr._rule
    monkeypatch.setattr(sr, "_rule", lambda kind, text, why=None: kinds.append(kind) or real(kind, text, why))
    block = sr.prompt_block(_busy_constraints())
    rules = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert rules and all(ln.startswith(("- [HARD] ", "- [SOFT] ")) for ln in rules), rules
    assert len(kinds) >= 15
    for kind in kinds:
        assert kind in sr.HARD | sr.SOFT, kind
        assert sr.rule_tag(kind) == ("[HARD]" if kind in sr.HARD else "[SOFT]")
    # The split, said once in the standing instructions.
    assert "HOW RULES ARE MARKED" in sp.static_block(True, NOTE_WORDS)
    # The old header that called every rule "verified … a breach is flagged" is gone.
    assert "every one of these is verified" not in block


def test_pr27_each_hard_rule_says_why():
    block = sr.prompt_block(_busy_constraints())
    line = lambda start: next(ln for ln in block.splitlines() if start in ln)
    assert line("NON-NEGOTIABLE").endswith("— the owner's non-negotiable: somebody is always in charge of the floor.")
    assert line("Availability:").endswith("— the person cannot be there.")
    assert "legal exposure" in line("hours between the end of one shift")
    assert "child-labor law is the owner's legal exposure" in line("MINORS")
    assert line("Nobody works more than").endswith("— the owner's limit on days in a row.")
    assert line("Nobody past their weekly maximum").endswith("— the most hours the owner allows anyone.")
    assert line("Roles: only a role").endswith("— anything else is a role nobody has trained them for.")
    assert line("The staffing floors below are hard").endswith(
        "— the fewest people the owner says a shift can run with.")
    assert line("Closers, by role").endswith("— the owner chose who closes each role.")


# ── PR-6, PR-14: room to reason, and only the contract in force ───────────

def test_pr6_a_thinking_model_reasons_natively_and_is_never_told_to_do_it_silently(monkeypatch):
    for structured in (True, False):
        kw, _ = _call(monkeypatch, structured=structured)
        assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert kw["output_config"]["effort"] == labor.SCHEDULE_EFFORT
        text = _prompt(kw) + kw["system"]
        for gone in ("silently", "slow down internally", "<think>", "without narrating", "in your head"):
            assert gone not in text, (structured, gone)


def test_pr14_json_mode_carries_no_csv_text_and_no_model_facing_audit_tags(monkeypatch):
    kw, _ = _call(monkeypatch, staff_notes=[{"employee_name": "S0", "notes": "no Fridays"}])
    prompt = _prompt(kw)
    for csv_era in ("CSV", "column order", "scrambled", "emoji anywhere", "---SUMMARY---", "date,day,employee"):
        assert csv_era not in prompt, csv_era
    tag = re.compile(r"\b(?:PR|SQ|NS\d|DH\d|SCHED|PROMPTS|INT|CA\d|BM\d)-\d+\b|\b[DEPL]-\d+\b|re-audit|audit \d")
    assert not tag.search(prompt), tag.search(prompt)
    assert not tag.search(kw["system"])
    assert "below" not in kw["system"]          # the system prompt never points into the user turn


# ── PR-7, PR-10: context blocks are context; defaults rank last ───────────

LEVERS = re.compile(r"by a person or two|add there last|trim there first|add hours where|a second body|"
                    r"scale (?:that day's|the day's) headcount|prioriti[sz]e closers|more kitchen labor|"
                    r"starting point|proportionally across roles|do not thin it|universal", re.I)


def test_pr7_pr10_the_context_blocks_carry_no_add_or_trim_verbs(monkeypatch):
    import demand_signals
    import schedule_economics as econ
    import staffing_signals
    kw, _ = _call(monkeypatch, daypart_split="lunch 30%, dinner 70%", delivery_pct=25, section_count=4,
                  weather_forecast=[{"date": WEEK[4], "high_f": 60, "short_forecast": "Rain", "precip_pct": 80}])
    rendered = [_prompt(kw),
                demand_signals.prompt_block({WEEK[4]: {"labels": ["Homecoming"], "lift_pct": 30},
                                             WEEK[5]: {"labels": ["Trivia"], "assumed": True, "lift_pct": None}}, WEEK),
                econ.splh_block({"Friday": {"lunch": {"splh": 90.0}, "dinner": {"splh": 60.0}}}),
                staffing_signals.soft_block([{"day": "Friday", "date": WEEK[4], "daypart": "night", "text":
                                              "+1 Server (the reviews diagnosis)"}]),
                se._reliability_block({})]
    for text in rendered:
        assert not LEVERS.search(text), LEVERS.search(text)
    prompt = rendered[0]
    assert "DAYPART REVENUE SPLIT (the owner's estimate): lunch 30%, dinner 70%. Context:" in prompt
    assert "OFF-PREMISE SALES (the owner's figure): 25% of revenue" in prompt
    # The heuristics are defaults, ranked last, and say so.
    static = sp.static_block(True, NOTE_WORDS)
    assert ("HOW TO WRITE SHIFTS — defaults. They apply only where this restaurant's own data (SHIFT REQUIREMENTS, "
            "its RESTAURANT HOURS & SHIFT RULES, its ROSTER and history) says nothing, and they rank last "
            "(PRIORITIES 5).") in static
    for gone in ("commonly ~8:30-9pm", "At most 1-2 servers", "below,"):
        assert gone not in static


def test_pr9_starts_follow_the_ramp():
    static = sp.static_block(True, NOTE_WORDS)
    assert ("Starts follow the half-hour ramp: people of one role start together only when the ramp or a floor "
            "needs them on at the same time") in static
    assert "Never schedule two employees in the same role at the exact same start time" not in static


# ── PR-8: a date's lift is said once ──────────────────────────────────────

def test_pr8_a_dates_lift_is_said_once_in_its_requirements_row(monkeypatch):
    import demand_signals
    import time_utils
    fri = WEEK[4]
    away = (dt.date.fromisoformat(fri) - time_utils.restaurant_now(None, naive=True).date()).days
    dd = {fri: {"ratio": 1.4, "pct": 40, "sources": ["holiday"], "reasons": ["Homecoming: +40% (measured 3 times)"],
                "projected_sales": 7000.0}}
    yoy = [{"next_week_dow": "Friday", "next_week_date": fri, "yoy_date": "2025-10-10", "yoy_dow": "Friday",
            "yoy_sales": 9000.0, "holiday_matched": True, "is_holiday": True, "holiday_name": "Homecoming"}]
    kw, _ = _call(monkeypatch, date_demand=dd, yoy_context=yoy, role_floors={"Server": {"night": 2}},
                  upcoming_events=[{"name": "Homecoming", "date_str": "Oct 9", "days_away": away}],
                  extra_blocks=demand_signals.prompt_block({fri: {"labels": ["Homecoming"], "lift_pct": 40,
                                                                  "lift_source": "measured", "measured_n": 3}}, WEEK))
    prompt = _prompt(kw)
    # Every mention of the figure is on that date's own SHIFT REQUIREMENTS
    # row, with its reason — none in the YoY line, the holidays line or the
    # dated facts, where the model could stack it again.
    at = prompt.index(f"\n  Fri {fri} night") + 1
    row = prompt[at:prompt.index("\n", at)]
    hits = [m.start() for m in re.finditer("40%", prompt)]
    assert hits and all(at <= h < at + len(row) for h in hits), [prompt[h - 80:h + 10] for h in hits]
    assert "Homecoming: +40% (measured 3 times)" in row
    for gone in ("USE THIS", "match staffing to last year's holiday labor hours", "expect about",
                 "scale that day's headcount"):
        assert gone not in prompt, gone
    assert f"Holidays this week: Homecoming (Fri {fri})" in prompt


# ── PR-16: a slice is told its share and each person's room ───────────────

def test_pr16_a_slice_gets_its_share_of_the_budget_and_the_seam_its_room(monkeypatch):
    analysis = dict(ANALYSIS, by_day={d: {"actual": 30.0} for d in
                                      ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
                                       "2026-09-26", "2026-09-27")})
    prior = [{"date": WEEK[0], "day": "Monday", "employee": "S0", "role": "Server", "shift_start": "4:00pm",
              "shift_end": "10:00pm", "scheduled_hours": "6"}]
    kw, out = _call(monkeypatch, analysis=analysis, week_slice=WEEK[4:], prior_rows=prior,
                    monthly_revenue_target=365000.0,
                    roster_facts={"S0": {"min": 30.0, "ot": 40.0, "max": 40.0, "ceiling": 40.0, "carried": []}})
    request = _blocks(kw)[2]
    share = round(sum(out["daily_target_hours"][d] for d in WEEK[4:]), 1)
    assert f"HOURS FOR THESE DATES: their day targets add up to {share:g}h of the week's" in request
    seam = request[request.index("ALREADY WRITTEN FOR THE OTHER DAYS OF THIS WEEK"):]
    assert "S0: 6h so far on Mon" in seam and "needs 24h more for their 30h minimum" in seam
    assert "34h left before overtime" in seam
    assert "days off" not in seam.split("\n", 1)[0]


# ── PR-20: one date format, no Python reprs ───────────────────────────────

def test_pr20_every_date_the_model_reads_is_weekday_and_iso(monkeypatch):
    kw, _ = _call(monkeypatch, staff_notes=[{"employee_name": "S0", "notes": "off 10/9/26 for a wedding",
                                             "created_at": "2026-09-30 10:00:00"}],
                  weather_forecast=[{"date": WEEK[5], "high_f": 70, "short_forecast": "Sunny", "stale": True,
                                     "as_of": "2026-10-01T10:00:00+00:00"},
                                    {"date": WEEK[6], "high_f": 72, "short_forecast": "Sunny"}])
    prompt = _outside_fences(_prompt(kw))
    assert not re.search(r"(?<![\d/])\d{1,2}/\d{1,2}/\d{2,4}(?![\d/])", prompt)
    for m in re.finditer(r"(\d{4})-(\d{2})-(\d{2})", prompt):
        d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        assert prompt[m.start() - 4:m.start()] == d.strftime("%a") + " ", prompt[m.start() - 30:m.end()]
    reprs = re.compile(r"\{'|\['|': |\bNone\b(?! of)|\bTrue\b|\bFalse\b")
    assert not reprs.search(prompt), prompt[reprs.search(prompt).start() - 60:][:120]
    assert "- Labor % by weekday: Monday 40.2%, Friday 22.5%" in prompt
    assert "- Recent overstaffed days: Mon 2026-09-28 (40.2%)" in prompt
    assert "- Recent understaffed days: Sun 2026-09-27" in prompt
    # People's own words keep their own dates, inside their fence.
    assert "off 10/9/26 for a wedding" in _prompt(kw)


def test_pr20_the_owner_reads_the_models_dates_as_mdy(monkeypatch):
    kw, out = _call(monkeypatch, answer={"days": [], "summary": ["Fri 2026-10-09 dinner gets a fifth server"]})
    assert all("2026-10-09" not in b for b in out["summary"])
    assert sp.owner_dates("Fri 2026-10-09 dinner, Saturday 2026-10-10 lunch") == "Fri 10/9/26 dinner, Saturday 10/10/26 lunch"


# ── PR-21: one CAN WORK standard ──────────────────────────────────────────

def test_pr21_can_work_is_held_roles_proven_roles_and_certificates_never_a_single_pickup():
    from schedule_intel import MENTOR_SHIFTS_TO_HOLD
    # As build_constraints files them: the roster role, held roles and
    # every role worked, all "known" to the code's looser legality test.
    c = _c(roster_names=["Ana", "Bo", "Cy"], held_roles={"bo": {"bartender"}},
           known_roles={"ana": {"server", "host"}, "bo": {"server", "bartender"}, "cy": {"server", "host"}},
           role_names={"bartender": "Bartender", "server": "Server", "host": "Host"},
           role_requirements={"bartender": {"bassett"}})
    facts = sr.person_facts(c, ["Ana", "Bo", "Cy"])
    facts["Ana"]["role_shifts"] = {"Server": 40, "Host": MENTOR_SHIFTS_TO_HOLD}         # proven: kept
    facts["Cy"]["role_shifts"] = {"Server": 40, "Host": 1}                              # one pickup: not
    people = {p["name"]: p["can_work"] for p in labor._roster_people(
        [("Ana", "Server"), ("Bo", "Server"), ("Cy", "Server")], facts=facts)}
    assert people["Ana"] == "Server, Host"                     # 8+ shifts as a Host: proven
    assert people["Cy"] == "Server"                            # one Host pickup is not a role
    # Bo holds Bartender, but the role needs a certificate Bo lacks.
    assert facts["Bo"]["held"] == ["Bartender"]
    assert people["Bo"] == "Server, not Bartender (needs bassett)"


def test_pr21_cross_trained_costs_nothing_extra_is_gone(monkeypatch):
    kw, _ = _call(monkeypatch, history=_history() + [{"date": "2026-10-02", "employee": "S0", "role": "Host",
                                                       "shift_start": "11:00am", "shift_end": "3:00pm",
                                                       "scheduled_hours": 4}])
    prompt = _prompt(kw)
    assert "costs nothing extra" not in prompt and "CROSS-TRAINED" not in prompt and "TRAINED UP" not in prompt
    assert "Each role is paid its own rate" in prompt


# ── PR-22: retired rules never reach the prompt ───────────────────────────

def test_pr22_retired_rules_stay_out_whatever_is_stored(monkeypatch):
    c = _c(roster_names=["Ann"], managers={"ann": "Manager"})
    c.compliance.update({"min_consecutive_days_off": 2, "part_time_days_off": 1, "manager_on_duty": True})
    block = sr.prompt_block(c)
    kw, _ = _call(monkeypatch, rules_block=block, extra_blocks=None,
                  prior_rows=[{"date": WEEK[0], "day": "Monday", "employee": "S0", "role": "Server",
                               "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6"}],
                  week_slice=WEEK[1:])
    prompt = _prompt(kw)
    for gone in ("CONSECUTIVE DAYS OFF", "consecutive days off", "No employee over 40h",
                 "Every open daypart has somebody authorized to close", "placed LAST"):
        assert gone not in prompt, gone
    seam_head = prompt[prompt.index("ALREADY WRITTEN FOR THE OTHER DAYS OF THIS WEEK"):].split("\n", 1)[0]
    assert "days off" not in seam_head


# ── PR-23: one no-show rule ───────────────────────────────────────────────

def test_pr23_one_rule_for_people_who_miss_shifts(monkeypatch):
    kw, _ = _call(monkeypatch)
    prompt = _prompt(kw)
    assert prompt.count("pair them with a dependable teammate") == 1
    assert "Never add a person beyond SHIFT REQUIREMENTS for it" in prompt
    for gone in ("a second body", "NO-SHOW BUFFER", "is the fix"):
        assert gone not in prompt, gone


# ── PR-24: one hours anchor ───────────────────────────────────────────────

def test_pr24_each_dates_hours_are_said_once_and_the_ceiling_only_removes(monkeypatch):
    import schedule_economics as econ
    analysis = dict(ANALYSIS, by_day={d: {"actual": 30.0} for d in
                                      ("2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
                                       "2026-09-26", "2026-09-27")})
    kw, out = _call(monkeypatch, analysis=analysis, monthly_revenue_target=365000.0,
                    role_floors={"Server": {"night": 2}})
    prompt = _prompt(kw)
    par = prompt[prompt.index("PAR HOURS CEILING"):]
    par = par[:par.index("\n\n")]
    assert "Per-day targets" not in par and "Each date's day target in SHIFT REQUIREMENTS is its share of it" in par
    assert "The hours ceiling only ever removes hours; it never adds them." in par
    assert len(par) < 900, len(par)
    for d in WEEK:
        t = f"day target {out['daily_target_hours'][d]:g}h"
        rows = [ln for ln in prompt.splitlines() if ln.startswith(f"  {DAYS[WEEK.index(d)][:3]} {d}")]
        assert rows and all(t in ln for ln in rows if " late night " not in ln), d
    assert "must not EXCEED" not in prompt
    # The productivity pace carries no second hours figure.
    obj = {"available": True, "by_day": {"Saturday": {"morning": 100.0, "night": 60.0}},
           "targets": {"morning": {"target": 100.0}, "night": {"target": 60.0}},
           "daypart_sales": {"Saturday": {"morning": 1000.0, "night": 3000.0}}, "basis": "your own pace"}
    assert "≈" not in econ.splh_objective_block(obj, [WEEK[5]])


# ── PR-25: the worked example counts the way the scorer counts ────────────

def _example_rows():
    return [{"date": WEEK[4], "day": "Friday", "employee": n, "role": r, "shift_start": s, "shift_end": e,
             "scheduled_hours": "0", "notes": note} for n, r, s, e, note, _p in sp.EXAMPLE_ROWS]


def _m(clock):
    return sq._slot_minutes(clock)


def test_pr25_the_example_is_in_the_standing_part_and_identical_on_every_call():
    static = sp.static_block(True, NOTE_WORDS)
    ex = static[static.index("<example>"):static.index("</example>")]
    assert ex in sp.static_block(False, NOTE_WORDS)
    for name, role, s, e, note, planned in sp.EXAMPLE_ROWS:
        assert f"{name:<4} {role:<9} {s}-{e}" in ex
        assert (note in so.NOTE_VALUES) and (not note or f"note: {note}" in ex)


def test_pr25_the_example_meets_its_own_needs_by_the_scorers_rules():
    rows = _example_rows()
    # Presence: who counts at lunch and at dinner, by shift_quality's rule.
    for part, needs in sp.EXAMPLE_NEEDS.items():
        for role, n in needs.items():
            here = {r["employee"] for r in rows if r["role"] == role and part in sq.present_dayparts(r)}
            assert len(here) == n, (part, role, sorted(here))
    assert set(sq.present_dayparts(next(r for r in rows if r["employee"] == "Ben"))) == {"morning", "night"}
    # The half-hour ramps, from each ramp point through the daypart.
    close = _m(sp.EXAMPLE_CLOSE)
    for (role, part), ramp in sp.EXAMPLE_RAMPS.items():
        points = [(_m(t), n) for t, n in ramp]
        end = sq.DAYPART_CUTOVER if part == "morning" else close
        for t in range(points[0][0], end, sq.SLOT_MINUTES):
            need = [n for m, n in points if m <= t][-1]
            on = sum(1 for r in rows if r["role"] == role and _m(r["shift_start"]) <= t < _m(r["shift_end"]))
            assert on == need, (role, part, t, on, need)
    # The night floor holds over the window the scorer holds it over.
    for role, n in sp.EXAMPLE_FLOORS["night"].items():
        res = sq.floor_shortfall(rows, role, n, "night", _m(sp.EXAMPLE_OPEN), close)
        assert res["held"], res
    # A manager on every minute anyone is: the code's own manager gaps.
    c = _c(roster_names=[r[0] for r in sp.EXAMPLE_ROWS], managers={"dana": "Manager", "eli": "Manager"})
    assert sr.manager_gaps(rows, c) == {}
    # One straight-through server, everyone else a clear lunch or dinner.
    both = [r["employee"] for r in rows if r["role"] == "Server" and len(sq.present_dayparts(r)) == 2]
    assert both == ["Ben"]


# ── PR-33: one ROSTER line per person, nobody capped away ─────────────────

def test_pr33_sixty_four_people_sixty_four_lines(monkeypatch):
    roster = [(f"P{i:02d} Long-Surname-{i:02d}", ("Server", "Line Cook", "Host", "Busser")[i % 4]) for i in range(64)]
    history = [{"date": d, "employee": n, "role": r, "shift_start": "4:00pm", "shift_end": "10:00pm",
                "scheduled_hours": 6} for d in ("2026-09-25", "2026-10-02") for n, r in roster]
    pattern = {n: {"days": ["Friday", "Saturday"], "dayparts": ["night"], "avg_hours": 24.0,
                   "starts": {"night": "4:00pm"}} for n, _r in roster}
    kw, _ = _call(monkeypatch, roster=roster, history=history, prior_pattern=pattern,
                  tenure={n: 30 for n, _r in roster},
                  staff_availability=[{"employee_name": n, "available_days": "[]", "unavailable_days": '["Monday"]',
                                       "notes": ""} for n, _r in roster[::3]])
    week = _blocks(kw)[1]
    table = week[week.index("ROSTER — "):]
    table = table[:table.index("\n\n")]
    assert "(64 people)" in table
    for n, r in roster:
        assert sum(1 for ln in table.splitlines() if ln.startswith(f"  {n} | ")) == 1, n
    lines = [ln for ln in table.splitlines() if ln.startswith("  P")]
    assert len(lines) == 64 and all(ln.count(" | ") == len(sp.ROSTER_COLUMNS) - 1 for ln in lines)
    assert "Fri/Sat nights, ~24h a week, starts 4:00pm" in lines[0]
    prompt = _prompt(kw)
    for gone in ("not listed", "EMPLOYEE AVAILABILITY", "EXPERIENCED STAFF", "USUAL PATTERN", "USUAL HOURS",
                 "WHAT STAFF WANT", "CROSS-TRAINED", "PER-PERSON LIMITS", "AUTHORIZED TO CLOSE —"):
        assert gone not in prompt, gone


# ── E-9, D-19: carried hours per payroll week ─────────────────────────────

def test_e9_d19_hours_already_published_are_said_per_payroll_week():
    c = _c(roster_names=["Ann"], week_start_day=2)                  # a Wednesday payroll week
    bucket = c.bucket(WEEK[0])
    assert bucket == "2026-09-30" and c.bucket(WEEK[2]) == "2026-10-07"
    c.base_hours = {"ann": {bucket: 36.0}}
    facts = sr.person_facts(c, ["Ann"])
    line = labor._roster_people([("Ann", "Server")], facts=facts)[0]["hours"]
    assert line == ("36h already in the payroll week Wed 2026-09-30 to Tue 2026-10-06 (Mon-Tue here): "
                    "4h left before OT")
    # The code allows exactly that: 4h Mon-Tue and a fresh 40h Wed-Sun.
    rows = [{"date": WEEK[0], "day": "Monday", "employee": "Ann", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "8:00pm", "scheduled_hours": "4"}]
    rows += [{"date": d, "day": DAYS[WEEK.index(d)], "employee": "Ann", "role": "Server", "shift_start": "8:00am",
              "shift_end": "4:00pm", "scheduled_hours": "8"} for d in WEEK[2:]]
    assert not [v for v in sr.violations(rows, c, person_only=True) if v["kind"] == "over_max_hours"]
    # The seam says the room per payroll week too.
    seam = req.seam_lines(rows[:1], limits={"Ann": {"min": None, "ot": 40.0, "carried": {bucket: 36.0}}},
                          payroll_weeks={d: c.bucket(d) for d in WEEK})
    assert seam[0].endswith("overtime room 0h Mon-Tue, 40h Wed-Sun")


# ── L-31: the last published week, labelled, with how it went ─────────────

def test_l31_the_last_published_week_is_named_and_judged():
    c = _c(roster_names=["Ann", "Bo"], managers={"ann": "Manager"})
    last = [{"date": "2026-09-28", "day": "Monday", "employee": "Bo", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    verdict = se._published_week_verdict(last, c, {"quality_score": 61, "quality_band": "fair"})
    assert verdict[0] == "Nobody managing for 6.0h across 1 of its 1 days (Mon 2026-09-28); no manager at all on 1 of them."
    assert verdict[1] == "Shift Quality 61/100 (fair) when last saved."


def test_l31_the_block_says_published_not_generated(monkeypatch):
    kw, _ = _call(monkeypatch, prior_schedule_summary={"Monday": {"Server": {"count": 3, "hours": 18.0}}},
                  prior_week={"start": "2026-09-28", "end": "2026-10-04",
                              "verdict": ["A manager was on every minute anyone was."]})
    prompt = _prompt(kw)
    assert "PREVIOUS GENERATED SCHEDULE" not in prompt
    assert "LAST PUBLISHED WEEK — Mon 2026-09-28 to Sun 2026-10-04" in prompt
    assert "A manager was on every minute anyone was." in prompt


# ── SQ-30: every scored dimension named; leader counts the scorer allows ──

def test_sq30_every_scored_dimension_is_named(monkeypatch):
    kw, _ = _call(monkeypatch, shift_profiles=[sq.ShiftProfile()])
    prompt = _prompt(kw)
    head = prompt[prompt.index("SHIFT PROFILES"):]
    head = head[:head.index("\n")]
    for key, label in sq.DIMENSION_LABELS.items():
        assert label.lower() in head.lower(), (key, label)


def test_sq30_a_leader_rule_asks_only_for_what_the_roster_can_meet(monkeypatch):
    rules = [{"role": "Bartender", "count": 3, "min_score": 5, "days": ["Friday"], "daypart": "night"}]
    kw, _ = _call(monkeypatch, roster=[("Bea", "Bartender"), ("Cal", "Bartender"), ("Dee", "Bartender")],
                  history=_history(("Bea", "Cal", "Dee"), "Bartender"),
                  operational_scores={"Bea": 5, "Cal": 3, "Dee": 2}, leader_rules=rules)
    prompt = _prompt(kw)
    assert "at least 1 Bartender scoring 5 or above (the rule asks 3; only 1 on the roster can)" in prompt
    row = next(ln for ln in prompt.splitlines() if ln.startswith(f"  Fri {WEEK[4]} night"))
    assert "1 Bartender scoring 5+" in row and "3 Bartender" not in row


def test_roster_signals_reads_role_shifts_into_the_facts(monkeypatch):
    """The year's shifts per role reach each person's line: the reader was
    called through a name that was never imported, so the NameError was
    soft-failed and every roster went without CAN WORK's evidence."""
    import schedule_engine
    import schedule_intel
    monkeypatch.setattr(schedule_intel, "role_shifts",
                        lambda rid, *a, **k: {"Ana": {"Server": 12}, "ana": {"Server": 3, "Host": 2}})
    failed = []
    monkeypatch.setattr(schedule_engine, "_soft_fail", lambda *a, **k: failed.append(a))
    facts = {"Ana": {}}
    schedule_engine._roster_signals(facts, 1, display=lambda n: "Ana" if n.lower() == "ana" else n)
    assert not failed
    assert facts["Ana"]["role_shifts"] == {"Server": 15, "Host": 2}
