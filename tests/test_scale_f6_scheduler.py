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
