"""Memory audit 9/29/26, link_trackers (workstream M6): the results of what
Cavnar AI does itself — guest texts, repricing, Ask's tracking — reach the
restaurant's learner. A tracker keyed by its campaign, its month or its Ask
title carries the recommendation it measures (measures_key), is linked to that
episode when it starts, has its verdict carried to every linked episode by
the nightly sync, and is filed under that recommendation's kind by the
platform sync."""
import sqlite3
from datetime import date, timedelta

import pytest

import ask_cavnar_tools
import client_api
import guest_marketing
import metrics
import models
import outcomes
import rec_ledger
import rec_learning
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, outcomes, metrics, rec_ledger, rec_learning):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(rec_ledger, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})
    yield


def _rid(db_path):
    return create_restaurant(Restaurant(name="Link Co", owner_email="l@x.test", module_marketing=1,
                                        module_inventory=1, module_labor=1), db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _tracker_of(db_path, rid, key):
    c = _conn(db_path)
    row = c.execute("SELECT tracker_id FROM rec_instances WHERE restaurant_id=? AND key=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1", (rid, key)).fetchone()
    c.close()
    return row["tracker_id"] if row else None


def _sales(db_path, rid, days=70):
    c = _conn(db_path)
    start = date.today() - timedelta(days=days)
    for i in range(days):
        d = start + timedelta(days=i)
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, labor_pct) "
                  "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), 300.0, 1000.0, 30.0))
    c.commit()
    c.close()


# ── guest texts ──────────────────────────────────────────────────────────────

def test_a_fill_a_night_text_measures_the_slow_day_advice_it_answered(db_path):
    rid = _rid(db_path)
    _sales(db_path, rid)
    rec_ledger.present(rid, "slow_day:Tuesday", "marketing", "marketing", title="Fill Tuesday", db_path=db_path)
    got = guest_marketing.track_campaign_outcome(rid, "Tuesday", {"ok": True, "sent": 40}, user_id=1)
    assert got["ok"], got
    row = got["outcome"]
    assert row["source_key"].startswith("campaign:Tuesday:") and row["measures_key"] == "slow_day:Tuesday"
    assert _tracker_of(db_path, rid, "slow_day:Tuesday") == row["id"]
    assert row["module"] == "marketing"


def test_a_text_begun_on_a_feed_card_measures_the_card_and_the_slow_day_both(db_path):
    rid = _rid(db_path)
    _sales(db_path, rid)
    card = "holiday:halloween"
    rec_ledger.present(rid, card, "marketing", "marketing", title="Halloween", db_path=db_path)
    rec_ledger.present(rid, "slow_day:Friday", "marketing", "marketing", title="Fill Friday", db_path=db_path)
    got = guest_marketing.track_campaign_outcome(rid, "Friday", {"ok": True, "sent": 40}, user_id=1, rec_key=card)
    tid = got["outcome"]["id"]
    assert got["outcome"]["measures_key"] == card
    assert _tracker_of(db_path, rid, card) == tid and _tracker_of(db_path, rid, "slow_day:Friday") == tid


# ── repricing ────────────────────────────────────────────────────────────────

def _repriced(db_path, rid, dish):
    key = rec_ledger.rec_key("reprice", dish)
    rec_ledger.present(rid, key, "food", "food", title=f"Reprice {dish}", db_path=db_path)
    rec_ledger.implemented(rid, key, "food", user_id=1, meta={"module": "food"}, db_path=db_path)
    return key


def test_every_dish_repriced_this_month_is_linked_to_the_months_tracker(db_path):
    rid = _rid(db_path)
    salmon = _repriced(db_path, rid, "Salmon")
    got = client_api.track_reprice(rid, user_id=1)
    assert got and got["ok"], got
    tid = got["outcome"]["id"]
    assert _tracker_of(db_path, rid, salmon) == tid
    # A second dish the same month joins the same tracker.
    steak = _repriced(db_path, rid, "Steak")
    again = client_api.track_reprice(rid, user_id=1)
    assert again["outcome"]["id"] == tid and _tracker_of(db_path, rid, steak) == tid


# ── Ask's tracking ───────────────────────────────────────────────────────────

def test_asks_tracker_takes_the_recommendation_the_model_names(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "trim_day:Monday", "labor", "home", title="Trim Monday", db_path=db_path)
    assert ask_cavnar_tools.track_rec_key(rid, "labor_pct", "trim_day:Monday") == "trim_day:Monday"
    assert ask_cavnar_tools.track_rec_key(rid, "labor_pct", "no_such:key") is None


def test_asks_tracker_finds_the_one_recommendation_on_that_number_and_never_guesses(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "slow_day:Tuesday", "marketing", "marketing", title="Fill Tuesday", db_path=db_path)
    assert ask_cavnar_tools.track_rec_key(rid, "weekday_sales:Tuesday") == "slow_day:Tuesday"
    assert ask_cavnar_tools.track_rec_key(rid, "weekday_sales:Friday") is None
    rec_ledger.present(rid, "overtime:week", "labor", "home", title="Overtime", db_path=db_path)
    rec_ledger.present(rid, "overtime_move:ana", "labor", "schedule", title="Move Ana", db_path=db_path)
    assert ask_cavnar_tools.track_rec_key(rid, "overtime_hours") is None      # two candidates: none


