"""Schedule fix round 10/10/26, workstream J3 — MARKET-21 / RM-22: the
outcome benchmark. Weeks Cavnar AI drafted against weeks the manager built,
on the same as-run measures, with intervals; across restaurants only above
the privacy floor."""
import datetime as dt

import pytest

import models
import outcome_benchmark as ob
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import intelligence.jobs as ij
    monkeypatch.setattr(ij, "excluded_learning_ids", lambda *a, **k: set())
    yield


TODAY = dt.date(2026, 10, 12)          # a Monday: the last ended week starts 10/5


def _restaurant(i=0, cav_pct=26.0, man_pct=30.0, weeks=8):
    """The newest `weeks` drafted by Cavnar AI, the `weeks` before them and
    the same calendar weeks a year earlier built by the manager — the
    season-matched comparison reads a Cavnar AI week against the manager's
    weeks of the same time of year (blind re-audit 10/11/26 LEARNING-29)."""
    rid = create_restaurant(Restaurant(name=f"Bench Co {i}", owner_email=f"o{i}@x.test", module_labor=1))
    conn = models.get_conn()
    last = dt.date(2026, 10, 5)
    for w in list(range(2 * weeks)) + list(range(52, 52 + 2 * weeks)):
        ws = last - dt.timedelta(weeks=w)
        cavnar = w < weeks                                   # the newest weeks are Cavnar AI's
        pct = (cav_pct if cavnar else man_pct) + (w % 3) * 0.4
        for d in range(7):
            day = ws + dt.timedelta(days=d)
            conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales) "
                         "VALUES (?,?,?,?,?,?)", (rid, day.isoformat(), day.strftime("%A"), pct, pct * 10, 1000.0))
        if cavnar:
            hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                               "published_at) VALUES (?,?,?,?,datetime('now'))",
                               (rid, ws.isoformat(), (ws + dt.timedelta(days=6)).isoformat(), "x")).lastrowid
            conn.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) "
                         "VALUES (?,?,1,'generated','x')", (rid, hid))
    conn.commit()
    conn.close()
    return rid


def test_a_week_is_classed_by_who_built_it():
    rid = _restaurant()
    assert ob.week_class(rid, dt.date(2026, 10, 5))[0] == "cavnar"
    assert ob.week_class(rid, dt.date(2026, 7, 6)) == ("manager", None)


def test_the_owner_sees_cavnar_weeks_against_their_own_with_an_interval():
    rid = _restaurant()
    v = ob.restaurant_view(rid, today=TODAY)
    lab = v["measures"]["labor_pct"]
    assert v["available"] and v["weeks"] == {"cavnar": 8, "manager": 24}
    assert lab["stated"] and lab["difference"] == pytest.approx(-4.0, abs=0.4) and lab["hi"] < 0
    assert lab["verdict"] == "better" and lab["matched"]["cavnar"] == 8
    assert "in the 8 weeks Cavnar AI drafted" in v["lines"][0] and "90% range" in v["lines"][0]
    assert "same time of year" in v["lines"][0]
    # Nothing else was measured: no figure, never a zero.
    assert v["measures"]["ot_hours"]["stated"] is False and v["measures"]["cover_rate"]["cavnar"]["mean"] is None
    assert "not a controlled test" in v["caveat"]


def test_too_few_weeks_on_a_side_is_not_stated():
    rid = _restaurant(weeks=3)
    v = ob.restaurant_view(rid, today=TODAY)
    assert v["available"] is False and v["measures"]["labor_pct"]["stated"] is False and v["lines"] == []


