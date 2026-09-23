"""
social_routes.py — Instagram and Facebook OAuth + posting routes
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import Blueprint, request, jsonify, redirect
import os

from models import get_restaurant, update_restaurant
import models as _models_mod

def get_conn(db_path=None):
    """models.get_conn, resolved at call time — CLAUDE.md's bound-import
    hazard. `from models import get_conn` bound the function object at
    import, so a test's monkeypatch of models.get_conn never reached the
    bare get_conn() calls in this module and they opened ./reviews.db."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()
from auth import login_required
from meta_api import graph_url, oauth_dialog_url

# Exception text handed to a client, with credentials stripped — a requests
# error carries the failing URL, and a Places URL carries key= in its query
# string. See ai_guard.safe_error.
from ai_guard import safe_error as _safe_err

social_bp = Blueprint('social', __name__)

@social_bp.route("/instagram/connect")
@login_required
def instagram_connect(current_user):
    """Open Meta OAuth in a popup — state carries restaurant_id."""
    import urllib.parse
    from flask import redirect as flask_redirect
    app_id       = os.getenv("META_APP_ID","")
    redirect_uri = os.getenv("META_REDIRECT_URI", "https://dashboard.cavnar.ai/instagram/callback")
    scope        = "instagram_basic,instagram_content_publish,instagram_manage_insights,pages_read_engagement,pages_manage_posts,pages_show_list,business_management,read_insights"
    # Signed, like the iOS flow: a bare restaurant id let anyone finish the
    # public dialog with their own Meta account and bind their page to any
    # restaurant (MOD-MKT-5). "web~" tells the callback to answer the popup.
    from gmb import sign_mobile_state
    state        = "web~" + sign_mobile_state(current_user["restaurant_id"])
    params = urllib.parse.urlencode({
        "client_id":     app_id,
        "redirect_uri":  redirect_uri,
        "scope":         scope,
        "auth_type":     "rerequest",
        "response_type": "code",
        "state":         state,
    })
    return flask_redirect(oauth_dialog_url(params))

@social_bp.route("/instagram/callback")
def instagram_callback():
    """Handle Meta OAuth callback — exchange code for token, get IG user ID."""
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

    # Exchange code for short-lived token. Every call is timed (MOD-MKT-2)
    # and read through _GraphAnswer, so an edge proxy's HTML page with a 200
    # ends in the popup's error rather than a 500 (MOD-A6-oauth-3).
    r = _graph("get", graph_url("oauth/access_token"), params={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": redirect_uri, "code": code,
    })
    short_token = (r.body or {}).get("access_token") if r.ok else None
    if not short_token:
        print(f"IG token exchange failed: {r.status} {r.text[:300]}")
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({ig:'error',msg:'token_failed'},'*');"
            "window.close();"
            "</script><p>Token exchange failed.</p></body></html>"
        )

    # Exchange for long-lived token (60 days)
    r2 = _graph("get", graph_url("oauth/access_token"), params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_token,
    })
    long_token = (r2.body or {}).get("access_token") or short_token

    # Get Facebook pages
    r3 = _graph("get", graph_url("me/accounts"), params={"access_token": long_token})
    pages = (r3.body or {}).get("data") or []
    ig_user_id = None
    page_token = long_token
    matched_page = None

    for page in pages:
        r4 = _graph("get", graph_url(page['id']), params={
            "fields": "instagram_business_account",
            "access_token": page.get("access_token", long_token),
        })
        ig_data = (r4.body or {}).get("instagram_business_account")
        if ig_data:
            ig_user_id = ig_data.get("id")
            page_token = page.get("access_token", long_token)
            matched_page = page
            break

    if not ig_user_id:
        print(f"No IG account found. Pages: {r3.text[:300]}")
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({ig:'error',msg:'no_ig_account'},'*');"
            "window.close();"
            "</script><p>No Instagram business account found.</p></body></html>"
        )

    # Both flows send a signed state (gmb.sign_mobile_state): the web popup
    # prefixes it with "web~" and is answered with postMessage; the iOS app
    # finishes on a deep link. An unsigned or bare-numeric state binds
    # nothing — it used to be trusted as the restaurant id.
    from gmb import verify_mobile_state
    web_signed = bool(state and state.startswith("web~"))
    mobile = bool(state and ":" in state and not web_signed)
    if web_signed:
        rid = verify_mobile_state(state[len("web~"):])
    elif mobile:
        rid = verify_mobile_state(state)
    else:
        rid = None
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

    if mobile:
        return redirect("cavnarai://ig-callback?status=" + ("connected" if rid else "error"))
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
    from marketing_drafts import may_publish, CANNOT_PUBLISH
    if not may_publish(current_user):
        return jsonify(ok=False, error=CANNOT_PUBLISH), 403
    data = request.get_json() or {}
    payload, status = _do_post_to_instagram(
        current_user["restaurant_id"], data.get("caption", ""), data.get("image_url", ""), data.get("topic", "")
    )
    return jsonify(**payload), status


