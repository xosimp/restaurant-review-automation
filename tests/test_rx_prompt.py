"""Schedule re-audit 10/4/26, lens PROMPT: the generation prompt and the
model call (PROMPT-1, 3, 4, 5, 7-13). Each test reproduces the auditor's
finding against the code as it was and holds the fix."""
import json
import re

import pytest

import ai_utils
import models
import schedule_rules as sr
import sys

import test_schedule_b2_calls  # noqa: F401  (imports every module the generation imports lazily)
from test_schedule_b2_calls import WEEK, DAYS, _restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    """Every get_conn — bound copies included — on this test's database, the
    shift reads left real (the Simple EJ's history is read from it)."""
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


# ── PROMPT-9: a cache read is priced per model ─────────────────────────────

def test_opus_5_5_cache_reads_are_priced_at_their_list_rate():
    # Opus 5.5: $4 input, cache reads $0.20/MTok — 0.05x, not 0.1x.
    read = ai_utils._estimate_cost("claude-opus-5-5", 0, 0, cache_read_tokens=1_000_000)
    assert read == pytest.approx(0.20)
    # Fable 5.1 (and Mythos 5.1): $0.25 on $10 — 0.025x.
    assert ai_utils._estimate_cost("claude-fable-5-1", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.25)
    assert ai_utils._estimate_cost("claude-mythos-5-1", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.25)
    # Every other model reads at 0.1x: Sonnet 5.5 $0.20 on $2, Fable 5 $1 on $10.
    assert ai_utils._estimate_cost("claude-sonnet-5-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.20)
    assert ai_utils._estimate_cost("claude-fable-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(1.00)
    # A 5-minute cache write stays 1.25x of input.
    assert ai_utils._estimate_cost("claude-opus-5-5", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(5.00)
    assert ai_utils.PRICE_VERSION >= "2026-10-04"


# ── PROMPT-10: a default is never presented as the owner's choice ──────────

def _line(block, start):
    return next(ln for ln in block.splitlines() if start in ln)


def test_a_default_limit_is_not_called_the_owners(db):
    rid = _restaurant(db)
    c = sr.build_constraints(rid, WEEK, DAYS, db_path=db)
    block = sr.prompt_block(c)
    assert "the owner's limit" not in _line(block, "Nobody works more than")
    assert _line(block, "Nobody works more than").endswith("the default limit on days in a row: the owner has set none.")
    assert "the owner allows anyone" not in _line(block, "Nobody past their weekly maximum")
    assert _line(block, "Nobody past their weekly maximum").endswith("the default ceiling: the owner has set none.")
    # Set by the owner, it is theirs.
    conn = models.get_conn(db)
    conn.execute("UPDATE restaurants SET compliance_json=? WHERE id=?",
                 (json.dumps({"max_consecutive_days": 5, "weekly_hours_ceiling": 38}), rid))
    conn.commit()
    conn.close()
    c = sr.build_constraints(rid, WEEK, DAYS, db_path=db)
    block = sr.prompt_block(c)
    assert _line(block, "Nobody works more than 5 days").endswith("the owner's limit on days in a row.")
    assert _line(block, "Nobody past their weekly maximum").endswith("the most hours the owner allows anyone.")


# ── PROMPT-5: PRIORITIES ranks a staffing floor and a closer once ──────────

import schedule_prompt as sp  # noqa: E402
import schedule_output as so  # noqa: E402

NOTE_WORDS = ", ".join(v for v in so.NOTE_VALUES if v)


def test_priorities_rank_floors_and_closers_at_two_only():
    static = sp.static_block(True, NOTE_WORDS)
    pri = static[static.index("PRIORITIES —"):]
    one_b = pri[pri.index("1b."):pri.index("  2. ")]
    two = pri[pri.index("  2. "):pri.index("  3. ")]
    # 1b is each person's own limits — never "every [HARD] rule", which
    # swept floors and closers into 1b while item 2 put them below it.
    assert "every [HARD] rule" not in one_b
    for word in ("floor", "closer", "arrival"):
        assert word not in one_b, word
    assert "availability" in one_b and "minor" in one_b and "rest" in one_b
    # 2: the floors, the closers, somebody until close and the hours a role
    # may start, hard, below every item of 1.
    assert "below every item of 1" in two
    for word in ("staffing floors", "closer", "on until close", "start and end", "[HARD]"):
        assert word in two, word
    # The owner's words: the hours notes' staffing rules sit at 2, its
    # opening and closing times at 1b.
    assert "2 for every staffing rule in them" in pri


# ── PROMPT-7: the fallback contracts never contradict their instructions ──

def test_the_answer_date_is_the_bare_iso_date_on_every_contract():
    for structured in (True, False):
        static = sp.static_block(structured, NOTE_WORDS)
        assert "write each date exactly as THIS REQUEST lists it" not in static
        assert "a date is the ISO date alone, without its weekday" in static
    assert "never with its weekday in front" in sp.static_block(False, NOTE_WORDS)


def test_the_shape_without_enums_never_points_at_a_list_of_times():
    plain = sp.static_block(True, NOTE_WORDS, enums=False)
    assert "schema's list" not in plain and "schema's clock times" not in plain
    assert "12-hour" in plain
    full = sp.static_block(True, NOTE_WORDS)
    assert "schema's list" in full and full != plain
    # The CSV contract is one text whatever `enums` says.
    assert sp.static_block(False, NOTE_WORDS, enums=False) == sp.static_block(False, NOTE_WORDS)


def test_a_date_written_with_its_weekday_is_read_as_its_iso_date():
    assert so.iso_date_of("Mon 2026-10-05") == "2026-10-05"
    assert so.iso_date_of("2026-10-05") == "2026-10-05"
    assert so.iso_date_of("Monday") == "" and so.iso_date_of("2026-13-45") == ""
    ans = {"days": [{"date": "Mon 2026-10-05", "shifts": [{"employee": "Ana", "role": "Server",
                                                           "start": "4:00pm", "end": "10:00pm"}]}],
           "summary": ["ok"]}
    got = so.parse_answer(json.dumps(ans), dates=["2026-10-05"])
    assert got["complete_dates"] == ["2026-10-05"] and len(got["rows"]) == 1


def test_the_csv_fallback_keeps_a_row_dated_as_the_request_names_it(monkeypatch):
    import types
    import labor
    seen = {}

    def fake(client, **kw):
        seen.update(kw)
        dates = sp.request_dates(kw["messages"][0]["content"])
        rows = "\n".join(f"{sp.day_date(d)},{so._datetime.strptime(d, '%Y-%m-%d').strftime('%A')},Ana,Server,"
                         f"4:00pm,10:00pm,6,closer" for d in dates)
        text = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + rows + \
            "\n---SUMMARY---\n- ok"
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    out = labor.generate_optimized_schedule(
        {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
         "total_sales": 60000, "period_days": 21, "by_day": {}},
        [{"date": "2026-09-28", "employee": "Ana", "role": "Server", "shift_start": "16:00", "shift_end": "22:00",
          "scheduled_hours": 6}],
        restaurant_name="EJ", hourly_rate=20.0, labor_target=30.0, roster=[("Ana", "Server")], structured=False)
    lines = [ln for ln in out["schedule_csv"].splitlines()[1:] if ln.strip()]
    assert lines and all(re.match(r"^\d{4}-\d{2}-\d{2},", ln) for ln in lines), lines[:2]


# ── a realistic fixture: the Simple EJ's demo, end to end ─────────────────

import demo_seed  # noqa: E402
import schedule_engine as se  # noqa: E402


@pytest.fixture
def ejs(db, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None: dt.datetime(2026, 10, 5))
    return demo_seed._seed_simple_ejs(db)


# ── PROMPT-11: a department call is told its own closers only ─────────────

def test_a_kitchen_part_is_told_only_its_own_closers(ejs, db):
    import staff_settings
    c = sr.build_constraints(ejs, WEEK, DAYS, db_path=db)
    roster = [(e["name"], e["role"]) for e in staff_settings.roster(ejs, db_path=db)]
    kitchen = [(n, r) for n, r in roster if r in ("Line Cook", "Prep Cook", "Dishwasher")]
    whole = sr.prompt_block(c)
    assert "Bartender: Jade F., Marcus R." in whole            # the whole week's rules name them
    block = se._chunk_rules_block(c, kitchen)
    line = _line(block, "Closers, by role")
    assert "Line Cook: Hector L., Vince L." in line
    for other in ("Bartender", "Manager", "Server", "Jade F.", "Dana S.", "Angela M."):
        assert other not in line, other


def _answer_for(kw):
    """A schema answer for every date THIS REQUEST asks, twelve of the
    roster at lunch (the auditor's render harness)."""
    content = kw["messages"][0]["content"]
    dates = sp.request_dates(content)
    sch = (kw.get("output_config") or {}).get("format", {}).get("schema") or {}
    try:
        shift = sch["properties"]["days"]["items"]["properties"]["shifts"]["items"]["properties"]
        names, roles = shift["employee"]["enum"][:12], shift["role"]["enum"]
    except (KeyError, TypeError):
        names, roles = ["Ana"], ["Server"]
    return {"days": [{"date": d, "shifts": [{"employee": n, "role": roles[0], "start": "11:00am", "end": "4:00pm"}
                                            for n in names]} for d in dates],
            "summary": ["kept Friday dinner tight"]}


@pytest.fixture
def calls(monkeypatch):
    """Every schedule call's kwargs; the model is never called."""
    import types
    import labor
    import weather
    seen = []
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])

    def fake(client, **kw):
        seen.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(_answer_for(kw)))],
                                     stop_reason="end_turn", usage=None, stop_details=None)
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    return seen


