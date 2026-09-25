"""
rec_learning.py — what the recommendation ledger has learned about ONE
restaurant, and the owner's own record of it.

rec_ledger holds every recommendation's episode and events. Until the ROI
audit (Sep 2026) only the admin console read them and only one Home band
learned from a result. This module is the restaurant-scoped reader:

  effectiveness(rid)   the per-restaurant effectiveness model the rankers
                       read (home_brief.order_recommendations, the DSR's
                       action ranking, business_intelligence.pick_one_thing)
                       — acceptance × success × dollar calibration by kind
                       and by subject tag, shrunk toward the cohort's own
                       rates when the cohort clears the privacy floor, else
                       toward even, plus a decaying penalty for a result that
                       got worse (audit #24, #29, #47). BOUNDED: it moves a
                       rank by at most ±25% (a worse result can take it to
                       0.6×); it only ever reorders, never removes, and the
                       rankers never apply it to a critical item.
  summary(rid, days)   what the owner followed, by module (ignored episodes
                       stay in the denominator), and what it did, by tag —
                       GET /recs/summary.
  timeline(rid)        every recommendation shown, newest first, with its
                       answer, reason, implementation and tracker —
                       GET /recs/timeline.
  viewer_sees(viewer, row)   the redaction every owner-facing read applies:
                       a manager never sees a loss, a food-cost or an
                       owner-only recommendation (issues.viewer_sees_loss and
                       the module view permissions — the rules the issue list
                       and the DSR already draw).

Level 1 only: every query is WHERE restaurant_id = ?. The one cross-
restaurant input — the cohort prior — comes through the intelligence facade,
exists only over MIN_COHORT restaurants, and is asserted anonymous before it
is used. Deterministic; no model, no network.
"""
import json
import math
from datetime import datetime, timedelta

import models as _models_mod
from models import DB_PATH
import rec_ledger


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


# ── the numbers behind every rate ───────────────────────────────────────────

# An acceptance rate is quoted once this many recommendations have SETTLED
# (answered, or shown and left to expire); a success rate once this many
# results were measured with a clear verdict. Below them the figure is
# returned with enough=false and the surfaces say "too few yet".
MIN_SETTLED_FOR_RATE = 10
MIN_MEASURED_FOR_RATE = 5
PRIOR_MIN_MEASURED = 10       # cohort measured results before a cohort rate stands in (intelligence.confidence)
SUMMARY_WINDOWS = (30, 90, 180)
_Z90 = 1.645
CLEAR_VERDICTS = ("improved", "worsened", "no_clear_change")
# outcomes.INFORMATIONAL_PREFIX / INFORMATIONAL_PREFIXES (a test holds them
# in step): a tracker that measured what followed an alert being READ, a
# routine supplier order, or advice NOT taken — not a change.
INFORMATIONAL_PREFIX = "observed:alert_"
INFORMATIONAL_PREFIXES = (INFORMATIONAL_PREFIX, "observed:supplier_order_sent:", "observed:untaken:")

# The effectiveness model (see the module docstring).
EFFECT_WINDOW_DAYS = 365
SHRINK_K = 5                   # pseudo-observations pulling a rate to its prior
# How much a point of each moves the weight. Acceptance is what the owner
# LIKES, and rank decides exposure, which drives acceptance: a loop (CA2
# #8). So acceptance weighs at most 0.2, and it can never lift a weight
# above 1.0 on its own: the upward ceiling comes from measured success
# alone (below).
W_ACCEPT, W_SUCCESS = 0.2, 1.0
MIN_WEIGHT, MAX_WEIGHT = 0.75, 1.25
# A kind with no record here may be ranked with help from similar
# restaurants' results, within these bounds only (BM3-12, Top-50 #32).
COLD_PRIOR_BOUNDS = (0.9, 1.1)
# The cohort record a prior reads: the last PRIOR_WINDOW_DAYS only
# (intelligence.scoring.PRIOR_WINDOW_DAYS; a test holds them in step).
PRIOR_WINDOW_DAYS = 365
# The upward ceiling scales with the LOWER end of the 90% Wilson interval of
# this restaurant's own measured success for the kind (or its best subject
# tag) above the prior: 3 of 3 (lower bound 0.53) allows about 1.01, 30 of
# 30 (0.92) about 1.21, so three results and thirty no longer rank alike
# (CA2 probe E). No measured result, no lift above 1.0.
FLOOR_WEIGHT = 0.6             # the lowest a worse result can take it
WORSE_KEY_STEP, WORSE_KEY_CAP = 0.20, 0.30     # per worse result on this very key
WORSE_KIND_STEP, WORSE_KIND_CAP = 0.08, 0.20   # per worse result on its kind
WORSE_HALF_LIFE_DAYS = 60
MIN_CALIBRATION_PAIRS = 3      # predicted-vs-measured pairs before dollars adjust
CALIBRATION_BOUNDS = (0.8, 1.2)
# Once this many pairs exist the lower bound is lifted (re-audit B2 #8): 2
# of 10 results realised $1,000 and 8 realised $0 on $1,000 estimates, and
# the 0.8 floor showed $800 against a measured mean of $200. With enough
# pairs the measured median stands, shrunk toward 1 as before; the realised
# mean is shown beside it either way (adjusted_dollars `realised_mean`).
CALIBRATION_WIDE_PAIRS = 8
CALIBRATION_WIDE_BOUNDS = (0.0, 1.2)

# The success rate expected from doing nothing (re-audit B2 #2, #7): a
# result reads "improved" by chance when a move of nothing crosses the noise
# band on the improving side — half the band's two-sided false-alarm rate,
# which is stated at 10% (metrics.BAND_K), so 5%. Measured do-nothing rates
# ran 3-6% (probe B2 p2 S1/S2). Every success rate here is shrunk toward
# THIS, not toward even: shrinking toward 0.5 inflated a short record (0 of
# 5 read 28%) and ranked never-measured kinds above measured ones.
BASE_RATE_STATED = 0.05
BASE_RATE_BOUNDS = (0.02, 0.5)

# Historical Accuracy's recency-weighted companion (re-audit B2 #15): each
# measured result weighs 0.5 ** (age / RECENT_HALF_LIFE_DAYS), age from its
# evaluation, so a year-old result counts a sixteenth of this month's. An
# ADDITIONAL figure (kind_record `rate_recent`); `rate` is unchanged.
RECENT_HALF_LIFE_DAYS = 90


