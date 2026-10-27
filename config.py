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
import logging
import os
import threading

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


def build_id() -> str:
    """The commit this process serves, its first 12 characters
    (RAILWAY_GIT_COMMIT_SHA, which Railway sets on every deploy, else
    GIT_COMMIT), or "" when it is not known — a local run. A page renders
    it (`cavnar_build`, hosted_dashboard's context processor) so the fetch
    wrapper (_csrf_fetch.html) can tell when a JSON answer's X-Cavnar-Build
    names a newer build than the JavaScript on screen (audit 10/26/26
    A06-13)."""
    return build_sha()[:12]


def scheduling_allowed() -> bool:
    """Whether this process may run scheduled jobs and send what only the
    scheduler's process may send (scheduler.scheduling_allowed delegates
    here). Only on Railway, unless ALLOW_LOCAL_SCHEDULER=1 says otherwise;
    never while a restore (RESTORE_FROM) or the Postgres cutover's write
    freeze (MAINTENANCE_READ_ONLY) is in force, nor while restore_guard
    holds a database restored under the running service (restore_frozen).

    Here, not only in scheduler.py, so a gate read from a generation stage
    (push.native_push_allowed, reached through live_activities) never
    imports the scheduler: importing it used to load the production .env
    into a keyless process (blind re-audit 10/11/26 BUGSGEN-3, SCALE-29)."""
    if (os.getenv("RESTORE_FROM") or "").strip():
        return False
    if (os.getenv("MAINTENANCE_READ_ONLY") or "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    if restore_frozen():
        return False
    if os.getenv("ALLOW_LOCAL_SCHEDULER", "").strip().lower() in ("1", "true", "yes"):
        return True
    if not railway_marked():
        return False
    # ...and only the production environment schedules (release-safety audit
    # 10/26/26 A05-06): a staging copy made with "duplicate environment"
    # carries production's keys, and its scheduler would brief, alert and
    # text real people. "local" here is a Railway process that names no
    # environment at all, which has never been seen — kept as it was.
    return deploy_env() in ("production", "local")


# ── Which deployment this is, and whether it may reach real people ──────────
# (release-safety audit 10/26/26, A05-06 / R8). CAVNAR_ENV names it; without
# it Railway's own environment name does (production's is "production", so
# production needs no new variable); a laptop or a test run is "local".
# Staging is any other name: it boots, serves and runs its queue like
# production, but its scheduler stands down (scheduling_allowed) and every
# send point — emails.deliver, notify's Twilio send, APNs and Web Push,
# Stripe with a live key, DocuSign outside its demo account — refuses
# (outbound_sends_allowed), whatever keys it carries.

_TRUTHY = ("1", "true", "yes", "on")


def deploy_env() -> str:
    """"production" | "staging" | "local" | another environment's name,
    lower-cased: CAVNAR_ENV, else RAILWAY_ENVIRONMENT_NAME (or the older
    RAILWAY_ENVIRONMENT, which Railway sets to the same name), else
    "local"."""
    for var in ("CAVNAR_ENV", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT"):
        value = (os.getenv(var) or "").strip().lower()
        if value:
            return value
    return "local"


def is_production() -> bool:
    return deploy_env() == "production"


def outbound_sends_allowed() -> bool:
    """Whether this process may email, text, push, charge or send contracts
    to real people. True in production and on a laptop ("local" — the owner
    and login flows a local backend sends today, CLAUDE.md, are deliberate;
    the scheduler's sends are held there by scheduling_allowed); False in
    any other named environment (staging, a PR environment, a duplicated
    one) unless ALLOW_NONPROD_SENDS=1 says otherwise, deliberately — e.g. a
    staging that holds only test keys and test addresses."""
    if deploy_env() in ("production", "local"):
        return True
    return (os.getenv("ALLOW_NONPROD_SENDS") or "").strip().lower() in _TRUTHY


def sends_refused_reason() -> str:
    """The one sentence a refused send records: names the environment."""
    return (f"outbound sends are off in the {deploy_env()!r} environment "
            "(CAVNAR_ENV / RAILWAY_ENVIRONMENT_NAME is not production; ALLOW_NONPROD_SENDS=1 overrides)")


# Every send the gate refused in this process, by channel: {channel: {count,
# first_at, last_at}} (blind re-audit 10/27/26). A refused send used to leave
# no trace at all — no ledger row, no log — so a production whose
# environment name was not exactly "production" went silent with nothing to
# show for it. /health and the console read `refused_sends()`.
_REFUSED_SENDS = {}
_REFUSED_LOCK = threading.Lock()


def refused_send(channel) -> str:
    """Records one send refused by the environment gate on `channel`
    ("email", "sms", "push", "rpower_schedule", "meta_post" …) — a counter,
    and a warning in the log on the first refusal per channel and every
    hundredth after — and returns sends_refused_reason() for the caller's
    answer. Every refusing send point calls this, never the reason alone."""
    from datetime import datetime as _dt, timezone as _tz
    channel = str(channel or "send")[:40]
    now = _dt.now(_tz.utc).isoformat(timespec="seconds")
    with _REFUSED_LOCK:
        row = _REFUSED_SENDS.setdefault(channel, {"count": 0, "first_at": now, "last_at": now})
        row["count"] += 1
        row["last_at"] = now
        n = row["count"]
    reason = sends_refused_reason()
    if n == 1 or n % 100 == 0:
        logging.getLogger("config").warning("send refused (%s, %d in this process): %s", channel, n, reason)
    return reason


def refused_sends() -> dict:
    """{channel: {count, first_at, last_at}} for this process (a copy)."""
    with _REFUSED_LOCK:
        return {k: dict(v) for k, v in _REFUSED_SENDS.items()}


def environment_problem() -> str:
    """"" when this deployment's name is as meant; else the /health problem
    code "not_production_env": a production-looking Railway service whose
    name stands its scheduler and every send down (environment_name_problem
    below, the one check — /health, the boot logs and the console all read
    it; blind re-audit 10/27/26)."""
    return "not_production_env" if environment_name_problem() else ""


def report_environment_at_boot(logger=None) -> dict:
    """Logs which deployment this is and what it may do — deploy_env(),
    scheduling_allowed(), outbound_sends_allowed() — once, at boot of the
    queue worker (worker.py; the web process logs it through
    status_manager.boot_environment_report, which also records it), as an
    ERROR when environment_problem() has one. Returns what it logged."""
    logger = logger or logging.getLogger("config")
    state = {"env": deploy_env(), "scheduling_allowed": scheduling_allowed(),
             "outbound_sends_allowed": outbound_sends_allowed(), "problem": environment_problem()}
    if state["problem"]:
        logger.error("DEPLOYMENT ENVIRONMENT: %s", environment_name_problem())
    else:
        logger.warning("deployment environment %r: scheduling %s, outbound sends %s", state["env"],
                       "allowed" if state["scheduling_allowed"] else "off",
                       "allowed" if state["outbound_sends_allowed"] else "off")
    return state


def build_sha() -> str:
    """The commit this process runs, as Railway (RAILWAY_GIT_COMMIT_SHA) or
    an emergency deploy (GIT_COMMIT) names it; "" when neither does."""
    return (os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT") or "").strip()


def build_label() -> str:
    """build_sha() shortened to 12, or "unknown" — never empty, so /health
    and the X-Cavnar-Build header always say which build answered (A05-13:
    a `railway up` deploy names no commit, and that must be visible)."""
    sha = build_sha()
    return sha[:12] if sha else "unknown"


# Railway sets all of these on every deploy (and `railway run` / `railway
# shell` inject them into a local command); any one is enough.
RAILWAY_MARKERS = ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME")


def railway_marked() -> bool:
    """Whether any Railway marker is in this process's environment — the one
    test scheduling_allowed and platform_database_url_allowed share."""
    return any(os.getenv(v) for v in RAILWAY_MARKERS)


def platform_database_url_allowed() -> bool:
    """Whether DATABASE_URL (and SHADOW_DATABASE_URL) may route this
    process's platform database to Postgres (audit #25 A01-06, R16). Only on
    Railway — the same markers scheduling_allowed reads, which arrive with
    DATABASE_URL itself in the container, a `railway ssh` session or a
    `railway run` — unless ALLOW_LOCAL_PG=1 says a local Postgres is meant
    (a scratch server, never production's). A shell that exported
    production's URL for a psql session used to point a local app, or the
    test suite, at production: boot DDL and the suite's per-test deletes."""
    if (os.getenv("ALLOW_LOCAL_PG") or "").strip().lower() in ("1", "true", "yes"):
        return True
    return railway_marked()


def restore_frozen() -> bool:
    """Whether restore_guard found the database put back to an earlier point
    under the running service and nobody has confirmed it yet (scale
    re-audit 10/20/26 R8-04): its `.restore_freeze` file beside the
    database file, on the app volume. A stat, no import of ours beyond the
    already-loaded models (its DB_PATH, so a test's database has its own)."""
    import sys
    if volume_optional() and not (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip() and on_railway():
        # No volume (release-safety audit 10/26/26, P3): the freeze lives in
        # object storage (marker_store), answered from a 30-second cache.
        # A store that cannot answer reads as FROZEN (blind re-audit 10/27/26
        # B5 P3): during an object-storage outage on the day of a restore,
        # "no freeze" resumed briefs and alerts against a rewound database.
        # Scheduling and sends wait until the store answers; serving does not.
        try:
            import marker_store
            return marker_store.exists(".restore_freeze", strict=True)
        except Exception as e:
            import time as _t_rf
            if _t_rf.monotonic() - _freeze_unreadable_logged[0] > 60:
                _freeze_unreadable_logged[0] = _t_rf.monotonic()
                logging.getLogger("config").error("restore freeze unreadable in object storage (%s): scheduling "
                                                  "and sends held until it can be read", e)
            return True
    models = sys.modules.get("models")
    path = getattr(models, "DB_PATH", None) or os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "reviews.db")
    return os.path.exists(os.path.join(os.path.dirname(os.path.abspath(path)) or ".", ".restore_freeze"))


_freeze_unreadable_logged = [-1e9]   # restore_frozen's error line, at most once a minute


def volume_optional() -> bool:
    """VOLUME_OPTIONAL=1 (release-safety audit 10/26/26, P3): this service may
    run with no Railway volume — its database is Postgres, its markers live
    in object storage (marker_store) and its backups go straight to object
    storage. Off by default: production keeps its volume until Will detaches
    it (docs/ops/RECOVERY.md → Retiring the volume)."""
    return (os.getenv("VOLUME_OPTIONAL") or "").strip().lower() in _TRUTHY


# ── Boot-time checks of the deployment's own settings (blind re-audit
# 10/27/26, RF6). Each returns a sentence naming what is wrong, or None.
# Pure reads of the environment (`environ` for tests); the boot logs them
# (status_manager.boot_environment_report) and /health reports them.

# Railway sets these in a deployed container beside the markers; a process
# carrying one of them but no marker is on the container with its markers
# lost (a maintenance shell, a future process type). Never
# RAILWAY_VOLUME_MOUNT_PATH alone: the test suite and boot_check set it.
RAILWAY_CONTAINER_HINTS = ("RAILWAY_DEPLOYMENT_ID", "RAILWAY_REPLICA_ID", "RAILWAY_SERVICE_NAME",
                           "RAILWAY_PROJECT_NAME", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_PRIVATE_DOMAIN",
                           "RAILWAY_VOLUME_NAME", "RAILWAY_VOLUME_ID")


def _env_of(environ):
    return os.environ if environ is None else environ


def database_url_problem(environ=None):
    """Why this process must not quietly open SQLite in place of the
    DATABASE_URL it was given, or None (blind re-audit B8 P3: a refused
    DATABASE_URL fell back to SQLite — on the production container that is
    the volume's frozen pre-cutover reviews.db, plausible and stale, and every
    write landed in a file nobody reads). On Railway (a marker, or a
    container hint with the markers lost) a DATABASE_URL that is set but is
    not a Postgres URL (a typo, an unresolved ${{Postgres.DATABASE_URL}}, the
    wrong scheme), or a Postgres one refused for want of a marker, is a
    problem. DB_BACKEND=sqlite (the deliberate rollback to SQLite) and a
    local run (A01-06: ignored with a warning, as before) are not."""
    env = _env_of(environ)
    url = (env.get("DATABASE_URL") or "").strip()
    if not url or (env.get("DB_BACKEND") or "").strip().lower() == "sqlite":
        return None
    marked = any((env.get(v) or "").strip() for v in RAILWAY_MARKERS)
    hinted = any((env.get(v) or "").strip() for v in RAILWAY_CONTAINER_HINTS)
    if not (marked or hinted):
        return None
    if not url.startswith(("postgres://", "postgresql://")):
        scheme = url.split("://", 1)[0][:20] if "://" in url else url[:12]
        return (f"DATABASE_URL is set on Railway but is not a Postgres URL (it starts {scheme!r}): refusing to "
                "open the SQLite file instead — on the production container that is the frozen pre-cutover "
                "copy. Fix the variable (DATABASE_URL=${{Postgres.DATABASE_URL}}), or set DB_BACKEND=sqlite "
                "for a deliberate rollback to SQLite (docs/ops/RECOVERY.md).")
    if not marked and (env.get("ALLOW_LOCAL_PG") or "").strip().lower() not in ("1", "true", "yes"):
        return ("DATABASE_URL names a Postgres but this process carries none of Railway's markers "
                f"({', '.join(RAILWAY_MARKERS)}) although it runs on the Railway container: refusing to open "
                "the SQLite file instead. Run it with the service's variables (railway ssh / railway run).")
    return None


# Environment names that say "not production" on purpose: a staging copy,
# a PR environment, a drill.
_NONPRODUCTION_NAMES = ("staging", "stage", "dev", "test", "preview", "qa", "sandbox", "demo", "drill")
KNOWN_ENVIRONMENTS = ("production", "staging", "local")


def _nonproduction_on_purpose(name) -> bool:
    import re
    name = str(name or "").lower()
    return any(w in name for w in _NONPRODUCTION_NAMES) or bool(re.search(r"\bpr[-_ ]?\d+\b", name))


def _production_signs(env) -> list:
    """What makes this service look like production: the operator's own
    flag, a live Stripe key, or DATABASE_URL on the production Postgres
    service's private host (PRODUCTION_DB_HOST, default Railway's name for
    the `Postgres` service)."""
    signs = []
    if (env.get("CAVNAR_PRODUCTION_SERVICE") or "").strip().lower() in _TRUTHY:
        signs.append("CAVNAR_PRODUCTION_SERVICE is set")
    if str(env.get("STRIPE_SECRET_KEY") or "").strip().startswith(("sk_live_", "rk_live_")):
        signs.append("a live Stripe key")
    url = (env.get("DATABASE_URL") or "").strip()
    host = (env.get("PRODUCTION_DB_HOST") or "postgres.railway.internal").strip().lower()
    if host and url.startswith(("postgres://", "postgresql://")):
        try:
            from urllib.parse import urlsplit
            if (urlsplit(url).hostname or "").lower() == host:
                signs.append(f"DATABASE_URL on {host}")
        except ValueError:
            pass
    return signs


def environment_name_problem(environ=None):
    """The scheduler, every send point and the paging stand down unless
    deploy_env() is "production" (A05-06), and the name comes from Railway's
    environment or CAVNAR_ENV — so a production service whose environment is
    renamed ("prod"), or a CAVNAR_ENV typo set in an incident, went silent
    with /health still "ok" (blind re-audit B8 P2). A sentence when this is
    Railway, the name is not production and does not say "not production" on
    purpose, and the service looks like production; else None."""
    env = _env_of(environ)
    if not any((env.get(v) or "").strip() for v in RAILWAY_MARKERS):
        return None
    name = None
    for var in ("CAVNAR_ENV", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT"):
        name = (env.get(var) or "").strip().lower()
        if name:
            break
    if name == "production":
        return None
    cavnar = (env.get("CAVNAR_ENV") or "").strip().lower()
    if cavnar and (cavnar in KNOWN_ENVIRONMENTS or _nonproduction_on_purpose(cavnar)):
        return None
    if not cavnar and _nonproduction_on_purpose(name):
        return None
    signs = _production_signs(env)
    if not signs:
        return None
    where = (f"CAVNAR_ENV={cavnar!r} is not a known environment name" if cavnar else
             f"Railway names this environment {name!r}, not 'production', and CAVNAR_ENV is unset")
    return (f"{where}, but this service looks like production ({', '.join(signs)}): its scheduler, every "
            "email, text and push, and the paging all stand down. Set CAVNAR_ENV=production if it is "
            "production, or CAVNAR_ENV=staging if it is not.")


# The seconds a deploy's shutdown needs (gunicorn.conf.py publishes its own
# figure as CAVNAR_SHUTDOWN_SECONDS to the web worker; this is the fallback).
DEFAULT_SHUTDOWN_SECONDS = 35


def shutdown_seconds_needed(environ=None) -> int:
    env = _env_of(environ)
    try:
        return max(1, int(str(env.get("CAVNAR_SHUTDOWN_SECONDS") or DEFAULT_SHUTDOWN_SECONDS)))
    except ValueError:
        return DEFAULT_SHUTDOWN_SECONDS


def drain_seconds(environ=None):
    """RAILWAY_DEPLOYMENT_DRAINING_SECONDS as an int, or None when unset or
    unreadable — Railway's default is 0: SIGKILL straight after SIGTERM."""
    raw = str(_env_of(environ).get("RAILWAY_DEPLOYMENT_DRAINING_SECONDS") or "").strip()
    try:
        return int(float(raw)) if raw else None
    except ValueError:
        return None


def drain_budget_problem(environ=None):
    """On Railway, a sentence when the deploy's drain (SIGTERM to SIGKILL)
    is shorter than the shutdown needs, else None (blind re-audit B5 P2 /
    B8 P3: nothing read the setting; unset, Railway's default of 0 kills the
    container mid hand-back, rows wait out their lease and the next
    scheduler waits for the lease)."""
    env = _env_of(environ)
    if not any((env.get(v) or "").strip() for v in RAILWAY_MARKERS):
        return None
    need = shutdown_seconds_needed(env)
    have = drain_seconds(env)
    if have is None:
        return (f"RAILWAY_DEPLOYMENT_DRAINING_SECONDS is unset, so Railway kills the old container at once "
                f"(its default is 0 s) — the shutdown needs {need} s to finish requests, hand queue rows back "
                "and release the scheduler lease. Set it to 60 on the web service.")
    if have < need:
        return (f"RAILWAY_DEPLOYMENT_DRAINING_SECONDS is {have} s but the shutdown needs {need} s (finish "
                "requests, hand queue rows back, release the scheduler lease). Set it to 60 on the web service.")
    return None


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
    module in a test that has no RequestsClient is left as it is.

    STRIPE_API_VERSION, when set, pins the API version every call is made
    at (audit #25 A06-08): the SDK otherwise sends the version it was built
    for, so a stripe upgrade in the lock silently changed the shape of every
    object read back. Set it to the account's version from the Stripe
    dashboard (Developers → API version); unset keeps the SDK's own.

    A failure to install the bounded client is logged, never swallowed
    (A06-10): without it every call may wait the SDK's ~80 s default."""
    import stripe
    stripe.api_key = key if key is not None else os.getenv("STRIPE_SECRET_KEY", "")
    version = (os.getenv("STRIPE_API_VERSION") or "").strip()
    if version:
        stripe.api_version = version
    # A live key outside production is refused (A05-06): a staging copy
    # carrying production's STRIPE_SECRET_KEY would charge, refund and
    # cancel real customers. A test-mode key (sk_test_ / rk_test_) works in
    # any environment.
    if not outbound_sends_allowed() and str(stripe.api_key or "").startswith(("sk_live_", "rk_live_")):
        stripe.api_key = ""
        raise RuntimeError("Stripe: a live key is refused here — " + refused_send("stripe"))
    if not getattr(stripe, "_cavnar_bounded", False):
        http_client = getattr(stripe, "http_client", None)
        client_cls = getattr(stripe, "RequestsClient", None) or getattr(http_client, "RequestsClient", None)
        if client_cls is None:
            return stripe          # a stand-in module with no HTTP client: nothing to bound
        try:
            stripe.default_http_client = client_cls(timeout=STRIPE_TIMEOUT_SECONDS)
            stripe.max_network_retries = STRIPE_MAX_NETWORK_RETRIES
            stripe._cavnar_bounded = True
        except Exception as e:
            global _stripe_unbounded_logged
            if not _stripe_unbounded_logged:
                _stripe_unbounded_logged = True
                logging.getLogger(__name__).error(
                    "Stripe's bounded HTTP client could not be installed (%s: %s); Stripe calls keep the "
                    "SDK's default timeout and retries", type(e).__name__, e, exc_info=True)
    return stripe


# The bound every Stripe call is held to (config.stripe_api, AI-33).
STRIPE_TIMEOUT_SECONDS = 10
STRIPE_MAX_NETWORK_RETRIES = 1
_stripe_unbounded_logged = False      # said once per process, not on every call
