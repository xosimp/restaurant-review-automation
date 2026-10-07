"""Ask Cavnar's AI cost audit fixes (10/7/26): #16, #17, #18, #29, #30, #32,
#33, #66, #97.

What these hold:

- the request shape every call sends: static | snapshot | per-turn system
  blocks, cache breakpoints on the static block, the snapshot and this
  turn's newest two user messages only, never more than the API's four, and
  none on a forced text-only call (#16, #29, #30);
- the tool schemas stay under a size ceiling with every tool still offered
  (#17);
- an executive question carries read_business_snapshot's result from the
  start, as a call and its result, without a round spent asking (#18);
- the cross-module brief and the labor analysis are computed once per
  question (#33);
- a round's reads run together and come back in the model's order (#32);
- older answers replay cut to 800 characters and still verify (#66);
- every question has its own correlation id (#97).
"""
import json
import threading
import time

import pytest

import ai_utils
import ask_cavnar
import ask_cavnar_tools as tools
import business_intelligence as bi
import models
from models import Restaurant, create_restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    import guest_marketing
    for mod in (models, tools, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    guest_marketing.init_guest_marketing(db_path=db_path)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **kw: False)
    monkeypatch.setattr(ask_cavnar, "get_client", lambda *a, **k: object())
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    ask_cavnar.invalidate_context()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Cost Audit Co")
    kw.setdefault("owner_email", "o@x.test")
    for flag in ("module_reviews", "module_labor", "module_inventory", "module_marketing"):
        kw.setdefault(flag, 0)
    rid = create_restaurant(Restaurant(**kw), db_path=db_path)
    return get_restaurant(rid, db_path=db_path)


class _Tool:
    def __init__(self, name, tool_input=None, block_id="tu_1"):
        self.type, self.name, self.input, self.id = "tool_use", name, tool_input or {}, block_id


class _Text:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Msg:
    def __init__(self, stop_reason, content):
        self.stop_reason, self.content = stop_reason, content


def _script(monkeypatch, replies):
    """Stub the model: each call returns the next reply; records each call's
    kwargs (messages copied as sent)."""
    calls = []

    def fake(client, **kw):
        kw = dict(kw)
        kw["messages"] = list(kw["messages"])
        calls.append(kw)
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        return r(kw) if callable(r) else r
    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake)
    return calls


def _marked(messages):
    """[(index, role)] of the messages whose last block carries a breakpoint."""
    out = []
    for i, m in enumerate(messages):
        c = m.get("content")
        if isinstance(c, list):
            if any(isinstance(b, dict) and b.get("cache_control") for b in c):
                assert isinstance(c[-1], dict) and c[-1].get("cache_control"), "only the last block is marked"
                out.append((i, m["role"]))
    return out


# ── #29 / #30: static | snapshot | per turn ─────────────────────────────────

def test_the_static_block_is_the_same_at_every_depth_and_the_depth_is_per_turn(db_path):
    for depth in ("brief", "standard", "executive"):
        blocks = ask_cavnar._system_blocks("R", "SNAP", depth)
        assert blocks[0]["text"] == ask_cavnar._SYSTEM_STATIC
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert blocks[1]["cache_control"] == {"type": "ephemeral"} and "SNAP" in blocks[1]["text"]
        assert all("cache_control" not in b for b in blocks[2:])
    assert ask_cavnar._DEPTH_EXECUTIVE.strip() not in ask_cavnar._SYSTEM_STATIC
    assert ask_cavnar._DEPTH_BRIEF.strip() not in ask_cavnar._SYSTEM_STATIC
    assert ask_cavnar._system_blocks("R", "S", "executive")[2]["text"] == ask_cavnar._DEPTH_EXECUTIVE.strip()
    assert len(ask_cavnar._system_blocks("R", "S", "standard")) == 2


