"""
schedule_memory.py — what a restaurant's scheduling has learned, as one
compact memory with a confidence, a decay and an enforcement level, and the
observation log it is built from (schedule audit 10/3/26 L-29).

Before this, only the manager's edit habits had a compact store
(schedule_standing_patterns); outcomes, attendance by weekday, drop and
claim preferences and mentoring were recomputed raw on every generation
with no confidence and no "last confirmed", openers, sections, teams and
overtime were never learned at all (L-16, L-21, L-22), and what was learned
reached only the prompt, so the solver, the optimizer, the budget trim and
the scorer could undo it (L-3). Nothing said in one place what the
restaurant had learned, how sure it was, or when it was last confirmed.

THE OBSERVATION LOG (`schedule_observations`, raw, 400 days): one row per
atomic fact, with its ORIGIN (manager, cavnar, staff, system), PHASE
(pre_publish, post_publish, as_run) and AUTHORITY (principal, delegate,
system, admin). Edits and the owner's no are events, appended
(schedule_versions.observe_publish, record_rejection, schedule_learning.
capture_save — wave 1, H1); what was MEASURED carries a `fact_key` and is
re-recorded in place (actual hours and people per slot, a shift that ran
past its end, overtime actually worked, the week's Shift Quality as planned
and as it ran — record_outcomes, the nightly join, the consolidation).

THE MEMORY (`schedule_memory`, compact, one row per fact key): kind, scope
(person, role, weekday, daypart), value, the evidence (`opportunities` — the
weeks or shifts the fact could have shown — and `hits`, each weighted by its
age's half-life), `misses_by_hand`, `last_confirmed_by_hand`, `confidence`
(the Wilson lower bound of the weighted hits over the weighted
opportunities, times the half-life decay since a hand last confirmed a
habit), `status` and `enforcement`. The ladder:

  candidate   at least CANDIDATE_MIN_HITS weeks and CANDIDATE_MIN_RATE of
              its opportunities — from the manager's own pre-publish hand,
              the restaurant's own scheduling before Cavnar AI, or what was
              measured on the floor; told to the model, enforced nowhere
  active      confidence >= ACTIVE_CONFIDENCE: a soft cost in the solver,
              optimizer, trim and scorer (enforced_signals → signals
              ["learned"]) and one short prompt line
  rule        the owner made it one (availability, a role floor or time, a
              pair, a standing shift) — hard, in Constraints; no line here
  retest      a standing pattern left out of one draft on purpose (L-30)
  retired     reversed twice by hand, faded below RETIRE_CONFIDENCE with no
              hand confirmation in two half-lives, no longer seen, or the
              owner let it go
  dormant     about somebody with no shifts lately

The guards (raw_L §3 F): an admin's (view-as, support) word and Cavnar AI's
own kept changes are stored and never counted as the manager's; a week the
automatic publish sent is nobody's evidence; a reaction to a sent week, a
staff swap and a Cavnar-authored save never become a manager habit; a draft
choice the manager merely left in place keeps a memory alive but never
starts one (a habit is born only from a hand). A learned "keep apart" is
never enforced on its own — it waits for the owner.

What it hands the rest of the pipeline:

  observe(...)              one fact into the log; never raises into the
                            caller's work
  consolidate(rid)          the memory rebuilt from its sources (the nightly
                            job strategy_jobs.run_schedule_memory — bounded
                            and resumable; the generation refreshes the
                            patterns alone before it reads)
  enforced_signals(...)     the active memories that bind this week's
                            passes, [{kind, key, person, day, daypart, role,
                            value, confidence, enforcement, source}] —
                            schedule_engine._learning_signals passes them as
                            signals["learned"]; learned_cost / misses read
                            them for any pass
  prompt_lines(...)         the memories, and the generation's other
                            learned blocks handed in as `sections`, as ONE
                            budgeted, relevance-ranked block (L-28)
  pad_overruns(...)         the closing rows of a role that measurably runs
                            past its scheduled end, ended when it really ends
                            (L-16), where that is legal
  memory_view / owner_answer   the owner's screen and say: keep, let go,
                            make it a rule
  usual_sections(...)       each server's usual section, for the Studio's
                            picker to suggest (never assigned by code)
  memory_lines(req)         the memory_context provider for the labor read
                            and Ask
  suggested_ratings(...)    measured server performance offered as a rating
                            for the owner to confirm (L-23, D-27) — owner-
                            only, never in any prompt

What it learns (`kind`): the manager's habits migrated whole from the
standing patterns (moved_off / moved_on, retime_start / retime_end,
headcount_add / headcount_cut, role_change, leader_swap), who opens and who
closes each role on each weekday (opener, closer), each server's usual
section (section), teams (pair — pairs and trios), who keeps running into
overtime (ot_risk), closes that run past their end (end_overrun), the
owner's redos and discards (redo_reason), and the other learners' facts held
here as those learners decide them (staff_avoid / staff_prefer, reliability,
daypart_outcome, could_hold).
"""
import json
import logging
import math
import re
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports): a
    test's patched models.get_conn must reach this module too, and a
    db_path equal to the default means "whatever models uses now"."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


# ── vocabulary ────────────────────────────────────────────────────────────

ORIGINS = ("manager", "cavnar", "staff", "system", "measured", "owner")
PHASES = ("pre_publish", "post_publish", "as_run")
AUTHORITIES = ("principal", "delegate", "system", "admin")
STATUSES = ("candidate", "active", "rule", "retest", "retired", "dormant")
ENFORCEMENTS = ("prompt", "soft", "hard")

# The raw log is temporary: the memory is what outlives it (ops retention).
OBSERVATION_RETAIN_DAYS = 400
# How far back the learners read their sources — the edit learners' own
# window (schedule_versions.LEARN_WEEKS), every week weighted by its age.
WINDOW_DAYS = 168

# The ladder (raw_L §3 B).
CANDIDATE_MIN_HITS = 2
CANDIDATE_MIN_RATE = 0.5
ACTIVE_CONFIDENCE = 0.6
RETIRE_CONFIDENCE = 0.3
# Two hand reversals this close together retire a habit (the standing
# patterns' own rule, schedule_versions.STANDING_OVERRIDE_WINDOW_DAYS).
REVERSAL_WINDOW_DAYS = 56

# Half-lives by fact class (raw_L §3 B): a person on a slot 120 days, a
# role's headcount or start time 180, a pairing or an opener 180,
# reliability 90 (as staff_settings reads it).
PERSON_HALF_LIFE_DAYS = 120
SLOT_HALF_LIFE_DAYS = 180
HALF_LIFE_DAYS = {
    "moved_off": PERSON_HALF_LIFE_DAYS, "moved_on": PERSON_HALF_LIFE_DAYS, "role_change": PERSON_HALF_LIFE_DAYS,
    "leader_swap": SLOT_HALF_LIFE_DAYS, "retime_start": SLOT_HALF_LIFE_DAYS, "retime_end": SLOT_HALF_LIFE_DAYS,
    "headcount_add": SLOT_HALF_LIFE_DAYS, "headcount_cut": SLOT_HALF_LIFE_DAYS,
    "opener": SLOT_HALF_LIFE_DAYS, "closer": SLOT_HALF_LIFE_DAYS, "section": SLOT_HALF_LIFE_DAYS,
    "pair": SLOT_HALF_LIFE_DAYS,
    "ot_risk": PERSON_HALF_LIFE_DAYS, "end_overrun": SLOT_HALF_LIFE_DAYS, "redo_reason": SLOT_HALF_LIFE_DAYS,
    "staff_avoid": PERSON_HALF_LIFE_DAYS, "staff_prefer": PERSON_HALF_LIFE_DAYS, "reliability": 90,
    "daypart_outcome": SLOT_HALF_LIFE_DAYS, "could_hold": 365,
}

# What each kind is a fact about, for the owner's screen.
FACT_CLASS = {
    "moved_off": "habit", "moved_on": "habit", "retime_start": "habit", "retime_end": "habit",
    "headcount_add": "habit", "headcount_cut": "habit", "role_change": "habit", "leader_swap": "habit",
    "opener": "ownership", "closer": "ownership", "section": "ownership", "pair": "team", "ot_risk": "overtime",
    "end_overrun": "overtime", "redo_reason": "rejection", "staff_avoid": "staff", "staff_prefer": "staff",
    "reliability": "attendance", "daypart_outcome": "outcome", "could_hold": "mentoring",
}
PATTERN_KINDS = ("moved_off", "moved_on", "retime_start", "retime_end", "headcount_add", "headcount_cut",
                 "role_change", "leader_swap")
# What an ACTIVE memory of each kind is held to. Candidates are prompt-only.
KIND_ENFORCEMENT = {k: "soft" for k in PATTERN_KINDS}
KIND_ENFORCEMENT.update({"opener": "soft", "closer": "soft", "section": "prompt", "pair": "soft",
                         "ot_risk": "soft",
                         "end_overrun": "soft", "redo_reason": "prompt", "staff_avoid": "soft",
                         "staff_prefer": "soft", "reliability": "soft", "daypart_outcome": "prompt",
                         "could_hold": "prompt"})
# Facts bound in code through another signal than signals["learned"] — held
# there once, never a second cost here: learned headcount is IN the
# requirements table (labor.apply_learned_headcount), what staff drop and
# claim is signals["learned_preferences"] (L-19), attendance is
# signals["reliability"]. Their memory rows say what is learned; the binding
# stays where it was.
BOUND_ELSEWHERE = {
    "headcount_add": "the requirements table (labor.apply_learned_headcount)",
    "headcount_cut": "the requirements table (labor.apply_learned_headcount)",
    "staff_avoid": 'signals["learned_preferences"]', "staff_prefer": 'signals["learned_preferences"]',
    "reliability": 'signals["reliability"]',
}
# The kinds enforced_signals hands the passes.
SIGNAL_KINDS = tuple(k for k, e in KIND_ENFORCEMENT.items() if e == "soft" and k not in BOUND_ELSEWHERE)
# Kinds a learner rebuilds each run, and the learner that owns them — a
# learner that failed tonight leaves its kinds as they were.
_MIRROR_KINDS = ("staff_avoid", "staff_prefer", "reliability", "daypart_outcome", "could_hold")

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MEAL = {"morning": "lunch/day", "night": "dinner/night", "late": "late night"}


def half_life(kind) -> int:
    return HALF_LIFE_DAYS.get(kind, SLOT_HALF_LIFE_DAYS)


def recency_weight(age_days, half_life_days) -> float:
    """1.0 for evidence from today, 0.5 one half-life ago (the edit
    learners' own rule, schedule_versions.recency_weight)."""
    try:
        return 0.5 ** (max(0.0, float(age_days or 0)) / float(half_life_days))
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0


def wilson_lower(hits, n, z=1.645) -> float:
    """The Wilson lower bound of hits/n at 90% — 2 of 2 is far less sure
    than 20 of 20; weighted counts allowed (schedule_versions.wilson_lower)."""
    try:
        n, hits = float(n), float(hits)
    except (TypeError, ValueError):
        return 0.0
    if n <= 0:
        return 0.0
    p = min(1.0, max(0.0, hits / n))
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(max(0.0, p * (1 - p) / n + z2 / (4 * n * n)))
    return max(0.0, (centre - margin) / (1 + z2 / n))


def _nk(name) -> str:
    """staff_settings.name_key: one person however the name was typed."""
    return " ".join(str(name or "").split()).casefold()


def _weekday(iso) -> str:
    try:
        return datetime.strptime(str(iso or "")[:10], "%Y-%m-%d").strftime("%A")
    except ValueError:
        return ""


def _minutes(value):
    """Minutes past midnight from "4:00pm", "4pm", "16:00" or "16:00:00"."""
    raw = str(value or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(raw, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    return None


def _daypart(start) -> str:
    from schedule_rules import daypart_of
    return daypart_of(start or "")


def _end_part(row) -> str:
    """The daypart a shift ENDS in — what a close is about: a double that
    started at 10am and closed at 11pm closed the night."""
    s, e = _minutes(row.get("shift_start")), _minutes(row.get("shift_end"))
    if s is None or e is None:
        return _daypart(row.get("shift_start"))
    e = e + 1440 if e <= s else e
    return "night" if e > 15 * 60 else "morning"


def _clock(minutes, like=""):
    """`minutes` past midnight written in the clock style of `like` ("10:30pm"
    for a 12-hour row, "22:30" for a 24-hour one)."""
    m = int(minutes) % (24 * 60)
    h, mm = divmod(m, 60)
    if re.search(r"[ap]m", str(like or ""), re.I) or not like:
        suffix = "am" if h < 12 else "pm"
        return f"{(h % 12) or 12}:{mm:02d}{suffix}"
    return f"{h:02d}:{mm:02d}"


def _pct(x) -> str:
    return f"{int(round(float(x) * 100))}%" if x is not None else "—"


def _dumps(value):
    if value is None:
        return None
    return json.dumps(value, default=str, sort_keys=True)


def _loads(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _today(restaurant_id, today=None) -> date:
    if today is not None:
        return today if isinstance(today, date) and not isinstance(today, datetime) else (
            today.date() if isinstance(today, datetime) else date.fromisoformat(str(today)[:10]))
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        return date.today()


def _capture(e, context, restaurant_id=None):
    """A failure that matters reaches the daily digest (CLAUDE.md: no silent
    handler around a write)."""
    log.warning("schedule_memory %s failed for rid=%s: %s", context, restaurant_id, e)
    try:
        import ops
        ops.capture(e, job="schedule_memory", context=f"{context} restaurant_id={restaurant_id}",
                    restaurant_id=restaurant_id)
    except Exception as ce:
        log.warning("schedule_memory: capture failed too: %s", ce)


# ── the tables (boot DDL, models.init_db) ─────────────────────────────────

def init_schedule_memory(db_path=None):
    """The observation log, the memory and the per-restaurant run state —
    created at boot (models.init_db), never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_observations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            kind           TEXT    NOT NULL,
            history_id     INTEGER,
            week_start     TEXT,
            date           TEXT,
            daypart        TEXT,
            role           TEXT,
            person         TEXT,
            person_id      INTEGER,
            value_json     TEXT,
            origin         TEXT    NOT NULL,
            phase          TEXT    NOT NULL,
            authority      TEXT    NOT NULL,
            editor         TEXT,
            source         TEXT,
            fact_key       TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT
        )""")
        # A measured fact is re-recorded in place; NULL keys (events) never
        # collide — SQLite keeps NULLs distinct in a unique index.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_sched_obs_fact ON schedule_observations(restaurant_id, fact_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_obs_kind ON schedule_observations(restaurant_id, kind, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_obs_created ON schedule_observations(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_memory (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id           INTEGER NOT NULL REFERENCES restaurants(id),
            memory_key              TEXT    NOT NULL,
            kind                    TEXT    NOT NULL,
            fact_class              TEXT    NOT NULL,
            person                  TEXT,
            person_id               INTEGER,
            role                    TEXT,
            day                     TEXT,
            daypart                 TEXT,
            value_json              TEXT,
            text                    TEXT,
            opportunities           REAL    NOT NULL DEFAULT 0,
            hits                    REAL    NOT NULL DEFAULT 0,
            weeks                   INTEGER NOT NULL DEFAULT 0,
            of_weeks                INTEGER NOT NULL DEFAULT 0,
            misses_by_hand          INTEGER NOT NULL DEFAULT 0,
            last_confirmed_by_hand  TEXT,
            first_seen              TEXT,
            last_seen               TEXT,
            confidence              REAL,
            half_life_days          INTEGER,
            status                  TEXT    NOT NULL DEFAULT 'candidate',
            enforcement             TEXT    NOT NULL DEFAULT 'prompt',
            source                  TEXT,
            origin                  TEXT,
            authority_of_evidence   TEXT,
            retired_reason          TEXT,
            retired_at              TEXT,
            rule_ref                TEXT,
            owner_said              TEXT,
            owner_said_by           TEXT,
            owner_said_at           TEXT,
            created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(restaurant_id, memory_key)
        )""")
        # Whose answer owner_said is (schedule re-audit 10/4/26 LEARN-3):
        # permissions.answer_authority of the login that kept it or let it
        # go — principal (an account holder) or delegate (a manager or a
        # member). Only a principal's answer is the owner's; a delegate's
        # never overrides it. NULL on a row answered before the column:
        # read as the owner's, so no answer the owner gave is lost.
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_memory)").fetchall()}
        if "owner_said_authority" not in _cols:
            try:
                conn.execute("ALTER TABLE schedule_memory ADD COLUMN owner_said_authority TEXT")
            except Exception as _e:
                if "duplicate column" not in str(_e).lower():
                    raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_memory_status ON schedule_memory(restaurant_id, status)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_memory_state (
            restaurant_id     INTEGER PRIMARY KEY REFERENCES restaurants(id),
            consolidated_at   TEXT,
            stats_json        TEXT
        )""")
        conn.commit()
    finally:
        conn.close()


# ── the observation log ────────────────────────────────────────────────────

def _norm(value, allowed, default):
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def observe(restaurant_id, kind, *, week_start=None, date=None, daypart=None, role=None, person=None,
            value=None, origin="manager", phase="pre_publish", authority="principal", editor=None,
            source=None, history_id=None, db_path=None, fact_key=None, person_id=None):
    """Record one observation. Never raises into the caller's work: a write
    that failed is captured (ops.capture) and None returned.

    An event (an edit, a redo, a publish) is appended; a MEASURED fact passes
    `fact_key` — the same fact recorded again (the outcomes job re-reads the
    last weeks, the nightly join the last seven nights) replaces its value
    instead of adding a row. Returns the row id."""
    try:
        k = str(kind or "").strip()[:40]
        if not restaurant_id or not k:
            return None
        values = (restaurant_id, k, int(history_id) if history_id not in (None, "") else None,
                  str(week_start)[:10] if week_start else None, str(date)[:10] if date else None,
                  (str(daypart).strip()[:20] or None) if daypart else None,
                  (" ".join(str(role).split())[:80] or None) if role else None,
                  (" ".join(str(person).split())[:120] or None) if person else None,
                  int(person_id) if person_id not in (None, "") else None, _dumps(value),
                  _norm(origin, ORIGINS, "manager"), _norm(phase, PHASES, "pre_publish"),
                  _norm(authority, AUTHORITIES, "principal"),
                  (str(editor)[:120] or None) if editor else None, (str(source)[:60] or None) if source else None,
                  (str(fact_key)[:300] or None) if fact_key else None)
        conn = get_conn(db_path)
        try:
            cur = conn.execute(
                "INSERT INTO schedule_observations (restaurant_id, kind, history_id, week_start, date, daypart, role, "
                "person, person_id, value_json, origin, phase, authority, editor, source, fact_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, fact_key) DO UPDATE SET "
                "value_json=excluded.value_json, history_id=COALESCE(excluded.history_id, history_id), "
                "week_start=COALESCE(excluded.week_start, week_start), role=COALESCE(excluded.role, role), "
                "person=COALESCE(excluded.person, person), origin=excluded.origin, phase=excluded.phase, "
                "authority=excluded.authority, source=COALESCE(excluded.source, source), "
                "updated_at=datetime('now')", values)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        _capture(e, f"observe {kind}", restaurant_id)
        return None


