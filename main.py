"""
main.py — orchestrator + scheduler
Run modes:
  python main.py --demo          # full pipeline on sample_reviews.csv, no APIs needed
  python main.py --report-only   # rebuild + print digest from existing DB data
  python main.py --legacy-scheduler
                                 # the original single-restaurant schedule loop

The production scheduler is scheduler.scheduler_loop, run by hosted_dashboard
(or worker.py) under the database lease. The legacy loop below fetches,
drafts and emails a Monday digest for restaurant 1 with no lease, so `python
main.py` with no flags used to start a second, unleased scheduler that sent
real email (#161). It now refuses unless --legacy-scheduler is passed AND
scheduler.scheduling_allowed() is true, and each run takes the scheduler
lease first, so it can never run beside the real one. Removing the legacy
loop is a candidate for future cleanup after additional verification.
"""
import os, sys, schedule, time
from dotenv import load_dotenv

load_dotenv()

from models import init_db, create_restaurant, get_restaurant, Restaurant
from fetcher import fetch_google, ingest_csv, save_reviews
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

    elif "--legacy-scheduler" in sys.argv:
        from scheduler import scheduling_allowed
        import ops
        if not scheduling_allowed():
            print("Refusing: scheduler.scheduling_allowed() is false here (not on Railway, or a restore is "
                  "in progress). Set ALLOW_LOCAL_SCHEDULER=1 deliberately — this loop sends real email.")
            sys.exit(2)
        init_db()
        rid = get_or_create_restaurant()

        def _leased(fn):
            def run(**kw):
                # Never beside the production scheduler: only the lease
                # holder runs anything (ops.acquire_scheduler_lease).
                if not ops.acquire_scheduler_lease():
                    print("Skipped: another process holds the scheduler lease.")
                    return
                fn(**kw)
            return run
        schedule.every().day.at("08:00").do(_leased(run_daily), restaurant_id=rid)
        schedule.every().monday.at("09:00").do(_leased(run_weekly), restaurant_id=rid)
        print(f"Legacy scheduler running for {RESTAURANT_NAME}. Ctrl+C to stop.")
        while True:
            schedule.run_pending()
            time.sleep(60)

    else:
        print(__doc__)
        print("No mode given. The production scheduler runs inside hosted_dashboard.py (or worker.py); "
              "`python main.py` no longer starts a second, unleased one.")
        sys.exit(2)
