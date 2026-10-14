"""Blind re-audit 10/11/26, workstream Q2 — the Postgres path made real
(SCALE-5 … SCALE-12).

The first half needs no server: the translator's SQLite semantics, the
registries held to the code, the bounded pool and the per-resource locks
against fakes. The second half runs against a real Postgres when
CAVNAR_TEST_PG_URL names a scratch server (CI's `postgres` job; locally a
throwaway cluster) — the boot twice, the copy then the boot, every static
statement PREPAREd, and the semantics a translation must keep."""
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import date

import pytest

import db_dialect
import models

ROOT = os.path.join(os.path.dirname(__file__), "..")
PG = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()


def _tr(pks=None, cols=None, notnull=None, uniques=None, types=None):
    pks, cols, notnull, uniques, types = pks or {}, cols or {}, notnull or {}, uniques or {}, types or {}
    return db_dialect.Translator(lambda t: pks.get(t), lambda t: cols.get(t), lambda t: notnull.get(t),
                                 lambda t: uniques.get(t), lambda t: types.get(t))


# ── the translator keeps SQLite's meaning ───────────────────────────────────

def test_boolean_aggregates_count_ones_and_keep_null():                  # SCALE-6
    out = _tr().translate("SELECT SUM(status='open') AS o, AVG(score >= 4) AS a, TOTAL(x IS NULL), "
                          "SUM(CASE WHEN a=1 THEN 1 ELSE 0 END) AS c, SUM(n) FROM issues")
    assert "SUM(CAST((status='open') AS INTEGER))" in out and "AVG(CAST((score >= 4) AS INTEGER))" in out
    assert "COALESCE(SUM(CAST((x IS NULL) AS INTEGER)), 0.0)" in out
    assert "SUM(CASE WHEN a=1 THEN 1 ELSE 0 END)" in out and "SUM(n)" in out


def test_collate_nocase_is_lower_on_both_sides():                       # SCALE-7
    tr = _tr()
    assert "ORDER BY lower(COALESCE(m.employee_name, u.username)) NULLS FIRST" in \
        tr.translate("SELECT 1 FROM m ORDER BY COALESCE(m.employee_name, u.username) COLLATE NOCASE")
    assert "ORDER BY lower(r.name) NULLS FIRST" in tr.translate("SELECT 1 FROM r ORDER BY r.name COLLATE NOCASE")
    assert "lower(email) = lower(%s)" in tr.translate("SELECT 1 FROM u WHERE email = ? COLLATE NOCASE")
    assert "NOCASE" not in _tr().translate("CREATE TABLE t (a TEXT COLLATE NOCASE)")


def test_null_order_is_sqlites_and_a_not_null_column_keeps_its_index():   # SCALE-9
    tr = _tr(cols={"t": ["id", "published_at", "created_at"]}, notnull={"t": {"id", "created_at"}})
    out = tr.translate("SELECT id FROM t ORDER BY published_at DESC, created_at DESC, id LIMIT 1")
    assert "published_at DESC NULLS LAST" in out and "created_at DESC," in out and out.endswith("id LIMIT 1")
    assert "x NULLS FIRST" in tr.translate("SELECT x FROM t ORDER BY x")
    # Behind an outer join a NOT NULL column can still be NULL.
    assert "created_at NULLS FIRST" in tr.translate("SELECT 1 FROM t LEFT JOIN u ON u.a=t.id ORDER BY created_at")
    # The index is built in the order the query asks for, so it serves it.
    assert tr.translate("CREATE INDEX IF NOT EXISTS ix ON t(published_at DESC, created_at)") == \
        "CREATE INDEX IF NOT EXISTS ix ON t(published_at DESC NULLS LAST, created_at)"
    # Inside a window, before LIMIT, and never twice.
    w = tr.translate("SELECT ROW_NUMBER() OVER (PARTITION BY a ORDER BY published_at) FROM t")
    assert "ORDER BY published_at NULLS FIRST)" in w
    assert tr.translate("SELECT 1 FROM t ORDER BY x DESC NULLS FIRST").count("NULLS") == 1


