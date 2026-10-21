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
import logging
import os
import re
import threading
import time

log = logging.getLogger("credentials")

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


def previous_keys() -> list:
    """CREDENTIAL_KEY_PREVIOUS: the keys a rotation retires, comma-separated,
    newest first (scale audit 10/19/26, SEC-14). They only DECRYPT; every
    write uses CREDENTIAL_KEY, and the boot's rotate_existing() re-encrypts
    what they sealed, after which they can be removed."""
    raw = (os.getenv("CREDENTIAL_KEY_PREVIOUS") or "").strip()
    return [k.strip() for k in raw.split(",") if k.strip()]


def _single(key):
    f = _fernet_cache.get(key)
    if f is None:
        from cryptography.fernet import Fernet
        f = Fernet(key.encode() if isinstance(key, str) else key)
        _fernet_cache[key] = f
    return f


def _fernet():
    """The current key's Fernet — or, while a rotation is under way, a
    MultiFernet that encrypts with CREDENTIAL_KEY and decrypts with it or any
    CREDENTIAL_KEY_PREVIOUS. Rotating used to mean every POS, Google and
    Meta token decrypted to None (SEC-14)."""
    key = (os.getenv("CREDENTIAL_KEY") or "").strip()
    if not key:
        global _warned
        if not _warned:
            _warned = True
            print("WARNING: CREDENTIAL_KEY not set — client OAuth/POS credentials are stored unencrypted. "
                  "Generate one with Fernet.generate_key() and set it in Railway.")
        return None
    olds = [k for k in previous_keys() if k != key]
    if not olds:
        return _single(key)
    cache_key = (key, tuple(olds))
    f = _fernet_cache.get(cache_key)
    if f is None:
        from cryptography.fernet import MultiFernet
        f = MultiFernet([_single(key)] + [_single(k) for k in olds])
        _fernet_cache[cache_key] = f
    return f


class CredentialKeyError(RuntimeError):
    """A credential save refused because this server cannot seal it (Audit
    #23 F7, A7-08): on Railway a missing or invalid CREDENTIAL_KEY used to
    store the token in plaintext with one print. The message is safe to show
    an admin."""


def must_seal() -> bool:
    """Whether a credential may be written only sealed: on Railway (or
    wherever the scheduler may run). A laptop with no key still stores
    plaintext, as before, so tests and local work need no key."""
    try:
        import config
        return bool(config.on_railway() or config.scheduling_allowed())
    except Exception:
        return False


def saves_refused():
    """The sentence a credential save answers with when this server must
    seal it and cannot (A7-08), else None."""
    state = key_state()
    if state == "ok" or not must_seal():
        return None
    return ("Credential saves are refused: CREDENTIAL_KEY is " + state + " on this server, so the token "
            "could not be encrypted. Fix the key in Railway first.")


def encrypt(value):
    if value is None or value == "" or not isinstance(value, str) or value.startswith(PREFIX):
        return value
    state = key_state()
    f = _fernet() if state in ("ok", "missing") else None   # missing: None, and said once
    if f is None:
        refused = saves_refused()
        if refused or state == "invalid":
            # An invalid key raised here before too (Fernet would not load);
            # a missing one now raises as well wherever it must seal.
            refused = refused or "CREDENTIAL_KEY is not a valid Fernet key; the credential was not saved."
            _alert("key", f"CREDENTIAL_KEY is {key_state()}: a credential save was refused",
                   ["A token or secret was about to be stored in plaintext; it was refused instead.",
                    "Set a valid CREDENTIAL_KEY in Railway (Fernet.generate_key()) and redeploy."])
            raise CredentialKeyError(refused)
        return value
    return PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


# ── the page when a credential cannot be sealed or opened (A7-08) ───────────
# A lost or changed CREDENTIAL_KEY decrypts every token to None; is_connected
# then reads False everywhere and every store stops syncing with nothing but
# an ops.capture. Paged (ops.page_operator, its own hourly cooldown), off the
# caller's thread, at most once per ALERT_EVERY_SECONDS per process — decrypt
# runs on every restaurant load.
ALERT_EVERY_SECONDS = 3600
_alerted = {}
_alert_lock = threading.Lock()


def _alert(kind, subject, lines):
    if not must_seal():
        return
    now = time.monotonic()
    with _alert_lock:
        last = _alerted.get(kind)
        if last is not None and now - last < ALERT_EVERY_SECONDS:
            return
        _alerted[kind] = now

    def _send():
        try:
            import ops
            ops.page_operator(f"credentials:{kind}", f"Cavnar AI: {subject}", lines)
        except Exception as e:
            log.warning("credential alert %s not paged: %s", kind, e)
    threading.Thread(target=_send, name="credential-alert", daemon=True).start()


