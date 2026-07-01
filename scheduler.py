"""
scheduler.py — Cavnar AI background scheduler
Runs as a daemon thread inside hosted_dashboard.py on Railway.

Jobs:
  2:00am daily  — backup reviews.db and email to will@cavnar.ai
  10:00am daily — onboarding email sequence (day 2, 7, 30)
  11:00am Monday — inactive client check (14+ days no login)
  7:00am daily  — fetch new reviews for all live clients
                — analyse & draft responses automatically
                — send IMMEDIATE urgent alert to owner if critical review found
  8:00am weekly — send weekly digest to clients on their chosen day
"""
import os, threading, time, logging, html as _html
from status_manager import record_scheduler_heartbeat, run_health_checks
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo as _ZI_sch
def _chi_now():
    return datetime.now(_ZI_sch('America/Chicago')).replace(tzinfo=None)
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
  <div style="font-size:13px;color:#1a1714;line-height:1.7">{_html.escape(draft)}</div>
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
    {stars} — {_html.escape(r.get("author","Guest"))} via {_html.escape(r.get("platform","").title())}
  </div>
  <div style="font-size:13px;color:#1a1714;line-height:1.6">{_html.escape(r.get("text",""))}</div>
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
            "SELECT id FROM restaurants WHERE reviews_live=1 OR gmb_refresh_token IS NOT NULL"
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
            if restaurant.gmb_refresh_token:
                try:
                    from gmb import get_valid_token, fetch_reviews_via_gmb, get_gmb_account_id, get_gmb_location_id
                    token = get_valid_token(rid)
                    if token:
                        loc_id = restaurant.gmb_location_id
                        if not loc_id and restaurant.google_place_id:
                            acct_id = get_gmb_account_id(token)
                            if acct_id:
                                loc_id = get_gmb_location_id(token, acct_id, restaurant.google_place_id)
                        if loc_id:
                            reviews += fetch_reviews_via_gmb(token, loc_id, rid)
                except Exception as e:
                    log.error(f"GMB fetch [{restaurant.name}]: {e}")
            elif restaurant.google_place_id:
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

            new_count, new_reviews = save_reviews(reviews)
            if new_count == 0:
                continue

            log.info(f"{new_count} new reviews for {restaurant.name}")

            # Fire SMS/email alerts for newly saved reviews
            try:
                from notify import fire_review_alerts
                fire_review_alerts(rid, restaurant.name, new_reviews)
            except Exception as _ae:
                log.error(f"Alert fire error [{restaurant.name}]: {_ae}")

            # Fire outbound webhooks for each new review
            try:
                from webhooks import fire_webhook as _fw
                for _nr in new_reviews:
                    _payload = {
                        "platform": _nr.platform,
                        "rating":   _nr.rating,
                        "author":   _nr.author,
                        "body":     (_nr.text or "")[:500],
                        "sentiment": getattr(_nr, "sentiment", None),
                    }
                    _fw(rid, "review.received", _payload)
                    if (_nr.rating or 5) <= 2:
                        _fw(rid, "review.negative", _payload)
                    if (_nr.rating or 0) >= 4:
                        _fw(rid, "review.positive", _payload)
            except Exception:
                pass

            # Analyse
            for r in get_pending_analysis(rid, limit=50):
                try:
                    analyse_review(r.id, r.rating, r.text)
                except Exception as e:
                    log.error(f"Analyse error: {e}")

            # Draft — include approved examples for style learning
            from models import get_approved_examples
            approved_examples = get_approved_examples(rid, limit=4)
            for r in get_pending_drafts(rid, limit=50):
                try:
                    draft_response(r.id, r.rating, r.text, r.sentiment,
                                  restaurant.name, restaurant.voice_notes or "",
                                  approved_examples=approved_examples,
                                  sign_off=restaurant.sign_off_name or restaurant.name,
                                  never_say=restaurant.never_say or "")
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

            # Email + SMS alerts now handled by notify.fire_review_alerts() at ingest time

    except Exception as e:
        log.error(f"Daily fetch error: {e}")


