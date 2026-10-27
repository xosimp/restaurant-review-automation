"""
client_compat.py — which app builds talk to the server, turning away the ones
too old to read its answers, and hearing from a phone that could not read one
(release-safety audit #25: A06-02, A06-04, A05-14).

Every request from the iPhone app (owner tier and staff tier, and the share
extension) carries two headers:

    X-Cavnar-App: 1.0 (4210)        CFBundleShortVersionString (CFBundleVersion)
    X-Cavnar-Platform: ios

The build number is `git rev-list --count HEAD` at generate time
(ios/CavnarAI/generate.sh), so it only ever rises.

* **Identify.** The label ("ios 1.0 (4210)") is kept per login: on the
  session row and its login_history row when it is created (auth.create_session),
  and refreshed on the session by the once-a-minute last_active touch
  (auth.get_session_user) — so an upgrade without a new sign-in is seen too.
  `installed_builds()` reads the live sessions: what is installed, today.
* **Retire.** `MOBILE_MIN_BUILD` (unset: no minimum) turns a lower build away
  on /mobile/api/* and /staff/api/* with 426 `{update_required: true,
  message}`; the app shows its blocking "Update Cavnar AI" sheet. A request
  with NO header is a build from before the header existed: allowed until
  `MOBILE_HEADERLESS_CUTOFF` (a date; unset: allowed for good), so the builds
  installed today keep working. The browser staff portal (/staff/api/* on its
  cookie, no Bearer) sends no header and is never app traffic. The 426 also
  carries `error`, the one field an old build shows from any refusal.
* **Hear.** A phone that could not decode an answer posts
  /mobile/api/client-errors: the route, the HTTP status, the DecodingError's
  coding path and description — never the body — and its build. Stored in
  `client_error_reports` (an ops ledger: 30 days, `ops._RETENTION_DAYS`),
  deduplicated per device + route + path for an hour and capped per device,
  per address and in total. More than `CLIENT_DECODE_ALERT_DEVICES` distinct
  devices in an hour is a line in the platform check
  (`ops.check_platform_sla`), naming the routes.
"""
import hashlib
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import NamedTuple, Optional

import models as _models_mod

log = logging.getLogger("client_compat")


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, "Bound imports")."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


APP_HEADER = "X-Cavnar-App"
PLATFORM_HEADER = "X-Cavnar-Platform"
# The API prefixes the gate guards: the owner app's and the staff app's.
GATED_PREFIXES = ("/mobile/api/", "/staff/api/")
REPORT_PATH = "/mobile/api/client-errors"
# A build too old for everything else may still say what it could not read.
EXEMPT_PATHS = frozenset({REPORT_PATH})
UPDATE_MESSAGE = ("This version of Cavnar AI is too old to keep working. "
                  "Update it in the App Store to carry on.")
UPDATE_STATUS = 426

# "1.0 (4210)": a marketing version, then the build number in brackets.
_APP_RE = re.compile(r"^\s*([0-9A-Za-z][0-9A-Za-z.\-]{0,23})\s*\(\s*(\d{1,9})\s*\)\s*$")
_PLATFORMS = ("ios",)


class ClientApp(NamedTuple):
    version: str
    build: int
    platform: str

    @property
    def label(self) -> str:
        """'ios 1.0 (4210)' — what a session and a report keep."""
        return f"{self.platform} {self.version} ({self.build})"


def parse_app(value, platform="") -> Optional[ClientApp]:
    """The X-Cavnar-App value as a ClientApp, or None when it is missing or
    not "<version> (<build>)". `platform` is X-Cavnar-Platform's; anything
    but a known platform reads as "ios" (the only app), so a build that sent
    the version header is never mistaken for a browser."""
    m = _APP_RE.match(str(value or ""))
    if not m:
        return None
    plat = str(platform or "").strip().lower()
    return ClientApp(m.group(1), int(m.group(2)), plat if plat in _PLATFORMS else "ios")


def from_headers(headers) -> Optional[ClientApp]:
    """The app a request says it is (any mapping with .get), or None."""
    if headers is None:
        return None
    return parse_app(headers.get(APP_HEADER), headers.get(PLATFORM_HEADER))


def _env(environ):
    return os.environ if environ is None else environ


