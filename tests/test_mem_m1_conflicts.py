"""Memory audit 9/29/26, workstream M1 — conflicts.

Contradictory advice from different modules is reconciled where cards are
assembled (lever_conflicts): "Trim Tuesday" against "Fill Tuesday" (on this
surface or another), a reprice on a dish guests call poor value, a
promotion of a dish whose ingredient is critically low. The weaker card is
annotated; the owner's choice is stored as a decision and settles the same
conflict the same way next time. Reprice reads the scorecard's value
complaints to lower its confidence.
"""
import json

import pytest

import models
import rec_ledger as rl
import lever_conflicts as lc
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    lc._FACTS_CACHE.clear()
    yield


def _rid(db_path, name="Conflict Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)


NO_FACTS = {"value": {}, "low": {}}


def _cards():
    return [{"key": "trim_day:Tuesday", "title": "Trim Tuesday staffing", "rank_score": 300},
            {"key": "slow_day:Tuesday", "title": "Fill Tuesday with a text to regulars", "rank_score": 120},
            {"key": "reprice:Short Rib", "title": "Reprice Short Rib to $34.25", "rank_score": 200},
            {"key": "dish_promote:Salmon Special", "title": "Promote the Salmon Special", "rank_score": 90}]


def test_the_map_finds_each_rule():
    fx = {"value": {"short rib": {"n": 2, "sample": "overpriced for the size"}},
          "low": {"salmon special": ["Salmon"]}}
    found = {c["rule"]: c for c in lc.find(_cards(), fx)}
    assert set(found) == {"trim_vs_fill", "reprice_vs_value", "promote_vs_stock"}
    assert found["trim_vs_fill"]["weaker"] == "slow_day:Tuesday"          # the lower-ranked card
    assert found["reprice_vs_value"]["a"] == "reprice:Short Rib" and "poor value in 2 reviews" in \
        found["reprice_vs_value"]["why"]
    assert "Salmon" in found["promote_vs_stock"]["why"] and "critically low" in found["promote_vs_stock"]["why"]


def test_the_weaker_card_is_annotated_and_the_choice_settles_it_next_time(db_path):
    rid = _rid(db_path)
    cards = lc.apply(rid, [dict(c) for c in _cards()[:2]], NO_FACTS, db_path=db_path, others=[])
    fill = next(c for c in cards if c["key"] == "slow_day:Tuesday")
    assert fill["conflict"]["rule"] == "trim_vs_fill" and fill["conflict"]["with"] == "Trim Tuesday staffing"
    assert {o["signature"] for o in fill["conflict"]["choose"]} == {"labor:day:tuesday",
                                                                     "guest_outreach:day:tuesday"}
    assert fill["conflict"]["route"]["web"] == "/api/recs/conflict"
    # The owner keeps the fill: the trim is held from now on.
    out = lc.record_choice(rid, fill["conflict"]["id"], "guest_outreach:day:tuesday", user={"id": 1}, db_path=db_path)
    assert out["ok"]
    held = []
    kept = lc.apply(rid, [dict(c) for c in _cards()[:2]], NO_FACTS, db_path=db_path, held_out=held, others=[])
    assert [c["key"] for c in kept] == ["slow_day:Tuesday"] and held[0]["held_key"] == "trim_day:Tuesday"
    # The choice is a decision, kept out of every acceptance figure.
    assert not rl.counts_in_acceptance("conflict:" + fill["conflict"]["id"])


def test_another_surfaces_live_card_is_a_conflict_here(db_path):
    rid = _rid(db_path)
    rl.present(rid, "slow_day:Tuesday", "marketing", "marketing", title="Fill Tuesday", db_path=db_path)
    cards = lc.apply(rid, [{"key": "trim_day:Tuesday", "title": "Trim Tuesday staffing", "rank_score": 500}],
                     NO_FACTS, db_path=db_path)
    assert cards[0]["conflict"]["rule"] == "trim_vs_fill" and cards[0]["conflict"]["with"] == "Fill Tuesday"


def test_hold_on_a_card_against_a_fact(db_path):
    rid = _rid(db_path)
    fx = {"value": {"short rib": {"n": 3, "sample": "not worth it"}}, "low": {}}
    card = lc.apply(rid, [{"key": "reprice:Short Rib", "title": "Reprice Short Rib", "rank_score": 1}], fx,
                    db_path=db_path, others=[])[0]
    lc.record_choice(rid, card["conflict"]["id"], "hold", db_path=db_path)
    assert lc.apply(rid, [{"key": "reprice:Short Rib", "title": "Reprice Short Rib"}], fx, db_path=db_path,
                    others=[]) == []
    # "Keep it" keeps it, with no question asked again.
    lc.record_choice(rid, card["conflict"]["id"], "pricing:dish:short rib", db_path=db_path)
    kept = lc.apply(rid, [{"key": "reprice:Short Rib", "title": "Reprice Short Rib"}], fx, db_path=db_path, others=[])
    assert kept and not kept[0].get("conflict")


def test_value_complaints_come_from_the_reviews(db_path):
    rid = _rid(db_path)
    c = models.get_conn(db_path)
    for i, (sent, complaint) in enumerate((("negative", "way overpriced for a small portion"),
                                           ("negative", "cold"), ("positive", None))):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, sentiment, "
                  "entities, specific_complaint, processed, review_date, fetched_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,1, date('now','-3 days'), datetime('now'))",
                  (rid, "google", f"v{i}", "G", 2, "x", sent, json.dumps({"dishes": ["short rib"]}), complaint))
    c.commit()
    c.close()
    import menu_intelligence
    monkey = menu_intelligence.get_conn
    try:
        menu_intelligence.get_conn = lambda *a, **k: models.get_conn(db_path)
        fx = lc.facts_uncached(rid, db_path=db_path, critical_low=[])
    finally:
        menu_intelligence.get_conn = monkey
    assert fx["value"]["short rib"]["n"] == 1                               # the value complaint only


def test_the_owners_choice_route_is_on_web_and_phone():
    import strategy_routes as sr
    paths = {p for p, _m, _f, _e in sr._ROUTES}
    assert "/recs/conflict" in paths and "/data-health/verify" in paths and "/actions/<int:action_id>/why" in paths
