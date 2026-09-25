"""Confidence re-audit, round 2 — Group T: confidence reaches every
recommendation-bearing surface (server).

  T1  a K1 confidence on the AI-read lines, Intel, What connects, Ask
      suggestions, morning-brief lines and the group view; the % label and
      as-of on emails and pushes; the digest reads K1, never a model band;
      the rating-trend alert states a measured figure
  T2  "not for us" by advice signature on Shift Quality items, the AI-read
      lines, the digest move, the quiet-night push, Ask suggestions; a
      one-day trim decline no longer suppresses the whole-schedule card
  T3  the brief footer, the monthly value headings, the digest's labor /
      waste / rating tags
  T4  Home's negative-share item and the 8-week slope alert have two keys
  T5  demand accuracy names its sign
  T6  the prime-cost projection is withheld server-side on an often-wide record
  T7  M/D/YY: labor_period, the digest prompt date
  T8  food drivers carry rec_key; the web's "schedule" surface is known; the
      DSR verification says why lines were dropped
  T9  every Labor-read path carries its read state

No model, email, SMS or push is reached: every sender is faked (conftest)
and every model call is stubbed.
"""
import inspect
import sys
import types
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant

# Imported at collection, never first inside a test (the bound-import hazard).
import ask_cavnar  # noqa: E402
import business_intelligence as bi  # noqa: E402
import client_api  # noqa: E402
import confidence_engine as ce  # noqa: E402
import demand  # noqa: E402
import emails  # noqa: E402
import food_cost_intelligence as fci  # noqa: E402
import home_brief  # noqa: E402
import insight_store  # noqa: E402
import mobile_api  # noqa: E402
import morning_brief  # noqa: E402
import notify  # noqa: E402
import rec_ledger  # noqa: E402
import rec_trust  # noqa: E402
import reporter  # noqa: E402
import schedule_engine  # noqa: E402
import strategy_jobs  # noqa: E402
import strategy_routes  # noqa: E402
from dsr import deliver as dsr_deliver  # noqa: E402
from dsr import narrative as dsr_narrative  # noqa: E402


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (client_api, home_brief, mobile_api):
        monkeypatch.setattr(mod, "get_conn", conn)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    client_api._insight_cache.clear()
    home_brief.invalidate()
    yield db_path
    client_api._insight_cache.clear()


def _rid(db, **kw):
    fields = dict(name="Round Two", owner_email="r2@x.test", module_reviews=1, module_labor=1,
                  module_inventory=1, module_marketing=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db)


def _decline(db, rid, key, title=None):
    rec_ledger.present(rid, key, "labor", "home", title=title or key, db_path=db)
    rec_ledger.record(rid, key, "dismissed", surface="home", db_path=db, meta={"kind": "not_for_us"})


def _k1(pct_ev=100, as_of_iso="2026-09-23"):
    return ce.assemble(ce.evidence(n=3, kind="night_facts"), ce.accuracy(None),
                       ce.freshness([{"key": "dsr", "pct": 95, "as_of_iso": as_of_iso, "basis": "report"}]))


def _is_k1(conf):
    return (isinstance(conf, dict) and {"pct", "band", "label", "reason", "score", "caution", "dimensions",
                                         "version"} <= set(conf)
            and {"evidence", "accuracy", "freshness"} <= set(conf["dimensions"]))


READ = ("Labor ran 31% over the last four weeks.\n\n"
        "1. Cut one server from Tuesday dinner.\n"
        "2. Move the Friday opener back an hour.\n")


# ══ T1 — the one outbound line ═════════════════════════════════════════════

def test_t1_the_outbound_label_is_the_k1_label_and_its_as_of():
    assert rec_trust.outbound_label(_k1()) == "70% confidence · data through 9/23/26"
    assert rec_trust.outbound_label(ce.unknown()) == "Confidence not yet measurable"
    assert rec_trust.outbound_label(None) == "" and rec_trust.outbound_label({"band": "high"}) == ""
    html = emails.report_confidence(_k1())
    assert "70% confidence · data through 9/23/26" in html and emails.report_confidence(None) == ""


