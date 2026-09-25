"""Seed ~20 weeks of realistic reviews for PRODUCTION Simple EJ's
(restaurant_id 4) so the Rating trend chart, per-week bars, topic
heatmap and response performance all have real history on
dashboard.cavnar.ai instead of an empty inbox. Run via railway ssh,
targeting the volume-mounted reviews.db directly (same DB the app uses).
"""
import json
import random
import sqlite3
import os
from datetime import date, timedelta

RID = 4
TODAY = date(2026, 9, 17)
WEEKS = 20

random.seed(20260917)

POSITIVE = [
    ("Best slice in St. Charles, hands down. The crust has that perfect char and the sauce isn't drowning in sugar like everywhere else.", ["food_quality"]),
    ("Ordered the Italian sub for lunch and it's massive — easily two meals. Bread was fresh, not that stale roll you get most places.", ["food_quality", "value"]),
    ("Staff remembered my order from last time and had it ready before I even sat down. That's the kind of place worth coming back to.", ["service"]),
    ("Family of five, everyone found something they liked, and the bill wasn't painful. Will be back for game night pizza.", ["value", "food_quality"]),
    ("The garlic knots alone are worth the trip. Ordered a dozen extra to take home and regretted not getting more.", ["food_quality"]),
    ("Clean dining room, fast counter service, pizza came out hot. Nothing fancy but it's exactly what a neighborhood spot should be.", ["cleanliness", "wait_time"]),
    ("Called in a last-minute order for 15 people and they had it boxed and ready in 20 minutes. Lifesavers.", ["service", "wait_time"]),
    ("The meatball sub is criminally underrated. Sauce is homemade, you can tell.", ["food_quality"]),
    ("Great spot for takeout on a weeknight. Consistent every single time, which is rare these days.", ["food_quality", "takeout_delivery"]),
    ("Owner came out to check on our table personally — you don't see that kind of hospitality much anymore.", ["service", "ambiance"]),
    ("Delivery arrived hot and on time, pizza wasn't a soggy mess like the last place we tried.", ["takeout_delivery"]),
    ("Reasonably priced for the portion sizes. My kids' pizza night just got a permanent home.", ["value"]),
]

NEUTRAL = [
    ("Pizza was good, service was a little slow on a Friday night but they were clearly slammed.", ["wait_time"]),
    ("Solid sub, nothing life-changing but I'd order again. Wish they had more veggie options.", ["food_quality"]),
    ("Decent place, parking out front is tight during dinner rush.", ["ambiance"]),
    ("Food's fine, prices crept up a bit since last year but still fair for the area.", ["value"]),
]

NEGATIVE = [
    ("Waited almost 45 minutes for two subs on a Tuesday afternoon with barely anyone else in the store. Something's off with the kitchen flow.", ["wait_time", "service"]),
    ("Pizza showed up lukewarm and the cheese had that reheated texture. First bad experience here after years of ordering.", ["food_quality", "takeout_delivery"]),
    ("Called three times to place a catering order and never got a callback. Ended up going with a competitor.", ["service"]),
    ("Dining room floor was sticky and the bathroom needed attention. Food was fine but the space needs a deep clean.", ["cleanliness"]),
    ("Sub was thrown together sloppy, bread was stale on one side. Not the quality I remember from a few months back.", ["food_quality"]),
    ("Ordered online, arrived missing half the toppings we paid extra for. No easy way to flag it after the fact.", ["takeout_delivery", "food_quality"]),
]

AUTHORS = [
    "Mike D.", "Sarah T.", "Jennifer K.", "Chris P.", "Amanda R.", "David L.",
    "Rachel B.", "Tom H.", "Lauren M.", "Kevin S.", "Nicole F.", "Brian W.",
    "Jessica C.", "Mark A.", "Emily N.", "Ryan G.",
]