def test_scalar_max_min_are_null_when_an_argument_is():                   # SCALE-9
    out = _tr().translate("UPDATE jobs SET n=MAX(0, n-?) WHERE id=?")
    assert out == ("UPDATE jobs SET n=(SELECT CASE WHEN COUNT(_v) < COUNT(*) THEN NULL ELSE MAX(_v) END "
                   "FROM (VALUES (0), (n-%s)) AS _m(_v)) WHERE id=%s")
    assert out.count("%s") == 2                                            # each parameter bound once
    assert "GREATEST(" in _tr().translate("CREATE TABLE t (a REAL DEFAULT (MAX(1, 2)))")


def test_like_has_no_escape_character_as_in_sqlite():                    # SCALE-9
    out = _tr().translate("SELECT 1 FROM t WHERE name LIKE '%a\\b%' AND x = ? OR y LIKE ? || '%'")
    assert "name ILIKE '%%a\\b%%' ESCAPE '' AND" in out and "y ILIKE %s || '%%' ESCAPE ''" in out
    assert out.count("ESCAPE") == 2
    assert "ESCAPE '!'" in _tr().translate("SELECT 1 FROM t WHERE a LIKE ? ESCAPE '!'")


def test_json_extract_keeps_its_type():                                   # SCALE-9
    tr = _tr()
    sel = tr.translate("SELECT json_extract(c, '$.ps') ps, json_extract(c,'$.a') AS a FROM r")
    assert "(CAST(c AS jsonb) #> '{ps}') ps" in sel and "(CAST(c AS jsonb) #> '{a}') AS a" in sel
    num = tr.translate("SELECT 1 FROM r WHERE json_extract(c, '$.n') > 5")
    assert "jsonb_typeof(CAST(c AS jsonb) #> '{n}') = 'number'" in num and "AS double precision) > 5" in num
    assert "(CAST(c AS jsonb) #>> '{tab}')='labor'" in tr.translate(
        "SELECT 1 FROM r WHERE json_extract(c, '$.tab')='labor'")
    assert "CAST((CAST(d AS jsonb) #>> '{id}') AS INTEGER)" in tr.translate(
        "SELECT 1 FROM r WHERE CAST(json_extract(d, '$.id') AS INTEGER)=?")


def test_dates_read_through_cavnar_ts():                                  # SCALE-9
    out = _tr().translate("SELECT date(x), datetime(?, '+1 day'), julianday(created_at) FROM t")
    assert "to_char(cavnar_ts(CAST(x AS text)), 'YYYY-MM-DD')" in out
    assert "(cavnar_ts(CAST(%s AS text)) + interval '+1 day')" in out
    assert "cavnar_ts(CAST(created_at AS text))" in out
    assert "cavnar_ts" not in _tr().translate("SELECT datetime('now', '-5 minutes')")
    sql = open(os.path.join(ROOT, "migrations", "postgres", "0000_sqlite_compat.sql")).read()
    assert "CREATE OR REPLACE FUNCTION cavnar_ts(v text)" in sql and "timezone('UTC'" in sql


def test_is_comparisons_and_untyped_parameters():
    tr = _tr()
    assert "user_id IS NOT DISTINCT FROM %s" in tr.translate("SELECT 1 FROM a WHERE user_id IS ?")
    assert "cohort IS DISTINCT FROM %s" in tr.translate("UPDATE e SET c=? WHERE cohort IS NOT ?")
    assert "x IS DISTINCT FROM COALESCE(%s, x)" in tr.translate("SELECT 1 FROM e WHERE x IS NOT COALESCE(?, x)")
    assert "x IS NULL" in tr.translate("SELECT 1 FROM e WHERE x IS NULL")
    assert "(CAST(%s AS text) IS NULL OR user_id=%s)" in tr.translate("SELECT 1 FROM m WHERE (? IS NULL OR user_id=?)")
    assert "CASE WHEN CAST(%s AS integer) <> 0 THEN" in tr.translate("UPDATE t SET n=CASE WHEN ? THEN 1 ELSE n END")
    assert "BETWEEN %s AND %s" in tr.translate("SELECT 1 FROM t WHERE d BETWEEN ? AND ?")


