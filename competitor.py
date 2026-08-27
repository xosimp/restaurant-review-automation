"""
competitor.py — Competitor intelligence for Cavnar AI
Pulls nearby restaurant reviews via Google Places API and generates AI insights.
"""
import os, json, requests, anthropic
from ai_utils import create_with_retry, extract_text

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
            "If no menu items found, respond with exactly: NO_MENU_FOUND\n\nMenu text:\n" + text
        )
        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=400,
            temperature=0.2,
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
            "If no menu items found, respond with exactly: NO_MENU_FOUND\n\nPage content:\n" + page_text
        )
        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=400,
            temperature=0.2,
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


def _is_pure_beverage_spot(types) -> bool:
    type_set = set(types or [])
    return bool(type_set & _PURE_BEVERAGE_TYPES) and not (type_set & _FOOD_SIGNAL_TYPES)


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
        fast_food_chains = {"mcdonald", "burger king", "wendy", "taco bell", "subway",
                           "kfc", "domino", "pizza hut", "little caesar", "papa john",
                           "chipotle", "panera", "dunkin", "starbucks", "popeyes", "chick-fil-a"}

        def _filter(candidates, enforce_price):
            out = []
            seen_ids = set()
            for p in candidates:
                name = p.get("name", "")
                pid = p.get("place_id")
                if not pid or pid in seen_ids or name == own_name:
                    continue
                if p.get("business_status") != "OPERATIONAL":
                    continue
                if any(chain in name.lower() for chain in fast_food_chains):
                    continue
                if _is_pure_beverage_spot(p.get("types", [])):
                    continue
                if enforce_price:
                    p_price = p.get("price_level")
                    if own_price and p_price and abs(own_price - p_price) > 2:
                        continue
                seen_ids.add(pid)
                out.append({
                    "place_id": pid,
                    "name": name,
                    "rating": p.get("rating", 0),
                    "review_count": p.get("user_ratings_total", 0),
                    "vicinity": p.get("vicinity", ""),
                })
                if len(out) >= max_results:
                    break
            return out

        competitors = _filter(places, enforce_price=True)

        # If still too few after filtering, relax the price-level match
        if len(competitors) < 3:
            competitors = _filter(places, enforce_price=False)

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
                for extra in _filter(more_places, enforce_price=False):
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


def generate_competitor_insight(restaurant_name: str, competitors: list, owner_name: str = None, restaurant_profile: dict = None, tz_name: str = None, restaurant_id: int = None) -> str:
    """Use Claude to generate a strategic competitor insight."""
    if not competitors or not ANTHROPIC_KEY:
        return ""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        comp_summary = ""
        for c in competitors:
            # Use up to 5 reviews, 250 chars each for richer insight
            rev_list = c.get("reviews", [])
            if rev_list:
                reviews_text = "\n  ".join([
                    f'[{r["rating"]}★] "{r["text"][:250].strip()}"'
                    for r in rev_list[:5]
                ])
            else:
                reviews_text = "No recent reviews"
            comp_summary += f"""
- {c["name"]} ({c["rating"]}★, {c["review_count"]} reviews)
  Recent customer reviews:
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

About {restaurant_name}:
{profile_context}

CRITICAL RULES:
- Only recommend actions that fit {restaurant_name}'s actual concept and cuisine
- NEVER recommend menu items or food categories outside their concept (e.g. don't suggest a burger promotion to a breakfast cafe)
- Focus on service quality, marketing angles, atmosphere, timing, and operational strengths
- Recommendations must be something {restaurant_name} can realistically act on given what they already are

Nearby competitors and their recent customer reviews:
{comp_summary}

Write a competitive intelligence report for {restaurant_name} in this EXACT format with these EXACT headers:

{greeting}, here is your competitive landscape snapshot.

WHAT COMPETITORS ARE DOING WELL:
Write 2-3 bullet points (starting with -). EACH BULLET IS ONE SENTENCE, 12 WORDS OR FEWER. Name the restaurant and the one specific strength — no parenthetical asides, no stacked examples, no explaining why it matters.

WHAT COMPETITORS ARE DOING POORLY:
Write 2-3 bullet points (starting with -). EACH BULLET IS ONE SENTENCE, 12 WORDS OR FEWER. Name the restaurant and the one specific complaint — no parenthetical asides, no stacked examples, no explaining why it matters.

PRICE POSITIONING:
One sentence, 15 words or fewer. Based on price-related language in competitor reviews ("overpriced", "great value", "worth it", "too expensive", "affordable"), state whether competitors read as overpriced or good value. Skip this section entirely if no price signals appear in the reviews.

Recommendations:
1. [One concrete action {restaurant_name} can take this week, 15 words or fewer]
2. [One specific differentiator to emphasize — service, atmosphere, value, or a strength competitors lack — 15 words or fewer]
3. [One tactical move to capture dissatisfied competitor customers this week, 15 words or fewer]

Tone: sharp, direct, trusted business advisor. Every line is a single punchy sentence, not a paragraph — cut qualifiers, cut context, cut anything that isn't the point itself. Name specific competitors and cite specific review themes anyway, just in fewer words. Always use $ signs before dollar amounts."""

        msg = create_with_retry(
            client,
            model=os.getenv("CLAUDE_REPORTER_MODEL", "claude-sonnet-5"),
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
            restaurant_id=restaurant_id,
            action="competitor_insight",
        )
        return extract_text(msg).strip()
    except Exception as e:
        print(f"[Competitor] generate_competitor_insight error: {e}")
        return ""


def run_competitor_analysis(restaurant_id: int) -> dict:
    """Full pipeline: fetch competitors, get reviews, generate insight."""
    try:
        from models import get_restaurant, get_conn, update_restaurant
        restaurant = get_restaurant(restaurant_id)
        if not restaurant or not restaurant.google_place_id:
            return {"ok": False, "error": "No Google Place ID set"}

        competitors = get_nearby_competitors(restaurant.google_place_id)

        # Add any manually specified competitor Place IDs
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
                            "fields": "name,rating,user_ratings_total,types,vicinity",
                            "key": PLACES_API_KEY,
                        }, timeout=8)
                        d = r.json().get("result", {})
                        if d.get("name"):
                            competitors.append({
                                "place_id": pid,
                                "name": d["name"],
                                "rating": d.get("rating", 0),
                                "review_count": d.get("user_ratings_total", 0),
                                "vicinity": d.get("vicinity", ""),
                                "types": d.get("types", []),
                                "custom": True,
                            })
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

        # Store in DB — stamped in the restaurant's local time so "generated
        # today" reads correctly on their dashboard
        from time_utils import restaurant_now
        _now_ct = restaurant_now(restaurant, naive=True)
        result = {
            "competitors": competitors,
            "insight": insight,
            "generated_at": _now_ct.strftime("%Y-%m-%d"),
        }
        conn = get_conn()
        conn.execute(
            "UPDATE restaurants SET competitor_intel=?, competitor_updated_at=? WHERE id=?",
            (json.dumps(result), _now_ct.strftime("%Y-%m-%d %H:%M:%S"), restaurant_id)
        )
        conn.commit()
        conn.close()
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
        return {"ok": False, "error": str(e)}
