"""The Schedule Studio's Crew stage (crew_matrix, owner 10/10/26): each role's
usual crew per weekday and shift beside the draft; where the draft runs
leaner the owner answers once.

Since the schedule audit of 10/10/26 every number on the grid is people ON
AT ONCE through the core of the shift — the unit a floor is held in — the
usual from the history and the draft from its rows counted the same way
(LOGIC-2/5, BUGSGEN-1/2/17). Keep N writes that concurrent number (never
the count of different people) with a review date; Leaner is fine binds the
requirement; Undo restores exactly what was there, and only while the
minimum is still the one the hold set; answers come back for the week they
were given on; answers and floors are one compare-and-swap."""
import json
from datetime import date, timedelta

import pytest

import crew_matrix as cm
import labor
import models
import schedule_rules as sr
from models import Restaurant, create_restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


WEEK0 = date(2026, 10, 12)          # the draft week, a Monday


def _dates(weekday_index, n=8):
    """The `n` dates of a weekday (0 = Monday) before the draft week."""
    first = WEEK0 + timedelta(days=weekday_index) - timedelta(weeks=n)
    return [(first + timedelta(weeks=i)).isoformat() for i in range(n)]


def _hist(weekday_index, spec, n=8):
    return [{"date": d, "employee": e, "role": role, "shift_start": st, "shift_end": en}
            for d in _dates(weekday_index, n) for e, role, st, en in spec]


# The LOGIC-2 repro: a staggered Friday dinner of six servers. Never more than
# six on at once (6:30pm), four on at 5:30pm: six different people, four on
# through the whole core window.
FRIDAY_SERVERS = [("Ann", "Server", "11:00am", "7:00pm"), ("Bo", "Server", "4:00pm", "11:00pm"),
                  ("Cy", "Server", "4:30pm", "11:00pm"), ("Di", "Server", "5:00pm", "11:00pm"),
                  ("Ed", "Server", "6:00pm", "11:00pm"), ("Flo", "Server", "6:30pm", "11:00pm")]
# A 2pm-10pm server (BUGSGEN-1): a lunch start, a dinner shift.
TUESDAY = [("Gus", "Server", "2:00pm", "10:00pm"), ("Hal", "Server", "5:00pm", "10:00pm"),
           ("Ivy", "Kitchen", "9:00am", "3:00pm"), ("Jo", "Kitchen", "9:00am", "3:00pm"),
           ("Kim", "Kitchen", "4:00pm", "10:00pm")]


def _baseline():
    return labor.historical_patterns(_hist(4, FRIDAY_SERVERS) + _hist(1, TUESDAY))


def _week(rid, rows, start="2026-10-12"):
    head = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
    body = "".join(f"{d},{day},{e},{role},{st},{en},6,\n" for d, day, e, role, st, en in rows)
    end = (date.fromisoformat(start) + timedelta(days=6)).isoformat()
    return models.save_schedule_history(rid, start, end, 40, 100, 30, head + body, [])


FRI, TUE = "2026-10-16", "2026-10-13"


def _setup(monkeypatch, friday=None, tuesday=None, baseline=None):
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"))
    base = baseline or _baseline()
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: base)
    rows = [(FRI, "Friday", e, role, st, en) for e, role, st, en in (FRIDAY_SERVERS if friday is None else friday)]
    rows += [(TUE, "Tuesday", e, role, st, en) for e, role, st, en in (TUESDAY if tuesday is None else tuesday)]
    hid = _week(rid, rows)
    return rid, hid


def _cell(out, role, day, part):
    r = next(x for x in out["roles"] if x["role"] == role)
    return next(c for c in r["cells"] if c["day"] == day and c["part"] == part)


# ── one unit: the usual and the draft both people on at once ──────────────

def test_the_usual_is_people_on_at_once_never_the_different_people_who_worked_it():
    base = _baseline()
    fri = base["concurrent"][("Friday", "night")]["Server"]
    assert (fri["held"], fri["peak"]) == (4, 6)            # 4 on at 5:30pm, 6 at 6:30pm
    assert base["typical_headcount"][("Friday", "night")]["Server"] == 6   # the different people, kept apart
    assert fri["slots"][17 * 60 + 30] == 4 and fri["slots"][18 * 60 + 30] == 6


