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
  describe(e, tz=)      "Bears vs New York Jets · Sun 10/4/26 · 12pm · FOX",
                        the start on the restaurant's clock when `tz` is
                        given (local_kickoff)
  local_kickoff(e, tz)  (date, "HH:MM") of a game's start on a restaurant's
                        clock — catalog kickoffs are the series' own zone
  effect_for(rid, e)    this restaurant's measured lift on games like this
                        one, most specific first: same class and prime
                        time, then same class (game_class: side, season
                        class — regular, preseason, playoffs, special — and
                        home ground or another) — never across a class,
                        from event_outcomes, never estimated; for a
                        frequent series' game (not a playoff game), its
                        label's measured figure, the one headline judges it
                        by — the label carries the season class too
  headline(rid, e)      whether a game earns an owner's attention unasked
  one_read()            a call's shared reads (past games, usual nights)
  context_for(rid, d)   the events on a date with their effect: what the
                        report, the brief and Ask say about it
  last_like(rid, e)     the last finished game of the same kind, with what
                        that night sold — "compared to our last home game"

Nothing here calls a model. Reads never raise into their caller.
"""
import contextlib
import contextvars
import logging
import math
from datetime import date, timedelta

from event_intel import store

log = logging.getLogger(__name__)

PAST_DAYS = 400          # game nights this far back are flagged (event_memory reads 400 days of history)
AHEAD_DAYS = 120         # and this far ahead
RECORD_MAX = 40          # past game nights re-recorded per restaurant per pass; the rest are queued
RECORD_QUEUE_PREFIX = "event_sync_record:"   # job_cursors: the nights a later pass still owes
SEGMENT_MIN_N = 2        # nights a segment needs before it is said
# past_games reads at most this far back (POS and punches are kept 1095
# days, so an older night can be neither re-measured nor re-read) and at
# most this many of the newest measured games of each class (side, season
# class, home ground) — so a Home load, which reads each past game's
# punches, checks and items, costs the same in a team's fifth season as in
# its second (re-audit X-2).
PAST_GAMES_DAYS = 1095
PAST_GAMES_PER_CLASS = 16
UNRESOLVED_SUFFIX = " · if necessary"   # a past if-necessary game's copy while no result is in
SIGNAL_SOURCE = "events"
SYNC_CURSOR_KEY = "event_sync"
SYNC_MAX_SECONDS = 600


def _d(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


# ── one read: what a single call reads once (re-audit X-2) ─────────────────
#
# A game's plan (playbook.alert: staffing, the rush, the item mix) walks the
# same past games, and each past game the same usual nights, three times
# over; consecutive games' usual nights overlap seven weeks in eight. Inside
# `one_read()` each of those reads (past_games, playbook.usual_nights and its
# per-night punches and checks) is made once and shared. The scope is the
# calling context's only (contextvars): nothing is kept between calls, so a
# figure is never older than the call that says it.

_ONE_READ = contextvars.ContextVar("event_intel_one_read", default=None)


@contextlib.contextmanager
def one_read():
    """Share reads within one call; reentrant (an inner scope joins the
    outer one)."""
    if _ONE_READ.get() is not None:
        yield
        return
    token = _ONE_READ.set({})
    try:
        yield
    finally:
        _ONE_READ.reset(token)


def read_once(fn):
    """A public read run inside one_read() (playbook.alert: a whole brief
    line's reads shared)."""
    import functools

    @functools.wraps(fn)
    def wrapped(*a, **k):
        with one_read():
            return fn(*a, **k)
    return wrapped


def memo(key, fn):
    """fn() once per `key` inside one_read(); a plain call outside it."""
    m = _ONE_READ.get()
    if m is None:
        return fn()
    if key not in m:
        m[key] = fn()
    return m[key]


def haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ── words ───────────────────────────────────────────────────────────────────

HOME_TOKEN = "home venue"      # a home game's label when neither it nor its series names a venue


SEASON_CLASSES = ("regular", "preseason", "postseason", "special")


def season_class(e) -> str:
    """"regular", "preseason", "postseason" or "special" (a cup match, an
    exhibition: store.SEASON_TYPES) — the season class a game is never
    measured across (game_class, label_for). A game naming no known season
    type is the regular season's."""
    st = (e or {}).get("season_type")
    return st if st in SEASON_CLASSES else "regular"


def _class_word(e) -> str:
    """The season class's word in a label and a sentence ("preseason",
    "playoff", "special"; "" for the regular season) —
    event_memory.SEASON_CLASS_WORDS, the words its record match keeps
    apart."""
    import event_memory
    return event_memory.SEASON_CLASS_WORDS.get(season_class(e), "")


def label_for(e) -> str:
    """The demand_signals label. A home game always carries a word of its
    own after "home game" — its venue, else its series' home venue, else
    HOME_TOKEN — because "home" and "game" are stop words: a bare "Sox home
    game" normalised to "sox", a subset of the road label "sox road", and
    measured_effect merged the two (re-audit P1-09). A preseason, playoff or
    special (cup) game names its class right after the team ("Bulls
    preseason home game · United Center"), so its nights are never pooled
    with another class's in any label reader — the quiet test, the
    forecast, by_date, the baselines (event re-audit 2, R1-02 / R4-01 /
    RX-02)."""
    short = e.get("short_name") or e.get("series_name") or "Event"
    if e.get("category") == "sports":
        who = f"{short} {_class_word(e)}".strip()
        if e.get("home_away") == "home":
            venue = e.get("venue") or e.get("series_home_venue") or e.get("home_venue") or HOME_TOKEN
            return f"{who} home game · {venue}"
        return f"{who} road game"
    return e.get("opponent") or e.get("series_name") or short


# What a game's start is called, by league ("two hours before puck drop").
START_WORDS = {"NFL": "kickoff", "MLS": "kickoff", "NHL": "puck drop", "NBA": "tip-off", "WNBA": "tip-off",
               "MLB": "first pitch"}


def start_word(e) -> str:
    return START_WORDS.get(str((e or {}).get("league") or "").upper(), "the start")


def _clock(hhmm):
    if not hhmm:
        return None
    h, m = int(str(hhmm)[:2]), int(str(hhmm)[3:5])
    return f"{(h % 12) or 12}{'' if m == 0 else f':{m:02d}'}{'am' if h < 12 else 'pm'}"


def local_kickoff(e, tz=None):
    """(date, "HH:MM" or None) of a game's start on the restaurant's clock.
    A catalog kickoff is wall-clock time in the game's own `timezone`, else
    its series' (`series_timezone`), else store.DEFAULT_TZ — every bundled
    file is Central — and a restaurant 115 km from Soldier Field may be on
    Eastern time (re-audit P2-04). `tz` is the restaurant's clock: a
    Restaurant, an IANA name, or None to keep the catalog's own. The date
    moves with the time across midnight. None for a game with no date.
    Never raises."""
    if not e or not e.get("event_date"):
        return None
    day = _d(e["event_date"])
    k = str(e.get("kickoff_local") or "")
    if len(k) < 5 or k[2] != ":":
        return day, None
    if tz is None:
        return day, k[:5]
    try:
        from datetime import datetime, time as _time
        from zoneinfo import ZoneInfo
        from time_utils import restaurant_tz
        src = e.get("timezone") or e.get("series_timezone") or store.DEFAULT_TZ
        at = datetime.combine(day, _time(int(k[:2]), int(k[3:5])), tzinfo=ZoneInfo(src))
        here = at.astimezone(restaurant_tz(tz))
        return here.date(), here.strftime("%H:%M")
    except Exception:
        return day, k[:5]


def restaurant_clock(restaurant_id, db_path=store.DB_PATH):
    """A restaurant's timezone name for local_kickoff / describe(tz=), or
    None (the catalog's own clock) when it can't be read. Never raises."""
    try:
        import models
        r = models.get_restaurant(restaurant_id, db_path=db_path)
        return getattr(r, "timezone", None) or None
    except Exception:
        return None


def describe(e, with_date=True, tz=None) -> str:
    """One line a person reads: who, against whom, when, where it's shown.
    `with_date` False where the date is already said (a day's own row).
    `tz` (the restaurant's clock — a Restaurant or an IANA name) says the
    start, and its date, on that clock (local_kickoff); without it the
    catalog's own clock."""
    from time_utils import mdy
    short = e.get("short_name") or e.get("series_name") or ""
    if e.get("category") == "sports" and e.get("opponent"):
        who = f"{short} {'vs' if e.get('home_away') == 'home' else 'at'} {e['opponent']}"
    else:
        who = e.get("opponent") or e.get("series_name") or short
    bits = [who]
    lk = local_kickoff(e, tz)
    if not with_date:
        pass
    elif lk:
        bits.append(f"{lk[0].strftime('%a')} {mdy(lk[0])}")
    else:
        bits.append("date to be set")
    start = lk[1] if lk else e.get("kickoff_local")      # a date to be set keeps the catalog's clock
    if _clock(start):
        bits.append(_clock(start) + (" (prime time)" if e.get("is_primetime") else ""))
    if e.get("broadcast"):
        bits.append(e["broadcast"])
    if (e.get("attributes") or {}).get("holiday"):
        bits.append(e["attributes"]["holiday"])
    if (e.get("attributes") or {}).get("if_necessary"):
        bits.append("if necessary")
    return " · ".join(bits)


def kind_words(e) -> str:
    """"home prime-time games" — the segment an effect is said about. A
    preseason, playoff or special game's class is said ("preseason home
    games"), and a home game away from the series' ground is its own:
    "home games at SeatGeek Stadium" (store.alt_venue)."""
    if e.get("category") != "sports":
        return "nights like it"
    side = f"{_class_word(e)} {'home' if e.get('home_away') == 'home' else 'road'}".strip()
    words = f"{side} prime-time games" if e.get("is_primetime") else f"{side} games"
    if store.alt_venue(e):
        words += f" at {e.get('venue') or 'another ground'}"
    return words


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
        reach = km <= float(s["radius_km"])
        # An auto follow tracks where the restaurant is now: a corrected
        # address outside the radius withdraws it, and its distance is kept
        # current (re-audit P1-07). An owner's or admin's row is theirs.
        store.refresh_auto_follow(restaurant.id, s["id"], km, reach, db_path=db_path)
        if reach and store.auto_follow(restaurant.id, s["id"], km, db_path=db_path):
            added.append(s["slug"])
    return added


def _today(restaurant):
    try:
        from time_utils import restaurant_now
        return restaurant_now(restaurant).date()
    except Exception:
        return store.local_today()


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
    want, used, pending_days = {}, set(), set()
    for e in events:
        if e.get("status") in ("cancelled", "postponed") or e["id"] in skip:
            continue
        base = label_for(e)
        if store.unresolved(e, today=today):
            # An if-necessary game past its date with no result may or may
            # not have been played. Its night stays flagged — out of every
            # baseline, never an ordinary night (re-audit P4-03: erasing it
            # put a playoff night into 8 weeks of baselines) — and is not
            # measured here. The label says so, so a result or a
            # cancellation entered later changes the copy and the night is
            # re-recorded as what it was.
            base += UNRESOLVED_SUFFIX
            pending_days.add(e["event_date"])
        label, n = base, 1
        # Two games of one series on a date (a doubleheader) need two labels:
        # demand_signals keeps one row per date and label.
        while (e["event_date"], label) in used:
            n += 1
            label = f"{base} · game {n}"
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
    recorded, left = _record_past(restaurant, new_past, pending_days, today, db_path)
    return {"followed": [f["slug"] for f in followed], "added": added, "moved": moved, "removed": removed,
            "nights_recorded": recorded, "nights_queued": left}


def _record_queue(restaurant_id, db_path, value=None):
    """The past nights this restaurant's sync still owes a re-record
    (job_cursors `event_sync_record:<rid>`, a JSON list of ISO dates): read,
    or written when `value` is given ([] removes the row)."""
    import json
    key = f"{RECORD_QUEUE_PREFIX}{restaurant_id}"
    conn = store.get_conn(db_path)
    try:
        if value is None:
            r = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
            try:
                got = json.loads(r["value"]) if r and r["value"] else []
            except (TypeError, ValueError):
                got = []
            return [str(d)[:10] for d in got if d] if isinstance(got, list) else []
        if value:
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (key, json.dumps(sorted(set(value), reverse=True))))
        else:
            conn.execute("DELETE FROM job_cursors WHERE key=?", (key,))
        conn.commit()
        return value
    finally:
        conn.close()


