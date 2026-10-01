"""event_intel.peers — what games do across restaurants that follow them
(Event Intelligence phase 4), behind the privacy floor.

For a restaurant that has not measured games like one yet, the median of
what the same series' games of the same side (home or road, preseason kept
apart) did at OTHER restaurants that follow it, each restaurant counted once
by its own measured median (engine.effect_for), and only:

  * restaurants that may teach (intelligence.jobs.real_restaurant_ids: no
    demo, test or internal account);
  * the viewer's whole organisation left out (privacy.org_map);
  * at least privacy.MIN_COHORT restaurants from privacy.MIN_ORGS
    organisations, no one organisation over privacy.MAX_ORG_SHARE;
  * rounded to PEER_STEP_PCT, so no member's own figure can be read back.

It is said as theirs, never as this restaurant's, and is never applied to a
forecast, a staffing plan or a push: those wait for this restaurant's own
nights. Every payload passes privacy.assert_anonymous. Never raises.
"""
import logging
import time

from event_intel import engine, store

log = logging.getLogger(__name__)

PEER_STEP_PCT = 5
_MEMO_SECONDS = 3600
_memo = {}


def _round(x):
    return int(PEER_STEP_PCT * round(float(x) / PEER_STEP_PCT))


def peer_effect(restaurant_id, e, db_path=store.DB_PATH):
    """{"segment", "median_lift_pct", "n", "orgs", "text", "claim_kind":
    "peers"} or None below the floor. `e` is a catalog event."""
    try:
        from intelligence import privacy
        from intelligence.jobs import real_restaurant_ids
        orgs = privacy.org_map(db_path=None if db_path == store.DB_PATH else db_path)
        mine = orgs.get(int(restaurant_id))
        side = e.get("home_away")
        pre = e.get("season_type") == "preseason"
        key = (e.get("series_id"), side, pre, mine)
        hit = _memo.get(key)
        if hit and time.monotonic() - hit[0] < _MEMO_SECONDS:
            return hit[1]
        real = real_restaurant_ids(db_path=db_path)
        probe = dict(e, is_primetime=0)
        lifts, members = [], []
        for rid in store.followers(e["series_id"], db_path=db_path):
            if rid == int(restaurant_id) or rid not in real or (mine is not None and orgs.get(rid) == mine):
                continue
            eff = engine.effect_for(rid, probe, db_path=db_path)
            if not eff:
                continue
            lifts.append(float(eff["median_lift_pct"]))
            members.append(orgs.get(rid, f"r{rid}"))
        out = None
        n_orgs, share = privacy.org_counts(members)
        if privacy.cohort_ok(len(lifts)) and privacy.orgs_ok(n_orgs, share):
            med = _round(engine._median(lifts))
            short = e.get("short_name") or e.get("series_name") or ""
            words = engine.kind_words(probe)
            out = privacy.assert_anonymous({
                "segment": f"{short} {words}".strip(), "median_lift_pct": med, "n": len(lifts),
                "claim_kind": "peers",
                "text": (f"Across {len(lifts)} other restaurants that follow the {short}, {words} have run about "
                         f"{med:+d}% against a usual same weekday — their nights, not yours; Cavnar AI plans on "
                         f"your own once {engine.SEGMENT_MIN_N} are measured here")})
        _memo[key] = (time.monotonic(), out)
        return out
    except Exception as ex:
        log.warning("event_intel.peers failed rid=%s: %s", restaurant_id, ex)
        return None
