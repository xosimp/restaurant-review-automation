"""good_news.py and milestones.py — the positive half of the product.

Everything in this platform detected trouble. These two modules detect the
opposite, and the risk they carry is the mirror image: a false alarm annoys,
but a false CONGRATULATION teaches an owner that the congratulations are
worthless. So most of what is pinned here is the refusals — the cases where
something looks like good news and must not be reported as any.
"""
from datetime import date, timedelta

import pytest

import good_news
import metrics
import milestones
import models
import outcomes
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))


def _restaurant(db_path, **kw):
    kw.setdefault("module_labor", 1)
    kw.setdefault("module_reviews", 1)
    kw.setdefault("module_inventory", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "Good News Co"),
                                        owner_email="g@x.com", **kw), db_path=db_path)


def _days(db_path, rid, start, n, sales=2000.0, labor_ratio=0.30):
    """n consecutive days of sales + labor ending (start + n - 1)."""
    conn = get_conn(db_path)
    for i in range(n):
        d = start + timedelta(days=i)
        conn.execute("INSERT OR REPLACE INTO labor_daily_history "
                     "(restaurant_id, date, day_of_week, sales, labor_cost) VALUES (?,?,?,?,?)",
                     (rid, d.isoformat(), d.strftime("%A"), sales, sales * labor_ratio))
    conn.commit()
    conn.close()


# ── records ──────────────────────────────────────────────────────────────────

def test_a_record_needs_prior_history(db_path):
    """Rule 1. The first good month of a new account is the first month, not
    a record. Two windows of data must produce nothing."""
    rid = _restaurant(db_path)
    today = date.today()
    # Two 28-day windows only: the most recent is much better.
    _days(db_path, rid, today - timedelta(days=55), 28, sales=1000.0)
    _days(db_path, rid, today - timedelta(days=27), 28, sales=3000.0)
    assert good_news.records(rid, today=today, db_path=db_path) == []


def test_a_record_fires_with_enough_history(db_path):
    rid = _restaurant(db_path)
    today = date.today()
    # Eight 28-day windows, oldest first, current one clearly the best.
    for i in range(8, 0, -1):
        start = today - timedelta(days=28 * i - 1)
        _days(db_path, rid, start, 28, sales=1500.0 if i > 1 else 2600.0)
    recs = [r for r in good_news.records(rid, today=today, db_path=db_path)
            if r["metric"] == "sales"]
    assert len(recs) == 1
    r = recs[0]
    assert r["kind"] == "record"
    assert r["value"] == pytest.approx(2600.0)
    assert r["previous_best"] == pytest.approx(1500.0)
    assert r["periods_compared"] >= good_news.MIN_PRIOR_PERIODS + 1
    assert good_news.CAVEAT in r["caveat"]


def test_a_record_must_beat_the_runner_up_by_more_than_noise(db_path):
    """Rule 2. Sales carries a 5% relative noise band; beating the previous
    best by 1% is the same number twice, not a record."""
    rid = _restaurant(db_path)
    today = date.today()
    for i in range(8, 0, -1):
        start = today - timedelta(days=28 * i - 1)
        _days(db_path, rid, start, 28, sales=2000.0 if i > 1 else 2020.0)
    assert [r for r in good_news.records(rid, today=today, db_path=db_path)
            if r["metric"] == "sales"] == []


def test_an_unmeasurable_window_is_skipped_not_zeroed(db_path):
    """Rule 3. A gap in the data must not read as a $0 window that the
    current window then trivially beats."""
    rid = _restaurant(db_path)
    today = date.today()
    # Only the current window has data. If gaps counted as zero this would
    # look like the best of twelve.
    _days(db_path, rid, today - timedelta(days=27), 28, sales=2500.0)
    assert good_news.records(rid, today=today, db_path=db_path) == []


