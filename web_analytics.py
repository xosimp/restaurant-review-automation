"""web_analytics — the restaurant's own website and Google search, read daily
and judged against everything else Cavnar AI knows (owner, 10/2/26: Danny,
Simple EJ's marketer, asked whether Marketing reads the site's analytics and
catches trends, spikes and dips against the rest of the data).

Two sources, one read-only Google service account (no OAuth screen, no
password changes hands): the client adds our service-account email as a
Viewer on their Google Analytics 4 property and as a Restricted user in
Search Console, and pastes the property ID / site URL on Account →
Connections. GA_SERVICE_ACCOUNT_JSON holds the key (Railway only); with it
unset everything here is dormant and says so.

    GA4             visits, users, page views, engaged visits, visits by
                    channel, menu-page views, and clicks out to booking,
                    ordering and checkout sites (GA4's outbound-click event)
    Search Console  clicks, impressions and average position on Google, and
                    the top searches of the last 28 days

Stored per day in web_analytics_daily (long form: one row per metric); the
first sync backfills BACKFILL_DAYS, every later one re-reads RECENT_DAYS
(both sources restate recent days). No model is called anywhere here.

signals() judges each tracked metric's recent days against the median of
the same weekday over the BASELINE_WEEKS before (never a stand-in figure:
a weekday with fewer than MIN_BASELINE points is not judged), and 3-week
runs up or down; each spike or dip carries what else happened that day —
sales against the weekday's own typical night, a game or event, a post, the
reviews — as things that moved together, never as a cause.
"""
import base64
import json
import re
import logging
import os
import statistics
import threading
import time
from datetime import date, datetime, timedelta, timezone

import models as _models

log = logging.getLogger(__name__)

SERVICE_ACCOUNT_ENV = "GA_SERVICE_ACCOUNT_JSON"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GA4_URL = "https://analyticsdata.googleapis.com/v1beta/properties/{pid}:runReport"
GSC_URL = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
SCOPES = ("https://www.googleapis.com/auth/analytics.readonly "
          "https://www.googleapis.com/auth/webmasters.readonly")
HTTP_TIMEOUT = (5, 30)

BACKFILL_DAYS = 400        # GA4 and Search Console both keep at least this much
RECENT_DAYS = 10           # restated days re-read on every sync
QUERY_WINDOW_DAYS = 28
TOP_QUERIES = 25           # what the Website screen lists
# What is kept (10/7/26): the AI-visibility check asks AI the searches that
# really bring guests to the site, and the 25 the screen shows were nearly all
# the restaurant's own name. Search Console returns up to 25,000.
STORED_QUERIES = 500

BASELINE_WEEKS = 8
MIN_BASELINE = 6
LOOKBACK_DAYS = 7          # the recent days judged for a spike or a dip
SPIKE_RATIO = 1.5
DIP_RATIO = 0.6
TREND_WEEKS = 3
TREND_CHANGE = 0.15        # three falling (rising) weeks, the last 15%+ off the start

# What a tracked metric is called, and the smallest gap worth naming: a
# Tuesday with 6 visits against a typical 4 is +50% and means nothing.
TRACKED = (
    ("sessions", "Website visits", 25),
    ("booking_clicks", "Clicks to book a table", 5),
    ("ordering_clicks", "Clicks to order online", 5),
    ("checkout_clicks", "Clicks to checkout", 5),
    ("menu_views", "Menu page views", 15),
    ("search_clicks", "Clicks from Google search", 10),
    ("search_impressions", "Times you showed up on Google", 150),
)
LABELS = {k: label for k, label, _f in TRACKED}
FLOORS = {k: f for k, _l, f in TRACKED}
GSC_METRICS = ("search_clicks", "search_impressions", "search_position")

# Outbound domains by what a click on them means. Stripe stays "checkout"
# until a restaurant says what it sells there (EJ's: unknown, 10/2/26).
BOOKING_DOMAINS = ("exploretock.com", "tock.com", "opentable.com", "resy.com", "sevenrooms.com", "yelp.com/reservations")
ORDERING_DOMAINS = ("toasttab.com", "doordash.com", "ubereats.com", "grubhub.com", "chownow.com",
                    "order.online", "square.site", "olo.com", "slicelife.com")
