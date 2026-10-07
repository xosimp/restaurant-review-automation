"""Connection reuse (owner, 10/3/26): a new SQLite connection re-reads the
whole schema (~2.6ms); get_conn hands out handles over pooled connections.
What a caller sees must not change — these hold the promises in
models.get_conn's comment."""
import sqlite3
import threading

import pytest

import models


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "pool.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE z (v)")
    c.commit()
    c.close()
    models.close_pooled_connections()
    yield p
    models.close_pooled_connections()


def test_a_handle_is_reused_after_close(path):
    a = models.get_conn(path)
    raw = a._conn
    a.close()
    b = models.get_conn(path)
    assert b._conn is raw
    b.close()


def test_a_nested_open_gets_its_own_connection(path):
    a = models.get_conn(path)
    b = models.get_conn(path)
    assert a._conn is not b._conn
    a.execute("INSERT INTO z VALUES (1)")
    b.close()                       # b's close never touches a's open work
    a.commit()
    a.close()
    c = models.get_conn(path)
    assert c.execute("SELECT count(*) FROM z").fetchone()[0] == 1
    c.close()


def test_uncommitted_work_is_discarded_on_close_as_before(path):
    a = models.get_conn(path)
    a.execute("INSERT INTO z VALUES (1)")
    a.close()
    b = models.get_conn(path)
    assert b.execute("SELECT count(*) FROM z").fetchone()[0] == 0 and not b.in_transaction
    b.close()


def test_a_closed_handle_refuses_use(path):
    a = models.get_conn(path)
    a.close()
    with pytest.raises(sqlite3.ProgrammingError):
        a.execute("SELECT 1")


def test_what_a_caller_changed_is_reset_or_the_connection_is_not_reused(path):
    a = models.get_conn(path)
    a.row_factory = None
    a.execute("PRAGMA foreign_keys=OFF")
    a.close()
    b = models.get_conn(path)
    assert b.row_factory is sqlite3.Row and b.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    raw = b._conn
    b.execute("PRAGMA query_only=ON")
    b.close()
    c = models.get_conn(path)
    assert c._conn is not raw and c.execute("PRAGMA query_only").fetchone()[0] == 0
    c.create_function("f", 0, lambda: 1)
    raw = c._conn
    c.close()
    d = models.get_conn(path)
    assert d._conn is not raw
    d.close()


def test_a_dropped_handle_is_returned_and_threads_do_not_share(path):
    models.get_conn(path).execute("SELECT 1")
    mine = models.get_conn(path)
    seen = {}

    def other():
        h = models.get_conn(path)
        seen["raw"] = h._conn
        h.close()
    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert seen["raw"] is not mine._conn
    mine.close()


def test_a_replaced_file_is_never_served_from_the_pool(path, tmp_path):
    import os
    a = models.get_conn(path)
    raw = a._conn
    a.close()
    os.replace(str(tmp_path / "pool.db"), str(tmp_path / "old.db"))
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE fresh (v)")
    c.commit()
    c.close()
    b = models.get_conn(path)
    assert b._conn is not raw and b.execute("SELECT count(*) FROM sqlite_master WHERE name='fresh'").fetchone()[0] == 1
    b.close()


def test_the_kill_switch_opens_and_closes_for_real(path, monkeypatch):
    monkeypatch.setenv("CAVNAR_CONN_POOL", "0")
    a = models.get_conn(path)
    assert isinstance(a, sqlite3.Connection)
    a.close()


def test_memory_databases_are_never_pooled():
    a = models.get_conn(":memory:")
    a.execute("CREATE TABLE t (v)")
    a.close()
    b = models.get_conn(":memory:")
    assert b.execute("SELECT count(*) FROM sqlite_master WHERE name='t'").fetchone()[0] == 0
    b.close()


# ── synchronous=NORMAL under WAL (AI cost audit 10/7/26 #92) ──────────────

def _sync_level(conn):
    return conn.execute("PRAGMA synchronous").fetchone()[0]


def test_a_new_connection_is_wal_with_synchronous_normal(path, monkeypatch):
    monkeypatch.delenv("SQLITE_SYNCHRONOUS", raising=False)
    models.close_pooled_connections()
    c = models.get_conn(path)
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert _sync_level(c) == 1          # NORMAL
    finally:
        c.close()


def test_a_pooled_connection_keeps_its_level_and_full_is_the_override(path, monkeypatch):
    monkeypatch.delenv("SQLITE_SYNCHRONOUS", raising=False)
    models.close_pooled_connections()
    a = models.get_conn(path)
    a.close()
    b = models.get_conn(path)                # the same connection, reused
    assert _sync_level(b) == 1
    b.close()
    models.close_pooled_connections()
    monkeypatch.setenv("SQLITE_SYNCHRONOUS", "full")
    c = models.get_conn(path)
    try:
        assert _sync_level(c) == 2          # FULL, the old behaviour
    finally:
        c.close()
    monkeypatch.setenv("CAVNAR_CONN_POOL", "0")
    monkeypatch.setenv("SQLITE_SYNCHRONOUS", "NORMAL")
    d = models.get_conn(path)                # the unpooled path sets it too
    try:
        assert _sync_level(d) == 1
    finally:
        d.close()


def test_normal_is_never_set_outside_wal(monkeypatch):
    """In a rollback-journal database NORMAL can corrupt on power loss, so a
    connection whose journal is not WAL (":memory:") keeps FULL."""
    monkeypatch.delenv("SQLITE_SYNCHRONOUS", raising=False)
    c = models.get_conn(":memory:")
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
        assert _sync_level(c) == 2
    finally:
        c.close()
    assert models.sqlite_synchronous() == "NORMAL"
    monkeypatch.setenv("SQLITE_SYNCHRONOUS", "bogus")
    assert models.sqlite_synchronous() == "NORMAL"


def test_a_caller_changing_synchronous_is_never_handed_on(path):
    a = models.get_conn(path)
    raw = a._conn
    a.execute("PRAGMA synchronous=OFF")
    a.close()
    b = models.get_conn(path)
    try:
        assert b._conn is not raw and _sync_level(b) == 1
    finally:
        b.close()
