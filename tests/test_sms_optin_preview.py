"""The public SMS opt-in preview page (/sms-optin-preview).

Three rounds of Twilio A2P 10DLC rejections, in order:
1. Error 30925 ("the opt-in checkbox is missing or appears to be
   pre-selected") — the original mockup's copy said "unchecked until SMS
   is selected" while rendering it pre-checked.
2. Errors 30896/30924 plus two explicit reviewer notes: "Consent language
   is missing required disclosures (frequency,)" and "The Opt-in link
   provided ... lacks phone number field." The page was still a static
   mockup — CSS boxes styled to look like a checkbox and fields, no real
   <input> anywhere, and the frequency disclosure lived in a separate
   paragraph below the mockup instead of inside the consent language a
   reviewer would actually check off.

The page is now a genuinely functional, unauthenticated opt-in form: a
real <input type=tel> phone field, a real <input type=checkbox> unchecked
by default, all four required disclosures (message type, frequency,
"rates may apply", STOP) together in that checkbox's own label, and a
real server-side-validated POST back to itself.
"""
from flask import Flask

from admin_routes import admin_bp


def _app():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    return app


def _client():
    return _app().test_client()


def _csrf_token(client):
    """GET first (as a browser would) to pick up the csrf_js cookie, then
    read the token echoed into the hidden form field — the same
    double-submit pattern client_api.py's staff-schedule form already
    uses for a plain-HTML-POST page behind csrf_protect()."""
    html = client.get("/sms-optin-preview").data.decode()
    start = html.index('name="csrf_token" value="') + len('name="csrf_token" value="')
    return html[start:html.index('"', start)]


# ── reachable without logging in ──────────────────────────────────────────

def test_the_page_is_reachable_without_logging_in():
    resp = _client().get("/sms-optin-preview")
    assert resp.status_code == 200


# ── error 30925: never pre-checked ────────────────────────────────────────

def test_the_consent_checkbox_is_a_real_unchecked_input_by_default():
    html = _client().get("/sms-optin-preview").data.decode()
    assert '<input type="checkbox" id="optin-consent" name="consent" required>' in html, \
        "the consent control must be a real <input type=checkbox>, not a styled <span>"
    assert "checked>" not in html.split('id="optin-consent"')[1][:5]


# ── errors 30896/30924 + the two reviewer notes ───────────────────────────

def test_a_real_phone_input_exists():
    html = _client().get("/sms-optin-preview").data.decode()
    assert '<input type="tel" id="optin-phone" name="phone" required' in html, \
        "reviewer note: \"the opt-in link provided lacks phone number field\""


def test_all_four_required_disclosures_are_in_the_consent_label_itself():
    """Not scattered across the page — a reviewer checks the box next to
    text that has to carry all four on its own: message type, frequency,
    rates-may-apply, and STOP. Frequency living only in a separate
    'Message program details' paragraph is exactly what "missing required
    disclosures (frequency,)" flagged last time."""
    html = _client().get("/sms-optin-preview").data.decode()
    label = html.split('for="optin-consent">', 1)[1].split("</label>", 1)[0]
    assert "review notifications" in label or "review alerts" in label, "message type"
    assert "per week" in label, "message frequency"
    assert "Message and data rates may apply" in label
    assert "STOP" in label and "HELP" in label
    assert 'href="/privacy"' in label and 'href="/terms"' in label


def test_the_form_posts_to_itself():
    html = _client().get("/sms-optin-preview").data.decode()
    assert '<form class="optin-form" method="POST" action="/sms-optin-preview">' in html


# ── server-side validation, not just browser-side ─────────────────────────

def test_submitting_without_consent_is_refused_server_side():
    c = _client()
    tok = _csrf_token(c)
    c.set_cookie("csrf_js", tok)
    resp = c.post("/sms-optin-preview", data={"csrf_token": tok, "phone": "5551234567"})
    html = resp.data.decode()
    assert resp.status_code == 200
    assert 'id="optin-card" data-state="submitted"' not in html
    assert "consent" in html.lower()


def test_submitting_without_a_valid_phone_is_refused():
    c = _client()
    tok = _csrf_token(c)
    c.set_cookie("csrf_js", tok)
    resp = c.post("/sms-optin-preview", data={"csrf_token": tok, "phone": "123", "consent": "on"})
    html = resp.data.decode()
    assert 'id="optin-card" data-state="submitted"' not in html
    assert "valid mobile phone" in html.lower()


def test_a_valid_submission_shows_the_confirmation_state():
    c = _client()
    tok = _csrf_token(c)
    c.set_cookie("csrf_js", tok)
    resp = c.post("/sms-optin-preview", data={
        "csrf_token": tok, "name": "Jane Doe", "phone": "(555) 123-4567", "consent": "on",
    })
    html = resp.data.decode()
    assert 'id="optin-card" data-state="submitted"' in html
    assert "You're opted in" in html


def test_confirmation_is_explicit_that_no_real_text_is_sent():
    """The messaging service this opt-in registers for is itself pending
    approval — there is nothing live to send a real confirmation SMS
    through, and a stranger's number entered by a reviewer must never
    get a real text from us. The success state has to say so plainly
    rather than implying one went out."""
    c = _client()
    tok = _csrf_token(c)
    c.set_cookie("csrf_js", tok)
    resp = c.post("/sms-optin-preview", data={"csrf_token": tok, "phone": "5551234567", "consent": "on"})
    html = resp.data.decode()
    assert "No text message is sent from this preview page" in html


def test_a_mismatched_or_missing_csrf_token_is_refused():
    c = _client()
    _csrf_token(c)  # establishes the cookie
    resp = c.post("/sms-optin-preview", data={"csrf_token": "wrong", "phone": "5551234567", "consent": "on"})
    html = resp.data.decode()
    assert 'id="optin-card" data-state="submitted"' not in html
