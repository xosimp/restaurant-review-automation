"""
gmb.py — Google My Business (Business Profile API) OAuth + review reply posting
"""
import os, json, requests
from datetime import datetime, timezone, timedelta

GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI         = os.getenv("GMB_REDIRECT_URI", "https://dashboard.cavnar.ai/auth/google/callback")

SCOPES = "https://www.googleapis.com/auth/business.manage"

# ── OAuth helpers ─────────────────────────────────────────────────────────────

def get_auth_url(restaurant_id: int) -> str:
    """Build Google OAuth URL. restaurant_id passed as state."""
    return (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={SCOPES}"
        f"&state={restaurant_id}"
        f"&access_type=offline"
        f"&prompt=consent"
    )


def exchange_code(code: str) -> dict:
    """Exchange auth code for access + refresh tokens."""
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "code":          code,
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri":  REDIRECT_URI,
        "grant_type":    "authorization_code",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    """Get a new access token using the refresh token."""
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "refresh_token": refresh_token,
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "grant_type":    "refresh_token",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_valid_token(restaurant_id: int) -> str | None:
    """
    Return a valid access token for the restaurant, refreshing if needed.
    Returns None if not connected.
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
        return access_token
    except Exception as e:
        print(f"[GMB] Token refresh failed for restaurant {restaurant_id}: {e}")
        return None


# ── Account/Location discovery ────────────────────────────────────────────────

def get_gmb_account_id(access_token: str) -> str | None:
    """Get the first GBP account ID using the current Account Management API."""
    try:
        resp = requests.get(
            "https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        accounts = resp.json().get("accounts", [])
        if accounts:
            return accounts[0]["name"]  # e.g. "accounts/123456"
        return None
    except Exception as e:
        print(f"[GMB] get_gmb_account_id error: {e}")
        return None


def get_gmb_location_id(access_token: str, account_id: str, place_id: str) -> str | None:
    """
    Find the GBP location using the current Business Information API.
    Returns the location name e.g. "locations/456".
    """
    try:
        resp = requests.get(
            f"https://mybusinessbusinessinformation.googleapis.com/v1/{account_id}/locations",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"readMask": "name,title,phoneNumbers,websiteUri,profile"},
            timeout=10,
        )
        resp.raise_for_status()
        locations = resp.json().get("locations", [])
        if locations:
            return locations[0]["name"]  # e.g. "locations/456"
        return None
    except Exception as e:
        print(f"[GMB] get_gmb_location_id error: {e}")
        return None


# ── Review fetching via Business Profile API ─────────────────────────────────

def fetch_reviews_via_gmb(access_token: str, location_id: str, restaurant_id: int) -> list:
    """
    Fetch reviews using the current Business Profile Reviews API.
    location_id format: "locations/456"
    """
    from models import Review
    try:
        resp = requests.get(
            f"https://mybusinessreviews.googleapis.com/v1/{location_id}/reviews",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"pageSize": 20},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json().get("reviews", [])
        reviews = []
        for r in raw:
            star_map = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
            rating = star_map.get(r.get("starRating", "THREE"), 3)
            reviewer = r.get("reviewer", {})
            author = reviewer.get("displayName", "Anonymous")
            text = r.get("comment", "")
            update_time = r.get("updateTime", "")
            review_name = r.get("name", "")  # e.g. accounts/123/locations/456/reviews/789

            reviews.append(Review(
                restaurant_id=restaurant_id,
                platform="google",
                external_id=review_name or f"google_{update_time}_{author}",
                author=author,
                rating=rating,
                text=text,
                review_date=update_time,
                review_name=review_name,
            ))
        return reviews
    except Exception as e:
        print(f"[GMB] fetch_reviews_via_gmb error: {e}")
        return []


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
        else:
            return {"ok": False, "error": f"API error {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
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
            update_fields = {"gbp_rating": float(rating)}
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
    Returns dict with primaryPhone, websiteUri, description, title.
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
            params={"readMask": "name,title,phoneNumbers,websiteUri,profile"},
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
