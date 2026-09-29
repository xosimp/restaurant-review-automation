"""Memory fix round 9/29/26, workstream M8 — what a recommendation's prior
and its record rest on (rec_learning, intelligence.scoring):

  prior_ladder  concept → the confirmed partition of the kind's family →
                every restaurant on Cavnar AI for a BEHAVIOUR kind only;
                the finest rung that clears the floors; the rung is said
                (prior_rung, the learned block) and the all-types rung is
                never an owner-facing figure (PLATFORM-1).
  decay         one window rule — every episode inside the horizon, weighed
                by the kind's decay, same-season weighting for seasonal
                kinds — for the rankers, Historical Accuracy, the priors and
                Ask's memory (PLATFORM-11).
"""
import sys
from datetime import datetime, timedelta

import pytest

import models
import rec_ledger as rl
import rec_learning
from models import Restaurant, create_restaurant

import intelligence  # noqa: E402
from intelligence import feedback, scoring, jobs, memory, categories  # noqa: E402
import confidence_engine as ce  # noqa: E402


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import auth
    auth.init_auth(db_path=db_path)
    scoring.invalidate_org_map()
    jobs.invalidate_excluded()
    yield db_path
    scoring.invalidate_org_map()
    jobs.invalidate_excluded()


def _rid(db, name, profile=None, **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.replace(' ', '').lower()}@x.test", **kw),
                            db_path=db)
    if profile:
        sm, concept = profile
        models.update_restaurant(rid, {"service_model": sm, "concept": concept, "category": concept,
                                       "profile_source": "set", "profile_confirmed_at": "2026-09-01T00:00:00"})
    return rid


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _rows(db, rid, kind, results, answers=("done",), days_ago=20, cohort=None, partition=None):
    """Peer rows as the sync files them: answers per episode, then measured
    results per tracker."""
    at = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    for i, a in enumerate(answers):
        _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, partition_key, action, "
               "event_at) VALUES (?,?,?,?,?,?,?)", (rid, kind, f"{kind}:x#e{'%032x' % (rid * 100 + i)}", cohort,
                                                   partition, a, at))
    for i, o in enumerate(results):
        _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, cohort, partition_key, action, "
               "outcome, event_at) VALUES (?,?,?,?,?, 'measured', ?, ?)",
           (rid, kind, f"{kind}:x#o{rid * 100 + i}", cohort, partition, o, at))


# ══ prior_ladder ═════════════════════════════════════════════════════════════

def test_a_new_pizzeria_borrows_from_its_service_model_partition_when_too_few_pizzerias_measured(db):
    me = _rid(db, "New Pie", profile=("full_service", "pizza"))
    for i in range(3):                                   # three pizzerias: below the floor
        _rows(db, _rid(db, f"Pie Peer {i}", profile=("full_service", "pizza")), "trim_day",
              ["improved", "improved"], cohort="pizza", partition="sm:full_service")
    for i in range(6):                                   # six other full-service restaurants
        _rows(db, _rid(db, f"Full Peer {i}", profile=("full_service", "italian")), "trim_day",
              ["improved", "worsened"], cohort="italian", partition="sm:full_service")
    m = rec_learning.effectiveness(me, db_path=db)
    acc, suc = m.prior("trim_day")
    rung = m.prior_rung("trim_day")
    assert rung["success"] == "partition" and rung["acceptance"] == "partition"
    assert rung["label"] == categories.partition_label("sm:full_service")
    assert suc != m.base_rate("trim_day")
    # Historical Accuracy's prior is a NAMED group: the partition, labelled.
    rec = rec_learning.kind_record(me, "trim_day", db_path=db)
    assert rec["prior_rung"] == "partition" and rec["prior_measured"] >= rec_learning.PRIOR_MIN_MEASURED
    assert rec["prior_label"] == categories.partition_label("sm:full_service") and rec["prior_unlock"] is None


def test_the_concept_rung_comes_first_when_it_clears_the_floors(db):
    me = _rid(db, "Pie Home", profile=("full_service", "pizza"))
    for i in range(6):
        _rows(db, _rid(db, f"Pie Mate {i}", profile=("full_service", "pizza")), "trim_day",
              ["improved", "improved"], cohort="pizza", partition="sm:full_service")
    m = rec_learning.effectiveness(me, db_path=db)
    m.prior("trim_day")
    assert m.prior_rung("trim_day")["success"] == "concept"


