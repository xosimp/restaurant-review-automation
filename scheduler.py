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
import config
import os, threading, time, logging, html as _html
from concurrent.futures import ThreadPoolExecutor, as_completed
from status_manager import record_scheduler_heartbeat, run_health_checks
import emails as _emails
import ops as _ops
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo as _ZI_sch
from time_utils import parse_stored_dt
def _chi_now():
    return datetime.now(_ZI_sch('America/Chicago')).replace(tzinfo=None)
from dotenv import load_dotenv
import pathlib


from emails import html_document as _html_doc  # one definition; emails reads its env lazily

load_dotenv(pathlib.Path(__file__).parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [scheduler] %(message)s")
log = logging.getLogger("scheduler")

from emails import _resend_key, _from_email  # one definition each
def send_urgent_alert(restaurant_name, owner_email, urgent_reviews):
    """Email the owner immediately when a high-urgency review comes in."""
    if not _resend_key() or not owner_email:
        log.warning(f"Cannot send urgent alert for {restaurant_name} — no key/email")
        return
    try:
        import resend as _resend
        _resend.api_key = _resend_key()

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
            "from": _emails.sender("client"),
            "to": [owner_email],
            "subject": f"\u26a0 Urgent review alert \u2014 {restaurant_name}",
            "html": _html_doc(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:white;border-radius:12px;padding:28px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:20px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin:0 0 6px">
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
    <a href="mailto:{_from_email()}" style="color:#c84b2f;text-decoration:none">{_from_email()}</a>
  </p>
</div>
</div>"""),
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


def get_owner_emails(restaurant_id):
    """Every active owner login's email for this restaurant — co-owners
    included — oldest first. Falls back to the restaurant's owner_email when
    no owner login exists. A restaurant run by two partners gets the owner
    mail to both; an unordered LIMIT 1 used to send it to one of them (and
    could pick a manager)."""
    from models import get_conn, get_restaurant
    conn = get_conn()
    rows = conn.execute(
        "SELECT email FROM users WHERE restaurant_id=? AND is_admin=0 "
        "AND COALESCE(is_active,1)=1 AND COALESCE(NULLIF(role,''),'client') IN ('client','owner') "
        "AND email IS NOT NULL AND email != '' ORDER BY id",
        (restaurant_id,)
    ).fetchall()
    conn.close()
    emails = []
    for r in rows:
        e = r["email"].strip().lower()
        if e not in emails:
            emails.append(e)
    if emails:
        return emails
    r = get_restaurant(restaurant_id)
    return [r.owner_email] if r and r.owner_email else []


def get_owner_email(restaurant_id):
    """The first owner's email — for callers that address one person."""
    emails = get_owner_emails(restaurant_id)
    return emails[0] if emails else None


# ── review fetch bounds ─────────────────────────────────────────────────────
#
# The pass is network-bound: a Google fetch, then a Claude analysis and a
# draft per NEW review. Parallelism here buys wall-clock without buying CPU.
# Kept modest on purpose — every worker writes to one SQLite file, and the
# model API has its own rate limits that a wide fan-out turns into 429s.
FETCH_WORKERS = int(os.getenv("FETCH_WORKERS", "6"))

# Stop before the next scheduled slot rather than running into it. The slots
# are four hours apart; this leaves an hour of headroom.
FETCH_MAX_SECONDS = int(os.getenv("FETCH_MAX_SECONDS", str(3 * 3600)))

# Where the last bounded pass stopped, so the next one starts there instead
# of at the top of the list again. Without this, a pass that can only reach
# 400 of 900 restaurants reaches the SAME 400 every time and the other 500
# are never fetched at all — the failure mode the bound would otherwise
# introduce while fixing the runaway one.
_FETCH_CURSOR_KEY = "review_fetch_cursor"

# The Food Cost restaurant sweeps (resumable_sweep). One worker by default:
# each restaurant's pass is a run of SQLite writes and the file has one
# writer; the bound and the cursor are what keep a pass from running away.
SWEEP_WORKERS = int(os.getenv("SWEEP_WORKERS", "1"))
SWEEP_MAX_SECONDS = int(os.getenv("SWEEP_MAX_SECONDS", str(45 * 60)))
DEPLETION_CURSOR_KEY = "inventory_depletion_cursor"
SNAPSHOT_CURSOR_KEY = "food_cost_snapshot_cursor"


def _fetch_order(ids, key=_FETCH_CURSOR_KEY):
    """The live restaurant ids, rotated so the ones skipped last time lead.
    `key` names the sweep whose cursor this reads (job_cursors.key)."""
    if not ids:
        return []
    try:
        from models import get_conn
        conn = get_conn()
        # job_cursors is created by models.init_db (one owner, one definition).
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?",
                           (key,)).fetchone()
        conn.close()
        last = int(row["value"]) if row and str(row["value"]).isdigit() else None
    except Exception as e:
        log.warning(f"_fetch_order: cursor unreadable ({e}) — starting at the top")
        last = None
    ordered = sorted(ids)
    if last is None or last not in ordered:
        return ordered
    cut = ordered.index(last) + 1
    return ordered[cut:] + ordered[:cut]


def _remember_fetch_cursor(order, processed, key=_FETCH_CURSOR_KEY):
    """Record the last restaurant this pass actually covered."""
    if not order or processed <= 0:
        return
    last = order[min(processed, len(order)) - 1]
    try:
        from models import get_conn
        conn = get_conn()
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                     "updated_at=excluded.updated_at", (key, str(last)))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"_remember_fetch_cursor failed: {e}")


def resumable_sweep(key, ids, fn, max_seconds, workers=1, job=None):
    """Run `fn(rid)` over restaurant ids the run_daily_fetch way: rotated to
    start after job_cursors[key], a worker pool, a wall-clock bound — and the
    cursor saved as each restaurant finishes, so a redeploy mid-pass resumes
    where it died instead of at the first restaurant again. Returns
    (completed, hit_bound).

    The cursor is the end of the longest finished PREFIX of this pass's
    order, so with several workers a restaurant still in flight is never
    skipped. `fn` handles its own per-restaurant failures; anything else it
    raises is captured and counts as covered (a restaurant that always
    raises must not pin the cursor). A BaseException — the process going
    away — is not caught and does not advance the cursor.
    """
    order = _fetch_order(list(ids), key=key)
    if not order:
        return 0, False
    lock = threading.Lock()
    finished, state = set(), {"prefix": 0}

    def _covered(rid):
        with lock:
            finished.add(rid)
            p = state["prefix"]
            while p < len(order) and order[p] in finished:
                p += 1
            advanced = p != state["prefix"]
            state["prefix"] = p
        if advanced:
            _remember_fetch_cursor(order, p, key=key)

    def _run(rid):
        fn(rid)
        _covered(rid)

    def _failed(rid, e):
        log.error(f"{job or key}: restaurant {rid} failed: {e}")
        _ops.capture(e, job=job or key, context=f"restaurant_id={rid}")
        _covered(rid)

    return bounded_map(order, _run, workers, max_seconds, on_error=_failed)


def bounded_map(items, fn, workers, max_seconds, on_error=None):
    """Run `fn` over `items` with a worker pool and a wall-clock budget.

    Returns (completed_count, hit_bound).

    A BOUNDED IN-FLIGHT WINDOW, not submit-everything-then-wait. Submitting
    the whole list up front and checking the clock in the submit loop does
    not bound anything: submission is instant, so the check never fires and
    every item is queued regardless — the pass then runs as long as it runs,
    which is the behaviour the bound exists to stop. That was the first
    implementation here, and it was caught by testing the bound rather than
    assuming it.

    Work is handed out only as a worker frees up, so the clock is consulted
    between real units of work and the run stops within roughly one item of
    the budget. Items already running are always allowed to finish — killing
    a restaurant's fetch halfway through is how you get partial state.
    """
    pending = list(items)
    if not pending:
        return 0, False
    done, hit_bound = 0, False
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        in_flight = {}

        def _fill():
            while pending and len(in_flight) < max(1, workers):
                item = pending.pop(0)
                in_flight[pool.submit(fn, item)] = item

        _fill()
        while in_flight:
            for fut in as_completed(list(in_flight)):
                item = in_flight.pop(fut)
                try:
                    fut.result()
                    done += 1
                except Exception as e:
                    if on_error:
                        on_error(item, e)
                break      # re-evaluate the budget after every completion
            if time.time() - started > max_seconds:
                if pending:
                    hit_bound = True
                pending = []
            _fill()
    return done, hit_bound


def _record_places_gap(rid, name, total, stored_new):
    """Places returns at most five reviews a fetch. When Google's own count
    of the listing grew by more than this fetch stored, the difference was
    never returned and is lost unless recorded (MOD-REV-11): an activity-log
    entry the account can show, and a failure-digest line for the operator.
    The last total seen lives in job_cursors."""
    from models import get_conn
    key = f"places_total:{rid}"
    prev = None
    try:
        conn = get_conn()
        try:
            row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
            prev = int(row["value"]) if row and str(row["value"]).isdigit() else None
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                         "updated_at=excluded.updated_at", (key, str(int(total))))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"places total cursor for {rid}: {e}")
        return
    if prev is None:
        return
    missed = (int(total) - prev) - int(stored_new or 0)
    if missed <= 0:
        return
    detail = (f"Google's count for {name} rose by {int(total) - prev} but this fetch returned "
              f"{int(stored_new or 0)} new — {missed} reviews missed (a gap: Places returns at most "
              f"five). Connecting Google Business Profile reads them all.")
    try:
        from models import log_event
        log_event(rid, "review_fetch_gap", {"missed": missed, "google_total": int(total),
                                            "previous_total": prev, "detail": detail})
    except Exception:
        pass
    _ops.capture(RuntimeError(detail), job="review_fetch_gap", context=f"restaurant_id={rid}")