def _blocks(kw):
    return [b["text"] for b in kw["messages"][0]["content"]]


# ── PROMPT-13: "this week" in a redo counts the kept days ─────────────────

def test_a_redo_counts_the_kept_days_in_the_managers_week(ejs, db, calls):
    import schedule_versions as sv
    with se.frozen_inputs():
        first = se._build_schedule_result(ejs)
        rows = sv.rows_from_csv(first["schedule_csv"])
        redo = ["2026-10-09", "2026-10-10"]
        keep = [r for r in rows if r["date"] not in redo]
        second = se._build_schedule_result(ejs, week_start="2026-10-05", dates=redo, prior_rows=keep)
    week = _blocks(calls[-1])[1]
    line = _line(week, "Manager hours this week")
    assert "the kept days included" in line
    planned = [r for r in (second.get("manager_plan") or {}).get("rows") or [] if r["date"] in redo]
    for name in ("Dana S.", "Luis G.", "Parker S."):
        want = sum(sr.row_hours(r) for r in keep + planned if r.get("employee") == name)
        if want:
            assert f"{name} {round(want, 2):g}h" in line, (name, want, line)


# ── PROMPT-3: a call's cost is a fixed part plus a part per row ───────────

THINK = 30000          # the auditor's assumption: adaptive thinking, roughly fixed per call
WEEK_ROWS = 224        # Simple EJ's week


