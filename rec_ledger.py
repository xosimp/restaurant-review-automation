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
            "digest", "ios", "web", "auto", "unknown")

# How long each answer silences the same key everywhere.
SILENCE_DAYS = {"hide": 14, "not_for_us": 3650, "done": 3650}
# An accepted or completed recommendation is not re-asked while its outcome
# is being measured.
ACCEPTED_QUIET_DAYS = 14
# An open episode nobody has answered in this long is ignored, not pending:
# it closes as `expired`, and the next showing starts a new episode.
EXPIRE_AFTER_DAYS = 14


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


def _latest(conn, rid, key):
    return conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND key=? ORDER BY created_at DESC, rowid DESC "
                        "LIMIT 1", (rid, key)).fetchone()


def _silenced_row(row, now=None) -> bool:
    if row is None:
        return False
    now = now or _now()
    if row["silenced_until"] and row["silenced_until"] > now:
        return True
    if row["status"] == "open" and row["snoozed_until"] and row["snoozed_until"] > now:
        return True
    return False


def _open_or_new(conn, rid, key, module, kind, title=None, attrs=None, surface=None, position=None):
    """The episode a showing belongs to: the open one, else a new one — unless
    the latest was answered and is still silencing (then None)."""
    row = _latest(conn, rid, key)
    now = _now()
    if row is not None:
        if row["status"] == "open":
            stale = (datetime.utcnow() - timedelta(days=EXPIRE_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
            if row["last_event_at"] >= stale:
                return row["rec_id"]
            _close(conn, row["rec_id"], rid, key, "expired", meta={"reason": "no answer"})
        elif _silenced_row(row, now):
            return None
    attrs = attrs or {}
    rec_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, dollar_value, confidence_band, "
        "evidence_sources, cross_module, model_written, cavnar_completes, expected_metric, expected_by, first_surface, "
        "first_position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec_id, rid, key, module, kind or kind_of(key), (title or "")[:200] or None,
         _num(attrs.get("dollar_value")), attrs.get("confidence_band"),
         json.dumps(sorted(set(attrs.get("evidence_sources") or []))) if attrs.get("evidence_sources") else None,
         1 if (attrs.get("cross_module") or len(set(attrs.get("evidence_sources") or [])) > 1) else 0,
         1 if attrs.get("model_written") else 0, 1 if attrs.get("cavnar_completes") else 0,
         attrs.get("expected_metric"), attrs.get("expected_by"), surface, position))
    return rec_id


def _num(v):
    try:
        return round(float(v), 2) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _add_event(conn, rec_id, rid, key, event, surface=None, user_id=None, role=None, dedupe=None, meta=None):
    cur = conn.execute(
        "INSERT OR IGNORE INTO rec_events (rec_id, restaurant_id, key, event, surface, user_id, role, dedupe, meta) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rec_id, rid, key, event, surface, user_id, (role or None), dedupe or f"{event}:{uuid.uuid4().hex}",
         json.dumps(meta)[:2000] if meta else None))
    if cur.rowcount:
        conn.execute("UPDATE rec_instances SET last_event_at=datetime('now') WHERE rec_id=?", (rec_id,))
    return bool(cur.rowcount)


