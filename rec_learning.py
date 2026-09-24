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
SUMMARY_WINDOWS = (30, 90, 180)
_Z90 = 1.645
CLEAR_VERDICTS = ("improved", "worsened", "no_clear_change")
# outcomes.INFORMATIONAL_PREFIX (a test holds them in step): a tracker that
# measured what followed an alert being READ, not a change.
INFORMATIONAL_PREFIX = "observed:alert_"

# The effectiveness model (see the module docstring).
EFFECT_WINDOW_DAYS = 365
SHRINK_K = 5                   # pseudo-observations pulling a rate to its prior
W_ACCEPT, W_SUCCESS = 0.5, 1.0  # how much a point of each moves the weight
MIN_WEIGHT, MAX_WEIGHT = 0.75, 1.25
FLOOR_WEIGHT = 0.6             # the lowest a worse result can take it
WORSE_KEY_STEP, WORSE_KEY_CAP = 0.20, 0.30     # per worse result on this very key
WORSE_KIND_STEP, WORSE_KIND_CAP = 0.08, 0.20   # per worse result on its kind
WORSE_HALF_LIFE_DAYS = 60
MIN_CALIBRATION_PAIRS = 3      # predicted-vs-measured pairs before dollars adjust
CALIBRATION_BOUNDS = (0.8, 1.2)


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


def _tracker_rows(conn, rid, tids):
    out = {}
    for i in range(0, len(tids), 400):
        chunk = tids[i:i + 400]
        try:
            rows = conn.execute(f"SELECT {_TRACKER_COLS} FROM recommendation_outcomes WHERE restaurant_id=? AND id IN "
                                f"({','.join('?' for _ in chunk)})", (rid, *chunk)).fetchall()
        except Exception as e:
            # A database from before the re-check / check-in columns: the
            # verdict alone, and said so (a silent fallback here once hid
            # every re-check and check-in from the learning).
            print(f"[rec_learning] tracker columns missing, reading verdicts only: {e}")
            rows = conn.execute(f"SELECT id, status, verdict, dollars_monthly, evaluate_on FROM "
                                f"recommendation_outcomes WHERE restaurant_id=? AND id IN "
                                f"({','.join('?' for _ in chunk)})", (rid, *chunk)).fetchall()
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
      * a move that faded or reversed at its re-check → `no_clear_change`:
        not a win (and not a loss — the number went back);
      * anything that is not a clear verdict → `unknown`.
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
    if str(tr.get("source_key") or "").startswith(INFORMATIONAL_PREFIX):
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
    return {"ok": True, "days": days, "since": since_d.strftime("%Y-%m-%d"), "by_module": by_module,
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
        for (start, end), e in sorted(items, key=lambda x: (x[0][0], x[1]["rec_id"])):
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

    def __init__(self, restaurant_id, episodes, cohort=None, db_path=DB_PATH, now=None):
        self.rid = restaurant_id
        self.cohort = cohort
        self.db_path = db_path
        self.now = now or datetime.utcnow()
        self._priors = {}
        self.kinds, self.tags = {}, {}
        self.worse_keys, self.worse_kinds = {}, {}
        self.calibration = {}
        for e in episodes:
            if not e["shown"] or e["state"] in ("superseded", "open", "snoozed"):
                continue
            for bucket, name in ((self.kinds, e["kind"] or rec_ledger.kind_of(e["key"])),
                                 *((self.tags, t) for t in e["tag_list"] if t.startswith(
                                     ("topic:", "focus:", "category:", "dish:", "item:", "daypart:")))):
                s = bucket.setdefault(name, {"taken": 0, "settled": 0, "improved": 0, "measured": 0})
                s["settled"] += 1
                if _taken(e):
                    s["taken"] += 1
                    if e["verdict"] in CLEAR_VERDICTS:
                        s["measured"] += 1
                        s["improved"] += 1 if e["verdict"] == "improved" else 0
            if _taken(e) and e["verdict"] == "worsened":
                age = self._age_days(e["verdict_at"])
                self.worse_keys.setdefault(e["key"], []).append(age)
                self.worse_kinds.setdefault(e["kind"] or rec_ledger.kind_of(e["key"]), []).append(age)
            tr = e.get("tracker") or {}
            if _taken(e) and e.get("dollar_value") and e["verdict"] in CLEAR_VERDICTS:
                realised = float(tr.get("dollars_monthly") or 0.0) if e["verdict"] != "no_clear_change" else 0.0
                if tr.get("dollars_monthly") is not None or e["verdict"] == "no_clear_change":
                    self.calibration.setdefault(e["kind"] or rec_ledger.kind_of(e["key"]), []).append(
                        realised / float(e["dollar_value"]))

    def _age_days(self, at):
        t = rec_ledger._stamp(at)
        if not t:
            return 0.0
        return max(0.0, (self.now - datetime.strptime(t, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400.0)

    def prior(self, kind):
        """(acceptance prior, success prior) for a kind: the cohort's shrunk
        rates when the cohort clears MIN_COHORT (asserted anonymous), else
        even."""
        if kind in self._priors:
            return self._priors[kind]
        acc = suc = 0.5
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
                                                        exclude_restaurant_id=self.rid)
                privacy.assert_anonymous(s)
                if s.get("answered") and privacy.cohort_ok(s.get("answered_restaurants")):
                    acc = float(s.get("acceptance_rate_shrunk") or 0.5)
                if s.get("measured") and privacy.cohort_ok(s.get("measured_restaurants")):
                    suc = float(s.get("success_rate_shrunk") or 0.5)
            except Exception as e:
                print(f"[rec_learning] cohort prior unavailable for {kind}: {e}")
        self._priors[kind] = (acc, suc)
        return acc, suc

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
        cal = self.calibration.get(kind) or []
        if len(cal) >= MIN_CALIBRATION_PAIRS:
            ratio = sorted(cal)[len(cal) // 2]
            ratio = (ratio * len(cal) + 1.0 * 3) / (len(cal) + 3)      # shrunk toward 1
            ratio = min(CALIBRATION_BOUNDS[1], max(CALIBRATION_BOUNDS[0], ratio))
            w *= ratio
            why.append(f"measured dollars run {ratio:.2f}× the estimate")
        w = min(MAX_WEIGHT, max(MIN_WEIGHT, w))
        key_pen = min(WORSE_KEY_CAP, sum(WORSE_KEY_STEP * 0.5 ** (a / WORSE_HALF_LIFE_DAYS)
                                         for a in self.worse_keys.get(key, [])))
        kind_pen = min(WORSE_KIND_CAP, sum(WORSE_KIND_STEP * 0.5 ** (a / WORSE_HALF_LIFE_DAYS)
                                           for a in self.worse_kinds.get(kind, [])))
        if key_pen or kind_pen:
            w *= (1 - key_pen) * (1 - kind_pen)
            why.append("a result got worse after it here" if key_pen else f"a {kind} result got worse here")
        w = round(max(FLOOR_WEIGHT, min(MAX_WEIGHT, w)), 3)
        return w, why

    def __call__(self, key, kind=None, tags=None):
        return self.weight(key, kind=kind, tags=tags)


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
            import intelligence
            cohort = intelligence.cohort_for(restaurant)[0]
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
