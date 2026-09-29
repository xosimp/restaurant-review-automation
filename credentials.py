"""Encryption at rest for the credentials the platform holds on behalf of
its clients: Google Business Profile, Instagram/Facebook, Toast, Square,
Clover, RPOWER, Back Office and reservation-system tokens.

Before this they were plain columns on `restaurants`, so a read of the
volume — or of any of the fourteen nightly snapshots beside the database —
was every tenant's Google listing. Now `update_restaurant` encrypts these
fields on the way in and `get_restaurant` decrypts them on the way out;
the rest of the codebase reads `restaurant.gmb_refresh_token` exactly as
before.

Values carry a prefix so plaintext rows written before the key existed,
and encrypted rows, decode side by side. With no CREDENTIAL_KEY set the
functions pass values through untouched and say so once — the product keeps
working, the gap is logged rather than hidden — and once the key is set,
encrypt_existing() (run at every boot) re-saves the plaintext rows.

FIELDS is checked against the schema by a test: every restaurants column
whose name says token, secret or api key is either here or in EXCLUDED
with the reason it cannot be (#102 — the Square, Clover and RPOWER tokens
and the reservation key were never encrypted, even with the key set).
Every reader of the four added columns goes through get_restaurant (the
provider modules' _headers/_creds, reservation_feeds.status) or only tests
them for truth (admin_ops, pos_health, emails), which ciphertext passes.
"""
import os
import re

FIELDS = ("gmb_access_token", "gmb_refresh_token", "ig_token", "fb_page_token",
          "toast_client_secret", "toast_access_token", "toast_refresh_token",
          "backoffice_api_key",
          "square_access_token", "clover_api_token", "rpower_token", "reservation_api_key")

# Credential-looking restaurants columns that are NOT encrypted, and why.
EXCLUDED = {
    # Read back by raw SQL and compared to a cookie by value (auth.py's
    # legacy single-slot trusted device); encrypting it would sign every
    # remembered device out. Superseded by trusted_devices and only ever
    # cleared now; hashing or dropping it is the auth module's call.
    "two_fa_device_token": "legacy lookup-by-value slot, cleared, never written",
    # Blanked at every boot (models.init_db); the demo seed's password is
    # the auth module's to store.
    "temp_password": "blanked at boot; demo password storage belongs to auth",
    # Timestamps, not secrets.
    "ig_token_expires": "expiry timestamp", "fb_token_expires": "expiry timestamp",
    "gmb_token_expires": "expiry timestamp", "toast_token_expires": "expiry timestamp",
}

# What a credential column's name looks like.
CREDENTIAL_NAME = re.compile(r"(token|secret|api_key|apikey|password)", re.I)

PREFIX = "enc:v1:"
_warned = False
_fernet_cache = {}


def key_configured():
    return bool((os.getenv("CREDENTIAL_KEY") or "").strip())


def key_state():
    """missing | invalid | ok — invalid is a key Fernet will not load, with
    which every credential save raises."""
    key = (os.getenv("CREDENTIAL_KEY") or "").strip()
    if not key:
        return "missing"
    try:
        _fernet()
        return "ok"
    except Exception:
        return "invalid"


def _fernet():
    key = (os.getenv("CREDENTIAL_KEY") or "").strip()
    if not key:
        global _warned
        if not _warned:
            _warned = True
            print("WARNING: CREDENTIAL_KEY not set — client OAuth/POS credentials are stored unencrypted. "
                  "Generate one with Fernet.generate_key() and set it in Railway.")
        return None
    f = _fernet_cache.get(key)
    if f is None:
        from cryptography.fernet import Fernet
        f = Fernet(key.encode() if isinstance(key, str) else key)
        _fernet_cache[key] = f
    return f