def _close(conn, rec_id, rid, key, status, meta=None):
    # An answer given to an episode that had already expired is still an
    # answer: the episode takes the answer's status. It stayed 'expired',
    # so the admin acceptance view and quiet_kinds counted an answered
    # recommendation as ignored (M-31, H-22). Expiry itself only ever
    # closes an open episode.
    allowed = "('open')" if status == "expired" else "('open', 'expired')"
    conn.execute(f"UPDATE rec_instances SET status=?, closed_at=datetime('now') WHERE rec_id=? AND status IN {allowed}",
                 (status, rec_id))
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
           silence_days=None, snooze_until=None, db_path=DB_PATH) -> bool:
    """The owner (or Cavnar on their behalf) did something with a
    recommendation. `source_ref` makes a re-recorded answer (a sync, a
    retry) a no-op. Terminal events close the episode; `dismissed` and
    `completed`/`accepted` silence the key everywhere for SILENCE_DAYS /
    ACCEPTED_QUIET_DAYS. Never raises."""
    if event not in EVENTS:
        raise ValueError(f"unknown recommendation event {event}")
    key = str(key or "").strip()[:160]
    if not restaurant_id or not key:
        return False
    try:
        conn = get_conn(db_path)
    except Exception as e:
        print(f"[rec_ledger] record unavailable: {e}")
        return False
    try:
        row = _latest(conn, restaurant_id, key)
        if row is None:
            # Answered before any surface logged showing it (an alert, an
            # older client): the episode starts at the answer.
            rec_id = _open_or_new(conn, restaurant_id, key, (meta or {}).get("module"), kind_of(key), surface=surface)
            if rec_id is None:
                conn.commit()
                return False
        else:
            rec_id = row["rec_id"]
        dedupe = f"{event}:{source_ref}" if source_ref else None
        added = _add_event(conn, rec_id, restaurant_id, key, event, surface=surface, user_id=user_id, role=role,
                           dedupe=dedupe, meta=meta)
        if added:
            if event in TERMINAL:
                _close(conn, rec_id, restaurant_id, key, TERMINAL[event])
            if event == "dismissed":
                days = silence_days or SILENCE_DAYS.get((meta or {}).get("kind") or "hide", SILENCE_DAYS["hide"])
                conn.execute("UPDATE rec_instances SET silenced_until=datetime('now', ?) WHERE rec_id=?",
                             (f"+{int(days)} days", rec_id))
            elif event in ("accepted", "completed"):
                conn.execute("UPDATE rec_instances SET silenced_until=MAX(COALESCE(silenced_until, ''), datetime('now', ?)) "
                             "WHERE rec_id=?", (f"+{int(silence_days or ACCEPTED_QUIET_DAYS)} days", rec_id))
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
    """Close open episodes nobody answered in `days` as expired — the
    'shown and ignored' outcome the acceptance rate needs in its
    denominator. Bounded: at most 5000 per call."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    n = 0
    try:
        rows = conn.execute("SELECT rec_id, restaurant_id, key FROM rec_instances WHERE status='open' AND last_event_at < ? "
                            "LIMIT 5000", (cutoff,)).fetchall()
        for r in rows:
            _close(conn, r["rec_id"], r["restaurant_id"], r["key"], "expired", meta={"reason": "no answer"})
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def sync_existing(db_path=DB_PATH, days=120) -> dict:
    """Carry answers the older ledgers hold into the trail, idempotently
    (source_ref per row): Home dismissals, outcome trackers and verdicts,
    issue resolutions, Ask confirms/dismissals, schedule recommendation
    answers, automatic actions. Scheduled nightly."""
    counts = {}

    def bump(k):
        counts[k] = counts.get(k, 0) + 1
    conn = get_conn(db_path)
    since = f"-{int(days)} days"
    try:
        try:
            home = conn.execute("SELECT restaurant_id, key, kind, dismissed_by, dismissed_at FROM home_dismissals "
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
            sched = conn.execute("SELECT id, restaurant_id, kind, key, action FROM schedule_recommendation_events "
                                 "WHERE action IN ('accepted','dismissed') AND created_at >= datetime('now', ?)",
                                 (since,)).fetchall()
        except Exception:
            sched = []
    finally:
        conn.close()
    for r in home:
        kind = r["kind"] if r["kind"] in ("done", "not_for_us") else "hide"
        ev = "completed" if kind == "done" else "dismissed"
        if record(r["restaurant_id"], r["key"], ev, surface="home", user_id=r["dismissed_by"],
                  meta={"kind": kind}, source_ref=f"home:{r['key']}:{r['dismissed_at']}", db_path=db_path):
            bump("home")
    for r in outs:
        if r["verdict"] in ("improved", "worsened", "no_clear_change"):
            if record(r["restaurant_id"], r["source_key"], "outcome", meta={"verdict": r["verdict"]},
                      source_ref=f"outcome:{r['id']}", db_path=db_path):
                bump("outcomes")
    for r in iss:
        if r["resolved_at"]:
            if record(r["restaurant_id"], r["source_key"], "completed", surface="issue_sms",
                      source_ref=f"issue:{r['id']}:resolved", db_path=db_path):
                bump("issues")
        elif r["acknowledged_at"]:
            if record(r["restaurant_id"], r["source_key"], "accepted", surface="issue_sms",
                      source_ref=f"issue:{r['id']}:ack", db_path=db_path):
                bump("issues")
    for r in asks:
        ev = {"confirmed": "accepted", "dismissed": "dismissed"}.get(r["outcome"])
        if ev:
            # Answers name the proposal they settle (ask:<proposal id>, the key
            # the card was shown under); an older client's answer does not.
            akey = rec_key("ask", r["proposal_id"]) if r["proposal_id"] else rec_key("ask", r["action"])
            if record(r["restaurant_id"], akey, ev, surface="ask",
                      meta={"kind": "hide"} if ev == "dismissed" else None,
                      source_ref=f"ask:{r['id']}", db_path=db_path):
                bump("ask")
    for r in sched:
        key = rec_key("schedule_" + (r["kind"] or "other"), (r["key"] or "")[:100])
        ev = "accepted" if r["action"] == "accepted" else "dismissed"
        if record(r["restaurant_id"], key, ev, surface="schedule_review",
                  meta={"kind": "not_for_us"} if ev == "dismissed" else None,
                  source_ref=f"sched:{r['id']}", db_path=db_path):
            bump("schedule")
    return counts
