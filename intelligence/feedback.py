"""Feedback collection: every recommendation's life as events.

Every surface records its answers in rec_ledger (rec_instances /
rec_events); `sync()` reads that trail forward from a cursor, and still
reads the four older tables for what predates it — `home_dismissals`,
trackers in `recommendation_outcomes`, Ask's `ask_cavnar_actions` and the
queue's `delayed_actions` — deriving one event row per (restaurant,
recommendation EPISODE, action) with nothing counted twice (see sync). New
callers may `record()` directly.

Actions: presented · done · not_for_us · hidden · snoozed · accepted ·
tracking · implemented · measured · confirmed · dismissed · auto · ignored.
A snooze ("Not today") is not a no; an expired episode is `ignored` — shown
and never answered, in the acceptance denominator (scoring).

`rec_kind` is the key's prefix ("trim_day", "cut_waste", "reprice",
"observed:schedule_published" …) — a category of recommendation, never the
text — so kinds can be compared across restaurants without carrying what
was said to whom.

One row per EPISODE (memory audit 9/29/26, PLATFORM-4): an answer is filed
under "<key>#e<rec_id>" (episode_key), as a measured result already was
under "<key>#o<tracker id>" (measured_key), each with its own event_at. The
unique (restaurant, key, action) row used to keep only the FIRST answer a
key ever had: a restaurant that declined trim_day:Monday in nine episodes
and took it once was one "taken", and a key first hidden 14 months ago and
again last week fell out of the 365-day prior altogether — while the
restaurant's own model counted per episode. An answer with no episode
behind it (older than the ledger) keeps the bare key.

Labels are re-stamped whole whenever a restaurant's change (PLATFORM-15):
`cohort` (the owner-confirmed concept, NULL included — a guess never joins
a group), `partition_key` (the confirmed partition of the row's metric
family, the prior ladder's second rung) and `google_data` (a review-derived
row of a Google-connected restaurant, never pooled — intelligence.
provenance), for every restaurant including seeded and excluded ones.

Bounded (PLATFORM-18): trackers are read forward from a (changed_at, id)
cursor — models.ensure_tracker_change_stamps keeps the stamp — so a night
re-reads only the trackers that changed (and the others on the same number,
for the one-result-per-window rule), never every tracker ever written.
"""
import json
import re
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

# Row keys: one per episode (answers) and one per tracker (results).
EPISODE_KEY_SEP = "#e"
MEASURED_KEY_SEP = "#o"


# Only a suffix of exactly that shape is a row suffix: a dish named
# "Burger #one" keeps its "#o".
_ROW_SUFFIX_RE = re.compile(r"(?:#e[0-9a-f]{32}|#o\d+)$")


def base_key(source_key) -> str:
    """The recommendation key a row belongs to, without its episode
    ("#e<rec_id>") or tracker ("#o<id>") suffix."""
    return _ROW_SUFFIX_RE.sub("", str(source_key or "").strip())


def kind_of(source_key: str) -> str:
    key = base_key(source_key)
    if key.startswith("observed:"):
        return "observed:" + key.split(":")[1] if key.count(":") >= 1 else "observed"
    return key.split(":", 1)[0] or "unknown"


def episode_key(source_key, rec_id) -> str:
    """The intel_rec_events key of one episode's answer."""
    return f"{str(source_key or '')[:160]}{EPISODE_KEY_SEP}{rec_id}"


