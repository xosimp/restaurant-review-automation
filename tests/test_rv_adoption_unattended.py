"""Workstream A, unattended group: the weekly digest, the DSR narrative, the
weekly plan and the personalised emails run the Response Validation Layer
(response_validation.py) on every line before it is sent or filed.

These four surfaces are read by nobody before the recipient, so the engine's
unattended delivery applies: a rewrite (a lowered modal, a softened cause)
stands; anything above a caveat drops the line — or, for the DSR's lead and
an email paragraph that empties, refuses to the surface's fallback.

Each test below replays a claim the old hand-rolled checks let through:
a peer comparison with no benchmark, "will definitely", another tenant's
name, a cause resting only on a recommended action, a food-safety shortcut,
a name Ask flagged as unsupported. No model, email, SMS or scheduler is
reached: every model client is a fake and every send is stubbed.
"""
import json
import types
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, Review, create_restaurant, save_reviews, update_restaurant

# Imported before any fixture patches get_conn (CLAUDE.md's bound-import hazard).
import ai_guard  # noqa: E402
import ai_utils  # noqa: E402
import dsr  # noqa: E402
import emails  # noqa: E402
import reporter  # noqa: E402
import response_validation as rv  # noqa: E402
import review_intelligence  # noqa: E402
import strategy_jobs  # noqa: E402
from dsr import narrative  # noqa: E402


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(review_intelligence, "DB_PATH", db_path)
    monkeypatch.setattr(review_intelligence, "get_conn", fake, raising=False)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    models._invalidate_tenant_names()
    yield
    models._invalidate_tenant_names()


@pytest.fixture
def logged(monkeypatch):
    """Every verdict the call sites log (rv.log), by surface."""
    seen = []
    real = rv.log

    def capture(verdict, ctx, *a, **k):
        seen.append((ctx.surface, verdict.verdict, verdict.codes))
        return real(verdict, ctx, *a, **k)
    monkeypatch.setattr(rv, "log", capture)
    return seen


def _rid(db_path, name="Probe Bistro", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=kw.pop("owner_email", "o@x.test"), **kw),
                             db_path=db_path)


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


# ══ the weekly digest (reporter.generate_ai_digest_summary) ═══════════════

def _digest(db_path, monkeypatch, reply, rid=None, reviews=None, name="Probe Bistro"):
    rid = rid or _rid(db_path, name=name)
    save_reviews(reviews or [
        Review(restaurant_id=rid, platform="google", external_id="d1", author="Dana Ray", rating=5,
               text="Lovely dinner and a friendly room.", review_date=date.today().isoformat()),
        Review(restaurant_id=rid, platform="google", external_id="d2", author="Sam Lee", rating=4,
               text="Good tacos, slow bar.", review_date=date.today().isoformat()),
    ], db_path=db_path)
    report = reporter.build_report_from_db(rid, name, days=7, db_path=db_path)
    seen = {}
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: seen.update(k) or _msg(reply))
    out = reporter.generate_ai_digest_summary(report, name, "Pat", restaurant_id=rid)
    return out, seen


def test_a_digest_line_comparing_the_restaurant_with_most_restaurants_is_dropped(db_path, monkeypatch):
    """NS4 PEER_CLAIM_UNATTENDED: every figure in the line is the digest's
    own, so the old figure/name/cause checks passed it."""
    out, _ = _digest(db_path, monkeypatch,
                     "HEADLINE: Pat, two new reviews this week.\n"
                     "REVIEWS: Your 4.5 rating beats most restaurants in the area.")
    assert out.get("headline") and "reviews" not in out


def test_a_digest_line_naming_another_cavnar_restaurant_is_dropped(db_path, monkeypatch):
    """T1: the name is in the prompt (a guest wrote it), so the old name
    check passed it — but it is another tenant's."""
    _rid(db_path, name="Rosa's Cantina", owner_email="rosa@x.test")
    rid = _rid(db_path)
    reviews = [Review(restaurant_id=rid, platform="google", external_id="t1", author="Dana Ray", rating=5,
                      text="Better than Rosa's Cantina, honestly.", review_date=date.today().isoformat())]
    out, _ = _digest(db_path, monkeypatch,
                     "HEADLINE: Pat, one new 5★ review this week.\n"
                     "REVIEWS: Dana said you beat Rosa's Cantina.", rid=rid, reviews=reviews)
    assert out.get("headline") and "reviews" not in out