def run_weekly_digests():
    """Send weekly digest to all restaurants scheduled for today."""
    try:
        from models import get_restaurants_for_digest, get_restaurant
        from reporter import build_report_from_db, render_html
        import resend as _resend

        today = _chi_now().strftime("%A").lower()
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
                # Only skip if reviews-only client AND no reviews this week
                # Full system clients always get a digest (labor/inventory data still valuable)
                has_other_modules = (restaurant.module_labor or
                                     restaurant.module_inventory or
                                     restaurant.module_marketing)
                if report.total_reviews == 0 and not has_other_modules:
                    log.info(f"No reviews this week for {restaurant.name} — skipping digest")
                    continue

                owner_name = restaurant.sign_off_name or restaurant.owner_email.split("@")[0].title()
                html = render_html(report, restaurant.name, owner_name=owner_name, restaurant_id=restaurant.id)
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
                try:
                    from webhooks import fire_webhook as _fw_rep
                    _fw_rep(restaurant.id, "report.weekly", {"restaurant": restaurant.name, "email": owner_email})
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
            if not r.module_inventory or r.billing_status not in ("trial", "active"):
                continue
            # Check updated_at on inventory data
            conn = __import__('models').get_conn()
            row = conn.execute(
                "SELECT updated_at, inventory_source FROM client_data WHERE restaurant_id=? LIMIT 1",
                (r.id,)
            ).fetchone()
            conn.close()

            if not row or not row["updated_at"]:
                stale.append((r.name, "never uploaded"))
                continue

            updated = datetime.fromisoformat(row["updated_at"])
            days_old = (_chi_now() - updated).days
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


def run_toast_sync():
    """
    Nightly sync: pull fresh Toast POS data for every connected restaurant
    and write it into client_data.shifts_csv so the Labor module stays current.
    Runs at 3am CT — before the 8am review fetch, so labor data is ready for the day.
    """
    try:
        from models import get_all_restaurants
        from toast import is_connected, sync_to_db

        restaurants = get_all_restaurants()
        connected   = [r for r in restaurants if is_connected(r.id)]

        if not connected:
            return

        log.info(f"Toast nightly sync for {len(connected)} restaurant(s)")
        for r in connected:
            try:
                result = sync_to_db(r.id)
                if result["ok"]:
                    log.info(f"Toast sync OK for {r.name} — {result.get('rows', '?')} shift rows")
                else:
                    log.warning(f"Toast sync failed for {r.name}: {result.get('error')}")
            except Exception as e:
                log.error(f"Toast sync error for {r.name}: {e}")

    except Exception as e:
        log.error(f"run_toast_sync error: {e}")


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
        soon = (_chi_now() + timedelta(days=7)).strftime("%Y-%m-%d")

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
                    new_expires = (_chi_now() + timedelta(days=60)).strftime("%Y-%m-%d")
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

_last_fetch_date      = None
_last_digest_date     = None
_last_backup_date     = None
_last_toast_sync_date = None
_last_onboard_date    = None
_last_stale_inv_date  = None
_last_inactive_date   = None
_last_monthly_date    = None


def backup_db():
    """
    Dump reviews.db and email it to will@cavnar.ai as an attachment.
    Runs daily at 2am. No external dependencies — uses Resend which is already in the stack.
    """
    import base64, shutil, tempfile
    from models import DB_PATH

    FROM_EMAIL = os.getenv("FROM_EMAIL", "will@cavnar.ai")
    WILL_EMAIL = os.getenv("WILL_EMAIL", "will@cavnar.ai")

    try:
        if not RESEND_API_KEY:
            log.warning("backup_db: RESEND_API_KEY not set — skipping backup")
            return

        import resend as _resend
        _resend.api_key = RESEND_API_KEY

        # Copy DB to a temp file so we don't lock the live DB during read
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copy2(DB_PATH, tmp_path)

        # Read and base64-encode for email attachment
        with open(tmp_path, "rb") as f:
            db_bytes = f.read()
        os.unlink(tmp_path)

        db_b64    = base64.b64encode(db_bytes).decode()
        size_kb   = round(len(db_bytes) / 1024, 1)
        timestamp = _chi_now().strftime("%Y-%m-%d")
        filename  = f"cavnar_ai_backup_{timestamp}.db"

        _resend.Emails.send({
            "from": f"Cavnar AI Backups <{FROM_EMAIL}>",
            "to":   [WILL_EMAIL],
            "subject": f"Daily DB backup — {timestamp} ({size_kb} KB)",
            "html": f"""
<div style="font-family:-apple-system,sans-serif;max-width:480px;color:#1a1714">
  <p style="font-size:14px">Daily backup of <strong>reviews.db</strong> attached.</p>
  <table style="font-size:13px;color:#3a3530;border-collapse:collapse">
    <tr><td style="padding:3px 12px 3px 0;color:#7a736a">Date</td><td>{timestamp}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#7a736a">File</td><td>{filename}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#7a736a">Size</td><td>{size_kb} KB</td></tr>
  </table>
  <p style="font-size:12px;color:#7a736a;margin-top:16px">
    To restore: download the attachment, rename to reviews.db, and replace the file on Railway.
  </p>
</div>""",
            "attachments": [{
                "filename": filename,
                "content":  db_b64,
            }],
        })
        log.info(f"backup_db: sent {filename} ({size_kb} KB) to {WILL_EMAIL}")

    except Exception as e:
        log.error(f"backup_db failed: {e}")




