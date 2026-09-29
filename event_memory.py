"""
event_memory — what each night teaches: how a game, a holiday, rain, the 1st
of the month or a campaign moved THIS restaurant's sales, measured after the
night is final and kept forever; and the restaurant's public history (its own
Google rating week by week, competitors arriving, leaving and moving) — memory
audit 9/29/26: event_memory, public_history (CROSS-4, QUALITY-6, LOOPS-4,
FORGET-9).

What was lost: every Cubs home game ran 22% above normal and the manager wrote
"Cubs game" in the close-out each time, and the next game was still forecast
as a plain Tuesday median; a delivery-heavy restaurant was told on every rainy
night that rain would lower sales, and the prediction kept grading
"incorrect"; the owner's "Homecoming +30%" was never checked. The weather
that actually happened was never kept, so the effect of rain could not be
learned at all.

THE RECORD — one row per night and label (event_outcomes), written by
record_night once the night is final (the nightly report's hook, and the
nightly job that re-reads the last RECENT_NIGHTS and backfills history):

  kind       event     what the owner listed for the date (demand_signals);
                       one fed by a campaign (source 'campaign') is a campaign
             holiday   the dining-holiday calendar (schedule_economics), never
                       one whose date is only approximated (the Super Bowl)
             influence the closer's own "what was going on" (close_outs)
             rain      observed rain during service (weather_daily, NWS
                       observations — never a forecast)
             campaign  a guest text aimed at that weekday, the first such
                       night after it went (guest_campaigns.target_day)
             payday    the 1st or the 15th of the month
  label      normalised (normalise_label: "Cubs home game" and "cubs game"
             are "cubs"); a free-text note naming two things is two labels
  lift_pct   the night's net against the median of the SAME weekday over the
             BASELINE_WEEKS before it, other flagged nights left out, on ONE
             basis (canonical_facts.net_series: the report's own net, an
             imported workbook, a POS total on the same basis) — measured,
             before and after, never proof
  covers, labor %, the owner's own guess (owner_lift_pct) beside it

THE SUMMARIES — event_effects, one row per label: n, median lift, the spread,
the direction, the last night — kept forever (a per-label summary outlives
any window). measured_effect() reads the record by label TOKENS, so "cubs"
counts "Cubs home game" and "Cubs vs Cardinals" alike.

WHO READS IT
  demand_signals.by_date   a listed event with no figure takes its label's
                           measured median once it has EFFECT_MIN_N nights
                           ("measured 3 times"); an owner's own figure stands,
                           with the measured record beside it
  demand.forecast_day      applies the measured effects known before the
                           night (effects_for_day) behind the sample floor
  dsr.predictions          states a rain or event effect only in this
                           restaurant's measured direction
  memory_context           memory_lines(): the measured effects for the dates
                           in play, fenced (labels are people's words)
  M3 / M4                  measured_effect(), night_facts()

Nothing here calls a model. Every read never raises into its caller.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH

log = logging.getLogger(__name__)


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


KINDS = ("event", "holiday", "influence", "rain", "campaign", "payday")
KNOWN_BEFORE = ("event", "holiday", "campaign", "payday")     # what a forecast may use
BASELINE_WEEKS = 8
BASELINE_MIN = 3            # same-weekday nights a lift needs under it
EFFECT_MIN_N = 3            # nights before a label's effect is applied ("measured 3 times")
# A holiday comes once a year: one measured night of it applies only when it
# moved sales past ordinary day-to-day variation (demand.OFF_DAY_PCT).
HOLIDAY_ONE_NIGHT_PCT = 20
EFFECT_FLOOR_PCT = 5        # a median effect smaller than this is "no measurable effect"
EFFECT_BOUNDS = (-50.0, 100.0)
RAIN_LABEL = "rain"
PAYDAY_LABELS = {1: ("1st month", "the 1st of the month"), 15: ("15th month", "the 15th of the month")}
CAMPAIGN_LABEL = ("guest text", "a guest text campaign")


# ── schema (at boot: models.init_db) ────────────────────────────────────────

def init_event_memory(db_path=DB_PATH):
    """The event record, its summaries, the observed weather and the public
    history — every one kept forever (none is in ops._RETENTION_DAYS): a
    night's lesson, a label's summary, a day's weather and a rating week are
    each a few hundred bytes, and their value is that they are old."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS event_outcomes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            business_date   TEXT    NOT NULL,
            weekday         TEXT    NOT NULL,
            kind            TEXT    NOT NULL,
            label           TEXT    NOT NULL,
            raw_label       TEXT,
            source          TEXT,
            net             REAL,
            baseline        REAL,
            baseline_n      INTEGER,
            lift_pct        REAL,
            basis           TEXT,
            net_source      TEXT,
            covers          INTEGER,
            labor_pct       REAL,
            owner_lift_pct  REAL,
            figure_kind     TEXT    NOT NULL DEFAULT 'measured',
            recorded_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, business_date, kind, label)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_outcomes_rid_date ON event_outcomes(restaurant_id, business_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_outcomes_rid_label ON event_outcomes(restaurant_id, label)")
        conn.execute("""CREATE TABLE IF NOT EXISTS event_effects (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            label           TEXT    NOT NULL,
            kind            TEXT    NOT NULL,
            display         TEXT,
            n               INTEGER NOT NULL,
            median_lift_pct REAL,
            low_lift_pct    REAL,
            high_lift_pct   REAL,
            direction       TEXT,
            first_date      TEXT,
            last_date       TEXT,
            weekdays_json   TEXT,
            owner_guesses   INTEGER NOT NULL DEFAULT 0,
            owner_median_pct REAL,
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, label)
        )""")
        # The weather that happened, per LOCAL day (NWS observations of the
        # nearest station): high/low, the station's measured precipitation,
        # whether it rained during service, and the station read. kind is
        # always 'measured' — a forecast is never stored here.
        conn.execute("""CREATE TABLE IF NOT EXISTS weather_daily (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            date            TEXT    NOT NULL,
            high_f          REAL,
            low_f           REAL,
            precip_in       REAL,
            rain            INTEGER,
            wet_hours       INTEGER,
            conditions      TEXT,
            station         TEXT,
            n_obs           INTEGER,
            source          TEXT    NOT NULL DEFAULT 'nws_observed',
            kind            TEXT    NOT NULL DEFAULT 'measured',
            fetched_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, date)
        )""")
        # public_history: the restaurant's own public rating, one row per ISO
        # week (the latest reading that week), and what its market did.
        conn.execute("""CREATE TABLE IF NOT EXISTS own_rating_history (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            week            TEXT    NOT NULL,
            rating          REAL    NOT NULL,
            review_count    INTEGER,
            source          TEXT,
            recorded_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, week)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS market_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            place_id        TEXT    NOT NULL,
            name            TEXT,
            kind            TEXT    NOT NULL,
            from_rating     REAL,
            to_rating       REAL,
            review_count    INTEGER,
            observed_on     TEXT    NOT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, place_id, kind, observed_on)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_market_events_rid ON market_events(restaurant_id, observed_on)")
        conn.execute("""CREATE TABLE IF NOT EXISTS competitor_rating_monthly (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            place_id        TEXT    NOT NULL,
            month           TEXT    NOT NULL,
            name            TEXT,
            rating          REAL,
            review_count    INTEGER,
            captured_at     TEXT,
            PRIMARY KEY (restaurant_id, place_id, month)
        )""")
        conn.commit()
    finally:
        conn.close()
    backfill_public_history(db_path)


def backfill_public_history(db_path=DB_PATH):
    """Once per restaurant, at boot: the public history from what is already
    on file, before retention takes it — each competitor check still in
    competitor_snapshots replayed in order through record_market_snapshot
    (so arrivals, departures and moves carry the day they were seen), and
    the restaurant's current Google rating as its first rating week. A
    restaurant that already has a history is left alone. Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            have_market = {r[0] for r in conn.execute("SELECT DISTINCT restaurant_id FROM competitor_rating_monthly")}
            have_own = {r[0] for r in conn.execute("SELECT DISTINCT restaurant_id FROM own_rating_history")}
            try:
                snaps = [dict(r) for r in conn.execute(
                    "SELECT restaurant_id, DATE(captured_at) AS d, place_id, name, rating, review_count "
                    "FROM competitor_snapshots ORDER BY restaurant_id, d, id").fetchall()]
            except Exception:
                snaps = []
            own = [dict(r) for r in conn.execute(
                "SELECT id, gbp_rating, gbp_review_count, gbp_rating_updated_at FROM restaurants "
                "WHERE gbp_rating IS NOT NULL AND gbp_rating > 0").fetchall()]
        finally:
            conn.close()
        runs = {}
        for r in snaps:
            if r["restaurant_id"] in have_market or not r["d"]:
                continue
            runs.setdefault((r["restaurant_id"], r["d"]), []).append(r)
        for (rid, d), comps in sorted(runs.items()):
            record_market_snapshot(rid, comps, at=date.fromisoformat(d), db_path=db_path)
        for r in own:
            if r["id"] in have_own:
                continue
            try:
                at = date.fromisoformat(str(r["gbp_rating_updated_at"] or "")[:10])
            except ValueError:
                at = None
            record_own_rating(r["id"], r["gbp_rating"], r["gbp_review_count"], source="on_file", at=at,
                              db_path=db_path)
    except Exception as e:
        log.warning("event_memory: public history not backfilled: %s", e)


# ── labels ─────────────────────────────────────────────────────────────────

_SPLIT_RE = re.compile(r"\s*(?:[,;+&/|]|\band\b|\bplus\b|\balso\b|\bwith\b)\s*", re.I)
# Words that say nothing about WHICH thing happened.
_STOP = {"game", "games", "home", "away", "vs", "v", "versus", "the", "a", "an", "at", "of", "in", "on", "for",
         "to", "our", "night", "nite", "day", "match", "event", "events", "big", "huge", "was", "were", "is",
         "there", "had", "lots", "lot", "busy", "slow", "crazy", "packed", "dead", "very", "really", "some",
         "tonight", "today", "yesterday", "going", "went", "happening", "nearby", "near", "downtown", "local"}
_SYNONYMS = {"rainy": "rain", "raining": "rain", "rained": "rain", "storm": "rain", "storms": "rain",
             "stormy": "rain", "thunderstorm": "rain", "thunderstorms": "rain", "showers": "rain",
             "downpour": "rain", "snowy": "snow", "snowing": "snow", "snowstorm": "snow", "bday": "birthday"}
_NOTHING = {"nothing", "none", "n/a", "na", "normal", "nothing special", "nothing listed", "no", "-", "regular",
            "none noted", "nothing going on", "nothing really", "same as usual", "usual"}


def normalise_label(text) -> str:
    """"Cubs home game!" -> "cubs": lower case, the words that name the
    thing (at most four), synonyms folded ("rainy" is "rain"), numbers and
    words that name nothing dropped. "" when nothing is left."""
    toks = []
    for w in re.findall(r"[a-z0-9']+", str(text or "").lower()):
        w = _SYNONYMS.get(w.strip("'"), w.strip("'"))
        if not w or w.isdigit() or w in _STOP or len(w) < 2:
            continue
        if w not in toks:
            toks.append(w)
    return " ".join(toks[:4])


def split_labels(text) -> list:
    """A note may name several things ("Cubs game + rain"): each is a label.
    A note that says nothing happened is no label."""
    raw = " ".join(str(text or "").split())
    if not raw or raw.lower().strip(" .!") in _NOTHING:
        return []
    out = []
    for part in _SPLIT_RE.split(raw):
        lab = normalise_label(part)
        if lab and lab not in out:
            out.append(lab)
    return out[:4]


def _tokens(label) -> set:
    return set(str(label or "").split())


def _iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]


def _as_date(d):
    return d if isinstance(d, date) and not isinstance(d, datetime) else date.fromisoformat(str(d)[:10])


# ── what is known about a date ─────────────────────────────────────────────

def _holiday(day):
    """(label, name) for a dining holiday on `day`, or None — never one the
    calendar only approximates (demand.APPROXIMATE_HOLIDAYS)."""
    try:
        import schedule_economics
        from demand import APPROXIMATE_HOLIDAYS, holiday_display_name
        name = schedule_economics._holiday_dates(day.year).get(day.isoformat())
    except Exception:
        return None
    if not name:
        return None
    name = holiday_display_name(name)
    if name in APPROXIMATE_HOLIDAYS:
        return None
    return normalise_label(name) or None, name


def _campaign_nights(conn, restaurant_id, start, end) -> dict:
    """{iso: [message]} — the first night on its target weekday after each
    guest text campaign that actually sent (guest_campaigns.target_day)."""
    out = {}
    try:
        rows = conn.execute(
            "SELECT target_day, COALESCE(completed_at, created_at) AS at, message FROM guest_campaigns "
            "WHERE restaurant_id=? AND target_day IS NOT NULL AND TRIM(target_day) != '' AND sent_count > 0 "
            "AND date(COALESCE(completed_at, created_at)) BETWEEN ? AND ?",
            (restaurant_id, (_as_date(start) - timedelta(days=7)).isoformat(), _iso(end))).fetchall()
    except Exception:
        return out
    names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    for r in rows:
        day = str(r["target_day"] or "").strip().capitalize()
        if day not in names:
            continue
        try:
            sent = _as_date(str(r["at"])[:10])
        except ValueError:
            continue
        ahead = (names.index(day) - sent.weekday()) % 7
        night = sent + timedelta(days=ahead)
        out.setdefault(night.isoformat(), []).append(str(r["message"] or "")[:80])
    return out


def flags_for(restaurant_id, days, db_path=None, known_before=False) -> dict:
    """{iso: [{"kind", "label", "raw", "source", "owner_lift_pct", "covers"}]}
    — everything flagged on each date. `known_before` keeps only what could
    be known BEFORE the night (listed events, holidays, campaigns, paydays):
    what a forecast may use. Never raises; an unreadable part is empty."""
    isos = sorted({_iso(d) for d in days if d})
    out = {d: [] for d in isos}
    if not isos:
        return out
    marks = ",".join("?" for _ in isos)
    conn = get_conn(db_path)
    try:
        try:
            for r in conn.execute(f"SELECT date, kind, label, covers, lift_pct, source FROM demand_signals "
                                  f"WHERE restaurant_id=? AND kind='event' AND date IN ({marks})",
                                  (restaurant_id, *isos)).fetchall():
                kind = "campaign" if str(r["source"] or "") == "campaign" else "event"
                for lab in split_labels(r["label"]):
                    out[str(r["date"])[:10]].append({"kind": kind, "label": lab, "raw": r["label"],
                                                     "source": "demand_signals", "owner_lift_pct": r["lift_pct"],
                                                     "covers": r["covers"]})
        except Exception as e:
            log.warning("event_memory: events unreadable rid=%s: %s", restaurant_id, e)
        for d, msgs in _campaign_nights(conn, restaurant_id, isos[0], isos[-1]).items():
            if d in out and not any(f["kind"] == "campaign" for f in out[d]):
                out[d].append({"kind": "campaign", "label": CAMPAIGN_LABEL[0], "raw": CAMPAIGN_LABEL[1],
                               "source": "guest_campaigns", "owner_lift_pct": None, "covers": None})
        if not known_before:
            try:
                for r in conn.execute(f"SELECT business_date, influence FROM close_outs WHERE restaurant_id=? "
                                      f"AND business_date IN ({marks})", (restaurant_id, *isos)).fetchall():
                    for lab in split_labels(r["influence"]):
                        out[str(r["business_date"])[:10]].append(
                            {"kind": "influence", "label": lab, "raw": r["influence"], "source": "close_out",
                             "owner_lift_pct": None, "covers": None})
            except Exception as e:
                log.warning("event_memory: close-outs unreadable rid=%s: %s", restaurant_id, e)
            try:
                for r in conn.execute(f"SELECT date, rain, conditions FROM weather_daily WHERE restaurant_id=? "
                                      f"AND rain=1 AND date IN ({marks})", (restaurant_id, *isos)).fetchall():
                    out[str(r["date"])[:10]].append({"kind": "rain", "label": RAIN_LABEL,
                                                     "raw": r["conditions"] or "Rain", "source": "weather_daily",
                                                     "owner_lift_pct": None, "covers": None})
            except Exception as e:
                log.warning("event_memory: weather unreadable rid=%s: %s", restaurant_id, e)
    finally:
        conn.close()
    for d in isos:
        day = _as_date(d)
        hol = _holiday(day)
        if hol and hol[0]:
            out[d].append({"kind": "holiday", "label": hol[0], "raw": hol[1], "source": "calendar",
                           "owner_lift_pct": None, "covers": None})
        if day.day in PAYDAY_LABELS:
            lab, text = PAYDAY_LABELS[day.day]
            out[d].append({"kind": "payday", "label": lab, "raw": text, "source": "calendar",
                           "owner_lift_pct": None, "covers": None})
    return out


# ── recording a night ──────────────────────────────────────────────────────

def _median(vals):
    s = sorted(vals)
    if not s:
        return None
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _night_extras(conn, restaurant_id, iso):
    covers = labor = None
    try:
        r = conn.execute("SELECT covers FROM covers_daily WHERE restaurant_id=? AND date=?", (restaurant_id, iso)).fetchone()
        covers = int(r["covers"]) if r and r["covers"] is not None else None
    except Exception:
        pass
    try:
        r = conn.execute("SELECT value FROM dsr_metrics WHERE restaurant_id=? AND business_date=? AND metric='labor.pct'",
                         (restaurant_id, iso)).fetchone()
        labor = float(r["value"]) if r and r["value"] is not None else None
        if labor is None:
            from canonical_facts import FINAL_SQL
            r = conn.execute(f"SELECT labor_pct FROM labor_daily_history WHERE restaurant_id=? AND date=? AND {FINAL_SQL}",
                             (restaurant_id, iso)).fetchone()
            labor = float(r["labor_pct"]) if r and r["labor_pct"] is not None else None
    except Exception:
        pass
    return covers, labor


def measure_night(restaurant_id, day, db_path=None, flags=None) -> dict:
    """{"net", "basis", "source", "baseline", "baseline_n", "lift_pct",
    "flags"} for one night, or {"reason"} when it cannot be measured: the
    night's canonical net against the median of the same weekday over the
    BASELINE_WEEKS before it, on the night's own basis, every other flagged
    night left out. Pure read."""
    import canonical_facts as cf
    day = _as_date(day)
    iso = day.isoformat()
    first = day - timedelta(weeks=BASELINE_WEEKS)
    same_days = [day - timedelta(weeks=k) for k in range(1, BASELINE_WEEKS + 1)]
    series = cf.net_series(restaurant_id, db_path=db_path, dates=[day] + same_days, pos=cf.POS_ALL)
    night = series.get(iso)
    if not night or not night.get("net") or night["net"] <= 0:
        return {"reason": "no final net sales for the night"}
    fl = flags if flags is not None else flags_for(restaurant_id, [day] + same_days, db_path=db_path)
    base = [x["net"] for d, x in series.items() if d != iso and x.get("basis") == night.get("basis")
            and x.get("net") and x["net"] > 0 and not (fl.get(d) or [])]
    if len(base) < BASELINE_MIN:
        return {"reason": f"only {len(base)} ordinary {day.strftime('%A')}s on the same basis in the "
                          f"{BASELINE_WEEKS} weeks before", "net": night["net"], "basis": night.get("basis"),
                "flags": fl.get(iso) or []}
    med = _median(base)
    return {"net": night["net"], "basis": night.get("basis"), "source": night.get("source"),
            "baseline": round(med, 2), "baseline_n": len(base), "since": first.isoformat(),
            "lift_pct": round((night["net"] / med - 1) * 100, 1), "flags": fl.get(iso) or []}


def record_night(restaurant_id, day, db_path=None) -> dict:
    """Measure one FINAL night and keep what it teaches: one event_outcomes
    row per label flagged on it (re-recording replaces the night's rows, so
    a late figure or a label added afterwards corrects it), then the
    summaries of every label it touched. {"recorded": n, "labels": [...]}
    or {"recorded": 0, "reason"}. Never raises."""
    try:
        day = _as_date(day)
        iso = day.isoformat()
        same_days = [day - timedelta(weeks=k) for k in range(1, BASELINE_WEEKS + 1)]
        fl = flags_for(restaurant_id, [day] + same_days, db_path=db_path)
        flags = fl.get(iso) or []
        conn = get_conn(db_path)
        try:
            before = {r["label"] for r in conn.execute(
                "SELECT label FROM event_outcomes WHERE restaurant_id=? AND business_date=?",
                (restaurant_id, iso)).fetchall()}
        finally:
            conn.close()
        if not flags and not before:
            return {"recorded": 0, "reason": "nothing flagged on the night"}
        m = measure_night(restaurant_id, day, db_path=db_path, flags=fl) if flags else {"reason": "no flags"}
        conn = get_conn(db_path)
        try:
            conn.execute("DELETE FROM event_outcomes WHERE restaurant_id=? AND business_date=?", (restaurant_id, iso))
            written = []
            if m.get("lift_pct") is not None:
                covers, labor = _night_extras(conn, restaurant_id, iso)
                seen = set()
                for f in flags:
                    key = (f["kind"], f["label"])
                    if key in seen:
                        continue
                    seen.add(key)
                    conn.execute(
                        "INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, "
                        "source, net, baseline, baseline_n, lift_pct, basis, net_source, covers, labor_pct, "
                        "owner_lift_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (restaurant_id, iso, day.strftime("%A"), f["kind"], f["label"], str(f.get("raw") or "")[:160],
                         f.get("source"), m["net"], m["baseline"], m["baseline_n"], m["lift_pct"], m.get("basis"),
                         m.get("source"), covers if covers is not None else f.get("covers"), labor,
                         f.get("owner_lift_pct")))
                    written.append(f["label"])
            conn.commit()
        finally:
            conn.close()
        refresh_effects(restaurant_id, set(written) | before, db_path=db_path)
        if not written:
            return {"recorded": 0, "reason": m.get("reason") or "not measurable", "labels": []}
        return {"recorded": len(written), "labels": written, "lift_pct": m["lift_pct"]}
    except Exception as e:
        log.warning("event_memory: night not recorded rid=%s day=%s: %s", restaurant_id, day, e)
        return {"recorded": 0, "reason": f"error: {e}"}


