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

        # Look up draft responses for these reviews
        try:
            from models import get_conn as _gc
            _conn = _gc()
            draft_map = {}
            for r in urgent_reviews:
                if r.get("id"):
                    row = _conn.execute("SELECT draft_response FROM reviews WHERE id=?", (r["id"],)).fetchone()
                    if row and row["draft_response"]:
                        draft_map[r["id"]] = row["draft_response"]
            _conn.close()
        except Exception:
            draft_map = {}

        reviews_html = ""
        for r in urgent_reviews:
            rating = r.get("rating", 1)
            stars  = "★" * rating + "☆" * (5 - rating)
            draft  = draft_map.get(r.get("id"), "")
            draft_html = f"""
<div style="background:#f0faf4;border-left:3px solid #2d6a4f;border-radius:4px;
            padding:12px 14px;margin-top:8px">
  <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
              color:#2d6a4f;margin-bottom:6px">AI Draft Response</div>
  <div style="font-size:13px;color:#1a1714;line-height:1.7">{draft}</div>
  <div style="font-size:11px;color:#7a736a;margin-top:8px">
    Log in to approve, edit, or regenerate this response →
  </div>
</div>""" if draft else """
<div style="font-size:11px;color:#7a736a;margin-top:8px;font-style:italic">
  Draft response being prepared — log in to view it.
</div>"""
            reviews_html += f"""
<div style="background:#fff5f5;border-left:3px solid #c84b2f;border-radius:4px;
            padding:12px 14px;margin-bottom:10px">
  <div style="font-size:12px;font-weight:600;color:#c84b2f;margin-bottom:4px">
    {stars} — {r.get("author","Guest")} via {r.get("platform","").title()}
  </div>
  <div style="font-size:13px;color:#1a1714;line-height:1.6">{r.get("text","")}</div>
  {draft_html}
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
    {"A draft response is included below." if len(urgent_reviews)==1 else "Draft responses are included below."} Log in to approve or edit before posting.
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
        try:
            from models import log_email as _le, get_conn as _gc
            _c = _gc()
            _row = _c.execute("SELECT id FROM restaurants WHERE owner_email=? LIMIT 1", (owner_email,)).fetchone()
            _c.close()
            if _row: _le(_row[0], "urgent", owner_email, f"Urgent review alert — {restaurant_name}")
        except Exception: pass
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
                try:
                    from models import log_email as _le
                    _le(restaurant.id, "digest", owner_email, f"Weekly digest — {restaurant.name}")
                except Exception: pass
            except Exception as e:
                log.error(f"Digest failed for {restaurant.name}: {e}")

    except Exception as e:
        log.error(f"Weekly digest error: {e}")


def check_stale_inventory():
    """Alert Will when a client's inventory data is more than 7 days old."""
    if not RESEND_API_KEY:
        return
    try:
        from models import get_all_restaurants
        from datetime import datetime, timedelta
        import resend as _resend

        restaurants = get_all_restaurants()
        stale = []

        for r in restaurants:
            if not r.module_inventory or not r.is_active:
                continue
            # Check updated_at on inventory data
            conn = __import__('models').get_conn()
            row = conn.execute(
                "SELECT updated_at, inventory_source FROM restaurant_data WHERE restaurant_id=? LIMIT 1",
                (r.id,)
            ).fetchone()
            conn.close()

            if not row or not row["updated_at"]:
                stale.append((r.name, "never uploaded"))
                continue

            updated = datetime.fromisoformat(row["updated_at"])
            days_old = (datetime.now() - updated).days
            freq = getattr(r, 'inventory_frequency', 'weekly')
            threshold = 7 if freq == 'weekly' else (14 if freq == 'biweekly' else 30)
            if days_old >= threshold:
                stale.append((r.name, f"{days_old} days old"))

        if not stale:
            return

        stale_html = "".join([
            f'<tr><td style="padding:6px 12px;border-bottom:1px solid #f0ece6"><strong>{name}</strong></td>'            f'<td style="padding:6px 12px;border-bottom:1px solid #f0ece6;color:#c84b2f">{status}</td></tr>'
            for name, status in stale
        ])

        _resend.api_key = RESEND_API_KEY
        _resend.Emails.send({
            "from": f"Cavnar AI <{FROM_EMAIL}>",
            "to": ["will@cavnar.ai"],
            "subject": f"⚠ Stale inventory data — {len(stale)} client(s) need updating",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:20px">
    <h2 style="font-family:Georgia,serif;font-size:20px;font-weight:400;margin:0 0 4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </h2>
    <p style="font-size:11px;color:#7a736a;margin:0;letter-spacing:1px;text-transform:uppercase">
      Weekly Inventory Check
    </p>
  </div>
  <p style="font-size:14px;line-height:1.6;margin-bottom:16px">
    The following clients have inventory data that needs updating.
    Follow up to get a fresh CSV export from them this week.
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:13px;background:white;border:1px solid #e0dbd0;border-radius:6px;overflow:hidden">
    <thead>
      <tr style="background:#f7f4ef">
        <th style="padding:8px 12px;text-align:left;font-size:11px;color:#7a736a;font-weight:600">RESTAURANT</th>
        <th style="padding:8px 12px;text-align:left;font-size:11px;color:#7a736a;font-weight:600">STATUS</th>
      </tr>
    </thead>
    <tbody>{stale_html}</tbody>
  </table>
  <p style="font-size:12px;color:#7a736a;margin-top:16px">
    Update inventory data at <a href="https://dashboard.cavnar.ai/admin" style="color:#c84b2f">dashboard.cavnar.ai/admin</a>
    → client → Manage Data.
  </p>
</div>"""
        })
        log.info(f"Stale inventory alert sent for {len(stale)} client(s)")
        try:
            from models import log_email as _le, get_conn as _gc
            _c = _gc()
            _wrow = _c.execute("SELECT r.id FROM restaurants r JOIN users u ON u.restaurant_id=r.id WHERE u.email='will@cavnar.ai' AND u.is_admin=1 LIMIT 1").fetchone()
            _c.close()
            if _wrow: _le(_wrow[0], "stale_inventory", "will@cavnar.ai", f"Stale inventory — {len(stale)} client(s)")
        except Exception: pass
    except Exception as e:
        log.error(f"Stale inventory check error: {e}")


def refresh_expiring_tokens():
    """Refresh Instagram and Facebook tokens expiring within 7 days."""
    try:
        import requests as _req, os
        from models import get_all_restaurants, update_restaurant
        from datetime import datetime, timedelta

        app_id     = os.getenv("META_APP_ID","")
        app_secret = os.getenv("META_APP_SECRET","")
        if not app_id or not app_secret:
            return

        restaurants = get_all_restaurants()
        soon = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

        for r in restaurants:
            if not r.ig_token:
                continue
            expires = r.ig_token_expires or "2000-01-01"
            if expires > soon:
                continue  # Not expiring soon

            try:
                resp = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
                    "grant_type": "fb_exchange_token",
                    "client_id": app_id, "client_secret": app_secret,
                    "fb_exchange_token": r.ig_token,
                })
                if resp.status_code == 200:
                    new_token   = resp.json().get("access_token", r.ig_token)
                    new_expires = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
                    update_data = {"ig_token": new_token, "ig_token_expires": new_expires}
                    if r.fb_page_token:
                        resp2 = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
                            "grant_type": "fb_exchange_token",
                            "client_id": app_id, "client_secret": app_secret,
                            "fb_exchange_token": r.fb_page_token,
                        })
                        if resp2.status_code == 200:
                            update_data["fb_page_token"]    = resp2.json().get("access_token", r.fb_page_token)
                            update_data["fb_token_expires"] = new_expires
                    update_restaurant(r.id, update_data)
                    log.info(f"Refreshed IG/FB tokens for {r.name}, new expiry {new_expires}")
                else:
                    log.warning(f"Token refresh failed for {r.name}: {resp.text[:100]}")
            except Exception as e:
                log.error(f"Token refresh error for {r.name}: {e}")

    except Exception as e:
        log.error(f"refresh_expiring_tokens error: {e}")


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

            # Monday 6am — run competitor analysis for all clients
            if now.hour == 6 and now.weekday() == 0 and _last_fetch_date != today:
                log.info("Running weekly competitor analysis...")
                try:
                    from competitor import run_competitor_analysis
                    from models import get_all_restaurants
                    for r in get_all_restaurants():
                        if r.google_place_id and r.id:
                            try:
                                run_competitor_analysis(r.id)
                            except Exception as ce:
                                log.error(f"Competitor analysis failed for {r.name}: {ce}")
                except Exception as e:
                    log.error(f"Competitor analysis scheduler error: {e}")

            if now.hour == 7 and _last_fetch_date != today:
                log.info("Refreshing expiring IG/FB tokens...")
                refresh_expiring_tokens()

            if now.hour == 8 and _last_fetch_date != today:
                _last_fetch_date = today
                log.info("Running daily fetch + urgent alert check...")
                run_daily_fetch()

            if now.hour == 9 and _last_digest_date != today:
                _last_digest_date = today
                log.info("Running weekly digest check...")
                run_weekly_digests()

            if now.hour == 10 and now.weekday() == 0 and _last_digest_date != today:
                # Monday 10am — check for stale inventory data
                log.info("Running stale inventory check...")
                check_stale_inventory()

        except Exception as e:
            log.error(f"Scheduler loop error: {e}")

        time.sleep(3600)


def start_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    log.info("Scheduler thread started")
    return t