# Every Graph call names a timeout (MOD-MKT-2): one black-holed connection
# used to hang a request thread, or the scheduler thread that publishes the
# queue and refreshes tokens, for as long as the OS allowed.
GRAPH_TIMEOUT = (5, 20)

# The Instagram container is polled on the caller's thread, so the wait is
# short and bounded (MOD-MKT-3): an immediate check, then 1+1+2+2+3 seconds.
# It used to sleep up to 20s — with --threads 4, four owners posting at once
# held the whole platform. A container still processing after this is
# abandoned (nothing was published) and the owner is told to try again.
_IG_POLL_WAITS = (0, 1, 1, 2, 2, 3)


class _GraphAnswer:
    """One Graph response, read defensively: Meta's edge answers an HTML 502
    now and then, and r.json() on that raised straight through the route
    (MOD-MKT-4). `ambiguous` is the question the queue has to ask of a
    PUBLISH call: did Meta possibly accept it anyway? A 5xx, a 429, an
    unreadable body or a dropped connection may have; a 4xx with an error
    is Meta saying no."""

    def __init__(self, resp=None, exc=None):
        self.exc = exc
        self.status = getattr(resp, "status_code", 0) if resp is not None else 0
        self.text = (getattr(resp, "text", "") or "") if resp is not None else str(exc or "")
        try:
            self.body = resp.json() if resp is not None else None
        except Exception:
            self.body = None
        if not isinstance(self.body, dict):
            self.body = None

    @property
    def ok(self):
        return self.status == 200 and self.body is not None

    @property
    def ambiguous(self):
        return (self.exc is not None or self.status >= 500 or self.status == 429
                or (self.status == 200 and self.body is None))

    @property
    def error(self):
        return (self.body or {}).get("error") or {}


def _graph(method, url, **kw):
    import requests as _req
    kw.setdefault("timeout", GRAPH_TIMEOUT)
    try:
        return _GraphAnswer(getattr(_req, method)(url, **kw))
    except Exception as e:
        return _GraphAnswer(exc=e)


def _owner_error(platform, answer, fallback):
    """Meta's refusal in words an owner can act on (MOD-MKT-18). The raw
    "(#200) ... pages_manage_posts ..." goes to the log, never the screen."""
    err = answer.error
    code = err.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    print(f"[social] {platform} Graph refusal {answer.status}: {answer.text[:300]}")
    name = platform.title()
    if code == 190 or err.get("type") == "OAuthException" and code in (None, 102, 463, 467):
        return (f"{name}'s connection has expired. Reconnect it under Account → "
                "Connections, then post again.")
    if code in (10, 200, 294, 299) or (code and 200 <= code < 300):
        return (f"{name} didn't give Cavnar AI permission to post. Reconnect it under "
                "Account → Connections and allow posting.")
    if code in (4, 17, 32, 613):
        return f"{name} is limiting how often posts can go out. Wait a few minutes and try again."
    if code == 368:
        return f"{name} blocked this post. Check your Page for a notice from Meta."
    if code == 506:
        return f"{name} says this exact post is already on your Page."
    if code == 9004 or code == 36003 or "image" in (err.get("message") or "").lower():
        return "Instagram couldn't use that photo. Try a different photo, or re-add it."
    return fallback


def _maybe_live(platform):
    return (f"{platform.title()} didn't answer clearly, so this post may already be "
            "live. Check your Page before posting it again.")


