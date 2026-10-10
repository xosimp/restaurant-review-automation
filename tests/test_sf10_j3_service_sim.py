"""Schedule fix round 10/10/26, workstream J3 — MARKET-22: the
counterfactual service simulator (a queueing twin fitted from the ticket
archive and the punches, replayed with a different crew)."""
import random
from datetime import date, datetime, timedelta

import pytest

import models
import service_sim as sim
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    sim._CACHE.clear()
    yield


TODAY = date(2026, 10, 10)          # a Saturday
TRUE_P, TRUE_W = 6.0, 7.0


def test_the_replay_is_first_come_first_served_with_the_crew_on_at_the_time():
    # Two tickets at once, one cook, 10 minutes each: the second waits 10.
    assert sim.simulate([0.0, 0.0], {0: 1, 1: 1, 2: 1}, 10) == [0.0, 10.0]
    # A second cook takes it at once.
    assert sim.simulate([0.0, 0.0], {0: 2, 1: 2}, 10) == [0.0, 0.0]
    assert sim.simulate([0.0, 0.0], {0: 1, 1: 1}, 10, delta=1) == [0.0, 0.0]
    # Nobody on in the first quarter hour: the ticket waits for the second.
    assert sim.simulate([2.0], {0: 0, 1: 1}, 5) == [13.0]
    # One fewer than the only cook strands what arrives (never below zero,
    # and a quarter hour that had nobody keeps nobody).
    assert sim.simulate([1.0, 20.0], {0: 1, 1: 1}, 5, delta=-1) == [None, None]


def _seed(rid):
    """Ten Saturdays and ten Fridays, 5-10pm: kitchen tickets fired at a
    steady rate, ticket times from the true twin (+-10%), two or three cooks
    on depending on the night."""
    rng = random.Random(7)
    conn = models.get_conn()
    n = 0
    for w in range(1, 11):
        for back in (0, 1):                       # Saturday, Friday
            night = TODAY - timedelta(weeks=w) - timedelta(days=back)
            cooks = 2 if (w + back) % 2 else 3
            for i in range(cooks):
                n += 1
                conn.execute("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date, employee_name, "
                             "role, clock_in, clock_out) VALUES (?,?,?,?,?,?,?,?)",
                             (rid, "rpower", f"p{n}", night.isoformat(), f"Cook{i}", "Line Cook",
                              f"{night.isoformat()} 16:30:00", f"{night.isoformat()} 22:30:00"))
            arr = []
            for k in range(17 * 4, 22 * 4):
                per = 3 if (k < 18 * 4 or k >= 21 * 4) else 4                   # tickets a quarter hour
                arr += [k * 15 + j * 0.5 for j in range(per)]           # a wave of tickets each quarter hour
            crew = {k: cooks for k in range(66, 92)}
            waits = sim.simulate(arr, crew, TRUE_W)
            for a, wt in zip(arr, waits):
                n += 1
                fired = datetime.combine(night, datetime.min.time()) + timedelta(minutes=a)
                mins = (TRUE_P + TRUE_W + wt) * rng.uniform(0.9, 1.1)
                conn.execute("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, "
                             "closed_at, fired_at, bumped_at, guest_count) VALUES (?,?,?,?,?,?,?,?,0)",
                             (rid, "rpower", f"t{n}", night.isoformat(), fired.strftime("%Y-%m-%d %H:%M:%S"),
                              (fired + timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S"),
                              fired.strftime("%Y-%m-%d %H:%M:%S"),
                              (fired + timedelta(minutes=mins)).strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def test_the_twin_recovers_the_kitchen_and_beats_the_plain_mean():
    rid = create_restaurant(Restaurant(name="Sim Co", owner_email="s@x.test", module_labor=1))
    _seed(rid)
    f = sim.fit(rid, today=TODAY)
    k = f["kitchen"]
    assert k["available"] and k["nights"] >= 19
    assert k["skill"] > 0.2
    assert abs(k["W"] - TRUE_W) / TRUE_W < 0.4
    assert len(k["boot"]) >= 20
    # The dining room has no timed tables here: no twin, said.
    assert f["floor"]["available"] is False and f["floor"]["reason"]


def test_one_fewer_cook_on_a_past_night_runs_longer_with_an_interval():
    rid = create_restaurant(Restaurant(name="Sim Co", owner_email="s@x.test", module_labor=1))
    _seed(rid)
    night = (TODAY - timedelta(weeks=2)).isoformat()            # a three-cook Saturday
    r = sim.replay(rid, night, {"line_cook": -1}, today=TODAY)
    k = r["stages"]["kitchen"]
    assert r["ok"] and k["available"] and k["change"] == -1
    assert k["moved"] > 1.0 and k["predicted"] > k["as_run"]
    assert k["moved_lo"] <= k["moved"] <= k["moved_hi"]
    # One more cook is the other direction.
    more = sim.replay(rid, night, {"kitchen": 1}, today=TODAY)["stages"]["kitchen"]
    assert more["moved"] < 0
    # Not enough reviewed nights: the star risk is withheld, never invented.
    assert r["review_risk"]["stars"] is None and "Not stated" in r["review_risk"]["basis"]


def test_the_studio_what_if_replays_the_last_same_weekdays_in_plain_words():
    rid = create_restaurant(Restaurant(name="Sim Co", owner_email="s@x.test", module_labor=1))
    _seed(rid)
    w = sim.what_if(rid, "saturday", {"line_cook": -1}, today=TODAY)
    assert w["available"] and w["weekday"] == "Saturday" and len(w["nights"]) == sim.LOOKBACK
    assert all("/" in n and "-" not in n for n in w["nights"])           # M/D/YY
    assert w["stages"]["kitchen"]["moved"] > 0
    assert "With 1 fewer cook, Saturday's ticket time would have run about" in w["sentence"]
    assert "longer" in w["sentence"]
    assert sim.marginal_minutes(rid, "kitchen", 18, 2, today=TODAY) > 0


def test_g2s_measured_effect_sits_beside_the_model_never_blended(monkeypatch):
    rid = create_restaurant(Restaurant(name="Sim Co", owner_email="s@x.test", module_labor=1))
    _seed(rid)
    import staffing_elasticity
    monkeypatch.setattr(staffing_elasticity, "stored", lambda *a, **k: {
        ("line_cook", "night"): {"outcomes": {"kitchen": {"est": -3.0, "lo": -5.0, "hi": -1.0, "n": 6,
                                                          "method": "pooled"}}}})
    w = sim.what_if(rid, "Saturday", {"line_cook": -1}, today=TODAY)
    k = w["stages"]["kitchen"]
    assert k["measured"]["implied"] == 3.0 and k["measured"]["per_person"] == -3.0
    # The model's own figure stays the model's: never replaced by the measured one.
    assert k["moved"] == sim.what_if(rid, "Saturday", {"line_cook": -1}, today=TODAY)["stages"]["kitchen"]["moved"]
    assert "your own short dinners measured about 3 minutes longer" in w["sentence"]


def test_no_ticket_archive_has_no_twin_and_says_why():
    rid = create_restaurant(Restaurant(name="Toast Co", owner_email="t@x.test", module_labor=1))
    w = sim.what_if(rid, "Saturday", {"server": -1}, today=TODAY)
    assert w["available"] is False and "RPOWER" in (w["reason"] or "")
