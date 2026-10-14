"""
schedule_calibration.py — the Shift Quality weights calibrated where nobody
presses Apply (schedule audit 10/10/26 LEARNING-9), honestly held out
(blind re-audit 10/11/26 LEARNING-1, -2, -3).

schedule_learning.calibrate_weights (v2: as-run dimensions, weekday ×
daypart and demand fixed effects, per-outcome fits combined in dollars,
held-out weeks, a corrected threshold search) only ever SUGGESTED, and
across thousands of restaurants almost no owner reads a ridge regression
and presses Apply: the score that drives the solver, the optimizer and the
trim stays at its defaults everywhere.

Weekly (strategy_jobs.run_quality_calibration):

  cohort_evidence()      a stratified sample of the restaurants that may
                         teach (learning_stats.fleet_sample, rotated weekly —
                         never the first N by id), each one's TRAINING weeks
                         only (its newest CALIBRATION_HOLDOUT_SHARE are held
                         out of the pool, so no restaurant's check weeks are
                         in the prior it is shrunk toward), pooled with each
                         restaurant's own weekday × daypart cells as fixed
                         effects — kept only with MIN_COHORT restaurants or
                         more and as anonymous ratios (intelligence.privacy)
  shadow_run(rid)        three gates, each a fresh test:
    1. new evidence      nothing runs unless the restaurant has a published
                         week it did not have at the last run (LEARNING-3:
                         the same evidence used to re-promote every Sunday,
                         walking a weight to its cap on one week of data)
    2. retrospective     the candidate is FITTED ON THE OLDER WEEKS ONLY
                         (schedule_learning's split — it used to be fitted on
                         every week, the held-out ones included, so a check
                         on "held-out" weeks confirmed noise 42% of the time)
                         and checked on the newest weeks it never saw
                         (Williams' test of dependent correlations); it must
                         beat the
                         weights now AND the weights before any automatic
                         change (the cumulative move, not the step). Passing
                         makes it `pending` — nothing changes yet.
    3. prospective       a pending candidate is checked again, unchanged, on
                         the PROSPECTIVE_MIN_WEEKS published weeks that did
                         not exist when it was proposed — a true out-of-time
                         test. `confirmed` there, it is promoted (when
                         promotion is on), else `refuted` and dropped.
  revert(rid)            the owner's undo: the weights before the promotion
                         back, and no promotion for REVERT_QUIET_DAYS
  fleet_readout()        the fleet-level evaluation the promotion switch is
                         decided on: every prospective check's gain, the
                         restaurant-cluster mean with its 90% interval.

Statuses (schedule_calibration_runs.status): not_ready (never stored),
shadow (the retrospective check failed or nothing moved), pending, refuted,
held (confirmed, not promoted: switched off, a demo, or the owner's recent
Undo), promoted, reverted.

The owner is told what changed (the labor intel payload's
weight_calibration.promoted, with Undo) — never a silent change. A
restaurant that does not learn for itself (a demo, an admin's exclusion)
is never promoted. Auto-promotion is OFF unless
SCHEDULE_CALIBRATION_AUTOPROMOTE=1 — off, a confirmed candidate is `held`;
it stays off until fleet_readout() shows the confirmed candidates gaining
on the weeks they never saw.
"""
import json
import logging
import os
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# A candidate passes a check when it tracks the held-out shifts' dollar
# outcomes better than the weights it is compared with at a one-sided
# p-value of at most 1 - SHADOW_MIN_CONFIDENCE (Williams' test of two
# dependent correlations, schedule_learning.better_correlation). Two
# independent checks (held-out weeks, then new weeks) must both pass.
SHADOW_MIN_CONFIDENCE = 0.9
BOOTSTRAP_TRIES = 200                    # kept for readers of the old name
COHORT_PRIOR_SHIFTS = 40
REVERT_QUIET_DAYS = 56
AUTOPROMOTE_ENV = "SCHEDULE_CALIBRATION_AUTOPROMOTE"
COHORT_MAX_RESTAURANTS = 300
RUNS_KEEP_DAYS = 400
# The prospective check: published weeks newer than the candidate, and
# their shifts with an outcome, before it is read.
PROSPECTIVE_MIN_WEEKS = 2
PROSPECTIVE_MIN_SHIFTS = 10
# A pending candidate whose prospective weeks never come is dropped after
# this long (and the next run proposes afresh).
PENDING_MAX_DAYS = 70


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
    # reused weeks it was fitted on, so noise promoted). The split is honest
    # now (fitted on older weeks, held-out weeks, then new weeks); the switch
    # stays off until fleet_readout() shows a gain. SCHEDULE_CALIBRATION_
    # AUTOPROMOTE=1 turns it on.
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


