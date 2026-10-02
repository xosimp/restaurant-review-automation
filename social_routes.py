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
from csrf import csrf_exempt
from meta_api import graph_url, oauth_dialog_url

# Exception text handed to a client, with credentials stripped — a requests
# error carries the failing URL, and a Places URL carries key= in its query
# string. See ai_guard.safe_error.
from ai_guard import safe_error as _safe_err

social_bp = Blueprint('social', __name__)
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(social_bp)

@social_bp.route("/instagram/connect")
@login_required
def instagram_connect(current_user):
    """Open Meta OAuth in a popup — state carries restaurant_id. Owner-only,
    like every other connection: the callback (no session) trusts the signed
    state this mints, so this is where the owner check has to be."""
    from permissions import principal_only
    denied = principal_only(current_user, "the Instagram and Facebook connection")
    if denied:
        return denied
    import urllib.parse
    from flask import redirect as flask_redirect
    app_id       = os.getenv("META_APP_ID","")
    redirect_uri = os.getenv("META_REDIRECT_URI", "https://dashboard.cavnar.ai/instagram/callback")
    from meta_api import SCOPES as scope
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

def _ig_popup_error(code, sentence):
    """The web popup's failure: tell the opener (its `msg` code is what the
    Connections card reads), close, and say it in words if the window stays
    open. Nothing in it comes from the request or from Meta."""
    return (
        "<html><body><script>"
        "window.opener&&window.opener.postMessage({ig:'error',msg:'" + code + "'},window.location.origin);"
        "window.close();"
        "</script><p>" + sentence + "</p></body></html>"
    )


@social_bp.route("/instagram/callback")
def instagram_callback():
    """Handle Meta OAuth callback — exchange code for token, get IG user ID.

    The signed state is checked FIRST (MB-21, SOC-13): the code used to be
    exchanged and every Page read before the state was looked at, and the
    web popup then answered "connected" whatever the state said — an
    expired or foreign state saved nothing and the owner was told it had."""
    from models import update_restaurant as _update_r

    code         = request.args.get("code")
    state        = request.args.get("state")
    app_id       = os.getenv("META_APP_ID","")
    app_secret   = os.getenv("META_APP_SECRET","")
    redirect_uri = os.getenv("META_REDIRECT_URI", "https://dashboard.cavnar.ai/instagram/callback")

    # Both flows send a signed state (gmb.sign_mobile_state): the web popup
    # prefixes it with "web~" and is answered with postMessage; the iOS app
    # finishes on a deep link. An unsigned, bare-numeric, tampered or
    # expired state binds nothing — it used to be trusted as the restaurant id.
    from gmb import verify_mobile_state
    web_signed = bool(state and state.startswith("web~"))
    mobile = bool(state and ":" in state and not web_signed)
    if web_signed:
        rid = verify_mobile_state(state[len("web~"):])
    elif mobile:
        rid = verify_mobile_state(state)
    else:
        rid = None

    def _fail(code_, sentence):
        if mobile:
            return redirect("cavnarai://ig-callback?status=error")
        return _ig_popup_error(code_, sentence)

    if not code:
        # Meta sends error=access_denied when the person says no in its dialog.
        if request.args.get("error") == "access_denied":
            return _fail("denied", "You didn't allow Cavnar AI on Facebook. Nothing was connected.")
        return _fail("no_code", "Connection failed.")
    if not rid:
        return _fail("state_invalid", "This connection link expired or didn't start here. "
                                      "Nothing was connected — start again from Account → Connections.")

    # Exchange code for short-lived token. Every call is timed (MOD-MKT-2)
    # and read through _GraphAnswer, so an edge proxy's HTML page with a 200
    # ends in the popup's error rather than a 500 (MOD-A6-oauth-3). The app
    # secret travels in a POST body, never a query string a proxy or an
    # error message can log (MB-9).
    r = _graph("post", graph_url("oauth/access_token"), data={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": redirect_uri, "code": code,
    })
    short_token = (r.body or {}).get("access_token") if r.ok else None
    if not short_token:
        # Meta's own error message only, redacted — never the raw body.
        why = (r.error or {}).get("message") or ("no answer" if r.exc is not None else "an unreadable answer")
        print(f"[social] IG token exchange failed: {r.status} {_safe_err(why)}")
        return _fail("token_failed", "Token exchange failed.")

    # Exchange for long-lived token (60 days). The Page tokens read with it
    # never expire; their real expiry is read from Meta below.
    r2 = _graph("post", graph_url("oauth/access_token"), data={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_token,
    })
    long_token = (r2.body or {}).get("access_token") or short_token

    pages, answer = _read_pages(long_token)
    if pages is None:
        print(f"[social] Page list unreadable: {answer.status} "
              f"{_safe_err((answer.error or {}).get('message') or 'no message')}")
        return _fail("pages_failed", "Facebook didn't send the Page list. Nothing was connected — try again.")
    if not pages:
        return _fail("no_pages", "Facebook didn't share any Page. Sign in with a Facebook account that's an admin "
                                 "of the restaurant's Page, and tick that Page when Facebook asks.")
    if len(pages) == 1:
        return _connect_page(rid, pages[0], mobile)
    # More than one Page (someone who runs several restaurants' Pages, 10/2/26):
    # never guess. The first Page with Instagram used to be bound, whoever's it was.
    return _page_picker(pages, _seal_pick(rid, mobile, long_token))


