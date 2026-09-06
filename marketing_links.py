"""marketing_links.py — short links, so marketing is measurable off-platform.

Nothing this module published could be traced past the platform it went to.
Instagram tells you a post got reach; it cannot tell you anyone came. SMS was
worse: draft_campaign_message's own prompt says "No links or phone numbers",
so a text could never carry one at all, which means a text club could ask
guests to come back and never learn whether one did.

A short link is the smallest honest answer. `/g/<token>` records the click and
redirects; the count hangs off the campaign or the post that carried it.

Deliberately not a tracking pixel or a per-recipient token: this counts
clicks on a campaign, not people. A restaurant does not need to know which
guest tapped, and building that would turn a text club into surveillance.
"""
import logging
import secrets
from urllib.parse import urlparse, urlencode, urlunparse, parse_qsl

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

# UTM source per channel, so anything the restaurant already has (Google
# Analytics on their own site, a Toast online-ordering report) can see the
# same traffic Cavnar AI is counting.
UTM_SOURCE = {
    "sms": "cavnar_sms",
    "instagram": "cavnar_instagram",
    "facebook": "cavnar_facebook",
    "google": "cavnar_google",
    "email": "cavnar_email",
}


def _valid_target(url: str) -> str:
    """Only absolute http(s). An open redirect that accepts anything is a
    phishing endpoint wearing a restaurant's domain."""
    url = (url or "").strip()
    if not url:
        return ""
    lowered = url.lower()
    if not lowered.startswith(("http://", "https://")):
        # A bare "example.com/menu" is what an owner types, and is fine. But
        # anything carrying its OWN scheme is not — prepending https:// to
        # "javascript:alert(1)" produced a URL that parsed cleanly and turned
        # this redirect into a script-injection endpoint on the restaurant's
        # own domain.
        host = lowered.split("/", 1)[0]
        if ":" in host and not host.rsplit(":", 1)[-1].isdigit():
            # "example.com:8080/menu" is a host and a port. "javascript:..."
            # is a scheme, and prepending https:// to it produced a URL that
            # parsed cleanly and turned this redirect into a script-injection
            # endpoint on the restaurant's own domain.
            return ""
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    # Reject a netloc that is really a scheme in disguise.
    if ":" in parsed.netloc and not parsed.netloc.rsplit(":", 1)[-1].isdigit():
        return ""
    return url


def _tag(url: str, source: str, campaign: str) -> str:
    """Append UTMs without clobbering any the owner already put on the link."""
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params.setdefault("utm_source", UTM_SOURCE.get(source, "cavnar"))
    params.setdefault("utm_medium", "referral")
    if campaign:
        params.setdefault("utm_campaign", campaign[:60])
    return urlunparse(parsed._replace(query=urlencode(params)))


def create_link(restaurant_id, target_url, *, source="sms", campaign="", label="",
                db_path: str = DB_PATH) -> dict:
    target = _valid_target(target_url)
    if not target:
        return {"ok": False, "error": "That doesn't look like a web address."}
    token = secrets.token_urlsafe(7)
    tagged = _tag(target, source, campaign)
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO marketing_links (restaurant_id, token, target_url, label, source, campaign) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, token, tagged, label or None, source, campaign or None),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "token": token, "target_url": tagged}


def resolve(token: str, db_path: str = DB_PATH):
    """Record the click and return where to send them, or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, target_url FROM marketing_links WHERE token=?", (token,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE marketing_links SET clicks=clicks+1, last_click_at=datetime('now') WHERE id=?",
            (row["id"],),
        )
        conn.commit()
        return row["target_url"]
    except Exception as e:
        log.warning("link resolve failed for %s: %s", token, e)
        return None
    finally:
        conn.close()


def link_stats(restaurant_id, limit=20, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT token, target_url, label, source, campaign, clicks, created_at, last_click_at "
            "FROM marketing_links WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
            (restaurant_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
