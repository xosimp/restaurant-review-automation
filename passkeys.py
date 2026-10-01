"""Passkeys: sign in with Face ID, Touch ID or a security key (owner, 9/30/26).

WebAuthn through Duo Labs' py_webauthn (attestation "none": we trust the
signature, not the device's make). Two ceremonies:

  register  - a signed-in login adds a passkey from Account -> Security,
              after typing its password (the step-up), so a session left
              open on a shared computer cannot plant one;
  sign in   - the login page asks the browser for any passkey for this
              site (discoverable credentials, no username typed); the
              passkey's own id names the login.

Every challenge is stored server-side, single use, PASSKEY_CHALLENGE_MINUTES
old at most. User verification (the Face ID / PIN step) is REQUIRED on both,
so a passkey is something you have plus something you are or know - which is
why a passkey sign-in counts as this sign-in's second factor (auth_routes).

The relying party is the host the page is served from, when it is this
site's own (config.base_url), localhost, or one listed in PASSKEY_HOSTS
(an ngrok tunnel for testing): a passkey is bound to that host by the
browser, so one made on dashboard.cavnar.ai never answers anywhere else.
Tables: auth.AUTH_SCHEMA user_passkeys, passkey_challenges.
"""
import json
import os
import secrets
from urllib.parse import urlparse

import models as _models_mod


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md's bound-import
    hazard: a test's patch of models.get_conn must reach every call)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


PASSKEY_CHALLENGE_MINUTES = 5
MAX_PASSKEYS_PER_LOGIN = 10
RP_NAME = "Cavnar AI"


class PasskeyError(Exception):
    """A ceremony that cannot finish; the message is safe to show."""


# ── relying party ────────────────────────────────────────────────────────────

def relying_party(host: str):
    """(rp_id, origin) for a request to `host` ("dashboard.cavnar.ai",
    "localhost:5050"), or None when the host is not one of ours."""
    import config
    host = (host or "").strip().lower()
    name = host.split(":")[0]
    ours = (urlparse(config.base_url()).hostname or "").lower()
    extra = {h.strip().lower() for h in (os.getenv("PASSKEY_HOSTS") or "").split(",") if h.strip()}
    local = name in ("localhost", "127.0.0.1")
    if not name or not (name == ours or local or name in extra):
        return None
    return name, ("http://" if local else "https://") + host


# ── challenges ───────────────────────────────────────────────────────────────

def _new_challenge(purpose: str, user_id=None, db_path=None) -> bytes:
    raw = secrets.token_bytes(32)
    from webauthn.helpers import bytes_to_base64url
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM passkey_challenges WHERE created_at < datetime('now', ?)",
                     (f"-{PASSKEY_CHALLENGE_MINUTES} minutes",))
        conn.execute("INSERT INTO passkey_challenges (challenge, purpose, user_id) VALUES (?,?,?)",
                     (bytes_to_base64url(raw), purpose, user_id))
        conn.commit()
    finally:
        conn.close()
    return raw


def _take_challenge(credential: dict, purpose: str, user_id=None, db_path=None) -> bytes:
    """The challenge the browser signed, if we issued it for this purpose
    (and this login) within the window - consumed either way."""
    import base64
    from webauthn.helpers import base64url_to_bytes
    try:
        cdj = (credential.get("response") or {}).get("clientDataJSON") or ""
        client = json.loads(base64.urlsafe_b64decode(cdj + "=" * (-len(cdj) % 4)))
        challenge = str(client.get("challenge") or "")
    except Exception:
        raise PasskeyError("That passkey response could not be read. Try again.")
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT purpose, user_id FROM passkey_challenges WHERE challenge=? "
            "AND created_at >= datetime('now', ?)",
            (challenge, f"-{PASSKEY_CHALLENGE_MINUTES} minutes")).fetchone()
        conn.execute("DELETE FROM passkey_challenges WHERE challenge=?", (challenge,))
        conn.commit()
    finally:
        conn.close()
    if not row or row["purpose"] != purpose or (user_id is not None and row["user_id"] != user_id):
        raise PasskeyError("That request expired. Try again.")
    return base64url_to_bytes(challenge)


# ── stored passkeys ──────────────────────────────────────────────────────────

def list_passkeys(user_id: int, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, created_at, last_used_at, backed_up FROM user_passkeys "
            "WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def remove_passkey(user_id: int, passkey_id: int, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        n = conn.execute("DELETE FROM user_passkeys WHERE id=? AND user_id=?", (passkey_id, user_id)).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(n)


def _device_name(user_agent: str) -> str:
    ua = user_agent or ""
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                         ("Macintosh", "Mac"), ("Windows", "Windows"), ("CrOS", "Chromebook")):
        if needle in ua:
            return name
    return "Passkey"


