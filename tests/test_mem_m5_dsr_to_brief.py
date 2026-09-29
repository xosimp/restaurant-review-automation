"""Memory fix round M5, dsr_to_brief (memory audit 9/29/26, CROSS-7).

The 11pm report said "Tomorrow: call in a second cook for the 60-cover
party; reorder salmon", and the 7am brief — the owner's one read before
service — said "Today looks like a typical Saturday: about $9,800", with no
party, no time-off note and a "one thing" picked by another ranking.
dsr.memory.morning_carry hands the brief what the report said about TODAY
(its forecast and confidence, its Tomorrow items and predictions, its
unanswered priorities under the report's own keys), redacted for the
viewer, and only for the date the report named.
"""
import sys
from datetime import date, timedelta

import pytest

import dsr
import models
import pos
from dsr import memory, store
from models import Restaurant, create_restaurant, get_restaurant

SAT = date(2026, 9, 26)
FRI = SAT - timedelta(days=1)
OWNER = {"id": 1, "role": "client", "is_admin": 0}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0}
KEY = "dsr_action:adjust_staffing:labor"


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
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


def _report(db, rid, tomorrow_date=SAT):
    r = store.create_report(rid, FRI, trigger="sweep", db_path=db)
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics={"net": 9100.0}), db_path=db)
    store.save_block(r["id"], "labor", dsr.block(dsr.READY, source="rpower", metrics={"pct": 27.0, "cost": 2457.0}),
                     db_path=db)
    store.save_section(r["id"], "tomorrow", {
        "date": tomorrow_date.isoformat(), "weekday": tomorrow_date.strftime("%A"),
        "items": [{"kind": "time_off", "tone": "warn", "text": "2 employees off (Ana, Ben)"},
                  {"kind": "event", "tone": "warn", "text": "60-cover party"},
                  {"kind": "stock", "tone": "warn", "text": "Salmon low (1 day left)"}],
        "scheduled": 14,
        "forecast": {"typical": 9800.0, "low": 8500.0, "high": 11200.0, "samples": 8, "weekday": "Saturday",
                     "effects": [], "text": "$8,500–$11,200", "basis": "the median of the last 8 Saturdays"},
        "confidence": {"pct": 80, "label": "80%"},
        "predictions": [{"key": "sales_range", "text": "Sales between $8,500 and $11,200"},
                        {"key": "sales_budget", "text": "Sales expected above budget ($9,000)"}],
        "_preds": []}, db_path=db)
    store.save_narrative(r["id"], {
        "executive_summary": {"text": "Net sales were $9,100.", "cites": ["sales.net"]},
        "actions_tomorrow": [
            {"text": "Call in a second cook for the 60-cover party", "why": "Labor ran 27%.",
             "urgency": "before_service", "cites": ["labor.pct"], "key": KEY},
            {"text": "Reorder salmon before Saturday", "why": "Salmon is low.", "urgency": "before_service",
             "cites": ["labor.pct"], "key": "dsr_action:reorder:food/salmon"}]}, db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    return r


def _rid(db):
    return create_restaurant(Restaurant(name="Carry Co", owner_email="carry@x.test"), db_path=db)


def test_the_carry_is_what_the_report_said_about_today(db):
    rid = _rid(db)
    _report(db, rid)
    c = memory.morning_carry(rid, SAT, db_path=db)
    assert c["date"] == "2026-09-26" and c["report_date"] == "2026-09-25"
    assert c["forecast"]["typical"] == 9800.0 and c["confidence"]["pct"] == 80
    assert [i["text"] for i in c["items"]][:2] == ["2 employees off (Ana, Ben)", "60-cover party"]
    assert [p["text"] for p in c["predictions"]][0] == "Sales between $8,500 and $11,200"
    assert [a["key"] for a in c["actions"]] == [KEY, "dsr_action:reorder:food/salmon"]


def test_only_for_the_date_the_report_named(db):
    rid = _rid(db)
    _report(db, rid, tomorrow_date=SAT + timedelta(days=1))
    assert memory.morning_carry(rid, SAT, db_path=db) is None
    assert memory.morning_carry(rid, SAT + timedelta(days=2), db_path=db) is None       # no report of the night before


def test_an_answer_anywhere_silences_a_carried_priority(db):
    import rec_ledger
    rid = _rid(db)
    _report(db, rid)
    rec_ledger.present_many(rid, [{"key": KEY, "module": "labor", "title": "Call in a second cook"}], "dsr",
                            db_path=db)
    rec_ledger.record(rid, KEY, "completed", surface="dsr", db_path=db)
    c = memory.morning_carry(rid, SAT, db_path=db)
    assert [a["key"] for a in c["actions"]] == ["dsr_action:reorder:food/salmon"]


def test_a_manager_reads_the_carry_as_the_report_shows_them(db):
    rid = _rid(db)
    _report(db, rid)
    c = memory.morning_carry(rid, SAT, viewer=dict(MANAGER, restaurant_id=rid), db_path=db)
    assert not [p for p in c["predictions"] if "budget" in p["text"].lower()]
    assert c["view"] == "manager"


def test_the_brief_reads_today_from_the_report_instead_of_recomputing(db):
    import morning_brief
    rid = _rid(db)
    _report(db, rid)
    r = get_restaurant(rid, db_path=db)
    brief = morning_brief.build(rid, restaurant=r, today=SAT, db_path=db)
    (today,) = [l for l in brief["lines"] if l["key"] == "today"]
    assert today["source"] == "dsr" and today["text"].startswith("Today, from last night's report: about $9,800")
    assert "$8,500–$11,200" in today["text"] and "held 80% of the time" in today["text"]
    assert "60-cover party" in today["text"] and "2 employees off" in today["text"]
    assert today["predictions"][0] == "Sales between $8,500 and $11,200"
    (act,) = [l for l in brief["lines"] if l["key"] == "dsr_action"]
    assert act["rec"] == KEY and act["rec_key"] == KEY and act["answerable"] is True
    assert act["text"] == "From last night's report: Call in a second cook for the 60-cover party."
    assert not morning_brief.shown_on_home(act)                  # Home's Last night card says it already
    assert "last night's report's priority (an inference)" in morning_brief.footer_source(brief["lines"])