def _record_on(conn, restaurant_id, rec_kind, source_key, action, outcome=None, days_to_effect=None,
               confidence_at=None, event_at=None, cohort=None, synced_from=None, rec_id=None, trust_version=None,
               metric=None, partition_key=None, google_data=None) -> bool:
    """record() on the caller's connection, uncommitted — sync() writes a
    whole pass on one connection (re-audit B22). `review_derived` is judged
    here (intelligence.provenance) from the key, the kind and a measured
    result's metric; `google_data` is the caller's (the restaurant is
    Google-connected) AND that."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome}")
    from . import provenance
    key = str(source_key)[:200]
    rd = 1 if provenance.review_derived(key, kind=rec_kind, metric=metric,
                                        sources=_episode_sources(conn, restaurant_id, key, rec_id)) else 0
    gd = 1 if (rd and google_data) else 0
    cur = conn.execute(
        "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, action, outcome, days_to_effect, "
        "confidence_at, event_at, synced_from, rec_id, trust_version, partition_key, review_derived, google_data) "
        "VALUES (?,?,?,?,?,?,?,?,COALESCE(?, datetime('now')),?,?,?,?,?,?) "
        "ON CONFLICT(restaurant_id, source_key, action) DO NOTHING",
        (restaurant_id, rec_kind, key, cohort, action, outcome, days_to_effect, confidence_at, event_at, synced_from,
         rec_id, trust_version, partition_key, rd, gd))
    inserted = cur.rowcount > 0
    if not inserted and (outcome is not None or days_to_effect is not None):
        # An existing event takes a verdict that changed since (a re-check,
        # the owner's check-in). Never counted as new. Its labels are the
        # restaurant's re-stamp's (_stamp_labels), never this row's.
        conn.execute("UPDATE intel_rec_events SET outcome=COALESCE(?, outcome), days_to_effect=COALESCE(?, days_to_effect), "
                     "review_derived=MAX(COALESCE(review_derived, 0), ?) "
                     "WHERE restaurant_id=? AND source_key=? AND action=? "
                     "AND (outcome IS NOT COALESCE(?, outcome) OR days_to_effect IS NOT COALESCE(?, days_to_effect) "
                     "     OR COALESCE(review_derived, 0) < ?)",
                     (outcome, days_to_effect, rd, restaurant_id, key, action, outcome, days_to_effect, rd))
    if not inserted and (confidence_at is not None or rec_id is not None):
        conn.execute("UPDATE intel_rec_events SET confidence_at=COALESCE(confidence_at, ?), "
                     "trust_version=COALESCE(trust_version, ?), rec_id=COALESCE(rec_id, ?) "
                     "WHERE restaurant_id=? AND source_key=? AND action=? AND "
                     "((confidence_at IS NULL AND ? IS NOT NULL) OR (rec_id IS NULL AND ? IS NOT NULL))",
                     (confidence_at, trust_version, rec_id, restaurant_id, key, action, confidence_at, rec_id))
    return inserted


def _episode_sources(conn, restaurant_id, key, rec_id=None):
    """The evidence sources an episode declared (rec_instances.
    evidence_sources): the row's own episode when it has one, else every
    episode of its key — a list of source names, [] when none is on file.
    What provenance.review_derived judges first (memory re-audit 9/29/26,
    PLATFORM-5). Never raises."""
    out = []
    try:
        if rec_id:
            rows = conn.execute("SELECT evidence_sources FROM rec_instances WHERE rec_id=?", (rec_id,)).fetchall()
        else:
            rows = conn.execute("SELECT evidence_sources FROM rec_instances WHERE restaurant_id=? AND key=? "
                                "AND evidence_sources IS NOT NULL", (restaurant_id, base_key(key))).fetchall()
        for r in rows:
            try:
                out += list(json.loads(r["evidence_sources"] or "[]") or [])
            except (TypeError, ValueError):
                continue
    except Exception:
        return []
    return out


def record(restaurant_id, rec_kind, source_key, action, outcome=None, days_to_effect=None,
           confidence_at=None, event_at=None, cohort=None, synced_from=None, db_path=DB_PATH, rec_id=None,
           metric=None) -> bool:
    """File one event for a caller that is not one of sync()'s sources. Pass
    `rec_id` for an answer to one episode (the row is keyed per episode). A
    review-derived row of a Google-connected restaurant is marked
    google_data here as it is by sync."""
    from . import scoring
    conn = get_conn(db_path)
    try:
        try:
            from .jobs import learning_labels
            lab = learning_labels(db_path=db_path, ids=[restaurant_id], conn=conn).get(int(restaurant_id)) or {}
        except Exception as e:
            print(f"[intelligence.feedback] labels unavailable for {restaurant_id}: {e}")
            lab = {}
        key = episode_key(source_key, rec_id) if rec_id and action != "measured" else source_key
        fam = scoring.kind_family(rec_kind)
        inserted = _record_on(conn, restaurant_id, rec_kind, key, action, outcome=outcome,
                              days_to_effect=days_to_effect, confidence_at=confidence_at, event_at=event_at,
                              cohort=cohort if cohort is not None else lab.get("cohort"), synced_from=synced_from,
                              rec_id=rec_id, metric=metric, partition_key=(lab.get("partitions") or {}).get(fam),
                              google_data=bool(lab.get("google")))
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
# Trackers, forward from "<changed_at>|<id>" (PLATFORM-18).
TRACKER_CURSOR = "intelligence_feedback_trackers"
TRACKERS_PER_PASS = 20000
# The last labels each restaurant's rows were stamped with (JSON map).
LABELS_MARK = "intelligence_feedback_labels"
REPAIR_MARK = "intelligence_feedback_repair:v2"
# Measured results are filed one per EPISODE (re-audit B2 #3, probe p10):
# the unique (restaurant, key, action) row used to hold the latest of every
# tracker a key ever had, overwritten in place — improved, then worsened,
# then improved again read as one result flipping. Each tracker's result is
# now its own row, keyed "<source_key>#o<tracker id>", and the rows written
# the old way are removed once (EPISODE_REPAIR_MARK); the next pass derives
# them again from recommendation_outcomes.
EPISODE_REPAIR_MARK = "intelligence_feedback_repair:episodes_v1"
# Answers per episode (PLATFORM-4): the bare-key answer rows are dropped once
# wherever the ledger still holds the answer, and re-derived per episode.
PER_EPISODE_REPAIR_MARK = "intelligence_feedback_repair:per_episode_v1"
# A derived row whose confidence snapshot was missing is looked for again
# only while it is this young: an episode's snapshot is written when it is
# first shown, so a row older than this can never gain one (PLATFORM-18).
CONFIDENCE_FILL_DAYS = 30
_ANSWER_EVENTS = ("accepted", "completed", "dismissed", "snoozed", "implemented", "expired")


def measured_key(source_key, tracker_id) -> str:
    """The intel_rec_events key of one tracker's measured result."""
    return f"{str(source_key or '')[:180]}{MEASURED_KEY_SEP}{int(tracker_id)}"