def test_the_snapshot_carries_no_time_of_day_and_the_turn_does(db_path, monkeypatch):
    r = _restaurant(db_path)
    ctx = ask_cavnar.build_context(r)
    assert "Today's date:" in ctx and "Local time" not in ctx
    assert ask_cavnar._CONTEXT_TTL_SECONDS == 300
    calls = _script(monkeypatch, [_Msg("end_turn", [_Text("ok")])])
    ask_cavnar.ask_with_tools(r, "what time do we open")
    system = calls[0]["system"]
    assert system[0]["text"] == ask_cavnar._SYSTEM_STATIC
    assert system[1]["text"].startswith("Restaurant: Cost Audit Co") and "Local time" not in system[1]["text"]
    assert system[2]["text"].startswith("NOW\n- Local time:")


def test_the_snapshot_cache_is_keyed_on_the_restaurants_day(db_path, monkeypatch):
    from datetime import datetime
    import time_utils
    r = _restaurant(db_path)
    days = iter([datetime(2026, 10, 7, 23, 58), datetime(2026, 10, 7, 23, 59), datetime(2026, 10, 8, 0, 1)])
    current = {"now": next(days)}
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: current["now"])
    first = ask_cavnar.build_context(r)
    current["now"] = next(days)
    assert ask_cavnar.build_context(r) is first                  # same day: the cached copy
    current["now"] = next(days)
    nxt = ask_cavnar.build_context(r)
    assert nxt is not first and "October 08, 2026" in nxt


def test_data_state_follows_the_snapshot_uncached(db_path, monkeypatch):
    r = _restaurant(db_path, module_reviews=1)
    import data_health
    monkeypatch.setattr(data_health, "readiness", lambda *a, **k: dict(data_health.NOT_APPLICABLE,
                                                                       prompt_block="DATA STATE (test) line"))
    calls = _script(monkeypatch, [_Msg("end_turn", [_Text("ok")])])
    ask_cavnar.ask_with_tools(r, "how are my reviews")
    texts = [b["text"] for b in calls[0]["system"]]
    assert "DATA STATE" not in texts[1]
    ds = next(i for i, t in enumerate(texts) if t.startswith("DATA STATE"))
    assert ds > 1 and "cache_control" not in calls[0]["system"][ds]


# ── #16: message breakpoints ────────────────────────────────────────────────

def test_round_one_marks_the_question_and_later_rounds_the_newest_results(db_path, monkeypatch):
    r = _restaurant(db_path)
    calls = _script(monkeypatch, [
        _Msg("tool_use", [_Tool("read_alerts", {}, "tu_a")]),
        _Msg("tool_use", [_Tool("read_alerts", {"days": 3}, "tu_b")]),
        _Msg("tool_use", [_Tool("read_alerts", {"days": 2}, "tu_c")]),
        _Msg("end_turn", [_Text("Nothing urgent.")]),
    ])
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "earlier answer"}]
    ask_cavnar.ask_with_tools(r, "anything urgent?", history=history)
    assert len(calls) == 4
    # round 1: the question (index 2), never the history before it
    assert _marked(calls[0]["messages"]) == [(2, "user")]
    # round 2: the question and the first results
    assert _marked(calls[1]["messages"]) == [(2, "user"), (4, "user")]
    # round 3: the two newest results; the question's marker is gone
    assert _marked(calls[2]["messages"]) == [(4, "user"), (6, "user")]
    assert _marked(calls[3]["messages"]) == [(6, "user"), (8, "user")]
    for kw in calls:
        assert ask_cavnar.cache_breakpoints(kw.get("tools"), kw["system"], kw["messages"]) \
            <= ask_cavnar.MAX_CACHE_BREAKPOINTS


