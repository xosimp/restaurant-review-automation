"""The Command Center's server half (command_center.py, Friction audit #14,
#48) and the nav paths every "take me there" payload now carries (#2).

  * the registry lists only what this login may run here — the same test
    Ask's tools pass (tool_allowed + the route's own permission);
  * search is permission-filtered and never reaches a hidden module;
  * propose builds Ask's confirm card with no model call, strips the
    denylisted switches, and logs a "proposed" row Ask's route settles;
  * a stored proposal reopens without a model call, scoped like Ask's route;
  * attention items, queue items and notification rows carry a `nav`.
"""
import json

import pytest

import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import action_queue, client_api, issues, ask_cavnar_tools, push, auth
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, action_queue, client_api, issues, ask_cavnar_tools, push, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Command Co")
    kw.setdefault("owner_email", "o@x.test")
    for m in ("module_reviews", "module_labor", "module_inventory", "module_marketing"):
        kw.setdefault(m, 1)
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _user(rid, role="client", uid=1):
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "role": role, "is_admin": False}


def _review(db_path, rid, author="Dana", rating=1, status="drafted", text="The fish was cold."):
    import uuid
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                       "draft_response, review_date, fetched_at, processed, response_status) "
                       "VALUES (?,'google',?,?,?,?,'Thank you.',date('now'),datetime('now'),1,?)",
                       (rid, uuid.uuid4().hex[:12], author, rating, text, status))
    conn.commit()
    rv = cur.lastrowid
    conn.close()
    return rv


# ── registry ─────────────────────────────────────────────────────────────────

def test_registry_lists_places_actions_and_ask_for_an_owner(db_path):
    import command_center
    rid = _rid(db_path)
    out, status = command_center.registry(_user(rid))
    assert status == 200 and out["ok"]
    by_id = {c["id"]: c for c in out["commands"]}
    assert by_id["go:labor/schedule"]["nav"] == "labor/schedule"
    assert by_id["go:account/notifications"]["nav"] == "account/notifications"
    assert by_id["action:approve_all_reviews"]["kind"] == "action"
    assert by_id["action:approve_all_reviews"]["tier"] == 2, "posting publicly is outward"
    assert by_id["action:generate_schedule"]["tier"] == 1
    assert by_id["ask"]["kind"] == "ask"
    for c in out["commands"]:
        assert set(c) == {"id", "label", "keywords", "kind", "tier", "nav", "action", "args"}
        assert c["kind"] in ("nav", "action", "ask") and c["tier"] in (0, 1, 2)


def test_registry_hides_what_a_manager_would_be_refused(db_path):
    """A manager holds no FOOD_COST_VIEW: no Food Cost places, no supplier
    order. A legacy teammate holds no SCHEDULE_PUBLISH: no "Send the
    schedule". Neither is an account holder: no Billing or People."""
    import command_center
    rid = _rid(db_path)
    mgr = {c["id"] for c in command_center.registry(_user(rid, "manager", 2))[0]["commands"]}
    assert not any(i.startswith("go:inventory") for i in mgr)
    assert "action:send_supplier_order" not in mgr
    assert "action:publish_schedule" in mgr
    assert "go:account/billing" not in mgr and "go:account/people" not in mgr
    member = {c["id"] for c in command_center.registry(_user(rid, "member", 3))[0]["commands"]}
    assert "action:publish_schedule" not in member
    assert "action:generate_schedule" in member


def test_registry_follows_the_restaurants_modules(db_path):
    import command_center
    rid = _rid(db_path, module_labor=0, module_inventory=0)
    ids = {c["id"] for c in command_center.registry(_user(rid))[0]["commands"]}
    assert "go:labor" not in ids and "action:publish_schedule" not in ids
    assert "go:intel" not in ids, "Intel needs all four modules and a listing"
    assert "go:reviews" in ids


# ── search ───────────────────────────────────────────────────────────────────