def encrypt(value):
    if value is None or value == "" or not isinstance(value, str) or value.startswith(PREFIX):
        return value
    f = _fernet()
    if f is None:
        return value
    return PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value):
    """The plaintext, the value unchanged when it was never encrypted, or
    None when it cannot be decrypted (a rotated key) — never the ciphertext,
    which a caller would otherwise send to Google as a token."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="credential_decrypt")
        except Exception:
            pass
        return None


def encrypt_fields(updates):
    return {k: (encrypt(v) if k in FIELDS else v) for k, v in updates.items()}


def decrypt_restaurant(r):
    for f in FIELDS:
        if hasattr(r, f):
            v = getattr(r, f)
            if isinstance(v, str) and v.startswith(PREFIX):
                setattr(r, f, decrypt(v))
    return r


# ── the columns on disk ─────────────────────────────────────────────────────

def _present_fields(conn):
    have = {r[1] for r in conn.execute('PRAGMA table_info("restaurants")')}
    return [f for f in FIELDS if f in have]


def plaintext_count(conn) -> int:
    """How many credential values in restaurants are stored unencrypted."""
    n = 0
    for col in _present_fields(conn):
        n += conn.execute(
            f"SELECT COUNT(*) FROM restaurants WHERE {col} IS NOT NULL AND {col} <> '' "
            f"AND substr({col}, 1, {len(PREFIX)}) <> ?", (PREFIX,)).fetchone()[0]
    return n


def status(db_path=None) -> dict:
    """{key: ok|missing|invalid, plaintext, encrypted} for the admin system
    card, which warns while the key is unset (#102)."""
    from models import get_conn, DB_PATH
    out = {"key": key_state(), "plaintext": None, "encrypted": None, "fields": list(FIELDS)}
    conn = get_conn(db_path or DB_PATH)
    try:
        out["plaintext"] = plaintext_count(conn)
        enc = 0
        for col in _present_fields(conn):
            enc += conn.execute(f"SELECT COUNT(*) FROM restaurants WHERE substr({col}, 1, {len(PREFIX)}) = ?",
                                (PREFIX,)).fetchone()[0]
        out["encrypted"] = enc
    finally:
        conn.close()
    return out


def encrypt_existing(db_path=None) -> dict:
    """Re-save every plaintext credential encrypted, once CREDENTIAL_KEY is
    set. Run at boot; idempotent (an encrypted value is skipped).

    Safe by construction: each value is encrypted and decrypted back before
    it is written, and written only if the row still holds the plaintext it
    was read with (compare-and-set), so a save racing this cannot be
    overwritten with a stale value. Nothing happens without a working key.
    Returns {encrypted, skipped, failed, key}."""
    out = {"encrypted": 0, "skipped": 0, "failed": 0, "key": key_state()}
    if out["key"] != "ok":
        return out
    from models import get_conn, DB_PATH, _invalidate_request_cache
    conn = get_conn(db_path or DB_PATH)
    try:
        for col in _present_fields(conn):
            rows = conn.execute(
                f"SELECT id, {col} FROM restaurants WHERE {col} IS NOT NULL AND {col} <> '' "
                f"AND substr({col}, 1, {len(PREFIX)}) <> ?", (PREFIX,)).fetchall()
            for rid, value in rows:
                if not isinstance(value, str):
                    out["skipped"] += 1
                    continue
                enc = encrypt(value)
                if not enc.startswith(PREFIX) or decrypt(enc) != value:
                    out["failed"] += 1
                    continue
                cur = conn.execute(f"UPDATE restaurants SET {col}=? WHERE id=? AND {col}=?", (enc, rid, value))
                if cur.rowcount == 1:
                    out["encrypted"] += 1
                    _invalidate_request_cache(rid)
                else:
                    out["skipped"] += 1
            conn.commit()
    finally:
        conn.close()
    return out


# ── one registry of what counts as a credential column ──────────────────────

_NOT_A_SECRET_SUFFIXES = ("_expires", "_expires_at", "_hash", "tokens", "_id", "_at", "_strength")
_NOT_A_SECRET_PREFIXES = ("must_",)

def credential_columns(conn) -> list:
    """[(table, column)] for every column in this database whose name says
    it holds a credential, restaurants' FIELDS included — the registry an
    off-site copy is scrubbed from (the backup's hand-kept list missed the
    Square, Clover and RPOWER tokens, the reservation and Back Office keys
    and the webhook secrets). Hashes, stamps, ids, flags and scores that
    merely mention a credential (password_changed_at, device_token_id,
    must_reset_password), token COUNTS (ai_usage.input_tokens) and
    idempotency or source keys are not credentials; whole tables of secrets
    (sessions, app_secrets) are the scrub's own list, not column names."""
    out = set()
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        for r in conn.execute(f'PRAGMA table_info("{table}")'):
            col = r[1]
            low = col.lower()
            if not CREDENTIAL_NAME.search(low):
                continue
            if low.endswith(_NOT_A_SECRET_SUFFIXES) or low.startswith(_NOT_A_SECRET_PREFIXES):
                continue
            out.add((table, col))
    have_restaurants = {r[1] for r in conn.execute('PRAGMA table_info("restaurants")')}
    out.update(("restaurants", f) for f in FIELDS if f in have_restaurants)
    return sorted(out)