def _split(samples):
    """(train, held, held_weeks): the newest CALIBRATION_HOLDOUT_SHARE of the
    published weeks held out (schedule_learning's own split)."""
    import schedule_learning as sl
    weeks = sorted({s[2] for s in samples})
    k = max(1, int(round(len(weeks) * sl.CALIBRATION_HOLDOUT_SHARE)))
    held = set(weeks[-k:])
    return [s for s in samples if s[2] not in held], [s for s in samples if s[2] in held], held


def cohort_evidence(db_path=None, today=None, store=True) -> dict:
    """The pooled evidence per dimension across a stratified, weekly-rotated
    sample (learning_stats.fleet_sample) of the Labor restaurants that may
    teach (intelligence.jobs.excluded_learning_ids) with a ready calibration
    — each restaurant's TRAINING weeks only (its held-out weeks stay out of
    every prior), its own weekday × daypart cells as fixed effects — or {}
    below intelligence.privacy.MIN_COHORT restaurants. Kept weekly in
    schedule_calibration_cohort as anonymous ratios."""
    import learning_stats
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
    y, w, _ = (today or datetime.utcnow().date()).isocalendar()
    week_key = f"{y}-W{w:02d}"
    rows = models.get_all_restaurants(db_path) if db_path else models.get_all_restaurants()
    eligible = [r.id for r in rows if r.id not in excluded and getattr(r, "module_labor", 0)]
    # Read across the whole fleet, rotated weekly (LEARNING-23): the first
    # COHORT_MAX_RESTAURANTS by id were the oldest customers only. Twice the
    # sample is read so restaurants not ready yet do not shrink it.
    pick = learning_stats.fleet_sample(eligible, 2 * COHORT_MAX_RESTAURANTS, key=f"calibration|{week_key}")
    pooled, n = [], 0
    for rid in pick:
        if n >= COHORT_MAX_RESTAURANTS:
            break
        try:
            samples, _w = sl._calibration_samples(rid, db_path or sl.DB_PATH)
        except Exception as e:
            log.warning("calibration cohort: samples unreadable for a restaurant: %s", e)
            continue
        if len({s[2] for s in samples}) < sl.CALIBRATION_MIN_WEEKS or len(samples) < sl.CALIBRATION_MIN_SHIFTS:
            continue
        train, _held, _hw = _split(samples)
        if not train:
            continue
        n += 1
        tag = f"r{n}"                       # a cell per restaurant, never its id
        pooled += [(d, oc, f"{tag}:{h}", dict(m or {}, cell=f"{tag}|{(m or {}).get('cell') or ''}"))
                   for d, oc, h, m in train]
    if n < privacy.MIN_COHORT:
        return {}
    usd = sl._dollar_weights(pooled)
    ev = _evidence(pooled, list(DEFAULT_WEIGHTS), usd)
    out = privacy.assert_anonymous({"restaurants": n, "shifts": len(pooled),
                                    "evidence": {k: round(v, 4) for k, (v, _n) in ev.items()}})
    if store:
        try:
            conn = get_conn(db_path)
            try:
                conn.execute("INSERT INTO schedule_calibration_cohort (week, restaurants, shifts, evidence_json) "
                             "VALUES (?,?,?,?) ON CONFLICT(week) DO UPDATE SET restaurants=excluded.restaurants, "
                             "shifts=excluded.shifts, evidence_json=excluded.evidence_json, computed_at=datetime('now')",
                             (week_key, n, len(pooled), json.dumps(out["evidence"])))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning("calibration cohort not kept: %s", e)
    return out


