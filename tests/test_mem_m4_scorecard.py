"""scorecard: only the schedule measured whether the AI improved for a
restaurant, and fatigue was only an admin number (memory audit 9/29/26, M4).
A monthly learning scorecard per restaurant is kept forever, flags a curve
that worsens or stays flat, and a fatigued restaurant is shown less."""
import json
from datetime import date, datetime, timedelta

import pytest

import learning_scorecard as lsc
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


# The restaurant's date (operator time when no zone is set), as the
# scorecard reads it - not the machine's (CI is UTC: tomorrow after 7pm).
from time_utils import restaurant_now as _rnow
TODAY = _rnow(None, naive=True).date()
MONTH = TODAY.replace(day=1)


def _rid(name="Curve Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _episodes(rid, n_taken, n_dismissed, n_ignored=0):
    at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    i = 0
    for status, n in (("completed", n_taken), ("dismissed", n_dismissed), ("expired", n_ignored)):
        for _ in range(n):
            i += 1
            _exec("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, status, created_at, "
                  "last_event_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (f"c{rid}-{i}", rid, f"trim_day:D{i}", "labor", "trim_day", "t", status, at, at))
            _exec("INSERT INTO rec_events (rec_id, restaurant_id, key, event, dedupe, at) VALUES (?,?,?,?,?,?)",
                  (f"c{rid}-{i}", rid, f"trim_day:D{i}", "shown", f"s{i}", at))


def test_the_month_reads_every_curve_from_its_own_record():
    rid = _rid()
    _episodes(rid, 6, 4)
    for i, cat in enumerate(("light", "unchanged", "unchanged", "heavy")):
        _exec("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at, draft_response, "
              "edit_category, approved_at, response_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
              (rid, "google", f"r{i}", 4, "ok", TODAY.isoformat(), "Thanks!", cat, f"{TODAY.isoformat()} 10:00:00",
               "approved"))
    for i, h in enumerate((1, 1, 0)):
        _exec("INSERT INTO ask_feedback (restaurant_id, message_id, helpful, created_at) VALUES (?,?,?,?)",
              (rid, i + 1, h, f"{TODAY.isoformat()} 09:00:00"))
    _exec("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct) "
          "VALUES (?,?,?,?,?,?)", (rid, "labor_week", MONTH.isoformat(), 33.0, 30.0, 10.0))
    m = lsc.compute_month(rid, MONTH)
    assert m["metrics"]["acceptance"] == {"value": 0.6, "k": 6, "n": 10}
    assert m["metrics"]["reply_edit_rate"]["value"] == 0.5
    assert m["metrics"]["ask_helpful_rate"]["value"] == pytest.approx(0.667, abs=0.001)
    assert m["forecast_error_by_kind"]["labor_week"] == {"value": 10.0, "n": 1}
    assert m["fatigue"]["n"] == 10 and m["fatigue"]["fatigued"] is False, "below the 20-settled floor"


def test_a_fatigued_restaurant_is_shown_less_until_it_recovers():
    rid = _rid()
    _episodes(rid, 2, 20, 4)                       # 24 of 26 dismissed or ignored
    assert lsc.volume_limit(rid, "home_recs", 3) == 3, "nothing stored yet"
    out = lsc.snapshot(rid)
    assert out["written"] >= 1
    assert lsc.fatigued(rid) is True
    assert lsc.volume_limit(rid, "home_recs", 3) == 2
    assert lsc.volume_limit(rid, "dsr_actions", 5) == 3
    assert lsc.volume_limit(rid, "feed_cards", 3) == 2
    assert lsc.volume_limit(rid, "unknown_surface", 7) == 7
    calm = _rid("Calm Co")
    _episodes(calm, 20, 4)
    lsc.snapshot(calm)
    assert lsc.volume_limit(calm, "home_recs", 3) == 3


def test_every_throttled_surface_reads_the_limit():
    import inspect
    import home_brief
    import marketing_opportunities
    from dsr import narrative
    assert 'volume_limit(rid, "home_recs"' in inspect.getsource(home_brief)
    assert 'volume_limit(restaurant_id, "feed_cards"' in inspect.getsource(marketing_opportunities)
    assert 'volume_limit(ctx.restaurant_id, "dsr_actions"' in inspect.getsource(narrative)


def _stored(rid, month, **metrics):
    body = {"metrics": {k: {"value": v, "n": 50} for k, v in metrics.items()}, "forecast_error_by_kind": {},
            "fatigue": None}
    _exec("INSERT INTO learning_scorecards (restaurant_id, month, metrics_json, fatigued, version) "
          "VALUES (?,?,?,?,?)", (rid, month, json.dumps(body), 0, lsc.SCORECARD_VERSION))


def test_a_worsening_or_flat_curve_is_flagged():
    rid = _rid()
    _stored(rid, "2026-06", acceptance=0.60, reply_edit_rate=0.40, measured_success=0.70)
    _stored(rid, "2026-07", acceptance=0.58, reply_edit_rate=0.41, measured_success=0.72)
    _stored(rid, "2026-08", acceptance=0.40, reply_edit_rate=0.40, measured_success=0.75)
    fl = {f["curve"]: f for f in lsc.flags(lsc.scorecards(rid))}
    assert fl["acceptance"]["state"] == "worsening"
    assert fl["reply_edit_rate"]["state"] == "flat", "the drafter is not learning the owner's voice"
    assert "measured_success" not in fl, "a curve at a good level is not flagged for staying there"


def test_the_admin_console_turns_flags_and_fatigue_into_issues(monkeypatch):
    import admin_ops
    rid = _rid()
    _exec("UPDATE restaurants SET billing_status='active' WHERE id=?", (rid,))
    _exec("INSERT INTO learning_scorecards (restaurant_id, month, metrics_json, flags_json, fatigued, version) "
          "VALUES (?,?,?,?,?,?)", (rid, TODAY.strftime("%Y-%m"), "{}",
                                   json.dumps([{"curve": "acceptance", "label": "recommendation acceptance",
                                                "state": "worsening", "latest": 0.4, "before": 0.59,
                                                "months": ["2026-06", "2026-07", "2026-08"]}]), 1,
                                   lsc.SCORECARD_VERSION))
    got = [i for i in admin_ops.issues()["issues"] if i["restaurant_id"] == rid]
    keys = {i["key"] for i in got}
    assert f"{rid}:learning:acceptance" in keys and f"{rid}:learning:fatigue" in keys
    title = next(i["title"] for i in got if i["key"] == f"{rid}:learning:acceptance")
    assert title == "Learning curve worsening: recommendation acceptance"
    assert admin_ops._learning_scorecards(rid)[0]["fatigued"] is True


def test_the_nightly_pass_writes_the_scorecard():
    import learning_memory
    rid = _rid()
    _episodes(rid, 3, 1)
    out = learning_memory.nightly(rid)
    assert out["steps"]["scorecard"]["written"] >= 1
    assert lsc.scorecards(rid)[-1]["metrics"]["acceptance"]["n"] == 4


def test_a_months_last_evening_counts_in_that_month():
    """9/30/26: Ask feedback stamped 10/1 03:00 UTC is the evening of 9/30 in
    Chicago - it belongs to September's scorecard, not October's. The month
    was read against UTC stamps as if they were local dates."""
    from datetime import date as _d
    rid = _rid("Boundary Co")
    _exec("UPDATE restaurants SET timezone='America/Chicago' WHERE id=?", (rid,))
    _exec("INSERT INTO ask_feedback (restaurant_id, message_id, helpful, created_at) VALUES (?,?,?,?)",
          (rid, 1, 1, "2026-10-01 03:00:00"))
    assert lsc.compute_month(rid, _d(2026, 9, 1))["metrics"]["ask_helpful_rate"]["n"] == 1
    assert lsc.compute_month(rid, _d(2026, 10, 1))["metrics"]["ask_helpful_rate"]["n"] == 0
