"""The people memory reaches the model calls that decide who works (memory
audit 9/29/26: memory_context provider "people", wired into the schedule
prompt and the labor read; the labor target read through the one resolver,
goal first).

Before, the schedule prompt and the labor read each picked their own memory
by hand and the labor read picked none: Maria's three missed Saturdays, the
server trained on bar, who is new and who has had no shift in three weeks,
and which guests named whom never reached a read that suggested trimming
Saturday.
"""
import types
from datetime import date, timedelta

import pytest

import attendance
import labor
import memory_context
import models
import people
import shift_facts
import staff_settings
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    monkeypatch.setattr(models, "other_tenant_names", lambda rid, db_path=None: set())
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    labor._NOTE_CACHE.clear()
    yield
    labor._NOTE_CACHE.clear()


TODAY = date.today()


def _rid():
    return create_restaurant(Restaurant(name="Memory Co", owner_email="m@x.test", module_labor=1, module_reviews=1))


def _shifts(rid, name, role, days, source="upload"):
    """Shifts on file. From a POS (RPOWER) the schedule is the punch copied,
    so nothing in them watches attendance; an upload's own schedule does."""
    pos = source != "upload"
    rows = [{"date": d.isoformat(), "day": d.strftime("%A"), "employee": name, "role": role, "shift_start": "16:00",
             "shift_end": "22:00", "scheduled_hours": "6", "actual_hours": "6", "sales": "1000",
             **({"schedule_known": "0"} if pos else {})} for d in days]
    shift_facts.ingest(rid, rows, source)


def _req(rid, surface):
    return memory_context.MemoryRequest(restaurant_id=rid, surface=surface, now=None)


def _saturdays(n):
    last = TODAY - timedelta(days=(TODAY.weekday() - 5) % 7 or 7)
    return [last - timedelta(weeks=w) for w in range(n)]


def _staff(rid, source="rpower"):
    """Ten weeks of history: Maria and Ana throughout, Tom until a month
    ago, Cy new two weeks ago."""
    ten_weeks = [TODAY - timedelta(days=d) for d in range(1, 71, 3)]
    _shifts(rid, "Maria G.", "Bartender", ten_weeks, source)
    _shifts(rid, "Ana B.", "Server", ten_weeks, source)
    _shifts(rid, "Tom B.", "Server", [d for d in ten_weeks if (TODAY - d).days > 30], source)
    _shifts(rid, "Cy D.", "Server", [TODAY - timedelta(days=d) for d in (13, 9, 4, 1)], source)


# ── the provider ────────────────────────────────────────────────────────────

def test_the_labor_read_hears_attendance_roles_and_who_is_new_each_dated_and_fenced():
    rid = _rid()
    _staff(rid)
    for d in _saturdays(3):
        attendance.record(rid, "Maria G.", d.isoformat(), "no_show", "coverage_check", shift_start="4:00pm")
    attendance.record(rid, "Ana B.", _saturdays(1)[0].isoformat(), "on_time", "schedule_vs_punch_join")
    people.add_role(rid, "Ana B.", "Bartender", since=(TODAY - timedelta(days=20)).isoformat())
    lines = people.memory_lines(_req(rid, "labor_read"))
    texts = [l["text"] for l in lines]
    maria = next(l for l in lines if l["text"].startswith("Maria G.: missed 3"))
    assert "3 of them Saturdays" in maria["text"] and maria["subject"] == "labor:day:saturday"
    assert any("Ana B. is trained for Bartender" in t and "as well as Server" in t for t in texts)
    new = next(t for t in texts if t.startswith("New on the staff"))
    assert "Cy D." in new and "Maria G." not in new
    gone = next(t for t in texts if t.startswith("No shifts in the three weeks"))
    assert "Tom B." in gone and "Whether they left isn't known" in gone
    # Every line naming a person is fenced, and no line's text carries an ISO date.
    assert all(not l["trusted"] for l in lines)
    assert not any(str(TODAY.year) + "-" in t for t in texts)
    block = memory_context.memory_context(rid, "labor_read")
    assert "Maria G.: missed 3" in block.text and "<<" in block.text


def test_the_schedule_hears_only_what_its_own_blocks_do_not_say():
    rid = _rid()
    _staff(rid)
    for d in _saturdays(3):
        attendance.record(rid, "Maria G.", d.isoformat(), "no_show", "coverage_check")
    people.add_role(rid, "Ana B.", "Bartender")
    texts = [l["text"] for l in people.memory_lines(_req(rid, "schedule"))]
    assert any("Ana B. is trained for Bartender" in t for t in texts)
    # Attendance is the schedule prompt's RELIABILITY block, standing patterns
    # its learned block, tenure its own: never paid for twice.
    assert not any("missed" in t or t.startswith("New on the staff") for t in texts)


