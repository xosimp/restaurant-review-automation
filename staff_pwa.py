"""
staff_pwa.py — the staff portal as an installable app for staff without an
iPhone (scheduler audit 10/10/26 MARKET-18).

There is no native Android app (EXTERNAL: a Kotlin/Play Store project is
outside this repository). What is built instead, on the same /staff/api/*
routes every client uses:

  manifest()       the web app manifest (/staff/manifest.webmanifest):
                   standalone, scope /staff/, start at /staff/home, the
                   brand icons (static/pwa).
  service_worker() the service worker (/staff/sw.js, scope /staff/): it
                   caches ONLY the shell — the static icons, fonts and
                   stylesheets under /static/ and an offline page that holds
                   nothing personal. /staff/home and every /staff/api/* call
                   go to the network every time and are never stored: a
                   shared phone never shows the last person's schedule. It
                   shows Web Push notices and opens the right tab on a tap.
  subscriptions    `web_push_subscriptions` (one per browser endpoint, the
                   login and the restaurant from the session, revocable,
                   revoked when the push service says it is gone). Web Push
                   itself is `web_push` — off until the VAPID keys are set
                   (EXTERNAL step: generate them and set them in Railway).
  send_to_user()   people.deliver's Web Push leg: a staff notice to a login
                   with no iPhone app device but a live browser subscription.
  platform         every offer answer records the client it came from —
                   the iPhone app (a Bearer token), the installed portal, or
                   a phone browser (Android or not, from the user agent) —
                   in `offer_response_platforms`, with the seconds from the
                   offer to the answer; `response_times()` is the admin's
                   read: median and n per platform, the measure the audit
                   asked for to judge whether Android staff are served.
"""
import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

PLATFORMS = ("ios_app", "android_pwa", "android_browser", "ios_browser", "pwa", "browser")
MIN_N = 5                       # answers on a platform before its median is stated
RESPONSE_DAYS = 90              # the admin's read of offer answers by client


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_staff_pwa(db_path=None):
    """Boot DDL (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS web_push_subscriptions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            endpoint        TEXT    NOT NULL UNIQUE,
            p256dh          TEXT    NOT NULL,
            auth            TEXT    NOT NULL,
            platform        TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            last_ok_at      TEXT,
            failures        INTEGER NOT NULL DEFAULT 0,
            revoked_at      TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_web_push_user ON web_push_subscriptions(user_id, restaurant_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS offer_response_platforms (
            offer_id        INTEGER PRIMARY KEY,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            platform        TEXT    NOT NULL,
            accepted        INTEGER,
            offered_at      TEXT,
            answered_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            seconds         REAL
        )""")
        # The retention prune and response_times() both read by answered_at.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_offer_resp_platforms_answered "
                     "ON offer_response_platforms(answered_at)")
        conn.commit()
    finally:
        conn.close()


# ── the shell ──────────────────────────────────────────────────────────────

