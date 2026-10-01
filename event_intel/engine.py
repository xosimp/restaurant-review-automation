"""
event_intel.engine — making every restaurant event-aware from one catalog.

  ensure_follows(r)     follow every series whose radius reaches the
                        restaurant (haversine from its geocoded lat/lng);
                        an owner's opt-out is never overridden
  sync_restaurant(r)    write each followed event into the restaurant's
                        demand_signals (source "events", ref "event:<id>"),
                        move or drop the copies the catalog moved or a
                        follow ended, and re-record the past game nights it
                        newly flagged (event_memory.record_night) so their
                        lift is measured at once
  run_event_sync()      the daily job: both, for every restaurant in
                        service, bounded and resumable

  label_for(e)          the demand_signals label. Home and away are kept
                        apart IN THE LABEL ("Bears home game · Soldier
                        Field" / "Bears road game") because event_memory
                        drops "home"/"away" as stop words: the forecast,
                        schedule and predictions then learn each one's lift
                        through the machinery they already use
  describe(e)           "Bears vs New York Jets · Sun 10/4/26 · 12pm · FOX"
  effect_for(rid, e)    this restaurant's measured lift on games like this
                        one, most specific first: same home/away and prime
                        time, then same home/away, then every game of the
                        series — from event_outcomes, never estimated
  context_for(rid, d)   the events on a date with their effect: what the
                        report, the brief and Ask say about it
  last_like(rid, e)     the last finished game of the same kind, with what
                        that night sold — "compared to our last home game"

Nothing here calls a model. Reads never raise into their caller.
"""
import logging
import math
from datetime import date, timedelta

from event_intel import store

log = logging.getLogger(__name__)

PAST_DAYS = 400          # game nights this far back are flagged (event_memory reads 400 days of history)
AHEAD_DAYS = 120         # and this far ahead
RECORD_MAX = 40          # past game nights re-recorded per restaurant per pass
SEGMENT_MIN_N = 2        # nights a segment needs before it is said
SIGNAL_SOURCE = "events"
SYNC_CURSOR_KEY = "event_sync"
SYNC_MAX_SECONDS = 600


