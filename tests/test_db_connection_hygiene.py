"""SQLite connections are closed even when a request fails.

From the pre-launch audit. The prevailing shape in this codebase is
`conn = get_conn(); ...; conn.close()`. That closes on every normal path —
no site was found that forgets one — but not when something raises in
between, and the exception's traceback keeps the frame, and therefore the
connection, alive well past the failure. On SQLite that matters: a leaked
connection that was mid-write holds a RESERVED lock, and every other writer
sits behind it for the full 30-second busy timeout.

models.db_conn (a context manager) is the shape new code should use;
close_thread_connections is the net under the wire for the ~90 call sites
that predate it.
"""
import sqlite3

import pytest
from flask import Flask

import models
from models import Restaurant, close_thread_connections, create_restaurant, db_conn, get_conn


def _is_closed(conn):
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True


@pytest.fixture(autouse=True)
def _clean_bag():
    close_thread_connections()
    yield
    close_thread_connections()


# ── the context manager ─────────────────────────────────────────────────────

def test_db_conn_closes_on_the_happy_path(db_path):
    with db_conn(db_path) as conn:
        conn.execute("SELECT 1")
    assert _is_closed(conn)


def test_db_conn_closes_when_the_block_raises(db_path):
    with pytest.raises(RuntimeError):
        with db_conn(db_path) as conn:
            conn.execute("SELECT 1")
            raise RuntimeError("mid-block failure")
    assert _is_closed(conn)


def test_db_conn_rolls_back_an_uncommitted_write(db_path):
    rid = create_restaurant(Restaurant(name="Rollback Co", owner_email="r@x.test"), db_path=db_path)
    with pytest.raises(RuntimeError):
        with db_conn(db_path) as conn:
            conn.execute("UPDATE restaurants SET name='Clobbered' WHERE id=?", (rid,))
            raise RuntimeError("failed before commit")
    check = get_conn(db_path)
    assert check.execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()[0] == "Rollback Co"
    check.close()


# ── the sweep ───────────────────────────────────────────────────────────────

def test_the_sweep_closes_a_connection_nobody_closed(db_path):
    leaked = get_conn(db_path)
    assert close_thread_connections() == 1
    assert _is_closed(leaked)


def test_the_sweep_does_not_count_connections_that_were_closed_properly(db_path):
    conn = get_conn(db_path)
    conn.close()
    assert close_thread_connections() == 0


def test_the_sweep_releases_a_write_lock_a_failed_request_left_behind(db_path):
    """The failure this exists to prevent: a connection abandoned mid-write
    holds a RESERVED lock and every other writer waits out the busy timeout."""
    rid = create_restaurant(Restaurant(name="Lock Co", owner_email="l@x.test"), db_path=db_path)
    stuck = get_conn(db_path)
    stuck.execute("UPDATE restaurants SET name='half-written' WHERE id=?", (rid,))  # no commit

    other = sqlite3.connect(db_path, timeout=0.2)
    with pytest.raises(sqlite3.OperationalError):
        other.execute("UPDATE restaurants SET name='mine' WHERE id=?", (rid,))
        other.commit()

    assert close_thread_connections() == 1
    other.execute("UPDATE restaurants SET name='mine' WHERE id=?", (rid,))
    other.commit()
    assert other.execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()[0] == "mine"
    other.close()


def test_the_sweep_is_safe_to_run_when_nothing_was_opened():
    assert close_thread_connections() == 0
    assert close_thread_connections() == 0


# ── wired into the app ──────────────────────────────────────────────────────

def test_a_route_that_raises_does_not_leave_a_connection_open(db_path, monkeypatch):
    """End to end through Flask's teardown, which is where the real net is."""
    app = Flask(__name__)
    opened = {}

    @app.route("/boom")
    def boom():
        opened["conn"] = get_conn(db_path)
        opened["conn"].execute("SELECT 1")
        raise RuntimeError("handler blew up")

    @app.teardown_appcontext
    def _sweep(exc):
        opened["leaked"] = close_thread_connections()

    client = app.test_client()
    assert client.get("/boom").status_code == 500  # Flask catches it, as in production

    assert opened["leaked"] == 1
    assert _is_closed(opened["conn"])
