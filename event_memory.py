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
# A holiday is keyed by its calendar identity, never by its words (memory
# re-audit 9/29/26, QUALITY-5): "New Year's Day" normalised to "new year's",
# a token subset of "new year's eve", so New Year's Day read New Year's Eve's
# nights (and Christmas Day read Christmas Eve's). holiday_key("New Year's
# Day") is "holiday:new_years_day", matched exactly.
HOLIDAY_PREFIX = "holiday:"
# Event effects age (QUALITY-21): a night counts half for every
# EFFECT_HALF_LIFE_YEARS it is older than the label's newest night, in whole
# years — nights inside a year of the newest count alike, so a recent record
# is its plain median. When the last DRIFT_RECENT nights all sit outside the
# spread of the older ones (and at least DRIFT_RECENT older nights exist),
# the record has moved: the recent median is the figure, and the sentence
# says "was +25%, the last 3 nights +8%".
EFFECT_HALF_LIFE_YEARS = 2.0
DRIFT_RECENT = 3


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
        # Who worked the night (owner, 9/30/26: "does it learn ... how many
        # servers were on that day?"): the people punched in by role, their
        # count and their hours - so a rain night that sold 20% under can be
        # read beside the floor that stood for it.
        _have = {r[1] for r in conn.execute("PRAGMA table_info(event_outcomes)").fetchall()}
        for _col, _typ in (("headcount", "INTEGER"), ("headcount_json", "TEXT"), ("labor_hours", "REAL")):
            if _col not in _have:
                conn.execute(f"ALTER TABLE event_outcomes ADD COLUMN {_col} {_typ}")
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
        # A night that carried more than one thing (a holiday AND the owner's
        # "Mother's Day brunch", a game on a rainy payday) is `confounded`:
        # its one lift belongs to no single label, so a label is measured on
        # its nights alone where it has enough of them, and `co_labels` (JSON)
        # names what else was on the night, for display (QUALITY-4). The
        # same two facts ride on each label's summary (event_effects), with
        # `drift_json` when its recent nights moved (QUALITY-21).
        for table, cols in (("event_outcomes", (("confounded", "INTEGER NOT NULL DEFAULT 0"),
                                                ("co_labels", "TEXT"))),
                            ("event_effects", (("confounded", "INTEGER NOT NULL DEFAULT 0"),
                                               ("drift_json", "TEXT")))):
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col, typ in cols:
                if col not in have:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    except Exception as e:
                        if "duplicate column" not in str(e).lower():
                            raise
        conn.commit()
    finally:
        conn.close()
    backfill_public_history(db_path)
    retract_churn_events(db_path)
    rekey_record(db_path)


# The day record_night started marking each night's confounding itself
# (QUALITY-4, 9b6ffd62, 9/29/26): rows recorded since carry their own marks.
CONFOUNDING_MARKED_FROM = "2026-09-30"


def rekey_record(db_path=DB_PATH) -> dict:
    """At boot, idempotent: bring the record kept before the re-audit fix
    round (9/29/26) to its rules — every holiday row re-keyed by its date's
    calendar identity (holiday_key, QUALITY-5), every night's rows marked
    `confounded` when the night carried more than one thing (QUALITY-4) —
    and the summaries of every label it touched recomputed. {"rekeyed",
    "marked"}. Never raises."""
    out = {"rekeyed": 0, "marked": 0}
    try:
        conn = get_conn(db_path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, restaurant_id, business_date, kind, label, confounded, co_labels, recorded_at "
                "FROM event_outcomes ORDER BY restaurant_id, business_date, id").fetchall()]
        finally:
            conn.close()
        if not rows:
            return out
        touched = {}
        updates = []
        for r in rows:
            if r["kind"] != "holiday" or is_holiday_key(r["label"]):
                continue
            try:
                hol = _holiday(_as_date(r["business_date"]))
            except ValueError:
                hol = None
            if hol and hol[0] and hol[0] != r["label"]:
                touched.setdefault(r["restaurant_id"], set()).update({r["label"], hol[0]})
                updates.append(("label", hol[0], r["id"]))
                r["label"] = hol[0]
                out["rekeyed"] += 1
        nights = {}
        for r in rows:
            nights.setdefault((r["restaurant_id"], r["business_date"]), []).append(r)
        for (rid, _d), group in nights.items():
            # Only a night recorded before record_night marked its own
            # confounding: a later night's marks know which games were quiet
            # (_confounding's quiet and games), which this pass cannot —
            # re-marking it by the plain rule at every boot undid them (event
            # re-audit P4-08).
            if any(str(r.get("recorded_at") or "") >= CONFOUNDING_MARKED_FROM for r in group):
                continue
            marks = _confounding([{"label": r["label"]} for r in group])
            for r in group:
                conf, co = marks[r["label"]]
                if int(r["confounded"] or 0) != conf or (r["co_labels"] or None) != co:
                    updates.append(("confounded", (conf, co), r["id"]))
                    touched.setdefault(rid, set()).add(r["label"])
                    out["marked"] += 1
        if not updates:
            return out
        conn = get_conn(db_path)
        try:
            for what, value, row_id in updates:
                if what == "label":
                    conn.execute("UPDATE OR IGNORE event_outcomes SET label=? WHERE id=?", (value, row_id))
                else:
                    conn.execute("UPDATE event_outcomes SET confounded=?, co_labels=? WHERE id=?",
                                 (value[0], value[1], row_id))
            conn.commit()
        finally:
            conn.close()
        for rid, labels in touched.items():
            refresh_effects(rid, labels, db_path=db_path)
        if out["rekeyed"] or out["marked"]:
            log.info("event_memory: record re-keyed (%s holiday rows, %s confounding marks)",
                     out["rekeyed"], out["marked"])
    except Exception as e:
        log.warning("event_memory: record not re-keyed: %s", e)
    return out


# The day openings and closures started needing evidence (NEW_PLACE_MAX_REVIEWS,
# `closed`). Every "gone" before it was only a place dropping out of that
# week's search, and an "arrived" with many reviews was a rival the search
# ranked in — neither happened.
EVIDENCE_RULE_FROM = "2026-09-30"


def retract_churn_events(db_path=DB_PATH) -> int:
    """Remove the market events the old membership rule invented, all
    observed before EVIDENCE_RULE_FROM: every "gone", and every "arrived"
    with more than NEW_PLACE_MAX_REVIEWS reviews (or none reported). Idempotent, at boot. Returns rows
    removed; never raises."""
    try:
        conn = get_conn(db_path)
        try:
            n = conn.execute("DELETE FROM market_events WHERE observed_on<? AND (kind='gone' OR "
                             "(kind='arrived' AND (review_count IS NULL OR review_count>?)))",
                             (EVIDENCE_RULE_FROM, NEW_PLACE_MAX_REVIEWS)).rowcount
            conn.commit()
        finally:
            conn.close()
        if n:
            log.info("event_memory: %s market events from search churn retracted", n)
        return n or 0
    except Exception as e:
        log.warning("event_memory: churn retraction skipped: %s", e)
        return 0


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


