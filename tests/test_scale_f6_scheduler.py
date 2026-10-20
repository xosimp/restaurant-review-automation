"""Scale fix round 10/19/26, agent F6 — the schedule generator at fleet
scale (scale audit 10/19/26 A5: F2, F4, F5, F6, F7, F12, F13, F14, F15,
F16; A6 FE-11; A1 INF-15)."""
import builtins
import json
import os
import sys
import time

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import job_queue
    job_queue.reset_for_tests()
    monkeypatch.delenv("QUEUE_WORKER", raising=False)
    return db_path


@pytest.fixture
def quiet(monkeypatch):
    """No consumer threads: the test drives claim / run_one itself."""
    import job_queue
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    return job_queue


def _restaurant(db_path, name="Scale Grill", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                     (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _fake_cgroup(monkeypatch, tmp_path, files):
    """open() reads `files` {cgroup path: text} from tmp_path instead."""
    real = builtins.open

    def fake_open(path, *a, **k):
        if str(path) in files:
            p = tmp_path / os.path.basename(str(path))
            p.write_text(files[str(path)])
            return real(p, *a, **k)
        if str(path).startswith("/sys/fs/cgroup/"):
            raise FileNotFoundError(path)
        return real(path, *a, **k)
    monkeypatch.setattr(builtins, "open", fake_open)


# ── A5-F2 / F7: generation slots sized to the container ─────────────────────

def test_the_memory_limit_is_the_containers_cgroup(monkeypatch, tmp_path):
    import job_queue
    _fake_cgroup(monkeypatch, tmp_path, {"/sys/fs/cgroup/memory.max": str(8 * 1024 ** 3) + "\n"})
    assert job_queue.memory_limit_mb() == 8192
    _fake_cgroup(monkeypatch, tmp_path, {"/sys/fs/cgroup/memory.max": "max\n"})
    assert job_queue.memory_limit_mb() is None
    # cgroup v1's "no limit" is a page-rounded 2^63
    _fake_cgroup(monkeypatch, tmp_path, {"/sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712\n"})
    assert job_queue.memory_limit_mb() is None
    _fake_cgroup(monkeypatch, tmp_path, {})
    assert job_queue.memory_limit_mb() is None


def test_generation_slots_follow_the_cores_and_the_memory_not_a_fixed_two(monkeypatch):
    import job_queue
    import schedule_cpsat as cps
    import schedule_engine as se
    monkeypatch.delenv("QUEUE_GENERATION_CONCURRENCY", raising=False)
    monkeypatch.setattr(job_queue, "_gateway_streams", lambda: None)
    monkeypatch.setattr(cps, "WORKERS", 4)
    monkeypatch.setattr(se, "SCHEDULE_GEN_WORKERS", 0)
    monkeypatch.setattr(job_queue, "cpu_cores", lambda: 32)
    monkeypatch.setattr(job_queue, "memory_limit_mb", lambda: None)
    assert job_queue.group_concurrency("generation") == 8          # 32 cores / 4 threads: no fixed 2
    monkeypatch.setattr(job_queue, "memory_limit_mb", lambda: 8192.0)
    # (8192 - 1536) // 1400 = 4: the memory carries four generations
    assert job_queue.group_concurrency("generation") == 4
    monkeypatch.setattr(job_queue, "cpu_cores", lambda: 8)
    assert job_queue.group_concurrency("generation") == 2          # the cores bind first
    monkeypatch.setattr(job_queue, "memory_limit_mb", lambda: 1024.0)
    assert job_queue.group_concurrency("generation") == 1          # always at least one
    monkeypatch.setattr(job_queue, "memory_limit_mb", lambda: None)
    monkeypatch.setattr(job_queue, "cpu_cores", lambda: 32)
    monkeypatch.setattr(se, "SCHEDULE_GEN_WORKERS", 3)             # the operator's ceiling
    assert job_queue.group_concurrency("generation") == 3
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "5")        # the operator's own word stands
    assert job_queue.group_concurrency("generation") == 5


def test_the_capacity_says_when_the_solver_threads_outnumber_the_cores(monkeypatch):
    import job_queue
    import schedule_cpsat as cps
    monkeypatch.setattr(job_queue, "_gateway_streams", lambda: None)
    monkeypatch.setattr(job_queue, "memory_limit_mb", lambda: None)
    monkeypatch.setattr(cps, "WORKERS", 4)
    monkeypatch.setattr(job_queue, "cpu_cores", lambda: 8)
    monkeypatch.delenv("QUEUE_GENERATION_CONCURRENCY", raising=False)
    cap = job_queue.generation_capacity()
    assert cap["slots"] == 2 and not cap["oversubscribed"]
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "4")
    cap = job_queue.generation_capacity()
    assert cap["slots"] == 4 and cap["oversubscribed"]              # 16 solver threads on 8 cores


