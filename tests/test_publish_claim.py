"""Audit #4 P0: a scheduled post could publish twice.

run_due_posts used to call publish_now() while the row still said
'scheduled', writing the outcome only afterwards. A crash between the Graph
call and the UPDATE, or a timeout on a request the platform accepted, left
the row due — and the next tick, five minutes later, published it again.
This is the only defect in that audit whose damage lands on the restaurant's
public feed, so these pin the claim hard.
"""
import sqlite3
from datetime import timedelta

import pytest

import marketing_publish as mp


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import models
    p = str(tmp_path / "t.db")
    models.init_db(p)
    models.ensure_columns(p)
    conn = models.get_conn(p)
    conn.execute("INSERT INTO restaurants (id, name, owner_email) VALUES (1, 'The Copper Table', 'o@example.com')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(mp, "DB_PATH", p)
    return p


def _schedule(db, minutes_ago=5, status="scheduled"):
    """Due, but inside LATE_TOLERANCE_HOURS — a slot further back than that is
    deliberately failed rather than published, which is a different path."""
    when = (mp._local_now(1) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO marketing_scheduled_posts (restaurant_id, platform, body, scheduled_for, status) "
        "VALUES (1, 'facebook', 'Half price wings tonight', ?, ?)", (when, status))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def _status(db, row_id):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT status FROM marketing_scheduled_posts WHERE id=?", (row_id,)).fetchone()[0]
    finally:
        conn.close()


def test_only_one_claim_can_win(db):
    row_id = _schedule(db)
    assert mp._claim_for_publish(row_id, db_path=db) is True
    # A second tick arriving while the first is still in the Graph call.
    assert mp._claim_for_publish(row_id, db_path=db) is False
    assert _status(db, row_id) == "publishing"


def test_a_timeout_after_the_request_went_out_is_never_retried(db, monkeypatch):
    """The whole point. publish_now raised, so the platform may have it."""
    row_id = _schedule(db)
    calls = []

    def _publish(*a, **kw):
        calls.append(1)
        return {"ok": False, "error": "That platform rejected the post", "reached_platform": True}

    monkeypatch.setattr(mp, "publish_now", _publish)
    monkeypatch.setattr(mp, "_alert_failed_post", lambda *a, **kw: None)

    mp.run_due_posts(db_path=db)
    assert calls == [1]
    assert _status(db, row_id) == "failed"

    # The next tick, and every tick after it, must not publish again.
    mp.run_due_posts(db_path=db)
    mp.run_due_posts(db_path=db)
    assert calls == [1], "a post that may already be live was published again"


def test_a_definite_rejection_is_still_retried(db, monkeypatch):
    """Nothing reached the platform, so retrying costs nothing and a
    transient failure shouldn't burn the post."""
    row_id = _schedule(db)
    calls = []

    def _publish(*a, **kw):
        calls.append(1)
        return {"ok": False, "error": "Instagram needs a photo", "reached_platform": False}

    monkeypatch.setattr(mp, "publish_now", _publish)
    monkeypatch.setattr(mp, "_alert_failed_post", lambda *a, **kw: None)

    mp.run_due_posts(db_path=db)
    assert _status(db, row_id) == "scheduled"
    mp.run_due_posts(db_path=db)
    assert len(calls) == 2
    # MAX_ATTEMPTS reached
    mp.run_due_posts(db_path=db)
    assert _status(db, row_id) == "failed"
    mp.run_due_posts(db_path=db)
    assert len(calls) == mp.MAX_ATTEMPTS, "kept retrying past MAX_ATTEMPTS"


def test_a_success_publishes_exactly_once(db, monkeypatch):
    row_id = _schedule(db)
    calls = []
    monkeypatch.setattr(mp, "publish_now",
                        lambda *a, **kw: calls.append(1) or {"ok": True, "post_id": "p1"})
    mp.run_due_posts(db_path=db)
    mp.run_due_posts(db_path=db)
    assert calls == [1]
    assert _status(db, row_id) == "posted"


def test_a_row_abandoned_mid_publish_is_failed_not_republished(db, monkeypatch):
    """Simulates the process dying between the Graph call and the UPDATE:
    the row is left in 'publishing' with an old claim stamp."""
    row_id = _schedule(db)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE marketing_scheduled_posts SET status='publishing', "
                 "claimed_at=datetime('now','-45 minutes') WHERE id=?", (row_id,))
    conn.commit()
    conn.close()

    alerted = []
    monkeypatch.setattr(mp, "_alert_failed_post", lambda row, err, **kw: alerted.append(err))
    calls = []
    monkeypatch.setattr(mp, "publish_now", lambda *a, **kw: calls.append(1) or {"ok": True})

    mp.run_due_posts(db_path=db)
    assert calls == [], "an abandoned row was published again"
    assert _status(db, row_id) == "failed"
    assert alerted, "the owner was never told the post may not have gone out"


def test_a_fresh_claim_is_not_reaped_out_from_under_a_running_publish(db):
    row_id = _schedule(db)
    mp._claim_for_publish(row_id, db_path=db)
    assert mp.reap_stuck_publishes(db_path=db) == 0
    assert _status(db, row_id) == "publishing"
