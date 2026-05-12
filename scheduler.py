"""
scheduler.py — Automatic weekly digest sender for all Cavnar AI clients
Runs as a background thread inside hosted_dashboard.py on Railway.
Checks every hour — sends digests to clients on their chosen day.
"""
import os, threading, time, logging
from datetime import datetime, timezone
from dotenv import load_dotenv
import pathlib
load_dotenv(pathlib.Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [scheduler] %(message)s")
log = logging.getLogger("scheduler")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "will@cavnar.ai")


def send_digest_for_restaurant(restaurant: dict):
    """Build and email the weekly digest for one restaurant."""
    try:
        from models import get_reviews_since, DB_PATH
        from reporter import build_report_from_db, render_html
        import resend as _resend
        from datetime import timedelta

        rid   = restaurant["id"]
        name  = restaurant["name"]
        email = restaurant.get("contact_email") or restaurant.get("owner_email")
        if not email:
            log.warning(f"No email for {name} (id={rid}), skipping")
            return

        since = (datetime.now() - timedelta(days=7)).isoformat()
        report = build_report_from_db(rid, name, days=7)

        if report.total_reviews == 0:
            log.info(f"No new reviews for {name} this week, skipping digest")
            return

        html = render_html(report, name)

        _resend.api_key = RESEND_API_KEY
        _resend.Emails.send({
            "from": f"Cavnar AI <{FROM_EMAIL}>",
            "to": [email],
            "subject": f"Your weekly review digest — {name}",
            "html": html,
        })
        log.info(f"Digest sent to {email} for {name} ({report.total_reviews} reviews)")

    except Exception as e:
        log.error(f"Digest failed for {restaurant.get('name','?')}: {e}")


def run_digest_check():
    """Check if any restaurants need their digest sent today."""
    try:
        from models import get_restaurants_for_digest
        today = datetime.now().strftime("%A").lower()  # e.g. "monday"
        restaurants = get_restaurants_for_digest(today)
        if not restaurants:
            return
        log.info(f"Sending digests for {len(restaurants)} restaurant(s) on {today.title()}")
        for r in restaurants:
            send_digest_for_restaurant(r)
    except Exception as e:
        log.error(f"Digest check error: {e}")


def run_fetch_check():
    """Fetch new reviews for all restaurants with live review tracking enabled."""
    try:
        from models import get_conn, get_restaurant, get_pending_analysis
        from fetcher import fetch_google, fetch_yelp, save_reviews
        from analyser import analyse_review
        from drafter import draft_response

        conn = get_conn()
        live_restaurants = conn.execute(
            "SELECT id FROM restaurants WHERE reviews_live=1"
        ).fetchall()
        conn.close()

        for row in live_restaurants:
            rid = row["id"]
            restaurant = get_restaurant(rid)
            if not restaurant:
                continue
            reviews = []
            if restaurant.google_place_id:
                try:
                    reviews += fetch_google(restaurant.google_place_id, rid)
                except Exception as e:
                    log.error(f"Google fetch error for {restaurant.name}: {e}")
            if restaurant.yelp_business_id:
                try:
                    reviews += fetch_yelp(restaurant.yelp_business_id, rid)
                except Exception as e:
                    log.error(f"Yelp fetch error for {restaurant.name}: {e}")

            if reviews:
                new = save_reviews(reviews)
                if new > 0:
                    log.info(f"Fetched {new} new reviews for {restaurant.name}")
                    # Analyse and draft
                    pending = get_pending_analysis(rid, limit=50)
                    for r in pending:
                        try:
                            analyse_review(r.id, r.rating, r.text)
                        except Exception as e:
                            log.error(f"Analysis error: {e}")
                    from models import get_pending_drafts
                    for r in get_pending_drafts(rid, limit=50):
                        try:
                            draft_response(r.id, r.rating, r.text, r.sentiment,
                                          restaurant.name, restaurant.voice_notes or "")
                        except Exception as e:
                            log.error(f"Draft error: {e}")

    except Exception as e:
        log.error(f"Fetch check error: {e}")


# ── Track what we've already sent today ───────────────────────────────────────
_last_digest_date = None
_last_fetch_date  = None


def scheduler_loop():
    """Main loop — runs every hour, checks if actions needed."""
    global _last_digest_date, _last_fetch_date
    log.info("Scheduler started")

    while True:
        try:
            now   = datetime.now()
            today = now.date()

            # Fetch reviews once per day at 7am
            if now.hour == 7 and _last_fetch_date != today:
                _last_fetch_date = today
                log.info("Running daily review fetch...")
                run_fetch_check()

            # Send digests once per day at 8am
            if now.hour == 8 and _last_digest_date != today:
                _last_digest_date = today
                log.info("Running digest check...")
                run_digest_check()

        except Exception as e:
            log.error(f"Scheduler loop error: {e}")

        time.sleep(3600)  # Check every hour


def start_scheduler():
    """Start the scheduler in a background daemon thread."""
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    log.info("Scheduler thread started")
    return t
