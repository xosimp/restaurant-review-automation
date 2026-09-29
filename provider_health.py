"""
provider_health.py — is each third party still accepting our credentials?

The admin console's key panel checked that a variable was set (#29). Only
Resend was ever called to check its key, and only for the public status
page, so a revoked Twilio token, an Anthropic key rotated without updating
Railway, an exhausted Resend quota or a Places key whose billing lapsed was
discovered by the first customer send that failed.

run_probes() makes one cheap authenticated call per provider. None of them
sends, spends or writes anything on the provider's side:

  resend         GET /domains (a sending-only key answers restricted_api_key,
                 which proves the key); plus the newest email_log rows, so a
                 daily or monthly quota that is refusing sends reads failing
  twilio         GET the account resource: active, suspended or closed
  anthropic      GET /v1/models; plus the newest ai_usage rows, so a credit
                 balance that has run out reads failing
  stripe         GET /v1/balance (a restricted key's 403 still proves the key)
  google_places  Place Details asking only for place_id — Google bills that
                 under its no-charge ID-refresh SKU
  apns           mint the ES256 provider token (no network: it proves the .p8
                 parses and signs, which is how push failed for months)

Each answer is recorded in provider_health (one row per probe). A provider
that goes from anything else to failing, or stays unreachable for three
probes running, pages Will once. Scheduled hourly by the scheduler (see
jobs_registry), read by the admin system card and by the status page's
email row. PROVIDER_PROBES_SKIP=google_places,... turns a probe off.
"""
import json
import logging
import os
import sqlite3
import time

log = logging.getLogger("provider_health")

# (connect, read) seconds. Four gunicorn threads are the whole platform, but
# these run on the scheduler thread; the bound is still explicit
# (scripts/check_timeouts.py).
PROBE_TIMEOUT = (3.05, 6)

PROVIDERS = ("resend", "twilio", "anthropic", "stripe", "google_places", "apns")

# A place Google's own documentation uses. If it is ever retired Google
# answers NOT_FOUND, which still proves the key.
PROBE_PLACE_ID = "ChIJN1t_tDeuEmsRUsoyG83frY4"

# Consecutive unreachable probes (timeouts, 5xx) before a transient error
# pages: one blip is the internet, three hours of it is an outage.
ERROR_STREAK_TO_PAGE = 3

