"""What makes a review "urgent" to the owner (Will, 9/29/26): the analyser's
safety/legal call, or a 1-2 star review still owed a reply. The stored
urgency column is not widened — it drives the health alert."""
from datetime import date, timedelta

import models
from models import Restaurant, create_restaurant


def _review(db_path, rid, n, rating, status="pending", urgency="normal", days_ago=2):
    day = (date.today() - timedelta(days=days_ago)).isoformat()
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                 "fetched_at, processed, sentiment, urgency, response_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, "google", f"g{n}", f"Guest {n}", rating, "text", day, day, 1,
                  "negative" if rating <= 2 else "positive", urgency, status))
    conn.commit()
    conn.close()


def test_a_two_star_review_owed_a_reply_is_urgent_and_an_answered_or_old_one_is_not(db_path, monkeypatch):
    monkeypatch.setattr(models, "DB_PATH", db_path)
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = create_restaurant(Restaurant(name="Urgent Co", owner_email="u@x.test"), db_path=db_path)
    _review(db_path, rid, 1, 2)                                  # owed: urgent
    _review(db_path, rid, 2, 2, status="posted")                 # answered
    _review(db_path, rid, 3, 1, days_ago=45)                     # older than the owed window
    _review(db_path, rid, 4, 5, urgency="high", status="posted") # the analyser's safety call stands
    _review(db_path, rid, 5, 3)                                  # 3 stars: not urgent
    rows = {r["external_id"]: r["urgent"] for r in models.get_reviews_data(rid)}
    assert rows == {"g1": True, "g2": False, "g3": False, "g4": True, "g5": False}
    assert {r["external_id"] for r in models.get_reviews_data(rid, filter_by="urgent")} == {"g1", "g4"}
    assert models.get_reviews_data(rid)[0]["external_id"] == "g1"  # the owed 2-star leads the inbox
    assert models.get_review_stats(rid)["urgent"] == 1           # unanswered urgent only
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT urgency FROM reviews WHERE external_id='g1'").fetchone()[0] == "normal"
    conn.close()
