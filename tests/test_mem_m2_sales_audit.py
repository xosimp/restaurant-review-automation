"""Memory audit 9/29/26 (workstream M2, sales_audit): the in-person audit
becomes the account's founding memory when it is linked — the owner's own
answers as facts sourced "Sales audit M/D/YY" (visible, forgettable,
private to the account holders), the targets they gave as goals the owner
confirms — once.
"""
import pytest

import goals
import models
import owner_memory
import promise
import sales_audits
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import metrics
    for mod in (models, sales_audits, goals, metrics):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    sales_audits.init_sales_audits(db_path=db_path)
    import ops
    ops._claim_fallback.clear()
    yield


def _audit():
    return sales_audits.create_audit(answers={
        "restaurant_name": "Simple EJ's", "owner_name": "Erik", "owner_email": "erik@private.test",
        "owner_phone": "555-0100",
        "lab_frustration": "Managers overstaff Tuesday lunch every week",
        "food_purchasing": "Sysco twice a week, Fresh Co for produce on Fridays",
        "pri_improve_year": "Be the place people come to watch the game",
        "lab_target_pct": 28, "food_target_pct": "29",
        "pri_necessity": "Save me an hour a day",
    }, audit_date="2026-09-08")
    return a["id"]


def test_linking_an_audit_seeds_the_owners_own_answers_as_attributed_private_facts():
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@x.test"))
    promise.link(_audit(), rid)
    facts = {f["fact"]: f for f in models.get_ask_memory(rid)}
    f = facts["The labor problem that frustrates them most: Managers overstaff Tuesday lunch every week"]
    assert f["source"] == "Sales audit 9/8/26" and f["origin"] == "audit" and f["audience"] == "principals"
    assert f["modules"] == "labor" and f["author_label"] == "Sales audit 9/8/26"
    assert "How purchasing works: Sysco twice a week, Fresh Co for produce on Fridays" in facts
    aim = facts["What they are trying to improve this year: Be the place people come to watch the game"]
    assert aim["kind"] == "goal"
    joined = " ".join(facts)
    assert "erik@private.test" not in joined and "555-0100" not in joined, "no contact details"
    assert "hour a day" not in joined, "nothing about selling to them"


def test_the_targets_they_gave_become_goals_the_owner_confirms():
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@x.test"))
    promise.link(_audit(), rid)
    props = {g["metric"]: g for g in goals.proposed(rid)}
    assert props["labor_pct"]["target"] == 28 and props["food_cost_pct"]["target"] == 29
    assert props["labor_pct"]["source"] == "audit" and goals.progress(rid) == []
    assert owner_memory.target_for(rid, "labor_pct") is None, "nothing judges against it until confirmed"


def test_a_relink_never_seeds_twice_or_brings_back_a_forgotten_fact():
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@x.test"))
    aid = _audit()
    promise.link(aid, rid)
    n = len(models.get_ask_memory(rid))
    fact = "How purchasing works: Sysco twice a week, Fresh Co for produce on Fridays"
    owner_memory.forget(rid, fact)
    promise.link(aid, rid)
    assert len(models.get_ask_memory(rid)) == n - 1
    assert len(goals.proposed(rid)) == 2