def test_search_finds_a_review_a_place_and_nothing_hidden(db_path):
    import command_center
    rid = _rid(db_path)
    rv = _review(db_path, rid, author="Dana Kowalski")
    out, _ = command_center.search(_user(rid), "dana")
    review = [r for r in out["results"] if r["type"] == "review"]
    assert review and review[0]["nav"] == f"review/{rv}"
    assert review[0]["location"]["id"] == rid, "every result names its location"
    places = command_center.search(_user(rid), "schedule")[0]["results"]
    assert any(r["type"] == "place" and r["nav"] == "labor/schedule" for r in places)
    # A login whose role cannot read Reviews gets no review rows.
    rid2 = _rid(db_path, name="Two", module_reviews=0)
    _review(db_path, rid2, author="Dana Two")
    assert not [r for r in command_center.search(_user(rid2), "dana")[0]["results"] if r["type"] == "review"]
    assert command_center.search(_user(rid), "d")[0]["results"] == [], "one letter matches everything"


def test_search_never_reads_another_restaurant(db_path):
    import command_center
    a, b = _rid(db_path, name="A"), _rid(db_path, name="B", owner_email="b@x.test")
    _review(db_path, b, author="Zelda")
    assert not [r for r in command_center.search(_user(a), "zelda")[0]["results"] if r["type"] == "review"]


# ── propose ──────────────────────────────────────────────────────────────────

def test_propose_builds_asks_card_and_logs_it_proposed(db_path):
    import command_center
    rid = _rid(db_path)
    _review(db_path, rid, rating=5, text="Lovely night")
    out, status = command_center.propose(_user(rid), "approve_all_reviews", {"acknowledge": True})
    assert status == 200 and out["ok"]
    p = out["proposal"]
    assert p["action"] == "approve_all_reviews" and p["requires_confirmation"]
    assert p["route"]["web"] == "/api/reviews/approve-all"
    assert "acknowledge" not in p["body"], "the denylist applies to palette arguments too"
    assert p["tier"] == 2 and p["surface"] == "command"
    row = models.get_ask_proposal(rid, p["proposal_id"], db_path=db_path)
    assert row and row["outcome"] == "proposed" and row["user_id"] == 1


def test_propose_refuses_what_ask_would_refuse(db_path):
    import command_center
    rid = _rid(db_path)
    assert command_center.propose(_user(rid), "read_reviews", {})[1] == 400, "a read is not an action"
    assert command_center.propose(_user(rid, "manager", 2), "send_supplier_order", {})[1] == 403
    out, status = command_center.propose(_user(rid), "send_guest_campaign",
                                         {"message": "Tonight only! evil.example.com/deal"})
    assert status == 400 and "own website" in out["error"]
    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM ask_cavnar_actions WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 0, "a refused proposal writes nothing"


# ── reopen a stored proposal ─────────────────────────────────────────────────

def test_a_stored_proposal_reopens_without_a_model_call(db_path, monkeypatch):
    import command_center, ask_cavnar
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", lambda *a, **k: pytest.fail("no model call"), raising=False)
    rid = _rid(db_path)
    rv = _review(db_path, rid, rating=4)
    pid = models.log_ask_action(rid, "approve_review", summary=f"Approve and post the reply to review #{rv}",
                                body={}, outcome="proposed", user_id=1, db_path=db_path)
    out, status = command_center.reopen(_user(rid), pid)
    assert status == 200 and out["proposal"]["proposal_id"] == pid
    assert out["proposal"]["route"]["web"] == f"/approve/{rv}", "the review id comes back from the summary"
    # Settled: says how it ended, no card.
    models.log_ask_action(rid, "approve_review", outcome="dismissed", proposal_id=pid, db_path=db_path)
    out, _ = command_center.reopen(_user(rid), pid)
    assert out["proposal"] is None and out["settled"]["outcome"] == "dismissed"


def test_reopen_is_scoped_to_the_restaurant_and_the_login(db_path):
    import command_center
    a, b = _rid(db_path, name="A"), _rid(db_path, name="B", owner_email="b@x.test")
    pid = models.log_ask_action(a, "generate_schedule", summary="Generate next week's schedule",
                                outcome="proposed", user_id=1, db_path=db_path)
    assert command_center.reopen(_user(b), pid)[1] == 404, "another restaurant's proposal is not found"
    assert command_center.reopen(_user(a, "manager", 7), pid)[1] == 404, "another login's, for a non-holder"
    assert command_center.reopen(_user(a, "client", 9), pid)[1] == 200, "an account holder sees it"


