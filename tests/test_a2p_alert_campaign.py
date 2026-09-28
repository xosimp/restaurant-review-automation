"""The owner-alert campaign (Messaging Service ...ef4d), rejected with 30923
"consent cannot be a required condition": its opt-in says texts are
optional, and every alert text names its sender (9/28/26)."""
import inspect
import os
import re

import notify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_every_alert_text_names_cavnar_ai_once():
    assert notify.with_sender("🔴 1★ Review — Simple EJ's\n\"cold fries\"\nRespond now · dashboard.cavnar.ai") \
        .startswith("Cavnar AI: 🔴 1★ Review — Simple EJ's")
    assert notify.with_sender("Cavnar AI · Simple EJ's: 3 things this morning.") == \
        "Cavnar AI · Simple EJ's: 3 things this morning."
    assert notify.with_sender("") == ""
    src = inspect.getsource(notify.deliver_alert)
    assert "with_sender(keyed_sms_text(" in src


def test_the_in_app_consent_says_it_is_optional_and_how_often():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    label = re.search(r'id="al-sms-consent"[^>]*>(.*?)</label>', src, re.S).group(1)
    for needed in ("Optional", "typically up to about 5 a week", "Message &amp; data rates may apply",
                   "STOP", "HELP", "by email and in the app", 'href="/privacy#sms"', 'href="/terms#sms"'):
        assert needed in label, needed