def _display(rows):
    """The words a label is shown in: the newest raw wording the owner, the
    closer or the calendar used."""
    rows = sorted(rows, key=lambda r: r["business_date"])
    for r in reversed(rows):
        if r.get("raw_label"):
            return str(r["raw_label"])[:80]
    return rows[-1]["label"] if rows else None


def _summary(rows) -> dict | None:
    """One label's summary over its nights (one per date)."""
    by_date = {}
    for r in rows:
        if r.get("lift_pct") is None:
            continue
        by_date.setdefault(r["business_date"], r)
    nights = sorted(by_date.values(), key=lambda r: r["business_date"])
    if not nights:
        return None
    lifts = sorted(float(r["lift_pct"]) for r in nights)
    med = _median(lifts)
    guesses = [float(r["owner_lift_pct"]) for r in nights if r.get("owner_lift_pct") is not None]
    wd = {}
    for r in nights:
        wd[r["weekday"]] = wd.get(r["weekday"], 0) + 1
    kinds = [r["kind"] for r in nights]
    return {"n": len(nights), "median_lift_pct": round(med, 1), "low_lift_pct": lifts[0], "high_lift_pct": lifts[-1],
            "direction": ("up" if med >= EFFECT_FLOOR_PCT else "down" if med <= -EFFECT_FLOOR_PCT else "none"),
            "first_date": nights[0]["business_date"], "last_date": nights[-1]["business_date"],
            "weekdays": wd, "kind": max(set(kinds), key=kinds.count), "display": _display(nights),
            "owner_guesses": len(guesses), "owner_median_pct": round(_median(guesses), 1) if guesses else None,
            "basis": sorted({r.get("basis") or "" for r in nights} - {""})}


