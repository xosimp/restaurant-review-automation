"""Scheduler audit 10/10/26, workstream H2 — Postgres readiness (RM-13,
RM-14, SCALE-20, SCALE-21, ARCH-34): the dialect layer behind
models.get_conn, the copy and its verification, the normalized schedule
model's shadow, blobs in object storage, the multipart off-site upload.

No Postgres server is installed where this suite runs: the translation is
tested as text, PgConnection against a recording driver, the copy and the
shadow end to end against SQLite targets. The live-Postgres check is the
runbook's (docs/ops/POSTGRES_CUTOVER.md)."""
import json
import os
import sqlite3
import sys
from datetime import date

import pytest

import db_dialect
import models
from models import Restaurant, create_restaurant


def _tr(pks=None, cols=None):
    pks = pks or {}
    cols = cols or {}
    return db_dialect.Translator(lambda t: pks.get(t), lambda t: cols.get(t))


# ── translation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sqlite, pg", [
    ("SELECT 1 FROM t WHERE a=? AND b=?", "SELECT 1 FROM t WHERE a=%s AND b=%s"),
    ("SELECT * FROM t WHERE created_at < datetime('now', ?)",
     "SELECT * FROM t WHERE created_at < to_char((timezone('UTC', now()) + CAST(%s AS interval)), "
     "'YYYY-MM-DD HH24:MI:SS')"),
    ("SELECT date('now', '-7 days')", "SELECT to_char((timezone('UTC', now()) + interval '-7 days'), 'YYYY-MM-DD')"),
    # A text stamp is read through cavnar_ts: NULL for '' or no date, as
    # SQLite's datetime('') is (blind re-audit 10/11/26, SCALE-9).
    ("SELECT datetime(x)", "SELECT to_char(cavnar_ts(CAST(x AS text)), 'YYYY-MM-DD HH24:MI:SS')"),
    # SQLite's scalar MAX(a, b) is NULL when an argument is; GREATEST is not.
    ("UPDATE jobs SET n=MAX(0, n-1)", "UPDATE jobs SET n=(SELECT CASE WHEN COUNT(_v) < COUNT(*) THEN NULL "
     "ELSE MAX(_v) END FROM (VALUES (0), (n-1)) AS _m(_v))"),
    ("SELECT MAX(n) FROM t", "SELECT MAX(n) FROM t"),
    ("SELECT IFNULL(a, 0), INSTR(a, 'x') FROM t", "SELECT COALESCE(a, 0), STRPOS(a, 'x') FROM t"),
    ("SELECT name FROM t WHERE name LIKE '%a%'", "SELECT name FROM t WHERE name ILIKE '%%a%%' ESCAPE ''"),
    ("SELECT * FROM t LIMIT -1", "SELECT * FROM t LIMIT ALL"),
    ("SELECT CURRENT_TIMESTAMP", "SELECT to_char(timezone('UTC', now()), 'YYYY-MM-DD HH24:MI:SS')"),
])
def test_statements_translate(sqlite, pg):
    assert _tr().translate(sqlite) == pg


def test_epoch_julianday_and_week_formats_keep_sqlites_answers():
    tr = _tr()
    out = tr.translate("SELECT CAST(strftime('%s', deadline_at) AS INTEGER) FROM t")
    assert "floor(extract(epoch from cavnar_ts(CAST(deadline_at AS text))))" in out
    assert "2440587.5" in tr.translate("SELECT julianday('now') - julianday(created_at) FROM t")
    week = tr.translate("SELECT strftime('%Y-W%W', d) FROM t")
    assert "'YYYY-\"W\"'" in week and "extract(doy from" in week and "lpad(" in week
    assert "extract(dow from" in tr.translate("SELECT strftime('%w', d) FROM t")
    with pytest.raises(db_dialect.TranslationError):
        tr.translate("SELECT strftime('%Q', d) FROM t")


