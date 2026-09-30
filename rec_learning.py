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

# ── one window rule, with decay (memory audit 9/29/26, PLATFORM-11) ────────
# A hard 365-day window dropped everything the model knew about once-a-year
# advice just as it came back, and counted three years of results the same
# as one. Every learner — the rankers (Effectiveness), Historical Accuracy
# (kind_record → confidence_engine), the cross-restaurant priors (scoring,
# `decay`) and Ask's memory (intelligence.memory) — now reads ONE window,
# DECAY_HORIZON_DAYS, and weighs each episode by decay_weight: an
# exponential half-life per kind (by what it pulls on), same-season
# weighting for a seasonal kind (last December's holiday result counts
# almost fully this December), tapering to nothing at the horizon so no
# result falls off a cliff. The horizon sits inside the ledger's retention
# (ops._RETENTION_DAYS["rec_events"], 800 days — a test holds it): the
# answers behind an older episode are pruned, so it could not be read.
DECAY_HORIZON_DAYS = 730
DECAY_TAPER_DAYS = 120
DEFAULT_HALF_LIFE_DAYS = 365
# Marketing and guest-facing tactics wear out faster than how a restaurant
# staffs, prices or orders (rec_ledger.KIND_TOPIC's topics).
TOPIC_HALF_LIFE_DAYS = {"posting": 180, "marketing": 180, "guest_outreach": 180, "replies": 180, "sales": 180,
                        "competition": 180, "visibility": 180, "guest_experience": 270}
# Advice tied to a time of year: a result from the same season a year back
# weighs SEASONAL_YEAR_WEIGHT (per year); out of season, the kind's decay.
SEASONAL_KINDS = ("holiday_promo",)
SEASON_WINDOW_DAYS = 35
SEASONAL_YEAR_WEIGHT = 0.85
# What the model counts under: bump with any change to how it weighs. It is
# logged with every ranking it moves (rec_ledger's shown meta and
# rec_rank_builds — memory audit 9/29/26, "rank_log"), so "did the model
# raise acceptance?" can be read per version.
# 2: decay instead of the 365-day cutoff, and the prior ladder (9/29/26).
# 3: the owner's reasons as bounded penalties (too costly, doesn't fit),
#    answers read per side (a delegate's never the owner's; an admin's
#    view-as nobody's), and the advice signature's "sig:" bucket (9/29/26).
EFFECTIVENESS_VERSION = 3

# The effectiveness model (see the module docstring). The load window is the
# decay horizon (kept under this name for rec_trust.Context).
EFFECT_WINDOW_DAYS = DECAY_HORIZON_DAYS
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
# The cohort record a prior reads: the same horizon, decayed
# (intelligence.scoring.PRIOR_WINDOW_DAYS; a test holds them in step).
PRIOR_WINDOW_DAYS = DECAY_HORIZON_DAYS
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

# What the owner's reasons teach the ranker (memory audit 9/29/26,
# "reasons"): "too costly" is a bounded, decaying ease penalty on the KIND,
# "doesn't fit us" a bounded, decaying decline weight on the subject's TAGS
# (topic, focus) — so five "too costly" answers no longer leave the costly
# cards ranked first. Each answer takes REASON_STEP off, at most REASON_CAP,
# halving every REASON_HALF_LIFE_DAYS. Only the viewer's own side counts: a
# principal's for the owner's ranking; a delegate's for the delegate's.
REASON_STEP, REASON_CAP = 0.05, 0.15
REASON_HALF_LIFE_DAYS = 90
# The states an episode can be in that count in no rate: replaced, still
# live, snoozed, put off for timing ("bad timing"), or answered only by a
# delegate the owner has not answered (from the owner's side). And (memory
# re-audit 9/29/26):
#   distrusted  "don't trust the data" (silence_rule distrust / verified) —
#               a question about the feed, not a "no" to the advice; every
#               other reader (insight_store, decisions.declined_subjects)
#               already treated it so, while the ranker and the fatigue
#               throttle counted it a rejection (LOOPS-11).
#   reopened    a decline the owner took back ("Use again", restoring a
#               quiet kind — rec_ledger.unsilence's `reopened` event): the
#               owner asked for the advice back, so the decline no longer
#               teaches (QUALITY-1).
#   unseen      went unanswered, but only ever DELIVERED where nobody may
#               have looked — an email that was never opened
#               (rec_ledger.DELIVERY_ONLY_SURFACES) — never on a screen, a
#               push, a text or a click: not "ignored", in no denominator
#               (LOOPS-10).
UNSETTLED_STATES = ("superseded", "open", "snoozed", "deferred", "delegated", "distrusted", "reopened", "unseen")


