"""The iPhone answer card (iOS readability round, 10/8/26: "Web explains.
iPhone decides.").

The phone's Ask routes ask for a labelled lead — a headline, one sentence,
Cause / Do first / Expect / Follow-ups, a "---" line, then the detail — and
the server reads the card out of the VALIDATED answer deterministically
(ask_cavnar.answer_card, no second model call). These pin the parser, the
surface flag on the routes, the card on both wires, and a reopened chat
getting the same card, confidence and evidence back."""
import json
import re
import types

import pytest
from flask import Flask

import ai_utils
import ask_cavnar
import auth
import client_api
import models
from client_api import client_bp
from models import Restaurant, create_restaurant

LEAD = (
    "Labor ran 31% last week, 3 points over your 28% target.\n"
    "That is about $410 more than plan for the week.\n"
    "Cause: because two closers stayed past 11pm on slow weeknights.\n"
    "Do first: Cut one closer to 10pm on Tuesday and Wednesday.\n"
    "Expect: If you do, about $85 a night back.\n"
    "Follow-ups: Which nights ran longest? | What did Friday look like? | How is overtime?\n"
)
DETAIL = ("Two closers clocked out after 11:30pm on four slow nights.\n\n"
          "- Cut one closer to 10pm on Tuesday and Wednesday.\n- Check the Friday close.")
ANSWER = LEAD + "\n---\n\n" + DETAIL


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **kw: False)


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Card Co")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


# ── the parser ──────────────────────────────────────────────────────────────

def test_a_well_formed_lead_is_a_card_and_the_rest_is_the_detail():
    card, detail = ask_cavnar.answer_card(ANSWER, {})
    assert card["headline"] == "Labor ran 31% last week, 3 points over your 28% target."
    assert card["summary"] == "That is about $410 more than plan for the week."
    # The label carries the "because"; the card says the reason itself.
    assert card["cause"] == "Two closers stayed past 11pm on slow weeknights."
    assert card["cause_flagged"] is False
    assert card["action"] == "Cut one closer to 10pm on Tuesday and Wednesday."
    assert card["action_key"] == ask_cavnar.suggestion_key(card["action"])
    assert card["outcome"] == "If you do, about $85 a night back."
    assert card["follow_ups"] == ["Which nights ran longest?", "What did Friday look like?", "How is overtime?"]
    assert detail == DETAIL


def test_missing_parts_collapse_and_markdown_is_cleaned():
    text = ("**Reviews are steady at 4.6 stars.**\n"
            "**Do first:** Reply to the two 2-star reviews from Saturday.\n"
            "---\nThe detail.")
    card, detail = ask_cavnar.answer_card(text, {})
    assert card["headline"] == "Reviews are steady at 4.6 stars."
    assert card["summary"] is None and card["cause"] is None and card["outcome"] is None
    assert card["action"] == "Reply to the two 2-star reviews from Saturday."
    assert card["follow_ups"] == []
    assert detail == "The detail."


@pytest.mark.parametrize("text", [
    "",
    "Labor ran 31% last week.",                                   # a plain answer — every web answer
    LEAD + DETAIL,                                                 # no "---" line
    "Labor ran 31%.\nThat matters.\n---\nDetail.",                 # no labelled line at all
    "Labor ran 31%.\nThat matters.\nA third prose line.\nDo first: Cut a closer.\n---\nDetail.",
    "Cause: one.\nCause: two.\nHeadline here.\n---\nDetail.",      # the same label twice
    ask_cavnar.ASK_REFUSED_ANSWER,
    "x" * 260 + "\nDo first: Cut one closer on Tuesday.\n---\nDetail.",  # not a headline
])
def test_anything_off_contract_is_no_card(text):
    assert ask_cavnar.answer_card(text, {}) == (None, None)


def test_the_parser_never_raises():
    assert ask_cavnar.answer_card(None, None) == (None, None)
    assert ask_cavnar.answer_card(ANSWER, {"unsupported_causes": [None, 3], "unverified_figures": [None]})[0]


def test_a_cause_the_check_could_not_support_is_flagged():
    meta = {"unsupported_causes": ["Cause: because two closers stayed past 11pm on slow weeknights."]}
    card, _ = ask_cavnar.answer_card(ANSWER, meta)
    assert card["cause_flagged"] is True
    # A flagged sentence about something else leaves the Cause line alone.
    other = {"unsupported_causes": ["Sales dipped because of the rain on Saturday."]}
    assert ask_cavnar.answer_card(ANSWER, other)[0]["cause_flagged"] is False


