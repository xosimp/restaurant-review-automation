"""The staff schedule-texts campaign's public opt-in (/staff-sms-optin-preview).

The real switch is in the staff app (Me → Schedule texts) behind an
employee's PIN, so the carrier reviewer needs a live copy of it — built on
the four rejections the owner campaign took (test_sms_optin_preview.py):
a real unchecked box, a real phone field, nothing required, and every
disclosure inside the consent label. The label is people.STAFF_SMS_CONSENT_TEXT
itself, so the page, the app and the registered campaign say one sentence.
"""
import html as _html
import os

from flask import Flask

import people
from admin_routes import admin_bp

URL = "/staff-sms-optin-preview"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _client():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    return app.test_client()


def _csrf(client):
    html = client.get(URL).data.decode()
    start = html.index('name="csrf_token" value="') + len('name="csrf_token" value="')
    tok = html[start:html.index('"', start)]
    client.set_cookie("csrf_js", tok)
    return tok


def _label(html):
    return html.split('for="optin-consent">', 1)[1].split("</label>", 1)[0]


def test_the_consent_sentence_carries_every_disclosure_a_reviewer_checks():
    t = people.STAFF_SMS_CONSENT_TEXT
    assert "schedule" in t and "swaps" in t and "time off" in t, "message types"
    assert "frequency varies" in t and "a week" in t, "frequency — the owner campaign's 30896"
    assert "Msg & data rates may apply" in t
    assert "STOP" in t and "HELP" in t


def test_reachable_without_logging_in_with_a_real_unchecked_box_and_phone_field():
    resp = _client().get(URL)
    assert resp.status_code == 200
    html = resp.data.decode()
    assert '<input type="checkbox" id="optin-consent" name="consent">' in html
    assert '<input type="tel" id="optin-phone" name="phone" autocomplete="tel"' in html
    form = html.split('<form class="optin-form"', 1)[1].split("</form>", 1)[0]
    assert " required" not in form and "checked" not in form
    assert f'action="{URL}"' in form


def test_the_label_is_the_apps_own_sentence_with_the_policy_links():
    label = _label(_client().get(URL).data.decode())
    assert _html.escape(people.STAFF_SMS_CONSENT_TEXT) in label
    assert 'href="/privacy#sms"' in label and 'href="/terms#sms"' in label
    assert "not a condition" in label


def test_it_saves_without_consent_and_without_a_number():
    c = _client()
    tok = _csrf(c)
    for data in ({}, {"phone": "5551234567"}):
        html = c.post(URL, data=dict(data, csrf_token=tok)).data.decode()
        assert 'id="optin-card" data-state="submitted"' in html, data
        assert "Saved — no text messages" in html


def test_the_box_without_a_number_asks_for_one_and_a_valid_one_confirms_honestly():
    c = _client()
    tok = _csrf(c)
    html = c.post(URL, data={"csrf_token": tok, "consent": "on"}).data.decode()
    assert 'id="optin-card" data-state="submitted"' not in html and "leave the box unchecked" in html
    html = c.post(URL, data={"csrf_token": tok, "phone": "(555) 123-4567", "consent": "on"}).data.decode()
    assert 'id="optin-card" data-state="submitted"' in html and "You're opted in to schedule texts" in html
    assert "No text message is sent from this preview page" in html


def test_a_wrong_csrf_token_is_refused():
    c = _client()
    _csrf(c)
    html = c.post(URL, data={"csrf_token": "wrong", "phone": "5551234567", "consent": "on"}).data.decode()
    assert 'id="optin-card" data-state="submitted"' not in html


def test_the_terms_point_the_program_at_this_page_and_the_app_falls_back_to_the_same_sentence():
    with open(os.path.join(ROOT, "public", "terms.html")) as f:
        terms = f.read()
    assert "dashboard.cavnar.ai/staff-sms-optin-preview" in terms
    with open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Features", "Staff", "StaffMeView.swift")) as f:
        assert people.STAFF_SMS_CONSENT_TEXT in f.read()
