"""Audit #25 A07-15 / A07-08 — a multi-statement write is one unit.

Every statement is atomic on its own (db_dialect's per-statement savepoint
on Postgres, matching SQLite), so a failure a broad handler swallowed inside
a unit of several writes committed the rest half-applied.
models.atomic(conn) makes the unit one SAVEPOINT: a failure inside rolls all
of it back. On SQLite and on Postgres (CAVNAR_TEST_PG_URL routes db_path).
"""
import pytest

import models


def _value(db_path, key):
    conn = models.get_conn(db_path)
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_a_failure_in_the_second_statement_rolls_back_the_first(db_path):
    conn = models.get_conn(db_path)
    try:
        with pytest.raises(Exception):
            with models.atomic(conn, "ws6_unit"):
                conn.execute("INSERT INTO job_cursors (key, value) VALUES ('ws6:first', '1')")
                conn.execute("INSERT INTO ws6_no_such_table (a) VALUES (1)")
        conn.commit()
    finally:
        conn.close()
    assert _value(db_path, "ws6:first") is None, "half a unit committed"


def test_a_unit_that_succeeds_lands_and_nests(db_path):
    conn = models.get_conn(db_path)
    try:
        with models.atomic(conn, "outer"):
            conn.execute("INSERT INTO job_cursors (key, value) VALUES ('ws6:a', '1')")
            with pytest.raises(Exception):
                with models.atomic(conn, "inner"):
                    conn.execute("INSERT INTO job_cursors (key, value) VALUES ('ws6:b', '2')")
                    raise RuntimeError("the inner unit fails")
            conn.execute("INSERT INTO job_cursors (key, value) VALUES ('ws6:c', '3')")
        conn.commit()
    finally:
        conn.close()
    assert (_value(db_path, "ws6:a"), _value(db_path, "ws6:b"), _value(db_path, "ws6:c")) == ("1", None, "3")


def test_the_feedback_repair_is_one_unit(db_path, monkeypatch):
    """intelligence.feedback._repair_per_episode: its deletes, stamp, cursors
    and mark land together. A failure part-way (here, writing the mark) used
    to commit the deletes with the cursors where they were."""
    from intelligence import feedback
    rid = models.create_restaurant(models.Restaurant(name="Harbor Grill", owner_email="o@harbor.test"),
                                   db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        conn.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at, synced_from) "
                     "VALUES (?, 'labor', 'rec-1', 'tracking', datetime('now'), 'recommendation_outcomes')", (rid,))
        conn.commit()
        real = feedback._cursor_set

        def failing(c, value, key=feedback.LEDGER_CURSOR):
            if key == feedback.PER_EPISODE_REPAIR_MARK:
                raise RuntimeError("disk full")
            return real(c, value, key=key)
        monkeypatch.setattr(feedback, "_cursor_set", failing)
        with pytest.raises(RuntimeError):
            feedback._repair_per_episode(conn)
        conn.commit()
        left = conn.execute("SELECT COUNT(*) FROM intel_rec_events WHERE source_key='rec-1'").fetchone()[0]
        assert left == 1, "the delete committed without its mark"
        monkeypatch.setattr(feedback, "_cursor_set", real)
        assert feedback._repair_per_episode(conn) is True
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM intel_rec_events WHERE source_key='rec-1'").fetchone()[0] == 0
        assert feedback._marked(conn, feedback.PER_EPISODE_REPAIR_MARK) is True
    finally:
        conn.close()


def test_a_failed_coverage_mark_is_recorded_not_swallowed(db_path, monkeypatch):
    import ops
    from dsr import store
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, job="unknown", context="", **k: seen.append((job, context)))
    # No such restaurant row: the foreign key holds on Postgres; on SQLite
    # break the table instead.
    conn = models.get_conn(db_path)
    try:
        conn.execute("DROP TABLE dsr_coverage_runs")
        conn.commit()
    finally:
        conn.close()
    store.mark_coverage_ran(5, "2026-10-25", db_path=db_path)        # never raises
    assert seen and seen[0][0] == "dsr_coverage" and "restaurant_id=5" in seen[0][1]
