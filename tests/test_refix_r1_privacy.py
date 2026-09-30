"""Memory re-audit fix round R1 (9/29/26) — privacy reads: nothing owner-only
or out-of-permission reaches a teammate or a shared output.

Each test is a reviewer's failing case (memreaudit/people.md, prompts.md,
inventory.md, quality.md) turned around:

  * PEOPLE-1   the Account activity log never carries a memory fact's words.
  * PEOPLE-2   comps and voids goals and results (and item waste) need the
               loss / food-cost view in Ask and in memory goal lines.
  * PROMPTS-7 / INVENTORY-4   a fact about two modules keeps both gates, in
               the prompt, in Account and in forget.
  * PEOPLE-8 / QUALITY-9 / PROMPTS-18 / PEOPLE-9   an owner-level output
               with no login reads as PRINCIPALS (never one login's own
               line); unattended Ask reads no one's ratings; the morning
               brief is built per login when memory is personal.
  * PROMPTS-8 / PEOPLE-14 / PROMPTS-14   the Monday plan runs as the team,
               its food-cost items are filed with their module and hidden
               from a login without it; the plan and the DSR carry one
               decisions copy, read as the team.
  * PEOPLE-11 / QUALITY-15 / QUALITY-16   what worked (and what was
               declined) is gated by every module, loss and owner-only.
  * PEOPLE-18 / PROMPTS-17   a chat with no login is the owner's, not
               everyone's.
  * PEOPLE-10  personnel and money default to owners only, whoever says
               it, with a one-tap share.
  * PROMPTS-9  the brand voice is an account holder's to write.
"""
import json

import pytest

import models
from models import Restaurant, create_restaurant

OWNER = {"id": 1, "is_admin": 0, "role": "client", "username": "erik"}
MANAGER = {"id": 2, "is_admin": 0, "role": "manager", "username": "dana"}
MANAGER2 = {"id": 3, "is_admin": 0, "role": "manager", "username": "sam"}


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    import goals, morning_brief, staff_settings, strategy_jobs
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    for mod in (goals, morning_brief, staff_settings, strategy_jobs):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    import auth
    auth.init_auth(db_path=db_path)
    models.init_ask_memory(db_path=db_path)


def _rid(**kw):
    kw.setdefault("module_labor", 1)
    kw.setdefault("module_inventory", 1)
    kw.setdefault("module_reviews", 1)
    rid = create_restaurant(Restaurant(name="Proof Co", owner_email="p@x.test", **kw))
    for u in (OWNER, MANAGER, MANAGER2):
        u["restaurant_id"] = rid
    return rid


def _users(rid):
    """Real logins with the fixture ids (the chat backfill reads users)."""
    conn = models.get_conn()
    for u in (OWNER, MANAGER, MANAGER2):
        conn.execute("INSERT OR REPLACE INTO users (id, restaurant_id, username, email, password_hash, role, is_admin) "
                     "VALUES (?,?,?,?,?,?,0)", (u["id"], rid, u["username"], f"{u['username']}@x.test", "x", u["role"]))
    conn.commit()
    conn.close()


# ── PEOPLE-1: the activity log ─────────────────────────────────────────────

def test_a_manager_never_reads_a_memory_facts_words_in_the_activity_log(monkeypatch):
    import strategy_routes as sr
    rid = _rid()
    monkeypatch.setattr(sr, "_body", lambda: {"fact": "We are letting Dana go in October", "kind": "context"})
    payload, status = sr._do_memory_add(OWNER)
    assert status == 200 and payload["audience"] == "principals"
    # The fact is forgotten by the owner too: the forget event names no text either.
    monkeypatch.setattr(sr, "_body", lambda: {"fact": "We are letting Dana go in October"})
    assert sr._do_memory_forget(OWNER)[1] == 200
    # A legacy row written before the fix carries the text: the reader drops it.
    models.log_event(rid, "memory_added", {"detail": "Dana is on a PIP", "actor": "erik"})
    events = models.get_account_activity(rid)
    mem = [e for e in events if e["type"] in ("memory_added", "memory_forgotten")]
    assert len(mem) == 3
    blob = json.dumps(events)
    assert "Dana" not in blob and "PIP" not in blob
    assert any(e["detail"] and "account holders only" in e["detail"] for e in mem)