def test_the_card_invents_no_figure():
    """Every figure on the card is one the answer said — the card is read
    out of the text, never written."""
    card, _ = ask_cavnar.answer_card(ANSWER, {})
    figures = re.findall(r"\$?\d[\d,.]*%?", " ".join(str(v) for k, v in card.items() if isinstance(v, str) and k != "action_key"))
    assert figures and all(f in ANSWER for f in figures)


def test_an_expect_line_is_conditional_untraced_free_and_never_a_sum():
    def outcome(line, meta=None):
        text = LEAD.replace("Expect: If you do, about $85 a night back.", line) + "---\nDetail."
        return ask_cavnar.answer_card(text, meta or {})[0]["outcome"]
    assert outcome("Expect: If you do, about $85 a night back.") is not None
    # Untraced by the answer's own check: off the card.
    assert outcome("Expect: If you do, about $85 a night back.", {"unverified_figures": ["$85"]}) is None
    # A promise is not an expectation.
    assert outcome("Expect: $85 a night back.") is None
    # Two figures added together are never one outcome.
    assert outcome("Expect: If you do both, about $85 plus $40, $125 a night combined.") is None


def test_an_untraced_figure_keeps_a_follow_up_off_and_the_action_unkeyed():
    meta = {"unverified_figures": ["10pm"]}
    card, _ = ask_cavnar.answer_card(ANSWER, meta)
    assert card["action_key"] is None
    meta = {"unverified_figures": ["Friday"]}
    assert "What did Friday look like?" not in ask_cavnar.answer_card(ANSWER, meta)[0]["follow_ups"]


def test_the_do_first_line_is_a_suggestion_once():
    items = ask_cavnar.extract_suggestions(ANSWER)
    texts = [i["text"] for i in items]
    assert texts[0] == "Cut one closer to 10pm on Tuesday and Wednesday."
    assert texts.count("Cut one closer to 10pm on Tuesday and Wednesday.") == 1
    assert "Check the Friday close." in texts


def test_finish_checks_the_cause_line_as_the_cause_it_states(db_path, monkeypatch):
    """The "Cause:" label makes any sentence a cause claim even without a
    "because" in it: _finish checks it against what the answer read, and
    nothing this restaurant's data says names the closers."""
    text = LEAD.replace("because two closers", "two closers") + "---\nDetail."
    assert ask_cavnar.answer_card(text, {})[0]["cause_flagged"] is False   # the parse alone cannot tell
    r = models.get_restaurant(_restaurant(db_path), db_path=db_path)
    _capture_calls(monkeypatch, text)
    _a, _t, _p, meta = ask_cavnar.ask_with_tools(r, "How do I get labor down?", surface="ios")
    assert meta["card"]["cause_flagged"] is True


def test_the_cause_probe_passes_a_cause_the_data_states():
    corpus = ["Labor ran high because two closers stayed past 11pm on slow weeknights."]
    assert ask_cavnar._card_cause_unsupported("Two closers stayed past 11pm on slow weeknights.", corpus) is False
    assert ask_cavnar._card_cause_unsupported("The new POS dropped tickets.", corpus) is True


def test_the_stored_view_keys_are_the_ones_history_returns():
    assert tuple(ask_cavnar.turn_view({}).keys()) == models.ASK_TURN_VIEW_KEYS


# ── the prompt: per turn, phone only ────────────────────────────────────────

def _capture_calls(monkeypatch, text):
    seen = []

    def fake_create(client, **kwargs):
        seen.append(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], stop_reason="end_turn")
    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create)
    return seen


def _system(kwargs):
    return "\n".join(b["text"] for b in kwargs["system"])


def _user_turn(kwargs):
    c = kwargs["messages"][-1]["content"]
    return "".join(b["text"] for b in c) if isinstance(c, list) else c


def test_the_iphone_contract_is_a_per_turn_block_on_the_phone_only(db_path, monkeypatch):
    r = models.get_restaurant(_restaurant(db_path), db_path=db_path)
    seen = _capture_calls(monkeypatch, ANSWER)
    answer, _t, _p, meta = ask_cavnar.ask_with_tools(r, "How do I get labor down?", surface="ios")
    blocks = seen[-1]["system"]
    # Never in a cached block: the static rules and the snapshot stay the web's.
    assert all(ask_cavnar._IPHONE_NOTE not in b["text"] for b in blocks if b.get("cache_control"))
    assert any(b["text"] == ask_cavnar._IPHONE_NOTE and not b.get("cache_control") for b in blocks)
    assert ask_cavnar._IPHONE_TURN_NOTE.strip() in _user_turn(seen[-1])
    # Parsed after validation, carried on the meta.
    assert meta["card"]["headline"].startswith("Labor ran 31%") and meta["detail"]
    assert answer.startswith("Labor ran 31%")

    seen = _capture_calls(monkeypatch, ANSWER)
    ask_cavnar.ask_with_tools(r, "How do I get labor down?")
    assert ask_cavnar._IPHONE_NOTE not in _system(seen[-1])
    assert ask_cavnar._IPHONE_TURN_NOTE.strip() not in _user_turn(seen[-1])