# ══ T1 / T2 — the AI-read lines ════════════════════════════════════════════

def test_t1_every_read_line_carries_a_measured_confidence(db):
    rid = _rid(db)
    ev = {"n": 28, "kind": "trading_days", "coverage": 1.0, "basis": "28 days of shifts with sales"}
    items = client_api.insight_rec_items(rid, READ, "insight_labor", "labor", "labor", evidence=ev,
                                         sources=("labor",))
    assert len(items) == 2
    for it in items:
        c = it["confidence"]
        assert _is_k1(c)
        # a model wrote it: inferred caps evidence below high, and the basis says so
        assert c["dimensions"]["evidence"]["pct"] <= ce.PARTIAL_CAP
        assert c["dimensions"]["evidence"]["basis"].startswith("a model-written line from 28 days")
        assert c["pct"] is not None and c["label"].endswith("% confidence")
    # an unverified read: every line capped low, and no controls
    un = client_api.insight_rec_items(rid, READ, "insight_labor", "labor", "labor", evidence=ev,
                                      sources=("labor",), promote=False)
    assert all(u["confidence"]["dimensions"]["evidence"]["pct"] <= ce.UNVERIFIED_CAP for u in un)
    assert all(u["controls"] is False for u in un)
    # no evidence handed in: not measurable, never a guess
    bare = client_api.insight_rec_items(rid, READ, "insight_labor", "labor", "labor")
    assert all(b["confidence"]["pct"] is None for b in bare)


def test_t1_the_read_payloads_carry_the_line_confidence(db):
    rid = _rid(db)
    items = client_api.insight_rec_items(rid, READ, "insight_food", "food", "food",
                                         evidence={"n": 8, "kind": "weeks", "basis": "8 weeks of counts"},
                                         sources=("inventory",))
    flat = client_api.flat_recs(items)
    assert flat and all(_is_k1(r["confidence"]) for r in flat)
    html = client_api.format_insight_html(READ, rec_items=items, surface="food", module="food")
    assert 'class="rec-conf"' in html and items[0]["confidence"]["label"] in html
    js = mobile_api._insight_json(READ, items)
    assert len(js["insight_rec_confidence"]) == len(js["insight_recommendations"]) == 2
    assert all(_is_k1(c) for c in js["insight_rec_confidence"])


def test_t1_the_labor_read_evidence_is_the_shifts_it_was_written_from():
    ev = client_api.labor_read_evidence(1, {"is_live": True, "period_days": 28, "date_range": {"days": 28},
                                            "days_missing_sales": ["a", "b"]})
    assert ev["n"] == 26 and ev["kind"] == "trading_days" and "days_missing_sales" in ev["flags"]
    assert client_api.labor_read_evidence(1, {"is_live": False})["sample"] is True
    assert client_api.food_read_evidence(1, is_live=False)["sample"] is True


def test_t2_a_decline_on_home_drops_the_same_advice_from_a_read(db):
    rid = _rid(db)
    _decline(db, rid, "trim_day:Tuesday", "Trim Tuesday staffing")
    items = client_api.insight_rec_items(rid, READ, "insight_labor", "labor", "labor",
                                         evidence={"n": 28, "kind": "trading_days"}, sources=("labor",))
    tue = next(i for i in items if "Tuesday" in i["text"])
    fri = next(i for i in items if "Friday" in i["text"])
    assert tue["advice_signature"] == "labor:day:tuesday" and tue["answered"] is True
    assert fri["answered"] is False and fri["answerable"] is True
    assert [r["text"] for r in client_api.flat_recs(items)] == [fri["text"]]


# ══ T1 — Intel, What connects, Ask suggestions ═════════════════════════════