# ── PEOPLE-2: comps and voids, item waste ──────────────────────────────────

def test_metric_permission_is_one_rule():
    import metrics
    assert metrics.metric_permission("food_cost_pct") == "food"
    assert metrics.metric_permission("item_waste:Salmon") == "food"
    assert metrics.metric_permission("weekly_waste") == "food"
    assert metrics.metric_permission("comp_rate") == "loss" and metrics.metric_permission("void_rate") == "loss"
    assert metrics.metric_permission("labor_pct") is None and metrics.metric_permission("sales") is None


def test_a_comp_goal_reaches_neither_a_managers_ask_nor_the_shared_report(db_path):
    import goals, memory_context, outcomes
    import ask_cavnar_tools as tools
    rid = _rid()
    goals.set_goal(rid, "comp_rate", 1.5, user_id=1, authority="principal", db_path=db_path)
    goals.set_goal(rid, "labor_pct", 27, user_id=1, authority="principal", db_path=db_path)
    view = tools.viewer_restaurant(models.get_restaurant(rid), MANAGER)
    assert [g["metric"] for g in tools._read_goals(rid, _viewer=view)["goals"]] == ["labor_pct"]
    assert "error" in tools._set_goal(rid, "void_rate", 1.0, _viewer=view)
    blk = memory_context.memory_context(rid, "ask", viewer=MANAGER, db_path=db_path)
    assert not any("Comps" in l["text"] for l in blk.sections.get("goals", []))
    blk = memory_context.memory_context(rid, "dsr_narrative", db_path=db_path)
    assert not any("Comps" in l["text"] for l in blk.sections.get("goals", []))
    # The owner still reads it everywhere.
    blk = memory_context.memory_context(rid, "ask", viewer=OWNER, db_path=db_path)
    assert any("Comps" in l["text"] for l in blk.sections.get("goals", []))
    owner_view = tools.viewer_restaurant(models.get_restaurant(rid), OWNER)
    assert "comp_rate" in [g["metric"] for g in tools._read_goals(rid, _viewer=owner_view)["goals"]]
    # Item waste is food-cost dollars at every gate.
    assert tools.metric_visible(view, "item_waste:Salmon") is False
    assert outcomes.metric_visible_to(MANAGER, "item_waste:Salmon") is False
    assert outcomes.metric_visible_to(OWNER, "item_waste:Salmon") is True


def test_asks_outcomes_read_uses_the_rest_routes_rule(monkeypatch):
    import outcomes
    import ask_cavnar_tools as tools
    rid = _rid()
    rows = [{"id": 1, "metric": "labor_pct", "status": "improved", "source_key": "owner_only_thing"},
            {"id": 2, "metric": "labor_pct", "status": "improved", "source_key": "fine"}]
    monkeypatch.setattr(outcomes, "list_outcomes", lambda *a, **k: rows)
    monkeypatch.setattr(outcomes, "progress", lambda *a, **k: [])
    monkeypatch.setattr(outcomes, "summarise", lambda r: "x")
    seen = []
    monkeypatch.setattr(outcomes, "visible_to", lambda v, r, linked=None, db_path=None:
                        seen.append(r["id"]) or r["source_key"] != "owner_only_thing")
    view = tools.viewer_restaurant(models.get_restaurant(rid), MANAGER)
    got = tools._read_outcomes(rid, _viewer=view)
    assert [r["id"] for r in got["results"]] == [2] and seen == [1, 2]


# ── PROMPTS-7 / INVENTORY-4: two modules, both gates ───────────────────────

