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
