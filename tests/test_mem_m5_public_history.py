"""Memory fix round M5, public_history (memory audit 9/29/26, FORGET-9).

The restaurant's own Google rating was UPDATEd in place and its competitors'
openings, closures and moves lived only in a snapshot table pruned at a year
and read over 60 days. Now: own_rating_history (one row per week), and
market_events plus a monthly competitor rating series — all kept forever,
written by every path that writes the figures, and backfilled at boot from
what is still on file.
"""
from datetime import date

import pytest

import event_memory as em
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Rating Co", place="place-own"):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                       google_place_id=place))
    return rid


def test_the_own_rating_keeps_one_row_a_week_and_a_trajectory():
    rid = _rid()
    em.record_own_rating(rid, 4.3, 210, source="places", at=date(2026, 3, 2))
    em.record_own_rating(rid, 4.4, 215, source="places", at=date(2026, 3, 4))     # same ISO week: replaced
    em.record_own_rating(rid, 4.6, 380, source="business_profile", at=date(2026, 9, 28))
    t = em.own_rating_trajectory(rid)
    assert t["available"] and t["weeks"] == 2
    assert t["first"]["rating"] == 4.4 and t["latest"]["rating"] == 4.6 and t["change"] == 0.2
    em.record_own_rating(rid, None)                               # nothing measured, nothing kept
    em.record_own_rating(rid, 0)
    assert em.own_rating_trajectory(rid)["weeks"] == 2


def test_both_writers_of_the_google_rating_keep_its_history(monkeypatch):
    import competitor
    import gmb
    rid = _rid()
    competitor._remember_own_listing("place-own", ["restaurant"], 2, rating=4.5, rating_count=300)
    conn = models.get_conn()
    rows = conn.execute("SELECT rating, review_count, source FROM own_rating_history WHERE restaurant_id=?",
                        (rid,)).fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [(4.5, 300, "places")]

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"rating": 4.7, "userRatingCount": 320}
    monkeypatch.setattr(gmb.requests, "get", lambda *a, **k: _R())
    assert gmb.fetch_location_rating(rid, "token", "locations/1")["ok"]
    conn = models.get_conn()
    row = conn.execute("SELECT rating, review_count, source FROM own_rating_history WHERE restaurant_id=?",
                       (rid,)).fetchone()
    conn.close()
    assert tuple(row) == (4.7, 320, "business_profile")         # the same week: the latest reading


def _comp(pid, name, rating, count=100):
    return {"place_id": pid, "name": name, "rating": rating, "review_count": count}


def test_market_events_mark_arrivals_departures_and_moves_that_add_up():
    rid = _rid()
    first = em.record_market_snapshot(rid, [_comp("a", "Bella's", 4.5), _comp("b", "Tony's", 4.0)],
                                      at=date(2026, 3, 2))
    assert first == []                                             # the set a restaurant starts with
    # An opening is a place never tracked with few reviews; an established
    # place the week's search ranks in is not one (owner, 9/29/26).
    got = em.record_market_snapshot(rid, [_comp("a", "Bella's", 4.4), _comp("b", "Tony's", 4.1),
                                          _comp("c", "Sal's", 4.8, count=20), _comp("e", "Old Faithful", 4.6, 800)],
                                    at=date(2026, 3, 9))
    assert [(e["name"], e["kind"]) for e in got] == [("Sal's", "arrived")]
    # 0.1 a week never crosses 0.2 against the last reading, but it adds up
    # against the first one.
    got = em.record_market_snapshot(rid, [_comp("a", "Bella's", 4.3), _comp("b", "Tony's", 4.1),
                                          _comp("c", "Sal's", 4.8, count=20)], at=date(2026, 3, 16))
    assert [(e["name"], e["kind"], e["from_rating"], e["to_rating"]) for e in got] == \
        [("Bella's", "rating_down", 4.5, 4.3)]
    # Dropping out of the search is not closing: only Google's status is.
    got = em.record_market_snapshot(rid, [_comp("a", "Bella's", 4.3), _comp("c", "Sal's", 4.8, count=20)],
                                    at=date(2026, 3, 30))
    assert [e["kind"] for e in got] == []
    got = em.record_market_snapshot(rid, [_comp("a", "Bella's", 4.3), _comp("c", "Sal's", 4.8, count=20)],
                                    at=date(2026, 4, 6),
                                    closed=[{"place_id": "b", "name": "Tony's", "status": "CLOSED_PERMANENTLY"}])
    assert [(e["name"], e["kind"]) for e in got] == [("Tony's", "gone")]
    hist = em.market_history(rid)
    assert [e["kind"] for e in hist] == ["gone", "rating_down", "arrived"]
    conn = models.get_conn()
    months = conn.execute("SELECT place_id, month, rating FROM competitor_rating_monthly WHERE restaurant_id=? "
                          "ORDER BY place_id, month", (rid,)).fetchall()
    conn.close()
    assert [tuple(m) for m in months] == [("a", "2026-03", 4.3), ("a", "2026-04", 4.3), ("b", "2026-03", 4.1),
                                          ("c", "2026-03", 4.8), ("c", "2026-04", 4.8), ("e", "2026-03", 4.6)]