def test_a_fact_about_food_and_labor_keeps_its_food_gate(db_path):
    import memory_context, owner_memory
    rid = _rid()
    owner_memory.remember(rid, "Sysco beef jumped 12% and burger margin is down to 22%", kind="context",
                          modules=["food", "labor"], user=OWNER, db_path=db_path)
    owner_memory.remember(rid, "Food cost is the whole story this month", kind="context", modules=["food"],
                          user=OWNER, db_path=db_path)
    mgr = memory_context.memory_context(rid, "ask", viewer=MANAGER, db_path=db_path)
    assert "Sysco" not in mgr.text
    sched = memory_context.memory_context(rid, "schedule", viewer=OWNER, db_path=db_path)   # shared: TEAM
    assert "Sysco" not in sched.text
    own = memory_context.memory_context(rid, "ask", viewer=OWNER, db_path=db_path)
    assert "Sysco" in own.text
    # Account and forget read the same gate.
    facts = [f["fact"] for f in owner_memory.account_view(rid, MANAGER, db_path=db_path)["facts"]]
    assert not any("Sysco" in f or "Food cost" in f for f in facts)
    assert "Sysco" not in json.dumps(owner_memory.forget(rid, "nothing like it", user=MANAGER, db_path=db_path))
    assert len(owner_memory.account_view(rid, OWNER, db_path=db_path)["facts"]) == 2


# ── PEOPLE-8 / QUALITY-9 / PROMPTS-18 / PEOPLE-9: no login is not everyone ──

def test_the_digest_reads_as_principals_never_a_managers_own_note(db_path):
    import memory_context, owner_memory
    rid = _rid()
    owner_memory.remember(rid, "Remind me I am interviewing for another job Thursday", kind="context",
                          audience="author", user=MANAGER, db_path=db_path)
    owner_memory.remember(rid, "Keep the patio open late on Fridays", kind="context", user=OWNER, db_path=db_path)
    owner_memory.remember(rid, "We are selling the bar next spring", kind="context", user=OWNER, db_path=db_path)
    dig = memory_context.memory_context(rid, "digest", db_path=db_path)
    assert "another job" not in dig.text
    assert "patio" in dig.text and "selling the bar" in dig.text          # team + principals
    explicit = memory_context.memory_context(rid, "digest", viewer=memory_context.PRINCIPALS, db_path=db_path)
    assert explicit.text == dig.text
    # The manager's own Ask still has it; the internal data path still sees all.
    assert "another job" in memory_context.memory_context(rid, "ask", viewer=MANAGER, db_path=db_path).text
    assert len(owner_memory.facts_for(rid, viewer=None, db_path=db_path)) == 3


def test_unattended_ask_reads_no_ones_ratings(monkeypatch):
    import ask_cavnar
    monkeypatch.setattr(models, "ask_feedback_summary", lambda *a, **k: {"rated": 5, "helpful": 1, "days": 90})
    assert ask_cavnar._feedback_context(1, viewer=None) == ""
    assert "this person's own ratings" in ask_cavnar._feedback_context(1, viewer={"id": 2})


def test_the_morning_brief_is_built_per_login_when_memory_is_personal(db_path):
    import morning_brief, owner_memory
    rid = _rid()
    assert morning_brief._personal(rid, OWNER, db_path) is False
    owner_memory.remember(rid, "Keep the patio open late", kind="context", user=OWNER, db_path=db_path)
    assert morning_brief._personal(rid, OWNER, db_path) is False                  # team only: one brief
    owner_memory.remember(rid, "Dana's last day is Friday", kind="context", user=OWNER, db_path=db_path)
    assert morning_brief._personal(rid, OWNER, db_path) is True