def observations(restaurant_id, kinds=None, since=None, db_path=None, counted_only=False) -> list:
    """The log's rows (newest first) as dicts with `value` decoded. With
    `counted_only`, only what may teach a manager habit: not an admin's word
    and not Cavnar AI's own change (raw_L §3 F)."""
    sql = "SELECT * FROM schedule_observations WHERE restaurant_id=?"
    args = [restaurant_id]
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args += kinds
    if since:
        sql += " AND COALESCE(date, substr(created_at, 1, 10)) >= ?"
        args.append(str(since)[:10])
    if counted_only:
        sql += " AND authority <> 'admin' AND origin <> 'cavnar'"
    conn = get_conn(db_path)
    try:
        rows = conn.execute(sql + " ORDER BY id DESC", args).fetchall()
    except Exception as e:
        log.warning("schedule_memory: observations unreadable for %s: %s", restaurant_id, e)
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["value"] = _loads(d.pop("value_json", None)) or {}
        out.append(d)
    return out


def latest(restaurant_id, kind, db_path=None):
    """The newest observation of `kind` ({..., value, created_at}), or None."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM schedule_observations WHERE restaurant_id=? AND kind=? "
                         "ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT 1",
                         (restaurant_id, kind)).fetchone()
    except Exception as e:
        log.warning("schedule_memory: %s unreadable for %s: %s", kind, restaurant_id, e)
        return None
    finally:
        conn.close()
    if r is None:
        return None
    d = dict(r)
    d["value"] = _loads(d.pop("value_json", None)) or {}
    return d


# ── what the learners read, once per restaurant per run ───────────────────

# Overtime (L-16): a payroll week past the line by more than this is
# overtime worked; a week counts toward the evidence once the person was
# within OT_NEAR_HOURS of the line, planned or worked — a 20-hour week can
# never run into overtime and is not a week they "didn't".
OT_TOLERANCE_HOURS = 0.25
OT_NEAR_HOURS = 32.0
OT_HEADROOM_MAX = 8.0
# A close that ran this far past its scheduled end stayed late (L-16); the
# pad a role's closes get is the typical overrun rounded to 15 minutes,
# never more than END_PAD_MAX_MINUTES.
STAYED_LATE_MINUTES = 15
END_PAD_STEP = 15
END_PAD_MAX_MINUTES = 60
# A punch more than this far from a row's start is another shift.
PUNCH_MATCH_MINUTES = 180
# Openers (L-22): everyone who clocked in within this of the role's first
# clock-in that day opened it.
OPENER_TIE_MINUTES = 15
# Teams (L-21): pairs and trios need this many shared shifts with a
# reading, and their share of shifts that ran well must sit this far above
# (or below) the restaurant's own.
PAIR_MIN_SHARED = 6
PAIR_MIN_LIFT = 0.15
TRIO_MAX_PEOPLE = 12
# Every pair and trio of each night's crew is tested at once — hundreds of
# groups — so a team is a finding only when its one-sided binomial test
# against the restaurant's own rate survives Holm's correction for all the
# groups tested at PAIR_ALPHA (schedule re-audit 10/4/26 LEARN-7: with a
# fixed bar, about one restaurant in four learned and enforced a "team"
# from outcomes that had nothing to do with who worked). The rate the team
# is held against is the higher of the restaurant's and the one on the
# nights some of them worked without the others (PAIR_MIN_APART of those
# at least): a person who lifts every night they work is not a team.
PAIR_ALPHA = 0.05
PAIR_MIN_APART = 3
GOOD_REVIEW = 4.0
POS_SOURCES = ("rpower", "toast", "square", "clover")


def chosen_closers_of(restaurant_id, restaurant=None, db_path=None) -> dict:
    """{role family: {name key}} — the closers the owner chose for each role
    (schedule_rules.chosen_closers: the closer flags in the roles the owner
    chose, the draft's own closer rule), keyed as this module keys a person
    (_nk). A learned "closer" is about one of THEM (schedule re-audit
    10/4/26 LEARN-10: a closer is the person chosen to close, owner's rule
    2). {} when none is marked or the read failed (logged): no role then has
    chosen closers, and nothing is filtered."""
    try:
        import models
        import schedule_rules
        got = schedule_rules.chosen_closers(restaurant_id, restaurant=restaurant, db_path=db_path or models.DB_PATH)
    except Exception as e:
        log.warning("schedule_memory: chosen closers unreadable for %s: %s", restaurant_id, e)
        return {}
    return {fam: {_nk(n) for n in names} for fam, names in (got or {}).items() if fam and names}


def _not_a_chosen_closer(kind, role, person, closers, families=None) -> bool:
    """A learned closer for a role whose closers the owner chose, about
    somebody who is not one of them (LEARN-10)."""
    if kind != "closer" or not closers:
        return False
    fam = _fam(role, families) if role else ""
    chosen = closers.get(fam) or set()
    return bool(chosen) and _nk(person) not in chosen


class _Ctx:
    """One restaurant's sources for one consolidation, each read once."""

    def __init__(self, restaurant_id, today=None, db_path=None):
        import models
        self.rid = restaurant_id
        self.db = db_path
        self.today = _today(restaurant_id, today)
        self.since = (self.today - timedelta(days=WINDOW_DAYS)).isoformat()
        self.restaurant = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
        try:
            import schedule_rules
            self.families = schedule_rules.role_families(self.restaurant) or {}
        except Exception as e:
            log.warning("schedule_memory: role families unreadable for %s: %s", restaurant_id, e)
            self.families = {}
        try:
            self.salaried = {models.salaried_name_key(s["name"]) for s in models.salaried_staff(self.restaurant)}
        except Exception:
            self.salaried = set()
        self.week_start_day = int(getattr(self.restaurant, "week_start_day", 0) or 0)
        self._cache = {}

    def fam(self, role) -> str:
        from shift_quality import role_family
        return role_family(role, self.families)

    def age(self, iso) -> int:
        try:
            return max(0, (self.today - date.fromisoformat(str(iso)[:10])).days)
        except ValueError:
            return 0

    def _get(self, name, fn):
        if name not in self._cache:
            self._cache[name] = fn()
        return self._cache[name]

    def weeks(self) -> list:
        """The weeks a person of the restaurant finished with, as the manager
        settled them before they went out (schedule_versions.learning_weeks —
        the original draft, the manager's own pre-publish changes; Cavnar
        AI's and an admin's out, the automatic publish nobody's)."""
        def read():
            import schedule_versions
            kw = {"db_path": self.db} if self.db else {}
            return schedule_versions.learning_weeks(self.rid, today=self.today, **kw)
        return self._get("weeks", read)

    def punches(self) -> list:
        """The window's worked shifts (shift_facts), each person spelled as
        they are now, the salaried left out (they don't clock in)."""
        def read():
            import shift_facts
            rows = shift_facts.rows(self.rid, since=self.since,
                                    until=(self.today - timedelta(days=1)).isoformat(), db_path=self.db)
            return [r for r in rows if _nk(r.get("employee")) not in self.salaried]
        return self._get("punches", read)

    def published(self) -> list:
        """[(history_id, week_start, rows)] — each published week in the
        window as staff last had it (schedule_history.schedule_csv), names
        read as the people they mean now."""
        def read():
            from schedule_versions import rows_from_csv, canonical_rows
            conn = get_conn(self.db)
            try:
                hist = conn.execute(
                    "SELECT id, week_start, schedule_csv FROM schedule_history h WHERE restaurant_id=? AND "
                    "published_at IS NOT NULL AND superseded_by IS NULL AND week_end >= ? AND week_start < ? AND NOT "
                    "EXISTS (SELECT 1 FROM schedule_history n WHERE n.restaurant_id=h.restaurant_id AND "
                    "n.week_start=h.week_start AND n.published_at IS NOT NULL AND n.id > h.id) ORDER BY week_start",
                    (self.rid, self.since, self.today.isoformat())).fetchall()
            finally:
                conn.close()
            weeks = [(h["id"], h["week_start"], rows_from_csv(h["schedule_csv"] or "")) for h in hist]
            try:
                kw = {"db_path": self.db} if self.db else {}
                canon = canonical_rows(self.rid, [w[2] for w in weeks], **kw)
                weeks = [(w[0], w[1], canon[i]) for i, w in enumerate(weeks)]
            except Exception as e:
                log.warning("schedule_memory: published names not resolved for %s: %s", self.rid, e)
            return weeks
        return self._get("published", read)

    def cavnar_dates(self) -> set:
        """The dates a published week Cavnar AI drafted covers — evidence
        there is read through learning_weeks (the manager's own hand), never
        as the restaurant's own scheduling."""
        def read():
            conn = get_conn(self.db)
            try:
                hist = conn.execute(
                    "SELECT week_start, week_end FROM schedule_history h WHERE restaurant_id=? AND week_end >= ? "
                    "AND EXISTS (SELECT 1 FROM schedule_versions v WHERE v.history_id=h.id AND v.reason='generated')",
                    (self.rid, self.since)).fetchall()
            except Exception:
                hist = []
            finally:
                conn.close()
            out = set()
            for h in hist:
                try:
                    d, end = date.fromisoformat(str(h["week_start"])[:10]), date.fromisoformat(str(h["week_end"])[:10])
                except (TypeError, ValueError):
                    continue
                while d <= end:
                    out.add(d.isoformat())
                    d += timedelta(days=1)
            return out
        return self._get("cavnar_dates", read)

    def closers(self) -> dict:
        """{role family: {name key}} — the people the owner chose to close
        each role (schedule_rules.chosen_closers, as the draft's closer rule
        reads them). {} when nobody is marked to close."""
        def read():
            return chosen_closers_of(self.rid, restaurant=self.restaurant, db_path=self.db)
        return self._get("closers", read)

    def previous(self, kind) -> list:
        """This restaurant's memory rows of `kind` as they stood before
        tonight's read (value decoded) — what a measured learner checks its
        own effect against (LEARN-2: a pad must not erase its own evidence)."""
        def read():
            conn = get_conn(self.db)
            try:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM schedule_memory WHERE restaurant_id=? AND kind=?", (self.rid, kind)).fetchall()]
            except Exception as e:
                log.warning("schedule_memory: previous %s unreadable for %s: %s", kind, self.rid, e)
                rows = []
            finally:
                conn.close()
            for r in rows:
                r["value"] = _loads(r.get("value_json")) or {}
            return rows
        return self._get(f"previous:{kind}", read)

    def person_ids(self) -> dict:
        def read():
            conn = get_conn(self.db)
            try:
                return {r["name_key"]: r["id"] for r in conn.execute(
                    "SELECT id, name_key FROM people WHERE restaurant_id=? AND merged_into IS NULL", (self.rid,))}
            except Exception:
                return {}
            finally:
                conn.close()
        return self._get("person_ids", read)

    def presence(self) -> tuple:
        """({name key: newest date worked or published}, newest date, {name
        key of somebody marked inactive})."""
        def read():
            last = {}
            for r in self.punches():
                k, d = _nk(r.get("employee")), str(r.get("date") or "")[:10]
                if k and d > last.get(k, ""):
                    last[k] = d
            for _hid, _ws, rows in self.published():
                for r in rows:
                    k, d = _nk(r.get("employee")), str(r.get("date") or "")[:10]
                    if k and d > last.get(k, ""):
                        last[k] = d
            inactive = set()
            conn = get_conn(self.db)
            try:
                inactive = {_nk(r["employee_name"]) for r in conn.execute(
                    "SELECT employee_name FROM staff_settings WHERE restaurant_id=? AND active=0", (self.rid,))}
            except Exception:
                inactive = set()
            finally:
                conn.close()
            return last, (max(last.values()) if last else None), inactive
        return self._get("presence", read)

    def away(self, name) -> bool:
        """Somebody with no shift in people.MEMORY_GONE_DAYS of the
        restaurant's own newest (a sync that stopped is not everyone
        leaving), or marked inactive: their memories sleep (dormant)."""
        if not name:
            return False
        last, newest, inactive = self.presence()
        k = _nk(name)
        if k in inactive:
            return True
        if not newest:
            return False
        try:
            import people
            gone = int(people.MEMORY_GONE_DAYS)
        except Exception:
            gone = 21
        edge = (date.fromisoformat(newest) - timedelta(days=gone)).isoformat()
        return last.get(k, "") < edge


def _evidence(kind) -> dict:
    return {"kind": kind, "opps_w": 0.0, "hits_w": 0.0, "opps": 0, "hits": 0, "hand": 0, "hand_dates": [],
            "miss_dates": [], "weeks": set(), "hit_weeks": set(), "last": "", "first": "", "names": {},
            "roles": {}, "starts": [], "kept_dates": []}


def _note(e, iso, w, hit, hand=False, person=None, role=None, start=None, week=None):
    e["opps_w"] += w
    e["opps"] += 1
    if week:
        e["weeks"].add(week)
    if hit:
        e["hits_w"] += w
        e["hits"] += 1
        if week:
            e["hit_weeks"].add(week)
        if hand:
            e["hand"] += 1
            e["hand_dates"].append(iso)
        if start is not None:
            e["starts"].append(start)
    if iso:
        e["last"] = max(e["last"], iso)
        e["first"] = min(e["first"], iso) if e["first"] else iso
    if person:
        e["names"][person] = max(e["names"].get(person, ""), iso or "")
    if role:
        e["roles"][role] = e["roles"].get(role, 0) + 1


def _display(e, field="names") -> str:
    """The latest spelling of a name (by date), or the most worked role (by
    count)."""
    d = e.get(field) or {}
    return max(d.items(), key=lambda kv: kv[1])[0] if d else ""


def _week_of(iso) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except ValueError:
        return ""
    return (d - timedelta(days=d.weekday())).isoformat()


def _confidence(e, kind, ctx, habit=True) -> float:
    """Wilson × recency: the lower bound of the recency-weighted hits over
    the recency-weighted opportunities — and for a manager habit, times the
    half-life decay since a hand last confirmed it (the standing patterns'
    rule, L-30): weeks the draft merely carried it keep it, they never
    renew it. What was MEASURED on the floor renews it too (`kept_dates`:
    the punches show the person really opened or closed a day Cavnar AI
    drafted — schedule re-audit 10/4/26 LEARN-11: once Cavnar AI drafted
    every week nothing renewed an opener's decay, so a habit the manager
    kept every week fell out of force about 100 days in). A habit is still
    BORN only by hand (qualifies_new counts hand dates alone)."""
    conf = wilson_lower(e["hits_w"], e["opps_w"])
    if habit:
        hand = _renewed(e)
        conf *= recency_weight(ctx.age(hand) if hand else 2 * half_life(kind), half_life(kind))
    return round(conf, 3)


def _renewed(e) -> str:
    """The newest date a hand confirmed the fact or the floor showed it
    (hand_dates, kept_dates) — what a habit's decay runs from."""
    return max(list(e.get("hand_dates") or []) + list(e.get("kept_dates") or []) or [""])


def _rate(e) -> float:
    return round(e["hits_w"] / e["opps_w"], 3) if e["opps_w"] > 0 else 0.0


def _person_token(ctx, name) -> str:
    """The person part of a memory key: their people id when they have one
    (a rename keeps the row), else their name key."""
    k = _nk(name)
    pid = ctx.person_ids().get(k)
    return f"p{pid}" if pid else k


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _memory(ctx, key, kind, e, *, person=None, role=None, day=None, daypart=None, value=None, text="",
            source="", origin="manager", habit=True, may_activate=True, min_rate=CANDIDATE_MIN_RATE,
            authority="principal") -> dict:
    """One learned fact as the writer settles it (_settle)."""
    rate = _rate(e)
    conf = _confidence(e, kind, ctx, habit=habit)
    return {"key": key, "kind": kind, "person": person, "role": role, "day": day, "daypart": daypart,
            "value": value or {}, "text": text, "opps_w": round(e["opps_w"], 3), "hits_w": round(e["hits_w"], 3),
            "opps": e["opps"], "hits": e["hits"], "weeks": len(e["hit_weeks"]), "of_weeks": len(e["weeks"]),
            "hand_dates": sorted(e["hand_dates"]), "miss_dates": sorted(e["miss_dates"]),
            "kept_dates": sorted(e.get("kept_dates") or []),
            "first": e["first"], "last": e["last"], "rate": rate, "confidence": conf, "habit": habit,
            # A habit is BORN only from a hand (or the restaurant's own
            # scheduling before Cavnar AI drafted): a draft choice the
            # manager merely left in place keeps it alive, never starts it.
            "qualifies_new": (e["hand"] if habit else e["hits"]) >= CANDIDATE_MIN_HITS and rate >= min_rate,
            "qualifies": e["hits"] >= CANDIDATE_MIN_HITS and rate >= min_rate,
            "may_activate": may_activate, "source": source, "origin": origin, "authority": authority,
            "person_id": ctx.person_ids().get(_nk(person)) if person else None}


# ── the learners ───────────────────────────────────────────────────────────

def _pattern_value(p) -> dict:
    kind = p.get("kind")
    out = {}
    if kind in ("moved_off", "moved_on"):
        out["slot"] = "off" if kind == "moved_off" else "on"
    elif kind in ("retime_start", "retime_end"):
        out["time"] = p.get("time")
    elif kind in ("headcount_add", "headcount_cut"):
        out["delta"] = p.get("delta")
    elif kind == "role_change":
        out.update(role=p.get("role"), was_role=p.get("was_role"))
    elif kind == "leader_swap":
        out["names"] = p.get("names") or []
    if p.get("editors"):
        out["editors"] = p.get("editors")
    return out


_STANDING_STATUS = {"active": "active", "retest": "retest", "retired": "retired", "ruled": "rule",
                    "dormant": "dormant"}


def _fresh_keep(said_at, kind, ctx) -> bool:
    """The owner's Keep, said less than two half-lives ago."""
    said = str(said_at or "")[:10]
    return bool(said) and ctx.age(said) < 2 * half_life(kind)


def _learn_patterns(ctx, patterns=None) -> list:
    """The manager's edit habits — every standing pattern with its evidence
    (schedule_standing_patterns, kept by schedule_versions.
    refresh_standing_patterns: opportunities, hits, reversals, the last hand
    confirmation, its own confidence and status) and every live pattern the
    window shows that has not stood yet — migrated in whole, nothing lost:
    a retired, ruled, dormant or re-tested pattern keeps that status here.
    `patterns`: (patterns, conflicts) from schedule_versions.
    patterns_for_draft when the caller already read them. A standing
    pattern is active (enforced) once its confidence clears
    ACTIVE_CONFIDENCE; below it, a candidate the prompt alone carries."""
    import schedule_versions as sv
    import schedule_intel as si
    kw = {"db_path": ctx.db} if ctx.db else {}
    standing = sv.standing_patterns(ctx.rid, include_retired=True, **kw)
    if patterns is None:
        live_all, conflicts = sv.patterns_for_draft(ctx.rid, **kw)
    else:
        live_all, conflicts = patterns
    dismissed = si.dismissed_patterns(ctx.rid, **kw)
    ctx._cache["dismissed_patterns"] = dismissed
    # Every column the standing row keeps, carried as it is (the migration
    # loses nothing): the counts and dates the owner's screen and the
    # standing lifecycle read beside the evidence above.
    conn = get_conn(ctx.db)
    try:
        raw = {r["pattern_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM schedule_standing_patterns WHERE restaurant_id=?", (ctx.rid,)).fetchall()}
    finally:
        conn.close()
    out, keys = [], set()
    clash = {(_nk(c.get("employee")), c.get("day"), c.get("daypart")) for c in (conflicts or [])}
    for s in standing:
        kind = s["kind"]
        e = _evidence(kind)
        e.update(opps=int(s.get("opportunities") or 0), hits=int(s.get("hits") or 0),
                 opps_w=float(s.get("opportunities") or 0), hits_w=float(s.get("hits") or 0),
                 first=s.get("first_learned_iso") or "", last=s.get("last_confirmed_iso") or "")
        hand = s.get("last_hand_iso")
        e["hand_dates"] = [hand] if hand else []
        m = _memory(ctx, "pattern:" + s["key"], kind, e, person=s.get("employee"), role=s.get("role"),
                    day=s.get("day"), daypart=s.get("daypart"), value=_pattern_value(s),
                    text=s.get("text") or "", source=("owner_said" if s.get("source") == "owner_said"
                                                      else "manager_edits"),
                    origin=("owner" if s.get("source") == "owner_said" else "manager"))
        # The standing row's own confidence (Wilson × decay since the last
        # hand confirmation, schedule_versions.standing_confidence).
        if s.get("confidence") is not None:
            m["confidence"] = round(float(s["confidence"]), 3)
        m["misses_by_hand"] = int(s.get("times_overridden") or 0)
        m["last_hand"] = hand
        status = _STANDING_STATUS.get(s["status"], "candidate")
        row = raw.get(s["key"]) or {}
        # The owner's own one-tap "always" (source owner_said, L-35) is their
        # word, applied at once; so is the owner's Keep (owner_kept_at, set
        # only for an account holder's answer — schedule re-audit 10/4/26
        # LEARN-4: a Keep on a habit under the line only stamped a hand date,
        # and the habit stayed "Learning" while the owner was told it was
        # kept), held for two half-lives from when they said it, as _settle
        # holds the other kinds. A learned row binds the passes only once
        # its confidence clears the line.
        owner_word = s.get("source") == "owner_said" or _fresh_keep(row.get("owner_kept_at"), kind, ctx)
        if status == "rule" and not _pattern_rule_alive(ctx, s):
            _unrule_pattern(ctx, s["key"])
            status = "active"
        if status == "active" and m["confidence"] < ACTIVE_CONFIDENCE and not owner_word:
            status = "candidate"
        reason = s.get("retired_reason") if status == "retired" else None
        if s["key"] in dismissed:
            status, reason = "retired", "dismissed"
        if kind in ("moved_off", "moved_on") and (_nk(s.get("employee")), s.get("day"), s.get("daypart")) in clash \
                and status in ("active", "candidate"):
            # Two editors pull opposite ways: held for the owner, not enforced.
            status = "candidate"
            m["value"]["conflict"] = True
        if status == "rule":
            m["value"]["rule"] = (s.get("rule") or {}).get("note")
        m["value"]["standing"] = {f: row.get(f) for f in (
            "times_applied", "times_confirmed", "times_overridden", "last_overridden", "checked_through",
            "first_learned", "last_confirmed", "retired_at", "retired_week", "retest_since", "last_retest_end",
            "retests", "dormant_at", "rule_note", "ruled_by", "source", "authority", "status", "owner_kept_at",
            "answer_authority") if f in row}
        if row.get("person_id"):
            m["person_id"] = row["person_id"]
        m["status"], m["retired_reason"] = status, reason
        m["authority"] = s.get("authority") or "principal"
        out.append(m)
        keys.add(s["key"])
    for p in live_all or []:
        if p.get("standing"):
            continue
        k = si.pattern_key(p)
        if k in keys:
            continue
        kind = p.get("kind")
        e = _evidence(kind)
        e.update(opps=int(p.get("opportunities") or p.get("times") or 0), hits=int(p.get("times") or 0),
                 opps_w=float(p.get("opportunities") or p.get("times") or 0), hits_w=float(p.get("times") or 0),
                 last=str(p.get("last_week_start") or "")[:10])
        e["hand_dates"] = [e["last"]] if e["last"] else []
        m = _memory(ctx, "pattern:" + k, kind, e, person=p.get("employee"), role=p.get("role"), day=p.get("day"),
                    daypart=p.get("daypart"), value=_pattern_value(p), text=p.get("text") or "",
                    source="manager_edits")
        if p.get("confidence") is not None:
            m["confidence"] = round(float(p["confidence"]), 3)
        if p.get("rate") is not None:
            m["rate"] = round(float(p["rate"]), 3)
        status = "active" if m["confidence"] >= ACTIVE_CONFIDENCE else "candidate"
        m["status"], m["retired_reason"] = status, None
        out.append(m)
        keys.add(k)
    return out


def _by_role_day(ctx, rows) -> dict:
    """{(date, role family): [(start minutes, name, role, start text)]}."""
    by = {}
    for r in rows or []:
        d = str(r.get("date") or "")[:10]
        name = " ".join(str(r.get("employee") or "").split())
        fam = ctx.fam(r.get("role"))
        m = _minutes(r.get("shift_start"))
        if len(d) != 10 or not name or not fam or m is None:
            continue
        by.setdefault((d, fam), []).append((m, name, (r.get("role") or "").strip(), r.get("shift_start") or "",
                                            _minutes(r.get("shift_end"))))
    return by


def _end_adj(m, end):
    """A row's end in minutes, read across midnight from its start."""
    if end is None:
        return None
    return end + (1440 if end <= m else 0)


def _edge_of(group, kind) -> set:
    """The people who open (first in, OPENER_TIE_MINUTES of the first) or
    close (last out, as close to the last) a role on one day."""
    if kind == "opener":
        first = min(m for m, *_r in group)
        return {_nk(n) for m, n, *_r in group if m - first <= OPENER_TIE_MINUTES}
    ends = [(_end_adj(m, e), n) for m, n, _r, _s, e in group if e is not None]
    if not ends:
        return set()
    last = max(x for x, _n in ends)
    return {_nk(n) for x, n in ends if last - x <= OPENER_TIE_MINUTES}


def _openers_of(group) -> set:
    return _edge_of(group, "opener")


def _learn_edges(ctx, kind) -> list:
    """Who opens, or closes, each role on each weekday (L-22: "Ana always
    opens Saturday kitchen" was relearned by hand every week; raw_L §3:
    openers and closers per role are shift ownership the manager keeps
    choosing). An opener is the first of the role in that day, a closer the
    last of the role out — the owner's own meaning of a closer (the last of
    their role to leave). Evidence:
      * the restaurant's own scheduling before Cavnar AI drafted it — who
        clocked in first, or out last, for the role that day (shift_facts),
        by hand;
      * each week Cavnar AI drafted, as the manager settled it before it
        went out (learning_weeks: their own changes only) — BY HAND when the
        manager gave them the opening or the close (the draft had somebody
        else), merely kept when the draft already had them; the manager
        handing it to somebody else is a miss by hand.
    An opportunity is a day the person worked that role.

    For a role whose closers the owner chose (the closer flags — the
    draft's closer rule), a closer is learned only AMONG them: which of the
    chosen closers is last out on Fridays (schedule re-audit 10/4/26
    LEARN-10 — "Bo closes Server on Fridays" was learned from who happened
    to leave last before Cavnar AI, beside a rule naming Ana, and the solver
    paid to make Bo last out). A day none of them worked teaches nothing.

    Who really opened or closed a day Cavnar AI drafted (the punches) keeps
    an existing fact alive (`kept_dates`, LEARN-11) without adding evidence
    of its own: those days are already counted, as the manager settled
    them, from the weeks above."""
    hl = half_life(kind)
    ev = {}
    closers = ctx.closers() if kind == "closer" else {}

    def among(group, fam):
        """The part of a role's day a closer is learned from: its chosen
        closers when the owner chose any (None: no chosen closer worked)."""
        chosen = closers.get(fam) if closers else None
        if not chosen:
            return group
        mine = [x for x in group if _nk(x[1]) in chosen]
        return mine or None

    def take(group, d, edge, by_hand, missed=()):
        w = recency_weight(ctx.age(d), hl)
        seen = set()
        for m, name, role, _start, end in sorted(group, key=lambda x: (x[0], x[1])):
            k = _nk(name)
            if k in seen:
                continue
            seen.add(k)
            e = ev.setdefault((ctx.fam(role), _weekday(d), k), dict(_evidence(kind), ends=[]))
            hit = k in edge
            _note(e, d, w, hit, hand=hit and by_hand(k), person=name, role=role,
                  start=m if hit else None, week=_week_of(d))
            if hit and end is not None:
                e["ends"].append(_end_adj(m, end))
            if not hit and k in missed:
                e["miss_dates"].append(d)

    cav = ctx.cavnar_dates()
    pre = [r for r in ctx.punches() if str(r.get("date") or "")[:10] not in cav]
    for (d, fam), g in _by_role_day(ctx, pre).items():
        g = among(g, fam)
        if g:
            take(g, d, _edge_of(g, kind), lambda k: True)
    for rec in ctx.weeks():
        fin, base = _by_role_day(ctx, rec.get("final")), _by_role_day(ctx, rec.get("base"))
        for (d, fam), g in fin.items():
            g = among(g, fam)
            if not g:
                continue
            b = among(base[(d, fam)], fam) if (d, fam) in base else None
            drafted = _edge_of(b, kind) if b else set()
            # By hand only when the manager put the person there — their
            # shift is not the draft's as it stood; one who became the
            # first in (or last out) only because the manager took the
            # drafted one off was chosen by nobody (re-audit 10/4/26, the
            # LEARN lens's open suspicion).
            as_drafted = {(_nk(x[1]), x[0], x[4]) for x in base.get((d, fam)) or []}
            mine = {}
            for x in g:
                mine.setdefault(_nk(x[1]), set()).add((_nk(x[1]), x[0], x[4]))
            take(g, d, _edge_of(g, kind),
                 lambda k, _d=drafted, _m=mine, _a=as_drafted: k not in _d and not (_m.get(k, set()) <= _a),
                 missed=drafted)
    post = [r for r in ctx.punches() if str(r.get("date") or "")[:10] in cav]
    for (d, fam), g in _by_role_day(ctx, post).items():
        g = among(g, fam)
        if not g:
            continue
        for k in _edge_of(g, kind):
            e = ev.get((fam, _weekday(d), k))
            if e is not None:
                e["kept_dates"].append(d)
    out = []
    for (fam, wd, k), e in ev.items():
        if not e["hits"] or not fam or not wd:
            continue
        name = _display(e)
        start = _median(e["starts"])
        end = _median(e.get("ends") or [])
        part = _daypart(_clock(start)) if start is not None else None
        role = _display(e, "roles") or fam
        meal = _MEAL.get(part, "")
        if kind == "opener":
            text = (f"{name} opens {role} on {wd}s — the first {role} in on {e['hits']} of the {e['opps']} {wd}s "
                    f"they worked it" + (f", usually at {_clock(start)}" if start is not None else "") + ".")
        else:
            text = (f"{name} closes {role} on {wd}s — the last {role} out on {e['hits']} of the {e['opps']} {wd}s "
                    f"they worked it" + (f", usually until {_clock(end)}" if end is not None else "") + ".")
        out.append(_memory(ctx, f"{kind}|{fam}|{wd}|{_person_token(ctx, name)}", kind, e, person=name,
                           role=fam, day=wd, daypart=part,
                           value={"start": _clock(start) if start is not None else None,
                                  "end": _clock(end) if end is not None else None, "role": role, "meal": meal},
                           text=text, source="punches_and_edits", origin="manager"))
    return out


def _learn_openers(ctx) -> list:
    return _learn_edges(ctx, "opener")


def _learn_closers(ctx) -> list:
    return _learn_edges(ctx, "closer")


def _learn_sections(ctx) -> list:
    """Each server's usual floor section by weekday and daypart (L-22:
    shift_sections was stored and carried, and read by no learner). Every
    assignment is a person's own hand; one an admin made through view-as or
    support is left out (shift_sections.authority)."""
    import models
    try:
        named = {n.casefold(): n for n in models.foh_sections(ctx.restaurant)}
    except Exception:
        named = {}
    if not named:
        return []
    conn = get_conn(ctx.db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(shift_sections)")}
        auth = "authority" if "authority" in cols else "NULL AS authority"
        rows = conn.execute(f"SELECT date, employee_name, shift_start, section, {auth} FROM shift_sections "
                            "WHERE restaurant_id=? AND date >= ? AND date < ?",
                            (ctx.rid, ctx.since, ctx.today.isoformat())).fetchall()
    finally:
        conn.close()
    hl = half_life("section")
    slots = {}
    for r in rows:
        if (r["authority"] or "") == "admin":
            continue
        sec = named.get(str(r["section"] or "").casefold())
        if not sec:
            continue
        d = str(r["date"])[:10]
        key = (_nk(r["employee_name"]), _weekday(d), _daypart(r["shift_start"]))
        slots.setdefault(key, []).append((d, sec, r["employee_name"]))
    out = []
    for (k, wd, part), entries in slots.items():
        for sec in {s for _d, s, _n in entries}:
            e = _evidence("section")
            for d, s, name in entries:
                _note(e, d, recency_weight(ctx.age(d), hl), s == sec, hand=True, person=name, week=_week_of(d))
            if not e["hits"]:
                continue
            name = _display(e)
            text = (f"{name} usually takes {sec} on {wd} {_MEAL.get(part, part)} — {e['hits']} of {e['opps']} "
                    f"shifts there with a section.")
            out.append(_memory(ctx, f"section|{_person_token(ctx, name)}|{wd}|{part}|{sec.casefold()}", "section", e,
                               person=name, day=wd, daypart=part, value={"section": sec}, text=text,
                               source="shift_sections", origin="manager"))
    return out


def match_punch(row, punches):
    """The punch that is this scheduled row's: the same person's punch that
    day whose clock-in is nearest the row's start, within
    PUNCH_MATCH_MINUTES. None when there is none."""
    s = _minutes(row.get("shift_start"))
    best, gap = None, None
    for p in punches or []:
        ps = _minutes(p.get("shift_start"))
        if ps is None or s is None:
            continue
        g = min(abs(ps - s), 1440 - abs(ps - s))
        if g <= PUNCH_MATCH_MINUTES and (gap is None or g < gap):
            best, gap = p, g
    return best


def minutes_past_end(row, punch):
    """How many minutes past the row's scheduled end its punch clocked out
    (negative: before it), both read across midnight from the row's start —
    None when either end cannot be read."""
    s, e = _minutes(row.get("shift_start")), _minutes(row.get("shift_end"))
    out = _minutes((punch or {}).get("shift_end"))
    if s is None or e is None or out is None:
        return None
    if e <= s:
        e += 1440
    if out < s - 120:
        out += 1440
    return out - e


def _learn_overtime(ctx) -> list:
    """Who keeps running into overtime (L-16: it was forecast over the draft
    and never learned). Per payroll week, from the punches: hours past the
    line (labor.OVERTIME_THRESHOLD_HOURS) are overtime actually worked —
    observed as `ot_actual` — and beside them what the published week had
    them for. A week counts as an opportunity once they were within
    OT_NEAR_HOURS of the line, planned or worked. The memory carries the
    headroom the passes should leave them: the typical overtime, or the
    typical run past their scheduled week, whichever is larger."""
    from labor import OVERTIME_THRESHOLD_HOURS as LINE, _week_key
    actual, names = {}, {}
    for r in ctx.punches():
        try:
            h = float(r.get("actual_hours"))
        except (TypeError, ValueError):
            continue
        k = _nk(r.get("employee"))
        b = _week_key(str(r["date"])[:10], ctx.week_start_day)
        actual[(k, b)] = actual.get((k, b), 0.0) + h
        names[k] = r.get("employee")
    planned = {}
    for _hid, _ws, rows in ctx.published():
        for r in rows:
            k = _nk(r.get("employee"))
            if k in ctx.salaried or not r.get("date"):
                continue
            try:
                h = float(r.get("scheduled_hours") or 0)
            except (TypeError, ValueError):
                h = 0.0
            b = _week_key(str(r["date"])[:10], ctx.week_start_day)
            planned[(k, b)] = planned.get((k, b), 0.0) + h
    hl = half_life("ot_risk")
    ev = {}
    for (k, b), h in sorted(actual.items(), key=lambda kv: kv[0][1]):
        try:
            if date.fromisoformat(b) + timedelta(days=7) > ctx.today:
                continue                     # the payroll week has not ended
        except ValueError:
            continue
        plan = planned.get((k, b))
        over = h - LINE
        if over > OT_TOLERANCE_HOURS:
            observe(ctx.rid, "ot_actual", week_start=b, person=names.get(k), value={
                "hours": round(h, 2), "over": round(over, 2), "planned": round(plan, 2) if plan else None,
                "line": LINE}, origin="system", phase="as_run", authority="system", source="punches",
                db_path=ctx.db, fact_key=f"ot_actual|{k}|{b}")
        if max(h, plan or 0.0) < OT_NEAR_HOURS:
            continue
        e = ev.setdefault(k, dict(_evidence("ot_risk"), over=[], overrun=[]))
        # A week is evidence when they ran into overtime OR ran past what
        # they were scheduled by more than the tolerance — overtime had they
        # been scheduled to the line (schedule re-audit 10/4/26 LEARN-2: once
        # the passes kept them the headroom under the line they stopped
        # crossing it, the hits stopped, the memory fell under the active
        # line and the next draft took the room away again).
        ran_over = plan is not None and plan > 0 and (h - plan) > OT_TOLERANCE_HOURS
        _note(e, b, recency_weight(ctx.age(b), hl), over > OT_TOLERANCE_HOURS or ran_over, person=names.get(k),
              week=b)
        if over > OT_TOLERANCE_HOURS:
            e["over"].append(over)
        if plan:
            e["overrun"].append(h - plan)
    out = []
    for k, e in ev.items():
        if not e["hits"]:
            continue
        name = _display(e)
        over = sum(e["over"]) / len(e["over"]) if e["over"] else 0.0
        runs = [x for x in e["overrun"] if x > 0]
        overrun = sum(runs) / len(e["overrun"]) if e["overrun"] else 0.0
        head = min(OT_HEADROOM_MAX, math.ceil(max(over, overrun) * 2) / 2.0)
        how = (f"about {over:.1f}h past {LINE:g} when they ran over" if over
               else f"about {overrun:.1f}h past what they were scheduled")
        text = (f"{name} has run into overtime, or past their scheduled week, in {e['hits']} of their last "
                f"{e['opps']} payroll weeks near full time — {how}; keep about {head:g}h of room under the "
                f"line for them.")
        out.append(_memory(ctx, f"ot_risk|{_person_token(ctx, name)}", "ot_risk", e, person=name,
                           value={"headroom_hours": head, "over_hours": round(over, 2),
                                  "overrun_hours": round(overrun, 2), "weeks": e["hits"], "of": e["opps"],
                                  "line": LINE},
                           text=text, source="punches", origin="measured", habit=False))
    return out


def _learn_overruns(ctx) -> list:
    """Which closes run past their scheduled end (L-16): each published
    close — the last of its role family out that day — against its punch.
    Ran STAYED_LATE_MINUTES or more past it: stayed late (observed
    `stayed_late`). The memory per role, weekday and daypart carries the pad
    the draft's closes should get: the typical overrun, rounded to
    END_PAD_STEP, at most END_PAD_MAX_MINUTES.

    An overrun is measured against the close as the restaurant set it
    BEFORE any pad: the earlier of the published end and the usual close
    this memory already holds (`ends_at`, the unpadded end) — never against
    an end the memory itself moved (schedule re-audit 10/4/26 LEARN-2: a
    padded close that ran exactly to its padded end read as on time, two
    such weeks dropped the memory under the active line and the next draft
    ended the close early again). `ends_at` is kept from those same
    unpadded ends, so a pad never drifts into the baseline."""
    punches = {}
    for p in ctx.punches():
        if p.get("source") in POS_SOURCES or p.get("shift_end"):
            punches.setdefault((str(p.get("date"))[:10], _nk(p.get("employee"))), []).append(p)
    hl = half_life("end_overrun")
    ev = {}
    today = ctx.today.isoformat()
    unpadded = {}
    for prev in ctx.previous("end_overrun"):
        u = _minutes((prev.get("value") or {}).get("ends_at"))
        if u is not None and prev.get("status") in ("candidate", "active", "rule", "dormant"):
            unpadded[(prev.get("role"), prev.get("day"), prev.get("daypart"))] = u
    for _hid, _ws, rows in ctx.published():
        by = {}
        for r in rows:
            d = str(r.get("date") or "")[:10]
            if len(d) != 10 or d >= today or _nk(r.get("employee")) in ctx.salaried:
                continue
            s, e_ = _minutes(r.get("shift_start")), _minutes(r.get("shift_end"))
            if s is None or e_ is None:
                continue
            by.setdefault((d, ctx.fam(r.get("role"))), []).append((e_ + (1440 if e_ <= s else 0), r))
        for (d, fam), g in by.items():
            if not fam:
                continue
            last = max(x for x, _r in g)
            for end, r in g:
                if end != last:
                    continue
                k = _nk(r.get("employee"))
                p = match_punch(r, punches.get((d, k)))
                over = minutes_past_end(r, p) if p else None
                if over is None:
                    continue
                part = _end_part(r)
                base = last
                u = unpadded.get((fam, _weekday(d), part))
                if u is not None:
                    # The usual close on the same side of midnight as this one.
                    u += 1440 if (u < 12 * 60 and last >= 12 * 60) else 0
                    if u < last:
                        over += last - u
                        base = u
                e = ev.setdefault((fam, _weekday(d), part), dict(_evidence("end_overrun"), over=[], ends=[]))
                e["ends"].append(base)
                hit = over >= STAYED_LATE_MINUTES
                _note(e, d, recency_weight(ctx.age(d), hl), hit, role=(r.get("role") or "").strip(), week=_week_of(d))
                if hit:
                    e["over"].append(over)
                    observe(ctx.rid, "stayed_late", date=d, daypart=part, role=r.get("role"), person=r.get("employee"),
                            value={"minutes": int(over), "scheduled_end": r.get("shift_end"),
                                   "punched_out": p.get("shift_end"), "closing": True},
                            origin="system", phase="as_run", authority="system", source="punches",
                            db_path=ctx.db, fact_key=f"stayed_late|{d}|{k}|{_minutes(r.get('shift_start'))}")
    out = []
    for (fam, wd, part), e in ev.items():
        if not e["hits"]:
            continue
        typical = _median(e["over"]) or 0
        pad = int(min(END_PAD_MAX_MINUTES, max(END_PAD_STEP, round(typical / END_PAD_STEP) * END_PAD_STEP)))
        ends = _median(e["ends"])
        role = _display(e, "roles") or fam
        text = (f"{role} closes on {wd} {_MEAL.get(part, part)} have run about {int(round(typical))} minutes past "
                f"the scheduled end ({e['hits']} of {e['opps']} closes) — end them {pad} minutes later.")
        out.append(_memory(ctx, f"end_overrun|{fam}|{wd}|{part}", "end_overrun", e, role=fam, day=wd,
                           daypart=part, value={"minutes": pad, "typical_over": int(round(typical)),
                                                "closes": e["opps"], "over": e["hits"], "role": role,
                                                "ends_at": _clock(ends) if ends is not None else None,
                                                "padded_end": _clock(ends + pad) if ends is not None else None},
                           text=text, source="punches", origin="measured", habit=False))
    return out


def binom_tail(k, n, p, upper=True) -> float:
    """P(X >= k) (or P(X <= k) with upper False) for X ~ Binomial(n, p)."""
    n, k = int(n), int(k)
    p = min(1.0, max(0.0, float(p)))
    rng = range(k, n + 1) if upper else range(0, k + 1)
    return min(1.0, sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in rng))


def fisher_tail(hits, n, other_hits, other_n, upper=True) -> float:
    """One-sided Fisher exact p-value that `hits` of `n` is higher (upper)
    or lower than `other_hits` of `other_n` beyond chance — both rates
    measured, neither taken as known (the hypergeometric tail)."""
    hits, n, oh, on = int(hits), int(n), int(other_hits), int(other_n)
    total, good = n + on, hits + oh
    if n <= 0 or on <= 0:
        return 1.0
    denom = math.comb(total, n)
    lo, hi = max(0, n - (total - good)), min(n, good)
    rng = range(hits, hi + 1) if upper else range(lo, hits + 1)
    return min(1.0, sum(math.comb(good, i) * math.comb(total - good, n - i) for i in rng) / float(denom))


def significant_teams(stats, base, alpha=PAIR_ALPHA) -> dict:
    """{group: (kind, held_to, p_value)} — the groups whose shared shifts
    ran well (prefer) or badly (avoid) beyond chance (LEARN-7). `stats`:
    {group: {n, hits, other_n, other_hits, apart_n, apart_hits}} for every
    group tested (shared PAIR_MIN_SHARED shifts or more): their shared
    shifts, the read shifts they were NOT all on (other_*), and of those the
    ones some of them worked without the rest (apart_*). `base` (the
    restaurant's share of shifts that ran well) stands in when there are no
    other shifts. Each group is held to the higher (prefer) or lower (avoid)
    of the rate without them together and its own apart rate (PAIR_MIN_APART
    apart shifts at least) — a crew that works most nights is never held to
    a rate it makes itself, and a person who lifts every night they work is
    not a team — must clear it by PAIR_MIN_LIFT, and its one-sided p-value
    must survive Holm's step-down over every group of its size tested
    (pairs are what the passes may enforce; trios only reach the prompt).
    The p-value is Fisher's exact test of the shared shifts against the
    shifts without them together, and against the apart shifts when there
    are enough — the larger of the two (both comparisons must hold); with
    no other shifts at all, the binomial test against `base`. A trio is
    never "avoid" (keeping people apart pairs two). Pure."""
    out = {}
    for size in sorted({len(g) for g in stats}):
        family = {g: s for g, s in stats.items() if len(g) == size}
        m = len(family)
        tested = []
        for g, s in family.items():
            n, hits = int(s.get("n") or 0), int(s.get("hits") or 0)
            if n <= 0:
                continue
            rate = hits / float(n)
            other = (s["other_hits"] / float(s["other_n"])) if int(s.get("other_n") or 0) else base
            apart = (s.get("apart_hits", 0) / float(s["apart_n"])) if int(s.get("apart_n") or 0) >= PAIR_MIN_APART \
                else None
            hi = max(other, apart) if apart is not None else other
            lo = min(other, apart) if apart is not None else other

            def pvalue(upper, s=s, n=n, hits=hits):
                if not int(s.get("other_n") or 0):
                    return binom_tail(hits, n, base, upper=upper)
                pv = fisher_tail(hits, n, s["other_hits"], s["other_n"], upper=upper)
                if int(s.get("apart_n") or 0) >= PAIR_MIN_APART:
                    pv = max(pv, fisher_tail(hits, n, s.get("apart_hits", 0), s["apart_n"], upper=upper))
                return pv
            if rate >= hi + PAIR_MIN_LIFT and rate >= CANDIDATE_MIN_RATE:
                tested.append((pvalue(True), g, "prefer", hi))
            elif size == 2 and rate <= lo - PAIR_MIN_LIFT:
                tested.append((pvalue(False), g, "avoid", lo))
        for i, (pv, g, kind, held_to) in enumerate(sorted(tested, key=lambda t: (t[0], t[1]))):
            if pv > alpha / float(m - i):
                break                                # Holm: the first to fail stops the rest
            out[g] = (kind, held_to, pv)
    return out


def _learn_pairs(ctx) -> list:
    """Teams that do well together (L-21: chemistry needed watched nights,
    read "no issue" as the only success, and nothing applied until the owner
    acted). Every recorded shift (schedule_outcomes, the punches for who
    actually worked it) is read as ran-well or not against the restaurant's
    own record: its sales per labor hour at or above the median for that
    weekday and daypart, its Shift Quality as it ran at or above that
    night's median, no coverage or no-show issue on a night the check was
    watching, no review under GOOD_REVIEW placed on it — every reading
    the shift has, none of the readings it lacks. A pair (or a trio) whose
    shared shifts ran well clearly more often than the restaurant's do —
    PAIR_MIN_SHARED of them at least, PAIR_MIN_LIFT above it — is a
    candidate; active only with confidence. A pair that ran clearly worse
    is a candidate for the owner, never enforced on its own: keeping people
    apart is the owner's call."""
    conn = get_conn(ctx.db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_outcomes)")}
        if "actual_hours" not in cols:
            return []
        outs = conn.execute(
            "SELECT o.history_id, o.date, o.daypart, CASE WHEN o.split_basis='measured' THEN o.sales END AS sales, "
            "o.actual_hours, o.issues, o.review_rating_attributed AS rating, o.reviews_attributed AS reviews "
            "FROM schedule_outcomes o JOIN schedule_history h ON h.id=o.history_id WHERE o.restaurant_id=? "
            "AND o.date >= ? AND h.week_end < ? ORDER BY o.history_id",
            (ctx.rid, ctx.since, ctx.today.isoformat())).fetchall()
        owner_pairs = set()
        for r in conn.execute("SELECT employee_a, employee_b FROM staff_pairs WHERE restaurant_id=?", (ctx.rid,)):
            owner_pairs.add(frozenset((_nk(r["employee_a"]), _nk(r["employee_b"]))))
    except Exception as e:
        log.warning("schedule_memory: outcomes unreadable for %s: %s", ctx.rid, e)
        return []
    finally:
        conn.close()
    slots = {}
    for o in outs:
        slots[(str(o["date"])[:10], o["daypart"])] = o          # the newest publish of the week
    if not slots:
        return []
    import schedule_intel
    dates = sorted(d for d, _p in slots)
    kw = {"db_path": ctx.db} if ctx.db else {}
    watched = schedule_intel.watched_dates(ctx.rid, dates[0], dates[-1], **kw)
    who = {}
    for p in ctx.punches():
        d = str(p.get("date") or "")[:10]
        part = _daypart(p.get("shift_start"))
        if (d, part) in slots:
            who.setdefault((d, part), {})[_nk(p.get("employee"))] = p.get("employee")
    splh = {}
    for (d, part), o in slots.items():
        try:
            if o["sales"] and o["actual_hours"]:
                splh[(d, part)] = float(o["sales"]) / float(o["actual_hours"])
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    medians = {}
    for (d, part), v in splh.items():
        medians.setdefault((_weekday(d), part), []).append(v)
    medians = {k: _median(v) for k, v in medians.items() if len(v) >= 3}

    # The shift's Shift Quality as it ran (schedule_intel.record_as_run_quality),
    # against the same weekday and daypart's median as-run score.
    as_run = {}
    for o in observations(ctx.rid, kinds=("sq_as_run",), since=ctx.since, db_path=ctx.db):
        if o.get("date") and (o.get("value") or {}).get("score") is not None:
            as_run[(str(o["date"])[:10], o.get("daypart"))] = float(o["value"]["score"])
    sq_med = {}
    for (d, part), v in as_run.items():
        sq_med.setdefault((_weekday(d), part), []).append(v)
    sq_med = {k: _median(v) for k, v in sq_med.items() if len(v) >= 3}

    def ran_well(d, part, o):
        readings = []
        med = medians.get((_weekday(d), part))
        if (d, part) in splh and med:
            readings.append(splh[(d, part)] >= med)
        if (d, part) in as_run and sq_med.get((_weekday(d), part)) is not None:
            readings.append(as_run[(d, part)] >= sq_med[(_weekday(d), part)])
        if (o["issues"] or 0) or d in watched:
            readings.append(not (o["issues"] or 0))
        if o["rating"] is not None and (o["reviews"] or 0):
            readings.append(float(o["rating"]) >= GOOD_REVIEW)
        return all(readings) if readings else None

    read = {}
    for (d, part), o in slots.items():
        verdict = ran_well(d, part, o)
        if verdict is not None and who.get((d, part)):
            read[(d, part)] = verdict
    if not read:
        return []
    base = sum(1 for v in read.values() if v) / float(len(read))
    hl = half_life("pair")
    ev, names = {}, {}
    nights_of = {}
    from itertools import combinations
    for (d, part), good in read.items():
        people = who[(d, part)]
        names.update(people)
        keys = sorted(people)
        for k in keys:
            nights_of.setdefault(k, set()).add((d, part))
        w = recency_weight(ctx.age(d), hl)
        groups = list(combinations(keys, 2))
        if len(keys) <= TRIO_MAX_PEOPLE:
            groups += list(combinations(keys, 3))
        for g in groups:
            e = ev.setdefault(g, _evidence("pair"))
            _note(e, d, w, good, week=_week_of(d))
    stats = {}
    for g, e in ev.items():
        if e["opps"] < PAIR_MIN_SHARED:
            continue
        if len(g) == 2 and frozenset(g) in owner_pairs:
            continue                                     # the owner already said: it is their rule
        # The nights some of them worked without the others.
        some = set().union(*(nights_of.get(k, set()) for k in g))
        every = set.intersection(*(nights_of.get(k, set()) for k in g))
        apart = some - every
        other = [v for s, v in read.items() if s not in every]
        stats[g] = {"n": e["opps"], "hits": e["hits"], "apart_n": len(apart),
                    "apart_hits": sum(1 for s in apart if read.get(s)),
                    "other_n": len(other), "other_hits": sum(1 for v in other if v)}
    found = significant_teams(stats, base)
    out = []
    for g, (kind_, held_to, pvalue) in found.items():
        e = ev[g]
        rate = _rate(e)
        if kind_ == "prefer":
            conf_e = e
        else:
            conf_e = dict(e, hits_w=e["opps_w"] - e["hits_w"], hits=e["opps"] - e["hits"])
        shown = [names.get(k) or k.title() for k in g]
        together = ", ".join(shown[:-1]) + " and " + shown[-1]
        if kind_ == "prefer":
            text = (f"{together} on the same shift: {e['hits']} of {e['opps']} shared shifts ran well (sales per "
                    f"labor hour and the Shift Quality as it ran at or above the usual for that night, no coverage "
                    f"issue, no poor review) against {_pct(held_to)} of this restaurant's shifts — a team worth "
                    f"keeping together.")
        else:
            # Worded by what its evidence counts — the shared shifts that did
            # NOT run well — so "6 of 6" beside it reads true.
            text = (f"{together} on the same shift: {e['opps'] - e['hits']} of {e['opps']} shared shifts did not "
                    f"run well, against {_pct(1 - held_to)} of this restaurant's shifts — for the owner to look at, "
                    f"never applied on its own.")
        m = _memory(ctx, f"pair|{kind_}|" + "+".join(_person_token(ctx, names.get(k) or k) for k in g), "pair",
                    conf_e, person=names.get(g[0]) or g[0],
                    value={"with": [names.get(k) or k for k in g[1:]], "kind": kind_, "rate": rate,
                           "baseline": round(base, 3), "held_to": round(held_to, 3), "p_value": round(pvalue, 5),
                           "shared": e["opps"], "ran_well": e["hits"], "size": len(g)},
                    text=text, source="outcomes_and_punches", origin="measured", habit=False,
                    # A trio is told to the model, never enforced (LEARN-7);
                    # keeping two apart is the owner's call alone.
                    may_activate=(kind_ == "prefer" and len(g) == 2), min_rate=0.0)
        m["qualifies_new"] = m["qualifies"] = True
        out.append(m)
    return out


def _learn_redos(ctx) -> list:
    """The owner's no, remembered (raw_L §3, "explicit rejections"): a
    weekday the owner keeps redoing, with the reason they gave — from the
    redo route's observations (redo_days), an admin's redo never counted.
    The opportunity is a week Cavnar AI drafted."""
    rows = observations(ctx.rid, kinds=("redo_days", "draft_discarded"), since=ctx.since, db_path=ctx.db,
                        counted_only=True)
    if not rows:
        return []
    conn = get_conn(ctx.db)
    try:
        drafted = {str(r["week_start"])[:10] for r in conn.execute(
            "SELECT DISTINCT h.week_start FROM schedule_history h WHERE h.restaurant_id=? AND h.week_start >= ? AND "
            "EXISTS (SELECT 1 FROM schedule_versions v WHERE v.history_id=h.id AND v.reason='generated')",
            (ctx.rid, ctx.since)).fetchall() if r["week_start"]}
    finally:
        conn.close()
    hl = half_life("redo_reason")
    by = {}
    for o in rows:
        # A whole draft thrown away is a rejection of the week (day None);
        # a redo of some days, of each weekday it named.
        wd = _weekday(o.get("date")) if o.get("kind") == "redo_days" else "week"
        if not wd:
            continue
        chip = " ".join(str((o.get("value") or {}).get("reason") or "").split())[:40]
        by.setdefault((wd, chip.casefold()), {"chip": chip, "weeks": set()})["weeks"].add(
            str(o.get("week_start") or _week_of(o.get("date")))[:10])
    out = []
    for (wd, ck), v in by.items():
        e = _evidence("redo_reason")
        for wk in sorted(drafted | v["weeks"]):
            _note(e, wk, recency_weight(ctx.age(wk), hl), wk in v["weeks"], hand=True, week=wk)
        reason = f' ("{v["chip"]}")' if v["chip"] else ""
        if wd == "week":
            text = (f"The owner threw away {e['hits']} of {e['opps']} recent drafts whole{reason} — check the week "
                    f"against the requirements and the owner's rules before writing it.")
        else:
            text = (f"The owner redid {wd} in {e['hits']} of {e['opps']} recent drafts{reason} — check {wd} against "
                    f"the requirements and the owner's rules before writing it.")
        out.append(_memory(ctx, f"redo_reason|{wd}|{ck or '-'}", "redo_reason", e, day=None if wd == "week" else wd,
                           value={"reason": v["chip"] or None, "whole_week": wd == "week"}, text=text,
                           source="redo_route", origin="owner"))
    return out


def _learn_mirrors(ctx) -> list:
    """What other learners already decide, held here too so the memory is
    the one place that says what the restaurant has learned (L-29): what
    staff keep dropping and claiming (schedule_intel.behaviour_preferences),
    who misses or is late to shifts (staff_settings.reliability), how each
    weekday's dayparts went (schedule_intel.outcomes_by_daypart — planned
    and worked hours side by side, L-12) and who could hold a station
    (mentoring). Each keeps its own learner's rule and binding: their status
    is that learner's, never re-judged here."""
    import schedule_intel
    import staff_settings
    kw = {"db_path": ctx.db} if ctx.db else {}
    out = []

    def mirror(key, kind, text, value, person=None, role=None, day=None, daypart=None, conf=None, source=""):
        e = _evidence(kind)
        m = _memory(ctx, key, kind, e, person=person, role=role, day=day, daypart=daypart, value=value, text=text,
                    source=source, origin="staff" if kind.startswith("staff_") else "measured", habit=False)
        m["confidence"] = conf
        m["status"], m["retired_reason"] = "active", None
        out.append(m)

    for name, p in (schedule_intel.behaviour_preferences(ctx.rid, **kw) or {}).items():
        for which, kind in (("avoids", "staff_avoid"), ("prefers", "staff_prefer")):
            for slot in p.get(which) or []:
                wd, _sp, part = str(slot).partition(" ")
                part = "night" if part == "night" else "morning"
                verb = "keeps asking to drop" if kind == "staff_avoid" else "keeps picking up"
                mirror(f"{kind}|{_person_token(ctx, name)}|{wd}|{part}", kind,
                       f"{name} {verb} {wd} {_MEAL.get(part, part)} shifts.",
                       {"drops": p.get("drops"), "claims": p.get("claims")}, person=name, day=wd, daypart=part,
                       source="shift_requests")
    try:
        rel = staff_settings.reliability(ctx.rid, today=ctx.today, **kw) or {}
    except Exception as e:
        log.warning("schedule_memory: reliability unreadable for %s: %s", ctx.rid, e)
        rel = {}
    for name, r in rel.items():
        rate = float(r.get("no_show_rate") or 0)
        if rate < 0.2 and not r.get("late_risk"):
            continue
        bits = []
        if rate >= 0.2:
            bits.append(f"missed {r.get('no_shows', 0)} of {r.get('shifts', 0)} watched shifts")
        if r.get("late_risk"):
            bits.append(f"late to {r.get('late', 0)} of {r.get('late_shifts', 0)} clocked shifts")
        mirror(f"reliability|{_person_token(ctx, name)}", "reliability", f"{name} has " + " and ".join(bits) + ".",
               {k: r.get(k) for k in ("no_show_rate", "no_shows", "shifts", "late_rate", "late_risk")}, person=name,
               source="attendance")
    for wd, parts in (schedule_intel.outcomes_by_daypart(ctx.rid, **kw) or {}).items():
        for part, o in parts.items():
            worked = o.get("avg_actual_hours")
            text = (f"{wd} {_MEAL.get(part, part)}: about {o['avg_hours']:g}h scheduled"
                    + (f", {worked:g}h worked" if worked is not None else "")
                    + (f", ${o['splh']:,.0f} of sales per labor hour" if o.get("splh") else "")
                    + f", {o.get('issues_label')}" + (" — a daypart that has gone wrong before" if o.get("troubled") else "")
                    + ".")
            mirror(f"daypart_outcome|{wd}|{part}", "daypart_outcome", text, dict(o), day=wd, daypart=part,
                   source="outcomes")
    try:
        held = schedule_intel.could_hold(schedule_intel.mentoring(ctx.rid, **kw)) or {}
    except Exception as e:
        log.warning("schedule_memory: mentoring unreadable for %s: %s", ctx.rid, e)
        held = {}
    for name, roles in held.items():
        for role in roles:
            mirror(f"could_hold|{_person_token(ctx, name)}|{ctx.fam(role)}", "could_hold",
                   f"{name} could hold {role} — trained beside a closer often enough.", {"role": role},
                   person=name, role=ctx.fam(role), source="mentoring")
    return out


# ── settling and writing ──────────────────────────────────────────────────

def _reversed(m) -> bool:
    """Two hand reversals inside REVERSAL_WINDOW_DAYS since the last hand
    confirmation retire a habit (the standing patterns' rule)."""
    last_hand = max(m.get("hand_dates") or [""])
    misses = sorted(d for d in m.get("miss_dates") or [] if d > last_hand)
    for a, b in zip(misses, misses[1:]):
        try:
            if (date.fromisoformat(b) - date.fromisoformat(a)).days <= REVERSAL_WINDOW_DAYS:
                return True
        except ValueError:
            continue
    return False


def said_by_owner(row) -> bool:
    """Whether a memory's answer (owner_said) is an account holder's — the
    owner's word. A delegate's (a manager's, a member's) is theirs: a hand
    confirmation or a let-go, never the owner's (schedule re-audit 10/4/26
    LEARN-3). A row answered before whose answer it was got recorded reads
    as the owner's: no answer the owner gave is lost."""
    return bool(row) and (row.get("owner_said_authority") or "principal") == "principal"


def _rule_alive(ctx, row) -> bool:
    """Whether the rule a memory was made into (_make_rule) still exists:
    the owner's pair in Team (staff_pairs), the person's standing shift on
    that weekday, the role's end-time rule for that night. A read that fails
    keeps the rule (never dropped for want of a read); a kind with no rule
    of its own here keeps it too (schedule re-audit 10/4/26 LEARN-5)."""
    kind, value = row.get("kind"), _loads(row.get("value_json")) if "value_json" in row else (row.get("value") or {})
    value = value or {}
    try:
        import staff_settings
        kw = {"db_path": ctx.db} if ctx.db else {}
        if kind == "pair":
            sets = ctx._get("pair_sets", lambda: staff_settings.pair_sets(ctx.rid, **kw))
            other = (value.get("with") or [None])[0]
            want = frozenset((" ".join(str(row.get("person") or "").lower().split()),
                              " ".join(str(other or "").lower().split())))
            which = value.get("kind") if value.get("kind") in ("prefer", "avoid") else "prefer"
            return want in {frozenset(" ".join(str(x).lower().split()) for x in p)
                            for p in (sets or {}).get(which) or ()}
        if kind in ("opener", "closer"):
            mine = staff_settings.for_name(ctx.rid, row.get("person"), **kw) or {}
            return any(str((s or {}).get("day") or "") == str(row.get("day") or "")
                       for s in mine.get("standing_shifts") or [])
        if kind == "end_overrun":
            rules = _standing_note_rules(ctx)
            fam = ctx.fam(row.get("role"))
            return any(r.get("kind") == "end" and ctx.fam(r.get("role")) == fam
                       and (not r.get("days") or row.get("day") in r["days"])
                       and (not r.get("dayparts") or row.get("daypart") in r["dayparts"]) for r in rules or [])
    except Exception as e:
        log.warning("schedule_memory: rule of %s unreadable for %s: %s", row.get("memory_key"), ctx.rid, e)
        return True
    return True


def _standing_note_rules(ctx) -> list:
    """The restaurant's note rules in force (schedule_note_rules' rows),
    read so that a failed read raises — note_rules() answers [] on a
    failure, which would read as every rule removed."""
    def read():
        import schedule_note_rules
        conn = get_conn(ctx.db)
        try:
            return [schedule_note_rules._row(r) for r in conn.execute(
                "SELECT * FROM schedule_note_rules WHERE restaurant_id=? AND removed_at IS NULL", (ctx.rid,))]
        finally:
            conn.close()
    return ctx._get("note_rules", read)


def _pattern_rule_alive(ctx, s) -> bool:
    """Whether the rule a standing pattern was made into (schedule_versions.
    make_rule) is still there: a moved_off's day and daypart still not one
    the person can work, a moved_on's daypart still preferred, a
    headcount's role floor and a start or end time still among the note
    rules. The pattern side of LEARN-5. A read that fails keeps the rule."""
    kind, day, part = s.get("kind"), s.get("day"), s.get("daypart")
    try:
        if kind in ("moved_off", "moved_on"):
            import staff_settings
            kw = {"db_path": ctx.db} if ctx.db else {}
            mine = staff_settings.for_name(ctx.rid, s.get("employee"), **kw) or {}
            if kind == "moved_off":
                return (mine.get("daypart_availability") or {}).get(day, "any") not in ("any", part)
            return part in (mine.get("preferred_dayparts") or [])
        if kind in ("headcount_add", "retime_start", "retime_end"):
            want = {"headcount_add": "floor", "retime_start": "start", "retime_end": "end"}[kind]
            fam = ctx.fam(s.get("role"))
            return any(r.get("kind") == want and ctx.fam(r.get("role")) == fam and r.get("scope") == "every"
                       and (not r.get("days") or day in r["days"]) and part in (r.get("dayparts") or [part])
                       for r in _standing_note_rules(ctx))
    except Exception as e:
        log.warning("schedule_memory: rule of pattern %s unreadable for %s: %s", s.get("key"), ctx.rid, e)
        return True
    return True


def _unrule_pattern(ctx, key):
    """The owner removed the rule a standing pattern was made into: the
    standing row is a learned pattern again (its evidence decides), never a
    rule nothing holds (LEARN-5)."""
    conn = get_conn(ctx.db)
    try:
        conn.execute("UPDATE schedule_standing_patterns SET status='active', rule_note=NULL, ruled_by=NULL, "
                     "updated_at=datetime('now') WHERE restaurant_id=? AND pattern_key=? AND status='ruled'",
                     (ctx.rid, key))
        conn.commit()
    finally:
        conn.close()


def _settle(m, prev, ctx) -> tuple:
    """(status, retired_reason) for one learned fact, or (None, None) when
    it is not yet anything (not created). The ladder (module doc).

    A rule holds while the rule it was made into exists; once the owner
    removed it (deleted the pair in Team, the standing shift, the time rule)
    the evidence decides again, as for any learned fact — it read "A rule,
    held by the checks" for good while nothing held it (LEARN-5).

    Only the owner's answer (said_by_owner) holds a fact applied whatever
    its confidence, or retires it as "you let it go"; a delegate's keep is a
    hand confirmation — the fact still has to clear the line — and their
    let-go retires it as theirs (LEARN-3, LEARN-4)."""
    if prev and prev.get("status") == "rule" and m["kind"] not in PATTERN_KINDS:
        if _rule_alive(ctx, prev):
            return "rule", None
        m["_rule_gone"] = True
        prev = dict(prev, status="candidate", owner_said=None, owner_said_at=None, owner_said_authority=None)
    if m.get("status"):
        return m["status"], m.get("retired_reason")
    hand_new = max(m.get("hand_dates") or [""])
    owners = said_by_owner(prev)
    if prev and prev.get("owner_said") == "keep" and prev.get("status") != "retired" and owners:
        # The owner's "keep" is a hand confirmation that holds the fact
        # applied for two half-lives from when they said it — unless the
        # manager has since reversed it twice by hand.
        said = str(prev.get("owner_said_at") or "")[:10]
        if said and ctx.age(said) < 2 * half_life(m["kind"]) and not _reversed(
                dict(m, hand_dates=list(m.get("hand_dates") or []) + [said])):
            return ("dormant", None) if (m.get("person") and ctx.away(m["person"])) else ("active", None)
    if prev and prev.get("owner_said") == "keep" and prev.get("status") != "retired" and not owners:
        # A delegate's keep: their hand confirming it on that day — the
        # decay runs from it, the confidence still has to clear the line.
        said = str(prev.get("owner_said_at") or "")[:10]
        if said and m.get("habit") and said > _renewed(m):
            m["hand_dates"] = sorted(list(m.get("hand_dates") or []) + [said])
            m["confidence"] = round(wilson_lower(m.get("hits_w") or 0, m.get("opps_w") or 0)
                                    * recency_weight(ctx.age(said), half_life(m["kind"])), 3)
    if prev and prev.get("owner_said") == "let_go":
        if not (m["qualifies_new"] and hand_new > str(prev.get("owner_said_at") or "")[:10]):
            return "retired", ("owner" if owners else "manager")
    elif prev and prev.get("status") == "retired":
        newer = (m["hand_dates"] or not m.get("habit")) and (
            (hand_new if m.get("habit") else m.get("last") or "") > str(prev.get("retired_at") or "")[:10])
        if not (m["qualifies_new"] and newer):
            return "retired", prev.get("retired_reason") or "faded"
        prev = None                                       # back on newer evidence: a new candidate
    alive = prev is not None and prev.get("status") in ("candidate", "active", "dormant", "retest")
    if not (m["qualifies"] if alive else m["qualifies_new"]):
        return ("retired", "faded") if alive else (None, None)
    if m.get("habit"):
        if _reversed(m):
            return "retired", "reversed"
        renewed = _renewed(m)
        age = ctx.age(renewed) if renewed else 10 ** 6
        if m["confidence"] < RETIRE_CONFIDENCE and age >= 2 * half_life(m["kind"]):
            return "retired", "decayed"
    if m.get("person") and ctx.away(m["person"]):
        return "dormant", None
    if m["confidence"] >= ACTIVE_CONFIDENCE and m.get("may_activate", True):
        return "active", None
    return "candidate", None


def _enforcement(kind, status) -> str:
    if status == "rule":
        return "hard"
    if status == "active":
        return KIND_ENFORCEMENT.get(kind, "prompt")
    return "prompt"


def _write(ctx, produced, kinds_done) -> dict:
    """Upsert every learned fact, settle its status, and retire what a
    learner that ran tonight no longer finds (a pattern whose standing row
    went, an opener nobody opens as any more). {created, updated, retired,
    by_status}."""
    stats = {"created": 0, "updated": 0, "retired": 0, "by_status": {}}
    ctx.closers()                    # read before the write lock is taken
    conn = get_conn(ctx.db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        have = {r["memory_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM schedule_memory WHERE restaurant_id=?", (ctx.rid,)).fetchall()}
        seen = set()
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for m in produced:
            key = str(m["key"])[:300]
            if key in seen:
                continue
            seen.add(key)
            prev = have.get(key)
            status, reason = _settle(m, prev, ctx)
            if status is None:
                continue
            hand = m.get("last_hand") if "last_hand" in m else (max(m["hand_dates"]) if m.get("hand_dates") else None)
            row = (m["kind"], FACT_CLASS.get(m["kind"], "other"), m.get("person"), m.get("person_id"), m.get("role"),
                   m.get("day"), m.get("daypart"), _dumps(m.get("value") or {}), m.get("text"),
                   float(m.get("opps") or 0), float(m.get("hits") or 0), int(m.get("weeks") or 0),
                   int(m.get("of_weeks") or 0), int(m.get("misses_by_hand") or len(m.get("miss_dates") or [])),
                   hand or None, m.get("first") or None, m.get("last") or None, m.get("confidence"),
                   half_life(m["kind"]), status, _enforcement(m["kind"], status), m.get("source"), m.get("origin"),
                   m.get("authority"), reason)
            if prev is None:
                conn.execute(
                    "INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, person_id, role, "
                    "day, daypart, value_json, text, opportunities, hits, weeks, of_weeks, misses_by_hand, "
                    "last_confirmed_by_hand, first_seen, last_seen, confidence, half_life_days, status, enforcement, "
                    "source, origin, authority_of_evidence, retired_reason, retired_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ctx.rid, key) + row + (now if status == "retired" else None,))
                stats["created"] += 1
            else:
                conn.execute(
                    "UPDATE schedule_memory SET kind=?, fact_class=?, person=?, person_id=COALESCE(?, person_id), "
                    "role=?, day=?, daypart=?, value_json=?, text=?, opportunities=?, hits=?, weeks=?, of_weeks=?, "
                    "misses_by_hand=?, last_confirmed_by_hand=COALESCE(?, last_confirmed_by_hand), "
                    "first_seen=COALESCE(first_seen, ?), last_seen=COALESCE(?, last_seen), confidence=?, "
                    "half_life_days=?, status=?, enforcement=?, source=?, origin=?, authority_of_evidence=?, "
                    "retired_reason=?, retired_at=CASE WHEN ?='retired' THEN COALESCE(retired_at, ?) ELSE NULL END, "
                    "updated_at=datetime('now') WHERE id=?",
                    row + (status, now, prev["id"]))
                stats["updated"] += 1
            if m.get("_rule_gone"):
                # The owner removed the rule: it is no longer theirs to show,
                # nor an answer of theirs to hold (LEARN-5).
                conn.execute("UPDATE schedule_memory SET rule_ref=NULL, owner_said=NULL, owner_said_by=NULL, "
                             "owner_said_at=NULL, owner_said_authority=NULL WHERE restaurant_id=? AND memory_key=?",
                             (ctx.rid, key))
            stats["by_status"][status] = stats["by_status"].get(status, 0) + 1
        for key, prev in have.items():
            if key in seen or prev["kind"] not in kinds_done or prev["status"] == "retired":
                continue
            if prev["status"] == "rule":
                # A rule the learner no longer finds holds while its rule
                # exists; once the owner removed it, it retires (LEARN-5).
                if prev["kind"] in PATTERN_KINDS or _rule_alive(ctx, prev):
                    continue
                conn.execute("UPDATE schedule_memory SET status='retired', enforcement='prompt', "
                             "retired_reason='rule_removed', retired_at=?, rule_ref=NULL, owner_said=NULL, "
                             "owner_said_by=NULL, owner_said_at=NULL, owner_said_authority=NULL, "
                             "updated_at=datetime('now') WHERE id=?", (now, prev["id"]))
                stats["retired"] += 1
                continue
            dismissed = prev["kind"] in PATTERN_KINDS and key.split(":", 1)[-1] in ctx._cache.get(
                "dismissed_patterns", ())
            reason = ("dismissed" if dismissed else
                      "gone" if prev["kind"] in _MIRROR_KINDS or prev["kind"] in PATTERN_KINDS else
                      "not_closer" if _not_a_chosen_closer(prev["kind"], prev.get("role"), prev.get("person"),
                                                           ctx.closers(), ctx.families) else "faded")
            conn.execute("UPDATE schedule_memory SET status='retired', enforcement='prompt', retired_reason=?, "
                         "retired_at=?, updated_at=datetime('now') WHERE id=?", (reason, now, prev["id"]))
            stats["retired"] += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stats


_LEARNERS = (
    ("patterns", PATTERN_KINDS, None),
    ("openers", ("opener",), _learn_openers),
    ("closers", ("closer",), _learn_closers),
    ("sections", ("section",), _learn_sections),
    ("overtime", ("ot_risk",), _learn_overtime),
    ("overruns", ("end_overrun",), _learn_overruns),
    ("pairs", ("pair",), _learn_pairs),
    ("redos", ("redo_reason",), _learn_redos),
    ("mirrors", _MIRROR_KINDS, _learn_mirrors),
)


def consolidate(restaurant_id, db_path=None, today=None, patterns=None, only=None) -> dict:
    """Rebuild one restaurant's memory from its sources (see the module
    doc). Each learner runs on its own: one that fails is captured and its
    kinds are left exactly as they were — never retired for want of a read.
    `patterns` = (patterns, conflicts) from schedule_versions.
    patterns_for_draft when the caller has them; `only` names the learners
    to run (the generation refreshes the patterns alone). A restaurant that
    does not learn for itself (a demo, an admin's exclusion —
    models.learns_for_itself) learns nothing. Returns the run's stats."""
    import models
    stats = {"learners": {}, "failed": []}
    try:
        if not models.learns_for_itself(restaurant_id, db_path=db_path or None):
            return dict(stats, skipped="not_eligible")
    except Exception:
        return dict(stats, skipped="not_eligible")
    ctx = _Ctx(restaurant_id, today=today, db_path=db_path)
    produced, done = [], set()
    for name, kinds, fn in _LEARNERS:
        if only and name not in only:
            continue
        try:
            got = _learn_patterns(ctx, patterns) if name == "patterns" else fn(ctx)
        except Exception as e:
            _capture(e, f"learner {name}", restaurant_id)
            stats["failed"].append(name)
            continue
        produced += got
        done |= set(kinds)
        stats["learners"][name] = len(got)
    stats.update(_write(ctx, produced, done))
    if not only:
        try:
            stats["ratings_waiting"] = len(_note_ratings(ctx))
        except Exception as e:
            _capture(e, "measured ratings", restaurant_id)
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO schedule_memory_state (restaurant_id, consolidated_at, stats_json) VALUES "
                         "(?, datetime('now'), ?) ON CONFLICT(restaurant_id) DO UPDATE SET "
                         "consolidated_at=excluded.consolidated_at, stats_json=excluded.stats_json",
                         (restaurant_id, _dumps(stats)))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _capture(e, "run state", restaurant_id)
    return stats


# ── what binds the week's passes (L-3) ─────────────────────────────────────

def _families(restaurant_id, db_path=None) -> dict:
    """The restaurant's role families map (schedule_rules.role_families)."""
    try:
        import models
        import schedule_rules
        return schedule_rules.role_families(models.get_restaurant(restaurant_id, db_path or models.DB_PATH)) or {}
    except Exception:
        return {}


def _learns(restaurant_id, db_path=None) -> bool:
    try:
        import models
        return bool(models.learns_for_itself(restaurant_id, db_path=db_path or None))
    except Exception:
        return False


def _rows(restaurant_id, statuses, db_path=None, kinds=None) -> list:
    sql = ("SELECT * FROM schedule_memory WHERE restaurant_id=? AND status IN (%s)"
           % ",".join("?" * len(statuses)))
    args = [restaurant_id, *statuses]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args += list(kinds)
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql + " ORDER BY COALESCE(confidence, 0) DESC, id", args).fetchall()]
    finally:
        conn.close()


