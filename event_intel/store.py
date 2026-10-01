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
        # A game the owner removed from their own list stays removed: the
        # daily sync never writes it back (audit 10/1/26).
        conn.execute("""CREATE TABLE IF NOT EXISTS event_dismissals (
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            event_id      INTEGER NOT NULL REFERENCES catalog_events(id),
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, event_id)
        )""")
        # An admin's correction to a game (a kickoff flexed, Week 18's date
        # set) is kept apart from the season file and laid over it on every
        # load, so the daily reload never undoes it (phase 2, 10/1/26).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(catalog_events)").fetchall()}
        if "overrides_json" not in cols:
            conn.execute("ALTER TABLE catalog_events ADD COLUMN overrides_json TEXT")
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
                "is_primetime=excluded.is_primetime, "
                # a season file says "scheduled" for a game since played:
                # a completed (or cancelled) game never goes back
                "status=CASE WHEN excluded.status='scheduled' AND catalog_events.status<>'scheduled' "
                "THEN catalog_events.status ELSE excluded.status END, "
                "result=COALESCE(excluded.result, catalog_events.result), attributes_json=excluded.attributes_json, "
                "source_url=excluded.source_url, updated_at=datetime('now')", vals)
            _apply_overrides(conn, series_id, e["external_id"])
            written += 1
            if row and (row["event_date"], row["kickoff_local"]) != (e.get("date"), e.get("kickoff")):
                changed += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "moved": changed}


# What an admin may correct on a game, and how each value is checked.
EDITABLE = ("event_date", "kickoff_local", "broadcast", "status", "result")


def _clean_edit(field, value):
    """A corrected value as stored, or raises ValueError naming the field."""
    import re
    if value in (None, ""):
        if field in ("status",):
            raise ValueError("A game always has a status.")
        return None
    v = str(value).strip()
    if field == "event_date":
        from datetime import date as _date
        try:
            return _date.fromisoformat(v[:10]).isoformat()
        except ValueError:
            raise ValueError("The date must be YYYY-MM-DD.")
    if field == "kickoff_local":
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("The kickoff must be a 24-hour local time, like 19:15.")
        return v
    if field == "status":
        if v not in STATUSES:
            raise ValueError(f"The status must be one of {', '.join(STATUSES)}.")
        return v
    return v[:80]


def _apply_overrides(conn, series_id, external_id):
    """Lay an admin's corrections over the season file's row."""
    row = conn.execute("SELECT id, overrides_json FROM catalog_events WHERE series_id=? AND external_id=?",
                       (series_id, external_id)).fetchone()
    if not row or not row["overrides_json"]:
        return
    try:
        over = {k: v for k, v in json.loads(row["overrides_json"]).items() if k in EDITABLE}
    except (TypeError, ValueError):
        return
    if not over:
        return
    sets = ", ".join(f"{k}=?" for k in over)
    args = list(over.values())
    if "kickoff_local" in over:
        sets += ", is_primetime=?"
        args.append(1 if is_primetime(over["kickoff_local"]) else 0)
    conn.execute(f"UPDATE catalog_events SET {sets} WHERE id=?", args + [row["id"]])


def edit_event(event_id, changes, clear=(), db_path=DB_PATH) -> dict:
    """An admin's correction to one game: `changes` {field: value} over
    EDITABLE, `clear` the fields to hand back to the season file (they
    take its value on the next load). Returns {"before", "after"}; raises
    ValueError on a bad value or LookupError when there is no such game."""
    bad = [k for k in list(changes or {}) + list(clear or ()) if k not in EDITABLE]
    if bad:
        raise ValueError(f"Only {', '.join(EDITABLE)} can be corrected.")
    clean = {k: _clean_edit(k, v) for k, v in (changes or {}).items()}
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM catalog_events WHERE id=?", (int(event_id),)).fetchone()
        if not row:
            raise LookupError("No such game in the catalog.")
        before = {k: row[k] for k in EDITABLE}
        try:
            over = json.loads(row["overrides_json"] or "{}")
        except (TypeError, ValueError):
            over = {}
        over.update(clean)
        for k in clear or ():
            over.pop(k, None)
        conn.execute("UPDATE catalog_events SET overrides_json=?, updated_at=datetime('now') WHERE id=?",
                     (json.dumps(over) if over else None, row["id"]))
        _apply_overrides(conn, row["series_id"], row["external_id"])
        conn.commit()
        after_row = conn.execute("SELECT * FROM catalog_events WHERE id=?", (row["id"],)).fetchone()
        return {"before": before, "after": {k: after_row[k] for k in EDITABLE}, "overrides": over,
                "series_id": row["series_id"]}
    finally:
        conn.close()


def catalog(db_path=DB_PATH) -> list:
    """Every series with its games (dated or not) and how many restaurants
    follow it — the admin catalog editor's read."""
    conn = get_conn(db_path)
    try:
        series = [dict(r) for r in conn.execute("SELECT * FROM event_series ORDER BY name").fetchall()]
        for s in series:
            s["followers"] = conn.execute("SELECT COUNT(*) AS n FROM event_follows WHERE series_id=? AND active=1",
                                          (s["id"],)).fetchone()["n"]
            s["events"] = []
            for r in conn.execute("SELECT * FROM catalog_events WHERE series_id=? ORDER BY event_date IS NULL, "
                                  "event_date, kickoff_local, external_id", (s["id"],)).fetchall():
                ev = dict(r)
                try:
                    ev["overrides"] = json.loads(ev.pop("overrides_json") or "{}")
                except (TypeError, ValueError):
                    ev["overrides"] = {}
                ev.pop("attributes_json", None)
                s["events"].append(ev)
            s.pop("meta_json", None)
        return series
    finally:
        conn.close()


def followers(series_id, db_path=DB_PATH) -> list:
    """Restaurant ids that follow a series."""
    conn = get_conn(db_path)
    try:
        return [r["restaurant_id"] for r in conn.execute(
            "SELECT restaurant_id FROM event_follows WHERE series_id=? AND active=1", (int(series_id),)).fetchall()]
    finally:
        conn.close()


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


def mark_past_completed(today, db_path=DB_PATH) -> int:
    """A scheduled event whose date has passed is completed (its result,
    when a source gives one, is kept or added later)."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE catalog_events SET status='completed', updated_at=datetime('now') "
                           "WHERE status='scheduled' AND event_date IS NOT NULL AND event_date < ?", (str(today)[:10],))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def dismiss(restaurant_id, event_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO event_dismissals (restaurant_id, event_id) VALUES (?,?)",
                     (restaurant_id, int(event_id)))
        conn.commit()
    finally:
        conn.close()


def dismissed(restaurant_id, db_path=DB_PATH) -> set:
    conn = get_conn(db_path)
    try:
        return {r["event_id"] for r in conn.execute("SELECT event_id FROM event_dismissals WHERE restaurant_id=?",
                                                    (restaurant_id,)).fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()