def test_the_platform_figure_is_published_only_above_the_privacy_floor():
    few = [_restaurant(i) for i in range(4)]
    v = ob.platform_view(today=TODAY, restaurant_ids=few)
    lab = v["measures"]["labor_pct"]
    assert lab["restaurants"] == 4 and lab["published"] is None and "4 of 5 restaurants" in lab["floor"]
    more = few + [_restaurant(i) for i in range(4, 7)]
    v = ob.platform_view(today=TODAY, restaurant_ids=more)
    lab = v["measures"]["labor_pct"]
    assert lab["restaurants"] == 7 and lab["organisations"] == 7 and lab["floor"] is None
    pub = lab["published"]
    assert pub and pub["difference"] < 0 and pub["ci90"][1] < 0
    assert pub["claim"].startswith("Across 5+ restaurants, weeks Cavnar AI drafted ran")
    assert "Bench Co" not in str(pub)
    # A measure nobody has stays unpublished.
    assert v["measures"]["ot_hours"]["published"] is None


def test_unmanaged_minutes_come_from_the_scorecards_rows_of_published_weeks():
    rid = create_restaurant(Restaurant(name="Scored Co", owner_email="s@x.test", module_labor=1))
    conn = models.get_conn()
    last = dt.date(2026, 10, 5)
    for w in list(range(16)) + list(range(52, 68)):
        ws = last - dt.timedelta(weeks=w)
        hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                           "VALUES (?,?,?,?,datetime('now'))",
                           (rid, ws.isoformat(), (ws + dt.timedelta(days=6)).isoformat(), "x")).lastrowid
        drafted = w < 8
        if drafted:
            conn.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) "
                         "VALUES (?,?,1,'generated','x')", (rid, hid))
        conn.execute("INSERT INTO schedule_learning_scores (restaurant_id, history_id, week_start, unmanaged_minutes) "
                     "VALUES (?,?,?,?)", (rid, hid, ws.isoformat(), (0 if drafted else 45) + w % 3))
    conn.commit()
    conn.close()
    v = ob.restaurant_view(rid, today=TODAY)
    um = v["measures"]["unmanaged_minutes"]
    assert um["stated"] and um["cavnar"]["n"] == 8 and um["manager"]["n"] == 24 and um["verdict"] == "better"
    assert any("Minutes on the floor with no manager" in ln for ln in v["lines"])


def test_a_season_the_manager_never_ran_is_not_compared():
    # Cavnar AI's weeks are October's; the manager's only weeks are March's:
    # nothing of the same time of year, so nothing is stated (LEARNING-29).
    rid = create_restaurant(Restaurant(name="Season Co", owner_email="season@x.test", module_labor=1))
    conn = models.get_conn()
    last = dt.date(2026, 10, 5)
    for w in list(range(8)) + list(range(26, 40)):
        ws = last - dt.timedelta(weeks=w)
        for d in range(7):
            day = ws + dt.timedelta(days=d)
            conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, "
                         "sales) VALUES (?,?,?,?,?,?)", (rid, day.isoformat(), day.strftime("%A"), 30, 300, 1000.0))
        if w < 8:
            hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                               "published_at) VALUES (?,?,?,?,datetime('now'))",
                               (rid, ws.isoformat(), (ws + dt.timedelta(days=6)).isoformat(), "x")).lastrowid
            conn.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) "
                         "VALUES (?,?,1,'generated','x')", (rid, hid))
    conn.commit()
    conn.close()
    lab = ob.restaurant_view(rid, today=TODAY)["measures"]["labor_pct"]
    assert lab["stated"] is False and lab["matched"]["cavnar"] == 0


def test_one_measure_by_chance_is_not_a_claim():
    # Five measures, each a paired difference of pure noise: Holm over them
    # keeps a chance interval from becoming "better" (LEARNING-29).
    import random as _r
    rng = _r.Random(3)
    claims = 0
    for _ in range(200):
        weeks = []
        for k in range(8):
            for cls in ("cavnar", "manager"):
                ws = (dt.date(2026, 8, 10) + dt.timedelta(weeks=k)) if cls == "cavnar" else \
                    (dt.date(2025, 8, 10) + dt.timedelta(weeks=k))
                weeks.append({"week_start": ws.isoformat(), "class": cls,
                              **{m: rng.gauss(10, 2) for m in ob.MEASURES}})
        claims += any(e["verdict"] in ("better", "worse") for e in ob.compare(weeks).values())
    assert claims / 200.0 <= 0.15, claims