def _record(db, rid, rows, kind, seconds=None, think=THINK):
    so.record_call(rid, {"model": ai_utils.model_for("schedule")}, rows=rows, model=ai_utils.model_for("schedule"),
                   contract="schema", stop_reason="end_turn", usage={"output_tokens": think + 30 * rows},
                   answer_chars=84 * rows, seconds=seconds, call_kind=kind, db_path=db)


def test_a_gate_rewrite_no_longer_splits_every_later_week(db):
    # The auditor's ratchet (work-PROMPT/test_ratchet.py): each week written,
    # then the gate's 64-row rewrite. The median of tokens a row read the
    # gate's thinking as a dear row and every week after the first was split
    # in two (~30k more output tokens a week, a second call on the clock).
    rid = _restaurant(db)
    dates = WEEK
    people = [(f"P{i}", "Server") for i in range(55)]
    plans = []
    for _week in range(6):
        per_call = se.rows_per_call(rid)
        plan = se._plan_tasks(WEEK_ROWS, dates, people, (), per_call)
        plans.append(len(plan))
        for _ in plan:
            _record(db, rid, int(WEEK_ROWS / len(plan)), "week" if len(plan) == 1 else "slice")
        _record(db, rid, 64, "gate")
    assert plans == [1] * 6
    costs = so.call_costs(rid, model=ai_utils.model_for("schedule"), db_path=db)
    assert costs["source"] == "fit"
    assert costs["fixed"] == pytest.approx(THINK, rel=0.01) and costs["per_row"] == pytest.approx(30, rel=0.01)
    # Measured per call kind (week / slice / redo / gate).
    assert costs["by_kind"]["week"]["tokens_per_row"] == pytest.approx((THINK + 30 * WEEK_ROWS) / WEEK_ROWS, abs=0.2)
    assert costs["by_kind"]["gate"]["tokens_per_row"] == pytest.approx((THINK + 30 * 64) / 64, abs=0.2)
    assert costs["by_kind"]["week"]["calls"] == 6 and costs["by_kind"]["gate"]["calls"] == 6