def _members(r, value) -> list:
    names = [r.get("person")] if r.get("person") else []
    if r.get("kind") == "pair":
        names += [n for n in (value.get("with") or []) if n]
    return names


def _pair_binds(r, value) -> bool:
    """Whether an active pair memory may reach the passes (LEARN-1): a team
    to keep together ("prefer"), or a keep-apart the OWNER kept ("avoid" —
    the learner never activates one, and only an account holder's Keep
    does). A pair with any other polarity, or none, never binds: the passes
    read a pair's polarity from its value, and a missing one must not read
    as "together"."""
    kind = value.get("kind")
    if kind == "prefer":
        return True
    if kind == "avoid":
        return r.get("owner_said") == "keep" and said_by_owner(r) and len(value.get("with") or []) == 1
    return False


def enforced_signals(restaurant_id, week_dates, roster_names=None, db_path=None, closers=None) -> list:
    """The active memories that bind this week's passes: [{kind, key, person,
    day, daypart, role, value, confidence, enforcement, source}], most
    confident first — schedule_engine._learning_signals hands them to the
    scorer, the solver, the optimizer and the trim as signals["learned"]
    (D2 reads them; learned_cost / misses / slot_cost are their meaning in
    one place). Only what is ACTIVE (confidence past ACTIVE_CONFIDENCE, or
    kept by the owner) and soft-enforced through this signal (SIGNAL_KINDS —
    learned headcount is in the requirements table, staff preferences and
    attendance in their own signals: never counted twice); only what is
    about this week's weekdays and people on the roster; never a pattern two
    editors pull opposite ways. A closing overrun gives way to the manager's
    own end time for that role and night (a retime_end memory, active or
    not): their hand over the punches.

    Kinds and their `value` (role is the role FAMILY, day a weekday,
    daypart "morning" | "night"):
      moved_off / moved_on   {slot}: keep the person off / on that slot
      retime_start / _end    {time}: the role's rows there start / end then
      role_change            {role, was_role}: the person works that role there
      leader_swap            {names}: one of them on that role's slot
      opener                 {start, end, role}: the person is the role's
                             first one in that weekday, around `start`
      closer                 {start, end, role}: the person is the last of
                             the role out that weekday, around `end`
      pair                   {with, kind, rate, baseline, shared}: kind
                             "prefer" — the person with `with` on the same
                             shifts; kind "avoid" — the owner kept a learned
                             keep-apart: never two of them on the same shift
                             (LEARN-1). Nothing else is a pair signal.
      ot_risk                {headroom_hours, over_hours, overrun_hours}:
                             keep the person headroom_hours under their line
      end_overrun            {minutes, ends_at, padded_end}: the role's
                             closes there end `minutes` later (pad_overruns)

    A learned closer for a role whose closers the owner chose binds only
    when it is one of them (LEARN-10): `closers` ({family: {name key}},
    chosen_closers_of) when the caller has them, else read here."""
    if not restaurant_id or not _learns(restaurant_id, db_path):
        return []
    rows = _rows(restaurant_id, ("active",), db_path, kinds=SIGNAL_KINDS + ("retime_end",))
    weekdays = {_weekday(d) for d in (week_dates or []) if _weekday(d)}
    roster = {_nk(n) for n in roster_names} if roster_names else None
    families = _families(restaurant_id, db_path)
    retimed_ends = set()
    for r in _rows(restaurant_id, ("active", "candidate"), db_path, kinds=("retime_end",)):
        retimed_ends.add((_fam(r.get("role"), families), r.get("day"), r.get("daypart")))
    if closers is None and any(r["kind"] == "closer" for r in rows):
        closers = chosen_closers_of(restaurant_id, db_path=db_path)
    out = []
    for r in rows:
        if r["enforcement"] not in ("soft", "hard") or r["kind"] not in SIGNAL_KINDS:
            continue
        value = _loads(r.get("value_json")) or {}
        if value.get("conflict"):
            continue
        if r["kind"] == "pair" and not _pair_binds(r, value):
            continue
        if _not_a_chosen_closer(r["kind"], r.get("role"), r.get("person"), closers, families):
            continue
        if r.get("day") and weekdays and r["day"] not in weekdays:
            continue
        if roster is not None and any(_nk(n) not in roster for n in _members(r, value)):
            continue
        if r["kind"] == "end_overrun" and (r.get("role"), r.get("day"), r.get("daypart")) in retimed_ends:
            continue
        # The role as its family ("Server PM" is a server), one vocabulary
        # for every pass; a role change's exact role is in its value.
        role = _fam(r.get("role"), families) if r.get("role") else None
        out.append({"kind": r["kind"], "key": r["memory_key"], "person": r.get("person"), "day": r.get("day"),
                    "daypart": r.get("daypart"), "role": role, "value": value,
                    "confidence": float(r["confidence"]) if r.get("confidence") is not None else None,
                    "enforcement": r["enforcement"], "source": r.get("source")})
    return out