def _stamp(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def half_life_days(kind, key=None) -> float:
    """The decay half-life of a recommendation kind (by its topic)."""
    try:
        topic = rec_ledger._topic_of(str(kind or ""), str(key or kind or ""))
    except Exception:
        topic = None
    return float(TOPIC_HALF_LIFE_DAYS.get(topic, DEFAULT_HALF_LIFE_DAYS))


def _age(at, now):
    t = rec_ledger._stamp(at)
    if not t:
        return None
    try:
        return max(0.0, (now - datetime.strptime(t, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return None


def decay_weight(kind, at, now=None, key=None) -> float:
    """How much one episode or result of `kind` dated `at` counts now, 0–1
    (module constants above): the kind's half-life; for a SEASONAL_KINDS
    kind within SEASON_WINDOW_DAYS of the same date a year back,
    SEASONAL_YEAR_WEIGHT per year instead; tapered to 0 over the last
    DECAY_TAPER_DAYS before DECAY_HORIZON_DAYS. An undated one counts
    fully (the old window's reading of it). Pure."""
    now = now or datetime.utcnow()
    age = _age(at, now)
    if age is None:
        return 1.0
    if age >= DECAY_HORIZON_DAYS:
        return 0.0
    k = str(kind or "").split(":", 1)[0]
    years = int(round(age / 365.25))
    if k in SEASONAL_KINDS and years >= 1 and abs(age - years * 365.25) <= SEASON_WINDOW_DAYS:
        w = SEASONAL_YEAR_WEIGHT ** years
    else:
        w = 0.5 ** (age / half_life_days(kind, key))
    edge = DECAY_HORIZON_DAYS - DECAY_TAPER_DAYS
    if age > edge:
        w *= max(0.0, (DECAY_HORIZON_DAYS - age) / float(DECAY_TAPER_DAYS))
    return round(w, 6)


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
# The Opportunity Feed's dish_promote rests on plate margins (re-audit OPP-5):
# a login without Food Cost may not see it, nor answer it for the owner.
FOOD_KINDS = ("reprice", "price_spike", "food_cost_driver", "cut_waste", "stock_low", "critical_low", "stock",
              "diag_food", "food_diagnosis", "insight_food", "food_waste", "invoice", "dish_promote")
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

# rec_events columns the readers fold in; `authority` (memory audit
# 9/29/26) says whose answer each is.
_EVENT_COLS = "rec_id, event, surface, meta, at, authority"
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


def _load(conn, rid, since=None, before=None, limit=None, order_desc=False, rec_ids=None, lean=False,
          perspective="principal"):
    """Episodes of this restaurant (bookkeeping keys excluded) with their
    events folded in. `before` is a (created_at, rec_id) cursor.

    `lean` is for the effectiveness model, which reads a year of episodes
    on every Home build: the `shown` rows — most of the trail, one per
    surface per day — are not loaded, only whether each episode has one
    (re-audit B18); `surfaces` is then empty.

    `perspective` is whose answers decide each episode's state (_state):
    "principal" (the owner's view — the default) or "delegate" (a manager's
    own view). An admin's view-as answer decides nothing either way."""
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
    seen_shown = set()
    ids = [r["rec_id"] for r in rows]
    only = ",".join(f"'{x}'" for x in rec_ledger.DELIVERY_ONLY_SURFACES)
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        marks = ",".join("?" for _ in chunk)
        if lean:
            for e in conn.execute(f"SELECT {_EVENT_COLS} FROM rec_events WHERE rec_id IN ({marks}) "
                                  f"AND event != 'shown' ORDER BY at, id", chunk).fetchall():
                evs.setdefault(e["rec_id"], []).append(dict(e))
            for e in conn.execute(f"SELECT rec_id, MAX(CASE WHEN COALESCE(surface, '') NOT IN ({only}) "
                                  f"THEN 1 ELSE 0 END) AS on_screen FROM rec_events WHERE rec_id IN ({marks}) "
                                  f"AND event = 'shown' GROUP BY rec_id", chunk).fetchall():
                shown.add(e["rec_id"])
                if e["on_screen"]:
                    seen_shown.add(e["rec_id"])
        else:
            for e in conn.execute(f"SELECT {_EVENT_COLS} FROM rec_events WHERE rec_id IN "
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
        # Whether anyone could have seen it (LOOPS-10): an episode only an
        # unopened email carried is `unseen`, not ignored (_state).
        if lean:
            emailed_only = r["rec_id"] in shown and r["rec_id"] not in seen_shown
            r["seen"] = not emailed_only or any(e["event"] in rec_ledger.SEEN_EVENTS for e in es)
        else:
            r["seen"] = rec_ledger.episode_seen(es)
        r["surfaces"] = sorted({e["surface"] for e in es if e["event"] == "shown" and e["surface"]})
        r["tag_list"] = rec_ledger.episode_tags(r)
        r["state"] = _state(r, now, perspective)
        r["verdict"], r["verdict_at"] = _verdict(r, es, trackers.get(r.get("tracker_id")))
        r["tracker"] = trackers.get(r.get("tracker_id"))
    return rows


def _meta(e):
    try:
        return json.loads(e.get("meta") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _authority(e) -> str:
    """Whose answer an event is: principal (also every answer from before
    authority was recorded — production's were all a co-owner's), delegate
    or admin."""
    a = (e or {}).get("authority")
    return a if a in ("delegate", "admin") else "principal"


def reopened_after(events, perspective="principal") -> bool:
    """Whether the latest decline in `events` (an episode's, oldest first)
    was taken back afterwards (rec_ledger.unsilence's `reopened`) by the
    same side — the owner's reversal for the owner's decline, a delegate's
    for theirs. An admin's view-as reversal takes nothing back."""
    side = "delegate" if perspective == "delegate" else "principal"
    last_decline = last_reopen = None
    for i, e in enumerate(events or ()):
        if _authority(e) != side:
            continue
        if e["event"] == "dismissed":
            last_decline = (str(e.get("at") or ""), i)
        elif e["event"] == "reopened":
            last_reopen = (str(e.get("at") or ""), i)
    return bool(last_decline and last_reopen and last_reopen > last_decline)


def _delegate_declined(r) -> bool:
    return any(e["event"] in ("dismissed", "snoozed") and _authority(e) == "delegate" for e in r.get("events") or ())


def _state(r, now, perspective="principal"):
    """accepted | completed | implemented | dismissed | ignored | superseded |
    snoozed | deferred | delegated | open — what the episode amounts to now.
    A taken episode whose change was actually made reads implemented
    whichever answer took it (a reprice is answered and applied in one
    step).

    deferred   a "bad timing" answer (silence_rule bad_timing): put off,
               in no denominator (memory audit, "reasons").
    delegated  from the owner's side, an episode only a delegate answered
               (their decline held for them alone): the owner did not
               ignore it — they left it with the manager — so it counts in
               no rate of theirs. From the delegate's side it is their
               dismissal ("who_answered")."""
    st = r["status"]
    if st in rec_ledger.TAKEN_STATUSES and r.get("implemented_at"):
        return "implemented"
    if st == "dismissed" and str(r.get("silence_rule") or "") == "bad_timing":
        return "deferred"
    if st == "dismissed" and str(r.get("silence_rule") or "") in ("distrust", "verified"):
        return "distrusted"
    if st == "dismissed" and reopened_after(r.get("events") or (), perspective):
        return "reopened"
    if st in ("accepted", "completed", "implemented", "dismissed", "superseded"):
        return st
    if st == "expired":
        base = "ignored"
    elif r.get("snoozed_until") and r["snoozed_until"] > _stamp(now):
        base = "snoozed"
    elif rec_ledger.is_stale(r, now=now):
        base = "ignored"
    else:
        base = "open"
    if base in ("ignored", "open") and _delegate_declined(r):
        if perspective == "delegate":
            return "reopened" if reopened_after(r.get("events") or (), "delegate") else "dismissed"
        return "delegated" if base == "ignored" else base
    if base == "ignored" and r.get("seen") is False:
        return "unseen"
    return base


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
    want = {"accepted": ("accepted",), "completed": ("completed", "dismissed"),
            "implemented": ("implemented", "accepted"), "dismissed": ("dismissed",), "deferred": ("dismissed",),
            "snoozed": ("snoozed",)}.get(r["state"])
    if not want:
        return None
    for e in reversed(r["events"]):
        # An admin's view-as answer never decided an episode (view_as).
        if e["event"] in want and _authority(e) != "admin":
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
    reasons when nothing has been learned.

    Every episode inside DECAY_HORIZON_DAYS counts, weighed by
    decay_weight (the `*_w` sums; the plain counts stay for the words), and
    the prior a kind is weighed against comes from the finest rung of the
    prior ladder that clears the floors (prior_rungs; memory audit
    PLATFORM-1): the confirmed concept, then the confirmed partition of the
    kind's metric family, then — a behaviour kind only, and only as a
    ranking weight — every restaurant on Cavnar AI. prior_rung(kind) says
    which rung was read; weight_detail(key) carries it for the shown log.

    `perspective` is whose answers it learns from (memory audit 9/29/26,
    who_answered): "principal" — the owner's own; a manager's decline is
    never the owner's rejection — or "delegate", a manager's view where the
    principal's answer still outranks theirs. The owner's reasons move it
    too: "too costly" and "doesn't fit us" are bounded, decaying penalties
    (reason_penalties)."""

    def __init__(self, restaurant_id, episodes, cohort=None, db_path=DB_PATH, now=None, base_rates=None,
                 profile=None, perspective="principal"):
        self.rid = restaurant_id
        self.cohort = cohort
        self.profile = profile
        self.db_path = db_path
        self.now = now or datetime.utcnow()
        self.perspective = perspective
        self.version = EFFECTIVENESS_VERSION
        self._priors = {}
        self._cold = {}
        self._rungs = {}
        self._base_rates = base_rates
        self.kinds, self.tags = {}, {}
        self.worse_keys, self.worse_kinds = {}, {}
        self.calibration = {}
        self.calibration_realised = {}
        # The owner's reasons (REASON_*): ages of "too costly" answers by
        # kind, of "doesn't fit us" answers by topic / focus tag.
        self.costly_kinds, self.unfit_tags = {}, {}
        sides = ("principal", "delegate") if perspective == "delegate" else ("principal",)
        clear_eps = {}
        for e in episodes:
            for ev in e.get("events") or ():
                if ev["event"] != "dismissed" or _authority(ev) not in sides:
                    continue
                code = _meta(ev).get("reason_code")
                age = self._age_days(ev.get("at"))
                if code == "too_costly":
                    self.costly_kinds.setdefault(e["kind"] or rec_ledger.kind_of(e["key"]), []).append(age)
                elif code == "doesnt_fit":
                    for t in e["tag_list"]:
                        if t.startswith(("topic:", "focus:")):
                            self.unfit_tags.setdefault(t, []).append(age)
            if not e["shown"] or e["state"] in UNSETTLED_STATES:
                continue
            kind = e["kind"] or rec_ledger.kind_of(e["key"])
            w = decay_weight(kind, e.get("closed_at") or e.get("last_event_at") or e.get("created_at"), self.now,
                             key=e.get("key"))
            if w <= 0:
                continue
            for bucket, name in ((self.kinds, kind),
                                 *((self.tags, t) for t in e["tag_list"] if t.startswith(
                                     ("topic:", "focus:", "category:", "dish:", "item:", "daypart:", "sig:")))):
                # `worsened` (a count, like measured and improved) is what
                # held() reads (memory audit 9/29/26, "thresholds").
                s = bucket.setdefault(name, {"taken": 0, "settled": 0, "improved": 0, "measured": 0,
                                             "worsened": 0,
                                             "taken_w": 0.0, "settled_w": 0.0, "improved_w": 0.0, "measured_w": 0.0})
                s["settled"] += 1
                s["settled_w"] += w
                if _taken(e):
                    s["taken"] += 1
                    s["taken_w"] += w
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
            s["worsened"] = sum(1 for e in kept if e["verdict"] == "worsened")
            ws = [(decay_weight(e["kind"] or rec_ledger.kind_of(e["key"]), e.get("verdict_at") or e.get("created_at"),
                                self.now, key=e.get("key")), e) for e in kept]
            s["measured_w"] = sum(w for w, _e in ws)
            s["improved_w"] = sum(w for w, e in ws if e["verdict"] == "improved")
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

    # ── stop proposing and ask (memory audit 9/29/26, "thresholds") ─────────
    kept_kinds = frozenset()

    def held(self, kind):
        """{"kind", "measured", "improved", "worsened", "upper", "harm_low",
        "do_nothing", "why"} when this restaurant's own record says to STOP
        proposing the kind and ask the owner instead — at least
        MIN_MEASURED_FOR_RATE measured results, and either the upper end of
        its 90% interval of success sits below the do-nothing rate, or the
        lower end of its interval of HARM sits above it (it keeps making
        things worse). None otherwise, or when the owner said to keep it
        (kept_kinds, KIND_HOLD_KEEP_DAYS)."""
        ks = self.kinds.get(kind)
        if not ks or ks.get("measured", 0) < MIN_MEASURED_FOR_RATE or kind in self.kept_kinds:
            return None
        n = ks["measured"]
        _lo, up = wilson(ks["improved"], n)
        harm_lo, _hi = wilson(ks.get("worsened", 0), n)
        dn = self.base_rate(kind)
        if up is not None and up < dn:
            why = f"improved {ks['improved']} of {n} measured here — no better than doing nothing"
        elif harm_lo is not None and harm_lo > dn:
            why = f"measured worse {ks.get('worsened', 0)} of {n} times here"
        else:
            return None
        return {"kind": kind, "measured": n, "improved": ks["improved"], "worsened": ks.get("worsened", 0),
                "upper": round(up, 3) if up is not None else None,
                "harm_low": round(harm_lo, 3) if harm_lo is not None else None,
                "do_nothing": round(dn, 3), "why": why}

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
        """(acceptance prior, success prior) for a kind: each from the finest
        rung of prior_rungs whose OWN population clears the floors — the
        acceptance rate over MIN_COHORT answering restaurants from MIN_ORGS
        organisations, the success rate over MIN_COHORT measuring
        restaurants from MIN_ORGS organisations AND PRIOR_MIN_MEASURED
        capped results (asserted anonymous) — its decayed success over the
        capped counts (no one organisation above scoring.MAX_RESTAURANT_SHARE
        of it) shrunk toward this kind's base rate; else even acceptance
        and the BASE RATE for success (re-audit B2 #7: a success prior of
        0.5 ranked a never-measured kind above one that measurably worked).
        The rung each came from is prior_rung(kind)."""
        if kind in self._priors:
            return self._priors[kind]
        acc, suc = 0.5, self.base_rate(kind)
        used = {"acceptance": None, "success": None, "label": None}
        for rung, kw, label in prior_rungs(kind, self.cohort, self.profile):
            if used["acceptance"] and used["success"]:
                break
            try:
                import intelligence
                from intelligence import privacy
                # The group WITHOUT this restaurant's organisation — its own
                # record is weighed against the prior, never counted inside
                # it — and each rate only over the restaurants that
                # contributed to it (re-audit B3).
                s = intelligence.recommendation_success(kind, db_path=self.db_path, exclude_restaurant_id=self.rid,
                                                        window_days=PRIOR_WINDOW_DAYS, decay=True, **kw)
                privacy.assert_anonymous(s)
            except Exception as e:
                print(f"[rec_learning] {rung} prior unavailable for {kind}: {e}")
                continue
            if used["acceptance"] is None and s.get("answered") and s.get("acceptance_available"):
                a = s.get("acceptance_rate_decayed_shrunk")
                acc = float(a if a is not None else (s.get("acceptance_rate_shrunk") or 0.5))
                used["acceptance"] = rung
                used["label"] = used["label"] or label
            capped = float(s.get("measured_capped", s.get("measured")) or 0.0)
            if (used["success"] is None and s.get("measured") and s.get("success_available")
                    and capped >= PRIOR_MIN_MEASURED):
                pm = s.get("measured_decayed")
                pi = s.get("improved_decayed")
                if pm is None:
                    pm, pi = capped, float(s.get("improved_capped", s.get("improved")) or 0.0)
                pm, pi = float(pm or 0.0), float(pi or 0.0)
                suc = _shrink(pi / pm if pm else None, pm, suc)
                used["success"] = rung
                used["label"] = label
        self._priors[kind] = (acc, suc)
        self._rungs[kind] = used
        return acc, suc

    def prior_rung(self, kind) -> dict:
        """{acceptance, success, label, cold} — the prior ladder rung each
        figure of this kind's prior was read from (concept | partition |
        platform, or None: even acceptance, the base rate), and the rung a
        cold-start ranking borrowed from. What the shown log records beside
        the weight (PLATFORM-1/3)."""
        if kind not in self._rungs:
            self.prior(kind)
        out = dict(self._rungs.get(kind) or {})
        cold = self._cold.get(kind)
        out["cold"] = (cold or {}).get("rung")
        out["unlock"] = None if (self.profile or {}).get("confirmed") else "confirm_profile"
        return out

    def cold_prior(self, kind):
        """{weight, restaurants, rate, rung} for a kind this restaurant has
        no record of, from intelligence.scoring.similar_prior over the
        finest rung of prior_rungs that clears the floors (DNA similarity ×
        decay, capped per organisation) — or None. The weight is
        1 + W_SUCCESS × (shrunk rate − this kind's do-nothing rate), held
        to COLD_PRIOR_BOUNDS: peers can nudge the order of a new kind's
        cards, never decide it."""
        if kind in self._cold:
            return self._cold[kind]
        out = None
        from intelligence import scoring as _scoring
        for rung, kw, label in prior_rungs(kind, self.cohort, self.profile):
            try:
                sp = _scoring.similar_prior(kind, self.rid, db_path=self.db_path, now=self.now,
                                            platform=(rung == "platform"), **kw)
            except Exception as e:
                print(f"[rec_learning] similar-restaurant prior unavailable for {kind}: {e}")
                continue
            if sp.get("available") and sp.get("rate") is not None:
                base = self.base_rate(kind)
                rate = _shrink(sp["rate"], sp.get("weighted") or 0.0, base)
                w = 1.0 + W_SUCCESS * (rate - base)
                out = {"weight": round(min(COLD_PRIOR_BOUNDS[1], max(COLD_PRIOR_BOUNDS[0], w)), 3),
                       "restaurants": int(sp["restaurants"]), "rate": round(rate, 3), "rung": rung,
                       "label": label}
                break
        self._cold[kind] = out
        return out

    def _delta(self, s, prior):
        acc_p, suc_p = prior
        tw, sw = s.get("taken_w", s["taken"]), s.get("settled_w", s["settled"])
        iw, mw = s.get("improved_w", s["improved"]), s.get("measured_w", s["measured"])
        acc = _shrink(tw / sw if sw else None, sw, acc_p)
        suc = _shrink(iw / mw if mw else None, mw, suc_p)
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
            # The prior is read only when there is something of this
            # restaurant's own to weigh against it.
            prior = self.prior(kind)
            deltas = [self._delta(s, prior) for s in learned]
        if ks:
            why.append(f"{kind}: acted on {ks['taken']} of {ks['settled']}, {ks['improved']} of {ks['measured']} "
                       f"measured improved")
        w = 1.0 + (sum(deltas) / len(deltas) if deltas else 0.0)
        cold = None
        if not learned:
            # No record of its own: ranked with help from similar
            # restaurants' results (BM3-12, Top-50 #32), bounded to
            # COLD_PRIOR_BOUNDS and said. Ranking only — never a %, and
            # the all-types rung never with a count (PLATFORM-1).
            cold = self.cold_prior(kind)
            if cold is not None:
                w = cold["weight"]
                if cold.get("rung") == "platform":
                    why.append("ranked with help from restaurants of every type on Cavnar AI")
                else:
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
        costly, unfit = self.reason_penalties(kind, tags)
        if costly:
            w *= (1 - costly)
            why.append(f"you said {len(self.costly_kinds.get(kind, []))} like this cost too much")
        if unfit:
            w *= (1 - unfit)
            why.append("you said advice like this doesn't fit you")
        w = round(max(FLOOR_WEIGHT, min(MAX_WEIGHT, w)), 3)
        return w, why

    def reason_penalties(self, kind, tags):
        """(too_costly penalty on the kind, doesn't-fit penalty on its
        topic/focus tags): REASON_STEP per answer, decaying with
        REASON_HALF_LIFE_DAYS, each at most REASON_CAP."""
        def pen(ages):
            return min(REASON_CAP, sum(REASON_STEP * 0.5 ** (a / REASON_HALF_LIFE_DAYS) for a in ages or ()))
        costly = pen(self.costly_kinds.get(kind))
        unfit = max((pen(self.unfit_tags.get(t)) for t in tags or () if t in self.unfit_tags), default=0.0)
        return round(costly, 4), round(unfit, 4)

    def weight_detail(self, key, kind=None, tags=None) -> dict:
        """{weight, why, prior_rung, version} — weight() with the prior
        ladder rung it read and EFFECTIVENESS_VERSION, for the log a shown
        recommendation carries (PLATFORM-1/3)."""
        kind = kind or rec_ledger.kind_of(str(key or ""))
        w, why = self.weight(key, kind=kind, tags=tags)
        return {"weight": w, "why": why, "prior_rung": self.prior_rung(kind), "version": EFFECTIVENESS_VERSION}

    def explain(self, key, kind=None, tags=None, title=None) -> dict:
        """weight_detail for one key — what rank_log stores beside a shown
        card (memory audit 9/29/26) — plus `rung`, the compact form of the
        prior it stood on: "own/<the success rung, or base_rate>" when this
        restaurant has its own record of the kind or its tags,
        "cold/<rung>" when similar restaurants' results ranked it, else
        "none". `title` lets a model line's words name its advice signature
        (its sig: tag), which its hash key cannot."""
        kind = kind or rec_ledger.kind_of(str(key or ""))
        if tags is None:
            tags = rec_ledger.tags_for(str(key or ""), kind=kind)
            if title:
                tags = rec_ledger.with_signature_tag(tags, rec_ledger.signature_for(key, title))
        d = self.weight_detail(key, kind=kind, tags=tags)
        pr = d.get("prior_rung") or {}
        if self.kinds.get(kind) or any(self.tags.get(t) for t in tags):
            rung = f"own/{pr.get('success') or 'base_rate'}"
        elif pr.get("cold"):
            rung = f"cold/{pr['cold']}"
        else:
            rung = "none"
        return {"weight": d["weight"], "why": list((d.get("why") or [])[:3]), "prior_rung": pr, "rung": rung,
                "version": d.get("version")}

    def ceiling(self, learned, kind=None):
        """The highest this weight may reach: 1.0 plus MAX_WEIGHT's headroom,
        scaled by how far the Wilson lower bound of measured success (the
        best of the kind's and its tags' own records, decay-weighted) sits
        above the prior."""
        best = 0.0
        prior = self.prior(kind)[1] if (learned and kind) else 0.5
        for s in learned or []:
            if not s.get("measured"):
                continue
            lo, _ = wilson(s.get("improved_w", s["improved"]), s.get("measured_w", s["measured"]))
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

    def __call__(self, key, kind=None, tags=None, title=None):
        if tags is None and title:
            kind = kind or rec_ledger.kind_of(str(key or ""))
            tags = rec_ledger.with_signature_tag(rec_ledger.tags_for(str(key or ""), kind=kind),
                                                 rec_ledger.signature_for(key, title))
        return self.weight(key, kind=kind, tags=tags)


# The owner's answer to "keep suggesting this kind?" (kind_hold:<kind>): a
# yes (Done / Track) keeps the kind proposed for this long; a "not for us"
# leaves it stopped.
KIND_HOLD_PREFIX = rec_ledger.KIND_HOLD_KIND
# The keep's length lives in rec_ledger, which silences the question for
# exactly as long (answer_silence; memory re-audit 9/29/26, LOOPS-6).
KIND_HOLD_KEEP_DAYS = rec_ledger.KIND_HOLD_KEEP_DAYS


def kept_hold_kinds(restaurant_id, db_path=DB_PATH, now=None) -> set:
    """The kinds the owner said to KEEP proposing despite their record, in
    the last KIND_HOLD_KEEP_DAYS. Never raises."""
    now = now or datetime.utcnow()
    try:
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT key FROM rec_instances WHERE restaurant_id=? AND key LIKE ? "
                "AND status IN ('accepted','completed','implemented') AND last_event_at >= ?",
                (restaurant_id, f"{KIND_HOLD_PREFIX}:%",
                 _stamp(now - timedelta(days=KIND_HOLD_KEEP_DAYS)))).fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f"[rec_learning] kept kinds unreadable for {restaurant_id}: {e}")
        return set()
    return {str(r["key"]).split(":", 1)[1] for r in rows if ":" in str(r["key"])}


def hold_ask(hold) -> dict:
    """The ask Home shows in place of a held kind's cards: key
    kind_hold:<kind>, the question and what it rests on. Done / Track on it
    keeps the kind proposed (KIND_HOLD_KEEP_DAYS); Not for us leaves it
    stopped."""
    kind = hold["kind"]
    label = _kind_label(kind)
    return {"key": f"{KIND_HOLD_PREFIX}:{kind}", "kind": kind,
            "title": f"Keep suggesting {label}?",
            "why": f"{hold['why'][:1].upper()}{hold['why'][1:]} (before and after, not proof). Cavnar AI has "
                   f"stopped suggesting it until you say otherwise.",
            "measured": hold["measured"], "improved": hold["improved"], "worsened": hold["worsened"],
            "do_nothing_pct": int(round(100 * hold["do_nothing"])),
            "answers": {"completed": "Keep suggesting it", "not_for_us": "Stop suggesting it"}}


# ── the prior ladder (memory audit 9/29/26, PLATFORM-1) ─────────────────────
# A prior used to exist only for an owner-confirmed concept: of 40
# restaurants with measured trim_day results, 3 pizzerias left a new
# pizzeria at exactly 1.0, and an owner who never confirmed a type never had
# one however large the platform grew. The ladder mirrors
# categories.partition_ladder: the concept, then the confirmed partition of
# the kind's metric family (finest first — with the bar-led split, then
# without), then every restaurant on Cavnar AI for a BEHAVIOUR kind only
# (scoring.kind_comparability), where a type difference cannot pass as an
# effect. Each rung must clear the floors on its own population; a reader
# takes the finest that does. The platform rung is a ranking weight or a
# prior centre only — never an owner-facing figure (kind_record reads the
# named rungs only).
_PLATFORM_LABEL = "restaurants of every type on Cavnar AI"


def prior_rungs(kind, cohort=None, profile=None, owner_facing=False, key=None) -> list:
    """[(rung, recommendation_success kwargs, group label)] finest first:
    ("concept", {cohort}), ("partition", {partition}) per rung of the
    profile's partition ladder for the kind's family, then ("platform", {})
    for a behaviour kind unless `owner_facing`. [] with no confirmed type,
    no confirmed partition and a kind that is not behaviour."""
    from intelligence import categories as _cats, scoring as _sc
    out = []
    if cohort:
        try:
            from intelligence import benchmarks as _bm
            label = _bm.cohort_label(cohort)
        except Exception:
            label = f"{_cats.label(cohort)} on Cavnar AI"
        out.append(("concept", {"cohort": cohort}, label))
    if profile and profile.get("confirmed"):
        fam = _sc.kind_family(kind, key)
        for pk in _cats.partition_ladder(profile, fam):
            out.append(("partition", {"partition": pk}, _cats.partition_label(pk)))
    if not owner_facing and _sc.kind_comparability(kind, key) == "behaviour":
        out.append(("platform", {}, _PLATFORM_LABEL))
    return out


def learned_note(learned, key, weight, why) -> dict:
    """The `learned` block a ranked recommendation carries (home_brief.
    order_recommendations, business_intelligence.pick_one_thing): the weight
    and its reasons, plus — from an Effectiveness model — the prior ladder
    rung its prior was read from and EFFECTIVENESS_VERSION, so the log of
    what was shown can say what the ranking rested on (memory audit
    PLATFORM-1/3). A plain callable (a test's) gives weight and why only.
    Never raises."""
    out = {"weight": weight, "why": list(why or [])[:3]}
    fn = getattr(learned, "prior_rung", None)
    if callable(fn):
        try:
            out["prior_rung"] = fn(rec_ledger.kind_of(str(key or "")))
            out["version"] = EFFECTIVENESS_VERSION
        except Exception as e:
            print(f"[rec_learning] prior rung unavailable for {key}: {e}")
    return out


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


def effectiveness(restaurant_id, db_path=DB_PATH, restaurant=None, now=None, perspective="principal") -> Effectiveness:
    """The model for one restaurant from its episodes inside
    DECAY_HORIZON_DAYS, each weighed by decay_weight. Never raises: with the
    ledger unreadable it is the neutral model (every weight 1.0).
    `perspective` is whose answers it learns from (_state): "principal" —
    the owner's own (a manager's decline never counts as the owner's
    rejection) — or "delegate", a manager's view, where the principal's
    answer still outranks theirs. An admin's view-as answer teaches neither
    (memory audit 9/29/26, who_answered / view_as). A demo, or an account an
    admin excluded (models.learns_for_itself), ranks on the neutral model; a
    test-named or internal account learns for itself (memory re-audit
    9/29/26, INVENTORY-1) and stays out of pooled learning."""
    now = now or datetime.utcnow()
    cohort, profile = None, None
    try:
        if restaurant is None:
            restaurant = _models_mod.get_restaurant(restaurant_id, db_path=db_path)
        if restaurant is not None:
            # A demo, or an account an admin excluded, learns nothing — its
            # own ranking included (models.learns_for_itself, memory
            # re-audit 9/29/26 INVENTORY-1): its ranking is the neutral model.
            if hasattr(_models_mod, "learns_for_itself") and not _models_mod.learns_for_itself(restaurant):
                return Effectiveness(restaurant_id, [], cohort=None, db_path=db_path, now=now,
                                     perspective=perspective)
            # Only a type and a partition the owner SET: a guess reads no
            # group's record (Benchmarking re-audit R2-7, #14, #20).
            from intelligence import categories as _cats
            cohort = _cats.confirmed_type(restaurant)
            profile = _cats.profile_for(restaurant)
    except Exception as e:
        print(f"[rec_learning] cohort unresolved for {restaurant_id}: {e}")
    try:
        conn = get_conn(db_path)
        try:
            eps = _load(conn, restaurant_id,
                        since=_learning_floor(restaurant, _stamp(now - timedelta(days=EFFECT_WINDOW_DAYS))),
                        lean=True, perspective=perspective)
        finally:
            conn.close()
    except Exception as e:
        print(f"[rec_learning] effectiveness unavailable for {restaurant_id}: {e}")
        eps = []
    model = Effectiveness(restaurant_id, eps, cohort=cohort, db_path=db_path, now=now, profile=profile,
                          perspective=perspective)
    # The kinds the owner said to keep proposing despite their record
    # (kind_hold:<kind> answered Done / Track — memory audit 9/29/26,
    # "thresholds"): held() never stops those.
    model.kept_kinds = frozenset(kept_hold_kinds(restaurant_id, db_path=db_path, now=now))
    return model


def _learning_floor(restaurant, since):
    """The later of `since` and the restaurant's learning_since: a converted
    demo's own model never learns from its demo era (memory audit 9/29/26,
    "eligibility"; models.learning_eligible; M7's patch, applied by the
    integration wave, INT #22)."""
    floor = getattr(restaurant, "learning_since", None) if restaurant is not None else None
    if floor is None and isinstance(restaurant, dict):
        floor = restaurant.get("learning_since")
    if floor and str(floor)[:19] > str(since or "")[:19]:
        return str(floor)[:19]
    return since


def _learning_since_of(restaurant_id, restaurant, db_path):
    """The restaurant's learning_since, from the row a caller passed or one
    light read. None for a restaurant that was never a demo."""
    if restaurant is not None:
        got = getattr(restaurant, "learning_since", None)
        if got is None and isinstance(restaurant, dict):
            got = restaurant.get("learning_since")
        return got
    try:
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT learning_since FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        finally:
            conn.close()
        return r[0] if r else None
    except Exception:
        return None


def weigh(learned, key, title=None) -> dict:
    """{weight, why, prior_rung, rung, version} from any ranker's `learned`
    — the model itself (explain: M8's prior ladder rung and
    EFFECTIVENESS_VERSION too) or a plain callable key -> (weight, why).
    Never raises: a failure is the neutral weight."""
    try:
        if isinstance(learned, Effectiveness):
            return learned.explain(key, title=title)
        w, why = learned(key)
        return {"weight": w, "why": list((why or [])[:3]), "prior_rung": None, "rung": None, "version": None}
    except Exception as e:
        print(f"[rec_learning] weight unavailable for {key}: {e}")
        return {"weight": 1.0, "why": [], "prior_rung": None, "rung": None, "version": None}


def rank_meta(item, base_score, learned_info) -> dict:
    """The compact ranking record rank_log keeps for one candidate (memory
    audit 9/29/26, "rank_log"): the score before and after learning, the
    weight and why, the prior it stood on — `prior_rung` as prior_rung()
    reads it ({acceptance, success, cold}: the same rung learned_note puts
    on the card) and `rung`, its compact form — and EFFECTIVENESS_VERSION."""
    li = learned_info or {}
    pr = li.get("prior_rung") if isinstance(li.get("prior_rung"), dict) else None
    return {"base": round(float(base_score or 0), 2), "score": round(float(item.get("rank_score")
                                                                           or item.get("score") or 0), 2),
            "weight": li.get("weight", 1.0), "why": list(li.get("why") or [])[:2],
            "prior_rung": ({k: pr.get(k) for k in ("acceptance", "success", "cold")} if pr else None),
            "rung": li.get("rung"), "version": li.get("version")}


def perspective_of(user) -> str:
    """The effectiveness perspective for a login: "delegate" for a manager
    or employee, else "principal" (an owner, and an admin viewing the
    owner's ranking)."""
    try:
        from permissions import answer_authority
        return "delegate" if answer_authority(user) == "delegate" else "principal"
    except Exception:
        return "principal"


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

    One window rule (memory audit PLATFORM-11): every episode inside
    DECAY_HORIZON_DAYS; `measured` / `improved` are the counts the owner is
    told, `measured_eff` / `improved_eff` the same results weighed by
    decay_weight, which the Beta read counts (confidence_engine.accuracy).
    The prior's group is the finest NAMED rung of the ladder (prior_rungs,
    owner_facing — the concept, then the confirmed partition; never the
    all-types rung, whose counts are never an owner-facing figure):
    `prior_rung`, and `prior_unlock` "confirm_profile" when the profile is
    unconfirmed (what would unlock a finer group).
    Never raises. `episodes` lets a caller that already loaded the ledger
    (Home) pass it in."""
    kind = str(kind or "")
    now = now or datetime.utcnow()
    out = {"kind": kind, "measured": 0, "improved": 0, "rate": None, "low": None, "high": None,
           "source": "none", "prior_measured": 0, "prior_improved": 0, "prior_restaurants": 0,
           "rate_recent": None, "rate_recent_n_eff": None, "recent_half_life_days": RECENT_HALF_LIFE_DAYS,
           "base_rate": BASE_RATE_STATED, "base_rate_source": "stated", "base_rate_n": 0,
           "base_rate_basis": None, "measured_eff": 0.0, "improved_eff": 0.0, "prior_rung": None,
           "prior_unlock": None, "window_days": DECAY_HORIZON_DAYS}
    try:
        # A converted demo's record starts at its learning_since (INT #22):
        # its demo era is never its own measured record, loaded here or
        # handed in by a caller that loaded the ledger itself.
        since_learning = _learning_since_of(restaurant_id, restaurant, db_path)
        if episodes is None:
            conn = get_conn(db_path)
            try:
                episodes = _load(conn, restaurant_id, lean=True,
                                 since=_learning_floor({"learning_since": since_learning},
                                                       _stamp(now - timedelta(days=EFFECT_WINDOW_DAYS))))
            finally:
                conn.close()
        elif since_learning:
            episodes = [e for e in episodes
                        if not e.get("created_at") or str(e["created_at"])[:19] >= str(since_learning)[:19]]
        mine = [e for e in episodes
                if e.get("shown") and _taken(e)
                and (e.get("kind") or rec_ledger.kind_of(e["key"])) == kind]
        measured = [e for e in _one_per_window(mine) if e["verdict"] in CLEAR_VERDICTS]
        measured = [e for e in measured if decay_weight(kind, e.get("verdict_at") or e.get("created_at"), now,
                                                        key=e.get("key")) > 0]
        out["measured"] = len(measured)
        out["improved"] = sum(1 for e in measured if e["verdict"] == "improved")
        ws = [(decay_weight(kind, e.get("verdict_at") or e.get("created_at"), now, key=e.get("key")), e)
              for e in measured]
        out["measured_eff"] = round(sum(w for w, _e in ws), 3)
        out["improved_eff"] = round(sum(w for w, e in ws if e["verdict"] == "improved"), 3)
    except Exception as e:
        print(f"[rec_learning] kind_record unavailable for {restaurant_id}/{kind}: {e}")
        return out
    # A diagnosis's or a nightly report's own claim, scored at its horizon
    # (ai_reads.score_due, memory audit 9/29/26 "claims"): advice that was
    # TAKEN and then measured over its own window — never one whose advice
    # was tracked (its tracker is the result above) or never taken
    # (untested, not a failure). The verdicts feed Historical Accuracy as
    # the code always promised; `claims` carries their own counts.
    claims = _claim_results(restaurant_id, kind, db_path)
    if claims:
        out["claims"] = {k: claims[k] for k in ("measured", "improved", "worsened", "no_clear_change",
                                                 "untested", "unmeasurable")}
        out["measured"] += claims["measured"]
        out["improved"] += claims["improved"]
        measured = list(measured) + list(claims.get("results") or [])
    # Pooled by lever (memory audit 9/29/26, "positive_volume"): below its
    # own floor a kind borrows the measured results of every kind that pulls
    # the same lever here (the topic tag — trim_day, a nightly report's
    # adjust_staffing and a labor read's cut are all "staffing"), so one
    # kind's five results no longer have to come from that kind alone. The
    # confidence engine reads it with the same shrinkage toward doing
    # nothing, and says it is pooled.
    if out["measured"] < MIN_MEASURED_FOR_RATE:
        pool = _lever_pool(episodes, kind)
        if pool and pool["measured"] >= MIN_MEASURED_FOR_RATE:
            out["pooled"] = pool
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
        profile = _cats.profile_for(restaurant) if restaurant is not None else None
        if not (profile or {}).get("confirmed"):
            out["prior_unlock"] = "confirm_profile"
        for rung, kw, label in prior_rungs(kind, cohort, profile, owner_facing=True):
            s = intelligence.recommendation_success(kind, db_path=db_path, exclude_restaurant_id=restaurant_id,
                                                    window_days=PRIOR_WINDOW_DAYS, **kw)
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
                           prior_restaurants=int(s.get("measured_restaurants") or 0), prior_rung=rung)
                # The label of the group ACTUALLY read (NS4 H4): never "like
                # yours" when it is a wider one.
                out["prior_label"] = label or "other restaurants on Cavnar AI"
                if not own:
                    out.update(rate=pi / pm, source="cohort")
                    out["low"], out["high"] = wilson(pi, pm)
                break
    except Exception as e:
        print(f"[rec_learning] cohort record unavailable for {kind}: {e}")
    return out


def _lever_pool(episodes, kind):
    """{"topic", "label", "measured", "improved", "kinds"}: the measured
    results of every kind sharing `kind`'s lever (its commonest topic tag
    here, else the ledger's topic for the kind), taken and shown, one per
    change (_one_per_window) — or None with no lever. Pure over the
    episodes it is handed."""
    mine = [e for e in episodes or [] if (e.get("kind") or rec_ledger.kind_of(e.get("key"))) == kind]
    counts = {}
    for e in mine:
        for t in e.get("tag_list") or []:
            if str(t).startswith("topic:"):
                counts[t] = counts.get(t, 0) + 1
    topic = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if counts else None
    if topic is None:
        t = rec_ledger._topic_of(kind, f"{kind}:")
        topic = f"topic:{t}" if t else None
    if topic is None:
        return None
    pool = [e for e in episodes or [] if e.get("shown") and _taken(e) and topic in (e.get("tag_list") or [])]
    measured = [e for e in _one_per_window(pool) if e.get("verdict") in CLEAR_VERDICTS]
    return {"topic": topic.split(":", 1)[1], "label": rec_ledger.tag_label(topic).lower(),
            "measured": len(measured), "improved": sum(1 for e in measured if e["verdict"] == "improved"),
            "kinds": sorted({e.get("kind") or rec_ledger.kind_of(e.get("key")) for e in measured})}


def _claim_results(restaurant_id, kind, db_path=DB_PATH):
    """ai_reads.claims_record for a kind that makes claims (ai_reads.
    CLAIM_KINDS), or None. Never raises."""
    try:
        import ai_reads
        if kind not in ai_reads.CLAIM_KINDS:
            return None
        rec = ai_reads.claims_record(restaurant_id, kind, days=EFFECT_WINDOW_DAYS,
                                     db_path=None if db_path == DB_PATH else db_path)
        return rec if (rec.get("measured") or rec.get("untested") or rec.get("unmeasurable")) else None
    except Exception as e:
        print(f"[rec_learning] claim record unavailable for {restaurant_id}/{kind}: {e}")
        return None


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
                "n": n, "basis": (f"{k} of {n} improved when this advice was not acted on here, shrunk toward "
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


# ── what has worked HERE (memory audit 9/29/26, "what_worked") ──────────────
#
# The effectiveness model only ever reordered cards; no text generator knew
# that two measured trims of Tuesday dinner improved labor % and coincided
# with a rating drop, or that the owner had ignored a kind of advice six
# times. what_worked(rid) is the per-restaurant record by kind and by
# subject tag — acceptance, measured results (one per change) and how often
# it was left unanswered — and what_worked_lines serves it to every model
# call through memory_context. The nightly learning pass snapshots it by
# month (rec_learning_summaries, kept forever), so how a restaurant's record
# moved is never lost to the 365-day window.

WORKED_TAG_PREFIXES = ("topic:", "focus:", "category:", "dish:", "item:", "daypart:", "day:")
IGNORED_LINE_MIN = 3            # left unanswered this often, never taken → said
WORKED_LINES_MAX = 6
# The modules whose kinds and the lever topics whose tags each surface reads.
SURFACE_SCOPE = {
    "labor_read": (("labor", "schedule"), ("staffing", "hours", "overtime")),
    "schedule": (("labor", "schedule"), ("staffing", "hours", "overtime")),
    "food_read": (("food",), ("waste", "ordering", "purchasing", "pricing", "food_cost")),
    "food_diagnosis": (("food",), ("waste", "ordering", "purchasing", "pricing", "food_cost")),
    "review_read": (("reviews",), ("guest_experience", "replies")),
    "review_diagnosis": (("reviews",), ("guest_experience", "replies", "staffing")),
    "marketing": (("marketing", "guests"), ("posting", "marketing", "guest_outreach", "sales")),
}


def init_rec_learning(db_path: str = DB_PATH):
    """Boot DDL (models.init_db): the monthly what-worked snapshots."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS rec_learning_summaries (
            restaurant_id   INTEGER NOT NULL,
            month           TEXT    NOT NULL,
            scope           TEXT    NOT NULL,
            name            TEXT    NOT NULL,
            module          TEXT,
            shown           INTEGER NOT NULL DEFAULT 0,
            settled         INTEGER NOT NULL DEFAULT 0,
            taken           INTEGER NOT NULL DEFAULT 0,
            dismissed       INTEGER NOT NULL DEFAULT 0,
            ignored         INTEGER NOT NULL DEFAULT 0,
            measured        INTEGER NOT NULL DEFAULT 0,
            improved        INTEGER NOT NULL DEFAULT 0,
            worsened        INTEGER NOT NULL DEFAULT 0,
            no_clear_change INTEGER NOT NULL DEFAULT 0,
            computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, month, scope, name)
        )""")
        conn.commit()
    finally:
        conn.close()


def _worked_bucket():
    return {"shown": 0, "settled": 0, "taken": 0, "dismissed": 0, "ignored": 0, "measured": 0, "improved": 0,
            "worsened": 0, "no_clear_change": 0, "modules": {}}


def what_worked(restaurant_id, db_path=DB_PATH, now=None, episodes=None) -> dict:
    """{"kinds": {kind: stats}, "tags": {tag: stats}} over the last
    EFFECT_WINDOW_DAYS — stats {shown, settled, taken, dismissed, ignored,
    measured, improved, worsened, no_clear_change, module}. Counted the
    way kind_record and the owner's record count: shown episodes, read
    through learned_verdict, one result per change (_one_per_window).
    Never raises."""
    now = now or datetime.utcnow()
    out = {"kinds": {}, "tags": {}}
    try:
        if episodes is None:
            conn = get_conn(db_path)
            try:
                episodes = _load(conn, restaurant_id, since=_stamp(now - timedelta(days=EFFECT_WINDOW_DAYS)),
                                 lean=True)
            finally:
                conn.close()
    except Exception as e:
        print(f"[rec_learning] what-worked record unavailable for {restaurant_id}: {e}")
        return out
    groups = {}
    for e in episodes or []:
        if not e.get("shown") or e.get("state") == "superseded":
            continue
        kind = e.get("kind") or rec_ledger.kind_of(e["key"])
        names = [("kinds", kind)] + [("tags", t) for t in e.get("tag_list") or []
                                     if str(t).startswith(WORKED_TAG_PREFIXES)]
        for scope, name in names:
            b = out[scope].setdefault(name, _worked_bucket())
            b["shown"] += 1
            m = e.get("module") or "home"
            b["modules"][m] = b["modules"].get(m, 0) + 1
            st = e.get("state")
            if _settled(e):
                b["settled"] += 1
            if _taken(e):
                b["taken"] += 1
                if e.get("verdict"):
                    groups.setdefault((scope, name), []).append(e)
            elif st == "dismissed":
                b["dismissed"] += 1
            elif st == "ignored":
                b["ignored"] += 1
    for (scope, name), eps in groups.items():
        b = out[scope][name]
        for e in _one_per_window(eps):
            if e["verdict"] in CLEAR_VERDICTS:
                b["measured"] += 1
                b[e["verdict"]] += 1
    for scope in ("kinds", "tags"):
        for b in out[scope].values():
            mods = b.pop("modules")
            b["module"] = sorted(mods.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if mods else None
    return out


def snapshot_what_worked(restaurant_id, db_path=DB_PATH, now=None) -> int:
    """Write this month's what-worked rows (rec_learning_summaries), one per
    kind and tag, replacing earlier writes of the same month — the nightly
    learning pass. A past month's rows are never touched again: the history
    of the record is kept forever. Returns rows written. Never raises."""
    now = now or datetime.utcnow()
    month = now.strftime("%Y-%m")
    rec = what_worked(restaurant_id, db_path=db_path, now=now)
    rows = [(restaurant_id, month, scope[:-1], name, b.get("module"), b["shown"], b["settled"], b["taken"],
             b["dismissed"], b["ignored"], b["measured"], b["improved"], b["worsened"], b["no_clear_change"])
            for scope in ("kinds", "tags") for name, b in rec[scope].items()]
    try:
        conn = get_conn(db_path)
    except Exception:
        return 0
    try:
        conn.execute("DELETE FROM rec_learning_summaries WHERE restaurant_id=? AND month=?", (restaurant_id, month))
        conn.executemany(
            "INSERT INTO rec_learning_summaries (restaurant_id, month, scope, name, module, shown, settled, taken, "
            "dismissed, ignored, measured, improved, worsened, no_clear_change) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        conn.commit()
        return len(rows)
    except Exception as e:
        print(f"[rec_learning] what-worked snapshot failed for {restaurant_id}: {e}")
        return 0
    finally:
        conn.close()


def _stored_what_worked(restaurant_id, db_path=DB_PATH):
    """The latest month's snapshot as what_worked's shape, or None."""
    try:
        conn = get_conn(db_path)
    except Exception:
        return None
    try:
        row = conn.execute("SELECT MAX(month) AS m FROM rec_learning_summaries WHERE restaurant_id=?",
                           (restaurant_id,)).fetchone()
        if not row or not row["m"]:
            return None
        out = {"kinds": {}, "tags": {}}
        for r in conn.execute("SELECT * FROM rec_learning_summaries WHERE restaurant_id=? AND month=?",
                              (restaurant_id, row["m"])).fetchall():
            d = dict(r)
            out["kinds" if d["scope"] == "kind" else "tags"][d["name"]] = d
        return out
    except Exception:
        return None
    finally:
        conn.close()


def _kind_label(kind) -> str:
    k = str(kind or "")
    if k.startswith("insight_"):
        return f"the {k[len('insight_'):]} read's suggestions"
    if k.startswith("diag_"):
        return f"the {k[len('diag_'):]} diagnosis's action"
    return k.replace("_", " ")


def _worked_sentence(label, b) -> str:
    """One line of the record: taken of settled, the measured results when
    they clear the floor, how often it was left unanswered."""
    bits = []
    if b.get("settled"):
        bits.append(f"taken {b['taken']} of {b['settled']} times it was answered or left")
    if b.get("measured", 0) >= MIN_MEASURED_FOR_RATE:
        bits.append(f"improved {b['improved']} of {b['measured']} measured results"
                    + (f", {b['worsened']} worse" if b.get("worsened") else "")
                    + " (before and after, not proof)")
    elif b.get("measured"):
        bits.append(f"{b['measured']} measured so far — too few to say")
    if b.get("ignored", 0) >= IGNORED_LINE_MIN and not b.get("taken"):
        bits.append(f"ignored {b['ignored']} times and never taken")
    return f"{label}: " + "; ".join(bits) if bits else ""


def what_worked_lines(req):
    """memory_context provider: "WHAT HAS WORKED HERE" — per kind and per
    subject tag of this restaurant's advice: how often it was taken, how
    often a taken one measurably improved ("k of n (before and after)",
    only at MIN_MEASURED_FOR_RATE results), and how often it was ignored.
    Scoped to the surface's modules and levers (SURFACE_SCOPE); with
    subjects ("day:friday", "labor:day:friday", "category:service"), the
    tags that match them first. Computed by Cavnar AI: trusted lines.
    Reads the nightly snapshot, else computes it."""
    rid = getattr(req, "restaurant_id", None)
    if not rid:
        return []
    db_path = getattr(req, "db_path", None) or DB_PATH
    rec = _stored_what_worked(rid, db_path=db_path) or what_worked(rid, db_path=db_path)
    modules, topics = SURFACE_SCOPE.get(getattr(req, "surface", None), (None, None))
    subjects = [str(s).lower() for s in (getattr(req, "subjects", None) or ()) if s]
    wanted_tags = set()
    for s in subjects:
        parts = s.split(":")
        for i in range(len(parts) - 1):
            wanted_tags.add(f"{parts[i]}:{parts[i + 1]}")
    cand = []
    for kind, b in (rec.get("kinds") or {}).items():
        if modules and (b.get("module") or "home") not in modules:
            continue
        cand.append(("kind", kind, b))
    for tag, b in (rec.get("tags") or {}).items():
        head, _, val = tag.partition(":")
        if topics and head == "topic" and val not in topics:
            continue
        if topics and head == "focus" and not any(val.endswith("_" + t) for t in topics):
            continue
        if modules and head not in ("topic", "focus") and (b.get("module") or "home") not in modules \
                and tag not in wanted_tags:
            continue
        cand.append(("tag", tag, b))
    lines = []
    for scope, name, b in cand:
        informative = (b.get("measured", 0) >= MIN_MEASURED_FOR_RATE
                       or (b.get("ignored", 0) >= IGNORED_LINE_MIN and not b.get("taken"))
                       or b.get("measured"))
        if not informative:
            continue
        label = rec_ledger.tag_label(name) if scope == "tag" else _kind_label(name)
        text = _worked_sentence(label, b)
        if not text:
            continue
        weight = (3.0 if name in wanted_tags else 0.0) + (2.0 if b.get("measured", 0) >= MIN_MEASURED_FOR_RATE
                                                          else 1.0 if b.get("ignored", 0) >= IGNORED_LINE_MIN
                                                          else 0.5) + min(1.0, b.get("measured", 0) / 20.0)
        line = {"text": text, "date": None, "source": "system", "subject": name, "weight": weight,
                "trusted": True}
        if b.get("module") and b["module"] != "home":
            # Scoped to the logins who may see that module (the assembler's
            # viewer check): a manager without Food Cost never reads what
            # food advice did here.
            line["module"] = b["module"]
        lines.append(line)
    lines.sort(key=lambda ln: -ln["weight"])
    return lines[:WORKED_LINES_MAX]


# Advice that takes hours or people OUT of a day. Adding coverage is also
# "staffing", and its result says nothing about a trim.
_CUT_KINDS = ("trim_day", "pulse_cut", "labor_over")
_CUT_WORDS = None


def _is_cut(e) -> bool:
    global _CUT_WORDS
    import re
    if _CUT_WORDS is None:
        _CUT_WORDS = re.compile(r"\b(cut|cuts|trim\w*|reduc\w*|fewer|drop|shorten\w*|send\s+\w+\s+home|"
                                r"take\s+\w+\s+off)\b", re.I)
    kind = e.get("kind") or rec_ledger.kind_of(e.get("key"))
    if kind in _CUT_KINDS:
        return True
    if kind == "dsr_action" and ":control_hours:" in f"{e.get('key')}:":
        return True
    return bool(_CUT_WORDS.search(str(e.get("title") or "")))


def worsened_levers(restaurant_id, db_path=DB_PATH, now=None, episodes=None) -> dict:
    """{weekday: {"worsened", "measured", "label"}} — weekdays where advice
    that CUT staffing or hours (topic staffing / hours, a day tag) was taken
    and measured worse at least once, one result per change. What the
    schedule optimizer reads to hold back a trim of the same day (memory
    audit 9/29/26, "what_worked"). Never raises."""
    now = now or datetime.utcnow()
    try:
        if episodes is None:
            conn = get_conn(db_path)
            try:
                episodes = _load(conn, restaurant_id, since=_stamp(now - timedelta(days=EFFECT_WINDOW_DAYS)),
                                 lean=True)
            finally:
                conn.close()
    except Exception as e:
        print(f"[rec_learning] worsened levers unavailable for {restaurant_id}: {e}")
        return {}
    by_day = {}
    for e in episodes or []:
        if not (e.get("shown") and _taken(e) and e.get("verdict")):
            continue
        tags = e.get("tag_list") or []
        if not any(t in ("topic:staffing", "topic:hours") for t in tags) or not _is_cut(e):
            continue
        for t in tags:
            if t.startswith("day:"):
                by_day.setdefault(t[4:], []).append(e)
    out = {}
    for day, eps in by_day.items():
        kept = [e for e in _one_per_window(eps) if e["verdict"] in CLEAR_VERDICTS]
        worse = sum(1 for e in kept if e["verdict"] == "worsened")
        if worse:
            out[day] = {"worsened": worse, "measured": len(kept),
                        "label": f"staffing cuts on {day.capitalize()}s"}
    return out
