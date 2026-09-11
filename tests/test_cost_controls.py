"""Audit #7 re-run: every paid dependency answers to one ceiling.

The audit's core finding was that the budget system was well built and only
covered Claude. Perplexity and Google Places were both outside it — no
ledger row, no ceiling — while AI visibility could fire nine sonar queries a
minute per restaurant. Alongside that: the global pool was a flat number that
shrank in usefulness with every client won, and the ledger the budget check
reads on every call had no index and nothing pruning it.
"""
import pytest

import ai_utils
import models
import ops


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, ai_utils, ops):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ai_utils._budget_cache.clear()
    yield
    ai_utils._budget_cache.clear()


def _restaurant(db_path, rid=1, billing_status="active"):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email, billing_status) VALUES (?,?,?,?)",
                 (rid, f"R{rid}", f"o{rid}@x.test", billing_status))
    conn.commit()
    conn.close()
    return rid


def _spend_rows(db_path, **where):
    conn = models.get_conn(db_path)
    try:
        conn.execute(ai_utils._USAGE_TABLE_SQL)
        sql = "SELECT * FROM ai_usage"
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        return [dict(r) for r in conn.execute(sql, tuple(where.values()))]
    finally:
        conn.close()


# ── Perplexity and Places are metered ──────────────────────────────────────

def test_a_perplexity_search_lands_in_the_ledger(db_path):
    rid = _restaurant(db_path)
    cost = ai_utils.log_api_call(rid, "ai_visibility", "perplexity-search",
                                 calls=1, input_tokens=800, output_tokens=200, db_path=db_path)
    rows = _spend_rows(db_path, model="perplexity-search")
    assert len(rows) == 1 and rows[0]["cost_usd"] > 0
    # Per-search fee plus tokens, not one or the other.
    assert cost > ai_utils._PER_CALL_PRICING["perplexity-search"]


def test_a_places_request_lands_in_the_ledger(db_path):
    rid = _restaurant(db_path)
    ai_utils.log_api_call(rid, "review_fetch", "google-places-details", calls=1, db_path=db_path)
    rows = _spend_rows(db_path, model="google-places-details")
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == pytest.approx(ai_utils._PER_CALL_PRICING["google-places-details"])


def test_non_claude_spend_counts_against_the_same_ceiling(db_path):
    """The whole point: one budget covering every paid dependency, not just
    the vendor that happened to report token counts."""
    rid = _restaurant(db_path)
    assert ai_utils.ai_budget_exceeded(rid, db_path) is None
    for _ in range(int(ai_utils.AI_DAILY_BUDGET_USD / ai_utils._PER_CALL_PRICING["google-places-nearby"]) + 2):
        ai_utils.log_api_call(rid, "competitor_intel", "google-places-nearby", calls=1, db_path=db_path)
    ai_utils._budget_cache.clear()
    assert ai_utils.ai_budget_exceeded(rid, db_path) is not None, \
        "Places spend still cannot trip the ceiling"


def test_a_failed_call_is_recorded_but_not_charged(db_path):
    rid = _restaurant(db_path)
    ai_utils.log_api_call(rid, "ai_visibility", "perplexity-search", calls=1,
                          status="error", error="HTTP 500", db_path=db_path)
    rows = _spend_rows(db_path, model="perplexity-search")
    assert len(rows) == 1 and rows[0]["cost_usd"] == 0.0


def test_every_places_caller_meters(db_path):
    """A new Places call site is easy to add and easy to forget."""
    import inspect
    import competitor, fetcher, weather
    for mod in (competitor, fetcher, weather):
        src = inspect.getsource(mod)
        assert "maps.googleapis.com" in src
        assert "_meter_places(" in src, f"{mod.__name__} calls Places without metering it"


# ── AI visibility is cached ────────────────────────────────────────────────

def test_visibility_has_a_cache_and_a_budget_gate():
    import inspect
    import client_api
    src = inspect.getsource(client_api._do_ai_visibility_inner)
    assert "_aivis_cache" in src, "the same three questions are asked again every time"
    assert "ai_budget_exceeded" in src, "Perplexity still bypasses the ceiling"


def test_a_partial_run_is_never_cached():
    """Caching a Perplexity outage would pin it in place for six hours and
    make it look like the restaurant's real standing."""
    import inspect
    import client_api
    src = inspect.getsource(client_api._do_ai_visibility_inner)
    assert 'if not _payload["partial"]' in src


def test_the_cache_window_is_shorter_than_a_day():
    import client_api
    assert 0 < client_api._AIVIS_CACHE_SECS <= 86400


# ── The global ceiling scales with the business ────────────────────────────