def test_the_same_staggered_night_reads_as_its_usual_not_short(monkeypatch):
    rid, _hid = _setup(monkeypatch)
    out = cm.build(rid)
    assert out["unit"] == "concurrent"
    fri = _cell(out, "Server", "Friday", "night")
    assert (fri["usual"], fri["draft"], fri["status"]) == (4, 4, "match")
    assert (fri["usual_peak"], fri["draft_peak"]) == (6, 6)
    # A 2pm-10pm server is dinner crew on both sides (BUGSGEN-1): no "lunch
    # over, dinner under" for a shift that is the usual one.
    assert _cell(out, "Server", "Tuesday", "night")["status"] == "match"
    assert _cell(out, "Server", "Tuesday", "morning")["status"] == "match"
    assert out["open"] == 0


def test_a_draft_with_fewer_at_the_rush_says_so_without_asking(monkeypatch):
    four = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(4)]
    rid, _hid = _setup(monkeypatch, friday=four)
    fri = _cell(cm.build(rid), "Server", "Friday", "night")
    assert fri["status"] == "match" and fri["draft_peak"] == 4
    assert fri["peak_note"] == "2 fewer at the busiest half hour" and fri["answer"] is None


def test_the_draft_counts_people_not_rows_and_never_a_no_show():
    import schedule_rules as R
    rows = [{"date": FRI, "day": "Friday", "employee": "Bo", "role": "Server", "shift_start": "11:00am",
             "shift_end": "2:00pm"},
            {"date": FRI, "day": "Friday", "employee": "Bo", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "10:00pm"},                                    # a split shift: one person
            {"date": FRI, "day": "Friday", "employee": "Cy", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "10:00pm"},
            {"date": FRI, "day": "Friday", "employee": "Off", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "10:00pm"},                                    # on approved time off
            {"date": FRI, "day": "Friday", "employee": "Tia", "role": "Training", "shift_start": "5:00pm",
             "shift_end": "10:00pm"}]
    c = R.Constraints(restaurant_id=1, week_dates=[FRI], week_days=["Friday"])
    c.compliance = dict(R.DEFAULTS)
    c.blocked_dates = {"off": {FRI: R.LABELS["approved_time_off"]}}
    figs = cm.draft_figures(1, rows, constraints=c)
    assert figs[("Server", "Friday", "night")] == {"held": 2, "peak": 2, "people": 2}
    assert figs[("Server", "Friday", "morning")]["people"] == 1
    assert not any(k[0] == "Training" for k in figs)


# ── Keep N: a concurrent floor the usual night keeps ───────────────────────