def min_build(environ=None) -> Optional[int]:
    """MOBILE_MIN_BUILD as an int, or None (no minimum). A value that is not
    a whole number is ignored with a warning — never a reason to refuse
    every phone."""
    raw = str(_env(environ).get("MOBILE_MIN_BUILD") or "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        log.warning("MOBILE_MIN_BUILD=%r is not a build number; no minimum is enforced", raw)
        return None
    return n if n > 0 else None


def headerless_cutoff(environ=None) -> Optional[date]:
    """MOBILE_HEADERLESS_CUTOFF (YYYY-MM-DD): the last UTC day a request
    with no X-Cavnar-App header is served. None (unset, or not a date — with
    a warning) serves them for good."""
    raw = str(_env(environ).get("MOBILE_HEADERLESS_CUTOFF") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        log.warning("MOBILE_HEADERLESS_CUTOFF=%r is not a YYYY-MM-DD date; headerless builds stay allowed", raw)
        return None


def app_store_url(environ=None) -> Optional[str]:
    """Where the update is: IOS_APP_STORE_URL once the app is listed."""
    url = str(_env(environ).get("IOS_APP_STORE_URL") or "").strip()
    return url or None


def _bearer(headers) -> bool:
    return str((headers or {}).get("Authorization") or "").startswith("Bearer ")


def is_app_traffic(path, headers) -> bool:
    """A request the gate judges: /mobile/api/* (only the app calls it), or
    /staff/api/* carrying a Bearer token (the app's staff tier — the browser
    portal calls the same routes on its cookie, with no header to send) or
    the version header itself."""
    path = str(path or "")
    if path.startswith("/mobile/api/"):
        return True
    if path.startswith("/staff/api/"):
        return _bearer(headers) or from_headers(headers) is not None
    return False


def update_body(minimum=None, environ=None) -> dict:
    """The 426 answer. `error` is what every shipped build shows from a
    refusal; `update_required` is what this build's sheet opens on."""
    return {"ok": False, "update_required": True, "error": UPDATE_MESSAGE, "message": UPDATE_MESSAGE,
            "min_build": minimum, "app_store_url": app_store_url(environ)}


def update_refusal(path, method, headers, today=None, environ=None) -> Optional[dict]:
    """The 426 body for this request, or None to serve it. Pure, so the rule
    is tested without an app: `today` is the UTC date (the cutoff's)."""
    if str(method or "").upper() == "OPTIONS":
        return None
    path = str(path or "")
    if not path.startswith(GATED_PREFIXES) or path in EXEMPT_PATHS:
        return None
    if not is_app_traffic(path, headers):
        return None
    minimum = min_build(environ)
    app = from_headers(headers)
    if app is None:
        cutoff = headerless_cutoff(environ)
        if cutoff is None:
            return None
        today = today or datetime.now(timezone.utc).date()
        return update_body(minimum, environ) if today > cutoff else None
    if app.platform != "ios" or minimum is None:
        return None
    return update_body(minimum, environ) if app.build < minimum else None


def gate():
    """App-level before_request (install): a build below the minimum, or a
    headerless one past the cutoff, gets 426 before any route runs."""
    from flask import jsonify, request
    body = update_refusal(request.path, request.method, request.headers)
    if body is None:
        return None
    resp = jsonify(**body)
    resp.status_code = UPDATE_STATUS
    resp.headers["Cache-Control"] = "no-store"
    return resp


def install(app):
    """Wire the gate onto `app` once (hosted_dashboard; a test's own app)."""
    if not getattr(app, "_cavnar_client_gate", False):
        app.before_request(gate)
        app._cavnar_client_gate = True
    return app


def request_client_app() -> Optional[str]:
    """The label of the app the current request says it is ('ios 1.0
    (4210)'), or None outside a request or without the header — what
    auth.create_session and the session touch keep."""
    try:
        from flask import has_request_context, request
    except ImportError:
        return None
    if not has_request_context():
        return None
    app = from_headers(request.headers)
    return app.label if app else None


# ── what is installed ───────────────────────────────────────────────────────

def _label_build(label) -> Optional[int]:
    m = re.search(r"\((\d+)\)\s*$", str(label or ""))
    return int(m.group(1)) if m else None


def installed_builds(db_path=None) -> dict:
    """The app builds on live sessions: [{client_app, build, device_type,
    sessions, last_active}] newest build first, the headerless sessions
    (builds from before the header, or a browser's staff portal), and how
    many live app sessions sit below MOBILE_MIN_BUILD."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT COALESCE(client_app, '') AS client_app, device_type, COUNT(*) AS n, "
            "MAX(last_active) AS last_active FROM sessions "
            "WHERE device_type IN ('ios', 'staff_pin') AND expires_at > datetime('now') "
            "GROUP BY COALESCE(client_app, ''), device_type").fetchall()
    finally:
        conn.close()
    minimum = min_build()
    builds, headerless, below = [], 0, 0
    for r in rows:
        label, n = r["client_app"], int(r["n"] or 0)
        if not label:
            headerless += n
            continue
        build = _label_build(label)
        if minimum is not None and build is not None and build < minimum:
            below += n
        builds.append({"client_app": label, "build": build, "device_type": r["device_type"], "sessions": n,
                       "last_active": r["last_active"]})
    builds.sort(key=lambda b: (-(b["build"] or 0), b["device_type"]))
    cutoff = headerless_cutoff()
    return {"builds": builds, "headerless_sessions": headerless, "below_minimum": below, "min_build": minimum,
            "headerless_cutoff": cutoff.isoformat() if cutoff else None}


# ── decode-failure reports ──────────────────────────────────────────────────

_REPORTS_SQL = """CREATE TABLE IF NOT EXISTS client_error_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    restaurant_id INTEGER REFERENCES restaurants(id),
    user_id       INTEGER,
    device_key    TEXT    NOT NULL,
    ip_key        TEXT,
    platform      TEXT,
    client_app    TEXT,
    app_build     INTEGER,
    route         TEXT    NOT NULL,
    method        TEXT,
    http_status   INTEGER,
    kind          TEXT    NOT NULL DEFAULT 'decoding',
    coding_path   TEXT,
    description   TEXT,
    repeats       INTEGER NOT NULL DEFAULT 1
)"""
_REPORT_INDEXES = (
    # The retention prune's column (ops._RETENTION_COLUMN).
    "CREATE INDEX IF NOT EXISTS idx_client_error_reports_created ON client_error_reports(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_client_error_reports_device ON client_error_reports(device_key, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_client_error_reports_ip ON client_error_reports(ip_key, created_at)",
)


def init_client_compat(db_path=None):
    """client_error_reports and its indexes, at boot (models.init_db) —
    never on the report's own path."""
    conn = get_conn(db_path)
    try:
        conn.execute(_REPORTS_SQL)
        for sql in _REPORT_INDEXES:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


def _int_env(name, default):
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# Per device and per address an hour (the phone itself sends one per route
# an hour), and for everyone an hour: a flood fills nothing past these.
DEVICE_HOURLY = _int_env("CLIENT_ERROR_DEVICE_HOURLY", 20)
IP_HOURLY = _int_env("CLIENT_ERROR_IP_HOURLY", 60)
GLOBAL_HOURLY = _int_env("CLIENT_ERROR_GLOBAL_HOURLY", 2000)
# Distinct devices reporting a decode failure within an hour that make the
# platform check say so.
ALERT_DEVICES = _int_env("CLIENT_DECODE_ALERT_DEVICES", 3)

KINDS = ("decoding",)
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_ROUTE, MAX_PATH, MAX_DESCRIPTION = 200, 300, 500
# An id in a path is not part of the route: /mobile/api/dsr/2026-10-25 and
# /mobile/api/reviews/412 are one route each.
_ID_SEGMENT = re.compile(r"^(?:\d+|\d{4}-\d{2}-\d{2}|[0-9a-f]{16,}|[0-9a-fA-F-]{32,36})$")


def _key(kind, raw) -> str:
    return hashlib.sha256(f"cavnar-client-error:{kind}:{raw}".encode("utf-8")).hexdigest()[:32]


def normalise_route(raw) -> Optional[str]:
    """The route a report names, without its query and with ids as `:id`;
    None for anything that is not one of the app's API paths."""
    path = str(raw or "").strip().split("?", 1)[0].split("#", 1)[0][:MAX_ROUTE]
    if not path.startswith(GATED_PREFIXES):
        return None
    parts = [(":id" if _ID_SEGMENT.match(p) else p) for p in path.split("/")]
    return "/".join(parts)


def _clip(value, limit) -> Optional[str]:
    text = " ".join(str(value).split()) if value is not None else ""
    return text[:limit] or None


def _status(value) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 999 else None


def record_report(route=None, method=None, status=None, coding_path=None, description=None, kind=None,
                  device_id=None, user=None, ip=None, client=None, db_path=None):
    """(answer, HTTP status) for one decode-failure report. `user` is the
    session that sent it when its token was live (owner or staff), `client`
    the ClientApp its headers name. Never stores a body: only the route, the
    status, the coding path and the error's description."""
    norm = normalise_route(route)
    if norm is None:
        return {"ok": False, "error": "That isn't one of the app's routes."}, 400
    device_raw = _clip(device_id, 100)
    ip_key = _key("ip", ip or "unknown")
    device_key = _key("device", device_raw) if device_raw else "ip:" + ip_key
    rid = (user or {}).get("restaurant_id") if user else None
    uid = (user or {}).get("id") if user else None
    row = {
        "route": norm,
        "method": str(method or "").upper() if str(method or "").upper() in METHODS else None,
        "http_status": _status(status),
        "kind": kind if kind in KINDS else "decoding",
        "coding_path": _clip(coding_path, MAX_PATH),
        "description": _clip(description, MAX_DESCRIPTION),
    }
    conn = get_conn(db_path)
    try:
        hour = "datetime('now', '-1 hours')"
        same = conn.execute(
            "SELECT id FROM client_error_reports WHERE device_key=? AND route=? AND COALESCE(coding_path, '')=? "
            f"AND COALESCE(restaurant_id, 0)=? AND created_at >= {hour} ORDER BY id DESC LIMIT 1",
            (device_key, norm, row["coding_path"] or "", int(rid or 0))).fetchone()
        if same is not None:
            conn.execute("UPDATE client_error_reports SET repeats=repeats+1, last_at=datetime('now') "
                         "WHERE id=? AND COALESCE(restaurant_id, 0)=?", (same["id"], int(rid or 0)))
            conn.commit()
            return {"ok": True, "stored": False, "repeat": True}, 200
        counts = (
            (conn.execute("SELECT COUNT(*) FROM client_error_reports WHERE device_key=? "
                          f"AND created_at >= {hour}", (device_key,)).fetchone()[0], DEVICE_HOURLY),
            (conn.execute("SELECT COUNT(*) FROM client_error_reports WHERE ip_key=? "
                          f"AND created_at >= {hour}", (ip_key,)).fetchone()[0], IP_HOURLY),
            (conn.execute(f"SELECT COUNT(*) FROM client_error_reports WHERE created_at >= {hour}"
                          ).fetchone()[0], GLOBAL_HOURLY),
        )
        if any(int(n or 0) >= cap for n, cap in counts):
            return {"ok": False, "rate_limited": True, "error": "Too many reports from here for now."}, 429
        conn.execute(
            "INSERT INTO client_error_reports (restaurant_id, user_id, device_key, ip_key, platform, client_app, "
            "app_build, route, method, http_status, kind, coding_path, description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, uid, device_key, ip_key, client.platform if client else None, client.label if client else None,
             client.build if client else None, norm, row["method"], row["http_status"], row["kind"],
             row["coding_path"], row["description"]))
        conn.commit()
    finally:
        conn.close()
    log.warning("client decode failure: %s %s %s at %s (%s)", row["method"] or "", norm, row["http_status"],
                row["coding_path"] or "$", client.label if client else "no build header")
    return {"ok": True, "stored": True}, 200


