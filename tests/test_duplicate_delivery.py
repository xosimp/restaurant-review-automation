"""Audit #4 P1: the three paths that could deliver the same thing twice.

A duplicate DocuSign callback re-sent a new client both their payment link and
their temporary password. An APNs retry after a timeout put the same alert on
the same phone up to three times, because Apple may well have delivered the
attempt whose response got lost. And a job that died without raising left no
failure row at all, so the 8am digest reported silence.
"""
import pytest

import models
import ops
import push


# ── DocuSign: at-least-once delivery, exactly-once side effects ────────────

@pytest.fixture()
def _wh(db_path, monkeypatch):
    import webhook_routes
    real = models.get_conn
    monkeypatch.setattr(webhook_routes, "get_conn", lambda *a, **k: real(db_path), raising=False)
    return webhook_routes


def test_a_repeat_docusign_callback_is_claimed_once(_wh):
    assert _wh._claim_docusign_event("env-1", "completed") is True
    assert _wh._claim_docusign_event("env-1", "completed") is False
    assert _wh._claim_docusign_event("env-1", "completed") is False


def test_different_envelopes_are_independent(_wh):
    assert _wh._claim_docusign_event("env-1", "completed") is True
    assert _wh._claim_docusign_event("env-2", "completed") is True


def test_a_missing_envelope_id_is_never_swallowed(_wh):
    """Better to process an unidentifiable callback than to drop the one that
    starts a paying client's account."""
    assert _wh._claim_docusign_event("", "completed") is True
    assert _wh._claim_docusign_event("", "completed") is True


def test_the_claim_fails_open_when_the_database_is_unreachable(_wh, monkeypatch):
    monkeypatch.setattr(_wh, "get_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")),
                        raising=False)
    assert _wh._claim_docusign_event("env-9", "completed") is True


# ── APNs: a retry must land as one banner, not three ───────────────────────

def test_retries_of_one_alert_share_a_collapse_id():
    a = push._collapse_id(4, "1star", {"review_id": 991})
    b = push._collapse_id(4, "1star", {"review_id": 991})
    assert a == b


def test_two_different_reviews_stay_two_notifications():
    """Over-collapsing would be its own bug: two 1-star reviews in one fetch
    are two things the owner needs to see."""
    a = push._collapse_id(4, "1star", {"review_id": 991})
    b = push._collapse_id(4, "1star", {"review_id": 992})
    assert a != b


def test_restaurants_never_share_a_collapse_id():
    assert push._collapse_id(4, "1star", {"review_id": 1}) != push._collapse_id(5, "1star", {"review_id": 1})


def test_alert_types_never_share_a_collapse_id():
    assert push._collapse_id(4, "labor_over", {}) != push._collapse_id(4, "food_waste", {})


def test_apple_will_accept_it():
    """APNs caps apns-collapse-id at 64 bytes and rejects the push otherwise."""
    long_type = "x" * 300
    for key in (push._collapse_id(999999, long_type, {}),
                push._collapse_id(1, "1star", {"review_id": 2 ** 62}),
                push._collapse_id(1, "labor_over", {})):
        assert 0 < len(key.encode()) <= 64, key


def test_the_header_is_actually_sent():
    import inspect
    src = inspect.getsource(push._deliver)
    assert '"apns-collapse-id"' in src, "the collapse id is computed but never sent"


# ── A job that died without raising must not read as silence ───────────────

@pytest.fixture(autouse=True)
def _ops_db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _run_row(db_path, job, minutes_ago, finished):
    conn = models.get_conn(db_path)
    conn.execute(ops._RUNS_SQL)
    conn.execute(
        "INSERT INTO job_runs (job, started_at, finished_at, ok) "
        "VALUES (?, datetime('now', ?), ?, ?)",
        (job, f"-{minutes_ago} minutes", "2026-01-01 00:00:00" if finished else None,
         1 if finished else None))
    conn.commit()
    conn.close()


def test_a_job_that_started_and_never_finished_is_reported(db_path):
    _run_row(db_path, "daily_alerts", 240, finished=False)
    jobs = [j["job"] for j in ops.stuck_jobs()]
    assert "daily_alerts" in jobs


def test_a_job_still_running_is_not_reported(db_path):
    _run_row(db_path, "review_fetch", 5, finished=False)
    assert ops.stuck_jobs() == []


def test_a_completed_job_is_not_reported(db_path):
    _run_row(db_path, "pos_sync", 240, finished=True)
    assert ops.stuck_jobs() == []


def test_the_digest_sends_for_stuck_jobs_even_with_zero_failures(db_path, monkeypatch):
    """The whole point: capture() never fired, so failures_last_24h() is
    empty and the digest used to return early and say nothing."""
    _run_row(db_path, "monthly_summary", 240, finished=False)
    monkeypatch.setattr(ops, "failures_last_24h", lambda: [])
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    sent = {}

    class _Emails:
        @staticmethod
        def send(payload):
            sent.update(payload)
            return {"id": "e1"}

    import sys, types
    monkeypatch.setitem(sys.modules, "resend", types.SimpleNamespace(api_key=None, Emails=_Emails))

    assert ops.send_failure_digest() is True
    assert "monthly_summary" in sent["html"]
    assert "never finished" in sent["subject"]


def test_the_digest_still_stays_quiet_when_nothing_is_wrong(db_path, monkeypatch):
    """Silence has to keep meaning something."""
    monkeypatch.setattr(ops, "failures_last_24h", lambda: [])
    assert ops.send_failure_digest() is False