# ── the meaning of a learned signal, for any pass (pure) ──────────────────

def _fam(role, families=None) -> str:
    from shift_quality import role_family
    return role_family(role, families)


def _row_part(r) -> str:
    from shift_quality import present_dayparts
    return (present_dayparts(r) or ["unknown"])[0]


def misses(rows, learned, families=None, line=None, bucket=None) -> list:
    """Which of the `learned` signals (enforced_signals) a week's rows break:
    [{key, kind, text, weight, indexes}] — `weight` is the signal's
    confidence (scaled for overtime by how far past the headroom it goes):
    the soft cost a solver, an optimizer, the trim or the scorer charges.
    Pure; the semantics of every kind live here, once."""
    from labor import OVERTIME_THRESHOLD_HOURS
    line = float(line or OVERTIME_THRESHOLD_HOURS)
    out = []
    by_slot = {}
    for i, r in enumerate(rows or []):
        d = str(r.get("date") or "")[:10]
        by_slot.setdefault((_weekday(d), _row_part(r)), []).append(i)

    def name_of(i):
        return _nk((rows[i] or {}).get("employee"))

    for m in learned or []:
        kind, conf = m.get("kind"), float(m.get("confidence") or 0)
        v = m.get("value") or {}
        day, part, who = m.get("day"), m.get("daypart"), _nk(m.get("person"))
        slot = by_slot.get((day, part), [])
        hit = []
        if kind == "moved_off":
            hit = [i for i in slot if name_of(i) == who]
        elif kind == "moved_on":
            works = any(name_of(i) == who for i in range(len(rows or [])))
            if works and slot and not any(name_of(i) == who for i in slot):
                hit = [-1]
        elif kind in ("retime_start", "retime_end"):
            want = _minutes(v.get("time"))
            col = "shift_start" if kind == "retime_start" else "shift_end"
            hit = [i for i in slot if _fam(rows[i].get("role"), families) == _fam(m.get("role"), families)
                   and want is not None and _minutes(rows[i].get(col)) != want]
        elif kind == "role_change":
            hit = [i for i in slot if name_of(i) == who and _nk(rows[i].get("role")) != _nk(v.get("role"))]
        elif kind == "leader_swap":
            names = {_nk(n) for n in v.get("names") or []}
            mine = [i for i in slot if not m.get("role")
                    or _fam(rows[i].get("role"), families) == _fam(m.get("role"), families)]
            if mine and not any(name_of(i) in names for i in mine):
                hit = [-1]
        elif kind in ("opener", "closer"):
            by_date = {}
            for i, r in enumerate(rows or []):
                if _weekday(r.get("date")) == day and _fam(r.get("role"), families) == _fam(m.get("role"), families):
                    s_, e_ = _minutes(r.get("shift_start")), _minutes(r.get("shift_end"))
                    if s_ is not None:
                        by_date.setdefault(r.get("date"), []).append((s_, r.get("employee"), r.get("role"),
                                                                       r.get("shift_start"), e_, i))
            for _d, g in by_date.items():
                mine = [x for x in g if _nk(x[1]) == who]
                if mine and who not in _edge_of([x[:5] for x in g], kind):
                    hit.append(mine[0][5])
        elif kind == "pair":
            # Its polarity is its meaning (schedule re-audit 10/4/26 LEARN-1:
            # every pair was read as "together", so the owner's Keep on a
            # learned keep-apart charged the week for keeping them apart).
            polarity = v.get("kind")
            if polarity not in ("prefer", "avoid"):
                continue
            group = {who} | {_nk(n) for n in v.get("with") or []}
            dates, idx = {}, {}
            for i, r in enumerate(rows or []):
                if name_of(i) in group:
                    d_, p_ = str(r.get("date"))[:10], _row_part(r)
                    dates.setdefault(d_, {}).setdefault(p_, set()).add(name_of(i))
                    idx.setdefault((d_, p_), []).append(i)
            for _d, parts in dates.items():
                if polarity == "avoid":
                    # Two of them on the same shift (a row's slot: its first
                    # daypart) — one miss, naming the rows on it.
                    clash = [i for p_, ps in parts.items() if len(ps) > 1 for i in idx[(_d, p_)]]
                    if clash:
                        hit.append(-1)
                        hit += clash
                    continue
                there = set().union(*parts.values())
                if len(there) > 1 and any(len(ps & there) < len(there) for ps in parts.values()):
                    hit.append(-1)
        elif kind == "ot_risk":
            head = float(v.get("headroom_hours") or 0)
            weeks = {}
            for i, r in enumerate(rows or []):
                if name_of(i) == who:
                    b = bucket(r.get("date")) if bucket else _week_of(r.get("date"))
                    try:
                        weeks[b] = weeks.get(b, 0.0) + float(r.get("scheduled_hours") or 0)
                    except (TypeError, ValueError):
                        continue
            for b, h in weeks.items():
                if head and h > line - head:
                    out.append({"key": m.get("key"), "kind": kind, "indexes": [],
                                "weight": round(conf * min(1.0, (h - (line - head)) / head), 3),
                                "text": f"{m.get('person')} is drafted {h:g}h — they usually run about {head:g}h over "
                                        f"what they are scheduled, past {line:g}h."})
            continue
        elif kind == "end_overrun":
            pad = int(v.get("minutes") or 0)
            want = _minutes(v.get("padded_end"))
            if pad and want is not None:
                closes = {}
                for i, r in enumerate(rows or []):
                    if _weekday(r.get("date")) == day and _end_part(r) == part \
                            and _fam(r.get("role"), families) == _fam(m.get("role"), families):
                        closes.setdefault(r.get("date"), []).append(i)
                for _d, idx in closes.items():
                    def _end(i):
                        s, e = _minutes(rows[i].get("shift_start")), _minutes(rows[i].get("shift_end"))
                        return None if s is None or e is None else e + (1440 if e <= s else 0)
                    ends = [(_end(i), i) for i in idx if _end(i) is not None]
                    if ends:
                        last = max(e for e, _i in ends)
                        want_adj = want + (1440 if want < 12 * 60 and last >= 1440 else 0)
                        hit += [i for e, i in ends if e == last and e < want_adj]
        if hit:
            out.append({"key": m.get("key"), "kind": kind, "indexes": [i for i in hit if i >= 0],
                        "weight": round(conf * (len(hit) if kind in ("retime_start", "retime_end", "end_overrun")
                                                else 1), 3),
                        "text": _short(m)})
    return out


