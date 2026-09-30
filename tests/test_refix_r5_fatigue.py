"""Memory re-audit fix round 9/29/26 (R5, fatigue_month — LOOPS-5, QUALITY-8):
the fatigue throttle reads the episodes SETTLED in the last 30 days, so it no
longer lifts itself on the 1st of every month."""
import sys
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _ignored(rid, n, created_days_ago, prefix="cut_waste:Item", status="expired", surface="home"):
    conn = models.get_conn()
    for i in range(n):
        created = f"-{created_days_ago + (i % 3)} days"
        rid_ = f"f{rid}{prefix}{i}{created_days_ago}"
        conn.execute("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, status, tags, "
                     "created_at, last_event_at, closed_at) VALUES (?,?,?,?,?,?,?,?,datetime('now', ?),"
                     "datetime('now', ?),datetime('now', ?, '+14 days'))",
                     (rid_, rid, f"{prefix}{i}", "food", prefix.split(":")[0], "t", status, "[]", created, created,
                      created))
        conn.execute("INSERT INTO rec_events (rec_id, restaurant_id, key, event, surface, dedupe, at) "
                     "VALUES (?,?,?,?,?,?,datetime('now', ?))",
                     (rid_, rid, f"{prefix}{i}", "shown", surface, f"s{i}{created_days_ago}", created))
    conn.commit()
    conn.close()


def test_the_throttle_holds_across_the_turn_of_the_month():
    import learning_scorecard as lsc
    rid = create_restaurant(Restaurant(name="Tired Co", owner_email="t@x.test"))
    # 25 cards shown 30-32 days ago, every one ignored: they settled (expired)
    # 16-18 days ago — inside the rolling window, whichever month they began.
    _ignored(rid, 25, 30)
    last_month_day = date.today() - timedelta(days=42)
    lsc.snapshot(rid, today=last_month_day)
    lsc.snapshot(rid, today=date.today())     # tonight's pass writes this month's row
    assert lsc.fatigued(rid) is True           # the reviewer's proof asserted False here
    assert lsc.volume_limit(rid, "home_recs", 3) == lsc.THROTTLE["home_recs"]
    now = lsc.rolling_fatigue(rid)
    assert now["n"] == 25 and now["share"] == 1.0 and now["fatigued"]


def test_the_throttle_lifts_when_the_owner_starts_answering():
    import learning_scorecard as lsc
    import rec_ledger
    rid = create_restaurant(Restaurant(name="Better Co", owner_email="b@x.test"))
    _ignored(rid, 22, 30)
    assert lsc.rolling_fatigue(rid)["fatigued"]
    # 20 newer cards the owner took: the newest settled say recovered
    for i in range(20):
        key = f"trim_day:D{i}"
        rec_ledger.present(rid, key, "labor", "home", title=key)
        rec_ledger.record(rid, key, "accepted", surface="home", authority="principal")
    got = lsc.rolling_fatigue(rid)
    assert got["n"] == 42 and not got["fatigued"]


def test_unseen_and_distrusted_episodes_do_not_count_toward_fatigue():
    import learning_scorecard as lsc
    rid = create_restaurant(Restaurant(name="Inbox Co", owner_email="i@x.test"))
    _ignored(rid, 25, 30, surface="weekly_email")         # emailed, never opened
    now = lsc.rolling_fatigue(rid)
    assert now["n"] == 0 and not now["fatigued"]


def test_below_the_floor_the_newest_twenty_in_the_horizon_are_read():
    import learning_scorecard as lsc
    rid = create_restaurant(Restaurant(name="Slow Co", owner_email="s@x.test"))
    _ignored(rid, 12, 50)          # settled ~34-36 days ago: outside 30 days, inside the 60-day horizon
    _ignored(rid, 10, 20, prefix="reprice:Dish")       # settled ~4-6 days ago
    got = lsc.rolling_fatigue(rid)
    assert got["n"] == 20 and got["fatigued"]