def refresh_effects(restaurant_id, labels, db_path=None):
    """Recompute the per-label summaries (event_effects) for `labels` from
    the record. A label with no measured night left is removed from the
    summaries — its record went with it. Never raises."""
    labels = {l for l in (labels or ()) if l}
    if not labels:
        return
    try:
        conn = get_conn(db_path)
        try:
            for lab in labels:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM event_outcomes WHERE restaurant_id=? AND label=?", (restaurant_id, lab)).fetchall()]
                s = _summary(rows)
                if not s:
                    conn.execute("DELETE FROM event_effects WHERE restaurant_id=? AND label=?", (restaurant_id, lab))
                    continue
                conn.execute(
                    "INSERT INTO event_effects (restaurant_id, label, kind, display, n, median_lift_pct, low_lift_pct, "
                    "high_lift_pct, direction, first_date, last_date, weekdays_json, owner_guesses, owner_median_pct, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                    "ON CONFLICT(restaurant_id, label) DO UPDATE SET kind=excluded.kind, display=excluded.display, "
                    "n=excluded.n, median_lift_pct=excluded.median_lift_pct, low_lift_pct=excluded.low_lift_pct, "
                    "high_lift_pct=excluded.high_lift_pct, direction=excluded.direction, "
                    "first_date=excluded.first_date, last_date=excluded.last_date, "
                    "weekdays_json=excluded.weekdays_json, owner_guesses=excluded.owner_guesses, "
                    "owner_median_pct=excluded.owner_median_pct, updated_at=excluded.updated_at",
                    (restaurant_id, lab, s["kind"], s["display"], s["n"], s["median_lift_pct"], s["low_lift_pct"],
                     s["high_lift_pct"], s["direction"], s["first_date"], s["last_date"], json.dumps(s["weekdays"]),
                     s["owner_guesses"], s["owner_median_pct"]))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("event_memory: summaries not refreshed rid=%s: %s", restaurant_id, e)