def run_daily_fetch():
    """Fetch reviews for all live clients, analyse, draft, alert on urgent."""
    try:
        from models import get_conn, get_restaurant, save_reviews
        from models import get_pending_analysis, get_pending_drafts, update_last_fetched
        from fetcher import fetch_google
        from analyser import analyse_review
        from drafter import draft_response

        conn = get_conn()
        # In service only (MOD-REV-2): a cancelled restaurant is not fetched,
        # analysed, drafted or alerted — its reviews are no longer ours to
        # read and its Google listing no longer ours to reply on.
        from models import in_service_sql
        live = conn.execute(
            "SELECT id FROM restaurants WHERE (reviews_live=1 OR gmb_refresh_token IS NOT NULL) "
            "AND deletion_requested_at IS NULL "    # the owner asked for it gone
            "AND " + in_service_sql()
        ).fetchall()
        conn.close()

        if not live:
            return

        log.info(f"Daily fetch for {len(live)} live restaurant(s)")

        def _process_restaurant(row):
            rid = row["id"]
            restaurant = get_restaurant(rid)
            if not restaurant:
                return

            # Fetch.
            #
            # `fetched_ok` is the whole point of this block. last_fetched_at
            # used to be stamped unconditionally, one line below here, which
            # meant a restaurant whose Google connection had died still looked
            # freshly synced — and status_manager's 25-hour staleness check
            # reads exactly that column, so the monitor built to catch this
            # could never fire. Reviews stopped arriving permanently and
            # everything reported operational.
            reviews = []
            fetched_ok = False
            gbp_listing = None      # (location_id, the GbpReviews it returned)
            places_total = None     # Google's user_ratings_total, Places path
            gmb_failed_reason = None

            if restaurant.gmb_refresh_token:
                try:
                    from gmb import (get_valid_token, fetch_reviews_via_gmb, find_gmb_location,
                                     refresh_failed_transiently, GoogleTokenUnavailable)
                    token = get_valid_token(rid)
                    _blip = None if token else refresh_failed_transiently(rid)
                    if _blip:
                        # A timeout or a Google 5xx on the refresh: into the
                        # except below (Places fallback, failure digest) — not
                        # a revoked connection, so the owner is not told to
                        # reconnect (AI-22).
                        raise GoogleTokenUnavailable(f"Google token refresh failed: {_blip}")
                    if not token:
                        # Returns None rather than raising — a revoked or
                        # expired refresh token used to land here and be
                        # indistinguishable from "nothing new today".
                        gmb_failed_reason = "Google refresh token is no longer valid (revoked, or expired)"
                        # Tell the OWNER, once a week. Until now this reached
                        # Will's failure digest and status_manager, while the
                        # owner's dashboard kept showing last week's reviews
                        # with a freshness stamp nobody reads.
                        try:
                            if _ops.claim_period(f"connection_lost:{rid}",
                                                 _chi_now().strftime("%G-W%V")):
                                import notify as _nf
                                _nf.raise_alert(
                                    rid, "connection_lost",
                                    f"Cavnar AI: your Google connection needs reconnecting — "
                                    f"reviews stopped syncing for {restaurant.name}.",
                                    f"Google needs reconnecting — {restaurant.name}",
                                    lines=["Google has stopped honouring the connection this account "
                                           "uses to read your reviews. Nothing is lost — new reviews "
                                           "are being read the slower way — but replies cannot post "
                                           "until it is reconnected.",
                                           "Open Account → Connections and choose Google."])
                        except Exception as _cl:
                            log.warning(f"connection_lost alert failed for {rid}: {_cl}")
                    else:
                        loc_id = restaurant.gmb_location_id
                        acct_id = restaurant.gmb_account_id
                        if not loc_id and restaurant.google_place_id:
                            # Matched on this restaurant's own Place ID. The
                            # old backfill took accounts[0]/locations[0],
                            # which on a multi-location account silently
                            # bound this restaurant to a sibling's listing.
                            _m = find_gmb_location(token, restaurant.google_place_id)
                            if _m.get("ok"):
                                loc_id = _m["location"]
                                acct_id = _m["account"]
                                try:
                                    from models import update_restaurant as _ur
                                    _ur(rid, {"gmb_account_id": _m["account"],
                                              "gmb_location_id": _m["location"]})
                                except Exception:
                                    pass
                            else:
                                gmb_failed_reason = _m.get("error") or "location not matched"
                        if not loc_id:
                            gmb_failed_reason = gmb_failed_reason or "Google Business location could not be resolved"
                        else:
                            _gbp = fetch_reviews_via_gmb(token, loc_id, rid)
                            reviews += _gbp
                            gbp_listing = (loc_id, _gbp)
                            fetched_ok = True
                            try:
                                from gmb import fetch_gmb_logo_url
                                fetch_gmb_logo_url(rid, token, acct_id, loc_id)
                            except Exception:
                                pass
                except Exception as e:
                    gmb_failed_reason = str(e)[:200]
                    log.error(f"GMB fetch [{restaurant.name}]: {e}")
                    _ops.capture(e, job="review_fetch", context=f"GMB {restaurant.name}")

                if gmb_failed_reason:
                    # Fall back to Places rather than fetching nothing. This
                    # used to be an `elif` on gmb_refresh_token, so a
                    # connected-but-broken restaurant never reached it and
                    # simply stopped receiving reviews.
                    _ops.capture(RuntimeError(f"GMB unusable: {gmb_failed_reason}"),
                                 job="review_fetch",
                                 context=f"restaurant_id={rid} {restaurant.name} — falling back to Places")
                    if restaurant.google_place_id:
                        try:
                            _pl = fetch_google(restaurant.google_place_id, rid)
                            reviews += _pl
                            places_total = getattr(_pl, "total", None)
                            fetched_ok = True
                            log.warning(f"GMB unusable for {restaurant.name}; served from Places instead")
                        except Exception as e:
                            log.error(f"Places fallback [{restaurant.name}]: {e}")
                            _ops.capture(e, job="review_fetch", context=f"Places fallback {restaurant.name}")

            elif restaurant.google_place_id:
                try:
                    _pl = fetch_google(restaurant.google_place_id, rid)
                    reviews += _pl
                    places_total = getattr(_pl, "total", None)
                    fetched_ok = True
                except Exception as e:
                    log.error(f"Google fetch [{restaurant.name}]: {e}")
                    _ops.capture(e, job="review_fetch", context=f"Google {restaurant.name}")

            # Only a fetch that actually reached a provider counts as a sync.
            # A failed one leaves last_fetched_at where it was, so the 25-hour
            # staleness check sees it and the status page goes degraded.
            if fetched_ok:
                update_last_fetched(rid)
            else:
                log.warning(f"Review fetch did not complete for {restaurant.name} — last_fetched_at left stale on purpose")

            new_count, new_reviews = 0, []
            downgraded = []
            if reviews:
                new_count, new_reviews = save_reviews(reviews, downgrades=downgraded)
                if new_count:
                    log.info(f"{new_count} new reviews for {restaurant.name}")
                if downgraded:
                    log.info(f"{len(downgraded)} review(s) edited down for {restaurant.name}")

            # A complete Business Profile listing is the whole truth about
            # this location: a stored review it no longer contains is one
            # Google removed (a fake review taken down, a guest who deleted
            # theirs), and it stops counting (MOD-REV-14).
            if gbp_listing and getattr(gbp_listing[1], "complete", False):
                try:
                    from gmb import retire_unlisted_reviews
                    retire_unlisted_reviews(rid, gbp_listing[0],
                                            [r.review_name for r in gbp_listing[1] if r.review_name])
                except Exception as _re:
                    _ops.capture(_re, job="review_fetch", context=f"retire unlisted rid={rid}")
            if places_total is not None and not gbp_listing:
                _record_places_gap(rid, restaurant.name, places_total, new_count)

            # Analyse BEFORE alerting. Alerts used to fire on the raw batch,
            # so sentiment and urgency were both still NULL when the health
            # alert ran — which is the only reason that alert reads raw text
            # against a keyword list and sends a 🚨 HEALTH ALERT for a
            # five-star review saying "no roach problem here". It is also
            # why every review.received webhook reported sentiment: null,
            # and why the negative-spike count saw none of the batch that
            # triggered it.
            #
            # This sweep (and the drafting one below it) used to be nested
            # under `if new_count:`, so a review stuck unanalysed or
            # undrafted — a failed Haiku call, a rate limit, one inserted
            # outside the normal fetch path (e.g. a seed/import script) —
            # only got retried on a cycle where this SAME restaurant also
            # happened to receive a genuinely new review that day. No new
            # review meant the backlog sat there forever, fixable only by
            # a manual click. Runs every cycle now, independent of whether
            # this fetch turned up anything new.
            for r in get_pending_analysis(rid, limit=max(new_count, 50)):
                try:
                    analyse_review(r.id, r.rating, r.text, restaurant_id=rid)
                except Exception as e:
                    from ai_utils import is_platform_stop
                    if is_platform_stop(e):
                        # Budget or provider breaker: not this review's fault
                        # and the same for the rest — no attempt spent (AI-4).
                        log.warning(f"Analysis paused for {restaurant.name}: {e}")
                        break
                    log.error(f"Analyse error: {e}")
                    _ops.capture(e, job="review_analyse", context=restaurant.name)
                    try:
                        from models import record_ai_attempt
                        record_ai_attempt(r.id, "analysis")
                    except Exception:
                        pass

            if new_reviews or downgraded:
                # Re-read the batch so alerts and webhooks see the analysis.
                try:
                    from models import get_reviews_by_ids as _grbi
                    if new_reviews:
                        new_reviews = _grbi(rid, [r.id for r in new_reviews if getattr(r, "id", None)]) or new_reviews
                except Exception:
                    pass

                # Fire SMS/email alerts for newly saved reviews, and for any
                # review whose author lowered their own rating.
                try:
                    from notify import fire_review_alerts
                    fire_review_alerts(rid, restaurant.name, new_reviews,
                                       edited_reviews=downgraded)
                except Exception as _ae:
                    log.error(f"Alert fire error [{restaurant.name}]: {_ae}")

                # Fire outbound webhooks for each new review
                try:
                    from webhooks import fire_webhook as _fw
                    from notify import is_recent_review as _recent
                    # A history import is not news: no review.received per
                    # years-old row on a first Google connect (MOD-REV-6).
                    for _nr in [r for r in new_reviews if _recent(r)]:
                        _payload = {
                            "platform": _nr.platform,
                            "rating":   _nr.rating,
                            "author":   _nr.author,
                            "body":     (_nr.text or "")[:500],
                            "sentiment": getattr(_nr, "sentiment", None),
                            "urgency":   getattr(_nr, "urgency", None),
                        }
                        _fw(rid, "review.received", _payload)
                        if (_nr.rating or 5) <= 2:
                            _fw(rid, "review.negative", _payload)
                        if (_nr.rating or 0) >= 4:
                            _fw(rid, "review.positive", _payload)
                except Exception:
                    pass

            # Draft — include approved examples for style learning. Same
            # unconditional-sweep reasoning as the analysis loop above.
            from models import get_approved_examples
            approved_examples = get_approved_examples(rid, limit=4)
            for r in get_pending_drafts(rid, limit=50):
                try:
                    draft_response(r.id, r.rating, r.text, r.sentiment,
                                  restaurant.name, restaurant.voice_notes or "",
                                  restaurant_id=rid,
                                  approved_examples=approved_examples,
                                  sign_off=restaurant.sign_off_name or restaurant.name,
                                  never_say=restaurant.never_say or "",
                                  language=getattr(restaurant, "response_language", None) or None,
                                  # Without this the whole urgent-issue
                                  # escalation in drafter.py (80-100 words,
                                  # no minimising, invite direct contact)
                                  # was dead in the ONE path that drafts
                                  # essentially every production reply — it
                                  # defaulted to "normal", so a food-safety
                                  # report got the standard 1-star template.
                                  urgency=r.urgency or "normal",)
                except Exception as e:
                    from ai_utils import is_platform_stop
                    if is_platform_stop(e):
                        log.warning(f"Drafting paused for {restaurant.name}: {e}")
                        break
                    log.error(f"Draft error: {e}")
                    _ops.capture(e, job="review_draft", context=restaurant.name)
                    try:
                        from models import record_ai_attempt
                        record_ai_attempt(r.id, "draft")
                    except Exception:
                        pass

            # Auto-approve rule (Account -> Profile -> Auto-approve): only
            # ever drafted 5-star responses, only under the daily cap, and
            # nothing at all while the kill switch is on.
            try:
                auto_approve_five_stars(rid, restaurant)
            except Exception as e:
                log.error(f"Auto-approve error: {e}")

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

        # One restaurant's failure is one restaurant's failure. This loop
        # used to sit bare inside the outer try below, so an unhandled error
        # anywhere in the body above — a locked database on save_reviews, a
        # get_restaurant that raised — ended the cycle for every restaurant
        # after it, silently, behind a single log line.
        #
        # It was also strictly SERIAL and unbounded in time. Each restaurant
        # is one Google fetch plus a Claude analysis and a draft per new
        # review — overwhelmingly network wait — so at a few hundred
        # restaurants a pass ran for hours, the 8am/12pm/4pm/8pm slots
        # collapsed into one continuous run, and the restaurants at the end
        # of the list were simply never reached. Nothing reported that,
        # because nothing failed: an unreached restaurant and a restaurant
        # with no new reviews looked identical.
        #
        # Two bounds now, and a cursor so neither one starves anybody:
        #   FETCH_MAX_SECONDS  — stop cleanly before the next slot.
        #   FETCH_WORKERS      — I/O-bound fan-out, small enough to stay well
        #                        under SQLite's writer contention and the
        #                        model API's rate limits.
        # _process_restaurant opens and closes its own connection per call
        # and shares no SQLite handle across calls, which is what makes it
        # safe on a worker thread — sqlite3 connections are not thread-safe
        # and this codebase's prevailing get_conn()/close() shape is exactly
        # what keeps that true here.
        order = _fetch_order([r["id"] for r in live])
        by_id = {r["id"]: r for r in live}

        def _failed(row, e):
            log.error(f"Review cycle failed for restaurant {row['id']}: {e}")
            _ops.capture(e, job="review_fetch", context=f"restaurant_id={row['id']}")

        done, ran_out = bounded_map([by_id[rid] for rid in order], _process_restaurant,
                                    FETCH_WORKERS, FETCH_MAX_SECONDS, on_error=_failed)
        # `done` counts SUCCESSES, so a pass with failures leaves the cursor
        # short of what it attempted and those restaurants lead the next
        # pass. That is deliberate — a failed fetch should be retried, and
        # re-fetching is free (save_reviews only appends on a successful
        # insert against UNIQUE(restaurant_id, platform, external_id)). It
        # cannot starve the tail: the cursor still advances by the number
        # that succeeded, which is the whole list when nothing is wrong.
        _remember_fetch_cursor(order, done)
        if ran_out:
            # Not a failure — a bound working as intended — but the operator
            # needs to know the pass did not cover everyone, because the
            # symptom for the restaurants it missed is silence.
            log.warning(f"run_daily_fetch: stopped after {done}/{len(live)} "
                        f"restaurants at the {FETCH_MAX_SECONDS}s bound")
            _ops.capture(
                RuntimeError(f"Review fetch covered {done} of {len(live)} restaurants "
                             f"before the {FETCH_MAX_SECONDS}s bound. The rest start "
                             f"the next pass — see _fetch_order."),
                job="review_fetch", context="time_bound")
        return {"restaurants": len(live), "processed": done, "hit_time_bound": ran_out}

    except Exception as e:
        log.error(f"Daily fetch error: {e}")



def run_weekly_digests():
    """Send the weekly digest to every owner scheduled for today.

    Deduplicated by ADDRESS across the whole pass, not per restaurant. A
    multi-location owner has one login per location sharing one owner_email,
    each location claims its own weekly_digest period, and each sent its own
    copy — so running three restaurants meant three identical-looking digests
    landing in one inbox on one morning. The per-restaurant dedup that was
    here proves the case was considered; the cross-location one was missed.

    Restaurants are deduplicated too: get_restaurants_for_digest JOINs users
    without grouping, so a restaurant with three logins comes back three
    times. The claim_period below hid that (rows 2 and 3 lose the claim), at
    the cost of a wasted get_restaurant per row.
    """
    try:
        from models import get_restaurants_for_digest, get_restaurant
        from reporter import build_report_from_db, render_html

        today = _chi_now().strftime("%A").lower()
        scheduled = get_restaurants_for_digest(today)
        if not scheduled:
            return

        seen_rids, unique = set(), []
        for row in scheduled:
            if row["id"] in seen_rids:
                continue
            seen_rids.add(row["id"])
            unique.append(row)

        log.info(f"Weekly digests for {len(unique)} restaurant(s) on {today.title()}")
        from reporter import render_group_html

        # Pass 1: build every due report and bucket it by owner address.
        # Pass 2: one email per address — a single location gets the digest
        # it always got; several get every location in one shell
        # (reporter.render_group_html). The dedup-by-address that lived here
        # solved "three identical-looking digests" by dropping two
        # restaurants' weeks on the floor; the monthly, meanwhile, sent three.
        by_email = {}
        for row in unique:
            rid = row["id"]
            restaurant = get_restaurant(rid)
            if not restaurant:
                continue
            # 9am in the restaurant's own timezone, once per day.
            if not local_due(restaurant, 9, claim_key="weekly_digest"):
                continue
            owner_emails = get_owner_emails(rid)
            if not owner_emails:
                log.warning(f"No email for {restaurant.name}, skipping")
                continue
            try:
                report = build_report_from_db(rid, restaurant.name, days=7)
                has_other_modules = (restaurant.module_labor or
                                     restaurant.module_inventory or
                                     restaurant.module_marketing)
                if report.total_reviews == 0 and not has_other_modules:
                    log.info(f"No reviews this week for {restaurant.name} — skipping digest")
                    continue
                for owner_email in owner_emails:
                    key = (owner_email or "").strip().lower()
                    if not key:
                        continue
                    by_email.setdefault(key, {"to": owner_email, "items": []})
                    by_email[key]["items"].append((restaurant, report))
            except Exception as e:
                log.error(f"Digest build failed for {restaurant.name}: {e}")
                _ops.capture(e, job="weekly_digest", context=f"restaurant_id={rid}")

        for key, bucket in by_email.items():
            items = bucket["items"]
            first_rest, first_rep = items[0]
            try:
                owner_name = _emails.greeting_name(first_rest)
                if len(items) == 1:
                    html = render_html(first_rep, first_rest.name, owner_name=owner_name,
                                       restaurant_id=first_rest.id, owner_view=True)
                    subject = f"Your week at {first_rest.name}"
                    preheader = _emails.digest_preheader(first_rep, first_rest)
                else:
                    html = render_group_html(items, owner_name=owner_name,
                                             group_name=getattr(first_rest, "location_group", None))
                    subject = f"Your week across {len(items)} locations"
                    preheader = "Every location, one email — strongest and weakest first."
                result = _emails.deliver(email_type="digest", restaurant_id=first_rest.id, payload={
                    "from": _emails.sender("client"),
                    "to": [bucket["to"]],
                    "subject": subject,
                    "preheader": preheader,
                    "html": _html_doc(html),
                })
                if getattr(result, "ok", False):
                    log.info(f"Digest sent to {bucket['to']} covering {len(items)} location(s)")
                else:
                    log.error(f"Digest send to {bucket['to']} failed: {result.error}")
                    _ops.capture(RuntimeError(result.error or "digest send failed"),
                                 job="weekly_digest", context=f"restaurant_id={first_rest.id}")
                for rest, _rep in items:
                    try:
                        from webhooks import fire_webhook as _fw_rep
                        _fw_rep(rest.id, "report.weekly", {"restaurant": rest.name, "email": bucket["to"]})
                    except Exception: pass
            except Exception as e:
                log.error(f"Digest failed for {bucket['to']}: {e}")
                _ops.capture(e, job="weekly_digest", context=f"restaurant_id={first_rest.id}")

    except Exception as e:
        log.error(f"Weekly digest error: {e}")
        _ops.capture(e, job="weekly_digest", context="outer")


