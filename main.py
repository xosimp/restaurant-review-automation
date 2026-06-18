"""
main.py — orchestrator + scheduler
Run modes:
  python main.py --demo          # full pipeline on sample_reviews.csv, no APIs needed
  python main.py --report-only   # rebuild + print digest from existing DB data
  python main.py                 # production scheduler (requires .env with API keys)
"""
import os, sys, schedule, time
from dotenv import load_dotenv

load_dotenv()

from models import init_db, create_restaurant, get_restaurant, Restaurant
from fetcher import fetch_google, fetch_yelp, ingest_csv, save_reviews
from analyser import analyse_pending
from drafter import draft_pending
from reporter import build_report, send_digest, print_console_report

RESTAURANT_NAME  = os.getenv("RESTAURANT_NAME", "Maplewood Kitchen")
OWNER_EMAIL      = os.getenv("OWNER_EMAIL", "owner@maplewoodkitchen.com")
GOOGLE_PLACE_ID  = os.getenv("GOOGLE_PLACE_ID")
YELP_BUSINESS_ID = os.getenv("YELP_BUSINESS_ID")


def get_or_create_restaurant() -> int:
    """Return restaurant id=1, creating it from env vars if needed."""
    r = get_restaurant(1)
    if r:
        return r.id
    rid = create_restaurant(Restaurant(
        name=RESTAURANT_NAME,
        owner_email=OWNER_EMAIL,
        google_place_id=GOOGLE_PLACE_ID,
        yelp_business_id=YELP_BUSINESS_ID,
        voice_notes="Warm, genuine tone. Always invite guests back. Never sound corporate.",
    ))
    print(f"  Created restaurant: {RESTAURANT_NAME} (id={rid})")
    return rid


def run_demo():
    """End-to-end demo: CSV → analyse → draft → print digest. No live APIs needed."""
    print("\n" + "═"*60)
    print("  DEMO MODE — Maplewood Kitchen")
    print("  No live API keys required except ANTHROPIC_API_KEY")
    print("═"*60 + "\n")

    init_db()
    rid = get_or_create_restaurant()

    print("Step 1 — Ingesting sample reviews...")
    reviews = ingest_csv("sample_reviews.csv", rid)
    new, _ = save_reviews(reviews)
    print(f"  {new} reviews loaded ({len(reviews) - new} already in DB)\n")

    print("Step 2 — Analysing with Claude...")
    analyse_pending(rid)
    print()

    print("Step 3 — Drafting responses with Claude...")
    draft_pending(rid)
    print()

    print("Step 4 — Building weekly digest...")
    report = build_report(rid, RESTAURANT_NAME, days=365)  # wide window for demo
    print_console_report(report, RESTAURANT_NAME)

    return report


def run_daily(restaurant_id: int):
    print("\n--- Daily fetch ---")
    reviews = []
    if GOOGLE_PLACE_ID:
        reviews += fetch_google(GOOGLE_PLACE_ID, restaurant_id)
    if YELP_BUSINESS_ID:
        reviews += fetch_yelp(YELP_BUSINESS_ID, restaurant_id)
    new, _ = save_reviews(reviews)
    print(f"  {new} new reviews saved")
    analyse_pending(restaurant_id)
    draft_pending(restaurant_id)
    print("  Done.\n")


def run_weekly(restaurant_id: int):
    print("\n--- Weekly digest ---")
    report = build_report(restaurant_id, RESTAURANT_NAME)
    restaurant = get_restaurant(restaurant_id)
    send_digest(report, RESTAURANT_NAME, restaurant.owner_email)


if __name__ == "__main__":
    if "--demo" in sys.argv:
        run_demo()

    elif "--report-only" in sys.argv:
        init_db()
        rid = get_or_create_restaurant()
        report = build_report(rid, RESTAURANT_NAME, days=365)
        print_console_report(report, RESTAURANT_NAME)

    else:
        init_db()
        rid = get_or_create_restaurant()
        schedule.every().day.at("08:00").do(run_daily, restaurant_id=rid)
        schedule.every().monday.at("09:00").do(run_weekly, restaurant_id=rid)
        print(f"Scheduler running for {RESTAURANT_NAME}. Ctrl+C to stop.")
        while True:
            schedule.run_pending()
            time.sleep(60)