def test_asks_track_tool_links_its_tracker(db_path):
    rid = _rid(db_path)
    _sales(db_path, rid)
    rec_ledger.present(rid, "slow_day:Tuesday", "marketing", "marketing", title="Fill Tuesday", db_path=db_path)
    out = ask_cavnar_tools._track_outcome(rid, title="Tuesday trivia night", metric="weekday_sales:Tuesday")
    assert out["ok"] and out["outcome"]["measures_key"] == "slow_day:Tuesday"
    assert _tracker_of(db_path, rid, "slow_day:Tuesday") == out["outcome"]["id"]


# ── the verdict reaches the learner and the platform ─────────────────────────

def _evaluate(db_path, tid, verdict="improved"):
    c = _conn(db_path)
    c.execute("UPDATE recommendation_outcomes SET status='evaluated', verdict=?, after_value=1200, "
              "after_start=started_on, after_end=evaluate_on, concurrent='[]' WHERE id=?", (verdict, tid))
    c.commit()
    c.close()


def test_the_nightly_sync_carries_the_verdict_to_every_linked_episode(db_path):
    rid = _rid(db_path)
    salmon, steak = _repriced(db_path, rid, "Salmon"), _repriced(db_path, rid, "Steak")
    tid = client_api.track_reprice(rid, user_id=1)["outcome"]["id"]
    _evaluate(db_path, tid)
    c = _conn(db_path)
    out = rec_ledger._sync_trackers(c)
    events = {r["key"]: r["n"] for r in c.execute(
        "SELECT key, COUNT(*) AS n FROM rec_events WHERE event='outcome' GROUP BY key")}
    c.close()
    assert out.get("outcomes") == 2 and events == {salmon: 1, steak: 1}
    c = _conn(db_path)
    assert rec_ledger._sync_trackers(c).get("outcomes") is None          # idempotent
    c.close()


def test_a_campaign_tracker_the_start_could_not_link_is_linked_by_its_rec_key(db_path):
    rid = _rid(db_path)
    _sales(db_path, rid)
    got = guest_marketing.track_campaign_outcome(rid, "Tuesday", {"ok": True, "sent": 40}, user_id=1)
    tid = got["outcome"]["id"]
    assert got["outcome"]["measures_key"] == "slow_day:Tuesday" and _tracker_of(db_path, rid, "slow_day:Tuesday") is None
    # The card is recorded a moment later (within the sync's minute of grace).
    rec_ledger.present(rid, "slow_day:Tuesday", "marketing", "marketing", title="Fill Tuesday", db_path=db_path)
    c = _conn(db_path)
    c.execute("UPDATE rec_instances SET tracker_id=NULL")
    c.execute("UPDATE recommendation_outcomes SET created_at=datetime('now', '+5 seconds') WHERE id=?", (tid,))
    c.commit()
    assert rec_ledger._sync_trackers(c).get("linked") == 1
    c.close()
    assert _tracker_of(db_path, rid, "slow_day:Tuesday") == tid


def test_the_slow_day_kind_builds_a_track_record_from_a_text(db_path):
    """Read like any tracker on that recommendation: its baseline mirrors the
    window that fired the card (CA2 #1), so the history reaches back past it
    — a result read against the slow nights that triggered the card is
    `unknown` to the learner, however it moved."""
    rid = _rid(db_path)
    _sales(db_path, rid, days=200)
    rec_ledger.present(rid, "slow_day:Tuesday", "marketing", "marketing", title="Fill Tuesday", db_path=db_path)
    got = guest_marketing.track_campaign_outcome(rid, "Tuesday", {"ok": True, "sent": 40}, user_id=1)
    assert not got["outcome"]["baseline_overlaps_trigger"] and got["outcome"]["trigger_start"]
    _evaluate(db_path, got["outcome"]["id"])
    learned = rec_learning.effectiveness(rid, db_path=db_path)
    s = learned.kinds.get("slow_day") or {}
    assert s.get("measured") == 1 and s.get("improved") == 1


def test_the_platform_files_the_result_under_the_recommendations_kind(db_path):
    import intelligence.feedback as feedback
    rid = _rid(db_path)
    _sales(db_path, rid, days=200)
    rec_ledger.present(rid, "slow_day:Tuesday", "marketing", "marketing", title="Fill Tuesday", db_path=db_path)
    got = guest_marketing.track_campaign_outcome(rid, "Tuesday", {"ok": True, "sent": 40}, user_id=1)
    _evaluate(db_path, got["outcome"]["id"])
    feedback.sync(db_path=db_path)
    c = _conn(db_path)
    kinds = {r["rec_kind"] for r in c.execute("SELECT rec_kind FROM intel_rec_events WHERE action='measured'")}
    c.close()
    assert kinds == {"slow_day"}