def _d(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ── words ───────────────────────────────────────────────────────────────────

def label_for(e) -> str:
    short = e.get("short_name") or e.get("series_name") or "Event"
    if e.get("category") == "sports":
        if e.get("home_away") == "home":
            venue = e.get("venue")
            return f"{short} home game" + (f" · {venue}" if venue else "")
        return f"{short} road game"
    return e.get("opponent") or e.get("series_name") or short


def _clock(hhmm):
    if not hhmm:
        return None
    h, m = int(str(hhmm)[:2]), int(str(hhmm)[3:5])
    return f"{(h % 12) or 12}{'' if m == 0 else f':{m:02d}'}{'am' if h < 12 else 'pm'}"


def describe(e, with_date=True) -> str:
    """One line a person reads: who, against whom, when, where it's shown.
    `with_date` False where the date is already said (a day's own row)."""
    from time_utils import mdy
    short = e.get("short_name") or e.get("series_name") or ""
    if e.get("category") == "sports" and e.get("opponent"):
        who = f"{short} {'vs' if e.get('home_away') == 'home' else 'at'} {e['opponent']}"
    else:
        who = e.get("opponent") or e.get("series_name") or short
    bits = [who]
    if not with_date:
        pass
    elif e.get("event_date"):
        bits.append(f"{_d(e['event_date']).strftime('%a')} {mdy(e['event_date'])}")
    else:
        bits.append("date to be set")
    if _clock(e.get("kickoff_local")):
        bits.append(_clock(e["kickoff_local"]) + (" (prime time)" if e.get("is_primetime") else ""))
    if e.get("broadcast"):
        bits.append(e["broadcast"])
    if (e.get("attributes") or {}).get("holiday"):
        bits.append(e["attributes"]["holiday"])
    return " · ".join(bits)


def kind_words(e) -> str:
    """"home prime-time games" — the segment an effect is said about."""
    if e.get("category") != "sports":
        return "nights like it"
    side = "home" if e.get("home_away") == "home" else "road"
    return f"{side} prime-time games" if e.get("is_primetime") else f"{side} games"


# ── follows and sync ────────────────────────────────────────────────────────

def ensure_follows(restaurant, db_path=store.DB_PATH) -> list:
    """Auto-follow every series whose radius reaches this restaurant. Returns
    the slugs newly followed. A restaurant with no location follows nothing
    automatically (an owner or admin can still follow)."""
    lat, lng = getattr(restaurant, "latitude", None), getattr(restaurant, "longitude", None)
    if lat is None or lng is None:
        return []
    added = []
    for s in store.all_series(db_path=db_path):
        if s.get("lat") is None or s.get("lng") is None or not s.get("radius_km"):
            continue
        km = round(haversine_km(float(lat), float(lng), float(s["lat"]), float(s["lng"])), 1)
        if km <= float(s["radius_km"]) and store.auto_follow(restaurant.id, s["id"], km, db_path=db_path):
            added.append(s["slug"])
    return added


def _today(restaurant):
    try:
        from time_utils import restaurant_now
        return restaurant_now(restaurant).date()
    except Exception:
        return date.today()


def sync_restaurant(restaurant, today=None, db_path=store.DB_PATH) -> dict:
    """Write the followed events into this restaurant's demand_signals and
    measure the past game nights it newly flags. Idempotent."""
    import demand_signals
    rid = restaurant.id
    today = _d(today) if today else _today(restaurant)
    followed = store.follows(rid, db_path=db_path)
    events = store.events_for([f["series_id"] for f in followed], today - timedelta(days=PAST_DAYS),
                              today + timedelta(days=AHEAD_DAYS), db_path=db_path)
    skip = store.dismissed(rid, db_path=db_path)
    want, used = {}, set()
    for e in events:
        if e.get("status") in ("cancelled", "postponed") or e["id"] in skip:
            continue
        label, n = label_for(e), 1
        # Two games of one series on a date (a doubleheader) need two labels:
        # demand_signals keeps one row per date and label.
        while (e["event_date"], label) in used:
            n += 1
            label = f"{label_for(e)} · game {n}"
        used.add((e["event_date"], label))
        want[f"event:{e['id']}"] = (e["event_date"], label)
    conn = demand_signals.get_conn(db_path)
    added, moved, removed, new_past = 0, 0, 0, []
    try:
        # Only copies inside the window are the sync's to move or drop: a game
        # older than PAST_DAYS stays, measured — deleting it re-recorded the
        # night unflagged and erased last season's lift (audit 10/1/26).
        lo, hi = (today - timedelta(days=PAST_DAYS)).isoformat(), (today + timedelta(days=AHEAD_DAYS)).isoformat()
        have = {r["ref"]: dict(r) for r in conn.execute(
            "SELECT id, date, label, ref FROM demand_signals WHERE restaurant_id=? AND source=? AND "
            "((date >= ? AND date <= ?) OR ref IN (SELECT 'event:' || id FROM catalog_events WHERE "
            "event_date >= ? AND event_date <= ?))", (rid, SIGNAL_SOURCE, lo, hi, lo, hi)).fetchall() if r["ref"]}
        for ref, row in have.items():
            if ref not in want or (row["date"], row["label"]) != want[ref]:
                conn.execute("DELETE FROM demand_signals WHERE id=?", (row["id"],))
                removed += 1
                # A past night that loses its game is re-measured too, so
                # its old label stops counting toward the game's effect.
                if row["date"] < today.isoformat():
                    new_past.append(row["date"])
        for ref, (day, label) in want.items():
            if ref in have and (have[ref]["date"], have[ref]["label"]) == (day, label):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO demand_signals (restaurant_id, date, kind, label, source, created_by, ref) "
                "VALUES (?,?,?,?,?,?,?)", (rid, day, "event", label, SIGNAL_SOURCE, "Cavnar AI", ref))
            if cur.rowcount:
                if ref in have:
                    moved += 1
                else:
                    added += 1
                if day < today.isoformat():
                    new_past.append(day)
        conn.commit()
    finally:
        conn.close()
    recorded = 0
    if new_past:
        try:
            import event_memory
            for day in sorted(set(new_past), reverse=True)[:RECORD_MAX]:
                recorded += event_memory.record_night(rid, day, db_path=db_path).get("recorded", 0)
        except Exception as e:
            log.warning("event_intel: past game nights not recorded rid=%s: %s", rid, e)
    return {"followed": [f["slug"] for f in followed], "added": added, "moved": moved, "removed": removed,
            "nights_recorded": recorded}