def decode_failure_surge(hours=1, threshold=None, db_path=None) -> dict:
    """The last `hours` of decode reports, platform-wide (an operator read):
    {devices, threshold, over, routes: [{route, devices, reports, coding_path,
    builds}]} — `over` when more than `threshold` distinct devices reported."""
    threshold = ALERT_DEVICES if threshold is None else threshold
    since = f"datetime('now', '-{int(hours)} hours')"
    conn = get_conn(db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(DISTINCT device_key) FROM client_error_reports WHERE created_at >= {since}"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT route, COUNT(DISTINCT device_key) AS devices, SUM(repeats) AS reports, "
            "MAX(coding_path) AS coding_path, MAX(client_app) AS client_app "
            f"FROM client_error_reports WHERE created_at >= {since} "
            "GROUP BY route ORDER BY COUNT(DISTINCT device_key) DESC, route LIMIT 10").fetchall()
    finally:
        conn.close()
    total = int(total or 0)
    routes = [{"route": r["route"], "devices": int(r["devices"] or 0), "reports": int(r["reports"] or 0),
               "coding_path": r["coding_path"], "client_app": r["client_app"]} for r in rows]
    return {"devices": total, "threshold": threshold, "over": total > threshold, "routes": routes}


def surge_problem(surge) -> Optional[str]:
    """The platform check's sentence for a surge, or None."""
    if not surge or not surge.get("over"):
        return None
    parts = [f"{r['route']} ({r['devices']} device{'s' if r['devices'] != 1 else ''}"
             + (f", at {r['coding_path']}" if r.get("coding_path") else "") + ")"
             for r in (surge.get("routes") or [])[:4]]
    return (f"The iPhone app could not read the server's answer on {surge['devices']} devices in the last hour "
            f"(alert past {surge['threshold']}): " + "; ".join(parts)
            + " — a response changed shape under a shipped build (client_error_reports).")
