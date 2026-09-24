import os
import math
import sqlite3
import json
import threading
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# On Railway, RAILWAY_VOLUME_MOUNT_PATH points at the persistent volume
# (currently /app/data). Without this, reviews.db was written to the
# container's ephemeral filesystem and silently reset to empty on every
# deploy — masked because boot-time seed code in hosted_dashboard.py
# deterministically recreates the admin/demo accounts and reviews, so
# only sessions/login_history (which have no such reseed) visibly emptied.
DB_PATH = os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "reviews.db")

# ── The one time axis every review query uses ─────────────────────────────────
#
# When a GUEST wrote the review, falling back to when Cavnar pulled it only
# when the platform gave us nothing. There were three different answers to
# this question across the codebase — bare `review_date` in get_review_stats
# and home_brief, `fetched_at` in the weekly digest, and this COALESCE in the
# AI insight — so the same restaurant's "last 30 days" meant three different
# sets of reviews depending on which surface asked, and a CSV-imported review
# with no review_date was counted by one and dropped by another.
#
# Interpolate it into SQL as a column expression. It is a constant built from
# literal column names, never from user input.
# Unqualified column names: every review query selects from `reviews` alone.
REVIEW_TIME_AXIS_BARE = "COALESCE(NULLIF(review_date,''), fetched_at)"

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
    waste_target_pct REAL,               -- target waste as % of purchases for the Food Cost waste-trend chart; NULL means "use the 4.5% industry default" (waste_trend.WASTE_TARGET_PCT)
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
    platform            TEXT    NOT NULL CHECK(platform IN ('google','yelp','csv','manual','tripadvisor','doordash','ubereats')),
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
CREATE INDEX IF NOT EXISTS idx_weekly_reports_restaurant ON weekly_reports(restaurant_id);

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
CREATE INDEX IF NOT EXISTS idx_reviews_rest_date    ON reviews(restaurant_id, review_date);
CREATE INDEX IF NOT EXISTS idx_reviews_rest_status  ON reviews(restaurant_id, response_status);
CREATE INDEX IF NOT EXISTS idx_reviews_rest_processed ON reviews(restaurant_id, processed);
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

-- The layer above a restaurant: a group, a franchise, a company.
--
-- This used to be `restaurants.location_group` — free text an admin typed per
-- location, scoped by (group_name, owner_email) at every read. That worked,
-- but it made the group an emergent property of matching strings rather than
-- a thing that exists: a typo silently created a second group, two clients
-- who typed "Syrup" became one tenant until the owner_email scope was added,
-- and there was nowhere to hang anything a GROUP owns rather than a location.
--
-- A real id makes multi-location employee assignment expressible (memberships
-- already carry a restaurant_id per person, so an organization is simply the
-- set they can belong to) and is a prerequisite for payroll and clock-in.
--
-- It is additive on purpose. location_group stays, is still written, and is
-- still what the backfill derives from, so every existing read path keeps
-- working while the id becomes the grouping key underneath it.
CREATE TABLE IF NOT EXISTS organizations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    owner_email     TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name, owner_email)
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
    waste_target_pct: Optional[float]    = None
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
    # When a geocode last resolved to nothing usable. A place_id that returns
    # no geometry is permanent until someone corrects it, and without this the
    # retry re-billed Google on every call — 570 requests for one restaurant
    # in eight days (audit #17).
    geocode_failed_at: Optional[str]     = None
    # Hold non-critical alerts through lunch and dinner service — see
    # notify.rush_release_at. On by default; an owner who wants everything
    # the moment it lands can turn it off.
    alert_hold_during_service: int       = 1
    # Hour (restaurant-local) to text the routed manager that tonight's
    # lineup notes are ready. 0 = off, which is the default: nobody asked
    # for another text.
    preshift_nudge_hour: int             = 0
    morning_brief_enabled: int           = 1
    morning_brief_hour: int              = 7
    # How much unprompted briefing the owner wants: calm | normal | all.
    # Alerts have a hard ceiling; briefings (NON_ALERT_TYPES) had none.
    briefing_level: str                  = "normal"
    # Set by a self-serve pause: the day Stripe resumes collection, so the
    # product can say "paused until" rather than "lapsed".
    paused_until: str                    = None
    auto_draft_schedule: int             = 0
    external_scheduling_tool: Optional[str] = None   # "Fourth", "7shifts" — a scheduler they already pay for
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
    # RPOWER Core API. The token is issued by RPOWER per integrator+customer
    # and is the ONLY credential — there is no client_id/secret exchange and
    # no refresh, so unlike Toast there is nothing to expire and re-mint.
    # cg (consolidation group) and store_mid are not entered by hand: they
    # come back from /store/get using the token itself, and rpower.bootstrap
    # writes them. Kept as columns anyway because every subsequent call needs
    # both, and re-deriving them per request would be a round trip each time.
    rpower_token: Optional[str]          = None
    rpower_cg: Optional[int]             = None
    rpower_store_mid: Optional[str]      = None
    rpower_store_name: Optional[str]     = None
    rpower_last_synced: Optional[str]    = None
    rpower_sync_error: Optional[str]     = None
    # Set once /store/get has been called successfully — the difference
    # between "a token was pasted in" and "the token works and we know which
    # store it points at".
    rpower_verified_at: Optional[str]    = None
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
    # Notify the owner when an employee opens the staff portal. Off by
    # default: the portal is meant to be used every shift, so this is an
    # opt-in for owners who want to watch it, not a default alarm.
    staff_signin_notify: int         = 0
    marketing_emails_opt_out: int    = 0
    # The restaurant's physical mailing address, printed at the foot of every
    # guest newsletter. CAN-SPAM requires one on commercial email, and the
    # newsletter carried none (MOD-EML-6).
    mailing_address: Optional[str]   = None
    # The monthly business review. On by default and deliberately NOT part
    # of marketing_emails_opt_out: it is a service report on a paid account
    # (metrics vs last month, measured results, goals, what to fix next),
    # and was silently lost by anyone unsubscribing from promotional mail.
    monthly_review_enabled: int      = 1
    # Settings audit additions (Account tab, iOS + web)
    alert_health_bypass_quiet: int   = 0     # health/safety alerts ignore quiet hours
    alert_food_waste: int            = 0     # daily: waste flagged on several items / a real dollar amount
    alert_ai_visibility_drop: int    = 0     # daily: AI visibility score fell vs. the previous run
    alert_competitor_move: int       = 1     # weekly: a tracked competitor's rating moved / a new one appeared
    # The nightly DSR (dsr/): the restaurant's own fiscal calendar and switches.
    fiscal_week_start_dow: Optional[int] = None   # 0=Mon..6=Sun; Erik's week starts Wednesday (2)
    fiscal_year_start: Optional[str] = None       # ISO date of Period 1, Week 1
    fiscal_period_scheme: Optional[str] = None    # "4x13" | "445"
    dsr_enabled: int                 = 1
    dsr_deadline_hour: int           = 4          # local hour a still-incomplete night goes out provisional
    dsr_notify: int                  = 0          # email + push the finished report; off until the owner turns it on
    alert_extra_emails: Optional[str] = None # comma list; alert + digest emails also go here
    push_sound: int                  = 1     # 0 = silent pushes
    auto_approve_5star: int          = 0     # auto-approve (and post) drafted 5-star responses
    # Graduated trust: extend the rule to 3-star and 4-star once the owner's
    # own edit rate on that band has earned it (auto_approve_trust).
    auto_approve_earned: int         = 0
    # Publish the Thursday draft to staff on Friday, with a two-hour undo,
    # once the last few drafts went out unedited (schedule_publish_trust).
    auto_publish_schedule: int       = 0
    # Queue orders to suppliers with a record, inside the usual band, with an
    # hour to undo (ordering.py).
    auto_order_trusted: int          = 0
    # Monday: the agent reads the week and files three owned actions as
    # issues (strategy_jobs.run_weekly_plan). Off by default.
    weekly_plan_enabled: int         = 0
    # Manual sends (supplier order, schedule publish) wait this many minutes
    # with an undo before leaving the building. 0 = send now.
    send_delay_minutes: int          = 0
    auto_approve_4star: int          = 0     # ...and 4-star, under the same cap and the same urgency gate
    auto_approve_daily_cap: int      = 5
    auto_approve_paused: int         = 0     # kill switch — keeps the rule configured but off
    open_times_json: Optional[str]   = None  # {"Monday":"11:00am",...}; close_times_json already exists
    compliance_json: Optional[str]   = None  # schedule_rules.DEFAULTS overrides: min_rest_hours, max_shift_hours, minors, days off
    role_floors_json: Optional[str]  = None  # {"Line Cook": {"morning": 1, "night": 2, "days": {"Saturday": {"night": 3}}}}
    jurisdiction: Optional[str]      = None  # compliance_packs code (CA, NY, …) applied under the owner's own rules
    role_arrival_json: Optional[str] = None  # {"Line Cook": -60} minutes relative to open a role may start (negative = before)
    role_close_min_json: Optional[str] = None  # {"Bartender": 60} the last of a role stays until N minutes after close
    role_requirements_json: Optional[str] = None  # {"Bartender": ["alcohol"]} certifications a role needs
    foh_roles_json: Optional[str]    = None  # ["Server", "Bartender"] roles the section cap counts; default server only
    patio_roles_json: Optional[str]  = None  # roles a rainy day thins first
    role_cross_training_json: Optional[str] = None  # {"Server": 40} % of a role on a shift able to cover a second station
    trim_to_budget: int              = 1     # the deterministic trim past the hours budget (schedule_economics)
    reservation_provider: Optional[str] = None  # reservation_feeds provider code
    reservation_api_key: Optional[str]  = None
    response_language: Optional[str] = None  # None = match the review's language (drafter default)
    tone_preset: Optional[str]       = None  # warm / professional / playful / concise
    data_retention_months: int       = 0     # 0 = keep everything
    alert_1star:          int       = 1
    alert_2star:          int       = 0
    # 3-star was the one rating with no alert path at all — the classic
    # silent-churn review ("waited 45 minutes, food was cold, 3 stars").
    alert_3star:          int       = 0
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
    al_3star_email:       int       = 1
    al_3star_sms:         int       = 0
    al_3star_push:        int       = 1
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
    # Restaurant type for the intelligence engine's cohorts (intelligence/categories.py).
    category: Optional[str] = None
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
        # Scheduling rules the owner sets once (schedule_rules.py): rest
        # between shifts, shift length, minors, days off; and per-role,
        # per-daypart staffing floors that replace the one hardcoded rule.
        ("restaurants", "compliance_json", "TEXT"),
        ("restaurants", "role_floors_json", "TEXT"),
        ("restaurants", "jurisdiction", "TEXT"),
        ("restaurants", "role_arrival_json", "TEXT"),
        ("restaurants", "role_close_min_json", "TEXT"),
        ("restaurants", "role_requirements_json", "TEXT"),
        ("restaurants", "foh_roles_json", "TEXT"),
        ("restaurants", "patio_roles_json", "TEXT"),
        ("restaurants", "role_cross_training_json", "TEXT"),
        ("restaurants", "trim_to_budget", "INTEGER DEFAULT 1"),
        ("restaurants", "reservation_provider", "TEXT"),
        ("restaurants", "reservation_api_key", "TEXT"),
        # schedule_history grew these after it shipped (also ensured lazily
        # by _ensure_history_columns for a database created before boot ran).
        ("schedule_history", "published_at", "TEXT"),
        ("schedule_history", "published_by", "TEXT"),
        # The publish claim: a week goes to staff once, however many devices
        # or retries press Publish (SCHED-29, DATA-12).
        ("schedule_history", "publishing_at", "TEXT"),
        ("schedule_history", "superseded_by", "INTEGER"),
        ("schedule_history", "review_json", "TEXT"),
        ("schedule_history", "generation_seconds", "REAL"),
        ("schedule_history", "weather_json", "TEXT"),
        # The rest of _ensure_history_columns' list, so a fresh database has
        # them at boot and concurrent first saves never race to ALTER.
        ("schedule_history", "quality_json", "TEXT"),
        ("schedule_history", "edited_at", "TEXT"),
        ("schedule_history", "edited_by", "TEXT"),
        ("schedule_history", "quality_score", "REAL"),
        ("schedule_history", "quality_band", "TEXT"),
        ("schedule_history", "quality_confidence", "TEXT"),
        ("schedule_history", "what_if_json", "TEXT"),
        ("schedule_history", "republished_at", "TEXT"),
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
        ("restaurants", "organization_id", "INTEGER"),
        ("restaurants", "location_name", "TEXT"),
        ("restaurants", "pos_system", "TEXT"),
        ("restaurants", "inventory_frequency", "TEXT"),
        ("restaurants", "delivery_days", "TEXT"),
        ("restaurants", "inventory_notes", "TEXT"),
        ("restaurants", "food_cost_target", "REAL"),
        ("restaurants", "waste_target_pct", "REAL"),
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
        ("restaurants", "geocode_failed_at", "TEXT"),
        # Morning brief (audit #18): on by default — it is the owner's own
        # phone, and a brief that has to be discovered in settings is a brief
        # nobody gets. Hour is restaurant-local.
        ("restaurants", "alert_hold_during_service", "INTEGER DEFAULT 1"),
        ("restaurants", "preshift_nudge_hour", "INTEGER DEFAULT 0"),
        ("restaurants", "morning_brief_enabled", "INTEGER DEFAULT 1"),
        ("restaurants", "morning_brief_hour", "INTEGER DEFAULT 7"),
        ("restaurants", "briefing_level", "TEXT DEFAULT 'normal'"),
        ("restaurants", "paused_until", "TEXT"),
        # Weekly schedule auto-draft: OFF unless asked for. It spends AI and
        # writes a draft, and a restaurant already on Fourth or 7shifts does
        # not want one.
        ("restaurants", "auto_draft_schedule", "INTEGER DEFAULT 0"),
        ("restaurants", "external_scheduling_tool", "TEXT"),
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
        # The supplier group's draft hash a PO was sent for — the durable
        # "this exact order already went" claim (record_purchase_order).
        ("purchase_orders", "draft_hash", "TEXT"),
        # What a delivery cost WHEN it arrived (cogs.purchases_in_window).
        # Priced at today's unit_cost, an invoice re-priced every past
        # delivery in the window (MOD-FC-23). NULL on rows from before.
        ("ingredient_stock_events", "unit_cost", "REAL"),
        # The POS's id for a member of staff, so a comp/void concentration
        # can be named to a person the owner knows (moat audit #9).
        ("staff_contacts", "pos_id", "TEXT"),
        # Changelog seen state
        ("restaurants", "changelog_seen_at", "TEXT"),
        ("restaurants", "category", "TEXT"),
        # Notifications (alert_log) seen state — same stamp-on-read pattern.
        # Superseded by the per-login notification_reads table (two co-owners
        # share one restaurant row, so one of them opening the bell cleared
        # the other's badge). Kept as the fallback for a login that has never
        # opened the list, and so an old client build keeps working.
        ("restaurants", "notifications_seen_at", "TEXT"),
        # What an alert_log row fired on (notify._log_alert) and how urgent it
        # is (notify.PRIORITY). Both were added lazily on the write path
        # before; owned by init_db now, per the no-DDL-on-a-request rule.
        ("alert_log", "value", "REAL"),
        ("alert_log", "priority", "INTEGER"),
        # The figure a held daily alert fired on, so releasing it after the
        # rush still records what _waste_alert_worsened compares against.
        ("alert_holds", "value", "REAL"),
        # Engagement, kept SEPARATE from email_log.status so an open does not
        # overwrite the delivery state. Only populated when open/click
        # tracking is enabled on the Resend side; no events simply means no
        # timestamps, never a wrong one.
        ("email_log", "opened_at", "TEXT"),
        ("email_log", "clicked_at", "TEXT"),
        # NULL = every email; 'guest' = only guest-facing mail (see
        # suppress_email). A guest's newsletter complaint used to stop the
        # same person's staff schedules too (MOD-EML-7).
        ("email_suppressions", "scope", "TEXT"),
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
        # schedule_history's edit stamp was added lazily on the first edit
        # (save_schedule_edit); schedule_publish_trust reads it on a fresh
        # database, so it belongs in the startup list like everything else.
        ("schedule_history", "quality_json", "TEXT"),
        ("schedule_history", "edited_at",   "TEXT"),
        ("schedule_history", "edited_by",   "TEXT"),
        ("reviews", "original_rating",   "INTEGER"),
        # A draft that generated cleanly but states something unverifiable.
        ("reviews", "draft_needs_review", "INTEGER DEFAULT 0"),
        ("reviews", "draft_review_reason", "TEXT"),
        # Suggested vs chosen (audit #41): the model's draft as it stood when
        # the owner first edited it — kept, never overwritten by the edit.
        ("reviews", "original_draft", "TEXT"),
        # What the owner's edit did to that draft, measured at approval
        # (reply_edits.compare; ROI audit #40): a word edit distance, its
        # category and the closed-vocabulary signals. Read by the drafter's
        # few-shot selection and its OWNER'S EDITS note.
        ("reviews", "edit_distance", "REAL"),
        ("reviews", "edit_category", "TEXT"),
        ("reviews", "edit_signals", "TEXT"),
        # When a drafted reply was skipped: a skip is the owner declining the
        # draft, so it counts against auto-approve trust (audit #15).
        ("reviews", "skipped_at", "TEXT"),
        # Recipe provenance (audit #35): 'owner' (typed or imported by a
        # person), 'draft_accepted' (a Cavnar draft accepted unedited) or
        # 'draft_edited' (a draft line the owner changed before accepting).
        # NULL on rows written before this existed = entered by a person.
        ("recipe_ingredients", "source", "TEXT"),
        # What the owner actually accepted from a recipe draft, beside the
        # draft's own lines_json (audit #41: suggested vs chosen).
        ("recipe_drafts", "accepted_lines_json", "TEXT"),
        ("recipe_drafts", "edited_lines", "INTEGER"),
        # Supplier orders (audit #41): who sent it ('owner' or 'automatic'),
        # the draft it was built from, and whether the sent lines differ.
        # Order trust counts only owner-sent, unedited orders.
        ("purchase_orders", "source", "TEXT"),
        ("purchase_orders", "draft_items_json", "TEXT"),
        ("purchase_orders", "edited", "INTEGER DEFAULT 0"),
        # An invoice scan the trusted-supplier rule applied on its own (M-4):
        # never the owner's evidence in ordering.invoice_trust, or the rule
        # grades itself.
        ("invoice_imports", "auto_applied", "INTEGER DEFAULT 0"),
        # Figures in a stored diagnosis the verifier could not trace to the
        # data (JSON list), so every surface shows the caveat (M-17).
        ("review_diagnoses", "unsupported_figures", "TEXT"),
        # A post's lift verdict against its own noise band (M-23): readers
        # colour the result from it, never from the sign of lift_pct.
        ("marketing_attribution", "verdict", "TEXT"),
        ("marketing_attribution", "noise_band_pct", "REAL"),
        ("food_cost_diagnoses", "unsupported_figures", "TEXT"),
        # 'owner' | 'job' | 'marker' — so "pieces this month" counts content
        # a person made or published, not calendar markers and job drafts.
        ("marketing_content_log", "origin", "TEXT"),
        # When the official Google rating was last refreshed. Without it a
        # failed refresh left the previous value in place indefinitely,
        # shown as current and driving the rating-threshold alert.
        ("restaurants", "gbp_rating_updated_at", "TEXT"),
        # Sample size behind each visibility run, so a change can be told
        # from a difference in how many queries came back.
        ("ai_visibility_runs", "answered", "INTEGER"),
        ("ai_visibility_runs", "appeared", "INTEGER"),
        # What each run was measured against (MOD-INT-4): "<source>:<city>".
        # A profile-city run and a Google-city run ask different questions
        # and match different strings; comparing them is a change of ruler.
        ("ai_visibility_runs", "city_basis", "TEXT"),
        # The run as the owner saw it, so the Intel tab can serve the stored
        # measurement after a redeploy instead of re-asking Perplexity
        # eight live questions on a request thread (MOD-INT-5).
        ("ai_visibility_runs", "payload_json", "TEXT"),
        # Recommendation-trust audit (notify / issues / Ask). An issue filed
        # with notify=False keeps that intent: issues.tick used to text every
        # open, assigned, un-notified issue on the next pass, so a comp/void
        # flag naming a manager was texted to the routed manager anyway (#1).
        ("ops_issues", "notify_suppressed", "INTEGER DEFAULT 0"),
        # Structured detail an issue's page acts on (a coverage issue's
        # suggested covers, so "Ask Ana to cover" is one tap).
        ("ops_issues", "meta_json", "TEXT"),
        # A held alert's recommendation key and audience, so its release
        # records and targets exactly what raising it would have.
        ("alert_holds", "meta_json", "TEXT"),
        # Which notification an open answers, so time-to-open is measurable
        # (#39); keyed only by alert_type before.
        ("notification_opens", "alert_log_id", "INTEGER"),
        ("notification_opens", "rec_key", "TEXT"),
        # One Ask proposal is settled by ITS id, not by every proposal with
        # the same action name (#23); and why a "Not now" was said.
        ("ask_cavnar_actions", "proposal_id", "INTEGER"),
        ("ask_cavnar_actions", "reason", "TEXT"),
        # Weekly competitor-movement alert, on by default (#48).
        ("restaurants", "alert_competitor_move", "INTEGER DEFAULT 1"),
        # The nightly DSR: fiscal calendar and switches (dsr/).
        ("restaurants", "fiscal_week_start_dow", "INTEGER"),
        ("restaurants", "fiscal_year_start", "TEXT"),
        ("restaurants", "fiscal_period_scheme", "TEXT"),
        ("restaurants", "dsr_enabled", "INTEGER DEFAULT 1"),
        ("restaurants", "dsr_deadline_hour", "INTEGER DEFAULT 4"),
        ("restaurants", "dsr_notify", "INTEGER DEFAULT 0"),
        # The close-out's DSR fields (closeout.DSR_FIELDS): what the closer
        # knows that no system records. All optional, all the manager's own
        # words; `influence` is Erik's "Influence/Result" column.
        ("close_outs", "equipment", "TEXT"),
        ("close_outs", "vip_guests", "TEXT"),
        ("close_outs", "maintenance", "TEXT"),
        ("close_outs", "shift_notes", "TEXT"),
        ("close_outs", "general_notes", "TEXT"),
        ("close_outs", "influence", "TEXT"),
    ]
    try:
        for table, col, col_type in columns_to_add:
            # "no such table" too: a table an init_* helper creates later in
            # boot is migrated when ensure_columns runs again after it.
            if _apply_migration(conn, f"ALTER TABLE {table} ADD COLUMN {col} {col_type}",
                                also_tolerate=("no such table",)):
                conn.commit()
                print(f"Added column {table}.{col}")
    finally:
        conn.close()


# The only failures a boot migration may treat as "already applied". Every
# migration used to be `try/except: pass`, so "database is locked" (an
# overlapped container, worker.py, a `railway ssh sqlite3` session) was read
# as "column exists", init_db returned normally and the app served on a
# drifted schema (DATA-11). Anything else now raises and fails the boot.
_MIGRATION_ALREADY_APPLIED = ("duplicate column name", "already exists")


def _apply_migration(conn, sql, also_tolerate=()):
    """Run one boot migration. True if it applied, False if it was already
    applied; any other failure raises."""
    try:
        conn.execute(sql)
        return True
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if any(s in msg for s in _MIGRATION_ALREADY_APPLIED + tuple(also_tolerate)):
            return False
        raise

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
                    # Every inbox and analytics query is scoped to one
                    # restaurant and then ordered/filtered by time or
                    # status; a bare restaurant_id index left the rest to a
                    # scan of that restaurant's whole history.
                    "CREATE INDEX IF NOT EXISTS idx_reviews_rest_date ON reviews(restaurant_id, review_date)",
                    "CREATE INDEX IF NOT EXISTS idx_reviews_rest_status ON reviews(restaurant_id, response_status)",
                    "CREATE INDEX IF NOT EXISTS idx_reviews_rest_processed ON reviews(restaurant_id, processed)",
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


_OLD_REVIEW_PLATFORM_CHECK = "CHECK(platform IN ('google','yelp','csv','manual'))"
_REVIEW_PLATFORM_CHECK = "CHECK(platform IN ('google','yelp','csv','manual','tripadvisor','doordash','ubereats'))"