def test_upserts_resolve_like_sqlite():
    tr = _tr(pks={"ai_rate_hits": ["day", "bucket"], "labor_daily_history": ["id"], "x": ["id"]},
             cols={"ai_rate_hits": ["day", "bucket", "hits"], "x": ["id", "rid", "k", "v"],
                   "labor_daily_history": ["id", "restaurant_id", "date", "labor_pct", "saved_at"]},
             uniques={"x": [["id"], ["rid", "k"]]})
    up = tr.translate("INSERT INTO ai_rate_hits (day, bucket, hits) VALUES (date('now'), ?, 1) "
                      "ON CONFLICT(day, bucket) DO UPDATE SET hits = hits + 1")
    assert "DO UPDATE SET hits = ai_rate_hits.hits + 1" in up
    # INSERT OR REPLACE conflicts on the unique key the row names (SQLite
    # resolves any), every other column taking the new row's value.
    rep = tr.translate("INSERT OR REPLACE INTO x (rid, k, v) VALUES (?,?,?)")
    assert "ON CONFLICT (rid, k) DO UPDATE SET v = EXCLUDED.v" in rep
    # A table declared ON CONFLICT REPLACE: a plain INSERT replaces.
    dec = tr.translate("INSERT INTO labor_daily_history (restaurant_id, date, labor_pct) VALUES (?,?,?)")
    assert "ON CONFLICT (restaurant_id, date) DO UPDATE SET" in dec and "labor_pct = EXCLUDED.labor_pct" in dec
    assert "ON CONFLICT" not in _tr().translate("CREATE TABLE t (a, b, UNIQUE(a, b) ON CONFLICT REPLACE)")


def test_every_conflict_replace_table_is_in_the_registry():
    found = set()
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "tests", ".venv", "ios", ".claude", "node_modules")]
        for f in files:
            if f.endswith(".py") and f != "db_dialect.py":
                text = open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read()
                for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\s*\)", text, re.S):
                    if re.search(r"ON\s+CONFLICT\s+REPLACE", m.group(2), re.I):
                        found.add(m.group(1))
    assert found and found == set(db_dialect.CONFLICT_REPLACE)


def test_every_sqlite_trigger_and_function_has_a_postgres_form():
    import pg_copy
    code = ""
    for f in os.listdir(ROOT):
        if f.endswith(".py") and f not in ("db_dialect.py", "pg_copy.py"):
            code += open(os.path.join(ROOT, f), encoding="utf-8", errors="ignore").read()
    code += open(os.path.join(ROOT, "intelligence", "features.py")).read()
    raising = set(re.findall(r"CREATE TRIGGER IF NOT EXISTS (\w+)_append_only", code))
    assert raising == set(pg_copy.APPEND_ONLY)
    named = set(re.findall(r'create_function\("(\w+)"', code))
    assert named and named <= db_dialect.SQL_PORTED_FUNCTIONS


def test_aggregate_extras_round_json_group_and_having():
    tr = _tr(types={"staff_settings": {"active": "bigint", "phone": "text"}})
    assert "ROUND(CAST((SUM(cost_usd)) AS numeric), 2)" in tr.translate("SELECT ROUND(SUM(cost_usd), 2) FROM u")
    assert "(CAST(meta AS text) IS JSON)" in tr.translate("SELECT 1 FROM t WHERE json_valid(meta)")
    js = tr.translate("UPDATE d SET s=json_set(s, '$.n', json(?), '$.b.status', 'landed') WHERE id=?")
    assert "jsonb_set(jsonb_set(CAST(s AS jsonb), '{n}', CAST(%s AS jsonb), true), '{b,status}', " \
           "to_jsonb(CAST('landed' AS text)), true)" in js
    each = tr.translate("SELECT 1 FROM i WHERE NOT EXISTS (SELECT 1 FROM json_each(i.meta, '$.modules') j "
                        "WHERE j.value IN (?))")
    assert "jsonb_array_elements_text(cavnar_json_array((CAST(i.meta AS jsonb) #> '{modules}'))) AS j(value)" in each
    g = tr.translate("SELECT a.rid, r.name, r.grp AS g, COUNT(*) AS n FROM a JOIN r ON r.id=a.rid "
                     "GROUP BY a.rid HAVING n > 1")
    assert "MIN(r.name) AS name, MIN(r.grp) AS g" in g and "HAVING COUNT(*) > 1" in g
    # Beside a MAX() SQLite takes the bare column from THAT row: never MIN().
    assert "MIN(" not in tr.translate("SELECT MAX(h) AS h, net FROM p GROUP BY d")
    assert "string_agg(DISTINCT CAST(d AS text), ',')" in tr.translate("SELECT GROUP_CONCAT(DISTINCT d) FROM t")
    assert "SET active=COALESCE(active, %s)" in tr.translate(
        "UPDATE staff_settings SET active=COALESCE(NULLIF(active, ''), ?), phone=COALESCE(NULLIF(phone, ''), ?)")


