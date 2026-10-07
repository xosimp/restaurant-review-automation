"""
competitor.py — Competitor intelligence for Cavnar AI
Pulls nearby restaurant reviews via Google Places API and generates AI insights.
"""
import config
import os, json, requests
from ai_utils import create_with_retry, extract_text, get_client, model_for
from ai_guard import UNTRUSTED_NOTE, wrap_untrusted

# Every Google Places request goes through ai_utils.places_request (#123):
# metered as it is made, refused before it is sent when the key is missing,
# the Places breaker is open or the restaurant's Places ceiling is spent.
# The per-run `usage` tally and the meter loop in run_competitor_analysis are
# gone — they metered after the fact, guessed at the request count, and
# never saw an owner-added competitor's details or reviews.
from ai_utils import places_request as _places_request, PlacesUnavailable

PLACES_API_KEY = config.google_places_key()  # either variable name; used to read only GOOGLE_PLACES_API_KEY
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")


def fetch_menu_notes_from_places(google_place_id: str, restaurant_id: int = None) -> str:
    """Fetch menu URL, editorial summary, and cuisine info from Google Places API.
    Returns a string suitable for menu_notes field, or empty string if nothing useful found.

    `restaurant_id` is the restaurant the notes are for; the menu extraction
    below is billed to it. Without it the spend landed in the global pool
    only, outside that restaurant's own budget (AI-30)."""
    if not PLACES_API_KEY or not google_place_id:
        return ""
    try:
        r = _places_request("details", {
            "place_id": google_place_id,
            "fields": "name,types,price_level,editorial_summary,menu_url,website,serves_breakfast,serves_brunch,serves_lunch,serves_dinner,serves_beer,serves_wine,serves_cocktails,serves_vegetarian_food",
            "key": PLACES_API_KEY,
        }, restaurant_id=restaurant_id, action="menu_notes", timeout=8)
        data = r.json()
        if data.get("status") != "OK":
            return ""
        place_data = data.get("result", {})

        parts = []

        # Editorial summary (Google's own description)
        summary = place_data.get("editorial_summary", {}).get("overview", "")
        if summary:
            parts.append(f"Google description: {summary}")

        # Meal services
        meal_flags = []
        if place_data.get("serves_breakfast"): meal_flags.append("breakfast")
        if place_data.get("serves_brunch"): meal_flags.append("brunch")
        if place_data.get("serves_lunch"): meal_flags.append("lunch")
        if place_data.get("serves_dinner"): meal_flags.append("dinner")
        if meal_flags:
            parts.append(f"Serves: {', '.join(meal_flags)}")

        # Drinks
        drinks = []
        if place_data.get("serves_beer"): drinks.append("beer")
        if place_data.get("serves_wine"): drinks.append("wine")
        if place_data.get("serves_cocktails"): drinks.append("cocktails")
        if drinks:
            parts.append(f"Drinks: {', '.join(drinks)}")

        if place_data.get("serves_vegetarian_food"):
            parts.append("Vegetarian options available")

        # Cuisine types
        generic = {"restaurant","food","point_of_interest","establishment","bar","cafe"}
        types = [t.replace("_restaurant","").replace("_"," ")
                 for t in place_data.get("types", []) if t not in generic]
        if types:
            parts.append(f"Cuisine type: {', '.join(types[:3])}")

        # Menu URL — try to parse it for actual menu items
        menu_url = place_data.get("menu_url", "") or place_data.get("website", "")
        if menu_url:
            parts.append(f"Menu URL: {menu_url}")
            try:
                menu_items = fetch_menu_from_url(menu_url, restaurant_id=restaurant_id)
                if menu_items:
                    parts.append(f"Menu items (auto-extracted):\n{menu_items}")
            except Exception:
                pass

        return "\n".join(parts) if parts else ""
    except Exception as e:
        print(f"[fetch_menu_notes] error: {e}")
        return ""











def fetch_menu_from_pdf_bytes(pdf_bytes: bytes, restaurant_name: str = "", restaurant_id: int = None) -> str:
    """Extract menu items from PDF bytes using pypdf then AI."""
    try:
        import io, os
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        text = text[:8000]
        if len(text) < 50:
            return ""
        client = get_client()
        extract_prompt = (
            "Extract the key menu items from this restaurant menu text. "
            "Return a concise summary: Signature dishes: [list]. Appetizers: [list]. "
            "Mains: [list]. Desserts: [list]. Drinks: [list]. "
            "Only include actual menu items. Max 300 words. "
            "If no menu items found, respond with exactly: NO_MENU_FOUND\n\n"
            # An uploaded PDF is a file someone handed us; its text went into
            # this prompt raw. Fenced and labelled like every other block of
            # outside text.
            + UNTRUSTED_NOTE + "\n\nMenu text:\n" + wrap_untrusted(text)
        )
        import data_health
        import ai_orchestrator
        # On the orchestrator's rung (menu_extract_pdf: T1, one call — AI
        # cost audit 10/7/26, orchestration Phase 3); spot_check_menu checks it.
        msg = ai_orchestrator.generate("menu_extract_pdf", restaurant_id, lambda route, notes: create_with_retry(
            client,
            restaurant_id=restaurant_id,
            action="menu_extract_pdf",
            # Rests on no data source: menu extraction from the supplied PDF.
            readiness=data_health.NOT_APPLICABLE,
            **route.apply(dict(model=model_for("competitor_extract"), max_tokens=400,
                               messages=[{"role": "user", "content": extract_prompt}])),
        ), subject="menu:pdf").result
        result = extract_text(msg).strip()
        if "NO_MENU_FOUND" in result or len(result) < 30:
            return ""
        # Only items the PDF's own text contains (H11).
        return spot_check_menu(result, text)
    except Exception as e:
        print(f"[fetch_menu_from_pdf_bytes] error: {e}")
        return ""


# A menu URL comes from a Google listing's `website` field or an admin's
# paste — a page whose author is not our customer. It was fetched with
# requests' default redirect-following and no host check, so a site that
# answered 302 to http://169.254.169.254/... had the server read its own
# cloud metadata (instance credentials) and hand it to the model (AI-30).
# Every hop is now checked against the same public-address rule webhooks use.
_MENU_MAX_REDIRECTS = 3


def _public_url(url: str) -> bool:
    """Whether `url` may be fetched: net_safety's rule, the one outbound
    webhooks use."""
    try:
        import net_safety
        net_safety.vet(url)
        return True
    except Exception:
        return False


def _get_public(url, headers, timeout):
    """GET `url`, following at most _MENU_MAX_REDIRECTS redirects by hand and
    refusing any hop — the first included — that is not a public address.
    Returns the final response, or None when a hop was refused.

    Each hop goes through net_safety.safe_get (#156): the host is resolved
    once and the connection made to the address that was checked. The check
    used to resolve it, and then requests resolved it again to connect — a
    name answering public to the first and private to the second passed."""
    import net_safety
    from urllib.parse import urljoin
    for _hop in range(_MENU_MAX_REDIRECTS + 1):
        try:
            r = net_safety.safe_get(url, headers=headers, timeout=timeout)
        except net_safety.UnsafeURL:
            print(f"[fetch_menu_from_url] refused a non-public address: {url[:120]}")
            return None
        if r.status_code in (301, 302, 303, 307, 308):
            location = (r.headers or {}).get("Location") or (r.headers or {}).get("location")
            if not location:
                return r
            url = urljoin(url, location)
            continue
        return r
    return None


def fetch_menu_from_url(menu_url: str, restaurant_id: int = None) -> str:
    """Fetch a restaurant's menu page and use AI to extract key menu items."""
    if not menu_url:
        return ""
    try:
        import os
        # Identify as a normal browser so servers don't reject a bare
        # "python-requests" client — this is a single honest identity, not
        # rotated or retried to work around a site's bot-blocking response.
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = _get_public(menu_url, headers, 12)
        if r is None or r.status_code != 200 or len(r.text) <= 500:
            return ""  # Not available, or the site declined the request — fail cleanly
        page_text = r.text[:10000]

        # Check if page has useful content or is just a JS shell
        # Strip script/style tags first, then check remaining content
        import re as _re2
        clean = _re2.sub(r'<script[^>]*>.*?</script>', '', page_text, flags=_re2.DOTALL)
        clean = _re2.sub(r'<style[^>]*>.*?</style>', '', clean, flags=_re2.DOTALL)
        clean = _re2.sub(r'<[^>]+>', ' ', clean)
        clean_words = [w for w in clean.split() if len(w) > 2]
        if len(clean_words) < 80:
            return ""  # JS-rendered site — no readable content after stripping tags

        client = get_client()
        extract_prompt = (
            "Extract the key menu items from this restaurant page. "
            "Return a concise summary: Signature dishes: [list]. Appetizers: [list]. "
            "Mains: [list]. Desserts: [list]. Drinks: [list]. "
            "Only include actual menu items. Skip prices and HTML. Max 300 words. "
            "If no menu items found, respond with exactly: NO_MENU_FOUND\n\n"
            # Scraped from a URL — a page whose author is not our customer.
            + UNTRUSTED_NOTE + "\n\nPage content:\n" + wrap_untrusted(page_text)
        )
        import data_health
        import ai_orchestrator
        # On the orchestrator's rung (menu_extract_url: T1, one call — AI
        # cost audit 10/7/26, orchestration Phase 3); spot_check_menu checks it.
        msg = ai_orchestrator.generate("menu_extract_url", restaurant_id, lambda route, notes: create_with_retry(
            client,
            restaurant_id=restaurant_id,
            action="menu_extract_url",
            # Rests on no data source: menu extraction from the supplied page.
            readiness=data_health.NOT_APPLICABLE,
            **route.apply(dict(model=model_for("competitor_extract"), max_tokens=400,
                               messages=[{"role": "user", "content": extract_prompt}])),
        ), subject="menu:url").result
        result = extract_text(msg).strip()
        if "NO_MENU_FOUND" in result or len(result) < 30:
            return ""
        # Only items the page's own text contains (H11): the extraction was
        # stored and quoted with nothing checking it against its source.
        return spot_check_menu(result, clean)
    except Exception as e:
        print(f"[fetch_menu_from_url] error: {e}")
        return ""


# A coffee shop or bar with no real food-service classification isn't a
# genuine dining competitor, even though Google's nearby-restaurant search
# sometimes surfaces one — the `type` request param is a strong hint, not
# an ironclad filter, especially once the `keyword` param broadens the
# match (e.g. a restaurant whose own types include "bar" builds a "bar
# pub" keyword, which can pull in places matched mostly on that keyword
# text rather than a true restaurant classification). Excluded only if
# beverage/nightlife types are ALL it has — a food-serving brewery or
# gastropub (bar + restaurant, or bar + meal_takeaway) still competes on
# food and stays.
_PURE_BEVERAGE_TYPES = {"cafe", "bar", "night_club"}
_FOOD_SIGNAL_TYPES = {"restaurant", "meal_takeaway", "meal_delivery", "bakery"}


def _same_business(name_a: str, name_b: str) -> bool:
    """Do these two listings name the same business?

    Google duplicates happen, and a duplicate listing of the restaurant
    itself used to appear in its own competitor set under an exact-match
    self-check. Normalised comparison plus a containment test catches
    "Simple EJ's" against "Simple EJs Sports Bar & Grill".
    """
    import re as _re
    def _n(x):
        x = _re.sub(r"[^a-z0-9 ]", "", (x or "").lower())
        for filler in (" restaurant", " bar and grill", " bar grill", " sports bar",
                       " grill", " kitchen", " cafe", " and ", " the "):
            x = x.replace(filler, " ")
        return " ".join(x.split())
    a, b = _n(name_a), _n(name_b)
    if not a or not b:
        return False
    if a == b:
        return True
    # Containment only counts when the shorter name is distinctive enough to
    # BE a business name on its own. Without this, a restaurant genuinely
    # called "Bar" excluded "Bar Louie" from its own competitor set as a
    # duplicate of itself, and "Mia" swallowed "Gia Mia" — the exact
    # confusion the containment test was added to solve, running backwards.
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < 8 and len(shorter.split()) < 2:
        return False
    # And it has to sit on word boundaries: "lous" must not match "louses".
    import re as _re
    return bool(_re.search(r"(?:^|\s)" + _re.escape(shorter) + r"(?:\s|$)", longer))


# A chain is a chain because it has hundreds of locations, not because it
# is on a list somebody typed. This was 16 hardcoded fast-food brands, so
# every casual-dining chain — Applebee's, Chili's, Olive Garden, Buffalo
# Wild Wings, Texas Roadhouse — passed straight through as a "local
# competitor". Kept as a named set because Places gives us no franchise
# count, but broadened to the categories that actually distort a local
# comparison, and grouped so a reader can see what the list is FOR.
_CHAIN_NAMES = {
    # fast food
    "mcdonald", "burger king", "wendy", "taco bell", "subway", "kfc", "domino",
    "pizza hut", "little caesar", "papa john", "chipotle", "panera", "dunkin",
    "starbucks", "popeyes", "chick-fil-a", "arby", "sonic drive", "jack in the box",
    "five guys", "shake shack", "whataburger", "culver", "raising cane", "jimmy john",
    "jersey mike", "firehouse subs", "wingstop", "dairy queen", "hardee", "carl's jr",
    "del taco", "el pollo loco", "panda express", "quiznos", "portillo",
    # casual dining — the ones that most distort a local set
    "applebee", "chili's", "chilis", "olive garden", "outback", "texas roadhouse",
    "buffalo wild wings", "red lobster", "tgi friday", "cheesecake factory",
    "ihop", "denny", "cracker barrel", "red robin", "longhorn steakhouse",
    "bj's restaurant", "yard house", "hooters", "ruby tuesday", "carrabba",
    "bonefish grill", "p.f. chang", "pf chang", "maggiano", "the capital grille",
    "ruth's chris", "morton's the steakhouse", "first watch", "another broken egg",
}