def _migrate_reviews_platform_check(conn):
    """Widen reviews.platform's CHECK to the third-party imports.

    The CSV import (client_api.import_tripadvisor) writes 'tripadvisor',
    'doordash' and 'ubereats', which the CHECK refused: every row was
    rejected, save_reviews logged it, and the route told the owner
    "imported: N" with nothing stored (DATA-21). SQLite cannot alter a CHECK,
    so this is the same rebuild as _migrate_reviews_unique — new table, copy,
    swap, one transaction — and a no-op once the table has the wider CHECK.
    The table's indexes are captured first and replayed after the swap.
    """
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'").fetchone()
        create = (row[0] if row else "") or ""
        if _OLD_REVIEW_PLATFORM_CHECK not in create:
            return
        cols = [r[1] for r in conn.execute("PRAGMA table_info(reviews)").fetchall()]
        col_list = ", ".join(cols)
        indexes = [r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='reviews' AND sql IS NOT NULL").fetchall()]
        import re as _re
        new_create, n = _re.subn(r'^CREATE TABLE\s+(?:"reviews"|reviews)(?=\s*\()', "CREATE TABLE reviews_platforms",
                                 create.replace(_OLD_REVIEW_PLATFORM_CHECK, _REVIEW_PLATFORM_CHECK), count=1)
        if n != 1:
            print("[migrate] reviews platform CHECK: could not rewrite CREATE statement, leaving as is")
            return
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS reviews_platforms")
        conn.execute(new_create)
        conn.execute(f"INSERT INTO reviews_platforms ({col_list}) SELECT {col_list} FROM reviews")
        conn.execute("DROP TABLE reviews")
        conn.execute("ALTER TABLE reviews_platforms RENAME TO reviews")
        for sql in indexes:
            conn.execute(sql)
        conn.execute("COMMIT")
        conn.execute("PRAGMA foreign_keys=ON")
        print("[migrate] reviews.platform now accepts tripadvisor, doordash and ubereats")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass
        print(f"[migrate] reviews platform CHECK FAILED, table left untouched: {e}")


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
        "ALTER TABLE restaurants ADD COLUMN organization_id INTEGER",
        "ALTER TABLE restaurants ADD COLUMN location_name TEXT",
        "ALTER TABLE restaurants ADD COLUMN inventory_frequency TEXT DEFAULT 'weekly'",
        "ALTER TABLE restaurants ADD COLUMN inventory_notes TEXT",
        "ALTER TABLE restaurants ADD COLUMN food_cost_target REAL DEFAULT 30.0",
        "ALTER TABLE restaurants ADD COLUMN waste_target_pct REAL",
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
        "ALTER TABLE restaurants ADD COLUMN staff_signin_notify INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN marketing_emails_opt_out INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN mailing_address TEXT",
        "ALTER TABLE restaurants ADD COLUMN monthly_review_enabled INTEGER DEFAULT 1",
        # Optimistic concurrency. Bumped by every update_restaurant write;
        # only compared against when a caller passes expected_version.
        "ALTER TABLE restaurants ADD COLUMN row_version INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_health_bypass_quiet INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_food_waste INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_ai_visibility_drop INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN alert_extra_emails TEXT",
        "ALTER TABLE restaurants ADD COLUMN push_sound INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN auto_approve_5star INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN auto_approve_4star INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN auto_approve_earned INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN auto_publish_schedule INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN auto_order_trusted INTEGER DEFAULT 0",
        # home_dismissals was created lazily by home_brief; owned here now so
        # the repeat-hide counter exists on every database at boot.
        """CREATE TABLE IF NOT EXISTS home_dismissals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'recommendation',
            dismissed_by INTEGER,
            dismissed_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            times INTEGER NOT NULL DEFAULT 1,
            UNIQUE(restaurant_id, key) ON CONFLICT REPLACE
        )""",
        "ALTER TABLE home_dismissals ADD COLUMN times INTEGER NOT NULL DEFAULT 1",
        # Resumable-job cursors (scheduler.run_daily_fetch, intelligence.jobs):
        # where the last bounded pass stopped, so the next one starts there.
        # Two callers used to create this table themselves with two different
        # definitions; whichever ran first on a fresh volume won. One owner.
        """CREATE TABLE IF NOT EXISTS job_cursors (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        # The admin console's own ledgers (admin_events, admin_ops). Each used
        # to be created by the function that first wrote it.
        """CREATE TABLE IF NOT EXISTS admin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            restaurant_id INTEGER,
            customer_id TEXT,
            email TEXT,
            amount REAL,
            summary TEXT,
            payload TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS admin_issue_resolutions (
            key TEXT PRIMARY KEY,
            resolved_at TEXT DEFAULT (datetime('now')),
            note TEXT,
            actor TEXT
        )""",
        # Inbound-webhook idempotency (webhook_routes): the INSERT on the
        # primary key is the claim. Both used to be created inside the
        # webhook handler, on every delivery.
        """CREATE TABLE IF NOT EXISTS stripe_events_seen (
            event_id   TEXT PRIMARY KEY,
            event_type TEXT,
            seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        # The newest Stripe event applied to each restaurant's billing state.
        # Stripe does not deliver in order; an older event arriving later
        # (an invoice.paid created before the cancellation) must not undo a
        # newer one (MOD-BIL-1).
        # The subscription a restaurant's checkout created. Paying in both the
        # monthly and the annual tab made two live subscriptions and two
        # setup charges (MOD-BIL-5); the second is cancelled against this.
        # Every DocuSign envelope a restaurant has been sent. Re-sending a
        # contract replaced docusign_envelope_id, so a client who then signed
        # the FIRST email's envelope matched nothing (MOD-BIL-6).
        # Answers to paid, retryable requests (invoice and recipe scans),
        # keyed by the client's Idempotency-Key: a retry after a timeout is
        # answered from the first result instead of a second model call
        # (CLIENT-21). Kept 7 days.
        """CREATE TABLE IF NOT EXISTS idempotent_responses (
            restaurant_id INTEGER NOT NULL,
            route         TEXT    NOT NULL,
            idem_key      TEXT    NOT NULL,
            status        INTEGER,
            payload_json  TEXT,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, route, idem_key)
        )""",
        """CREATE TABLE IF NOT EXISTS docusign_envelopes (
            envelope_id   TEXT PRIMARY KEY,
            restaurant_id INTEGER NOT NULL,
            sent_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS stripe_subscriptions (
            restaurant_id   INTEGER PRIMARY KEY,
            subscription_id TEXT NOT NULL,
            session_id      TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS stripe_billing_clock (
            restaurant_id      INTEGER PRIMARY KEY,
            last_event_created INTEGER NOT NULL,
            last_event_id      TEXT,
            updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS docusign_events_seen (
            event_key   TEXT PRIMARY KEY,
            envelope_id TEXT,
            status      TEXT,
            seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        # Durable login throttling (security.py): keyed by IP and by account,
        # so a deploy no longer resets the counter and one worker is no
        # longer a security requirement.
        """CREATE TABLE IF NOT EXISTS login_attempts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            key          TEXT NOT NULL,
            kind         TEXT NOT NULL,
            ip           TEXT,
            attempted_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_key ON login_attempts(key, attempted_at)",
        # Temporary passwords are no longer persisted (security audit A3):
        # they are emailed once and a fresh one is minted at contract
        # signing. Anything still stored is a plaintext login credential.
        "UPDATE restaurants SET temp_password=NULL WHERE temp_password IS NOT NULL AND temp_password != ''",
        "ALTER TABLE restaurants ADD COLUMN weekly_plan_enabled INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN send_delay_minutes INTEGER DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS recipe_drafts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            menu_item_id   INTEGER NOT NULL,
            menu_item_name TEXT,
            lines_json     TEXT    NOT NULL,
            note           TEXT,
            status         TEXT    NOT NULL DEFAULT 'pending',
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            answered_at    TEXT,
            answered_by    INTEGER
        )""",
        """CREATE TABLE IF NOT EXISTS delayed_actions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            kind          TEXT    NOT NULL,
            label         TEXT,
            payload_json  TEXT,
            execute_at    TEXT    NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'pending',
            created_by    TEXT,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            executed_at   TEXT,
            result_json   TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_delayed_actions_due ON delayed_actions(status, execute_at)",
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
        # Read per restaurant, newest first (MOD-PERF-6), and pruned by
        # created_at alone (ops.prune_ledgers, DATA-40).
        "CREATE INDEX IF NOT EXISTS idx_aivis_runs_rest ON ai_visibility_runs(restaurant_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_aivis_runs_created ON ai_visibility_runs(created_at)",
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
        # RPOWER Core API — see the dataclass for why cg/store_mid are stored
        # rather than entered.
        "ALTER TABLE restaurants ADD COLUMN rpower_token TEXT",
        "ALTER TABLE restaurants ADD COLUMN rpower_cg INTEGER",
        "ALTER TABLE restaurants ADD COLUMN rpower_store_mid TEXT",
        "ALTER TABLE restaurants ADD COLUMN rpower_store_name TEXT",
        "ALTER TABLE restaurants ADD COLUMN rpower_last_synced TEXT",
        "ALTER TABLE restaurants ADD COLUMN rpower_sync_error TEXT",
        "ALTER TABLE restaurants ADD COLUMN rpower_verified_at TEXT",
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
        "CREATE INDEX IF NOT EXISTS idx_review_requests_rest ON review_requests(restaurant_id, sent_at)",  # MOD-PERF-6
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
        # ops.prune_ledgers deletes by fired_at alone, which the index above
        # cannot serve (DATA-40).
        "CREATE INDEX IF NOT EXISTS idx_alert_log_fired ON alert_log(fired_at)",
        # Per-LOGIN notification read state. restaurants.notifications_seen_at
        # was one stamp for the whole restaurant, so a co-owner opening the
        # bell cleared their partner's unread badge — and the two clients
        # disagreed anyway, since the web bell kept its own localStorage copy.
        # seen_at is written in SQLite's own "%Y-%m-%d %H:%M:%S" so it
        # compares correctly against alert_log.fired_at: the old ISO 'T'
        # stamp sorted BELOW every same-day fired_at (' ' < 'T'), which made
        # the unread count structurally incapable of seeing today's alerts.
        # Which notifications actually get opened, per login per type. The
        # product could say how many notifications it SENT and nothing about
        # whether any of them were worth sending — so the one question an
        # owner would ask ("is this thing useful?") had no answer, and
        # neither did the one Will would ask about a client.
        #
        # Deliberately NOT used to suppress anything automatically: reading a
        # banner on a lock screen is engagement and leaves no tap behind, so
        # inferring "they ignore these" from taps alone would quietly switch
        # off alerts an owner reads every day. It powers a SUGGESTION the
        # owner accepts or ignores (notify.engagement_report).
        """CREATE TABLE IF NOT EXISTS notification_opens (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            user_id       INTEGER,
            alert_type    TEXT NOT NULL,
            opened_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_notification_opens ON notification_opens(restaurant_id, alert_type, opened_at)",
        # ops.prune_ledgers deletes by age (MOD-NOT-14).
        "CREATE INDEX IF NOT EXISTS idx_notification_opens_opened ON notification_opens(opened_at)",
        """CREATE TABLE IF NOT EXISTS notification_reads (
            user_id       INTEGER NOT NULL,
            restaurant_id INTEGER NOT NULL,
            seen_at       TEXT    NOT NULL,
            PRIMARY KEY (user_id, restaurant_id)
        )""",
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
        "ALTER TABLE restaurants ADD COLUMN alert_3star INTEGER DEFAULT 0",
        "ALTER TABLE reviews ADD COLUMN analysis_attempts INTEGER DEFAULT 0",
        "ALTER TABLE reviews ADD COLUMN draft_attempts INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN al_3star_email INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN al_3star_sms INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN al_3star_push INTEGER DEFAULT 1",
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
        # What the post was about (marketing_tags.py): the dish it promoted,
        # the occasion, and its kind — so its result reads against them.
        "ALTER TABLE marketing_content_log ADD COLUMN menu_item_id INTEGER",
        "ALTER TABLE marketing_content_log ADD COLUMN occasion TEXT",
        "ALTER TABLE marketing_content_log ADD COLUMN post_kind TEXT",
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
        # Recent taps per link, by a one-way visitor key, so a repeat or a
        # flood from one visitor is counted once (MOD-MKT-18). Pruned after
        # two days by ops.prune_ledgers.
        """CREATE TABLE IF NOT EXISTS marketing_link_taps (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            link_id    INTEGER NOT NULL,
            visitor    TEXT    NOT NULL,
            tapped_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mkt_link_taps ON marketing_link_taps(link_id, visitor, tapped_at)",
        "CREATE INDEX IF NOT EXISTS idx_mkt_link_taps_tapped ON marketing_link_taps(tapped_at)",

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
        "CREATE INDEX IF NOT EXISTS idx_marketing_attr_rest ON marketing_attribution(restaurant_id)",  # MOD-PERF-6
        # Beyond total sales: the promoted dish's own units, reviews that
        # mentioned it, and the guest list's move in the post's window.
        "ALTER TABLE marketing_attribution ADD COLUMN item_lift_pct REAL",
        "ALTER TABLE marketing_attribution ADD COLUMN item_window_qty REAL",
        "ALTER TABLE marketing_attribution ADD COLUMN item_baseline_qty REAL",
        "ALTER TABLE marketing_attribution ADD COLUMN reviews_mentioning INTEGER",
        "ALTER TABLE marketing_attribution ADD COLUMN guest_list_delta INTEGER",
        "ALTER TABLE marketing_attribution ADD COLUMN engagement_rate REAL",

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
        # Time off, asked for by the person who needs it (staff portal) and
        # decided by a manager; approved ranges are hard constraints on the
        # next schedule draft (moat audit #5).
        """CREATE TABLE IF NOT EXISTS staff_time_off (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            employee_name   TEXT NOT NULL,
            start_date      TEXT NOT NULL,
            end_date        TEXT NOT NULL,
            reason          TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            decided_by      INTEGER,
            decided_at      TEXT,
            decision_note   TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_staff_time_off_restaurant ON staff_time_off(restaurant_id, status, start_date)",
        # ── Restaurant Intelligence Engine (INTELLIGENCE_ENGINE.md) ──────────
        # One row per restaurant-week of ratios, rates and counts. This is the
        # ONLY table cross-restaurant learning reads; it holds no names, no
        # dollars and no people (intelligence/privacy.py).
        """CREATE TABLE IF NOT EXISTS intel_features (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            week           TEXT    NOT NULL,
            features_json  TEXT    NOT NULL,
            completeness   REAL    NOT NULL DEFAULT 0,
            computed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, week)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_intel_features_week ON intel_features(week, restaurant_id)",
        # Every recommendation's life: presented, answered, measured.
        """CREATE TABLE IF NOT EXISTS intel_rec_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            rec_kind       TEXT    NOT NULL,
            source_key     TEXT    NOT NULL,
            cohort         TEXT,
            action         TEXT    NOT NULL,
            outcome        TEXT,
            days_to_effect INTEGER,
            confidence_at  REAL,
            event_at       TEXT    NOT NULL,
            synced_from    TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, source_key, action)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_intel_rec_events_kind ON intel_rec_events(rec_kind, action)",
        "CREATE INDEX IF NOT EXISTS idx_intel_rec_events_rest ON intel_rec_events(restaurant_id, event_at)",
        # Discovered patterns: counts and effects only, never a name.
        """CREATE TABLE IF NOT EXISTS intel_patterns (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            key            TEXT    NOT NULL UNIQUE,
            cohort         TEXT    NOT NULL,
            hypothesis     TEXT    NOT NULL,
            n_with         INTEGER NOT NULL,
            n_without      INTEGER NOT NULL,
            effect         REAL    NOT NULL,
            effect_unit    TEXT,
            cohen_d        REAL,
            p_value        REAL    NOT NULL,
            q_value        REAL,
            confidence     REAL    NOT NULL,
            sentence       TEXT    NOT NULL,
            evidence_json  TEXT,
            status         TEXT    NOT NULL DEFAULT 'active',
            first_seen     TEXT    NOT NULL DEFAULT (datetime('now')),
            last_confirmed TEXT    NOT NULL DEFAULT (datetime('now')),
            computed_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS intel_benchmarks (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            cohort         TEXT    NOT NULL,
            metric         TEXT    NOT NULL,
            week           TEXT    NOT NULL,
            n              INTEGER NOT NULL,
            p25            REAL,
            p50            REAL,
            p75            REAL,
            mean           REAL,
            computed_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(cohort, metric, week)
        )""",
        """CREATE TABLE IF NOT EXISTS intel_confidence_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            week            TEXT    NOT NULL,
            cohort          TEXT    NOT NULL,
            rec_kind        TEXT    NOT NULL,
            n               INTEGER NOT NULL,
            mean_confidence REAL,
            acceptance_rate REAL,
            success_rate    REAL,
            computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(week, cohort, rec_kind)
        )""",
        # Cover counts by day. The labor analysis had no way to tell a lean
        # day from a short-staffed one; covers are the one figure that
        # separates them (moat audit #4). Entered or imported, never inferred.
        """CREATE TABLE IF NOT EXISTS covers_daily (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            date            TEXT NOT NULL,
            covers          INTEGER NOT NULL,
            source          TEXT NOT NULL DEFAULT 'manual',
            saved_at        TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, date)
        )""",
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
        "CREATE INDEX IF NOT EXISTS idx_email_log_sent ON email_log(sent_at)",   # prune_ledgers (DATA-40)
        # Addresses Resend told us are undeliverable or that reported us as
        # spam. Suppressed at send time: retrying a hard bounce forever, or
        # continuing to mail someone who hit "report spam", is exactly what
        # burns a sending domain — and this domain also carries 2FA and
        # password-reset mail.
        # Secrets this install mints for itself and keeps, so a link signed
        # with one survives a SECRET_KEY rotation (MOD-EML-9): unsubscribe
        # links sit in inboxes for years.
        """CREATE TABLE IF NOT EXISTS app_secrets (
            name        TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
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
        # "Was this useful?" on an Ask answer (ROI audit #48): one row per
        # answer and login, changed in place if they change their mind. The
        # message can be trimmed from the transcript later; the rating is
        # the record, so it keeps the conversation id beside the message id.
        """CREATE TABLE IF NOT EXISTS ask_feedback (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            user_id         INTEGER,
            message_id      INTEGER NOT NULL,
            conversation_id INTEGER,
            helpful         INTEGER NOT NULL CHECK(helpful IN (0, 1)),
            note            TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, message_id, user_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ask_feedback_restaurant ON ask_feedback(restaurant_id, created_at)",
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
        # A recipe row should exist once per (dish, ingredient). Without this
        # the same ingredient could be bound to a dish twice and the plate
        # cost silently double-counted it. Collapse any existing duplicates
        # first, keeping the most recently added row, or the unique index
        # can't be created and the guarantee would silently not exist.
        """DELETE FROM recipe_ingredients WHERE id NOT IN (
               SELECT MAX(id) FROM recipe_ingredients GROUP BY menu_item_id, ingredient_id
           )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_recipe_pair ON recipe_ingredients(menu_item_id, ingredient_id)",
        # Units sold per dish per day. compute_daily_depletion already reads
        # exactly this from Toast to drive depletion and then discarded it,
        # which is why menu margins had no popularity data: "best dish" meant
        # lowest food cost %, so a $3 soda outranked a $38 entree carrying $25
        # of contribution, and the menu's average food cost % was an
        # unweighted mean of per-dish percentages rather than a revenue-
        # weighted one.
        """CREATE TABLE IF NOT EXISTS menu_item_sales (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL,
            menu_item_id    INTEGER NOT NULL REFERENCES menu_items(id),
            business_date   TEXT NOT NULL,
            qty_sold        REAL NOT NULL,
            UNIQUE(restaurant_id, menu_item_id, business_date)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_menu_item_sales ON menu_item_sales(restaurant_id, business_date)",
        # inventory_history is read on every waste-trend request, filtered by
        # restaurant_id and sorted by week_end, and had no index at all. The
        # table itself was only ever created lazily by whichever food-cost
        # path ran first, so it is declared here too — an index migration
        # against a table that doesn't exist yet fails silently.
        """CREATE TABLE IF NOT EXISTS inventory_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL,
            waste_json      TEXT,
            week_end        TEXT,
            items_json      TEXT,
            saved_at        TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_inventory_history_rid ON inventory_history(restaurant_id, week_end)",
        # _compute_current_stock and recompute_rollups filter on ingredient_id
        # alone, so idx_stock_events_ingredient's leading restaurant_id column
        # never matched and every rollup scanned.
        "CREATE INDEX IF NOT EXISTS idx_stock_events_by_ingredient ON ingredient_stock_events(ingredient_id, event_type, id)",

        # ── Review intelligence: the operational dimension of a review ──────
        #
        # The analyser returned {sentiment, categories, summary, urgency} and
        # nothing else, so "cold food" and "the steak was overcooked" and
        # "they forgot my appetiser" all collapsed into the single token
        # `food_quality`. Every downstream question an owner actually asks —
        # which dish, which shift, which role, is this the same problem as
        # last week — was unanswerable from stored data at any price, because
        # the detail was discarded at the moment of analysis and the review
        # text was never shown to a model again.
        #
        # These three columns are that detail. They cost no extra AI call:
        # the analyser already reads the full text, it simply threw this away.
        "ALTER TABLE reviews ADD COLUMN entities TEXT",             # JSON {dishes, staff_roles, daypart, service_mode}
        "ALTER TABLE reviews ADD COLUMN specific_complaint TEXT",   # <=8 words, the actual thing that went wrong
        "ALTER TABLE reviews ADD COLUMN severity TEXT",             # safety|legal|operational|service|minor
        "CREATE INDEX IF NOT EXISTS idx_reviews_rest_severity ON reviews(restaurant_id, severity)",
        # The time axis every intelligence query filters on is an EXPRESSION,
        # not a column — REVIEW_TIME_AXIS_BARE. idx_reviews_rest_date covers
        # review_date alone, which the planner cannot use for the COALESCE,
        # so complaint_clusters/rating_trend narrowed to the restaurant and
        # then evaluated the date over its whole review history in memory.
        # Bounded (one restaurant, not the table) but it grows with every
        # review that restaurant ever receives. Audit #17.
        "CREATE INDEX IF NOT EXISTS idx_reviews_rest_axis "
        "ON reviews(restaurant_id, COALESCE(NULLIF(review_date,''), fetched_at))",
        # usage_summary groups by action+model and the admin AI-cost view is
        # the only reader; only created_at and (restaurant_id, created_at)
        # existed.
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_action ON ai_usage(action, model)",

        # ── Strategic feature audit (#18) ─────────────────────────────────
        # Whether a recommendation the owner acted on actually moved the
        # number it was aimed at. Baseline is snapshotted when they commit to
        # it, the same metric is re-measured after the window, and the
        # verdict respects each metric's noise band (see metrics.compare).
        """CREATE TABLE IF NOT EXISTS recommendation_outcomes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            source          TEXT NOT NULL,
            source_key      TEXT NOT NULL,
            title           TEXT NOT NULL,
            metric          TEXT NOT NULL,
            baseline_value  REAL,
            baseline_start  TEXT,
            baseline_end    TEXT,
            baseline_detail TEXT,
            started_on      TEXT NOT NULL,
            evaluate_on     TEXT NOT NULL,
            after_value     REAL,
            after_start     TEXT,
            after_end       TEXT,
            verdict         TEXT,
            delta           REAL,
            dollars_monthly REAL,
            status          TEXT NOT NULL DEFAULT 'tracking',
            created_by      INTEGER,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_outcomes_restaurant ON recommendation_outcomes(restaurant_id, status)",
        # One live tracker per recommendation — committing to the same fix
        # twice must not double-count it.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_outcomes_tracking ON recommendation_outcomes"
        "(restaurant_id, source_key) WHERE status='tracking'",
        # Rec-ROI audit (#5, #14, #16, #23, #30, #31, #33). After the CREATE,
        # so a fresh database gets them too; outcomes.init_outcomes fills
        # what can be recomputed on older rows at boot.
        "ALTER TABLE recommendation_outcomes ADD COLUMN module TEXT",            # credited module (value vocabulary)
        "ALTER TABLE recommendation_outcomes ADD COLUMN baseline_kind TEXT",     # prior window | matched weekdays | same weeks last year
        "ALTER TABLE recommendation_outcomes ADD COLUMN baseline_raw REAL",      # the before-window reading, before any seasonal adjustment
        "ALTER TABLE recommendation_outcomes ADD COLUMN after_detail TEXT",
        "ALTER TABLE recommendation_outcomes ADD COLUMN delta_pct REAL",
        "ALTER TABLE recommendation_outcomes ADD COLUMN attribution TEXT",       # none | associated | consistent | held
        "ALTER TABLE recommendation_outcomes ADD COLUMN concurrent TEXT",        # JSON [{kind, label, date}]; NULL = never checked
        "ALTER TABLE recommendation_outcomes ADD COLUMN recheck_on TEXT",
        "ALTER TABLE recommendation_outcomes ADD COLUMN recheck_value REAL",
        "ALTER TABLE recommendation_outcomes ADD COLUMN recheck_verdict TEXT",   # held | faded | reversed | unknown
        "ALTER TABLE recommendation_outcomes ADD COLUMN rechecked_at TEXT",
        "ALTER TABLE recommendation_outcomes ADD COLUMN accrued_through TEXT",   # last day outcome_value_days has read
        "CREATE INDEX IF NOT EXISTS idx_outcomes_recheck ON recommendation_outcomes(status, recheck_on)",
        # Measured dollars, one row per tracker per measured day (#14): the
        # cumulative figure is a SUM of these, never monthly x months. Signed
        # (a worsened change's days are negative); `counted` marks the one
        # row per restaurant, family, direction and day that is summed, so
        # waste and food cost over the same days are one saving (#4).
        """CREATE TABLE IF NOT EXISTS outcome_value_days (
            outcome_id     INTEGER NOT NULL REFERENCES recommendation_outcomes(id),
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            day            TEXT    NOT NULL,
            module         TEXT,
            metric         TEXT    NOT NULL,
            family         TEXT    NOT NULL,
            sign           INTEGER NOT NULL,              -- +1 a win, -1 a change that got worse
            dollars        REAL    NOT NULL,              -- signed; 0 on a measured day it did not hold
            held           INTEGER NOT NULL DEFAULT 0,
            counted        INTEGER NOT NULL DEFAULT 0,
            basis          TEXT    NOT NULL,              -- 'window' | 'daily'
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (outcome_id, day)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_outcome_value_days ON outcome_value_days(restaurant_id, day)",

        # Structured goals. They used to exist only as free text in
        # ask_memory ("wants labor under 26%"), which the assistant could
        # quote but nothing could measure.
        """CREATE TABLE IF NOT EXISTS owner_goals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            metric          TEXT NOT NULL,
            target          REAL NOT NULL,
            deadline        TEXT,
            baseline_value  REAL,
            baseline_detail TEXT,
            status          TEXT NOT NULL DEFAULT 'active',
            note            TEXT,
            created_by      INTEGER,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            achieved_at     TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_goals_restaurant ON owner_goals(restaurant_id, status)",

        # Moments worth marking, each fired at most once ever. The UNIQUE
        # index IS the once-only guarantee: milestones.fire() does an INSERT
        # and treats an IntegrityError as "already celebrated", the same
        # shape ops.claim_period uses for jobs.
        #
        # This replaces a localStorage flag. The one celebration the product
        # had (the 100% response-rate modal) was gated on
        # localStorage['cavnar_congrats_shown'], so it re-fired on every new
        # browser and was lost on a cleared cache — a "this will not appear
        # again" promise the storage could not keep.
        """CREATE TABLE IF NOT EXISTS milestones (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            kind            TEXT NOT NULL,
            key             TEXT NOT NULL,
            title           TEXT NOT NULL,
            body            TEXT,
            value           REAL,
            data            TEXT,
            notified_at     TEXT,
            seen_at         TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_milestones ON milestones(restaurant_id, key)",
        "CREATE INDEX IF NOT EXISTS idx_milestones_restaurant ON milestones(restaurant_id, created_at)",

        # The accountability loop: an issue has an owner, is acknowledged,
        # is resolved, and escalates when nobody picks it up. Asked for by a
        # regional manager in so many words ("AI alerts via phone to hold
        # local managers accountable"); nothing in the product assigned,
        # acknowledged or escalated anything before this.
        """CREATE TABLE IF NOT EXISTS ops_issues (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id         INTEGER NOT NULL REFERENCES restaurants(id),
            kind                  TEXT NOT NULL,
            source_key            TEXT,
            title                 TEXT NOT NULL,
            detail                TEXT,
            severity              TEXT NOT NULL DEFAULT 'normal',
            assignee_contact_id   INTEGER,
            assignee_name         TEXT,
            status                TEXT NOT NULL DEFAULT 'open',
            created_by            INTEGER,
            created_at            TEXT NOT NULL DEFAULT (datetime('now')),
            notified_at           TEXT,
            acknowledged_at       TEXT,
            resolved_at           TEXT,
            resolution_note       TEXT,
            escalated_at          TEXT,
            escalation_contact_id INTEGER
        )""",
        "CREATE INDEX IF NOT EXISTS idx_issues_restaurant ON ops_issues(restaurant_id, status, created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_issues_source ON ops_issues(restaurant_id, source_key) "
        "WHERE source_key IS NOT NULL",
        # One link PER PERSON, not one per issue. With a single token on the
        # issue, escalating or reassigning had to replace it — and the
        # original assignee's link silently stopped working the moment
        # someone else was brought in.
        """CREATE TABLE IF NOT EXISTS issue_links (
            token_hash   TEXT PRIMARY KEY,
            issue_id     INTEGER NOT NULL REFERENCES ops_issues(id),
            contact_id   INTEGER,
            purpose      TEXT NOT NULL DEFAULT 'assignee',
            created_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_issue_links_issue ON issue_links(issue_id)",
        # Who issues go to at each location, and who they escalate to.
        # References consented alert_contacts rows rather than holding its
        # own phone numbers, so every number texted has already been through
        # the SMS consent path.
        """CREATE TABLE IF NOT EXISTS issue_routing (
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            role           TEXT NOT NULL,
            contact_id     INTEGER NOT NULL,
            escalate_after_minutes INTEGER DEFAULT 120,
            updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, role)
        )""",

        # Comps, voids and refunds per business date, from the POS. Stored
        # rather than re-queried, per RPOWER's own ask that integrators
        # "download and archive the data".
        """CREATE TABLE IF NOT EXISTS pos_loss_daily (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT NOT NULL,
            kind            TEXT NOT NULL,
            amount          REAL NOT NULL DEFAULT 0,
            events          INTEGER NOT NULL DEFAULT 0,
            by_approver     TEXT,
            provider        TEXT,
            synced_at       TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date, kind)
        )""",

        # Supplier invoices read from a photo. Every extracted line is kept
        # with what was actually applied, so a wrong price can be traced to
        # the invoice it came from.
        # Net sales so far today, captured hourly during service. Nothing
        # in this product could see a day while it was happening — the POS
        # syncs at 3am — and there is no hour-level history to compare a
        # running total against, so this builds one: after a few weeks the
        # same weekday at the same hour is a real baseline.
        """CREATE TABLE IF NOT EXISTS pos_intraday (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT NOT NULL,
            captured_hour   INTEGER NOT NULL,
            weekday         TEXT NOT NULL,
            net_sales       REAL NOT NULL,
            provider        TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, business_date, captured_hour)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_pos_intraday_profile ON pos_intraday(restaurant_id, weekday, captured_hour)",

        # "Not today" on an action-queue item. Deliberately a date, not a
        # dismissal: the queue is what is still open, and something snoozed
        # is still open tomorrow.
        """CREATE TABLE IF NOT EXISTS action_snoozes (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            key             TEXT NOT NULL,
            until_date      TEXT NOT NULL,
            snoozed_by      INTEGER,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, key)
        )""",

        # The manager's 60-second handoff at close. The product cannot see
        # today's service — the POS syncs at 3am — so the only account of
        # what happened is the person who was there. It feeds the next
        # morning's brief and the night's DSR. The six DSR columns
        # (equipment … influence) are added by ensure_columns.
        """CREATE TABLE IF NOT EXISTS close_outs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT NOT NULL,
            submitted_by    TEXT,
            user_id         INTEGER,
            went_well       TEXT,
            went_wrong      TEXT,
            eighty_sixed    TEXT,
            callouts        TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, business_date)
        )""",

        # An alert that arrived mid-service, held until the rush ends. See
        # notify.rush_release_at: interrupting a manager at 12:15 with a
        # two-star review helps nobody, and the same alert at 2:30 is acted on.
        """CREATE TABLE IF NOT EXISTS alert_holds (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            alert_type      TEXT NOT NULL,
            subject         TEXT,
            html            TEXT,
            sms_text        TEXT,
            review_id       INTEGER,
            release_at      TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            sent_at         TEXT,
            value           REAL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_alert_holds_due ON alert_holds(sent_at, release_at)",
        "CREATE INDEX IF NOT EXISTS idx_alert_holds_created ON alert_holds(created_at)",

        """CREATE TABLE IF NOT EXISTS invoice_imports (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            supplier        TEXT,
            invoice_date    TEXT,
            image_sha       TEXT,
            lines_json      TEXT,
            applied_json    TEXT,
            created_by      INTEGER,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            applied_at      TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_invoice_imports_restaurant ON invoice_imports(restaurant_id, created_at)",

        # One stored root-cause diagnosis per (restaurant, category, window).
        #
        # This is the step the module never took. It knew "food quality is
        # your most-mentioned complaint (11)" and stopped there, which is a
        # fact the owner already had. The diagnosis is what a consultant adds
        # after that sentence: the likely operational cause, the reviews it
        # rests on, the figure from another module that supports or
        # contradicts it, an alternative explanation, and what would settle
        # which one is right.
        #
        # Stored rather than recomputed because it is a Sonnet call over a
        # cluster of reviews, it changes on the timescale of days, and both
        # clients plus the weekly digest read the same answer.
        """CREATE TABLE IF NOT EXISTS review_diagnoses (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id        INTEGER NOT NULL REFERENCES restaurants(id),
            category             TEXT    NOT NULL,
            window_days          INTEGER NOT NULL,
            mention_count        INTEGER NOT NULL DEFAULT 0,
            cause                TEXT    NOT NULL,
            alternative_cause    TEXT,
            evidence_review_ids  TEXT,   -- JSON list of review ids the cause rests on
            operational_evidence TEXT,   -- JSON list of {module, metric, value} from other modules
            confidence           TEXT,   -- high|medium|low
            what_would_confirm   TEXT,
            recommended_action   TEXT,
            expected_outcome     TEXT,
            revenue_at_risk_low  REAL,
            revenue_at_risk_high REAL,
            generated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, category, window_days)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_review_diagnoses ON review_diagnoses(restaurant_id, generated_at)",

        # ── Food cost intelligence ─────────────────────────────────────────
        #
        # inventory_history was written by exactly one thing: the AI insight,
        # on page render. So the weekly waste series, the multi-week price
        # trends, the price-spike alert and the opening/closing snapshots
        # behind food cost % were all functions of whether the owner happened
        # to open the tab. A week nobody looked at is not a zero in the trend
        # chart, it is ABSENT from it. `source` says which rows were written
        # by the scheduled job and which by a page view, so a render-time
        # write can upsert on top of the day's scheduled row instead of
        # competing with it.
        "ALTER TABLE inventory_history ADD COLUMN source TEXT",
        "ALTER TABLE inventory_history ADD COLUMN inv_value REAL",
        # week_end is queried per restaurant, ordered, and bucketed by ISO
        # week on every trend read; it had no index at all.
        "CREATE INDEX IF NOT EXISTS idx_inv_history_rid_week ON inventory_history(restaurant_id, week_end)",

        # One stored root-cause diagnosis per restaurant per window.
        #
        # The module could say "waste is $420 this week" and stopped there.
        # Nothing said WHY the cost moved, and no prompt in the food-cost path
        # asked. This is the CFO's paragraph: the driver, what it cost, the
        # alternative explanation, what would confirm it, and the reviews of
        # the ledger it rests on.
        """CREATE TABLE IF NOT EXISTS food_cost_diagnoses (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id        INTEGER NOT NULL REFERENCES restaurants(id),
            window_days          INTEGER NOT NULL,
            headline             TEXT    NOT NULL,
            cause                TEXT    NOT NULL,
            alternative_cause    TEXT,
            what_would_confirm   TEXT,
            drivers_json         TEXT,   -- the ranked cost drivers this rests on
            operational_evidence TEXT,   -- JSON [{module, metric, value}]
            confidence           TEXT,   -- high|medium|low
            recommended_action   TEXT,
            expected_outcome     TEXT,
            dollars_at_stake     REAL,
            generated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, window_days)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_food_cost_diagnoses ON food_cost_diagnoses(restaurant_id, generated_at)",

        # Every forecast this module states, stored so it can be scored.
        #
        # The weekly waste FORECAST line was emitted and never compared to
        # what happened. A forecast nobody scores is a claim with no cost to
        # being wrong, which is the opposite of what a CFO's projection is
        # for. `actual` and `error_pct` are filled in by the scheduler once
        # the period it predicted has closed.
        """CREATE TABLE IF NOT EXISTS forecast_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            kind           TEXT    NOT NULL,   -- 'waste_week' | 'profitability_month'
            horizon_end    TEXT    NOT NULL,   -- the date this predicts through
            predicted      REAL    NOT NULL,
            basis          TEXT,
            actual         REAL,
            error_pct      REAL,
            scored_at      TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, kind, horizon_end)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_forecast_log ON forecast_log(restaurant_id, kind, horizon_end)",
        # Signed error, for the calibration loop (food_cost_intelligence).
        # After the CREATE, so a fresh database gets the column too.
        "ALTER TABLE forecast_log ADD COLUMN signed_error_pct REAL",
    ]
    try:
        for m in migrations:
            # "no such table": a few of these touch a table an init_* helper
            # creates later in boot (users, on a fresh file).
            _apply_migration(conn, m, also_tolerate=("no such table",))
        conn.commit()
    except Exception:
        conn.close()
        raise
    _migrate_reviews_unique(conn)
    _migrate_reviews_platform_check(conn)
    _ensure_place_id_uniqueness(conn)
    # Chats existed before conversations did — fold any pre-conversation
    # messages into one chat per restaurant so they show up in history.
    try:
        conn.row_factory = sqlite3.Row
        _adopt_legacy_ask_messages(conn)
    except Exception as e:
        print(f"ask_cavnar legacy adoption skipped: {e}")
    conn.close()
    # Ensure any columns managed by ensure_columns() are present before seeding.
    # On THIS database: a bare ensure_columns() migrated the default
    # DB_PATH as well, so the restore drill's init_db(scratch) ran a
    # migration on production (DATA-44).
    ensure_columns(db_path)
    # The tables that grew their own init_* helper after day one. Creating
    # them here means a request path never has to: a CREATE TABLE IF NOT
    # EXISTS on every read took SQLite's write lock for nothing.
    for _init in (init_two_fa_backup_codes, init_email_log, init_staff_notes,
                  init_staff_availability, init_team_messages, init_task_management,
                  init_staff_capabilities, init_manual_team_members, init_shift_profiles,
                  init_ask_memory, init_capability_changes, init_onboarding_emails,
                  init_competitor_snapshots, init_ai_visibility_queries,
                  init_staff_settings, init_demand_signals, init_schedule_versions,
                  init_shift_requests):
        _init(db_path)
    from schedule_intel import init_schedule_intel
    init_schedule_intel(db_path)
    # The week's generation arm and per-restaurant pins (schedule_experiments, audit #50).
    from schedule_experiments import init_schedule_experiments
    init_schedule_experiments(db_path)
    # One identity and event trail for every recommendation (rec_ledger).
    from rec_ledger import init_rec_ledger
    init_rec_ledger(db_path)
    # The rec-ROI columns on trackers written before them (module from the
    # recommendation's own rec_instances row, so after the ledger exists).
    from outcomes import init_outcomes
    init_outcomes(db_path)
    # The nightly Daily Sales Report: reports, searchable metrics, budgets,
    # the POS-department → DSR-category map (dsr/).
    from dsr import init_dsr
    init_dsr(db_path)
    # One stored AI read per restaurant and data fingerprint, shared by web
    # and iOS (insight_store), and the reprice decisions record
    # (menu_intelligence) — audit #22 / #26 / #41.
    from insight_store import init_insight_store
    init_insight_store(db_path)
    from menu_intelligence import init_menu_intelligence
    init_menu_intelligence(db_path)
    # Job claims, runs, failures, async jobs and the scheduler lease — at
    # boot, not on each claim (DATA-6).
    import ops as _ops
    _ops.init_ops(db_path)
    # Runs after ensure_columns() so organization_id exists to write into.
    backfill_organizations(db_path=db_path)
    print(f"Database initialised at {db_path}")


def _auto_seed_demo_clients():
    """The demo accounts live in demo_seed.py; this name stays for the boot block."""
    import demo_seed
    return demo_seed._auto_seed_demo_clients()


def _seed_simple_ejs(db_path: str = DB_PATH):
    """demo_seed._seed_simple_ejs, kept here because tests reach it as models._seed_simple_ejs."""
    import demo_seed
    return demo_seed._seed_simple_ejs(db_path)


def _seed_gia_mia(db_path: str = DB_PATH):
    import demo_seed
    return demo_seed._seed_gia_mia(db_path)


# ── Restaurant CRUD ───────────────────────────────────────────────────────────

# Alert-configuration fields on the Restaurant dataclass (models.py:~409-441)
# predate this INSERT and were never added to it — a value passed to any of
# these at creation (e.g. Restaurant(al_1star_sms=1, ...)) was silently
# dropped; the new row got whatever the column's own ALTER TABLE ... DEFAULT
# is regardless. update_restaurant()'s whitelist already has all of these
# correctly; this list and its VALUES are built from getattr(r, ...) rather
# than typed out positionally, since a 40+ item literal tuple is exactly
# where a hand-counted "?" placeholder silently drifts from its value.
_ALERT_CONFIG_FIELDS = (
    "alert_1star", "alert_2star", "alert_3star", "alert_health", "alert_neg_spike",
    "alert_negative_trend", "alert_no_response", "alert_5star",
    "alert_rating_threshold", "alert_rating_floor", "alert_labor_over",
    "alert_any_review", "alert_resp_approved",
    "urgent_via_email", "urgent_via_sms",
    "al_health_email", "al_health_sms", "al_health_push",
    "al_1star_email", "al_1star_sms", "al_1star_push",
    "al_2star_email", "al_2star_sms", "al_2star_push",
    "al_3star_email", "al_3star_sms", "al_3star_push",
    "al_5star_email", "al_5star_sms", "al_5star_push",
    "al_spike_email", "al_spike_sms", "al_spike_push",
    "al_unres_email", "al_unres_sms", "al_unres_push",
)


def create_restaurant(r: Restaurant, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    alert_cols = ", ".join(_ALERT_CONFIG_FIELDS)
    alert_qs = ", ".join("?" * len(_ALERT_CONFIG_FIELDS))
    alert_vals = tuple(getattr(r, f) for f in _ALERT_CONFIG_FIELDS)
    cur = conn.execute(f"""
        INSERT INTO restaurants (name, owner_email, google_place_id, yelp_business_id,
            voice_notes, neighborhood, vibe, known_for, sign_off_name, never_say,
            hourly_rate, labor_target_pct, stripe_customer_id,
            location_group, location_name, pos_system, reviews_live, billing_status, is_demo,
            service_tier, module_reviews, module_labor, module_inventory, module_marketing,
            owner_name, owner_phone, digest_day, digest_enabled, created_at, timezone,
            {alert_cols})
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, {alert_qs})
    """, (r.name, r.owner_email, r.google_place_id, r.yelp_business_id,
          r.voice_notes, r.neighborhood, r.vibe, r.known_for,
          r.sign_off_name, r.never_say, r.hourly_rate, r.labor_target_pct,
          r.stripe_customer_id, r.location_group, r.location_name, r.pos_system, r.reviews_live, r.billing_status, r.is_demo,
          r.service_tier,
          r.module_reviews, r.module_labor, r.module_inventory,
          r.module_marketing, r.owner_name, r.owner_phone,
          r.digest_day, r.digest_enabled, r.created_at,
          r.timezone or "America/Chicago") + alert_vals)
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


class StaleWrite(RuntimeError):
    """Raised when a caller's copy of a restaurant row has moved on.

    Only ever raised for callers that OPT IN by passing expected_version —
    see update_restaurant. Everything else keeps last-write-wins, which is
    correct for the many single-field writes in this codebase (a POS sync
    stamping toast_last_synced has nothing to conflict with).
    """

    # Our own wording, safe to hand a client as-is (routes use this, never
    # str(e) — see tests/test_intel_integrity.py).
    user_message = ("Someone else changed these settings while you were editing. "
                    "Reload and make your change again.")

    def __init__(self, current_version):
        super().__init__(self.user_message)
        self.current_version = current_version


# Process-local caches built FROM a restaurant's row (home_brief's payload,
# ask_cavnar's context) register here, and update_restaurant tells them the
# row changed. A settings save left Home and Ask answering from the old row
# for up to a minute (DATA-39). A registry rather than imports, so the data
# layer does not reach up into the modules that read it.
_restaurant_change_listeners = []


def on_restaurant_change(fn):
    if fn not in _restaurant_change_listeners:
        _restaurant_change_listeners.append(fn)
    return fn


def _notify_restaurant_change(restaurant_id):
    for fn in list(_restaurant_change_listeners):
        try:
            fn(restaurant_id)
        except Exception as e:
            print(f"[models] restaurant-change listener {getattr(fn, '__name__', fn)} failed: {e}")


def expected_version_from(data) -> Optional[int]:
    """The `expected_version` a whole-form save carries, or None.

    Settings routes never passed one, so update_restaurant's compare-and-
    swap was dead code: a stale phone form silently reverted never_say — the
    only gate on auto-published replies — saved a minute earlier from the
    web (DATA-28). A route passes this through; a client that sends the
    row_version it loaded gets a 409 instead of a silent revert, and one
    that sends nothing keeps last-write-wins."""
    v = (data or {}).get("expected_version")
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def restaurant_version(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """The row's current version, for a caller that wants to detect a
    conflicting write later."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT row_version FROM restaurants WHERE id=?",
                           (restaurant_id,)).fetchone()
        return int((row["row_version"] if row and row["row_version"] is not None else 0))
    except Exception:
        return 0
    finally:
        conn.close()


# Columns _restaurant_from_row converts with int()/float(). A value that
# does not convert makes the row fail to hydrate, and get_all_restaurants
# then skips it — the restaurant leaves every scheduled job (DATA-43). So a
# write that would store one is refused here instead.
_NUMERIC_RESTAURANT_FIELDS = {"week_start_day": int, "monthly_revenue_target": float}


def _check_numeric_fields(updates):
    for k, cast in _NUMERIC_RESTAURANT_FIELDS.items():
        v = updates.get(k)
        if k not in updates or v is None or isinstance(v, (int, float)):
            continue
        if isinstance(v, str) and not v.strip():
            continue
        try:
            cast(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k} must be a number, not {v!r}") from None


def update_restaurant(restaurant_id: int, fields: dict, db_path: str = DB_PATH,
                      expected_version: int = None):
    """Update any restaurant fields by dict.

    `expected_version` makes the write conditional: if the row has changed
    since the caller read it, nothing is written and StaleWrite is raised.
    Two managers on the settings screen, or a manual edit landing during a
    POS sync, otherwise silently discard one side — there was no version
    column and no compare-and-swap anywhere on this table, while the sales
    audit tool three files away has had an optimistic version counter since
    it shipped.

    Omitting it keeps the previous behaviour exactly.
    """
    allowed = {
        "name","owner_email","google_place_id","yelp_business_id","voice_notes",
        "neighborhood","vibe","known_for","sign_off_name","never_say",
        "hourly_rate","labor_target_pct","week_start_day","role_strength_json","shift_leader_rules_json","quality_weights_json","monthly_revenue_target","hours_notes","role_rates_json","close_times_json","role_close_buffer_json","stripe_customer_id","docusign_envelope_id","contract_status","location_group","location_name","pos_system","inventory_frequency","delivery_days","inventory_notes","food_cost_target","waste_target_pct","inventory_updated_at","temp_password","ig_token","ig_user_id","fb_page_token","fb_page_id","ig_token_expires","fb_token_expires","competitor_intel","competitor_updated_at","reviews_live","billing_status","is_demo","internal_notes","gmb_access_token","gmb_refresh_token","gmb_account_id","gmb_location_id","gmb_token_expires",
        "service_tier","module_reviews","module_labor","module_inventory","module_marketing",
        "last_active_tab","last_activity","owner_name","owner_phone","digest_day","digest_enabled","menu_notes","menu_url","skip_holidays","custom_competitors",
        "two_fa_enabled","two_fa_code","two_fa_expires","two_fa_device_token","two_fa_pending","two_fa_method","login_notify","staff_signin_notify","marketing_emails_opt_out","mailing_address","monthly_review_enabled","timezone","onboarding_dismissed",
        "alert_health_bypass_quiet","alert_food_waste","alert_ai_visibility_drop","alert_competitor_move","alert_extra_emails","push_sound",
        "fiscal_week_start_dow","fiscal_year_start","fiscal_period_scheme","dsr_enabled","dsr_deadline_hour","dsr_notify",
        "auto_approve_earned","auto_publish_schedule","auto_order_trusted","weekly_plan_enabled","send_delay_minutes",
        "auto_approve_5star","auto_approve_4star","auto_approve_daily_cap","auto_approve_paused","open_times_json",
        "compliance_json","role_floors_json",
        "jurisdiction","role_arrival_json","role_close_min_json","role_requirements_json","foh_roles_json","patio_roles_json","role_cross_training_json",
        "trim_to_budget","reservation_provider","reservation_api_key",
        "response_language","tone_preset","data_retention_months",
        "toast_client_id","toast_client_secret","toast_restaurant_guid",
        "rpower_token","rpower_cg","rpower_store_mid","rpower_store_name",
        "rpower_last_synced","rpower_sync_error","rpower_verified_at",
        "toast_access_token","toast_token_expires","toast_last_synced","toast_sync_error",
        "square_access_token","square_location_id","square_last_synced","square_sync_error",
        "clover_merchant_id","clover_api_token","clover_last_synced","clover_sync_error",
        "gbp_rating","gbp_review_count","gbp_rating_updated_at",
        "alert_1star","alert_2star","alert_3star","alert_health","alert_neg_spike","alert_negative_trend","alert_no_response",
        "alert_5star","alert_rating_threshold","alert_rating_floor","alert_labor_over",
        "alert_any_review","alert_resp_approved",
        "urgent_via_email","urgent_via_sms",
        "al_health_email","al_health_sms","al_health_push",
        "al_1star_email","al_1star_sms","al_1star_push",
        "al_2star_email","al_2star_sms","al_2star_push",
        "al_3star_email","al_3star_sms","al_3star_push",
        "al_5star_email","al_5star_sms","al_5star_push",
        "al_spike_email","al_spike_sms","al_spike_push",
        "al_unres_email","al_unres_sms","al_unres_push",
        "changelog_seen_at","notifications_seen_at", "category",
        "alert_quiet_start","alert_quiet_end","alert_max_per_day",
        "brand_name","brand_color","brand_logo_url",
        "section_count","daypart_split","delivery_pct","role_minimums_json","sched_notes","email_theme",
        "latitude","longitude","weather_cache_json","weather_cached_at",
        "geocode_failed_at",
        "alert_hold_during_service", "preshift_nudge_hour",
        "morning_brief_enabled", "morning_brief_hour", "briefing_level", "paused_until",
        "auto_draft_schedule", "external_scheduling_tool",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    _check_numeric_fields(updates)
    # OAuth/POS credentials are encrypted at rest (credentials.py); every
    # reader sees plaintext through get_restaurant.
    import credentials as _cred
    updates = _cred.encrypt_fields(updates)
    set_clause = ", ".join(f"{k}=?" for k in updates)
    # The version bump rides in the SAME statement as the update, so it
    # cannot be skipped by an early return and cannot race a reader.
    set_clause += ", row_version=COALESCE(row_version,0)+1"
    values = list(updates.values())
    conn = get_conn(db_path)
    try:
        if updates.get("docusign_envelope_id"):
            # Keep every envelope ever sent, so signing an older one still
            # counts (MOD-BIL-6). Best-effort: a missing table never blocks
            # the update itself.
            try:
                conn.execute("INSERT OR IGNORE INTO docusign_envelopes (envelope_id, restaurant_id) VALUES (?,?)",
                             (updates["docusign_envelope_id"], restaurant_id))
            except Exception as _env_e:
                print(f"[update_restaurant] envelope history not recorded for {restaurant_id}: {_env_e}")
        if expected_version is None:
            conn.execute(f"UPDATE restaurants SET {set_clause} WHERE id=?",
                         values + [restaurant_id])
        else:
            cur = conn.execute(
                f"UPDATE restaurants SET {set_clause} "
                f"WHERE id=? AND COALESCE(row_version,0)=?",
                values + [restaurant_id, int(expected_version)])
            if cur.rowcount == 0:
                row = conn.execute("SELECT row_version FROM restaurants WHERE id=?",
                                   (restaurant_id,)).fetchone()
                conn.close()
                raise StaleWrite(int((row["row_version"] or 0) if row else 0))
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    # get_restaurant is memoised for the life of a request, so a settings POST
    # that writes and then re-reads in the same request would otherwise be
    # served the row as it was before its own write.
    _invalidate_request_cache(restaurant_id)
    # And the process caches built from this row (Home, Ask) — DATA-39.
    _notify_restaurant_change(restaurant_id)
    # Keep the organization key in step with the group name an admin typed.
    # location_group remains the field the admin console writes; this is what
    # turns that string into the real grouping key without the console having
    # to know organizations exist yet.
    if "location_group" in updates or "owner_email" in updates:
        try:
            _sync_restaurant_organization(restaurant_id, db_path=db_path)
        except Exception:
            pass


def _sync_restaurant_organization(restaurant_id: int, db_path: str = DB_PATH):
    """Point a restaurant at the organization its (group, owner) names."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT location_group, owner_email FROM restaurants WHERE id=?",
            (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return
    group = ((row["location_group"] or "") or "").strip()
    if not group:
        set_restaurant_organization(restaurant_id, None, db_path=db_path)
        return
    org = get_or_create_organization(group, row["owner_email"], db_path=db_path)
    if org:
        set_restaurant_organization(restaurant_id, org["id"], db_path=db_path)


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
    _invalidate_request_cache(restaurant_id)
    return now


def delete_restaurant(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Remove a restaurant and every row that belongs to it, in one
    transaction. Returns {table: rows_deleted}.

    There was no deletion routine: deletion_requested_at was a flag nothing
    acted on, and DELETE FROM restaurants failed on the ~70 child tables
    that reference it (DATA-61). This is the routine. It is deliberately
    NOT called by anything yet — when to run it after a request (the 30-day
    notice request_account_deletion describes), who confirms it, and what
    happens to Stripe and to the nightly snapshots that still hold the rows
    are decisions for the operator, not for a background job to make.

    What goes: every row in every table with a restaurant_id column, the
    restaurants row, and then any row left pointing (by foreign key) at a
    row this removed — sessions of a deleted login, versions of a deleted
    schedule — until none is left. A login whose home restaurant this is but
    who still has an active membership elsewhere is re-homed there rather
    than deleted. Foreign-key violations that existed before the call are
    not touched.
    """
    rid = int(restaurant_id)
    conn = get_conn(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")          # must be set outside the transaction
    deleted = {}
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        had_orphans = {tuple(v) for v in conn.execute("PRAGMA foreign_key_check")}
        conn.execute("BEGIN IMMEDIATE")
        if "memberships" in tables:
            for uid, other in conn.execute(
                    "SELECT u.id, (SELECT m.restaurant_id FROM memberships m WHERE m.user_id=u.id "
                    "  AND m.restaurant_id<>? AND COALESCE(m.is_active,1)=1 ORDER BY m.restaurant_id LIMIT 1) "
                    "FROM users u WHERE u.restaurant_id=?", (rid, rid)).fetchall():
                if other is not None:
                    conn.execute("UPDATE users SET restaurant_id=? WHERE id=?", (other, uid))
        for t in tables:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')}
            if "restaurant_id" in cols:
                n = conn.execute(f'DELETE FROM "{t}" WHERE restaurant_id=?', (rid,)).rowcount
                if n:
                    deleted[t] = n
        n = conn.execute("DELETE FROM restaurants WHERE id=?", (rid,)).rowcount
        if n:
            deleted["restaurants"] = n
        for _ in range(20):
            orphans = [v for v in conn.execute("PRAGMA foreign_key_check")
                       if tuple(v) not in had_orphans and v[1] is not None]
            if not orphans:
                break
            for table, rowid, _parent, _fk in orphans:
                if conn.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rowid,)).rowcount:
                    deleted[table] = deleted.get(table, 0) + 1
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        finally:
            conn.close()
    _invalidate_request_cache(rid)
    return deleted


def _request_cache():
    """This request's read memo, or None when there is no request.

    Audit #17 measured ONE web Home load at 116 queries and 87 SQLite
    connection opens, of which 35 were get_restaurant() fetching the SAME row
    35 times. Nothing was wrong with any individual call; there was just no
    scope in which "I already read this" could be true.

    Deliberately scoped to the Flask request and nowhere else. models.py is
    imported by the scheduler, the test suite and one-off scripts, none of
    which have an app context — they get None here and behave exactly as
    before. A process-lifetime cache would be wrong for all three: a job loop
    that runs for an hour must see a restaurant's row change under it.
    """
    try:
        from flask import g, has_app_context
        if not has_app_context():
            return None
        cache = getattr(g, "_cavnar_read_cache", None)
        if cache is None:
            cache = {}
            g._cavnar_read_cache = cache
        return cache
    except Exception:
        # No Flask, or a context torn down mid-call. Uncached is always correct.
        return None


def _invalidate_request_cache(restaurant_id=None):
    """Drop memoised reads after a write, so a read-after-write in the same
    request sees the write. Called by update_restaurant."""
    cache = _request_cache()
    if cache is None:
        return
    if restaurant_id is None:
        cache.clear()
        return
    for key in [k for k in cache if k[0] == "restaurant" and k[1] == int(restaurant_id)]:
        cache.pop(key, None)


def get_restaurant(restaurant_id: int, db_path: str = DB_PATH) -> Optional[Restaurant]:
    # Memoised per request. Restaurant objects are read-only by convention
    # everywhere in this codebase (the only dataclass mutations anywhere are
    # on Review inside save_reviews), so handing the same instance to two
    # callers in one request cannot alias.
    cache = _request_cache()
    ckey = ("restaurant", int(restaurant_id), db_path)
    if cache is not None and ckey in cache:
        return cache[ckey]

    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    conn.close()
    if not row:
        if cache is not None:
            cache[ckey] = None
        return None
    result = _restaurant_from_row(row)
    if cache is not None:
        cache[ckey] = result
    return result


def _restaurant_from_row(row) -> Restaurant:
    """Row -> Restaurant. Split out so callers that already hold the row can
    hydrate from it instead of re-querying by id (see get_all_restaurants)."""
    # One keys() call per row, not one per optional column: sqlite3.Row
    # builds a fresh list on every keys(), ~190 of them per restaurant,
    # for every restaurant every scheduled job hydrates (MOD-PERF-2).
    if not isinstance(row, dict):
        row = dict(zip(row.keys(), row))
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
        waste_target_pct=row["waste_target_pct"] if "waste_target_pct" in row.keys() and row["waste_target_pct"] is not None else None,
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
        staff_signin_notify=row["staff_signin_notify"] if "staff_signin_notify" in row.keys() else 0,
        marketing_emails_opt_out=row["marketing_emails_opt_out"] if "marketing_emails_opt_out" in row.keys() else 0,
        mailing_address=row["mailing_address"] if "mailing_address" in row.keys() else None,
        monthly_review_enabled=row["monthly_review_enabled"] if "monthly_review_enabled" in row.keys() else 1,
        alert_health_bypass_quiet=row["alert_health_bypass_quiet"] if "alert_health_bypass_quiet" in row.keys() else 0,
        alert_food_waste=row["alert_food_waste"] if "alert_food_waste" in row.keys() else 0,
        alert_ai_visibility_drop=row["alert_ai_visibility_drop"] if "alert_ai_visibility_drop" in row.keys() else 0,
        alert_competitor_move=row["alert_competitor_move"] if "alert_competitor_move" in row.keys() and row["alert_competitor_move"] is not None else 1,
        fiscal_week_start_dow=row["fiscal_week_start_dow"] if "fiscal_week_start_dow" in row.keys() else None,
        fiscal_year_start=row["fiscal_year_start"] if "fiscal_year_start" in row.keys() else None,
        fiscal_period_scheme=row["fiscal_period_scheme"] if "fiscal_period_scheme" in row.keys() else None,
        dsr_enabled=row["dsr_enabled"] if "dsr_enabled" in row.keys() and row["dsr_enabled"] is not None else 1,
        dsr_deadline_hour=row["dsr_deadline_hour"] if "dsr_deadline_hour" in row.keys() and row["dsr_deadline_hour"] is not None else 4,
        dsr_notify=row["dsr_notify"] if "dsr_notify" in row.keys() and row["dsr_notify"] is not None else 0,
        alert_extra_emails=row["alert_extra_emails"] if "alert_extra_emails" in row.keys() else None,
        push_sound=row["push_sound"] if "push_sound" in row.keys() and row["push_sound"] is not None else 1,
        auto_approve_5star=row["auto_approve_5star"] if "auto_approve_5star" in row.keys() else 0,
        auto_approve_4star=row["auto_approve_4star"] if "auto_approve_4star" in row.keys() else 0,
        auto_approve_earned=(row["auto_approve_earned"] if "auto_approve_earned" in row.keys()
                             and row["auto_approve_earned"] is not None else 0),
        auto_publish_schedule=(row["auto_publish_schedule"] if "auto_publish_schedule" in row.keys()
                               and row["auto_publish_schedule"] is not None else 0),
        auto_order_trusted=(row["auto_order_trusted"] if "auto_order_trusted" in row.keys()
                            and row["auto_order_trusted"] is not None else 0),
        weekly_plan_enabled=(row["weekly_plan_enabled"] if "weekly_plan_enabled" in row.keys()
                             and row["weekly_plan_enabled"] is not None else 0),
        send_delay_minutes=(row["send_delay_minutes"] if "send_delay_minutes" in row.keys()
                            and row["send_delay_minutes"] is not None else 0),
        auto_approve_daily_cap=row["auto_approve_daily_cap"] if "auto_approve_daily_cap" in row.keys() and row["auto_approve_daily_cap"] is not None else 5,
        auto_approve_paused=row["auto_approve_paused"] if "auto_approve_paused" in row.keys() else 0,
        open_times_json=row["open_times_json"] if "open_times_json" in row.keys() else None,
        compliance_json=row["compliance_json"] if "compliance_json" in row.keys() else None,
        role_floors_json=row["role_floors_json"] if "role_floors_json" in row.keys() else None,
        jurisdiction=row["jurisdiction"] if "jurisdiction" in row.keys() else None,
        role_arrival_json=row["role_arrival_json"] if "role_arrival_json" in row.keys() else None,
        role_close_min_json=row["role_close_min_json"] if "role_close_min_json" in row.keys() else None,
        role_requirements_json=row["role_requirements_json"] if "role_requirements_json" in row.keys() else None,
        foh_roles_json=row["foh_roles_json"] if "foh_roles_json" in row.keys() else None,
        patio_roles_json=row["patio_roles_json"] if "patio_roles_json" in row.keys() else None,
        role_cross_training_json=row["role_cross_training_json"] if "role_cross_training_json" in row.keys() else None,
        trim_to_budget=(row["trim_to_budget"] if row["trim_to_budget"] is not None else 1) if "trim_to_budget" in row.keys() else 1,
        reservation_provider=row["reservation_provider"] if "reservation_provider" in row.keys() else None,
        reservation_api_key=row["reservation_api_key"] if "reservation_api_key" in row.keys() else None,
        response_language=row["response_language"] if "response_language" in row.keys() else None,
        tone_preset=row["tone_preset"] if "tone_preset" in row.keys() else None,
        data_retention_months=row["data_retention_months"] if "data_retention_months" in row.keys() and row["data_retention_months"] is not None else 0,
        alert_1star=row["alert_1star"] if "alert_1star" in row.keys() else 1,
        alert_2star=row["alert_2star"] if "alert_2star" in row.keys() else 0,
        alert_health=row["alert_health"] if "alert_health" in row.keys() else 1,
        alert_neg_spike=row["alert_neg_spike"] if "alert_neg_spike" in row.keys() else 1,
        alert_3star=row["alert_3star"] if "alert_3star" in row.keys() else 0,
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
        al_3star_email=row["al_3star_email"] if "al_3star_email" in row.keys() else 1,
        al_3star_sms=row["al_3star_sms"]     if "al_3star_sms"   in row.keys() else 0,
        al_3star_push=row["al_3star_push"]   if "al_3star_push"  in row.keys() else 1,
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
        rpower_token=row["rpower_token"] if "rpower_token" in row.keys() else None,
        rpower_cg=row["rpower_cg"] if "rpower_cg" in row.keys() else None,
        rpower_store_mid=row["rpower_store_mid"] if "rpower_store_mid" in row.keys() else None,
        rpower_store_name=row["rpower_store_name"] if "rpower_store_name" in row.keys() else None,
        rpower_last_synced=row["rpower_last_synced"] if "rpower_last_synced" in row.keys() else None,
        rpower_sync_error=row["rpower_sync_error"] if "rpower_sync_error" in row.keys() else None,
        rpower_verified_at=row["rpower_verified_at"] if "rpower_verified_at" in row.keys() else None,
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
        category=row["category"] if "category" in row.keys() else None,
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
        geocode_failed_at=row["geocode_failed_at"]     if "geocode_failed_at" in row.keys() else None,
        preshift_nudge_hour=(row["preshift_nudge_hour"] if "preshift_nudge_hour" in row.keys()
                             and row["preshift_nudge_hour"] is not None else 0),
        alert_hold_during_service=(row["alert_hold_during_service"]
                                   if "alert_hold_during_service" in row.keys()
                                   and row["alert_hold_during_service"] is not None else 1),
        morning_brief_enabled=(row["morning_brief_enabled"] if "morning_brief_enabled" in row.keys()
                               and row["morning_brief_enabled"] is not None else 1),
        morning_brief_hour=(row["morning_brief_hour"] if "morning_brief_hour" in row.keys()
                            and row["morning_brief_hour"] is not None else 7),
        briefing_level=(row["briefing_level"] if "briefing_level" in row.keys()
                        and row["briefing_level"] else "normal"),
        paused_until=(row["paused_until"] if "paused_until" in row.keys() else None),
        auto_draft_schedule=(row["auto_draft_schedule"] if "auto_draft_schedule" in row.keys()
                             and row["auto_draft_schedule"] is not None else 0),
        external_scheduling_tool=row["external_scheduling_tool"] if "external_scheduling_tool" in row.keys() else None,
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

def _apply_review_edit(conn, r: "Review") -> tuple:
    """Update a stored review when its author has edited it.

    Returns (changed, downgraded) — downgraded meaning the guest lowered
    their own star rating, which is a reputation event the owner needs to
    hear about and which used to pass in complete silence: an edit is not a
    new row, so it never reached new_reviews, so no alert, push or webhook
    ever fired for a five-star dropped to one.

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
        return (False, False)
    old_rating = int(row["rating"] or 0)
    new_rating = int(r.rating or 0)
    same_rating = old_rating == new_rating
    same_text = (row["text"] or "").strip() == (r.text or "").strip()
    if same_rating and same_text:
        return (False, False)
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
    # Carried on the object so the alert can say "5★ → 1★" rather than just
    # "now 1★" (the dataclass has no column for it; the DB keeps the first
    # rating in original_rating).
    r.previous_rating = old_rating
    return (True, bool(new_rating and old_rating and new_rating < old_rating))


def _same_guest_review(row, r: "Review") -> bool:
    """Whether a stored row and an incoming review are one guest review seen
    through the other Google API. Same rating, same second written (Places
    `time` and GBP createTime are one instant; both are stored to the second
    in the restaurant's day), and the same author or the same words."""
    if int(row["rating"] or 0) != int(r.rating or 0):
        return False
    if (row["review_date"] or "")[:19] != (r.review_date or "")[:19] or not (r.review_date or ""):
        return False
    same_author = (row["author"] or "").strip().lower() == (r.author or "").strip().lower()
    same_text = (row["text"] or "").strip() == (r.text or "").strip()
    return same_author or same_text


def _cross_source_copy(conn, r: "Review"):
    """The stored row that is this Google review arriving through the OTHER
    API, or None (MOD-REV-3).

    Places keys a review google_<time>_<author url>; the Business Profile API
    keys it by its resource name. The same guest review fetched both ways was
    two rows: counted twice, alerted twice, and the Places row — the one the
    owner was most likely to approve — could never be posted to Google."""
    if r.platform != "google" or not r.review_date:
        return None
    if r.review_name:
        where = "review_name IS NULL AND external_id LIKE 'google_%'"
    elif (r.external_id or "").startswith("google_"):
        where = "review_name IS NOT NULL"
    else:
        return None
    rows = conn.execute(
        f"SELECT id, rating, author, text, review_date, review_name FROM reviews "
        f"WHERE restaurant_id=? AND platform='google' AND deleted_at IS NULL AND {where} "
        f"AND substr(review_date, 1, 19) = ?",
        (r.restaurant_id, (r.review_date or "")[:19])).fetchall()
    for row in rows:
        if _same_guest_review(row, r):
            return row
    return None


def save_reviews(reviews: list[Review], db_path: str = DB_PATH,
                 downgrades: list = None, rejected: list = None) -> tuple[int, list]:
    """Upsert reviews; skip ones this restaurant already has.

    Returns (new_count, new_review_objects). Pass a list as `downgrades` to
    also collect the reviews whose author LOWERED their own rating on this
    fetch — an out-parameter rather than a third return value so the two
    dozen existing `a, b = save_reviews(...)` call sites keep working.

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
    # Review.fetched_at's dataclass default is a hardcoded America/Chicago
    # stamp, while fetcher.py carefully resolves the RESTAURANT's timezone
    # for review_date — and fetched_at is the fallback axis every trend
    # query uses when review_date is missing, so the two disagreed by hours
    # for anyone outside Central. One lookup per batch (they share a
    # restaurant), applied only to rows about to be inserted.
    _tz_stamp = None
    if reviews:
        try:
            from zoneinfo import ZoneInfo as _ZI_sr
            from datetime import datetime as _dt_sr
            _r0 = get_restaurant(reviews[0].restaurant_id, db_path=db_path)
            _tz = _ZI_sr(getattr(_r0, "timezone", None) or "America/Chicago")
            _tz_stamp = _dt_sr.now(_tz).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            _tz_stamp = None
    for r in reviews:
        if _tz_stamp:
            r.fetched_at = _tz_stamp
        # One guest review, one row, whichever Google API brought it
        # (MOD-REV-3). A GBP copy of a stored Places row takes that row over
        # (its resource name is what a reply is posted to); a Places copy of
        # a stored GBP row is the same review again. Neither is new.
        try:
            twin = _cross_source_copy(conn, r)
        except Exception:
            twin = None
        if twin is not None:
            already_had += 1
            if r.review_name and not twin["review_name"]:
                conn.execute("UPDATE reviews SET external_id=?, review_name=?, source_updated_at=? "
                             "WHERE id=?", (r.external_id, r.review_name, r.source_updated_at, twin["id"]))
            r.id = twin["id"]
            continue
        try:
            # review_name and source_updated_at are stored now. They were on
            # the Review the GBP fetch built and never written, so no fetched
            # review could ever be posted to or retracted from Google.
            cur = conn.execute("""
                INSERT INTO reviews
                    (restaurant_id, platform, external_id, author, rating,
                     text, review_date, fetched_at, review_name, source_updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (r.restaurant_id, r.platform, r.external_id, r.author,
                  r.rating, r.text, r.review_date, r.fetched_at,
                  r.review_name or None, getattr(r, "source_updated_at", None)))
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
                    if r.review_name:
                        conn.execute("UPDATE reviews SET review_name=? WHERE restaurant_id=? AND platform=? "
                                     "AND external_id=? AND review_name IS NULL",
                                     (r.review_name, r.restaurant_id, r.platform, r.external_id))
                    _changed, _downgraded = _apply_review_edit(conn, r)
                    if _changed:
                        edited += 1
                    if _downgraded and downgrades is not None:
                        downgrades.append(r)
                except Exception as _ee:
                    unexpected.append((r.external_id, f"edit failed: {_ee}"))
            else:
                unexpected.append((r.external_id, str(e)))
    conn.commit()
    conn.close()
    if rejected is not None:
        # Same out-parameter shape as `downgrades`: rows refused for a reason
        # other than "this restaurant already has it" (DATA-21).
        rejected.extend(unexpected)
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


# A review that fails analysis or drafting is retried on the next cycle —
# but not forever. Without a ceiling a permanently-unanalysable review was
# re-sent to the model on all four fetch cycles a day, indefinitely, against
# the restaurant's own AI budget, with nothing anywhere saying so.
MAX_AI_ATTEMPTS = 5


def get_pending_analysis(restaurant_id: int, limit: int = 50,
                          db_path: str = DB_PATH) -> list[Review]:
    """Reviews fetched but not yet analysed by Claude, excluding the ones
    that have already failed MAX_AI_ATTEMPTS times."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT * FROM reviews
        WHERE restaurant_id=? AND processed=0
          AND COALESCE(analysis_attempts, 0) < ?
          AND deleted_at IS NULL      -- retired by the retention setting (DATA-60)
        ORDER BY fetched_at DESC LIMIT ?
    """, (restaurant_id, MAX_AI_ATTEMPTS, limit)).fetchall()
    conn.close()
    return [_row_to_review(r) for r in rows]


def get_pending_drafts(restaurant_id: int, limit: int = 50,
                        db_path: str = DB_PATH) -> list[Review]:
    """Analysed reviews that still need a response drafted."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT * FROM reviews
        WHERE restaurant_id=? AND processed=1 AND response_status='pending'
          AND COALESCE(draft_attempts, 0) < ?
          AND deleted_at IS NULL      -- retired by the retention setting (DATA-60)
        ORDER BY
            CASE urgency WHEN 'high' THEN 0 ELSE 1 END,
            fetched_at DESC
        LIMIT ?
    """, (restaurant_id, MAX_AI_ATTEMPTS, limit)).fetchall()
    conn.close()
    return [_row_to_review(r) for r in rows]