def test_rowid_reads_back_under_its_name_and_the_ddl_gives_one():
    tr = _tr(pks={"d": ["restaurant_id", "key"]}, cols={"d": ["restaurant_id", "key", "rowid"]})
    assert tr.translate("SELECT rowid AS rowid, * FROM d WHERE rowid!=?") == \
        "SELECT rowid AS rowid, * FROM d WHERE rowid!=%s"
    assert tr.translate("SELECT rowid, key FROM d") == "SELECT rowid AS rowid, key FROM d"
    ddl = _tr().translate_ddl("CREATE TABLE IF NOT EXISTS d (restaurant_id INTEGER, key TEXT, "
                              "PRIMARY KEY (restaurant_id, key))")
    assert db_dialect.ROWID_COLUMN in ddl
    assert "rowid" not in _tr().translate_ddl("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT)")


def test_the_copy_keeps_a_columns_constraints_after_its_reference():
    import pg_copy
    pre, post = pg_copy.table_ddl("client_data", "CREATE TABLE client_data (id INTEGER PRIMARY KEY, "
                                  "restaurant_id INTEGER NOT NULL REFERENCES restaurants(id) UNIQUE, x TEXT)")
    assert "restaurant_id BIGINT NOT NULL UNIQUE" in pre[0]
    assert post == ["ALTER TABLE client_data ADD FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)"]


# ── statement atomicity, against a driver that keeps Postgres's rule ────────

class _AbortingRaw:
    """A DB-API connection over SQLite that enforces Postgres's rule: after
    any failed statement the transaction refuses everything until ROLLBACK
    (or ROLLBACK TO SAVEPOINT) — the auditor's pg_emul, kept as a test."""
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("BEGIN")
        self.aborted, self.points = False, []

    def cursor(self):
        return _AbortingCur(self)

    def commit(self):
        if self.aborted:
            self.aborted = False
            self.db.execute("ROLLBACK")
            self.db.execute("BEGIN")
            raise RuntimeError("commit of an aborted transaction")
        self.db.execute("COMMIT")
        self.db.execute("BEGIN")

    def rollback(self):
        self.aborted = False
        self.db.execute("ROLLBACK")
        self.db.execute("BEGIN")

    def close(self):
        pass


class _AbortingCur:
    def __init__(self, raw):
        self.raw, self.description, self._rows, self.rowcount = raw, None, [], -1

    def execute(self, sql, params=None):
        up = sql.strip().upper()
        if up.startswith("ROLLBACK TO SAVEPOINT"):
            self.raw.aborted = False
        elif self.raw.aborted:
            raise RuntimeError("InFailedSqlTransaction: current transaction is aborted")
        if "information_schema" in sql or "pg_index" in sql or "advisory" in sql:
            self._rows, self.description = [], None
            return
        try:
            c = self.raw.db.execute(sql.replace("%s", "?"), params or ())
        except sqlite3.Error as e:
            self.raw.aborted = True
            raise RuntimeError(("DuplicateColumn: column already exists " if "duplicate column" in str(e) else "")
                               + str(e))
        self.description, self.rowcount = c.description, c.rowcount
        self._rows = c.fetchall() if c.description else []

    def fetchall(self):
        return list(self._rows)


def test_a_tolerated_error_never_aborts_the_transaction_it_is_in():       # SCALE-5, SCALE-8
    db_dialect.Catalog.forget()
    raw = _AbortingRaw()
    conn = db_dialect.PgConnection(raw)
    raw.db.execute("CREATE TABLE forecast_log (id INTEGER PRIMARY KEY, naive_n INTEGER)")
    conn.execute("INSERT INTO forecast_log (naive_n) VALUES (1)")               # opens the transaction
    # init_db's already-applied ALTER, tolerated, then more work
    assert models._apply_migration(conn, "ALTER TABLE forecast_log ADD COLUMN naive_n INTEGER") is False
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT nope FROM forecast_log")                           # swallow-and-continue
    conn.execute("INSERT INTO forecast_log (naive_n) VALUES (2)")
    conn.commit()
    assert [r[0] for r in raw.db.execute("SELECT naive_n FROM forecast_log ORDER BY id")] == [1, 2]
    db_dialect.Catalog.forget()


