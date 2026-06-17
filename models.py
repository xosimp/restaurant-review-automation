import sqlite3
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

DB_PATH = "reviews.db"


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
    stripe_customer_id TEXT,              -- Stripe customer ID for billing lookup
    docusign_envelope_id TEXT,            -- DocuSign envelope ID for contract tracking
    contract_status TEXT DEFAULT 'pending', -- pending/sent/signed
    location_group  TEXT,                 -- group name for multi-location clients (e.g. "Syrup")
    location_name   TEXT,                 -- specific location name (e.g. "Lincoln Park")
    inventory_frequency TEXT DEFAULT 'weekly', -- how often to request inventory data
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
    UNIQUE(platform, external_id)
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
    stripe_customer_id: Optional[str]    = None
    docusign_envelope_id: Optional[str]  = None
    contract_status: str                 = "pending"
    location_group: Optional[str]        = None
    location_name: Optional[str]         = None
    inventory_frequency: str             = "weekly"
    inventory_notes: Optional[str]       = None
    food_cost_target: float              = 30.0
    monthly_revenue_target: float        = 0.0
    hours_notes: Optional[str]           = None
    role_rates_json: Optional[str]       = None
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
    pos_system: Optional[str]       = None
    owner_name: Optional[str]       = None
    owner_phone: Optional[str]      = None
    digest_day: str                 = "monday"
    digest_enabled: int             = 1
    last_fetched_at: Optional[str]  = None
    reviews_live: int               = 0
    billing_status: str             = "trial"
    two_fa_enabled: int             = 0
    two_fa_code: str                = None
    two_fa_expires: str             = None
    two_fa_device_token: str        = None
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

def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_columns(db_path: str = DB_PATH):
    """Ensure all required columns exist — runs on every startup."""
    conn = get_conn(db_path)
    columns_to_add = [
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
        ("restaurants", "inventory_notes", "TEXT"),
        ("restaurants", "food_cost_target", "REAL"),
        ("restaurants", "monthly_revenue_target", "REAL"),
        ("restaurants", "hours_notes", "TEXT"),
        ("restaurants", "role_rates_json", "TEXT"),
        ("restaurants", "inventory_updated_at", "TEXT"),
        ("restaurants", "gbp_rating", "REAL"),
        ("restaurants", "gbp_review_count", "INTEGER"),
        ("client_data", "food_cost_json", "TEXT"),
        ("labor_daily_history", "total_hours", "REAL"),
    ]
    for table, col, col_type in columns_to_add:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
            conn.commit()
            print(f"Added column {table}.{col}")
        except Exception:
            pass  # Column already exists
    conn.close()

