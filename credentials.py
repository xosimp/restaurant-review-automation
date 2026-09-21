"""Encryption at rest for the credentials the platform holds on behalf of
its clients: Google Business Profile, Instagram/Facebook and Toast tokens.

Before this they were plain columns on `restaurants`, so a read of the
volume — or of any of the fourteen nightly snapshots beside the database —
was every tenant's Google listing. Now `update_restaurant` encrypts these
fields on the way in and `get_restaurant` decrypts them on the way out;
the rest of the codebase reads `restaurant.gmb_refresh_token` exactly as
before.

Values carry a prefix so plaintext rows written before the key existed,
and encrypted rows, decode side by side; migrating the old rows is a
matter of re-saving them. With no CREDENTIAL_KEY set the functions pass
values through untouched and say so once — the product keeps working, the
gap is logged rather than hidden.
"""
import os

FIELDS = ("gmb_access_token", "gmb_refresh_token", "ig_token", "fb_page_token",
          "toast_client_secret", "toast_access_token", "toast_refresh_token")
PREFIX = "enc:v1:"
_warned = False
_fernet_cache = {}


def key_configured():
    return bool((os.getenv("CREDENTIAL_KEY") or "").strip())


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