def manifest() -> dict:
    return {
        "name": "Cavnar AI — Staff", "short_name": "Cavnar AI",
        "description": "Your shifts, requests, tasks and messages.",
        "id": "/staff/home", "start_url": "/staff/home", "scope": "/staff/", "display": "standalone",
        "background_color": "#0c0c0c", "theme_color": "#0c0c0c", "orientation": "portrait",
        "icons": [
            {"src": "/static/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/pwa/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }


CACHE_VERSION = "cavnar-staff-shell-v1"
OFFLINE_PATH = "/staff/offline"

# The service worker, served with the scope /staff/. ES5 like every script
# the portal ships. Only /static/ and the offline page are ever cached.
SERVICE_WORKER = r"""/* Cavnar AI staff portal service worker (staff_pwa.py). It caches only the
   shell: /static/ files and the offline page. Nothing under /staff/api/ and
   no page with a person's data is ever stored. */
var CACHE = '__CACHE__';
var OFFLINE = '__OFFLINE__';
self.addEventListener('install', function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) {
    return c.addAll([OFFLINE, '/static/pwa/icon-192.png']);
  }).then(function () { return self.skipWaiting(); }));
});
self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches['delete'](k); }));
  }).then(function () { return self.clients.claim(); }));
});
self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.indexOf('/static/') === 0) {
    e.respondWith(caches.open(CACHE).then(function (c) {
      return c.match(req).then(function (hit) {
        var net = fetch(req).then(function (res) { if (res && res.ok) c.put(req, res.clone()); return res; });
        return hit || net;
      });
    }));
    return;
  }
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(function () { return caches.match(OFFLINE); }));
  }
});
self.addEventListener('push', function (e) {
  var d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = {title: 'Cavnar AI', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Cavnar AI', {
    body: d.body || '', tag: d.tag || undefined, data: {tab: d.tab || 'today'},
    icon: '/static/pwa/icon-192.png', badge: '/static/pwa/icon-192.png'
  }));
});
self.addEventListener('notificationclick', function (e) {
  e.notification.close();
  var tab = (e.notification.data && e.notification.data.tab) || 'today';
  var target = '/staff/home#' + tab;
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].url.indexOf('/staff/') >= 0 && 'focus' in list[i]) { list[i].navigate(target); return list[i].focus(); }
    }
    return self.clients.openWindow(target);
  }));
});
"""


def service_worker() -> str:
    return SERVICE_WORKER.replace("__CACHE__", CACHE_VERSION).replace("__OFFLINE__", OFFLINE_PATH)


OFFLINE_PAGE = ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
                "content='width=device-width,initial-scale=1'><title>Offline — Cavnar AI</title>"
                "<style>:root{--paper:#0c0c0c;--ink:#f0ebe0;--ink2:#c4bdb4}body{margin:0;background:var(--paper);"
                "color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;"
                "padding:0 16px}.w{max-width:420px;margin:20vh auto;text-align:center}p{color:var(--ink2)}</style>"
                "</head><body><div class='w'><h1>You're offline</h1><p>Your shifts load when you're back online. "
                "Nothing about you is stored on this phone.</p></div></body></html>")


# ── platform ───────────────────────────────────────────────────────────────

def platform_of(headers, bearer: bool) -> str:
    """The client an answer came from: the iPhone app (a Bearer token), the
    installed portal (the page says display-mode standalone), or a browser
    — Android or iPhone by the user agent."""
    if bearer:
        return "ios_app"
    ua = str((headers or {}).get("User-Agent") or "").lower()
    display = str((headers or {}).get("X-Cavnar-Display") or "").lower()
    installed = display == "standalone"
    if "android" in ua:
        return "android_pwa" if installed else "android_browser"
    if "iphone" in ua or "ipad" in ua:
        return "pwa" if installed else "ios_browser"
    return "pwa" if installed else "browser"


def record_offer_answer(restaurant_id, offer_id, platform, accepted, db_path=None):
    """The answer's platform and seconds since the offer (never raises)."""
    try:
        conn = get_conn(db_path)
        try:
            o = conn.execute("SELECT created_at, answered_at FROM shift_offers WHERE id=? AND restaurant_id=?",
                             (int(offer_id), restaurant_id)).fetchone()
            secs = None
            if o and o["created_at"]:
                a = datetime.strptime(str(o["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
                b = (datetime.strptime(str(o["answered_at"])[:19], "%Y-%m-%d %H:%M:%S") if o["answered_at"]
                     else datetime.utcnow())
                secs = max(0.0, (b - a).total_seconds())
            conn.execute("INSERT OR REPLACE INTO offer_response_platforms (offer_id, restaurant_id, platform, accepted, "
                         "offered_at, seconds) VALUES (?,?,?,?,?,?)",
                         (int(offer_id), restaurant_id, platform if platform in PLATFORMS else "browser",
                          1 if accepted else 0, o["created_at"] if o else None, secs))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("offer platform not recorded rid=%s: %s", restaurant_id, e)


def response_times(db_path=None, days=RESPONSE_DAYS) -> dict:
    """{platforms: [{platform, n, median_minutes, accepted_share}]} over the
    last `days` — the median stated from MIN_N answers."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT platform, seconds, accepted FROM offer_response_platforms WHERE answered_at >= "
                            "datetime('now', ?)", (f"-{int(days)} days",)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    by = {}
    for r in rows:
        by.setdefault(r["platform"], []).append(r)
    out = []
    for p, rs in sorted(by.items()):
        secs = sorted(float(r["seconds"]) for r in rs if r["seconds"] is not None)
        med = secs[len(secs) // 2] / 60.0 if len(secs) >= MIN_N else None
        out.append({"platform": p, "n": len(rs), "median_minutes": round(med, 1) if med is not None else None,
                    "accepted_share": round(sum(1 for r in rs if r["accepted"]) / float(len(rs)), 3)})
    import web_push
    return {"ok": True, "days": days, "platforms": out, "web_push_enabled": web_push.enabled(),
            "subscriptions": _subscription_counts(db_path)}


def _subscription_counts(db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        return {r["platform"] or "browser": r["n"] for r in conn.execute(
            "SELECT platform, COUNT(*) AS n FROM web_push_subscriptions WHERE revoked_at IS NULL GROUP BY platform")}
    except Exception:
        return {}
    finally:
        conn.close()


# ── subscriptions ──────────────────────────────────────────────────────────

def subscribe(user_id, restaurant_id, sub: dict, platform=None, db_path=None) -> dict:
    import web_push
    if not web_push.enabled():
        raise ValueError("notifications in the browser aren't switched on for Cavnar AI yet")
    endpoint = str((sub or {}).get("endpoint") or "")
    keys = (sub or {}).get("keys") or {}
    if not web_push.endpoint_allowed(endpoint) or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("that isn't a browser push subscription")
    if len(endpoint) > 1000 or len(str(keys["p256dh"])) > 200 or len(str(keys["auth"])) > 100:
        raise ValueError("that subscription is too long")
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO web_push_subscriptions (user_id, restaurant_id, endpoint, p256dh, auth, platform) "
                     "VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, "
                     "restaurant_id=excluded.restaurant_id, p256dh=excluded.p256dh, auth=excluded.auth, "
                     "platform=excluded.platform, revoked_at=NULL, failures=0",
                     (int(user_id), restaurant_id, endpoint, str(keys["p256dh"]), str(keys["auth"]),
                      platform if platform in PLATFORMS else None))
        conn.commit()
    finally:
        conn.close()
    return {"subscribed": True}


def unsubscribe(user_id, endpoint, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE web_push_subscriptions SET revoked_at=datetime('now') WHERE user_id=? AND endpoint=? "
                           "AND revoked_at IS NULL", (int(user_id), str(endpoint or "")))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def subscribed_users(restaurant_id, user_ids, db_path=None) -> set:
    """The logins among `user_ids` with a live browser subscription here —
    people.reach's Web Push channel (empty while Web Push is off)."""
    import web_push
    ids = [int(u) for u in user_ids or [] if u]
    if not ids or not web_push.enabled():
        return set()
    conn = get_conn(db_path)
    try:
        return {r["user_id"] for r in conn.execute(
            f"SELECT DISTINCT user_id FROM web_push_subscriptions WHERE restaurant_id=? AND revoked_at IS NULL AND "
            f"user_id IN ({','.join('?' for _ in ids)})", [restaurant_id] + ids).fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def send_to_user(restaurant_id, user_id, title, body, tab=None, tag=None, db_path=None) -> bool:
    """Every live subscription of this login here; True when any push
    service accepted it. A gone subscription is revoked."""
    import web_push
    conn = get_conn(db_path)
    try:
        subs = [dict(r) for r in conn.execute(
            "SELECT * FROM web_push_subscriptions WHERE restaurant_id=? AND user_id=? AND revoked_at IS NULL",
            (restaurant_id, int(user_id))).fetchall()]
    finally:
        conn.close()
    ok = False
    for s in subs:
        res = web_push.send(s, {"title": str(title)[:120], "body": str(body)[:300], "tab": tab or "today",
                                "tag": tag})
        conn = get_conn(db_path)
        try:
            if res.get("ok"):
                ok = True
                conn.execute("UPDATE web_push_subscriptions SET last_ok_at=datetime('now'), failures=0 WHERE id=?",
                             (s["id"],))
            elif res.get("gone"):
                conn.execute("UPDATE web_push_subscriptions SET revoked_at=datetime('now') WHERE id=?", (s["id"],))
            else:
                conn.execute("UPDATE web_push_subscriptions SET failures=failures+1 WHERE id=?", (s["id"],))
            conn.commit()
        finally:
            conn.close()
    return ok