def test_a_bar_led_member_stands_in_its_service_models_group(db):
    me = _rid(db, "Plain Room", profile=("full_service", "italian"))
    for i in range(6):
        r = _rid(db, f"Bar Room {i}", profile=("full_service", "steakhouse"))
        models.update_restaurant(r, {"bar_led": 1})
        _rows(db, r, "trim_day", ["improved", "improved"], partition="sm:full_service|bar")
    s = scoring.kind_stats("trim_day", partition="sm:full_service", db_path=db, exclude_restaurant_id=me)
    assert s["measured_restaurants"] == 6
    only_bar = scoring.kind_stats("trim_day", partition="sm:full_service|bar", db_path=db)
    assert only_bar["measured_restaurants"] == 6
    assert scoring.kind_stats("trim_day", partition="sm:counter", db_path=db)["measured_restaurants"] == 0


def test_the_all_types_rung_is_for_behaviour_kinds_only_and_never_an_owner_facing_figure(db):
    me = _rid(db, "Unsure Place")                        # never confirmed a profile
    for i in range(6):
        r = _rid(db, f"Poster {i}")
        _rows(db, r, "post_this_week", ["improved", "improved"])
        _rows(db, r, "trim_day", ["improved", "improved"])
    assert scoring.kind_comparability("post_this_week") == "behaviour"
    assert scoring.kind_comparability("trim_day") == "economics"
    m = rec_learning.effectiveness(me, db_path=db)
    m.prior("post_this_week")
    assert m.prior_rung("post_this_week")["success"] == "platform"
    assert m.prior_rung("post_this_week")["unlock"] == "confirm_profile"
    # an economics kind never borrows across types
    assert m.prior("trim_day") == (0.5, m.base_rate("trim_day"))
    assert m.prior_rung("trim_day")["success"] is None
    # Historical Accuracy never quotes the all-types record
    rec = rec_learning.kind_record(me, "post_this_week", db_path=db)
    assert rec["prior_measured"] == 0 and rec["prior_rung"] is None and rec["prior_unlock"] == "confirm_profile"
    # ... and says what would unlock a named group, on the K1 object itself
    assert ce.accuracy(rec)["prior_unlock"] == "confirm_profile"
    # a cold start ranks with help from every type, and says so without a count
    w, why = m.weight("post_this_week:Tuesday")
    assert w > 1.0 and why == ["ranked with help from restaurants of every type on Cavnar AI"]
    assert m.prior_rung("post_this_week")["cold"] == "platform"


def test_the_learned_block_carries_the_rung_and_the_model_version(db):
    me = _rid(db, "Logged Rung", profile=("full_service", "pizza"))
    for i in range(6):
        _rows(db, _rid(db, f"Rung Peer {i}", profile=("full_service", "italian")), "trim_day",
              ["improved", "improved"], partition="sm:full_service")
    m = rec_learning.effectiveness(me, db_path=db)
    note = rec_learning.learned_note(m, "trim_day:Monday", 1.1, ["x"])
    assert note["prior_rung"]["success"] == "partition" and note["version"] == rec_learning.EFFECTIVENESS_VERSION
    assert rec_learning.learned_note(lambda k: (1.0, []), "trim_day:Monday", 1.0, []) == {"weight": 1.0, "why": []}
    d = m.weight_detail("trim_day:Monday")
    assert set(d) == {"weight", "why", "prior_rung", "version"}


def test_a_confirmed_partition_is_stamped_beside_the_concept_on_every_row(db):
    rid = _rid(db, "Stamped Co", profile=("counter", "pizza"))
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db)
    rl.record(rid, "trim_day:Monday", "completed", surface="home", db_path=db)
    rl.present(rid, "cut_waste:Flour", "food", "home", db_path=db)
    rl.record(rid, "cut_waste:Flour", "completed", surface="home", db_path=db)
    feedback.sync(db_path=db)
    c = models.get_conn(db)
    rows = {feedback.base_key(r["source_key"]): (r["cohort"], r["partition_key"]) for r in
            c.execute("SELECT source_key, cohort, partition_key FROM intel_rec_events WHERE restaurant_id=?", (rid,))}
    c.close()
    assert rows["trim_day:Monday"] == ("pizza", "sm:counter")            # labor family
    assert rows["cut_waste:Flour"] == ("pizza", "sm:counter|starch")     # food family: × menu family


# ══ decay ════════════════════════════════════════════════════════════════════