def _same_thing(a, b) -> bool:
    """Two labels name one thing when one's words are all in the other's
    ("cubs" and "cubs cards") — measured_effect's own match. A holiday's key
    is one word no event label can contain."""
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def _confounding(flags, quiet=frozenset(), games=frozenset()) -> dict:
    """{label: (confounded 0|1, co_labels JSON or None)} for one night's
    flags (QUALITY-4): the night's labels grouped into the distinct things
    that happened (_same_thing), and every label on a night with more than
    one thing is confounded, carrying the others' labels. A closer's "Cubs
    game" beside the owner's listed "Cubs home game" is one thing.

    `quiet` (ids of quiet_flags) are games that confound no label but a
    game's, and `games` (ids of catalog-game flags) the games: a game's own
    row is confounded by everything else on its night, quiet or loud; any
    other label only by the loud things. Neither rule reads the label's OWN
    state, so a game turning loud (or quiet again) changes no row of its
    own — only other labels' — and cannot flip itself back on a re-record
    (event re-audit P4-08)."""
    groups = []                    # [[labels], loud, game]
    for f in flags:
        lab = f["label"]
        loud, game = id(f) not in quiet, id(f) in games
        for g in groups:
            if any(_same_thing(lab, o) for o in g[0]):
                if lab not in g[0]:
                    g[0].append(lab)
                g[1], g[2] = g[1] or loud, g[2] or game
                break
        else:
            groups.append([[lab], loud, game])
    out = {}
    for g in groups:
        others = [o[0][0] for o in groups if o is not g and (o[1] or g[2])]
        for lab in g[0]:
            out[lab] = (1, json.dumps(others)) if others else (0, None)
    return out


def _iso(d):
    return d.isoformat() if hasattr(d, "isoformat") else str(d)[:10]


def _as_date(d):
    return d if isinstance(d, date) and not isinstance(d, datetime) else date.fromisoformat(str(d)[:10])


# ── what is known about a date ─────────────────────────────────────────────

def holiday_key(name) -> str:
    """"New Year's Day" -> "holiday:new_years_day": a holiday's calendar
    identity (QUALITY-5), "" for no name. Never a token subset of another
    holiday's key, and never matched by an event's words."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower().replace("'", "").replace("\u2019", "")).strip("_")
    return f"{HOLIDAY_PREFIX}{slug}" if slug else ""


def is_holiday_key(label) -> bool:
    return str(label or "").startswith(HOLIDAY_PREFIX)


def _holiday(day):
    """(key, name) for a dining holiday on `day` (holiday_key: its calendar
    identity), or None — never one the calendar only approximates
    (demand.APPROXIMATE_HOLIDAYS)."""
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
    return holiday_key(name) or None, name


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
            for r in conn.execute(f"SELECT date, kind, label, covers, lift_pct, source, ref FROM demand_signals "
                                  f"WHERE restaurant_id=? AND kind='event' AND date IN ({marks})",
                                  (restaurant_id, *isos)).fetchall():
                if str(r["source"] or "") == "campaign":
                    # A fill-a-night text (demand_signals.record_campaign) is
                    # the one campaign label, however its words ran ("Text to
                    # 412 guests to fill Tuesday"), so every campaign night
                    # adds to ONE measured campaign effect (campaign_effect —
                    # INT PRED-27); its lift is never the owner's guess.
                    out[str(r["date"])[:10]].append({"kind": "campaign", "label": CAMPAIGN_LABEL[0],
                                                     "raw": r["label"], "source": "demand_signals",
                                                     "owner_lift_pct": None, "covers": r["covers"]})
                    continue
                for lab in split_labels(r["label"]):
                    out[str(r["date"])[:10]].append({"kind": "event", "label": lab, "raw": r["label"],
                                                     "source": "demand_signals", "owner_lift_pct": r["lift_pct"],
                                                     "covers": r["covers"], "ref": r["ref"]})
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
                    d = str(r["business_date"])[:10]
                    games = [g for g in out[d] if g["kind"] == "event"
                             and str(g.get("ref") or "").startswith("event:")]
                    for lab in split_labels(r["influence"]):
                        f = {"kind": "influence", "label": lab, "raw": r["influence"], "source": "close_out",
                             "owner_lift_pct": None, "covers": None}
                        # The closer naming the catalog game flagged that
                        # night ("Bulls game", or the old close-out prefill's
                        # "Bulls home game · United Center") is that game:
                        # quiet while it is, its kin in a baseline — never a
                        # second, never-quiet thing that knocks the night out
                        # of every baseline (event re-audit P1-02).
                        same = next((g for g in games if _same_thing(lab, g["label"])), None)
                        if same is not None:
                            f["ref"], f["game"] = same["ref"], same["raw"]
                        out[d].append(f)
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


# ── which flagged nights still count as ordinary ───────────────────────────

# A frequent series (an NBA, NHL or MLS season: 30+ regular-season games)
# flags most nights of its season, and a baseline that left every flagged
# night out ran out of ordinary nights (Event Intelligence phase 4). Its game
# is QUIET (quiet_game) until this restaurant has measured games like it to
# matter — the same test measured_effect uses to apply an effect (`applies`,
# EFFECT_MIN_N nights) past EFFECT_FLOOR_PCT — and while quiet it:
#   * leaves its night in other nights' baselines (ordinary_nights),
#   * confounds no other label's night but another game's (record_night),
#   * is no concurrent change to an outcome (outcomes.concurrent_changes),
#   * earns no unasked surface (event_intel.engine.headline, the same test;
#     memory_lines; the pre-shift notes),
#   * is context, never a planned lift or a held cut (demand_signals.by_date).
# A closer's note naming the same game that night ("Bulls game") is that
# game (flags_for), never a second, never-quiet thing. A playoff game and an
# infrequent series (the Bears) are never quiet. A game's own baseline never
# holds its own label's nights (the same series, the same side) or a home
# game at its venue, so a series that does matter is not measured against
# itself — and home and road stay apart (event re-audit P4-06).
FREQUENT_SERIES_GAMES = 30
_MATTERS_SECONDS = 60
_matters_memo = {}


def effect_matters(eff) -> bool:
    """A measured_effect a plan may act on: it applies (EFFECT_MIN_N nights,
    _clears_floor) and its median is past EFFECT_FLOOR_PCT. The one test
    behind label_matters, effects_for_day and demand_signals.by_date."""
    return bool(eff and eff.get("applies") and abs(eff.get("median_lift_pct") or 0) >= EFFECT_FLOOR_PCT)


def label_matters(restaurant_id, label, db_path=None) -> bool:
    """Games like `label` measured here past EFFECT_FLOOR_PCT on enough
    nights to apply (effect_matters; memoised a minute). Never raises."""
    key = (restaurant_id, str(label or ""), db_path)
    hit = _matters_memo.get(key)
    import time as _t
    if hit and _t.monotonic() - hit[0] < _MATTERS_SECONDS:
        return hit[1]
    out = effect_matters(measured_effect(restaurant_id, label, db_path=db_path))
    if len(_matters_memo) > 5000:
        _matters_memo.clear()
    _matters_memo[key] = (_t.monotonic(), out)
    return out


def _forget_matters(restaurant_id):
    """Drop the restaurant's memoised label_matters answers (a label's state
    just changed)."""
    for k in [k for k in list(_matters_memo) if k[0] == restaurant_id]:
        _matters_memo.pop(k, None)


def quiet_game(restaurant_id, label, regular_games, season_type=None, db_path=None) -> bool:
    """A frequent series' game this restaurant hasn't measured to matter."""
    if season_type == "postseason" or int(regular_games or 0) < FREQUENT_SERIES_GAMES:
        return False
    return not label_matters(restaurant_id, label, db_path=db_path)


def _catalog_info(refs, db_path=None) -> dict:
    """{ref: {"regular", "season_type", "series_id", "venue", "home_away",
    "alt_venue"}} for catalog refs ("event:<id>"). A home game's `venue` is
    where it is played — its own venue, else its series' home venue (as
    engine.label_for names it); an alt-venue game (store.alt_venue: the
    Fire at SeatGeek Stadium) keeps its own. Never raises."""
    ids = []
    for ref in refs:
        try:
            ids.append(int(str(ref).split(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    if not ids:
        return {}
    try:
        conn = get_conn(db_path)
        try:
            marks = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT e.id, e.series_id, e.season_type, e.venue, e.home_away, e.attributes_json, "
                f"s.home_venue AS series_home_venue, "
                f"(SELECT COUNT(*) FROM catalog_events o WHERE o.series_id=e.series_id AND o.season=e.season "
                f"AND o.season_type='regular') AS n FROM catalog_events e LEFT JOIN event_series s "
                f"ON s.id=e.series_id WHERE e.id IN ({marks})", ids).fetchall()
        finally:
            conn.close()
    except Exception:
        return {}
    from event_intel import store as _catalog
    out = {}
    for r in rows:
        r = dict(r)
        alt = _catalog.alt_venue(r)
        venue = r.get("venue") or ("" if alt or r.get("home_away") != "home" else r.get("series_home_venue"))
        out[f"event:{r['id']}"] = {"regular": int(r["n"] or 0), "season_type": r["season_type"],
                                   "series_id": r["series_id"], "venue": (venue or "").strip().lower(),
                                   "home_away": r["home_away"], "alt_venue": alt}
    return out


def _catalog_refs(flags) -> set:
    """The catalog refs ("event:<id>") flags carry — a listed game's, and a
    closer's note that names it (flags_for)."""
    return {f.get("ref") for f in flags or [] if str(f.get("ref") or "").startswith("event:")}