def test_the_turns_own_messages_never_carry_a_marker_and_the_forced_call_has_none(db_path, monkeypatch):
    r = _restaurant(db_path, module_inventory=1)
    calls = _script(monkeypatch, [
        _Msg("tool_use", [_Tool("send_supplier_order", {}, "tu_w")]),
        _Msg("end_turn", [_Text("That is queued for your OK.")]),
    ])
    monkeypatch.setattr(tools, "build_proposal", lambda *a, **k: {"action": "send_supplier_order", "summary": "x"})
    ask_cavnar.ask_with_tools(r, "send the order")
    assert len(calls) == 2 and calls[1].get("tool_choice") == {"type": "none"}
    assert _marked(calls[1]["messages"]) == [], "a tool_choice change misses the message cache: write nothing"
    # the question went out as a block with a marker, but the turn's own
    # list is untouched (a string), so no marker leaks into a later call
    assert calls[1]["messages"][0] == {"role": "user", "content": "send the order"}
    assert ask_cavnar.cache_breakpoints(calls[1]["tools"], calls[1]["system"], calls[1]["messages"]) == 2


def test_no_tool_definition_carries_its_own_breakpoint():
    assert not any("cache_control" in t for t in tools.tool_specs())


# ── #17: the schema budget ──────────────────────────────────────────────────

# 45,866 characters (~11,400 tokens) before the audit; 25,7xx after.
TOOL_SCHEMA_CEILING_CHARS = 26500


def test_the_tool_schemas_stay_under_their_ceiling_with_every_tool_offered():
    specs = tools.tool_specs()
    assert len(json.dumps(specs)) < TOOL_SCHEMA_CEILING_CHARS
    assert len(specs) == len(tools.TOOLS) >= 81, "trimmed, never removed"
    for s in specs:
        assert s["description"].strip()


def test_the_rules_the_descriptions_dropped_are_said_once_in_the_static_block():
    p = ask_cavnar._SYSTEM_STATIC
    assert "TOOL CONVENTIONS" in p and 'starts "Propose"' in p
    for name in ("remember", "set_goal", "track_outcome", "change_setting", "edit_review_reply", "skip_review"):
        assert name in p
    assert "never recompute" in p


# ── #18: the executive pre-read ─────────────────────────────────────────────

def test_an_executive_question_carries_the_business_snapshot_without_a_round(db_path, monkeypatch):
    r = _restaurant(db_path, module_reviews=1)
    monkeypatch.setattr(bi, "_executive_brief", lambda *a, **k: {
        "modules_consulted": ["reviews"], "reviews": {"fix_first": "Answer the 1-star reviews"},
        "links": [], "money": {"ranked": []}, "modules_off": [], "degraded": [], "unanswered": []})
    calls = _script(monkeypatch, [_Msg("end_turn", [_Text("Answer the 1-star reviews first.")])])
    answer, _t, _p, meta = ask_cavnar.ask_with_tools(r, "what should I focus on this week?")
    assert len(calls) == 1, "the model answered at once: no round was spent asking for it"
    msgs = calls[0]["messages"]
    assert msgs[0]["role"] == "user"
    use = msgs[1]["content"][0]
    assert msgs[1]["role"] == "assistant" and use["type"] == "tool_use" and use["name"] == "read_business_snapshot"
    res = msgs[2]["content"][0]
    assert res["type"] == "tool_result" and res["tool_use_id"] == use["id"]
    assert json.loads(res["content"])["has_data"] is True
    assert "read_business_snapshot" in meta["tools_used"]
    assert "reviews" in meta["modules_consulted"]
    assert {"name": "read_business_snapshot", "input": {}} in meta["tool_calls"]
    texts = [b["text"] for b in calls[0]["system"]]
    assert bi.SNAPSHOT_HEADER not in texts[1]
    assert ask_cavnar._PRERUN_NOTE in texts
    assert ask_cavnar._DEPTH_EXECUTIVE.strip() in texts
    # the newest user message — the result — and the question carry the breakpoints
    assert _marked(msgs) == [(0, "user"), (2, "user")]


def test_a_standard_question_gets_no_pre_read(db_path, monkeypatch):
    r = _restaurant(db_path, module_reviews=1)
    calls = _script(monkeypatch, [_Msg("end_turn", [_Text("4.5 stars.")])])
    _a, _t, _p, meta = ask_cavnar.ask_with_tools(r, "what's my rating")
    assert len(calls[0]["messages"]) == 1 and "read_business_snapshot" not in meta["tools_used"]
    assert ask_cavnar._PRERUN_NOTE not in [b["text"] for b in calls[0]["system"]]


