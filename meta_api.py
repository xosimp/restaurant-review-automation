"""
meta_api.py — shared Meta (Facebook/Instagram) Graph API version + helpers.

Every Graph API call in the app was hardcoded to v19.0 across
social_routes.py, admin_routes.py, and scheduler.py (19 call sites). Meta
retires each API version roughly 2 years after release — v19.0 shipped
Feb 2024, so it is at real risk of already being rejected outright. One
constant now controls every call site; bump META_GRAPH_VERSION in Railway
env vars the moment Meta's developer dashboard flags a deprecation instead
of hunting down 19 literals.
"""
import os

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")
GRAPH_BASE = "https://graph.facebook.com"


def graph_url(path: str) -> str:
    """Build a versioned Graph API URL. Pass the path with or without a
    leading slash — e.g. graph_url("me/accounts") or graph_url(f"{page_id}/feed")."""
    return f"{GRAPH_BASE}/{GRAPH_VERSION}/{path.lstrip('/')}"


def oauth_dialog_url(params: str) -> str:
    """The user-facing (non-Graph) OAuth dialog also carries a version prefix."""
    return f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?{params}"
