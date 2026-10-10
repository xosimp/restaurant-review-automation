"""Schedule fix round 10/10/26, workstream J3 — the Studio's Bench stage:
one body for the web route and its phone twin, owner-only parts gated, and
the page wiring (ES5, .cbtn, tokens)."""
import os
import re

import pytest

import models
import studio_bench as sb
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


OWNER = {"id": 1, "role": "client", "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "username": "gm"}


def test_every_part_answers_and_says_why_when_it_has_nothing():
    rid = create_restaurant(Restaurant(name="Bench Co", owner_email="b@x.test", module_labor=1))
    for part in sb.PARTS:
        payload, status = sb.api_body(rid, "GET", args={"part": part}, user=OWNER)
        assert status == 200 and payload.get("ok") is not False, (part, payload)
    wi, _s = sb.api_body(rid, "GET", args={"part": "what_if", "weekday": "Friday", "role": "server", "change": "-1"},
                         user=OWNER)
    assert wi["available"] is False and wi["reason"]


def test_retention_and_tips_are_the_owners_alone():
    rid = create_restaurant(Restaurant(name="Bench Co", owner_email="b@x.test", module_labor=1))
    for part in ("retention", "tips"):
        payload, status = sb.api_body(rid, "GET", args={"part": part}, user=MANAGER)
        assert status == 403 and "Only the owner" in payload["error"]
    payload, status = sb.api_body(rid, "POST", data={"action": "retention_protect", "name": "Ana", "protect": True},
                                  user=MANAGER, may_write=True)
    assert status == 403
    payload, status = sb.api_body(rid, "POST", data={"action": "bench_site", "enabled": True}, user=MANAGER,
                                  may_write=True)
    assert status == 403
    payload, status = sb.api_body(rid, "POST", data={"action": "bench_site", "enabled": True}, user=OWNER, may_write=True)
    assert status == 200 and payload["enabled"] is True


def test_writes_need_the_drafting_permission_and_refusals_are_said():
    rid = create_restaurant(Restaurant(name="Bench Co", owner_email="b@x.test", module_labor=1))
    payload, status = sb.api_body(rid, "POST", data={"action": "standby_policy", "pay_hours": 2}, user=OWNER)
    assert status == 403
    payload, status = sb.api_body(rid, "POST", data={"action": "standby_policy"}, user=OWNER, may_write=True)
    assert status == 400 and "standby pay" in payload["error"] or "hours of pay" in payload["error"]
    payload, status = sb.api_body(rid, "POST", data={"action": "standby_policy", "pay_hours": 3}, user=OWNER,
                                  may_write=True)
    assert status == 200 and payload["policy"]["pay_hours"] == 3.0


def test_the_route_and_its_phone_twin_share_one_body():
    web = open(os.path.join(ROOT, "client_api.py"), encoding="utf-8").read()
    phone = open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8").read()
    assert '@client_bp.route("/api/labor/bench", methods=["GET", "POST"])' in web
    assert '@mobile_bp.route("/labor/bench", methods=["GET", "POST"])' in phone
    assert web.count("_sb.api_body(") == 1 and phone.count("_sb.api_body(") == 1


def test_the_bench_stage_is_wired_in_the_studio():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert 'data-ss-go="bench">Bench</button>' in src and 'data-stage="bench"' in src
    assert "else if (stage === 'bench') ssRenderBench();" in src
    i = src.index("var SS_BENCH_PARTS")
    block = src[i:src.index("function ssRenderCrew() {", i)]
    for part in sb.PARTS:
        assert "['" + part + "'," in block
    assert not re.search(r"=>|`|\bconst\s|\blet\s", block)