# ── reading ────────────────────────────────────────────────────────────────

def measured_effect(restaurant_id, label, db_path=None):
    """This restaurant's measured effect of a recurring label ("football
    sunday", "rain", "1st of month") -> {"median_lift_pct": float, "n": int,
    "last": date} once it has a sample, else None.

    Matched by label TOKENS over the record, one night per date: "cubs"
    counts every night whose label names the Cubs ("cubs", "cubs cardinals").
    Also carries "label", "display", "kind", "low_lift_pct",
    "high_lift_pct", "direction" (up | down | none, past EFFECT_FLOOR_PCT),
    "applies" (n clears the floor — EFFECT_MIN_N, or a holiday's one night
    past HOLIDAY_ONE_NIGHT_PCT), "owner_median_pct" (what the owner guessed
    on those nights, when they did) and "basis" — the sentence a surface
    says. Never raises."""
    norm = normalise_label(label)
    want = _tokens(norm)
    if not want:
        return None
    probe = max(want, key=len)
    try:
        conn = get_conn(db_path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM event_outcomes WHERE restaurant_id=? AND lift_pct IS NOT NULL AND label LIKE ?",
                (restaurant_id, f"%{probe}%")).fetchall()]
        finally:
            conn.close()
    except Exception:
        return None
    hits = [r for r in rows if want <= _tokens(r["label"])]
    s = _summary(hits)
    if not s:
        return None
    n, med = s["n"], s["median_lift_pct"]
    applies = n >= EFFECT_MIN_N or (s["kind"] == "holiday" and (n >= 2 or abs(med) >= HOLIDAY_ONE_NIGHT_PCT))
    from time_utils import mdy
    word = "above" if med >= 0 else "below"
    basis = (f"{s['display'] or label}: nights here ran a median {abs(med):.0f}% {word} a typical same weekday "
             f"(measured {n} time{'s' if n != 1 else ''}, last {mdy(s['last_date'])}) — before and after, not proof")
    return {"median_lift_pct": med, "n": n, "last": date.fromisoformat(s["last_date"]),
            "label": norm,
            "display": s["display"], "kind": s["kind"], "low_lift_pct": s["low_lift_pct"],
            "high_lift_pct": s["high_lift_pct"], "direction": s["direction"], "applies": bool(applies),
            "owner_median_pct": s["owner_median_pct"], "basis": basis}


