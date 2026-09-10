"""Audit #6 re-run: the same failures injected, against the fixed code.

Each test reproduces one scenario from the chaos audit and asserts the
behaviour that was missing. The worst finding was that a revoked Google token
stopped reviews permanently while the monitor built to catch it reported
green, because last_fetched_at was stamped one line before the check that
would have caught it.
"""
import os
import sqlite3

import pytest

import models
import ops


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    # users lives in auth's schema, not init_db's — the review-sync detector
    # joins it to find active locations.
    import auth
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: real(db_path), raising=False)
    auth.init_auth(db_path=db_path)


def _restaurant(db_path, rid=1, **kw):
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test"}
    cols.update(kw)
    conn = models.get_conn(db_path)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _last_fetched(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return conn.execute("SELECT last_fetched_at FROM restaurants WHERE id=?", (rid,)).fetchone()[0]
    finally:
        conn.close()


def _captures(db_path):
    conn = models.get_conn(db_path)
    try:
        conn.execute(ops._TABLE_SQL)
        return [dict(r) for r in conn.execute("SELECT job, error FROM job_failures")]
    finally:
        conn.close()


# ── Scenario 1: the Google refresh token is revoked ────────────────────────

def _run_fetch(db_path, monkeypatch, *, token, place_id, places_reviews=None):
    """Drive run_daily_fetch with the Google layer injected."""
    import scheduler
    monkeypatch.setattr(models, "DB_PATH", db_path)
    real = models.get_conn
    monkeypatch.setattr(scheduler, "get_conn", lambda *a, **k: real(db_path), raising=False)

    import gmb
    monkeypatch.setattr(gmb, "get_valid_token", lambda rid: token, raising=False)
    monkeypatch.setattr(gmb, "fetch_reviews_via_gmb", lambda *a, **k: [], raising=False)

    import fetcher
    called = {"places": 0}

    def _places(pid, rid):
        called["places"] += 1
        return places_reviews or []

    monkeypatch.setattr(fetcher, "fetch_google", _places, raising=False)
    monkeypatch.setattr(scheduler, "fetch_google", _places, raising=False)
    scheduler.run_daily_fetch()
    return called


def test_a_revoked_token_does_not_look_like_a_fresh_sync(db_path, monkeypatch):
    """The worst finding. last_fetched_at was stamped whether or not the
    fetch reached Google, so the 25-hour staleness detector could never fire
    and reviews stopped arriving forever with everything reporting green."""
    rid = _restaurant(db_path, 1, gmb_refresh_token="revoked-token", reviews_live=1)
    _run_fetch(db_path, monkeypatch, token=None, place_id=None)
    assert _last_fetched(db_path, rid) is None, \
        "a failed fetch stamped last_fetched_at, which is what hid this for good"


def test_a_revoked_token_is_reported_to_engineering(db_path, monkeypatch):
    """get_valid_token returns None rather than raising, so nothing was
    captured — the failure reached no log, no digest, no status page."""
    _restaurant(db_path, 1, gmb_refresh_token="revoked-token", reviews_live=1)
    _run_fetch(db_path, monkeypatch, token=None, place_id=None)
    jobs = [c["job"] for c in _captures(db_path)]
    assert "review_fetch" in jobs, "a dead Google connection was still silent"


def test_a_revoked_token_falls_back_to_places(db_path, monkeypatch):
    """This used to be an `elif` on gmb_refresh_token, so a connected-but-
    broken restaurant never reached the Places path and fetched nothing."""
    _restaurant(db_path, 1, gmb_refresh_token="revoked-token",
                google_place_id="ChIJplace", reviews_live=1)
    called = _run_fetch(db_path, monkeypatch, token=None, place_id="ChIJplace")
    assert called["places"] == 1, "no fallback — the restaurant would fetch nothing at all"


def test_a_working_fetch_still_stamps_the_sync(db_path, monkeypatch):
    """The fix must not swing the other way and mark healthy restaurants stale."""
    rid = _restaurant(db_path, 1, google_place_id="ChIJplace", reviews_live=1)
    _run_fetch(db_path, monkeypatch, token=None, place_id="ChIJplace")
    assert _last_fetched(db_path, rid) is not None


# ── Scenario 2: the detector watches the right population ──────────────────

def test_the_staleness_detector_sees_a_stalled_restaurant(db_path, monkeypatch):
    import status_manager as sm
    real = models.get_conn
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: real(db_path), raising=False)
    rid = _restaurant(db_path, 1, gmb_refresh_token="t", reviews_live=1)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO users (restaurant_id, username, email, password_hash, is_active) "
                 "VALUES (?,?,?,?,1)", (rid, "u1", "u1@x.test", "x"))
    conn.execute("UPDATE restaurants SET last_fetched_at = datetime('now','-40 hours') WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    sm.seed_default_services()
    sm._check_review_sync()
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT status FROM service_status WHERE service_key='review_sync'").fetchone()
    conn.close()
    assert row["status"] in ("degraded", "outage"), \
        "the detector still reports green while reviews are dead"


def test_a_places_only_restaurant_is_monitored_too(db_path, monkeypatch):
    """A Places-only restaurant with reviews_live=1 IS fetched, so it must be
    monitored. The original check read gmb_access_token and missed it."""
    import status_manager as sm
    real = models.get_conn
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: real(db_path), raising=False)
    rid = _restaurant(db_path, 1, google_place_id="ChIJplace", reviews_live=1)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO users (restaurant_id, username, email, password_hash, is_active) "
                 "VALUES (?,?,?,?,1)", (rid, "u1", "u1@x.test", "x"))
    conn.execute("UPDATE restaurants SET last_fetched_at = datetime('now','-40 hours') WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    sm.seed_default_services()
    sm._check_review_sync()
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT status FROM service_status WHERE service_key='review_sync'").fetchone()
    conn.close()
    assert row["status"] in ("degraded", "outage")


def test_a_restaurant_that_is_not_fetched_is_not_reported_stale(db_path, monkeypatch):
    """The over-correction. A Place ID with reviews_live=0 is deliberately not
    fetched, so measuring its last_fetched_at reports an outage that nothing
    can ever clear — which is what the live status page was showing."""
    import status_manager as sm
    real = models.get_conn
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: real(db_path), raising=False)
    rid = _restaurant(db_path, 1, google_place_id="ChIJplace", reviews_live=0)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO users (restaurant_id, username, email, password_hash, is_active) "
                 "VALUES (?,?,?,?,1)", (rid, "u1", "u1@x.test", "x"))
    conn.execute("UPDATE restaurants SET last_fetched_at = NULL WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    sm.seed_default_services()
    sm._check_review_sync()
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT status FROM service_status WHERE service_key='review_sync'").fetchone()
    conn.close()
    assert row["status"] == "operational", \
        "a restaurant the scheduler never fetches was reported as a review-sync outage"


# ── Scenario 3: Stripe is down during onboarding ───────────────────────────

def test_a_stripe_outage_reaches_the_failure_digest(db_path, monkeypatch):
    """It was a print and a traceback: the client got an email promising a
    payment link, and nobody knew it had failed."""
    import emails
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    import sys, types
    boom = types.SimpleNamespace(
        api_key=None,
        Product=types.SimpleNamespace(search=lambda **k: (_ for _ in ()).throw(RuntimeError("stripe is down"))),
        Price=types.SimpleNamespace(create=lambda **k: None),
        checkout=types.SimpleNamespace(Session=types.SimpleNamespace(create=lambda **k: None)),
    )
    monkeypatch.setitem(sys.modules, "stripe", boom)
    assert emails.create_stripe_checkout(4, "o@x.test", "R", "monthly") is None
    jobs = [c["job"] for c in _captures(db_path)]
    assert "stripe_checkout" in jobs, "a Stripe outage during onboarding was still invisible"


# ── Scenario 4: the model returns malformed rows ───────────────────────────

def test_dropped_schedule_rows_are_counted_and_surfaced():
    """A garbled response produced a SHORT schedule that looked complete."""
    import inspect
    import client_api
    src = inspect.getsource(client_api._run_schedule_job)
    assert "_dropped_rows.append" in src, "malformed rows are silently skipped again"
    assert "dropped_rows=len(_dropped_rows)" in src, "the count never reaches the client"
    assert 'job="schedule_generate"' in src, "the count never reaches engineering"


# ── Scenario 5: browser refresh loses an async result ──────────────────────

def test_a_refresh_does_not_destroy_a_finished_result(db_path):
    ops.start_async_job("chaos-1", "schedule", 3)
    ops.finish_async_job("chaos-1", "done", {"ok": True, "schedule_csv": "x"})
    first = ops.read_async_job("chaos-1")
    assert ops.read_async_job("chaos-1") == first
    assert ops.read_async_job("chaos-1") == first


# ── Scenario 6: the volume fills up ────────────────────────────────────────

def test_a_full_volume_is_detected(db_path, monkeypatch):
    """A full disk takes writes down without taking reads down, so the app
    keeps serving and data quietly stops saving. Nothing watched for it."""
    import shutil
    import status_manager as sm
    real = models.get_conn
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: real(db_path), raising=False)
    sm.seed_default_services()

    Usage = type("Usage", (), {})
    full = Usage(); full.total = 10 * 1024**3; full.free = 5 * 1024**2  # 5 MB left
    monkeypatch.setattr(shutil, "disk_usage", lambda p: full)
    sm._check_storage()
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT status, message FROM service_status WHERE service_key='storage'").fetchone()
    conn.close()
    assert row["status"] == "outage" and "full" in (row["message"] or "").lower()


def test_a_healthy_volume_reports_operational(db_path, monkeypatch):
    import shutil
    import status_manager as sm
    real = models.get_conn
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: real(db_path), raising=False)
    sm.seed_default_services()
    Usage = type("Usage", (), {})
    fine = Usage(); fine.total = 10 * 1024**3; fine.free = 8 * 1024**3
    monkeypatch.setattr(shutil, "disk_usage", lambda p: fine)
    sm._check_storage()
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT status FROM service_status WHERE service_key='storage'").fetchone()
    conn.close()
    assert row["status"] == "operational"


# ── Scenario 7: a crash mid-depletion must not empty the day ───────────────

def test_depletion_is_one_transaction(db_path):
    """It deletes the day's events and rebuilds them. Between those two the
    day is empty, and that was safe only because sqlite rolls back an
    uncommitted transaction on close — an implicit default holding up a real
    guarantee. It is explicit now."""
    import inspect
    import inventory_ledger
    src = inspect.getsource(inventory_ledger.compute_daily_depletion)
    assert "BEGIN IMMEDIATE" in src, "the rebuild relies on default isolation again"
