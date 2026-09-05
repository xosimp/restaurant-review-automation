"""Ask Cavnar's tool layer.

The safety property under test: read tools execute, write tools never do.
A write tool call produces a proposal for the owner to confirm — it must
not reach the route that emails a supplier or posts a public reply.
"""
import json

import pytest
from flask import Flask

import ask_cavnar
import ask_cavnar_tools as tools
import auth
import client_api
import models
from client_api import client_bp
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api, tools):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Tool Co")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _login_as(monkeypatch, rid):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 7, "restaurant_id": rid, "is_admin": 0,
                                 "username": "owner", "email": "o@x.test"})


def _review(db_path, rid, text, rating=2, sentiment="negative", urgency="normal", draft=None):
    import uuid
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
        "review_date, fetched_at, processed, sentiment, urgency, draft_response, response_status) "
        "VALUES (?,?,?,?,?,?,date('now'),datetime('now'),1,?,?,?,'pending')",
        (rid, "google", f"ext-{uuid.uuid4().hex[:12]}", "Guest", rating, text, sentiment, urgency, draft))
    conn.commit()
    conn.close()


# ── Registry integrity ───────────────────────────────────────────────────

def test_every_tool_declares_a_kind_and_a_schema():
    for t in tools.TOOLS:
        assert t["kind"] in ("read", "write")
        spec = t["spec"]
        assert spec["name"] and spec["description"]
        assert spec["input_schema"]["type"] == "object"


def test_every_write_tool_requires_confirmation_and_names_a_route():
    """A write tool with no route, or one that doesn't require confirming,
    would be a way to act without the owner seeing it."""
    for t in tools.TOOLS:
        if t["kind"] != "write":
            continue
        assert t.get("confirm") is True, f"{t['spec']['name']} must require confirmation"
        assert t["route"]["web"] and t["route"]["mobile"] and t["route"]["method"]


def test_tool_specs_expose_only_the_api_shape():
    for spec in tools.tool_specs():
        assert set(spec) == {"name", "description", "input_schema"}


# ── Read tools ───────────────────────────────────────────────────────────

def test_read_reviews_returns_individual_reviews(db_path):
    rid = _restaurant(db_path)
    _review(db_path, rid, "The patio is lovely but it is loud inside")
    _review(db_path, rid, "Great pizza, no notes", rating=5, sentiment="positive")
    out = json.loads(tools.run_read_tool("read_reviews", rid, {}))
    assert out["count"] == 2
    assert {r["rating"] for r in out["reviews"]} == {2, 5}


def test_read_reviews_can_search_text(db_path):
    """The gap that made "which reviews mention the patio" unanswerable."""
    rid = _restaurant(db_path)
    _review(db_path, rid, "The patio is lovely")
    _review(db_path, rid, "Parking was terrible")
    out = json.loads(tools.run_read_tool("read_reviews", rid, {"search": "patio"}))
    assert out["count"] == 1 and "patio" in out["reviews"][0]["text"]


def test_read_reviews_never_crosses_restaurants(db_path):
    mine, theirs = _restaurant(db_path), _restaurant(db_path, name="Other Co")
    _review(db_path, theirs, "Their review")
    out = json.loads(tools.run_read_tool("read_reviews", mine, {}))
    assert out["count"] == 0


def test_read_reviews_caps_the_row_count(db_path):
    rid = _restaurant(db_path)
    for i in range(30):
        _review(db_path, rid, f"Review number {i}")
    out = json.loads(tools.run_read_tool("read_reviews", rid, {"limit": 999}))
    assert out["count"] <= 20


