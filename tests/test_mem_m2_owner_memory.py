"""Memory audit 9/29/26 (workstream M2): the owner's memory, split into
lanes, typed, attributed, and served to every generator.

  owner_lanes — each kind has its own budget, and a write past one evicts
                the oldest fact OF THAT KIND into an archive Account shows;
                Home's decline reasons are no longer copied in; "Use again"
                and a restored kind retract the matching fact; follow-ups
                and time-bound facts expire; every write carries its author
                and drops Ask's cached snapshot.
  owner_reach — the memory is typed (kind, modules, subject, audience,
                valid_until) and served through memory_context to the
                surfaces it is about; a structured constraint becomes a
                confirm card that writes the store the schedule obeys.
"""
import json
from datetime import date, timedelta

import pytest
from flask import Flask

import ask_cavnar
import ask_cavnar_tools as tools
import auth
import home_brief
import memory_context
import models
import owner_memory
from models import Restaurant, create_restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, home_brief):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    yield


def _rid(**kw):
    kw.setdefault("name", "Lane Co")
    kw.setdefault("owner_email", "lane@x.test")
    return create_restaurant(Restaurant(**kw))


def _owner(rid, uid=1, name="erik"):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": name}


def _manager(rid, uid=2, name="dana"):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "manager", "username": name}


def _count(sql, *args):
    conn = models.get_conn()
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


# ── owner_lanes: one budget per kind, evictions archived ─────────────────────

def test_a_full_lane_evicts_its_own_oldest_fact_into_the_archive_never_another_kinds():
    rid = _rid()
    owner_memory.remember(rid, "Football Sundays start next week", kind="context", user=_owner(rid))
    for i in range(models.ASK_MEMORY_CAPS["preference"] + 3):
        owner_memory.remember(rid, f"Prefers option {i}", kind="preference", user=_owner(rid))
    facts = [f["fact"] for f in models.get_ask_memory(rid)]
    assert "Football Sundays start next week" in facts, "a busy preference lane never evicts context"
    assert _count("SELECT COUNT(*) FROM ask_memory WHERE restaurant_id=? AND kind='preference'", rid) == \
        models.ASK_MEMORY_CAPS["preference"]
    archived = models.get_ask_memory_archive(rid)
    assert {a["fact"] for a in archived} == {"Prefers option 0", "Prefers option 1", "Prefers option 2"}
    assert {a["reason"] for a in archived} == {"evicted"}


def test_home_decline_reasons_are_not_copied_into_the_owners_memory():
    rid = _rid()
    for i in range(20):
        home_brief.dismiss(rid, f"trim_day:D{i}", kind="not_for_us", user_id=1, reason="We need the cover",
                           title=f"Trim day {i}")
    assert models.get_ask_memory(rid) == []
    # ...the reason is kept with the answer, where decisions reads it.
    import decisions
    rows = {r["key"]: r for r in decisions.history(rid)}
    assert rows["trim_day:D3"]["reason"] == "We need the cover"
    assert rows["trim_day:D3"]["title"] == "Trim day 3"


def test_use_again_retracts_the_remembered_not_doing_fact():
    rid = _rid()
    # a legacy Home copy, and a typed fact about the same subject
    models.remember_ask_fact(rid, "Not doing “Patio promo”: too cold", kind="preference", source="Home")
    owner_memory.remember(rid, "Don't push the patio", kind="preference", subject="patio_promo:fall",
                          user=_owner(rid))
    import rec_ledger
    rec_ledger.present(rid, "patio_promo:fall", "marketing", "home", title="Patio promo")
    home_brief.dismiss(rid, "patio_promo:fall", kind="not_for_us", user_id=1, reason="too cold", title="Patio promo")
    home_brief.undismiss(rid, "patio_promo:fall")
    assert models.get_ask_memory(rid) == []
    assert {a["reason"] for a in models.get_ask_memory_archive(rid)} == {"retracted"}


def test_a_restored_kind_retracts_facts_about_its_keys():
    import decisions
    import rec_ledger
    rid = _rid()
    rec_ledger.present(rid, "reprice:Salmon", "food", "home", title="Reprice Salmon")
    owner_memory.remember(rid, "Leave the salmon price alone", kind="preference", subject="reprice:Salmon",
                          user=_owner(rid))
    decisions.restore_kind(rid, "reprice", user_id=1)
    assert models.get_ask_memory(rid) == []