def record_ai_attempt(review_id: int, kind: str, db_path: str = DB_PATH):
    """Count one analysis/draft attempt against a review.

    Called on failure so a review that can never be processed stops being
    retried four times a day forever; get_pending_analysis and
    get_pending_drafts both skip rows past MAX_AI_ATTEMPTS, and
    count_stalled_reviews() reports what is sitting there.
    """
    col = "analysis_attempts" if kind == "analysis" else "draft_attempts"
    conn = get_conn(db_path)
    conn.execute(f"UPDATE reviews SET {col} = COALESCE({col}, 0) + 1 WHERE id=?", (review_id,))
    conn.commit()
    conn.close()


def count_stalled_reviews(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Reviews that gave up after MAX_AI_ATTEMPTS — the ones a human has to
    look at, rather than silently missing forever."""
    conn = get_conn(db_path)
    row = conn.execute("""
        SELECT
          SUM(processed=0 AND COALESCE(analysis_attempts,0) >= ?) AS unanalysed,
          SUM(processed=1 AND response_status='pending'
              AND COALESCE(draft_attempts,0) >= ?)               AS undrafted
        FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL
    """, (MAX_AI_ATTEMPTS, MAX_AI_ATTEMPTS, restaurant_id)).fetchone()
    conn.close()
    return {"unanalysed": (row["unanalysed"] or 0) if row else 0,
            "undrafted": (row["undrafted"] or 0) if row else 0}



def get_reviews_since(restaurant_id: int, since: str,
                       db_path: str = DB_PATH) -> list[Review]:
    """Reviews a guest wrote since `since` — the weekly digest's whole input.

    Two things were wrong. It windowed on `fetched_at`, so on a first connect
    the entire multi-year history arrives stamped with one timestamp and the
    first weekly digest reported every review the restaurant had ever received
    as "this week". And it never excluded soft-deleted rows, so a review the
    owner removed still shaped their digest's rating, sentiment split and top
    themes.

    Analysed or not (MOD-REV-17): it required processed=1, so a review the AI
    had not got to yet — a budget pause, a failed call — was missing from the
    week's count and average. A guest's rating is a fact before any model
    reads it; callers that need the analysis check `processed` themselves.
    """
    conn = get_conn(db_path)
    rows = conn.execute(f"""
        SELECT * FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL
          AND {REVIEW_TIME_AXIS_BARE} >= ?
        ORDER BY {REVIEW_TIME_AXIS_BARE} DESC
    """, (restaurant_id, since)).fetchall()
    conn.close()
    return [_row_to_review(r) for r in rows]


def update_analysis(review_id: int, sentiment: str, categories: list,
                     summary: str, urgency: str, db_path: str = DB_PATH,
                     entities: dict = None, specific_complaint: str = None,
                     severity: str = None):
    """Store the analyser's read of one review.

    entities/specific_complaint/severity are the operational dimension — the
    dish, the role, the daypart, the actual thing that went wrong, and how
    serious it is beyond the binary high/normal urgency. They default to None
    so every existing caller (and any review analysed before these columns
    existed) keeps working; the readers all treat absent as "not known" rather
    than as a value.
    """
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE reviews
        SET sentiment=?, categories=?, summary=?, urgency=?, processed=1,
            entities=?, specific_complaint=?, severity=?
        WHERE id=?
    """, (sentiment, json.dumps(categories), summary, urgency,
          json.dumps(entities) if entities else None,
          specific_complaint or None, severity or None, review_id))
    conn.commit()
    conn.close()