def test_the_phone_gets_less_executive_room_and_the_web_keeps_its_own(db_path, monkeypatch):
    r = models.get_restaurant(_restaurant(db_path), db_path=db_path)
    q = "What should I focus on this week?"
    assert ask_cavnar._depth_for(q) == "executive"
    seen = _capture_calls(monkeypatch, "fine")
    ask_cavnar.ask_with_tools(r, q, surface="ios")
    assert seen[-1]["max_tokens"] == ask_cavnar._IOS_MAX_TOKENS["executive"]
    seen = _capture_calls(monkeypatch, "fine")
    ask_cavnar.ask_with_tools(r, q)
    assert seen[-1]["max_tokens"] == ask_cavnar._MAX_TOKENS["executive"]
    # A standard question is the same size on both.
    seen = _capture_calls(monkeypatch, "fine")
    ask_cavnar.ask_with_tools(r, "How are reviews?", surface="ios")
    assert seen[-1]["max_tokens"] == ask_cavnar._MAX_TOKENS["standard"]


# ── the routes ──────────────────────────────────────────────────────────────

def _routes_app():
    import mobile_api
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def test_the_mobile_routes_send_surface_ios_and_the_web_routes_do_not(db_path, monkeypatch):
    seen = []
    monkeypatch.setattr(client_api, "_do_ask_cavnar",
                        lambda *a, **k: (seen.append(("plain", k.get("surface"))) or ({"ok": True}, 200)))
    monkeypatch.setattr(client_api, "_ask_cavnar_stream_response",
                        lambda *a, **k: (seen.append(("stream", k.get("surface"))) or ({"ok": True}, 200)))
    rid = _restaurant(db_path)
    owner = {"id": 7, "restaurant_id": rid, "role": "client", "is_admin": 0, "username": "o", "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: owner)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: owner, raising=False)
    c = _routes_app().test_client()
    body, hdr = {"question": "labor?"}, {"Authorization": "Bearer t"}
    c.post("/api/ask-cavnar", json=body)
    c.post("/api/ask-cavnar/stream", json=body)
    c.post("/mobile/api/ask-cavnar", json=body, headers=hdr)
    c.post("/mobile/api/ask-cavnar/stream", json=body, headers=hdr)
    assert seen == [("plain", None), ("stream", None), ("plain", "ios"), ("stream", "ios")]


def test_the_plain_route_forwards_the_surface_and_returns_the_card(db_path, monkeypatch):
    rid = _restaurant(db_path)
    got = {}

    def fake_ask(restaurant, question, **kw):
        got.update(kw)
        return ANSWER, False, [], {"confidence_detail": {"pct": 72, "band": "medium"},
                                   "modules_consulted": ["labor"], "unverified_figures": []}
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", fake_ask)
    payload, status = client_api._do_ask_cavnar(rid, "How do I get labor down?", user_id=7, surface="ios")
    assert status == 200 and got.get("surface") == "ios"
    assert payload["answer"] == ANSWER, "the whole text, unchanged, for older builds"
    assert payload["card"]["action"] == "Cut one closer to 10pm on Tuesday and Wednesday."
    assert payload["detail"] == DETAIL
    # The Do first line is answerable, under the key the card names.
    keys = [s["rec_key"] for s in payload["suggestions"]]
    assert payload["card"]["action_key"] in keys

    got.clear()
    payload, _ = client_api._do_ask_cavnar(rid, "How do I get labor down?", user_id=7)
    assert "surface" not in got


def test_a_plain_answer_has_no_card_and_is_returned_as_it_was(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", lambda *a, **k: ("Reviews look steady.", False, [], {}))
    payload, _ = client_api._do_ask_cavnar(rid, "Reviews?", user_id=7, surface="ios")
    assert payload["card"] is None and payload["detail"] is None
    assert payload["answer"] == "Reviews look steady."


def test_the_stream_answer_event_carries_the_card(db_path, monkeypatch):
    rid = _restaurant(db_path)
    owner = {"id": 7, "restaurant_id": rid, "role": "client", "is_admin": 0, "username": "o", "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: owner)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: owner, raising=False)
    got = {}

    def fake_ask(restaurant, question, **kw):
        got.update(kw)
        return ANSWER, False, [], {}
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", fake_ask)
    c = _routes_app().test_client()
    raw = c.post("/mobile/api/ask-cavnar/stream", json={"question": "labor?"},
                 headers={"Authorization": "Bearer t"}).get_data(as_text=True)
    events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
    answer = next(e for e in events if e["type"] == "answer")
    assert got.get("surface") == "ios"
    assert answer["card"]["headline"].startswith("Labor ran 31%") and answer["detail"] == DETAIL


def test_a_reopened_chat_keeps_its_card_confidence_and_evidence(db_path, monkeypatch):
    rid = _restaurant(db_path)
    detail = {"pct": 72, "band": "medium", "label": "72% confidence"}
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", lambda *a, **k: (
        ANSWER, False, [], {"confidence_detail": detail, "modules_consulted": ["labor"],
                            "unverified_figures": ["$410"], "validation": {"caveats": ["1 figure unverified"]}}))
    payload, _ = client_api._do_ask_cavnar(rid, "How do I get labor down?", user_id=7, surface="ios")
    cid = payload["conversation_id"]
    out, status = client_api._do_get_ask_conversation(rid, cid, viewer_id=7)
    assert status == 200
    user_turn, answer = out["messages"]
    assert "meta" not in user_turn
    meta = answer["meta"]
    assert meta["card"] == payload["card"]
    assert meta["confidence_detail"]["pct"] == 72
    assert meta["modules_consulted"] == ["labor"] and meta["unverified_figures"] == ["$410"]
    assert meta["caveats"] == ["1 figure unverified"]
    assert answer["detail"] == DETAIL
    # Never the turn's tools or ids.
    assert set(meta) == set(models.ASK_TURN_VIEW_KEYS)


def test_a_stored_turn_meta_is_always_whole_json(db_path):
    rid = _restaurant(db_path)
    big = {"depth": "standard", "turn_id": "ask:1",
           "view": {"confidence_detail": {"basis": "x" * 20000}, "card": {"headline": "h"}}}
    models.save_ask_message(rid, "user", "q", db_path=db_path)
    models.save_ask_message(rid, "assistant", "a", meta=big, db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        raw = conn.execute("SELECT meta_json FROM ask_cavnar_messages WHERE role='assistant'").fetchone()[0]
    finally:
        conn.close()
    stored = json.loads(raw)
    assert stored["turn_id"] == "ask:1"
    assert stored["view"]["confidence_detail"] is None and stored["view"]["card"] == {"headline": "h"}


# ── the phone reads it ──────────────────────────────────────────────────────

def _swift(*parts):
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "ios", "CavnarAI", "CavnarAI", *parts), encoding="utf-8") as f:
        return f.read()


