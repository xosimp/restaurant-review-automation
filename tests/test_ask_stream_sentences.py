"""Ask Cavnar AI streams validated sentences (AI cost audit 10/7/26 #68).

What these hold:

- a sentence reaches the client only once the answer so far, through it,
  passes the same first-pass validation the whole answer gets; a sentence
  the engine would rewrite, drop or caveat is never streamed, and nothing
  after it is either (the preview is always the answer's opening);
- a tool round never shows: its text is withdrawn when the tool_use block
  starts, and a round that offers tools previews nothing until it has two
  sentences;
- only text deltas are read (never thinking), only the first text block;
- the final answer is authoritative and unchanged by the preview;
- ASK_STREAM_SENTENCES=0 is the old turn exactly: no stream, no events;
- ai_utils hands every stream event to on_stream, after a None per attempt;
- the stream route sends sentence events before the answer, and both
  clients render them (web ES5, iOS ignores nothing it cannot decode).
"""
import json
import os
import re

import pytest

import ai_utils
import ask_cavnar
import ask_cavnar_tools as tools
import models
import response_validation as rv
from models import Restaurant, create_restaurant, get_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    monkeypatch.delenv("ASK_STREAM_SENTENCES", raising=False)
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    ask_cavnar.invalidate_context()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Stream Co")
    kw.setdefault("owner_email", "o@x.test")
    for flag in ("module_reviews", "module_labor", "module_inventory", "module_marketing"):
        kw.setdefault(flag, 0)
    rid = create_restaurant(Restaurant(**kw), db_path=db_path)
    return get_restaurant(rid, db_path=db_path)


class _O:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Text:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Tool:
    def __init__(self, name, tool_input=None, block_id="tu_1"):
        self.type, self.name, self.input, self.id = "tool_use", name, tool_input or {}, block_id


class _Msg:
    def __init__(self, stop_reason, content):
        self.stop_reason, self.content = stop_reason, content


def _events(msg, chunk=7, thinking=None):
    """The SDK events a message streams as: each block's start, then its
    text in small deltas (a sentence split across many of them)."""
    out = []
    i = 0
    if thinking:
        out.append(_O(type="content_block_start", index=i, content_block=_O(type="thinking")))
        out.append(_O(type="content_block_delta", index=i, delta=_O(type="thinking_delta", thinking=thinking)))
        i += 1
    for b in msg.content:
        out.append(_O(type="content_block_start", index=i, content_block=_O(type=b.type)))
        if b.type == "text":
            for k in range(0, len(b.text), chunk):
                out.append(_O(type="content_block_delta", index=i,
                              delta=_O(type="text_delta", text=b.text[k:k + chunk])))
        i += 1
    return out


def _script(monkeypatch, replies, thinking=None):
    """Stub the model the way create_with_retry streams: with on_stream, a
    None for the attempt, then every event; returns each call's kwargs."""
    calls = []

    def fake(client, **kw):
        calls.append(dict(kw))
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        on = kw.get("on_stream")
        if kw.get("stream") and on:
            on(None)
            for e in _events(r, thinking=thinking):
                on(e)
        return r
    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake)
    return calls


def _collect():
    got = []
    return got, ask_cavnar.sentence_events(got.append)


PLAIN = ("Guests keep coming back to the patio. The kitchen should prep the brunch menu earlier. "
         "Keep the host stand staffed at open.")


# ── sentence_ends: the engine's own split, as text arrives ──────────────────

def test_sentence_ends_split_as_the_engine_does_and_wait_for_the_next_sentence():
    t = "Sales held steady. Labor ran high.\n\n- Cut one server Tuesday. Then check.\n1. Next"
    ends = rv.sentence_ends(t)
    assert [t[:e].split("\n")[-1].strip() for e in ends] == [
        "Sales held steady.", "Sales held steady. Labor ran high.", "- Cut one server Tuesday.",
        "- Cut one server Tuesday. Then check."]
    # The tail line's last sentence is complete only once the text is whole.
    assert rv.sentence_ends(t, final=True)[-1] == len(t)
    # A decimal or a half-written sentence is never a boundary.
    assert rv.sentence_ends("It was 3.5 points") == []
    assert rv.sentence_ends("One. Tw") == [4]


# ── the preview ─────────────────────────────────────────────────────────────

def test_validated_sentences_stream_and_the_final_answer_is_authoritative(db_path, monkeypatch):
    r = _restaurant(db_path)
    _script(monkeypatch, [_Msg("end_turn", [_Text(PLAIN)])])
    got, on = _collect()
    answer, truncated, proposals, meta = ask_cavnar.ask_with_tools(r, "what should we do", on_sentence=on)
    sent = [e["text"] for e in got if e["type"] == "sentence"]
    assert sent, "a clean answer streams its sentences"
    preview = "".join(sent)
    # The preview is the opening of the answer, sentence by sentence, and the
    # answer the turn returns is the one it always returned.
    assert preview == ("Guests keep coming back to the patio. The kitchen should prep the brunch "
                       "menu earlier.")
    assert PLAIN.startswith(preview)
    assert answer.startswith(preview.strip())
    calls_answer, *_ = ask_cavnar.ask_with_tools(r, "what should we do")
    assert calls_answer == answer
    # The last sentence is never complete until the text is: it arrives with
    # the answer event.
    assert not preview.rstrip().endswith("at open.")