# ── the bounded pool (SCALE-11) ─────────────────────────────────────────────

class _FakeRaw:
    opened = 0

    def __init__(self):
        _FakeRaw.opened += 1
        self.closed = False

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
        self.closed = True


@pytest.fixture
def fake_pool(monkeypatch):
    monkeypatch.setattr(db_dialect, "_driver_connect", lambda url: _FakeRaw())
    url = f"postgresql://fake/{uuid.uuid4().hex}"
    monkeypatch.setenv("PG_POOL_MAX", "2")
    monkeypatch.setenv("PG_POOL_OVERFLOW", "1")
    monkeypatch.setenv("PG_POOL_IDLE", "1")
    monkeypatch.setenv("PG_POOL_TIMEOUT", "1")
    yield url
    db_dialect.close_idle(url)


def test_the_pool_caps_a_process_and_lets_a_nested_connection_through(fake_pool):
    url = fake_pool
    a = db_dialect.connect(url)
    nested = db_dialect.connect(url)                      # this thread holds one: overflow
    assert db_dialect.pool_stats(url) == {"in_use": 2, "base": 1, "overflow": 1, "idle": 0, "open": 2}
    got, err = [], []

    def other():
        try:
            got.append(db_dialect.connect(url))           # a second thread: the base cap's last slot
            db_dialect.connect(url)                       # nested there: overflow is full -> waits, fails
        except sqlite3.OperationalError as e:
            err.append(str(e))
    t = threading.Thread(target=other)
    t.start()
    t.join(5)
    assert got and err and "pool exhausted" in err[0]
    for c in (a, nested, got[0]):
        c.close()
    st = db_dialect.pool_stats(url)
    assert st["in_use"] == 0 and st["idle"] == 1 and st["open"] == 1            # one kept, two closed


def test_a_connection_never_closed_gives_its_slot_back(fake_pool):
    url = fake_pool

    def leak():
        db_dialect.connect(url)
    t = threading.Thread(target=leak)
    t.start()
    t.join()
    import gc
    gc.collect()
    assert db_dialect.pool_stats(url)["in_use"] == 0


def test_an_old_connection_is_retired_not_reused(fake_pool, monkeypatch):
    url = fake_pool
    c = db_dialect.connect(url)
    raw = c._raw
    c.close()
    monkeypatch.setenv("PG_POOL_MAX_LIFETIME", "0")
    time.sleep(0.01)
    c2 = db_dialect.connect(url)
    assert c2._raw is not raw and raw.closed
    c2.close()


# ── per-resource locks (SCALE-12) ───────────────────────────────────────────

def test_hot_paths_lock_what_they_serialise_not_the_platform(tmp_path):
    assert db_dialect.resource_key("jobs", "claim", "generation") == db_dialect.resource_key("jobs", "claim",
                                                                                             "generation")
    assert db_dialect.resource_key("jobs", "claim", "generation") != db_dialect.resource_key("jobs", "claim", "bulk")
    c = sqlite3.connect(str(tmp_path / "x.db"), isolation_level=None)
    db_dialect.begin_write(c, "jobs", "enqueue", "k")
    assert c.in_transaction                                     # SQLite: BEGIN IMMEDIATE, as before
    c.execute("ROLLBACK")
    for path, needle in (("job_queue.py", 'begin_write(conn, "jobs", "enqueue", kind)'),
                         ("job_queue.py", 'db_dialect.lock(conn, "jobs", "claim", grp)'),
                         ("model_gateway.py", 'begin_write(conn, "model_gateway", minute)'),
                         ("ai_utils.py", 'begin_write(conn, "ai_rate", key)')):
        assert needle in open(os.path.join(ROOT, path)).read(), path


# ── the shadow compares content (SCALE-10) ──────────────────────────────────

