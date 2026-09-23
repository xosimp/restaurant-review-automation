"""Email safety: the suite must never send real mail, and production must
never be able to flood one address.

Background (Sep 7 2026): the email audit two days earlier fixed
emails._resend_key() to read RESEND_API_KEY at call time. Before that the
key was frozen empty at import, so every send in the test suite was a
silent no-op. After it, each full pytest run mailed ~75 real 2FA and
notification emails to the tests' fake addresses through the production
key and exhausted the Resend daily quota the night before a client
meeting. Prod itself sent zero emails in that week — the suite was the
whole leak. These tests pin the guard and the runtime cap."""
import pytest
import requests

import emails
import models


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    models.init_email_log(db_path=db_path)


def _payload(to="someone@x.test", subject="hello"):
    return {"from": "Will <will@cavnar.ai>", "to": [to], "subject": subject, "html": "<p>hi</p>"}


# ── the suite can't reach the network ────────────────────────────────────────

def test_resend_key_is_blank_under_test():
    assert emails._resend_key() == ""


def test_deliver_without_key_never_touches_the_network(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("network")))
    r = emails.deliver(email_type="send_2fa_code", payload=_payload())
    assert not r.ok and "RESEND_API_KEY" in r.error and calls == []


def test_stray_post_to_resend_trips(monkeypatch):
    with pytest.raises(RuntimeError, match="api.resend.com"):
        requests.post("https://api.resend.com/emails", json={})
    with pytest.raises(RuntimeError, match="api.twilio.com"):
        requests.post("https://api.twilio.com/2010-04-01/Accounts/x/Messages.json", data={})
    with pytest.raises(RuntimeError, match="api.stripe.com"):
        requests.Session().request("POST", "https://api.stripe.com/v1/prices")


def test_resend_sdk_is_blocked():
    resend = pytest.importorskip("resend")
    with pytest.raises(RuntimeError, match="Resend SDK"):
        resend.Emails.send({"from": "a@b.c", "to": ["d@e.f"], "subject": "x", "html": "y"})


def test_2fa_code_send_is_a_no_op_under_test():
    r = emails.send_2fa_code("owner@x.test", "Test Grill", "123456", "Pat")
    assert not r


def test_every_send_site_goes_through_a_guarded_path():
    """Static check: the only ways out are emails.deliver (requests) and the
    Resend SDK — both blocked above. A new raw urllib/httpx send would
    slip past the guard, so it fails here."""
    import glob
    import re
    offenders = []
    for path in glob.glob("*.py"):
        src = open(path, encoding="utf-8").read()
        # status_manager posts through requests too (guarded); anything else is new.
        if "api.resend.com" in src and path not in ("emails.py", "status_manager.py"):
            offenders.append(path)
        # Raw SDK sends — every one of these is blocked by conftest's
        # resend.Emails.send patch. A new file appearing here means a new
        # send path someone should route through emails.deliver instead.
        #
        # Matched on `.Emails.send`, not the literal "resend.Emails.send".
        # The old substring missed every ALIASED import, and notify.py —
        # the file that sends every review, health, labor and waste alert —
        # was exactly that: `import resend as _r` then `_r.Emails.send(...)`.
        # It sat outside this allowlist for the life of the alert system,
        # bypassing suppression, retry and email_log, and this test reported
        # green the whole time.
        #
        # The allowlist that used to sit here (eight modules, ~19 sends) is
        # gone: every one now goes through emails.deliver (MOD-EML-4).
        if re.search(r"\.Emails\.send\s*\(", src) and path != "emails.py":
            offenders.append(path + " (raw SDK send)")
    assert offenders == [], offenders


# ── production flood guard ───────────────────────────────────────────────────

class _Resp:
    status_code = 200
    text = '{"id":"m1"}'

    @staticmethod
    def json():
        return {"id": "m1"}


def _arm_real_send(monkeypatch):
    sent = []
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_fake")
    monkeypatch.setattr(requests, "post", lambda url, **k: sent.append(k.get("json")) or _Resp())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return sent


def test_flood_guard_caps_code_emails_per_recipient(monkeypatch):
    sent = _arm_real_send(monkeypatch)
    results = [emails.deliver(email_type="send_2fa_code", payload=_payload("erik@x.test")) for _ in range(12)]
    assert sum(1 for r in results if r.ok) == 8
    assert len(sent) == 8
    assert all("flood guard" in r.error for r in results[8:])
    # a different recipient is unaffected
    assert emails.deliver(email_type="send_2fa_code", payload=_payload("pat@x.test")).ok


def test_flood_guard_leaves_uncapped_types_alone(monkeypatch):
    sent = _arm_real_send(monkeypatch)
    for _ in range(12):
        assert emails.deliver(email_type="send_welcome_email", payload=_payload("erik@x.test")).ok
    assert len(sent) == 12


def test_flood_guard_skips_are_logged_not_lost():
    models.init_email_log()
    for _ in range(9):
        emails.deliver(email_type="send_2fa_code", payload=_payload("erik@x.test"))  # no key → all logged failed
    conn = models.get_conn()
    rows = conn.execute("SELECT status, error FROM email_log WHERE to_email='erik@x.test'").fetchall()
    conn.close()
    assert len(rows) == 9 and all(r["status"] == "failed" for r in rows)


def test_flood_guard_fails_open_when_log_unavailable(monkeypatch):
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert emails._flood_guard_ok("send_2fa_code", "erik@x.test") is True