def test_t1_intel_recommendations_carry_confidence(db, monkeypatch):
    rid = _rid(db)
    import json
    blob = {"insight": "x", "competitors": [{"name": "A", "reviews": []}, {"name": "B", "reviews": []}],
            "generated_at": "2026-09-20"}
    c = models.get_conn(db)
    c.execute("UPDATE restaurants SET competitor_intel=? WHERE id=?", (json.dumps(blob), rid))
    c.commit()
    c.close()
    import competitor_intel_format
    monkeypatch.setattr(competitor_intel_format, "parse_competitor_intel", lambda t: {
        "recommendation_items": [{"text": "Add a weekday lunch special against A's.", "cites": []}],
        "withheld_recommendations": 0, "nothing_to_act_on": False, "unverified": None})
    p = client_api.intel_recs_payload(rid)
    assert p["recs"] and _is_k1(p["recs"][0]["confidence"])
    ev = p["recs"][0]["confidence"]["dimensions"]["evidence"]
    assert ev["kind"] == "competitors" and ev["n"] == 2


def test_t1_what_connects_links_carry_the_one_thing_cards_confidence(db):
    rid = _rid(db)
    link = {"kind": "reviews_x_labor", "subject": "service:Friday", "headline": "Friday complaints on lean Fridays",
            "modules": ["reviews", "labor"], "evidence": ["5 service complaints", "Friday 2 fewer staff"]}
    u = {"id": 1, "restaurant_id": rid, "role": "owner"}
    _ff, links = strategy_routes._present_cross_module(u, None, [link])
    assert links and _is_k1(links[0]["confidence"])
    # the same input as the one-thing candidate for the same link
    assert bi.link_evidence_input(link) == next(c for c in bi.one_thing_candidates(
        rid, {}, links=[link], db_path=db) if c["key"] == bi.link_key(link))["evidence_input"]
    assert links[0]["confidence"]["dimensions"]["evidence"]["pct"] <= ce.PARTIAL_CAP


def test_t1_t2_ask_suggestions_carry_confidence_and_honour_a_decline(db):
    rid = _rid(db)
    meta = {"modules_consulted": ["labor"],
            "confidence_detail": ce.assemble(ce.evidence(n=3, kind="evidence_items", basis="3 live reads"),
                                             ce.accuracy(None), ce.freshness(()))}
    answer = "Here is what I would do:\n1. Cut one server from Tuesday dinner.\n2. Post the Friday special tonight."
    got = ask_cavnar.record_suggestions(rid, answer, meta)
    assert len(got) == 2 and all(_is_k1(s["confidence"]) for s in got)
    assert got[0]["confidence"]["dimensions"]["evidence"]["n"] == 3
    _decline(db, rid, "trim_day:Tuesday")
    got = ask_cavnar.record_suggestions(rid, answer, meta)
    assert [s["text"] for s in got] == ["Post the Friday special tonight."]


# ══ T1 / T3 — the morning brief ════════════════════════════════════════════

def test_t1_the_brief_one_thing_carries_the_picks_confidence():
    src = inspect.getsource(morning_brief.build)
    assert '"confidence": f.get("confidence")' in src and "_attach_confidence(restaurant_id, lines" in src


def test_t1_brief_lines_carry_confidence_into_push_email_and_ledger(db):
    rid = _rid(db)
    lines = [{"key": "stock", "tone": "bad", "rec": "stock_low:Salmon", "recs": ["stock_low:Salmon"],
              "text": "Running low: Salmon."},
             {"key": "reviews", "tone": "bad", "rec": "no_response", "text": "3 reviews waiting on a reply."}]
    morning_brief._attach_confidence(rid, lines, db)
    # Group P item 3: the items running low on the last counts are a FACT,
    # like the replies waiting — neither carries a confidence.
    assert "confidence" not in lines[0] and "confidence" not in lines[1]
    slow = [{"key": "slow_day", "rec": "slow_day:Tuesday", "_samples": 6, "text": "Tuesday runs slow."}]
    morning_brief._attach_confidence(rid, slow, db)
    assert _is_k1(slow[0]["confidence"])
    brief = {"date": "2026-09-24", "restaurant_id": rid,
             "lines": [dict(lines[0], confidence=_k1()), lines[1]]}
    push = morning_brief.push_text(brief, "R")
    assert "(70% confidence · data through 9/23/26)" in push["body"]
    html = morning_brief._email_html(brief, "R")
    assert "70% confidence · data through 9/23/26" in html
    items = morning_brief.line_items(brief["lines"])
    assert any(isinstance(i.get("confidence"), dict) for i in items)