def _is_chain(name: str) -> bool:
    low = (name or "").lower()
    return any(chain in low for chain in _CHAIN_NAMES)


def _distance_m(lat1, lng1, lat2, lng2):
    """Straight-line metres between two points, or None."""
    if None in (lat1, lng1, lat2, lng2):
        return None
    import math as _m
    r = 6371000.0
    p1, p2 = _m.radians(lat1), _m.radians(lat2)
    dp, dl = _m.radians(lat2 - lat1), _m.radians(lng2 - lng1)
    a = _m.sin(dp / 2) ** 2 + _m.cos(p1) * _m.cos(p2) * _m.sin(dl / 2) ** 2
    return int(round(2 * r * _m.asin(_m.sqrt(a))))


# Below this, a star rating is a handful of opinions rather than a
# reputation, and comparing against it is comparing against noise.
MIN_REVIEWS_FOR_A_MEANINGFUL_RATING = 15

# Types that mean "no dining room". A ghost kitchen competes on delivery
# economics, not on the things this module advises about — service scripts,
# staffing, atmosphere — so it distorts a dine-in comparison.
_DELIVERY_ONLY_TYPES = {"meal_delivery"}
_DINE_IN_SIGNAL_TYPES = {"restaurant", "bar", "cafe", "bakery", "meal_takeaway"}


def _is_delivery_only(types) -> bool:
    t = set(types or [])
    return bool(t & _DELIVERY_ONLY_TYPES) and not (t & _DINE_IN_SIGNAL_TYPES)


def _is_pure_beverage_spot(types) -> bool:
    type_set = set(types or [])
    return bool(type_set & _PURE_BEVERAGE_TYPES) and not (type_set & _FOOD_SIGNAL_TYPES)


def remove_competitor_from_cache(restaurant_id: int, place_id: str) -> bool:
    """Drops one competitor from the already-cached competitor_intel blob
    (restaurant.competitor_intel — written by run_competitor_analysis)
    without re-running the full pipeline. A removal doesn't need fresh
    Google Places calls or a new Claude generation for the competitors
    that are staying — it only needs the one that's leaving gone from the
    list. mobile_api.py's own remove-competitor route was previously
    calling the SAME async refresh job add-competitor uses (20-40s,
    Google Places + Claude), which is why deleting a competitor visibly
    lagged for several seconds even though the underlying change is a
    one-line filter on an already-cached JSON blob.

    Deliberately leaves the cached insight text untouched rather than
    regenerating it — a small chance the prose still names the removed
    competitor until the next real refresh, traded against every deletion
    finishing in under a second instead of tens of seconds.

    Returns False (no-op) if there's no cached blob yet or the place_id
    wasn't actually in it — both harmless, nothing to remove either way.
    """
    from models import get_restaurant, update_restaurant
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.competitor_intel:
        return False
    try:
        blob = json.loads(restaurant.competitor_intel)
    except Exception:
        return False
    competitors = blob.get("competitors", [])
    new_competitors = [c for c in competitors if c.get("place_id") != place_id]
    if len(new_competitors) == len(competitors):
        return False
    blob["competitors"] = new_competitors
    # The read now stands for the owner's list without it, so a Refresh
    # right after a removal is served the stored read (#42), not a new run.
    if isinstance(blob.get("custom_ids"), list):
        blob["custom_ids"] = [p for p in blob["custom_ids"] if p != place_id]
    update_restaurant(restaurant_id, {"competitor_intel": json.dumps(blob)})
    return True


def search_places_near(query: str, lat: float = None, lng: float = None, max_results: int = 6) -> list:
    """Text-search Google Places for a restaurant by name, biased toward
    (lat, lng) when available — powers the owner-facing "add a competitor"
    search in the app (mobile_api.py's /intel/search-places), so an owner
    can type a name and pick from real nearby candidates instead of the
    admin-only custom_competitors field's raw Place ID text entry.

    Deliberately a broad name search with a location BIAS, not the
    type/keyword-filtered search get_nearby_competitors runs — that filter
    is exactly why a genuine neighbor can be invisible to the automatic
    discovery: it biases toward restaurants sharing the same Google
    "types" as this restaurant's own listing, so a fine-dining place next
    door to a casual spot may never surface in get_nearby_competitors'
    own results even at 2000m, purely because their types don't overlap.
    This function exists specifically so an owner can add that exact case
    themselves.
    """
    if not PLACES_API_KEY or not query:
        return []
    try:
        params = {"query": f"{query} restaurant", "key": PLACES_API_KEY}
        if lat is not None and lng is not None:
            params["location"] = f"{lat},{lng}"
            params["radius"] = 8000
        # Billed to the owner's restaurant (the request's session, or
        # ai_context), and against its Places ceiling.
        r = _places_request("textsearch", params, action="competitor_search", timeout=8)
        data = r.json()
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            return []
        return [
            {
                "place_id": item.get("place_id", ""),
                "name": item.get("name", ""),
                "address": item.get("formatted_address", ""),
                # A search hit, not a measurement we store; the app decodes a
                # number here.
                "rating": item.get("rating", 0),
                "review_count": item.get("user_ratings_total", 0),
            }
            for item in data.get("results", [])[:max_results]
            if item.get("place_id") and item.get("name")
        ]
    except Exception as e:
        print(f"[Competitor] search_places_near error: {e}")
        return []


# ── Places economy (AI cost audit 10/7/26) ───────────────────────────────────
#
# Every Places request costs real money per call; these are the rules that
# keep one competitor read from buying the same public facts twice.
REDISCOVER_DAYS = 28            # #39: nearby discovery at most monthly — inside Google's 30-day content limit
REVIEWS_REUSE_HOURS = 24        # #15: the daily check's newest reviews stand in for a run's own lookup
OWN_RATING_FRESH_HOURS = 24     # #24: a rating this fresh is not bought again
REFRESH_FRESH_HOURS = 6         # #42: an owner Refresh inside this serves the stored read
CUSTOM_COMPETITORS_MAX = 10     # #40: owner-added competitors, each a Details call every run
# One field set for every competitor lookup — the daily check, a run's
# refresh of a stored competitor, the review lookup — so the Details cache
# (ai_utils.places_request, #13) answers any of them from any other the same
# day. name and business_status are Basic Data (free with the request);
# rating, user_ratings_total and reviews are the one Atmosphere SKU.
COMPETITOR_FIELDS = "name,rating,user_ratings_total,business_status,reviews"
# An owner-added competitor: the same, plus what the comparison shows about
# it — one call, reviews included (#40; it used to be two).
CUSTOM_FIELDS = COMPETITOR_FIELDS + ",types,vicinity,price_level"


def _own_listing_key(google_place_id) -> str:
    return f"own_listing_at:{google_place_id}"


def _business_profile_rating_fresh(conn, restaurant_id, hours=OWN_RATING_FRESH_HOURS) -> bool:
    """Whether the Business Profile connection read this restaurant's rating
    in the last `hours` (own_rating_history keeps each reading's source).
    Unknown reads as not fresh."""
    try:
        return conn.execute("SELECT 1 FROM own_rating_history WHERE restaurant_id=? AND source='business_profile' "
                            "AND recorded_at >= datetime('now', ?) LIMIT 1",
                            (restaurant_id, f"-{int(hours)} hours")).fetchone() is not None
    except Exception:
        return False


def _stamp_age_hours(value):
    """Hours since a stored timestamp (time_utils.parse_stamp's formats), or
    None when there is none."""
    from datetime import datetime as _dt, timezone as _tz
    from time_utils import parse_stamp
    stamp = parse_stamp(value) if value else None
    if stamp is None:
        return None
    return max(0.0, (_dt.now(_tz.utc) - stamp).total_seconds() / 3600.0)


def _now_stamp() -> str:
    from datetime import datetime as _dt, timezone as _tz
    return _dt.now(_tz.utc).isoformat(timespec="seconds")


def _remember_own_listing(google_place_id, types, price_level, rating=None, rating_count=None):
    """Keep the restaurant's OWN Google types and price level (Benchmarking
    audit #8, BM2-2): they were fetched on every competitor refresh and
    thrown away. They cross-check the service model Cavnar guesses for the
    "is that right?" prompt — never a peer key by themselves. A direct write
    outside update_restaurant, so each row's request cache is invalidated.
    Never raises: a failed write only loses the cross-check."""
    if not google_place_id:
        return
    try:
        import models as _m
        conn = _m.get_conn()
        rated = []
        try:
            ids = [r["id"] for r in conn.execute("SELECT id FROM restaurants WHERE google_place_id=?",
                                                 (google_place_id,)).fetchall()]
            # A caller with no types in hand (an answer that carried only the
            # rating) leaves the stored ones alone instead of blanking them.
            if ids and types is not None:
                conn.execute("UPDATE restaurants SET google_types=?, google_price_level=? WHERE google_place_id=?",
                             (json.dumps([str(t) for t in (types or [])][:20]),
                              int(price_level) if isinstance(price_level, (int, float)) else None, google_place_id))
                # When they were last read from Google: a competitor discovery
                # reuses them instead of a Details call of its own only while
                # they are inside Google's 30-day limit (AI cost audit 10/7/26 #39).
                try:
                    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?, datetime('now'), "
                                 "datetime('now')) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                                 "updated_at=excluded.updated_at", (_own_listing_key(google_place_id),))
                except Exception as e:
                    print(f"[competitor] own listing stamp not kept for {google_place_id}: {e}")
            # The listing's public Google rating, the one every guest sees
            # (owner, 9/26/26): only the Business Profile connection wrote
            # it, so a Places-only restaurant was shown its imported
            # reviews' average instead. The same details call carries it —
            # but never over a Business Profile reading from the last day
            # (AI cost audit 10/7/26 #24): that one is Google's own figure
            # for the account, read free, and it is the fresher of the two.
            if isinstance(rating, (int, float)) and rating > 0:
                from datetime import datetime as _dt_r, timezone as _tz_r
                stamp = _dt_r.now(_tz_r.utc).isoformat(timespec="seconds")
                for rid in ids:
                    if _business_profile_rating_fresh(conn, rid):
                        continue
                    conn.execute("UPDATE restaurants SET gbp_rating=?, gbp_review_count=COALESCE(?, gbp_review_count), "
                                 "gbp_rating_updated_at=? WHERE id=?",
                                 (round(float(rating), 1),
                                  int(rating_count) if isinstance(rating_count, (int, float)) else None, stamp, rid))
                    rated.append(rid)
            conn.commit()
        finally:
            conn.close()
        for rid in ids:
            _m._invalidate_request_cache(rid)
        # The rating is overwritten in place; its week-by-week history is
        # kept (memory audit 9/29/26, public_history).
        if rated:
            import event_memory
            from time_utils import restaurant_now_by_id
            for rid in rated:
                # The week on the restaurant's clock, never the server's UTC
                # date (a Sunday-evening reading was next week's: RX-06).
                event_memory.record_own_rating(rid, rating, rating_count, source="places",
                                               at=restaurant_now_by_id(rid, naive=True))
    except Exception as e:
        print(f"[competitor] own listing not kept for {google_place_id}: {e}")


_own_rating_asked = set()


def refresh_own_rating(restaurant_id: int, background: bool = True) -> None:
    """Fetch this restaurant's public Google rating once when Cavnar has none
    (a Places-only restaurant before its next competitor refresh). One
    metered details call; at most once per process per restaurant; in a
    daemon thread by default so no page waits on Google. Never raises."""
    if restaurant_id in _own_rating_asked or not PLACES_API_KEY:
        return
    _own_rating_asked.add(restaurant_id)

    def _go():
        try:
            from models import get_restaurant as _gr
            r = _gr(restaurant_id)
            pid = getattr(r, "google_place_id", None) if r else None
            if not pid or getattr(r, "gbp_rating", None):
                return
            resp = _places_request("details", {
                "place_id": pid, "fields": "rating,user_ratings_total,types,price_level", "key": PLACES_API_KEY,
            }, restaurant_id=restaurant_id, action="own_rating", timeout=8)
            data = resp.json()
            if data.get("status") != "OK":
                return
            res = data.get("result") or {}
            _remember_own_listing(pid, res.get("types"), res.get("price_level"),
                                  res.get("rating"), res.get("user_ratings_total"))
        except Exception as e:
            print(f"[competitor] own rating not fetched for {restaurant_id}: {e}")
    if background:
        import threading
        threading.Thread(target=_go, daemon=True).start()
    else:
        _go()


