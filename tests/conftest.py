"""Shared fixtures: every test gets a real, throwaway SQLite database built by
the same init_db() the app uses, so schema drift is caught here instead of in
production."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from models import init_db, ensure_columns, create_restaurant, save_reviews, Restaurant, Review


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
