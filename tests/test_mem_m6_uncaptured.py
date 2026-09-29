"""Memory audit 9/29/26, uncaptured (workstream M6's share): an owner re-tag
action whose corrections become analyser examples, with the menu as the dish
vocabulary; review requests matched to the reviews they produced. (The
marketing-side signal — a regenerated draft counted as a "no" — is in
tests/test_mem_m6_mkt_edits.py; who takes covers, promotions and staff
mentions are workstream M3's.)"""
import json
import sqlite3
import types
from datetime import datetime, timedelta

import pytest
from flask import Flask

import analyser
import marketing
import memory_context
import models
import review_signals as rs
import strategy_routes
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name="Tag Co", owner_email="t@x.test", **kw), db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


_N = [0]


def _review(db_path, rid, text="The wait was long and the pasta was cold", rating=2, author="Maria G.",
            categories='["wait_time"]', days_ago=1):
    _N[0] += 1
    c = _conn(db_path)
    cur = c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                    "sentiment, categories, severity, entities, review_date, fetched_at) "
                    "VALUES (?,?,?,?,?,?,1,'negative',?,'service',?,datetime('now', ?),datetime('now'))",
                    (rid, "google", f"t{_N[0]}", author, rating, text, categories, json.dumps({"dishes": ["pasta"]}),
                     f"-{int(days_ago)} days"))
    c.commit()
    rv = cur.lastrowid
    c.close()
    return rv


OWNER = {"id": 11, "role": "client"}
ADMIN = {"id": 11, "role": "client", "acting_admin_id": 99, "device_type": "admin-view-as"}


# ── re-tags ──────────────────────────────────────────────────────────────────

def test_an_owner_re_tag_corrects_the_review_and_is_kept_with_who_made_it(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid)
    out = rs.retag(rid, rv, {"categories": ["food_quality"], "severity": "operational",
                             "dishes": ["Carbonara"]}, user=OWNER)
    assert out["ok"] and sorted(out["changed"]) == ["categories", "dishes", "severity"]
    c = _conn(db_path)
    row = c.execute("SELECT categories, severity, entities FROM reviews WHERE id=?", (rv,)).fetchone()
    kept = [dict(r) for r in c.execute("SELECT field, before_json, after_json, authority, user_id FROM review_retags "
                                       "ORDER BY field")]
    c.close()
    assert json.loads(row["categories"]) == ["food_quality"] and row["severity"] == "operational"
    assert json.loads(row["entities"])["dishes"] == ["carbonara"]
    assert kept[0] == {"field": "categories", "before_json": '["wait_time"]', "after_json": '["food_quality"]',
                       "authority": "principal", "user_id": 11}


def test_a_re_tag_outside_the_analysers_vocabulary_is_refused(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid)
    assert "categories must be" in rs.retag(rid, rv, {"categories": ["vibes"]}, user=OWNER)["error"]
    assert rs.retag(rid, 99999, {"sentiment": "positive"}, user=OWNER)["error"] == "Review not found."
    assert rs.retag(rid + 1, rv, {"sentiment": "positive"}, user=OWNER)["error"] == "Review not found."


def test_the_retag_route_is_on_both_surfaces(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid)
    u = dict(OWNER, restaurant_id=rid)
    with Flask(__name__).test_request_context("/", method="POST", json={"sentiment": "neutral"}):
        out, st = strategy_routes._do_review_retag(u, rv)
    assert st == 200 and out["changed"] == ["sentiment"]
    paths = {p for p, _m, fn, _e in strategy_routes._ROUTES if fn is strategy_routes._do_review_retag}
    assert paths == {"/reviews/<int:review_id>/retag"}


def _msg(obj):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(obj))],
                                 stop_reason="end_turn")


ANALYSIS = {"sentiment": "negative", "categories": ["wait_time"], "summary": "Slow service", "urgency": "normal",
            "severity": "service", "specific_complaint": "slow", "entities": {"dishes": ["the carbonara"]}}


def _stub(monkeypatch, result=ANALYSIS):
    seen = []
    monkeypatch.setattr(analyser, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(analyser, "create_with_retry", lambda *a, **k: seen.append(k["messages"][0]["content"])
                        or _msg(result))
    return seen


def _dish(db_path, rid, name):
    c = _conn(db_path)
    c.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, name))
    c.commit()
    c.close()


def test_the_analyser_reads_the_owners_corrections_and_the_menu(db_path, monkeypatch):
    import ai_guard
    rid = _rid(db_path)
    _dish(db_path, rid, "Carbonara Pasta")
    fixed = _review(db_path, rid, text="Took forever to get our carbonara, the host ignored us")
    rs.retag(rid, fixed, {"categories": ["service", "wait_time"]}, user=OWNER)
    admin_fixed = _review(db_path, rid, text="An admin's correction must not teach anything")
    rs.retag(rid, admin_fixed, {"categories": ["value"]}, user=ADMIN)
    seen = _stub(monkeypatch)
    rv = _review(db_path, rid)
    analyser.analyse_review(rv, 2, "Waited an hour for the carbonara", restaurant_id=rid)
    p = seen[-1]
    assert "THIS RESTAURANT'S CORRECTIONS" in p and "Took forever to get our carbonara" in p
    assert "The owner's tags: categories: service, wait_time" in p and ai_guard.UNTRUSTED_OPEN in p
    assert "An admin's correction" not in p
    assert "This restaurant's menu" in p and "Carbonara Pasta" in p


