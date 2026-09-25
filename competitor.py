"""
competitor.py — Competitor intelligence for Cavnar AI
Pulls nearby restaurant reviews via Google Places API and generates AI insights.
"""
import config
import os, json, requests
from ai_utils import create_with_retry, extract_text, get_client, model_for
from ai_guard import UNTRUSTED_NOTE, wrap_untrusted


from ai_utils import meter_places as _meter_places

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
        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        r = requests.get(details_url, params={
            "place_id": google_place_id,
            "fields": "name,types,price_level,editorial_summary,menu_url,website,serves_breakfast,serves_brunch,serves_lunch,serves_dinner,serves_beer,serves_wine,serves_cocktails,serves_vegetarian_food",
            "key": PLACES_API_KEY,
        }, timeout=8)
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
        msg = create_with_retry(
            client,
            model=model_for("competitor_extract"),
            max_tokens=400,
            messages=[{"role": "user", "content": extract_prompt}],
            restaurant_id=restaurant_id,
            action="menu_extract_pdf",
            # Rests on no data source: menu extraction from the supplied PDF.
            readiness=data_health.NOT_APPLICABLE,
        )
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
    try:
        from webhooks import _validate_webhook_url, InvalidWebhookURL
    except Exception:
        return False
    try:
        _validate_webhook_url(url)
        return True
    except InvalidWebhookURL:
        return False
    except Exception:
        return False


def _get_public(url, headers, timeout):
    """GET `url`, following at most _MENU_MAX_REDIRECTS redirects by hand and
    refusing any hop — the first included — that is not a public address.
    Returns the final response, or None when a hop was refused."""
    from urllib.parse import urljoin
    for _hop in range(_MENU_MAX_REDIRECTS + 1):
        if not _public_url(url):
            print(f"[fetch_menu_from_url] refused a non-public address: {url[:120]}")
            return None
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=False)
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
        msg = create_with_retry(
            client,
            model=model_for("competitor_extract"),
            max_tokens=400,
            messages=[{"role": "user", "content": extract_prompt}],
            restaurant_id=restaurant_id,
            action="menu_extract_url",
            # Rests on no data source: menu extraction from the supplied page.
            readiness=data_health.NOT_APPLICABLE,
        )
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
        r = requests.get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params=params, timeout=8,
        )
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


