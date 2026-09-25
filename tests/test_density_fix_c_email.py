"""Density fix round, agent C — the owner emails (items 11 and 46).

  #11  the weekly digest and the monthly review lead with the verdict (H1 and
       subject), then one row of figures, then ONE action, then Needs a
       reply; "What your changes did" is said once, the headline is not
       repeated, and the generic caveats are one footer line. The monthly's
       "What worked for you" reads "Measured alongside your changes".
  #46  the morning brief and the alert email sit on report_shell with a
       headline and BRAND colours; the brief is capped at 5 lines plus
       "N more in the app"; the alert email says when a reply is drafted.

HTML is built, never sent: no model, network, email, SMS or push is reached.
"""
import pytest

import business_intelligence as bi
import emails
import models
import reporter
from models import Restaurant, WeeklyReport, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(bi, "get_conn", fake, raising=False)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI called")))
    yield


def _rid(db_path, **kw):
    fields = dict(name="Gia Mia", owner_email="o@x.test", owner_name="Sam Owner", module_reviews=1,
                  module_labor=1, module_inventory=0, module_marketing=0)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db_path)


FIX_FIRST = {"key": "cut_waste:Salmon", "what": "Cut salmon waste", "why": "it is the biggest driver",
             "modules": ["food_cost"], "dollars_monthly": 312.0}
METRICS = [
    {"key": "sales", "label": "Sales per day", "unit": "$", "value": 5400.0, "previous": 5000.0,
     "verdict": "improved", "delta": 400.0, "monthly_dollars": 12000.0, "why": ""},
    {"key": "labor_pct", "label": "Labor %", "unit": "%", "value": 31.2, "previous": 29.0,
     "verdict": "worsened", "delta": 2.2, "monthly_dollars": -1100.0, "why": ""},
]
RESULT = {"id": 1, "metric": "labor_pct", "status": "evaluated"}


def _week(monkeypatch, fix_first=True, results=True):
    import weekly_review
    import outcomes
    monkeypatch.setattr(weekly_review, "build", lambda *a, **k: {
        "week": "9/14/26 – 9/20/26", "window": ["2026-09-14", "2026-09-20"], "compared_with": "9/7/26 – 9/13/26",
        "metrics": [dict(m) for m in METRICS], "results": [dict(RESULT)] if results else [],
        "priorities": [], "fix_first": dict(FIX_FIRST) if fix_first else None})
    monkeypatch.setattr(outcomes, "summarise", lambda r: "Labor % moved after your change.")
    monkeypatch.setattr(outcomes, "recent_results", lambda *a, **k: [dict(RESULT)])
    monkeypatch.setattr(bi, "correlations", lambda *a, **k: [])


class _Review:
    def __init__(self, urgency="high", rating=1, sentiment="negative", text="Cold food and a long wait."):
        self.author, self.rating, self.text = "Dana", rating, text
        self.urgency, self.sentiment = urgency, sentiment


def _report(rid, reviews=()):
    r = WeeklyReport(restaurant_id=rid, period_start="9/14/26", period_end="9/20/26",
                     total_reviews=len(reviews), avg_rating=2.0 if reviews else 0.0,
                     sentiment={"positive": 0, "negative": len(reviews), "neutral": 0}, top_issues=[])
    r._reviews = list(reviews)
    return r


def _digest(monkeypatch, rid, action="Call the fish supplier about Friday.", headline="Model says hi."):
    monkeypatch.setattr(reporter, "generate_ai_digest_summary",
                        lambda *a, **k: {"headline": headline, "action": action})
    import rec_delivery
    with rec_delivery.collect():
        return reporter.render_digest(_report(rid, [_Review()]), "Gia Mia", "Sam", rid, owner_view=True)


def test_the_digest_leads_with_the_verdict_then_figures_then_one_action_then_replies(db_path, monkeypatch):
    rid = _rid(db_path)
    _week(monkeypatch)
    out = _digest(monkeypatch, rid)
    html = out["html"]
    import weekly_review
    verdict = weekly_review.headline(weekly_review.build(rid))
    # The H1 is the verdict, and the subject carries it.
    assert f">{verdict}</h1>" in html.replace("&#x27;", "'")
    assert out["subject"] == f"{verdict.rstrip('.')} — your week at Gia Mia"
    # Said once: not again inside "The week against".
    assert html.count(verdict) == 1
    kpi, act, reply = html.index("The week in numbers"), html.index("If you only do one thing"), \
        html.index("Needs a reply")
    assert kpi < act < reply < html.index("The week against") < html.index("Review Intelligence")
    # ONE action: the one thing, so the model's move is not rendered too.
    assert "Call the fish supplier" not in html and "This week&#x27;s move" not in html
    # "What your changes did" once, though both sources had results.
    assert html.count("What your changes did") == 1
    # The generic caveats are one footer line, after everything else.
    assert html.count("Caveats:") == 1
    assert html.index("Caveats:") > html.index("Review Intelligence")
    import outcomes
    assert html.count(outcomes.CAUSATION_CAVEAT.replace("'", "&#x27;")) <= 1


def test_the_model_move_is_the_action_only_when_there_is_no_one_thing(db_path, monkeypatch):
    rid = _rid(db_path)
    _week(monkeypatch, fix_first=False, results=False)
    html = _digest(monkeypatch, rid)["html"]
    assert "Call the fish supplier" in html and "If you only do one thing" not in html
    assert html.index("Call the fish supplier") < html.index("Needs a reply")
    # No weekly-review results: the follow-through's outcomes are the one block.
    assert html.count("What your changes did") == 1