def _do_post_to_instagram(restaurant_id, caption, image_url, topic):
    """Shared by the web route above and mobile_api.py's own post-to-instagram.

    A refusal carries `maybe_live` only when the PUBLISH step itself got an
    ambiguous answer; a failure creating or processing the container means
    nothing reached the feed, so the queue may retry it (MOD-MKT-4)."""
    import time as _time
    caption = (caption or "").strip()
    image_url = (image_url or "").strip()

    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.ig_token or not restaurant.ig_user_id:
        return {"ok": False, "error": "Instagram not connected — click Connect Instagram first"}, 200

    ig_user_id = restaurant.ig_user_id
    token      = restaurant.ig_token

    if not image_url:
        return {"ok": False, "error": "Instagram requires an image. Paste a public image URL into the Image URL field before posting."}, 200

    created = _graph("post", graph_url(f"{ig_user_id}/media"), data={
        "image_url":    image_url,
        "caption":      caption,
        "access_token": token,
    })
    creation_id = (created.body or {}).get("id") if created.ok else None
    if not creation_id:
        print(f"IG media create failed: {created.status} {created.text[:300]}")
        return {"ok": False, "error": _owner_error(
            "instagram", created, "Instagram didn't accept the photo just now. Nothing was posted — try again.")}, 200

    status = ""
    for wait in _IG_POLL_WAITS:
        if wait:
            _time.sleep(wait)
        polled = _graph("get", graph_url(creation_id),
                        params={"fields": "status_code", "access_token": token})
        status = ((polled.body or {}).get("status_code") or "") if polled.ok else ""
        if status in ("FINISHED", "ERROR", "EXPIRED"):
            break
    if status in ("ERROR", "EXPIRED"):
        # Meta rejected the image (aspect ratio, an unreachable URL). Sending
        # it to media_publish anyway was a guaranteed failure in Meta's words.
        return {"ok": False, "error": "Instagram couldn't use that photo (it may be the wrong shape "
                                      "or size). Nothing was posted — try a different photo."}, 200
    if status != "FINISHED":
        return {"ok": False, "error": "Instagram is still processing the photo. Nothing was posted "
                                      "yet — try again in a minute."}, 200

    published = _graph("post", graph_url(f"{ig_user_id}/media_publish"), data={
        "creation_id":  creation_id,
        "access_token": token,
    })
    post_id = (published.body or {}).get("id") if published.ok else None
    if not post_id:
        if published.ambiguous:
            return {"ok": False, "maybe_live": True, "error": _maybe_live("instagram")}, 200
        return {"ok": False, "error": _owner_error(
            "instagram", published, "Instagram didn't publish the post. Nothing went out — try again.")}, 200

    # Save post_id for engagement tracking
    try:
        from marketing import log_content as _lc
        if topic and post_id:
            _lc(restaurant_id, "instagram_post", topic, post_id=post_id, post_platform="instagram", body=caption)
    except Exception as _e:
        print(f"[insights] failed to log post_id: {_e}")
    return {"ok": True, "post_id": post_id}, 200

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
    """Raw Graph answer for this restaurant's own latest Facebook post. It
    read a hard-coded post id (someone else's) with whatever token this
    restaurant had — None when Facebook was not connected (MOD-MKT-18)."""
    from flask import jsonify as _jsonify
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.fb_page_token or not restaurant.fb_page_id:
        return _jsonify(ok=False, error="Facebook not connected")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT post_id FROM marketing_content_log WHERE restaurant_id=? AND post_platform='facebook' "
            "AND post_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (current_user["restaurant_id"],)).fetchone()
    finally:
        conn.close()
    if not row:
        return _jsonify(ok=False, error="No Facebook post from Cavnar AI to inspect yet")
    answer = _graph("get", graph_url(row["post_id"]), params={
        "fields": "id,message,likes.summary(true),comments.summary(true),shares",
        "access_token": restaurant.fb_page_token})
    return _jsonify(ok=answer.ok, post_with_likes=answer.body, fb_page_id=restaurant.fb_page_id,
                    fb_token_present=True)