# ── Which Page, and its Instagram (10/2/26) ─────────────────────────────────
#
# One Meta sign-in can manage many Pages - Danny runs marketing for more than
# one restaurant. The callback binds the one Page shared, or shows a picker:
# each Page by name with the Instagram account linked to it. The choice comes
# back to /instagram/choose carrying `pick`, the restaurant and the sign-in's
# token sealed (Fernet, a key derived from SECRET_KEY, 15 minutes) - nothing
# is stored until a Page is chosen, and the pick cannot be forged or replayed
# after it expires. A Page with no Instagram linked still connects Facebook.

PICK_TTL_SECONDS = 15 * 60


def _pick_fernet():
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    secret = os.getenv("SECRET_KEY")
    if not secret:
        raise RuntimeError("SECRET_KEY is not set; cannot seal the Page choice")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("meta-page-pick:" + secret).encode()).digest()))


def _seal_pick(rid, mobile, user_token):
    import json as _json
    return _pick_fernet().encrypt(_json.dumps({"rid": int(rid), "m": bool(mobile), "t": user_token}).encode()).decode()


def _open_pick(blob):
    """{"rid", "m", "t"} from a sealed pick, or None when it is forged,
    tampered or older than PICK_TTL_SECONDS."""
    import json as _json
    try:
        return _json.loads(_pick_fernet().decrypt((blob or "").encode(), ttl=PICK_TTL_SECONDS))
    except Exception:
        return None


def _read_pages(user_token):
    """([{"id", "name", "token", "ig_id", "ig_username"}], answer) for every
    Page the sign-in shared, or (None, answer) when Meta's list is unreadable."""
    answer = _graph("get", graph_url("me/accounts"), params={
        "fields": "id,name,access_token,instagram_business_account{id,username}",
        "limit": 100, "access_token": user_token,
    })
    if not answer.ok:
        return None, answer
    out = []
    for p in (answer.body or {}).get("data") or []:
        if not p.get("id") or not p.get("access_token"):
            continue
        ig = p.get("instagram_business_account") or {}
        out.append({"id": str(p["id"]), "name": (p.get("name") or "").strip() or "Untitled Page",
                    "token": p["access_token"], "ig_id": str(ig["id"]) if ig.get("id") else None,
                    "ig_username": (ig.get("username") or "").strip() or None})
    return out, answer


def _token_expiry(token):
    """The token's real expiry from Meta (debug_token): None for a Page
    token that never expires (expires_at 0) or one Meta wouldn't describe
    - data_freshness reads a missing expiry as live, and refresh_expiring_tokens
    leaves it alone. Else YYYY-MM-DD."""
    app_id, app_secret = os.getenv("META_APP_ID", ""), os.getenv("META_APP_SECRET", "")
    if not (app_id and app_secret and token):
        return None
    d = _graph("get", graph_url("debug_token"), params={"input_token": token,
                                                         "access_token": f"{app_id}|{app_secret}"})
    try:
        at = int(((d.body or {}).get("data") or {}).get("expires_at") or 0)
    except (TypeError, ValueError):
        at = 0
    if not at:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%d")


def _js_str(value):
    """A value for an inline script, safe inside <script>."""
    import json as _json
    return _json.dumps(value or "").replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _connect_page(rid, page, mobile):
    """Bind one Page (and its Instagram, when linked) to the restaurant and
    answer the popup or the phone. A Page without Instagram clears any old
    Instagram binding, so the two never point at different businesses."""
    expires = _token_expiry(page["token"])
    update = {"fb_page_token": page["token"], "fb_page_id": page["id"], "fb_page_name": page["name"],
              "fb_token_expires": expires}
    if page.get("ig_id"):
        update.update({"ig_token": page["token"], "ig_user_id": page["ig_id"],
                       "ig_username": page.get("ig_username"), "ig_token_expires": expires})
    else:
        update.update({"ig_token": None, "ig_user_id": None, "ig_username": None, "ig_token_expires": None})
    try:
        from models import update_restaurant as _update_r
        _update_r(rid, update)
    except Exception as e:
        # Nothing saved is never "connected" (MB-21).
        print(f"[social] Meta connection save failed for restaurant {rid}: {_safe_err(e)}")
        if mobile:
            return redirect("cavnarai://ig-callback?status=error")
        return _ig_popup_error("save_failed", "The connection couldn't be saved. Try connecting again.")
    has_ig = bool(page.get("ig_id"))
    print(f"[social] Meta connected for restaurant {rid}: Page {page['id']}"
          + (f" + Instagram {page['ig_id']}" if has_ig else " (no Instagram linked)"))
    if mobile:
        return redirect("cavnarai://ig-callback?status=connected" + ("" if has_ig else "&instagram=none"))
    import html as _h
    words = (f"Facebook: {_h.escape(page['name'])}" + (f" · Instagram: @{_h.escape(page['ig_username'] or '')}"
             if has_ig else " · Instagram isn't linked to this Page"))
    return (
        "<html><body style=\"font-family:-apple-system,sans-serif;background:#141110;color:#f0ebe0;padding:28px\"><script>"
        "window.opener&&window.opener.postMessage({ig:'connected',page:" + _js_str(page["name"])
        + ",username:" + _js_str(page.get("ig_username") if has_ig else "") + ",instagram:" + ("true" if has_ig else "false")
        + "},window.location.origin);window.close();"
        "</script><p>Connected. " + words + ". You can close this window.</p></body></html>"
    )


