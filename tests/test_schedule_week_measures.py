"""#31 — weekly measures are weighted once and shown at week level.

Fatigue is a property of the WEEK (too many busy shifts, too many days in a
row, an over-long week), but it was scored on every shift a tired person
worked: the same strain counted five or six times, more on a peak night
(which weighs double), and more on a thin shift than a busy one. It is now
judged once per person per week and blended into the week score once, at
the share it always carried, and shown in the week-level breakdown.
"""
import schedule_optimizer as so
import shift_quality as sq

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _row(i, emp, role="Cook", start="4:00pm", end="10:00pm"):
    return {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "6", "notes": ""}


KW = dict(profiles=[sq.ShiftProfile()], typical_headcount={(d, "night"): {"Cook": 3} for d in DAYS})


def _week(nonstop_days=7):
    rows = [_row(i, "Nonstop") for i in range(nonstop_days)]
    for n in ("A", "B", "C"):
        rows += [_row(i, n) for i in range(7) if (i + ord(n)) % 3]
    return rows


def test_fatigue_is_judged_once_for_the_week_not_on_every_shift():
    out = sq.score_rows(_week(), **KW)
    assert not any(d["key"] == "fatigue" for s in out["shifts"] for d in s["dimensions"])
    week = [d for d in out["week_dimensions"] if d["key"] == "fatigue"]
    assert len(week) == 1
    f = week[0]
    # once per PERSON: one strained cook of four
    assert f["facts"]["strained"] == ["Nonstop"] and f["facts"]["tracked"] == 4
    assert f["score"] == 75
    assert 0 < f["share"] < 1
    # said once, at week level, and on the roll-up as a week-level entry
    assert sum("7 days in a row" in w for w in out["weaknesses"]) == 1
    assert not any("in a row" in w for s in out["shifts"] for w in s.get("weaknesses") or [])
    rollup = next(d for d in out["dimensions"] if d["key"] == "fatigue")
    assert rollup["week_level"] is True and rollup["customer_facing"] is False


def test_a_strained_person_costs_the_week_the_same_however_many_shifts_they_work():
    """The strain is the person's, once: seven days in a row cost the
    fatigue measure the same whether two of those days are busy nights
    (which weigh 1.5 in the week) or not — it used to cost more on them."""
    light = sq.score_rows(_week(), **KW)
    busy_kw = dict(KW, profiles=[sq.ShiftProfile(key="wknd", days=["Friday", "Saturday"], daypart="night",
                                                 demand="high", source="restaurant"), sq.ShiftProfile()])
    busy = sq.score_rows(_week(), **busy_kw)
    assert {s["profile"]["demand"] for s in busy["shifts"]} == {"high", "normal"}
    fl = next(d for d in light["week_dimensions"] if d["key"] == "fatigue")
    fb = next(d for d in busy["week_dimensions"] if d["key"] == "fatigue")
    assert fl["score"] == fb["score"] == 75


def test_the_week_score_does_not_move_when_nobody_is_strained(monkeypatch):
    """With nobody tired the week scores what it did when fatigue sat on
    every shift at the same weight: the share is exactly the one it had."""
    rows = _week(nonstop_days=5)
    new = sq.score_rows(rows, **KW)
    assert next(d for d in new["week_dimensions"] if d["key"] == "fatigue")["score"] == 100
    # the old arithmetic: fatigue per shift, no week-level measure
    monkeypatch.setattr(sq, "WEEK_LEVEL_DIMENSIONS", {})
    old = sq.score_rows(rows, **KW)
    # only each shift's own rounding differs (it no longer carries fatigue)
    assert abs(new["raw_score"] - old["raw_score"]) < 0.5
    assert abs(new["score"] - old["score"]) <= 1


def test_the_week_score_stays_on_the_scale():
    scored = [{"score": 0, "profile": {"demand": "peak"}, "dimensions": [{"weight": 20}]},
              {"score": 100, "profile": {"demand": "low"}, "dimensions": [{"weight": 20}]}]
    for fatigue in (0, 100):
        d = sq.DimensionResult(key="fatigue", label="Fatigue", score=fatigue, weight=7)
        raw = sq.week_score_raw(scored, [d])
        assert 0.0 <= raw <= 100.0
    assert sq.week_score_raw(scored, []) == sq.week_score_raw(scored)
    d = sq.DimensionResult(key="fatigue", label="Fatigue", score=100, weight=0)
    assert sq.week_score_raw(scored, [d]) == sq.week_score_raw(scored, [])


def test_weighting_fatigue_to_zero_takes_it_out_of_the_week():
    out = sq.score_rows(_week(), weights={"fatigue": 0}, **KW)
    assert out["week_dimensions"] == []


def test_fatigue_alone_still_never_produces_a_score():
    out = sq.score_rows([_row(5, "A"), _row(6, "A")], profiles=[sq.ShiftProfile()])
    assert out["checked"] is False


def test_the_repair_loop_sees_the_week_measure():
    out = sq.score_rows(_week(), **KW)
    assert so.objective(out) == out["raw_score"]
    probs = so._problems(out)
    assert any(d.get("week_level") and d["key"] == "fatigue" for _c, _s, d in probs)
    # one problem per shift the strained person works, so the moves stay per date
    assert len([1 for _c, s, d in probs if d.get("week_level")]) == 7


def test_the_recommendation_still_names_the_tired_person():
    out = sq.score_rows(_week(), **KW)
    assert any(r.startswith("Give Nonstop a day off") for r in out["recommendations"])