def test_records_respect_module_flags(db_path):
    rid = _restaurant(db_path, module_labor=0)
    today = date.today()
    for i in range(8, 0, -1):
        _days(db_path, rid, today - timedelta(days=28 * i - 1), 28,
              sales=1500.0 if i > 1 else 2600.0)
    assert [r for r in good_news.records(rid, today=today, db_path=db_path)
            if r["metric"] == "sales"] == []


def test_records_respect_denied_modules(db_path):
    """A manager without FOOD_COST_VIEW must not be congratulated on a
    margin record — the same filtering /api/value does."""
    rid = _restaurant(db_path)
    today = date.today()
    for i in range(8, 0, -1):
        _days(db_path, rid, today - timedelta(days=28 * i - 1), 28,
              sales=1500.0 if i > 1 else 2600.0)
    assert [r for r in good_news.records(rid, today=today, db_path=db_path)
            if r["metric"] == "sales"]
    assert [r for r in good_news.records(rid, today=today, db_path=db_path,
                                         denied_modules={"labor"})
            if r["metric"] == "sales"] == []


def test_windows_do_not_overlap(db_path):
    """"Best ever" against a window sharing most of the same days is not a
    comparison. Consecutive windows must be disjoint and contiguous."""
    today = date.today()
    wins = good_news._windows("sales", today, 4)
    assert wins[0][1] == today
    for newer, older in zip(wins, wins[1:]):
        assert older[1] == newer[0] - timedelta(days=1)
        assert (newer[1] - newer[0]).days == (older[1] - older[0]).days


# ── streaks ──────────────────────────────────────────────────────────────────

def test_streak_counts_completed_weeks_under_the_owners_target(db_path):
    rid = _restaurant(db_path, labor_target_pct=30.0)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    # Six completed weeks, all at 25% labor.
    _days(db_path, rid, monday - timedelta(days=42), 42, labor_ratio=0.25)
    s = [x for x in good_news.streaks(rid, today=today, db_path=db_path)
         if x["metric"] == "labor_pct"]
    assert len(s) == 1
    assert s[0]["weeks"] == 6
    assert s[0]["target"] == 30.0


def test_a_short_streak_says_nothing(db_path):
    rid = _restaurant(db_path, labor_target_pct=30.0)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    _days(db_path, rid, monday - timedelta(days=14), 14, labor_ratio=0.25)
    assert [x for x in good_news.streaks(rid, today=today, db_path=db_path)
            if x["metric"] == "labor_pct"] == []


def test_a_streak_breaks_on_a_week_over_target(db_path):
    rid = _restaurant(db_path, labor_target_pct=30.0)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    # Four good weeks, then the most recent completed week blows the target.
    _days(db_path, rid, monday - timedelta(days=35), 28, labor_ratio=0.25)
    _days(db_path, rid, monday - timedelta(days=7), 7, labor_ratio=0.40)
    assert [x for x in good_news.streaks(rid, today=today, db_path=db_path)
            if x["metric"] == "labor_pct"] == []


def test_the_current_partial_week_never_counts(db_path):
    """A Tuesday is not a week. Counting it would break every streak each
    Monday and rebuild it each Sunday."""
    rid = _restaurant(db_path, labor_target_pct=30.0)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    _days(db_path, rid, monday - timedelta(days=28), 28, labor_ratio=0.25)
    before = [x for x in good_news.streaks(rid, today=today, db_path=db_path)
              if x["metric"] == "labor_pct"][0]["weeks"]
    # Add a terrible partial current week — the streak must not change.
    _days(db_path, rid, monday, today.weekday() + 1, labor_ratio=0.90)
    after = [x for x in good_news.streaks(rid, today=today, db_path=db_path)
             if x["metric"] == "labor_pct"][0]["weeks"]
    assert before == after == 4


def test_no_target_means_no_streak(db_path):
    """Rule 4: a streak is against a target the owner set. With none set
    there is nothing honest to count against."""
    rid = _restaurant(db_path, labor_target_pct=0)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    _days(db_path, rid, monday - timedelta(days=42), 42, labor_ratio=0.25)
    assert [x for x in good_news.streaks(rid, today=today, db_path=db_path)
            if x["metric"] == "labor_pct"] == []