def stored_cohort(db_path=None, today=None) -> dict:
    """This ISO week's kept cohort evidence (what a queued per-restaurant
    run reads), or {}."""
    y, w, _ = (today or datetime.utcnow().date()).isocalendar()
    try:
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT restaurants, shifts, evidence_json FROM schedule_calibration_cohort WHERE week=?",
                             (f"{y}-W{w:02d}",)).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not r:
        return {}
    try:
        return {"restaurants": r["restaurants"], "shifts": r["shifts"], "evidence": json.loads(r["evidence_json"])}
    except (TypeError, ValueError):
        return {}


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


def _compare_on(test, against, candidate, usd) -> dict:
    """{shifts, weeks, current, candidate, gain, p_better}: on the `test`
    shifts (never fitted on), the within-weekday-and-daypart correlation of
    the score under `against` and under `candidate` with what the shifts
    were worth in dollars, and the one-sided test that the candidate's is
    the higher (Williams' t for dependent correlations, schedule_learning.
    better_correlation — LEARNING-1: the bootstrap it replaces confirmed
    noise): p_value, and p_better = 1 − p_value."""
    import schedule_learning as sl
    vals, a_, b_, groups, weeks = [], [], [], [], []
    for s in test:
        v = [sl.CALIBRATION_OUTCOMES[o][0] * s[1][o] * float(usd.get(o) or 0) for o in sl.CALIBRATION_OUTCOMES
             if s[1].get(o) is not None]

        def comp(w):
            den = sum(float(w.get(k_, 0)) for k_ in s[0] if k_ in w)
            return sum(float(w.get(k_, 0)) * x for k_, x in s[0].items() if k_ in w) / den if den else None
        x, y = comp(against), comp(candidate)
        if v and x is not None and y is not None:
            vals.append(sum(v))
            a_.append(x)
            b_.append(y)
            groups.append(sl._fe_group(s))
            weeks.append(s[2])
    out = {"weeks": len(set(weeks)), "shifts": len(vals), "current": None, "candidate": None, "gain": None,
           "p_better": None}
    if len(vals) < 10:
        return out
    yv = sl._within(vals, groups)
    xa, xb = sl._within(a_, groups), sl._within(b_, groups)
    rc = sl._pearson(list(zip(xa, yv)))
    rn = sl._pearson(list(zip(xb, yv)))
    if rc is None or rn is None:
        return out
    test = sl.better_correlation(xa, xb, yv)
    out.update(current=round(rc, 4), candidate=round(rn, 4), gain=round(rn - rc, 4),
               p_better=test["better"], p_value=test["p"])
    return out


def _passes(check) -> bool:
    gain, sure = (check or {}).get("gain"), (check or {}).get("p_better")
    return gain is not None and sure is not None and gain > 0 and sure >= SHADOW_MIN_CONFIDENCE


def _holdout_compare(samples, current, candidate, usd) -> dict:
    """The newest held-out weeks' comparison of `candidate` with `current`
    (_compare_on). Kept for callers of the old name; the candidate it is
    handed must have been fitted without those weeks."""
    _train, held, hw = _split(samples)
    return dict(_compare_on(held, current, candidate, usd), weeks=len(hw))


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


def _runs(restaurant_id, db_path=None, limit=60) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM schedule_calibration_runs WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
                            (restaurant_id, int(limit))).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("before_json", "candidate_json", "holdout_json"):
            try:
                d[k[:-5]] = json.loads(d.get(k) or "null")
            except (TypeError, ValueError):
                d[k[:-5]] = None
        out.append(d)
    return out


def _evidence_mark(samples) -> dict:
    """What the run read: the published weeks (history ids) and how many."""
    weeks = sorted({s[2] for s in samples})
    return {"newest_week": weeks[-1] if weeks else None, "weeks_seen": len(weeks)}


