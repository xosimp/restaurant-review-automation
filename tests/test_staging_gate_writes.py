"""The staging send gate covers every write to a third party on a customer's
behalf, and a refused send leaves a trace (blind re-audit 10/27/26: B5 "the
staging gate misses POS writes and social posts", B8 "staging's 'sends
nothing real' claim does not cover Meta posting", B4 "one environment name
now silences every send, and a refused send leaves no trace").

config.outbound_sends_allowed() already held email, texts, push, Stripe,
DocuSign, Google replies and posts, and webhooks. Now also: the RPOWER and
Toast schedule pushes (a live store's POS), every Meta Graph write (an
Instagram container or post, a Facebook photo or feed post), retracting a
Google reply and editing the Google listing. Every refusal goes through
config.refused_send(channel): a per-process counter and a log line, on
/health and the console. On Railway, an environment that is not named
production while CAVNAR_ENV does not say so is a /health problem and an
ERROR at boot."""
import logging
import types

import pytest
import requests

import config

# The deploy gate's contract slice (TESTING.md, Tiers): a few seconds, and what it
# guards reaches real people (a send) or every installed phone (a path).
pytestmark = pytest.mark.contract

_ENV_VARS = ("CAVNAR_ENV", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID",
             "RAILWAY_SERVICE_ID", "ALLOW_LOCAL_SCHEDULER", "ALLOW_NONPROD_SENDS", "RESTORE_FROM",
             "MAINTENANCE_READ_ONLY")


@pytest.fixture
def env(monkeypatch):
    for v in _ENV_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(config, "_REFUSED_SENDS", {})

    def set_env(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
    return set_env


def _staging(env):
    env(RAILWAY_ENVIRONMENT_NAME="staging", RAILWAY_ENVIRONMENT="staging", RAILWAY_PROJECT_ID="p1",
        CAVNAR_ENV="staging")


class _Resp:
    status_code = 200
    ok = True
    text = '{"id": "1"}'

    def json(self):
        return {"id": "1", "access_token": "t"}


@pytest.fixture
def wire(monkeypatch):
    """Every outbound write recorded, never sent."""
    calls = []

    def record(verb):
        def fn(url, *a, **kw):
            calls.append((verb, url))
            return _Resp()
        return fn
    for verb in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(requests, verb, record(verb))

    class _Session:
        def post(self, url, *a, **kw):
            calls.append(("post", url))
            return _Resp()
    import rpower
    monkeypatch.setattr(rpower, "_session", lambda: _Session())
    return calls


SHIFTS = [{"employee": "Kayla Brennan", "role": "Server", "start": "2026-11-02T16:00", "end": "2026-11-02T22:00"}]


def test_the_rpower_schedule_push_is_refused_in_staging(env, wire):
    import rpower
    _staging(env)
    res = rpower.push_labor_schedule(987654, SHIFTS)
    assert res["ok"] is False and "staging" in res["error"] and wire == []
    assert config.refused_sends()["rpower_schedule"]["count"] == 1


def test_the_toast_schedule_push_is_refused_in_staging(env, wire, monkeypatch):
    import toast
    monkeypatch.setenv(toast.SCHEDULE_WRITE_ENV, "1")
    _staging(env)
    res = toast.push_labor_schedule(987654, SHIFTS)
    assert res["ok"] is False and "staging" in res["error"] and wire == []
    assert config.refused_sends()["toast_schedule"]["count"] == 1


def test_every_meta_graph_write_is_refused_in_staging_and_nothing_reads_as_maybe_posted(env, wire):
    import social_routes
    from meta_api import graph_url
    _staging(env)
    for path in ("1784/media", "1784/media_publish", "991/photos", "991/feed"):
        answer = social_routes._graph("post", graph_url(path), data={"access_token": "page-token"})
        assert not answer.ok and not answer.ambiguous, path      # a definite no: the publish claim is given back
        assert "staging" in answer.error.get("message", "")
    assert wire == []
    assert config.refused_sends()["meta_post"]["count"] == 4
    # The OAuth token exchange is a read, not a post: it still goes out.
    social_routes._graph("post", graph_url("oauth/access_token"), data={"code": "x"})
    assert wire == [("post", graph_url("oauth/access_token"))]


def test_a_meta_post_goes_out_in_production(env, wire):
    import social_routes
    from meta_api import graph_url
    env(RAILWAY_ENVIRONMENT_NAME="production", RAILWAY_PROJECT_ID="p1")
    answer = social_routes._graph("post", graph_url("991/feed"), data={"message": "hi"})
    assert answer.ok and wire == [("post", graph_url("991/feed"))]
    assert config.refused_sends() == {}


def test_google_reply_retraction_and_listing_edits_are_refused_in_staging(env, wire, monkeypatch):
    import gmb
    import models
    monkeypatch.setattr(gmb, "get_valid_token", lambda rid: "tok")
    monkeypatch.setattr(models, "get_restaurant", lambda rid, *a, **k: types.SimpleNamespace(
        gmb_refresh_token="r", gmb_location_id="locations/1", gmb_account_id="accounts/1"))
    _staging(env)
    assert "staging" in gmb.delete_reply(5, "accounts/1/locations/1/reviews/9")["error"]
    assert "staging" in gmb.update_gbp_listing(5, {"phone": "312-555-0100"})["error"]
    assert "staging" in gmb.post_reply(5, "accounts/1/locations/1/reviews/9", "Thanks!")["error"]
    assert wire == []
    assert set(config.refused_sends()) == {"google_reply_delete", "google_listing", "google_reply"}


# ── the trace ────────────────────────────────────────────────────────────────

def test_a_refused_send_is_counted_and_logged(env, caplog):
    _staging(env)
    with caplog.at_level(logging.WARNING, logger="config"):
        for _ in range(3):
            reason = config.refused_send("email")
    assert "staging" in reason
    got = config.refused_sends()["email"]
    assert got["count"] == 3 and got["first_at"] and got["last_at"]
    # Logged on the first refusal (and every hundredth), not every one.
    assert sum("send refused (email" in r.getMessage() for r in caplog.records) == 1


def test_the_existing_send_points_record_their_refusals(env, monkeypatch, db_path):
    import emails
    import notify
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    monkeypatch.setattr(notify, "TWILIO_SID", "AC1")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "t")
    _staging(env)
    res = emails.deliver({"from": "a@cavnar.ai", "to": ["o@example.com"], "subject": "s", "html": "x"},
                         email_type="password_reset")
    assert res.reason == "env_refused"
    assert config.refused_sends()["email"]["count"] == 1


