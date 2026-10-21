"""Opaque guest opt-in links.

`/join/<restaurant_id>` let anyone walk the customer list by integer and
read each restaurant's name. The link now carries a signature over the id,
so a guessed id is a 404. The bare-integer form is refused unless
ALLOW_LEGACY_JOIN_LINKS=1 is set: no restaurant has a printed bare-id QR
code, and accepting it let anyone reach every restaurant's opt-in form by
counting (MOD-MKT-9).

Signed with a KEPT, versioned secret (models.kept_secret "join_links", #133),
not SECRET_KEY. These are printed table-tent QR codes: rotating SECRET_KEY
used to 404 every one of them, silently. A token is `<rid>-v<n>.<sig>`; the
version it names is the secret it verifies against, so a deliberate rotation
(models.rotate_kept_secret) signs new codes with the next version while every
code already printed keeps working. The older `<rid>-<sig>` form, signed with
SECRET_KEY, still verifies for as long as that key is unchanged.
"""
import hashlib
import hmac
import os

_JOIN_SECRET = "join_links"


def _legacy_secret():
    """The key the older `<rid>-<sig>` links verify against: SECRET_KEY only.
    It used to fall back to CREDENTIAL_KEY — the encryption key doubling as
    a link-signing key — and then to a constant in this file (Audit #23
    A7-18). The constant is a laptop's only; on Railway no SECRET_KEY means
    no legacy link verifies (None)."""
    key = (os.getenv("SECRET_KEY") or "").strip()
    if key:
        return key.encode()
    try:
        import config
        if config.on_railway():
            return None
    except Exception:
        return None
    return b"cavnar-dev-only"


def _legacy_sig(restaurant_id):
    secret = _legacy_secret()
    if secret is None:
        return None
    return hmac.new(secret, f"join:{int(restaurant_id)}".encode(), hashlib.sha256).hexdigest()[:16]


def _sig(restaurant_id, version):
    from models import kept_secret
    key = kept_secret(_JOIN_SECRET, version)
    if not key:
        return None
    return hmac.new(key, f"join:{int(restaurant_id)}".encode(), hashlib.sha256).hexdigest()[:16]


def sign_join(restaurant_id):
    from models import kept_secret_version
    version = kept_secret_version(_JOIN_SECRET)
    return f"{int(restaurant_id)}-v{version}.{_sig(restaurant_id, version)}"


def verify_join(token):
    """The restaurant id behind a join token, or None."""
    token = str(token if token is not None else "").strip()
    if not token:
        return None
    if token.isdigit():
        if os.getenv("ALLOW_LEGACY_JOIN_LINKS", "0") == "1":
            return int(token)
        return None
    rid, _, sig = token.partition("-")
    if not rid.isdigit() or not sig:
        return None
    if sig.startswith("v") and "." in sig:
        ver, _, mac = sig[1:].partition(".")
        if not ver.isdigit() or not mac:
            return None
        expected = _sig(rid, int(ver))
        return int(rid) if expected and hmac.compare_digest(mac, expected) else None
    legacy = _legacy_sig(rid)
    return int(rid) if legacy and hmac.compare_digest(sig, legacy) else None