def test_before_the_calls_spread_only_week_and_slice_calls_set_the_row_cost(db):
    rid = _restaurant(db)
    _record(db, rid, 64, "gate")
    _record(db, rid, 32, "redo")
    costs = so.call_costs(rid, model=ai_utils.model_for("schedule"), db_path=db)
    assert costs["source"] == "estimate"               # small calls alone say nothing about a week's call
    assert se.rows_per_call(rid) == se.CHUNK_ROWS_PER_CALL
    _record(db, rid, WEEK_ROWS, "week")
    costs = so.call_costs(rid, model=ai_utils.model_for("schedule"), db_path=db)
    assert costs["source"] in ("fit", "comparable")
    assert se.rows_per_call(rid) == se.CHUNK_ROWS_PER_CALL


def test_each_call_records_what_it_wrote(ejs, db, calls):
    import schedule_versions as sv
    with se.frozen_inputs():
        first = se._build_schedule_result(ejs)
        rows = sv.rows_from_csv(first["schedule_csv"])
        keep = [r for r in rows if r["date"] != "2026-10-09"]
        se._build_schedule_result(ejs, week_start="2026-10-05", dates=["2026-10-09"], prior_rows=keep)
        se._build_schedule_result(ejs, week_start="2026-10-05", dates=["2026-10-09"], prior_rows=keep,
                                  focus=["Friday 2026-10-09 dinner: short"])
    conn = models.get_conn(db)
    kinds = [r[0] for r in conn.execute("SELECT call_kind FROM schedule_model_calls WHERE restaurant_id=? "
                                        "ORDER BY id", (ejs,)).fetchall()]
    conn.close()
    assert kinds == ["week", "redo", "gate"]


# ── PROMPT-4: a call's time budget carries its thinking ───────────────────

def test_a_one_call_week_is_given_the_time_its_thinking_takes():
    # At the code's only recorded rate (the old 9,600 tokens in 360 s) a call
    # that uses its thinking reserve and writes a week needs ~29 minutes; it
    # was given 360 seconds, inside a 15-minute job.
    needed = (se.THINKING_TOKENS_RESERVED + 30 * WEEK_ROWS) / (se.ROW_TOKENS_PER_CALL / 360.0)
    assert se.call_seconds(WEEK_ROWS) >= needed
    assert se.SCHEDULE_CALL_SECONDS >= se.call_seconds(WEEK_ROWS)
    clock = se.GenerationClock()
    clock.plan(1, seconds=se.call_seconds(WEEK_ROWS))
    assert clock.model_deadline - clock.started >= needed
    assert clock.deadline - clock.started <= se.SCHEDULE_JOB_MAX_SECONDS


