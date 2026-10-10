"""Schedule fix round 10/10/26, workstream J3 — MARKET-27: the commitment
ladder. Each published shift is firm, likely or tentative from the
forecast's range; the person sees it; a later change is priced, in the
solver's objective and per person as stability."""
import datetime as dt

import pytest

import commitment_ladder as cl
import demand
import models
import schedule_rules as sr
import schedule_solver as ss
import shift_quality as sq
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


TODAY = dt.date(2026, 10, 10)
FRI = "2026-10-16"
SAT = "2026-10-17"
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _row(d, name, start="5:00pm", end="10:00pm", role="Server"):
    return {"date": d, "day": sq._day_name(d), "employee": name, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "5", "notes": ""}


def _forecasts(monkeypatch):
    def fake(rid, day, db_path=None, **k):
        d = day.isoformat()
        if d == FRI:     # an event night: $5,000 with the event, $4,000 without; 80% low $3,500
            return {"available": True, "typical_sales": 5000.0, "low": 3500.0, "high": 6500.0, "base_sales": 4000.0,
                    "effect_pct": 25, "effects": [{"display": "Bears game", "label": "bears"}]}
        if d == SAT:
            return {"available": True, "typical_sales": 4000.0, "low": None}
        return {"available": False}
    monkeypatch.setattr(demand, "forecast_day", fake)


def test_the_ladder_is_firm_at_the_forecasts_low_end_and_tentative_for_the_measured_lift(monkeypatch):
    _forecasts(monkeypatch)
    rid = create_restaurant(Restaurant(name="Ladder Co", owner_email="l@x.test", module_labor=1))
    rows = [_row(FRI, "Ana", "4:00pm", "11:00pm"), _row(FRI, "Bob", "4:30pm", "10:30pm"),
            _row(FRI, "Cy", "5:00pm", "10:00pm"), _row(FRI, "Dee", "5:00pm", "10:00pm"),
            _row(FRI, "Eve", "5:30pm", "9:30pm"), _row(FRI, "Flo", "6:30pm", "9:00pm"),
            _row(FRI, "Mia", "3:00pm", "11:30pm", role="Manager"), _row(SAT, "Ana")]
    lv = cl.ladder(rid, rows)
    get = lambda n, d=FRI: lv[(d, n.lower(), next(r["shift_start"] for r in rows if r["employee"] == n and r["date"] == d))]
    assert get("Mia")["level"] == "firm" and "manager" in get("Mia")["reason"]
    # 6 servers, low/typical 0.7 -> 5 needed even at the low end; the base
    # (no event) needs 4.8 -> 5: the one beyond is there for the game.
    assert [get(n)["level"] for n in ("Ana", "Bob", "Cy", "Dee", "Eve")] == ["firm"] * 5
    assert get("Flo")["level"] == "tentative" and "Bears game" in get("Flo")["reason"]
    # No range for Saturday: nothing is called firm by assumption.
    assert get("Ana", SAT)["level"] == "likely" and "no forecast range" in get("Ana", SAT)["reason"]


def _publish(rid, rows, hid=None):
    csv = HEAD + "".join(",".join(str(r[k]) for k in ("date", "day", "employee", "role", "shift_start", "shift_end",
                                                       "scheduled_hours", "notes")) + "\n" for r in rows)
    conn = models.get_conn()
    if hid is None:
        hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                           "VALUES (?,?,?,?,datetime('now'))", (rid, "2026-10-12", "2026-10-18", csv)).lastrowid
    else:
        conn.execute("UPDATE schedule_history SET schedule_csv=? WHERE id=?", (csv, hid))
    conn.commit()
    conn.close()
    return hid


def test_a_published_week_is_committed_once_and_later_changes_are_counted_per_person(monkeypatch):
    _forecasts(monkeypatch)
    rid = create_restaurant(Restaurant(name="Ladder Co", owner_email="l@x.test", module_labor=1))
    rows = [_row(FRI, "Ana"), _row(FRI, "Bob"), _row(SAT, "Ana")]
    hid = _publish(rid, rows)
    assert cl.ensure(rid, hid, today=TODAY) == {"committed": 3, "changed": 0}
    assert cl.ensure(rid, hid, today=TODAY) == {"committed": 0, "changed": 0}
    # The staff read carries each shift's level and its words.
    shifts = cl.annotate(rid, [{"date": FRI, "start": "5:00pm"}, {"date": SAT, "start": "5:00pm"}], "Ana")
    assert shifts[0]["commitment"] == "firm" and shifts[0]["commitment_text"].startswith("Firm")
    assert shifts[1]["commitment"] == "likely"
    # Re-published without Bob's Friday and with Ana retimed Saturday.
    before = rows
    after = [_row(FRI, "Ana"), _row(SAT, "Ana", end="9:00pm")]
    cost = cl.change_cost(rid, before, after)
    assert cost["points"] == cl.COMMIT_POINTS["firm"] + cl.COMMIT_POINTS["likely"]
    assert {(x["employee"], x["level"]) for x in cost["rows"]} == {("Bob", "firm"), ("Ana", "likely")}
    assert "1 of them firm" in cost["text"] and cost["predictability_pay"] is None or isinstance(cost["predictability_pay"], dict)
    _publish(rid, after, hid)
    assert cl.ensure(rid, hid, today=TODAY)["changed"] == 2
    stab = cl.stability(rid, today=dt.date(2026, 10, 20))
    assert stab["Bob"]["changed"] == 1 and stab["Bob"]["kept_share"] == 0.0
    assert stab["Ana"]["committed"] == 2 and stab["Ana"]["kept_share"] == 0.5
    # Only rows still as committed reach the solver.
    assert cl.committed_for_week(rid, [FRI, SAT]) == {(FRI, "ana", "5:00pm"): "firm"}


WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", FRI, SAT, "2026-10-18"]


def test_the_solver_keeps_a_firm_shift_with_its_holder_unless_it_has_to():
    names = ["Ann", "Bob", "Cat"]
    rows = [_row(FRI, "Ann"), _row(WEEK[1], "Bob", "11:00am", "3:00pm"), _row(WEEK[2], "Cat", "11:00am", "3:00pm")]
    rotation = {"roles": {"Server": {"people": names, "weekend_due": [], "weekend_streak": {}, "rest_from_close": [],
                                     "next_close": [],
                                     "tips": {"rich_slots": [{"day": "Friday", "part": "night", "mean": 180}],
                                              "rest_from_rich": ["Ann"], "next_rich": ["Bob", "Cat"], "role_mean": 60,
                                              "per_shift": {"Ann": 120}}}}}
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=list(names), active={n.lower() for n in names})
    sig = {"roster": names, "roster_roles": {n: "Server" for n in names}, "rotation": rotation,
           "typical_headcount": {("Friday", "night"): {"Server": 1}, ("Tuesday", "morning"): {"Server": 1},
                                 ("Wednesday", "morning"): {"Server": 1}}}
    moved = ss.improve(rows, {}, signals=sig, constraints=c)
    assert [r["employee"] for r in moved["rows"] if r["date"] == FRI] != ["Ann"]
    kept = ss.improve(rows, {}, signals=dict(sig, commitments={(FRI, "ann", "5:00pm"): "firm"}), constraints=c)
    assert [r["employee"] for r in kept["rows"] if r["date"] == FRI] == ["Ann"]