def test_a_week_that_measured_nothing_keeps_the_plain_subject(db_path, monkeypatch):
    import weekly_review
    rid = _rid(db_path)
    _week(monkeypatch)
    monkeypatch.setattr(weekly_review, "build", lambda *a, **k: {
        "week": "w", "compared_with": "c", "metrics": [], "results": [], "priorities": [], "fix_first": None})
    out = _digest(monkeypatch, rid, headline="A quiet week at the bar.")
    assert out["subject"] == "Your week at Gia Mia"
    assert ">A quiet week at the bar.</h1>" in out["html"]
    assert out["html"].count("A quiet week at the bar.") == 1
    assert reporter.digest_subject("Gia Mia", "A better week: sales improved.") == \
        "A better week: sales improved — your week at Gia Mia"


def test_the_scheduler_sends_the_verdict_subject():
    import inspect
    import scheduler
    src = inspect.getsource(scheduler.run_weekly_digests)
    assert 'render_digest(' in src and 'subject = f"Your week at {first_rest.name}"' not in src


def test_the_monthly_review_leads_with_the_verdict_and_one_action(db_path, monkeypatch):
    import monthly_review
    rid = _rid(db_path)
    sent = {}
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(monthly_review, "build", lambda *a, **k: {
        "month": "August 2026", "months": 1, "metrics": [dict(m) for m in METRICS], "results": [],
        "goals": [], "compared_with": "July", "priorities": [], "fix_first": dict(FIX_FIRST)})
    monkeypatch.setattr(emails, "generate_email_personalization", lambda ctx, fallback, **k: fallback)
    monkeypatch.setattr(emails, "restaurant_usage", lambda rid: {"owns": {"reviews": True}, "used": {},
                                                                 "pending_reviews": 3})
    monkeypatch.setattr(emails, "deliver", lambda **k: sent.update(k) or None)
    emails.send_monthly_summary_email("o@x.test", "Gia Mia", "Sam", restaurant_id=rid)
    html, subject = sent["payload"]["html"], sent["payload"]["subject"]
    verdict = monthly_review.headline(monthly_review.build(rid))
    assert f">{verdict}</h1>" in html and subject.startswith(verdict.rstrip("."))
    assert html.count(verdict) == 1
    kpi, act = html.index("in numbers"), html.index("If you only do one thing")
    assert kpi < act < html.index("Needs a reply") < html.index("The month against")
    # One action: the drafted replies are "Needs a reply", not a second move.
    assert "Your next move" not in html
    assert html.count("Caveats:") <= 1


def test_the_monthly_label_is_measured_alongside_your_changes():
    import inspect
    src = inspect.getsource(emails._monthly_review_parts)
    assert 'report_eyebrow("Measured alongside your changes")' in src
    assert 'report_eyebrow("What worked for you")' not in src


# ── #46 the morning brief and the alert email ───────────────────────────────

def _lines(n):
    tones = ["neutral", "neutral", "good", "action", "neutral", "bad", "neutral"]
    return [{"key": f"k{i}", "tone": tones[i % len(tones)], "text": f"Line {i}."} for i in range(n)]


def test_the_brief_email_is_on_report_shell_with_a_state_headline_and_a_five_line_cap():
    import morning_brief
    brief = {"date": "2026-09-25", "lines": _lines(7), "restaurant_id": None}
    shown, more = morning_brief.email_lines(brief)
    assert len(shown) == 5 and more == 2
    # The lines that need the owner are kept; the brief's order is kept.
    assert {"k3", "k5"} <= {l["key"] for l in shown}
    assert [l["key"] for l in shown] == sorted((l["key"] for l in shown), key=lambda k: int(k[1:]))
    html = morning_brief._email_html(brief, "Gia Mia",
                                     verdict={"label": "Good day", "tone": "good", "overall": 78})
    assert ">2 things need you this morning</h1>" in html
    assert "Good day" in html and "78/100" in html and emails.BRAND["good"] in html
    assert "2 more in the app" in html and "Line 5." in html and "Line 6." not in html
    assert emails.BRAND["paper"] in html and "Morning brief" in html
    quiet = morning_brief._email_html({"date": "2026-09-25", "lines": _lines(2)}, "Gia Mia")
    assert ">Nothing needs you this morning</h1>" in quiet and "more in the app" not in quiet
    assert "Last night:" not in quiet


def test_the_brief_presents_only_the_lines_its_email_showed():
    import inspect
    import morning_brief
    src = inspect.getsource(morning_brief.deliver)
    assert "_branded_email" not in src
    assert "dict(brief, lines=email_lines(brief)[0])" in src
    assert "night_verdict(restaurant_id" in src


def test_the_alert_email_is_on_report_shell_without_emoji_and_names_a_ready_draft():
    import inspect
    import notify
    html = notify._alert_email_html("Gia Mia", "\U0001F534 1★ review received on Google",
                                    ["A 1-star review was posted on Google:", '<em>"Cold food"</em>'],
                                    cta_label="Respond now", draft_ready=True)
    assert ">1★ review received on Google</h1>" in html and "\U0001F534" not in html
    assert "A reply is drafted: read and post it" in html and "Respond now" not in html
    assert notify.CTA_PLACEHOLDER in html and emails.BRAND["paper"] in html
    plain = notify._alert_email_html("Gia Mia", "\U0001F6A8 Health/safety mention in a new review", ["x"],
                                     cta_label="Respond now")
    assert ">Health/safety mention in a new review</h1>" in plain
    assert "Respond now" in plain and "A reply is drafted" not in plain
    assert inspect.getsource(notify.fire_review_alerts).count("draft_ready=drafted") == 6
