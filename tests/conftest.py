"""Shared fixtures: every test gets a real, throwaway SQLite database built by
the same init_db() the app uses, so schema drift is caught here instead of in
production."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# Security controls that would otherwise refuse or block in a test process:
# a pepper so staff PINs can be set, no live breach lookups, and the admin
# 2FA gate off except in the test that turns it on.
os.environ.setdefault("CAVNAR_PIN_PEPPER", "test-pepper")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("HIBP_DISABLED", "1")
os.environ.setdefault("ADMIN_REQUIRE_2FA", "0")
# The learning holdout (rec_learning.holdout_arm) is off in the suite, so a
# ranking test never lands on a held-out day by the calendar; its own tests
# turn it on with monkeypatch.setenv.
os.environ.setdefault("LEARNING_HOLDOUT_PCT", "0")
# Cavnar AI's own marketing is not sent without a CAN-SPAM postal address
# (emails.postal_address, #159). Set here as production must set it; the
# tests of the unset case delete it.
os.environ.setdefault("CAVNAR_POSTAL_ADDRESS", "100 Test St, Testville, IL 60000")

# The default database (models.DB_PATH, used by every call that passes no
# db_path — init_db's ensure_columns(), status_manager, lazily imported
# modules) must never be the developer's ./reviews.db. It was, and a full
# run grew that file by ~550 KB. Point the default at a throwaway volume
# before models is imported. The file is created empty first so
# adopt_legacy_db sees an existing database and does not copy the real
# reviews.db into it.
import tempfile as _tempfile
_TEST_VOLUME = _tempfile.mkdtemp(prefix="cavnar-test-volume-")
open(os.path.join(_TEST_VOLUME, "reviews.db"), "a").close()
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = _TEST_VOLUME

from models import init_db, ensure_columns, create_restaurant, save_reviews, Restaurant, Review


def _build_default_schema():
    """Give the throwaway default database the same schema hosted_dashboard
    builds at boot, so a call that passes no db_path meets real tables —
    as it did when the default was the developer's reviews.db, but now
    empty of anyone's data. Mirrors hosted_dashboard.py's module-level init."""
    import models
    from auth import init_auth
    from webhooks import init_webhooks
    from guest_marketing import init_guest_marketing
    from push import init_push
    from sales_audits import init_sales_audits
    import platform_monitor
    import provider_health
    init_db()
    init_auth()
    # F's telemetry tables and provider probe ledger, created at boot right
    # after init_auth (hosted_dashboard); the code tolerates their absence,
    # but a test on the default database should meet what production has.
    platform_monitor.init_platform_tables()
    provider_health.init_provider_health()
    models.init_staff_notes()
    models.init_staff_availability()
    ensure_columns()
    for fn in ("init_email_log", "init_onboarding_emails", "init_two_fa_backup_codes",
               "init_competitor_snapshots", "init_ai_visibility_queries", "init_staff_capabilities",
               "init_shift_profiles", "init_capability_changes", "init_ask_memory"):
        getattr(models, fn)()
    init_webhooks()
    init_guest_marketing()
    init_push()
    init_sales_audits()


_build_default_schema()


@pytest.fixture(autouse=True)
def _reset_ai_rate_limiter():
    """The AI rate limiter's window is keyed by restaurant_id, and every test
    gets a fresh database whose ids start at 1 — so one test's calls counted
    against the next test's budget and a route would 429 only when the whole
    file ran, never in isolation. Since fix round G the window lives in the
    database (ai_rate_events) with ai_utils._ai_call_log as its fallback,
    and the process also remembers breakers, budget totals and warning
    claims; ai_utils.reset_process_state clears all of it, in the default
    database too (a test that redirects nothing writes there). The default
    database's ledger is emptied as well, so Places and AI ceilings never
    trip on spend earlier tests left behind."""
    import ai_utils
    import models

    def _reset():
        ai_utils.reset_process_state(models.DB_PATH)
        try:
            conn = models.get_conn(models.DB_PATH)
            try:
                conn.execute("DELETE FROM ai_usage")
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass
    _reset()
    yield
    ai_utils.reset_process_state()


@pytest.fixture(autouse=True)
def _reset_labor_note_cache():
    """labor._NOTE_CACHE is keyed by restaurant id and data fingerprint; a
    fresh database per test reuses ids, so one test's stubbed note would be
    another's answer."""
    import labor
    labor._NOTE_CACHE.clear()
    yield
    labor._NOTE_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_tenant_names_cache():
    """models.other_tenant_names caches every restaurant's name per process
    (the Response Validation Layer's T1 list); one test's restaurants must
    not be another test's "other tenants"."""
    import models
    models._invalidate_tenant_names()
    yield
    models._invalidate_tenant_names()


