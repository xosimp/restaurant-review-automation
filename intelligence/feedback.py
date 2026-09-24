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


def _record_on(conn, restaurant_id, rec_kind, source_key, action, outcome=None, days_to_effect=None,
               confidence_at=None, event_at=None, cohort=None, synced_from=None) -> bool:
    """record() on the caller's connection, uncommitted — sync() writes a
    whole pass on one connection (re-audit B22)."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome}")
    key = str(source_key)[:200]
    cur = conn.execute(
        "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, days_to_effect, "
        "confidence_at, event_at, synced_from) VALUES (?,?,?,?,?,?,?,?,COALESCE(?, datetime('now')),?) "
        "ON CONFLICT(restaurant_id, source_key, action) DO NOTHING",
        (restaurant_id, rec_kind, key, cohort, action, outcome, days_to_effect, confidence_at, event_at, synced_from))
    inserted = cur.rowcount > 0
    if not inserted and (outcome is not None or days_to_effect is not None or cohort is not None):
        # An existing event takes what it lacked (a cohort resolved later)
        # and a verdict that changed since (a re-check, the owner's
        # check-in). Never counted as new.
        conn.execute("UPDATE intel_rec_events SET outcome=COALESCE(?, outcome), days_to_effect=COALESCE(?, days_to_effect), "
                     "cohort=COALESCE(?, cohort) WHERE restaurant_id=? AND source_key=? AND action=? "
                     "AND (outcome IS NOT COALESCE(?, outcome) OR days_to_effect IS NOT COALESCE(?, days_to_effect) "
                     "     OR cohort IS NOT COALESCE(?, cohort))",
                     (outcome, days_to_effect, cohort, restaurant_id, key, action, outcome, days_to_effect, cohort))
    return inserted


def record(restaurant_id, rec_kind, source_key, action, outcome=None, days_to_effect=None,
           confidence_at=None, event_at=None, cohort=None, synced_from=None, db_path=DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        inserted = _record_on(conn, restaurant_id, rec_kind, source_key, action, outcome=outcome,
                              days_to_effect=days_to_effect, confidence_at=confidence_at, event_at=event_at,
                              cohort=cohort, synced_from=synced_from)
        conn.commit()
        return inserted
    finally:
        conn.close()


LEDGER_CURSOR = "intelligence_feedback_ledger"
LEDGER_EVENTS_PER_PASS = 20000
# The append-only older tables are read forward from their own cursors too
# (re-audit B22): home_dismissals (a new answer replaces its row, so it
# takes a new id), ask_cavnar_actions (append-only) and delayed_actions (a
# low-water mark: the cursor never passes a row still pending).
HOME_CURSOR = "intelligence_feedback_home"
ASK_CURSOR = "intelligence_feedback_ask"
AUTO_CURSOR = "intelligence_feedback_auto"
LEGACY_ROWS_PER_PASS = 20000
REPAIR_MARK = "intelligence_feedback_repair:v2"
# Measured results are filed one per EPISODE (re-audit B2 #3, probe p10):
# the unique (restaurant, key, action) row used to hold the latest of every
# tracker a key ever had, overwritten in place — improved, then worsened,
# then improved again read as one result flipping. Each tracker's result is
# now its own row, keyed "<source_key>#o<tracker id>", and the rows written
# the old way are removed once (EPISODE_REPAIR_MARK); the next pass derives
# them again from recommendation_outcomes, which it reads whole.
MEASURED_KEY_SEP = "#o"
EPISODE_REPAIR_MARK = "intelligence_feedback_repair:episodes_v1"


def measured_key(source_key, tracker_id) -> str:
    """The intel_rec_events key of one tracker's measured result."""
    return f"{str(source_key or '')[:180]}{MEASURED_KEY_SEP}{int(tracker_id)}"


