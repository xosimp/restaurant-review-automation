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
from status_manager import record_scheduler_heartbeat, record_running_job, run_health_checks
import emails as _emails
import jobs_registry
import ops as _ops
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo as _ZI_sch
from time_utils import parse_stored_dt, mdy
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

        # Through emails.deliver, like every other owner email: this was a
        # direct SDK send, so a bounced or complained address kept getting it
        # and nothing reached email_log (MOD-EML-4).
        _alert_rid = next((r.get("restaurant_id") for r in urgent_reviews if r.get("restaurant_id")), None)
        if _alert_rid is None:
            try:
                from models import get_conn as _gc0
                _c0 = _gc0()
                _row0 = _c0.execute("SELECT id FROM restaurants WHERE owner_email=? LIMIT 1", (owner_email,)).fetchone()
                _c0.close()
                _alert_rid = _row0[0] if _row0 else None
            except Exception:
                _alert_rid = None
        _emails.deliver_or_raise(email_type="urgent", restaurant_id=_alert_rid, payload={
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
    <strong>{_html.escape(restaurant_name or "")}</strong> received
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
        log.info(f"Urgent alert handled for {owner_email} ({restaurant_name})")
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

    The writes only ever move the cursor forward (#137): each prefix was
    computed under the lock but written after it, so with several workers
    a thread holding an older, shorter prefix could write last and leave
    the cursor behind restaurants already done — the next pass re-fetched
    them first. Seen as a flaky test on the six-worker review fetch.
    """
    order = _fetch_order(list(ids), key=key)
    if not order:
        return 0, False
    lock, write_lock = threading.Lock(), threading.Lock()
    finished, state = set(), {"prefix": 0, "written": 0}

    def _covered(rid):
        with lock:
            finished.add(rid)
            p = state["prefix"]
            while p < len(order) and order[p] in finished:
                p += 1
            advanced = p != state["prefix"]
            state["prefix"] = p
        if advanced:
            # One writer at a time, and never backwards; the SQLite write
            # stays outside `lock` so workers finishing are not held up.
            with write_lock:
                if p > state["written"]:
                    _remember_fetch_cursor(order, p, key=key)
                    state["written"] = p

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
    entry the account shows (review_fetch_gap is an ACCOUNT_EVENT_TYPE), and
    a failure-digest line for the operator. The last total seen lives in
    job_cursors; the running coverage — reviews stored ÷ Google's growth —
    in `places_coverage:<rid>` (fetcher.places_coverage reads it, CA3 F13)."""
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
    try:
        import fetcher
        fetcher.record_places_coverage(rid, int(total) - prev, int(stored_new or 0))
    except Exception as e:
        log.warning(f"places coverage for {rid}: {e}")
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


RATING_REFRESH_HOURS = 12


def _rating_refresh_due(restaurant, now=None) -> bool:
    """True when Google's published rating (restaurants.gbp_rating_updated_at)
    is missing or older than RATING_REFRESH_HOURS."""
    from time_utils import parse_stamp
    stamp = parse_stamp(getattr(restaurant, "gbp_rating_updated_at", None))
    if stamp is None:
        return True
    now = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp) >= timedelta(hours=RATING_REFRESH_HOURS)


def run_daily_fetch(restaurant_ids=None):
    """Fetch reviews for all live clients, analyse, draft, alert on urgent.

    `restaurant_ids` narrows the pass to those restaurants — the console's
    "Sync now" (admin fetch-reviews), which used to fetch and alert on its
    own, alerting on UNANALYSED reviews and never recording the sync, so
    the issue that prompted it never cleared (#65, #120). A narrowed pass
    runs exactly this code — analyse, then alert, last_fetched_at, the Data
    Health ledger, the in-service check — and leaves the fleet's cursor
    alone.

    Returns the standard counts: a restaurant whose fetch reached no
    provider is a FAILED restaurant, not a success (#39) — a night on which
    every Google call failed was a green run."""
    try:
        from models import get_conn, get_restaurant, save_reviews
        from models import get_pending_analysis, get_pending_drafts, update_last_fetched
        from fetcher import fetch_google
        from analyser import analyse_review
        from drafter import draft_response

        conn = get_conn()
        # In service only (MOD-REV-2): a cancelled restaurant is not fetched,
        # analysed, drafted or alerted — its reviews are no longer ours to
        # read and its Google listing no longer ours to reply on. A
        # deletion request does NOT stop the fetch (fix round B2): the
        # account is served until the offboarding checklist deletes it or
        # it churns, and a request can be withdrawn — skipping it left a
        # hole in the owner's reviews for the whole 30-day notice.
        from models import in_service_sql
        live = conn.execute(
            "SELECT id FROM restaurants WHERE (reviews_live=1 OR gmb_refresh_token IS NOT NULL) "
            "AND " + in_service_sql()
        ).fetchall()
        conn.close()
        if restaurant_ids is not None:
            wanted = {int(x) for x in restaurant_ids}
            live = [r for r in live if r["id"] in wanted]

        if not live:
            return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False,
                    "restaurants": 0, "processed": 0}

        log.info(f"Daily fetch for {len(live)} live restaurant(s)")

        def _process_restaurant(row):
            rid = row["id"]
            restaurant = get_restaurant(rid)
            if not restaurant:
                return None

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
            gbp_revoked = False     # the revoked-token branch told the owner itself
            fetch_error = None

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
                        gbp_revoked = True
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
                            # Google's own published rating, refreshed with the
                            # reviews at most every RATING_REFRESH_HOURS. Only the
                            # admin fetch used to write it, so the rating alert
                            # (which now needs a recent rating) went quiet.
                            try:
                                if _rating_refresh_due(restaurant):
                                    from gmb import fetch_location_rating
                                    fetch_location_rating(rid, token, loc_id)
                            except Exception as _re:
                                log.warning(f"GBP rating refresh [{restaurant.name}]: {_re}")
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
                            fetch_error = f"Places fallback failed: {str(e)[:160]}"
                            log.error(f"Places fallback [{restaurant.name}]: {e}")
                            _ops.capture(e, job="review_fetch", context=f"Places fallback {restaurant.name}")

            elif restaurant.google_place_id:
                try:
                    _pl = fetch_google(restaurant.google_place_id, rid)
                    reviews += _pl
                    places_total = getattr(_pl, "total", None)
                    fetched_ok = True
                except Exception as e:
                    fetch_error = f"Google fetch failed: {str(e)[:160]}"
                    log.error(f"Google fetch [{restaurant.name}]: {e}")
                    _ops.capture(e, job="review_fetch", context=f"Google {restaurant.name}")

            # Only a fetch that actually reached a provider counts as a sync.
            # A failed one leaves last_fetched_at where it was, so the 25-hour
            # staleness check sees it and the status page goes degraded.
            if fetched_ok:
                update_last_fetched(rid)
            else:
                log.warning(f"Review fetch did not complete for {restaurant.name} — last_fetched_at left stale on purpose")
            _record_review_fetch(restaurant, fetched_ok, gbp_listing is not None, gmb_failed_reason,
                                 gbp_revoked, fetch_error)

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

            # A review that just arrived may answer a review request: matched
            # by the guest's name, one-to-one or not at all (review_signals;
            # memory audit 9/29/26, uncaptured).
            if new_reviews:
                try:
                    import review_signals
                    review_signals.match_review_requests(rid)
                except Exception as _me:
                    log.error(f"Review request matching error [{restaurant.name}]: {_me}")

            # Draft. Same unconditional-sweep reasoning as the analysis loop
            # above. Each draft picks its own style examples from the owner's
            # approvals for reviews of its star band (drafter.draft_response;
            # memory audit 9/29/26, reply_voice) — four fetched here once
            # used to be every draft's examples, 1-star or 5.
            for r in get_pending_drafts(rid, limit=50):
                try:
                    draft_response(r.id, r.rating, r.text, r.sentiment,
                                  restaurant.name, restaurant.voice_notes or "",
                                  restaurant_id=rid,
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

            # Email + SMS alerts are handled by notify.fire_review_alerts() at
            # ingest time, above. A per-restaurant "urgent reviews fetched in
            # the last two hours" SELECT whose result nothing read stood here
            # (#162); removed after the ten-point trace (commit message).
            return {"fetched_ok": fetched_ok, "new": new_count}

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
        by_id = {r["id"]: r for r in live}
        counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
        lock = threading.Lock()

        def _one(rid):
            try:
                out = _process_restaurant(by_id[rid])
            except Exception:
                with lock:
                    counts["attempted"] += 1
                    counts["failed"] += 1
                raise
            with lock:
                if out is None:
                    counts["skipped"] += 1
                    return
                counts["attempted"] += 1
                # A fetch that reached no provider is a failed restaurant:
                # it was counted ok, so a night on which every Google call
                # failed read as a clean run (#39, RELIABILITY-9).
                counts["ok" if out["fetched_ok"] else "failed"] += 1

        if restaurant_ids is not None:
            # "Sync now" for named restaurants: the same work, no bound, and
            # the fleet's cursor left where the scheduled pass put it.
            ran_out = False
            for rid in sorted(by_id):
                try:
                    _one(rid)
                except Exception as e:
                    log.error(f"Review cycle failed for restaurant {rid}: {e}")
                    _ops.capture(e, job="review_fetch", context=f"restaurant_id={rid}")
        else:
            # The cursor is the end of the longest finished PREFIX, saved as
            # each restaurant finishes (resumable_sweep, #137). It was written
            # once, after the pass, from the SUCCESS count, so a pass with F
            # failures landed F places short and re-ran the last F successes,
            # and a deploy mid-pass lost the whole pass's place.
            _done, ran_out = resumable_sweep(_FETCH_CURSOR_KEY, sorted(by_id), _one, FETCH_MAX_SECONDS,
                                             workers=FETCH_WORKERS, job="review_fetch")
        done = counts["ok"] + counts["failed"]
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
        return {"restaurants": len(live), "processed": counts["ok"], "hit_time_bound": ran_out,
                "attempted": counts["attempted"], "ok": counts["ok"], "failed": counts["failed"],
                "skipped": counts["skipped"], "hit_bound": bool(ran_out)}

    except Exception as e:
        # Captured AND re-raised (DH2-1): logged only, a pass that never
        # started — a locked database, a bad import — was a green job_runs row.
        log.error(f"Daily fetch error: {e}")
        _ops.capture(e, job="review_fetch", context="outer")
        raise


# A connected Business Profile served from the Places fallback this many
# fetches in a row — whatever the cause — tells the owner (connection_lost,
# at most weekly), not only the operator: replies can't post and only five
# reviews arrive a fetch (DH2-9). Four fetches is one day.
GBP_FALLBACK_ALERT_SLOTS = 4


def _record_review_fetch(restaurant, fetched_ok, gbp_ok, gmb_failed_reason, gbp_revoked, fetch_error):
    """Record one review fetch in the Data Health ledger: `reviews` (with
    how it was fetched — gbp, places_fallback or places — as its provider)
    and, for a Business Profile connection, `gbp` (whose consecutive
    failures are the fallback slots data_freshness reads as a sampled,
    failing source). Then the owner alert after GBP_FALLBACK_ALERT_SLOTS.
    Never raises."""
    rid = restaurant.id
    try:
        import data_health
        mode = "gbp" if gbp_ok else ("places_fallback" if restaurant.gmb_refresh_token else "places")
        if restaurant.gmb_refresh_token:
            data_health.record_attempt(rid, "gbp", bool(gbp_ok), provider="gbp",
                                       error=None if gbp_ok else (gmb_failed_reason or "Business Profile fetch failed"))
        data_health.record_attempt(rid, "reviews", bool(fetched_ok), provider=mode,
                                   error=None if fetched_ok else (fetch_error or gmb_failed_reason
                                                                  or "review fetch failed"))
        if not restaurant.gmb_refresh_token or gbp_ok or gbp_revoked:
            return
        fails = int((data_health.health_rows(rid).get("gbp") or {}).get("consecutive_failures") or 0)
        if fails >= GBP_FALLBACK_ALERT_SLOTS and _ops.claim_period(f"connection_lost:{rid}",
                                                                   _chi_now().strftime("%G-W%V")):
            import notify as _nf
            _nf.raise_alert(
                rid, "connection_lost",
                f"Cavnar AI: Google Business Profile isn't answering — reviews for {restaurant.name} "
                "are coming in five at a time.",
                f"Google needs attention — {restaurant.name}",
                lines=["Your Google Business Profile connection has not answered for the last "
                       f"{fails} review checks, so Cavnar AI is reading the five most recent reviews from "
                       "Google Maps instead. Replies cannot post until it answers again.",
                       "Open Account → Connections and reconnect Google."])
    except Exception as e:
        log.warning(f"review fetch not recorded for {rid}: {e}")



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

    Returns the standard counts over the emails (one per owner address):
    attempted, ok (Resend accepted it), failed, skipped (a restaurant with no
    address or nothing measured, an address already sent to today). A
    failure outside one email raises (#39).
    """
    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    try:
        from models import get_restaurants_for_digest, get_restaurant
        from reporter import build_report_from_db, render_html

        today = _chi_now().strftime("%A").lower()
        scheduled = get_restaurants_for_digest(today)
        if not scheduled:
            return counts

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
                counts["skipped"] += 1
                continue
            try:
                report = build_report_from_db(rid, restaurant.name, days=7)
                # No digest without something measured this week in a
                # module the restaurant has on (NS4 C2). This skipped only
                # when there were no reviews AND no other module was switched
                # on — so a switched-on module with nothing in it got a
                # generated email of invented comparisons.
                from reporter import digest_has_data
                if not digest_has_data(restaurant, report):
                    log.info(f"Not enough data this week for {restaurant.name} — skipping digest")
                    counts["skipped"] += 1
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
                counts["attempted"] += 1
                counts["failed"] += 1

        from time_utils import restaurant_now as _rnow
        for key, bucket in by_email.items():
            items = bucket["items"]
            first_rest, first_rep = items[0]
            # Once per address per day, recorded only when it was actually
            # delivered — so a pass retried after a failure (below) never
            # mails an address that already got it.
            sent_period = _rnow(first_rest, naive=True).date().isoformat()
            if _ops.period_claimed(f"weekly_digest_to:{key}", sent_period):
                counts["skipped"] += 1
                continue
            counts["attempted"] += 1
            try:
                import rec_delivery
                owner_name = _emails.greeting_name(first_rest)
                # What the digest renders is staged while it is built and
                # presented to rec_ledger only once Resend accepted it — a
                # failed or suppressed send showed nobody anything
                # (rec_delivery; the weekly_email and digest surfaces).
                with rec_delivery.collect() as shown:
                    if len(items) == 1:
                        # The subject carries the week's verdict, the
                        # same sentence as the H1 (density audit #11).
                        from reporter import render_digest
                        _dg = render_digest(first_rep, first_rest.name, owner_name=owner_name,
                                            restaurant_id=first_rest.id, owner_view=True)
                        html, subject = _dg["html"], _dg["subject"]
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
                    shown.flush()
                    _ops.claim_period(f"weekly_digest_to:{key}", sent_period)
                    counts["ok"] += 1
                    log.info(f"Digest sent to {bucket['to']} covering {len(items)} location(s)")
                else:
                    counts["failed"] += 1
                    log.error(f"Digest send to {bucket['to']} failed: {result.error}")
                    _ops.capture(RuntimeError(result.error or "digest send failed"),
                                 job="weekly_digest", context=f"restaurant_id={first_rest.id}")
                    # The day was claimed before sending (local_due), so a
                    # transient Resend failure lost the week's digest
                    # (MOD-EML-8). Give the claims back so the next hourly
                    # tick inside the window tries again; a refusal that will
                    # not change (suppressed, a 4xx) keeps them. One rule for
                    # every sender now: SendResult.transient.
                    if result.transient:
                        for rest, _rep in items:
                            _ops.release_period(f"weekly_digest:{rest.id}",
                                                _rnow(rest, naive=True).date().isoformat())
                # The integration hears "the weekly report went out" only
                # when it did.
                for rest, _rep in (items if getattr(result, "ok", False) else []):
                    try:
                        from webhooks import fire_webhook as _fw_rep
                        _fw_rep(rest.id, "report.weekly", {"restaurant": rest.name, "email": bucket["to"]})
                    except Exception: pass
            except Exception as e:
                log.error(f"Digest failed for {bucket['to']}: {e}")
                _ops.capture(e, job="weekly_digest", context=f"restaurant_id={first_rest.id}")
                counts["failed"] += 1

    except Exception as e:
        # Raised, not swallowed (#39): ops.run_job captures it and records
        # the run failed — it used to be a green run over no digests at all.
        log.error(f"Weekly digest error: {e}")
        raise
    return counts


def _served_client(r):
    """A client this job serves: in service (models.in_service — past due
    included while Stripe retries the card, #155) and not Cavnar AI's own
    internal account. Replaces the hand-kept ('trial', 'active') lists,
    which dropped past-due clients and disagreed with every sync job."""
    from models import in_service
    return in_service(r) and str(getattr(r, "billing_status", "") or "").strip().lower() != "internal"


def _served_client_sql(column="billing_status"):
    """_served_client as a WHERE fragment."""
    from models import in_service_sql
    return f"{in_service_sql(column)} AND LOWER(TRIM(COALESCE({column},''))) <> 'internal'"


def check_stale_inventory():
    """Alert Will when a client's inventory data is more than 7 days old.
    Returns the standard counts; a send that failed raises (#39) — it used
    to be logged and recorded as a clean run."""
    if not _resend_key():
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1, "hit_bound": False}
    stale = find_stale_inventory()
    if not stale:
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "stale": 0}
    _send_stale_inventory(stale)
    return {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False, "stale": len(stale)}


def find_stale_inventory():
    """[(restaurant name, why)] for every Food Cost client whose inventory
    data is stale — the list the Monday email and the operator's weekly
    digest (#35) both carry."""
    stale = []
    try:
        from models import get_all_restaurants
        from datetime import datetime, timedelta

        restaurants = get_all_restaurants()
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
            if not r.module_inventory or not _served_client(r):
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
    except Exception as e:
        log.error(f"Stale inventory check error: {e}")
        _ops.capture(e, job="stale_inventory", context="outer")
        raise
    return stale


def _send_stale_inventory(stale):
    """The Monday stale-inventory email to Will. Raises when it did not go
    (#39): a failed send was logged and the run recorded clean."""
    b = _emails.BRAND
    stale_html = "".join(
        f'<tr><td style="padding:6px 12px;border-bottom:1px solid {b["rule"]}"><strong>{_html.escape(str(name))}</strong></td>'
        f'<td style="padding:6px 12px;border-bottom:1px solid {b["rule"]};color:{b["ember"]}">{_html.escape(str(status))}</td></tr>'
        for name, status in stale)
    res = _emails.deliver(email_type="ops_stale_inventory", payload={
        "from": _emails.sender("client"),
        "to": [config.will_email()],
        "subject": f"⚠ Stale inventory data — {len(stale)} client(s) need updating",
        "html": _emails._branded_email(f"""
  <p style="font-size:11px;color:{b['muted']};margin:0 0 12px;letter-spacing:1px;text-transform:uppercase">Weekly Inventory Check</p>
  <p style="font-size:14px;line-height:1.6;margin:0 0 16px;color:{b['body']}">
    The following clients have inventory data that needs updating.
    Follow up to get a fresh CSV export from them this week.
  </p>
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid {b['border']}">
    <thead><tr style="background:{b['paper']}">
      <th style="padding:8px 12px;text-align:left;font-size:11px;color:{b['muted']};font-weight:600">RESTAURANT</th>
      <th style="padding:8px 12px;text-align:left;font-size:11px;color:{b['muted']};font-weight:600">STATUS</th>
    </tr></thead>
    <tbody>{stale_html}</tbody>
  </table>
  <p style="font-size:12px;color:{b['muted']};margin-top:16px">
    Update inventory data at <a href="https://dashboard.cavnar.ai/admin" style="color:{b['ember']}">dashboard.cavnar.ai/admin</a>
    → client → Manage Data. The same list is in Monday's operator digest.
  </p>"""),
    })
    if not getattr(res, "ok", bool(res)):
        raise RuntimeError(f"stale inventory email not sent: {getattr(res, 'error', None) or 'not accepted'}")
    log.info(f"Stale inventory alert sent for {len(stale)} client(s)")


def run_toast_sync():
    """
    Nightly POS sync (3am CT): pull fresh shift data for every connected
    restaurant into client_data.shifts_csv so the Labor module stays current.
    Dispatches through the pos.py registry — this used to be Toast-only,
    which silently skipped Square and Clover clients every night.
    """
    try:
        import pos
        stats = {}
        results = pos.sync_all(stats=stats)
        if results:
            ok = sum(1 for r in results if r.get("ok"))
            log.info(f"POS nightly sync: {ok}/{len(results)} restaurants OK")
        run_inventory_sync()
        # The counts reach job_runs (ops.run_outcome): a night on which every
        # POS failed is a failed run, some failing a partial one (DH2-1).
        return stats
    except Exception as e:
        log.error(f"run_toast_sync error: {e}")
        _ops.capture(e, job="pos_sync", context="outer")
        raise


def run_pos_sync_one(restaurant_id, trigger="manual"):
    """One restaurant's POS sync through pos.sync_restaurant — the path the
    nightly sweep takes, so the attempt lands in the Data Health ledger and
    the data_through date moves (#65). Standard counts."""
    import pos
    result = pos.sync_restaurant(restaurant_id, trigger=trigger) or {}
    ok = bool(result.get("ok"))
    return {"attempted": 1, "ok": 1 if ok else 0, "failed": 0 if ok else 1, "skipped": 0, "hit_bound": False,
            "provider": result.get("provider"), "error": None if ok else result.get("error")}


def start_manual_pos_sync(restaurant_id, actor="admin"):
    """The manual "Sync now" for a POS — the console's Toast and RPOWER
    buttons and the owner's account card (#65, #153): through
    pos.sync_restaurant on the bounded admin pool, not a direct
    sync_to_db on an unbounded thread per click, which skipped the Data
    Health ledger so the issue that prompted the click never cleared.
    (job_id, joined); the console polls GET /admin/api/tasks/<job_id>."""
    return _ops.run_admin_task("pos_sync_one", restaurant_id, "pos_sync_one", run_pos_sync_one, restaurant_id,
                               context=f"restaurant_id={restaurant_id} manual by {actor}")


def run_inventory_sync():
    """Nightly, after the POS: every restaurant with an inventory system
    connected (inventory_sync.py; Back Office first) has its items, prices
    and counts pulled into Food Cost's tables, bounded and resumable like
    run_daily_fetch. Nothing connected is a no-op. Never raises into the
    POS job - its own failures are captured under inventory_sync."""
    try:
        import inventory_sync
        ids = inventory_sync.connected_ids()
        if not ids:
            return 0
        done, hit = resumable_sweep(inventory_sync.SYNC_CURSOR_KEY, ids, inventory_sync.sync_restaurant,
                                    inventory_sync.SYNC_MAX_SECONDS, job="inventory_sync")
        log.info(f"Inventory sync: {done}/{len(ids)} restaurants{' (hit the time bound)' if hit else ''}")
        return done
    except Exception as e:
        log.error(f"run_inventory_sync error: {e}")
        _ops.capture(e, job="inventory_sync", context="outer")
        return 0


# The automatic-recovery pass (DH5-7): restaurants whose POS sync failed and
# whose retry is due (data_health.due_retries — never an auth failure) are
# retried hourly, until this local hour. Three retries a day at most, at +1h,
# +3h and +6h after each failure (pos.RETRY_DELAYS_HOURS).
POS_RETRY_UNTIL_LOCAL_HOUR = 11
POS_RETRY_MAX_SECONDS = int(os.getenv("POS_RETRY_MAX_SECONDS", str(15 * 60)))


def run_pos_retry():
    """Hourly: retry every POS sync whose retry is due, bounded and
    resumable, before POS_RETRY_UNTIL_LOCAL_HOUR in the restaurant's own
    zone. Returns the sweep counts for job_runs, hit_bound included."""
    import data_health
    import pos
    from models import get_restaurant, in_service
    from time_utils import restaurant_now
    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
    lock = threading.Lock()
    due = data_health.due_retries("pos")

    def _one(rid):
        r = get_restaurant(rid)
        local = restaurant_now(r, naive=True) if r is not None else None
        if r is None or not in_service(r) or local.hour >= POS_RETRY_UNTIL_LOCAL_HOUR:
            with lock:
                counts["skipped"] += 1
            return
        attempt = next((n for n in range(1, len(pos.RETRY_DELAYS_HOURS) + 1)
                        if _ops.claim_period(f"pos_retry_attempt:{rid}", f"{local.date().isoformat()}#{n}")), None)
        if attempt is None:
            with lock:
                counts["skipped"] += 1
            return
        result = pos.sync_restaurant(rid, trigger="retry", attempt=attempt)
        with lock:
            counts["attempted"] += 1
            counts["ok" if result.get("ok") else "failed"] += 1

    _done, hit_bound = resumable_sweep("pos_retry_cursor", due, _one, POS_RETRY_MAX_SECONDS,
                                       workers=SWEEP_WORKERS, job="pos_retry")
    if hit_bound:
        _ops.capture(RuntimeError(f"POS retry pass stopped at its {POS_RETRY_MAX_SECONDS}s bound; "
                                  "the rest lead the next pass"), job="pos_retry", context="time_bound")
    counts["hit_bound"] = bool(hit_bound)
    return counts


# The depletion catch-up (DH2-4): from the last depleted business date —
# never fewer than DEPLETION_OVERLAP_DAYS back, for late edits and voids —
# and never more than DEPLETION_CATCHUP_MAX_DAYS, so an outage over a long
# weekend is backfilled once the POS answers again. A fixed three days left
# anything older permanently missing.
DEPLETION_OVERLAP_DAYS = 2
DEPLETION_CATCHUP_MAX_DAYS = 14


def _depletion_start(restaurant_id, end):
    from datetime import date as _date, timedelta as _td
    start = end - _td(days=DEPLETION_OVERLAP_DAYS)
    last = None
    try:
        import data_health
        last = (data_health.health_rows(restaurant_id).get("depletion") or {}).get("data_through")
        if not last:
            from models import get_conn as _gc
            c = _gc()
            try:
                row = c.execute("SELECT MAX(event_date) FROM ingredient_stock_events WHERE restaurant_id=? "
                                "AND event_type='depletion'", (restaurant_id,)).fetchone()
                last = row[0] if row else None
            finally:
                c.close()
        if last:
            start = min(start, _date.fromisoformat(str(last)[:10]))
    except Exception as e:
        log.warning(f"depletion catch-up start unreadable for {restaurant_id}: {e}")
    return max(start, end - _td(days=DEPLETION_CATCHUP_MAX_DAYS))


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
        from models import get_all_restaurants, get_conn as _gc, in_service

        conn = _gc()
        restaurants_with_recipes = {
            row["restaurant_id"] for row in conn.execute(
                "SELECT DISTINCT mi.restaurant_id FROM recipe_ingredients ri "
                "JOIN menu_items mi ON mi.id = ri.menu_item_id"
            ).fetchall()
        }
        conn.close()

        counts = {"ok": 0, "total": 0, "failed": 0, "skipped": 0}
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
            if not in_service(r):
                continue
            by_id[r.id] = r

        def _one(rid):
            r = by_id[rid]
            import time as _time
            import data_health
            t0 = _time.monotonic()
            provider = None
            try:
                # A POS that cannot report item-level sales is skipped
                # explicitly rather than erroring per restaurant every night:
                # Square and Clover report daily totals but no line items, so
                # there is nothing to deplete from and that is a fact about
                # the integration, not a failure.
                if not pos.supports(r.id, "fetch_order_selections"):
                    counts["skipped"] += 1
                    return
                counts["total"] += 1
                end = _chi_now().date()
                # Since the last depleted business date, capped (DH2-4) —
                # idempotent, so the overlap re-syncs late edits for free.
                start = _depletion_start(r.id, end)
                business_dates, provider = pos.fetch_business_days(r.id, start, end)
                for bd_str in sorted(business_dates):
                    result = inventory_ledger.compute_daily_depletion(r.id, __import__('datetime').date.fromisoformat(bd_str))
                    if result.get("unmapped_selections"):
                        log.warning(f"[inventory_depletion] {r.name}: "
                                   f"{len(result['unmapped_selections'])} unmapped selection(s) on {bd_str}")
                counts["ok"] += 1
                # Per restaurant (DH2-4): depleted through the last business
                # day that had ENDED — the source data_freshness dates, and
                # what the snapshot and trusted orders check before trusting
                # stock.
                data_health.record_attempt(r.id, "depletion", True, provider=provider,
                                           data_through=pos.complete_through(r),
                                           duration_ms=int((_time.monotonic() - t0) * 1000))
            except Exception as e:
                counts["failed"] += 1
                log.warning(f"[inventory_depletion] {r.name} failed: {e}")
                ops.capture(e, job="inventory_depletion", context=f"restaurant_id={r.id} {r.name}")
                data_health.record_attempt(r.id, "depletion", False, provider=provider, error=str(e),
                                           duration_ms=int((_time.monotonic() - t0) * 1000))

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
        return {"attempted": counts["total"], "ok": counts["ok"], "failed": counts["failed"],
                "skipped": counts["skipped"], "hit_bound": bool(ran_out)}
    except Exception as e:
        log.error(f"run_daily_depletion_sync error: {e}")
        _ops.capture(e, job="inventory_depletion", context="outer")
        raise


# Graph calls a restaurant's token refresh may make in one day. The job is
# attempted hourly from 7am; a restaurant whose refresh succeeded is no longer
# expiring and drops out, one whose refresh failed (a timeout, a Graph 5xx)
# is tried again the next hour rather than the next day (DATA-49).
TOKEN_REFRESH_ATTEMPTS_PER_DAY = 3


def refresh_ig_token(r) -> dict:
    """Refresh one restaurant's Instagram (and Facebook page) token with
    Meta: {"ok", "expires", "error"}. The ONE refresh — the nightly job and
    the console's "Refresh IG token" button both call it (#65). The button
    had its own copy that pushed the expiry 60 days out even when Meta
    returned no new token, reintroducing MOD-A6-oauth-5: only a token Meta
    actually handed back moves the expiry. Never raises; a Meta refusal is
    captured once here."""
    import requests as _req
    from datetime import timedelta
    from models import update_restaurant
    from meta_api import graph_url
    app_id = os.getenv("META_APP_ID", "")
    app_secret = os.getenv("META_APP_SECRET", "")
    if not app_id or not app_secret:
        return {"ok": False, "error": "META_APP_ID / META_APP_SECRET are not set"}
    if not getattr(r, "ig_token", None):
        return {"ok": False, "error": "No Instagram token to refresh"}
    try:
        # Timed (MOD-MKT-2): this runs on the one scheduler thread, and a
        # black-holed Graph connection stopped every job with it.
        resp = _req.get(graph_url("oauth/access_token"), params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id, "client_secret": app_secret,
            "fb_exchange_token": r.ig_token,
        }, timeout=(5, 20))
        try:
            new_token = (resp.json() or {}).get("access_token") if resp.status_code == 200 else None
        except Exception:
            new_token = None
        if not new_token:
            log.warning(f"Token refresh failed for {r.name}: {resp.status_code} {(resp.text or '')[:100]}")
            _ops.capture(RuntimeError(f"Meta token refresh returned {resp.status_code}"),
                         job="refresh_tokens", context=f"restaurant_id={r.id}")
            return {"ok": False, "error": f"Meta returned {resp.status_code} with no new token"}
        # Only a token Meta actually handed back moves the expiry. A 200
        # with no access_token used to keep the old token and still push its
        # expiry 60 days out, so the job stopped trying while the real token
        # died (MOD-A6-oauth-5).
        new_expires = (_chi_now() + timedelta(days=60)).strftime("%Y-%m-%d")
        update_data = {"ig_token": new_token, "ig_token_expires": new_expires}
        if getattr(r, "fb_page_token", None):
            resp2 = _req.get(graph_url("oauth/access_token"), params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id, "client_secret": app_secret,
                "fb_exchange_token": r.fb_page_token,
            }, timeout=(5, 20))
            try:
                fb_token = (resp2.json() or {}).get("access_token") if resp2.status_code == 200 else None
            except Exception:
                fb_token = None
            if fb_token:
                update_data["fb_page_token"] = fb_token
                update_data["fb_token_expires"] = new_expires
        update_restaurant(r.id, update_data)
        log.info(f"Refreshed IG/FB tokens for {r.name}, new expiry {new_expires}")
        return {"ok": True, "expires": new_expires}
    except Exception as e:
        log.error(f"Token refresh error for {r.name}: {e}")
        _ops.capture(e, job="refresh_tokens", context=f"restaurant_id={r.id}")
        return {"ok": False, "error": str(e)[:200]}


TOKEN_REFRESH_MAX_SECONDS = int(os.getenv("TOKEN_REFRESH_MAX_SECONDS", str(10 * 60)))


def refresh_expiring_tokens():
    """Refresh Instagram and Facebook tokens expiring within 7 days, one
    restaurant at a time through refresh_ig_token — bounded and resumable
    (#84): it looped every restaurant serially with no bound or cursor.
    Returns the standard counts."""
    from datetime import timedelta
    from models import get_all_restaurants
    if not os.getenv("META_APP_ID", "") or not os.getenv("META_APP_SECRET", ""):
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    try:
        today = _chi_now().date().isoformat()
        soon = (_chi_now() + timedelta(days=7)).strftime("%Y-%m-%d")
        due = {r.id: r for r in get_all_restaurants()
               if r.ig_token and (r.ig_token_expires or "2000-01-01") <= soon}
    except Exception as e:
        log.error(f"refresh_expiring_tokens error: {e}")
        _ops.capture(e, job="refresh_tokens", context="outer")
        raise
    c = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
    lock = threading.Lock()

    def _one(rid):
        r = due[rid]
        if not any(_ops.claim_period(f"refresh_tokens_attempt:{r.id}", f"{today}#{n}")
                   for n in range(TOKEN_REFRESH_ATTEMPTS_PER_DAY)):
            with lock:
                c["skipped"] += 1        # tried enough today
            return
        out = refresh_ig_token(r)
        with lock:
            c["attempted"] += 1
            c["ok" if out.get("ok") else "failed"] += 1

    _done, ran_out = resumable_sweep("refresh_tokens_cursor", sorted(due), _one, TOKEN_REFRESH_MAX_SECONDS,
                                     workers=1, job="refresh_tokens")
    c["hit_bound"] = bool(ran_out)
    return c


# One nightly metrics pass stops taking on restaurants after this long; the
# job_cursors cursor makes the next night start where it stopped.
METRICS_SYNC_SECONDS = int(os.getenv("METRICS_SYNC_SECONDS", "1800"))


def record_metrics_sync(restaurant_id, ok, error=None):
    """Stamp one restaurant's Meta metrics sync in job_cursors
    (`metrics_sync:<rid>` → JSON {last_attempt_at, last_ok_at, error}), so a
    freshness reading can say how old the marketing figures are instead of
    calling them fresh whenever a token exists. Never raises."""
    import json as _json
    from models import get_conn
    from time_utils import utc_stamp
    key = f"metrics_sync:{int(restaurant_id)}"
    try:
        conn = get_conn()
        try:
            row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
            try:
                state = _json.loads(row["value"]) if row and row["value"] else {}
            except ValueError:
                state = {}
            now = utc_stamp()
            state["last_attempt_at"] = now
            if ok:
                state["last_ok_at"], state["error"] = now, None
            else:
                state["error"] = str(error or "unknown")[:300]
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (key, _json.dumps(state)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"metrics sync stamp for {restaurant_id}: {e}")
    # The Data Health ledger too (DH5-1): metrics_sync_state keeps reading
    # its job_cursors stamp as before, and source_health carries the
    # attempt history, reliability and consecutive failures for `marketing`.
    try:
        import data_health
        data_health.record_attempt(int(restaurant_id), "marketing", bool(ok), provider="meta",
                                   error=None if ok else str(error or "unknown"))
    except Exception as e:
        log.warning(f"metrics sync not recorded for {restaurant_id}: {e}")


def metrics_sync_state(restaurant_id, db_path=None, conn=None) -> dict:
    """{last_attempt_at, last_ok_at, error} — UTC "YYYY-MM-DD HH:MM:SS"
    stamps (time_utils.parse_stamp reads them) — or {} when never synced.
    `conn` reads through a caller's open connection (data_freshness, which
    already holds one for the database it was asked about)."""
    import json as _json
    from models import get_conn
    try:
        own = conn is None
        c = (get_conn(db_path) if db_path else get_conn()) if own else conn
        try:
            row = c.execute("SELECT value FROM job_cursors WHERE key=?",
                            (f"metrics_sync:{int(restaurant_id)}",)).fetchone()
        finally:
            if own:
                c.close()
        return _json.loads(row["value"]) if row and row["value"] else {}
    except Exception:
        return {}


def run_marketing_metrics_sync():
    """Nightly: refresh Meta post-performance metrics for every restaurant
    with marketing on and a connected account. Previously this only ever ran
    client-side (a 60s poll while someone had the Marketing tab open), so the
    numbers behind the tab's analytics card were stale the moment nobody was
    looking — an owner who checks once a week saw whatever reach/engagement
    happened to be cached from their last visit, not real current totals.

    Bounded and resumable (resumable_sweep): it looped every connected
    restaurant serially with no wall-clock bound and no cursor (MOD-MKT-15)."""
    try:
        from models import get_all_restaurants, in_service
        import social_routes

        candidates = {r.id: r for r in get_all_restaurants()
                      if r.module_marketing and (r.ig_token or r.fb_page_token) and in_service(r)}
        if not candidates:
            return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
        log.info(f"Marketing metrics sync for {len(candidates)} restaurant(s)")
        tally = {"ok": 0, "failed": 0, "partial": 0}
        lock = threading.Lock()

        def _count(key):
            with lock:
                tally[key] += 1

        def _one(rid):
            r = candidates[rid]
            try:
                result = social_routes.refresh_post_metrics(rid) or {}
            except Exception as e:
                from ai_guard import safe_error as _se
                record_metrics_sync(rid, False, _se(e))
                _count("failed")
                raise
            # The pass is green only when every post it owed was measured
            # (MB-7): it used to stamp success whenever the token was alive,
            # so a night where Meta refused every insights call read
            # "Metrics synced" over posts showing 0. A partial or failed
            # pass is recorded as not ok, with how much it missed.
            status = result.get("status") or ("ok" if result.get("ok") else "failed")
            if status == "ok":
                log.info(f"Metrics synced for {r.name} — {len(result.get('posts', []))} posts")
                record_metrics_sync(rid, True)
                _count("ok")
            elif status == "partial":
                log.warning(f"Metrics sync partial for {r.name}: {result.get('error')}")
                record_metrics_sync(rid, False, result.get("error") or "some posts couldn't be measured")
                # Recorded not-ok in the restaurant's own ledger above, so it
                # is a failed restaurant here too; `partial` says how many.
                _count("failed")
                _count("partial")
            else:
                _count("failed")
                # Was only logged, so marketing figures read current after
                # the Meta token died (CA3 F15). Stamped per restaurant for
                # the freshness registry. Not captured again here: the insights
                # call already raised one operator signal for this restaurant
                # (MOD-MKT-15 — one per restaurant, not one per call).
                log.warning(f"Metrics sync skipped for {r.name}: {result.get('error')}")
                record_metrics_sync(rid, False, result.get("error"))

        done, hit_bound = resumable_sweep("marketing_metrics_sync", list(candidates), _one,
                                          METRICS_SYNC_SECONDS, workers=1, job="marketing_metrics_sync")
        if hit_bound:
            log.info(f"Marketing metrics sync stopped at its bound after {done}; "
                     "the next pass resumes from there")
            _ops.capture(RuntimeError(f"Marketing metrics sync stopped at its {METRICS_SYNC_SECONDS}s bound "
                                      f"after {done}"), job="marketing_metrics_sync", context="time_bound")
        # Each restaurant's own verdict (#39): a night on which Meta refused
        # every insights call returned {attempted, hit_bound} and read green.
        return {"attempted": tally["ok"] + tally["failed"], "ok": tally["ok"], "failed": tally["failed"],
                "skipped": 0, "partial": tally["partial"], "hit_bound": bool(hit_bound)}
    except Exception as e:
        log.error(f"run_marketing_metrics_sync error: {e}")
        _ops.capture(e, job="marketing_metrics_sync", context="outer")
        raise


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
        if claim_key is not None and local.hour >= until:
            note_missed_window(claim_key, getattr(restaurant, "id", restaurant), local.date().isoformat())
        return False
    if claim_key is None:
        return True
    return _ops.claim_period(f"{claim_key}:{getattr(restaurant, 'id', restaurant)}",
                             local.date().isoformat())


def note_missed_window(job, restaurant_id, local_date):
    """Record that `job`'s window for this restaurant closed on `local_date`
    with nothing claimed — the job never got its turn while the window was
    open (a long pass held the loop, an outage, a deploy). Nothing recorded
    "window closed, not sent" before (#131); the console and the digest
    read ops.missed_windows. A read first, so the usual answer — it ran —
    costs no write. Never raises."""
    try:
        if _ops.period_claimed(f"{job}:{restaurant_id}", local_date):
            return False
        return _ops.record_missed_window(job, restaurant_id, local_date)
    except Exception as e:
        log.warning(f"missed window not noted ({job}, {restaurant_id}): {e}")
        return False


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

# What must never leave the server in a backup artifact is ONE registry now,
# offsite_backup.SCRUB_TABLES / SCRUB_COLUMNS / KEEP_COLUMNS, built from
# credentials.FIELDS and every credential-looking column (#102). The six
# hand-named columns that stood here let the POS, reservation and webhook
# credentials and every staff-portal link ride in the "stripped" copy.

# A backup needs this many times the database's size free before it starts:
# the snapshot, the scrubbed copy and its encryption (~1.33x) exist at once
# (#28). A 2am run that filled the volume would take its own snapshot down
# and every write with it.
BACKUP_FREE_SPACE_FACTOR = float(os.getenv("BACKUP_FREE_SPACE_FACTOR", "3.5"))


# The emailed copy is skipped, and the skip reported, above this many bytes
# of encrypted file (MOD-PERF-7). Resend caps a message at 40 MB and the
# attachment travels base64'd, so ~25 MB is the most that reliably arrives;
# past it the only off-volume copy has to live somewhere other than email.
BACKUP_EMAIL_MAX_BYTES = int(os.getenv("BACKUP_EMAIL_MAX_BYTES", str(25 * 1024 * 1024)))
_BACKUP_CHUNK = 3 * 1024 * 1024           # a multiple of 3, so base64 chunks join cleanly


def _encrypt_file_chunked(src_path, dest_path, key):
    """Encrypt src to dest one chunk at a time: one Fernet token per line.

    The old copy was Fernet(key).encrypt(f.read()) — the whole database
    read, then encrypted, then base64'd, three to four times its size in
    the web process's memory every night (DATA-18). A file of one token
    (the old format) decrypts with the same loop (docs/ops/RECOVERY.md)."""
    from cryptography.fernet import Fernet
    fernet = Fernet(key.encode())
    with open(src_path, "rb") as src, open(dest_path, "wb") as dst:
        while True:
            chunk = src.read(_BACKUP_CHUNK)
            if not chunk:
                break
            dst.write(fernet.encrypt(chunk) + b"\n")


def _base64_file(path):
    import base64
    parts = []
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_BACKUP_CHUNK)
            if not chunk:
                break
            parts.append(base64.b64encode(chunk).decode())
    return "".join(parts)


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
    # Written under a temporary name and renamed into place only once it
    # passes its integrity check. A snapshot that failed the check used to
    # stay on disk under today's name, so "the newest snapshot" — what a
    # restore and the restore drill pick — was the corrupt one (DATA-34).
    partial = dest_path + ".partial"
    try:
        src = sqlite3.connect(DB_PATH, timeout=30)
        try:
            dst = sqlite3.connect(partial)
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
        os.replace(partial, dest_path)
    finally:
        for leftover in (partial, partial + "-journal", partial + "-wal", partial + "-shm"):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass


def _redact_snapshot(path):
    """Strip credentials from a COPY of a snapshot before it leaves the
    server (offsite_backup.redact, the one scrub registry)."""
    import offsite_backup
    return offsite_backup.redact(path)


def _prune_old_backups(backup_dir):
    import glob
    cutoff = time.time() - (BACKUP_RETAIN_DAYS * 86400)
    for old in glob.glob(os.path.join(backup_dir, "cavnar_ai_backup_*.db")):
        try:
            if os.path.getmtime(old) < cutoff:
                os.unlink(old)
        except OSError:
            pass


class BackupFailed(RuntimeError):
    """The nightly backup did not produce what it must: a good local
    snapshot AND at least one off-site copy (decision 7)."""


def _dir_bytes(path, pattern="cavnar_ai_backup_*.db"):
    import glob
    total = 0
    for p in glob.glob(os.path.join(path, pattern)):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def _backup_failed(run, message, page=True):
    """Record the failed run, page Will once a day, tell the external
    monitor, and raise — the run is a failed job_runs row, not ok=1 (#2)."""
    import json as _json
    run["finished_at"] = _now_utc_stamp()
    run.setdefault("local_ok", 0)
    run.setdefault("offsite_ok", 0)
    detail = dict(run.pop("_detail", {}) or {})
    detail["error"] = str(message)[:300]
    run["detail_json"] = _json.dumps(detail, default=str)[:2000]
    _ops.record_backup_run(run)
    if page:
        try:
            _ops.page_operator("backup_failed", "Cavnar AI: last night's backup failed", [str(message)],
                               cooldown_minutes=12 * 60)
        except Exception as e:
            log.error(f"backup page failed: {e}")
    _ops.ping_healthcheck("fail")
    raise BackupFailed(message)


def _now_utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _scrub_lines_html(scrubbed):
    """The backup email's account of what left the copy — from what
    offsite_backup.redact actually did (offsite_backup.describe_scrub), not
    a fixed sentence (#102)."""
    import offsite_backup
    b = _emails.BRAND
    d = offsite_backup.describe_scrub(scrubbed)
    esc = lambda items: _html.escape(", ".join(items)) if items else "none"  # noqa: E731
    lines = [f"<b>Emptied</b> ({len(d['emptied'])} tables): {esc(d['emptied'])}.",
             f"<b>Blanked</b> ({len(d['blanked'])} credential columns): {esc(d['blanked'])}."]
    if d["precaution"]:
        lines.append(f"<b>Also blanked, not yet classified</b>: {esc(d['precaution'])} — add each to "
                     "offsite_backup.SCRUB_COLUMNS or KEEP_COLUMNS.")
    if d.get("removed_rows"):
        lines.append(f"<b>Guest text of removed rows</b>: {esc(d['removed_rows'])}.")
    lines.append(f"<b>Kept on purpose</b>: {_html.escape(d['kept'])}.")
    return "".join(f'<p style="font-size:12px;color:{b["muted"]};margin:0 0 6px">{l}</p>' for l in lines)


def _email_offsite(enc_path, enc_name, timestamp, size_kb, scrubbed=None):
    """The encrypted email copy (the second off-site path). Returns None on
    success, or why it did not go. `scrubbed` is what redact() took out of
    the copy; the email says exactly that."""
    if not _resend_key():
        return "RESEND_API_KEY is not set"
    enc_bytes = os.path.getsize(enc_path)
    if enc_bytes > BACKUP_EMAIL_MAX_BYTES:
        # Too big to arrive as an attachment, and building it means holding
        # it in memory. Said out loud rather than attempted.
        return (f"encrypted backup is {size_kb} KB, over BACKUP_EMAIL_MAX_BYTES ({BACKUP_EMAIL_MAX_BYTES} bytes) — "
                "too large to email")
    b = _emails.BRAND
    res = _emails.deliver(email_type="ops_backup", payload={
        "from": _emails.sender("ops"),
        "to": [config.will_email()],
        "subject": f"Daily DB backup — {timestamp} ({size_kb} KB, encrypted)",
        "html": _emails._branded_email(f"""
  <p style="font-size:14px;color:{b['strong']};margin:0 0 12px">Encrypted daily backup attached.</p>
  <table style="font-size:13px;color:{b['body']};border-collapse:collapse">
    <tr><td style="padding:3px 12px 3px 0;color:{b['muted']}">Date</td><td>{_html.escape(timestamp)}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:{b['muted']}">File</td><td>{_html.escape(enc_name)}</td></tr>
    <tr><td style="padding:3px 12px 3px 0;color:{b['muted']}">Size</td><td>{size_kb} KB</td></tr>
  </table>
  <p style="font-size:12px;color:{b['muted']};margin:16px 0 6px">What was stripped from this copy:</p>
  {_scrub_lines_html(scrubbed)}
  <p style="font-size:12px;color:{b['muted']};margin-top:10px">
    Decrypt with BACKUP_ENCRYPTION_KEY, then rename to reviews.db (docs/ops/RECOVERY.md).
  </p>"""),
        "attachments": [{"filename": enc_name, "content": _base64_file(enc_path)}],
    })
    if not getattr(res, "ok", False):
        return f"email not sent: {getattr(res, 'error', None) or 'not accepted'}"
    return None


def backup_db():
    """Daily 2am backup: a local snapshot, then the off-site copies.

    1. Free space first (#28): at least BACKUP_FREE_SPACE_FACTOR times the
       database (and its WAL) must be free, or the run fails before it
       writes anything.
    2. The snapshot is taken with sqlite3's online backup API and
       integrity-checked, not shutil.copy2 — see _write_consistent_snapshot.
       The LOCAL snapshot is NOT redacted: it is the restore artifact.
    3. Off-site (#1, decision 7): a scrubbed COPY (offsite_backup.redact —
       every credential out), encrypted with BACKUP_ENCRYPTION_KEY, goes to
       object storage (BACKUP_S3_*, SigV4, checksummed) and, when it fits,
       by email. The old version base64'd the entire live database into an
       email every night: every restaurant's financials, guest phone
       numbers, and a working bearer token for every logged-in owner.

    The run FAILS — raises, so job_runs records it failed, pages Will and
    pings the external monitor's /fail — when the snapshot fails, when there
    is not room for it, and when NO off-site copy was made (#1, #2): a
    missing key used to log a warning and record a clean run, while every
    snapshot sat on the one volume that could be lost. One backup_runs row
    per run either way (ops.backup_status reads it), with the database, WAL
    and backup sizes for the trend (#28). Returns the standard counts over
    the copies it made.
    """
    import json as _json
    import offsite_backup
    from models import DB_PATH

    timestamp = _chi_now().strftime("%Y-%m-%d")
    filename = f"cavnar_ai_backup_{timestamp}.db"
    backup_dir = os.getenv("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)) or ".", "backups")
    run = {"started_at": _now_utc_stamp(), "_detail": {}}
    try:
        run["db_bytes"] = os.path.getsize(DB_PATH)
    except OSError:
        run["db_bytes"] = None
    try:
        run["wal_bytes"] = os.path.getsize(DB_PATH + "-wal")
    except OSError:
        run["wal_bytes"] = 0

    try:
        os.makedirs(backup_dir, exist_ok=True)
        import shutil as _sh
        free = _sh.disk_usage(backup_dir).free
        run["free_bytes"] = free
        need = int(BACKUP_FREE_SPACE_FACTOR * ((run["db_bytes"] or 0) + (run["wal_bytes"] or 0)))
        if free < need:
            _backup_failed(run, f"not enough free space to back up: {free // (1024 * 1024)} MB free, "
                                f"{need // (1024 * 1024)} MB needed ({BACKUP_FREE_SPACE_FACTOR:g}x the database)")
    except BackupFailed:
        raise
    except Exception as e:
        log.warning(f"backup_db: free-space check unavailable: {e}")

    try:
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
        run.update(local_ok=1, integrity_ok=1, local_path=local_path, size_bytes=os.path.getsize(local_path))
        log.info(f"backup_db: local snapshot {local_path} ({size_kb} KB)")
        _prune_old_backups(backup_dir)
        run["backups_bytes"] = _dir_bytes(backup_dir)
    except Exception as e:
        log.error(f"backup_db: snapshot failed: {e}")
        try:
            _ops.capture(e, job="backup_db", context="snapshot")
        except Exception:
            pass
        run.update(local_ok=0, integrity_ok=0 if "integrity" in str(e) else None)
        _backup_failed(run, f"the snapshot failed: {e}")

    cfg = {"s3": offsite_backup.s3_config() is not None, "email": bool(_resend_key())}
    key = os.getenv("BACKUP_ENCRYPTION_KEY", "").strip()
    if not key:
        _backup_failed(run, "BACKUP_ENCRYPTION_KEY is not set, so no copy left the server: every snapshot is on "
                            "the one volume that could be lost. Generate a key with `python3 -c \"from "
                            "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`, set it "
                            "in Railway AND keep a copy off Railway, and set BACKUP_S3_* for object storage.")
    if not cfg.get("s3") and not cfg.get("email"):
        _backup_failed(run, "no off-site copy is configured: set BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET, "
                            "BACKUP_S3_ACCESS_KEY_ID and BACKUP_S3_SECRET_ACCESS_KEY (or RESEND_API_KEY for the "
                            "emailed copy)")

    redacted_path = local_path + ".redacted"
    enc_path = local_path + ".enc"
    enc_name = filename + ".enc"
    targets, errors = [], []
    try:
        import shutil as _shutil
        # Redact a COPY. The off-site copies are the artifacts that leave the
        # server; the local snapshot stays whole so a restore is a restore.
        _shutil.copy2(local_path, redacted_path)
        scrubbed = _redact_snapshot(redacted_path) or {}
        run["_detail"]["scrubbed_unclassified"] = scrubbed.get("unclassified") or []
        run["_detail"]["scrubbed_tables"] = scrubbed.get("tables") or []
        run["_detail"]["scrubbed_columns"] = len(scrubbed.get("columns") or [])
        _encrypt_file_chunked(redacted_path, enc_path, key)
        enc_bytes = os.path.getsize(enc_path)
        size_kb = round(enc_bytes / 1024, 1)
        digest = offsite_backup.sha256_file(enc_path)
        run["sha256"] = digest
        run["_detail"]["encrypted_bytes"] = enc_bytes

        if cfg.get("s3"):
            try:
                up = offsite_backup.upload_file(enc_path, enc_name, sha256_hex=digest)
                targets.append(up["target"])
                log.info(f"backup_db: uploaded {up['target']} ({size_kb} KB, sha256 {digest[:12]}…)")
            except Exception as e:
                errors.append(f"object storage: {e}")
                log.error(f"backup_db: object-storage copy failed: {e}")
                _ops.capture(e, job="backup_db", context="s3")
        if cfg.get("email"):
            why = _email_offsite(enc_path, enc_name, timestamp, size_kb, scrubbed=scrubbed)
            if why is None:
                targets.append("email")
                log.info(f"backup_db: emailed encrypted {enc_name} ({size_kb} KB)")
            else:
                errors.append(f"email: {why}")
                log.error(f"backup_db: email copy failed: {why}")
                _ops.capture(RuntimeError(why), job="backup_db", context="email")
    except BackupFailed:
        raise
    except Exception as e:
        errors.append(f"preparing the off-site copy: {e}")
        log.error(f"backup_db: off-site copy failed (local snapshot is intact): {e}")
        _ops.capture(e, job="backup_db", context="offsite")
    finally:
        # The redacted copy exists only to be encrypted and sent. Leaving it
        # on the volume would double the backup directory's size and put a
        # second, restore-useless file next to every real snapshot.
        for _tmp in (redacted_path, enc_path):
            try:
                if os.path.exists(_tmp):
                    os.unlink(_tmp)
            except OSError as e:
                log.warning(f"backup_db: could not remove {_tmp}: {e}")

    run.update(offsite_ok=1 if targets else 0, offsite_target=", ".join(targets) or None,
               offsite_error="; ".join(errors)[:500] or None)
    if not targets:
        _backup_failed(run, "no off-site copy was made tonight — " + ("; ".join(errors) or "unknown"))
    run["finished_at"] = _now_utc_stamp()
    run["detail_json"] = _json.dumps(run.pop("_detail", {}), default=str)[:2000]
    _ops.record_backup_run(run)
    _ops.ping_healthcheck()
    attempted = 1 + (1 if cfg.get("s3") else 0) + (1 if cfg.get("email") else 0)
    return {"attempted": attempted, "ok": 1 + len(targets), "failed": len(errors), "skipped": 0,
            "hit_bound": False, "offsite": targets, "size_bytes": run.get("size_bytes")}


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


def onboarding_eligible(r):
    """(True, None) when restaurant `r` may be sent onboarding mail, else
    (False, why) — one gate for the day-2/7/30 sequence, the lifecycle
    emails and the step nudges (#20).

    Onboarding starts once the contract is signed (or the client pays) AND
    the welcome email that creates their login was delivered
    (models.onboarding_started_at). It used to key on billing_status and
    created_at alone, so unsigned prospects, demo accounts and every
    location of a group got it, saying "Log in anytime" before any login
    existed. In service means past due keeps it and a paused, churned or
    canceled account does not (#155); internal and pending accounts are
    not clients being onboarded."""
    from models import in_service, is_paying, onboarding_started_at
    if not getattr(r, "owner_email", None):
        return False, "no owner email"
    if int(getattr(r, "is_demo", 0) or 0):
        return False, "demo account"
    status = (getattr(r, "billing_status", None) or "").strip().lower()
    if not in_service(r) or status in ("internal", "pending"):
        return False, f"billing status {status or 'unknown'}"
    if (getattr(r, "contract_status", None) or "").lower() != "signed" and not is_paying(r):
        return False, "contract not signed"
    started = onboarding_started_at(r.id)
    if not started:
        return False, "welcome email not delivered yet"
    return True, None


def _onboarding_days(r):
    """Whole days since this restaurant's onboarding began (UTC), or None."""
    from models import onboarding_started_at
    started = parse_stored_dt(onboarding_started_at(r.id), tz="UTC")
    if started is None:
        return None
    return (datetime.utcnow() - started).days


def _onboarding_outcome(r, key, result, claim_period=None):
    """Record what one onboarding/lifecycle send did (#16).

    ok — marked sent. Transient (a 429, a 5xx, a timeout) — nothing is
    marked and the day's claim is released, so the next hourly tick inside
    the window tries again (the weekly digest's pattern). Refused for good
    (suppressed, a 4xx) — marked failed so it is not retried, and raised to
    the operator. Anything else (no key, no postal address, a build error) —
    raised and left for tomorrow's pass. Returns True when it was sent."""
    from models import mark_onboarding_sent
    if getattr(result, "ok", False):
        mark_onboarding_sent(r.id, key)
        return True
    if getattr(result, "skipped", False):
        mark_onboarding_sent(r.id, key, status="skipped", error=getattr(result, "error", None))
        return False
    err = getattr(result, "error", None) or "not sent"
    if getattr(result, "transient", False):
        log.warning(f"Onboarding {key} for {r.name} failed (will retry): {err}")
        if claim_period:
            _ops.release_period(f"onboarding:{r.id}", claim_period)
        return False
    if getattr(result, "refused", False):
        mark_onboarding_sent(r.id, key, status="failed", error=err)
    # A missing postal address is reported once a day by emails.deliver
    # itself; one capture per restaurant per day on top would bury it.
    if getattr(result, "reason", None) != "no_postal_address":
        _ops.capture(RuntimeError(f"onboarding {key} not sent: {err}"), job="onboarding_email",
                     context=f"restaurant_id={r.id}")
    return False


def run_onboarding_sequence(local_hour: int = None):
    """
    Send each signed, onboarded client the right onboarding email for how
    long ago their onboarding began. Runs at 10am in each restaurant's own
    timezone; each step at most once per restaurant (onboarding_emails), and
    once per OWNER across a group's locations.

    Who: onboarding_eligible — contract signed or paying, the welcome email
    delivered, not a demo, in service (#20, #155). A step is marked done
    only when Resend accepted it (#16); a transient failure releases the
    day's claim for the next tick, a permanent one is recorded and raised.

    Returns the standard counts over the sends (#39): attempted, ok (Resend
    accepted it), failed, skipped (a send the sender declined, a step
    another location already got, a settled client). Restaurants that
    cannot be read raise.
    """
    from models import get_all_restaurants, get_onboarding_sent, mark_onboarding_sent, owner_got_onboarding_step
    from emails import send_onboarding_day2, send_onboarding_day7, send_onboarding_day30

    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}

    def _outcome(r, key, result, period):
        """_onboarding_outcome, counted."""
        sent = _onboarding_outcome(r, key, result, period)
        if sent:
            counts["attempted"] += 1
            counts["ok"] += 1
        elif getattr(result, "skipped", False):
            counts["skipped"] += 1
        else:
            counts["attempted"] += 1
            counts["failed"] += 1
        return sent

    restaurants = get_all_restaurants()

    from time_utils import restaurant_now as _rnow
    for r in restaurants:
        ok, _why = onboarding_eligible(r)
        if not ok:
            continue
        # 10am in the restaurant's own timezone, once a day — the loop
        # attempts this hourly. local_hour=None (a direct call: a test, an
        # admin re-run) is never time-gated, matching notify._gated_out.
        if local_hour is not None and not local_due(r, local_hour, claim_key="onboarding"):
            continue
        period = _rnow(r, naive=True).date().isoformat() if local_hour is not None else None
        # Onboarding day-2/7/30 are all product-tips/marketing content, not
        # transactional — the one flag gates all three from this single
        # early-continue, same as the owner_email guard above.
        _opted_out = bool(getattr(r, "marketing_emails_opt_out", 0))

        # Days since onboarding BEGAN (the welcome email), not since the
        # row was created: a prospect created a month before signing used
        # to get the day-30 check-in on their second day.
        days_since = _onboarding_days(r)
        if days_since is None:
            continue
        already_sent = get_onboarding_sent(r.id)

        # Build module list for context
        modules = []
        if r.module_reviews:  modules.append("Review Intelligence")
        if r.module_labor:    modules.append("Labor Optimizer")
        if r.module_inventory: modules.append("Food Cost Control")
        if r.module_marketing: modules.append("Marketing Autopilot")

        def _covered(key):
            """Another location of this owner already got this step: it is
            theirs, not a second copy (#20)."""
            if owner_got_onboarding_step(r.owner_email, key, exclude_restaurant_id=r.id):
                mark_onboarding_sent(r.id, key, status="covered")
                counts["skipped"] += 1
                return True
            return False

        # Day 2 — a window, not ">= 2". These sends were open-ended, which
        # was harmless only while the whole job was crashing before it could
        # send anything: with the crash fixed and onboarding_emails empty,
        # ">=" would mail a "getting started" note to clients who signed up
        # months ago. An upper bound keeps a late/paused scheduler catching
        # up without ever backfilling stale onboarding at a settled client.
        if _opted_out and days_since < 60:
            continue
        if 2 <= days_since <= 6 and "day_2" not in already_sent:
            if not _covered("day_2"):
                result = send_onboarding_day2(
                    to_email=r.owner_email,
                    restaurant_name=r.name,
                    owner_name=r.owner_name,
                    modules=modules,
                    restaurant_id=r.id,
                )
                if _outcome(r, "day_2", result, period):
                    log.info(f"Onboarding day 2 sent to {r.owner_email} ({r.name})")

        # Day 7 — same windowing rationale as day 2 above.
        elif 7 <= days_since <= 29 and "day_7" not in already_sent:
            # "Here's what you're missing" to someone who signs in every
            # morning is the clearest possible sign nobody reads what they
            # do. Marked done, not deferred: they are past needing it.
            _logins, _days_idle = _onboarding_engagement(r.id)
            if _logins >= ONBOARDING_SETTLED_LOGINS and (_days_idle or 99) <= 7:
                mark_onboarding_sent(r.id, "day_7", status="skipped", error="settled: signs in often")
                counts["skipped"] += 1
                log.info(f"Onboarding day 7 skipped for {r.name} — "
                         f"{_logins} logins, last {_days_idle}d ago")
                continue
            if not _covered("day_7"):
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

                result = send_onboarding_day7(
                    to_email=r.owner_email,
                    restaurant_name=r.name,
                    owner_name=r.owner_name,
                    has_labor=bool(r.module_labor),
                    has_inventory=bool(r.module_inventory),
                    approved_count=approved_count,
                    pending_count=pending_count,
                    restaurant_id=r.id,
                )
                if _outcome(r, "day_7", result, period):
                    log.info(f"Onboarding day 7 sent to {r.owner_email} ({r.name})")

        # Day 30 — same windowing rationale; two weeks of grace, then the
        # onboarding sequence is simply over for that client.
        elif 30 <= days_since <= 44 and "day_30" not in already_sent:
            if not _covered("day_30"):
                result = send_onboarding_day30(
                    to_email=r.owner_email,
                    restaurant_name=r.name,
                    owner_name=r.owner_name,
                    modules=modules,
                    restaurant_id=r.id,
                )
                if _outcome(r, "day_30", result, period):
                    log.info(f"Onboarding day 30 sent to {r.owner_email} ({r.name})")

        # Days 60, 90, 180 — the lifecycle after onboarding. The retention
        # audit found nothing spoke to an owner between the day-30 check-in
        # and a cancellation email. These carry the business review (what is
        # now measurable, the first record window, the six-month ledger), so
        # like the monthly they are gated on monthly_review_enabled and NOT
        # on the marketing opt-out — the day-30 gate above already sent us
        # past that `continue` for opted-out clients, so re-check here. They
        # are about THIS location's own figures, so each location gets its
        # own.
        for _day in (60, 90, 180):
            _key = f"day_{_day}"
            if _day <= days_since <= _day + 14 and _key not in already_sent:
                if not getattr(r, "monthly_review_enabled", 1):
                    mark_onboarding_sent(r.id, _key, status="skipped", error="monthly review switched off")
                    counts["skipped"] += 1
                    break
                from emails import send_lifecycle_email
                result = send_lifecycle_email(_day, to_email=r.owner_email, restaurant_name=r.name,
                                              owner_name=r.owner_name, restaurant_id=r.id)
                if _outcome(r, _key, result, period):
                    log.info(f"Lifecycle day {_day} sent to {r.owner_email} ({r.name})")
                break
    return counts


# ── Step-based onboarding nudges (#41) ──────────────────────────────────────
# The sequence above is time-based: day 2, 7, 30 whatever the owner has or
# hasn't done. These are the other half: one short email per setup step
# still missing — Google not connected, no brand voice, no reply approved
# yet, the app not installed — each at most once, never before the step has
# had a fair chance, and never again once it is done. Same gate as the
# sequence (onboarding_eligible), same per-owner rule, and they honour the
# marketing opt-out like the other onboarding mail. Scheduled by the
# integration wave (jobs_registry); `run_onboarding_nudges(local_hour=11)`.

# (step key, earliest day since onboarding began). Latest is the window's end.
ONBOARDING_NUDGE_STEPS = (("reviews", 2), ("voice", 4), ("respond", 6), ("app", 8))
ONBOARDING_NUDGE_LAST_DAY = 45


def onboarding_missing_steps(r):
    """The setup steps this restaurant has NOT done, in nudge order, each
    {"key", ...facts the email needs}. Read fresh, so a step done since the
    last pass is never nudged. Only steps the owner can act on today."""
    from models import get_conn as _gc
    import os as _os
    missing = []
    conn = _gc()
    try:
        row = conn.execute("SELECT gmb_refresh_token, reviews_live, voice_notes FROM restaurants WHERE id=?",
                           (r.id,)).fetchone()
        if r.module_reviews and row is not None and not (row["gmb_refresh_token"] or row["reviews_live"]):
            missing.append({"key": "reviews"})
        if row is not None and not (row["voice_notes"] or "").strip():
            missing.append({"key": "voice"})
        if r.module_reviews:
            approved = conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                                    "AND response_status IN ('approved','posted')", (r.id,)).fetchone()[0]
            waiting = conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                                   "AND COALESCE(TRIM(draft_response),'')!='' "
                                   "AND response_status NOT IN ('approved','posted','skipped')",
                                   (r.id,)).fetchone()[0]
            # Nothing to approve is not a missing step: there is no draft yet.
            if not approved and waiting:
                missing.append({"key": "respond", "waiting": int(waiting)})
        # The app step only when there is somewhere to send them (never an
        # invented store link).
        app_url = (_os.getenv("IOS_APP_STORE_URL") or "").strip()
        if app_url:
            has_device = conn.execute(
                "SELECT 1 FROM device_tokens d JOIN users u ON u.id=d.user_id WHERE d.restaurant_id=? "
                "AND COALESCE(u.is_admin,0)=0 LIMIT 1", (r.id,)).fetchone()
            if not has_device:
                missing.append({"key": "app", "url": app_url})
    except Exception as e:
        log.warning(f"onboarding steps unreadable for {r.id}: {e}")
    finally:
        conn.close()
    return missing


def run_onboarding_nudges(local_hour: int = None):
    """One nudge per missing setup step (#41). At most one email per
    restaurant per day; each step's nudge at most once (onboarding_emails
    key "nudge_<step>"), marked only when Resend accepted it (#16). Returns
    the standard counts; raises when the restaurants cannot be read (#39)."""
    from models import get_all_restaurants, get_onboarding_sent, mark_onboarding_sent, owner_got_onboarding_step
    from emails import send_onboarding_nudge
    from time_utils import restaurant_now as _rnow
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    restaurants = get_all_restaurants()
    for r in restaurants:
        ok, _why = onboarding_eligible(r)
        if not ok or getattr(r, "marketing_emails_opt_out", 0):
            out["skipped"] += 1
            continue
        days = _onboarding_days(r)
        if days is None or days > ONBOARDING_NUDGE_LAST_DAY:
            out["skipped"] += 1
            continue
        missing = [m for m in onboarding_missing_steps(r)
                   if days >= dict(ONBOARDING_NUDGE_STEPS).get(m["key"], 999)]
        done = set(get_onboarding_sent(r.id))
        todo = [m for m in missing if f"nudge_{m['key']}" not in done]
        if not todo:
            continue
        if local_hour is not None and not local_due(r, local_hour, claim_key="onboarding_nudge"):
            continue
        step = todo[0]
        key = f"nudge_{step['key']}"
        if owner_got_onboarding_step(r.owner_email, key, exclude_restaurant_id=r.id):
            mark_onboarding_sent(r.id, key, status="covered")
            continue
        out["attempted"] += 1
        result = send_onboarding_nudge(step, to_email=r.owner_email, restaurant_name=r.name,
                                       owner_name=r.owner_name, restaurant_id=r.id)
        if _onboarding_outcome(r, key, result, None):
            out["ok"] += 1
            continue
        out["failed"] += 1
        if local_hour is not None and getattr(result, "transient", False):
            _ops.release_period(f"onboarding_nudge:{r.id}", _rnow(r, naive=True).date().isoformat())
    return out


def check_inactive_clients():
    """
    Alert Will when a client hasn't logged in for 14+ days.
    Runs every Monday at 11am. Only checks active/trial clients.
    Returns the standard counts; a failed send raises (#39).
    """
    if not os.getenv("RESEND_API_KEY", ""):
        log.warning("check_inactive_clients: no RESEND_API_KEY — skipping")
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1, "hit_bound": False}
    inactive = find_inactive_clients()
    if not inactive:
        log.info("check_inactive_clients: no inactive clients this week")
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "inactive": 0}
    _send_inactive_clients(inactive)
    return {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False, "inactive": len(inactive)}


