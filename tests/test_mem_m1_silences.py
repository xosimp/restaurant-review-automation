"""Memory audit 9/29/26, workstream M1 — what an answer holds (phase 0).

  silences      One Done or "Pass" held a subject silent on every surface for
                ten years, and no new evidence could reopen it. Answers now
                hold by kind and by the owner's reason (rec_ledger.
                answer_silence); a situational Done re-arms once its trigger
                clears and fires again; any answered key reopens when its
                figure doubles; a re-offered card names the earlier answer.
  critical_low  One answer on the running-out card silenced the alert for
                every item on it for ten years. A stock answer now holds only
                the current stock-out cycle: at most SAFETY_CYCLE_DAYS, and
                it ends the moment a count or delivery of the item arrives.
  reasons       "Bad timing" is a snooze in no denominator, "already doing
                it" is taken, "don't trust the data" holds per data source
                until re-verified.
"""
import json
from datetime import datetime, timedelta

import pytest

import models
import rec_ledger as rl
import insight_store
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path, name="Silence Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)


def _sql(db_path, sql, *args):
    c = models.get_conn(db_path)
    c.execute(sql, args)
    c.commit()
    c.close()


def _one(db_path, sql, *args):
    c = models.get_conn(db_path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


def _held_days(db_path, rid, key):
    row = _one(db_path, "SELECT julianday(silenced_until) - julianday(silenced_at) AS d, silence_rule FROM rec_instances "
                        "WHERE restaurant_id=? AND key=? ORDER BY created_at DESC, rowid DESC LIMIT 1", rid, key)
    return (round(row["d"]) if row["d"] is not None else None), row["silence_rule"]


# ── the policy ───────────────────────────────────────────────────────────────

def test_no_answer_holds_ten_years_any_more():
    assert max(rl.SILENCE_DAYS.values()) <= 365
    for key, event, kind in (("trim_day:Saturday", "completed", None), ("reprice:Soup", "dismissed", "not_for_us"),
                             ("stock_low:Salmon", "completed", None), ("reprice:Soup", "completed", None)):
        days, _rule = rl.answer_silence(key, event, kind=kind)
        assert days <= 365


def test_a_situational_done_is_held_sixty_days_and_a_one_off_a_year():
    assert rl.answer_silence("trim_day:Saturday", "completed") == (60, "situational_done")
    assert rl.answer_silence("cut_waste:Salmon", "completed") == (60, "situational_done")
    assert rl.answer_silence("diag_review:slow_service", "completed") == (60, "situational_done")
    assert rl.answer_silence("schedule_hours:Trim 4h Tuesday", "completed")[1] == "situational_done"
    assert rl.answer_silence("reprice:Soup", "completed") == (365, "done")
    # A "not for us" with no timing reason is a year, re-offered after.
    assert rl.answer_silence("trim_day:Saturday", "dismissed", kind="not_for_us") == (365, "decline")
    # The four recurring kinds keep their occurrence rule (OPP-9).
    assert rl.answer_silence("slow_day:Tuesday", "completed") == (6, "recurring_done")


def test_reasons_each_mean_something():
    assert rl.answer_silence("trim_day:Friday", "dismissed", "not_for_us", "bad_timing") == (rl.BAD_TIMING_DAYS,
                                                                                          "bad_timing")
    assert rl.answer_silence("trim_day:Friday", "dismissed", "not_for_us", "already_doing")[1] == "situational_done"
    assert rl.answer_silence("trim_day:Friday", "dismissed", "not_for_us", "dont_trust_data")[1] == "distrust"
    assert rl.answer_silence("trim_day:Friday", "dismissed", "not_for_us", "too_costly")[1] == "decline"
    # A free reason that names a time is a timing answer; one that says no is not.
    assert rl.timing_reason("not right now, after the holidays")
    assert rl.timing_reason("busy season")
    assert not rl.timing_reason("we never cut the Friday closer")
    assert not rl.timing_reason("not for us, never")
    assert rl.answer_silence("trim_day:Friday", "dismissed", "not_for_us", None, "maybe next month")[1] == "bad_timing"


def test_the_stock_answer_is_never_past_the_stock_out_cycle():
    for event, kind in (("completed", None), ("dismissed", "not_for_us"), ("dismissed", "hide")):
        days, rule = rl.answer_silence("stock_low:Salmon", event, kind=kind)
        assert days <= rl.SAFETY_CYCLE_DAYS and rule == "safety_cycle"


# ── recorded answers ────────────────────────────────────────────────────────

def test_done_on_trim_saturday_holds_sixty_days_not_ten_years(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Saturday", "labor", "home", db_path=db_path)
    # The route used to pass SILENCE_DAYS["done"] (3,650): the policy caps it.
    rl.record(rid, "trim_day:Saturday", "completed", surface="home", silence_days=3650, db_path=db_path)
    assert _held_days(db_path, rid, "trim_day:Saturday") == (60, "situational_done")


def test_bad_timing_is_a_short_deferral_not_a_decline_anywhere(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Friday", "labor", "home", title="Trim Friday staffing", db_path=db_path)
    rl.record(rid, "trim_day:Friday", "dismissed", meta={"kind": "not_for_us", "reason_code": "bad_timing"},
              db_path=db_path)
    assert _held_days(db_path, rid, "trim_day:Friday") == (rl.BAD_TIMING_DAYS, "bad_timing")
    assert "labor:day:friday" not in insight_store.declined_signatures(rid, db_path=db_path)
    # A plain "not for us" on the same advice is a decline across surfaces.
    rl.present(rid, "trim_day:Monday", "labor", "home", title="Trim Monday staffing", db_path=db_path)
    rl.record(rid, "trim_day:Monday", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    assert "labor:day:monday" in insight_store.declined_signatures(rid, db_path=db_path)


def test_already_doing_it_closes_the_recommendation_as_taken(db_path):
    rid = _rid(db_path)
    rl.present(rid, "cut_waste:Salmon", "food", "home", db_path=db_path)
    rl.record(rid, "cut_waste:Salmon", "dismissed", meta={"kind": "not_for_us", "reason_code": "already_doing"},
              db_path=db_path)
    row = _one(db_path, "SELECT status FROM rec_instances WHERE restaurant_id=? AND key='cut_waste:Salmon'", rid)
    assert row["status"] == "completed"
    ev = _one(db_path, "SELECT event, meta FROM rec_events WHERE restaurant_id=? AND key='cut_waste:Salmon' "
                       "AND event='dismissed'", rid)
    assert json.loads(ev["meta"])["reason_code"] == "already_doing"      # kept as the owner said it


# ── critical_low ─────────────────────────────────────────────────────────────

def _ingredient(db_path, rid, name):
    c = models.get_conn(db_path)
    cur = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, par_level, current_stock) VALUES (?,?,?,?,?)",
                    (rid, name, "lb", 10, 1))
    c.commit()
    iid = cur.lastrowid
    c.close()
    return iid


def test_a_count_or_delivery_after_the_answer_lifts_the_stock_silence(db_path):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, "Salmon")
    rl.present(rid, "stock_low:Salmon", "food", "home", db_path=db_path)
    rl.record(rid, "stock_low:Salmon", "completed", surface="home", silence_days=3650, db_path=db_path)
    assert _held_days(db_path, rid, "stock_low:Salmon") == (rl.SAFETY_CYCLE_DAYS, "safety_cycle")
    assert "stock_low:Salmon" in rl.silenced_keys(rid, db_path=db_path)
    # A count recorded BEFORE the answer does not lift it.
    _sql(db_path, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                  "source, created_at) VALUES (?,?,?,?,?,?, datetime('now','-1 day'))",
         rid, iid, "recount", 2, "2026-09-28", "manual")
    assert "stock_low:Salmon" in rl.silenced_keys(rid, db_path=db_path)
    # A delivery after it does: the next stock-out is a new cycle.
    _sql(db_path, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                  "source, created_at) VALUES (?,?,?,?,?,?, datetime('now','+1 second'))",
         rid, iid, "receiving", 20, "2026-09-29", "purchase_order")
    assert "stock_low:Salmon" not in rl.silenced_keys(rid, db_path=db_path)


def test_the_critical_low_alert_is_no_longer_silenced_after_a_new_count(db_path):
    """The alert path itself (notify.silenced_keys → rec_ledger)."""
    import notify
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, "Chicken")
    rl.present(rid, "stock_low:Chicken", "food", "alert_sms", db_path=db_path)
    rl.record(rid, "stock_low:Chicken", "dismissed", meta={"kind": "not_for_us", "reason": "ordered already"},
              db_path=db_path)
    assert "stock_low:Chicken" in notify.silenced_keys(rid, db_path)
    _sql(db_path, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                  "source, created_at) VALUES (?,?,?,?,?,?, datetime('now','+1 second'))",
         rid, iid, "recount", 0, "2026-09-29", "manual")
    assert "stock_low:Chicken" not in notify.silenced_keys(rid, db_path)