def remeasure_past(restaurant, days, today=None, db_path=store.DB_PATH) -> dict:
    """Re-measure past nights whose flags changed outside a sync — an owner
    removing a game from their list (demand_signals.delete, event re-audit
    2, R1-01 / RX-05): the sync's own rule (_record_past) for those nights
    alone — a restaurant that learns for itself, nights inside PAST_DAYS
    before its own today — without draining what an earlier pass owes
    (that stays for the next sync). A night it cannot measure now waits in
    the record queue. {"nights_recorded", "nights_queued"}. Never raises."""
    try:
        today = _d(today) if today else _today(restaurant)
        recorded, left = _record_past(restaurant, [str(d)[:10] for d in days or ()], set(), today, db_path,
                                      drain=False)
        return {"nights_recorded": recorded, "nights_queued": left}
    except Exception as e:
        log.warning("event_intel: nights not re-measured rid=%s: %s", getattr(restaurant, "id", None), e)
        return {"nights_recorded": 0, "nights_queued": 0}


def _record_past(restaurant, new_past, pending_days, today, db_path, drain=True):
    """Re-record the past nights whose flags this pass changed, plus the
    ones an earlier pass owed, RECORD_MAX per pass, newest first. The rest
    wait in the restaurant's record queue for the next pass — a bulk follow
    change (a Bulls season, 60+ nights) used to re-record 40 and leave the
    others stale or unmeasured for good (re-audit P1-06 / P4-13 / X-1).
    Only a restaurant that learns for itself (models.learns_for_itself: not
    a demo, not admin-excluded) is measured at all (re-audit P1-05); its
    demand_signals copy is written either way. An `unresolved` if-necessary
    game's night is never measured here. `drain` False measures `new_past`
    only and leaves the owed nights queued (remeasure_past). Returns
    (recorded, still queued)."""
    import models
    if not models.learns_for_itself(restaurant):
        return 0, 0
    rid = restaurant.id
    floor = (today - timedelta(days=PAST_DAYS)).isoformat()
    try:
        owed = _record_queue(rid, db_path)
    except Exception as e:
        log.warning("event_intel: record queue unreadable rid=%s: %s", rid, e)
        owed = []
    in_window = lambda d: floor <= d < today.isoformat() and d not in pending_days
    todo = sorted({d for d in (list(owed) if drain else []) + list(new_past) if in_window(d)}, reverse=True)
    if not todo and not owed:
        return 0, 0
    recorded, done = 0, set()
    try:
        import event_memory
        for day in todo[:RECORD_MAX]:
            recorded += event_memory.record_night(rid, day, db_path=db_path).get("recorded", 0)
            done.add(day)
    except Exception as e:
        log.warning("event_intel: past game nights not recorded rid=%s: %s", rid, e)
    left = [d for d in todo if d not in done]
    if not drain:
        left = sorted(set(left) | {d for d in owed if in_window(d)}, reverse=True)
    if sorted(left) != sorted(set(owed)):
        try:
            _record_queue(rid, db_path, value=left)
        except Exception as e:
            log.warning("event_intel: record queue not written rid=%s: %s", rid, e)
    return recorded, len(left)