# ── stopped ──────────────────────────────────────────────────────────────────

def _reviews(db_path, rid, day, n, category=None, negative=False):
    conn = get_conn(db_path)
    import json
    for i in range(n):
        conn.execute(
            "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, "
            "sentiment, categories, processed, review_date, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,1,?,?)",
            (rid, "google", f"gn-{day}-{category}-{negative}-{i}", 2 if negative else 5,
             "text", "negative" if negative else "positive",
             json.dumps([category] if category else []), day.isoformat(),
             day.isoformat()))
    conn.commit()
    conn.close()


def test_stopped_reports_a_complaint_category_that_went_away(db_path):
    rid = _restaurant(db_path)
    today = date.today()
    window = metrics.describe("complaints")["default_window_days"]
    was_day = today - timedelta(days=window + 5)
    now_day = today - timedelta(days=5)
    _reviews(db_path, rid, was_day, 4, category="service", negative=True)
    _reviews(db_path, rid, was_day, 16)
    _reviews(db_path, rid, now_day, 20)
    got = good_news.stopped(rid, today=today, db_path=db_path)
    assert [g["category"] for g in got] == ["service"]
    assert got[0]["was"] == pytest.approx(20.0)
    assert got[0]["now"] == pytest.approx(0.0)


def test_stopped_says_nothing_when_it_was_never_a_real_share(db_path):
    rid = _restaurant(db_path)
    today = date.today()
    window = metrics.describe("complaints")["default_window_days"]
    _reviews(db_path, rid, today - timedelta(days=window + 5), 1,
             category="service", negative=True)
    _reviews(db_path, rid, today - timedelta(days=window + 5), 40)
    _reviews(db_path, rid, today - timedelta(days=5), 40)
    assert good_news.stopped(rid, today=today, db_path=db_path) == []


def test_stopped_needs_both_windows_measurable(db_path):
    """Under five reviews in a window is unmeasurable. A quiet fortnight is
    not the same as a fixed problem."""
    rid = _restaurant(db_path)
    today = date.today()
    window = metrics.describe("complaints")["default_window_days"]
    _reviews(db_path, rid, today - timedelta(days=window + 5), 4,
             category="service", negative=True)
    _reviews(db_path, rid, today - timedelta(days=window + 5), 16)
    _reviews(db_path, rid, today - timedelta(days=5), 2)     # too few to read
    assert good_news.stopped(rid, today=today, db_path=db_path) == []


# ── milestones ───────────────────────────────────────────────────────────────

def test_a_milestone_fires_exactly_once(db_path):
    rid = _restaurant(db_path)
    first = milestones.fire(rid, "savings", "savings:1000", "One thousand",
                            db_path=db_path)
    second = milestones.fire(rid, "savings", "savings:1000", "One thousand",
                             db_path=db_path)
    assert first and first["title"] == "One thousand"
    assert second is None


def test_milestones_are_per_restaurant(db_path):
    a = _restaurant(db_path, name="A")
    b = _restaurant(db_path, name="B")
    assert milestones.fire(a, "savings", "savings:1000", "x", db_path=db_path)
    assert milestones.fire(b, "savings", "savings:1000", "x", db_path=db_path)


def _win(db_path, rid, dollars, key="fix"):
    """An evaluated, improved tracker worth `dollars` a month."""
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, "
        "metric, baseline_value, started_on, evaluate_on, after_value, verdict, delta, "
        "dollars_monthly, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, "recommendation", key, "Trim Monday lunch", "labor_pct", 30.0,
         (date.today() - timedelta(days=60)).isoformat(),
         (date.today() - timedelta(days=30)).isoformat(), 27.0, "improved", -3.0,
         dollars, "evaluated"))
    conn.commit()
    conn.close()


