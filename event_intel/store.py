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
        # events_for's per-row season count (phase 4 audit).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_catalog_events_series_season "
                     "ON catalog_events(series_id, season, season_type)")
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
        # Who removed a game, and whether it was the owner or an admin in
        # view-as (event re-audit X-4): the events card says it beside Put back.
        dcols = {r["name"] for r in conn.execute("PRAGMA table_info(event_dismissals)").fetchall()}
        if "dismissed_by" not in dcols:
            conn.execute("ALTER TABLE event_dismissals ADD COLUMN dismissed_by TEXT")
        if "source" not in dcols:
            conn.execute("ALTER TABLE event_dismissals ADD COLUMN source TEXT")
        conn.commit()
    finally:
        conn.close()
    load_bundled(db_path=db_path)


# Prime time is a football idea: an NBA, NHL or MLS night game is every game
# (phase 4 audit) and would split each segment for nothing.
PRIMETIME_LEAGUES = ("NFL",)


def is_primetime(kickoff_local, league="NFL") -> bool:
    k = str(kickoff_local or "")
    if league and str(league).upper() not in PRIMETIME_LEAGUES:
        return False
    return bool(k) and k[:5] >= PRIMETIME_FROM


def _league_of(conn, series_id):
    try:
        r = conn.execute("SELECT league FROM event_series WHERE id=?", (series_id,)).fetchone()
        return r["league"] if r else None
    except Exception:
        return None


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
    _c = get_conn(db_path)
    try:
        league = _league_of(_c, series_id)
    finally:
        _c.close()
    conn = get_conn(db_path)
    try:
        for e in events or []:
            row = conn.execute("SELECT event_date, kickoff_local, status, result FROM catalog_events "
                               "WHERE series_id=? AND external_id=?", (series_id, e["external_id"])).fetchone()
            vals = (series_id, e["external_id"], e.get("season", season), e.get("season_type") or "regular",
                    e.get("week"), e.get("date"), e.get("kickoff"), e.get("timezone") or timezone,
                    e.get("home_away"), e.get("opponent"), e.get("venue"), e.get("broadcast"),
                    1 if is_primetime(e.get("kickoff"), league) else 0, e.get("status") or "scheduled", e.get("result"),
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
                # a completed game never goes back. A postponed or cancelled
                # one the file reinstates does (re-audit P1-03: a rescheduled
                # game stayed postponed for good, copied and measured nowhere);
                # an admin's own status is laid back over it below.
                "status=CASE WHEN excluded.status='scheduled' AND catalog_events.status='completed' "
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
        args.append(1 if is_primetime(over["kickoff_local"], _league_of(conn, series_id)) else 0)
    conn.execute(f"UPDATE catalog_events SET {sets} WHERE id=?", args + [row["id"]])


_FILE_KEY = {"event_date": "date", "kickoff_local": "kickoff", "broadcast": "broadcast", "status": "status",
             "result": "result"}


def _season_values(slug, external_id) -> dict | None:
    """The season file's own values for one game ({EDITABLE field: value}),
    or None when no bundled file carries it."""
    if not os.path.isdir(SEASONS_DIR):
        return None
    for fn in sorted(os.listdir(SEASONS_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SEASONS_DIR, fn), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if (data.get("series") or {}).get("slug") != slug:
            continue
        for e in data.get("events") or []:
            if e.get("external_id") == external_id:
                return {f: (e.get(k) if f != "status" else (e.get(k) or "scheduled")) for f, k in _FILE_KEY.items()}
    return None


def edit_event(event_id, changes, clear=(), db_path=DB_PATH) -> dict:
    """An admin's correction to one game: `changes` {field: value} over
    EDITABLE, `clear` the fields to hand back to the season file (they
    take its value now; the caller re-syncs the followers). Returns {"before", "after"}; raises
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
        # A cleared field takes the season file's value NOW: the daily load
        # never moves a status back to scheduled nor a result back to empty,
        # so a cleared "cancelled" used to stand for good (audit 10/1/26).
        cleared = [k for k in (clear or ()) if k not in clean]
        if cleared:
            s = conn.execute("SELECT slug, timezone FROM event_series WHERE id=?", (row["series_id"],)).fetchone()
            file_vals = _season_values(s["slug"] if s else None, row["external_id"]) or {}
            sets = {k: file_vals.get(k) for k in cleared if k in file_vals}
            # "Past" on the game's own clock, never the server's UTC date: an
            # admin clearing tonight's postponed status after 7pm Central
            # got "completed" for a game still being played (re-audit X-6).
            today = local_today(row["timezone"] or (s["timezone"] if s else None)).isoformat()
            if "status" in sets and sets["status"] == "scheduled" and row["event_date"] and \
                    (file_vals.get("event_date") or row["event_date"]) < today:
                sets["status"] = "completed"       # a past game the file still calls scheduled was played
            if sets:
                cols = ", ".join(f"{k}=?" for k in sets)
                args = list(sets.values())
                if "kickoff_local" in sets:
                    cols += ", is_primetime=?"
                    args.append(1 if is_primetime(sets["kickoff_local"], _league_of(conn, row["series_id"])) else 0)
                conn.execute(f"UPDATE catalog_events SET {cols} WHERE id=?", args + [row["id"]])
        _apply_overrides(conn, row["series_id"], row["external_id"])
        conn.commit()
        after_row = conn.execute("SELECT * FROM catalog_events WHERE id=?", (row["id"],)).fetchone()
        return {"before": before, "after": {k: after_row[k] for k in EDITABLE}, "overrides": over,
                "series_id": row["series_id"]}
    finally:
        conn.close()


def catalog(db_path=DB_PATH) -> list:
    """Every series with its games (dated or not) and how many restaurants
    follow it — the admin catalog editor's read. Each game carries
    `needs_result` (an if-necessary game whose date has passed with no
    result and no cancellation — `unresolved`: its night is held out of
    every baseline, unmeasured, until a result or a cancellation is entered)
    and each series `needs_result`, how many of its games do."""
    conn = get_conn(db_path)
    try:
        series = [dict(r) for r in conn.execute("SELECT * FROM event_series ORDER BY name").fetchall()]
        for s in series:
            s["followers"] = conn.execute("SELECT COUNT(*) AS n FROM event_follows WHERE series_id=? AND active=1",
                                          (s["id"],)).fetchone()["n"]
            s["events"] = []
            today = local_today(s.get("timezone")).isoformat()
            for r in conn.execute("SELECT * FROM catalog_events WHERE series_id=? ORDER BY event_date IS NULL, "
                                  "event_date, kickoff_local, external_id", (s["id"],)).fetchall():
                ev = dict(r)
                try:
                    ev["overrides"] = json.loads(ev.pop("overrides_json") or "{}")
                except (TypeError, ValueError):
                    ev["overrides"] = {}
                ev["attributes"] = attributes_of(ev)
                ev.pop("attributes_json", None)
                ev["needs_result"] = unresolved(
                    ev, today=local_today(ev["timezone"]).isoformat() if ev.get("timezone") else today)
                s["events"].append(ev)
            s["needs_result"] = sum(1 for ev in s["events"] if ev["needs_result"])
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
    # `series_games`: the REGULAR-season games in this one's series and
    # season (a Bulls season 80-odd, a Bears season 17, a playoff file none),
    # so a playoff game added to a team's file never reads as one of 80. A
    # date's events come postseason first and preseason last, then rarest
    # first (a Bears game before a Blackhawks game the same day), then home
    # before road, then by start —
    # so a reader taking a date's first event takes the one that matters
    # (Event Intelligence phase 4).
    sql = (f"SELECT e.*, s.name AS series_name, s.short_name, s.category, s.slug, s.league, "
           f"s.timezone AS series_timezone, s.home_venue AS series_home_venue, "
           f"(SELECT COUNT(*) FROM catalog_events o WHERE o.series_id=e.series_id AND o.season=e.season "
           f"AND o.season_type='regular') AS series_games FROM catalog_events e "
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
        rows = [dict(r) for r in conn.execute(
            sql + " ORDER BY e.event_date, CASE e.season_type WHEN 'postseason' THEN 0 WHEN 'preseason' THEN 2 "
                  "ELSE 1 END, series_games, "
                  "CASE e.home_away WHEN 'home' THEN 0 ELSE 1 END, "
                  "e.kickoff_local", args).fetchall()]
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
        r = conn.execute("SELECT e.*, s.name AS series_name, s.short_name, s.category, s.slug, s.league, "
                         "s.timezone AS series_timezone, s.home_venue AS series_home_venue "
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


def mark_past_completed(today=None, db_path=DB_PATH) -> int:
    """A scheduled event whose date has passed is completed (its result,
    when a source gives one, is kept or added later). "Passed" is against
    `today` when given, else each game's own local date (its timezone, or
    its series', `local_today`) — never the server's UTC date, which runs a
    day ahead of Central every evening (re-audit X-6). An if-necessary game
    with no result stays scheduled (`unresolved`: it may never have been
    played — phase 4 audit)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT e.id, e.event_date, e.status, e.result, e.attributes_json, "
            "COALESCE(e.timezone, s.timezone) AS tz FROM catalog_events e JOIN event_series s ON s.id=e.series_id "
            "WHERE e.status='scheduled' AND e.event_date IS NOT NULL").fetchall()
        todays, ids = {}, []
        for r in rows:
            t = _iso(today) if today else todays.setdefault(r["tz"], local_today(r["tz"]).isoformat())
            ev = dict(r)
            if str(ev["event_date"])[:10] < t and not unresolved(ev, today=t):
                ids.append(ev["id"])
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            conn.execute(f"UPDATE catalog_events SET status='completed', updated_at=datetime('now') "
                         f"WHERE status='scheduled' AND id IN ({','.join('?' for _ in chunk)})", chunk)
        conn.commit()
        return len(ids)
    finally:
        conn.close()


# ── one game's state: the predicates every reader shares (re-audit P4-03/04) ─

DEFAULT_TZ = "America/Chicago"      # a game or series that names no timezone


def local_today(tz=None):
    """Today's date on a clock — an IANA name, a Restaurant, or None for
    DEFAULT_TZ (Central) — through time_utils, never the server's own
    date."""
    from time_utils import restaurant_now
    return restaurant_now(tz or DEFAULT_TZ).date()


def _iso(v) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else str(v)[:10]


def attributes_of(e) -> dict:
    """A game's attributes, from a parsed `attributes` dict or the stored
    `attributes_json`. Never raises."""
    a = e.get("attributes")
    if isinstance(a, dict):
        return a
    try:
        out = json.loads(e.get("attributes_json") or "{}")
        return out if isinstance(out, dict) else {}
    except (TypeError, ValueError):
        return {}


def _event_today(e, today):
    return _iso(today) if today else local_today(e.get("timezone") or e.get("series_timezone")).isoformat()


def unresolved(e, today=None) -> bool:
    """An if-necessary game whose date has passed (before `today`, else the
    game's own local date) with no result entered and still `scheduled` —
    not cancelled, not postponed, not marked completed by an admin. It may
    or may not have been played: nothing may count it as played (no
    "played" count, no game-night figure, no recent game), and its night is
    neither a game night nor an ordinary one — it stays flagged, out of
    every baseline, and unmeasured until a result or a cancellation is
    entered (engine.sync_restaurant; the admin catalog's `needs_result`)."""
    if not e or not e.get("event_date") or not attributes_of(e).get("if_necessary"):
        return False
    if str(e.get("result") or "").strip() or (e.get("status") or "scheduled") != "scheduled":
        return False
    return str(e["event_date"])[:10] < _event_today(e, today)


def played(e, today=None) -> bool:
    """A game that happened: its date has passed (before `today`, else the
    game's own local date), it was neither cancelled nor postponed, and it
    is not an `unresolved` if-necessary game. The one test for "played"
    (gameday.season_value, playbook.game_night, Ask's recent games,
    engine.past_games)."""
    if not e or not e.get("event_date"):
        return False
    if e.get("status") in ("cancelled", "postponed"):
        return False
    t = _event_today(e, today)
    return str(e["event_date"])[:10] < t and not unresolved(e, today=t)


def alt_venue(e) -> bool:
    """A home game played away from the series' own ground (season file
    `attributes.alt_venue`: the Fire's 11/7/26 game at SeatGeek Stadium,
    19 km from Soldier Field). Its own segment in engine.effect_for, never
    pooled with the home venue's nights (re-audit SD-02)."""
    return (e or {}).get("home_away") == "home" and bool(attributes_of(e or {}).get("alt_venue"))


def unresolved_refs(refs, today=None, db_path=DB_PATH) -> set:
    """The demand_signals refs ("event:<id>") whose catalog game is
    `unresolved` — for a reader holding only a night's flags (event_memory's
    record_night). Never raises."""
    ids = {}
    for ref in refs or ():
        try:
            ids[int(str(ref).split(":", 1)[1])] = ref
        except (IndexError, TypeError, ValueError):
            continue
    if not ids:
        return set()
    try:
        conn = get_conn(db_path)
        try:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT e.id, e.event_date, e.status, e.result, e.attributes_json, e.timezone, "
                                f"s.timezone AS series_timezone FROM catalog_events e JOIN event_series s "
                                f"ON s.id=e.series_id WHERE e.id IN ({marks})", list(ids)).fetchall()
        finally:
            conn.close()
    except Exception:
        return set()
    return {ids[r["id"]] for r in rows if unresolved(dict(r), today=today)}


