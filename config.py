"""
config.py — the environment values more than one module reads.

Each accessor reads the environment at call time (a rotated value is
picked up by the next call; tests can monkeypatch.setenv) and carries the
one default. Before this, BASE_URL was read in ten modules with three
different defaults, the sender address in six, the Google Places key in
five with two different variable names, and "are we on Railway" with two
different tests. Stdlib only: this module is imported by everything and
imports nothing of ours.

Values read in ONE module stay in that module (models.DB_PATH, the Stripe
and DocuSign secrets, ADMIN_USERNAME, …); this is for the shared ones.
"""
import os

DEFAULT_BASE_URL = "https://dashboard.cavnar.ai"
DEFAULT_SENDER = "will@cavnar.ai"


def base_url() -> str:
    """The dashboard's public origin, no trailing slash. Every link in an
    email, an SMS or a push payload is built on this."""
    return (os.getenv("BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def from_email() -> str:
    """The address transactional mail is sent from (Resend sender)."""
    return os.getenv("FROM_EMAIL", DEFAULT_SENDER)


def will_email() -> str:
    """Where operator mail goes: failure digests, backups, drills, signups."""
    return os.getenv("WILL_EMAIL", DEFAULT_SENDER)


def on_railway() -> bool:
    """True in the production container. Railway sets both variables; a
    local run sets neither. Used for secure cookies, the local-vs-prod
    draft path and the admin status line."""
    return bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))


def google_places_key() -> str:
    """The Google Places key. Two variable names have been used over time;
    either works, GOOGLE_PLACES_API_KEY first."""
    return os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