def test_no_pre_read_for_a_model_that_thinks(db_path, monkeypatch):
    r = _restaurant(db_path, module_reviews=1)
    monkeypatch.setattr(ask_cavnar, "model_for", lambda purpose: "claude-opus-5-5")
    calls = _script(monkeypatch, [_Msg("end_turn", [_Text("ok")])])
    ask_cavnar.ask_with_tools(r, "what should I focus on?")
    assert len(calls[0]["messages"]) == 1


def test_the_across_section_is_cut_cleanly():
    ctx = ("TODAY\n- Today's date: x\n\nLABOR\n- a\n\n" + bi.SNAPSHOT_HEADER + "\n- Monthly dollars\n")
    out = ask_cavnar._without_across(ctx)
    assert bi.SNAPSHOT_HEADER not in out and "LABOR\n- a" in out
    mid = "A\n- 1\n\n" + bi.SNAPSHOT_HEADER + "\n- x\n\nB\n- 2\n"
    assert ask_cavnar._without_across(mid) == "A\n- 1\n\nB\n- 2\n"


# ── #33: once per question ──────────────────────────────────────────────────

def test_the_brief_and_the_labor_analysis_run_once_per_question(db_path, monkeypatch):
    r = _restaurant(db_path, module_labor=1)
    import labor
    shifts = []
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda rid, **k: shifts.append(rid) or {"is_live": False})
    briefs = []
    real = bi._executive_brief
    monkeypatch.setattr(bi, "_executive_brief", lambda *a, **k: briefs.append(1) or real(*a, **k))
    _script(monkeypatch, [_Msg("end_turn", [_Text("ok")])])
    ask_cavnar.ask_with_tools(r, "what should I focus on?")     # snapshot + pre-read
    assert len(briefs) == 1 and len(shifts) == 1
    # outside a question nothing is memoised
    bi.executive_brief(r.id, restaurant=r)
    bi.executive_brief(r.id, restaurant=r)
    assert len(briefs) == 3


def test_a_write_inside_the_question_drops_its_memo(db_path, monkeypatch):
    import labor
    n = []
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid, **k: n.append(rid) or {"is_live": True})
    with bi.question_memo():
        bi.shift_analysis(5)
        bi.shift_analysis(5)
        ask_cavnar.invalidate_context(5)
        bi.shift_analysis(5)
    assert len(n) == 2


# ── #32: a round's reads together ───────────────────────────────────────────

def test_a_rounds_reads_run_together_and_come_back_in_order(db_path, monkeypatch):
    r = _restaurant(db_path)
    seen = {}
    delays = {"read_alerts": 0.3, "read_email_history": 0.0, "read_data_health": 0.15}

    def fake_run(name, rid, tool_input, restaurant=None):
        time.sleep(delays[name])
        seen[name] = (threading.current_thread().name, ai_utils.current_ai_context().get("correlation_id"))
        return json.dumps({"tool": name})
    monkeypatch.setattr(tools, "run_read_tool", fake_run)
    turn = []
    calls = _script(monkeypatch, [
        lambda kw: turn.append(ai_utils.current_ai_context().get("correlation_id")) or _Msg(
            "tool_use", [_Tool("read_alerts", {}, "tu_1"), _Tool("read_email_history", {}, "tu_2"),
                         _Tool("read_data_health", {}, "tu_3")]),
        _Msg("end_turn", [_Text("done")]),
    ])
    _a, _t, _p, meta = ask_cavnar.ask_with_tools(r, "what's been happening")
    results = calls[1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in results] == ["tu_1", "tu_2", "tu_3"]
    assert [json.loads(b["content"])["tool"] for b in results] == ["read_alerts", "read_email_history",
                                                                     "read_data_health"]
    assert all(t.startswith("ask-read") for t, _c in seen.values())
    # each read ran under the question's correlation id
    assert turn[0].startswith("ask:") and {c for _t, c in seen.values()} == {turn[0]}
    assert [c["name"] for c in meta["tool_calls"]] == ["read_alerts", "read_email_history", "read_data_health"]


