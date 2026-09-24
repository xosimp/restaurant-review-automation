"""Feedback collection: every recommendation's life as events.

Every surface records its answers in rec_ledger (rec_instances /
rec_events); `sync()` reads that trail forward from a cursor, and still
reads the four older tables for what predates it — `home_dismissals`,
trackers in `recommendation_outcomes`, Ask's `ask_cavnar_actions` and the
queue's `delayed_actions` — deriving one event row per (restaurant,
recommendation, action) with nothing counted twice (see sync). New callers
may `record()` directly.

Actions: presented · done · not_for_us · hidden · snoozed · accepted ·
tracking · implemented · measured · confirmed · dismissed · auto · ignored.
A snooze ("Not today") is not a no; an expired episode is `ignored` — shown
and never answered, in the acceptance denominator (scoring).

`rec_kind` is the key's prefix ("trim_day", "cut_waste", "reprice",
"observed:schedule_published" …) — a category of recommendation, never the
text — so kinds can be compared across restaurants without carrying what
was said to whom.
"""
from datetime import date

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

ACTIONS = ("presented", "done", "not_for_us", "hidden", "tracking", "measured", "confirmed", "dismissed", "auto",
           "accepted", "implemented", "snoozed", "ignored")
OUTCOMES = ("improved", "worsened", "no_clear_change", "unknown")


def kind_of(source_key: str) -> str:
    key = str(source_key or "").strip()
    if key.startswith("observed:"):
        return "observed:" + key.split(":")[1] if key.count(":") >= 1 else "observed"
    return key.split(":", 1)[0] or "unknown"


def record(restaurant_id, rec_kind, source_key, action, outcome=None, days_to_effect=None,
           confidence_at=None, event_at=None, cohort=None, synced_from=None, db_path=DB_PATH) -> bool:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome}")
    key = str(source_key)[:200]
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, days_to_effect, "
            "confidence_at, event_at, synced_from) VALUES (?,?,?,?,?,?,?,?,COALESCE(?, datetime('now')),?) "
            "ON CONFLICT(restaurant_id, source_key, action) DO NOTHING",
            (restaurant_id, rec_kind, key, cohort, action, outcome, days_to_effect, confidence_at, event_at, synced_from))
        inserted = cur.rowcount > 0
        if not inserted and (outcome is not None or days_to_effect is not None or cohort is not None):
            # An existing event only ever gains what it lacked — a verdict that
            # arrived later, a cohort resolved later. Never counted as new.
            conn.execute("UPDATE intel_rec_events SET outcome=COALESCE(?, outcome), days_to_effect=COALESCE(?, days_to_effect), "
                         "cohort=COALESCE(?, cohort) WHERE restaurant_id=? AND source_key=? AND action=?",
                         (outcome, days_to_effect, cohort, restaurant_id, key, action))
        conn.commit()
        return inserted
    finally:
        conn.close()


LEDGER_CURSOR = "intelligence_feedback_ledger"
LEDGER_EVENTS_PER_PASS = 20000
# rec_ledger keys that are bookkeeping, not advice (rec_ledger.BOOKKEEPING_PREFIXES).
_BOOKKEEPING = ("restore_kind:", "calibration:", "standby:")


def _ask_key(proposal_id, action, summary) -> str:
    """The key an Ask answer is filed under: the ledger's own
    "ask:<proposal id>", the same identity Ask, the queue, decisions and
    rec_ledger use. An answer from a client too old to name its proposal
    keeps the legacy "ask:<action>:<summary>"."""
    if proposal_id:
        return f"ask:{proposal_id}"
    return f"ask:{action}:{(summary or '')[:40]}"


def _ledger_action(key, event, meta):
    """The engine's action for one rec_ledger answer, or None when the
    event is not an answer the engine counts."""
    if event == "dismissed":
        return "not_for_us" if meta.get("kind") == "not_for_us" else "hidden"
    if event == "completed":
        return "done"
    if event == "accepted":
        return "tracking" if (meta.get("tracker_id") or meta.get("tracking")) else "accepted"
    if event == "implemented":
        return "implemented"
    if event == "snoozed":
        return "snoozed"
    if event == "expired":
        return "ignored"
    return None


def _cursor_get(conn):
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (LEDGER_CURSOR,)).fetchone()
        return int(row["value"]) if row and row["value"] else 0
    except Exception:
        return 0


