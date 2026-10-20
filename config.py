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


DEFAULT_SALES_EMAIL = "sales@cavnar.ai"


def sales_email() -> str:
    """Where every sales question goes — adding a module, a new restaurant,
    a sales audit (owner, 10/20/26). Support, billing and security keep
    will@cavnar.ai."""
    return (os.getenv("SALES_EMAIL") or DEFAULT_SALES_EMAIL).strip()


DEFAULT_ROLE_EMAILS = {
    "support": "support@cavnar.ai",    # owners' help, "this looks wrong"
    "billing": "billing@cavnar.ai",    # payments, invoices, account holds
    "security": "security@cavnar.ai",  # vulnerability reports, a login that wasn't you
    "privacy": "privacy@cavnar.ai",    # data, deletion and privacy-rights requests
    "legal": "legal@cavnar.ai",        # contracts and legal notices
}


def role_email(role: str) -> str:
    """One of Cavnar AI's role addresses (10/20/26: each a Google Group that
    delivers to Will today and to whoever takes the role later), overridable
    as <ROLE>_EMAIL. sales@ is sales_email()."""
    return (os.getenv(f"{role.upper()}_EMAIL") or DEFAULT_ROLE_EMAILS[role]).strip()


def support_email() -> str:
    return role_email("support")


def billing_email() -> str:
    return role_email("billing")


def security_email() -> str:
    return role_email("security")


def privacy_email() -> str:
    return role_email("privacy")


def legal_email() -> str:
    return role_email("legal")


def from_email() -> str:
    """The address transactional mail is sent from (Resend sender)."""
    return os.getenv("FROM_EMAIL", DEFAULT_SENDER)


def guest_from_email() -> str:
    """The address mail to a restaurant's GUESTS is sent from — newsletters
    and review requests — or '' to keep the one it always used (scale audit
    10/19/26, SEC-02). Guest mail on the same domain as owners' sign-in
    codes and reset links let one tenant's stale list lower the reputation
    of every owner's security mail. Set GUEST_FROM_EMAIL to an address on
    its own verified Resend domain (e.g. news@guests.cavnar.ai)."""
    return (os.getenv("GUEST_FROM_EMAIL") or "").strip()


def will_email() -> str:
    """Where operator mail goes: failure digests, backups, drills, signups."""
    return os.getenv("WILL_EMAIL", DEFAULT_SENDER)


def on_railway() -> bool:
    """True in the production container. Railway sets both variables; a
    local run sets neither. Used for secure cookies, the local-vs-prod
    draft path and the admin status line."""
    return bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))


def scheduling_allowed() -> bool:
    """Whether this process may run scheduled jobs and send what only the
    scheduler's process may send (scheduler.scheduling_allowed delegates
    here). Only on Railway, unless ALLOW_LOCAL_SCHEDULER=1 says otherwise;
    never while a restore (RESTORE_FROM) or the Postgres cutover's write
    freeze (MAINTENANCE_READ_ONLY) is in force.

    Here, not only in scheduler.py, so a gate read from a generation stage
    (push.native_push_allowed, reached through live_activities) never
    imports the scheduler: importing it used to load the production .env
    into a keyless process (blind re-audit 10/11/26 BUGSGEN-3, SCALE-29)."""
    if (os.getenv("RESTORE_FROM") or "").strip():
        return False
    if (os.getenv("MAINTENANCE_READ_ONLY") or "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    if os.getenv("ALLOW_LOCAL_SCHEDULER", "").strip().lower() in ("1", "true", "yes"):
        return True
    # Railway sets all of these on every deploy; any one is enough.
    return any(os.getenv(v) for v in ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID",
                                       "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME"))


def google_places_key() -> str:
    """The Google Places key. Two variable names have been used over time;
    either works, GOOGLE_PLACES_API_KEY first."""
    return os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""


def stripe_api(key=None):
    """The stripe module with the secret key set and a bounded HTTP client.

    Stripe's SDK waits ~80 s per request by default and retries on its own,
    and the billing-page calls run several of those back to back inside one
    of the four request threads (AI-33). Every caller that talks to Stripe
    goes through here, so one place bounds them all: 10 s per request and a
    single SDK retry. The HTTP client is installed once per process; a fake
    module in a test that has no RequestsClient is left as it is."""
    import stripe
    stripe.api_key = key if key is not None else os.getenv("STRIPE_SECRET_KEY", "")
    if not getattr(stripe, "_cavnar_bounded", False):
        try:
            client_cls = getattr(stripe, "RequestsClient", None) or stripe.http_client.RequestsClient
            stripe.default_http_client = client_cls(timeout=10)
            stripe.max_network_retries = 1
            stripe._cavnar_bounded = True
        except Exception:
            pass
    return stripe
