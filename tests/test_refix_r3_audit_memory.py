"""Memory re-audit 9/29/26, workstream R3 — audit_memory (QUALITY-20,
FORGET-14).

seed_from_audit claimed its once-ever marker before reading the audit, so a
transient failure lost the founding memory for good; up to 22 answers plus
the insights went into the 30-slot context lane, evicting the owner's facts
(or the audit's own) on the day; and point-in-time observations were kept as
current forever.
"""
from datetime import date, timedelta

import pytest

import goals
import models
import ops
import owner_memory
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
    ops._claim_fallback.clear()
    yield


def _full_audit():
    answers = {qid: f"answer to {qid} with some words" for qid, _w, _m in owner_memory.AUDIT_FACT_QUESTIONS}
    answers.update({"restaurant_name": "Simple EJ's", "owner_name": "Erik"})
    return sales_audits.create_audit(answers=answers, audit_date=date.today().isoformat())


def test_a_failed_read_does_not_spend_the_once_ever_marker(monkeypatch):
    rid = create_restaurant(Restaurant(name="Seed Co", owner_email="s@x.test"))
    aid = _full_audit()
    real = sales_audits.get_audit
    monkeypatch.setattr(sales_audits, "get_audit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    out = owner_memory.seed_from_audit(aid, rid)
    assert out["skipped"].startswith("audit unreadable") and models.get_ask_memory(rid) == []
    monkeypatch.setattr(sales_audits, "get_audit", real)
    out = owner_memory.seed_from_audit(aid, rid)
    assert out["facts"] == len(owner_memory.AUDIT_FACT_QUESTIONS)
    assert owner_memory.seed_from_audit(aid, rid)["skipped"] == "already seeded from this audit"


def test_the_audit_has_its_own_lane_and_never_evicts_the_owners_context():
    rid = create_restaurant(Restaurant(name="Seed Co", owner_email="s@x.test"))
    owner = {"id": 101, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"}
    for i in range(models.ASK_MEMORY_CAPS["context"]):
        owner_memory.remember(rid, f"Owner background note {i}", user=owner)
    owner_memory.seed_from_audit(_full_audit(), rid)
    live = {f["fact"] for f in models.get_ask_memory(rid)}
    assert all(f"Owner background note {i}" in live for i in range(models.ASK_MEMORY_CAPS["context"]))
    assert sum(1 for f in models.get_ask_memory(rid) if f["origin"] == "audit") == \
        len(owner_memory.AUDIT_FACT_QUESTIONS)
    assert models.get_ask_memory_archive(rid) == []


def test_observations_get_a_review_date_and_stated_aims_do_not():
    rid = create_restaurant(Restaurant(name="Seed Co", owner_email="s@x.test"))
    owner_memory.seed_from_audit(_full_audit(), rid)
    facts = {f["fact"].split(":", 1)[0]: f for f in models.get_ask_memory(rid)}
    complaint = facts["Top guest complaints at the audit"]
    assert complaint["valid_until"] == (date.today() + timedelta(days=owner_memory.AUDIT_REVIEW_DAYS)).isoformat()
    assert facts["Their top concerns"]["valid_until"] is None
    # past its review date it leaves, and Account asks whether it still holds
    owner_memory.expire(rid, today=date.today() + timedelta(days=owner_memory.AUDIT_REVIEW_DAYS + 1))
    owner = {"id": 101, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"}
    left = owner_memory.account_view(rid, owner)["archived"]
    assert left and all(a["reason_label"] == "its review date passed — put it back if it still holds" for a in left)