def test_the_brief_says_whose_note_it_is(monkeypatch):
    import memory_context, morning_brief
    from datetime import date
    today = date(2026, 9, 29)
    blk = memory_context.MemoryBlock(sections={"constraints": [
        {"text": "Constraint: two on the line tonight", "date": "2026-09-29", "author_id": 1, "who": "Erik, owner"}]})
    monkeypatch.setattr(memory_context, "memory_context", lambda *a, **k: blk)
    mine = morning_brief._memory_lines(1, today, OWNER, [])
    theirs = morning_brief._memory_lines(1, today, MANAGER, [])
    assert mine[0]["text"].startswith("Your note for today:")
    assert theirs[0]["text"].startswith("Note for today from Erik, owner:")


# ── PROMPTS-8 / PEOPLE-14 / PROMPTS-14: the plan and the report ───────────

def test_the_plan_runs_as_the_team_and_carries_one_memory_block(monkeypatch):
    import ask_cavnar, memory_context
    rid = _rid()
    seen = {}

    class Stop(Exception):
        pass

    def _capture(restaurant):
        seen["r"] = restaurant
        raise Stop()
    monkeypatch.setattr(ask_cavnar, "build_context", _capture)
    with pytest.raises(Stop):
        ask_cavnar.ask_with_tools(models.get_restaurant(rid), "q", user=None, read_only=True,
                                  delivery="unattended", action="weekly_plan")
    r = seen["r"]
    assert r._ask_sees_loss is False and "inventory" not in r._ask_denied
    assert memory_context.is_team(r._ask_dsr_user) and r._ask_memory_viewer == "team"
    assert ask_cavnar._memory_context(rid, viewer=r) == ""           # plan_memory is the one block
    for surface in ("weekly_plan", "dsr_narrative"):
        assert "decisions" not in memory_context.SURFACE_SECTIONS[surface]


def test_food_cost_plan_items_are_hidden_from_a_login_without_food_cost(monkeypatch, db_path):
    import issues, strategy_jobs, strategy_routes as sr
    rid = _rid()
    assert strategy_jobs.plan_item_modules({"title": "Reprice the short rib", "why": "margin slipped"}) == ["inventory"]
    assert strategy_jobs.plan_item_modules({"title": "Trim Tuesday dinner", "why": "one server"}) == ["labor"]
    food, _ = issues.create_issue(rid, "plan", "Reprice the short rib", notify=False, db_path=db_path,
                                  meta={"modules": ["inventory"]})
    labor, _ = issues.create_issue(rid, "plan", "Trim Tuesday", notify=False, db_path=db_path,
                                   meta={"modules": ["labor"]})
    issues.create_issue(rid, "manual", "Fix the door", notify=False, db_path=db_path)
    hide = issues.hidden_modules(MANAGER)
    assert hide == {"inventory"} and issues.hidden_modules(OWNER) == frozenset()
    titles = [i["title"] for i in issues.list_issues(rid, hide_modules=hide, db_path=db_path)]
    assert "Reprice the short rib" not in titles and {"Trim Tuesday", "Fix the door"} <= set(titles)
    assert issues.summary(rid, hide_modules=hide, db_path=db_path)["open"] == 2
    assert len(issues.list_issues(rid, db_path=db_path)) == 3
    monkeypatch.setattr(sr, "request", type("R", (), {"args": {}})())
    monkeypatch.setattr(sr, "present_covers", lambda *a, **k: {})
    payload, _ = sr._do_issues_list(MANAGER)
    assert "Reprice the short rib" not in [i["title"] for i in payload["issues"]]
    assert sr._issue_hidden(MANAGER, food["id"]) and not sr._issue_hidden(OWNER, food["id"])
    assert not sr._issue_hidden(MANAGER, labor["id"])


def test_the_dsr_reads_decisions_as_the_team():
    import inspect
    from dsr import narrative
    src = inspect.getsource(narrative)
    assert "decisions.context(rid, db_path=ctx.db_path, sees_loss=False,\n" in src
    assert "viewer=NARRATIVE_MEMORY_VIEWER)" in src


