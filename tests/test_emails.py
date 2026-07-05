"""Onboarding email rendering. The day-7 email shipped for weeks with literal
"{restaurant_name}" text because of a double-brace bug inside an f-string —
these tests make that class of bug loud."""
import sys
import types

import emails


class FakeEmails:
    last = None

    @staticmethod
    def send(payload):
        FakeEmails.last = payload
        return {"id": "fake"}


def _stub_resend(monkeypatch):
    fake = types.ModuleType("resend")
    fake.Emails = FakeEmails
    fake.api_key = None
    monkeypatch.setitem(sys.modules, "resend", fake)
    monkeypatch.setattr(emails, "RESEND_API_KEY", "fake-key")


def test_personalization_falls_back_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = emails.generate_email_personalization("some context", "the fallback")
    assert out == "the fallback"


def test_day7_renders_real_values_not_placeholders(monkeypatch):
    _stub_resend(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    emails.send_onboarding_day7("t@x.com", "Gia Mia", "Will",
                                has_labor=True, approved_count=3, pending_count=1)
    html = FakeEmails.last["html"]
    assert "Gia Mia" in html
    for leaked in ("{restaurant_name}", "{activity_sentence}", "{pending_sentence}"):
        assert leaked not in html


def test_monthly_summary_marketing_block_interpolates(monkeypatch):
    _stub_resend(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    emails.send_monthly_summary_email("t@x.com", "Gia Mia", "Will",
                                      restaurant_id=None, has_reviews=True, has_marketing=True)
    html = FakeEmails.last["html"]
    assert '{now.strftime("%B")}' not in html


def test_no_template_placeholder_leaks_in_any_onboarding_email(monkeypatch):
    """Catch-all: no single-brace python expression should ever survive into
    sent HTML for the emails that previously shipped broken."""
    _stub_resend(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    emails.send_onboarding_day2("t@x.com", "Gia Mia", "Will", modules=["Review Intelligence"])
    day2 = FakeEmails.last["html"]
    emails.send_onboarding_day30("t@x.com", "Gia Mia", "Will",
                                 modules=["Review Intelligence"], restaurant_id=None)
    day30 = FakeEmails.last["html"]
    import re
    for html in (day2, day30):
        leaks = re.findall(r"\{[a-z_]+\}", html)
        assert not leaks, f"unrendered placeholders leaked: {leaks}"