def init_db(db_path: str = DB_PATH):
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
    ]
    for m in migrations:
        try:
            conn.execute(m)
        except Exception:
            pass  # column already exists
    conn.commit()
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
    if row and row["shifts_source"] == "upload":
        conn.close()
        return  # preserve real data

    # Seed June 2025 daily history (YoY context for schedule generation)
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
        conn.execute("""
            INSERT OR REPLACE INTO labor_daily_history
              (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, total_hours, saved_at)
            VALUES (?,?,?,?,?,?,?,datetime('now'))
        """, (2, ds, DAYS_OF_WEEK[d.weekday()], labor_pct, labor_cost, sales, hours))
        d += timedelta(days=1)

    # Commit and close before calling helpers that open their own connections
    conn.commit()
    conn.close()

    gia_mia_hours = (
        "RESTAURANT HOURS: Open 11:00am daily. "
        "Close: 9:00pm Sun–Wed; 10:00pm Thu–Sat. "
        "\n\nSTAFF ARRIVAL TIMES (hard rules — do not deviate):\n"
        "- Bussers: arrive 8:00am every day.\n"
        "- Cooks: arrive 8:30am Mon–Thu; arrive 8:00am Fri, Sat, Sun.\n"
        "- Servers: arrive 10:00am every day (1 hour before 11am open for side work).\n"
        "- Bartenders: NO morning shifts — evening only. "
        "Bartenders start no earlier than 3:00pm. "
        "Bartenders stay 1 hour after close: until 10:00pm Sun–Wed; until 11:00pm Thu–Sat.\n"
        "\nSHIFT END / CLOSER RULES:\n"
        "- Always keep 2 servers as closers (until restaurant close time).\n"
        "- Cut all other servers 1–1.5h before close when volume allows.\n"
        "- Fri/Sat: 3 server closers + 2 bartender closers.\n"
        "- Cooks: 1 cook always stays through close; cut others 45min–1h early on slow nights.\n"
        "- Bussers cut 30min before close."
    )
    # Use update_restaurant so the correct DB connection path is always used
    update_restaurant(2, {
        "monthly_revenue_target": 365000.0,
        "labor_target_pct": 23.0,
        "hours_notes": gia_mia_hours,
        "role_rates_json": None,
    })
    print("[auto-seed] Restaurant settings updated: labor_target=23%, monthly_revenue=$365k")

    gia_mia_csv = """date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes
2026-06-01,Monday,Sofia R.,Server,4:30pm,9:30pm,5.0,5.0,8000,
2026-06-01,Monday,Marcus T.,Server,4:30pm,10:00pm,5.5,5.5,8000,
2026-06-01,Monday,Jamie L.,Server,11:00am,4:00pm,5.0,5.0,8000,
2026-06-01,Monday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Elena V.,Server,4:00pm,9:00pm,5.0,5.0,8000,
2026-06-01,Monday,Derek M.,Bartender,4:00pm,11:00pm,7.0,7.0,8000,
2026-06-01,Monday,Tomas H.,Bartender,4:30pm,10:00pm,5.5,5.5,8000,
2026-06-01,Monday,Carlos B.,Cook,10:30am,7:00pm,8.5,8.5,8000,
2026-06-01,Monday,Amy C.,Cook,3:00pm,10:00pm,7.0,7.0,8000,
2026-06-01,Monday,Raj P.,Cook,11:00am,7:00pm,8.0,8.0,8000,
2026-06-01,Monday,James H.,Host,11:00am,5:30pm,6.5,6.5,8000,
2026-06-01,Monday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-02,Tuesday,Sofia R.,Server,4:30pm,10:00pm,5.5,5.5,9000,
2026-06-02,Tuesday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-02,Tuesday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-02,Tuesday,Elena V.,Server,4:00pm,9:30pm,5.5,5.5,9000,
2026-06-02,Tuesday,Nina W.,Server,4:30pm,10:00pm,5.5,5.5,9000,
2026-06-02,Tuesday,Derek M.,Bartender,4:00pm,11:30pm,7.5,7.5,9000,
2026-06-02,Tuesday,Tomas H.,Bartender,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Carlos B.,Cook,10:30am,7:00pm,8.5,8.5,9000,
2026-06-02,Tuesday,Amy C.,Cook,2:30pm,10:30pm,8.0,8.0,9000,
2026-06-02,Tuesday,Raj P.,Cook,11:00am,7:30pm,8.5,8.5,9000,
2026-06-02,Tuesday,James H.,Host,11:00am,5:30pm,6.5,6.5,9000,
2026-06-02,Tuesday,Lena S.,Host,4:00pm,10:30pm,6.5,6.5,9000,
2026-06-03,Wednesday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Marcus T.,Server,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Priya K.,Server,11:00am,5:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Elena V.,Server,4:00pm,10:30pm,6.5,6.5,11000,
2026-06-03,Wednesday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Marco D.,Server,5:00pm,10:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Derek M.,Bartender,4:00pm,11:30pm,7.5,7.5,11000,
2026-06-03,Wednesday,Tomas H.,Bartender,4:30pm,11:30pm,7.0,7.0,11000,
2026-06-03,Wednesday,Carlos B.,Cook,10:00am,7:00pm,9.0,9.0,11000,
2026-06-03,Wednesday,Amy C.,Cook,2:00pm,11:00pm,9.0,9.0,11000,
2026-06-03,Wednesday,Raj P.,Cook,11:00am,7:30pm,8.5,8.5,11000,
2026-06-03,Wednesday,Leo K.,Cook,3:00pm,10:00pm,7.0,7.0,11000,
2026-06-03,Wednesday,James H.,Host,11:00am,5:30pm,6.5,6.5,11000,
2026-06-03,Wednesday,Lena S.,Host,4:00pm,11:00pm,7.0,7.0,11000,
2026-06-04,Thursday,Sofia R.,Server,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Marcus T.,Server,4:30pm,11:30pm,7.0,7.0,13000,
2026-06-04,Thursday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Priya K.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Elena V.,Server,4:00pm,11:00pm,7.0,7.0,13000,
2026-06-04,Thursday,Nina W.,Server,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Gina F.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Derek M.,Bartender,4:00pm,12:00am,8.0,8.0,13000,
2026-06-04,Thursday,Tomas H.,Bartender,4:30pm,11:30pm,7.0,7.0,13000,
2026-06-04,Thursday,Kim T.,Bartender,6:00pm,12:00am,6.0,6.0,13000,
2026-06-04,Thursday,Carlos B.,Cook,10:00am,7:30pm,9.5,9.5,13000,
2026-06-04,Thursday,Amy C.,Cook,2:00pm,11:00pm,9.0,9.0,13000,
2026-06-04,Thursday,Raj P.,Cook,11:00am,8:00pm,9.0,9.0,13000,
2026-06-04,Thursday,Leo K.,Cook,3:00pm,11:00pm,8.0,8.0,13000,
2026-06-04,Thursday,James H.,Host,11:00am,6:00pm,7.0,7.0,13000,
2026-06-04,Thursday,Lena S.,Host,4:00pm,11:30pm,7.5,7.5,13000,
2026-06-05,Friday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-05,Friday,Marcus T.,Server,4:30pm,12:00am,7.5,7.5,18000,
2026-06-05,Friday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-05,Friday,Priya K.,Server,4:30pm,11:30pm,7.0,7.0,18000,
2026-06-05,Friday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-05,Friday,Nina W.,Server,4:30pm,12:00am,7.5,7.5,18000,
2026-06-05,Friday,Marco D.,Server,5:00pm,12:00am,7.0,7.0,18000,
2026-06-05,Friday,Gina F.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-05,Friday,Tony A.,Busser,4:30pm,12:00am,7.5,7.5,18000,
2026-06-05,Friday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-05,Friday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,18000,
2026-06-05,Friday,Tomas H.,Bartender,4:30pm,12:00am,7.5,7.5,18000,
2026-06-05,Friday,Kim T.,Bartender,5:30pm,12:00am,6.5,6.5,18000,
2026-06-05,Friday,Carlos B.,Cook,10:00am,8:00pm,10.0,10.0,18000,
2026-06-05,Friday,Amy C.,Cook,12:00pm,10:00pm,10.0,10.0,18000,
2026-06-05,Friday,Raj P.,Cook,10:30am,8:30pm,10.0,10.0,18000,
2026-06-05,Friday,Leo K.,Cook,3:00pm,12:00am,9.0,9.0,18000,
2026-06-05,Friday,James H.,Host,11:00am,6:00pm,7.0,7.0,18000,
2026-06-05,Friday,Lena S.,Host,4:30pm,12:00am,7.5,7.5,18000,
2026-06-06,Saturday,Sofia R.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Marcus T.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Priya K.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Elena V.,Server,11:00am,5:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Nina W.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Marco D.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Gina F.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Tony A.,Busser,11:00am,6:00pm,7.0,7.0,20000,
2026-06-06,Saturday,Sam V.,Busser,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Tomas H.,Bartender,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Kim T.,Bartender,5:00pm,12:00am,7.0,7.0,20000,
2026-06-06,Saturday,Carlos B.,Cook,9:00am,7:00pm,10.0,10.0,20000,
2026-06-06,Saturday,Amy C.,Cook,11:00am,9:00pm,10.0,10.0,20000,
2026-06-06,Saturday,Raj P.,Cook,9:30am,7:30pm,10.0,10.0,20000,
2026-06-06,Saturday,Leo K.,Cook,2:00pm,12:00am,10.0,10.0,20000,
2026-06-06,Saturday,James H.,Host,11:00am,7:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Lena S.,Host,4:30pm,12:00am,7.5,7.5,20000,
2026-06-07,Sunday,Sofia R.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,12250,
2026-06-07,Sunday,Priya K.,Server,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Elena V.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Marco D.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,12250,
2026-06-07,Sunday,Tomas H.,Bartender,4:00pm,11:00pm,7.0,7.0,12250,
2026-06-07,Sunday,Kim T.,Bartender,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Carlos B.,Cook,9:00am,6:30pm,9.5,9.5,12250,
2026-06-07,Sunday,Amy C.,Cook,11:00am,8:00pm,9.0,9.0,12250,
2026-06-07,Sunday,Raj P.,Cook,10:00am,7:00pm,9.0,9.0,12250,
2026-06-07,Sunday,Leo K.,Cook,2:00pm,10:00pm,8.0,8.0,12250,
2026-06-07,Sunday,James H.,Host,11:00am,5:30pm,6.5,6.5,12250,
2026-06-07,Sunday,Lena S.,Host,4:00pm,10:30pm,6.5,6.5,12250,
2026-06-08,Monday,Sofia R.,Server,4:30pm,9:30pm,5.0,5.0,8000,
2026-06-08,Monday,Marcus T.,Server,4:30pm,10:00pm,5.5,5.5,8000,
2026-06-08,Monday,Jamie L.,Server,11:00am,4:00pm,5.0,5.0,8000,
2026-06-08,Monday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Elena V.,Server,4:00pm,9:00pm,5.0,5.0,8000,
2026-06-08,Monday,Derek M.,Bartender,4:00pm,11:00pm,7.0,7.0,8000,
2026-06-08,Monday,Tomas H.,Bartender,4:30pm,10:00pm,5.5,5.5,8000,
2026-06-08,Monday,Carlos B.,Cook,10:30am,7:00pm,8.5,8.5,8000,
2026-06-08,Monday,Amy C.,Cook,3:00pm,10:00pm,7.0,7.0,8000,
2026-06-08,Monday,Raj P.,Cook,11:00am,7:00pm,8.0,8.0,8000,
2026-06-08,Monday,James H.,Host,11:00am,5:30pm,6.5,6.5,8000,
2026-06-08,Monday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-09,Tuesday,Sofia R.,Server,4:30pm,10:00pm,5.5,5.5,9000,
2026-06-09,Tuesday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Elena V.,Server,4:00pm,9:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Nina W.,Server,4:30pm,10:00pm,5.5,5.5,9000,
2026-06-09,Tuesday,Derek M.,Bartender,4:00pm,11:30pm,7.5,7.5,9000,
2026-06-09,Tuesday,Tomas H.,Bartender,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Carlos B.,Cook,10:30am,7:00pm,8.5,8.5,9000,
2026-06-09,Tuesday,Amy C.,Cook,2:30pm,10:30pm,8.0,8.0,9000,
2026-06-09,Tuesday,Raj P.,Cook,11:00am,7:30pm,8.5,8.5,9000,
2026-06-09,Tuesday,James H.,Host,11:00am,5:30pm,6.5,6.5,9000,
2026-06-09,Tuesday,Lena S.,Host,4:00pm,10:30pm,6.5,6.5,9000,
2026-06-10,Wednesday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Marcus T.,Server,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-10,Wednesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-10,Wednesday,Priya K.,Server,11:00am,5:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Elena V.,Server,4:00pm,10:30pm,6.5,6.5,11000,
2026-06-10,Wednesday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Marco D.,Server,5:00pm,10:30pm,5.5,5.5,11000,
2026-06-10,Wednesday,Derek M.,Bartender,4:00pm,11:30pm,7.5,7.5,11000,
2026-06-10,Wednesday,Tomas H.,Bartender,4:30pm,11:30pm,7.0,7.0,11000,
2026-06-10,Wednesday,Carlos B.,Cook,10:00am,7:00pm,9.0,9.0,11000,
2026-06-10,Wednesday,Amy C.,Cook,2:00pm,11:00pm,9.0,9.0,11000,
2026-06-10,Wednesday,Raj P.,Cook,11:00am,7:30pm,8.5,8.5,11000,
2026-06-10,Wednesday,Leo K.,Cook,3:00pm,10:00pm,7.0,7.0,11000,
2026-06-10,Wednesday,James H.,Host,11:00am,5:30pm,6.5,6.5,11000,
2026-06-10,Wednesday,Lena S.,Host,4:00pm,11:00pm,7.0,7.0,11000,
2026-06-11,Thursday,Sofia R.,Server,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-11,Thursday,Marcus T.,Server,4:30pm,11:30pm,7.0,7.0,13000,
2026-06-11,Thursday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Priya K.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Elena V.,Server,4:00pm,11:00pm,7.0,7.0,13000,
2026-06-11,Thursday,Nina W.,Server,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-11,Thursday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Gina F.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-11,Thursday,Derek M.,Bartender,4:00pm,12:00am,8.0,8.0,13000,
2026-06-11,Thursday,Tomas H.,Bartender,4:30pm,11:30pm,7.0,7.0,13000,
2026-06-11,Thursday,Kim T.,Bartender,6:00pm,12:00am,6.0,6.0,13000,
2026-06-11,Thursday,Carlos B.,Cook,10:00am,7:30pm,9.5,9.5,13000,
2026-06-11,Thursday,Amy C.,Cook,2:00pm,11:00pm,9.0,9.0,13000,
2026-06-11,Thursday,Raj P.,Cook,11:00am,8:00pm,9.0,9.0,13000,
2026-06-11,Thursday,Leo K.,Cook,3:00pm,11:00pm,8.0,8.0,13000,
2026-06-11,Thursday,James H.,Host,11:00am,6:00pm,7.0,7.0,13000,
2026-06-11,Thursday,Lena S.,Host,4:00pm,11:30pm,7.5,7.5,13000,
2026-06-12,Friday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Marcus T.,Server,4:30pm,12:00am,7.5,7.5,18000,
2026-06-12,Friday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Priya K.,Server,4:30pm,11:30pm,7.0,7.0,18000,
2026-06-12,Friday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Nina W.,Server,4:30pm,12:00am,7.5,7.5,18000,
2026-06-12,Friday,Marco D.,Server,5:00pm,12:00am,7.0,7.0,18000,
2026-06-12,Friday,Gina F.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Tony A.,Busser,4:30pm,12:00am,7.5,7.5,18000,
2026-06-12,Friday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-12,Friday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,18000,
2026-06-12,Friday,Tomas H.,Bartender,4:30pm,12:00am,7.5,7.5,18000,
2026-06-12,Friday,Kim T.,Bartender,5:30pm,12:00am,6.5,6.5,18000,
2026-06-12,Friday,Carlos B.,Cook,10:00am,8:00pm,10.0,10.0,18000,
2026-06-12,Friday,Amy C.,Cook,12:00pm,10:00pm,10.0,10.0,18000,
2026-06-12,Friday,Raj P.,Cook,10:30am,8:30pm,10.0,10.0,18000,
2026-06-12,Friday,Leo K.,Cook,3:00pm,12:00am,9.0,9.0,18000,
2026-06-12,Friday,James H.,Host,11:00am,6:00pm,7.0,7.0,18000,
2026-06-12,Friday,Lena S.,Host,4:30pm,12:00am,7.5,7.5,18000,
2026-06-13,Saturday,Sofia R.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Marcus T.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Priya K.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Elena V.,Server,11:00am,5:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Nina W.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Marco D.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Gina F.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Tony A.,Busser,11:00am,6:00pm,7.0,7.0,20000,
2026-06-13,Saturday,Sam V.,Busser,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Tomas H.,Bartender,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Kim T.,Bartender,5:00pm,12:00am,7.0,7.0,20000,
2026-06-13,Saturday,Carlos B.,Cook,9:00am,7:00pm,10.0,10.0,20000,
2026-06-13,Saturday,Amy C.,Cook,11:00am,9:00pm,10.0,10.0,20000,
2026-06-13,Saturday,Raj P.,Cook,9:30am,7:30pm,10.0,10.0,20000,
2026-06-13,Saturday,Leo K.,Cook,2:00pm,12:00am,10.0,10.0,20000,
2026-06-13,Saturday,James H.,Host,11:00am,7:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Lena S.,Host,4:30pm,12:00am,7.5,7.5,20000,
2026-06-14,Sunday,Sofia R.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Priya K.,Server,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Elena V.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Marco D.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Tomas H.,Bartender,4:00pm,11:00pm,7.0,7.0,12250,
2026-06-14,Sunday,Kim T.,Bartender,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Carlos B.,Cook,9:00am,6:30pm,9.5,9.5,12250,
2026-06-14,Sunday,Amy C.,Cook,11:00am,8:00pm,9.0,9.0,12250,
2026-06-14,Sunday,Raj P.,Cook,10:00am,7:00pm,9.0,9.0,12250,
2026-06-14,Sunday,Leo K.,Cook,2:00pm,10:00pm,8.0,8.0,12250,
2026-06-14,Sunday,James H.,Host,11:00am,5:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Lena S.,Host,4:00pm,10:30pm,6.5,6.5,12250,"""

    # Use save_client_data so the correct DB path is always used
    save_client_data(2, "shifts", gia_mia_csv, source="seed")
    print("[auto-seed] Gia Mia data seeded successfully")


