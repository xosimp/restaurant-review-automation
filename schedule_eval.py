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

  THE SCORE (score_arm). FIRST what the arm itself wrote, before any repair
  (pre_repair_metrics — re-audit 10/11/26 AI-4): its hard breaches, its
  minutes with no manager and the requirement items it leaves unmet; their
  sum of hard breaches and unmet items is ROUTE_METRIC. Then the rows go
  through the production post-model path (schedule_engine.post_model_rows:
  repair_week with the generation's own RepairContext, then the quality
  gate's fill in code when the gate trips) — never repair_rows alone —
  with how much the repair changed (rows_repaired, repair_seconds), and are
  compared with the week the owner published: row-level precision and
  recall on (person, date, role family, start within 30 minutes) and the
  hours by role and day (L1 distance) — SECONDARY, and in a paired
  comparison read only on weeks neither arm drafted (anchored, AI-3).

  VARIANCE (summarize_runs). Every arm runs at least RUNS_PER_ARM (3) times
  per week; an arm is reported as the paired mean over weeks with a 90%
  interval clustered by restaurant (schedule_experiments.cluster_mean_ci),
  and against a baseline arm as the paired difference with its interval.
  A routing decision needs an interval that excludes zero.

  VERDICTS (schedule_eval_verdicts). record_verdict() stores one paired
  comparison per metric and roster-size band (or kind of week: ROUTE_TAGS);
  route_evidence() and tag_evidence() are what schedule_engine's pre-router
  reads: a week starts on T4 for its roster size, or for being a holiday,
  first or many-rule week, only when a stored verdict says T4 beat T3 there
  at 90% on ROUTE_METRIC (AI-3, AI-27). A winner is named only from
  MIN_VERDICT_WEEKS weeks of MIN_VERDICT_RESTAURANTS learning-eligible
  restaurants (AI-5); freeze() takes no demo, test or internal account.

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
# The paired metric the router decides on (scheduler re-audit 10/11/26
# AI-3, AI-4): an OUTCOME of the arm's own rows, read BEFORE the repair loop
# and the gate's fill touch them — the hard breaches the model wrote plus
# the requirement items its week left unmet. It was agreement with the week
# the owner published (F1), which is an edit of whatever production drafted
# (or of last week, carried forward): the arm that drafted it and the
# carry-forward arm won by anchoring, and a better-than-habit week scored
# worse. And it was read after the repair, which flattened Sonnet and Opus
# to the same week (78 v 79, 0 hard after repair, 10/7/26) — the eval could
# only say whether repair could fix a model, never whether a model was
# better. Agreement with the published week is kept as a SECONDARY metric,
# read only on weeks a third arm drafted (ANCHORED_METRICS, anchored).
ROUTE_METRIC = "pre_repair_defects"
VERDICT_METHOD = "paired_cluster_90"
# The metrics on which smaller is better: a verdict on one of them names
# arm_a the winner when its interval sits BELOW zero.
LOWER_IS_BETTER = frozenset({"pre_repair_defects", "hard_before", "unmet_before", "manager_minutes_before",
                             "rows_repaired", "repair_seconds", "hard", "manager_minutes", "hours_l1",
                             "hours_l1_share", "hours_over_budget_share", "cost", "seconds"})
# The metrics anchored to whatever drafted the week the owner published:
# compared only on weeks neither arm of the pair drafted (AI-3).
ANCHORED_METRICS = frozenset({"published_f1", "published_precision", "published_recall", "hours_l1",
                              "hours_l1_share"})
# A verdict names a winner only from at least this many weeks from at least
# this many learning-eligible restaurants in its band or tag (AI-5) — the
# live experiments' own floor (schedule_experiments.MIN_WEEKS_PER_ARM,
# MIN_RESTAURANTS_PER_ARM). cluster_mean_ci gives an interval from two
# restaurants, so Simple EJ's and its demo copy (the same data) could have
# sent a whole roster band to Opus.
MIN_VERDICT_WEEKS = 20
MIN_VERDICT_RESTAURANTS = 5
# The kinds of week the pre-router knows (schedule_engine.hard_week_signals):
# a tag sends its weeks to T4 only on a stored verdict for that tag (AI-27).
ROUTE_TAGS = ("holiday", "closure", "first_week", "many_rules", "escalated")

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
        # A verdict for a kind of week (tag) rather than a roster band, and
        # why it names no winner (AI-5, AI-27).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_eval_verdicts)").fetchall()}
        if "tag" not in cols:
            conn.execute("ALTER TABLE schedule_eval_verdicts ADD COLUMN tag TEXT")
        if "note" not in cols:
            conn.execute("ALTER TABLE schedule_eval_verdicts ADD COLUMN note TEXT")
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

def verdict_winner(metric, arm_a, arm_b, paired) -> tuple:
    """(winner or None, why none): arm_a or arm_b when the 90% interval of
    arm_a minus arm_b excludes zero in the metric's better direction
    (LOWER_IS_BETTER), and only from MIN_VERDICT_WEEKS weeks of
    MIN_VERDICT_RESTAURANTS restaurants (AI-5)."""
    weeks = int(paired.get("weeks") or 0)
    rests = int(paired.get("restaurants") or 0)
    if weeks < MIN_VERDICT_WEEKS or rests < MIN_VERDICT_RESTAURANTS:
        return None, (f"{weeks}/{MIN_VERDICT_WEEKS} weeks from {rests}/{MIN_VERDICT_RESTAURANTS} restaurants — "
                      "too few to name a winner")
    ci = paired.get("ci90")
    if not ci or ci[0] is None or ci[1] is None:
        return None, "no interval"
    lower = metric in LOWER_IS_BETTER
    if ci[0] > 0:
        return (arm_b if lower else arm_a), ""
    if ci[1] < 0:
        return (arm_a if lower else arm_b), ""
    return None, "the 90% interval includes zero"


def record_verdict(metric, arm_a, arm_b, band, paired, version=None, run_key=None, tag=None, db_path=None) -> dict:
    """Store one paired comparison of arm_a against arm_b on `metric` for a
    roster-size `band` (lo, hi) — or, with `tag`, for one kind of week
    (ROUTE_TAGS: holiday, first week ...) whatever its size: `paired` is
    summarize_pair's dict ({"diff", "ci90", "weeks", "restaurants"}). The
    winner is named only past the floors and when the interval excludes
    zero in the metric's better direction (verdict_winner); otherwise the
    row says why not (`note`)."""
    ci = paired.get("ci90")
    winner, note = verdict_winner(metric, arm_a, arm_b, paired)
    if tag:
        band = (0, 10000)
    row = {"metric": metric, "arm_a": arm_a, "arm_b": arm_b, "band_lo": int(band[0]), "band_hi": int(band[1]),
           "weeks": int(paired.get("weeks") or 0), "restaurants": int(paired.get("restaurants") or 0),
           "diff": paired.get("diff"), "ci_lo": (ci or (None, None))[0], "ci_hi": (ci or (None, None))[1],
           "winner": winner, "method": VERDICT_METHOD, "tag": tag, "note": note or None}
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO schedule_eval_verdicts (version, run_key, metric, arm_a, arm_b, band_lo, band_hi, weeks, "
            "restaurants, diff, ci_lo, ci_hi, winner, method, tag, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version, run_key, row["metric"], arm_a, arm_b, row["band_lo"], row["band_hi"], row["weeks"],
             row["restaurants"], row["diff"], row["ci_lo"], row["ci_hi"], winner, VERDICT_METHOD, tag,
             row["note"]))
        conn.commit()
    finally:
        conn.close()
    return row