def _page_picker(pages, pick):
    """The popup's Page choice: each Page by name with its Instagram, one
    button each. Dark, like the dashboard; every name escaped."""
    import html as _h
    rows = ""
    for p in pages:
        ig = (f"Instagram @{_h.escape(p['ig_username'])}" if p.get("ig_id") and p.get("ig_username")
              else ("Instagram linked" if p.get("ig_id") else "No Instagram linked to this Page"))
        rows += ('<form method="post" action="/instagram/choose" style="margin:0">'
                 f'<input type="hidden" name="pick" value="{_h.escape(pick)}">'
                 f'<input type="hidden" name="page" value="{_h.escape(p["id"])}">'
                 '<button type="submit" style="width:100%;text-align:left;display:block;padding:14px 16px;margin:0 0 10px;'
                 'border-radius:12px;border:1px solid rgba(240,235,224,.14);background:#1e1a18;color:#f0ebe0;cursor:pointer;font:inherit">'
                 f'<b style="display:block;font-size:16px">{_h.escape(p["name"])}</b>'
                 f'<span style="font-size:14px;color:#a89f94">{ig}</span></button></form>')
    return (
        '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Choose the Page — Cavnar AI</title></head>'
        '<body style="margin:0;font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#141110;color:#f0ebe0">'
        '<div style="max-width:520px;margin:0 auto;padding:28px 22px">'
        '<div style="font-size:11px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#d4583a">Cavnar AI</div>'
        '<h1 style="font-size:22px;margin:8px 0 6px">Which Page is this restaurant?</h1>'
        '<p style="font-size:15px;line-height:1.5;color:#a89f94;margin:0 0 20px">This Facebook account manages more than '
        'one Page. Pick this restaurant\'s — Cavnar AI posts to it and to the Instagram linked to it, nothing else.</p>'
        + rows + '<p style="font-size:13px;color:#7d756c;margin-top:16px">This choice expires in 15 minutes.</p></div></body></html>'
    )


@social_bp.route("/instagram/choose", methods=["POST"])
@csrf_exempt
def instagram_choose():
    """The picker's answer. The sealed `pick` is the whole authority - it
    names the restaurant and carries the sign-in, can't be forged, and
    expires in PICK_TTL_SECONDS - so this needs no session (the phone's
    in-app browser has none), and a stale or tampered one binds nothing."""
    data = _open_pick(request.form.get("pick"))
    if not data:
        return _ig_popup_error("state_invalid", "This choice expired. Nothing was connected — start again from "
                                                "Account → Connections.")
    mobile = bool(data.get("m"))
    pages, answer = _read_pages(data.get("t"))
    chosen = next((p for p in (pages or []) if p["id"] == str(request.form.get("page") or "")), None)
    if not chosen:
        if mobile:
            return redirect("cavnarai://ig-callback?status=error")
        return _ig_popup_error("page_gone", "That Page isn't shared with Cavnar AI any more. Nothing was connected "
                                            "— start again.")
    return _connect_page(int(data["rid"]), chosen, mobile)


@social_bp.route("/api/post-to-instagram", methods=["POST"])
@login_required
def post_to_instagram(current_user):
    """Post a caption to Instagram. Client must have connected their account."""
    from marketing_drafts import may_publish, CANNOT_PUBLISH
    if not may_publish(current_user):
        return jsonify(ok=False, error=CANNOT_PUBLISH), 403
    data = request.get_json() or {}
    rid = current_user["restaurant_id"]
    image_url, bad = photo_url_from(rid, data)
    if bad:
        return jsonify(ok=False, error=bad), 400
    payload, status = _do_post_to_instagram(
        rid, data.get("caption", ""), image_url, data.get("topic", ""),
        content_log_id=_content_log_id(data), user=current_user,
    )
    if payload.get("ok") and data.get("rec_key"):
        import marketing_opportunities   # began on a feed card (OPP-10)
        marketing_opportunities.implemented_by_send(current_user["restaurant_id"], data.get("rec_key"), "social",
                                                    user_id=current_user.get("id"))
    return jsonify(**payload), status


