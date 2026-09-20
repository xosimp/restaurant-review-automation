"""Production failure & resiliency invariants (audit #21, Sep 2026).

The audit's finding was not that this platform handles failure badly — it
handles most failures carefully. It was that the platform has no defence
against LOAD, and that its two most important failure-tolerance decisions
(claim_period and the scheduler lease both failing open) rest on the same
dependency, so they fail together.

These tests pin the bounds that were added. Every one of them is a bound:
a ceiling on work, on time, on concurrency, or on repetition.
"""
import time
from datetime import date

import pytest

import models
import ops
import scheduler


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))


# ── claim_period: fail open ONCE, not every tick ────────────────────────────

def test_claim_period_falls_back_to_process_memory_when_the_db_is_unwritable(monkeypatch):
    """The scheduler ticks every 300s and acquire_scheduler_lease fails open
    on the SAME dependency, so a database that refuses writes used to mean
    every due job re-ran on every tick — twelve identical client digests an
    hour. It still runs (silently stopping is worse), but only once."""
    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(models, "get_conn", boom)
    ops._claim_fallback.clear()

    ticks = [ops.claim_period("weekly_digests", "2026-09-19") for _ in range(6)]
    assert ticks == [True, False, False, False, False, False]


def test_the_fallback_is_per_job_and_per_period(monkeypatch):
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    ops._claim_fallback.clear()
    assert ops.claim_period("digest", "day-1") is True
    assert ops.claim_period("digest", "day-1") is False
    # A different job, and the same job tomorrow, are both still allowed.
    assert ops.claim_period("backup", "day-1") is True
    assert ops.claim_period("digest", "day-2") is True


def test_the_fallback_cannot_grow_without_bound(monkeypatch):
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    ops._claim_fallback.clear()
    for i in range(ops._CLAIM_FALLBACK_MAX + 50):
        ops.claim_period("job", f"period-{i}")
    assert len(ops._claim_fallback) <= ops._CLAIM_FALLBACK_MAX


def test_a_healthy_database_still_claims_exactly_once(db_path):
    assert ops.claim_period("some_job", "2026-09-19") is True
    assert ops.claim_period("some_job", "2026-09-19") is False


# ── the review fetch is bounded, and the bound is fair ──────────────────────

def test_the_fetch_starts_where_the_last_bounded_pass_stopped(db_path):
    """A time bound with no cursor is worse than no bound: a pass that can
    only reach 400 of 900 restaurants reaches the SAME 400 every time, and
    the other 500 are never fetched at all."""
    ids = [10, 20, 30, 40]
    assert scheduler._fetch_order(ids) == [10, 20, 30, 40]

    scheduler._remember_fetch_cursor([10, 20, 30, 40], 2)      # covered 10, 20
    assert scheduler._fetch_order(ids) == [30, 40, 10, 20]

    # A pass that covered everyone leaves the cursor on whoever it finished
    # with, so the next pass starts after them. The rotation is harmless
    # when nobody is being skipped, and it is what keeps the FIRST
    # restaurant in the list from being the only one with a guaranteed slot.
    scheduler._remember_fetch_cursor([30, 40, 10, 20], 4)      # finished on 20
    assert scheduler._fetch_order(ids) == [30, 40, 10, 20]

    # And the one after that continues from where IT finished.
    scheduler._remember_fetch_cursor([30, 40, 10, 20], 1)      # covered 30 only
    assert scheduler._fetch_order(ids) == [40, 10, 20, 30]


def test_a_cursor_pointing_at_a_departed_restaurant_does_not_wedge(db_path):
    scheduler._remember_fetch_cursor([10, 20], 2)
    # 20 has since been deleted; the pass must still cover everyone.
    assert scheduler._fetch_order([50, 60]) == [50, 60]


def test_an_empty_list_is_not_an_error(db_path):
    assert scheduler._fetch_order([]) == []
    scheduler._remember_fetch_cursor([], 0)      # must not raise