def run_onboarding_sequence():
    """
    Check all active clients and send the right onboarding email based on days since signup.
    Runs daily at 10am. Skips clients who already received each email (UNIQUE constraint).
    Only sends to clients with billing_status in ('trial', 'active').
    """
    from datetime import datetime, timedelta
    from models import get_all_restaurants, get_onboarding_sent, mark_onboarding_sent, log_email
    from emails import send_onboarding_day2, send_onboarding_day7, send_onboarding_day30

    try:
        restaurants = get_all_restaurants()
    except Exception as e:
        log.error(f"run_onboarding_sequence: could not load restaurants: {e}")
        return

    now = _chi_now()

    for r in restaurants:
        # Only send to trial or active clients
        if getattr(r, "billing_status", "trial") not in ("trial", "active"):
            continue
        # Need an email address
        if not r.owner_email:
            continue
        # Need a signup date
        if not r.created_at:
            continue

        try:
            created = datetime.fromisoformat(r.created_at.replace("Z", ""))
        except Exception:
            continue

        days_since = (now - created).days
        already_sent = get_onboarding_sent(r.id)

        # Build module list for context
        modules = []
        if r.module_reviews:  modules.append("Review Intelligence")
        if r.module_labor:    modules.append("Labor Optimizer")
        if r.module_inventory: modules.append("Inventory Control")
        if r.module_marketing: modules.append("Marketing Autopilot")

        # Day 2 — send on day 2 or 3 (small buffer in case scheduler runs slightly late)
        if days_since >= 2 and "day_2" not in already_sent:
            try:
                send_onboarding_day2(
                    to_email=r.owner_email,
                    restaurant_name=r.name,
                    owner_name=r.owner_name,
                    modules=modules,
                )
                mark_onboarding_sent(r.id, "day_2")
                log_email(r.id, "Onboarding Day 2", r.owner_email, f"Getting started — {r.name}")
                log.info(f"Onboarding day 2 sent to {r.owner_email} ({r.name})")
            except Exception as e:
                log.error(f"Onboarding day 2 failed for {r.name}: {e}")

        # Day 7 — send on day 7, 8, or 9
        elif days_since >= 7 and "day_7" not in already_sent:
            try:
                # Pull actual activity stats for personalization
                try:
                    from models import get_conn as _gc
                    _conn = _gc()
                    _reviews_row = _conn.execute(
                        "SELECT COUNT(*) as cnt FROM reviews WHERE restaurant_id=? AND response_status IN ('approved','posted')",
                        (r.id,)
                    ).fetchone()
                    _pending_row = _conn.execute(
                        "SELECT COUNT(*) as cnt FROM reviews WHERE restaurant_id=? AND response_status NOT IN ('approved','posted','skipped')",
                        (r.id,)
                    ).fetchone()
                    _conn.close()
                    approved_count = _reviews_row["cnt"] if _reviews_row else 0
                    pending_count = _pending_row["cnt"] if _pending_row else 0
                except Exception:
                    approved_count = 0
                    pending_count = 0

                send_onboarding_day7(
                    to_email=r.owner_email,
                    restaurant_name=r.name,
                    owner_name=r.owner_name,
                    has_labor=bool(r.module_labor),
                    has_inventory=bool(r.module_inventory),
                    approved_count=approved_count,
                    pending_count=pending_count,
                )
                mark_onboarding_sent(r.id, "day_7")
                log_email(r.id, "Onboarding Day 7", r.owner_email, f"One week in — {r.name}")
                log.info(f"Onboarding day 7 sent to {r.owner_email} ({r.name})")
            except Exception as e:
                log.error(f"Onboarding day 7 failed for {r.name}: {e}")

        # Day 30 — send on day 30+
        elif days_since >= 30 and "day_30" not in already_sent:
            try:
                send_onboarding_day30(
                    to_email=r.owner_email,
                    restaurant_name=r.name,
                    owner_name=r.owner_name,
                    modules=modules,
                    restaurant_id=r.id,
                )
                mark_onboarding_sent(r.id, "day_30")
                log_email(r.id, "Onboarding Day 30", r.owner_email, f"30-day check-in — {r.name}")
                log.info(f"Onboarding day 30 sent to {r.owner_email} ({r.name})")
            except Exception as e:
                log.error(f"Onboarding day 30 failed for {r.name}: {e}")


