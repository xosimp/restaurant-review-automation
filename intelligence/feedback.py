"""Feedback collection: every recommendation's life as events.

Nothing in the product changes how it records an answer — Home writes
`home_dismissals`, trackers live in `recommendation_outcomes`, Ask writes
`ask_cavnar_actions`, the queue writes `delayed_actions`. `sync()` reads
those and derives one event row per (restaurant, recommendation, action),
so history that predates the engine is learned from too. New callers may
`record()` directly.

`rec_kind` is the key's prefix ("trim_day", "cut_waste", "reprice",
"observed:schedule_published" …) — a category of recommendation, never the
text — so kinds can be compared across restaurants without carrying what
was said to whom.
"""
from datetime import date

from models import get_conn, DB_PATH

ACTIONS = ("presented", "done", "not_for_us", "hidden", "tracking", "measured", "confirmed", "dismissed", "auto")
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


def sync(db_path=DB_PATH, cohorts: dict = None) -> dict:
    """Derive events from the tables that already hold the answers.
    Idempotent: the UNIQUE(restaurant, key, action) makes a re-run a no-op.
    `cohorts` is {restaurant_id: cohort} so events carry their cohort."""
    cohorts = cohorts or {}
    written = 0
    conn = get_conn(db_path)
    try:
        dis = conn.execute("SELECT restaurant_id, key, kind, dismissed_at FROM home_dismissals").fetchall()
        outs = conn.execute("SELECT restaurant_id, source, source_key, status, verdict, started_on, evaluate_on, created_at "
                            "FROM recommendation_outcomes").fetchall()
        asks = conn.execute("SELECT restaurant_id, action, summary, outcome, created_at FROM ask_cavnar_actions "
                            "WHERE outcome IN ('confirmed','dismissed')").fetchall()
        auto = conn.execute("SELECT restaurant_id, kind, id, status, executed_at, created_at FROM delayed_actions "
                            "WHERE status IN ('done','executed','cancelled','canceled')").fetchall()
    finally:
        conn.close()
    for r in dis:
        action = {"done": "done", "not_for_us": "not_for_us"}.get(r["kind"], "hidden")
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
        key = f"ask:{r['action']}:{(r['summary'] or '')[:40]}"
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
    return {"events": written}


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
