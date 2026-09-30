"""Memory re-audit fix round (9/29/26), R9 — "rollup_failures" (FORGET-11, FORGET-19).

Every registered rollup raises on failure, so ops' registry keeps that
night's raw rows (a swallowed failure read as a clean rollup and the rows
were pruned with no summary). And a null-subject read group in the
quarterly summary counts only its own reads' claims.
"""
import sqlite3

import pytest

import ai_reads
import ai_utils
import history_rollups
import models
import ops


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default, db_path) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ai_reads, "get_conn", redirected, raising=False)
    yield


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def test_ai_reads_rollup_raises_when_a_summary_fails(db_path, monkeypatch):
    _x(db_path, "INSERT INTO ai_reads (restaurant_id, surface, text_hash, shown_text, rec_keys, created_at) "
                "VALUES (1, 'labor', 'h', 'x', '[\"trim_day\"]', date('now', '-200 days'))")

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(ai_reads, "_action_state", broken)
    with pytest.raises(RuntimeError, match="quarterly summaries failed"):
        ai_reads.rollup_quarters(db_path)
    # The learning pass's own call still never raises.
    assert ai_reads.summarise_quarters(1, db_path=db_path) == 0


def test_a_failed_ai_reads_rollup_keeps_the_raw_reads(db_path, monkeypatch):
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: {"sent": True})
    _x(db_path, "INSERT INTO ai_reads (restaurant_id, surface, text_hash, shown_text, created_at) "
                "VALUES (1, 'labor', 'h', 'x', datetime('now', '-500 days'))")
    monkeypatch.setattr(ai_reads, "summarise_quarters",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug")))
    ops.prune_ledgers(db_path)
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM ai_reads").fetchone()[0] == 1


def test_validation_rollup_failure_raises(db_path, monkeypatch):
    _x(db_path, "INSERT INTO ai_validation_log (restaurant_id, surface, verdict, created_at) "
                "VALUES (1, 's', 'pass', datetime('now'))")

    def broken(conn, day):
        raise sqlite3.OperationalError("database disk image is malformed")
    monkeypatch.setattr(ai_utils, "_roll_validation_day", broken)
    with pytest.raises(sqlite3.OperationalError):
        ai_utils.rollup_usage(db_path=db_path)


def test_roll_alerts_raises_rather_than_writing_zero_opens(db_path, monkeypatch):
    _x(db_path, "INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (1, 'x', datetime('now','-40 days'))")
    _x(db_path, "ALTER TABLE notification_opens RENAME COLUMN opened_at TO opened_when")
    with pytest.raises(sqlite3.OperationalError):
        history_rollups.roll_alerts(db_path)


def test_newsletter_stamp_waits_when_tracking_is_unreadable(db_path, monkeypatch):
    import guest_email
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path)
    _x(db_path, "INSERT INTO guest_newsletters (restaurant_id, subject, body, content_hash, created_at) "
                "VALUES (1, 's', 'b', 'h', datetime('now', '-40 days'))")
    monkeypatch.setattr(guest_email, "opens_tracked",
                        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    with pytest.raises(sqlite3.OperationalError):
        history_rollups.stamp_newsletter_results(db_path)
    assert sqlite3.connect(db_path).execute(
        "SELECT results_stamped_at FROM guest_newsletters").fetchone()[0] is None


def test_an_unscoped_read_group_counts_only_its_own_claims(db_path):
    c = sqlite3.connect(db_path)
    try:
        # One unscoped read with 1 claim, one "Dana" read with 2 claims, same surface and quarter.
        c.execute("INSERT INTO ai_reads (id, restaurant_id, surface, subject, text_hash, shown_text, created_at) "
                  "VALUES (10, 1, 'labor', NULL, 'a', 'whole read', '2025-01-10 10:00:00')")
        c.execute("INSERT INTO ai_reads (id, restaurant_id, surface, subject, text_hash, shown_text, created_at) "
                  "VALUES (11, 1, 'labor', 'Dana', 'b', 'dana read', '2025-01-11 10:00:00')")
        for rid_, subj in ((10, "whole"), (11, "Dana"), (11, "Dana")):
            c.execute("INSERT INTO ai_claims (restaurant_id, read_id, last_read_id, surface, subject, claim_type, "
                      "text, verdict, created_at) VALUES (1, ?, ?, 'labor', ?, 'cause', 't', 'held', "
                      "'2025-01-12 10:00:00')", (rid_, rid_, subj))
        c.commit()
    finally:
        c.close()
    ai_reads.summarise_quarters(1, today="2025-05-01", db_path=db_path)
    c = sqlite3.connect(db_path)
    rows = dict(c.execute("SELECT subject, claims_n FROM ai_read_summaries WHERE quarter='2025-Q1'").fetchall())
    assert rows == {"": 1, "Dana": 2}