def test_the_digest_lowers_a_certainty_it_cannot_back(db_path, monkeypatch):
    out, _ = _digest(db_path, monkeypatch,
                     "HEADLINE: Pat, your 4.5 rating will definitely hold next week.")
    assert "definitely" not in out["headline"] and "4.5" in out["headline"]


def test_a_recommended_action_is_never_a_cause_anchor_in_the_digest(db_path, monkeypatch):
    """The old digest passed the diagnosis's recommended_action to the cause
    check, so a cause built from the fix ("no manager on the pass") read as
    supported. Only the cause (likely) and the alternative (association)
    anchor a cause now."""
    diag = {"category": "wait_time", "mention_count": 4, "cause": "The line is short a cook on weekends",
            "alternative_cause": None, "what_would_confirm": None,
            "recommended_action": "Put a manager on the pass for Friday dinner"}
    monkeypatch.setattr(review_intelligence, "get_diagnoses", lambda *a, **k: [diag])
    out, _ = _digest(db_path, monkeypatch,
                     "HEADLINE: Pat, two new reviews this week.\n"
                     "REVIEWS: Tickets backed up because there was no manager on the pass at Friday dinner.")
    assert out.get("headline") and "reviews" not in out


def test_every_digest_line_is_logged_on_the_digest_surface(db_path, monkeypatch, logged):
    _digest(db_path, monkeypatch, "HEADLINE: Pat, two new reviews this week.\n"
                                  "REVIEWS: Two reviews came in at a 4.5 average.")
    assert [s for s, _v, _c in logged].count("digest") == 2


def test_a_stale_labor_line_carries_its_caveat_into_the_email(db_path, monkeypatch):
    """M1: labor data three weeks old stated as "this week" keeps the line
    only with the caveat saying which dates it reads — rendered in the email,
    since nobody reads the digest before it goes."""
    rid = _rid(db_path, module_labor=1)
    import labor
    end = (date.today() - timedelta(days=21)).isoformat()
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {
        "is_live": True, "overall_labor_pct": 31.0, "overtime_risk": [],
        "date_range": {"end": end, "days": 14}})
    out, _ = _digest(db_path, monkeypatch,
                     "HEADLINE: Pat, two new reviews this week.\n"
                     "REVIEWS: Two reviews came in at a 4.5 average.\n"
                     "LABOR: Labor ran 31.0% of revenue this week.", rid=rid)
    assert out.get("labor") and out.get("_caveats")
    assert any("not this week" in c or "out of date" in c for c in out["_caveats"])
    # …and the email shows the caveat: it cannot be read beside the line
    # anywhere else.
    report = reporter.build_report_from_db(rid, "Probe Bistro", days=7, db_path=db_path)
    monkeypatch.setattr(reporter, "generate_ai_digest_summary", lambda *a, **k: out)
    parts = reporter._digest_parts(report, "Probe Bistro", "Pat", restaurant_id=rid)
    assert any(out["_caveats"][0] in s for s in parts["sections"])


# ══ the DSR narrative (dsr/narrative.check_item / write) ══════════════════

def _night():
    return {"schema": dsr.SCHEMA_VERSION, "restaurant_id": 1, "business_date": "2026-09-19",
            "fiscal": {"week_start": "2026-09-16", "week_end": "2026-09-22", "period": 9, "week": 4},
            "blocks": {
                "sales": dsr.block(dsr.READY, source="rpower", metrics={
                    "net": 19850.40, "net_last_week": 17210.15, "gross": 21430.0, "gross_budget": 20000},
                    detail={"top_items": [{"name": "Smash Burger", "qty": 142}]}),
                "labor": dsr.block(dsr.READY, source="rpower", metrics={
                    "dollars": 4812.30, "pct": 24.2, "target_pct": 26.0, "overtime_hours": 6.5}),
                "food": dsr.block(dsr.READY, source="cavnar", metrics={
                    "waste_dollars": 84.5, "recoverable_monthly": 640.0}),
                "closeout": dsr.block(dsr.READY, source="cavnar", metrics={"callouts": 0},
                                      detail={"went_wrong": "Ice machine slow again, and the walk-in door sticks.",
                                              "submitted_by": "Jim"}),
            }}


def _F():
    return narrative.Facts(_night())


def test_a_dsr_line_comparing_with_most_restaurants_is_dropped():
    it = {"text": "Labor ran 24.2%, better than most restaurants.", "cites": ["labor.pct"]}
    assert narrative.check_item(it, _F())