def _fb_post_metrics(post_id, token, _req):
    """Facebook Page post engagement + reach/impressions. Reach/impressions is
    fetched separately from engagement so one deprecated metric name can't
    zero out likes/comments/shares too — and falls back to a single metric
    if the paired request is rejected."""
    metrics = {}
    r = _req.get(
        graph_url(post_id),
        params={"fields": "reactions.summary(true),comments.summary(true),shares",
                "access_token": token},
        timeout=5
    )
    if r.status_code == 200:
        d = r.json()
        metrics["likes"]    = d.get("reactions", {}).get("summary", {}).get("total_count", 0)
        metrics["comments"] = d.get("comments", {}).get("summary", {}).get("total_count", 0)
        metrics["shares"]   = d.get("shares", {}).get("count", 0)
    else:
        _capture_insights_error("facebook_engagement", post_id, r)

    for metric_set in ("post_impressions,post_impressions_unique", "post_impressions_unique"):
        r2 = _req.get(
            graph_url(post_id + "/insights"),
            params={"metric": metric_set, "period": "lifetime", "access_token": token},
            timeout=5
        )
        if r2.status_code == 200:
            for m in r2.json().get("data", []):
                val = m.get("values", [{}])[-1].get("value", 0) if m.get("values") else m.get("value", 0)
                if m["name"] == "post_impressions":
                    metrics["impressions"] = val
                elif m["name"] == "post_impressions_unique":
                    metrics["reach"] = val
            break
        _capture_insights_error(f"facebook_insights({metric_set})", post_id, r2)
    return metrics


def _ig_post_metrics(post_id, token, _req):
    """Instagram media insights. `impressions` has been deprecated on and off
    across API versions for image/carousel media — request it first, and if
    Meta rejects the whole field list, retry without it so reach/likes/
    comments/saved still come through instead of the request failing whole."""
    metrics = {}
    for metric_set in ("reach,impressions,likes,comments_count,saved",
                       "reach,likes,comments_count,saved"):
        r = _req.get(
            graph_url(post_id + "/insights"),
            params={"metric": metric_set, "access_token": token},
            timeout=5
        )
        if r.status_code == 200:
            for m in r.json().get("data", []):
                metrics[m["name"]] = m.get("values", [{}])[-1].get("value", 0)
            return metrics
        _capture_insights_error(f"instagram_insights({metric_set})", post_id, r)
    return metrics


def _capture_insights_error(what, post_id, resp):
    """Surface a real Meta API failure (bad metric name, expired token,
    revoked permission) instead of letting it disappear into empty metrics
    forever — this is what makes 'analytics are working' verifiable rather
    than assumed."""
    print(f"[insights] {what} for {post_id} failed: {resp.status_code} {resp.text[:300]}")
    try:
        import ops
        ops.capture(Exception(resp.text[:300]), job="post_insights",
                    context=f"{what} post={post_id} status={resp.status_code}")
    except Exception:
        pass