def _week_dates(weeks_back):
    week_start = TODAY - timedelta(days=weeks_back * 7 + TODAY.weekday())
    n = random.choice([1, 2, 2, 3, 3, 4])
    offsets = random.sample(range(7), n)
    return [week_start + timedelta(days=o) for o in offsets]


def _pick_review(week_idx):
    if 6 <= week_idx <= 11:
        weights = [0.35, 0.20, 0.45]
    elif week_idx < 4:
        weights = [0.55, 0.20, 0.25]
    else:
        weights = [0.72, 0.18, 0.10]
    bucket = random.choices(["pos", "neu", "neg"], weights=weights)[0]
    if bucket == "pos":
        text, cats = random.choice(POSITIVE)
        rating = random.choice([4, 5, 5, 5])
        sentiment = "positive"
    elif bucket == "neu":
        text, cats = random.choice(NEUTRAL)
        rating = 3
        sentiment = "neutral"
    else:
        text, cats = random.choice(NEGATIVE)
        rating = random.choice([1, 2, 2])
        sentiment = "negative"
    return text, cats, rating, sentiment


def main():
    p = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "reviews.db")
    conn = sqlite3.connect(p)
    # Seed data only ever goes into the DEMO: Erik's live account has the
    # same name, so refuse unless restaurant RID is still flagged is_demo.
    flag = conn.execute("SELECT is_demo FROM restaurants WHERE id=?", (RID,)).fetchone()
    if not flag or not flag[0]:
        raise SystemExit(f"restaurant {RID} is not a demo account; refusing to seed reviews into it")
    existing = conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=?", (RID,)).fetchone()[0]
    assert existing == 0, "restaurant 4 already has %d reviews — refusing to touch real data" % existing

    seq = 0
    inserted = 0
    for week_idx in range(WEEKS - 1, -1, -1):
        for d in sorted(_week_dates(week_idx)):
            text, cats, rating, sentiment = _pick_review(week_idx)
            seq += 1
            author = random.choice(AUTHORS)
            review_date = d.isoformat()
            fetched_at = f"{review_date} {random.randint(8,20):02d}:{random.randint(0,59):02d}:00"
            urgency = "high" if sentiment == "negative" and rating <= 2 else "normal"

            days_old = (TODAY - d).days
            if days_old > 10:
                status = "posted"
                approved_at = f"{(d + timedelta(days=random.randint(0, 2))).isoformat()} {random.randint(8,20):02d}:00:00"
                posted_at = approved_at
                draft = f"Thank you so much for the kind words, {author.split()[0]}! We'll pass this along to the team." if sentiment == "positive" else \
                        f"We appreciate you taking the time to share this, {author.split()[0]} — we're looking into it and would love the chance to make it right."
            elif days_old > 3:
                status = random.choice(["approved", "drafted"])
                approved_at = f"{d.isoformat()} 12:00:00" if status == "approved" else None
                posted_at = None
                draft = f"Thanks for stopping in, {author.split()[0]}! Glad you enjoyed it." if sentiment == "positive" else \
                        f"Sorry to hear this, {author.split()[0]} — we'd like to make it right. Please reach out directly."
            else:
                status = "pending"
                approved_at = None
                posted_at = None
                draft = None

            conn.execute("""
                INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
                                      review_date, fetched_at, sentiment, categories, urgency,
                                      draft_response, response_status, approved_at, posted_at, processed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            """, (RID, "google", f"seed-ej-prod-{seq}", author, rating, text,
                  review_date, fetched_at, sentiment, json.dumps(cats), urgency,
                  draft, status, approved_at, posted_at))
            inserted += 1

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=?", (RID,)).fetchone()[0]
    avg = conn.execute("SELECT ROUND(AVG(rating),2) FROM reviews WHERE restaurant_id=?", (RID,)).fetchone()[0]
    conn.close()
    print(f"inserted {inserted} reviews across {WEEKS} weeks — {n} total, avg rating {avg}")


if __name__ == "__main__":
    main()
