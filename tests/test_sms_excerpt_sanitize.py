"""_sms_safe_excerpt() (notify.py) and its wiring into fire_review_alerts().

A review's own wording is unmoderated text embedded straight into a
regulated A2P 10DLC SMS — a URL, a phone number, or unusual characters in
a customer's review is a known carrier spam-filter risk factor the app
never asked for. Email/push keep the full quote (they aren't subject to
carrier content filtering); SMS gets a shorter, narrower excerpt instead.
"""
import pytest

import notify
from models import Restaurant, Review, create_restaurant


# ── the sanitizer itself ──────────────────────────────────────────────────

def test_strips_urls():
    cleaned, _ = notify._sms_safe_excerpt("Check out http://spam.example/promo for a deal")
    assert "http" not in cleaned and "spam.example" not in cleaned


def test_strips_www_urls_without_a_scheme():
    cleaned, _ = notify._sms_safe_excerpt("visit www.example.com now")
    assert "www.example.com" not in cleaned


def test_strips_phone_number_shaped_runs():
    cleaned, _ = notify._sms_safe_excerpt("call the owner at 555-123-4567 about this")
    assert "555" not in cleaned and "4567" not in cleaned


def test_keeps_short_numbers_like_a_star_count_or_minutes():
    """Phone-number stripping targets 7+ digit runs — an ordinary '45
    minutes' or '3 stars' in a review must survive."""
    cleaned, _ = notify._sms_safe_excerpt("waited 45 minutes for a table, only 3 stars")
    assert "45" in cleaned and "3" in cleaned


def test_drops_emoji_and_other_symbols():
    cleaned, _ = notify._sms_safe_excerpt("terrible service \U0001F621\U0001F4A9 never again")
    assert "\U0001F621" not in cleaned and "\U0001F4A9" not in cleaned
    assert "terrible service" in cleaned


def test_keeps_ordinary_punctuation_and_apostrophes():
    cleaned, _ = notify._sms_safe_excerpt("Wasn't happy, food was cold.")
    assert cleaned == "Wasn't happy, food was cold."


def test_truncates_shorter_than_the_email_preview():
    long_text = "a" * 200
    cleaned, ellipsis = notify._sms_safe_excerpt(long_text)
    assert len(cleaned) <= 60
    assert ellipsis == "…"


def test_no_ellipsis_when_text_fits():
    cleaned, ellipsis = notify._sms_safe_excerpt("short review")
    assert ellipsis == ""


def test_empty_text_does_not_crash():
    assert notify._sms_safe_excerpt("") == ("", "")
    assert notify._sms_safe_excerpt(None) == ("", "")


# ── wired into the actual alert SMS bodies, not just defined ─────────────

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(notify, "_resend_key", lambda: "")


def _restaurant(db_path, **kw):
    # create_restaurant()'s INSERT predates the al_*/urgent_via_* columns —
    # they only get whatever the ALTER TABLE's own DEFAULT is (0) no matter
    # what's passed to the Restaurant dataclass at creation. update_restaurant()
    # does have them in its whitelist, so flip the toggles that way instead.
    from models import update_restaurant
    rid = create_restaurant(
        Restaurant(name=kw.pop("name", "Sanitize Co"), owner_email="s@x.com", **kw), db_path=db_path
    )
    update_restaurant(rid, {
        "urgent_via_sms": 1, "al_1star_sms": 1,
        "urgent_via_email": 1, "al_1star_email": 1,
    }, db_path=db_path)
    return rid


def _review(rid, **kw):
    return Review(
        restaurant_id=rid, platform="google", external_id=kw.pop("external_id", "r1"),
        author="Ann", rating=kw.pop("rating", 1), text=kw.pop("text", "Bad experience."),
        id=1, **kw,
    )


def test_1star_sms_body_uses_the_sanitized_excerpt_not_the_raw_review(db_path, monkeypatch):
    sent = {}
    monkeypatch.setattr(notify, "send_sms", lambda phone, msg, use_case="alert": sent.setdefault("msg", msg) or True)
    monkeypatch.setattr(notify, "get_alert_contacts", lambda *a, **k: [{"phone": "+15551234567"}])
    monkeypatch.setattr(notify, "alert_recipients", lambda *a, **k: [])

    rid = _restaurant(db_path)
    text = "Terrible! Call me at 555-987-6543 or see http://scam.example for proof."
    review = _review(rid, rating=1, text=text)
    notify.fire_review_alerts(rid, "Sanitize Co", [review], db_path=db_path)

    assert "msg" in sent, "expected the 1-star SMS to fire"
    assert "555-987-6543" not in sent["msg"]
    assert "scam.example" not in sent["msg"]
    assert "Terrible" in sent["msg"]


def test_email_html_still_carries_the_full_unsanitized_quote(db_path, monkeypatch):
    """Only the SMS channel narrows the excerpt — email isn't carrier
    content-filtered the way A2P SMS is, so it keeps the full quote."""
    captured = {}
    monkeypatch.setattr(notify, "_send_alert_email",
                         lambda owner_email, subject, html, restaurant_id=None: captured.setdefault("html", html) or True)
    monkeypatch.setattr(notify, "get_alert_contacts", lambda *a, **k: [])
    monkeypatch.setattr(notify, "alert_recipients", lambda *a, **k: ["owner@x.com"])

    rid = _restaurant(db_path)
    text = "Call 555-987-6543 about this — terrible!"
    review = _review(rid, rating=1, text=text)
    notify.fire_review_alerts(rid, "Sanitize Co", [review], db_path=db_path)

    assert "html" in captured, "expected the 1-star email to fire"
    assert "555-987-6543" in captured["html"]