def run_event_sync(db_path=None, now=None) -> dict:
    """Daily (before event_memory): the bundled seasons, every in-service
    restaurant's follows and its copy of the events, through
    scheduler.resumable_sweep. Returns {attempted, ok, failed, skipped,
    hit_bound}."""
    import threading
    import models
    db = db_path or store.DB_PATH
    store.load_bundled(db_path=db)
    # Yesterday's games are played (the Central-time date is close enough
    # for a catalog flag; each restaurant's own today drives its sync).
    store.mark_past_completed(date.today() - timedelta(days=1), db_path=db)
    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "added": 0, "recorded": 0}
    lock = threading.Lock()
    conn = store.get_conn(db)
    try:
        ids = sorted(r["id"] for r in conn.execute("SELECT id FROM restaurants WHERE " + models.in_service_sql())
                     .fetchall())
    finally:
        conn.close()

    def _one(rid):
        with lock:
            counts["attempted"] += 1
        try:
            r = models.get_restaurant(rid, db_path=db) if db_path else models.get_restaurant(rid)
            if r is None:
                with lock:
                    counts["skipped"] += 1
                return
            ensure_follows(r, db_path=db)
            got = sync_restaurant(r, db_path=db)
            with lock:
                counts["ok"] += 1
                counts["added"] += got["added"]
                counts["recorded"] += got["nights_recorded"]
        except Exception as e:
            with lock:
                counts["failed"] += 1
            import ops
            ops.capture(e, job="event_sync", context=f"restaurant_id={rid}")

    if ids:
        import scheduler
        _done, hit = scheduler.resumable_sweep(SYNC_CURSOR_KEY, ids, _one, SYNC_MAX_SECONDS, workers=1,
                                               job="event_sync")
        counts["hit_bound"] = bool(hit)
    return counts


# ── what the events did here ───────────────────────────────────────────────

def _outcomes(restaurant_id, dates, db_path, series_word=None):
    """{date: {"lift_pct", "net", "baseline", "covers", "labor_pct",
    "headcount_json", "labor_hours"}} — event_memory's record of those
    nights, one row per night (the strongest-evidence row when a night
    carried several labels)."""
    if not dates:
        return {}
    marks = ",".join("?" for _ in dates)
    conn = store.get_conn(db_path)
    try:
        rows = conn.execute(f"SELECT * FROM event_outcomes WHERE restaurant_id=? AND kind='event' "
                            f"AND lift_pct IS NOT NULL AND business_date IN ({marks})",
                            [restaurant_id] + list(dates)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    want = None
    if series_word:
        try:
            import event_memory
            want = set(event_memory.normalise_label(series_word).split()) or None
        except Exception:
            want = None
    out = {}
    for r in rows:
        r = dict(r)
        # Only the night's row about this series ("bears home soldier field"
        # holds "bears"), never another event on the same date.
        if want and not want <= set(str(r.get("label") or "").split()):
            continue
        out.setdefault(r["business_date"], r)
    return out


def _median(vals):
    s = sorted(vals)
    n = len(s)
    return None if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0)


def past_games(restaurant_id, e, db_path=store.DB_PATH) -> list:
    """Finished events of the same series before this one, with what each
    night measured here: [{event, outcome}] newest first."""
    if not e.get("event_date"):
        before = date.max.isoformat()
    else:
        before = e["event_date"]
    rows = [x for x in store.events_for([e["series_id"]], None, before, db_path=db_path)
            if x["event_date"] < before and x["id"] != e.get("id")]
    outs = _outcomes(restaurant_id, [x["event_date"] for x in rows], db_path,
                     series_word=e.get("short_name") or e.get("series_name"))
    # One night, one game: a doubleheader's two events share one outcome row
    # and would count it twice toward a segment's floor (audit 10/1/26).
    seen, got = set(), []
    for x in rows:
        if x["event_date"] in outs and x["event_date"] not in seen:
            seen.add(x["event_date"])
            got.append({"event": x, "outcome": outs[x["event_date"]]})
    return sorted(got, key=lambda g: g["event"]["event_date"], reverse=True)