def _repair_episode_keys(conn):
    """Once (EPISODE_REPAIR_MARK): drop the measured rows written under the
    bare recommendation key — derived rows, re-derived per episode by the
    same pass. Returns True when it ran."""
    try:
        if conn.execute("SELECT 1 FROM job_cursors WHERE key=?", (EPISODE_REPAIR_MARK,)).fetchone():
            return False
    except Exception:
        return False
    conn.execute("DELETE FROM intel_rec_events WHERE action='measured' "
                 "AND COALESCE(synced_from, 'recommendation_outcomes')='recommendation_outcomes' "
                 "AND instr(source_key, ?) = 0", (MEASURED_KEY_SEP,))
    _cursor_set(conn, 1, key=EPISODE_REPAIR_MARK)
    return True


def _counted_tracker_ids(rows) -> set:
    """The evaluated trackers whose result counts in the cohort record — the
    own record's rule (rec_learning._one_per_window): one result per
    tracker, and on one number (restaurant, metric) one per non-overlapping
    after-window, the earliest kept. Only a clear learned verdict holds a
    window (an unknown result is no change to count once); a row without its
    window is kept."""
    kept, by = set(), {}
    for r in rows:
        if r.get("status") != "evaluated" or r.get("id") is None:
            continue
        if _outcome_of(r) not in ("improved", "worsened", "no_clear_change"):
            kept.add(r["id"])            # filed as its own (unknown) verdict
            continue
        start = str(r.get("after_start") or r.get("started_on") or "")[:10]
        end = str(r.get("after_end") or r.get("evaluate_on") or start)[:10]
        if not r.get("metric") or not start:
            kept.add(r["id"])
            continue
        by.setdefault((r["restaurant_id"], r["metric"]), []).append((start, end, r["id"]))
    for items in by.values():
        last_end = ""
        for start, end, tid in sorted(items):
            if last_end and start <= last_end:
                continue
            kept.add(tid)
            last_end = max(last_end, end)
    return kept
# rec_ledger keys that are bookkeeping, not advice (rec_ledger.BOOKKEEPING_PREFIXES).
_BOOKKEEPING = ("restore_kind:", "calibration:", "standby:")
_AUTO_DONE = ("done", "executed")
_AUTO_OFF = ("cancelled", "canceled")


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


def _cursor_get(conn, key=LEDGER_CURSOR):
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row and row["value"] else 0
    except Exception:
        return 0


def _cursor_set(conn, value, key=LEDGER_CURSOR):
    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                 (key, str(int(value))))


def _repair(conn, dis, asks):
    """Two derived-row corrections, run ONCE (REPAIR_MARK; re-audit B22):

    * a Home "Not today" (home_dismissals kind 'snooze') was learned as
      `hidden` — a deliberate no — by the old sync, which wrote it with the
      snooze row's own dismissed_at. Only a `hidden` row carrying THAT time
      becomes `snoozed` (a duplicate `snoozed` wins): a real, earlier hide
      of the same key — whose row a later "Not today" replaced — is an
      answer and stays one (re-audit B10: it used to be erased).
    * an Ask answer was keyed "ask:<action>:<summary>" while every other
      reader keys it "ask:<proposal id>". Rows whose answer names its
      proposal move to the proposal's key, so the ledger's copy of the
      same answer cannot count it twice."""
    for r in dis:
        if r["kind"] != "snooze":
            continue
        rid, key, at = r["restaurant_id"], str(r["key"])[:200], r["dismissed_at"]
        conn.execute("UPDATE OR IGNORE intel_rec_events SET action='snoozed' WHERE restaurant_id=? AND source_key=? "
                     "AND action='hidden' AND synced_from='home_dismissals' AND event_at=?", (rid, key, at))
        conn.execute("DELETE FROM intel_rec_events WHERE restaurant_id=? AND source_key=? AND action='hidden' "
                     "AND synced_from='home_dismissals' AND event_at=?", (rid, key, at))
    for r in asks:
        if not r["proposal_id"]:
            continue
        legacy, new = _ask_key(None, r["action"], r["summary"]), _ask_key(r["proposal_id"], r["action"], r["summary"])
        conn.execute("UPDATE OR IGNORE intel_rec_events SET source_key=? WHERE restaurant_id=? AND source_key=? "
                     "AND synced_from='ask_cavnar_actions'", (new, r["restaurant_id"], legacy))
        conn.execute("DELETE FROM intel_rec_events WHERE restaurant_id=? AND source_key=? "
                     "AND synced_from='ask_cavnar_actions'", (r["restaurant_id"], legacy))