def quiet_flags(restaurant_id, flags, info=None, db_path=None) -> set:
    """The ids (id(flag)) of the catalog flags in `flags` that are quiet —
    a game's flag, and a closer's note flags_for tied to it (judged by the
    game's own label, `game`)."""
    info = info if info is not None else _catalog_info(_catalog_refs(flags), db_path=db_path)
    out = set()
    for f in flags or []:
        i = info.get(f.get("ref")) if f.get("kind") in ("event", "influence") else None
        if i and quiet_game(restaurant_id, f.get("game") or f.get("raw") or f.get("label"), i["regular"],
                            i["season_type"], db_path=db_path):
            out.add(id(f))
    return out


def quiet_catalog(restaurant_id, label, ref, db_path=None) -> bool:
    """Whether one demand_signals catalog row (source "events", `ref`
    "event:<id>", its `label`) is a quiet game — quiet_flags for one row,
    for a surface that reads the rows themselves (the pre-shift notes)."""
    f = {"kind": "event", "label": normalise_label(label), "raw": label, "ref": ref}
    return id(f) in quiet_flags(restaurant_id, [f], db_path=db_path)


def ordinary_nights(restaurant_id, flags_by_day, db_path=None, tonight=None) -> set:
    """The ISO dates in `flags_by_day` ({iso: flags}, flags_for) that count
    as ordinary for a baseline: nothing flagged but quiet games — and, when
    `tonight` (the measured night's flags) carries a catalog game, none of
    its kin: a game of the same series on the same side and ground (its own
    label), or a home game at the venue it is played at (a Bulls home night
    is never measured against other United Center nights; the Fire's
    SeatGeek Stadium night against SeatGeek nights, not Soldier Field's).
    The series' other side stays in: a quiet
    Bulls road night is an ordinary night for a Bulls home game and the
    reverse, so home and road are measured apart, each on enough ordinary
    nights (event re-audit P4-06). Never raises."""
    refs = set()
    for fl in list(flags_by_day.values()) + [tonight or []]:
        refs |= _catalog_refs(fl)
    info = _catalog_info(refs, db_path=db_path) if refs else {}
    mine = [info[f["ref"]] for f in (tonight or []) if f.get("ref") in info]
    # A game's own label is its series and side — and its ground: an
    # alt-venue home game ("Fire home game · SeatGeek Stadium") is its own
    # label, kin to home games at the ground it is played on, never to the
    # series' home ground's nights (event re-audit, A1 handoff 8).
    own = lambda i: (i["series_id"], i["home_away"] == "home", bool(i.get("alt_venue")))
    sides = {own(m) for m in mine}
    venues = {m["venue"] for m in mine if m["home_away"] == "home" and m["venue"]}

    def _kin(f):
        i = info.get(f.get("ref"))
        return bool(i) and (own(i) in sides or (i["home_away"] == "home" and i["venue"] in venues))

    out = set()
    for d, fl in flags_by_day.items():
        fl = fl or []
        quiet = quiet_flags(restaurant_id, fl, info=info, db_path=db_path)
        if all(id(f) in quiet and not _kin(f) for f in fl):
            out.add(d)
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


