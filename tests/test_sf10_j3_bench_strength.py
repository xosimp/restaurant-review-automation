"""Schedule fix round 10/10/26, workstream J3 — MARKET-32: single points of
failure, who to cross-train next, and when to hire."""
import datetime as dt

import pytest

import bench_strength as bs
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


TODAY = dt.date(2026, 10, 12)


def _row(d, name, role, start="5:00pm", end="11:00pm", hours=6):
    return {"date": d.isoformat(), "employee": name, "role": role, "shift_start": start, "shift_end": end,
            "scheduled_hours": hours}


def _data(grow=0.0):
    """Twelve weeks: the bar runs 5 nights a week with one bartender a night,
    Bea closing almost all of it and Ben on the rest; servers run 3 a night
    from a team of six, Sal among them having worked 7 bar shifts beside a
    closer."""
    rows = []
    start = TODAY - dt.timedelta(weeks=12)
    for w in range(12):
        for i in range(5):
            d = start + dt.timedelta(weeks=w, days=i)
            rows.append(_row(d, "Bea" if i < 4 else "Ben", "Bartender", hours=6 + grow * w))
            for j, n in enumerate(("Sal", "Sue", "Sam", "Sid", "Sky", "Sol")):
                if (i + j) % 2 == 0 and j < 6:
                    rows.append(_row(d, n, "Server", hours=5))
    roster = {n.casefold(): n for n in ("Bea", "Ben", "Sal", "Sue", "Sam", "Sid", "Sky", "Sol")}
    return {"rows": rows, "roster": roster, "closers": {"bartender": {"bea"}}, "leaders": {"bea"},
            "mentored": {"sal": {"Bartender": 7}, "sue": {"Bartender": 2}}, "scores": {"sal": 4, "sue": 5},
            "left": [], "families": {}}


def test_one_closer_for_the_bar_is_a_single_point_of_failure():
    st = bs.strength(1, today=TODAY, data=_data())
    bar = next(p for p in st["pools"] if p["family"] == "bartender" and p["daypart"] == "night")
    assert bar["status"] == "critical" and bar["why"] == "only Bea can close"
    assert bar["closers"] == 1 and bar["nights"] == 5.0 and bar["need"] == 1
    srv = next(p for p in st["pools"] if p["family"] == "server")
    assert srv["status"] == "ok" and srv["slack"] > 0
    assert st["pools"][0]["status"] == "critical"


def test_the_next_closer_to_train_is_from_the_role_ranked_by_shifts_beside_a_closer():
    data = _data()
    plan = bs.cross_train(1, today=TODAY, data=data)
    close = [c for c in plan if c["kind"] == "close" and c["family"] == "bartender"]
    # Ben works the bar already; Sal has shifts beside a closer but has never
    # worked a bar shift — a closer is trained from the people in the role.
    assert close[0]["who"] == "Ben" and close[0]["text"].startswith("Train Ben to close bartender")


def test_the_hiring_forecast_crosses_capacity_when_the_hours_keep_growing():
    flat = bs.hiring(1, today=TODAY, data=_data())
    bar = next(h for h in flat if h["family"] == "bartender")
    assert bar["weeks_left"] is None and bar["text"] is None
    grow = bs.hiring(1, today=TODAY, data=_data(grow=0.6))
    bar = next(h for h in grow if h["family"] == "bartender")
    assert bar["trend_per_week"] > 0 and bar["weeks_left"] is not None
    assert bar["hire_by_label"] and "/" in bar["hire_by_label"] and ("hire by" in bar["text"] or "hire now" in bar["text"])


def test_a_restaurant_with_no_history_has_nothing_to_say():
    rid = create_restaurant(Restaurant(name="New Co", owner_email="n@x.test", module_labor=1))
    v = bs.owner_view(rid, today=TODAY)
    assert v["ok"] and v["available"] is False and v["pools"] == [] and v["hiring"] == []