# ── Restaurant CRUD ───────────────────────────────────────────────────────────

def create_restaurant(r: Restaurant, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO restaurants (name, owner_email, google_place_id, yelp_business_id,
            voice_notes, neighborhood, vibe, known_for, sign_off_name, never_say,
            hourly_rate, labor_target_pct, stripe_customer_id,
            location_group, location_name, pos_system, reviews_live, billing_status,
            service_tier, module_reviews, module_labor, module_inventory, module_marketing,
            owner_name, owner_phone, digest_day, digest_enabled, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (r.name, r.owner_email, r.google_place_id, r.yelp_business_id,
          r.voice_notes, r.neighborhood, r.vibe, r.known_for,
          r.sign_off_name, r.never_say, r.hourly_rate, r.labor_target_pct,
          r.stripe_customer_id, r.location_group, r.location_name, r.pos_system, r.reviews_live, r.billing_status,
          r.service_tier,
          r.module_reviews, r.module_labor, r.module_inventory,
          r.module_marketing, r.owner_name, r.owner_phone,
          r.digest_day, r.digest_enabled, r.created_at))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_restaurant(restaurant_id: int, fields: dict, db_path: str = DB_PATH):
    """Update any restaurant fields by dict."""
    allowed = {
        "name","owner_email","google_place_id","yelp_business_id","voice_notes",
        "neighborhood","vibe","known_for","sign_off_name","never_say",
        "hourly_rate","labor_target_pct","monthly_revenue_target","hours_notes","role_rates_json","stripe_customer_id","docusign_envelope_id","contract_status","location_group","location_name","pos_system","inventory_frequency","inventory_notes","food_cost_target","inventory_updated_at","temp_password","ig_token","ig_user_id","fb_page_token","fb_page_id","ig_token_expires","fb_token_expires","competitor_intel","competitor_updated_at","reviews_live","billing_status","internal_notes","gmb_access_token","gmb_refresh_token","gmb_account_id","gmb_location_id","gmb_token_expires",
        "service_tier","module_reviews","module_labor","module_inventory","module_marketing",
        "last_active_tab","last_activity","owner_name","owner_phone","digest_day","digest_enabled","menu_notes","menu_url","skip_holidays","custom_competitors",
        "two_fa_enabled","two_fa_code","two_fa_expires","two_fa_device_token","login_notify",
        "toast_client_id","toast_client_secret","toast_restaurant_guid",
        "toast_access_token","toast_token_expires","toast_last_synced","toast_sync_error",
        "gbp_rating","gbp_review_count"
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
        stripe_customer_id=row["stripe_customer_id"] if "stripe_customer_id" in row.keys() else None,
        docusign_envelope_id=row["docusign_envelope_id"] if "docusign_envelope_id" in row.keys() else None,
        contract_status=row["contract_status"] if "contract_status" in row.keys() else "pending",
        location_group=row["location_group"] if "location_group" in row.keys() else None,
        location_name=row["location_name"] if "location_name" in row.keys() else None,
        inventory_frequency=row["inventory_frequency"] if "inventory_frequency" in row.keys() else "weekly",
        inventory_notes=row["inventory_notes"] if "inventory_notes" in row.keys() else None,
        food_cost_target=row["food_cost_target"] if "food_cost_target" in row.keys() else 30.0,
        monthly_revenue_target=float(row["monthly_revenue_target"]) if "monthly_revenue_target" in row.keys() and row["monthly_revenue_target"] else 0.0,
        hours_notes=row["hours_notes"] if "hours_notes" in row.keys() else None,
        role_rates_json=row["role_rates_json"] if "role_rates_json" in row.keys() else None,
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
        last_active_tab=row["last_active_tab"] if "last_active_tab" in row.keys() else None,
        menu_notes=row["menu_notes"] if "menu_notes" in row.keys() else None,
        menu_url=row["menu_url"] if "menu_url" in row.keys() else None,
        skip_holidays=row["skip_holidays"] if "skip_holidays" in row.keys() else None,
        custom_competitors=row["custom_competitors"] if "custom_competitors" in row.keys() else None,
        login_notify=row["login_notify"] if "login_notify" in row.keys() else 0,
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
        last_activity=row["last_activity"] if "last_activity" in row.keys() else None,
        gbp_rating=row["gbp_rating"] if "gbp_rating" in row.keys() else None,
        gbp_review_count=row["gbp_review_count"] if "gbp_review_count" in row.keys() else None,
        owner_name=row["owner_name"] if "owner_name" in row.keys() else None,
        owner_phone=row["owner_phone"] if "owner_phone" in row.keys() else None,
        digest_day=row["digest_day"] if "digest_day" in row.keys() else "monday",
        digest_enabled=row["digest_enabled"] if "digest_enabled" in row.keys() else 1,
        last_fetched_at=row["last_fetched_at"] if "last_fetched_at" in row.keys() else None,
    )


# ── Review CRUD ───────────────────────────────────────────────────────────────

def save_reviews(reviews: list[Review], db_path: str = DB_PATH) -> int:
    """Upsert reviews; skip duplicates. Returns count of newly inserted rows."""
    conn = get_conn(db_path)
    new_count = 0
    for r in reviews:
        try:
            conn.execute("""
                INSERT INTO reviews
                    (restaurant_id, platform, external_id, author, rating,
                     text, review_date, fetched_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, (r.restaurant_id, r.platform, r.external_id, r.author,
                  r.rating, r.text, r.review_date, r.fetched_at))
            new_count += 1
        except sqlite3.IntegrityError:
            pass  # UNIQUE(platform, external_id) — already stored
    conn.commit()
    conn.close()
    return new_count


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


def update_draft(review_id: int, draft: str, db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE reviews SET draft_response=?, response_status='drafted' WHERE id=?
    """, (draft, review_id))
    conn.commit()
    conn.close()


def approve_response(review_id: int, db_path: str = DB_PATH):
    conn = get_conn(db_path)
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
"""

def init_email_log(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS email_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER,
        email_type TEXT,
        to_email TEXT,
        subject TEXT,
        sent_at TEXT DEFAULT (datetime('now')),
        status TEXT DEFAULT 'sent'
    )""")
    conn.commit()
    conn.close()