def _cursor_set(conn, value):
    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                 (LEDGER_CURSOR, str(int(value))))


def _repair(conn, dis, asks):
    """Two derived-row corrections, idempotent, run before each sync:

    * a Home "Not today" (home_dismissals kind 'snooze') was learned as
      `hidden` — a deliberate no. Where the key has no real hide, that row
      becomes `snoozed` (a duplicate `snoozed` from the ledger wins).
    * an Ask answer was keyed "ask:<action>:<summary>" while every other
      reader keys it "ask:<proposal id>". Rows whose answer names its
      proposal move to the proposal's key, so the ledger's copy of the
      same answer cannot count it twice."""
    hides = {(r["restaurant_id"], r["key"]) for r in dis if r["kind"] not in ("snooze", "done", "not_for_us")}
    for rid, key in {(r["restaurant_id"], r["key"]) for r in dis if r["kind"] == "snooze"} - hides:
        conn.execute("UPDATE OR IGNORE intel_rec_events SET action='snoozed' WHERE restaurant_id=? AND source_key=? "
                     "AND action='hidden' AND synced_from='home_dismissals'", (rid, str(key)[:200]))
        conn.execute("DELETE FROM intel_rec_events WHERE restaurant_id=? AND source_key=? AND action='hidden' "
                     "AND synced_from='home_dismissals'", (rid, str(key)[:200]))
    for r in asks:
        if not r["proposal_id"]:
            continue
        legacy, new = _ask_key(None, r["action"], r["summary"]), _ask_key(r["proposal_id"], r["action"], r["summary"])
        conn.execute("UPDATE OR IGNORE intel_rec_events SET source_key=? WHERE restaurant_id=? AND source_key=? "
                     "AND synced_from='ask_cavnar_actions'", (new, r["restaurant_id"], legacy))
        conn.execute("DELETE FROM intel_rec_events WHERE restaurant_id=? AND source_key=? "
                     "AND synced_from='ask_cavnar_actions'", (r["restaurant_id"], legacy))
    conn.commit()


