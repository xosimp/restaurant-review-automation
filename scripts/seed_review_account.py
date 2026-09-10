#!/usr/bin/env python3
"""Create the App Store review account.

Apple cannot approve an app they cannot sign into (Guideline 2.1), and this
app no longer offers self-serve signup — /mobile/api/register returns 403
unless ALLOW_PUBLIC_SIGNUP is set, and Apple/Google sign-in both refuse an
unknown identity. So the reviewer needs a real account, made in advance, with
credentials in the App Store Connect review notes.

What it makes, and why each choice matters for review:

  * Two-factor OFF. A reviewer cannot receive an emailed or texted code, and
    a login they cannot complete is the same rejection as no login at all.
  * billing_status 'internal'. It counts as a paid account so nothing is
    entitlement-gated mid-review, and 'internal' is excluded from client MRR
    and from the monthly-summary and onboarding email sequences.
  * All four modules on, so every tab the reviewer can see actually opens.
  * marketing_emails_opt_out set, so the account never receives a campaign.
  * reviews_live left OFF, so the scheduled fetch skips it. The demo data is
    seeded here and stays put rather than being overwritten by a live pull.

Run it against production:

    DB=/app/data/reviews.db python3 scripts/seed_review_account.py

Re-running is safe: it updates the existing account and rotates the password
rather than creating a second one. It prints the credentials once, at the end.
"""
import argparse
import os
import secrets
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REVIEW_USERNAME = os.getenv("REVIEW_USERNAME", "appreview")
REVIEW_EMAIL = os.getenv("REVIEW_EMAIL", "appreview@cavnar.ai")
REVIEW_RESTAURANT = os.getenv("REVIEW_RESTAURANT", "The Copper Table")


def _password() -> str:
    """Readable enough to retype from the review notes, strong enough to be
    a real password. No characters that are ambiguous in a sans-serif font."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(3))


# ── Demo content ────────────────────────────────────────────────────────────
#
# A reviewer opening Labor or Food Cost on an empty account sees empty states,
# which reviews badly and says nothing about what the app does. Everything
# below is invented for a restaurant that does not exist — no real person's
# review, no real business's numbers — and the restaurant is flagged is_demo,
# which is the flag the rest of the codebase uses to mean "seed data, never a
# client".

_DEMO_REVIEWS = [
    # (days_ago, rating, author, text, response_status)
    (2,  5, "Marcus L.", "Came in for the game and ended up staying for dinner. Wings were genuinely great and the staff kept our glasses full without hovering. Back next week.", "posted"),
    (3,  4, "Priya R.",  "Solid burger, good beer list. Took a while to get a table on a Saturday but they were honest about the wait.", "posted"),
    (5,  2, "Dan W.",    "Ordered the fish and chips and it came out lukewarm. Server was apologetic and took it off the bill, so credit for that, but it soured the evening.", "approved"),
    (6,  5, "Aisha K.",  "Best sports bar on this side of town. Twelve screens, none of them showing the same thing, and the kitchen is open late.", "posted"),
    (8,  5, "Tom B.",    "Booked the back room for a birthday. They sorted the whole thing over email in a day and the food came out together for eighteen people.", "posted"),
    (9,  3, "Erin M.",   "Fine. Drinks are well priced, food is average. Music was loud enough that we gave up on conversation.", "drafted"),
    (11, 4, "Luis F.",   "Good spot to watch a match with friends. Wish they took reservations on game nights.", "posted"),
    (13, 1, "Kayla S.",  "Waited forty minutes for food on a quiet Tuesday and nobody checked on us once. Walked out and paid for the drinks at the bar.", "posted"),
    (15, 5, "Ben O.",    "The pretzel bites alone are worth the trip. Friendly bar staff who actually know the beer list.", "posted"),
    (18, 4, "Nadia H.",  "Nice change from the usual chain sports bars. Real kitchen, real beer, still shows every game.", "posted"),
]

_DEMO_ROLES = [("Server", 4), ("Bartender", 2), ("Cook", 3), ("Host", 1), ("Busser", 2)]
_DEMO_NAMES = ["Marcus T.", "Jamie L.", "Priya K.", "Derek M.", "Sofia R.", "Carlos B.",
               "Amy C.", "James H.", "Tara N.", "Owen P.", "Lena V.", "Rafa D."]


def _demo_shifts_csv(weeks=3):
    """Three weeks of shifts with a sales figure per day, in the ten-column
    shape the Labor module's own template documents."""
    from datetime import date, timedelta
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    today = date.today()
    name_i = 0
    for back in range(weeks * 7, 0, -1):
        d = today - timedelta(days=back)
        weekday = d.strftime("%A")
        # A sports bar: weekends and Thursday carry the week.
        # Tuned so labor lands a little OVER the 30% target. A demo where the
        # restaurant is already under target shows a green number and nothing
        # else; over target is the case the module exists for, so the savings
        # figure and the AI recommendation both have something to say.
        base = {"Friday": 8300, "Saturday": 9500, "Sunday": 7300, "Thursday": 6300}.get(weekday, 4400)
        sales = base + (d.day * 37) % 900
        for role, count in _DEMO_ROLES:
            for n in range(count):
                emp = _DEMO_NAMES[name_i % len(_DEMO_NAMES)]
                name_i += 1
                if role == "Bartender":
                    start, end, hrs = "16:00", "24:00", 8
                elif role == "Cook":
                    start, end, hrs = "10:00", "18:00", 8
                elif role == "Host":
                    start, end, hrs = "17:00", "22:00", 5
                else:
                    start, end, hrs = "11:00", "17:00", 6
                actual = hrs + (0.4 if (d.day + n) % 5 == 0 else 0.0)
                rows.append(f"{d.isoformat()},{weekday},{emp},{role},{start},{end},{hrs},{actual},{sales},")
    return "\n".join(rows)