CHECKOUT_DOMAINS = ("stripe.com", "buy.stripe.com", "checkout.stripe.com")

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_analytics_daily (
    restaurant_id INTEGER NOT NULL,
    source        TEXT    NOT NULL,          -- ga4 | gsc
    day           TEXT    NOT NULL,          -- YYYY-MM-DD, the property's own day
    metric        TEXT    NOT NULL,
    value         REAL    NOT NULL,
    fetched_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (restaurant_id, source, day, metric)
);
CREATE INDEX IF NOT EXISTS idx_web_analytics_daily_day ON web_analytics_daily(restaurant_id, day);
CREATE INDEX IF NOT EXISTS idx_web_analytics_daily_purge ON web_analytics_daily(day);   -- ops retention deletes by day
CREATE TABLE IF NOT EXISTS web_search_queries (
    restaurant_id INTEGER NOT NULL,
    window_end    TEXT    NOT NULL,
    query         TEXT    NOT NULL,
    clicks        REAL    NOT NULL DEFAULT 0,
    impressions   REAL    NOT NULL DEFAULT 0,
    position      REAL,
    PRIMARY KEY (restaurant_id, window_end, query)
);
"""


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path is not None else _models.get_conn()


def init_web_analytics(db_path=None):
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


class WebAnalyticsError(Exception):
    """A refusal in words an owner can act on (`code` for the client)."""

    def __init__(self, message, code="error"):
        super().__init__(message)
        self.message = message
        self.code = code


# ── the service account ─────────────────────────────────────────────────────

def service_account():
    """The key from GA_SERVICE_ACCOUNT_JSON (JSON, or base64 of it), or None."""
    raw = (os.getenv(SERVICE_ACCOUNT_ENV) or "").strip()
    if not raw:
        return None
    for text in (raw, None):
        try:
            if text is None:
                text = base64.b64decode(raw).decode("utf-8")
            sa = json.loads(text)
            if sa.get("client_email") and sa.get("private_key"):
                return sa
        except Exception:
            continue
    return None


def service_email():
    """The address a client adds in Google Analytics and Search Console."""
    sa = service_account()
    return sa.get("client_email") if sa else None


def configured() -> bool:
    return service_account() is not None


_token_cache = {}
_token_lock = threading.Lock()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _assertion(sa, now=None):
    """A signed JWT the token endpoint trades for an access token (RS256)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(now or time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if sa.get("private_key_id"):
        header["kid"] = sa["private_key_id"]
    claims = {"iss": sa["client_email"], "scope": SCOPES, "aud": TOKEN_URL, "iat": now, "exp": now + 3600}
    signing = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
               + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig = key.sign(signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing + "." + _b64url(sig)


def access_token():
    """A Google access token for the service account, cached until a minute
    before it expires (one process, one worker: a cache, never state)."""
    sa = service_account()
    if not sa:
        raise WebAnalyticsError("Website analytics isn't set up on this server yet.", "not_configured")
    with _token_lock:
        hit = _token_cache.get(sa["client_email"])
        if hit and hit[1] > time.time() + 60:
            return hit[0]
    import requests
    try:
        resp = requests.post(TOKEN_URL, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                              "assertion": _assertion(sa)}, timeout=HTTP_TIMEOUT)
        body = resp.json() if resp.content else {}
    except Exception as e:
        raise WebAnalyticsError("Google didn't answer the sign-in for website analytics. Try again shortly.",
                                "token_failed") from e
    tok = body.get("access_token") if resp.status_code == 200 else None
    if not tok:
        log.warning("web_analytics token refused: %s %s", resp.status_code, str(body.get("error"))[:120])
        raise WebAnalyticsError("Google refused Cavnar AI's analytics sign-in. The service-account key may "
                                "have been removed.", "token_failed")
    with _token_lock:
        _token_cache[sa["client_email"]] = (tok, time.time() + int(body.get("expires_in") or 3600))
    return tok


def _post(url, body, what):
    import requests
    try:
        resp = requests.post(url, json=body, headers={"Authorization": f"Bearer {access_token()}"},
                             timeout=HTTP_TIMEOUT)
    except WebAnalyticsError:
        raise
    except Exception as e:
        raise WebAnalyticsError(f"{what} didn't answer. Try again shortly.", "unreachable") from e
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code == 200:
        return data
    err = (data.get("error") or {}) if isinstance(data, dict) else {}
    status = (err.get("status") or "") if isinstance(err, dict) else ""
    reasons = {d.get("reason") for d in (err.get("details") or []) if isinstance(d, dict)} if isinstance(err, dict) else set()
    email = service_email() or "Cavnar AI's analytics address"
    if "SERVICE_DISABLED" in reasons:
        # Our Google Cloud project has this API switched off: nothing the
        # owner adds fixes it, so never tell them to add the address.
        log.warning("web_analytics %s API disabled in the Cloud project", what)
        raise WebAnalyticsError(f"Cavnar AI's {what} reading is switched off on our side. Nothing for you to "
                                "change; we've been told.", "api_disabled")
    if resp.status_code in (401, 403) or status == "PERMISSION_DENIED":
        where = ("Google Analytics → Admin → Property access management, as a Viewer" if what == "Google Analytics"
                 else "Search Console → Settings → Users and permissions, as a Restricted user")
        raise WebAnalyticsError(f"{what} hasn't let Cavnar AI in yet. Add {email} in {where}.", "no_access")
    if resp.status_code == 404 or status == "NOT_FOUND":
        raise WebAnalyticsError(f"{what} doesn't know that property. Check the ID or URL.", "not_found")
    if resp.status_code == 429:
        raise WebAnalyticsError(f"{what} is limiting requests. The next daily read tries again.", "rate_limited")
    log.warning("web_analytics %s refused: %s %s", what, resp.status_code, str(data)[:200])
    raise WebAnalyticsError(f"{what} refused the request ({resp.status_code}).", "refused")


# ── GA4 and Search Console reads ────────────────────────────────────────────

def clean_property_id(raw) -> str:
    """A GA4 property ID: digits only ("properties/412345678" and stray
    spaces accepted). '' when it isn't one (a G-XXXX measurement ID is not)."""
    text = str(raw or "").strip()
    if text.lower().startswith("properties/"):
        text = text.split("/", 1)[1]
    return text if text.isdigit() and 6 <= len(text) <= 14 else ""


def clean_site_url(raw) -> str:
    """A Search Console property: "sc-domain:example.com" or a URL prefix
    ending in "/". A bare domain ("simpleejs.com", what Danny sent 10/5/26)
    is the Domain property, Search Console's default kind. '' when it is
    none of these."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"(?:www\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", text):
        text = "sc-domain:" + (text[4:] if text.lower().startswith("www.") else text)
    if text.lower().startswith("sc-domain:"):
        host = text.split(":", 1)[1].strip().lower()
        return f"sc-domain:{host}" if host and "." in host and "/" not in host else ""
    if not text.lower().startswith(("http://", "https://")):
        return ""
    return text if text.endswith("/") else text + "/"


def ga4_report(property_id, start, end, dimensions, metrics, dim_filter=None, limit=100000):
    body = {"dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": d} for d in dimensions], "metrics": [{"name": m} for m in metrics],
            "limit": limit, "keepEmptyRows": False}
    if dim_filter:
        body["dimensionFilter"] = dim_filter
    data = _post(GA4_URL.format(pid=property_id), body, "Google Analytics")
    out = []
    for row in data.get("rows") or []:
        dims = [d.get("value") for d in row.get("dimensionValues") or []]
        vals = []
        for v in row.get("metricValues") or []:
            try:
                vals.append(float(v.get("value") or 0))
            except (TypeError, ValueError):
                vals.append(0.0)
        out.append((dims, vals))
    return out


def gsc_query(site_url, start, end, dimensions, limit=25000):
    from urllib.parse import quote
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": list(dimensions),
            "rowLimit": limit, "dataState": "final"}
    data = _post(GSC_URL.format(site=quote(site_url, safe="")), body, "Search Console")
    return data.get("rows") or []


def _ga_day(text):
    """GA4's "20261001" as "2026-10-01"."""
    t = str(text or "")
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) == 8 and t.isdigit() else None


