"""Onboarding email rendering. The day-7 email shipped for weeks with literal
"{restaurant_name}" text because of a double-brace bug inside an f-string —
these tests make that class of bug loud."""
import emails


class FakeEmails:
    """Captures the payload emails.deliver() would have posted to Resend.

    Sends now go through one requests.post inside deliver() rather than the
    resend library, so this intercepts there instead of stubbing the module.
    """
    last = None


def _stub_resend(monkeypatch):
    class _Resp:
        status_code = 200
        text = '{"id": "fake"}'
        @staticmethod
        def json():
            return {"id": "fake"}

    def _capture(url, headers=None, json=None, timeout=None, **kw):
        FakeEmails.last = json
        return _Resp()

    monkeypatch.setattr(emails, "_resend_key", lambda: "fake-key")
    monkeypatch.setattr("requests.post", _capture)
    # Never touch the real database from a rendering test.
    monkeypatch.setattr(emails, "_record", lambda *a, **kw: None)


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


def test_send_team_invite_email_contains_temp_password(monkeypatch):
    _stub_resend(monkeypatch)
    emails.send_team_invite_email("teammate@x.com", "Gia Mia", "gia_teammate", "S3cr3t-Temp-9",
                                  inviter_name="Will")
    payload = FakeEmails.last
    assert payload["to"] == ["teammate@x.com"]
    html = payload["html"]
    assert "gia_teammate" in html
    assert "S3cr3t-Temp-9" in html
    assert "Gia Mia" in html
    assert "Will" in html
