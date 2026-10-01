"""diagnosis_slice: root-cause diagnoses saw restaurant-wide averages, not the
shifts, edits, events and notes behind a complaint, and nothing of what was
already tried (memory audit 9/29/26, M4). The review diagnosis now reads its
cluster's own slice; the food diagnosis reads the drivers' items in the
receiving and invoice record; both read what was already tried."""
import csv
import io
import json
from datetime import date, timedelta

import pytest

import ai_guard
import models
import rec_ledger
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


TODAY = date.today()


def _rid(name="Slice Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _last(weekday, weeks_back):
    """The date of `weekday` `weeks_back` weeks before its most recent past occurrence."""
    names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    d = TODAY - timedelta(days=1)
    while names[d.weekday()] != weekday:
        d -= timedelta(days=1)
    return d - timedelta(days=7 * weeks_back)


def _csv(rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=("date", "day", "employee", "role", "shift_start", "shift_end",
                                        "scheduled_hours", "notes"))
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return out.getvalue()


def _cluster(**kw):
    c = {"category": "service", "mentions": 7, "review_ids": [1, 2], "avg_rating": 2.0, "dish": None,
         "role": None, "daypart": {"value": "dinner", "count": 5, "share": 0.7},
         "weekday": {"value": "Friday", "count": 5, "share": 0.7}, "weekday_pair": None, "complaints": [],
         "worst_severity": "service", "severity_counts": {}, "unclassified": 0, "first_seen": None,
         "last_seen": None, "window_days": 90}
    c.update(kw)
    return c


def _seed_slice(rid):
    hid = _exec("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                "VALUES (?,?,?,?,datetime('now'))", (rid, (TODAY - timedelta(days=20)).isoformat(),
                                                     (TODAY - timedelta(days=14)).isoformat(), ""))
    # Friday nights: 30 hours / 5 people lately, 44 / 7 in the four weeks before.
    for w in range(4):
        _exec("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people) "
              "VALUES (?,?,?,?,?,?)", (rid, hid, _last("Friday", w).isoformat(), "night", 30.0, 5))
        _exec("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people) "
              "VALUES (?,?,?,?,?,?)", (rid, hid, _last("Friday", w + 4).isoformat(), "night", 44.0, 7))
        # A Tuesday lunch that must not count.
        _exec("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people) "
              "VALUES (?,?,?,?,?,?)", (rid, hid, _last("Tuesday", w).isoformat(), "morning", 99.0, 9))
    fri = _last("Friday", 2)
    gen = _csv([{"date": fri.isoformat(), "day": "Friday", "employee": f"S{i}", "role": "Server",
                 "shift_start": "5:00pm", "shift_end": "11:00pm", "scheduled_hours": "6", "notes": ""}
                for i in range(6)])
    pub = _csv([{"date": fri.isoformat(), "day": "Friday", "employee": f"S{i}", "role": "Server",
                 "shift_start": "5:00pm", "shift_end": "11:00pm", "scheduled_hours": "6", "notes": ""}
                for i in range(4)])
    _exec("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) "
          "VALUES (?,?,?,?,?)", (rid, hid, 1, "generated", gen))
    _exec("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) "
          "VALUES (?,?,?,?,?)", (rid, hid, 2, "published", pub))
    _exec("INSERT INTO demand_signals (restaurant_id, date, kind, label) VALUES (?,?,?,?)",
          (rid, _last("Friday", 1).isoformat(), "event", "Cubs home game"))
    _exec("INSERT INTO close_outs (restaurant_id, business_date, went_wrong, callouts) VALUES (?,?,?,?)",
          (rid, _last("Friday", 1).isoformat(), "Tickets backed up at 7:30", "Two servers called out"))
    _exec("INSERT INTO close_outs (restaurant_id, business_date, went_wrong) VALUES (?,?,?)",
          (rid, _last("Tuesday", 1).isoformat(), "Nothing to do with Friday"))


def test_the_slice_reads_the_complaints_own_shifts_edits_events_and_notes():
    import review_intelligence as ri
    rid = _rid()
    _seed_slice(rid)
    sl = ri.slice_context(rid, _cluster())
    assert sl["label"] == "Friday dinner"
    block = sl["block"]
    assert "Friday dinner averaged 30 scheduled hours and 5 people" in block
    assert "against 44 hours and 7 people" in block
    assert "went from 36 drafted hours to 24 published" in block
    assert "Cubs home game" in block and "Two servers called out" in block
    assert "Nothing to do with Friday" not in block and "99" not in block
    assert ai_guard.UNTRUSTED_OPEN in block, "the closers' words and event labels are fenced"
    assert sl["line"].fields["hours_now"]["value"] == 30.0
    assert any("Two servers called out" in u for u in sl["untrusted"])