def _night_staff(restaurant_id, iso, db_path=None):
    """(headcount, {role: people}, hours) for one night from the stored
    punches (shift_facts): each person once, by the role they worked. (None,
    None, None) with no punches on file. Never raises."""
    try:
        import shift_facts
        rows = shift_facts.rows(restaurant_id, since=iso, until=iso, db_path=db_path)
    except Exception:
        return None, None, None
    people, by_role, hours = set(), {}, 0.0
    for r in rows or []:
        if str(r.get("date") or "")[:10] != iso:
            continue
        who = " ".join(str(r.get("employee") or "").lower().split())
        if not who:
            continue
        people.add(who)
        by_role.setdefault((r.get("role") or "Unassigned").strip() or "Unassigned", set()).add(who)
        try:
            hours += float(r.get("actual_hours") or r.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            pass
    if not people:
        return None, None, None
    return len(people), {k: len(v) for k, v in sorted(by_role.items())}, round(hours, 1)


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
    ordinary = ordinary_nights(restaurant_id, {d.isoformat(): fl.get(d.isoformat()) for d in same_days},
                               db_path=db_path, tonight=fl.get(iso))
    base = [x["net"] for d, x in series.items() if d != iso and x.get("basis") == night.get("basis")
            and x.get("net") and x["net"] > 0 and d in ordinary]
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
        # An if-necessary game past its date with no result in may or may not
        # have been played, and the night's one lift can't be split from it:
        # the whole night is left unmeasured — its rows go, its flag stays,
        # so ordinary_nights keeps it out of every baseline — until a result
        # or a cancellation is entered (store.unresolved; event re-audit
        # P4-03, completing engine.sync_restaurant's half).
        from event_intel import store as _catalog
        if _catalog.unresolved_refs(_catalog_refs(flags), db_path=db_path):
            conn = get_conn(db_path)
            try:
                conn.execute("DELETE FROM event_outcomes WHERE restaurant_id=? AND business_date=?",
                             (restaurant_id, iso))
                conn.commit()
            finally:
                conn.close()
            refresh_effects(restaurant_id, before, db_path=db_path)
            return {"recorded": 0, "reason": "an if-necessary game with no result yet", "labels": []}
        m = measure_night(restaurant_id, day, db_path=db_path, flags=fl) if flags else {"reason": "no flags"}
        conn = get_conn(db_path)
        try:
            conn.execute("DELETE FROM event_outcomes WHERE restaurant_id=? AND business_date=?", (restaurant_id, iso))
            written = []
            if m.get("lift_pct") is not None:
                covers, labor = _night_extras(conn, restaurant_id, iso)
                heads, heads_by_role, hours = _night_staff(restaurant_id, iso, db_path=db_path)
                # One lift, one night: a night that carried two things is
                # kept for each, marked confounded (QUALITY-4) — a label is
                # measured on its own nights where it has enough of them.
                # A quiet game (a frequent series not measured to matter)
                # confounds no label but another game; a game is confounded
                # by everything else on its night, quiet or loud — one rule
                # whatever its own state (_confounding, event re-audit P4-08).
                info = _catalog_info(_catalog_refs(flags), db_path=db_path)
                marks = _confounding(flags, quiet=quiet_flags(restaurant_id, flags, info=info, db_path=db_path),
                                     games={id(f) for f in flags if f.get("ref") in info})
                seen = set()
                for f in flags:
                    key = (f["kind"], f["label"])
                    if key in seen:
                        continue
                    seen.add(key)
                    conf, co = marks.get(f["label"], (0, None))
                    conn.execute(
                        "INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, "
                        "source, net, baseline, baseline_n, lift_pct, basis, net_source, covers, labor_pct, "
                        "owner_lift_pct, confounded, co_labels, headcount, headcount_json, labor_hours) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (restaurant_id, iso, day.strftime("%A"), f["kind"], f["label"], str(f.get("raw") or "")[:160],
                         f.get("source"), m["net"], m["baseline"], m["baseline_n"], m["lift_pct"], m.get("basis"),
                         m.get("source"), covers if covers is not None else f.get("covers"), labor,
                         f.get("owner_lift_pct"), conf, co, heads,
                         json.dumps(heads_by_role) if heads_by_role else None, hours))
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


def _clears_floor(kind, n, med) -> bool:
    """A label's record is a pattern (measured_effect's `applies`): EFFECT_MIN_N
    nights, or a holiday's two nights or one past HOLIDAY_ONE_NIGHT_PCT."""
    return n >= EFFECT_MIN_N or (kind == "holiday" and (n >= 2 or abs(med or 0) >= HOLIDAY_ONE_NIGHT_PCT))


def _age_weight(night, newest) -> float:
    """A night's weight (QUALITY-21): half for every EFFECT_HALF_LIFE_YEARS
    it is older than the label's newest night, counted in whole years."""
    try:
        years = (_as_date(newest) - _as_date(night)).days // 365
    except ValueError:
        return 1.0
    return 0.5 ** (max(0, years) / EFFECT_HALF_LIFE_YEARS)


def _weighted_median(pairs):
    """The weighted median of [(value, weight)]: each value placed at the
    middle of its weight's share of the whole and read at the half by
    interpolation — equal weights give exactly the plain median."""
    pts = sorted((float(v), float(w)) for v, w in pairs if w > 0)
    if not pts:
        return None
    total = sum(w for _v, w in pts)
    run, xs = 0.0, []
    for v, w in pts:
        xs.append(((run + w / 2.0) / total, v))
        run += w
    if 0.5 <= xs[0][0]:
        return xs[0][1]
    for (p0, v0), (p1, v1) in zip(xs, xs[1:]):
        if p0 <= 0.5 <= p1:
            return v0 if p1 == p0 else v0 + (v1 - v0) * (0.5 - p0) / (p1 - p0)
    return xs[-1][1]


def _summary(rows) -> dict | None:
    """One label's summary over its nights (one per date).

    Which nights (QUALITY-4): the ones the label had to itself (not
    `confounded`) when they alone clear the floor (_clears_floor); else
    every night, and the summary says `confounded` — its figure also carries
    what else was on those nights, so effects_for_day never multiplies it
    with a label measured on the same nights. `dates` are the nights used.

    The figure (QUALITY-21): the age-weighted median (_age_weight) — the
    plain median while every night is within a year of the newest — and when
    the last DRIFT_RECENT nights all fall outside the older nights' spread,
    the recent median, with `drift` {"was", "recent", "n_recent", "since"}."""
    by_date = {}
    for r in sorted(rows, key=lambda r: int(r.get("confounded") or 0)):
        if r.get("lift_pct") is None:
            continue
        by_date.setdefault(r["business_date"], r)
    nights = sorted(by_date.values(), key=lambda r: r["business_date"])
    if not nights:
        return None
    kinds = [r["kind"] for r in nights]
    kind = max(set(kinds), key=kinds.count)
    clean = [r for r in nights if not int(r.get("confounded") or 0)]
    use = nights
    if clean and len(clean) < len(nights) and \
            _clears_floor(kind, len(clean), _median([float(r["lift_pct"]) for r in clean])):
        use = clean
    confounded = any(int(r.get("confounded") or 0) for r in use)
    newest = use[-1]["business_date"]
    med = _weighted_median([(r["lift_pct"], _age_weight(r["business_date"], newest)) for r in use])
    drift = None
    if len(use) >= 2 * DRIFT_RECENT:
        recent, older = use[-DRIFT_RECENT:], use[:-DRIFT_RECENT]
        r_lifts = [float(r["lift_pct"]) for r in recent]
        o_lifts = [float(r["lift_pct"]) for r in older]
        lo, hi = min(o_lifts), max(o_lifts)
        if all(x < lo for x in r_lifts) or all(x > hi for x in r_lifts):
            r_med, o_med = _median(r_lifts), _weighted_median(
                [(r["lift_pct"], _age_weight(r["business_date"], older[-1]["business_date"])) for r in older])
            if abs(r_med - o_med) >= EFFECT_FLOOR_PCT:
                drift = {"was": round(o_med, 1), "recent": round(r_med, 1), "n_recent": DRIFT_RECENT,
                         "since": recent[0]["business_date"]}
                med = r_med
    lifts = sorted(float(r["lift_pct"]) for r in use)
    guesses = [float(r["owner_lift_pct"]) for r in use if r.get("owner_lift_pct") is not None]
    wd = {}
    for r in use:
        wd[r["weekday"]] = wd.get(r["weekday"], 0) + 1
    return {"n": len(use), "median_lift_pct": round(med, 1), "low_lift_pct": lifts[0], "high_lift_pct": lifts[-1],
            "direction": ("up" if med >= EFFECT_FLOOR_PCT else "down" if med <= -EFFECT_FLOOR_PCT else "none"),
            "first_date": use[0]["business_date"], "last_date": use[-1]["business_date"],
            "weekdays": wd, "kind": kind, "display": _display(nights),
            "owner_guesses": len(guesses), "owner_median_pct": round(_median(guesses), 1) if guesses else None,
            "basis": sorted({r.get("basis") or "" for r in use} - {""}),
            "confounded": bool(confounded), "n_nights": len(nights), "n_alone": len(clean),
            "dates": [r["business_date"] for r in use], "drift": drift}


