"""
competitor.py — Competitor intelligence for Cavnar AI
Pulls nearby restaurant reviews via Google Places API and generates AI insights.
"""
import os, json, requests, anthropic

PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")


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
