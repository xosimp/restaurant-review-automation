"""#12 — the fill-in and trim passes choose the move that costs the least score.

Only the budget trim ever asked the Shift Quality score which of its legal
options to take; the role floors, the coverage top-up, the section-cap
trim and the stagger each took the first option their own order gave.
Each now scores its legal options with shift_quality.LocalScorer, which
re-evaluates only the dates a move touches, and takes the one that costs
the week the least. Without a scorer every pass behaves exactly as before.
"""
import models
import schedule_economics as econ
import schedule_engine as se
import schedule_rules as sr
import shift_quality as sq

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=WEEK, week_days=DAYS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(i, emp, role="Cook", start="4:00pm", end="10:00pm", hours=6.0):
    return {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _scorer_for(scores, role, **extra):
    profiles = [sq.ShiftProfile(key="n", min_strength={role: 4}, source="restaurant")]
    return lambda rows: sq.LocalScorer(rows, profiles=profiles, scores=scores, **extra)


# ── the local scorer ────────────────────────────────────────────────────

def test_the_local_scorer_agrees_with_a_full_score_and_re_scores_only_the_touched_date():
    rows = [_row(i, n) for i in range(5) for n in ("Ana", "Bob")]
    kw = dict(profiles=[sq.ShiftProfile()], typical_headcount={(d, "night"): {"Cook": 2} for d in DAYS})
    scorer = sq.LocalScorer(rows, **kw)
    assert abs(scorer.baseline - sq.score_rows(rows, **kw)["raw_score"]) < 1e-3
    first = scorer.evaluations
    assert first == 5                                     # one evaluation per date
    changed = [r for r in rows if not (r["date"] == WEEK[3] and r["employee"] == "Bob")]
    val = scorer.score(changed)
    assert scorer.evaluations == first + 1                # only Thursday was re-scored
    assert abs(val - sq.score_rows(changed, **kw)["raw_score"]) < 1e-3
    assert scorer.cost(changed) > 0


# ── role floors ─────────────────────────────────────────────────────────

def test_the_role_floor_takes_the_legal_cook_that_costs_the_least(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])
    floors = {"Cook": {"morning": 0, "night": 1}}
    rows = [_row(0, "Bob")]
    c = _c(roster_roles={"Ana": "Cook", "Bob": "Cook"})
    # the pass's own order: the fewest hours first — Ana
    out, _n, _d = se._ensure_role_floors([dict(r) for r in rows], WEEK[:2], DAYS[:2], 1, {}, {},
                                         floors=floors, constraints=c)
    assert [r["employee"] for r in out if r["date"] == WEEK[1]] == ["Ana"]
    # the score: Tuesday night wants a strong cook, and Bob is the level 5
    out, _n, _d = se._ensure_role_floors([dict(r) for r in rows], WEEK[:2], DAYS[:2], 1, {}, {},
                                         floors=floors, constraints=c,
                                         scorer_for=_scorer_for({"Ana": 2, "Bob": 5}, "Cook"))
    assert [r["employee"] for r in out if r["date"] == WEEK[1]] == ["Bob"]


# ── the coverage top-up ─────────────────────────────────────────────────

def test_the_top_up_takes_the_legal_people_that_cost_the_least(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])
    # Tuesday to Saturday: a Sunday shift is nobody's seventh day in a row
    rows = [_row(i, n, role="Server", start="4:00pm", end="9:00pm", hours=5.0)
            for i in range(1, 6) for n in ("Ana", "Bob", "Cy")]
    rows.append(_row(6, "Ana", role="Server", start="4:00pm", end="9:00pm", hours=5.0))
    c = _c(roster_roles={"Dee": "Server", "Eve": "Server"})
    kw = dict(hours_budget=400.0, hours_scheduled=95.0, restaurant_id=1, close_times={}, role_buffers={},
              constraints=c)
    targets = {d: 40.0 for d in WEEK}
    out, _h, _d = se._top_up_hours_gap([dict(r) for r in rows], dict(targets), **kw)
    plain = sorted(r["employee"] for r in out if r["date"] == WEEK[6] and "top-up" in r["notes"])
    assert plain == ["Dee", "Eve"]                        # the fewest hours first
    plain_rows = out
    scorer_for = _scorer_for({"Ana": 4, "Bob": 5, "Cy": 5, "Dee": 1, "Eve": 1}, "Server")
    out, _h, _d = se._top_up_hours_gap([dict(r) for r in rows], dict(targets), scorer_for=scorer_for, **kw)
    scored = sorted(r["employee"] for r in out if r["date"] == WEEK[6] and "top-up" in r["notes"])
    # Sunday wants strength: the level 5 goes on first, and the week the
    # score built is at least as good as the one the order built.
    assert "Bob" in scored and scored != plain
    judge = scorer_for(rows)
    assert judge.score(out) > judge.score(plain_rows)


