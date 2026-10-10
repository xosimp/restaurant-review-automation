"""SCALE-30 / SCALE-14 (scheduler audit 10/10/26): a restaurant's read of a
growing table must SEARCH an index that leads with restaurant_id, never SCAN
the table — a scan costs O(every restaurant's rows) on every call, and the
_WeekData version query did exactly that (`SCAN schedule_versions USING
INDEX idx_schedule_versions_hist`, 2-3 ms per 2,400 rows against 0.05 ms).

Every literal `SELECT ... restaurant_id=? ...` in the app's modules is run
through EXPLAIN QUERY PLAN on the boot schema (the default test database,
built by the same init chain hosted_dashboard runs). A SCAN of a ledger —
any table on the retention registry — or of one of the other tables that
grow with use fails here, naming the query and the plan."""
import ast
import glob
import os
import re
import sqlite3

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tables that grow with use (beyond the retention registry's ledgers, which
# are added below): the schedule store, the shift history, the POS archive,
# reviews, and the per-send / per-proposal tables a restaurant reads back.
GROWING = {
    "schedule_history", "schedule_versions", "schedule_shares", "shift_facts", "reviews",
    "ask_cavnar_actions", "guest_newsletters", "guest_optin_requests", "recipe_drafts",
    "async_jobs", "users", "inventory_history", "labor_history", "labor_daily_history",
    "quality_blobs",
}


def _growing_tables():
    import ops
    out = set(GROWING) | set(ops._RETENTION_DAYS)
    return out


def _literal_restaurant_selects():
    """(path, line, sql) for every string literal in the app that is a
    SELECT/WITH with a `restaurant_id = ?` predicate. Adjacent literals are
    one constant to the parser, so a query split over lines is read whole."""
    out = []
    files = glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "*", "*.py"))
    for path in files:
        rel = os.path.relpath(path, ROOT)
        if rel.startswith(("tests", "scripts", ".claude")):
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = " ".join(node.value.split())
                if re.match(r"(?i)^(SELECT|WITH)\b", sql) and re.search(r"(?i)restaurant_id\s*=\s*\?", sql):
                    out.append((rel, node.lineno, sql))
    return out


def _alias_map(sql):
    """{alias or table name: table} from the FROM/JOIN clauses."""
    out = {}
    for m in re.finditer(r"(?i)\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)(?:\s+(?:AS\s+)?([a-z_][a-z0-9_]*))?", sql):
        table, alias = m.group(1), m.group(2)
        out[table] = table
        if alias and alias.upper() not in ("WHERE", "ON", "JOIN", "LEFT", "INNER", "GROUP", "ORDER",
                                            "LIMIT", "USING", "CROSS", "NATURAL", "UNION", "SET"):
            out[alias] = table
    return out


def test_every_literal_restaurant_query_searches_its_growing_tables():
    import models
    conn = sqlite3.connect(models.DB_PATH)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        growing = _growing_tables() & tables
        queries = _literal_restaurant_selects()
        assert len(queries) > 300, len(queries)        # the probe is reading the code
        planned, bad = 0, []
        for rel, line, sql in queries:
            try:
                plan = conn.execute("EXPLAIN QUERY PLAN " + sql, [None] * sql.count("?")).fetchall()
            except sqlite3.Error:
                continue            # named parameters, a table made elsewhere, a fragment
            planned += 1
            names = _alias_map(sql)
            for row in plan:
                m = re.match(r"SCAN (\w+)", row[-1])
                if not m:
                    continue
                table = names.get(m.group(1), m.group(1))
                if table in growing:
                    bad.append(f"{rel}:{line}: {row[-1]} :: {sql[:160]}")
        assert planned > 300, planned
        assert not bad, "restaurant queries scanning a growing table:\n" + "\n".join(bad)
    finally:
        conn.close()


def test_the_week_data_version_query_searches_by_restaurant_and_time():
    """The exact query _WeekData runs for every learner (SCALE-14)."""
    import models
    conn = sqlite3.connect(models.DB_PATH)
    try:
        plan = " ".join(r[-1] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT DISTINCT history_id FROM schedule_versions WHERE restaurant_id=? "
            "AND created_at >= datetime('now', ?)", (1, "-168 days")).fetchall())
    finally:
        conn.close()
    assert "SEARCH schedule_versions USING INDEX idx_schedule_versions_rid_created" in plan, plan


@pytest.mark.parametrize("table, index", [
    ("schedule_versions", "idx_schedule_versions_rid_created"),
    ("login_history", "idx_login_history_rid"),
    ("webhook_deliveries", "idx_webhook_deliveries_rid"),
    ("ask_cavnar_actions", "idx_ask_actions_rid"),
    ("schedule_shares", "idx_schedule_shares_rid"),
])
def test_the_tenant_indexes_exist_in_a_booted_database(db_path, table, index):
    conn = sqlite3.connect(db_path)
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                                           (table,))}
    finally:
        conn.close()
    # db_path is init_db + ensure_columns; the auth / webhook tables are
    # created by their own init_* at boot and checked on the default db.
    if table in ("login_history", "webhook_deliveries"):
        import models
        conn = sqlite3.connect(models.DB_PATH)
        try:
            have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                                               (table,))}
        finally:
            conn.close()
    assert index in have, (table, sorted(have))
