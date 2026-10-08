"""Blind re-audit of the Places / AI visibility / diagnosis work (10/7/26).

P1  the stored AI-visibility check is read on open (web and iOS) and drawn
    honestly — not measured stays the idle state, never a 0.
P2  the daily competitor check re-reads only for a new complaint, a
    competitor's newest five all new, a burst or a closure, and at most once
    every REANALYSE_MIN_DAYS.
P3  the batched competitor read: the daily pass skips one still out and
    records nothing until it lands (the rest is in test_ai_cost_batches_1007).
P4  the food diagnosis's evidence hash ignores cents and prose.
P6  an owner-added competitor's Places content is read again inside 30 days.
P7  a closed owner-added competitor leaves the owner's list (permanently) or
    the cap (temporarily); a removal always updates the stored read's list.
P9  the competitor read's rules are a cached system block; an owner's
    Refresh thread carries the owner's attribution.
(P5 is in test_ai_cost_sched_1007; P8 and the P6 city in test_aivis_cost_1007.)
"""
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

import admin_routes  # noqa: F401  (bound imports: imported before any patch)
import ai_utils
import competitor
import models
import scheduler
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    return open(os.path.join(ROOT, path), encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _set(db_path, rid, **cols):
    c = sqlite3.connect(db_path)
    c.execute(f"UPDATE restaurants SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), rid))
    c.commit()
    c.close()


def _rid(db_path, place_id="own", **cols):
    rid = create_restaurant(Restaurant(name="Reaudit Co", owner_email="r@x.test", google_place_id=place_id,
                                       timezone="America/Chicago"), db_path=db_path)
    if cols:
        _set(db_path, rid, **cols)
    return rid


def _tracked(db_path, competitors, **cols):
    rid = _rid(db_path, **cols)
    blob = {"competitors": competitors, "insight": "read", "generated_at": "2026-10-01",
            "discovered_at": _ago(days=3), "custom_ids": []}
    _set(db_path, rid, competitor_intel=json.dumps(blob))
    return rid


def _blob(rid):
    return json.loads(models.get_restaurant(rid).competitor_intel)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _fake_places(answers):
    calls = []

    def fake(endpoint, params, restaurant_id=None, action=None, timeout=10):
        calls.append((endpoint, params.get("place_id"), action, params.get("fields")))
        return _Resp({"status": "OK", "result": answers.get(params.get("place_id"), {})})
    return fake, calls


def _review(ts, stars=5, text="Great"):
    return {"author_name": "Ann", "rating": stars, "text": text, "time": ts, "relative_time_description": "a day ago"}


RIVAL = {"name": "Rec Haus", "place_id": "p1", "rating": 4.5, "review_count": 300,
         "reviews": [{"text": "old", "ts": 1_700_000_000}]}


# ── P2: what re-reads, and how often ────────────────────────────────────────

def test_a_new_low_star_review_asks_for_a_new_read(db_path, monkeypatch):
    rid = _tracked(db_path, [dict(RIVAL)])
    fake, _calls = _fake_places({"p1": {"rating": 4.5, "user_ratings_total": 301, "business_status": "OPERATIONAL",
                                        "reviews": [_review(1_700_000_500, stars=2, text="Cold food")]}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.check_ratings(rid)
    assert out["reanalyse"] is True and out["held"] is False
    assert any("a new 2★ review" in t for t in out["triggers"])


def test_a_competitors_whole_visible_set_turning_over_asks_for_a_new_read(db_path, monkeypatch):
    rid = _tracked(db_path, [dict(RIVAL)])
    fresh = [_review(1_700_000_000 + 100 * i) for i in range(1, competitor.NEWER_TURNOVER + 1)]
    fake, _calls = _fake_places({"p1": {"rating": 4.5, "user_ratings_total": 305, "business_status": "OPERATIONAL",
                                        "reviews": fresh}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.check_ratings(rid)
    assert out["reanalyse"] is True and any("5 reviews since the last read" in t for t in out["triggers"])


def test_the_thresholds_are_the_documented_ones():
    assert (competitor.LOW_STAR, competitor.NEWER_TURNOVER, competitor.REANALYSE_MIN_DAYS,
            competitor.REVIEW_BURST) == (2, 5, 3, 15)
    doc = _read("SYSTEM_ARCHITECTURE.md")
    assert "`LOW_STAR`" in doc and "`NEWER_TURNOVER`" in doc and "`REANALYSE_MIN_DAYS` (3)" in doc


def test_a_review_trigger_waits_out_the_cap_and_a_closure_does_not(db_path, monkeypatch):
    rid = _tracked(db_path, [dict(RIVAL), {"name": "Gone Diner", "place_id": "p2", "rating": 4.0,
                                           "review_count": 50}],
                   competitor_updated_at=_ago(days=1))
    fake, _calls = _fake_places({"p1": {"rating": 4.5, "user_ratings_total": 301, "business_status": "OPERATIONAL",
                                        "reviews": [_review(1_700_000_500, stars=1)]},
                                 "p2": {"rating": 4.0, "user_ratings_total": 50, "business_status": "OPERATIONAL"}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.check_ratings(rid)
    assert out["triggers"] and out["held"] is True and out["reanalyse"] is False
    # Past the cap the same unread complaint asks again.
    _set(db_path, rid, competitor_updated_at=_ago(days=competitor.REANALYSE_MIN_DAYS, hours=1))
    assert competitor.check_ratings(rid)["reanalyse"] is True
    # A closure inside the cap is not held.
    _set(db_path, rid, competitor_updated_at=_ago(hours=5))
    fake2, _c = _fake_places({"p1": {"rating": 4.5, "user_ratings_total": 301, "business_status": "OPERATIONAL"},
                              "p2": {"rating": 4.0, "user_ratings_total": 50,
                                     "business_status": "CLOSED_PERMANENTLY"}})
    monkeypatch.setattr(competitor, "_places_request", fake2)
    out = competitor.check_ratings(rid)
    assert out["reanalyse"] is True and out["held"] is False


# ── P3: the daily pass and a read still out ─────────────────────────────────

def _daily_ready(db_path, monkeypatch):
    rid = _rid(db_path, place_id="ChIJdaily")
    monkeypatch.setattr(models, "is_full_tier", lambda r: True)
    monkeypatch.setattr(models, "in_service", lambda r: True)
    recorded = []
    monkeypatch.setattr(scheduler, "_record", lambda r, src, ok, **k: recorded.append((r, ok)))
    return rid, recorded


def test_the_daily_pass_skips_a_restaurant_whose_batched_read_is_out(db_path, monkeypatch):
    import ai_batches
    rid, _rec = _daily_ready(db_path, monkeypatch)
    monkeypatch.setattr(ai_batches, "open_items", lambda wf, r=None: [competitor.batch_custom_id(r)] if r == rid else [])
    monkeypatch.setattr(competitor, "check_ratings", lambda r: pytest.fail("checked while its read was out"))
    out = scheduler.run_daily_competitor_ratings()
    assert out["skipped"] >= 1 and out["attempted"] == 0


def test_a_batched_daily_reread_is_not_recorded_until_it_lands(db_path, monkeypatch):
    import ai_batches
    rid, recorded = _daily_ready(db_path, monkeypatch)
    monkeypatch.setattr(ai_batches, "open_items", lambda *a, **k: [])
    monkeypatch.setattr(competitor, "check_ratings", lambda r: {"ok": True, "reanalyse": True, "triggers": ["x"]})
    monkeypatch.setattr(competitor, "run_competitor_analysis",
                        lambda r: {"ok": True, "batched": True, "competitors_analyzed": 3})
    out = scheduler.run_daily_competitor_ratings()
    assert out["batched"] == 1 and out["reanalysed"] == 0 and recorded == []
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda r: {"ok": False, "error": "Places refused"})
    scheduler.run_daily_competitor_ratings()
    assert recorded == [(rid, False)]


# ── P4: the food diagnosis's evidence hash ──────────────────────────────────

def _drivers(*rows):
    return {"drivers": [dict(kind=k, label=l, item=i, dollars_monthly=d, evidence=e) for k, l, i, d, e in rows]}


def test_the_food_evidence_hash_ignores_cents_and_prose():
    import food_cost_intelligence as fci
    base = _drivers(("price", "Salmon price up 12%", "Salmon", 412.37, "Salmon moved $9.10 to $10.20 this week"),
                    ("waste", "Basil waste above tolerance", "Basil", 120.0, "Basil wasted 14% 9/30-10/6"))
    nightly = _drivers(("price", "Salmon price up 13%", "Salmon", 409.81, "Salmon moved $9.10 to $10.31 this week"),
                       ("waste", "Basil waste above tolerance", "Basil", 121.4, "Basil wasted 15% 10/1-10/7"))
    assert fci.driver_evidence_hash(nightly) == fci.driver_evidence_hash(base), \
        "a rolling 28-day figure moving by cents rewrote the read every day"
    # A real move, a new driver or a new order is new evidence.
    moved = _drivers(("price", "Salmon price up 30%", "Salmon", 560.0, "e"),
                     ("waste", "Basil waste above tolerance", "Basil", 120.0, "e"))
    swapped = _drivers(("waste", "Basil waste above tolerance", "Basil", 120.0, "e"),
                       ("price", "Salmon price up 12%", "Salmon", 412.37, "e"))
    new = _drivers(("price", "Salmon price up 12%", "Salmon", 412.37, "e"),
                   ("waste", "Basil waste above tolerance", "Basil", 120.0, "e"),
                   ("menu", "Pasta runs at 38% food cost", None, 90.0, "e"))
    for other in (moved, swapped, new):
        assert fci.driver_evidence_hash(other) != fci.driver_evidence_hash(base)
    assert fci.DRIVER_DOLLAR_BAND == 1.3


def test_a_label_with_figures_is_read_without_them():
    import food_cost_intelligence as fci
    assert fci._driver_identity({"label": "Pasta runs at 31.5% food cost"}) == \
        fci._driver_identity({"label": "Pasta runs at 33% food cost"})
    assert fci._dollar_band(0) == 0 and fci._dollar_band(412) != fci._dollar_band(560)


# ── P6 / P7: owner-added competitors ────────────────────────────────────────

def _quiet_run(monkeypatch):
    import webhooks
    monkeypatch.setattr(competitor, "generate_competitor_insight", lambda *a, **k: "A grounded insight.")
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)


def _stored(pid, name, **kw):
    c = {"place_id": pid, "name": name, "rating": 4.2, "review_count": 90, "match_basis": "nearby",
         "reviews": [], "reviews_at": _ago(hours=1)}
    c.update(kw)
    return c


def _with_custom(db_path, custom_entry, closed_custom=(), custom="c1"):
    rid = _tracked(db_path, [_stored("p1", "Rec Haus")] + ([custom_entry] if custom_entry else []))
    _set(db_path, rid, custom_competitors=custom)
    blob = _blob(rid)
    blob["custom_ids"] = [p for p in custom.split(",") if p]
    blob["closed_custom"] = list(closed_custom)
    _set(db_path, rid, competitor_intel=json.dumps(blob))
    return rid


def test_an_owner_added_competitors_places_content_is_read_again_past_28_days(db_path, monkeypatch):
    _quiet_run(monkeypatch)
    entry = _stored("c1", "Custom Rival", custom=True, vicinity="1 Main St", price_level=2,
                    latest_reviews=[], latest_reviews_at=_ago(hours=2), custom_fields_at=_ago(days=3))
    rid = _with_custom(db_path, entry)
    answer = {"c1": {"name": "Custom Rival", "business_status": "OPERATIONAL", "rating": 4.1,
                     "user_ratings_total": 41, "vicinity": "1 Main St", "price_level": 3,
                     "reviews": [_review(1_700_000_000)]}}
    fake, calls = _fake_places(answer)
    monkeypatch.setattr(competitor, "_places_request", fake)
    competitor.run_competitor_analysis(rid)
    assert [c for c in calls if c[1] == "c1"] == [], "inside 28 days the morning's read is carried"
    blob = _blob(rid)
    blob["competitors"][1]["custom_fields_at"] = _ago(days=competitor.REDISCOVER_DAYS)
    _set(db_path, rid, competitor_intel=json.dumps(blob))
    out = competitor.run_competitor_analysis(rid)
    assert [(c[1], c[3]) for c in calls] == [("c1", competitor.CUSTOM_FIELDS)]
    custom = next(c for c in out["competitors"] if c["place_id"] == "c1")
    assert custom["price_level"] == 3 and custom["custom_fields_at"]


def test_a_permanently_closed_owner_added_competitor_leaves_the_owners_list(db_path, monkeypatch):
    _quiet_run(monkeypatch)
    rid = _with_custom(db_path, None, custom="c1,c2")
    fake, _calls = _fake_places({
        "c1": {"name": "Rosie's", "business_status": "CLOSED_PERMANENTLY"},
        "c2": {"name": "Open Rival", "business_status": "OPERATIONAL", "rating": 4.0, "user_ratings_total": 30}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.run_competitor_analysis(rid)
    assert [c["name"] for c in out["closed_custom"]] == ["Rosie's"]
    assert models.get_restaurant(rid).custom_competitors == "c2"
    assert _blob(rid)["custom_ids"] == ["c2"]
    assert competitor._discovery_due(_blob(rid), competitor._capped_custom_ids(models.get_restaurant(rid))) is False


def test_a_temporarily_closed_one_stays_and_counts_toward_nothing(db_path):
    ids = ",".join(f"c{i}" for i in range(competitor.CUSTOM_COMPETITORS_MAX + 1))
    rid = _with_custom(db_path, None, custom=ids,
                       closed_custom=[{"place_id": "c0", "name": "Paused", "status": "CLOSED_TEMPORARILY"}])
    capped = competitor._capped_custom_ids(models.get_restaurant(rid))
    assert len(capped) == competitor.CUSTOM_COMPETITORS_MAX + 1 and "c0" in capped
    src = _read("mobile_api.py")
    add = src[src.index("def mobile_add_competitor("):src.index("def mobile_remove_competitor(")]
    assert "_paused_custom_ids" in add and "not in paused" in add


def test_removing_one_that_is_not_in_the_comparison_still_updates_the_reads_list(db_path):
    rid = _with_custom(db_path, None, custom="c1",
                       closed_custom=[{"place_id": "c1", "name": "Paused", "status": "CLOSED_TEMPORARILY"}])
    assert competitor.remove_competitor_from_cache(rid, "c1") is True
    blob = _blob(rid)
    assert blob["custom_ids"] == [] and blob["closed_custom"] == []
    assert competitor.remove_competitor_from_cache(rid, "nope") is False


# ── P9: the cached rules and the Refresh's attribution ──────────────────────

def test_the_competitor_read_sends_its_rules_as_a_cached_system_block():
    system = competitor.INSIGHT_SYSTEM
    assert len(system) > 4600, "under the 1,024-token floor a prefix is never cached"
    assert not re.search(r"\{[a-z_]+\}", system), "an unformatted placeholder"
    assert "EVIDENCE RULES" in system and "CRITICAL RULES" in system and "HOW THE MESSAGE IS LAID OUT" in system
    req = competitor._insight_request("MESSAGE")
    assert req["system"] == [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    assert req["messages"] == [{"role": "user", "content": "MESSAGE"}]
    assert competitor.insight_validation_text("MESSAGE").startswith(system)


def test_the_message_is_the_restaurants_own_and_carries_no_rules(monkeypatch):
    monkeypatch.setattr(competitor, "_insight_readiness", lambda rid: {})
    prompt, _ready = competitor._insight_prompt("Gia Mia", [dict(RIVAL, reviews=[{"rating": 2, "text": "Slow"}])],
                                                owner_name="Erik")
    assert prompt.startswith("RESTAURANT: Gia Mia")
    assert "Hi Erik, here is your competitive landscape snapshot." in prompt
    assert "EVIDENCE RULES" not in prompt and "CRITICAL RULES" not in prompt
    assert "Gia Mia" not in competitor.INSIGHT_SYSTEM


def test_an_owners_refresh_thread_runs_under_the_owners_attribution(monkeypatch):
    import inspect
    src = inspect.getsource(admin_routes.start_competitor_job)
    assert "attributed(_run_competitor_job)" in src
    seen = []
    monkeypatch.setattr(ai_utils, "attribution_for_thread",
                        lambda: {"trigger": "owner", "actor_user_id": 42, "correlation_id": None})

    def job(job_id, rid):
        seen.append(ai_utils.current_ai_context())
    wrapped = ai_utils.attributed(job)
    wrapped("j", 1)
    assert seen[0]["trigger"] == "owner" and seen[0]["actor_user_id"] == 42


# ── P1: both clients read the stored check on open ──────────────────────────

def test_the_web_reads_the_stored_check_when_intel_opens():
    html = _read("templates/dashboard.html")
    opener = html[html.index("window.switchIntelTab = function(tab) {"):]
    opener = opener[:opener.index("\n};\n")]
    assert "aivLoadStored();" in opener
    loader = html[html.index("function aivLoadStored() {"):html.index("function runAIVisibility(btn) {")]
    assert "fetch('/api/ai-visibility', {credentials: 'same-origin'})" in loader and "POST" not in loader
    assert "d.state === 'not_measured'" in loader and "renderAIVisibility(d)" in loader
    assert "dr-pulse" in loader, "the sliding pulse while it loads, never a spinner"
    assert 'id="aiv-stored-state"' in html
    # A missing measurement is a dash on the orbit, never 0.
    orbit = html[html.index("window.renderVisibilityOrbit=function(score,runs){"):]
    assert "Math.round(score||0)" not in orbit[:orbit.index("if(hasTrend){")]


def test_a_failed_check_keeps_the_recorded_one_on_the_web():
    html = _read("templates/dashboard.html")
    run = html[html.index("function runAIVisibility(btn) {"):html.index("function aivRangeReading(")]
    assert "var _aivPrev = _aivData;" in run and "renderAIVisibility(_aivPrev)" in run
    assert "document.getElementById('aiv-idle').innerHTML" not in run, "the error no longer wipes the Check button"


def test_the_phone_reads_the_stored_check_on_appear():
    vm = _read("ios/CavnarAI/CavnarAI/Features/Intel/AIVisibilityViewModel.swift")
    load = vm[vm.index("func loadStored() async {"):vm.index("var history: [AIVisibilityRun]")]
    assert 'client.send(\n            "/mobile/api/intel/ai-visibility", hapticOnError: false)' in load
    assert "method: .post" not in load and "isNotMeasured" in load
    assert "case ok, error, queries, checklist, partial, city, roadmap, cached, state, measured, reason" in vm
    assert 'state?.value == "not_measured"' in vm
    section = _read("ios/CavnarAI/CavnarAI/Features/Intel/AIVisibilitySection.swift")
    task = section[section.index(".task {"):section.index("private var storedState")]
    assert task.index("viewModel.loadStored()") < task.index("viewModel.loadHistory()")
    assert "Not measured yet." in section and "viewModel.checkError" in section