def test_the_top_up_still_tries_the_next_legal_person_when_the_first_is_not():
    """The fewest-hours person failing the hours check used to drop the
    whole day, with two legal servers free."""
    rows = [_row(i, n, role="Server", start="4:00pm", end="9:00pm", hours=5.0)
            for i in range(1, 6) for n in ("Ana", "Bob", "Cy")]
    rows.append(_row(6, "Ana", role="Server", start="4:00pm", end="9:00pm", hours=5.0))
    c = _c(roster_roles={"Dee": "Server"}, hours_limits={"dee": (None, 3)})
    out, _h, dates = se._top_up_hours_gap([dict(r) for r in rows], {d: 40.0 for d in WEEK}, 400.0, 95.0, 1,
                                          {}, {}, constraints=c)
    assert WEEK[6] in dates


# ── the section cap ─────────────────────────────────────────────────────

def test_the_cap_cuts_the_server_the_floor_can_best_spare():
    rows = [_row(5, "Ana", role="Server", start="4:00pm"), _row(5, "Bob", role="Server", start="4:30pm"),
            _row(5, "Cy", role="Server", start="5:00pm")]
    out, n, _d = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=2)
    assert n == 1 and "Cy" not in {r["employee"] for r in out}     # last in, first cut
    out, n, _d = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=2,
                                             scorer_for=_scorer_for({"Ana": 2, "Bob": 2, "Cy": 5}, "Server"))
    assert n == 1
    kept = {r["employee"]: r for r in out}
    assert "Cy" in kept and kept["Cy"]["shift_end"] == "10:00pm"  # the level 5 stays on for the night
    assert any("over the 2-server cap" in (r.get("notes") or "") for r in out)


# ── the stagger ─────────────────────────────────────────────────────────

def test_the_stagger_backs_off_where_the_curve_needs_people_on():
    rows = [_row(5, n, role="Server", start="5:00pm", end="10:00pm", hours=5.0) for n in ("Ana", "Bob", "Cy", "Dee")]
    curve = {"Saturday": {17: 0.28, 18: 0.29, 19: 0.30, 20: 0.13}}
    out, changes = econ.stagger_same_starts([dict(r) for r in rows], curve)
    assert len(changes) == 3                              # the pass alone moves everyone but the first
    scorer = sq.LocalScorer(rows, profiles=[sq.ShiftProfile()],
                            typical_headcount={("Saturday", "night"): {"Server": 4}}, demand_curve=curve)
    out, changes = econ.stagger_same_starts([dict(r) for r in rows], curve, score_fn=scorer.score)
    # three of the four must stay on at 5pm, where the curve already needs three
    assert sum(1 for r in out if r["shift_start"] == "5:00pm") >= 3
    assert scorer.score(out) >= scorer.baseline - 1e-9


# ── the engine wires every pass to the scorer ───────────────────────────

def test_the_engine_hands_every_pass_the_scorer():
    import inspect
    src = inspect.getsource(se._run_schedule_job)
    for call in ("_ensure_role_floors(", "_top_up_hours_gap(", "_trim_server_overlap_cap(", "stagger_same_starts("):
        seg = src.split(call, 1)[1].split(")", 3)
        assert "scorer" in "".join(seg[:3]), call
    assert "_trim_scorer" in src                           # the budget trim scores locally too
