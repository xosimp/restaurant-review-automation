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
  `installed_builds()` reads the live sessions: what is installed, today —
  how many app sessions still send no header, and the decode reports per
  build (`decode_reports_by_build`) on the console's Engineering page.
* **Retire.** `MOBILE_MIN_BUILD` (unset: no minimum) turns a lower build away
  on /mobile/api/* and /staff/api/* with 426 `{update_required: true,
  message}`; the app shows its blocking "Update Cavnar AI" sheet. A request
  with NO header is a build from before the header existed: allowed until
  `MOBILE_HEADERLESS_CUTOFF` (a date; unset: allowed for good), so the builds
  installed today keep working. The browser staff portal (/staff/api/* on its
  cookie, no Bearer) sends no header and is never app traffic. The 426 also
  carries `error`, the one field an old build shows from any refusal.
  A minimum above the newest build any live session has run in
  `MIN_BUILD_SEEN_DAYS` days (plus `MOBILE_MIN_BUILD_MARGIN`) would turn
  every installed phone away: it is checked at boot (`check_min_build`),
  ignored with a loud log line, and named in the platform check and on the
  console until it is fixed — unless `MOBILE_MIN_BUILD_FORCE=1` says it is
  meant (blind re-audit 10/27/26).
* **Hear.** A phone that could not decode an answer posts
  /mobile/api/client-errors: the route, the HTTP status, the DecodingError's
  coding path and description — never the body — and its build. Stored in
  `client_error_reports` (an ops ledger: 30 days, `ops._RETENTION_DAYS`),
  deduplicated per device + route + path for an hour and capped per device,
  per address and in total. A report must name a route the server really
  has (`known_route`). Only a report that came with a live session counts
  toward an alert: more than `CLIENT_DECODE_ALERT_USERS` distinct signed-in
  users in an hour is a line in the platform check (`ops.check_platform_sla`)
  — a fixed sentence and counts, never a reported string, under one fixed
  page key. A report with no session is stored (a sign-in that cannot be
  decoded must still be heard) on its own, smaller budget, and never pages
  (blind re-audit 10/27/26: four made-up device ids paged the operator
  with attacker-chosen text, under a fresh key per route).
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


# The post-deploy smoke test's mobile read (scripts/smoke.py) names itself
# "smoke (999999999)": above any minimum, so a cutoff or a minimum never
# fails a deploy's smoke — and never kept as an installed build, so it
# cannot vouch for a minimum (check_min_build) or skew the console.
SMOKE_VERSION = "smoke"


class ClientApp(NamedTuple):
    version: str
    build: int
    platform: str

    @property
    def label(self) -> str:
        """'ios 1.0 (4210)' — what a session and a report keep."""
        return f"{self.platform} {self.version} ({self.build})"

    @property
    def is_smoke(self) -> bool:
        return self.version.lower() == SMOKE_VERSION


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


def _raw_min_build(environ=None) -> Optional[int]:
    raw = str(_env(environ).get("MOBILE_MIN_BUILD") or "").strip()
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        log.warning("MOBILE_MIN_BUILD=%r is not a build number; no minimum is enforced", raw)
        return None
    return n if n > 0 else None


def min_build(environ=None) -> Optional[int]:
    """MOBILE_MIN_BUILD as an int, or None (no minimum). A value that is not
    a whole number is ignored with a warning — never a reason to refuse
    every phone. Read from this process's environment (`environ` None), a
    value the sanity check (`check_min_build`, at boot and on every platform
    check) found above every build installed is ignored too."""
    n = _raw_min_build(environ)
    if n is not None and environ is None:
        verdict = _MIN_BUILD_CHECK
        if verdict.get("ignored") and verdict.get("raw") == n and verdict.get("db") == _db_key():
            return None
    return n


def _db_key(db_path=None):
    """Which database a verdict was read from: the process's own (None)
    unless a caller named another — a test's check never decides for the
    process."""
    return db_path or getattr(_models_mod, "DB_PATH", None)


# How far back a session's build counts as installed, for the minimum's
# sanity check: phones update over days, and a session row lives on while
# its phone is in use.
MIN_BUILD_SEEN_DAYS = 7

# The last sanity check of MOBILE_MIN_BUILD in this process (check_min_build):
# {raw, enforced, ignored, newest, why}. Empty until one ran — a minimum is
# enforced as set until then.
_MIN_BUILD_CHECK = {}
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("client_compat._MIN_BUILD_CHECK", _MIN_BUILD_CHECK)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def min_build_verdict(minimum, newest, margin=0, force=False):
    """(the minimum to enforce, why it is not) for MOBILE_MIN_BUILD=`minimum`
    when the newest build seen on a live session is `newest` (None: no
    session sent a build). Pure. A minimum above `newest` + `margin` turns
    away every phone that names its build — a typed digit too many, or
    main's commit count where the store build came from a release branch —
    and nothing can be updated to: it is ignored unless `force`."""
    if minimum is None:
        return None, None
    if force:
        return minimum, None
    if newest is None:
        return None, (f"MOBILE_MIN_BUILD={minimum} is ignored: no live session in the last {MIN_BUILD_SEEN_DAYS} days "
                      "sent a build number, so nothing shows it could be met. Set MOBILE_MIN_BUILD_FORCE=1 if it "
                      "is meant.")
    if minimum > newest + max(0, int(margin or 0)):
        return None, (f"MOBILE_MIN_BUILD={minimum} is ignored: it is above the newest build any live session ran in "
                      f"the last {MIN_BUILD_SEEN_DAYS} days ({newest}"
                      + (f", plus a margin of {int(margin)}" if margin else "")
                      + "), so it would turn every installed phone away. Fix the value, or set "
                      "MOBILE_MIN_BUILD_FORCE=1 if it is meant.")
    return minimum, None


def newest_seen_build(db_path=None, days=None) -> Optional[int]:
    """The highest build number a session (or a sign-in) reported in the last
    `days` days, or None when none sent one."""
    days = MIN_BUILD_SEEN_DAYS if days is None else int(days)
    since = f"datetime('now', '-{int(days)} days')"
    conn = get_conn(db_path)
    try:
        labels = [r[0] for r in conn.execute(
            "SELECT DISTINCT client_app FROM sessions WHERE client_app IS NOT NULL AND client_app <> '' "
            f"AND last_active >= {since}").fetchall()]
        labels += [r[0] for r in conn.execute(
            "SELECT DISTINCT client_app FROM login_history WHERE client_app IS NOT NULL AND client_app <> '' "
            f"AND created_at >= {since}").fetchall()]
    finally:
        conn.close()
    builds = [b for b in (_label_build(x) for x in labels if f" {SMOKE_VERSION} (" not in str(x).lower())
              if b is not None]
    return max(builds) if builds else None


def check_min_build(db_path=None, environ=None) -> dict:
    """Checks MOBILE_MIN_BUILD against the builds actually installed and
    keeps the verdict for `min_build` (this process's environment only). Run
    at boot (hosted_dashboard) and on every platform check, so a minimum the
    fleet catches up with starts to apply without a restart. A refused value
    is logged as an error once per verdict, and the platform check and the
    console name it (`min_build_problem`). Never raises."""
    env = _env(environ)
    raw = _raw_min_build(environ)
    verdict = {"raw": raw, "enforced": raw, "ignored": False, "newest": None, "why": None, "db": _db_key(db_path)}
    if raw is not None:
        try:
            newest = newest_seen_build(db_path)
        except Exception as e:      # a check that cannot read never refuses phones on its own
            log.error("MOBILE_MIN_BUILD=%s could not be checked against the installed builds (%s); "
                      "it is enforced as set", raw, e)
            newest = raw
        enforced, why = min_build_verdict(raw, newest, margin=_int_env("MOBILE_MIN_BUILD_MARGIN", 0, env),
                                          force=_truthy(env.get("MOBILE_MIN_BUILD_FORCE")))
        verdict.update(enforced=enforced, ignored=enforced is None, newest=newest, why=why)
    if environ is None:
        previous = dict(_MIN_BUILD_CHECK)
        _MIN_BUILD_CHECK.clear()
        _MIN_BUILD_CHECK.update(verdict)
        if verdict["ignored"] and (previous.get("raw"), previous.get("ignored")) != (raw, True):
            log.error("%s", verdict["why"])
    return verdict


def min_build_problem() -> Optional[str]:
    """The platform check's and the console's sentence while this process's
    minimum is being ignored, or None."""
    v = _MIN_BUILD_CHECK
    if v.get("ignored") and v.get("db") == _db_key() and v.get("raw") == _raw_min_build():
        return v.get("why")
    return None


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


# ── cohort switches (rollout — blind re-audit 10/27/26 RF6, B4/B5 P2) ──────
# Each refusal rule is a rollout feature, ON for everyone by default (today's
# behaviour). A console flag (POST /admin/api/rollout) narrows it at run time
# with no deploy — percent 0 is the kill switch, 10 retires a build at a
# tenth of the restaurants first — and ROLLOUT_<FEATURE>=off is the
# deploy-time one. Consulted only when a refusal is about to be sent.
FEATURE_MIN_BUILD = "client_min_build"
FEATURE_HEADERLESS = "client_headerless_cutoff"
# The web dashboard's "Cavnar AI was updated" bar (_csrf_fetch.html, A06-13).
FEATURE_RELOAD_BAR = "stale_tab_reload"


def _session_restaurant_id(headers):
    """The restaurant of the session the request's Bearer token names, or
    None (no token, a dead one, an unreadable store — logged)."""
    raw = str((headers or {}).get("Authorization") or "")
    token = raw[7:].strip() if raw.startswith("Bearer ") else ""
    if not token:
        return None
    try:
        import auth
        user = auth.get_session_user(token)
    except Exception as e:
        log.warning("client gate: the session behind a refusal could not be read: %s", e)
        return None
    rid = (user or {}).get("restaurant_id")
    return int(rid) if rid is not None else None


def refusal_allowed(headers) -> bool:
    """Whether the rule about to refuse this request is on for its
    restaurant's cohort (rollout.enabled, default ON). A request whose
    restaurant is unknown (no session yet) is refused only at 100%."""
    import rollout
    feature = FEATURE_MIN_BUILD if from_headers(headers) is not None else FEATURE_HEADERLESS
    return rollout.enabled(feature, _session_restaurant_id(headers), default=True)


def page_build():
    """The build a dashboard page is rendered with (`cavnar_build`, which
    the stale-tab bar compares with each answer's X-Cavnar-Build), or "" —
    no bar — when the viewer's restaurant is outside FEATURE_RELOAD_BAR's
    cohort. Default ON."""
    import config
    build = config.build_id()
    if not build:
        return ""
    rid = None
    try:
        from flask import g, has_request_context
        if has_request_context():
            rid = (getattr(g, "cavnar_current_user", None) or {}).get("restaurant_id")
    except ImportError:
        rid = None
    import rollout
    return build if rollout.enabled(FEATURE_RELOAD_BAR, rid, default=True) else ""


def gate():
    """App-level before_request (install): a build below the minimum, or a
    headerless one past the cutoff, gets 426 before any route runs — when
    its rule is on for the request's restaurant (refusal_allowed)."""
    from flask import jsonify, request
    body = update_refusal(request.path, request.method, request.headers)
    if body is None or not refusal_allowed(request.headers):
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
    return app.label if app and not app.is_smoke else None


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
    header = sum(b["sessions"] for b in builds)
    total = header + headerless
    return {"builds": builds, "headerless_sessions": headerless, "header_sessions": header,
            # The share of live app sessions on a build from before the
            # header: until it is small, those phones are guarded only by the
            # frozen v1 contracts (tests/test_ios_api_v1.py,
            # test_ios_response_shapes) — they send no build and no report.
            "headerless_share_pct": round(100.0 * headerless / total, 1) if total else None,
            "below_minimum": below, "min_build": minimum, "min_build_set": _raw_min_build(),
            "min_build_problem": min_build_problem(),
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


def _int_env(name, default, environ=None):
    try:
        return max(0, int((os.environ if environ is None else environ).get(name, str(default))))
    except ValueError:
        return default


# Per device, per signed-in user and per address an hour (the phone itself
# sends one per route an hour), and for everyone an hour: a flood fills
# nothing past these.
DEVICE_HOURLY = _int_env("CLIENT_ERROR_DEVICE_HOURLY", 20)
USER_HOURLY = _int_env("CLIENT_ERROR_USER_HOURLY", 30)
IP_HOURLY = _int_env("CLIENT_ERROR_IP_HOURLY", 60)
GLOBAL_HOURLY = _int_env("CLIENT_ERROR_GLOBAL_HOURLY", 2000)
# A report with no live session (a sign-in that could not be decoded, or
# anyone at all): its own, smaller budgets per address and in total, so
# junk can neither crowd out signed-in reports nor fill the table.
ANON_IP_HOURLY = _int_env("CLIENT_ERROR_ANON_IP_HOURLY", 10)
ANON_GLOBAL_HOURLY = _int_env("CLIENT_ERROR_ANON_GLOBAL_HOURLY", 200)
# Distinct SIGNED-IN users reporting a decode failure within an hour that
# make the platform check say so. Device ids are the client's own words and
# never count (CLIENT_DECODE_ALERT_DEVICES is read as before, for an old
# setting).
ALERT_USERS = _int_env("CLIENT_DECODE_ALERT_USERS", _int_env("CLIENT_DECODE_ALERT_DEVICES", 3))

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


def known_route(raw, method=None, app=None) -> bool:
    """Whether `raw` (a path, query allowed) is a route the server really
    has — any method: a report says which call failed, and a method the rule
    does not take is still that rule. `app` defaults to the current Flask
    app; outside one nothing can be checked and the answer is True."""
    path = str(raw or "").strip().split("?", 1)[0].split("#", 1)[0][:MAX_ROUTE]
    if not path.startswith(GATED_PREFIXES):
        return False
    if app is None:
        try:
            from flask import current_app, has_app_context
        except ImportError:
            return True
        if not has_app_context():
            return True
        app = current_app
    from werkzeug.exceptions import MethodNotAllowed, NotFound
    from werkzeug.routing import RequestRedirect
    verb = str(method or "GET").upper()
    try:
        app.url_map.bind("localhost").match(path, method=verb if verb in METHODS else "GET")
    except (MethodNotAllowed, RequestRedirect):
        return True
    except NotFound:
        return False
    return True


def record_report(route=None, method=None, status=None, coding_path=None, description=None, kind=None,
                  device_id=None, user=None, ip=None, client=None, known=None, db_path=None):
    """(answer, HTTP status) for one decode-failure report. `user` is the
    session that sent it when its token was live (owner or staff), `client`
    the ClientApp its headers name, `known` whether the route matched one of
    the server's rules (`known_route`; None: not checked). Never stores a
    body: only the route, the status, the coding path and the error's
    description. A report with no live session is kept on the anonymous
    budgets and never counts toward an alert (`decode_failure_surge`)."""
    norm = normalise_route(route)
    if norm is None or known is False:
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
        counts = [
            (conn.execute("SELECT COUNT(*) FROM client_error_reports WHERE device_key=? "
                          f"AND created_at >= {hour}", (device_key,)).fetchone()[0], DEVICE_HOURLY),
            (conn.execute("SELECT COUNT(*) FROM client_error_reports WHERE ip_key=? "
                          f"AND created_at >= {hour}", (ip_key,)).fetchone()[0], IP_HOURLY),
            (conn.execute(f"SELECT COUNT(*) FROM client_error_reports WHERE created_at >= {hour}"
                          ).fetchone()[0], GLOBAL_HOURLY),
        ]
        if uid is not None:
            counts.append((conn.execute("SELECT COUNT(*) FROM client_error_reports WHERE user_id=? "
                                        f"AND created_at >= {hour}", (int(uid),)).fetchone()[0], USER_HOURLY))
        else:
            counts.append((conn.execute("SELECT COUNT(*) FROM client_error_reports WHERE ip_key=? "
                                        f"AND user_id IS NULL AND created_at >= {hour}", (ip_key,)).fetchone()[0],
                           ANON_IP_HOURLY))
            counts.append((conn.execute("SELECT COUNT(*) FROM client_error_reports WHERE user_id IS NULL "
                                        f"AND created_at >= {hour}").fetchone()[0], ANON_GLOBAL_HOURLY))
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
    {users, restaurants, devices, anonymous_devices, threshold, over,
    routes: [{route, users, devices, reports, coding_path, client_app}]}.
    `over` when more than `threshold` distinct SIGNED-IN users reported:
    a report with no live session, and a device id (the client's own word),
    never counts toward it."""
    threshold = ALERT_USERS if threshold is None else threshold
    since = f"datetime('now', '-{int(hours)} hours')"
    conn = get_conn(db_path)
    try:
        totals = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS users, COUNT(DISTINCT restaurant_id) AS restaurants, "
            "COUNT(DISTINCT device_key) AS devices FROM client_error_reports "
            f"WHERE created_at >= {since}").fetchone()
        anonymous = conn.execute(
            "SELECT COUNT(DISTINCT device_key) FROM client_error_reports WHERE user_id IS NULL "
            f"AND created_at >= {since}").fetchone()[0]
        rows = conn.execute(
            "SELECT route, COUNT(DISTINCT user_id) AS users, COUNT(DISTINCT device_key) AS devices, "
            "SUM(repeats) AS reports, MAX(coding_path) AS coding_path, MAX(client_app) AS client_app "
            f"FROM client_error_reports WHERE created_at >= {since} "
            "GROUP BY route ORDER BY COUNT(DISTINCT user_id) DESC, COUNT(DISTINCT device_key) DESC, route "
            "LIMIT 10").fetchall()
    finally:
        conn.close()
    users = int(totals["users"] or 0)
    routes = [{"route": r["route"], "users": int(r["users"] or 0), "devices": int(r["devices"] or 0),
               "reports": int(r["reports"] or 0), "coding_path": r["coding_path"], "client_app": r["client_app"]}
              for r in rows]
    return {"users": users, "restaurants": int(totals["restaurants"] or 0), "devices": int(totals["devices"] or 0),
            "anonymous_devices": int(anonymous or 0), "threshold": threshold, "over": users > threshold,
            "routes": routes}


def surge_problem(surge) -> Optional[str]:
    """The platform check's sentence for a surge, or None: a fixed sentence
    and counts. Never a reported route, coding path or description — those
    are the client's words, and this sentence is texted and emailed to the
    operator; they are on the console (client_error_reports)."""
    if not surge or not surge.get("over"):
        return None
    users, rest, n = int(surge.get("users") or 0), int(surge.get("restaurants") or 0), len(surge.get("routes") or [])
    return (f"The iPhone app could not read the server's answer for {users} signed-in user{'s' if users != 1 else ''} "
            f"at {rest} restaurant{'s' if rest != 1 else ''} in the last hour, on {n} route{'s' if n != 1 else ''} "
            f"(alert past {surge.get('threshold')}) — a response changed shape under a shipped build. The routes "
            "and coding paths are on the console (Engineering, iPhone app; client_error_reports).")


def decode_reports_by_build(hours=24 * 7, db_path=None) -> list:
    """Decode reports per app build over the last `hours` (the console's
    compatibility read): [{client_app, reports, users, devices, routes}],
    most reports first; a report with no build header is "no build header"."""
    since = f"datetime('now', '-{int(hours)} hours')"
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT COALESCE(client_app, '') AS client_app, SUM(repeats) AS reports, "
            "COUNT(DISTINCT user_id) AS users, COUNT(DISTINCT device_key) AS devices, "
            "COUNT(DISTINCT route) AS routes FROM client_error_reports "
            f"WHERE created_at >= {since} GROUP BY COALESCE(client_app, '') "
            "ORDER BY SUM(repeats) DESC, COALESCE(client_app, '') LIMIT 20").fetchall()
    finally:
        conn.close()
    return [{"client_app": r["client_app"] or "no build header", "reports": int(r["reports"] or 0),
             "users": int(r["users"] or 0), "devices": int(r["devices"] or 0), "routes": int(r["routes"] or 0)}
            for r in rows]
