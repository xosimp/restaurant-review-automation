import os
import math
import sqlite3
import json
import threading
import weakref
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

# On Railway, RAILWAY_VOLUME_MOUNT_PATH points at the persistent volume
# (currently /app/data). Without this, reviews.db was written to the
# container's ephemeral filesystem and silently reset to empty on every
# deploy — masked because boot-time seed code in hosted_dashboard.py
# deterministically recreates the admin/demo accounts and reviews, so
# only sessions/login_history (which have no such reseed) visibly emptied.
DB_PATH = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "reviews.db")

# Restaurant.service_tier's human-readable display names — lives here
# rather than in hosted_dashboard.py (where it originated) so anything
# needing just this small static lookup (e.g. ask_cavnar.py's context
# builder) can import it without pulling in the full Flask app module,
# which has real import-time side effects (background seed jobs, route
# registration) unsafe to trigger from a plain data helper.
TIER_LABELS = {
    "trial":             "Trial",
    "starter_reviews":   "Starter Module — Review Intelligence",
    "starter_labor":     "Starter Module — Labor Optimizer",
    "starter_inventory": "Starter Module — Food Cost Control",
    "starter_marketing": "Starter Module — Marketing Autopilot",
    "full":              "Full System",
}


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS restaurants (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    owner_email     TEXT    NOT NULL,
    google_place_id TEXT,
    yelp_business_id TEXT,
    voice_notes     TEXT,          -- owner brand-voice guidance for Claude
    -- Marketing profile
    neighborhood    TEXT,          -- e.g. "Lincoln Park, Chicago"
    vibe            TEXT,          -- e.g. "warm neighborhood bistro"
    known_for       TEXT,          -- e.g. "short rib pasta, brunch, cocktails"
    sign_off_name   TEXT,          -- e.g. "Sarah" or "The Maplewood Team"
    never_say       TEXT,          -- words/phrases to avoid in AI responses
    -- Labor settings
    hourly_rate     REAL DEFAULT 26.0,
    labor_target_pct REAL DEFAULT 30.0,  -- owner's custom labor % target
    -- Payroll workweek start (0=Monday .. 6=Sunday). FLSA overtime is
    -- computed on the employer's OWN designated 7-day workweek, which is
    -- very often not Monday. Defaults to Monday, which is what the module
    -- hardcoded before this column existed.
    week_start_day  INTEGER DEFAULT 0,
    stripe_customer_id TEXT,              -- Stripe customer ID for billing lookup
    docusign_envelope_id TEXT,            -- DocuSign envelope ID for contract tracking
    contract_status TEXT DEFAULT 'pending', -- pending/sent/signed
    location_group  TEXT,                 -- group name for multi-location clients (e.g. "Syrup")
    location_name   TEXT,                 -- specific location name (e.g. "Lincoln Park")
    inventory_frequency TEXT DEFAULT 'weekly', -- how often to request inventory data
    delivery_days   TEXT,                 -- comma-separated weekday abbrevs this client's supplier delivers on, e.g. "Mon,Thu"
    inventory_notes TEXT,                 -- admin notes on how to get data from this client
    food_cost_target REAL DEFAULT 30.0,  -- target food cost % of revenue
    inventory_updated_at TEXT,            -- last time inventory data was uploaded
    -- Tech info
    pos_system      TEXT,          -- Toast / Square / Lightspeed / etc
    owner_name      TEXT,              -- owner/GM name for personalization
    owner_phone     TEXT,              -- owner phone number
    digest_day      TEXT DEFAULT 'monday',  -- day of week for weekly digest email
    digest_enabled  INTEGER DEFAULT 1,        -- 1 = send weekly digest
    last_fetched_at TEXT,                     -- when reviews were last fetched
    -- Status
    reviews_live    INTEGER DEFAULT 0,  -- 1 = pulling real reviews
    -- Admin
    billing_status  TEXT    DEFAULT 'trial',  -- trial/active/paused/churned
    is_demo         INTEGER DEFAULT 0,  -- 1 = seed data may be wiped/reseeded automatically; never true for a real client
    internal_notes  TEXT,                      -- private notes for Will only
    -- Service tier drives module access automatically
    service_tier    TEXT    DEFAULT 'trial',  -- trial/starter_reviews/starter_labor/starter_inventory/starter_marketing/full
    -- Module access (auto-set by service_tier, can override)
    module_reviews  INTEGER DEFAULT 1,
    module_labor    INTEGER DEFAULT 1,
    module_inventory INTEGER DEFAULT 1,
    module_marketing INTEGER DEFAULT 1,
    -- Activity
    last_active_tab TEXT,
    last_activity   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id       INTEGER NOT NULL REFERENCES restaurants(id),
    platform            TEXT    NOT NULL CHECK(platform IN ('google','yelp','csv','manual')),
    external_id         TEXT    NOT NULL,
    author              TEXT,
    rating              INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    text                TEXT    NOT NULL,
    review_date         TEXT,
    fetched_at          TEXT    NOT NULL,

    -- Claude analysis outputs
    sentiment           TEXT    CHECK(sentiment IN ('positive','neutral','negative')),
    categories          TEXT,       -- JSON list
    summary             TEXT,
    urgency             TEXT    CHECK(urgency IN ('high','normal')) DEFAULT 'normal',

    -- Response workflow
    draft_response      TEXT,
    response_status     TEXT    NOT NULL
                        CHECK(response_status IN ('pending','drafted','approved','posted','skipped'))
                        DEFAULT 'pending',
    approved_at         TEXT,
    posted_at           TEXT,

    processed           INTEGER NOT NULL DEFAULT 0,
    -- restaurant_id is part of the key on purpose. It used to be
    -- UNIQUE(platform, external_id), which is global: two restaurants sharing
    -- a google_place_id (a franchise double-entry, two tenants in one food
    -- hall, a demo copy of a real client) produce identical external_ids, so
    -- whichever restaurant was fetched first claimed every review and the
    -- others were silently skipped by save_reviews' IntegrityError handler
    -- — permanently, with the reviews filed under the wrong restaurant and
    -- replies drafted in the wrong brand voice. See _migrate_reviews_unique.
    UNIQUE(restaurant_id, platform, external_id)
);

CREATE TABLE IF NOT EXISTS labor_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    period_start    TEXT NOT NULL,
    period_end      TEXT NOT NULL,
    labor_pct       REAL,
    total_labor     REAL,
    total_sales     REAL,
    saved_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_labor_history_restaurant ON labor_history(restaurant_id);

CREATE TABLE IF NOT EXISTS labor_daily_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    date            TEXT NOT NULL,
    day_of_week     TEXT,
    labor_pct       REAL,
    labor_cost      REAL,
    sales           REAL,
    total_hours     REAL,
    saved_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(restaurant_id, date) ON CONFLICT REPLACE
);
CREATE INDEX IF NOT EXISTS idx_labor_daily_restaurant ON labor_daily_history(restaurant_id, date);

CREATE TABLE IF NOT EXISTS activity_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL,
    event_type      TEXT NOT NULL,  -- 'tab_view', 'review_approved', 'csv_upload', 'login'
    event_data      TEXT,           -- JSON extra info
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (restaurant_id) REFERENCES restaurants(id)
);
CREATE INDEX IF NOT EXISTS idx_activity_log_restaurant ON activity_log(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_created ON activity_log(created_at);

CREATE TABLE IF NOT EXISTS weekly_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    period_start    TEXT    NOT NULL,
    period_end      TEXT    NOT NULL,
    total_reviews   INTEGER,
    avg_rating      REAL,
    sentiment_json  TEXT,       -- {"positive":N,"neutral":N,"negative":N}
    top_issues_json TEXT,       -- [["food_quality",3],...]
    sent_at         TEXT
);