def effect_for(restaurant_id, e, db_path=store.DB_PATH):
    """{"segment", "n", "median_lift_pct", "low_pct", "high_pct", "dates",
    "basis"} for games like this one at this restaurant, the most specific
    segment with SEGMENT_MIN_N nights; None before there are any."""
    games = past_games(restaurant_id, e, db_path=db_path)
    if not games:
        return None
    side = e.get("home_away")
    pre = lambda x: x.get("season_type") == "preseason"
    same_class = lambda g: pre(g["event"]) == pre(e)
    # Most specific first, and never across sides: a road game is never
    # told what home games did. Preseason is kept apart while regular-season
    # nights suffice (audit 10/1/26).
    segs = [
        (kind_words(e), lambda g: g["event"].get("home_away") == side and same_class(g)
         and bool(g["event"].get("is_primetime")) == bool(e.get("is_primetime"))),
        (kind_words(dict(e, is_primetime=0)), lambda g: g["event"].get("home_away") == side and same_class(g)),
        (kind_words(dict(e, is_primetime=0)), lambda g: g["event"].get("home_away") == side),
    ]
    short = e.get("short_name") or e.get("series_name") or ""
    for words, keep in segs:
        hits = [g for g in games if keep(g)]
        # A night that carried something else too (a game on Christmas, on
        # a rainy payday) is left out while clean nights suffice; when it
        # has to count, the sentence says so.
        clean = [g for g in hits if not int(g["outcome"].get("confounded") or 0)]
        use, mixed = (clean, False) if len(clean) >= SEGMENT_MIN_N else (hits, len(clean) < len(hits))
        if len(use) >= SEGMENT_MIN_N:
            lifts = [float(g["outcome"]["lift_pct"]) for g in use]
            med = _median(lifts)
            basis = (f"{short} {words} have run {med:+.0f}% against a usual same weekday here "
                     f"(median of {len(use)}, {min(lifts):+.0f}% to {max(lifts):+.0f}%"
                     + ("; some of those nights had something else on too" if mixed else "") + ")")
            return {"segment": f"{short} {words}".strip(), "n": len(use), "median_lift_pct": round(med, 1),
                    "low_pct": round(min(lifts), 1), "high_pct": round(max(lifts), 1), "confounded": mixed,
                    "dates": [g["event"]["event_date"] for g in use], "basis": basis}
    return None


def last_like(restaurant_id, e, db_path=store.DB_PATH):
    """The last finished game of the same side (home/road) with its night:
    {"event", "describe", "net", "baseline", "lift_pct", "covers",
    "labor_pct", "headcount"} or None."""
    import json
    for g in past_games(restaurant_id, e, db_path=db_path):
        if g["event"].get("home_away") == e.get("home_away"):
            o = g["outcome"]
            try:
                heads = json.loads(o.get("headcount_json") or "null")
            except (TypeError, ValueError):
                heads = None
            return {"event": g["event"], "describe": describe(g["event"]), "net": o.get("net"),
                    "baseline": o.get("baseline"), "lift_pct": o.get("lift_pct"), "covers": o.get("covers"),
                    "labor_pct": o.get("labor_pct"), "headcount": heads, "labor_hours": o.get("labor_hours")}
    return None


def context_for(restaurant_id, day, db_path=store.DB_PATH) -> list:
    """The followed events on a date, each {"event", "describe", "label",
    "effect", "last_like"} — what the report, the brief and Ask say."""
    day = _d(day).isoformat()
    followed = store.follows(restaurant_id, db_path=db_path)
    out = []
    for e in store.events_for([f["series_id"] for f in followed], day, day, db_path=db_path):
        out.append({"event": e, "describe": describe(e), "label": label_for(e),
                    "effect": effect_for(restaurant_id, e, db_path=db_path),
                    "last_like": last_like(restaurant_id, e, db_path=db_path)})
    return out