def domain_family(domain) -> str:
    d = str(domain or "").lower()
    if any(k in d for k in BOOKING_DOMAINS):
        return "booking_clicks"
    if any(k in d for k in ORDERING_DOMAINS):
        return "ordering_clicks"
    if any(k in d for k in CHECKOUT_DOMAINS):
        return "checkout_clicks"
    return "other_clicks"


def read_ga4(property_id, start, end) -> dict:
    """{(day, metric): value} for the property over [start, end]."""
    out = {}

    def add(day, metric, value):
        if day:
            out[(day, metric)] = out.get((day, metric), 0.0) + float(value or 0)

    for dims, vals in ga4_report(property_id, start, end, ["date"],
                                 ["sessions", "totalUsers", "screenPageViews", "engagedSessions"]):
        day = _ga_day(dims[0])
        for name, v in zip(("sessions", "users", "pageviews", "engaged_sessions"), vals):
            add(day, name, v)
    for dims, vals in ga4_report(property_id, start, end, ["date", "sessionDefaultChannelGroup"], ["sessions"]):
        add(_ga_day(dims[0]), f"channel:{(dims[1] or 'Unassigned')[:40]}", vals[0])
    clicks = ga4_report(property_id, start, end, ["date", "linkDomain"], ["eventCount"],
                        dim_filter={"filter": {"fieldName": "eventName",
                                               "stringFilter": {"matchType": "EXACT", "value": "click"}}})
    for dims, vals in clicks:
        day, domain = _ga_day(dims[0]), (dims[1] or "").strip().lower()
        if not domain or domain == "(not set)":
            continue
        add(day, domain_family(domain), vals[0])
        add(day, f"click:{domain[:60]}", vals[0])
    for dims, vals in ga4_report(property_id, start, end, ["date"], ["screenPageViews"],
                                 dim_filter={"filter": {"fieldName": "pagePath", "stringFilter": {
                                     "matchType": "CONTAINS", "value": "menu", "caseSensitive": False}}}):
        add(_ga_day(dims[0]), "menu_views", vals[0])
    return out