def learned_cost(rows, learned, families=None, line=None, bucket=None) -> float:
    """The total soft cost the learned signals put on a week (misses' weights)."""
    return round(sum(x["weight"] for x in misses(rows, learned, families, line, bucket)), 3)


def slot_cost(person, date_str, daypart, role, learned, families=None) -> float:
    """What putting `person` on `date_str`'s `daypart` in `role` costs (+)
    or earns (−) against the learned signals that are about one person on
    one slot (moved_off, moved_on, role_change): a solver's per-assignment
    term. Pure."""
    wd, who, cost = _weekday(date_str), _nk(person), 0.0
    for m in learned or []:
        if _nk(m.get("person")) != who or m.get("day") != wd or m.get("daypart") != daypart:
            continue
        conf = float(m.get("confidence") or 0)
        if m.get("kind") == "moved_off":
            cost += conf
        elif m.get("kind") == "moved_on":
            cost -= conf
        elif m.get("kind") == "role_change":
            cost += 0.0 if _nk(role) == _nk((m.get("value") or {}).get("role")) else conf
    return round(cost, 3)


# ── closes that run late, ended when they really end (L-16) ───────────────

def pad_overruns(rows, learned, c=None, editable=None) -> dict:
    """The closes of a role that measurably run past their scheduled end
    (an active end_overrun memory), ended when they really end — at the
    memory's `padded_end` (the usual scheduled close plus the typical
    overrun, rounded to END_PAD_STEP; the close's own end plus `minutes`
    when the memory has no usual close): a close already drafted that late
    is left alone. Only where the person can legally take the longer shift
    (Constraints.can_add, their overtime line included: a pad never creates
    overtime), the row is not pinned and its day is being drafted, and the
    owner set no end time for that role and night (Constraints.role_times —
    their rule stands). A close is the last of its role family out that day.
    Returns {rows, padded:[{index, employee, date, from, to, minutes,
    reason}], left:[{index, employee, date, reason}]} — the reasons go to the
    review, never into a row's note (staff read the notes)."""
    out = [dict(r) for r in rows or []]
    padded, left = [], []
    fams = getattr(c, "role_families", None) if c is not None else None
    role_times = getattr(c, "role_times", None) or {}
    for m in learned or []:
        if m.get("kind") != "end_overrun":
            continue
        v = m.get("value") or {}
        pad = int(v.get("minutes") or 0)
        if pad <= 0:
            continue
        day, part, fam = m.get("day"), m.get("daypart"), m.get("role")
        if any(k[1] == day and k[2] == part and "end" in (t or {}) and _fam(k[0], fams) == fam
               for k, t in role_times.items() if isinstance(k, tuple) and len(k) == 3):
            continue
        by_date = {}
        for i, r in enumerate(out):
            d = str(r.get("date") or "")[:10]
            if _weekday(d) != day or _fam(r.get("role"), fams) != fam:
                continue
            s, e = _minutes(r.get("shift_start")), _minutes(r.get("shift_end"))
            if s is None or e is None:
                continue
            by_date.setdefault(d, []).append((e + (1440 if e <= s else 0), i))
        for d, g in sorted(by_date.items()):
            last = max(e for e, _i in g)
            for end, i in g:
                if end != last:
                    continue
                r = out[i]
                if part and _end_part(r) != part:
                    continue
                if r.get("_pinned"):
                    left.append({"index": i, "employee": r.get("employee"), "date": d, "reason": "a fixed row"})
                    continue
                if editable is not None and d not in editable:
                    continue
                try:
                    hours = float(r.get("scheduled_hours") or 0)
                except (TypeError, ValueError):
                    hours = 0.0
                want = _minutes(v.get("padded_end"))
                if want is not None:
                    # The usual close, read on the same side of midnight as this one.
                    want += 1440 if (want < 12 * 60 and end >= 12 * 60) else 0
                    step = want - end
                else:
                    step = pad
                if step <= 0:
                    continue                       # already drafted until they really finish
                trial = dict(r, shift_end=_clock(end + step, like=r.get("shift_end")),
                             scheduled_hours=f"{hours + step / 60.0:g}")
                if c is not None:
                    ok, why = c.can_add(trial, [x for j, x in enumerate(out) if j != i], overtime=True)
                    if not ok:
                        left.append({"index": i, "employee": r.get("employee"), "date": d, "reason": why})
                        continue
                out[i] = trial
                padded.append({"index": i, "employee": r.get("employee"), "date": d, "from": r.get("shift_end"),
                               "to": trial["shift_end"], "minutes": step,
                               "reason": (f"{v.get('role') or fam} closes on {day}s have run about "
                                          f"{v.get('typical_over') or pad} minutes past the scheduled end "
                                          f"({v.get('over')} of {v.get('closes')} closes)")})
    return {"rows": out, "padded": padded, "left": left}