_DEMO_INVENTORY_CSV = """item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week
Chicken wings,protein,120,74,2.85,16.5,120,4
Ground beef,protein,80,61,4.20,9.0,80,2
Cod fillet,protein,40,12,6.75,5.5,40,14
Mozzarella,dairy,50,38,3.10,6.0,50,1
Cheddar,dairy,45,41,2.95,5.2,45,0
Potatoes,produce,90,52,0.65,12.0,90,26
Lettuce,produce,30,9,1.40,4.5,30,11
Tomatoes,produce,35,22,1.85,4.0,35,3
Burger buns,dry,150,96,0.42,18.0,150,2
Wing sauce,dry,24,17,5.50,2.5,24,0
Draft IPA keg,beverage,6,3,145.00,0.8,6,0
Draft lager keg,beverage,8,5,128.00,1.1,8,0
House red wine,beverage,24,19,8.50,2.0,24,1
Well vodka,beverage,12,8,14.25,1.2,12,0
"""


def _seed_demo_data(rid, db_path):
    """Populate the review account so no screen opens empty."""
    from datetime import datetime, timedelta
    from models import Review, save_reviews, save_client_data, get_conn

    conn = get_conn(db_path)
    already = conn.execute("SELECT COUNT(*) c FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()["c"]
    conn.close()

    if already:
        print(f"Demo data already present ({already} reviews) — leaving it alone.")
    else:
        now = datetime.now()
        reviews = []
        for days_ago, rating, author, text, status in _DEMO_REVIEWS:
            when = now - timedelta(days=days_ago)
            reviews.append(Review(
                restaurant_id=rid,
                platform="google",
                # Namespaced so it can never collide with a real Google review id.
                external_id=f"demo-appreview-{days_ago}",
                author=author,
                rating=rating,
                text=text,
                review_date=when.strftime("%Y-%m-%d"),
                fetched_at=when.strftime("%Y-%m-%d %H:%M:%S"),
            ))
        count, _ = save_reviews(reviews, db_path=db_path)

        # Give them the analysed/drafted/posted spread a live account has, so
        # the Reviews tab shows something in each state rather than a wall of
        # "needs a reply".
        conn = get_conn(db_path)
        for days_ago, rating, _a, _t, status in _DEMO_REVIEWS:
            sentiment = "positive" if rating >= 4 else ("negative" if rating <= 2 else "neutral")
            draft = ("Thank you for taking the time to write this — it means a lot to the team."
                     if rating >= 4 else
                     "I'm sorry this was your experience. That isn't the standard we hold "
                     "ourselves to, and I'd like to make it right — please email us.")
            conn.execute(
                "UPDATE reviews SET sentiment=?, response_status=?, draft_response=?, processed=1 "
                "WHERE restaurant_id=? AND external_id=?",
                (sentiment, status, draft, rid, f"demo-appreview-{days_ago}"))
        conn.commit()
        conn.close()
        print(f"Seeded {count} reviews across every response state.")

    save_client_data(rid, "shifts", _demo_shifts_csv(), source="demo", db_path=db_path)
    save_client_data(rid, "inventory", _DEMO_INVENTORY_CSV, source="demo", db_path=db_path)
    print("Seeded 3 weeks of shifts and a 14-item inventory.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("DB"), help="path to reviews.db (default: models.DB_PATH)")
    ap.add_argument("--keep-password", action="store_true",
                    help="do not rotate the password if the account already exists")
    args = ap.parse_args()

    import models
    from models import Restaurant, create_restaurant, get_conn, update_restaurant
    import auth
    from auth import create_user, init_auth

    db_path = args.db or models.DB_PATH
    print(f"Using database: {db_path}")
    init_auth(db_path=db_path)

    conn = get_conn(db_path)
    existing = conn.execute(
        "SELECT id, restaurant_id FROM users WHERE LOWER(username)=? OR LOWER(email)=?",
        (REVIEW_USERNAME.lower(), REVIEW_EMAIL.lower()),
    ).fetchone()
    conn.close()

    password = _password()

    if existing:
        rid = existing["restaurant_id"]
        print(f"Review account already exists (user {existing['id']}, restaurant {rid}).")
        if not args.keep_password:
            auth.update_password(existing["id"], password, db_path=db_path)
            print("Password rotated.")
        else:
            password = "(unchanged — pass without --keep-password to rotate)"
    else:
        rid = create_restaurant(Restaurant(
            name=REVIEW_RESTAURANT,
            owner_email=REVIEW_EMAIL,
            owner_name="App Review",
            sign_off_name=REVIEW_RESTAURANT,
            neighborhood="Chicago, Illinois",
            known_for="Sports bar food, wings, burgers, craft beer",
            vibe="Neighborhood sports bar and kitchen",
        ), db_path=db_path)
        create_user(restaurant_id=rid, username=REVIEW_USERNAME, email=REVIEW_EMAIL,
                    password=password, db_path=db_path)
        print(f"Created restaurant {rid} and user {REVIEW_USERNAME}.")

    _seed_demo_data(rid, db_path)

    update_restaurant(rid, {
        "billing_status": "internal",     # entitled, but not a client for MRR or email sequences
        "two_fa_enabled": 0,              # a reviewer cannot receive a code
        "marketing_emails_opt_out": 1,    # never enrol it in a campaign
        "module_reviews": 1, "module_labor": 1,
        "module_inventory": 1, "module_marketing": 1,
        "reviews_live": 0,                # the scheduled fetch skips it; seeded data stays put
        "alert_max_per_day": 5,
        "is_demo": 1,                     # seed data, never a client — the flag the rest of the codebase keys on
    }, db_path=db_path)

    print("\n" + "=" * 62)
    print("  APP STORE CONNECT → App Review Information → Sign-In Required")
    print("=" * 62)
    print(f"  Username: {REVIEW_USERNAME}")
    print(f"  Password: {password}")
    print("=" * 62)
    print("\nCheck it before submitting:")
    print(f"  curl -s -X POST https://dashboard.cavnar.ai/mobile/api/login \\")
    print(f"    -H 'Content-Type: application/json' \\")
    print(f"    -d '{{\"username\":\"{REVIEW_USERNAME}\",\"password\":\"<the password above>\"}}'")
    print("\nA successful response contains \"ok\": true and a token.")


if __name__ == "__main__":
    main()
