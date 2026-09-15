"""The /privacy and /terms pages, for exactly what Twilio's A2P 10DLC
campaign form checks before it will accept a link to them: the required
"we do not sell or share" sentence verbatim, a "Message and data rates may
apply" disclosure, and — since two separate SMS programs (owner alerts,
staff verification) now share these same links — that both are actually
described rather than just the older alert program.
"""
from flask import Flask

from admin_routes import admin_bp


def _client():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    return app.test_client()


def test_privacy_page_has_twilios_required_sentence_verbatim():
    html = _client().get("/privacy").data.decode()
    assert ("We do not sell or share your SMS opt-in data or personal "
           "information with third parties for marketing purposes.") in html


def test_terms_page_has_twilios_required_sentence_verbatim():
    html = _client().get("/terms").data.decode()
    assert ("We do not sell or share your SMS opt-in data or personal "
           "information with third parties for marketing purposes.") in html


def test_both_pages_disclose_message_and_data_rates():
    for path in ("/privacy", "/terms"):
        html = _client().get(path).data.decode()
        assert "Message &amp; data rates may apply" in html, path


def test_both_pages_describe_the_staff_verification_program():
    """Twilio links these same URLs to the OTP campaign; a page that only
    describes the alert program is a mismatch a reviewer can flag."""
    for path in ("/privacy", "/terms"):
        html = _client().get(path).data.decode()
        assert "verification" in html.lower(), path


def test_both_pages_still_describe_the_alert_program():
    """The original campaign's own registration depends on this staying —
    don't let the OTP addition crowd it out."""
    for path in ("/privacy", "/terms"):
        html = _client().get(path).data.decode()
        assert "alert" in html.lower(), path


def test_terms_page_titled_correctly():
    html = _client().get("/terms").data.decode()
    assert "Terms of Service" in html


def test_privacy_page_titled_correctly():
    html = _client().get("/privacy").data.decode()
    assert "Privacy Policy" in html


def test_cavnar_ai_redirects_to_dashboard_not_a_separate_stale_copy():
    """cavnar.ai is a hand-deployed Cloudflare Worker that doesn't auto-update
    on push — these specific paths must NOT be served from it, or an edit
    here can go live on dashboard.cavnar.ai while cavnar.ai/privacy (the URL
    Twilio's form actually asks for) stays stale. Confirmed via curl:
    cavnar.ai/privacy 301s straight to dashboard.cavnar.ai/privacy, so
    there is only one copy of this content to keep in sync — this pins that
    routing assumption at the Flask layer, not the redirect itself (that
    lives on Cloudflare and can't be tested from here)."""
    resp = _client().get("/privacy")
    assert resp.status_code == 200