def effects_for_day(restaurant_id, day, db_path=None, flags=None) -> dict | None:
    """The measured effects a forecast of `day` may apply: for each kind known
    BEFORE the night (a listed event, the holiday, a campaign, the 1st or
    15th), the label on the day whose measured effect clears the floor
    (measured_effect's `applies`) and moves sales past EFFECT_FLOOR_PCT —
    the most-measured one per kind — combined multiplicatively and bounded
    to EFFECT_BOUNDS. {"pct", "applied": [{"label", "display", "kind",
    "lift_pct", "n"}], "basis"} or None when nothing applies. Never raises."""
    try:
        day = _as_date(day)
        fl = (flags if flags is not None else flags_for(restaurant_id, [day], db_path=db_path,
                                                        known_before=True)).get(day.isoformat()) or []
        best = {}
        for f in fl:
            if f["kind"] not in KNOWN_BEFORE:
                continue
            e = measured_effect(restaurant_id, f["label"], db_path=db_path)
            if not e or not e["applies"] or abs(e["median_lift_pct"]) < EFFECT_FLOOR_PCT:
                continue
            cur = best.get(f["kind"])
            if cur is None or (e["n"], abs(e["median_lift_pct"])) > (cur["n"], abs(cur["lift_pct"])):
                best[f["kind"]] = {"label": f["label"], "display": e["display"] or f.get("raw"), "kind": f["kind"],
                                   "lift_pct": e["median_lift_pct"], "n": e["n"], "basis": e["basis"]}
        if not best:
            return None
        factor = 1.0
        for e in best.values():
            factor *= 1.0 + e["lift_pct"] / 100.0
        pct = max(EFFECT_BOUNDS[0], min(EFFECT_BOUNDS[1], round((factor - 1.0) * 100.0, 1)))
        applied = sorted(best.values(), key=lambda e: -abs(e["lift_pct"]))
        return {"pct": pct, "applied": applied,
                "basis": "; ".join(e["basis"] for e in applied)}
    except Exception as e:
        log.warning("event_memory: effects unreadable rid=%s day=%s: %s", restaurant_id, day, e)
        return None


def night_facts(restaurant_id, day, db_path=None):
    """What is known about one date: [{"kind", "label", "measured_lift_pct",
    "n"}] (events, holidays, influence notes, weather, campaign targets).

    Each fact carries its label's measured effect here ("measured_lift_pct",
    the median over "n" nights, None below one night), "display" (the words
    it was written in), "source", and — for a night already recorded —
    "this_night_lift_pct". A past date's weather is what was OBSERVED
    (weather_daily: "weather_observed" with high, low and whether it rained
    in service); a future date carries no weather here (the forecast is
    weather.forecast_for_day's). Never raises."""
    try:
        day = _as_date(day)
        iso = day.isoformat()
        facts = []
        fl = flags_for(restaurant_id, [day], db_path=db_path).get(iso) or []
        conn = get_conn(db_path)
        try:
            recorded = {(r["kind"], r["label"]): r["lift_pct"] for r in conn.execute(
                "SELECT kind, label, lift_pct FROM event_outcomes WHERE restaurant_id=? AND business_date=?",
                (restaurant_id, iso)).fetchall()}
            w = conn.execute("SELECT * FROM weather_daily WHERE restaurant_id=? AND date=?", (restaurant_id, iso)).fetchone()
        finally:
            conn.close()
        seen = set()
        for f in fl:
            if (f["kind"], f["label"]) in seen:
                continue
            seen.add((f["kind"], f["label"]))
            e = measured_effect(restaurant_id, f["label"], db_path=db_path)
            facts.append({"kind": f["kind"], "label": f["label"], "display": f.get("raw") or f["label"],
                          "source": f.get("source"),
                          "measured_lift_pct": e["median_lift_pct"] if e else None, "n": e["n"] if e else 0,
                          "applies": bool(e and e["applies"]),
                          "owner_lift_pct": f.get("owner_lift_pct"),
                          "this_night_lift_pct": recorded.get((f["kind"], f["label"]))})
        if w is not None:
            facts.append({"kind": "weather_observed", "label": RAIN_LABEL if w["rain"] == 1 else "dry",
                          "display": w["conditions"] or ("Rain" if w["rain"] == 1 else "No rain"),
                          "source": "weather_daily", "high_f": w["high_f"], "low_f": w["low_f"],
                          "precip_in": w["precip_in"], "rained_in_service": w["rain"],
                          "measured_lift_pct": None, "n": 0, "applies": False})
        return facts
    except Exception as e:
        log.warning("event_memory: night facts unreadable rid=%s day=%s: %s", restaurant_id, day, e)
        return []