def _marked(conn, mark) -> bool | None:
    """True once `mark` is recorded, False before, None without job_cursors."""
    try:
        return bool(conn.execute("SELECT 1 FROM job_cursors WHERE key=?", (mark,)).fetchone())
    except Exception:
        return None


def _repair_episode_keys(conn):
    """Once (EPISODE_REPAIR_MARK): drop the measured rows written under the
    bare recommendation key — derived rows, re-derived per episode by the
    same pass. Returns True when it ran."""
    if _marked(conn, EPISODE_REPAIR_MARK) is not False:
        return False
    conn.execute("DELETE FROM intel_rec_events WHERE action='measured' "
                 "AND COALESCE(synced_from, 'recommendation_outcomes')='recommendation_outcomes' "
                 "AND instr(source_key, ?) = 0", (MEASURED_KEY_SEP,))
    _cursor_set(conn, 1, key=EPISODE_REPAIR_MARK)
    return True


def _repair_per_episode(conn):
    """Once (PER_EPISODE_REPAIR_MARK, memory audit PLATFORM-4): the answer
    rows filed under a bare key are re-derived per episode from the ledger,
    which holds every answer (rec_events, kept ops._RETENTION_DAYS):

      * a bare ledger row whose answers the ledger still holds is dropped,
        and the ledger cursor goes back to the start — the next passes
        derive every answer again, one row per episode, each with its own
        time. A bare row whose events were pruned stays (its only record).
      * a bare Home row (home_dismissals) the ledger also carries on a
        SHOWN episode — the same answer within rec_ledger.
        SAME_ANSWER_SECONDS — is dropped: the ledger's per-episode row is
        that answer (one on an episode nobody was shown stays: the ledger
        path never learns it, B20).
      * a bare `tracking` row is dropped and the tracker cursor starts over:
        trackers are re-derived under their episode's key.
      * trackers written before the change stamp get their creation time.
    Returns True when it ran."""
    if _marked(conn, PER_EPISODE_REPAIR_MARK) is not False:
        return False
    try:
        import rec_ledger
        span = int(rec_ledger.SAME_ANSWER_SECONDS)
    except Exception:
        span = 300
    marks = ",".join("?" for _ in _ANSWER_EVENTS)
    try:
        conn.execute(
            f"DELETE FROM intel_rec_events WHERE synced_from='rec_ledger' AND instr(source_key, ?) = 0 "
            f"AND EXISTS (SELECT 1 FROM rec_events e WHERE e.restaurant_id = intel_rec_events.restaurant_id "
            f"AND e.key = intel_rec_events.source_key AND e.event IN ({marks}))", (EPISODE_KEY_SEP, *_ANSWER_EVENTS))
        conn.execute(
            "DELETE FROM intel_rec_events WHERE synced_from='home_dismissals' AND instr(source_key, ?) = 0 "
            "AND EXISTS (SELECT 1 FROM rec_events e WHERE e.restaurant_id = intel_rec_events.restaurant_id "
            "AND e.key = intel_rec_events.source_key AND e.event IN ('completed','dismissed','snoozed') "
            "AND e.at BETWEEN datetime(intel_rec_events.event_at, ?) AND datetime(intel_rec_events.event_at, ?) "
            "AND EXISTS (SELECT 1 FROM rec_events s WHERE s.rec_id = e.rec_id AND s.event = 'shown'))",
            (EPISODE_KEY_SEP, f"-{span} seconds", f"+{span} seconds"))
    except Exception as e:           # a database from before the ledger
        print(f"[intelligence.feedback] per-episode repair skipped the ledger: {e}")
    conn.execute("DELETE FROM intel_rec_events WHERE synced_from='recommendation_outcomes' AND action='tracking' "
                 "AND instr(source_key, ?) = 0 AND instr(source_key, ?) = 0", (EPISODE_KEY_SEP, MEASURED_KEY_SEP))
    try:
        conn.execute("UPDATE recommendation_outcomes SET changed_at = COALESCE(created_at, datetime('now')) "
                     "WHERE changed_at IS NULL")
    except Exception:
        pass                          # no change stamp on this database: the pass reads every tracker
    _cursor_set(conn, 0)
    _cursor_set(conn, "", key=TRACKER_CURSOR)
    _cursor_set(conn, 1, key=PER_EPISODE_REPAIR_MARK)
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
_BOOKKEEPING = ("restore_kind:", "calibration:", "standby:", "conflict:")
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
    event is not an answer the engine counts. The owner's reason decides
    what a dismissal was (rec_ledger.REASON_EFFECT, memory audit 9/29/26):
    "already doing it" is done, "bad timing" a snooze — neither a no."""
    if event == "dismissed":
        try:
            import rec_ledger
            effect = rec_ledger.reason_effect(meta.get("reason_code"), meta.get("reason"))
        except Exception:
            effect = None
        if effect == "taken":
            return "done"
        if effect == "defer":
            return "snoozed"
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


def _cursor_get_str(conn, key):
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (key,)).fetchone()
        return str(row["value"] or "") if row else ""
    except Exception:
        return ""


def _cursor_set(conn, value, key=LEDGER_CURSOR):
    val = str(int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
                 (key, val))


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
    if _marked(conn, REPAIR_MARK) is not False:
        return False                      # done, or no job_cursors: nothing to mark, nothing repaired
    dis = conn.execute("SELECT restaurant_id, key, kind, dismissed_at FROM home_dismissals WHERE kind='snooze'").fetchall()
    asks = conn.execute("SELECT restaurant_id, action, summary, proposal_id FROM ask_cavnar_actions "
                        "WHERE outcome IN ('confirmed','dismissed') AND proposal_id IS NOT NULL").fetchall()
    _repair(conn, dis, asks)
    _cursor_set(conn, 1, key=REPAIR_MARK)
    return True


def effect_of(tracker) -> dict | None:
    """What one measured result MOVED (BM4-6, Top-50 #25), from its
    recommendation_outcomes row: {metric, effect_pct, effect_z,
    baseline_kind, after_end}, each effect signed so POSITIVE MEANS BETTER
    (metrics.describe's lower_is_better). effect_pct is the % change of the
    metric from its baseline (delta_pct, else delta ÷ baseline); effect_z is
    delta ÷ the restaurant's own noise sigma for one window. None when the
    metric is unknown. Pure — the caller decides whether the row counts."""
    t = tracker or {}
    metric = t.get("metric")
    if not metric:
        return None
    try:
        import metrics
        lower = bool(metrics.describe(metric)["lower_is_better"])
    except Exception:
        return None
    sign = -1.0 if lower else 1.0

    def _f(x):
        try:
            return None if x is None else float(x)
        except (TypeError, ValueError):
            return None
    delta, base, pct, sigma = _f(t.get("delta")), _f(t.get("baseline_value")), _f(t.get("delta_pct")), \
        _f(t.get("noise_sigma"))
    if pct is None and delta is not None and base:
        pct = delta / base * 100.0
    z = delta / sigma if (delta is not None and sigma and sigma > 0) else None
    return {"metric": str(metric)[:80], "effect_pct": round(sign * pct, 2) if pct is not None else None,
            "effect_z": round(sign * z, 3) if z is not None else None,
            "baseline_kind": t.get("baseline_kind"), "after_end": str(t.get("after_end") or "")[:10] or None}


_EFFECT_COLS = ("metric", "effect_pct", "effect_z", "baseline_kind", "after_end", "tags_json")


def _set_effect(conn, restaurant_id, key, eff, tags_json):
    """Write (or clear, eff None) one measured row's effect columns, only
    when they differ."""
    vals = [None] * 5 if eff is None else [eff["metric"], eff["effect_pct"], eff["effect_z"],
                                            eff["baseline_kind"], eff["after_end"]]
    vals.append(tags_json if eff is not None else None)
    sets = ", ".join(f"{c}=?" for c in _EFFECT_COLS)
    diff = " OR ".join(f"{c} IS NOT ?" for c in _EFFECT_COLS)
    conn.execute(f"UPDATE intel_rec_events SET {sets} WHERE restaurant_id=? AND source_key=? AND action='measured' "
                 f"AND ({diff})", (*vals, restaurant_id, str(key)[:200], *vals))


def _outcome_of(r):
    """The engine's verdict for one tracker row — rec_learning.learned_verdict,
    the one mapping both readers share (re-audit B9): the owner saying they
    did not make the change, or that something else changed, is `unknown`;
    a move that faded or reversed at its re-check is not a win."""
    import rec_learning
    return rec_learning.learned_verdict(r["verdict"], dict(r))


class _Episodes:
    """The episode an answer or a tracker belongs to, read per row from
    rec_instances' indexes and cached for the pass: the confidence the owner
    was shown with it (confidence_pct as 0–1, and its trust_version)."""

    def __init__(self, conn):
        self.conn = conn
        self._snap = {}
        self._keys = {}

    def snapshot(self, rec_id):
        if not rec_id:
            return None, None
        if rec_id not in self._snap:
            try:
                row = self.conn.execute("SELECT confidence_pct, trust_version FROM rec_instances WHERE rec_id=?",
                                        (rec_id,)).fetchone()
            except Exception:
                row = None
            pct = row["confidence_pct"] if row is not None else None
            self._snap[rec_id] = ((float(pct) / 100.0) if pct is not None else None,
                                  row["trust_version"] if row is not None else None)
        return self._snap[rec_id]

    def _one(self, sql, args):
        try:
            row = self.conn.execute(sql, args).fetchone()
        except Exception:
            return None
        return row["rec_id"] if row else None

    def key(self, rec_id):
        """The recommendation key of an episode (memory audit 9/29/26,
        link_trackers: a tracker's result is filed under the kind of the
        recommendation it measures)."""
        if not rec_id:
            return None
        if rec_id not in self._keys:
            try:
                row = self.conn.execute("SELECT key FROM rec_instances WHERE rec_id=?", (rec_id,)).fetchone()
            except Exception:
                row = None
            self._keys[rec_id] = row["key"] if row is not None else None
        return self._keys[rec_id]

    def for_tracker(self, tracker_id):
        if tracker_id is None:
            return None
        return self._one("SELECT rec_id FROM rec_instances WHERE tracker_id=? ORDER BY created_at DESC LIMIT 1",
                         (int(tracker_id),))

    def at(self, restaurant_id, key, at):
        """The episode current at `at` (rec_ledger._episode_at's rule)."""
        if not key or not at:
            return None
        return self._one("SELECT rec_id FROM rec_instances WHERE restaurant_id=? AND key=? AND created_at <= ? "
                         "ORDER BY created_at DESC, rowid DESC LIMIT 1", (restaurant_id, str(key)[:160], str(at)))


def _tracker_cols(conn):
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(recommendation_outcomes)").fetchall()}
    except Exception:
        return set()


_TRACKER_READ = ("id", "restaurant_id", "source", "source_key", "status", "verdict", "started_on", "evaluate_on",
                 "created_at", "recheck_verdict", "owner_checkin", "baseline_overlaps_trigger", "concurrent", "metric",
                 "after_start", "after_end", "baseline_value", "delta", "delta_pct", "noise_sigma", "baseline_kind",
                 "measures_key")


def _changed_trackers(conn, have):
    """(rows to derive, new cursor) — the trackers changed since the tracker
    cursor, plus every other tracker on the same (restaurant, metric), whose
    one-result-per-window standing a change can move. A database without the
    change stamp reads every tracker (the old pass)."""
    cols = ", ".join(c for c in _TRACKER_READ if c in have)
    if "changed_at" not in have:
        return [dict(r) for r in conn.execute(f"SELECT {cols} FROM recommendation_outcomes").fetchall()], None
    raw = _cursor_get_str(conn, TRACKER_CURSOR)
    at, _, last_id = raw.partition("|")
    try:
        last_id = int(last_id or 0)
    except ValueError:
        at, last_id = "", 0
    changed = [dict(r) for r in conn.execute(
        f"SELECT {cols}, changed_at FROM recommendation_outcomes WHERE changed_at > ? OR (changed_at = ? AND id > ?) "
        f"ORDER BY changed_at, id LIMIT ?", (at, at, last_id, TRACKERS_PER_PASS)).fetchall()]
    if not changed:
        return [], None
    new_cursor = f"{changed[-1]['changed_at']}|{changed[-1]['id']}"
    groups = {(r["restaurant_id"], r.get("metric")) for r in changed if r.get("metric")}
    by_id = {r["id"]: r for r in changed}
    for rid, metric in sorted(groups, key=lambda g: (g[0], str(g[1]))):
        for r in conn.execute(f"SELECT {cols} FROM recommendation_outcomes WHERE restaurant_id=? AND metric=?",
                              (rid, metric)).fetchall():
            by_id.setdefault(r["id"], dict(r))
    return list(by_id.values()), new_cursor


def sync(db_path=DB_PATH, cohorts: dict = None, labels: dict = None) -> dict:
    """Derive events from the tables that hold the answers.

    rec_ledger (rec_events) is the input for every surface — Home, the
    brief, Reviews, Food, Marketing, Intel, the DSR, the schedule, the
    queue, alerts and issues — read forward from a cursor in job_cursors,
    bounded per pass. Only an answer to an episode some surface SHOWED is
    counted (re-audit B20): an episode an answer opened, with nothing shown
    behind it, is not a recommendation the owner took or declined. Every
    answer is filed under its episode ("<key>#e<rec_id>", PLATFORM-4) with
    the time it was given and the confidence the episode was shown with.

    The four older tables are still read for what predates the ledger:
    home_dismissals, ask_cavnar_actions and delayed_actions forward from
    their own cursors; recommendation_outcomes (the source of every
    measured verdict and its days to effect) forward from its change stamp
    (PLATFORM-18), each verdict read through rec_learning.learned_verdict
    (re-audit B9). Ask answers are filed under the ledger's own
    "ask:<proposal id>".

    Never counted twice: a Home answer and a tracker are filed under the
    episode they belong to (the tracker's linked episode, else the one
    current when it started), so the ledger's copy of the same answer — a
    Home "not for us", a Track that names its tracker — reaches the same
    (restaurant, episode key, action) row and UNIQUE keeps one. The
    ledger's Ask keys and outcome events are skipped (ask_cavnar_actions
    and recommendation_outcomes are their sources), and `presented` is not
    derived (no rate reads it).

    Ledger mapping: dismissed → not_for_us | hidden; completed → done;
    accepted → tracking (a tracker named) | accepted; implemented →
    implemented; snoozed → snoozed (a "Not today" is not a no); expired →
    ignored.

    `labels` is {restaurant_id: {cohort, partitions: {family: key},
    google}} (jobs.learning_labels — every restaurant, computed here when
    not given); `cohorts` ({restaurant_id: cohort}) is still accepted and
    overrides the cohort. A restaurant whose labels changed since its rows
    were last stamped has them re-stamped whole — the cohort set to today's
    value, NULL included (PLATFORM-15). The whole pass is written on one
    connection and committed once (re-audit B22). Idempotent."""
    import json as _json
    from . import provenance, scoring
    written = 0
    from_ledger = 0
    conn = get_conn(db_path)
    try:
        if labels is None:
            try:
                from .jobs import learning_labels
                labels = learning_labels(db_path=db_path, conn=conn)
            except Exception as e:
                print(f"[intelligence.feedback] labels unavailable: {e}")
                labels = {}
        labels = {int(k): dict(v or {}) for k, v in (labels or {}).items()}
        for rid, c in (cohorts or {}).items():
            labels.setdefault(int(rid), {})["cohort"] = c
        _repair_once(conn)
        _repair_episode_keys(conn)
        _repair_per_episode(conn)
        eps = _Episodes(conn)

        def put(rid, kind, key, action, **kw):
            lab = labels.get(rid) or {}
            # The family is the KIND's (the prior reads by kind): a DSR
            # action or a link, whose own family varies by key, is read by
            # service model — the coarsest confirmed partition.
            fam = scoring.kind_family(kind)
            return _record_on(conn, rid, kind, key, action, cohort=lab.get("cohort"),
                              partition_key=(lab.get("partitions") or {}).get(fam),
                              google_data=bool(lab.get("google")), **kw)

        def snap_kw(rec_id):
            c, tv = eps.snapshot(rec_id)
            return {"rec_id": rec_id, "confidence_at": c, "trust_version": tv}

        h0 = _cursor_get(conn, HOME_CURSOR)
        dis = conn.execute("SELECT id, restaurant_id, key, kind, dismissed_at FROM home_dismissals WHERE id > ? "
                           "ORDER BY id LIMIT ?", (h0, LEGACY_ROWS_PER_PASS)).fetchall()
        for r in dis:
            action = {"done": "done", "not_for_us": "not_for_us", "snooze": "snoozed"}.get(r["kind"], "hidden")
            rec_id = eps.at(r["restaurant_id"], r["key"], r["dismissed_at"])
            key = episode_key(r["key"], rec_id) if rec_id else r["key"]
            written += put(r["restaurant_id"], kind_of(r["key"]), key, action, event_at=r["dismissed_at"],
                           synced_from="home_dismissals", **(snap_kw(rec_id) if rec_id else {}))
        if dis:
            _cursor_set(conn, max(int(r["id"]) for r in dis), key=HOME_CURSOR)

        have = _tracker_cols(conn)
        outs, tracker_cursor = _changed_trackers(conn, have) if have else ([], None)
        counted = _counted_tracker_ids([r for r in outs if not str(r.get("source_key") or "")
                                        .startswith("observed:untaken:")])
        # The episode's subject tags (BM4-6), for the trackers read tonight.
        tids = [r["id"] for r in outs if r.get("id") is not None]
        episode_tags = {}
        for i in range(0, len(tids), 400):
            chunk = tids[i:i + 400]
            try:
                for t in conn.execute(f"SELECT tracker_id, tags FROM rec_instances WHERE tracker_id IN "
                                      f"({','.join('?' for _ in chunk)})", chunk).fetchall():
                    episode_tags[t["tracker_id"]] = t["tags"]
            except Exception:
                break
        effects_ok = True
        for r in outs:
            # The kind comes from the key, not the tracker's source: Home's
            # "Done" starts an `observed` tracker under the recommendation's
            # own key ("trim_day:Monday"), whose result belongs to trim_day —
            # not to a meaningless "observed:Monday". kind_of already gives a
            # genuine observed:<action>:<month> key its observed: kind.
            if str(r["source_key"] or "").startswith("observed:untaken:"):
                continue     # advice NOT taken (outcomes.observe_untaken): a comparison, never an answer
            # The recommendation the tracker measures — its linked episode,
            # else its measures_key (outcomes.record rec_key; memory audit
            # 9/29/26, link_trackers) — names the kind, so a fill-a-night
            # text's result is slow_day's, as the restaurant's learner reads
            # it, never a "campaign" kind no recommendation has.
            rec_key = r.get("measures_key") or r["source_key"]
            rec_id = eps.for_tracker(r.get("id")) or eps.at(r["restaurant_id"], rec_key, r.get("created_at"))
            kind = kind_of(eps.key(rec_id) or rec_key)
            tkey = episode_key(r["source_key"], rec_id) if rec_id else r["source_key"]
            written += put(r["restaurant_id"], kind, tkey, "tracking", event_at=r.get("created_at"),
                           synced_from="recommendation_outcomes", metric=r.get("metric"),
                           **(snap_kw(rec_id) if rec_id else {}))
            if r["status"] != "evaluated":
                continue
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
            written += put(r["restaurant_id"], kind, mkey, "measured", outcome=outcome, days_to_effect=days,
                           event_at=r["evaluate_on"], synced_from="recommendation_outcomes", metric=r.get("metric"),
                           **(snap_kw(rec_id) if rec_id else {}))
            if r.get("id") is not None and effects_ok:
                eff = None
                if r["id"] in counted and outcome in ("improved", "worsened", "no_clear_change"):
                    eff = effect_of(r)
                tags = episode_tags.get(r["id"])
                if eff is not None and not tags:
                    try:
                        import rec_ledger
                        tags = json.dumps(sorted(rec_ledger.tags_for(rec_key, kind=kind)))
                    except Exception:
                        tags = None
                try:
                    _set_effect(conn, r["restaurant_id"], mkey, eff, tags)
                except Exception as e:       # a database from before the effect columns
                    print(f"[intelligence.feedback] effect not written: {e}")
                    effects_ok = False
        if tracker_cursor:
            _cursor_set(conn, tracker_cursor, key=TRACKER_CURSOR)

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
        ledger = []
        # `authority` is whose answer it was (rec_ledger's column, memory
        # audit 9/29/26, who_answered / view_as); a database from before the
        # column is read without it.
        for cols in ("e.id, e.rec_id, e.restaurant_id, e.key, e.event, e.meta, e.at, e.authority, ",
                     "e.id, e.rec_id, e.restaurant_id, e.key, e.event, e.meta, e.at, "):
            try:
                ledger = conn.execute(
                    "SELECT " + cols +
                    "EXISTS (SELECT 1 FROM rec_events s WHERE s.rec_id=e.rec_id AND s.event='shown') AS shown "
                    "FROM rec_events e WHERE e.id > ? AND e.event IN "
                    "('accepted','completed','dismissed','snoozed','implemented','expired') ORDER BY e.id LIMIT ?",
                    (start, LEDGER_EVENTS_PER_PASS)).fetchall()
                break
            except Exception:            # a database from before the column, or the ledger
                ledger = []
        last = start
        for r in ledger:
            last = max(last, int(r["id"]))
            key = str(r["key"] or "")
            if not key or key.startswith(_BOOKKEEPING) or key.startswith("ask:") or not r["shown"]:
                continue
            # An admin's answer through view-as is support at work, never
            # the restaurant's preference (memory audit 9/29/26, view_as).
            if (r["authority"] if "authority" in r.keys() else None) == "admin":
                continue
            try:
                meta = _json.loads(r["meta"] or "{}") or {}
            except (TypeError, ValueError):
                meta = {}
            # An admin's answer (support triage, view-as) is nobody's
            # preference and teaches no learner (permissions.answer_authority,
            # memory round contract; recorded on the answer's meta).
            if (meta.get("authority") or meta.get("answer_authority")) == "admin":
                continue
            action = _ledger_action(key, r["event"], meta)
            if not action:
                continue
            n = put(r["restaurant_id"], kind_of(key), episode_key(key, r["rec_id"]), action, event_at=r["at"],
                    synced_from="rec_ledger", **snap_kw(r["rec_id"]))
            written += n
            from_ledger += n
        if ledger:
            _cursor_set(conn, last)
        _fill_confidence(conn)
        _stamp_labels(conn, labels, provenance)
        _rejudge_provenance(conn, labels, provenance)
        conn.commit()
    finally:
        conn.close()
    return {"events": written, "from_ledger": from_ledger, "ledger_cursor": last,
            "trackers_read": len(outs)}


def _fill_confidence(conn):
    """The confidence a bare-key row was shown with (confidence audit, K3):
    the latest episode of the key started on or before the event, as a 0–1
    figure with its trust version. A per-episode row takes its own
    episode's snapshot when it is written; this reads only rows derived in
    the last CONFIDENCE_FILL_DAYS that still have none (PLATFORM-18) — an
    older row's episode can never gain a snapshot, so it is not re-scanned
    every night. An episode shown with no confidence leaves it NULL."""
    sub = ("FROM rec_instances i WHERE i.restaurant_id = intel_rec_events.restaurant_id "
           "AND i.key = intel_rec_events.source_key AND i.confidence_pct IS NOT NULL "
           "AND substr(i.created_at, 1, 10) <= substr(COALESCE(intel_rec_events.event_at, '9999-12-31'), 1, 10)")
    try:
        conn.execute(
            f"UPDATE intel_rec_events SET confidence_at = (SELECT i.confidence_pct / 100.0 {sub} "
            f"ORDER BY i.created_at DESC LIMIT 1), trust_version = (SELECT i.trust_version {sub} "
            f"ORDER BY i.created_at DESC LIMIT 1) "
            f"WHERE confidence_at IS NULL AND created_at >= datetime('now', ?) AND EXISTS (SELECT 1 {sub})",
            (f"-{int(CONFIDENCE_FILL_DAYS)} days",))
    except Exception as e:           # a database from before the snapshot columns
        print(f"[intelligence.feedback] confidence_at not filled: {e}")


def _labels_sig(lab) -> str:
    parts = lab.get("partitions")
    return json.dumps({"c": lab.get("cohort"), "p": parts if parts is not None else "-",
                       "g": lab.get("google") if lab.get("google") is not None else "-"}, sort_keys=True)


def _stamp_labels(conn, labels, provenance):
    """Re-stamp a restaurant's rows whenever its labels changed since they
    were last stamped (and, the first time, every restaurant's — the one-off
    repair of guessed cohorts, PLATFORM-15): `cohort` set to today's value,
    NULL included; `partition_key` per row from the row's metric family;
    `review_derived` where it was never judged; `google_data` = review-
    derived AND the restaurant is Google-connected. Rows of a restaurant no
    longer in the table (a deleted restaurant's kept, anonymised rows) keep
    the labels they had. A label the caller did not give (None) is left as
    it is."""
    from . import scoring
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (LABELS_MARK,)).fetchone()
        seen = json.loads(row["value"]) if row and row["value"] else {}
    except Exception:
        seen = None
    if seen is None:
        return
    changed = False
    for rid, lab in sorted(labels.items()):
        sig = _labels_sig(lab)
        if seen.get(str(rid)) == sig:
            continue
        if "cohort" in lab:
            c = lab.get("cohort")
            conn.execute("UPDATE intel_rec_events SET cohort=? WHERE restaurant_id=? AND cohort IS NOT ?", (c, rid, c))
        parts = lab.get("partitions")
        google = lab.get("google")
        if parts is not None or google is not None:
            for r in conn.execute("SELECT id, rec_kind, source_key, metric, partition_key, review_derived, google_data "
                                  "FROM intel_rec_events WHERE restaurant_id=?", (rid,)).fetchall():
                sets, args = [], []
                rd = r["review_derived"]
                if rd is None:
                    rd = 1 if provenance.review_derived(r["source_key"], kind=r["rec_kind"], metric=r["metric"]) else 0
                    sets.append("review_derived=?"); args.append(rd)
                if parts is not None:
                    pk = parts.get(scoring.kind_family(r["rec_kind"]))
                    if pk != r["partition_key"]:
                        sets.append("partition_key=?"); args.append(pk)
                if google is not None:
                    gd = 1 if (rd and google) else 0
                    if gd != (r["google_data"] or 0):
                        sets.append("google_data=?"); args.append(gd)
                if sets:
                    conn.execute(f"UPDATE intel_rec_events SET {', '.join(sets)} WHERE id=?", (*args, r["id"]))
        seen[str(rid)] = sig
        changed = True
    if changed:
        _cursor_set(conn, json.dumps(seen, sort_keys=True), key=LABELS_MARK)


def _rejudge_provenance(conn, labels, provenance):
    """When the provenance rule changed (provenance.RULE_VERSION against its
    mark in job_cursors), re-judge `review_derived` and `google_data` on
    EVERY intel_rec_events row once (memory re-audit 9/29/26, PLATFORM-6):
    rows already written kept the old rule's labels — review_derived is set
    once and a re-stamp only filled NULLs — so adding dish_praise to the
    rule would have left every dish_praise row already pooled. A row's
    Google label is its restaurant's (`labels`); a kept, anonymised row of a
    deleted restaurant that becomes review-derived is taken as Google data
    (its connection can no longer be read — the conservative side). On the
    caller's connection, uncommitted. Returns rows changed, or None when the
    rule has not changed."""
    try:
        row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (provenance.RULE_MARK,)).fetchone()
    except Exception:
        return None
    if row and str(row["value"] or "") == str(provenance.RULE_VERSION):
        return None
    by_key = provenance.sources_by_key(conn)
    ep = {}
    try:
        for r in conn.execute("SELECT rec_id, evidence_sources FROM rec_instances "
                              "WHERE evidence_sources LIKE '%review%'").fetchall():
            ep[r["rec_id"]] = provenance.review_sourced(r["evidence_sources"])
    except Exception:
        pass
    changed = 0
    for r in conn.execute("SELECT id, restaurant_id, rec_kind, source_key, metric, rec_id, review_derived, google_data "
                          "FROM intel_rec_events").fetchall():
        rid = int(r["restaurant_id"])
        sourced = ep.get(r["rec_id"]) if r["rec_id"] else by_key.get((rid, base_key(r["source_key"])))
        rd = 1 if provenance.review_derived(r["source_key"], kind=r["rec_kind"], metric=r["metric"],
                                            sources=["reviews"] if sourced else None) else 0
        lab = labels.get(rid)
        old_rd, old_gd = int(r["review_derived"] or 0), int(r["google_data"] or 0)
        if lab is not None and lab.get("google") is not None:
            gd = 1 if (rd and lab.get("google")) else 0
        else:
            gd = old_gd if rd == old_rd else (1 if rd else 0)
        if rd != old_rd or gd != old_gd or r["review_derived"] is None:
            conn.execute("UPDATE intel_rec_events SET review_derived=?, google_data=? WHERE id=?", (rd, gd, r["id"]))
            changed += 1
    _cursor_set(conn, str(provenance.RULE_VERSION), key=provenance.RULE_MARK)
    return changed


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
