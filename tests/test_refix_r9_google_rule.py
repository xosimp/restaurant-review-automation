"""Memory re-audit fix round (9/29/26), R9 — "google_rule" (PLATFORM-5, -6).

  * review_derived is judged first from the episode's DECLARED evidence
    sources (rec_instances.evidence_sources): dish_praise (topic
    "marketing", evidence "reviews") and a dish_promote that states a
    praise count are review-derived, so a Google-connected restaurant's
    answers to them never enter pooled learning.
  * The rule is versioned: when it changes, every intel_rec_events row is
    re-judged once (review_derived and google_data), not only new rows.
"""
import json
import sys

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    from intelligence import jobs
    jobs.invalidate_excluded()
    yield


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute("PRAGMA foreign_keys=OFF")       # a deleted restaurant's kept rows
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _q(sql, args=()):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def test_declared_review_sources_make_a_row_review_derived():
    from intelligence import provenance
    assert provenance.review_derived("dish_praise:brisket")                      # the kind, as an immediate fix
    assert provenance.review_derived("dish_promote:brisket", sources=["marketing", "food", "reviews"])
    assert provenance.review_derived("anything:x", sources='["reviews"]')
    assert not provenance.review_derived("dish_promote:brisket", sources=["marketing", "pos", "sales"])


def test_a_dish_promote_that_states_praise_declares_the_reviews(monkeypatch):
    import marketing_opportunities as mo
    import menu_intelligence
    rid = create_restaurant(Restaurant(name="Dish Co", owner_email="d@x.test"))
    dishes = [{"name": n, "units_sold": u, "margin": m, "action": "promote" if n in ("Brisket", "Ribs") else "keep"}
              for n, u, m in (("Brisket", 10, 20.0), ("Ribs", 12, 19.0), ("Soup", 90, 4.0), ("Salad", 80, 5.0),
                              ("Fries", 70, 3.0))]
    monkeypatch.setattr(menu_intelligence, "dish_scorecard",
                        lambda *a, **k: {"available": True, "has_sales_data": True, "dishes": dishes})
    monkeypatch.setattr(mo, "_sales_days", lambda *a, **k: 28)
    cards = {c["subject"]: c for c in mo.dish_margins(rid, praise=[{"name": "Brisket", "positive_reviews": 3}])}
    assert "reviews" in cards["Brisket"]["sources"], "it states and scores on a praise count"
    assert "reviews" not in cards["Ribs"]["sources"]


def test_a_rule_change_rejudges_every_row_already_written():
    from intelligence import feedback, provenance
    rid = create_restaurant(Restaurant(name="Google Co", owner_email="g@x.test"))
    _x("INSERT INTO rec_instances (rec_id, restaurant_id, key, kind, evidence_sources) "
       "VALUES ('a' || hex(randomblob(15)), ?, 'dish_promote:brisket', 'dish_promote', '[\"marketing\", \"reviews\"]')",
       (rid,))
    ep = _q("SELECT rec_id FROM rec_instances")[0]["rec_id"]
    # Written under the old rule: not review-derived, so pooled.
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, rec_id, review_derived, google_data, event_at) "
       "VALUES (?, 'dish_promote', ?, 'accepted', ?, 0, 0, datetime('now'))", (rid, f"dish_promote:brisket#e{ep}", ep))
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, review_derived, google_data, event_at) "
       "VALUES (?, 'dish_praise', 'dish_praise:ribs', 'accepted', 0, 0, datetime('now'))", (rid,))
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, review_derived, google_data, event_at) "
       "VALUES (?, 'trim_day', 'trim_day:tue', 'accepted', 0, 0, datetime('now'))", (rid,))
    _x("INSERT INTO job_cursors (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
       (provenance.RULE_MARK,))
    feedback.sync(labels={rid: {"cohort": None, "partitions": {}, "google": True}})
    rows = {r["rec_kind"]: (r["review_derived"], r["google_data"])
            for r in _q("SELECT rec_kind, review_derived, google_data FROM intel_rec_events")}
    assert rows == {"dish_promote": (1, 1), "dish_praise": (1, 1), "trim_day": (0, 0)}
    assert _q("SELECT value FROM job_cursors WHERE key=?", (provenance.RULE_MARK,))[0]["value"] == \
        str(provenance.RULE_VERSION)


def test_a_deleted_restaurants_row_that_becomes_review_derived_is_held_out():
    from intelligence import feedback, provenance
    _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, review_derived, google_data, event_at) "
       "VALUES (-9, 'dish_praise', 'dish_praise:ribs', 'accepted', 0, 0, datetime('now'))")
    feedback.sync(labels={})
    r = _q("SELECT review_derived, google_data FROM intel_rec_events WHERE restaurant_id=-9")[0]
    assert (r["review_derived"], r["google_data"]) == (1, 1)