# Which dates each surface's memory is about (memory_context surfaces).
_SURFACE_DAYS = {"schedule": (0, 13), "labor_read": (0, 6), "weekly_plan": (0, 6), "marketing": (0, 13),
                 "brief": (0, 0), "dsr_narrative": (-1, 1), "ask": (0, 6)}
MEMORY_TOP_LABELS = 3


def memory_lines(req):
    """memory_context provider: measured event effects for the dates in play.

    The dates are req.subjects of the form "date:YYYY-MM-DD" when the caller
    names them, else the surface's own window (_SURFACE_DAYS) from req.now's
    date. One line per flagged date whose label has a measured effect here,
    and — for Ask, the schedule and the weekly plan — the strongest recurring
    effects on record (MEMORY_TOP_LABELS, applied ones only). Labels are
    people's words (a closer's note, the owner's event name), so every line
    is fenced (trusted False); dates are M/D/YY through memory_context."""
    from time_utils import mdy
    rid = req.restaurant_id
    db_path = getattr(req, "db_path", None)
    now = getattr(req, "now", None) or datetime.now()
    today = now.date() if isinstance(now, datetime) else _as_date(now)
    days = []
    for s in getattr(req, "subjects", ()) or ():
        if str(s).startswith("date:"):
            try:
                days.append(_as_date(str(s)[5:]))
            except ValueError:
                continue
    if not days:
        lo, hi = _SURFACE_DAYS.get(getattr(req, "surface", ""), (0, 6))
        days = [today + timedelta(days=k) for k in range(lo, hi + 1)]
    out, named = [], set()
    # A night already past carries what happened on it (the closer's note,
    # the rain observed); today and ahead only what is known before.
    fl = flags_for(rid, [d for d in days if d >= today], db_path=db_path, known_before=True)
    fl.update(flags_for(rid, [d for d in days if d < today], db_path=db_path))
    for d in sorted(fl):
        for f in fl[d]:
            e = measured_effect(rid, f["label"], db_path=db_path)
            if not e:
                continue
            named.add(e["label"])
            wd = _as_date(d).strftime("%A")
            word = "above" if e["median_lift_pct"] >= 0 else "below"
            guess = ""
            if f.get("owner_lift_pct") is not None:
                guess = f"; the owner listed it at {float(f['owner_lift_pct']):+.0f}%"
            floor = "" if e["applies"] else f" — fewer than {EFFECT_MIN_N} nights, not yet a pattern"
            out.append({"text": (f"{wd} {mdy(d)}: {f.get('raw') or f['label']} — nights like it here ran a median "
                                 f"{abs(e['median_lift_pct']):.0f}% {word} a typical same weekday (measured {e['n']} "
                                 f"time{'s' if e['n'] != 1 else ''}, last {mdy(e['last'])}; before and after, not "
                                 f"proof){guess}{floor}."),
                        "date": d, "source": "system", "subject": f"event:{e['label']}",
                        "weight": 2.0 + min(e["n"], 10) / 10.0, "trusted": False})
    if getattr(req, "surface", "") in ("ask", "schedule", "weekly_plan", "marketing"):
        try:
            conn = get_conn(db_path)
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT label, display, n, median_lift_pct, last_date FROM event_effects WHERE restaurant_id=? "
                    "AND n >= ? AND ABS(median_lift_pct) >= ? ORDER BY n DESC, ABS(median_lift_pct) DESC LIMIT ?",
                    (rid, EFFECT_MIN_N, EFFECT_FLOOR_PCT, MEMORY_TOP_LABELS + len(named))).fetchall()]
            finally:
                conn.close()
        except Exception:
            rows = []
        for r in [r for r in rows if r["label"] not in named][:MEMORY_TOP_LABELS]:
            word = "above" if r["median_lift_pct"] >= 0 else "below"
            out.append({"text": (f"Recurring here: {r['display'] or r['label']} — nights ran a median "
                                 f"{abs(r['median_lift_pct']):.0f}% {word} a typical same weekday (measured {r['n']} "
                                 f"times; before and after, not proof)."),
                        "date": r["last_date"], "source": "system", "subject": f"event:{r['label']}",
                        "weight": 1.0 + min(r["n"], 10) / 10.0, "trusted": False})
    return out


# ── the weather that happened ──────────────────────────────────────────────

def _last_station(conn, restaurant_id):
    try:
        r = conn.execute("SELECT station FROM weather_daily WHERE restaurant_id=? AND station IS NOT NULL "
                         "ORDER BY date DESC LIMIT 1", (restaurant_id,)).fetchone()
        return r["station"] if r else None
    except Exception:
        return None


def record_weather(restaurant, days, db_path=None) -> dict:
    """Capture the observed weather of each LOCAL day in `days` that is not
    on file yet (weather_daily), from the nearest NWS station — one station
    lookup per restaurant ever (kept on its rows), one observations call per
    pass. Only a restaurant whose coordinates are already known is read:
    this never geocodes (a billed Places call). {"recorded", "skipped",
    "failed", "reason"}. Never raises."""
    rid = getattr(restaurant, "id", None)
    out = {"recorded": 0, "skipped": 0, "failed": 0, "reason": None}
    lat, lon = getattr(restaurant, "latitude", None), getattr(restaurant, "longitude", None)
    if rid is None or lat is None or lon is None:
        out["reason"] = "no coordinates on file"
        return out
    try:
        import weather
        from time_utils import restaurant_tz, service_window
        tz = restaurant_tz(restaurant)
        conn = get_conn(db_path)
        try:
            have = {str(r["date"])[:10] for r in conn.execute(
                "SELECT date FROM weather_daily WHERE restaurant_id=?", (rid,)).fetchall()}
            station = _last_station(conn, rid)
        finally:
            conn.close()
        todo = sorted({_as_date(d) for d in days} - {date.fromisoformat(h) for h in have})
        if not todo:
            out["skipped"] = len(days)
            return out
        if not station:
            station, failure = weather.observation_station(lat, lon)
            if not station:
                out["failed"], out["reason"] = len(todo), f"no observation station ({failure})"
                return out
        start = datetime.combine(todo[0], datetime.min.time()).replace(tzinfo=tz)
        end = datetime.combine(todo[-1] + timedelta(days=2), datetime.min.time()).replace(tzinfo=tz)
        obs, failure = weather.fetch_observations(station, start, end)
        if failure:
            out["failed"], out["reason"] = len(todo), f"observations unavailable ({failure})"
            return out
        conn = get_conn(db_path)
        try:
            for d in todo:
                s = weather.summarise_day(obs, tz, d, service=service_window(restaurant, d))
                if s is None:
                    out["skipped"] += 1
                    continue
                conn.execute(
                    "INSERT INTO weather_daily (restaurant_id, date, high_f, low_f, precip_in, rain, wet_hours, "
                    "conditions, station, n_obs) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, date) "
                    "DO UPDATE SET high_f=excluded.high_f, low_f=excluded.low_f, precip_in=excluded.precip_in, "
                    "rain=excluded.rain, wet_hours=excluded.wet_hours, conditions=excluded.conditions, "
                    "station=excluded.station, n_obs=excluded.n_obs, fetched_at=datetime('now')",
                    (rid, d.isoformat(), s["high_f"], s["low_f"], s["precip_in"], s["rain"], s["wet_hours"],
                     s["conditions"], station, s["n_obs"]))
                out["recorded"] += 1
            conn.commit()
        finally:
            conn.close()
        return out
    except Exception as e:
        log.warning("event_memory: weather not recorded rid=%s: %s", rid, e)
        out["failed"], out["reason"] = len(days or ()), f"error: {e}"
        return out


