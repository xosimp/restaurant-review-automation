"""ops.py — the silent-failure capture layer. If this breaks, failures go
back to being invisible, so it gets its own tests."""
import ops


def _use_tmp_db(monkeypatch, db_path):
    """Point ops' models.get_conn at the test database. Patching DB_PATH is
    not enough — get_conn binds it as a default argument at import time."""
    import models
    real_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda _ignored=None: real_get_conn(db_path))


def test_capture_persists_failure(monkeypatch, db_path):
    _use_tmp_db(monkeypatch, db_path)
    ops.capture(ValueError("google api quota exceeded"), job="review_fetch", context="Gia Mia")
    rows = ops.failures_last_24h()
    assert len(rows) == 1
    assert rows[0]["job"] == "review_fetch"
    assert "quota" in rows[0]["sample_error"]


def test_capture_never_raises(monkeypatch):
    """Even with a completely broken DB layer, capture() must not blow up the
    job that called it."""
    import models
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    ops.capture(ValueError("original error"), job="x")  # must not raise


def test_run_job_returns_result_on_success(monkeypatch, db_path):
    _use_tmp_db(monkeypatch, db_path)
    assert ops.run_job("fine", lambda: 42) == 42
    assert ops.failures_last_24h() == []


def test_run_job_captures_crash_and_returns_none(monkeypatch, db_path):
    _use_tmp_db(monkeypatch, db_path)

    def boom():
        raise RuntimeError("backup exploded")

    assert ops.run_job("backup_db", boom) is None
    rows = ops.failures_last_24h()
    assert rows and rows[0]["job"] == "backup_db"


def test_digest_silent_when_no_failures(monkeypatch, db_path):
    _use_tmp_db(monkeypatch, db_path)
    monkeypatch.setenv("RESEND_API_KEY", "fake")
    assert ops.send_failure_digest() is False  # no failures -> no email


def test_digest_sends_when_failures_exist(monkeypatch, db_path):
    import sys, types
    _use_tmp_db(monkeypatch, db_path)
    ops.capture(ValueError("boom"), job="review_fetch")

    sent = {}

    class FakeEmails:
        @staticmethod
        def send(payload):
            sent.update(payload)
            return {"id": "fake"}

    fake = types.ModuleType("resend")
    fake.Emails = FakeEmails
    fake.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake)
    monkeypatch.setenv("RESEND_API_KEY", "fake")

    assert ops.send_failure_digest() is True
    assert "review_fetch" in sent["html"]
    assert "failure" in sent["subject"]


# ── claim_period: the scheduler's run-once-per-period guard ──────────────────
#
# This replaced the module-level `_last_*_date` globals. Those reset on every
# Railway redeploy (so a deploy during a job's hour re-ran it), and several of
# them compared a string against a date so the guard was never satisfied and
# the job fired on every 300s tick.

def test_first_claim_wins_and_the_second_is_refused(monkeypatch, db_path):
    _use_tmp_db(monkeypatch, db_path)
    assert ops.claim_period("nightly_backup", "2026-09-08") is True
    assert ops.claim_period("nightly_backup", "2026-09-08") is False
    # ...and stays refused however many ticks arrive in the same hour
    assert [ops.claim_period("nightly_backup", "2026-09-08") for _ in range(11)] == [False] * 11


def test_a_new_period_is_claimable_again(monkeypatch, db_path):
    _use_tmp_db(monkeypatch, db_path)
    assert ops.claim_period("daily_digest", "2026-09-08") is True
    assert ops.claim_period("daily_digest", "2026-09-09") is True
    assert ops.claim_period("daily_digest", "2026-09-09") is False


def test_jobs_do_not_share_a_claim(monkeypatch, db_path):
    """The old bug: several jobs gated on the same `_last_fetch_date`, so
    whichever ran first silently suppressed the others."""
    _use_tmp_db(monkeypatch, db_path)
    assert ops.claim_period("competitor_analysis", "2026-09-08") is True
    assert ops.claim_period("review_fetch", "2026-09-08") is True
    assert ops.claim_period("toast_sync", "2026-09-08") is True


def test_a_claim_survives_a_redeploy(monkeypatch, db_path):
    """The whole point of moving this out of process memory: reloading the
    module (a fresh worker after a deploy) must not hand back the claim."""
    _use_tmp_db(monkeypatch, db_path)
    assert ops.claim_period("monthly_summary", "2026-09") is True

    import importlib
    reloaded = importlib.reload(ops)
    _use_tmp_db(monkeypatch, db_path)
    try:
        assert reloaded.claim_period("monthly_summary", "2026-09") is False
    finally:
        importlib.reload(ops)


def test_claim_fails_open_when_the_database_is_unreachable(monkeypatch):
    """A scheduler that silently stops working is worse than one that
    occasionally repeats a job."""
    import models
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert ops.claim_period("anything", "2026-09-08") is True


def test_old_claims_are_pruned(monkeypatch, db_path):
    _use_tmp_db(monkeypatch, db_path)
    import models
    ops.claim_period("stale_job", "2026-01-01")
    conn = models.get_conn()
    conn.execute("UPDATE job_period_claims SET claimed_at = datetime('now','-90 days')")
    conn.commit()
    conn.close()

    ops.claim_period("some_other_job", "2026-09-08")  # any call runs the prune

    conn = models.get_conn()
    remaining = [r[0] for r in conn.execute("SELECT job_key FROM job_period_claims").fetchall()]
    conn.close()
    assert "stale_job:2026-01-01" not in remaining
    assert "some_other_job:2026-09-08" in remaining