def get_nearby_competitors(google_place_id: str, radius_meters: int = 2000, max_results: int = 5,
                           usage: dict = None) -> list:
    """Find nearby restaurants using the Google Places API.

    `usage`, when given, is filled with the billed Places requests this made
    by kind ({"details": n, "nearby": n}). A run can make up to three nearby
    searches (keyword, broad, widened radius) and the caller metered one
    (MOD-INT-6), so two thirds of the spend never reached the budget."""
    if not PLACES_API_KEY or not google_place_id:
        return []
    if usage is None:
        usage = {}

    def _billed(kind):
        usage[kind] = usage.get(kind, 0) + 1
    try:
        # First get the restaurant's coordinates and types from its place ID
        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        r = requests.get(details_url, params={
            "place_id": google_place_id,
            "fields": "geometry,name,vicinity,types,price_level",
            "key": PLACES_API_KEY,
        }, timeout=8)
        _billed("details")
        data = r.json()
        from ai_utils import places_error as _pe, PlacesError as _PlacesError
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
        nearby_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        params = {
            "location": f"{lat},{lng}",
            "radius": radius_meters,
            "type": "restaurant",
            "key": PLACES_API_KEY,
            "rankby": "prominence",
        }
        if meal_keyword:
            params["keyword"] = meal_keyword

        r2 = requests.get(nearby_url, params=params, timeout=8)
        _billed("nearby")
        r2_data = r2.json()
        if _pe(r2_data):
            # A refused search is not an empty neighbourhood.
            raise _pe(r2_data)
        places = r2_data.get("results", [])

        # If keyword search returns too few, fall back to broader search
        if len(places) < 3:
            params.pop("keyword", None)
            r2 = requests.get(nearby_url, params=params, timeout=8)
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
                wider = requests.get(nearby_url, params={
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
        if isinstance(e, _PlacesError):
            raise
        print(f"[Competitor] get_nearby_competitors error: {e}")
        return []


def get_competitor_reviews(place_id: str, max_reviews: int = 5) -> list:
    """Get recent reviews for a competitor."""
    if not PLACES_API_KEY:
        return []
    try:
        url = "https://maps.googleapis.com/maps/api/place/details/json"
        r = requests.get(url, params={
            "place_id": place_id,
            "fields": "name,rating,reviews",
            "key": PLACES_API_KEY,
        }, timeout=8)
        data = r.json()
        if data.get("status") != "OK":
            return []
        reviews = data["result"].get("reviews", [])[:max_reviews]  # Google Places API returns max 5
        return [{
            "author": rev.get("author_name", "Guest"),
            "rating": rev.get("rating", 3),
            "text": rev.get("text", ""),
            "time": rev.get("relative_time_description", ""),
        } for rev in reviews]
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

        from competitor_intel_format import NOTHING_TO_ACT_ON
        prompt = f"""You are the Cavnar AI Consultant analyzing the competitive landscape for {restaurant_name}.
Today's date: {today_comp}{holiday_rec_context}{weather_ctx}

{UNTRUSTED_NOTE}

About {restaurant_name}:
{profile_context}

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

        msg = create_with_retry(
            client,
            model=model_for("competitor_insight"),
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
            restaurant_id=restaurant_id,
            action="competitor_insight",
            readiness=_ready_ci,
        )
        if getattr(msg, "stop_reason", None) == "max_tokens":
            raise ValueError("competitor insight was truncated")
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
    text = _validate_recommendation_citations(raw or "", competitors)
    # Strengths and weaknesses are cite-checked the same way (H11).
    text = _validate_bullets(text, competitors)
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


def intel_blob(competitors, insight, generated_at, closed_custom=None) -> dict:
    """The restaurants.competitor_intel blob: the read, and beside it the
    verdict it was shown under (`validation`, carrying the engine's
    version), so a later engine version re-validates the stored read on the
    next open (current_intel) instead of serving an old verdict. The model's
    raw text is deliberately NOT stored here: the whole blob is handed to
    the web (/api/competitor-intel), and the raw text still holds what the
    engine took out (another tenant's name, a dropped sentence)."""
    blob = {"competitors": competitors, "insight": str(insight or ""), "generated_at": generated_at,
            "closed_custom": closed_custom or []}
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


def _validate_bullets(text, competitors):
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
        try:
            import ops
            ops.capture(RuntimeError(f"competitor insight: {dropped} strength/weakness bullet(s) without a valid "
                                     f"citation to the named competitor dropped"),
                        job="competitor_insight", context="bullet citations")
        except Exception:
            pass
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


def _validate_recommendation_citations(text, competitors):
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
        try:
            import ops
            ops.capture(RuntimeError(f"competitor insight: {dropped} recommendation(s) without a valid review citation dropped"),
                        job="competitor_insight", context="citations")
        except Exception:
            pass
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
            return {k: v for k, v in c.items() if k != "reviews"}
    return None


def run_competitor_analysis(restaurant_id: int) -> dict:
    """Full pipeline: fetch competitors, get reviews, generate insight."""
    try:
        from models import get_restaurant, get_conn, update_restaurant
        restaurant = get_restaurant(restaurant_id)
        if not restaurant or not restaurant.google_place_id:
            return {"ok": False, "error": "No Google Place ID set"}

        from ai_utils import PlacesError as _PlacesError
        _usage = {}
        try:
            competitors = get_nearby_competitors(restaurant.google_place_id, usage=_usage)
        except _PlacesError as pe:
            _meter_places(restaurant_id, "competitor_intel", "nearby", status="error", error=str(pe)[:200])
            try:
                import ops
                ops.capture(pe, job="competitor_intel", context=f"restaurant_id={restaurant_id}")
            except Exception:
                pass
            return {"ok": False, "error": pe.owner_message, "places_status": pe.status}
        # One nearby search, then a details lookup per candidate it kept.
        # Google Places is billed per request and was invisible to the budget
        # entirely, which for a weekly job across every full-tier client is
        # real money no ceiling could see.
        # Every billed search the lookup made — own details, then one to
        # three nearby searches (MOD-INT-6) — then a details lookup per
        # candidate it kept.
        for _ in range(_usage.get("details", 0)):
            _meter_places(restaurant_id, "competitor_intel", "details")
        for _ in range(max(1, _usage.get("nearby", 0))):
            _meter_places(restaurant_id, "competitor_intel", "nearby")
        for _ in competitors or []:
            _meter_places(restaurant_id, "competitor_intel", "details")

        # Add any manually specified competitor Place IDs
        _closed_custom = []
        if restaurant.custom_competitors:
            custom_ids = [pid.strip() for pid in restaurant.custom_competitors.split(',') if pid.strip()]
            existing_ids = {c['place_id'] for c in competitors}
            for pid in custom_ids:
                if pid not in existing_ids:
                    try:
                        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
                        import requests as _req
                        r = _req.get(details_url, params={
                            "place_id": pid,
                            "fields": "name,rating,user_ratings_total,types,vicinity,"
                                      "business_status,price_level",
                            "key": PLACES_API_KEY,
                        }, timeout=8)
                        d = r.json().get("result", {})
                        # An owner-added competitor skipped every check the
                        # discovered ones run, including whether it is still
                        # trading. A restaurant that closed two years ago
                        # stayed in the comparison forever with its frozen
                        # rating, and the AI wrote strategy against it.
                        _status = d.get("business_status") or "OPERATIONAL"
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
                                # The same floor the discovered ones carry: a
                                # rating on too few reviews is provisional
                                # whoever added the place (B6 sub-audit).
                                "rating_is_provisional": int(d.get("user_ratings_total") or 0)
                                                         < MIN_REVIEWS_FOR_A_MEANINGFUL_RATING,
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

        # Enrich with reviews in parallel — 5 sequential calls → 1 parallel batch
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        with ThreadPoolExecutor(max_workers=5) as _pool:
            _futs = {_pool.submit(get_competitor_reviews, c["place_id"]): i for i, c in enumerate(competitors)}
            for _fut in _as_completed(_futs):
                competitors[_futs[_fut]]["reviews"] = _fut.result()

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
        result = intel_blob(competitors, insight, _now_ct.strftime("%Y-%m-%d"), _closed_custom)
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
        # One JSON blob overwritten every Monday was the entire record, so
        # nothing could show that a competitor's rating fell, that a new one
        # opened, or that a complaint theme appeared. A snapshot per run is
        # what makes any of that answerable later.
        try:
            from models import record_competitor_snapshot
            record_competitor_snapshot(restaurant_id, competitors)
        except Exception as _se:
            print(f"[Competitor] snapshot failed: {_se}")
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