def _anchor(stored, runs):
    """The weights before the unbroken chain of automatic promotions now in
    force (the cumulative move is tested against them — LEARNING-3): walk
    the promotions back while each one's result is the weights the next one
    started from. The weights now when nothing automatic is in force."""
    cur = {k: round(float(v), 1) for k, v in (stored or {}).items()}
    anchor = dict(stored or {})
    for r in runs:
        if r["status"] == "reverted":
            break
        if r["status"] != "promoted":
            continue
        after = {k: round(float(v), 1) for k, v in (r.get("candidate") or {}).items()}
        if after != cur:
            break
        anchor = dict(r.get("before") or {})
        cur = {k: round(float(v), 1) for k, v in anchor.items()}
    return anchor


def weights_without_promotions(restaurant_id, stored=None, db_path=None) -> dict:
    """The weights in force with the automatic promotions taken back — the
    owner's own (learning_v2's off week reads these, LEARNING-14)."""
    import models
    if stored is None:
        stored = models.get_quality_weights(restaurant_id) or {}
    try:
        return dict(_anchor(stored, _runs(restaurant_id, db_path)))
    except Exception:
        return dict(stored or {})


def shadow_run(restaurant_id, cohort=None, db_path=None) -> dict:
    """One restaurant's weekly calibration run (module doc). Returns {status
    (not_ready | no_new_data | shadow | pending | refuted | held |
    promoted), before, candidate, holdout, reason}."""
    import models
    import schedule_learning as sl
    from shift_quality import DEFAULT_WEIGHTS
    samples, _w = sl._calibration_samples(restaurant_id, db_path or sl.DB_PATH)
    if len({s[2] for s in samples}) < sl.CALIBRATION_MIN_WEEKS or len(samples) < sl.CALIBRATION_MIN_SHIFTS:
        return {"status": "not_ready"}
    mark = _evidence_mark(samples)
    runs = _runs(restaurant_id, db_path)
    # 1. New evidence only (LEARNING-3): a run reads the same published weeks
    # as the last one says nothing new — nothing is recorded or moved.
    last = next((r for r in runs if (r.get("holdout") or {}).get("newest_week") is not None), None)
    if last is not None and (last["holdout"].get("newest_week"), last["holdout"].get("weeks_seen")) == \
            (mark["newest_week"], mark["weeks_seen"]):
        return {"status": "no_new_data", "reason": "no published week since the last run"}
    stored = models.get_quality_weights(restaurant_id) or {}
    current = dict(DEFAULT_WEIGHTS)
    current.update({k: float(v) for k, v in stored.items() if k in DEFAULT_WEIGHTS})
    anchor = dict(DEFAULT_WEIGHTS)
    anchor.update({k: float(v) for k, v in _anchor(stored, runs).items() if k in DEFAULT_WEIGHTS})
    # 3. A pending candidate meets the weeks it never saw.
    pending = runs[0] if runs and runs[0]["status"] == "pending" else None
    if pending is not None:
        got = _prospective(restaurant_id, pending, samples, current, anchor, mark, cohort, db_path)
        if got is not None:
            return got
    # 2. The retrospective check: fitted on the older weeks only.
    train, held, hw = _split(samples)
    if len(train) < sl.CALIBRATION_MIN_SHIFTS or len(held) < 10:
        why = "too few weeks to hold some out and check a change on them"
        _record(restaurant_id, "shadow", current, current, dict(mark, weeks=len(hw)), cohort, why, db_path)
        return {"status": "shadow", "before": current, "candidate": current, "holdout": {}, "reason": why}
    usd = sl._dollar_weights(train)
    own = _evidence(train, list(DEFAULT_WEIGHTS), usd)
    # Shrunk toward the cohort (its pool holds no restaurant's held-out
    # weeks): a restaurant with few shifts borrows the platform's reading,
    # one with many keeps its own.
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
    vs_now = _compare_on(held, current, candidate, usd)
    holdout = dict(vs_now, **mark, weeks=len(hw), fitted_on_weeks=len({s[2] for s in train}))
    if anchor != current:
        holdout["vs_anchor"] = _compare_on(held, anchor, candidate, usd)
    base = {"before": current, "candidate": candidate, "holdout": holdout}
    if not moves:
        _record(restaurant_id, "shadow", current, candidate, holdout, cohort, "nothing to move", db_path)
        return dict(base, status="shadow", reason="nothing to move")
    if not _passes(vs_now) or ("vs_anchor" in holdout and not _passes(holdout["vs_anchor"])):
        sure = vs_now.get("p_better")
        why = ("too few held-out shifts to check it" if vs_now.get("gain") is None or sure is None else
               "fitted on the older weeks, it was not clearly better on the newest weeks at tracking what the "
               f"shifts were worth (one-sided p = {1 - sure:.2f})"
               if not _passes(vs_now) else
               "it beat the weights now but not the weights before Cavnar AI's earlier changes")
        _record(restaurant_id, "shadow", current, candidate, holdout, cohort, why, db_path)
        return dict(base, status="shadow", reason=why)
    why = (f"fitted on the older weeks, it tracked the newest {len(hw)} held-out week{'s' if len(hw) != 1 else ''} "
           f"better ({vs_now['current']:+.2f} → {vs_now['candidate']:+.2f}); waiting for "
           f"{PROSPECTIVE_MIN_WEEKS} new published weeks to check it on before anything changes")
    run_id = _record(restaurant_id, "pending", current, candidate, holdout, cohort, why, db_path)
    return dict(base, status="pending", reason=why, run_id=run_id)


