"""Memory re-audit fix round (9/29/26), R9 — "policy_mismatch" (FORGET-9).

The published retention and deletion policy against the code:
  * a cancelled account's operational data is deleted within 30 days of
    cancellation (privacy.html section 07) — the console raises it as an
    offboarding issue, never an auto-delete;
  * off-site copies expire: a weekly check reads the object store's
    lifecycle rule and pages when none deletes copies within
    BACKUP_OFFSITE_MAX_DAYS; BACKUP_EMAIL_MODE=fallback sends the emailed
    copy only on a night object storage did not take one.
"""
from datetime import datetime, timedelta, timezone

import pytest

import admin_ops
import ops
import scheduler


class _Resp:
    def __init__(self, status=200, content=b""):
        self.status_code, self.content, self.text = status, content, content.decode("utf-8", "ignore")


def _s3(monkeypatch, prefix="cavnar-backups/"):
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "cavnar-backups")
    monkeypatch.setenv("BACKUP_S3_ACCESS_KEY_ID", "AKTEST")
    monkeypatch.setenv("BACKUP_S3_SECRET_ACCESS_KEY", "SKTEST")
    monkeypatch.setenv("BACKUP_S3_PREFIX", prefix)


_RULES = b"""<?xml version="1.0" encoding="UTF-8"?>
<LifecycleConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Rule><ID>old-drafts</ID><Status>Enabled</Status><Filter><Prefix>other/</Prefix></Filter>
        <Expiration><Days>3</Days></Expiration></Rule>
  <Rule><ID>backups-35</ID><Status>Enabled</Status><Filter><Prefix>cavnar-backups/</Prefix></Filter>
        <Expiration><Days>35</Days></Expiration></Rule>
  <Rule><ID>disabled</ID><Status>Disabled</Status><Filter><Prefix></Prefix></Filter>
        <Expiration><Days>1</Days></Expiration></Rule>
</LifecycleConfiguration>"""


def test_the_lifecycle_rule_covering_the_backups_is_read(monkeypatch):
    import requests
    import offsite_backup
    _s3(monkeypatch)
    seen = {}

    def get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout)
        return _Resp(200, _RULES)
    monkeypatch.setattr(requests, "get", get)
    got = offsite_backup.lifecycle_days()
    assert got == {"days": 35, "rule": "backups-35", "error": None}
    assert seen["url"].endswith("/cavnar-backups?lifecycle=") and seen["timeout"]
    assert seen["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256")


def test_no_rule_fails_the_weekly_check_and_pages(monkeypatch):
    import requests
    _s3(monkeypatch)
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, *a, **k: pages.append(key) or {"sent": True})
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(404, b"<Error><Code>NoSuchLifecycleConfiguration</Code></Error>"))
    out = scheduler.run_offsite_lifecycle_check()
    assert out["failed"] == 1 and out["lifecycle_days"] is None and pages == ["backup_lifecycle"]
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(200, _RULES))
    out = scheduler.run_offsite_lifecycle_check()
    assert out["ok"] == 1 and out["lifecycle_days"] == 35


def test_the_weekly_check_skips_without_object_storage(monkeypatch):
    for v in ("BACKUP_S3_ENDPOINT", "BACKUP_S3_BUCKET", "BACKUP_S3_ACCESS_KEY_ID", "BACKUP_S3_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(v, raising=False)
    out = scheduler.run_offsite_lifecycle_check()
    assert out["skipped"] == 1 and out["attempted"] == 0


def test_the_loop_runs_the_weekly_check_once_a_week():
    import inspect
    src = inspect.getsource(scheduler.scheduler_loop)
    assert '_ops.run_job("offsite_lifecycle", run_offsite_lifecycle_check)' in src


# ── the cancelled account's data ────────────────────────────────────────────

def _issues(bs="canceled", days_ago=10, deletion=None, is_demo=0):
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    r = {"id": 7, "billing_status": bs, "is_demo": is_demo, "contract_status": "signed", "name": "Harbor Grill"}
    class _D(dict):
        def __missing__(self, key):          # every other per-restaurant map: empty
            return {}
    d = _D(users={}, events={}, status_changes={7: {bs: at}})
    return admin_ops._issues_for(r, d, None, [], [], {"complete": True, "done": 1, "total": 1}, None,
                                 billing={}, users=[], deletion=deletion)


def _by_key(issues):
    return {i["key"].split(":", 1)[1]: i for i in issues}


def test_a_cancelled_account_raises_its_data_deletion_due_date():
    got = _by_key(_issues(days_ago=10))
    assert "cancelled_data" in got and got["cancelled_data"]["severity"] == "warning"
    assert "privacy policy" in got["cancelled_data"]["detail"]
    over = _by_key(_issues(days_ago=45))["cancelled_data"]
    assert over["severity"] == "critical" and "overdue" in over["title"]


def test_no_cancelled_data_issue_for_a_live_or_demo_account_or_an_open_request():
    assert "cancelled_data" not in _by_key(_issues(bs="active"))
    assert "cancelled_data" not in _by_key(_issues(is_demo=1))
    req = {"requested_at": "2026-09-01T00:00:00Z", "due_at": "2026-10-01T00:00:00Z", "days_left": 2,
           "due_label": "10/1/26"}
    assert "cancelled_data" not in _by_key(_issues(deletion=req))
