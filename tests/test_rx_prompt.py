"""Schedule re-audit 10/4/26, lens PROMPT: the generation prompt and the
model call (PROMPT-1, 3, 4, 5, 7-13). Each test reproduces the auditor's
finding against the code as it was and holds the fix."""
import json
import re

import pytest

import ai_utils
import models
import schedule_rules as sr
from test_schedule_b2_calls import WEEK, DAYS, db, _restaurant  # noqa: F401  (db is a fixture)


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
