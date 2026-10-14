"""
schedule_calibration.py — the Shift Quality weights calibrated where nobody
presses Apply (schedule audit 10/10/26 LEARNING-9).

schedule_learning.calibrate_weights (v2: as-run dimensions, weekday ×
daypart and demand fixed effects, per-outcome fits combined in dollars,
held-out weeks, a corrected threshold search) only ever SUGGESTED, and
across thousands of restaurants almost no owner reads a ridge regression
and presses Apply: the score that drives the solver, the optimizer and the
trim stays at its defaults everywhere.

Now, weekly (strategy_jobs.run_quality_calibration):

  cohort_evidence()      every restaurant that may teach and has a ready
                         calibration, pooled (each restaurant's own weekday ×
                         daypart cells as fixed effects) — the evidence per
                         dimension, kept only with MIN_COHORT restaurants or
                         more and as anonymous ratios (intelligence.privacy)
  shadow_run(rid)        this restaurant's evidence shrunk toward the
                         cohort's (COHORT_PRIOR_SHIFTS of pseudo-shifts), the
                         bounded next weights from it (one CALIBRATION_MAX_STEP
                         step, within CALIBRATION_MAX_NUDGE of the default), run
                         as a SHADOW score on the restaurant's newest held-out
                         weeks against the weights now — and PROMOTED, within
                         those bounds, only when it tracks what the shifts
                         were worth better in SHADOW_MIN_CONFIDENCE of the
                         bootstrap resamples; recorded either way
                         (schedule_calibration_runs)
  revert(rid)            the owner's undo: the weights before the promotion
                         back, and no promotion for REVERT_QUIET_DAYS

The owner is told what changed (the labor intel payload's
weight_calibration.promoted, with Undo) — never a silent change. A
restaurant that does not learn for itself (a demo, an admin's exclusion)
is never promoted. Auto-promotion is OFF unless SCHEDULE_CALIBRATION_AUTOPROMOTE=1 (10/11/26); off, every run is a
shadow.
"""
import json
import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# A shadow is promoted when it tracks the held-out shifts' dollar outcomes
# better in at least SHADOW_MIN_CONFIDENCE of BOOTSTRAP_TRIES resamples.
SHADOW_MIN_CONFIDENCE = 0.9
BOOTSTRAP_TRIES = 200
COHORT_PRIOR_SHIFTS = 40
REVERT_QUIET_DAYS = 56
AUTOPROMOTE_ENV = "SCHEDULE_CALIBRATION_AUTOPROMOTE"
COHORT_MAX_RESTAURANTS = 300
RUNS_KEEP_DAYS = 400


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_schedule_calibration(db_path=None):
    """Boot DDL (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_calibration_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            run_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            status          TEXT    NOT NULL,
            before_json     TEXT,
            candidate_json  TEXT,
            holdout_json    TEXT,
            cohort_json     TEXT,
            reason          TEXT,
            reverted_by     TEXT,
            reverted_at     TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_cal_runs ON schedule_calibration_runs(restaurant_id, run_at)")
        # The retention sweep deletes by run_at alone (ops._RETENTION_DAYS),
        # which the (restaurant_id, run_at) index cannot serve.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_cal_runs_at ON schedule_calibration_runs(run_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schedule_calibration_cohort (
            week           TEXT PRIMARY KEY,
            restaurants    INTEGER NOT NULL,
            shifts         INTEGER NOT NULL,
            evidence_json  TEXT    NOT NULL,
            computed_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.commit()
    finally:
        conn.close()


def _autopromote() -> bool:
    # OFF unless set (blind re-audit 10/11/26: the shadow run's hold-out
    # reused weeks it was fitted on, so noise promoted; held off until the
    # split is honest). Set SCHEDULE_CALIBRATION_AUTOPROMOTE=1 to turn it on.
    return (os.environ.get(AUTOPROMOTE_ENV) or "0").strip().lower() in ("1", "on", "true", "yes")


def _evidence(samples, keys, usd) -> dict:
    """{dimension: (evidence, shifts)} — calibrate_weights' v2 arithmetic over
    `samples`: each outcome fitted on its own, combined in dollars."""
    import schedule_learning as sl
    fits = {o: sl._ridge_fit(samples, keys, o, sl.CALIBRATION_MIN_PAIRS)[0] for o in sl.CALIBRATION_OUTCOMES}
    scale = sl._outcome_scale(samples, usd)
    out = {}
    for key in keys:
        contrib = {o: sl.CALIBRATION_OUTCOMES[o][0] * fits[o][key] for o in sl.CALIBRATION_OUTCOMES if key in fits[o]}
        norm = sum(scale.get(o, 0.0) for o in contrib)
        if contrib and norm > 0:
            out[key] = (sum(c * scale.get(o, 0.0) for o, c in contrib.items()) / norm,
                        sum(1 for s in samples if key in s[0]))
    return out


def cohort_evidence(db_path=None, today=None, store=True) -> dict:
    """The pooled evidence per dimension across every Labor restaurant that
    may teach (intelligence.jobs.excluded_learning_ids) with a ready
    calibration — each restaurant's own weekday × daypart cells as fixed
    effects — or {} below intelligence.privacy.MIN_COHORT restaurants. Kept
    weekly in schedule_calibration_cohort as anonymous ratios."""
    import models
    import schedule_learning as sl
    from shift_quality import DEFAULT_WEIGHTS
    from intelligence import privacy
    try:
        from intelligence.jobs import excluded_learning_ids
        excluded = set(excluded_learning_ids(db_path=db_path) if db_path else excluded_learning_ids())
    except Exception as e:
        log.warning("calibration cohort: eligibility unreadable: %s", e)
        return {}
    pooled, n = [], 0
    rows = models.get_all_restaurants(db_path) if db_path else models.get_all_restaurants()
    for r in rows:
        if n >= COHORT_MAX_RESTAURANTS:
            break
        if r.id in excluded or not getattr(r, "module_labor", 0):
            continue
        try:
            samples, _w = sl._calibration_samples(r.id, db_path or sl.DB_PATH)
        except Exception as e:
            log.warning("calibration cohort: samples unreadable for a restaurant: %s", e)
            continue
        if len({s[2] for s in samples}) < sl.CALIBRATION_MIN_WEEKS or len(samples) < sl.CALIBRATION_MIN_SHIFTS:
            continue
        n += 1
        tag = f"r{n}"                       # a cell per restaurant, never its id
        pooled += [(d, oc, f"{tag}:{h}", dict(m or {}, cell=f"{tag}|{(m or {}).get('cell') or ''}"))
                   for d, oc, h, m in samples]
    if n < privacy.MIN_COHORT:
        return {}
    usd = sl._dollar_weights(pooled)
    ev = _evidence(pooled, list(DEFAULT_WEIGHTS), usd)
    out = privacy.assert_anonymous({"restaurants": n, "shifts": len(pooled),
                                    "evidence": {k: round(v, 4) for k, (v, _n) in ev.items()}})
    if store:
        try:
            y, w, _ = (today or datetime.utcnow().date()).isocalendar()
            conn = get_conn(db_path)
            try:
                conn.execute("INSERT INTO schedule_calibration_cohort (week, restaurants, shifts, evidence_json) "
                             "VALUES (?,?,?,?) ON CONFLICT(week) DO UPDATE SET restaurants=excluded.restaurants, "
                             "shifts=excluded.shifts, evidence_json=excluded.evidence_json, computed_at=datetime('now')",
                             (f"{y}-W{w:02d}", n, len(pooled), json.dumps(out["evidence"])))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning("calibration cohort not kept: %s", e)
    return out


def _bounded(evidence, current) -> dict:
    """The next weights from evidence: calibrate_weights' bounds (the
    evidence floor, CALIBRATION_MAX_NUDGE of the default at most, one
    CALIBRATION_MAX_STEP step)."""
    import schedule_learning as sl
    from shift_quality import DEFAULT_WEIGHTS
    out = {}
    for key, default in DEFAULT_WEIGHTS.items():
        ev = evidence.get(key)
        nudge = max(-sl.CALIBRATION_MAX_NUDGE, min(sl.CALIBRATION_MAX_NUDGE, ev)) \
            if (ev is not None and abs(ev) >= sl.CALIBRATION_MIN_EVIDENCE) else 0.0
        now = float(current.get(key, default))
        step = sl.CALIBRATION_MAX_STEP * default
        out[key] = round(now + max(-step, min(step, default * (1 + nudge) - now)), 1)
    return out


def _holdout_compare(samples, current, candidate, usd) -> dict:
    """{current, candidate, gain, shifts}: the held-out correlation (newest
    weeks, within each weekday and daypart) of the score under each set of
    weights with what the shifts were worth in dollars."""
    import schedule_learning as sl
    weeks = sorted({s[2] for s in samples})
    k = max(1, int(round(len(weeks) * sl.CALIBRATION_HOLDOUT_SHARE)))
    held = set(weeks[-k:])
    vals, a_, b_, groups = [], [], [], []
    for s in samples:
        if s[2] not in held:
            continue
        v = [sl.CALIBRATION_OUTCOMES[o][0] * s[1][o] * float(usd.get(o) or 0) for o in sl.CALIBRATION_OUTCOMES
             if s[1].get(o) is not None]

        def comp(w):
            den = sum(float(w.get(k_, 0)) for k_ in s[0] if k_ in w)
            return sum(float(w.get(k_, 0)) * x for k_, x in s[0].items() if k_ in w) / den if den else None
        x, y = comp(current), comp(candidate)
        if v and x is not None and y is not None:
            vals.append(sum(v))
            a_.append(x)
            b_.append(y)
            groups.append(sl._fe_group(s))
    out = {"weeks": k, "shifts": len(vals), "current": None, "candidate": None, "gain": None, "p_better": None}
    if len(vals) < 10:
        return out
    yv = sl._within(vals, groups)
    xa, xb = sl._within(a_, groups), sl._within(b_, groups)
    rc = sl._pearson(list(zip(xa, yv)))
    rn = sl._pearson(list(zip(xb, yv)))
    if rc is None or rn is None:
        return out
    # How sure the candidate is better: the share of bootstrap resamples of
    # the held-out shifts in which it tracks the outcomes better (paired,
    # seeded — deterministic; schedule_learning.paired_better).
    out.update(current=round(rc, 4), candidate=round(rn, 4), gain=round(rn - rc, 4),
               p_better=sl.paired_better(xa, xb, yv, tries=BOOTSTRAP_TRIES))
    return out


def _record(restaurant_id, status, before, candidate, holdout, cohort, reason, db_path=None) -> int:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO schedule_calibration_runs (restaurant_id, status, before_json, candidate_json, "
                           "holdout_json, cohort_json, reason) VALUES (?,?,?,?,?,?,?)",
                           (restaurant_id, status, json.dumps(before), json.dumps(candidate), json.dumps(holdout),
                            json.dumps(cohort) if cohort else None, (reason or "")[:300]))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _quiet(restaurant_id, db_path=None) -> bool:
    """An owner reverted a promotion within REVERT_QUIET_DAYS."""
    since = (datetime.utcnow() - timedelta(days=REVERT_QUIET_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT 1 FROM schedule_calibration_runs WHERE restaurant_id=? AND status='reverted' AND "
                         "reverted_at >= ? LIMIT 1", (restaurant_id, since)).fetchone()
    finally:
        conn.close()
    return r is not None


def shadow_run(restaurant_id, cohort=None, db_path=None) -> dict:
    """One restaurant's weekly calibration run (module doc). Returns {status
    (not_ready | shadow | held | promoted), before, candidate, holdout,
    reason}."""
    import models
    import schedule_learning as sl
    from shift_quality import DEFAULT_WEIGHTS
    samples, _w = sl._calibration_samples(restaurant_id, db_path or sl.DB_PATH)
    if len({s[2] for s in samples}) < sl.CALIBRATION_MIN_WEEKS or len(samples) < sl.CALIBRATION_MIN_SHIFTS:
        return {"status": "not_ready"}
    stored = models.get_quality_weights(restaurant_id) or {}
    current = dict(DEFAULT_WEIGHTS)
    current.update({k: float(v) for k, v in stored.items() if k in DEFAULT_WEIGHTS})
    usd = sl._dollar_weights(samples)
    own = _evidence(samples, list(DEFAULT_WEIGHTS), usd)
    # Shrunk toward the cohort: a restaurant with few shifts borrows the
    # platform's reading, one with many keeps its own.
    ce = (cohort or {}).get("evidence") or {}
    shrunk = {}
    for k in DEFAULT_WEIGHTS:
        e, n = own.get(k, (None, 0))
        if e is None and k not in ce:
            continue
        if e is None:
            shrunk[k] = ce[k] * COHORT_PRIOR_SHIFTS / float(COHORT_PRIOR_SHIFTS + n) if n else None
        elif k in ce:
            shrunk[k] = (n * e + COHORT_PRIOR_SHIFTS * ce[k]) / float(n + COHORT_PRIOR_SHIFTS)
        else:
            shrunk[k] = e
    shrunk = {k: v for k, v in shrunk.items() if v is not None}
    candidate = _bounded(shrunk, current)
    moves = {k: (current[k], candidate[k]) for k in candidate if abs(candidate[k] - current[k]) >= 0.05}
    holdout = _holdout_compare(samples, current, candidate, usd)
    base = {"before": current, "candidate": candidate, "holdout": holdout}
    if not moves:
        _record(restaurant_id, "shadow", current, candidate, holdout, cohort, "nothing to move", db_path)
        return dict(base, status="shadow", reason="nothing to move")
    gain, sure = holdout.get("gain"), holdout.get("p_better")
    if gain is None or sure is None or gain <= 0 or sure < SHADOW_MIN_CONFIDENCE:
        why = ("too few held-out shifts to check it" if gain is None or sure is None else
               f"on the newest weeks it was not clearly better at tracking what the shifts were worth "
               f"(better in {int(round(sure * 100))}% of resamples)")
        _record(restaurant_id, "shadow", current, candidate, holdout, cohort, why, db_path)
        return dict(base, status="shadow", reason=why)
    try:
        learns = models.learns_for_itself(restaurant_id, db_path=db_path)
    except Exception:
        learns = False
    if not learns or not _autopromote() or _quiet(restaurant_id, db_path):
        why = ("this restaurant does not learn for itself" if not learns else
               "automatic promotion is switched off" if not _autopromote() else
               f"the owner undid the last change less than {REVERT_QUIET_DAYS} days ago")
        _record(restaurant_id, "held", current, candidate, holdout, cohort, why, db_path)
        return dict(base, status="held", reason=why)
    after = dict(stored)
    after.update({k: candidate[k] for k in moves})
    models.update_restaurant(restaurant_id, {"quality_weights_json": json.dumps(after)})
    try:
        models.record_capability_change(restaurant_id, "quality_weights_promoted", subject="weights",
                                        before=json.dumps(stored), after=json.dumps(after),
                                        changed_by="Cavnar AI (calibration)")
    except Exception as e:
        log.warning("calibration promotion not in the change history for %s: %s", restaurant_id, e)
    why = (f"on the newest {holdout['weeks']} held-out week{'s' if holdout['weeks'] != 1 else ''} the new weights "
           f"tracked what the shifts were worth better ({holdout['current']:+.2f} → {holdout['candidate']:+.2f})")
    run_id = _record(restaurant_id, "promoted", stored, after, holdout, cohort, why, db_path)
    try:
        import schedule_memory
        schedule_memory.observe(restaurant_id, "calibration_promoted", value={
            "run_id": run_id, "moves": {k: [round(a, 1), round(b, 1)] for k, (a, b) in moves.items()},
            "holdout": holdout}, origin="system", phase="as_run", authority="system", source="calibration",
            db_path=db_path)
    except Exception:
        pass
    return dict(base, status="promoted", reason=why, run_id=run_id, moves=moves)


# ── Does the score predict better weeks here? (blind re-audit 10/11/26
# TRUST-31, STRESS-14) ──────────────────────────────────────────────────────
# The ring compared a score with the restaurant's last twelve scores only:
# graded against itself, its meaning asserted, never shown. Here the
# restaurant's own published weeks are split by the score their draft got —
# OUTCOME_HIGH and over (the "good" band) against under OUTCOME_LOW — and
# each side's measured outcomes stated: the coverage and no-show issues a
# week on the nights the coverage check watched (a night nobody watched is
# not a clean one), and the share of the draft's shifts managers changed
# before sending (schedule_versions.acceptance — the score calibrated
# against the managers' own edits). Each figure needs OUTCOME_MIN_WEEKS weeks
# on BOTH sides, else it is "—" with how many there are so far.
OUTCOME_HIGH = 78
OUTCOME_LOW = 65
OUTCOME_MIN_WEEKS = 3
OUTCOME_WEEKS = 26


def score_outcome_link(restaurant_id, db_path=None, weeks: int = OUTCOME_WEEKS) -> dict:
    """{"ready", "high": {"min", "weeks", "issues_per_week", "issue_weeks",
    "changed_share", "edit_weeks"}, "low": {...  "max" ...}, "predicts":
    True | False | None, "text" | None, "note"} — never raises (an
    unreadable record is not ready)."""
    out = {"ready": False, "predicts": None, "text": None, "floor": OUTCOME_MIN_WEEKS,
           "high": {"min": OUTCOME_HIGH, "weeks": 0, "issues_per_week": None, "issue_weeks": 0,
                    "changed_share": None, "edit_weeks": 0},
           "low": {"max": OUTCOME_LOW - 1, "weeks": 0, "issues_per_week": None, "issue_weeks": 0,
                   "changed_share": None, "edit_weeks": 0}, "note": None}
    try:
        import schedule_versions as _sv
        acc = _sv.acceptance(restaurant_id, weeks=weeks, **({"db_path": db_path} if db_path else {}))
        wk = [w for w in acc.get("weeks") or [] if w.get("history_id")]
        if not wk:
            out["note"] = _outcome_note(out)
            return out
        ids = [int(w["history_id"]) for w in wk]
        marks = ",".join("?" for _ in ids)
        conn = get_conn(db_path)
        try:
            scores = {r["id"]: r["quality_score"] for r in conn.execute(
                f"SELECT id, quality_score FROM schedule_history WHERE restaurant_id=? AND id IN ({marks})",
                (restaurant_id, *ids)).fetchall()}
            outs = conn.execute(
                f"SELECT history_id, date, SUM(issues) AS issues FROM schedule_outcomes WHERE restaurant_id=? "
                f"AND history_id IN ({marks}) GROUP BY history_id, date", (restaurant_id, *ids)).fetchall()
        finally:
            conn.close()
        dates = sorted(str(o["date"])[:10] for o in outs if o["date"])
        watched = set()
        if dates:
            from schedule_intel import watched_dates
            watched = watched_dates(restaurant_id, dates[0], dates[-1],
                                    **({"db_path": db_path} if db_path else {}))
        per_week = {}
        for o in outs:
            d = str(o["date"] or "")[:10]
            if d not in watched:
                continue
            e = per_week.setdefault(o["history_id"], {"issues": 0.0, "nights": 0})
            e["issues"] += float(o["issues"] or 0)
            e["nights"] += 1
    except Exception as e:
        log.warning("[calibration] score outcome link unavailable for %s: %s", restaurant_id, e)
        out["note"] = _outcome_note(out)
        return out
    groups = {"high": [], "low": []}
    for w in wk:
        s = scores.get(int(w["history_id"]))
        if s is None:
            continue
        side = "high" if float(s) >= OUTCOME_HIGH else ("low" if float(s) < OUTCOME_LOW else None)
        if side:
            groups[side].append(w)
    for side, ws in groups.items():
        g = out[side]
        g["weeks"] = len(ws)
        iss = [per_week[w["history_id"]] for w in ws if (per_week.get(w["history_id"]) or {}).get("nights")]
        g["issue_weeks"] = len(iss)
        if iss:
            g["issues_per_week"] = round(sum(e["issues"] / e["nights"] * 7.0 for e in iss) / len(iss), 1)
        ed = [1.0 - float(w["unchanged_share"]) for w in ws if w.get("unchanged_share") is not None]
        g["edit_weeks"] = len(ed)
        if ed:
            g["changed_share"] = round(sum(ed) / len(ed), 3)
    hi, lo = out["high"], out["low"]
    issues_ok = hi["issue_weeks"] >= OUTCOME_MIN_WEEKS and lo["issue_weeks"] >= OUTCOME_MIN_WEEKS
    edits_ok = hi["edit_weeks"] >= OUTCOME_MIN_WEEKS and lo["edit_weeks"] >= OUTCOME_MIN_WEEKS
    if not (issues_ok or edits_ok):
        out["note"] = _outcome_note(out)
        return out
    out["ready"] = True
    hi_bits, lo_bits, better = [], [], []
    if issues_ok:
        hi_bits.append(f"{hi['issues_per_week']:g} coverage or no-show issues a week")
        lo_bits.append(f"{lo['issues_per_week']:g}")
        better.append(hi["issues_per_week"] < lo["issues_per_week"])
    if edits_ok:
        hi_bits.append(f"{int(round(100 * hi['changed_share']))}% of shifts changed before sending")
        lo_bits.append(f"{int(round(100 * lo['changed_share']))}%")
        better.append(hi["changed_share"] < lo["changed_share"])
    out["predicts"] = all(better)
    out["text"] = (f"Your weeks scoring {OUTCOME_HIGH}+: " + " and ".join(hi_bits) + f"; under {OUTCOME_LOW}: "
                   + " and ".join(lo_bits) + f" (over {hi['weeks']} and {lo['weeks']} weeks)."
                   + ("" if out["predicts"] else " Here the higher scores have not yet meant better weeks."))
    return out


def _outcome_note(out) -> str:
    return (f"How the score tracks your weeks is shown once {OUTCOME_MIN_WEEKS} of your published weeks score "
            f"{OUTCOME_HIGH}+ and {OUTCOME_MIN_WEEKS} score under {OUTCOME_LOW} ({out['high']['weeks']} and "
            f"{out['low']['weeks']} so far).")


def latest_promotion(restaurant_id, db_path=None) -> dict:
    """The newest promotion still in force, as the owner is told it:
    {run_id, when (M/D/YY), changes: [{dimension, before, after}], reason,
    can_undo} — or None."""
    from time_utils import mdy
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM schedule_calibration_runs WHERE restaurant_id=? AND status IN "
                         "('promoted', 'reverted') ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if r is None or r["status"] != "promoted":
        return None
    try:
        before, after = json.loads(r["before_json"] or "{}") or {}, json.loads(r["candidate_json"] or "{}") or {}
    except (TypeError, ValueError):
        return None
    from shift_quality import DEFAULT_WEIGHTS
    changes = []
    for k, v in after.items():
        b = before.get(k, DEFAULT_WEIGHTS.get(k))
        if b is None or abs(float(v) - float(b)) >= 0.05:
            changes.append({"dimension": k, "label": k.replace("_", " "), "before": b, "after": v})
    return {"run_id": r["id"], "when": mdy(r["run_at"]), "changes": changes, "reason": r["reason"], "can_undo": True}


def revert(restaurant_id, user=None, db_path=None) -> dict:
    """The owner's Undo of the newest promotion: the weights before it back,
    recorded, and no promotion for REVERT_QUIET_DAYS. Raises ValueError with
    the owner's words when there is nothing to undo."""
    import models
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM schedule_calibration_runs WHERE restaurant_id=? AND status='promoted' "
                         "ORDER BY id DESC LIMIT 1", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if r is None:
        raise ValueError("There is no automatic change to the weights to undo.")
    before = json.loads(r["before_json"] or "{}") or {}
    now = models.get_quality_weights(restaurant_id) or {}
    models.update_restaurant(restaurant_id, {"quality_weights_json": json.dumps(before)})
    who = ((user or {}).get("username") or (user or {}).get("email") or "owner") if isinstance(user, dict) else "owner"
    try:
        models.record_capability_change(restaurant_id, "quality_weights_reverted", subject="weights",
                                        before=json.dumps(now), after=json.dumps(before), changed_by=who)
    except Exception as e:
        log.warning("calibration revert not in the change history for %s: %s", restaurant_id, e)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE schedule_calibration_runs SET status='reverted', reverted_by=?, reverted_at=datetime('now') "
                     "WHERE id=?", (who[:120], r["id"]))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "weights": before, "run_id": r["id"]}