def test_unwatched_attendance_is_said_as_unknown_and_trusted():
    rid = _rid()
    _staff(rid)                                   # RPOWER: the schedule is the punch copied
    lines = people.memory_lines(_req(rid, "labor_read"))
    unk = [l for l in lines if l["text"].startswith("Attendance is not watched here yet")]
    assert unk and unk[0]["trusted"] is True and "Maria" not in unk[0]["text"]


def test_an_uploads_own_schedule_is_watched_and_its_no_shows_are_said():
    rid = _rid()
    _staff(rid, source="upload")
    lines = people.memory_lines(_req(rid, "labor_read"))
    assert not any(l["text"].startswith("Attendance is not watched") for l in lines)   # watched, nobody missed
    sats = _saturdays(2)
    shift_facts.ingest(rid, [{"date": d.isoformat(), "day": "Saturday", "employee": "Ana B.", "role": "Server",
                              "shift_start": "10:00", "shift_end": "15:00", "scheduled_hours": "5",
                              "actual_hours": "0", "sales": "1000"} for d in sats], "upload")
    ana = next(l for l in people.memory_lines(_req(rid, "labor_read")) if l["text"].startswith("Ana B.: missed 2"))
    assert ana["subject"] == "labor:day:saturday"


def test_a_short_history_says_nothing_about_who_is_new():
    rid = _rid()
    _shifts(rid, "Ana B.", "Server", [TODAY - timedelta(days=d) for d in (20, 10, 2)])
    texts = [l["text"] for l in people.memory_lines(_req(rid, "labor_read"))]
    assert not any(t.startswith(("New on the staff", "No shifts in")) for t in texts)


def test_a_stopped_sync_is_not_everyone_leaving():
    rid = _rid()
    old = [TODAY - timedelta(days=d) for d in range(60, 130, 3)]         # the last shift is two months old
    _shifts(rid, "Maria G.", "Bartender", old)
    _shifts(rid, "Ana B.", "Server", old)
    texts = [l["text"] for l in people.memory_lines(_req(rid, "labor_read"))]
    assert not any(t.startswith("No shifts in") for t in texts)


def test_a_standing_preference_and_a_cover_taker_reach_the_labor_read():
    rid = _rid()
    _staff(rid)
    conn = models.get_conn()
    conn.execute("INSERT INTO schedule_standing_patterns (restaurant_id, pattern_key, kind, employee, role, day, "
                 "daypart, text, editors, first_learned, last_confirmed, times_applied, status) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, "moved_off|bob|tuesday|night", "moved_off", "Bob", "Server", "Tuesday", "night",
                  "The manager has taken Bob off Tuesday dinner — 2 weeks", "{}", "2026-07-06", "2026-09-22", 8,
                  "active"))
    for i in range(3):
        conn.execute("INSERT INTO person_signals (restaurant_id, employee_name, employee_key, kind, signal_date, ref, "
                     "status) VALUES (?,?,?,?,?,?,?)", (rid, "Ana B.", staff_settings.name_key("Ana B."),
                                                       "cover_accepted", (TODAY - timedelta(days=10 + i)).isoformat(),
                                                       f"issue:{i}", "confirmed"))
    conn.commit()
    conn.close()
    lines = people.memory_lines(_req(rid, "labor_read"))
    pat = next(l for l in lines if l["text"].startswith("Standing preference"))
    assert "learned 7/6/26, kept 8 weeks" in pat["text"] and "taken Bob off Tuesday dinner" in pat["text"]
    assert pat["subject"] == "labor:day:tuesday" and pat["date"] == "2026-09-22"
    assert any(l["text"].startswith("Ana B. covered 3 shifts for teammates") for l in lines)


def test_only_mentions_the_owner_confirmed_reach_the_review_diagnosis():
    rid = _rid()
    _staff(rid)
    day = (TODAY - timedelta(days=5)).isoformat()
    people.record_signal(rid, "Maria G.", "review_mention", day, ref="review:1", polarity=1, status="proposed")
    assert not [l for l in people.memory_lines(_req(rid, "review_diagnosis")) if "Guests named" in l["text"]]
    sid = people.mentions(rid)[0]["id"]
    people.answer_mention(rid, sid, True)
    got = [l for l in people.memory_lines(_req(rid, "review_diagnosis")) if "Guests named" in l["text"]]
    assert got and "Maria G. in 1 review" in got[0]["text"] and "(1 positive)" in got[0]["text"]
    assert got[0]["module"] == "reviews"


# ── the labor read ──────────────────────────────────────────────────────────