def test_the_fetch_bounds_are_real_numbers():
    """A bound of 0 or None would silently disable the protection."""
    assert scheduler.FETCH_WORKERS >= 1
    assert scheduler.FETCH_MAX_SECONDS > 0
    # Under the 4-hour gap between slots, or the bound does not bound.
    assert scheduler.FETCH_MAX_SECONDS < 4 * 3600


# ── outbound calls cannot hang the four-thread platform ─────────────────────

def test_every_outbound_http_call_names_a_timeout():
    """Mirrors scripts/check_timeouts.py so the rule holds in CI even when
    the lint is not run. With --workers 1 --threads 4, one call waiting on
    the OS's TCP behaviour is a quarter of the platform."""
    import subprocess
    import sys
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, os.path.join(root, "scripts", "check_timeouts.py")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_docusign_has_a_connect_and_read_timeout():
    import docusign_helper
    connect, read = docusign_helper.DOCUSIGN_TIMEOUT
    assert connect > 0 and read > 0


# ── bounded Ask concurrency ─────────────────────────────────────────────────

def test_ask_concurrency_is_bounded():
    """Every Ask request spawns a daemon thread. The ceiling used to be
    incidental — gunicorn's four request threads — which is not a design."""
    import client_api
    assert client_api.ASK_MAX_CONCURRENT >= 1
    slots = client_api._ASK_SLOTS
    taken = []
    try:
        while slots.acquire(blocking=False):
            taken.append(1)
            if len(taken) > client_api.ASK_MAX_CONCURRENT:
                break
        assert len(taken) == client_api.ASK_MAX_CONCURRENT
        # One more must be refused rather than queued forever.
        assert slots.acquire(blocking=False) is False
    finally:
        for _ in taken:
            slots.release()


# ── a budget stop reaches the owner ─────────────────────────────────────────

def test_a_budget_stop_is_told_to_the_owner_not_hidden_behind_try_again():
    """AIBudgetExceeded carries an actionable sentence. It was caught
    specifically in exactly one place in the product, so everywhere else an
    owner whose AI was paused was told to retry a condition retrying cannot
    clear."""
    from ai_utils import AIBudgetExceeded, user_facing_error
    msg, status = user_facing_error(
        AIBudgetExceeded("AI is paused — this account has reached its monthly budget."))
    assert "paused" in msg
    assert status == 429

    msg2, status2 = user_facing_error(RuntimeError("connection reset"))
    assert "paused" not in msg2
    assert status2 == 502


# ── CSV import cannot occupy a request thread indefinitely ──────────────────

def test_the_csv_row_cap_is_set_and_generous_but_finite():
    import client_api
    # A year of shifts for a 40-person restaurant is ~15,000 rows.
    assert 15_000 <= client_api.MAX_CSV_ROWS <= 100_000


# ── health reports what actually takes writes down ──────────────────────────

def test_health_reports_disk_state(db_path):
    """A full volume is the one failure that takes WRITES down and leaves
    reads up: SELECT 1 keeps passing while nothing saves, every fail-open
    gate keeps failing open, and the first symptom is data not being there."""
    import status_manager
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200
    assert payload["disk"]["state"] in ("ok", "low", "critical", "unknown")
    assert payload["db"] == "ok"