def test_a_sentence_the_engine_would_flag_is_never_streamed_nor_anything_after(db_path, monkeypatch):
    r = _restaurant(db_path)
    text = ("Guests keep coming back to the patio. Sales were $48,312 last Friday. "
            "Keep the host stand staffed at open. Prep the brunch menu earlier.")
    _script(monkeypatch, [_Msg("end_turn", [_Text(text)])])
    got, on = _collect()
    answer, *_ = ask_cavnar.ask_with_tools(r, "how was friday", on_sentence=on)
    preview = "".join(e["text"] for e in got if e["type"] == "sentence")
    assert preview == "Guests keep coming back to the patio.", "the clean opening still streams"
    assert "48,312" not in preview
    assert "host stand" not in preview, "nothing after a held sentence streams"
    # The engine does flag it in the final answer's verdict — the same check.
    ctx = ask_cavnar._first_pass_context([ask_cavnar.build_context(r)], [], [], r.id)[0]
    assert not ask_cavnar.preview_passes("Guests keep coming back to the patio. Sales were $48,312 last Friday.", ctx)


def test_a_dropped_sentence_is_never_streamed(db_path, monkeypatch):
    r = _restaurant(db_path)
    # The verification marker is dropped by the engine (I1).
    text = ("Guests keep coming back to the patio.\nUNVERIFIED: the patio is great.\n"
            "Keep the host stand staffed at open. Prep the brunch menu earlier.")
    _script(monkeypatch, [_Msg("end_turn", [_Text(text)])])
    got, on = _collect()
    answer, *_ = ask_cavnar.ask_with_tools(r, "what about the patio", on_sentence=on)
    preview = "".join(e["text"] for e in got if e["type"] == "sentence")
    assert preview == "Guests keep coming back to the patio."
    assert "UNVERIFIED" not in preview and "UNVERIFIED" not in answer
    assert "host stand" not in preview


def test_every_streamed_prefix_passes_the_first_pass(db_path, monkeypatch):
    r = _restaurant(db_path)
    _script(monkeypatch, [_Msg("end_turn", [_Text(PLAIN)])])
    got, on = _collect()
    ask_cavnar.ask_with_tools(r, "what should we do", on_sentence=on)
    ctx = ask_cavnar._first_pass_context([ask_cavnar.build_context(r)], [], [], r.id)[0]
    so_far = ""
    for e in got:
        if e["type"] == "sentence":
            so_far += e["text"]
            assert ask_cavnar.preview_passes(so_far, ctx)


def test_a_tool_round_is_withdrawn_and_never_shown(db_path, monkeypatch):
    r = _restaurant(db_path)
    _script(monkeypatch, [
        _Msg("tool_use", [_Text("Let me look at your reviews first. I will check the patio ones. Then the rest."),
                          _Tool("read_reviews", {"search": "patio"})]),
        _Msg("end_turn", [_Text(PLAIN)]),
    ])
    got, on = _collect()
    answer, *_ = ask_cavnar.ask_with_tools(r, "what about the patio", on_sentence=on)
    kinds = [e["type"] for e in got]
    assert "sentence_reset" in kinds
    after = kinds[kinds.index("sentence_reset") + 1:]
    preview = "".join(e["text"] for e in got[kinds.index("sentence_reset") + 1:] if e["type"] == "sentence")
    assert "sentence" in after and "Let me look" not in preview and PLAIN.startswith(preview)


def test_a_one_line_preamble_before_a_tool_never_flashes(db_path, monkeypatch):
    r = _restaurant(db_path)
    _script(monkeypatch, [
        _Msg("tool_use", [_Text("Let me check your reviews."), _Tool("read_reviews", {})]),
        _Msg("end_turn", [_Text(PLAIN)]),
    ])
    got, on = _collect()
    ask_cavnar.ask_with_tools(r, "what about reviews", on_sentence=on)
    assert "sentence_reset" not in [e["type"] for e in got]
    assert all("Let me check" not in e.get("text", "") for e in got)


def test_only_text_deltas_of_the_first_text_block_are_read(db_path, monkeypatch):
    r = _restaurant(db_path)
    _script(monkeypatch, [_Msg("end_turn", [_Text(PLAIN), _Text("Second block. Never the answer. Ever.")])],
            thinking="Private reasoning. Sales were $99,999. Not for the owner.")
    got, on = _collect()
    ask_cavnar.ask_with_tools(r, "what should we do", on_sentence=on)
    preview = "".join(e["text"] for e in got if e["type"] == "sentence")
    assert preview and "Private reasoning" not in preview and "Second block" not in preview