def test_what_is_on_file_is_replayed_into_the_history_at_boot():
    import ops
    rid = _rid()
    conn = models.get_conn()
    for day, rows in (("2026-01-05", [("a", "Bella's", 4.5), ("b", "Tony's", 4.0)]),
                      ("2026-01-12", [("a", "Bella's", 4.5), ("b", "Tony's", 4.0), ("c", "Sal's", 4.8)])):
        for pid, name, rating in rows:
            conn.execute("INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, review_count, "
                         "captured_at) VALUES (?,?,?,?,?,?)",
                         (rid, pid, name, rating, 20 if pid == "c" else 900, day + " 09:00:00"))
    conn.execute("UPDATE restaurants SET gbp_rating=4.4, gbp_review_count=250, "
                 "gbp_rating_updated_at='2026-09-20T10:00:00+00:00' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    em.backfill_public_history()
    hist = em.market_history(rid)
    assert [(e["name"], e["kind"], e["observed_on"]) for e in hist] == [("Sal's", "arrived", "2026-01-12")]
    assert em.own_rating_trajectory(rid)["series"][0]["rating"] == 4.4
    em.backfill_public_history()                                   # a restaurant with a history is left alone
    assert len(em.market_history(rid)) == 1
    for table in ("own_rating_history", "market_events", "competitor_rating_monthly", "weather_daily",
                  "event_outcomes", "event_effects"):
        assert table not in ops._RETENTION_DAYS, f"{table} is kept forever"


def test_a_competitor_check_keeps_the_market_history():
    import inspect
    import competitor
    # The storing moved into _store_analysis (AI cost audit 10/7/26 #59: a
    # batched read lands there too).
    src = inspect.getsource(competitor._store_analysis)
    assert "event_memory.record_market_snapshot(restaurant_id, competitors, closed=_closed, at=_now_ct)" in src
    # The closure check also takes the statuses already read (AI cost audit
    # 10/7/26 #41), so the call continues past _closed_custom.
    assert "_closures_among_dropped(restaurant_id, competitors, _closed_custom," in src


def test_churn_events_the_old_rule_wrote_are_retracted():
    rid = _rid()
    conn = models.get_conn()
    for pid, kind, count, on in (("x", "gone", 300, "2026-09-29"), ("y", "arrived", 900, "2026-09-29"),
                                 ("z", "arrived", 25, "2026-09-29"), ("w", "gone", 300, "2026-10-06")):
        conn.execute("INSERT INTO market_events (restaurant_id, place_id, name, kind, review_count, observed_on) "
                     "VALUES (?,?,?,?,?,?)", (rid, pid, pid, kind, count, on))
    conn.commit()
    conn.close()
    assert em.retract_churn_events() == 2
    assert sorted(e["place_id"] for e in em.market_history(rid)) == ["w", "z"]


def test_the_movement_payload_carries_the_market_history_and_the_rating_trajectory():
    import inspect
    import client_api
    import mobile_api
    src = inspect.getsource(mobile_api.mobile_intel_movement)
    assert "market_history=event_memory.market_history(rid)[:MARKET_HISTORY_MAX]" in src
    assert "own_rating_history=event_memory.own_rating_trajectory(rid)" in src
    # The web route is the same body.
    assert '_m("mobile_intel_movement")' in inspect.getsource(client_api.intel_movement)


def test_only_a_dropout_google_calls_closed_is_a_closure(monkeypatch):
    import competitor
    rid = _rid()
    conn = models.get_conn()
    for pid, name in (("a", "Stays"), ("b", "Closed Co"), ("c", "Still Open")):
        conn.execute("INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, review_count, "
                     "captured_at) VALUES (?,?,?,?,?,?)", (rid, pid, name, 4.2, 300, "2026-09-22 09:00:00"))
    conn.commit()
    conn.close()
    asked = []

    class _R:
        def __init__(self, status):
            self.status = status

        def json(self):
            return {"status": "OK", "result": {"name": "x", "business_status": self.status}}

    def fake(endpoint, params, **kw):
        asked.append(params["place_id"])
        return _R("CLOSED_PERMANENTLY" if params["place_id"] == "b" else "OPERATIONAL")
    monkeypatch.setattr(competitor, "_places_request", fake)
    got = competitor._closures_among_dropped(rid, [{"place_id": "a"}])
    assert sorted(asked) == ["b", "c"]                        # only the dropouts are asked about
    assert [g["place_id"] for g in got] == ["b"]
