"""
docusign_helper.py — Cavnar AI DocuSign integration
Sends service agreements automatically when a new client is created.
Uses JWT authentication with RSA keypair.
"""
import os

INTEGRATION_KEY = os.getenv("DOCUSIGN_INTEGRATION_KEY", "")
USER_ID         = os.getenv("DOCUSIGN_USER_ID", "")
ACCOUNT_ID      = os.getenv("DOCUSIGN_ACCOUNT_ID", "")
TEMPLATE_ID     = os.getenv("DOCUSIGN_TEMPLATE_ID", "")
BASE_URL        = os.getenv("DOCUSIGN_BASE_URL", "https://demo.docusign.net")
PRIVATE_KEY     = os.getenv("DOCUSIGN_PRIVATE_KEY", "")
# OAuth callback registered on the integration key (must match exactly).
REDIRECT_URI    = os.getenv("DOCUSIGN_REDIRECT_URI", "https://dashboard.cavnar.ai/docusign/callback")


def is_demo(base_url: str = None) -> bool:
    """The developer sandbox lives at demo.docusign.net; every production
    account is on a shard like na4.docusign.net / www.docusign.net."""
    return "demo.docusign.net" in (base_url if base_url is not None else BASE_URL)


def auth_host(base_url: str = None) -> str:
    """The OAuth server follows the environment: account-d.docusign.com for
    the developer sandbox, account.docusign.com for production. Before Sep
    2026 this was hard-coded to the sandbox host, so flipping
    DOCUSIGN_BASE_URL to a production shard would have kept minting sandbox
    tokens and every production send would have failed with 401.
    DOCUSIGN_AUTH_HOST overrides the inference if DocuSign ever changes it."""
    override = os.getenv("DOCUSIGN_AUTH_HOST", "").strip()
    if override:
        return override
    return "account-d.docusign.com" if is_demo(base_url) else "account.docusign.com"


def consent_url(base_url: str = None) -> str:
    """One-time consent link for JWT impersonation. Open it while logged in
    to the DocuSign account that matches the environment (sandbox or
    production) and click Allow; consent is per environment."""
    from urllib.parse import urlencode
    q = urlencode({
        "response_type": "code",
        "scope": "signature impersonation",
        "client_id": INTEGRATION_KEY,
        "redirect_uri": REDIRECT_URI,
    })
    return f"https://{auth_host(base_url)}/oauth/auth?{q}"


# Every outbound call here is timed out. These were the only three requests
# in the codebase without one (AST-checked: 43 of 46 already had it), and
# with gunicorn running --workers 1 --threads 4 a single hung DocuSign call
# holds a quarter of the platform's total request capacity until the TCP
# stack gives up, which can be minutes. Signing is interactive, so the
# budget is generous but finite.
DOCUSIGN_TIMEOUT = (5, 30)   # (connect, read) seconds


def get_access_token() -> str:
    """Get a DocuSign access token using JWT authentication."""
    import jwt
    import time
    import requests

    # Clean up private key
    private_key = PRIVATE_KEY.replace("\\n", "\n")
    if not private_key.startswith("-----"):
        raise ValueError("DOCUSIGN_PRIVATE_KEY is not set or invalid")

    now = int(time.time())
    auth_domain = auth_host()

    # Use integration key as sub (works for both demo and production JWT auth)
    sub = USER_ID if USER_ID else INTEGRATION_KEY
    payload = {
        "iss": INTEGRATION_KEY,
        "sub": sub,
        "aud": auth_domain,
        "iat": now,
        "exp": now + 3600,
        "scope": "signature impersonation",
    }

    token = jwt.encode(payload, private_key, algorithm="RS256")
    auth_url = f"https://{auth_domain}/oauth/token"
    resp = requests.post(auth_url, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": token,
    }, timeout=DOCUSIGN_TIMEOUT)

    if resp.status_code != 200:
        raise Exception(f"DocuSign auth failed: {resp.status_code} {resp.text}")

    return resp.json()["access_token"]


# DocuSign's own reminders (#26): the first two days after sending, then
# every three, until the client signs or the envelope expires. Set on every
# envelope we create and re-applied when one is resent, so "Contract still
# unsigned" is no longer a manual chase.
REMINDER_DELAY_DAYS = 2
REMINDER_FREQUENCY_DAYS = 3
EXPIRE_AFTER_DAYS = 120


def _notification() -> dict:
    return {
        "useAccountDefaults": "false",
        "reminders": {"reminderEnabled": "true", "reminderDelay": str(REMINDER_DELAY_DAYS),
                      "reminderFrequency": str(REMINDER_FREQUENCY_DAYS)},
        "expirations": {"expireEnabled": "true", "expireAfter": str(EXPIRE_AFTER_DAYS), "expireWarn": "0"},
    }


def _capture(exc, what, restaurant_id=None):
    """A failed send used to be a print and a line in the admin modal
    (#144); it reaches the console's failures now."""
    try:
        import ops
        ops.capture(exc, job=f"docusign_{what}",
                    context=(f"restaurant_id={restaurant_id}" if restaurant_id else "docusign"))
    except Exception:
        pass


def send_contract(
    owner_email: str,
    owner_name: str,
    restaurant_name: str,
    module_count: int,
    modules_list: str,
    restaurant_id: int = None,
) -> dict:
    """
    Send a service agreement via DocuSign to a new client.
    Returns dict with status and envelope_id. Raises on failure, after
    recording it (ops.capture) so it reaches the console, not only the
    modal that asked.
    """
    try:
        return _send_contract(owner_email, owner_name, restaurant_name, module_count, modules_list)
    except Exception as e:
        _capture(e, "send", restaurant_id)
        raise


