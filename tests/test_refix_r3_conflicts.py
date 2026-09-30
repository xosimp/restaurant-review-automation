"""Memory re-audit 9/29/26, workstream R3 — fact_conflicts (QUALITY-11,
QUALITY-10).

Owner facts had no target, duplicate or conflict check: "keep labor under
26%" filed as a constraint was stored (prompts said 26%, modules judged
28%), paraphrases took two slots and "closed Mondays" / "open Mondays now"
both reached the schedule. And a missed goal never retired: it headed every
goal-reading prompt, weighted as the most urgent line, for months.
"""
from datetime import date, timedelta

import pytest
from flask import Flask

import ask_cavnar_tools as tools
import auth
import goals
import models
import owner_memory
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import metrics
    for mod in (models, auth, goals, metrics):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    owner_memory.invalidate_targets()
    yield
    owner_memory.invalidate_targets()


def _rid():
    return create_restaurant(Restaurant(name="Conflict Co", owner_email="c@x.test"))


def _owner(rid, uid=101):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"}


# ── QUALITY-11 ──────────────────────────────────────────────────────────────

def test_a_measurable_target_is_refused_in_any_kind_but_a_measured_figure_is_not():
    rid = _rid()
    for kind in ("constraint", "context", "preference"):
        with pytest.raises(owner_memory.MemoryRefused, match="set it as a goal"):
            owner_memory.remember(rid, "Keep labor under 26%", kind=kind, user=_owner(rid))
    out = tools._remember(rid, "Never let food cost pass 31%", kind="constraint")
    assert "set_goal" in out["error"]
    # background with a number in it is still background
    owner_memory.remember(rid, "Sales were $40k the week of the fair", user=_owner(rid))
    assert [f["fact"] for f in models.get_ask_memory(rid)] == ["Sales were $40k the week of the fair"]


@pytest.mark.parametrize("old,new", [
    ("We're closed Mondays", "We're open Mondays now"),
    ("Never cut the host", "Don't cut Maria, she's our host"),
])
def test_a_contradiction_or_paraphrase_is_handed_back_and_can_replace_the_old_one(old, new):
    rid = _rid()
    owner_memory.remember(rid, old, kind="constraint", user=_owner(rid))
    out = owner_memory.remember(rid, new, kind="constraint", user=_owner(rid))
    assert [s["fact"] for s in out["similar"]] == [old]
    # "does this replace …?" — yes
    out = owner_memory.remember(rid, new, kind="constraint", user=_owner(rid), replaces=old)
    assert out["replaced"] == old
    assert [f["fact"] for f in models.get_ask_memory(rid)] == [new]
    assert models.get_ask_memory_archive(rid)[0]["reason"] == "replaced"


def test_unrelated_facts_are_not_called_similar():
    rid = _rid()
    owner_memory.remember(rid, "We're closed Mondays", user=_owner(rid))
    out = owner_memory.remember(rid, "Deliveries from Sysco come Tuesday at 9", user=_owner(rid))
    assert out["similar"] == []


def test_the_ask_tool_offers_the_similar_note_and_account_shows_whose_wording_it_is():
    rid = _rid()
    viewer = type("V", (), {"_ask_dsr_user": _owner(rid)})()
    tools._remember(rid, "We're closed Mondays", kind="context", _viewer=viewer)
    out = tools._remember(rid, "We're open Mondays now", kind="context", _viewer=viewer)
    assert out["similar"] == ["We're closed Mondays"] and "replaces" in out["ask"]
    out = tools._remember(rid, "We're open Mondays now", kind="context", replaces="We're closed Mondays",
                          _viewer=viewer)
    assert out["replaced"] == "We're closed Mondays"
    f = owner_memory.account_view(rid, _owner(rid))["facts"][0]
    assert f["worded_by"] == "Cavnar AI"


# ── QUALITY-10 ──────────────────────────────────────────────────────────────

def _goal(rid, deadline, target=26.0, status="active"):
    conn = models.get_conn()
    cur = conn.execute("INSERT INTO owner_goals (restaurant_id, metric, target, deadline, status, created_by, "
                       "authority) VALUES (?, 'labor_pct', ?, ?, ?, 101, 'principal')",
                       (rid, target, deadline, status))
    conn.commit()
    gid = cur.lastrowid
    conn.close()
    return gid


class _Req:
    def __init__(self, rid, surface="ask"):
        self.restaurant_id, self.surface, self.db_path, self.subjects = rid, surface, None, ()


def test_a_missed_goal_leaves_the_prompts_after_its_grace_and_waits_to_be_renewed_or_closed():
    rid = _rid()
    gid = _goal(rid, (date.today() - timedelta(days=goals.MISSED_GRACE_DAYS + 30)).isoformat())
    # inside its grace a missed goal is still said; past it, it is not
    assert goals.progress(rid) == []
    assert not [l for l in owner_memory.goal_lines(_Req(rid)) if l["text"].startswith("Goal")]
    assert [g["id"] for g in goals.retire_missed(rid)] == [gid]
    missed = goals.missed(rid)
    assert [g["id"] for g in missed] == [gid] and missed[0]["summary"].startswith("Labor")
    new = goals.renew_missed(rid, gid, days=30, user_id=101, authority="principal")
    assert new["status"] == "active" and new["target"] == 26.0
    assert goals.missed(rid) == []
    assert owner_memory.target_for(rid, "labor_pct")["value"] == 26.0


def test_a_goal_inside_its_grace_is_still_said_as_missed():
    rid = _rid()
    _goal(rid, (date.today() - timedelta(days=3)).isoformat())
    assert [g["state"] for g in goals.progress(rid)] == ["missed"]
    assert goals.retire_missed(rid) == []


def test_target_for_reads_the_newest_goal_still_in_date():
    """QUALITY-10: a missed newest goal hid an older one still in date."""
    rid = _rid()
    _goal(rid, (date.today() + timedelta(days=60)).isoformat(), target=27.0)
    _goal(rid, (date.today() - timedelta(days=5)).isoformat(), target=24.0)
    assert owner_memory.target_for(rid, "labor_pct")["value"] == 27.0


def test_the_goals_routes_list_missed_goals_and_close_them(db_path):
    import strategy_routes
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    client = app.test_client()
    rid = _rid()
    uid = auth.create_user(rid, "own", "own@x.test", "pw", db_path=db_path)
    h = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db_path)}"}
    gid = _goal(rid, (date.today() - timedelta(days=60)).isoformat())
    body = client.get("/mobile/api/goals", headers=h).get_json()
    assert [g["id"] for g in body["missed"]] == [gid] and body["goals"] == []
    assert client.post(f"/mobile/api/goals/{gid}/close", headers=h, json={}).get_json()["ok"]
    assert client.get("/mobile/api/goals", headers=h).get_json()["missed"] == []
