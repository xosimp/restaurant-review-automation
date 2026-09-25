"""Re-audit M: review replies, trust, recommendation keys, food-cost dollars,
marketing credit and owner-facing copy.

Each test names the finding it pins (M-1 … M-33, plus the H items folded in).
No model, network, email, SMS or push is reached: every model call is stubbed.
"""
import json
import os
import re
import sqlite3
import types
from datetime import date, datetime, timedelta

import pytest

import client_api
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    import gmb
    monkeypatch.setattr(gmb, "is_connected", lambda rid: False)
    client_api._insight_cache.clear()
    yield
    client_api._insight_cache.clear()


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Gia Mia"), owner_email="o@x.test", **kw),
                             db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


_n = [0]


def _review(db_path, rid, *, status="drafted", draft="Thanks so much!", rating=4, days_ago=1,
            urgency="normal", flagged=0, reason=None, **cols):
    _n[0] += 1
    c = _conn(db_path)
    fields = {"restaurant_id": rid, "platform": "google", "external_id": f"r{_n[0]}", "author": "Ann",
              "rating": rating, "text": "Nice night", "processed": 1, "response_status": status,
              "draft_response": draft, "urgency": urgency, "draft_needs_review": flagged,
              "draft_review_reason": reason,
              "review_date": (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S"),
              "fetched_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}
    fields.update(cols)
    rv = c.execute(f"INSERT INTO reviews ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                   tuple(fields.values())).lastrowid
    c.commit(); c.close()
    return rv


# ── M-1 the web card shows the reply guard's flag ──────────────────────────

def test_m1_web_review_card_renders_the_flag_and_approve_asks_first(db_path):
    from flask import Flask
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    from time_utils import mdy
    app.jinja_env.filters.setdefault("format_date", lambda v, *a, **k: mdy(v) if v else "")
    rid = _rid(db_path)
    rv = _review(db_path, rid, draft="Dinner is on us next time.", flagged=1,
                 reason="states a specific action the restaurant may not have taken: on us")
    r = [x for x in models.get_reviews_data(rid) if x["id"] == rv][0]
    with app.test_request_context():
        from flask import render_template
        html = render_template("_review_card.html", r=r, restaurant=models.get_restaurant(rid), delay=0)
    assert 'id="draft-flag-%d"' % rv in html
    assert "Read this one before you post it" in html and "on us" in html
    dash = _read("templates", "dashboard.html")
    body = dash[dash.index("function approveR(id){"):]
    body = body[:body.index("\n}\n")]
    assert "_revFlagOk(id)" in body
    save = dash[dash.index("function saveDraft(id) {"):]
    save = save[:save.index("\n}\n")]
    assert "sd.needs_review" in save and "_revFlagOk" in save


def test_m1_regenerate_returns_the_guards_verdict(db_path, monkeypatch):
    import drafter
    rid = _rid(db_path)
    rv = _review(db_path, rid, status="drafted", draft="Old")
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry",
                        lambda client, **kw: _msg("So sorry. Your next dinner is on the house."))
    out, st = client_api._do_regenerate_draft(rv, rid)
    assert st == 200 and out["ok"] and out["needs_review"] is True
    assert "on the house" in out["review_reason"]


# ── M-2 bulk publish posts only what its label counts ──────────────────────

def test_m2_approve_all_skips_flagged_urgent_and_old_drafts_newest_first(db_path):
    rid = _rid(db_path)
    old_urgent = _review(db_path, rid, urgency="high", days_ago=90)
    urgent = _review(db_path, rid, urgency="high", days_ago=2)
    flagged = _review(db_path, rid, flagged=1, reason="x", days_ago=1)
    old = _review(db_path, rid, days_ago=60)
    older_ok = _review(db_path, rid, days_ago=5)
    newest_ok = _review(db_path, rid, days_ago=0)
    q = models.reply_queue_counts(rid)
    assert q == {"publishable": 2, "held": 2, "older": 2}
    out, st = client_api._do_approve_all(rid, limit=1)
    assert out["approved"] == 1 and out["remaining"] == 1 and out["held"] == 2
    c = _conn(db_path)
    st_by = {r["id"]: r["response_status"] for r in c.execute("SELECT id, response_status FROM reviews")}
    c.close()
    assert st_by[newest_ok] == "approved"
    for untouched in (old_urgent, urgent, flagged, old, older_ok):
        assert st_by[untouched] == "drafted"
    client_api._do_approve_all(rid, limit=25)
    c = _conn(db_path)
    st_by = {r["id"]: r["response_status"] for r in c.execute("SELECT id, response_status FROM reviews")}
    c.close()
    assert st_by[older_ok] == "approved"
    assert all(st_by[i] == "drafted" for i in (old_urgent, urgent, flagged, old))


def test_m2_a_draft_flagged_after_the_batch_select_is_not_claimed(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid)
    c = _conn(db_path)
    c.execute("UPDATE reviews SET draft_needs_review=1 WHERE id=?", (rv,))
    c.commit(); c.close()
    assert models.claim_approval(rv, rid, publishable_only=True) is False
    assert models.claim_approval(rv, rid) is True     # a person, one at a time, still may


def test_m2_web_and_ios_home_count_the_same_publishable_replies(db_path):
    import home_brief
    import mobile_api
    rid = _rid(db_path)
    _review(db_path, rid, days_ago=1)
    _review(db_path, rid, days_ago=2)
    _review(db_path, rid, days_ago=200)                 # history
    _review(db_path, rid, flagged=1, reason="x")        # held
    user = {"id": 1, "restaurant_id": rid, "role": "client", "username": "o"}
    brief, st = home_brief.build_home_brief(user)
    assert st == 200
    att = [a for a in brief["attention"] if a["key"] == "awaiting_approval"]
    assert att and att[0]["action"]["count"] == 2 and "Publish 2" in att[0]["action"]["label"]
    assert "1 urgent or flagged held" in att[0]["evidence"]
    src = _read("mobile_api.py")
    assert '"reviews_awaiting_approval": _publishable' in src
    assert "reply_queue_counts" in src
    payload, st = mobile_api._do_mobile_home(user)
    assert st == 200 and payload["reviews_awaiting_approval"] == 2


# ── M-3 trust counts regenerations and ignores bulk publishes ──────────────

def test_m3_regenerated_drafts_count_as_rejections(db_path):
    rid = _rid(db_path)
    for _ in range(10):
        _review(db_path, rid, status="posted", rating=3, regenerate_count=3, response_action="regenerated",
                approved_at=(datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
    t = models.auto_approve_trust(rid)[3]
    assert t["approved"] == 10 and t["edited"] == 10 and t["trusted"] is False


def test_m3_bulk_approve_is_marked_and_earns_no_trust_or_style(db_path):
    rid = _rid(db_path)
    ids = [_review(db_path, rid, rating=3, draft=f"Thank you {i}") for i in range(12)]
    from flask import Flask
    with Flask(__name__).test_request_context():
        out, st = client_api._do_approve_all(rid, limit=25)
    assert out["approved"] == 12
    c = _conn(db_path)
    acts = {r["response_action"] for r in c.execute("SELECT response_action FROM reviews")}
    c.close()
    assert acts == {"bulk_approved"}
    t = models.auto_approve_trust(rid)[3]
    assert t["approved"] == 0 and t["trusted"] is False
    assert models.get_approved_examples(rid) == []
    perf = models.get_response_performance(rid)
    assert perf["total"] == 0


def test_m3_an_edit_before_a_bulk_publish_is_still_the_owners_edit(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid, rating=4, draft_edited=1)
    from flask import Flask
    with Flask(__name__).test_request_context():
        client_api._do_approve_all(rid)
    c = _conn(db_path)
    assert c.execute("SELECT response_action FROM reviews WHERE id=?", (rv,)).fetchone()[0] == "edited"
    c.close()


# ── M-25 regenerate never replaces a sent reply ────────────────────────────

@pytest.mark.parametrize("status", ["posted", "approved"])
def test_m25_regenerate_refuses_a_sent_reply(db_path, monkeypatch, status):
    import drafter
    rid = _rid(db_path)
    rv = _review(db_path, rid, status=status, draft="Live reply")
    called = []
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry", lambda client, **kw: called.append(1) or _msg("New"))
    out, st = client_api._do_regenerate_draft(rv, rid)
    assert st == 409 and not out["ok"] and not called
    c = _conn(db_path)
    row = c.execute("SELECT response_status, draft_response FROM reviews WHERE id=?", (rv,)).fetchone()
    c.close()
    assert row["response_status"] == status and row["draft_response"] == "Live reply"


def test_m25_update_draft_never_overwrites_a_posted_reply(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid, status="posted", draft="Live reply")
    assert models.update_draft(rv, "Other text") is False
    c = _conn(db_path)
    row = c.execute("SELECT response_status, draft_response FROM reviews WHERE id=?", (rv,)).fetchone()
    c.close()
    assert row["response_status"] == "posted" and row["draft_response"] == "Live reply"


def test_m25_a_publish_during_regeneration_keeps_the_live_text(db_path, monkeypatch):
    import drafter
    rid = _rid(db_path)
    rv = _review(db_path, rid, status="drafted", draft="Draft one")

    def racing(client, **kw):
        c = _conn(db_path)
        c.execute("UPDATE reviews SET response_status='posted' WHERE id=?", (rv,))
        c.commit(); c.close()
        return _msg("Draft two")
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry", racing)
    out, st = client_api._do_regenerate_draft(rv, rid)
    assert st == 409 and not out["ok"]
    c = _conn(db_path)
    row = c.execute("SELECT response_status, draft_response, regenerate_count FROM reviews WHERE id=?",
                    (rv,)).fetchone()
    c.close()
    assert row["response_status"] == "posted" and row["draft_response"] == "Draft one"


# ── M-30 draft_pending honours the reply language ─────────────────────────

def test_m30_draft_pending_passes_the_restaurants_language(db_path, monkeypatch):
    import drafter
    rid = _rid(db_path)
    models.update_restaurant(rid, {"response_language": "es"})
    _review(db_path, rid, status="pending", draft=None, sentiment="positive")
    seen = {}
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"]) and _msg("Gracias"))
    drafter.draft_pending(rid)
    assert "Always write the response in Spanish" in seen["p"]


# ── M-4 invoice trust is the owner's, and the rule leaves flagged lines open ─

def _ingredient(db_path, rid, name, cost=2.0):
    c = _conn(db_path)
    i = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) VALUES (?,?,?,?,1)",
                  (rid, name, "lb", cost)).lastrowid
    c.commit(); c.close()
    return i


def _import(db_path, rid, supplier, lines, applied=None, auto=0):
    c = _conn(db_path)
    i = c.execute("INSERT INTO invoice_imports (restaurant_id, supplier, lines_json, applied_json, applied_at, "
                  "auto_applied) VALUES (?,?,?,?,?,?)",
                  (rid, supplier, json.dumps({"lines": lines}),
                   json.dumps(applied) if applied is not None else None,
                   datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") if applied is not None else None,
                   auto)).lastrowid
    c.commit(); c.close()
    return i


def test_m4_auto_applied_imports_are_not_owner_evidence(db_path):
    import ordering
    rid = _rid(db_path)
    line = [{"index": 0, "selected": True, "proposed_cost": 2.5}]
    for _ in range(10):
        _import(db_path, rid, "Sysco", line, applied=[{"index": 0}], auto=1)
    t = ordering.invoice_trust(rid, "Sysco")
    assert t["applied"] == 0 and t["trusted"] is False
    for _ in range(3):
        _import(db_path, rid, "Sysco", line, applied=[{"index": 0}], auto=0)
    assert ordering.invoice_trust(rid, "Sysco")["trusted"] is True


def test_m4_lines_the_rule_skipped_can_still_be_applied_by_the_owner(db_path):
    import invoices
    rid = _rid(db_path)
    beef = _ingredient(db_path, rid, "Beef")
    herbs = _ingredient(db_path, rid, "Herbs")
    iid = _import(db_path, rid, "Sysco", [{"index": 0, "selected": True, "proposed_cost": 2.2},
                                          {"index": 1, "selected": False, "proposed_cost": 9.0, "note": "big"}])
    out = invoices.apply(rid, iid, [{"index": 0, "ingredient_id": beef, "unit_cost": 2.2}], auto=True,
                         db_path=db_path)
    assert out["ok"], out
    got = invoices.get_import(rid, iid, db_path=db_path)
    assert got["awaiting_owner"] is True and got["auto_applied_count"] == 1
    assert [l["applied"] for l in got["lines"]] == [True, False]
    # The owner applies the flagged line the rule left.
    out = invoices.apply(rid, iid, [{"index": 0, "ingredient_id": beef, "unit_cost": 3.0},
                                    {"index": 1, "ingredient_id": herbs, "unit_cost": 4.0}], db_path=db_path)
    assert out["ok"] and [u["index"] for u in out["updated"]] == [1]
    c = _conn(db_path)
    costs = {r["id"]: r["unit_cost"] for r in c.execute("SELECT id, unit_cost FROM ingredients")}
    c.close()
    assert costs[beef] == 2.2 and costs[herbs] == 4.0      # line 0 is not written twice
    assert invoices.get_import(rid, iid, db_path=db_path)["awaiting_owner"] is False
    # A second tap applies nothing new.
    again = invoices.apply(rid, iid, [{"index": 1, "ingredient_id": herbs, "unit_cost": 4.0}], db_path=db_path)
    assert not again["ok"] and "already" in again["error"]


def test_m4_web_and_ios_keep_the_apply_button_while_lines_wait():
    dash = _read("templates", "dashboard.html")
    assert "if(v.applied_at&&!v.awaiting_owner)" in dash
    sw = _read("ios", "CavnarAI", "CavnarAI", "Features", "FoodCost", "InvoiceScanSheet.swift")
    assert 'case awaitingOwner = "awaiting_owner"' in sw and "inv.isOpen" in sw


# ── M-5 an edited order counts against the supplier ────────────────────────

def test_m5_edited_orders_count_against_supplier_trust(db_path):
    import ordering
    rid = _rid(db_path)
    c = _conn(db_path)
    for i in range(12):
        c.execute("INSERT INTO purchase_orders (restaurant_id, po_number, supplier_email, items_json, total_cost, "
                  "source, edited) VALUES (?,?,?,?,?,?,?)",
                  (rid, f"PO{i}", "rep@sysco.test", "[]", 400.0, "owner", 1 if i < 9 else 0))
    c.commit(); c.close()
    t = ordering.supplier_trust(rid, "rep@sysco.test")
    assert t["orders"] == 12 and t["edited"] == 9 and t["trusted"] is False
    ok, why = ordering.order_can_go(rid, {"supplier_email": "rep@sysco.test", "total_cost": 400})
    assert not ok and "changed before sending" in why


# ── M-6 diagnoses come back most important first, retired ones left out ────

def _neg(db_path, rid, cat, severity, n):
    for _ in range(n):
        _review(db_path, rid, status="posted", rating=1, sentiment="negative",
                categories=json.dumps([cat]), severity=severity)


def _diag_row(db_path, rid, cat, when, mentions=3):
    c = _conn(db_path)
    c.execute("INSERT INTO review_diagnoses (restaurant_id, category, window_days, mention_count, cause, "
              "confidence, recommended_action, generated_at) VALUES (?,?,?,?,?,?,?,?)",
              (rid, cat, 90, mentions, f"{cat} cause", "medium", f"fix {cat}", when))
    c.commit(); c.close()


def test_m6_diagnoses_rank_by_severity_and_drop_retired_clusters(db_path):
    import review_intelligence as ri
    rid = _rid(db_path)
    _neg(db_path, rid, "food_safety", "safety", 3)
    _neg(db_path, rid, "service", "service", 6)
    _neg(db_path, rid, "ambiance", "minor", 4)
    now = datetime.utcnow()
    # diagnose() writes most severe first, so the least important is newest.
    _diag_row(db_path, rid, "food_safety", (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"))
    _diag_row(db_path, rid, "service", (now - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S"))
    _diag_row(db_path, rid, "ambiance", (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"))
    _diag_row(db_path, rid, "parking", (now - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S"))
    got = [d["category"] for d in ri.get_diagnoses(rid, include_stale=True)]
    assert got == ["food_safety", "service", "ambiance"]
    assert "parking" in [d["category"] for d in ri.get_diagnoses(rid, include_stale=True, include_retired=True)]


# ── M-17 an untraced figure in a stored diagnosis keeps its caveat ─────────

def test_m17_review_diagnosis_keeps_its_unsupported_figures(db_path):
    import review_intelligence as ri
    rid = _rid(db_path)
    _neg(db_path, rid, "service", "service", 4)
    cluster = ri.complaint_clusters(rid)[0]
    result = {"cause": "Fridays run 40% short", "alternative_cause": None, "what_would_confirm": None,
              "evidence_review_ids": [], "operational_evidence": [], "confidence": "low",
              "recommended_action": "add a server", "expected_outcome": None,
              "unsupported_figures": ["40%"]}
    ri._save_diagnosis(rid, cluster, result, {}, db_path)
    d = ri.get_diagnoses(rid)[0]
    assert d["unsupported_figures"] == ["40%"]
    dash = _read("templates", "dashboard.html")
    assert "applyFigureCaveat(box.querySelector('.rv-diag-bd')" in dash
    assert "applyFigureCaveat(host.querySelector('.fc2-cfo-bd')" in dash
    assert 'case unsupportedFigures = "unsupported_figures"' in _read(
        "ios", "CavnarAI", "CavnarAI", "Features", "Reviews", "ReviewsAnalyticsViewModel.swift")


def test_m17_food_diagnosis_keeps_its_unsupported_figures(db_path):
    import food_cost_intelligence as fci
    rid = _rid(db_path)
    drv = {"drivers": [{"label": "Salmon waste", "item": "Salmon", "dollars_monthly": 300.0}],
           "total_monthly": 300.0, "total_monthly_deduplicated": 300.0}
    result = {"headline": "h", "cause": "c 12%", "alternative_cause": None, "what_would_confirm": None,
              "operational_evidence": [], "confidence": "low", "recommended_action": "a",
              "expected_outcome": None, "unsupported_figures": ["12%"]}
    fci._save_diagnosis(rid, drv, result, 300.0, db_path)
    assert fci.get_diagnosis(rid, db_path=db_path)["unsupported_figures"] == ["12%"]


# ── M-7 the stored read survives the hours ─────────────────────────────────

def test_m7_review_insight_fingerprint_is_stable_across_hours(db_path, monkeypatch):
    import ai_utils
    import review_intelligence as ri
    rid = _rid(db_path)
    _neg(db_path, rid, "service", "service", 4)
    base = {"category": "service", "mention_count": 4, "window_days": 90, "cause": "short on Fridays",
            "alternative_cause": None, "what_would_confirm": None, "recommended_action": "add a server",
            "expected_outcome": None, "evidence_review_ids": [], "operational_evidence": [],
            "confidence": "medium", "as_of": "9/22/26", "stale": False, "unsupported_figures": []}
    ages = iter([2.0, 5.0, 5.0])
    monkeypatch.setattr(ri, "get_diagnoses", lambda *a, **k: [dict(base, age_hours=next(ages))])
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry",
                        lambda client, **kw: calls.append(kw["messages"][0]["content"])
                        or _msg("📊 This week: 4 reviews.\n✅ Do today: Add a server Friday."))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    out1, _ = client_api._do_review_insight(rid)
    client_api._insight_cache.clear()
    out2, _ = client_api._do_review_insight(rid)
    assert len(calls) == 1, "an hour later the same data reads the stored read"
    assert "h ago" not in calls[0] and "read of 9/22/26" in calls[0]
    # ...and the age is current UI metadata, not the one frozen at storage.
    assert out2["diagnosis"]["age_hours"] == 5.0


# ── M-8 / H-10 answers do what they say ────────────────────────────────────

def _rec_event(rid, body, monkeypatch):
    import strategy_routes
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context(json=body):
        return strategy_routes._do_rec_event({"id": 1, "restaurant_id": rid, "role": "client"})


def test_m8_done_silences_for_good_not_fourteen_days(db_path, monkeypatch):
    import rec_ledger
    rid = _rid(db_path)
    rec_ledger.present(rid, "insight_food:abc", "food", "food")          # what the owner was shown (K2)
    out, st = _rec_event(rid, {"key": "insight_food:abc", "event": "completed", "surface": "food",
                               "module": "food"}, monkeypatch)
    assert st == 200 and "won’t suggest it again" in out["message"]
    c = _conn(db_path)
    until = c.execute("SELECT silenced_until FROM rec_instances WHERE key='insight_food:abc'").fetchone()[0]
    c.close()
    assert until > (datetime.utcnow() + timedelta(days=3000)).strftime("%Y-%m-%d")
    assert rec_ledger.silenced(rid, "insight_food:abc")


def test_m8_track_starts_a_real_tracker_on_the_modules_metric(db_path, monkeypatch):
    import metrics
    rid = _rid(db_path)
    rec_ledger_title = "Answer every 3-star review within a day"
    import rec_ledger
    rec_ledger.present(rid, "insight_review:xyz", "reviews", "reviews", title=rec_ledger_title)
    monkeypatch.setattr(metrics, "measure", lambda r, key, s, e, db_path=None: (4.3, "30 reviews"))
    out, st = _rec_event(rid, {"key": "insight_review:xyz", "event": "accepted", "surface": "reviews",
                               "module": "reviews"}, monkeypatch)
    assert st == 200 and out["tracking"]["metric"] == "avg_rating"
    assert "Tracking" in out["message"] and "average rating" in out["message"]
    c = _conn(db_path)
    row = c.execute("SELECT source_key, metric, baseline_value, title FROM recommendation_outcomes "
                    "WHERE restaurant_id=?", (rid,)).fetchone()
    c.close()
    assert row["source_key"] == "insight_review:xyz" and row["metric"] == "avg_rating"
    assert row["baseline_value"] == 4.3 and row["title"].startswith("Answer every")


def test_m8_track_with_nothing_to_measure_says_it_is_only_hidden(db_path, monkeypatch):
    import metrics
    rid = _rid(db_path)
    import rec_ledger
    rec_ledger.present(rid, "insight_marketing:q", "marketing", "marketing")   # what the owner was shown (K2)
    monkeypatch.setattr(metrics, "measure", lambda *a, **k: (None, "no data"))
    out, st = _rec_event(rid, {"key": "insight_marketing:q", "event": "accepted", "surface": "marketing",
                               "module": "marketing"}, monkeypatch)
    assert "tracking" not in out and "hidden for 14 days" in out["message"]
    c = _conn(db_path)
    assert c.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0] == 0
    c.close()


def test_m8_track_is_not_offered_where_nothing_can_be_measured():
    html = client_api.rec_controls_html("insight_intel:a", "intel", "intel")
    assert 'data-rec-event="accepted"' not in html and ">Pass<" in html
    assert 'data-rec-event="accepted"' in client_api.rec_controls_html("insight_food:a", "food", "food")
    dash = _read("templates", "dashboard.html")
    assert "REC_TRACKABLE[m]?" in dash and "if(d&&d.message)return String(d.message);" in dash
    assert "Cavnar will watch what changes" not in dash
    sw = _read("ios", "CavnarAI", "CavnarAI", "DesignSystem", "RecAnswerRow.swift")
    assert "Cavnar will watch what changes" not in sw and "answered.message ?? answered.answer.confirmation" in sw


def test_m8_answered_lines_reach_the_prompt_as_do_not_repeat(db_path, monkeypatch):
    import ai_utils
    import rec_ledger
    rid = _rid(db_path, module_marketing=1)
    rec_ledger.present(rid, "insight_marketing:111", "marketing", "marketing", title="Post the carbonara tonight.")
    rec_ledger.record(rid, "insight_marketing:111", "dismissed", meta={"kind": "not_for_us"})
    seen = {}
    monkeypatch.setattr(ai_utils, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"])
                        and _msg("Hi, x.\n\n1. a\n2. b"))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    client_api._do_mkt_insight(rid)
    assert "ALREADY ANSWERED" in seen["p"] and "Post the carbonara tonight." in seen["p"]


# ── M-9 / H-13 one key per diagnosis, everywhere ───────────────────────────

def test_m9_home_and_the_brief_use_the_modules_diagnosis_keys(db_path, monkeypatch):
    import business_intelligence as bi
    dg = {"recommended_action": "Order less salmon", "cause": "c",
          "drivers": [{"item": "Salmon", "label": "Salmon waste"}]}
    assert client_api.diagnosis_rec_key("diag_food", dg) == "diag_food:salmon"
    src = _read("home_brief.py")
    assert 'add_rec("food_diagnosis"' not in src and '_drk_fd("diag_food", _dg)' in src
    assert '_drk_rv("diag_review", dg)' in src
    bsrc = _read("business_intelligence.py")
    assert 'add("food_diagnosis"' not in bsrc and 'diagnosis_rec_key("diag_food", dg)' in bsrc
    assert bi.link_key({"kind": "reviews_x_labor", "subject": bi._link_subject("service", "Friday")}) == \
        "link:reviews_x_labor:service:friday"
    assert bi.link_key({"kind": "reviews_x_labor", "subject": "service:Monday"}) != \
        bi.link_key({"kind": "reviews_x_labor", "subject": "service:Friday"})


# ── M-10 / M-11 one honest "at stake" figure ───────────────────────────────

def test_m10_m11_at_stake_is_the_deduplicated_driver_total(db_path, monkeypatch):
    import ai_utils
    import food_cost_intelligence as fci
    rid = _rid(db_path, module_inventory=1)
    drv = {"available": True, "drivers": [
        {"kind": "waste", "label": "Salmon waste above tolerance", "item": "Salmon", "dollars_monthly": 400.0,
         "confidence": "high", "difficulty": "low", "evidence": "e", "if_ignored": "i"},
        {"kind": "price", "label": "Salmon price up 9%", "item": "Salmon", "dollars_monthly": 300.0,
         "confidence": "high", "difficulty": "medium", "evidence": "e", "if_ignored": "i"}],
        "total_monthly": 700.0, "total_monthly_deduplicated": 400.0, "degraded_sources": []}
    ev = {"drivers": drv, "food_cost": {"ok": False, "missing": []}, "coverage": {}, "waste_sources": {},
          "weekday": {}, "seasonal": {}, "operational": {},
          # this month is $2,000 BETTER than the last
          "profitability": {"available": True, "dollars_vs_last_month": -2000, "direction": "better"}}
    monkeypatch.setattr(fci, "build_evidence", lambda r, db_path=None: ev)
    for name in ("_position_block", "_profit_block", "_trust_block", "_pattern_block", "_operational_block"):
        monkeypatch.setattr(fci, name, lambda *a, **k: "")
    seen = {}
    reply = json.dumps({"headline": "h", "cause": "Salmon Salmon waste above tolerance", "alternative_cause": "a",
                        "what_would_confirm": "w", "operational_evidence": [], "confidence": "medium",
                        "recommended_action": "Order less salmon", "expected_outcome": "o"})
    monkeypatch.setattr(fci, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"]) and _msg(reply),
                        raising=False)
    monkeypatch.setattr(ai_utils, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"]) and _msg(reply))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(fci, "get_client", lambda *a, **k: object(), raising=False)
    out = fci.diagnose(rid, db_path=db_path, force=True)
    assert out["dollars_at_stake"] == 400.0
    assert "Combined: $400/month" in seen["p"] and "$700" not in seen["p"]
    sw = _read("ios", "CavnarAI", "CavnarAI", "Features", "FoodCost", "FoodCostAnalyticsSection.swift")
    assert "cfo.drivers?.atStake" in sw


# ── M-12 the dollar ranking is by dollars before it is cut ─────────────────

def test_m12_a_best_seller_is_not_crowded_out_by_rarely_sold_dishes(db_path, monkeypatch):
    import food_cost_intelligence as fci
    import inventory
    import inventory_ledger as il
    rid = _rid(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([{"item": "Beef", "avg_daily_usage": 10.0}], True,
                                                              {"waste_items": []}))
    monkeypatch.setattr(il, "inferred_variance", lambda r: {"material": []})
    monkeypatch.setattr(fci, "supplier_comparison", lambda r, db_path=None: {"comparisons": []})
    monkeypatch.setattr(fci, "_food_cost_target", lambda r: 30.0)
    rare = [{"id": i, "name": f"Rare {i}", "sell_price": 10.0, "plate_cost": 6.0, "food_cost_pct": 60.0,
             "units_sold": 0 if i < 3 else 1} for i in range(4)]
    best = {"id": 99, "name": "Burger", "sell_price": 20.0, "plate_cost": 7.6, "food_cost_pct": 38.0,
            "units_sold": 700}
    monkeypatch.setattr(il, "menu_profitability", lambda r: {"priced": rare + [best]})
    # Price watch sorted by |%|: two swings with no spend ahead of a real beef rise.
    monkeypatch.setattr(inventory, "compute_item_trends", lambda r, items: {})
    monkeypatch.setattr(inventory, "build_price_watch", lambda t: [
        {"item": "Parsley", "kind": "trend", "change_pct": 80, "old_price": 1, "new_price": 1.8, "is_big_8": False},
        {"item": "Lemons", "kind": "drop", "change_pct": -50, "old_price": 2, "new_price": 1, "is_big_8": True},
        {"item": "Onion", "kind": "trend", "change_pct": 40, "old_price": 1, "new_price": 1.4, "is_big_8": False},
        {"item": "Garlic", "kind": "trend", "change_pct": 30, "old_price": 1, "new_price": 1.3, "is_big_8": False},
        {"item": "Beef", "kind": "trend", "change_pct": 12, "old_price": 5.0, "new_price": 8.0, "is_big_8": True,
         "weeks": 3}])
    out = fci.cost_drivers(rid, db_path=db_path)
    labels = [d["label"] for d in out["drivers"]]
    assert any(l.startswith("Burger") for l in labels)
    assert any(l.startswith("Beef price up") for l in labels)
    assert not any(l.startswith("Rare 0") for l in labels)


# ── M-13 a reprice is not asked for twice ──────────────────────────────────

def test_m13_no_second_reprice_for_a_rise_already_priced_in_and_no_spikes(db_path, monkeypatch):
    import inventory
    import inventory_ledger as il
    import menu_intelligence as mi
    rid = _rid(db_path, module_inventory=1)
    ing = _ingredient(db_path, rid, "Beef", cost=12.0)
    c = _conn(db_path)
    mid = c.execute("INSERT INTO menu_items (restaurant_id, name, sell_price, is_active) VALUES (?,?,?,1)",
                    (rid, "Burger", 25.0)).lastrowid
    c.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
              (mid, ing, 1.0))
    c.commit(); c.close()
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([{"item": "Beef"}], True))
    monkeypatch.setattr(inventory, "compute_item_trends", lambda r, items: {})
    monkeypatch.setattr(il, "menu_profitability", lambda r: {"priced": [
        {"id": mid, "name": "Burger", "sell_price": 25.0, "plate_cost": 10.0, "units_sold": 100}]})
    trend = {"item": "Beef", "kind": "trend", "weeks": 3, "change_pct": 25.0, "old_price": 10.0, "new_price": 12.0}
    spike = dict(trend, kind="spike", weeks=None)
    monkeypatch.setattr(inventory, "build_price_watch", lambda t: [spike])
    assert mi.reprice_suggestions(rid, db_path=db_path)["suggestions"] == []
    monkeypatch.setattr(inventory, "build_price_watch", lambda t: [trend])
    assert [s["dish"] for s in mi.reprice_suggestions(rid, db_path=db_path)["suggestions"]] == ["Burger"]
    c = _conn(db_path)
    c.execute("INSERT INTO reprice_decisions (restaurant_id, menu_item_id, dish, old_price, suggested_price, "
              "chosen_price, source) VALUES (?,?,?,?,?,?,?)", (rid, mid, "Burger", 25.0, 31.25, 31.25, "one_tap"))
    c.commit(); c.close()
    assert mi.reprice_suggestions(rid, db_path=db_path)["suggestions"] == []


# ── M-14 / M-32 the listing score leaves out what it cannot read ───────────

def test_m14_unconnected_listing_fields_are_unmeasured_not_zero():
    src = _read("client_api.py")
    assert '_it["measured"] = False' in src
    assert "presence_score = round(_presence_done / len(_presence_measured) * 100) if _presence_measured else None" in src
    assert 'review_total = int(r.gbp_review_count) if getattr(r, "gbp_review_count", None) else _imported_total' in src
    # M-32: no unsourced claims about how AI tools use the listing.
    assert "AI tools crawl your website" not in src and 'AI tools can answer \\"is it open now\\" directly' not in src
    dash = _read("templates", "dashboard.html")
    assert "var gbpHas = (gbpRaw !== null && gbpRaw !== undefined);" in dash


# ── M-15 / M-27 only real marketing pieces count ───────────────────────────

def _content(db_path, rid, ct, topic, post_id=None, origin="owner", when="now"):
    c = _conn(db_path)
    c.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, origin, created_at) "
              "VALUES (?,?,?,?,?,datetime(?))", (rid, ct, topic, post_id, origin, when))
    c.commit(); c.close()


def test_m15_a_job_draft_month_earns_no_agency_credit(db_path):
    import value_delivered
    rid = _rid(db_path, module_marketing=1)
    _content(db_path, rid, "instagram_post", "Tuesday night — a reason to come in this week", origin="job",
             when="now")
    _content(db_path, rid, "calendar_instagram_post", "Oktoberfest", origin="marker", when="now")
    out = value_delivered.avoided(rid, db_path=db_path)
    assert not [i for i in out["items"] if i["key"] == "content"]
    # a person's post, regenerated three times, is one piece
    for _ in range(3):
        _content(db_path, rid, "instagram_post", "Carbonara night")
    item = [i for i in value_delivered.avoided(rid, db_path=db_path)["items"] if i["key"] == "content"][0]
    assert item["label"].startswith("1 posts written across 1 month") or item["label"].startswith("1 post")
    # one piece is one piece's worth, not a month's fee (NS3 M1)
    assert item["dollars"] == value_delivered.AGENCY_PER_PIECE


def test_m27_home_week_and_ios_receipt_count_real_pieces(db_path):
    import marketing
    import mobile_api
    rid = _rid(db_path, module_marketing=1)
    _content(db_path, rid, "instagram_post", "Carbonara")
    _content(db_path, rid, "instagram_post", "Carbonara")                    # a regenerate
    _content(db_path, rid, "calendar_instagram_post", "Oktoberfest", origin="marker")
    _content(db_path, rid, "instagram_post", "quiet", origin="job")
    assert marketing.count_pieces(rid, "-7 days") == 1
    assert marketing.pieces_this_month(rid) == 1
    src = _read("mobile_api.py")
    assert '"text": "made this week"' in src and '"text": "drafted and ready to post"' not in src


# ── M-16 the marketing caveat gates and survives the caches ────────────────

def test_m16_unverified_marketing_read_offers_no_controls_and_keeps_its_flag(db_path, monkeypatch):
    import ai_utils
    rid = _rid(db_path, module_marketing=1)
    monkeypatch.setattr(ai_utils, "create_with_retry",
                        lambda *a, **k: _msg("Hi, reach is up 340%.\n\n1. Post the carbonara.\n2. Feature brunch."))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    first, _ = client_api._do_mkt_insight(rid, raw=True)
    assert first["figures_verified"] is False and first["recs"] == []
    again, _ = client_api._do_mkt_insight(rid, raw=True)                 # the five-minute cache
    assert again["figures_verified"] is False and "340%" in again["unsupported_figures"][0]
    web, _ = client_api._do_mkt_insight(rid)
    assert "data-rec-key" not in web["insight"]
    dash = _read("templates", "dashboard.html")
    # The cache is restaurant-scoped now (cavGet, Data Freshness #14).
    assert "cavGet('mkt_brief_check')" in dash
    sw = _read("ios", "CavnarAI", "CavnarAI", "Features", "Marketing", "MarketingAnalyticsSection.swift")
    assert "CavnarCaveat.unverifiedFigures(insight.unsupportedFigures" in sw


# ── M-18 like for like, or no gap ──────────────────────────────────────────

def test_m18_benchmark_compares_google_rating_to_google_ratings(db_path):
    import review_intelligence as ri
    rid = _rid(db_path)
    models.update_restaurant(rid, {"competitor_intel": json.dumps(
        {"competitors": [{"name": "A", "rating": 4.4}, {"name": "B", "rating": 4.6},
                         {"name": "C", "rating": 4.5}]})})
    for _ in range(5):
        _review(db_path, rid, rating=4)
    b = ri.competitor_benchmark(rid, db_path=db_path)
    assert b["our_rating_90d"] == 4.0 and b["gap_vs_median"] is None       # no Google rating: no gap
    models.update_restaurant(rid, {"gbp_rating": 4.6})
    b = ri.competitor_benchmark(rid, db_path=db_path)
    assert b["gap_vs_median"] == round(4.6 - 4.5, 2)
    assert "your Google rating is" in _read("client_api.py")


# ── M-19 the revenue range reads the displayed rating, and stands apart ────

def test_m19_revenue_range_uses_the_all_time_rating_move(db_path, monkeypatch):
    import review_intelligence as ri
    import labor
    rid = _rid(db_path)
    # 200 five-star reviews of history, then a rough month of 3s: the 30-day
    # average falls two stars, the displayed (all-time) rating barely moves.
    for _ in range(400):
        _review(db_path, rid, rating=5, days_ago=45)
    for _ in range(20):
        _review(db_path, rid, rating=3, days_ago=5)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda r: {"is_live": True, "total_sales": 90000.0, "period_days": 30})
    out = ri.revenue_at_risk(rid, db_path=db_path)
    # The old 30-vs-prior-60 comparison read a two-star fall here.
    assert out["available"] is False and "all-time rating moved" in out["reason"]
    rid2 = _rid(db_path, name="Small")
    for _ in range(60):
        _review(db_path, rid2, rating=5, days_ago=45)
    for _ in range(20):
        _review(db_path, rid2, rating=3, days_ago=5)
    out = ri.revenue_at_risk(rid2, db_path=db_path)
    assert out["available"] and out["rating_delta"] == -0.5 and out["scope"] == "restaurant"
    dash = _read("templates", "dashboard.html")
    body = dash[dash.index("function renderReviewDiagnosis(d){"):]
    body = body[:body.index("\n}\n")]
    assert "h+='</div></div>'+money;" in body
    sw = _read("ios", "CavnarAI", "CavnarAI", "Features", "Reviews", "ReviewsAnalyticsSection.swift")
    assert "diagnosisCard(diagnosis)\n" in sw


# ── M-20 Intel counts only open recommendations ────────────────────────────

INTEL = ("Hi, snapshot.\n\nWHAT COMPETITORS ARE DOING WELL:\n- A is fast.\n\n"
         "PRICE POSITIONING:\nMost sit at $$ like you.\n\n"
         "Recommendations:\n1. Push the brunch. [R1]\n2. Greet at the door. [R2]\n3. Answer every review. [R1]")


def test_m20_answered_intel_recs_stop_counting_everywhere(db_path):
    import insight_store
    import rec_ledger
    import home_brief
    # Intel is on for a full-tier restaurant with a Place ID.
    rid = _rid(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1,
               google_place_id="ChIJx")
    models.update_restaurant(rid, {"competitor_intel": json.dumps(
        {"insight": INTEL, "competitors": [{"name": "A", "rating": 4.4}]}),
        "competitor_updated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})
    assert len(client_api.intel_open_recs(rid)["recs"]) == 3
    for t in ("Push the brunch.", "Greet at the door.", "Answer every review."):
        k = insight_store.line_key("insight_intel", t)
        rec_ledger.present(rid, k, "intel", "intel", title=t)
        rec_ledger.record(rid, k, "dismissed", meta={"kind": "not_for_us"})
    assert client_api.intel_open_recs(rid)["recs"] == []
    import mobile_api
    assert mobile_api._intel_home_kpi(models.get_restaurant(rid))["value"] == "0"
    brief, _ = home_brief.build_home_brief({"id": 1, "restaurant_id": rid, "role": "client"})
    snap = [s for s in brief["snapshot"] if s["key"] == "intel"][0]
    assert snap["value"] == "0" and "Add competitors" not in snap["interpretation"]
    assert not [r for r in brief["recommendations"] if r["key"] == "intel_recs"]


# ── M-28 price positioning is parsed ───────────────────────────────────────

def test_m28_price_positioning_is_a_section():
    from competitor_intel_format import parse_competitor_intel, section_tone
    p = parse_competitor_intel(INTEL)
    assert ("Price positioning", ["Most sit at $$ like you."]) in p["sections"]
    assert len(p["recommendations"]) == 3
    assert section_tone("Price positioning") == "neutral"


# ── M-21 an approved quiet-night draft expires with its night ──────────────

def test_m21_approved_quiet_night_drafts_expire_too(db_path):
    import strategy_jobs
    rid = _rid(db_path, module_marketing=1)
    c = _conn(db_path)
    for status, approved in (("draft", None), ("approved", "2026-09-01 10:00:00")):
        c.execute("INSERT INTO marketing_drafts (restaurant_id, content_type, topic, body, status, approved_at, "
                  "created_at) VALUES (?,?,?,?,?,?,datetime('now','-5 days'))",
                  (rid, "instagram_post", "Tuesday" + strategy_jobs._QN_POST_SUFFIX, "Come in Tuesday", status,
                   approved))
    c.commit(); c.close()
    assert strategy_jobs.expire_quiet_night_drafts(rid, db_path=db_path) == 2
    c = _conn(db_path)
    assert {r[0] for r in c.execute("SELECT status FROM marketing_drafts")} == {"expired"}
    c.close()


# ── M-22 campaigns are compared on what they could measure ─────────────────

def test_m22_campaigns_without_a_link_are_not_named_weakest_and_segments_get_no_verdict(db_path, monkeypatch):
    import guest_marketing as gm
    monkeypatch.setattr(gm, "get_conn", lambda *a, **k: models.get_conn(db_path))
    rid = _rid(db_path, module_marketing=1)
    monkeypatch.setattr(gm, "campaign_history", lambda r, limit=20, db_path=None: [
        {"id": 1, "sent_count": 50, "clicks": 9, "link_token": "a", "segment": "regulars",
         "segment_label": "Regulars", "created_at": "2026-09-01 12:00:00", "visits_matched": None},
        {"id": 2, "sent_count": 50, "clicks": 0, "link_token": None, "segment": "all",
         "segment_label": "Everyone", "created_at": "2026-09-08 12:00:00", "visits_matched": None}])
    assert gm.diagnose(rid)["available"] is False
    monkeypatch.setattr(gm, "campaign_history", lambda r, limit=20, db_path=None: [
        {"id": 1, "sent_count": 50, "clicks": 0, "link_token": "a", "segment": "regulars",
         "segment_label": "Regulars", "created_at": "2026-09-01 12:00:00", "visits_matched": 20},
        {"id": 2, "sent_count": 50, "clicks": 0, "link_token": "b", "segment": "lapsed_60",
         "segment_label": "Lapsed", "created_at": "2026-09-08 12:00:00", "visits_matched": 3}])
    d = gm.diagnose(rid)
    assert "stronger segment only" not in (d["what_would_confirm"] or "")
    assert d["confidence"] == "low"
    src = _read("guest_marketing.py")
    assert "else sent_on + _td(days=1))" in src


# ── M-23 a post's window starts after it went out ──────────────────────────

def test_m23_the_window_skips_the_post_day_after_trade_began_and_home_colours_by_verdict():
    import marketing_signals as ms
    assert ms._window_dates(datetime(2026, 9, 18, 15, 0), 2) == ["2026-09-19", "2026-09-20"]
    assert ms._window_dates(datetime(2026, 9, 18, 8, 0), 2) == ["2026-09-18", "2026-09-19"]
    src = _read("home_brief.py")
    assert '{"lifted": "good", "dropped": "bad"}.get(_verdict, "neutral")' in src
    assert '"good" if pr["lift_pct"] >= 0 else "bad"' not in src


# ── M-24 a guest text never invents an offer ───────────────────────────────

@pytest.mark.parametrize("copy", ["Tonight enjoy a free dessert with any entree!", "20% off all week, see you soon",
                                  "Half-price apps till 6 tonight", "Buy one get one pizza Tuesday"])
def test_m24_invented_offers_are_rejected(db_path, monkeypatch, copy):
    import guest_marketing as gm
    rid = _rid(db_path, module_marketing=1)
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(gm, "create_with_retry", lambda client, **kw: _msg(copy))
    with pytest.raises(ValueError, match="offers"):
        gm.draft_campaign_message(models.get_restaurant(rid), campaign_type="slow_day", topic="Tuesday")


def test_m24_an_offer_the_owner_wrote_down_is_theirs(db_path, monkeypatch):
    import guest_marketing as gm
    rid = _rid(db_path, module_marketing=1)
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    seen = {}
    monkeypatch.setattr(gm, "create_with_retry",
                        lambda client, **kw: seen.setdefault("p", kw["messages"][0]["content"])
                        and _msg("Half-price apps till 6 tonight"))
    out = gm.draft_campaign_message(models.get_restaurant(rid), topic="half-price apps till 6 on Tuesdays")
    assert out.startswith("Half-price") and "Never invent an offer" in seen["p"]


# ── M-26 / H-20 owner-facing dates are M/D/YY ──────────────────────────────

def test_m26_freshness_and_next_post_dates_read_mdy():
    from ai_guard import freshness
    f = freshness("2026-09-21 10:00:00")
    assert f["as_of"] == "9/21/26" and f["as_of_iso"] == "2026-09-21"
    assert "Next post {_mdy(" in _read("home_brief.py")
    assert "_escHtml(mdy(c.created_at||''))" in _read("templates", "dashboard.html")


# ── M-29 Edit on an approved reply is an undo, not a skip ──────────────────

def test_m29_edit_on_an_approved_reply_does_not_record_a_skip(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid, status="approved")
    card = _read("templates", "_review_card.html")
    assert 'onclick="editApprovedR({{ r.id }})">Edit' in card and 'onclick="skipR({{ r.id }})">Edit' not in card
    dash = _read("templates", "dashboard.html")
    body = dash[dash.index("function editApprovedR(id){"):]
    body = body[:body.index("\n}\n")]
    assert "fetch('/undo/'+id" in body and "/skip/" not in body
    out, st = client_api._do_undo(rv, rid)
    assert st == 200
    c = _conn(db_path)
    row = c.execute("SELECT response_status, skipped_at FROM reviews WHERE id=?", (rv,)).fetchone()
    c.close()
    assert row["response_status"] == "drafted" and row["skipped_at"] is None


# ── M-31 / H-22 an answer to an expired episode is an answer ───────────────

def test_m31_an_answer_after_expiry_closes_with_the_answer(db_path):
    import rec_ledger
    rid = _rid(db_path)
    rec_ledger.present(rid, "trim_day:Monday", "labor", "home", title="Trim Monday")
    assert rec_ledger.expire_stale(days=-1) == 1
    assert rec_ledger.record(rid, "trim_day:Monday", "dismissed", meta={"kind": "not_for_us"})
    c = _conn(db_path)
    st = c.execute("SELECT status FROM rec_instances WHERE key='trim_day:Monday'").fetchone()[0]
    c.close()
    assert st == "dismissed"


# ── M-33 the stale caveat promises only what happens ───────────────────────

def test_m33_the_older_read_caveat_does_not_promise_a_queued_refresh():
    dash = _read("templates", "dashboard.html")
    sw = _read("ios", "CavnarAI", "CavnarAI", "DesignSystem", "CavnarCaveat.swift")
    assert "A fresh one is on the way" not in dash and "A fresh one is on the way" not in sw
    assert "tries again the next time this opens" in dash and "tries again the next time this opens" in sw


# ── H-8 the Home value headline is monthly, measured and permission-filtered ─

def test_h8_value_headline_is_monthly_by_module_and_filtered(db_path, monkeypatch):
    import outcomes
    import value_delivered
    rid = _rid(db_path)

    def fake_total(r, db_path=None, since=None, denied_modules=None, exclude_metrics=None, exclude_ids=None):
        by = {"labor": 420.0, "inventory": 300.0}
        for m in (denied_modules or ()):
            by.pop(m, None)
        return {"monthly": sum(by.values()), "annual": 0, "wins": len(by), "evaluated": 2, "in_flight": 0,
                "unmeasurable": 0, "no_clear_change": 0, "by_module": by, "caveat": "c"}
    monkeypatch.setattr(outcomes, "total_value", fake_total)
    monkeypatch.setattr(outcomes, "best_ever", lambda *a, **k: None)
    owner = value_delivered.headline(rid, user={"id": 1, "role": "client"})
    assert owner["monthly"] == 720 and owner["label"] == "measured, per month" and owner["restaurant_wide"]
    assert [m["label"] for m in owner["by_module"]] == ["labor", "food cost"]
    import permissions
    monkeypatch.setattr(permissions, "has_permission",
                        lambda u, perm: perm != permissions.FOOD_COST_VIEW)
    mgr = value_delivered.headline(rid, user={"id": 2, "role": "manager"})
    assert mgr["monthly"] == 420 and not mgr["restaurant_wide"]
    dash = _read("templates", "dashboard.html")
    assert "since you started</small>" not in dash and "a month, measured" in dash
    sw = _read("ios", "CavnarAI", "CavnarAI", "Features", "Home", "HomeValueBand.swift")
    assert "SINCE YOU JOINED" not in sw and "posts drafted" not in sw and "measuredOn" in sw