def _ago(days, now):
    return (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def test_one_decay_rule_half_life_same_season_and_a_smooth_horizon():
    now = datetime(2026, 12, 15)
    assert rec_learning.decay_weight("trim_day", _ago(0, now), now) == 1.0
    assert rec_learning.decay_weight("trim_day", _ago(365, now), now) == pytest.approx(0.5, abs=1e-3)
    assert rec_learning.decay_weight("post_this_week", _ago(180, now), now) == pytest.approx(0.5, abs=1e-3)
    assert rec_learning.decay_weight("trim_day", _ago(rec_learning.DECAY_HORIZON_DAYS, now), now) == 0.0
    near = rec_learning.decay_weight("trim_day", _ago(rec_learning.DECAY_HORIZON_DAYS - 10, now), now)
    assert 0 < near < 0.05                                       # tapers, no cliff
    # last December's holiday result counts almost fully this December …
    same = rec_learning.decay_weight("holiday_promo", _ago(366, now), now)
    assert same == pytest.approx(rec_learning.SEASONAL_YEAR_WEIGHT)
    # … and far less in June
    june = datetime(2026, 6, 15)
    assert rec_learning.decay_weight("holiday_promo", _ago(182, june), june) < same
    assert rec_learning.decay_weight("trim_day", None, now) == 1.0


def test_the_windows_are_one_rule_inside_the_ledgers_retention():
    import ops
    assert rec_learning.EFFECT_WINDOW_DAYS == rec_learning.DECAY_HORIZON_DAYS == rec_learning.PRIOR_WINDOW_DAYS
    assert scoring.PRIOR_WINDOW_DAYS == rec_learning.PRIOR_WINDOW_DAYS
    assert rec_learning.DECAY_HORIZON_DAYS < ops._RETENTION_DAYS["rec_events"]


def _measured_episode(db, rid, key, verdict, days_ago):
    rec = rl.present(rid, key, "labor", "home", db_path=db)
    rl.record(rid, key, "accepted", surface="home", db_path=db)
    c = models.get_conn(db)
    cur = c.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
                    "evaluate_on, status, verdict, created_at) VALUES (?, 'recommendation', ?, 't', 'labor_pct', "
                    "date('now', ?), date('now', ?), 'evaluated', ?, datetime('now', ?))",
                    (rid, key, f"-{days_ago + 28} days", f"-{days_ago} days", verdict, f"-{days_ago + 28} days"))
    tid = cur.lastrowid
    at = f"-{days_ago + 30} days"
    c.execute("UPDATE rec_instances SET tracker_id=?, created_at=datetime('now', ?), last_event_at=datetime('now', ?), "
              "closed_at=datetime('now', ?) WHERE rec_id=?", (tid, at, at, at, rec))
    c.execute("UPDATE rec_events SET at=datetime('now', ?) WHERE rec_id=?", (at, rec))
    c.commit()
    c.close()
    return rec


def test_a_result_older_than_a_year_is_kept_and_counts_less(db):
    rid = _rid(db, "Long Memory")
    for i, days in enumerate((500, 440, 380, 90, 30)):          # after-windows that never overlap
        _measured_episode(db, rid, f"trim_day:D{i}", "improved", days_ago=days)
    rec = rec_learning.kind_record(rid, "trim_day", db_path=db)
    assert rec["measured"] == 5                    # the 365-day cutoff used to drop the three old ones
    assert 0 < rec["measured_eff"] < rec["measured"] and rec["window_days"] == rec_learning.DECAY_HORIZON_DAYS
    m = rec_learning.effectiveness(rid, db_path=db)
    s = m.kinds["trim_day"]
    assert s["measured"] == 5 and s["measured_w"] < 5 and s["settled_w"] < s["settled"]


def test_historical_accuracy_counts_each_result_by_its_age():
    base = {"measured": 6, "improved": 5, "source": "own", "base_rate": 0.05, "base_rate_source": "stated"}
    fresh = ce.accuracy(base)
    old = ce.accuracy(dict(base, measured_eff=3.0, improved_eff=2.5))
    assert old["pct"] < fresh["pct"] and old["n"] == 6 and "improved 5 of 6" in old["basis"]
    # a weight never exceeds one: a record cannot be inflated
    assert ce.accuracy(dict(base, measured_eff=60.0, improved_eff=60.0))["pct"] == fresh["pct"]


def test_asks_memory_reads_the_same_horizon(db):
    rid = _rid(db, "Ask Memory")
    now = datetime.utcnow()
    for i, days in enumerate((10, 40, 70, 100, 400, 900)):
        _x(db, "INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, outcome, event_at) "
               "VALUES (?, 'reprice', ?, 'measured', 'improved', ?)", (rid, f"reprice:x#o{i}",
                                                                      (now - timedelta(days=days)).strftime("%Y-%m-%d")))
    rec = memory.own_record(rid, db_path=db)
    assert rec["by_kind"]["reprice"]["measured"] == 5          # the 900-day-old result is past the horizon
    assert rec["worked"] == ["reprice"]