def test_upserts_and_the_partition_column():
    tr = _tr(pks={"x": ["id"]}, cols={"x": ["id", "a", "b"]})
    assert tr.translate("INSERT OR IGNORE INTO x (a) VALUES (?)").strip().endswith("ON CONFLICT DO NOTHING")
    rep = tr.translate("INSERT OR REPLACE INTO x (id, a, b) VALUES (?,?,?)")
    assert "ON CONFLICT (id) DO UPDATE SET a = EXCLUDED.a, b = EXCLUDED.b" in rep
    assert "INSERT OR" not in rep
    with pytest.raises(db_dialect.TranslationError):
        _tr().translate("INSERT OR REPLACE INTO unknown (a) VALUES (?)")
    up = tr.translate("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date) VALUES (?,?,?,?) "
                      "ON CONFLICT(restaurant_id, provider, ticket_id) DO UPDATE SET business_date=excluded.business_date")
    assert "ON CONFLICT(restaurant_id, provider, ticket_id, business_date)" in up


def test_rowid_glob_and_json():
    tr = _tr(pks={"jobs": ["id"]})
    assert tr.translate("DELETE FROM jobs WHERE rowid IN (SELECT rowid FROM jobs LIMIT 5)") == \
        "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs LIMIT 5)"
    assert "ctid" in tr.translate("DELETE FROM pos_tickets WHERE rowid IN (SELECT rowid FROM pos_tickets LIMIT 5)")
    g = tr.translate("SELECT 1 FROM t WHERE d GLOB '[0-9][0-9]-*' AND e NOT GLOB '?x' AND f GLOB ?")
    assert "d ~ '^[0-9][0-9]\\-.*$'" in g and "e !~ '^.x$'" in g and "f LIKE replace(replace(%s" in g
    # Selected as a column it keeps its JSON type (the driver hands back the
    # number / string it holds); compared, its text.
    j = tr.translate("SELECT json_extract(meta, '$.a.b') FROM t")
    assert "(CAST(meta AS jsonb) #> '{a,b}')" in j
    j = tr.translate("SELECT 1 FROM t WHERE json_extract(meta, '$.a.b') = ?")
    assert "(CAST(meta AS jsonb) #>> '{a,b}') = %s" in j


def test_ddl_types_and_defaults():
    ddl = _tr().translate("CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY AUTOINCREMENT, a REAL, b BLOB, "
                          "c TEXT NOT NULL DEFAULT (datetime('now')), n INTEGER DEFAULT 0, k TEXT COLLATE NOCASE)")
    assert "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" in ddl
    assert "a DOUBLE PRECISION" in ddl and "b BYTEA" in ddl and "n BIGINT" in ddl and "NOCASE" not in ddl
    assert "DEFAULT (to_char(timezone('UTC', now())" in ddl