def test_a_cluster_with_no_concentration_has_no_slice():
    import review_intelligence as ri
    rid = _rid()
    sl = ri.slice_context(rid, _cluster(daypart=None, weekday=None))
    assert sl["label"] is None and sl["block"] == "" and sl["line"] is None


def test_what_was_tried_is_dated_fenced_and_a_decline_is_never_repeated():
    import review_intelligence as ri
    rid = _rid()
    rec_ledger.present(rid, "diag_review:service", "reviews", "reviews", title="Add a Friday dinner server.")
    rec_ledger.record(rid, "diag_review:service", "dismissed", surface="reviews",
                      meta={"kind": "not_for_us", "reason_code": "too_costly"})
    text = ri.tried_on_theme(rid, "service")
    assert "passed on" in text and "Add a Friday dinner server." in text and "too costly" in text
    assert ai_guard.UNTRUSTED_OPEN in text and "Do NOT recommend again" in text
    assert "Nothing answered" in ri.tried_on_theme(rid, "wait_time")


def test_the_review_diagnosis_prompt_carries_the_slice_and_may_cite_its_shifts(monkeypatch):
    import ai_utils
    import data_health
    import review_intelligence as ri
    rid = _rid()
    _seed_slice(rid)
    for i in (1, 2):
        _exec("INSERT INTO reviews (id, restaurant_id, platform, external_id, rating, text, review_date, fetched_at, "
              "sentiment, categories, processed) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
              (i, rid, "google", f"e{i}", 2, "Slow", TODAY.isoformat(), TODAY.isoformat(), "negative",
               json.dumps(["service"])))
    monkeypatch.setattr(ri, "complaint_clusters", lambda *a, **k: [_cluster()])
    monkeypatch.setattr(ri, "operational_context", lambda *a, **k: {"notes": []})
    seen = {}
    reply = {"cause": "Friday dinner runs short after the schedule edit.", "alternative_cause": "a",
             "what_would_confirm": "w", "evidence_review_ids": [1, 2],
             "operational_evidence": [{"module": "shifts", "metric": "scheduled hours", "value": "30"}],
             "confidence": "high", "recommended_action": "Add a server.", "expected_outcome": "If the cause is right, x."}

    def fake(client, **kw):
        import types
        seen["p"] = kw["messages"][0]["content"]
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=json.dumps(reply))],
                                     stop_reason="end_turn")
    monkeypatch.setattr(ai_utils, "create_with_retry", fake)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "is_held", lambda r: False)
    monkeypatch.setattr(data_health, "unattended_readiness", lambda *a, **k: {})
    out = ri.diagnose(rid, force=True)
    p = seen["p"]
    assert "WHAT CHANGED ON THOSE SHIFTS — Friday dinner" in p and "WHAT WAS ALREADY TRIED" in p
    # "guests", "worked" and "nightly" joined the modules a diagnosis may
    # cite (re-audit 9/29/26, CROSSMODULE-9/18).
    assert "labor|food_cost|waste|marketing|guests|games|shifts|worked|nightly" in p
    assert out and [e["module"] for e in out[0]["operational_evidence"]] == ["shifts"]


def test_the_food_diagnosis_reads_receiving_and_invoice_prices_on_its_drivers():
    import food_cost_intelligence as fci
    rid = _rid()
    iid = _exec("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost) VALUES (?,?,?,?)",
                (rid, "Salmon", "lb", 10.4))
    other = _exec("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost) VALUES (?,?,?,?)",
                  (rid, "Kale", "lb", 2.0))
    for i in range(3):
        _exec("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
              "VALUES (?,?,?,?,?,?)", (rid, iid, "receiving", 12, (TODAY - timedelta(days=3 + i * 5)).isoformat(), "po"))
    _exec("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
          "VALUES (?,?,?,?,?,?)", (rid, iid, "receiving", 20, (TODAY - timedelta(days=40)).isoformat(), "po"))
    _exec("INSERT INTO invoice_imports (restaurant_id, supplier, invoice_date, applied_json, applied_at) "
          "VALUES (?,?,?,?,datetime('now', '-5 days'))",
          (rid, "Sea Co", (TODAY - timedelta(days=6)).isoformat(),
           json.dumps([{"ingredient_id": iid, "name": "Salmon", "old_cost": 9.2, "new_cost": 10.4},
                       {"ingredient_id": other, "name": "Kale", "old_cost": 2.0, "new_cost": 2.5}])))
    out = fci.purchasing_context(rid, [{"item": "Salmon", "label": "Salmon waste above tolerance"}])
    b = out["block"]
    assert "Salmon: received 3 times (36 lb) in the last 28 days against 1 (20 lb) before" in b
    assert "$9.20 → $10.40" in b and "Kale" not in b
    assert out["line"].fields["cost_0_0"]["value"] == 10.4
    assert fci.purchasing_context(rid, [{"label": "no item"}])["line"] is None