def run_event_sync(db_path=None, now=None) -> dict:
    """Daily (before event_memory): the bundled seasons, every in-service
    restaurant's follows and its copy of the events, through
    scheduler.resumable_sweep. Returns {attempted, ok, failed, skipped,
    hit_bound}."""
    import threading
    import models
    db = db_path or store.DB_PATH
    store.load_bundled(db_path=db)
    # Games before today are played — today on each game's own clock
    # (store.local_today: Central for every bundled file), never the
    # server's UTC date, which is tomorrow from 7pm Central (re-audit X-6).
    # Each restaurant's own today drives its sync.
    store.mark_past_completed(db_path=db)
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


def game_class(e) -> tuple:
    """(side, season class, home ground elsewhere?) — the class a game is
    never measured across: a road game is never told what home games did,
    preseason, the playoffs and special games are each their own crowd
    (season_class — the class label_for carries too, so the label's record
    and these segments keep the same nights apart), and a home game away
    from the series' ground (store.alt_venue) is its own night."""
    return (e.get("home_away"), season_class(e), store.alt_venue(e))


def past_games(restaurant_id, e, db_path=store.DB_PATH) -> list:
    """Played games of the same series before this one (store.played as of
    its date: never a cancelled or postponed one, never an if-necessary game
    with no result), with what each night measured here: [{event, outcome}]
    newest first. Bounded: games inside PAST_GAMES_DAYS of this one, and at
    most PAST_GAMES_PER_CLASS of the newest measured games of each
    game_class — effect_for's segments sit inside one class, so each is the
    median of its newest nights (re-audit X-2). Read once per one_read()."""
    key = ("past_games", restaurant_id, e.get("series_id"), e.get("event_date"), e.get("id"),
           e.get("short_name") or e.get("series_name"), db_path)
    return list(memo(key, lambda: _past_games(restaurant_id, e, db_path)))


