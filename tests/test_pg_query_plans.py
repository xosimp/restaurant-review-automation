"""Audit #25 WS10 (A01-16): the hottest statements' plans on a partitioned Postgres.

tests/test_query_plans.py reads SQLite's EXPLAIN QUERY PLAN only (and is skipped in the Postgres mode);
production runs on Postgres. Here the REAL functions behind the hottest paths run against a Postgres
database built the way production's was (the conftest's Postgres mode: the boot on SQLite, pg_copy into
month-partitioned fact tables, the boot again on Postgres), seeded across four months of partitions and
ANALYZEd; every SELECT they send is captured as Postgres received it (after db_dialect's translation, with
its parameters) and EXPLAINed with sequential scans, hash joins and merge joins priced out (`enable_seqscan`,
`enable_hashjoin`, `enable_mergejoin` off — so the verdict does not hang on how few rows a test seeds, and
a join's inner table must be reachable by its key). Every scan of a table must then be NARROWED by an index: an
Index / Index Only scan with an Index Cond, or a Bitmap Heap Scan fed by a Bitmap Index Scan with one. A
Seq Scan that survives the pricing, or an index walked whole with only a Filter (what the planner picks
over a priced-out Seq Scan when no index fits), reads every restaurant's rows on every call. The day-range
reads of the partitioned tables must also prune to the months they ask for.

The paths: the Home stamp (every Home load: ten per-restaurant reads) and a Home block read; the Labor
ribbon's daily window; the shift history window; a POS ticket day range (per-guest sales) and the Bar's
30-day ticket and ticket-line read; the review inbox; the DSR store's report, history and metric series;
the per-call AI spend check; the session lookup (every request); the scheduler's period claim; the job
queue's claim with work waiting.

Runs only with CAVNAR_TEST_PG_URL (a scratch server): CI's `postgres` job and the nightly full Postgres
suite run every test file that way. Nothing here reaches a provider or reads the wall clock for a date.
"""
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date, timedelta

import pytest

PG_URL = (os.getenv("CAVNAR_TEST_PG_URL") or "").strip()
pytestmark = pytest.mark.skipif(not PG_URL, reason="needs CAVNAR_TEST_PG_URL (a scratch Postgres): CI's postgres "
                                                    "job and the nightly Postgres suite")

# Four months of partitions, fixed: no wall clock.
DAY0 = date(2026, 6, 1)
DAYS = 120
RIDS = 3
LOGINS = 600
# Small, bounded tables a hot read may scan whole without growing with the fleet's activity — each with
# why. (A table here must stay small: one row per restaurant or per configuration.)
SCAN_OK = {}


def _url(dbname):
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(PG_URL)
    return urlunsplit((p.scheme, p.netloc, "/" + dbname, p.query, p.fragment))


@pytest.fixture
def pg(db_path, monkeypatch):
    """The test's Postgres database, every module's get_conn routed to it, seeded and analyzed."""
    import models
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if mod is not None and getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", conn)
        except Exception as e:      # a module that refuses getattr holds no get_conn
            print(f"skip {getattr(mod, '__name__', mod)}: {e}")
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import auth
    auth.init_auth(db_path)
    rids = _seed(db_path)
    return {"db_path": db_path, "rids": rids}