def find_inactive_clients():
    """[{name, email, last_login, days}] for trial/active clients nobody has
    signed in to for 14+ days (or ever, three days after signing up) — the
    list the Monday email and the operator's weekly digest (#35) carry."""
    from datetime import timedelta
    from models import get_all_restaurants, get_conn
    restaurants = get_all_restaurants()
    inactive = []
    now = _chi_now()
    cutoff = now - timedelta(days=14)

    for r in restaurants:
        if not _served_client(r):
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
            _ops.capture(e, job="inactive_clients", context=f"restaurant_id={r.id}")
    return inactive


def _send_inactive_clients(inactive):
    """The Monday inactive-clients email to Will. Raises when it did not go."""
    b = _emails.BRAND
    rows_html = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid {b['border']}'><strong>{_html.escape(str(c['name']))}</strong></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid {b['border']}'>{_html.escape(str(c['email'] or ''))}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid {b['border']};color:{b['ember']}'>{_html.escape(str(c['last_login']))}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid {b['border']}'>{c['days']}d ago</td></tr>"
        for c in inactive)
    res = _emails.deliver(email_type="ops_inactive_clients", payload={
        "from": _emails.sender("ops"),
        "to": [config.will_email()],
        "subject": f"👋 {len(inactive)} inactive client{'s' if len(inactive) > 1 else ''} — check in this week",
        "html": _emails._branded_email(f"""
  <p style="font-size:16px;font-weight:700;color:{b['strong']};margin:0">Inactive clients</p>
  <p style="font-size:12px;color:{b['muted']};margin:4px 0 16px">Clients who haven't logged in for 14+ days</p>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="background:{b['paper']}">
      <th style="padding:8px 12px;text-align:left">Client</th><th style="padding:8px 12px;text-align:left">Email</th>
      <th style="padding:8px 12px;text-align:left">Last login</th><th style="padding:8px 12px;text-align:left">Gap</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p style="font-size:13px;color:{b['body']};margin-top:16px;line-height:1.6">
    Worth a quick personal email or text to each of these — early churn usually shows up as disengagement first.
    The same list is in Monday's operator digest.
  </p>
  <p style="font-size:11px;color:{b['muted']}"><a href="https://dashboard.cavnar.ai/admin" style="color:{b['ember']}">Manage clients →</a></p>"""),
    })
    if not getattr(res, "ok", bool(res)):
        raise RuntimeError(f"inactive clients email not sent: {getattr(res, 'error', None) or 'not accepted'}")
    log.info(f"Inactive client alert sent — {len(inactive)} client(s)")


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
    sent = failed = attempted = 0
    now = _chi_now()
    for r in get_all_restaurants():
        if not _served_client(r):
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
            # Counted honestly (re-audit A-23): a review is "new" when it was
            # WRITTEN since they left, not merely fetched (an import of old
            # history is not news); "waiting" is the reply-owed window, not
            # every pending review ever; and "alerts" are the ones that asked
            # for something, not briefs and sign-ins.
            from push import ACTIONABLE_TYPES
            from thresholds import REPLY_OWED_MAX_AGE_DAYS
            written = "substr(COALESCE(NULLIF(review_date,''), fetched_at), 1, 10)"
            drafted = n("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                        f"AND draft_response IS NOT NULL AND fetched_at>=? AND {written} >= substr(?, 1, 10)",
                        r.id, since, since)
            arrived = n("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                        f"AND fetched_at>=? AND {written} >= substr(?, 1, 10)", r.id, since, since)
            waiting = n("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                        f"AND response_status IN ('pending','drafted') AND {written} >= date('now', ?)",
                        r.id, f"-{REPLY_OWED_MAX_AGE_DAYS} days")
            _types = sorted(ACTIONABLE_TYPES)
            alerts = n(f"SELECT COUNT(*) FROM alert_log WHERE restaurant_id=? AND fired_at>=? "
                       f"AND alert_type IN ({','.join('?' * len(_types))})", r.id, since, *_types)
            conn.close()
            if not (drafted or arrived or alerts):
                continue
            if not _ops.claim_period(f"while_away:{r.id}", now.strftime("%Y-%m")):
                continue
            attempted += 1
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
            failed += 1
            _ops.capture(e, job="while_away", context=f"restaurant_id={r.id}")
    return {"sent": sent, "attempted": max(attempted, failed), "ok": max(attempted, failed) - failed,
            "failed": failed, "skipped": 0, "hit_bound": False}


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


def _week_start_utc(now=None):
    """Monday 00:00 Chicago of the current ISO week, as a UTC
    "YYYY-MM-DD HH:MM:SS" — what a source_health last_ok_at is compared to."""
    from datetime import timedelta as _td, timezone as _tz
    now = now or _chi_now()
    monday = (now - _td(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    if monday.tzinfo is None:
        from zoneinfo import ZoneInfo
        monday = monday.replace(tzinfo=ZoneInfo("America/Chicago"))
    return monday.astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ok_this_week(restaurant_id, source, week_start):
    """Whether `source` succeeded for this restaurant since week_start
    (source_health last_ok_at) — the weekly jobs' daily retry skips it."""
    try:
        import data_health
        last = (data_health.health_rows(restaurant_id).get(source) or {}).get("last_ok_at")
        return bool(last) and str(last) >= week_start
    except Exception:
        return False


def _weekly_sweep(job, cursor_key, restaurants, fn):
    """Run `fn(restaurant)` over `restaurants` under WEEKLY_SWEEP_MAX_SECONDS,
    starting after the cursor; returns (processed, hit_bound). `fn` returns
    True for a success and False for a handled failure, or raises.

    Through resumable_sweep (#137): the cursor is saved as each restaurant
    finishes, not once at the end, so a pass a deploy killed and a reclaim
    re-ran does not start from the old cursor and buy the same Places,
    Claude and Perplexity calls again. A restaurant whose run failed still
    advances the cursor: it is retried by the daily retry pass, not first in
    line forever ahead of the ones never reached."""
    by_id = {r.id: r for r in restaurants}
    tally = {"attempted": 0}
    lock = threading.Lock()

    def _one(rid):
        with lock:
            tally["attempted"] += 1
        fn(by_id[rid])

    _done, ran_out = resumable_sweep(cursor_key, list(by_id), _one, WEEKLY_SWEEP_MAX_SECONDS,
                                     workers=1, job=job)
    if ran_out:
        _ops.capture(
            RuntimeError(f"{job} covered {tally['attempted']} of {len(by_id)} restaurants before the "
                         f"{WEEKLY_SWEEP_MAX_SECONDS}s bound; the rest lead the next pass."),
            job=job, context="time_bound")
    return tally["attempted"], ran_out


def _record(restaurant_id, source, ok, error=None, provider=None):
    try:
        import data_health
        data_health.record_attempt(restaurant_id, source, ok, provider=provider, error=error)
    except Exception as e:
        log.warning(f"{source} attempt not recorded for {restaurant_id}: {e}")


def run_weekly_competitor_analysis(retry_only=False):
    """Weekly — competitor analysis for every full-tier client in service.
    Claimed per ISO week with Monday–Wednesday catch-up; `retry_only` is the
    daily retry for restaurants with no success this week (DH2-10). Every
    attempt is recorded (source_health `competitor`)."""
    import competitor
    from models import get_all_restaurants, in_service, is_full_tier
    counts = {"analysed": 0, "failed": 0}
    week_start = _week_start_utc()

    def _analyse(r):
        try:
            # Looked up at call time so a test's (or a hot patch's) swap of
            # competitor.run_competitor_analysis is honoured.
            res = competitor.run_competitor_analysis(r.id) or {}
        except Exception as e:
            counts["failed"] += 1
            _record(r.id, "competitor", False, error=str(e), provider="places")
            raise
        # An analysis that returned ok:False did not analyse anything:
        # counting it as done hid a Places refusal behind "analysed".
        if res.get("ok") is False:
            counts["failed"] += 1
            _record(r.id, "competitor", False, error=res.get("error") or "competitor analysis failed",
                    provider="places")
            if res.get("places_status") or "No nearby competitors" not in (res.get("error") or ""):
                _ops.capture(RuntimeError(res.get("error") or "competitor analysis failed"),
                             job="competitor_analysis", context=f"restaurant_id={r.id}")
        else:
            counts["analysed"] += 1
            _record(r.id, "competitor", True, provider="places")

    # In service only (MOD-REV-2): no Places or Claude spend on a customer
    # who has cancelled.
    eligible = [r for r in get_all_restaurants()
                if r.google_place_id and r.id and is_full_tier(r) and in_service(r)]
    # Every pass, not only the retry: a restaurant already analysed this
    # week is not analysed again — a pass re-run after a deploy used to buy
    # the same Places and Claude calls twice (#137).
    before = len(eligible)
    eligible = [r for r in eligible if not _ok_this_week(r.id, "competitor", week_start)]
    _n, hit_bound = _weekly_sweep("competitor_analysis", _COMPETITOR_CURSOR_KEY, eligible, _analyse)
    counts.update(attempted=counts["analysed"] + counts["failed"], ok=counts["analysed"],
                  skipped=before - len(eligible), hit_bound=bool(hit_bound))
    return counts


def run_weekly_ai_visibility(retry_only=False):
    """Weekly, from Monday 7am (ISO-week claim, Monday–Wednesday catch-up,
    then a daily `retry_only` pass until each restaurant has one success
    this week — DH2-10) — one AI visibility run per full-tier client.

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
    week_start = _week_start_utc()

    def _check(r):
        try:
            payload, _ = client_api._do_ai_visibility_inner(r.id, force=True)
        except Exception as e:
            counts["failed"] += 1
            _record(r.id, "visibility", False, error=str(e), provider="perplexity")
            raise
        if payload.get("ok"):
            counts["checked"] += 1
            _record(r.id, "visibility", True, provider="perplexity")
        else:
            # A soft failure — budget paused, rate-limited, a missing key —
            # reached neither job_failures nor the digest (DH2-10).
            counts["failed"] += 1
            why = payload.get("error") or payload.get("reason") or "AI visibility check did not run"
            _record(r.id, "visibility", False, error=why, provider="perplexity")
            _ops.capture(RuntimeError(str(why)[:200]), job="ai_visibility", context=f"restaurant_id={r.id}")

    # In service only (MOD-REV-2): no Perplexity spend on a cancelled customer.
    eligible = [r for r in get_all_restaurants() if r.id and is_full_tier(r) and in_service(r)]
    # Every pass skips a restaurant already checked this week (#137).
    before = len(eligible)
    eligible = [r for r in eligible if not _ok_this_week(r.id, "visibility", week_start)]
    _n, hit_bound = _weekly_sweep("ai_visibility", _VISIBILITY_CURSOR_KEY, eligible, _check)
    counts.update(attempted=counts["checked"] + counts["failed"], ok=counts["checked"],
                  skipped=before - len(eligible), hit_bound=bool(hit_bound))
    return counts


def run_daily_alert_checks():
    """Unresponded, trend/threshold/labor, food waste and visibility alerts.
    Attempted hourly; each restaurant is served at 10am in its own timezone
    (notify._gated_out), once per local day. A failure in one must not take
    the rest down with it, which is why each is wrapped separately rather
    than the whole block sharing one except.

    Returns the standard counts over the checks and the batch flush: a
    check that failed used to come back as {"daily": "failed: …"}, which
    job_runs recorded as a clean run (#39). The retention purge and log
    prune that ran here every hour, each one DELETE under the write lock,
    are the nightly retention job now (run_nightly_retention, #81)."""
    import notify as _notify
    from notify import (check_no_response_alerts, check_daily_alerts, check_extra_daily_alerts,
                        check_competitor_alerts)
    out = {}
    # Collect across all three, then send once per restaurant. These eight
    # alert types describe the same week of trading, and arriving separately
    # is what teaches an owner to swipe Cavnar away without reading — taking
    # the morning brief with it. See notify.DAILY_BATCH_TYPES.
    _notify.begin_daily_batch()
    for name, fn in (("no_response", check_no_response_alerts),
                     ("daily", check_daily_alerts),
                     ("extra_daily", check_extra_daily_alerts),
                     # Monday only (per restaurant, its own timezone): a
                     # competitor's rating moving, or a new one nearby (#48).
                     ("competitor", check_competitor_alerts),
                     # A data source that stopped updating, or came back
                     # (Data Freshness #18) — once per source per ISO week.
                     ("data_source", _notify.check_data_source_alerts)):
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
        batch_ok = True
    except Exception as e:
        out["batch"] = f"failed: {e}"
        batch_ok = False
        log.error(f"Daily alert batch flush failed: {e}")
        _ops.capture(e, job="daily_alerts", context="batch flush")
    checks = [v for k, v in out.items() if k != "batch"]
    failed = sum(1 for v in checks if v != "ok") + (0 if batch_ok else 1)
    out.update(attempted=len(checks) + 1, ok=len(checks) + 1 - failed, failed=failed, skipped=0, hit_bound=False)
    return out


def run_nightly_retention():
    """2am, straight after the backup (so the pruned rows are in it): the one
    retention registry (ops.prune_ledgers — chunked, a commit per chunk,
    bounded, then planner statistics) and each owner's own review retention
    (models.purge_expired_reviews). Both ran hourly inside the daily alert
    checks, each table one DELETE under the write lock (#81)."""
    out = _ops.prune_ledgers()
    try:
        from models import purge_expired_reviews
        purged = purge_expired_reviews()
        out["reviews_purged"] = purged
        if purged:
            log.info(f"Data retention: soft-deleted {purged} expired reviews")
    except Exception as pe:
        out["failed"] = int(out.get("failed") or 0) + 1
        out["attempted"] = int(out.get("attempted") or 0) + 1
        log.error(f"Retention purge failed: {pe}")
        _ops.capture(pe, job="prune_ledgers", context="review retention purge")
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
        "SELECT id FROM restaurants WHERE module_inventory=1 AND " + _served_client_sql()
    ).fetchall()
    conn.close()
    c = {"written": 0, "skipped": 0, "failed": 0, "scored": 0, "held": 0}
    lock = threading.Lock()
    import data_freshness
    from models import get_restaurant

    def _one(rid):
        # Not over overstated stock (DH2-4): on-hand is the last count minus
        # depletion since, so a restaurant whose depletion did not land for
        # last night would write an inflated inventory value into history,
        # and every COGS bracket reads it. Held, not written, and said.
        r = get_restaurant(rid)
        behind = data_freshness.depletion_behind(r, max_days_behind=0) if r is not None else None
        if behind:
            with lock:
                c["held"] += 1
            log.warning(f"Food cost snapshot held for restaurant {rid}: {behind}")
            return
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
             f"{c['held']} held for depletion, {scored} forecasts scored")
    return {"written": written, "skipped": skipped, "failed": failed, "forecasts_scored": scored,
            "held": c["held"], "attempted": written + failed, "ok": written, "hit_bound": bool(ran_out)}


DATA_HEALTH_DAILY_CURSOR_KEY = "data_health_daily_cursor"


def run_data_health_daily():
    """Daily — one Data Health snapshot per restaurant in service
    (data_health.record_daily → data_health_daily), dated by the
    restaurant's own day, so the admin rollup reads a table instead of
    recomputing every restaurant on every page load (DH5-13). Bounded and
    resumable."""
    import data_health
    from models import get_all_restaurants, in_service
    from time_utils import restaurant_now
    rows = {r.id: r for r in get_all_restaurants() if in_service(r)}
    c = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
    lock = threading.Lock()

    def _one(rid):
        day = restaurant_now(rows[rid], naive=True).date().isoformat()
        ok = data_health.record_daily(rid, day)
        with lock:
            c["attempted"] += 1
            c["ok" if ok else "failed"] += 1

    _done, ran_out = resumable_sweep(DATA_HEALTH_DAILY_CURSOR_KEY, list(rows), _one, SWEEP_MAX_SECONDS,
                                     workers=SWEEP_WORKERS, job="data_health_daily")
    if ran_out:
        _ops.capture(RuntimeError(f"Data health snapshots stopped at the {SWEEP_MAX_SECONDS}s bound; "
                                  "the rest lead the next pass"), job="data_health_daily", context="time_bound")
    c["hit_bound"] = bool(ran_out)
    return c


FORECAST_SCORING_CURSOR_KEY = "forecast_scoring_cursor"


def run_forecast_scoring():
    """Daily — score every frozen forecast whose period has closed, for every
    restaurant holding one (forecast_log.score_due).

    The food-cost snapshot pass above scores its own restaurants, but it
    only walks restaurants with Food Cost on; the week's sales projection
    (frozen at schedule publish) and the labor, marketing and review
    forecast lines belong to restaurants that may not have it. Bounded and
    resumable like every sweep here."""
    import forecast_log
    ids = forecast_log.restaurants_due()
    c = {"scored": 0, "restaurants": 0, "failed": 0}
    lock = threading.Lock()

    def _one(rid):
        try:
            n = forecast_log.score_due(rid).get("scored", 0)
            with lock:
                c["scored"] += n
                c["restaurants"] += 1
        except Exception as e:
            with lock:
                c["failed"] += 1
            _ops.capture(e, job="forecast_scoring", context=f"restaurant_id={rid}")

    _done, ran_out = resumable_sweep(FORECAST_SCORING_CURSOR_KEY, ids, _one, SWEEP_MAX_SECONDS,
                                     workers=SWEEP_WORKERS, job="forecast_scoring")
    if ran_out:
        _ops.capture(RuntimeError(f"Forecast scoring stopped at the {SWEEP_MAX_SECONDS}s bound; "
                                  f"the rest lead the next pass"), job="forecast_scoring", context="time_bound")
    log.info(f"Forecast scoring: {c['scored']} scored across {c['restaurants']} restaurants")
    # The bound reached job_runs only as a capture (#39): it is in the counts now.
    c.update(attempted=c["restaurants"] + c["failed"], ok=c["restaurants"], skipped=0, hit_bound=bool(ran_out))
    return c


LEARNING_MEMORY_CURSOR_KEY = "learning_memory_cursor"


def run_learning_memory():
    """Daily, after the outcome evaluations — the nightly learning pass for
    every restaurant allowed to teach a learner (learning_memory.nightly:
    score AI claims at their horizon, summarise closed quarters of reads,
    and the steps the memory audit added after them). Bounded and resumable
    like every sweep here; sends nothing."""
    import learning_memory
    ids = learning_memory.eligible_ids()
    c = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
    lock = threading.Lock()

    def _one(rid):
        res = learning_memory.nightly(rid)
        with lock:
            c["attempted"] += 1
            c["ok" if res.get("ok") else "failed"] += 1

    _done, ran_out = resumable_sweep(LEARNING_MEMORY_CURSOR_KEY, ids, _one, SWEEP_MAX_SECONDS,
                                     workers=SWEEP_WORKERS, job="learning_memory")
    if ran_out:
        _ops.capture(RuntimeError(f"The learning pass stopped at the {SWEEP_MAX_SECONDS}s bound; "
                                  "the rest lead the next pass"), job="learning_memory", context="time_bound")
    c["hit_bound"] = bool(ran_out)
    return c


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
        "SELECT id FROM restaurants WHERE module_inventory=1 AND " + _served_client_sql()
    ).fetchall()
    conn.close()
    c = {"diagnosed": 0, "skipped": 0, "failed": 0}
    lock = threading.Lock()

    def _one(rid):
        try:
            r = get_restaurant(rid)
            # A restaurant whose AI budget is spent gets no diagnosis rather
            # than a refused call and a captured exception.
            if r and _ai_budget_spent(rid):
                with lock:
                    c["skipped"] += 1
                return
            out = fci.diagnose(rid)
            if out and out.get("ok"):
                with lock:
                    c["diagnosed"] += 1
                # A fresh cause makes every cached food-cost narrative for
                # this restaurant out of date — it is what the narrative is
                # now built around.
                try:
                    from client_api import invalidate_insight_cache
                    invalidate_insight_cache(rid)
                except Exception:
                    pass
            else:
                with lock:
                    c["skipped"] += 1
        except Exception as e:
            with lock:
                c["failed"] += 1
            log.error(f"Food cost diagnosis failed for restaurant {rid}: {e}")
            _ops.capture(e, job="food_cost_diagnoses", context=f"restaurant_id={rid}")

    # Bounded and resumable (#84): a Sonnet call per restaurant walked the
    # whole fleet serially on the loop thread with no bound and no cursor.
    _done, ran_out = resumable_sweep(FOOD_COST_DIAGNOSES_CURSOR_KEY, [row["id"] for row in rows], _one,
                                     DIAGNOSES_MAX_SECONDS, workers=1, job="food_cost_diagnoses")
    if ran_out:
        _ops.capture(RuntimeError(f"Food cost diagnoses stopped at the {DIAGNOSES_MAX_SECONDS}s bound; "
                                  "the rest lead the next pass"), job="food_cost_diagnoses", context="time_bound")
    log.info(f"Food cost diagnoses: {c['diagnosed']} produced, {c['skipped']} skipped, {c['failed']} failed")
    return {"diagnosed": c["diagnosed"], "skipped": c["skipped"], "failed": c["failed"],
            "attempted": c["diagnosed"] + c["failed"], "ok": c["diagnosed"], "hit_bound": bool(ran_out)}


# The two daily root-cause passes (#84): a Sonnet call per restaurant (per
# cluster, for reviews), bounded so a large fleet cannot hold the 6am chain
# past the 7am local briefs that read what they write, and resumable so the
# tail is reached the next day rather than never.
DIAGNOSES_MAX_SECONDS = int(os.getenv("DIAGNOSES_MAX_SECONDS", str(40 * 60)))
REVIEW_DIAGNOSES_CURSOR_KEY = "review_diagnoses_cursor"
FOOD_COST_DIAGNOSES_CURSOR_KEY = "food_cost_diagnoses_cursor"


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
        "SELECT id FROM restaurants WHERE module_reviews=1 AND " + _served_client_sql()
    ).fetchall()
    conn.close()
    c = {"diagnosed": 0, "skipped": 0, "failed": 0, "attempted": 0}
    lock = threading.Lock()

    def _one(rid):
        try:
            r = get_restaurant(rid)
            # A restaurant whose AI budget is spent gets no diagnosis rather
            # than a failed call per cluster — create_with_retry would refuse
            # each one individually and we would pay three exceptions for it.
            if r and _ai_budget_spent(rid):
                with lock:
                    c["skipped"] += 1
                return
            with lock:
                c["attempted"] += 1
            produced = ri.diagnose(rid)
            if produced:
                with lock:
                    c["diagnosed"] += 1
                # A fresh cause makes every cached insight for this
                # restaurant out of date — it is the thing the insight is now
                # built around.
                try:
                    from client_api import invalidate_insight_cache
                    invalidate_insight_cache(rid)
                except Exception:
                    pass
        except Exception as e:
            with lock:
                c["failed"] += 1
            log.error(f"Review diagnosis failed for restaurant {rid}: {e}")
            _ops.capture(e, job="review_diagnoses", context=f"restaurant_id={rid}")

    # Bounded and resumable (#84), like the food-cost pass above.
    _done, ran_out = resumable_sweep(REVIEW_DIAGNOSES_CURSOR_KEY, [row["id"] for row in rows], _one,
                                     DIAGNOSES_MAX_SECONDS, workers=1, job="review_diagnoses")
    if ran_out:
        _ops.capture(RuntimeError(f"Review diagnoses stopped at the {DIAGNOSES_MAX_SECONDS}s bound; "
                                  "the rest lead the next pass"), job="review_diagnoses", context="time_bound")
    log.info(f"Review diagnoses: {c['diagnosed']} produced, {c['skipped']} skipped, {c['failed']} failed")
    # A restaurant with nothing new to diagnose (no cluster) is attempted
    # and fine: `ok` is every attempt that did not fail.
    return {"diagnosed": c["diagnosed"], "skipped": c["skipped"], "failed": c["failed"],
            "attempted": c["attempted"], "ok": c["attempted"] - c["failed"], "hit_bound": bool(ran_out)}


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


def _summary_audience(r):
    """Whether restaurant `r` gets the monthly and quarterly summaries by
    billing state: in service (past due keeps them; paused, churned and
    canceled do not — canceled used to get them, #155) and a real client
    (not the operator's internal account)."""
    from models import in_service
    return bool(r.owner_email) and in_service(r) and (r.billing_status or "").lower() != "internal"


def _summary_outcome(job, rs, result, claim_key):
    """What one monthly or quarterly send did (#16): 'sent', 'skipped'
    (nothing to report) or 'failed'. A transient failure gives the day's
    claims back so the next hourly tick inside the 9am window retries; any
    other failure is raised to the operator."""
    from time_utils import restaurant_now as _rnow
    if getattr(result, "ok", False):
        return "sent"
    if getattr(result, "skipped", False):
        return "skipped"
    err = getattr(result, "error", None) or "not sent"
    if getattr(result, "transient", False):
        for r in rs:
            _ops.release_period(f"{claim_key}:{r.id}", _rnow(r, naive=True).date().isoformat())
    _ops.capture(RuntimeError(f"{job} not sent: {err}"), job=job, context=f"restaurant_id={rs[0].id}")
    return "failed"


def run_monthly_summaries():
    """1st of the month, 9am — the monthly summary email to active clients.

    Counted as sent, and the "August is in" push fired, only when Resend
    accepted the email (#16) — every attempt used to count as sent and the
    phones were told the month was in whether or not the email went."""
    from emails import send_monthly_summary_email, send_monthly_group_summary_email
    from models import get_all_restaurants
    sent = skipped = failed = 0
    # One email per OWNER: a three-location owner got three, each reading
    # as the whole business (moat audit #14). Restaurants due now are
    # grouped by owner email; a group of one takes the single-location path.
    due = []
    for r in get_all_restaurants():
        # 'paused' is the owner's own request for quiet — the monthly stops
        # too, and so does a cancellation (#155).
        if not _summary_audience(r):
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
                result = send_monthly_summary_email(
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
                result = send_monthly_group_summary_email(rs[0].owner_email, rs[0].owner_name,
                                                          sorted(rs, key=lambda x: x.name))
        except Exception as me:
            result = _emails.not_sent("build_error", str(me)[:300])
        outcome = _summary_outcome("monthly_summary", rs, result, "monthly_summary")
        if outcome == "sent":
            sent += len(rs)
            log.info(f"Monthly summary sent to {', '.join(x.name for x in rs)}")
            # The month-ready push only after the email went: it says the
            # month is in, and a phone must not hear that about an email
            # that never arrived.
            for r in rs:
                try:
                    _push_month_ready(r)
                except Exception as pe:
                    _ops.capture(pe, job="month_ready_push", context=f"restaurant_id={r.id}")
        elif outcome == "skipped":
            skipped += len(rs)
        else:
            failed += len(rs)
            log.error(f"Monthly summary failed for {', '.join(x.name for x in rs)}: "
                      f"{getattr(result, 'error', None)}")
    # The standard counts (#39), by restaurant, with `sent` as it was.
    return {"attempted": sent + failed, "ok": sent, "failed": failed, "skipped": skipped, "hit_bound": False,
            "sent": sent}


def run_quarterly_summaries():
    """1st of Jan / Apr / Jul / Oct, 9am local — the quarter that just
    ended. Same audience and the same switch as the monthly; counted as sent
    only when Resend accepted it (#16), "nothing to report" is skipped."""
    from emails import send_quarterly_summary_email
    from models import get_all_restaurants
    sent = skipped = failed = 0
    for r in get_all_restaurants():
        if not _summary_audience(r):
            skipped += 1
            continue
        if not local_due(r, 9, claim_key="quarterly_summary", day=1):
            skipped += 1
            continue
        if not getattr(r, "monthly_review_enabled", 1):
            skipped += 1
            continue
        try:
            result = send_quarterly_summary_email(to_email=r.owner_email, restaurant_name=r.name,
                                                  owner_name=r.owner_name, restaurant_id=r.id)
        except Exception as qe:
            result = _emails.not_sent("build_error", str(qe)[:300])
        outcome = _summary_outcome("quarterly_summary", [r], result, "quarterly_summary")
        if outcome == "sent":
            sent += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1
    # The standard counts (#39), with `sent` as it was.
    return {"attempted": sent + failed, "ok": sent, "failed": failed, "skipped": skipped, "hit_bound": False,
            "sent": sent}


AUTO_PUBLISH_UNDO_MINUTES = 120


def run_auto_publish_schedules():
    """9am local the day after the restaurant's draft day (Friday for the
    default Thursday draft; models.auto_publish_weekday): queue the draft to go to staff
    AUTO_PUBLISH_UNDO_MINUTES later, with those two hours as the undo window — for owners who turned it on AND
    whose last SCHEDULE_PUBLISH_TRUST_MIN published schedules went out
    unedited. The draft must be this coming week's, untouched, and not yet
    shared. Nothing is sent here; delayed.run_due sends it, and the owner
    is told now so "undo" is a real choice."""
    import delayed
    from models import (get_all_restaurants, get_conn, schedule_publish_trust, SCHEDULE_PUBLISH_TRUST_MIN, DB_PATH,
                        auto_publish_weekday)
    from time_utils import restaurant_now
    queued = skipped = failed = 0
    for r in get_all_restaurants():
        if not getattr(r, "auto_publish_schedule", 0) or not getattr(r, "module_labor", 0):
            continue
        if not _served_client(r):
            continue
        local = restaurant_now(r, naive=True)
        if local.weekday() != auto_publish_weekday(r) or not local_due(r, 9, claim_key="auto_publish_schedule"):
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
                # The week's CURRENT draft — the newest one no later draft
                # superseded — and only it. Filtering unedited rows first
                # meant that once the owner edited the current draft, an
                # older one they had abandoned was the "newest unedited" and
                # was queued to staff with "unchanged from the draft".
                row = conn.execute(
                    "SELECT h.id, h.week_start, h.edited_at FROM schedule_history h WHERE h.restaurant_id=? AND h.week_start=? "
                    "AND h.superseded_by IS NULL AND (h.schedule_csv IS NOT NULL AND h.schedule_csv != '') "
                    "AND NOT EXISTS (SELECT 1 FROM schedule_history p WHERE p.restaurant_id=h.restaurant_id "
                    "  AND p.week_start=h.week_start AND (p.published_at IS NOT NULL "
                    "  OR EXISTS (SELECT 1 FROM schedule_shares s WHERE s.schedule_id=p.id))) "
                    "ORDER BY h.id DESC LIMIT 1", (r.id, nxt["w"])).fetchone()
        finally:
            conn.close()
        if not row:
            skipped += 1
            continue
        if row["edited_at"]:
            # The owner has taken this week over; it goes out when they send it.
            skipped += 1
            continue
        # Never unread: a week with flagged rows, a hard rule breach, a
        # weak quality verdict or a low-confidence score waits for a human,
        # and the owner is told why instead of being told it went out.
        # The gate as it stands now, as automation sees it: the notice rule,
        # hard breaches against today's data, and the soft flags a person
        # decides (meal breaks, daily overtime, unanswered time off) all
        # hold it; any other soft flag is named in the notice (NS5 H2/H3/M7).
        soft = []
        try:
            from client_api import publish_review
            _rv = publish_review(r.id, row["id"], unattended=True)
            blockers = [b["text"] for b in _rv["blockers"]]
            soft = _rv.get("soft") or []
        except Exception as e:
            _ops.capture(e, job="auto_publish_schedule_check", context=f"restaurant_id={r.id}")
            blockers = ["The publish check could not run"]
        if blockers:
            skipped += 1
            try:
                from strategy_jobs import _reach
                _reach(r.id, "schedule_publish_held",
                       "Next week's schedule needs a look before it goes out",
                       "The draft for the week of " + mdy(row["week_start"]) + " was not sent: "
                       + "; ".join(blockers[:3]) + ". Review it on the Labor tab and send it yourself.",
                       {"schedule_id": row["id"]}, DB_PATH,
                       subject=f"Next week's schedule is waiting on you — {r.name}")
            except Exception as e:
                _ops.capture(e, job="auto_publish_schedule_hold", context=f"restaurant_id={r.id}")
            continue
        try:
            action = delayed.schedule(r.id, "schedule_publish", {"schedule_id": row["id"], "automatic": True},
                                      AUTO_PUBLISH_UNDO_MINUTES,
                                      label=f"Publishing the week of {mdy(row['week_start'])} to staff")
            from strategy_jobs import _reach, _clock
            # The real send time. The job's window runs 9am to 2pm local, so
            # a late pass queued it for up to 3:59pm while the notice still
            # said "at 11am" (re-audit A-18).
            from datetime import timedelta as _td
            goes = restaurant_now(r, naive=True) + _td(minutes=AUTO_PUBLISH_UNDO_MINUTES)
            at = _clock(goes.hour, goes.minute)
            _reach(r.id, "schedule_publish_pending",
                   f"Next week's schedule goes to staff at {at}",
                   f"The week of {mdy(row['week_start'])} is unchanged from the draft."
                   + (f" Worth a look: {'; '.join(soft[:3])}" + (f" and {len(soft) - 3} more" if len(soft) > 3 else "") + "."
                      if soft else "")
                   + " Undo from Home before then if you'd rather look first.",
                   {"delayed_action_id": action["id"]}, DB_PATH,
                   subject=f"Publishing next week's schedule at {at} — {r.name}",
                   # It carries Undo: only to the people the cancel route
                   # lets undo it (F3-13).
                   permissions=[__import__("permissions").SCHEDULE_PUBLISH])
            queued += 1
        except Exception as e:
            failed += 1
            _ops.capture(e, job="auto_publish_schedule", context=f"restaurant_id={r.id}")
    return {"queued": queued, "skipped": skipped, "attempted": queued + failed, "ok": queued, "failed": failed,
            "hit_bound": False}


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
    # Fresh (#2): the drill picked the newest snapshot with no age check, so
    # a backup that had silently stopped weeks ago still "passed".
    age_hours = round((time.time() - os.path.getmtime(newest)) / 3600.0, 1)
    report = {"snapshot": os.path.basename(newest),
              "size_mb": round(os.path.getsize(newest) / 1e6, 1), "ok": False,
              "age_hours": age_hours, "fresh": age_hours <= _ops.BACKUP_STALE_HOURS}
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
        report["ok"] = report["ok"] and report["tokens_survive"] and report["fresh"]
    finally:
        _clean()

    # The off-site copy is proved too, where there is one: downloaded,
    # checked against the checksum it was uploaded with, decrypted and
    # integrity-checked — the copy that survives losing the volume (#1).
    report["offsite"] = _drill_offsite(backup_dir)
    if report["offsite"].get("checked"):
        report["ok"] = report["ok"] and bool(report["offsite"].get("ok"))

    try:
        import html as _h
        lines = [f"Snapshot: {report['snapshot']} ({report['size_mb']} MB, {report['age_hours']}h old"
                 + ("" if report["fresh"] else " — STALE") + ")",
                 "Off-site copy: " + (report["offsite"].get("summary") or "not checked"),
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
    report.update(attempted=1, failed=0, skipped=0, hit_bound=False)
    return report


def _decrypt_file_chunked(src_path, dest_path, key):
    """Undo _encrypt_file_chunked: one Fernet token per line (a file of one
    token — the old format — is one line)."""
    from cryptography.fernet import Fernet
    fernet = Fernet(key.encode())
    with open(src_path, "rb") as src, open(dest_path, "wb") as dst:
        for line in src:
            line = line.strip()
            if line:
                dst.write(fernet.decrypt(line))


def _drill_offsite(backup_dir):
    """Download the newest object-storage copy, check it against the SHA-256
    it was uploaded with, decrypt it and integrity-check it, in a scratch
    file beside the snapshots. {"checked", "ok", "summary"}; "checked" is
    False where no object storage is configured or nothing was uploaded."""
    import sqlite3
    import offsite_backup
    cfg = offsite_backup.s3_config()
    key = (os.getenv("BACKUP_ENCRYPTION_KEY") or "").strip()
    if not cfg or not key:
        return {"checked": False, "summary": "no object storage configured"}
    from models import get_conn
    try:
        conn = get_conn()
        try:
            row = conn.execute("SELECT offsite_target, sha256, finished_at FROM backup_runs WHERE offsite_ok=1 "
                               "AND offsite_target LIKE 's3://%' ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:
        return {"checked": True, "ok": False, "summary": f"backup ledger unreadable: {e}"}
    if not row:
        return {"checked": True, "ok": False, "summary": "no object-storage copy has been recorded"}
    target = str(row["offsite_target"]).split(",")[0].strip()
    object_key = target.split("/", 3)[3] if target.count("/") >= 3 else ""
    enc = os.path.join(backup_dir, "restore_drill_offsite.enc")
    plain = os.path.join(backup_dir, "restore_drill_offsite.db")
    try:
        got = offsite_backup.download_file(object_key, enc, cfg=cfg, full_key=True)
        if row["sha256"] and got["sha256"] != row["sha256"]:
            return {"checked": True, "ok": False,
                    "summary": f"{target}: checksum mismatch ({got['sha256'][:12]}… vs {row['sha256'][:12]}…)"}
        _decrypt_file_chunked(enc, plain, key)
        c = sqlite3.connect(plain)
        try:
            integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
            restaurants = c.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
        finally:
            c.close()
        ok = integrity == "ok" and restaurants > 0
        return {"checked": True, "ok": ok,
                "summary": f"{target} ({round(got['bytes'] / 1e6, 1)} MB, uploaded {row['finished_at']} UTC): "
                           f"checksum ok, decrypts, integrity {integrity}, {restaurants} restaurants"}
    except Exception as e:
        return {"checked": True, "ok": False, "summary": f"{target}: {e}"}
    finally:
        for p in (enc, plain, plain + "-wal", plain + "-shm", plain + "-journal"):
            try:
                os.remove(p)
            except OSError:
                pass


def run_prune_login_attempts():
    """Daily — login-attempt rows older than two days (security.py's own
    prune), as a job run with the standard counts. It ran outside run_job
    and wrote no job run (#31)."""
    import security as _security
    _security.prune_login_attempts()
    return {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False}


def run_rec_ledger_pass():
    """8am — the recommendation trail (rec_ledger): answers held by the older
    ledgers carried in (after 6am's outcome verdicts), and recommendations
    nobody answered in two weeks closed as ignored. repair_sync_replays
    first: it undoes what the replaying sync wrote before it attached
    answers by time (a no-op after that), and the sync then re-carries those
    answers where they belong. backfill_tags tags episodes from before tags
    were stored (ROI #17), bounded; it finishes the tail on later nights.
    Module-level so "Run now" can reach it (it lived inside the loop)."""
    import rec_ledger as _rl
    out = {"repaired": _rl.repair_sync_replays(), "synced": _rl.sync_existing(),
           "expired": _rl.expire_stale(), "tagged": _rl.backfill_tags()}
    out.update(attempted=4, ok=4, failed=0, skipped=0, hit_bound=False)
    return out


def _minute_duties():
    """The per-tick work that owes the owner minutes, not hours: scheduled
    posts, delayed actions whose undo window closed, issue escalations and
    held notifications, alerts held through a rush, newsletter and campaign
    batches, and the push and webhook outboxes. Each is idempotent and
    claims its own rows, so running it an extra time is harmless.

    Runs as the `minute_duties` job (ops.run_job) — it wrote no job run at
    all (#31) — and returns the standard counts over its eight duties."""
    c = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}

    def _duty(fn, job):
        c["attempted"] += 1
        try:
            out = fn()
        except Exception as e:
            c["failed"] += 1
            _ops.capture(e, job=job)
            return None
        c["ok"] += 1
        return out

    # Not named `_due`: that name is scheduler_loop's hour gate.
    def _posts():
        from marketing_publish import run_due_posts
        return run_due_posts(base_url=config.base_url())
    posts = _duty(_posts, job="scheduled_posts")
    if posts and (posts.get("published") or posts.get("failed")):
        log.info(f"Scheduled posts: {posts}")

    def _delayed_run():
        import delayed as _delayed
        return _delayed.run_due()
    dl = _duty(_delayed_run, job="delayed_actions")
    if dl and (dl.get("ran") or dl.get("failed")):
        log.info(f"Delayed actions: {dl}")

    def _issues_tick():
        import issues as _issues
        return _issues.tick()
    _duty(_issues_tick, job="issues_tick")

    def _release():
        import notify as _notify_rel
        return _notify_rel.release_due_alerts()
    _duty(_release, job="release_held_alerts")

    # The rest of any newsletter the owner sent, in bounded batches
    # (guest_email.send_newsletter, MOD-EML-3).
    def _newsletters():
        from guest_email import run_newsletter_sends
        return run_newsletter_sends()
    nl = _duty(_newsletters, job="newsletter_sends")
    if nl and (nl.get("sent") or nl.get("failed")):
        log.info(f"Newsletter sends: {nl}")

    # Guest text campaigns still sending, cut off by a deploy, or waiting on
    # the 8am window, in bounded passes; the queue rows are the cursor
    # (guest_marketing.run_campaign_sends, MB-10).
    def _campaigns():
        from guest_marketing import run_campaign_sends
        return run_campaign_sends()
    gc = _duty(_campaigns, job="guest_campaign_sends")
    if gc and (gc.get("sent") or gc.get("failed")):
        log.info(f"Guest campaign sends: {gc}")

    # The push and webhook outboxes (#75): rows a restart or a full pool
    # left queued or half-delivered go back to the pool; rows too old to
    # matter expire, recorded as not sent.
    def _push_outbox():
        import push as _push
        return _push.reap_push_outbox()
    po = _duty(_push_outbox, job="push_outbox_reaper")
    if po and (po.get("submitted") or po.get("expired")):
        log.info(f"Push outbox: {po}")

    def _webhook_outbox():
        import webhooks as _webhooks
        return _webhooks.reap_webhook_outbox()
    wo = _duty(_webhook_outbox, job="webhook_outbox_reaper")
    if wo and (wo.get("submitted") or wo.get("expired")):
        log.info(f"Webhook outbox: {wo}")
    return c


def _pulse_interval():
    """How often the pulse fires while a job runs: once a tick, and well
    inside the lease's stale window so a live runner never looks dead."""
    return max(0.05, min(float(SCHEDULER_TICK_SECONDS), _ops.SCHEDULER_LEASE_STALE_SECONDS / 3.0))


class _PulsedOps:
    """scheduler_loop's view of ops: every attribute is ops' own, except
    run_job, which runs the job with a pulse beside it, and claim_period,
    which remembers what the tick claimed so a failed job can give its
    period back.

    The loop is one thread. A gated job that ran long — the review fetch is
    bounded at three hours — used to hold up everything after it in the
    tick (DATA-3 / MOD-PERF-1): an undo-window supplier order or
    auto-publish, a scheduled post, an issue escalation and a held alert
    all waited the whole pass out; the heartbeat went stale, so the status
    page showed an outage; and the lease, renewed only at the top of the
    loop, went stale too, so a standby process took it and ran the same
    tick beside the holder (DATA-4).

    The pulse is a short-lived thread that, while one job runs, renews the
    lease, stamps the heartbeat and runs _minute_duties once per
    _pulse_interval(), starting as soon as the job starts if the duties are
    due. Morning briefs stay on the loop thread: the 5-6am diagnoses must
    finish before a 7am local brief reads them.

    The watchdog (#121): the pulse vouches for the loop only while the job
    is inside its own bound (jobs_registry.max_minutes). Past it, the pulse
    stops stamping the heartbeat — the platform page then says which job the
    loop is stuck in — and captures the overrun once. The job it is in is
    recorded (status_manager.record_running_job) so "the loop has not
    finished a tick" can tell a long job from a tick failing part-way.

    The external dead-man monitor (HEALTHCHECK_PING_URL, #3) hears from the
    end of every tick (tick_completed) and, during a long job, from the
    pulse — on the same terms as the heartbeat: the job is inside its bound
    AND the loop has been completing its ticks (_keeping_up). Pinged only at
    the end of a tick, a review fetch or a diagnoses pass inside its bound
    (40 minutes to over three hours) went silent for longer than any sane
    monitor grace and paged a healthy platform; pinged from every pulse,
    a tick failing part-way still pinged from the jobs before its failure.

    Two guards on starting a job:
      * never one that is already running, here or in another process (a
        manual run, a run a lease hand-over left going) — skipped and
        logged, the period kept (#64, #153);
      * a non-sending daily or weekly job that FAILED (jobs_registry
        `retry`) gives its period back, and claim_period refuses it until
        its backoff (RETRY_BACKOFF_MINUTES) has passed, up to that many
        retries (#56): a failed nightly job used to wait a whole day.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last = None               # time.monotonic() the duties last ran
        self._claimed = {}              # claim key -> the period this tick claimed
        self._retries = {}              # (claim key, period) -> {"attempt", "at"}
        self._ticks = 0                 # ticks this process has begun
        self._tick_began = None         # time.monotonic() the current tick began
        self._completed_at = None       # time.monotonic() a tick last completed

    def __getattr__(self, name):
        return getattr(_ops, name)

    def became_runner(self):
        """Newly the lease holder: its record of completed ticks starts again,
        so ticks from before a stand-by do not count for or against it; and a
        job a dead holder was inside is no longer "running" — it would read
        as wedged until this runner's first job replaced it."""
        self._ticks = 0
        self._completed_at = None
        try:
            record_running_job(None)
        except Exception:
            pass

    def begin_tick(self):
        self._claimed = {}
        self._ticks += 1
        self._tick_began = time.monotonic()

    def tick_completed(self):
        """The end of a tick: remember it, and tell the external dead-man
        monitor (#3). ops.ping_healthcheck is short and never raises."""
        self._completed_at = time.monotonic()
        _ops.ping_healthcheck()

    def _keeping_up(self):
        """Whether the loop has been completing its ticks: this is the
        process's first tick, or the previous one completed no more than a
        tick interval (and a minute) before this one began. A tick failing
        part-way never completes, so from the next tick on this is False and
        the pulse stops pinging — the monitor's silence then pages."""
        if self._tick_began is None:
            return False
        if self._completed_at is None:
            return self._ticks <= 1
        return self._tick_began - self._completed_at <= SCHEDULER_TICK_SECONDS + 60

    def claim_period(self, job, period):
        retry = self._retries.get((job, period))
        if retry is not None:
            if time.monotonic() < retry["at"]:
                return False
            # Its backoff has passed: the period goes back for one more try.
            _ops.release_period(job, period)
        got = _ops.claim_period(job, period)
        if got:
            self._claimed[job] = period
        return got

    def duties_due(self):
        return self._last is None or time.monotonic() - self._last >= _pulse_interval()

    def run_duties(self, renew_lease=False, vouch=True):
        with self._lock:
            if renew_lease and not _ops.acquire_scheduler_lease():
                log.error("Scheduler lease lost mid-pass — another process now holds it")
            if renew_lease and vouch:
                try:
                    record_scheduler_heartbeat()
                except Exception:
                    pass
            # Its own job run (#31): it wrote no job_runs row at all. Through
            # ops' run_job, never this class's — a pulse inside a pulse would
            # wait on this lock forever.
            _ops.run_job("minute_duties", _minute_duties)
            self._last = time.monotonic()

    def _pulse(self, name, started, stop):
        started.wait()
        begun = time.monotonic()
        bound = jobs_registry.max_minutes(name) * 60.0
        overrun_told = False
        while not stop.is_set():
            wait = 0.0 if self._last is None else max(0.0, self._last + _pulse_interval() - time.monotonic())
            if stop.wait(wait):
                return
            within = time.monotonic() - begun <= bound
            if not within and not overrun_told:
                overrun_told = True
                log.error(f"{name} has run past its {int(bound // 60)}-minute bound — the loop is stuck in it")
                try:
                    _ops.capture(RuntimeError(f"{name} has run past its {int(bound // 60)}-minute bound"),
                                 job=name, context="watchdog")
                except Exception:
                    pass
            try:
                self.run_duties(renew_lease=True, vouch=within)
            except Exception as e:
                log.error(f"Scheduler pulse failed: {e}")
            # Outside the duties' lock: a slow monitor never holds them up.
            if within and self._keeping_up():
                _ops.ping_healthcheck()

    def _already_running(self, name, claim):
        for n in {name, claim or name}:
            if _ops.is_running(n):
                return {"job": n, "where": "this process"}
            other = _ops.running_elsewhere(n)
            if other:
                return dict(other, where="another run")
        return None

    def run_job(self, name, fn, *args, **kwargs):
        claim = kwargs.get("claim") or name
        period = self._claimed.get(claim)
        busy = self._already_running(name, kwargs.get("claim"))
        if busy:
            log.warning(f"{name} not started: a run is still going ({busy.get('where')}, "
                        f"started {busy.get('started_at', 'now')})")
            # Its period goes back, so a tick after the live run finishes
            # starts it (JOBS-12) — never where one claim covers several jobs.
            if period is not None and len(jobs_registry.jobs_for_claim(claim)) <= 1:
                _ops.release_period(claim, period)
                self._claimed.pop(claim, None)
            return None
        spec = jobs_registry.spec(name)
        outcome = {}
        started, stop = threading.Event(), threading.Event()

        def body(*a, **k):
            started.set()
            try:
                res = fn(*a, **k)
            except Exception:
                outcome["raised"] = True
                raise
            outcome["result"] = res
            return res
        try:
            record_running_job(name)
        except Exception:
            pass
        pulse = threading.Thread(target=self._pulse, args=(name, started, stop), daemon=True,
                                 name=f"scheduler-pulse-{name}")
        pulse.start()
        try:
            return _ops.run_job(name, body, *args, **kwargs)
        finally:
            stop.set()
            started.set()
            pulse.join()
            try:
                record_running_job(None)
            except Exception:
                pass
            self._after(name, claim, period, spec, outcome)

    def _after(self, name, claim, period, spec, outcome):
        """A failed retryable job gives its period back after a backoff."""
        if period is None:
            return
        key = (claim, period)
        failed = outcome.get("raised") or (
            "result" in outcome and _ops.run_outcome(outcome["result"])[0] == _ops.RUN_FAILED)
        if not failed:
            self._retries.pop(key, None)
            return
        if not spec.get("retry") or spec.get("sends") or len(jobs_registry.jobs_for_claim(claim)) > 1:
            return
        attempt = self._retries.get(key, {}).get("attempt", 0)
        backoff = jobs_registry.RETRY_BACKOFF_MINUTES
        if attempt >= len(backoff):
            log.error(f"{name} failed for {period} after {attempt} retries — the period stays spent")
            self._retries.pop(key, None)
            return
        self._retries[key] = {"attempt": attempt + 1, "at": time.monotonic() + backoff[attempt] * 60}
        log.warning(f"{name} failed for {period}; retry {attempt + 1} in {backoff[attempt]} minutes")

    def run_in_lane(self, lane, name, fn, *args, **kwargs):
        """Start `name` on its worker lane beside the loop (#98). False when
        the lane is busy or the job is already running — the caller gives
        its period back, so a later tick starts it."""
        if self._already_running(name, kwargs.get("claim")):
            return False
        return _LANES[lane].submit(name, fn, *args, **kwargs)


class _Lane:
    """One worker thread beside the loop for a long, network-bound sweep
    (#98, #131). The weekly Intel sweeps are bounded at three hours each
    and ran inline, so on a Monday morning briefs, the DSR and intraday
    captures waited behind them. A lane runs ONE job at a time, only while
    this process holds the scheduler lease, through ops.run_job (recorded,
    captured, pulse-stamped), with a pulse that renews the lease while it
    runs. It never stamps the loop's heartbeat: a live lane must not make a
    dead loop look alive."""

    def __init__(self, name):
        self.name = name
        self._lock = threading.Lock()
        self._thread = None

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def submit(self, job_name, fn, *args, **kwargs):
        with self._lock:
            if self.busy():
                return False
            self._thread = threading.Thread(target=self._run, args=(job_name, fn, args, kwargs), daemon=True,
                                            name=f"scheduler-lane-{self.name}")
            self._thread.start()
            return True

    def _run(self, job_name, fn, args, kwargs):
        if not _ops.acquire_scheduler_lease():
            log.error(f"lane {self.name}: {job_name} not started — this process no longer holds the lease")
            return
        stop = threading.Event()

        def _renew():
            while not stop.wait(_pulse_interval()):
                try:
                    _ops.acquire_scheduler_lease()
                except Exception:
                    pass
        keeper = threading.Thread(target=_renew, daemon=True, name=f"scheduler-lane-{self.name}-lease")
        keeper.start()
        try:
            _ops.run_job(job_name, fn, *args, **kwargs)
        except Exception as e:
            log.error(f"lane {self.name}: {job_name} crashed outside run_job: {e}")
        finally:
            stop.set()

    def join(self, timeout=None):
        t = self._thread
        if t is not None:
            t.join(timeout)


_LANES = {"intel": _Lane("intel")}


class _LeaseKeeper:
    """Renews this process's scheduler lease every ops.LEASE_RENEW_SECONDS,
    and advertises that it does (`kept`), so when this process dies the
    next one takes over after ops.LEASE_OWNER_GONE_SECONDS instead of
    idling out the 30-minute window (#134). It renews only a lease this
    process already holds, never while shutting down, and stops when the
    loop is stuck in a job past twice its bound — a wedged runner then
    loses the lease to a standby instead of holding it forever."""

    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and getattr(self._thread, "is_alive", lambda: False)():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="scheduler-lease-keeper")
        self._thread.start()

    def _healthy(self):
        try:
            import status_manager
            st = status_manager.scheduler_state()
        except Exception:
            return True
        if st.get("running_job") and st.get("running_minutes") is not None:
            return st["running_minutes"] <= 2 * (st.get("running_bound_minutes") or jobs_registry.DEFAULT_MAX_MINUTES)
        return True

    def _run(self):
        # Advertised only once the keeper really runs: acquire_scheduler_lease
        # writes kept=1, and the next process may then take over after
        # LEASE_OWNER_GONE_SECONDS of silence.
        _ops._lease_kept.set()
        while not self._stop.wait(_ops.LEASE_RENEW_SECONDS):
            if _ops._shutting_down.is_set():
                return
            try:
                if self._healthy():
                    _ops.renew_scheduler_lease()
            except Exception as e:
                log.warning(f"lease keeper: {e}")


_LEASE_KEEPER = _LeaseKeeper()


def _run_manual_requests(pulsed):
    """Run now, handed over by the console (#153): each request this
    process takes (ops.take_job_requests — a compare-and-set, so two
    processes never both run one) is run here, under the lease, through the
    loop's own run_job — with the pulse, and never beside a live run of the
    same job. Returns how many ran."""
    ran = 0
    for req in _ops.take_job_requests(limit=3):
        name = req["job"]
        spec = jobs_registry.spec(name)
        if not spec or not spec.get("runnable") or not spec.get("target"):
            _ops.finish_job_request(req["id"], False, "not a runnable job")
            continue
        try:
            import importlib
            mod, fn_name = spec["target"]
            fn = getattr(importlib.import_module(mod), fn_name)
        except Exception as e:
            _ops.finish_job_request(req["id"], False, f"could not load {spec['target']}: {e}")
            continue
        if pulsed._already_running(name, spec.get("claim")):
            _ops.finish_job_request(req["id"], False, "already running")
            continue
        kwargs = jobs_registry.run_kwargs(name, now=_chi_now())
        outcome = {}

        def body(fn=fn, kwargs=kwargs, outcome=outcome):
            try:
                res = fn(**kwargs)
            except Exception as e:
                outcome["error"] = str(e)
                raise
            outcome["result"] = res
            return res
        # An admin's Run now: its model calls are the admin's (#148), not
        # the scheduler's.
        try:
            import ai_utils as _ai_manual
            manual_ctx = _ai_manual.ai_context(trigger="admin", correlation_id=f"run_now:{name}:{req['id']}")
        except Exception:
            import contextlib as _ctxlib
            manual_ctx = _ctxlib.nullcontext()
        with manual_ctx:
            pulsed.run_job(name, body, context=f"manual by {req.get('requested_by') or 'admin'}",
                           request_id=req["id"])
        state = _ops.run_outcome(outcome["result"])[0] if "result" in outcome else _ops.RUN_FAILED
        _ops.finish_job_request(req["id"], state != _ops.RUN_FAILED,
                                outcome.get("error") or (None if state != _ops.RUN_FAILED else "the run failed"))
        ran += 1
    return ran


def scheduler_loop():
    # No module-level "already ran" globals any more — every gate below is
    # ops.claim_period(), which is DB-backed and survives redeploys. See
    # ops.claim_period for what was wrong with the globals.
    log.info("Scheduler started — review fetch every 4hr (8am/12pm/4pm/8pm CT), digests 9am on client's chosen day")


    _lease_lost_logged = False
    holding = False
    # Every `_ops.run_job(...)` below runs with a pulse beside it; every
    # other `_ops.` name is ops' own (_PulsedOps).
    _ops = _PulsedOps()

    while True:
        try:
            # One scheduler per deployment, enforced in the database rather
            # than by gunicorn's worker count. A second process idles here
            # and takes over only if the holder stops heartbeating.
            if not _ops.acquire_scheduler_lease():
                holding = False
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
            if not holding:
                # Newly the runner: the runs a previous holder left open are
                # closed as interrupted, not left "stuck" for 45 days (#150).
                holding = True
                _ops.became_runner()
                _ops.close_orphaned_runs()
            _ops.begin_tick()
            # The heartbeat is stamped only at the END of a tick (DH2-2): a
            # tick that died after stamping it at the top — the 9/19
            # UnboundLocalError — left a fresh heartbeat over a loop that ran
            # nothing. A long job is still a live scheduler: the pulse
            # stamps it while the job runs (_PulsedOps).
            now   = _chi_now()
            today = now.date()

            # Run now, from the console: handed to this process, run here
            # under the lease beside nothing already running (#153, #64).
            _run_manual_requests(_ops)

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
                # Straight after the backup, so the pruned rows are in it:
                # ops.prune_ledgers (the one retention registry) and each
                # owner's review retention, nightly (#72, #81).
                _ops.run_job("prune_ledgers", run_nightly_retention)

            # Weekly Intel, claimed per ISO WEEK (DH2-10): due from Monday
            # 6am with Monday–Wednesday catch-up, so a lost Monday runs
            # Tuesday instead of next week; after it, a daily retry pass for
            # restaurants with no success this week. On the Intel lane,
            # beside the loop (#98): each is bounded at three hours and ran
            # inline, holding briefs, the DSR and intraday behind it. A lane
            # still busy gives the claim back, so a later tick starts it.
            _iso_week = now.strftime("%G-W%V")
            if _due(now, 6) and now.weekday() <= 2 and _ops.claim_period("competitor_analysis", _iso_week):
                log.info("Running weekly competitor analysis...")
                if not _ops.run_in_lane("intel", "competitor_analysis", run_weekly_competitor_analysis):
                    _ops.release_period("competitor_analysis", _iso_week)
            elif _due(now, 6) and _ops.period_claimed("competitor_analysis", _iso_week) and \
                    _ops.claim_period("competitor_retry", str(today)):
                if not _ops.run_in_lane("intel", "competitor_analysis", run_weekly_competitor_analysis,
                                        retry_only=True, claim="competitor_retry"):
                    _ops.release_period("competitor_retry", str(today))

            # An hour after the competitor run, so the two weekly Intel jobs
            # do not compete for the same minute.
            if _due(now, 7) and now.weekday() <= 2 and _ops.claim_period("ai_visibility", _iso_week):
                log.info("Running weekly AI visibility checks...")
                if not _ops.run_in_lane("intel", "ai_visibility", run_weekly_ai_visibility):
                    _ops.release_period("ai_visibility", _iso_week)
            elif _due(now, 7) and _ops.period_claimed("ai_visibility", _iso_week) and \
                    _ops.claim_period("ai_visibility_retry", str(today)):
                if not _ops.run_in_lane("intel", "ai_visibility", run_weekly_ai_visibility,
                                        retry_only=True, claim="ai_visibility_retry"):
                    _ops.release_period("ai_visibility_retry", str(today))

            if _due(now, 3) and _ops.claim_period("pos_sync", str(today)):
                log.info("Running nightly Toast POS sync...")
                _ops.run_job("pos_sync", run_toast_sync)

            # Hourly — the automatic-recovery pass over failed POS syncs
            # whose retry is due (+1h, +3h, +6h; until 11am local).
            if _ops.claim_period("pos_retry", f"{today}-{now.hour}"):
                _ops.run_job("pos_retry", run_pos_retry)

            # 3am+ — comps/voids/refunds from POSes that report them. After
            # pos_sync so the same night's data is settled first.
            if _due(now, 3) and _ops.claim_period("loss_sync", str(today)):
                from strategy_jobs import run_loss_sync
                _ops.run_job("loss_sync", run_loss_sync)

            # 3:30am — refresh the Stripe subscription mirror and record
            # where Stripe and the local billing state disagree
            # (billing_jobs.reconcile_stripe: bounded, resumable, reads
            # Stripe only — it never changes a billing status).
            if (now.hour, now.minute) >= (3, 30) and _ops.claim_period("stripe_reconcile", str(today)):
                from billing_jobs import reconcile_stripe
                _ops.run_job("stripe_reconcile", reconcile_stripe)

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

            # After the snapshot, so a closed week's waste figure is on file
            # before its forecast is scored.
            if _due(now, 5) and _ops.claim_period("forecast_scoring", str(today)):
                _ops.run_job("forecast_scoring", run_forecast_scoring)

            # 5am+ — what the nights just finished taught (event_memory): the
            # weather each day actually had, and the measured lift of every
            # listed event, holiday, rain night, payday and campaign, before
            # the morning briefs. Sends nothing; bounded and resumable.
            if _due(now, 5) and _ops.claim_period("event_memory", str(today)):
                import event_memory as _event_memory
                _ops.run_job("event_memory", _event_memory.run_event_memory)

            # 6am+ — one Data Health snapshot per restaurant, after the
            # nightly chain (POS, depletion, snapshots) has landed.
            if _due(now, 6) and _ops.claim_period("data_health_daily", str(today)):
                _ops.run_job("data_health_daily", run_data_health_daily)

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

            # 6am+, after the evaluations — re-check measured moves at 90
            # days and accrue measured dollars day by day (rec-ROI #14, #33;
            # strategy_jobs.run_outcome_rechecks). Sends nothing.
            if _due(now, 6) and _ops.claim_period("outcome_rechecks", str(today)):
                from strategy_jobs import run_outcome_rechecks
                _ops.run_job("outcome_rechecks", run_outcome_rechecks)

            # 6am+, after the evaluations and re-checks — the nightly
            # learning pass (learning_memory): score AI claims whose horizon
            # passed, summarise closed quarters of reads. Sends nothing.
            if _due(now, 6) and _ops.claim_period("learning_memory", str(today)):
                _ops.run_job("learning_memory", run_learning_memory)

            # 6am+, after the outcome evaluations — each restaurant's four
            # value figures into value_figures_daily, which the admin
            # Intelligence page sums (intelligence.dashboard, #57). On the
            # Intel lane: a bounded sweep of up to 45 minutes must not hold
            # the morning briefs; a busy lane gives the claim back.
            if _due(now, 6) and _ops.claim_period("value_figures", str(today)):
                from intelligence.dashboard import snapshot_value_figures
                if not _ops.run_in_lane("intel", "value_figures", snapshot_value_figures):
                    _ops.release_period("value_figures", str(today))

            # 6am+, after the outcome evaluations — each restaurant's own
            # value point (value_snapshots, the Home sparkline), dated on its
            # local day. It was written only when someone opened Home, so the
            # series had holes on exactly the days nobody looked (memory audit
            # 9/29/26). Bounded, resumable; sends nothing.
            if _due(now, 6) and _ops.claim_period("value_snapshots", str(today)):
                from value_delivered import run_value_snapshots
                _ops.run_job("value_snapshots", run_value_snapshots)

            # Hourly: each restaurant is told about a result or a milestone
            # at ITS OWN 9am (strategy_jobs.WIN_HOUR, local_due inside),
            # never at a Chicago hour that is 4am in Los Angeles (A-10). The
            # 6am evaluation above runs first everywhere west of Hawaii's 9am.
            if _ops.claim_period("outcome_wins", f"{today}-{now.hour}"):
                from strategy_jobs import run_outcome_wins
                _ops.run_job("outcome_wins", run_outcome_wins)
            if _ops.claim_period("milestones", f"{today}-{now.hour}"):
                from strategy_jobs import run_milestones
                _ops.run_job("milestones", run_milestones)

            # 8am — the recommendation trail (rec_ledger): answers held by
            # the older ledgers carried in (after 6am's outcome verdicts), and
            # recommendations nobody answered in two weeks closed as ignored.
            # repair_sync_replays first: it undoes what the replaying sync
            # wrote before it attached answers by time (a no-op after that),
            # and the sync then re-carries those answers where they belong.
            # backfill_tags tags episodes from before tags were stored (ROI
            # #17), bounded; it finishes the tail on later nights.
            if _due(now, 8) and _ops.claim_period("rec_ledger", str(today)):
                _ops.run_job("rec_ledger", run_rec_ledger_pass)

            # 9am local, per restaurant — what is waiting on each manager
            # before the next shifts: requests close to their date, a drafted
            # week not sent (strategy_jobs.run_labor_reminders).
            if _ops.claim_period("labor_reminders", f"{today}-{now.hour}"):
                from strategy_jobs import run_labor_reminders
                _ops.run_job("labor_reminders", run_labor_reminders)

            # Sunday 5am — let each restaurant's own clean and troubled weeks
            # nudge its quality weights (strategy_jobs.run_quality_calibration).
            if _due(now, 5) and now.weekday() == 6 and _ops.claim_period("quality_calibration", str(today)):
                from strategy_jobs import run_quality_calibration
                _ops.run_job("quality_calibration", run_quality_calibration)

            # Monday 4am — what each published week actually did, by daypart.
            if _due(now, 4) and now.weekday() == 0 and _ops.claim_period("schedule_outcomes", str(today)):
                from strategy_jobs import run_schedule_outcomes
                _ops.run_job("schedule_outcomes", run_schedule_outcomes)

            # 5am daily, once the 3am POS sync (45 minutes at most) is in —
            # what last night taught about the people: attendance from the
            # punches and the checks, covers taken, guest mentions, standing
            # schedule patterns, the quarterly summaries (memory audit
            # 9/29/26). Sends nothing.
            if _due(now, 5) and _ops.claim_period("people_nightly", str(today)):
                from strategy_jobs import run_people_nightly
                _ops.run_job("people_nightly", run_people_nightly)

            # 5am daily — reservation feeds into demand_signals for each
            # restaurant whose draft is tomorrow (its auto_draft_weekday; the
            # Wednesday run served only the Thursday draft). No provider is
            # live yet; unconfigured restaurants are not read.
            if _due(now, 5) and _ops.claim_period("reservation_sync", str(today)):
                from reservation_feeds import run_reservation_sync
                _ops.run_job("reservation_sync", run_reservation_sync, weekday=now.weekday())

            # Hourly — draft next week's schedule for owners who opted in,
            # each on ITS day from 6am in its own zone (Thursday unless the
            # owner picked another; run_auto_draft_schedules selects them).
            # A draft in Schedule History; nothing reaches staff. Each pass
            # is time-bounded and starts at the cursor, and a restaurant is
            # attempted once a day, so a pass that ran out of time is
            # finished later the same day, not next week (SCHED-11).
            if _ops.claim_period("auto_draft_schedule", f"{today}-{now.hour}"):
                from strategy_jobs import run_auto_draft_schedules
                _ops.run_job("auto_draft_schedule", run_auto_draft_schedules, now=now)

            # Monday 7am local — the agent files the week's three actions.
            if now.weekday() == 0 and _ops.claim_period("weekly_plan", f"{today}-{now.hour}"):
                from strategy_jobs import run_weekly_plan
                _ops.run_job("weekly_plan", run_weekly_plan)

            # Tuesday 5am local — recipe drafts for dishes with none.
            if now.weekday() == 1 and _ops.claim_period("recipe_drafts", f"{today}-{now.hour}"):
                from strategy_jobs import run_recipe_drafts
                _ops.run_job("recipe_drafts", run_recipe_drafts)

            # Hourly: 8am local on each restaurant's order day (Monday unless
            # the owner picked another) — queue trusted supplier orders with
            # an hour to undo (strategy_jobs.run_trusted_orders).
            if _ops.claim_period("trusted_orders", f"{today}-{now.hour}"):
                from strategy_jobs import run_trusted_orders
                _ops.run_job("trusted_orders", run_trusted_orders)

            # Hourly: 9am local the day after each restaurant's draft day
            # (Friday for a Thursday draft) — queue the unedited draft to
            # publish at 11am with an undo window (run_auto_publish_schedules).
            if _ops.claim_period("auto_publish_schedule", f"{today}-{now.hour}"):
                _ops.run_job("auto_publish_schedule", run_auto_publish_schedules)

            if _due(now, 4) and _ops.claim_period("marketing_metrics_sync", str(today)):
                log.info("Running marketing metrics sync...")
                _ops.run_job("marketing_metrics_sync", run_marketing_metrics_sync)

            # Hourly from 7am: a restaurant whose refresh failed is retried
            # within the day (refresh_expiring_tokens caps the attempts).
            if _due(now, 7) and _ops.claim_period("refresh_tokens", f"{today}-{now.hour}"):
                log.info("Refreshing expiring IG/FB tokens...")
                _ops.run_job("refresh_tokens", refresh_expiring_tokens)

            # Fetch every 4 hours: 8am, 12pm, 4pm, 8pm Chicago time
            _fetch_slot = _latest_slot(now, (8, 12, 16, 20))
            if _fetch_slot is not None and _ops.claim_period("review_fetch", f"{today}-{_fetch_slot}"):
                log.info(f"Running review fetch for the {_fetch_slot}:00 CT slot "
                         f"(now {now.hour}:{now.minute:02d})...")
                _ops.run_job("review_fetch", run_daily_fetch)

            # Hourly — one authenticated, non-sending call per provider; a
            # provider that starts failing pages Will once (provider_health,
            # #29). Pages only where scheduling is allowed.
            if _ops.claim_period("provider_probes", f"{today}-{now.hour}"):
                import provider_health as _provider_health
                _ops.run_job("provider_probes", _provider_health.run_probes)

            # Hourly — dunning's safety net: owe the email for a failed
            # invoice attempt the webhook could not, stand down dunning for
            # invoices since paid, then send (billing_jobs.run_dunning, #25).
            if _ops.claim_period("dunning", f"{today}-{now.hour}"):
                from billing_jobs import run_dunning
                _ops.run_job("dunning", run_dunning)

            # 10am daily — the pay link again on days 2, 5 and 9 after
            # signing to a client who has not paid (#26; trials never expire).
            if _due(now, 10) and _ops.claim_period("contract_chase", str(today)):
                from billing_jobs import run_contract_chase
                _ops.run_job("contract_chase", run_contract_chase)

            # 8am daily — operator failure digest (only sends if something
            # failed, stuck, is overdue or the backup is unhealthy). A digest
            # that did not go out gives the day back, up to three more tries
            # (#33): the claim was spent and the day's failures never mailed.
            if _due(now, 8) and _ops.claim_period("ops_digest", str(today)):
                if _ops.run_job("ops_failure_digest", _ops.send_failure_digest, claim="ops_digest") is None \
                        and any(_ops.claim_period("ops_digest_retry", f"{today}#{n}") for n in range(3)):
                    _ops.release_period("ops_digest", str(today))

            # Monday 7am — one operator digest of the week (#35).
            if _due(now, 7) and now.weekday() == 0 and _ops.claim_period("operator_weekly_digest", str(today)):
                _ops.run_job("operator_weekly_digest", _ops.send_operator_weekly_digest)

            # Attempted hourly: each restaurant is gated on ITS 9am inside
            # (local_due), so one Chicago-timed daily claim would serve only
            # the restaurants whose local hour happened to match.
            if _ops.claim_period("weekly_digest", f"{today}-{now.hour}"):
                log.info("Running weekly digest check...")
                _ops.run_job("weekly_digests", run_weekly_digests, claim="weekly_digest")

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
                _ops.run_job("quarterly_summaries", run_quarterly_summaries, claim="quarterly_summary")

            # Attempted hourly so each restaurant is served at 9am in its own
            # timezone; run_monthly_summaries gates on the restaurant's own
            # 1st of the month (local_due day=1) and claims once per day.
            if _ops.claim_period("monthly_summary", f"{today}-{now.hour}"):
                log.info("Running monthly summary emails...")
                _ops.run_job("monthly_summary", run_monthly_summaries)

            if _ops.claim_period("onboarding", f"{today}-{now.hour}"):
                # 10am daily — onboarding email sequence
                log.info("Running onboarding sequence check...")
                _ops.run_job("onboarding_emails", run_onboarding_sequence, local_hour=10, claim="onboarding")

            # Hourly, each restaurant at ITS 11am (local_due inside) — one
            # nudge per missing setup step, each step once (#41).
            if _ops.claim_period("onboarding_nudges", f"{today}-{now.hour}"):
                _ops.run_job("onboarding_nudges", run_onboarding_nudges, local_hour=11)

            if _due(now, 11) and now.weekday() == 0 and _ops.claim_period("inactive_clients", str(today)):
                # Monday 11am — inactive client check
                log.info("Running inactive client check...")
                _ops.run_job("inactive_clients", check_inactive_clients)
                _ops.run_job("while_away", send_while_away_nudges, claim="inactive_clients")

            if (_due(now, 11, until=OPTIN_INVITE_LATEST_HOUR)
                    and _ops.claim_period("optin_invite", f"{today}-{now.hour}")):
                # From 11am, hourly — invite guests the POS identified at
                # each restaurant's last CLOSED business day to ask for a
                # review link; only restaurants whose owner turned invites
                # on (Campaigns -> Settings, MB-3). The date is each
                # restaurant's own (the job computes it): the server's
                # "yesterday" was still an open day for a restaurant behind
                # UTC (MB-19). Hourly, not once: 11am here is 6am in Hawaii,
                # and a restaurant outside its 8am-9pm window was deferred
                # and never retried (MOD-MKT-12). The job skips restaurants
                # it already finished for their date, so later passes are
                # cheap, and it is bounded with a cursor (MB-18).
                log.info("Running Toast opt-in invites...")
                from guest_marketing import run_toast_optin_invites
                _ops.run_job("toast_optin_invites", lambda: run_toast_optin_invites(),
                             claim="optin_invite")

            # 3am — the intelligence engine's feature pass (bounded, resumable),
            # then 4am learning over the materialized tables. INTELLIGENCE_ENGINE.md.
            if _due(now, 3) and _ops.claim_period("intelligence_features", str(today)):
                from intelligence import jobs as _intel_jobs
                _ops.run_job("intelligence_features", _intel_jobs.run_features)
            if _due(now, 4) and _ops.claim_period("intelligence_learning", str(today)):
                from intelligence import jobs as _intel_jobs
                _ops.run_job("intelligence_learning", _intel_jobs.run_learning)
            # 5am — past feature weeks from the raw tables (bounded,
            # resumable; memory audit PLATFORM-5), after the night's own
            # feature and learning passes.
            if _due(now, 5) and _ops.claim_period("intelligence_features_backfill", str(today)):
                from intelligence import jobs as _intel_jobs
                _ops.run_job("intelligence_features_backfill", _intel_jobs.run_features_backfill)
            # Monday 5am — the schedule A/B's clustered verdict, stored for
            # the week (memory audit PLATFORM-13).
            if _due(now, 5) and now.weekday() == 0 and _ops.claim_period("schedule_experiment_verdicts", str(today)):
                import schedule_experiments as _sx
                _ops.run_job("schedule_experiment_verdicts", _sx.record_verdicts)

            # Noon daily — which campaign recipients Toast saw on a later
            # check (guest_marketing.run_campaign_attribution). Reads each
            # restaurant's closed business days on its own calendar; each
            # date fetched once per restaurant; bounded with a cursor.
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

            # Hourly attempt, Monday morning in each restaurant's own zone —
            # a review-request nudge with its measured conversion, at most
            # weekly (strategy_jobs.run_review_request_nudge, resumable).
            if _ops.claim_period("review_request_nudge", f"{today}-{now.hour}"):
                from strategy_jobs import run_review_request_nudge
                _ops.run_job("review_request_nudge", run_review_request_nudge)

            # During service — the only part of the product that can see a
            # day while it is happening (Toast reads; RPOWER is month-at-a-
            # time and says so). Capture is hourly per restaurant, the pulse
            # is one push before dinner, coverage runs while they're open.
            if _ops.claim_period("intraday", f"{today}-{now.hour}-{now.minute // 20}"):
                # The restaurants are read ONCE per slot and handed to all
                # six jobs; each is a bounded, resumable sweep with its own
                # cursor (strategy_jobs._slot_sweep, DH5-9).
                from strategy_jobs import (run_intraday_capture, run_pre_dinner_pulse, run_coverage_check,
                                           slot_restaurants)
                _slot = slot_restaurants()
                _ops.run_job("intraday_capture", run_intraday_capture, restaurants=_slot, claim="intraday")
                _ops.run_job("pre_dinner_pulse", run_pre_dinner_pulse, restaurants=_slot, claim="intraday")
                _ops.run_job("coverage_check", run_coverage_check, restaurants=_slot, claim="intraday")
                from strategy_jobs import run_preshift_nudge
                _ops.run_job("preshift_nudge", run_preshift_nudge, restaurants=_slot, claim="intraday")
                # How tonight went, once the doors are shut — the one part
                # of the day nothing reported on while the owner could
                # still picture the room.
                from strategy_jobs import run_closing_summary
                _ops.run_job("closing_summary", run_closing_summary, restaurants=_slot, claim="intraday")
                # A quiet night two days out, once a week — the one area of
                # the product that produced no notification at all.
                from strategy_jobs import run_demand_opportunity
                _ops.run_job("demand_opportunity", run_demand_opportunity, restaurants=_slot, claim="intraday")

            # Every tick, claimed per 10-minute slot — the nightly DSR
            # (dsr.pipeline.run_sweep): each restaurant past its OWN close,
            # the POS close-day poll, block retries, the provisional
            # deadline and late-data versions. Bounded and resumable inside;
            # each night claims (restaurant, business date, version).
            if _ops.claim_period("dsr_sweep", f"{today}-{now.hour}-{now.minute // 10}"):
                from dsr.pipeline import run_sweep
                _ops.run_job("dsr_sweep", run_sweep)

            # Every tick, claimed per 10-minute slot — DSR pushes held through
            # a restaurant's quiet hours go out once they end
            # (dsr.deliver.release_held). Bounded; the held rows are the
            # queue, and each is taken (held -> sending) before it is sent.
            if _ops.claim_period("dsr_delivery", f"{today}-{now.hour}-{now.minute // 10}"):
                from dsr.deliver import release_held
                _ops.run_job("dsr_delivery", release_held)

            # Every tick — scheduled posts, delayed actions whose undo window
            # closed, issue escalations, alerts held through a rush. Skipped
            # when a job's pulse ran them within the last interval.
            if _ops.duties_due():
                _ops.run_duties()

            # Every tick — the billing mail a signing or a Stripe event owes
            # (receipts, dunning, the set-password welcome, pay reminders):
            # each owed_sends row is claimed before its send and retried with
            # backoff (billing_jobs, #12). Stripe-originated mail is sent only
            # from here.
            from billing_jobs import run_owed_sends
            _ops.run_job("owed_sends", run_owed_sends)

            # 11:50pm CT — the day's business metrics (business_metrics_daily,
            # never pruned) and each account's churn-risk state (#18, #83).
            # Idempotent. A night the loop missed, or a failed run's retry,
            # is taken before 6am for the day it belongs to.
            _metrics_day = today if (now.hour, now.minute) >= (23, 50) else (
                today - timedelta(days=1) if now.hour < 6 else None)
            if _metrics_day is not None and _ops.claim_period("business_metrics", str(_metrics_day)):
                from admin_ops import snapshot_business_metrics
                _ops.run_job("business_metrics", snapshot_business_metrics, day=str(_metrics_day))

            # Every tick — morning briefs go at each restaurant's own local
            # hour and claim themselves per restaurant per day. A job run of
            # its own, with the pulse (#31): it wrote none at all.
            import morning_brief as _mb
            _ops.run_job("morning_brief", _mb.run_due)

            # Daily — drop login-attempt rows older than two days.
            if _ops.claim_period("prune_login_attempts", str(today)):
                _ops.run_job("prune_login_attempts", run_prune_login_attempts)

            try:
                run_health_checks()
            except Exception:
                pass
            # The end of the tick: everything above got the chance to run.
            # loop_completed stamps the proof that the WHOLE tick ran (#121),
            # and the external dead-man monitor hears from us (#3).
            try:
                record_scheduler_heartbeat(loop_completed=True)
            except Exception:
                pass
            _ops.tick_completed()

        except Exception as e:
            # Captured, not only logged (DH2-2): an exception here skips the
            # rest of the tick AND the heartbeat, and the operator hears it
            # from the digest and from the request-path watchdog
            # (ops.check_platform_sla), not from an owner.
            log.error(f"Scheduler loop error: {e}")
            try:
                _ops.capture(e, job="scheduler_loop", context="tick")
            except Exception:
                pass

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


def _run_scheduler_thread():
    """The scheduler thread's body: the lease keeper — which renews the lease
    every minute and says so, so the next process takes over within minutes
    of this one dying rather than 30 (#134) — then the loop."""
    _LEASE_KEEPER.start()
    scheduler_loop()


def start_scheduler():
    if not scheduling_allowed():
        log.warning("Scheduler NOT started: not on Railway. Set ALLOW_LOCAL_SCHEDULER=1 to run "
                    "jobs locally — they send real email and SMS.")
        print("Scheduler not started (local) — set ALLOW_LOCAL_SCHEDULER=1 to run jobs here")
        return None
    # Runs a previous process left open (a deploy SIGKILLed it mid-job) are
    # closed as interrupted at boot, so "Stuck" and the digest stop
    # reporting them and Run now is not blocked behind them (#150).
    _ops.close_orphaned_runs()
    t = threading.Thread(target=_run_scheduler_thread, daemon=True)
    t.start()
    # A redeploy SIGTERMs this process; gunicorn exits the worker cleanly and
    # atexit runs, so the replacement takes the lease on its next tick rather
    # than 30 minutes later. shutdown_scheduler first marks this process as
    # exiting, so the loop, a pulse or the lease keeper — daemon threads still
    # running — cannot take the lease back after it is released (#161).
    import atexit
    atexit.register(_ops.shutdown_scheduler)
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
    # The public-reply check (drafter.check_reply: the Response Validation
    # Layer on reply_public). It reads what the restaurant has said about
    # itself (voice and menu notes — a cause, a sourcing claim or an award is
    # allowed only when its words are there or in the guest's own review),
    # the never-say list, the guest's name and every other tenant's name.
    from drafter import check_reply, REWORD_REVIEW_REASON
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
        # The engine refuses the injection residue check_public_reply always
        # caught, the never-say list, and the claims a reply may not make
        # unread — allergen/"safe for" promises, fault, inspections, comps,
        # "won't happen again", invented causes (NS5 H5, NS1 H6, NS2 H8),
        # awards, a staff member named in public, another tenant's name.
        draft_text = candidate.get("draft_response") or ""
        refusal, checked = check_reply(draft_text, restaurant, restaurant_id=rid, review_id=review_id,
                                       review_text=candidate.get("text") or "", action="auto_approve")
        if not refusal and " ".join(str(checked).split()) != " ".join(draft_text.split()):
            # The engine would reword it (a certainty or confidence phrase
            # lowered); what would publish is the stored draft, which it did
            # not pass as written. A person reads it first.
            refusal = REWORD_REVIEW_REASON
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
                            # The plain reason: every surface reads it after
                            # "This reply …" (drafter.owner_reason). Which
                            # path held it is the log line above.
                            (refusal, review_id, rid))
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
            payload, status = _do_approve(review_id, rid, auto=True)
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
