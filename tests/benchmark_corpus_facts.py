"""Benchmark facts in their PRODUCTION shape, for the validation golden
corpus (Benchmarking audit 9/24/26, BM3-2 / BM3-4: the corpus built its
benchmark facts by hand — no cohort_label on a registry fact, a platform
group labelled "platform" — so cases passed while production dropped the
sentence). Every fact here comes out of the code that makes it in
production: benchmark_registry.facts(for_category(...)) and
intelligence.engine.facts(compare(...)) over a real throwaway database.

A context in contexts.json names them with "facts_from":
  ["registry", metric, category, "set" | "inferred"]
  ["engine", scenario, metric]            scenario: see SCENARIOS
"""
import json
import os
import shutil
import tempfile
from datetime import date

_CACHE = {}
AS_OF_STAMP = "2026-09-20 03:00:00"
AS_OF = "9/20/26"

# scenario → (viewer category, viewer category set by the owner?, cohort
# size, the cohort's base values, the viewer's own values)
SCENARIOS = {
    # 12 pizza restaurants whose type the owner set; the viewer is one of
    # them and sits in the top quarter on labor (lower is better).
    "pizza": {"category": "pizza", "set": True, "n": 12,
              "base": {"labor_pct_28d": 28.0, "reply_rate_30d": 0.5, "avg_rating_30d": 4.0},
              "viewer": {"labor_pct_28d": 27.0, "reply_rate_30d": 0.95, "avg_rating_30d": 4.1}},
    # The same group, the viewer right at the middle.
    "pizza_middle": {"category": "pizza", "set": True, "n": 12,
                     "base": {"labor_pct_28d": 28.0, "reply_rate_30d": 0.5},
                     "viewer": {"labor_pct_28d": 29.1, "reply_rate_30d": 0.61}},
    # A weak comparison: the viewer's own figure rests on under half its
    # measures (completeness 0.45), so the strength is 72% — under 75%. (These
    # were guessed types capped at 74%; a guessed type no longer makes a
    # group at all — Benchmarking audit #8, workstream P.)
    "tacos_inferred": {"category": "mexican", "set": True, "n": 12, "viewer_completeness": 0.45, "history": True,
                       "base": {"labor_pct_28d": 26.0}, "viewer": {"labor_pct_28d": 24.0}},
    # A weak comparison, the viewer at the middle: a ranking word is rewritten.
    "tacos_middle": {"category": "mexican", "set": True, "n": 12, "viewer_completeness": 0.45,
                     "base": {"labor_pct_28d": 26.0}, "viewer": {"labor_pct_28d": 27.1}},
    # The re-audit's probe restaurant (R3-1 to R3-3, 9/24/26): the same group,
    # the viewer in the WORST quarter on labor % and on rating, and in the
    # best quarter on labor hours per $1k — so a claim bound to the wrong
    # metric, or read in the wrong direction, is visible.
    "pizza_bottom": {"category": "pizza", "set": True, "n": 12,
                     "base": {"labor_pct_28d": 28.0, "avg_rating_30d": 4.2, "labor_hours_per_1k_28d": 3.0},
                     "steps": {"avg_rating_30d": 0.04},
                     "viewer": {"labor_pct_28d": 33.4, "avg_rating_30d": 3.9, "labor_hours_per_1k_28d": 2.5}},
    # A strong comparison (fix round, re-audit #15/#16): the size of the group
    # now caps the strength (about 20 others reach ranking level), and a
    # quartile word needs the viewer's own week-to-week swing — so this group
    # is 24 restaurants from 24 owners and the viewer has 16 weeks of history.
    "pizza_strong": {"category": "pizza", "set": True, "n": 24, "history": True,
                     "steps": {"reply_rate_30d": 0.01, "labor_pct_28d": 0.1},
                     "base": {"labor_pct_28d": 28.0, "reply_rate_30d": 0.5},
                     "viewer": {"labor_pct_28d": 26.5, "reply_rate_30d": 0.95}},
    # A lone sushi bar among 12 pizza places: only the all-types band, and
    # only for a behaviour metric.
    "platform": {"category": "sushi", "set": True, "n": 12, "alone": True, "history": True,
                 "base": {"reply_rate_30d": 0.5, "labor_pct_28d": 28.0},
                 "viewer": {"reply_rate_30d": 0.95, "labor_pct_28d": 22.0}},
}


