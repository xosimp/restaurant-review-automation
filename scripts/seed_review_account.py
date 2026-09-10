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

    update_restaurant(rid, {
        "billing_status": "internal",     # entitled, but not a client for MRR or email sequences
        "two_fa_enabled": 0,              # a reviewer cannot receive a code
        "marketing_emails_opt_out": 1,    # never enrol it in a campaign
        "module_reviews": 1, "module_labor": 1,
        "module_inventory": 1, "module_marketing": 1,
        "reviews_live": 0,                # the scheduled fetch skips it; seeded data stays put
        "alert_max_per_day": 5,
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