def test_followups_and_time_bound_facts_expire_into_the_archive():
    rid = _rid()
    today = date(2026, 9, 29)
    owner_memory.remember(rid, "Check Friday's comps with me", kind="followup", due_on="2026-10-02",
                          user=_owner(rid), today=today)
    owner_memory.remember(rid, "Two new bartenders start", kind="context", valid_until="2026-10-31",
                          user=_owner(rid), today=today)
    assert len(owner_memory.facts_for(rid, today=today)) == 2
    # a week past the follow-up's due date it leaves; the context stays until its day
    assert [f["fact"] for f in owner_memory.facts_for(rid, today=date(2026, 10, 10))] == ["Two new bartenders start"]
    assert owner_memory.facts_for(rid, today=date(2026, 11, 1)) == []
    assert {a["reason"] for a in models.get_ask_memory_archive(rid)} == {"expired"}


def test_a_past_date_or_a_measurable_goal_is_refused_with_what_to_do_instead():
    rid = _rid()
    with pytest.raises(owner_memory.MemoryRefused):
        owner_memory.remember(rid, "Summer menu", kind="context", valid_until="2020-01-01", user=_owner(rid))
    out = tools._remember(rid, "Wants labor under 26% by December", kind="goal")
    assert "set_goal" in out["error"]


# ── owner_lanes: who said it, who may read it ───────────────────────────────

def test_every_write_carries_its_author_and_a_managers_words_are_never_the_owners():
    rid = _rid()
    owner_memory.remember(rid, "Never cut the host on Fridays", kind="constraint", user=_owner(rid))
    owner_memory.remember(rid, "The walk-in door sticks", kind="context", user=_manager(rid))
    by = {f["fact"]: f for f in models.get_ask_memory(rid)}
    assert by["Never cut the host on Fridays"]["author_label"] == "Erik, owner"
    assert by["Never cut the host on Fridays"]["authority"] == "principal"
    assert by["The walk-in door sticks"]["author_label"] == "Dana, manager"
    text = ask_cavnar._memory_context(rid)
    assert "Dana, manager" in text and "Erik, owner" in text


def test_ask_remember_passes_the_viewer_who_said_it():
    rid = _rid()
    view = tools.viewer_restaurant(get_restaurant(rid), _manager(rid))
    out = json.loads(tools.run_read_tool("remember", rid, {"fact": "Maria prefers mornings", "kind": "preference",
                                                            "modules": ["schedule"]}, restaurant=view))
    assert out["remembered"] == "Maria prefers mornings"
    row = models.get_ask_memory(rid)[0]
    assert row["user_id"] == 2 and row["author_label"] == "Dana, manager" and row["modules"] == "schedule"


def test_a_private_note_never_reaches_a_managers_prompt():
    rid = _rid()
    owner_memory.remember(rid, "I'm letting Dana go in October", kind="context", audience="team",
                          user=_owner(rid))
    row = models.get_ask_memory(rid)[0]
    assert row["audience"] == "principals", "personnel said by an owner stays theirs, whatever was asked"
    manager_view = tools.viewer_restaurant(get_restaurant(rid), _manager(rid))
    owner_view = tools.viewer_restaurant(get_restaurant(rid), _owner(rid))
    assert "letting Dana go" not in ask_cavnar.build_context(manager_view)
    assert "letting Dana go" in ask_cavnar.build_context(owner_view)


def test_a_personal_followup_is_its_authors_only():
    rid = _rid()
    owner_memory.remember(rid, "Remind me to call the linen company", kind="followup",
                          due_on=(date.today() + timedelta(days=2)).isoformat(), user=_manager(rid))
    assert models.get_ask_memory(rid)[0]["audience"] == "author"
    assert owner_memory.facts_for(rid, viewer=_manager(rid))
    assert owner_memory.facts_for(rid, viewer=_manager(rid, uid=3, name="sam")) == []