def test_the_whole_apps_sql_is_audited_and_the_gaps_are_named():
    """scripts/pg_sql_audit.py reads every .execute() in the app: what it
    cannot carry is a short, named list (the runbook's port list)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import pg_sql_audit
    rep = pg_sql_audit.audit()
    assert rep["statements"] > 3000
    assert rep["double_quoted"] == [] and rep["reserved_columns"] == []
    # Every statement translates; the SQLite triggers are no-ops on
    # Postgres because the copy installs their plpgsql form; two statements
    # build their SQL at run time (a table name, an interval) and translate
    # then.
    assert rep["untranslatable"] == [] and rep["sqlite_only"] == []
    assert len(rep["ported"]) == 6 and len(rep["dynamic"]) <= 3


# ── the connection, against a recording driver ──────────────────────────────

class _RawCursor:
    def __init__(self, raw):
        self.raw, self.description, self._rows, self.rowcount = raw, None, [], 0

    def execute(self, sql, params=None):
        self.raw.log.append((sql, params))
        if "information_schema.columns" in sql and "table_name" in sql and params:
            t = params[0]
            self._rows = [(c, "bigint" if c == "id" else "text", "YES", None, i + 1)
                          for i, c in enumerate(self.raw.tables.get(t, []))]
            self.description = [("column_name",), ("data_type",), ("is_nullable",), ("column_default",),
                                 ("ordinal_position",)]
        elif "indisunique" in sql:
            t = params[0]
            self._rows = [(f"{t}_pkey", ["id"], True)] if "id" in self.raw.tables.get(t, []) else []
            self.description = [("indexrelid",), ("cols",), ("pk",)]
        elif "pg_index" in sql:
            t = params[0]
            self._rows = [("id",)] if "id" in self.raw.tables.get(t, []) else []
            self.description = [("attname",)]
        elif "RETURNING id" in sql:
            self._rows, self.description, self.rowcount = [(41,)], [("id",)], 1
        elif sql.lstrip().upper().startswith("SELECT"):
            self._rows, self.description = [(7, "seven")], [("n",), ("Name",)]
        else:
            self._rows, self.description, self.rowcount = [], None, 3
        if self.raw.fail_on and self.raw.fail_on in sql:
            raise self.raw.error

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Raw:
    def __init__(self):
        self.log, self.commits, self.rollbacks = [], 0, 0
        self.tables = {"jobs": ["id", "kind"], "pairs": ["a", "b"]}
        self.fail_on, self.error = None, None

    def cursor(self):
        return _RawCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture
def pg():
    db_dialect.Catalog.forget()
    raw = _Raw()
    yield db_dialect.PgConnection(raw), raw
    db_dialect.Catalog.forget()


def test_a_pg_connection_reads_like_sqlite(pg):
    conn, raw = pg
    row = conn.execute("SELECT n, name FROM jobs WHERE id=?", (1,)).fetchone()
    assert row[0] == 7 and row["name"] == "seven" and row["NAME"] == "seven" and dict(row) == {"n": 7, "Name": "seven"}
    assert raw.log[-1] == ("SELECT n, name FROM jobs WHERE id=%s", (1,))
    cur = conn.execute("INSERT INTO jobs (kind) VALUES (?)", ("x",))
    # Inside the transaction the SELECT opened: under its own savepoint
    # (SQLite's statement atomicity, SCALE-5/8).
    sent = [s for s, _p in raw.log if not s.startswith(("SAVEPOINT", "RELEASE", "SELECT column_name", "SELECT a.attname",
                                                          "SELECT i.indexrelid"))]
    assert cur.lastrowid == 41 and sent[-1].endswith("RETURNING id")
    assert [s for s, _p in raw.log[-3:]] == ["SAVEPOINT cavnar_stmt", sent[-1], "RELEASE SAVEPOINT cavnar_stmt"]
    assert conn.execute("INSERT INTO pairs (a, b) VALUES (?, ?)", (1, 2)).lastrowid is None
    assert conn.execute("UPDATE jobs SET kind='y'").rowcount == 3


def test_begin_immediate_takes_the_one_writer_lock_and_pragmas_answer(pg):
    conn, raw = pg
    conn.execute("BEGIN IMMEDIATE")
    assert ("SELECT pg_advisory_xact_lock(%s)", (db_dialect.WRITE_LOCK_KEY,)) in raw.log
    assert conn.in_transaction
    conn.execute("COMMIT")
    assert raw.commits == 1 and not conn.in_transaction
    n = len(raw.log)
    assert conn.execute("PRAGMA foreign_keys=ON").fetchall() == []
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    info = conn.execute("PRAGMA table_info(jobs)").fetchall()
    assert [r[1] for r in info] == ["id", "kind"] and info[0][5] == 1
    assert conn.execute("DROP TRIGGER IF EXISTS trg_x").fetchall() == []
    assert conn.execute("PRAGMA database_list").fetchone()[1] == "main"
    assert all("PRAGMA" not in s and "TRIGGER" not in s for s, _p in raw.log[n:])


def test_driver_errors_arrive_as_sqlite3_errors(pg):
    conn, raw = pg

    class UniqueViolation(Exception):
        pass
    raw.fail_on, raw.error = "INSERT", UniqueViolation("duplicate key")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO pairs (a, b) VALUES (?, ?)", (1, 2))
    raw.fail_on, raw.error = "SELECT", RuntimeError('relation "nope" does not exist')
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM nope")
    with conn:
        pass
    assert raw.commits >= 1


def test_get_conn_is_postgres_only_when_database_url_says_so(monkeypatch, tmp_path):
    made = []
    monkeypatch.setattr(db_dialect, "connect", lambda url=None: made.append(url) or "PG")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    c = models.get_conn(str(tmp_path / "x.db"))
    c.close()
    assert models._db_backend() == "sqlite" and made == []
    monkeypatch.setenv("ALLOW_LOCAL_PG", "1")    # off Railway DATABASE_URL is ignored otherwise
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    assert models._db_backend() == "postgres"
    assert models.get_conn() == "PG"                       # the platform's database
    c = models.get_conn(str(tmp_path / "y.db"))            # an explicit file stays SQLite
    assert isinstance(c.execute("SELECT 1").fetchone()[0], int)
    c.close()
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    assert models._db_backend() == "sqlite"


# ── the copy, verified (SQLite -> SQLite stands in for the server) ──────────

@pytest.fixture
def source(db_path):
    rid = create_restaurant(Restaurant(name="Copy Grill", owner_email="c@x.com"), db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, source, "
                 "scheduled_hours) VALUES (?, '2026-09-01', 'Ana', 'ana', 'pos', '')", (rid,))
    conn.execute("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, source, "
                 "scheduled_hours) VALUES (?, '2026-10-02', 'Bo', 'bo', 'pos', 6.5)", (rid,))
    conn.commit()
    conn.close()
    return db_path, rid


@pytest.mark.sqlite_only
def test_the_schema_plan_partitions_the_facts_and_defers_the_foreign_keys(source):
    src = sqlite3.connect(source[0])
    try:
        plan = __import__("pg_copy").schema_plan(src, today=date(2026, 10, 10))
    finally:
        src.close()
    text = "\n".join(plan["before"])
    assert "CREATE TABLE IF NOT EXISTS shift_facts" in text and "PARTITION BY RANGE (business_date)" in text
    assert "CREATE INDEX IF NOT EXISTS idx_attendance_date" in text
    assert "PRIMARY KEY (id, business_date)" in text and "nextval('shift_facts_id_seq')" in text
    assert "shift_facts_p202609 PARTITION OF shift_facts FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')" in text
    assert "shift_facts_p202701" in text and "shift_facts_default PARTITION OF shift_facts DEFAULT" in text
    assert "CREATE OR REPLACE VIEW sqlite_master" in text
    assert "EXCLUDE USING gist (revision_id WITH =, person_id WITH =, span WITH &&)" in text
    assert "CREATE TRIGGER trg_outcomes_changed_upd BEFORE UPDATE ON recommendation_outcomes" in text
    assert " REFERENCES " not in "\n".join(s for s in plan["before"] if s.startswith("CREATE TABLE IF NOT EXISTS people ("))
    assert "ALTER TABLE people ADD FOREIGN KEY (merged_into) REFERENCES people(id)" in plan["after"]


@pytest.mark.sqlite_only
def test_a_copy_moves_every_row_counts_what_it_could_not_store_and_verifies(source, tmp_path):
    import pg_copy
    db, rid = source
    dst_path = str(tmp_path / "copy.db")
    models.init_db(dst_path)
    models.ensure_columns(dst_path)
    # The source is a test database: the whole boot schema, init_auth and
    # the rest included (conftest._boot_schema) — the copy's target is too.
    from conftest import _boot_schema
    _boot_schema(dst_path)
    src = sqlite3.connect(db)
    dst = sqlite3.connect(dst_path)
    try:
        dst.execute("PRAGMA foreign_keys=OFF")
        for name, _sql in pg_copy.sqlite_tables(dst):
            dst.execute(f'DELETE FROM "{name}"')
        dst.commit()
        report = {}
        for name, _sql in pg_copy.sqlite_tables(src):
            report[name] = pg_copy.copy_table(src, dst, name, batch=1, placeholder="?")
        assert report["shift_facts"]["rows"] == 2
        assert report["shift_facts"]["coerced"] == {"scheduled_hours": 1}      # '' in a REAL column
        v = pg_copy.verify(src, dst)
        assert v["ok"], [t for t, r in v["tables"].items() if not r["ok"]]
        dst.execute("DELETE FROM shift_facts WHERE employee_key='bo'")
        dst.commit()
        v = pg_copy.verify(src, dst, tables={"shift_facts"})
        assert not v["ok"] and v["tables"]["shift_facts"]["source"]["rows"] == 2
    finally:
        src.close()
        dst.close()


@pytest.mark.sqlite_only
def test_the_copy_script_plans_without_a_database(source, capsys):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import importlib
    script = importlib.import_module("pg_copy") if False else None
    import runpy
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_pg_copy.py")
    mod = runpy.run_path(path, run_name="pg_copy_script")
    assert mod["main"](["--sqlite", source[0], "--plan"]) == 0
    out = capsys.readouterr().out
    assert "PARTITION BY RANGE (business_date)" in out and out.count(";\n") > 300


# ── the shadow of the schedule model ────────────────────────────────────────

_SCHED_SQLITE = """
CREATE TABLE sched.schedule_weeks (id INTEGER PRIMARY KEY, restaurant_id INTEGER, week_start TEXT, week_end TEXT,
    working_revision_id INTEGER, published_revision_id INTEGER, superseded_by INTEGER, updated_at TEXT);