def _prospective(restaurant_id, pending, samples, current, anchor, mark, cohort, db_path):
    """The out-of-time check of a pending candidate (module doc, gate 3), or
    None when it lapsed and the run should propose afresh."""
    import models
    import schedule_learning as sl
    cand = {k: float(v) for k, v in (pending.get("candidate") or {}).items()}
    since = (pending.get("holdout") or {}).get("newest_week")
    fresh = [s for s in samples if since is not None and s[2] > since]
    fresh_weeks = sorted({s[2] for s in fresh})
    try:
        age = (datetime.utcnow() - datetime.strptime(str(pending["run_at"])[:19], "%Y-%m-%d %H:%M:%S")).days
    except (TypeError, ValueError):
        age = 0
    if len(fresh_weeks) < PROSPECTIVE_MIN_WEEKS or len(fresh) < PROSPECTIVE_MIN_SHIFTS:
        if age > PENDING_MAX_DAYS:
            _record(restaurant_id, "refuted", current, cand, dict(mark, prospective={"weeks": len(fresh_weeks)}),
                    cohort, f"no {PROSPECTIVE_MIN_WEEKS} new published weeks within {PENDING_MAX_DAYS} days to "
                    "check it on", db_path)
            return None
        return {"status": "pending", "before": current, "candidate": cand, "holdout": pending.get("holdout") or {},
                "reason": f"waiting: {len(fresh_weeks)} of {PROSPECTIVE_MIN_WEEKS} new published weeks to check it on"}
    usd = sl._dollar_weights([s for s in samples if s[2] <= since] or samples)
    vs_now = _compare_on(fresh, current, cand, usd)
    vs_anchor = _compare_on(fresh, anchor, cand, usd) if anchor != current else None
    holdout = dict(mark, retrospective=pending.get("holdout") or {}, prospective=vs_now,
                   prospective_vs_anchor=vs_anchor, proposed_run=pending["id"])
    base = {"before": current, "candidate": cand, "holdout": holdout}
    if not _passes(vs_now) or (vs_anchor is not None and not _passes(vs_anchor)):
        why = (f"on the {len(fresh_weeks)} weeks published since it was proposed it was not clearly better "
               f"(one-sided p = {1 - (vs_now.get('p_better') or 0):.2f}) — dropped")
        _record(restaurant_id, "refuted", current, cand, holdout, cohort, why, db_path)
        return dict(base, status="refuted", reason=why)
    moves = {k: (current.get(k), cand[k]) for k in cand if abs(cand[k] - float(current.get(k, cand[k]))) >= 0.05}
    try:
        learns = models.learns_for_itself(restaurant_id, db_path=db_path)
    except Exception:
        learns = False
    if not moves or not learns or not _autopromote() or _quiet(restaurant_id, db_path):
        why = ("the weights it would change have changed since" if not moves else
               "this restaurant does not learn for itself" if not learns else
               "confirmed on new weeks, but automatic promotion is switched off" if not _autopromote() else
               f"the owner undid the last change less than {REVERT_QUIET_DAYS} days ago")
        _record(restaurant_id, "held", current, cand, holdout, cohort, why, db_path)
        return dict(base, status="held", reason=why)
    stored = models.get_quality_weights(restaurant_id) or {}
    after = dict(stored)
    after.update({k: cand[k] for k in moves})
    models.update_restaurant(restaurant_id, {"quality_weights_json": json.dumps(after)})
    try:
        models.record_capability_change(restaurant_id, "quality_weights_promoted", subject="weights",
                                        before=json.dumps(stored), after=json.dumps(after),
                                        changed_by="Cavnar AI (calibration)")
    except Exception as e:
        log.warning("calibration promotion not in the change history for %s: %s", restaurant_id, e)
    retro = pending.get("holdout") or {}
    why = (f"fitted on older weeks, it tracked what the shifts were worth better on the held-out weeks and again on "
           f"the {len(fresh_weeks)} weeks published since ({vs_now['current']:+.2f} → {vs_now['candidate']:+.2f})")
    run_id = _record(restaurant_id, "promoted", stored, after, holdout, cohort, why, db_path)
    try:
        import schedule_memory
        schedule_memory.observe(restaurant_id, "calibration_promoted", value={
            "run_id": run_id, "moves": {k: [round(float(a or 0), 1), round(b, 1)] for k, (a, b) in moves.items()},
            "holdout": {"retrospective": retro, "prospective": vs_now}}, origin="system", phase="as_run",
            authority="system", source="calibration", db_path=db_path)
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