def test_savings_milestone_crosses_tiers_once_each(db_path):
    rid = _restaurant(db_path)
    _win(db_path, rid, 500.0)                      # $6,000/yr — crosses 1k and 5k
    fired = milestones.check_savings(rid, db_path=db_path)
    assert fired and fired["value"] == 5000
    assert milestones.get(rid, "savings:1000", db_path=db_path)
    assert milestones.check_savings(rid, db_path=db_path) is None


def test_savings_milestone_counts_only_measured_dollars(db_path):
    """An opportunity or an avoided-cost figure is not money anybody made,
    and must never cross a savings tier."""
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, "
        "metric, started_on, evaluate_on, status) VALUES (?,?,?,?,?,?,?,?)",
        (rid, "recommendation", "pending", "Not measured yet", "labor_pct",
         date.today().isoformat(), (date.today() + timedelta(days=30)).isoformat(),
         "tracking"))
    conn.commit()
    conn.close()
    assert milestones.check_savings(rid, db_path=db_path) is None


def test_response_rate_milestone_needs_reviews(db_path):
    """100% of nothing is not an achievement."""
    rid = _restaurant(db_path)
    assert milestones.check_response_rate(rid, db_path=db_path) is None


def test_anniversary_fires_only_on_listed_months(db_path):
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET created_at=? WHERE id=?",
                 ((date.today() - timedelta(days=95)).isoformat(), rid))
    conn.commit()
    conn.close()
    from models import get_restaurant
    r = get_restaurant(rid)
    assert milestones.check_anniversary(rid, restaurant=r, db_path=db_path)
    assert milestones.check_anniversary(rid, restaurant=r, db_path=db_path) is None


def test_mark_seen_is_idempotent(db_path):
    rid = _restaurant(db_path)
    milestones.fire(rid, "savings", "savings:1000", "x", db_path=db_path)
    assert milestones.mark_seen(rid, "savings:1000", db_path=db_path) is True
    assert milestones.mark_seen(rid, "savings:1000", db_path=db_path) is False


def test_the_cache_never_hands_one_viewer_another_viewers_answer(db_path):
    """The memo exists because morning_brief.deliver() rebuilds the brief
    per recipient. If the denied set were not part of the key, the first
    recipient to load would decide what everyone else sees — a permission
    boundary defeated by a dictionary."""
    rid = _restaurant(db_path)
    today = date.today()
    for i in range(8, 0, -1):
        _days(db_path, rid, today - timedelta(days=28 * i - 1), 28,
              sales=1500.0 if i > 1 else 2600.0)
    good_news._NEWS_CACHE.clear()
    full = good_news.all_good_news(rid, today=today, db_path=db_path)
    limited = good_news.all_good_news(rid, today=today, db_path=db_path,
                                      denied_modules={"labor"})
    assert [r["metric"] for r in full if r["metric"] == "sales"]
    assert [r["metric"] for r in limited if r["metric"] == "sales"] == []


def test_the_cache_hands_back_a_copy(db_path):
    """A caller that sorts or trims the result in place must not corrupt
    what the next reader sees."""
    rid = _restaurant(db_path)
    today = date.today()
    for i in range(8, 0, -1):
        _days(db_path, rid, today - timedelta(days=28 * i - 1), 28,
              sales=1500.0 if i > 1 else 2600.0)
    good_news._NEWS_CACHE.clear()
    first = good_news.all_good_news(rid, today=today, db_path=db_path)
    n = len(first)
    first.clear()
    assert len(good_news.all_good_news(rid, today=today, db_path=db_path)) == n


def test_recent_returns_unseen_only_when_asked(db_path):
    rid = _restaurant(db_path)
    milestones.fire(rid, "savings", "savings:1000", "one", db_path=db_path)
    milestones.fire(rid, "savings", "savings:5000", "five", db_path=db_path)
    milestones.mark_seen(rid, "savings:1000", db_path=db_path)
    assert len(milestones.recent(rid, db_path=db_path)) == 2
    unseen = milestones.recent(rid, db_path=db_path, unseen_only=True)
    assert [m["key"] for m in unseen] == ["savings:5000"]