def _content_log_id(data):
    """The generated piece this publish is (`content_log_id`, from
    /api/generate-content), or None — never guessed from the topic."""
    try:
        v = int((data or {}).get("content_log_id") or 0)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def photo_url_from(restaurant_id, data, base_url=None):
    """(url, error) — the photo a direct post carries, as an absolute URL
    Meta or Google can fetch. A library photo by `media_id` is checked
    against this restaurant and built on the public origin; an `image_url`
    that is the library's own relative "/m/<token>.jpg" (what a reopened
    draft sent — Meta can't fetch a relative URL, SOC-7) is made absolute.
    ("", None) when the post carries no photo."""
    data = data or {}
    root = (base_url or request.url_root or "").rstrip("/")
    media_id = data.get("media_id")
    if media_id not in (None, "", 0, "0"):
        from marketing_media import get_media_token, media_url
        try:
            token = get_media_token(int(media_id), restaurant_id)
        except (TypeError, ValueError):
            token = None
        if not token:
            return "", "That photo isn't in your library."
        return media_url(root, token), None
    url = (data.get("image_url") or "").strip()
    if url.startswith("/m/"):
        url = root + url
    return url, None


# How long the same post (Instagram photo and caption, or Facebook text)
# is refused after a publish started.
IG_PUBLISH_DEDUP_MINUTES = 10


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
        # Redacted at the source (MB-9): a requests error names the failing
        # URL, and a Graph GET carries access_token in its query string, so
        # every print of `text` was a place a Page token could land in a log.
        from ai_guard import redact_secrets
        self.text = redact_secrets((getattr(resp, "text", "") or "") if resp is not None else str(exc or ""))
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
    "(#200) ... pages_manage_posts ..." goes to the log, never the screen —
    and only Meta's error message, redacted, never the body (MB-9)."""
    err = answer.error
    code = err.get("code")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    print(f"[social] {platform} Graph refusal {answer.status}: "
          f"{_safe_err(err.get('message') or answer.text[:300], fallback='no message')}")
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


def _log_publish(restaurant_id, content_type, platform, post_id, caption, topic, content_log_id, user=None):
    """Every publish is logged, with its posted_at (MB-8): a direct post
    used to be logged only when the client sent a topic — the web's topic
    existed only after Generate, so a reopened draft or the owner's own
    caption left no row, no metrics, no attribution, and Home said
    "Nothing posted in N days" the day after a post. The generated row is
    matched by its id, never "the most recent unposted row for this topic"."""
    try:
        from marketing import log_content as _lc
        _lc(restaurant_id, content_type, (topic or "").strip() or caption[:80], post_id=post_id,
            post_platform=platform, body=caption, content_log_id=content_log_id, user=user)
    except Exception as e:
        print(f"[insights] failed to log {platform} post_id: {_safe_err(e)}")


def _do_post_to_instagram(restaurant_id, caption, image_url, topic, content_log_id=None, user=None):
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

    # One publish of this photo and caption at a time. An immediate publish
    # carried no claim, so a re-click during the ~20-second processing poll
    # (or the phone and the laptop at once) made a second public post
    # (DATA-25). The claim is given back only when Meta definitely refused;
    # a timeout may already be live, so it stands for the cooldown.
    import hashlib as _hl_ig
    import ops as _ops_ig
    _claim = "ig_publish:%s:%s" % (restaurant_id, _hl_ig.sha256(
        (image_url + "\x1f" + caption).encode("utf-8")).hexdigest()[:24])
    if not _ops_ig.claim_cooldown(_claim, IG_PUBLISH_DEDUP_MINUTES):
        # maybe_live: the queue treats it as possibly out already, not a retry.
        return {"ok": False, "duplicate": True, "maybe_live": True,
                "error": "This post is already being published. Check Instagram before posting it again."}, 409

    created = _graph("post", graph_url(f"{ig_user_id}/media"), data={
        "image_url":    image_url,
        "caption":      caption,
        "access_token": token,
    })
    creation_id = (created.body or {}).get("id") if created.ok else None
    if not creation_id:
        print(f"IG media create failed: {created.status} {created.text[:300]}")
        _ops_ig.release_period("cooldown", _claim)      # nothing was posted
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
        _ops_ig.release_period("cooldown", _claim)      # nothing was posted
        return {"ok": False, "error": "Instagram couldn't use that photo (it may be the wrong shape "
                                      "or size). Nothing was posted — try a different photo."}, 200
    if status != "FINISHED":
        _ops_ig.release_period("cooldown", _claim)      # nothing was posted
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
        _ops_ig.release_period("cooldown", _claim)      # Meta refused it; nothing is live
        return {"ok": False, "error": _owner_error(
            "instagram", published, "Instagram didn't publish the post. Nothing went out — try again.")}, 200

    # Save post_id for engagement tracking — always, topic or not (MB-8).
    _log_publish(restaurant_id, "instagram_post", "instagram", post_id, caption, topic, content_log_id, user=user)
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
    """Disconnect Instagram from this restaurant. Owner-only."""
    from permissions import principal_only
    denied = principal_only(current_user, "the Instagram and Facebook connection")
    if denied:
        return denied
    from models import update_restaurant
    update_restaurant(current_user["restaurant_id"], {"ig_token": "", "ig_user_id": "", "fb_page_token": "", "fb_page_id": "",
                                                      "ig_token_expires": None, "fb_token_expires": None,
                                                      "fb_page_name": None, "ig_username": None})
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

