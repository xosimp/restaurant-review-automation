"""
social_routes.py — Instagram and Facebook OAuth + posting routes
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import Blueprint, request, jsonify, redirect, make_response
import os

from models import get_conn, get_restaurant, update_restaurant
from auth import login_required, admin_required

social_bp = Blueprint('social', __name__)

@social_bp.route("/instagram/connect")
@login_required
def instagram_connect(current_user):
    """Open Meta OAuth in a popup — state carries restaurant_id."""
    import urllib.parse
    from flask import redirect as flask_redirect
    app_id       = os.getenv("META_APP_ID","")
    redirect_uri = os.getenv("META_REDIRECT_URI", "https://dashboard.cavnar.ai/instagram/callback")
    scope        = "instagram_basic,instagram_content_publish,instagram_manage_insights,pages_read_engagement,pages_show_list,business_management"
    state        = str(current_user["restaurant_id"])
    params = urllib.parse.urlencode({
        "client_id":     app_id,
        "redirect_uri":  redirect_uri,
        "scope":         scope,
        "auth_type":     "rerequest",
        "response_type": "code",
        "state":         state,
    })
    return flask_redirect(f"https://www.facebook.com/v19.0/dialog/oauth?{params}")

@social_bp.route("/instagram/callback")
def instagram_callback():
    """Handle Meta OAuth callback — exchange code for token, get IG user ID."""
    import requests as _req
    from flask import redirect as _ig_redirect
    from models import update_restaurant as _update_r

    code         = request.args.get("code")
    state        = request.args.get("state")
    app_id       = os.getenv("META_APP_ID","")
    app_secret   = os.getenv("META_APP_SECRET","")
    redirect_uri = os.getenv("META_REDIRECT_URI", "https://dashboard.cavnar.ai/instagram/callback")

    if not code:
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({ig:'error',msg:'no_code'},'*');"
            "window.close();"
            "</script><p>Connection failed.</p></body></html>"
        )

    # Exchange code for short-lived token
    r = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": redirect_uri, "code": code,
    })
    if r.status_code != 200:
        print(f"IG token exchange failed: {r.text}")
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({ig:'error',msg:'token_failed'},'*');"
            "window.close();"
            "</script><p>Token exchange failed.</p></body></html>"
        )
    short_token = r.json().get("access_token")

    # Exchange for long-lived token (60 days)
    r2 = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_token,
    })
    long_token = r2.json().get("access_token", short_token)

    # Get Facebook pages
    r3 = _req.get("https://graph.facebook.com/v19.0/me/accounts", params={"access_token": long_token})
    pages = r3.json().get("data", [])
    ig_user_id = None
    page_token = long_token
    matched_page = None

    for page in pages:
        r4 = _req.get(f"https://graph.facebook.com/v19.0/{page['id']}", params={
            "fields": "instagram_business_account",
            "access_token": page.get("access_token", long_token),
        })
        ig_data = r4.json().get("instagram_business_account")
        if ig_data:
            ig_user_id = ig_data.get("id")
            page_token = page.get("access_token", long_token)
            matched_page = page
            break

    if not ig_user_id:
        print(f"No IG account found. Pages: {r3.json()}")
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({ig:'error',msg:'no_ig_account'},'*');"
            "window.close();"
            "</script><p>No Instagram business account found.</p></body></html>"
        )

    rid = int(state) if state and state.isdigit() else None
    if rid:
        from datetime import datetime, timedelta
        expires = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
        update_data = {
            "ig_token": page_token,
            "ig_user_id": ig_user_id,
            "ig_token_expires": expires,
        }
        # Save Facebook page token/id from the matched page
        if matched_page:
            update_data["fb_page_token"]    = matched_page.get("access_token", long_token)
            update_data["fb_page_id"]       = matched_page.get("id", "")
            update_data["fb_token_expires"] = expires
        elif pages:
            update_data["fb_page_token"]    = pages[0].get("access_token", long_token)
            update_data["fb_page_id"]       = pages[0].get("id", "")
            update_data["fb_token_expires"] = expires
        _update_r(rid, update_data)
        print(f"Instagram+Facebook connected for restaurant {rid}, expires {expires}")

    return (
        "<html><body><script>"
        "window.opener&&window.opener.postMessage({ig:'connected'},'*');"
        "window.close();"
        "</script><p>Instagram connected! Close this window.</p></body></html>"
    )

@social_bp.route("/api/post-to-instagram", methods=["POST"])
@login_required
def post_to_instagram(current_user):
    """Post a caption to Instagram. Client must have connected their account."""
    import requests as _req
    data       = request.get_json()
    caption    = data.get("caption","").strip()
    image_url  = data.get("image_url","").strip()  # optional

    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.ig_token or not restaurant.ig_user_id:
        return jsonify(ok=False, error="Instagram not connected — click Connect Instagram first")

    ig_user_id = restaurant.ig_user_id
    token      = restaurant.ig_token

    if not image_url:
        return jsonify(ok=False, error="Instagram requires an image. Paste a public image URL into the Image URL field before posting.")

    r1 = _req.post(f"https://graph.facebook.com/v19.0/{ig_user_id}/media", data={
        "image_url":    image_url,
        "caption":      caption,
        "access_token": token,
    })

    if r1.status_code != 200:
        err = r1.json().get("error",{}).get("message","Unknown error")
        print(f"IG media create failed: {r1.text}")
        return jsonify(ok=False, error=err)

    creation_id = r1.json().get("id")

    # Wait for Instagram to process the image before publishing
    import time as _time
    _time.sleep(4)

    # Publish the media
    r2 = _req.post(f"https://graph.facebook.com/v19.0/{ig_user_id}/media_publish", data={
        "creation_id":  creation_id,
        "access_token": token,
    })

    if r2.status_code != 200:
        err = r2.json().get("error",{}).get("message","Publish failed")
        return jsonify(ok=False, error=err)

    post_id = r2.json().get("id")
    # Save post_id for engagement tracking
    try:
        from marketing import log_content as _lc
        _data = request.get_json() or {}
        _topic = _data.get("topic", "")
        if _topic and post_id:
            _lc(current_user["restaurant_id"], "instagram_post", _topic,
                post_id=post_id, post_platform="instagram")
    except Exception as _e:
        print(f"[insights] failed to log post_id: {_e}")
    return jsonify(ok=True, post_id=post_id)

@social_bp.route("/api/instagram-status")
@login_required
def instagram_status(current_user):
    """Check if Instagram is connected for this restaurant."""
    restaurant = get_restaurant(current_user["restaurant_id"])
    connected    = bool(restaurant and restaurant.ig_token and restaurant.ig_user_id)
    fb_connected = bool(restaurant and restaurant.fb_page_token and restaurant.fb_page_id)
    return jsonify(connected=connected, fb_connected=fb_connected)

@social_bp.route("/api/instagram-disconnect", methods=["POST"])
@login_required
def instagram_disconnect(current_user):
    """Disconnect Instagram from this restaurant."""
    from models import update_restaurant
    update_restaurant(current_user["restaurant_id"], {"ig_token": "", "ig_user_id": "", "fb_page_token": "", "fb_page_id": ""})
    return jsonify(ok=True)

@social_bp.route("/api/debug-insights")
@login_required
def debug_insights(current_user):
    import requests as _req
    from models import get_restaurant
    restaurant = get_restaurant(current_user["restaurant_id"])
    post_id = "1206793632506765_122108397639307271"
    results = {}
    # Try FB page token
    r1 = _req.get(f"https://graph.facebook.com/v19.0/{post_id}",
        params={"fields":"id,message,likes.summary(true),comments.summary(true),shares","access_token": restaurant.fb_page_token}, timeout=5)
    results["post_with_likes"] = r1.json()
    results["fb_page_id"] = restaurant.fb_page_id
    results["fb_token_present"] = bool(restaurant.fb_page_token)
    return __import__('flask').jsonify(results)

@social_bp.route("/api/post-insights")
@login_required
def post_insights(current_user):
    """Fetch engagement metrics for posted content."""
    import requests as _req
    from models import get_conn, get_restaurant
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or (not restaurant.ig_token and not restaurant.fb_page_token):
        return jsonify(ok=False, error="Not connected")
    try:
        conn = get_conn()
        # Add metrics columns if they don't exist yet
        for col in ("reach", "impressions", "engaged", "likes", "comments", "shares"):
            try:
                conn.execute(f"ALTER TABLE marketing_content_log ADD COLUMN {col} INTEGER DEFAULT 0")
            except Exception:
                pass
        conn.commit()
        rows = conn.execute(
            """SELECT id, topic, post_id, post_platform, created_at,
                      reach, impressions, engaged, likes, comments, shares
               FROM marketing_content_log
               WHERE restaurant_id=? AND post_id IS NOT NULL
               ORDER BY created_at DESC LIMIT 10""",
            (current_user["restaurant_id"],)
        ).fetchall()
        results = []
        for row in rows:
            if not row["post_id"]:
                continue
            try:
                _token = restaurant.fb_page_token if row["post_platform"] == "facebook" else restaurant.ig_token
                if not _token:
                    results.append({"topic": row["topic"], "post_id": row["post_id"],
                                    "platform": row["post_platform"], "metrics": {}})
                    continue
                metrics = {}
                if row["post_platform"] == "facebook":
                    # Basic engagement via post fields
                    r = _req.get(
                        "https://graph.facebook.com/v19.0/" + row["post_id"],
                        params={"fields": "reactions.summary(true),comments.summary(true),shares",
                                "access_token": _token},
                        timeout=5
                    )
                    print(f"[insights] FB engagement status={r.status_code} body={r.text[:300]}")
                    if r.status_code == 200:
                        d = r.json()
                        metrics["likes"]    = d.get("reactions", {}).get("summary", {}).get("total_count", 0)
                        metrics["comments"] = d.get("comments", {}).get("summary", {}).get("total_count", 0)
                        metrics["shares"]   = d.get("shares", {}).get("count", 0)
                    # Reach + impressions via Page Insights API
                    r2 = _req.get(
                        "https://graph.facebook.com/v19.0/" + row["post_id"] + "/insights",
                        params={"metric": "post_impressions,post_impressions_unique",
                                "period": "lifetime",
                                "access_token": _token},
                        timeout=5
                    )
                    print(f"[insights] FB insights status={r2.status_code} body={r2.text[:300]}")
                    if r2.status_code == 200:
                        for m in r2.json().get("data", []):
                            val = m.get("values", [{}])[-1].get("value", 0) if m.get("values") else m.get("value", 0)
                            if m["name"] == "post_impressions":
                                metrics["impressions"] = val
                            elif m["name"] == "post_impressions_unique":
                                metrics["reach"] = val
                else:
                    r = _req.get(
                        "https://graph.facebook.com/v19.0/" + row["post_id"] + "/insights",
                        params={"metric": "reach,impressions,likes,comments_count,saved",
                                "access_token": _token},
                        timeout=5
                    )
                    if r.status_code == 200:
                        for m in r.json().get("data", []):
                            metrics[m["name"]] = m.get("values", [{}])[-1].get("value", 0)
                # Write metrics back to DB for AI trend analysis
                if metrics:
                    conn.execute(
                        """UPDATE marketing_content_log
                           SET reach=?, impressions=?, engaged=?, likes=?, comments=?, shares=?
                           WHERE id=?""",
                        (metrics.get("reach", 0), metrics.get("impressions", 0),
                         metrics.get("engaged", 0), metrics.get("likes", 0),
                         metrics.get("comments", 0), metrics.get("shares", 0),
                         row["id"])
                    )
                    conn.commit()
                results.append({
                    "topic":    row["topic"],
                    "post_id":  row["post_id"],
                    "platform": row["post_platform"],
                    "metrics":  metrics
                })
            except Exception:
                results.append({"topic": row["topic"], "post_id": row["post_id"],
                               "platform": row["post_platform"], "metrics": {}})
        conn.close()
        return jsonify(ok=True, posts=results)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@social_bp.route("/api/post-to-facebook", methods=["POST"])
@login_required
def post_to_facebook(current_user):
    """Post to Facebook Page."""
    import requests as _req
    data       = request.get_json()
    caption    = data.get("caption","").strip()
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.fb_page_token or not restaurant.fb_page_id:
        return jsonify(ok=False, error="Facebook not connected — click Connect Instagram & Facebook first")
    r = _req.post(f"https://graph.facebook.com/v19.0/{restaurant.fb_page_id}/feed", data={
        "message":      caption,
        "access_token": restaurant.fb_page_token,
    })
    if r.status_code != 200:
        err = r.json().get("error",{}).get("message","Unknown error")
        print(f"FB post failed: {r.text}")
        return jsonify(ok=False, error=err)
    post_id = r.json().get("id")
    try:
        from marketing import log_content as _lc_fb
        _topic_fb = data.get("topic", "")
        if _topic_fb and post_id:
            _lc_fb(current_user["restaurant_id"], "facebook_post", _topic_fb,
                   post_id=post_id, post_platform="facebook")
    except Exception as _e_fb:
        print(f"[insights] failed to log fb post_id: {_e_fb}")
    return jsonify(ok=True, post_id=post_id)

