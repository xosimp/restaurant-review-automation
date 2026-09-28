"""What Twilio's A2P 10DLC reviewers check for the staff verification (2FA)
campaign, pinned so a later edit can't quietly undo it.

Rejected 9/28/26 (30908 / 30882 / 30896): the opt-in page at
dashboard.cavnar.ai/staff/ linked neither the Terms nor the Privacy Policy,
the privacy policy lacked the carriers' "no mobile information shared with
third parties or affiliates" statement, and the registered message flow
named neither URL. The campaign's own record also showed the Terms naming
two programs while cavnar.ai's contact form asked consent for a third."""
import html
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


LOGIN = _read("templates", "staff_login.html")
TERMS = _read("public", "terms.html")
PRIVACY = _read("public", "privacy.html")


def _consent_label():
    m = re.search(r'<label class="optin">(.*?)</label>', LOGIN, re.S)
    assert m, "the consent box sits in its own label"
    return m.group(1)


def test_the_consent_box_is_unchecked_and_carries_every_disclosure_and_both_links():
    label = _consent_label()
    box = re.search(r'<input type="checkbox" id="su-optin"[^>]*>', label).group(0)
    assert "checked" not in box, "never pre-selected (30925)"
    text = re.sub(r"<[^>]+>", "", label)
    for needed in ("Cavnar AI", "one-time verification code", "Message frequency: one message per request",
                   "Message and data rates may apply", "Reply HELP for help", "STOP to opt out",
                   "Terms of Service", "Privacy Policy"):
        assert needed in text, needed
    assert 'href="/terms#sms"' in label and 'href="/privacy#sms"' in label


def test_terms_and_privacy_are_linked_on_every_screen_of_the_page():
    footer = re.search(r'<footer class="legal">(.*?)</footer>', LOGIN, re.S).group(1)
    assert 'href="/terms"' in footer and 'href="/privacy"' in footer
    # outside both roots, so the sign-in screen shows it too
    assert LOGIN.index('<footer class="legal">') > LOGIN.index('id="signup-root"')
    assert LOGIN.index('<footer class="legal">') < LOGIN.index("<script>")


@pytest.fixture
def staff_client():
    from flask import Flask
    from staff_routes import staff_bp
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(staff_bp)
    return app.test_client()


def test_the_opt_in_url_opens_on_the_phone_step(staff_client):
    r = staff_client.get("/staff/signup")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "window.startSignup();" in html and 'id="su-phone-in"' in html and 'type="tel"' in html
    assert "window.startSignup();" not in staff_client.get("/staff/").get_data(as_text=True)


def test_the_hosted_screenshot_is_served_from_static():
    assert os.path.getsize(os.path.join(ROOT, "static", "sms", "staff-signup-optin.png")) > 10000


def test_privacy_carries_the_carriers_statement_and_names_twilio():
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", PRIVACY)))
    assert ("No mobile information will be shared with third parties or affiliates for marketing or "
            "promotional purposes.") in text
    assert "text messaging originator opt-in data and consent; this information will not be shared with any third parties" in text
    assert "directly to Cavnar AI" in text
    assert re.search(r"<strong>Twilio</strong>", PRIVACY), "the SMS processor is listed with the others"
    assert 'id="sms"' in PRIVACY


def test_terms_name_every_program_that_asks_for_consent_with_the_required_terms():
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", TERMS)))
    sms = text[text.index("SMS messaging terms"):]
    for program in ("Cavnar AI Staff Verification", "Cavnar AI Account Alerts", "Cavnar AI Schedule Texts",
                    "Inquiry replies"):
        assert program in sms, program
    for needed in ("Message & data rates may apply", "reply STOP", "reply HELP", "will@cavnar.ai",
                   "Carriers are not liable for delayed or undelivered messages",
                   "not a condition of purchasing", "one message per request",
                   "No mobile information will be shared with third parties or affiliates"):
        assert needed in sms, needed
    # the website's contact form consent is one of the programs described
    index = _read("public", "index.html")
    assert "about my inquiry and free audit" in index and "about their inquiry and free audit" in sms


def test_the_sample_message_in_the_terms_is_what_the_code_sends():
    import auth
    import inspect
    src = inspect.getsource(auth)
    assert 'f"Cavnar AI: Your verification code is {code}. "' in src
    assert "Cavnar AI: Your verification code is 412903. It expires in 10 minutes. Reply STOP to opt out." in \
        html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", TERMS)))
    assert auth.SIGNUP_CODE_TTL_MINUTES == 10


def test_cloudflare_leaves_the_contact_addresses_readable():
    for page in (TERMS, PRIVACY):
        assert page.index("<!--email_off-->") < page.index("will@cavnar.ai") < page.index("<!--/email_off-->")