def test_reads_after_a_direct_action_run_in_place_after_it(db_path, monkeypatch):
    r = _restaurant(db_path)
    order = []

    def fake_run(name, rid, tool_input, restaurant=None):
        order.append((name, threading.current_thread().name))
        return json.dumps({"ok": True})
    monkeypatch.setattr(tools, "run_read_tool", fake_run)
    calls = _script(monkeypatch, [
        _Msg("tool_use", [_Tool("read_alerts", {}, "a"), _Tool("read_email_history", {}, "b"),
                          _Tool("forget", {"fact": "x"}, "c"), _Tool("read_data_health", {}, "d")]),
        _Msg("end_turn", [_Text("done")]),
    ])
    ask_cavnar.ask_with_tools(r, "forget that and check")
    names = [n for n, _t in order]
    assert names.index("forget") < names.index("read_data_health")
    main = threading.current_thread().name
    assert dict(order)["forget"] == main and dict(order)["read_data_health"] == main
    assert dict(order)["read_alerts"].startswith("ask-read")
    assert [b["tool_use_id"] for b in calls[1]["messages"][-1]["content"]] == ["a", "b", "c", "d"]


# ── #66: older answers cut, and still verified ──────────────────────────────

def test_older_answers_replay_cut_and_their_figure_check_still_finds_them(db_path):
    r = _restaurant(db_path)
    older = "Labor ran 31.4% last week. " + "x" * 3000
    newest = "Food cost is 29%. " + "y" * 5000
    ask_cavnar.record_answer_check(r.id, older, [], db_path=db_path)
    ask_cavnar.record_answer_check(r.id, newest, [], db_path=db_path)
    out = ask_cavnar._sanitize_history([
        {"role": "user", "content": "q" * 3000}, {"role": "assistant", "content": older},
        {"role": "user", "content": "q2"}, {"role": "assistant", "content": newest}])
    assert len(out[0]["content"]) == ask_cavnar._MAX_HISTORY_TURN_LENGTH
    assert len(out[1]["content"]) == 800 and out[3]["content"] == newest.strip()
    verified = ask_cavnar._verified_history(r.id, out, db_path=db_path)
    assert len(verified) == 2


# ── #97: one id per question ────────────────────────────────────────────────

def test_every_question_gets_its_own_correlation_id(db_path, monkeypatch):
    r = _restaurant(db_path)
    ids = []
    monkeypatch.setattr(ask_cavnar, "create_with_retry", lambda client, **kw: ids.append(
        ai_utils.current_ai_context().get("correlation_id")) or _Msg("end_turn", [_Text("ok")]))
    with ai_utils.ai_context(correlation_id="job:outer"):
        ask_cavnar.ask_with_tools(r, "one")
        ask_cavnar.ask_with_tools(r, "two")
    assert all(i and i.startswith("ask:") for i in ids) and len(set(ids)) == 2
    # the weekly plan keeps the run's id
    with ai_utils.ai_context(correlation_id="weekly_plan:1:2026-10-05"):
        ask_cavnar.ask_with_tools(r, "plan", action="weekly_plan")
    assert ids[-1] == "weekly_plan:1:2026-10-05"


def test_the_chat_summary_call_carries_an_id(monkeypatch):
    import ask_conversations as conv
    seen = []
    monkeypatch.setattr(conv, "_summarize", lambda *a, **k: seen.append(
        ai_utils.current_ai_context().get("correlation_id")))
    conv.maybe_summarize(1, 99)
    conv.maybe_summarize(1, 99, correlation_id="ask:abc")
    assert seen[0].startswith("ask_summary:") and seen[1] == "ask:abc"
