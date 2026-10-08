"""
offsite_backup.py — the copy of the database that lives somewhere other
than the database's own volume (#1, decision 7).

Two things here:

1. An S3-compatible object-storage client (Cloudflare R2, AWS S3, Backblaze
   B2, MinIO), signed with AWS Signature Version 4 over `requests` — no new
   dependency. Configured by env:

     BACKUP_S3_ENDPOINT          https://<account>.r2.cloudflarestorage.com
                                 (or https://s3.<region>.amazonaws.com)
     BACKUP_S3_BUCKET            the bucket
     BACKUP_S3_ACCESS_KEY_ID     an access key scoped to that bucket
     BACKUP_S3_SECRET_ACCESS_KEY its secret
     BACKUP_S3_REGION            "auto" for R2 (the default), e.g. us-east-1
     BACKUP_S3_PREFIX            optional key prefix (default "cavnar-backups/")

   Objects are addressed path-style (<endpoint>/<bucket>/<key>), which R2 and
   S3 both accept. Every upload carries the SHA-256 of its body in
   x-amz-content-sha256, so the store refuses a body that arrived damaged,
   and in x-amz-meta-sha256 so the drill can prove what it downloads.

2. The credential scrub registry: what is taken out of a snapshot before it
   leaves the server. The LOCAL snapshot is never scrubbed — it is the
   restore artifact (docs/ops/RECOVERY.md). The redaction list used to name
   six restaurants columns by hand, so the POS, reservation and webhook
   credentials and every staff-portal link rode along in the "stripped"
   emailed copy (#102). It is built now from the platform's one credential
   registry (credentials.credential_columns: credentials.FIELDS plus every
   column whose name says it holds a secret) and the token tables below;
   tests/test_fix_d_backup.py fails when a new credential-looking column is
   neither scrubbed nor kept on purpose, and at runtime an unclassified one
   is scrubbed anyway. What a run actually took out is what the backup
   email says (describe_scrub).
"""
import datetime as _dt
import hashlib
import hmac
import logging
import os
import re
import urllib.parse

log = logging.getLogger("offsite_backup")

# Connect / read timeouts for the object store (scripts/check_timeouts.py):
# a snapshot upload is one long body, so the read allowance is generous.
S3_TIMEOUT = (10, 600)
# The bucket's lifecycle rule is one small XML document.
S3_LIFECYCLE_TIMEOUT = (10, 30)
# A single PUT carries at most this much (S3's and R2's single-part limit).
S3_MAX_SINGLE_PUT = 5 * 1024 ** 3


# ── configuration ───────────────────────────────────────────────────────────

def s3_config():
    """The object-store settings, or None when any required one is unset."""
    cfg = {
        "endpoint": (os.getenv("BACKUP_S3_ENDPOINT") or "").strip().rstrip("/"),
        "bucket": (os.getenv("BACKUP_S3_BUCKET") or "").strip(),
        "access_key": (os.getenv("BACKUP_S3_ACCESS_KEY_ID") or "").strip(),
        "secret_key": (os.getenv("BACKUP_S3_SECRET_ACCESS_KEY") or "").strip(),
        "region": (os.getenv("BACKUP_S3_REGION") or "auto").strip() or "auto",
        "prefix": os.getenv("BACKUP_S3_PREFIX", "cavnar-backups/"),
    }
    if not all(cfg[k] for k in ("endpoint", "bucket", "access_key", "secret_key")):
        return None
    if not cfg["endpoint"].startswith("https://"):
        # Credentials and the (encrypted) backup never travel in the clear.
        log.error("BACKUP_S3_ENDPOINT must be https://")
        return None
    return cfg


# ── AWS Signature Version 4 ─────────────────────────────────────────────────

def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key, date_stamp, region, service="s3"):
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def _uri_encode(value, slash_safe):
    """RFC 3986 encoding as SigV4 requires: unreserved characters kept, every
    other byte %XX (uppercase); "/" kept only in a path."""
    return urllib.parse.quote(value, safe="/~" if slash_safe else "~")


def canonical_query(params):
    if not params:
        return ""
    pairs = sorted((_uri_encode(str(k), False), _uri_encode(str(v), False)) for k, v in params.items())
    return "&".join(f"{k}={v}" for k, v in pairs)


