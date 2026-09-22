"""
gmb.py — Google My Business (Business Profile API) OAuth + review reply posting
"""
import os, requests
from datetime import datetime, timezone, timedelta

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI         = os.getenv("GMB_REDIRECT_URI", "https://dashboard.cavnar.ai/auth/google/callback")
# A separate, dedicated redirect URI for the mobile connect flow (see
# get_mobile_auth_url/verify_mobile_state below) — this exact URL must
# also be added as an Authorized redirect URI on the Google Cloud Console
# OAuth client, alongside the existing web one, or Google will reject the
# exchange with redirect_uri_mismatch.
MOBILE_REDIRECT_URI  = os.getenv("GMB_MOBILE_REDIRECT_URI", "https://dashboard.cavnar.ai/auth/google/mobile-callback")

SCOPES = "https://www.googleapis.com/auth/business.manage"

# ── OAuth helpers ─────────────────────────────────────────────────────────────

def get_auth_url(restaurant_id: int, nonce: str) -> str:
    """Build Google OAuth URL. state carries both the restaurant_id and a
    per-flow nonce the caller must verify against a cookie on callback, so
    the callback can't be tricked into attaching tokens to an arbitrary
    restaurant_id just by forging the query string."""
    state = f"{nonce}:{restaurant_id}"
    return (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&state={state}"
        f"&access_type=offline"
        f"&prompt=consent"
    )


def get_mobile_auth_url(restaurant_id: int) -> str:
    """Mobile equivalent of get_auth_url — the app has no session cookie to
    bind a CSRF nonce to (see verify_mobile_state), so state here is a
    signed, self-verifying token instead of a bare nonce."""
    state = sign_mobile_state(restaurant_id)
    return (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={MOBILE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&state={state}"
        f"&access_type=offline"
        f"&prompt=consent"
    )


def exchange_code(code: str, redirect_uri: str = None) -> dict:
    """Exchange auth code for access + refresh tokens. redirect_uri must
    match whatever was used to build the authorize URL exactly, or Google
    rejects the exchange — defaults to the web flow's REDIRECT_URI."""
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code":          code,
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri":  redirect_uri or REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _state_secret() -> bytes:
    """The key that signs the mobile connect state. It used to default to a
    literal 'cavnar-dev-secret' when SECRET_KEY was unset — a token anyone
    could forge. hosted_dashboard refuses to run sessions without a
    SECRET_KEY (it generates a random one and warns), so the only honest
    answer here is the same: no key, no signing."""
    secret = os.getenv("SECRET_KEY")
    if not secret:
        raise RuntimeError("SECRET_KEY is not set; cannot sign the Google connect state")
    return secret.encode()


def sign_mobile_state(restaurant_id: int) -> str:
    """Self-contained, storage-free CSRF token for the mobile GMB connect
    flow. Encodes restaurant_id plus a 10-minute expiry, HMAC-signed with
    the app's SECRET_KEY. The web flow binds its nonce to a gmb_oauth_state
    cookie because a browser session already carries one; the mobile app
    authenticates with a bearer token instead, so there's no cookie for an
    unauthenticated callback redirect to present — the signature itself is
    what proves this callback traces back to a request this server issued,
    for this restaurant, recently."""
    import hmac, hashlib, time
    secret  = _state_secret()
    expires = int(time.time()) + 600
    payload = f"{restaurant_id}:{expires}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}:{sig}"


def verify_mobile_state(state: str) -> int | None:
    """Returns the restaurant_id if state is a valid, unexpired token from
    sign_mobile_state(); None if it's missing, tampered with, or expired."""
    import hmac, hashlib, time
    try:
        rid_s, expires_s, sig = state.split(":")
        secret   = _state_secret()
        payload  = f"{rid_s}:{expires_s}"
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            return None
        if int(expires_s) < int(time.time()):
            return None
        return int(rid_s)
    except (ValueError, AttributeError, RuntimeError):
        return None


class GoogleTokenRevoked(RuntimeError):
    """Google answered invalid_grant: the refresh token is dead (revoked by
    the owner, expired, or superseded) and will never work again."""


def refresh_access_token(refresh_token: str) -> dict:
    """Get a new access token using the refresh token.

    Raises GoogleTokenRevoked for invalid_grant — the one answer that means
    the connection is gone. Only that one: any other 4xx (invalid_client,
    unauthorized_client) is a problem with OUR OAuth client, the same for
    every restaurant, and must not read as each owner's dead connection."""
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "refresh_token": refresh_token,
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "grant_type":    "refresh_token",
    }, timeout=10)
    if resp.status_code in (400, 401):
        try:
            error = (resp.json() or {}).get("error")
        except Exception:
            error = None
        if error == "invalid_grant":
            raise GoogleTokenRevoked("Google refresh token revoked or expired (invalid_grant)")
    resp.raise_for_status()
    return resp.json()