def update_draft(review_id: int, draft: str, db_path: str = DB_PATH,
                 needs_review: bool = False, review_reason: str = None):
    """Store a drafted reply.

    needs_review marks a draft that passed generation but states something
    the system cannot stand behind — see ai_guard.unsupported_commitments.
    It never blocks the owner from posting; it makes the reason visible
    before they do, and the auto-approve rule refuses to touch it.

    Never overwrites an approved or posted reply: a regenerate racing a
    publish used to flip a live Google reply back to "drafted" with other
    text (M-25). Returns True when the draft was stored.
    """
    conn = get_conn(db_path)
    cur = conn.execute("""
        UPDATE reviews
           SET draft_response=?, response_status='drafted',
               draft_needs_review=?, draft_review_reason=?
         WHERE id=? AND COALESCE(response_status, '') NOT IN ('posted', 'approved')
    """, (draft, 1 if needs_review else 0, review_reason, review_id))
    conn.commit()
    conn.close()
    return cur.rowcount == 1


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


# The drafts a bulk publish (Home's "Publish N replies", approve-all, Ask's
# approve_all_reviews) may post without anyone reading them one by one:
# recent, not urgent, and not flagged by the reply guard. It ignored all
# three, so a "Publish 1 reply" labelled from the last 30 days posted a
# 90-day-old urgent reply, or a flagged one promising a free dinner (M-2).
# One definition, read by the publish and by every count that labels it.
BULK_PUBLISHABLE_SQL = (
    "response_status='drafted' AND deleted_at IS NULL "
    "AND draft_response IS NOT NULL AND TRIM(draft_response) != '' "
    "AND COALESCE(draft_needs_review, 0) = 0 "
    "AND COALESCE(urgency, 'normal') != 'high' "
    "AND COALESCE(NULLIF(review_date, ''), fetched_at) >= date('now', ?)")


def bulk_publish_window() -> str:
    """The SQLite date modifier for BULK_PUBLISHABLE_SQL's recency bound."""
    from thresholds import REPLY_OWED_MAX_AGE_DAYS
    return f"-{int(REPLY_OWED_MAX_AGE_DAYS)} days"


