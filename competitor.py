"""
competitor.py — Competitor intelligence for Cavnar AI
Pulls nearby restaurant reviews via Google Places API and generates AI insights.
"""
import os, json, requests, anthropic

PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")


def get_nearby_competitors(google_place_id: str, radius_meters: int = 500, max_results: int = 5) -> list:
    """Find nearby restaurants using the Google Places API."""
    if not PLACES_API_KEY or not google_place_id:
        return []
    try:
        # First get the restaurant's coordinates from its place ID
        details_url = "https://maps.googleapis.com/maps/api/place/details/json"
        r = requests.get(details_url, params={
            "place_id": google_place_id,
            "fields": "geometry,name,vicinity",
            "key": PLACES_API_KEY,
        })
        data = r.json()
        if data.get("status") != "OK":
            return []
        loc = data["result"]["geometry"]["location"]
        lat, lng = loc["lat"], loc["lng"]
        own_name = data["result"].get("name", "")

        # Search for nearby restaurants
        nearby_url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        r2 = requests.get(nearby_url, params={
            "location": f"{lat},{lng}",
            "radius": radius_meters,
            "type": "restaurant",
            "key": PLACES_API_KEY,
        })
        places = r2.json().get("results", [])

        competitors = []
        for p in places:
            if p.get("name") == own_name:
                continue  # skip self
            if p.get("business_status") != "OPERATIONAL":
                continue
            competitors.append({
                "place_id": p["place_id"],
                "name": p["name"],
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
        reviews = data["result"].get("reviews", [])[:max_reviews]
        return [{
            "author": rev.get("author_name", "Guest"),
            "rating": rev.get("rating", 3),
            "text": rev.get("text", ""),
            "time": rev.get("relative_time_description", ""),
        } for rev in reviews]
    except Exception as e:
        print(f"[Competitor] get_competitor_reviews error: {e}")
        return []


def generate_competitor_insight(restaurant_name: str, competitors: list, owner_name: str = None) -> str:
    """Use Claude to generate a strategic competitor insight."""
    if not competitors or not ANTHROPIC_KEY:
        return ""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        comp_summary = ""
        for c in competitors:
            reviews_text = " | ".join([f'"{r["text"][:100]}"' for r in c.get("reviews", [])[:3]])
            comp_summary += f"""
- {c["name"]} ({c["rating"]}★, {c["review_count"]} reviews)
  Recent reviews: {reviews_text or "No recent reviews"}
"""

        greeting = f"Hi {owner_name}" if owner_name else "Hi"
        prompt = f"""You are the Cavnar AI Consultant analyzing the competitive landscape for {restaurant_name}.

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
            max_tokens=600,
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
            owner_name=restaurant.owner_name
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