def check_stale_inventory():
    """Alert Will when a client's inventory data is more than 7 days old."""
    if not _resend_key():
        return
    try:
        from models import get_all_restaurants
        from datetime import datetime, timedelta
        import resend as _resend

        restaurants = get_all_restaurants()
        stale = []
        # Both stamps below are SQLite datetime('now') — UTC. They were
        # compared with _chi_now(), Chicago time, which put every age off by
        # five or six hours; and one unparseable stamp raised out of the
        # loop, so a single bad row hid every other stale restaurant
        # (MOD-FC-26). Parsed as UTC against UTC now, one restaurant at a time.
        now_utc = datetime.utcnow()

        def _age_days(value):
            from time_utils import parse_stored_dt
            dt = parse_stored_dt(value, tz="UTC")
            return None if dt is None else (now_utc - dt).days

        for r in restaurants:
            if not r.module_inventory or r.billing_status not in ("trial", "active"):
                continue
            try:
                conn = __import__('models').get_conn()
                try:
                    # Once a restaurant is migrated to the ingredients ledger (see
                    # inventory_ledger.import_csv_to_ingredients), client_data.updated_at
                    # never changes again for it — checking only that would report
                    # a live, nightly-synced restaurant as "never uploaded" forever.
                    # Use the freshest ingredients.updated_at instead when it has any.
                    ledger_row = conn.execute(
                        "SELECT MAX(updated_at) AS updated_at FROM ingredients WHERE restaurant_id=? AND is_active=1",
                        (r.id,)
                    ).fetchone()
                    row = None
                    if not (ledger_row and ledger_row["updated_at"]):
                        # Legacy CSV path — for restaurants not yet migrated.
                        row = conn.execute(
                            "SELECT updated_at, inventory_source FROM client_data WHERE restaurant_id=? LIMIT 1",
                            (r.id,)
                        ).fetchone()
                finally:
                    conn.close()

                if ledger_row and ledger_row["updated_at"]:
                    days_old = _age_days(ledger_row["updated_at"])
                    if days_old is None:
                        stale.append((r.name, "ledger timestamp unreadable"))
                        continue
                    # A restaurant whose POS has dropped off has numbers that
                    # freeze rather than silently drifting wrong — surface that
                    # explicitly instead of applying the same day-count threshold
                    # as a live sync. Asked of pos.py so this reads correctly for
                    # every provider, not just Toast.
                    import pos as _pos
                    _pname, _pmod = _pos.connected_provider(r.id)
                    if not _pmod and days_old >= 3:
                        stale.append((r.name, f"POS disconnected, ledger frozen {days_old}d"))
                    elif days_old >= 3:
                        stale.append((r.name, f"ledger not synced in {days_old}d"))
                    continue

                if not row or not row["updated_at"]:
                    stale.append((r.name, "never uploaded"))
                    continue

                days_old = _age_days(row["updated_at"])
                if days_old is None:
                    stale.append((r.name, "upload timestamp unreadable"))
                    continue
                freq = getattr(r, 'inventory_frequency', 'weekly')
                threshold = 7 if freq == 'weekly' else (14 if freq == 'biweekly' else 30)
                if days_old >= threshold:
                    stale.append((r.name, f"{days_old} days old"))
            except Exception as e:
                log.error(f"Stale inventory check failed for {r.name}: {e}")
                _ops.capture(e, job="stale_inventory", context=f"restaurant_id={r.id}")

        if not stale:
            return

        stale_html = "".join([
            f'<tr><td style="padding:6px 12px;border-bottom:1px solid #f0ece6"><strong>{name}</strong></td>'            f'<td style="padding:6px 12px;border-bottom:1px solid #f0ece6;color:#c84b2f">{status}</td></tr>'
            for name, status in stale
        ])

        _resend.api_key = _resend_key()
        _resend.Emails.send({
            "from": _emails.sender("client"),
            "to": [config.will_email()],
            "subject": f"⚠ Stale inventory data — {len(stale)} client(s) need updating",
            "html": _html_doc(f"""
<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;color:#1a1714;background:white;border-radius:12px;padding:28px 24px;box-sizing:border-box">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:20px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin:0 0 6px">
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
</div>
</div>"""),
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
    Nightly POS sync (3am CT): pull fresh shift data for every connected
    restaurant into client_data.shifts_csv so the Labor module stays current.
    Dispatches through the pos.py registry — this used to be Toast-only,
    which silently skipped Square and Clover clients every night.
    """
    try:
        import pos
        results = pos.sync_all()
        if results:
            ok = sum(1 for r in results if r.get("ok"))
            log.info(f"POS nightly sync: {ok}/{len(results)} restaurants OK")
    except Exception as e:
        log.error(f"run_toast_sync error: {e}")


def run_daily_depletion_sync():
    """
    Nightly ingredient depletion sync (5am CT, right after the 3am labor
    sync): for every restaurant on a POS that reports item-level sales and
    that has at least one recipe configured, pull yesterday's real business
    date(s) and compute ingredient depletion from actual sales — see
    inventory_ledger.compute_daily_depletion(). Two-layer error isolation
    matching pos.sync_all(): one restaurant's bad recipe data must never
    take down the whole night's run for everyone else.

    Resolved through pos.py rather than toast directly. This was Toast-only,
    so a Square, Clover or RPOWER restaurant with recipes mapped got no
    depletion at all — their ingredients read as never used, which overstates
    days remaining and keeps everything out of the reorder list, silently.
    """
    try:
        import pos
        import inventory_ledger
        import ops
        from datetime import timedelta as _td
        from models import get_all_restaurants, get_conn as _gc

        conn = _gc()
        restaurants_with_recipes = {
            row["restaurant_id"] for row in conn.execute(
                "SELECT DISTINCT mi.restaurant_id FROM recipe_ingredients ri "
                "JOIN menu_items mi ON mi.id = ri.menu_item_id"
            ).fetchall()
        }
        conn.close()

        counts = {"ok": 0, "total": 0}
        by_id = {}
        for r in get_all_restaurants():
            # Item-level sales (menu_item_sales) are what a post's dish lift
            # and menu engineering read, and they were only ever written for
            # restaurants with recipes. A marketing restaurant on a POS that
            # reports line items gets them too — with no recipes there is
            # nothing to deplete, and the same pass records the units sold.
            wants_item_sales = bool(getattr(r, "module_marketing", 0) or getattr(r, "module_inventory", 0))
            if r.id not in restaurants_with_recipes and not wants_item_sales:
                continue
            if (getattr(r, "billing_status", None) or "trial").lower() in ("churned", "cancelled", "canceled", "paused"):
                continue
            by_id[r.id] = r

        def _one(rid):
            r = by_id[rid]
            try:
                # A POS that cannot report item-level sales is skipped
                # explicitly rather than erroring per restaurant every night:
                # Square and Clover report daily totals but no line items, so
                # there is nothing to deplete from and that is a fact about
                # the integration, not a failure.
                if not pos.supports(r.id, "fetch_order_selections"):
                    return
                counts["total"] += 1
                end = _chi_now().date()
                start = end - _td(days=2)  # small overlap window, idempotent re-sync covers gaps
                business_dates, _provider = pos.fetch_business_days(r.id, start, end)
                for bd_str in business_dates:
                    result = inventory_ledger.compute_daily_depletion(r.id, __import__('datetime').date.fromisoformat(bd_str))
                    if result.get("unmapped_selections"):
                        log.warning(f"[inventory_depletion] {r.name}: "
                                   f"{len(result['unmapped_selections'])} unmapped selection(s) on {bd_str}")
                counts["ok"] += 1
            except Exception as e:
                log.warning(f"[inventory_depletion] {r.name} failed: {e}")
                ops.capture(e, job="inventory_depletion", context=r.name)

        # Bounded and resumable (MOD-FC-19): this walked every restaurant in
        # one serial pass with no bound and no cursor, so a pass that could
        # not finish reached the same restaurants every night, and a redeploy
        # mid-pass started again from the first.
        _done, ran_out = resumable_sweep(DEPLETION_CURSOR_KEY, sorted(by_id), _one,
                                         SWEEP_MAX_SECONDS, workers=SWEEP_WORKERS,
                                         job="inventory_depletion")
        if ran_out:
            _ops.capture(RuntimeError(f"Depletion sync stopped at the {SWEEP_MAX_SECONDS}s bound; "
                                      f"the rest lead the next pass"), job="inventory_depletion",
                         context="time_bound")
        if counts["total"]:
            log.info(f"Inventory depletion nightly sync: {counts['ok']}/{counts['total']} restaurants OK")
    except Exception as e:
        log.error(f"run_daily_depletion_sync error: {e}")


def refresh_expiring_tokens():
    """Refresh Instagram and Facebook tokens expiring within 7 days."""
    try:
        import requests as _req, os
        from models import get_all_restaurants, update_restaurant
        from datetime import datetime, timedelta

        from meta_api import graph_url
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
                resp = _req.get(graph_url("oauth/access_token"), params={
                    "grant_type": "fb_exchange_token",
                    "client_id": app_id, "client_secret": app_secret,
                    "fb_exchange_token": r.ig_token,
                })
                if resp.status_code == 200:
                    new_token   = resp.json().get("access_token", r.ig_token)
                    new_expires = (_chi_now() + timedelta(days=60)).strftime("%Y-%m-%d")
                    update_data = {"ig_token": new_token, "ig_token_expires": new_expires}
                    if r.fb_page_token:
                        resp2 = _req.get(graph_url("oauth/access_token"), params={
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


def run_marketing_metrics_sync():
    """Nightly: refresh Meta post-performance metrics for every restaurant
    with marketing on and a connected account. Previously this only ever ran
    client-side (a 60s poll while someone had the Marketing tab open), so the
    numbers behind the tab's analytics card were stale the moment nobody was
    looking — an owner who checks once a week saw whatever reach/engagement
    happened to be cached from their last visit, not real current totals."""
    try:
        from models import get_all_restaurants, in_service
        from social_routes import refresh_post_metrics

        candidates = [r for r in get_all_restaurants()
                     if r.module_marketing and (r.ig_token or r.fb_page_token) and in_service(r)]
        if not candidates:
            return
        log.info(f"Marketing metrics sync for {len(candidates)} restaurant(s)")
        for r in candidates:
            try:
                result = refresh_post_metrics(r.id)
                if result.get("ok"):
                    log.info(f"Metrics synced for {r.name} — {len(result.get('posts', []))} posts")
                else:
                    log.warning(f"Metrics sync skipped for {r.name}: {result.get('error')}")
            except Exception as e:
                log.error(f"Metrics sync error for {r.name}: {e}")
                _ops.capture(e, job="marketing_metrics_sync", context=r.name)
    except Exception as e:
        log.error(f"run_marketing_metrics_sync error: {e}")


# ── Scheduler loop ────────────────────────────────────────────────────────────


# The loop used to sleep for an hour, which was fine when everything it did
# was daily. Scheduled posts need finer granularity than "sometime this hour".
SCHEDULER_TICK_SECONDS = int(os.getenv("SCHEDULER_TICK_SECONDS", "300"))


# Catch-up gating (audit #17, P0-2).
#
# Every daily job used to be gated on `now.hour == H`: it ran only if a tick
# happened to land inside that one hour. The loop is sequential, so a job that
# ran long — or a deploy, a crash, or the lease changing hands at the wrong
# moment — meant later hours were never observed and those jobs silently did
# not run that day. Nothing failed; the review fetch, the digest or the
# nightly backup just didn't happen.
#
# `_due` makes a job eligible from its hour until `until`, and the per-day
# claim_period key still guarantees it runs at most once. So an on-time day
# behaves exactly as before, and a late day runs the job late instead of not
# at all.
def local_due(restaurant, hour, until=14, claim_key=None, now_local=None, day=None):
    """True when it is `hour` or later (and before `until`) in THIS
    restaurant's own timezone, and nothing has claimed it today.

    The loop ticks on Chicago time, which is right for infrastructure jobs
    and wrong for anything an owner reads: a Pacific client's "9am digest"
    arrived at 7am local, and a "10am" alert at 8am. Per-restaurant jobs
    gate on this instead, exactly as morning_brief.run_due does, so each
    restaurant is served at its own hour and the claim keeps it to once a
    day however many ticks observe the window.

    `day` restricts the window to one day of the month, in the restaurant's
    own calendar. The monthly and quarterly summaries need it: when the
    once-a-day guard moved in here (workflow audit 1/4/16) the loop's
    `now.day == 1` check went with it, and the "month in review" started
    going out every morning at 9am local.
    """
    from time_utils import restaurant_now
    local = now_local or restaurant_now(restaurant, naive=True)
    if day is not None and local.day != day:
        return False
    if not (hour <= local.hour < until):
        return False
    if claim_key is None:
        return True
    return _ops.claim_period(f"{claim_key}:{getattr(restaurant, 'id', restaurant)}",
                             local.date().isoformat())


def _due(now, hour, until=24):
    """True from `hour` (inclusive) until `until` (exclusive), local time."""
    return hour <= now.hour < until


def _latest_slot(now, slots):
    """The most recent slot at or before now, or None before the first.

    For multi-slot jobs (the 8/12/16/20 review fetch). Catching up claims only
    the LATEST missed slot, so an outage spanning two slots produces one fetch,
    not two back to back.
    """
    passed = [h for h in slots if h <= now.hour]
    return max(passed) if passed else None


# The opt-in invite texts guests. It checks guest quiet hours itself and
# DEFERS inside them — but a deferred run still spends the day's single claim,
# and tomorrow's run scans a different business date, so those guests would
# never be invited. Its catch-up window closes well before quiet hours begin.
OPTIN_INVITE_LATEST_HOUR = 20


# Backups keep this many days of local snapshots on the Railway volume.
BACKUP_RETAIN_DAYS = int(os.getenv("BACKUP_RETAIN_DAYS", "7"))

# Tables whose contents must never leave the server in a backup artifact.
# `sessions` holds live bearer tokens (auth.py stores only their hash now, but
# a restore never needs live sessions anyway); the rest hold short-lived
# credentials that are meaningless in a restore and dangerous in an archive.
_BACKUP_REDACT = {
    "sessions": "DELETE FROM sessions",
    "two_fa_backup_codes": "DELETE FROM two_fa_backup_codes",
    "trusted_devices": "DELETE FROM trusted_devices",
    "device_tokens": "DELETE FROM device_tokens",
}
_BACKUP_SCRUB_COLUMNS = [
    ("users", ["reset_token", "reset_token_expires", "recovery_email_code"]),
    ("restaurants", ["temp_password", "gmb_access_token", "gmb_refresh_token",
                     "ig_token", "fb_page_token", "stripe_customer_id"]),
]


def _write_consistent_snapshot(dest_path):
    """Consistent copy of a LIVE WAL database.

    shutil.copy2 was wrong here and silently so: in WAL mode (models.py sets
    journal_mode=WAL) committed transactions can still live in reviews.db-wal,
    and this process serves four request threads alongside the scheduler, so a
    plain file copy can miss recent commits or capture a torn page. Neither
    shows up until a restore is attempted, which is the worst possible moment
    to discover your only backup doesn't open. sqlite3's own backup API takes
    a proper online snapshot with the source locked page-by-page instead.
    """
    import sqlite3
    from models import DB_PATH
    src = sqlite3.connect(DB_PATH, timeout=30)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
            # Integrity-check the artifact itself, so a corrupt backup is
            # caught here rather than during an emergency restore.
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError(f"backup integrity_check failed: {result}")
        finally:
            dst.close()
    finally:
        src.close()


def _redact_snapshot(path):
    """Strip credentials from a snapshot before it leaves the server."""
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, stmt in _BACKUP_REDACT.items():
            if table in existing:
                conn.execute(stmt)
        for table, columns in _BACKUP_SCRUB_COLUMNS:
            if table not in existing:
                continue
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col in columns:
                if col in have:
                    conn.execute(f"UPDATE {table} SET {col}=NULL")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def _prune_old_backups(backup_dir):
    import glob
    cutoff = time.time() - (BACKUP_RETAIN_DAYS * 86400)
    for old in glob.glob(os.path.join(backup_dir, "cavnar_ai_backup_*.db")):
        try:
            if os.path.getmtime(old) < cutoff:
                os.unlink(old)
        except OSError:
            pass


def backup_db():
    """Daily 2am backup.

    Two changes from the original, both from the pre-launch audit:

    1. The snapshot is taken with sqlite3's online backup API and
       integrity-checked, not shutil.copy2 — see _write_consistent_snapshot.
    2. The artifact no longer carries credentials, and the emailed copy is
       encrypted. The old version base64'd the entire live database into an
       email every night: every restaurant's financials, guest phone numbers,
       and — because sessions were stored in plaintext — a working bearer
       token for every logged-in owner. One leaked mailbox was full account
       takeover for every customer at once.

    The primary backup is now a local snapshot on the Railway volume (kept
    BACKUP_RETAIN_DAYS days). Email is a secondary copy and requires
    BACKUP_ENCRYPTION_KEY to be set — without it the local backup still runs
    and the email is skipped rather than sent in the clear.
    """
    import base64
    from models import DB_PATH

    WILL_EMAIL = config.will_email()
    timestamp = _chi_now().strftime("%Y-%m-%d")
    filename = f"cavnar_ai_backup_{timestamp}.db"
    backup_dir = os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "backups")

    try:
        os.makedirs(backup_dir, exist_ok=True)
        local_path = os.path.join(backup_dir, filename)
        _write_consistent_snapshot(local_path)
        # The LOCAL snapshot is NOT redacted, deliberately.
        #
        # It was, and that quietly made it useless as the thing it exists to
        # be. Redaction deletes sessions, device_tokens, trusted_devices and
        # the 2FA backup codes, and nulls every OAuth credential — Google
        # Business Profile, Toast, Instagram, Stripe. Restoring from it
        # brought the data back and severed every integration for every
        # client, by hand, with no runbook.
        #
        # And it protected nothing: this file sits on the same Railway
        # volume as reviews.db itself, with identical exposure. Redaction is
        # about what LEAVES the server, so it now happens on a throwaway
        # copy made for the email and nowhere else. See docs/ops/RECOVERY.md.
        size_kb = round(os.path.getsize(local_path) / 1024, 1)
        log.info(f"backup_db: local snapshot {local_path} ({size_kb} KB)")
        _prune_old_backups(backup_dir)
    except Exception as e:
        log.error(f"backup_db: snapshot failed: {e}")
        try:
            _ops.capture(e, job="backup_db", context="snapshot")
        except Exception:
            pass
        return

    key = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    if not key:
        log.warning(
            "backup_db: BACKUP_ENCRYPTION_KEY not set — local snapshot kept, "
            "email copy skipped. Generate one with "
            "`python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and set it in Railway."
        )
        return

    if not _resend_key():
        log.warning("backup_db: RESEND_API_KEY not set — local snapshot kept, email skipped")
        return

    redacted_path = local_path + ".redacted"
    try:
        from cryptography.fernet import Fernet
        import shutil as _shutil
        # Redact a COPY. The email is the artifact that leaves the server;
        # the local snapshot stays whole so a restore is a restore.
        _shutil.copy2(local_path, redacted_path)
        _redact_snapshot(redacted_path)
        with open(redacted_path, "rb") as f:
            payload = Fernet(key.encode()).encrypt(f.read())
        enc_name = filename + ".enc"
        size_kb = round(len(payload) / 1024, 1)

        import resend as _resend
        _resend.api_key = _resend_key()
        _resend.Emails.send({
            "from": _emails.sender("ops"),
            "to":   [WILL_EMAIL],
            "subject": f"Daily DB backup — {timestamp} ({size_kb} KB, encrypted)",
            "html": _html_doc(f"""
<div style="font-family:-apple-system,sans-serif;max-width:480px;color:#1a1714">
  <p style="font-size:14px">Encrypted daily backup attached.</p>
  <table style="font-size:13px;color:#3a3530;border-collapse:collapse">
    <tr><td style="padding:3px 12px 3px 0;color:#7a736a">Date</td><td>{timestamp}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#7a736a">File</td><td>{enc_name}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:#7a736a">Size</td><td>{size_kb} KB</td></tr>
  </table>
  <p style="font-size:12px;color:#7a736a;margin-top:16px">
    Sessions, device tokens and API credentials are stripped from this copy.
    Decrypt with BACKUP_ENCRYPTION_KEY, then rename to reviews.db.
  </p>
</div>"""),
            "attachments": [{
                "filename": enc_name,
                "content":  base64.b64encode(payload).decode(),
            }],
        })
        log.info(f"backup_db: emailed encrypted {enc_name} ({size_kb} KB) to {WILL_EMAIL}")
    except Exception as e:
        log.error(f"backup_db: email copy failed (local snapshot is intact): {e}")
        try:
            _ops.capture(e, job="backup_db", context="email")
        except Exception:
            pass
    finally:
        # The redacted copy exists only to be encrypted and attached. Leaving
        # it on the volume would double the backup directory's size and put a
        # second, restore-useless file next to every real snapshot.
        try:
            if os.path.exists(redacted_path):
                os.unlink(redacted_path)
        except OSError as e:
            log.warning(f"backup_db: could not remove {redacted_path}: {e}")


# An owner who has signed in this many times has found their way around.
# The day-7 email is "here is what you are missing", and sending that to
# someone using the product daily is the clearest possible signal that
# nobody is reading what they do.
ONBOARDING_SETTLED_LOGINS = 3


def _onboarding_engagement(restaurant_id):
    """(logins, days_since_last_login) for this restaurant's own logins.

    Onboarding sent identically to an owner who has never signed in and one
    who signs in every morning. Both got "here's what you're missing" on
    day 7.
    """
    try:
        from models import get_conn as _gc
        conn = _gc()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS logins, MAX(last_login) AS last "
                "FROM users WHERE restaurant_id=? AND is_admin=0 AND last_login IS NOT NULL",
                (restaurant_id,)).fetchone()
        finally:
            conn.close()
        if not row or not row["last"]:
            return 0, None
        from datetime import datetime as _dt
        last = _dt.strptime(str(row["last"])[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        return int(row["logins"] or 0), (_dt.utcnow() - last).days
    except Exception as e:
        # Fail toward sending: a settled client receiving one extra tip email
        # is a smaller failure than a new client receiving no onboarding.
        log.warning(f"onboarding engagement lookup failed for {restaurant_id}: {e}")
        return 0, None


def run_onboarding_sequence(local_hour: int = None):
    """
    Check all active clients and send the right onboarding email based on days since signup.
    Runs at 10am in each restaurant's own timezone. Skips clients who already
    received each email (UNIQUE constraint).
    Only sends to clients with billing_status in ('trial', 'active').
    """
    from datetime import datetime, timedelta
    from models import get_all_restaurants, get_onboarding_sent, mark_onboarding_sent
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
        # 10am in the restaurant's own timezone, once a day — the loop
        # attempts this hourly. local_hour=None (a direct call: a test, an
        # admin re-run) is never time-gated, matching notify._gated_out.
        if local_hour is not None and not local_due(r, local_hour, claim_key="onboarding"):
            continue
        # Need a signup date
        if not r.created_at:
            continue
        # Onboarding day-2/7/30 are all product-tips/marketing content, not
        # transactional — the one flag gates all three from this single
        # early-continue, same as the owner_email/created_at guards above.
        _opted_out = bool(getattr(r, "marketing_emails_opt_out", 0))

        # parse_stored_dt normalises both stored shapes (naive SQLite
        # datetimes and offset-aware isoformat strings) to naive local.
        # Comparing the two directly used to raise TypeError here, which
        # escaped the loop and killed the whole sweep on the first
        # offset-aware restaurant — so nobody got onboarding email at all.
        created = parse_stored_dt(r.created_at)
        if created is None:
            continue

        days_since = (now - created).days
        already_sent = get_onboarding_sent(r.id)

        # Build module list for context
        modules = []
        if r.module_reviews:  modules.append("Review Intelligence")
        if r.module_labor:    modules.append("Labor Optimizer")
        if r.module_inventory: modules.append("Food Cost Control")
        if r.module_marketing: modules.append("Marketing Autopilot")

        # Day 2 — a window, not ">= 2". These sends were open-ended, which
        # was harmless only while the whole job was crashing before it could
        # send anything: with the crash fixed and onboarding_emails empty,
        # ">=" would mail a "getting started" note to clients who signed up
        # months ago. An upper bound keeps a late/paused scheduler catching
        # up without ever backfilling stale onboarding at a settled client.
        if _opted_out and days_since < 60:
            continue
        if 2 <= days_since <= 6 and "day_2" not in already_sent:
            try:
                send_onboarding_day2(
                    to_email=r.owner_email,
                    restaurant_name=r.name,
                    owner_name=r.owner_name,
                    modules=modules,
                    restaurant_id=r.id,
                )
                mark_onboarding_sent(r.id, "day_2")
                log.info(f"Onboarding day 2 sent to {r.owner_email} ({r.name})")
            except Exception as e:
                log.error(f"Onboarding day 2 failed for {r.name}: {e}")

        # Day 7 — same windowing rationale as day 2 above.
        elif 7 <= days_since <= 29 and "day_7" not in already_sent:
            # "Here's what you're missing" to someone who signs in every
            # morning is the clearest possible sign nobody reads what they
            # do. Marked sent, not deferred: they are past needing it.
            _logins, _days_idle = _onboarding_engagement(r.id)
            if _logins >= ONBOARDING_SETTLED_LOGINS and (_days_idle or 99) <= 7:
                mark_onboarding_sent(r.id, "day_7")
                log.info(f"Onboarding day 7 skipped for {r.name} — "
                         f"{_logins} logins, last {_days_idle}d ago")
                continue
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
                    restaurant_id=r.id,
                )
                mark_onboarding_sent(r.id, "day_7")
                log.info(f"Onboarding day 7 sent to {r.owner_email} ({r.name})")
            except Exception as e:
                log.error(f"Onboarding day 7 failed for {r.name}: {e}")

        # Day 30 — same windowing rationale; two weeks of grace, then the
        # onboarding sequence is simply over for that client.
        elif 30 <= days_since <= 44 and "day_30" not in already_sent:
            try:
                send_onboarding_day30(
                    to_email=r.owner_email,
                    restaurant_name=r.name,
                    owner_name=r.owner_name,
                    modules=modules,
                    restaurant_id=r.id,
                )
                mark_onboarding_sent(r.id, "day_30")
                log.info(f"Onboarding day 30 sent to {r.owner_email} ({r.name})")
            except Exception as e:
                log.error(f"Onboarding day 30 failed for {r.name}: {e}")

        # Days 60, 90, 180 — the lifecycle after onboarding. The retention
        # audit found nothing spoke to an owner between the day-30 check-in
        # and a cancellation email. These carry the business review (what is
        # now measurable, the first record window, the six-month ledger), so
        # like the monthly they are gated on monthly_review_enabled and NOT
        # on the marketing opt-out — the day-30 gate above already sent us
        # past that `continue` for opted-out clients, so re-check here.
        for _day in (60, 90, 180):
            _key = f"day_{_day}"
            if _day <= days_since <= _day + 14 and _key not in already_sent:
                if not getattr(r, "monthly_review_enabled", 1):
                    mark_onboarding_sent(r.id, _key)      # respected, not deferred
                    break
                try:
                    from emails import send_lifecycle_email
                    send_lifecycle_email(_day, to_email=r.owner_email, restaurant_name=r.name,
                                         owner_name=r.owner_name, restaurant_id=r.id)
                    mark_onboarding_sent(r.id, _key)
                    log.info(f"Lifecycle day {_day} sent to {r.owner_email} ({r.name})")
                except Exception as e:
                    log.error(f"Lifecycle day {_day} failed for {r.name}: {e}")
                break


def check_inactive_clients():
    """
    Alert Will when a client hasn't logged in for 14+ days.
    Runs every Monday at 11am. Only checks active/trial clients.
    """
    from datetime import datetime, timedelta
    from models import get_all_restaurants, get_conn

    RESEND_API_KEY_LOCAL = os.getenv("RESEND_API_KEY", "")
    WILL_EMAIL_LOCAL     = config.will_email()
    FROM_EMAIL_LOCAL     = config.from_email()

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
                        created = parse_stored_dt(r.created_at)
                        if created is not None and (now - created).days >= 3:
                            inactive.append({"name": r.name, "email": r.owner_email, "last_login": "Never logged in", "days": (now - created).days})
                    except Exception:
                        pass
            else:
                try:
                    ll = parse_stored_dt(last_login)
                    if ll is not None and ll < cutoff:
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
            "from": _emails.sender("ops"),
            "to": [WILL_EMAIL_LOCAL],
            "subject": f"👋 {len(inactive)} inactive client{'s' if len(inactive)>1 else ''} — check in this week",
            "html": _html_doc(f"""<div style="font-family:sans-serif;max-width:580px;margin:0 auto">
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
            </div>"""),
        })
        log.info(f"Inactive client alert sent — {len(inactive)} client(s)")
    except Exception as e:
        log.error(f"check_inactive_clients email failed: {e}")


def send_while_away_nudges():
    """Tell an owner who has gone quiet what happened while they were away.

    check_inactive_clients has flagged a 14-day silence to Will since it
    shipped. The owner heard nothing — and an owner who stopped opening the
    app is exactly the one who does not know it drafted nine replies and
    caught a waste spike in the meantime. Once per 30 days per person, only
    when there is something real to show, and through strategy_jobs._reach
    so it is push-or-email, never both, and respects the briefing budget.
    """
    from datetime import timedelta
    from models import get_all_restaurants, get_conn, DB_PATH
    from strategy_jobs import _reach
    sent = 0
    now = _chi_now()
    for r in get_all_restaurants():
        if getattr(r, "billing_status", "trial") not in ("trial", "active"):
            continue
        try:
            conn = get_conn()
            row = conn.execute("SELECT MAX(last_login) AS ll FROM users WHERE restaurant_id=? "
                               "AND is_admin=0 AND is_active=1", (r.id,)).fetchone()
            last = parse_stored_dt(row["ll"]) if row and row["ll"] else None
            if last is None or (now - last).days < 14:
                conn.close(); continue
            since = last.strftime("%Y-%m-%d %H:%M:%S")
            def n(sql, *a):
                try:
                    return int(conn.execute(sql, a).fetchone()[0] or 0)
                except Exception:
                    return 0
            drafted = n("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                        "AND draft_response IS NOT NULL AND fetched_at>=?", r.id, since)
            arrived = n("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                        "AND fetched_at>=?", r.id, since)
            waiting = n("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                        "AND response_status IN ('pending','drafted')", r.id)
            alerts = n("SELECT COUNT(*) FROM alert_log WHERE restaurant_id=? AND fired_at>=?", r.id, since)
            conn.close()
            if not (drafted or arrived or alerts):
                continue
            if not _ops.claim_period(f"while_away:{r.id}", now.strftime("%Y-%m")):
                continue
            days = (now - last).days
            lines = []
            if arrived:
                lines.append(f"{arrived} new review{'s' if arrived != 1 else ''} came in"
                             + (f"; {drafted} {'have' if drafted != 1 else 'has'} a reply drafted in your voice"
                                if drafted else "") + ".")
            if waiting:
                lines.append(f"{waiting} {'are' if waiting != 1 else 'is'} waiting on your approval.")
            if alerts:
                lines.append(f"{alerts} alert{'s' if alerts != 1 else ''} fired while you were away.")
            try:
                import good_news as _gn
                for g in (_gn.all_good_news(r.id, limit=1) or []):
                    lines.append(g["summary"])
            except Exception:
                pass
            title = f"While you were away — {days} days at {r.location_name or r.name}"
            if _reach(r.id, "while_away", title, " ".join(lines[:2]),
                      {"ask_prompt": "What did I miss while I was away?"},
                      DB_PATH, subject=title, lines=lines, email_type="while_away"):
                sent += 1
        except Exception as e:
            _ops.capture(e, job="while_away", context=f"restaurant_id={r.id}")
    return {"sent": sent}


# ── Jobs that used to live inline in scheduler_loop ──────────────────────────
#
# These three ran inside bare try/except blocks rather than through
# ops.run_job(), so they wrote no job_runs row, never reached ops.capture(),
# and never appeared in the 8am failure digest or the admin console's Jobs
# page. Two of them are the largest email senders in the system: the audit
# found the only evidence a monthly summary run had died halfway was a line
# in the Railway log, which nobody reads on the first of the month.

# The weekly Intel sweeps follow run_daily_fetch's pattern (CLAUDE.md: work
# that iterates every restaurant is bounded and resumable). Each was a serial
# loop over every full-tier restaurant with no time bound — competitor
# analysis is Places plus Claude per restaurant, visibility eight paced
# Perplexity queries — so at scale one Monday pass ran for hours or days and
# a crash lost its place (AI-9, MOD-INT-1). Now a wall-clock bound, and a
# job_cursors cursor so the next pass starts with whoever this one missed.
# One worker: visibility is paced against a single Perplexity rate limit,
# and neither job is urgent enough to contend for it.
WEEKLY_SWEEP_MAX_SECONDS = int(os.getenv("WEEKLY_SWEEP_MAX_SECONDS", str(3 * 3600)))
_COMPETITOR_CURSOR_KEY = "competitor_sweep_cursor"
_VISIBILITY_CURSOR_KEY = "ai_visibility_sweep_cursor"


def _weekly_sweep(job, cursor_key, restaurants, fn):
    """Run `fn(restaurant)` over `restaurants` under WEEKLY_SWEEP_MAX_SECONDS,
    starting after the cursor; returns (processed, hit_bound). `fn` returns
    True for a success and False for a handled failure, or raises."""
    by_id = {r.id: r for r in restaurants}
    order = _fetch_order(list(by_id), key=cursor_key)
    tally = {"attempted": 0}

    def _one(rid):
        tally["attempted"] += 1
        fn(by_id[rid])

    def _failed(rid, e):
        log.error(f"{job} failed for restaurant {rid}: {e}")
        _ops.capture(e, job=job, context=f"restaurant_id={rid}")

    done, ran_out = bounded_map(order, _one, 1, WEEKLY_SWEEP_MAX_SECONDS, on_error=_failed)
    # Advance past everything this pass reached, success or failure: a
    # restaurant whose analysis failed is retried next week, not first in
    # line forever ahead of the ones never reached.
    _remember_fetch_cursor(order, tally["attempted"], key=cursor_key)
    if ran_out:
        _ops.capture(
            RuntimeError(f"{job} covered {tally['attempted']} of {len(order)} restaurants before the "
                         f"{WEEKLY_SWEEP_MAX_SECONDS}s bound; the rest lead the next pass."),
            job=job, context="time_bound")
    return tally["attempted"], ran_out


def run_weekly_competitor_analysis():
    """Monday 6am — competitor analysis for every full-tier client in service."""
    import competitor
    from models import get_all_restaurants, in_service, is_full_tier
    counts = {"analysed": 0, "failed": 0}

    def _analyse(r):
        try:
            # Looked up at call time so a test's (or a hot patch's) swap of
            # competitor.run_competitor_analysis is honoured.
            res = competitor.run_competitor_analysis(r.id) or {}
        except Exception:
            counts["failed"] += 1
            raise
        # An analysis that returned ok:False did not analyse anything:
        # counting it as done hid a Places refusal behind "analysed".
        if res.get("ok") is False:
            counts["failed"] += 1
            if res.get("places_status") or "No nearby competitors" not in (res.get("error") or ""):
                _ops.capture(RuntimeError(res.get("error") or "competitor analysis failed"),
                             job="competitor_analysis", context=f"restaurant_id={r.id}")
        else:
            counts["analysed"] += 1

    # In service only (MOD-REV-2): no Places or Claude spend on a customer
    # who has cancelled.
    eligible = [r for r in get_all_restaurants()
                if r.google_place_id and r.id and is_full_tier(r) and in_service(r)]
    _weekly_sweep("competitor_analysis", _COMPETITOR_CURSOR_KEY, eligible, _analyse)
    return counts


def run_weekly_ai_visibility():
    """Monday 7am — one AI visibility run per full-tier client.

    There was no scheduled run at all. Visibility was checked only when an
    owner happened to open the Intel tab, so ai_visibility_runs accumulated
    at whatever rate they browsed: two adjacent "runs" could be weeks apart
    and were drawn as consecutive points on a trend line, and the drop alert
    compared them as though they were a week apart. A fixed cadence is what
    makes that history a trend rather than a record of tab-opening.

    force=True bypasses the six-hour display cache; the budget ceiling and
    the per-restaurant rate limit inside the call still apply.
    """
    import client_api
    from models import get_all_restaurants, in_service, is_full_tier
    counts = {"checked": 0, "failed": 0}

    def _check(r):
        try:
            payload, _ = client_api._do_ai_visibility_inner(r.id, force=True)
        except Exception:
            counts["failed"] += 1
            raise
        if payload.get("ok"):
            counts["checked"] += 1
        else:
            counts["failed"] += 1

    # In service only (MOD-REV-2): no Perplexity spend on a cancelled customer.
    eligible = [r for r in get_all_restaurants() if r.id and is_full_tier(r) and in_service(r)]
    _weekly_sweep("ai_visibility", _VISIBILITY_CURSOR_KEY, eligible, _check)
    return counts


def run_daily_alert_checks():
    """Unresponded, trend/threshold/labor, food waste and visibility alerts,
    then the retention purge. Attempted hourly; each restaurant is served at
    10am in its own timezone (notify._gated_out), once per local day. A
    failure in one must not take the rest down with it, which is why each is
    wrapped separately rather than the whole block sharing one except."""
    import notify as _notify
    from notify import check_no_response_alerts, check_daily_alerts, check_extra_daily_alerts
    out = {}
    # Collect across all three, then send once per restaurant. These eight
    # alert types describe the same week of trading, and arriving separately
    # is what teaches an owner to swipe Cavnar away without reading — taking
    # the morning brief with it. See notify.DAILY_BATCH_TYPES.
    _notify.begin_daily_batch()
    for name, fn in (("no_response", check_no_response_alerts),
                     ("daily", check_daily_alerts),
                     ("extra_daily", check_extra_daily_alerts)):
        try:
            fn(local_hour=10)
            out[name] = "ok"
        except Exception as e:
            out[name] = f"failed: {e}"
            log.error(f"Alert check {name} failed: {e}")
            _ops.capture(e, job="daily_alerts", context=name)
    try:
        # Always flushed, even when a check above raised: anything already
        # collected is an alert that passed every gate, and dropping it
        # because a later, unrelated check failed would be silent loss.
        out["batch"] = _notify.flush_daily_batch()
    except Exception as e:
        out["batch"] = f"failed: {e}"
        log.error(f"Daily alert batch flush failed: {e}")
        _ops.capture(e, job="daily_alerts", context="batch flush")
    try:
        from models import purge_expired_reviews
        purged = purge_expired_reviews()
        out["purged"] = purged
        if purged:
            log.info(f"Data retention: soft-deleted {purged} expired reviews")
    except Exception as pe:
        out["purged"] = f"failed: {pe}"
        log.error(f"Retention purge failed: {pe}")
        _ops.capture(pe, job="daily_alerts", context="retention purge")
    try:
        from models import prune_operational_logs
        out["pruned"] = prune_operational_logs()
    except Exception as le:
        out["pruned"] = f"failed: {le}"
        _ops.capture(le, job="daily_alerts", context="operational log prune")
    return out


def run_food_cost_snapshots():
    """Daily — write every active restaurant's inventory snapshot, and score
    any forecast whose period has closed.

    `inventory_history` had exactly one writer in production: the AI insight
    function, on page render. The weekly waste series, the multi-week price
    trends, the price-spike alert and the opening/closing values behind food
    cost % all read that table, so all four were functions of whether the
    owner happened to open the tab — and a week nobody looked at is ABSENT
    from the series rather than zero in it.

    Runs before the diagnosis pass below, which reads what this writes.
    """
    from models import get_conn
    import food_cost_intelligence as fci
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM restaurants WHERE module_inventory=1 "
        "AND COALESCE(billing_status,'trial') IN ('trial','active')"
    ).fetchall()
    conn.close()
    c = {"written": 0, "skipped": 0, "failed": 0, "scored": 0}
    lock = threading.Lock()

    def _one(rid):
        try:
            out = fci.weekly_snapshot(rid)
            with lock:
                c["written" if out.get("ok") else "skipped"] += 1
        except Exception as e:
            with lock:
                c["failed"] += 1
            log.error(f"Food cost snapshot failed for restaurant {rid}: {e}")
            _ops.capture(e, job="food_cost_snapshots", context=f"restaurant_id={rid}")
        try:
            fci.record_profitability_forecast(rid)
            n = fci.score_forecasts(rid).get("scored", 0)
            with lock:
                c["scored"] += n
        except Exception as e:
            _ops.capture(e, job="food_cost_forecast_scoring", context=f"restaurant_id={rid}")

    # Bounded and resumable, like the depletion sync (MOD-FC-19).
    _done, ran_out = resumable_sweep(SNAPSHOT_CURSOR_KEY, [row["id"] for row in rows], _one,
                                     SWEEP_MAX_SECONDS, workers=SWEEP_WORKERS, job="food_cost_snapshots")
    if ran_out:
        _ops.capture(RuntimeError(f"Food cost snapshots stopped at the {SWEEP_MAX_SECONDS}s bound; "
                                  f"the rest lead the next pass"), job="food_cost_snapshots", context="time_bound")
    written, skipped, failed, scored = c["written"], c["skipped"], c["failed"], c["scored"]
    log.info(f"Food cost snapshots: {written} written, {skipped} skipped, {failed} failed, "
             f"{scored} forecasts scored")
    return {"written": written, "skipped": skipped, "failed": failed, "forecasts_scored": scored}


def run_food_cost_diagnoses():
    """Daily — the root-cause read over each restaurant's ranked cost drivers.

    The module could say "waste is $420 this week" and stopped there; no
    prompt in the food-cost path asked why. This is a Sonnet call per
    restaurant over the drivers food_cost_intelligence.cost_drivers already
    ranked, so it runs here rather than on the critical path of a page load.
    The insight endpoint and the morning brief both READ what this writes.
    """
    from models import get_conn, get_restaurant
    import food_cost_intelligence as fci
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM restaurants WHERE module_inventory=1 "
        "AND COALESCE(billing_status,'trial') IN ('trial','active')"
    ).fetchall()
    conn.close()
    done, skipped, failed = 0, 0, 0
    for row in rows:
        rid = row["id"]
        try:
            r = get_restaurant(rid)
            # A restaurant whose AI budget is spent gets no diagnosis rather
            # than a refused call and a captured exception.
            if r and _ai_budget_spent(rid):
                skipped += 1
                continue
            out = fci.diagnose(rid)
            if out and out.get("ok"):
                done += 1
                # A fresh cause makes every cached food-cost narrative for
                # this restaurant out of date — it is what the narrative is
                # now built around.
                try:
                    from client_api import invalidate_insight_cache
                    invalidate_insight_cache(rid)
                except Exception:
                    pass
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            log.error(f"Food cost diagnosis failed for restaurant {rid}: {e}")
            _ops.capture(e, job="food_cost_diagnoses", context=f"restaurant_id={rid}")
    log.info(f"Food cost diagnoses: {done} produced, {skipped} skipped, {failed} failed")
    return {"diagnosed": done, "skipped": skipped, "failed": failed}


def _ai_budget_spent(rid):
    """Whether this restaurant's AI budget is spent, from the ledger.

    Both diagnosis sweeps used getattr(restaurant, "ai_budget_exceeded"),
    an attribute the Restaurant dataclass has never had — so the guard was
    always False and an over-budget restaurant paid a refused call and a
    captured exception per cluster every day (AI-25)."""
    import ai_utils
    return bool(ai_utils.ai_budget_exceeded(rid))


def run_review_diagnoses():
    """Daily — produce the root-cause read for each restaurant's biggest
    complaint clusters.

    This is the step the Reviews module never took. It could tell an owner
    "food quality is your most-mentioned complaint (11)" — which they already
    knew — and had no code that went further. review_intelligence.diagnose
    takes each cluster, the reviews behind it, and what the other modules
    recorded over the same period, and produces a cited cause with an
    alternative explanation and a way to tell them apart.

    It runs here, on a schedule, rather than on a page load: it is a Sonnet
    call per cluster, and putting that on the critical path of opening a tab
    would make the tab slow and the bill large for an answer that changes on
    the timescale of days. The insight endpoint and the weekly digest both
    READ what this writes.

    Per restaurant in its own try, for the same reason every other sweep in
    this file is: one restaurant's failure must not cost the rest theirs.
    """
    from models import get_conn, get_restaurant
    import review_intelligence as ri
    conn = get_conn()
    rows = conn.execute(
        "SELECT id FROM restaurants WHERE module_reviews=1 "
        "AND COALESCE(billing_status,'trial') IN ('trial','active')"
    ).fetchall()
    conn.close()
    done, failed, skipped = 0, 0, 0
    for row in rows:
        rid = row["id"]
        try:
            r = get_restaurant(rid)
            # A restaurant whose AI budget is spent gets no diagnosis rather
            # than a failed call per cluster — create_with_retry would refuse
            # each one individually and we would pay three exceptions for it.
            if r and _ai_budget_spent(rid):
                skipped += 1
                continue
            produced = ri.diagnose(rid)
            if produced:
                done += 1
                # A fresh cause makes every cached insight for this
                # restaurant out of date — it is the thing the insight is now
                # built around.
                try:
                    from client_api import invalidate_insight_cache
                    invalidate_insight_cache(rid)
                except Exception:
                    pass
        except Exception as e:
            failed += 1
            log.error(f"Review diagnosis failed for restaurant {rid}: {e}")
            _ops.capture(e, job="review_diagnoses", context=f"restaurant_id={rid}")
    log.info(f"Review diagnoses: {done} produced, {skipped} skipped, {failed} failed")
    return {"diagnosed": done, "skipped": skipped, "failed": failed}


def _push_month_ready(r):
    """"August 2026 is in" — to the people with the app, after their email.

    The monthly review was an email on the 1st and a card on Home, and
    nothing on the phone said the month was ready: the morning brief that
    day carries month-to-date prime cost, not the review. push OR email is
    the rule (morning_brief.deliver), so this goes only to device holders —
    everyone else was reached by the email a moment ago. The tap opens Ask
    on the month, the way a brief push does. monthly_review has been a
    defined push type (push.py priority map, the notification labels) since
    the alert layer was written and nothing ever fired it.
    """
    import push, notify, monthly_review
    from models import DB_PATH
    if not notify.briefing_allowed(r.id, "monthly_review", DB_PATH):
        return 0
    tokens = push.get_device_tokens(r.id, DB_PATH, for_delivery=True) or []
    users = {int(t.get("user_id") or 0) for t in tokens} - {0}
    if not users:
        return 0
    try:
        review = monthly_review.build(r.id, restaurant=r)
        month, head = review["month"], monthly_review.headline(review)
    except Exception as e:
        _ops.capture(e, job="month_ready_push", context=f"restaurant_id={r.id}")
        month, head = "Last month", "Your monthly review is in."
    push.fire_push(r.id, "monthly_review", f"{month} is in", head,
                   data={"ask_prompt": f"Walk me through {month.lower() if month == 'Last month' else month}"},
                   db_path=DB_PATH, user_ids=users)
    notify.record_notification(r.id, "monthly_review", db_path=DB_PATH)
    return len(users)


def run_monthly_summaries():
    """1st of the month, 9am — the monthly summary email to active clients."""
    from emails import send_monthly_summary_email, send_monthly_group_summary_email
    from models import get_all_restaurants
    sent = skipped = failed = 0
    # One email per OWNER: a three-location owner got three, each reading
    # as the whole business (moat audit #14). Restaurants due now are
    # grouped by owner email; a group of one takes the single-location path.
    due = []
    for r in get_all_restaurants():
        # 'paused' is the owner's own request for quiet — the monthly stops too.
        if not r.owner_email or r.billing_status in ('internal', 'churned', 'paused'):
            skipped += 1
            continue
        # 9am local on the 1st, not 9am Chicago — and the restaurant's own
        # date, so a Pacific client isn't summarised a day early. `day=1`
        # is the once-a-month part; the claim alone only makes it once a day.
        if not local_due(r, 9, claim_key="monthly_summary", day=1):
            skipped += 1
            continue
        # NOT gated on marketing_emails_opt_out. The monthly summary carries
        # the business review — metrics against last month, what the owner's
        # own changes were measured to do, goals, and the three things worth
        # fixing next — so unsubscribing from promotional mail used to cost
        # an owner their service report. It has its own switch.
        if not getattr(r, "monthly_review_enabled", 1):
            skipped += 1
            continue
        due.append(r)
    groups = {}
    for r in due:
        groups.setdefault((r.owner_email or "").strip().lower(), []).append(r)
    for _email, rs in groups.items():
        try:
            if len(rs) == 1:
                r = rs[0]
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
            else:
                send_monthly_group_summary_email(rs[0].owner_email, rs[0].owner_name, sorted(rs, key=lambda x: x.name))
            sent += len(rs)
            log.info(f"Monthly summary sent to {', '.join(x.name for x in rs)}")
            for r in rs:
                try:
                    _push_month_ready(r)
                except Exception as pe:
                    _ops.capture(pe, job="month_ready_push", context=f"restaurant_id={r.id}")
        except Exception as me:
            failed += len(rs)
            log.error(f"Monthly summary failed for {', '.join(x.name for x in rs)}: {me}")
            _ops.capture(me, job="monthly_summary", context=f"restaurant_id={rs[0].id}")
    return {"sent": sent, "skipped": skipped, "failed": failed}


def run_quarterly_summaries():
    """1st of Jan / Apr / Jul / Oct, 9am local — the quarter that just
    ended. Same audience and the same switch as the monthly."""
    from emails import send_quarterly_summary_email
    from models import get_all_restaurants
    sent = skipped = failed = 0
    for r in get_all_restaurants():
        if not r.owner_email or r.billing_status in ('internal', 'churned', 'paused'):
            skipped += 1
            continue
        if not local_due(r, 9, claim_key="quarterly_summary", day=1):
            skipped += 1
            continue
        if not getattr(r, "monthly_review_enabled", 1):
            skipped += 1
            continue
        try:
            send_quarterly_summary_email(to_email=r.owner_email, restaurant_name=r.name,
                                         owner_name=r.owner_name, restaurant_id=r.id)
            sent += 1
        except Exception as qe:
            failed += 1
            _ops.capture(qe, job="quarterly_summary", context=f"restaurant_id={r.id}")
    return {"sent": sent, "skipped": skipped, "failed": failed}


def run_auto_publish_schedules():
    """Friday, 9am local: queue the Thursday draft to go to staff at 11am,
    with the two hours as the undo window — for owners who turned it on AND
    whose last SCHEDULE_PUBLISH_TRUST_MIN published schedules went out
    unedited. The draft must be this coming week's, untouched, and not yet
    shared. Nothing is sent here; delayed.run_due sends it, and the owner
    is told now so "undo" is a real choice."""
    import delayed
    from models import get_all_restaurants, get_conn, schedule_publish_trust, SCHEDULE_PUBLISH_TRUST_MIN, DB_PATH
    from time_utils import restaurant_now
    queued = skipped = 0
    for r in get_all_restaurants():
        if not getattr(r, "auto_publish_schedule", 0) or not getattr(r, "module_labor", 0):
            continue
        if (getattr(r, "billing_status", "") or "trial") not in ("trial", "active"):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != 4 or not local_due(r, 9, claim_key="auto_publish_schedule"):
            skipped += 1
            continue
        if schedule_publish_trust(r.id) < SCHEDULE_PUBLISH_TRUST_MIN:
            skipped += 1
            continue
        conn = get_conn()
        try:
            # The coming week only — the earliest week_start after today —
            # and never a week that already went out in any version. It used
            # to take the newest future row, which could be a draft weeks
            # out, and to queue a regenerated copy of a week staff already
            # had (SCHED-28).
            nxt = conn.execute("SELECT MIN(week_start) AS w FROM schedule_history WHERE restaurant_id=? AND week_start > ?",
                               (r.id, local.date().isoformat())).fetchone()
            row = None
            if nxt and nxt["w"]:
                row = conn.execute(
                    "SELECT h.id, h.week_start FROM schedule_history h WHERE h.restaurant_id=? AND h.week_start=? "
                    "AND h.edited_at IS NULL AND (h.schedule_csv IS NOT NULL AND h.schedule_csv != '') "
                    "AND NOT EXISTS (SELECT 1 FROM schedule_history p WHERE p.restaurant_id=h.restaurant_id "
                    "  AND p.week_start=h.week_start AND (p.published_at IS NOT NULL "
                    "  OR EXISTS (SELECT 1 FROM schedule_shares s WHERE s.schedule_id=p.id))) "
                    "ORDER BY h.id DESC LIMIT 1", (r.id, nxt["w"])).fetchone()
        finally:
            conn.close()
        if not row:
            skipped += 1
            continue
        # Never unread: a week with flagged rows, a hard rule breach, a
        # weak quality verdict or a low-confidence score waits for a human,
        # and the owner is told why instead of being told it went out.
        try:
            from client_api import publish_blockers
            blockers = publish_blockers(r.id, row["id"])
        except Exception as e:
            _ops.capture(e, job="auto_publish_schedule_check", context=f"restaurant_id={r.id}")
            blockers = ["The publish check could not run"]
        if blockers:
            skipped += 1
            try:
                from strategy_jobs import _reach
                _reach(r.id, "schedule_publish_held",
                       "Next week's schedule needs a look before it goes out",
                       "The draft for the week of " + str(row["week_start"]) + " was not sent: "
                       + "; ".join(blockers[:3]) + ". Review it on the Labor tab and send it yourself.",
                       {"schedule_id": row["id"]}, DB_PATH,
                       subject=f"Next week's schedule is waiting on you — {r.name}")
            except Exception as e:
                _ops.capture(e, job="auto_publish_schedule_hold", context=f"restaurant_id={r.id}")
            continue
        try:
            action = delayed.schedule(r.id, "schedule_publish", {"schedule_id": row["id"]}, 120,
                                      label=f"Publishing the week of {row['week_start']} to staff")
            from strategy_jobs import _reach
            _reach(r.id, "schedule_publish_pending",
                   f"Next week's schedule goes to staff at 11am",
                   f"The week of {row['week_start']} is unchanged from the draft. Undo from Home before then if you'd rather look first.",
                   {"delayed_action_id": action["id"]}, DB_PATH,
                   subject=f"Publishing next week's schedule at 11am — {r.name}")
            queued += 1
        except Exception as e:
            _ops.capture(e, job="auto_publish_schedule", context=f"restaurant_id={r.id}")
    return {"queued": queued, "skipped": skipped}


def run_restore_drill():
    """Restore the newest snapshot into a scratch file and prove it.

    A backup nobody has restored is a hypothesis (docs/ops/RECOVERY.md). The drill
    lived in a runbook that depended on a laptop, a logged-in CLI and
    someone remembering the quarter; the local run could not even prove the
    thing the unredacted snapshot exists for — that OAuth tokens survive —
    because a local database holds none. This runs where the snapshot is.

    Quarterly, and from /admin → Jobs. Read-only against production: the
    snapshot is copied to a scratch path beside it, checked, migrated the
    way a real restore is (init_db), and deleted. Emails Will the result
    either way; a failure also lands in ops like any job.
    """
    import sqlite3, glob, shutil
    from models import DB_PATH, init_db
    backup_dir = os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "backups")
    snaps = sorted(glob.glob(os.path.join(backup_dir, "cavnar_ai_backup_*.db")))
    if not snaps:
        raise RuntimeError(f"restore drill: no snapshot in {backup_dir}")
    newest = snaps[-1]
    scratch = os.path.join(backup_dir, "restore_drill_scratch.db")

    def _clean():
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(scratch + suffix)
            except FileNotFoundError:
                pass

    def _q(path, sql):
        conn = sqlite3.connect(path, timeout=30)
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    _clean()
    shutil.copyfile(newest, scratch)
    report = {"snapshot": os.path.basename(newest),
              "size_mb": round(os.path.getsize(newest) / 1e6, 1), "ok": False}
    try:
        report["integrity"] = _q(scratch, "PRAGMA integrity_check")
        report["restaurants"] = _q(scratch, "SELECT COUNT(*) FROM restaurants")
        report["latest_review"] = _q(scratch, "SELECT MAX(review_date) FROM reviews")
        report["latest_alert"] = _q(scratch, "SELECT MAX(fired_at) FROM alert_log")
        tok = "SELECT COUNT(*) FROM restaurants WHERE gmb_refresh_token IS NOT NULL AND gmb_refresh_token != ''"
        report["google_tokens_in_snapshot"] = _q(scratch, tok)
        report["google_tokens_live"] = _q(DB_PATH, tok)
        # The migration path a real restore takes: init_db over an older file.
        init_db(scratch)
        report["integrity_after_migrate"] = _q(scratch, "PRAGMA integrity_check")
        report["ok"] = (report["integrity"] == "ok" and report["integrity_after_migrate"] == "ok"
                        and (report["restaurants"] or 0) > 0)
        # Tokens: the snapshot is up to a day old, so equality is not
        # required — but a snapshot with NONE while production has some is
        # the redaction bug back, and that fails the drill.
        report["tokens_survive"] = not (report["google_tokens_live"] and not report["google_tokens_in_snapshot"])
        report["ok"] = report["ok"] and report["tokens_survive"]
    finally:
        _clean()

    try:
        import html as _h
        lines = [f"Snapshot: {report['snapshot']} ({report['size_mb']} MB)",
                 f"integrity_check: {report.get('integrity')} → after init_db: {report.get('integrity_after_migrate')}",
                 f"Restaurants: {report.get('restaurants')}",
                 f"Newest review: {report.get('latest_review')} · newest alert: {report.get('latest_alert')}",
                 f"Google tokens: {report.get('google_tokens_in_snapshot')} in the snapshot, "
                 f"{report.get('google_tokens_live')} live — {'survive' if report.get('tokens_survive') else 'MISSING'}"]
        _emails.deliver(email_type="restore_drill", restaurant_id=None, payload={
            "from": _emails.sender("ops"), "to": [config.will_email()],
            "subject": f"Restore drill {'passed' if report['ok'] else 'FAILED'} — {report['snapshot']}",
            "preheader": "The quarterly proof that the backup restores.",
            "html": _emails._branded_email("".join(f"<p>{_h.escape(x)}</p>" for x in lines)
                                           + "<p>Record the date in docs/ops/RECOVERY.md.</p>")})
    except Exception as e:
        _ops.capture(e, job="restore_drill_email")
    if not report["ok"]:
        raise RuntimeError(f"restore drill failed: {report}")
    return report


def scheduler_loop():
    # No module-level "already ran" globals any more — every gate below is
    # ops.claim_period(), which is DB-backed and survives redeploys. See
    # ops.claim_period for what was wrong with the globals.
    log.info("Scheduler started — review fetch every 4hr (8am/12pm/4pm/8pm CT), digests 9am on client's chosen day")


    _lease_lost_logged = False

    while True:
        try:
            # One scheduler per deployment, enforced in the database rather
            # than by gunicorn's worker count. A second process idles here
            # and takes over only if the holder stops heartbeating.
            if not _ops.acquire_scheduler_lease():
                if not _lease_lost_logged:
                    holder = _ops.scheduler_lease_holder() or {}
                    log.info(f"Scheduler standing by — lease held by {holder.get('owner')} "
                             f"(last heartbeat {holder.get('heartbeat_at')})")
                    _lease_lost_logged = True
                time.sleep(SCHEDULER_TICK_SECONDS)
                continue
            if _lease_lost_logged:
                log.info("Scheduler lease acquired — this process is now the runner")
                _lease_lost_logged = False

            now   = _chi_now()
            today = now.date()

            # Monday 6am — run competitor analysis for all clients
            # 2am daily — backup DB to email
            if _due(now, 2) and _ops.claim_period("backup_db", str(today)):
                log.info("Running daily DB backup...")
                _ops.run_job("backup_db", backup_db)
                # The day after a backup, once a quarter: prove the newest snapshot
                # restores. See run_restore_drill.
                if today.day == 2 and today.month in (1, 4, 7, 10) and \
                        _ops.claim_period("restore_drill", f"{today}"):
                    _ops.run_job("restore_drill", run_restore_drill)
                # Straight after the backup, so the pruned rows are in it.
                _ops.run_job("prune_ledgers", _ops.prune_ledgers)

            if _due(now, 6) and now.weekday() == 0 and _ops.claim_period("competitor_analysis", str(today)):
                log.info("Running weekly competitor analysis...")
                _ops.run_job("competitor_analysis", run_weekly_competitor_analysis)

            # An hour after the competitor run, so the two weekly Intel jobs
            # do not compete for the same minute.
            if _due(now, 7) and now.weekday() == 0 and _ops.claim_period("ai_visibility", str(today)):
                log.info("Running weekly AI visibility checks...")
                _ops.run_job("ai_visibility", run_weekly_ai_visibility)

            if _due(now, 3) and _ops.claim_period("pos_sync", str(today)):
                log.info("Running nightly Toast POS sync...")
                _ops.run_job("pos_sync", run_toast_sync)

            # 3am+ — comps/voids/refunds from POSes that report them. After
            # pos_sync so the same night's data is settled first.
            if _due(now, 3) and _ops.claim_period("loss_sync", str(today)):
                from strategy_jobs import run_loss_sync
                _ops.run_job("loss_sync", run_loss_sync)

            if _due(now, 5) and _ops.claim_period("inventory_depletion", str(today)):
                log.info("Running nightly ingredient depletion sync...")
                _ops.run_job("inventory_depletion", run_daily_depletion_sync)

            # ── 5-6am chain: snapshot, then both root-cause passes ──
            # Placed straight after the depletion sync rather than where their
            # hour would suggest, because under catch-up gating several jobs
            # can come due in ONE tick and they then run in the order they
            # appear here. The snapshot must follow depletion, the food cost
            # diagnosis must follow the snapshot, and the 9am digest must
            # follow both diagnoses — which it did not, positionally, before.
            if _due(now, 5) and _ops.claim_period("food_cost_snapshots", str(today)):
                log.info("Writing food cost snapshots...")
                _ops.run_job("food_cost_snapshots", run_food_cost_snapshots)

            if _due(now, 6) and _ops.claim_period("review_diagnoses", str(today)):
                log.info("Running review root-cause diagnoses...")
                _ops.run_job("review_diagnoses", run_review_diagnoses)

            if _due(now, 6) and _ops.claim_period("food_cost_diagnoses", str(today)):
                log.info("Running food cost root-cause diagnoses...")
                _ops.run_job("food_cost_diagnoses", run_food_cost_diagnoses)

            # 6am+ — close outcome trackers whose window ended and mark met
            # goals, before the morning briefs (7am+ local) announce them.
            if _due(now, 6) and _ops.claim_period("outcome_evaluations", str(today)):
                from strategy_jobs import run_outcome_evaluations
                _ops.run_job("outcome_evaluations", run_outcome_evaluations)

            # 7am — after outcome evaluations, which is what moves the
            # measured-dollars figure the savings tiers read. Running it
            # first would mean a tier crossed today is not noticed until
            # tomorrow.
            if _due(now, 7) and _ops.claim_period("milestones", str(today)):
                from strategy_jobs import run_milestones
                _ops.run_job("milestones", run_milestones)

            # Sunday 5am — let each restaurant's own clean and troubled weeks
            # nudge its quality weights (strategy_jobs.run_quality_calibration).
            if _due(now, 5) and now.weekday() == 6 and _ops.claim_period("quality_calibration", str(today)):
                from strategy_jobs import run_quality_calibration
                _ops.run_job("quality_calibration", run_quality_calibration)

            # Monday 4am — what each published week actually did, by daypart.
            if _due(now, 4) and now.weekday() == 0 and _ops.claim_period("schedule_outcomes", str(today)):
                from strategy_jobs import run_schedule_outcomes
                _ops.run_job("schedule_outcomes", run_schedule_outcomes)

            # Wednesday 5am — reservation feeds into demand_signals, a day
            # ahead of the Thursday draft (reservation_feeds; no provider is
            # live yet, unconfigured restaurants are counted and skipped).
            if _due(now, 5) and now.weekday() == 2 and _ops.claim_period("reservation_sync", str(today)):
                from reservation_feeds import run_reservation_sync
                _ops.run_job("reservation_sync", run_reservation_sync)

            # Thursday 6am+ — draft next week's schedule for owners who opted
            # in. A draft in Schedule History; nothing reaches staff. A pass
            # an hour: each is time-bounded and starts at the cursor, and a
            # restaurant is attempted once a day, so a pass that ran out of
            # time is finished later the same Thursday, not next week
            # (SCHED-11).
            if _due(now, 6) and now.weekday() == 3 and _ops.claim_period("auto_draft_schedule", f"{today}-{now.hour}"):
                from strategy_jobs import run_auto_draft_schedules
                _ops.run_job("auto_draft_schedule", run_auto_draft_schedules)

            # Monday 7am local — the agent files the week's three actions.
            if now.weekday() == 0 and _ops.claim_period("weekly_plan", f"{today}-{now.hour}"):
                from strategy_jobs import run_weekly_plan
                _ops.run_job("weekly_plan", run_weekly_plan)

            # Tuesday 5am local — recipe drafts for dishes with none.
            if now.weekday() == 1 and _ops.claim_period("recipe_drafts", f"{today}-{now.hour}"):
                from strategy_jobs import run_recipe_drafts
                _ops.run_job("recipe_drafts", run_recipe_drafts)

            # Monday 8am local, per restaurant — queue trusted supplier orders
            # with an hour to undo (strategy_jobs.run_trusted_orders).
            if now.weekday() == 0 and _ops.claim_period("trusted_orders", f"{today}-{now.hour}"):
                from strategy_jobs import run_trusted_orders
                _ops.run_job("trusted_orders", run_trusted_orders)

            # Friday 9am local, per restaurant — queue the unedited draft
            # to publish at 11am with an undo window (run_auto_publish_schedules).
            if now.weekday() == 4 and _ops.claim_period("auto_publish_schedule", f"{today}-{now.hour}"):
                _ops.run_job("auto_publish_schedule", run_auto_publish_schedules)

            if _due(now, 4) and _ops.claim_period("marketing_metrics_sync", str(today)):
                log.info("Running marketing metrics sync...")
                _ops.run_job("marketing_metrics_sync", run_marketing_metrics_sync)

            if _due(now, 7) and _ops.claim_period("refresh_tokens", str(today)):
                log.info("Refreshing expiring IG/FB tokens...")
                _ops.run_job("refresh_tokens", refresh_expiring_tokens)

            # Fetch every 4 hours: 8am, 12pm, 4pm, 8pm Chicago time
            _fetch_slot = _latest_slot(now, (8, 12, 16, 20))
            if _fetch_slot is not None and _ops.claim_period("review_fetch", f"{today}-{_fetch_slot}"):
                log.info(f"Running review fetch for the {_fetch_slot}:00 CT slot "
                         f"(now {now.hour}:{now.minute:02d})...")
                _ops.run_job("review_fetch", run_daily_fetch)

            # 8am daily — operator failure digest (only sends if something failed)
            if _due(now, 8) and _ops.claim_period("ops_digest", str(today)):
                _ops.run_job("ops_failure_digest", _ops.send_failure_digest)

            # Attempted hourly: each restaurant is gated on ITS 9am inside
            # (local_due), so one Chicago-timed daily claim would serve only
            # the restaurants whose local hour happened to match.
            if _ops.claim_period("weekly_digest", f"{today}-{now.hour}"):
                log.info("Running weekly digest check...")
                _ops.run_job("weekly_digests", run_weekly_digests)

            if _due(now, 10) and now.weekday() == 0 and _ops.claim_period("stale_inventory", str(today)):
                # Monday 10am — check for stale inventory data
                log.info("Running stale inventory check...")
                _ops.run_job("stale_inventory", check_stale_inventory)

            # 10am daily — no-response + trend/threshold/labor alerts
            if _ops.claim_period("daily_alerts", f"{today}-{now.hour}"):
                log.info("Running daily alert checks...")
                _ops.run_job("daily_alerts", run_daily_alert_checks)

            # 1st of the month at 9am — send monthly summary to all active clients
            if today.day == 1 and today.month in (1, 4, 7, 10) and \
                    _ops.claim_period("quarterly_summary", f"{today}-{now.hour}"):
                _ops.run_job("quarterly_summaries", run_quarterly_summaries)

            # Attempted hourly so each restaurant is served at 9am in its own
            # timezone; run_monthly_summaries gates on the restaurant's own
            # 1st of the month (local_due day=1) and claims once per day.
            if _ops.claim_period("monthly_summary", f"{today}-{now.hour}"):
                log.info("Running monthly summary emails...")
                _ops.run_job("monthly_summary", run_monthly_summaries)

            if _ops.claim_period("onboarding", f"{today}-{now.hour}"):
                # 10am daily — onboarding email sequence
                log.info("Running onboarding sequence check...")
                _ops.run_job("onboarding_emails", run_onboarding_sequence, local_hour=10)

            if _due(now, 11) and now.weekday() == 0 and _ops.claim_period("inactive_clients", str(today)):
                # Monday 11am — inactive client check
                log.info("Running inactive client check...")
                _ops.run_job("inactive_clients", check_inactive_clients)
                _ops.run_job("while_away", send_while_away_nudges)

            if (_due(now, 11, until=OPTIN_INVITE_LATEST_HOUR)
                    and _ops.claim_period("optin_invite", f"{today}-{now.hour}")):
                # From 11am, hourly — invite guests Toast identified
                # yesterday to opt in for themselves. Yesterday, not today:
                # Toast's business day doesn't end at midnight, so today's is
                # still open and would be re-scanned tomorrow anyway. Hourly,
                # not once: 11am here is 6am in Hawaii, and a restaurant
                # outside its 8am-9pm window was deferred and never retried
                # (MOD-MKT-12). The job skips restaurants it already finished
                # for the date, so the later passes are cheap.
                log.info("Running Toast opt-in invites...")
                from guest_marketing import run_toast_optin_invites
                from datetime import date as _d, timedelta as _td
                _ops.run_job("toast_optin_invites",
                             lambda: run_toast_optin_invites(business_date=_d.today() - _td(days=1)))

            # 3am — the intelligence engine's feature pass (bounded, resumable),
            # then 4am learning over the materialized tables. INTELLIGENCE_ENGINE.md.
            if _due(now, 3) and _ops.claim_period("intelligence_features", str(today)):
                from intelligence import jobs as _intel_jobs
                _ops.run_job("intelligence_features", _intel_jobs.run_features)
            if _due(now, 4) and _ops.claim_period("intelligence_learning", str(today)):
                from intelligence import jobs as _intel_jobs
                _ops.run_job("intelligence_learning", _intel_jobs.run_learning)

            # Noon daily — which campaign recipients Toast saw on a later
            # check (guest_marketing.run_campaign_attribution). Reads
            # yesterday and earlier; each date fetched once per restaurant.
            if _due(now, 12) and _ops.claim_period("campaign_attribution", str(today)):
                from guest_marketing import run_campaign_attribution
                _ops.run_job("campaign_attribution", run_campaign_attribution)

            # Hourly — automated post-visit review request texts. Eligibility
            # is "N hours since last_visit" in each restaurant's local time, so
            # this needs hourly granularity, but no more than that: the loop
            # now ticks every few minutes for scheduled posts, and re-running
            # this twelve times an hour would just be twelve queries.
            if _ops.claim_period("review_request_followups", f"{today}-{now.hour}"):
                from guest_marketing import run_review_request_followups
                _ops.run_job("review_request_followups", run_review_request_followups)

            # Hourly — a fresh bad review becomes an issue for the routed
            # manager. Only where the owner has set routing up.
            if _ops.claim_period("issue_scan", f"{today}-{now.hour}"):
                from strategy_jobs import run_issue_scan
                _ops.run_job("issue_scan", run_issue_scan, local_hour=10)

            # During service — the only part of the product that can see a
            # day while it is happening (Toast reads; RPOWER is month-at-a-
            # time and says so). Capture is hourly per restaurant, the pulse
            # is one push before dinner, coverage runs while they're open.
            if _ops.claim_period("intraday", f"{today}-{now.hour}-{now.minute // 20}"):
                from strategy_jobs import run_intraday_capture, run_pre_dinner_pulse, run_coverage_check
                _ops.run_job("intraday_capture", run_intraday_capture)
                _ops.run_job("pre_dinner_pulse", run_pre_dinner_pulse)
                _ops.run_job("coverage_check", run_coverage_check)
                from strategy_jobs import run_preshift_nudge
                _ops.run_job("preshift_nudge", run_preshift_nudge)
                # How tonight went, once the doors are shut — the one part
                # of the day nothing reported on while the owner could
                # still picture the room.
                from strategy_jobs import run_closing_summary
                _ops.run_job("closing_summary", run_closing_summary)
                # A quiet night two days out, once a week — the one area of
                # the product that produced no notification at all.
                from strategy_jobs import run_demand_opportunity
                _ops.run_job("demand_opportunity", run_demand_opportunity)

            # Every tick — an alert held through lunch or dinner service goes
            # out as soon as that rush ends (notify.rush_release_at).
            try:
                import notify as _notify_rel
                _notify_rel.release_due_alerts()
            except Exception as e:
                _ops.capture(e, job="release_held_alerts")

            # Every tick — issue escalations and held notifications need
            # minutes, not hours; morning briefs go at each restaurant's own
            # local hour and claim themselves per restaurant per day.
            try:
                import issues as _issues
                _issues.tick()
            except Exception as e:
                _ops.capture(e, job="issues_tick")
            try:
                import morning_brief as _mb
                _mb.run_due()
            except Exception as e:
                _ops.capture(e, job="morning_brief")

            # Every tick — run any delayed action whose undo window has
            # closed (delayed.py: auto-publish, trusted-supplier send).
            try:
                import delayed as _delayed
                _dl = _delayed.run_due()
                if _dl.get("ran") or _dl.get("failed"):
                    log.info(f"Delayed actions: {_dl}")
            except Exception as e:
                _ops.capture(e, job="delayed_actions")

            # Daily — drop login-attempt rows older than two days.
            if _ops.claim_period("prune_login_attempts", str(today)):
                try:
                    import security as _security
                    _security.prune_login_attempts()
                except Exception as e:
                    _ops.capture(e, job="prune_login_attempts")

            # Every tick — publish anything whose scheduled slot has arrived.
            # This is why the loop no longer sleeps for an hour: a post the
            # owner set for 11am should go out at 11am, not at 11:59.
            try:
                from marketing_publish import run_due_posts
                _base = config.base_url()
                # Not named `_due`: any assignment to a name inside this
                # function makes it local for the WHOLE function, so the
                # `_due(now, H)` gates above would raise UnboundLocalError on
                # every tick and no scheduled job would ever run.
                _posts = run_due_posts(base_url=_base)
                if _posts.get("published") or _posts.get("failed"):
                    log.info(f"Scheduled posts: {_posts}")
            except Exception as e:
                log.error(f"Scheduled post run failed: {e}")

            try:
                record_scheduler_heartbeat()
                run_health_checks()
            except Exception:
                pass

        except Exception as e:
            log.error(f"Scheduler loop error: {e}")

        # Five minutes, not an hour. Every daily/hourly job above is gated on
        # its own "already ran for this hour/date" marker, so a faster tick
        # doesn't re-run any of them — it exists so a post scheduled for 11am
        # publishes within a few minutes of 11am.
        time.sleep(SCHEDULER_TICK_SECONDS)


def scheduling_allowed():
    """Whether this process may run scheduled jobs at all.

    Only on Railway, unless ALLOW_LOCAL_SCHEDULER=1 says otherwise. A local
    dev server has its own SQLite file — and therefore its own scheduler
    lease — but the same Resend and Twilio keys as production, so it ran every
    job a second time against a copy of real restaurants: a second morning
    brief at 7:52, a second "one week in" at 1:39pm, a failure digest about
    the dev machine's own errors. Morning briefs, digests, alerts and issue
    texts reach real people; a laptop must never be a second sender.
    """
    # Held while a restore is in progress: jobs must not write into the
    # restored database (or send from it) until someone has confirmed it and
    # removed RESTORE_FROM.
    if (os.getenv("RESTORE_FROM") or "").strip():
        return False
    if os.getenv("ALLOW_LOCAL_SCHEDULER", "").strip().lower() in ("1", "true", "yes"):
        return True
    # Railway sets all of these on every deploy; any one is enough.
    return any(os.getenv(v) for v in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID",
                                       "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"))


def start_scheduler():
    if not scheduling_allowed():
        log.warning("Scheduler NOT started: not on Railway. Set ALLOW_LOCAL_SCHEDULER=1 to run "
                    "jobs locally — they send real email and SMS.")
        print("Scheduler not started (local) — set ALLOW_LOCAL_SCHEDULER=1 to run jobs here")
        return None
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    # A redeploy SIGTERMs this process; gunicorn exits the worker cleanly and
    # atexit runs, so the replacement takes the lease on its next tick rather
    # than 30 minutes later (ops.release_scheduler_lease).
    import atexit
    atexit.register(_ops.release_scheduler_lease)
    log.info("Scheduler thread started")
    return t


def auto_approve_five_stars(rid: int, restaurant) -> int:
    """Approves (and, via _do_approve, posts to Google when connected) drafted
    5-star responses for a restaurant with the rule on. Returns how many it
    approved. Capped per day and logged per review so the Account tab can
    show 'N auto-approved today'."""
    if not getattr(restaurant, "auto_approve_5star", 0) or getattr(restaurant, "auto_approve_paused", 0):
        return 0
    from models import auto_approve_candidates, count_auto_approved_today, in_service, log_event
    # Never publish on a former customer's listing under their name, whatever
    # the auto-approve flags still say (MOD-REV-2).
    if not in_service(restaurant):
        return 0
    cap = int(getattr(restaurant, "auto_approve_daily_cap", 5) or 0)
    done_today = count_auto_approved_today(rid)
    if cap and done_today >= cap:
        return 0
    approved = 0
    from ai_guard import check_public_reply
    never_say = getattr(restaurant, "never_say", "") or ""
    # 4-star joins the rule only when the owner turned it on. Same cap, same
    # urgency and needs-review gates; negative reviews never go here.
    ratings = (4, 5) if getattr(restaurant, "auto_approve_4star", 0) else (5,)
    # Earned: any band (3-5 only — auto_approve_trust never returns a
    # negative band) where the owner's own edit rate says the drafts go out
    # unchanged anyway. Measured, not assumed; re-measured every run.
    if getattr(restaurant, "auto_approve_earned", 0):
        try:
            from models import auto_approve_trust
            earned = tuple(star for star, t in auto_approve_trust(rid).items() if t["trusted"])
            ratings = tuple(sorted(set(ratings) | set(earned)))
        except Exception as te:
            log.warning(f"auto-approve trust unavailable for rid={rid}: {te}")
    for candidate in auto_approve_candidates(rid, ratings=ratings):
        if cap and done_today + approved >= cap:
            break
        review_id = candidate["id"]
        # The draft is model-written from text a stranger wrote, and this is
        # the one path that publishes it to a live Google listing with nobody
        # reading it first. Anything that fails the check stays drafted for
        # the owner to look at — refusing to auto-publish is always safe,
        # publishing something odd is not.
        refusal = check_public_reply(candidate.get("draft_response"), never_say=never_say)
        if refusal:
            log.warning(f"Auto-approve skipped review {review_id}: {refusal}")
            # Marked for the owner's review, which also takes it out of the
            # candidates: it stayed one and was re-logged and re-captured
            # four times a day forever (MOD-REV-16).
            try:
                from models import get_conn as _gc_held
                _hc = _gc_held()
                _hc.execute("UPDATE reviews SET draft_needs_review=1, draft_review_reason=? "
                            "WHERE id=? AND restaurant_id=?",
                            (f"held from auto-approve: {refusal}", review_id, rid))
                _hc.commit()
                _hc.close()
            except Exception as _he:
                log.warning(f"could not mark held draft {review_id}: {_he}")
            log_event(rid, "review_auto_approve_held", {"review_id": review_id, "reason": refusal})
            try:
                import ops
                ops.capture(RuntimeError(f"auto-approve held: {refusal}"),
                            job="auto_approve_five_stars",
                            context=f"restaurant_id={rid} review_id={review_id}")
            except Exception:
                pass
            continue
        try:
            from client_api import _do_approve
            payload, status = _do_approve(review_id, rid)
        except Exception as e:
            log.error(f"Auto-approve failed for review {review_id}: {e}")
            continue
        if status != 200 or not payload.get("ok"):
            continue        # someone else approved it first (compare-and-set)
        if payload.get("post_error"):
            # Approved but NOT published: it did not use up the owner's cap of
            # unread public replies, and it is reported (MOD-REV-10). It stays
            # 'approved' for Retry posting.
            try:
                import ops
                ops.capture(RuntimeError(f"auto-approve could not publish: {payload['post_error']}"),
                            job="auto_approve_five_stars",
                            context=f"restaurant_id={rid} review_id={review_id}")
            except Exception:
                pass
            continue
        log_event(rid, "review_auto_approved", {"review_id": review_id})
        approved += 1
    if approved:
        log.info(f"Auto-approved {approved} five-star responses for rid={rid}")
    return approved