def _record_words(s) -> str:
    """What a summary adds to its sentence: the drift, and nights shared
    with something else. "" when neither."""
    bits = []
    d = s.get("drift")
    if d:
        bits.append(f"was {d['was']:+.0f}%, the last {d['n_recent']} nights {d['recent']:+.0f}%")
    if s.get("confounded"):
        bits.append("on nights with something else going on too")
    return ("; " + "; ".join(bits)) if bits else ""


def refresh_effects(restaurant_id, labels, db_path=None):
    """Recompute the per-label summaries (event_effects) for `labels` from
    the record. A label with no measured night left is removed from the
    summaries — its record went with it. Never raises."""
    labels = {l for l in (labels or ()) if l}
    if not labels:
        return
    flipped = []
    try:
        conn = get_conn(db_path)
        try:
            for lab in labels:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM event_outcomes WHERE restaurant_id=? AND label=?", (restaurant_id, lab)).fetchall()]
                s = _summary(rows)
                old = conn.execute("SELECT kind, n, median_lift_pct FROM event_effects WHERE restaurant_id=? "
                                   "AND label=?", (restaurant_id, lab)).fetchone()
                if "event" in {(old["kind"] if old else None), (s["kind"] if s else None)} and \
                        _summary_matters(dict(old) if old else None) != _summary_matters(s):
                    flipped.append(lab)
                if not s:
                    conn.execute("DELETE FROM event_effects WHERE restaurant_id=? AND label=?", (restaurant_id, lab))
                    continue
                conn.execute(
                    "INSERT INTO event_effects (restaurant_id, label, kind, display, n, median_lift_pct, low_lift_pct, "
                    "high_lift_pct, direction, first_date, last_date, weekdays_json, owner_guesses, owner_median_pct, "
                    "confounded, drift_json, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                    "ON CONFLICT(restaurant_id, label) DO UPDATE SET kind=excluded.kind, display=excluded.display, "
                    "n=excluded.n, median_lift_pct=excluded.median_lift_pct, low_lift_pct=excluded.low_lift_pct, "
                    "high_lift_pct=excluded.high_lift_pct, direction=excluded.direction, "
                    "first_date=excluded.first_date, last_date=excluded.last_date, "
                    "weekdays_json=excluded.weekdays_json, owner_guesses=excluded.owner_guesses, "
                    "owner_median_pct=excluded.owner_median_pct, confounded=excluded.confounded, "
                    "drift_json=excluded.drift_json, updated_at=excluded.updated_at",
                    (restaurant_id, lab, s["kind"], s["display"], s["n"], s["median_lift_pct"], s["low_lift_pct"],
                     s["high_lift_pct"], s["direction"], s["first_date"], s["last_date"], json.dumps(s["weekdays"]),
                     s["owner_guesses"], s["owner_median_pct"], 1 if s["confounded"] else 0,
                     json.dumps(s["drift"]) if s["drift"] else None))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("event_memory: summaries not refreshed rid=%s: %s", restaurant_id, e)
        return
    if flipped:
        # A game's quiet/loud state decides other nights' baselines and other
        # labels' confounding: the nights recorded under the old state are
        # re-recorded (queue_rerecord, the backfill cursor), and the answers
        # memoised under it are dropped (event re-audit P4-08).
        _forget_matters(restaurant_id)
        queue_rerecord(restaurant_id, db_path=db_path)


def _summary_matters(s) -> bool:
    """effect_matters for a stored or fresh summary (event_effects row or
    _summary): whether the label, as recorded, is past the floor."""
    if not s:
        return False
    return _clears_floor(s.get("kind"), int(s.get("n") or 0), s.get("median_lift_pct")) and \
        abs(float(s.get("median_lift_pct") or 0)) >= EFFECT_FLOOR_PCT


# ── reading ────────────────────────────────────────────────────────────────

def measured_effect(restaurant_id, label, db_path=None):
    """This restaurant's measured effect of a recurring label ("football
    sunday", "rain", "1st of month") -> {"median_lift_pct": float, "n": int,
    "last": date} once it has a sample, else None.

    Matched by label TOKENS over the record, one night per date: "cubs"
    counts every night whose label names the Cubs ("cubs", "cubs cardinals").
    A holiday's key (holiday_key, "holiday:new_years_day") is matched
    exactly — New Year's Day never reads New Year's Eve (QUALITY-5).
    Also carries "label", "display", "kind", "low_lift_pct",
    "high_lift_pct", "direction" (up | down | none, past EFFECT_FLOOR_PCT),
    "applies" (n clears the floor — EFFECT_MIN_N, or a holiday's one night
    past HOLIDAY_ONE_NIGHT_PCT), "owner_median_pct" (what the owner guessed
    on those nights, when they did), "confounded" / "dates" (the nights it
    rests on, and whether something else was on them — QUALITY-4), "drift"
    (QUALITY-21) and "basis" — the sentence a surface says. Never raises."""
    if is_holiday_key(label):
        norm = str(label).strip()
        want = None
    else:
        norm = normalise_label(label)
        want = _tokens(norm)
        if not want:
            return None
    try:
        conn = get_conn(db_path)
        try:
            if want is None:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM event_outcomes WHERE restaurant_id=? AND lift_pct IS NOT NULL AND label=?",
                    (restaurant_id, norm)).fetchall()]
            else:
                probe = max(want, key=len)
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM event_outcomes WHERE restaurant_id=? AND lift_pct IS NOT NULL AND label LIKE ?",
                    (restaurant_id, f"%{probe}%")).fetchall()]
        finally:
            conn.close()
    except Exception:
        return None
    hits = rows if want is None else [r for r in rows if not is_holiday_key(r["label"]) and want <= _tokens(r["label"])]
    s = _summary(hits)
    if not s:
        return None
    n, med = s["n"], s["median_lift_pct"]
    applies = _clears_floor(s["kind"], n, med)
    from time_utils import mdy
    word = "above" if med >= 0 else "below"
    basis = (f"{s['display'] or label}: nights here ran a median {abs(med):.0f}% {word} a typical same weekday "
             f"(measured {n} time{'s' if n != 1 else ''}, last {mdy(s['last_date'])}{_record_words(s)}) — before and "
             f"after, not proof")
    staffing = _staffing_on(hits)
    if staffing:
        basis += f"; {staffing['text']}"
    return {"median_lift_pct": med, "n": n, "last": date.fromisoformat(s["last_date"]),
            "label": norm, "staffing": staffing,
            "display": s["display"], "kind": s["kind"], "low_lift_pct": s["low_lift_pct"],
            "high_lift_pct": s["high_lift_pct"], "direction": s["direction"], "applies": bool(applies),
            "owner_median_pct": s["owner_median_pct"], "basis": basis,
            "confounded": s["confounded"], "dates": list(s["dates"]), "drift": s["drift"]}