def test_the_shadow_compare_finds_a_shift_with_the_wrong_time(db_path, tmp_path, monkeypatch):
    import pg_shadow
    import schedule_versions as sv
    from test_sf10_h2_postgres import _SCHED_SQLITE, _csv, _row
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setenv("PG_SHADOW_WRITE", "1")
    target = sqlite3.connect(":memory:")
    target.execute(f"ATTACH '{tmp_path / 'sched.db'}' AS sched")
    target.executescript(_SCHED_SQLITE)
    rid = models.create_restaurant(models.Restaurant(name="Hash Grill", owner_email="h@x.com"), db_path=db_path)
    conn = real(db_path)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) "
                       "VALUES (?, '2026-10-19', '2026-10-25', '')", (rid,)).lastrowid
    conn.commit()
    conn.close()
    sv.append(rid, hid, "generated", _csv([_row("a" * 32, "Ana", "10:00am", "4:00pm", pid=1)]))
    assert pg_shadow.drain(target=target)["ok"] == 1
    good = pg_shadow.compare(target)
    assert good["ok"] and good["content"] == {"checked": 1, "mismatched": []}
    target.execute("UPDATE sched.shifts SET start_ts = '2026-10-19T16:00:00+00:00'")
    bad = pg_shadow.compare(target)
    assert bad["revisions"] == (1, 1) and not bad["ok"] and len(bad["content"]["mismatched"]) == 1
    target.close()


# ══ against a real Postgres (CAVNAR_TEST_PG_URL) ════════════════════════════

live = pytest.mark.skipif(not PG, reason="CAVNAR_TEST_PG_URL names no scratch Postgres")


def _url(name):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(PG)
    return urlunsplit((p.scheme, p.netloc, "/" + name, p.query, p.fragment))


@pytest.fixture
def scratch_db():
    import psycopg
    name = f"q2live_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    yield _url(name)
    db_dialect.close_idle(_url(name))
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


_BOOT = r"""
import os, sys, json, contextlib, io
sys.modules["dotenv"] = None                     # never a .env: no provider keys in this process
sys.path.insert(0, os.getcwd())
import models
with contextlib.redirect_stdout(io.StringIO()):
    import status_manager, platform_monitor, provider_health, auth, webhooks, guest_marketing, push, sales_audits
    status_manager.assert_platform_not_emptied()
    models.init_db()
    platform_monitor.record_boot_start()
    auth.init_auth()
    platform_monitor.init_platform_tables()
    provider_health.init_provider_health()
    models.ensure_columns()
    for fn in ("init_staff_notes", "init_staff_availability", "init_email_log", "init_onboarding_emails",
               "init_two_fa_backup_codes", "init_competitor_snapshots", "init_ai_visibility_queries",
               "init_staff_capabilities", "init_shift_profiles", "init_capability_changes", "init_ask_memory"):
        getattr(models, fn)()
    webhooks.init_webhooks(); guest_marketing.init_guest_marketing(); push.init_push()
    sales_audits.init_sales_audits()
c = models.get_conn()
if models._db_backend() == "postgres":
    rows = c.execute_native("SELECT table_name, column_name FROM information_schema.columns "
                            "WHERE table_schema = current_schema()").fetchall()
    out = {}
    for t, col in rows:
        out.setdefault(t, []).append(col)
    out.pop("sqlite_master", None)
else:
    out = {t: [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
           for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
c.close()
print("SCHEMA" + json.dumps(out))
"""