@pytest.fixture(autouse=True)
def _reset_home_brief_cache():
    """home_brief caches its whole payload per restaurant_id for 60s, and
    every test's fresh database starts its ids at 1 — so the second Home
    test in a file was served the first one's brief. Harmless in
    production (the TTL is the point); a silent cross-test leak here."""
    import home_brief
    home_brief.invalidate()
    yield
    home_brief.invalidate()


@pytest.fixture(autouse=True)
def _reset_usage_schema_flag():
    """ai_utils remembers, per process, which databases it has already created
    the usage table in — so the DDL is off the hot path of every AI call.
    It is keyed by db_path, and a test that points at the default ./reviews.db
    would otherwise hand its "already done" to the next one and read a table
    that test had just recreated. Same shape as the three resets above."""
    import ai_utils
    ai_utils._usage_schema_ready.clear()
    yield
    ai_utils._usage_schema_ready.clear()


@pytest.fixture(autouse=True)
def _reset_ask_context_cache():
    """Identical hazard to _reset_home_brief_cache: ask_cavnar.build_context
    caches the assembled snapshot per restaurant_id for 60s, and every test's
    fresh database starts its ids at 1, so one test's context would be served
    to the next. The TTL is the point in production, where ids are unique."""
    import ask_cavnar
    ask_cavnar.invalidate_context()
    yield
    ask_cavnar.invalidate_context()


@pytest.fixture(autouse=True)
def _reset_admin_rate_limits():
    """security's per-session /admin ceiling is a process-global window, and
    tests that fake an admin without a session share one key ("user:<id>"),
    so a long run would 429 whichever admin test came after the 240th
    request in a minute. Same shape as the resets above."""
    import security
    security.reset_admin_rate_limits()
    yield
    security.reset_admin_rate_limits()


@pytest.fixture(autouse=True)
def _fixture_logins_skip_the_password_policy(monkeypatch):
    """auth.create_user holds every typed password to the password policy
    (8+ characters, not breached — fix round A, #86). The suite's fixtures
    make throwaway logins with passwords like "pw" in ~300 places; the
    policy's own tests (tests/test_fix_a_*.py) switch it back on with
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", True)."""
    import auth
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", False)
    yield


@pytest.fixture(autouse=True)
def _no_real_email_or_sms(monkeypatch):
    """The suite must never reach Resend, Twilio, Stripe, DocuSign, Anthropic,
    Google Places or Perplexity.

    It did: the full run sent ~75 real 2FA and notification emails to the
    tests' fake addresses through the production Resend key and exhausted
    the account's daily quota the night before a client meeting. Blank the
    keys so every send short-circuits, and trip loudly if anything still
    tries the network. Tests that exercise delivery stub `requests.post`
    and `emails._resend_key` themselves, which overrides this."""
    import requests
    import emails
    monkeypatch.setenv("RESEND_API_KEY", "")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(emails, "_resend_key", lambda: "")
    # Anthropic, Places and Perplexity too: the hourly provider probes
    # (provider_health.run_probes, a registered job the registry test runs)
    # call them whenever a key is in the environment — and scheduler.py
    # loads the checkout's .env at import.
    blocked = ("api.resend.com", "api.twilio.com", "api.stripe.com", "docusign.net", "docusign.com",
               "api.anthropic.com", "maps.googleapis.com", "places.googleapis.com", "api.perplexity.ai")
    real_post, real_request = requests.post, requests.request

    def guard(fn):
        def wrapped(url, *a, **k):
            if any(b in str(url) for b in blocked):
                raise RuntimeError("test tried to reach %s — stub it" % url)
            return fn(url, *a, **k)
        return wrapped
    monkeypatch.setattr(requests, "post", guard(real_post))
    monkeypatch.setattr(requests, "request", guard(real_request))
    # requests.Session.request is what the Resend SDK (and any Session user)
    # goes through — the module-level functions above never see it.
    real_session_request = requests.Session.request

    def session_guard(self, method, url, *a, **k):
        if any(b in str(url) for b in blocked):
            raise RuntimeError("test tried to reach %s — stub it" % url)
        return real_session_request(self, method, url, *a, **k)
    monkeypatch.setattr(requests.Session, "request", session_guard)
    # The Resend SDK's own entry points, in case a call site bypasses
    # emails.deliver (admin_routes and webhook_routes still use the SDK).
    try:
        import resend

        def sdk_blocked(*a, **k):
            raise RuntimeError("test tried to send through the Resend SDK — stub it")
        monkeypatch.setattr(resend.Emails, "send", staticmethod(sdk_blocked), raising=False)
        monkeypatch.setattr(resend, "api_key", "", raising=False)
    except Exception:
        pass
    yield


