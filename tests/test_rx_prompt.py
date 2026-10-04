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