class GoogleTokenUnavailable(RuntimeError):
    """The refresh failed for a reason that says nothing about the
    connection: a timeout, a dropped connection, a Google 5xx, or an error
    from Google that is not invalid_grant. Not "reconnect Google" (AI-22,
    MOD-REV-12)."""


# restaurant_id -> the last refresh failure's exception type name, for a
# refresh that failed TRANSIENTLY and returned None. Cleared on the next
# success or revocation. Process-local and bounded by restaurant count.
_transient_refresh_failures = {}


def refresh_failed_transiently(restaurant_id: int) -> str | None:
    """Why the last get_valid_token for this restaurant returned None when
    the connection itself is fine (a timeout, a dropped connection, a Google
    5xx, a non-invalid_grant error), or None. The daily fetch asks this
    before telling the owner to reconnect (AI-22, MOD-REV-12)."""
    return _transient_refresh_failures.get(restaurant_id)


def get_valid_token(restaurant_id: int) -> str | None:
    """
    Return a valid access token for the restaurant, refreshing if needed.
    Returns None if not connected — including when Google has revoked the
    refresh token (invalid_grant), which is also cleared here so it is not
    re-tried four times a day forever (MOD-REV-12).

    Any other refresh failure also returns None, but is recorded for
    refresh_failed_transiently(): as far as the connection is concerned it
    is a blip, not "reconnect Google".
    """
    from models import get_restaurant, update_restaurant
    r = get_restaurant(restaurant_id)
    if not r or not r.gmb_refresh_token:
        return None

    # Check if token is still valid (with 5-minute buffer)
    if r.gmb_token_expires and r.gmb_access_token:
        try:
            expires = datetime.fromisoformat(r.gmb_token_expires.replace("Z", ""))
            if datetime.now(timezone.utc) < expires - timedelta(minutes=5):
                return r.gmb_access_token
        except Exception:
            pass

    # Refresh the token
    try:
        tokens = refresh_access_token(r.gmb_refresh_token)
        access_token = tokens["access_token"]
        expires_in   = tokens.get("expires_in", 3600)
        expires_at   = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
        update_restaurant(restaurant_id, {
            "gmb_access_token":  access_token,
            "gmb_token_expires": expires_at,
        })
        _transient_refresh_failures.pop(restaurant_id, None)
        return access_token
    except Exception as e:
        print(f"[GMB] Token refresh failed for restaurant {restaurant_id}: {e}")
        if isinstance(e, GoogleTokenRevoked):
            _transient_refresh_failures.pop(restaurant_id, None)
            try:
                update_restaurant(restaurant_id, {"gmb_refresh_token": None,
                                                  "gmb_access_token": None,
                                                  "gmb_token_expires": None})
            except Exception as _clear:
                print(f"[GMB] could not clear revoked token for {restaurant_id}: {_clear}")
            return None
        _transient_refresh_failures[restaurant_id] = type(e).__name__
        return None


# ── Account/Location discovery ────────────────────────────────────────────────