def test_forget_needs_an_exact_or_unique_match_and_a_teammate_drops_only_their_own():
    rid = _rid()
    owner_memory.remember(rid, "Closes Mondays in January", kind="context", user=_owner(rid))
    owner_memory.remember(rid, "Closes Mondays in February", kind="context", user=_owner(rid))
    out = owner_memory.forget(rid, "Closes Mondays", user=_owner(rid))
    assert "more than one" in out["error"] and len(out["candidates"]) == 2
    out = owner_memory.forget(rid, "Closes Mondays in January", user=_manager(rid))
    assert "someone else" in out["error"]
    assert owner_memory.forget(rid, "in February", user=_owner(rid)) == {"forgotten": "Closes Mondays in February"}


def test_every_memory_write_drops_asks_cached_snapshot():
    rid = _rid()
    r = get_restaurant(rid)
    assert "Kitchen closes at 9" not in ask_cavnar.build_context(r)
    owner_memory.remember(rid, "Kitchen closes at 9 on weeknights", kind="context", user=_owner(rid))
    assert "Kitchen closes at 9" in ask_cavnar.build_context(r), "a new fact reaches the next answer"
    owner_memory.forget(rid, "Kitchen closes at 9 on weeknights", user=_owner(rid))
    assert "Kitchen closes at 9" not in ask_cavnar.build_context(r)


# ── owner_reach: typed memory reaches the generators it is about ────────────

def test_a_labor_fact_reaches_the_schedule_and_labor_read_but_not_the_food_read():
    rid = _rid()
    owner_memory.remember(rid, "Football Sundays start 10/4 — staff the bar", kind="context",
                          modules=["labor", "marketing"], user=_owner(rid))
    owner_memory.remember(rid, "Stop suggesting the salmon special", kind="preference", modules=["food"],
                          user=_owner(rid))
    owner_memory.remember(rid, "We are a family place", kind="context", user=_owner(rid))
    sched = memory_context.memory_context(rid, "schedule").text
    food = memory_context.memory_context(rid, "food_read").text
    mkt = memory_context.memory_context(rid, "marketing").text
    assert "Football Sundays" in sched and "salmon" not in sched and "family place" in sched
    assert "salmon" in food and "Football" not in food and "family place" in food
    assert "Football Sundays" in mkt
    # fenced, dated M/D/YY, and says who said it
    import ai_guard
    assert ai_guard.UNTRUSTED_OPEN in sched and "Erik, owner" in sched


def test_the_remember_tool_steers_targets_closures_and_availability_to_their_own_stores():
    spec = tools._BY_NAME["remember"]["spec"]
    desc = spec["description"]
    assert "set_goal" in desc and "add_closed_date" in desc and "set_staff_unavailable" in desc
    assert set(spec["input_schema"]["properties"]) >= {"fact", "kind", "modules", "subject", "valid_until",
                                                        "due_on", "audience"}
    assert tools._BY_NAME["remember"].get("wants_viewer") and tools._BY_NAME["forget"].get("wants_viewer")


def test_a_closure_becomes_a_confirm_card_on_the_closures_route():
    rid = _rid()
    day = (date.today() + timedelta(days=14)).isoformat()
    card = tools.build_proposal("add_closed_date", {"date": day}, restaurant_id=rid)
    assert card["route"]["web"] == "/api/account-settings/closures"
    assert card["route"]["mobile"] == "/mobile/api/account/closures"
    assert card["body"] == {"add": day} and "as closed" in card["summary"]
    assert card["fields_shown"][0]["label"] == "Closed on"
    past = (date.today() - timedelta(days=3)).isoformat()
    assert tools.build_proposal("add_closed_date", {"date": past}, restaurant_id=rid) is None
    assert "passed" in tools.proposal_refusal("add_closed_date", {"date": past}, restaurant_id=rid)
    # a manager (whom the closures route refuses) is not offered it
    manager_view = tools.viewer_restaurant(get_restaurant(rid), _manager(rid))
    assert not tools.tool_allowed("add_closed_date", manager_view)


