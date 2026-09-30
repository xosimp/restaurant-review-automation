"""9/29/26: the backup key is R2 "Object Read & Write" on one bucket — least
privilege — and R2 answers its GET ?lifecycle with 403. The weekly check then
measures what the rule guarantees: no copy under the prefix older than
BACKUP_OFFSITE_MAX_DAYS (+1 day for the store's deletion lag). A readable
absence of a rule (404) still fails, as before (tests/test_refix_r9_policy.py)."""
from datetime import datetime, timedelta, timezone

import ops
import scheduler


class _Resp:
    def __init__(self, status=200, content=b""):
        self.status_code, self.content, self.text = status, content, content.decode("utf-8", "ignore")


def _s3(monkeypatch):
    monkeypatch.setenv("BACKUP_S3_ENDPOINT", "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setenv("BACKUP_S3_BUCKET", "cavnar-backups")
    monkeypatch.setenv("BACKUP_S3_ACCESS_KEY_ID", "AKTEST")
    monkeypatch.setenv("BACKUP_S3_SECRET_ACCESS_KEY", "SKTEST")
    monkeypatch.setenv("BACKUP_S3_PREFIX", "cavnar-backups/")


def _listing(ages_days, truncated=False, token=None):
    now = datetime.now(timezone.utc)
    items = "".join(f"<Contents><Key>cavnar-backups/b{i}.enc</Key><LastModified>"
                    f"{(now - timedelta(days=a)).strftime('%Y-%m-%dT%H:%M:%S.000Z')}</LastModified></Contents>"
                    for i, a in enumerate(ages_days))
    tail = f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"
    if token:
        tail += f"<NextContinuationToken>{token}</NextContinuationToken>"
    return (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"{items}{tail}</ListBucketResult>").encode()


def _store(monkeypatch, pages):
    import requests
    seen = []

    def get(url, headers=None, timeout=None):
        seen.append(url)
        assert timeout
        if "?lifecycle" in url:
            return _Resp(403, b"<Error><Code>AccessDenied</Code></Error>")
        return pages.pop(0)
    monkeypatch.setattr(requests, "get", get)
    return seen


def _paged(monkeypatch):
    got = []
    monkeypatch.setattr(ops, "page_operator", lambda key, *a, **k: got.append(key) or {"sent": True})
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    return got


def test_a_key_that_cannot_read_the_rule_is_checked_by_the_oldest_copy(monkeypatch):
    _s3(monkeypatch)
    paged = _paged(monkeypatch)
    seen = _store(monkeypatch, [_Resp(200, _listing([1, 20], truncated=True, token="t1")),
                                _Resp(200, _listing([35.5]))])
    out = scheduler.run_offsite_lifecycle_check()
    assert out["ok"] == 1 and out["checked"] == "copies" and out["copies"] == 3
    assert 35 <= out["oldest_copy_days"] <= 36 and paged == []
    assert "list-type=2" in seen[1] and "continuation-token=t1" in seen[2]


def test_a_copy_past_the_limit_pages(monkeypatch):
    _s3(monkeypatch)
    paged = _paged(monkeypatch)
    _store(monkeypatch, [_Resp(200, _listing([2, 40]))])
    out = scheduler.run_offsite_lifecycle_check()
    assert out["failed"] == 1 and "40" in out["error"] and paged == ["backup_lifecycle"]


def test_no_copies_yet_is_not_a_failure_and_an_unlistable_store_is(monkeypatch):
    _s3(monkeypatch)
    paged = _paged(monkeypatch)
    _store(monkeypatch, [_Resp(200, _listing([]))])
    assert scheduler.run_offsite_lifecycle_check()["ok"] == 1
    _store(monkeypatch, [_Resp(403, b"")])
    out = scheduler.run_offsite_lifecycle_check()
    assert out["failed"] == 1 and "unlistable" in out["error"] and paged == ["backup_lifecycle"]