def observed_weather(restaurant_id, day, db_path=None):
    """The observed weather row for one local day (weather_daily), or None."""
    try:
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT * FROM weather_daily WHERE restaurant_id=? AND date=?",
                             (restaurant_id, _iso(day))).fetchone()
        finally:
            conn.close()
        return dict(r) if r else None
    except Exception:
        return None


# ── the nightly job ────────────────────────────────────────────────────────

RECENT_NIGHTS = 7             # re-recorded every pass: late figures and labels correct them
WEATHER_DAYS = 6              # NWS serves about a week of observations
BACKFILL_DAYS = 400           # how far back history is read once
BACKFILL_NIGHTS_PER_RUN = 120
EVENT_MEMORY_MAX_SECONDS = 20 * 60
EVENT_MEMORY_CURSOR_KEY = "event_memory_cursor"
BACKFILL_CURSOR_PREFIX = "event_memory_backfill:"


def _backfill_cursor(restaurant_id, db_path=None, value=None):
    """The oldest night the history backfill has reached for a restaurant
    (job_cursors), read, or written when `value` is given."""
    key = f"{BACKFILL_CURSOR_PREFIX}{restaurant_id}"
    conn = get_conn(db_path)
    try:
        if value is None:
            r = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
            return r["value"] if r else None
        conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                     (key, str(value)))
        conn.commit()
        return value
    finally:
        conn.close()


def reset_backfill(restaurant_id, db_path=None):
    """Read the restaurant's history again on the next pass — after an
    import of past DSR workbooks brought nights the backfill never saw.
    Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("DELETE FROM job_cursors WHERE key=?", (f"{BACKFILL_CURSOR_PREFIX}{restaurant_id}",))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("event_memory: backfill not reset rid=%s: %s", restaurant_id, e)


def _history_nights(restaurant_id, start, end, db_path=None) -> list:
    """The nights in [start, end] with a final net on file, newest first."""
    import canonical_facts as cf
    return sorted(cf.sales_history(restaurant_id, start, end, db_path=db_path), reverse=True)


def remember_restaurant(restaurant, today=None, db_path=None) -> dict:
    """One restaurant's pass: the observed weather of the last WEATHER_DAYS,
    the last RECENT_NIGHTS re-recorded, then up to BACKFILL_NIGHTS_PER_RUN
    older nights of history (holidays, paydays, listed events and close-out
    notes it already holds), resuming where the last pass stopped."""
    from time_utils import restaurant_now
    rid = restaurant.id
    today = today or restaurant_now(restaurant).date()
    yesterday = today - timedelta(days=1)
    w = record_weather(restaurant, [yesterday - timedelta(days=k) for k in range(WEATHER_DAYS)], db_path=db_path)
    recorded = 0
    for k in range(RECENT_NIGHTS):
        recorded += record_night(rid, yesterday - timedelta(days=k), db_path=db_path).get("recorded", 0)
    reached = _backfill_cursor(rid, db_path=db_path)
    floor = today - timedelta(days=BACKFILL_DAYS)
    top = (date.fromisoformat(reached) - timedelta(days=1)) if reached else (yesterday - timedelta(days=RECENT_NIGHTS))
    backfilled = 0
    if top >= floor:
        nights = _history_nights(rid, floor, top, db_path=db_path)[:BACKFILL_NIGHTS_PER_RUN]
        for d in nights:
            recorded += record_night(rid, d, db_path=db_path).get("recorded", 0)
            backfilled += 1
        done = len(nights) < BACKFILL_NIGHTS_PER_RUN
        _backfill_cursor(rid, db_path=db_path, value=(floor.isoformat() if done or not nights else nights[-1]))
    return {"weather": w, "recorded": recorded, "backfilled": backfilled}


def run_event_memory(db_path=None, now=None) -> dict:
    """Daily (the scheduler, after the nightly chain) — each learning-eligible
    restaurant in service: capture yesterday's observed weather, record what
    the last nights taught, and backfill history, through
    scheduler.resumable_sweep (a worker, a wall-clock bound and a cursor in
    job_cursors). Returns {attempted, ok, failed, skipped, hit_bound}."""
    import threading
    counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False,
              "nights_recorded": 0, "weather_days": 0}
    lock = threading.Lock()
    try:
        from models import in_service_sql, learning_eligible
        conn = get_conn(db_path)
        try:
            rows = conn.execute("SELECT id FROM restaurants WHERE " + in_service_sql()).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning("event_memory: restaurants unreadable: %s", e)
        raise
    ids = []
    for r in rows:
        try:
            rest = _models_mod.get_restaurant(r["id"])
        except Exception:
            rest = None
        if rest is not None and learning_eligible(rest):
            ids.append(r["id"])
        else:
            counts["skipped"] += 1

    def _one(rid):
        rest = _models_mod.get_restaurant(rid)
        with lock:
            counts["attempted"] += 1
        try:
            got = remember_restaurant(rest, db_path=db_path)
            with lock:
                counts["ok"] += 1
                counts["nights_recorded"] += got.get("recorded", 0)
                counts["weather_days"] += (got.get("weather") or {}).get("recorded", 0)
        except Exception as e:
            with lock:
                counts["failed"] += 1
            import ops
            ops.capture(e, job="event_memory", context=f"restaurant_id={rid}")

    if ids:
        import scheduler
        _done, hit = scheduler.resumable_sweep(EVENT_MEMORY_CURSOR_KEY, sorted(ids), _one, EVENT_MEMORY_MAX_SECONDS,
                                               workers=1, job="event_memory")
        counts["hit_bound"] = bool(hit)
    return counts


# ── public history (public_history) ────────────────────────────────────────

# A competitor's rating moving this much (★) since its last marked reading is
# a market event; the monthly series keeps the rest.
MARKET_MOVE_STARS = 0.2


def _iso_week(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def record_own_rating(restaurant_id, rating, review_count=None, source=None, at=None, db_path=None):
    """Keep this week's reading of the restaurant's OWN public Google rating
    (own_rating_history, one row per ISO week, the latest reading of the
    week). restaurants.gbp_rating is overwritten in place, so "your rating
    went from 4.3 to 4.6 since you joined" could not be proved. Called by
    every writer of gbp_rating (competitor._remember_own_listing,
    gmb.fetch_location_rating). Never raises."""
    try:
        if rating is None or float(rating) <= 0:
            return
        when = at or datetime.now()
        day = when.date() if isinstance(when, datetime) else _as_date(when)
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO own_rating_history (restaurant_id, week, rating, review_count, source, recorded_at) "
                         "VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, week) DO UPDATE SET "
                         "rating=excluded.rating, review_count=COALESCE(excluded.review_count, own_rating_history.review_count), "
                         "source=excluded.source, recorded_at=excluded.recorded_at",
                         (restaurant_id, _iso_week(day), round(float(rating), 2),
                          int(review_count) if isinstance(review_count, (int, float)) else None, source))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("event_memory: own rating not kept rid=%s: %s", restaurant_id, e)


def own_rating_trajectory(restaurant_id, db_path=None) -> dict:
    """{"available", "first": {"week", "rating", "review_count"}, "latest":
    {...}, "change", "weeks", "series": [...]} — the restaurant's own public
    rating over time, oldest first. Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT week, rating, review_count, source FROM own_rating_history WHERE restaurant_id=? ORDER BY week",
                (restaurant_id,)).fetchall()]
        finally:
            conn.close()
    except Exception:
        rows = []
    if len(rows) < 2:
        return {"available": False, "weeks": len(rows), "series": rows,
                "reason": "the rating needs two weeks on file before it has a trajectory"}
    return {"available": True, "first": rows[0], "latest": rows[-1], "weeks": len(rows),
            "change": round(rows[-1]["rating"] - rows[0]["rating"], 2), "series": rows}