def _staffing_on(rows):
    """Who stood on a label's nights: {"n", "median_headcount",
    "median_labor_pct", "median_hours", "by_role", "text"} over the nights
    that carry punches, or None. by_role is each role's median people.
    The words say what was staffed, never whether it was right - that is
    the owner's (or the schedule's) to judge against the lift."""
    nights = {}
    for r in rows or []:
        if r.get("headcount"):
            nights.setdefault(r["business_date"], r)
    if not nights:
        return None
    vals = list(nights.values())
    heads = _median([v["headcount"] for v in vals])
    labor = [v["labor_pct"] for v in vals if v.get("labor_pct") is not None]
    hours = [v["labor_hours"] for v in vals if v.get("labor_hours")]
    roles = {}
    for v in vals:
        try:
            for k, c in (json.loads(v.get("headcount_json") or "{}") or {}).items():
                roles.setdefault(k, []).append(int(c))
        except (TypeError, ValueError):
            continue
    by_role = {k: _median(c) for k, c in sorted(roles.items())}
    lab = _median(labor) if labor else None
    top = ", ".join(f"{v:g} {k}" for k, v in sorted(by_role.items(), key=lambda kv: -kv[1])[:3])
    text = (f"on those nights a median {heads:g} people worked" + (f" ({top})" if top else "")
            + (f" and labor ran {lab:.1f}% of sales" if lab is not None else "")
            + f", over {len(vals)} night{'s' if len(vals) != 1 else ''} with punches")
    return {"n": len(vals), "median_headcount": heads, "median_labor_pct": lab,
            "median_hours": _median(hours) if hours else None, "by_role": by_role, "text": text}


def summaries(restaurant_id, limit=12, db_path=None) -> list:
    """What the nights taught here, for the owner: [{"label", "display",
    "kind", "n", "median_lift_pct", "low_lift_pct", "high_lift_pct",
    "direction", "last_date", "applies", "owner_median_pct", "text"}] from
    the per-label summaries (event_effects), the ones past the sample floor
    first, then the most measured. `text` is the sentence, M/D/YY. Never
    raises."""
    from time_utils import mdy
    try:
        conn = get_conn(db_path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT label, display, kind, n, median_lift_pct, low_lift_pct, high_lift_pct, direction, last_date, "
                "owner_median_pct, confounded, drift_json FROM event_effects WHERE restaurant_id=? "
                "ORDER BY n DESC, ABS(median_lift_pct) DESC", (restaurant_id,)).fetchall()]
        finally:
            conn.close()
    except Exception:
        return []
    out = []
    for r in rows:
        n, med = int(r["n"] or 0), float(r["median_lift_pct"] or 0.0)
        applies = _clears_floor(r["kind"], n, med)
        word = "above" if med >= 0 else "below"
        try:
            drift = json.loads(r.pop("drift_json") or "null")
        except ValueError:
            drift = None
        r["drift"] = drift
        r["confounded"] = bool(r.get("confounded"))
        text = (f"{r['display'] or r['label']}: nights ran a median {abs(med):.0f}% {word} a typical same weekday "
                f"(measured {n} time{'s' if n != 1 else ''}, last {mdy(r['last_date'])}{_record_words(r)}) — before "
                f"and after, not proof")
        if r.get("owner_median_pct") is not None:
            text += f"; you had listed it at {float(r['owner_median_pct']):+.0f}%"
        out.append(dict(r, applies=bool(applies), text=text))
    out.sort(key=lambda x: (not x["applies"], -int(x["n"] or 0)))
    return out[:limit]


def campaign_effect(restaurant_id, db_path=None):
    """THE measured effect of a guest text campaign here (INT PRED-27): the
    campaign nights' own lift against their typical same weekday
    (event_outcomes, label CAMPAIGN_LABEL), measured_effect's shape and
    floor (`applies`). Staffing (demand_signals), the forecast
    (effects_for_day) and the campaign's own result (campaign_night) all
    read this one measurement. None before the first measured night."""
    return measured_effect(restaurant_id, CAMPAIGN_LABEL[0], db_path=db_path)


def campaign_night(restaurant_id, day, db_path=None):
    """{"date", "lift_pct"} — what one campaign's target night measured
    against its typical same weekday (the recorded event_outcomes row), or
    None when the night is not recorded yet. Never raises."""
    try:
        iso = _as_date(day).isoformat()
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT lift_pct FROM event_outcomes WHERE restaurant_id=? AND business_date=? "
                             "AND kind='campaign' AND lift_pct IS NOT NULL ORDER BY id DESC LIMIT 1",
                             (restaurant_id, iso)).fetchone()
        finally:
            conn.close()
        return {"date": iso, "lift_pct": round(float(r["lift_pct"]), 1)} if r else None
    except Exception:
        return None