def get_nearby_competitors(google_place_id: str, radius_meters: int = 2000, max_results: int = 5,
                           usage: dict = None, own: dict = None) -> list:
    """Find nearby restaurants using the Google Places API.

    `usage`, when given, is filled with the billed Places requests this made
    by kind ({"details": n, "nearby": n}). A run can make up to three nearby
    searches (keyword, broad, widened radius) and the caller metered one
    (MOD-INT-6), so two thirds of the spend never reached the budget. Each
    request is metered by places_request itself now (restaurant and action
    from ai_context); the tally is kept for callers that read it.

    A refused lookup is never an empty market: a Places error or a request
    refused before it was sent (PlacesUnavailable) is raised, not turned
    into [] (the Intel invariant).

    `own` ({"lat", "lng", "types", "price_level", "name"}), when given, is
    the restaurant's own listing as already stored (_stored_own_listing):
    its Details call is then not made (AI cost audit 10/7/26 #39)."""
    if not PLACES_API_KEY or not google_place_id:
        return []
    if usage is None:
        usage = {}

    def _billed(kind):
        usage[kind] = usage.get(kind, 0) + 1
    from ai_utils import places_error as _pe, PlacesError as _PlacesError
    try:
        if own and own.get("lat") is not None and own.get("lng") is not None and own.get("types"):
            lat, lng = own["lat"], own["lng"]
            own_name = own.get("name") or ""
            own_types = list(own.get("types") or [])
            own_price = own.get("price_level")
        else:
            # First get the restaurant's coordinates and types from its place ID
            r = _places_request("details", {
                "place_id": google_place_id,
                "fields": "geometry,name,vicinity,types,price_level,rating,user_ratings_total",
                "key": PLACES_API_KEY,
            }, timeout=8)
            _billed("details")
            data = r.json()
            if _pe(data):
                raise _pe(data)
            if data.get("status") != "OK":
                raise _PlacesError(data.get("status") or "NOT_FOUND", "no details for this listing")
            result_data = data.get("result", {})
            geometry = result_data.get("geometry", {})
            location = geometry.get("location", {})
            if not location.get("lat") or not location.get("lng"):
                return []
            lat, lng = location["lat"], location["lng"]
            own_name = result_data.get("name", "")
            own_types = result_data.get("types", [])
            own_price = result_data.get("price_level")
            _remember_own_listing(google_place_id, own_types, own_price,
                                  result_data.get("rating"), result_data.get("user_ratings_total"))

        # Build a keyword from the restaurant's type to filter similar competitors
        # Exclude generic types that apply to everything
        generic_types = {"restaurant","food","point_of_interest","establishment"}
        specific_types = [t.replace("_", " ") for t in own_types if t not in generic_types]

        # Determine meal type keyword — prefer breakfast/brunch/cafe if applicable
        meal_keyword = None
        type_str = " ".join(own_types).lower()
        if any(k in type_str for k in ["breakfast", "brunch", "cafe", "bakery"]):
            meal_keyword = "breakfast brunch cafe"
        elif any(k in type_str for k in ["bar", "pub", "night_club"]):
            meal_keyword = "bar pub"
        elif specific_types:
            meal_keyword = specific_types[0]

        # Search for nearby similar restaurants — wider radius for suburban areas
        params = {
            "location": f"{lat},{lng}",
            "radius": radius_meters,
            "type": "restaurant",
            "key": PLACES_API_KEY,
            "rankby": "prominence",
        }
        if meal_keyword:
            params["keyword"] = meal_keyword

        r2 = _places_request("nearbysearch", params, timeout=8)
        _billed("nearby")
        r2_data = r2.json()
        if _pe(r2_data):
            # A refused search is not an empty neighbourhood.
            raise _pe(r2_data)
        places = r2_data.get("results", [])

        # If keyword search returns too few, fall back to broader search
        if len(places) < 3:
            params.pop("keyword", None)
            r2 = _places_request("nearbysearch", params, timeout=8)
            _billed("nearby")
            r2_data = r2.json()
            if _pe(r2_data):
                raise _pe(r2_data)
            places = r2_data.get("results", [])

        # Filter: skip self, skip fast food chains, skip pure beverage
        # spots, prefer similar price level

        def _filter(candidates, enforce_price, basis="cuisine and price match nearby"):
            out = []
            seen_ids = set()
            for p in candidates:
                name = p.get("name", "")
                pid = p.get("place_id")
                if not pid or pid in seen_ids:
                    continue
                # Self-exclusion was `name == own_name`, an exact match, so a
                # duplicate Google listing of this same restaurant under a
                # slightly different name ("Simple EJ's" vs "Simple EJs Sports
                # Bar") became its own competitor. Compare on the Place ID
                # first, then on a normalised name.
                if pid == google_place_id or _same_business(name, own_name):
                    continue
                if p.get("business_status") != "OPERATIONAL":
                    continue
                if _is_chain(name):
                    continue
                if _is_pure_beverage_spot(p.get("types", [])):
                    continue
                if _is_delivery_only(p.get("types", [])):
                    continue
                if enforce_price:
                    p_price = p.get("price_level")
                    if own_price and p_price and abs(own_price - p_price) > 2:
                        continue
                seen_ids.add(pid)
                _loc = (p.get("geometry") or {}).get("location") or {}
                out.append({
                    "place_id": pid,
                    "name": name,
                    # None when Places has no rating (no reviews yet): a
                    # missing measurement, never a 0-star place (MOD-INT-3).
                    "rating": p.get("rating"),
                    "review_count": p.get("user_ratings_total", 0),
                    "vicinity": p.get("vicinity", ""),
                    "price_level": p.get("price_level"),
                    "types": p.get("types", []),
                    # Which selection pass produced this one. Four passes run,
                    # relaxing cuisine, then price, then distance out to 8km —
                    # and a different-cuisine restaurant five miles away used
                    # to arrive in the same list, in the same shape, as a
                    # direct match across the street.
                    "match_basis": basis,
                    # A rating resting on a handful of reviews is not a
                    # reputation. Kept in the set — a new place nearby IS a
                    # competitor — but flagged so nothing averages it in or
                    # compares against it as though it were settled.
                    "rating_is_provisional": int(p.get("user_ratings_total") or 0)
                                             < MIN_REVIEWS_FOR_A_MEANINGFUL_RATING,
                    "distance_m": _distance_m(lat, lng, _loc.get("lat"), _loc.get("lng")),
                })
                if len(out) >= max_results:
                    break
            return out

        competitors = _filter(places, enforce_price=True,
                              basis="same cuisine type and similar price level, within "
                                    + str(radius_meters // 1000) + "km")

        # If still too few after filtering, relax the price-level match
        if len(competitors) < 3:
            competitors = _filter(places, enforce_price=False,
                                  basis="same cuisine type nearby, price level not matched")

        # Still short of a reasonable minimum — the initial radius may
        # just not have enough comparable restaurants in it (suburban/
        # low-density areas especially). One broader retry, doubled
        # radius and no keyword/price constraints, merging in anything
        # new rather than starting over — an owner should see a real
        # competitive picture, not two results because the first pass
        # happened to be narrow.
        if len(competitors) < 3:
            try:
                wider = _places_request("nearbysearch", {
                    "location": f"{lat},{lng}",
                    "radius": min(radius_meters * 2, 8000),
                    "type": "restaurant",
                    "key": PLACES_API_KEY,
                    "rankby": "prominence",
                }, timeout=8)
                _billed("nearby")
                wider = wider.json()
                more_places = wider.get("results", []) if wider.get("status") in ("OK", "ZERO_RESULTS") else []
                existing_ids = {c["place_id"] for c in competitors}
                for extra in _filter(more_places, enforce_price=False,
                                     basis="widened search — no cuisine or price match, up to "
                                           + str(min(radius_meters * 2, 8000) // 1000) + "km away"):
                    if extra["place_id"] not in existing_ids:
                        competitors.append(extra)
                        existing_ids.add(extra["place_id"])
                    if len(competitors) >= max_results:
                        break
            except Exception as e:
                print(f"[Competitor] Wider-radius retry failed: {e}")

        return competitors
    except Exception as e:
        from ai_utils import PlacesError as _PlacesError
        if isinstance(e, (_PlacesError, PlacesUnavailable)):
            raise
        print(f"[Competitor] get_nearby_competitors error: {e}")
        return []


def _reviews_from(result, max_reviews: int = 5) -> list:
    """A Details answer's reviews in the shape the competitor blob keeps:
    author, rating, text, Google's relative `time`, the ISO `date` (shown
    as M/D/YY, not "3 months ago") and `ts`, the review's own epoch second —
    what "newer than the last read" is decided on (AI cost audit 10/7/26 #15)."""
    out = []
    for rev in ((result or {}).get("reviews") or [])[:max_reviews]:  # Google Places returns max 5
        day, ts = None, None
        try:
            if rev.get("time"):
                from datetime import datetime as _dt, timezone as _tz
                ts = int(rev["time"])
                day = _dt.fromtimestamp(ts, tz=_tz.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            day, ts = None, None
        out.append({
            "author": rev.get("author_name", "Guest"),
            "rating": rev.get("rating", 3),
            "text": rev.get("text", ""),
            "time": rev.get("relative_time_description", ""),
            "date": day,
            "ts": ts,
        })
    return out


def _lookup_competitor(place_id: str, fields: str = COMPETITOR_FIELDS):
    """One Details answer for a competitor — newest reviews first — as its
    `result` dict, or None when Google did not answer OK. Raises
    PlacesUnavailable (refused before it was sent); any other failure is
    None. Served from the day's Details cache when the place was read
    today (#13)."""
    # Newest first (owner, 9/26/26): Google's default is its "most
    # relevant" five, which cited a months-old review as what a neighbour
    # is doing now.
    try:
        r = _places_request("details", {
            "place_id": place_id,
            "fields": fields,
            "reviews_sort": "newest",
            "key": PLACES_API_KEY,
        }, timeout=8)
        data = r.json()
    except PlacesUnavailable:
        raise
    except Exception as e:
        print(f"[Competitor] lookup of {place_id} failed: {e}")
        return None
    if not isinstance(data, dict) or data.get("status") != "OK":
        return None
    return data.get("result") or {}


def get_competitor_reviews(place_id: str, max_reviews: int = 5) -> list:
    """Get recent reviews for a competitor."""
    if not PLACES_API_KEY:
        return []
    try:
        res = _lookup_competitor(place_id)
        return _reviews_from(res, max_reviews) if res is not None else []
    except Exception as e:
        print(f"[Competitor] get_competitor_reviews error: {e}")
        return []


# Words that look like a proper noun in an insight but are not a business.
_NOT_A_BUSINESS = {
    "WHAT", "PRICE", "POSITIONING", "COMPETITORS", "RECOMMENDATIONS", "DOING",
    "WELL", "POORLY", "UNVERIFIED", "GOOGLE", "YELP", "AI", "THE", "THIS",
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY",
    "HI", "HELLO", "HEY",
}

# The version of the checks below that a stored read was shown under. A read
# checked by an older version is checked again on its next open
# (current_intel), even when the engine's own version has not moved. 2: the
# prompt's own greeting ("Hi Erik, here is...") read as an invented
# business and held every recommendation back (9/24/26).
INTEL_CHECK = 2


def _invented_competitors(text: str, competitors: list) -> list:
    """Capitalised multi-word names in the insight that are not in the list.

    Deliberately narrow: only runs of two or more capitalised words, which
    is what a restaurant name looks like and what a sentence opener does
    not. A single capitalised word is far too noisy to flag.
    """
    import re as _re
    known = set()
    for c in competitors or []:
        n = (c.get("name") or "").strip().lower()
        if n:
            known.add(n)
            for word in n.split():
                if len(word) > 3:
                    known.add(word)
    out = []
    for m in _re.finditer(r"\b([A-Z][\w&\'-]+(?:\s+[A-Z][\w&\'-]+){1,3})\b", text or ""):
        phrase = m.group(1).strip()
        if phrase.upper() == phrase and phrase.replace(" ", "") .isalpha():
            continue  # an ALL-CAPS section header
        if any(w.upper() in _NOT_A_BUSINESS for w in phrase.split()):
            continue
        low = phrase.lower()
        if low in known:
            continue
        if any(low in k or k in low for k in known):
            continue
        if any(w in known for w in low.split() if len(w) > 3):
            continue
        out.append(phrase)
    seen, uniq = set(), []
    for p_ in out:
        if p_.lower() not in seen:
            seen.add(p_.lower())
            uniq.append(p_)
    return uniq


def generate_competitor_insight(restaurant_name: str, competitors: list, owner_name: str = None, restaurant_profile: dict = None, tz_name: str = None, restaurant_id: int = None) -> str:
    """Use Claude to generate a strategic competitor insight."""
    if not competitors or not ANTHROPIC_KEY:
        return ""
    try:
        client = get_client()

        _PRICE_WORDS = {1: "$ (inexpensive)", 2: "$$ (moderate)",
                        3: "$$$ (expensive)", 4: "$$$$ (very expensive)"}
        comp_summary = ""
        # Every review handed to the model gets an id ("R1", "R2", ...) that a
        # recommendation must cite, and the id is kept on the review so the
        # screen can show which reviews a recommendation rests on — the same
        # rule review diagnoses follow with review ids (audit #31).
        _ref_n = 0
        for c in competitors:
            for r in (c.get("reviews") or [])[:5]:
                _ref_n += 1
                r["ref"] = f"R{_ref_n}"
        for c in competitors:
            # Use up to 5 reviews, 250 chars each for richer insight
            rev_list = c.get("reviews", [])
            if rev_list:
                # Competitor review text is written by the public, so it —
                # and only it — is fenced (R8, B5 #8): the ratings, review
                # counts and price levels around it are Google's data, and
                # fencing them too meant the figure check could never verify
                # a true "4.5★, 812 reviews", so its UNVERIFIED flag carried
                # no information. See UNTRUSTED_NOTE in the prompt.
                #
                # Each review now carries its age. Google picks these five
                # by its own relevance ranking, not by recency, so without a
                # date a complaint from three years ago read as what a
                # competitor is doing wrong now — and that is what the
                # "DOING POORLY" section was built from.
                reviews_text = "\n  ".join([
                    f'[{r.get("ref")} · {r["rating"]}★, {r.get("time") or "date unknown"}] "{r["text"][:250].strip()}"'
                    for r in rev_list[:5]
                ])
            else:
                reviews_text = "No recent reviews"
            # price_level is a real Google field. It was fetched and used to
            # filter candidates, then thrown away — so the model was asked to
            # judge price positioning from adjectives in five reviews while
            # the actual figure sat unused two functions away.
            _pl = c.get("price_level")
            price_line = f"\n  Google price level: {_PRICE_WORDS.get(_pl, 'not listed')}" if _pl else "\n  Google price level: not listed"
            _prov = c.get("rating_is_provisional")
            _how = c.get("match_basis")
            match_line = f"\n  How this one was selected: {_how}" if _how else ""
            _dist = c.get("distance_m")
            dist_line = f"\n  About {round(_dist/1000, 1)} km away" if _dist else ""
            prov_line = ("\n  NOTE: this rating rests on very few reviews — treat it as provisional "
                         "and do not compare against it as a settled figure.") if _prov else ""
            comp_summary += f"""
- {c["name"]} ({(str(c["rating"]) + "★") if c.get("rating") else "no rating yet"}, {c["review_count"]} reviews){price_line}{dist_line}{match_line}{prov_line}
  Recent customer reviews (with how long ago each was written):
  {wrap_untrusted(reviews_text) if rev_list else reviews_text}
"""

        greeting = f"Hi {owner_name}" if owner_name else "Hi"

        # Build restaurant profile context
        profile = restaurant_profile or {}
        profile_lines = []
        if profile.get("vibe"):
            profile_lines.append(f"Concept/vibe: {profile['vibe']}")
        if profile.get("known_for"):
            profile_lines.append(f"Known for: {profile['known_for']}")
        if profile.get("neighborhood"):
            profile_lines.append(f"Location: {profile['neighborhood']}")
        # If no profile data, try to infer from competitor types as a last resort
        if not profile_lines:
            profile_lines.append(f"Name: {restaurant_name}")
            profile_lines.append("Independent restaurant — focus recommendations on service, hospitality, and marketing")
        profile_context = "\n".join(profile_lines)

        # Add upcoming holidays for timely recommendations
        try:
            from marketing import get_upcoming_holidays as _get_hols_c
            from time_utils import restaurant_now
            _now_hc = restaurant_now(tz_name, naive=True)
            _upcoming_hc = _get_hols_c(_now_hc)
            holiday_rec_context = f"\nUpcoming holidays/events in the next 30 days: {_upcoming_hc}. Consider these when making recommendations." if _upcoming_hc else ""
            today_comp = _now_hc.strftime("%B %d, %Y")
        except Exception:
            holiday_rec_context = ""
            from datetime import datetime as _dt_hc2
            today_comp = _dt_hc2.now().strftime("%B %d, %Y")

        # The recommendations below are asked for as something a manager can
        # start THIS SHIFT and push THIS WEEK, and the only temporal context
        # was a holiday list. Labor already fetches a real NWS forecast and
        # frames it carefully; competitor intel had none of it, so it advised
        # on patio pushes into a week of rain.
        weather_ctx = ""
        try:
            if restaurant_id:
                from models import get_restaurant as _gr_w
                from weather import get_forecast_for_week as _fc
                from datetime import timedelta as _td_w
                _r_w = _gr_w(restaurant_id)
                _days = [(_now_hc + _td_w(days=i)).strftime("%Y-%m-%d") for i in range(7)]
                _fcast = _fc(_r_w, _days) or []
                if _fcast:
                    _lines = [f"  {w['day_name']}: {w['high_f']}°F, {w['short_forecast']}"
                              + (f", {w['precip_pct']}% rain" if w.get("precip_pct") else "")
                              for w in _fcast[:7]]
                    weather_ctx = (
                        "\n\nWeather where this restaurant is, for the week these recommendations "
                        "cover:\n" + "\n".join(_lines) +
                        "\n  Use it only where it changes what is sensible — a patio or outdoor "
                        "push into a wet week, a delivery angle on a cold one. Weather is a nudge, "
                        "never the reason for a recommendation on its own, and a forecast is not "
                        "what will happen."
                    )
        except Exception as _we:
            print(f"[Competitor] weather context unavailable: {_we}")

        # What Cavnar AI remembers about this restaurant (memory audit
        # 9/29/26: memory_context, surface 'competitor_read' — the owner's
        # constraints, the last read and its verdict, the answers they gave),
        # fenced and dated M/D/YY by the reader; "" when there is nothing.
        memory_ctx = ""
        if restaurant_id:
            try:
                import memory_context as _mc
                _mt = _mc.memory_context(restaurant_id, "competitor_read", subjects=("intel",)).text
                if _mt:
                    from ai_guard import MEMORY_FENCE_NOTE as _MFN
                    memory_ctx = ("\n\nWHAT CAVNAR AI REMEMBERS ABOUT THIS RESTAURANT — context, not evidence: a "
                                  "recommendation the owner already answered is not made again, and nothing here "
                                  "is a competitor fact. " + _MFN + "\n" + _mt)
            except Exception as _me:
                print(f"[Competitor] memory unavailable for {restaurant_id}: {_me}")

        from competitor_intel_format import NOTHING_TO_ACT_ON
        prompt = f"""You are the Cavnar AI Consultant analyzing the competitive landscape for {restaurant_name}.
Today's date: {today_comp}{holiday_rec_context}{weather_ctx}

{UNTRUSTED_NOTE}

About {restaurant_name}:
{profile_context}{memory_ctx}

CRITICAL RULES:
- Only recommend actions that fit {restaurant_name}'s actual concept and cuisine
- NEVER recommend menu items or food categories outside their concept (e.g. don't suggest a burger promotion to a breakfast cafe)
- Focus on service quality, marketing angles, atmosphere, timing, and operational strengths
- Recommendations must be something a manager could literally start THIS SHIFT with the staff, menu, and equipment {restaurant_name} already has
- NEVER recommend creating a new dish, adding a menu item, running a "promotion" or "campaign" with no specifics, redesigning the space, buying equipment, or hiring — these take weeks restaurants don't have and are not real advice
- Every recommendation must name a specific, existing lever: a service script change, a staffing/timing adjustment, promoting an EXISTING dish or existing strength on social/signage, a direct fix to a named complaint from the competitor reviews above, or a specific way to win over customers unhappy with a named competitor

Nearby competitors and their recent customer reviews:
{comp_summary}

EVIDENCE RULES — these bound what you may claim:
- Each review carries how long ago it was written. A review over a year old is NOT evidence of what a competitor is doing now. Prefer recent ones, and if you cite an older one, say when it was ("last year", "two years ago").
- Every competitor strength or weakness you state must trace to a review quoted above for THAT named competitor. Never attribute a complaint to a restaurant it was not written about.
- Only name restaurants that appear in the list above. Do not introduce any other business.
- "How this one was selected" tells you how close a match each competitor is. One selected on a widened radius with no cuisine or price constraint is a weaker comparison — do not present it as a direct rival without saying so.
- State no figure — a dollar amount, a percentage, a count — that does not appear above.

Write a competitive intelligence report for {restaurant_name} in this EXACT format with these EXACT headers:

{greeting}, here is your competitive landscape snapshot.

WHAT COMPETITORS ARE DOING WELL:
Write 2-3 bullet points (starting with -). EACH BULLET IS ONE SENTENCE, 12 WORDS OR FEWER. Name the restaurant and the one specific strength — no parenthetical asides, no stacked examples, no explaining why it matters. End each bullet with the ids of THAT restaurant's reviews it rests on, in square brackets, e.g. [R3]. A bullet with no review of that restaurant behind it must not be written.

WHAT COMPETITORS ARE DOING POORLY:
Write 2-3 bullet points (starting with -). EACH BULLET IS ONE SENTENCE, 12 WORDS OR FEWER. Name the restaurant and the one specific complaint — no parenthetical asides, no stacked examples, no explaining why it matters. End each bullet with the ids of THAT restaurant's reviews it rests on, in square brackets, e.g. [R4].

(Do not write a price positioning section — it is computed from the price levels and added for you.)

Recommendations:
Write between ZERO and THREE, numbered "1.", "2.", "3.". Write one only where these reviews give a genuine, specific reason to act this week — never pad to three. Each is 15 words or fewer and ends with the ids of the competitor reviews it rests on, in square brackets, exactly as they appear above, e.g. [R2, R5]. A recommendation with no review behind it must not be written. Kinds that fit:
- an operational or service fix using only what {restaurant_name} already has — a specific script, timing, or staffing change
- a specific EXISTING dish, deal, or strength to push harder in marketing/signage this week — never a new item
- a specific tactic to win a named competitor's dissatisfied customers, tied to an actual complaint quoted above
If nothing in these reviews is worth acting on, write exactly this one line under Recommendations and nothing else: {NOTHING_TO_ACT_ON}

Tone: sharp, direct, trusted business advisor. Every line is a single punchy sentence, not a paragraph — cut qualifiers, cut context, cut anything that isn't the point itself. Name specific competitors and cite specific review themes anyway, just in fewer words. Always use $ signs before dollar amounts."""

        # The readiness gate before the call (DH5-2). This read is written
        # from the rivals fetched for it just now, so its own source (the
        # last competitor read) is what it replaces, never a reason to hold
        # it; the one registry-dated input it carries is the weather.
        import data_health as _dh_ci
        from ai_utils import with_data_state as _with_ds_ci
        _ready_ci = (_dh_ci.readiness(restaurant_id, "intel", sources=("weather",), include_not_connected=False)
                     if restaurant_id else _dh_ci.NOT_APPLICABLE)
        prompt = _with_ds_ci(prompt, _ready_ci)

        import ai_orchestrator
        # On the orchestrator's rung (competitor_insight: T2, one call — AI
        # cost audit 10/7/26, orchestration Phase 3); finish_competitor_insight
        # validates it as before.
        msg = ai_orchestrator.generate("competitor_insight", restaurant_id, lambda route, notes: create_with_retry(
            client,
            restaurant_id=restaurant_id,
            action="competitor_insight",
            readiness=_ready_ci,
            **route.apply(dict(model=model_for("competitor_insight"), max_tokens=900,
                               messages=[{"role": "user", "content": prompt}])),
        ), subject="competitor_insight").result
        if getattr(msg, "stop_reason", None) == "max_tokens":
            # An output problem, filed as one (ledger outcome 'truncated',
            # and an AI-quality event) — not a failing job (#58).
            import ai_utils as _ai_q
            _ai_q.record_quality_event("competitor_insight", "truncated", restaurant_id=restaurant_id,
                                       detail="competitor insight was truncated at max_tokens; no read stored")
            return ""
        text = extract_text(msg).strip()
        return finish_competitor_insight(text, prompt, competitors, restaurant_name,
                                         own_price_level=(restaurant_profile or {}).get("price_level"),
                                         restaurant_id=restaurant_id, owner_name=owner_name,
                                         registry_state=_ready_ci.get("data_state"))
    except Exception as e:
        print(f"[Competitor] generate_competitor_insight error: {e}")
        try:
            import ops
            ops.capture(e, job="competitor_insight", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return ""


# ── the Response Validation Layer on competitor intel (workstream A) ────────
#
# One check between the model's text and the owner (surface "intel"). It
# replaces verify_figures(check_counts) here and adds what intel never had:
# each rating and review count bound to ITS competitor (F2), causes held to
# the cited reviews (K1, association strength), another Cavnar tenant's
# name dropped (T1), injection and certainty rules. The structural
# validators stay: the recommendation and bullet citation checks, the
# computed price line, and _invented_competitors (a multi-word business name
# at a sentence start, which the engine's N1 patterns do not read).

def _intel_context(prompt, competitors, restaurant_name="", restaurant_id=None, owner_name=None, weather=None,
                   registry_state=None):
    """The ValidationContext for one competitor read. Facts: each
    competitor's Google rating (★) and review count, bound to its name;
    the prompt backs anything else it states (hybrid). names_allowed: the
    competitors, the restaurant and its owner — competitors are exempt from
    the tenant list. Untrusted and association-strength anchors: the review
    texts the model was handed. Weather is a missing input when the prompt
    carried no forecast (`weather`: None reads that off the prompt; True
    when it is not known, so nothing is flagged for it)."""
    import re as _re
    import response_validation as rv
    facts, untrusted, anchors = [], [], []
    names = {n for n in (restaurant_name, owner_name) if n}
    for c in competitors or []:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        names.add(name)
        slug = _re.sub(r"\W+", "_", name.lower()).strip("_") or "competitor"
        facts.append(rv.Fact(f"competitor.{slug}.rating", c.get("rating"), "★", "measured", entity=name))
        facts.append(rv.Fact(f"competitor.{slug}.review_count", c.get("review_count"), "count", "measured",
                             entity=name))
        for r in (c.get("reviews") or [])[:5]:
            t = str((r or {}).get("text") or "")[:250].strip()
            if t:
                untrusted.append(t)
                anchors += rv.anchor(t, "association")
    tenants = set()
    if restaurant_id:
        try:
            import models as _m
            tenants = _m.other_tenant_names(restaurant_id)
        except Exception:
            tenants = set()
    if weather is None:
        weather = "Weather where this restaurant is" in (prompt or "")
    missing = [] if weather else ["weather"]
    data_state = {"missing_inputs": missing}
    if registry_state:
        # The registry's state of the forecast this read carried (DH1-2).
        import data_health as _dh_ic
        data_state = _dh_ic.merge_data_state(data_state, registry_state)
    return rv.ValidationContext(
        restaurant_id=restaurant_id, surface="intel", facts=facts, context_text=prompt or "",
        cause_anchors=anchors, names_allowed=names, tenant_names_denied=tenants, untrusted=untrusted,
        confidence=None, data_state=data_state,
        policy={"action": "competitor_insight", "check_counts": True})


def finish_competitor_insight(raw, prompt, competitors, restaurant_name="", own_price_level=None,
                              restaurant_id=None, owner_name=None, registry_state=None):
    """The competitor read as the owner gets it, from the model's raw text:
    the citation checks (recommendations, then the strength and weakness
    bullets), the Response Validation Layer, the computed price line, and
    the legacy "UNVERIFIED:" line (every client's recommendation gate reads
    it) carrying the verdict's caveats and any invented business. Returns a
    Validated str — "" when refused, which the caller treats as no read."""
    # A recommendation stands only on reviews it cites that exist. One
    # citing nothing, or an id that was never handed over, is dropped,
    # and if none survive the section says so honestly (audit #31).
    text = _validate_recommendation_citations(raw or "", competitors, restaurant_id=restaurant_id)
    # Strengths and weaknesses are cite-checked the same way (H11).
    text = _validate_bullets(text, competitors, restaurant_id=restaurant_id)
    import response_validation as rv
    import re as _re_fc
    if registry_state and not _re_fc.search(r"\b(?:weather|rain\w*|snow\w*|storm\w*|forecast|patio|heat|cold)\b",
                                            raw or "", _re_fc.I):
        # The forecast is the only registry-dated input here: an out-of-date
        # one needs disclosing only in a read that leans on it.
        registry_state = {k: v for k, v in registry_state.items() if k != "stale_sources"}
    ctx = _intel_context(prompt, competitors, restaurant_name, restaurant_id, owner_name,
                         registry_state=registry_state)
    return _checked_intel(rv.enforce(text, ctx, marker=False), ctx, competitors, restaurant_name, own_price_level)


def _checked_intel(checked, ctx, competitors, restaurant_name="", own_price_level=None):
    """After the engine (`checked`, rv.enforce's Validated text, over a read
    whose citations were already checked — a fresh one, or a stored one
    being re-validated by current_intel): the computed price line and the
    legacy marker."""
    import response_validation as rv
    verdict = checked.verdict
    if not str(checked).strip():
        return rv.Validated("", validation=checked.validation, verdict=verdict)
    # The price line is computed from the price levels, not written (H11),
    # so it is added after the check.
    text = _with_price_positioning(str(checked), price_positioning(competitors, own_price_level))
    validation = dict(checked.validation or {})
    notes = []
    note = rv.legacy_note(verdict)
    if note:
        notes.append(note.rstrip("."))
    # A named restaurant that was never in the competitor list is an invented
    # competitor, the single worst thing this module can produce. The
    # restaurant's own name is not one.
    # Every name the read may use — the competitors, the restaurant and its
    # owner (the prompt opens the read with "Hi <owner>") — is a known name.
    allowed = [{"name": n} for n in (getattr(ctx, "names_allowed", None) or ()) if n]
    invented = _invented_competitors(text, list(competitors or []) + [{"name": restaurant_name or ""}] + allowed)
    if invented:
        notes.append("names a business that is not in your competitor list: " + ", ".join(invented[:3]))
        validation.update({
            "verdict": "withhold" if validation.get("verdict") in ("pass", "caveat") else validation.get("verdict"),
            "controls": False,
            "codes": list(dict.fromkeys(list(validation.get("codes") or []) + ["N1"])),
            "caveats": list(validation.get("caveats") or []) + [f"A name here isn't in the data: {invented[0]}."]})
    if notes and rv.mode_for(ctx.surface) == "enforce":
        text = text.rstrip() + "\n\nUNVERIFIED: " + "; ".join(notes) + "."
    validation["intel_check"] = INTEL_CHECK
    return rv.Validated(text, validation=validation, verdict=verdict)


def intel_blob(competitors, insight, generated_at, closed_custom=None, discovered_at=None,
               custom_ids=None) -> dict:
    """The restaurants.competitor_intel blob: the read, and beside it the
    verdict it was shown under (`validation`, carrying the engine's
    version), so a later engine version re-validates the stored read on the
    next open (current_intel) instead of serving an old verdict. The model's
    raw text is deliberately NOT stored here: the whole blob is handed to
    the web (/api/competitor-intel), and the raw text still holds what the
    engine took out (another tenant's name, a dropped sentence).

    `discovered_at` (UTC ISO: when the nearby set was last searched, #39) and
    `custom_ids` (the owner-added list the read was made with, #42) are kept
    beside it when given."""
    blob = {"competitors": competitors, "insight": str(insight or ""), "generated_at": generated_at,
            "closed_custom": closed_custom or []}
    if discovered_at:
        blob["discovered_at"] = discovered_at
    if custom_ids is not None:
        blob["custom_ids"] = list(custom_ids)
    val = getattr(insight, "validation", None)
    if val is not None:
        blob["validation"] = val
    return blob


def _stored_intel_context(competitors) -> str:
    """The figures a stored read's prompt held about its competitors —
    name, Google rating, review count, price level, distance, how it was
    selected — rebuilt from the stored competitor list, for re-validating a
    read whose prompt was not kept. Review texts stay fenced, as they were."""
    lines = []
    for c in competitors or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        rating = (str(c["rating"]) + "★") if c.get("rating") else "no rating yet"
        line = f"- {c['name']} ({rating}, {c.get('review_count') or 0} reviews)"
        if c.get("price_level"):
            line += f" Google price level: {_PRICE_WORDS.get(c.get('price_level'), 'not listed')}"
        if c.get("distance_m"):
            line += f" About {round(c['distance_m'] / 1000, 1)} km away"
        if c.get("match_basis"):
            line += f" How this one was selected: {c['match_basis']}"
        revs = [f'[{r.get("ref")} · {r.get("rating")}★, {r.get("time") or "date unknown"}] '
                f'"{str(r.get("text") or "")[:250].strip()}"' for r in (c.get("reviews") or [])[:5] if isinstance(r, dict)]
        if revs:
            line += "\n  " + wrap_untrusted("\n  ".join(revs))
        lines.append(line)
    return "\n".join(lines)


def current_intel(restaurant_id, blob, persist=True):
    """The stored competitor blob, its read re-validated when it was shown
    under an older engine version — or before the engine (no `validation`)
    — with no model call: the shown text, its old "UNVERIFIED:" line and
    computed price line taken off, is checked again against the stored
    competitors (their figures rebuilt as the prompt stated them). Weather
    is not flagged on this path (whether the prompt held a forecast was not
    kept). Written back — only while the row still holds that same read — so
    every reader of the row (the web page, Home, Ask) then sees it. Never
    raises; on any failure the blob comes back as it was."""
    try:
        import response_validation as rv
        if not isinstance(blob, dict):
            return blob
        val = blob.get("validation")
        text = blob.get("insight") or ""
        if (isinstance(val, dict) and val.get("version") == rv.VERSION
                and val.get("intel_check") == INTEL_CHECK) or not str(text).strip():
            return blob
        comps = [c for c in (blob.get("competitors") or []) if isinstance(c, dict)]
        restaurant_name, owner_name = "", None
        if restaurant_id:
            try:
                import models as _m
                _r = _m.get_restaurant(restaurant_id)
                restaurant_name = getattr(_r, "name", "") or ""
                owner_name = getattr(_r, "owner_name", None)
            except Exception:
                pass
        body = _with_price_positioning(rv.strip_marker(text), None)
        ctx = _intel_context(_stored_intel_context(comps), comps, restaurant_name, restaurant_id, owner_name,
                             weather=True)
        out = _checked_intel(rv.enforce(body, ctx, marker=False), ctx, comps, restaurant_name)
        new = dict(blob, insight=str(out), validation=out.validation)
        if persist and restaurant_id:
            import models as _m
            conn = _m.get_conn()
            try:
                row = conn.execute("SELECT competitor_intel FROM restaurants WHERE id=?",
                                   (restaurant_id,)).fetchone()
                cur = json.loads(row[0]) if row and row[0] else {}
                if isinstance(cur, dict) and cur.get("insight") == text and cur.get("validation") == val:
                    conn.execute("UPDATE restaurants SET competitor_intel=? WHERE id=? AND competitor_intel=?",
                                 (json.dumps(new), restaurant_id, row[0]))
                    conn.commit()
            finally:
                conn.close()
            _m._invalidate_request_cache(restaurant_id)
        return new
    except Exception as e:
        print(f"[Competitor] stored read not re-validated: {e}")
        return blob


# Words in a restaurant's name too generic to identify it on their own.
_GENERIC_NAME_WORDS = {"restaurant", "kitchen", "grill", "cafe", "café", "house", "bistro", "pizza", "pizzeria",
                       "tavern", "diner", "eatery", "bar", "pub", "the", "and", "co", "company", "taqueria",
                       "cantina", "trattoria", "steakhouse", "brewing", "brewery", "bakery", "deli", "express"}


def _named_competitors(line, competitors) -> list:
    """The competitors a line names — the full name, or its first word
    when that word identifies it (not "The", not "Pizza")."""
    import re as _re
    low = str(line or "").lower()
    out = []
    for c in competitors or []:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        first = _re.sub(r"['’]s$", "", name.split()[0].lower())
        if name.lower() in low or (len(first) >= 4 and first not in _GENERIC_NAME_WORDS
                                   and _re.search(rf"(?<![a-z]){_re.escape(first)}", low)):
            out.append(c)
    return out


def _refs_of(competitors) -> set:
    return {str(r.get("ref")).upper() for c in competitors or [] for r in (c.get("reviews") or []) if r.get("ref")}


def _cites_about_named(line, cites, competitors) -> bool:
    """A line that names a competitor may cite only that competitor's
    reviews (H11): an id that exists but belongs to another restaurant is a
    complaint attributed to the wrong place. A line naming none is left to
    the existence check."""
    named = _named_competitors(line, competitors)
    if not named:
        return True
    own = _refs_of(named)
    return all(c in own for c in cites)


# Words a strength or weakness bullet uses whatever it claims; sharing only
# these with a cited review is no support.
_SUPPORT_GENERIC = {"known", "great", "good", "best", "nice", "really", "very", "always", "never", "town",
                    "restaurant", "diner", "place", "competitor", "customers", "customer", "people", "their",
                    "strong", "weak", "poor", "popular", "loved", "love", "loves", "consistently"}


def _cites_support(line, cites, competitors) -> bool:
    """Whether the reviews a bullet cites say what the bullet says (R13, B5
    #17): at least one content word of the claim — the competitor's own name
    and generic praise left out — appears in a cited review. The id check
    proved only that the review exists and is that competitor's: "best patio
    in town [R3]" stood on a review about cold coffee."""
    from ai_guard import _content_stems
    by_ref = {str(r.get("ref")).upper(): str(r.get("text") or "") for c in competitors or []
              for r in (c.get("reviews") or []) if r.get("ref")}
    names = set()
    for c in competitors or []:
        names |= _content_stems(str(c.get("name") or ""))
    claim = {w for w in _content_stems(line) if w not in names and w not in _SUPPORT_GENERIC}
    if not claim:
        return False
    cited = set()
    for ref in cites or []:
        cited |= _content_stems(by_ref.get(str(ref).upper(), ""))
    return bool(claim & cited)


def _validate_bullets(text, competitors, restaurant_id=None):
    """The DOING WELL / DOING POORLY bullets, cite-checked (H11). Each bullet
    must name a competitor and end with the ids of that competitor's reviews
    it rests on; one that cites nothing, an id never handed over, or another
    restaurant's review is dropped. The ids are taken off the kept bullets
    — the section reads as it always did — and a section left with no
    bullet is removed with its header."""
    import re as _re
    from competitor_intel_format import split_citations
    known = _refs_of(competitors)
    out, dropped = [], 0
    section = None
    pending_header = None
    kept_in_section = 0
    for line in (text or "").splitlines():
        st = line.strip()
        head = _re.match(r"^\**\s*WHAT COMPETITORS ARE DOING (WELL|POORLY)\s*:?\s*\**\s*$", st, _re.I)
        if head:
            section = head.group(1).upper()
            pending_header, kept_in_section = line, 0
            continue
        if section and _re.match(r"^\**\s*(PRICE POSITIONING|Recommendations?)\b", st, _re.I):
            section = None
            pending_header = None
        if section and st.startswith("-"):
            body, cites = split_citations(st.lstrip("- ").strip())
            if (cites and all(c in known for c in cites) and _named_competitors(body, competitors)
                    and _cites_about_named(body, cites, competitors)
                    and _cites_support(body, cites, competitors)):
                if pending_header is not None:
                    out.append(pending_header)
                    pending_header = None
                out.append(f"- {body}")
                kept_in_section += 1
            else:
                dropped += 1
            continue
        if section and not st and pending_header is not None:
            continue
        out.append(line)
    if dropped:
        # An AI-quality finding (rate on the AI page), not a failing job (#58).
        import ai_utils as _ai_q
        _ai_q.record_quality_event(
            "competitor_insight", "citation_dropped", n=dropped,
            restaurant_id=restaurant_id if restaurant_id is not None else _ai_q._context_restaurant(),
            detail=f"{dropped} strength/weakness bullet(s) without a valid citation to the named competitor dropped")
    return "\n".join(out)


_PRICE_WORDS = {1: "$ (inexpensive)", 2: "$$ (moderate)", 3: "$$$ (expensive)", 4: "$$$$ (very expensive)"}


def price_positioning(competitors, own_level=None) -> str | None:
    """The PRICE POSITIONING sentence, computed from Google's price levels —
    a real field — rather than written by the model (H11). None when fewer
    than two competitors list one, the rule the prompt used to state."""
    levels = []
    for c in competitors or []:
        try:
            lv = int(c.get("price_level"))
        except (TypeError, ValueError):
            continue
        if 1 <= lv <= 4:
            levels.append(lv)
    if len(levels) < 2:
        return None
    counts = {lv: levels.count(lv) for lv in sorted(set(levels))}
    top = max(counts, key=lambda lv: (counts[lv], -lv))
    spread = ", ".join(f"{n} at {'$' * lv}" for lv, n in counts.items())
    line = (f"{counts[top]} of the {len(levels)} competitors that list a Google price level sit at "
            f"{_PRICE_WORDS[top]}" + (f" ({spread})." if len(counts) > 1 else "."))
    try:
        own = int(own_level) if own_level is not None else None
    except (TypeError, ValueError):
        own = None
    if own and 1 <= own <= 4:
        rel = "the same level as" if own == top else ("above" if own > top else "below")
        line += f" Your own listing is {'$' * own}, {rel} most of them."
    return line


def _with_price_positioning(text, sentence):
    """The insight with its PRICE POSITIONING section replaced by the
    computed sentence, or removed when there is none."""
    import re as _re
    lines = (text or "").splitlines()
    out, skipping = [], False
    for line in lines:
        st = line.strip()
        if _re.match(r"^\**\s*PRICE POSITIONING\s*:?\s*\**", st, _re.I):
            skipping = True
            continue
        if skipping:
            if _re.match(r"^\**\s*(WHAT COMPETITORS|Recommendations?)\b", st, _re.I):
                skipping = False
            else:
                continue
        out.append(line)
    body = "\n".join(out)
    if not sentence:
        return body
    m = _re.search(r"(?im)^\s*\**\s*Recommendations?\s*:?\s*\**\s*$", body)
    block = f"PRICE POSITIONING:\n{sentence}\n\n"
    return (body[:m.start()] + block + body[m.start():]) if m else (body.rstrip() + "\n\n" + block.rstrip())


def spot_check_menu(summary, source_text) -> str:
    """A menu extraction with every item the source text does not contain
    taken out (H11). The extraction is "Signature dishes: a, b. Mains: c."
    — each listed item is kept only when its significant words appear in
    the page or PDF it was read from, so an item the model imagined never
    reaches the competitor read. "" when nothing survives."""
    import re as _re
    src = " ".join(_re.findall(r"[a-z0-9]+", str(source_text or "").lower()))
    if not summary or not src:
        return ""
    src_words = set(src.split())

    src_numbers = set(_re.findall(r"\d+(?:\.\d+)?", str(source_text or "")))

    def present(item):
        words = [w for w in _re.findall(r"[a-z0-9]+", item.lower()) if len(w) >= 3]
        # A price is checked whatever its length (R13, B5 #17): words under
        # three characters were skipped, so "$19" passed on a menu reading 14.
        nums = _re.findall(r"\d+(?:\.\d+)?", item)
        return (bool(words) and all(w in src_words or w.rstrip("s") in src_words for w in words if not w.isdigit())
                and all(n in src_numbers for n in nums))

    out_parts, kept_total = [], 0
    for part in _re.split(r"(?<=\.)\s+(?=[A-Z][A-Za-z ]{2,30}:)", summary.strip()):
        m = _re.match(r"^\s*([A-Za-z][A-Za-z /&-]{1,40}):\s*(.*)$", part.strip(), _re.S)
        if not m:
            continue
        label, items = m.group(1).strip(), m.group(2).strip().rstrip(".")
        items = items.strip("[]")
        kept = [i.strip() for i in _re.split(r",|;", items) if i.strip() and present(i.strip())]
        if kept:
            out_parts.append(f"{label}: {', '.join(kept)}.")
            kept_total += len(kept)
    return " ".join(out_parts) if kept_total else ""


def _validate_recommendation_citations(text, competitors, restaurant_id=None):
    """Rewrite the Recommendations section keeping only lines whose
    citations all resolve to a review the model was given — and, when the
    line names a competitor, to THAT competitor's reviews (H11). Nothing
    else in the text changes."""
    import re as _re
    from competitor_intel_format import split_citations, NOTHING_TO_ACT_ON
    known = {str(r.get("ref")).upper() for c in competitors for r in (c.get("reviews") or []) if r.get("ref")}
    m = _re.search(r"(?im)^\s*\**\s*Recommendations?\s*:?\s*\**\s*$", text or "")
    if not m:
        return text
    head, body = text[:m.end()], text[m.end():]
    # The section runs to a blank-line-separated paragraph that is not a
    # numbered line (the closing Tone line is never output, but be safe).
    kept, dropped, rest = [], 0, []
    in_list = True
    for line in body.splitlines():
        st = line.strip()
        if not st:
            if kept or dropped:
                in_list = False
            continue
        if in_list and _re.match(r"^\d+[.)]\s+", st):
            content = _re.sub(r"^\d+[.)]\s+", "", st)
            _body, cites = split_citations(content)
            if cites and all(c in known for c in cites) and _cites_about_named(_body, cites, competitors):
                kept.append(f"{_body} [{', '.join(cites)}]")
            else:
                dropped += 1
            continue
        if in_list and _re.match(r"^nothing (?:worth acting on|to act on)", st, _re.I):
            continue
        in_list = False
        rest.append(line)
    if dropped:
        # An AI-quality finding (rate on the AI page), not a failing job (#58).
        import ai_utils as _ai_q
        _ai_q.record_quality_event(
            "competitor_insight", "citation_dropped", n=dropped,
            restaurant_id=restaurant_id if restaurant_id is not None else _ai_q._context_restaurant(),
            detail=f"{dropped} recommendation(s) without a valid review citation dropped")
    lines = [f"{i}. {k}" for i, k in enumerate(kept[:3], 1)] or [NOTHING_TO_ACT_ON]
    out = head.rstrip() + "\n" + "\n".join(lines)
    if rest:
        out += "\n\n" + "\n".join(rest)
    return out


def _previous_competitor(restaurant, place_id):
    """This competitor as the last stored analysis had it, or None."""
    try:
        blob = json.loads(getattr(restaurant, "competitor_intel", None) or "{}")
    except (TypeError, ValueError):
        return None
    for c in (blob.get("competitors") or []) if isinstance(blob, dict) else []:
        if isinstance(c, dict) and c.get("place_id") == place_id:
            return {k: v for k, v in c.items() if k not in _REVIEW_KEYS}
    return None


# The keys a stored competitor carries its reviews under: `reviews` (the set
# the read was written from) and `reviews_at` (when that set was fetched);
# `latest_reviews` / `latest_reviews_at`, the newest five the daily check
# read (AI cost audit 10/7/26 #15).
_REVIEW_KEYS = ("reviews", "reviews_at", "latest_reviews", "latest_reviews_at")


def _stored_blob(restaurant) -> dict:
    """restaurants.competitor_intel as a dict ({} when none or unreadable)."""
    try:
        blob = json.loads(getattr(restaurant, "competitor_intel", None) or "{}")
    except (TypeError, ValueError):
        return {}
    return blob if isinstance(blob, dict) else {}


def _fresh_reviews(c, hours=REVIEWS_REUSE_HOURS):
    """(reviews, fetched_at) a stored competitor already holds from inside
    the last `hours` — the daily check's newest five first, else the set
    the last run fetched — or (None, None) when it must be looked up."""
    if not isinstance(c, dict):
        return None, None
    for key, at in (("latest_reviews", "latest_reviews_at"), ("reviews", "reviews_at")):
        age = _stamp_age_hours(c.get(at))
        if age is not None and age < hours and isinstance(c.get(key), list):
            return list(c[key]), c[at]
    return None, None


def _newest_ts(reviews):
    """The newest review's epoch second in a stored set, or None. A set
    stored before reviews carried `ts` is read by its ISO `date` — the end
    of that day, so a review from the same day is never counted newer."""
    best = None
    for r in reviews or []:
        if not isinstance(r, dict):
            continue
        ts = r.get("ts")
        if not isinstance(ts, (int, float)) and r.get("date"):
            try:
                from datetime import datetime as _dt, timezone as _tz
                ts = int(_dt.fromisoformat(str(r["date"])[:10]).replace(tzinfo=_tz.utc).timestamp()) + 86399
            except ValueError:
                ts = None
        if isinstance(ts, (int, float)) and (best is None or ts > best):
            best = ts
    return best


def _newer_reviews(latest, analysed) -> int:
    """How many of the daily check's reviews are newer than every review the
    last read was written from (#15: the one thing a read cannot already
    know). A read written from no reviews counts every one."""
    floor = _newest_ts(analysed)
    return sum(1 for r in latest or [] if isinstance(r, dict) and isinstance(r.get("ts"), (int, float))
               and (floor is None or r["ts"] > floor))


def _custom_ids(restaurant) -> list:
    """The owner-added competitors' Place IDs, in order, duplicates dropped."""
    out = []
    for pid in str(getattr(restaurant, "custom_competitors", None) or "").split(","):
        pid = pid.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def _custom_signature(blob) -> set:
    """The owner-added competitors the stored read was made with: its
    `custom_ids`, or — on a read stored before it kept them — its custom
    entries and the owner-added ones it found closed."""
    if not isinstance(blob, dict):
        return set()
    if isinstance(blob.get("custom_ids"), list):
        return {str(p) for p in blob["custom_ids"]}
    ids = {c.get("place_id") for c in blob.get("competitors") or [] if isinstance(c, dict) and c.get("custom")}
    ids |= {c.get("place_id") for c in blob.get("closed_custom") or [] if isinstance(c, dict)}
    return {p for p in ids if p}


def _discovery_due(blob, custom_ids) -> bool:
    """Whether this run searches Google for the nearby set again (AI cost
    audit 10/7/26 #39): when there is no stored set, when the last search is
    REDISCOVER_DAYS old (so names, types and price levels never sit past
    Google's 30-day content limit), or when the owner's own list changed.
    Otherwise the run re-reads the stored place_ids."""
    if not any(isinstance(c, dict) and c.get("place_id") and not c.get("custom")
               for c in blob.get("competitors") or []):
        return True
    age = _stamp_age_hours(blob.get("discovered_at"))
    if age is None or age >= REDISCOVER_DAYS * 24:
        return True
    return _custom_signature(blob) != set(custom_ids or [])


def _own_listing_fresh(google_place_id, days=REDISCOVER_DAYS) -> bool:
    """Whether the restaurant's own types and price level were read from
    Google inside `days` (_remember_own_listing stamps it)."""
    try:
        import models as _m
        conn = _m.get_conn()
        try:
            row = conn.execute("SELECT updated_at FROM job_cursors WHERE key=?",
                               (_own_listing_key(google_place_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    age = _stamp_age_hours(row["updated_at"]) if row else None
    return age is not None and age < days * 24


def _stored_own_listing(restaurant):
    """The restaurant's own location, types and price level as stored
    (restaurants.latitude / longitude / google_types / google_price_level),
    for a discovery that then needs no Details call of its own (#39) — or
    None when any is missing or the types are past Google's 30-day limit."""
    lat, lng = getattr(restaurant, "latitude", None), getattr(restaurant, "longitude", None)
    try:
        types = json.loads(getattr(restaurant, "google_types", None) or "null")
    except (TypeError, ValueError):
        types = None
    if lat is None or lng is None or not isinstance(types, list) or not types:
        return None
    if not _own_listing_fresh(restaurant.google_place_id):
        return None
    return {"lat": lat, "lng": lng, "types": types, "price_level": getattr(restaurant, "google_price_level", None),
            "name": getattr(restaurant, "name", "") or ""}


def _apply_lookup(c, res, at):
    """Fold one Details answer for a stored competitor into its entry: the
    numbers, the open/closed status, the newest reviews."""
    if res.get("name"):
        c["name"] = res["name"]
    if res.get("rating") is not None:
        c["rating"] = res["rating"]
    if res.get("user_ratings_total") is not None:
        c["review_count"] = int(res.get("user_ratings_total") or 0)
        c["rating_is_provisional"] = c["review_count"] < MIN_REVIEWS_FOR_A_MEANINGFUL_RATING
    if res.get("business_status"):
        c["business_status"] = res["business_status"]
    c["reviews"], c["reviews_at"] = _reviews_from(res), at
    return c



# A tracked competitor missing from this week's search is usually not closed:
# the set is the top matches of a Google "nearby" search whose ranking and
# relaxing filters move week to week (owner, 9/29/26: "they're obviously still
# there"). Only Google's own business_status says a place closed — asked once
# per dropout, a few a week at most.
CLOSURE_CHECKS_MAX = 10
CLOSED_STATUSES = ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY")


def _status_from_daily_check(restaurant_id, prev_blob):
    """{place_id: business_status} as the daily ratings check stored it,
    when that check ran today or yesterday on the restaurant's clock — the
    closure check then asks Google nothing for them (AI cost audit 10/7/26
    #41). {} otherwise."""
    if not isinstance(prev_blob, dict) or not prev_blob.get("ratings_checked_at"):
        return {}
    try:
        from datetime import timedelta as _td
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id).date()
        checked = str(prev_blob["ratings_checked_at"])[:10]
        if checked not in (today.isoformat(), (today - _td(days=1)).isoformat()):
            return {}
    except Exception:
        return {}
    return {c["place_id"]: c["business_status"] for c in prev_blob.get("competitors") or []
            if isinstance(c, dict) and c.get("place_id") and c.get("business_status")}


def _closures_among_dropped(restaurant_id, competitors, closed_custom=(), prev_blob=None, known_status=None):
    """[{place_id, name, status}] for places tracked on the last run, missing
    from this one, that Google says are closed — plus owner-added ones found
    closed this run. A status this run already read (`known_status`) or the
    daily check stored today or yesterday (`prev_blob`) is used as it is; only
    the rest are asked. A lookup that fails says nothing. Never raises."""
    known = dict(_status_from_daily_check(restaurant_id, prev_blob))
    known.update(known_status or {})
    out = [dict(c) for c in closed_custom or () if c.get("status") in CLOSED_STATUSES]
    seen = {c["place_id"] for c in out}
    try:
        from models import get_conn
        conn = get_conn()
        try:
            last = conn.execute("SELECT MAX(DATE(captured_at)) FROM competitor_snapshots WHERE restaurant_id=?",
                                (restaurant_id,)).fetchone()[0]
            prev = {r[0]: r[1] for r in conn.execute(
                "SELECT place_id, name FROM competitor_snapshots WHERE restaurant_id=? AND DATE(captured_at)=?",
                (restaurant_id, last))} if last else {}
        finally:
            conn.close()
    except Exception as e:
        print(f"[Competitor] closure check skipped: {e}")
        return out
    now = {c.get("place_id") for c in competitors or []}
    dropped = [(pid, name) for pid, name in prev.items() if pid not in now and pid not in seen]
    asked = 0
    for pid, name in dropped:
        if pid in known:
            if known[pid] in CLOSED_STATUSES:
                out.append({"place_id": pid, "name": name, "status": known[pid]})
            continue
        if asked >= CLOSURE_CHECKS_MAX:
            continue
        asked += 1
        try:
            r = _places_request("details", {"place_id": pid, "fields": "name,business_status",
                                            "key": PLACES_API_KEY}, action="competitor_closure_check", timeout=8)
            d = (r.json() or {}).get("result") or {}
            status = d.get("business_status")
            if status in CLOSED_STATUSES:
                out.append({"place_id": pid, "name": d.get("name") or name, "status": status})
        except Exception as e:
            print(f"[Competitor] closure check for {pid} failed: {e}")
    return out

def _intel_run(fn):
    """Every Places request and the model call inside one analysis run are
    attributed to its restaurant and to one correlation id for the run
    (ai_utils.ai_context, fix round G #148), whether the weekly job, the
    owner's refresh or an admin started it. A decorator, so the run keeps
    its own name and body."""
    import functools

    @functools.wraps(fn)
    def wrapper(restaurant_id, *args, **kwargs):
        import ai_utils as _ai_ctx
        with _ai_ctx.ai_context(restaurant_id=restaurant_id, action="competitor_intel",
                                correlation_id=_ai_ctx.new_correlation_id("intel")):
            return fn(restaurant_id, *args, **kwargs)
    return wrapper


import contextlib as _contextlib
import contextvars as _contextvars

# Set while an owner's (or an admin's) Refresh runs the analysis: the
# nearby set is searched again whatever its age (#39 "on owner request").
# A context variable, not an argument, so the job runner's call stays
# run_competitor_analysis(restaurant_id).
_REDISCOVER = _contextvars.ContextVar("competitor_rediscover", default=False)


@_contextlib.contextmanager
def rediscovery_requested():
    """Within this block, run_competitor_analysis searches Google for the
    nearby set again instead of re-reading the stored one."""
    token = _REDISCOVER.set(True)
    try:
        yield
    finally:
        _REDISCOVER.reset(token)


@_intel_run
def run_competitor_analysis(restaurant_id: int, rediscover=None) -> dict:
    """Full pipeline: fetch competitors, get reviews, generate insight.

    The Places spend is kept to what is new (AI cost audit 10/7/26): the
    nearby search runs only when discovery is due (#39, `_discovery_due`, or
    `rediscover` / rediscovery_requested()) — otherwise the stored
    place_ids are re-read; the restaurant's own location comes from what is
    stored; a competitor whose newest reviews were read inside
    REVIEWS_REUSE_HOURS is not looked up again (#15); an owner-added one is
    one Details call with its reviews (#40); and every Details call is
    served from the day's cache when the place was already read today (#13)."""
    try:
        from models import get_restaurant, get_conn, update_restaurant
        restaurant = get_restaurant(restaurant_id)
        if not restaurant or not restaurant.google_place_id:
            return {"ok": False, "error": "No Google Place ID set"}

        prev = _stored_blob(restaurant)
        prev_by_id = {c["place_id"]: c for c in prev.get("competitors") or []
                      if isinstance(c, dict) and c.get("place_id")}
        custom_ids = _custom_ids(restaurant)
        if len(custom_ids) > CUSTOM_COMPETITORS_MAX:
            # The add routes refuse an eleventh; an admin-typed list longer
            # than that is read up to the cap, every one a Details call a run.
            print(f"[Competitor] {len(custom_ids)} owner-added competitors for {restaurant_id}; "
                  f"the first {CUSTOM_COMPETITORS_MAX} are read")
            custom_ids = custom_ids[:CUSTOM_COMPETITORS_MAX]
        if rediscover is None:
            rediscover = _REDISCOVER.get() or _discovery_due(prev, custom_ids)
        _run_at = _now_stamp()
        _known_status = {}

        from ai_utils import PlacesError as _PlacesError
        _usage = {}
        if rediscover:
            try:
                _own = _stored_own_listing(restaurant)
                _kw = {"usage": _usage}
                if _own:
                    _kw["own"] = _own
                competitors = get_nearby_competitors(restaurant.google_place_id, **_kw)
            except PlacesUnavailable as pu:
                # Refused before it was sent (no key, the Places breaker, this
                # restaurant's Places ceiling) — already a blocked ledger row.
                return {"ok": False, "error": pu.owner_message, "places_status": pu.reason}
            except _PlacesError as pe:
                try:
                    import ops
                    ops.capture(pe, job="competitor_intel", context=f"restaurant_id={restaurant_id}")
                except Exception:
                    pass
                return {"ok": False, "error": pe.owner_message, "places_status": pe.status}
            _discovered_at = _run_at
            # A place tracked before brings the reviews it was read with
            # inside the reuse window (#15) — a Details call saved per place.
            for c in competitors:
                _reviews, _at = _fresh_reviews(prev_by_id.get(c.get("place_id")))
                if _reviews is not None:
                    c["reviews"], c["reviews_at"] = _reviews, _at
        else:
            # The stored set, re-read (#39): Google allows a place_id to be
            # kept indefinitely; the figures beside it are refreshed below.
            # A place the daily check found closed leaves the set and is
            # reported by the closure check from that status.
            competitors = []
            for c in prev.get("competitors") or []:
                if not isinstance(c, dict) or not c.get("place_id") or c.get("custom"):
                    continue
                if c.get("business_status") in CLOSED_STATUSES:
                    _known_status[c["place_id"]] = c["business_status"]
                    continue
                entry = {k: v for k, v in c.items() if k not in _REVIEW_KEYS and k != "stale"}
                _reviews, _at = _fresh_reviews(c)
                if _reviews is not None:
                    entry["reviews"], entry["reviews_at"] = _reviews, _at
                else:
                    entry["_refresh"] = True
                competitors.append(entry)
            _discovered_at = prev.get("discovered_at")
        # Every Places request this run makes — own details, one to three
        # nearby searches, a details per competitor, the owner-added ones —
        # is metered by places_request as it is made (#123).

        # Add any manually specified competitor Place IDs
        _closed_custom = []
        if custom_ids:
            existing_ids = {c['place_id'] for c in competitors}
            for pid in custom_ids:
                if pid not in existing_ids:
                    # Read inside the reuse window (the daily check covers
                    # owner-added ones too): carried as it stands, no call.
                    _last_full = prev_by_id.get(pid)
                    _reviews, _at = _fresh_reviews(_last_full) if (_last_full or {}).get("custom") else (None, None)
                    if _reviews is not None and _last_full.get("business_status", "OPERATIONAL") == "OPERATIONAL":
                        entry = {k: v for k, v in _last_full.items() if k not in _REVIEW_KEYS and k != "stale"}
                        entry.update(custom=True, match_basis="added by you", reviews=_reviews, reviews_at=_at)
                        competitors.append(entry)
                        existing_ids.add(pid)
                        continue
                    try:
                        # One call, reviews included (#40): it was a details
                        # call here and a second for the reviews below.
                        r = _places_request("details", {
                            "place_id": pid,
                            "fields": CUSTOM_FIELDS,
                            "reviews_sort": "newest",
                            "key": PLACES_API_KEY,
                        }, timeout=8)
                        d = r.json().get("result", {})
                        # An owner-added competitor skipped every check the
                        # discovered ones run, including whether it is still
                        # trading. A restaurant that closed two years ago
                        # stayed in the comparison forever with its frozen
                        # rating, and the AI wrote strategy against it.
                        _status = d.get("business_status") or "OPERATIONAL"
                        if d.get("name"):
                            _known_status[pid] = _status
                        if d.get("name") and _status == "OPERATIONAL":
                            competitors.append({
                                "place_id": pid,
                                "name": d["name"],
                                "rating": d.get("rating"),
                                "review_count": d.get("user_ratings_total", 0),
                                "vicinity": d.get("vicinity", ""),
                                "types": d.get("types", []),
                                "price_level": d.get("price_level"),
                                "match_basis": "added by you",
                                "custom": True,
                                "business_status": _status,
                                # The same floor the discovered ones carry: a
                                # rating on too few reviews is provisional
                                # whoever added the place (B6 sub-audit).
                                "rating_is_provisional": int(d.get("user_ratings_total") or 0)
                                                         < MIN_REVIEWS_FOR_A_MEANINGFUL_RATING,
                                "reviews": _reviews_from(d),
                                "reviews_at": _run_at,
                            })
                        elif d.get("name"):
                            print(f"[Competitor] custom competitor {d['name']} is {_status} — skipped")
                            _closed_custom.append({"place_id": pid, "name": d["name"],
                                                   "status": _status})
                    except Exception as ce:
                        print(f"[Competitor] Could not fetch custom competitor {pid}: {ce}")
                        # Our lookup failing is not the rival closing. It was
                        # dropped from the set and the next roster comparison
                        # told the owner it was "gone" (MOD-INT-8). Carry the
                        # last known entry, marked as not refreshed.
                        _last = _previous_competitor(restaurant, pid)
                        if _last:
                            competitors.append(dict(_last, custom=True, stale=True,
                                                    match_basis="added by you — not refreshed this run"))
                            existing_ids.add(pid)

        if not competitors:
            return {"ok": False, "error": "No nearby competitors found"}

        # Enrich with reviews in parallel — 5 sequential calls → 1 parallel
        # batch — for the competitors not already holding fresh ones: a
        # stored competitor gets one Details call that refreshes its figures
        # with its reviews; a just-discovered one (its figures came with the
        # search) only its reviews.
        def _enrich(c):
            if c.pop("_refresh", False):
                try:
                    res = _lookup_competitor(c["place_id"])
                except PlacesUnavailable:
                    res = None
                if res is not None:
                    return _apply_lookup(c, res, _now_stamp())
                return dict(c, reviews=[], stale=True)
            return dict(c, reviews=get_competitor_reviews(c["place_id"]), reviews_at=_now_stamp())

        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        from ai_utils import context_runner as _ctx_runner
        _todo = [i for i, c in enumerate(competitors) if "reviews" not in c]
        if _todo:
            with ThreadPoolExecutor(max_workers=5) as _pool:
                # Each worker runs in a copy of this run's ai_context, so every
                # review lookup is metered to this restaurant and this run.
                _futs = {_pool.submit(_ctx_runner(_enrich), competitors[i]): i for i in _todo}
                for _fut in _as_completed(_futs):
                    competitors[_futs[_fut]] = _fut.result()
        for c in competitors:
            if c.get("business_status") in CLOSED_STATUSES and not c.get("custom"):
                _known_status[c["place_id"]] = c["business_status"]
        # A stored competitor found closed by this run's own lookup leaves.
        competitors = [c for c in competitors
                       if c.get("custom") or c.get("business_status") not in CLOSED_STATUSES]
        if not competitors:
            return {"ok": False, "error": "No nearby competitors found"}

        insight = generate_competitor_insight(
            restaurant.name, competitors,
            owner_name=restaurant.owner_name,
            restaurant_profile={
                "vibe": restaurant.vibe or "",
                "known_for": restaurant.known_for or "",
                "neighborhood": restaurant.neighborhood or "",
            },
            tz_name=getattr(restaurant, "timezone", None),
            restaurant_id=restaurant_id,
        )

        # generate_competitor_insight returns "" on any failure. That empty
        # string used to be stored and the freshness timestamp stamped with
        # it, so the dashboard showed a competitor set with no analysis under
        # today's date and only the ops digest knew anything was wrong.
        if not (insight or "").strip():
            return {"ok": False, "error": "Competitor analysis could not be generated",
                    "competitors": competitors}

        # Store in DB — stamped in the restaurant's local time so "generated
        # today" reads correctly on their dashboard
        from time_utils import restaurant_now
        _now_ct = restaurant_now(restaurant, naive=True)
        # The read's verdict and engine version are stored beside it
        # (intel_blob), so a later engine version re-validates it on read.
        result = intel_blob(competitors, insight, _now_ct.strftime("%Y-%m-%d"), _closed_custom,
                            discovered_at=_discovered_at, custom_ids=custom_ids)
        # The freshness stamp is UTC with an offset (DH1-16): the local wall
        # clock with no offset was read as UTC by ai_guard.freshness and
        # time_utils.parse_stamp, five to ten hours off. The blob's own date
        # above stays the restaurant's local day, which is what it displays.
        from datetime import datetime as _dt_utc, timezone as _tz
        conn = get_conn()
        conn.execute(
            "UPDATE restaurants SET competitor_intel=?, competitor_updated_at=? WHERE id=?",
            (json.dumps(result), _dt_utc.now(_tz.utc).isoformat(timespec="seconds"), restaurant_id)
        )
        conn.commit()
        conn.close()
        import models as _models_inv
        _models_inv._invalidate_request_cache(restaurant_id)
        # The read kept as history (ai_reads, memory audit 9/29/26): the blob is
        # overwritten every Monday, so what last week's read said was gone; the
        # next competitor read sees it through memory_context's last_claim section.
        try:
            import ai_reads
            ai_reads.record_read(restaurant_id, "competitor_read", str(insight), subject="intel",
                                 meta={"kind": "competitor", "verdict": ai_reads.verdict_of(insight),
                                       "competitors": [c.get("name") for c in competitors][:8],
                                       "date": _now_ct.strftime("%Y-%m-%d")})
        except Exception as _are:
            print(f"[Competitor] read not kept as history: {_are}")
        # One JSON blob overwritten every Monday was the entire record, so
        # nothing could show that a competitor's rating fell, that a new one
        # opened, or that a complaint theme appeared. A snapshot per run is
        # what makes any of that answerable later.
        # Before this run's snapshot: which of last run's places that dropped
        # out Google says closed (not every dropout is a closure).
        _closed = _closures_among_dropped(restaurant_id, competitors, _closed_custom,
                                          prev_blob=prev, known_status=_known_status)
        try:
            from models import record_competitor_snapshot
            record_competitor_snapshot(restaurant_id, competitors)
        except Exception as _se:
            print(f"[Competitor] snapshot failed: {_se}")
        # The market's history, kept forever (memory audit 9/29/26,
        # public_history): the snapshots are pruned at a year, so openings,
        # closures and rating moves are kept as events, with a monthly
        # rating series. Never raises. Dated on the restaurant's clock, as
        # the read above is — the server's UTC date is tomorrow from 7pm
        # Central (event re-audit 2 RX-06).
        try:
            import event_memory
            event_memory.record_market_snapshot(restaurant_id, competitors, closed=_closed, at=_now_ct)
        except Exception as _me:
            print(f"[Competitor] market history not kept: {_me}")
        print(f"[Competitor] Analysis complete for {restaurant.name}")
        try:
            from webhooks import fire_webhook as _fw_intel
            _fw_intel(restaurant_id, "intel.updated", {
                "competitors_analyzed": len(competitors),
                "generated_at": result["generated_at"],
            })
        except Exception:
            pass
        # The model's raw text and the prompt stay in the stored row for
        # re-validation; they are not part of what a caller is handed.
        return {"ok": True, **{k: v for k, v in result.items() if k not in ("insight_raw", "validation_input")}}
    except Exception as e:
        print(f"[Competitor] run_competitor_analysis error: {e}")
        from ai_guard import safe_error
        return {"ok": False, "error": safe_error(e, "Competitor analysis could not be completed.")}


# ── the daily ratings check (owner, 10/2/26) ─────────────────────────────────
#
# The full analysis (nearby search, details, reviews and a Claude read) runs
# weekly — a week-old rating comparison read as current until Monday. Every
# morning this re-reads ONLY the tracked competitors — rating, review count,
# open/closed status and their newest five reviews, one Places details call
# each (~13), never a search, never a model call — and the restaurant's own
# rating when nothing fresher is on file. The stored comparison is updated
# in place. The full analysis runs again the same morning only for what a
# stored read cannot already say (AI cost audit 10/7/26 #14): a competitor
# closed, reviews newer than the ones the read was written from (#15), or a
# burst of new reviews. A rating move alone is not one of them — the new
# figure is written into the comparison here, and a Claude read is not
# bought to restate it.
RATING_MOVE = 0.1             # stars, either way — reported, not a reason to re-read
REVIEW_BURST = 15             # new reviews since the last check
DAILY_CHECK_MAX = 15          # competitors re-read per restaurant per day


def _own_rating_fresh(restaurant, hours=OWN_RATING_FRESH_HOURS) -> bool:
    """Whether the restaurant's own public rating was read in the last
    `hours` — by the Business Profile connection (free) or by the review
    fetch, which carries it (#24). The daily check then buys no own_rating
    call."""
    age = _stamp_age_hours(getattr(restaurant, "gbp_rating_updated_at", None))
    return age is not None and age < hours


def check_ratings(restaurant_id: int) -> dict:
    """{"ok", "checked", "moved": [why...], "reanalyse": bool, "triggers":
    [why...]} — re-read the stored competitors and, when nothing fresher is
    on file, the restaurant's own rating. `moved` names every change the
    morning found; `reanalyse` is True only for one in `triggers` (a
    closure, newer reviews, a review burst — #14). Never raises."""
    try:
        from models import get_restaurant, get_conn
        import models as _m
        r = get_restaurant(restaurant_id)
        raw = getattr(r, "competitor_intel", None) if r else None
        blob = json.loads(raw) if raw else None
        if not blob or not blob.get("competitors"):
            return {"ok": False, "reason": "no competitor read to refresh yet"}
        moved, triggers, checked = [], [], 0
        for c in blob["competitors"][:DAILY_CHECK_MAX]:
            pid = c.get("place_id")
            if not pid:
                continue
            try:
                # The same fields every competitor lookup asks (the Details
                # cache answers one from another), newest reviews first:
                # they ride on the Atmosphere SKU the rating already bills (#15).
                resp = _places_request("details", {"place_id": pid, "fields": COMPETITOR_FIELDS,
                                                   "reviews_sort": "newest", "key": PLACES_API_KEY},
                                       restaurant_id=restaurant_id, action="competitor_daily", timeout=8)
                data = resp.json()
            except PlacesUnavailable:
                break
            except Exception:
                continue
            if data.get("status") != "OK":
                continue
            checked += 1
            res = data.get("result") or {}
            new_r, new_n = res.get("rating"), int(res.get("user_ratings_total") or 0)
            old_r, old_n = c.get("rating"), int(c.get("review_count") or 0)
            if new_r is not None and old_r is not None and abs(float(new_r) - float(old_r)) >= RATING_MOVE - 1e-9:
                moved.append(f"{c.get('name')}: {old_r}★ → {new_r}★")
            if new_n - old_n >= REVIEW_BURST:
                why = f"{c.get('name')}: {new_n - old_n} new reviews"
                moved.append(why)
                triggers.append(why)
            status = res.get("business_status")
            if status and status != "OPERATIONAL" and not c.get("closed") \
                    and c.get("business_status") != status:
                why = f"{c.get('name')}: {status.replace('_', ' ').lower()}"
                moved.append(why)
                triggers.append(why)
            if status:
                # Kept whatever it says: the closure check reuses it (#41).
                c["business_status"] = status
            if "reviews" in res:
                latest = _reviews_from(res)
                newer = _newer_reviews(latest, c.get("reviews"))
                if newer:
                    why = f"{c.get('name')}: {newer} review{'s' if newer != 1 else ''} since the last read"
                    moved.append(why)
                    triggers.append(why)
                c["latest_reviews"], c["latest_reviews_at"] = latest, _now_stamp()
            if new_r is not None:
                c["rating"] = new_r
            c["review_count"] = new_n
            c["rating_is_provisional"] = new_n < MIN_REVIEWS_FOR_A_MEANINGFUL_RATING
        # The restaurant's own rating, from the same morning — unless the
        # Business Profile connection or the review fetch read it in the
        # last day (#24).
        pid = getattr(r, "google_place_id", None)
        if pid and not _own_rating_fresh(r):
            try:
                resp = _places_request("details", {"place_id": pid, "fields": "rating,user_ratings_total,types,price_level",
                                                   "key": PLACES_API_KEY},
                                       restaurant_id=restaurant_id, action="own_rating", timeout=8)
                data = resp.json()
                if data.get("status") == "OK":
                    res = data.get("result") or {}
                    _remember_own_listing(pid, res.get("types"), res.get("price_level"), res.get("rating"),
                                          res.get("user_ratings_total"))
            except Exception:
                pass
        from time_utils import restaurant_now
        blob["ratings_checked_at"] = restaurant_now(r, naive=True).strftime("%Y-%m-%d")
        conn = get_conn()
        try:
            conn.execute("UPDATE restaurants SET competitor_intel=? WHERE id=?", (json.dumps(blob), restaurant_id))
            conn.commit()
        finally:
            conn.close()
        _m._invalidate_request_cache(restaurant_id)
        try:
            import event_memory
            event_memory.record_market_snapshot(restaurant_id, blob["competitors"],
                                                at=restaurant_now(r, naive=True))
        except Exception as e:
            print(f"[competitor] market history not kept on the daily check: {e}")
        return {"ok": True, "checked": checked, "moved": moved, "triggers": triggers,
                "reanalyse": bool(triggers)}
    except Exception as e:
        print(f"[competitor] daily ratings check failed for {restaurant_id}: {e}")
        return {"ok": False, "reason": "the ratings check failed"}


# ── the owner's Refresh, when the read is already fresh (#42) ────────────────

def fresh_stored_read(restaurant_id, hours=REFRESH_FRESH_HOURS):
    """The stored read, as a refresh job's result, when it is under `hours`
    old and was made with the owner-added competitors the restaurant has now
    — an owner's Refresh then returns it instead of buying a new nearby
    search, Details calls and a Claude read (AI cost audit 10/7/26 #42).
    The payload is the run's own shape ({"ok": True, competitors, insight,
    generated_at, ...}) plus `fresh: True`, `updated_at` and `note` ("Already
    up to date — refreshed 2h ago"). None when a new run is due. Never raises."""
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        if not r or not r.google_place_id:
            return None
        blob = _stored_blob(r)
        if not blob.get("competitors") or not str(blob.get("insight") or "").strip():
            return None
        age = _stamp_age_hours(getattr(r, "competitor_updated_at", None))
        if age is None or age >= hours:
            return None
        if _custom_signature(blob) != set(_custom_ids(r)[:CUSTOM_COMPETITORS_MAX]):
            return None
        hrs = int(age)
        when = "under an hour ago" if hrs < 1 else f"{hrs}h ago"
        out = {k: v for k, v in blob.items() if k not in ("insight_raw", "validation_input")}
        out.update(ok=True, fresh=True, updated_at=r.competitor_updated_at,
                   note=f"Already up to date — refreshed {when}")
        return out
    except Exception as e:
        print(f"[competitor] stored read not checked for {restaurant_id}: {e}")
        return None
