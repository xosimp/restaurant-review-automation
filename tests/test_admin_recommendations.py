"""The internal Recommendation Acceptance figures (admin_ops): ignored
recommendations stay in the denominator, the score waits for enough of
them, and taken-then-improved is measured against what was taken."""
import sys

import pytest

import admin_ops
import models
import rec_ledger
from models import create_restaurant, Restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def test_ignored_counts_in_the_denominator_and_ras_waits_for_twenty(db):
    rid = create_restaurant(Restaurant(name="Rec Co", owner_email="r@x.com"), db_path=db)
    rec_ledger.present_many(rid, [dict(key=f"k:{i}", module="labor", kind="k", dollar_value=100 if i < 2 else None)
                                  for i in range(4)], "home")
    rec_ledger.record(rid, "k:0", "accepted")
    rec_ledger.record(rid, "k:0", "outcome", meta={"verdict": "improved"})
    rec_ledger.record(rid, "k:1", "dismissed", meta={"kind": "hide"})
    c = models.get_conn(db)
    c.execute("UPDATE rec_instances SET last_event_at=datetime('now','-20 days') WHERE key='k:3'")
    c.commit(); c.close()
    d = admin_ops.recommendation_acceptance(days=30)
    t = d["total"]
    assert t["n"] == 4 and t["accepted"] == 1 and t["dismissed"] == 1 and t["ignored"] == 1
    assert t["accept_rate"] == 0.25 and t["outcome_rate"] == 1.0
    assert t["ras"] is None                                  # 4 < 20
    dollars = {r["group"]: r for r in d["by_dollars"]}
    assert dollars["has a $ figure"]["accept_rate"] == 0.5 and dollars["no $ figure"]["accept_rate"] == 0.0


def test_the_score_appears_at_twenty(db):
    rid = create_restaurant(Restaurant(name="Rec Co", owner_email="r@x.com"), db_path=db)
    rec_ledger.present_many(rid, [dict(key=f"k:{i}", module="reviews") for i in range(20)], "home")
    for i in range(10):
        rec_ledger.record(rid, f"k:{i}", "accepted")
    d = admin_ops.recommendation_acceptance(days=30, restaurant_id=rid)
    # 100 × (0.15 × 0.5 + 0.40 × 0.5 + 0 + 0) = 27.5
    assert d["total"]["ras"] == 27.5
    lo, hi = d["total"]["accept_ci90"]
    assert lo < 0.5 < hi