def undecryptable(where="a stored credential"):
    """Say, once an hour, that a sealed value no configured key opens."""
    _alert("decrypt", "a stored credential no longer decrypts",
           [f"{where} is sealed under a key this server does not hold (CREDENTIAL_KEY is {key_state()}).",
            "Every integration that reads it (POS, Google, Meta) reads as disconnected until the key is "
            "restored, or CREDENTIAL_KEY_PREVIOUS names the old one."])


def decrypt(value):
    """The plaintext, the value unchanged when it was never encrypted, or
    None when it cannot be decrypted (a rotated key) — never the ciphertext,
    which a caller would otherwise send to Google as a token."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        f = _fernet()
    except Exception:
        f = None                       # an invalid key: nothing opens
    if f is None:
        undecryptable()
        return None
    try:
        return f.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="credential_decrypt")
        except Exception:
            pass
        undecryptable()
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


# ── key rotation (scale audit 10/19/26, SEC-14) ─────────────────────────────
# Every column sealed with this module's PREFIX: the restaurants credentials,
# an internal login's authenticator secrets, a sales-audit share link and a
# customer webhook's signing secret.
SEALED_COLUMNS = tuple(("restaurants", f) for f in FIELDS) + (
    ("pos_credentials", "token"),
    ("user_totp", "secret"), ("user_totp", "pending_secret"),
    ("sales_audit_shares", "token_enc"), ("webhooks", "secret"))


def _table_columns(conn, table):
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except Exception:
        return set()


def rotate_existing(db_path=None) -> dict:
    """Re-encrypt under CREDENTIAL_KEY every sealed value that only a
    CREDENTIAL_KEY_PREVIOUS key opens. Run at boot while one is set;
    idempotent, compare-and-set per value (a save racing it wins), never
    writes a value it could not read back. Returns {rotated, current,
    unreadable, key}: `unreadable` > 0 means a value no configured key
    opens — keep the old key until it is 0."""
    out = {"rotated": 0, "current": 0, "unreadable": 0, "key": key_state()}
    if out["key"] != "ok" or not previous_keys():
        return out
    key = (os.getenv("CREDENTIAL_KEY") or "").strip()
    current, every = _single(key), _fernet()
    from models import get_conn, DB_PATH, _invalidate_request_cache
    conn = get_conn(db_path or DB_PATH)
    try:
        for table, col in SEALED_COLUMNS:
            if col not in _table_columns(conn, table):
                continue
            pk = "id" if "id" in _table_columns(conn, table) else "rowid"
            rows = conn.execute(f"SELECT {pk} AS k, {col} AS v FROM {table} "
                                f"WHERE substr({col}, 1, {len(PREFIX)}) = ?", (PREFIX,)).fetchall()
            for r in rows:
                sealed = r["v"]
                body = sealed[len(PREFIX):].encode("ascii")
                try:
                    current.decrypt(body)
                    out["current"] += 1
                    continue
                except Exception:
                    pass
                try:
                    fresh = PREFIX + every.rotate(body).decode("ascii")
                    if current.decrypt(fresh[len(PREFIX):].encode("ascii")) != every.decrypt(body):
                        raise ValueError("round trip")
                except Exception:
                    out["unreadable"] += 1
                    continue
                cur = conn.execute(f"UPDATE {table} SET {col}=? WHERE {pk}=? AND {col}=?", (fresh, r["k"], sealed))
                if cur.rowcount == 1:
                    out["rotated"] += 1
                    if table == "restaurants":
                        _invalidate_request_cache(r["k"])
            conn.commit()
    finally:
        conn.close()
    return out


def encrypt_webhook_secrets(db_path=None) -> int:
    """Seal every customer webhook signing secret still stored plain
    (SEC-14; webhooks.py writes them sealed since 10/19/26). Compare-and-
    set, round-tripped, a no-op without a working key."""
    if key_state() != "ok":
        return 0
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    n = 0
    try:
        if "secret" not in _table_columns(conn, "webhooks"):
            return 0
        for r in conn.execute("SELECT id, secret FROM webhooks WHERE secret IS NOT NULL AND secret <> '' "
                              f"AND substr(secret, 1, {len(PREFIX)}) <> ?", (PREFIX,)).fetchall():
            enc = encrypt(r["secret"])
            if enc.startswith(PREFIX) and decrypt(enc) == r["secret"]:
                n += conn.execute("UPDATE webhooks SET secret=? WHERE id=? AND secret=?",
                                  (enc, r["id"], r["secret"])).rowcount
        conn.commit()
    finally:
        conn.close()
    return n


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


# ── the POS partner credential, held once (Audit #23 F7, A7-01 / A1-14) ─────
#
# RPOWER issues ONE integrator token that reads every store a customer has
# authorised. It was copied, encrypted, into every restaurant row
# (restaurants.rpower_token): a rotation or a leak meant re-pasting it at
# every store and picking each store again, and any one decrypted row read
# every store. It is now one sealed, versioned row of pos_credentials; a
# restaurant keeps only its store (rpower_cg, rpower_store_mid) and the id
# (rpower_credential_id). rpower._creds resolves the token through it, and
# reads the legacy per-row copy only for a row with no credential id.
# Rotation is one write (rotate), and the request budget is keyed by the
# credential id, so it survives a rotation (rpower._bucket_key).
#
# AT_REST_SEALED: columns outside restaurants that hold credentials.PREFIX
# ciphertext at rest — an off-site copy carries them as they are
# (offsite_backup.seal_plan), never scrubbed, so a restore keeps them.
AT_REST_SEALED = (("pos_credentials", "token"),)

POS_CREDENTIALS_SCHEMA = """CREATE TABLE IF NOT EXISTS pos_credentials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT NOT NULL,
    label        TEXT,
    token        TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    created_by   TEXT,
    rotated_at   TEXT,
    rotated_by   TEXT,
    revoked_at   TEXT
)"""

# The legacy per-row copies of a bound token are kept this long after their
# credential was made (a rollback to a build that reads only the row keeps
# working), then cleared at boot (clear_legacy_pos_tokens); a rotation or a
# revoke clears them at once.
LEGACY_TOKEN_KEEP_DAYS = 7
_plain_cache = {}
_PLAIN_CACHE_MAX = 256


def _conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def init_pos_credentials(db_path=None):
    """The table, at boot (models.init_db) — never on a request."""
    conn = _conn(db_path)
    try:
        conn.execute(POS_CREDENTIALS_SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_credentials_fp ON pos_credentials(provider, fingerprint)")
        conn.commit()
    finally:
        conn.close()


def fingerprint(token) -> str:
    """A digest that finds a credential by its token — never the token."""
    import hashlib
    return hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()[:32]


def _open(sealed):
    if not sealed:
        return None
    hit = _plain_cache.get(sealed)
    if hit is not None:
        return hit
    plain = decrypt(sealed)
    if plain is not None and isinstance(sealed, str) and sealed.startswith(PREFIX):
        if len(_plain_cache) >= _PLAIN_CACHE_MAX:
            _plain_cache.clear()
        _plain_cache[sealed] = plain
    return plain


def credential_token(cred_id, db_path=None):
    """The credential's token, or None (unknown, revoked, or sealed under a
    key this server does not hold — undecryptable() pages for that)."""
    if not cred_id:
        return None
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT token, revoked_at FROM pos_credentials WHERE id=?", (int(cred_id),)).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not row or row["revoked_at"]:
        return None
    return _open(row["token"])


def get_credential(cred_id, db_path=None):
    """{id, provider, label, version, fingerprint, created_at, created_by,
    rotated_at, rotated_by, revoked_at, stores} — never the token."""
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT id, provider, label, version, fingerprint, created_at, created_by, rotated_at, "
                           "rotated_by, revoked_at FROM pos_credentials WHERE id=?", (int(cred_id),)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["stores"] = int(conn.execute(
            "SELECT COUNT(*) FROM restaurants WHERE rpower_credential_id=? AND COALESCE(is_demo,0)=0",
            (int(cred_id),)).fetchone()[0])
    finally:
        conn.close()
    out["fingerprint"] = (out.get("fingerprint") or "")[:8]
    return out


def list_credentials(provider="rpower", db_path=None) -> list:
    conn = _conn(db_path)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM pos_credentials WHERE provider=? ORDER BY id",
                                          (provider,)).fetchall()]
    finally:
        conn.close()
    return [c for c in (get_credential(i, db_path) for i in ids) if c]


def stores_on(cred_id, db_path=None) -> int:
    """Live (non-demo) restaurants bound to this credential."""
    conn = _conn(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM restaurants WHERE rpower_credential_id=? "
                                "AND COALESCE(is_demo,0)=0", (int(cred_id),)).fetchone()[0])
    finally:
        conn.close()


def find_credential(provider, token, db_path=None):
    """The id of the live credential holding this token, or None."""
    token = (token or "").strip()
    if not token:
        return None
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT id, token FROM pos_credentials WHERE provider=? AND fingerprint=? "
                            "AND revoked_at IS NULL ORDER BY id DESC", (provider, fingerprint(token))).fetchall()
    finally:
        conn.close()
    for r in rows:
        if _open(r["token"]) == token:
            return int(r["id"])
    return None


def credential_for_token(provider, token, actor=None, label=None, db_path=None) -> int:
    """The live credential holding `token` — made (sealed) if there is none.
    Raises CredentialKeyError where the token cannot be sealed (A7-08)."""
    token = (token or "").strip()
    if not token:
        raise ValueError("a credential needs a token")
    hit = find_credential(provider, token, db_path)
    if hit:
        return hit
    sealed = encrypt(token)
    conn = _conn(db_path)
    try:
        cur = conn.execute("INSERT INTO pos_credentials (provider, label, token, fingerprint, created_by) "
                           "VALUES (?,?,?,?,?)", (provider, (label or "")[:120] or None, sealed,
                                                  fingerprint(token), (actor or "")[:120] or None))
        new_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return new_id


def _invalidate(rids):
    try:
        from models import _invalidate_request_cache
        for rid in rids:
            _invalidate_request_cache(rid)
    except Exception as e:
        log.debug("request cache not invalidated: %s", e)


def rotate(cred_id, new_token, actor=None, db_path=None) -> dict:
    """Replace a credential's token in one write: every store bound to it
    reads the new token on its next request, its version goes up, and the
    legacy per-row copies of the old token are cleared (they are dead or
    leaked). Raises CredentialKeyError where it cannot be sealed. Returns
    {ok, version, stores, legacy_cleared} or {ok: False, error}."""
    new_token = (new_token or "").strip()
    if not new_token:
        return {"ok": False, "error": "Paste the new token."}
    sealed = encrypt(new_token)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT id, version, revoked_at FROM pos_credentials WHERE id=?",
                           (int(cred_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "No such credential."}
        if row["revoked_at"]:
            return {"ok": False, "error": "That credential is revoked; save the token as a new one."}
        version = int(row["version"] or 1) + 1
        conn.execute("UPDATE pos_credentials SET token=?, fingerprint=?, version=?, rotated_at=datetime('now'), "
                     "rotated_by=? WHERE id=?", (sealed, fingerprint(new_token), version,
                                                 (actor or "")[:120] or None, int(cred_id)))
        rids = [int(r[0]) for r in conn.execute("SELECT id FROM restaurants WHERE rpower_credential_id=?",
                                                (int(cred_id),)).fetchall()]
        cleared = conn.execute("UPDATE restaurants SET rpower_token=NULL WHERE rpower_credential_id=? "
                               "AND rpower_token IS NOT NULL", (int(cred_id),)).rowcount
        conn.commit()
    finally:
        conn.close()
    _invalidate(rids)
    return {"ok": True, "version": version, "stores": len(rids), "legacy_cleared": int(cleared or 0)}


def revoke(cred_id, actor=None, db_path=None) -> dict:
    """Retire a credential: every store bound to it reads as not connected
    until it is bound to another (a leaked token RPOWER has not yet
    replaced). The legacy copies go with it. Returns {ok, stores}."""
    conn = _conn(db_path)
    try:
        n = conn.execute("UPDATE pos_credentials SET revoked_at=datetime('now'), rotated_by=? WHERE id=? "
                         "AND revoked_at IS NULL", ((actor or "")[:120] or None, int(cred_id))).rowcount
        rids = [int(r[0]) for r in conn.execute("SELECT id FROM restaurants WHERE rpower_credential_id=?",
                                                (int(cred_id),)).fetchall()]
        conn.execute("UPDATE restaurants SET rpower_token=NULL WHERE rpower_credential_id=?", (int(cred_id),))
        conn.commit()
    finally:
        conn.close()
    _invalidate(rids)
    return {"ok": bool(n), "stores": len(rids)}


def bind_legacy_pos_tokens(db_path=None) -> dict:
    """At boot: every restaurant still holding its own RPOWER token and no
    credential id is bound to the credential that holds that token (made
    once per distinct token). Safe for every store as it stands: the token
    resolved is the same token, so nothing a store reads changes — Simple
    EJ's and Will's demo mirror of it (one token) share one credential. A
    `demo-store:` token (the simulated store) stays on its row: it is not a
    secret and never leaves the process. Idempotent. Returns {bound,
    credentials, skipped, unreadable, refused}."""
    out = {"bound": 0, "credentials": 0, "skipped": 0, "unreadable": 0, "refused": False}
    if must_seal() and key_state() != "ok":
        out["refused"] = True                    # cannot seal here: the rows stay as they are
        return out
    conn = _conn(db_path)
    try:
        try:
            rows = conn.execute("SELECT id, rpower_token FROM restaurants WHERE rpower_token IS NOT NULL "
                                "AND rpower_token <> '' AND rpower_credential_id IS NULL").fetchall()
        except Exception:
            return out                           # a database from before the column
    finally:
        conn.close()
    made = {}
    for r in rows:
        plain = decrypt(r["rpower_token"])
        if not plain:
            out["unreadable"] += 1
            continue
        if plain.startswith("demo-store:"):
            out["skipped"] += 1
            continue
        cid = made.get(plain) or find_credential("rpower", plain, db_path)
        if not cid:
            cid = credential_for_token("rpower", plain, actor="boot: legacy per-row token",
                                       label="RPOWER partner token", db_path=db_path)
            out["credentials"] += 1
        made[plain] = cid
        conn = _conn(db_path)
        try:
            n = conn.execute("UPDATE restaurants SET rpower_credential_id=? WHERE id=? "
                             "AND rpower_credential_id IS NULL", (cid, int(r["id"]))).rowcount
            conn.commit()
        finally:
            conn.close()
        if n:
            out["bound"] += 1
            _invalidate([int(r["id"])])
    return out


def clear_legacy_pos_tokens(keep_days=None, db_path=None) -> int:
    """Clear the per-row RPOWER token of every restaurant bound to a live
    credential made more than `keep_days` ago (LEGACY_TOKEN_KEEP_DAYS) whose
    token still opens — the copies the rollback window kept. Returns rows
    cleared."""
    days = LEGACY_TOKEN_KEEP_DAYS if keep_days is None else int(keep_days)
    conn = _conn(db_path)
    rids, n = [], 0
    try:
        try:
            creds = conn.execute("SELECT id, token FROM pos_credentials WHERE revoked_at IS NULL "
                                 "AND created_at <= datetime('now', ?)", (f"-{days} days",)).fetchall()
        except Exception:
            return 0
        for c in creds:
            if not _open(c["token"]):
                continue                         # cannot read it: keep the copies
            rids += [int(x[0]) for x in conn.execute(
                "SELECT id FROM restaurants WHERE rpower_credential_id=? AND rpower_token IS NOT NULL",
                (int(c["id"]),)).fetchall()]
            n += conn.execute("UPDATE restaurants SET rpower_token=NULL WHERE rpower_credential_id=? "
                              "AND rpower_token IS NOT NULL", (int(c["id"]),)).rowcount
        conn.commit()
    finally:
        conn.close()
    _invalidate(rids)
    return int(n or 0)


def encrypt_pos_credentials(db_path=None) -> int:
    """Seal every pos_credentials token written in plaintext (a laptop with
    no key) once a key works. Compare-and-set, round-tripped."""
    if key_state() != "ok":
        return 0
    conn = _conn(db_path)
    n = 0
    try:
        try:
            rows = conn.execute("SELECT id, token FROM pos_credentials WHERE token IS NOT NULL AND token <> '' "
                                f"AND substr(token, 1, {len(PREFIX)}) <> ?", (PREFIX,)).fetchall()
        except Exception:
            return 0
        for r in rows:
            enc = encrypt(r["token"])
            if enc.startswith(PREFIX) and decrypt(enc) == r["token"]:
                n += conn.execute("UPDATE pos_credentials SET token=? WHERE id=? AND token=?",
                                  (enc, r["id"], r["token"])).rowcount
        conn.commit()
    finally:
        conn.close()
    return n


def key_problem_page() -> bool:
    """At boot, on Railway: page when CREDENTIAL_KEY is not ok (A7-08) —
    credential saves are refused until it is. Returns whether it paged."""
    state = key_state()
    if state == "ok" or not must_seal():
        return False
    _alert("key", f"CREDENTIAL_KEY is {state}", [
        "Credential saves (POS tokens, Google, Meta) are refused until it is fixed, and every stored credential "
        "reads as disconnected.", "Set a valid CREDENTIAL_KEY in Railway and redeploy."])
    return True