def _boot(tmp_path, url=None):
    """hosted_dashboard's boot chain in a process of its own — as production
    boots: a fresh volume, DATABASE_URL when `url`, no provider keys.
    Returns {table: [columns]} of the database it built."""
    import json
    import subprocess
    vol = tmp_path / ("pgvol" if url else "litevol")
    vol.mkdir(exist_ok=True)
    (vol / "reviews.db").write_bytes(b"")
    env = {k: v for k, v in os.environ.items()
           if not k.endswith(("_API_KEY", "_AUTH_TOKEN", "_SECRET_KEY")) and k not in
           ("DATABASE_URL", "CAVNAR_TEST_PG_URL")}
    env["RAILWAY_VOLUME_MOUNT_PATH"] = str(vol)
    if url:
        env["DATABASE_URL"] = url
    r = subprocess.run([sys.executable, "-c", _BOOT], cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    line = next(x for x in r.stdout.splitlines() if x.startswith("SCHEMA"))
    return {t: set(c) for t, c in json.loads(line[6:]).items()}


@live
def test_the_boot_runs_twice_on_postgres_and_builds_sqlites_schema(scratch_db, tmp_path):
    first = _boot(tmp_path, scratch_db)
    again = _boot(tmp_path, scratch_db)                        # every ALTER already applied
    lite = _boot(tmp_path)
    assert first == again
    assert set(first) == set(lite)
    assert {t: cols - {"rowid"} for t, cols in first.items()} == lite
    c = db_dialect.connect(scratch_db)
    try:
        trig = {r[0] for r in c.execute_native("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()}
        assert {"trg_outcomes_changed_ins", "trg_intel_features_version", "guest_consent_events_append_only",
                "intel_pattern_history_append_only"} <= trig
    finally:
        c.close()


@live
def test_the_copy_then_the_boot_is_the_cutovers_step_4(scratch_db, tmp_path):
    import contextlib
    import io
    import pg_copy
    import psycopg
    db_path = str(tmp_path / "source.db")                     # a SQLite file, as the volume's
    with contextlib.redirect_stdout(io.StringIO()):
        models.init_db(db_path)
        models.ensure_columns(db_path)
    rid = models.create_restaurant(models.Restaurant(name="Copy Live", owner_email="cl@x.com"), db_path=db_path)
    s = sqlite3.connect(db_path)
    s.execute("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, source, "
              "scheduled_hours) VALUES (?, '2026-09-01', 'Ana', 'ana', 'pos', '')", (rid,))
    s.commit()
    s.close()
    models.checkpoint(db_path)
    dst = psycopg.connect(scratch_db, autocommit=False)
    try:
        report = pg_copy.copy_database(db_path, dst, today=date(2026, 10, 14))
    finally:
        dst.close()
    assert report["verify"]["ok"], [t for t, r in report["verify"]["tables"].items() if not r["ok"]]
    # Step 4: DATABASE_URL set, the boot runs — twice (the next deploy).
    after = _boot(tmp_path, scratch_db)
    assert _boot(tmp_path, scratch_db) == after and "restaurants" in after
    c = db_dialect.connect(scratch_db)
    try:
        assert c.execute("SELECT name FROM restaurants WHERE id=?", (rid,)).fetchone()[0] == "Copy Live"
    finally:
        c.close()


@live
def test_every_static_statement_prepares_on_postgres(scratch_db):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import pg_sql_audit
    out = pg_sql_audit.prepare_check(scratch_db)
    assert out["prepared"] > 3500
    assert out["prepare_failed"] == [], out["prepare_failed"][:5]


@live
def test_postgres_answers_as_sqlite_does(scratch_db):
    c = db_dialect.connect(scratch_db)
    try:
        db_dialect.install_compat(c)
        c.execute("CREATE TABLE IF NOT EXISTS q2 (id INTEGER PRIMARY KEY AUTOINCREMENT, k TEXT, n INTEGER, "
                  "stamp TEXT, meta TEXT, name TEXT)")
        c.executemany("INSERT INTO q2 (k, n, stamp, meta, name) VALUES (?,?,?,?,?)",
                      [("a", 1, None, '{"n": 7, "s": "x"}', "bob"), ("b", None, "2026-10-01 10:00:00",
                                                                   '{"n": 2}', "Ann"),
                       ("c", 3, "", "{}", "carl")])
        c.commit()
        assert [r[0] for r in c.execute("SELECT k FROM q2 ORDER BY stamp DESC")] == ["b", "c", "a"]
        assert c.execute("SELECT MAX(n, 5) FROM q2 WHERE k='b'").fetchone()[0] is None
        assert c.execute("SELECT date(stamp) FROM q2 WHERE k='c'").fetchone()[0] is None       # '' -> NULL
        row = c.execute("SELECT json_extract(meta, '$.n') AS n, json_extract(meta, '$.s') AS s FROM q2 "
                        "WHERE k='a'").fetchone()
        assert row["n"] == 7 and row["s"] == "x"
        assert c.execute("SELECT k FROM q2 WHERE json_extract(meta, '$.n') > 5").fetchone()[0] == "a"
        assert c.execute("SELECT SUM(n IS NULL), SUM(n), AVG(n) FROM q2").fetchone()[:] == (1, 4, 2.0)
        assert [r[0] for r in c.execute("SELECT name FROM q2 ORDER BY name COLLATE NOCASE")] == ["Ann", "bob", "carl"]
        assert c.execute("SELECT COUNT(*) FROM q2 WHERE name LIKE 'B%'").fetchone()[0] == 1
        # A failed statement in a transaction costs only itself.
        c.execute("UPDATE q2 SET n = 10 WHERE k='a'")
        with pytest.raises(sqlite3.OperationalError):
            c.execute("SELECT nope FROM q2")
        c.execute("UPDATE q2 SET n = n + 1 WHERE k='a'")
        c.commit()
        assert c.execute("SELECT n FROM q2 WHERE k='a'").fetchone()[0] == 11
        # executemany is one batch; a bad row undoes the batch, not the transaction.
        c.execute("UPDATE q2 SET n = 0 WHERE k='c'")
        with pytest.raises(sqlite3.IntegrityError):
            c.executemany("INSERT INTO q2 (id, k) VALUES (?, ?)", [(100, "x"), (1, "dup")])
        c.commit()
        assert c.execute("SELECT n FROM q2 WHERE k='c'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM q2 WHERE id=100").fetchone()[0] == 0
    finally:
        c.close()


@live
def test_a_rowid_survives_an_update_on_postgres(scratch_db):
    c = db_dialect.connect(scratch_db)
    try:
        c.execute("CREATE TABLE IF NOT EXISTS q2d (restaurant_id INTEGER, key TEXT, by TEXT, "
                  "PRIMARY KEY (restaurant_id, key))")
        c.execute("INSERT INTO q2d (restaurant_id, key, by) VALUES (1, 'a', 'x')")
        rowid = c.execute("SELECT rowid AS rowid, * FROM q2d").fetchone()["rowid"]
        c.execute("UPDATE q2d SET by='y' WHERE rowid=?", (rowid,))
        c.execute("UPDATE q2d SET by='z' WHERE rowid=?", (rowid,))                # ctid moved; rowid did not
        assert c.execute("SELECT by FROM q2d WHERE rowid=?", (rowid,)).fetchone()[0] == "z"
        assert "rowid" not in c.execute("SELECT * FROM q2d").fetchone().keys()   # as SQLite's SELECT *
        c.commit()
    finally:
        c.close()


@live
def test_locks_on_different_resources_do_not_wait(scratch_db):
    a, b = db_dialect.connect(scratch_db), None
    done = []

    def other(resource):
        c = db_dialect.connect(scratch_db)
        try:
            c.execute_native("SET lock_timeout = '300ms'")
            db_dialect.begin_write(c, *resource)
            done.append(resource)
        except sqlite3.OperationalError:
            done.append(("blocked",) + resource)
        finally:
            c.rollback()
            c.close()
    try:
        db_dialect.begin_write(a, "jobs", "claim", "generation")
        for res in (("jobs", "claim", "bulk"), ("jobs", "claim", "generation")):
            t = threading.Thread(target=other, args=(res,))
            t.start()
            t.join(5)
        assert done == [("jobs", "claim", "bulk"), ("blocked", "jobs", "claim", "generation")]
    finally:
        a.rollback()
        a.close()


@live
def test_the_shadow_on_postgres_matches_content_and_catches_a_change(db_path, scratch_db, tmp_path, monkeypatch):
    import pg_copy
    import pg_shadow
    import schedule_versions as sv
    from test_sf10_h2_postgres import _csv, _row
    target = db_dialect.connect(scratch_db)
    try:
        for stmt in db_dialect.split_statements(pg_copy._read_migration("0001_schedule_model.sql")):
            target.execute_native(stmt)
        target.commit()
        real = models.get_conn
        monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
        monkeypatch.setattr(models, "DB_PATH", db_path)
        monkeypatch.setenv("PG_SHADOW_WRITE", "1")
        rid = models.create_restaurant(models.Restaurant(name="Shadow Live", owner_email="sl@x.com"),
                                       db_path=db_path)
        conn = real(db_path)
        hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) "
                           "VALUES (?, '2026-10-19', '2026-10-25', '')", (rid,)).lastrowid
        conn.commit()
        conn.close()
        sv.append(rid, hid, "generated", _csv([_row("a" * 32, "Ana", "10:00am", "4:00pm", pid=1),
                                                _row("b" * 32, "Bo", "5:00pm", "1:00am", pid=2)]))
        assert pg_shadow.drain(target=target)["ok"] == 1
        good = pg_shadow.compare(target)
        assert good["ok"] and good["content"]["checked"] == 1
        target.execute_native("UPDATE sched.shifts SET employee = 'Someone Else' WHERE shift_id = %s", ("b" * 32,))
        target.commit()
        bad = pg_shadow.compare(target)
        assert bad["revisions"] == (1, 1) and not bad["ok"] and bad["content"]["mismatched"]
    finally:
        target.close()
