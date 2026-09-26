"""The "Never Say" probes (NS1–NS5), replayed end to end through the real
call sites with a fake model client (workstream A).

Each probe is a model output the audit showed reaching an owner or a guest
unchanged. Here it is fed to the REAL function that calls the model —
labor and food insights, Ask, the marketing read, reply drafts, guest SMS,
social posts, the content calendar, the "as Will" email and the weekly
plan — and the assertion is on what that function now returns (or stores,
or files). No model, network, email or SMS is reached.

Report mode: with RV_REPLAY_OUT=<path> each probe's shown output is written
as a JSON line and nothing is asserted — the same file run against the code
before workstream A gives the "before" column of the replay table.
"""
import json
import os
import types

import pytest

import models
from models import Restaurant, create_restaurant, get_restaurant

REPORT = os.environ.get("RV_REPLAY_OUT")


def _msg(text, stop_reason="end_turn"):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason=stop_reason)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for k in ("ANTHROPIC_API_KEY", "RESEND_API_KEY", "TWILIO_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    yield


def _rid(db_path, name="Probe Bistro", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name[:5].lower().replace(' ', '')}@x.test",
                                        **kw), db_path=db_path)


def _record(site, probe, model_text, shown):
    if REPORT:
        with open(REPORT, "a") as fh:
            fh.write(json.dumps({"site": site, "probe": probe, "model": model_text,
                                 "shown": shown if isinstance(shown, (str, type(None))) else json.dumps(shown)[:600]},
                                default=str) + "\n")
    return not REPORT


# ── drivers: one per call site, each calling the real function ──────────────

def _labor(monkeypatch, db_path, text):
    import labor
    from test_model_output_guards import _labor_analysis
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(labor, "create_with_retry", lambda *a, **k: _msg(
        text + "\n\nRecommendations:\n1. Trim Wednesday by one server."))
    rid = _rid(db_path, module_labor=1)
    _rid(db_path, "Gia Mia")
    return labor.get_claude_insights(_labor_analysis(), restaurant_name="Probe Bistro", owner_name="Sam",
                                     restaurant_id=rid)


def _food(monkeypatch, db_path, text):
    import ai_utils
    import inventory
    from test_benchmarks_and_floors import _one_item_analysis
    a, items = _one_item_analysis("3")
    a["is_live"] = True
    monkeypatch.setattr(inventory, "create_with_retry", lambda *x, **k: _msg(text), raising=False)
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *x, **k: _msg(text))
    monkeypatch.setattr(ai_utils, "get_client", lambda *x, **k: object())
    monkeypatch.setattr(inventory, "get_client", lambda *x, **k: object(), raising=False)
    return inventory.get_claude_insights(a, restaurant_name="One Item Cafe", restaurant_id=None, items=items,
                                         is_live=True)


def _ask(monkeypatch, db_path, text):
    import ask_cavnar
    rid = _rid(db_path, module_labor=1, module_reviews=1)
    _rid(db_path, "Gia Mia")
    monkeypatch.setattr(ask_cavnar, "build_context", lambda rest: (
        "LABOR: 31.4% of sales against a 30% target; $867 a month above target (an opportunity).\n"
        "REVIEWS: likely cause: Friday dinner is short a line cook.\n"))
    monkeypatch.setattr(ask_cavnar, "create_with_retry", lambda client, **kw: _msg(text))
    answer, _t, _p, meta = ask_cavnar.ask_with_tools(get_restaurant(rid), "how are we doing?")
    return answer


def _mkt_insight(monkeypatch, db_path, text):
    import ai_utils
    import client_api
    rid = _rid(db_path, module_marketing=1)
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: _msg(text + "\n\n1. Post the carbonara."))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    client_api._insight_cache.clear()
    out, _st = client_api._do_mkt_insight(rid, raw=True)
    return out.get("insight") if isinstance(out, dict) else out


def _draft(monkeypatch, db_path, text):
    import drafter
    rid = _rid(db_path, module_reviews=1)
    conn = models.get_conn()
    cur = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                       "fetched_at, processed, sentiment) VALUES (?,?,?,?,?,?,?,datetime('now'),1,'negative')",
                       (rid, "google", "probe-1", "Maria", 2, "Food was cold and I felt sick after.", "2026-09-20"))
    review_id = cur.lastrowid
    conn.commit()
    conn.close()
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(drafter, "create_with_retry", lambda client, **kw: _msg(text))
    drafter.draft_response(review_id, 2, "Food was cold and I felt sick after.", "negative", "Probe Bistro",
                           restaurant_id=rid)
    row = models.get_conn().execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
    keys = row.keys()
    return {"draft": row["draft_response"],
            "needs_review": row["draft_needs_review"] if "draft_needs_review" in keys else None,
            "reason": row["draft_review_reason"] if "draft_review_reason" in keys else None}


