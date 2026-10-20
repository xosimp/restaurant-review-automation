"""Blind re-audit 10/20/26, refix round G2 — the database layer on Postgres
(R2-04 … R2-13).

The first half needs no server. The second half runs against a real
Postgres when CAVNAR_TEST_PG_URL names a scratch server; anything touching
a partitioned table runs on a database built by pg_copy (production's
shape), never on the suite's unpartitioned boot."""
import contextlib
import gc
import io
import os
import sqlite3
import threading
import time
import uuid
from datetime import date

import pytest

import db_dialect
import models

PG = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()
live = pytest.mark.skipif(not PG, reason="CAVNAR_TEST_PG_URL names no scratch Postgres")


# ── R2-04: `col IS ?` decided per value ──────────────────────────────────────

def test_is_param_becomes_equality_or_is_null_by_its_value():
    tr = db_dialect.Translator()
    sql = "SELECT 1 FROM ai_calls WHERE restaurant_id IS ? AND action=? LIMIT 1"
    marked = tr.translate(sql, marked=True)
    text, params = db_dialect.bind_is(marked, (7, "a"))
    assert "(restaurant_id = %s AND restaurant_id IS NOT NULL) AND action=%s" in text and params == (7, "a")
    text, params = db_dialect.bind_is(marked, (None, "a"))
    assert "restaurant_id IS NULL AND action=%s" in text and params == ("a",)
    # A qualified column, two in one statement, the static form unchanged.
    m2 = tr.translate("SELECT 1 FROM r WHERE r.w=? AND r.restaurant_id IS ? AND subject IS ?", marked=True)
    text, params = db_dialect.bind_is(m2, ("w", None, "s"))
    assert "r.restaurant_id IS NULL AND (subject = %s AND subject IS NOT NULL)" in text and params == ("w", "s")
    assert "user_id IS NOT DISTINCT FROM %s" in tr.translate("SELECT 1 FROM a WHERE user_id IS ?")
    # What PREPARE checks (scripts/pg_sql_audit.py): the value form.
    assert db_dialect.bind_is(marked, None)[0].count("%s") == 2
    # executemany's rows may differ: the static form.
    assert "IS NOT DISTINCT FROM" in db_dialect.static_is(marked)


def test_is_param_stays_null_safe_where_the_comparison_is_not_a_plain_column():
    tr = db_dialect.Translator()
    for sql in ("SELECT 1 FROM t WHERE a || b IS ?", "SELECT 1 FROM t WHERE x IS ? || 'a'",
                "SELECT 1 FROM t WHERE COALESCE(x, 0) IS ?", "SELECT 1 FROM t WHERE ? IS x"):
        out = tr.translate(sql, marked=True)
        assert db_dialect._IS_A not in out and "IS NOT DISTINCT FROM" in out, (sql, out)
    assert "IS DISTINCT FROM %s" in tr.translate("SELECT 1 FROM t WHERE x IS NOT ?", marked=True)
    # A literal %s in a LIKE pattern is never taken for a parameter.
    out = tr.translate("SELECT 1 FROM t WHERE y LIKE '%s%' AND x IS ?", marked=True)
    text, params = db_dialect.bind_is(out, (3,))
    assert "(x = %s AND x IS NOT NULL)" in text and params == (3,)


# ── R2-05: a lock timeout is SQLite's "database is locked" ───────────────────

def test_a_lock_timeout_reads_as_locked():
    class LockNotAvailable(Exception):
        pass
    e = db_dialect._map_error(LockNotAvailable("canceling statement due to lock timeout"))
    assert isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e)
    import status_manager

    class _Conn:
        in_transaction = False

        def execute(self, sql, *a):
            if sql.startswith("BEGIN"):
                raise e
            return self

        def rollback(self):
            pass
    assert status_manager.db_write_probe(conn=_Conn())["state"] == "busy"


# ── R2-06: no object-store PUT inside a transaction ──────────────────────────

def test_a_blob_written_inside_a_transaction_stays_inline_for_the_offload(monkeypatch):
    import blob_store
    import offsite_backup
    monkeypatch.setenv("BLOB_STORE", "s3")
    monkeypatch.setattr(blob_store, "s3_config", lambda: {"endpoint": "https://x", "bucket": "b"})
    puts = []
    monkeypatch.setattr(offsite_backup, "put_bytes", lambda key, data, cfg: puts.append(key))
    assert blob_store.store("quality", "1/abc", b"zz", in_transaction=True) == b"zz" and puts == []
    assert blob_store.is_pointer(blob_store.store("quality", "1/abc", b"zz")) and puts == ["quality/1/abc"]


