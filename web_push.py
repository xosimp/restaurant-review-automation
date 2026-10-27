"""
web_push.py — Web Push for the installable staff portal, the one wrapper of
the protocol (scheduler audit 10/10/26 MARKET-18; ARCHITECTURE_MANIFEST §3:
third-party protocols are wrapped once — APNs in `push`, Web Push here).

Staff without an iPhone — Android above all — use the portal in a browser.
Installed as an app (staff_pwa: manifest + service worker), it can receive
the same notices the iPhone app gets, by the browser's push service, with no
new dependency: the payload is encrypted per RFC 8291 (aes128gcm, ECDH on
P-256 + HKDF + AES-GCM, `cryptography`) and the request is signed per RFC
8292 (VAPID, an ES256 JWT, `pyjwt`).

Configuration (docs/ops/ENVIRONMENT.md): WEB_PUSH_VAPID_PUBLIC_KEY (the
65-byte uncompressed P-256 point, base64url), WEB_PUSH_VAPID_PRIVATE_KEY
(the 32-byte private scalar, base64url) and WEB_PUSH_SUBJECT
("mailto:..."). Without both keys Web Push is OFF: `enabled()` is False, the
portal never offers it, and people.deliver goes on to text or email as
before — generating the key pair and setting it in Railway is the one
external step.

Endpoints are the browser vendors' push services only (PUSH_HOSTS — an
endpoint is a URL the browser chose, so it is also vetted by net_safety),
every request names a timeout, and a subscription the service says is gone
(404/410) is revoked.
"""
import base64
import json
import logging
import os
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
DEFAULT_TTL = 12 * 3600
RECORD_SIZE = 4096
# The browsers' push services. Anything else is refused before a request.
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com",
              "notify.windows.com", "push.apple.com")


def _b64u_decode(s: str) -> bytes:
    s = (s or "").strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def vapid_keys():
    """(public b64url, private key object) or (None, None) when unset or
    unreadable (never raises)."""
    pub = (os.environ.get("WEB_PUSH_VAPID_PUBLIC_KEY") or "").strip()
    priv = (os.environ.get("WEB_PUSH_VAPID_PRIVATE_KEY") or "").strip()
    if not pub or not priv:
        return None, None
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        key = ec.derive_private_key(int.from_bytes(_b64u_decode(priv), "big"), ec.SECP256R1())
        derived = key.public_key().public_bytes(serialization.Encoding.X962,
                                                serialization.PublicFormat.UncompressedPoint)
        if _b64u(derived) != _b64u(_b64u_decode(pub)):
            log.warning("web push: WEB_PUSH_VAPID_PUBLIC_KEY does not match the private key — Web Push is off")
            return None, None
        return _b64u(derived), key
    except Exception as e:
        log.warning("web push: VAPID keys unreadable — Web Push is off: %s", e)
        return None, None


def enabled() -> bool:
    return vapid_keys()[0] is not None


def endpoint_allowed(endpoint) -> bool:
    try:
        u = urlparse(str(endpoint or ""))
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes = None, server_key=None) -> bytes:
    """The aes128gcm body of RFC 8291 for one subscription: header (salt,
    record size, the server's public key) then one record."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    ua_public = _b64u_decode(p256dh)
    auth_secret = _b64u_decode(auth)
    if len(ua_public) != 65 or len(auth_secret) < 16:
        raise ValueError("that subscription's keys are not a P-256 key and a 16-byte secret")
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = server_key.public_key().public_bytes(serialization.Encoding.X962,
                                                     serialization.PublicFormat.UncompressedPoint)
    shared = server_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    if len(payload) > RECORD_SIZE - 17 - 86:
        raise ValueError("that notice is too long for one push")
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_public)]) + as_public + body


def vapid_header(endpoint, now=None) -> str:
    """'vapid t=<jwt>, k=<public key>' for the endpoint's push service."""
    import jwt
    pub, key = vapid_keys()
    if not pub:
        raise RuntimeError("Web Push is not configured")
    u = urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(now or time.time()) + 12 * 3600,
              "sub": (os.environ.get("WEB_PUSH_SUBJECT") or "mailto:support@cavnar.ai").strip()}
    token = jwt.encode(claims, key, algorithm="ES256")
    return f"vapid t={token}, k={pub}"


def send(subscription: dict, message: dict, ttl=DEFAULT_TTL, urgency="normal") -> dict:
    """POST one encrypted notice to one subscription ({endpoint, p256dh,
    auth}). {ok, status, gone} — `gone` when the push service says the
    subscription no longer exists. Never raises."""
    endpoint = subscription.get("endpoint")
    import config as _config_gate
    if not _config_gate.outbound_sends_allowed():
        # Staging and other non-production environments (A05-06).
        return {"ok": False, "status": None, "gone": False, "error": _config_gate.refused_send("web_push")}
    if not enabled():
        return {"ok": False, "status": None, "gone": False, "error": "Web Push is not configured"}
    if not endpoint_allowed(endpoint):
        return {"ok": False, "status": None, "gone": True, "error": "not a browser push service"}
    try:
        body = encrypt(json.dumps(message, separators=(",", ":")).encode(), subscription["p256dh"], subscription["auth"])
        import net_safety
        r = net_safety.safe_post(endpoint, data=body, timeout=TIMEOUT_SECONDS, headers={
            "Authorization": vapid_header(endpoint), "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream", "TTL": str(int(ttl)),
            "Urgency": urgency if urgency in ("very-low", "low", "normal", "high") else "normal"})
        status = getattr(r, "status_code", None)
        return {"ok": status in (200, 201, 202), "status": status, "gone": status in (404, 410)}
    except Exception as e:
        log.warning("web push failed: %s", e)
        return {"ok": False, "status": None, "gone": False, "error": str(e)[:200]}