def test_a_retried_attempt_withdraws_what_the_failed_one_streamed():
    got, on = _collect()
    p = ask_cavnar._SentencePreview(on, lambda: rv.ValidationContext(surface="ask"))
    p.begin(False)
    p.on_event(None)
    for e in _events(_Msg("end_turn", [_Text("One thing. Two things. Three")])):
        p.on_event(e)
    assert [e["type"] for e in got] == ["sentence", "sentence"]
    p.on_event(None)                      # the call is retried from the start
    assert got[-1] == {"type": "sentence_reset"}


def test_flag_off_is_the_old_turn_exactly(db_path, monkeypatch):
    r = _restaurant(db_path)
    monkeypatch.setenv("ASK_STREAM_SENTENCES", "0")
    calls = _script(monkeypatch, [_Msg("end_turn", [_Text(PLAIN)])])
    got, on = _collect()
    answer, *_ = ask_cavnar.ask_with_tools(r, "what should we do", on_sentence=on)
    assert got == []
    assert all("stream" not in c and "on_stream" not in c for c in calls)
    assert answer.startswith("Guests keep coming back")


def test_no_on_sentence_means_no_stream(db_path, monkeypatch):
    r = _restaurant(db_path)
    calls = _script(monkeypatch, [_Msg("end_turn", [_Text(PLAIN)])])
    ask_cavnar.ask_with_tools(r, "what should we do")
    assert all("stream" not in c for c in calls)


def test_the_flag_is_documented():
    doc = open(os.path.join(ROOT, "docs", "ops", "ENVIRONMENT.md"), encoding="utf-8").read()
    assert "`ASK_STREAM_SENTENCES`" in doc


# ── ai_utils: the stream is handed to on_stream ─────────────────────────────

class _Stream:
    def __init__(self, events, final):
        self._events, self._final = events, final
        self.current_message_snapshot = final

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


def test_send_hands_every_event_to_on_stream_after_a_none():
    final = _Msg("end_turn", [_Text("Hi there. Bye.")])
    evs = _events(final)
    client = _O(messages=_O(stream=lambda **kw: _Stream(evs, final)))
    seen = []
    out = ai_utils._send(client, {"model": "m"}, stream=True, on_stream=seen.append)
    assert out is final and seen[0] is None and seen[1:] == evs
    # A broken watcher never fails the call.
    out = ai_utils._send(client, {"model": "m"}, stream=True, on_stream=lambda e: 1 / 0)
    assert out is final


def test_create_with_retry_never_sends_on_stream_to_the_api(monkeypatch):
    sent = {}

    def stream(**kw):
        sent.update(kw)
        return _Stream([], _Msg("end_turn", [_Text("ok")]))
    client = _O(messages=_O(stream=stream, create=lambda **kw: _Msg("end_turn", [_Text("ok")])))
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10, messages=[],
                               readiness=None, stream=True, on_stream=lambda e: None)
    assert "on_stream" not in sent and "stream" not in sent


# ── the route and the clients ───────────────────────────────────────────────

def test_the_stream_route_passes_on_sentence():
    src = open(os.path.join(ROOT, "client_api.py"), encoding="utf-8").read()
    body = src[src.index("def _ask_cavnar_stream_response"):src.index("def ask_cavnar_stream(")]
    assert "on_sentence=_ac_stream.sentence_events(events.put)" in body
    mob = open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8").read()
    assert "_capi._ask_cavnar_stream_response(" in mob, "the mobile twin shares the one body"


def test_sentence_events_never_reset_with_nothing_shown():
    got, on = _collect()
    on(None)
    assert got == []
    on("One.")
    on(None)
    on(None)
    assert got == [{"type": "sentence", "text": "One."}, {"type": "sentence_reset"}]


def test_web_renders_sentence_events_and_the_answer_replaces_the_preview():
    html = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    i = html.index("window.sendAskCavnar = function()")
    js = html[i:html.index("</script>", i)]
    assert "evt.type === 'sentence'" in js and "evt.type === 'sentence_reset'" in js
    assert "_askPreviewShow" in js and "_askPreviewClear" in js
    # The answer is drawn into the preview's bubble, never beside it.
    assert re.search(r"_finish[\s\S]*_askPreviewBubble", js)


def test_ios_reads_sentence_events_and_ignores_unknown_ones():
    swift = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "AskCavnar",
                              "AskCavnarViewModel.swift"), encoding="utf-8").read()
    assert 'case "sentence":' in swift and 'case "sentence_reset":' in swift
    assert "default:\n                break" in swift, "an older event type is ignored, never a failure"
    api = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Core", "APIClient.swift"), encoding="utf-8").read()
    sse = api[api.index("struct SSEEvent"):api.index("var evidence: AskEvidence")]
    assert "case type, label, state, answer, text," in sse
