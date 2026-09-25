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
    # Types Cavnar guessed from the names: the strength is capped at 74%.
    "tacos_inferred": {"category": "mexican", "set": False, "n": 12,
                       "base": {"labor_pct_28d": 26.0}, "viewer": {"labor_pct_28d": 24.0}},
    # Guessed types, the viewer at the middle: a ranking word is rewritten.
    "tacos_middle": {"category": "mexican", "set": False, "n": 12,
                     "base": {"labor_pct_28d": 26.0}, "viewer": {"labor_pct_28d": 27.1}},
    # A lone sushi bar among 12 pizza places: only the all-types band, and
    # only for a behaviour metric.
    "platform": {"category": "sushi", "set": True, "n": 12, "alone": True,
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

    def add(rid_name, category, vals, set_type=True, email="x"):
        rid = models.create_restaurant(models.Restaurant(name=rid_name, owner_email=f"{email}@x.test"), db_path=db)
        conn = models.get_conn(db)
        if set_type:
            conn.execute("UPDATE restaurants SET category=? WHERE id=?", (category, rid))
        conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, completeness) "
                     "VALUES (?,?,?,1.0)", (rid, week, json.dumps(vals)))
        conn.commit()
        conn.close()
        return rid

    peer_cat = "pizza" if sc.get("alone") else sc["category"]
    names = {"pizza": "Pizza", "mexican": "Tacos", "sushi": "Sushi"}
    cohorts = {}
    for i in range(sc["n"]):
        rid = add(f"{names[peer_cat]} Peer {i}", peer_cat, {k: round(v + i * 0.2 if v > 1 else v + i * 0.02, 3)
                                                            for k, v in sc["base"].items()},
                  set_type=sc["set"] or sc.get("alone"), email=f"p{i}")
        cohorts[rid] = peer_cat
    viewer = add(f"{names[sc['category']]} Viewer", sc["category"], sc["viewer"], set_type=sc["set"], email="v")
    cohorts[viewer] = sc["category"]
    bm.compute(db_path=db, cohorts=cohorts)
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
