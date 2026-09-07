"""
docusign_helper.py — Cavnar AI DocuSign integration
Sends service agreements automatically when a new client is created.
Uses JWT authentication with RSA keypair.
"""
import os
import json
import base64
from datetime import datetime, timedelta

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
    })

    if resp.status_code != 200:
        raise Exception(f"DocuSign auth failed: {resp.status_code} {resp.text}")

    return resp.json()["access_token"]


def send_contract(
    owner_email: str,
    owner_name: str,
    restaurant_name: str,
    module_count: int,
    modules_list: str,
) -> dict:
    """
    Send a service agreement via DocuSign to a new client.
    Returns dict with status and envelope_id.
    """
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
    )

    if resp.status_code not in (200, 201):
        raise Exception(f"DocuSign envelope failed: {resp.status_code} {resp.text}")

    data = resp.json()
    return {
        "ok": True,
        "envelope_id": data.get("envelopeId"),
        "status": data.get("status"),
    }


def get_envelope_status(envelope_id: str) -> dict:
    """Check the status of a sent envelope."""
    access_token = get_access_token()
    import requests
    resp = requests.get(
        f"{BASE_URL}/restapi/v2.1/accounts/{ACCOUNT_ID}/envelopes/{envelope_id}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if resp.status_code != 200:
        return {"ok": False, "error": resp.text}
    data = resp.json()
    return {
        "ok": True,
        "status": data.get("status"),
        "completed": data.get("completedDateTime"),
        "sent": data.get("sentDateTime"),
    }
