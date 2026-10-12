"""Most recent and This month on the Reviews inbox (owner, 10/12/26: no way
to see the latest reviews or this month's). Both read newest first; every
other view still puts what needs you first."""
from datetime import date, timedelta
from pathlib import Path

import models
from models import Restaurant, create_restaurant, get_conn

SRC = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text(encoding="utf-8")


def _seed(db_path):
    rid = create_restaurant(Restaurant(name="R", owner_email="r@x.test"), db_path=db_path)
    today = date.today()
    first = today.replace(day=1)
    rows = [("old-urgent", 1, (first - timedelta(days=20)).isoformat(), "pending"),
            ("this-month-answered", 5, first.isoformat(), "posted"),
            ("newest", 4, today.isoformat(), "posted")]
    conn = get_conn(db_path)
    for ext, rating, d, st in rows:
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                     "fetched_at, response_status, processed) VALUES (?,?,?,?,?,?,?,datetime('now'),?,1)",
                     (rid, "google", ext, ext, rating, "t", d + "T12:00:00", st))
    conn.commit()
    conn.close()
    return rid


def test_most_recent_is_newest_first_and_this_month_is_only_this_month(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = _seed(db_path)
    all_ = [r["external_id"] for r in models.get_reviews_data(rid, "all")]
    assert all_[0] == "old-urgent", "every other view: what needs you first"
    recent = [r["external_id"] for r in models.get_reviews_data(rid, "recent")]
    assert recent == ["newest", "this-month-answered", "old-urgent"]
    month = [r["external_id"] for r in models.get_reviews_data(rid, "month")]
    assert month == ["newest", "this-month-answered"]


def test_the_pills_are_on_the_inbox_and_always_ask_the_server():
    assert "onclick=\"setRF('recent',this)\">Most recent</button>" in SRC
    assert "onclick=\"setRF('month',this)\">This month</button>" in SRC
    assert "var ordered=/^(recent|month)\\|/.test(want);" in SRC
    assert "if(rfilter==='recent'||rfilter==='month')return;" in SRC
