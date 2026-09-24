"""
rec_ledger.py — one identity and one event trail for every recommendation.

A recommendation used to live separately on every surface that said it:
Home kept `home_dismissals`, the schedule kept `schedule_recommendation_events`,
Ask kept `ask_cavnar_actions`, the brief and the alerts kept nothing. So an
owner's "no" on Home did not reach the brief or the alert saying the same
thing, the same news arrived five ways, and nobody could say how often a
recommendation was shown, let alone acted on.

Here a recommendation is an EPISODE: (restaurant, key) from the first time
any surface shows it until the owner answers it or it goes stale. Keys carry
their subject ("trim_day:Monday", "reprice:Carbonara") so a different day or
dish is a different recommendation. Every surface that shows one calls
`present()`; every answer calls `record()`; every surface that is about to
say something asks `silenced()` first. The admin console reads the trail
(admin_ops.recommendation_acceptance) — owners never see these numbers.

Events:  shown · opened · evidence_viewed · accepted · dismissed · snoozed ·
         completed · outcome · expired
Status:  open → accepted | completed | dismissed | expired  (snoozed stays
         open with snoozed_until)

Writes are small and bounded: a `shown` is one row per episode, surface and
day however many times a page renders. Pure SQL; no model, no network.
"""
import json
import uuid
from datetime import datetime, timedelta

import models as _models_mod
from models import DB_PATH

EVENTS = ("shown", "opened", "evidence_viewed", "accepted", "dismissed", "snoozed",
          "completed", "outcome", "expired")
TERMINAL = {"accepted": "accepted", "completed": "completed", "dismissed": "dismissed", "expired": "expired"}
MODULES = ("reviews", "labor", "schedule", "food", "marketing", "intel", "guests", "ops", "home", "ask")
SURFACES = ("home", "brief_email", "brief_push", "weekly_email", "alert_sms", "alert_email", "alert_push",
            "queue", "ask", "schedule_review", "labor", "reviews", "food", "marketing", "intel", "issue_sms",
            "digest", "monthly_email", "ios", "web", "auto", "unknown", "dsr", "dsr_email")
# What each surface is called where a person reads it (the admin console's
# "by surface" breakdown). Every surface has one; a test holds them in step.
SURFACE_LABELS = {
    "home": "home", "brief_email": "brief email", "brief_push": "brief push", "weekly_email": "weekly email",
    "alert_sms": "alert text", "alert_email": "alert email", "alert_push": "alert push", "queue": "queue",
    "ask": "ask", "schedule_review": "schedule review", "labor": "labor", "reviews": "reviews", "food": "food",
    "marketing": "marketing", "intel": "intel", "issue_sms": "issue text", "digest": "digest",
    "monthly_email": "monthly email", "ios": "ios", "web": "web", "auto": "automatic", "unknown": "unknown",
    "dsr": "daily report", "dsr_email": "daily report email",
}


def surface_label(surface) -> str:
    return SURFACE_LABELS.get(surface, str(surface or "unknown"))