def test_a_dsr_line_keeps_its_figures_with_the_certainty_lowered():
    it = {"text": "Labor ran 24.2% and it will definitely stay under target.", "cites": ["labor.pct",
                                                                                      "labor.target_pct"]}
    assert narrative.check_item(it, _F()) is None
    assert "definitely" not in it["text"] and "24.2%" in it["text"]


def test_a_dsr_action_recommending_a_food_safety_shortcut_is_dropped():
    act = {"text": "Extend holding time on the brisket to cut waste.", "why": "Waste was $84.50 last night.",
           "dollars_monthly": None, "urgency": "this_week", "effort": "low", "kind": "reduce_waste",
           "subject": None, "cites": ["food.waste_dollars"]}
    assert narrative.check_item(act, _F(), action=True)


def test_a_dsr_line_echoing_the_closeout_is_still_dropped():
    it = {"text": "Labor ran 24.2%; the ice machine slow again, and the walk-in door sticks.",
          "cites": ["labor.pct"]}
    assert narrative.check_item(it, _F())


def test_the_dsr_logs_each_checked_line_on_the_dsr_surface(logged):
    it = {"text": "Labor ran 24.2% against a 26% target.", "cites": ["labor.pct", "labor.target_pct"]}
    assert narrative.check_item(it, _F()) is None
    assert ("dsr", "pass", []) in logged


class _Client:
    def __init__(self, reply):
        self.reply, self.calls, self.messages = reply, [], self

    def create(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=json.dumps(self.reply))], stop_reason="end_turn",
            usage=types.SimpleNamespace(input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
                                        cache_read_input_tokens=0))


def _reply(lead):
    return {"executive_summary": {"text": lead, "cites": ["sales.net", "sales.net_last_week", "labor.pct"]},
            "went_well": [], "needs_attention": [], "actions_tomorrow": []}


def test_a_dsr_lead_with_a_peer_claim_refuses_the_narrative(monkeypatch, db_path):
    ai_utils.reset_breaker()
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    rest = models.get_restaurant(_rid(db_path, name="Simple Test"), db_path=db_path)
    lead = ("Saturday did $19,850 net, $2,640 above last Saturday. "
            "Labor ran 24.2%, well ahead of the industry average.")
    client = _Client(_reply(lead))
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: client)
    out = narrative.write(dsr.Context(rest, "2026-09-19", db_path=db_path), _night())
    assert out["ok"] is False and "held back" in out["reason"]
    # …and the same lead without the peer claim stands.
    client.reply = _reply("Saturday did $19,850 net, $2,640 above last Saturday. Labor ran 24.2%.")
    out = narrative.write(dsr.Context(rest, "2026-09-19", db_path=db_path), _night())
    assert out["ok"], out


def test_the_dsr_uses_the_shared_tolerance_and_shingles():
    """NS6 A3: one tolerance and one shingle function (ai_guard's), where the
    move is behaviour-preserving. The direction reader stays the DSR's own:
    ai_guard.claimed_direction reads "fell to 31%" as down, which would drop
    every true "Labor fell to 24.2%" line citing a positive labor.pct."""
    for raw in ("$4,212", "$4,200", "$20,000", "31.4%", "$2.4k"):
        c = ai_guard.figure_claims(raw)[0]
        assert narrative._tolerance(c) == ai_guard.precision_tolerance(c)
    assert narrative._shingles("the walk-in door sticks again and again tonight") == \
        ai_guard.shingles("the walk-in door sticks again and again tonight")


# ══ the weekly plan (strategy_jobs.run_weekly_plan) ═══════════════════════

def _plan(db_path, monkeypatch, items, meta=None):
    import ask_cavnar, issues, ops, time_utils
    rid = _rid(db_path)
    update_restaurant(rid, {"weekly_plan_enabled": 1}, db_path=db_path)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: datetime(2026, 9, 21, 8, 0))
    monkeypatch.setattr(ask_cavnar, "ask_with_tools",
                        lambda *a, **k: (json.dumps(items), False, [], dict(meta or {"unverified_all": []})))
    filed = []
    monkeypatch.setattr(issues, "create_issue",
                        lambda r_, kind, title, **k: filed.append((title, k.get("detail"))) or ({}, None))
    monkeypatch.setattr(ops, "claim_period", lambda *a, **k: True)
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    strategy_jobs.run_weekly_plan(db_path=db_path)
    return rid, filed