# ── PEOPLE-11 / QUALITY-15 / QUALITY-16: what worked ───────────────────────

def test_what_worked_is_gated_by_every_module_loss_and_owner_only(db_path):
    import memory_context, rec_learning, rec_ledger
    rid = _rid()
    rec_ledger.present(rid, "reprice:Salmon", "home", "home", title="Reprice salmon", kind="reprice", db_path=db_path)
    rec_ledger.present(rid, "loss:2026-W39:comp:12", "home", "home", title="Comps", db_path=db_path)
    rec_ledger.present(rid, "dsr_action:2026-09-28:labor/x", "labor", "dsr", title="Cut a server", db_path=db_path,
                       owner_only=True)
    rec_ledger.present(rid, "trim_day:Tuesday", "labor", "home", title="Trim Tuesday", db_path=db_path)
    gates = rec_learning.record_gates(rid, db_path=db_path)

    def sees(viewer, kind):
        return memory_context.visible(dict(rec_learning.line_gate("kind", kind, "home", gates), text="x"), viewer)
    assert not sees(MANAGER, "reprice") and not sees(memory_context.TEAM, "reprice") and sees(OWNER, "reprice")
    assert not sees(MANAGER, "loss") and not sees(memory_context.TEAM, "loss") and sees(OWNER, "loss")
    assert not sees(MANAGER, "dsr_action") and sees(OWNER, "dsr_action")
    assert sees(MANAGER, "trim_day") and sees(memory_context.TEAM, "trim_day")
    # Unreadable gates fail closed.
    assert not memory_context.visible(dict(rec_learning.line_gate("kind", "trim_day", None, (None, None)), text="x"),
                                      MANAGER)


def test_asks_memory_tool_and_snapshot_carry_no_unprojected_record(monkeypatch, db_path):
    import intelligence, memory_context, rec_ledger
    import ask_cavnar_tools as tools
    from intelligence import memory
    rid = _rid()
    rec_ledger.present(rid, "reprice:Salmon", "home", "home", title="Reprice salmon", kind="reprice", db_path=db_path)
    rec_ledger.present(rid, "trim_day:Tuesday", "labor", "home", title="Trim Tuesday", db_path=db_path)
    rec = {"worked": ["reprice", "trim_day"], "ignored": ["reprice"], "worked_detail": [],
           "declined_detail": [{"kind": "reprice", "label": "reprice", "subjects": ["Salmon"], "n": 3, "since": "9/1/26"}],
           "by_kind": {k: {"accepted": 1, "declined": 0, "measured": 3, "improved": 2, "success_rate": None}
                       for k in ("reprice", "trim_day")}}
    monkeypatch.setattr(intelligence, "restaurant_memory", lambda *a, **k: {
        "features": {}, "slopes": {}, "busiest_days": {}, "seasonality": {}, "record": rec})
    view = tools.viewer_restaurant(models.get_restaurant(rid), MANAGER)
    got = tools._read_restaurant_memory(rid, _viewer=view)["record"]
    assert got["worked"] == ["trim_day"] and got["declined"] == [] and set(got["by_kind"]) == {"trim_day"}
    owner = tools._read_restaurant_memory(rid, _viewer=tools.viewer_restaurant(models.get_restaurant(rid), OWNER))
    assert owner["record"]["worked"] == ["reprice", "trim_day"]
    # Ask's snapshot: no record lines from the engine, what_worked from memory.
    assert not memory.lines({"record": rec}, record=False)
    assert memory.lines({"record": dict(rec, worked_detail=[{"kind": "reprice", "improved": 2, "measured": 3}])})
    assert "what_worked" in memory_context.SURFACE_SECTIONS["ask"]


# ── PEOPLE-18 / PROMPTS-17: a chat with no login ───────────────────────────