def upcoming(restaurant_id, days=14, today=None, db_path=store.DB_PATH) -> list:
    """The followed events in the next `days`, each with its effect. Without
    `today`, the restaurant's own local date (never the server's UTC one)."""
    if today:
        start = _d(today)
    else:
        try:
            import models
            start = _today(models.get_restaurant(restaurant_id, db_path=db_path))
        except Exception:
            start = date.today()
    followed = store.follows(restaurant_id, db_path=db_path)
    rows = store.events_for([f["series_id"] for f in followed], start, start + timedelta(days=int(days)),
                            db_path=db_path)
    return [{"event": e, "describe": describe(e), "label": label_for(e),
             "effect": effect_for(restaurant_id, e, db_path=db_path)} for e in rows]


def context_by_ref(restaurant_id, ref, db_path=store.DB_PATH):
    """A demand_signals ref ("event:<id>") back to its catalog event."""
    if not str(ref or "").startswith("event:"):
        return None
    try:
        e = store.event_by_id(int(str(ref).split(":", 1)[1]), db_path=db_path)
    except (TypeError, ValueError):
        return None
    if not e:
        return None
    return {"event": e, "describe": describe(e), "describe_short": describe(e, with_date=False),
            "label": label_for(e), "effect": effect_for(restaurant_id, e, db_path=db_path)}


# ── owner follow settings (phase 2) ────────────────────────────────────────

def follow_choices(restaurant, today=None, db_path=store.DB_PATH) -> list:
    """The calendars an owner can follow or stop following here: every
    series this restaurant has a follow row for (on or off) and every one
    whose radius reaches it. Each {"series_id", "slug", "name", "category",
    "following", "source", "distance_km", "in_reach", "next"}."""
    rows = {f["series_id"]: f for f in store.follows(restaurant.id, active_only=False, db_path=db_path)}
    lat, lng = getattr(restaurant, "latitude", None), getattr(restaurant, "longitude", None)
    start = _d(today) if today else _today(restaurant)
    out = []
    for s in store.all_series(db_path=db_path):
        km = None
        if lat is not None and lng is not None and s.get("lat") is not None and s.get("lng") is not None:
            km = round(haversine_km(float(lat), float(lng), float(s["lat"]), float(s["lng"])), 1)
        reach = km is not None and bool(s.get("radius_km")) and km <= float(s["radius_km"])
        f = rows.get(s["id"])
        if not f and not reach:
            continue
        nxt = [e for e in store.events_for([s["id"]], start, None, db_path=db_path)
               if e.get("status") not in ("cancelled", "postponed")][:1]
        # What the season's games brought here, measured (phase 3) — beside
        # the switch, so the choice is made knowing it.
        season = None
        if f and f["active"]:
            from event_intel import gameday
            sv = gameday.season_value(restaurant.id, s["id"], today=start, db_path=db_path)
            season = {"text": sv["text"], "measured": sv["measured"], "played": sv["played"],
                      "incremental": sv["incremental"], "basis": sv["basis"]} if sv else None
        out.append({"series_id": s["id"], "slug": s["slug"], "name": s["name"], "category": s["category"],
                    "following": bool(f and f["active"]), "source": f["source"] if f else None,
                    "distance_km": km if km is not None else (f or {}).get("distance_km"), "in_reach": reach,
                    "next": describe(nxt[0]) if nxt else None, "season": season})
    return out


def set_owner_follow(restaurant, series_id, active, source="owner", db_path=store.DB_PATH) -> dict:
    """An owner's (or admin's) follow choice, then this restaurant's copy of
    the games re-synced at once — the games appear, or leave the schedule's
    inputs, without waiting for the 5am job. Raises LookupError for a
    series that isn't in the catalog."""
    if not any(s["id"] == int(series_id) for s in store.all_series(db_path=db_path)):
        raise LookupError("No such calendar.")
    store.set_follow(restaurant.id, int(series_id), bool(active), source=source, db_path=db_path)
    return sync_restaurant(restaurant, db_path=db_path)
