"""
scheduler.py — Cavnar AI background scheduler
Runs as a daemon thread inside hosted_dashboard.py on Railway.

Jobs:
  7:00am daily  — fetch new reviews for all live clients
                — analyse & draft responses automatically
                — send IMMEDIATE urgent alert to owner if critical review found
  8:00am weekly — send weekly digest to clients on their chosen day
"""
import os, threading, time, logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import pathlib

load_dotenv(pathlib.Path(__file__).parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [scheduler] %(message)s")
log = logging.getLogger("scheduler")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", "will@cavnar.ai")


def send_urgent_alert(restaurant_name, owner_email, urgent_reviews):
    """Email the owner immediately when a high-urgency review comes in."""
    if not RESEND_API_KEY or not owner_email:
        log.warning(f"Cannot send urgent alert for {restaurant_name} — no key/email")
        return
    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY

        reviews_html = ""
        for r in urgent_reviews:
            rating = r.get("rating", 1)
            stars  = "★" * rating + "☆" * (5 - rating)
            reviews_html += f"""
<div style="background:#fff5f5;border-left:3px solid #c84b2f;border-radius:4px;
            padding:12px 14px;margin-bottom:10px">
  <div style="font-size:12px;font-weight:600;color:#c84b2f;margin-bottom:4px">
    {stars} — {r.get("author","Guest")} via {r.get("platform","").title()}
  </div>
  <div style="font-size:13px;color:#1a1714;line-height:1.6">{r.get("text","")}</div>
</div>"""

        _resend.Emails.send({
            "from": f"Cavnar AI Alerts <{FROM_EMAIL}>",
            "to": [owner_email],
            "subject": f"\u26a0 Urgent review alert \u2014 {restaurant_name}",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:20px">
    <h2 style="font-family:Georgia,serif;font-size:20px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Urgent Review Alert
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6;margin-bottom:6px">
    <strong>{restaurant_name}</strong> received
    {"a review" if len(urgent_reviews)==1 else f"{len(urgent_reviews)} reviews"}
    that {"needs" if len(urgent_reviews)==1 else "need"} immediate attention.
  </p>
  <p style="font-size:13px;color:#7a736a;margin-bottom:16px">
    A draft response has been prepared. Please log in and approve it as soon as possible.
  </p>
  {reviews_html}
  <div style="margin-top:20px">
    <a href="https://dashboard.cavnar.ai"
       style="display:inline-block;background:#c84b2f;color:white;padding:11px 22px;
              border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">
      Review &amp; approve response &#8594;
    </a>
  </div>
  <hr style="border:none;border-top:1px solid #e0dbd0;margin:24px 0"/>
  <p style="font-size:12px;color:#7a736a;margin:0">
    Cavnar AI &#183;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &#183;
    <a href="mailto:{FROM_EMAIL}" style="color:#c84b2f;text-decoration:none">{FROM_EMAIL}</a>
  </p>
</div>"""
        })
        log.info(f"Urgent alert sent to {owner_email} for {restaurant_name}")
    except Exception as e:
        log.error(f"Urgent alert failed for {restaurant_name}: {e}")


def get_owner_email(restaurant_id):
    """Get the owner's email from users table (most reliable source)."""
    from models import get_conn, get_restaurant
    conn = get_conn()
    row = conn.execute(
        "SELECT email FROM users WHERE restaurant_id=? AND is_admin=0 LIMIT 1",
        (restaurant_id,)
    ).fetchone()
    conn.close()
    if row:
        return row["email"]
    r = get_restaurant(restaurant_id)
    return r.owner_email if r else None


def run_daily_fetch():
    """Fetch reviews for all live clients, analyse, draft, alert on urgent."""
    try:
        from models import get_conn, get_restaurant, save_reviews
        from models import get_pending_analysis, get_pending_drafts, update_last_fetched
        from fetcher import fetch_google, fetch_yelp
        from analyser import analyse_review
        from drafter import draft_response

        conn = get_conn()
        live = conn.execute(
            "SELECT id FROM restaurants WHERE reviews_live=1"
        ).fetchall()
        conn.close()

        if not live:
            return

        log.info(f"Daily fetch for {len(live)} live restaurant(s)")

        for row in live:
            rid = row["id"]
            restaurant = get_restaurant(rid)
            if not restaurant:
                continue

            # Fetch
            reviews = []
            if restaurant.google_place_id:
                try:
                    reviews += fetch_google(restaurant.google_place_id, rid)
                except Exception as e:
                    log.error(f"Google fetch [{restaurant.name}]: {e}")
            if restaurant.yelp_business_id:
                try:
                    reviews += fetch_yelp(restaurant.yelp_business_id, rid)
                except Exception as e:
                    log.error(f"Yelp fetch [{restaurant.name}]: {e}")

            update_last_fetched(rid)

            if not reviews:
                continue

            new_count = save_reviews(reviews)
            if new_count == 0:
                continue

            log.info(f"{new_count} new reviews for {restaurant.name}")

            # Analyse
            for r in get_pending_analysis(rid, limit=50):
                try:
                    analyse_review(r.id, r.rating, r.text)
                except Exception as e:
                    log.error(f"Analyse error: {e}")

            # Draft
            for r in get_pending_drafts(rid, limit=50):
                try:
                    draft_response(r.id, r.rating, r.text, r.sentiment,
                                  restaurant.name, restaurant.voice_notes or "")
                except Exception as e:
                    log.error(f"Draft error: {e}")

            # Check for urgent reviews fetched in last hour
            conn = get_conn()
            urgent = conn.execute("""
                SELECT * FROM reviews
                WHERE restaurant_id=?
                  AND urgency='high'
                  AND response_status NOT IN ('posted','approved','skipped')
                  AND fetched_at >= datetime('now', '-2 hours')
            """, (rid,)).fetchall()
            conn.close()

            if urgent:
                owner_email = get_owner_email(rid)
                if owner_email:
                    send_urgent_alert(restaurant.name, owner_email,
                                     [dict(r) for r in urgent])

    except Exception as e:
        log.error(f"Daily fetch error: {e}")


def run_weekly_digests():
    """Send weekly digest to all restaurants scheduled for today."""
    try:
        from models import get_restaurants_for_digest, get_restaurant
        from reporter import build_report_from_db, render_html
        import resend as _resend

        today = datetime.now().strftime("%A").lower()
        scheduled = get_restaurants_for_digest(today)

        if not scheduled:
            return

        log.info(f"Weekly digests for {len(scheduled)} restaurant(s) on {today.title()}")

        for row in scheduled:
            rid = row["id"]
            restaurant = get_restaurant(rid)
            if not restaurant:
                continue

            owner_email = get_owner_email(rid)
            if not owner_email:
                log.warning(f"No email for {restaurant.name}, skipping")
                continue

            try:
                report = build_report_from_db(rid, restaurant.name, days=7)
                if report.total_reviews == 0:
                    log.info(f"No reviews this week for {restaurant.name}")
                    continue

                html = render_html(report, restaurant.name)
                _resend.api_key = RESEND_API_KEY
                _resend.Emails.send({
                    "from": f"Cavnar AI <{FROM_EMAIL}>",
                    "to": [owner_email],
                    "subject": f"Your weekly review digest \u2014 {restaurant.name}",
                    "html": html,
                })
                log.info(f"Digest sent to {owner_email} for {restaurant.name}")
            except Exception as e:
                log.error(f"Digest failed for {restaurant.name}: {e}")

    except Exception as e:
        log.error(f"Weekly digest error: {e}")


# ── Scheduler loop ────────────────────────────────────────────────────────────

_last_fetch_date  = None
_last_digest_date = None


def scheduler_loop():
    global _last_fetch_date, _last_digest_date
    log.info("Scheduler started — daily fetch 8am, digests 9am on client's chosen day")

    while True:
        try:
            now   = datetime.now()
            today = now.date()

            if now.hour == 8 and _last_fetch_date != today:
                _last_fetch_date = today
                log.info("Running daily fetch + urgent alert check...")
                run_daily_fetch()

            if now.hour == 9 and _last_digest_date != today:
                _last_digest_date = today
                log.info("Running weekly digest check...")
                run_weekly_digests()

        except Exception as e:
            log.error(f"Scheduler loop error: {e}")

        time.sleep(3600)


def start_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    log.info("Scheduler thread started")
    return t
