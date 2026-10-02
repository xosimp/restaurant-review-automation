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

Two halves (re-audit 10/1/26, P4-05). Each member's median is MATERIALISED
nightly — `store_member_effects`, called for every restaurant by the bounded,
cursor-resumable intelligence features pass (intelligence.jobs.run_features)
— into `event_peer_effects`: one row per restaurant × series × side × season
class, a percentage and its night count, nothing else. A request reads only
that table, one indexed query per segment shared by every viewer, and
applies the viewer's organisation exclusion and the floors. It never reads
another restaurant's per-night `event_outcomes` (INTELLIGENCE_ENGINE.md rule
2): that read used to run per follower, per viewer, per request.

It is said as theirs, never as this restaurant's, and is never applied to a
forecast, a staffing plan or a push: those wait for this restaurant's own
nights. Every payload passes privacy.assert_anonymous. Never raises.
"""
import logging
import time

from event_intel import engine, store

log = logging.getLogger(__name__)

PEER_STEP_PCT = 5
SIDES = ("home", "away")
# A minute, and dropped whenever any restaurant row changes (an opt-out, an
# exclusion, an organisation change) — models.on_restaurant_change. Keyed by
# segment and database, never by viewer: every viewer of a segment shares it.
_MEMO_SECONDS = 60
_memo = {}
_hooked = [False]


def init_peers(db_path=store.DB_PATH):
    """The materialised member table — at boot only (event_intel.init_event_intel,
    from models.init_db), never on a request path."""
    conn = store.get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS event_peer_effects (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            series_id       INTEGER NOT NULL,
            home_away       TEXT    NOT NULL,
            preseason       INTEGER NOT NULL DEFAULT 0,
            median_lift_pct REAL    NOT NULL,
            nights          INTEGER NOT NULL,
            first_night     TEXT,
            computed_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, series_id, home_away, preseason)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_event_peer_effects_segment "
                     "ON event_peer_effects(series_id, home_away, preseason)")
        conn.commit()
    finally:
        conn.close()


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


def _memoised(key, build):
    hit = _memo.get(key)
    if hit and time.monotonic() - hit[0] < _MEMO_SECONDS:
        return hit[1]
    val = build()
    if len(_memo) > 2000:
        _memo.clear()
    _memo[key] = (time.monotonic(), val)
    return val


# ── nightly: one restaurant's own medians ─────────────────────────────────

def member_effects(restaurant_id, db_path=store.DB_PATH, since=None) -> list:
    """[{"series_id", "home_away", "preseason", "median_lift_pct", "nights",
    "first_night"}] — this restaurant's own measured median per followed
    series, side and season class (engine.effect_for(exact=True)), its
    demo-era nights left out (`since`: jobs.learning_since_by_id())."""
    from intelligence.jobs import before_learning
    out = []
    for f in store.follows(restaurant_id, db_path=db_path):
        base = {"series_id": f["series_id"], "short_name": f.get("short_name"), "series_name": f.get("name"),
                "category": f.get("category"), "event_date": None, "id": None, "is_primetime": 0}
        for side in SIDES:
            for pre in (False, True):
                e = dict(base, home_away=side, season_type="preseason" if pre else "regular")
                eff = engine.effect_for(restaurant_id, e, db_path=db_path, exact=True,
                                        keep=lambda d: not before_learning(restaurant_id, d, since))
                if not eff:
                    continue
                out.append({"series_id": int(f["series_id"]), "home_away": side, "preseason": int(pre),
                            "median_lift_pct": float(eff["median_lift_pct"]), "nights": int(eff["n"]),
                            "first_night": min(eff["dates"]) if eff.get("dates") else None})
    return out


def store_member_effects(restaurant_id, db_path=store.DB_PATH, real=None, since=None) -> int:
    """Replace this restaurant's rows in event_peer_effects with tonight's
    medians; a restaurant that may not teach (`real`: jobs.real_restaurant_ids())
    keeps none. Returns the rows written. Raises to its caller (the features
    pass captures one restaurant's failure and moves on)."""
    from intelligence.jobs import learning_since_by_id, real_restaurant_ids
    rid = int(restaurant_id)
    real = real_restaurant_ids(db_path=db_path) if real is None else real
    rows = []
    if rid in real:
        since = learning_since_by_id(db_path=db_path) if since is None else since
        rows = member_effects(rid, db_path=db_path, since=since)
    conn = store.get_conn(db_path)
    try:
        conn.execute("DELETE FROM event_peer_effects WHERE restaurant_id=?", (rid,))
        conn.executemany(
            "INSERT INTO event_peer_effects (restaurant_id, series_id, home_away, preseason, median_lift_pct, "
            "nights, first_night, computed_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
            [(rid, r["series_id"], r["home_away"], r["preseason"], r["median_lift_pct"], r["nights"],
              r["first_night"]) for r in rows])
        conn.commit()
    finally:
        conn.close()
    return len(rows)


# ── on request: the viewer's exclusion and the floors ─────────────────────

def _segment_rows(series_id, side, pre, db_path):
    """[(restaurant_id, median_lift_pct, first_night)] of the segment's
    materialised members that still follow the series."""
    conn = store.get_conn(db_path)
    try:
        return [(int(r["restaurant_id"]), float(r["median_lift_pct"]), r["first_night"]) for r in conn.execute(
            "SELECT p.restaurant_id, p.median_lift_pct, p.first_night FROM event_peer_effects p "
            "JOIN event_follows f ON f.restaurant_id=p.restaurant_id AND f.series_id=p.series_id AND f.active=1 "
            "WHERE p.series_id=? AND p.home_away=? AND p.preseason=?",
            (int(series_id), side, int(bool(pre)))).fetchall()]
    finally:
        conn.close()


def plan_words() -> str:
    """When Cavnar AI plans on this restaurant's own game nights instead: the
    forecast applies a label once event_memory.measured_effect `applies`
    (EFFECT_MIN_N nights) and its median moves sales past EFFECT_FLOOR_PCT
    (event_memory.effects_for_day) — re-audit P4-09: the line said "once 2",
    the floor for SAYING a segment (engine.SEGMENT_MIN_N), not for planning."""
    import event_memory
    return (f"Cavnar AI plans on your own nights once {event_memory.EFFECT_MIN_N} games like it are measured "
            f"here and move sales by {event_memory.EFFECT_FLOOR_PCT}% or more")


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
        side = e.get("home_away")
        if side not in SIDES or e.get("series_id") is None:
            return None
        # A home game at another ground (store.alt_venue: the Fire at
        # SeatGeek Stadium) is its own segment (re-audit SD-02), and no
        # member's figure for it is materialised — the home rows are the
        # home ground's crowd, never to be said as "home games at SeatGeek
        # Stadium" (re-audit 2 R4-02). No peer figure for it.
        if store.alt_venue(e):
            return None
        pre = e.get("season_type") == "preseason"
        orgs, real, since = _memoised(("ctx", dbp), lambda: (privacy.org_map(db_path=dbp),
                                                             real_restaurant_ids(db_path=db_path),
                                                             learning_since_by_id(db_path=db_path)))
        rows = _memoised(("seg", dbp, int(e["series_id"]), side, pre),
                         lambda: _segment_rows(e["series_id"], side, pre, db_path))
        me = int(restaurant_id)
        mine = orgs.get(me)
        lifts, members = [], []
        for rid, lift, first in rows:
            # The viewer, its whole organisation, anyone who may not teach
            # now, and a row whose nights reach into the member's demo era
            # (its learning_since moved after the row was written).
            if rid == me or rid not in real or (mine is not None and orgs.get(rid) == mine) \
                    or before_learning(rid, first, since):
                continue
            lifts.append(lift)
            members.append(orgs.get(rid, f"r{rid}"))
        n_orgs, share = privacy.org_counts(members)
        if len(lifts) < MIN_QUARTILE_N or not privacy.orgs_ok(n_orgs, share):
            return None
        med = _round(harrell_davis(lifts, 50))
        short = e.get("short_name") or e.get("series_name") or ""
        words = engine.kind_words(dict(e, is_primetime=0))
        return privacy.assert_anonymous({
            "segment": f"{short} {words}".strip(), "median_lift_pct": med, "n": MIN_QUARTILE_N,
            "claim_kind": "computed",
            "text": (f"Across {MIN_QUARTILE_N} or more other restaurants that follow the {short}, {words} have "
                     f"run about {med:+d}% against a usual same weekday — their nights, not yours; "
                     f"{plan_words()}")})
    except Exception as ex:
        log.warning("event_intel.peers failed rid=%s: %s", restaurant_id, ex)
        return None