def test_an_unavailable_weekday_card_keeps_the_persons_other_days_and_notes():
    rid = _rid(module_labor=1)
    models.save_staff_availability(rid, "Maria", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
                                   ["Saturday"], notes="school on Saturdays")
    card = tools.build_proposal("set_staff_unavailable", {"employee_name": "maria", "weekdays": ["Sunday"]},
                                restaurant_id=rid)
    assert card["route"]["web"] == "/api/labor/availability"
    assert card["body"] == {"employee_name": "Maria", "unavailable_days": ["Saturday", "Sunday"],
                            "notes": "school on Saturdays"}
    assert tools.build_proposal("set_staff_unavailable", {"employee_name": "Nobody", "weekdays": ["Sunday"]},
                                restaurant_id=rid) is None
    assert "roster" in tools.proposal_refusal("set_staff_unavailable",
                                              {"employee_name": "Nobody", "weekdays": ["Sunday"]}, restaurant_id=rid)


# ── Account: the memory, its lanes and what left ────────────────────────────

@pytest.fixture
def client(db_path):
    import strategy_routes
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    return app.test_client()


def _bearer(db_path, rid, username="own", role=None):
    uid = auth.create_user(rid, username, f"{username}@x.test", "pw", db_path=db_path)
    if role:
        conn = models.get_conn()
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        conn.commit()
        conn.close()
    return uid, {"Authorization": f"Bearer {auth.create_session(uid, db_path=db_path)}"}


def test_account_lists_facts_with_their_author_lanes_and_what_left(client, db_path):
    rid = _rid()
    uid, h = _bearer(db_path, rid)
    assert client.post("/mobile/api/account/memory/add", headers=h,
                       json={"fact": "Closed the Monday after Thanksgiving", "kind": "constraint",
                             "modules": ["schedule"]}).get_json()["ok"]
    for i in range(models.ASK_MEMORY_CAPS["goal"] + 1):
        owner_memory.remember(rid, f"Aim {i}: be the neighbourhood's living room", kind="goal", user=_owner(rid, uid))
    body = client.get("/mobile/api/account/memory", headers=h).get_json()
    fact = next(f for f in body["facts"] if f["fact"].startswith("Closed the Monday"))
    assert fact["kind"] == "constraint" and fact["author"] == "Own, owner" and fact["can_forget"] is True
    assert {"kind": "goal", "count": models.ASK_MEMORY_CAPS["goal"], "cap": models.ASK_MEMORY_CAPS["goal"]} \
        in body["lanes"]
    gone = body["archived"][0]
    assert gone["reason"] == "evicted" and gone["reason_label"] == "its lane was full" and gone["can_restore"]
    # ...and it can be put back
    assert client.post("/mobile/api/account/memory/restore", headers=h, json={"id": gone["id"]}).get_json()["ok"]
    assert any(f["fact"] == gone["fact"] for f in models.get_ask_memory(rid))


def test_a_teammate_cannot_forget_the_owners_fact_through_account(client, db_path):
    rid = _rid()
    owner_memory.remember(rid, "Never cut the host", kind="constraint", user=_owner(rid, uid=99))
    _uid, h = _bearer(db_path, rid, username="dana", role="manager")
    r = client.post("/mobile/api/account/memory/forget", headers=h, json={"fact": "Never cut the host"})
    assert r.status_code == 403
    assert models.get_ask_memory(rid)


def test_the_activity_feed_shows_only_the_followups_this_login_may_read():
    import activity
    rid = _rid()
    soon = (date.today() + timedelta(days=2)).isoformat()
    owner_memory.remember(rid, "Call the landlord about the patio lease", kind="followup", due_on=soon,
                          audience="principals", user=_owner(rid))
    owner_memory.remember(rid, "Check Friday's comps", kind="followup", due_on=soon, audience="team",
                          user=_owner(rid))
    view = tools.viewer_restaurant(get_restaurant(rid), _manager(rid))
    texts = [m["text"] for m in activity.build(rid, restaurant=view)["memory"]]
    assert "Remembering: Check Friday's comps" in texts
    assert not any("landlord" in t for t in texts)


def test_kitchen_talk_is_not_mistaken_for_a_personnel_secret():
    rid = _rid()
    for text in ("Fire the grill by 4pm on game days", "Replacing the walk-in door on Monday"):
        owner_memory.remember(rid, text, kind="context", user=_owner(rid))
    assert {f["audience"] for f in models.get_ask_memory(rid)} == {"team"}
    owner_memory.remember(rid, "Thinking of replacing the GM after the holidays", kind="context", user=_owner(rid))
    assert models.get_ask_memory(rid)[0]["audience"] == "principals"