def test_homes_own_row_for_a_stock_answer_obeys_the_cycle_too(db_path):
    import home_brief
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, "Rice")
    rl.present(rid, "stock_low:Rice", "food", "home", db_path=db_path)
    out = home_brief.dismiss(rid, "stock_low:Rice", kind="done", _card=False)
    assert out["days"] <= rl.SAFETY_CYCLE_DAYS
    row = _one(db_path, "SELECT julianday(expires_at) - julianday(dismissed_at) AS d FROM home_dismissals "
                        "WHERE restaurant_id=? AND key='stock_low:Rice'", rid)
    assert round(row["d"]) <= rl.SAFETY_CYCLE_DAYS
    _sql(db_path, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                  "source, created_at) VALUES (?,?,?,?,?,?, datetime('now','+1 second'))",
         rid, iid, "recount", 0, "2026-09-29", "manual")
    assert "stock_low:Rice" not in rl.silenced_keys(rid, db_path=db_path)


# ── reopening ────────────────────────────────────────────────────────────────

def test_an_answered_key_reopens_when_its_figure_doubles(db_path):
    rid = _rid(db_path)
    first = rl.present(rid, "trim_day:Saturday", "labor", "home", dollar_value=120, db_path=db_path)
    rl.record(rid, "trim_day:Saturday", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    # Small moves do not reopen a decline.
    assert rl.present(rid, "trim_day:Saturday", "labor", "home", dollar_value=180, db_path=db_path) is None
    again = rl.present(rid, "trim_day:Saturday", "labor", "home", dollar_value=900, db_path=db_path)
    assert again and again != first
    row = _one(db_path, "SELECT reopened_from FROM rec_instances WHERE rec_id=?", again)
    assert row["reopened_from"] == first
    prev = rl.previous_answers(rid, ["trim_day:Saturday"], db_path=db_path)["trim_day:Saturday"]
    assert prev["answer"] == "dismissed" and "$120/mo then" in prev["text"] and "doubled" in prev["text"]
    # M/D/YY, never ISO.
    assert "-" not in prev["answered_on"] and prev["answered_on"].count("/") == 2
    # The answer is kept in the trail.
    assert _one(db_path, "SELECT COUNT(*) AS n FROM rec_events WHERE rec_id=? AND event='dismissed'", first)["n"] == 1


def test_a_snooze_or_a_timing_answer_is_never_reopened_by_a_figure(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Sunday", "labor", "home", dollar_value=100, db_path=db_path)
    rl.record(rid, "trim_day:Sunday", "dismissed", meta={"kind": "not_for_us", "reason_code": "bad_timing"},
              db_path=db_path)
    assert rl.present(rid, "trim_day:Sunday", "labor", "home", dollar_value=1000, db_path=db_path) is None


def test_a_situational_done_rearms_when_the_trigger_clears_and_fires_again(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Saturday", "labor", "home", dollar_value=200, db_path=db_path)
    rl.record(rid, "trim_day:Saturday", "completed", db_path=db_path)
    kinds = {"trim_day"}
    # Still firing right after the answer: held.
    assert rl.reconsider(rid, [{"key": "trim_day:Saturday", "dollar_value": 210}], kinds, db_path=db_path) == set()
    # The trigger clears (a build that evaluated trim_day did not fire it)...
    assert rl.reconsider(rid, [], kinds, db_path=db_path) == set()
    assert _one(db_path, "SELECT trigger_clear_at FROM rec_instances WHERE restaurant_id=?", rid)["trigger_clear_at"]
    # ...and fires again before REARM_MIN_DAYS: still held.
    assert rl.reconsider(rid, [{"key": "trim_day:Saturday"}], kinds, db_path=db_path) == set()
    # Once the answer is old enough, it re-arms.
    _sql(db_path, "UPDATE rec_instances SET silenced_at=datetime('now','-20 days'), "
                  "trigger_clear_at=datetime('now','-5 days') WHERE restaurant_id=?", rid)
    assert rl.reconsider(rid, [{"key": "trim_day:Saturday"}], kinds, db_path=db_path) == {"trim_day:Saturday"}
    assert "trim_day:Saturday" not in rl.silenced_keys(rid, db_path=db_path)
    new = rl.present(rid, "trim_day:Saturday", "labor", "home", dollar_value=250, db_path=db_path)
    assert new
    prev = rl.previous_answers(rid, ["trim_day:Saturday"], db_path=db_path)["trim_day:Saturday"]
    assert prev["answer"] == "completed" and "cleared" in prev["text"]


def test_a_build_that_records_nothing_judges_without_writing(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Tuesday", "labor", "home", db_path=db_path)
    rl.record(rid, "trim_day:Tuesday", "completed", db_path=db_path)
    rl.reconsider(rid, [], {"trim_day"}, db_path=db_path, write=False)
    assert _one(db_path, "SELECT trigger_clear_at FROM rec_instances WHERE restaurant_id=?", rid)[0] is None


# ── the boot cap ─────────────────────────────────────────────────────────────

def test_old_ten_year_answers_are_capped_at_boot(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db_path)
    rl.present(rid, "reprice:Soup", "food", "home", db_path=db_path)
    _sql(db_path, "UPDATE rec_instances SET status='completed', closed_at=datetime('now','-2 days'), "
                  "silenced_until=datetime('now','+3640 days'), silence_rule=NULL WHERE restaurant_id=?", rid)
    _sql(db_path, "INSERT INTO home_dismissals (restaurant_id, key, kind, dismissed_at, expires_at) "
                  "VALUES (?, 'cut_waste:Salmon', 'done', datetime('now','-2 days'), datetime('now','+3640 days'))", rid)
    rl.init_rec_ledger(db_path)
    assert _one(db_path, "SELECT round(julianday(silenced_until) - julianday(closed_at)) AS d FROM rec_instances "
                         "WHERE key='trim_day:Monday'")["d"] == 60
    assert _one(db_path, "SELECT round(julianday(silenced_until) - julianday(closed_at)) AS d FROM rec_instances "
                         "WHERE key='reprice:Soup'")["d"] == 365
    assert _one(db_path, "SELECT round(julianday(expires_at) - julianday(dismissed_at)) AS d FROM home_dismissals "
                         "WHERE key='cut_waste:Salmon'")["d"] == 60
    # Idempotent.
    rl.init_rec_ledger(db_path)
    assert _one(db_path, "SELECT round(julianday(silenced_until) - julianday(closed_at)) AS d FROM rec_instances "
                         "WHERE key='trim_day:Monday'")["d"] == 60


# ── what the owner is told ───────────────────────────────────────────────────

def test_the_answer_message_says_what_it_does():
    assert "year" in rl.silence_message("reprice:Soup", "dismissed", kind="not_for_us")
    assert "weeks" in rl.silence_message("trim_day:Friday", "dismissed", "not_for_us", "bad_timing")
    assert "count or delivery" in rl.silence_message("stock_low:Salmon", "completed")
    assert "comes back" in rl.silence_message("trim_day:Friday", "completed")
    assert "re-verified" in rl.silence_message("trim_day:Friday", "dismissed", "not_for_us", "dont_trust_data")