CREATE TABLE IF NOT EXISTS service_status (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service_key TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'operational'
                CHECK(status IN ('operational','degraded','outage','maintenance')),
    message     TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS status_incidents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT NOT NULL,
    body          TEXT,
    affected_keys TEXT,   -- JSON array of service_key strings
    severity      TEXT NOT NULL DEFAULT 'degraded'
                  CHECK(severity IN ('degraded','outage','maintenance')),
    status        TEXT NOT NULL DEFAULT 'investigating'
                  CHECK(status IN ('investigating','monitoring','resolved')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at   TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS status_incident_updates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES status_incidents(id),
    message     TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS response_templates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    category      TEXT DEFAULT 'general',  -- general, positive, negative, neutral
    use_count     INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_templates_restaurant ON response_templates(restaurant_id);

CREATE TABLE IF NOT EXISTS changelog_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    body         TEXT,
    tag          TEXT DEFAULT 'feature',  -- feature, fix, improvement
    published_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_published INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_reviews_restaurant   ON reviews(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_reviews_status       ON reviews(response_status);
CREATE INDEX IF NOT EXISTS idx_reviews_fetched      ON reviews(fetched_at);
CREATE INDEX IF NOT EXISTS idx_reviews_urgency      ON reviews(urgency);

CREATE TABLE IF NOT EXISTS client_data (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id) UNIQUE,
    shifts_csv      TEXT,           -- raw CSV content for labor module
    inventory_csv   TEXT,           -- raw CSV content for inventory module
    shifts_source   TEXT,           -- "upload" | "manual" | "sample"
    inventory_source TEXT,          -- "upload" | "manual" | "sample"
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class Restaurant:
    name: str
    owner_email: str
    google_place_id: Optional[str]  = None
    yelp_business_id: Optional[str] = None
    voice_notes: Optional[str]      = None
    neighborhood: Optional[str]     = None
    vibe: Optional[str]             = None
    known_for: Optional[str]        = None
    sign_off_name: Optional[str]    = None
    never_say: Optional[str]        = None
    hourly_rate: float              = 26.0
    labor_target_pct: float         = 30.0
    week_start_day: int             = 0
    stripe_customer_id: Optional[str]    = None
    docusign_envelope_id: Optional[str]  = None
    contract_status: str                 = "pending"
    location_group: Optional[str]        = None
    location_name: Optional[str]         = None
    inventory_frequency: str             = "weekly"
    delivery_days: Optional[str]         = None
    inventory_notes: Optional[str]       = None
    food_cost_target: float              = 30.0
    monthly_revenue_target: float        = 0.0
    hours_notes: Optional[str]           = None
    role_rates_json: Optional[str]       = None
    close_times_json: Optional[str]      = None   # e.g. {"Monday":"9:00pm","Friday":"10:00pm"} — per-day close time, used to hard-cap generated shift_end
    role_close_buffer_json: Optional[str] = None  # e.g. {"Bartender":60} — minutes a role may run past close; any role not listed defaults to 0 (must end at or before close)
    section_count: Optional[int]         = None
    daypart_split: Optional[str]         = None   # e.g. "lunch:35,dinner:65"
    delivery_pct: Optional[int]          = None   # % of revenue from delivery/takeout
    role_minimums_json: Optional[str]    = None   # e.g. {"Server":2,"Cook":2,"Bartender":1}
    # Minimum combined Operational Score per role on a shift, e.g.
    # {"Bartender": 10, "Cook": 15}. Two 5-rated bartenders make 10. Nothing
    # is hardcoded; an unset role has no threshold.
    role_strength_json: Optional[str]    = None
    # Shift leader requirements layered on the same capability data, e.g.
    # [{"days":["Saturday"],"daypart":"night","role":"Bartender","min_score":5,"count":1}]
    shift_leader_rules_json: Optional[str] = None
    # Per-restaurant weighting of the Shift Quality dimensions. Empty means
    # the engine's own defaults, which is what almost every restaurant wants.
    quality_weights_json: Optional[str]   = None
    sched_notes: Optional[str]           = None   # freeform scheduling notes from admin
    latitude: Optional[float]            = None   # geocoded once from google_place_id, cached
    longitude: Optional[float]           = None
    weather_cache_json: Optional[str]    = None   # cached NWS forecast periods
    weather_cached_at: Optional[str]     = None
    email_theme: Optional[str]           = "dark"  # 'dark' or 'light' — drives weekly digest email theme
    inventory_updated_at: Optional[str]  = None
    temp_password: Optional[str]         = None
    ig_token: Optional[str]              = None
    competitor_intel: Optional[str]      = None
    competitor_updated_at: Optional[str] = None
    ig_user_id: Optional[str]            = None
    ig_token_expires: Optional[str]      = None
    fb_token_expires: Optional[str]      = None
    fb_page_token: Optional[str]         = None
    fb_page_id: Optional[str]            = None
    gmb_access_token: Optional[str]      = None
    gmb_refresh_token: Optional[str]     = None
    gmb_account_id: Optional[str]        = None
    gmb_location_id: Optional[str]       = None
    gmb_token_expires: Optional[str]     = None
    # Toast POS credentials (admin-managed, server-to-server only)
    toast_client_id: Optional[str]       = None
    toast_client_secret: Optional[str]   = None
    toast_restaurant_guid: Optional[str] = None
    toast_access_token: Optional[str]    = None
    toast_token_expires: Optional[str]   = None
    toast_last_synced: Optional[str]     = None
    toast_sync_error: Optional[str]      = None
    square_access_token: Optional[str]   = None
    square_location_id: Optional[str]    = None
    square_last_synced: Optional[str]    = None
    square_sync_error: Optional[str]     = None
    clover_merchant_id: Optional[str]    = None
    clover_api_token: Optional[str]      = None
    clover_last_synced: Optional[str]    = None
    clover_sync_error: Optional[str]     = None
    pos_system: Optional[str]       = None
    owner_name: Optional[str]       = None
    owner_phone: Optional[str]      = None
    digest_day: str                 = "monday"
    digest_enabled: int             = 1
    last_fetched_at: Optional[str]  = None
    reviews_live: int               = 0
    billing_status: str             = "trial"
    is_demo: int                    = 0
    two_fa_enabled: int             = 0
    two_fa_code: str                = None
    two_fa_expires: str             = None
    two_fa_device_token: str        = None
    two_fa_pending: str             = None
    two_fa_method: str              = "email"  # "email" or "sms" — which channel a verification code goes to; set once setup verifies, not at send-test time
    timezone: str                   = "America/Chicago"  # IANA name; all per-restaurant "today"/trend math uses this
    onboarding_dismissed: int       = 0
    internal_notes: Optional[str]   = None
    service_tier: str               = "trial"   # trial / starter_reviews / starter_labor / starter_inventory / starter_marketing / full
    module_reviews: int             = 1
    module_labor: int               = 1
    module_inventory: int           = 1
    module_marketing: int           = 1
    last_active_tab: Optional[str]  = None
    menu_notes:      Optional[str]  = None
    menu_url:        Optional[str]  = None
    skip_holidays:    Optional[str]  = None
    custom_competitors: Optional[str] = None
    login_notify:     int            = 0
    marketing_emails_opt_out: int    = 0
    # Settings audit additions (Account tab, iOS + web)
    alert_health_bypass_quiet: int   = 0     # health/safety alerts ignore quiet hours
    alert_food_waste: int            = 0     # daily: waste flagged on several items / a real dollar amount
    alert_ai_visibility_drop: int    = 0     # daily: AI visibility score fell vs. the previous run
    alert_extra_emails: Optional[str] = None # comma list; alert + digest emails also go here
    push_sound: int                  = 1     # 0 = silent pushes
    auto_approve_5star: int          = 0     # auto-approve (and post) drafted 5-star responses
    auto_approve_daily_cap: int      = 5
    auto_approve_paused: int         = 0     # kill switch — keeps the rule configured but off
    open_times_json: Optional[str]   = None  # {"Monday":"11:00am",...}; close_times_json already exists
    response_language: Optional[str] = None  # None = match the review's language (drafter default)
    tone_preset: Optional[str]       = None  # warm / professional / playful / concise
    data_retention_months: int       = 0     # 0 = keep everything
    alert_1star:          int       = 1
    alert_2star:          int       = 0
    alert_health:         int       = 1
    alert_neg_spike:      int       = 1
    alert_negative_trend: int       = 1
    alert_no_response:    int       = 0
    alert_5star:          int       = 0
    alert_rating_threshold: int     = 0
    alert_rating_floor:   float     = 4.0
    alert_labor_over:     int       = 0
    alert_any_review:     int       = 0
    alert_resp_approved:  int       = 0
    urgent_via_email:     int       = 1
    urgent_via_sms:       int       = 0
    # Per-alert-type channel matrix
    al_health_email:      int       = 1
    al_health_sms:        int       = 0
    al_health_push:       int       = 1
    al_1star_email:       int       = 1
    al_1star_sms:         int       = 0
    al_1star_push:        int       = 1
    al_2star_email:       int       = 1
    al_2star_sms:         int       = 0
    al_2star_push:        int       = 1
    al_5star_email:       int       = 0
    al_5star_sms:         int       = 0
    al_5star_push:        int       = 1
    al_spike_email:       int       = 1
    al_spike_sms:         int       = 0
    al_spike_push:        int       = 1
    al_unres_email:       int       = 1
    al_unres_sms:         int       = 0
    al_unres_push:        int       = 1
    changelog_seen_at: Optional[str] = None
    notifications_seen_at: Optional[str] = None
    alert_quiet_start: Optional[str] = None
    alert_quiet_end:   Optional[str] = None
    alert_max_per_day: int           = 0
    brand_name:        Optional[str] = None
    brand_color:       Optional[str] = None
    brand_logo_url:    Optional[str] = None
    last_activity: Optional[str]    = None
    gbp_rating: Optional[float]     = None
    gbp_review_count: Optional[int] = None
    id: Optional[int]               = None
    created_at: str = field(default_factory=lambda: __import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S'))


@dataclass
class Review:
    restaurant_id: int
    platform: str
    external_id: str
    author: str
    rating: int
    text: str
    fetched_at: str = field(default_factory=lambda: __import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S'))
    review_date: Optional[str] = None
    id: Optional[int] = None
    sentiment: Optional[str] = None
    categories: Optional[list] = None
    summary: Optional[str] = None
    urgency: str = "normal"
    draft_response: Optional[str] = None
    response_status: str = "pending"
    approved_at: Optional[str] = None
    posted_at: Optional[str] = None
    review_name: Optional[str] = None  # GMB API name for auto-posting
    # Google's own updateTime, so an edit to an existing review can be
    # detected on the next fetch. review_date stays the CREATE time.
    source_updated_at: Optional[str] = None
    processed: bool = False


@dataclass
class WeeklyReport:
    restaurant_id: int
    period_start: str
    period_end: str
    total_reviews: int = 0
    avg_rating: float = 0.0
    sentiment: dict = field(default_factory=lambda: {"positive": 0, "neutral": 0, "negative": 0})
    top_issues: list = field(default_factory=list)
    id: Optional[int] = None
    sent_at: Optional[str] = None


# ── Connection ────────────────────────────────────────────────────────────────

class _TrackedConnection(sqlite3.Connection):
    """A plain sqlite3 connection that can be weak-referenced.

    The C type can't be, and being able to hold a weak reference is what lets
    close_thread_connections() below sweep a connection that leaked without
    keeping it alive itself.
    """


# Per-thread bag of connections handed out but not yet closed. Weak, so a
# connection the caller closes and drops disappears from here on its own.
_open_conns = threading.local()


def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, factory=_TrackedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    bag = getattr(_open_conns, "bag", None)
    if bag is None:
        bag = _open_conns.bag = weakref.WeakSet()
    try:
        bag.add(conn)
    except TypeError:
        pass
    return conn


def close_thread_connections() -> int:
    """Close whatever this thread opened and didn't. Returns how many.

    The codebase's prevailing shape is `conn = get_conn(); ...; conn.close()`,
    which closes on every normal path (no site was found that forgets one) but
    not when something raises in between — and an exception carries a
    traceback that keeps the frame, and therefore the connection, alive well
    past the failure. On SQLite that matters: a leaked connection that was
    mid-write holds a RESERVED lock, and every other writer waits out the
    30-second busy timeout behind it.

    Called from the Flask app's teardown so a failed request can't leave one
    behind. New code should still prefer `with db_conn() as conn:` — this is
    the net under the wire, not a licence to skip the close.
    """
    bag = getattr(_open_conns, "bag", None)
    if not bag:
        return 0
    leaked = 0
    for conn in list(bag):
        try:
            # A connection the caller already closed raises here, which is how
            # this counts real leaks rather than every request that ran.
            conn.execute("SELECT 1")
        except Exception:
            continue
        leaked += 1
        try:
            conn.close()
        except Exception:
            pass
    bag.clear()
    return leaked


from contextlib import contextmanager as _contextmanager

@_contextmanager
def db_conn(db_path: str = DB_PATH):
    """Context-manager form of get_conn() — guarantees the connection is closed
    even if an exception is raised mid-block, unlike the conn=get_conn()...
    conn.close() pattern used throughout this codebase, where an exception
    between open and close leaks the connection. New code should prefer this:

        with db_conn() as conn:
            conn.execute(...)
            conn.commit()

    Existing call sites aren't being migrated wholesale — this is additive."""
    conn = get_conn(db_path)
    try:
        yield conn
    finally:
        conn.close()


def ensure_columns(db_path: str = DB_PATH):
    """Ensure all required columns exist — runs on every startup."""
    conn = get_conn(db_path)
    columns_to_add = [
        # Staff schedule links used to live forever: a former employee's
        # link kept showing next week's roster indefinitely.
        ("schedule_shares", "expires_at", "TEXT"),
        # email_log.status existed from the start but nothing could write it:
        # log_email() had no status parameter, so a failed send was recorded
        # as 'sent' like every other row.
        ("email_log", "error", "TEXT"),
        ("email_log", "message_id", "TEXT"),
        ("restaurants", "temp_password", "TEXT"),
        ("restaurants", "ig_token", "TEXT"),
        ("restaurants", "competitor_intel", "TEXT"),
        ("restaurants", "competitor_updated_at", "TEXT"),
        ("restaurants", "ig_user_id", "TEXT"),
        ("restaurants", "ig_token_expires", "TEXT"),
        ("restaurants", "fb_token_expires", "TEXT"),
        ("restaurants", "fb_page_token", "TEXT"),
        ("restaurants", "fb_page_id", "TEXT"),
        ("restaurants", "docusign_envelope_id", "TEXT"),
        ("restaurants", "contract_status", "TEXT"),
        ("restaurants", "stripe_customer_id", "TEXT"),
        ("restaurants", "location_group", "TEXT"),
        ("restaurants", "location_name", "TEXT"),
        ("restaurants", "pos_system", "TEXT"),
        ("restaurants", "inventory_frequency", "TEXT"),
        ("restaurants", "delivery_days", "TEXT"),
        ("restaurants", "inventory_notes", "TEXT"),
        ("restaurants", "food_cost_target", "REAL"),
        ("restaurants", "monthly_revenue_target", "REAL"),
        ("restaurants", "hours_notes", "TEXT"),
        ("restaurants", "role_rates_json", "TEXT"),
        ("restaurants", "close_times_json", "TEXT"),
        ("restaurants", "role_close_buffer_json", "TEXT"),
        ("restaurants", "section_count", "INTEGER"),
        ("restaurants", "daypart_split", "TEXT"),
        ("restaurants", "delivery_pct", "INTEGER"),
        ("restaurants", "role_minimums_json", "TEXT"),
        # Operational Score — see staff_capabilities and shift_strength().
        ("restaurants", "role_strength_json", "TEXT"),
        ("restaurants", "shift_leader_rules_json", "TEXT"),
        ("restaurants", "quality_weights_json", "TEXT"),
        ("restaurants", "sched_notes", "TEXT"),
        ("restaurants", "latitude", "REAL"),
        ("restaurants", "longitude", "REAL"),
        ("restaurants", "weather_cache_json", "TEXT"),
        ("restaurants", "weather_cached_at", "TEXT"),
        ("restaurants", "email_theme", "TEXT DEFAULT 'dark'"),
        ("restaurants", "inventory_updated_at", "TEXT"),
        ("restaurants", "gbp_rating", "REAL"),
        ("restaurants", "gbp_review_count", "INTEGER"),
        ("client_data", "food_cost_json", "TEXT"),
        ("labor_daily_history", "total_hours", "REAL"),
        ("users", "role", "TEXT DEFAULT 'client'"),
        ("users", "google_id", "TEXT"),
        ("users", "apple_user_id", "TEXT"),
        ("sessions", "active_restaurant_id", "INTEGER"),
        # Alert channel matrix
        ("restaurants", "al_health_email", "INTEGER DEFAULT 1"),
        ("restaurants", "al_health_sms",   "INTEGER DEFAULT 0"),
        ("restaurants", "al_health_push",  "INTEGER DEFAULT 1"),
        ("restaurants", "al_1star_email",  "INTEGER DEFAULT 1"),
        ("restaurants", "al_1star_sms",    "INTEGER DEFAULT 0"),
        ("restaurants", "al_1star_push",   "INTEGER DEFAULT 1"),
        ("restaurants", "al_2star_email",  "INTEGER DEFAULT 1"),
        ("restaurants", "al_2star_sms",    "INTEGER DEFAULT 0"),
        ("restaurants", "al_2star_push",   "INTEGER DEFAULT 1"),
        ("restaurants", "al_5star_email",  "INTEGER DEFAULT 0"),
        ("restaurants", "al_5star_sms",    "INTEGER DEFAULT 0"),
        ("restaurants", "al_5star_push",   "INTEGER DEFAULT 1"),
        ("restaurants", "al_spike_email",  "INTEGER DEFAULT 1"),
        ("restaurants", "al_spike_sms",    "INTEGER DEFAULT 0"),
        ("restaurants", "al_spike_push",   "INTEGER DEFAULT 1"),
        ("restaurants", "al_unres_email",  "INTEGER DEFAULT 1"),
        ("restaurants", "al_unres_sms",    "INTEGER DEFAULT 0"),
        ("restaurants", "al_unres_push",   "INTEGER DEFAULT 1"),
        # Review invite SMS
        ("review_requests", "customer_phone", "TEXT"),
        # Soft-delete
        ("reviews", "deleted_at", "TEXT"),
        # Who each ingredient is ordered from, so a suggested order can
        # actually be sent somewhere (see build_purchase_orders below).
        ("menu_items", "sell_price", "REAL"),
        ("ingredients", "supplier_name", "TEXT"),
        ("ingredients", "supplier_email", "TEXT"),
        # Changelog seen state
        ("restaurants", "changelog_seen_at", "TEXT"),
        # Notifications (alert_log) seen state — same stamp-on-read pattern
        ("restaurants", "notifications_seen_at", "TEXT"),
        # Alert DND / throttle
        ("restaurants", "alert_quiet_start", "TEXT"),
        ("restaurants", "alert_quiet_end",   "TEXT"),
        ("restaurants", "alert_max_per_day", "INTEGER DEFAULT 0"),
        # White-label branding
        ("restaurants", "brand_name",      "TEXT"),
        ("restaurants", "brand_color",     "TEXT"),
        ("restaurants", "brand_logo_url",  "TEXT"),
        # Response performance tracking
        # Existing databases: the scheduled-post publish claim.
        ("marketing_scheduled_posts", "claimed_at", "TEXT"),
        ("reviews", "draft_edited",     "INTEGER DEFAULT 0"),
        ("reviews", "regenerate_count", "INTEGER DEFAULT 0"),
        ("reviews", "response_action",  "TEXT"),
        # Edited-review tracking — see save_reviews.
        ("reviews", "source_updated_at", "TEXT"),
        ("reviews", "edited_at",         "TEXT"),
        ("reviews", "original_rating",   "INTEGER"),
        # A draft that generated cleanly but states something unverifiable.
        ("reviews", "draft_needs_review", "INTEGER DEFAULT 0"),
        ("reviews", "draft_review_reason", "TEXT"),
        # When the official Google rating was last refreshed. Without it a
        # failed refresh left the previous value in place indefinitely,
        # shown as current and driving the rating-threshold alert.
        ("restaurants", "gbp_rating_updated_at", "TEXT"),
        # Sample size behind each visibility run, so a change can be told
        # from a difference in how many queries came back.
        ("ai_visibility_runs", "answered", "INTEGER"),
        ("ai_visibility_runs", "appeared", "INTEGER"),
    ]
    for table, col, col_type in columns_to_add:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            conn.commit()
            print(f"Added column {table}.{col}")
        except Exception:
            pass  # Column already exists
    conn.close()

def _reviews_unique_is_global(conn) -> bool:
    """True while `reviews` still carries the old UNIQUE(platform, external_id)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'"
    ).fetchone()
    sql = (row[0] if row else "") or ""
    return "UNIQUE(platform, external_id)" in sql.replace("\n", " ")


def _migrate_reviews_unique(conn):
    """Re-key `reviews` from UNIQUE(platform, external_id) to
    UNIQUE(restaurant_id, platform, external_id).

    SQLite can't alter a table-level UNIQUE, and the implicit index it
    creates can't be dropped, so this is a rebuild: new table, copy, swap.
    Safe by construction — every existing row already satisfies the new,
    strictly weaker constraint, so the copy cannot fail on data. Wrapped in
    one transaction, and a no-op on a database that already has the new key.
    """
    try:
        if not _reviews_unique_is_global(conn):
            return
        cols = [r[1] for r in conn.execute("PRAGMA table_info(reviews)").fetchall()]
        col_list = ", ".join(cols)
        create = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'"
        ).fetchone()[0]
        new_create = (create
                      .replace("UNIQUE(platform, external_id)",
                               "UNIQUE(restaurant_id, platform, external_id)")
                      .replace("CREATE TABLE reviews", "CREATE TABLE reviews_rekeyed", 1)
                      .replace('CREATE TABLE "reviews"', "CREATE TABLE reviews_rekeyed", 1))
        if "reviews_rekeyed" not in new_create:
            print("[migrate] reviews: could not rewrite CREATE statement, leaving as is")
            return
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS reviews_rekeyed")
        conn.execute(new_create)
        conn.execute(f"INSERT INTO reviews_rekeyed ({col_list}) SELECT {col_list} FROM reviews")
        moved = conn.execute("SELECT COUNT(*) FROM reviews_rekeyed").fetchone()[0]
        conn.execute("DROP TABLE reviews")
        conn.execute("ALTER TABLE reviews_rekeyed RENAME TO reviews")
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")
        # The rebuild drops the table's indexes with it.
        for idx in ("CREATE INDEX IF NOT EXISTS idx_reviews_restaurant ON reviews(restaurant_id)",
                    "CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(response_status)",
                    "CREATE INDEX IF NOT EXISTS idx_reviews_fetched ON reviews(fetched_at)",
                    "CREATE INDEX IF NOT EXISTS idx_reviews_urgency ON reviews(urgency)"):
            conn.execute(idx)
        conn.commit()
        print(f"[migrate] reviews re-keyed to UNIQUE(restaurant_id, platform, external_id) — {moved} rows")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        print(f"[migrate] reviews re-key FAILED, table left untouched: {e}")


def _ensure_place_id_uniqueness(conn):
    """One live restaurant per Google listing.

    Two restaurants pointed at the same google_place_id is what made the
    review key collide in the first place, and it also means two owners
    drafting replies to the same reviews. Demo copies are excluded — a demo
    row deliberately mirrors a real listing — so this only constrains rows
    that actually fetch and reply.

    Best-effort: if the data already violates it, the index isn't created and
    the offending ids are printed rather than the boot failing.
    """
    try:
        dupes = conn.execute(
            "SELECT google_place_id, GROUP_CONCAT(id) AS ids, COUNT(*) AS n FROM restaurants "
            "WHERE COALESCE(google_place_id,'')<>'' AND COALESCE(is_demo,0)=0 "
            "GROUP BY google_place_id HAVING n > 1"
        ).fetchall()
        if dupes:
            for d in dupes:
                print(f"[migrate] google_place_id {d[0]} is on live restaurants {d[1]} — "
                      f"reviews for it can only reach one of them. Clear it on the duplicates, "
                      f"or mark them is_demo=1, then redeploy to enable the unique index.")
            return
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurants_place_id_live "
            "ON restaurants(google_place_id) "
            "WHERE COALESCE(google_place_id,'')<>'' AND COALESCE(is_demo,0)=0"
        )
        conn.commit()
    except Exception as e:
        print(f"[migrate] place_id uniqueness skipped: {e}")


def adopt_legacy_db(db_path: str = DB_PATH):
    """Carry the pre-volume database onto the volume, once.

    Moving DB_PATH onto the Railway volume fixes the reset-every-deploy bug,
    but it also points the app at a file that has never existed before — so
    the first boot after that change would come up on an empty database and
    quietly abandon whatever was in the container's copy. The boot seed
    recreates the admin and demo accounts, which is exactly what makes the
    loss hard to notice: real client configuration is what actually goes.

    So on the first boot where the volume has no database and the old
    location still has one, the old one is copied across. Copy, not move —
    if anything goes wrong the original is still sitting there. Runs only
    when the two paths genuinely differ, which off Railway they do not, so
    this is a no-op for local runs and tests.
    """
    # Two guards before anything is touched, and both matter. Only Railway
    # has a volume to adopt onto, and only the module's own DB_PATH is the
    # database the app actually runs on — without the second check this
    # fires for every test that inits a database at a tmp_path, and quietly
    # copies the developer's real reviews.db into it.
    if not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        return False
    if os.path.abspath(db_path) != os.path.abspath(DB_PATH):
        return False
    legacy = "reviews.db"
    if os.path.abspath(db_path) == os.path.abspath(legacy):
        return False
    if os.path.exists(db_path) or not os.path.exists(legacy):
        return False
    try:
        import shutil
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # sqlite3's own backup API rather than a file copy: it takes a read
        # lock and captures a consistent snapshot even if something is
        # mid-write, and it folds in any -wal content instead of leaving it
        # behind in a sidecar file we would not be copying.
        src = sqlite3.connect(legacy)
        dst = sqlite3.connect(db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        print(f"[models] adopted legacy {legacy} -> {db_path}")
        return True
    except Exception as e:
        print(f"[models] could not adopt legacy {legacy}: {e}")
        return False


def init_db(db_path: str = DB_PATH):
    adopt_legacy_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # Migrate: add new columns to existing databases
    migrations = [
        "ALTER TABLE restaurants ADD COLUMN neighborhood TEXT",
        "ALTER TABLE restaurants ADD COLUMN vibe TEXT",
        "ALTER TABLE restaurants ADD COLUMN known_for TEXT",
        "ALTER TABLE restaurants ADD COLUMN sign_off_name TEXT",
        "ALTER TABLE restaurants ADD COLUMN never_say TEXT",
        "ALTER TABLE restaurants ADD COLUMN hourly_rate REAL DEFAULT 26.0",
        "ALTER TABLE restaurants ADD COLUMN pos_system TEXT",
        "ALTER TABLE restaurants ADD COLUMN owner_name TEXT",
        "ALTER TABLE restaurants ADD COLUMN labor_target_pct REAL DEFAULT 30.0",
        "ALTER TABLE restaurants ADD COLUMN week_start_day INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN stripe_customer_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN docusign_envelope_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN contract_status TEXT DEFAULT 'pending'",
        "ALTER TABLE restaurants ADD COLUMN location_group TEXT",
        "ALTER TABLE restaurants ADD COLUMN location_name TEXT",
        "ALTER TABLE restaurants ADD COLUMN inventory_frequency TEXT DEFAULT 'weekly'",
        "ALTER TABLE restaurants ADD COLUMN inventory_notes TEXT",
        "ALTER TABLE restaurants ADD COLUMN food_cost_target REAL DEFAULT 30.0",
        "ALTER TABLE restaurants ADD COLUMN inventory_updated_at TEXT",
        "ALTER TABLE restaurants ADD COLUMN temp_password TEXT",
        "ALTER TABLE restaurants ADD COLUMN ig_token TEXT",
        "ALTER TABLE restaurants ADD COLUMN ig_user_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN ig_token_expires TEXT",
        "ALTER TABLE restaurants ADD COLUMN fb_token_expires TEXT",
        "ALTER TABLE restaurants ADD COLUMN fb_page_token TEXT",
        "ALTER TABLE restaurants ADD COLUMN fb_page_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN owner_phone TEXT",
        "ALTER TABLE restaurants ADD COLUMN digest_day TEXT DEFAULT 'monday'",
        "ALTER TABLE restaurants ADD COLUMN digest_enabled INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN last_fetched_at TEXT",
        "ALTER TABLE restaurants ADD COLUMN reviews_live INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN billing_status TEXT DEFAULT 'trial'",
        "ALTER TABLE restaurants ADD COLUMN is_demo INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN internal_notes TEXT",
        "ALTER TABLE restaurants ADD COLUMN service_tier TEXT DEFAULT 'trial'",
        "ALTER TABLE restaurants ADD COLUMN module_reviews INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN module_labor INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN module_inventory INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN module_marketing INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN two_fa_enabled INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN two_fa_code TEXT",
        "ALTER TABLE restaurants ADD COLUMN two_fa_expires TEXT",
        "ALTER TABLE restaurants ADD COLUMN two_fa_device_token TEXT",
        "ALTER TABLE restaurants ADD COLUMN two_fa_pending TEXT",
        "ALTER TABLE restaurants ADD COLUMN two_fa_method TEXT DEFAULT 'email'",
        "ALTER TABLE restaurants ADD COLUMN timezone TEXT DEFAULT 'America/Chicago'",
        "ALTER TABLE restaurants ADD COLUMN onboarding_dismissed INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN last_active_tab TEXT",
        "ALTER TABLE restaurants ADD COLUMN last_activity TEXT",
        "ALTER TABLE client_data ADD COLUMN shifts_csv TEXT",
        "ALTER TABLE client_data ADD COLUMN inventory_csv TEXT",
        "ALTER TABLE client_data ADD COLUMN food_cost_json TEXT",
        """CREATE TABLE IF NOT EXISTS labor_daily_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            day_of_week TEXT,
            labor_pct REAL,
            labor_cost REAL,
            sales REAL,
            total_hours REAL,
            saved_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, date) ON CONFLICT REPLACE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_labor_daily_restaurant ON labor_daily_history(restaurant_id, date)",
        "ALTER TABLE restaurants ADD COLUMN gmb_access_token TEXT",
        "ALTER TABLE users ADD COLUMN reset_token TEXT",
        "ALTER TABLE users ADD COLUMN reset_token_expires TEXT",
        "ALTER TABLE restaurants ADD COLUMN menu_notes TEXT",
        "ALTER TABLE restaurants ADD COLUMN menu_url TEXT",
        "ALTER TABLE restaurants ADD COLUMN skip_holidays TEXT",
        "ALTER TABLE restaurants ADD COLUMN custom_competitors TEXT",
        "ALTER TABLE restaurants ADD COLUMN login_notify INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN marketing_emails_opt_out INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_health_bypass_quiet INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_food_waste INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_ai_visibility_drop INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_extra_emails TEXT",
        "ALTER TABLE restaurants ADD COLUMN push_sound INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN auto_approve_5star INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN auto_approve_daily_cap INTEGER DEFAULT 5",
        "ALTER TABLE restaurants ADD COLUMN auto_approve_paused INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN open_times_json TEXT",
        "ALTER TABLE restaurants ADD COLUMN response_language TEXT",
        "ALTER TABLE restaurants ADD COLUMN tone_preset TEXT",
        "ALTER TABLE restaurants ADD COLUMN data_retention_months INTEGER DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS ai_visibility_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            ai_score INTEGER,
            gbp_score INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "ALTER TABLE restaurants ADD COLUMN gmb_refresh_token TEXT",
        "ALTER TABLE restaurants ADD COLUMN gmb_account_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN gmb_location_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN gmb_token_expires TEXT",
        "ALTER TABLE reviews ADD COLUMN review_name TEXT",
        "ALTER TABLE restaurants ADD COLUMN toast_client_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN toast_client_secret TEXT",
        "ALTER TABLE restaurants ADD COLUMN toast_restaurant_guid TEXT",
        "ALTER TABLE restaurants ADD COLUMN toast_access_token TEXT",
        "ALTER TABLE restaurants ADD COLUMN toast_token_expires TEXT",
        "ALTER TABLE restaurants ADD COLUMN toast_last_synced TEXT",
        "ALTER TABLE restaurants ADD COLUMN toast_sync_error TEXT",
        "ALTER TABLE restaurants ADD COLUMN square_access_token TEXT",
        "ALTER TABLE restaurants ADD COLUMN square_location_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN square_last_synced TEXT",
        "ALTER TABLE restaurants ADD COLUMN square_sync_error TEXT",
        "ALTER TABLE restaurants ADD COLUMN clover_merchant_id TEXT",
        "ALTER TABLE restaurants ADD COLUMN clover_api_token TEXT",
        "ALTER TABLE restaurants ADD COLUMN clover_last_synced TEXT",
        "ALTER TABLE restaurants ADD COLUMN clover_sync_error TEXT",
        "ALTER TABLE restaurants ADD COLUMN gbp_rating REAL",
        "ALTER TABLE restaurants ADD COLUMN gbp_review_count INTEGER",
        """CREATE TABLE IF NOT EXISTS review_requests (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            customer_name TEXT,
            customer_email TEXT NOT NULL,
            sent_at       TEXT NOT NULL DEFAULT (datetime('now')),
            method        TEXT NOT NULL DEFAULT 'email',
            status        TEXT NOT NULL DEFAULT 'sent'
        )""",
        """CREATE TABLE IF NOT EXISTS service_status (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            service_key TEXT NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            description TEXT,
            status      TEXT NOT NULL DEFAULT 'operational',
            message     TEXT,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS status_incidents (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            title         TEXT NOT NULL,
            body          TEXT,
            affected_keys TEXT,
            severity      TEXT NOT NULL DEFAULT 'degraded',
            status        TEXT NOT NULL DEFAULT 'investigating',
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at   TEXT,
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS status_incident_updates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            message     TEXT NOT NULL,
            status      TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS alert_contacts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            name          TEXT,
            phone         TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_alert_contacts_restaurant ON alert_contacts(restaurant_id)",
        # SMS consent must come from the number's own owner, not a third party
        # typing it in on their behalf — the admin "add alert contact" endpoint
        # had no consent check at all, so an admin-added number could receive
        # SMS with zero record of that person ever opting in. Every contact now
        # carries whether real consent was captured and when.
        "ALTER TABLE alert_contacts ADD COLUMN sms_consent INTEGER DEFAULT 0",
        "ALTER TABLE alert_contacts ADD COLUMN sms_consent_at TEXT",
        """CREATE TABLE IF NOT EXISTS alert_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            alert_type    TEXT NOT NULL,
            review_id     INTEGER,
            fired_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_alert_log_restaurant ON alert_log(restaurant_id, fired_at)",
        # One row per restaurant per day — the mobile Home tab's "Total value
        # delivered" sparkline needs real history to plot, and the figure
        # itself is computed fresh on every request rather than stored
        # anywhere, so there was nothing to chart from. Populated
        # opportunistically (upsert-on-conflict) from the first Home-tab
        # load of each day — see value_delivered.py — rather than a
        # separate scheduled job.
        """CREATE TABLE IF NOT EXISTS value_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            snapshot_date TEXT NOT NULL,
            total_value   INTEGER NOT NULL,
            UNIQUE(restaurant_id, snapshot_date)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_value_snapshots_restaurant ON value_snapshots(restaurant_id, snapshot_date)",
        "ALTER TABLE restaurants ADD COLUMN alert_1star INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN alert_2star INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_health INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN alert_neg_spike INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN alert_negative_trend INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN alert_no_response INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN urgent_via_email INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN urgent_via_sms INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_5star INTEGER DEFAULT 0",
        # Account -> Close my account: Apple App Store Review Guideline
        # 5.1.1(v) requires apps that support account creation to also let
        # the user initiate deletion from inside the app — a "please email
        # us" flow doesn't count for anything but a handful of regulated
        # industries, which this isn't. Cavnar AI still can't self-serve
        # deactivate an account (clients are under contract), so this
        # records the request and notifies Will to wind it down per the
        # 30-day notice policy, same as before, but the user now actually
        # DOES something in the app rather than being routed to their own
        # email client.
        "ALTER TABLE restaurants ADD COLUMN deletion_requested_at TEXT",
        "ALTER TABLE restaurants ADD COLUMN alert_rating_threshold INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_rating_floor REAL DEFAULT 4.0",
        "ALTER TABLE restaurants ADD COLUMN alert_labor_over INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_any_review INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_resp_approved INTEGER DEFAULT 0",
        # marketing_content_log had 4 different ad-hoc CREATE TABLE statements
        # scattered across client_api.py/hosted_dashboard.py/marketing.py, some
        # missing post_id/post_platform — CREATE TABLE IF NOT EXISTS silently
        # no-ops on a table that already exists with the wrong shape, so a
        # database that hit the incomplete-schema path first stayed broken
        # forever. One canonical schema, centrally migrated, fixes it for good.
        """CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            content_type TEXT,
            topic TEXT,
            post_id TEXT,
            post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        "ALTER TABLE marketing_content_log ADD COLUMN post_id TEXT",
        "ALTER TABLE marketing_content_log ADD COLUMN post_platform TEXT",
        "ALTER TABLE marketing_content_log ADD COLUMN reach INTEGER DEFAULT 0",
        "ALTER TABLE marketing_content_log ADD COLUMN impressions INTEGER DEFAULT 0",
        "ALTER TABLE marketing_content_log ADD COLUMN engaged INTEGER DEFAULT 0",
        "ALTER TABLE marketing_content_log ADD COLUMN likes INTEGER DEFAULT 0",
        "ALTER TABLE marketing_content_log ADD COLUMN comments INTEGER DEFAULT 0",
        "ALTER TABLE marketing_content_log ADD COLUMN shares INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_mkt_content_restaurant ON marketing_content_log(restaurant_id, created_at)",
        # One generated content calendar per restaurant per week. It used to be
        # regenerated from scratch on every read: the web tab did it on a
        # button press, but /mobile/api/marketing called it on EVERY load, so
        # opening the Marketing tab on the phone fired a Sonnet call, blocked
        # the whole tab on it, and handed back a different "this week" every
        # time — while the generator's own comment claimed the week never
        # changes until Sunday. Now it's generated once and read after that.
        """CREATE TABLE IF NOT EXISTS content_calendar_cache (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            week_start      TEXT    NOT NULL,
            ideas_json      TEXT    NOT NULL,
            generated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, week_start)
        )""",

        # ── Marketing: scheduling, media, drafts, links, attribution ────────
        # Everything below exists because the module could only ever do one
        # thing at one moment: generate now, post now, to whoever is on the
        # list. A restaurant owner does admin at 11pm and posts on Tuesday
        # lunch; the photo is in their camera roll; the person writing the
        # copy often isn't the person who approves it; and nobody could say
        # whether any of it sold a pizza.

        # Uploaded photos live in the database rather than on disk or in
        # object storage. Two reasons: Railway's container filesystem is
        # rebuilt on every deploy, and Instagram fetches the image URL at
        # PUBLISH time — which for a scheduled post can be days after the
        # upload — so a file that disappears on deploy is a post that fails
        # silently later. Storing bytes here gives photos exactly the same
        # durability as every other piece of client data, with no new
        # infrastructure to configure. Images are downscaled and re-encoded
        # on upload (see marketing_media.py) so rows stay small.
        """CREATE TABLE IF NOT EXISTS marketing_media (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            token           TEXT    NOT NULL UNIQUE,
            mime            TEXT    NOT NULL,
            data            BLOB    NOT NULL,
            width           INTEGER,
            height          INTEGER,
            size_bytes      INTEGER,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mkt_media_restaurant ON marketing_media(restaurant_id, created_at)",

        # A post written now and published later. scheduled_for is stored in
        # the RESTAURANT's local wall clock, not UTC — the owner picks
        # "Tuesday 11am" meaning 11am in their dining room, and every other
        # time-of-day column in this schema (last_visit, schedule shifts)
        # already follows that convention.
        """CREATE TABLE IF NOT EXISTS marketing_scheduled_posts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            platform        TEXT    NOT NULL,
            content_type    TEXT,
            topic           TEXT,
            body            TEXT    NOT NULL,
            media_id        INTEGER REFERENCES marketing_media(id),
            cta_type        TEXT,
            cta_url         TEXT,
            scheduled_for   TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'scheduled',
            post_id         TEXT,
            error           TEXT,
            attempts        INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            posted_at       TEXT,
            -- Stamped when a scheduler tick takes the row for publishing, so
            -- a row abandoned by a dying process can be told apart from one
            -- that is simply due. See marketing_publish._claim_for_publish.
            claimed_at      TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mkt_sched_due ON marketing_scheduled_posts(status, scheduled_for)",
        "CREATE INDEX IF NOT EXISTS idx_mkt_sched_restaurant ON marketing_scheduled_posts(restaurant_id, scheduled_for)",

        # Generated copy survived exactly as long as the screen it was on.
        # A draft is the saved version; approving one is the separate act
        # that says it may go out, which is what lets a GM write and an owner
        # release.
        """CREATE TABLE IF NOT EXISTS marketing_drafts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            content_type    TEXT,
            topic           TEXT,
            body            TEXT    NOT NULL,
            media_id        INTEGER REFERENCES marketing_media(id),
            status          TEXT    NOT NULL DEFAULT 'draft',
            created_by      INTEGER,
            approved_by     INTEGER,
            approved_at     TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mkt_drafts_restaurant ON marketing_drafts(restaurant_id, updated_at)",

        # Nothing this module published was measurable once it left the
        # platform. A short link is the only way to know a text drove a
        # click, and SMS in particular carried no links at all.
        """CREATE TABLE IF NOT EXISTS marketing_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            token           TEXT    NOT NULL UNIQUE,
            target_url      TEXT    NOT NULL,
            label           TEXT,
            source          TEXT,
            campaign        TEXT,
            clicks          INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            last_click_at   TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mkt_links_restaurant ON marketing_links(restaurant_id, created_at)",

        # What a post did to the till. Cached per post because it reads POS
        # sales over a window and is not worth recomputing on every render.
        """CREATE TABLE IF NOT EXISTS marketing_attribution (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id     INTEGER NOT NULL REFERENCES restaurants(id),
            content_log_id    INTEGER NOT NULL,
            window_hours      INTEGER NOT NULL,
            baseline_sales    REAL,
            window_sales      REAL,
            lift_pct          REAL,
            computed_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(content_log_id, window_hours)
        )""",

        # Campaign history was written and never read back. Segment says WHO
        # it went to, which is the question an owner asks first when a list
        # of 200 reaches 40.
        # (guest_campaigns/guest_contacts columns live in
        # guest_marketing.init_guest_marketing — those tables are created
        # there, so an ALTER here runs before they exist and does nothing.)
        "ALTER TABLE marketing_content_log ADD COLUMN scheduled_post_id INTEGER",
        "ALTER TABLE marketing_content_log ADD COLUMN media_id INTEGER",
        "ALTER TABLE marketing_content_log ADD COLUMN link_token TEXT",
        "ALTER TABLE marketing_content_log ADD COLUMN posted_at TEXT",
        # Toast-driven food cost engine — persistent per-ingredient records
        # replacing the old re-parsed inventory_csv blob, plus a stock-event
        # ledger (recount/receiving/depletion/waste) and a recipe/BOM mapping
        # so ingredient usage can be computed from real Toast sales instead
        # of guessed. See inventory_ledger.py.
        """CREATE TABLE IF NOT EXISTS ingredients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            name            TEXT NOT NULL,
            category        TEXT,
            unit            TEXT,
            par_level       REAL DEFAULT 0,
            unit_cost       REAL DEFAULT 0,
            case_size       REAL DEFAULT 1.0,
            current_stock   REAL DEFAULT 0,
            avg_daily_usage REAL DEFAULT 0,
            last_order_qty  REAL DEFAULT 0,
            waste_last_week REAL DEFAULT 0,
            last_recount_at TEXT,
            is_active       INTEGER DEFAULT 1,
            -- Also in the ALTER list below, for databases created before
            -- suppliers existed. Both paths are needed: the ALTERs run
            -- before these CREATE TABLEs, so a brand-new database only
            -- gets these columns from here.
            supplier_name   TEXT,
            supplier_email  TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ingredients_restaurant ON ingredients(restaurant_id, is_active)",
        # One row per supplier per "send order" — the record of what was
        # actually ordered, which is also what lets receiving pre-fill
        # quantities instead of re-typing them off the invoice.
        """CREATE TABLE IF NOT EXISTS purchase_orders (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            po_number       TEXT NOT NULL,
            supplier_name   TEXT,
            supplier_email  TEXT,
            items_json      TEXT NOT NULL,
            total_cost      REAL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'sent',
            sent_at         TEXT NOT NULL DEFAULT (datetime('now')),
            received_at     TEXT,
            UNIQUE(restaurant_id, po_number)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_purchase_orders_restaurant ON purchase_orders(restaurant_id, sent_at)",
        # Was created lazily inside save_schedule_history, which meant a
        # fresh database simply didn't have it until someone happened to
        # generate a schedule — and schedule_shares below references it.
        # The lazy CREATE stays there too; both are IF NOT EXISTS.
        """CREATE TABLE IF NOT EXISTS schedule_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL,
            generated_at    TEXT NOT NULL DEFAULT (datetime('now')),
            week_start      TEXT,
            week_end        TEXT,
            hours_scheduled REAL,
            hours_budget    REAL,
            labor_target    REAL,
            schedule_csv    TEXT,
            summary_json    TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_schedule_history_restaurant ON schedule_history(restaurant_id, id)",
        # Second table with the same lazy-init history as schedule_history
        # above: it was only ever created by init_staff_availability(), so a
        # fresh database didn't have it until something happened to call
        # that. The staff-facing availability form depends on it existing.
        """CREATE TABLE IF NOT EXISTS staff_availability (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            employee_name    TEXT    NOT NULL,
            available_days   TEXT    NOT NULL DEFAULT '[]',
            unavailable_days TEXT,
            notes            TEXT,
            updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, employee_name)
        )""",
        # Where to reach each member of staff. Employees are identified by
        # NAME throughout this app (they come from POS shift data, not a
        # roster we own — see staff_availability/staff_notes, keyed the same
        # way), so this follows that convention rather than inventing an
        # employee id the POS wouldn't recognise.
        """CREATE TABLE IF NOT EXISTS staff_contacts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            employee_name   TEXT NOT NULL,
            email           TEXT,
            phone           TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, employee_name)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_staff_contacts_restaurant ON staff_contacts(restaurant_id)",
        # One row per employee per published schedule: the tokenised link
        # they were sent, and whether they have actually opened it. That
        # last part is the difference between "I sent the schedule" and "the
        # closing server has seen the schedule".
        """CREATE TABLE IF NOT EXISTS schedule_shares (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            schedule_id     INTEGER NOT NULL REFERENCES schedule_history(id),
            employee_name   TEXT NOT NULL,
            token           TEXT NOT NULL UNIQUE,
            sent_to         TEXT,
            sent_at         TEXT NOT NULL DEFAULT (datetime('now')),
            viewed_at       TEXT,
            view_count      INTEGER NOT NULL DEFAULT 0,
            expires_at      TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_schedule_shares_schedule ON schedule_shares(schedule_id)",
        "CREATE INDEX IF NOT EXISTS idx_schedule_shares_token ON schedule_shares(token)",
        # Also created lazily by init_email_log(), but a fresh database that
        # logged an email before that ran hit "no such table" — the same
        # lazy-init gap already fixed for schedule_history and
        # staff_availability. Both paths are IF NOT EXISTS, so keeping both
        # is harmless.
        """CREATE TABLE IF NOT EXISTS email_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER,
            email_type    TEXT,
            to_email      TEXT,
            subject       TEXT,
            sent_at       TEXT DEFAULT (datetime('now')),
            status        TEXT DEFAULT 'sent',
            error         TEXT,
            message_id    TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_email_log_restaurant ON email_log(restaurant_id, sent_at)",
        # Addresses Resend told us are undeliverable or that reported us as
        # spam. Suppressed at send time: retrying a hard bounce forever, or
        # continuing to mail someone who hit "report spam", is exactly what
        # burns a sending domain — and this domain also carries 2FA and
        # password-reset mail.
        """CREATE TABLE IF NOT EXISTS email_suppressions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT NOT NULL UNIQUE,
            reason      TEXT NOT NULL,
            detail      TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_email_suppressions_email ON email_suppressions(email)",
        # Ask Cavnar conversations. Previously the chat lived only in the
        # client's memory, so closing the app lost it entirely. Now that the
        # assistant can propose actions, this doubles as the record of what
        # was proposed and what the owner actually approved.
        """CREATE TABLE IF NOT EXISTS ask_cavnar_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            user_id       INTEGER,
            role          TEXT NOT NULL CHECK(role IN ('user','assistant')),
            content       TEXT NOT NULL,
            proposals     TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ask_cavnar_restaurant ON ask_cavnar_messages(restaurant_id, id)",
        # Chats, not one endless transcript. Each conversation is a
        # separate thread the owner can reopen or delete from the app's
        # chat history; a message belongs to exactly one. `title` is the
        # first question, set once and never rewritten.
        """CREATE TABLE IF NOT EXISTS ask_cavnar_conversations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            user_id       INTEGER,
            title         TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ask_conversations_restaurant "
        "ON ask_cavnar_conversations(restaurant_id, updated_at)",
        "ALTER TABLE ask_cavnar_messages ADD COLUMN conversation_id INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_ask_cavnar_conversation ON ask_cavnar_messages(conversation_id, id)",
        """CREATE TABLE IF NOT EXISTS ask_cavnar_actions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            user_id       INTEGER,
            action        TEXT NOT NULL,
            summary       TEXT,
            body          TEXT,
            outcome       TEXT NOT NULL DEFAULT 'proposed',
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        # event_type: 'recount' (qty = absolute counted amount) |
        # 'receiving' | 'depletion' | 'waste' (qty = signed delta).
        # source: 'toast' | 'manual' | 'admin' | 'inferred' | 'migration'.
        """CREATE TABLE IF NOT EXISTS ingredient_stock_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL,
            ingredient_id   INTEGER NOT NULL REFERENCES ingredients(id),
            event_type      TEXT NOT NULL,
            qty             REAL NOT NULL,
            event_date      TEXT NOT NULL,
            source          TEXT NOT NULL,
            note            TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_stock_events_ingredient ON ingredient_stock_events(restaurant_id, ingredient_id, event_date)",
        """CREATE TABLE IF NOT EXISTS menu_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL,
            toast_guid      TEXT,
            name            TEXT NOT NULL,
            is_active       INTEGER DEFAULT 1,
            -- What the dish sells for. Recipes already cost the plate;
            -- without a price there is no margin to show. Also in the ALTER
            -- list below for databases created before this existed.
            sell_price      REAL,
            UNIQUE(restaurant_id, toast_guid)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_menu_items_restaurant ON menu_items(restaurant_id, toast_guid)",
        """CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_item_id    INTEGER NOT NULL REFERENCES menu_items(id),
            ingredient_id   INTEGER NOT NULL REFERENCES ingredients(id),
            qty_per_unit    REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_recipe_menu_item ON recipe_ingredients(menu_item_id)",
        "CREATE INDEX IF NOT EXISTS idx_recipe_ingredient ON recipe_ingredients(ingredient_id)",
    ]
    for m in migrations:
        try:
            conn.execute(m)
        except Exception:
            pass  # column already exists
    conn.commit()
    _migrate_reviews_unique(conn)
    _ensure_place_id_uniqueness(conn)
    # Chats existed before conversations did — fold any pre-conversation
    # messages into one chat per restaurant so they show up in history.
    try:
        conn.row_factory = sqlite3.Row
        _adopt_legacy_ask_messages(conn)
    except Exception as e:
        print(f"ask_cavnar legacy adoption skipped: {e}")
    conn.close()
    # Ensure any columns managed by ensure_columns() are present before seeding
    ensure_columns()
    print(f"Database initialised at {db_path}")


def _auto_seed_demo_clients():
    """Seed demo client data on every startup if not already seeded from a real upload."""
    try:
        _seed_gia_mia()
    except Exception as e:
        print(f"[auto-seed] Gia Mia seed failed: {e}")
    try:
        _seed_simple_ejs()
    except Exception as e:
        print(f"[auto-seed] Simple EJ's seed failed: {e}")


# ── Simple EJ's — the working demo account ────────────────────────────────
#
# A separate restaurant rather than a rename of the existing demo, because
# that one's hours notes, scheduling rules and role rates name Gia Mia and
# its wood-fired pizza inside the text that feeds the scheduling prompt, and
# its Place ID is Gia Mia's real Google listing. Renaming it would have put
# another restaurant's operating rules, reviews and competitors behind
# Erik's name.
#
# EVERY NUMBER BELOW IS A PLACEHOLDER. It is a plausible mid-size bar and
# grill, not Erik's real operation, and it is flagged as such in the
# restaurant's internal notes so nobody mistakes it for confirmed data.
# Replace it with his real CSV and hours the moment you have them.
SIMPLE_EJS_NAME = "Simple EJ's"
SIMPLE_EJS_EMAIL = "cavnarwill@gmail.com"

_EJS_ROSTER = [
    # name, role, operational score, typical start, typical end, hours
    ("Marcus R.", "Bartender", 5, "3:00pm", "11:30pm", 8.5),
    ("Devon K.",  "Bartender", 4, "4:00pm", "11:30pm", 7.5),
    ("Priya S.",  "Bartender", 3, "4:00pm", "10:30pm", 6.5),
    ("Cole T.",   "Bartender", 2, "5:00pm", "11:00pm", 6.0),
    ("Angela M.", "Server",    5, "10:30am", "5:00pm", 6.5),
    ("Reuben O.", "Server",    4, "4:00pm", "10:00pm", 6.0),
    ("Hana W.",   "Server",    3, "11:00am", "5:00pm", 6.0),
    ("Trey B.",   "Server",    3, "4:30pm", "10:30pm", 6.0),
    ("Simone A.", "Server",    2, "5:00pm", "10:00pm", 5.0),
    ("Vince L.",  "Line Cook", 5, "2:00pm", "11:00pm", 9.0),
    ("Omar H.",   "Line Cook", 4, "3:00pm", "11:00pm", 8.0),
    ("Bea C.",    "Line Cook", 3, "3:00pm", "10:30pm", 7.5),
    ("Nico F.",   "Line Cook", 1, "4:00pm", "10:00pm", 6.0),
    ("Jules P.",  "Prep Cook", 4, "8:00am", "3:30pm", 7.5),
    ("Ari D.",    "Prep Cook", 3, "8:00am", "3:00pm", 7.0),
    ("Tessa G.",  "Host",      4, "4:00pm", "10:00pm", 6.0),
    ("Milo J.",   "Host",      3, "11:00am", "4:30pm", 5.5),
    ("Kase N.",   "Busser",    3, "4:30pm", "10:30pm", 6.0),
    ("Lupe V.",   "Busser",    2, "4:30pm", "10:30pm", 6.0),
]

# Who is trusted to lock up. Deliberately not the highest scores: being
# trusted with keys and cash is a different thing from being good on a
# Saturday, which is the whole reason the capability layer stores it as a
# flag rather than a point on the rating scale.
_EJS_CLOSERS = ("Marcus R.", "Angela M.", "Vince L.")

_EJS_HOURS = (
    "RESTAURANT HOURS: Open 11:00am Mon-Sat, 10:00am Sunday. "
    "Close: 10:00pm Sun-Wed; 12:00am Thu-Sat.\n\n"
    "STAFF ARRIVAL TIMES:\n"
    "- Prep cooks: arrive 8:00am every day.\n"
    "- Line cooks: first arrives 2:00pm; others stagger from 3:00pm.\n"
    "- Servers: first arrives 10:30am for side work before open.\n"
    "- Bartenders: evening only, no earlier than 3:00pm. Stay 30 minutes "
    "after close to break down the bar.\n"
    "- Hosts: one on at open, a second from 4:00pm Thu-Sat.\n\n"
    "SHIFT END / CLOSER RULES:\n"
    "- Keep 2 servers through close; cut the rest about an hour after the "
    "dinner rush drops.\n"
    "- 1 line cook always stays through close.\n"
    "- Bussers cut 30 minutes before close.\n"
    "- Thu-Sat: 2 bartenders close together; Sun-Wed one is enough.\n\n"
    "MINIMUM STAFFING FLOORS:\n"
    "- Servers: minimum 2 on the floor during any open hour; maximum 5 at once.\n"
    "- Line cooks: minimum 2 for dinner service every night, 3 Thu-Sat.\n"
    "- Bartenders: minimum 1 whenever the bar is open, 2 from 5:00pm Thu-Sat.\n"
    "- Hosts: minimum 1 whenever the dining room is open.\n"
    "- Bussers: minimum 1 at night, 2 Fri and Sat.\n\n"
    "SHIFT LENGTHS:\n"
    "- Servers 5-7h, bartenders 6-9h, line cooks 7-9h, prep 7-8h, "
    "hosts 5-6h, bussers 5-6h."
)

_EJS_SCHED_NOTES = (
    "Thursday through Saturday nights are the week. Sunday is a steady "
    "all-day trade rather than a rush. Monday and Tuesday are the quiet "
    "pair and are where somebody new should be learning."
)

_EJS_ROLE_RATES = {
    "Bartender": 9.00, "Server": 9.00, "Busser": 9.00, "Host": 15.00,
    "Line Cook": 21.00, "Prep Cook": 19.00,
}

_EJS_CLOSE_TIMES = {
    "Sunday": "10:00pm", "Monday": "10:00pm", "Tuesday": "10:00pm",
    "Wednesday": "10:00pm", "Thursday": "12:00am", "Friday": "12:00am",
    "Saturday": "12:00am",
}

# Bartenders are the one role authorised past close, to break down the bar.
_EJS_CLOSE_BUFFER = {"Bartender": 30}

# Per-role combined Operational Score targets, and the one leadership rule
# Erik described: a strong bartender on the busiest night.
_EJS_STRENGTH = {"Bartender": 8, "Line Cook": 9, "Server": 9}
_EJS_LEADER_RULES = [
    {"role": "Bartender", "days": ["Friday", "Saturday"], "daypart": "night",
     "min_score": 5, "count": 1},
    {"closing": True, "role": "Bartender", "attribute": "can_close", "count": 1},
]


def _seed_simple_ejs(db_path: str = DB_PATH):
    """Create and seed the Simple EJ's demo account, idempotently.

    Never touches a restaurant carrying real uploaded shifts, never touches
    Gia Mia, and never writes settings over an admin edit — the same three
    guards the existing demo seed uses, for the same reason: a redeploy must
    not silently undo work done in the admin panel.
    """
    from datetime import date, timedelta
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id, is_demo FROM restaurants WHERE name=? LIMIT 1",
                           (SIMPLE_EJS_NAME,)).fetchone()
    finally:
        conn.close()

    if row:
        rid = row["id"]
        if not row["is_demo"]:
            print(f"[auto-seed] {SIMPLE_EJS_NAME} (id={rid}) is no longer flagged demo — leaving it alone")
            return rid
    else:
        rid = create_restaurant(Restaurant(
            name=SIMPLE_EJS_NAME, owner_email=SIMPLE_EJS_EMAIL, owner_name="Erik",
            is_demo=1, module_reviews=1, module_labor=1, module_inventory=1,
            module_marketing=1, service_tier="full", timezone="America/Chicago",
            location_name="Simple EJ's", hourly_rate=12.50, labor_target_pct=26.0,
        ), db_path=db_path)
        print(f"[auto-seed] created {SIMPLE_EJS_NAME} as id={rid}")

    _seed_ejs_history(rid, db_path)
    _seed_ejs_settings(rid, db_path)
    _seed_ejs_shifts(rid, db_path)
    _seed_ejs_capabilities(rid, db_path)
    _ensure_ejs_login(rid, db_path)
    return rid


def _seed_ejs_history(rid: int, db_path: str):
    """Four weeks of daily sales and hours, so demand, year-over-year and the
    PAR budget all have something real to work from."""
    from datetime import date, timedelta
    # Monday quiet through Saturday peak. Sunday is steady all-day trade.
    BY_WEEKDAY = {0: (4100, 52), 1: (3900, 50), 2: (5200, 61), 3: (7400, 78),
                  4: (11800, 116), 5: (13200, 128), 6: (8600, 90)}
    conn = get_conn(db_path)
    try:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        d = date(2025, 6, 2)
        while d <= date(2025, 6, 29):
            sales, hours = BY_WEEKDAY[d.weekday()]
            cost = round(hours * 12.5, 2)
            conn.execute("""INSERT OR REPLACE INTO labor_daily_history
                (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales,
                 total_hours, saved_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                (rid, d.strftime("%Y-%m-%d"), days[d.weekday()],
                 round(cost / sales * 100, 2), cost, float(sales), float(hours)))
            d += timedelta(days=1)
        conn.commit()
    finally:
        conn.close()


def _seed_ejs_settings(rid: int, db_path: str):
    """Hours, rates, close times and targets — only on a restaurant that has
    never been configured, so an admin edit is never overwritten."""
    existing = get_restaurant(rid, db_path)
    if existing and (existing.hours_notes or "").strip():
        return
    update_restaurant(rid, {
        "monthly_revenue_target": 232000.0,
        "labor_target_pct": 26.0,
        "hourly_rate": 12.50,
        "hours_notes": _EJS_HOURS,
        "sched_notes": _EJS_SCHED_NOTES,
        "section_count": 5,
        "daypart_split": "lunch 30%, dinner 70%",
        "role_rates_json": json.dumps(_EJS_ROLE_RATES),
        "close_times_json": json.dumps(_EJS_CLOSE_TIMES),
        "role_close_buffer_json": json.dumps(_EJS_CLOSE_BUFFER),
        "role_strength_json": json.dumps(_EJS_STRENGTH),
        "shift_leader_rules_json": json.dumps(_EJS_LEADER_RULES),
        "role_minimums_json": json.dumps({"Bartender": 1, "Server": 2, "Line Cook": 2}),
        "internal_notes": ("DEMO ACCOUNT. Every figure here is a placeholder written to "
                           "give the product something realistic to run on — hours, wages, "
                           "sales, roster and ratings are all invented. Replace with Erik's "
                           "real CSV and hours before treating any number as his."),
        "email_theme": "dark",
    })
    print(f"[auto-seed] {SIMPLE_EJS_NAME} settings written")


def _seed_ejs_shifts(rid: int, db_path: str):
    """Two weeks of shifts, generated rather than hand-written so the roster,
    the day-of-week volume and the role mix stay consistent with each other."""
    from datetime import date, timedelta
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT shifts_source FROM client_data WHERE restaurant_id=?",
                           (rid,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if row and row["shifts_source"] in ("upload", "toast"):
        return   # a real upload always wins

    BY_WEEKDAY = {0: (4100, 0.55), 1: (3900, 0.55), 2: (5200, 0.7), 3: (7400, 0.85),
                  4: (11800, 1.0), 5: (13200, 1.0), 6: (8600, 0.8)}
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    lines = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,"
             "actual_hours,sales,notes"]
    d = date(2026, 8, 31)                      # a Monday
    for _ in range(14):
        sales, share = BY_WEEKDAY[d.weekday()]
        # A quieter day drops the back half of each role rather than
        # thinning every role evenly — which is how a real rota shrinks.
        by_role = {}
        for name, role, _score, start, end, hours in _EJS_ROSTER:
            by_role.setdefault(role, []).append((name, start, end, hours))
        for role, people in by_role.items():
            keep = max(1, int(round(len(people) * share)))
            for name, start, end, hours in people[:keep]:
                lines.append(f"{d.strftime('%Y-%m-%d')},{days[d.weekday()]},{name},{role},"
                             f"{start},{end},{hours},{hours},{sales},")
        d += timedelta(days=1)
    save_client_data(rid, "shifts", "\n".join(lines), source="seed")
    print(f"[auto-seed] {SIMPLE_EJS_NAME} shift data written ({len(lines) - 1} rows)")


def _seed_ejs_capabilities(rid: int, db_path: str):
    """Operational Scores and closer flags, so the Shift Quality engine has
    something to show rather than sitting dormant behind a demo."""
    existing = get_capabilities(rid, db_path=db_path)
    if existing:
        return   # already rated, by this seed or by hand
    for name, _role, score, _s, _e, _h in _EJS_ROSTER:
        try:
            set_capability(rid, name, score=score, updated_by="seed", db_path=db_path)
        except Exception:
            pass
    for name in _EJS_CLOSERS:
        try:
            set_capability(rid, name, attribute="can_close", flag=True,
                           updated_by="seed", db_path=db_path)
        except Exception:
            pass
    print(f"[auto-seed] {SIMPLE_EJS_NAME} ratings written for {len(_EJS_ROSTER)} staff")


def _ensure_ejs_login(rid: int, db_path: str):
    """A login for the demo account, created once with a random password.

    Printed to the log and stored in restaurants.temp_password, which the
    admin client card already surfaces — the same place the Add Client form
    puts a new client's first password.
    """
    import secrets
    from auth import create_user
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id FROM users WHERE restaurant_id=? LIMIT 1",
                           (rid,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if row:
        return
    password = secrets.token_urlsafe(9)
    try:
        create_user(rid, "erik", "erik+demo@cavnar.ai", password, db_path=db_path)
        update_restaurant(rid, {"temp_password": password})
        print(f"[auto-seed] {SIMPLE_EJS_NAME} login created — username 'erik', "
              f"password {password} (also on the admin client card)")
    except Exception as e:
        print(f"[auto-seed] {SIMPLE_EJS_NAME} login not created: {e}")


def _seed_gia_mia(db_path: str = DB_PATH):
    """Seed Gia Mia (id=2) labor history + shift CSV unless real upload exists."""
    from datetime import date, timedelta
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    # Check if real (non-seed) shifts already uploaded
    try:
        row = conn.execute(
            "SELECT shifts_source FROM client_data WHERE restaurant_id=2"
        ).fetchone()
    except Exception:
        conn.close()
        return
    skip_shift_seed = row and row["shifts_source"] in ("upload", "toast")
    conn.close()

    if not skip_shift_seed:
        # Seed June 2025 daily history (YoY context for schedule generation)
        _conn2 = sqlite3.connect(db_path, timeout=5)
        _conn2.row_factory = sqlite3.Row
        _conn2.execute("PRAGMA journal_mode=WAL")
        DAY_TEMPLATES = {
            0: {"sales": 8200,  "hours": 71},
            1: {"sales": 8800,  "hours": 76},
            2: {"sales": 10500, "hours": 91},
            3: {"sales": 12200, "hours": 105},
            4: {"sales": 16400, "hours": 142},
            5: {"sales": 18800, "hours": 163},
            6: {"sales": 13200, "hours": 114},
        }
        HOLIDAY_OVERRIDES = {"2025-06-15": {"sales": 22400, "hours": 194}}
        DAYS_OF_WEEK = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        d = date(2025, 6, 2)
        while d <= date(2025, 6, 29):
            ds = d.strftime("%Y-%m-%d")
            tmpl = HOLIDAY_OVERRIDES.get(ds, DAY_TEMPLATES[d.weekday()])
            sales = float(tmpl["sales"])
            hours = float(tmpl["hours"])
            labor_cost = round(hours * 26, 2)
            labor_pct  = round(labor_cost / sales * 100, 2)
            _conn2.execute("""
                INSERT OR REPLACE INTO labor_daily_history
                  (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, total_hours, saved_at)
                VALUES (?,?,?,?,?,?,?,datetime('now'))
            """, (2, ds, DAYS_OF_WEEK[d.weekday()], labor_pct, labor_cost, sales, hours))
            d += timedelta(days=1)
        _conn2.commit()
        _conn2.close()

    gia_mia_hours = (
        "RESTAURANT HOURS: Open 11:00am daily. "
        "Close: 9:00pm Sun–Wed; 10:00pm Thu–Sat.\n\n"
        "STAFF ARRIVAL TIMES (hard rules — do not deviate):\n"
        "- Bussers: arrive 8:00am every day.\n"
        "- Cooks: arrive 8:30am Mon–Thu; arrive 8:00am Fri, Sat, Sun.\n"
        "- Servers: arrive 10:00am every day (1 hour before 11am open for side work).\n"
        "- Bartenders: NO morning shifts — evening only. Start no earlier than 3:00pm. "
        "Stay 1 hour after close: until 10:00pm Sun–Wed; until 11:00pm Thu–Sat.\n"
        "- Food Runners: arrive with kitchen for dinner service. "
        "On Pizza Mondays and Fridays, also scheduled for lunch.\n\n"
        "SHIFT END / CLOSER RULES:\n"
        "- Always keep 2 servers as closers (until restaurant close time).\n"
        "- Cut all other servers 1–1.5h before close when volume allows.\n"
        "- Fri/Sat: 3 server closers + 2 bartender closers.\n"
        "- Cooks: 1 cook always stays through close; cut others 45min–1h early on slow nights.\n"
        "- Bussers cut 30min before close.\n"
        "- Food Runners: cut after dinner rush, typically 1–2h before close.\n\n"
        "FLOOR LAYOUT & SECTIONS:\n"
        "- 31 tables inside, 24 tables on patio (patio open May–Labor Day).\n"
        "- Sections: 10s, 20s, 30s, 40s, PDR (private dining room — events only), outside patio.\n"
        "- Each server handles approximately 6 tables per section.\n"
        "- HARD CAP: never schedule more than 7 servers at once. "
        "Only in extreme circumstances would 8 ever be needed — avoid this.\n\n"
        "SERVER STAGGER RULES:\n"
        "- Exactly ONE server opens (arrives 10:00am, 1h before 11am open).\n"
        "- Second server starts at 11am (open) or 11:30am depending on day volume.\n"
        "- Additional servers only at 12pm+ and only when YoY/event data backs it up.\n"
        "- Never two servers at the same start time.\n\n"
        "MINIMUM STAFFING FLOORS:\n"
        "- Servers: minimum 3 on the floor during ANY open service hour, every single day, "
        "morning and night alike -- 2 is never enough to cover the floor, that's a hard "
        "rule regardless of how slow TYPICAL HEADCOUNT makes a given shift look. Maximum 7 "
        "at once.\n"
        "- Double shifts: before adding closers for dinner/night service, first count "
        "anyone ALREADY on the floor from a double shift that day (scheduled for both "
        "morning and night) -- they already count toward the night total, don't add new "
        "closers on top of them as if the floor were starting from zero. Add up double-"
        "shift carryovers plus newly-scheduled closers and check that total against both "
        "the 3-minimum floor above and the 7-person hard cap before finalizing -- 8 "
        "servers at once has never actually happened at Gia Mia and should never be "
        "scheduled.\n"
        "- Cooks/Kitchen for morning/lunch service: minimum 2 people total, every single "
        "day -- 1 Prep/Pantry cook alone is never enough. Scale to 3 on Pizza Monday and "
        "weekends. This is a hard floor, not a historical average, same as the dinner "
        "floor below -- a morning with only 1 cook and 1 server on is never correct, no "
        "matter what day it is.\n"
        "- Cooks/Kitchen for dinner/night service: minimum 4 people total, every single "
        "night. Count them: 1 on Pizza (wood-fired pizza is Gia Mia's signature dish, so "
        "Pizza station is never left empty during service, no exceptions) + 3 on "
        "Pantry/Saute = 4 minimum. On Pizza Monday and weekends, scale to 2 on Pizza + 5 "
        "on Pantry/Saute = 7 minimum. This is a hard floor, not a historical average -- "
        "even on the slowest night of the week, count your kitchen rows and confirm there "
        "are at least 4 before finalizing, regardless of what TYPICAL HEADCOUNT shows.\n"
        "- Pizza Cook: minimum 1 on the Pizza station at all times, every open service "
        "hour, all 7 days -- morning/lunch AND dinner/night alike, no exceptions. The "
        "wood-fired oven is never left unstaffed, regardless of what TYPICAL HEADCOUNT "
        "shows for that specific shift. This is one of the people already counted in the "
        "Cooks/Kitchen totals above (already explicit for dinner: 1 of the 4; for "
        "morning/lunch, 1 of the 2) -- not an extra body added on top, but that slot must "
        "specifically be a Pizza Cook, not just any cook.\n"
        "- Bartenders: minimum 1 whenever the bar is open.\n"
        "- Hosts: minimum 1 whenever the dining room is open.\n"
        "- Bussers, NIGHT service: minimum 2 on at once, every single night, no exceptions "
        "-- this applies all 7 days, not just Pizza Monday/weekends. This is a hard floor, "
        "not a historical average.\n"
        "- Bussers, MORNING/lunch service: minimum 2 on at once every day EXCEPT Tuesday, "
        "Wednesday, and Thursday, where 1 is acceptable if that's what TYPICAL HEADCOUNT "
        "shows. Monday, Friday, Saturday, Sunday mornings still need 2 minimum.\n"
        "- Food Runners: see FOOD RUNNER RULES below for the full pattern.\n\n"
        "SHIFT LENGTHS:\n"
        "- Servers: 4–7h. Openers run 6–7h through lunch. Closers run 5–7h.\n"
        "- Bartenders: 6–9h. Closers stay 1h after restaurant close.\n"
        "- Cooks: 6–10h. Kitchen closers often need 9–10h for full service + breakdown.\n"
        "- Hosts: 5–8h. One opener, close when last table is seated.\n"
        "- Food Runners: 4–6h dinner-only; 8–10h on days they run both lunch and dinner.\n"
        "- Bussers: 6–9h.\n\n"
        "PIZZA MONDAY RULE:\n"
        "- Monday is Pizza Monday (half-price pizzas all day) — significantly busier than a typical Monday.\n"
        "- Staff Monday closer to a busy Friday than a slow weekday (see FOOD RUNNER RULES below for its food runner coverage).\n\n"
        "FOOD RUNNER RULES:\n"
        "- 1 food runner on for dinner/night service every night of the week — baseline, no exceptions.\n"
        "- Pizza Monday and weekend nights (Fri, Sat, Sun): 2 food runners for dinner/night service.\n"
        "- Occasionally — only if labor % and volume genuinely support it — add 1 food runner for a weekday "
        "morning/lunch shift (Tue-Thu). This is optional and should be rare; never schedule it as a fixed weekly requirement.\n"
        "- Never more than 2 food runners at once, except for a private PDR event."
    )
    gia_mia_sched_notes = (
        "Monday is Pizza Monday — treat Monday lunch like a busy Friday for staffing (see hours notes for food "
        "runner coverage specifics). "
        "Hard cap: never exceed 7 servers on floor at once. "
        "PDR (private dining room) is separate from floor sections and requires a dedicated server for private events."
    )
    # Real base wages (Illinois — a tip-credit state, so tipped roles carry
    # a low base wage; tips are guest money, not a restaurant labor cost, so
    # base wage is the right figure for labor-cost-% and PAR math, not a
    # loaded base+tip-makeup number). Confirmed with the client. Carry Out
    # is host duty (see gia_mia_sched_notes), so it shares the Host rate.
    # Runner and Shift Supervisor rates are this session's own estimate
    # (tipped-support parity with Busser/Bartender for Runner; a modest
    # premium over Server base for the added Shift Supervisor
    # responsibility) — not confirmed with the client, flagged here so
    # they're easy to find and correct later.
    gia_mia_role_rates = {
        "Server": 9.00, "Host": 15.00, "Busser": 9.00, "Bartender": 9.00,
        "Pantry Cook": 22.00, "Prep Cook": 22.00, "Saute Cook": 22.00, "Pizza Cook": 22.00,
        "Runner": 9.00, "Carry Out": 15.00, "Shift Supervisor": 12.00,
    }
    # Matches hours_notes' own "RESTAURANT HOURS" line exactly — the
    # generator was told this in prose already, but prose alone let it
    # occasionally borrow Thu-Sat's later close for a Sun-Wed night (e.g.
    # scheduling Monday servers to 9:30-10pm when Monday actually closes at
    # 9pm). This structured copy is what the post-generation enforcement in
    # client_api.py checks every shift_end against — a hard cap, not a
    # request.
    gia_mia_close_times = {
        "Sunday": "9:00pm", "Monday": "9:00pm", "Tuesday": "9:00pm", "Wednesday": "9:00pm",
        "Thursday": "10:00pm", "Friday": "10:00pm", "Saturday": "10:00pm",
    }
    # The only role hours_notes explicitly authorizes to run past close
    # ("Stay 1 hour after close"). Every other role defaults to 0 — must
    # end at or before that day's close time.
    gia_mia_role_close_buffer = {"Bartender": 60}
    # Only apply on a genuinely fresh/never-configured restaurant (or one
    # that somehow lost its notes) — this used to run unconditionally on
    # every single server restart, which meant any admin-panel edit to
    # these exact fields (hours_notes, role_rates_json, close_times_json,
    # etc. — all editable from client_settings.html) got silently
    # overwritten back to these hardcoded values the next time the server
    # redeployed. hours_notes empty is the signal "still needs seeding";
    # once it's set (by this block or by a real admin edit), it never gets
    # blown away again just because the process restarted.
    _existing = get_restaurant(2, db_path)
    if not _existing or not (_existing.hours_notes or "").strip():
        # Use update_restaurant so the correct DB connection path is always used
        update_restaurant(2, {
            "monthly_revenue_target": 365000.0,
            "labor_target_pct": 23.0,
            # Was a flat 26.0 — nowhere close to any real role's actual wage,
            # which meant PAR's hours_budget (dollars / rate) was computed
            # against a rate roughly double the real weighted blended rate
            # (~$13.53/hr given the current role/hours mix), understating
            # achievable hours by about half. role_rates_json below is the
            # real fix (per-role, used everywhere cost is computed); this flat
            # value now only matters as the last-resort fallback for a role
            # that isn't in role_rates_json, so it's set to roughly match the
            # real blended rate rather than being wildly high.
            "hourly_rate": 13.50,
            "hours_notes": gia_mia_hours,
            "sched_notes": gia_mia_sched_notes,
            "section_count": 7,
            "daypart_split": "lunch 40%, dinner 60%",
            "role_rates_json": json.dumps(gia_mia_role_rates),
            "close_times_json": json.dumps(gia_mia_close_times),
            "role_close_buffer_json": json.dumps(gia_mia_role_close_buffer),
            "location_name": "St. Charles, IL",
            "email_theme": "dark",
        })
        print("[auto-seed] Restaurant settings updated: labor_target=23%, monthly_revenue=$365k")
    else:
        print("[auto-seed] Restaurant settings already configured — skipping (won't clobber admin edits)")

    gia_mia_csv = """date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes
2026-06-01,Monday,Derek M.,Bartender,4:00pm,10:30pm,6.5,6.5,8000,
2026-06-01,Monday,Kim T.,Bartender,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Rosa M.,Bartender,4:00pm,10:30pm,6.5,6.5,8000,
2026-06-01,Monday,Tomas H.,Bartender,5:00pm,11:30pm,6.5,6.5,8000,
2026-06-01,Monday,Cody M.,Busser,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Rowan K.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-01,Monday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-01,Monday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-01,Monday,Jonah S.,Carry Out,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-01,Monday,Rhea D.,Carry Out,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-01,Monday,James H.,Host,11:00am,5:00pm,6.0,6.0,8000,
2026-06-01,Monday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-01,Monday,Piper A.,Host,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-01,Monday,Freya S.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Ivy R.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-01,Monday,Leo K.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-01,Monday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,8000,
2026-06-01,Monday,Farah A.,Prep Cook,8:00am,3:30pm,7.5,7.5,8000,
2026-06-01,Monday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,8000,
2026-06-01,Monday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,8000,
2026-06-01,Monday,Felix G.,Saute Cook,3:00pm,11:00pm,8.0,8.0,8000,
2026-06-01,Monday,Layla N.,Saute Cook,5:00pm,11:30pm,6.5,6.5,8000,
2026-06-01,Monday,Nora J.,Saute Cook,3:00pm,11:00pm,8.0,8.0,8000,
2026-06-01,Monday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,8000,
2026-06-01,Monday,Theo A.,Saute Cook,3:00pm,11:00pm,8.0,8.0,8000,
2026-06-01,Monday,Bella C.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Derek M.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Diego L.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Jamie L.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Marco D.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Marcus T.,Server,11:00am,5:00pm,6.0,6.0,8000,
2026-06-01,Monday,Mason C.,Server,11:00am,5:00pm,6.0,6.0,8000,
2026-06-01,Monday,Maya R.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Noah K.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Priya K.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Rosa M.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-01,Monday,Sienna P.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-01,Monday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Zoe H.,Server,5:00pm,11:00pm,6.0,6.0,8000,
2026-06-02,Tuesday,James H.,Carry Out,4:00pm,10:00pm,6.0,6.0,9000,
2026-06-02,Tuesday,Marisol T.,Host,11:00am,5:00pm,6.0,6.0,9000,
2026-06-02,Tuesday,Carlos B.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Hector M.,Pantry Cook,10:30am,6:30pm,8.0,8.0,9000,
2026-06-02,Tuesday,Wei C.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Miles B.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Nikolai P.,Prep Cook,8:00am,3:30pm,7.5,7.5,9000,
2026-06-02,Tuesday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,9000,
2026-06-02,Tuesday,Felix G.,Saute Cook,5:00pm,11:30pm,6.5,6.5,9000,
2026-06-02,Tuesday,Diego L.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Priya K.,Server,5:00pm,11:00pm,6.0,6.0,9000,
2026-06-02,Tuesday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-03,Wednesday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,Kim T.,Bartender,4:00pm,10:30pm,6.5,6.5,11000,
2026-06-03,Wednesday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,James H.,Host,11:00am,5:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Owen K.,Host,4:00pm,10:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Aisha K.,Pantry Cook,10:30am,6:30pm,8.0,8.0,11000,
2026-06-03,Wednesday,Bruno T.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Carlos B.,Pantry Cook,10:30am,6:30pm,8.0,8.0,11000,
2026-06-03,Wednesday,Dante F.,Pantry Cook,10:30am,6:30pm,8.0,8.0,11000,
2026-06-03,Wednesday,Hector M.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,Jasper N.,Pizza Cook,8:00am,4:00pm,8.0,8.0,11000,
2026-06-03,Wednesday,Leo K.,Pizza Cook,8:00am,4:00pm,8.0,8.0,11000,
2026-06-03,Wednesday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,Amy C.,Prep Cook,10:00am,5:30pm,7.5,7.5,11000,
2026-06-03,Wednesday,Camila G.,Prep Cook,8:00am,3:30pm,7.5,7.5,11000,
2026-06-03,Wednesday,Duncan L.,Prep Cook,8:00am,3:30pm,7.5,7.5,11000,
2026-06-03,Wednesday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,11000,
2026-06-03,Wednesday,Isla V.,Saute Cook,5:00pm,11:30pm,6.5,6.5,11000,
2026-06-03,Wednesday,Diego L.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Gina F.,Server,4:00pm,9:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Jamie L.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Kim T.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Liam P.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Marco D.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Priya K.,Server,4:00pm,9:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Xavier R.,Server,4:00pm,9:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Zoe H.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-04,Thursday,Derek M.,Bartender,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-04,Thursday,Kim T.,Bartender,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Rosa M.,Bartender,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-04,Thursday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Delphine A.,Busser,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,13000,
2026-06-04,Thursday,Piper A.,Carry Out,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Xavier R.,Carry Out,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,James H.,Host,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Rhea D.,Host,11:00am,5:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Carlos B.,Pantry Cook,10:30am,6:30pm,8.0,8.0,13000,
2026-06-04,Thursday,Simone R.,Pantry Cook,8:00am,4:00pm,8.0,8.0,13000,
2026-06-04,Thursday,Jasper N.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,13000,
2026-06-04,Thursday,Kenji H.,Prep Cook,10:00am,5:30pm,7.5,7.5,13000,
2026-06-04,Thursday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,13000,
2026-06-04,Thursday,Caleb W.,Saute Cook,8:30am,4:30pm,8.0,8.0,13000,
2026-06-04,Thursday,Isla V.,Saute Cook,3:00pm,11:00pm,8.0,8.0,13000,
2026-06-04,Thursday,Layla N.,Saute Cook,3:00pm,11:00pm,8.0,8.0,13000,
2026-06-04,Thursday,Nora J.,Saute Cook,8:30am,4:30pm,8.0,8.0,13000,
2026-06-04,Thursday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,13000,
2026-06-04,Thursday,Ava S.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Diego L.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Elena V.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Ethan M.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Gina F.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Grace T.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Omar T.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Ruby F.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-04,Thursday,Sofia R.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-05,Friday,Derek M.,Bartender,4:00pm,10:30pm,6.5,6.5,18000,
2026-06-05,Friday,Tomas H.,Bartender,4:00pm,10:30pm,6.5,6.5,18000,
2026-06-05,Friday,Rowan K.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-05,Friday,Sam V.,Busser,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-05,Friday,Tony A.,Busser,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-05,Friday,Bella C.,Carry Out,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Jonah S.,Carry Out,4:00pm,10:00pm,6.0,6.0,18000,
2026-06-05,Friday,Lena S.,Carry Out,11:00am,5:00pm,6.0,6.0,18000,
2026-06-05,Friday,Sienna P.,Host,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Bruno T.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,18000,
2026-06-05,Friday,Hector M.,Pantry Cook,8:00am,4:00pm,8.0,8.0,18000,
2026-06-05,Friday,Simone R.,Pantry Cook,10:30am,6:30pm,8.0,8.0,18000,
2026-06-05,Friday,Jasper N.,Pizza Cook,8:00am,4:00pm,8.0,8.0,18000,
2026-06-05,Friday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-05,Friday,Amy C.,Prep Cook,10:00am,5:30pm,7.5,7.5,18000,
2026-06-05,Friday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,18000,
2026-06-05,Friday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,18000,
2026-06-05,Friday,Caleb W.,Saute Cook,8:30am,4:30pm,8.0,8.0,18000,
2026-06-05,Friday,Isla V.,Saute Cook,5:00pm,11:30pm,6.5,6.5,18000,
2026-06-05,Friday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,18000,
2026-06-05,Friday,Theo A.,Saute Cook,3:00pm,11:00pm,8.0,8.0,18000,
2026-06-05,Friday,Diego L.,Server,4:00pm,9:30pm,5.5,5.5,18000,
2026-06-05,Friday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,18000,
2026-06-05,Friday,Marcus T.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-05,Friday,Nina W.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-05,Friday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-05,Friday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Tomas H.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Tony A.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-05,Friday,Marcus T.,Shift Supervisor,4:00pm,10:00pm,6.0,6.0,18000,
2026-06-06,Saturday,Kim T.,Bartender,5:00pm,11:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Omar T.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-06,Saturday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-06,Saturday,Cody M.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Delphine A.,Busser,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-06,Saturday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Owen K.,Carry Out,4:00pm,10:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Bella C.,Host,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Dario V.,Host,4:00pm,10:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Tobias N.,Host,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Xavier R.,Host,4:00pm,10:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Aisha K.,Pantry Cook,10:30am,6:30pm,8.0,8.0,20000,
2026-06-06,Saturday,Bruno T.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-06,Saturday,Dante F.,Pantry Cook,8:00am,4:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-06,Saturday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-06,Saturday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Duncan L.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Farah A.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Nikolai P.,Prep Cook,8:00am,3:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Yara S.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-06,Saturday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,20000,
2026-06-06,Saturday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,20000,
2026-06-06,Saturday,Isla V.,Saute Cook,5:00pm,11:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Nora J.,Saute Cook,3:00pm,11:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Raj P.,Saute Cook,3:00pm,11:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Theo A.,Saute Cook,3:00pm,11:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Chloe B.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Diego L.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-06,Saturday,Elena V.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Ethan M.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Gina F.,Server,4:00pm,9:30pm,5.5,5.5,20000,
2026-06-06,Saturday,James H.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-06,Saturday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Lena S.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Liam P.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Marco D.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-06,Saturday,Marcus T.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Mason C.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Nina W.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Noah K.,Server,4:00pm,9:30pm,5.5,5.5,20000,
2026-06-06,Saturday,Priya K.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-07,Sunday,Kim T.,Bartender,4:00pm,10:30pm,6.5,6.5,12250,
2026-06-07,Sunday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,12250,
2026-06-07,Sunday,Tony A.,Busser,4:30pm,11:00pm,6.5,6.5,12250,
2026-06-07,Sunday,Marisol T.,Carry Out,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Dario V.,Host,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,James H.,Host,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Sienna P.,Host,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Tobias N.,Host,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,12250,
2026-06-07,Sunday,Simone R.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Wei C.,Pantry Cook,10:30am,6:30pm,8.0,8.0,12250,
2026-06-07,Sunday,Freya S.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,12250,
2026-06-07,Sunday,Camila G.,Prep Cook,10:00am,5:30pm,7.5,7.5,12250,
2026-06-07,Sunday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,12250,
2026-06-07,Sunday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,12250,
2026-06-07,Sunday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-07,Sunday,Ava S.,Server,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Diego L.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Elena V.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Gina F.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Grace T.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,12250,
2026-06-07,Sunday,Marco D.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Marcus T.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-07,Sunday,Owen D.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Sofia R.,Server,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-08,Monday,Derek M.,Bartender,4:00pm,10:30pm,6.5,6.5,8000,
2026-06-08,Monday,Kim T.,Bartender,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-08,Monday,Omar T.,Bartender,4:00pm,10:30pm,6.5,6.5,8000,
2026-06-08,Monday,Delphine A.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-08,Monday,Rowan K.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-08,Monday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,8000,
2026-06-08,Monday,Tony A.,Busser,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-08,Monday,James H.,Carry Out,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Lena S.,Carry Out,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-08,Monday,Owen K.,Carry Out,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Rhea D.,Carry Out,11:00am,5:00pm,6.0,6.0,8000,
2026-06-08,Monday,Aisha K.,Pantry Cook,8:00am,4:00pm,8.0,8.0,8000,
2026-06-08,Monday,Carlos B.,Pantry Cook,10:30am,6:30pm,8.0,8.0,8000,
2026-06-08,Monday,Dante F.,Pantry Cook,10:30am,6:30pm,8.0,8.0,8000,
2026-06-08,Monday,Wei C.,Pantry Cook,10:30am,6:30pm,8.0,8.0,8000,
2026-06-08,Monday,Ivy R.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Leo K.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,8000,
2026-06-08,Monday,Miles B.,Pizza Cook,8:00am,4:00pm,8.0,8.0,8000,
2026-06-08,Monday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,8000,
2026-06-08,Monday,Yara S.,Prep Cook,8:00am,3:30pm,7.5,7.5,8000,
2026-06-08,Monday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,8000,
2026-06-08,Monday,Caleb W.,Saute Cook,8:30am,4:30pm,8.0,8.0,8000,
2026-06-08,Monday,Felix G.,Saute Cook,5:00pm,11:30pm,6.5,6.5,8000,
2026-06-08,Monday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,8000,
2026-06-08,Monday,Diego L.,Server,5:00pm,11:00pm,6.0,6.0,8000,
2026-06-08,Monday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Jamie L.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-08,Monday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,8000,
2026-06-08,Monday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Mason C.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Nina W.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Omar T.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Tomas H.,Server,4:30pm,10:30pm,6.0,6.0,8000,
2026-06-08,Monday,Xavier R.,Server,4:00pm,9:30pm,5.5,5.5,8000,
2026-06-09,Tuesday,Dario V.,Host,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,9000,
2026-06-09,Tuesday,Carlos B.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Freya S.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,9000,
2026-06-09,Tuesday,Talia D.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,9000,
2026-06-09,Tuesday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,9000,
2026-06-09,Tuesday,Felix G.,Saute Cook,3:00pm,11:00pm,8.0,8.0,9000,
2026-06-09,Tuesday,Theo A.,Saute Cook,5:00pm,11:30pm,6.5,6.5,9000,
2026-06-09,Tuesday,Marcus T.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Nina W.,Server,5:00pm,11:00pm,6.0,6.0,9000,
2026-06-09,Tuesday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Zoe H.,Server,4:00pm,9:30pm,5.5,5.5,9000,
2026-06-10,Wednesday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-10,Wednesday,Tomas H.,Bartender,5:00pm,11:30pm,6.5,6.5,11000,
2026-06-10,Wednesday,Sienna P.,Carry Out,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Bella C.,Host,4:00pm,10:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Aisha K.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Bruno T.,Pantry Cook,10:30am,6:30pm,8.0,8.0,11000,
2026-06-10,Wednesday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,11000,
2026-06-10,Wednesday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-10,Wednesday,Jasper N.,Pizza Cook,8:00am,4:00pm,8.0,8.0,11000,
2026-06-10,Wednesday,Talia D.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,11000,
2026-06-10,Wednesday,Jonah S.,Runner,11:00am,2:30pm,3.5,3.5,11000,
2026-06-10,Wednesday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,11000,
2026-06-10,Wednesday,Chloe B.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-10,Wednesday,Elena V.,Server,11:00am,5:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,James H.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-10,Wednesday,Marco D.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Marcus T.,Server,11:00am,5:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Maya R.,Server,5:00pm,11:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Rosa M.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Ruby F.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-11,Thursday,Kim T.,Bartender,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-11,Thursday,Rowan K.,Busser,11:00am,5:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Sam V.,Busser,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-11,Thursday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,13000,
2026-06-11,Thursday,James H.,Carry Out,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Jonah S.,Host,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-11,Thursday,Xavier R.,Host,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Carlos B.,Pantry Cook,10:30am,6:30pm,8.0,8.0,13000,
2026-06-11,Thursday,Simone R.,Pantry Cook,8:00am,4:00pm,8.0,8.0,13000,
2026-06-11,Thursday,Duncan L.,Prep Cook,8:00am,3:30pm,7.5,7.5,13000,
2026-06-11,Thursday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,13000,
2026-06-11,Thursday,Felix G.,Saute Cook,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Theo A.,Saute Cook,5:00pm,11:30pm,6.5,6.5,13000,
2026-06-11,Thursday,Ava S.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Gina F.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Grace T.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Marco D.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Owen D.,Server,4:00pm,9:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,13000,
2026-06-11,Thursday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-11,Thursday,Marcus T.,Shift Supervisor,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-12,Friday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Kim T.,Bartender,4:00pm,10:30pm,6.5,6.5,18000,
2026-06-12,Friday,Omar T.,Bartender,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Cody M.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-12,Friday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-12,Friday,Tony A.,Busser,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Jonah S.,Carry Out,4:00pm,10:00pm,6.0,6.0,18000,
2026-06-12,Friday,Lena S.,Carry Out,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Marisol T.,Carry Out,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Piper A.,Carry Out,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Rhea D.,Carry Out,4:00pm,10:00pm,6.0,6.0,18000,
2026-06-12,Friday,Dario V.,Host,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,James H.,Host,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Sienna P.,Host,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Bruno T.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Wei C.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Freya S.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Leo K.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,18000,
2026-06-12,Friday,Amy C.,Prep Cook,8:00am,3:30pm,7.5,7.5,18000,
2026-06-12,Friday,Farah A.,Prep Cook,8:00am,3:30pm,7.5,7.5,18000,
2026-06-12,Friday,Yara S.,Prep Cook,8:00am,3:30pm,7.5,7.5,18000,
2026-06-12,Friday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,18000,
2026-06-12,Friday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,18000,
2026-06-12,Friday,Caleb W.,Saute Cook,5:00pm,11:30pm,6.5,6.5,18000,
2026-06-12,Friday,Isla V.,Saute Cook,5:00pm,11:30pm,6.5,6.5,18000,
2026-06-12,Friday,Raj P.,Saute Cook,8:30am,4:30pm,8.0,8.0,18000,
2026-06-12,Friday,Ava S.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-12,Friday,Bella C.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Ethan M.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Gina F.,Server,4:00pm,9:30pm,5.5,5.5,18000,
2026-06-12,Friday,Grace T.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Kim T.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Marcus T.,Server,5:00pm,11:00pm,6.0,6.0,18000,
2026-06-12,Friday,Nina W.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Noah K.,Server,5:00pm,11:00pm,6.0,6.0,18000,
2026-06-12,Friday,Tony A.,Server,4:30pm,10:30pm,6.0,6.0,18000,
2026-06-13,Saturday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Kim T.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Rosa M.,Bartender,4:00pm,10:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Tomas H.,Bartender,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Delphine A.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Sam V.,Busser,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Marisol T.,Carry Out,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Piper A.,Carry Out,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Bella C.,Host,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,James H.,Host,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Tobias N.,Host,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Aisha K.,Pantry Cook,10:30am,6:30pm,8.0,8.0,20000,
2026-06-13,Saturday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Dante F.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Hector M.,Pantry Cook,8:00am,4:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Ivy R.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Jasper N.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Miles B.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Talia D.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,20000,
2026-06-13,Saturday,Amy C.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-13,Saturday,Camila G.,Prep Cook,8:00am,3:30pm,7.5,7.5,20000,
2026-06-13,Saturday,Duncan L.,Prep Cook,10:00am,5:30pm,7.5,7.5,20000,
2026-06-13,Saturday,Farah A.,Prep Cook,8:00am,3:30pm,7.5,7.5,20000,
2026-06-13,Saturday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,20000,
2026-06-13,Saturday,Sam V.,Runner,4:30pm,9:00pm,4.5,4.5,20000,
2026-06-13,Saturday,Isla V.,Saute Cook,3:00pm,11:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Layla N.,Saute Cook,5:00pm,11:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Ethan M.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Gina F.,Server,4:00pm,9:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Lena S.,Server,4:30pm,10:30pm,6.0,6.0,20000,
2026-06-13,Saturday,Liam P.,Server,4:00pm,9:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Marco D.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Marcus T.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Maya R.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Nina W.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Owen D.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Sienna P.,Server,11:00am,4:30pm,5.5,5.5,20000,
2026-06-13,Saturday,Sofia R.,Server,5:00pm,11:00pm,6.0,6.0,20000,
2026-06-14,Sunday,Derek M.,Bartender,4:30pm,11:00pm,6.5,6.5,12250,
2026-06-14,Sunday,Kim T.,Bartender,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Rosa M.,Bartender,4:30pm,11:00pm,6.5,6.5,12250,
2026-06-14,Sunday,Tomas H.,Bartender,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Tony A.,Busser,11:00am,5:30pm,6.5,6.5,12250,
2026-06-14,Sunday,James H.,Carry Out,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Xavier R.,Carry Out,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Lena S.,Host,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Owen K.,Host,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Bruno T.,Pantry Cook,8:00am,4:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Carlos B.,Pantry Cook,8:00am,4:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Dante F.,Pantry Cook,8:00am,4:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Hector M.,Pantry Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Simone R.,Pantry Cook,10:30am,6:30pm,8.0,8.0,12250,
2026-06-14,Sunday,Leo K.,Pizza Cook,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Miles B.,Pizza Cook,4:30pm,11:00pm,6.5,6.5,12250,
2026-06-14,Sunday,Amy C.,Prep Cook,10:00am,5:30pm,7.5,7.5,12250,
2026-06-14,Sunday,Kenji H.,Prep Cook,8:00am,3:30pm,7.5,7.5,12250,
2026-06-14,Sunday,Jonah S.,Runner,4:30pm,9:00pm,4.5,4.5,12250,
2026-06-14,Sunday,Piper A.,Runner,4:30pm,9:00pm,4.5,4.5,12250,
2026-06-14,Sunday,Felix G.,Saute Cook,8:30am,4:30pm,8.0,8.0,12250,
2026-06-14,Sunday,Isla V.,Saute Cook,8:30am,4:30pm,8.0,8.0,12250,
2026-06-14,Sunday,Layla N.,Saute Cook,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Nora J.,Saute Cook,3:00pm,11:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Raj P.,Saute Cook,5:00pm,11:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Derek M.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Diego L.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Elena V.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Gina F.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Nina W.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Noah K.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Ruby F.,Server,4:00pm,9:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Zoe H.,Server,4:30pm,10:30pm,6.0,6.0,12250,"""

    if not skip_shift_seed:
        # Use save_client_data so the correct DB path is always used
        save_client_data(2, "shifts", gia_mia_csv, source="seed")
        print("[auto-seed] Gia Mia shift data seeded successfully")
    print("[auto-seed] Gia Mia settings always applied")


# ── Restaurant CRUD ───────────────────────────────────────────────────────────

def create_restaurant(r: Restaurant, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO restaurants (name, owner_email, google_place_id, yelp_business_id,
            voice_notes, neighborhood, vibe, known_for, sign_off_name, never_say,
            hourly_rate, labor_target_pct, stripe_customer_id,
            location_group, location_name, pos_system, reviews_live, billing_status, is_demo,
            service_tier, module_reviews, module_labor, module_inventory, module_marketing,
            owner_name, owner_phone, digest_day, digest_enabled, created_at, timezone)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (r.name, r.owner_email, r.google_place_id, r.yelp_business_id,
          r.voice_notes, r.neighborhood, r.vibe, r.known_for,
          r.sign_off_name, r.never_say, r.hourly_rate, r.labor_target_pct,
          r.stripe_customer_id, r.location_group, r.location_name, r.pos_system, r.reviews_live, r.billing_status, r.is_demo,
          r.service_tier,
          r.module_reviews, r.module_labor, r.module_inventory,
          r.module_marketing, r.owner_name, r.owner_phone,
          r.digest_day, r.digest_enabled, r.created_at,
          r.timezone or "America/Chicago"))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_restaurant(restaurant_id: int, fields: dict, db_path: str = DB_PATH):
    """Update any restaurant fields by dict."""
    allowed = {
        "name","owner_email","google_place_id","yelp_business_id","voice_notes",
        "neighborhood","vibe","known_for","sign_off_name","never_say",
        "hourly_rate","labor_target_pct","week_start_day","role_strength_json","shift_leader_rules_json","quality_weights_json","monthly_revenue_target","hours_notes","role_rates_json","close_times_json","role_close_buffer_json","stripe_customer_id","docusign_envelope_id","contract_status","location_group","location_name","pos_system","inventory_frequency","delivery_days","inventory_notes","food_cost_target","inventory_updated_at","temp_password","ig_token","ig_user_id","fb_page_token","fb_page_id","ig_token_expires","fb_token_expires","competitor_intel","competitor_updated_at","reviews_live","billing_status","is_demo","internal_notes","gmb_access_token","gmb_refresh_token","gmb_account_id","gmb_location_id","gmb_token_expires",
        "service_tier","module_reviews","module_labor","module_inventory","module_marketing",
        "last_active_tab","last_activity","owner_name","owner_phone","digest_day","digest_enabled","menu_notes","menu_url","skip_holidays","custom_competitors",
        "two_fa_enabled","two_fa_code","two_fa_expires","two_fa_device_token","two_fa_pending","two_fa_method","login_notify","marketing_emails_opt_out","timezone","onboarding_dismissed",
        "alert_health_bypass_quiet","alert_food_waste","alert_ai_visibility_drop","alert_extra_emails","push_sound",
        "auto_approve_5star","auto_approve_daily_cap","auto_approve_paused","open_times_json",
        "response_language","tone_preset","data_retention_months",
        "toast_client_id","toast_client_secret","toast_restaurant_guid",
        "toast_access_token","toast_token_expires","toast_last_synced","toast_sync_error",
        "square_access_token","square_location_id","square_last_synced","square_sync_error",
        "clover_merchant_id","clover_api_token","clover_last_synced","clover_sync_error",
        "gbp_rating","gbp_review_count","gbp_rating_updated_at",
        "alert_1star","alert_2star","alert_health","alert_neg_spike","alert_negative_trend","alert_no_response",
        "alert_5star","alert_rating_threshold","alert_rating_floor","alert_labor_over",
        "alert_any_review","alert_resp_approved",
        "urgent_via_email","urgent_via_sms",
        "al_health_email","al_health_sms","al_health_push",
        "al_1star_email","al_1star_sms","al_1star_push",
        "al_2star_email","al_2star_sms","al_2star_push",
        "al_5star_email","al_5star_sms","al_5star_push",
        "al_spike_email","al_spike_sms","al_spike_push",
        "al_unres_email","al_unres_sms","al_unres_push",
        "changelog_seen_at","notifications_seen_at",
        "alert_quiet_start","alert_quiet_end","alert_max_per_day",
        "brand_name","brand_color","brand_logo_url",
        "section_count","daypart_split","delivery_pct","role_minimums_json","sched_notes","email_theme",
        "latitude","longitude","weather_cache_json","weather_cached_at",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [restaurant_id]
    conn = get_conn(db_path)
    conn.execute(f"UPDATE restaurants SET {set_clause} WHERE id=?", values)
    conn.commit()
    conn.close()


def get_deletion_requested_at(restaurant_id: int, db_path: str = DB_PATH):
    """Raw column read, not the Restaurant dataclass — deletion_requested_at
    isn't one of its declared fields, so get_restaurant() would never
    surface it and a getattr() against the dataclass would silently always
    return the default instead of the real value."""
    conn = get_conn(db_path)
    row = conn.execute("SELECT deletion_requested_at FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    conn.close()
    return row["deletion_requested_at"] if row else None


def request_account_deletion(restaurant_id: int, db_path: str = DB_PATH) -> str:
    """Records that the owner asked to close their account. Idempotent — a
    second request returns the original timestamp rather than resetting the
    clock, so re-opening the sheet and tapping the button again doesn't look
    like it reset a 30-day notice period that already started."""
    conn = get_conn(db_path)
    existing = conn.execute("SELECT deletion_requested_at FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    if existing and existing["deletion_requested_at"]:
        conn.close()
        return existing["deletion_requested_at"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE restaurants SET deletion_requested_at=? WHERE id=?", (now, restaurant_id))
    conn.commit()
    conn.close()
    return now


def get_restaurant(restaurant_id: int, db_path: str = DB_PATH) -> Optional[Restaurant]:
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return Restaurant(
        id=row["id"], name=row["name"], owner_email=row["owner_email"],
        google_place_id=row["google_place_id"], yelp_business_id=row["yelp_business_id"],
        voice_notes=row["voice_notes"], created_at=row["created_at"],
        neighborhood=row["neighborhood"] if "neighborhood" in row.keys() else None,
        vibe=row["vibe"] if "vibe" in row.keys() else None,
        known_for=row["known_for"] if "known_for" in row.keys() else None,
        sign_off_name=row["sign_off_name"] if "sign_off_name" in row.keys() else None,
        never_say=row["never_say"] if "never_say" in row.keys() else None,
        hourly_rate=row["hourly_rate"] if "hourly_rate" in row.keys() else 26.0,
        labor_target_pct=row["labor_target_pct"] if "labor_target_pct" in row.keys() else 30.0,
        week_start_day=int(row["week_start_day"] or 0) if "week_start_day" in row.keys() else 0,
        stripe_customer_id=row["stripe_customer_id"] if "stripe_customer_id" in row.keys() else None,
        docusign_envelope_id=row["docusign_envelope_id"] if "docusign_envelope_id" in row.keys() else None,
        contract_status=row["contract_status"] if "contract_status" in row.keys() else "pending",
        location_group=row["location_group"] if "location_group" in row.keys() else None,
        location_name=row["location_name"] if "location_name" in row.keys() else None,
        inventory_frequency=row["inventory_frequency"] if "inventory_frequency" in row.keys() else "weekly",
        delivery_days=row["delivery_days"] if "delivery_days" in row.keys() else None,
        inventory_notes=row["inventory_notes"] if "inventory_notes" in row.keys() else None,
        food_cost_target=row["food_cost_target"] if "food_cost_target" in row.keys() else 30.0,
        monthly_revenue_target=float(row["monthly_revenue_target"]) if "monthly_revenue_target" in row.keys() and row["monthly_revenue_target"] else 0.0,
        hours_notes=row["hours_notes"] if "hours_notes" in row.keys() else None,
        email_theme=row["email_theme"] if "email_theme" in row.keys() and row["email_theme"] else "dark",
        role_rates_json=row["role_rates_json"] if "role_rates_json" in row.keys() else None,
        close_times_json=row["close_times_json"] if "close_times_json" in row.keys() else None,
        role_close_buffer_json=row["role_close_buffer_json"] if "role_close_buffer_json" in row.keys() else None,
        inventory_updated_at=row["inventory_updated_at"] if "inventory_updated_at" in row.keys() else None,
        temp_password=row["temp_password"] if "temp_password" in row.keys() else None,
        ig_token=row["ig_token"] if "ig_token" in row.keys() else None,
        competitor_intel=row["competitor_intel"] if "competitor_intel" in row.keys() else None,
        competitor_updated_at=row["competitor_updated_at"] if "competitor_updated_at" in row.keys() else None,
        ig_user_id=row["ig_user_id"] if "ig_user_id" in row.keys() else None,
        ig_token_expires=row["ig_token_expires"] if "ig_token_expires" in row.keys() else None,
        fb_token_expires=row["fb_token_expires"] if "fb_token_expires" in row.keys() else None,
        fb_page_token=row["fb_page_token"] if "fb_page_token" in row.keys() else None,
        fb_page_id=row["fb_page_id"] if "fb_page_id" in row.keys() else None,
        pos_system=row["pos_system"] if "pos_system" in row.keys() else None,
        reviews_live=row["reviews_live"] if "reviews_live" in row.keys() else 0,
        billing_status=row["billing_status"] if "billing_status" in row.keys() else "trial",
        is_demo=row["is_demo"] if "is_demo" in row.keys() and row["is_demo"] is not None else 0,
        internal_notes=row["internal_notes"] if "internal_notes" in row.keys() else None,
        service_tier=row["service_tier"] if "service_tier" in row.keys() else "trial",
        module_reviews=row["module_reviews"] if "module_reviews" in row.keys() else 1,
        module_labor=row["module_labor"] if "module_labor" in row.keys() else 1,
        module_inventory=row["module_inventory"] if "module_inventory" in row.keys() else 1,
        module_marketing=row["module_marketing"] if "module_marketing" in row.keys() else 1,
        two_fa_enabled=row["two_fa_enabled"] if "two_fa_enabled" in row.keys() else 0,
        two_fa_code=row["two_fa_code"] if "two_fa_code" in row.keys() else None,
        two_fa_expires=row["two_fa_expires"] if "two_fa_expires" in row.keys() else None,
        two_fa_device_token=row["two_fa_device_token"] if "two_fa_device_token" in row.keys() else None,
        two_fa_pending=row["two_fa_pending"] if "two_fa_pending" in row.keys() else None,
        two_fa_method=(row["two_fa_method"] if "two_fa_method" in row.keys() and row["two_fa_method"] else "email"),
        timezone=(row["timezone"] if "timezone" in row.keys() and row["timezone"] else "America/Chicago"),
        onboarding_dismissed=(row["onboarding_dismissed"] if "onboarding_dismissed" in row.keys() else 0) or 0,
        last_active_tab=row["last_active_tab"] if "last_active_tab" in row.keys() else None,
        menu_notes=row["menu_notes"] if "menu_notes" in row.keys() else None,
        menu_url=row["menu_url"] if "menu_url" in row.keys() else None,
        skip_holidays=row["skip_holidays"] if "skip_holidays" in row.keys() else None,
        custom_competitors=row["custom_competitors"] if "custom_competitors" in row.keys() else None,
        login_notify=row["login_notify"] if "login_notify" in row.keys() else 0,
        marketing_emails_opt_out=row["marketing_emails_opt_out"] if "marketing_emails_opt_out" in row.keys() else 0,
        alert_health_bypass_quiet=row["alert_health_bypass_quiet"] if "alert_health_bypass_quiet" in row.keys() else 0,
        alert_food_waste=row["alert_food_waste"] if "alert_food_waste" in row.keys() else 0,
        alert_ai_visibility_drop=row["alert_ai_visibility_drop"] if "alert_ai_visibility_drop" in row.keys() else 0,
        alert_extra_emails=row["alert_extra_emails"] if "alert_extra_emails" in row.keys() else None,
        push_sound=row["push_sound"] if "push_sound" in row.keys() and row["push_sound"] is not None else 1,
        auto_approve_5star=row["auto_approve_5star"] if "auto_approve_5star" in row.keys() else 0,
        auto_approve_daily_cap=row["auto_approve_daily_cap"] if "auto_approve_daily_cap" in row.keys() and row["auto_approve_daily_cap"] is not None else 5,
        auto_approve_paused=row["auto_approve_paused"] if "auto_approve_paused" in row.keys() else 0,
        open_times_json=row["open_times_json"] if "open_times_json" in row.keys() else None,
        response_language=row["response_language"] if "response_language" in row.keys() else None,
        tone_preset=row["tone_preset"] if "tone_preset" in row.keys() else None,
        data_retention_months=row["data_retention_months"] if "data_retention_months" in row.keys() and row["data_retention_months"] is not None else 0,
        alert_1star=row["alert_1star"] if "alert_1star" in row.keys() else 1,
        alert_2star=row["alert_2star"] if "alert_2star" in row.keys() else 0,
        alert_health=row["alert_health"] if "alert_health" in row.keys() else 1,
        alert_neg_spike=row["alert_neg_spike"] if "alert_neg_spike" in row.keys() else 1,
        alert_negative_trend=row["alert_negative_trend"] if "alert_negative_trend" in row.keys() else 1,
        alert_no_response=row["alert_no_response"] if "alert_no_response" in row.keys() else 0,
        alert_5star=row["alert_5star"] if "alert_5star" in row.keys() else 0,
        alert_rating_threshold=row["alert_rating_threshold"] if "alert_rating_threshold" in row.keys() else 0,
        alert_rating_floor=row["alert_rating_floor"] if "alert_rating_floor" in row.keys() else 4.0,
        alert_labor_over=row["alert_labor_over"] if "alert_labor_over" in row.keys() else 0,
        alert_any_review=row["alert_any_review"] if "alert_any_review" in row.keys() else 0,
        alert_resp_approved=row["alert_resp_approved"] if "alert_resp_approved" in row.keys() else 0,
        urgent_via_email=row["urgent_via_email"] if "urgent_via_email" in row.keys() else 1,
        urgent_via_sms=row["urgent_via_sms"] if "urgent_via_sms" in row.keys() else 0,
        al_health_email=row["al_health_email"] if "al_health_email" in row.keys() else 1,
        al_health_sms=row["al_health_sms"]   if "al_health_sms"   in row.keys() else 0,
        al_health_push=row["al_health_push"] if "al_health_push"  in row.keys() else 1,
        al_1star_email=row["al_1star_email"] if "al_1star_email" in row.keys() else 1,
        al_1star_sms=row["al_1star_sms"]     if "al_1star_sms"   in row.keys() else 0,
        al_1star_push=row["al_1star_push"]   if "al_1star_push"  in row.keys() else 1,
        al_2star_email=row["al_2star_email"] if "al_2star_email" in row.keys() else 1,
        al_2star_sms=row["al_2star_sms"]     if "al_2star_sms"   in row.keys() else 0,
        al_2star_push=row["al_2star_push"]   if "al_2star_push"  in row.keys() else 1,
        al_5star_email=row["al_5star_email"] if "al_5star_email" in row.keys() else 0,
        al_5star_sms=row["al_5star_sms"]     if "al_5star_sms"   in row.keys() else 0,
        al_5star_push=row["al_5star_push"]   if "al_5star_push"  in row.keys() else 1,
        al_spike_email=row["al_spike_email"] if "al_spike_email" in row.keys() else 1,
        al_spike_sms=row["al_spike_sms"]     if "al_spike_sms"   in row.keys() else 0,
        al_spike_push=row["al_spike_push"]   if "al_spike_push"  in row.keys() else 1,
        al_unres_email=row["al_unres_email"] if "al_unres_email" in row.keys() else 1,
        al_unres_sms=row["al_unres_sms"]     if "al_unres_sms"   in row.keys() else 0,
        al_unres_push=row["al_unres_push"]   if "al_unres_push"  in row.keys() else 1,
        gmb_access_token=row["gmb_access_token"] if "gmb_access_token" in row.keys() else None,
        gmb_refresh_token=row["gmb_refresh_token"] if "gmb_refresh_token" in row.keys() else None,
        gmb_account_id=row["gmb_account_id"] if "gmb_account_id" in row.keys() else None,
        gmb_location_id=row["gmb_location_id"] if "gmb_location_id" in row.keys() else None,
        gmb_token_expires=row["gmb_token_expires"] if "gmb_token_expires" in row.keys() else None,
        toast_client_id=row["toast_client_id"] if "toast_client_id" in row.keys() else None,
        toast_client_secret=row["toast_client_secret"] if "toast_client_secret" in row.keys() else None,
        toast_restaurant_guid=row["toast_restaurant_guid"] if "toast_restaurant_guid" in row.keys() else None,
        toast_access_token=row["toast_access_token"] if "toast_access_token" in row.keys() else None,
        toast_token_expires=row["toast_token_expires"] if "toast_token_expires" in row.keys() else None,
        toast_last_synced=row["toast_last_synced"] if "toast_last_synced" in row.keys() else None,
        toast_sync_error=row["toast_sync_error"] if "toast_sync_error" in row.keys() else None,
        square_access_token=row["square_access_token"] if "square_access_token" in row.keys() else None,
        square_location_id=row["square_location_id"] if "square_location_id" in row.keys() else None,
        square_last_synced=row["square_last_synced"] if "square_last_synced" in row.keys() else None,
        square_sync_error=row["square_sync_error"] if "square_sync_error" in row.keys() else None,
        clover_merchant_id=row["clover_merchant_id"] if "clover_merchant_id" in row.keys() else None,
        clover_api_token=row["clover_api_token"] if "clover_api_token" in row.keys() else None,
        clover_last_synced=row["clover_last_synced"] if "clover_last_synced" in row.keys() else None,
        clover_sync_error=row["clover_sync_error"] if "clover_sync_error" in row.keys() else None,
        last_activity=row["last_activity"] if "last_activity" in row.keys() else None,
        gbp_rating=row["gbp_rating"] if "gbp_rating" in row.keys() else None,
        gbp_review_count=row["gbp_review_count"] if "gbp_review_count" in row.keys() else None,
        owner_name=row["owner_name"] if "owner_name" in row.keys() else None,
        owner_phone=row["owner_phone"] if "owner_phone" in row.keys() else None,
        digest_day=row["digest_day"] if "digest_day" in row.keys() else "monday",
        digest_enabled=row["digest_enabled"] if "digest_enabled" in row.keys() else 1,
        last_fetched_at=row["last_fetched_at"] if "last_fetched_at" in row.keys() else None,
        changelog_seen_at=row["changelog_seen_at"] if "changelog_seen_at" in row.keys() else None,
        notifications_seen_at=row["notifications_seen_at"] if "notifications_seen_at" in row.keys() else None,
        alert_quiet_start=row["alert_quiet_start"] if "alert_quiet_start" in row.keys() else None,
        alert_quiet_end=row["alert_quiet_end"]     if "alert_quiet_end"   in row.keys() else None,
        alert_max_per_day=row["alert_max_per_day"] if "alert_max_per_day" in row.keys() else 0,
        brand_name=row["brand_name"]         if "brand_name"     in row.keys() else None,
        brand_color=row["brand_color"]       if "brand_color"    in row.keys() else None,
        brand_logo_url=row["brand_logo_url"] if "brand_logo_url" in row.keys() else None,
        # These 5 were in the schema, ensure_columns, and update_restaurant's
        # allowed set, but never actually read back here — settings saved via
        # the admin scheduling form (section count, daypart split, sched
        # notes, etc.) silently had zero effect on generated schedules.
        section_count=row["section_count"]           if "section_count" in row.keys() else None,
        daypart_split=row["daypart_split"]            if "daypart_split" in row.keys() else None,
        delivery_pct=row["delivery_pct"]              if "delivery_pct" in row.keys() else None,
        role_minimums_json=row["role_minimums_json"]  if "role_minimums_json" in row.keys() else None,
        role_strength_json=row["role_strength_json"] if "role_strength_json" in row.keys() else None,
        shift_leader_rules_json=row["shift_leader_rules_json"] if "shift_leader_rules_json" in row.keys() else None,
        quality_weights_json=row["quality_weights_json"] if "quality_weights_json" in row.keys() else None,
        sched_notes=row["sched_notes"]                if "sched_notes" in row.keys() else None,
        latitude=row["latitude"]                       if "latitude" in row.keys() else None,
        longitude=row["longitude"]                     if "longitude" in row.keys() else None,
        weather_cache_json=row["weather_cache_json"]   if "weather_cache_json" in row.keys() else None,
        weather_cached_at=row["weather_cached_at"]     if "weather_cached_at" in row.keys() else None,
    )


def is_full_tier(restaurant: Optional["Restaurant"]) -> bool:
    """True if all 4 modules are on — the gate for Intel/competitor-analysis
    access. This exact 4-way AND used to be copy-pasted independently in
    scheduler.py (weekly competitor job), hosted_dashboard.py (whether to
    load competitor_data), and dashboard.html (whether to show the Intel
    tab) — a real drift risk if the tier rule ever changes (e.g. Intel
    becomes its own paid add-on rather than a full-tier bonus)."""
    return bool(restaurant and restaurant.module_reviews and restaurant.module_labor
                and restaurant.module_inventory and restaurant.module_marketing)


# The one place "what modules exist and what's this restaurant entitled to"
# is defined. Before this, hosted_dashboard.py's index(), mobile_api.py's
# _do_mobile_home(), and dashboard.html's Jinja each independently re-derived
# the same booleans by hand — a client-count-scaling problem as much as a
# DRY one, since every new module meant hunting down and updating three
# separate places. `column` entries that don't exist yet on `restaurants`
# (waitlist/bar — sold on the pricing page, not built) are simply treated as
# off, the same tolerant `"col" in row.keys()` pattern used everywhere else
# in this file — so these entries can sit in the registry, inert, until a
# real column and feature ship, with zero risk to restaurants that predate it.
_MODULE_REGISTRY = [
    {"key": "reviews",   "label": "Reviews",     "column": "module_reviews"},
    {"key": "labor",     "label": "Labor",       "column": "module_labor"},
    {"key": "inventory", "label": "Food Cost",   "column": "module_inventory"},
    {"key": "marketing", "label": "Marketing",   "column": "module_marketing"},
    # No DB column — derived, exactly per is_full_tier()'s docstring above.
    {"key": "intel",     "label": "Intel",       "derived": lambda r: bool(getattr(r, "google_place_id", None)) and is_full_tier(r)},
    # Sold on pricing.html, not implemented anywhere yet — reserved so the
    # mobile "coming soon" placeholder mechanism has somewhere to point once
    # a module_waitlist/module_bar column and real feature exist.
    {"key": "waitlist",  "label": "Waitlist",      "column": "module_waitlist",  "status": "coming_soon"},
    {"key": "bar",       "label": "Bar & Alcohol", "column": "module_bar",       "status": "coming_soon"},
]


def restaurant_has_module(restaurant_id: int, key: str, db_path: str = DB_PATH) -> bool:
    """Does this restaurant have the module `key`?

    Reads the same _MODULE_REGISTRY get_active_modules displays from, so the
    answer the UI shows and the answer a route enforces can never drift —
    including "intel", which has no column of its own and is derived from
    full tier plus a connected listing.

    Fails OPEN on a lookup error, for the same reason
    subscription_allows_access does: a database hiccup must not take a
    paying client's Labor tab away.
    """
    try:
        restaurant = get_restaurant(restaurant_id, db_path=db_path)
        if not restaurant:
            return True
        if key == "intel":
            # Entitlement, not readiness. get_active_modules also requires a
            # connected Google listing before it will *show* the Intel tab,
            # but a full-tier client who hasn't linked Google yet has bought
            # Intel — refusing them here would tell them it isn't part of
            # their plan, which is false. The routes themselves say "connect
            # your listing", which is the true answer.
            return is_full_tier(restaurant)
        return any(m["key"] == key for m in get_active_modules(restaurant))
    except Exception:
        return True


def module_label(key: str) -> str:
    for entry in _MODULE_REGISTRY:
        if entry["key"] == key:
            return entry["label"]
    return key.title()


def get_active_modules(restaurant: Optional["Restaurant"]) -> list[dict]:
    """Returns only the modules this restaurant actually has, in registry
    order, each as {"key", "label", "status"} — status is "available" unless
    the registry entry says otherwise. Callers (web index(), mobile_api's
    home endpoint) should use this instead of checking restaurant.module_*
    booleans by hand."""
    if not restaurant:
        return []
    active = []
    for entry in _MODULE_REGISTRY:
        if "derived" in entry:
            is_on = entry["derived"](restaurant)
        else:
            is_on = bool(getattr(restaurant, entry["column"], 0))
        if is_on:
            active.append({
                "key": entry["key"],
                "label": entry["label"],
                "status": entry.get("status", "available"),
            })
    return active


# ── Review CRUD ───────────────────────────────────────────────────────────────

def _apply_review_edit(conn, r: "Review") -> bool:
    """Update a stored review when its author has edited it. True if changed.

    Compares the guest's own fields only. Everything the restaurant did —
    the draft, the approval, the posted reply — is left alone, because an
    edit to the review is not a reason to throw away a reply already
    published against it.
    """
    row = conn.execute(
        "SELECT id, rating, text, original_rating FROM reviews "
        "WHERE restaurant_id=? AND platform=? AND external_id=?",
        (r.restaurant_id, r.platform, r.external_id)).fetchone()
    if not row:
        return False
    same_rating = int(row["rating"] or 0) == int(r.rating or 0)
    same_text = (row["text"] or "").strip() == (r.text or "").strip()
    if same_rating and same_text:
        return False
    # Keep what the guest first said, so a rating that moved can be shown as
    # having moved rather than quietly replaced.
    original = row["original_rating"] if row["original_rating"] is not None else row["rating"]
    conn.execute(
        "UPDATE reviews SET rating=?, text=?, original_rating=?, "
        "source_updated_at=?, edited_at=datetime('now'), "
        "processed=0, sentiment=NULL, categories=NULL, summary=NULL, urgency='normal' "
        "WHERE id=?",
        (r.rating, r.text, original, r.source_updated_at, row["id"]))
    r.id = row["id"]
    return True


def save_reviews(reviews: list[Review], db_path: str = DB_PATH) -> tuple[int, list]:
    """Upsert reviews; skip ones this restaurant already has.

    Returns (new_count, new_review_objects).

    The skip used to be a bare `pass` under a GLOBAL UNIQUE(platform,
    external_id), which meant "another restaurant already claimed this
    review" was indistinguishable from "we already have it" — and the first
    case is a data-isolation failure that ran for months without a single
    log line. The key is per-restaurant now, so a collision here really does
    mean a duplicate; anything else is reported rather than swallowed.
    """
    conn = get_conn(db_path)
    new_count = 0
    new_reviews = []
    already_had = 0
    edited = 0
    unexpected = []
    for r in reviews:
        try:
            cur = conn.execute("""
                INSERT INTO reviews
                    (restaurant_id, platform, external_id, author, rating,
                     text, review_date, fetched_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (r.restaurant_id, r.platform, r.external_id, r.author,
                  r.rating, r.text, r.review_date, r.fetched_at))
            # The row id, carried back onto the object. Without it every
            # caller downstream saw review.id as None: alert_log rows were
            # written with a null review_id, so an alert could not be traced
            # to the review that caused it, and nothing could re-read the
            # batch after analysis.
            r.id = cur.lastrowid
            new_count += 1
            new_reviews.append(r)
        except sqlite3.IntegrityError as e:
            # UNIQUE(restaurant_id, platform, external_id) — this restaurant
            # already has it, which is the normal re-fetch case.
            if "UNIQUE" in str(e).upper():
                already_had += 1
                # A guest can edit their own review. This was insert-only, so
                # a one-star the guest later raised to five stayed a one-star
                # here forever: in the average, in the sentiment split, in the
                # owner's reply queue, and in every chart. Google returns the
                # edit under the same review name, so the change is visible on
                # the very next fetch and was simply being discarded.
                #
                # Only the guest's own content is updated. response_status,
                # draft_response and the approval timestamps are the
                # restaurant's work and are never touched. The text changed,
                # so the row goes back for re-analysis.
                try:
                    if _apply_review_edit(conn, r):
                        edited += 1
                except Exception as _ee:
                    unexpected.append((r.external_id, f"edit failed: {_ee}"))
            else:
                unexpected.append((r.external_id, str(e)))
    conn.commit()
    conn.close()
    if edited:
        print(f"[reviews] {edited} review(s) were edited by their author and have been updated")
    if unexpected:
        print(f"[reviews] {len(unexpected)} row(s) rejected for a reason other than a duplicate: "
              f"{unexpected[:3]}")
        try:
            import ops
            ops.capture(RuntimeError(f"{len(unexpected)} reviews rejected: {unexpected[:3]}"),
                        job="save_reviews",
                        context=f"restaurant_id={reviews[0].restaurant_id if reviews else '?'}")
        except Exception:
            pass
    return new_count, new_reviews


def get_pending_analysis(restaurant_id: int, limit: int = 50,
                          db_path: str = DB_PATH) -> list[Review]:
    """Reviews fetched but not yet analysed by Claude."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT * FROM reviews
        WHERE restaurant_id=? AND processed=0
        ORDER BY fetched_at DESC LIMIT ?
    """, (restaurant_id, limit)).fetchall()
    conn.close()
    return [_row_to_review(r) for r in rows]


def get_pending_drafts(restaurant_id: int, limit: int = 50,
                        db_path: str = DB_PATH) -> list[Review]:
    """Analysed reviews that still need a response drafted."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT * FROM reviews
        WHERE restaurant_id=? AND processed=1 AND response_status='pending'
        ORDER BY
            CASE urgency WHEN 'high' THEN 0 ELSE 1 END,
            fetched_at DESC
        LIMIT ?
    """, (restaurant_id, limit)).fetchall()
    conn.close()
    return [_row_to_review(r) for r in rows]


def get_urgent_reviews(restaurant_id: int, db_path: str = DB_PATH) -> list[Review]:
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT * FROM reviews
        WHERE restaurant_id=? AND urgency='high' AND response_status NOT IN ('posted','skipped')
        ORDER BY fetched_at DESC
    """, (restaurant_id,)).fetchall()
    conn.close()
    return [_row_to_review(r) for r in rows]


def get_reviews_since(restaurant_id: int, since: str,
                       db_path: str = DB_PATH) -> list[Review]:
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT * FROM reviews
        WHERE restaurant_id=? AND fetched_at >= ? AND processed=1
        ORDER BY review_date DESC
    """, (restaurant_id, since)).fetchall()
    conn.close()
    return [_row_to_review(r) for r in rows]


def update_analysis(review_id: int, sentiment: str, categories: list,
                     summary: str, urgency: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE reviews
        SET sentiment=?, categories=?, summary=?, urgency=?, processed=1
        WHERE id=?
    """, (sentiment, json.dumps(categories), summary, urgency, review_id))
    conn.commit()
    conn.close()


def update_draft(review_id: int, draft: str, db_path: str = DB_PATH,
                 needs_review: bool = False, review_reason: str = None):
    """Store a drafted reply.

    needs_review marks a draft that passed generation but states something
    the system cannot stand behind — see ai_guard.unsupported_commitments.
    It never blocks the owner from posting; it makes the reason visible
    before they do, and the auto-approve rule refuses to touch it.
    """
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE reviews
           SET draft_response=?, response_status='drafted',
               draft_needs_review=?, draft_review_reason=?
         WHERE id=?
    """, (draft, 1 if needs_review else 0, review_reason, review_id))
    conn.commit()
    conn.close()


def approve_response(review_id: int, restaurant_id: int = None, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    if restaurant_id is not None:
        conn.execute("""
            UPDATE reviews
            SET response_status='approved', approved_at=datetime('now')
            WHERE id=? AND restaurant_id=?
        """, (review_id, restaurant_id))
    else:
        conn.execute("""
            UPDATE reviews
            SET response_status='approved', approved_at=datetime('now')
            WHERE id=?
        """, (review_id,))
    conn.commit()
    conn.close()


def mark_posted(review_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE reviews
        SET response_status='posted', posted_at=datetime('now')
        WHERE id=?
    """, (review_id,))
    conn.commit()
    conn.close()


def revert_to_drafted(review_id: int, restaurant_id: int, db_path: str = DB_PATH):
    """Puts a review back in the actionable 'drafted' queue — undoes a skip,
    undoes a not-yet-posted approval, or completes a retract (once the live
    Google reply has actually been deleted). Callers are responsible for
    checking the review's current status is appropriate before calling this;
    it doesn't gate on status itself so the same helper serves all three."""
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE reviews
        SET response_status='drafted', approved_at=NULL, posted_at=NULL
        WHERE id=? AND restaurant_id=?
    """, (review_id, restaurant_id))
    conn.commit()
    conn.close()


# ── Reporting ─────────────────────────────────────────────────────────────────

def save_weekly_report(report: WeeklyReport, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO weekly_reports
            (restaurant_id, period_start, period_end, total_reviews,
             avg_rating, sentiment_json, top_issues_json)
        VALUES (?,?,?,?,?,?,?)
    """, (report.restaurant_id, report.period_start, report.period_end,
          report.total_reviews, report.avg_rating,
          json.dumps(report.sentiment), json.dumps(report.top_issues)))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


# ── Internal helpers ──────────────────────────────────────────────────────────

def _row_to_review(row: sqlite3.Row) -> Review:
    return Review(
        id=row["id"],
        restaurant_id=row["restaurant_id"],
        platform=row["platform"],
        external_id=row["external_id"],
        author=row["author"] or "Anonymous",
        rating=row["rating"],
        text=row["text"],
        review_date=row["review_date"],
        fetched_at=row["fetched_at"],
        sentiment=row["sentiment"],
        categories=json.loads(row["categories"]) if row["categories"] else None,
        summary=row["summary"],
        urgency=row["urgency"] or "normal",
        draft_response=row["draft_response"],
        response_status=row["response_status"],
        approved_at=row["approved_at"],
        posted_at=row["posted_at"],
        processed=bool(row["processed"]),
    )


# ── CLI helper ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    # Seed a demo restaurant
    demo = Restaurant(
        name="Maplewood Kitchen",
        owner_email="owner@maplewoodkitchen.com",
        google_place_id="ChIJdemo123",
        voice_notes="Casual, warm tone. We always invite guests back. Never overly formal.",
    )
    rid = create_restaurant(demo)
    print(f"Created restaurant id={rid}: {demo.name}")

    # Verify schema
    conn = get_conn()
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print("Tables:", [t["name"] for t in tables])
    conn.close()

# ── Staff notes table ────────────────────────────────────────────────────────

STAFF_NOTES_SCHEMA = """
CREATE TABLE IF NOT EXISTS staff_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    employee_name   TEXT    NOT NULL,
    notes           TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(restaurant_id, employee_name)
);

CREATE TABLE IF NOT EXISTS staff_availability (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    employee_name   TEXT    NOT NULL,
    available_days  TEXT    NOT NULL,  -- JSON list e.g. ["Friday","Saturday","Sunday"]
    unavailable_days TEXT,             -- JSON list of days they cannot work
    notes           TEXT,              -- e.g. "student — no weekday mornings before noon"
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(restaurant_id, employee_name)
);
"""

def init_two_fa_backup_codes(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS two_fa_backup_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        code_hash TEXT NOT NULL,
        used_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
    conn.close()

def generate_backup_codes(restaurant_id: int, count: int = 10, db_path: str = DB_PATH) -> list:
    """Invalidates any previously-issued codes and mints a fresh set —
    shown to the user exactly once (here, at generation time) since only
    the hash is ever persisted, matching how password_hash never stores
    the plaintext either."""
    from werkzeug.security import generate_password_hash as _gph
    import secrets as _secrets
    codes = [f"{_secrets.token_hex(4).upper()[:4]}-{_secrets.token_hex(4).upper()[4:]}" for _ in range(count)]
    conn = get_conn(db_path)
    conn.execute("DELETE FROM two_fa_backup_codes WHERE restaurant_id=?", (restaurant_id,))
    for code in codes:
        conn.execute(
            "INSERT INTO two_fa_backup_codes (restaurant_id, code_hash) VALUES (?, ?)",
            (restaurant_id, _gph(code))
        )
    conn.commit()
    conn.close()
    return codes

def verify_and_consume_backup_code(restaurant_id: int, code: str, db_path: str = DB_PATH) -> bool:
    from werkzeug.security import check_password_hash as _cph
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT id, code_hash FROM two_fa_backup_codes WHERE restaurant_id=? AND used_at IS NULL",
        (restaurant_id,)
    ).fetchall()
    for row in rows:
        if _cph(row["code_hash"], code.strip()):
            conn.execute("UPDATE two_fa_backup_codes SET used_at=datetime('now') WHERE id=?", (row["id"],))
            conn.commit()
            conn.close()
            return True
    conn.close()
    return False

def count_unused_backup_codes(restaurant_id: int, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM two_fa_backup_codes WHERE restaurant_id=? AND used_at IS NULL",
        (restaurant_id,)
    ).fetchone()["n"]
    conn.close()
    return n

def init_email_log(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS email_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER,
        email_type TEXT,
        to_email TEXT,
        subject TEXT,
        sent_at TEXT DEFAULT (datetime('now')),
        status TEXT DEFAULT 'sent',
        error TEXT,
        message_id TEXT
    )""")
    conn.commit()
    conn.close()

def log_email(restaurant_id, email_type, to_email, subject, db_path: str = DB_PATH,
              status: str = "sent", error: str = None, message_id: str = None):
    from datetime import datetime, timezone, timedelta
    # Convert UTC to US/Chicago time
    try:
        import zoneinfo
        chicago = zoneinfo.ZoneInfo("America/Chicago")
        local_now = datetime.now(timezone.utc).astimezone(chicago).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # Fallback: manual UTC-5 offset
        local_now = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status, error, message_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (restaurant_id, email_type, to_email, subject, local_now, status, error, message_id)
    )
    conn.commit()
    conn.close()

def get_email_log(restaurant_id=None, limit=100, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    if restaurant_id:
        rows = conn.execute(
            """SELECT e.*, r.name as restaurant_name FROM email_log e
               LEFT JOIN restaurants r ON r.id = e.restaurant_id
               WHERE e.restaurant_id=? ORDER BY e.sent_at DESC LIMIT ?""",
            (restaurant_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.*, r.name as restaurant_name FROM email_log e
               LEFT JOIN restaurants r ON r.id = e.restaurant_id
               ORDER BY e.sent_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_email_log_for_client(restaurant_id: int, limit: int = 50, db_path: str = DB_PATH) -> list:
    """One restaurant's own email history, labelled for display.

    get_email_log() already supported a restaurant filter but nothing
    client-facing ever called it — an owner had no way to confirm whether a
    staff schedule or supplier order actually went out, and the admin panel
    was the only place any of this was visible.
    """
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT email_type, to_email, subject, sent_at, status, error "
            "FROM email_log WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
            (restaurant_id, limit)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = email_type_label(d.get("email_type"))
        # Never surface a raw provider error to a restaurant owner; it's
        # noise to them and can echo internal detail. The status is the
        # actionable part.
        d["failed"] = (d.get("status") or "sent") not in ("sent", "delivered")
        d.pop("error", None)
        out.append(d)
    return out

def init_staff_notes(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.executescript(STAFF_NOTES_SCHEMA)
    conn.commit()
    conn.close()

def save_staff_note(restaurant_id: int, employee_name: str,
                    notes: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""
        INSERT INTO staff_notes (restaurant_id, employee_name, notes)
        VALUES (?,?,?)
        ON CONFLICT(restaurant_id, employee_name)
        DO UPDATE SET notes=excluded.notes
    """, (restaurant_id, employee_name.strip(), notes.strip()))
    conn.commit()
    conn.close()

def get_staff_notes(restaurant_id: int,
                    db_path: str = DB_PATH) -> list[dict]:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM staff_notes WHERE restaurant_id=? ORDER BY employee_name",
        (restaurant_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_staff_note(note_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM staff_notes WHERE id=?", (note_id,))
    conn.commit()
    conn.close()

# ── Staff availability ────────────────────────────────────────────────────────

def init_staff_availability(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS staff_availability (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
        employee_name   TEXT    NOT NULL,
        available_days  TEXT    NOT NULL DEFAULT '[]',
        unavailable_days TEXT,
        notes           TEXT,
        updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, employee_name)
    )""")
    conn.commit()
    conn.close()

# ── Employee capability layer ─────────────────────────────────────────────
#
# The thing an owner asked for was "does the AI know how good each employee
# is" — but building that as a rating column would make every later
# capability (closing ability, trainer, cocktail expertise, reliability) a
# new migration and a new set of call sites.
#
# So: one row per employee per ATTRIBUTE. Version 1 registers exactly one,
# `overall`, and exposes only that. Adding a second is a registry entry and
# nothing else — no schema change, no backfill, and each attribute carries
# its own provenance so "who set this and when" is answerable per skill
# rather than per employee.
#
# Employees are keyed by NAME, matching staff_notes, staff_availability and
# staff_contacts. They come from POS shift data rather than a roster this
# app owns, so a name is the only identity available.

SCORE_MIN, SCORE_MAX = 1, 5

# Each entry declares what an attribute IS, so validation, the UI and the
# scheduler can all read the same definition instead of three copies.
#   kind    "score" (numeric, SCORE_MIN..SCORE_MAX) or "flag" (boolean)
#   v1      whether Version 1 surfaces it
CAPABILITY_ATTRIBUTES = {
    "overall": {
        "label": "Operational Score",
        "kind": "score",
        "v1": True,
        "help": "1 very weak · 2 below average · 3 average · 4 strong · 5 excellent",
    },
    # Registered so shift-leader rules can reference it and validation knows
    # its shape. Not surfaced in Version 1's UI.
    "can_close": {"label": "Authorised to close", "kind": "flag", "v1": False,
                  "help": "May be listed as the closer on a shift"},
}

SCORE_LABELS = {1: "Very weak", 2: "Below average", 3: "Average",
                4: "Strong", 5: "Excellent"}


def init_staff_capabilities(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS staff_capabilities (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        employee_name  TEXT    NOT NULL,
        attribute      TEXT    NOT NULL,
        score          REAL,
        flag           INTEGER,
        notes          TEXT,
        updated_by     TEXT,
        updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, employee_name, attribute)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_cap_rest "
                 "ON staff_capabilities(restaurant_id, attribute)")
    conn.commit()
    conn.close()


class CapabilityError(ValueError):
    """A capability write that would store something meaningless."""


def set_capability(restaurant_id: int, employee_name: str, attribute: str = "overall",
                   score=None, flag=None, notes: str = None, updated_by: str = None,
                   db_path: str = DB_PATH) -> dict:
    """Set one attribute for one employee. Raises CapabilityError on junk.

    Passing score=None for a score attribute CLEARS the rating rather than
    storing a zero — "not rated yet" and "rated 1" are different facts and
    the scheduler treats them differently.
    """
    spec = CAPABILITY_ATTRIBUTES.get(attribute)
    if not spec:
        raise CapabilityError(f"{attribute!r} is not a capability this system knows about")
    name = (employee_name or "").strip()
    if not name:
        raise CapabilityError("an employee name is required")

    if spec["kind"] == "score":
        if score is None:
            _clear_capability(restaurant_id, name, attribute, db_path)
            return {"employee_name": name, "attribute": attribute, "score": None}
        try:
            score = int(round(float(score)))
        except (TypeError, ValueError):
            raise CapabilityError(f"{score!r} is not a number")
        if not SCORE_MIN <= score <= SCORE_MAX:
            raise CapabilityError(f"score must be {SCORE_MIN}-{SCORE_MAX}, got {score}")
        flag = None
    else:
        if flag is None:
            _clear_capability(restaurant_id, name, attribute, db_path)
            return {"employee_name": name, "attribute": attribute, "flag": None}
        flag = 1 if flag else 0
        score = None

    # A caller who said nothing about notes keeps the notes already on file.
    # The rating control sends a score on every tap and never carries the
    # note with it, so writing excluded.notes unconditionally would erase an
    # owner's note the next time they nudged that person's score. Passing an
    # empty string is how you clear one deliberately.
    notes_given = notes is not None
    clean_notes = (notes or "").strip()[:500] or None

    init_staff_capabilities(db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO staff_capabilities
                (restaurant_id, employee_name, attribute, score, flag, notes, updated_by, updated_at)
            VALUES (?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(restaurant_id, employee_name, attribute) DO UPDATE SET
                score=excluded.score, flag=excluded.flag,
                notes=CASE WHEN ? THEN excluded.notes ELSE staff_capabilities.notes END,
                updated_by=excluded.updated_by, updated_at=datetime('now')
        """, (restaurant_id, name, attribute, score, flag,
              clean_notes, (updated_by or "").strip()[:120] or None,
              1 if notes_given else 0))
        conn.commit()
    finally:
        conn.close()
    return {"employee_name": name, "attribute": attribute, "score": score, "flag": flag}


def _clear_capability(restaurant_id, employee_name, attribute, db_path):
    """Un-rate somebody without throwing away what was written about them.

    A note explaining WHY a rating was where it was outlives the rating
    itself, so a row carrying one is blanked rather than deleted; a row
    carrying nothing else goes.
    """
    init_staff_capabilities(db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE staff_capabilities SET score=NULL, flag=NULL, "
                     "updated_at=datetime('now') WHERE restaurant_id=? "
                     "AND employee_name=? AND attribute=? "
                     "AND COALESCE(TRIM(notes),'') <> ''",
                     (restaurant_id, employee_name, attribute))
        conn.execute("DELETE FROM staff_capabilities WHERE restaurant_id=? "
                     "AND employee_name=? AND attribute=? "
                     "AND COALESCE(TRIM(notes),'') = ''",
                     (restaurant_id, employee_name, attribute))
        conn.commit()
    finally:
        conn.close()


def get_capabilities(restaurant_id: int, attribute: str = None,
                     db_path: str = DB_PATH) -> dict:
    """{employee_name: {attribute: {...}}} for one restaurant."""
    init_staff_capabilities(db_path)
    conn = get_conn(db_path)
    sql = ("SELECT employee_name, attribute, score, flag, notes, updated_by, updated_at "
           "FROM staff_capabilities WHERE restaurant_id=?")
    args = [restaurant_id]
    if attribute:
        sql += " AND attribute=?"
        args.append(attribute)
    rows = conn.execute(sql, tuple(args)).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["employee_name"], {})[r["attribute"]] = {
            "score": int(r["score"]) if r["score"] is not None else None,
            "flag": bool(r["flag"]) if r["flag"] is not None else None,
            "notes": r["notes"],
            "updated_by": r["updated_by"],
            "updated_at": r["updated_at"],
        }
    return out


def capability_version(restaurant_id: int, db_path: str = DB_PATH) -> str:
    """A stamp that moves whenever anything the engine scores against moves.

    Clients cache a whole generated schedule, quality panel included, and
    nothing connected a rating change to that cache — so an owner could
    rate three people, reopen the app, and read a score computed against
    the ratings they had just replaced, with nothing marking it stale.
    """
    init_staff_capabilities(db_path)
    conn = get_conn(db_path)
    try:
        caps = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(updated_at),'') FROM staff_capabilities "
            "WHERE restaurant_id=?", (restaurant_id,)).fetchone()
        try:
            profiles = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at),'') FROM shift_profiles "
                "WHERE restaurant_id=?", (restaurant_id,)).fetchone()
        except Exception:
            profiles = (0, "")
        rest = conn.execute(
            "SELECT COALESCE(role_strength_json,'') || COALESCE(shift_leader_rules_json,'') "
            "|| COALESCE(quality_weights_json,'') FROM restaurants WHERE id=?",
            (restaurant_id,)).fetchone()
    finally:
        conn.close()
    import hashlib
    raw = f"{caps[0]}|{caps[1]}|{profiles[0]}|{profiles[1]}|{(rest[0] if rest else '')}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def get_operational_scores(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """{employee_name: 1-5} for everyone who has been rated. Absent means
    NOT RATED, which is deliberately different from a low rating."""
    caps = get_capabilities(restaurant_id, attribute="overall", db_path=db_path)
    return {n: c["overall"]["score"] for n, c in caps.items()
            if c.get("overall", {}).get("score") is not None}


def capability_coverage(restaurant_id: int, roster: list, db_path: str = DB_PATH) -> dict:
    """How much of this roster has been rated.

    The whole feature stays dormant at zero — no thresholds enforced, no
    warnings raised — so an existing restaurant schedules exactly as it did
    before anyone touches a rating.
    """
    scores = get_operational_scores(restaurant_id, db_path)
    names = [n for n in (roster or []) if n]
    rated = [n for n in names if n in scores]
    return {
        "rated": len(rated),
        "total": len(names),
        "unrated": sorted(n for n in names if n not in scores),
        "active": bool(rated),
        "pct": round(len(rated) / len(names) * 100) if names else 0,
    }


def get_role_strength_thresholds(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """{role: minimum combined score}. Empty when unconfigured."""
    import json as _j
    r = get_restaurant(restaurant_id, db_path)
    if not r or not getattr(r, "role_strength_json", None):
        return {}
    try:
        raw = _j.loads(r.role_strength_json)
        out = {}
        for role, v in (raw or {}).items():
            try:
                n = float(v)
            except (TypeError, ValueError):
                continue
            if n > 0:
                out[str(role)] = n
        return out
    except Exception:
        return {}


def validate_strength_thresholds(thresholds: dict, roster_scores: dict = None,
                                 roles_by_employee: dict = None) -> list:
    """Problems with a threshold set, as plain sentences. Empty when fine.

    A threshold no roster could ever reach is not a target, it is a warning
    the owner will see every week and learn to ignore.
    """
    problems = []
    for role, v in (thresholds or {}).items():
        try:
            n = float(v)
        except (TypeError, ValueError):
            problems.append(f"{role}: {v!r} is not a number")
            continue
        if n < 0:
            problems.append(f"{role}: a threshold cannot be negative")
        if roster_scores and roles_by_employee:
            # Case-insensitive, for the same reason the strength check
            # itself is: "Bartender" in the editor and "bartender" in the
            # CSV are one role, and matching exactly made this validation
            # quietly compare a target against an empty team.
            want = (role or "").strip().lower()
            best = sorted((s for e, s in roster_scores.items()
                           if (roles_by_employee.get(e) or "").strip().lower() == want),
                          reverse=True)
            if best and n > sum(best):
                problems.append(
                    f"{role}: {n:g} is higher than your whole {role.lower()} team combined "
                    f"({sum(best):g}), so it can never be met")
    return problems


# ── Shift profiles ─────────────────────────────────────────────────────────
#
# Not every shift is judged the same way. Monday lunch and Saturday dinner
# are different jobs, and one global threshold flattens them into one.
#
# The whole profile is stored as JSON rather than as columns, because the
# Shift Quality Engine's dimensions are meant to grow — reservations,
# certifications, prep volume — and each new one would otherwise be a
# migration. key/label/priority/active are lifted out as real columns
# because those are what the engine orders and filters on.

def init_shift_profiles(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shift_profiles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            key           TEXT    NOT NULL,
            label         TEXT    NOT NULL,
            config_json   TEXT    NOT NULL,
            priority      INTEGER NOT NULL DEFAULT 0,
            active        INTEGER NOT NULL DEFAULT 1,
            updated_by    TEXT,
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, key)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shift_profiles_rest "
                 "ON shift_profiles(restaurant_id, active)")
    conn.commit()
    conn.close()


def init_capability_changes(db_path: str = DB_PATH):
    """Who changed a target, when, and what it was before.

    staff_capabilities and shift_profiles carry updated_by for the CURRENT
    value only, and the per-role targets and dimension weights live in plain
    columns on restaurants with no provenance at all — so "who moved the
    bartender target and when", which is the first question after a disputed
    schedule, had no answer anywhere.
    """
    conn = get_conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS capability_changes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            kind          TEXT    NOT NULL,   -- rating | threshold | leader_rule | profile | weights
            subject       TEXT,               -- employee name, role, or profile key
            before_json   TEXT,
            after_json    TEXT,
            changed_by    TEXT,
            changed_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_capability_changes_rest "
                 "ON capability_changes(restaurant_id, changed_at)")
    conn.commit()
    conn.close()


def record_capability_change(restaurant_id: int, kind: str, subject: str = None,
                             before=None, after=None, changed_by: str = None,
                             db_path: str = DB_PATH):
    """Append one change. Never raises — an audit row failing to write must
    not stop an owner setting a target."""
    import json as _j
    try:
        init_capability_changes(db_path)
        conn = get_conn(db_path)
        conn.execute(
            "INSERT INTO capability_changes "
            "(restaurant_id, kind, subject, before_json, after_json, changed_by) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, str(kind)[:40], (str(subject)[:160] if subject else None),
             _j.dumps(before) if before is not None else None,
             _j.dumps(after) if after is not None else None,
             (changed_by or "").strip()[:120] or None))
        conn.commit()
        conn.close()
    except Exception as exc:
        # The write is genuinely optional — an audit row must never stop an
        # owner setting a target — but it is NOT allowed to fail invisibly.
        # A change log you cannot trust is worse than none, because you
        # would believe it when it is empty.
        try:
            import ops as _ops_cc
            _ops_cc.capture(exc, job="capability_change",
                            context=f"restaurant_id={restaurant_id} kind={kind}")
        except Exception:
            print(f"[capability_change] failed to record {kind}: {exc}")


def get_capability_changes(restaurant_id: int, limit: int = 100,
                           db_path: str = DB_PATH) -> list:
    import json as _j
    init_capability_changes(db_path)
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM capability_changes WHERE restaurant_id=? "
        "ORDER BY id DESC LIMIT ?", (restaurant_id, int(limit))).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        for side in ("before", "after"):
            raw = d.pop(f"{side}_json", None)
            try:
                d[side] = _j.loads(raw) if raw else None
            except Exception:
                d[side] = None
        out.append(d)
    return out


def get_shift_profiles(restaurant_id: int, include_inactive: bool = False,
                       db_path: str = DB_PATH) -> list:
    """This restaurant's own profiles, as plain dicts. Empty means it has
    none, and the engine then judges against its built-in set."""
    import json as _j
    init_shift_profiles(db_path)
    conn = get_conn(db_path)
    sql = "SELECT * FROM shift_profiles WHERE restaurant_id=?"
    if not include_inactive:
        sql += " AND active=1"
    sql += " ORDER BY priority, key"
    rows = conn.execute(sql, (restaurant_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            config = _j.loads(r["config_json"]) or {}
        except Exception:
            # A corrupt row must not take the whole profile set down with
            # it — the schedule still has to generate.
            continue
        config.update({"key": r["key"], "label": r["label"], "priority": r["priority"]})
        config["active"] = bool(r["active"])
        config["updated_by"] = r["updated_by"]
        config["updated_at"] = r["updated_at"]
        out.append(config)
    return out


def save_shift_profile(restaurant_id: int, profile: dict, updated_by: str = None,
                       db_path: str = DB_PATH) -> dict:
    """Insert or replace one profile, keyed by its own key."""
    import json as _j
    key = str(profile.get("key") or "").strip()
    if not key:
        raise ValueError("a profile needs a key")
    label = str(profile.get("label") or key.replace("_", " ").title()).strip()
    priority = int(profile.get("priority") or 0)
    active = 0 if profile.get("active") is False else 1
    config = {k: v for k, v in profile.items()
              if k not in ("key", "label", "priority", "active", "updated_by", "updated_at")}
    init_shift_profiles(db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO shift_profiles
                (restaurant_id, key, label, config_json, priority, active, updated_by, updated_at)
            VALUES (?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(restaurant_id, key) DO UPDATE SET
                label=excluded.label, config_json=excluded.config_json,
                priority=excluded.priority, active=excluded.active,
                updated_by=excluded.updated_by, updated_at=datetime('now')
        """, (restaurant_id, key, label, _j.dumps(config), priority, active,
              (updated_by or "").strip()[:120] or None))
        conn.commit()
    finally:
        conn.close()
    return dict(config, key=key, label=label, priority=priority, active=bool(active))