def list_gmb_accounts(access_token: str) -> list:
    """Every GBP account this token can see, newest API shape."""
    try:
        resp = requests.get(
            "https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("accounts", []) or []
    except Exception as e:
        print(f"[GMB] list_gmb_accounts error: {e}")
        return []


def get_gmb_account_id(access_token: str) -> str | None:
    """First GBP account id. Only meaningful when the token sees exactly one;
    callers that need a specific location should use find_gmb_location, which
    searches every account rather than assuming this one."""
    accounts = list_gmb_accounts(access_token)
    return accounts[0]["name"] if accounts else None


def list_gmb_locations(access_token: str, account_id: str) -> list:
    """Locations under one account, with the Place ID each one maps to.

    metadata.placeId is the field that ties a GBP location to the
    google_place_id already stored on the restaurant. It has to be asked
    for explicitly in readMask or it simply is not returned.
    """
    out, page_token = [], None
    try:
        for _ in range(10):  # bounded; 100 per page covers any real group
            params = {
                "readMask": "name,title,storefrontAddress,metadata",
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            resp = requests.get(
                f"https://mybusinessbusinessinformation.googleapis.com/v1/{account_id}/locations",
                headers={"Authorization": f"Bearer {access_token}"},
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("locations", []) or [])
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        print(f"[GMB] list_gmb_locations error: {e}")
    return out


def find_gmb_location(access_token: str, place_id: str) -> dict:
    """Resolve the ONE GBP location that matches this restaurant's Place ID.

    Returns {"ok": True, "account": ..., "location": ..., "title": ...} or
    {"ok": False, "error": ..., "choices": [...]}.

    This used to be get_gmb_location_id(access_token, account_id, place_id),
    which accepted place_id and never read it — it returned locations[0] of
    accounts[0]. An owner with three restaurants under one Business Profile
    connected all three to the same location, so every one of them fetched
    the same reviews and post_reply published each restaurant's drafts onto
    that single listing. The reviews table was deliberately re-keyed per
    restaurant to stop exactly this; the location lookup undid it upstream.

    Matching on nothing is never safe here, so an unmatched Place ID is an
    error carrying the candidates rather than a silent first-item pick.
    """
    want = (place_id or "").strip()
    if not want:
        return {"ok": False, "error": "This restaurant has no Google Place ID on file. "
                                      "Add one before connecting Google Business Profile."}
    accounts = list_gmb_accounts(access_token)
    if not accounts:
        return {"ok": False, "error": "This Google account manages no Business Profile locations."}

    choices = []
    for acct in accounts:
        acct_name = acct.get("name")
        if not acct_name:
            continue
        for loc in list_gmb_locations(access_token, acct_name):
            loc_place = ((loc.get("metadata") or {}).get("placeId") or "").strip()
            title = loc.get("title") or loc.get("name") or "(untitled)"
            choices.append({"account": acct_name, "location": loc.get("name"),
                            "title": title, "place_id": loc_place})
            if loc_place and loc_place == want:
                return {"ok": True, "account": acct_name,
                        "location": loc.get("name"), "title": title}

    return {"ok": False,
            "error": ("None of the locations on this Google account match this restaurant's "
                      "Place ID. Connect the Google account that manages this specific "
                      "listing, or correct the Place ID in settings."),
            "choices": choices}


# ── Review fetching via Business Profile API ─────────────────────────────────

# Google returns at most 50 reviews per page. Without following
# nextPageToken a restaurant with years of history imported its newest 50
# and never backfilled the rest — and since save_reviews is keyed on
# external_id, the older ones were not "pending", they were simply never
# seen. Bounded so one restaurant's 12-year backlog cannot hold the single
# fetch thread for every other restaurant in the cycle; the next run picks
# up where this one stopped, because anything already stored is a no-op.
GMB_REVIEW_PAGE_SIZE = 50
GMB_MAX_REVIEW_PAGES = 20


class GbpReviews(list):
    """The reviews one fetch returned, plus `complete`: True only when the
    fetch walked the listing from its first page to its last, so a stored
    review this listing does not contain is one Google no longer lists
    (MOD-REV-14). An incremental fetch that stopped early is not complete."""
    complete = False


def _known_review_names(restaurant_id, names):
    """Which of these GBP resource names this restaurant already stores."""
    if not names:
        return set()
    from models import get_conn
    conn = get_conn()
    try:
        marks = ",".join("?" * len(names))
        rows = conn.execute(
            f"SELECT external_id FROM reviews WHERE restaurant_id=? AND platform='google' "
            f"AND external_id IN ({marks})", (restaurant_id, *names)).fetchall()
        return {r["external_id"] for r in rows}
    finally:
        conn.close()


def _backfill_key(restaurant_id, location_id):
    return f"gbp_backfill:{restaurant_id}:{location_id}"


def _backfill_token(restaurant_id, location_id, value=False):
    """Read (value=False) or write (value=str/None) the page token a capped
    history walk stopped at. job_cursors holds it, so the next run continues
    there instead of starting at page 1 again (MOD-REV-7)."""
    from models import get_conn
    key = _backfill_key(restaurant_id, location_id)
    try:
        conn = get_conn()
        try:
            if value is False:
                row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
                return row["value"] if row and row["value"] else None
            if value:
                conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                             "updated_at=excluded.updated_at", (key, value))
            else:
                conn.execute("DELETE FROM job_cursors WHERE key=?", (key,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[GMB] backfill cursor {key}: {e}")
    return None


def _local_review_time(stamp, restaurant_id):
    """A GBP createTime (UTC, "…Z") as the restaurant's local wall-clock time,
    the way fetcher stores a Places review. Stored verbatim, a 9:30pm CDT
    review was dated the next day on every day/week/month axis (MOD-REV-9)."""
    if not stamp:
        return stamp
    try:
        from datetime import datetime as _dt
        from fetcher import _restaurant_tz
        text = stamp.replace("Z", "+00:00")
        if "." in text:
            head, tail = text.split(".", 1)
            zone = tail[tail.find("+"):] if "+" in tail else ""
            text = head + zone
        when = _dt.fromisoformat(text)
        if when.tzinfo is None:
            return stamp
        return when.astimezone(_restaurant_tz(restaurant_id)).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return stamp


def fetch_reviews_via_gmb(access_token: str, location_id: str, restaurant_id: int) -> list:
    """
    Fetch reviews using the current Business Profile Reviews API.
    location_id format: "locations/456"

    Raises on API/transport failure rather than returning []. It used to
    swallow every exception and return an empty list, which the caller
    could not tell apart from "nothing new today" — so scheduler.py set
    fetched_ok=True, stamped last_fetched_at, and the 25-hour staleness
    monitor built to catch a dead Google connection never fired. Reviews
    stopped arriving permanently while every health indicator read green.
    fetcher.fetch_google already raises; this now matches it.

    Two walks share GMB_MAX_REVIEW_PAGES (MOD-REV-7):
      * new reviews, from page 1, stopping at the first page this
        restaurant already stores entirely — an incremental fetch used to
        walk all 20 pages every run;
      * history, continuing from the page token the last capped walk
        stopped at (job_cursors) — every run used to start at page 1, so
        nothing past the newest 1,000 was ever fetched.
    """
    from models import Review

    def _page(token):
        params = {"pageSize": GMB_REVIEW_PAGE_SIZE}
        if token:
            params["pageToken"] = token
        resp = requests.get(
            f"https://mybusinessreviews.googleapis.com/v1/{location_id}/reviews",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("reviews") or [], body.get("nextPageToken")

    raw = []
    budget = GMB_MAX_REVIEW_PAGES
    complete = False
    stored_backfill = _backfill_token(restaurant_id, location_id)
    page_token = None
    while budget > 0:
        page, page_token = _page(page_token)
        budget -= 1
        raw.extend(page)
        if not page_token:
            complete = True         # walked from page 1 to the end
            break
        names = [r.get("name") for r in page if r.get("name")]
        if names and len(_known_review_names(restaurant_id, names)) == len(names):
            break                   # everything from here down is already stored
    if page_token and not stored_backfill and budget == 0:
        # A first import capped by the page budget: remember where it stopped.
        _backfill_token(restaurant_id, location_id, page_token)
        print(f"[GMB] stopped at {GMB_MAX_REVIEW_PAGES} pages for {location_id} — "
              f"more history remains, next run continues")
    elif stored_backfill and not complete:
        token = stored_backfill
        while budget > 0 and token:
            page, token = _page(token)
            budget -= 1
            raw.extend(page)
        _backfill_token(restaurant_id, location_id, token or None)
    elif complete and stored_backfill:
        _backfill_token(restaurant_id, location_id, None)
    try:
        reviews = []
        for r in raw:
            star_map = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
            raw_star = r.get("starRating")
            rating = star_map.get(raw_star)
            if rating is None:
                # Defaulted to 3. Google really does return
                # STAR_RATING_UNSPECIFIED, and a fabricated neutral rating
                # enters the average, the sentiment mix and the owner's
                # headline number as if a guest had chosen it. Skip instead.
                print(f"[GMB] skipping review with unusable starRating {raw_star!r}")
                continue
            reviewer = r.get("reviewer", {})
            author = reviewer.get("displayName") or "Anonymous"
            text = r.get("comment", "")
            # createTime, not updateTime. updateTime moves when a review is
            # edited OR when anybody replies to it, so importing a backlog a
            # previous agency had replied to dated every one of those
            # reviews to the day of the reply — and save_reviews is
            # insert-only, so that wrong date was then frozen for good.
            create_time = r.get("createTime") or r.get("updateTime") or ""
            update_time = r.get("updateTime") or ""
            review_name = r.get("name", "")  # e.g. accounts/123/locations/456/reviews/789

            reviews.append(Review(
                restaurant_id=restaurant_id,
                platform="google",
                external_id=review_name or f"google_{create_time}_{author}",
                author=author,
                rating=rating,
                text=text,
                review_date=_local_review_time(create_time, restaurant_id),
                review_name=review_name,
                source_updated_at=update_time,
            ))
        out = GbpReviews(reviews)
        out.complete = complete
        return out
    except Exception as e:
        # Parsing failures only — the HTTP call above is deliberately left
        # to raise so the caller can tell a dead connection from a quiet day.
        print(f"[GMB] fetch_reviews_via_gmb parse error: {e}")
        raise


def retire_unlisted_reviews(restaurant_id: int, location_id: str, listed_names: list) -> int:
    """Soft-delete this location's stored GBP reviews that a COMPLETE listing
    no longer contains — Google removed them, so they stop counting in the
    rating, the stats and the reply queue (MOD-REV-14). save_reviews is
    insert-or-edit only, so a fake one-star Google took down kept dragging
    the average forever.

    Refuses when the listing is empty or would retire more than half of what
    is stored: that is an API glitch or a wrong location, not a purge."""
    from models import get_conn
    listed = set(n for n in listed_names if n)
    if not listed or not location_id:
        return 0
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, review_name FROM reviews WHERE restaurant_id=? AND platform='google' "
            "AND deleted_at IS NULL AND review_name LIKE ?",
            (restaurant_id, f"%/{location_id}/reviews/%")).fetchall()
        gone = [r["id"] for r in rows if r["review_name"] not in listed]
        if not gone:
            return 0
        if len(gone) * 2 > len(rows):
            print(f"[GMB] not retiring {len(gone)} of {len(rows)} reviews for {location_id}: "
                  f"too many to be removals")
            return 0
        conn.executemany("UPDATE reviews SET deleted_at=datetime('now') WHERE id=?", [(i,) for i in gone])
        conn.commit()
        print(f"[GMB] {len(gone)} review(s) no longer on Google retired for restaurant {restaurant_id}")
        return len(gone)
    finally:
        conn.close()


# ── Reply posting ─────────────────────────────────────────────────────────────

def post_reply(restaurant_id: int, review_name: str, reply_text: str) -> dict:
    """
    Post a reply to a Google review using the Business Profile API.
    review_name format: accounts/{accountId}/locations/{locationId}/reviews/{reviewId}
    Returns {"ok": True} or {"ok": False, "error": "..."}
    """
    if not review_name:
        return {"ok": False, "error": "No review name — review was fetched before GMB was connected"}

    access_token = get_valid_token(restaurant_id)
    if not access_token:
        return {"ok": False, "error": "Google Business not connected"}

    try:
        url  = f"https://mybusinessreviews.googleapis.com/v1/{review_name}/reply"
        resp = requests.put(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json",
            },
            json={"comment": reply_text},
            timeout=10,
        )
        if resp.status_code in (200, 201):
            return {"ok": True}
        # `error` is shown to the owner as-is, so it is a sentence, never
        # Google's JSON body (MOD-REV-15); the raw detail goes to the logs.
        print(f"[GMB] post_reply {resp.status_code} for {review_name}: {(resp.text or '')[:300]}")
        return {"ok": False, "status": resp.status_code, "removed": resp.status_code == 404,
                "error": reply_error_message(resp.status_code)}
    except Exception as e:
        print(f"[GMB] post_reply transport error for {review_name}: {e}")
        return {"ok": False, "status": None,
                "error": ("Couldn't reach Google to post this reply. Nothing was lost — "
                          "use Retry posting in a few minutes.")}


def reply_error_message(status_code) -> str:
    """What an owner is told when Google refuses a reply, by status."""
    if status_code == 404:
        return ("Google says this review was removed, so there's nothing to reply to. "
                "It has been taken out of your queue.")
    if status_code == 401:
        return ("Google didn't accept Cavnar's connection. Reconnect Google in "
                "Account → Connections, then use Retry posting.")
    if status_code == 403:
        return ("Google says this login can't reply on this listing. Check you're an owner or "
                "manager of it in Google Business Profile, then use Retry posting.")
    if status_code == 429 or (status_code or 0) >= 500:
        return ("Google is having trouble right now. Nothing was lost — use Retry posting "
                "in a few minutes.")
    return ("Google didn't accept this reply. Check it reads as you want it to, then use "
            "Retry posting.")


def delete_reply(restaurant_id: int, review_name: str) -> dict:
    """
    Retract a previously-posted reply via the Business Profile API's
    deleteReply endpoint — the counterpart to post_reply(). Used to undo an
    auto-posted approval: once this succeeds, the reply is actually gone
    from the business's live Google listing, not just marked differently
    on our side.
    Returns {"ok": True} or {"ok": False, "error": "..."}
    """
    if not review_name:
        return {"ok": False, "error": "No review name on file for this review"}

    access_token = get_valid_token(restaurant_id)
    if not access_token:
        return {"ok": False, "error": "Google Business not connected"}

    try:
        url  = f"https://mybusinessreviews.googleapis.com/v1/{review_name}/reply"
        resp = requests.delete(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code in (200, 204):
            return {"ok": True}
        else:
            return {"ok": False, "error": f"API error {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fetch_gmb_logo_url(restaurant_id: int, access_token: str, account_id: str, location_id: str) -> dict:
    """One-time backfill of restaurants.brand_logo_url from the listing's own
    LOGO-category photo, so the Account hero banner shows the restaurant's
    real logo instead of a letter initial.

    Never overwrites a value that's already there: an admin who hand-set
    brand_logo_url, or a restaurant this already ran for, is left alone —
    callers can invoke this unconditionally on every sync without it
    re-fetching or fighting a manual override.

    account_id/location_id format: "accounts/123" / "locations/456", matching
    every other v4 Media API call in this module (create_local_post, etc).
    """
    from models import get_restaurant, update_restaurant
    r = get_restaurant(restaurant_id)
    if not r or r.brand_logo_url:
        return {"ok": False, "error": "brand_logo_url already set"}
    if not account_id or not location_id:
        return {"ok": False, "error": "missing account or location id"}
    try:
        resp = requests.get(
            f"https://mybusiness.googleapis.com/v4/{account_id}/{location_id}/media",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"ok": False, "error": f"Media API {resp.status_code}: {resp.text[:200]}"}
        items = resp.json().get("mediaItems", []) or []
        logo = next((m for m in items
                    if (m.get("locationAssociation") or {}).get("category") == "LOGO"), None)
        if not logo:
            return {"ok": False, "error": "No LOGO-category photo on this listing"}
        url = logo.get("googleUrl") or logo.get("sourceUrl")
        if not url:
            return {"ok": False, "error": "LOGO item had no usable URL"}
        update_restaurant(restaurant_id, {"brand_logo_url": url})
        print(f"[GMB] Logo cached for restaurant {restaurant_id}")
        return {"ok": True, "url": url}
    except Exception as e:
        print(f"[GMB] fetch_gmb_logo_url error for restaurant {restaurant_id}: {e}")
        return {"ok": False, "error": str(e)}


def fetch_location_rating(restaurant_id: int, access_token: str, location_id: str) -> dict:
    """
    Fetch the official GBP overall rating + review count from the location's metadata.
    Stores result to restaurants.gbp_rating and gbp_review_count.
    location_id format: "locations/456"
    """
    try:
        resp = requests.get(
            f"https://mybusinessbusinessinformation.googleapis.com/v1/{location_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"readMask": "rating,userRatingCount"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        rating = data.get("rating")
        count  = data.get("userRatingCount")
        if rating is not None:
            from models import update_restaurant
            update_fields = {"gbp_rating": float(rating),
                             "gbp_rating_updated_at": datetime.now(timezone.utc).isoformat()}
            if count is not None:
                update_fields["gbp_review_count"] = int(count)
            update_restaurant(restaurant_id, update_fields)
            print(f"[GMB] GBP rating for restaurant {restaurant_id}: {rating} ({count} reviews)")
            return {"ok": True, "rating": float(rating), "count": count}
        return {"ok": False, "error": "No rating in response"}
    except Exception as e:
        print(f"[GMB] fetch_location_rating error for restaurant {restaurant_id}: {e}")
        return {"ok": False, "error": str(e)}


def is_connected(restaurant_id: int) -> bool:
    """Check if a restaurant has GMB connected."""
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    return bool(r and r.gmb_refresh_token)


# ── Listing read/write ────────────────────────────────────────────────────────

def get_gbp_listing(restaurant_id: int) -> dict:
    """
    Fetch current business info from GBP.
    Returns dict with primaryPhone, websiteUri, description, title, has_hours.
    """
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r or not r.gmb_refresh_token or not r.gmb_location_id:
        return {"ok": False, "error": "Google Business not connected"}
    token = get_valid_token(restaurant_id)
    if not token:
        return {"ok": False, "error": "Could not refresh Google token"}
    try:
        resp = requests.get(
            f"https://mybusinessbusinessinformation.googleapis.com/v1/{r.gmb_location_id}",
            headers={"Authorization": f"Bearer {token}"},
            # regularHours added for the AI Visibility checklist's own
            # "hours listed" item (client_api.py) — same endpoint, same
            # scope this call already had, just one more field on the mask.
            params={"readMask": "name,title,phoneNumbers,websiteUri,profile,regularHours"},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"ok": False, "error": f"GBP API {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        return {
            "ok":          True,
            "title":       data.get("title", ""),
            "phone":       data.get("phoneNumbers", {}).get("primaryPhone", ""),
            "website":     data.get("websiteUri", ""),
            "description": data.get("profile", {}).get("description", ""),
            "has_hours":   bool(data.get("regularHours", {}).get("periods")),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def update_gbp_listing(restaurant_id: int, fields: dict) -> dict:
    """
    PATCH the GBP listing with updated fields.
    fields: dict with any subset of {phone, website, description}
    """
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r or not r.gmb_refresh_token or not r.gmb_location_id:
        return {"ok": False, "error": "Google Business not connected"}
    token = get_valid_token(restaurant_id)
    if not token:
        return {"ok": False, "error": "Could not refresh Google token"}

    body       = {}
    mask_parts = []
    if "phone" in fields:
        body["phoneNumbers"] = {"primaryPhone": fields["phone"]}
        mask_parts.append("phoneNumbers.primaryPhone")
    if "website" in fields:
        body["websiteUri"] = fields["website"]
        mask_parts.append("websiteUri")
    if "description" in fields:
        body.setdefault("profile", {})["description"] = fields["description"]
        mask_parts.append("profile.description")

    if not mask_parts:
        return {"ok": False, "error": "No fields to update"}

    try:
        resp = requests.patch(
            f"https://mybusinessbusinessinformation.googleapis.com/v1/{r.gmb_location_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params={"updateMask": ",".join(mask_parts)},
            json=body,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            return {"ok": True}
        return {"ok": False, "error": f"GBP API {resp.status_code}: {resp.text[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Local posts ────────────────────────────────────────────────────────────────

# Valid callToAction actionType values, per Google's Local Posts reference.
# BOOK/ORDER/SHOP/LEARN_MORE/SIGN_UP all require a url; CALL uses the
# listing's own phone number and must NOT carry one.
LOCAL_POST_ACTIONS = ("LEARN_MORE", "BOOK", "ORDER", "SHOP", "SIGN_UP", "CALL")


def create_local_post(restaurant_id: int, summary: str, cta_type: str = None,
                      cta_url: str = None, language_code: str = "en-US") -> dict:
    """Publish a "What's new" post to the connected Google Business Profile.

    Marketing already generates this copy (the `google_promo` content type)
    — this is the step that actually puts it on the listing.

    IMPORTANT, unverified: the endpoint below is Google's documented Local
    Posts API (v4 `mybusiness.googleapis.com`, which Local Posts stayed on
    after the rest of the surface moved to v1). It has NOT been exercised
    against a live listing here, because no Google Business Profile can be
    connected in this environment yet (the OAuth redirect URI and the
    Cloudflare challenge are both still outstanding). The request shape
    follows the published reference and the call construction is covered by
    tests, but treat the first real post as the actual verification — same
    caveat style as toast.fetch_order_selections' refundStatus note.

    Returns {"ok": True, "name": "<post resource name>"} or
    {"ok": False, "error": "..."}.
    """
    from models import get_restaurant

    summary = (summary or "").strip()
    if not summary:
        return {"ok": False, "error": "Post text is required"}
    # Google rejects anything over 1500 characters outright.
    if len(summary) > 1500:
        return {"ok": False, "error": "Post text is over Google's 1,500-character limit"}

    r = get_restaurant(restaurant_id)
    if not r or not r.gmb_refresh_token or not r.gmb_account_id or not r.gmb_location_id:
        return {"ok": False, "error": "Google Business not connected"}

    token = get_valid_token(restaurant_id)
    if not token:
        return {"ok": False, "error": "Could not refresh Google token"}

    body = {"languageCode": language_code, "summary": summary, "topicType": "STANDARD"}
    if cta_type:
        cta_type = cta_type.upper()
        if cta_type not in LOCAL_POST_ACTIONS:
            return {"ok": False, "error": f"Unsupported call to action: {cta_type}"}
        action = {"actionType": cta_type}
        if cta_type == "CALL":
            # CALL uses the listing's own number; sending a url is rejected.
            if cta_url:
                return {"ok": False, "error": "A Call button can't carry a link"}
        else:
            if not cta_url:
                return {"ok": False, "error": f"{cta_type} needs a link"}
            action["url"] = cta_url
        body["callToAction"] = action

    # gmb_account_id is "accounts/123", gmb_location_id is "locations/456"
    # (see get_gmb_account_id / get_gmb_location_id) — localPosts hangs off
    # the combined parent.
    parent = f"{r.gmb_account_id}/{r.gmb_location_id}"
    try:
        resp = requests.post(
            f"https://mybusiness.googleapis.com/v4/{parent}/localPosts",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return {"ok": True, "name": (resp.json() or {}).get("name", "")}
        return {"ok": False, "error": f"GBP API {resp.status_code}: {resp.text[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
