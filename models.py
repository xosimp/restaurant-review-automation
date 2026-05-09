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
    voice_notes     TEXT,          -- owner's brand-voice guidance for Claude
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
"""


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class Restaurant:
    name: str
    owner_email: str
    google_place_id: Optional[str] = None
    yelp_business_id: Optional[str] = None
    voice_notes: Optional[str] = None
    id: Optional[int] = None
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
    conn.commit()
    conn.close()
    print(f"Database initialised at {db_path}")


# ── Restaurant CRUD ───────────────────────────────────────────────────────────

def create_restaurant(r: Restaurant, db_path: str = DB_PATH) -> int:
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO restaurants (name, owner_email, google_place_id, yelp_business_id, voice_notes, created_at)
        VALUES (?,?,?,?,?,?)
    """, (r.name, r.owner_email, r.google_place_id, r.yelp_business_id, r.voice_notes, r.created_at))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


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
