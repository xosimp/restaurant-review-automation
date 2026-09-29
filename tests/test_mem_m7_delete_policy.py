"""Memory fix round (9/29/26), workstream M7 — "delete_policy".

Owner decision (Will, 9/29/26): when a restaurant is deleted its own data is
always deleted, but its anonymised learning — its intel_rec_events and
intel_features rows — is KEPT under a tombstoned id (no name, no raw rows,
every recommendation key hashed), so cohort priors do not forget what a
churned restaurant measured. A demo's or a test account's rows go with it,
and so does anything a converted demo recorded before its learning_since.
"""
import json
import sqlite3

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    models._internal_homes_cache.clear()
    yield


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _q(sql, args=()):
    c = models.get_conn()
    try:
        return [tuple(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _world(name, **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{abs(hash(name)) % 10**6}@x.test"))
    cols = dict({"category": "italian"}, **cols)
    for k, v in cols.items():
        _x(f"UPDATE restaurants SET {k}=? WHERE id=?", (v, rid))
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, event_at) "
       "VALUES (?,?,?,?,?,?,?)", (rid, "overtime_move", "overtime_move:Maria G.:2026-09-01#o12", "italian",
                                  "measured", "improved", "2026-09-10"))
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, event_at) "
       "VALUES (?,?,?,?,?,?)", (rid, "trim_day", "trim_day:tuesday", "italian", "accepted", "2026-08-01"))
    _x("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) VALUES (?,?,?,?)",
       (rid, "2026-W37", json.dumps({"labor_pct_28d": 29.5}), 0.8))
    _x("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, fetched_at) "
       "VALUES (?,?,?,?,?,?,datetime('now'))", (rid, "google", "g1", "Guest", 5, "Lovely"))
    return rid


def test_a_real_restaurants_learning_is_kept_under_a_tombstone_and_its_own_data_goes():
    rid = _world("Bella's Bistro", profile_source="set")
    models.delete_restaurant(rid)
    assert _q("SELECT id FROM restaurants WHERE id=?", (rid,)) == []
    assert _q("SELECT id FROM reviews WHERE restaurant_id=?", (rid,)) == [], "the restaurant's own data goes"
    assert _q("SELECT id FROM intel_rec_events WHERE restaurant_id=?", (rid,)) == []
    (tid, cat, src, rows_json), = _q("SELECT id, category, category_source, rows_json FROM learning_tombstones")
    assert (cat, src) == ("italian", "set")
    assert json.loads(rows_json) == {"intel_features": 1, "intel_rec_events": 2}
    kept = _q("SELECT restaurant_id, rec_kind, source_key, cohort, outcome FROM intel_rec_events ORDER BY id")
    assert {r[0] for r in kept} == {-tid}
    keys = [r[2] for r in kept]
    assert keys[0].startswith("overtime_move:~") and keys[0].endswith("#o12") and "Maria" not in keys[0]
    assert keys[1].startswith("trim_day:~") and "tuesday" not in keys[1]
    assert _q("SELECT restaurant_id, week FROM intel_features") == [(-tid, "2026-W37")]
    blob = json.dumps(_q("SELECT * FROM learning_tombstones")) + json.dumps(kept)
    assert "Bella" not in blob and str(rid) not in [str(x) for r in kept for x in r if isinstance(x, int) and x > 0]


def test_the_tombstoned_rows_still_count_toward_the_platform_rate():
    from intelligence import scoring
    rid = _world("Bella's Bistro")
    before = scoring.kind_stats("overtime_move")
    models.delete_restaurant(rid)
    after = scoring.kind_stats("overtime_move")
    assert after["measured"] == before["measured"] == 1


def test_a_second_delete_does_not_sweep_the_tombstones_as_orphans():
    a = _world("Bella's Bistro")
    b = _world("Nonna's Kitchen")
    models.delete_restaurant(a)
    models.delete_restaurant(b)
    assert len(_q("SELECT id FROM learning_tombstones")) == 2
    assert len(_q("SELECT id FROM intel_rec_events WHERE restaurant_id < 0")) == 4


@pytest.mark.parametrize("name,cols", [("Simple EJ's Demo", {"is_demo": 1}), ("Preview Test", {}),
                                       ("Our Kitchen", {"billing_status": "internal"})])
def test_a_demo_or_test_accounts_learning_goes_with_it(name, cols):
    rid = _world(name, **cols)
    models.delete_restaurant(rid)
    assert _q("SELECT COUNT(*) FROM intel_rec_events")[0][0] == 0
    assert _q("SELECT COUNT(*) FROM learning_tombstones")[0][0] == 0


def test_a_converted_demo_keeps_only_what_it_recorded_after_learning_since():
    rid = _world("Bella's Bistro", learning_since="2026-09-05 00:00:00")
    models.delete_restaurant(rid)
    kept = _q("SELECT rec_kind FROM intel_rec_events")
    assert kept == [("overtime_move",)], "the demo-era answer (8/1/26) was not kept"
    assert _q("SELECT week FROM intel_features") == [("2026-W37",)]


def test_the_policy_is_written_where_the_routine_is():
    import inspect
    doc = inspect.getdoc(models.delete_restaurant)
    assert "tombstoned id" in doc and "own data always goes" in doc
    assert "NOT called" not in doc