def check_inactive_clients():
    """
    Alert Will when a client hasn't logged in for 14+ days.
    Runs every Monday at 11am. Only checks active/trial clients.
    """
    from datetime import datetime, timedelta
    from models import get_all_restaurants, get_conn

    RESEND_API_KEY_LOCAL = os.getenv("RESEND_API_KEY", "")
    WILL_EMAIL_LOCAL     = os.getenv("WILL_EMAIL", "will@cavnar.ai")
    FROM_EMAIL_LOCAL     = os.getenv("FROM_EMAIL", "will@cavnar.ai")

    if not RESEND_API_KEY_LOCAL:
        log.warning("check_inactive_clients: no RESEND_API_KEY — skipping")
        return

    try:
        restaurants = get_all_restaurants()
    except Exception as e:
        log.error(f"check_inactive_clients: could not load restaurants: {e}")
        return

    inactive = []
    now = _chi_now()
    cutoff = now - timedelta(days=14)

    for r in restaurants:
        if getattr(r, "billing_status", "trial") not in ("trial", "active"):
            continue
        try:
            conn = get_conn()
            row = conn.execute(
                """SELECT last_login FROM users
                   WHERE restaurant_id=? AND is_admin=0 AND is_active=1
                   ORDER BY last_login DESC LIMIT 1""",
                (r.id,)
            ).fetchone()
            conn.close()
            if not row:
                continue
            last_login = row["last_login"]
            if not last_login:
                # Never logged in — check if they've been a client for 3+ days
                if r.created_at:
                    try:
                        created = datetime.fromisoformat(r.created_at.replace("Z",""))
                        if (now - created).days >= 3:
                            inactive.append({"name": r.name, "email": r.owner_email, "last_login": "Never logged in", "days": (now - created).days})
                    except Exception:
                        pass
            else:
                try:
                    ll = datetime.fromisoformat(last_login.replace("Z",""))
                    if ll < cutoff:
                        days_ago = (now - ll).days
                        inactive.append({"name": r.name, "email": r.owner_email, "last_login": ll.strftime("%b %d"), "days": days_ago})
                except Exception:
                    pass
        except Exception as e:
            log.error(f"check_inactive_clients: error checking {r.name}: {e}")

    if not inactive:
        log.info("check_inactive_clients: no inactive clients this week")
        return

    rows_html = "".join([
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #e0dbd0'><strong>{c['name']}</strong></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e0dbd0'>{c['email']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e0dbd0;color:#c84b2f'>{c['last_login']}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #e0dbd0'>{c['days']}d ago</td></tr>"
        for c in inactive
    ])

    try:
        import resend as _resend
        _resend.api_key = RESEND_API_KEY_LOCAL
        _resend.Emails.send({
            "from": f"Cavnar AI Alerts <{FROM_EMAIL_LOCAL}>",
            "to": [WILL_EMAIL_LOCAL],
            "subject": f"👋 {len(inactive)} inactive client{'s' if len(inactive)>1 else ''} — check in this week",
            "html": f"""<div style="font-family:sans-serif;max-width:580px;margin:0 auto">
                <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:16px">
                    <h3 style="color:#0e0c0a;margin:0">Inactive clients</h3>
                    <p style="font-size:12px;color:#7a736a;margin:4px 0 0">Clients who haven't logged in for 14+ days</p>
                </div>
                <table style="width:100%;border-collapse:collapse;font-size:13px">
                    <thead><tr style="background:#f7f4ef">
                        <th style="padding:8px 12px;text-align:left">Client</th>
                        <th style="padding:8px 12px;text-align:left">Email</th>
                        <th style="padding:8px 12px;text-align:left">Last login</th>
                        <th style="padding:8px 12px;text-align:left">Gap</th>
                    </tr></thead>
                    <tbody>{rows_html}</tbody>
                </table>
                <p style="font-size:13px;color:#3a3530;margin-top:16px;line-height:1.6">
                    Worth a quick personal email or text to each of these — early churn usually shows up as disengagement first.
                </p>
                <hr style="border:none;border-top:1px solid #e0dbd0;margin:16px 0"/>
                <p style="font-size:11px;color:#7a736a">
                    <a href="https://dashboard.cavnar.ai/admin" style="color:#c84b2f">Manage clients →</a>
                </p>
            </div>"""
        })
        log.info(f"Inactive client alert sent — {len(inactive)} client(s)")
    except Exception as e:
        log.error(f"check_inactive_clients email failed: {e}")


