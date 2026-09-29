"""Workstream F — provider probes (#29).

Each probe is driven through provider_health._get, which the tests replace:
nothing here reaches a provider.
"""
import pytest

import models
import provider_health as ph


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _fake(monkeypatch, responses):
    """responses: {url_fragment: _Resp or Exception}; records calls."""
    calls = []

    def fake_get(url, **kw):
        calls.append((url, kw))
        for frag, resp in responses.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected probe URL {url}")
    monkeypatch.setattr(ph, "_get", fake_get)
    return calls


@pytest.fixture
def db(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ph.init_provider_health(db_path)
    return db_path


# ── Resend ───────────────────────────────────────────────────────────────────

def test_resend_states(monkeypatch, db):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    _fake(monkeypatch, {"resend.com": _Resp(200, {"data": []})})
    assert ph.probe_resend(db)["state"] == "ok"
    # Resend answers a valid SENDING-ONLY key with 401 restricted_api_key:
    # that proves the key. The old check read a 401 as "key invalid".
    _fake(monkeypatch, {"resend.com": _Resp(401, {"name": "restricted_api_key"})})
    assert ph.probe_resend(db)["state"] == "ok"
    # ...and an invalid key with 403 invalid_api_key, which it read as fine.
    _fake(monkeypatch, {"resend.com": _Resp(403, {"name": "invalid_api_key"})})
    assert ph.probe_resend(db)["state"] == "failing"
    _fake(monkeypatch, {"resend.com": _Resp(429, {"name": "daily_quota_exceeded"})})
    r = ph.probe_resend(db)
    assert r["state"] == "failing" and "quota" in r["detail"]
    _fake(monkeypatch, {"resend.com": TimeoutError("slow")})
    assert ph.probe_resend(db)["state"] == "error"
    monkeypatch.setenv("RESEND_API_KEY", "")
    assert ph.probe_resend(db)["state"] == "unconfigured"


def test_a_resend_quota_refusing_sends_reads_failing_from_the_email_log(monkeypatch, db):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    _fake(monkeypatch, {"resend.com": _Resp(200, {"data": []})})
    conn = models.get_conn(db)
    conn.execute("INSERT INTO email_log (email_type, to_email, status) VALUES ('digest','a@x.test','sent')")
    conn.execute("INSERT INTO email_log (email_type, to_email, status, error) VALUES "
                 "('digest','b@x.test','failed','429 daily_quota_exceeded: You have reached your daily email sending quota')")
    conn.commit()
    r = ph.probe_resend(db)
    assert r["state"] == "failing" and "quota" in r["detail"]
    conn.execute("INSERT INTO email_log (email_type, to_email, status) VALUES ('digest','c@x.test','sent')")
    conn.commit()
    conn.close()
    assert ph.probe_resend(db)["state"] == "ok", "a send that went out after the refusal clears it"


# ── the others ───────────────────────────────────────────────────────────────

def test_twilio_states(monkeypatch, db):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    calls = _fake(monkeypatch, {"twilio.com": _Resp(200, {"status": "active"})})
    assert ph.probe_twilio(db)["state"] == "ok"
    assert calls[0][1]["auth"] == ("AC123", "tok")
    _fake(monkeypatch, {"twilio.com": _Resp(200, {"status": "suspended"})})
    r = ph.probe_twilio(db)
    assert r["state"] == "failing" and "suspended" in r["detail"]
    _fake(monkeypatch, {"twilio.com": _Resp(401, {"code": 20003})})
    assert ph.probe_twilio(db)["state"] == "failing"
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    assert ph.probe_twilio(db)["state"] == "unconfigured"


def test_anthropic_states_including_an_empty_credit_balance(monkeypatch, db):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _fake(monkeypatch, {"anthropic.com": _Resp(200, {"data": []})})
    assert ph.probe_anthropic(db)["state"] == "ok"
    assert calls[0][1]["headers"]["x-api-key"] == "sk-ant-test"
    _fake(monkeypatch, {"anthropic.com": _Resp(401, {"error": {"type": "authentication_error"}})})
    assert ph.probe_anthropic(db)["state"] == "failing"
    _fake(monkeypatch, {"anthropic.com": _Resp(529, {"error": {"type": "overloaded_error"}})})
    assert ph.probe_anthropic(db)["state"] == "error"
    # /v1/models authenticates without spending, so an empty balance shows
    # only in the calls that were refused.
    conn = models.get_conn(db)
    conn.execute("CREATE TABLE IF NOT EXISTS ai_usage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "status TEXT DEFAULT 'ok', error TEXT)")
    conn.execute("INSERT INTO ai_usage (status, error) VALUES ('ok', NULL)")
    conn.execute("INSERT INTO ai_usage (status, error) VALUES ('error', "
                 "'400 invalid_request_error: Your credit balance is too low to access the Anthropic API.')")
    conn.commit()
    conn.close()
    _fake(monkeypatch, {"anthropic.com": _Resp(200, {"data": []})})
    r = ph.probe_anthropic(db)
    assert r["state"] == "failing" and "credit" in r["detail"]


def test_stripe_states(monkeypatch, db):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
    _fake(monkeypatch, {"stripe.com": _Resp(200, {"available": []})})
    assert ph.probe_stripe(db)["state"] == "ok"
    _fake(monkeypatch, {"stripe.com": _Resp(403, {"error": {}})})
    assert ph.probe_stripe(db)["state"] == "ok", "a restricted key still authenticates"
    _fake(monkeypatch, {"stripe.com": _Resp(401, {"error": {}})})
    assert ph.probe_stripe(db)["state"] == "failing"


def test_places_states_and_the_request_asks_only_for_the_place_id(monkeypatch, db):
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "AIzaTEST")
    calls = _fake(monkeypatch, {"maps.googleapis.com": _Resp(200, {"status": "OK"})})
    assert ph.probe_google_places(db)["state"] == "ok"
    assert calls[0][1]["params"]["fields"] == "place_id", "the no-charge ID-refresh request"
    _fake(monkeypatch, {"maps.googleapis.com": _Resp(200, {"status": "NOT_FOUND"})})
    assert ph.probe_google_places(db)["state"] == "ok", "a retired place id still proves the key"
    _fake(monkeypatch, {"maps.googleapis.com": _Resp(200, {"status": "REQUEST_DENIED",
                                                           "error_message": "The provided API key is invalid."})})
    r = ph.probe_google_places(db)
    assert r["state"] == "failing" and "REQUEST_DENIED" in r["detail"]


