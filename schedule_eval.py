"""
schedule_eval.py — the frozen golden evaluation set for schedule generation,
how an arm's week is scored against the week the owner published, and the
verdicts the router reads (scheduler audit 10/10/26: RM-1, AI-3, AI-5, AI-6,
AI-7, AI-8, AI-22, AI-30).

Why it exists. The model, effort and contract the schedule runs on were
decided on ONE stored week with one sample per arm (PROMPT_LIBRARY →
Schedule), the replays read schedule_model_calls — deleted at 90 days
(ops._RETENTION_DAYS) — so a quarter later the baseline is gone, and the
script scored the old repair_rows alone, never the week the owner actually
sent out. Here:

  THE SET (schedule_eval_items). freeze() copies a curated set of stored
  generations — each one's every call (request and answer, scrubbed as
  schedule_output.record_call already scrubs: a guest's contact details and
  labelled names redacted, staff names and figures kept, since a replay
  needs them), the post-model context the job scored it in (the Constraints,
  the quality signals, the week's figures — schedule_metrics' snapshot of
  the generation, `context_z`), and the PUBLISHED rows of that week — into a
  versioned set (`version`) that NO retention rule touches
  (ops._RETENTION_DAYS has no entry for these tables, and
  tests/test_sf10_e_eval.py holds it so). Items are tagged (`tags`: size
  band, holiday, first week, redo, gate) so a set spans 15-250 staff,
  holiday, first and redo weeks.

  THE SCORE (score_against_published). An arm's rows go through the
  production post-model path (schedule_engine.post_model_rows: repair_week
  with the generation's own RepairContext, then the quality gate's fill in
  code when the gate trips) — never repair_rows alone — and are compared
  with the week the owner published: row-level precision and recall on
  (person, date, role family, start within 30 minutes) and the hours by
  role and day (L1 distance), beside hard breaches, manager minutes,
  quality and unmet items.

  VARIANCE (summarize_runs). Every arm runs at least RUNS_PER_ARM (3) times
  per week; an arm is reported as the paired mean over weeks with a 90%
  interval clustered by restaurant (schedule_experiments.cluster_mean_ci),
  and against a baseline arm as the paired difference with its interval.
  A routing decision needs an interval that excludes zero.

  VERDICTS (schedule_eval_verdicts). record_verdict() stores one paired
  comparison per metric and roster-size band; route_evidence() is what
  schedule_engine's pre-router reads: a week starts on T4 for its roster
  size only when a stored verdict says T4 beat T3 there at 90% (AI-3: it was
  "roster over 120", a size the code itself called untested).

Pure where it can be; the tables are created at boot (init_schedule_eval,
from models.init_db).
"""
import json

import zlib


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


# Every arm runs this many times on every week before its mean is read.
RUNS_PER_ARM = 3
# A start within this many minutes of the published one is the same shift.
START_TOLERANCE_MINUTES = 30
# Roster-size bands the verdicts (and the router) are read by: [lo, hi].
SIZE_BANDS = ((0, 39), (40, 79), (80, 119), (120, 179), (180, 10000))
# The paired metric the router decides on: agreement with the published
# week (F1 of row precision and recall) — what an owner keeps, not the
# internal scorer.
ROUTE_METRIC = "published_f1"
VERDICT_METHOD = "paired_cluster_90"

