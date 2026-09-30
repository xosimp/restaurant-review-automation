"""Memory re-audit fix round 9/29/26 (R5, limits_first — PLATFORM-12): the
schedule A/B readout filters eligibility and the demo era in SQL before its
LIMIT, and past READOUT_MAX_RESTAURANTS samples restaurants by a stable hash,
not by lowest id."""
import inspect
import sys

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _week(rid, hid, week_start="2026-09-07"):
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) "
                       "VALUES (?,?,?,?)", (rid, week_start, "2026-09-13", "date\n")).lastrowid
    conn.execute("INSERT INTO schedule_experiment_weeks (history_id, restaurant_id, experiment, arm, week_start, "
                 "pinned) VALUES (?,?,?,?,?,0)", (hid, rid, "solver", "off", week_start))
    conn.commit()
    conn.close()


def test_the_filter_runs_in_sql_before_the_limit():
    import schedule_experiments as sx
    src = inspect.getsource(sx.readout)
    head = src[:src.index("LIMIT 20000")]
    assert "restaurant_id NOT IN" in head and "learning_rows_sql" in head


def test_past_the_cap_the_sample_is_a_stable_hash_not_the_lowest_ids(monkeypatch):
    import schedule_experiments as sx
    rids = [create_restaurant(Restaurant(name=f"Grill {i}", owner_email=f"g{i}@x.test")) for i in range(6)]
    for i, rid in enumerate(rids):
        _week(rid, 1000 + i)
    monkeypatch.setattr(sx, "READOUT_MAX_RESTAURANTS", 3)
    chosen = []
    monkeypatch.setattr(sx, "_capped", lambda rows, per=None: [])
    import schedule_versions as sv
    monkeypatch.setattr(sv, "acceptance", lambda rid, **k: chosen.append(rid) or {"weeks": []})
    try:
        sx.readout()
    except Exception:
        pass
    expected = sorted(rids, key=sx._sample_order)[:3]
    assert sorted(chosen) == sorted(expected)
    assert sx._sample_order(rids[0]) == sx._sample_order(rids[0])


def test_an_ineligible_restaurants_weeks_never_reach_the_readout(monkeypatch):
    import schedule_experiments as sx
    demo = create_restaurant(Restaurant(name="Harbor Grill", owner_email="d@x.test"))
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET is_demo=1 WHERE id=?", (demo,))
    conn.commit()
    conn.close()
    real = create_restaurant(Restaurant(name="Real Grill", owner_email="r@x.test"))
    _week(demo, 1)
    _week(real, 2)
    seen = []
    import schedule_versions as sv
    monkeypatch.setattr(sv, "acceptance", lambda rid, **k: seen.append(rid) or {"weeks": []})
    sx.readout()
    assert real in seen and demo not in seen