# ── A5-F4: the auto-draft's spread, deadline and the drafts' reserved slot ──

def test_the_draft_day_spread_covers_every_draft_day_and_keeps_the_days_in_force(db):
    # Restaurants that read the four-day spread before keep their day, once.
    conn = models.get_conn()
    try:
        conn.executemany("INSERT INTO restaurants (name, owner_email, week_start_day) VALUES (?,?,?)",
                         [(f"Old {i}", f"o{i}@x.test", 2 if i == 0 else 0) for i in range(8)])
        conn.execute("DELETE FROM data_migrations WHERE name=?", (models.AUTO_DRAFT_DAY_SPREAD6_MIGRATION,))
        conn.commit()
        old = [r[0] for r in conn.execute("SELECT id FROM restaurants ORDER BY id").fetchall()]
        wsd = {r[0]: r[1] for r in conn.execute("SELECT id, week_start_day FROM restaurants").fetchall()}
    finally:
        conn.close()
    before = {rid: models.effective_auto_draft_weekday(rid, None, 0, week_start_day=wsd[rid] or 0,
                                                       spread=models.AUTO_DRAFT_SPREAD_WEEKDAYS_V1) for rid in old}
    assert set(before.values()) <= {0, 1, 2, 3}
    assert models.keep_four_day_spread_days() == len(old)
    for rid in old:
        r = models.get_restaurant(rid)
        assert r.auto_draft_weekday_chosen == 1 and models.auto_draft_weekday(r) == before[rid]
    assert models.keep_four_day_spread_days() == 0                     # once
    # A restaurant made after reads the six-day spread.
    new = [_restaurant(db, name=f"New{i} Grill") for i in range(6)]
    assert {models.auto_draft_weekday(models.get_restaurant(rid)) for rid in new} == {0, 1, 2, 3, 4, 5}


def test_the_drafts_spread_and_deadline_grow_with_the_work_ahead(db, monkeypatch):
    import strategy_jobs as sj
    monkeypatch.setattr(sj, "generation_seconds_estimate", lambda: 60.0)
    monkeypatch.setattr(sj, "_generation_slots", lambda: 2)
    monkeypatch.setattr(sj, "AUTO_DRAFT_SPREAD_MINUTES", 120)
    assert sj.auto_draft_spread_seconds(10) == 7200                  # 10 drafts clear in minutes: the least
    assert sj.auto_draft_spread_seconds(400) == 12000                 # 400 x 60 s / 2 slots
    assert sj.auto_draft_spread_seconds(100000) == sj.AUTO_DRAFT_SPREAD_MAX_MINUTES * 60
    assert sj.auto_draft_deadline_seconds() == sj.AUTO_DRAFT_QUEUE_HOURS * 3600   # nothing queued
    import job_queue
    job_queue.enqueue_many("schedule_generate", [{"restaurant_id": None, "payload": {}, "idempotency_key": f"g{i}"}
                                                 for i in range(1000)], wake=False)
    # 1,000 queued x 60 s / 2 slots = 30,000 s of work; 1.5x of it
    assert sj.auto_draft_deadline_seconds() == 45000
    job_queue.enqueue_many("schedule_generate", [{"restaurant_id": None, "payload": {}, "idempotency_key": f"h{i}"}
                                                 for i in range(5000)], wake=False)
    assert sj.auto_draft_deadline_seconds() == sj.AUTO_DRAFT_QUEUE_MAX_HOURS * 3600
    # The offset stays the restaurant's own place in whatever span the day has.
    assert 0 <= sj.auto_draft_offset_seconds(7, span=12000) < 12000
    assert sj.auto_draft_offset_seconds(7, span=0) == 0


# ── A5-F12: an expired auto-draft is queued again, and the owner is told ────

def _set_job_state(qid, state):
    conn = models.get_conn()
    try:
        conn.execute("UPDATE jobs SET state=? WHERE id=?", (state, qid))
        conn.commit()
    finally:
        conn.close()