def _db():
    import conftest
    path = os.path.join(tempfile.mkdtemp(prefix="cavnar-bench-corpus-"), "bench.db")
    shutil.copyfile(conftest._db_template(), path)
    return path


def _scenario(name):
    key = ("scenario", name)
    if key in _CACHE:
        return _CACHE[key]
    import models
    from intelligence import benchmarks as bm
    from intelligence import features as feat
    sc = SCENARIOS[name]
    db = _db()
    week = feat.iso_week(date.today())

    from datetime import timedelta
    live = (date.today() - timedelta(days=120)).isoformat() + "T00:00:00"

    def add(rid_name, category, vals, set_type=True, email="x", completeness=1.0, history=False):
        # Live 17 weeks, a real labor cost basis, and — when the type is set —
        # an owner-confirmed profile: the peer group is the confirmed partition
        # (counter service here; the lone sushi bar is full service).
        rid = models.create_restaurant(models.Restaurant(name=rid_name, owner_email=f"{email}@x.test",
                                                         created_at=live, hourly_rate=18.0), db_path=db)
        conn = models.get_conn(db)
        if set_type:
            conn.execute("UPDATE restaurants SET category=?, concept=?, service_model=?, profile_source='set' "
                         "WHERE id=?", (category, category, "full_service" if category == "sushi" else "counter",
                                        rid))
        conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, completeness) "
                     "VALUES (?,?,?,?)", (rid, week, json.dumps(vals), completeness))
        if history:
            # Its own weekly history, steady with a small swing, so the engine
            # knows its own noise (standing() needs it for a quartile word).
            for back in range(1, 17):
                wk = feat.iso_week(date.today() - timedelta(weeks=back))
                jig = (1 if back % 2 else -1)
                past = {k: round(v + jig * (0.1 if v > 1 else 0.005), 3) for k, v in vals.items()}
                conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, "
                             "completeness) VALUES (?,?,?,?)", (rid, wk, json.dumps(past), completeness))
        conn.commit()
        conn.close()
        return rid

    peer_cat = "pizza" if sc.get("alone") else sc["category"]
    names = {"pizza": "Pizza", "mexican": "Tacos", "sushi": "Sushi"}
    cohorts = {}
    steps = sc.get("steps") or {}
    for i in range(sc["n"]):
        rid = add(f"{names[peer_cat]} Peer {i}", peer_cat,
                  {k: round(v + i * steps.get(k, 0.2 if v > 1 else 0.02), 3) for k, v in sc["base"].items()},
                  set_type=sc["set"] or sc.get("alone"), email=f"p{i}")
        cohorts[rid] = peer_cat
    viewer = add(f"{names[sc['category']]} Viewer", sc["category"], sc["viewer"], set_type=sc["set"], email="v",
                 completeness=sc.get("viewer_completeness", 1.0), history=sc.get("history", False))
    cohorts[viewer] = sc["category"]
    bm.compute(db_path=db)          # each one's confirmed partition
    # A fixed as-of date, so a case can quote it: the band's week stays this
    # week (its age and strength are real), only its computed stamp is set.
    conn = models.get_conn(db)
    conn.execute("UPDATE intel_benchmarks SET computed_at=?", (AS_OF_STAMP,))
    conn.commit()
    conn.close()
    _CACHE[key] = (db, viewer)
    return _CACHE[key]


def engine_facts(scenario, metric) -> list:
    key = ("engine", scenario, metric)
    if key not in _CACHE:
        from intelligence import engine
        db, viewer = _scenario(scenario)
        out = engine.compare(viewer, metric, kinds=("peers", "platform", "industry"), db_path=db)
        _CACHE[key] = out["facts"]
    return [json.loads(json.dumps(f)) for f in _CACHE[key]]


def engine_comparison(scenario, metric) -> dict:
    from intelligence import engine
    db, viewer = _scenario(scenario)
    return engine.compare(viewer, metric, kinds=("peers", "platform", "industry"), db_path=db)


def registry_facts(metric, category, source="set") -> list:
    import benchmark_registry
    return benchmark_registry.facts(benchmark_registry.for_category(metric, category, source))


def resolve(spec) -> list:
    out = []
    for item in spec or ():
        if item[0] == "registry":
            out += registry_facts(*item[1:])
        elif item[0] == "engine":
            out += engine_facts(*item[1:])
        else:
            raise ValueError(f"unknown facts_from {item!r}")
    return out
