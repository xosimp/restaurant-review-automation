"""
competitor.py — Competitor intelligence for Cavnar AI
Pulls nearby restaurant reviews via Google Places API and generates AI insights.
"""
import os, json, requests, anthropic
from ai_utils import create_with_retry, extract_text
from ai_guard import UNTRUSTED_NOTE, wrap_untrusted


def _meter_places(restaurant_id, action, kind="details", status="ok", error=None):
    """Google Places is billed per request. Audit #7 found it outside the
    ledger and the budget entirely, so a Places-only restaurant's four daily
    review fetches and the weekly competitor run were real money that no
    ceiling could see. Best-effort: metering must never break a fetch."""
    try:
        from ai_utils import log_api_call
        log_api_call(restaurant_id, action, f"google-places-{kind}",
                     calls=1, status=status, error=error)
    except Exception:
        pass


PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")


def fetch_menu_notes_from_places(google_place_id: str) -> str:
    """Fetch menu URL, editorial summary, and cuisine info from Google Places API.
    Returns a string suitable for menu_notes field, or empty string if nothing useful found."""
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
                menu_items = fetch_menu_from_url(menu_url)
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
        import io, anthropic, os
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        text = text[:8000]
        if len(text) < 50:
            return ""
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
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
        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=400,
            messages=[{"role": "user", "content": extract_prompt}],
            restaurant_id=restaurant_id,
            action="menu_extract_pdf",
        )
        result = extract_text(msg).strip()
        return "" if "NO_MENU_FOUND" in result or len(result) < 30 else result
    except Exception as e:
        print(f"[fetch_menu_from_pdf_bytes] error: {e}")
        return ""


def fetch_menu_from_url(menu_url: str, restaurant_id: int = None) -> str:
    """Fetch a restaurant's menu page and use AI to extract key menu items."""
    if not menu_url:
        return ""
    try:
        import requests as _req
        import anthropic, os
        # Identify as a normal browser so servers don't reject a bare
        # "python-requests" client — this is a single honest identity, not
        # rotated or retried to work around a site's bot-blocking response.
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = _req.get(menu_url, headers=headers, timeout=12, allow_redirects=True)
        if r.status_code != 200 or len(r.text) <= 500:
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

        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        extract_prompt = (
            "Extract the key menu items from this restaurant page. "
            "Return a concise summary: Signature dishes: [list]. Appetizers: [list]. "
            "Mains: [list]. Desserts: [list]. Drinks: [list]. "
            "Only include actual menu items. Skip prices and HTML. Max 300 words. "
            "If no menu items found, respond with exactly: NO_MENU_FOUND\n\n"
            # Scraped from a URL — a page whose author is not our customer.
            + UNTRUSTED_NOTE + "\n\nPage content:\n" + wrap_untrusted(page_text)
        )
        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=400,
            messages=[{"role": "user", "content": extract_prompt}],
            restaurant_id=restaurant_id,
            action="menu_extract_url",
        )
        result = extract_text(msg).strip()
        if "NO_MENU_FOUND" in result or len(result) < 30:
            return ""
        return result
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
                "rating": item.get("rating", 0),
                "review_count": item.get("user_ratings_total", 0),
            }
            for item in data.get("results", [])[:max_results]
            if item.get("place_id") and item.get("name")
        ]
    except Exception as e:
        print(f"[Competitor] search_places_near error: {e}")
        return []