# How long each answer silences the same key everywhere.
SILENCE_DAYS = {"hide": 14, "not_for_us": 3650, "done": 3650}
# An accepted or completed recommendation is not re-asked while its outcome
# is being measured.
ACCEPTED_QUIET_DAYS = 14
# An open episode nobody has answered in this long is ignored, not pending:
# it closes as `expired`, and the next showing starts a new episode.
EXPIRE_AFTER_DAYS = 14
# Events that describe an episode someone already saw and never begin one:
# a tracker's verdict, an alert opened, the evidence read. An outcome with
# no episode behind it is an episode nobody was shown (H-5).
NON_OPENING = ("outcome", "opened", "evidence_viewed")
# Keys the ledger holds as bookkeeping rather than as advice an owner was
# shown: the moment a quiet kind was restored (decisions.restore_kind), the
# quality weights applied, an on-call ask sent. They stay in the trail —
# restore_kind's stamp is what decisions.quiet_kinds counts from — and stay
# out of every acceptance figure (admin_ops), so none is read as a
# recommendation that was taken.
BOOKKEEPING_PREFIXES = ("restore_kind:", "calibration:", "standby:")
# How close (seconds) an answer the ledger already holds must be to an older
# ledger's row for sync_existing to treat the row as that same answer.
SAME_ANSWER_SECONDS = 300


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def init_rec_ledger(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS rec_instances (
            rec_id            TEXT PRIMARY KEY,
            restaurant_id     INTEGER NOT NULL,
            key               TEXT NOT NULL,
            module            TEXT,
            kind              TEXT,
            title             TEXT,
            status            TEXT NOT NULL DEFAULT 'open',
            dollar_value      REAL,
            confidence_band   TEXT,
            evidence_sources  TEXT,
            cross_module      INTEGER DEFAULT 0,
            model_written     INTEGER DEFAULT 0,
            cavnar_completes  INTEGER DEFAULT 0,
            expected_metric   TEXT,
            expected_by       TEXT,
            first_surface     TEXT,
            first_position    INTEGER,
            silenced_until    TEXT,
            snoozed_until     TEXT,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            last_event_at     TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at         TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_inst_key ON rec_instances(restaurant_id, key, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_inst_open ON rec_instances(status, last_event_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_inst_created ON rec_instances(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS rec_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_id         TEXT NOT NULL,
            restaurant_id  INTEGER NOT NULL,
            key            TEXT NOT NULL,
            event          TEXT NOT NULL,
            surface        TEXT,
            user_id        INTEGER,
            role           TEXT,
            dedupe         TEXT NOT NULL,
            meta           TEXT,
            at             TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(rec_id, dedupe)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_ev_rec ON rec_events(rec_id, event)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_ev_at ON rec_events(at)")
        conn.commit()
    finally:
        conn.close()


# ── keys ─────────────────────────────────────────────────────────────────────

def rec_key(kind: str, subject=None) -> str:
    """"kind:subject" — the identity every surface uses for one recommendation."""
    kind = str(kind or "").strip()
    subject = str(subject).strip() if subject not in (None, "") else ""
    return (f"{kind}:{subject}" if subject else kind)[:160]


def kind_of(key: str) -> str:
    return (str(key or "").split(":", 1)[0] or "unknown")[:60]


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _stamp(v):
    """A stored timestamp as the ledger's own 'YYYY-MM-DD HH:MM:SS' (UTC),
    or None. Accepts sqlite datetime('now') text, isoformat and datetimes;
    an aware value is converted to UTC."""
    if not v:
        return None
    if isinstance(v, datetime):
        d = v
    else:
        t = str(v).strip().replace("T", " ").replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(t)
        except ValueError:
            try:
                d = datetime.strptime(t[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if d.tzinfo is not None:
        from datetime import timezone as _tz
        d = d.astimezone(_tz.utc).replace(tzinfo=None)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def counts_in_acceptance(key) -> bool:
    """Whether an episode under this key is a recommendation the acceptance
    figures count (BOOKKEEPING_PREFIXES are not)."""
    return not str(key or "").startswith(BOOKKEEPING_PREFIXES)


def stale_cutoff(days=EXPIRE_AFTER_DAYS, now=None) -> str:
    """Episodes created before this and still unanswered are ignored."""
    return ((now or datetime.utcnow()) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def is_stale(row, now=None, days=EXPIRE_AFTER_DAYS) -> bool:
    """THE expiry rule, read the same way by _open_or_new, expire_stale and
    the admin page's `ignored`: an episode still open (no accept, complete
    or dismiss) that was CREATED more than `days` ago and is not inside a
    snooze. Measured from creation, not from the last event: a card shown
    every day bumps its last event daily, so it never expired and the
    recommendations owners ignore most were never counted as ignored."""
    if row is None or row["status"] != "open":
        return False
    now_s = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    if row["snoozed_until"] and row["snoozed_until"] > now_s:
        return False
    return (row["created_at"] or "") < stale_cutoff(days, now)


def _latest(conn, rid, key):
    return conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND key=? ORDER BY created_at DESC, rowid DESC "
                        "LIMIT 1", (rid, key)).fetchone()


def _episode_at(conn, rid, key, at):
    """The episode that was current at `at`: the latest created at or before
    it. An answer belongs to what the owner was looking at when they gave
    it, never to an episode shown afterwards."""
    return conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND key=? AND created_at <= ? "
                        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (rid, key, at)).fetchone()


def _silenced_row(row, now=None) -> bool:
    if row is None:
        return False
    now = now or _now()
    if row["silenced_until"] and row["silenced_until"] > now:
        return True
    if row["status"] == "open" and row["snoozed_until"] and row["snoozed_until"] > now:
        return True
    return False


def _open_or_new(conn, rid, key, module, kind, title=None, attrs=None, surface=None, position=None, created_at=None):
    """The episode a showing belongs to: the open one, else a new one — unless
    the latest was answered and is still silencing (then None)."""
    row = _latest(conn, rid, key)
    now = _now()
    if row is not None:
        if row["status"] == "open":
            if not is_stale(row):
                return row["rec_id"]
            _close(conn, row["rec_id"], rid, key, "expired", meta={"reason": "no answer"})
        elif _silenced_row(row, now):
            return None
    attrs = attrs or {}
    rec_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, dollar_value, confidence_band, "
        "evidence_sources, cross_module, model_written, cavnar_completes, expected_metric, expected_by, first_surface, "
        "first_position, created_at, last_event_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec_id, rid, key, module, kind or kind_of(key), (title or "")[:200] or None,
         _num(attrs.get("dollar_value")), attrs.get("confidence_band"),
         json.dumps(sorted(set(attrs.get("evidence_sources") or []))) if attrs.get("evidence_sources") else None,
         1 if (attrs.get("cross_module") or len(set(attrs.get("evidence_sources") or [])) > 1) else 0,
         1 if attrs.get("model_written") else 0, 1 if attrs.get("cavnar_completes") else 0,
         attrs.get("expected_metric"), attrs.get("expected_by"), surface, position, created_at or now,
         created_at or now))
    return rec_id


def _num(v):
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _add_event(conn, rec_id, rid, key, event, surface=None, user_id=None, role=None, dedupe=None, meta=None, at=None):
    at = at or _now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO rec_events (rec_id, restaurant_id, key, event, surface, user_id, role, dedupe, meta, at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rec_id, rid, key, event, surface, user_id, (role or None), dedupe or f"{event}:{uuid.uuid4().hex}",
         json.dumps(meta)[:2000] if meta else None, at))
    if cur.rowcount:
        conn.execute("UPDATE rec_instances SET last_event_at=MAX(last_event_at, ?) WHERE rec_id=?", (at, rec_id))
    return bool(cur.rowcount)


def _close(conn, rec_id, rid, key, status, meta=None, at=None):
    # An answer given to an episode that had already expired is still an
    # answer: the episode takes the answer's status. It stayed 'expired',
    # so the admin acceptance view and quiet_kinds counted an answered
    # recommendation as ignored (M-31, H-22). Expiry itself only ever
    # closes an open episode.
    allowed = "('open')" if status == "expired" else "('open', 'expired')"
    conn.execute(f"UPDATE rec_instances SET status=?, closed_at=? WHERE rec_id=? AND status IN {allowed}",
                 (status, at or _now(), rec_id))
    if status == "expired":
        _add_event(conn, rec_id, rid, key, "expired", dedupe="expired", meta=meta)


# ── the calls every surface makes ────────────────────────────────────────────

def present(restaurant_id, key, module, surface, title=None, kind=None, position=None, user_id=None,
            db_path=DB_PATH, **attrs):
    """A surface is showing this recommendation. Returns its rec_id, or None
    when the owner has already answered it (the caller should not show it).

    attrs: dollar_value, confidence_band, evidence_sources (list of modules),
    cross_module, model_written, cavnar_completes, expected_metric,
    expected_by. One `shown` row per episode, surface and day."""
    out = present_many(restaurant_id, [dict(key=key, module=module, title=title, kind=kind, position=position,
                                            **attrs)], surface, user_id=user_id, db_path=db_path)
    return out.get(key)


def present_many(restaurant_id, items: list, surface: str, user_id=None, db_path=DB_PATH) -> dict:
    """{key: rec_id or None} for a batch shown together (a Home page, a
    brief), on one connection. Never raises: measurement must not take a
    surface down."""
    out = {}
    if not restaurant_id or not items:
        return out
    day = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        conn = get_conn(db_path)
    except Exception as e:
        print(f"[rec_ledger] present unavailable: {e}")
        return out
    try:
        for i, it in enumerate(items):
            key = str(it.get("key") or "").strip()[:160]
            if not key:
                continue
            attrs = {k: it.get(k) for k in ("dollar_value", "confidence_band", "evidence_sources", "cross_module",
                                             "model_written", "cavnar_completes", "expected_metric", "expected_by")}
            pos = it.get("position") if it.get("position") is not None else i
            rec_id = _open_or_new(conn, restaurant_id, key, it.get("module"), it.get("kind"), it.get("title"), attrs,
                                  surface, pos)
            out[key] = rec_id
            if rec_id:
                _add_event(conn, rec_id, restaurant_id, key, "shown", surface=surface, user_id=user_id,
                           dedupe=f"shown:{surface}:{day}", meta={"position": pos})
        conn.commit()
    except Exception as e:
        print(f"[rec_ledger] present failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return out


def record(restaurant_id, key, event, surface=None, user_id=None, role=None, meta=None, source_ref=None,
           silence_days=None, snooze_until=None, at=None, silence_until=None, db_path=DB_PATH) -> bool:
    """The owner (or Cavnar on their behalf) did something with a
    recommendation. Terminal events close the episode; `dismissed` and
    `completed`/`accepted` silence the key everywhere for SILENCE_DAYS /
    ACCEPTED_QUIET_DAYS (or `silence_days`, or an absolute `silence_until`).
    Never raises.

    `source_ref` names the answer: the same answer recorded again (a sync,
    a retry) is a no-op for this key across EVERY episode, not only the
    latest — a nightly replay of an old answer used to land on each new
    episode and close it.

    `at` is when the answer was given, for an answer carried in from
    another table (sync_existing). It attaches to the episode that was
    current then, never to one shown afterwards, and its silence runs from
    `at`, not from the moment it was copied.

    An outcome, an open or an evidence view never starts an episode: with
    nothing shown behind it there is nothing to attach it to."""
    if event not in EVENTS:
        raise ValueError(f"unknown recommendation event {event}")
    key = str(key or "").strip()[:160]
    if not restaurant_id or not key:
        return False
    when = _stamp(at) if at else None
    try:
        conn = get_conn(db_path)
    except Exception as e:
        print(f"[rec_ledger] record unavailable: {e}")
        return False
    try:
        dedupe = f"{event}:{source_ref}" if source_ref else None
        if dedupe and conn.execute("SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND dedupe=? LIMIT 1",
                                   (restaurant_id, key, dedupe)).fetchone():
            return False
        if when:
            row = _episode_at(conn, restaurant_id, key, when)
            if row is None and _latest(conn, restaurant_id, key) is not None:
                # Every episode of this key began after the answer: whatever
                # it answered was never logged, and it cannot answer what
                # the owner has been shown since.
                return False
        else:
            row = _latest(conn, restaurant_id, key)
        if row is None:
            if event in NON_OPENING:
                return False
            # Answered before any surface logged showing it (an alert, an
            # older client): the episode starts at the answer.
            rec_id = _open_or_new(conn, restaurant_id, key, (meta or {}).get("module"), kind_of(key), surface=surface,
                                  created_at=when)
            if rec_id is None:
                conn.commit()
                return False
        else:
            rec_id = row["rec_id"]
        added = _add_event(conn, rec_id, restaurant_id, key, event, surface=surface, user_id=user_id, role=role,
                           dedupe=dedupe, meta=meta, at=when)
        base = when or _now()
        if added:
            if event in TERMINAL:
                _close(conn, rec_id, restaurant_id, key, TERMINAL[event], at=when)
            if event == "dismissed":
                days = silence_days or SILENCE_DAYS.get((meta or {}).get("kind") or "hide", SILENCE_DAYS["hide"])
                conn.execute("UPDATE rec_instances SET silenced_until=COALESCE(?, datetime(?, ?)) WHERE rec_id=?",
                             (_stamp(silence_until), base, f"+{int(days)} days", rec_id))
            elif event in ("accepted", "completed"):
                conn.execute("UPDATE rec_instances SET silenced_until=MAX(COALESCE(silenced_until, ''), "
                             "COALESCE(?, datetime(?, ?))) WHERE rec_id=?",
                             (_stamp(silence_until), base, f"+{int(silence_days or ACCEPTED_QUIET_DAYS)} days", rec_id))
            elif event == "snoozed":
                until = snooze_until or (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute("UPDATE rec_instances SET snoozed_until=? WHERE rec_id=?", (str(until), rec_id))
        conn.commit()
        return added
    except Exception as e:
        print(f"[rec_ledger] record failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def recorded(restaurant_id, key, event, source_ref, db_path=DB_PATH) -> bool:
    """Whether this answer (`event` under `source_ref`) is already in the
    trail for this key, on any episode. Never raises."""
    key = str(key or "").strip()[:160]
    if not restaurant_id or not key or not source_ref:
        return False
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        return bool(conn.execute("SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND dedupe=? LIMIT 1",
                                 (restaurant_id, key, f"{event}:{source_ref}")).fetchall())
    except Exception:
        return False
    finally:
        conn.close()


def unsilence(restaurant_id, key, db_path=DB_PATH) -> bool:
    """The owner took an answer back ("Use again"): the key can be shown."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return False
    try:
        n = conn.execute("UPDATE rec_instances SET silenced_until=NULL, snoozed_until=NULL WHERE restaurant_id=? AND key=?",
                         (restaurant_id, str(key or "")[:160])).rowcount
        conn.commit()
        return bool(n)
    finally:
        conn.close()


def silenced(restaurant_id, key, db_path=DB_PATH) -> bool:
    return str(key or "")[:160] in silenced_keys(restaurant_id, db_path=db_path)


def silenced_keys(restaurant_id, db_path=DB_PATH) -> set:
    """Every key an answer is currently silencing for this restaurant — on
    any surface. Includes Home's own dismissals, so an answer given before
    the ledger existed still holds."""
    now = _now()
    out = set()
    try:
        conn = get_conn(db_path)
    except Exception:
        return out
    try:
        for r in conn.execute(
                "SELECT key FROM rec_instances WHERE restaurant_id=? AND ((silenced_until IS NOT NULL AND silenced_until > ?) "
                "OR (status='open' AND snoozed_until IS NOT NULL AND snoozed_until > ?))", (restaurant_id, now, now)):
            out.add(r["key"])
        try:
            for r in conn.execute("SELECT key FROM home_dismissals WHERE restaurant_id=? AND expires_at > datetime('now')",
                                  (restaurant_id,)):
                out.add(r["key"])
        except Exception:
            pass
    except Exception as e:
        print(f"[rec_ledger] silenced_keys failed: {e}")
    finally:
        conn.close()
    return out


def expire_stale(db_path=DB_PATH, days=EXPIRE_AFTER_DAYS) -> int:
    """Close open episodes nobody answered within `days` of being created
    (is_stale) as expired — the 'shown and ignored' outcome the acceptance
    rate needs in its denominator. Bounded: at most 5000 per call."""
    now = datetime.utcnow()
    cutoff, now_s = stale_cutoff(days, now), now.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    n = 0
    try:
        rows = conn.execute("SELECT rec_id, restaurant_id, key FROM rec_instances WHERE status='open' AND created_at < ? "
                            "AND (snoozed_until IS NULL OR snoozed_until <= ?) LIMIT 5000", (cutoff, now_s)).fetchall()
        for r in rows:
            _close(conn, r["rec_id"], r["restaurant_id"], r["key"], "expired", meta={"reason": "no answer"})
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def _answered_near(conn, rid, key, events, at, not_dedupe=None) -> bool:
    """Whether the trail already holds one of `events` for this key within
    SAME_ANSWER_SECONDS of `at` — the answer a surface wrote itself when it
    was given, which an older ledger's row for it must not repeat."""
    t = _stamp(at)
    key = str(key or "").strip()[:160]
    if not t or not key or not events:
        return False
    marks = ",".join("?" for _ in events)
    span = f"{int(SAME_ANSWER_SECONDS)} seconds"
    return bool(conn.execute(
        f"SELECT 1 FROM rec_events WHERE restaurant_id=? AND key=? AND event IN ({marks}) "
        "AND at BETWEEN datetime(?, ?) AND datetime(?, ?) AND dedupe != ? LIMIT 1",
        (rid, key, *events, t, "-" + span, t, "+" + span, not_dedupe or "")).fetchall())


def sync_existing(db_path=DB_PATH, days=120) -> dict:
    """Carry answers the older ledgers hold into the trail: Home dismissals
    (and snoozes), outcome verdicts, issue resolutions, Ask confirms and
    dismissals, schedule recommendation answers. Scheduled nightly.

    Idempotent per (restaurant, key, answer) across every episode — the
    same row re-read each night is one answer, not one per new episode.
    Each answer attaches to the episode current when it was given (`at`)
    and silences from then, so it never closes a recommendation shown after
    it. A Home snooze stays a snooze (to its own expiry). Answers a surface
    already wrote to the trail itself are not written twice."""
    counts = {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1
    conn = get_conn(db_path)
    since = f"-{int(days)} days"
    try:
        try:
            home = conn.execute("SELECT restaurant_id, key, kind, dismissed_by, dismissed_at, expires_at FROM home_dismissals "
                                "WHERE dismissed_at >= datetime('now', ?)", (since,)).fetchall()
        except Exception:
            home = []
        try:
            outs = conn.execute("SELECT id, restaurant_id, source_key, verdict, status, created_at FROM recommendation_outcomes "
                                "WHERE created_at >= datetime('now', ?)", (since,)).fetchall()
        except Exception:
            outs = []
        try:
            iss = conn.execute("SELECT id, restaurant_id, source_key, status, resolved_at, acknowledged_at FROM ops_issues "
                               "WHERE source_key IS NOT NULL AND created_at >= datetime('now', ?)", (since,)).fetchall()
        except Exception:
            iss = []
        try:
            asks = conn.execute("SELECT id, restaurant_id, action, outcome, created_at, proposal_id FROM ask_cavnar_actions "
                                "WHERE created_at >= datetime('now', ?)", (since,)).fetchall()
        except Exception:
            asks = []
        try:
            sched = conn.execute("SELECT id, restaurant_id, kind, key, action, created_at FROM schedule_recommendation_events "
                                 "WHERE action IN ('accepted','dismissed') AND created_at >= datetime('now', ?)",
                                 (since,)).fetchall()
        except Exception:
            sched = []

        for r in home:
            rid, key, kind = r["restaurant_id"], r["key"], r["kind"]
            extra = {}
            if kind == "snooze":
                # "Not today" is a snooze to the row's own expiry — never a
                # fortnight's dismissal.
                ev, meta = "snoozed", {"until": r["expires_at"]}
                extra["snooze_until"] = _stamp(r["expires_at"])
            elif kind == "done":
                ev, meta = "completed", {"kind": "done"}
                extra["silence_until"] = r["expires_at"]
            else:
                ev, meta = "dismissed", {"kind": "not_for_us" if kind == "not_for_us" else "hide"}
                extra["silence_until"] = r["expires_at"]
            if _answered_near(conn, rid, key, (ev,), r["dismissed_at"]):
                continue            # home_brief.dismiss wrote this answer itself
            if record(rid, key, ev, surface="home", user_id=r["dismissed_by"], meta=meta,
                      source_ref=f"home:{key}:{r['dismissed_at']}", at=r["dismissed_at"], db_path=db_path, **extra):
                bump("home")
        for r in outs:
            key = r["source_key"] or ""
            # An observed tracker ("observed:schedule_published:2026-09")
            # measures something the owner did unprompted — no
            # recommendation was shown, so there is no episode to measure.
            if key.startswith("observed:"):
                continue
            if r["verdict"] in ("improved", "worsened", "no_clear_change"):
                # The verdict belongs to the episode that was taken when the
                # tracker started, not to whichever is newest when it lands.
                if record(r["restaurant_id"], key, "outcome", meta={"verdict": r["verdict"]},
                          source_ref=f"outcome:{r['id']}", at=r["created_at"], db_path=db_path):
                    bump("outcomes")
        for r in iss:
            # issues._resolve records the resolution under this same
            # source_ref; the acknowledgement arrives only through here.
            if r["resolved_at"]:
                if record(r["restaurant_id"], r["source_key"], "completed", surface="issue_sms",
                          source_ref=f"issue:{r['id']}:resolved", at=r["resolved_at"], db_path=db_path):
                    bump("issues")
            elif r["acknowledged_at"]:
                if record(r["restaurant_id"], r["source_key"], "accepted", surface="issue_sms",
                          source_ref=f"issue:{r['id']}:ack", at=r["acknowledged_at"], db_path=db_path):
                    bump("issues")
        for r in asks:
            ev = {"confirmed": "accepted", "dismissed": "dismissed"}.get(r["outcome"])
            if not ev:
                continue
            # Answers name the proposal they settle (ask:<proposal id>, the key
            # the card was shown under), and client_api records them under
            # "ask:<proposal id>:<outcome>" itself; an older client's answer
            # names no proposal.
            if r["proposal_id"]:
                akey, ref = rec_key("ask", r["proposal_id"]), f"ask:{r['proposal_id']}:{r['outcome']}"
            else:
                akey, ref = rec_key("ask", r["action"]), f"ask:{r['id']}"
            if _answered_near(conn, r["restaurant_id"], akey, (ev,), r["created_at"]):
                continue
            if record(r["restaurant_id"], akey, ev, surface="ask",
                      meta={"kind": "hide"} if ev == "dismissed" else None,
                      source_ref=ref, at=r["created_at"], db_path=db_path):
                bump("ask")
        from schedule_intel import schedule_rec_key
        for r in sched:
            # The key the recommendation was shown under (schedule_engine
            # presents schedule_rec_key) — a private truncation here filed
            # long sentences under a key nobody was shown.
            key = schedule_rec_key(r["kind"], r["key"])
            ev = "accepted" if r["action"] == "accepted" else "dismissed"
            # The button (strategy_routes._do_recommendation_event) writes
            # the trail itself; an edit that carried a recommendation out
            # (schedule_versions) reaches it only through here.
            if _answered_near(conn, r["restaurant_id"], key, (ev,), r["created_at"]):
                continue
            if record(r["restaurant_id"], key, ev, surface="schedule_review",
                      meta={"kind": "not_for_us"} if ev == "dismissed" else None,
                      source_ref=f"sched:{r['id']}", at=r["created_at"], db_path=db_path):
                bump("schedule")
    finally:
        conn.close()
    return counts


# ── one-off repair of what the replaying sync wrote (H-1) ────────────────────

_SYNC_DEDUPE_PATTERNS = ("dismissed:home:%", "completed:home:%", "completed:issue:%", "accepted:issue:%",
                         "accepted:ask:%", "dismissed:ask:%", "accepted:sched:%", "dismissed:sched:%",
                         "outcome:outcome:%")


def _sync_source_time(conn, ev):
    """(when the synced answer was really given, the direct events that
    would be the same answer) for one event the old sync wrote — or
    (None, ()) when its source row is gone and nothing can be said with
    certainty."""
    import re
    dd, key, event, rid = ev["dedupe"], ev["key"], ev["event"], ev["restaurant_id"]

    def one(sql, args):
        try:
            row = conn.execute(sql, args).fetchall()
        except Exception:
            return None
        return _stamp(row[0][0]) if row and row[0][0] else None
    home = f"{event}:home:{key}:"
    if dd.startswith(home):
        same = ("dismissed", "snoozed") if event == "dismissed" else (event,)
        return _stamp(dd[len(home):]), same
    m = re.fullmatch(r"(?:completed|accepted):issue:(\d+):(resolved|ack)", dd)
    if m:
        col = "resolved_at" if m.group(2) == "resolved" else "acknowledged_at"
        return one(f"SELECT {col} FROM ops_issues WHERE id=? AND restaurant_id=?", (int(m.group(1)), rid)), ()
    m = re.fullmatch(r"(?:accepted|dismissed):ask:(\d+)", dd)
    if m:
        return one("SELECT created_at FROM ask_cavnar_actions WHERE id=? AND restaurant_id=?",
                   (int(m.group(1)), rid)), (event,)
    m = re.fullmatch(r"(?:accepted|dismissed):sched:(\d+)", dd)
    if m:
        return one("SELECT created_at FROM schedule_recommendation_events WHERE id=? AND restaurant_id=?",
                   (int(m.group(1)), rid)), (event,)
    m = re.fullmatch(r"outcome:outcome:(\d+)", dd)
    if m:
        return one("SELECT created_at FROM recommendation_outcomes WHERE id=? AND restaurant_id=?",
                   (int(m.group(1)), rid)), ()
    return None, ()


_REPAIR_MARK = "rec_ledger:repair_sync_replays:v1"


def repair_sync_replays(db_path=DB_PATH, force=False) -> dict:
    """Undo what sync_existing wrote while it replayed old answers onto new
    episodes. A one-off: the nightly rec_ledger job calls it just before the
    sync (which then re-carries any removed answer onto the episode it
    really belongs to), and once it has completed it leaves a mark in
    job_cursors and does nothing on later nights. Idempotent regardless —
    the fixed sync writes nothing this matches — so `force` re-runs it
    safely.

    Removed, because each is certain from the rows themselves:
      * an answer written onto an episode that was created (and shown)
        after the answer was given — the source row still says when;
      * a Home "Not today" replayed as a 14-day dismissal, or any synced
        answer duplicating one the surface wrote itself — the direct event
        sits within SAME_ANSWER_SECONDS of the source row's own time.
    An episode left with no accept, complete or dismiss is reopened and its
    silence lifted (a lapsed one then expires on the normal rule).

    Left alone: a synced event whose source row has since gone (a replaced
    home_dismissals row, a pruned table) — without it the time of the
    answer is unknown, so a wrong attachment cannot be told from a right
    one."""
    out = {"removed": 0, "reopened": 0}
    conn = get_conn(db_path)
    try:
        if not force:
            try:
                if conn.execute("SELECT 1 FROM job_cursors WHERE key=?", (_REPAIR_MARK,)).fetchall():
                    return out
            except Exception:
                pass
        where = " OR ".join("e.dedupe LIKE ?" for _ in _SYNC_DEDUPE_PATTERNS)
        rows = conn.execute(
            "SELECT e.id, e.rec_id, e.restaurant_id, e.key, e.event, e.dedupe, e.at, i.created_at AS ep_created "
            f"FROM rec_events e JOIN rec_instances i ON i.rec_id=e.rec_id WHERE ({where})",
            _SYNC_DEDUPE_PATTERNS).fetchall()
        touched = set()
        for ev in rows:
            src, same = _sync_source_time(conn, ev)
            if not src:
                continue
            later = conn.execute("SELECT datetime(?, '+60 seconds') > ?", (src, ev["ep_created"])).fetchone()[0] == 0
            wrong_episode = later and bool(conn.execute(
                "SELECT 1 FROM rec_events WHERE rec_id=? AND event='shown' AND at <= ? LIMIT 1",
                (ev["rec_id"], ev["at"])).fetchall())
            duplicate = bool(same) and _answered_near(conn, ev["restaurant_id"], ev["key"], same, src,
                                                      not_dedupe=ev["dedupe"])
            if not (wrong_episode or duplicate):
                continue
            conn.execute("DELETE FROM rec_events WHERE id=?", (ev["id"],))
            out["removed"] += 1
            touched.add(ev["rec_id"])
        for rec_id in touched:
            still = conn.execute("SELECT 1 FROM rec_events WHERE rec_id=? AND event IN ('accepted','completed','dismissed') "
                                 "LIMIT 1", (rec_id,)).fetchall()
            if still:
                continue
            n = conn.execute("UPDATE rec_instances SET status='open', closed_at=NULL, silenced_until=NULL "
                             "WHERE rec_id=? AND status IN ('accepted','completed','dismissed')", (rec_id,)).rowcount
            out["reopened"] += n
        try:
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?, ?, datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (_REPAIR_MARK, json.dumps(out)))
        except Exception as e:           # no job_cursors table: it simply runs again next night
            print(f"[rec_ledger] repair mark not written: {e}")
        conn.commit()
    except Exception as e:
        print(f"[rec_ledger] repair_sync_replays failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return out