def log_email(restaurant_id, email_type, to_email, subject, db_path: str = DB_PATH):
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
        "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at) VALUES (?,?,?,?,?)",
        (restaurant_id, email_type, to_email, subject, local_now)
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
    """Return recent labor snapshots for trend awareness."""
    conn = get_conn(db_path)
    rows = conn.execute("""
        SELECT period_start, period_end, labor_pct, total_labor, total_sales
        FROM labor_history WHERE restaurant_id=?
        ORDER BY period_start DESC LIMIT ?
    """, (restaurant_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def save_labor_daily_history(restaurant_id: int, by_day: dict, hourly_rate: float,
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
        labor_cost = round(actual_hours * hourly_rate, 2)
        labor_pct = round(labor_cost / sales * 100, 1) if sales else 0
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


def get_location_group(group_name: str, db_path: str = DB_PATH) -> list:
    """Get all restaurants in a location group."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM restaurants WHERE location_group=? ORDER BY location_name",
        (group_name,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_location_groups(db_path: str = DB_PATH) -> list:
    """Get distinct location group names."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT DISTINCT location_group FROM restaurants WHERE location_group IS NOT NULL ORDER BY location_group"
    ).fetchall()
    conn.close()
    return [r["location_group"] for r in rows]

def get_review_stats(restaurant_id):
    conn = get_conn()
    # Single query for sentiment/status counts
    rows = conn.execute("""
        SELECT
            COUNT(*)                                                                    AS total,
            SUM(sentiment='positive')                                                   AS positive,
            SUM(sentiment='negative')                                                   AS negative,
            SUM(sentiment='neutral')                                                    AS neutral,
            AVG(rating)                                                                 AS avg_rating,
            SUM(response_status='drafted')                                              AS drafted,
            SUM(urgency='high' AND response_status NOT IN ('posted','approved','skipped')) AS urgent,
            SUM(response_status='posted')                                               AS posted,
            SUM(response_status IN ('posted','approved'))                               AS responded,
            SUM(response_status='skipped')                                              AS skipped,
            SUM(response_status IN ('posted','approved') AND approved_at >= date('now','start of month')) AS responded_this_month,
            SUM(review_date >= date('now','start of month'))                            AS received_this_month,
            SUM(review_date >= date('now','-30 days'))                                  AS last_30d,
            AVG(CASE WHEN review_date >= date('now','-30 days') THEN rating END)        AS avg_rating_30d
        FROM reviews WHERE processed=1 AND restaurant_id=?
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
    """, (restaurant_id,)).fetchone()

    conn.close()
    total     = rows["total"]     or 0
    posted    = rows["posted"]    or 0
    responded = rows["responded"] or 0
    drafted   = rows["drafted"]   or 0
    skipped   = rows["skipped"]   or 0
    this_month = rows["responded_this_month"] or 0
    last_30d  = rows["last_30d"]  or 0
    # Response rate = approved+posted / total
    response_rate = round((responded / total * 100) if total > 0 else 0, 1)
    avg_rating_30d = round(rows["avg_rating_30d"] or 0, 1)
    # Avg response time — cap at reasonable max for display
    avg_hrs_raw = rt_row["avg_hrs"] if rt_row and rt_row["avg_hrs"] else None
    if avg_hrs_raw and avg_hrs_raw > 0:
        avg_response_hours = round(avg_hrs_raw, 1)
    else:
        avg_response_hours = None

    positive = rows["positive"] or 0
    positive_pct = round(positive / total * 100) if total > 0 else 0

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
        posted            = posted,
        responded         = responded,
        skipped           = skipped,
        this_month        = this_month,              # responded this calendar month
        received_this_month = rows["received_this_month"] or 0,  # reviews received this month
        last_30d          = last_30d,
        response_rate     = response_rate,
        avg_response_hours= avg_response_hours,
    )

def get_sentiment_trend(restaurant_id, weeks=8):
    """Return weekly positive/negative counts for the last N weeks."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            strftime('%Y-%W', fetched_at)          AS week_key,
            MIN(DATE(fetched_at))                  AS week_start,
            SUM(sentiment='positive')              AS positive,
            SUM(sentiment='negative')              AS negative,
            SUM(sentiment='neutral')               AS neutral,
            COUNT(*)                               AS total,
            ROUND(AVG(rating),1)                   AS avg_rating
        FROM reviews
        WHERE restaurant_id=? AND processed=1
          AND fetched_at >= datetime('now', ? || ' days')
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

def get_platform_breakdown(restaurant_id):
    """Return review count and avg rating per platform."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT platform,
               COUNT(*)    AS total,
               AVG(rating) AS avg_rating,
               SUM(sentiment='positive')  AS positive,
               SUM(sentiment='negative')  AS negative
        FROM reviews
        WHERE restaurant_id=? AND processed=1
        GROUP BY platform
        ORDER BY total DESC
    """, (restaurant_id,)).fetchall()
    conn.close()
    result = []
    for row in rows:
        if row["platform"] in ("csv","manual"):
            continue  # skip non-public platforms
        result.append({
            "platform":   row["platform"],
            "total":      row["total"],
            "avg_rating": round(row["avg_rating"] or 0, 1),
            "positive":   row["positive"] or 0,
            "negative":   row["negative"] or 0,
        })
    return result

def get_top_issues(restaurant_id, days=90, limit=6):
    """Return top review categories by mention count for the last N days."""
    from collections import Counter
    conn = get_conn()
    rows = conn.execute("""
        SELECT categories FROM reviews
        WHERE restaurant_id=? AND processed=1
        AND categories IS NOT NULL AND categories != '[]'
        AND fetched_at >= datetime('now', '-' || ? || ' days')
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

def get_reviews_data(restaurant_id, filter_by="all", search=""):
    conn = get_conn()
    where  = ["processed=1", "restaurant_id=?"]
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