# ── the learned block of the prompt (L-28) ────────────────────────────────

# The memory's own kinds in the prompt. Learned headcount is IN the
# requirements table (saying it again asked the model for the same extra
# person twice — schedule_versions.prompt_block's rule); staff preferences,
# attendance, outcomes and mentoring have their own blocks, handed in as
# sections.
PROMPT_KINDS = ("moved_off", "moved_on", "retime_start", "retime_end", "role_change", "leader_swap", "opener",
                "closer", "section", "pair", "ot_risk", "end_overrun", "redo_reason")
LEARNED_TITLE = ("WHAT THIS RESTAURANT'S SCHEDULING HAS LEARNED (from the manager's own edits before a week went "
                 "out, what actually happened on the floor and the owner's answers — each with how sure it is. "
                 "A line marked [held] is also held by the checks that run after you, so write the week that way; "
                 "the rest are good defaults, ranked below the hard rules and the shift requirements)")
# Each section's share of the learned budget, relative to the others that
# have something to say on this call (memory_context's rule): the memory
# first, then attendance, then what published weeks did and what staff want.
SECTION_SHARES = {"memory": 4, "reliability": 3, "outcomes": 2, "preferences": 2, "rotation": 2,
                  "splh_objective": 2, "ledger": 1, "could_hold": 1, "splh": 1, "cohort": 1, "starting": 1}
