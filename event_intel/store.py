"""
event_intel.store — the Event Intelligence catalog's tables and their writes.

Three tables, created at boot (models.init_db → init_event_intel):

  event_series     one thing that recurs: a team, a festival, a venue's
                   season, a holiday calendar. Where it is (lat/lng and the
                   radius within which a restaurant feels it), its category,
                   league and timezone.
  catalog_events   one dated occurrence of a series: date (NULL while the
                   date is TBD), local kickoff/start, home/away, opponent,
                   venue, broadcast, prime time, season type, status and
                   result. Global — no restaurant column: one Bears game is
                   one row however many restaurants feel it.
  event_follows    which restaurant feels which series: auto (within the
                   series' radius), owner or admin; `active` 0 is an
                   explicit opt-out that auto-follow never overrides.

A restaurant's own copy of each game it follows is a demand_signals row
(source "events", ref "event:<id>") written by event_intel.engine.sync —
the table every forecast, schedule, report and prediction already reads.
"""
import json
import os
from datetime import datetime

import models as _models_mod
from models import DB_PATH

SEASONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seasons")
CATEGORIES = ("sports", "holiday", "concert", "festival", "local", "venue", "school", "civic")
SEASON_TYPES = ("preseason", "regular", "postseason", "special")
STATUSES = ("scheduled", "completed", "postponed", "cancelled")
FOLLOW_SOURCES = ("auto", "owner", "admin")
PRIMETIME_FROM = "18:30"          # a local start at or after this is prime time


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_event_intel(db_path=DB_PATH):
    """The three tables, then the bundled seasons (idempotent upserts)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS event_series (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            slug          TEXT    NOT NULL UNIQUE,
            name          TEXT    NOT NULL,
            short_name    TEXT    NOT NULL,
            category      TEXT    NOT NULL,
            league        TEXT,
            metro         TEXT,
            home_venue    TEXT,
            timezone      TEXT,
            lat           REAL,
            lng           REAL,
            radius_km     REAL,
            source_url    TEXT,
            meta_json     TEXT,
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS catalog_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id     INTEGER NOT NULL REFERENCES event_series(id),
            external_id   TEXT    NOT NULL,
            season        INTEGER,
            season_type   TEXT    NOT NULL DEFAULT 'regular',
            week          TEXT,
            event_date    TEXT,
            kickoff_local TEXT,
            timezone      TEXT,
            home_away     TEXT,
            opponent      TEXT,
            venue         TEXT,
            broadcast     TEXT,
            is_primetime  INTEGER NOT NULL DEFAULT 0,
            status        TEXT    NOT NULL DEFAULT 'scheduled',
            result        TEXT,
            attributes_json TEXT,
            source_url    TEXT,
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(series_id, external_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_events_date ON catalog_events(event_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS event_follows (
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            series_id     INTEGER NOT NULL REFERENCES event_series(id),
            active        INTEGER NOT NULL DEFAULT 1,
            source        TEXT    NOT NULL DEFAULT 'auto',
            distance_km   REAL,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, series_id)
        )""")
        conn.commit()
    finally:
        conn.close()
    load_bundled(db_path=db_path)


def is_primetime(kickoff_local) -> bool:
    k = str(kickoff_local or "")
    return bool(k) and k[:5] >= PRIMETIME_FROM


def upsert_series(s, db_path=DB_PATH) -> int:
    """Insert or update a series by slug; returns its id."""
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO event_series (slug, name, short_name, category, league, metro, home_venue, timezone, lat, "
            "lng, radius_km, source_url, meta_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(slug) DO UPDATE SET name=excluded.name, short_name=excluded.short_name, "
            "category=excluded.category, league=excluded.league, metro=excluded.metro, "
            "home_venue=excluded.home_venue, timezone=excluded.timezone, lat=excluded.lat, lng=excluded.lng, "
            "radius_km=excluded.radius_km, source_url=excluded.source_url, meta_json=excluded.meta_json, "
            "updated_at=datetime('now')",
            (s["slug"], s["name"], s.get("short_name") or s["name"], s.get("category") or "local", s.get("league"),
             s.get("metro"), s.get("home_venue"), s.get("timezone"), s.get("lat"), s.get("lng"), s.get("radius_km"),
             s.get("source_url"), json.dumps(s.get("meta") or {})))
        sid = conn.execute("SELECT id FROM event_series WHERE slug=?", (s["slug"],)).fetchone()["id"]
        conn.commit()
        return sid
    finally:
        conn.close()


