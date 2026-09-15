"""The public SMS opt-in preview page (/sms-optin-preview).

This page exists for exactly one reader: a carrier's A2P 10DLC campaign
reviewer, who cannot log into the dashboard to see the real Alert Settings
screen. It has to show the consent checkbox UNCHECKED by default, because
that mismatch — the copy says "unchecked until SMS is selected" while the
mockup rendered it pre-checked — is what got the first campaign submission
rejected (Twilio error 30925: "the opt-in checkbox is missing or appears to
be pre-selected"). Public, unauthenticated, no login_required.
"""
from flask import Flask

from admin_routes import admin_bp


def _client():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    return app.test_client()


def test_the_page_is_reachable_without_logging_in():
    resp = _client().get("/sms-optin-preview")
    assert resp.status_code == 200


def test_the_default_state_shows_every_box_unchecked():
    html = _client().get("/sms-optin-preview").data.decode()
    default_block, _, rest = html.partition("Default state")
    after_block = rest.split("After the owner")[0]
    assert "chk checked" not in after_block, (
        "the default-state mockup shows a pre-checked box — this is the "
        "exact defect Twilio's error 30925 flagged")


def test_the_after_state_is_still_shown_for_context():
    """The reviewer should also see what consent looks like once given —
    just not confuse it for the starting state."""
    html = _client().get("/sms-optin-preview").data.decode()
    _, _, after_block = html.partition("After the owner")
    assert "chk checked" in after_block


def test_the_copy_and_the_mockup_agree():
    """A reviewer reads the sentence and looks at the picture together —
    the two must describe the same state."""
    html = _client().get("/sms-optin-preview").data.decode()
    assert "unchecked until SMS delivery is selected" in html
    default_block = html.split("Default state")[1].split("After the owner")[0]
    assert 'class="chk"></span> SMS' in default_block \
        or 'class="chk"></span>' in default_block