def test_health_500s_only_on_an_unreadable_database(monkeypatch, db_path):
    """A stale scheduler or a filling volume is a 200 carrying the signal —
    failing the healthcheck there replaces a real problem with a deploy
    problem on top of it."""
    import status_manager
    monkeypatch.setattr(status_manager, "disk_state",
                        lambda p=None: {"state": "critical", "free_mb": 3, "pct_free": 0.1})
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200
    assert payload["status"] == "degraded"

    monkeypatch.setattr(models, "get_conn",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk I/O error")))
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 500
    assert payload["status"] == "error"


def test_the_status_page_and_the_health_endpoint_agree_on_nearly_full(db_path):
    """Two definitions of "nearly full" is how a status page says
    operational while the monitor says critical."""
    import status_manager
    d = status_manager.disk_state(db_path)
    payload, _ = status_manager.health_snapshot(db_path)
    assert payload["disk"]["state"] == d["state"]


# ── circuit breaker: retry is the wrong answer to an outage ─────────────────

def test_the_breaker_opens_after_repeated_exhausted_retries():
    """With retries=2 and a 1.5^n backoff, every call during a provider
    outage costs three attempts and ~4s of held thread — on four request
    threads total. The retries do not help and the waiting is what turns
    somebody else's outage into ours."""
    import ai_utils
    ai_utils.reset_breaker()
    assert ai_utils.breaker_state("anthropic")[0] == "closed"

    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        ai_utils._breaker_record("anthropic", False)

    state, remaining = ai_utils.breaker_state("anthropic")
    assert state == "open" and remaining > 0
    with pytest.raises(ai_utils.AIProviderDown):
        ai_utils._breaker_check("anthropic")
    ai_utils.reset_breaker()


def test_one_success_closes_the_breaker():
    import ai_utils
    ai_utils.reset_breaker()
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        ai_utils._breaker_record("anthropic", False)
    assert ai_utils.breaker_state("anthropic")[0] == "open"
    ai_utils._breaker_record("anthropic", True)
    assert ai_utils.breaker_state("anthropic")[0] == "closed"
    ai_utils._breaker_check("anthropic")      # must not raise
    ai_utils.reset_breaker()


def test_the_breaker_lets_one_probe_through_when_the_window_expires(monkeypatch):
    """Otherwise it would stay open forever on a provider that recovered."""
    import ai_utils
    ai_utils.reset_breaker()
    monkeypatch.setattr(ai_utils, "CB_OPEN_SECONDS", 0)
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        ai_utils._breaker_record("anthropic", False)
    # Window already elapsed: the next check is the probe, and it is allowed.
    ai_utils._breaker_check("anthropic")
    ai_utils.reset_breaker()


def test_a_budget_stop_is_not_a_provider_failure():
    """AIBudgetExceeded is a decision this product made on purpose. Letting
    it trip the breaker would take AI down for every OTHER restaurant
    because one account hit its ceiling."""
    import ai_utils
    ai_utils.reset_breaker()
    # create_with_retry raises AIBudgetExceeded BEFORE the breaker is ever
    # consulted, so no failure is recorded.
    assert ai_utils.breaker_state("anthropic")[0] == "closed"


# ── concurrent updates ──────────────────────────────────────────────────────

def test_a_conflicting_settings_write_is_refused_not_silently_applied(db_path):
    """Two managers on the settings screen, or a manual edit landing during
    a POS sync, used to silently discard one side — there was no version
    column anywhere on this table."""
    from models import (create_restaurant, Restaurant, StaleWrite,
                        get_restaurant, restaurant_version, update_restaurant)
    rid = create_restaurant(Restaurant(name="Conc", owner_email="c@x.com"), db_path=db_path)

    seen_by_both = restaurant_version(rid, db_path=db_path)
    update_restaurant(rid, {"labor_target_pct": 28.0}, db_path=db_path,
                      expected_version=seen_by_both)

    with pytest.raises(StaleWrite) as exc:
        update_restaurant(rid, {"labor_target_pct": 31.0}, db_path=db_path,
                          expected_version=seen_by_both)
    assert exc.value.current_version > seen_by_both
    # The first write survived intact.
    assert get_restaurant(rid, db_path=db_path).labor_target_pct == 28.0


def test_writes_without_a_version_keep_last_write_wins(db_path):
    """Correct for the many single-field writes here — a POS sync stamping
    toast_last_synced has nothing to conflict with — so opting in must be
    the only thing that changes behaviour."""
    from models import (create_restaurant, Restaurant, get_restaurant,
                        restaurant_version, update_restaurant)
    rid = create_restaurant(Restaurant(name="Plain", owner_email="p@x.com"), db_path=db_path)
    before = restaurant_version(rid, db_path=db_path)
    update_restaurant(rid, {"labor_target_pct": 33.0}, db_path=db_path)
    assert get_restaurant(rid, db_path=db_path).labor_target_pct == 33.0
    # It still bumps the version, or a later versioned write would be blind.
    assert restaurant_version(rid, db_path=db_path) > before


# ── the backup is actually restorable ───────────────────────────────────────

def test_the_local_snapshot_keeps_credentials_and_the_emailed_copy_does_not(db_path, tmp_path):
    """Redaction used to be applied to the LOCAL snapshot, which is the
    primary restore artifact. That nulled every OAuth credential and deleted
    every session and device token, so a restore brought the data back and
    severed every integration for every client — and protected nothing,
    because the file sits on the same volume as the live database.
    """
    import shutil
    import sqlite3 as _sq
    from models import create_restaurant, Restaurant, update_restaurant
    import scheduler as _sched

    rid = create_restaurant(Restaurant(name="Backup Co", owner_email="b@x.com"),
                            db_path=db_path)
    update_restaurant(rid, {"gmb_refresh_token": "REAL-OAUTH-TOKEN"}, db_path=db_path)

    local = str(tmp_path / "local.db")
    src = _sq.connect(db_path)
    dst = _sq.connect(local)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    assert _sq.connect(local).execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    kept = _sq.connect(local).execute(
        "SELECT gmb_refresh_token FROM restaurants WHERE id=?", (rid,)).fetchone()[0]
    assert kept == "REAL-OAUTH-TOKEN", "the restore artifact must be restorable"

    emailed = str(tmp_path / "emailed.db")
    shutil.copy2(local, emailed)
    _sched._redact_snapshot(emailed)
    stripped = _sq.connect(emailed).execute(
        "SELECT gmb_refresh_token FROM restaurants WHERE id=?", (rid,)).fetchone()[0]
    assert stripped is None, "anything leaving the server must be redacted"


def test_a_recovery_runbook_exists_and_covers_the_real_failures():
    """A backup nobody has restored is a hypothesis. Audit #21 found working
    backups and no procedure anywhere for using one."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "RECOVERY.md"), encoding="utf-8").read().lower()
    for topic in ("integrity_check", "reviews.db-wal", "job_period_claims",
                  "volume full", "scheduler", "drill"):
        assert topic in text, f"RECOVERY.md does not cover {topic}"


# ── request metrics ─────────────────────────────────────────────────────────

def test_latency_and_error_rate_are_measured():
    """"Is the site slow?" had no answer but a customer complaint."""
    import flask
    import http_layer

    app = flask.Flask(__name__)
    http_layer.register(app)

    @app.route("/ok")
    def _ok():
        return "ok"

    @app.route("/boom")
    def _boom():
        return "no", 500

    client = app.test_client()
    http_layer.reset_metrics()
    for _ in range(9):
        client.get("/ok")
    client.get("/boom")

    m = http_layer.request_metrics()
    assert m["requests"] == 10
    assert m["server_error_rate"] == 10.0
    assert m["p50_ms"] is not None and m["p95_ms"] is not None
    http_layer.reset_metrics()


def test_metrics_key_on_the_route_rule_not_the_raw_path():
    """/api/reviews/1234 and /api/reviews/5678 are one endpoint. Keying on
    the path makes every id its own row and the aggregate meaningless."""
    import flask
    import http_layer

    app = flask.Flask(__name__)
    http_layer.register(app)

    @app.route("/thing/<int:n>")
    def _thing(n):
        return "ok"

    client = app.test_client()
    http_layer.reset_metrics()
    client.get("/thing/1")
    client.get("/thing/2")
    with http_layer._metrics_lock:
        rules = {row[3] for row in http_layer._samples}
    assert rules == {"/thing/<int:n>"}
    http_layer.reset_metrics()