def test_an_expired_auto_draft_is_queued_again_on_the_next_pass(db, quiet):
    import strategy_jobs as sj
    rid = _restaurant(db, auto_draft_schedule=1, module_labor=1)
    r = models.get_restaurant(rid)
    first, created = sj._enqueue_auto_draft(r, period="2026-10-15", target="2026-10-19", attempt=0)
    assert created
    # The same try while it waits is the same row.
    assert sj._enqueue_auto_draft(r, period="2026-10-15", target="2026-10-19", attempt=0) == (first, False)
    # Expired unstarted (nothing ran, nothing was paid for): queued again.
    _set_job_state(first, "expired")
    nxt, created = sj._enqueue_auto_draft(r, period="2026-10-15", target="2026-10-19", attempt=0)
    assert created and nxt != first
    conn = models.get_conn()
    try:
        keys = {row[0]: row[1] for row in conn.execute("SELECT id, idempotency_key FROM jobs WHERE kind='auto_draft'")}
    finally:
        conn.close()
    assert keys[nxt] == f"auto_draft:{rid}:2026-10-19#0" and keys[first].startswith(keys[nxt] + "~expired:")
    # A row that ran (or runs) is still never queued twice.
    for state in ("done", "failed", "running"):
        _set_job_state(nxt, state)
        assert sj._enqueue_auto_draft(r, period="2026-10-15", target="2026-10-19", attempt=0) == (nxt, False)


def test_an_expiry_with_no_pass_left_tells_the_owner(db, monkeypatch):
    import strategy_jobs as sj
    from datetime import datetime
    rid = _restaurant(db, auto_draft_schedule=1, module_labor=1, auto_draft_weekday=3, auto_draft_weekday_chosen=1)
    told = []
    monkeypatch.setattr(sj, "_tell_auto_draft_gave_up", lambda r, db_path, target, why="tries": told.append(why))
    thu, fri, fri_late, sat = (datetime(2026, 10, 15, 13), datetime(2026, 10, 16, 10), datetime(2026, 10, 16, 23, 30),
                               datetime(2026, 10, 17, 2))
    assert not sj._tell_if_no_pass_left(rid, "2026-10-19", now=thu)        # the draft day: later passes
    assert not sj._tell_if_no_pass_left(rid, "2026-10-19", now=fri)        # the fallback day
    assert sj._tell_if_no_pass_left(rid, "2026-10-19", now=fri_late)       # its last hour
    assert sj._tell_if_no_pass_left(rid, "2026-10-19", now=sat)            # past both
    assert told == ["busy", "busy"]
    # The expiry hook hands the day and the try back, then asks.
    asked = []
    monkeypatch.setattr(sj, "_tell_if_no_pass_left", lambda rid_, target: asked.append((rid_, target)))
    job = type("J", (), {"payload": {"restaurant_id": rid, "period": "2026-10-15", "target": "2026-10-19",
                                     "attempt": 0}})()
    sj.on_auto_draft_expired(job)
    assert asked == [(rid, "2026-10-19")]


def test_the_owner_is_told_why_the_draft_never_ran(db, monkeypatch):
    import strategy_jobs as sj
    import notify
    import push
    import morning_brief
    import permissions
    rid = _restaurant(db, auto_draft_schedule=1, module_labor=1)
    r = models.get_restaurant(rid)
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    monkeypatch.setattr(notify, "record_notification", lambda *a, **k: None)
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 9, "role": "owner"}])
    monkeypatch.setattr(permissions, "has_permission", lambda u, p: True)
    sent = []
    monkeypatch.setattr(push, "fire_push", lambda rid_, kind, title, body, **k: sent.append(body))
    sj._tell_auto_draft_gave_up(r, None, "2026-10-19", why="busy")
    assert sent and "too busy" in sent[0] and "10/19/26" in sent[0] and "2026-10-19" not in sent[0]


# ── A5-F13: a code draft is built and solved once ───────────────────────────

def test_a_code_draft_is_never_planned_for_a_batch(db, monkeypatch):
    import ops
    import schedule_batch as sb
    import strategy_jobs as sj
    monkeypatch.setenv("SCHEDULE_DRAFT_SOURCE", "solver")
    rid = _restaurant(db, auto_draft_schedule=1, module_labor=1)
    monkeypatch.setattr(sb, "plan_requests", lambda *a, **k: pytest.fail("built and solved only to batch nothing"))
    monkeypatch.setattr(sb, "submit_auto_draft", lambda *a, **k: pytest.fail("a code draft has nothing to batch"))

    monkeypatch.setattr(ops, "claim_async_job", lambda *a, **k: ("owner-job", True))   # joined the owner's
    bumped = []
    sj._draft_one(models.get_restaurant(rid), db, None, bumped.append, period="2026-10-15", target="2026-10-19",
                  attempt=0)
    assert bumped == ["skipped"]                                      # straight to the run (here: joined)
    assert sj._drafted_in_code(rid, "2026-10-19")
    monkeypatch.setenv("SCHEDULE_DRAFT_SOURCE", "model")
    assert not sj._drafted_in_code(rid, "2026-10-19")
