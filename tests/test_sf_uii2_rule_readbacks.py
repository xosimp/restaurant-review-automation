"""Schedule fix round 10/3/26, UI wave (iOS workstream I2): the owner's
staffing rules said back where they are written and where they are set.

  D-38  an owner-only staffing rule's words reach an account holder only —
        the rules screen read every parsed rule back to every login that
        can see labor, private ones included
  F2-7  the owner-only rules the schedule can't check had no surface:
        Account → Memory now says, beside each staffing rule, how every
        draft checks it, or that it can't (owner-only ones to the owner)
"""
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import models
import owner_memory
import schedule_setup as setup
import strategy_routes
from models import Restaurant, create_restaurant

WEEK = [(date(2026, 10, 12) + timedelta(days=i)).isoformat() for i in range(7)]
OWNER = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0, "username": "dana"}


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
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(setup, "coming_week", lambda rid: list(WEEK))
    yield


def _rid():
    rid = create_restaurant(Restaurant(name="Rules Co", owner_email="r@x.test", timezone="America/Chicago",
                                       module_labor=1))
    for n in ("Ana", "Ben", "Cy"):
        models.add_manual_team_member(rid, n, role="Server")
    return rid


def _rule(rid, text, audience):
    owner_memory.remember(rid, text, kind="constraint", modules=["schedule"], user=dict(OWNER, restaurant_id=rid),
                          audience=audience, audience_chosen=True)


def _call(fn, u, body=None):
    with Flask(__name__).test_request_context(json=body or {}):
        return fn(u)


def test_an_owner_only_staffing_rule_is_read_back_to_an_account_holder_only():
    rid = _rid()
    _rule(rid, "Always two servers on Saturday night", "principals")
    _rule(rid, "At least 1 server every day", "team")
    owner, status = _call(strategy_routes._do_compliance_get, dict(OWNER, restaurant_id=rid))
    assert status == 200
    texts = {r["text"] for r in owner["owner_rules"]}
    assert texts == {"Always two servers on Saturday night", "At least 1 server every day"}
    manager, status = _call(strategy_routes._do_compliance_get, dict(MANAGER, restaurant_id=rid))
    assert status == 200
    assert [r["text"] for r in manager["owner_rules"]] == ["At least 1 server every day"], \
        "the owner-only rule's words never reach a manager"


def test_memory_says_how_the_schedule_checks_each_staffing_rule_and_which_it_cannot():
    rid = _rid()
    _rule(rid, "Always two servers on Saturday night", "principals")
    _rule(rid, "Keep the rotation fair for the new hires", "principals")
    _rule(rid, "Give the new hires a fair rotation please", "team")
    owner, status = _call(strategy_routes._do_memory_list, dict(OWNER, restaurant_id=rid))
    assert status == 200
    assert owner["schedule_reads"] == [{"text": "Always two servers on Saturday night",
                                        "reads_as": "at least 2 Server on Sat at dinner/night"}]
    assert set(owner["schedule_unchecked"]) == {"Keep the rotation fair for the new hires",
                                                "Give the new hires a fair rotation please"}
    manager, status = _call(strategy_routes._do_memory_list, dict(MANAGER, restaurant_id=rid))
    assert status == 200
    assert manager.get("schedule_reads") == []
    assert manager.get("schedule_unchecked") == ["Give the new hires a fair rotation please"], \
        "an owner-only rule the schedule can't check is said to the owner alone"


def test_memory_with_no_staffing_rule_builds_no_schedule():
    rid = _rid()
    owner_memory.remember(rid, "We close early the first Sunday", kind="context",
                          user=dict(OWNER, restaurant_id=rid))
    out, status = _call(strategy_routes._do_memory_list, dict(OWNER, restaurant_id=rid))
    assert status == 200 and "schedule_reads" not in out and "schedule_unchecked" not in out
