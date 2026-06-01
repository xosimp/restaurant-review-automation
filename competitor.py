"""
competitor.py — Competitor intelligence for Cavnar AI
Pulls nearby restaurant reviews via Google Places API and generates AI insights.
"""
import os, json, requests, anthropic

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
        })
        data = r.json()
        if data.get("status") != "OK":
            return ""
        result = data.get("result", {})

        parts = []

        # Editorial summary (Google's own description)
        summary = result.get("editorial_summary", {}).get("overview", "")
        if summary:
            parts.append(f"Google description: {summary}")

        # Meal services
        meal_flags = []
        if result.get("serves_breakfast"): meal_flags.append("breakfast")
        if result.get("serves_brunch"): meal_flags.append("brunch")
        if result.get("serves_lunch"): meal_flags.append("lunch")
        if result.get("serves_dinner"): meal_flags.append("dinner")
        if meal_flags:
            parts.append(f"Serves: {', '.join(meal_flags)}")

        # Drinks
        drinks = []
        if result.get("serves_beer"): drinks.append("beer")
        if result.get("serves_wine"): drinks.append("wine")
        if result.get("serves_cocktails"): drinks.append("cocktails")
        if drinks:
            parts.append(f"Drinks: {', '.join(drinks)}")

        if result.get("serves_vegetarian_food"):
            parts.append("Vegetarian options available")

        # Cuisine types
        generic = {"restaurant","food","point_of_interest","establishment","bar","cafe"}
        types = [t.replace("_restaurant","").replace("_"," ") 
                 for t in result.get("types", []) if t not in generic]
        if types:
            parts.append(f"Cuisine type: {', '.join(types[:3])}")

        # Menu URL — try to parse it for actual menu items
        menu_url = result.get("menu_url", "") or result.get("website", "")
        if menu_url:
            parts.append(f"Menu URL: {menu_url}")
            try:
                menu_items = fetch_menu_from_url(menu_url)
                if menu_items:
                    parts.append(f"Menu items (auto-extracted):\n{menu_items}")
            except Exception:
                pass

        result = "\n".join(parts) if parts else ""

        # If Places returned nothing useful, try Yelp ID or web search as fallback
        if not result or len(result) < 50:
            try:
                from models import get_conn as _gc_m, get_restaurant
                _conn = _gc_m()
                row = _conn.execute(
                    "SELECT id, name, yelp_business_id FROM restaurants WHERE google_place_id=? LIMIT 1",
                    (google_place_id,)
                ).fetchone()
                _conn.close()
                if row:
                    yelp_id = row["yelp_business_id"] if "yelp_business_id" in row.keys() else None
                    rname = row["name"] if "name" in row.keys() else ""
                    if yelp_id:
                        yelp_result = fetch_menu_from_yelp_id(yelp_id)
                        if yelp_result:
                            result = (result + "\n" + yelp_result).strip()
                    if (not result or len(result) < 50) and rname:
                        web_result = search_and_fetch_menu(rname)
                        if web_result:
                            result = (result + "\n" + web_result).strip()
            except Exception as fe:
                print(f"[fetch_menu_notes fallback] {fe}")

        return result
    except Exception as e:
        print(f"[fetch_menu_notes] error: {e}")
        return ""


def fetch_menu_from_yelp_id(yelp_business_id: str) -> str:
    """Fetch menu info from Yelp business page — static HTML, reliable."""
    if not yelp_business_id:
        return ""
    try:
        import requests as _req
        url = f"https://www.yelp.com/biz/{yelp_business_id}"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        r = _req.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return ""
        text = r.text[:12000]
        # Extract menu section and review mentions of dishes
        import re
        # Pull text between menu-related sections
        menu_matches = re.findall(r'menu[^<]{0,200}', text, re.IGNORECASE)
        dish_matches = re.findall('"([A-Z][a-zA-Z &]{3,40})"', text)
        # Filter to likely dish names
        dish_names = [d for d in dish_matches if 2 < len(d.split()) < 8
                      and not any(skip in d.lower() for skip in ['photo','review','rating','yelp','write','click','more','open','close'])]
        dish_names = list(dict.fromkeys(dish_names))[:20]  # dedup, limit
        if dish_names:
            import anthropic, os
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""))
            msg = client.messages.create(
                model=os.getenv("CLAUDE_MODEL","claude-haiku-4-5-20251001"),
                max_tokens=300,
                messages=[{"role":"user","content":
                    f"From these potential dish names extracted from a restaurant Yelp page, identify which ones are actual menu items and organize them into a brief menu summary. Names: {', '.join(dish_names)}. Return: Signature dishes: [list]. Appetizers: [list]. Mains: [list]. Desserts: [list if any]. Keep it concise."}]
            )
            return msg.content[0].text.strip()
    except Exception as e:
        print(f"[fetch_menu_from_yelp_id] error: {e}")
    return ""