def _json_body(resp):
    try:
        body = resp.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _insight_value(m):
    """One insights entry's figure: `values[-1].value` (lifetime media
    insights), `total_value.value` (the newer shape), or a bare `value`."""
    if m.get("values"):
        return (m["values"][-1] or {}).get("value")
    if isinstance(m.get("total_value"), dict):
        return m["total_value"].get("value")
    return m.get("value")


def _fb_post_metrics(post_id, token, _req):
    """Facebook Page post engagement + reach/impressions. Reach/impressions is
    fetched separately from engagement so one deprecated metric name can't
    zero out likes/comments/shares too — and falls back to a single metric
    if the paired request is rejected. `post_impressions_unique` is REACH
    and `post_impressions` IMPRESSIONS: two columns, never added (MB-6)."""
    metrics = {}
    r = _req.get(
        graph_url(post_id),
        params={"fields": "reactions.summary(true),comments.summary(true),shares",
                "access_token": token},
        timeout=5
    )
    if r.status_code == 200:
        d = _json_body(r)
        metrics["likes"]    = (d.get("reactions") or {}).get("summary", {}).get("total_count", 0)
        metrics["comments"] = (d.get("comments") or {}).get("summary", {}).get("total_count", 0)
        metrics["shares"]   = (d.get("shares") or {}).get("count", 0)
    else:
        _capture_insights_error("facebook_engagement", post_id, r)

    for metric_set in ("post_impressions,post_impressions_unique", "post_impressions_unique"):
        r2 = _req.get(
            graph_url(post_id + "/insights"),
            params={"metric": metric_set, "period": "lifetime", "access_token": token},
            timeout=5
        )
        if r2.status_code == 200:
            for m in _json_body(r2).get("data", []) or []:
                val = _insight_value(m)
                if val is None:
                    continue
                if m.get("name") == "post_impressions":
                    metrics["impressions"] = val
                elif m.get("name") == "post_impressions_unique":
                    metrics["reach"] = val
            break
        _capture_insights_error(f"facebook_insights({metric_set})", post_id, r2)
    return metrics


# The Instagram media insights this sync asks for (MB-7). `comments_count`
# is a field of the media object, not an insights metric, and `impressions`
# was retired for media on Graph v21 — a metric list carrying either was
# refused whole (#100), so every Instagram post read 0 reach under a green
# "Metrics synced". Each name maps to a column of the same name.
IG_INSIGHT_METRICS = "reach,likes,comments,shares"
# The media object's own counts — what shows under the post, readable
# without the insights permission. Filled in only for what insights did
# not answer.
IG_MEDIA_FIELDS = "like_count,comments_count"


def _ig_post_metrics(post_id, token, _req):
    """Instagram media insights: reach, likes, comments and shares in one
    call. If Meta refuses the list (a media type without one of them),
    reach alone is asked for, and likes and comments come from the media
    object's own like_count / comments_count. Nothing unanswered is
    written as 0."""
    metrics = {}
    r = _req.get(graph_url(post_id + "/insights"),
                 params={"metric": IG_INSIGHT_METRICS, "access_token": token}, timeout=5)
    if r.status_code == 200:
        for m in _json_body(r).get("data", []) or []:
            val = _insight_value(m)
            if m.get("name") in ("reach", "likes", "comments", "shares") and val is not None:
                metrics[m["name"]] = val
    else:
        _capture_insights_error(f"instagram_insights({IG_INSIGHT_METRICS})", post_id, r)
        r2 = _req.get(graph_url(post_id + "/insights"),
                      params={"metric": "reach", "access_token": token}, timeout=5)
        if r2.status_code == 200:
            for m in _json_body(r2).get("data", []) or []:
                val = _insight_value(m)
                if m.get("name") == "reach" and val is not None:
                    metrics["reach"] = val
        else:
            _capture_insights_error("instagram_insights(reach)", post_id, r2)
    if "likes" not in metrics or "comments" not in metrics:
        r3 = _req.get(graph_url(post_id), params={"fields": IG_MEDIA_FIELDS, "access_token": token},
                      timeout=5)
        if r3.status_code == 200:
            d = _json_body(r3)
            if "likes" not in metrics and d.get("like_count") is not None:
                metrics["likes"] = d["like_count"]
            if "comments" not in metrics and d.get("comments_count") is not None:
                metrics["comments"] = d["comments_count"]
        else:
            _capture_insights_error("instagram_media_fields", post_id, r3)
    return metrics