def _past_games(restaurant_id, e, db_path):
    if not e.get("event_date"):
        before = store.local_today(e.get("timezone") or e.get("series_timezone")).isoformat()
    else:
        before = e["event_date"]
    lo = (_d(before) - timedelta(days=PAST_GAMES_DAYS)).isoformat()
    rows = [x for x in store.events_for([e["series_id"]], lo, before, db_path=db_path)
            if x["event_date"] < before and x["id"] != e.get("id") and store.played(x, today=before)]
    outs = _outcomes(restaurant_id, [x["event_date"] for x in rows], db_path,
                     series_word=e.get("short_name") or e.get("series_name"))
    # One night, one game: a doubleheader's two events share one outcome row
    # and would count it twice toward a segment's floor (audit 10/1/26).
    seen, got, per = set(), [], {}
    for x in sorted(rows, key=lambda r: r["event_date"], reverse=True):
        if x["event_date"] in outs and x["event_date"] not in seen:
            seen.add(x["event_date"])
            k = game_class(x)
            if per.get(k, 0) >= PAST_GAMES_PER_CLASS:
                continue
            per[k] = per.get(k, 0) + 1
            got.append({"event": x, "outcome": outs[x["event_date"]]})
    return got


def same_kind(e, g) -> bool:
    """Games a plan for `e` may rest on — staffing, the rush and the item
    mix alike: the same game_class (side, preseason or not, the home ground
    or another — re-audit SD-02) and the same kickoff class (prime time or
    not). The one test playbook and gameday both use."""
    return game_class(g) == game_class(e) and bool(g.get("is_primetime")) == bool(e.get("is_primetime"))


