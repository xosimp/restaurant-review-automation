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
    -- Tech info
    pos_system      TEXT,          -- Toast / Square / Lightspeed / etc
    owner_phone     TEXT,              -- owner phone number
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
    pos_system: Optional[str]       = None
    owner_phone: Optional[str]      = None
    reviews_live: int               = 0
    billing_status: str             = "trial"
    internal_notes: Optional[str]   = None
    service_tier: str               = "trial"   # trial / starter_reviews / starter_labor / starter_inventory / starter_marketing / full
    module_reviews: int             = 1
    module_labor: int               = 1
    module_inventory: int           = 1
    module_marketing: int           = 1
    last_active_tab: Optional[str]  = None
    last_activity: Optional[str]    = None
    id: Optional[int]               = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Review:
    restaurant_id: int
    platform: str
    external_id: str
    author: str
    rating: int
    text: str
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
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
        "ALTER TABLE restaurants ADD COLUMN owner_phone TEXT",
        "ALTER TABLE restaurants ADD COLUMN reviews_live INTEGER DEFAULT 0",
        "ALTER TABLE restaurants ADD COLUMN billing_status TEXT DEFAULT 'trial'",
        "ALTER TABLE restaurants ADD COLUMN internal_notes TEXT",
        "ALTER TABLE restaurants ADD COLUMN service_tier TEXT DEFAULT 'trial'",
        "ALTER TABLE restaurants ADD COLUMN module_reviews INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN module_labor INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN module_inventory INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN module_marketing INTEGER DEFAULT 1",
        "ALTER TABLE restaurants ADD COLUMN last_active_tab TEXT",
        "ALTER TABLE restaurants ADD COLUMN last_activity TEXT",
        "ALTER TABLE client_data ADD COLUMN shifts_csv TEXT",
        "ALTER TABLE client_data ADD COLUMN inventory_csv TEXT",
    ]
    for m in migrations:
        try:
            conn.execute(m)
        except Exception:
            pass  # column already exists
    conn.commit()
    conn.close()
    print(f"Database initialised at {db_path}")


# ── Restaurant CRUD ───────────────────────────────────────────────────────────

def create_restaurant(r: Restaurant, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO restaurants (name, owner_email, google_place_id, yelp_business_id,
            voice_notes, neighborhood, vibe, known_for, sign_off_name, never_say,
            hourly_rate, pos_system, reviews_live, billing_status,
            service_tier, module_reviews, module_labor, module_inventory, module_marketing,
            owner_phone, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (r.name, r.owner_email, r.google_place_id, r.yelp_business_id,
          r.voice_notes, r.neighborhood, r.vibe, r.known_for,
          r.sign_off_name, r.never_say, r.hourly_rate,
          r.pos_system, r.reviews_live, r.billing_status,
          r.service_tier,
          r.module_reviews, r.module_labor, r.module_inventory,
          r.module_marketing, r.owner_phone, r.created_at))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_restaurant(restaurant_id: int, fields: dict, db_path: str = DB_PATH):
    """Update any restaurant fields by dict."""
    allowed = {
        "name","owner_email","google_place_id","yelp_business_id","voice_notes",
        "neighborhood","vibe","known_for","sign_off_name","never_say",
        "hourly_rate","pos_system","reviews_live","billing_status","internal_notes",
        "service_tier","module_reviews","module_labor","module_inventory","module_marketing",
        "last_active_tab","last_activity","owner_phone"
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
        pos_system=row["pos_system"] if "pos_system" in row.keys() else None,
        reviews_live=row["reviews_live"] if "reviews_live" in row.keys() else 0,
        billing_status=row["billing_status"] if "billing_status" in row.keys() else "trial",
        internal_notes=row["internal_notes"] if "internal_notes" in row.keys() else None,
        service_tier=row["service_tier"] if "service_tier" in row.keys() else "trial",
        module_reviews=row["module_reviews"] if "module_reviews" in row.keys() else 1,
        module_labor=row["module_labor"] if "module_labor" in row.keys() else 1,
        module_inventory=row["module_inventory"] if "module_inventory" in row.keys() else 1,
        module_marketing=row["module_marketing"] if "module_marketing" in row.keys() else 1,
        last_active_tab=row["last_active_tab"] if "last_active_tab" in row.keys() else None,
        last_activity=row["last_activity"] if "last_activity" in row.keys() else None,
        owner_phone=row["owner_phone"] if "owner_phone" in row.keys() else None,
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


def log_activity(restaurant_id: int, tab: str,
                 db_path: str = DB_PATH):
    """Record last active tab and timestamp."""
    from datetime import datetime, timezone
    conn = get_conn(db_path)
    conn.execute("""
        UPDATE restaurants SET last_active_tab=?, last_activity=? WHERE id=?
    """, (tab, datetime.now(timezone.utc).isoformat(), restaurant_id))
    conn.commit()
    conn.close()


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