import threading as _threading

# Inside refresh_post_metrics the failures of one restaurant's pass are
# collected here and reported ONCE: every failed Graph call used to run
# ops.capture, so one expired token was ~50 operator-digest entries a
# night (MOD-MKT-15).
_insights_pass = _threading.local()


def _meta_error_code(resp):
    try:
        return int(((resp.json() or {}).get("error") or {}).get("code"))
    except Exception:
        return None


def _capture_insights_error(what, post_id, resp):
    """Surface a real Meta API failure (bad metric name, expired token,
    revoked permission) instead of letting it disappear into empty metrics
    forever — this is what makes 'analytics are working' verifiable rather
    than assumed. The text is redacted first (MB-9): a failed GET names its
    URL, and that URL carries the access token."""
    status = getattr(resp, "status_code", 0)
    text = _safe_err((getattr(resp, "text", "") or "")[:300], fallback="no body")
    print(f"[insights] {what} for {post_id} failed: {status} {text}")
    bucket = getattr(_insights_pass, "errors", None)
    if bucket is not None:
        bucket.append({"what": what, "post_id": post_id, "status": status, "text": text,
                       "code": _meta_error_code(resp)})
        return
    try:
        import ops
        ops.capture(Exception(text), job="post_insights",
                    context=f"{what} post={post_id} status={status}")
    except Exception:
        pass


# Metrics a sync can measure, and so the only columns it may write. `engaged`
# is not a Graph field at all; writing metrics.get(k, 0) for every column
# zeroed whatever a failed or fallback call did not return (MOD-MKT-15).
_METRIC_COLUMNS = ("reach", "impressions", "engaged", "likes", "comments", "shares")

# One refresh stops asking Meta after this long (ARC-10); the posts it did
# not reach count as not measured this pass, so the pass reads "partial".
# 25 posts x up to 3 calls x a 5s timeout was over six minutes on one
# thread. The owner's refresh button, on a request thread, passes less.
REFRESH_DEADLINE_SECONDS = 90
OWNER_REFRESH_DEADLINE_SECONDS = 20


def refresh_post_metrics(restaurant_id, limit=25, deadline_s=REFRESH_DEADLINE_SECONDS):
    """Pull fresh engagement metrics for a restaurant's recent Instagram and
    Facebook posts and write them back to marketing_content_log, stamping
    each post Meta answered for (metrics_synced_at). Shared by the owner's
    refresh (/api/post-insights POST, the phone's refresh-metrics) and the
    nightly scheduler sync — one implementation, so a fix here reaches both.

    {"ok", "status", "measured", "attempted", "posts", "error"}: `status` is
    "ok" when every post it tried was measured (or there was nothing to
    measure), "partial" when some were not, "failed" when none were or the
    token is dead (MB-7). It used to say ok whenever the token was alive,
    so a pass where Meta refused every call stamped the sync green."""
    import time as _time
    import requests as _req
    from models import get_restaurant
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or (not restaurant.ig_token and not restaurant.fb_page_token):
        return {"ok": False, "error": "Not connected", "posts": []}

    def _token_for(platform):
        return restaurant.fb_page_token if platform == "facebook" else restaurant.ig_token

    conn = get_conn()
    _insights_pass.errors = errors = []
    token_dead = out_of_time = False
    measured = 0
    started = _time.monotonic()
    try:
        rows = conn.execute(
            """SELECT id, topic, post_id, post_platform, created_at,
                      reach, impressions, engaged, likes, comments, shares
               FROM marketing_content_log
               WHERE restaurant_id=? AND post_id IS NOT NULL AND TRIM(post_id) != ''
                 -- Meta's platforms only. A Google post has no Graph id: it
                 -- was sent to Graph with the Instagram token, a wasted call
                 -- and an error per post, and it ate the limit (SOC-20).
                 AND (post_platform IS NULL OR post_platform IN ('instagram', 'facebook'))
               ORDER BY COALESCE(posted_at, created_at) DESC, id DESC LIMIT ?""",
            (restaurant_id, limit)
        ).fetchall()
        # The posts this pass owes a measurement: every one whose platform
        # is connected. One it never reached (a dead token, the deadline)
        # is one it did not measure.
        attempted = sum(1 for r in rows if _token_for(r["post_platform"] or "instagram"))
        results = []
        for row in rows:
            if any(e.get("code") in (190, 102) for e in errors):
                # The token is dead; the other 24 posts would each fail the
                # same way. Stop and say so once.
                token_dead = True
                break
            if deadline_s is not None and _time.monotonic() - started > deadline_s:
                out_of_time = True
                break
            platform = row["post_platform"] or "instagram"
            token = _token_for(platform)
            if not token:
                # That platform is not connected now: not a failed
                # measurement, nothing to ask.
                results.append({"topic": row["topic"], "post_id": row["post_id"],
                                "platform": platform, "metrics": {}, "measured": False})
                continue
            try:
                if platform == "facebook":
                    metrics = _fb_post_metrics(row["post_id"], token, _req)
                else:
                    metrics = _ig_post_metrics(row["post_id"], token, _req)
                cols = [c for c in _METRIC_COLUMNS if metrics.get(c) is not None]
                if cols:
                    # A column this pass did not measure keeps its figure —
                    # except the DEFAULT 0 of a row never stamped before,
                    # which was never a measurement: it becomes NULL, so
                    # stamping the row does not turn that 0 into one (MB-7).
                    # (SET reads the row as it was: metrics_synced_at is
                    # still the old value inside the CASE.)
                    others = [c for c in _METRIC_COLUMNS if c not in cols]
                    conn.execute(
                        "UPDATE marketing_content_log SET "
                        + ", ".join(f"{c}=?" for c in cols)
                        + "".join(f", {c}=CASE WHEN metrics_synced_at IS NULL AND {c}=0 THEN NULL ELSE {c} END"
                                  for c in others)
                        + ", metrics_synced_at=datetime('now') WHERE id=?",
                        tuple(metrics[c] for c in cols) + (row["id"],)
                    )
                    conn.commit()
                    measured += 1
                results.append({
                    "topic":    row["topic"],
                    "post_id":  row["post_id"],
                    "platform": platform,
                    "metrics":  {c: metrics[c] for c in cols},
                    "measured": bool(cols),
                })
            except Exception as e:
                _capture_insights_error("refresh_post_metrics row", row["post_id"],
                                        type("R", (), {"status_code": 0, "text": _safe_err(e)})())
                results.append({"topic": row["topic"], "post_id": row["post_id"],
                               "platform": platform, "metrics": {}, "measured": False})
        if token_dead:
            return {"ok": False, "status": "failed", "measured": measured, "attempted": attempted,
                    "posts": results,
                    "error": "Meta says the connection has expired — reconnect Instagram & Facebook."}
        if attempted == 0 or measured == attempted:
            return {"ok": True, "status": "ok", "measured": measured, "attempted": attempted,
                    "posts": results}
        if measured == 0:
            return {"ok": False, "status": "failed", "measured": 0, "attempted": attempted,
                    "posts": results,
                    "error": f"Meta didn't return numbers for any of {attempted} post"
                             f"{'' if attempted == 1 else 's'}"}
        missed = attempted - measured
        return {"ok": False, "status": "partial", "measured": measured, "attempted": attempted,
                "posts": results,
                "error": f"{missed} of {attempted} posts couldn't be measured"
                         + (" before the refresh ran out of time" if out_of_time else "")}
    finally:
        conn.close()
        _insights_pass.errors = None
        if errors:
            first = errors[0]
            try:
                import ops
                ops.capture(Exception(first["text"]), job="post_insights",
                            context=f"restaurant_id={restaurant_id} {len(errors)} failed call(s); "
                                    f"first: {first['what']} post={first['post_id']} status={first['status']}")
            except Exception:
                pass