def test_a_schedule_save_never_calls_the_object_store_under_its_lock(db_path, monkeypatch):
    import blob_store
    import offsite_backup
    import schedule_versions
    monkeypatch.setenv("BLOB_STORE", "s3")
    monkeypatch.setattr(blob_store, "s3_config", lambda: {"endpoint": "https://x", "bucket": "b"})
    monkeypatch.setattr(offsite_backup, "put_bytes", lambda *a, **k: pytest.fail("a PUT under the write lock"))
    rid = models.create_restaurant(models.Restaurant(name="B", owner_email="b@x.com"), db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ref = schedule_versions.store_detail(conn, rid, {"score": 81, "people": ["a"] * 50})
        conn.commit()
        body = conn.execute("SELECT body FROM quality_blobs WHERE hash=?", (ref,)).fetchone()[0]
    finally:
        conn.close()
    assert not blob_store.is_pointer(body)               # inline until blob_offload moves it


# ── R2-12: one read scope per Home build ─────────────────────────────────────

def test_the_home_builds_run_in_one_read_scope(db_path, monkeypatch):
    # Off a request the restaurant row was read ~190 times per brief, each a
    # round trip on Postgres (measured on the seeded Simple EJ's: 721
    # statements -> 520).
    import good_news
    import monthly_review
    import morning_brief
    for fn in (morning_brief.build, monthly_review.build, good_news.all_good_news):
        assert hasattr(fn, "__wrapped__"), fn
    rid = models.create_restaurant(models.Restaurant(name="S", owner_email="s@x.com"), db_path=db_path)
    reads = []
    real = models.get_conn

    class _Counting:
        def __init__(self, conn):
            object.__setattr__(self, "_c", conn)

        def execute(self, sql, *a, **k):
            if str(sql).lstrip().startswith("SELECT * FROM restaurants WHERE id"):
                reads.append(sql)
            return self._c.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._c, name)

        def __setattr__(self, name, value):
            setattr(self._c, name, value)

    monkeypatch.setattr(models, "get_conn", lambda *a, **k: _Counting(real(db_path)))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    with contextlib.redirect_stdout(io.StringIO()):
        morning_brief.build.__wrapped__(rid, db_path=db_path)
    unscoped, reads[:] = len(reads), []
    with contextlib.redirect_stdout(io.StringIO()):
        morning_brief.build(rid, db_path=db_path)
    assert len(reads) <= 3 and len(reads) * 5 < unscoped, (len(reads), unscoped)


# ── R2-13: a collected connection gives its thread's count back ─────────────

class _FakeRaw:
    def cursor(self):
        return self

    def execute(self, *a, **k):
        self.description = None

    def fetchall(self):
        return []

    def rollback(self):
        pass

    def commit(self):
        pass

    def close(self):
        pass


def test_a_collected_connection_leaves_its_thread_on_the_base_slots(monkeypatch):
    monkeypatch.setattr(db_dialect, "_driver_connect", lambda url: _FakeRaw())
    url = f"postgresql://fake/{uuid.uuid4().hex}"
    monkeypatch.setenv("PG_POOL_MAX", "2")
    monkeypatch.setenv("PG_POOL_OVERFLOW", "1")
    out = {}

    def worker():
        c = db_dialect.connect(url)
        del c                                        # leaked, never closed
        gc.collect()
        out["held"] = db_dialect._held().get(url, 0)
        c2 = db_dialect.connect(url)
        out["slot"] = c2._slot
        c2.close()
    t = threading.Thread(target=worker)
    t.start()
    t.join(10)
    db_dialect.close_idle(url)
    assert out == {"held": 0, "slot": "base"}, out


# ══ against a real Postgres ═════════════════════════════════════════════════

def _url(name):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(PG)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
def scratch_db():
    import psycopg
    name = f"g2live_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    yield _url(name)
    db_dialect.close_idle(_url(name))
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _copied(tmp_path, url, seed=None):
    """A pg_copy'd database (production's partitioned shape) from a fresh
    SQLite boot; `seed(sqlite_conn, rid_a, rid_b)` adds rows first."""
    import pg_copy
    import psycopg
    db_path = str(tmp_path / "source.db")
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(db_path)
        models.ensure_columns(db_path)
    a = models.create_restaurant(models.Restaurant(name="A", owner_email="a@x.com"), db_path=db_path)
    b = models.create_restaurant(models.Restaurant(name="B", owner_email="b@x.com"), db_path=db_path)
    if seed:
        s = sqlite3.connect(db_path)
        seed(s, a, b)
        s.commit()
        s.close()
    models.checkpoint(db_path)
    dst = psycopg.connect(url, autocommit=False)
    try:
        assert pg_copy.copy_database(db_path, dst, today=date(2026, 10, 20))["verify"]["ok"]
    finally:
        dst.close()
    return a, b