def sign(method, path, headers, payload_hash, access_key, secret_key, region, amz_date,
         query=None, service="s3"):
    """The Authorization header value for one request (SigV4, single chunk).

    `path` is the raw object path ("/bucket/key"), encoded here; `headers`
    are every header to sign (host and x-amz-date included), names in any
    case; `amz_date` is "YYYYMMDDTHHMMSSZ". Pure — the test vectors from
    AWS's own documentation pin it (tests/test_fix_d_backup.py)."""
    canon_headers = {k.lower().strip(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    names = sorted(canon_headers)
    signed = ";".join(names)
    canonical_request = "\n".join([
        method.upper(),
        _uri_encode(path, True),
        canonical_query(query),
        "".join(f"{n}:{canon_headers[n]}\n" for n in names),
        signed,
        payload_hash,
    ])
    date_stamp = amz_date[:8]
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(signing_key(secret_key, date_stamp, region, service),
                         string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"


def _object_url(cfg, key):
    path = f"/{cfg['bucket']}/{key.lstrip('/')}"
    return cfg["endpoint"] + _uri_encode(path, True), path


def _signed_headers(cfg, method, path, payload_hash, extra=None, now=None):
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    host = urllib.parse.urlparse(cfg["endpoint"]).netloc
    headers = {"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
    headers.update({k.lower(): v for k, v in (extra or {}).items()})
    auth = sign(method, path, headers, payload_hash, cfg["access_key"], cfg["secret_key"], cfg["region"], amz_date)
    out = {k: v for k, v in headers.items() if k != "host"}
    out["Authorization"] = auth
    return out


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


class OffsiteError(RuntimeError):
    """The object store did not take (or give back) the copy."""


def upload_file(path, key, cfg=None, sha256_hex=None):
    """PUT one file to the bucket, streamed (never read whole into memory),
    with its SHA-256 as the signed payload hash, so the store verifies what
    arrived. Returns {"target", "sha256", "bytes"}; raises OffsiteError."""
    import requests
    cfg = cfg or s3_config()
    if not cfg:
        raise OffsiteError("object storage is not configured (BACKUP_S3_*)")
    size = os.path.getsize(path)
    if size > S3_MAX_SINGLE_PUT:
        raise OffsiteError(f"{size} bytes is over the {S3_MAX_SINGLE_PUT}-byte single-PUT limit; "
                           "the uploader needs multipart before the database is this large")
    digest = sha256_file(path) if sha256_hex is None else sha256_hex
    url, obj_path = _object_url(cfg, (cfg.get("prefix") or "") + key)
    headers = _signed_headers(cfg, "PUT", obj_path, digest, extra={"x-amz-meta-sha256": digest})
    headers["Content-Type"] = "application/octet-stream"
    headers["Content-Length"] = str(size)
    with open(path, "rb") as body:
        resp = requests.put(url, data=body, headers=headers, timeout=S3_TIMEOUT)
    if not 200 <= resp.status_code < 300:
        raise OffsiteError(f"object store refused the upload: HTTP {resp.status_code} {(resp.text or '')[:200]}")
    return {"target": f"s3://{cfg['bucket']}/{(cfg.get('prefix') or '')}{key}", "sha256": digest, "bytes": size}


def download_file(key, dest, cfg=None, full_key=False):
    """GET one object to `dest`, streamed; returns {"sha256", "meta_sha256",
    "bytes"}. `full_key` says `key` already carries the prefix."""
    import requests
    cfg = cfg or s3_config()
    if not cfg:
        raise OffsiteError("object storage is not configured (BACKUP_S3_*)")
    empty = hashlib.sha256(b"").hexdigest()
    url, obj_path = _object_url(cfg, key if full_key else (cfg.get("prefix") or "") + key)
    headers = _signed_headers(cfg, "GET", obj_path, empty)
    h, n = hashlib.sha256(), 0
    with requests.get(url, headers=headers, stream=True, timeout=S3_TIMEOUT) as resp:
        if resp.status_code != 200:
            raise OffsiteError(f"object store refused the download: HTTP {resp.status_code}")
        meta = resp.headers.get("x-amz-meta-sha256")
        with open(dest, "wb") as out:
            for block in resp.iter_content(chunk_size=1024 * 1024):
                if block:
                    out.write(block)
                    h.update(block)
                    n += len(block)
    return {"sha256": h.hexdigest(), "meta_sha256": meta, "bytes": n}


def lifecycle_days(cfg=None) -> dict:
    """{"days", "rule", "error"} — how many days the object store keeps a
    backup copy before its own lifecycle rule deletes it: the shortest
    Expiration of any ENABLED rule whose prefix covers BACKUP_S3_PREFIX, or
    days None (no rule; the store keeps every copy forever) with why.
    Memory re-audit 9/29/26 (FORGET-9): the expiry the recovery runbook
    relies on was a manual bucket setting nothing in code ever read. One
    signed GET ?lifecycle, with a timeout. Never raises."""
    import requests
    import xml.etree.ElementTree as ET
    cfg = cfg or s3_config()
    if not cfg:
        return {"days": None, "rule": None, "error": "object storage is not configured (BACKUP_S3_*)"}
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    empty = hashlib.sha256(b"").hexdigest()
    path = f"/{cfg['bucket']}"
    host = urllib.parse.urlparse(cfg["endpoint"]).netloc
    headers = {"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": empty}
    auth = sign("GET", path, headers, empty, cfg["access_key"], cfg["secret_key"], cfg["region"], amz_date,
                query={"lifecycle": ""})
    send = {k: v for k, v in headers.items() if k != "host"}
    send["Authorization"] = auth
    try:
        resp = requests.get(cfg["endpoint"] + _uri_encode(path, True) + "?lifecycle=", headers=send,
                            timeout=S3_LIFECYCLE_TIMEOUT)
    except Exception as e:
        return {"days": None, "rule": None, "error": f"lifecycle unreadable: {e}"}
    if resp.status_code == 404:
        return {"days": None, "rule": None, "error": "no lifecycle rule on the bucket"}
    if resp.status_code != 200:
        return {"days": None, "rule": None, "error": f"lifecycle unreadable: HTTP {resp.status_code}"}
    try:
        root = ET.fromstring(resp.content or b"")
    except ET.ParseError as e:
        return {"days": None, "rule": None, "error": f"lifecycle unreadable: {e}"}

    def _local(tag):
        return tag.rsplit("}", 1)[-1]
    prefix = cfg.get("prefix") or ""
    best = None
    for rule in (el for el in root.iter() if _local(el.tag) == "Rule"):
        fields = {}
        for el in rule.iter():
            name = _local(el.tag)
            if name in ("ID", "Status", "Prefix", "Days") and name not in fields:
                fields[name] = (el.text or "").strip()
        if fields.get("Status", "").lower() != "enabled":
            continue
        if not any(_local(el.tag) == "Expiration" for el in rule.iter()):
            continue
        rule_prefix = fields.get("Prefix", "")
        if rule_prefix and not prefix.startswith(rule_prefix):
            continue
        try:
            days = int(fields.get("Days") or "")
        except ValueError:
            continue
        if best is None or days < best[0]:
            best = (days, fields.get("ID") or None)
    if best is None:
        return {"days": None, "rule": None, "error": "no enabled expiration rule covers the backup prefix"}
    return {"days": best[0], "rule": best[1], "error": None}


# A key scoped to one bucket's objects (R2 "Object Read & Write" — the least
# privilege the backup needs) cannot read the bucket's lifecycle rule: the
# GET ?lifecycle answers 403. The copies themselves can still be listed.
LIFECYCLE_FORBIDDEN = ("lifecycle unreadable: HTTP 401", "lifecycle unreadable: HTTP 403")
S3_LIST_MAX_PAGES = 50                      # 50,000 objects; a nightly backup makes one a day


def oldest_copy_days(cfg=None) -> dict:
    """{"oldest_days", "count", "error"} — the age in days of the oldest
    object under BACKUP_S3_PREFIX (ListObjectsV2, paged, each call with a
    timeout). What the lifecycle rule exists to guarantee, measured
    directly, for a key that may not read the rule (9/29/26). oldest_days is
    None when there are no copies yet. Never raises."""
    import requests
    import xml.etree.ElementTree as ET
    cfg = cfg or s3_config()
    if not cfg:
        return {"oldest_days": None, "count": 0, "error": "object storage is not configured (BACKUP_S3_*)"}
    empty = hashlib.sha256(b"").hexdigest()
    path = f"/{cfg['bucket']}"
    host = urllib.parse.urlparse(cfg["endpoint"]).netloc
    now = _dt.datetime.now(_dt.timezone.utc)
    oldest, count, token = None, 0, None
    for _ in range(S3_LIST_MAX_PAGES):
        query = {"list-type": "2", "prefix": cfg.get("prefix") or ""}
        if token:
            query["continuation-token"] = token
        amz_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = {"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": empty}
        auth = sign("GET", path, headers, empty, cfg["access_key"], cfg["secret_key"], cfg["region"], amz_date,
                    query=query)
        send = {k: v for k, v in headers.items() if k != "host"}
        send["Authorization"] = auth
        try:
            resp = requests.get(cfg["endpoint"] + _uri_encode(path, True) + "?" + canonical_query(query),
                                headers=send, timeout=S3_LIFECYCLE_TIMEOUT)
        except Exception as e:
            return {"oldest_days": None, "count": count, "error": f"copies unlistable: {e}"}
        if resp.status_code != 200:
            return {"oldest_days": None, "count": count, "error": f"copies unlistable: HTTP {resp.status_code}"}
        try:
            root = ET.fromstring(resp.content or b"")
        except ET.ParseError as e:
            return {"oldest_days": None, "count": count, "error": f"copies unlistable: {e}"}
        truncated, token = False, None
        for el in root:
            name = el.tag.rsplit("}", 1)[-1]
            if name == "Contents":
                stamp = next((c.text for c in el if c.tag.rsplit("}", 1)[-1] == "LastModified"), None)
                try:
                    at = _dt.datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
                except ValueError:
                    continue
                count += 1
                age = (now - at).total_seconds() / 86400.0
                oldest = age if oldest is None or age > oldest else oldest
            elif name == "IsTruncated":
                truncated = (el.text or "").strip().lower() == "true"
            elif name == "NextContinuationToken":
                token = (el.text or "").strip() or None
        if not (truncated and token):
            return {"oldest_days": None if oldest is None else round(oldest, 1), "count": count, "error": None}
    return {"oldest_days": None if oldest is None else round(oldest, 1), "count": count,
            "error": f"copies unlistable: more than {S3_LIST_MAX_PAGES} pages"}


# ── the credential scrub registry (#102) ────────────────────────────────────

# Column names that look like a credential. Anything matching is scrubbed
# from the off-site copy unless it is kept on purpose below.
CREDENTIAL_NAME = re.compile(r"token|secret|api_?key|password|passcode|private_?key|credential", re.I)

# Whole tables that never leave the server: live access, not data.
SCRUB_TABLES = {
    "sessions": "bearer sessions (hashed); a restore never needs live sessions",
    "two_fa_backup_codes": "2FA recovery codes",
    "trusted_devices": "remembered 2FA devices",
    "device_tokens": "APNs device tokens",
    "live_activity_tokens": "APNs Live Activity push tokens (parity audit #61)",
    "login_reports": "one-time 'this wasn't me' links that revoke sessions",
    "staff_portal_tokens": "bearer links into each restaurant's staff portal",
    "app_secrets": "this install's own link-signing secrets",
    "view_as_sessions": "admin view-as sessions",
    "user_backup_codes": "each login's own 2FA recovery codes (hashed) — the admin's included (fix round A)",
    "user_totp": "each internal login's authenticator-app secret (encrypted under CREDENTIAL_KEY) — a live "
                 "second factor, never needed off the server (R10)",
    "async_jobs": "transient job results (6-hour TTL); one can hold a one-time password (the review-account "
                  "seed's password_once) until it is read",
}

# Columns nulled in the off-site copy, beyond every credentials.FIELDS
# column (read at call time, so a field added there is scrubbed here).
SCRUB_COLUMNS = {
    ("users", "reset_token"), ("users", "reset_token_expires"), ("users", "recovery_email_code"),
    ("restaurants", "temp_password"), ("restaurants", "two_fa_device_token"),
    ("restaurants", "gmb_access_token"), ("restaurants", "gmb_refresh_token"),
    ("restaurants", "ig_token"), ("restaurants", "fb_page_token"),
    ("restaurants", "toast_client_secret"), ("restaurants", "toast_access_token"),
    ("restaurants", "square_access_token"), ("restaurants", "clover_api_token"),
    ("restaurants", "rpower_token"), ("restaurants", "reservation_api_key"),
    ("restaurants", "backoffice_api_key"), ("restaurants", "stripe_customer_id"),
    ("webhooks", "secret"),
    # The share link itself, encrypted under CREDENTIAL_KEY so the admin can
    # copy it again (fix round B2): a working public link once decrypted.
    ("sales_audit_shares", "token_enc"),
}

# Rows whose words never leave the server (memory re-audit 9/29/26,
# FORGET-1): {table: (which rows, "module:CONSTANT" naming the columns)}.
# A review the owner's retention, the owner or Google removed is hidden from
# every screen, and its guest text is erased on the server a month later
# (history_rollups.erase_removed_reviews) — but every off-site copy carried
# it in full, forever. The off-site copy blanks it at once.
SCRUB_ROWS = {
    "reviews": ("deleted_at IS NOT NULL", "history_rollups:REVIEW_GUEST_TEXT"),
}


def _scrub_rows(conn):
    """Blank SCRUB_ROWS' columns on their rows in a copy. Returns
    ["table.column", ...] blanked (only columns this database has)."""
    import importlib
    done = []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t, (where, ref) in SCRUB_ROWS.items():
        if t not in tables:
            continue
        mod, const = ref.split(":", 1)
        info = list(conn.execute(f'PRAGMA table_info("{t}")'))
        have = {r[1] for r in info}
        notnull = {r[1] for r in info if r[3]}
        cols = [c for c in getattr(importlib.import_module(mod), const) if c in have]
        if not cols:
            continue
        sets = ", ".join(f'"{c}"=' + ("''" if c in notnull else "NULL") for c in cols)
        conn.execute(f'UPDATE "{t}" SET {sets} WHERE {where}')
        done += [f"{t}.{c} (removed rows)" for c in cols]
    return done


# Credential-LOOKING columns that are deliberately kept, and why. A new one
# that matches CREDENTIAL_NAME must be added to SCRUB_COLUMNS or here —
# tests/test_fix_d_backup.py fails until it is.
KEEP_COLUMNS = {
    ("users", "password_hash"): "a salted hash; dropping it locks every user out of a restored copy",
    ("users", "password_changed_at"): "a date",
    ("users", "password_strength"): "a label",
    ("users", "must_reset_password"): "a flag",
    ("restaurants", "ig_token_expires"): "a date",
    ("restaurants", "fb_token_expires"): "a date",
    ("restaurants", "gmb_token_expires"): "a date",
    ("restaurants", "toast_token_expires"): "a date",
    ("issue_links", "token_hash"): "the hash of a one-time link, not the link",
    ("staff_signups", "token_hash"): "the hash of a one-time link, not the link",
    ("staff_pin_resets", "token_hash"): "the hash of a one-time PIN-reset token, not the token",
    ("ai_validation_log", "tokens"): "words from the model's own output, not credentials",
    ("ai_runs", "input_tokens"): "a count of model input tokens",
    ("ai_runs", "output_tokens"): "a count of model output tokens",
    ("context_sections", "tokens"): "an estimate of a context section's model tokens",
    ("push_deliveries", "device_token_id"): "an id",
    ("user_passkeys", "credential_id"): "a passkey's public id (passkeys.py); its key is a public key, and "
                                        "dropping it unlinks every passkey in a restored copy",
    ("guest_campaigns", "link_token"): "a public link id printed in a guest text",
    ("guest_contacts", "email_token"): "a guest's unsubscribe link id",
    ("guest_newsletter_recipients", "email_token"): "a guest's unsubscribe link id",
    ("marketing_content_log", "link_token"): "a public tracked-link id in a published post",
    ("marketing_links", "token"): "a public tracked-link id in a published post",
    ("marketing_media", "token"): "a public media link id",
    ("schedule_shares", "token"): "the SHA-256 of a schedule link (models._share_hash), not the link",
    ("staff_calendar_links", "token_hash"): "the SHA-256 of a staff calendar feed link (staff_insights), "
                                            "not the link",
    ("task_proof_media", "token"): "a task sheet proof photo's id — served only to a signed-in login of the same restaurant, and the photo is in the copy anyway",
    ("sales_audit_shares", "token"): "the SHA-256 of an audit share link (sales_audits._hash_token), not the link",
    ("sales_audit_shares", "token_hint"): "the link's last four characters, which open nothing",
    ("push_outbox", "device_token_id"): "an id into device_tokens, which is emptied",
    # Model token COUNTS — usage and cost figures, not credentials.
    ("ai_calls", "input_tokens"): "a count of model tokens",
    ("ai_calls", "output_tokens"): "a count of model tokens",
    ("ai_usage", "input_tokens"): "a count of model tokens",
    ("ai_usage", "output_tokens"): "a count of model tokens",
    ("ai_usage", "cache_write_tokens"): "a count of model tokens",
    ("ai_usage", "cache_read_tokens"): "a count of model tokens",
    ("ai_usage_daily", "input_tokens"): "a count of model tokens",
    ("ai_usage_daily", "output_tokens"): "a count of model tokens",
    ("ai_usage_daily", "cache_write_tokens"): "a count of model tokens",
    ("ai_usage_daily", "cache_read_tokens"): "a count of model tokens",
}


def credential_columns(conn):
    """[(table, column)] for every column whose name looks like a credential."""
    out = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in sorted(tables):
        for r in conn.execute(f'PRAGMA table_info("{t}")'):
            if CREDENTIAL_NAME.search(r[1]):
                out.append((t, r[1]))
    return out


def scrub_plan(conn):
    """(tables to empty, [(table, column)] to null, unclassified) for this
    database.

    The columns are built from the platform's one credential registry,
    credentials.credential_columns(conn) (credentials.FIELDS plus every
    column whose name says it holds a secret), and SCRUB_COLUMNS for what a
    name cannot tell (a Stripe customer id, a reset token's expiry). A
    column kept on purpose (KEEP_COLUMNS: hashes, counts, public link ids a
    restore must keep working) is never nulled. `unclassified` are
    credential-looking columns — by this module's broader pattern or the
    registry — named in no list: scrubbed anyway (fail safe) and reported,
    so a new one is noticed (tests/test_fix_d_backup.py fails on one)."""
    import credentials
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    have = {}

    def _cols(t):
        if t not in have:
            have[t] = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')} if t in tables else set()
        return have[t]
    wipe = sorted(t for t in SCRUB_TABLES if t in tables)
    named = set(SCRUB_COLUMNS) | {("restaurants", f) for f in credentials.FIELDS}
    registry = set(credentials.credential_columns(conn))
    unclassified = []
    for t, c in sorted(set(credential_columns(conn)) | registry):
        if t in SCRUB_TABLES or (t, c) in named or (t, c) in KEEP_COLUMNS:
            continue
        unclassified.append((t, c))
    null = sorted((t, c) for t, c in (named | registry | set(unclassified)) - set(KEEP_COLUMNS)
                  if t not in SCRUB_TABLES and c in _cols(t))
    return wipe, null, unclassified


def redact(path):
    """Scrub a COPY of a snapshot before it leaves the server: empty
    SCRUB_TABLES, null every credential column, VACUUM so the old pages go
    too. Returns what it did. Never called on the local snapshot."""
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        wipe, null, unclassified = scrub_plan(conn)
        if unclassified:
            log.error(f"off-site backup scrubbed unclassified credential-looking columns: {unclassified}")
        for t in wipe:
            conn.execute(f'DELETE FROM "{t}"')
        notnull = {}
        for t, c in null:
            if t not in notnull:
                notnull[t] = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")') if r[3]}
            # A NOT NULL column (webhooks.secret) is blanked rather than nulled.
            blank = "''" if c in notnull[t] else "NULL"
            conn.execute(f'UPDATE "{t}" SET "{c}"={blank} WHERE "{c}" IS NOT NULL AND "{c}" != \'\'')
        rows = _scrub_rows(conn)
        conn.commit()
        conn.execute("VACUUM")
        return {"tables": wipe, "columns": [f"{t}.{c}" for t, c in null], "unclassified": unclassified,
                "rows": rows}
    finally:
        conn.close()


def describe_scrub(scrubbed) -> dict:
    """What one redact() took out of the copy, in words for the backup
    email: {emptied, blanked, precaution, kept}. The email used to say
    "every API credential and secret" whatever had actually been stripped;
    now it names the tables emptied and the columns blanked, what was
    blanked only because nobody had classified it, and what is kept on
    purpose. Lists are sorted, plain strings; the caller escapes them."""
    scrubbed = scrubbed or {}
    return {
        "emptied": sorted(scrubbed.get("tables") or []),
        "blanked": sorted(scrubbed.get("columns") or []),
        "precaution": sorted(f"{t}.{c}" for t, c in (scrubbed.get("unclassified") or [])),
        "removed_rows": sorted(scrubbed.get("rows") or []),
        "kept": "password hashes, hashed link tokens, public link ids guests and staff already hold, and "
                "model token counts (offsite_backup.KEEP_COLUMNS)",
    }