def test_a_chat_with_no_login_becomes_the_owners_and_no_one_elses(db_path):
    import ask_conversations
    rid = _rid()
    _users(rid)
    cid = models.create_ask_conversation(rid, user_id=None)
    models.save_ask_message(rid, "user", "should I let Dana go?", user_id=None, conversation_id=cid)
    models.save_ask_message(rid, "assistant", "Let's think it through.", user_id=None, conversation_id=cid)
    # Before boot attributes it, a manager no longer reads it either.
    assert ask_conversations.past_conversations(rid, MANAGER["id"], db_path=db_path) == []
    conn = models.get_conn()
    conn.row_factory = __import__("sqlite3").Row
    assert models._attribute_ownerless_ask_chats(conn) >= 2
    conn.close()
    assert models.get_ask_conversation(rid, cid, viewer_id=OWNER["id"]) is not None
    assert models.get_ask_conversation(rid, cid, viewer_id=MANAGER["id"]) is None
    assert ask_conversations.past_conversations(rid, MANAGER["id"], db_path=db_path) == []
    assert [c["conversation_id"] for c in ask_conversations.past_conversations(rid, OWNER["id"], db_path=db_path)] == [cid]
    mid = models.latest_ask_answer_id(rid, cid, user_id=OWNER["id"])
    assert mid and models.record_ask_feedback(rid, mid, True, user_id=MANAGER["id"]) is None


# ── PEOPLE-10: personnel and money default to owners only ──────────────────

@pytest.mark.parametrize("text", [
    "Fire Dana next week", "Dana is on a PIP", "Dana's last day is Friday",
    "Cutting Dana's hours after the review", "Giving Marco a $2 raise", "Marco makes $18 an hour",
    "Writing up Dana for no-shows", "We are selling the bar", "Talking to a buyer for the restaurant"])
def test_the_reviewers_personnel_and_money_facts_are_private(text):
    import owner_memory
    assert owner_memory.is_private(text)


@pytest.mark.parametrize("text", ["We close Mondays", "Friday dinner needs two extra servers",
                                  "Raise the burger price to $16", "Pay the Sysco invoice Friday",
                                  "Promote the patio on Instagram"])
def test_ordinary_facts_stay_the_teams(text):
    import owner_memory
    assert not owner_memory.is_private(text)


def test_a_managers_personnel_remark_is_private_too_and_can_be_shared(monkeypatch, db_path):
    import memory_context, owner_memory, strategy_routes as sr
    rid = _rid()
    saved = owner_memory.remember(rid, "we're firing the dishwasher", kind="context", audience="team",
                                  user=MANAGER, db_path=db_path)
    assert saved["audience"] == "principals" and saved["private_default"]
    # A roster name the capitalisation cannot see.
    monkeypatch.setattr(owner_memory, "_roster_names", lambda *a, **k: {"dana", "dana ruiz"})
    assert owner_memory.remember(rid, "writing up dana tomorrow", user=OWNER, db_path=db_path)["audience"] == "principals"
    assert owner_memory.remember(rid, "gm review: hours cut", subject="person:dana", user=OWNER,
                                 db_path=db_path)["audience"] == "principals"
    blk = memory_context.memory_context(rid, "ask", viewer=MANAGER, db_path=db_path)
    assert "dishwasher" in blk.text                                     # its author reads it
    assert "dishwasher" not in memory_context.memory_context(rid, "ask", viewer=MANAGER2, db_path=db_path).text
    fid = next(f["id"] for f in models.get_ask_memory(rid, db_path=db_path) if "dishwasher" in f["fact"])
    monkeypatch.setattr(sr, "_body", lambda: {"id": fid, "audience": "team"})
    payload, status = sr._do_memory_audience(MANAGER2)
    assert status == 404                                                # not theirs to read, nor to change
    payload, status = sr._do_memory_audience(MANAGER)
    assert status == 200 and payload["audience"] == "team"
    assert "dishwasher" in memory_context.memory_context(rid, "ask", viewer=MANAGER2, db_path=db_path).text