def read_gsc(site_url, start, end) -> dict:
    out = {}
    for row in gsc_query(site_url, start, end, ["date"]):
        day = (row.get("keys") or [None])[0]
        if not day:
            continue
        out[(day, "search_clicks")] = float(row.get("clicks") or 0)
        out[(day, "search_impressions")] = float(row.get("impressions") or 0)
        if row.get("position") is not None:
            out[(day, "search_position")] = round(float(row.get("position")), 2)
    return out


def _store(restaurant_id, source, values, db_path=None):
    if not values:
        return 0
    conn = get_conn(db_path)
    try:
        conn.executemany(
            "INSERT INTO web_analytics_daily (restaurant_id, source, day, metric, value, fetched_at) "
            "VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, source, day, metric) DO UPDATE SET "
            "value=excluded.value, fetched_at=excluded.fetched_at",
            [(restaurant_id, source, day, metric, value) for (day, metric), value in values.items()])
        conn.commit()
    finally:
        conn.close()
    return len(values)


def _has_rows(restaurant_id, source, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        return bool(conn.execute("SELECT 1 FROM web_analytics_daily WHERE restaurant_id=? AND source=? LIMIT 1",
                                 (restaurant_id, source)).fetchone())
    finally:
        conn.close()


def _store_queries(restaurant_id, site_url, end, db_path=None):
    start = end - timedelta(days=QUERY_WINDOW_DAYS - 1)
    rows = gsc_query(site_url, start, end, ["query"], limit=STORED_QUERIES)
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM web_search_queries WHERE restaurant_id=?", (restaurant_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO web_search_queries (restaurant_id, window_end, query, clicks, impressions, position) "
            "VALUES (?,?,?,?,?,?)",
            [(restaurant_id, end.isoformat(), str((r.get("keys") or [""])[0])[:120], float(r.get("clicks") or 0),
              float(r.get("impressions") or 0), r.get("position")) for r in rows if (r.get("keys") or [""])[0]])
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def _today(restaurant):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant.id, naive=True).date()
    except Exception:
        return date.today()


def sync(restaurant_id, db_path=None, restaurant=None) -> dict:
    """Read GA4 and Search Console for one restaurant into
    web_analytics_daily. {"ok", "ga4": {...}, "gsc": {...}, "error"}; each
    source on its own, so one refusing never empties the other. Records
    the attempt on the data-health ledger (source "website") and the
    restaurant's web_analytics_synced_at / _error."""
    r = restaurant or _models.get_restaurant(restaurant_id)
    pid, site = clean_property_id(getattr(r, "ga4_property_id", None)), clean_site_url(getattr(r, "gsc_site_url", None))
    if not (pid or site):
        return {"ok": False, "error": "No website analytics connected.", "code": "not_connected"}
    if not configured():
        return {"ok": False, "error": "Website analytics isn't set up on this server yet.", "code": "not_configured"}
    today = _today(r)
    end = today - timedelta(days=1)
    out, errors = {"ok": True}, []
    for source, key, reader in (("ga4", pid, read_ga4), ("gsc", site, read_gsc)):
        if not key:
            continue
        start = end - timedelta(days=(RECENT_DAYS if _has_rows(restaurant_id, source, db_path) else BACKFILL_DAYS) - 1)
        try:
            values = reader(key, start, end)
            out[source] = {"ok": True, "rows": _store(restaurant_id, source, values, db_path),
                           "from": start.isoformat(), "to": end.isoformat()}
            if source == "gsc":
                out[source]["queries"] = _store_queries(restaurant_id, key, end, db_path)
        except WebAnalyticsError as e:
            out[source] = {"ok": False, "error": e.message, "code": e.code}
            errors.append(e.message)
    out["ok"] = not errors
    out["error"] = " ".join(errors) or None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        fields = {"web_analytics_error": out["error"]}
        if out["ok"]:
            fields["web_analytics_synced_at"] = now
        _models.update_restaurant(restaurant_id, fields)
    except Exception as e:
        log.warning("web_analytics state not saved for %s: %s", restaurant_id, e)
    try:
        import data_health
        data_health.record_attempt(restaurant_id, "website", out["ok"], provider="google", error=out["error"],
                                   data_through=end.isoformat() if out["ok"] else None)
    except Exception:
        pass
    return out