def _stamp(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def wilson(k, n, z=_Z90):
    """90% Wilson interval for k of n as (low, high); (None, None) at n=0."""
    if not n:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return round(max(0.0, (c - m) / d), 3), round(min(1.0, (c + m) / d), 3)


def _shrink(rate, n, prior, k=SHRINK_K):
    if rate is None or not n:
        return prior
    return (float(rate) * n + prior * k) / (n + k)


# ── who may read which recommendation ───────────────────────────────────────

# Kinds that are Food Cost whatever module their surface filed them under.
FOOD_KINDS = ("reprice", "price_spike", "food_cost_driver", "cut_waste", "stock_low", "critical_low", "stock",
              "diag_food", "food_diagnosis", "insight_food", "food_waste", "invoice")
_MONEY_MODULE = {"food_cost": "food", "food": "food", "labor": "labor", "reviews": "reviews",
                 "marketing": "marketing", "intel": "intel"}
# The names a surface files a module's evidence under, as the permission
# vocabulary viewer_sees reads. "food_cost" (business_intelligence's links),
# "inventory" (Home's module key) and "menu" are all Food Cost: read as
# their own names they matched no permission and a manager was shown a
# food-cost link (re-audit B12).
MODULE_ALIASES = {"food_cost": "food", "inventory": "food", "menu": "food", "labor_cost": "labor",
                  "review": "reviews", "guest": "guests"}


def _module_name(m):
    m = str(m or "").strip().lower()
    return MODULE_ALIASES.get(m, m)


def _is_loss(row) -> bool:
    key = str(row.get("key") or "")
    return row.get("kind") == "loss" or key == "loss" or key.startswith("loss:")


def modules_of(row) -> set:
    """Every module a recommendation's content comes from: its own, its
    evidence, and what its kind says (a food kind filed under Home is food;
    "money:food_cost" is food; a DSR action on the food block is food; a
    cross-module link names both of its modules — "reviews_x_food_cost",
    "reviews_x_menu" are food). Names are normalised (MODULE_ALIASES)."""
    out = set()
    if row.get("module"):
        out.add(_module_name(row["module"]))
    src = row.get("evidence_sources")
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except (TypeError, ValueError):
            src = []
    out.update(_module_name(s) for s in (src or []) if s)
    key = str(row.get("key") or "")
    kind = row.get("kind") or rec_ledger.kind_of(key)
    parts = key.split(":")
    if kind in FOOD_KINDS:
        out.add("food")
    if kind == "money" and len(parts) > 1:
        out.add(_MONEY_MODULE.get(parts[1], _module_name(parts[1])))
    if kind == "dsr_action" and len(parts) > 2:
        block = parts[2].split("/", 1)[0]
        out.add({"sales": "ops", "closeout": "ops"}.get(block, _module_name(block)))
    if kind == "link" and len(parts) > 1:
        out.update(_module_name(m) for m in parts[1].split("_x_") if m)
    out.discard("")
    return out


def viewer_sees(viewer, row) -> bool:
    """Whether this login may see this recommendation. No viewer is an
    internal caller (and an admin sees everything). Fails closed."""
    if viewer is None or (isinstance(viewer, dict) and viewer.get("is_admin")):
        return True
    try:
        import issues
        from permissions import (has_permission, is_principal, REVIEWS_VIEW, LABOR_VIEW, FOOD_COST_VIEW,
                                 MARKETING_VIEW, INTEL_VIEW)
        if _is_loss(row) and not issues.viewer_sees_loss(viewer):
            return False
        if row.get("owner_only") and not is_principal(viewer):
            return False
        need = {"reviews": REVIEWS_VIEW, "labor": LABOR_VIEW, "schedule": LABOR_VIEW, "food": FOOD_COST_VIEW,
                "marketing": MARKETING_VIEW, "guests": MARKETING_VIEW, "intel": INTEL_VIEW}
        for m in modules_of(row):
            perm = need.get(m)
            if perm and not has_permission(viewer, perm):
                return False
        return True
    except Exception as e:
        print(f"[rec_learning] viewer check failed closed: {e}")
        return False


# ── episodes, as the readers below need them ────────────────────────────────

_TRACKER_COLS = ("id, status, verdict, dollars_monthly, evaluate_on, started_on, metric, after_start, "
                 "after_end, recheck_verdict, owner_checkin, source_key")
# outcomes._ADDED_COLUMNS the learning reads (CA2 #1): a result measured
# against its own trigger window is never a win; and the other changes found
# in its window (`concurrent`, re-audit B2 #5): a confounded result is never
# counted either way.
_TRACKER_CALIBRATION_COLS = ", baseline_overlaps_trigger, attribution, concurrent"


def _tracker_rows(conn, rid, tids):
    out = {}
    for i in range(0, len(tids), 400):
        chunk = tids[i:i + 400]
        marks = ",".join("?" for _ in chunk)
        rows = None
        for cols in (_TRACKER_COLS + _TRACKER_CALIBRATION_COLS, _TRACKER_COLS):
            try:
                rows = conn.execute(f"SELECT {cols} FROM recommendation_outcomes WHERE restaurant_id=? AND id IN "
                                    f"({marks})", (rid, *chunk)).fetchall()
                break
            except Exception as e:
                print(f"[rec_learning] tracker columns missing ({e}); reading fewer")
        if rows is None:
            # A database from before the re-check / check-in columns: the
            # verdict alone, and said so (a silent fallback here once hid
            # every re-check and check-in from the learning).
            rows = conn.execute(f"SELECT id, status, verdict, dollars_monthly, evaluate_on FROM "
                                f"recommendation_outcomes WHERE restaurant_id=? AND id IN ({marks})",
                                (rid, *chunk)).fetchall()
        for o in rows:
            out[o["id"]] = dict(o)
    return out


def _load(conn, rid, since=None, before=None, limit=None, order_desc=False, rec_ids=None, lean=False):
    """Episodes of this restaurant (bookkeeping keys excluded) with their
    events folded in. `before` is a (created_at, rec_id) cursor.

    `lean` is for the effectiveness model, which reads a year of episodes
    on every Home build: the `shown` rows — most of the trail, one per
    surface per day — are not loaded, only whether each episode has one
    (re-audit B18); `surfaces` is then empty."""
    where, args = ["restaurant_id=?"], [rid]
    if since:
        where.append("created_at >= ?")
        args.append(since)
    if before:
        where.append("(created_at < ? OR (created_at = ? AND rec_id < ?))")
        args += [before[0], before[0], before[1]]
    if rec_ids is not None:
        if not rec_ids:
            return []
        where.append(f"rec_id IN ({','.join('?' for _ in rec_ids)})")
        args += list(rec_ids)
    sql = (f"SELECT * FROM rec_instances WHERE {' AND '.join(where)} "
           f"ORDER BY created_at {'DESC' if order_desc else 'ASC'}, rec_id {'DESC' if order_desc else 'ASC'}")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    rows = [r for r in rows if rec_ledger.counts_in_acceptance(r["key"])]
    if not rows:
        return []
    evs = {}
    shown = set()
    ids = [r["rec_id"] for r in rows]
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" for _ in chunk)
        if lean:
            for e in conn.execute(f"SELECT rec_id, event, surface, meta, at FROM rec_events WHERE rec_id IN ({marks}) "
                                  f"AND event != 'shown' ORDER BY at, id", chunk).fetchall():
                evs.setdefault(e["rec_id"], []).append(dict(e))
            for e in conn.execute(f"SELECT DISTINCT rec_id FROM rec_events WHERE rec_id IN ({marks}) "
                                  f"AND event = 'shown'", chunk).fetchall():
                shown.add(e["rec_id"])
        else:
            for e in conn.execute(f"SELECT rec_id, event, surface, meta, at FROM rec_events WHERE rec_id IN "
                                  f"({marks}) ORDER BY at, id", chunk).fetchall():
                evs.setdefault(e["rec_id"], []).append(dict(e))
    trackers = {}
    tids = sorted({r["tracker_id"] for r in rows if r.get("tracker_id")})
    if tids:
        try:
            trackers = _tracker_rows(conn, rid, tids)
        except Exception as e:
            print(f"[rec_learning] trackers unreadable: {e}")
    now = datetime.utcnow()
    for r in rows:
        es = evs.get(r["rec_id"], [])
        r["events"] = es
        r["shown"] = (r["rec_id"] in shown) if lean else any(e["event"] == "shown" for e in es)
        r["surfaces"] = sorted({e["surface"] for e in es if e["event"] == "shown" and e["surface"]})
        r["tag_list"] = rec_ledger.episode_tags(r)
        r["state"] = _state(r, now)
        r["verdict"], r["verdict_at"] = _verdict(r, es, trackers.get(r.get("tracker_id")))
        r["tracker"] = trackers.get(r.get("tracker_id"))
    return rows


def _meta(e):
    try:
        return json.loads(e.get("meta") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _state(r, now):
    """accepted | completed | implemented | dismissed | ignored | superseded |
    snoozed | open — what the episode amounts to now. A taken episode whose
    change was actually made reads implemented whichever answer took it (a
    reprice is answered and applied in one step)."""
    st = r["status"]
    if st in rec_ledger.TAKEN_STATUSES and r.get("implemented_at"):
        return "implemented"
    if st in ("accepted", "completed", "implemented", "dismissed", "superseded"):
        return st
    if st == "expired":
        return "ignored"
    if r.get("snoozed_until") and r["snoozed_until"] > _stamp(now):
        return "snoozed"
    if rec_ledger.is_stale(r, now=now):
        return "ignored"
    return "open"


def _confounded(tr) -> bool:
    """outcomes.confounded on a tracker row (a test holds the two in step):
    its stored `concurrent` list names at least one other change, trend or
    level shift. NULL (never checked) is not confounded."""
    raw = (tr or {}).get("concurrent")
    if raw in (None, ""):
        return False
    if isinstance(raw, list):
        conc = raw
    else:
        try:
            conc = json.loads(raw)
        except (TypeError, ValueError):
            return False
    return bool(isinstance(conc, list) and any(isinstance(c, dict) for c in conc))


def learned_verdict(verdict, tracker=None, checkin=None):
    """What a measured result says about the recommendation it measured —
    the ONE mapping rec_learning and the engine's feedback.sync both read
    (re-audit B9), following outcomes.counts_in_delivered:

      * the owner said they did not make the change, or that something else
        changed in those weeks (the tracker's owner_checkin, or the ledger's
        latest check-in) → `unknown`: neither this recommendation working
        nor failing;
      * a tracker that measured what followed a READ (informational) →
        `unknown`: it measured no change;
      * a result read against a baseline that overlapped the window which
        triggered the recommendation (outcomes baseline_overlaps_trigger,
        CA2 #1) → `unknown`: a number coming back from a bad stretch on its
        own is not the recommendation working;
      * a result read alongside another change on the same number, a trend
        already under way or a level shift at the trigger (outcomes.
        confounded — the tracker's stored `concurrent` list; re-audit B2
        #5, #10) → `unknown`, whichever way it read: it can't be separated
        from what else moved the number;
      * a move that faded or reversed at its re-check → `no_clear_change`:
        not a win (and not a loss — the number went back);
      * anything that is not a clear verdict → `unknown`.
    A result is improved or worsened here exactly when outcomes.
    counts_in_delivered counts it (the learning == value rule, CA2 #7).
    `verdict` is the verdict as recorded; `tracker` the recommendation_
    outcomes row (dict) when there is one."""
    tr = tracker or {}
    ck = checkin
    if ck is None:
        raw = tr.get("owner_checkin")
        if isinstance(raw, dict):
            ck = raw
        elif raw:
            try:
                ck = json.loads(raw)
            except (TypeError, ValueError):
                ck = None
    if isinstance(ck, dict) and (ck.get("did_it") == "no" or ck.get("conditions_changed")
                                 or (ck.get("attribution") or {}).get("discount")):
        return "unknown"
    if str(tr.get("source_key") or "").startswith(INFORMATIONAL_PREFIXES):
        return "unknown"
    try:
        overlaps = bool(int(tr.get("baseline_overlaps_trigger") or 0))
    except (TypeError, ValueError):
        overlaps = bool(tr.get("baseline_overlaps_trigger"))
    if overlaps:
        return "unknown"
    if _confounded(tr):
        return "unknown"
    if verdict in ("improved", "worsened") and tr.get("recheck_verdict") in ("faded", "reversed"):
        return "no_clear_change"
    return verdict if verdict in CLEAR_VERDICTS else "unknown"


def _verdict(r, events, tracker):
    """The measured result of an episode — the ledger's latest outcome event,
    else its linked tracker's verdict — or (None, None), read through
    learned_verdict: a result the owner's check-in discounted (did not do
    it, or something else changed) reads `unknown`, and one that faded or
    reversed at its re-check is not a win."""
    verdict, at = None, None
    for e in events:
        if e["event"] == "outcome":
            v = _meta(e).get("verdict")
            if v:
                verdict, at = v, e["at"]
    if tracker and tracker.get("status") == "evaluated":
        # The tracker's CURRENT verdict: the ledger's outcome event is the
        # verdict as first carried in.
        verdict, at = (tracker.get("verdict") or verdict or "unknown"), tracker.get("evaluate_on")
    if verdict is None:
        return None, None
    if tracker and tracker.get("evaluate_on"):
        at = tracker["evaluate_on"]
    checkins = [_meta(e) for e in events if e["event"] == "checkin"]
    return learned_verdict(verdict, tracker, checkins[-1] if checkins else None), at


def _taken(r):
    return r["state"] in rec_ledger.TAKEN_STATUSES


def _settled(r):
    return r["state"] in rec_ledger.TAKEN_STATUSES + ("dismissed", "ignored")


# ── the owner's summary ─────────────────────────────────────────────────────

def summary(restaurant_id, days=30, viewer=None, db_path=DB_PATH) -> dict:
    """What this restaurant followed and what it did, over the last `days`
    (episodes first shown in the window). See API_REFERENCE.md → /recs/summary.

    by_module: shown, answered, accepted, completed, implemented, dismissed,
    ignored, n (settled = answered + ignored — an ignored recommendation is
    in the denominator), accept_rate ((accepted + completed + implemented) /
    n) with its 90% Wilson interval, enough (n ≥ MIN_SETTLED_FOR_RATE).

    by_tag: one row per subject tag, over recommendations TAKEN with a
    result, across every module the tag was recommended under — measured
    (clear verdicts, one per change: _one_per_window), improved, worsened,
    no_clear_change, unknown (not measurable, or discounted by the owner's
    check-in; never in the denominator), success_rate (improved / measured),
    enough (measured ≥ MIN_MEASURED_FOR_RATE), module (where most of its
    results came from) and modules. A result that faded or reversed at its
    re-check is not a win (learned_verdict). most_effective: the tag with
    enough results and the best lower bound of success, when that is at
    least even, with its own improved/measured."""
    days = int(days)
    since_d = datetime.utcnow() - timedelta(days=days)
    conn = get_conn(db_path)
    try:
        eps = _load(conn, restaurant_id, since=_stamp(since_d))
    finally:
        conn.close()
    eps = [e for e in eps if e["shown"] and e["state"] != "superseded" and viewer_sees(viewer, e)]
    by_module = {}
    for e in eps:
        m = by_module.setdefault(e["module"] or "home", {"shown": 0, "answered": 0, "accepted": 0, "completed": 0,
                                                         "implemented": 0, "dismissed": 0, "ignored": 0})
        m["shown"] += 1
        if e["state"] in ("accepted", "completed", "implemented", "dismissed"):
            m[e["state"]] += 1
            m["answered"] += 1
        elif e["state"] == "ignored":
            m["ignored"] += 1
    for m in by_module.values():
        n = m["answered"] + m["ignored"]
        took = m["accepted"] + m["completed"] + m["implemented"]
        lo, hi = wilson(took, n)
        m.update({"n": n, "accept_rate": round(took / n, 3) if n else None, "accept_rate_low": lo,
                  "accept_rate_high": hi, "enough": n >= MIN_SETTLED_FOR_RATE})
    by_tag = _by_tag([e for e in eps if _taken(e) and e["verdict"]])
    # The record in three figures (#recs' strip, density audit #40): taken
    # of what settled, measured better of what was measured (each change
    # once, _one_per_window), and what is still open. Each rate carries its
    # own floor (`*_enough`), below which the page shows "—", never a rate.
    shown = sum(m["shown"] for m in by_module.values())
    settled = sum(m["n"] for m in by_module.values())
    took_all = sum(m["accepted"] + m["completed"] + m["implemented"] for m in by_module.values())
    results = _one_per_window([e for e in eps if _taken(e) and e["verdict"]])
    measured = sum(1 for e in results if e["verdict"] in CLEAR_VERDICTS)
    totals = {"shown": shown, "settled": settled, "taken": took_all, "open": max(shown - settled, 0),
              "measured": measured, "improved": sum(1 for e in results if e["verdict"] == "improved"),
              "taken_enough": settled >= MIN_SETTLED_FOR_RATE,
              "measured_enough": measured >= MIN_MEASURED_FOR_RATE}
    return {"ok": True, "days": days, "since": since_d.strftime("%Y-%m-%d"), "by_module": by_module,
            "totals": totals,
            "by_tag": by_tag, "most_effective": most_effective(by_tag),
            "min_settled": MIN_SETTLED_FOR_RATE, "min_measured": MIN_MEASURED_FOR_RATE}


def _after_window(tracker):
    """(start, end) ISO dates a tracker's "after" was read over — the same
    reading as outcomes._after_window — or None without a tracker."""
    tr = tracker or {}
    start = tr.get("after_start") or tr.get("started_on")
    if not start:
        return None
    end = tr.get("after_end") or tr.get("evaluate_on") or start
    return str(start)[:10], str(end)[:10]


def _one_per_window(eps) -> list:
    """The measured episodes of one tag with each change counted once: one
    result per tracker, and on one metric one result per non-overlapping
    after-window (the earliest kept) — two recommendations read over the
    same weeks on the same number are one change, whichever way it went
    (owner_report's rule for results, applied per tag; re-audit B13)."""
    seen, by_metric, kept = set(), {}, []
    for e in eps:
        tr = e.get("tracker") or {}
        tid = tr.get("id")
        if tid is not None:
            if tid in seen:
                continue
            seen.add(tid)
        win = _after_window(tr)
        if e["verdict"] in CLEAR_VERDICTS and tr.get("metric") and win:
            by_metric.setdefault(tr["metric"], []).append((win, e))
        else:
            kept.append(e)
    for items in by_metric.values():
        last_end = ""
        for (start, end), e in sorted(items, key=lambda x: (x[0][0], x[1].get("rec_id") or 0)):
            if last_end and start <= last_end:
                continue
            kept.append(e)
            last_end = max(last_end, end)
    return kept


def _by_tag(eps) -> list:
    """One row per subject TAG, across every module it was recommended
    under — "weekend staffing" filed under Labor (Home's trim_day cards) and
    under Schedule (Shift Quality's lines) is one subject, and splitting it
    let 5 of 5 in one module stand as the answer while 0 of 6 in the other
    was never weighed (re-audit B13). `module` is the module most of its
    results came from (the key clients match most_effective against);
    `modules` lists them all."""
    by_tag = {}
    for e in eps:
        for t in e["tag_list"]:
            by_tag.setdefault(t, []).append(e)
    out = []
    for tag, group in by_tag.items():
        g = {"improved": 0, "worsened": 0, "no_clear_change": 0, "unknown": 0}
        mods = {}
        for e in _one_per_window(group):
            v = e["verdict"] if e["verdict"] in CLEAR_VERDICTS else "unknown"
            g[v] += 1
            m = e["module"] or "home"
            mods[m] = mods.get(m, 0) + 1
        measured = g["improved"] + g["worsened"] + g["no_clear_change"]
        module = sorted(mods.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if mods else "home"
        out.append({"tag": tag, "label": rec_ledger.tag_label(tag), "module": module, "modules": sorted(mods),
                    "measured": measured, **g,
                    "success_rate": round(g["improved"] / measured, 3) if measured else None,
                    "enough": measured >= MIN_MEASURED_FOR_RATE})
    out.sort(key=lambda r: (-r["measured"], r["tag"], r["module"]))
    return out


def most_effective(by_tag):
    """The subject most often followed by an improvement: enough measured
    results (one per change — _one_per_window — across every module the
    tag was recommended under), success at least even, ranked by the LOWER
    end of its 90% interval (so three of three never outranks nine of ten).
    Carries its own counts (`improved` of `measured`), so a sentence quotes
    the same totals it was chosen on. Followed by an improvement, not the
    cause of one: the measurement is before-and-after."""
    best, best_key = None, None
    for r in by_tag or []:
        if not r["enough"] or r["success_rate"] is None or r["success_rate"] < 0.5:
            continue
        lo, _ = wilson(r["improved"], r["measured"])
        k = (lo, r["measured"], r["tag"])
        if best_key is None or k > best_key:
            best, best_key = r, k
    if best is None:
        return None
    return {"tag": best["tag"], "label": best["label"], "module": best["module"], "modules": best.get("modules"),
            "success_rate": best["success_rate"], "measured": best["measured"], "improved": best["improved"]}


# ── the owner's timeline ────────────────────────────────────────────────────

TIMELINE_MAX = 100


def _cursor(before):
    """(created_at, rec_id) from `before`: the `next_before` a page returned
    ("<created_at>|<rec_id>"), or a plain timestamp."""
    if not before:
        return None
    raw = str(before)
    stamp, _, rec_id = raw.partition("|")
    t = rec_ledger._stamp(stamp)
    if not t:
        return None
    return t, (rec_id or "~")          # "~" sorts after every hex id: a bare stamp keeps nothing at it


def _answer_event(r):
    """The event that gave the episode its state, or None."""
    want = {"accepted": ("accepted",), "completed": ("completed",), "implemented": ("implemented", "accepted"),
            "dismissed": ("dismissed",), "snoozed": ("snoozed",)}.get(r["state"])
    if not want:
        return None
    for e in reversed(r["events"]):
        if e["event"] in want:
            return e
    return None


def _item(r) -> dict:
    ev = _answer_event(r)
    meta = _meta(ev) if ev else {}
    if r["state"] in ("ignored", "superseded"):
        answered_at = r.get("closed_at")
    else:
        answered_at = ev["at"] if ev else (r.get("closed_at") if r["state"] != "open" else None)
    first = next((e["at"] for e in r["events"] if e["event"] == "shown"), r["created_at"])
    return {"key": r["key"], "title": r.get("title") or r["key"], "module": r.get("module") or "home",
            "tags": r["tag_list"], "first_shown_at": first, "surfaces": r["surfaces"],
            "answer": "expired" if r["state"] == "ignored" else r["state"], "answered_at": answered_at,
            "reason_code": meta.get("reason_code") if meta.get("reason_code") in rec_ledger.REASON_CODES else None,
            "reason": meta.get("reason"), "implemented_at": r.get("implemented_at"),
            "tracker_id": r.get("tracker_id")}


def timeline(restaurant_id, limit=30, before=None, viewer=None, db_path=DB_PATH) -> dict:
    """Every recommendation this restaurant was shown, newest first:
    {items, next_before}. `before` is the previous page's next_before. The
    viewer's redaction is applied before the page is filled, so a manager's
    page is full of what they may see."""
    try:
        limit = max(1, min(int(limit or 30), TIMELINE_MAX))
    except (TypeError, ValueError):
        limit = 30
    cur = _cursor(before)
    items, exhausted = [], False
    batch = limit * 2
    conn = get_conn(db_path)
    try:
        for _ in range(6):                      # bounded refills after redaction
            raw = conn.execute(
                "SELECT created_at, rec_id FROM rec_instances WHERE restaurant_id=? "
                + ("AND (created_at < ? OR (created_at = ? AND rec_id < ?)) " if cur else "")
                + "ORDER BY created_at DESC, rec_id DESC LIMIT ?",
                ((restaurant_id, cur[0], cur[0], cur[1], batch) if cur else (restaurant_id, batch))).fetchall()
            if not raw:
                exhausted = True
                break
            loaded = {r["rec_id"]: r for r in _load(conn, restaurant_id, rec_ids=[x["rec_id"] for x in raw])}
            stopped = False
            for x in raw:
                cur = (x["created_at"], x["rec_id"])
                r = loaded.get(x["rec_id"])
                if r and r["shown"] and viewer_sees(viewer, r):
                    items.append(_item(r))
                    if len(items) >= limit:
                        stopped = x is not raw[-1]
                        break
            if len(items) >= limit:
                exhausted = len(raw) < batch and not stopped
                break
            if len(raw) < batch:
                exhausted = True
                break
    finally:
        conn.close()
    return {"ok": True, "items": items,
            "next_before": None if (exhausted or not cur) else f"{cur[0]}|{cur[1]}"}


# ── the effectiveness model the rankers read ────────────────────────────────

class Effectiveness:
    """This restaurant's learned weight for a recommendation — see the
    module docstring. `weight(key)` returns (weight, why); 1.0 and no
    reasons when nothing has been learned."""

    def __init__(self, restaurant_id, episodes, cohort=None, db_path=DB_PATH, now=None, base_rates=None):
        self.rid = restaurant_id
        self.cohort = cohort
        self.db_path = db_path
        self.now = now or datetime.utcnow()
        self._priors = {}
        self._cold = {}
        self._base_rates = base_rates
        self.kinds, self.tags = {}, {}
        self.worse_keys, self.worse_kinds = {}, {}
        self.calibration = {}
        self.calibration_realised = {}
        clear_eps = {}
        for e in episodes:
            if not e["shown"] or e["state"] in ("superseded", "open", "snoozed"):
                continue
            kind = e["kind"] or rec_ledger.kind_of(e["key"])
            for bucket, name in ((self.kinds, kind),
                                 *((self.tags, t) for t in e["tag_list"] if t.startswith(
                                     ("topic:", "focus:", "category:", "dish:", "item:", "daypart:")))):
                s = bucket.setdefault(name, {"taken": 0, "settled": 0, "improved": 0, "measured": 0})
                s["settled"] += 1
                if _taken(e):
                    s["taken"] += 1
                    if e["verdict"] in CLEAR_VERDICTS:
                        clear_eps.setdefault((id(bucket), name), (s, []))[1].append(e)
        # One result per change (re-audit B2 #7): the measured results of a
        # kind or tag are counted exactly as kind_record counts them — one
        # per tracker, one per overlapping after-window on a number — and the
        # worse-result penalties and dollar pairs come from the same set.
        for (bid, name), (s, eps) in clear_eps.items():
            kept = [e for e in _one_per_window(eps) if e["verdict"] in CLEAR_VERDICTS]
            s["measured"] = len(kept)
            s["improved"] = sum(1 for e in kept if e["verdict"] == "improved")
            if bid != id(self.kinds):
                continue
            for e in kept:
                if e["verdict"] == "worsened":
                    age = self._age_days(e["verdict_at"])
                    self.worse_keys.setdefault(e["key"], []).append(age)
                    self.worse_kinds.setdefault(name, []).append(age)
                tr = e.get("tracker") or {}
                if e.get("dollar_value"):
                    realised = float(tr.get("dollars_monthly") or 0.0) if e["verdict"] != "no_clear_change" else 0.0
                    if tr.get("dollars_monthly") is not None or e["verdict"] == "no_clear_change":
                        self.calibration.setdefault(name, []).append(realised / float(e["dollar_value"]))
                        self.calibration_realised.setdefault(name, []).append(realised)

    def _age_days(self, at):
        t = rec_ledger._stamp(at)
        if not t:
            return 0.0
        return max(0.0, (self.now - datetime.strptime(t, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400.0)

    def base_rate(self, kind):
        """The success rate doing nothing gives for this kind here
        (rec_learning.base_rate), read once per model."""
        if self._base_rates is None:
            try:
                self._base_rates = _base_rate_inputs(self.rid, self.db_path)
            except Exception as e:
                print(f"[rec_learning] base rates unavailable for {self.rid}: {e}")
                self._base_rates = {}
        return base_rate_from(self._base_rates, kind)["rate"]

    def prior(self, kind):
        """(acceptance prior, success prior) for a kind: the cohort's rates
        when the cohort clears MIN_COHORT (asserted anonymous) — its success
        rate over the capped counts (no one restaurant above scoring.
        MAX_RESTAURANT_SHARE of it) shrunk toward this kind's base rate —
        else even acceptance and the BASE RATE for success (re-audit B2 #7:
        a success prior of 0.5 ranked a never-measured kind above one that
        measurably worked)."""
        if kind in self._priors:
            return self._priors[kind]
        acc = 0.5
        suc = self.base_rate(kind)
        if self.cohort:
            try:
                import intelligence
                from intelligence import privacy
                # The cohort WITHOUT this restaurant — its own record is
                # weighed against the prior, never counted inside it — and
                # each rate only over the restaurants that contributed to it:
                # five restaurants that let one card expire are no floor for a
                # success rate one restaurant measured (re-audit B3).
                s = intelligence.recommendation_success(kind, cohort=self.cohort, db_path=self.db_path,
                                                        exclude_restaurant_id=self.rid,
                                                        window_days=PRIOR_WINDOW_DAYS)
                privacy.assert_anonymous(s)
                if s.get("answered") and s.get("acceptance_available"):
                    acc = float(s.get("acceptance_rate_shrunk") or 0.5)
                if s.get("measured") and s.get("success_available"):
                    pm = float(s.get("measured_capped", s.get("measured")) or 0.0)
                    pi = float(s.get("improved_capped", s.get("improved")) or 0.0)
                    suc = _shrink(pi / pm if pm else None, pm, suc)
            except Exception as e:
                print(f"[rec_learning] cohort prior unavailable for {kind}: {e}")
        self._priors[kind] = (acc, suc)
        return acc, suc

    def cold_prior(self, kind):
        """{weight, restaurants, rate} for a kind this restaurant has no
        record of, from intelligence.scoring.similar_prior (the cohort's
        other restaurants, weighted by DNA similarity and recency, capped
        per restaurant, over the privacy floors) — or None. The weight is
        1 + W_SUCCESS × (shrunk rate − this kind's do-nothing rate), held
        to COLD_PRIOR_BOUNDS: peers can nudge the order of a new kind's
        cards, never decide it."""
        if kind in self._cold:
            return self._cold[kind]
        out = None
        if self.cohort:
            try:
                from intelligence import scoring as _scoring
                sp = _scoring.similar_prior(kind, self.rid, self.cohort, db_path=self.db_path, now=self.now)
                if sp.get("available") and sp.get("rate") is not None:
                    base = self.base_rate(kind)
                    rate = _shrink(sp["rate"], sp.get("weighted") or 0.0, base)
                    w = 1.0 + W_SUCCESS * (rate - base)
                    out = {"weight": round(min(COLD_PRIOR_BOUNDS[1], max(COLD_PRIOR_BOUNDS[0], w)), 3),
                           "restaurants": int(sp["restaurants"]), "rate": round(rate, 3)}
            except Exception as e:
                print(f"[rec_learning] similar-restaurant prior unavailable for {kind}: {e}")
        self._cold[kind] = out
        return out

    def _delta(self, s, prior):
        acc_p, suc_p = prior
        acc = _shrink(s["taken"] / s["settled"] if s["settled"] else None, s["settled"], acc_p)
        suc = _shrink(s["improved"] / s["measured"] if s["measured"] else None, s["measured"], suc_p)
        return W_ACCEPT * (acc - acc_p) + W_SUCCESS * (suc - suc_p)

    def weight(self, key, kind=None, tags=None):
        key = str(key or "")
        kind = kind or rec_ledger.kind_of(key)
        tags = rec_ledger.tags_for(key, kind=kind) if tags is None else tags
        why = []
        ks = self.kinds.get(kind)
        learned = ([ks] if ks else []) + [self.tags[t] for t in tags if self.tags.get(t)]
        deltas = []
        if learned:
            # The cohort prior is read only when there is something of this
            # restaurant's own to weigh against it.
            prior = self.prior(kind)
            deltas = [self._delta(s, prior) for s in learned]
        if ks:
            why.append(f"{kind}: taken {ks['taken']} of {ks['settled']}, {ks['improved']} of {ks['measured']} "
                       f"measured improved")
        w = 1.0 + (sum(deltas) / len(deltas) if deltas else 0.0)
        cold = None
        if not learned:
            # No record of its own: ranked with help from similar
            # restaurants' results (BM3-12, Top-50 #32), bounded to
            # COLD_PRIOR_BOUNDS and said. Ranking only — never a %.
            cold = self.cold_prior(kind)
            if cold is not None:
                w = cold["weight"]
                why.append(f"ranked with help from {cold['restaurants']} similar restaurants' results")
        ratio, _n_cal = self.calibration_ratio(kind)
        if ratio is not None:
            w *= ratio
            why.append(f"measured dollars run {ratio:.2f}× the estimate")
        w = min(self.ceiling(learned, kind) if cold is None else COLD_PRIOR_BOUNDS[1], max(MIN_WEIGHT, w))
        key_pen = min(WORSE_KEY_CAP, sum(WORSE_KEY_STEP * 0.5 ** (a / WORSE_HALF_LIFE_DAYS)
                                         for a in self.worse_keys.get(key, [])))
        kind_pen = min(WORSE_KIND_CAP, sum(WORSE_KIND_STEP * 0.5 ** (a / WORSE_HALF_LIFE_DAYS)
                                           for a in self.worse_kinds.get(kind, [])))
        if key_pen or kind_pen:
            w *= (1 - key_pen) * (1 - kind_pen)
            why.append("a result got worse after it here" if key_pen else f"a {kind} result got worse here")
        w = round(max(FLOOR_WEIGHT, min(MAX_WEIGHT, w)), 3)
        return w, why

    def ceiling(self, learned, kind=None):
        """The highest this weight may reach: 1.0 plus MAX_WEIGHT's headroom,
        scaled by how far the Wilson lower bound of measured success (the
        best of the kind's and its tags' own records) sits above the prior."""
        best = 0.0
        prior = self.prior(kind)[1] if (learned and kind) else 0.5
        for s in learned or []:
            if not s.get("measured"):
                continue
            lo, _ = wilson(s["improved"], s["measured"])
            if lo is not None and prior < 1.0:
                best = max(best, (lo - prior) / (1.0 - prior))
        return 1.0 + (MAX_WEIGHT - 1.0) * min(1.0, max(0.0, best))

    def calibration_ratio(self, kind):
        """(ratio, n): measured dollars over the dollars the recommendation
        was shown with, the median of this restaurant's pairs for the kind,
        shrunk toward 1 by 3 pseudo-pairs and bounded to CALIBRATION_BOUNDS
        — below CALIBRATION_WIDE_PAIRS; from there to CALIBRATION_WIDE_BOUNDS
        (no floor: the measured shortfall stands) — or (None, n) below
        MIN_CALIBRATION_PAIRS."""
        cal = self.calibration.get(kind) or []
        if len(cal) < MIN_CALIBRATION_PAIRS:
            return None, len(cal)
        ratio = sorted(cal)[len(cal) // 2]
        ratio = (ratio * len(cal) + 1.0 * 3) / (len(cal) + 3)      # shrunk toward 1
        lo, hi = CALIBRATION_WIDE_BOUNDS if len(cal) >= CALIBRATION_WIDE_PAIRS else CALIBRATION_BOUNDS
        return round(min(hi, max(lo, ratio)), 3), len(cal)

    def realised(self, kind) -> dict:
        """{realised_mean, realised_ratio_mean, n}: what this restaurant's
        measured results of the kind actually came to — the mean realised
        monthly dollars (no clear change = $0) and the mean of realised ÷
        estimate — or Nones below MIN_CALIBRATION_PAIRS."""
        cal = self.calibration.get(kind) or []
        dollars = self.calibration_realised.get(kind) or []
        if len(cal) < MIN_CALIBRATION_PAIRS or not dollars:
            return {"realised_mean": None, "realised_ratio_mean": None, "n": len(cal)}
        return {"realised_mean": round(sum(dollars) / len(dollars), 2),
                "realised_ratio_mean": round(sum(cal) / len(cal), 3), "n": len(cal)}

    def adjusted_dollars(self, key, dollars, kind=None) -> dict:
        """The dollars a recommendation is SHOWN with, corrected by what this
        restaurant's measured results of its kind actually came to (CA2 #8:
        the calibration used to move rank only, never the figure). Returns
        {dollars, dollars_adjusted, calibration_n, calibration_ratio, note}:
        `dollars_adjusted` is None (show `dollars` as it is) below
        MIN_CALIBRATION_PAIRS measured results; otherwise the figure times
        the ratio, and `note` reads "adjusted from N measured results"."""
        kind = kind or rec_ledger.kind_of(str(key or ""))
        ratio, n = self.calibration_ratio(kind)
        real = self.realised(kind)
        out = {"dollars": dollars, "dollars_adjusted": None, "calibration_n": n, "calibration_ratio": ratio,
               "note": None, "realised_mean": real["realised_mean"],
               "realised_ratio_mean": real["realised_ratio_mean"]}
        try:
            d = float(dollars)
        except (TypeError, ValueError):
            return out
        if ratio is None:
            return out
        out["dollars_adjusted"] = round(d * ratio, 2)
        out["note"] = f"adjusted from {n} measured result{'s' if n != 1 else ''}"
        return out

    def __call__(self, key, kind=None, tags=None):
        return self.weight(key, kind=kind, tags=tags)


def attach_dollar_calibration(item, learned, key=None, dollars_field="dollars_monthly") -> dict:
    """Put the calibrated dollars beside a shown recommendation's own figure
    (CA2 #8, F6) — the one place Home cards, the one-thing hero and the DSR
    actions get them from:

      dollars_adjusted   the figure times this restaurant's measured ratio
                         for the kind, or None below MIN_CALIBRATION_PAIRS
                         (the client then shows `dollars_field` as it is)
      calibration_n      measured predicted-vs-realised pairs behind it
      calibration_note   "adjusted from N measured results", or None
      realised_mean      the mean monthly dollars those measured results of
                         the kind actually realised (no clear change = $0),
                         shown beside the estimate — None below
                         MIN_CALIBRATION_PAIRS (re-audit B2 #8)

    `dollars_field` (dollars_monthly) is left as the raw estimate on
    purpose: it is what the ledger snapshots as `dollar_value`, and the
    ratio is realised ÷ dollar_value. Snapshotting the adjusted figure
    would feed each correction back into the next ratio. Never raises."""
    item["dollars_adjusted"] = None
    item["calibration_n"] = 0
    item["calibration_note"] = None
    item["realised_mean"] = None
    if learned is None or item.get(dollars_field) in (None, 0, 0.0):
        return item
    try:
        adj = learned.adjusted_dollars(key or item.get("key"), item.get(dollars_field))
    except Exception as e:
        print(f"[rec_learning] dollar calibration failed for {key or item.get('key')}: {e}")
        return item
    item["calibration_n"] = int(adj.get("calibration_n") or 0)
    item["realised_mean"] = adj.get("realised_mean")
    if adj.get("dollars_adjusted") is not None:
        item["dollars_adjusted"] = adj["dollars_adjusted"]
        item["calibration_note"] = adj.get("note")
    return item


def effectiveness(restaurant_id, db_path=DB_PATH, restaurant=None, now=None) -> Effectiveness:
    """The model for one restaurant from its last EFFECT_WINDOW_DAYS of
    episodes. Never raises: with the ledger unreadable it is the neutral
    model (every weight 1.0)."""
    now = now or datetime.utcnow()
    cohort = None
    try:
        if restaurant is None:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path=db_path)
        if restaurant is not None:
            # Only a type the owner SET: a guessed type reads no group's
            # record (Benchmarking re-audit R2-7, #14, #20).
            from intelligence import categories as _cats
            cohort = _cats.confirmed_type(restaurant)
    except Exception as e:
        print(f"[rec_learning] cohort unresolved for {restaurant_id}: {e}")
    try:
        conn = get_conn(db_path)
        try:
            eps = _load(conn, restaurant_id, since=_stamp(now - timedelta(days=EFFECT_WINDOW_DAYS)), lean=True)
        finally:
            conn.close()
    except Exception as e:
        print(f"[rec_learning] effectiveness unavailable for {restaurant_id}: {e}")
        eps = []
    return Effectiveness(restaurant_id, eps, cohort=cohort, db_path=db_path, now=now)


def kind_record(restaurant_id, kind, db_path=DB_PATH, restaurant=None, now=None, episodes=None) -> dict:
    """This restaurant's measured record for one recommendation kind — the
    Historical Accuracy input of the confidence engine (rec_trust).

    Counted exactly as the owner's record is: episodes that were shown and
    taken, read through learned_verdict (a disowned, conditions-changed,
    informational, faded or reversed result is never a win), one result per
    overlapping after-window. Returns
      {kind, measured, improved, rate, low, high, source, prior_measured,
       prior_improved, prior_restaurants, rate_recent, rate_recent_n_eff,
       recent_half_life_days, base_rate, base_rate_source, base_rate_n,
       base_rate_basis, untaken}
    where `rate` is the own improved share only at MIN_MEASURED_FOR_RATE
    measured; below that `source` is "cohort" when the anonymous cohort of
    the restaurant's CONFIRMED type (categories.confirmed_type; its whole
    organisation excluded) clears privacy.cohort_ok and privacy.MIN_ORGS
    organisations and has at least PRIOR_MIN_MEASURED measured results
    after no one organisation is allowed more than
    scoring.MAX_RESTAURANT_SHARE of them (the capped counts — re-audit B2
    #3, Benchmarking re-audit #14; the uncapped counts are not returned),
    else "none" and rate None. The cohort's `prior_*`
    counts are filled at ANY own count (group P): the confidence engine
    reads them only as its prior's centre, which they may only lower.

    `rate_recent` (re-audit B2 #15) is the own improved share with each
    result weighted 0.5 ** (age / RECENT_HALF_LIFE_DAYS), beside `rate`, at
    the same floor; `rate_recent_n_eff` is the summed weight. `base_rate` is
    what doing nothing gives for this kind here (base_rate) — the value a
    success rate should be shrunk toward, never 0.5.
    Never raises. `episodes` lets a caller that already loaded the ledger
    (Home) pass it in."""
    kind = str(kind or "")
    now = now or datetime.utcnow()
    out = {"kind": kind, "measured": 0, "improved": 0, "rate": None, "low": None, "high": None,
           "source": "none", "prior_measured": 0, "prior_improved": 0, "prior_restaurants": 0,
           "rate_recent": None, "rate_recent_n_eff": None, "recent_half_life_days": RECENT_HALF_LIFE_DAYS,
           "base_rate": BASE_RATE_STATED, "base_rate_source": "stated", "base_rate_n": 0,
           "base_rate_basis": None}
    try:
        if episodes is None:
            conn = get_conn(db_path)
            try:
                episodes = _load(conn, restaurant_id, since=_stamp(now - timedelta(days=EFFECT_WINDOW_DAYS)),
                                 lean=True)
            finally:
                conn.close()
        mine = [e for e in episodes
                if e.get("shown") and _taken(e)
                and (e.get("kind") or rec_ledger.kind_of(e["key"])) == kind]
        measured = [e for e in _one_per_window(mine) if e["verdict"] in CLEAR_VERDICTS]
        out["measured"] = len(measured)
        out["improved"] = sum(1 for e in measured if e["verdict"] == "improved")
    except Exception as e:
        print(f"[rec_learning] kind_record unavailable for {restaurant_id}/{kind}: {e}")
        return out
    out["untaken"] = untaken_comparison(restaurant_id, kind, taken_measured=out["measured"],
                                        taken_improved=out["improved"], db_path=db_path)
    try:
        br = base_rate(restaurant_id, kind, db_path=db_path)
        out.update(base_rate=br["rate"], base_rate_source=br["source"], base_rate_n=br["n"],
                   base_rate_basis=br["basis"])
    except Exception as e:
        print(f"[rec_learning] base rate unavailable for {restaurant_id}/{kind}: {e}")
    own = out["measured"] >= MIN_MEASURED_FOR_RATE
    if own:
        out["rate"] = out["improved"] / out["measured"]
        out["low"], out["high"] = wilson(out["improved"], out["measured"])
        out["source"] = "own"
        out["rate_recent"], out["rate_recent_n_eff"] = recency_weighted_rate(measured, now=now)
    # The anonymous cohort is read at ANY own count now (confidence round 2,
    # group P): it is the prior's centre in the engine's Beta read — which
    # it may only pull DOWN — and never a figure on its own. Below the own
    # floor it is labelled (source "cohort") but carries no percentage
    # (B1 H9, B2 #3).
    try:
        import intelligence
        from intelligence import privacy
        if restaurant is None:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path=db_path)
        from intelligence import categories as _cats
        cohort = _cats.confirmed_type(restaurant)
        if cohort:
            s = intelligence.recommendation_success(kind, cohort=cohort, db_path=db_path,
                                                    exclude_restaurant_id=restaurant_id,
                                                    window_days=PRIOR_WINDOW_DAYS)
            privacy.assert_anonymous(s)
            # The capped counts (scoring.MAX_RESTAURANT_SHARE): one peer's
            # eight results among twelve no longer stand as the cohort.
            pm = float(s.get("measured_capped", s.get("measured")) or 0)
            pi = float(s.get("improved_capped", s.get("improved")) or 0)
            # Over MIN_COHORT restaurants from privacy.MIN_ORGS organisations,
            # the viewer's own organisation out (scoring, re-audit #14) — and
            # only the capped counts: the raw ones had no owner use and made
            # one organisation's share recoverable (R1-02).
            if pm >= PRIOR_MIN_MEASURED and s.get("success_available"):
                out.update(prior_measured=int(round(pm)), prior_improved=int(round(pi)),
                           prior_restaurants=int(s.get("measured_restaurants") or 0))
                # The label of the cohort ACTUALLY read (NS4 H4): never "like
                # yours" when it is the whole platform.
                try:
                    from intelligence import benchmarks as _bm
                    out["prior_label"] = _bm.cohort_label(cohort)
                except Exception:
                    out["prior_label"] = "other restaurants on Cavnar"
                if not own:
                    out.update(rate=pi / pm, source="cohort")
                    out["low"], out["high"] = wilson(pi, pm)
    except Exception as e:
        print(f"[rec_learning] cohort record unavailable for {kind}: {e}")
    return out


def recency_weighted_rate(measured, now=None, half_life_days=RECENT_HALF_LIFE_DAYS):
    """(rate, n_eff) over measured episodes, each weighted 0.5 ** (age /
    half_life_days) with age from its verdict — or (None, 0.0) with no
    weight. Pure."""
    now = now or datetime.utcnow()
    num = den = 0.0
    for e in measured or []:
        t = rec_ledger._stamp(e.get("verdict_at"))
        try:
            age = max(0.0, (now - datetime.strptime(t, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400.0) if t else 0.0
        except (TypeError, ValueError):
            age = 0.0
        w = 0.5 ** (age / float(half_life_days))
        den += w
        num += w if e.get("verdict") == "improved" else 0.0
    if den <= 0:
        return None, 0.0
    return round(num / den, 3), round(den, 2)


def _base_rate_inputs(restaurant_id, db_path=DB_PATH) -> dict:
    """{kind: {"far": [stated false-alarm rates of its trackers], "untaken":
    [(verdict, metric, after_start, after_end)]}, "*": {"far": [...]}} — the
    raw material of base_rate, read in two queries. Taken trackers give the
    false-alarm rate their band was read at; untaken trackers
    (outcomes.observe_untaken) what the number did when the advice was not
    taken."""
    out = {"*": {"far": [], "untaken": []}}
    conn = get_conn(db_path)
    try:
        try:
            for r in conn.execute(
                    "SELECT i.kind, i.key, o.false_alarm_rate FROM rec_instances i JOIN recommendation_outcomes o "
                    "ON o.id = i.tracker_id AND o.restaurant_id = i.restaurant_id WHERE i.restaurant_id=? "
                    "AND o.false_alarm_rate IS NOT NULL", (restaurant_id,)).fetchall():
                k = r["kind"] or rec_ledger.kind_of(r["key"])
                out.setdefault(k, {"far": [], "untaken": []})["far"].append(float(r["false_alarm_rate"]))
                out["*"]["far"].append(float(r["false_alarm_rate"]))
        except Exception as e:
            print(f"[rec_learning] tracker false-alarm rates unreadable for {restaurant_id}: {e}")
        for k, row in _untaken_rows(conn, restaurant_id):
            out.setdefault(k, {"far": [], "untaken": []})["untaken"].append(row)
    finally:
        conn.close()
    return out


def base_rate_from(inputs, kind) -> dict:
    """base_rate over _base_rate_inputs (see base_rate). Pure."""
    ki = (inputs or {}).get(kind) or {}
    far = ki.get("far") or ((inputs or {}).get("*") or {}).get("far") or []
    if far:
        chance = sum(far) / len(far) / 2.0
        src, basis = "false_alarm", (f"half the {sum(far) / len(far):.0%} false-alarm rate of this restaurant's "
                                     f"{len(far)} measured noise band{'s' if len(far) != 1 else ''}")
    else:
        chance = BASE_RATE_STATED
        src, basis = "stated", "half the stated 10% false-alarm rate of the noise band"
    chance = min(BASE_RATE_BOUNDS[1], max(BASE_RATE_BOUNDS[0], chance))
    kept = _one_untaken_per_window(ki.get("untaken") or [])
    n = len(kept)
    if n >= MIN_MEASURED_FOR_RATE:
        k = sum(1 for v in kept if v[0] == "improved")
        rate = _shrink(k / n, n, chance)
        return {"rate": round(min(BASE_RATE_BOUNDS[1], max(BASE_RATE_BOUNDS[0], rate)), 3), "source": "untaken",
                "n": n, "basis": (f"{k} of {n} improved when this advice was not taken here, shrunk toward "
                                  f"{basis}")}
    return {"rate": round(chance, 3), "source": src, "n": len(far), "basis": basis}


def base_rate(restaurant_id, kind, db_path=DB_PATH) -> dict:
    """The success rate DOING NOTHING gives for this kind at this restaurant
    — the value every success rate here is shrunk toward (re-audit B2 #2,
    #7), and the base rate the confidence engine's Historical Accuracy
    shrinks toward (kind_record `base_rate`):
      {rate, source, n, basis}
    source "untaken": at least MIN_MEASURED_FOR_RATE clear results of the
    kind measured when the advice was NOT taken (outcomes.observe_untaken,
    one per window, same trigger-mirror baseline), shrunk by SHRINK_K toward
    the chance rate; else "false_alarm": half the mean false-alarm rate the
    restaurant's own noise bands were read at (the kind's, else any); else
    "stated": BASE_RATE_STATED. Held to BASE_RATE_BOUNDS. Never raises."""
    try:
        return base_rate_from(_base_rate_inputs(restaurant_id, db_path), kind)
    except Exception as e:
        print(f"[rec_learning] base rate unavailable for {restaurant_id}/{kind}: {e}")
        return {"rate": BASE_RATE_STATED, "source": "stated", "n": 0,
                "basis": "half the stated 10% false-alarm rate of the noise band"}


def _untaken_rows(conn, restaurant_id):
    """[(kind, (verdict, metric, after_start, after_end))] for this
    restaurant's evaluated untaken trackers that count (clear verdict, not
    measured against the trigger window, not confounded)."""
    rows = None
    for extra in (", o.baseline_overlaps_trigger, o.concurrent", ", o.baseline_overlaps_trigger", ""):
        try:
            rows = conn.execute(
                "SELECT o.verdict, o.after_start, o.after_end, o.metric, i.kind, i.key" + extra +
                " FROM recommendation_outcomes o "
                "JOIN rec_instances i ON i.rec_id = substr(o.source_key, ?) AND i.restaurant_id = o.restaurant_id "
                "WHERE o.restaurant_id=? AND o.status='evaluated' AND o.source_key LIKE ?",
                (len("observed:untaken:") + 1, restaurant_id, "observed:untaken:%")).fetchall()
            break
        except Exception as e:
            if not extra:
                print(f"[rec_learning] untaken trackers unreadable for {restaurant_id}: {e}")
                return []
    out = []
    for r in rows or []:
        d = dict(r)
        try:
            overl = bool(int(d.get("baseline_overlaps_trigger") or 0))
        except (TypeError, ValueError):
            overl = False
        if d["verdict"] not in CLEAR_VERDICTS or overl or _confounded(d):
            continue
        out.append((d["kind"] or rec_ledger.kind_of(d["key"]),
                    (d["verdict"], d["metric"], str(d["after_start"] or ""), str(d["after_end"] or ""))))
    return out


def _one_untaken_per_window(rows):
    """One result per window on a number, earliest first — the rule both
    sides of untaken_comparison count by."""
    kept, last = [], {}
    for v in sorted(rows, key=lambda x: (x[2], x[3])):
        m = v[1]
        if m in last and v[2] <= last[m]:
            continue
        kept.append(v)
        last[m] = v[3]
    return kept


def untaken_comparison(restaurant_id, kind, taken_measured=0, taken_improved=0, db_path=DB_PATH) -> dict:
    """What the number did after this kind of recommendation was shown and
    NOT taken (outcomes.observe_untaken's informational trackers, CA2 #11),
    beside what it did when it was taken:
      {measured, improved, rate, taken_measured, taken_improved, taken_rate,
       enough, label}
    `label` ("Compared with when you didn't: …") only when both sides have
    MIN_MEASURED_FOR_RATE clear results. A comparison of what followed, not
    a cause: the owner chose which ones to take. Never raises."""
    out = {"measured": 0, "improved": 0, "rate": None, "taken_measured": int(taken_measured or 0),
           "taken_improved": int(taken_improved or 0), "taken_rate": None, "enough": False, "label": None}
    # The taken side reads through learned_verdict, which never counts a
    # result read against a baseline overlapping its trigger window (CA2 #1)
    # or a confounded one (re-audit B2 #5); the untaken side follows the same
    # rules (_untaken_rows), or the comparison would set clean results
    # against regressions to the mean — and one result per window on a
    # number, as the taken side is counted.
    try:
        conn = get_conn(db_path)
        try:
            rows = _untaken_rows(conn, restaurant_id)
        finally:
            conn.close()
    except Exception as e:
        print(f"[rec_learning] untaken comparison unavailable for {restaurant_id}/{kind}: {e}")
        return out
    kept = _one_untaken_per_window([v for k, v in rows if k == kind])
    out["measured"] = len(kept)
    out["improved"] = sum(1 for v in kept if v[0] == "improved")
    if out["measured"]:
        out["rate"] = round(out["improved"] / out["measured"], 3)
    if out["taken_measured"]:
        out["taken_rate"] = round(out["taken_improved"] / out["taken_measured"], 3)
    out["enough"] = (out["measured"] >= MIN_MEASURED_FOR_RATE and out["taken_measured"] >= MIN_MEASURED_FOR_RATE)
    if out["enough"]:
        out["label"] = (f"Compared with when you didn't: {out['taken_improved']} of {out['taken_measured']} improved "
                        f"after you took this kind of recommendation, {out['improved']} of {out['measured']} after "
                        f"you didn't. What followed, not proof of cause.")
    return out


# ── the check-in ────────────────────────────────────────────────────────────

def episode_for(restaurant_id, key, db_path=DB_PATH):
    """The latest episode of `key` as a row dict, or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND key=? "
                           "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                           (restaurant_id, str(key or "").strip()[:160])).fetchone()
        out = dict(row) if row else None
        if out is not None:
            out["shown"] = bool(conn.execute("SELECT 1 FROM rec_events WHERE rec_id=? AND event='shown' LIMIT 1",
                                             (out["rec_id"],)).fetchone())
    finally:
        conn.close()
    return out


def answerable_episode(viewer, restaurant_id, key, db_path=DB_PATH):
    """The episode a login's answer to `key` belongs to, or None — the check
    every answer route makes before it writes (K2): the key's latest
    episode exists at THIS restaurant, some surface showed it (a
    client-originated answer names something it was shown), and this login
    may see it (viewer_sees: a manager may not answer — and so silence for
    the owner — a loss, food-cost or owner-only recommendation). The routes
    answer None with a 404 that confirms nothing. Never raises."""
    key = str(key or "").strip()[:160]
    if not restaurant_id or not key:
        return None
    try:
        ep = episode_for(restaurant_id, key, db_path=db_path)
    except Exception as e:
        print(f"[rec_learning] answer check failed closed for {restaurant_id} {key}: {e}")
        return None
    if ep is None or not ep.get("shown") or not viewer_sees(viewer, ep):
        return None
    return ep
