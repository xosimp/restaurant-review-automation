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
    # For demo: account-d.docusign.com, for prod: account.docusign.com
    # Integration key lives on developer account (apps-d.docusign.com)
    # JWT auth always goes through account-d regardless of API base URL
    auth_domain = "account-d.docusign.com"

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

    setup_fee    = f"${module_count * 500:,}"
    monthly_fee  = f"${module_count * 300:,}/mo"

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
                "roleName":  "Will Cavnar",
                "name":      "Will Cavnar",
                "email":     "will@cavnar.ai",
            },
            {
                "roleName":  "Client",
                "name":      owner_name,
                "email":     owner_email,
                "tabs": {
                    "textTabs": [
                        {
                            "tabLabel": "setup_fee",
                            "value":    setup_fee,
                        },
                        {
                            "tabLabel": "monthly_fee",
                            "value":    monthly_fee,
                        },
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