@live
def test_is_param_uses_the_index_and_keeps_sqlites_answers(scratch_db, tmp_path):
    a, b = _copied(tmp_path, scratch_db)
    c = db_dialect.connect(scratch_db)
    try:
        c.execute_native("INSERT INTO ai_calls (call_id, restaurant_id, action, created_at) "
                         "SELECT 'c' || g, g % 500, 'act' || (g % 10), '2026-10-01' FROM generate_series(1, 20000) g")
        c.execute_native("INSERT INTO ai_calls (call_id, restaurant_id, action, created_at) "
                         "VALUES ('n1', NULL, 'act3', '2026-10-01')")
        c.execute_native("ANALYZE ai_calls")
        c.commit()
        q = "SELECT COUNT(*) FROM ai_calls WHERE restaurant_id IS ? AND action=?"
        assert c.execute(q, (3, "act3")).fetchone()[0] == 40
        assert c.execute(q, (None, "act3")).fetchone()[0] == 1
        assert c.execute(q, (None, "act4")).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM ai_calls WHERE NOT (restaurant_id IS ?) AND action='act3'",
                         (3,)).fetchone()[0] == 1961                 # the NULL row counted: never NULL
        text, params = db_dialect.bind_is(c._tr.translate(q, marked=True), (3, "act3"))
        c.execute_native("SET plan_cache_mode = force_generic_plan")
        c.execute_native(f"PREPARE g2 AS {text.replace('%s', '$1', 1).replace('%s', '$2', 1)}")
        plan = "\n".join(r[0] for r in c.execute_native("EXPLAIN EXECUTE g2(3, 'act3')").fetchall())
        assert "Index" in plan and "Seq Scan" not in plan, plan
    finally:
        c.rollback()
        c.close()


@live
def test_lastrowid_is_the_id_on_a_partitioned_table(scratch_db, tmp_path):
    a, b = _copied(tmp_path, scratch_db)
    c = db_dialect.connect(scratch_db)
    try:
        cur = c.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd) VALUES (?, 't', 'm', 0.01)", (a,))
        rid1 = cur.lastrowid
        cur = c.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd) VALUES (?, 't', 'm', 0.01)", (a,))
        assert isinstance(rid1, int) and cur.lastrowid == rid1 + 1
        sid = c.execute("INSERT INTO schedule_model_calls (restaurant_id, model) VALUES (?, 'm')", (a,)).lastrowid
        assert isinstance(sid, int)
        c.commit()
    finally:
        c.close()


@live
def test_a_re_dated_pos_record_is_stored_once_on_a_partitioned_table(scratch_db, tmp_path, monkeypatch):
    import pos_archive
    a, b = _copied(tmp_path, scratch_db)
    monkeypatch.setattr(pos_archive, "get_conn", lambda *x, **k: db_dialect.connect(scratch_db))

    def day(iso):
        return {"tickets": [{"ticket_id": "T1", "business_date": iso, "net_sales": 50.0}],
                "lines": [{"line_id": "L1", "ticket_id": "T1", "business_date": iso, "kind": "sale", "qty": 1,
                           "sales": 50.0}],
                "punches": [{"punch_id": "P1", "business_date": iso, "employee_id": "E1"}],
                "payments": [{"payment_id": "Y1", "ticket_id": "T1", "business_date": iso, "amount": 50.0}],
                "max_stamp": None}

    class _Fake:
        def __init__(self):
            self.days = {}

        def archive_rows(self, rid, d):
            return self.days[d.isoformat()]
    fake = _Fake()
    fake.days = {"2026-10-12": day("2026-10-12"), "2026-10-13": day("2026-10-13")}
    pos_archive._archive_day(a, date(2026, 10, 12), "2026-10-12", "rpower", fake, None)
    # The POS moves the ticket to the next business date (closed after midnight).
    pos_archive._archive_day(a, date(2026, 10, 13), "2026-10-13", "rpower", fake, None)
    c = db_dialect.connect(scratch_db)
    try:
        for table in ("pos_tickets", "pos_ticket_lines", "pos_punches"):
            rows = c.execute(f"SELECT business_date FROM {table} WHERE restaurant_id=?", (a,)).fetchall()
            assert [r[0] for r in rows] == ["2026-10-13"], (table, rows)
        assert c.execute("SELECT item_rollup FROM pos_archive_days WHERE restaurant_id=? AND business_date=?",
                         (a, "2026-10-12")).fetchone()[0] is None
    finally:
        c.close()