def test_apns_mints_a_token_or_says_why_not(monkeypatch, db):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    monkeypatch.setenv("APNS_KEY_ID", "KEY123")
    monkeypatch.setenv("APNS_TEAM_ID", "TEAM123")
    monkeypatch.setenv("APNS_PRIVATE_KEY", pem)
    assert ph.probe_apns(db)["state"] == "ok"
    masked = "-----BEGIN PRIVATE KEY-----\n" + "•" * 60 + "\n-----END PRIVATE KEY-----"
    monkeypatch.setenv("APNS_PRIVATE_KEY", masked)
    r = ph.probe_apns(db)
    assert r["state"] == "failing" and "masked" in r["detail"]
    monkeypatch.delenv("APNS_PRIVATE_KEY")
    assert ph.probe_apns(db)["state"] == "unconfigured"


# ── the ledger and the page ─────────────────────────────────────────────────

def _only(monkeypatch, name, result_seq):
    """Probe only `name`, answering from result_seq in order."""
    seq = iter(result_seq)
    monkeypatch.setattr(ph, "PROVIDERS", (name,))
    monkeypatch.setitem(ph.PROBES, name, lambda db_path=None: next(seq))


def test_run_probes_records_and_pages_once_when_a_provider_starts_failing(monkeypatch, db):
    import scheduler
    import ops
    pages = []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: pages.append(lines) or True)
    ok = {"state": "ok", "detail": None, "http_status": 200, "latency_ms": 5}
    bad = {"state": "failing", "detail": "key rejected (invalid_api_key)", "http_status": 403, "latency_ms": 5}
    _only(monkeypatch, "resend", [ok, bad, bad, ok])
    out = ph.run_probes(db)
    assert out == {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "results": {"resend": "ok"}}
    assert pages == []
    assert ph.run_probes(db)["failed"] == 1
    assert len(pages) == 1 and "resend" in pages[0][0] and "was ok" in pages[0][0]
    ph.run_probes(db)
    assert len(pages) == 1, "still failing is not a new page"
    ph.run_probes(db)
    latest = ph.latest(db)["resend"]
    assert latest["state"] == "ok" and latest["last_ok_at"] and latest["streak"] == 1


def test_an_unreachable_provider_pages_after_three_probes_running(monkeypatch, db):
    import scheduler
    import ops
    pages = []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: pages.append(lines) or True)
    err = {"state": "error", "detail": "unreachable: Timeout", "http_status": None, "latency_ms": 6000}
    _only(monkeypatch, "twilio", [err] * 5)
    for _ in range(2):
        ph.run_probes(db)
    assert pages == []
    ph.run_probes(db)
    assert len(pages) == 1
    ph.run_probes(db)
    ph.run_probes(db)
    assert len(pages) == 1


def test_a_laptop_never_pages(monkeypatch, db):
    import scheduler
    import ops
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    monkeypatch.setattr(ops, "alert_will", lambda *a: pytest.fail("paged from a laptop"))
    _only(monkeypatch, "stripe", [{"state": "failing", "detail": "key rejected", "http_status": 401,
                                   "latency_ms": 3}])
    assert ph.run_probes(db)["failed"] == 1


def test_unconfigured_and_skipped_providers_are_not_counted_as_failures(monkeypatch, db):
    monkeypatch.setenv("PROVIDER_PROBES_SKIP", "google_places")
    for var in ("RESEND_API_KEY", "TWILIO_ACCOUNT_SID", "ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY",
                "GOOGLE_PLACES_API_KEY", "GOOGLE_API_KEY", "APNS_KEY_ID"):
        monkeypatch.setenv(var, "")
    monkeypatch.setattr(ph, "_get", lambda *a, **k: pytest.fail("probed an unconfigured provider"))
    out = ph.run_probes(db, alert=False)
    assert out["attempted"] == 0 and out["failed"] == 0 and out["skipped"] == 6
    assert "google_places" not in out["results"]