def test_a_failing_read_tool_returns_an_error_not_an_exception(db_path, monkeypatch):
    """A broken tool should let the model say so, not collapse the chat."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(tools, "_read_menu_margins",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    tools._BY_NAME["read_menu_margins"]["fn"] = tools._read_menu_margins
    out = json.loads(tools.run_read_tool("read_menu_margins", rid, {}))
    assert "error" in out


def test_an_unknown_tool_name_is_rejected(db_path):
    out = json.loads(tools.run_read_tool("rm_rf_everything", _restaurant(db_path), {}))
    assert "error" in out


def test_a_write_tool_cannot_be_run_as_a_read_tool(db_path):
    """Belt and braces: even asked directly, the read path refuses it."""
    out = json.loads(tools.run_read_tool("send_supplier_order", _restaurant(db_path), {}))
    assert "error" in out


# ── Write proposals ──────────────────────────────────────────────────────

def test_a_write_tool_builds_a_proposal_not_a_send():
    p = tools.build_proposal("send_supplier_order", {"supplier_email": "orders@fresh.test"})
    assert p["action"] == "send_supplier_order"
    assert p["requires_confirmation"] is True
    assert p["route"]["web"] == "/api/food-cost/send-order"
    assert p["body"] == {"supplier_email": "orders@fresh.test"}
    assert "orders@fresh.test" in p["summary"]


def test_a_proposal_without_a_named_supplier_says_every_supplier():
    assert "every supplier" in tools.build_proposal("send_supplier_order", {})["summary"]


def test_is_write_tool_classifies_correctly():
    assert tools.is_write_tool("send_supplier_order") is True
    assert tools.is_write_tool("read_reviews") is False
    assert tools.is_write_tool("nonexistent") is False


# ── The loop ─────────────────────────────────────────────────────────────

class _Block:
    def __init__(self, name, tool_input, block_id="tu_1"):
        self.type, self.name, self.input, self.id = "tool_use", name, tool_input, block_id


class _Msg:
    def __init__(self, stop_reason, content):
        self.stop_reason, self.content = stop_reason, content


def test_a_write_tool_call_yields_a_proposal_and_never_executes(db_path, monkeypatch):
    """The core safety test: the model asking to send must not send."""
    rid = _restaurant(db_path)
    restaurant = models.get_restaurant(rid, db_path=db_path)

    fired = []
    monkeypatch.setattr("emails.send_supplier_order_email",
                        lambda **kw: fired.append(kw))

    calls = {"n": 0}
    def fake_create(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Msg("tool_use", [_Block("send_supplier_order", {"supplier_email": "orders@fresh.test"})])
        return _Msg("end_turn", [])
    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create)
    monkeypatch.setattr(ask_cavnar, "extract_text", lambda m: "Confirm below and it goes out.")

    answer, truncated, proposals = ask_cavnar.ask_with_tools(restaurant, "send the order")
    assert [p["action"] for p in proposals] == ["send_supplier_order"]
    assert fired == [], "a proposal must never actually send"
    assert "Confirm" in answer


def test_a_read_tool_call_executes_and_feeds_back(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _review(db_path, rid, "The patio is lovely")
    restaurant = models.get_restaurant(rid, db_path=db_path)

    seen = {}
    calls = {"n": 0}
    def fake_create(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Msg("tool_use", [_Block("read_reviews", {"search": "patio"})])
        seen["messages"] = kw["messages"]
        return _Msg("end_turn", [])
    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create)
    monkeypatch.setattr(ask_cavnar, "extract_text", lambda m: "One review mentions the patio.")

    answer, _, proposals = ask_cavnar.ask_with_tools(restaurant, "which reviews mention the patio?")
    assert proposals == []
    tool_results = [b for m in seen["messages"] if isinstance(m.get("content"), list)
                    for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert tool_results and "patio" in tool_results[0]["content"]


def test_the_loop_stops_after_a_bounded_number_of_rounds(db_path, monkeypatch):
    """A model that keeps asking for tools must not loop forever."""
    rid = _restaurant(db_path)
    restaurant = models.get_restaurant(rid, db_path=db_path)
    calls = {"n": 0}
    def always_tools(*a, **kw):
        calls["n"] += 1
        return _Msg("tool_use", [_Block("read_reviews", {})])
    monkeypatch.setattr(ask_cavnar, "create_with_retry", always_tools)
    monkeypatch.setattr(ask_cavnar, "extract_text", lambda m: "done")
    ask_cavnar.ask_with_tools(restaurant, "loop forever")
    assert calls["n"] <= ask_cavnar._MAX_TOOL_ROUNDS + 1


# ── Persistence + audit ──────────────────────────────────────────────────

def test_conversation_survives_and_replays_oldest_first(db_path):
    from models import save_ask_message, get_ask_history
    rid = _restaurant(db_path)
    save_ask_message(rid, "user", "first question", db_path=db_path)
    save_ask_message(rid, "assistant", "first answer", db_path=db_path)
    history = get_ask_history(rid, db_path=db_path)
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert history[0]["content"] == "first question"


def test_history_is_scoped_to_the_restaurant(db_path):
    from models import save_ask_message, get_ask_history
    mine, theirs = _restaurant(db_path), _restaurant(db_path, name="Other Co")
    save_ask_message(theirs, "user", "their secret", db_path=db_path)
    assert get_ask_history(mine, db_path=db_path) == []


def test_proposals_are_stored_with_the_assistant_turn(db_path):
    from models import save_ask_message, get_ask_history
    rid = _restaurant(db_path)
    save_ask_message(rid, "assistant", "confirm?", db_path=db_path,
                     proposals=[{"action": "send_supplier_order"}])
    assert get_ask_history(rid, db_path=db_path)[0]["proposals"][0]["action"] == "send_supplier_order"


def test_an_action_is_audited_from_proposal_through_outcome(db_path):
    from models import log_ask_action, get_ask_actions
    rid = _restaurant(db_path)
    log_ask_action(rid, "send_supplier_order", summary="Email Fresh Co", outcome="proposed", db_path=db_path)
    log_ask_action(rid, "send_supplier_order", summary="Email Fresh Co", outcome="confirmed",
                   user_id=7, db_path=db_path)
    outcomes = [a["outcome"] for a in get_ask_actions(rid, db_path=db_path)]
    assert outcomes == ["confirmed", "proposed"]


def test_clearing_history_leaves_the_action_audit_intact(db_path):
    """Deleting a chat must not erase the record of what was actually done."""
    from models import save_ask_message, clear_ask_history, log_ask_action, get_ask_actions
    rid = _restaurant(db_path)
    save_ask_message(rid, "user", "send it", db_path=db_path)
    log_ask_action(rid, "send_supplier_order", outcome="confirmed", db_path=db_path)
    clear_ask_history(rid, db_path=db_path)
    assert len(get_ask_actions(rid, db_path=db_path)) == 1


# ── Routes ───────────────────────────────────────────────────────────────

def test_the_action_route_records_an_outcome(client, db_path, monkeypatch):
    from models import get_ask_actions
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    resp = client.post("/api/ask-cavnar/action",
                       json={"action": "send_supplier_order", "outcome": "confirmed",
                             "summary": "Email Fresh Co"})
    assert resp.get_json()["ok"] is True
    assert get_ask_actions(rid, db_path=db_path)[0]["outcome"] == "confirmed"


def test_the_action_route_rejects_a_bogus_outcome(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    resp = client.post("/api/ask-cavnar/action",
                       json={"action": "send_supplier_order", "outcome": "definitely_sent"})
    assert resp.status_code == 400


def test_history_routes_round_trip(client, db_path, monkeypatch):
    from models import save_ask_message
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    save_ask_message(rid, "user", "hello", db_path=db_path)
    assert len(client.get("/api/ask-cavnar/history").get_json()["messages"]) == 1
    assert client.delete("/api/ask-cavnar/history").get_json()["ok"] is True
    assert client.get("/api/ask-cavnar/history").get_json()["messages"] == []
