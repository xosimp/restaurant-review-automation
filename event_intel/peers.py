"""event_intel.peers — what games do across restaurants that follow them
(Event Intelligence phase 4), behind the privacy floor.

For a restaurant that has not measured games like one yet, the median of
what the same series' games of the same side (home or road, preseason kept
apart) did at OTHER restaurants that follow it, each restaurant counted once
by its own measured median (engine.effect_for), and only:

  * restaurants that may teach (intelligence.jobs.real_restaurant_ids: no
    demo, test or internal account), and none of a converted demo's nights
    from before its learning_since (jobs.before_learning);
  * the viewer's whole organisation left out (privacy.org_map);
  * at least benchmarks.MIN_QUARTILE_N other restaurants from
    privacy.MIN_ORGS organisations, no one organisation over
    privacy.MAX_ORG_SHARE — the published band's own floor;
  * the Harrell–Davis median (stats.harrell_davis), never one member's own
    figure, rounded to PEER_STEP_PCT, with n said as a floor ("8 or more"),
    never exactly (phase 4 audit);
  * each member's figure from the SAME segment only (side and season
    class, engine.effect_for(exact=True)) — never a fallback that mixes
    preseason in.

It is said as theirs, never as this restaurant's, and is never applied to a
forecast, a staffing plan or a push: those wait for this restaurant's own
nights. Every payload passes privacy.assert_anonymous. Never raises.
"""
import logging
import time

from event_intel import engine, store

log = logging.getLogger(__name__)

PEER_STEP_PCT = 5
# A minute, and dropped whenever any restaurant row changes (an opt-out, an
# exclusion, an organisation change) — models.on_restaurant_change.
_MEMO_SECONDS = 60
_memo = {}
_hooked = [False]


def invalidate(*_a, **_k):
    _memo.clear()


def _hook():
    if _hooked[0]:
        return
    try:
        import models
        models.on_restaurant_change(invalidate)
        _hooked[0] = True
    except Exception:
        pass


def _round(x):
    return int(PEER_STEP_PCT * round(float(x) / PEER_STEP_PCT))


def peer_effect(restaurant_id, e, db_path=store.DB_PATH):
    """{"segment", "median_lift_pct", "n", "claim_kind": "computed", "text"}
    or None below the floor. `e` is a catalog event. `n` is the floor the
    figure clears (MIN_QUARTILE_N), never the exact count."""
    try:
        from intelligence import privacy
        from intelligence.benchmarks import MIN_QUARTILE_N
        from intelligence.jobs import before_learning, learning_since_by_id, real_restaurant_ids
        from intelligence.stats import harrell_davis
        _hook()
        dbp = None if db_path == store.DB_PATH else db_path
        orgs = privacy.org_map(db_path=dbp)
        mine = orgs.get(int(restaurant_id))
        side = e.get("home_away")
        pre = e.get("season_type") == "preseason"
        key = (dbp, int(restaurant_id), e.get("series_id"), side, pre, mine)
        hit = _memo.get(key)
        if hit and time.monotonic() - hit[0] < _MEMO_SECONDS:
            return hit[1]
        real = real_restaurant_ids(db_path=db_path)
        since = learning_since_by_id(db_path=db_path)
        lifts, members = [], []
        for rid in store.followers(e["series_id"], db_path=db_path):
            if rid == int(restaurant_id) or rid not in real or (mine is not None and orgs.get(rid) == mine):
                continue
            eff = engine.effect_for(rid, e, db_path=db_path, exact=True,
                                    keep=lambda d, _r=rid: not before_learning(_r, d, since))
            if not eff:
                continue
            lifts.append(float(eff["median_lift_pct"]))
            members.append(orgs.get(rid, f"r{rid}"))
        out = None
        n_orgs, share = privacy.org_counts(members)
        if len(lifts) >= MIN_QUARTILE_N and privacy.orgs_ok(n_orgs, share):
            med = _round(harrell_davis(lifts, 50))
            short = e.get("short_name") or e.get("series_name") or ""
            words = engine.kind_words(dict(e, is_primetime=0))
            out = privacy.assert_anonymous({
                "segment": f"{short} {words}".strip(), "median_lift_pct": med, "n": MIN_QUARTILE_N,
                "claim_kind": "computed",
                "text": (f"Across {MIN_QUARTILE_N} or more other restaurants that follow the {short}, {words} have "
                         f"run about {med:+d}% against a usual same weekday — their nights, not yours; Cavnar AI "
                         f"plans on your own once {engine.SEGMENT_MIN_N} are measured here")})
        if len(_memo) > 2000:
            _memo.clear()
        _memo[key] = (time.monotonic(), out)
        return out
    except Exception as ex:
        log.warning("event_intel.peers failed rid=%s: %s", restaurant_id, ex)
        return None
