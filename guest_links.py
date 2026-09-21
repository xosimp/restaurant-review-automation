"""Opaque guest opt-in links.

`/join/<restaurant_id>` let anyone walk the customer list by integer and
read each restaurant's name. The link now carries a signature over the id
(HMAC with the app secret), so a guessed id is a 404. The bare-integer
form keeps working while ALLOW_LEGACY_JOIN_LINKS is on, because printed QR
codes exist; turn it off once those are reissued.
"""
import hashlib
import hmac
import os


def _secret():
    return (os.getenv("SECRET_KEY") or os.getenv("CREDENTIAL_KEY") or "cavnar-dev-only").encode()


def sign_join(restaurant_id):
    sig = hmac.new(_secret(), f"join:{int(restaurant_id)}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"{int(restaurant_id)}-{sig}"


def verify_join(token):
    """The restaurant id behind a join token, or None."""
    token = str(token if token is not None else "").strip()
    if not token:
        return None
    if token.isdigit():
        if os.getenv("ALLOW_LEGACY_JOIN_LINKS", "1") == "1":
            return int(token)
        return None
    rid, _, sig = token.partition("-")
    if not rid.isdigit() or not sig:
        return None
    expected = sign_join(rid).split("-", 1)[1]
    return int(rid) if hmac.compare_digest(sig, expected) else None