def test_the_phone_decodes_the_card_on_both_wires_and_on_reopen():
    vm = _swift("Features", "AskCavnar", "AskCavnarViewModel.swift")
    api = _swift("Core", "APIClient.swift")
    for src in (vm, api):
        assert "case card, detail, depth, validation" in src
    assert 'case causeFlagged = "cause_flagged"' in vm and 'case followUps = "follow_ups"' in vm
    # A reopened answer keeps what it was shown with (#90).
    assert "var meta: AskStoredView?" in vm and 'case confidence = "confidence_detail"' in vm
    # Per-suggestion confidence is read (the meter on each Worth doing line).
    assert "case text, answerable, confidence" in vm


def test_the_bubble_draws_the_card_with_the_answer_kit():
    view = _swift("Features", "AskCavnar", "AskCavnarView.swift")
    assert "CavnarAnswerCard(" in view and 'detailLabel: "Full analysis"' in view
    assert "isHypothesis: card.causeFlagged" in view
    # Proposals stay visible on the card; "Sent with this" is a kicker.
    assert "ProposalCard(proposal: first, viewModel: viewModel)" in view
    assert 'CavnarKicker("Sent with this")' in view
    # The per-bubble label is gone; warnings are amber caveats, not red lines.
    assert 'Text("CAVNAR AI")' not in view and "struct FlowChips" not in view
    assert "AccountFlowLayout(" in view and "caveatCards" in view
    # A new answer lands at its top (#36).
    assert "proxy.scrollTo(last.id, anchor: .top)" in view and "anchor: .center" not in view


def test_siri_speaks_the_card():
    siri = _swift("Core", "CavnarAppIntents.swift")
    assert "card: r.card?.card, confidence: r.confidenceDetail" in siri
    assert "static func spokenLead(" in siri
