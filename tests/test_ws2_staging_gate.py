"""Release-safety fix round 10/26/26, WS2 — the staging send gate (A05-06, R8).

config.deploy_env() names the deployment (CAVNAR_ENV, else Railway's own
environment name, else "local"); config.scheduling_allowed() runs the
scheduler only in production; config.outbound_sends_allowed() holds every
send point in any other named environment. Production — Railway's
environment named "production", no new variable — keeps scheduling and
sending exactly as before."""
import pytest
import requests

import config

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

_ENV_VARS = ("CAVNAR_ENV", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID",
             "RAILWAY_SERVICE_ID", "ALLOW_LOCAL_SCHEDULER", "ALLOW_NONPROD_SENDS", "RESTORE_FROM",
             "MAINTENANCE_READ_ONLY")


@pytest.fixture
def env(monkeypatch):
    for v in _ENV_VARS:
        monkeypatch.delenv(v, raising=False)

    def set_env(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
    return set_env


def _production(env):
    env(RAILWAY_ENVIRONMENT_NAME="production", RAILWAY_ENVIRONMENT="production", RAILWAY_PROJECT_ID="p1")


def _staging(env):
    env(RAILWAY_ENVIRONMENT_NAME="staging", RAILWAY_ENVIRONMENT="staging", RAILWAY_PROJECT_ID="p1")


# ── config ───────────────────────────────────────────────────────────────────

def test_deploy_env_reads_cavnar_env_then_railways_name_then_local(env):
    assert config.deploy_env() == "local"
    env(RAILWAY_ENVIRONMENT="Production")
    assert config.deploy_env() == "production"
    env(RAILWAY_ENVIRONMENT_NAME="staging")
    assert config.deploy_env() == "staging"
    env(CAVNAR_ENV="preview")
    assert config.deploy_env() == "preview"


def test_production_with_no_new_variable_schedules_and_sends(env):
    _production(env)
    assert config.deploy_env() == "production" and config.is_production()
    assert config.scheduling_allowed() is True
    assert config.outbound_sends_allowed() is True


def test_a_railway_process_that_names_no_environment_keeps_todays_answer(env):
    env(RAILWAY_PROJECT_ID="p1")
    assert config.scheduling_allowed() is True
    assert config.outbound_sends_allowed() is True


def test_staging_never_schedules_or_sends_unless_told(env):
    _staging(env)
    assert config.scheduling_allowed() is False
    assert config.outbound_sends_allowed() is False
    env(ALLOW_LOCAL_SCHEDULER="1")
    assert config.scheduling_allowed() is True
    assert config.outbound_sends_allowed() is False, "the scheduler override is not a send override"
    env(ALLOW_NONPROD_SENDS="1")
    assert config.outbound_sends_allowed() is True


def test_cavnar_env_staging_wins_over_a_production_railway_name(env):
    """A staging made by duplicating production's environment inside another
    project still says production to Railway: CAVNAR_ENV is the override."""
    _production(env)
    env(CAVNAR_ENV="staging")
    assert config.scheduling_allowed() is False
    assert config.outbound_sends_allowed() is False


def test_a_laptop_keeps_its_deliberate_sends_and_no_scheduler(env):
    assert config.deploy_env() == "local"
    assert config.outbound_sends_allowed() is True
    assert config.scheduling_allowed() is False


def test_build_label_is_never_empty(env, monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    assert config.build_label() == "unknown"
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc4d51f0123456789")
    assert config.build_label() == "abc4d51f0123"


# ── the send points ──────────────────────────────────────────────────────────

class _Resp:
    status_code = 200
    ok = True
    text = ""
    content = b"{}"

    def json(self):
        return {"id": "m1", "sid": "SM1", "status": "queued"}


@pytest.fixture
def wire(monkeypatch):
    """Every outbound POST recorded, never sent."""
    calls = []

    def post(url, *a, **kw):
        calls.append(url)
        r = _Resp()
        if "twilio" in url:
            r.status_code = 201
        return r
    monkeypatch.setattr(requests, "post", post)
    return calls


def test_email_refused_in_staging_and_sent_in_production(env, wire, monkeypatch):
    import emails
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    payload = {"from": "a@cavnar.ai", "to": ["owner@example.com"], "subject": "s", "html": "<p>x</p>"}
    _staging(env)
    res = emails.deliver(dict(payload), email_type="password_reset")
    assert not res.ok and res.reason == "env_refused" and res.refused and wire == []
    assert "staging" in res.error
    env(CAVNAR_ENV="production")
    res = emails.deliver(dict(payload), email_type="password_reset")
    assert res.ok and len(wire) == 1 and "api.resend.com" in wire[0]


def test_sms_refused_in_staging_and_sent_in_production(env, wire, monkeypatch, db_path):
    import notify
    monkeypatch.setattr(notify, "TWILIO_SID", "AC1")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "t")
    monkeypatch.setattr(notify, "TWILIO_FROM", "+13125550100")
    monkeypatch.setattr(notify, "TWILIO_MESSAGING_SERVICE_SID", "MG1")
    _staging(env)
    res = notify.send_sms_result("+13125550111", "hello", use_case="otp", db_path=db_path)
    assert not res.ok and res.permanent and "staging" in (res.error or "") and wire == []
    env(CAVNAR_ENV="production")
    res = notify.send_sms_result("+13125550111", "hello", use_case="otp", db_path=db_path)
    assert res.ok and len(wire) == 1 and "api.twilio.com" in wire[0]


def test_push_refused_in_staging(env, monkeypatch):
    import push

    def boom(*a, **k):
        raise AssertionError("APNs reached from staging")
    monkeypatch.setattr(push, "_client", boom)
    _staging(env)
    row = {"id": 1, "restaurant_id": None, "apns_token": "a" * 64, "environment": "production"}
    out = push._deliver(row, "health", "t", "b", {})
    assert out["ok"] is False and "staging" in out["error"]
    ok, status, reason, _env = push._apns_post("a" * 64, "production", {"aps": {}}, "background", 5, "ai.cavnar")
    assert ok is False and status == 0 and "staging" in reason


def test_web_push_refused_in_staging(env, monkeypatch):
    import web_push
    monkeypatch.setattr(web_push, "enabled", lambda: True)
    _staging(env)
    out = web_push.send({"endpoint": "https://fcm.googleapis.com/x", "p256dh": "k", "auth": "a"}, {"t": 1})
    assert out["ok"] is False and out["gone"] is False and "staging" in out["error"]


def test_stripe_live_key_refused_outside_production_test_key_allowed(env):
    import stripe
    before = stripe.api_key
    try:
        _staging(env)
        with pytest.raises(RuntimeError, match="live key is refused"):
            config.stripe_api("sk_live_abc")
        assert stripe.api_key == ""
        assert config.stripe_api("sk_test_abc").api_key == "sk_test_abc"
        env(CAVNAR_ENV="production")
        assert config.stripe_api("sk_live_abc").api_key == "sk_live_abc"
    finally:
        stripe.api_key = before


def test_docusign_live_account_refused_in_staging_demo_allowed(env, monkeypatch):
    import docusign_helper
    _staging(env)
    monkeypatch.setattr(docusign_helper, "BASE_URL", "https://na4.docusign.net")
    with pytest.raises(docusign_helper.DocuSignRefused):
        docusign_helper._send_contract("o@example.com", "O", "R", 3, "Reviews")
    with pytest.raises(docusign_helper.DocuSignRefused):
        docusign_helper.void_envelope("env-1", "closed")
    monkeypatch.setattr(docusign_helper, "BASE_URL", "https://demo.docusign.net")
    docusign_helper._refuse_outside_production()          # the sandbox: allowed
    monkeypatch.setattr(docusign_helper, "BASE_URL", "https://na4.docusign.net")
    env(CAVNAR_ENV="production")
    docusign_helper._refuse_outside_production()          # production: allowed


def test_public_review_reply_refused_in_staging(env, monkeypatch):
    import gmb

    def boom(*a, **k):
        raise AssertionError("Google reached from staging")
    monkeypatch.setattr(gmb, "get_valid_token", boom)
    _staging(env)
    out = gmb.post_reply(1, "accounts/1/locations/2/reviews/3", "Thanks!")
    assert out["ok"] is False and "staging" in out["error"]


def test_customer_webhook_refused_in_staging(env, monkeypatch):
    import net_safety
    import webhooks

    def boom(*a, **k):
        raise AssertionError("a customer endpoint reached from staging")
    monkeypatch.setattr(net_safety, "safe_post", boom)
    _staging(env)
    out = webhooks._deliver({"id": 1, "restaurant_id": 1, "url": "https://hooks.example.com/x", "secret": "s"},
                            "review.new", {})
    assert out["ok"] is False and out["attempts"] == 0


def test_billing_mail_held_in_staging_even_with_the_scheduler_override(env):
    import billing_jobs
    _staging(env)
    env(ALLOW_LOCAL_SCHEDULER="1")
    assert billing_jobs._sending_allowed() is False
    env(CAVNAR_ENV="production")
    assert billing_jobs._sending_allowed() is True