def test_a_plan_item_naming_someone_ask_could_not_find_is_not_filed(db_path, monkeypatch):
    """NS6 A2: the plan ignored meta.unsupported_names."""
    items = [{"title": "Have Marco cover Friday", "why": "Labor ran 31.4% on Fridays.", "owner": "manager",
              "due_days": 3}]
    _rid_, filed = _plan(db_path, monkeypatch, items, {"unverified_all": [], "unsupported_names": ["Marco"]})
    assert filed == []


def test_a_plan_item_with_a_peer_comparison_is_not_filed(db_path, monkeypatch):
    items = [{"title": "Add a host Saturday", "why": "Labor ran 31.4% on Saturdays, above the industry average.",
              "owner": "manager", "due_days": 3}]
    _rid_, filed = _plan(db_path, monkeypatch, items)
    assert filed == []


def test_a_plan_item_naming_another_tenant_is_not_filed(db_path, monkeypatch):
    _rid(db_path, name="Rosa's Cantina", owner_email="rosa@x.test")
    items = [{"title": "Match Rosa's Cantina's happy hour", "why": "Labor ran 31.4% on Tuesdays.",
              "owner": "owner", "due_days": 3}]
    _rid_, filed = _plan(db_path, monkeypatch, items)
    assert filed == []


def test_a_plan_item_is_filed_with_its_certainty_lowered(db_path, monkeypatch):
    items = [{"title": "Trim Tuesday close", "why": "Labor ran 31.4% on Tuesdays; this will definitely fix it.",
              "owner": "manager", "due_days": 3}]
    _rid_, filed = _plan(db_path, monkeypatch, items)
    assert len(filed) == 1 and "definitely" not in filed[0][1] and "31.4%" in filed[0][1]


def test_the_weekly_plan_logs_each_item_on_its_surface(db_path, monkeypatch, logged):
    items = [{"title": "Trim Tuesday close", "why": "Labor ran 31.4% on Tuesdays.", "owner": "manager",
              "due_days": 3}]
    _plan(db_path, monkeypatch, items)
    assert any(s == "weekly_plan" for s, _v, _c in logged)


# ══ the personalised emails (emails.generate_email_personalization) ═══════

@pytest.fixture
def model(monkeypatch):
    box = {"text": ""}
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: _msg(box["text"]))
    return box


def test_a_peer_claim_in_wills_voice_sends_the_fallback(model):
    """Probe p_email: no figure, so verify_figures passed it."""
    model["text"] = "What a first month — Quiet Tavern is already running ahead of most restaurants I bring on."
    out = emails.generate_email_personalization("Restaurant: Quiet Tavern. Reviews handled this month: 0.",
                                                "FALLBACK", restaurant_id=None,
                                                facts=[rv.Fact("reviews.handled", 0, "count")])
    assert out == "FALLBACK"


def test_a_cause_nothing_measured_sends_the_fallback(model):
    """NS2 p10: every figure was handed over, so verify_figures passed it."""
    model["text"] = "Answering all 24 reviews lifted your rating to 4.6."
    out = emails.generate_email_personalization("Reviews handled: 24. Average rating: 4.6.", "FALLBACK",
                                                facts=[rv.Fact("reviews.handled", 24, "count"),
                                                       rv.Fact("reviews.rating", 4.6, "★")])
    assert out == "FALLBACK"


def test_one_dropped_sentence_leaves_the_rest_of_the_paragraph(model):
    model["text"] = "All 24 of your replies are out. Restaurants like yours see a 12% lift."
    out = emails.generate_email_personalization("Reviews handled: 24.", "FALLBACK",
                                                facts=[rv.Fact("reviews.handled", 24, "count")])
    assert out == "All 24 of your replies are out."


def test_an_invented_figure_still_sends_the_fallback(model):
    model["text"] = "You answered 31 reviews this month."
    assert emails.generate_email_personalization("Reviews handled: 24.", "FALLBACK") == "FALLBACK"


def test_the_day30_email_hands_its_figures_over_typed(monkeypatch):
    seen = {}
    monkeypatch.setattr(emails, "generate_email_personalization",
                        lambda ctx, fallback, **k: seen.update(k, ctx=ctx) or fallback)
    monkeypatch.setattr(emails, "_resend_key", lambda: "fake-key")
    monkeypatch.setattr(emails, "deliver", lambda **k: True)
    monkeypatch.setattr(emails, "restaurant_usage", lambda rid: {"owns": {}, "used": {}})
    emails.send_onboarding_day30("t@x.com", "Gia Mia", "Will", restaurant_id=None)
    keys = {f.key for f in seen.get("facts") or []}
    assert {"reviews.handled", "reviews.responded"} <= keys
