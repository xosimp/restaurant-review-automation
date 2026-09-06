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


# ── The two report emails ──────────────────────────────────────────────────
# The monthly summary and the weekly digest share one layout (emails.report_*).
# Both regressions these cover shipped and were only visible in a real inbox:
# flex stat rows that collapse in Outlook, and a digest that rendered dark
# because it read the WEB DASHBOARD's dark-mode column.

def test_monthly_summary_has_no_flex_layout(monkeypatch):
    """`display:flex` is not implemented by Outlook or parts of Gmail, so the
    stat row it laid out arrived as a ragged vertical stack."""
    _stub_resend(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    emails.send_monthly_summary_email("t@x.com", "Gia Mia", "Will", restaurant_id=None,
                                      has_reviews=True, has_labor=True,
                                      has_inventory=True, has_marketing=True)
    html = FakeEmails.last["html"]
    assert "display:flex" not in html.replace(" ", "")
    assert html.lstrip().lower().startswith("<!doctype")


def test_monthly_summary_says_nothing_about_modules_the_client_lacks(monkeypatch):
    _stub_resend(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    emails.send_monthly_summary_email("t@x.com", "Gia Mia", "Will", restaurant_id=None,
                                      has_reviews=True, has_labor=False,
                                      has_inventory=False, has_marketing=False)
    html = FakeEmails.last["html"]
    for absent in ("Labor Optimizer", "Food Cost Control", "Marketing Autopilot"):
        assert absent not in html


def test_html_document_does_not_wrap_a_document_twice():
    """reporter.render_html returns a whole document and five call sites then
    passed it through this wrapper, nesting <!doctype> inside a <td>."""
    once = emails.html_document("<div>hi</div>")
    assert emails.html_document(once) == once


def test_weekly_digest_renders_light_even_for_a_dark_dashboard(monkeypatch):
    """The dashboard POSTs its own dark-mode switch to /api/theme on every page
    load. That column used to decide this email's colors, so preferring a dark
    dashboard silently turned the weekly digest dark."""
    import reporter
    from models import WeeklyReport

    class _Rest:
        id = 1
        name = "Gia Mia"
        location_name = None
        email_theme = "dark"
        module_reviews = True
        module_labor = False
        module_inventory = False
        module_marketing = False

    monkeypatch.setattr("models.get_restaurant", lambda *a, **k: _Rest())
    monkeypatch.setattr(reporter, "generate_ai_digest_summary",
                        lambda *a, **k: {"headline": "A quiet week."})
    report = WeeklyReport(restaurant_id=1, period_start="2026-01-01", period_end="2026-01-07",
                          total_reviews=2, avg_rating=4.6,
                          sentiment={"positive": 2, "negative": 0, "neutral": 0},
                          top_issues=[])
    html = reporter.render_html(report, "Gia Mia", owner_name="Will", restaurant_id=1)
    assert "#f7f4ef" in html          # the paper ground every other email uses
    assert "#0e0a06" not in html      # the old dark page background
    assert "display:flex" not in html.replace(" ", "")
