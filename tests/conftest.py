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
    init_db()
    init_auth()
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
    """ai_utils._ai_call_log is a process-global sliding window keyed by
    restaurant_id, and every test gets a fresh database whose ids start at 1 —
    so one test's calls counted against the next test's budget and a route
    would 429 only when the whole file ran, never in isolation. Cleared
    between tests so a rate limit is something a test asks for on purpose."""
    import ai_utils
    ai_utils._ai_call_log.clear()
    yield
    ai_utils._ai_call_log.clear()


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
def _reset_supplier_order_cooldown():
    """Same shape of problem as _reset_ai_rate_limiter above: the
    supplier-order send cooldown is a process-global keyed by restaurant_id,
    and every test's fresh database starts its ids at 1 — so the first test to
    send an order would 429 the next one."""
    import client_api
    client_api._order_send_last.clear()
    yield
    client_api._order_send_last.clear()


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
def _no_real_email_or_sms(monkeypatch):
    """The suite must never reach Resend, Twilio, Stripe or DocuSign.

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
    blocked = ("api.resend.com", "api.twilio.com", "api.stripe.com", "docusign.net", "docusign.com")
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


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_reviews.db")
    init_db(db_path=path)
    # Real app boot (hosted_dashboard.py) calls both init_db() AND
    # ensure_columns() — they're two separate migration paths (the latter
    # covers columns like alert_quiet_start/alert_max_per_day). Skipping
    # ensure_columns() here meant tests had a schema real production never has.
    ensure_columns(db_path=path)
    return path


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