def _seed(db_path):
    import models
    import pg_partitions
    c = models.get_conn(db_path)
    try:
        pg_partitions.ensure(c, today=DAY0, months_ahead=4)
    finally:
        c.close()
    rids = [models.create_restaurant(models.Restaurant(name=f"Plan {i}", owner_email=f"plan{i}@x.example"))
            for i in range(RIDS)]
    days = [(DAY0 + timedelta(days=d)).isoformat() for d in range(DAYS)]
    c = models.get_conn(db_path)
    try:
        tickets, lines, facts, labor, dsr, usage, reviews, archived = [], [], [], [], [], [], [], []
        for rid in rids:
            for i, d in enumerate(days):
                for t in range(4):
                    tid = f"{d}-{t}"
                    tickets.append((rid, "rpower", tid, d, 40.0 + t, 2))
                    lines.append((rid, "rpower", f"{tid}-1", tid, d, "item1", "Burger", "sale", 1.0, 15.0))
                for e in range(3):
                    facts.append((rid, d, f"Person {e}", f"person {e}", "Server", "4:00pm", "10:00pm", 6.0, 6.0,
                                  "rpower"))
                labor.append((rid, d, "Monday", 4000.0, 1100.0, 27.5, 60.0))
                archived.append((rid, "rpower", d, 4, 4))
                dsr.append((rid, d, 1, "final"))
                usage.append((rid, "ask", "claude-sonnet-5-5", 0.01, f"{d} 12:00:00"))
            for k in range(60):
                reviews.append((rid, "google", f"r{rid}-{k}", "Guest", 4, "Good", days[k], days[k], "pending"))
        c.executemany("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, net_sales, "
                      "guest_count) VALUES (?,?,?,?,?,?)", tickets)
        c.executemany("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, ticket_id, business_date, "
                      "item_id, item_name, kind, qty, sales) VALUES (?,?,?,?,?,?,?,?,?,?)", lines)
        c.executemany("INSERT INTO shift_facts (restaurant_id, business_date, employee_name, employee_key, role, "
                      "shift_start, shift_end, scheduled_hours, actual_hours, source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      facts)
        c.executemany("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, "
                      "labor_pct, total_hours) VALUES (?,?,?,?,?,?,?)", labor)
        c.executemany("INSERT INTO dsr_reports (restaurant_id, business_date, version, status) VALUES (?,?,?,?)", dsr)
        c.executemany("INSERT INTO pos_archive_days (restaurant_id, provider, business_date, tickets, lines) "
                      "VALUES (?,?,?,?,?)", archived)
        c.executemany("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at) VALUES (?,?,?,?,?)",
                      usage)
        c.executemany("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                      "fetched_at, response_status) VALUES (?,?,?,?,?,?,?,?,?)", reviews)
        # Logins enough that the planner joins a session to its login the way production's does (from the
        # session's token in, never from a walk of every login out).
        c.executemany("INSERT INTO users (restaurant_id, username, email, password_hash) VALUES (?,?,?,?)",
                      [(rids[k % RIDS], f"plan{k}", f"plan{k}@x.example", "x") for k in range(LOGINS)])
        uids = [r[0] for r in c.execute("SELECT id FROM users WHERE username LIKE 'plan%' ORDER BY id").fetchall()]
        c.executemany("INSERT INTO memberships (user_id, restaurant_id, role) VALUES (?,?,?)",
                      [(u, rids[k % RIDS], "manager") for k, u in enumerate(uids)])
        c.executemany("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                      [(f"plan-token-{k}-{j}", u, "2099-01-01 00:00:00") for k, u in enumerate(uids) for j in range(2)])
        c.commit()
        c.execute_native("ANALYZE")
        c.commit()
    finally:
        c.close()
    return rids


@contextmanager
def _captured(monkeypatch):
    """Every SELECT the app sends Postgres while inside: (database, SQL as sent, parameters)."""
    import db_dialect
    seen = []
    real = db_dialect.PgConnection._run

    def _run(self, sql, params, cur):
        if re.match(r"\s*(SELECT|WITH)\b", sql, re.I):
            seen.append((self._raw.info.dbname, sql, tuple(params or ())))
        return real(self, sql, params, cur)
    monkeypatch.setattr(db_dialect.PgConnection, "_run", _run)
    try:
        yield seen
    finally:
        monkeypatch.setattr(db_dialect.PgConnection, "_run", real)


def _narrowed(node) -> bool:
    """Whether a scan node reads through an index condition (not a Seq Scan, not a whole index)."""
    kind = node.get("Node Type")
    if kind in ("Index Scan", "Index Only Scan", "Bitmap Index Scan"):
        return bool(node.get("Index Cond"))
    if kind == "Bitmap Heap Scan":
        feeds = []

        def walk(n):
            for c in n.get("Plans") or ():
                if c.get("Node Type") == "Bitmap Index Scan":
                    feeds.append(c)
                elif c.get("Node Type") in ("BitmapAnd", "BitmapOr"):
                    walk(c)
        walk(node)
        return bool(feeds) and all(c.get("Index Cond") for c in feeds)
    return False


def _scans(node, out):
    """[(node type, relation, narrowed)] of every table scan in a JSON plan tree."""
    if node.get("Relation Name"):
        out.append((node.get("Node Type"), node["Relation Name"], _narrowed(node)))
    for child in node.get("Plans") or ():
        _scans(child, out)
    return out


def _explain(dbname, sql, params):
    import psycopg
    with psycopg.connect(_url(dbname), connect_timeout=10, autocommit=True) as c:
        # Sequential scans, hash joins and merge joins priced out: what is left reaches every table through
        # an index if one fits — the verdict is about the indexes, not about how few rows a test seeds.
        c.execute("SET enable_seqscan = off")
        c.execute("SET enable_hashjoin = off")
        c.execute("SET enable_mergejoin = off")
        row = c.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()
    plan = row[0] if not isinstance(row[0], str) else json.loads(row[0])
    return _scans(plan[0]["Plan"], [])


def _parent(relation):
    """A partition's parent table (pos_tickets_p202608 -> pos_tickets, pos_tickets_default -> pos_tickets)."""
    import db_dialect
    m = re.match(r"^(?P<t>.+?)_(p\d{6}|default)$", relation)
    if m and m.group("t") in db_dialect.PARTITIONED:
        return m.group("t")
    return relation


def _plans(seen):
    """[(sql, [(node, relation)])] for every distinct captured statement."""
    out, done = [], set()
    for dbname, sql, params in seen:
        key = (sql, params)
        if key in done:
            continue
        done.add(key)
        out.append((sql, _explain(dbname, sql, params)))
    return out


def _seq_scans(plans):
    """Every scan no index narrows, outside SCAN_OK: "<node> on <relation> :: <statement>"."""
    bad = []
    for sql, scans in plans:
        for node, rel, narrowed in scans:
            if not narrowed and _parent(rel) not in SCAN_OK:
                bad.append(f"{node} on {rel} (no index condition) :: {' '.join(sql.split())[:220]}")
    return bad


def _run_hot_paths(pg):
    import ai_utils
    import auth
    import home_snapshots
    import job_queue
    import metrics
    import models
    import ops
    import shift_facts
    from dsr import store as dsr_store
    db, rid = pg["db_path"], pg["rids"][1]
    d0, d1 = "2026-08-01", "2026-08-31"
    home_snapshots.data_stamp(rid, db_path=db)
    home_snapshots._read(rid, "brief", "owner", db_path=db)
    models.get_labor_daily(rid, 14, db_path=db)
    shift_facts.history_rows(rid, since=d0, until=d1, db_path=db)
    shift_facts.rows(rid, since=d0, until=d1, db_path=db)
    metrics._per_guest(rid, d0, d1, None, db)
    from bar import comps as bar_comps
    bar_comps.comps_block(rid, today=date(2026, 9, 1), db_path=db)
    models.get_reviews_data(rid, limit=50, include_total=True)
    dsr_store.get_report(rid, "2026-08-15", db_path=db)
    dsr_store.latest_finished_report(rid, "2026-08-15", db_path=db)
    dsr_store.list_reports(rid, limit=30, db_path=db)
    dsr_store.metric_series(rid, "sales.net", d0, d1, db_path=db)
    ai_utils._spend_since("2026-09-01 00:00:00", restaurant_id=rid, db_path=db)
    uid = auth.create_user(rid, "planowner", "planowner@x.example", "x", db_path=db, generated=True)
    token = auth.create_session(uid, db_path=db, restaurant_id=rid)
    auth.get_session_user(token, db_path=db)
    ops.claim_period("ws10_plan_probe", "2026-08-15")
    job_queue.claim("ws10-plan-worker", ("generation",), db_path=db)


def test_the_hot_paths_never_scan_a_growing_table(pg, monkeypatch):
    with _captured(monkeypatch) as seen:
        _run_hot_paths(pg)
    plans = _plans(seen)
    tables = {_parent(rel) for _sql, scans in plans for _n, rel, _x in scans}
    # The probe really reached the tables it is about (a path that stopped querying would pass vacuously).
    for t in ("pos_tickets", "pos_ticket_lines", "pos_archive_days", "shift_facts", "labor_daily_history", "reviews",
              "dsr_reports", "dsr_metrics", "ai_usage", "sessions", "users", "memberships", "job_period_claims", "jobs",
              "home_snapshots", "schedule_history", "inventory_history"):
        assert t in tables, (t, sorted(tables))
    bad = _seq_scans(plans)
    assert not bad, "hot statements with no index that serves them:\n" + "\n".join(bad)


def test_a_day_range_reads_only_its_months_partitions(pg, monkeypatch):
    import metrics
    import shift_facts
    db, rid = pg["db_path"], pg["rids"][0]
    with _captured(monkeypatch) as seen:
        metrics._per_guest(rid, "2026-08-01", "2026-08-31", None, db)
        shift_facts.rows(rid, since="2026-08-03", until="2026-08-20", db_path=db)
    scanned = {rel for _sql, scans in _plans(seen) for _n, rel, _x in scans}
    for table in ("pos_tickets", "shift_facts"):
        parts = {r for r in scanned if _parent(r) == table and r != table}
        assert parts == {f"{table}_p202608"}, (table, sorted(parts))


def test_the_queue_claim_and_the_period_claim_use_their_indexes(pg, monkeypatch):
    """The two claims every consumer and every scheduler tick make, with work waiting."""
    import job_queue
    import ops
    db = pg["db_path"]
    for i in range(30):
        job_queue.enqueue("auto_draft", {"n": i}, restaurant_id=pg["rids"][i % RIDS],
                          idempotency_key=f"ws10-plan-{i}", wake=False, db_path=db)
    with _captured(monkeypatch) as seen:
        job_queue.claim("ws10-plan-worker", ("generation",), db_path=db)
        ops.claim_period("ws10_plan_probe", "2026-08-16")
        ops.claim_period("ws10_plan_probe", "2026-08-16")
    plans = _plans(seen)
    assert any("jobs" in {_parent(r) for _n, r, _x in scans} for _sql, scans in plans)
    bad = _seq_scans(plans)
    assert not bad, "\n".join(bad)