DEFAULT_SECTION_SHARE = 1
SECTION_ORDER = ("memory", "reliability", "outcomes", "preferences", "rotation", "ledger", "could_hold",
                 "splh", "splh_objective", "cohort", "starting")
# What one generation's learned sections may cost together (the engine's
# budget; prompt_lines' own default stays the contract's 1600 for the
# memory alone).
LEARNED_PROMPT_BUDGET_CHARS = 6000
_MORE_NOTE_CHARS = 90


def _short(m) -> str:
    """One short line for a fact the passes also hold (L-28: anything
    enforced in code needs no paragraph)."""
    kind, v = m.get("kind"), m.get("value") or {}
    who, day, meal = m.get("person") or "", m.get("day") or "", _MEAL.get(m.get("daypart"), m.get("daypart") or "")
    role = v.get("role") if kind in ("opener", "closer") else m.get("role")
    if kind == "moved_off":
        return f"{who} off {day} {meal}"
    if kind == "moved_on":
        return f"{who} on {day} {meal}"
    if kind in ("retime_start", "retime_end"):
        return f"{role} {'start' if kind == 'retime_start' else 'end'} at {v.get('time')} on {day} {meal}"
    if kind == "role_change":
        return f"{who} as {v.get('role')} on {day} {meal}"
    if kind == "leader_swap":
        return f"one of {', '.join(v.get('names') or [])} on {day} {meal}" + (f" ({role})" if role else "")
    if kind == "opener":
        return f"{who} opens {role} on {day}s" + (f", from about {v.get('start')}" if v.get("start") else "")
    if kind == "closer":
        return f"{who} closes {v.get('role') or role} on {day}s" + (
            f", until about {v.get('end')}" if v.get("end") else "")
    if kind == "pair" and v.get("kind") == "avoid":
        # The owner's own keep-apart: never named where it is read (LEARN-6).
        return "two people the owner keeps apart, on the same shift"
    if kind == "pair":
        return f"{who} with {' and '.join(v.get('with') or [])} on the same shifts"
    if kind == "ot_risk":
        return f"{who}: keep about {v.get('headroom_hours')}h of room under the overtime line"
    if kind == "end_overrun":
        return f"{v.get('role') or role} closes on {day} {meal} end {v.get('minutes')} minutes later"
    return str(m.get("text") or "")


def _memory_units(restaurant_id, week_dates, roster_names, db_path, closers=None) -> list:
    """[(text, rank)] — the memory's lines for this week: candidates in
    full, with their evidence; an active fact held in code as one short
    [held] line; only what is about this week's weekdays and people still on
    the roster.

    Never a keep-apart (schedule re-audit 10/4/26 LEARN-1, LEARN-6): the
    draft is shared with every login that opens the schedule, and a learned
    keep-apart is the owner's alone — a candidate is for the owner to look
    at, and one the owner kept is held by the passes, counted and never
    named, as an owner-only pairing is (the [held] line said "Ana with Bo on
    the same shifts", the opposite of what the owner kept). Never a learned
    closer who is not one of the closers the owner chose (LEARN-10)."""
    if not _learns(restaurant_id, db_path):
        return []
    weekdays = {_weekday(d) for d in (week_dates or []) if _weekday(d)}
    roster = {_nk(n) for n in roster_names} if roster_names else None
    rows = _rows(restaurant_id, ("candidate", "active"), db_path, kinds=PROMPT_KINDS)
    if closers is None and any(r["kind"] == "closer" for r in rows):
        closers = chosen_closers_of(restaurant_id, db_path=db_path)
    families = _families(restaurant_id, db_path) if closers else None
    units = []
    for r in rows:
        value = _loads(r.get("value_json")) or {}
        if value.get("conflict"):
            continue
        if r["kind"] == "pair" and value.get("kind") != "prefer":
            continue
        if _not_a_chosen_closer(r["kind"], r.get("role"), r.get("person"), closers, families):
            continue
        if r.get("day") and weekdays and r["day"] not in weekdays:
            continue
        if roster is not None and any(_nk(n) not in roster for n in _members(r, value)):
            continue
        conf = float(r["confidence"]) if r.get("confidence") is not None else 0.0
        sure = f"{_pct(conf)} sure"
        held = r["status"] == "active" and r["enforcement"] in ("soft", "hard") and r["kind"] in SIGNAL_KINDS
        m = dict(r, value=value)
        if held:
            text = f"[held] {_short(m)} ({sure})."
        else:
            evidence = (f"{int(r.get('hits') or 0)} of {int(r.get('opportunities') or 0)}"
                        if r.get("opportunities") else "")
            body = str(r.get("text") or _short(m)).strip().rstrip(".")
            text = f"{body} ({sure}" + (f"; {evidence}" if evidence and evidence not in body else "") + ")."
        units.append((text, 2.0 * conf + (1.0 if held else 0.0)))
    return units


def _block_paragraphs(text) -> list:
    """[(heading, [unit lines])] of one prompt block: paragraphs split on a
    blank line; a unit is an indented line ("  …") with any deeper-indented
    lines under it; a paragraph with no indented line is one unit with no
    heading."""
    out = []
    for para in re.split(r"\n[ \t]*\n", str(text or "").strip("\n")):
        if not para.strip():
            continue
        head, units = [], []
        for ln in para.split("\n"):
            if ln.startswith("  ") and (head or units):
                if units and ln.startswith("    "):
                    units[-1] += "\n" + ln
                else:
                    units.append(ln)
            elif not units:
                head.append(ln)
            else:
                units[-1] += "\n" + ln
        if not units:
            out.append(("", ["\n".join(head)]))
        else:
            out.append(("\n".join(head), units))
    return out


_DAY_WORDS = {d: (d.lower(), d[:3].lower() + " ") for d in WEEKDAYS}


def _unit_rank(text, names, weekdays) -> float:
    """Relevance of one learned line to this week: people on the roster it
    names count up, a weekday of this week counts up and a weekday the week
    does not have counts down (a slice drafts some days only)."""
    low = " " + str(text or "").lower() + " "
    score = 0.0
    hits = 0
    for n in names:
        if n and re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", low):
            hits += 1
    score += min(2, hits)
    said = {d for d, (full, abbr) in _DAY_WORDS.items() if full in low or (" " + abbr) in low}
    if said and weekdays and len(weekdays) < 7:
        score += 1.0 if said & weekdays else -1.5
    return score


def prompt_lines(restaurant_id, week_dates, roster_names=None, budget_chars=1600, db_path=None,
                 sections=None, closers=None) -> str:
    """ONE learned block for the schedule prompt, inside `budget_chars`
    (schedule audit 10/3/26 L-28: about fifteen learned blocks were
    concatenated with no budget and no ranking, the standing patterns with
    no cap, so the prompt bloated and diluted as history grew).

    The memory's own section (_memory_units: candidates in full with their
    evidence and how sure; a fact the passes hold as one short [held] line)
    and each block the caller hands in as `sections` — [(name, text)], the
    generation's other learned blocks (attendance, what published weeks did,
    what staff want, the rotation, sales per labor hour, who could hold a
    station, the peer ratio, a borrowed start) in the caller's own words —
    share the budget (SECTION_SHARES, the shares of the sections that have
    something to say); within each, lines are ranked by relevance to this
    week (people on the roster, this week's weekdays) and the memory's by
    confidence, and the least relevant go first. A section that had to drop
    lines says how many. What is kept reads in its original order. "" when
    nothing has anything to say."""
    weekdays = {_weekday(d) for d in (week_dates or []) if _weekday(d)}
    names = sorted({_nk(n) for n in (roster_names or []) if _nk(n)}, key=len, reverse=True)
    parts = []
    try:
        mem = _memory_units(restaurant_id, week_dates, roster_names, db_path, closers=closers)
    except Exception as e:
        _capture(e, "prompt memory", restaurant_id)
        mem = []
    if mem:
        parts.append(("memory", LEARNED_TITLE + ":", ["  - " + t for t, _r in mem], [r for _t, r in mem]))
    for name, text in sections or []:
        for heading, units in _block_paragraphs(text):
            parts.append((str(name or "other"), heading, units, [_unit_rank(u, names, weekdays) for u in units]))
    if not parts:
        return ""
    order = {n: i for i, n in enumerate(SECTION_ORDER)}
    parts.sort(key=lambda p: order.get(p[0], len(order)))
    # Room is kept for each section's "(+N more … not shown)" line, so the
    # block stays inside its budget whatever it had to cut.
    budget = max(0, int(budget_chars or 0) - _MORE_NOTE_CHARS * len(parts))
    shares = [SECTION_SHARES.get(p[0], DEFAULT_SECTION_SHARE) for p in parts]
    total = float(sum(shares)) or 1.0
    taken = [[] for _p in parts]
    spent = [0] * len(parts)

    def cost(i, j):
        c = len(parts[i][2][j]) + 1
        if not taken[i]:
            c += len(parts[i][1]) + 2 if parts[i][1] else 2
        return c

    def fill(i, allowance):
        ranked = sorted(range(len(parts[i][2])), key=lambda j: (-parts[i][3][j], j))
        used = 0
        for j in ranked:
            if j in taken[i]:
                continue
            c = cost(i, j)
            if used + c > allowance:
                continue
            taken[i].append(j)
            used += c
        spent[i] += used
        return used

    left = budget
    for i in range(len(parts)):
        left -= fill(i, int(budget * shares[i] / total))
    for i in range(len(parts)):
        if left <= 0:
            break
        left -= fill(i, left)
    out = []
    for i, (name, heading, units, _ranks) in enumerate(parts):
        if not taken[i]:
            continue
        kept = [units[j] for j in sorted(taken[i])]
        dropped = len(units) - len(kept)
        text = (heading + "\n" if heading else "") + "\n".join(kept)
        if dropped:
            text += f"\n  (+{dropped} more learned line{'s' if dropped != 1 else ''} here not shown — the least " \
                    f"relevant to this week)"
        out.append(text)
    if not out:
        return ""
    return "\n\n" + "\n\n".join(out)


# ── the owner's view and say ──────────────────────────────────────────────

CLASS_LABELS = {"habit": "The manager's habits", "ownership": "Who opens, and sections",
                "team": "Teams", "overtime": "Overtime and late closes", "rejection": "Days the owner redid",
                "staff": "What staff keep dropping and picking up", "attendance": "Attendance",
                "outcome": "How each daypart went", "mentoring": "Who could hold a station"}
STATUS_LABELS = {"candidate": "Learning", "active": "Applied", "rule": "A rule", "retest": "Being re-tested",
                 "retired": "Retired", "dormant": "Asleep"}
RETIRED_WORDS = {"reversed": "reversed by hand", "decayed": "faded unconfirmed", "faded": "no longer seen",
                 "gone": "no longer seen", "owner": "you let it go", "dismissed": "dismissed",
                 "retest": "not put back when tested", "manager": "a manager let it go",
                 "rule_removed": "its rule was removed", "not_closer": "not one of the closers you chose"}
# What "make it a rule" becomes for each kind; the rest are refused in words.
_RULE_NOTES = {
    "ot_risk": "Overtime risk can't be a rule — set their hours limit in Team to cap their week.",
    "section": "Sections are set per shift in the Studio; a usual section stays a learned habit.",
    "redo_reason": "A redo reason is a reminder for the draft, not a rule.",
    "pair_avoid_trio": "A rule pairs two people.",
}


def owner_only(r, value=None) -> bool:
    """A memory only the account holders may read (schedule re-audit
    10/4/26 LEARN-6): a learned keep-apart — that two people's shared
    shifts ran worse is a judgement about them, the owner's call alone
    (memory_lines' audience rule, held on every surface)."""
    value = value if value is not None else (_loads(r.get("value_json")) or {})
    return r.get("kind") == "pair" and value.get("kind") == "avoid"


def _answer_words(r, principal=True) -> str:
    """Who answered a memory, in the viewer's words, or None."""
    said = r.get("owner_said")
    if said not in ("keep", "let_go") or r.get("status") == "rule":
        return None
    who = ("you" if principal else "the owner") if said_by_owner(r) else "a manager"
    return ("Kept by " if said == "keep" else "Let go by ") + who


def memory_view(restaurant_id, db_path=None, include_retired=True, principal=True) -> dict:
    """The memory for the owner's screen: {items, counts, consolidated_at
    (M/D/YY), classes, ladder}. Each item says what it is (class, kind,
    text), how sure (confidence 0-1 and `confidence_pct` — a computed %, "—"
    below CANDIDATE_MIN_HITS opportunities), on what (opportunities, hits,
    misses by hand), when it was last confirmed by hand and last seen (M/D/YY),
    its status and what holds it (held_in_code, bound_by), who answered it
    (`answered`: "Kept by you", "Let go by a manager"…) and what the viewer
    may do (can_keep — `keep_label` names it: "Keep them apart" for a
    keep-apart —, can_let_go, can_be_rule).

    `principal` False (a manager's or a member's login): the owner-only
    facts are left out (owner_only, LEARN-6), and nothing the owner already
    answered can be answered the other way (LEARN-3)."""
    from time_utils import mdy
    statuses = STATUSES if include_retired else ("candidate", "active", "rule", "retest", "dormant")
    rows = _rows(restaurant_id, statuses, db_path)
    conn = get_conn(db_path)
    try:
        st = conn.execute("SELECT consolidated_at FROM schedule_memory_state WHERE restaurant_id=?",
                          (restaurant_id,)).fetchone()
    finally:
        conn.close()
    items, counts = [], {}
    for r in rows:
        value = _loads(r.get("value_json")) or {}
        if not principal and owner_only(r, value):
            continue
        opps = int(r.get("opportunities") or 0)
        conf = r.get("confidence")
        held = r["status"] == "active" and r["kind"] in SIGNAL_KINDS
        mirror = r["kind"] in _MIRROR_KINDS
        live = not mirror and r["status"] in ("candidate", "active", "retest", "dormant")
        owners = r.get("owner_said") and said_by_owner(r)
        items.append({
            "key": r["memory_key"], "kind": r["kind"], "fact_class": r["fact_class"],
            "class_label": CLASS_LABELS.get(r["fact_class"], r["fact_class"]),
            "person": r.get("person"), "role": r.get("role"), "day": r.get("day"), "daypart": r.get("daypart"),
            "text": r.get("text"), "value": value, "status": r["status"],
            "status_label": STATUS_LABELS.get(r["status"], r["status"]), "enforcement": r["enforcement"],
            "held_in_code": held or r["status"] == "rule",
            "bound_by": BOUND_ELSEWHERE.get(r["kind"]) if r["status"] == "active" else None,
            "confidence": round(float(conf), 3) if conf is not None else None,
            "confidence_pct": _pct(conf) if (conf is not None and (opps >= CANDIDATE_MIN_HITS or mirror)) else "—",
            "opportunities": opps, "hits": int(r.get("hits") or 0), "misses_by_hand": int(r.get("misses_by_hand") or 0),
            "last_confirmed_by_hand": mdy(r["last_confirmed_by_hand"]) if r.get("last_confirmed_by_hand") else None,
            "first_seen": mdy(r["first_seen"]) if r.get("first_seen") else None,
            "last_seen": mdy(r["last_seen"]) if r.get("last_seen") else None,
            "source": r.get("source"), "origin": r.get("origin"),
            "retired_reason": r.get("retired_reason"),
            "retired_words": RETIRED_WORDS.get(r.get("retired_reason")) if r["status"] == "retired" else None,
            "owner_said": r.get("owner_said"), "rule": r.get("rule_ref"),
            "answered": _answer_words(r, principal),
            "keep_label": "Keep them apart" if owner_only(r, value) else "Keep",
            "can_keep": live and (principal or not (owners and r.get("owner_said") == "let_go")),
            "can_let_go": live and (principal or not (owners and r.get("owner_said") == "keep")),
            "can_be_rule": (not mirror and r["status"] in ("candidate", "active", "retest")
                            and _rule_refusal(r, value) is None)})
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"items": items, "counts": counts,
            "consolidated_at": mdy(st["consolidated_at"]) if st and st["consolidated_at"] else None,
            "classes": CLASS_LABELS,
            "ladder": {"candidate_min_weeks": CANDIDATE_MIN_HITS, "candidate_min_rate": CANDIDATE_MIN_RATE,
                       "active_confidence": ACTIVE_CONFIDENCE, "retire_confidence": RETIRE_CONFIDENCE}}


def _rule_refusal(r, value):
    """None when the fact can become a rule, else the owner's words why not."""
    kind = r["kind"]
    if kind in PATTERN_KINDS:
        import schedule_versions as sv
        return None if kind in sv.RULE_KINDS else (
            "A cut can't be a rule: Cavnar AI holds staffing minimums, not maximums." if kind == "headcount_cut"
            else "Only a pattern about one person, a headcount the manager keeps adding, or a start or end time "
                 "can become a rule.")
    if kind == "pair":
        if value.get("size", 2) != 2:
            return _RULE_NOTES["pair_avoid_trio"]
        return None
    if kind in ("opener", "closer"):
        return None if value.get("start") and value.get("end") else "There is no usual shift to hold yet."
    if kind == "end_overrun":
        return None if value.get("padded_end") and value.get("role") else "There is no usual close time to hold yet."
    return _RULE_NOTES.get(kind, "This one follows its own record and can't be a rule.")


