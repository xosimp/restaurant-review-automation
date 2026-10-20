"""The "dsr" widget wake is spread over the fleet (scale audit 10/19/26
FE-12): held reports are released together when quiet hours end, and every
owner phone woke at once to make its ~5 reads. Each restaurant's wake now
waits its own fixed offset in the job queue; the "waiting" echo of an
owner's own action still goes at once."""
import time

import pytest

import job_queue
import push
from tests.test_live_activities import _device, _login, _restaurant, apns, db  # noqa: F401


def test_offsets_are_fixed_per_restaurant_and_spread_over_the_window(monkeypatch):
    monkeypatch.setenv("SILENT_PUSH_JITTER_SECONDS", "600")
    offs = [push.silent_offset(rid, "dsr") for rid in range(1, 401)]
    assert all(0 <= o < 600 for o in offs)
    assert push.silent_offset(7, "dsr") == push.silent_offset(7, "dsr")
    # Spread, not clumped: every tenth of the window holds some restaurants.
    assert len({o // 60 for o in offs}) == 10
    assert push.silent_offset(7, "waiting") == 0
    monkeypatch.setenv("SILENT_PUSH_JITTER_SECONDS", "0")
    assert push.silent_offset(7, "dsr") == 0


def test_a_dsr_wake_is_queued_at_its_offset_and_sent_by_the_queue(db, apns, monkeypatch):
    monkeypatch.setenv("SILENT_PUSH_JITTER_SECONDS", "600")
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid)
    monkeypatch.setattr(push, "silent_offset", lambda r, reason: 300 if reason == "dsr" else 0)
    queued = []
    monkeypatch.setattr(job_queue, "enqueue", lambda kind, payload, **k: queued.append((kind, payload, k)) or (1, True))
    t0 = time.time()
    assert push.fire_silent(rid, "dsr", db_path=db) == 1
    assert apns == []                                   # nothing at once
    kind, payload, kw = queued[0]
    assert kind == "silent_push" and payload == {"restaurant_id": rid, "reason": "dsr", "user_ids": None}
    assert t0 + 299 <= kw["not_before"] <= time.time() + 301
    # The queue's handler sends it to the devices on file then.
    assert push.run_queued_silent({"payload": payload}) == {"sent": 1}
    assert [p["payload"]["cavnar"]["silent"] for p in apns] == ["dsr"]


def test_the_waiting_echo_still_goes_at_once(db, apns, monkeypatch):
    monkeypatch.setenv("SILENT_PUSH_JITTER_SECONDS", "600")
    rid = _restaurant(db)
    uid, _t, _h = _login(db, rid)
    _device(db, uid, rid)
    assert push.fire_silent(rid, "waiting", user_ids=[uid], db_path=db) == 1
    assert [p["payload"]["cavnar"]["silent"] for p in apns] == ["waiting"]


def test_the_queue_knows_the_kind():
    assert job_queue.KINDS["silent_push"][0] == "bulk"
    assert job_queue.HANDLERS["silent_push"] == "push:run_queued_silent"
    assert job_queue._resolve(job_queue.HANDLERS["silent_push"]) is push.run_queued_silent