def get_nearby_competitors(google_place_id: str, radius_meters: int = 2000, max_results: int = 5) -> list:
    """Find nearby restaurants using the Google Places API."""
    if not PLACES_API_KEY or not google_place_id:
        return []
    try:
        # First get the restaurant's coordinates and types from its place ID
        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        r = requests.get(details_url, params={
            "place_id": google_place_id,
            "fields": "geometry,name,vicinity,types,price_level",
            "key": PLACES_API_KEY,
        }, timeout=8)
        data = r.json()
        if data.get("status") != "OK":
            return []
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
        r2_data = r2.json()
        if r2_data.get("status") not in ("OK", "ZERO_RESULTS"):
            print(f"[Competitor] Nearby search error: {r2_data.get('status')} {r2_data.get('error_message','')}")
        places = r2_data.get("results", [])

        # If keyword search returns too few, fall back to broader search
        if len(places) < 3:
            params.pop("keyword", None)
            r2 = requests.get(nearby_url, params=params, timeout=8)
            r2_data = r2.json()
            if r2_data.get("status") not in ("OK", "ZERO_RESULTS"):
                print(f"[Competitor] Fallback search error: {r2_data.get('status')} {r2_data.get('error_message','')}")
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
                if enforce_price:
                    p_price = p.get("price_level")
                    if own_price and p_price and abs(own_price - p_price) > 2:
                        continue
                seen_ids.add(pid)
                _loc = (p.get("geometry") or {}).get("location") or {}
                out.append({
                    "place_id": pid,
                    "name": name,
                    "rating": p.get("rating", 0),
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
                }, timeout=8).json()
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
}


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
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        _PRICE_WORDS = {1: "$ (inexpensive)", 2: "$$ (moderate)",
                        3: "$$$ (expensive)", 4: "$$$$ (very expensive)"}
        comp_summary = ""
        for c in competitors:
            # Use up to 5 reviews, 250 chars each for richer insight
            rev_list = c.get("reviews", [])
            if rev_list:
                # Competitor review text is written by the public. Fenced
                # below with the rest of the block — see UNTRUSTED_NOTE in
                # the prompt.
                #
                # Each review now carries its age. Google picks these five
                # by its own relevance ranking, not by recency, so without a
                # date a complaint from three years ago read as what a
                # competitor is doing wrong now — and that is what the
                # "DOING POORLY" section was built from.
                reviews_text = "\n  ".join([
                    f'[{r["rating"]}★, {r.get("time") or "date unknown"}] "{r["text"][:250].strip()}"'
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
            _how = c.get("match_basis")
            match_line = f"\n  How this one was selected: {_how}" if _how else ""
            _dist = c.get("distance_m")
            dist_line = f"\n  About {round(_dist/1000, 1)} km away" if _dist else ""
            comp_summary += f"""
- {c["name"]} ({c["rating"]}★, {c["review_count"]} reviews){price_line}{dist_line}{match_line}
  Recent customer reviews (with how long ago each was written):
  {reviews_text}
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

        prompt = f"""You are the Cavnar AI Consultant analyzing the competitive landscape for {restaurant_name}.
Today's date: {today_comp}{holiday_rec_context}

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
{wrap_untrusted(comp_summary)}

EVIDENCE RULES — these bound what you may claim:
- Each review carries how long ago it was written. A review over a year old is NOT evidence of what a competitor is doing now. Prefer recent ones, and if you cite an older one, say when it was ("last year", "two years ago").
- Every competitor strength or weakness you state must trace to a review quoted above for THAT named competitor. Never attribute a complaint to a restaurant it was not written about.
- Only name restaurants that appear in the list above. Do not introduce any other business.
- "How this one was selected" tells you how close a match each competitor is. One selected on a widened radius with no cuisine or price constraint is a weaker comparison — do not present it as a direct rival without saying so.
- State no figure — a dollar amount, a percentage, a count — that does not appear above.

Write a competitive intelligence report for {restaurant_name} in this EXACT format with these EXACT headers:

{greeting}, here is your competitive landscape snapshot.

WHAT COMPETITORS ARE DOING WELL:
Write 2-3 bullet points (starting with -). EACH BULLET IS ONE SENTENCE, 12 WORDS OR FEWER. Name the restaurant and the one specific strength — no parenthetical asides, no stacked examples, no explaining why it matters.

WHAT COMPETITORS ARE DOING POORLY:
Write 2-3 bullet points (starting with -). EACH BULLET IS ONE SENTENCE, 12 WORDS OR FEWER. Name the restaurant and the one specific complaint — no parenthetical asides, no stacked examples, no explaining why it matters.

PRICE POSITIONING:
One sentence, 15 words or fewer, based on the Google price levels listed above — a real field, not an impression. State where these competitors sit as a group. You may add whether review language agrees, but never state a positioning that the price levels alone do not support. Skip this section entirely if fewer than two competitors have a price level listed.

Recommendations:
1. [One operational or service fix using only what {restaurant_name} already has — a specific script, timing, or staffing change, 15 words or fewer]
2. [One specific EXISTING dish, deal, or strength to push harder in marketing/signage this week — never a new item, 15 words or fewer]
3. [One specific tactic to win a named competitor's dissatisfied customers, tied to an actual complaint quoted above, 15 words or fewer]

Tone: sharp, direct, trusted business advisor. Every line is a single punchy sentence, not a paragraph — cut qualifiers, cut context, cut anything that isn't the point itself. Name specific competitors and cite specific review themes anyway, just in fewer words. Always use $ signs before dollar amounts."""

        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_REPORTER_MODEL", "claude-sonnet-5"),
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
            restaurant_id=restaurant_id,
            action="competitor_insight",
        )
        if getattr(msg, "stop_reason", None) == "max_tokens":
            raise ValueError("competitor insight was truncated")
        text = extract_text(msg).strip()

        # Every other AI insight in this codebase runs through this guard —
        # labor, review, marketing, inventory, email. Competitor intel, the
        # one whose prompt says "Always use $ signs before dollar amounts",
        # did not. A figure the model states that was never in its input is
        # exactly what an owner would act on.
        from ai_guard import verify_figures
        unsupported = verify_figures(text, prompt, "competitor_insight", restaurant_id)

        # And a named restaurant that was never in the competitor list is an
        # invented competitor, which is the single worst thing this module
        # can produce.
        invented = _invented_competitors(text, competitors)

        if unsupported or invented:
            notes = []
            if invented:
                notes.append("names a business that is not in your competitor list: "
                             + ", ".join(invented[:3]))
            if unsupported:
                notes.append("states figures that were not in the data: "
                             + ", ".join(str(u) for u in unsupported[:3]))
            text = text.rstrip() + "\n\nUNVERIFIED: " + "; ".join(notes) + "."
        return text
    except Exception as e:
        print(f"[Competitor] generate_competitor_insight error: {e}")
        try:
            import ops
            ops.capture(e, job="competitor_insight", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return ""


def run_competitor_analysis(restaurant_id: int) -> dict:
    """Full pipeline: fetch competitors, get reviews, generate insight."""
    try:
        from models import get_restaurant, get_conn, update_restaurant
        restaurant = get_restaurant(restaurant_id)
        if not restaurant or not restaurant.google_place_id:
            return {"ok": False, "error": "No Google Place ID set"}

        competitors = get_nearby_competitors(restaurant.google_place_id)
        # One nearby search, then a details lookup per candidate it kept.
        # Google Places is billed per request and was invisible to the budget
        # entirely, which for a weekly job across every full-tier client is
        # real money no ceiling could see.
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
                                "rating": d.get("rating", 0),
                                "review_count": d.get("user_ratings_total", 0),
                                "vicinity": d.get("vicinity", ""),
                                "types": d.get("types", []),
                                "price_level": d.get("price_level"),
                                "match_basis": "added by you",
                                "custom": True,
                            })
                        elif d.get("name"):
                            print(f"[Competitor] custom competitor {d['name']} is {_status} — skipped")
                            _closed_custom.append({"place_id": pid, "name": d["name"],
                                                   "status": _status})
                    except Exception as ce:
                        print(f"[Competitor] Could not fetch custom competitor {pid}: {ce}")

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
        result = {
            "competitors": competitors,
            "insight": insight,
            "generated_at": _now_ct.strftime("%Y-%m-%d"),
            "closed_custom": _closed_custom,
        }
        conn = get_conn()
        conn.execute(
            "UPDATE restaurants SET competitor_intel=?, competitor_updated_at=? WHERE id=?",
            (json.dumps(result), _now_ct.strftime("%Y-%m-%d %H:%M:%S"), restaurant_id)
        )
        conn.commit()
        conn.close()
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
        return {"ok": True, **result}
    except Exception as e:
        print(f"[Competitor] run_competitor_analysis error: {e}")
        from ai_guard import safe_error
        return {"ok": False, "error": safe_error(e, "Competitor analysis could not be completed.")}