def _repair_once(conn):
    try:
        if conn.execute("SELECT 1 FROM job_cursors WHERE key=?", (REPAIR_MARK,)).fetchone():
            return False
    except Exception:
        return False                      # no job_cursors: nothing to mark, nothing repaired
    dis = conn.execute("SELECT restaurant_id, key, kind, dismissed_at FROM home_dismissals WHERE kind='snooze'").fetchall()
    asks = conn.execute("SELECT restaurant_id, action, summary, proposal_id FROM ask_cavnar_actions "
                        "WHERE outcome IN ('confirmed','dismissed') AND proposal_id IS NOT NULL").fetchall()
    _repair(conn, dis, asks)
    _cursor_set(conn, 1, key=REPAIR_MARK)
    return True


def _outcome_of(r):
    """The engine's verdict for one tracker row — rec_learning.learned_verdict,
    the one mapping both readers share (re-audit B9): the owner saying they
    did not make the change, or that something else changed, is `unknown`;
    a move that faded or reversed at its re-check is not a win."""
    import rec_learning
    return rec_learning.learned_verdict(r["verdict"], dict(r))


def sync(db_path=DB_PATH, cohorts: dict = None) -> dict:
    """Derive events from the tables that hold the answers.

    rec_ledger (rec_events) is the input for every surface — Home, the
    brief, Reviews, Food, Marketing, Intel, the DSR, the schedule, the
    queue, alerts and issues — read forward from a cursor in job_cursors,
    bounded per pass. Only an answer to an episode some surface SHOWED is
    counted (re-audit B20): an episode an answer opened, with nothing shown
    behind it, is not a recommendation the owner took or declined.

    The four older tables are still read for what predates the ledger:
    home_dismissals, ask_cavnar_actions and delayed_actions forward from
    their own cursors; recommendation_outcomes (the source of every
    measured verdict and its days to effect) whole, because its rows change
    after they are written — a tracker is evaluated, re-checked, checked in
    on — and each verdict is read through rec_learning.learned_verdict
    (re-audit B9). Ask answers are filed under the ledger's own
    "ask:<proposal id>".

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
    cohort. The whole pass is written on one connection and committed once
    (re-audit B22). Idempotent."""
    import json as _json
    cohorts = cohorts or {}
    written = 0
    from_ledger = 0
    conn = get_conn(db_path)
    try:
        _repair_once(conn)
        _repair_episode_keys(conn)

        def put(rid, kind, key, action, **kw):
            return _record_on(conn, rid, kind, key, action, cohort=cohorts.get(rid), **kw)

        h0 = _cursor_get(conn, HOME_CURSOR)
        dis = conn.execute("SELECT id, restaurant_id, key, kind, dismissed_at FROM home_dismissals WHERE id > ? "
                           "ORDER BY id LIMIT ?", (h0, LEGACY_ROWS_PER_PASS)).fetchall()
        for r in dis:
            action = {"done": "done", "not_for_us": "not_for_us", "snooze": "snoozed"}.get(r["kind"], "hidden")
            written += put(r["restaurant_id"], kind_of(r["key"]), r["key"], action, event_at=r["dismissed_at"],
                           synced_from="home_dismissals")
        if dis:
            _cursor_set(conn, max(int(r["id"]) for r in dis), key=HOME_CURSOR)

        try:
            # baseline_overlaps_trigger: learned_verdict never reads a result
            # measured against its own trigger window as a win (CA2 #1);
            # concurrent: nor one confounded by another change (B2 #5); id,
            # metric and the after-window: one result per episode and window.
            outs = conn.execute("SELECT id, restaurant_id, source, source_key, status, verdict, started_on, "
                                "evaluate_on, created_at, recheck_verdict, owner_checkin, baseline_overlaps_trigger, "
                                "concurrent, metric, after_start, after_end FROM recommendation_outcomes").fetchall()
        except Exception:
            outs = None
        if outs is None:
            try:
                outs = conn.execute("SELECT restaurant_id, source, source_key, status, verdict, started_on, "
                                    "evaluate_on, created_at, recheck_verdict, owner_checkin, "
                                    "baseline_overlaps_trigger FROM recommendation_outcomes").fetchall()
            except Exception:
                outs = None
        if outs is None:
            try:
                outs = conn.execute("SELECT restaurant_id, source, source_key, status, verdict, started_on, "
                                    "evaluate_on, created_at, recheck_verdict, owner_checkin "
                                    "FROM recommendation_outcomes").fetchall()
            except Exception:
                outs = None
        if outs is None:                 # a database from before the re-check / check-in columns
            outs = conn.execute("SELECT restaurant_id, source, source_key, status, verdict, started_on, evaluate_on, "
                                "created_at FROM recommendation_outcomes").fetchall()
        outs = [dict(r) for r in outs]
        counted = _counted_tracker_ids([r for r in outs if not str(r.get("source_key") or "")
                                        .startswith("observed:untaken:")])
        for r in outs:
            # The kind comes from the key, not the tracker's source: Home's
            # "Done" starts an `observed` tracker under the recommendation's
            # own key ("trim_day:Monday"), whose result belongs to trim_day —
            # not to a meaningless "observed:Monday". kind_of already gives a
            # genuine observed:<action>:<month> key its observed: kind.
            if str(r["source_key"] or "").startswith("observed:untaken:"):
                continue     # advice NOT taken (outcomes.observe_untaken): a comparison, never an answer
            kind = kind_of(r["source_key"])
            written += put(r["restaurant_id"], kind, r["source_key"], "tracking", event_at=r["created_at"],
                           synced_from="recommendation_outcomes")
            if r["status"] == "evaluated":
                days = None
                try:
                    days = (date.fromisoformat(str(r["evaluate_on"])[:10])
                            - date.fromisoformat(str(r["started_on"])[:10])).days
                except (TypeError, ValueError):
                    pass
                # One row per episode; a result whose after-window overlaps
                # an earlier one on the same number is filed `unknown` —
                # never a second result for one change.
                if r.get("id") is None:          # a database from before the id was read
                    mkey, outcome = r["source_key"], _outcome_of(r)
                else:
                    mkey = measured_key(r["source_key"], r["id"])
                    outcome = _outcome_of(r) if r["id"] in counted else "unknown"
                written += put(r["restaurant_id"], kind, mkey, "measured", outcome=outcome,
                               days_to_effect=days, event_at=r["evaluate_on"], synced_from="recommendation_outcomes")

        a0 = _cursor_get(conn, ASK_CURSOR)
        asks = conn.execute("SELECT id, restaurant_id, action, summary, outcome, created_at, proposal_id "
                            "FROM ask_cavnar_actions WHERE id > ? ORDER BY id LIMIT ?",
                            (a0, LEGACY_ROWS_PER_PASS)).fetchall()
        for r in asks:
            if r["outcome"] not in ("confirmed", "dismissed"):
                continue
            key = _ask_key(r["proposal_id"], r["action"], r["summary"])
            written += put(r["restaurant_id"], f"ask:{r['action']}", key, r["outcome"], event_at=r["created_at"],
                           synced_from="ask_cavnar_actions")
        if asks:
            _cursor_set(conn, max(int(r["id"]) for r in asks), key=ASK_CURSOR)

        d0 = _cursor_get(conn, AUTO_CURSOR)
        auto = conn.execute("SELECT id, restaurant_id, kind, status, executed_at, created_at FROM delayed_actions "
                            "WHERE id > ? ORDER BY id LIMIT ?", (d0, LEGACY_ROWS_PER_PASS)).fetchall()
        low = None                       # the first row still pending: the cursor stops before it
        for r in auto:
            if r["status"] in _AUTO_DONE or r["status"] in _AUTO_OFF:
                written += put(r["restaurant_id"], f"auto:{r['kind']}", f"auto:{r['kind']}:{r['id']}",
                               "auto" if r["status"] in _AUTO_DONE else "dismissed",
                               event_at=r["executed_at"] or r["created_at"], synced_from="delayed_actions")
            elif r["status"] == "pending" and low is None:
                low = int(r["id"])
        if auto:
            _cursor_set(conn, (low - 1) if low is not None else max(int(r["id"]) for r in auto), key=AUTO_CURSOR)

        start = _cursor_get(conn)
        try:
            ledger = conn.execute(
                "SELECT e.id, e.restaurant_id, e.key, e.event, e.meta, e.at, "
                "EXISTS (SELECT 1 FROM rec_events s WHERE s.rec_id=e.rec_id AND s.event='shown') AS shown "
                "FROM rec_events e WHERE e.id > ? AND e.event IN "
                "('accepted','completed','dismissed','snoozed','implemented','expired') ORDER BY e.id LIMIT ?",
                (start, LEDGER_EVENTS_PER_PASS)).fetchall()
        except Exception:            # a database from before the ledger
            ledger = []
        last = start
        for r in ledger:
            last = max(last, int(r["id"]))
            key = str(r["key"] or "")
            if not key or key.startswith(_BOOKKEEPING) or key.startswith("ask:") or not r["shown"]:
                continue
            try:
                meta = _json.loads(r["meta"] or "{}") or {}
            except (TypeError, ValueError):
                meta = {}
            action = _ledger_action(key, r["event"], meta)
            if not action:
                continue
            n = put(r["restaurant_id"], kind_of(key), key, action, event_at=r["at"], synced_from="rec_ledger")
            written += n
            from_ledger += n
        if ledger:
            _cursor_set(conn, last)
        # The confidence the owner was shown with it (confidence audit, K3):
        # each event takes its episode's snapshot — the latest episode of
        # the key started on or before the event — as a 0-1 figure, so
        # intel_confidence_log.mean_confidence stops being NULL by
        # construction. Filled once; an episode shown with no confidence
        # leaves it NULL (nothing was said).
        try:
            conn.execute(
                "UPDATE intel_rec_events SET confidence_at = ("
                "  SELECT i.confidence_pct / 100.0 FROM rec_instances i"
                "  WHERE i.restaurant_id = intel_rec_events.restaurant_id AND i.key = intel_rec_events.source_key"
                "    AND i.confidence_pct IS NOT NULL"
                "    AND substr(i.created_at, 1, 10) <= substr(COALESCE(intel_rec_events.event_at, '9999-12-31'), 1, 10)"
                "  ORDER BY i.created_at DESC LIMIT 1) "
                "WHERE confidence_at IS NULL AND EXISTS ("
                "  SELECT 1 FROM rec_instances i WHERE i.restaurant_id = intel_rec_events.restaurant_id"
                "    AND i.key = intel_rec_events.source_key AND i.confidence_pct IS NOT NULL"
                "    AND substr(i.created_at, 1, 10) <= substr(COALESCE(intel_rec_events.event_at, '9999-12-31'), 1, 10))")
        except Exception as e:           # a database from before the snapshot columns
            print(f"[intelligence.feedback] confidence_at not filled: {e}")
        # Rows read on an earlier pass are not read again, but the cohort
        # they belong to is today's (a category set or changed since) — one
        # statement per restaurant, touching only rows that differ.
        for rid, cohort in cohorts.items():
            if cohort:
                conn.execute("UPDATE intel_rec_events SET cohort=? WHERE restaurant_id=? AND cohort IS NOT ?",
                             (cohort, rid, cohort))
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