def upsert_events(series_id, events, season=None, timezone=None, source_url=None, db_path=DB_PATH) -> dict:
    """Insert or update a series' events by external_id. A date the source
    moved replaces the old one (the next sync moves each restaurant's copy);
    nothing is ever deleted here — a cancelled game is a status."""
    written = changed = 0
    conn = get_conn(db_path)
    try:
        for e in events or []:
            row = conn.execute("SELECT event_date, kickoff_local, status, result FROM catalog_events "
                               "WHERE series_id=? AND external_id=?", (series_id, e["external_id"])).fetchone()
            vals = (series_id, e["external_id"], e.get("season", season), e.get("season_type") or "regular",
                    e.get("week"), e.get("date"), e.get("kickoff"), e.get("timezone") or timezone,
                    e.get("home_away"), e.get("opponent"), e.get("venue"), e.get("broadcast"),
                    1 if is_primetime(e.get("kickoff")) else 0, e.get("status") or "scheduled", e.get("result"),
                    json.dumps(e.get("attributes") or {}), e.get("source_url") or source_url)
            conn.execute(
                "INSERT INTO catalog_events (series_id, external_id, season, season_type, week, event_date, "
                "kickoff_local, timezone, home_away, opponent, venue, broadcast, is_primetime, status, result, "
                "attributes_json, source_url, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(series_id, external_id) DO UPDATE SET season=excluded.season, "
                "season_type=excluded.season_type, week=excluded.week, event_date=excluded.event_date, "
                "kickoff_local=excluded.kickoff_local, timezone=excluded.timezone, home_away=excluded.home_away, "
                "opponent=excluded.opponent, venue=excluded.venue, broadcast=excluded.broadcast, "
                "is_primetime=excluded.is_primetime, status=excluded.status, "
                "result=COALESCE(excluded.result, catalog_events.result), attributes_json=excluded.attributes_json, "
                "source_url=excluded.source_url, updated_at=datetime('now')", vals)
            written += 1
            if row and (row["event_date"], row["kickoff_local"]) != (e.get("date"), e.get("kickoff")):
                changed += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "moved": changed}


def load_season(path, db_path=DB_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    s = data["series"]
    sid = upsert_series(s, db_path=db_path)
    got = upsert_events(sid, data.get("events") or [], season=data.get("season"), timezone=s.get("timezone"),
                        source_url=s.get("source_url"), db_path=db_path)
    return dict(got, series_id=sid, slug=s["slug"])


def load_bundled(db_path=DB_PATH) -> list:
    """Every season file in event_intel/seasons, in name order."""
    out = []
    if not os.path.isdir(SEASONS_DIR):
        return out
    for fn in sorted(os.listdir(SEASONS_DIR)):
        if fn.endswith(".json"):
            out.append(load_season(os.path.join(SEASONS_DIR, fn), db_path=db_path))
    return out


def series_by_slug(slug, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM event_series WHERE slug=?", (slug,)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def all_series(db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM event_series ORDER BY name").fetchall()]
    finally:
        conn.close()


def events_for(series_ids, start=None, end=None, db_path=DB_PATH) -> list:
    """Dated events of these series between start and end (ISO, inclusive),
    each with its series' name, short name and category."""
    ids = [int(i) for i in series_ids or []]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    sql = (f"SELECT e.*, s.name AS series_name, s.short_name, s.category, s.slug, s.league FROM catalog_events e "
           f"JOIN event_series s ON s.id=e.series_id WHERE e.series_id IN ({marks}) AND e.event_date IS NOT NULL")
    args = list(ids)
    if start:
        sql += " AND e.event_date >= ?"
        args.append(str(start)[:10])
    if end:
        sql += " AND e.event_date <= ?"
        args.append(str(end)[:10])
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY e.event_date, e.kickoff_local", args).fetchall()]
    finally:
        conn.close()
    for r in rows:
        try:
            r["attributes"] = json.loads(r.pop("attributes_json") or "{}")
        except (TypeError, ValueError):
            r["attributes"] = {}
    return rows


def event_by_id(event_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT e.*, s.name AS series_name, s.short_name, s.category, s.slug, s.league "
                         "FROM catalog_events e JOIN event_series s ON s.id=e.series_id WHERE e.id=?",
                         (int(event_id),)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    out = dict(r)
    try:
        out["attributes"] = json.loads(out.pop("attributes_json") or "{}")
    except (TypeError, ValueError):
        out["attributes"] = {}
    return out


def follows(restaurant_id, active_only=True, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        sql = ("SELECT f.*, s.slug, s.name, s.short_name, s.category FROM event_follows f "
               "JOIN event_series s ON s.id=f.series_id WHERE f.restaurant_id=?")
        if active_only:
            sql += " AND f.active=1"
        return [dict(r) for r in conn.execute(sql + " ORDER BY s.name", (restaurant_id,)).fetchall()]
    finally:
        conn.close()


def set_follow(restaurant_id, series_id, active, source="owner", distance_km=None, db_path=DB_PATH):
    """An owner's or admin's choice. It always wins over auto-follow."""
    if source not in FOLLOW_SOURCES:
        raise ValueError(f"unknown follow source {source!r}")
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO event_follows (restaurant_id, series_id, active, source, distance_km, updated_at) "
            "VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, series_id) DO UPDATE SET "
            "active=excluded.active, source=excluded.source, "
            "distance_km=COALESCE(excluded.distance_km, event_follows.distance_km), updated_at=datetime('now')",
            (restaurant_id, int(series_id), 1 if active else 0, source, distance_km))
        conn.commit()
    finally:
        conn.close()


def auto_follow(restaurant_id, series_id, distance_km, db_path=DB_PATH) -> bool:
    """Follow a series because it is near — never over a row that exists
    (an owner who turned it off stays off). True when a row was added."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT OR IGNORE INTO event_follows (restaurant_id, series_id, active, source, distance_km) "
                           "VALUES (?,?,1,'auto',?)", (restaurant_id, int(series_id), distance_km))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def now_iso():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