def _regular_games(e, db_path) -> int:
    """The series' regular-season games in the game's season (catalog
    `series_games`, else counted) — what makes a series frequent."""
    n = e.get("series_games")
    if n is None:
        conn = store.get_conn(db_path)
        try:
            r = conn.execute("SELECT COUNT(*) AS n FROM catalog_events WHERE series_id=? AND season=? "
                             "AND season_type='regular'", (e.get("series_id"), e.get("season"))).fetchone()
            n = int(r["n"] or 0) if r else 0
        finally:
            conn.close()
    return int(n or 0)


def _judged_by_its_label(e, db_path) -> bool:
    """A frequent series' regular-season, preseason or special game:
    whether it earns attention is decided by its label's measured figure
    (event_memory.quiet_game, the baselines' own test), so that figure is
    the one said about it. The label carries the season class (label_for),
    so a preseason game's figure is its preseason nights' alone. A playoff
    game is never quiet, and is said from its own segments."""
    import event_memory
    return season_class(e) != "postseason" and \
        _regular_games(e, db_path) >= event_memory.FREQUENT_SERIES_GAMES


def _label_effect(restaurant_id, e, db_path):
    """effect_for's answer for a game judged by its label: the label's
    measured figure (event_memory.measured_effect — the one the quiet test
    and the forecast use), in effect_for's shape, or None below
    SEGMENT_MIN_N nights."""
    import event_memory
    m = event_memory.measured_effect(restaurant_id, label_for(e), db_path=db_path)
    if not m or int(m.get("n") or 0) < SEGMENT_MIN_N:
        return None
    short = e.get("short_name") or e.get("series_name") or ""
    words = kind_words(dict(e, is_primetime=0))
    med, lo, hi, n = float(m["median_lift_pct"]), float(m["low_lift_pct"]), float(m["high_lift_pct"]), int(m["n"])
    basis = (f"{short} {words} have run {med:+.0f}% against a usual same weekday here "
             f"(median of {n}, {lo:+.0f}% to {hi:+.0f}%"
             + ("; some of those nights had something else on too" if m.get("confounded") else "") + ")")
    return {"segment": f"{short} {words}".strip(), "n": n, "median_lift_pct": round(med, 1),
            "low_pct": round(lo, 1), "high_pct": round(hi, 1), "confounded": bool(m.get("confounded")),
            "dates": list(m.get("dates") or []), "basis": basis, "applies": bool(m.get("applies"))}