def stored_post_metrics(restaurant_id, limit=25) -> list:
    """What the last sync wrote for the recent posts, read from the database
    only — never Meta. A figure the platform never measured is absent (the
    post is `measured: False`), never 0."""
    from marketing_signals import MEASURED_SQL
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT topic, post_id, post_platform, reach, impressions, likes, comments, shares,
                      metrics_synced_at, CASE WHEN {MEASURED_SQL} THEN 1 ELSE 0 END AS measured
               FROM marketing_content_log
               WHERE restaurant_id=? AND post_id IS NOT NULL AND TRIM(post_id) != ''
               ORDER BY COALESCE(posted_at, created_at) DESC, id DESC LIMIT ?""",
            (restaurant_id, limit)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        m = {c: r[c] for c in ("reach", "impressions", "likes", "comments", "shares")
             if r["measured"] and r[c] is not None}
        out.append({"topic": r["topic"], "post_id": r["post_id"], "platform": r["post_platform"],
                    "metrics": m, "measured": bool(r["measured"]),
                    "metrics_synced_at": r["metrics_synced_at"]})
    return out


# The owner's "Refresh from Meta": the phone's twin's budget, on the same
# key, so the two share it (mobile_api.mobile_refresh_metrics).
OWNER_REFRESH_MAX_CALLS = 4
OWNER_REFRESH_WINDOW_SECS = 120


@social_bp.route("/api/post-insights", methods=["GET", "POST"])
@login_required
def post_insights(current_user):
    """GET: the recent posts' stored metrics — what the nightly sync (or an
    owner's refresh) wrote; never a call to Meta. The web Marketing tab used
    to GET this every 60 seconds while it was open, and each GET was up to
    75 Graph calls on a request thread with no rate limit (MB-17).

    POST: one owner-triggered refresh from Meta, rate limited like the
    phone's (4 per 2 minutes, the same key) and bounded in time. The answer
    says whether every post was measured (`status` ok | partial | failed)."""
    rid = current_user["restaurant_id"]
    if request.method != "POST":
        try:
            return jsonify(ok=True, stored=True, posts=stored_post_metrics(rid))
        except Exception as e:
            return jsonify(ok=False, error=_safe_err(e)), 500
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"mktmetrics:{rid}", max_calls=OWNER_REFRESH_MAX_CALLS,
                       window_secs=OWNER_REFRESH_WINDOW_SECS):
        return jsonify(ok=False, throttled=True, refreshed=0,
                       error="The numbers were just refreshed — try again in a couple of minutes.")
    try:
        result = refresh_post_metrics(rid, deadline_s=OWNER_REFRESH_DEADLINE_SECONDS)
        result["refreshed"] = int(result.get("measured") or 0)
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
    rid = current_user["restaurant_id"]
    image_url, bad = photo_url_from(rid, data)
    if bad:
        return jsonify(ok=False, error=bad), 400
    kw = {"content_log_id": _content_log_id(data), "user": current_user}
    if image_url:
        kw["image_url"] = image_url
    payload, status = _do_post_to_facebook(rid, data.get("caption", ""), data.get("topic", ""), **kw)
    if payload.get("ok") and data.get("rec_key"):
        import marketing_opportunities   # began on a feed card (OPP-10)
        marketing_opportunities.implemented_by_send(rid, data.get("rec_key"), "social",
                                                    user_id=current_user.get("id"))
    return jsonify(**payload), status


def _do_post_to_facebook(restaurant_id, caption, topic, image_url=None, content_log_id=None, user=None):
    """Shared by the web route above and mobile_api.py's own post-to-facebook.
    `maybe_live` marks an answer that may hide an accepted post (MOD-MKT-4).

    With `image_url` it is a photo post — /{page}/photos with the caption as
    its message — so the Page shows the photo the preview showed (CS-20,
    SOC-10): /feed took the text alone and the photo was silently dropped,
    on a direct post and on a scheduled one. The answer's `post_id` (the
    Page post, "<page>_<post>") is what insights read; `id` is the photo."""
    caption = (caption or "").strip()
    image_url = (image_url or "").strip()
    if not caption:
        return {"ok": False, "error": "There's no post text to publish."}, 200
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.fb_page_token or not restaurant.fb_page_id:
        return {"ok": False, "error": "Facebook not connected — click Connect Instagram & Facebook first"}, 200
    # The same claim as Instagram's (DATA-25): a double press, or the client
    # retrying after its own timeout, reached /feed twice and put the same
    # copy on the Page twice (MOD-A6-direct-5). Given back only when Meta
    # definitely refused; an ambiguous answer may be live, so it stands.
    import hashlib as _hl_fb
    import ops as _ops_fb
    _claim = "fb_publish:%s:%s" % (restaurant_id, _hl_fb.sha256(
        (caption if not image_url else image_url + "\x1f" + caption).encode("utf-8")).hexdigest()[:24])
    if not _ops_fb.claim_cooldown(_claim, IG_PUBLISH_DEDUP_MINUTES):
        return {"ok": False, "duplicate": True, "maybe_live": True,
                "error": "This post is already being published. Check Facebook before posting it again."}, 409
    if image_url:
        answer = _graph("post", graph_url(f"{restaurant.fb_page_id}/photos"), data={
            "url":          image_url,
            "message":      caption,
            "access_token": restaurant.fb_page_token,
        })
        post_id = ((answer.body or {}).get("post_id") or (answer.body or {}).get("id")) if answer.ok else None
    else:
        answer = _graph("post", graph_url(f"{restaurant.fb_page_id}/feed"), data={
            "message":      caption,
            "access_token": restaurant.fb_page_token,
        })
        post_id = (answer.body or {}).get("id") if answer.ok else None
    if not post_id:
        if answer.ambiguous:
            print(f"[social] FB post unclear: {answer.status} {_safe_err(answer.text[:300], fallback='no answer')}")
            return {"ok": False, "maybe_live": True, "error": _maybe_live("facebook")}, 200
        _ops_fb.release_period("cooldown", _claim)      # Meta refused it; nothing is live
        return {"ok": False, "error": _owner_error(
            "facebook", answer, "Facebook didn't accept the post. Nothing went out — try again.")}, 200
    # Always logged, topic or not (MB-8).
    _log_publish(restaurant_id, "facebook_post", "facebook", post_id, caption, topic, content_log_id, user=user)
    return {"ok": True, "post_id": post_id}, 200


@social_bp.route("/api/meta-review-test")
@login_required
def meta_review_test(current_user):
    """Temporary endpoint to trigger required Meta App Review test calls.

    Candidate for future cleanup after additional verification (MB-21,
    SOC-28): it answers any console login with raw Graph bodies. Trace
    9/28/26: no template, iOS or scheduler caller; listed in auth.py's
    module-exempt paths; one test (test_edge_mod_b_mkt_publish) asserts it
    makes no Graph call when Facebook is not connected; documented only in
    docs/audits. Meta may still need it for an App Review resubmission, so
    it stays until that is confirmed."""
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