# ── auto-follows that no longer reach (re-audit P1-07) ─────────────────────

def refresh_auto_follow(restaurant_id, series_id, distance_km, in_reach, db_path=DB_PATH) -> bool:
    """An AUTO follow row brought up to date with where the restaurant is:
    in reach, its distance refreshed; out of reach, withdrawn (deleted, so a
    restaurant that moves back is followed again). An owner's or admin's
    row is never touched. True when an auto follow was withdrawn."""
    conn = get_conn(db_path)
    try:
        if in_reach:
            conn.execute("UPDATE event_follows SET distance_km=?, updated_at=datetime('now') WHERE restaurant_id=? "
                         "AND series_id=? AND source='auto' AND COALESCE(distance_km, -1) <> ?",
                         (distance_km, restaurant_id, int(series_id), distance_km))
            conn.commit()
            return False
        cur = conn.execute("DELETE FROM event_follows WHERE restaurant_id=? AND series_id=? AND source='auto'",
                           (restaurant_id, int(series_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def dismiss(restaurant_id, event_id, by=None, source="owner", db_path=DB_PATH):
    """Keep one game off this restaurant's calendar. `by` the login that
    removed it (an admin's name in view-as), `source` owner | admin."""
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO event_dismissals (restaurant_id, event_id, dismissed_by, source) "
                     "VALUES (?,?,?,?)", (restaurant_id, int(event_id), (str(by)[:120] if by else None),
                                          source if source in ("owner", "admin") else "owner"))
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


# ── putting a removed game back (event re-audit X-4) ───────────────────────
# A removal used to be permanent: no route, screen or admin action reversed
# it. Put back is per game, deliberately the ONE way back — a re-follow
# leaves removals alone, because an owner who removed the one game they are
# closed for would otherwise get it back silently by toggling the calendar.

def dismissals(restaurant_id, db_path=DB_PATH) -> list:
    """The games this restaurant removed, newest removal first: each the
    catalog row (with series_name, short_name, category, slug, league) plus
    dismissed_at, dismissed_by and dismissed_source (owner | admin)."""
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT e.*, s.name AS series_name, s.short_name, s.category, s.slug, s.league, "
            "d.created_at AS dismissed_at, d.dismissed_by, d.source AS dismissed_source "
            "FROM event_dismissals d JOIN catalog_events e ON e.id=d.event_id "
            "JOIN event_series s ON s.id=e.series_id WHERE d.restaurant_id=? "
            "ORDER BY d.created_at DESC, e.event_date DESC", (restaurant_id,)).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()
    for r in rows:
        try:
            r["attributes"] = json.loads(r.pop("attributes_json") or "{}")
        except (TypeError, ValueError):
            r["attributes"] = {}
        r.pop("overrides_json", None)
    return rows


def undismiss(restaurant_id, event_id, db_path=DB_PATH) -> bool:
    """Let the sync copy this game again. True when a removal was lifted."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM event_dismissals WHERE restaurant_id=? AND event_id=?",
                           (restaurant_id, int(event_id)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