def _api():
    """The account's REST base and auth headers, on a fresh JWT token."""
    return (f"{BASE_URL}/restapi/v2.1/accounts/{ACCOUNT_ID}",
            {"Authorization": f"Bearer {get_access_token()}", "Content-Type": "application/json"})


def resend_envelope(envelope_id: str, restaurant_id: int = None) -> dict:
    """Re-send an envelope the client has not signed yet — the same envelope,
    never a new one (#26: every resend used to mint a new envelope, so a
    client could end up with three contracts in their inbox) — with the
    reminders turned on. {ok, status, resendable}; resendable False for an
    envelope that is completed, declined or voided (those need a new one)."""
    import requests
    try:
        base, headers = _api()
        resp = requests.get(f"{base}/envelopes/{envelope_id}", headers=headers, timeout=DOCUSIGN_TIMEOUT)
        if resp.status_code != 200:
            raise Exception(f"DocuSign envelope lookup failed: {resp.status_code} {resp.text[:300]}")
        status = ((resp.json() or {}).get("status") or "").lower()
        if status in ("completed", "declined", "voided"):
            return {"ok": False, "status": status, "resendable": False}
        r1 = requests.put(f"{base}/envelopes/{envelope_id}/notification", headers=headers,
                          json=_notification(), timeout=DOCUSIGN_TIMEOUT)
        if r1.status_code not in (200, 201):
            raise Exception(f"DocuSign reminder update failed: {r1.status_code} {r1.text[:300]}")
        r2 = requests.put(f"{base}/envelopes/{envelope_id}", headers=headers,
                          params={"resend_envelope": "true"}, json={}, timeout=DOCUSIGN_TIMEOUT)
        if r2.status_code not in (200, 201):
            raise Exception(f"DocuSign resend failed: {r2.status_code} {r2.text[:300]}")
        return {"ok": True, "status": status, "resendable": True}
    except Exception as e:
        _capture(e, "resend", restaurant_id)
        raise


def _send_contract(
    owner_email: str,
    owner_name: str,
    restaurant_name: str,
    module_count: int,
    modules_list: str,
) -> dict:
    if not all([INTEGRATION_KEY, USER_ID, ACCOUNT_ID, TEMPLATE_ID, PRIVATE_KEY]):
        missing = [k for k, v in {
            "INTEGRATION_KEY": INTEGRATION_KEY,
            "USER_ID": USER_ID,
            "ACCOUNT_ID": ACCOUNT_ID,
            "TEMPLATE_ID": TEMPLATE_ID,
            "PRIVATE_KEY": PRIVATE_KEY,
        }.items() if not v]
        raise ValueError(f"Missing DocuSign env vars: {missing}")

    from pricing import plan_for, money
    plan = plan_for(module_count)
    setup_fee    = money(plan["setup"])
    monthly_fee  = f"{money(plan['monthly'])} / month"
    annual_fee   = f"{money(plan['annual'])} / year"

    access_token = get_access_token()

    import requests
    api_base = f"{BASE_URL}/restapi/v2.1/accounts/{ACCOUNT_ID}"
    headers  = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
    }

    # Build envelope from template
    envelope = {
        "templateId": TEMPLATE_ID,
        "status": "sent",
        # DocuSign reminds the client on its own (#26).
        "notification": _notification(),
        "emailSubject": f"Your Cavnar AI Service Agreement — {restaurant_name}",
        "emailBlurb": (
            f"Hi {owner_name} — please review and sign the attached service agreement "
            f"for Cavnar AI. Once signed I'll get your dashboard built and send over "
            f"your payment link. Takes about 2 minutes. — Will Cavnar, Cavnar AI"
        ),
        "templateRoles": [
            {
                # The template's second signer role is named "Admin" (verified
                # against the live template, Sep 2026). A roleName that doesn't
                # match a template role is silently dropped by DocuSign, which
                # left this envelope relying on the template's pre-filled
                # values — fine while they're Will's, but say the real name.
                "roleName":  "Admin",
                "name":      "Will Cavnar",
                "email":     "will@cavnar.ai",
            },
            {
                "roleName":  "Client",
                "name":      owner_name,
                "email":     owner_email,
                "tabs": {
                    "textTabs": [
                        {"tabLabel": "setup_fee",       "value": setup_fee,       "locked": "true"},
                        {"tabLabel": "monthly_fee",     "value": monthly_fee,     "locked": "true"},
                        {"tabLabel": "annual_fee",      "value": annual_fee,      "locked": "true"},
                        {"tabLabel": "modules",         "value": modules_list or plan["label"], "locked": "true"},
                        {"tabLabel": "restaurant_name", "value": restaurant_name, "locked": "true"},
                        {"tabLabel": "owner_name",      "value": owner_name,      "locked": "true"},
                        {"tabLabel": "owner_email",     "value": owner_email,     "locked": "true"},
                    ],
                },
            },
        ],
    }

    resp = requests.post(
        f"{api_base}/envelopes",
        headers=headers,
        json=envelope,
        timeout=DOCUSIGN_TIMEOUT,
    )

    if resp.status_code not in (200, 201):
        raise Exception(f"DocuSign envelope failed: {resp.status_code} {resp.text}")

    data = resp.json()
    return {
        "ok": True,
        "envelope_id": data.get("envelopeId"),
        "status": data.get("status"),
    }