def effect_for(restaurant_id, e, db_path=store.DB_PATH, exact=False, keep=None):
    """{"segment", "n", "median_lift_pct", "low_pct", "high_pct", "dates",
    "basis", "applies"} for games like this one at this restaurant, the most
    specific segment with SEGMENT_MIN_N nights; None before there are any.

    One figure decides and is said (re-audit P4-07): a frequent series'
    game (regular-season, preseason or special — its label carries the
    class) earns an owner's attention by its label's measured figure
    (headline → event_memory.quiet_game), so that is the figure said about
    it here (_label_effect) — never a segment median that can sit on
    the other side of EFFECT_FLOOR_PCT. `exact` (a cross-restaurant figure,
    event_intel.peers) and `keep` always read the segments."""
    if not exact and keep is None and _judged_by_its_label(e, db_path):
        return _label_effect(restaurant_id, e, db_path)
    games = past_games(restaurant_id, e, db_path=db_path)
    if keep is not None:
        games = [g for g in games if keep(g["event"]["event_date"])]
    if not games:
        return None
    mine = game_class(e)
    same_class = lambda g: game_class(g["event"]) == mine
    # Most specific first, and never across a class (game_class) in ANY
    # segment: a road game is never told what home games did, no season
    # class is pooled with another (season_class) — the old last
    # fallback dropped the season test, so a preseason game's push said
    # regular-season lifts (re-audit P3-02) — and a home game at another
    # ground is its own segment (re-audit SD-02).
    segs = [
        (kind_words(e), lambda g: same_class(g)
         and bool(g["event"].get("is_primetime")) == bool(e.get("is_primetime"))),
        (kind_words(dict(e, is_primetime=0)), same_class),
    ]
    if exact:
        # One segment, no fallback: same side, same season class, prime time
        # ignored (a cross-restaurant figure — event_intel.peers).
        segs = [segs[1]]
    short = e.get("short_name") or e.get("series_name") or ""
    import event_memory
    for words, in_seg in segs:
        hits = [g for g in games if in_seg(g)]
        # A night that carried something else too (a game on Christmas, on
        # a rainy payday) is left out while clean nights alone reach the
        # floor `applies` decides on (EFFECT_MIN_N — the label record's own
        # rule, event_memory._summary); when they can't, every night counts
        # and the sentence says so. One more clean night never turns a
        # pattern into "not enough" (event re-audit 2, R4-03 / R1-05).
        use, mixed = store.clean_first(hits, event_memory.EFFECT_MIN_N)
        if len(use) >= SEGMENT_MIN_N:
            lifts = [float(g["outcome"]["lift_pct"]) for g in use]
            med = _median(lifts)
            basis = (f"{short} {words} have run {med:+.0f}% against a usual same weekday here "
                     f"(median of {len(use)}, {min(lifts):+.0f}% to {max(lifts):+.0f}%"
                     + ("; some of those nights had something else on too" if mixed else "") + ")")
            return {"segment": f"{short} {words}".strip(), "n": len(use), "median_lift_pct": round(med, 1),
                    "low_pct": round(min(lifts), 1), "high_pct": round(max(lifts), 1), "confounded": mixed,
                    "dates": [g["event"]["event_date"] for g in use], "basis": basis,
                    "applies": len(use) >= event_memory.EFFECT_MIN_N}
    return None