# ── PROMPTS-9: the brand voice ─────────────────────────────────────────────

def test_only_an_account_holder_writes_the_brand_voice(monkeypatch):
    import client_api
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    rid = _rid()
    models.update_restaurant(rid, {"voice_notes": "warm", "menu_notes": "Wings"})
    payload, status = client_api._do_brand_voice(rid, {"menu_notes": "Half-price wings every Tuesday"}, MANAGER)
    assert status == 403 and payload.get("owner_only")
    assert models.get_restaurant(rid).menu_notes == "Wings"
    payload, status = client_api._do_brand_voice(rid, {"never_say": "delightful"}, OWNER)
    assert status == 200
    r = models.get_restaurant(rid)
    assert r.never_say == "delightful" and r.menu_notes == "Wings" and r.voice_notes == "warm"   # absent: kept


def test_the_phone_profile_refuses_a_teammates_brand_voice_change(monkeypatch, db_path):
    """The mobile twin (mobile_update_profile): a manager's save of the same
    form leaves the brand voice as it is; a change to it is refused; the
    owner's goes through; a field not sent is never cleared."""
    from flask import Flask
    import auth, auth_routes, client_api, mobile_api
    from auth import create_user
    real = models.get_conn
    for mod in (auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    auth_routes._login_attempts.clear()
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    client = app.test_client()
    rid = _rid()
    models.update_restaurant(rid, {"voice_notes": "Warm", "menu_notes": "Deep dish", "owner_name": "Erik"})
    create_user(rid, "owner1", "owner1@x.test", "correct-horse", db_path=db_path)
    create_user(rid, "mgr1", "mgr1@x.test", "correct-horse", db_path=db_path, role="manager")

    def tok(u):
        return {"Authorization": "Bearer " + client.post("/mobile/api/login",
                                                         json={"username": u, "password": "correct-horse"}).get_json()["token"]}
    mgr, own = tok("mgr1"), tok("owner1")
    r = client.post("/mobile/api/account/update-profile", headers=mgr,
                    json={"voice_notes": "Warm", "owner_name": "Erik", "timezone": "America/Denver"})
    assert r.status_code == 200
    got = models.get_restaurant(rid)
    assert got.timezone == "America/Denver" and got.menu_notes == "Deep dish"       # menu_notes not sent: kept
    r = client.post("/mobile/api/account/update-profile", headers=mgr,
                    json={"menu_notes": "Half-price wings every Tuesday"})
    assert r.status_code == 403 and r.get_json()["owner_only"]
    assert models.get_restaurant(rid).menu_notes == "Deep dish"
    r = client.post("/mobile/api/account/update-profile", headers=own, json={"menu_notes": "Wings Tuesday"})
    assert r.status_code == 200 and models.get_restaurant(rid).menu_notes == "Wings Tuesday"


def test_a_rating_derived_preference_steers_only_that_logins_ask(db_path):
    import memory_context
    rid = _rid()
    models.remember_ask_fact(rid, "Prefers short, direct answers", kind="preference", source="Your ratings",
                             user_id=MANAGER["id"], db_path=db_path, subject="ask:answer_length:short",
                             audience="author", author_label="From their own ratings", authority="system",
                             origin="ratings")
    assert "Prefers short" in memory_context.memory_context(rid, "ask", viewer=MANAGER, db_path=db_path).text
    assert "Prefers short" not in memory_context.memory_context(rid, "brief", viewer=MANAGER, db_path=db_path).text
    assert "Prefers short" not in memory_context.memory_context(rid, "digest", db_path=db_path).text


def test_the_brief_audience_of_a_result_uses_the_one_metric_rule():
    import permissions, strategy_jobs
    assert strategy_jobs._metric_permissions("item_waste:Salmon") == {permissions.FOOD_COST_VIEW}
    assert strategy_jobs._metric_permissions("void_rate") == {permissions.LOSS_VIEW}