# ── register (signed in, password just typed) ────────────────────────────────

def registration_options(user: dict, host: str, db_path=None) -> dict:
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)
    rp = relying_party(host)
    if not rp:
        raise PasskeyError("Passkeys can only be added on dashboard.cavnar.ai.")
    mine = _credential_ids(user["id"], db_path)
    if len(mine) >= MAX_PASSKEYS_PER_LOGIN:
        raise PasskeyError(f"This login already has {MAX_PASSKEYS_PER_LOGIN} passkeys. Remove one first.")
    opts = generate_registration_options(
        rp_id=rp[0], rp_name=RP_NAME,
        user_id=f"cavnar-user-{user['id']}".encode(),
        user_name=user.get("username") or user.get("email") or str(user["id"]),
        user_display_name=user.get("display_name") or user.get("username") or "",
        challenge=_new_challenge("register", user["id"], db_path),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c)) for c in mine])
    return json.loads(options_to_json(opts))


def finish_registration(user: dict, credential: dict, host: str, user_agent: str = "", db_path=None) -> dict:
    from webauthn import verify_registration_response
    from webauthn.helpers import bytes_to_base64url
    rp = relying_party(host)
    if not rp:
        raise PasskeyError("Passkeys can only be added on dashboard.cavnar.ai.")
    challenge = _take_challenge(credential, "register", user["id"], db_path)
    try:
        v = verify_registration_response(credential=credential, expected_challenge=challenge,
                                         expected_rp_id=rp[0], expected_origin=rp[1],
                                         require_user_verification=True)
    except Exception:
        raise PasskeyError("That passkey could not be verified. Try again.")
    cred_id = bytes_to_base64url(v.credential_id)
    transports = (credential.get("response") or {}).get("transports") or []
    conn = get_conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM user_passkeys WHERE credential_id=?", (cred_id,)).fetchone():
            raise PasskeyError("That passkey is already saved.")
        cur = conn.execute(
            "INSERT INTO user_passkeys (user_id, credential_id, public_key, sign_count, transports, aaguid, "
            "backed_up, name) VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], cred_id, v.credential_public_key, int(v.sign_count or 0),
             json.dumps([str(t) for t in transports])[:200], str(v.aaguid or ""),
             1 if getattr(v, "credential_backed_up", False) else 0, _device_name(user_agent)))
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()
    return {"id": pid, "name": _device_name(user_agent)}


def _credential_ids(user_id: int, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT credential_id FROM user_passkeys WHERE user_id=?",
                                           (user_id,)).fetchall()]
    finally:
        conn.close()


# ── sign in (nobody signed in yet) ───────────────────────────────────────────

def authentication_options(host: str, db_path=None) -> dict:
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers.structs import UserVerificationRequirement
    rp = relying_party(host)
    if not rp:
        raise PasskeyError("Passkeys work on dashboard.cavnar.ai.")
    opts = generate_authentication_options(rp_id=rp[0], challenge=_new_challenge("login", None, db_path),
                                           user_verification=UserVerificationRequirement.REQUIRED)
    return json.loads(options_to_json(opts))


def finish_authentication(credential: dict, host: str, db_path=None) -> int:
    """The user id this passkey belongs to, once its signature, challenge,
    origin, user verification and counter all check out."""
    from webauthn import verify_authentication_response
    rp = relying_party(host)
    if not rp:
        raise PasskeyError("Passkeys work on dashboard.cavnar.ai.")
    challenge = _take_challenge(credential, "login", None, db_path)
    cred_id = str(credential.get("id") or credential.get("rawId") or "")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM user_passkeys WHERE credential_id=?", (cred_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise PasskeyError("That passkey isn't saved on any Cavnar AI login. Sign in with your password, "
                           "then add it under Account → Security.")
    try:
        v = verify_authentication_response(credential=credential, expected_challenge=challenge,
                                           expected_rp_id=rp[0], expected_origin=rp[1],
                                           credential_public_key=row["public_key"],
                                           credential_current_sign_count=int(row["sign_count"] or 0),
                                           require_user_verification=True)
    except Exception:
        raise PasskeyError("That passkey could not be verified. Try again.")
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE user_passkeys SET sign_count=?, last_used_at=datetime('now') WHERE id=?",
                     (int(v.new_sign_count or 0), row["id"]))
        conn.commit()
    finally:
        conn.close()
    return int(row["user_id"])
