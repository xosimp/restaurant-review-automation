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
    # "exa mple.com" is not a host; forwarding a guest there is a broken
    # page with the restaurant's name on the link (MOD-A6-links-7).
    if any(ch.isspace() for ch in parsed.netloc):
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


# Booking pages live on the reservation system's own host: a restaurant
# whose book is on Resy links guests to resy.com (restaurants.reservation_provider,
# reservation_feeds.PROVIDERS).
BOOKING_HOSTS = {
    "tock": ("exploretock.com", "tock.com"),
    "opentable": ("opentable.com",),
    "resy": ("resy.com",),
}


def _host_of(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    host = urlparse(raw if "://" in raw else "https://" + raw).netloc.lower()
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def allowed_hosts(restaurant_id, db_path: str = DB_PATH) -> set:
    """Where a short link may send a guest (MB-21, SOC-11): the restaurant's
    own website (its menu link, restaurants.menu_url), its booking system's
    host, and the platform itself (a join link). A link minted to any other
    host is an open redirect wearing the restaurant's name — a phishing
    front any tenant could mint, and a reason carriers filter the number."""
    out = set()
    try:
        import config
        out.add(_host_of(config.base_url()))
    except Exception:
        pass
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT menu_url, reservation_provider FROM restaurants WHERE id=?",
                           (restaurant_id,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if row:
        own = _host_of(row["menu_url"])
        if own:
            out.add(own)
        out.update(BOOKING_HOSTS.get((row["reservation_provider"] or "").strip().lower(), ()))
    return {h for h in out if h}


def _is_allowed(host: str, allowed: set) -> bool:
    return bool(host) and any(host == d or host.endswith("." + d) for d in allowed)


NOT_OWN_SITE = ("Short links go only to your own website, your booking page or Cavnar AI. "
                "Add your website as the Menu URL in Settings, then try again.")


def create_link(restaurant_id, target_url, *, source="sms", campaign="", label="",
                db_path: str = DB_PATH) -> dict:
    target = _valid_target(target_url)
    if not target:
        return {"ok": False, "error": "That doesn't look like a web address."}
    if not _is_allowed(_host_of(target), allowed_hosts(restaurant_id, db_path=db_path)):
        return {"ok": False, "not_own_site": True, "error": NOT_OWN_SITE}
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


# A second tap from the same visitor on the same link inside this window is
# the same guest (a double tap, a back-and-forward, a refresh), not another
# one. Also what stops anyone with the link inflating a campaign by reloading
# it (MOD-MKT-18).
TAP_DEDUPE_MINUTES = 30

# Link-preview fetchers, crawlers and scripted clients. A preview is the
# messaging app or social network reading the link, not a guest tapping it,
# and taps rank campaigns in guest_marketing.diagnose (MOD-MKT-18).
_NOT_A_GUEST = ("facebookexternalhit", "facebot", "slackbot", "twitterbot", "whatsapp",
                "telegrambot", "discordbot", "linkedinbot", "skypeuripreview", "pinterest",
                "redditbot", "embedly", "applebot", "googlebot", "bingbot", "yandex",
                "duckduckbot", "baiduspider", "bot/", "bot ", "crawler", "spider",
                "preview", "curl/", "wget/", "python-requests", "httpx", "go-http-client",
                "headlesschrome", "okhttp")


def is_preview_agent(user_agent: str) -> bool:
    ua = (user_agent or "").strip().lower()
    if not ua:
        return True             # nobody's phone sends no User-Agent
    return ua.endswith("bot") or any(marker in ua for marker in _NOT_A_GUEST)


def visitor_key(remote_addr: str, user_agent: str) -> str:
    """A one-way key for de-duplicating taps. Not a person: a hash of the
    address and browser, kept two days (ops retention) and never shown."""
    import hashlib
    import os
    salt = os.getenv("SECRET_KEY") or "cavnar-taps"
    raw = f"{salt}|{remote_addr or ''}|{(user_agent or '')[:200]}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


# The writes a public GET may cause (MB-21): a counted tap is two writes on
# the one SQLite writer, and /g/ needs no login. Past these a guest is still
# forwarded — the tap is just not written. Per link, whoever asks, and per
# visitor across links. Process-local (ai_utils.ai_rate_limited), like the
# other request limits while gunicorn runs one worker.
TAP_WRITES_PER_LINK_PER_MINUTE = 60
TAP_WRITES_PER_VISITOR_PER_MINUTE = 10


def _tap_write_allowed(token: str, visitor: str) -> bool:
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"gtap:{token}", max_calls=TAP_WRITES_PER_LINK_PER_MINUTE, window_secs=60):
        return False
    if visitor and ai_rate_limited(f"gtapv:{visitor}", max_calls=TAP_WRITES_PER_VISITOR_PER_MINUTE,
                                   window_secs=60):
        return False
    return True


def resolve(token: str, db_path: str = DB_PATH, count: bool = True, visitor: str = None):
    """Return where to send them, or None, and record the tap when `count`.

    The /g/ route passes count=False for a HEAD or a link-preview fetcher,
    and a `visitor` key so the same visitor tapping again within
    TAP_DEDUPE_MINUTES is not counted twice. A tap past the write budget
    (_tap_write_allowed) forwards without writing."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, target_url FROM marketing_links WHERE token=?", (token,)
        ).fetchone()
        if not row:
            return None
        if count and not _tap_write_allowed(token, visitor):
            count = False
        if count and visitor:
            seen = conn.execute(
                "SELECT 1 FROM marketing_link_taps WHERE link_id=? AND visitor=? "
                "AND tapped_at >= datetime('now', ?) LIMIT 1",
                (row["id"], visitor, f"-{TAP_DEDUPE_MINUTES} minutes")).fetchone()
            if seen:
                count = False
            else:
                conn.execute("INSERT INTO marketing_link_taps (link_id, visitor) VALUES (?,?)",
                             (row["id"], visitor))
        if count:
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