def sync(db_path=DB_PATH, cohorts: dict = None) -> dict:
    """Derive events from the tables that hold the answers.

    rec_ledger (rec_events) is the input for every surface — Home, the
    brief, Reviews, Food, Marketing, Intel, the DSR, the schedule, the
    queue, alerts and issues — read forward from a cursor in job_cursors,
    bounded per pass. The four older tables are still read for what
    predates the ledger: home_dismissals, recommendation_outcomes (the
    source of every measured verdict and its days to effect),
    ask_cavnar_actions (every Ask answer, under the ledger's own
    "ask:<proposal id>") and delayed_actions.

    Never counted twice: an answer reaches the same (restaurant, key,
    action) row from both paths — a Home "not for us" is `not_for_us` from
    home_dismissals and from the ledger; Track is `tracking` from the
    tracker and from the ledger's accept that names it — and UNIQUE
    (restaurant, key, action) keeps one. The ledger's Ask keys and outcome
    events are skipped (ask_cavnar_actions and recommendation_outcomes are
    their sources), and `presented` is not derived (no rate reads it).

    Ledger mapping: dismissed → not_for_us | hidden; completed → done;
    accepted → tracking (a tracker named) | accepted; implemented →
    implemented; snoozed → snoozed (a "Not today" is not a no); expired →
    ignored. `cohorts` is {restaurant_id: cohort} so events carry their
    cohort. Idempotent."""
    import json as _json
    cohorts = cohorts or {}
    written = 0
    conn = get_conn(db_path)
    try:
        dis = conn.execute("SELECT restaurant_id, key, kind, dismissed_at FROM home_dismissals").fetchall()
        outs = conn.execute("SELECT restaurant_id, source, source_key, status, verdict, started_on, evaluate_on, created_at "
                            "FROM recommendation_outcomes").fetchall()
        asks = conn.execute("SELECT restaurant_id, action, summary, outcome, created_at, proposal_id FROM ask_cavnar_actions "
                            "WHERE outcome IN ('confirmed','dismissed')").fetchall()
        auto = conn.execute("SELECT restaurant_id, kind, id, status, executed_at, created_at FROM delayed_actions "
                            "WHERE status IN ('done','executed','cancelled','canceled')").fetchall()
        _repair(conn, dis, asks)
        start = _cursor_get(conn)
        try:
            ledger = conn.execute(
                "SELECT id, restaurant_id, key, event, meta, at FROM rec_events WHERE id > ? AND event IN "
                "('accepted','completed','dismissed','snoozed','implemented','expired') ORDER BY id LIMIT ?",
                (start, LEDGER_EVENTS_PER_PASS)).fetchall()
        except Exception:            # a database from before the ledger
            ledger = []
    finally:
        conn.close()
    for r in dis:
        action = {"done": "done", "not_for_us": "not_for_us", "snooze": "snoozed"}.get(r["kind"], "hidden")
        written += record(r["restaurant_id"], kind_of(r["key"]), r["key"], action, event_at=r["dismissed_at"],
                          cohort=cohorts.get(r["restaurant_id"]), synced_from="home_dismissals", db_path=db_path)
    for r in outs:
        # The kind comes from the key, not the tracker's source: Home's
        # "Done" starts an `observed` tracker under the recommendation's own
        # key ("trim_day:Monday"), whose result belongs to trim_day — not to
        # a meaningless "observed:Monday". kind_of already gives a genuine
        # observed:<action>:<month> key its observed: kind.
        kind = kind_of(r["source_key"])
        written += record(r["restaurant_id"], kind, r["source_key"], "tracking", event_at=r["created_at"],
                          cohort=cohorts.get(r["restaurant_id"]), synced_from="recommendation_outcomes", db_path=db_path)
        if r["status"] == "evaluated":
            days = None
            try:
                days = (date.fromisoformat(str(r["evaluate_on"])[:10]) - date.fromisoformat(str(r["started_on"])[:10])).days
            except (TypeError, ValueError):
                pass
            outcome = r["verdict"] if r["verdict"] in OUTCOMES else "unknown"
            written += record(r["restaurant_id"], kind, r["source_key"], "measured", outcome=outcome, days_to_effect=days,
                              event_at=r["evaluate_on"], cohort=cohorts.get(r["restaurant_id"]),
                              synced_from="recommendation_outcomes", db_path=db_path)
    for r in asks:
        key = _ask_key(r["proposal_id"], r["action"], r["summary"])
        written += record(r["restaurant_id"], f"ask:{r['action']}", key, r["outcome"], event_at=r["created_at"],
                          cohort=cohorts.get(r["restaurant_id"]), synced_from="ask_cavnar_actions", db_path=db_path)
    for r in auto:
        if r["status"] in ("done", "executed"):
            written += record(r["restaurant_id"], f"auto:{r['kind']}", f"auto:{r['kind']}:{r['id']}", "auto",
                              event_at=r["executed_at"] or r["created_at"], cohort=cohorts.get(r["restaurant_id"]),
                              synced_from="delayed_actions", db_path=db_path)
        else:
            written += record(r["restaurant_id"], f"auto:{r['kind']}", f"auto:{r['kind']}:{r['id']}", "dismissed",
                              event_at=r["executed_at"] or r["created_at"], cohort=cohorts.get(r["restaurant_id"]),
                              synced_from="delayed_actions", db_path=db_path)
    from_ledger = 0
    last = start
    for r in ledger:
        last = max(last, int(r["id"]))
        key = str(r["key"] or "")
        if not key or key.startswith(_BOOKKEEPING) or key.startswith("ask:"):
            continue
        try:
            meta = _json.loads(r["meta"] or "{}") or {}
        except (TypeError, ValueError):
            meta = {}
        action = _ledger_action(key, r["event"], meta)
        if not action:
            continue
        n = record(r["restaurant_id"], kind_of(key), key, action, event_at=r["at"],
                   cohort=cohorts.get(r["restaurant_id"]), synced_from="rec_ledger", db_path=db_path)
        written += n
        from_ledger += n
    if ledger:
        conn = get_conn(db_path)
        try:
            _cursor_set(conn, last)
            conn.commit()
        finally:
            conn.close()
    return {"events": written, "from_ledger": from_ledger, "ledger_cursor": last}


def history(restaurant_id, limit=100, db_path=DB_PATH) -> list:
    """This restaurant's own recommendation events, newest first (Level 1)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT rec_kind, source_key, action, outcome, days_to_effect, confidence_at, event_at "
                            "FROM intel_rec_events WHERE restaurant_id=? ORDER BY event_at DESC LIMIT ?",
                            (restaurant_id, int(limit))).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