# ── routes: both twins, and on the ungated (cross-module) list ───────────────

def test_command_routes_exist_on_web_and_mobile():
    from flask import Flask
    import auth, strategy_routes
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    rules = {r.rule for r in app.url_map.iter_rules()}
    for rule in rules:
        if "/command/" in rule:
            assert any(rule.startswith(p) for p in auth._UNGATED_PREFIXES), "listed as cross-module"
    for path in ("/command/registry", "/command/search", "/command/propose",
                 "/ask-cavnar/proposals/<int:proposal_id>"):
        assert "/api" + path in rules and "/mobile/api" + path in rules, path


# ── nav on every "take me there" payload (Friction audit #2) ─────────────────

def test_notification_rows_carry_where_they_open(db_path):
    import client_api
    rid = _rid(db_path)
    rv = _review(db_path, rid)
    conn = get_conn(db_path)
    for t, r in (("1star", rv), ("morning_brief", None), ("schedule_publish_held", None), ("login", None),
                 ("issue_escalated", None)):
        conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, review_id) VALUES (?,?,?)", (rid, t, r))
    conn.commit(); conn.close()
    rows = {n["type"]: n["nav"] for n in client_api._do_get_notifications(rid)[0]["notifications"]}
    assert rows["1star"] == f"review/{rv}"
    assert rows["morning_brief"].startswith("ask?q="), "a brief opens Ask on its question"
    assert rows["schedule_publish_held"] == "labor/schedule"
    assert rows["login"] == "account/security"
    assert rows["issue_escalated"] == "issue", "issues open Home's Needs attention, as on iOS"


def test_queue_items_carry_the_item_they_finish(db_path):
    import action_queue, issues
    rid = _rid(db_path)
    _review(db_path, rid, rating=1)
    iid = issues.create_issue(rid, "manual", "Walk-in is warm", severity="high", db_path=db_path)
    pid = models.log_ask_action(rid, "generate_schedule", summary="Generate next week's schedule",
                                outcome="proposed", db_path=db_path)
    out = {i["key"]: i for i in action_queue.items(rid, db_path=db_path)["items"]}
    issue_id = iid[0]["id"]
    assert out[f"issue:{issue_id}"]["nav"] == f"issue/{issue_id}"
    assert out[f"ask:{pid}"]["nav"] == f"action/{pid}", "a proposal reopens, not re-asks"
    assert out[f"ask:{pid}"]["action"]["nav"] == f"action/{pid}"
    assert out["no_response"]["nav"] == "reviews?filter=pending"


def test_queue_nav_for_labor_items():
    import action_queue
    assert action_queue.nav_for({"key": "time_off:12", "kind": "time_off"}) == "request/time_off-12"
    assert action_queue.nav_for({"key": "shift_request:7", "kind": "shift_request"}) == "request/shift_request-7"
    assert action_queue.nav_for({"key": "schedule_unsent:88", "kind": "schedule"}) == "schedule/88"
    assert action_queue.nav_for({"key": "schedule:next-week", "kind": "schedule"}) == "labor/schedule"
    assert action_queue.nav_for({"key": "invoice:pending", "kind": "invoice"}) == "inventory/invoices"


def test_attention_and_quick_actions_name_their_focus():
    import home_brief
    assert home_brief.attention_nav("urgent_reviews", "reviews") == "reviews?filter=urgent"
    assert home_brief.attention_nav("critical_low", "inventory") == "inventory/order"
    assert home_brief.attention_nav("labor_over", "labor") == "labor"
    assert home_brief.attention_nav("anything", "competitor") == "intel"
    assert home_brief.QUICK_NAV["order"] == "inventory/order"
    src = open(home_brief.__file__, encoding="utf-8").read()
    assert '"nav": attention_nav(key, module)' in src, "every attention item's action carries its nav"