def test_t3_the_brief_footer_says_measured_only_when_every_line_is():
    only = [{"key": "yesterday", "text": "x"}]
    assert morning_brief.footer_source(only) == "Every figure above is measured from your own data."
    for line, word in (({"key": "prime_cost", "claim_kind": "forecast"}, "the prime-cost projection (a projection)"),
                       ({"key": "money", "claim_kind": "opportunity"}, "the dollar opportunity (an estimate)"),
                       ({"key": "fix_first", "claim_kind": "inferred"}, "the one thing (an inference)"),
                       ({"key": "today", "forecast": True}, "today's forecast (a projection)")):
        s = morning_brief.footer_source(only + [line])
        assert word in s and "Every figure above is measured from your own data." not in s
    assert "weather" in morning_brief.footer_source(only + [{"key": "today", "outside": True}])


# ══ T1 — the group view ════════════════════════════════════════════════════

def test_t1_group_attention_advice_carries_confidence_and_facts_none(db):
    rid = _rid(db)
    issues = [{"severity": "important", "text": "Labor 34.0% — 4.0 pts over target", "module": "labor",
               "kind": "labor_over"},
              {"severity": "important", "text": "2 items critically low", "module": "inventory",
               "kind": "critical_low"},
              {"severity": "important", "text": "Rating slipped", "module": "reviews", "kind": "rating_drop"},
              {"severity": "critical", "text": "2 urgent reviews unanswered", "module": "reviews"}]
    home_brief._group_issue_confidence(rid, issues, {"overtime": 0}, {"critical_low": 2}, {"n30": 9},
                                       {"n": 20, "kind": "trading_days", "basis": "20 days"})
    # Items critically low are a FACT, as on the location's own Home (group
    # P item 3): no confidence; labor over target and a rating slip keep it.
    assert _is_k1(issues[0]["confidence"]) and _is_k1(issues[2]["confidence"])
    assert "confidence" not in issues[1] and "confidence" not in issues[3]
    src = inspect.getsource(home_brief._location_record)
    assert "_group_issue_confidence(rid, issues" in src


# ══ T1 — DSR, weekly/monthly one thing ═════════════════════════════════════

def test_t1_dsr_email_actions_keep_their_confidence(db):
    rid = _rid(db)
    payload = {"business_date": "2026-09-23", "facts": {"blocks": {}}, "view": "owner",
               "narrative": {"actions_tomorrow": [{"text": "Cut one server from Tuesday dinner.",
                                                   "key": "dsr_action:adjust_staffing:labor", "confidence": _k1()}]}}
    r = types.SimpleNamespace(location_name=None, name="R")
    d = dsr_deliver.digest(payload, r)
    assert _is_k1(d["actions"][0]["confidence"])
    assert d["actions"][0]["confidence_label"] == "70% confidence · data through 9/23/26"
    _subject, html, _pre = emails.dsr_email(d)
    assert "70% confidence · data through 9/23/26" in html


def test_t1_the_one_thing_email_block_prints_its_confidence():
    out = []
    emails._one_thing_block(out, {"key": "trim_day:Tuesday", "what": "Trim Tuesday", "confidence": _k1()},
                            "weekly_email", "this week")
    assert "70% confidence · data through 9/23/26" in out[0]


# ══ T1 / T2 / T3 / T7 — the weekly digest ══════════════════════════════════