def owner_answer(restaurant_id, key, action, user=None, db_path=None) -> dict:
    """The owner's say over one memory (raw_L §3 B: "rule: owner confirms"):
      keep    the owner's (an account holder's): the fact is applied
              (active) for two half-lives whatever its confidence (_settle;
              a standing pattern through schedule_versions.confirm_standing,
              owner_kept_at — LEARN-4), unless the manager reverses it
              twice by hand; a keep-apart the owner keeps holds the two
              APART in every pass (LEARN-1). A delegate's (a manager's, a
              member's): their hand confirmation, recorded as theirs — the
              fact still has to clear the line.
      let_go  retired as the answerer's ("you let it go" for the owner, "a
              manager let it go" for a delegate); only newer hand evidence
              brings it back (a pattern: its dismissal or its standing row
              let go)
      rule    made something code enforces: a person pattern, a headcount or
              a start/end through schedule_versions.make_rule (L-33); a
              pair → the owner's pair (staff_pairs); an opener → their
              standing shift; a closing overrun → the role's end-time rule
              (schedule_note_rules.add_time_rule)
    Whose word it is (schedule re-audit 10/4/26 LEARN-3): a delegate never
    undoes the owner's answer (their Let go on what the owner kept, their
    Keep on what the owner let go — refused in words), never answers an
    owner-only fact (a keep-apart), and never makes a rule beyond a person's
    own slot pattern; the answer is stored with its authority
    (owner_said_authority). Through view-as nothing changes — it is not the
    restaurant's word (L-8, L-10). A fact that is a rule is changed where
    the rule lives. Returns {ok, key, action, rule, message}; raises
    ValueError with the owner's words."""
    import schedule_versions as sv
    if action not in ("keep", "let_go", "rule"):
        raise ValueError("Answer keep, let go or make it a rule.")
    auth = sv.authority_of(user)
    if auth == "admin":
        raise ValueError("An answer through view-as doesn't change what the draft keeps — the owner answers this one.")
    owner = auth == "principal"
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM schedule_memory WHERE restaurant_id=? AND memory_key=?",
                           (restaurant_id, str(key)[:300])).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError("That isn't something the schedule has learned.")
    r, value = dict(row), _loads(row["value_json"]) or {}
    if r["kind"] in _MIRROR_KINDS:
        raise ValueError("This one follows its own record — it changes as that record does.")
    if r["status"] == "rule" and action != "rule":
        raise ValueError("This one is a rule now — change or remove it where the rule is kept.")
    if owner_only(r, value) and not owner:
        raise ValueError("Only the owner answers this one.")
    if action == "rule" and not owner and r["kind"] not in ("moved_off", "moved_on"):
        raise ValueError("Only the account owner can turn something learned into a rule the draft must keep.")
    if not owner and r.get("owner_said") in ("keep", "let_go") and said_by_owner(r) and r["status"] != "rule":
        if action == "let_go" and r["owner_said"] == "keep":
            raise ValueError("The owner kept this one — only the owner can let it go.")
        if action == "keep" and r["owner_said"] == "let_go":
            raise ValueError("The owner let this one go — only the owner can bring it back.")
        if action == r["owner_said"]:
            return {"ok": True, "key": key, "action": action, "rule": None, "unchanged": True,
                    "message": "The owner already " + ("kept" if action == "keep" else "let go of") + " this one."}
    who = (user or {}).get("username") or (user or {}).get("email") or ("owner" if owner else "manager")
    said_auth = "principal" if owner else "delegate"
    kw = {"db_path": db_path} if db_path else {}
    rule_ref = None
    if r["kind"] in PATTERN_KINDS:
        pkey = r["memory_key"].split(":", 1)[1]
        if action == "rule":
            out = sv.make_rule(restaurant_id, pkey, user=user, **kw)
            rule_ref = str(out.get("rule") or out.get("note") or "rule")[:200]
        elif action == "keep":
            try:
                sv.confirm_standing(restaurant_id, pkey, user=user, keep=True, **kw)
            except sv.OwnerAnswered as e:
                raise ValueError(str(e))
            except ValueError:
                p = {"kind": r["kind"], "employee": r.get("person"), "role": r.get("role"), "day": r.get("day"),
                     "daypart": r.get("daypart"), "time": value.get("time"), "text": r.get("text"),
                     "was_role": value.get("was_role"), "delta": value.get("delta"), "names": value.get("names")}
                sv.owner_said_pattern(restaurant_id, p, auth, who=who, **kw)
        else:
            try:
                sv.confirm_standing(restaurant_id, pkey, user=user, keep=False, **kw)
            except sv.OwnerAnswered as e:
                raise ValueError(str(e))
            except ValueError:
                import schedule_intel
                schedule_intel.dismiss_pattern(restaurant_id, pkey, actor=who, authority=auth, **kw)
        if action != "rule":
            _record_answer(restaurant_id, r["id"], action, who, said_auth, db_path)
        consolidate(restaurant_id, db_path=db_path, only=("patterns",))
        return {"ok": True, "key": key, "action": action, "rule": rule_ref,
                "message": _answer_message(action, owner, r, value)}
    if action == "rule":
        why = _rule_refusal(r, value)
        if why:
            raise ValueError(why)
        rule_ref = _make_rule(restaurant_id, r, value, user, db_path)
    if action == "keep" and not owner:
        # A delegate's keep: their hand confirmation, recorded as theirs;
        # the status stays what the evidence makes it (_settle) — one
        # retired by anybody but the owner goes back on the ladder.
        status = "candidate" if r["status"] == "retired" else r["status"]
    else:
        status = {"keep": "active", "let_go": "retired", "rule": "rule"}[action]
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE schedule_memory SET status=?, enforcement=?, owner_said=?, owner_said_by=?, "
                     "owner_said_authority=?, owner_said_at=datetime('now'), "
                     "last_confirmed_by_hand=CASE WHEN ?='keep' THEN date('now') ELSE last_confirmed_by_hand END, "
                     "retired_reason=CASE WHEN ?='let_go' THEN ? ELSE NULL END, "
                     "retired_at=CASE WHEN ?='let_go' THEN datetime('now') ELSE NULL END, "
                     "rule_ref=COALESCE(?, rule_ref), updated_at=datetime('now') WHERE id=?",
                     (status, _enforcement(r["kind"], status), action if action != "rule" else "keep", who, said_auth,
                      action, action, "owner" if owner else "manager", action, rule_ref, r["id"]))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "key": key, "action": action, "rule": rule_ref,
            "message": _answer_message(action, owner, r, value)}


def _record_answer(restaurant_id, memory_id, action, who, authority, db_path=None):
    """A pattern memory's answer, with whose it is (the standing row holds
    its effect; the memory row says who answered, for the screen and for
    the next answer's check)."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE schedule_memory SET owner_said=?, owner_said_by=?, owner_said_authority=?, "
                     "owner_said_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
                     (action, who, authority, memory_id))
        conn.commit()
    finally:
        conn.close()


def _answer_message(action, owner, r, value) -> str:
    """What the screen says after an answer, in the answerer's words."""
    if action == "rule":
        return "Now a rule the draft keeps."
    if action == "let_go":
        return ("Let go — only newer edits of yours bring it back." if owner
                else "Let go — the owner can still keep it.")
    if not owner:
        return "Noted as your confirmation — it's applied once Cavnar AI is sure enough, or when the owner keeps it."
    if owner_only(r, value):
        return "Kept — every draft keeps them on different shifts."
    return "Kept — the draft applies it."


def _make_rule(restaurant_id, r, value, user, db_path) -> str:
    """The owner's rule from a learned fact; returns its words."""
    kw = {"db_path": db_path} if db_path else {}
    who = (user or {}).get("username") or (user or {}).get("email") or "owner"
    if r["kind"] == "pair":
        import staff_settings
        kind = value.get("kind") if value.get("kind") in ("prefer", "avoid") else "prefer"
        other = (value.get("with") or [None])[0]
        staff_settings.set_pair(restaurant_id, r.get("person"), other, kind,
                                note="learned from how their shared shifts went", created_by=who, **kw)
        return f"{r.get('person')} and {other}: " + ("work well together" if kind == "prefer" else "keep apart")
    if r["kind"] in ("opener", "closer"):
        import staff_settings
        name = r.get("person")
        mine = staff_settings.for_name(restaurant_id, name, **kw) or {}
        standing = list(mine.get("standing_shifts") or [])
        standing.append({"day": r.get("day"), "start": value.get("start"), "end": value.get("end"),
                         "role": value.get("role")})
        staff_settings.upsert(restaurant_id, name, standing_shifts=standing, updated_by=who, **kw)
        return f"{name} works {r.get('day')}s {value.get('start')}–{value.get('end')} ({value.get('role')})"
    if r["kind"] == "end_overrun":
        import schedule_note_rules
        schedule_note_rules.add_time_rule(restaurant_id, value.get("role"), "end", value.get("padded_end"),
                                          r.get("daypart"), days=[r.get("day")],
                                          source_text="learned from the punches: closes ran past the scheduled end",
                                          user=user, **kw)
        return f"{value.get('role')} on {r.get('day')} {_MEAL.get(r.get('daypart'), '')} ends at {value.get('padded_end')}"
    raise ValueError(_rule_refusal(r, value) or "This one can't be a rule.")


# ── measured server performance, offered as a rating (L-23, D-27) ─────────
#
# The scheduler's strength came only from manual Operational Scores, and EJ's
# had none counted, while the POS already records every ticket each server
# rang (service_performance). The measure here is what a server sells per
# guest against what the house sells per guest at the SAME mealtime — so a
# lunch server is never compared with a dinner one — over the tickets they
# rang that carry a guest count. Past a sample floor it becomes a suggested
# 1-5 rating. It is personnel data: the account holder's alone (the routes
# check is_principal), never in any prompt or any team-visible memory; the
# owner confirms each one (or sets another score), and only then is it a
# rating the scorer reads — an ordinary owner's rating, with its author.

SUGGEST_DAYS = 56
SUGGEST_MIN_TICKETS = 20          # service_performance.MIN_TICKETS: a slow Tuesday is not a trend
# Sales per guest against the house at the same mealtime → a suggested score:
# 20% or more above the house a 5, 7% above a 4, within 7% a 3, up to 20%
# below a 2, further below a 1.
SUGGEST_BANDS = ((1.20, 5), (1.07, 4), (0.93, 3), (0.80, 2))


def _band(ratio) -> int:
    for floor, score in SUGGEST_BANDS:
        if ratio >= floor:
            return score
    return 1


def suggested_ratings(restaurant_id, db_path=None, today=None, days=SUGGEST_DAYS) -> dict:
    """{available, window: [iso, iso], servers: [{name, tickets, covers,
    sales_per_cover, house_sales_per_cover, ratio, suggested, current,
    differs, reason}], min_tickets, note} — one row per person on the roster
    who rang at least SUGGEST_MIN_TICKETS tickets with a guest count in the
    window. `current` is their counted rating (None: not rated)."""
    import service_performance as sp
    import models
    today = _today(restaurant_id, today)
    start, end = sp._window(days, today)
    try:
        tickets, _comps, _archived = sp._tickets(restaurant_id, start, end, db_path or models.DB_PATH)
    except Exception as e:
        log.warning("schedule_memory: tickets unreadable for %s: %s", restaurant_id, e)
        tickets = []
    rows = [t for t in tickets if t.get("server_name") and (t.get("guest_count") or 0) > 0
            and (t.get("net_sales") or 0) > 0]
    if not rows:
        return {"available": False, "window": [start.isoformat(), end.isoformat()], "servers": [],
                "min_tickets": SUGGEST_MIN_TICKETS,
                "reason": "No tickets with a guest count and a server are archived for these days yet."}
    house = {}
    for t in rows:
        h = house.setdefault(t.get("mealtime") or "", [0.0, 0])
        h[0] += float(t["net_sales"])
        h[1] += int(t["guest_count"])
    house_spc = {k: v[0] / v[1] for k, v in house.items() if v[1]}
    import staff_settings
    kw = {"db_path": db_path} if db_path else {}
    roster = {_nk(p["name"]): p["name"] for p in staff_settings.roster(restaurant_id, **kw)}
    try:
        import people
        canon = people.canonical_names(restaurant_id, sorted({t["server_name"] for t in rows}), db_path=db_path)
    except Exception:
        canon = {}
    by = {}
    for t in rows:
        name = canon.get(t["server_name"], t["server_name"])
        by.setdefault(_nk(name), []).append(t)
    scores = models.get_operational_scores(restaurant_id, db_path or models.DB_PATH) or {}
    current = {_nk(n): v for n, v in scores.items()}
    out = []
    for k, ts in by.items():
        if k not in roster or len(ts) < SUGGEST_MIN_TICKETS:
            continue
        sales = sum(float(t["net_sales"]) for t in ts)
        covers = sum(int(t["guest_count"]) for t in ts)
        expected = sum(int(t["guest_count"]) * house_spc.get(t.get("mealtime") or "", 0.0) for t in ts)
        if not covers or not expected:
            continue
        ratio = sales / expected
        score = _band(ratio)
        cur = current.get(k)
        out.append({"name": roster[k], "tickets": len(ts), "covers": covers,
                    "sales_per_cover": round(sales / covers, 2),
                    "house_sales_per_cover": round(expected / covers, 2), "ratio": round(ratio, 3),
                    "suggested": score, "current": cur, "differs": cur is None or int(cur) != score,
                    "reason": (f"{roster[k]} sold ${sales / covers:,.2f} a guest over {len(ts)} tickets against "
                               f"${expected / covers:,.2f} for the house at the same mealtimes "
                               f"({int(round((ratio - 1) * 100)):+d}%).")})
    out.sort(key=lambda x: (-x["tickets"], x["name"]))
    return {"available": bool(out), "window": [start.isoformat(), end.isoformat()], "servers": out,
            "min_tickets": SUGGEST_MIN_TICKETS,
            "note": ("Measured from the POS's own tickets: what each server sells a guest against the house at the "
                     "same mealtimes. A suggestion only — nothing is rated until you confirm it.")}


def confirm_suggested_rating(restaurant_id, name, score=None, user=None, db_path=None) -> dict:
    """The account holder confirms a suggested rating (or sets their own
    score) — then, and only then, it is an Operational Score, written as
    theirs (models.set_capability with their login: authority principal).
    An admin through view-as, or a login that is not an account holder,
    is refused in words (ValueError)."""
    import models
    from permissions import answer_authority
    if answer_authority(user or {}) != "principal":
        raise ValueError("Only the account holder, signed in as themselves, can confirm a measured rating.")
    sug = {_nk(s["name"]): s for s in suggested_ratings(restaurant_id, db_path=db_path).get("servers") or []}
    s = sug.get(_nk(name))
    if s is None:
        raise ValueError("There is no measured suggestion for that person.")
    final = s["suggested"] if score in (None, "") else int(score)
    kw = {"db_path": db_path} if db_path else {}
    before = (models.get_capabilities(restaurant_id, **kw).get(s["name"]) or {}).get("overall")
    out = models.set_capability(restaurant_id, s["name"], "overall", score=final,
                                updated_by=(user or {}).get("username") or (user or {}).get("email") or "owner",
                                user=user, **kw)
    try:
        models.record_capability_change(restaurant_id, "rating", subject=s["name"], attribute="overall",
                                        before=before, after=out,
                                        changed_by=(user or {}).get("username") or "owner")
    except Exception as e:
        _capture(e, "rating change record", restaurant_id)
    return {"ok": True, "name": s["name"], "score": final, "suggested": s["suggested"]}


def _note_ratings(ctx) -> list:
    """The servers whose measured suggestion differs from their counted
    rating (or who have none), kept once a night for the action queue
    (`ratings_suggested`) — the tickets are read here, never on a page load."""
    sug = suggested_ratings(ctx.rid, db_path=ctx.db, today=ctx.today)
    names = [x["name"] for x in sug.get("servers") or [] if x.get("differs")]
    observe(ctx.rid, "ratings_suggested", value={"names": names, "window": sug.get("window")}, origin="system",
            phase="as_run", authority="system", source="pos_tickets", db_path=ctx.db, fact_key="ratings_suggested")
    return names


def ratings_waiting(restaurant_id, db_path=None) -> list:
    """The names last night's read found waiting on the owner's confirmation,
    less anyone the owner has rated since."""
    import models
    row = latest(restaurant_id, "ratings_suggested", db_path=db_path)
    names = list(((row or {}).get("value") or {}).get("names") or [])
    if not names:
        return []
    at = str(row.get("updated_at") or row.get("created_at") or "")
    caps = models.get_capabilities(restaurant_id, attribute="overall", **({"db_path": db_path} if db_path else {}))
    since = {_nk(n) for n, c in caps.items() if str((c.get("overall") or {}).get("updated_at") or "") >= at}
    return [n for n in names if _nk(n) not in since]


# ── the memory for other surfaces (memory_context provider) ───────────────

def memory_lines(req) -> list:
    """memory_context provider "schedule_memory" (the labor read and Ask —
    never the schedule surface, which reads prompt_lines in its own learned
    budget): each candidate or applied fact the memory holds, ranked by its
    confidence and tied to its weekday (`labor:day:<weekday>`), the figures
    as a trusted `measured` line under the fenced words (names are people's
    words). The other learners' facts are left to their own providers (the
    people memory's attendance), and a learned "keep apart" is the account
    holders' alone."""
    rid = req.restaurant_id
    db = getattr(req, "db_path", None)
    if not _learns(rid, db):
        return []
    out = []
    for r in _rows(rid, ("candidate", "active", "rule"), db, kinds=PROMPT_KINDS + ("headcount_add", "headcount_cut")):
        value = _loads(r.get("value_json")) or {}
        if value.get("conflict"):
            continue
        conf = r.get("confidence")
        held = r["status"] in ("active", "rule") and (r["kind"] in SIGNAL_KINDS or r["kind"] in BOUND_ELSEWHERE)
        measured = (f"Measured: {int(r.get('hits') or 0)} of {int(r.get('opportunities') or 0)}"
                    + (f", {_pct(conf)} sure" if conf is not None else "")
                    + ("; held by the schedule's checks" if held else "; not held yet") + ".")
        line = {"text": str(r.get("text") or _short(dict(r, value=value))), "date": r.get("last_seen"),
                "source": "system", "subject": f"labor:day:{str(r.get('day') or '').lower()}" if r.get("day") else "labor",
                "weight": float(conf or 0) + (1.0 if held else 0.0), "trusted": False, "module": "labor",
                "audience": "team", "measured": measured if r.get("opportunities") else None}
        if r["kind"] == "pair" and value.get("kind") == "avoid":
            line["audience"] = "principals"
        out.append(line)
    return out


def usual_sections(restaurant_id, db_path=None) -> list:
    """[{employee, day, daypart, section, confidence, status, because}] —
    each server's usual floor section on a weekday and daypart, as the
    memory holds it (candidate or applied), for the Studio's section picker
    to suggest beside a shift that has none (L-22: what the manager keeps
    choosing is offered, never assigned by code — a section is the
    manager's call on the night)."""
    if not _learns(restaurant_id, db_path):
        return []
    out = []
    for r in _rows(restaurant_id, ("candidate", "active"), db_path, kinds=("section",)):
        value = _loads(r.get("value_json")) or {}
        if not value.get("section"):
            continue
        out.append({"employee": r.get("person"), "day": r.get("day"), "daypart": r.get("daypart"),
                    "section": value["section"], "confidence": r.get("confidence"), "status": r["status"],
                    "because": r.get("text")})
    return out
