"""The scheduler notices when a restaurant's usual crew moves and stays
moved (owner, 10/9/26: "if RPOWER shows EJ's schedules 4 bartenders on a
Saturday night, will it match this and notice trends up or down?"). The
last 4 weeks against the 4 before, held on 3 of the recent weeks, an event
or closure week left out; the typical then uses the recent level."""
from datetime import date, timedelta

import labor

SAT = [date(2026, 8, 15) + timedelta(weeks=i) for i in range(8)]      # 8 Saturdays


def _rows(per_week, role="Bartender PM", others=6):
    rows = []
    for d, n in zip(SAT, per_week):
        for i in range(n):
            rows.append({"date": d.isoformat(), "employee": f"B{i}", "role": role, "shift_start": "16:00",
                         "shift_end": "23:00"})
        for i in range(others):
            rows.append({"date": d.isoformat(), "employee": f"S{i}", "role": "Server PM", "shift_start": "16:00",
                         "shift_end": "23:00"})
    return rows


def _bar(p):
    return (p["typical_headcount"].get(("Saturday", "night")) or {}).get("Bartender PM")


def test_a_move_up_that_holds_uses_the_new_level_and_says_so():
    p = labor.historical_patterns(_rows([3, 3, 3, 3, 4, 4, 4, 4]))
    assert _bar(p) == 4, "the recent level, not the blend"
    t = [x for x in p["headcount_trends"] if x["role"] == "Bartender PM"]
    assert t and t[0]["direction"] == "up" and (t[0]["was"], t[0]["now"]) == (3, 4) and t[0]["since"] == "2026-09-12"


def test_one_busy_week_is_not_a_trend():
    p = labor.historical_patterns(_rows([3, 3, 3, 3, 3, 3, 5, 3]))
    assert not [x for x in p["headcount_trends"] if x["role"] == "Bartender PM"]


def test_a_move_down_that_holds():
    p = labor.historical_patterns(_rows([4, 4, 4, 4, 3, 3, 3, 2]))
    t = [x for x in p["headcount_trends"] if x["role"] == "Bartender PM"]
    assert t and t[0]["direction"] == "down" and _bar(p) == t[0]["now"] == 3


def test_an_event_week_is_left_out_of_the_trend():
    rows = _rows([3, 3, 3, 3, 3, 3, 3, 3])
    # One Saturday doubled across the whole crew (a holiday) - not a trend.
    d = SAT[6].isoformat()
    rows += [{"date": d, "employee": f"X{i}", "role": "Server PM", "shift_start": "16:00", "shift_end": "23:00"}
             for i in range(8)]
    rows += [{"date": d, "employee": f"Y{i}", "role": "Bartender PM", "shift_start": "16:00", "shift_end": "23:00"}
             for i in range(3)]
    p = labor.historical_patterns(rows)
    assert not [x for x in p["headcount_trends"] if x["role"] == "Bartender PM"]


def test_the_forecast_tab_shows_the_usual_crew_and_what_moved(monkeypatch):
    import schedule_engine as se
    p = labor.historical_patterns(_rows([3, 3, 3, 3, 4, 4, 4, 4]))
    monkeypatch.setattr(labor, "staffing_baseline", lambda rid, **k: p)
    out = se.learned_crew(1)
    sat = next(r for r in out["learned_crew"] if r["day"] == "Saturday" and r["part"] == "night")
    bar = next(x for x in sat["roles"] if x["role"] == "Bartender PM")
    assert bar["n"] == 4 and bar["trend"] == {"was": 3, "since": "9/12/26", "direction": "up"}
    assert out["crew_trends"][0]["since"] == "9/12/26"
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text()
    assert "h += ssCrewHtml(d);" in src and "out.update(learned_crew(restaurant_id))" in \
        (Path(__file__).resolve().parents[1] / "schedule_engine.py").read_text()