def effects_for_day(restaurant_id, day, db_path=None, flags=None) -> dict | None:
    """The measured effects a forecast of `day` may apply: for each kind known
    BEFORE the night (a listed event, the holiday, a campaign aimed at it, the
    1st or 15th), the label on the day whose measured effect clears the floor
    (measured_effect's `applies`) and moves sales past EFFECT_FLOOR_PCT —
    the most-measured one per kind — combined multiplicatively and bounded
    to EFFECT_BOUNDS.

    Never twice for one lift (QUALITY-4): two labels measured on any of the
    same nights (Mother's Day and the owner's "Mother's Day brunch", every
    one of them the same Sunday) are not independent, so only the larger
    applies — the other is `subsumed`, never multiplied in. Mother's Day at
    +40% three years running forecasts +40%, not +96%.

    {"pct", "applied": [{"label", "display", "kind", "lift_pct", "n"}],
    "subsumed": [same shape, with "by"], "basis"} or None when nothing
    applies. Never raises."""
    try:
        day = _as_date(day)
        fl = (flags if flags is not None else flags_for(restaurant_id, [day], db_path=db_path,
                                                        known_before=True)).get(day.isoformat()) or []
        best = {}
        for f in fl:
            if f["kind"] not in KNOWN_BEFORE:
                continue
            e = measured_effect(restaurant_id, f["label"], db_path=db_path)
            # effect_matters, spelled out (tests/test_mem_int_measurement.py
            # reads this floor in the source).
            if not e or not e["applies"] or abs(e["median_lift_pct"]) < EFFECT_FLOOR_PCT:
                continue
            cur = best.get(f["kind"])
            if cur is None or (e["n"], abs(e["median_lift_pct"])) > (cur["n"], abs(cur["lift_pct"])):
                best[f["kind"]] = {"label": f["label"], "display": e["display"] or f.get("raw"), "kind": f["kind"],
                                   "lift_pct": e["median_lift_pct"], "n": e["n"], "basis": e["basis"],
                                   "_dates": set(e.get("dates") or ())}
        if not best:
            return None
        applied, subsumed, used = [], [], {}
        for e in sorted(best.values(), key=lambda e: (-abs(e["lift_pct"]), -e["n"])):
            shared = next((lab for lab, ds in used.items() if ds & e["_dates"]), None)
            if shared is not None:
                subsumed.append(dict(e, by=shared))
                continue
            applied.append(e)
            used[e["label"]] = e["_dates"]
        factor = 1.0
        for e in applied:
            factor *= 1.0 + e["lift_pct"] / 100.0
        pct = max(EFFECT_BOUNDS[0], min(EFFECT_BOUNDS[1], round((factor - 1.0) * 100.0, 1)))
        for e in applied + subsumed:
            e.pop("_dates", None)
        return {"pct": pct, "applied": applied, "subsumed": subsumed,
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
    people's words (a closer's note, the owner's event name), so each line's
    text — the label, and an owner's own guess at the lift — is fenced
    (trusted False); what Cavnar AI measured is the line's trusted
    `measured` suffix, outside the fence, so an answer that cites the
    measured lift verifies (memory re-audit 9/29/26, PROMPTS-3: fenced with
    the label, "18%" could never be cited). Dates are M/D/YY through
    memory_context."""
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
    asked = getattr(req, "surface", "") == "ask"
    for d in sorted(fl):
        # A quiet game (and a closer's note naming it) earns no unasked line
        # — not the brief's "Remembered:", not the report's, schedule's,
        # labor read's or weekly plan's prompt; Ask, being asked, still has
        # it (event re-audit P4-01).
        quiet = set() if asked else quiet_flags(rid, fl[d], db_path=db_path)
        for f in fl[d]:
            if id(f) in quiet:
                continue
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
            out.append({"text": f"{wd} {mdy(d)}: {f.get('raw') or f['label']}{guess}",
                        "measured": (f"Measured here: nights like it ran a median {abs(e['median_lift_pct']):.0f}% "
                                     f"{word} a typical same weekday (measured {e['n']} "
                                     f"time{'s' if e['n'] != 1 else ''}, last {mdy(e['last'])}{_record_words(e)}; "
                                     f"before and after, not proof){floor}."),
                        "date": d, "source": "system", "subject": f"event:{e['label']}",
                        # the catalog game it is about ("event:<id>"), so a
                        # surface that speaks for that game says it once
                        # (morning_brief, event re-audit P2-06)
                        "ref": f.get("ref"),
                        "weight": 2.0 + min(e["n"], 10) / 10.0, "trusted": False})
    if getattr(req, "surface", "") in ("ask", "schedule", "weekly_plan", "marketing"):
        try:
            conn = get_conn(db_path)
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT label, display, n, median_lift_pct, last_date, confounded, drift_json FROM event_effects "
                    "WHERE restaurant_id=? "
                    "AND n >= ? AND ABS(median_lift_pct) >= ? ORDER BY n DESC, ABS(median_lift_pct) DESC LIMIT ?",
                    (rid, EFFECT_MIN_N, EFFECT_FLOOR_PCT, MEMORY_TOP_LABELS + len(named))).fetchall()]
            finally:
                conn.close()
        except Exception:
            rows = []
        for r in [r for r in rows if r["label"] not in named][:MEMORY_TOP_LABELS]:
            word = "above" if r["median_lift_pct"] >= 0 else "below"
            try:
                r["drift"] = json.loads(r.get("drift_json") or "null")
            except ValueError:
                r["drift"] = None
            out.append({"text": f"Recurring here: {r['display'] or r['label']}",
                        "measured": (f"Measured here: nights ran a median {abs(r['median_lift_pct']):.0f}% {word} "
                                     f"a typical same weekday (measured {r['n']} times{_record_words(r)}; before and "
                                     f"after, not proof)."),
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


RERECORD_PREFIX = "event_memory_rerecord:"


def queue_rerecord(restaurant_id, db_path=None):
    """Ask for the restaurant's history to be recorded again — a label's
    quiet/loud state changed (refresh_effects), so every night recorded
    under the old state (its baselines, other labels' confounding) is stale.
    A marker in job_cursors that remember_restaurant drains through the
    backfill cursor: at once when the backfill is finished, else when the
    one under way finishes (a restart mid-way would starve its oldest
    nights). Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (f"{RERECORD_PREFIX}{restaurant_id}", datetime.now().isoformat(timespec="seconds")))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("event_memory: re-record not queued rid=%s: %s", restaurant_id, e)


def _rerecord_queued(restaurant_id, db_path=None, clear=False) -> bool:
    key = f"{RERECORD_PREFIX}{restaurant_id}"
    conn = get_conn(db_path)
    try:
        if clear:
            conn.execute("DELETE FROM job_cursors WHERE key=?", (key,))
            conn.commit()
            return False
        return conn.execute("SELECT 1 FROM job_cursors WHERE key=?", (key,)).fetchone() is not None
    finally:
        conn.close()


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
    # Covers from the POS guest count first, so the nights recorded below
    # carry them (covers.sync_from_pos: the last 35 nights, never over a
    # count someone entered).
    try:
        import covers as _cov
        _cov.sync_from_pos(rid, db_path=db_path)
    except Exception:
        pass
    w = record_weather(restaurant, [yesterday - timedelta(days=k) for k in range(WEATHER_DAYS)], db_path=db_path)
    recorded = 0
    for k in range(RECENT_NIGHTS):
        recorded += record_night(rid, yesterday - timedelta(days=k), db_path=db_path).get("recorded", 0)
    reached = _backfill_cursor(rid, db_path=db_path)
    floor = today - timedelta(days=BACKFILL_DAYS)
    top = (date.fromisoformat(reached) - timedelta(days=1)) if reached else (yesterday - timedelta(days=RECENT_NIGHTS))
    # A label's quiet/loud state changed (queue_rerecord): once no backfill
    # is under way, read the history again from the top (event re-audit
    # P4-08). One under way finishes first, then this restarts it.
    if (not reached or top < floor) and _rerecord_queued(rid, db_path=db_path):
        _rerecord_queued(rid, db_path=db_path, clear=True)
        if reached:
            reset_backfill(rid, db_path=db_path)
            reached, top = None, yesterday - timedelta(days=RECENT_NIGHTS)
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
        from models import in_service_sql, learns_for_itself
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
        # Its own event memory (models.learns_for_itself, INVENTORY-1):
        # only a demo or an admin-excluded account is skipped.
        if rest is not None and learns_for_itself(rest):
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
# The kind of market_events row that only holds the reading a move is
# measured from — not an event, never listed.
TRACKED = "tracked"
MARKET_KINDS = ("arrived", "gone", "rating_up", "rating_down")
# "New nearby" means the place opened, not that this week's search returned it:
# a place never tracked before AND with this few Google reviews. An
# established rival with hundreds of reviews that the search ranks in this
# week did not open (owner, 9/29/26 — the tracked set is the top matches of a
# Google nearby search and moves week to week).
NEW_PLACE_MAX_REVIEWS = 60


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


def record_market_snapshot(restaurant_id, competitors, at=None, db_path=None, closed=()) -> list:
    """After a competitor check, keep what the market did: the monthly rating
    series (competitor_rating_monthly, the latest reading each month) and a
    market event (market_events) when a competitor OPENED (never tracked
    before, after the first check, and with at most NEW_PLACE_MAX_REVIEWS
    reviews — a place the search merely ranked in this week is not an
    opening), CLOSED (`closed`: Google's own business_status for places that
    dropped out of the search — dropping out alone is not closing), or its
    rating moved MARKET_MOVE_STARS or more since the
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
                if cur.rowcount and kind != TRACKED:
                    written.append({"place_id": pid, "name": name, "kind": kind, "from_rating": frm,
                                    "to_rating": to, "observed_on": day})
            for pid, c in now_set.items():
                rating = c.get("rating")
                rating = float(rating) if isinstance(rating, (int, float)) and rating > 0 else None
                count = int(c["review_count"]) if isinstance(c.get("review_count"), (int, float)) else None
                opened = (pid not in latest and count is not None and count <= NEW_PLACE_MAX_REVIEWS)
                if not first_run and (opened or pid in gone):
                    _event(pid, c.get("name"), "arrived", None, rating, count)
                elif pid not in anchors:
                    # The reading a later move is measured from, kept as a
                    # 'tracked' row (never listed as a market event): the
                    # monthly series keeps only each month's latest reading.
                    anchor = first_rating.get(pid)
                    if rating is not None and anchor is not None \
                            and abs(rating - anchor) >= MARKET_MOVE_STARS - 1e-9:
                        _event(pid, c.get("name"), "rating_up" if rating > anchor else "rating_down",
                               round(anchor, 2), rating, count)
                    elif rating is not None:
                        _event(pid, c.get("name"), TRACKED, None, anchor if anchor is not None else rating, count)
                else:
                    anchor = anchors[pid]
                    if rating is not None and abs(rating - anchor) >= MARKET_MOVE_STARS - 1e-9:
                        _event(pid, c.get("name"), "rating_up" if rating > anchor else "rating_down",
                               round(anchor, 2), rating, count)
                conn.execute("INSERT INTO competitor_rating_monthly (restaurant_id, place_id, month, name, rating, "
                             "review_count, captured_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, place_id, "
                             "month) DO UPDATE SET name=excluded.name, rating=COALESCE(excluded.rating, "
                             "competitor_rating_monthly.rating), review_count=COALESCE(excluded.review_count, "
                             "competitor_rating_monthly.review_count), captured_at=excluded.captured_at",
                             (restaurant_id, pid, month, c.get("name"), rating, count, day))
            for c in closed or ():
                pid = c.get("place_id")
                if pid and pid in latest and pid not in now_set and pid not in gone:
                    r = latest[pid]
                    _event(pid, c.get("name") or r.get("name"), "gone", r.get("rating"), None, r.get("review_count"))
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
                  f"WHERE restaurant_id=? AND kind != '{TRACKED}'"
            if since:
                sql += " AND observed_on >= ?"
                args.append(_iso(since))
            return [dict(r) for r in conn.execute(sql + " ORDER BY observed_on DESC, id DESC", args).fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


# ── the public history, read back (INVENTORY-6) ─────────────────────────────
#
# The market's events and the restaurant's own rating trajectory were kept
# forever and read by one screen (/intel/movement): "Bella's opened across the
# street in March and our rating slid 0.3 since" sat on a chart and never in
# the competitor read, the review read, the weekly plan, marketing or Ask.

MARKET_MEMORY_DAYS = 180
MARKET_MEMORY_MAX_EVENTS = 6


def _week_start(week):
    """"2026-W39" -> the Monday that ISO week starts, or None."""
    try:
        y, w = str(week).split("-W")
        return date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, TypeError):
        return None


def market_summary(restaurant_id, today=None, days=MARKET_MEMORY_DAYS, db_path=None) -> dict:
    """{"since", "events": [market_history rows, newest first, at most
    MARKET_MEMORY_MAX_EVENTS], "n_events", "own": {"from", "to", "from_week",
    "to_week", "change", "weeks"} or None, "text": [the sentences, M/D/YY]}
    — what the local market did in the last `days` (competitors opening,
    closing, their ratings moving MARKET_MOVE_STARS or more) and this
    restaurant's own public rating over the same window. The one reading
    memory_context's "market" section and Ask's read_market_history say.
    Public Google listings, as the weekly competitor check saw them. Never
    raises."""
    from time_utils import mdy
    today = _as_date(today) if today else date.today()
    since = today - timedelta(days=days)
    events = market_history(restaurant_id, since=since, db_path=db_path)
    text, items = [], []
    for ev in events[:MARKET_MEMORY_MAX_EVENTS]:
        before = len(text)
        name = str(ev.get("name") or "A competitor")[:80]
        when = mdy(ev.get("observed_on"))
        k = ev.get("kind")
        if k == "arrived":
            count = ev.get("review_count")
            text.append(f"{name} opened nearby (first seen {when}"
                        + (f", {int(count)} Google reviews then" if isinstance(count, (int, float)) else "") + ").")
        elif k == "gone":
            text.append(f"{name} closed (Google lists it as closed, seen {when}).")
        elif k in ("rating_up", "rating_down") and ev.get("from_rating") is not None \
                and ev.get("to_rating") is not None:
            text.append(f"{name}'s Google rating {'rose' if k == 'rating_up' else 'fell'} from "
                        f"{float(ev['from_rating']):.1f} to {float(ev['to_rating']):.1f} (seen {when}).")
        if len(text) > before:
            items.append({"event": ev, "text": text[-1]})
    own = None
    traj = own_rating_trajectory(restaurant_id, db_path=db_path)
    series = traj.get("series") or []
    if len(series) >= 2:
        cutoff = _iso_week(since)
        before = [r for r in series if str(r["week"]) <= cutoff]
        first = before[-1] if before else series[0]
        last = series[-1]
        if first is not last:
            change = round(float(last["rating"]) - float(first["rating"]), 2)
            own = {"from": first["rating"], "to": last["rating"], "from_week": first["week"],
                   "to_week": last["week"], "change": change, "weeks": len(series)}
            start, end = _week_start(first["week"]), _week_start(last["week"])
            move = ("held at" if abs(change) < 0.05 else "rose from" if change > 0 else "fell from")
            own["text"] = (f"This restaurant's own Google rating {move} {float(first['rating']):.1f}"
                           + ("" if move == "held at" else f" to {float(last['rating']):.1f}")
                           + (f" between the weeks of {mdy(start)} and {mdy(end)}" if start and end else "")
                           + f" ({len(series)} weeks on file).")
            text.append(own["text"])
    return {"since": since.isoformat(), "events": events[:MARKET_MEMORY_MAX_EVENTS], "n_events": len(events),
            "items": items, "own": own, "text": text}


def market_lines(req):
    """memory_context provider ("market"): market_summary's sentences for the
    surfaces that reason about the market — the competitor read, the review
    read, the weekly plan, marketing and Ask (memory_context.SURFACE_SECTIONS).
    Competitors' names are Google's public listing text, so those lines are
    fenced (trusted False); the own-rating line is figures only. Each line is
    the Intel module's (module "intel"): a login that cannot open Intel never
    reads it."""
    rid = req.restaurant_id
    now = getattr(req, "now", None) or datetime.now()
    today = now.date() if isinstance(now, datetime) else _as_date(now)
    s = market_summary(rid, today=today, db_path=getattr(req, "db_path", None))
    out = []
    for it in s["items"]:
        ev = it["event"]
        out.append({"text": it["text"], "date": ev.get("observed_on"), "source": "system", "module": "intel",
                    "subject": f"market:{ev.get('place_id')}", "weight": 1.5, "trusted": False})
    if s["own"]:
        out.append({"text": s["own"]["text"], "date": _week_start(s["own"]["to_week"]), "source": "system",
                    "module": "intel", "subject": "market:own_rating", "weight": 2.0, "trusted": True})
    return out
