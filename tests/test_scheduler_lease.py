"""Audit #4 P2: one scheduler per deployment, enforced in the database.

Everything in scheduler_loop is gated by claim_period(), so two schedulers
mostly collide harmlessly — but run_due_posts has no claim of its own (it has
to publish within minutes of a slot, not once a day), and claim_period
deliberately fails OPEN, so one database hiccup drops the guard for every job
at once. The only thing that had ever prevented that was `--workers 1` in
railway.json: one character of deploy config between today's behaviour and
every scheduled job running twice.
"""
import pytest

import models
import ops


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _backdate(db_path, expr="-2 hours"):
    """Stand in for the holder being SIGKILLed: its heartbeat stops advancing."""
    conn = models.get_conn(db_path)
    conn.execute(f"UPDATE scheduler_lease SET heartbeat_at=datetime('now','{expr}') WHERE id=1")
    conn.commit()
    conn.close()


def test_only_one_process_holds_the_lease(db_path):
    assert ops.acquire_scheduler_lease("worker-A") is True
    assert ops.acquire_scheduler_lease("worker-B") is False
    assert ops.scheduler_lease_holder()["owner"] == "worker-A"


def test_the_holder_can_renew_indefinitely(db_path):
    ops.acquire_scheduler_lease("worker-A")
    for _ in range(5):
        assert ops.acquire_scheduler_lease("worker-A") is True
    assert ops.acquire_scheduler_lease("worker-B") is False


def test_a_dead_holder_is_taken_over_rather_than_stopping_the_scheduler(db_path):
    ops.acquire_scheduler_lease("worker-A")
    assert ops.acquire_scheduler_lease("worker-B") is False
    _backdate(db_path)
    assert ops.acquire_scheduler_lease("worker-B") is True, \
        "a crashed holder would have stopped every scheduled job forever"
    assert ops.scheduler_lease_holder()["owner"] == "worker-B"
    # And the old owner does not get it back while B is alive.
    assert ops.acquire_scheduler_lease("worker-A") is False


def test_only_one_of_several_contenders_wins_a_free_lease(db_path):
    """Four workers boot together after a deploy; the lease starts unheld."""
    winners = [w for w in ("A", "B", "C", "D") if ops.acquire_scheduler_lease(w)]
    assert winners == ["A"], f"more than one scheduler started: {winners}"


def test_a_stale_window_shorter_than_a_tick_is_what_makes_this_dangerous(db_path):
    """Documents why SCHEDULER_LEASE_STALE_SECONDS must exceed the tick: with
    a tiny window a slow pass loses its own lease mid-run and a second process
    starts alongside it, which is the exact failure the lease exists to stop."""
    ops.acquire_scheduler_lease("worker-A")
    _backdate(db_path, "-5 seconds")
    assert ops.acquire_scheduler_lease("worker-B", stale_seconds=1) is True
    assert ops.SCHEDULER_LEASE_STALE_SECONDS > 300, \
        "the default window must comfortably exceed the 300s scheduler tick"


def test_it_fails_open_so_a_database_outage_cannot_stop_every_job(db_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(models, "get_conn", _boom, raising=False)
    assert ops.acquire_scheduler_lease("worker-A") is True
    assert ops.scheduler_lease_holder() is None