_DDL = (
    """CREATE TABLE IF NOT EXISTS provider_health (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        provider    TEXT NOT NULL,
        state       TEXT NOT NULL,
        detail      TEXT,
        http_status INTEGER,
        latency_ms  INTEGER,
        checked_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_provider_health_provider ON provider_health(provider, id)",
    "CREATE INDEX IF NOT EXISTS idx_provider_health_checked ON provider_health(checked_at)",
)


def init_provider_health(db_path=None):
    """Create the table at boot (hosted_dashboard). Never on a request."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        for sql in _DDL:
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


# ── the one HTTP call (tests replace it) ────────────────────────────────────

def _get(url, **kwargs):
    import requests
    return requests.get(url, timeout=PROBE_TIMEOUT, allow_redirects=False, **kwargs)


def _json(resp):
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _result(state, detail=None, http_status=None, started=None):
    return {"state": state, "detail": (_redact(detail)[:240] if detail else None),
            "http_status": http_status,
            "latency_ms": round((time.monotonic() - started) * 1000) if started else None}


def _redact(text):
    try:
        from ai_guard import redact_secrets
        return redact_secrets(str(text))
    except Exception:
        return str(text)[:60]


def _transport_error(e, started):
    return _result("error", f"unreachable: {type(e).__name__}", None, started)


# ── probes (each returns {state, detail, http_status, latency_ms}; never raises)

_QUOTA_NAMES = ("daily_quota_exceeded", "monthly_quota_exceeded")


def _resend_quota_refusing(db_path=None):
    """Whether Resend's quota is refusing sends right now: the newest
    email_log row that failed on a quota is newer than the newest one that
    went out. Read by row order, not time, because sent_at has been written
    in two zones."""
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
    except Exception:
        return False
    try:
        quota = conn.execute(
            "SELECT MAX(id) FROM email_log WHERE status <> 'sent' AND (error LIKE '%quota%' "
            "OR error LIKE '%daily_quota_exceeded%' OR error LIKE '%monthly_quota_exceeded%')").fetchone()[0]
        sent = conn.execute("SELECT MAX(id) FROM email_log WHERE status = 'sent'").fetchone()[0]
        return bool(quota) and (not sent or quota > sent)
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def probe_resend(db_path=None):
    key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not key:
        return _result("unconfigured", "RESEND_API_KEY is not set")
    started = time.monotonic()
    try:
        resp = _get("https://api.resend.com/domains", headers={"Authorization": f"Bearer {key}"})
    except Exception as e:
        return _transport_error(e, started)
    code, name = resp.status_code, str(_json(resp).get("name") or "")
    if code == 200 or name == "restricted_api_key":
        if _resend_quota_refusing(db_path):
            return _result("failing", "sending quota reached: recent sends were refused", code, started)
        return _result("ok", "sending-only key" if name == "restricted_api_key" else None, code, started)
    if name in _QUOTA_NAMES:
        return _result("failing", f"quota reached ({name})", code, started)
    if code in (401, 403):
        return _result("failing", f"key rejected ({name or code})", code, started)
    if code == 429:
        return _result("error", "rate limited", code, started)
    return _result("error", f"HTTP {code}", code, started)


def probe_twilio(db_path=None):
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not sid or not token:
        return _result("unconfigured", "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
    started = time.monotonic()
    try:
        resp = _get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json", auth=(sid, token))
    except Exception as e:
        return _transport_error(e, started)
    code = resp.status_code
    if code == 200:
        status = str(_json(resp).get("status") or "").lower()
        if status and status != "active":
            return _result("failing", f"account is {status}", code, started)
        return _result("ok", None, code, started)
    if code in (401, 403, 404):
        return _result("failing", f"credentials rejected (HTTP {code})", code, started)
    return _result("error", f"HTTP {code}", code, started)


def _anthropic_credit_exhausted(db_path=None):
    """The newest ai_usage error saying the credit balance is too low is
    newer than the newest successful call. /v1/models authenticates without
    spending, so it cannot see an empty balance itself."""
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
    except Exception:
        return False
    try:
        bad = conn.execute("SELECT MAX(id) FROM ai_usage WHERE status = 'error' "
                           "AND LOWER(COALESCE(error, '')) LIKE '%credit balance%'").fetchone()[0]
        good = conn.execute("SELECT MAX(id) FROM ai_usage WHERE status = 'ok'").fetchone()[0]
        return bool(bad) and (not good or bad > good)
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def probe_anthropic(db_path=None):
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        return _result("unconfigured", "ANTHROPIC_API_KEY is not set")
    started = time.monotonic()
    try:
        resp = _get("https://api.anthropic.com/v1/models?limit=1",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    except Exception as e:
        return _transport_error(e, started)
    code = resp.status_code
    err = _json(resp).get("error") or {}
    kind = str(err.get("type") or "") if isinstance(err, dict) else ""
    if code == 200:
        if _anthropic_credit_exhausted(db_path):
            return _result("failing", "credit balance too low: recent calls were refused", code, started)
        return _result("ok", None, code, started)
    if code in (401, 403):
        return _result("failing", f"key rejected ({kind or code})", code, started)
    if code == 429:
        return _result("error", "rate limited", code, started)
    return _result("error", f"HTTP {code}" + (f" ({kind})" if kind else ""), code, started)


def probe_stripe(db_path=None):
    key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        return _result("unconfigured", "STRIPE_SECRET_KEY is not set")
    started = time.monotonic()
    try:
        resp = _get("https://api.stripe.com/v1/balance", auth=(key, ""))
    except Exception as e:
        return _transport_error(e, started)
    code = resp.status_code
    if code == 200:
        return _result("ok", "live" if key.startswith(("sk_live", "rk_live")) else "test key", code, started)
    if code == 403:
        return _result("ok", "restricted key (balance not readable)", code, started)
    if code == 401:
        return _result("failing", "key rejected", code, started)
    return _result("error", f"HTTP {code}", code, started)


def probe_google_places(db_path=None):
    try:
        import config
        key = config.google_places_key()
    except Exception:
        key = ""
    if not key:
        return _result("unconfigured", "GOOGLE_PLACES_API_KEY is not set")
    started = time.monotonic()
    try:
        resp = _get("https://maps.googleapis.com/maps/api/place/details/json",
                    params={"place_id": PROBE_PLACE_ID, "fields": "place_id", "key": key})
    except Exception as e:
        return _transport_error(e, started)
    code = resp.status_code
    body = _json(resp)
    status = str(body.get("status") or "")
    if code == 200 and status in ("OK", "NOT_FOUND", "INVALID_REQUEST", "ZERO_RESULTS"):
        return _result("ok", None if status == "OK" else status, code, started)
    if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT", "OVER_DAILY_LIMIT"):
        msg = body.get("error_message") or ""
        return _result("failing", f"{status}" + (f": {msg}" if msg else ""), code, started)
    return _result("error", f"HTTP {code}" + (f" ({status})" if status else ""), code, started)


def probe_apns(db_path=None):
    key_id = (os.getenv("APNS_KEY_ID") or "").strip()
    team_id = (os.getenv("APNS_TEAM_ID") or "").strip()
    private_key = (os.getenv("APNS_PRIVATE_KEY") or "").replace("\\n", "\n").strip()
    if not (key_id and team_id and private_key):
        return _result("unconfigured", "APNS_KEY_ID / APNS_TEAM_ID / APNS_PRIVATE_KEY not set")
    started = time.monotonic()
    try:
        import jwt as _pyjwt
        _pyjwt.encode({"iss": team_id, "iat": int(time.time())}, private_key,
                      algorithm="ES256", headers={"kid": key_id})
        return _result("ok", None, None, started)
    except Exception as e:
        body = "".join(private_key.splitlines()[1:-1])
        if body and not any(c.isalnum() for c in body):
            return _result("failing", "APNS_PRIVATE_KEY is a masked placeholder, not a key", None, started)
        return _result("failing", f"APNS_PRIVATE_KEY does not sign: {type(e).__name__}", None, started)


PROBES = {"resend": probe_resend, "twilio": probe_twilio, "anthropic": probe_anthropic,
          "stripe": probe_stripe, "google_places": probe_google_places, "apns": probe_apns}


def probe(provider, db_path=None):
    """One live probe, not recorded. Never raises."""
    fn = PROBES.get(provider)
    if fn is None:
        return _result("unconfigured", f"unknown provider {provider}")
    try:
        return fn(db_path)
    except Exception as e:
        return _result("error", f"probe failed: {type(e).__name__}")


# ── the ledger ──────────────────────────────────────────────────────────────

def _skipped():
    raw = os.getenv("PROVIDER_PROBES_SKIP") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def _record(conn, provider, r):
    conn.execute("INSERT INTO provider_health (provider, state, detail, http_status, latency_ms) "
                 "VALUES (?,?,?,?,?)", (provider, r["state"], r.get("detail"), r.get("http_status"),
                                        r.get("latency_ms")))


def _streak(conn, provider):
    """(state of the newest row, how many newest rows share it)."""
    rows = conn.execute("SELECT state FROM provider_health WHERE provider=? ORDER BY id DESC LIMIT 20",
                        (provider,)).fetchall()
    if not rows:
        return None, 0
    first = rows[0][0]
    n = 0
    for (s,) in rows:
        if s != first:
            break
        n += 1
    return first, n


def latest(db_path=None) -> dict:
    """{provider: {state, detail, http_status, latency_ms, checked_at,
    last_ok_at, streak}} from the newest probe of each."""
    from models import get_conn, DB_PATH
    try:
        conn = get_conn(db_path or DB_PATH)
    except Exception:
        return {}
    try:
        rows = conn.execute(
            "SELECT p.provider, p.state, p.detail, p.http_status, p.latency_ms, p.checked_at FROM provider_health p "
            "WHERE p.id = (SELECT MAX(id) FROM provider_health q WHERE q.provider = p.provider)").fetchall()
        oks = {r[0]: r[1] for r in conn.execute(
            "SELECT provider, MAX(checked_at) FROM provider_health WHERE state='ok' GROUP BY provider").fetchall()}
        out = {}
        for r in rows:
            d = dict(r)
            d["last_ok_at"] = oks.get(d["provider"])
            d["streak"] = _streak(conn, d["provider"])[1]
            out[d.pop("provider")] = d
        return out
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def run_probes(db_path=None, alert=True) -> dict:
    """Probe every provider, record the answers, and page Will on a provider
    that has just started failing. {attempted, ok, failed, skipped,
    hit_bound, results}. `failed` counts failing and unreachable providers, so a night
    of them shows on the job's run like any other partial pass."""
    from models import get_conn, DB_PATH
    skip = _skipped()
    results = {}
    for name in PROVIDERS:
        if name in skip:
            continue
        results[name] = probe(name, db_path)
    pages = []
    conn = get_conn(db_path or DB_PATH)
    try:
        for name, r in results.items():
            before, _n = _streak(conn, name)
            _record(conn, name, r)
            conn.commit()
            now_state, streak = _streak(conn, name)
            if r["state"] == "failing" and before != "failing":
                pages.append(f"{name}: {r.get('detail') or 'failing'} (was {before or 'never probed'})")
            elif r["state"] == "error" and streak == ERROR_STREAK_TO_PAGE:
                pages.append(f"{name}: unreachable for {streak} probes running ({r.get('detail') or 'error'})")
    finally:
        conn.close()
    if pages and alert:
        _page(pages)
    counted = [r for r in results.values() if r["state"] != "unconfigured"]
    return {"attempted": len(counted),
            "ok": sum(1 for r in counted if r["state"] == "ok"),
            "failed": sum(1 for r in counted if r["state"] in ("failing", "error")),
            "skipped": len(results) - len(counted) + len(skip & set(PROVIDERS)),
            "hit_bound": False,
            "results": {k: v["state"] for k, v in results.items()}}


def _page(lines):
    """One alert for every provider that just started failing. Only where
    the scheduler runs: a laptop with production keys must not page."""
    try:
        import scheduler
        if not scheduler.scheduling_allowed():
            return False
        import ops
        return bool(ops.alert_will("Cavnar AI: a provider is refusing requests", lines))
    except Exception as e:
        log.error("provider alert failed: %s", e)
        return False