CREATE TABLE sched.schedule_revisions (id INTEGER PRIMARY KEY, week_id INTEGER, restaurant_id INTEGER,
    version INTEGER, parent_version INTEGER, kind TEXT, author TEXT, authority TEXT, created_at TEXT,
    UNIQUE (week_id, version));
CREATE TABLE sched.shifts (revision_id INTEGER, shift_id TEXT, week_id INTEGER, restaurant_id INTEGER,
    person_id INTEGER, employee TEXT, role TEXT, business_date TEXT, start_ts TEXT, end_ts TEXT,
    scheduled_hours REAL, notes TEXT, locked INTEGER, published INTEGER, PRIMARY KEY (revision_id, shift_id));
CREATE TABLE sched.schedule_events (week_id INTEGER, seq INTEGER, restaurant_id INTEGER, revision_id INTEGER,
    actor TEXT, authority TEXT, kind TEXT, payload TEXT, created_at TEXT, PRIMARY KEY (week_id, seq));
"""


@pytest.fixture
def shadow(db_path, tmp_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setenv("PG_SHADOW_WRITE", "1")
    target = sqlite3.connect(":memory:")
    target.execute(f"ATTACH '{tmp_path / 'sched.db'}' AS sched")
    target.executescript(_SCHED_SQLITE)
    rid = create_restaurant(Restaurant(name="Shadow Grill", owner_email="s@x.com"), db_path=db_path)
    conn = real(db_path)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) "
                       "VALUES (?, '2026-10-19', '2026-10-25', '')", (rid,)).lastrowid
    conn.commit()
    conn.close()
    yield target, rid, hid
    target.close()


def _csv(rows):
    import schedule_rows
    return schedule_rows.to_csv(rows)


def _row(sid, who, start, end, date_="2026-10-19", pid=None):
    return {"date": date_, "day": "Mon", "employee": who, "role": "Server", "shift_start": start, "shift_end": end,
            "scheduled_hours": "6", "notes": "", "shift_id": sid, "person_id": str(pid or ""), "locked": ""}


def test_each_revision_is_noted_and_written_to_the_shadow_once(shadow):
    import pg_shadow
    import schedule_versions as sv
    target, rid, hid = shadow
    a, b = "a" * 32, "b" * 32
    sv.append(rid, hid, "generated", _csv([_row(a, "Ana", "10:00am", "4:00pm", pid=1),
                                           _row(b, "Bo", "5:00pm", "1:00am", pid=2)]))
    sv.append(rid, hid, "edited", _csv([_row(a, "Ana", "11:00am", "4:00pm", pid=1)]))
    sv.append(rid, hid, "published", _csv([_row(a, "Ana", "11:00am", "4:00pm", pid=1)]))
    out = pg_shadow.drain(target=target)
    assert out["ok"] == 3 and out["failed"] == 0
    assert pg_shadow.drain(target=target)["attempted"] == 0               # sent once
    week = target.execute("SELECT working_revision_id, published_revision_id FROM sched.schedule_weeks").fetchone()
    revs = target.execute("SELECT id, version, kind FROM sched.schedule_revisions ORDER BY version").fetchall()
    assert [r[2] for r in revs] == ["generated", "edited", "published"]
    assert week == (revs[2][0], revs[2][0])
    # The overnight close lands on the next day, in real (UTC) time.
    bo = target.execute("SELECT start_ts, end_ts FROM sched.shifts WHERE shift_id=?", (b,)).fetchone()
    assert bo[0].startswith("2026-10-19T") and bo[1].startswith("2026-10-20T")
    kinds = [r[0] for r in target.execute("SELECT kind FROM sched.schedule_events ORDER BY seq")]
    assert kinds == ["generate", "add", "add", "change", "remove", "publish"]
    assert target.execute("SELECT published FROM sched.shifts WHERE revision_id=?", (revs[2][0],)).fetchone()[0] == 1
    cmp = pg_shadow.compare(target)
    assert cmp["ok"] and cmp["revisions"] == (3, 3) and cmp["outbox_open"] == 0


def test_the_shadow_is_off_by_default_and_a_refused_row_keeps_its_error(shadow, monkeypatch):
    import pg_shadow
    import schedule_versions as sv
    target, rid, hid = shadow
    monkeypatch.delenv("PG_SHADOW_WRITE")
    sv.append(rid, hid, "generated", _csv([_row("c" * 32, "Cy", "10:00am", "2:00pm")]))
    conn = models.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM pg_shadow_outbox").fetchone()[0] == 0
    finally:
        conn.close()
    assert pg_shadow.run_shadow_pass()["skipped"] == 1
    monkeypatch.setenv("PG_SHADOW_WRITE", "1")
    assert pg_shadow.backfill() == 1
    target.execute("DROP TABLE sched.shifts")
    out = pg_shadow.drain(target=target)
    assert out["failed"] == 1
    conn = models.get_conn()
    try:
        row = conn.execute("SELECT attempts, error, next_at FROM pg_shadow_outbox").fetchone()
    finally:
        conn.close()
    assert row["attempts"] == 1 and "shifts" in row["error"] and row["next_at"]


# ── blobs out of the database ───────────────────────────────────────────────

def test_blobs_move_to_the_store_and_read_back_through_their_pointer(db_path, tmp_path, monkeypatch):
    import blob_store
    import schedule_output
    import schedule_versions as sv
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setenv("BLOB_STORE", "local")
    monkeypatch.setenv("BLOB_STORE_DIR", str(tmp_path / "blobs"))
    req = {"model": "m", "messages": [{"role": "user", "content": "WEEK"}]}
    rec = schedule_output.record_call(1, req, inputs={"a": 1}, answer="the answer", generation_id="g", job_id="j",
                                      outcome="ok")
    conn = real(db_path)
    try:
        row = conn.execute("SELECT request_z, answer_z FROM schedule_model_calls WHERE id=?", (rec,)).fetchone()
        assert blob_store.is_pointer(row["request_z"]) and blob_store.is_pointer(row["answer_z"])
        call = schedule_output.load_calls(generation_id="g")[0]
        assert call["answer"] == "the answer" and call["request"]["model"] == "m" and call["inputs"]["a"] == 1
        assert schedule_output.stored_answers("j")[0]["answer"] == "the answer"
        rid = create_restaurant(Restaurant(name="Blob Grill", owner_email="b@x.com"), db_path=db_path)
        head, ref = sv.store_quality(conn, rid, {"score": 81, "detail": "x" * 5000})
        conn.commit()
        body = conn.execute("SELECT body FROM quality_blobs WHERE hash=?", (ref,)).fetchone()[0]
        assert blob_store.is_pointer(body)
        assert sv.version_quality(conn, head, ref)["detail"] == "x" * 5000
    finally:
        conn.close()
    # BLOB_STORE=db (the default) keeps everything inline; offload moves old
    # inline rows once a store is named.
    monkeypatch.setenv("BLOB_STORE", "db")
    rec2 = schedule_output.record_call(1, req, answer="inline", generation_id="g2", outcome="ok")
    conn = real(db_path)
    try:
        assert not blob_store.is_pointer(conn.execute("SELECT answer_z FROM schedule_model_calls WHERE id=?",
                                                      (rec2,)).fetchone()[0])
        conn.execute("UPDATE schedule_model_calls SET created_at=datetime('now', '-3 days')")
        conn.commit()
    finally:
        conn.close()
    assert blob_store.offload()["skipped"] == 1
    monkeypatch.setenv("BLOB_STORE", "local")
    out = blob_store.offload()
    assert out["ok"] >= 1 and out["failed"] == 0
    assert schedule_output.load_calls(generation_id="g2")[0]["answer"] == "inline"


def test_the_blob_store_never_uses_the_backup_bucket(monkeypatch):
    import blob_store
    for k, v in {"BLOB_S3_ENDPOINT": "https://s", "BLOB_S3_BUCKET": "b", "BLOB_S3_ACCESS_KEY_ID": "k",
                 "BLOB_S3_SECRET_ACCESS_KEY": "s", "BACKUP_S3_BUCKET": "b"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("BLOB_STORE", "s3")
    assert blob_store.s3_config() is None and blob_store.active() == "db"
    monkeypatch.setenv("BLOB_S3_BUCKET", "blobs")
    assert blob_store.s3_config()["bucket"] == "blobs" and blob_store.active() == "s3"


# ── the off-site copy in parts (SCALE-20) ───────────────────────────────────

def test_a_big_copy_goes_up_in_parts_and_a_failed_part_aborts(monkeypatch, tmp_path):
    import offsite_backup
    import requests
    cfg = {"endpoint": "https://store.example", "bucket": "b", "access_key": "k", "secret_key": "s",
           "region": "auto", "prefix": "p/"}
    path = tmp_path / "big.enc"
    path.write_bytes(os.urandom(2500))
    calls = []

    class R:
        def __init__(self, status=200, content=b"", headers=None):
            self.status_code, self.content, self.headers, self.text = status, content, headers or {}, ""

    def post(url, data=None, headers=None, timeout=None):
        calls.append(("POST", url))
        if "uploads=" in url:
            return R(content=b"<InitiateMultipartUploadResult><UploadId>U1</UploadId></InitiateMultipartUploadResult>")
        return R(content=b"<CompleteMultipartUploadResult/>")

    def put(url, data=None, headers=None, timeout=None):
        calls.append(("PUT", url, len(data)))
        assert timeout and headers["x-amz-content-sha256"]
        return R(headers={"ETag": f'"e{len(calls)}"'})

    def delete(url, headers=None, timeout=None):
        calls.append(("DELETE", url))
        return R(204)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(requests, "put", put)
    monkeypatch.setattr(requests, "delete", delete)
    monkeypatch.setattr(offsite_backup, "MULTIPART_THRESHOLD", 1000)
    monkeypatch.setattr(offsite_backup, "MULTIPART_PART_BYTES", 1000)
    out = offsite_backup.upload_file(str(path), "big.enc", cfg=cfg)
    assert out["parts"] == 3 and out["bytes"] == 2500 and out["target"] == "s3://b/p/big.enc"
    assert [c[2] for c in calls if c[0] == "PUT"] == [1000, 1000, 500]
    assert "partNumber=1&uploadId=U1" in [c for c in calls if c[0] == "PUT"][0][1]
    assert calls[-1][0] == "POST" and "uploadId=U1" in calls[-1][1]
    calls.clear()
    monkeypatch.setattr(requests, "put", lambda *a, **k: R(500))
    with pytest.raises(offsite_backup.OffsiteError):
        offsite_backup.upload_file(str(path), "big.enc", cfg=cfg)
    assert calls[-1][0] == "DELETE"


def test_the_nightly_snapshot_on_postgres_is_an_export(monkeypatch, tmp_path):
    import pg_copy
    import scheduler
    made = []

    def export(path, url=None, pg=None):
        sqlite3.connect(path).execute("CREATE TABLE t (x)").connection.close()
        made.append(path)
        return {"tables": {}}
    monkeypatch.setattr(pg_copy, "export_to_sqlite", export)
    monkeypatch.setenv("ALLOW_LOCAL_PG", "1")    # off Railway DATABASE_URL is ignored otherwise
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    dest = str(tmp_path / "snap.db")
    scheduler._write_consistent_snapshot(dest)
    assert made and os.path.exists(dest)


def test_the_shadow_writes_through_postgres_with_typed_casts(pg):
    import pg_shadow
    conn, raw = pg
    proj = {"week": {"id": 1, "restaurant_id": 2, "week_start": "2026-10-19", "week_end": "2026-10-25",
                     "superseded_by": None},
            "revision": {"id": 9, "week_id": 1, "restaurant_id": 2, "version": 1, "parent_version": None,
                         "kind": "published", "author": "o", "authority": "principal",
                         "created_at": "2026-10-10T10:00:00+00:00"},
            "shifts": [{"shift_id": "a" * 32, "person_id": 3, "employee": "Ana", "role": "Server",
                        "business_date": "2026-10-19", "start_ts": "2026-10-19T15:00:00+00:00",
                        "end_ts": "2026-10-19T21:00:00+00:00", "scheduled_hours": 6.0, "notes": None,
                        "locked": False, "published": True}],
            "events": [{"kind": "publish", "payload": {"rows": 1}}], "published": True}
    pg_shadow.write(conn, proj)
    sql = "\n".join(s for s, _p in raw.log)
    assert "CAST(%s AS timestamptz)" in sql and "CAST(%s AS jsonb)" in sql and "CAST(%s AS date)" in sql
    assert "INSERT INTO sched.shifts" in sql and "ON CONFLICT (revision_id, shift_id) DO NOTHING" in sql
    assert "published_revision_id = %s" in sql