def test_a_dish_the_guest_names_is_stored_as_the_one_menu_dish_it_is(db_path, monkeypatch):
    rid = _rid(db_path)
    _dish(db_path, rid, "Carbonara Pasta")
    _dish(db_path, rid, "Rigatoni")
    _stub(monkeypatch)
    rv = _review(db_path, rid)
    analyser.analyse_review(rv, 2, "Waited an hour for the carbonara", restaurant_id=rid)
    c = _conn(db_path)
    ents = json.loads(c.execute("SELECT entities FROM reviews WHERE id=?", (rv,)).fetchone()[0])
    c.close()
    assert ents["dishes"] == ["carbonara pasta"] and ents["dishes_said"] == ["the carbonara"]


def test_the_menu_mapping_never_guesses_between_two_dishes():
    menu = ["Carbonara Pasta", "Pesto Pasta", "Tiramisu"]
    assert rs.map_dishes(["the carbonara", "pasta", "tiramisu", "gelato"], menu) == \
        ["carbonara pasta", "pasta", "tiramisu", "gelato"]


def test_a_re_analysis_keeps_the_owners_correction(db_path, monkeypatch):
    rid = _rid(db_path)
    rv = _review(db_path, rid)
    rs.retag(rid, rv, {"categories": ["food_quality"], "sentiment": "neutral"}, user=OWNER)
    _stub(monkeypatch)
    out = analyser.analyse_review(rv, 2, "The wait was long and the pasta was cold", restaurant_id=rid)
    assert out["categories"] == ["food_quality"] and out["sentiment"] == "neutral"
    c = _conn(db_path)
    row = c.execute("SELECT categories, sentiment FROM reviews WHERE id=?", (rv,)).fetchone()
    c.close()
    assert json.loads(row["categories"]) == ["food_quality"] and row["sentiment"] == "neutral"


def test_a_demo_accounts_corrections_are_not_examples(db_path):
    rid = _rid(db_path, is_demo=1)
    rv = _review(db_path, rid)
    rs.retag(rid, rv, {"categories": ["value"]}, user=OWNER)
    assert rs.retag_examples(rid) == []


# ── review requests matched to their reviews ─────────────────────────────────

def _request(db_path, rid, name, days_ago):
    c = _conn(db_path)
    cur = c.execute("INSERT INTO review_requests (restaurant_id, customer_name, customer_email, sent_at) "
                    "VALUES (?,?,?,datetime('now', ?))", (rid, name, f"{name.split()[0].lower()}@x.test",
                                                          f"-{int(days_ago)} days"))
    c.commit()
    rq = cur.lastrowid
    c.close()
    return rq


def test_a_review_request_is_matched_to_the_review_it_produced_one_to_one(db_path):
    rid = _rid(db_path)
    maria = _request(db_path, rid, "Maria Garcia", 5)
    rv = _review(db_path, rid, author="Maria G.", days_ago=3)
    tom = _request(db_path, rid, "Tom Baker", 30)
    _review(db_path, rid, author="Tom B.", days_ago=2)                       # 28 days later: outside the window
    ann1, ann2 = _request(db_path, rid, "Ann Lee", 6), _request(db_path, rid, "Ann Smith", 4)
    _review(db_path, rid, author="Ann", days_ago=1)                          # two requests could be it: neither
    assert rs.match_review_requests(rid) == 1
    c = _conn(db_path)
    got = {r["id"]: r["review_id"] for r in c.execute("SELECT id, review_id FROM review_requests")}
    c.close()
    assert got == {maria: rv, tom: None, ann1: None, ann2: None}
    assert rs.match_review_requests(rid) == 0                                # idempotent


def test_the_request_stats_carry_a_conversion_over_closed_windows(db_path):
    rid = _rid(db_path)
    for i in range(10):
        rq = _request(db_path, rid, f"Guest{i} Last", 20)
        if i < 3:
            c = _conn(db_path)
            c.execute("UPDATE review_requests SET review_id=? WHERE id=?", (1000 + i, rq))
            c.commit()
            c.close()
    _request(db_path, rid, "Recent Guest", 2)                                # window still open: not counted
    conv = models.get_review_request_stats(rid)["conversion"]
    assert (conv["asked"], conv["reviewed"], conv["pct"]) == (10, 3, 30.0)
    line = [l["text"] for l in marketing.memory_lines(memory_context.MemoryRequest(restaurant_id=rid, surface="ask"))
            if "Review requests" in l["text"]]
    assert line == ["Review requests: 3 of 10 guests asked left a review within 14 days (30%; matched by the "
                    "guest's name)."]


def test_below_ten_closed_requests_no_conversion_is_said(db_path):
    rid = _rid(db_path)
    for i in range(4):
        _request(db_path, rid, f"Guest{i} Last", 20)
    assert models.get_review_request_stats(rid)["conversion"]["pct"] is None


def test_the_fetch_matches_requests_when_new_reviews_arrive():
    import inspect
    import scheduler
    src = inspect.getsource(scheduler.run_daily_fetch)
    assert "review_signals.match_review_requests(rid)" in src