def reply_queue_counts(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Drafted replies, split the way web and iOS Home both say them:
    `publishable` — what one bulk publish may post; `held` — recent drafts
    that are urgent or flagged, which a person reads one at a time; `older`
    — drafts on reviews past the reply window, history rather than owed."""
    conn = get_conn(db_path)
    try:
        win = bulk_publish_window()
        row = conn.execute(
            "SELECT "
            f" SUM(CASE WHEN {BULK_PUBLISHABLE_SQL} THEN 1 ELSE 0 END) AS publishable, "
            " SUM(CASE WHEN recent AND (COALESCE(draft_needs_review,0)=1 OR COALESCE(urgency,'normal')='high') "
            "     THEN 1 ELSE 0 END) AS held, "
            " SUM(CASE WHEN NOT recent THEN 1 ELSE 0 END) AS older "
            "FROM (SELECT *, COALESCE(NULLIF(review_date, ''), fetched_at) >= date('now', ?) AS recent "
            "      FROM reviews WHERE restaurant_id=? AND response_status='drafted' AND deleted_at IS NULL "
            "        AND draft_response IS NOT NULL AND TRIM(draft_response) != '')",
            (win, win, restaurant_id)).fetchone()
    finally:
        conn.close()
    return {"publishable": int((row and row["publishable"]) or 0),
            "held": int((row and row["held"]) or 0),
            "older": int((row and row["older"]) or 0)}


def claim_approval(review_id: int, restaurant_id: int, db_path: str = DB_PATH,
                   publishable_only: bool = False) -> bool:
    """Approve a drafted reply as a compare-and-set. True only for the one
    caller that moved THIS restaurant's live, drafted, non-empty reply to
    'approved'; everyone else gets False and nothing changes.

    approve_response is an unconditional UPDATE, so an approve of an
    already-posted, deleted, draftless or other restaurant's review returned
    200 and published or confirmed about nothing (MOD-REV-4), and two
    approves at once both went on to post to Google (MOD-REV-5). The WHERE
    clause is the lock: SQLite serialises the writes, only one matches.

    `publishable_only`: a bulk publish's claim, held to BULK_PUBLISHABLE_SQL
    at the moment of the write, so a draft regenerated into a flagged one
    between the batch's SELECT and its approve is not posted (M-2)."""
    conn = get_conn(db_path)
    try:
        if publishable_only:
            cur = conn.execute(
                "UPDATE reviews SET response_status='approved', approved_at=datetime('now') "
                f"WHERE id=? AND restaurant_id=? AND {BULK_PUBLISHABLE_SQL}",
                (review_id, restaurant_id, bulk_publish_window()))
            conn.commit()
            return cur.rowcount == 1
        cur = conn.execute("""
            UPDATE reviews
            SET response_status='approved', approved_at=datetime('now')
            WHERE id=? AND restaurant_id=? AND response_status IN ('drafted','pending')
              AND deleted_at IS NULL
              AND draft_response IS NOT NULL AND TRIM(draft_response) != ''
        """, (review_id, restaurant_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_log_sent ON email_log(sent_at)")   # prune_ledgers (DATA-40)
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

# ── Team messages (manager DMs) ─────────────────────────────────────────────
#
# Scoped to the people who actually have a login — this product only ever
# creates an account for an owner or someone they invite (auth.invite_team_
# member), so "everyone with a login" and "the managers" are the same set.
# 1:1 only for v1, matching what was actually asked for ("himself and the
# other managers... message eachother in DMs"); group/role channels are a
# 7shifts feature nobody here requested yet.

def init_team_messages(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS team_messages (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        sender_id      INTEGER NOT NULL REFERENCES users(id),
        recipient_id   INTEGER NOT NULL REFERENCES users(id),
        body           TEXT    NOT NULL,
        read_at        TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_team_msg_thread ON "
                 "team_messages(restaurant_id, sender_id, recipient_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_team_msg_inbox ON "
                 "team_messages(restaurant_id, recipient_id, read_at)")
    conn.commit()
    conn.close()


class TeamMessageError(ValueError):
    """A DM that would be meaningless or would cross a tenant boundary."""


def send_team_message(restaurant_id: int, sender_id: int, recipient_id: int,
                      body: str, db_path: str = DB_PATH) -> dict:
    """Send one DM. Both accounts must be active logins on THIS restaurant —
    recipient_id is never trusted as someone else's teammate, the same
    cross-tenant care add_recipe_ingredient takes with menu items."""
    clean = (body or "").strip()[:2000]
    if not clean:
        raise TeamMessageError("a message can't be empty")
    if recipient_id == sender_id:
        raise TeamMessageError("you can't message yourself")
    conn = get_conn(db_path)
    try:
        valid = conn.execute(
            "SELECT id FROM users WHERE id IN (?,?) AND restaurant_id=? AND is_active=1",
            (sender_id, recipient_id, restaurant_id)).fetchall()
        if len(valid) != 2:
            raise TeamMessageError("that teammate isn't on this restaurant's team")
        cur = conn.execute(
            "INSERT INTO team_messages (restaurant_id, sender_id, recipient_id, body) "
            "VALUES (?,?,?,?)", (restaurant_id, sender_id, recipient_id, clean))
        conn.commit()
        row = conn.execute(
            "SELECT id, sender_id, recipient_id, body, read_at, created_at "
            "FROM team_messages WHERE id=?", (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return dict(row)


def get_team_conversation(restaurant_id: int, user_a: int, user_b: int,
                          limit: int = 200, db_path: str = DB_PATH) -> list:
    """One thread, oldest first (how a chat reads), scoped so neither side
    can read a conversation it isn't part of."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT id, sender_id, recipient_id, body, read_at, created_at
            FROM team_messages
            WHERE restaurant_id=?
              AND ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?))
            ORDER BY created_at DESC, id DESC LIMIT ?
        """, (restaurant_id, user_a, user_b, user_b, user_a, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in reversed(rows)]


def mark_team_messages_read(restaurant_id: int, reader_id: int, other_id: int,
                            db_path: str = DB_PATH) -> int:
    """Marks everything OTHER sent TO reader as read. Never touches the
    reader's own sent messages — those are read by definition."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE team_messages SET read_at=datetime('now') "
            "WHERE restaurant_id=? AND sender_id=? AND recipient_id=? AND read_at IS NULL",
            (restaurant_id, other_id, reader_id))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_team_inbox(restaurant_id: int, user_id: int, db_path: str = DB_PATH) -> list:
    """Every other active login on this restaurant — not just people already
    messaged — each with their last exchange (if any) and how many of their
    messages are still unread. A 2-5 person team is small enough that
    showing the whole roster beats maintaining a separate "conversations
    I've started" list."""
    from auth import get_team_members
    mates = [m for m in get_team_members(restaurant_id, db_path=db_path) if m["id"] != user_id]
    conn = get_conn(db_path)
    try:
        out = []
        for m in mates:
            last = conn.execute("""
                SELECT body, sender_id, created_at FROM team_messages
                WHERE restaurant_id=? AND ((sender_id=? AND recipient_id=?) OR (sender_id=? AND recipient_id=?))
                ORDER BY created_at DESC, id DESC LIMIT 1
            """, (restaurant_id, user_id, m["id"], m["id"], user_id)).fetchone()
            unread = conn.execute(
                "SELECT COUNT(*) AS n FROM team_messages "
                "WHERE restaurant_id=? AND sender_id=? AND recipient_id=? AND read_at IS NULL",
                (restaurant_id, m["id"], user_id)).fetchone()
            out.append({
                "user_id": m["id"], "username": m["username"], "role": m["role"],
                "last_message": last["body"] if last else None,
                "last_from_me": bool(last and last["sender_id"] == user_id),
                "last_at": last["created_at"] if last else None,
                "unread": unread["n"],
            })
        # Most recent activity first; teammates never messaged sort to the
        # bottom by name rather than jumbling in at an arbitrary position.
        # Two stable sorts: username breaks ties, then last_at (descending)
        # decides order — "" (never messaged) sorts after every real
        # timestamp, so those teammates land at the bottom on their own.
        out.sort(key=lambda x: x["username"])
        out.sort(key=lambda x: x["last_at"] or "", reverse=True)
        return out
    finally:
        conn.close()


def count_unread_team_messages(restaurant_id: int, user_id: int, db_path: str = DB_PATH) -> int:
    """Unread messages from teammates still actually on the team. A sender
    who's since been revoked never shows up in get_team_inbox()'s roster
    again — counting their old unread message anyway left the badge stuck
    on forever, with no thread the recipient could ever open to clear it."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM team_messages tm "
            "JOIN users u ON u.id = tm.sender_id "
            "WHERE tm.restaurant_id=? AND tm.recipient_id=? AND tm.read_at IS NULL AND u.is_active=1",
            (restaurant_id, user_id)).fetchone()
        return row["n"]
    finally:
        conn.close()


# ── Daily task checklists ───────────────────────────────────────────────────
#
# One template per recurring duty, scoped to a role ("Wipe down the bar" —
# Bartender). Completion is a separate row keyed by (template, date), so
# "done" resets on its own at midnight with no cron job clearing anything —
# tomorrow's date just has no completion row yet.

def init_task_management(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS task_templates (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        role           TEXT    NOT NULL,
        label          TEXT    NOT NULL,
        sort_order     INTEGER NOT NULL DEFAULT 0,
        is_active      INTEGER NOT NULL DEFAULT 1,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS task_completions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        template_id    INTEGER NOT NULL REFERENCES task_templates(id),
        task_date      TEXT    NOT NULL,
        completed_by   TEXT,
        completed_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(template_id, task_date)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tpl_role ON "
                 "task_templates(restaurant_id, role, is_active)")
    conn.commit()
    conn.close()


class TaskTemplateError(ValueError):
    """A task-template add that would store something meaningless."""


def add_task_template(restaurant_id: int, role: str, label: str,
                      db_path: str = DB_PATH) -> dict:
    role = (role or "").strip()[:60]
    label = (label or "").strip()[:200]
    if not role or not label:
        raise TaskTemplateError("a role and a task description are both required")
    conn = get_conn(db_path)
    try:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(sort_order),-1)+1 AS n FROM task_templates "
            "WHERE restaurant_id=? AND role=?", (restaurant_id, role)).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO task_templates (restaurant_id, role, label, sort_order) "
            "VALUES (?,?,?,?)", (restaurant_id, role, label, nxt))
        conn.commit()
        return {"id": cur.lastrowid, "role": role, "label": label, "sort_order": nxt}
    finally:
        conn.close()


def remove_task_template(restaurant_id: int, template_id: int, db_path: str = DB_PATH) -> bool:
    """Soft-delete — history of who completed it on which past days stays
    intact in task_completions, same reasoning as deactivate_ingredient."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE task_templates SET is_active=0 WHERE id=? AND restaurant_id=?",
            (template_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_task_templates(restaurant_id: int, role: str = None, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        sql = ("SELECT id, role, label, sort_order FROM task_templates "
               "WHERE restaurant_id=? AND is_active=1")
        args = [restaurant_id]
        if role:
            # A job role typed "server " is the "Server" checklist (MOD-EMP-6).
            sql += " AND lower(trim(role))=lower(trim(?))"
            args.append(role)
        sql += " ORDER BY role, sort_order, id"
        rows = conn.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_todays_tasks(restaurant_id: int, role: str, task_date: str = None,
                     db_path: str = DB_PATH) -> list:
    """Every active template for this role, each flagged with whether
    today's (or the given date's) completion row exists."""
    from datetime import date as _date
    task_date = task_date or _date.today().isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("""
            SELECT t.id, t.role, t.label, t.sort_order,
                   c.completed_by, c.completed_at
            FROM task_templates t
            LEFT JOIN task_completions c
              ON c.template_id = t.id AND c.task_date = ?
            WHERE t.restaurant_id=? AND lower(trim(t.role))=lower(trim(?)) AND t.is_active=1
            ORDER BY t.sort_order, t.id
        """, (task_date, restaurant_id, role)).fetchall()
        return [{
            "id": r["id"], "role": r["role"], "label": r["label"],
            "done": r["completed_at"] is not None,
            "completed_by": r["completed_by"], "completed_at": r["completed_at"],
        } for r in rows]
    finally:
        conn.close()


def set_task_completion(restaurant_id: int, template_id: int, task_date: str,
                        done: bool, completed_by: str = None,
                        db_path: str = DB_PATH) -> bool:
    """Checks or unchecks one task for one day. Scoped through the
    template's own restaurant_id — task_completions carries restaurant_id
    too, redundantly, purely so a completions-only query never needs a
    join to stay tenant-scoped."""
    conn = get_conn(db_path)
    try:
        owner = conn.execute(
            "SELECT id FROM task_templates WHERE id=? AND restaurant_id=?",
            (template_id, restaurant_id)).fetchone()
        if not owner:
            return False
        if done:
            conn.execute("""
                INSERT INTO task_completions (restaurant_id, template_id, task_date, completed_by, completed_at)
                VALUES (?,?,?,?,datetime('now'))
                ON CONFLICT(template_id, task_date) DO UPDATE SET
                    completed_by=excluded.completed_by, completed_at=excluded.completed_at
            """, (restaurant_id, template_id, task_date, (completed_by or "").strip()[:120] or None))
        else:
            conn.execute(
                "DELETE FROM task_completions WHERE template_id=? AND task_date=?",
                (template_id, task_date))
        conn.commit()
        return True
    finally:
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


def rename_capability_holder(restaurant_id: int, old_name: str, new_name: str, db_path: str = DB_PATH):
    """Move every capability row (ratings, closer flag) from one name to
    another. Returns rows moved, or None when the new name already holds a
    row for an attribute being moved — never silently overwrites a rating."""
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        attrs = [r["attribute"] for r in conn.execute(
            "SELECT attribute FROM staff_capabilities WHERE restaurant_id=? AND employee_name=?",
            (restaurant_id, old_name)).fetchall()]
        if not attrs:
            conn.rollback()
            return 0
        clash = conn.execute(
            "SELECT 1 FROM staff_capabilities WHERE restaurant_id=? AND employee_name=? AND attribute IN (%s)"
            % ",".join("?" * len(attrs)), (restaurant_id, new_name, *attrs)).fetchone()
        if clash:
            conn.rollback()
            return None
        cur = conn.execute("UPDATE staff_capabilities SET employee_name=?, updated_at=datetime('now') "
                           "WHERE restaurant_id=? AND employee_name=?", (new_name, restaurant_id, old_name))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


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
    # A rating on "Maria G." is a rating on the "maria g." the schedule uses
    # (MOD-EMP-2): matched on case- and space-folded names.
    keys = {" ".join(str(k).split()).casefold() for k in scores}
    rated = [n for n in names if " ".join(str(n).split()).casefold() in keys]
    return {
        "rated": len(rated),
        "total": len(names),
        "unrated": sorted(n for n in names if n not in rated),
        "active": bool(rated),
        "pct": round(len(rated) / len(names) * 100) if names else 0,
    }


def init_staff_settings(db_path: str = DB_PATH):
    """Per-person scheduling facts the roster never had: whether they still
    work here, full or part time, an hours envelope, which dayparts they can
    work on each day, and whether they are a minor. staff_settings.py owns
    the reads and writes; this only creates the tables."""
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS staff_settings (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id        INTEGER NOT NULL REFERENCES restaurants(id),
        employee_name        TEXT    NOT NULL,
        active               INTEGER NOT NULL DEFAULT 1,
        employment_type      TEXT,               -- 'full' | 'part' | NULL (unknown)
        min_hours            REAL,
        max_hours            REAL,
        daypart_availability TEXT,               -- JSON {"Monday": "any|morning|night|off", ...}
        is_minor             INTEGER NOT NULL DEFAULT 0,
        updated_by           TEXT,
        updated_at           TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, employee_name)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS staff_pairs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        employee_a     TEXT    NOT NULL,
        employee_b     TEXT    NOT NULL,
        kind           TEXT    NOT NULL,          -- 'prefer' | 'avoid'
        note           TEXT,
        created_by     TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, employee_a, employee_b, kind)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_settings_rest ON staff_settings(restaurant_id)")
    have = {r[1] for r in conn.execute("PRAGMA table_info(staff_settings)")}
    for name, decl in (("time_windows", "TEXT"),          # {"Monday": {"earliest": "10:00am", "latest": "9:00pm"}}
                       ("certifications", "TEXT"),        # ["alcohol", "food_handler", "manager"]
                       ("preferred_dayparts", "TEXT"),    # the employee's own: ["night"]
                       ("desired_hours", "REAL"),         # the employee's own weekly wish
                       ("experienced", "INTEGER")):       # owner's word that they know the job, whatever the history shows
        if name not in have:
            conn.execute(f"ALTER TABLE staff_settings ADD COLUMN {name} {decl}")
    conn.commit()
    conn.close()


def init_demand_signals(db_path: str = DB_PATH):
    """Owner-entered events and reservation counts for specific dates
    (demand_signals.py). The scheduler read holidays and weekday medians
    and nothing an owner actually knew about next Saturday."""
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS demand_signals (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        date           TEXT    NOT NULL,
        kind           TEXT    NOT NULL,          -- 'event' | 'reservations'
        label          TEXT    NOT NULL,
        covers         INTEGER,
        lift_pct       INTEGER,
        source         TEXT    NOT NULL DEFAULT 'manual',
        created_by     TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, date, kind, label)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_demand_signals_date ON demand_signals(restaurant_id, date)")
    conn.commit()
    conn.close()


def init_schedule_versions(db_path: str = DB_PATH):
    """Every saved state of a schedule (schedule_versions.py): the draft as
    generated, each manager save, the copy that was published. Edits used
    to overwrite the one row and the draft was gone."""
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_versions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        history_id     INTEGER NOT NULL REFERENCES schedule_history(id),
        version        INTEGER NOT NULL,
        reason         TEXT    NOT NULL,          -- 'generated' | 'edited' | 'published' | 'fixes' | 'swap' (staff drop/cover)
        schedule_csv   TEXT    NOT NULL,
        quality_json   TEXT,
        diff_json      TEXT,                      -- vs the previous version
        saved_by       TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(history_id, version)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_versions_hist ON schedule_versions(history_id)")
    conn.commit()
    conn.close()


def init_shift_requests(db_path: str = DB_PATH):
    """A member of staff asking to drop a published shift, the manager's
    answer, and who picked it up (shift_requests.py)."""
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS shift_change_requests (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
        history_id       INTEGER NOT NULL REFERENCES schedule_history(id),
        employee_name    TEXT    NOT NULL,
        date             TEXT    NOT NULL,
        shift_start      TEXT    NOT NULL,
        shift_end        TEXT,
        role             TEXT,
        reason           TEXT,
        status           TEXT    NOT NULL DEFAULT 'pending',   -- pending | open | denied | covered | withdrawn
        replacement_name TEXT,
        decided_by       TEXT,
        decided_at       TEXT,
        created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shift_requests_rest ON shift_change_requests(restaurant_id, status)")
    have = {r[1] for r in conn.execute("PRAGMA table_info(shift_change_requests)")}
    for name, decl in (("kind", "TEXT DEFAULT 'drop'"),   # drop | swap
                       ("target_name", "TEXT"), ("target_date", "TEXT"), ("target_start", "TEXT"), ("target_end", "TEXT"),
                       # a swap moves the colleague's shift too, so it needs their yes (SCHED-21)
                       ("target_accepted_at", "TEXT")):
        if name not in have:
            conn.execute(f"ALTER TABLE shift_change_requests ADD COLUMN {name} {decl}")
    conn.commit()
    conn.close()


def init_manual_team_members(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS manual_team_members (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
        employee_name  TEXT    NOT NULL,
        role           TEXT,
        added_by       TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, employee_name)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_team_rest "
                 "ON manual_team_members(restaurant_id)")
    conn.commit()
    conn.close()


class ManualTeamMemberError(ValueError):
    """A manual roster add/remove that would store something meaningless."""


def add_manual_team_member(restaurant_id: int, employee_name: str, role: str = None,
                           added_by: str = None, db_path: str = DB_PATH) -> dict:
    """Add (or update the role on) someone the owner types in by hand.

    This exists because the roster is normally built entirely from shift
    CSV rows (see mobile_labor_team) — an owner with no POS/back-office
    hookup yet, or a brand-new hire who hasn't worked a shift, would
    otherwise have no way to rate someone in the Operational Score panel
    at all. A manual entry is keyed the same way a shift-derived one is
    (restaurant_id, employee_name), so rating it with set_capability()
    works identically, and if the same name later shows up in real shift
    data the two rows collapse into one person rather than duplicating.
    """
    name = (employee_name or "").strip()[:120]
    if not name:
        raise ManualTeamMemberError("an employee name is required")
    clean_role = (role or "").strip()[:60] or None
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO manual_team_members (restaurant_id, employee_name, role, added_by, created_at)
            VALUES (?,?,?,?,datetime('now'))
            ON CONFLICT(restaurant_id, employee_name) DO UPDATE SET
                role=excluded.role, added_by=excluded.added_by
        """, (restaurant_id, name, clean_role, (added_by or "").strip()[:120] or None))
        conn.commit()
    finally:
        conn.close()
    return {"employee_name": name, "role": clean_role}


def remove_manual_team_member(restaurant_id: int, employee_name: str, db_path: str = DB_PATH) -> bool:
    """Remove a hand-entered roster row. Never touches shift-derived rows —
    those represent real worked shifts and aren't this table's business."""
    name = (employee_name or "").strip()
    if not name:
        return False
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM manual_team_members WHERE restaurant_id=? AND employee_name=?",
            (restaurant_id, name))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_manual_team_members(restaurant_id: int, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT employee_name, role FROM manual_team_members "
            "WHERE restaurant_id=? ORDER BY employee_name COLLATE NOCASE",
            (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [{"name": r["employee_name"], "role": r["role"]} for r in rows]


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


# ── What the assistant remembers between conversations ─────────────────────
#
# The transcript is not memory. History is scoped to one conversation id, so
# a new chat starts blank — an owner who said on Tuesday that they are hiring
# two bartenders and want labor down had to say it again on Wednesday, which
# is exactly the "stop making me explain my restaurant" problem.
#
# Deliberately small and deliberately written rather than inferred. The model
# calls a tool to record a fact; nothing is harvested automatically, because a
# memory that fills itself becomes a second prompt nobody reviewed.
ASK_MEMORY_LIMIT = 12
ASK_MEMORY_MAX_LENGTH = 240


def init_ask_memory(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ask_memory (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            fact          TEXT    NOT NULL,
            kind          TEXT,             -- goal | context | preference | followup
            source        TEXT,             -- where it came from, for the owner to judge
            user_id       INTEGER,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, fact)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ask_memory_rest "
                 "ON ask_memory(restaurant_id, created_at)")
    conn.commit()
    conn.close()


def remember_ask_fact(restaurant_id: int, fact: str, kind: str = "context",
                      source: str = None, user_id: int = None,
                      db_path: str = DB_PATH) -> dict:
    """Record one durable fact about this owner. Idempotent on the text.

    Oldest facts fall off past ASK_MEMORY_LIMIT rather than growing without
    bound — a memory that only ever accumulates ends up costing every
    subsequent question more and telling the model less.
    """
    text = (fact or "").strip()[:ASK_MEMORY_MAX_LENGTH]
    if not text:
        raise ValueError("a fact needs some text")
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO ask_memory (restaurant_id, fact, kind, source, user_id) "
            "VALUES (?,?,?,?,?) ON CONFLICT(restaurant_id, fact) DO UPDATE SET "
            "kind=excluded.kind, source=excluded.source, created_at=datetime('now')",
            (restaurant_id, text, (kind or "context")[:20], (source or "")[:160] or None, user_id))
        conn.execute(
            "DELETE FROM ask_memory WHERE restaurant_id=? AND id NOT IN "
            "(SELECT id FROM ask_memory WHERE restaurant_id=? ORDER BY created_at DESC, id DESC LIMIT ?)",
            (restaurant_id, restaurant_id, ASK_MEMORY_LIMIT))
        conn.commit()
    finally:
        conn.close()
    return {"fact": text, "kind": kind or "context"}


def get_ask_memory(restaurant_id: int, db_path: str = DB_PATH) -> list:
    try:
        conn = get_conn(db_path)
        rows = conn.execute(
            "SELECT fact, kind, source, created_at FROM ask_memory WHERE restaurant_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (restaurant_id, ASK_MEMORY_LIMIT)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def forget_ask_fact(restaurant_id: int, fact: str, db_path: str = DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM ask_memory WHERE restaurant_id=? AND fact=?",
                           (restaurant_id, (fact or "").strip()))
        conn.commit()
        return cur.rowcount > 0
    finally:
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
        # The upload is a rolling window; staff_first_seen remembers the
        # earliest date and the most shifts ever counted for each name, so
        # a person here since March is not "still new" in October.
        try:
            import schedule_intel as _si
            out = _si.tenure(restaurant_id, out, db_path=db_path)
        except Exception:
            pass
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
            # Only a PUBLISHED week at the sibling counts — a draft there is
            # not a commitment (the same rule schedule_rules._published_tail keeps).
            _ensure_history_columns(conn)
            row = conn.execute(
                "SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
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

    # A shifts upload is a rolling window; remember each name's first date
    # and most shifts ever counted so tenure survives the window rolling on.
    if data_type == "shifts":
        try:
            from labor import load_shifts
            import schedule_intel as _si
            _si.remember_tenure(restaurant_id, load_shifts(csv_string=csv_content) or [], db_path=db_path)
        except Exception:
            pass

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
    """Admin reset of a user password — through auth.update_password, so it
    ends the login's sessions and clears must_reset_password like every
    other password write (SEC-7, SEC-8)."""
    from auth import update_password
    update_password(user_id, new_password, db_path=db_path)


def _hash_reset_token(token: str) -> str:
    import hashlib
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


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
    # Only the hash is stored (security audit A2) — the same rule as
    # sessions. A database read yields nothing that opens an account.
    conn.execute("UPDATE users SET reset_token=?, reset_token_expires=? WHERE id=?",
                 (_hash_reset_token(token), expires, user["id"]))
    conn.commit()
    conn.close()
    return token


def validate_reset_token(token: str, db_path: str = DB_PATH) -> dict | None:
    """Validate a reset token. Returns user row or None if invalid/expired."""
    from datetime import datetime, timezone
    conn = get_conn(db_path)
    user = conn.execute(
        "SELECT * FROM users WHERE reset_token=? AND is_active=1", (_hash_reset_token(token),)
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
    # Burn the token first, conditionally, so exactly one submission can
    # win. Validate-then-update-by-id let two concurrent submissions both
    # pass validation and both set a password (DATA-50).
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE users SET reset_token=NULL, reset_token_expires=NULL "
                           "WHERE id=? AND reset_token=?", (user["id"], _hash_reset_token(token)))
        conn.commit()
        won = cur.rowcount == 1
    finally:
        conn.close()
    if not won:
        return False
    # Ends every session and clears must_reset_password (SEC-7, SEC-8).
    from auth import update_password
    update_password(user["id"], new_password, db_path=db_path)
    return True


def get_approved_examples(restaurant_id: int, limit: int = 5,
                           db_path: str = DB_PATH) -> list:
    """Return recent approved review responses as style examples for the AI.

    A reply the auto-approve rule published is the model's own text, not
    the owner's style: learning from it would feed the drafter its own
    output (audit #15), so only replies a person approved are examples. A
    bulk publish (response_action='bulk_approved') posted drafts nobody read
    one by one — the model's text again, not the owner's choice (M-3)."""
    conn = get_conn(db_path)
    # A reply the owner EDITED before approving comes first: it is their
    # own words, where one approved as written is the model's (audit #40).
    # Most recent within each group; deterministic for a fixed table.
    rows = conn.execute("""
        SELECT rating, text, draft_response FROM reviews
        WHERE restaurant_id=?
          AND response_status IN ('approved','posted')
          AND COALESCE(response_action, '') NOT IN ('auto_approved', 'bulk_approved')
          AND draft_response IS NOT NULL
          AND draft_response != ''
        ORDER BY CASE WHEN edit_category IN ('light', 'heavy', 'rewrite') THEN 0 ELSE 1 END, id DESC
        LIMIT ?
    """, (restaurant_id, limit)).fetchall()
    conn.close()
    return [{"rating": r["rating"], "review": r["text"][:120], "response": r["draft_response"]} for r in rows]


def record_reply_edit(review_id: int, restaurant_id: int, db_path: str = DB_PATH):
    """At approval: compare the model's draft (original_draft, kept from the
    first edit) with the reply the owner approved and store the summary on
    the review (reply_edits.compare). A reply approved as drafted records
    "unchanged" — a signal too, that the drafter got it right. A reply with
    no model draft behind it (typed from blank) records nothing. Returns
    the summary or None; never raises."""
    import json as _json
    import reply_edits
    try:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT original_draft, draft_response, COALESCE(draft_edited, 0) AS edited "
                               "FROM reviews WHERE id=? AND restaurant_id=?", (review_id, restaurant_id)).fetchone()
            if not row or not (row["draft_response"] or "").strip():
                return None
            original = row["original_draft"]
            if original is None and not row["edited"]:
                original = row["draft_response"]          # approved exactly as drafted
            if not (original or "").strip():
                return None
            summary = reply_edits.compare(original, row["draft_response"])
            conn.execute("UPDATE reviews SET edit_distance=?, edit_category=?, edit_signals=? "
                         "WHERE id=? AND restaurant_id=?",
                         (summary["distance"], summary["category"], _json.dumps(summary["signals"]),
                          review_id, restaurant_id))
            conn.commit()
            return summary
        finally:
            conn.close()
    except Exception as e:
        print(f"[reply_edits] not recorded for review {review_id}: {e}")
        return None


def get_reply_edit_summaries(restaurant_id: int, limit: int = 12, db_path: str = DB_PATH) -> list:
    """The most recent approved replies' edit summaries, newest first —
    what drafter.draft_response turns into its OWNER'S EDITS note. Only a
    person's approvals (not the auto-approve rule's or a bulk publish's)."""
    import json as _json
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT edit_distance, edit_category, edit_signals, original_draft, draft_response FROM reviews "
            "WHERE restaurant_id=? AND edit_category IS NOT NULL AND response_status IN ('approved','posted') "
            "AND COALESCE(response_action, '') NOT IN ('auto_approved', 'bulk_approved') "
            "ORDER BY COALESCE(approved_at, '') DESC, id DESC LIMIT ?", (restaurant_id, int(limit))).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            signals = _json.loads(r["edit_signals"] or "[]")
        except Exception:
            signals = []
        words = len((r["draft_response"] or "").split())
        before = len((r["original_draft"] or r["draft_response"] or "").split())
        out.append({"distance": r["edit_distance"], "category": r["edit_category"], "signals": signals,
                    "words_after": words, "words_before": before})
    return out


def save_labor_snapshot(restaurant_id: int, period_start: str, period_end: str,
                         labor_pct: float, total_labor: float, total_sales: float,
                         db_path: str = DB_PATH):
    """Save a labor analysis snapshot for trend tracking — once per period.
    It was written on every insight view, so the second view found "the
    previous upload" to be the same period and compared it with itself: the
    trend and forecast lines vanished."""
    conn = get_conn(db_path)
    try:
        same = conn.execute("SELECT 1 FROM labor_history WHERE restaurant_id=? AND period_start IS ? AND period_end IS ? "
                            "AND ABS(COALESCE(labor_pct, 0) - COALESCE(?, 0)) < 0.05 LIMIT 1",
                            (restaurant_id, period_start, period_end, labor_pct)).fetchone()
        if same:
            return
        conn.execute("""
            INSERT INTO labor_history (restaurant_id, period_start, period_end, labor_pct, total_labor, total_sales)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (restaurant_id, period_start, period_end, labor_pct, total_labor, total_sales))
        conn.commit()
    finally:
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
                           what_if: dict = None, db_path: str = DB_PATH) -> int:
    """Persists every generated schedule permanently, independent of
    whatever the mobile app's own client-side caching does — a durable
    record on the Account tab's Schedule History screen that survives
    regardless of any iOS view-state bug, rather than depending on getting
    every layer of client caching right. Returns the new row's id.
    """
    import json as _json_sh
    conn = get_conn(db_path)
    # The Shift Quality verdict used to live only in the async job result,
    # which is deleted the first time it is polled — so the headline number
    # an owner is asked to trust could never be looked at again, and
    # Schedule History showed past weeks with no score and no trend.
    _ensure_history_columns(conn)
    q = quality or {}
    cur = conn.execute("""
        INSERT INTO schedule_history
            (restaurant_id, week_start, week_end, hours_scheduled, hours_budget,
             labor_target, schedule_csv, summary_json, quality_json,
             quality_score, quality_band, quality_confidence, what_if_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, labor_target,
          schedule_csv, _json_sh.dumps(summary or []),
          _json_sh.dumps(quality) if quality else None,
          q.get("score"), q.get("band"), (q.get("confidence") or {}).get("level"),
          _json_sh.dumps(what_if) if what_if else None))
    conn.commit()
    new_id = cur.lastrowid
    # Drafts of the same week that were never sent are superseded by this
    # one, so the history reads as one draft per week, not five.
    if week_start:
        try:
            conn = get_conn(db_path)
            conn.execute("UPDATE schedule_history SET superseded_by=? WHERE restaurant_id=? AND week_start=? AND id<>? "
                         "AND published_at IS NULL AND superseded_by IS NULL", (new_id, restaurant_id, week_start, new_id))
            conn.commit()
        finally:
            conn.close()
    return new_id


_HISTORY_COLUMNS_OK = set()


def _ensure_history_columns(conn):
    """Columns added to schedule_history after it first shipped.

    Boot adds all of them (init_db's column list); this only covers a
    database created before boot ran, once per database file per process,
    so no request path reads or changes the schema after the first call."""
    try:
        db_file = conn.execute("PRAGMA database_list").fetchone()[2] or ":memory:"
    except Exception:
        db_file = None
    if db_file and db_file != ":memory:" and db_file in _HISTORY_COLUMNS_OK:
        return
    have = {r[1] for r in conn.execute("PRAGMA table_info(schedule_history)")}
    for name, decl in (("quality_json", "TEXT"), ("edited_at", "TEXT"),
                       ("edited_by", "TEXT"), ("published_at", "TEXT"), ("published_by", "TEXT"),
                       ("review_json", "TEXT"), ("generation_seconds", "REAL"),
                       ("weather_json", "TEXT"), ("quality_score", "REAL"), ("quality_band", "TEXT"),
                       ("quality_confidence", "TEXT"), ("what_if_json", "TEXT"), ("superseded_by", "INTEGER"),
                       ("republished_at", "TEXT"), ("publishing_at", "TEXT")):
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE schedule_history ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as e:
                # Another connection added it between our read and this write.
                if "duplicate column" not in str(e).lower():
                    raise
    if db_file and db_file != ":memory:":
        _HISTORY_COLUMNS_OK.add(db_file)


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


_HISTORY_BAND_LEAD = {"excellent": "Excellent week", "good": "Solid week",
                      "fair": "A few soft spots", "weak": "Needs attention"}


def _history_summary_line(quality: dict, hours_scheduled, hours_budget, edited_at) -> tuple:
    """One honest line for a Schedule History row.

    Built only from what the Shift Quality Engine (shift_quality.evaluate_
    schedule) and the hours themselves actually established for THIS week —
    never a generic claim ("no leadership issues") the engine didn't make.
    weaknesses/strengths are the engine's own week-level sentences (already
    human-readable — see shift_quality._week_reasons), so the most notable
    one is used verbatim rather than re-summarized into something new that
    could drift from what it actually found. Returns (line, tone) where
    tone is 'good' | 'warn' | 'info' for the row's status dot.
    """
    q = quality or {}
    if not q.get("checked"):
        # No quality read on this week (engine failed, or the row predates
        # this feature) — hours are the one thing always known.
        if hours_budget and hours_scheduled:
            diff = round(hours_budget - hours_scheduled, 1)
            if abs(diff) < 0.5:
                return "On budget", "info"
            if diff > 0:
                return "%s hrs under budget" % _fmt_hrs_hs(diff), "good"
            return "%s hrs over budget" % _fmt_hrs_hs(-diff), "warn"
        return "Not yet scored", "info"

    band = q.get("band") or ""
    lead = _HISTORY_BAND_LEAD.get(band, "Scored")
    tone = "good" if band in ("excellent", "good") else "warn"
    weaknesses = q.get("weaknesses") or []
    strengths = q.get("strengths") or []
    detail = None
    if weaknesses:
        detail = weaknesses[0]
        tone = "warn"
    elif strengths:
        detail = strengths[0]
    line = lead + (" · " + detail if detail else "")
    # The row used to hard-wrap to one line (CSS text-overflow:ellipsis),
    # so anything past ~110 chars got clipped twice over — once here, then
    # again by the browser mid-word. The row wraps to a second line now, so
    # this cap only exists to stop a truly pathological string; the engine's
    # own week-level sentences (see shift_quality._hoist_common_lines) are
    # routinely 110-160 chars and are meant to read in full.
    if len(line) > 220:
        line = line[:217].rstrip() + "…"
    if edited_at:
        line = "Manually edited · " + line
    return line, tone


def _fmt_hrs_hs(n) -> str:
    n = round(float(n), 1)
    return str(int(n)) if n == int(n) else str(n)


def get_schedule_history(restaurant_id: int, limit: int = 300, db_path: str = DB_PATH) -> list:
    """Summary rows only (no schedule_csv) for the history list screen —
    keeps the list payload light; fetch the full record via
    get_schedule_history_detail() once a specific entry is tapped."""
    conn = get_conn(db_path)
    try:
        _ensure_history_columns(conn)
        rows = conn.execute("""
            SELECT id, generated_at, week_start, week_end, hours_scheduled,
                   hours_budget, labor_target,
                   quality_json, quality_score, quality_band, quality_confidence,
                   edited_at, edited_by, published_at, published_by, superseded_by, republished_at
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
        if d.get("quality_score") is None:
            d["quality_score"] = (q or {}).get("score")
            d["quality_band"] = (q or {}).get("band")
        d["confidence"] = d.pop("quality_confidence", None) or ((q or {}).get("confidence") or {}).get("level")
        d["summary_line"], d["summary_tone"] = _history_summary_line(
            q, d.get("hours_scheduled"), d.get("hours_budget"), d.get("edited_at"))
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
    for col, key in (("review_json", "review"), ("weather_json", "weather_forecast")):
        try:
            d[key] = _json_sh.loads(d.pop(col, None) or "null")
        except Exception:
            d[key] = None
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
    try:
        conn.execute("BEGIN IMMEDIATE")
        own = conn.execute("SELECT published_at FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (history_id, restaurant_id)).fetchone()
        if not own:
            conn.rollback()
            return False
        # Every generation writes a schedule_versions row, whose foreign key
        # made this DELETE raise for every modern draft (SCHED-16). A draft's
        # versions, and the share links of a publish that never completed,
        # belong to it and go with it. A published week keeps its dependents
        # (requests, outcomes) and is refused by the routes before this.
        if not own["published_at"]:
            conn.execute("DELETE FROM schedule_versions WHERE history_id=? AND restaurant_id=?", (history_id, restaurant_id))
            conn.execute("DELETE FROM schedule_shares WHERE schedule_id=? AND restaurant_id=?", (history_id, restaurant_id))
        cur = conn.execute("DELETE FROM schedule_history WHERE id=? AND restaurant_id=?", (history_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


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
    # Roles match the way per-shift rates do (labor._shift_rate): case- and
    # whitespace-insensitively. An hours cell that is not a number counts as
    # nothing rather than raising (MOD-LAB-14).
    by_key = {(k or "").strip().lower(): v for k, v in role_rates.items() if k != "_default"}
    for s in shifts:
        role = (s.get("role") or "").strip().lower()
        try:
            hrs = float(s.get("actual_hours") or s.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            hrs = 0.0
        if hrs != hrs or hrs < 0 or hrs == float("inf"):
            hrs = 0.0
        rate = by_key.get(role, default)
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

    # Every candidate date across every requested date, fetched once.
    #
    # This was a query per offset per date — seven dates by a seven-day window
    # is 49 round trips to answer one question about one restaurant's history
    # (audit #17). The window and the tie-break below are unchanged; only the
    # number of queries is.
    wanted = set()
    for date_str in next_week_dates:
        try:
            yoy_dt = _dt.strptime(date_str, "%Y-%m-%d") - _td(weeks=52)
        except Exception:
            continue
        for offset in range(-3, 4):
            wanted.add((yoy_dt + _td(days=offset)).strftime("%Y-%m-%d"))

    by_date = {}
    if wanted:
        marks = ",".join("?" * len(wanted))
        for row in conn.execute(
            f"SELECT * FROM labor_daily_history WHERE restaurant_id=? AND date IN ({marks})",
            (restaurant_id, *sorted(wanted))
        ).fetchall():
            by_date[row["date"]] = dict(row)

    rows_out = []
    for date_str in next_week_dates:
        try:
            dt = _dt.strptime(date_str, "%Y-%m-%d")
            yoy_dt = dt - _td(weeks=52)
            # Same ±3 day window, still walked in offset order so the
            # fallback below keeps picking the earliest date with data.
            candidates = [
                by_date[d] for d in
                ((yoy_dt + _td(days=o)).strftime("%Y-%m-%d") for o in range(-3, 4))
                if d in by_date
            ]
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
    _invalidate_request_cache(restaurant_id)


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
    """Every restaurant record, hydrated from one query.

    This used to SELECT * and then throw every column away, calling
    get_restaurant(row["id"]) per row — one query and one connection each.
    Measured at 17 queries for 16 restaurants; at 1,000 restaurants it is
    1,001, and ten scheduler jobs call this daily (audit #17).

    The hydration is a pure row -> dataclass mapping, so reusing the rows
    already in hand is the same object by a cheaper route.
    """
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM restaurants WHERE id > 0 ORDER BY id"
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        # Per-row guard kept: one malformed row must not cost the caller the
        # whole list. But a row that fails is reported, not dropped in
        # silence: it leaves every scheduled job with it, and nothing else
        # would ever say so (DATA-43 / MOD-PERF-5).
        try:
            result.append(_restaurant_from_row(row))
        except Exception as e:
            _report_unhydratable(row, e, db_path)
    return result


# (db_path, restaurant_id) -> monotonic time last reported. A row that cannot
# hydrate fails on every call; the failure digest needs it once an hour,
# not once per job and page view.
_hydration_reported = {}


def _report_unhydratable(row, exc, db_path):
    import time as _time
    try:
        rid = row["id"]
    except Exception:
        rid = None
    key = (str(db_path), rid)
    now = _time.monotonic()
    last = _hydration_reported.get(key)
    if last is not None and now - last < 3600:
        return
    _hydration_reported[key] = now
    print(f"[models] restaurant {rid} could not be loaded and is skipped: {exc}")
    try:
        import ops
        ops.capture(exc, job="restaurant_hydration",
                    context=f"restaurant_id={rid} — skipped by get_all_restaurants, so every scheduled job skips it",
                    db_path=db_path if db_path != DB_PATH else None)
    except Exception:
        pass

def get_restaurants_for_digest(day: str, db_path: str = DB_PATH) -> list:
    """Get all restaurants scheduled for digest on a given day of week."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT r.*, u.email as contact_email
        FROM restaurants r
        JOIN users u ON u.restaurant_id = r.id AND u.is_admin = 0
        WHERE r.digest_day=? AND r.digest_enabled=1 AND r.module_reviews=1
          -- A cancelled, paused or deleting account gets no weekly digest
          -- (DATA-51); unknown/NULL billing stays in service (in_service).
          AND """ + in_service_sql("r.billing_status") + """
          AND r.deletion_requested_at IS NULL
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
    _invalidate_request_cache(restaurant_id)


# Billing states that still entitle a restaurant to use the product.
# `paused`/`churned` do not. Anything unrecognised — including NULL on rows
# that predate the column — DOES, deliberately: locking a paying customer out
# because of a value nobody anticipated is a far worse failure than briefly
# serving one who cancelled, and the audit flagged both directions.
ACTIVE_BILLING_STATES = {"trial", "active", "internal", "past_due", "pending", ""}
BLOCKED_BILLING_STATES = {"churned", "paused", "canceled", "cancelled"}


def in_service(restaurant) -> bool:
    """subscription_allows_access for a restaurant object already in hand —
    what every BACKGROUND job asks before spending on or contacting one
    (MOD-REV-2). The request side refused a cancelled customer; the
    scheduler kept fetching, publishing replies under their name, alerting
    them and paying Places, Claude and Perplexity for their Intel."""
    if hasattr(restaurant, "billing_status"):
        status = restaurant.billing_status
    else:
        try:
            status = restaurant["billing_status"]    # a sqlite3.Row or a dict
        except (KeyError, IndexError, TypeError):
            status = None
    return (status or "").strip().lower() not in BLOCKED_BILLING_STATES


def in_service_sql(column: str = "billing_status") -> str:
    """The same rule as a WHERE fragment, for jobs that select restaurants
    in SQL. NULL/unknown stays in service, as subscription_allows_access."""
    states = ",".join("'%s'" % s for s in sorted(BLOCKED_BILLING_STATES))
    return f"LOWER(TRIM(COALESCE({column},''))) NOT IN ({states})"


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


# ── Organizations (the layer above a restaurant) ───────────────────────────

def backfill_organizations(db_path: str = DB_PATH) -> int:
    """Give every existing location group a real organization row.

    Idempotent, runs at boot, derives everything from data already present:
    one organization per distinct (location_group, normalised owner_email),
    which is exactly the pair every multi-location read already scopes by. A
    restaurant with no group name gets no organization — a single-location
    client is not a group of one, and inventing one would put a concept in
    front of clients who have no use for it.

    Returns how many restaurants it linked, for the migration test.
    """
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute("""
                SELECT DISTINCT TRIM(location_group) AS g, owner_email
                FROM restaurants
                WHERE location_group IS NOT NULL AND TRIM(location_group) <> ''
                  AND organization_id IS NULL
            """).fetchall()
            linked = 0
            for r in rows:
                group, email = r["g"], normalize_owner_email(r["owner_email"])
                if not group or not email:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO organizations (name, owner_email) VALUES (?,?)",
                    (group, email))
                org = conn.execute(
                    "SELECT id FROM organizations WHERE name=? AND owner_email=?",
                    (group, email)).fetchone()
                if not org:
                    continue
                cur = conn.execute("""
                    UPDATE restaurants SET organization_id=?
                    WHERE organization_id IS NULL
                      AND TRIM(location_group)=?
                      AND LOWER(TRIM(COALESCE(owner_email,'')))=?
                """, (org["id"], group, email))
                linked += cur.rowcount or 0
            conn.commit()
            if linked:
                _invalidate_request_cache()
            return linked
        finally:
            conn.close()
    except Exception:
        # Same stance as auth.backfill_memberships: a backfill failure must
        # not stop the app booting, and every read still falls back to the
        # (group_name, owner_email) string match it has always used.
        return 0


def get_organization(organization_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM organizations WHERE id=?",
                           (organization_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_organizations(owner_email=None, db_path: str = DB_PATH) -> list:
    """Existing organizations, optionally for one owner.

    This is what stops a typo creating a second group: an admin picks from
    what exists instead of retyping the name on each location.
    """
    conn = get_conn(db_path)
    try:
        if owner_email is not None:
            rows = conn.execute(
                "SELECT * FROM organizations WHERE owner_email=? ORDER BY name COLLATE NOCASE",
                (normalize_owner_email(owner_email),)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM organizations ORDER BY name COLLATE NOCASE").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_or_create_organization(name: str, owner_email: str, db_path: str = DB_PATH):
    """The organization for this (name, owner), creating it on first use."""
    group = (name or "").strip()
    email = normalize_owner_email(owner_email)
    if not group or not email:
        return None
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO organizations (name, owner_email) VALUES (?,?)",
                     (group, email))
        conn.commit()
        row = conn.execute("SELECT * FROM organizations WHERE name=? AND owner_email=?",
                           (group, email)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def set_restaurant_organization(restaurant_id: int, organization_id, db_path: str = DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE restaurants SET organization_id=? WHERE id=?",
                           (organization_id, restaurant_id))
        conn.commit()
        _invalidate_request_cache(restaurant_id)
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def get_organization_locations(organization_id: int, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM restaurants WHERE organization_id=? ORDER BY location_name",
            (organization_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


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

    When an organization row exists for that pair, membership is resolved by
    organization_id — a real key rather than two strings that have to keep
    agreeing. The string match stays as the fallback for anything the backfill
    has not reached, so this is a strictly additive change in behaviour.
    """
    conn = get_conn(db_path)
    if owner_email is not None:
        org = conn.execute(
            "SELECT id FROM organizations WHERE name=? AND owner_email=?",
            ((group_name or "").strip(), normalize_owner_email(owner_email))).fetchone()
        if org:
            rows = conn.execute(
                "SELECT * FROM restaurants WHERE organization_id=? ORDER BY location_name",
                (org["id"],)).fetchall()
            if rows:
                conn.close()
                return [dict(r) for r in rows]
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


# 30 days. Past this, the figure reflects a backfill/import rather than how
# fast anyone actually responds.
RESPONSE_TIME_CAP_HOURS = 30 * 24



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
    #
    # The three time-windowed columns below read bare `review_date`, which
    # meant a CSV-imported review with no review_date was silently absent from
    # "this month", "last 30 days" and the 30-day average — while the AI
    # insight's own windows, built on the COALESCE axis, counted it. The same
    # prompt therefore carried two incompatible definitions of the same
    # period. REVIEW_TIME_AXIS_BARE is now the single answer everywhere.
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
            SUM({_AX} >= date('now','start of month'))                                  AS received_this_month,
            SUM({_AX} >= date('now','-30 days'))                                        AS last_30d,
            AVG(CASE WHEN {_AX} >= date('now','-30 days') THEN rating END)              AS avg_rating_30d
        FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL
    """.format(_AX=REVIEW_TIME_AXIS_BARE), (restaurant_id,)).fetchone()

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
    from datetime import datetime as _dt_st, timedelta as _td_st
    by_key = {}
    for row in rows:
        by_key[row["week_key"]] = row

    # Emit every week from the first one that HAS data through to now,
    # including the quiet ones. GROUP BY only produces weeks that exist, so
    # a week with no reviews simply vanished and the chart drew the weeks
    # either side of it as adjacent — time silently compressed, and a gap
    # in trading (a closure, a slow January) read as continuity.
    #
    # A restaurant with nothing in the window still returns [] rather than
    # a row of zero bars, because every caller uses an empty list to mean
    # "show the empty state" and a fake 8-bar chart is worse than none.
    if not rows:
        return []
    # The SQL above windows and buckets on UTC timestamps (review_date and
    # fetched_at are stored in UTC), so "this week" has to be UTC here too.
    # Local time put the two clocks a day apart every evening after 7pm
    # Chicago, and across a Monday boundary that dropped a bar.
    from datetime import timezone as _tz_st
    today = _dt_st.now(_tz_st.utc).replace(tzinfo=None)
    monday = today - _td_st(days=today.weekday())
    first_key = min(by_key)
    result = []
    for i in range(weeks - 1, -1, -1):
        start = monday - _td_st(weeks=i)
        if start.strftime("%Y-%W") < first_key:
            continue
        key = start.strftime("%Y-%W")
        row = by_key.get(key)
        if row is not None:
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
        else:
            result.append({
                "label":      f"{start.month}/{start.day}",
                "week_key":   key,
                "positive":   0, "negative": 0, "neutral": 0,
                "total":      0, "avg_rating": 0,
            })
    return result

def get_top_issues(restaurant_id, days=90, limit=6, sentiment="negative"):
    """Top review categories by mention count for the last N days.

    sentiment="negative" (the default) counts ONLY the categories attached
    to negative reviews, because every caller that reads position 0 of this
    list calls it a complaint — Home's headline recommendation renders it
    as "Look into {topic} — it's the most-mentioned complaint" under the
    line "Repeat themes in negative reviews are the fixable kind." It
    counted every sentiment, so a restaurant whose guests consistently
    PRAISED its food quality was told to go fix its food quality.

    Pass sentiment=None for "what guests talk about", regardless of tone —
    which is what the AI insight wants, and which it labels "Top topics".
    """
    from collections import Counter
    conn = get_conn()
    where_sentiment = "AND sentiment=?" if sentiment else ""
    params = [restaurant_id] + ([sentiment] if sentiment else []) + [str(days)]
    rows = conn.execute(f"""
        SELECT categories FROM reviews
        WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL
        {where_sentiment}
        AND categories IS NOT NULL AND categories != '[]'
        AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', '-' || ? || ' days')
    """, params).fetchall()
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

# Both periods need at least this many mentions of a topic before its
# arrow claims a direction. Mirrors notify.MIN_TREND_REVIEWS_PER_WEEK.
MIN_TOPIC_TREND_MENTIONS = 3


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
        # A direction needs enough mentions on BOTH sides of the comparison
        # to mean anything. Without this, one mention this period against
        # zero last period rendered a "trending up" arrow — a signal an
        # owner may act on, drawn from a single guest. MIN_TOPIC_TREND_
        # MENTIONS is the same idea as notify.MIN_TREND_REVIEWS_PER_WEEK.
        if total < MIN_TOPIC_TREND_MENTIONS or pv < MIN_TOPIC_TREND_MENTIONS:
            trend = "flat"
        elif total > pv * 1.15:
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


# Kept here rather than imported from analyser.py so that reading a review
# never pulls in the anthropic client at module scope.
_SEVERITY_LABELS = {
    "safety":      "Guest safety",
    "legal":       "Legal exposure",
    "operational": "Operational failure",
    "service":     "Service quality",
    "minor":       "Minor",
}

# The inbox's page size. The list used to be unbounded: every review a
# restaurant had ever received was selected, serialised and — on the web —
# rendered server-side as a full card with a draft box, a textarea and a
# template picker. Fine at 58 reviews, a multi-megabyte document at 3,000.
REVIEWS_PAGE_SIZE = 50


def get_reviews_data(restaurant_id, filter_by="all", search="", category=None, platform=None,
                     limit=None, offset=0, include_total=False, review_id=None):
    """Rows for the review inbox.

    Unanalysed reviews are INCLUDED. The filter used to be `processed=1`,
    which put this query at odds with get_review_stats (which deliberately
    counts every review, analysed or not): a review whose Haiku analysis
    failed was counted in the owner's "needs a reply" totals and in the tab
    badge, but was missing from the list those numbers point at — a badge
    reading 3 over an inbox with nothing in it, and no way to tell why.
    Callers can read `processed` on each row to label the ones still
    waiting on analysis.

    limit/offset paginate; include_total adds the unpaginated count so a
    caller can tell whether more remain.
    """
    conn = get_conn()
    where  = ["restaurant_id=?", "deleted_at IS NULL"]
    params = [restaurant_id]
    if review_id is not None:
        # One review, in the inbox's own row shape (a cited review opened
        # from a diagnosis). Scoped to the restaurant like every other read.
        where.append("id=?"); params.append(int(review_id))
    if filter_by == "urgent":
        where.append("urgency='high'")
    elif filter_by in ("positive","neutral","negative"):
        where.append("sentiment=?"); params.append(filter_by)
    elif filter_by == "pending":
        # "To approve" is everything still sitting in the queue: a drafted
        # reply waiting on a decision AND a review with no draft yet. It
        # meant only 'drafted' here while the web's own client-side filter
        # meant something else again and iOS a third thing.
        where.append("response_status IN ('pending','drafted')")
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
    total = None
    if include_total:
        total = conn.execute(
            f"SELECT COUNT(*) FROM reviews WHERE {' AND '.join(where)}", params
        ).fetchone()[0]
    # Severity orders ahead of sentiment now. A guest-safety report and a
    # parking gripe were both simply "negative" and sorted identically; the
    # tier the analyser assigns is the one thing that says which of twelve
    # open complaints to read first. NULL severity (analysed before the column
    # existed) sorts with 'service', neither pushed to the top nor buried.
    sql = f"""SELECT * FROM reviews WHERE {' AND '.join(where)}
        ORDER BY CASE urgency WHEN 'high' THEN 0 ELSE 1 END,
        CASE COALESCE(severity,'service')
             WHEN 'safety' THEN 0 WHEN 'legal' THEN 1 WHEN 'operational' THEN 2
             WHEN 'service' THEN 3 ELSE 4 END,
        CASE sentiment WHEN 'negative' THEN 0 WHEN 'neutral' THEN 1 ELSE 2 END,
        COALESCE(NULLIF(review_date,''), fetched_at) DESC"""
    page_params = list(params)
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        page_params += [int(limit), int(offset or 0)]
    rows = conn.execute(sql, page_params).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["categories"] = json.loads(d["categories"] or "[]")
        d["processed"] = bool(d.get("processed"))
        # Whether Retract can possibly work on this review, so a client
        # doesn't have to infer it from response_status alone and offer a
        # button that always 400s (see client_api._do_retract's gate:
        # posted AND google AND a real review_name from our own auto-post).
        d["can_retract"] = bool(
            d.get("response_status") == "posted"
            and d.get("platform") == "google"
            and d.get("review_name")
        )
        # The operational read of this review. `summary` was generated on
        # every single review, stored, and then rendered by nothing on either
        # platform — the only per-review AI reasoning the system produced was
        # dead output. These four fields are what the card now shows.
        try:
            d["entities"] = json.loads(d.get("entities") or "null")
        except Exception:
            d["entities"] = None
        d["severity"] = d.get("severity") or None
        d["severity_label"] = _SEVERITY_LABELS.get(d.get("severity") or "", None)
        d["specific_complaint"] = d.get("specific_complaint") or None
        result.append(d)
    if include_total:
        return result, total
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
    """Return approved-as-is / edited / regenerated counts for the given window.

    Only a person's handling of one draft is counted: 'auto_approved' (the
    rule) and 'bulk_approved' (a publish-many, nobody read the draft) are
    left out of every bucket and the total on purpose."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT response_action, COUNT(*) as cnt
        FROM reviews
        WHERE restaurant_id=?
          AND deleted_at IS NULL
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
    """True when the restaurant's OWN clock is inside its quiet window. It
    read America/Chicago for every restaurant, so a Los Angeles owner's
    "10pm to 7am" ended at 5am their time (MOD-NOT-4)."""
    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT alert_quiet_start, alert_quiet_end, timezone FROM restaurants WHERE id=?",
        (restaurant_id,)
    ).fetchone()
    conn.close()
    if not row or not row["alert_quiet_start"] or not row["alert_quiet_end"]:
        return False
    try:
        from datetime import datetime as _dt
        from time_utils import restaurant_tz
        now = _dt.now(restaurant_tz(row["timezone"] if "timezone" in row.keys() else None))
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

# alert_log is two things at once: the notification HISTORY both clients
# read, and the tally the daily cap and the hard ceiling are counted from.
# The advisory notifications write history rows so they stop being invisible
# to the bell — but they must not consume an owner's "max 3 alerts a day",
# because they are not alerts: the brief is one a day by construction, the
# pulse only fires when the day is genuinely off, and an issue text goes to
# a manager, not to the owner whose cap it would otherwise spend.
NON_ALERT_TYPES = (
    "morning_brief", "intraday_pulse", "closing_summary", "weekly_review",
    "monthly_review", "daily_briefing", "schedule_drafted", "outcome_achieved",
    "issue", "issue_escalated", "coverage", "demand_opportunity",
    "while_away", "connection_lost", "schedule_publish_pending", "order_send_pending",
    "order_send_voided",
    # A manager's task notices (re-audit A-6): a staff drop/swap/time-off
    # request and what became of it, and the 9am "waiting on you in Labor".
    "shift_request", "labor_reminder",
    # Auto-publish's held notice and a milestone (re-audit A-18), and a
    # supplier order the trusted-order job held back (A-22).
    "schedule_publish_held", "milestone", "order_send_held",
    # The nightly Daily Sales Report — one a night by construction
    # (dsr.deliver's claims), so never an alert against the cap.
    "dsr",
)


def count_briefings_today(restaurant_id: int, db_path: str = DB_PATH, types=None) -> int:
    """Briefing-style notifications sent so far in the restaurant's own day —
    the NON_ALERT_TYPES rows count_alerts_today deliberately excludes.

    The retention audit found twelve types exempt from the 50/day ceiling
    with no budget of their own: a POS-connected owner could receive a
    morning brief, a pre-shift nudge, a pre-dinner pulse, a coverage push, a
    demand opportunity and a closing summary in one day, none of which
    counted. notify.briefing_allowed reads this, passing `types` — only the
    briefings the budget governs (notify.budgeted_briefing_types)."""
    # fired_at is UTC; comparing its date to the local date reset this
    # budget at 7pm Chicago — the same bug count_alerts_today fixed.
    since = _local_day_start_utc(restaurant_id)
    types = tuple(types) if types is not None else NON_ALERT_TYPES
    if not types:
        return 0
    placeholders = ",".join("?" * len(types))
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM alert_log WHERE restaurant_id=? "
            f"AND fired_at >= ? AND alert_type IN ({placeholders})",
            (restaurant_id, since, *types)).fetchone()
        return int((row["n"] if row else 0) or 0)
    except Exception:
        return 0
    finally:
        conn.close()


def _local_day_start_utc(restaurant_id: int) -> str:
    """The restaurant's local midnight, expressed in UTC the way alert_log
    stores fired_at — the one clock every per-day count must use."""
    try:
        from time_utils import restaurant_now_by_id, restaurant_tz
        local_now = restaurant_now_by_id(restaurant_id)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=restaurant_tz(None))
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # Never let a timezone lookup turn into "no cap at all".
        return datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")


def count_alerts_today(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """Alerts sent so far in the RESTAURANT's own day.

    This compared against date('now'), which sqlite evaluates in UTC. For a
    Chicago restaurant that rolls over at 7pm local, so "max 5 alerts a day"
    was really five before dinner service and five more during it — the cap
    reset in the middle of the shift it existed to protect. fired_at is
    stored in UTC, so the local midnight is converted back to UTC to compare.
    """
    since = _local_day_start_utc(restaurant_id)
    conn = get_conn(db_path)
    placeholders = ",".join("?" * len(NON_ALERT_TYPES))
    row = conn.execute(
        f"SELECT COUNT(*) as c FROM alert_log WHERE restaurant_id=? AND fired_at >= ? "
        f"AND alert_type NOT IN ({placeholders})",
        (restaurant_id, since, *NON_ALERT_TYPES)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


# ── Notification read state (per login) ──────────────────────────────────────
#
# Written in SQLite's own timestamp format, deliberately. alert_log.fired_at
# is `datetime('now')` — "2026-09-19 11:00:00" — and the unread query is a
# TEXT comparison. The previous stamp used isoformat's 'T' separator, and
# ' ' (0x20) sorts below 'T' (0x54), so EVERY alert fired on the same
# calendar date as the last read compared as older than the read and was
# counted as already seen. The badge could only ever show yesterday's news.

def _now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def mark_notifications_seen(user_id: int, restaurant_id: int, db_path: str = DB_PATH) -> str:
    """Stamp this login's read mark. Returns the stamp written."""
    stamp = _now_sql()
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO notification_reads (user_id, restaurant_id, seen_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id, restaurant_id) DO UPDATE SET seen_at=excluded.seen_at",
            (int(user_id), int(restaurant_id), stamp))
        conn.commit()
    finally:
        conn.close()
    return stamp


def notifications_seen_at(user_id: int, restaurant_id: int, db_path: str = DB_PATH):
    """This login's read mark, falling back to the restaurant-wide stamp for
    a login that has never opened the list on this build. The fallback is
    normalised out of the old ISO 'T' form so it compares correctly."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT seen_at FROM notification_reads WHERE user_id=? AND restaurant_id=?",
            (int(user_id), int(restaurant_id))).fetchone()
        if row and row["seen_at"]:
            return row["seen_at"]
        legacy = conn.execute(
            "SELECT notifications_seen_at FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    value = legacy["notifications_seen_at"] if legacy else None
    return value.replace("T", " ")[:19] if value else None


def unread_notification_count(user_id: int, restaurant_id: int, db_path: str = DB_PATH,
                              visible=None) -> int:
    """This login's unread badge.

    Counts only what the list would show this login: `visible(alert_type)`
    is the caller's role filter (client_api.notification_visibility), and
    nothing from before the login existed — a co-owner invited today was
    badged with the restaurant's entire history, and a manager was badged
    for food-cost rows their list never shows (MOD-NOT-10)."""
    since = notifications_seen_at(user_id, restaurant_id, db_path)
    conn = get_conn(db_path)
    try:
        born = conn.execute("SELECT created_at FROM users WHERE id=?", (int(user_id or 0),)).fetchone()
        born = (born["created_at"] or "").replace("T", " ")[:19] if born else ""
        if since and since >= born:
            where, arg = "fired_at > ?", since
        else:
            where, arg = "fired_at >= ?", born
        rows = conn.execute(
            f"SELECT alert_type, COUNT(*) AS c FROM alert_log WHERE restaurant_id=? AND {where} "
            "GROUP BY alert_type", (restaurant_id, arg)).fetchall()
    finally:
        conn.close()
    return sum(r["c"] for r in rows if visible is None or visible(r["alert_type"]))


def record_notification_open(restaurant_id: int, alert_type: str, user_id: int = None,
                             db_path: str = DB_PATH, alert_log_id: int = None, rec_key: str = None):
    """One row when a notification is actually opened.

    `alert_log_id` is the notification's own history row, carried in the
    push payload, so time-to-open (opened_at - alert_log.fired_at) is
    measurable per notification. Only accepted when that row belongs to this
    restaurant — the id arrives from a client."""
    if not alert_type:
        return
    conn = get_conn(db_path)
    try:
        log_id = None
        if alert_log_id:
            try:
                ok = conn.execute("SELECT 1 FROM alert_log WHERE id=? AND restaurant_id=?",
                                  (int(alert_log_id), restaurant_id)).fetchone()
                log_id = int(alert_log_id) if ok else None
            except (TypeError, ValueError):
                log_id = None
        conn.execute(
            "INSERT INTO notification_opens (restaurant_id, user_id, alert_type, alert_log_id, rec_key) "
            "VALUES (?,?,?,?,?)",
            (restaurant_id, user_id, str(alert_type)[:64], log_id, (str(rec_key)[:160] if rec_key else None)))
        conn.commit()
    finally:
        conn.close()


# alert_log.value holds "the figure the alert fired on", and that figure is a
# dollar amount for only some types: critical_low stores an ITEM COUNT,
# price_spike a PERCENTAGE, demand_opportunity a whole night's typical
# SALES. Summing the column across types added items and percentages to
# dollars. Only types listed here — whose value is money at stake — count,
# each with the module whose view it belongs to.
SURFACED_DOLLAR_ALERTS = {
    "food_waste": "inventory",      # notify: the week's waste cost, $
}


def money_surfaced(restaurant_id: int, days: int = 30, db_path: str = DB_PATH,
                   denied_modules=None) -> dict:
    """What the alerts Cavnar AI raised were worth, in the dollars they
    already carried.

    Counted per ALERT, which is what the owner experienced, and only for the
    alert types in SURFACED_DOLLAR_ALERTS: a type whose stored value is not
    dollars is not money. `denied_modules` drops a type the viewer's role may
    not see BEFORE the sum, so a manager without Food Cost cannot read the
    waste dollars back out of a total.
    """
    denied = set(denied_modules or ())
    types = [t for t, mod in SURFACED_DOLLAR_ALERTS.items() if mod not in denied]
    if not types:
        return {"days": int(days), "items": [], "dollars": 0.0, "alerts": 0,
                "basis": "alerts whose figure is dollars at stake"}
    since = f"-{int(days)} days"
    marks = ",".join("?" for _ in types)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT alert_type, COUNT(*) AS n, COALESCE(SUM(value),0) AS total "
            f"FROM alert_log WHERE restaurant_id=? AND value IS NOT NULL "
            f"AND alert_type IN ({marks}) "
            f"AND fired_at >= datetime('now', ?) "
            f"GROUP BY alert_type ORDER BY total DESC",
            (restaurant_id, *types, since)).fetchall()
    finally:
        conn.close()
    items = [{"alert_type": r["alert_type"], "count": r["n"],
              "dollars": round(float(r["total"] or 0), 2)} for r in rows]
    return {"days": int(days), "items": items,
            "dollars": round(sum(i["dollars"] for i in items), 2),
            "alerts": sum(i["count"] for i in items),
            "basis": "alerts whose figure is dollars at stake"}


def notification_engagement(restaurant_id: int, days: int = 60, db_path: str = DB_PATH) -> list:
    """[{alert_type, delivered, opened}] over the window, busiest first.

    `delivered` counts alert_log rows — one per notification raised, which
    is the number an owner experienced, rather than push_deliveries' one row
    per device.
    """
    since = f"-{int(days)} days"
    conn = get_conn(db_path)
    try:
        sent = {r["alert_type"]: r["n"] for r in conn.execute(
            "SELECT alert_type, COUNT(*) AS n FROM alert_log WHERE restaurant_id=? "
            "AND fired_at >= datetime('now', ?) GROUP BY alert_type", (restaurant_id, since))}
        opened = {r["alert_type"]: r["n"] for r in conn.execute(
            "SELECT alert_type, COUNT(*) AS n FROM notification_opens WHERE restaurant_id=? "
            "AND opened_at >= datetime('now', ?) GROUP BY alert_type", (restaurant_id, since))}
    finally:
        conn.close()
    rows = [{"alert_type": t, "delivered": n, "opened": opened.get(t, 0)}
            for t, n in sent.items()]
    rows.sort(key=lambda r: -r["delivered"])
    return rows


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
    # Security events the owner should see without asking (security audit).
    "login_locked", "account_frozen", "memory_forgotten", "memory_added", "staff_pin_reset",
    "time_off_decided", "covers_imported",
    "auto_publish_changed", "auto_order_changed", "weekly_plan_changed", "send_delay_changed",
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
    "memory_added": "Fact added to what Cavnar AI remembers",
    "memory_forgotten": "Fact removed from what Cavnar AI remembers",
    "time_off_decided": "Time-off request answered",
    "covers_imported": "Cover counts entered",
    "inventory_counted": "Inventory counted",
    "auto_publish_changed": "Automatic schedule publishing changed",
    "auto_order_changed": "Trusted supplier orders changed",
    "weekly_plan_changed": "Monday plan changed",
    "send_delay_changed": "Send delay changed",
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_compsnap_captured ON competitor_snapshots(captured_at)")  # DATA-40
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
        # A Google rating is 1.0-5.0; a 0 is how an unrated place used to be
        # stored (MOD-INT-3), so "0 then 4.6" is a place getting its first
        # reviews, not a +4.6 swing. Only real ratings are compared.
        rated = [x for x in snaps if x["rating"] is not None and float(x["rating"]) > 0]
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aivq_created ON ai_visibility_query_runs(created_at)")  # DATA-40
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


def ai_visibility_query_history(restaurant_id: int, runs: int = 8, db_path: str = DB_PATH) -> dict:
    """Each question across the last `runs` checks: {"runs": [{run_id, at}],
    "queries": [{query, kind, appeared: [bool|None per run], appearances}]}.
    None where a run did not ask that question. What the score hides — the
    same 3-of-6 can be a steady three or a different three every week."""
    conn = get_conn(db_path)
    try:
        run_rows = conn.execute(
            "SELECT run_id, MAX(created_at) AS at FROM ai_visibility_query_runs WHERE restaurant_id=? "
            "GROUP BY run_id ORDER BY at DESC LIMIT ?", (restaurant_id, int(runs))).fetchall()
        ids = [r["run_id"] for r in run_rows][::-1]
        if not ids:
            return {"runs": [], "queries": []}
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT run_id, query, query_kind, appeared FROM ai_visibility_query_runs "
            f"WHERE restaurant_id=? AND run_id IN ({marks})", (restaurant_id, *ids)).fetchall()
    finally:
        conn.close()
    by_q = {}
    for r in rows:
        q = by_q.setdefault(r["query"], {"query": r["query"], "kind": r["query_kind"], "appeared": [None] * len(ids)})
        q["appeared"][ids.index(r["run_id"])] = bool(r["appeared"])
    out = []
    for q in by_q.values():
        q["appearances"] = sum(1 for x in q["appeared"] if x)
        q["asked"] = sum(1 for x in q["appeared"] if x is not None)
        out.append(q)
    out.sort(key=lambda q: (-q["appearances"], q["query"]))
    return {"runs": [{"run_id": r["run_id"], "at": r["at"]} for r in reversed(run_rows)], "queries": out}


def ai_visibility_sources(restaurant_id: int, limit: int = 20, db_path: str = DB_PATH) -> list:
    """The citation URLs the most recent run's answers were grounded in."""
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
                             db_path: str = DB_PATH, city_basis: str = None):
    """Record one complete visibility run.

    answered/appeared are stored so a later comparison can tell a real
    change from a difference in sample size, and so the drop alert can
    refuse to fire on a sample too small to say anything. city_basis is
    what the run was measured against (MOD-INT-4).
    """
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO ai_visibility_runs (restaurant_id, ai_score, gbp_score, answered, appeared, "
            "city_basis) VALUES (?,?,?,?,?,?)",
            (restaurant_id, ai_score, gbp_score, answered, appeared, city_basis))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def attach_ai_visibility_payload(run_id: int, payload_json: str, db_path: str = DB_PATH):
    """Store the payload a recorded run was shown as (MOD-INT-5)."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE ai_visibility_runs SET payload_json=? WHERE id=?", (payload_json, run_id))
        conn.commit()
    finally:
        conn.close()


def latest_ai_visibility_payload(restaurant_id: int, max_age_days: int = 8,
                                 db_path: str = DB_PATH):
    """(payload dict, created_at) of the newest recorded run younger than
    max_age_days that stored its payload, or None. The weekly job records
    one a week; the Intel tab serves it rather than re-running (MOD-INT-5)."""
    import json as _json
    conn = get_conn(db_path)
    try:
        row = conn.execute("""
            SELECT payload_json, created_at FROM ai_visibility_runs
            WHERE restaurant_id=? AND payload_json IS NOT NULL
              AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC, id DESC LIMIT 1
        """, (restaurant_id, f"-{int(max_age_days)} days")).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        payload = _json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return None
    return (payload, row["created_at"]) if isinstance(payload, dict) else None


def last_two_ai_visibility_runs(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """The two most recent complete runs, newest first, with their samples —
    but only when both were measured against the same city (MOD-INT-4). A
    newest run on a different basis comes back alone, so nothing compares a
    change of ruler as a change in visibility."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT ai_score, answered, appeared, created_at, city_basis
        FROM ai_visibility_runs
        WHERE restaurant_id=? AND ai_score IS NOT NULL
        ORDER BY created_at DESC, id DESC LIMIT 2
    """, (restaurant_id,)).fetchall()
    conn.close()
    runs = [dict(r) for r in rows]
    if len(runs) == 2 and runs[0].get("city_basis") != runs[1].get("city_basis"):
        return runs[:1]
    return runs



# Operational logs nothing prunes.
#
# ai_usage is read by the budget check on EVERY AI call (a SUM over a window)
# and by the admin cost view; job_runs and push_deliveries are diagnostics.
# None of them had a retention policy, so all three grow forever and the
# budget SUM gets slower every day the product is used (audit #17).
#
# The windows are set by what actually reads them: the longest budget window
# is monthly, so a quarter of ai_usage is three times more history than any
# query asks for. Deliberately generous — this is about bounding unbounded
# growth, not about reclaiming bytes.
# (retention days, the column that carries the row's age).
#
# The timestamp column is NOT uniformly `created_at` — job_runs stamps
# `started_at` and email_log stamps `sent_at`. Naming it per table rather than
# assuming is the difference between pruning and a DELETE that raises, gets
# swallowed, and silently never runs.
_LOG_RETENTION_DAYS = {
    "ai_usage": (120, "created_at"),
    "job_runs": (90, "started_at"),
    "push_deliveries": (90, "created_at"),
    "email_log": (365, "sent_at"),    # the client-facing "what did you send me" view
    "activity_log": (180, "created_at"),
}


def prune_operational_logs(db_path: str = DB_PATH) -> dict:
    """Delete operational log rows past their retention window.

    Never touches anything a client reads as a record of their own business:
    reviews have their own owner-controlled retention (purge_expired_reviews),
    and ask_cavnar_actions is explicitly never pruned.

    A table whose timestamp column is missing is reported as a problem rather
    than skipped: silently pruning nothing looks identical to having nothing
    to prune, and the table would grow forever with nobody the wiser.
    """
    deleted, problems = {}, []
    conn = get_conn(db_path)
    try:
        existing = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, (days, column) in _LOG_RETENTION_DAYS.items():
            if table not in existing:
                continue          # not created yet on this database — fine
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                problems.append(f"{table}.{column} missing")
                continue
            cur = conn.execute(
                f"DELETE FROM {table} WHERE {column} < datetime('now', ?)",
                (f"-{int(days)} days",))
            if cur.rowcount:
                deleted[table] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if problems:
        try:
            import ops
            ops.capture(RuntimeError(f"retention could not prune: {problems}"),
                        job="prune_operational_logs", context=", ".join(problems))
        except Exception:
            pass
        deleted["_problems"] = problems
    return deleted


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
                  AND COALESCE(NULLIF(review_date,''), fetched_at) < datetime('now', '-{months * 30} days')
            """, (r["id"],))
            total += cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()
    return total


def _utc_offset_minutes(tz_name: str) -> int:
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    try:
        off = _dt.now(_ZI(tz_name or "America/Chicago")).utcoffset()
    except Exception:
        off = _dt.now(_ZI("America/Chicago")).utcoffset()
    return int(off.total_seconds() // 60) if off is not None else 0


def count_auto_approved_today(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """Auto-approvals since midnight in the RESTAURANT's day (MOD-REV-13).

    It compared log_event's America/Chicago wall-clock stamps with
    date('now','localtime') — the server's date, which on Railway is UTC —
    so from 7pm Central the cap reset and the 8pm slot published a second
    full day's worth of unread replies. Now the restaurant's midnight is
    worked out in SQL from the same clock and expressed in Chicago wall time,
    the zone activity_log is stamped in."""
    conn = get_conn(db_path)
    try:
        tz = None
        try:
            r = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
            tz = r["timezone"] if r else None
        except Exception:
            tz = None
        local = _utc_offset_minutes(tz)
        stamp = _utc_offset_minutes("America/Chicago")
        row = conn.execute("""
            SELECT COUNT(*) AS n FROM activity_log
            WHERE restaurant_id=? AND event_type='review_auto_approved'
              AND created_at >= strftime('%Y-%m-%dT%H:%M:%S',
                                         date('now', ?), ?)
        """, (restaurant_id, f"{local:+d} minutes", f"{stamp - local:+d} minutes")).fetchone()
        return int(row["n"]) if row else 0
    finally:
        conn.close()


AUTO_APPROVE_TRUST_MIN = 10        # approved replies on a star band before it can be trusted
AUTO_APPROVE_TRUST_EDIT_RATE = 0.10  # ...and at most this share of them edited first
AUTO_APPROVE_EARNABLE = (3, 4, 5)   # 1- and 2-star replies are never auto-published


def auto_approve_trust(restaurant_id: int, db_path: str = DB_PATH, days: int = 30) -> dict:
    """Per star band, whether the owner's own history says the drafts can go
    out unread: at least AUTO_APPROVE_TRUST_MIN approved in the window and
    an edit rate at or under AUTO_APPROVE_TRUST_EDIT_RATE. Every approval and
    every edit is already recorded (response_status, draft_edited); this
    reads them back so the product stops asking for a signature it has been
    given thirty times unchanged. Negative bands are never in the answer.

    Only a PERSON's answer is evidence (audit #15). A reply the rule
    auto-approved was stored exactly like an owner's unedited approval, so
    once a band was trusted its own output kept it trusted — the rule was
    grading itself. Those rows (response_action='auto_approved') are left
    out. A drafted reply the owner skipped is a "no" to the draft and counts
    against the band like an edit: it is in the denominator and the
    rejected count, so a band the owner keeps skipping cannot earn trust.

    Two more ways a "yes" was counted that was not one (M-3): a draft the
    owner regenerated before approving is a rejection of the draft they
    were shown, like an edit; and a bulk publish (response_action=
    'bulk_approved') read no single draft, so it is not evidence either way.
    Ten 3-star replies each regenerated three times used to read as
    edit_rate 0.0 and trusted."""
    conn = get_conn(db_path)
    since = f"-{int(days)} days"
    try:
        rows = conn.execute(
            "SELECT rating, COUNT(*) AS n, "
            "SUM(CASE WHEN COALESCE(draft_edited, 0) = 1 OR COALESCE(regenerate_count, 0) > 0 "
            "    OR response_action IN ('edited', 'regenerated') THEN 1 ELSE 0 END) AS edited FROM reviews "
            "WHERE restaurant_id=? AND deleted_at IS NULL AND response_status IN ('approved','posted') "
            "AND COALESCE(response_action, '') NOT IN ('auto_approved', 'bulk_approved') "
            "AND approved_at >= datetime('now', ?) AND rating IN (3,4,5) GROUP BY rating",
            (restaurant_id, since)).fetchall()
        try:
            skipped = conn.execute(
                "SELECT rating, COUNT(*) AS n FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                "AND response_status='skipped' AND draft_response IS NOT NULL AND TRIM(draft_response) != '' "
                "AND skipped_at >= datetime('now', ?) AND rating IN (3,4,5) GROUP BY rating",
                (restaurant_id, since)).fetchall()
        except Exception:
            skipped = []       # a database from before skipped_at existed
    finally:
        conn.close()
    by = {int(r["rating"]): (int(r["n"] or 0), int(r["edited"] or 0)) for r in rows}
    sk = {int(r["rating"]): int(r["n"] or 0) for r in skipped}
    out = {}
    for star in AUTO_APPROVE_EARNABLE:
        n, e = by.get(star, (0, 0))
        s_n = sk.get(star, 0)
        answered = n + s_n
        rejected = e + s_n
        rate = (rejected / answered) if answered else None
        out[star] = {"approved": n, "edited": e, "skipped": s_n, "edit_rate": rate,
                     "trusted": bool(n >= AUTO_APPROVE_TRUST_MIN and rate is not None
                                     and rate <= AUTO_APPROVE_TRUST_EDIT_RATE),
                     "needed": max(0, AUTO_APPROVE_TRUST_MIN - n)}
    return out


SCHEDULE_PUBLISH_TRUST_MIN = 3


def schedule_publish_trust(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """How many of the most recent published schedules RAN CLEAN, counting
    back from the latest until one did not (capped at 10). A schedule
    counts as published when published_at is set — a test share on an
    unpublished draft used to count.

    Clean used to mean "went out unedited". An unedited week proves the
    owner did not look; a week that ran is the evidence auto-publish needs:
    nobody flagged a coverage gap or a no-show as an issue during it, and
    the owner did not have to rewrite it after generation.

    "Nobody flagged" is only evidence when something could have: a week
    counts only if the coverage check was watching it
    (schedule_intel.watched_dates — the live clock-in feed, a routed
    manager, and a POS reading taken during the week). Otherwise trust
    reduced to "went out unedited" again (re-audit A-19)."""
    try:
        import schedule_intel as _si
    except Exception:
        return 0
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT h.id, h.edited_at, h.week_start, h.week_end FROM schedule_history h WHERE h.restaurant_id=? "
            "AND h.published_at IS NOT NULL AND h.superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=h.restaurant_id AND nw.week_start=h.week_start AND nw.published_at IS NOT NULL AND nw.id > h.id) "
            "ORDER BY h.id DESC LIMIT 10", (restaurant_id,)).fetchall()
        n = 0
        for r in rows:
            if r["edited_at"]:
                break
            if r["week_start"] and r["week_end"]:
                try:
                    trouble = conn.execute(
                        "SELECT 1 FROM ops_issues WHERE restaurant_id=? AND kind IN ('coverage', 'no_show') "
                        "AND substr(created_at, 1, 10) BETWEEN ? AND ? LIMIT 1",
                        (restaurant_id, r["week_start"], r["week_end"])).fetchone()
                except Exception:
                    trouble = None
                if trouble:
                    break
            if not (r["week_start"] and r["week_end"]
                    and _si.watched_dates(restaurant_id, r["week_start"], r["week_end"], db_path)):
                break                      # nobody was watching: not evidence it ran clean
            n += 1
    finally:
        conn.close()
    return n


def auto_approve_candidates(restaurant_id: int, db_path: str = DB_PATH, ratings=(5,)) -> list:
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
        WHERE restaurant_id=? AND rating IN ({placeholders}) AND response_status='drafted'
          AND draft_response IS NOT NULL AND deleted_at IS NULL
          AND COALESCE(urgency, 'normal') != 'high'
          -- A draft flagged for stating an action the restaurant may not
          -- have taken is exactly what must not be published unread.
          AND COALESCE(draft_needs_review, 0) = 0
        -- Newest guest first: a first-connect backlog of years-old 5-stars
        -- used to spend the daily cap before this week's (MOD A1 R3 #9).
        ORDER BY COALESCE(review_date, fetched_at) DESC, id DESC
    """.replace("{placeholders}", ",".join("?" * len(ratings))), (restaurant_id, *ratings)).fetchall()
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
            # The columns are current_stock and par_level. This asked for
            # "on_hand" and "par", which exist on no table, so g() returned ""
            # and every export ever produced had two permanently blank columns
            # — on the artifact an owner hands to an accountant.
            w.writerow([g("name"), g("unit"), g("current_stock"), g("par_level"), g("unit_cost"),
                        g("last_order_qty"), g("waste_last_week"), g("updated_at")])
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
        "digest_day", "digest_enabled", "login_notify", "staff_signin_notify", "marketing_emails_opt_out",
        "monthly_review_enabled",
        "alert_1star", "alert_2star", "alert_3star", "alert_health", "alert_neg_spike", "alert_negative_trend",
        "alert_no_response", "alert_5star", "alert_labor_over", "alert_food_waste", "alert_ai_visibility_drop", "alert_competitor_move",
        "alert_health_bypass_quiet", "alert_extra_emails", "push_sound", "urgent_via_email", "urgent_via_sms",
        "alert_quiet_start", "alert_quiet_end", "auto_approve_5star", "auto_approve_4star", "auto_approve_earned",
        "auto_publish_schedule", "auto_order_trusted", "weekly_plan_enabled", "send_delay_minutes", "auto_approve_daily_cap",
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


class DuplicatePurchaseOrder(Exception):
    """An open PO already carries exactly this supplier's draft. Carries the
    existing row's number and when it went out, so the refusal can name it."""

    def __init__(self, po_number, sent_at):
        super().__init__(f"already sent as {po_number}")
        self.po_number = po_number
        self.sent_at = sent_at


def record_purchase_order(restaurant_id: int, supplier_name: str,
                          supplier_email: str, items: list, total_cost: float,
                          db_path: str = DB_PATH, draft_hash: str = None,
                          allow_duplicate: bool = False) -> str:
    """Allocate a PO number and store what was sent, atomically. Returns the
    number.

    `draft_hash` is the durable claim on this exact order (DATA-15): while a
    PO with the same supplier and the same hash is still open (status
    'sent'), a second one raises DuplicatePurchaseOrder instead of being
    written. The re-send guard used to be only a 60-second, process-local
    cooldown, so a retry after a minute or after any deploy put the same
    purchase order in the supplier's inbox again. The check and the insert
    share the BEGIN IMMEDIATE below, so two concurrent sends cannot both
    pass it. `allow_duplicate` is the owner's explicit "send it again".

    The number is MAX+1 over this restaurant's PO numbers (MOD-FC-7). It
    was COUNT(*)+1, and a failed email deletes its row, so voiding any PO
    that was not the newest made every later allocation collide with an
    existing number: five retries, then a raise, for good.

    The number used to be read by a separate COUNT(*) before the supplier
    email went out, and only then inserted against UNIQUE(restaurant_id,
    po_number). Two concurrent sends both computed PO-0001, both emails left,
    and the second insert raised IntegrityError — an uncaught 500 with no PO
    row, no email log and no audit event, in front of an owner who would
    reasonably click send again. Allocating inside BEGIN IMMEDIATE closes
    that window; retrying on a unique collision covers the rest.
    """
    import json as _json
    import sqlite3 as _sqlite3
    conn = get_conn(db_path)
    try:
        for _attempt in range(5):
            try:
                conn.execute("BEGIN IMMEDIATE")
                if draft_hash and not allow_duplicate:
                    dup = conn.execute(
                        "SELECT po_number, sent_at FROM purchase_orders WHERE restaurant_id=? "
                        "AND LOWER(COALESCE(supplier_email,''))=? AND draft_hash=? AND status='sent' "
                        "ORDER BY id DESC LIMIT 1",
                        (restaurant_id, (supplier_email or "").strip().lower(), draft_hash)).fetchone()
                    if dup:
                        conn.rollback()
                        raise DuplicatePurchaseOrder(dup["po_number"], dup["sent_at"])
                n = conn.execute(
                    "SELECT MAX(CAST(SUBSTR(po_number, 4) AS INTEGER)) FROM purchase_orders "
                    "WHERE restaurant_id=? AND po_number LIKE 'PO-%'", (restaurant_id,)
                ).fetchone()[0] or 0
                po_number = f"PO-{n + 1:04d}"
                conn.execute("""
                    INSERT INTO purchase_orders
                        (restaurant_id, po_number, supplier_name, supplier_email, items_json, total_cost,
                         draft_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (restaurant_id, po_number, supplier_name, supplier_email,
                      _json.dumps(items or []), round(float(total_cost or 0), 2), draft_hash))
                conn.commit()
                return po_number
            except _sqlite3.IntegrityError:
                conn.rollback()
                continue
        raise RuntimeError("could not allocate a purchase order number")
    finally:
        conn.close()


def open_purchase_order(restaurant_id: int, supplier_email: str, draft_hash: str,
                        db_path: str = DB_PATH):
    """The open (sent, not yet received) PO carrying exactly this supplier's
    draft, as {po_number, sent_at}, or None."""
    if not draft_hash:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT po_number, sent_at FROM purchase_orders WHERE restaurant_id=? "
            "AND LOWER(COALESCE(supplier_email,''))=? AND draft_hash=? AND status='sent' "
            "ORDER BY id DESC LIMIT 1",
            (restaurant_id, (supplier_email or "").strip().lower(), draft_hash)).fetchone()
    finally:
        conn.close()
    return {"po_number": row["po_number"], "sent_at": row["sent_at"]} if row else None


def void_purchase_order(restaurant_id: int, po_number: str, db_path: str = DB_PATH) -> bool:
    """Remove a PO whose email never left. The row is written first so a sent
    order always has a record; when the send then fails there is nothing to
    keep, and leaving it would make the next number skip and show the owner an
    order the supplier never received."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM purchase_orders WHERE restaurant_id=? AND po_number=? AND status='sent'",
            (restaurant_id, po_number))
        conn.commit()
        return cur.rowcount > 0
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
            "SELECT id, employee_name, email, phone, pos_id FROM staff_contacts "
            "WHERE restaurant_id=? ORDER BY employee_name", (restaurant_id,)
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "employee_name": r["employee_name"],
             "email": r["email"] or "", "phone": r["phone"] or "", "pos_id": r["pos_id"] or ""} for r in rows]


def set_staff_contact(restaurant_id: int, employee_name: str, email: str = None,
                      phone: str = None, db_path: str = DB_PATH, pos_id: str = None) -> bool:
    """Upsert by (restaurant, employee name) — the same key
    staff_availability and staff_notes use, matched however the name's case
    or spacing was typed ("maria g." updates "Maria G.", MOD-EMP-2).

    None means "not given, keep what is stored" for email, phone and pos_id;
    "" clears. Saving only a phone number used to erase the stored email
    (MOD-EMP-4)."""
    name = " ".join((employee_name or "").split())
    if not name:
        return False

    def _v(x):
        return None if x is None else ((x or "").strip() or "")
    e, p, pid = _v(email), _v(phone), _v(pos_id)
    conn = get_conn(db_path)
    try:
        key = name.casefold()
        existing = next((r for r in conn.execute("SELECT id, employee_name FROM staff_contacts WHERE restaurant_id=?",
                                                 (restaurant_id,)).fetchall()
                         if " ".join((r["employee_name"] or "").split()).casefold() == key), None)
        if existing:
            conn.execute("UPDATE staff_contacts SET email=CASE WHEN ? IS NULL THEN email ELSE NULLIF(?, '') END, "
                         "phone=CASE WHEN ? IS NULL THEN phone ELSE NULLIF(?, '') END, "
                         "pos_id=CASE WHEN ? IS NULL OR ?='' THEN pos_id ELSE ? END, updated_at=datetime('now') "
                         "WHERE id=?", (e, e, p, p, pid, pid, pid, existing["id"]))
        else:
            conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name, email, phone, pos_id) "
                         "VALUES (?, ?, ?, ?, ?)", (restaurant_id, name, e or None, p or None, pid or None))
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

# Mail a restaurant's GUESTS receive. A complaint about one of these is about
# that guest list, not about the address: the same person can be a guest of
# one restaurant and on staff at another (MOD-EML-7).
GUEST_EMAIL_TYPES = frozenset({"guest_newsletter", "guest_review_request"})


def suppress_email(email: str, reason: str, detail: str = None, db_path: str = DB_PATH,
                   scope: str = None):
    """Stop sending to an address. Idempotent; the first reason wins so a
    later soft signal can't overwrite a hard bounce.

    `scope` None stops every email (a hard bounce: the mailbox does not
    work for anyone). 'guest' stops only GUEST_EMAIL_TYPES. A later
    all-mail suppression widens a guest-only one; never the reverse."""
    email = (email or "").strip().lower()
    if not email:
        return False
    scope = scope or None
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO email_suppressions (email, reason, detail, scope) VALUES (?,?,?,?)",
            (email, reason, (detail or "")[:500], scope)
        )
        if scope is None:
            conn.execute(
                "UPDATE email_suppressions SET scope=NULL, reason=?, detail=? "
                "WHERE email=? AND scope IS NOT NULL",
                (reason, (detail or "")[:500], email))
        conn.commit()
    finally:
        conn.close()
    return True


def is_email_suppressed(email: str, db_path: str = DB_PATH, email_type: str = None) -> bool:
    """Whether a send of `email_type` to this address is suppressed. A
    guest-scoped row only stops guest-facing mail."""
    email = (email or "").strip().lower()
    if not email:
        return False
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT scope FROM email_suppressions WHERE email=?", (email,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    scope = row["scope"] if "scope" in row.keys() else None
    if not scope:
        return True
    return scope == "guest" and email_type in GUEST_EMAIL_TYPES


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


def mark_email_engagement(message_id: str, kind: str, db_path: str = DB_PATH) -> bool:
    """Stamp the first open or click for a sent email.

    FIRST, not latest: "did this land" is the question, and a mail client
    that re-fetches images on every scroll would otherwise rewrite the
    timestamp all day. Deliberately not written to `status`, which is the
    DELIVERY state — an open must not erase the fact that it was delivered.

    Read these as a floor, not a measurement. Apple Mail Privacy Protection
    pre-fetches images, which counts as an open nobody performed, and a
    reader with images off is a real read that never registers. Useful for
    "nobody has opened this type in 30 days"; useless for a precise rate.
    """
    column = {"opened": "opened_at", "clicked": "clicked_at"}.get(kind)
    if not (message_id and column):
        return False
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            f"UPDATE email_log SET {column}=datetime('now') "
            f"WHERE message_id=? AND {column} IS NULL", (message_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def email_engagement(restaurant_id: int = None, days: int = 30, db_path: str = DB_PATH) -> list:
    """[{email_type, sent, opened, clicked}] over the window, busiest first.

    The product could say how many emails it SENT and nothing about whether
    any were worth sending — the same blind spot notification_opens closed
    for push.
    """
    sql = ("SELECT email_type, COUNT(*) AS sent, "
           "SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened, "
           "SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked "
           "FROM email_log WHERE sent_at >= datetime('now', ?) AND status != 'failed'")
    args = [f"-{int(days)} days"]
    if restaurant_id is not None:
        sql += " AND restaurant_id=?"
        args.append(restaurant_id)
    sql += " GROUP BY email_type ORDER BY sent DESC"
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    for row in rows:
        row["open_rate"] = (round(100.0 * (row["opened"] or 0) / row["sent"], 1)
                            if row["sent"] else None)
    return rows


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

def _link_secret(db_path: str = None) -> bytes:
    """This install's own signing secret for email links, minted once and
    kept in app_secrets. Unsubscribe links were signed with SECRET_KEY, so
    rotating it killed every link already in an inbox (MOD-EML-9)."""
    import secrets as _secrets
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        row = conn.execute("SELECT value FROM app_secrets WHERE name='email_links'").fetchone()
        if not row:
            conn.execute("INSERT OR IGNORE INTO app_secrets (name, value) VALUES ('email_links', ?)",
                         (_secrets.token_hex(32),))
            conn.commit()
            row = conn.execute("SELECT value FROM app_secrets WHERE name='email_links'").fetchone()
        return row["value"].encode()
    finally:
        conn.close()


def _legacy_link_secret() -> bytes:
    import os as _os
    return (_os.getenv("SECRET_KEY") or _os.getenv("RESEND_API_KEY") or "cavnar-fallback").encode()


def _link_sig(secret: bytes, message: str) -> str:
    import hmac, hashlib, base64
    sig = hmac.new(secret, message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")[:24]


def unsubscribe_token(restaurant_id: int) -> str:
    """Signed, stateless one-click unsubscribe token.

    Signed rather than stored so an old link in an old email never stops
    working, and HMAC'd so nobody can unsubscribe a restaurant by walking
    ids. Scoped to marketing only — it never touches security email. Signed
    with this install's own kept secret, not SECRET_KEY (MOD-EML-9).
    """
    return f"{restaurant_id}.{_link_sig(_link_secret(), f'unsub:{restaurant_id}')}"


def verify_unsubscribe_token(token: str):
    """Return the restaurant_id a token authorises, or None. Links signed
    with SECRET_KEY before the kept secret existed still verify while that
    key is unchanged."""
    import hmac as _hmac
    try:
        rid_str, sig = (token or "").split(".", 1)
        rid = int(rid_str)
    except Exception:
        return None
    message = f"unsub:{rid}"
    for secret in (_link_secret(), _legacy_link_secret()):
        if _hmac.compare_digest(_link_sig(secret, message), sig):
            return rid
    return None


def guest_optout_token(email: str) -> str:
    """A signed opt-out for one guest address, for guest mail that has no
    guest_contacts row behind it (the review-request email). The address is
    in the token, base64'd; the signature is what makes it unforgeable."""
    import base64
    email = (email or "").strip().lower()
    enc = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    return f"{enc}.{_link_sig(_link_secret(), f'guest-optout:{email}')}"


def verify_guest_optout_token(token: str):
    """The address a guest opt-out token names, or None."""
    import base64, hmac as _hmac
    try:
        enc, sig = (token or "").rsplit(".", 1)
        email = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode()
    except Exception:
        return None
    if "@" not in email:
        return None
    ok = _hmac.compare_digest(_link_sig(_link_secret(), f"guest-optout:{email}"), sig)
    return email if ok else None


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
    "send_morning_brief":             "Morning brief",
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


def latest_ask_answer_id(restaurant_id, conversation_id, user_id=None, db_path: str = DB_PATH):
    """The id of the newest assistant turn in this conversation for this
    login (the answer just saved) — what a client rates with
    POST /ask-cavnar/feedback. None when there is none."""
    if conversation_id is None:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(id) FROM ask_cavnar_messages WHERE restaurant_id=? AND conversation_id=? AND role='assistant' "
            "AND (user_id IS NULL OR ? IS NULL OR user_id=?)", (restaurant_id, conversation_id, user_id, user_id)).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row and row[0] else None


def record_ask_feedback(restaurant_id, message_id, helpful, note=None, user_id=None, db_path: str = DB_PATH):
    """Rate one Ask answer: `helpful` true/false, an optional note. The
    message must be an ASSISTANT turn of THIS restaurant, given to this
    login (or to nobody in particular) — a rating of another restaurant's,
    or another login's, answer is refused (None), never written. Rating the
    same answer again replaces the rating. Returns the stored row."""
    conn = get_conn(db_path)
    try:
        msg = conn.execute(
            "SELECT id, conversation_id, user_id FROM ask_cavnar_messages WHERE id=? AND restaurant_id=? "
            "AND role='assistant'", (int(message_id), restaurant_id)).fetchone()
        if not msg or (msg["user_id"] is not None and user_id is not None and msg["user_id"] != user_id):
            return None
        note = (str(note).strip()[:500] or None) if note else None
        conn.execute(
            "INSERT INTO ask_feedback (restaurant_id, user_id, message_id, conversation_id, helpful, note) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(restaurant_id, message_id, user_id) DO UPDATE SET "
            "helpful=excluded.helpful, note=excluded.note, updated_at=datetime('now')",
            (restaurant_id, user_id, int(message_id), msg["conversation_id"], 1 if helpful else 0, note))
        conn.commit()
        row = conn.execute("SELECT message_id, conversation_id, helpful, note, updated_at FROM ask_feedback "
                           "WHERE restaurant_id=? AND message_id=? AND user_id IS ?",
                           (restaurant_id, int(message_id), user_id)).fetchone()
    finally:
        conn.close()
    return ({"message_id": row["message_id"], "conversation_id": row["conversation_id"],
             "helpful": bool(row["helpful"]), "note": row["note"], "rated_at": row["updated_at"]} if row else None)


def ask_feedback_summary(restaurant_id, days: int = 90, db_path: str = DB_PATH) -> dict:
    """How this restaurant has rated Ask's answers: {"rated", "helpful",
    "not_helpful", "notes"} over the last `days` — notes are the most recent
    "not helpful" notes (at most two), for the assistant's context. Pure
    SQL; read by the Ask opening and the context snapshot, never a model."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(helpful), 0) AS yes FROM ask_feedback "
                           "WHERE restaurant_id=? AND updated_at >= datetime('now', ?)",
                           (restaurant_id, f"-{int(days)} days")).fetchone()
        notes = [r["note"] for r in conn.execute(
            "SELECT note FROM ask_feedback WHERE restaurant_id=? AND helpful=0 AND note IS NOT NULL "
            "AND updated_at >= datetime('now', ?) ORDER BY updated_at DESC, id DESC LIMIT 2",
            (restaurant_id, f"-{int(days)} days")).fetchall()]
    except Exception:
        return {"rated": 0, "helpful": 0, "not_helpful": 0, "notes": []}
    finally:
        conn.close()
    n, yes = int(row["n"] or 0), int(row["yes"] or 0)
    return {"rated": n, "helpful": yes, "not_helpful": n - yes, "notes": notes, "days": int(days)}


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
                   user_id=None, proposal_id=None, reason=None, db_path: str = DB_PATH):
    """Audit trail for anything the assistant proposed.

    Written at proposal time and again at confirm/dismiss, so "did the
    assistant send that, and who approved it" is answerable after the fact.

    `proposal_id` ties a confirm/dismiss to the ONE proposal it answers —
    the id of that proposal's own row — so two proposals with the same
    action name are no longer settled together. Proposal rows leave it
    empty (their id IS the proposal id); the table stays append-only.
    `reason` is the owner's optional why on a dismissal.
    """
    import json as _json
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO ask_cavnar_actions (restaurant_id, user_id, action, summary, body, outcome, "
            "proposal_id, reason) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, user_id, action, summary,
             _json.dumps(body) if body else None, outcome, proposal_id,
             (str(reason).strip()[:300] or None) if reason else None)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_ask_proposal(restaurant_id, proposal_id, db_path: str = DB_PATH):
    """The proposal row `proposal_id` names, scoped to this restaurant, with
    its settlement (the latest confirm/dismiss row for it), or None."""
    conn = get_conn(db_path)
    try:
        p = conn.execute("SELECT id, action, summary, body, outcome, created_at FROM ask_cavnar_actions "
                         "WHERE id=? AND restaurant_id=? AND outcome='proposed'",
                         (proposal_id, restaurant_id)).fetchone()
        if not p:
            return None
        s = conn.execute("SELECT outcome, reason, created_at FROM ask_cavnar_actions WHERE restaurant_id=? "
                         "AND proposal_id=? AND outcome!='proposed' ORDER BY id DESC LIMIT 1",
                         (restaurant_id, proposal_id)).fetchone()
    finally:
        conn.close()
    out = dict(p)
    out["settled"] = dict(s) if s else None
    return out


def get_ask_actions(restaurant_id, limit: int = 50, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, action, summary, outcome, proposal_id, reason, created_at FROM ask_cavnar_actions "
            "WHERE restaurant_id=? ORDER BY id DESC LIMIT ?", (restaurant_id, limit)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── credentials read back as plaintext ───────────────────────────────────────
# Wrapped at module end so every importer binds the decrypting version.
# Decrypting is idempotent (a plaintext value has no prefix), so the memoised
# instance being decrypted twice is harmless.
def _decrypting(fn, many=False):
    import functools

    @functools.wraps(fn)
    def inner(*a, **k):
        out = fn(*a, **k)
        try:
            import credentials as _cred
            if many:
                for r in (out or []):
                    _cred.decrypt_restaurant(r)
            elif out is not None:
                _cred.decrypt_restaurant(out)
        except Exception:
            pass
        return out
    return inner


get_restaurant = _decrypting(get_restaurant)
get_all_restaurants = _decrypting(get_all_restaurants, many=True)