@live
def test_an_existing_column_is_answered_without_a_lock_and_a_new_one_waits_bounded(scratch_db, tmp_path, monkeypatch):
    import psycopg
    _copied(tmp_path, scratch_db)
    reader = psycopg.connect(scratch_db, autocommit=False)
    try:
        reader.execute("SELECT COUNT(*) FROM restaurants").fetchone()     # ACCESS SHARE held, transaction open
        c = db_dialect.connect(scratch_db)
        try:
            t0 = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
                c.execute("ALTER TABLE restaurants ADD COLUMN name TEXT")
            assert time.monotonic() - t0 < 2                               # no lock asked for
            c.commit()
            monkeypatch.setattr(db_dialect, "DDL_LOCK_TIMEOUT_MS", 200)
            monkeypatch.setattr(db_dialect, "DDL_LOCK_TRIES", 2)
            t0 = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                c.execute("ALTER TABLE restaurants ADD COLUMN g2_new TEXT")
            assert time.monotonic() - t0 < 5
            # The session's own lock_timeout is back.
            assert c.execute_native("SELECT current_setting('lock_timeout')").fetchone()[0] in ("30s", "30000ms")
            c.rollback()
            reader.rollback()
            c.execute("ALTER TABLE restaurants ADD COLUMN g2_new TEXT")    # free now: applied
            c.commit()
            assert c._has_column("restaurants", "g2_new")
        finally:
            c.close()
    finally:
        reader.close()


@live
def test_the_health_probe_never_queues_behind_the_platform_lock(scratch_db, tmp_path):
    import psycopg
    import status_manager
    _copied(tmp_path, scratch_db)
    holder = psycopg.connect(scratch_db, autocommit=False)
    c = db_dialect.connect(scratch_db)
    try:
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (db_dialect.WRITE_LOCK_KEY,))   # a long BEGIN IMMEDIATE
        t0 = time.monotonic()
        got = status_manager.db_write_probe(conn=c, timeout_ms=300)
        assert got["state"] == "busy" and time.monotonic() - t0 < 3, got
        holder.rollback()
        holder.execute("SELECT pg_advisory_xact_lock_shared(%s)", (db_dialect.WRITE_LOCK_KEY,))  # a tenant writer
        assert status_manager.db_write_probe(conn=c, timeout_ms=300)["state"] == "ok"
        holder.rollback()
    finally:
        c.close()
        holder.close()


@live
def test_a_live_table_is_made_partitioned_with_its_rows_keys_and_ids(scratch_db, tmp_path):
    import pg_partitions
    # The suite's boot shape: plain tables (production's growing tables today).
    db_path = str(tmp_path / "boot.db")
    models._PG_ROUTES[db_path] = scratch_db
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            models.init_db(db_path)
        rid = models.create_restaurant(models.Restaurant(name="A", owner_email="a@x.com"), db_path=db_path)
        c = db_dialect.connect(scratch_db)
        try:
            m7 = c.execute("INSERT INTO menu_items (restaurant_id, name) VALUES (?, 'Burger')", (rid,)).lastrowid
            m8 = c.execute("INSERT INTO menu_items (restaurant_id, name) VALUES (?, 'Fries')", (rid,)).lastrowid
            for i, d in enumerate(("2026-08-03", "2026-09-14", "2026-10-05")):
                c.execute("INSERT INTO pos_payments (restaurant_id, provider, payment_id, business_date, amount) "
                          "VALUES (?, 'rpower', ?, ?, ?)", (rid, f"Y{i}", d, 10.0 + i))
                c.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                          "VALUES (?, ?, ?, ?)", (rid, m7, d, i + 1))
            c.commit()
            before = c.execute("SELECT MAX(id) FROM menu_item_sales").fetchone()[0]
            for t in ("pos_payments", "menu_item_sales"):
                out = pg_partitions.convert_to_partitioned(c, t, today=date(2026, 10, 20), drop_old=True)
                assert out["rows"] == 3 and out["partitions"] >= 5, out
            kinds = dict(c.execute_native("SELECT relname, relkind::text FROM pg_class WHERE relname IN "
                                          "('pos_payments', 'menu_item_sales')").fetchall())
            assert kinds == {"pos_payments": "p", "menu_item_sales": "p"}, kinds
            # Rows, upserts by the old keys and ids carry on.
            assert c.execute("SELECT SUM(amount) FROM pos_payments WHERE restaurant_id=?", (rid,)).fetchone()[0] == 33.0
            c.execute("INSERT OR REPLACE INTO pos_payments (restaurant_id, provider, payment_id, business_date, amount) "
                      "VALUES (?, 'rpower', 'Y1', '2026-09-14', 99.0)", (rid,))
            new_id = c.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                               "VALUES (?, ?, '2026-10-06', 1)", (rid, m8)).lastrowid
            c.commit()
            assert new_id == before + 1
            assert c.execute("SELECT COUNT(*), SUM(amount) FROM pos_payments").fetchone()[:] == (3, 121.0)
            with pytest.raises(sqlite3.IntegrityError):
                c.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                          "VALUES (?, ?, '2026-08-03', 1)", (rid, m7))
            c.rollback()
            assert pg_partitions.convert_to_partitioned(c, "pos_payments").get("already") is True
        finally:
            c.close()
    finally:
        models._PG_ROUTES.pop(db_path, None)