def verify(property_id=None, site_url=None) -> dict:
    """Whether Cavnar AI can read what an owner just pasted: one tiny report
    each. {"ga4": {"ok", "error"}, "gsc": {...}} for the parts given."""
    out = {}
    end = date.today() - timedelta(days=1)
    if property_id:
        try:
            ga4_report(property_id, end - timedelta(days=6), end, ["date"], ["sessions"], limit=7)
            out["ga4"] = {"ok": True}
        except WebAnalyticsError as e:
            out["ga4"] = {"ok": False, "error": e.message, "code": e.code}
    if site_url:
        try:
            gsc_query(site_url, end - timedelta(days=6), end, ["date"], limit=7)
            out["gsc"] = {"ok": True}
        except WebAnalyticsError as e:
            out["gsc"] = {"ok": False, "error": e.message, "code": e.code}
    return out


# ── what the numbers say ────────────────────────────────────────────────────

def _series(restaurant_id, metrics, start, end, db_path=None) -> dict:
    """{metric: {day: value}} across both sources."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT metric, day, SUM(value) AS v FROM web_analytics_daily WHERE restaurant_id=? AND day BETWEEN ? AND ? "
            f"AND metric IN ({','.join('?' * len(metrics))}) GROUP BY metric, day",
            (restaurant_id, start.isoformat(), end.isoformat(), *metrics)).fetchall()
    finally:
        conn.close()
    out = {m: {} for m in metrics}
    for r in rows:
        out[r["metric"]][r["day"]] = float(r["v"] or 0)
    return out


def _last_day(restaurant_id, source, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT MAX(day) FROM web_analytics_daily WHERE restaurant_id=? AND source=?",
                           (restaurant_id, source)).fetchone()
    finally:
        conn.close()
    try:
        return date.fromisoformat(row[0]) if row and row[0] else None
    except ValueError:
        return None


def judge_day(values: dict, day: date):
    """(value, typical, n) for one day against the median of the same weekday
    over the BASELINE_WEEKS before it; typical None when fewer than
    MIN_BASELINE of those weekdays have a reading."""
    key = day.isoformat()
    if key not in values:
        return None, None, 0
    base = [values[(day - timedelta(weeks=w)).isoformat()] for w in range(1, BASELINE_WEEKS + 1)
            if (day - timedelta(weeks=w)).isoformat() in values]
    if len(base) < MIN_BASELINE:
        return values[key], None, len(base)
    return values[key], statistics.median(base), len(base)


def _weeks(values: dict, end: date, n: int):
    """The last n complete 7-day blocks ending at `end`, oldest first; None
    for a block with a missing day."""
    out = []
    for i in range(n - 1, -1, -1):
        stop = end - timedelta(days=7 * i)
        days = [(stop - timedelta(days=k)).isoformat() for k in range(7)]
        out.append(sum(values[d] for d in days) if all(d in values for d in days) else None)
    return out


def _sales_context(restaurant_id, day, db_path=None):
    """That night's sales against the median of its weekday's previous 8
    finished nights, as a phrase; None when either side is missing."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT date, sales FROM labor_daily_history WHERE restaurant_id=? AND date<=? "
                            "AND date>=? AND sales>0", (restaurant_id, day.isoformat(),
                                                        (day - timedelta(weeks=BASELINE_WEEKS)).isoformat())).fetchall()
    except Exception:
        return None
    finally:
        conn.close()
    by = {r["date"]: float(r["sales"] or 0) for r in rows}
    night = by.get(day.isoformat())
    base = [by[(day - timedelta(weeks=w)).isoformat()] for w in range(1, BASELINE_WEEKS + 1)
            if (day - timedelta(weeks=w)).isoformat() in by]
    if night is None or len(base) < 4:
        return None
    typical = statistics.median(base)
    if typical <= 0:
        return None
    pct = round((night - typical) / typical * 100)
    return f"sales {'+' if pct >= 0 else ''}{pct}% against a typical {day.strftime('%A')}"


def _day_context(restaurant_id, day, db_path=None) -> list:
    """What else happened that day, each a short phrase."""
    out = []
    sales = _sales_context(restaurant_id, day, db_path)
    if sales:
        out.append(sales)
    conn = get_conn(db_path)
    try:
        for sql, fmt in (
                ("SELECT label FROM demand_signals WHERE restaurant_id=? AND date=? AND COALESCE(label,'')!='' LIMIT 2",
                 lambda r: r["label"]),
                ("SELECT post_platform FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL "
                 "AND substr(COALESCE(posted_at, created_at),1,10)=? LIMIT 3",
                 lambda r: f"you posted on {(r['post_platform'] or 'social').title()}"),
        ):
            try:
                for r in conn.execute(sql, (restaurant_id, day.isoformat())).fetchall():
                    phrase = fmt(r)
                    if phrase and phrase not in out:
                        out.append(phrase)
            except Exception:
                continue
        try:
            n = conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                             "AND substr(review_date,1,10)=?", (restaurant_id, day.isoformat())).fetchone()[0]
            if n:
                out.append(f"{n} new review{'' if n == 1 else 's'}")
        except Exception:
            pass
    finally:
        conn.close()
    return out