# ── a misnamed environment is visible ───────────────────────────────────────

def test_environment_problem_only_for_an_unacknowledged_non_production_name(env):
    # One check (config.environment_name_problem): a production-looking
    # service whose name stands it down. A staging with no production sign
    # is not a problem; one that looks like production must say so.
    assert config.environment_problem() == ""                       # a laptop
    env(RAILWAY_ENVIRONMENT_NAME="production", RAILWAY_PROJECT_ID="p1", CAVNAR_PRODUCTION_SERVICE="1")
    assert config.environment_problem() == ""
    env(RAILWAY_ENVIRONMENT_NAME="prod")                            # renamed, or mistyped
    assert config.environment_problem() == "not_production_env"
    env(CAVNAR_ENV="staging")                                       # said on purpose
    assert config.environment_problem() == ""


def test_the_boot_logs_an_error_for_a_misnamed_environment(env, caplog):
    env(RAILWAY_ENVIRONMENT_NAME="prod", RAILWAY_PROJECT_ID="p1", CAVNAR_PRODUCTION_SERVICE="1")
    log = logging.getLogger("test-boot")
    with caplog.at_level(logging.WARNING, logger="test-boot"):
        state = config.report_environment_at_boot(log)
    assert state["problem"] == "not_production_env" and state["outbound_sends_allowed"] is False
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "'prod'" in errors[0].getMessage() and "CAVNAR_ENV=production" in errors[0].getMessage()
    caplog.clear()
    env(RAILWAY_ENVIRONMENT_NAME="production")
    with caplog.at_level(logging.WARNING, logger="test-boot"):
        assert config.report_environment_at_boot(log)["problem"] == ""
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_health_shows_the_misnamed_environment_and_the_refused_sends(env, db_path, monkeypatch):
    import status_manager
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    env(RAILWAY_ENVIRONMENT_NAME="prod", RAILWAY_PROJECT_ID="p1", CAVNAR_PRODUCTION_SERVICE="1")
    config.refused_send("sms")
    payload, status = status_manager.health_snapshot(db_path)
    assert status == 200 and payload["status"] == "degraded"
    assert "not_production_env" in payload["problems"]
    assert payload["sends_refused"] == {"sms": 1}
    env(CAVNAR_ENV="staging")
    payload, _ = status_manager.health_snapshot(db_path)
    assert "not_production_env" not in payload["problems"]


def test_health_notes_an_unpinned_stripe_api_version(env, db_path, monkeypatch):
    import status_manager
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("STRIPE_API_VERSION", raising=False)
    payload, _ = status_manager.health_snapshot(db_path)
    assert payload.get("notes") == ["stripe_api_version_unset"]
    assert "stripe_api_version_unset" not in payload["problems"], "a note, never a degraded platform"
    monkeypatch.setenv("STRIPE_API_VERSION", "2025-02-24.acacia")
    payload, _ = status_manager.health_snapshot(db_path)
    assert "notes" not in payload


def test_the_console_warns_about_each(env, monkeypatch):
    import platform_monitor
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.delenv("STRIPE_API_VERSION", raising=False)
    w = platform_monitor.system_warnings({"deployment": {"env": "prod", "problem": "not_production_env",
                                                         "outbound_sends_allowed": False,
                                                         "sends_refused": {"email": {"count": 2}},
                                                         "stripe_api_version": None}})
    assert any("'prod', not production" in x for x in w)
    assert any("2 sends were refused" in x and "email" in x for x in w)
    assert any("STRIPE_API_VERSION is not set" in x for x in w)
