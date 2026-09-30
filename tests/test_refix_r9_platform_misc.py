"""Memory re-audit fix round (9/29/26), R9 — "platform_misc" (PLATFORM-15, -16).

  * The confidence log's weekly row is the ISO week's own figures; the
    trailing year sits in its own columns.
  * Every pattern's rec_kinds names a real ledger kind or topic (or an
    observed action), and support_for matches a kind through its topic.
"""
import sys
import uuid
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant


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
    from intelligence import jobs
    jobs.invalidate_excluded()
    models._internal_homes_cache.clear()
    yield
    jobs.invalidate_excluded()


def test_every_pattern_names_a_real_kind_or_topic():
    import outcomes
    import rec_ledger
    from intelligence import patterns
    real = set(rec_ledger.KIND_TOPIC) | set(rec_ledger.KIND_TOPIC.values()) \
        | {f"observed:{a}" for a in outcomes.OBSERVED_ACTIONS}
    for h in patterns.HYPOTHESES + patterns.PROSPECTIVE_HYPOTHESES:
        for k in h["rec_kinds"]:
            assert k in real, f"{h['key']} names {k!r}, which the ledger does not have"


def test_a_reply_pattern_supports_the_ledgers_reply_kinds():
    from intelligence import patterns
    kinds = patterns._HYPOTHESIS_KINDS["reply_fast_rating"]
    for k in ("urgent_reviews", "no_response:x", "publish_drafts", "review:123"):
        assert patterns.covers(kinds, k), k
    assert not patterns.covers(kinds, "trim_day:tue")
    assert patterns.covers(patterns._HYPOTHESIS_KINDS["adjust_schedule_labor"], "schedule_to_target")
    assert patterns.covers(patterns._HYPOTHESIS_KINDS["waste_tracking_food_cost"], "stock_low:beef")


NAMES = ("Harbor Grill", "Lakeside Diner", "Corner Bistro", "Maple Tavern", "Riverside Kitchen")


def _x(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def test_the_confidence_log_row_is_the_weeks_own():
    from intelligence import jobs
    today = date.today()
    this_week = (today - timedelta(days=today.weekday())).isoformat() + " 12:00:00"
    months_ago = (datetime.utcnow() - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
    for i, n in enumerate(NAMES):
        rid = create_restaurant(Restaurant(name=n, owner_email=f"w{i}@x{i}.test"))
        for when, action in ((months_ago, "accepted"), (months_ago, "accepted"), (months_ago, "accepted"),
                             (this_week, "not_for_us")):
            rec_id = uuid.uuid4().hex
            _x("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, event_at, rec_id) "
               "VALUES (?, 'trim_day', ?, ?, ?, ?)", (rid, f"trim_day:t#e{rec_id}", action, when, rec_id))
    jobs.invalidate_excluded()
    out = jobs.log_confidence(today=today)
    assert out["written"] >= 1
    c = models.get_conn()
    try:
        row = dict(c.execute("SELECT * FROM intel_confidence_log WHERE cohort='platform' AND rec_kind='trim_day'")
                   .fetchone())
    finally:
        c.close()
    assert row["n"] == 5 and row["acceptance_rate"] == 0.0, "this week: five declines"
    assert row["trailing_n"] == 20 and row["trailing_acceptance_rate"] == 0.75, "the trailing year"