def test_measured_seconds_set_a_calls_time(db):
    rid = _restaurant(db)
    # 80 output tokens a second, measured: seconds = (30k + 30/row) / 80.
    for rows in (224, 64, 112):
        _record(db, rid, rows, "slice", seconds=(THINK + 30 * rows) / 80.0)
    costs = se.call_cost_model(rid)
    assert costs["seconds_source"] == "fit"
    want = (THINK + 30 * WEEK_ROWS) / 80.0
    assert se.call_seconds(WEEK_ROWS, costs, headroom=False) == pytest.approx(want, rel=0.01)
    assert se.call_seconds(WEEK_ROWS, costs) == pytest.approx(want * se.CALL_SECONDS_HEADROOM, rel=0.01)
    by = costs["by_kind"]["slice"]
    assert by["tokens_per_second"] == pytest.approx(80.0, rel=0.01)


# ── PROMPT-8: a breakpoint only where it is read before it expires ────────

def test_cache_breakpoints_follow_what_the_calls_share_and_how_long_they_run():
    read = ai_utils._cache_read_multiplier("claude-opus-5-5")
    assert read == 0.05
    assert sp.cache_ttls(1, 1, 1900, read) == (None, None)          # one call: a write nobody reads
    assert sp.cache_ttls(3, 3, 100, read) == ("5m", "5m")           # quick calls: five minutes is cheapest
    assert sp.cache_ttls(3, 3, 1900, read) == ("1h", "1h")          # calls past five minutes: only an hour holds
    assert sp.cache_ttls(2, 2, 1900, read) == (None, None)          # 2 + 0.05 is dearer than two plain reads
    assert sp.cache_ttls(4, 2, 1900, read) == ("1h", None)          # departments: their week blocks differ
    # A one-hour entry never follows a five-minute one.
    assert sp.cache_ttls(2, 3, 200, read) == ("5m", "5m")
    t0, t1 = sp.cache_ttls(3, 4, 250, read)
    assert not (t0 == "5m" and t1 == "1h")
    content = sp.request_content("a", "b", "c", ttls=("1h", None))
    assert content[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"} and "cache_control" not in content[1]
    assert sp.request_content("a", "b", "c")[1]["cache_control"] == {"type": "ephemeral"}


def test_a_one_call_week_sets_no_breakpoint_nobody_would_read(ejs, db, calls):
    se._build_schedule_result(ejs)
    assert len(calls) == 1
    assert not any("cache_control" in b for b in calls[0]["messages"][0]["content"])


# ── PROMPT-12: strength guidance keeps the headcount ──────────────────────

def test_strength_guidance_never_trades_a_body_for_a_stronger_mix(ejs, db, calls):
    se._build_schedule_result(ejs)
    week = _blocks(calls[0])[1]
    assert "prefer fewer stronger people" not in week
    assert "at the same headcount prefer the stronger mix" in week
    assert "never drop a person a shift needs" in week


# ── PROMPT-1: the owner's written floors and arrival times win, and are checked

EJS_ROLES = {"Bartender", "Server", "Line Cook", "Prep Cook", "Host", "Busser", "Manager", "Dishwasher"}


def test_the_hours_notes_are_read_into_floors_hours_ceilings_and_stays():
    p = sr.parse_hours_rules(demo_seed._EJS_HOURS, EJS_ROLES)
    floors = {(f["role"], f["min"], f["daypart"], f["days"]) for f in p["floors"]}
    assert ("Host", 1, "morning", None) in floors                      # "one on at open"
    assert ("Host", 1, None, None) in floors                           # "whenever the dining room is open"
    assert ("Server", 2, None, None) in floors                         # "any open hour"
    assert ("Server", 2, "night", None) in floors                      # "Keep 2 servers through close"
    assert ("Busser", 1, "night", None) in floors
    assert ("Busser", 2, "night", ("Friday", "Saturday")) in floors    # "2 Fri and Sat" — the same half
    assert ("Dishwasher", 1, "morning", None) in floors                # "one from 10:00am"
    assert ("Dishwasher", 1, "night", None) in floors                  # "one from 3:00pm to close"
    assert ("Line Cook", 2, "night", None) in floors
    wins = {(w["role"], w.get("earliest"), w.get("latest"), w.get("before_close")) for w in p["windows"]}
    assert ("Bartender", 15 * 60, None, None) in wins                  # "evening only, no earlier than 3:00pm"
    assert ("Line Cook", 14 * 60, None, None) in wins                  # "first arrives 2:00pm"
    assert ("Server", 10 * 60 + 30, None, None) in wins                # "first arrives 10:30am"
    assert ("Busser", None, None, 30) in wins                          # "cut 30 minutes before close"
    assert [(c_["role"], c_["max"]) for c_ in p["caps"]] == [("Server", 5)]
    assert max(st["minutes"] for st in p["stays"] if st["role"] == "Bartender") == 30
    assert [(x["role"], x["start"]) for x in p["starts"]] == [("Prep Cook", 8 * 60)]
    assert {m["role"] for m in p["managers"]} == {"Manager"}
    # What it could not read is named, never guessed at; headings and the
    # restaurant's own hours are not rules to name.
    assert "Line cooks: others stagger from 3:00pm" in p["unchecked"]
    assert any(u.startswith("Servers 5-7h") for u in p["unchecked"])
    assert any("cut the rest about an hour" in u for u in p["unchecked"])
    assert not any("RESTAURANT HOURS" in u or u.endswith(":") for u in p["unchecked"])


def test_a_rule_the_owner_parser_cannot_read_is_never_guessed_at(monkeypatch):
    # RULES-8 makes the owner-rule parser answer {"unclear": …} for a rule
    # it cannot read as one floor; the hours reader takes that as unread.
    real = sr.parse_owner_rule

    def unclear(text, roles, families=None):
        got = real(text, roles, families)
        if got and text.startswith("at least 2 dishwasher"):
            return {"role": got["role"], "unclear": "it gives more than one number", "text": text}
        return got
    monkeypatch.setattr(sr, "parse_owner_rule", unclear)
    p = sr.parse_hours_rules("- Dishwashers: two at night Thu-Sat.", EJS_ROLES)
    assert p["floors"] == [] and p["unchecked"] == ["Dishwashers: two at night Thu-Sat"]


def test_the_constraints_hold_them(ejs, db):
    c = sr.build_constraints(ejs, WEEK, DAYS, db_path=db)
    # The hours a role works.
    assert c.role_windows["bartender"]["Wednesday"]["earliest"] == 15 * 60
    assert c.role_windows["busser"]["Monday"]["latest"] == 22 * 60 - 30         # a 10:00pm close
    assert c.role_windows["busser"]["Saturday"]["latest"] == 24 * 60 - 30       # a midnight close
    # The floors, where the role works them, naming the line.
    assert sr.floor_for(c.role_floors, "Host", "Tuesday", "morning") == 1
    assert sr.floor_for(c.role_floors, "Host", "Sunday", "night") == 1
    assert sr.floor_for(c.role_floors, "Server", "Monday", "morning") == 2
    assert sr.floor_for(c.role_floors, "Bartender", "Wednesday", "morning") == 0   # evening only: no lunch floor
    assert sr.floor_for(c.role_floors, "Bartender", "Wednesday", "night") >= 1
    assert sr.floor_for(c.role_floors, "Dishwasher", "Monday", "morning") == 1
    assert c.rule_floor_sources[("host", "Tuesday", "morning")].startswith("Hosts:")
    # The ceiling, the stays, the readings and the unread lines.
    assert c.role_caps["server"][0]["max"] == 5
    assert c.close_mins["bartender"] == 30 and c.close_mins["line cook"] == 0
    assert any(r["kind"] == "role_window" for r in c.hours_rules)
    assert "Line cooks: others stagger from 3:00pm" in c.hours_rules_unchecked


def test_the_requirements_table_follows_the_owners_rules_not_the_punches(ejs, db, calls):
    se._build_schedule_result(ejs)
    request = _blocks(calls[0])[2]
    rows = {ln.split("|")[0].strip(): ln for ln in request.splitlines() if re.match(r"^  \w{3} \d{4}-", ln)}

    def shift(day, part):
        """The people the shift needs (its second column; the leader rule is PROMPT-6's)."""
        return next(v for k, v in rows.items() if k.startswith(day) and k.endswith(part)).split(" | ")[1]
    for day in ("Wed", "Thu", "Fri"):
        assert "Bartender" not in shift(day, "morning"), day           # evening only, no earlier than 3:00pm
    for day in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
        assert "Line Cook" not in shift(day, "morning"), day           # the first line cook arrives at 2:00pm
        assert re.search(r"Host \d \(floor 1\)", shift(day, "morning")), day   # a host whenever it is open
        assert "Dishwasher" in shift(day, "morning"), day              # one from 10:00am
    assert re.search(r"Host \d \(floor \d\)", shift("Sun", "night"))
    assert "held to your maximum of 5 on at once" in shift("Sat", "night")
    # The table no longer claims to hold what the code did not read.
    assert "is not in these numbers: where it says otherwise, it wins over this table" in request


def test_the_prompt_says_how_each_line_is_held_and_which_it_could_not_read(ejs, db, calls):
    se._build_schedule_result(ejs)
    week = _blocks(calls[0])[1]
    hours = week[week.index("RESTAURANT HOURS & SHIFT RULES"):week.index("ADDITIONAL SCHEDULING NOTES")]
    assert "How the code checks them: [HARD] each count is a staffing floor" in hours
    assert "Not read by the code" in hours and "\"Line cooks: others stagger from 3:00pm\"" in hours
    assert "[HARD] Hours by role" in week and "Bartender: from 3:00pm" in week
    assert "[SOFT] Most of a role on at once: Server at most 5" in week
    assert "Host: at least 1 lunch/day" in week
    assert "Context, from punches" in week and "the rules win" in week


def _r(date, emp, role, start, end):
    return {"date": date, "day": DAYS[WEEK.index(date)], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(sr.row_hours({"shift_start": start, "shift_end": end}))}


def test_the_sweep_holds_the_draft_to_them(ejs, db):
    c = sr.build_constraints(ejs, WEEK, DAYS, db_path=db)
    wed = WEEK[2]
    lunch_bar = _r(wed, "Rita M.", "Bartender", "11:00am", "5:00pm")
    viols = sr.violations([lunch_bar], c)
    hit = [v for v in viols if v["kind"] == "role_window"]
    assert hit and hit[0]["hard"] and "no earlier than 3:00pm" in hit[0]["detail"]
    assert "evening only" in hit[0]["detail"] or "no earlier than 3:00pm" in hit[0]["detail"]
    assert sr.BREACH_TIER["role_window"] == sr.TIER_COVERAGE
    # No pass adds one.
    ok, why = c.can_add(lunch_bar, [])
    assert not ok and "no earlier than 3:00pm" in why
    # A late busser and a host short at Tuesday lunch.
    tue = WEEK[1]
    rows = [_r(tue, "Kase N.", "Busser", "4:30pm", "10:00pm"), _r(tue, "Angela M.", "Server", "10:30am", "4:00pm")]
    kinds = [(v["kind"], v.get("role")) for v in sr.violations(rows, c)]
    assert ("role_window", "Busser") in kinds                            # past 9:30pm on a 10:00pm close
    floors = [v for v in sr.violations(rows, c) if v["kind"] == "coverage_floor" and "Host" in v["detail"]]
    assert floors and any("Hosts:" in v["detail"] for v in floors)


def test_the_retime_pass_moves_a_row_into_the_roles_hours(ejs, db):
    c = sr.build_constraints(ejs, WEEK, DAYS, db_path=db)
    row = _r(WEEK[2], "Marcus R.", "Bartender", "1:00pm", "9:00pm")
    out = sr.apply_role_times([row], c)
    assert out["rows"][0]["shift_start"] == "3:00pm" and out["rows"][0]["shift_end"] == "9:00pm"
    assert out["retimed"] and "Bartenders" in out["retimed"][0]["reason"]
    assert not [v for v in sr.violations(out["rows"], c) if v["kind"] == "role_window"]


def test_too_many_of_a_role_on_at_once_is_flagged_and_the_sweeps_agree(ejs, db):
    c = sr.build_constraints(ejs, WEEK, DAYS, db_path=db)
    sat = WEEK[5]
    servers = ["Carla D.", "Mia L.", "Grant W.", "Zoe H.", "Reuben O.", "Trey B."]
    rows = [_r(sat, n, "Server", "5:00pm", "10:00pm") for n in servers]
    rows.append(_r(WEEK[4], "Carla D.", "Server", "5:00pm", "10:00pm"))
    viols = sr.violations(rows, c)
    cap = [v for v in viols if v["kind"] == "over_role_max"]
    assert len(cap) == 1 and not cap[0]["hard"] and "6 Server on at once" in cap[0]["detail"]
    assert sr.IncrementalSweep(c).violations(rows) == viols
    assert sr.IncrementalSweep(c).violations(rows[:5] + rows[6:]) == sr.violations(rows[:5] + rows[6:], c)
    # The owner reads it in what the week doesn't meet.
    items = so.unmet_items(rows, violations=viols)
    assert [i["what"] for i in items if i["kind"] == "role_max"] == ["Your Server maximum"]


def test_the_review_names_every_line_it_could_not_read(ejs, db):
    c = sr.build_constraints(ejs, WEEK, DAYS, db_path=db)
    items = so.unmet_items([], constraints=c, owner_rules_unchecked=list(c.owner_rules_unchecked)
                           + list(c.hours_rules_unchecked))
    named = [i["what"] for i in items if i["kind"] == "unchecked_rule"]
    assert any("others stagger from 3:00pm" in w for w in named)


def test_the_floors_reach_every_cut_surface(ejs, db):
    r = models.get_restaurant(ejs, db)
    floors = sr.effective_role_floors(r, day="2026-10-06", db_path=db)
    assert sr.floor_for(floors, "Host", "Tuesday", "morning") == 1
    assert sr.floor_for(floors, "Bartender", "Tuesday", "morning") == 0


def test_the_rules_screen_says_how_each_line_is_checked(ejs, db):
    import strategy_routes
    payload = strategy_routes._setup_payload(ejs, principal=True)
    reads = {r["text"]: r["reads_as"] for r in payload["hours_rules"]}
    assert reads["Bartenders: no earlier than 3:00pm"] == "every Bartender shift starts no earlier than 3:00pm"
    # Said as held: an evening-only bar's "whenever the bar is open" is a dinner floor.
    assert "dinner/night" in reads["Bartenders: minimum 1 whenever the bar is open"]
    assert "lunch/day" not in reads["Bartenders: minimum 1 whenever the bar is open"]
    assert "Line cooks: others stagger from 3:00pm" in payload["hours_rules_unchecked"]