def test_keep_writes_people_on_at_once_and_the_usual_night_breaks_no_floor(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    assert _cell(cm.build(rid, history_id=hid), "Server", "Friday", "night")["status"] == "under"
    out, status = cm.answer(rid, "Server", "Friday", "night", "hold", history_id=hid)
    assert status == 200
    floors = sr.role_floors(get_restaurant(rid))
    assert sr.floor_for(floors, "Server", "Friday", "night") == 4            # never the 6 different people
    cell = _cell(out, "Server", "Friday", "night")
    assert cell["answer"] == "hold" and cell["kept"] == 4 and cell["status"] == "below_rule" and cell["can_add"]
    assert cell["review"] and "-" not in cell["review"]                       # M/D/YY
    # The restaurant's own usual Friday (LOGIC-2's repro) holds the floor it kept.
    c = sr.Constraints(restaurant_id=rid, week_dates=[FRI], week_days=["Friday"])
    c.compliance = dict(sr.DEFAULTS)
    c.role_floors = floors
    usual_night = [{"date": FRI, "day": "Friday", "employee": e, "role": role, "shift_start": st, "shift_end": en}
                   for e, role, st, en in FRIDAY_SERVERS]
    assert not [v for v in sr.violations(usual_night, c) if v["kind"] == "coverage_floor"]
    stored = json.loads(get_restaurant(rid).crew_answers_json)["Server|Friday|night"]
    assert stored["unit"] == "concurrent" and stored["floor_set"] == 4 and stored["review"]


def test_a_floor_kept_in_crew_names_its_source_on_the_breach(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    cm.answer(rid, "Server", "Friday", "night", "hold", history_id=hid)
    c = sr.build_constraints(rid, [FRI], ["Friday"])
    assert ("server", "Friday", "night") in c.crew_floor_sources
    rows = [{"date": FRI, "day": "Friday", "employee": e, "role": "Server", "shift_start": "5:00pm",
             "shift_end": "11:00pm", "scheduled_hours": "6"} for e in ("A", "B", "C")]
    floor = [v for v in sr.violations(rows, c) if v["kind"] == "coverage_floor"]
    assert floor and floor[0]["floor_source"] == "crew" and "kept in Crew on" in floor[0]["detail"]


def test_undo_restores_exactly_the_minimum_that_was_there(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    # A general dinner floor of 2 with "none on Fridays" (an explicit 0).
    sr.save_role_floors(rid, {"Server": {"morning": 0, "night": 2, "days": {"Friday": {"night": 0}}}})
    cm.answer(rid, "Server", "Friday", "night", "hold", history_id=hid)
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Server", "Friday", "night") == 4
    cm.answer(rid, "Server", "Friday", "night", "clear", history_id=hid)
    floors = sr.role_floors(get_restaurant(rid))
    assert sr.floor_for(floors, "Server", "Friday", "night") == 0          # not the general 2 (BUGSGEN-18)
    assert floors["Server"]["days"]["Friday"] == {"night": 0}


def test_undo_never_overwrites_a_minimum_the_owner_changed_since(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    sr.save_role_floors(rid, {"Server": {"morning": 0, "night": 2, "days": {}}})
    cm.answer(rid, "Server", "Friday", "night", "hold", history_id=hid)
    floors = sr.role_floors(get_restaurant(rid))
    floors["Server"]["days"]["Friday"] = {"night": 5}                       # set in Rules after the hold
    sr.save_role_floors(rid, floors)
    out, _ = cm.answer(rid, "Server", "Friday", "night", "clear", history_id=hid)
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Server", "Friday", "night") == 5   # BUGSLIFE-18
    assert "stays at 5" in out["message"]
    assert _cell(out, "Server", "Friday", "night")["answer"] is None
    # and the breach no longer says Crew kept it
    assert not cm.floor_sources(get_restaurant(rid), sr.role_floors(get_restaurant(rid)))


def test_a_legacy_hold_in_the_old_unit_still_undoes(monkeypatch):
    rid, hid = _setup(monkeypatch)
    sr.save_role_floors(rid, {"Server": {"morning": 0, "night": 0, "days": {"Friday": {"night": 6}}}})
    models.update_restaurant(rid, {"crew_answers_json": json.dumps(
        {"Server|Friday|night": {"answer": "hold", "usual": 6, "floor_before": 0, "at": "2026-10-10 12:00:00"}})})
    cm.answer(rid, "Server", "Friday", "night", "clear", history_id=hid)
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Server", "Friday", "night") == 0


# ── Leaner is fine binds the requirement ───────────────────────────────────

def test_leaner_is_fine_binds_the_requirement_the_score_and_the_top_up_read(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    out, status = cm.answer(rid, "Server", "Friday", "night", "lean", history_id=hid)
    assert status == 200
    cell = _cell(out, "Server", "Friday", "night")
    assert cell["answer"] == "lean" and cell["accepted"] == 3
    # only Friday lunch (Ann's 11am-7pm, not in this draft) still asks
    assert out["open"] == 1 and _cell(out, "Server", "Friday", "morning")["status"] == "under"
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Server", "Friday", "night") == 0
    # The staffing baseline every requirement reads holds the shift to it.
    pats = labor.historical_patterns(_hist(4, FRIDAY_SERVERS))
    bound = cm.bind_answers(get_restaurant(rid), pats, today="2026-10-12")
    assert pats["typical_headcount"][("Friday", "night")]["Server"] == 3
    assert bound[("Friday", "night")]["Server"]["was"] == 6
    import schedule_requirements as req
    rows = req.shift_requirements([FRI], typical_headcount=pats["typical_headcount"], crew_answers=bound)
    night = next(r for r in rows if r["daypart"] == "night")
    srv = next(x for x in night["roles"] if x["role"] == "Server")
    assert srv["required"] == 3 and "leaner is fine" in srv["reason"]
    # The coverage score is not short for the accepted crew (TRUST-8).
    import shift_quality as sq
    draft = [{"date": FRI, "day": "Friday", "employee": f"S{i}", "role": "Server", "shift_start": "5:00pm",
              "shift_end": "11:00pm", "scheduled_hours": "6", "notes": ""} for i in range(3)]
    scored = sq.score_rows(draft, profiles=[sq.ShiftProfile()],
                           requirements_by_date=req.requirements_map(rows))
    night_shift = next(s for s in scored["shifts"] if s["daypart"] == "night")
    cov = next(d for d in night_shift["dimensions"] if d["key"] == "coverage")
    assert cov["score"] == 100 and cov["facts"]["required"] == 3


def test_leaner_is_fine_stops_binding_at_its_review_or_when_the_usual_moves(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    cm.answer(rid, "Server", "Friday", "night", "lean", history_id=hid, today="2026-10-11")
    pats = labor.historical_patterns(_hist(4, FRIDAY_SERVERS))
    late = (date(2026, 10, 11) + timedelta(days=cm.LEAN_REVIEW_DAYS + 1)).isoformat()
    assert cm.bind_answers(get_restaurant(rid), pats, today=late) == {}
    assert pats["typical_headcount"][("Friday", "night")]["Server"] == 6
    # the usual crew moved since: asked again, not bound
    moved = labor.historical_patterns(_hist(4, FRIDAY_SERVERS[1:4]))
    assert cm.bind_answers(get_restaurant(rid), moved, today="2026-10-12") == {}
    out = cm.build(rid, history_id=hid, baseline=moved, today="2026-10-12")
    assert _cell(out, "Server", "Friday", "night")["answer_state"] == "review"


def test_a_draft_leaner_than_what_was_accepted_asks_again(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    cm.answer(rid, "Server", "Friday", "night", "lean", history_id=hid)
    two = [(FRI, "Friday", f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(2)]
    hid2 = _week(rid, two)
    cell = _cell(cm.build(rid, history_id=hid2), "Server", "Friday", "night")
    assert cell["answer"] is None and cell["answer_state"] == "short_of_accepted"


# ── the week on screen, the dates, the race ────────────────────────────────

def test_an_answer_comes_back_for_the_week_it_was_given_on(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, old = _setup(monkeypatch, friday=three)
    _week(rid, [("2026-10-23", "Friday", "Z", "Server", "5:00pm", "11:00pm")], start="2026-10-19")
    out, status = cm.api_body(rid, "POST", {"role": "Server", "day": "Friday", "part": "night", "answer": "lean",
                                            "history_id": old}, may_answer=True)
    assert status == 200 and out["history_id"] == old and out["week_start"] == "2026-10-12"   # TRUST-10


def test_trend_dates_read_m_d_yy(monkeypatch):
    base = _baseline()
    base["concurrent_trends"] = [{"day": "Friday", "part": "night", "role": "Server", "now": 4, "was": 3,
                                  "since": "2026-09-14", "direction": "up"}]
    rid, _hid = _setup(monkeypatch, baseline=base)
    t = _cell(cm.build(rid), "Server", "Friday", "night")["trend"]
    assert t == {"was": 3, "since": "9/14/26"}                                 # TRUST-11


def test_two_answers_at_once_both_land(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three, tuesday=[("Ivy", "Kitchen", "9:00am", "3:00pm")])
    stale = cm._snapshot(rid)
    # Another login's answer lands between this one's read and its write.
    cm.answer(rid, "Kitchen", "Tuesday", "morning", "lean", history_id=hid)
    real = cm._snapshot
    calls = []

    def snap(*a, **k):
        calls.append(1)
        return stale if len(calls) == 1 else real(*a, **k)
    monkeypatch.setattr(cm, "_snapshot", snap)
    _out, status = cm.answer(rid, "Server", "Friday", "night", "hold", history_id=hid)
    assert status == 200 and len(calls) == 2                                  # the stale write was refused, retried
    stored = json.loads(get_restaurant(rid).crew_answers_json)
    assert set(stored) == {"Kitchen|Tuesday|morning", "Server|Friday|night"}  # BUGSLIFE-19
    assert sr.floor_for(sr.role_floors(get_restaurant(rid)), "Server", "Friday", "night") == 4


def test_add_one_this_week_offers_a_legal_shift_and_saves_nothing(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    import schedule_engine as se
    seen = {}

    def fake(restaurant_id, rows, the_date, role, part, constraints=None):
        seen.update(date=the_date, role=role, part=part, n=len(rows))
        return ({"date": the_date, "day": "Friday", "employee": "Jordan", "role": role, "shift_start": "4:30pm",
                 "shift_end": "10:00pm", "scheduled_hours": "5.5", "notes": "added — one more Server this week"}, 24.0)
    monkeypatch.setattr(se, "crew_add_suggestion", fake)
    before = get_restaurant(rid).crew_answers_json
    out, status = cm.answer(rid, "Server", "Friday", "night", "add", history_id=hid)
    assert status == 200 and out["ok"] and out["suggestion"]["employee"] == "Jordan"
    assert out["text"] == "Add Jordan, Fri 4:30pm–10:00pm (24h this week)"
    assert seen["date"] == FRI and seen["part"] == "night"
    assert get_restaurant(rid).crew_answers_json == before


# ── the rest of the stage ──────────────────────────────────────────────────

def test_only_someone_who_builds_the_schedule_answers_and_bad_input_is_refused(monkeypatch):
    rid, hid = _setup(monkeypatch)
    out, status = cm.api_body(rid, "POST", {"role": "Kitchen", "day": "Tuesday", "part": "morning", "answer": "hold"},
                              may_answer=False)
    assert status == 403
    out, status = cm.api_body(rid, "POST", {"role": "Kitchen", "day": "Funday", "part": "morning", "answer": "hold"},
                              may_answer=True)
    assert status == 400
    out, status = cm.api_body(rid, "POST", {"role": "Barback", "day": "Monday", "part": "morning", "answer": "hold"},
                              may_answer=True)
    assert status == 400 and "no usual crew" in out["error"]
    out, status = cm.api_body(rid, "GET", args={"history_id": "x"})
    assert status == 200 and out["ok"]


def test_a_restaurant_with_no_history_says_so(monkeypatch):
    rid = create_restaurant(Restaurant(name="New", owner_email="n@x.test"))
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: {})
    out = cm.build(rid)
    assert out["ok"] and not out["has_usual"] and out["roles"] == [] and out["week_start"] is None


def test_both_routes_exist_and_the_studio_has_the_stage():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert '@client_bp.route("/api/labor/crew-matrix"' in (root / "client_api.py").read_text()
    assert '@mobile_bp.route("/labor/crew-matrix"' in (root / "mobile_api.py").read_text()
    html = (root / "templates" / "dashboard.html").read_text()
    assert 'data-ss-go="crew"' in html and 'data-stage="crew"' in html and "function ssRenderCrew()" in html
    # The answer is sent for the week on screen, and each button says its scope.
    js = html[html.index("function ssCrewDraw()"):html.index("function ssLoadHistory()")]
    assert "history_id: hid" in js and "' every ' + d3" in js and "Add 1 this week" in js
    assert "on at once" in js


def test_with_no_week_built_it_shows_the_usual_crew_and_asks_nothing(monkeypatch):
    rid = create_restaurant(Restaurant(name="Fresh", owner_email="f@x.test"))
    base = _baseline()
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: base)
    out = cm.build(rid)
    assert out["has_usual"] and out["week_start"] is None
    assert out["open"] == 0 and out["differences"] == 0
    assert {c["status"] for r in out["roles"] for c in r["cells"]} == {"usual"}


def test_managers_and_owners_are_the_manager_rules_not_the_grids(monkeypatch):
    rid = create_restaurant(Restaurant(name="Mgr", owner_email="m@x.test"))
    base = _baseline()
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: base)
    _week(rid, [(TUE, "Tuesday", "Jim", "Manager FOH", "3:00pm", "11:00pm"),
                (TUE, "Tuesday", "Erik", "Owner", "10:00am", "4:00pm")])
    roles = {r["role"] for r in cm.build(rid)["roles"]}
    assert "Manager FOH" not in roles and "Owner" not in roles and "Kitchen" in roles


def test_crew_answers_are_on_the_memory_screen_and_in_the_log(monkeypatch):
    three = [(f"S{i}", "Server", "5:00pm", "11:00pm") for i in range(3)]
    rid, hid = _setup(monkeypatch, friday=three)
    cm.answer(rid, "Server", "Friday", "night", "hold", history_id=hid, user={"username": "erik"})
    import schedule_memory as smem
    items = [i for i in smem.memory_view(rid)["items"] if i["kind"] == "crew_answer"]
    assert items and items[0]["text"] == "Keep at least 4 Server on at once every Friday dinner"
    assert not (items[0]["can_keep"] or items[0]["can_let_go"] or items[0]["can_be_rule"])
    obs = smem.observations(rid, kinds="crew_answer")
    assert obs and obs[0]["value"]["answer"] == "hold" and obs[0]["source"] == "crew"