def headline(restaurant_id, e, db_path=store.DB_PATH) -> bool:
    """Whether a game earns an owner's attention unasked — the brief's alert,
    the report's games, the game-night line, Food Cost's game week, the
    push: a playoff game, any game of a series with fewer than
    event_memory.FREQUENT_SERIES_GAMES regular-season games (a Bears
    season), and a frequent series' game (Bulls, Blackhawks, Fire) only once
    games like it are measured to matter here — event_memory.quiet_game,
    the one test the baselines use too (phase 4 audit). Until then it is
    context: on the calendar and in Ask, and measured on every night that
    has event_memory.BASELINE_MIN ordinary same weekdays before it.

    It judges from the figure the surfaces say (effect_for, re-audit
    P4-07): a frequent series' game by its label's measured figure — the
    quiet test's own, which effect_for says for it. The label carries the
    season class (label_for), so a preseason or special game is judged by
    its own class's nights alone, by the same test every label reader uses
    (event re-audit 2, R1-02 / R4-01)."""
    import event_memory
    return not event_memory.quiet_game(restaurant_id, label_for(e), _regular_games(e, db_path),
                                       e.get("season_type"), db_path=db_path)


def last_like(restaurant_id, e, db_path=store.DB_PATH, tz=None):
    """The last finished game like this one — most specific first, as
    effect_for: the same kind (same_kind: game_class and prime time, the
    push's own test), else the same game_class (side, season class, ground)
    — never a preseason game for a regular-season one or the reverse (event
    re-audit 2, R2-04 / R1-06 / R4-05) — with its night: {"event",
    "describe", "net", "baseline", "lift_pct", "covers", "labor_pct",
    "headcount"} or None. `tz` as describe's."""
    import json
    games = past_games(restaurant_id, e, db_path=db_path)
    mine = game_class(e)
    g = next((g for g in games if same_kind(e, g["event"])), None) or \
        next((g for g in games if game_class(g["event"]) == mine), None)
    if g is None:
        return None
    o = g["outcome"]
    try:
        heads = json.loads(o.get("headcount_json") or "null")
    except (TypeError, ValueError):
        heads = None
    return {"event": g["event"], "describe": describe(g["event"], tz=tz), "net": o.get("net"),
            "baseline": o.get("baseline"), "lift_pct": o.get("lift_pct"), "covers": o.get("covers"),
            "labor_pct": o.get("labor_pct"), "headcount": heads, "labor_hours": o.get("labor_hours")}


def context_for(restaurant_id, day, db_path=store.DB_PATH) -> list:
    """The followed events on a date, each {"event", "describe", "label",
    "effect", "last_like"} — what the report, the brief and Ask say."""
    day = _d(day).isoformat()
    followed = store.follows(restaurant_id, db_path=db_path)
    out = []
    tz = restaurant_clock(restaurant_id, db_path=db_path) if followed else None
    for e in store.events_for([f["series_id"] for f in followed], day, day, db_path=db_path):
        out.append({"event": e, "describe": describe(e, tz=tz), "label": label_for(e),
                    "effect": effect_for(restaurant_id, e, db_path=db_path),
                    "last_like": last_like(restaurant_id, e, db_path=db_path, tz=tz)})
    return out


def upcoming(restaurant_id, days=14, today=None, db_path=store.DB_PATH) -> list:
    """The followed events in the next `days` that will be played here —
    never a cancelled or postponed game, nor one this restaurant removed
    from its list (store.dismissed), as every other reader (re-audit
    P1-04) — each {"event", "describe", "label", "status", "effect"}.
    Without `today`, the restaurant's own local date (never the server's
    UTC one)."""
    tz = restaurant_clock(restaurant_id, db_path=db_path)
    if today:
        start = _d(today)
    else:
        start = store.local_today(tz)
    followed = store.follows(restaurant_id, db_path=db_path)
    rows = store.events_for([f["series_id"] for f in followed], start, start + timedelta(days=int(days)),
                            db_path=db_path)
    gone = store.dismissed(restaurant_id, db_path=db_path) if rows else set()
    return [{"event": e, "describe": describe(e, tz=tz), "label": label_for(e), "status": e.get("status"),
             "effect": effect_for(restaurant_id, e, db_path=db_path)} for e in rows
            if e.get("status") not in ("cancelled", "postponed") and e["id"] not in gone]


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
    tz = restaurant_clock(restaurant_id, db_path=db_path)
    return {"event": e, "describe": describe(e, tz=tz), "describe_short": describe(e, with_date=False, tz=tz),
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
                    "next": describe(nxt[0], tz=restaurant) if nxt else None, "season": season})
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