_DB_TEMPLATE = None


def _db_template():
    """One database per test process built by the real boot migrations —
    init_db() AND ensure_columns(), the two separate paths hosted_dashboard
    runs (skipping ensure_columns once meant tests had a schema production
    never has) — which every db_path copies. Building it per test cost
    ~230 ms each, most of a full run; a copy costs well under 1 ms and is
    byte-for-byte what the migrations produce."""
    global _DB_TEMPLATE
    if _DB_TEMPLATE is None:
        import sqlite3
        path = os.path.join(_tempfile.mkdtemp(prefix="cavnar-test-template-"), "template.db")
        init_db(db_path=path)
        ensure_columns(db_path=path)
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        _DB_TEMPLATE = path
    return _DB_TEMPLATE


@pytest.fixture
def db_path(tmp_path):
    import shutil
    path = str(tmp_path / "test_reviews.db")
    shutil.copyfile(_db_template(), path)
    return path


def stamp_scheduler_heartbeat(db_path, minutes_ago, loop_minutes_ago=None, running=None, running_minutes=None):
    """Make the scheduler loop's heartbeat `minutes_ago` old in `db_path`.

    It lives in its own table, scheduler_heartbeat (fix round D, #4), written
    only by the loop — the one source /health, the platform SLA check, the
    public status page and the console read. It used to be
    service_status.updated_at, which every status write reset; stamping that
    column now changes nothing, which is how a /health test and a console
    test kept passing against a heartbeat nobody read. `loop_minutes_ago` is
    the last COMPLETED tick (defaults to the beat); `running` /
    `running_minutes` put the loop inside a job for the watchdog (#121)."""
    import sqlite3
    loop = minutes_ago if loop_minutes_ago is None else loop_minutes_ago
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO scheduler_heartbeat (id) VALUES (1)")
        conn.execute("UPDATE scheduler_heartbeat SET beat_at=datetime('now', ?), loop_completed_at=datetime('now', ?), "
                     "running_job=?, running_since=CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END "
                     "WHERE id=1",
                     (f"-{float(minutes_ago)} minutes", f"-{float(loop)} minutes", running, running,
                      f"-{float(running_minutes or 0)} minutes"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def scheduler_heartbeat(db_path):
    """stamp_scheduler_heartbeat bound to this test's database:
    `scheduler_heartbeat(60)` is a scheduler that died an hour ago;
    pass db_path= to stamp another file."""
    def _stamp(minutes_ago, loop_minutes_ago=None, running=None, running_minutes=None, db_path=db_path):
        stamp_scheduler_heartbeat(db_path, minutes_ago, loop_minutes_ago=loop_minutes_ago, running=running,
                                  running_minutes=running_minutes)
    return _stamp


@pytest.fixture
def two_restaurants(db_path):
    """Two restaurants with one review each — the minimum world in which
    cross-tenant bugs (IDOR) are observable."""
    rid_a = create_restaurant(Restaurant(name="Alpha Cafe", owner_email="a@x.com"), db_path=db_path)
    rid_b = create_restaurant(Restaurant(name="Bravo Bistro", owner_email="b@x.com"), db_path=db_path)
    save_reviews([
        Review(restaurant_id=rid_a, platform="google", external_id="ext-a1",
               author="Ann", rating=2, text="Cold food and a long wait."),
        Review(restaurant_id=rid_b, platform="google", external_id="ext-b1",
               author="Bob", rating=5, text="Fantastic dinner, will be back."),
    ], db_path=db_path)
    return {"db_path": db_path, "rid_a": rid_a, "rid_b": rid_b}