def latest_verdict(metric, arm_a, arm_b, roster_size=None, tag=None, db_path=None):
    """The newest stored verdict of arm_a against arm_b on `metric` — for
    the band holding `roster_size`, or (with `tag`) for that kind of week —
    or None."""
    conn = get_conn(db_path)
    try:
        if tag:
            r = conn.execute(
                "SELECT * FROM schedule_eval_verdicts WHERE metric=? AND arm_a=? AND arm_b=? AND tag=? "
                "ORDER BY id DESC LIMIT 1", (metric, arm_a, arm_b, tag)).fetchone()
        else:
            n = int(roster_size or 0)
            r = conn.execute(
                "SELECT * FROM schedule_eval_verdicts WHERE metric=? AND arm_a=? AND arm_b=? AND band_lo<=? "
                "AND band_hi>=? AND tag IS NULL ORDER BY id DESC LIMIT 1", (metric, arm_a, arm_b, n, n)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _decisive(v) -> bool:
    """A stored verdict the router may act on: T4 named the winner on
    ROUTE_METRIC, from at least the floors (re-checked on read, so a row
    stored before the floor existed is never evidence)."""
    return bool(v and v.get("winner") == "T4" and int(v.get("weeks") or 0) >= MIN_VERDICT_WEEKS
                and int(v.get("restaurants") or 0) >= MIN_VERDICT_RESTAURANTS)


def _evidence_text(v, what) -> str:
    return (f"the eval showed T4 better for {what} ({float(v['diff'] or 0):+.3f} {ROUTE_METRIC} T4 minus T3, "
            f"90% CI {float(v['ci_lo']):.3f} to {float(v['ci_hi']):.3f}, {v['weeks']} weeks from "
            f"{v['restaurants']} restaurants)")


def route_evidence(roster_size, db_path=None):
    """Why a week of this roster size starts on T4, or None (AI-3: route by
    measured evidence). A reason only when the newest stored verdict of T4
    against T3 for the roster's size band named T4 the winner at 90% on
    ROUTE_METRIC, from the floors (_decisive). Never raises: a store that
    cannot be read is no evidence, so the week starts where the ladder
    starts it."""
    try:
        v = latest_verdict(ROUTE_METRIC, "T4", "T3", roster_size, db_path=db_path)
    except Exception as e:
        print(f"[schedule_eval] route evidence not read: {e!r}")
        return None
    if not _decisive(v):
        return None
    return _evidence_text(v, f"rosters of {v['band_lo']}-{v['band_hi']}")


def tag_evidence(tag, db_path=None):
    """Why a week carrying `tag` (ROUTE_TAGS) starts on T4, or None — the
    same rule as route_evidence, for a kind of week (AI-27: holidays, first
    weeks, many rules and a week whose last draft escalated were sent to
    Opus on assertion). Never raises."""
    try:
        v = latest_verdict(ROUTE_METRIC, "T4", "T3", tag=tag, db_path=db_path)
    except Exception as e:
        print(f"[schedule_eval] tag evidence not read: {e!r}")
        return None
    if not _decisive(v):
        return None
    return _evidence_text(v, f"{str(tag).replace('_', ' ')} weeks")


def set_readiness(items) -> dict:
    """Whether a frozen set can carry a decision (AI-2): per roster band
    and per ROUTE_TAG, its weeks and its restaurants against the floors —
    {"bands": {"lo-hi": {weeks, restaurants, ready}}, "tags": {...},
    "ready": any band ready, "need": the floors}."""
    def _cell(group):
        rests = {it.get("restaurant_id") for it in group}
        return {"weeks": len(group), "restaurants": len(rests),
                "ready": len(group) >= MIN_VERDICT_WEEKS and len(rests) >= MIN_VERDICT_RESTAURANTS}
    bands, tags = {}, {}
    for it in items or []:
        lo, hi = band_of(it.get("roster_size"))
        bands.setdefault(f"{lo}-{hi}", []).append(it)
        for t in it.get("tags") or []:
            if t in ROUTE_TAGS:
                tags.setdefault(t, []).append(it)
    out = {"bands": {k: _cell(v) for k, v in sorted(bands.items())},
           "tags": {k: _cell(v) for k, v in sorted(tags.items())}}
    out["ready"] = any(c["ready"] for c in out["bands"].values())
    out["need"] = {"weeks": MIN_VERDICT_WEEKS, "restaurants": MIN_VERDICT_RESTAURANTS}
    return out


# ── the set: freezing stored generations (AI-6) ────────────────────────────

def _week(week_start):
    from datetime import date, timedelta
    d0 = date.fromisoformat(str(week_start)[:10])
    return [(d0 + timedelta(days=i)).isoformat() for i in range(7)]


def _published_rows(restaurant_id, week_start, db_path=None):
    """The rows the owner published for that week — its live copy's
    published rows (models.live_week_row, the one definition of the week
    in force, ARCH-11) — or None when the week was never published."""
    import schedule_versions as _sv
    import models as _m_live
    conn = get_conn(db_path)
    try:
        r = _m_live.live_week_row(conn, restaurant_id, str(week_start)[:10])
    finally:
        conn.close()
    return _sv.rows_from_csv(r["schedule_csv"] or "") if (r and r["published_at"]) else None


def item_tags(calls, roster_size, restaurant_id, week_start, extra=(), holidays=None, db_path=None) -> list:
    """An item's tags: its roster-size band, "redo" / "gate" when its calls
    were one, "first_week" when the restaurant had no earlier draft,
    "holiday" when a date of the week is in `holidays`, and any hand label
    (an instruction subset, say)."""
    lo, hi = band_of(roster_size)
    tags = [f"size:{lo}-{hi}"]
    kinds = {c.get("call_kind") for c in calls or []}
    if "redo" in kinds:
        tags.append("redo")
    if "gate" in kinds:
        tags.append("gate")
    # Which arm the published week is an edit of (AI-3): agreement with it
    # is never read for that arm.
    by = drafted_by(calls)
    if by:
        tags.append(f"drafted:{by}")
    conn = get_conn(db_path)
    try:
        earlier = conn.execute("SELECT 1 FROM schedule_history WHERE restaurant_id=? AND week_start<? LIMIT 1",
                               (restaurant_id, week_start)).fetchone()
    finally:
        conn.close()
    if not earlier:
        tags.append("first_week")
    if holidays and set(_week(week_start)) & set(holidays):
        tags.append("holiday")
    return sorted(set(tags) | set(extra or ()))


def _may_teach(restaurant_id, week_start, db_path=None) -> bool:
    """models.learning_eligible for the item's restaurant and week; a read
    that fails is no (never freeze what cannot be vouched for)."""
    try:
        import models
        return bool(models.learning_eligible(int(restaurant_id), since=str(week_start or "")[:10] or None,
                                             db_path=db_path))
    except Exception as e:
        print(f"[schedule_eval] eligibility not read for {restaurant_id}: {e!r}")
        return False


def drafted_by(calls) -> str:
    """The tier production's stored calls drafted the week on ("T3", "T4",
    "default" for a pinned model), "mixed" for more than one, or "" when no
    call says — what the week the owner published is anchored to (AI-3)."""
    tiers = sorted({str(c.get("tier")) for c in calls or [] if c.get("tier")})
    return tiers[0] if len(tiers) == 1 else ("mixed" if tiers else "")


def freeze(version, history_ids, tags=None, db_path=None) -> dict:
    """Copy each stored generation of `history_ids` into the frozen set
    `version`: its calls (already scrubbed as schedule_model_calls is), its
    post-model context (schedule_metrics' snapshot, with the last published
    week before it — what the carry-forward arm drafts from) and the rows
    the owner published for that week (the ground truth). A generation with
    no published week or no stored context is skipped, and said. `tags`
    ({history_id: [tag]}) adds hand labels. Returns {"frozen": [item_key],
    "skipped": [{history_id, why}]}."""
    import schedule_output as _so
    import schedule_metrics as _sm
    import schedule_fallback as _sfb
    frozen, skipped = [], []
    for hid in history_ids or []:
        calls = _so.load_calls(history_id=int(hid), db_path=db_path)
        if not calls:
            skipped.append({"history_id": hid, "why": "no stored calls"})
            continue
        first = calls[0]
        rid, ws, gid = first.get("restaurant_id"), first.get("week_start"), first.get("generation_id")
        # Only a restaurant that may teach (models.learning_eligible — not a
        # demo, test or internal account, and its data from after its demo
        # era): a demo copy of a live client is the same data counted twice,
        # and two such "restaurants" could carry a verdict (AI-5).
        if not _may_teach(rid, ws, db_path):
            skipped.append({"history_id": hid, "why": "a demo, test or internal account (or its demo era) — it "
                                                      "may not teach"})
            continue
        blob =_sm.context_for(generation_id=gid, db_path=db_path) or _sm.context_for(history_id=hid,
                                                                                       db_path=db_path)
        if not blob:
            skipped.append({"history_id": hid, "why": "no stored post-model context"})
            continue
        published = _published_rows(rid, ws, db_path=db_path)
        if not published:
            skipped.append({"history_id": hid, "why": "the week was never published"})
            continue
        ctx = _sm.load_context(blob) or {}
        res = ctx.get("result") or {}
        roster_size = len(res.get("roster") or [])
        try:
            base = _sfb.base_week(rid, _week(ws), db_path=db_path)
        except Exception as e:
            print(f"[schedule_eval] base week not read for {hid}: {e!r}")
            base = {}
        key = f"{rid}:{ws}:{gid}"
        tags_now = item_tags(calls, roster_size, rid, ws, extra=(tags or {}).get(hid) or (),
                             holidays=res.get("holidays"), db_path=db_path)
        slim = [{k: c.get(k) for k in ("dates", "model", "effort", "contract", "stop_reason", "outcome", "seconds",
                                       "usage", "rows", "call_kind", "tier", "request", "inputs", "answer")}
                for c in calls]
        conn = get_conn(db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO schedule_eval_items (version, item_key, restaurant_id, week_start, "
                "generation_id, history_id, roster_size, tags_json, calls_z, context_z, published_z) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (version, key, rid, ws, gid, int(hid), roster_size, json.dumps(tags_now), _z(slim),
                 _z({"blob": blob.hex(), "base": base}), _z(published)))
            conn.commit()
        finally:
            conn.close()
        frozen.append(key)
    return {"frozen": frozen, "skipped": skipped}


def load_items(version, tags=None, db_path=None) -> list:
    """The frozen items of `version` (all, or those carrying every tag in
    `tags`): [{item_key, restaurant_id, week_start, roster_size, tags,
    calls, context, base, published}]."""
    import schedule_metrics as _sm
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM schedule_eval_items WHERE version=? ORDER BY id", (version,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        t = json.loads(r["tags_json"] or "[]")
        if tags and not set(tags) <= set(t):
            continue
        holder = _unz(r["context_z"]) or {}
        blob = holder.get("blob")
        out.append({"item_key": r["item_key"], "restaurant_id": r["restaurant_id"], "week_start": r["week_start"],
                    "history_id": r["history_id"], "generation_id": r["generation_id"],
                    "roster_size": r["roster_size"], "tags": t, "calls": _unz(r["calls_z"]) or [],
                    "drafted_by": next((x.split(":", 1)[1] for x in t if str(x).startswith("drafted:")), ""),
                    "context": _sm.load_context(bytes.fromhex(blob)) if blob else None,
                    "base": holder.get("base") or {}, "published": _unz(r["published_z"]) or []})
    return out


# ── the score: against the week the owner published (AI-7) ─────────────────

def _start_min(t):
    from time_utils import parse_clock
    hm = parse_clock(t)
    return hm[0] * 60 + hm[1] if hm else None


def _family(role, c=None):
    if c is not None:
        try:
            return c.family(role)
        except Exception:
            pass
    from shift_quality import role_family
    return role_family(role or "")


def _hours(r) -> float:
    from schedule_rules import row_hours
    try:
        return float(row_hours(r) or 0)
    except Exception:
        return 0.0


def compare_with_published(rows, published, c=None) -> dict:
    """Row-level agreement of `rows` with the week the owner published: a
    row matches a published row of the same person, date and role family
    whose start is within START_TOLERANCE_MINUTES (each published row
    matched once). Precision, recall and F1, and the hours by role family
    and date as an L1 distance (and its share of the published hours)."""
    pub = [dict(p) for p in published or []]
    used = [False] * len(pub)
    by_key = {}
    for i, p in enumerate(pub):
        k = (" ".join(str(p.get("employee") or "").split()).lower(), p.get("date"), _family(p.get("role"), c))
        by_key.setdefault(k, []).append(i)
    hit = 0
    for r in rows or []:
        k = (" ".join(str(r.get("employee") or "").split()).lower(), r.get("date"), _family(r.get("role"), c))
        s = _start_min(r.get("shift_start"))
        for i in by_key.get(k, []):
            ps = _start_min(pub[i].get("shift_start"))
            if not used[i] and s is not None and ps is not None and abs(s - ps) <= START_TOLERANCE_MINUTES:
                used[i] = True
                hit += 1
                break
    n_rows, n_pub = len(rows or []), len(pub)
    precision = hit / n_rows if n_rows else 0.0
    recall = hit / n_pub if n_pub else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    hours = {}
    for sign, src in ((1, rows or []), (-1, pub)):
        for r in src:
            k = (_family(r.get("role"), c), r.get("date"))
            hours[k] = hours.get(k, 0.0) + sign * _hours(r)
    l1 = sum(abs(v) for v in hours.values())
    pub_hours = sum(_hours(p) for p in pub)
    return {"matched": hit, "rows": n_rows, "published_rows": n_pub, "published_precision": round(precision, 4),
            "published_recall": round(recall, 4), "published_f1": round(f1, 4), "hours_l1": round(l1, 2),
            "hours_l1_share": round(l1 / pub_hours, 4) if pub_hours else None}


def _sig(r) -> tuple:
    return (" ".join(str(r.get("employee") or "").split()).lower(), r.get("date") or "",
            str(r.get("role") or "").strip().lower(), r.get("shift_start") or "", r.get("shift_end") or "")


def pre_repair_metrics(rows, item) -> dict:
    """What the ARM wrote, before any repair (scheduler re-audit 10/11/26
    AI-4) — the primary model comparison: the hard breaches in its own rows,
    the minutes its own rows leave with no manager, and the requirement
    items its own week leaves unmet (schedule_output.unmet_items, before the
    gate's fill); `pre_repair_defects` is the hard breaches plus the unmet
    items (ROUTE_METRIC). None where the item carries no Constraints."""
    import schedule_rules as _sr
    ctx = item.get("context") or {}
    c = ctx.get("constraints")
    if c is None:
        return {"hard_before": None, "manager_minutes_before": None, "unmet_before": None,
                "pre_repair_defects": None}
    viols = _sr.violations(rows, c)
    prof = _sr.breach_profile(rows, c, viols)
    hard = sum(1 for v in viols if v.get("hard"))
    try:
        import schedule_output as _so
        res = ctx.get("result") or {}
        unmet = len(_so.unmet_items(rows, constraints=c, violations=viols,
                                    hours_budget=res.get("hours_budget")))
    except Exception as e:
        print(f"[schedule_eval] unmet before repair not read: {e!r}")
        unmet = None
    return {"hard_before": hard, "manager_minutes_before": int(sum((prof.get("manager") or {}).values())),
            "unmet_before": unmet, "pre_repair_defects": (hard + unmet) if unmet is not None else None}


def score_arm(rows, item, gate=True) -> dict:
    """One arm's rows for one frozen item: FIRST what the arm itself wrote
    (pre_repair_metrics — AI-4: the repair flattens models, so the
    comparison of models is read before it), then the rows as production
    would see them — through schedule_engine.post_model_rows with the
    item's own context (the repair loop and the gate's fill) — with how much
    the repair had to change (`rows_repaired`, `repair_seconds`), hard
    breaches, manager minutes, quality, the gate, the hours against the
    week's budget, and (secondary, AI-3) agreement with the week the owner
    published."""
    import time as _time
    import schedule_engine as _se
    import schedule_rules as _sr
    ctx = item.get("context") or {}
    c = ctx.get("constraints")
    out = pre_repair_metrics(rows, item)
    t0 = _time.time()
    post = _se.post_model_rows(rows, ctx, restaurant_id=None, gate=gate)
    repair_seconds = round(_time.time() - t0, 2)
    final = post["rows"]
    prof = _sr.breach_profile(final, c, post["violations"]) if c is not None else {}
    q = post.get("quality") or {}
    out.update(compare_with_published(final, item.get("published") or [], c))
    budget = float(((ctx.get("result") or {}).get("hours_budget")) or 0)
    hours = sum(_hours(r) for r in final)
    out.update({"hard": sum(1 for v in post["violations"] if v.get("hard")),
                "manager_minutes": int(sum((prof.get("manager") or {}).values())) if prof else None,
                "quality": q.get("score") if q.get("checked") else None,
                "gate": (post.get("gate") or {}).get("reason"), "gate_kept": (post.get("gate") or {}).get("kept"),
                "final_rows": len(final),
                "rows_repaired": len({_sig(r) for r in rows or []} ^ {_sig(r) for r in final}),
                "repair_seconds": repair_seconds,
                "hours_over_budget_share": round((hours - budget) / budget, 4) if budget > 0 else None})
    return out


# ── the code arms (AI-8): no model at all ──────────────────────────────────

CODE_ARMS = ("carry_forward", "solver", "solver_v0")
# The deterministic+critic arm (scheduler audit 10/10/26 AI-8): the
# "solver" arm's week reviewed by the model (schedule_critic), each edit
# validated by code — a model arm, run only with --live or an injected call.
CRITIC_ARM = "solver_critic"


def code_arm_rows(arm, item) -> list:
    """The rows a code arm drafts for a frozen item, as production's
    fallback draft would (schedule_fallback): "carry_forward" — the last
    published week before it, on the week's roster and rules (no base: the
    solver); "solver" — the solver-first draft (schedule_cpsat.solver_draft:
    shapes from the people-on-at-once curve, then CP-SAT — scheduler audit
    10/10/26 RM-21), falling back to "solver_v0" — SHIFT REQUIREMENTS laid
    onto the usual shift templates, people by the backtracking solver."""
    import schedule_fallback as _sfb
    ctx = item.get("context") or {}
    c = ctx.get("constraints")
    res = ctx.get("result") or {}
    dates = [d for d in (res.get("week_dates") or _week(item["week_start"]))
             if d not in set(res.get("closed_dates") or ())]
    if arm not in CODE_ARMS:
        raise ValueError(f"unknown code arm {arm!r} (one of {', '.join(CODE_ARMS)})")
    base = item.get("base") or {}
    if arm == "carry_forward" and base.get("rows"):
        return _sfb.carry_forward_rows(base, dates, c, res.get("roster"))["rows"]
    if arm == "solver":
        try:
            import schedule_cpsat as _cps
            if _cps.available():
                got = _cps.solver_draft(dates, c, res.get("requirements") or [], concurrent=res.get("concurrent"),
                                        history_rows=base.get("rows") or [], context_rows=[],
                                        last_week_rows=base.get("rows") if base.get("source") == "published" else [],
                                        signals=ctx.get("signals"), weights=ctx.get("weights"),
                                        roster_roles=res.get("roster_roles") or None,
                                        budget_dollars=res.get("labor_budget_dollars") or None, max_seconds=20.0)
                if got.get("rows"):
                    return got["rows"]
        except Exception as e:
            print(f"[schedule_eval] solver-first arm fell back to v0: {e!r}")
    return _sfb.solver_rows(dates, res.get("requirements_by_date") or {}, c, base_rows=base.get("rows") or [],
                            context_rows=[], open_times=res.get("open_times"), close_times=ctx.get("close_times"),
                            signals=ctx.get("signals"), weights=ctx.get("weights"),
                            roster_roles=res.get("roster_roles") or None, max_seconds=10.0)["rows"]


# ── variance: runs, paired means and intervals (AI-5) ──────────────────────

def record_run(run_key, version, item_key, arm, run_index, metrics, cost_usd=None, seconds=None, db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO schedule_eval_runs (run_key, version, item_key, arm, run_index, metrics_json, "
                     "cost_usd, seconds) VALUES (?,?,?,?,?,?,?,?)",
                     (run_key, version, item_key, arm, int(run_index), json.dumps(metrics, default=str),
                      cost_usd, seconds))
        conn.commit()
    finally:
        conn.close()


def item_means(runs, metric) -> dict:
    """{(arm, item_key): (restaurant, mean over the item's runs)} from
    [{arm, item_key, restaurant_id, metrics}]."""
    acc = {}
    for r in runs:
        v = (r.get("metrics") or {}).get(metric)
        if v is None:
            continue
        e = acc.setdefault((r["arm"], r["item_key"]), [r.get("restaurant_id"), 0.0, 0])
        e[1] += float(v)
        e[2] += 1
    return {k: (e[0], e[1] / e[2]) for k, e in acc.items() if e[2]}


def summarize_arm(runs, arm, metric) -> dict:
    """An arm on `metric`: the mean over items of each item's mean over its
    runs, with a 90% interval clustered by restaurant."""
    from schedule_experiments import cluster_mean_ci
    return cluster_mean_ci([v for (a, _k), v in item_means(runs, metric).items() if a == arm])


ANCHORED_ARMS = ("production", "carry_forward")


def anchored(arm, drafted) -> bool:
    """Whether agreement with a week's published rows says nothing about
    `arm` (AI-3): the stored production answer and the carried-forward last
    week always; a tier arm ("T4" or "T4:claude-opus-5-5/medium/stored") on
    a week its own tier drafted, or one drafted on more than one tier."""
    arm = str(arm or "")
    if arm in ANCHORED_ARMS:
        return True
    d = str(drafted or "")
    if not d:
        return False
    if d == "mixed":
        return arm.split(":", 1)[0] in ("T1", "T2", "T3", "T4", "default")
    return arm == d or arm.startswith(d + ":")


def summarize_pair(runs, arm_a, arm_b, metric) -> dict:
    """arm_a minus arm_b on `metric`, paired by item (each item's mean over
    its runs), with a 90% interval clustered by restaurant: {"diff",
    "ci90", "weeks", "restaurants"}. A decision needs ci90 to exclude 0."""
    from schedule_experiments import cluster_mean_ci
    if metric in ANCHORED_METRICS:
        # Agreement with the published week only on weeks neither arm
        # drafted (AI-3): the week the owner published is an edit of the arm
        # that drafted it, and carrying last week forward imitates habit.
        runs = [r for r in runs if not anchored(arm_a, r.get("drafted_by"))
                and not anchored(arm_b, r.get("drafted_by"))]
    m = item_means(runs, metric)
    pairs =[(rid, v - m[(arm_b, key)][1]) for (a, key), (rid, v) in m.items() if a == arm_a and (arm_b, key) in m]
    s = cluster_mean_ci(pairs)
    return {"diff": s.get("mean"), "ci90": s.get("ci90"), "weeks": s.get("n"), "restaurants": s.get("restaurants")}


def runs_short(runs, arms, items) -> list:
    """[(arm, item_key, runs)] for every arm and item with fewer than
    RUNS_PER_ARM runs — a report with any is not a decision."""
    n = {}
    for r in runs:
        n[(r["arm"], r["item_key"])] = n.get((r["arm"], r["item_key"]), 0) + 1
    return [(a, it, n.get((a, it), 0)) for a in arms for it in items if n.get((a, it), 0) < RUNS_PER_ARM]