def _mdy(d):
    from time_utils import mdy
    return mdy(d.isoformat())


def _num(v):
    return f"{v:,.0f}"


def signals(restaurant_id, db_path=None, today=None) -> dict:
    """Spikes, dips and 3-week runs in the tracked metrics, newest first,
    each with what else happened that day. {"available", "reason", "items"}."""
    ga_last, gsc_last = _last_day(restaurant_id, "ga4", db_path), _last_day(restaurant_id, "gsc", db_path)
    if not (ga_last or gsc_last):
        return {"available": False, "reason": "no website analytics yet", "items": []}
    newest = max(d for d in (ga_last, gsc_last) if d)
    start = newest - timedelta(weeks=BASELINE_WEEKS + 2)
    keys = [k for k, _l, _f in TRACKED]
    series = _series(restaurant_id, keys, start, newest, db_path)
    items = []
    for key in keys:
        values = series.get(key) or {}
        if not values:
            continue
        last = gsc_last if key in GSC_METRICS else ga_last
        if not last:
            continue
        for back in range(LOOKBACK_DAYS):
            day = last - timedelta(days=back)
            value, typical, n = judge_day(values, day)
            if value is None or typical is None or typical <= 0:
                continue
            gap = value - typical
            if abs(gap) < FLOORS[key]:
                continue
            kind = "spike" if value >= typical * SPIKE_RATIO else ("dip" if value <= typical * DIP_RATIO else None)
            if not kind:
                continue
            pct = round(gap / typical * 100)
            items.append({"kind": kind, "metric": key, "label": LABELS[key], "day": day.isoformat(),
                          "day_label": f"{day.strftime('%a')} {_mdy(day)}", "value": round(value, 1),
                          "typical": round(typical, 1), "pct": pct, "n": n,
                          "text": (f"{LABELS[key]} {'jumped' if kind == 'spike' else 'fell'} to {_num(value)} on "
                                   f"{day.strftime('%A')} {_mdy(day)}, against a typical {day.strftime('%A')}'s "
                                   f"{_num(typical)} ({'+' if pct >= 0 else ''}{pct}%)."),
                          "context": _day_context(restaurant_id, day, db_path)})
        weeks = _weeks(values, last, TREND_WEEKS + 1)
        if all(w is not None for w in weeks) and weeks[0] > 0:
            falling = all(weeks[i + 1] < weeks[i] for i in range(TREND_WEEKS))
            rising = all(weeks[i + 1] > weeks[i] for i in range(TREND_WEEKS))
            change = (weeks[-1] - weeks[0]) / weeks[0]
            if (falling and change <= -TREND_CHANGE or rising and change >= TREND_CHANGE) \
                    and abs(weeks[-1] - weeks[0]) >= FLOORS[key] * 2:
                pct = round(change * 100)
                items.append({"kind": "trend_down" if falling else "trend_up", "metric": key, "label": LABELS[key],
                              "day": last.isoformat(), "day_label": f"week to {_mdy(last)}",
                              "value": round(weeks[-1], 1), "typical": round(weeks[0], 1), "pct": pct, "n": 4,
                              "weeks": [round(w, 1) for w in weeks],
                              "text": (f"{LABELS[key]} {'fell' if falling else 'rose'} {TREND_WEEKS} weeks running: "
                                       f"{_num(weeks[-1])} in the week to {_mdy(last)}, against {_num(weeks[0])} "
                                       f"in the week {TREND_WEEKS} weeks earlier ({'+' if pct >= 0 else ''}{pct}%)."),
                              "context": []})
    # A run already says the last week moved: its own days going the same
    # way are the same news, said again (a dip inside a falling run).
    runs = {(i["metric"], "dip" if i["kind"] == "trend_down" else "spike")
            for i in items if i["kind"] in ("trend_down", "trend_up")}
    items = [i for i in items if (i["metric"], i["kind"]) not in runs]
    items.sort(key=lambda i: (i["day"], i["kind"].startswith("trend"), abs(i["pct"])), reverse=True)
    return {"available": True, "reason": None, "items": items,
            "basis": (f"each day against the median of the same weekday over the {BASELINE_WEEKS} weeks before; "
                      f"what else happened that day moved with it, which is not proof it caused it")}