def _analysis():
    return {"is_live": True, "total_sales": 10000.0, "total_labor_cost": 3400.0, "overall_labor_pct": 34.0,
            "labor_target": 30, "period_days": 14, "potential_savings": 400.0,
            "potential_savings_weekly": 200.0, "potential_savings_monthly": 866.67,
            "dow_summary": {"Saturday": 38.0, "Friday": 29.5},
            "overstaffed_days": [{"date": "9/26/26", "day": "Saturday", "labor_pct": 38.0, "labor_cost": 760.0,
                                  "sales": 2000.0, "over_target_dollars": 160.0}],
            "understaffed_days": [], "overtime_risk": [], "role_summary": {},
            "date_range": {"start": "2026-09-14", "end": "2026-09-27", "days": 14}}


def _stub(monkeypatch):
    seen = {"prompts": []}

    def fake(*a, **kw):
        seen["prompts"].append(kw["messages"][0]["content"])
        return types.SimpleNamespace(content=[types.SimpleNamespace(
            type="text", text="Sam, labor ran 34% against a 30% target.\n\nRecommendations:\n1. Trim Friday by one.")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)
    return seen


def test_the_labor_read_prompt_carries_the_memory_as_the_team_reads_it(monkeypatch):
    rid = _rid()
    _staff(rid)
    for d in _saturdays(3):
        attendance.record(rid, "Maria G.", d.isoformat(), "no_show", "coverage_check")
    asked = {}
    real = memory_context.memory_context

    def spy(restaurant_id, surface, viewer=None, subjects=(), **kw):
        asked.update(surface=surface, viewer=viewer, subjects=tuple(subjects))
        return real(restaurant_id, surface, viewer=viewer, subjects=subjects, **kw)
    monkeypatch.setattr(memory_context, "memory_context", spy)
    seen = _stub(monkeypatch)
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    prompt = seen["prompts"][0]
    assert "WHAT CAVNAR AI REMEMBERS FOR THIS RESTAURANT" in prompt and "Maria G.: missed 3" in prompt
    assert prompt.index("WHAT CAVNAR AI REMEMBERS") < prompt.index("EVIDENCE RULES")
    assert asked["surface"] == "labor_read" and "labor:day:saturday" in asked["subjects"]
    # The stored read serves every login with the labor view: memory is
    # assembled as a manager reads it — no owner-only line, no food cost.
    assert asked["viewer"] is labor.TEAM_VIEWER
    import permissions
    assert permissions.answer_authority(labor.TEAM_VIEWER) == "delegate"
    assert permissions.has_permission(labor.TEAM_VIEWER, permissions.LABOR_VIEW)
    assert not permissions.has_permission(labor.TEAM_VIEWER, permissions.FOOD_COST_VIEW)


def test_the_fenced_memory_words_join_the_untrusted_text_the_read_is_checked_under(monkeypatch):
    rid = _rid()
    _staff(rid)
    for d in _saturdays(3):
        attendance.record(rid, "Maria G.", d.isoformat(), "no_show", "coverage_check")
    block, words = labor.labor_memory_block(rid, _analysis())
    assert block.startswith("\n\nWHAT CAVNAR AI REMEMBERS") and any(w.startswith("Maria G.: missed 3") for w in words)
    ctx = labor.labor_read_context(_analysis(), "prompt" + block, restaurant_id=rid, memory_untrusted=words)
    assert any(u.startswith("Maria G.: missed 3") for u in ctx.untrusted)


def test_the_labor_read_names_whose_target_it_judges_against(monkeypatch):
    rid = _rid()
    assert labor.labor_target_whose(rid) == " (Cavnar AI's starting target — not one the owner set)"
    models.update_restaurant(rid, {"labor_target_pct": 27.0, "labor_target_source": "set"})
    assert labor.labor_target_whose(rid) == " (your target)"
    # The owner's goal comes through the one resolver (owner_memory.target_for,
    # goal first inside thresholds.target_for).
    import thresholds
    monkeypatch.setattr(thresholds, "target_for", lambda r, kind, **kw: {
        "pct": 26.0, "source": "goal", "label": "your goal of 26% by 12/1/26"})
    assert labor.labor_target_whose(rid) == " (your goal of 26% by 12/1/26)"
    seen = _stub(monkeypatch)
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert "- This restaurant's labor target: 30% (your goal of 26% by 12/1/26)" in seen["prompts"][0]


def test_the_schedule_prompt_reads_the_memory_as_the_team_does():
    import inspect
    import schedule_engine
    src = inspect.getsource(schedule_engine._build_schedule_result)
    assert 'memory_context(restaurant_id, "schedule", viewer=_team_viewer' in src