def scheduler_loop():
    global _last_fetch_date, _last_digest_date, _last_backup_date
    global _last_toast_sync_date, _last_onboard_date, _last_stale_inv_date
    global _last_inactive_date, _last_monthly_date
    log.info("Scheduler started — review fetch every 4hr (8am/12pm/4pm/8pm CT), digests 9am on client's chosen day")


    while True:
        try:
            now   = _chi_now()
            today = now.date()

            # Monday 6am — run competitor analysis for all clients
            # 2am daily — backup DB to email
            if now.hour == 2 and _last_backup_date != today:
                _last_backup_date = today
                log.info("Running daily DB backup...")
                backup_db()

            if now.hour == 6 and now.weekday() == 0 and _last_fetch_date != today:
                log.info("Running weekly competitor analysis...")
                try:
                    from competitor import run_competitor_analysis
                    from models import get_all_restaurants
                    for r in get_all_restaurants():
                        # Only run for full system clients (all 4 modules)
                        if (r.google_place_id and r.id and
                                r.module_reviews and r.module_labor and
                                r.module_inventory and r.module_marketing):
                            try:
                                run_competitor_analysis(r.id)
                            except Exception as ce:
                                log.error(f"Competitor analysis failed for {r.name}: {ce}")
                except Exception as e:
                    log.error(f"Competitor analysis scheduler error: {e}")

            if now.hour == 3 and _last_toast_sync_date != today:
                _last_toast_sync_date = today
                log.info("Running nightly Toast POS sync...")
                run_toast_sync()

            if now.hour == 7 and _last_fetch_date != today:
                log.info("Refreshing expiring IG/FB tokens...")
                refresh_expiring_tokens()

            # Fetch every 4 hours: 8am, 12pm, 4pm, 8pm Chicago time
            if now.hour in (8, 12, 16, 20) and _last_fetch_date != f"{today}-{now.hour}":
                _last_fetch_date = f"{today}-{now.hour}"
                log.info(f"Running review fetch (every 4hr) at {now.hour}:00 CT...")
                run_daily_fetch()

            if now.hour == 9 and _last_digest_date != today:
                _last_digest_date = today
                log.info("Running weekly digest check...")
                run_weekly_digests()

            if now.hour == 10 and now.weekday() == 0 and _last_stale_inv_date != today:
                _last_stale_inv_date = today
                # Monday 10am — check for stale inventory data
                log.info("Running stale inventory check...")
                check_stale_inventory()

            # 10am daily — no-response + trend/threshold/labor alerts
            if now.hour == 10 and _last_fetch_date != f"noresponse-{today}":
                try:
                    from notify import check_no_response_alerts, check_daily_alerts
                    check_no_response_alerts()
                    check_daily_alerts()
                except Exception as _nre:
                    log.error(f"Daily alert check failed: {_nre}")

            # 1st of the month at 9am — send monthly summary to all active clients
            if now.day == 1 and now.hour == 9 and _last_monthly_date != today:
                _last_monthly_date = today
                log.info("Running monthly summary emails...")
                try:
                    from emails import send_monthly_summary_email
                    from models import get_all_restaurants
                    for r in get_all_restaurants():
                        if not r.owner_email or r.billing_status in ('internal', 'churned'):
                            continue
                        try:
                            send_monthly_summary_email(
                                to_email=r.owner_email,
                                restaurant_name=r.name,
                                owner_name=r.owner_name,
                                restaurant_id=r.id,
                                has_reviews=bool(r.module_reviews),
                                has_labor=bool(r.module_labor),
                                has_inventory=bool(r.module_inventory),
                                has_marketing=bool(r.module_marketing),
                            )
                            log.info(f"Monthly summary sent to {r.name}")
                        except Exception as me:
                            log.error(f"Monthly summary failed for {r.name}: {me}")
                except Exception as e:
                    log.error(f"Monthly summary scheduler error: {e}")

            if now.hour == 10 and _last_onboard_date != today:
                _last_onboard_date = today
                # 10am daily — onboarding email sequence
                log.info("Running onboarding sequence check...")
                run_onboarding_sequence()

            if now.hour == 11 and now.weekday() == 0 and _last_inactive_date != today:
                _last_inactive_date = today
                # Monday 11am — inactive client check
                log.info("Running inactive client check...")
                check_inactive_clients()

            try:
                record_scheduler_heartbeat()
                run_health_checks()
            except Exception:
                pass

        except Exception as e:
            log.error(f"Scheduler loop error: {e}")

        time.sleep(3600)


def start_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    log.info("Scheduler thread started")
    return t