def test_the_global_pool_grows_with_paying_clients(db_path):
    """Flat $1,500 meant ten clients at their own cap before the shared pool
    bound — so the blast radius got worse with every client won."""
    base = ai_utils.global_monthly_budget(db_path)
    assert base == ai_utils.AI_GLOBAL_MONTHLY_BUDGET_USD
    for rid in range(1, 21):
        _restaurant(db_path, rid, "active")
    grown = ai_utils.global_monthly_budget(db_path)
    assert grown > base
    assert grown == 20 * ai_utils.AI_GLOBAL_PER_CLIENT_USD


def test_a_small_client_base_still_has_a_real_backstop(db_path):
    _restaurant(db_path, 1, "active")
    assert ai_utils.global_monthly_budget(db_path) == ai_utils.AI_GLOBAL_MONTHLY_BUDGET_USD


def test_unpaid_clients_do_not_inflate_the_pool(db_path):
    for rid in range(1, 30):
        _restaurant(db_path, rid, "trial")
    assert ai_utils.global_monthly_budget(db_path) == ai_utils.AI_GLOBAL_MONTHLY_BUDGET_USD


def test_a_zero_budget_still_disables_the_ceiling(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_GLOBAL_MONTHLY_BUDGET_USD", 0)
    for rid in range(1, 5):
        _restaurant(db_path, rid, "active")
    assert ai_utils.global_monthly_budget(db_path) == 0.0


# ── The ledger is indexed and pruned ───────────────────────────────────────

def test_the_budget_ledger_is_indexed(db_path):
    """_spend_since SUMs this table on every AI call. It was a full scan of a
    ledger nothing pruned."""
    _restaurant(db_path)
    ai_utils.log_ai_usage(1, "t", "claude-sonnet-5", 10, 10, db_path=db_path)
    conn = models.get_conn(db_path)
    idx = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ai_usage'")]
    conn.close()
    assert "idx_ai_usage_created" in idx
    assert "idx_ai_usage_restaurant_created" in idx


def _age_row(db_path, table, col, days):
    conn = models.get_conn(db_path)
    conn.execute(f"UPDATE {table} SET {col} = datetime('now', ?)", (f"-{days} days",))
    conn.commit()
    conn.close()


def test_old_ledger_rows_are_pruned(db_path):
    _restaurant(db_path)
    ai_utils.log_ai_usage(1, "t", "claude-sonnet-5", 10, 10, db_path=db_path)
    _age_row(db_path, "ai_usage", "created_at", 400)
    deleted = ops.prune_ledgers(db_path=db_path)
    assert deleted.get("ai_usage") == 1
    assert _spend_rows(db_path) == []


def test_recent_ledger_rows_survive(db_path):
    _restaurant(db_path)
    ai_utils.log_ai_usage(1, "t", "claude-sonnet-5", 10, 10, db_path=db_path)
    ops.prune_ledgers(db_path=db_path)
    assert len(_spend_rows(db_path)) == 1, "pruning ate spend the budget still needs"


def test_every_growing_ledger_has_a_retention_window():
    """Seven tables grew forever. Naming them here means a new one is a
    deliberate decision rather than an oversight."""
    for table in ("ai_usage", "job_runs", "job_failures", "push_deliveries",
                  "webhook_deliveries", "alert_log", "email_log",
                  # Added by the Intel audits. Eight rows per visibility run
                  # and one per competitor per weekly analysis, both
                  # initially registered nowhere.
                  "ai_visibility_query_runs", "competitor_snapshots",
                  "ai_visibility_runs"):
        assert table in ops._RETENTION_DAYS, f"{table} still grows without bound"
        assert table in ops._RETENTION_COLUMN, f"{table} has no timestamp column mapped"


def test_pruning_survives_a_table_that_does_not_exist(db_path, monkeypatch):
    monkeypatch.setitem(ops._RETENTION_DAYS, "no_such_table", 30)
    ops.prune_ledgers(db_path=db_path)  # must not raise


def test_retention_of_zero_disables_pruning_for_that_table(db_path, monkeypatch):
    _restaurant(db_path)
    ai_utils.log_ai_usage(1, "t", "claude-sonnet-5", 10, 10, db_path=db_path)
    _age_row(db_path, "ai_usage", "created_at", 400)
    monkeypatch.setitem(ops._RETENTION_DAYS, "ai_usage", 0)
    assert "ai_usage" not in ops.prune_ledgers(db_path=db_path)
    assert len(_spend_rows(db_path)) == 1


def test_the_nightly_prune_is_actually_scheduled():
    import inspect
    import scheduler
    src = inspect.getsource(scheduler.scheduler_loop)
    assert "prune_ledgers" in src, "retention exists but nothing runs it"