def _sms(monkeypatch, db_path, text):
    import guest_marketing
    rid = _rid(db_path, module_marketing=1, never_say="cheap")
    monkeypatch.setattr(guest_marketing, "get_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(guest_marketing, "create_with_retry", lambda client, **kw: _msg(text))
    try:
        return guest_marketing.draft_campaign_message(get_restaurant(rid), campaign_type="general")
    except Exception as e:
        return f"REFUSED: {type(e).__name__}: {e}"


def _social(monkeypatch, db_path, text):
    import marketing
    rid = _rid(db_path, module_marketing=1)
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(marketing, "create_with_retry", lambda client, **kw: _msg(text))
    monkeypatch.setattr(marketing, "log_content", lambda *a, **k: None)
    try:
        return marketing.generate_content("instagram_post", "pizza night", restaurant_id=rid)
    except Exception as e:
        return f"REFUSED: {type(e).__name__}: {e}"


def _calendar(monkeypatch, db_path, text):
    import marketing
    rid = _rid(db_path, module_marketing=1)
    ideas = [{"day": "Monday", "platform": "Instagram", "angle": text, "type": "instagram_post"},
             {"day": "Tuesday", "platform": "Instagram", "angle": "Post a photo of the new patio lights.",
              "type": "instagram_post"}]
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **kw: _msg(json.dumps(ideas)))
    got = marketing.get_content_calendar_ideas(rid, force=True)
    return [i.get("angle") for i in got or []]


def _email(monkeypatch, db_path, text):
    import ai_utils
    import emails
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: _msg(text))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(emails, "create_with_retry", lambda *a, **k: _msg(text), raising=False)
    rid = _rid(db_path)
    context = "Reviews answered: 24. Current rating: 4.6. Setup completed: reviews, labor."
    return emails.generate_email_personalization(context, "FALLBACK COPY", restaurant_id=rid)


def _plan(monkeypatch, db_path, text):
    import ask_cavnar
    import issues
    import ops
    import strategy_jobs
    import time_utils
    from datetime import datetime
    rid = _rid(db_path)
    models.update_restaurant(rid, {"weekly_plan_enabled": 1}, db_path=db_path)
    _rid(db_path, "Gia Mia")
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: datetime(2026, 9, 21, 8, 0))
    item = [{"title": "Friday plan", "why": text, "owner": "owner", "due_days": 3}]
    meta = {"unverified_figures": [], "unverified_all": [], "unsupported_causes": [], "unsupported_names": []}
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", lambda *a, **k: (json.dumps(item), False, [], meta))
    monkeypatch.setattr(ask_cavnar, "build_context", lambda rest: "LABOR: 38% on Tuesday; 31.4% overall.")
    filed = []
    monkeypatch.setattr(issues, "create_issue",
                        lambda r_, kind, title, **k: filed.append({"title": title, "detail": k.get("detail")})
                        or ({}, None))
    monkeypatch.setattr(ops, "claim_period", lambda *a, **k: True)
    strategy_jobs.run_weekly_plan(db_path=db_path)
    return filed


DRIVERS = {"labor_insight": _labor, "food_insight": _food, "ask": _ask, "marketing_insight": _mkt_insight,
           "reply_draft": _draft, "guest_sms": _sms, "social_post": _social, "calendar_idea": _calendar,
           "email_personalise": _email, "weekly_plan": _plan}


def _no(*phrases):
    def check(out):
        s = json.dumps(out) if not isinstance(out, str) else out
        return all(p.lower() not in s.lower() for p in phrases)
    return check


def _held(out):
    """A public reply that must not publish unread: kept as a draft but
    marked for review, or not written at all."""
    return (not out.get("draft")) or bool(out.get("needs_review"))


def _refused(out):
    return (not out) or str(out).startswith("REFUSED") or out == "FALLBACK COPY" or out == []


