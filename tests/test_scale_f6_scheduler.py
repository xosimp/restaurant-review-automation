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
    # 32 cores / 2 a generation (measured, blind re-audit 10/20/26 R5-08 — it
    # was the solve's 4 threads): no fixed 2
    assert job_queue.group_concurrency("generation") == 16
    monkeypatch.setattr(job_queue, "memory_limit_mb", lambda: 8192.0)
    # (8192 - 1536) // 1400 = 4: the memory carries four generations
    assert job_queue.group_concurrency("generation") == 4
    monkeypatch.setattr(job_queue, "cpu_cores", lambda: 4)
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
    assert cap["slots"] == 4 and not cap["oversubscribed"]          # 8 cores / 2 a generation (R5-08)
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "5")
    cap = job_queue.generation_capacity()
    assert cap["slots"] == 5 and cap["oversubscribed"]              # 10 cores' worth on 8 cores


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


# ── A5-F5 / A6 FE-11: the Studio's reads off the request threads' hot path ──

def test_background_writes_no_longer_move_the_schedule_inputs_version(db):
    rid = _restaurant(db, module_labor=1)
    before = models.schedule_inputs_version(rid)
    conn = models.get_conn()
    try:
        conn.execute("UPDATE restaurants SET last_fetched_at='2026-10-19 10:00:00', "
                     "weather_cached_at='2026-10-19T10:00:00+00:00', rpower_last_synced='2026-10-19 10:05:00', "
                     "gbp_review_count=412, last_activity='2026-10-19 10:06:00' WHERE id=?", (rid,))
        conn.commit()
    finally:
        conn.close()
    assert models.schedule_inputs_version(rid) == before                # a review fetch, a sync, a visit
    models.update_restaurant(rid, {"role_minimums_json": json.dumps({"Server": 2})})
    assert models.schedule_inputs_version(rid) != before                # a rule the week is built to