def delete_shift_profile(restaurant_id: int, key: str, db_path: str = DB_PATH) -> bool:
    init_shift_profiles(db_path)
    conn = get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM shift_profiles WHERE restaurant_id=? AND key=?",
                           (restaurant_id, key))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_quality_weights(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """How much each quality dimension counts for this restaurant.

    Empty means the engine's own defaults. Values are validated by the
    engine rather than here, so a stored weight for a dimension that no
    longer exists is simply ignored instead of raising.
    """
    import json as _j
    r = get_restaurant(restaurant_id, db_path)
    raw = getattr(r, "quality_weights_json", None) if r else None
    if not raw:
        return {}
    try:
        parsed = _j.loads(raw)
        return {str(k): float(v) for k, v in (parsed or {}).items()
                if isinstance(v, (int, float)) and float(v) >= 0}
    except Exception:
        return {}


# ── Signals the Shift Quality Engine reads about people ────────────────────

def _cached_shifts(restaurant_id: int) -> list:
    """One parse of the shift history per request, shared by every reader.

    get_employee_tenure, get_prior_shift_pattern and historical_patterns
    each loaded and re-parsed the whole CSV, so a single manager edit paid
    for three full passes over the restaurant's entire history. Scoped to
    the Flask request so it can never serve one restaurant's shifts to
    another, and falling back to a plain load outside a request context.
    """
    from labor import load_shifts_for_restaurant
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return load_shifts_for_restaurant(restaurant_id) or []
        cache = getattr(g, "_shift_cache", None)
        if cache is None:
            cache = g._shift_cache = {}
        if restaurant_id not in cache:
            cache[restaurant_id] = load_shifts_for_restaurant(restaurant_id) or []
        return cache[restaurant_id]
    except Exception:
        try:
            return load_shifts_for_restaurant(restaurant_id) or []
        except Exception:
            return []


def get_employee_tenure(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """{employee_name: shifts worked} from this restaurant's own history.

    A count of real shifts rather than a hire date, because shift data is
    what this product actually has. A hire-date field would be one more
    thing nobody fills in, and an experience dimension built on an empty
    column would score every restaurant identically.
    """
    try:
        out = {}
        for sh in _cached_shifts(restaurant_id):
            name = (sh.get("employee") or "").strip()
            if name:
                out[name] = out.get(name, 0) + 1
        return out
    except Exception:
        return {}


def get_leader_flags(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """{employee_name: True} for everyone marked authorised to close.

    Reads the capability layer's can_close attribute — registered since
    version one and surfaced for the first time here, which is the
    architecture claim actually paying off.
    """
    caps = get_capabilities(restaurant_id, attribute="can_close", db_path=db_path)
    return {name: bool(attrs.get("can_close", {}).get("flag"))
            for name, attrs in caps.items()
            if attrs.get("can_close", {}).get("flag")}


def get_prior_shift_pattern(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """{name: {"days": [...], "dayparts": [...]}} — what each person usually works.

    Schedule stability is worth something to staff and costs the restaurant
    nothing when demand has not moved. This is the baseline it is measured
    against, drawn from the shifts they have actually worked rather than
    from a stated preference nobody keeps up to date.
    """
    try:
        from shift_quality import daypart_of
        from datetime import datetime as _dt
        out = {}
        for sh in _cached_shifts(restaurant_id):
            name = (sh.get("employee") or "").strip()
            if not name:
                continue
            entry = out.setdefault(name, {"days": set(), "dayparts": set()})
            try:
                entry["days"].add(_dt.strptime(sh.get("date", ""), "%Y-%m-%d").strftime("%A"))
            except (ValueError, TypeError):
                if sh.get("day"):
                    entry["days"].add(sh["day"])
            part = daypart_of(sh.get("shift_start", ""))
            if part != "unknown":
                entry["dayparts"].add(part)
        return {n: {"days": sorted(v["days"]), "dayparts": sorted(v["dayparts"])}
                for n, v in out.items()}
    except Exception:
        return {}


def sibling_location_shifts(restaurant_id: int, dates: list,
                            db_path: str = DB_PATH) -> dict:
    """{employee_name: [{date, location}]} from the OTHER sites in this group.

    Employees are keyed by name per restaurant, which is correct isolation
    and means a person working two sites of the same group has two unrelated
    records — and nothing anywhere notices when both sites schedule them on
    the same night. Scores are deliberately NOT merged: two locations may
    rate the same person differently and both be right. Only the collision
    is reported, because only the collision is a fact rather than a judgement.

    Scoped to restaurants sharing this one's location_group AND owner_email,
    which is how the rest of the codebase defines that tenancy boundary.
    """
    if not dates:
        return {}
    conn = get_conn(db_path)
    try:
        me = conn.execute("SELECT location_group, owner_email, location_name "
                          "FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        group = ((me["location_group"] if me else "") or "").strip()
        if not group:
            return {}
        siblings = conn.execute(
            "SELECT id, COALESCE(location_name, name) AS label FROM restaurants "
            "WHERE location_group=? AND owner_email=? AND id<>?",
            (group, me["owner_email"], restaurant_id)).fetchall()
        if not siblings:
            return {}
        out = {}
        wanted = set(dates)
        for sib in siblings:
            row = conn.execute(
                "SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? "
                "ORDER BY id DESC LIMIT 1", (sib["id"],)).fetchone()
            for line in ((row["schedule_csv"] if row else "") or "").split("\n")[1:]:
                parts = [p.strip() for p in line.split(",", 7)]
                if len(parts) < 3:
                    continue
                date, name = parts[0], parts[2]
                if date in wanted and name:
                    entries = out.setdefault(name, [])
                    if not any(e["date"] == date and e["location"] == sib["label"]
                               for e in entries):
                        entries.append({"date": date, "location": sib["label"]})
        return out
    except Exception:
        return {}
    finally:
        conn.close()


def get_unavailability_map(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """{employee_name: {days they cannot work}} from staff_availability.

    The what-if pass needs this in a form it can test cheaply: a swap that
    puts somebody on a day they said they cannot work is not an
    improvement, it is a broken schedule with a better score.
    """
    import json as _j
    out = {}
    for row in get_staff_availability(restaurant_id, db_path=db_path) or []:
        name = (row.get("employee_name") or "").strip()
        if not name:
            continue
        try:
            blocked = set(_j.loads(row.get("unavailable_days") or "[]") or [])
        except Exception:
            blocked = set()
        if blocked:
            out[name] = blocked
    return out


def load_shifts_for_restaurant_roles(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """{employee_name: most recent role} from this restaurant's shift data.

    Threshold validation needs it to answer "could this team ever reach
    that number", which is a per-role question.
    """
    try:
        out, latest = {}, {}
        for sh in _cached_shifts(restaurant_id):
            n = (sh.get("employee") or "").strip()
            r = (sh.get("role") or "").strip()
            d = sh.get("date") or ""
            if not (n and r):
                continue
            if d >= latest.get(n, ""):
                latest[n] = d
                out[n] = r
        return out
    except Exception:
        return {}


def get_shift_leader_rules(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """Shift leader requirements, layered on the same capability data.

    A rule names a role, a bar, and where it applies:
      {"days": ["Saturday"], "daypart": "night", "role": "Bartender",
       "min_score": 5, "count": 1}
      {"closing": true, "role": "Server", "attribute": "can_close", "count": 1}
    """
    import json as _j
    r = get_restaurant(restaurant_id, db_path)
    if not r or not getattr(r, "shift_leader_rules_json", None):
        return []
    try:
        raw = _j.loads(r.shift_leader_rules_json)
        return [x for x in (raw or []) if isinstance(x, dict) and x.get("role")]
    except Exception:
        return []


def get_staff_availability(restaurant_id: int, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM staff_availability WHERE restaurant_id=? ORDER BY employee_name",
        (restaurant_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_staff_availability(restaurant_id: int, employee_name: str,
                             available_days: list, unavailable_days: list = None,
                             notes: str = None, db_path: str = DB_PATH):
    import json as _j
    conn = get_conn(db_path)
    conn.execute("""INSERT INTO staff_availability
        (restaurant_id, employee_name, available_days, unavailable_days, notes, updated_at)
        VALUES (?,?,?,?,?,datetime('now'))
        ON CONFLICT(restaurant_id, employee_name) DO UPDATE SET
            available_days=excluded.available_days,
            unavailable_days=excluded.unavailable_days,
            notes=excluded.notes,
            updated_at=excluded.updated_at""",
        (restaurant_id, employee_name,
         _j.dumps(available_days or []),
         _j.dumps(unavailable_days or []) if unavailable_days else None,
         notes))
    conn.commit()
    conn.close()

def delete_staff_availability(restaurant_id: int, employee_name: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM staff_availability WHERE restaurant_id=? AND employee_name=?",
                 (restaurant_id, employee_name))
    conn.commit()
    conn.close()

# ── Client data helpers ───────────────────────────────────────────────────────

def save_client_data(restaurant_id: int, data_type: str,
                     csv_content: str, source: str = "upload",
                     db_path: str = DB_PATH):
    """Save labor (shifts) or inventory CSV for a client."""
    conn = get_conn(db_path)
    existing = conn.execute(
        "SELECT id FROM client_data WHERE restaurant_id=?",
        (restaurant_id,)
    ).fetchone()
    if existing:
        conn.execute(f"""
            UPDATE client_data
            SET {data_type}_csv=?, {data_type}_source=?, updated_at=datetime('now')
            WHERE restaurant_id=?
        """, (csv_content, source, restaurant_id))
    else:
        conn.execute(f"""
            INSERT INTO client_data (restaurant_id, {data_type}_csv, {data_type}_source)
            VALUES (?, ?, ?)
        """, (restaurant_id, csv_content, source))
    conn.commit()
    conn.close()

    # Every inventory CSV save immediately becomes ledger rows too — no
    # separate "import" click needed. import_csv_to_ingredients() is
    # idempotent (skips already-imported names), so this is safe to run on
    # every save, including re-uploads that only add a few new items.
    if data_type == "inventory":
        try:
            import inventory_ledger
            inventory_ledger.import_csv_to_ingredients(restaurant_id)
        except Exception as e:
            print(f"[save_client_data] auto-migration to ingredients failed: {e}")


def get_client_data(restaurant_id: int,
                    db_path: str = DB_PATH) -> Optional[dict]:
    """Get client's CSV data. Returns None if not set."""
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT * FROM client_data WHERE restaurant_id=?",
        (restaurant_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def reset_user_password(user_id: int, new_password: str,
                        db_path: str = DB_PATH):
    """Admin reset of a user password."""
    from werkzeug.security import generate_password_hash
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (generate_password_hash(new_password), user_id))
    conn.commit()
    conn.close()


def create_reset_token(email: str, db_path: str = DB_PATH) -> str | None:
    """Create a password reset token for the user with this email. Returns token or None if not found."""
    import secrets
    from datetime import datetime, timezone, timedelta
    conn = get_conn(db_path)
    user = conn.execute("SELECT id FROM users WHERE email=? AND is_active=1", (email,)).fetchone()
    if not user:
        conn.close()
        return None
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    conn.execute("UPDATE users SET reset_token=?, reset_token_expires=? WHERE id=?",
                 (token, expires, user["id"]))
    conn.commit()
    conn.close()
    return token


def validate_reset_token(token: str, db_path: str = DB_PATH) -> dict | None:
    """Validate a reset token. Returns user row or None if invalid/expired."""
    from datetime import datetime, timezone
    conn = get_conn(db_path)
    user = conn.execute(
        "SELECT * FROM users WHERE reset_token=? AND is_active=1", (token,)
    ).fetchone()
    conn.close()
    if not user:
        return None
    expires = user["reset_token_expires"]
    if not expires:
        return None
    try:
        exp = datetime.fromisoformat(expires.replace("Z", ""))
        # Ensure both datetimes are timezone-aware for comparison
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            return None
    except Exception:
        return None
    return dict(user)


def consume_reset_token(token: str, new_password: str, db_path: str = DB_PATH) -> bool:
    """Reset password using token. Returns True on success."""
    from werkzeug.security import generate_password_hash
    user = validate_reset_token(token, db_path)
    if not user:
        return False
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE users SET password_hash=?, reset_token=NULL, reset_token_expires=NULL WHERE id=?",
        (generate_password_hash(new_password), user["id"])
    )
    conn.commit()
    conn.close()
    return True


def get_approved_examples(restaurant_id: int, limit: int = 5,
                           db_path: str = DB_PATH) -> list:
    """Return recent approved review responses as style examples for the AI."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT rating, text, draft_response FROM reviews
        WHERE restaurant_id=?
          AND response_status IN ('approved','posted')
          AND draft_response IS NOT NULL
          AND draft_response != ''
        ORDER BY id DESC
        LIMIT ?
    """, (restaurant_id, limit)).fetchall()
    conn.close()
    return [{"rating": r["rating"], "review": r["text"][:120], "response": r["draft_response"]} for r in rows]


def save_labor_snapshot(restaurant_id: int, period_start: str, period_end: str,
                         labor_pct: float, total_labor: float, total_sales: float,
                         db_path: str = DB_PATH):
    """Save a labor analysis snapshot for trend tracking."""
    conn = get_conn(db_path)
    conn.execute("""
        INSERT INTO labor_history (restaurant_id, period_start, period_end, labor_pct, total_labor, total_sales)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (restaurant_id, period_start, period_end, labor_pct, total_labor, total_sales))
    conn.commit()
    conn.close()


def get_labor_history(restaurant_id: int, limit: int = 4,
                      db_path: str = DB_PATH) -> list:
    """Return recent labor snapshots for trend awareness.

    save_labor_snapshot() inserts a new row every time an insight is
    generated, not just on a genuinely new upload — a restaurant whose
    data hasn't changed can accumulate many rows for the same
    period_start/period_end. Without dedup, those duplicates share one
    x-axis label on the client's trend chart, which makes Swift Charts'
    BarMark treat them as a stacked series (same category = stack) and
    sum them into one wildly-inflated bar. Keep only the latest snapshot
    (highest id) per distinct period.
    """
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT h.period_start, h.period_end, h.labor_pct, h.total_labor, h.total_sales
        FROM labor_history h
        JOIN (
            SELECT period_start, MAX(id) AS max_id
            FROM labor_history
            WHERE restaurant_id=?
            GROUP BY period_start
        ) latest ON h.id = latest.max_id
        ORDER BY h.period_start DESC LIMIT ?
    """, (restaurant_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_schedule_history(restaurant_id: int, week_start: str, week_end: str,
                           hours_scheduled: float, hours_budget: float, labor_target: float,
                           schedule_csv: str, summary: list, quality: dict = None,
                           db_path: str = DB_PATH) -> int:
    """Persists every generated schedule permanently, independent of
    whatever the mobile app's own client-side caching does — a durable
    record on the Account tab's Schedule History screen that survives
    regardless of any iOS view-state bug, rather than depending on getting
    every layer of client caching right. Returns the new row's id.
    """
    import json as _json_sh
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        generated_at TEXT NOT NULL DEFAULT (datetime('now')),
        week_start TEXT,
        week_end TEXT,
        hours_scheduled REAL,
        hours_budget REAL,
        labor_target REAL,
        schedule_csv TEXT,
        summary_json TEXT
    )""")
    # The Shift Quality verdict used to live only in the async job result,
    # which is deleted the first time it is polled — so the headline number
    # an owner is asked to trust could never be looked at again, and
    # Schedule History showed past weeks with no score and no trend.
    _ensure_history_columns(conn)
    cur = conn.execute("""
        INSERT INTO schedule_history
            (restaurant_id, week_start, week_end, hours_scheduled, hours_budget,
             labor_target, schedule_csv, summary_json, quality_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target,
          schedule_csv, _json_sh.dumps(summary or []),
          _json_sh.dumps(quality) if quality else None))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def _ensure_history_columns(conn):
    """Columns added to schedule_history after it first shipped."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(schedule_history)")}
    for name, decl in (("quality_json", "TEXT"), ("edited_at", "TEXT"),
                       ("edited_by", "TEXT")):
        if name not in have:
            conn.execute(f"ALTER TABLE schedule_history ADD COLUMN {name} {decl}")


def update_schedule_history_rows(restaurant_id: int, schedule_csv: str,
                                 quality: dict = None, history_id: int = None,
                                 edited_by: str = None, db_path: str = DB_PATH) -> int:
    """Write a manager's edited schedule back over the stored one.

    Without this the whole override feature was decorative: the edit lived
    in the page, the score moved, and publishing read the CSV saved at
    generation time — so staff received the week the manager had just
    fixed, unfixed. Returns the history row id, or 0 when there is nothing
    to write to.
    """
    import json as _json_sh
    conn = get_conn(db_path)
    try:
        _ensure_history_columns(conn)
        if history_id:
            row = conn.execute("SELECT id FROM schedule_history WHERE id=? AND restaurant_id=?",
                               (history_id, restaurant_id)).fetchone()
        else:
            row = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=? "
                               "ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
        if not row:
            return 0
        conn.execute("""UPDATE schedule_history
                        SET schedule_csv=?, quality_json=COALESCE(?, quality_json),
                            edited_at=datetime('now'), edited_by=?
                        WHERE id=? AND restaurant_id=?""",
                     (schedule_csv, _json_sh.dumps(quality) if quality else None,
                      (edited_by or "").strip()[:120] or None, row["id"], restaurant_id))
        conn.commit()
        return row["id"]
    finally:
        conn.close()


def get_schedule_history(restaurant_id: int, limit: int = 300, db_path: str = DB_PATH) -> list:
    """Summary rows only (no schedule_csv) for the history list screen —
    keeps the list payload light; fetch the full record via
    get_schedule_history_detail() once a specific entry is tapped."""
    conn = get_conn(db_path)
    try:
        _ensure_history_columns(conn)
        rows = conn.execute("""
            SELECT id, generated_at, week_start, week_end, hours_scheduled,
                   hours_budget, labor_target, quality_json, edited_at
            FROM schedule_history WHERE restaurant_id=?
            ORDER BY generated_at DESC, id DESC LIMIT ?
        """, (restaurant_id, limit)).fetchall()
    except Exception:
        rows = []  # table doesn't exist yet -- no schedule has ever been generated
    conn.close()
    # Only the headline travels with the list. The full evaluation is large
    # and the list screen shows a score and a band, not eleven dimensions.
    import json as _json_hs
    out = []
    for r in rows:
        d = dict(r)
        raw = d.pop("quality_json", None)
        try:
            q = _json_hs.loads(raw) if raw else None
        except Exception:
            q = None
        d["quality_score"] = (q or {}).get("score")
        d["quality_band"] = (q or {}).get("band")
        d["confidence"] = ((q or {}).get("confidence") or {}).get("level")
        out.append(d)
    return out


def get_schedule_history_detail(history_id: int, restaurant_id: int, db_path: str = DB_PATH):
    """Full record including schedule_csv, scoped to restaurant_id so one
    tenant can never read another's by guessing an id — returns None on a
    missing id or an id that belongs to a different restaurant, treating
    both the same to avoid confirming which ids exist for other tenants."""
    import json as _json_sh
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT * FROM schedule_history WHERE id=? AND restaurant_id=?
        """, (history_id, restaurant_id)).fetchone()
    except Exception:
        row = None
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["summary"] = _json_sh.loads(d.pop("summary_json") or "[]")
    except Exception:
        d["summary"] = []
    try:
        d["quality"] = _json_sh.loads(d.pop("quality_json", None) or "null")
    except Exception:
        d["quality"] = None
    return d


def delete_schedule_history(history_id: int, restaurant_id: int, db_path: str = DB_PATH) -> bool:
    """Deletes one schedule history row, scoped to restaurant_id so one
    tenant can never delete another's by guessing an id. Schedules are
    never removed automatically anywhere in this codebase -- this is the
    only deletion path, and it only ever fires on an explicit user action
    (the iOS swipe-to-delete). Returns True if a row was actually deleted,
    False if the id didn't exist or belonged to a different restaurant.
    """
    conn = get_conn(db_path)
    cur = conn.execute("DELETE FROM schedule_history WHERE id=? AND restaurant_id=?", (history_id, restaurant_id))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_role_rates(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Return per-role hourly rates dict. Falls back to flat hourly_rate for any missing role."""
    import json as _json
    r = get_restaurant(restaurant_id, db_path)
    if not r:
        return {}
    base = r.hourly_rate or 26.0
    if r.role_rates_json:
        try:
            rates = _json.loads(r.role_rates_json)
            return {k: float(v) for k, v in rates.items()}
        except Exception:
            pass
    return {"_default": base}


def get_close_times(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Per-day-of-week close time strings (e.g. {"Monday": "9:00pm", "Friday":
    "10:00pm"}) for hard-capping a generated schedule's shift_end. Empty dict
    when unconfigured — callers should treat "no config" as "don't enforce,"
    not as "close time is midnight," since most restaurants haven't set this
    yet and generation shouldn't start rejecting rows for restaurants that
    never opted in.
    """
    import json as _json
    r = get_restaurant(restaurant_id, db_path)
    if not r or not r.close_times_json:
        return {}
    try:
        return {k: str(v) for k, v in _json.loads(r.close_times_json).items()}
    except Exception:
        return {}


def get_role_close_buffers(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Per-role minutes a shift may run past close (e.g. {"Bartender": 60}
    for a stated "stay 1h after close" rule). Any role not present here
    defaults to 0 — must end at or before close."""
    import json as _json
    r = get_restaurant(restaurant_id, db_path)
    if not r or not r.role_close_buffer_json:
        return {}
    try:
        return {k: int(v) for k, v in _json.loads(r.role_close_buffer_json).items()}
    except Exception:
        return {}


def compute_blended_rate(shifts: list, role_rates: dict, fallback: float = 26.0) -> float:
    """Compute weighted blended hourly rate from actual shifts and per-role rates."""
    total_cost = 0.0
    total_hours = 0.0
    default = role_rates.get("_default", fallback)
    for s in shifts:
        role = s.get("role", "")
        hrs = float(s.get("actual_hours") or s.get("scheduled_hours") or 0)
        rate = role_rates.get(role, default)
        total_cost += hrs * rate
        total_hours += hrs
    if not total_hours:
        return default
    return round(total_cost / total_hours, 2)


def save_labor_daily_history(restaurant_id: int, by_day: dict,
                              db_path: str = DB_PATH):
    """Persist per-day labor breakdown from a shifts analysis. Called on every CSV upload."""
    from datetime import datetime as _dt
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS labor_daily_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        day_of_week TEXT,
        labor_pct REAL,
        labor_cost REAL,
        sales REAL,
        total_hours REAL,
        saved_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, date) ON CONFLICT REPLACE
    )""")
    for date_str, day_data in by_day.items():
        sales = day_data.get("sales", 0)
        actual_hours = day_data.get("actual", 0)
        # Use pre-computed per-role labor cost/pct from analyse_shifts (already correct)
        labor_cost = day_data.get("labor_cost", 0)
        labor_pct = day_data.get("labor_pct") if day_data.get("labor_pct") is not None else (
            round(labor_cost / sales * 100, 1) if sales else None
        )
        try:
            dow = _dt.strptime(date_str, "%Y-%m-%d").strftime("%A")
        except Exception:
            dow = None
        conn.execute("""
            INSERT INTO labor_daily_history
                (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, total_hours)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(restaurant_id, date) DO UPDATE SET
                day_of_week=excluded.day_of_week,
                labor_pct=excluded.labor_pct,
                labor_cost=excluded.labor_cost,
                sales=excluded.sales,
                total_hours=excluded.total_hours,
                saved_at=datetime('now')
        """, (restaurant_id, date_str, dow, labor_pct, labor_cost, sales, actual_hours))
    conn.commit()
    conn.close()


def get_yoy_schedule_context(restaurant_id: int, next_week_dates: list,
                              db_path: str = DB_PATH) -> list:
    """
    For each date in next_week_dates, find the same calendar day last year
    (52 weeks back = same weekday). Returns a list of dicts with YoY data.
    """
    from datetime import datetime as _dt, timedelta as _td
    conn = get_conn(db_path)
    rows_out = []
    for date_str in next_week_dates:
        try:
            dt = _dt.strptime(date_str, "%Y-%m-%d")
            yoy_dt = dt - _td(weeks=52)
            # Search ±3 days window around the 52-week-ago date for any data
            candidates = []
            for offset in range(-3, 4):
                candidate_date = (yoy_dt + _td(days=offset)).strftime("%Y-%m-%d")
                row = conn.execute(
                    "SELECT * FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                    (restaurant_id, candidate_date)
                ).fetchone()
                if row:
                    candidates.append(dict(row))
            # Prefer exact 52-week match, fall back to closest with data
            exact = next((c for c in candidates if c["date"] == yoy_dt.strftime("%Y-%m-%d")), None)
            best = exact or (candidates[0] if candidates else None)
            rows_out.append({
                "next_week_date": date_str,
                "next_week_dow": dt.strftime("%A"),
                "yoy_date": best["date"] if best else None,
                "yoy_sales": best["sales"] if best else None,
                "yoy_labor_pct": best["labor_pct"] if best else None,
                "yoy_labor_cost": best["labor_cost"] if best else None,
                "yoy_hours": best["total_hours"] if best else None,
            })
        except Exception:
            rows_out.append({"next_week_date": date_str, "yoy_date": None})
    conn.close()
    return rows_out


def log_activity(restaurant_id: int, tab: str,
                 db_path: str = DB_PATH):
    """Record last active tab, timestamp, and append to activity_log."""
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S')
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE restaurants SET last_active_tab=?, last_activity=? WHERE id=?
    """, (tab, now, restaurant_id))
    try:
        conn.execute("""
            INSERT INTO activity_log (restaurant_id, event_type, event_data, created_at)
            VALUES (?, 'tab_view', ?, ?)
        """, (restaurant_id, json.dumps({"tab": tab}), now))
    except Exception as e:
        # Best-effort, but not invisible: this feeds the Account page's
        # activity trail, and a gap there reads as "nothing happened".
        try:
            import ops
            ops.capture(e, job="log_activity", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def log_event(restaurant_id: int, event_type: str, event_data: dict = None,
              db_path: str = DB_PATH):
    """Log a named event to activity_log (login, review_approved, csv_upload, etc.)"""
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO activity_log (restaurant_id, event_type, event_data, created_at)
            VALUES (?, ?, ?, ?)
        """, (restaurant_id, event_type, json.dumps(event_data or {}),
                datetime.now(ZoneInfo('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S')))
        conn.commit()
    except Exception as e:
        print(f"log_event error: {e}")
    finally:
        conn.close()


def get_activity_summary(restaurant_id: int, days: int = 30,
                         db_path: str = DB_PATH) -> dict:
    """Return tab usage counts and recent events for a restaurant."""
    from datetime import datetime, timezone, timedelta
    import json
    from zoneinfo import ZoneInfo as _ZI_m
    since = (datetime.now(_ZI_m('America/Chicago')) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S')
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT event_type, event_data, created_at FROM activity_log
        WHERE restaurant_id=? AND created_at >= ?
        ORDER BY created_at DESC
    """, (restaurant_id, since)).fetchall()
    conn.close()

    tab_counts = {}
    event_counts = {}
    for row in rows:
        et = row["event_type"]
        event_counts[et] = event_counts.get(et, 0) + 1
        if et == "tab_view":
            try:
                data = json.loads(row["event_data"] or "{}")
                tab = data.get("tab", "unknown")
                tab_counts[tab] = tab_counts.get(tab, 0) + 1
            except Exception:
                pass
    return {
        "tab_counts": tab_counts,
        "event_counts": event_counts,
        "total_events": len(rows),
    }


# ── Service tier → module access ──────────────────────────────────────────────

TIER_MODULES = {
    "trial":              {"reviews":1,"labor":1,"inventory":1,"marketing":1},
    "starter_reviews":    {"reviews":1,"labor":0,"inventory":0,"marketing":0},
    "starter_labor":      {"reviews":0,"labor":1,"inventory":0,"marketing":0},
    "starter_inventory":  {"reviews":0,"labor":0,"inventory":1,"marketing":0},
    "starter_marketing":  {"reviews":0,"labor":0,"inventory":0,"marketing":1},
    "full":               {"reviews":1,"labor":1,"inventory":1,"marketing":1},
}


def set_service_tier(restaurant_id: int, tier: str,
                     db_path: str = DB_PATH):
    """Set service tier and auto-configure module access."""
    modules = TIER_MODULES.get(tier, TIER_MODULES["trial"])
    update_restaurant(restaurant_id, {
        "service_tier":    tier,
        "module_reviews":  modules["reviews"],
        "module_labor":    modules["labor"],
        "module_inventory":modules["inventory"],
        "module_marketing":modules["marketing"],
    }, db_path)


def get_all_restaurants(db_path: str = DB_PATH) -> list:
    """Get all active restaurant records."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM restaurants WHERE id > 0 ORDER BY id"
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        try:
            result.append(get_restaurant(row["id"], db_path))
        except Exception:
            pass
    return result

def get_restaurants_for_digest(day: str, db_path: str = DB_PATH) -> list:
    """Get all restaurants scheduled for digest on a given day of week."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT r.*, u.email as contact_email
        FROM restaurants r
        JOIN users u ON u.restaurant_id = r.id AND u.is_admin = 0
        WHERE r.digest_day=? AND r.digest_enabled=1 AND r.module_reviews=1
    """, (day.lower(),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_last_fetched(restaurant_id: int, db_path: str = DB_PATH):
    """Record when reviews were last fetched for a restaurant."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo as _ZI_m
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET last_fetched_at=? WHERE id=?",
                 (datetime.now(_ZI_m('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S'), restaurant_id))
    conn.commit()
    conn.close()


# Billing states that still entitle a restaurant to use the product.
# `paused`/`churned` do not. Anything unrecognised — including NULL on rows
# that predate the column — DOES, deliberately: locking a paying customer out
# because of a value nobody anticipated is a far worse failure than briefly
# serving one who cancelled, and the audit flagged both directions.
ACTIVE_BILLING_STATES = {"trial", "active", "internal", "past_due", "pending", ""}
BLOCKED_BILLING_STATES = {"churned", "paused", "canceled", "cancelled"}


def subscription_allows_access(restaurant_id: int, db_path: str = DB_PATH) -> bool:
    """Whether this restaurant's billing state still entitles it to service.

    Before this existed nothing anywhere checked billing_status for access:
    customer.subscription.deleted only emailed Will asking him to go
    deactivate the account by hand, so a cancelled customer kept the full
    dashboard, the iOS app and every Claude-backed feature — indefinitely,
    and at Cavnar's API cost — until someone noticed an email.

    Fails OPEN on any lookup error for the reason above: an unreachable
    database must never cut off paying customers.
    """
    try:
        conn = get_conn(db_path)
        row = conn.execute(
            "SELECT billing_status FROM restaurants WHERE id=?", (restaurant_id,)
        ).fetchone()
        conn.close()
    except Exception:
        return True
    if not row:
        return True
    status = (row["billing_status"] or "").strip().lower()
    return status not in BLOCKED_BILLING_STATES


def normalize_owner_email(value) -> str:
    """Owner emails are compared, never displayed, from here — one spelling."""
    return (value or "").strip().lower()


def get_location_group(group_name: str, db_path: str = DB_PATH, owner_email=None) -> list:
    """Every restaurant in a location group.

    `location_group` is free text an admin types per location, and it is what
    the multi-location features key on: switching the active location, the
    group Home rollup, and (since billing reconciliation moved here) which
    restaurants a Stripe cancellation churns. Matching on the string alone
    meant two unrelated clients typed into the same group — "Syrup" twice,
    a paste into the wrong row — became one tenant: either owner could
    switch into the other's locations and read their reviews, labor and
    sales, and cancelling one would churn the other.

    Passing `owner_email` scopes the group to that owner, which is what every
    caller acting on a client's behalf does. Callers that deliberately want
    the raw name match (admin tooling reporting on a collision) omit it.
    """
    conn = get_conn(db_path)
    if owner_email is not None:
        rows = conn.execute(
            "SELECT * FROM restaurants WHERE location_group=? AND LOWER(TRIM(COALESCE(owner_email,'')))=? "
            "ORDER BY location_name",
            (group_name, normalize_owner_email(owner_email))
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM restaurants WHERE location_group=? ORDER BY location_name",
            (group_name,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def place_id_conflict(place_id: str, exclude_id=None, db_path: str = DB_PATH):
    """The name of another LIVE restaurant already using this Google listing.

    Two live restaurants on one google_place_id means both fetch the same
    reviews, and only one of them can store any given review (the key is
    per-restaurant now, but the second copy is still a duplicate the wrong
    owner replies to). Demo rows are exempt — a demo deliberately mirrors a
    real listing and never fetches.
    """
    pid = (place_id or "").strip()
    if not pid:
        return None
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name FROM restaurants WHERE google_place_id=? AND COALESCE(is_demo,0)=0",
            (pid,)
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        return r["name"] or f"restaurant #{r['id']}"
    return None


def location_group_conflict(group_name: str, owner_email: str, exclude_id=None,
                            db_path: str = DB_PATH):
    """The owner email already using `group_name`, if it isn't this one.

    Returns None when the name is free or already belongs to `owner_email`.
    Called before an admin writes a group name so a collision is refused at
    the point it is created rather than discovered as a data leak later.
    """
    name = (group_name or "").strip()
    if not name:
        return None
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT id, owner_email FROM restaurants WHERE location_group=?", (name,)
    ).fetchall()
    conn.close()
    mine = normalize_owner_email(owner_email)
    for r in rows:
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        theirs = normalize_owner_email(r["owner_email"])
        if theirs and theirs != mine:
            return r["owner_email"]
    return None


def get_all_location_groups(db_path: str = DB_PATH) -> list:
    """Get distinct location group names."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT DISTINCT location_group FROM restaurants WHERE location_group IS NOT NULL ORDER BY location_group"
    ).fetchall()
    conn.close()
    return [r["location_group"] for r in rows]

# 30 days. Past this, the figure reflects a backfill/import rather than how
# fast anyone actually responds.
RESPONSE_TIME_CAP_HOURS = 30 * 24


# When the guest actually wrote the review. review_date is the truth and
# fetched_at is when Cavnar AI happened to pull it — on a first connect
# every review in a restaurant's history carries the same fetched_at, so
# bucketing or filtering on it collapsed three years of reviews into the
# onboarding week. Measured: an 8-week sentiment trend rendered one bar,
# "top issues in the last 90 days" counted a three-year-old review, and the
# negative-spike SMS claimed four reviews "in the last 7 days" when the
# newest was 54 days old. fetched_at stays as the fallback for a row that
# somehow has no review_date.
WRITTEN_AT = "COALESCE(NULLIF(r.review_date,''), r.fetched_at)"
WRITTEN_AT_BARE = "COALESCE(NULLIF(review_date,''), fetched_at)"


def get_reviews_by_ids(restaurant_id: int, ids: list, db_path: str = DB_PATH) -> list:
    """Re-read specific reviews as Review objects, scoped to one restaurant.

    Used to pick the analysis back up before alerts fire, so the health
    alert can read the urgency the analyser decided instead of matching
    keywords against raw guest text.
    """
    if not ids:
        return []
    conn = get_conn(db_path)
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM reviews WHERE restaurant_id=? AND id IN ({marks}) AND deleted_at IS NULL",
        (restaurant_id, *ids)).fetchall()
    conn.close()
    out = []
    for row in rows:
        k = row.keys()
        out.append(Review(
            id=row["id"], restaurant_id=row["restaurant_id"], platform=row["platform"],
            external_id=row["external_id"], author=row["author"], rating=row["rating"],
            text=row["text"], review_date=row["review_date"], fetched_at=row["fetched_at"],
            sentiment=row["sentiment"] if "sentiment" in k else None,
            summary=row["summary"] if "summary" in k else None,
            urgency=(row["urgency"] if "urgency" in k else None) or "normal",
            response_status=row["response_status"] if "response_status" in k else "pending",
            processed=bool(row["processed"]) if "processed" in k else False,
        ))
    by_id = {r.id: r for r in out}
    return [by_id[i] for i in ids if i in by_id]


def get_review_stats(restaurant_id):
    conn = get_conn()
    # Counts cover every review this restaurant has, analysed or not.
    #
    # This whole query was gated on processed=1, so a review whose Haiku
    # analysis failed — or that fell past analyse_pending's per-run limit —
    # vanished from the owner's totals, their average rating AND their
    # "needs response" queue. Measured on 25 reviews with 5 unanalysed
    # 1-stars: 20 shown, 4.2 average against a real 3.6, and five unanswered
    # one-star reviews nowhere in the queue of reviews to answer.
    #
    # Sentiment counts still require analysis, because an unanalysed review
    # genuinely has no sentiment; unanalysed is reported as its own number
    # rather than folded into one of the three.
    rows = conn.execute("""
        SELECT
            COUNT(*)                                                                    AS total,
            SUM(processed=0)                                                            AS unanalysed,
            SUM(sentiment='positive')                                                   AS positive,
            SUM(sentiment='negative')                                                   AS negative,
            SUM(sentiment='neutral')                                                    AS neutral,
            AVG(rating)                                                                 AS avg_rating,
            SUM(response_status='drafted')                                              AS drafted,
            SUM(response_status NOT IN ('drafted','posted','approved','skipped'))        AS needs_response,
            SUM(urgency='high' AND response_status NOT IN ('posted','approved','skipped')) AS urgent,
            SUM(response_status='posted')                                               AS posted,
            SUM(response_status IN ('posted','approved'))                               AS responded,
            SUM(response_status='skipped')                                              AS skipped,
            SUM(response_status IN ('posted','approved') AND approved_at >= date('now','start of month')) AS responded_this_month,
            SUM(review_date >= date('now','start of month'))                            AS received_this_month,
            SUM(review_date >= date('now','-30 days'))                                  AS last_30d,
            AVG(CASE WHEN review_date >= date('now','-30 days') THEN rating END)        AS avg_rating_30d
        FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL
    """, (restaurant_id,)).fetchone()

    # Average response time in hours (review_date → approved_at) — industry standard definition.
    # Matches how Google/Podium/Birdeye measure it: time from when customer wrote review
    # to when response appeared. Capped at 30 days in display layer to handle demo data.
    rt_row = conn.execute("""
        SELECT AVG(
            (julianday(approved_at) - julianday(review_date)) * 24
        ) AS avg_hrs
        FROM reviews
        WHERE restaurant_id=? AND response_status IN ('posted','approved')
          AND approved_at IS NOT NULL AND review_date IS NOT NULL
          AND deleted_at IS NULL
    """, (restaurant_id,)).fetchone()

    conn.close()
    total     = rows["total"]     or 0
    posted    = rows["posted"]    or 0
    responded = rows["responded"] or 0
    drafted   = rows["drafted"]   or 0
    needs_response = rows["needs_response"] or 0
    skipped   = rows["skipped"]   or 0
    this_month = rows["responded_this_month"] or 0
    last_30d  = rows["last_30d"]  or 0
    # Response rate = approved+posted / total
    response_rate = round((responded / total * 100) if total > 0 else 0, 1)
    avg_rating_30d = round(rows["avg_rating_30d"] or 0, 1)
    # Avg response time — cap at reasonable max for display
    # The SQL above documents a 30-day display cap that was never actually
    # implemented here — so seeded/backfilled reviews (approved long after
    # they were written) produced figures like "11495.9 hours", which
    # Ask Cavnar then stated to owners as fact. Values beyond the cap say
    # more about when data was imported than about response behaviour, so
    # they're reported as None ("no meaningful average yet") rather than as
    # a number nobody should act on.
    avg_hrs_raw = rt_row["avg_hrs"] if rt_row and rt_row["avg_hrs"] else None
    if avg_hrs_raw and 0 < avg_hrs_raw <= RESPONSE_TIME_CAP_HOURS:
        avg_response_hours = round(avg_hrs_raw, 1)
    else:
        avg_response_hours = None

    positive = rows["positive"] or 0
    # Over the reviews that HAVE a sentiment, not over every review held.
    # Dropping the processed=1 filter made `total` include reviews the
    # analyser never classified, so a restaurant with 3 positives out of 3
    # analysed and 2 unanalysed read 60% positive rather than 100% — a
    # number that moves when an AI call fails, not when a guest's opinion
    # does. The unanalysed count travels alongside so the shortfall is
    # visible rather than absorbed into this figure.
    _classified = (rows["positive"] or 0) + (rows["negative"] or 0) + (rows["neutral"] or 0)
    positive_pct = round(positive / _classified * 100) if _classified > 0 else 0
    # Reviews we hold but could not analyse. Non-zero means the sentiment
    # split and the topic charts cover less than the totals beside them.
    unanalysed = rows["unanalysed"] or 0

    return dict(
        total             = total,
        positive          = positive,
        positive_pct      = positive_pct,
        negative          = rows["negative"]  or 0,
        neutral           = rows["neutral"]   or 0,
        urgent            = rows["urgent"]    or 0,
        avg_rating        = round(rows["avg_rating"] or 0, 1),
        avg_rating_30d    = avg_rating_30d,
        awaiting_approval = drafted,
        needs_response    = needs_response,
        posted            = posted,
        responded         = responded,
        skipped           = skipped,
        this_month        = this_month,              # responded this calendar month
        received_this_month = rows["received_this_month"] or 0,  # reviews received this month
        last_30d          = last_30d,
        response_rate     = response_rate,
        avg_response_hours= avg_response_hours,
        unanalysed        = unanalysed,
        sentiment_complete = (unanalysed == 0),
        # How many reviews the sentiment split above actually covers.
        classified        = _classified,
    )

def get_sentiment_trend(restaurant_id, weeks=8):
    """Return weekly positive/negative counts for the last N weeks."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            strftime('%Y-%W', COALESCE(NULLIF(review_date,''), fetched_at))          AS week_key,
            MIN(DATE(COALESCE(NULLIF(review_date,''), fetched_at)))                  AS week_start,
            SUM(sentiment='positive')              AS positive,
            SUM(sentiment='negative')              AS negative,
            SUM(sentiment='neutral')               AS neutral,
            COUNT(*)                               AS total,
            ROUND(AVG(rating),1)                   AS avg_rating
        FROM reviews
        WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL
          AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', ? || ' days')
        GROUP BY week_key
        ORDER BY week_key ASC
    """, (restaurant_id, f"-{weeks * 7}")).fetchall()
    conn.close()
    from datetime import datetime as _dt_st
    result = []
    for row in rows:
        # Format label as M/D from week_start
        try:
            dt = _dt_st.strptime(row["week_start"], "%Y-%m-%d")
            label = f"{dt.month}/{dt.day}"
        except Exception:
            label = row["week_key"]
        result.append({
            "label":      label,
            "week_key":   row["week_key"],
            "positive":   row["positive"] or 0,
            "negative":   row["negative"] or 0,
            "neutral":    row["neutral"]  or 0,
            "total":      row["total"]    or 0,
            "avg_rating": row["avg_rating"] or 0,
        })
    return result

def get_top_issues(restaurant_id, days=90, limit=6):
    """Return top review categories by mention count for the last N days."""
    from collections import Counter
    conn = get_conn()
    rows = conn.execute("""
        SELECT categories FROM reviews
        WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL
        AND categories IS NOT NULL AND categories != '[]'
        AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', '-' || ? || ' days')
    """, (restaurant_id, str(days))).fetchall()
    conn.close()
    counts = Counter()
    for row in rows:
        try:
            cats = json.loads(row["categories"] or "[]")
            for c in cats:
                if c:
                    counts[c] += 1
        except Exception:
            pass
    # Friendly labels
    labels = {
        "food_quality":       "Food quality",
        "service":            "Service",
        "wait_time":          "Wait time",
        "value":              "Value",
        "ambiance":           "Ambiance",
        "cleanliness":        "Cleanliness",
        "reservation":        "Reservations",
        "takeout_delivery":   "Takeout / delivery",
    }
    results = []
    for cat, count in counts.most_common(limit):
        results.append({
            "category": cat,
            "label":    labels.get(cat, cat.replace("_", " ").title()),
            "count":    count,
        })
    return results

def get_topic_heatmap(restaurant_id: int, days: int = 90) -> list:
    """Return all 8 categories with sentiment breakdown and period-over-period trend."""
    from collections import defaultdict
    conn = get_conn()
    rows = conn.execute("""
        SELECT categories, sentiment FROM reviews
        WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL
          AND categories IS NOT NULL AND categories != '[]'
          AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', '-' || ? || ' days')
    """, (restaurant_id, str(days))).fetchall()
    prev_rows = conn.execute("""
        SELECT categories FROM reviews
        WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL
          AND categories IS NOT NULL AND categories != '[]'
          AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', '-' || ? || ' days')
          AND COALESCE(NULLIF(review_date,''), fetched_at) < datetime('now', '-' || ? || ' days')
    """, (restaurant_id, str(days * 2), str(days))).fetchall()
    conn.close()

    totals = defaultdict(int)
    pos    = defaultdict(int)
    neg    = defaultdict(int)
    neu    = defaultdict(int)
    for row in rows:
        try:
            for c in json.loads(row["categories"] or "[]"):
                if c:
                    totals[c] += 1
                    s = row["sentiment"] or "neutral"
                    if s == "positive":   pos[c] += 1
                    elif s == "negative": neg[c] += 1
                    else:                 neu[c] += 1
        except Exception:
            pass

    prev = defaultdict(int)
    for row in prev_rows:
        try:
            for c in json.loads(row["categories"] or "[]"):
                if c: prev[c] += 1
        except Exception:
            pass

    _LABELS = {
        "food_quality":    "Food Quality",
        "service":         "Service",
        "wait_time":       "Wait Time",
        "value":           "Value",
        "ambiance":        "Ambiance",
        "cleanliness":     "Cleanliness",
        "reservation":     "Reservations",
        "takeout_delivery":"Takeout & Delivery",
    }
    _ORDER = list(_LABELS.keys())

    results = []
    for cat in _ORDER:
        total = totals[cat]
        p, n, u = pos[cat], neg[cat], neu[cat]
        pv = prev[cat]
        if total == 0:
            trend = "flat"
        elif pv == 0 or total > pv * 1.15:
            trend = "up"
        elif total < pv * 0.85:
            trend = "down"
        else:
            trend = "flat"
        results.append({
            "category":    cat,
            "label":       _LABELS[cat],
            "count":       total,
            "positive":    p,
            "negative":    n,
            "neutral":     u,
            "pct_positive": round(p / total * 100) if total else 0,
            "pct_negative": round(n / total * 100) if total else 0,
            "trend":       trend,
        })
    results.sort(key=lambda x: x["count"], reverse=True)
    return results


def get_reviews_data(restaurant_id, filter_by="all", search="", category=None, platform=None):
    conn = get_conn()
    where  = ["processed=1", "restaurant_id=?", "deleted_at IS NULL"]
    params = [restaurant_id]
    if filter_by == "urgent":
        where.append("urgency='high'")
    elif filter_by in ("positive","neutral","negative"):
        where.append("sentiment=?"); params.append(filter_by)
    elif filter_by == "pending":
        where.append("response_status='drafted'")
    if search:
        where.append("(author LIKE ? OR text LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if category:
        # categories is a JSON array column (see get_topic_heatmap) — every
        # entry is one of the fixed keys in that function's _LABELS map, so
        # a quoted-substring LIKE reliably matches "the tag is present"
        # without needing SQLite's json_each.
        where.append("categories LIKE ?")
        params.append(f'%"{category}"%')
    if platform:
        where.append("platform=?")
        params.append(platform)
    rows = conn.execute(
        f"""SELECT * FROM reviews WHERE {' AND '.join(where)}
        ORDER BY CASE urgency WHEN 'high' THEN 0 ELSE 1 END,
        CASE sentiment WHEN 'negative' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END,
        fetched_at DESC""",
        params
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["categories"] = json.loads(d["categories"] or "[]")
        result.append(d)
    return result


def build_reviews_export_csv(restaurant_id: int) -> str:
    """The self-serve "export my data" setting's payload — deliberately
    narrower than admin's export_reviews() (9 columns, browser download):
    just the 4 fields a restaurant owner would actually recognize as "my
    review data" (date, rating, review text, response status), sized for
    an email attachment rather than a full admin audit export."""
    import csv, io
    rows = get_reviews_data(restaurant_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Rating", "Review", "Response Status"])
    for r in rows:
        writer.writerow([r.get("review_date") or "", r.get("rating"), r.get("text") or "", r.get("response_status") or ""])
    return buf.getvalue()


def get_response_performance(restaurant_id: int, days: int = 90, db_path: str = DB_PATH) -> dict:
    """Return approved-as-is / edited / regenerated counts for the given window."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT response_action, COUNT(*) as cnt
        FROM reviews
        WHERE restaurant_id=?
          AND response_action IS NOT NULL
          AND approved_at >= datetime('now', '-' || ? || ' days')
        GROUP BY response_action
    """, (restaurant_id, str(days))).fetchall()
    conn.close()
    counts = {"approved_as_is": 0, "edited": 0, "regenerated": 0}
    for r in rows:
        if r["response_action"] in counts:
            counts[r["response_action"]] = r["cnt"]
    total = sum(counts.values())
    return {"total": total, "days": days, **counts}


def get_changelog(since: str = None, db_path: str = DB_PATH) -> list:
    """Return published changelog entries, newest first. since= ISO datetime to only get unread."""
    conn = get_conn(db_path)
    if since:
        rows = conn.execute(
            "SELECT * FROM changelog_entries WHERE is_published=1 AND published_at > ? ORDER BY published_at DESC",
            (since,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM changelog_entries WHERE is_published=1 ORDER BY published_at DESC LIMIT 30"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_changelog_entry(title: str, body: str, tag: str = "feature", db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO changelog_entries (title, body, tag) VALUES (?,?,?)",
        (title.strip(), (body or "").strip(), tag)
    )
    row_id = cur.lastrowid
    conn.commit(); conn.close()
    return row_id

def delete_changelog_entry(entry_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM changelog_entries WHERE id=?", (entry_id,))
    conn.commit(); conn.close()

def is_in_quiet_hours(restaurant_id: int, db_path: str = DB_PATH) -> bool:
    """Return True if the current Chicago time falls within the restaurant's quiet window."""
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT alert_quiet_start, alert_quiet_end FROM restaurants WHERE id=?",
        (restaurant_id,)
    ).fetchone()
    conn.close()
    if not row or not row["alert_quiet_start"] or not row["alert_quiet_end"]:
        return False
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        now = _dt.now(ZoneInfo("America/Chicago"))
        now_t = now.hour * 60 + now.minute
        def _hm(s):
            h, m = s.split(":"); return int(h)*60+int(m)
        start = _hm(row["alert_quiet_start"])
        end   = _hm(row["alert_quiet_end"])
        if start <= end:
            return start <= now_t < end
        else:  # crosses midnight
            return now_t >= start or now_t < end
    except Exception:
        return False

def count_alerts_today(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """Alerts sent so far in the RESTAURANT's own day.

    This compared against date('now'), which sqlite evaluates in UTC. For a
    Chicago restaurant that rolls over at 7pm local, so "max 5 alerts a day"
    was really five before dinner service and five more during it — the cap
    reset in the middle of the shift it existed to protect. fired_at is
    stored in UTC, so the local midnight is converted back to UTC to compare.
    """
    try:
        from time_utils import restaurant_now_by_id, restaurant_tz
        local_now = restaurant_now_by_id(restaurant_id)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=restaurant_tz(None))
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # Never let a timezone lookup turn into "no cap at all".
        since = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT COUNT(*) as c FROM alert_log WHERE restaurant_id=? AND fired_at >= ?",
        (restaurant_id, since)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def get_response_templates(restaurant_id: int, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT id, title, body, category, use_count, created_at FROM response_templates WHERE restaurant_id=? ORDER BY use_count DESC, created_at DESC",
        (restaurant_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def create_response_template(restaurant_id: int, title: str, body: str, category: str = "general", db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO response_templates (restaurant_id, title, body, category) VALUES (?,?,?,?)",
        (restaurant_id, title.strip(), body.strip(), category)
    )
    row_id = cur.lastrowid
    conn.commit(); conn.close()
    return row_id

def delete_response_template(template_id: int, restaurant_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM response_templates WHERE id=? AND restaurant_id=?", (template_id, restaurant_id))
    conn.commit(); conn.close()

def increment_template_use(template_id: int, restaurant_id: int, db_path: str = DB_PATH):
    """restaurant_id is required. It used to default to None with an
    unscoped fallback UPDATE — the one caller always passed it, so the
    fallback was a trap set for the next one rather than a feature."""
    conn = get_conn(db_path)
    conn.execute("UPDATE response_templates SET use_count=use_count+1 WHERE id=? AND restaurant_id=?",
                 (template_id, restaurant_id))
    conn.commit(); conn.close()


# ── Onboarding email tracking ─────────────────────────────────────────────────

ONBOARDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS onboarding_emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
    email_type      TEXT    NOT NULL,  -- 'day_2', 'day_7', 'day_30'
    sent_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(restaurant_id, email_type)
);
"""

def init_onboarding_emails(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.executescript(ONBOARDING_SCHEMA)
    conn.commit()
    conn.close()

def get_onboarding_sent(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """Return list of email_types already sent to this restaurant."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT email_type FROM onboarding_emails WHERE restaurant_id=?",
        (restaurant_id,)
    ).fetchall()
    conn.close()
    return [r["email_type"] for r in rows]

def mark_onboarding_sent(restaurant_id: int, email_type: str, db_path: str = DB_PATH):
    """Record that an onboarding email was sent. UNIQUE constraint prevents duplicates."""
    try:
        conn = get_conn(db_path)
        conn.execute(
            "INSERT OR IGNORE INTO onboarding_emails (restaurant_id, email_type) VALUES (?,?)",
            (restaurant_id, email_type)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"mark_onboarding_sent error: {e}")

def get_review_request_stats(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Return count of review requests sent this month."""
    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT
            COUNT(*) AS total_sent,
            SUM(sent_at >= date('now','start of month')) AS sent_this_month
        FROM review_requests WHERE restaurant_id=?
    """, (restaurant_id,)).fetchone()
    conn.close()
    return {
        "total_sent":      row["total_sent"]      or 0,
        "sent_this_month": row["sent_this_month"] or 0,
    }


# ═══════════════════════════════════════════════════════════════════════
# Settings audit additions — account activity, AI-visibility history,
# data retention, auto-approve, wider exports
# ═══════════════════════════════════════════════════════════════════════

# The event types the user-facing Account activity log shows. activity_log
# also records every tab view (log_activity) and analytics-style events;
# those are admin telemetry, not "what changed on my account".
ACCOUNT_EVENT_TYPES = (
    "login", "password_changed", "email_changed", "recovery_email_set", "recovery_email_removed",
    "two_fa_enabled", "two_fa_disabled", "backup_codes_regenerated",
    "team_member_invited", "team_member_revoked",
    "sessions_revoked_others", "trusted_device_revoked", "trusted_devices_cleared",
    "login_reported_not_me", "data_exported", "alert_settings_saved",
    "login_notify_changed", "marketing_emails_changed", "auto_approve_changed",
    "hours_changed", "data_retention_changed", "profile_updated",
    "pos_connected", "pos_disconnected", "supplier_order_sent", "schedule_published",
)

ACCOUNT_EVENT_LABELS = {
    "login": "Signed in",
    "password_changed": "Password changed",
    "email_changed": "Email address changed",
    "recovery_email_set": "Recovery email set",
    "recovery_email_removed": "Recovery email removed",
    "two_fa_enabled": "Two-factor turned on",
    "two_fa_disabled": "Two-factor turned off",
    "backup_codes_regenerated": "Backup codes regenerated",
    "team_member_invited": "Team member invited",
    "team_member_revoked": "Team member removed",
    "sessions_revoked_others": "Signed out of other devices",
    "trusted_device_revoked": "Trusted device removed",
    "trusted_devices_cleared": "All trusted devices removed",
    "login_reported_not_me": "Sign-in reported as not you",
    "data_exported": "Data export emailed",
    "alert_settings_saved": "Alert settings saved",
    "login_notify_changed": "Sign-in notifications changed",
    "marketing_emails_changed": "Product update emails changed",
    "auto_approve_changed": "Auto-approve rule changed",
    "hours_changed": "Hours updated",
    "data_retention_changed": "Data retention changed",
    "profile_updated": "Profile updated",
    # `detail` carries which POS ("Square" / "Clover" / "Toast").
    "pos_connected": "POS connected",
    "pos_disconnected": "POS disconnected",
    "supplier_order_sent": "Supplier order sent",
    "schedule_published": "Schedule sent to staff",
}


def get_account_activity(restaurant_id: int, limit: int = 100, db_path: str = DB_PATH) -> list:
    """Account-level events only (see ACCOUNT_EVENT_TYPES), newest first."""
    import json as _json
    conn = get_conn(db_path)
    marks = ",".join("?" for _ in ACCOUNT_EVENT_TYPES)
    rows = conn.execute(f"""
        SELECT event_type, event_data, created_at FROM activity_log
        WHERE restaurant_id=? AND event_type IN ({marks})
        ORDER BY created_at DESC, id DESC LIMIT ?
    """, (restaurant_id, *ACCOUNT_EVENT_TYPES, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            data = _json.loads(r["event_data"] or "{}")
        except Exception:
            data = {}
        out.append({
            "type": r["event_type"],
            "label": ACCOUNT_EVENT_LABELS.get(r["event_type"], r["event_type"].replace("_", " ").capitalize()),
            "detail": data.get("detail"),
            "actor": data.get("actor"),
            "created_at": r["created_at"],
        })
    return out


def init_competitor_snapshots(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS competitor_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        place_id      TEXT    NOT NULL,
        name          TEXT,
        rating        REAL,
        review_count  INTEGER,
        price_level   INTEGER,
        match_basis   TEXT,
        captured_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_compsnap_rest_time "
                 "ON competitor_snapshots(restaurant_id, captured_at)")
    conn.commit()
    conn.close()


def record_competitor_snapshot(restaurant_id: int, competitors: list, db_path: str = DB_PATH):
    """One row per competitor per run.

    competitor_intel is a single JSON blob overwritten on every run, so the
    module could describe the competitive landscape today and nothing about
    how it changed. A rating sliding from 4.6 to 4.1 over two months is the
    most useful thing this module could tell an owner, and it was being
    thrown away every Monday.
    """
    init_competitor_snapshots(db_path)
    conn = get_conn(db_path)
    try:
        for c in competitors or []:
            if not c.get("place_id"):
                continue
            conn.execute(
                "INSERT INTO competitor_snapshots (restaurant_id, place_id, name, rating, "
                "review_count, price_level, match_basis) VALUES (?,?,?,?,?,?,?)",
                (restaurant_id, c["place_id"], c.get("name"), c.get("rating"),
                 c.get("review_count"), c.get("price_level"), c.get("match_basis")))
        conn.commit()
    finally:
        conn.close()


# Spread of restaurant star ratings on a 1-5 scale, skewed hard to 4 and 5.
# A stated assumption, not something estimated from data we do not hold.
_RATING_SIGMA = 1.1
# Two standard errors — the ordinary bar for "more than noise".
_RATING_SIGNIFICANT_Z = 2.0


def _least_squares_slope(snaps: list) -> float:
    """Rating points per 30 days across every snapshot, signed.

    Endpoints alone made a dip-and-recover look like no change and a single
    anomalous reading at a window edge look like the trend.
    """
    from datetime import datetime as _d
    pts = []
    for s_ in snaps:
        try:
            t = _d.strptime(str(s_["captured_at"])[:19], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        pts.append((t, float(s_["rating"])))
    if len(pts) < 2:
        return 0.0
    t0 = pts[0][0]
    xs = [(t - t0).total_seconds() / 86400.0 for t, _v in pts]
    ys = [v for _t, v in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom) * 30.0


def competitor_roster_changes(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Who appeared in, or dropped out of, the competitor set between the
    last two runs.

    The snapshot set changed silently every week. A restaurant opening
    nearby, or a rival closing, is the most actionable market-change signal
    this module can produce and nothing computed it.
    """
    init_competitor_snapshots(db_path)
    conn = get_conn(db_path)
    runs = conn.execute("""
        SELECT DISTINCT DATE(captured_at) AS d FROM competitor_snapshots
        WHERE restaurant_id=? ORDER BY d DESC LIMIT 2
    """, (restaurant_id,)).fetchall()
    if len(runs) < 2:
        conn.close()
        return {"ok": False, "reason": "needs two runs to compare",
                "arrived": [], "gone": []}
    now_d, prev_d = runs[0]["d"], runs[1]["d"]

    def _set(day):
        return {r["place_id"]: r["name"] for r in conn.execute(
            "SELECT place_id, name FROM competitor_snapshots "
            "WHERE restaurant_id=? AND DATE(captured_at)=?", (restaurant_id, day))}
    now, prev = _set(now_d), _set(prev_d)
    conn.close()
    return {
        "ok": True,
        "compared_from": prev_d,
        "compared_to": now_d,
        "arrived": [{"place_id": k, "name": v} for k, v in now.items() if k not in prev],
        "gone": [{"place_id": k, "name": v} for k, v in prev.items() if k not in now],
    }


def competitor_movement(restaurant_id: int, days: int = 60, db_path: str = DB_PATH) -> list:
    """How each competitor's rating and review count have moved.

    Returns one entry per competitor with its earliest and latest snapshot
    inside the window, and only where both exist — a single data point is
    not a movement and is reported as such by being absent.
    """
    init_competitor_snapshots(db_path)
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT place_id, name, rating, review_count, captured_at
        FROM competitor_snapshots
        WHERE restaurant_id=? AND captured_at >= datetime('now', ?)
        ORDER BY captured_at ASC
    """, (restaurant_id, f"-{int(days)} days")).fetchall()
    conn.close()
    by_place = {}
    for r in rows:
        by_place.setdefault(r["place_id"], []).append(dict(r))
    out = []
    for pid, snaps in by_place.items():
        if len(snaps) < 2:
            continue
        rated = [x for x in snaps if x["rating"] is not None]
        if len(rated) < 2:
            continue
        first, last = rated[0], rated[-1]
        change = last["rating"] - first["rating"]

        # A rating change is only a signal relative to the volume behind it.
        # Sorting on the raw change put a twelve-review venue moving 0.4 on
        # two reviews above a 2,400-review venue moving 0.1 on a hundred and
        # sixty — measured, and the second is by far the bigger thing to
        # happen in that market.
        #
        # Standard error of a mean rating is about sigma/sqrt(n). Restaurant
        # ratings sit on a 1-5 scale skewed hard to 4 and 5; sigma near 1.1
        # is the usual empirical figure and is used as a fixed, stated
        # assumption rather than estimated from data we do not hold.
        n_then = max(int(first["review_count"] or 0), 1)
        n_now = max(int(last["review_count"] or 0), 1)
        se = _RATING_SIGMA * math.sqrt(1.0 / n_then + 1.0 / n_now)
        z = abs(change) / se if se > 0 else 0.0

        # Google prunes reviews, so the count can fall. That is a correction
        # on their side, not a competitor losing reviews, and reporting it
        # as a negative gain reads as decline.
        raw_delta = (last["review_count"] or 0) - (first["review_count"] or 0)
        reviews_added = max(0, raw_delta)
        reviews_removed = max(0, -raw_delta)

        # Direction across every snapshot, not just the two endpoints. A
        # competitor that dipped and recovered showed no change; one
        # anomalous reading at a window edge WAS the change.
        slope = _least_squares_slope(rated)

        out.append({
            "place_id": pid,
            "name": last["name"],
            "rating_then": round(first["rating"], 2),
            "rating_now": round(last["rating"], 2),
            "rating_change": round(change, 2),
            # Points per 30 days across all snapshots, signed.
            "rating_trend_per_month": round(slope, 3),
            "reviews_then": first["review_count"],
            "reviews_now": last["review_count"],
            "reviews_added": reviews_added,
            "reviews_removed": reviews_removed,
            # How far the move is beyond what this review volume could
            # produce on its own. Below 2 is noise.
            "confidence_z": round(z, 2),
            "significant": bool(z >= _RATING_SIGNIFICANT_Z),
            "first_seen": first["captured_at"],
            "last_seen": last["captured_at"],
            "snapshots": len(rated),
        })
    # Significant movement first, then by how far past noise it sits.
    out.sort(key=lambda d: (d["significant"], d["confidence_z"]), reverse=True)
    return out


def init_ai_visibility_queries(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS ai_visibility_query_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id        INTEGER NOT NULL,
        restaurant_id INTEGER NOT NULL,
        query         TEXT    NOT NULL,
        query_kind    TEXT,
        appeared      INTEGER NOT NULL DEFAULT 0,
        answer        TEXT,
        sources       TEXT,
        competitors_named TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aivq_run ON ai_visibility_query_runs(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aivq_rest ON ai_visibility_query_runs(restaurant_id, created_at)")
    conn.commit()
    conn.close()


def record_ai_visibility_queries(run_id: int, restaurant_id: int, queries: list,
                                 db_path: str = DB_PATH):
    """Persist what a run actually asked and what came back.

    ai_visibility_runs stored a score and nothing else, so when the number
    moved nothing could say why — while the drop alert told the owner to
    "open Intel to see which questions changed". The questions had never
    been written down. Citations were fetched on every run and thrown away
    with them.
    """
    if not run_id or not queries:
        return
    init_ai_visibility_queries(db_path)
    import json as _j
    conn = get_conn(db_path)
    try:
        for q in queries:
            conn.execute(
                "INSERT INTO ai_visibility_query_runs (run_id, restaurant_id, query, "
                "query_kind, appeared, answer, sources, competitors_named) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, restaurant_id, q.get("query", ""), q.get("kind"),
                 1 if q.get("appeared") else 0, (q.get("answer") or "")[:2000],
                 _j.dumps(q.get("sources") or []),
                 _j.dumps(q.get("competitors_named") or [])))
        conn.commit()
    finally:
        conn.close()


def ai_visibility_query_diff(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Which questions changed between the last two complete runs.

    This is what the drop alert has always pointed the owner at and what
    nothing could produce.
    """
    init_ai_visibility_queries(db_path)
    conn = get_conn(db_path)
    runs = conn.execute(
        "SELECT DISTINCT run_id, MAX(created_at) AS at FROM ai_visibility_query_runs "
        "WHERE restaurant_id=? GROUP BY run_id ORDER BY at DESC LIMIT 2",
        (restaurant_id,)).fetchall()
    if len(runs) < 2:
        conn.close()
        return {"ok": False, "reason": "needs two completed checks to compare"}
    now_id, prev_id = runs[0]["run_id"], runs[1]["run_id"]

    def _rows(rid):
        return {r["query"]: bool(r["appeared"]) for r in conn.execute(
            "SELECT query, appeared FROM ai_visibility_query_runs WHERE run_id=?", (rid,))}
    now, prev = _rows(now_id), _rows(prev_id)
    conn.close()
    lost = sorted(q for q in now if prev.get(q) and not now[q])
    gained = sorted(q for q in now if now[q] and not prev.get(q))
    held = sorted(q for q in now if now[q] and prev.get(q))
    return {"ok": True, "lost": lost, "gained": gained, "held": held,
            "compared": len(set(now) & set(prev))}


def ai_visibility_sources(restaurant_id: int, limit: int = 20, db_path: str = DB_PATH) -> list:
    """The citation URLs the most recent run's answers were grounded in."""
    init_ai_visibility_queries(db_path)
    import json as _j
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT run_id FROM ai_visibility_query_runs WHERE restaurant_id=? "
        "ORDER BY created_at DESC LIMIT 1", (restaurant_id,)).fetchone()
    if not row:
        conn.close()
        return []
    rows = conn.execute(
        "SELECT sources FROM ai_visibility_query_runs WHERE run_id=?", (row["run_id"],)).fetchall()
    conn.close()
    out, seen = [], set()
    for r in rows:
        try:
            for u in _j.loads(r["sources"] or "[]"):
                if u and u not in seen:
                    seen.add(u)
                    out.append(u)
        except Exception:
            continue
    return out[:limit]


def record_ai_visibility_run(restaurant_id: int, ai_score: int, gbp_score: int = None,
                             answered: int = None, appeared: int = None,
                             db_path: str = DB_PATH):
    """Record one complete visibility run.

    answered/appeared are stored so a later comparison can tell a real
    change from a difference in sample size, and so the drop alert can
    refuse to fire on a sample too small to say anything.
    """
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO ai_visibility_runs (restaurant_id, ai_score, gbp_score, answered, appeared) "
            "VALUES (?,?,?,?,?)",
            (restaurant_id, ai_score, gbp_score, answered, appeared))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def last_two_ai_visibility_runs(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """The two most recent complete runs, newest first, with their samples."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT ai_score, answered, appeared, created_at
        FROM ai_visibility_runs
        WHERE restaurant_id=? AND ai_score IS NOT NULL
        ORDER BY created_at DESC, id DESC LIMIT 2
    """, (restaurant_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def last_two_ai_visibility_scores(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """Scores only. Kept for callers that just want the numbers."""
    return [r["ai_score"] for r in last_two_ai_visibility_runs(restaurant_id, db_path)]


def purge_expired_reviews(db_path: str = DB_PATH) -> int:
    """Soft-deletes reviews older than each restaurant's data_retention_months
    (0 = keep everything). Soft, not hard — every reviews query already
    filters deleted_at IS NULL, and a mistaken retention setting shouldn't
    be unrecoverable. Returns the number of rows touched."""
    conn = get_conn(db_path)
    total = 0
    try:
        rows = conn.execute(
            "SELECT id, data_retention_months FROM restaurants WHERE COALESCE(data_retention_months, 0) > 0"
        ).fetchall()
        for r in rows:
            months = int(r["data_retention_months"])
            cur = conn.execute(f"""
                UPDATE reviews SET deleted_at = datetime('now')
                WHERE restaurant_id=? AND deleted_at IS NULL
                  AND COALESCE(review_date, fetched_at) < datetime('now', '-{months * 30} days')
            """, (r["id"],))
            total += cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return total


def count_auto_approved_today(restaurant_id: int, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT COUNT(*) AS n FROM activity_log
        WHERE restaurant_id=? AND event_type='review_auto_approved'
          AND created_at >= date('now', 'localtime')
    """, (restaurant_id,)).fetchone()
    conn.close()
    return int(row["n"]) if row else 0


def auto_approve_candidates(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """Drafted, unapproved 5-star reviews — the only thing the auto-approve
    rule is ever allowed to touch.

    urgency is part of the gate now. A five-star rating says nothing about
    the text: a review can hand out five stars and still mention an allergic
    reaction or name a staff member, and the analyser flags exactly those as
    high urgency. Publishing a reply to one of those without a human reading
    it is the case this rule must never cover.

    Rows returned carry their draft so the caller can inspect the text before
    publishing it — see ai_guard.check_public_reply.
    """
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT id, draft_response FROM reviews
        WHERE restaurant_id=? AND rating=5 AND response_status='drafted'
          AND draft_response IS NOT NULL AND deleted_at IS NULL
          AND COALESCE(urgency, 'normal') != 'high'
          -- A draft flagged for stating an action the restaurant may not
          -- have taken is exactly what must not be published unread.
          AND COALESCE(draft_needs_review, 0) = 0
        ORDER BY fetched_at ASC
    """, (restaurant_id,)).fetchall()
    conn.close()
    return [{"id": r["id"], "draft_response": r["draft_response"]} for r in rows]


def build_labor_export_csv(restaurant_id: int, db_path: str = DB_PATH) -> str:
    import csv, io
    conn = get_conn(db_path)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["week_start", "hours_scheduled", "labor_cost", "labor_pct", "generated_at"])
    try:
        rows = conn.execute("""
            SELECT * FROM labor_history WHERE restaurant_id=? ORDER BY created_at DESC LIMIT 520
        """, (restaurant_id,)).fetchall()
        for r in rows:
            k = r.keys()
            w.writerow([
                r["week_start"] if "week_start" in k else "",
                r["hours_scheduled"] if "hours_scheduled" in k else "",
                r["labor_cost"] if "labor_cost" in k else "",
                r["labor_pct"] if "labor_pct" in k else "",
                r["created_at"] if "created_at" in k else "",
            ])
    except Exception as e:
        w.writerow([f"labor history unavailable: {e}"])
    finally:
        conn.close()
    return out.getvalue()


def build_food_cost_export_csv(restaurant_id: int, db_path: str = DB_PATH) -> str:
    import csv, io
    conn = get_conn(db_path)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ingredient", "unit", "on_hand", "par", "unit_cost", "last_order_qty", "waste_last_week", "updated_at"])
    try:
        rows = conn.execute("SELECT * FROM ingredients WHERE restaurant_id=? ORDER BY name", (restaurant_id,)).fetchall()
        for r in rows:
            k = r.keys()
            def g(col): return r[col] if col in k else ""
            w.writerow([g("name"), g("unit"), g("on_hand"), g("par"), g("unit_cost"),
                        g("last_order_qty"), g("waste_last_week"), g("updated_at")])
    except Exception as e:
        w.writerow([f"food cost data unavailable: {e}"])
    finally:
        conn.close()
    return out.getvalue()


def build_settings_export_json(restaurant_id: int, db_path: str = DB_PATH) -> str:
    """Every self-serve setting, minus anything secret (tokens, codes)."""
    import json as _json
    r = get_restaurant(restaurant_id, db_path)
    if not r:
        return "{}"
    keep = [
        "name", "location_name", "owner_name", "owner_email", "owner_phone", "timezone",
        "neighborhood", "vibe", "known_for", "voice_notes", "never_say", "menu_notes", "sign_off_name",
        "response_language", "tone_preset", "open_times_json", "close_times_json", "skip_holidays",
        "digest_day", "digest_enabled", "login_notify", "marketing_emails_opt_out",
        "alert_1star", "alert_2star", "alert_health", "alert_neg_spike", "alert_negative_trend",
        "alert_no_response", "alert_5star", "alert_labor_over", "alert_food_waste", "alert_ai_visibility_drop",
        "alert_health_bypass_quiet", "alert_extra_emails", "push_sound", "urgent_via_email", "urgent_via_sms",
        "alert_quiet_start", "alert_quiet_end", "auto_approve_5star", "auto_approve_daily_cap",
        "auto_approve_paused", "data_retention_months", "two_fa_enabled", "two_fa_method",
    ]
    return _json.dumps({k: getattr(r, k, None) for k in keep}, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════
# Analytics chart feeds (Sentiment River / Topic Heat Grid / Labor Ribbon /
# Visibility Orbit)
# ═══════════════════════════════════════════════════════════════════════

TOPIC_LABELS = {
    "food_quality": "Food Quality", "service": "Service", "wait_time": "Wait Time", "value": "Value",
    "ambience": "Ambience", "cleanliness": "Cleanliness", "portion": "Portions", "other": "Other",
}


def get_topic_weeks(restaurant_id: int, weeks: int = 8, db_path: str = DB_PATH) -> list:
    """Per category, per ISO week: positive / negative / total mentions for
    the last `weeks` weeks (oldest first). Feeds the Topic Heat Grid — the
    existing get_topic_heatmap() only has period totals."""
    import json as _json
    from collections import defaultdict
    from datetime import datetime, timedelta
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT categories, sentiment, COALESCE(review_date, fetched_at) AS d FROM reviews
        WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL
          AND categories IS NOT NULL AND categories != '[]'
          AND COALESCE(review_date, fetched_at) >= datetime('now', ?)
    """, (restaurant_id, f"-{weeks * 7} days")).fetchall()
    conn.close()
    # Week buckets, oldest first, keyed by ISO year-week.
    today = datetime.utcnow().date()
    keys = []
    for i in range(weeks - 1, -1, -1):
        d = today - timedelta(days=7 * i)
        y, w, _ = d.isocalendar()
        keys.append(f"{y}-W{w:02d}")
    idx = {k: i for i, k in enumerate(keys)}
    grid = defaultdict(lambda: [{"positive": 0, "negative": 0, "total": 0} for _ in keys])
    for r in rows:
        try:
            d = datetime.strptime(str(r["d"])[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        y, w, _ = d.isocalendar()
        k = f"{y}-W{w:02d}"
        if k not in idx:
            continue
        s = r["sentiment"] or "neutral"
        try:
            cats = _json.loads(r["categories"] or "[]")
        except Exception:
            cats = []
        for c in cats:
            if not c:
                continue
            cell = grid[c][idx[k]]
            cell["total"] += 1
            if s == "positive": cell["positive"] += 1
            elif s == "negative": cell["negative"] += 1
    out = []
    for cat, cells in grid.items():
        total = sum(c["total"] for c in cells)
        if total == 0:
            continue
        out.append({"category": cat, "label": TOPIC_LABELS.get(cat, cat.replace("_", " ").title()),
                    "total": total, "weeks": cells})
    out.sort(key=lambda x: -x["total"])
    # Label each column by the Monday that starts its week ("8/26", "9/2").
    labels = []
    for i in range(weeks - 1, -1, -1):
        d = today - timedelta(days=7 * i)
        monday = d - timedelta(days=d.weekday())
        labels.append(f"{monday.month}/{monday.day}")
    return {"week_labels": labels, "topics": out[:6]}


def get_labor_daily(restaurant_id: int, days: int = 14, db_path: str = DB_PATH) -> list:
    """Last `days` rows of labor_daily_history, oldest first — the Labor
    Ribbon's daily labor-% spline."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT date, day_of_week, labor_pct, labor_cost, sales, total_hours
        FROM labor_daily_history
        WHERE restaurant_id=? AND labor_pct IS NOT NULL
        ORDER BY date DESC LIMIT ?
    """, (restaurant_id, days)).fetchall()
    conn.close()
    return [dict(r) for r in rows][::-1]


def get_ai_visibility_history(restaurant_id: int, limit: int = 10, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT ai_score, gbp_score, created_at FROM ai_visibility_runs
        WHERE restaurant_id=? AND ai_score IS NOT NULL
        ORDER BY created_at DESC, id DESC LIMIT ?
    """, (restaurant_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows][::-1]


# ── Purchase orders ────────────────────────────────────────────────────────────

def next_po_number(restaurant_id: int, db_path: str = DB_PATH) -> str:
    """Sequential per restaurant — PO-0001, PO-0002... Derived from the
    count of existing rows rather than a global autoincrement so two
    restaurants never see each other's numbering, and so the number a
    supplier sees is small and human-quotable."""
    conn = get_conn(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM purchase_orders WHERE restaurant_id=?", (restaurant_id,)
        ).fetchone()[0] or 0
    finally:
        conn.close()
    return f"PO-{n + 1:04d}"


def record_purchase_order(restaurant_id: int, po_number: str, supplier_name: str,
                          supplier_email: str, items: list, total_cost: float,
                          db_path: str = DB_PATH) -> int:
    """Store what was actually sent, so receiving can pre-fill from it."""
    import json as _json
    conn = get_conn(db_path)
    try:
        cur = conn.execute("""
            INSERT INTO purchase_orders
                (restaurant_id, po_number, supplier_name, supplier_email, items_json, total_cost)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (restaurant_id, po_number, supplier_name, supplier_email,
              _json.dumps(items or []), round(float(total_cost or 0), 2)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_purchase_orders(restaurant_id: int, limit: int = 25, status: str = None,
                        db_path: str = DB_PATH) -> list:
    """Newest first. `status` filters to 'sent' (still outstanding) or
    'received'."""
    import json as _json
    conn = get_conn(db_path)
    try:
        sql = "SELECT * FROM purchase_orders WHERE restaurant_id=?"
        params = [restaurant_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY sent_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            items = _json.loads(r["items_json"] or "[]")
        except Exception:
            items = []
        out.append({
            "id": r["id"], "po_number": r["po_number"],
            "supplier_name": r["supplier_name"] or "", "supplier_email": r["supplier_email"] or "",
            "items": items, "total_cost": r["total_cost"] or 0,
            "status": r["status"], "sent_at": r["sent_at"], "received_at": r["received_at"],
        })
    return out


def mark_purchase_order_received(restaurant_id: int, po_id: int, db_path: str = DB_PATH) -> bool:
    """Scoped by restaurant_id so one restaurant can never close another's
    order. Returns False if the id doesn't belong to this restaurant."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("""
            UPDATE purchase_orders SET status='received', received_at=datetime('now')
            WHERE id=? AND restaurant_id=? AND status='sent'
        """, (po_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Staff contacts & schedule sharing ──────────────────────────────────────────

def get_staff_contacts(restaurant_id: int, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, employee_name, email, phone FROM staff_contacts "
            "WHERE restaurant_id=? ORDER BY employee_name", (restaurant_id,)
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "employee_name": r["employee_name"],
             "email": r["email"] or "", "phone": r["phone"] or ""} for r in rows]


def set_staff_contact(restaurant_id: int, employee_name: str, email: str = None,
                      phone: str = None, db_path: str = DB_PATH) -> bool:
    """Upsert by (restaurant, employee name) — the same key
    staff_availability and staff_notes use."""
    name = (employee_name or "").strip()
    if not name:
        return False
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO staff_contacts (restaurant_id, employee_name, email, phone)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(restaurant_id, employee_name)
            DO UPDATE SET email=excluded.email, phone=excluded.phone, updated_at=datetime('now')
        """, (restaurant_id, name, (email or "").strip() or None, (phone or "").strip() or None))
        conn.commit()
        return True
    finally:
        conn.close()


SCHEDULE_SHARE_TTL_DAYS = 60


def create_schedule_share(restaurant_id: int, schedule_id: int, employee_name: str,
                          sent_to: str = None, db_path: str = DB_PATH) -> str:
    """One tokenised link for one employee's view of one schedule.

    Re-publishing the same schedule to the same person reuses their existing
    token rather than minting a second one, so a link already sitting in
    someone's inbox never goes dead because the manager hit Publish twice.
    Re-publishing also pushes the expiry out again, since the link was just
    deliberately re-sent.

    Links expire after SCHEDULE_SHARE_TTL_DAYS. Without that they were
    permanent: someone who left the restaurant a year ago still had a working
    URL showing current staffing, and the only way to cut it off was deleting
    the row by hand.
    """
    import secrets as _secrets
    from datetime import datetime as _dt, timedelta as _td
    expires = (_dt.utcnow() + _td(days=SCHEDULE_SHARE_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT token FROM schedule_shares WHERE schedule_id=? AND employee_name=?",
            (schedule_id, employee_name)
        ).fetchone()
        if existing:
            conn.execute("UPDATE schedule_shares SET sent_at=datetime('now'), sent_to=?, expires_at=? "
                         "WHERE schedule_id=? AND employee_name=?",
                         (sent_to, expires, schedule_id, employee_name))
            conn.commit()
            return existing["token"]
        token = _secrets.token_urlsafe(24)
        conn.execute("""
            INSERT INTO schedule_shares (restaurant_id, schedule_id, employee_name, token, sent_to, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (restaurant_id, schedule_id, employee_name, token, sent_to, expires))
        conn.commit()
        return token
    finally:
        conn.close()


def get_schedule_share(token: str, db_path: str = DB_PATH) -> dict:
    """Resolve a public token to the schedule and employee it belongs to.
    Returns None for an unknown token — the public page then 404s rather
    than leaking whether a token ever existed."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT s.id, s.restaurant_id, s.schedule_id, s.employee_name, s.viewed_at, s.view_count,
                   s.expires_at,
                   h.week_start, h.week_end, h.schedule_csv, r.name AS restaurant_name
            FROM schedule_shares s
            JOIN schedule_history h ON h.id = s.schedule_id
            JOIN restaurants r ON r.id = s.restaurant_id
            WHERE s.token = ?
        """, (token,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    share = dict(row)
    # Distinguished from "no such token" so the page can say "this link has
    # expired, ask your manager to resend" instead of a bare 404 that reads
    # like the app is broken to someone who just wants their shifts.
    share["expired"] = _share_is_expired(share.get("expires_at"))
    return share


def _share_is_expired(expires_at) -> bool:
    """Rows created before expires_at existed have NULL and never expire —
    they predate the policy, and silently cutting off links already in
    people's inboxes would strand them mid-week."""
    if not expires_at:
        return False
    from datetime import datetime as _dt
    try:
        return _dt.utcnow() > _dt.fromisoformat(str(expires_at).replace("T", " "))
    except Exception:
        return False


def mark_schedule_share_viewed(token: str, db_path: str = DB_PATH):
    """First open stamps viewed_at; every open increments the count."""
    conn = get_conn(db_path)
    try:
        conn.execute("""
            UPDATE schedule_shares
            SET viewed_at = COALESCE(viewed_at, datetime('now')), view_count = view_count + 1
            WHERE token = ?
        """, (token,))
        conn.commit()
    finally:
        conn.close()


def get_schedule_share_status(restaurant_id: int, schedule_id: int, db_path: str = DB_PATH) -> list:
    """Who was sent this schedule and who has actually opened it."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT employee_name, sent_to, sent_at, viewed_at, view_count
            FROM schedule_shares WHERE restaurant_id=? AND schedule_id=?
            ORDER BY employee_name
        """, (restaurant_id, schedule_id)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── Email suppression list ──────────────────────────────────────────────────

def suppress_email(email: str, reason: str, detail: str = None, db_path: str = DB_PATH):
    """Stop sending to an address. Idempotent; the first reason wins so a
    later soft signal can't overwrite a hard bounce."""
    email = (email or "").strip().lower()
    if not email:
        return False
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO email_suppressions (email, reason, detail) VALUES (?,?,?)",
            (email, reason, (detail or "")[:500])
        )
        conn.commit()
    finally:
        conn.close()
    return True


def is_email_suppressed(email: str, db_path: str = DB_PATH) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT 1 FROM email_suppressions WHERE email=?", (email,)).fetchone()
    finally:
        conn.close()
    return bool(row)


def unsuppress_email(email: str, db_path: str = DB_PATH):
    """Manual reinstatement — a bounce can be a full mailbox that got emptied,
    or an address fixed after a typo."""
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM email_suppressions WHERE email=?", ((email or "").strip().lower(),))
        conn.commit()
    finally:
        conn.close()


def get_email_suppressions(limit: int = 200, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT email, reason, detail, created_at FROM email_suppressions "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def mark_email_delivery_event(message_id: str, status: str, detail: str = None, db_path: str = DB_PATH):
    """Reconcile a Resend webhook back onto the row we logged at send time."""
    if not message_id:
        return False
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE email_log SET status=?, error=COALESCE(?, error) WHERE message_id=?",
            (status, (detail or None), message_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Marketing unsubscribe tokens ────────────────────────────────────────────

def unsubscribe_token(restaurant_id: int) -> str:
    """Signed, stateless one-click unsubscribe token.

    Signed rather than stored so an old link in an old email never stops
    working, and HMAC'd so nobody can unsubscribe a restaurant by walking
    ids. Scoped to marketing only — it never touches security email.
    """
    import hmac, hashlib, base64, os as _os
    secret = (_os.getenv("SECRET_KEY") or _os.getenv("RESEND_API_KEY") or "cavnar-fallback").encode()
    sig = hmac.new(secret, f"unsub:{restaurant_id}".encode(), hashlib.sha256).digest()
    return f"{restaurant_id}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')[:24]}"


def verify_unsubscribe_token(token: str):
    """Return the restaurant_id a token authorises, or None."""
    import hmac as _hmac
    try:
        rid_str, _ = (token or "").split(".", 1)
        rid = int(rid_str)
    except Exception:
        return None
    return rid if _hmac.compare_digest(unsubscribe_token(rid), token or "") else None


# Human labels for email_log.email_type. The stored value is the sender
# function name — precise and greppable — but "send_onboarding_day2" is not
# what an owner should read in their own email history.
EMAIL_TYPE_LABELS = {
    "send_2fa_code":                  "Two-factor code",
    "send_login_notification":        "New sign-in alert",
    "send_password_reset_email":      "Password reset",
    "send_password_reset_code_email": "Password reset code",
    "send_password_changed_email":    "Password changed",
    "send_email_changed_email":       "Sign-in email changed",
    "send_recovery_email_code":       "Recovery email code",
    "send_signup_welcome_email":      "Welcome",
    "send_welcome_email":             "Dashboard access",
    "send_team_invite_email":         "Team invite",
    "send_payment_email":             "Payment link",
    "send_payment_failed_client_email": "Payment issue",
    "send_staff_schedule_email":      "Staff schedule",
    "send_supplier_order_email":      "Supplier order",
    "send_onboarding_day2":           "Getting started",
    "send_onboarding_day7":           "One week in",
    "send_onboarding_day30":          "30-day check-in",
    "send_monthly_summary_email":     "Monthly summary",
    "send_reactivation_email":        "Welcome back",
    "send_bug_report_email":          "Bug report",
    "send_signup_admin_alert":        "New signup (internal)",
}


def email_type_label(email_type: str) -> str:
    """Falls back to the raw value so an untyped or newly added sender still
    shows something rather than blank."""
    if not email_type:
        return "Email"
    return EMAIL_TYPE_LABELS.get(email_type, email_type.replace("send_", "").replace("_", " ").strip().capitalize())


# ── Ask Cavnar conversation + action log ────────────────────────────────────

_ASK_HISTORY_LIMIT = 40


# Transcripts are conveniences, not records — the action audit in
# ask_cavnar_actions is the part that must survive, and it is never pruned.
# Both caps are per restaurant: turns kept within one chat, and chats kept
# in the history list (oldest chats fall off, with their messages).
_ASK_TRANSCRIPT_KEEP = 200
_ASK_CONVERSATIONS_KEEP = 50
_ASK_TITLE_MAX = 80


def _adopt_legacy_ask_messages(conn):
    """One-time: messages written before chats existed have no
    conversation_id. Gather each restaurant's orphans into a single chat so
    nothing an owner already said disappears from their history."""
    orphans = conn.execute(
        "SELECT restaurant_id, MIN(user_id) AS user_id, MIN(created_at) AS first_at, "
        "MAX(created_at) AS last_at FROM ask_cavnar_messages "
        "WHERE conversation_id IS NULL GROUP BY restaurant_id"
    ).fetchall()
    for r in orphans:
        rid = r["restaurant_id"]
        first = conn.execute(
            "SELECT content FROM ask_cavnar_messages WHERE restaurant_id=? AND conversation_id IS NULL "
            "AND role='user' ORDER BY id ASC LIMIT 1", (rid,)
        ).fetchone()
        title = _ask_title(first["content"]) if first else "Earlier conversation"
        cur = conn.execute(
            "INSERT INTO ask_cavnar_conversations (restaurant_id, user_id, title, created_at, updated_at) "
            "VALUES (?,?,?,?,?)", (rid, r["user_id"], title, r["first_at"], r["last_at"])
        )
        conn.execute(
            "UPDATE ask_cavnar_messages SET conversation_id=? WHERE restaurant_id=? AND conversation_id IS NULL",
            (cur.lastrowid, rid)
        )
    conn.commit()


def _ask_title(question: str) -> str:
    """A chat is named by its first question — one line, trimmed. A
    "[Confirmed: …]" audit line is never a title."""
    text = " ".join((question or "").split())
    if not text or text.startswith("[Confirmed:") or text.startswith("[Dismissed:"):
        return "New conversation"
    if len(text) > _ASK_TITLE_MAX:
        text = text[:_ASK_TITLE_MAX - 1].rstrip() + "…"
    return text


def create_ask_conversation(restaurant_id, user_id=None, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO ask_cavnar_conversations (restaurant_id, user_id) VALUES (?,?)",
            (restaurant_id, user_id)
        )
        # Keep the history list bounded — the oldest chats fall off with
        # their messages. The action audit is untouched.
        stale = conn.execute(
            "SELECT id FROM ask_cavnar_conversations WHERE restaurant_id=? "
            "ORDER BY updated_at DESC, id DESC LIMIT -1 OFFSET ?",
            (restaurant_id, _ASK_CONVERSATIONS_KEEP)
        ).fetchall()
        for s in stale:
            conn.execute("DELETE FROM ask_cavnar_messages WHERE conversation_id=?", (s["id"],))
            conn.execute("DELETE FROM ask_cavnar_conversations WHERE id=?", (s["id"],))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()



# A conversation belongs to the restaurant AND to whoever started it. Passing
# viewer_id restricts a read to that person's own chats plus the ones that
# predate the user_id column (NULL), so an invited teammate can't page through
# the owner's assistant history — which carries labor cost, food cost and
# revenue in plain text. Omitted (None) means no viewer filter: that is what
# the data layer's own tests and any restaurant-wide maintenance use.
def _viewer_clause(viewer_id, alias=""):
    if viewer_id is None:
        return "", []
    col = f"{alias}user_id" if alias else "user_id"
    return f" AND ({col}=? OR {col} IS NULL)", [viewer_id]


def get_ask_conversation(restaurant_id, conversation_id, db_path: str = DB_PATH, viewer_id=None):
    """The conversation row, or None when it doesn't exist, belongs to
    another restaurant, or belongs to another person — the caller never
    learns which."""
    if conversation_id is None:
        return None
    where, params = _viewer_clause(viewer_id)
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, restaurant_id, user_id, title, created_at, updated_at "
            "FROM ask_cavnar_conversations WHERE id=? AND restaurant_id=?" + where,
            [conversation_id, restaurant_id, *params]
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def current_ask_conversation_id(restaurant_id, db_path: str = DB_PATH, viewer_id=None):
    """The most recently active chat — what a client that hasn't picked a
    specific conversation (the web panel) is talking in. None if there are
    no chats yet."""
    where, params = _viewer_clause(viewer_id)
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM ask_cavnar_conversations WHERE restaurant_id=?" + where +
            " ORDER BY updated_at DESC, id DESC LIMIT 1", [restaurant_id, *params]
        ).fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


def list_ask_conversations(restaurant_id, limit: int = _ASK_CONVERSATIONS_KEEP,
                           db_path: str = DB_PATH, viewer_id=None) -> list:
    """Newest first. Each entry carries what a history row needs: the
    title, a preview of the last thing said, when, and how many turns."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT c.id, c.title, c.created_at, c.updated_at, "
            "  (SELECT COUNT(*) FROM ask_cavnar_messages m WHERE m.conversation_id=c.id) AS message_count, "
            "  (SELECT content FROM ask_cavnar_messages m WHERE m.conversation_id=c.id "
            "   ORDER BY m.id DESC LIMIT 1) AS preview "
            "FROM ask_cavnar_conversations c WHERE c.restaurant_id=?" + _viewer_clause(viewer_id, "c.")[0] +
            " ORDER BY c.updated_at DESC, c.id DESC LIMIT ?",
            [restaurant_id, *_viewer_clause(viewer_id, "c.")[1], limit]
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["title"] = d.get("title") or "New conversation"
        preview = " ".join((d.get("preview") or "").split())
        d["preview"] = preview[:140]
        out.append(d)
    return out


def delete_ask_conversation(restaurant_id, conversation_id, db_path: str = DB_PATH, viewer_id=None) -> bool:
    """Permanently removes one chat and its messages. Scoped to the
    restaurant, so a guessed id from another account deletes nothing. The
    action audit is never touched."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM ask_cavnar_conversations WHERE id=? AND restaurant_id=?" + _viewer_clause(viewer_id)[0],
            [conversation_id, restaurant_id, *_viewer_clause(viewer_id)[1]]
        )
        if cur.rowcount:
            conn.execute("DELETE FROM ask_cavnar_messages WHERE conversation_id=? AND restaurant_id=?",
                         (conversation_id, restaurant_id))
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def save_ask_message(restaurant_id, role, content, proposals=None, user_id=None,
                     conversation_id=None, db_path: str = DB_PATH) -> int:
    """Appends a turn and returns the conversation it landed in.

    With no conversation_id, the turn goes into the restaurant's current
    chat (creating the first one if none exists) — the web panel's
    behaviour. A specific id must belong to this restaurant."""
    import json as _json
    if conversation_id is not None and get_ask_conversation(restaurant_id, conversation_id, db_path=db_path) is None:
        raise ValueError("conversation not found")
    if conversation_id is None:
        # Deliberately unfiltered: this is the write path, and the turn
        # continues whatever chat the restaurant is currently in.
        conversation_id = current_ask_conversation_id(restaurant_id, db_path=db_path)
    if conversation_id is None:
        conversation_id = create_ask_conversation(restaurant_id, user_id=user_id, db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO ask_cavnar_messages (restaurant_id, user_id, role, content, proposals, conversation_id) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, user_id, role, content,
             _json.dumps(proposals) if proposals else None, conversation_id)
        )
        # The first real question names the chat; later turns only bump it
        # to the top of the history list.
        if role == "user":
            conn.execute(
                "UPDATE ask_cavnar_conversations SET title=? WHERE id=? AND (title IS NULL OR title='')",
                (_ask_title(content), conversation_id)
            )
        conn.execute(
            "UPDATE ask_cavnar_conversations SET updated_at=datetime('now') WHERE id=?",
            (conversation_id,)
        )
        # Trim as we go. Only the most recent turns are ever replayed, so an
        # unbounded chat would grow forever to hold rows nothing reads.
        conn.execute(
            "DELETE FROM ask_cavnar_messages WHERE conversation_id=? AND id NOT IN "
            "(SELECT id FROM ask_cavnar_messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?)",
            (conversation_id, conversation_id, _ASK_TRANSCRIPT_KEEP)
        )
        conn.commit()
    finally:
        conn.close()
    return conversation_id


def get_ask_history(restaurant_id, limit: int = _ASK_HISTORY_LIMIT, conversation_id=None, viewer_id=None,
                    db_path: str = DB_PATH) -> list:
    """Oldest-first, so it can be handed straight to the model. Without a
    conversation_id this is the restaurant's current chat; a specific id
    must belong to this restaurant (anything else reads as empty)."""
    import json as _json
    if conversation_id is None:
        conversation_id = current_ask_conversation_id(restaurant_id, db_path=db_path, viewer_id=viewer_id)
        if conversation_id is None:
            return []
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, role, content, proposals, created_at FROM ask_cavnar_messages "
            "WHERE restaurant_id=? AND conversation_id=?" + _viewer_clause(viewer_id)[0] +
            " ORDER BY id DESC LIMIT ?",
            [restaurant_id, conversation_id, *_viewer_clause(viewer_id)[1], limit]
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in reversed(rows):
        d = dict(r)
        if d.get("proposals"):
            try:
                d["proposals"] = _json.loads(d["proposals"])
            except Exception:
                d["proposals"] = None
        out.append(d)
    return out


def clear_ask_history(restaurant_id, db_path: str = DB_PATH):
    """Every chat, gone. The action audit stays."""
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM ask_cavnar_messages WHERE restaurant_id=?", (restaurant_id,))
        conn.execute("DELETE FROM ask_cavnar_conversations WHERE restaurant_id=?", (restaurant_id,))
        conn.commit()
    finally:
        conn.close()


def log_ask_action(restaurant_id, action, summary=None, body=None, outcome="proposed",
                   user_id=None, db_path: str = DB_PATH):
    """Audit trail for anything the assistant proposed.

    Written at proposal time and again at confirm/dismiss, so "did the
    assistant send that, and who approved it" is answerable after the fact.
    """
    import json as _json
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO ask_cavnar_actions (restaurant_id, user_id, action, summary, body, outcome) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, user_id, action, summary,
             _json.dumps(body) if body else None, outcome)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_ask_actions(restaurant_id, limit: int = 50, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, action, summary, outcome, created_at FROM ask_cavnar_actions "
            "WHERE restaurant_id=? ORDER BY id DESC LIMIT ?", (restaurant_id, limit)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