def test_t1_the_digest_prompt_states_k1_never_the_model_band():
    assert reporter.digest_confidence_text({"confidence": "high", "confidence_detail": _k1()}) == "70% confidence"
    assert reporter.digest_confidence_text({"confidence": "high"}) == "confidence not yet measurable"
    src = inspect.getsource(reporter.generate_ai_digest_summary)
    assert "digest_confidence_text(_d0)" in src and "{_d0['confidence']} confidence" not in src
    # T7: the date the model is handed is M/D/YY
    assert "%B %d, %Y" not in src


def _digest(db, monkeypatch, rid, action):
    monkeypatch.setattr(reporter, "generate_ai_digest_summary",
                        lambda *a, **k: {"headline": "A steady week.", "action": action})
    report = models.WeeklyReport(restaurant_id=rid, period_start="9/17/26", period_end="9/24/26")
    import rec_delivery
    with rec_delivery.collect() if hasattr(rec_delivery, "collect") else _null():
        return "".join(reporter._digest_parts(report, "R", "Owner", rid, owner_view=False)["sections"])


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_t1_t2_t3_the_digest_move_its_confidence_its_decline_and_a_week_with_no_reviews(db, monkeypatch):
    rid = _rid(db, module_labor=0, module_inventory=0, module_marketing=0)
    html = _digest(db, monkeypatch, rid, "Cut one server from Tuesday dinner.")
    assert "This week&#x27;s move" in html or "This week's move" in html
    assert "% confidence" in html or "Confidence not yet measurable" in html
    # no reviews this week: no 0.0★ and no "Needs work"
    assert "0.0&#9733;" not in html and "Needs work" not in html and "No reviews this week" in html
    _decline(db, rid, "trim_day:Tuesday")
    html = _digest(db, monkeypatch, rid, "Cut one server from Tuesday dinner.")
    assert "Cut one server from Tuesday dinner." not in html


def test_t3_digest_labor_and_waste_tags():
    B = emails.BRAND
    assert reporter.labor_tag(27.0, 26.0, B)[1] == "Watch closely"     # was "On target" at a hard 32
    assert reporter.labor_tag(25.0, 26.0, B)[1] == "On target"
    # a target is not a budget (NS3 L5): "budget" is only dsr_budgets' word
    assert reporter.labor_tag(35.0, 26.0, B)[1] == "Over target"
    assert reporter.waste_tag(0, B)[2] == "Not measured" and reporter.waste_tag(0, B)[1] == B["muted"]
    assert reporter.waste_tag(120, B)[2] == "Low waste"
    src = inspect.getsource(reporter._digest_parts)
    # The one target read, with its source (re-audit #10) — this pinned
    # notify.labor_target_for, which knows no provenance.
    assert "lp <= 32" not in src and 'target_for(_rest, "labor")' in src


def test_t3_the_monthly_email_uses_the_value_sections_headings(db, monkeypatch):
    rid = _rid(db)
    import monthly_review
    import value_delivered as vd
    monkeypatch.setattr(monthly_review, "build", lambda *a, **k: {
        "month": "August 2026", "months": 1, "metrics": [], "results": [], "goals": [],
        "compared_with": "July", "priorities": [], "fix_first": None})
    monkeypatch.setattr(vd, "breakdown", lambda *a, **k: {
        "delivered": {"in_flight": 0, "caveat": "Associated, not proven.", "biggest": None},
        "avoided": {"hours": 12.0}, "surfaced": {"dollars": 900.0, "alerts": 3},
        "opportunity": {"monthly": 1500.0}})
    monkeypatch.setattr(vd, "value_lines", lambda d: ["$400/month measured in labor."])
    html = "".join(emails._monthly_review_sections(rid))
    assert "What Cavnar AI has been worth" not in html
    m, s = html.index(vd.VALUE_SECTIONS[0]["heading"]), html.index(vd.VALUE_SECTIONS[1]["heading"])
    assert m < html.index("$400/month measured") < s < html.index("still on the table")
    assert html.index("of problems put in front of you") > s


# ══ T1 — the rating-trend alert ════════════════════════════════════════════