def record_market_snapshot(restaurant_id, competitors, at=None, db_path=None) -> list:
    """After a competitor check, keep what the market did: the monthly rating
    series (competitor_rating_monthly, the latest reading each month) and a
    market event (market_events) when a competitor ARRIVED in the set (after
    the first check — the set a restaurant starts with is not arrivals), is
    GONE from it, or its rating moved MARKET_MOVE_STARS or more since the
    reading its last event was marked on (else its first reading), so a slow
    slide is caught once it adds up. competitor_snapshots are pruned at 365
    days and read over 60 at most, so "Bella's opened across the street in
    March" was gone a year later. Returns the events written. Never raises."""
    written = []
    try:
        when = at or datetime.now()
        day = (when.date() if isinstance(when, datetime) else _as_date(when)).isoformat()
        month = day[:7]
        now_set = {c.get("place_id"): c for c in competitors or [] if c.get("place_id")}
        conn = get_conn(db_path)
        try:
            months = [dict(r) for r in conn.execute(
                "SELECT place_id, name, rating, review_count, month FROM competitor_rating_monthly "
                "WHERE restaurant_id=? ORDER BY month", (restaurant_id,)).fetchall()]
            first_run = not months
            first_rating, latest = {}, {}
            for r in months:
                if r["rating"] is not None:
                    first_rating.setdefault(r["place_id"], float(r["rating"]))
                latest[r["place_id"]] = r
            anchors, state = {}, {}
            for r in conn.execute("SELECT place_id, kind, to_rating FROM market_events WHERE restaurant_id=? "
                                  "ORDER BY observed_on, id", (restaurant_id,)).fetchall():
                state[r["place_id"]] = r["kind"]
                if r["kind"] != "gone" and r["to_rating"] is not None:
                    anchors[r["place_id"]] = float(r["to_rating"])
            gone = {pid for pid, k in state.items() if k == "gone"}

            def _event(pid, name, kind, frm, to, count):
                cur = conn.execute("INSERT OR IGNORE INTO market_events (restaurant_id, place_id, name, kind, "
                                   "from_rating, to_rating, review_count, observed_on) VALUES (?,?,?,?,?,?,?,?)",
                                   (restaurant_id, pid, name, kind, frm, to, count, day))
                if cur.rowcount:
                    written.append({"place_id": pid, "name": name, "kind": kind, "from_rating": frm,
                                    "to_rating": to, "observed_on": day})
            for pid, c in now_set.items():
                rating = c.get("rating")
                rating = float(rating) if isinstance(rating, (int, float)) and rating > 0 else None
                count = int(c["review_count"]) if isinstance(c.get("review_count"), (int, float)) else None
                if not first_run and (pid not in latest or pid in gone):
                    _event(pid, c.get("name"), "arrived", None, rating, count)
                else:
                    anchor = anchors.get(pid, first_rating.get(pid))
                    if rating is not None and anchor is not None \
                            and abs(rating - anchor) >= MARKET_MOVE_STARS - 1e-9:
                        _event(pid, c.get("name"), "rating_up" if rating > anchor else "rating_down",
                               round(anchor, 2), rating, count)
                conn.execute("INSERT INTO competitor_rating_monthly (restaurant_id, place_id, month, name, rating, "
                             "review_count, captured_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, place_id, "
                             "month) DO UPDATE SET name=excluded.name, rating=COALESCE(excluded.rating, "
                             "competitor_rating_monthly.rating), review_count=COALESCE(excluded.review_count, "
                             "competitor_rating_monthly.review_count), captured_at=excluded.captured_at",
                             (restaurant_id, pid, month, c.get("name"), rating, count, day))
            if not first_run:
                for pid, r in latest.items():
                    if pid not in now_set and pid not in gone:
                        _event(pid, r.get("name"), "gone", r.get("rating"), None, r.get("review_count"))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("event_memory: market snapshot not kept rid=%s: %s", restaurant_id, e)
    return written


def market_history(restaurant_id, since=None, db_path=None) -> list:
    """[{"place_id", "name", "kind", "from_rating", "to_rating",
    "observed_on"}] — the market's events, newest first, kept forever."""
    try:
        conn = get_conn(db_path)
        try:
            args = [restaurant_id]
            sql = "SELECT place_id, name, kind, from_rating, to_rating, review_count, observed_on FROM market_events " \
                  "WHERE restaurant_id=?"
            if since:
                sql += " AND observed_on >= ?"
                args.append(_iso(since))
            return [dict(r) for r in conn.execute(sql + " ORDER BY observed_on DESC, id DESC", args).fetchall()]
        finally:
            conn.close()
    except Exception:
        return []
