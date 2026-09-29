"""Memory fix round M5 — memory reaching the nightly report, the weekly
digest and the morning brief (the lead's notes: wire memory_context into the
DSR narrative, the brief and the digest; record each through
ai_reads.record_read).

The DSR narrative and the digest are model calls: each asks memory_context
once for its surface ("dsr_narrative", "digest") and its block — fenced and
dated M/D/YY by memory_context — goes into the prompt as context. The
narrative's memory is what a MANAGER may read (one narrative renders into
both views). The brief asks no model: it shows memory dated today. Each
keeps what it said through ai_reads.record_read.
"""
import json
import types
from datetime import date, datetime

import pytest

import ai_utils
import dsr
import memory_context
import models
from dsr import narrative
from models import Restaurant, create_restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ai_utils.reset_breaker()
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    yield
    ai_utils.reset_breaker()


class _Client:
    def __init__(self, reply):
        self.reply, self.calls = reply, []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=json.dumps(self.reply))], stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0,
                                        cache_read_input_tokens=0))


def _facts(day="2026-09-22"):
    blocks = {"sales": dsr.block(dsr.READY, source="rpower", metrics={"net": 5210.6, "transactions": 171}),
              "labor": dsr.block(dsr.READY, source="rpower", metrics={"pct": 34.8, "target_pct": 26.0})}
    return {"schema": dsr.SCHEMA_VERSION, "restaurant_id": 1, "business_date": day, "blocks": blocks,
            "missing": dsr.missing_reasons(blocks)}


REPLY = {"executive_summary": {"text": "Net sales were $5,211. Labor ran 34.8% against a 26% target.",
                               "cites": ["sales.net", "labor.pct", "labor.target_pct"]},
         "went_well": [], "needs_attention": [{"text": "Labor ran 34.8%.", "cites": ["labor.pct"]}],
         "actions_tomorrow": []}

MEMORY = "EVENTS:\n- (9/22/26) <<fenced>> Rain nights here ran a median 9% below <<fenced>>"


def _stub_memory(monkeypatch, text=MEMORY):
    seen = []

    def fake(rid, surface, viewer=None, subjects=(), budget_chars=None, now=None, db_path=None):
        seen.append({"rid": rid, "surface": surface, "viewer": viewer, "subjects": tuple(subjects), "now": now})
        return memory_context.MemoryBlock(text=text, sections={"events": [{"text": "Rain nights ran 9% below",
                                                                          "date": date(2026, 9, 22)}]})
    monkeypatch.setattr(memory_context, "memory_context", fake)
    return seen


def _stub_reads(monkeypatch):
    import ai_reads
    kept = []

    def fake(restaurant_id, surface, text, subject=None, meta=None, call_id=None, db_path=None):
        kept.append({"rid": restaurant_id, "surface": surface, "text": text, "subject": subject, "meta": meta,
                     "call_id": call_id})
        return len(kept)
    monkeypatch.setattr(ai_reads, "record_read", fake)
    return kept


def test_the_nightly_report_reads_its_memory_as_a_manager_and_keeps_its_read(monkeypatch):
    rid = create_restaurant(Restaurant(name="Memory Tap", owner_email="m@x.test"))
    seen = _stub_memory(monkeypatch)
    kept = _stub_reads(monkeypatch)
    client = _Client(REPLY)
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: client)
    out = narrative.write(dsr.Context(get_restaurant(rid), "2026-09-22"), _facts())
    assert out["ok"], out
    (call,) = seen
    assert call["surface"] == "dsr_narrative" and call["viewer"] == narrative.NARRATIVE_MEMORY_VIEWER
    assert "date:2026-09-22" in call["subjects"] and "date:2026-09-23" in call["subjects"]
    user = client.calls[0]["messages"][0]["content"]
    assert "WHAT CAVNAR AI REMEMBERS ABOUT THIS RESTAURANT" in user and MEMORY in user
    assert "cite no figure from it" in user
    (read,) = kept
    assert read["surface"] == "dsr_narrative" and read["subject"] == "dsr:2026-09-22"
    assert read["text"].startswith("Net sales were $5,211.") and read["meta"]["business_date"] == "2026-09-22"


def test_a_night_with_nothing_remembered_has_no_memory_section(monkeypatch):
    rid = create_restaurant(Restaurant(name="Blank Tap", owner_email="b@x.test"))
    _stub_memory(monkeypatch, text="")
    _stub_reads(monkeypatch)
    client = _Client(REPLY)
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: client)
    assert narrative.write(dsr.Context(get_restaurant(rid), "2026-09-22"), _facts())["ok"]
    assert "REMEMBERS" not in client.calls[0]["messages"][0]["content"]


def test_a_failing_memory_read_never_stops_the_report(monkeypatch):
    rid = create_restaurant(Restaurant(name="Broken Tap", owner_email="br@x.test"))

    def boom(*a, **k):
        raise RuntimeError("memory down")
    monkeypatch.setattr(memory_context, "memory_context", boom)
    _stub_reads(monkeypatch)
    client = _Client(REPLY)
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: client)
    assert narrative.write(dsr.Context(get_restaurant(rid), "2026-09-22"), _facts())["ok"]


def test_the_digest_reads_memory_and_keeps_its_read():
    import inspect
    import reporter
    src = inspect.getsource(reporter.generate_ai_digest_summary)
    assert '_mc_dig.memory_context(_rid_dg, "digest")' in src
    assert "{_mem_section}" in src and "_untrusted_texts.append" in src
    assert '_air_dig.record_read(\n                    _rid_dg, "digest"' in src


def test_the_brief_shows_memory_dated_today_and_keeps_what_was_sent(monkeypatch):
    import morning_brief
    rid = create_restaurant(Restaurant(name="Brief Tap", owner_email="bt@x.test"))
    today = date(2026, 9, 22)

    def fake(r, surface, viewer=None, subjects=(), budget_chars=None, now=None, db_path=None):
        assert surface == "brief"
        return memory_context.MemoryBlock(text="x", sections={
            "constraints": [{"text": "Closed from 3pm for the private party", "date": today, "source": "owner"},
                            {"text": "Never schedule Ana on Sundays", "date": date(2026, 9, 1), "source": "owner"}],
            "events": [{"text": "Tue 9/22/26: Cubs game — nights like it ran a median 25% above", "date": today}]})
    monkeypatch.setattr(memory_context, "memory_context", fake)
    got = morning_brief._memory_lines(rid, today, None, [], None)
    assert [l["key"] for l in got] == ["memory:constraint", "memory:event"]
    assert got[0]["text"] == "Your note for today: Closed from 3pm for the private party"
    assert got[1]["text"].startswith("Remembered: Tue 9/22/26: Cubs game")
    # The today line already carrying measured effects: no second event line.
    carried = [{"key": "today", "text": "Today, from last night's report: about $9,800, with Cubs game +25% "
                                        "(measured 4 times here)."}]
    assert [l["key"] for l in morning_brief._memory_lines(rid, today, None, carried, None)] == ["memory:constraint"]
    kept = _stub_reads(monkeypatch)
    morning_brief._record_read(rid, {"date": "2026-09-22", "lines": [
        {"key": "yesterday", "text": "Last night: $5,000 net sales."},
        {"key": "dsr_action", "text": "From last night's report: Call a second cook.", "rec": "dsr_action:x"}]},
        view="owner")
    (read,) = kept
    assert read["surface"] == "brief" and read["subject"] == "brief:2026-09-22"
    assert read["meta"]["recs"] == ["dsr_action:x"] and "Call a second cook" in read["text"]
