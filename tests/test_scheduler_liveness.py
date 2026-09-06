"""The scheduler can't notice its own death.

Scheduled marketing posts publish from a thread inside the web process. If it
stops, nothing raises and nothing 500s — posts just quietly never go out, and
the first sign is a client asking why. These pin the two things that do
notice: a heartbeat read from a request thread, and the shape a stopped
scheduler leaves in the queue.
"""
from datetime import datetime, timedelta

import pytest

import models
import status_manager


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    status_manager.seed_default_services()
    # marketing_scheduled_posts.restaurant_id is a real foreign key.
    models.create_restaurant(
        models.Restaurant(name="Liveness Co", owner_email="l@x.com"), db_path=db_path)


def _stamp_scheduler(db_path, minutes_ago):
    when = (datetime.utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn = models.get_conn(db_path)
    conn.execute("UPDATE service_status SET updated_at=?, status='operational' WHERE service_key='scheduler'",
                 (when,))
    conn.commit()
    conn.close()


def _status(db_path, key):
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT status, message FROM service_status WHERE service_key=?", (key,)).fetchone()
    conn.close()
    return dict(row) if row else None


def test_a_fresh_heartbeat_reads_as_recent(db_path):
    status_manager.record_scheduler_heartbeat()
    age = status_manager.scheduler_heartbeat_age_minutes()
    assert age is not None and age < 2


def test_a_stale_heartbeat_is_reported_as_an_outage(db_path):
    _stamp_scheduler(db_path, status_manager.SCHEDULER_STALE_MINUTES + 10)

    age = status_manager.check_scheduler_liveness()

    assert age > status_manager.SCHEDULER_STALE_MINUTES
    state = _status(db_path, "scheduler")
    assert state["status"] == "outage"
    assert "scheduled posts" in state["message"]


def test_a_recent_heartbeat_is_left_alone(db_path):
    _stamp_scheduler(db_path, 2)
    status_manager.check_scheduler_liveness()
    assert _status(db_path, "scheduler")["status"] == "operational"


def _queue_post(db_path, status, scheduled_for, posted_at=None):
    conn = models.get_conn(db_path)
    conn.execute(
        "INSERT INTO marketing_scheduled_posts "
        "(restaurant_id, platform, body, scheduled_for, status, posted_at, created_at) "
        "VALUES (1,'facebook','copy',?,?,?,datetime('now'))",
        (scheduled_for, status, posted_at))
    conn.commit()
    conn.close()


def test_posts_sitting_past_their_slot_read_as_an_outage(db_path):
    """The shape a stopped scheduler makes: queued, due, and still there."""
    overdue = (datetime.utcnow() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    _queue_post(db_path, "scheduled", overdue)

    status_manager._check_scheduled_posts()

    state = _status(db_path, "scheduled_posts")
    assert state["status"] == "outage"
    assert "past their scheduled time" in state["message"]


def test_a_queue_that_is_merely_waiting_is_operational(db_path):
    later = (datetime.utcnow() + timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    _queue_post(db_path, "scheduled", later)

    status_manager._check_scheduled_posts()

    assert _status(db_path, "scheduled_posts")["status"] == "operational"


def test_one_failure_among_successes_is_not_a_platform_outage(db_path):
    """A single failure is the owner's to fix and they are emailed about it."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    _queue_post(db_path, "failed", now, posted_at=now)
    for _ in range(4):
        _queue_post(db_path, "posted", now, posted_at=now)

    status_manager._check_scheduled_posts()

    assert _status(db_path, "scheduled_posts")["status"] == "operational"


def test_everything_failing_and_nothing_posting_is_an_outage(db_path):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(2):
        _queue_post(db_path, "failed", now, posted_at=now)

    status_manager._check_scheduled_posts()

    assert _status(db_path, "scheduled_posts")["status"] == "outage"


def test_several_failures_alongside_successes_is_degraded(db_path):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(3):
        _queue_post(db_path, "failed", now, posted_at=now)
    for _ in range(5):
        _queue_post(db_path, "posted", now, posted_at=now)

    status_manager._check_scheduled_posts()

    state = _status(db_path, "scheduled_posts")
    assert state["status"] == "degraded"
    assert "3 of 8" in state["message"]


def test_a_newly_added_service_starts_reporting_without_a_status_page_visit(db_path):
    """update_service_status is a bare UPDATE, so a service added to SERVICES
    after a database was created had no row to update and every check for it
    was a silent no-op."""
    conn = models.get_conn(db_path)
    conn.execute("DELETE FROM service_status WHERE service_key='scheduled_posts'")
    conn.commit()
    conn.close()

    status_manager.run_health_checks()

    assert _status(db_path, "scheduled_posts") is not None