def test_the_studio_builds_a_weeks_inputs_once_however_many_ask_at_once(db, monkeypatch):
    import threading
    import schedule_engine as se
    rid = _restaurant(db, module_labor=1)
    rows = [{"date": WEEK[0], "day": "Monday", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    built = []
    gate = threading.Event()

    def slow_inputs(restaurant_id, daily_target_hours=None, week_rows=None):
        built.append(restaurant_id)
        gate.wait(5)
        return {"roster": []}
    monkeypatch.setattr(se, "quality_inputs_from_db", slow_inputs)
    monkeypatch.setattr(se, "_quality_signals", lambda rid_, inputs: ({}, {}))
    got = []
    threads = [threading.Thread(target=lambda: got.append(se.studio_prepared(rid, rows, history_id=4)))
               for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(built) == 1 and len(got) == 3 and all(g is got[0] for g in got)


def test_the_violations_check_reads_the_studios_kept_inputs(db, monkeypatch):
    from flask import Flask
    import schedule_engine as se
    import strategy_routes as sr_
    rid = _restaurant(db, module_labor=1)
    calls = []
    real = se.quality_inputs_from_db
    monkeypatch.setattr(se, "quality_inputs_from_db", lambda *a, **k: calls.append(1) or real(*a, **k))
    rows = [{"date": WEEK[0], "day": "Monday", "employee": "Ana", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    app = Flask(__name__)
    u = {"restaurant_id": rid, "is_admin": True, "id": 1}
    for _ in range(2):
        with app.test_request_context(json={"rows": rows, "history_id": 9}):
            out, status = sr_._do_schedule_violations(u)
        assert status == 200 and out["ok"]
    assert calls == [1]                                               # built once, read twice


def test_requests_share_the_parsed_history_until_it_changes(db, monkeypatch):
    from flask import Flask
    import labor
    rid = _restaurant(db, module_labor=1)
    models.save_client_data(rid, "shifts", "date,employee,role\n2026-10-01,Ana,Server\n")
    parses = []
    real = labor._own_shifts
    monkeypatch.setattr(labor, "_own_shifts", lambda r, cd: parses.append(r) or real(r, cd))
    app = Flask(__name__)
    for _ in range(2):
        with app.test_request_context():
            labor.load_shifts_for_restaurant(rid)
    assert parses == [rid]                                            # two requests, one parse
    labor.load_shifts_for_restaurant(rid)                             # a job: read fresh
    assert parses == [rid, rid]
    conn = models.get_conn()
    try:
        conn.execute("UPDATE client_data SET updated_at='2099-01-01 00:00:00' WHERE restaurant_id=?", (rid,))
        conn.commit()
    finally:
        conn.close()
    with app.test_request_context():
        labor.load_shifts_for_restaurant(rid)
    assert parses == [rid, rid, rid]                                  # an upload or a sync is seen at once
    labor.shared_history_clear()


def test_the_requirement_model_cache_keeps_one_entry_a_restaurant(monkeypatch):
    import schedule_engine as se
    se._REQUIREMENT_MODEL_CACHE.clear()
    stamps = iter([("a",), ("b",), ("c",)])
    monkeypatch.setattr(se, "shifts_stamp", lambda rid: next(stamps))
    import learning_stats
    monkeypatch.setattr(learning_stats, "learning_off", lambda: False)
    import staffing_model, schedule_economics, staffing_elasticity
    monkeypatch.setattr(staffing_model, "measure", lambda rid, shifts=None: None)
    monkeypatch.setattr(schedule_economics, "measured_half_hours", lambda rid: None)
    monkeypatch.setattr(staffing_elasticity, "moves", lambda rid: None)
    for _ in range(3):
        se._requirement_model(5)
    assert [k[1:] for k in se._REQUIREMENT_MODEL_CACHE] == [(5, ("c",))]
    monkeypatch.setattr(se, "REQUIREMENT_MODEL_ENTRIES", 2)
    stamps2 = iter([("x",), ("y",), ("z",)])
    monkeypatch.setattr(se, "shifts_stamp", lambda rid: next(stamps2))
    for rid in (6, 7, 8):
        se._requirement_model(rid)
    assert [k[1] for k in se._REQUIREMENT_MODEL_CACHE] == [7, 8]       # the least recent out, never all
    se._REQUIREMENT_MODEL_CACHE.clear()


def test_a_review_of_the_same_rows_on_screen_is_served_from_the_cache(db, monkeypatch):
    import prepublish
    rid = _restaurant(db, module_labor=1)
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Ana")]), [])
    built = []
    monkeypatch.setattr(prepublish, "_review", lambda *a, **k: built.append(k.get("rows")) or {"ok": True, "n": 1})
    prepublish.invalidate()
    rows = [{"date": WEEK[0], "employee": "Ana"}]
    assert prepublish.review(rid, hid, rows=rows)["n"] == 1
    assert prepublish.review(rid, hid, rows=rows)["n"] == 1
    assert len(built) == 1                                            # the Summary and Publish, one build
    prepublish.review(rid, hid, rows=[{"date": WEEK[0], "employee": "Bo"}])
    assert len(built) == 2                                            # an edit is new rows
    assert prepublish.CACHE_SECONDS >= 300
    prepublish.invalidate()


# ── A5-F14: publish claims before its gate; the outbox in fewer writes ──────

def _publish_week(db_path, rid):
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"]
    lines += [f"2026-10-19,Monday,{n},Server,5:00pm,10:00pm,5," for n in ("Ana", "Bo", "Cy")]
    return models.save_schedule_history(rid, "2026-10-19", "2026-10-25", 15, 0, 30, "\n".join(lines), [])


def _claim_state(hid):
    conn = models.get_conn()
    try:
        return dict(conn.execute("SELECT published_at, publishing_at FROM schedule_history WHERE id=?",
                                 (hid,)).fetchone())
    finally:
        conn.close()


def test_a_second_send_never_pays_for_the_gate_and_a_refusal_lets_the_claim_go(db, monkeypatch):
    import schedule_publish as sp
    rid = _restaurant(db, module_labor=1)
    hid = _publish_week(db, rid)
    gates = []
    review = {"blockers": [{"key": "k1", "text": "Ana has no break", "tier": sp.MUST_FIX}], "soft": [], "notes": [],
              "hours": None, "held_unattended": []}
    monkeypatch.setattr(sp, "publish_review", lambda r, s, **k: gates.append(s) or review)
    # Another manager's Send holds the week: this one is refused before the gate.
    conn = models.get_conn()
    try:
        conn.execute("UPDATE schedule_history SET publishing_at=datetime('now') WHERE id=?", (hid,))
        conn.commit()
    finally:
        conn.close()
    out, status = sp.publish(rid, hid, actor={"username": "mgr2"}, acknowledge=[])
    assert status == 409 and out["in_progress"] and gates == []
    # Free: the gate runs under the claim, and its refusal lets the claim go.
    conn = models.get_conn()
    try:
        conn.execute("UPDATE schedule_history SET publishing_at=NULL WHERE id=?", (hid,))
        conn.commit()
    finally:
        conn.close()
    out, status = sp.publish(rid, hid, actor={"username": "mgr1"}, acknowledge=[])
    assert out.get("needs_ack") and gates == [hid]
    assert _claim_state(hid) == {"published_at": None, "publishing_at": None}


def test_the_outbox_queues_a_staff_in_one_statement_each_and_claims_in_chunks(db, monkeypatch):
    import schedule_publish as sp
    rid = _restaurant(db, module_labor=1)
    hid = _publish_week(db, rid)
    names = [f"Person {i}" for i in range(23)]
    conn = models.get_conn()
    try:
        sp.enqueue(conn, rid, hid, "2026-10-19", "b-old", [(n, "posted") for n in names[:5]])
        sp.enqueue(conn, rid, hid, "2026-10-19", "b-new", [(n, "posted") for n in names])
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        conn.commit()
        rows = conn.execute("SELECT batch, status FROM schedule_notices ORDER BY id").fetchall()
    finally:
        conn.close()
    # A person's earlier notice about the week is replaced by the new one.
    assert [r["status"] for r in rows if r["batch"] == "b-old"] == ["cancelled"] * 5
    assert len([r for r in rows if r["batch"] == "b-new" and r["status"] == "pending"]) == 23
    claims = []
    real_claim = sp._claim_notices
    monkeypatch.setattr(sp, "_claim_notices", lambda chunk, db_path=None: claims.append(len(chunk))
                        or real_claim(chunk, db_path))
    monkeypatch.setattr(sp, "_deliver_one", lambda rid_, r, sid, rows_, name, ch, label, **k: (
        "sent", {"employee_name": name, "channel": "push"}))
    got = sp.deliver_notices(restaurant_id=rid, batch="b-new", budget_seconds=60)
    assert len(got["sent"]) == 23 and not got["pending"]
    assert claims == [10, 10, 3]                                       # three claim writes, not 23


def test_a_chunk_the_bound_cuts_short_hands_its_notices_back(db, monkeypatch):
    import schedule_publish as sp
    rid = _restaurant(db, module_labor=1)
    hid = _publish_week(db, rid)
    conn = models.get_conn()
    try:
        sp.enqueue(conn, rid, hid, "2026-10-19", "b1", [(f"P{i}", "posted") for i in range(6)])
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        conn.commit()
    finally:
        conn.close()
    clock = {"t": 0.0}
    monkeypatch.setattr(sp._time, "monotonic", lambda: clock["t"])

    def deliver(rid_, r, sid, rows_, name, ch, label, **k):
        clock["t"] += 3.0                         # each person takes 3 s of an 8 s window
        return "sent", {"employee_name": name}
    monkeypatch.setattr(sp, "_deliver_one", deliver)
    got = sp.deliver_notices(restaurant_id=rid, batch="b1", budget_seconds=8)
    assert len(got["sent"]) == 3 and got["hit_bound"]
    conn = models.get_conn()
    try:
        left = conn.execute("SELECT status, attempts, claimed_at FROM schedule_notices WHERE batch='b1' "
                            "AND status<>'sent'").fetchall()
    finally:
        conn.close()
    # Back as they were — due to the next pass at once, no try spent.
    assert [(r["status"], r["attempts"], r["claimed_at"]) for r in left] == [("pending", 0, None)] * 3
    queued = []
    monkeypatch.setattr(sp, "queue_delivery", lambda r, b, round_=0: queued.append((r, b)))
    clock["t"] = 0.0
    monkeypatch.setattr(sp, "INLINE_SECONDS", 4.0)
    sp.deliver_inline(rid, "b1")
    assert queued == [(rid, "b1")]                # what the inline window left goes to the queue


def test_the_queued_delivery_runs_the_batch_and_queues_the_rest(db, quiet, monkeypatch):
    import schedule_publish as sp
    jq = quiet
    calls = []
    monkeypatch.setattr(sp, "deliver_notices", lambda **k: calls.append(k) or {
        "sent": [1], "failed": [], "unreachable": [], "pending": ["Bo"], "attempted": 1, "hit_bound": True})
    sp.queue_delivery(7, "b9")
    sp.queue_delivery(7, "b9")                                    # one row a round
    # The notify group since the blind re-audit of 10/20/26 (R1-08).
    job = jq.claim("w", groups=("notify",))
    assert job["kind"] == "schedule_notices" and job.payload == {"restaurant_id": 7, "batch": "b9", "round": 0}
    out = jq.run_one(job)
    assert out["pending"] == 1 and calls[0]["batch"] == "b9" and calls[0]["budget_seconds"] == sp.NOTICE_QUEUE_SECONDS
    nxt = jq.claim("w", groups=("notify",))
    assert nxt is not None and nxt.payload["round"] == 1


def test_auto_publish_fans_out_one_queue_row_a_restaurant(db, quiet, monkeypatch):
    import scheduler
    import time_utils
    from datetime import datetime
    rid = _restaurant(db, module_labor=1, auto_publish_schedule=1, auto_draft_weekday=3, auto_draft_weekday_chosen=1,
                      billing_status="active")
    monkeypatch.setattr(scheduler, "_served_client", lambda r: True)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda r, naive=True: datetime(2026, 10, 16, 10, 0))  # Friday
    seen = []
    monkeypatch.setattr(scheduler, "local_due", lambda r, *a, **k: seen.append(r.id) or False)
    out = scheduler.run_auto_publish_schedules(enqueue=True)
    assert out["queued"] == 1 and out["ok"] == 1 and seen == []      # no gate run on the scheduler thread
    assert scheduler.run_auto_publish_schedules(enqueue=True)["queued"] == 0   # the same day queues nothing new
    job = quiet.claim("w", groups=("notify",))                       # R1-08: never behind the learners
    assert job["kind"] == "auto_publish" and job.payload == {"restaurant_id": rid}
    quiet.run_one(job)
    assert seen == [rid]                                              # the row runs the restaurant's own check


# ── A5-F15: a POS push a dead process left `sending` is tried again ─────────

def test_a_push_left_sending_by_a_dead_process_is_tried_again(db, monkeypatch):
    import pos_schedule_push as psp
    rid = _restaurant(db)
    conn = models.get_conn()
    try:
        ins = ("INSERT INTO pos_schedule_pushes (restaurant_id, history_id, week_start, provider, status, attempts, "
               "updated_at) VALUES (?,?,?,?,?,?,?)")
        conn.execute(ins, (rid, 1, "2026-10-19", "rpower", "sending", 1, "2026-10-01 09:00:00"))   # died mid-push
        conn.execute(ins, (rid, 2, "2026-10-26", "rpower", "sending", 4, "2026-10-01 09:00:00"))   # out of tries
        conn.execute(ins, (rid, 3, "2026-11-02", "rpower", "sending", 1,
                           time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))                        # running now
        conn.commit()
    finally:
        conn.close()
    assert psp._reap_stale() == 2
    conn = models.get_conn()
    try:
        rows = {r["history_id"]: dict(r) for r in conn.execute(
            "SELECT history_id, status, next_try_at, error FROM pos_schedule_pushes").fetchall()}
    finally:
        conn.close()
    assert rows[1]["status"] == "failed" and rows[1]["next_try_at"] and "interrupted" in rows[1]["error"]
    assert rows[2]["status"] == "failed" and rows[2]["next_try_at"] is None      # left for "push again"
    assert rows[3]["status"] == "sending"                                         # a live push is left alone
    # The queue run reaps first, then takes the due retry.
    ran = []
    monkeypatch.setattr(psp, "run_push", lambda pid, **k: ran.append(pid) or {"status": "sent"})
    conn = models.get_conn()
    try:
        conn.execute("UPDATE pos_schedule_pushes SET status='sending', updated_at='2026-10-01 09:00:00', attempts=1 "
                     "WHERE history_id=1")
        conn.commit()
        one = conn.execute("SELECT id FROM pos_schedule_pushes WHERE history_id=1").fetchone()[0]
    finally:
        conn.close()
    out = psp.run_queue()
    assert one in ran and out["ok"] >= 1


# ── A5-F16: a whole week never supersedes edits saved while it ran ──────────

WEEK = ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24", "2026-10-25"]


def _csv(names_by_date):
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"]
    for d, name in names_by_date:
        lines.append(f"{d},Monday,{name},Server,4:00pm,10:00pm,6,")
    return "\n".join(lines)


def test_the_save_refuses_a_working_copy_that_moved_since_the_week_began(db):
    rid = _restaurant(db)
    first = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Ana")]), [])
    conn = models.get_conn()
    try:
        row = models.week_draft(conn, rid, WEEK[0])
        seen = (int(row["id"]), row["schedule_csv"])
    finally:
        conn.close()
    # A manager's Studio save while the generation ran: the same row, new text.
    conn = models.get_conn()
    try:
        conn.execute("UPDATE schedule_history SET schedule_csv=? WHERE id=?", (_csv([(WEEK[0], "Bo")]), first))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(models.DraftChanged):
        models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Cy")]), [], week_copy=seen)
    conn = models.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM schedule_history WHERE restaurant_id=?", (rid,)).fetchone()[0] == 1
        assert conn.execute("SELECT superseded_by FROM schedule_history WHERE id=?", (first,)).fetchone()[0] is None
        row = models.week_draft(conn, rid, WEEK[0])
        now = (int(row["id"]), row["schedule_csv"])
    finally:
        conn.close()
    # Over the copy as it stands, it saves and supersedes it as before.
    new = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6, 0, 30, _csv([(WEEK[0], "Cy")]), [], week_copy=now)
    assert new != first
    # A week that had no working copy when it began, and has one now.
    other = ["2026-10-26", "2026-11-01"]
    models.save_schedule_history(rid, other[0], other[1], 6, 0, 30, _csv([(other[0], "Di")]), [])
    with pytest.raises(models.DraftChanged):
        models.save_schedule_history(rid, other[0], other[1], 6, 0, 30, _csv([(other[0], "Ed")]), [],
                                     week_copy=(None, ""))


def test_a_whole_week_runs_again_in_code_or_refuses_from_the_model(db, monkeypatch):
    import schedule_engine as se
    import schedule_job as sjob
    rid = _restaurant(db)
    req = sjob.Request("wk-1", rid, week_start=WEEK[0])
    built, commits = [], []

    class Cand:
        def __init__(self, code):
            self.result = {"draft_source": {"chosen": "solver" if code else "model"}}

    def make(code):
        def week_pass(job):
            built.append(job.week_copy)
            return Cand(code)
        return week_pass

    def commit(job, winner):
        commits.append(1)
        if len(commits) == 1:
            raise models.DraftChanged(1, "moved")
        return 7
    monkeypatch.setattr(sjob, "_commit", commit)
    monkeypatch.setattr(sjob, "_week_pass", make(True))
    job = sjob.GenerationRun(req, None)
    sjob._week(job)
    assert len(built) == 2 and len(commits) == 2                      # drafted again in code, saved over it
    assert built[0] == (WEEK[0], (None, ""))                          # the working copy it began from
    # From the model: never paid for twice — nothing saved, the edits stand.
    commits.clear()
    built.clear()
    monkeypatch.setattr(sjob, "_week_pass", make(False))
    with pytest.raises(se.ScheduleGenerationError) as why:
        sjob._week(sjob.GenerationRun(req, None))
    assert "your changes are kept" in str(why.value) and len(built) == 1
    # In code, at most WEEK_RERUNS_MAX more times.
    monkeypatch.setattr(sjob, "_week_pass", make(True))
    monkeypatch.setattr(sjob, "_commit", lambda job, winner: (_ for _ in ()).throw(models.DraftChanged(1, "x")))
    built.clear()
    with pytest.raises(se.ScheduleGenerationError):
        sjob._week(sjob.GenerationRun(req, None))
    assert len(built) == sjob.WEEK_RERUNS_MAX + 1