def summary(restaurant_id, db_path=None, days=28) -> dict:
    """The Marketing card: the last `days` against the `days` before, the
    daily visits line, visits by channel, clicks out by family, the top
    searches, and signals()."""
    r = _models.get_restaurant(restaurant_id)
    connected = bool(clean_property_id(getattr(r, "ga4_property_id", None))
                     or clean_site_url(getattr(r, "gsc_site_url", None)))
    ga_last, gsc_last = _last_day(restaurant_id, "ga4", db_path), _last_day(restaurant_id, "gsc", db_path)
    base = {"connected": connected, "configured": configured(), "synced_at": getattr(r, "web_analytics_synced_at", None),
            "error": getattr(r, "web_analytics_error", None)}
    if not (ga_last or gsc_last):
        return dict(base, available=False)
    out = dict(base, available=True, totals=[], channels=[], clicks=[], queries=[], line=[])
    for key, label, src_last in (("sessions", "Website visits", ga_last), ("booking_clicks", "Clicks to book", ga_last),
                                 ("ordering_clicks", "Clicks to order", ga_last),
                                 ("checkout_clicks", "Clicks to checkout", ga_last),
                                 ("menu_views", "Menu page views", ga_last),
                                 ("search_clicks", "Google search clicks", gsc_last),
                                 ("search_impressions", "Google search appearances", gsc_last)):
        if not src_last:
            continue
        cur_start = src_last - timedelta(days=days - 1)
        prev_end, prev_start = cur_start - timedelta(days=1), cur_start - timedelta(days=days)
        s = _series(restaurant_id, [key], prev_start, src_last, db_path)[key]
        cur = sum(v for d, v in s.items() if d >= cur_start.isoformat())
        prev_days = [d for d in s if prev_start.isoformat() <= d <= prev_end.isoformat()]
        prev = sum(s[d] for d in prev_days)
        if not cur and not prev:
            continue
        out["totals"].append({"metric": key, "label": label, "value": round(cur), "through": _mdy(src_last),
                              "prev": round(prev) if len(prev_days) >= days - 2 else None,
                              "pct": (round((cur - prev) / prev * 100) if prev and len(prev_days) >= days - 2 else None)})
    if ga_last:
        start = ga_last - timedelta(days=days - 1)
        line = _series(restaurant_id, ["sessions"], start, ga_last, db_path)["sessions"]
        out["line"] = [{"day": (start + timedelta(days=i)).isoformat(),
                        "value": line.get((start + timedelta(days=i)).isoformat())} for i in range(days)]
        conn = get_conn(db_path)
        try:
            for prefix, bucket in (("channel:", "channels"), ("click:", "clicks")):
                rows = conn.execute("SELECT metric, SUM(value) v FROM web_analytics_daily WHERE restaurant_id=? "
                                    "AND source='ga4' AND day>=? AND metric LIKE ? GROUP BY metric ORDER BY v DESC LIMIT 8",
                                    (restaurant_id, start.isoformat(), prefix + "%")).fetchall()
                total = sum(float(x["v"] or 0) for x in rows) or 0
                out[bucket] = [{"name": x["metric"][len(prefix):], "value": round(float(x["v"] or 0)),
                                "share": round(float(x["v"] or 0) / total * 100) if total else None,
                                "family": domain_family(x["metric"][len(prefix):]) if bucket == "clicks" else None}
                               for x in rows]
        finally:
            conn.close()
    conn = get_conn(db_path)
    try:
        q = conn.execute("SELECT window_end, query, clicks, impressions, position FROM web_search_queries "
                         "WHERE restaurant_id=? ORDER BY clicks DESC, impressions DESC LIMIT ?",
                         (restaurant_id, TOP_QUERIES)).fetchall()
    finally:
        conn.close()
    out["queries"] = [{"query": x["query"], "clicks": round(x["clicks"]), "impressions": round(x["impressions"]),
                       "position": round(x["position"], 1) if x["position"] is not None else None} for x in q]
    out["queries_through"] = _mdy(date.fromisoformat(q[0]["window_end"])) if q else None
    sig = signals(restaurant_id, db_path=db_path)
    out["signals"] = sig.get("items") or []
    out["signals_basis"] = sig.get("basis")
    return out


# ── Google searches → the questions asked of AI search ───────────────────────
#
# What people type into Google before landing on the site is the best
# evidence of what they will ask an AI assistant. The searches that are not
# the restaurant's own name ("restaurants in st charles il", "food near me")
# become AI-visibility questions, carrying their Google volume and position,
# so Intel can say "#1 on Google for this, not named by AI".

AI_SEARCH_QUESTIONS = 4          # of the eight questions a visibility check asks
MIN_QUESTION_IMPRESSIONS = 20    # below this a search is noise, not demand

# Words in a restaurant's name that say what it is, not who it is: a search
# for "pizza near me" is not a search for "Gia Mia Pizza Bar".
_GENERIC_NAME_WORDS = frozenset((
    "the and of kitchen tap taproom restaurant restaurants bar bars grill grille cafe café pub tavern house "
    "eatery co company bistro pizzeria pizza brewery brewing lounge diner deli bakery cantina taqueria "
    "steakhouse sushi bbq burger burgers wings wine coffee tea bagels bagel noodle noodles ramen thai "
    "mexican italian chinese indian sports social club market hall room").split())
# Words that change how a search is phrased, not what it is for.
_FILLER = frozenset("in near the best top a an for of good to around nearby me at on places place spots spot "
                    "what where are is some find".split())