def test_t1_the_rating_trend_alert_states_a_measured_figure(monkeypatch):
    import review_intelligence as ri
    series = [{"count": 4, "avg_rating": v} for v in (4.6, 4.5, 4.6, 4.3, 4.1)]
    monkeypatch.setattr(ri, "rating_trend", lambda *a, **k: {
        "direction": "declining", "confidence": "medium", "first": 4.6, "latest": 4.1, "series": series})
    neg = notify._negative_trend(1)
    assert neg["moves"] == 4 and neg["moves_down"] == 3
    assert notify.trend_measure_text(neg) == "3 of 4 week-to-week moves down"
    assert "trend strength 72%" in notify.trend_measure_text(dict(neg, trend_strength_pct=72.4))
    src = inspect.getsource(notify)
    assert "({neg['confidence']} confidence)" not in src and "trend_measure_text(neg)" in src


# ══ T1 / T2 — the quiet-night push ═════════════════════════════════════════

def test_t1_t2_the_quiet_night_push_is_declined_by_signature_and_carries_its_label(db):
    rid = _rid(db)
    key, title = "quiet_night:2026-09-30", "Wednesday is usually your quietest night"
    assert strategy_jobs.quiet_night_declined(rid, key, title, db_path=db) is False
    _decline(db, rid, "slow_day:Wednesday", "Wednesdays run slow")
    assert insight_store.advice_signature(key, title) == insight_store.advice_signature(
        "slow_day:Wednesday", "Wednesdays run slow")
    assert strategy_jobs.quiet_night_declined(rid, key, title, db_path=db) is True
    conf = strategy_jobs.quiet_night_confidence(rid, key, {"samples": 6, "weekday": "Wednesday"}, db_path=db)
    assert _is_k1(conf) and conf["dimensions"]["evidence"]["kind"] == "weekdays"
    src = inspect.getsource(strategy_jobs.run_demand_opportunity)
    assert src.index("quiet_night_declined(") < src.index("ops.claim_period(")
    assert "outbound_label(conf)" in src


# ══ T2 — Shift Quality items and the whole-schedule card ═══════════════════

def test_t2_shift_quality_items_honour_a_decline_elsewhere(db, monkeypatch):
    rid = _rid(db)
    import shift_quality
    recs = ["Trim about 6h from Tuesday night — it ran over target.", "Trim about 4h from Monday lunch."]
    monkeypatch.setattr(schedule_engine, "_quality_signals", lambda *a, **k: ({}, None))
    monkeypatch.setattr(shift_quality, "score_rows", lambda *a, **k: {"checked": False, "recommendations": list(recs),
                                                                       "confidence": {"score": 90, "reasons": []}})
    q, _ = schedule_engine._score_schedule_quality(rid, [], {})
    assert len(q["recommendation_items"]) == 2
    _decline(db, rid, "trim_day:Tuesday")
    q, _ = schedule_engine._score_schedule_quality(rid, [], {})
    texts = [i["text"] for i in q["recommendation_items"]]
    assert texts == [recs[1]] and q["recommendations"] == [recs[1]]


def test_t2_a_one_day_trim_decline_leaves_the_whole_schedule_card():
    day = insight_store.advice_signature("trim_day:Tuesday")
    whole = insight_store.advice_signature("schedule_to_target:30%",
                                           "Build the next schedule to your 30% target, starting with Tuesday")
    assert day == "labor:day:tuesday" and whole == "labor:schedule:whole" and day != whole


# ══ T4 — two keys for two findings ═════════════════════════════════════════

def test_t4_homes_negative_share_is_not_the_slope_alerts_key():
    src = inspect.getsource(home_brief)
    assert 'add_attn("negative_share"' in src and 'add_attn("negative_trend"' not in src
    assert rec_ledger.KIND_TOPIC.get("negative_share") == "guest_experience"
    assert '"negative_trend")' in inspect.getsource(notify)      # the alert keeps its own


# ══ T5 — demand accuracy's sign ════════════════════════════════════════════