_DDL = (
    """CREATE TABLE IF NOT EXISTS schedule_eval_items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        version         TEXT    NOT NULL,
        item_key        TEXT    NOT NULL,
        restaurant_id   INTEGER,
        week_start      TEXT,
        generation_id   TEXT,
        history_id      INTEGER,
        roster_size     INTEGER,
        tags_json       TEXT,
        calls_z         BLOB,
        context_z       BLOB,
        published_z     BLOB,
        frozen_at       TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(version, item_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sched_eval_items_v ON schedule_eval_items(version, id)",
    """CREATE TABLE IF NOT EXISTS schedule_eval_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_key         TEXT    NOT NULL,
        version         TEXT    NOT NULL,
        item_key        TEXT    NOT NULL,
        arm             TEXT    NOT NULL,
        run_index       INTEGER NOT NULL DEFAULT 0,
        metrics_json    TEXT,
        cost_usd        REAL,
        seconds         REAL,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sched_eval_runs_key ON schedule_eval_runs(run_key, arm)",
    """CREATE TABLE IF NOT EXISTS schedule_eval_verdicts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        version         TEXT,
        run_key         TEXT,
        metric          TEXT    NOT NULL,
        arm_a           TEXT    NOT NULL,
        arm_b           TEXT    NOT NULL,
        band_lo         INTEGER,
        band_hi         INTEGER,
        weeks           INTEGER,
        restaurants     INTEGER,
        diff            REAL,
        ci_lo           REAL,
        ci_hi           REAL,
        winner          TEXT,
        method          TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sched_eval_verdicts ON schedule_eval_verdicts(metric, arm_a, arm_b, id)",
)


def init_schedule_eval(db_path=None):
    """The eval set, its runs and its verdicts — at boot (models.init_db),
    never on a call."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _z(obj) -> bytes:
    return zlib.compress(json.dumps(obj, default=str).encode("utf-8", "replace"))


def _unz(blob):
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def band_of(roster_size):
    """The SIZE_BANDS band a roster of `roster_size` falls in, (lo, hi)."""
    n = int(roster_size or 0)
    for lo, hi in SIZE_BANDS:
        if lo <= n <= hi:
            return lo, hi
    return SIZE_BANDS[-1]


# ── verdicts and the router (AI-3, AI-5) ───────────────────────────────────

def record_verdict(metric, arm_a, arm_b, band, paired, version=None, run_key=None, db_path=None) -> dict:
    """Store one paired comparison of arm_a against arm_b on `metric` for a
    roster-size `band` (lo, hi): `paired` is summarize_pair's dict ({"diff",
    "ci90", "weeks", "restaurants"}). The winner is named only when the 90%
    interval excludes zero (arm_a above it: arm_a; below: arm_b)."""
    ci = paired.get("ci90")
    winner = None
    if ci and ci[0] is not None and ci[1] is not None:
        if ci[0] > 0:
            winner = arm_a
        elif ci[1] < 0:
            winner = arm_b
    row = {"metric": metric, "arm_a": arm_a, "arm_b": arm_b, "band_lo": int(band[0]), "band_hi": int(band[1]),
           "weeks": int(paired.get("weeks") or 0), "restaurants": int(paired.get("restaurants") or 0),
           "diff": paired.get("diff"), "ci_lo": (ci or (None, None))[0], "ci_hi": (ci or (None, None))[1],
           "winner": winner, "method": VERDICT_METHOD}
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO schedule_eval_verdicts (version, run_key, metric, arm_a, arm_b, band_lo, band_hi, weeks, "
            "restaurants, diff, ci_lo, ci_hi, winner, method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version, run_key, row["metric"], arm_a, arm_b, row["band_lo"], row["band_hi"], row["weeks"],
             row["restaurants"], row["diff"], row["ci_lo"], row["ci_hi"], winner, VERDICT_METHOD))
        conn.commit()
    finally:
        conn.close()
    return row


def latest_verdict(metric, arm_a, arm_b, roster_size, db_path=None):
    """The newest stored verdict of arm_a against arm_b on `metric` whose
    band holds `roster_size`, or None."""
    n = int(roster_size or 0)
    conn = get_conn(db_path)
    try:
        r = conn.execute(
            "SELECT * FROM schedule_eval_verdicts WHERE metric=? AND arm_a=? AND arm_b=? AND band_lo<=? "
            "AND band_hi>=? ORDER BY id DESC LIMIT 1", (metric, arm_a, arm_b, n, n)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def route_evidence(roster_size, db_path=None):
    """Why a week of this roster size starts on T4, or None (AI-3: route by
    measured evidence). A reason only when the newest stored verdict of T4
    against T3 for the roster's size band named T4 the winner at 90% on
    ROUTE_METRIC. Never raises: a store that cannot be read is no
    evidence, so the week starts where the ladder starts it."""
    try:
        v = latest_verdict(ROUTE_METRIC, "T4", "T3", roster_size, db_path=db_path)
    except Exception as e:
        print(f"[schedule_eval] route evidence not read: {e!r}")
        return None
    if not v or v.get("winner") != "T4":
        return None
    return (f"the eval showed T4 better for rosters of {v['band_lo']}-{v['band_hi']} "
            f"(+{float(v['diff'] or 0):.3f} {ROUTE_METRIC}, 90% CI {float(v['ci_lo']):.3f} to "
            f"{float(v['ci_hi']):.3f}, {v['weeks']} weeks)")