def _qnorm(s) -> str:
    s = str(s or "").lower().replace("\u2019", "").replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def _stem(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def brand_words(name) -> set:
    """The words that make a search about this restaurant: "Simple EJ's
    Kitchen & Tap" → {simple, ejs, ej}."""
    out = set()
    for w in _qnorm(name).split():
        if len(w) < 2 or w in _GENERIC_NAME_WORDS:
            continue
        out.add(w)
        if w.endswith("s") and len(w) > 2:
            out.add(w[:-1])
    return out


def is_branded(query, name) -> bool:
    words = set(_qnorm(query).split())
    brand = brand_words(name)
    return bool(brand and any(w in brand or _stem(w) in brand for w in words))


def _question_key(text, state="") -> frozenset:
    """Two phrasings of one search share a key: "restaurants st charles il"
    and "restaurants in St. Charles, IL" are one question."""
    drop = _FILLER | ({state.lower()} if state else set())
    return frozenset(_stem(w) for w in _qnorm(text).split() if w not in drop)


def to_ai_question(query, city, city_full) -> str | None:
    """A Google search as it would be put to an AI assistant, which has no
    idea where the person is: "near me" becomes near the restaurant's city,
    a city named without its state gets it, and a search naming no place
    gets one. None for a search that is only the place itself."""
    n = _qnorm(query)
    ncity = _qnorm(city)
    state = city_full.split(",")[-1].strip() if "," in (city_full or "") else ""
    if not n or not ncity:
        return None
    marker = " \x00 "
    if ncity in n:
        n = re.sub(r"\b" + re.escape(ncity) + r"\b(?:\s+" + re.escape(state.lower()) + r"\b)?" if state else
                   r"\b" + re.escape(ncity) + r"\b", marker, n, count=1)
    elif "near me" in n:
        n = n.replace("near me", "near" + marker, 1)
    else:
        n = n + " in" + marker
    rest = [w for w in n.replace("\x00", " ").split() if w not in _FILLER and w != state.lower()]
    if not rest:
        return None
    # The place reads at the end, after a preposition: "st charles restaurants"
    # and "restaurants st charles il" both ask "restaurants in St. Charles, IL".
    pre, _, post = n.partition("\x00")
    pre, post = pre.split(), post.split()
    if not pre:
        pre, post = post, []
    if pre and pre[-1] not in ("in", "near", "around", "of", "by", "from"):
        pre.append("in")
    return " ".join(pre + [city_full] + post)


def ai_questions(restaurant_id, name, city, city_full, limit=AI_SEARCH_QUESTIONS, db_path=None) -> list:
    """The non-branded searches that brought people to the site over the last
    28 days, as AI-visibility questions, most-searched first:
    [{"q", "kind": "search", "search": {"queries", "impressions", "clicks",
    "position"}}]. Phrasings of one search are merged and their volume summed."""
    if not (restaurant_id and city and city_full):
        return []
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT query, clicks, impressions, position FROM web_search_queries "
                            "WHERE restaurant_id=? AND impressions>=? ORDER BY impressions DESC",
                            (restaurant_id, MIN_QUESTION_IMPRESSIONS)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    state = city_full.split(",")[-1].strip() if "," in city_full else ""
    merged = {}
    for r in rows:
        if is_branded(r["query"], name):
            continue
        q = to_ai_question(r["query"], city, city_full)
        if not q:
            continue
        key = _question_key(q, state)
        g = merged.setdefault(key, {"q": q, "queries": [], "impressions": 0.0, "clicks": 0.0, "_pos": 0.0})
        impr = float(r["impressions"] or 0)
        g["queries"].append(r["query"])
        g["impressions"] += impr
        g["clicks"] += float(r["clicks"] or 0)
        if r["position"] is not None:
            g["_pos"] += float(r["position"]) * impr
    out = []
    for g in sorted(merged.values(), key=lambda g: -g["impressions"])[:max(0, int(limit))]:
        out.append({"q": g["q"], "kind": "search", "key": _question_key(g["q"], state),
                    "search": {"queries": g["queries"][:5], "impressions": round(g["impressions"]),
                               "clicks": round(g["clicks"]),
                               "position": round(g["_pos"] / g["impressions"], 1) if g["impressions"] and g["_pos"] else None}})
    return out


# ── the daily job ───────────────────────────────────────────────────────────

def eligible(db_path=None) -> list:
    """Restaurant ids in service with a GA4 property or Search Console site."""
    from models import get_all_restaurants, in_service
    out = []
    for r in get_all_restaurants():
        if (clean_property_id(getattr(r, "ga4_property_id", None)) or clean_site_url(getattr(r, "gsc_site_url", None))) \
                and in_service(r):
            out.append(r.id)
    return sorted(out)