def test_t5_demand_accuracy_names_its_sign(db):
    rid = _rid(db)
    today = date(2026, 9, 24)
    c = models.get_conn(db)
    for i, p in enumerate([10, 12, 8, 9, 11, 10, 10], start=1):
        c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,'ready')",
                  (rid, (today - timedelta(days=i)).isoformat(), "sales.vs_forecast_pct", p))
    c.commit()
    c.close()
    acc = demand.demand_accuracy(rid, today=today, db_path=db)
    assert acc["actual_vs_forecast_pct"] == 10.0 == acc["bias_pct"]
    assert acc["bias_direction"] == "above_forecast"
    assert "above the forecast" in acc["bias_reading"] and "forecast runs low" in acc["bias_reading"]


# ══ T6 — the prime-cost projection on an often-wide record ═════════════════

def test_t6_the_prime_cost_projection_is_withheld_on_an_often_wide_record(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(fci, "forecast_accuracy", lambda *a, **k: {
        "available": True, "withheld": True, "reading": "often wide",
        "reason": "past forecasts here missed by 41% on average over 4 months, so the next one is not shown"})
    out = fci.profitability_projection(rid, db_path=db)
    assert out["available"] is False and out["withheld"] is True
    assert "prime_cost_pct" not in out and "not shown" in out["reason"]
    # the nightly freeze still records (a record that stops being scored never recovers)
    assert "withhold=False" in inspect.getsource(fci.record_profitability_forecast)


# ══ T8 ═════════════════════════════════════════════════════════════════════

def test_t8_food_drivers_carry_their_rec_key(db):
    rid = _rid(db)
    drivers = [{"kind": "price", "item": "Salmon", "label": "Salmon price up 12%", "price_weeks": 3},
               {"kind": "waste", "item": "Lettuce", "label": "Lettuce waste above tolerance", "weeks_of_data": 2}]
    fci._driver_confidence(rid, drivers, db_path=db)
    assert drivers[0]["rec_key"] == "price_spike:Salmon"
    assert drivers[1]["rec_key"] == bi.driver_key(drivers[1])


def test_t8_the_webs_schedule_surface_is_a_known_ledger_surface():
    assert rec_ledger.known_surface("schedule") == "schedule_review"
    assert rec_ledger.known_surface("nope") == "unknown" and rec_ledger.known_surface("nope", "home") == "home"
    assert rec_ledger.known_surface("home") == "home"
    src = inspect.getsource(strategy_routes._do_rec_event)
    assert "known_surface(" in src


def test_t8_the_dsr_verification_counts_why_lines_were_dropped():
    src = inspect.getsource(dsr_narrative)
    assert '"failed_check": n_failed_check' in src and '"withheld_answered"' in src


# ══ T9 — one read state on every Labor path ════════════════════════════════

def _labor_app(db, monkeypatch, rid):
    from flask import Flask
    import auth
    user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "username": "o", "role": "owner",
            "is_admin": 0, "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    return app


def test_t9_the_cached_labor_read_carries_its_state_and_line_confidence(db, monkeypatch):
    import labor
    rid = _rid(db)
    app = _labor_app(db, monkeypatch, rid)
    client_api._insight_cache["labor-insight:" + str(rid)] = (datetime.utcnow(), READ)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r: {
        "is_live": True, "period_days": 28, "date_range": {"days": 28}, "total_sales": 1000.0,
        "overall_labor_pct": 25.0, "labor_target": 30})
    body = app.test_client().get("/api/labor-insight").get_json()
    assert body["stale"] is False and body["as_of"] and "age_days" in body
    assert body["recs"] and all(_is_k1(r["confidence"]) for r in body["recs"])
    assert body["recs"][0]["confidence"]["dimensions"]["evidence"]["n"] == 28
    # the fallback: the same fields, stale
    client_api._insight_cache["labor-insight:" + str(rid)] = (datetime.utcnow() - timedelta(hours=30), READ)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r: (_ for _ in ()).throw(RuntimeError("x")))
    body = app.test_client().get("/api/labor-insight").get_json()
    assert body["stale"] is True and "From a read on" in body["stale_note"] and body["as_of"]