def search_and_fetch_menu(restaurant_name: str, city: str = "") -> str:
    """Search for a restaurant menu using web search then fetch the best static result."""
    try:
        import requests as _req, anthropic, os, re
        # Search for menu on static-HTML-friendly sites
        search_query = f"{restaurant_name} {city} menu site:sirved.com OR site:allmenus.com OR site:menupix.com OR site:zmenu.com"
        # Use DuckDuckGo instant answer API (no key needed)
        r = _req.get(
            "https://html.duckduckgo.com/html/",
            params={"q": f"{restaurant_name} {city} dinner menu"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8
        )
        # Extract first few result URLs
        urls = re.findall(r'class="result__url"[^>]*>([^<]+)', r.text)[:3]
        for url in urls:
            url = url.strip()
            if not url.startswith("http"):
                url = "https://" + url
            try:
                page = _req.get(url, headers={"User-Agent":"Mozilla/5.0"}, timeout=8)
                if page.status_code == 200 and len(page.text) > 1000:
                    result = _extract_menu_with_ai(page.text[:8000], restaurant_name)
                    if result:
                        return result
            except Exception:
                continue
    except Exception as e:
        print(f"[search_and_fetch_menu] error: {e}")
    return ""


def _extract_menu_with_ai(page_text: str, restaurant_name: str) -> str:
    """Use AI to extract menu items from page text."""
    try:
        import anthropic, os
        alpha_chars = sum(1 for c in page_text if c.isalpha())
        if alpha_chars < 300:
            return ""
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""))
        msg = client.messages.create(
            model=os.getenv("CLAUDE_MODEL","claude-haiku-4-5-20251001"),
            max_tokens=350,
            messages=[{"role":"user","content":
                f"Extract menu items for {restaurant_name} from this page. "
                "Return: Signature dishes: [list]. Appetizers: [list]. Mains: [list]. Desserts: [list]. Drinks: [list]. "
                "Only real menu items, no prices or HTML. If no menu items found, say NO_MENU_FOUND.\n\n" + page_text}]
        )
        result = msg.content[0].text.strip()
        return "" if "NO_MENU_FOUND" in result or len(result) < 30 else result
    except Exception:
        return ""


def fetch_menu_from_pdf_bytes(pdf_bytes: bytes, restaurant_name: str = "") -> str:
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
        return _extract_menu_with_ai(text, restaurant_name)
    except Exception as e:
        print(f"[fetch_menu_from_pdf_bytes] error: {e}")
        return ""