def fleet_readout(db_path=None, days=365) -> dict:
    """The fleet-level evaluation behind the promotion switch (LEARNING-1:
    "keep autopromote off until a fleet-level eval shows gain"): every
    prospective check in the last `days` — candidates read on weeks that did
    not exist when they were proposed — with the restaurant-cluster mean
    gain in correlation and its 90% interval, and how many were confirmed
    and refuted. `recommend_on` only when that interval is above zero over
    at least intelligence.privacy.MIN_COHORT restaurants."""
    import schedule_experiments as sx
    from intelligence import privacy
    since = (datetime.utcnow() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT restaurant_id, status, holdout_json FROM schedule_calibration_runs WHERE "
                            "status IN ('refuted', 'held', 'promoted', 'reverted') AND run_at >= ?",
                            (since,)).fetchall()
    finally:
        conn.close()
    gains, counts = [], {"confirmed": 0, "refuted": 0}
    for r in rows:
        try:
            h = json.loads(r["holdout_json"] or "{}") or {}
        except (TypeError, ValueError):
            continue
        p = h.get("prospective") or {}
        if p.get("gain") is None:
            continue
        gains.append((r["restaurant_id"], float(p["gain"])))
        counts["refuted" if r["status"] == "refuted" else "confirmed"] += 1
    ci = sx.cluster_mean_ci(gains) if gains else {"n": 0, "mean": None, "ci90": None, "restaurants": 0}
    ok = bool(ci.get("ci90")) and ci["ci90"][0] > 0 and int(ci.get("restaurants") or 0) >= privacy.MIN_COHORT
    return {"checks": len(gains), "restaurants": ci.get("restaurants", 0), **counts, "mean_gain": ci.get("mean"),
            "ci90": ci.get("ci90"), "recommend_on": ok, "switch": AUTOPROMOTE_ENV, "switch_on": _autopromote(),
            "method": ("each candidate fitted on older weeks, passed the held-out weeks, then read unchanged on the "
                       f"{PROSPECTIVE_MIN_WEEKS}+ weeks published after it was proposed; the gain is its correlation "
                       "with what the shifts were worth minus the weights then in force; restaurant-cluster 90% "
                       "interval")}


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