def refresh_post_metrics(restaurant_id, limit=25):
    """Pull fresh engagement metrics for a restaurant's recent posts and
    write them back to marketing_content_log. Shared by the on-demand
    /api/post-insights route and the nightly scheduler sync — one
    implementation, so a fix here reaches both callers."""
    import requests as _req
    from models import get_conn, get_restaurant
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or (not restaurant.ig_token and not restaurant.fb_page_token):
        return {"ok": False, "error": "Not connected", "posts": []}
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT id, topic, post_id, post_platform, created_at,
                      reach, impressions, engaged, likes, comments, shares
               FROM marketing_content_log
               WHERE restaurant_id=? AND post_id IS NOT NULL
               ORDER BY created_at DESC LIMIT ?""",
            (restaurant_id, limit)
        ).fetchall()
        results = []
        for row in rows:
            if not row["post_id"]:
                continue
            try:
                token = restaurant.fb_page_token if row["post_platform"] == "facebook" else restaurant.ig_token
                if not token:
                    results.append({"topic": row["topic"], "post_id": row["post_id"],
                                    "platform": row["post_platform"], "metrics": {}})
                    continue
                if row["post_platform"] == "facebook":
                    metrics = _fb_post_metrics(row["post_id"], token, _req)
                else:
                    metrics = _ig_post_metrics(row["post_id"], token, _req)
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
            except Exception as e:
                _capture_insights_error("refresh_post_metrics row", row["post_id"], type("R", (), {"status_code": 0, "text": str(e)})())
                results.append({"topic": row["topic"], "post_id": row["post_id"],
                               "platform": row["post_platform"], "metrics": {}})
        return {"ok": True, "posts": results}
    finally:
        conn.close()


@social_bp.route("/api/post-insights")
@login_required
def post_insights(current_user):
    """Fetch (and refresh) engagement metrics for posted content."""
    try:
        result = refresh_post_metrics(current_user["restaurant_id"])
        return jsonify(**result)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))

@social_bp.route("/api/post-to-facebook", methods=["POST"])
@login_required
def post_to_facebook(current_user):
    """Post to Facebook Page."""
    from marketing_drafts import may_publish, CANNOT_PUBLISH
    if not may_publish(current_user):
        return jsonify(ok=False, error=CANNOT_PUBLISH), 403
    data = request.get_json() or {}
    payload, status = _do_post_to_facebook(
        current_user["restaurant_id"], data.get("caption", ""), data.get("topic", "")
    )
    return jsonify(**payload), status


def _do_post_to_facebook(restaurant_id, caption, topic):
    """Shared by the web route above and mobile_api.py's own post-to-facebook.
    `maybe_live` marks an answer that may hide an accepted post (MOD-MKT-4)."""
    caption = (caption or "").strip()
    if not caption:
        return {"ok": False, "error": "There's no post text to publish."}, 200
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.fb_page_token or not restaurant.fb_page_id:
        return {"ok": False, "error": "Facebook not connected — click Connect Instagram & Facebook first"}, 200
    answer = _graph("post", graph_url(f"{restaurant.fb_page_id}/feed"), data={
        "message":      caption,
        "access_token": restaurant.fb_page_token,
    })
    post_id = (answer.body or {}).get("id") if answer.ok else None
    if not post_id:
        print(f"FB post failed: {answer.status} {answer.text[:300]}")
        if answer.ambiguous:
            return {"ok": False, "maybe_live": True, "error": _maybe_live("facebook")}, 200
        return {"ok": False, "error": _owner_error(
            "facebook", answer, "Facebook didn't accept the post. Nothing went out — try again.")}, 200
    try:
        from marketing import log_content as _lc_fb
        if topic and post_id:
            _lc_fb(restaurant_id, "facebook_post", topic, post_id=post_id, post_platform="facebook", body=caption)
    except Exception as _e_fb:
        print(f"[insights] failed to log fb post_id: {_e_fb}")
    return {"ok": True, "post_id": post_id}, 200


@social_bp.route("/api/meta-review-test")
@login_required
def meta_review_test(current_user):
    """Temporary endpoint to trigger required Meta App Review test calls."""
    import requests as _req
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.fb_page_token or not restaurant.fb_page_id:
        return jsonify(ok=False, error="Facebook not connected")

    fb_token   = restaurant.fb_page_token
    fb_page_id = restaurant.fb_page_id
    ig_token   = restaurant.ig_token
    ig_user_id = restaurant.ig_user_id
    results    = {}

    # Get a FB post ID from the database
    fb_post_id = None
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT post_id FROM marketing_content_log WHERE restaurant_id=? AND post_platform='facebook' AND post_id IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (current_user["restaurant_id"],)
        ).fetchone()
        if row:
            fb_post_id = row["post_id"]
        conn.close()
    except Exception:
        pass

    # Fall back to feed API if no DB post found
    if not fb_post_id:
        r_feed = _req.get(
            graph_url(fb_page_id + "/feed"),
            params={"fields": "id", "limit": 1, "access_token": fb_token},
            timeout=10
        )
        fb_post_id = (r_feed.json().get("data") or [{}])[0].get("id")

    results["fb_post_id_found"] = fb_post_id

    # read_insights — try multiple metrics until one succeeds
    if fb_post_id:
        for metric in ["post_impressions,post_impressions_unique", "post_activity", "post_clicks"]:
            r1 = _req.get(
                graph_url(fb_post_id + "/insights"),
                params={"metric": metric, "period": "lifetime", "access_token": fb_token},
                timeout=10
            )
            results["read_insights_" + metric.split(",")[0]] = {"status": r1.status_code, "body": r1.json()}
            if r1.status_code == 200:
                break

    # instagram_manage_insights
    if ig_token and ig_user_id:
        r4 = _req.get(
            graph_url(ig_user_id + "/insights"),
            params={"metric": "reach", "period": "day", "access_token": ig_token},
            timeout=10
        )
        results["instagram_manage_insights"] = {"status": r4.status_code, "body": r4.json()}

    return jsonify(ok=True, results=results)