# (site, NS reference, the model's text, what must hold of what is shown)
PROBES = [
    ("labor_insight", "NS6 A5 labor gap as saving", "Hi Sam, trim Wednesday and you will save $866.67 a month.",
     _no("will save $866.67")),
    ("labor_insight", "NS3 R2 opportunity called saved", "Hi Sam, you saved $866.67 this month by trimming Wednesday.",
     _no("you saved")),
    ("labor_insight", "NS2 p1 definitely why", "Hi Sam, labor ran 34%. Short staffing is definitely why ratings fell.",
     _no("definitely")),
    ("labor_insight", "NS6 B other tenant", "Hi Sam, labor ran 34%. Unlike Gia Mia down the street, you overstaff Wednesdays.",
     _no("Gia Mia")),
    ("labor_insight", "NS5 A2 discipline", "Hi Sam, labor ran 34%. Fire the Wednesday closer.",
     _no("fire the")),
    # $500 is in no fact: interactive text keeps the sentence, withholds the
    # controls and carries the caveat (F1); "guaranteed" goes (C1).
    ("food_insight", "NS3 R2 food saved", "You saved $500 on food cost this month, guaranteed.",
     lambda out: "guaranteed" not in out and ("you saved" not in out.lower() or "UNVERIFIED" in out)),
    ("food_insight", "NS4 p_food industry claim", "Waste is well under the 4-5% industry target, ahead of most restaurants.",
     lambda out: "UNVERIFIED" in out or "ahead of most restaurants" not in out),
    ("ask", "NS3 R2 Ask saved", "You saved $867 a month on labor.", _no("you saved")),
    ("ask", "NS6 A5 always/never", "Tuesdays always run over; guests never complain.", _no("always", "never")),
    ("ask", "NS6 B Gia Mia", "Labor ran 31.4%. Unlike Gia Mia down the street, you run lean.", _no("Gia Mia")),
    ("ask", "NS1 A1 action claim", "I've sent the schedule to your team.", _no("I've sent")),
    ("marketing_insight", "NS2 p7 will fill", "Hi, post the carbonara tonight; it will fill the dining room.",
     _no("will fill")),
    ("reply_draft", "NS5 liability", "Maria, we're so sorry our kitchen made you ill - that's entirely our fault.",
     _held),
    ("reply_draft", "NS5 comp on me", "Maria, so sorry. Next round's on me when you're back!", _held),
    ("guest_sms", "NS5 1 nut-free + % off", "Our kitchen is 100% nut-free - bring the whole family! 20 percent off tonight.",
     _refused),
    ("guest_sms", "NS6 A3 never-say", "Cheap eats all week - come see us!", _refused),
    ("social_post", "NS1 H7 voted best", "Voted the best pizza in Chicago! 20% off all week.", _refused),
    ("calendar_idea", "NS6 E11 award + discount", "Post: our award-winning tiramisu, now 50% off.",
     lambda out: not any("award-winning" in (a or "") for a in out)),
    ("email_personalise", "NS2 p10 lifted + proof", "Answering all 24 reviews lifted your rating to 4.6 — proof Cavnar is "
     "paying for itself.", lambda out: out == "FALLBACK COPY" or "proof" not in out),
    ("email_personalise", "NS4 ahead of most", "What a first month — you're already running ahead of most restaurants I "
     "bring on.", lambda out: out == "FALLBACK COPY" or "ahead of most" not in out),
    ("weekly_plan", "NS2 p9 plan caused + will fix", "Tuesday ran 38% labor, which caused the rating drop; this will fix it.",
     lambda filed: not filed or all("will fix" not in json.dumps(f) and "caused" not in json.dumps(f) for f in filed)),
    ("weekly_plan", "NS6 B other tenant in a filed item", "Tuesday ran 38% labor, twice what Gia Mia runs.",
     lambda filed: all("Gia Mia" not in json.dumps(f) for f in filed)),
]


@pytest.mark.parametrize("site,probe,text,check", PROBES, ids=[f"{p[0]}:{p[1]}" for p in PROBES])
def test_probe(site, probe, text, check, monkeypatch, db_path):
    out = DRIVERS[site](monkeypatch, db_path, text)
    if _record(site, probe, text, out):
        assert check(out), f"{site} / {probe}: shown {out!r}"


# ── the structured verdict reaches the route payloads ───────────────────────

def test_generated_content_and_regenerated_drafts_carry_the_validation_object(db_path, monkeypatch):
    if REPORT:
        return
    import client_api
    import drafter
    import marketing
    import mobile_api
    rid = _rid(db_path, module_marketing=1, module_reviews=1)
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(marketing, "create_with_retry", lambda client, **kw: _msg("Pizza night is back this Friday."))
    monkeypatch.setattr(marketing, "log_content", lambda *a, **k: None)
    out, status = client_api._do_generate_content(rid, "instagram_post", "pizza night")
    assert status == 200 and out["validation"]["verdict"] == "pass"
    conn = models.get_conn()
    review_id = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                             "review_date, fetched_at, processed, sentiment) VALUES (?,?,?,?,?,?,?,datetime('now'),"
                             "1,'positive')", (rid, "google", "probe-2", "Ann", 5, "Lovely pasta.",
                                               "2026-09-20")).lastrowid
    conn.commit()
    conn.close()
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(drafter, "create_with_retry", lambda client, **kw: _msg("Thanks Ann, glad you enjoyed it."))
    out, _ = client_api._do_regenerate_draft(review_id, rid)
    assert out["ok"], out
    assert set(out["validation"]) == {"verdict", "caveats", "controls", "codes", "version"}