def fetch_menu_from_url(menu_url: str) -> str:
    """Fetch a restaurant's menu page and use AI to extract key menu items."""
    if not menu_url:
        return ""
    try:
        import requests as _req
        import anthropic, os
        headers = {"User-Agent": "Mozilla/5.0 (compatible; CavnarAI/1.0)"}
        r = _req.get(menu_url, headers=headers, timeout=10)
        if r.status_code != 200:
            return ""
        # Truncate to first 8000 chars — enough to get menu items
        page_text = r.text[:8000]

        # Check if page has useful content or is just a JS shell
        alpha_chars = sum(1 for c in page_text if c.isalpha())
        script_count = page_text.count("<script")
        if script_count > 10 or alpha_chars < 500:
            return ""  # JS-rendered site, no readable content

        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        extract_prompt = (
            "Extract the key menu items from this restaurant page. "
            "Return a concise summary: Signature dishes: [list]. Appetizers: [list]. "
            "Mains: [list]. Desserts: [list]. Drinks: [list]. "
            "Only include actual menu items. Skip prices and HTML. Max 300 words. "
            "If no menu items found, respond with exactly: NO_MENU_FOUND\n\nPage content:\n" + page_text
        )
        msg = client.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=400,
            messages=[{"role": "user", "content": extract_prompt}]
        )
        result = msg.content[0].text.strip()
        if "NO_MENU_FOUND" in result or len(result) < 30:
            return ""
        return result
    except Exception as e:
        print(f"[fetch_menu_from_url] error: {e}")
        return ""


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
        })
        data = r.json()
        if data.get("status") != "OK":
            return []
        loc = data["result"]["geometry"]["location"]
        lat, lng = loc["lat"], loc["lng"]
        own_name = data["result"].get("name", "")
        own_types = data["result"].get("types", [])
        own_price = data["result"].get("price_level")

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

        r2 = requests.get(nearby_url, params=params)
        places = r2.json().get("results", [])

        # If keyword search returns too few, fall back to broader search
        if len(places) < 3:
            params.pop("keyword", None)
            r2 = requests.get(nearby_url, params=params)
            places = r2.json().get("results", [])

        # Filter: skip self, skip fast food chains, prefer similar price level
        fast_food_chains = {"mcdonald", "burger king", "wendy", "taco bell", "subway",
                           "kfc", "domino", "pizza hut", "little caesar", "papa john",
                           "chipotle", "panera", "dunkin", "starbucks", "popeyes", "chick-fil-a"}

        competitors = []
        for p in places:
            name = p.get("name", "")
            if name == own_name:
                continue
            if p.get("business_status") != "OPERATIONAL":
                continue
            # Skip obvious fast food chains
            if any(chain in name.lower() for chain in fast_food_chains):
                continue
            # Prefer similar price level if known
            p_price = p.get("price_level")
            if own_price and p_price and abs(own_price - p_price) > 2:
                continue
            competitors.append({
                "place_id": p["place_id"],
                "name": name,
                "rating": p.get("rating", 0),
                "review_count": p.get("user_ratings_total", 0),
                "vicinity": p.get("vicinity", ""),
            })
            if len(competitors) >= max_results:
                break

        # If still too few after filtering, relax price filter
        if len(competitors) < 3:
            competitors = []
            for p in places:
                name = p.get("name", "")
                if name == own_name:
                    continue
                if p.get("business_status") != "OPERATIONAL":
                    continue
                if any(chain in name.lower() for chain in fast_food_chains):
                    continue
                competitors.append({
                    "place_id": p["place_id"],
                    "name": name,
                    "rating": p.get("rating", 0),
                    "review_count": p.get("user_ratings_total", 0),
                    "vicinity": p.get("vicinity", ""),
                })
                if len(competitors) >= max_results:
                    break

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
        })
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


def generate_competitor_insight(restaurant_name: str, competitors: list, owner_name: str = None, restaurant_profile: dict = None) -> str:
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

        prompt = f"""You are the Cavnar AI Consultant analyzing the competitive landscape for {restaurant_name}.

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
Write 2-3 short bullet points (starting with -) about what nearby competitors are genuinely excelling at based on their reviews. Be specific — name the restaurant and the specific strength.

WHAT COMPETITORS ARE DOING POORLY:
Write 2-3 short bullet points (starting with -) about real weaknesses or complaints in competitor reviews that {restaurant_name} could exploit. Be specific — name the restaurant and the specific complaint.

Recommendations:
1. [First concrete action {restaurant_name} can take this week based on the gaps above]
2. [Second specific differentiator to emphasize]
3. [Third tactical move to capture dissatisfied competitor customers]

Tone: sharp, direct, trusted business advisor. No generic advice. Name specific competitors and cite specific review themes."""

        msg = client.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
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
        if not competitors:
            return {"ok": False, "error": "No nearby competitors found"}

        # Enrich with reviews
        for c in competitors:
            c["reviews"] = get_competitor_reviews(c["place_id"])

        insight = generate_competitor_insight(
            restaurant.name, competitors,
            owner_name=restaurant.owner_name,
            restaurant_profile={
                "vibe": restaurant.vibe or "",
                "known_for": restaurant.known_for or "",
                "neighborhood": restaurant.neighborhood or "",
            }
        )

        # Store in DB
        result = {
            "competitors": competitors,
            "insight": insight,
            "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d"),
        }
        conn = get_conn()
        conn.execute(
            "UPDATE restaurants SET competitor_intel=?, competitor_updated_at=datetime('now') WHERE id=?",
            (json.dumps(result), restaurant_id)
        )
        conn.commit()
        conn.close()
        print(f"[Competitor] Analysis complete for {restaurant.name}")
        return {"ok": True, **result}
    except Exception as e:
        print(f"[Competitor] run_competitor_analysis error: {e}")
        return {"ok": False, "error": str(e)}
