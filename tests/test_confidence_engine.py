"""The Recommendation Confidence engine (confidence audit, group E):
confidence_engine (pure), data_freshness (the one source registry),
rec_trust (assess), the ledger snapshot, and the surfaces that carry it.

The owner's rule (binding): the UI keeps percentages, and every percentage
is a real computed measure — below a sample floor the dimension is null
with a basis saying what is needed, never a stand-in figure."""
import ast
import json
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

import confidence_engine as ce
import data_freshness
import models
import rec_ledger
import rec_learning
import rec_trust
from models import create_restaurant, Restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 9, 24, 17, 0, tzinfo=timezone.utc)


def _src(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _rid(db, **kw):
    fields = dict(name="Trust Co", owner_email="t@x.com", module_reviews=1, module_labor=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db)


def _ep(key, verdict, i, status="completed"):
    return {"rec_id": f"r{i}", "key": key, "kind": key.split(":")[0], "shown": True, "state": status,
            "verdict": verdict, "tracker": {}}


# ── constants held in step with the modules they mirror ────────────────────

def test_engine_floors_match_the_ledger_and_outcomes():
    import outcomes
    assert ce.MIN_MEASURED == rec_learning.MIN_MEASURED_FOR_RATE
    assert ce.PRIOR_MIN_MEASURED == rec_learning.PRIOR_MIN_MEASURED
    assert ce.MIN_COVERAGE == outcomes.MIN_COVERAGE
    assert ce.SHRINK_K == rec_learning.SHRINK_K


# ── Evidence Strength ──────────────────────────────────────────────────────

def test_evidence_is_computed_from_n_coverage_and_quality():
    assert ce.evidence(n=8, kind="reviews")["pct"] == 100
    assert ce.evidence(n=4, kind="reviews")["pct"] == 50
    assert ce.evidence(n=3, kind="waste_weeks")["pct"] == 75
    # a setup step has no sample: not measurable, never a guess
    assert ce.evidence(n=None, basis="a setup step")["pct"] is None
    # sample data scores 0 and says so
    s = ce.evidence(n=50, kind="reviews", sample=True)
    assert s["pct"] == 0 and "Sample" in s["basis"]


@pytest.mark.parametrize("kw", [
    {"coverage": 0.69},
    {"flags": ("days_missing_sales",)},
    {"flags": ("hours_are_estimated",)},
    {"flags": ("gross_missing",)},
    {"flags": ("inferred",)},
    {"unverified": 1},
])
def test_evidence_never_reads_high_with_partial_data_or_unverified_figures(kw):
    e = ce.evidence(n=28, kind="trading_days", **kw)
    assert e["pct"] < ce.HIGH_AT


def test_a_model_band_only_ever_lowers_evidence():
    for n in (1, 3, 8, 20):
        base = ce.evidence(n=n, kind="reviews")["pct"]
        for band in ("high", "medium", "low"):
            assert ce.evidence(n=n, kind="reviews", model_band=band)["pct"] <= base
    # "High confidence — a diagnosis read from 3 reviews" is gone (CA4 F2)
    assert ce.evidence(n=3, kind="reviews", model_band="high")["pct"] == 38
    assert ce.evidence(n=20, kind="reviews", model_band="medium")["pct"] == 65


# ── Historical Accuracy ────────────────────────────────────────────────────

def test_no_accuracy_percentage_below_the_own_floor_or_the_cohort_floor():
    for n in range(0, ce.MIN_MEASURED):
        a = ce.accuracy({"measured": n, "improved": n, "source": "none"})
        assert a["pct"] is None and f"{n} measured, needs {ce.MIN_MEASURED}" in a["basis"]
    # a mislabelled source never slips a figure through under the floor
    assert ce.accuracy({"measured": 3, "improved": 3, "source": "own"})["pct"] is None
    assert ce.accuracy({"measured": 0, "source": "cohort", "prior_measured": 9, "prior_improved": 9})["pct"] is None
    c = ce.accuracy({"measured": 1, "source": "cohort", "prior_measured": 15, "prior_improved": 12})
    assert c["source"] == "cohort" and c["pct"] is not None and "restaurants like yours: 12 of 15" in c["basis"]


def test_five_worsened_results_read_low_accuracy_never_one_hundred():
    """The CA6 probe replayed: five results that got worse — one the owner
    disowned, one that reversed at its re-check — scored 1.0 in the old
    `historical_accuracy`. Through learned_verdict and kind_record it is
    low, and none of them is a win."""
    raw = [("worsened", {}, None), ("worsened", {}, None), ("worsened", {}, None),
           ("improved", {"recheck_verdict": "reversed"}, None),                  # reversed: not a win
           ("improved", {}, {"did_it": "no"}),                                   # disowned: unknown
           ("improved", {}, {"conditions_changed": True}),                       # conditions changed
           ("improved", {"source_key": "observed:alert_labor_over"}, None),      # informational
           ("worsened", {}, None), ("worsened", {}, None)]
    eps = [_ep("cut_waste:x", rec_learning.learned_verdict(v, tr, ck), i) for i, (v, tr, ck) in enumerate(raw)]
    rec = rec_learning.kind_record(1, "cut_waste", episodes=eps)
    assert rec["improved"] == 0 and rec["measured"] == 6 and rec["source"] == "own"
    a = ce.accuracy(rec)
    assert a["pct"] < ce.MEDIUM_AT and a["pct"] != 100 and a["high"] < 50


def test_accuracy_counts_only_shown_and_taken_episodes():
    eps = [_ep("trim_day:Mon", "improved", i) for i in range(5)]
    eps += [dict(_ep("trim_day:Tue", "improved", 10 + i), shown=False) for i in range(5)]
    eps += [_ep("trim_day:Wed", "improved", 20 + i, status="dismissed") for i in range(5)]
    rec = rec_learning.kind_record(1, "trim_day", episodes=eps)
    assert rec["measured"] == 5


# ── Data Freshness ─────────────────────────────────────────────────────────

_CONNECT = {"toast": {"toast_restaurant_guid": "g"}, "square": {"square_access_token": "s"},
            "clover": {"clover_api_token": "c"}, "rpower": {"rpower_token": "t", "rpower_store_mid": "m"}}


def test_pos_freshness_drops_past_its_threshold_for_every_provider(db):
    import pos
    assert set(_CONNECT) == set(pos.get_providers().keys())
    for name, cols in _CONNECT.items():
        fresh = dict(cols, id=1, **{f"{name}_last_synced": (NOW - timedelta(hours=20)).isoformat()})
        stale = dict(cols, id=1, **{f"{name}_last_synced": (NOW - timedelta(days=9)).isoformat()})
        f = data_freshness.source_state(fresh, "pos", db_path=db, now=NOW)
        s = data_freshness.source_state(stale, "pos", db_path=db, now=NOW)
        assert f["state"] == "current" and f["pct"] == 100, name
        assert s["state"] == "stale" and s["pct"] < ce.AGING_AT, name
        assert re.match(r"^\d{1,2}/\d{1,2}/\d{2}$", f["as_of"]) and f["as_of_iso"][4] == "-"
        # a sync error halves it whatever the age
        err = dict(fresh, **{f"{name}_sync_error": "401"})
        assert data_freshness.source_state(err, "pos", db_path=db, now=NOW)["pct"] <= 50, name


def test_an_unknown_age_is_never_current(db):
    rid = _rid(db)
    c = models.get_conn(db)
    c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) VALUES (?,?,?,?,1)",
              (rid, "Flour", "lb", 1.0))                                 # on file, never counted
    c.commit(); c.close()
    for key, row in (("pos", {"id": 1, "square_access_token": "x"}),
                     ("reviews", {"id": 1, "reviews_live": 1}),
                     ("inventory", None)):
        st = data_freshness.source_state(row or {"id": rid}, key, db_path=db, now=NOW)
        assert st["state"] != "current" and not st["pct"], key
    # a restaurant with no shifts at all: the source does not apply (not stale)
    assert data_freshness.source_state({"id": rid}, "labor", db_path=db, now=NOW)["state"] == "not_connected"
    assert ce.recency(None, 1, 7) == 0.0


def test_labor_is_dated_by_the_last_day_it_covers_not_the_upload(db):
    """CA3 F2: shifts ending 20 days ago, uploaded today, are 20 days old."""
    rid = _rid(db)
    c = models.get_conn(db)
    end = (NOW - timedelta(days=20)).date()
    for i in range(10):
        d = (end - timedelta(days=i)).isoformat()
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_cost, labor_pct) "
                  "VALUES (?,?,?,?,?)", (rid, d, 1000.0 if i < 4 else 0.0, 300.0, 30.0))
    c.execute("INSERT OR REPLACE INTO client_data (restaurant_id, updated_at) VALUES (?, datetime('now'))", (rid,))
    c.commit(); c.close()
    st = data_freshness.source_state({"id": rid}, "labor", db_path=db, now=NOW)
    assert st["as_of_iso"] == end.isoformat() and st["state"] == "stale"
    assert "4 of 10 days carry sales" in st["basis"]


def test_inventory_is_dated_by_the_oldest_count(db):
    rid = _rid(db)
    c = models.get_conn(db)
    for name, age in (("Flour", 3), ("Salmon", 40)):
        c.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active, last_recount_at) "
                  "VALUES (?,?,?,?,1,?)", (rid, name, "lb", 1.0, (NOW - timedelta(days=age)).date().isoformat()))
    c.commit(); c.close()
    st = data_freshness.source_state({"id": rid}, "inventory", db_path=db, now=NOW)
    assert st["lag_days"] == 40 and st["state"] == "stale"


def test_freshness_is_the_minimum_over_sources_and_dates_read_mdy():
    fr = ce.freshness([{"key": "pos", "pct": 100, "as_of_iso": "2026-09-23", "basis": "Toast synced 9/23/26"},
                       {"key": "labor", "pct": 30, "as_of_iso": "2026-09-04", "basis": "Shifts through 9/4/26"},
                       {"key": "x", "pct": None}])
    assert fr["pct"] == 30 and fr["stalest"] == "labor" and fr["as_of"] == "9/4/26"
    assert ce.freshness([])["pct"] is None


# ── the overall figure and assess() ────────────────────────────────────────

def test_overall_is_capped_at_seventy_without_a_track_record():
    ev = ce.evidence(n=40, kind="reviews")
    k1 = ce.assemble(ev, ce.accuracy(None), ce.freshness([{"key": "reviews", "pct": 100, "as_of_iso": "2026-09-24"}]))
    assert k1["pct"] == ce.NO_TRACK_RECORD_CAP and k1["band"] == "medium" and k1["caution"]
    stale = ce.assemble(ev, ce.accuracy({"measured": 9, "improved": 9, "source": "own"}),
                        ce.freshness([{"key": "pos", "pct": 20, "as_of_iso": "2026-09-01", "basis": "old"}]))
    assert stale["pct"] <= ce.STALE_CAP and stale["band"] == "low"
    nothing = ce.assemble(ce.evidence(n=None), ce.accuracy(None), ce.freshness([]))
    assert nothing["pct"] is None and nothing["label"] == "Confidence not yet measurable" and nothing["score"] == 0.0


def test_assess_is_deterministic_never_raises_and_score_is_a_number(db, monkeypatch):
    rid = _rid(db)
    ev = {"n": 6, "kind": "reviews", "basis": "6 reviews in 90 days"}
    a = rec_trust.assess(rid, "top_issue:service", evidence=ev, sources=("reviews",), db_path=db)
    b = rec_trust.assess(rid, "top_issue:service", evidence=ev, sources=("reviews",), db_path=db)
    assert a == b and isinstance(a["score"], float) and a["version"] == ce.VERSION
    assert set(a) >= {"pct", "band", "label", "reason", "score", "caution", "dimensions"}
    monkeypatch.setattr(rec_learning, "kind_record", lambda *x, **k: (_ for _ in ()).throw(RuntimeError("x")))
    broken = rec_trust.assess(rid, "top_issue:service", evidence=ev, db_path=db)
    assert broken["band"] == "low" and broken["pct"] is None and broken["score"] == 0.0


def test_dont_trust_data_caps_that_kinds_evidence_for_thirty_days(db):
    rid = _rid(db)
    rec_ledger.present_many(rid, [{"key": "trim_day:Monday", "module": "labor"}], "home", db_path=db)
    rec_ledger.record(rid, "trim_day:Monday", "dismissed", meta={"kind": "not_for_us", "reason_code": "dont_trust_data"},
                      db_path=db)
    ev = {"n": 8, "kind": "weekdays"}
    k1 = rec_trust.assess(rid, "trim_day:Friday", evidence=ev, db_path=db)
    assert k1["dimensions"]["evidence"]["pct"] == rec_trust.DISTRUST_CAP
    assert "don't trust the data" in k1["dimensions"]["evidence"]["basis"]
    other = rec_trust.assess(rid, "cut_waste:Salmon", evidence={"n": 8, "kind": "weekdays"}, db_path=db)
    assert other["dimensions"]["evidence"]["pct"] == 100


# ── the snapshot at delivery (K3) ──────────────────────────────────────────

def test_present_many_snapshots_the_confidence_and_logs_moves(db):
    rid = _rid(db)
    conf = ce.assemble(ce.evidence(n=8, kind="reviews"), ce.accuracy(None), ce.freshness([]))
    rec_ledger.present_many(rid, [{"key": "top_issue:service", "module": "reviews", "confidence": conf},
                                  {"key": "post_this_week", "module": "marketing"}], "home", db_path=db)
    c = models.get_conn(db)
    rows = {r["key"]: dict(r) for r in c.execute("SELECT * FROM rec_instances").fetchall()}
    assert rows["top_issue:service"]["confidence_pct"] == 70 and rows["top_issue:service"]["evidence_pct"] == 100
    assert rows["top_issue:service"]["trust_version"] == ce.VERSION
    assert rows["top_issue:service"]["confidence_band"] == "medium"
    # shown with no confidence: nothing shown is recorded as said, but the
    # accuracy and freshness it would have had are, for calibration
    assert rows["post_this_week"]["confidence_pct"] is None and rows["post_this_week"]["trust_version"] == ce.VERSION
    meta = json.loads(c.execute("SELECT meta FROM rec_events WHERE key='top_issue:service' AND event='shown'")
                      .fetchone()["meta"])
    assert meta["confidence_pct"] == 70 and meta["evidence_pct"] == 100
    c.close()
    # a later showing whose confidence moved 10+ points notes the move
    lower = ce.assemble(ce.evidence(n=2, kind="reviews"), ce.accuracy(None), ce.freshness([]))
    rec_ledger.present_many(rid, [{"key": "top_issue:service", "module": "reviews", "confidence": lower}],
                            "brief_email", db_path=db)
    c = models.get_conn(db)
    metas = [json.loads(r["meta"]) for r in c.execute("SELECT meta FROM rec_events WHERE key='top_issue:service' "
                                                       "AND event='shown' ORDER BY id").fetchall()]
    snap = c.execute("SELECT confidence_pct FROM rec_instances WHERE key='top_issue:service'").fetchone()[0]
    c.close()
    assert metas[-1]["confidence_moved"] == {"from": 70, "to": lower["pct"]}
    assert snap == 70                            # the first showing's figure is kept


def test_feedback_sync_fills_confidence_at_from_the_snapshot(db):
    from intelligence import feedback
    rid = _rid(db)
    conf = ce.assemble(ce.evidence(n=8, kind="reviews"), ce.accuracy(None), ce.freshness([]))
    rec_ledger.present_many(rid, [{"key": "top_issue:service", "module": "reviews", "confidence": conf}], "home",
                            db_path=db)
    rec_ledger.record(rid, "top_issue:service", "accepted", db_path=db)
    feedback.sync(db_path=db)
    c = models.get_conn(db)
    row = c.execute("SELECT confidence_at FROM intel_rec_events WHERE source_key='top_issue:service' "
                    "AND action='accepted'").fetchone()
    c.close()
    assert row and row[0] == 0.7


def _calls(tree, names):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if name in names:
                yield node


def _dict_keys(node):
    keys = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Dict):
            keys |= {k.value for k in n.keys if isinstance(k, ast.Constant)}
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "dict":
            keys |= {kw.arg for kw in n.keywords if kw.arg}
    return keys


# The surfaces whose items carry their K1 confidence to the ledger. Every
# other present_many caller is snapshotted by the ledger itself (accuracy +
# freshness, rec_ledger._snapshots) until it carries a confidence of its own.
CARRY_CONFIDENCE = {"home_brief.py", "strategy_routes.py:cross_module", "insight_store.py", "dsr/narrative.py"}


def test_every_presenting_surface_that_shows_a_confidence_passes_it_to_the_ledger():
    tree = ast.parse(_src("home_brief.py"))
    batches = [c for c in _calls(tree, {"only_presentable"})]
    assert batches and all("confidence" in _dict_keys(b) for b in batches)
    assert '"confidence": ff.get("confidence")' in _src("strategy_routes.py")
    assert '"confidence": it.get("confidence")' in _src("insight_store.py")
    assert '"confidence": a.get("confidence")' in _src("dsr/narrative.py")
    # ...and the ledger snapshots every other caller itself, so no
    # present_many call site ships without a snapshot.
    assert "snaps = _snapshots(restaurant_id, items" in _src("rec_ledger.py")


def test_no_constant_confidence_stand_ins_remain():
    """The old 0.3/0.55/0.8 score map (and any band→number map like it) is
    gone from every module; a percentage is always computed."""
    pat = re.compile(r"""["']low["']\s*:\s*0\.3\s*,\s*["']medium["']\s*:\s*0\.55""")
    for dirpath, _dirs, files in os.walk(ROOT):
        if any(part in dirpath for part in ("/tests", "/.git", "/ios", "/docs", "/node_modules", "/.claude")):
            continue
        for fn in files:
            if fn.endswith(".py"):
                path = os.path.join(dirpath, fn)
                with open(path) as f:
                    assert not pat.search(f.read()), path


# ── intelligence/confidence.py (E4) ────────────────────────────────────────

def test_the_kind_model_weights_sum_to_one_and_nothing_is_called_accuracy():
    from intelligence import confidence
    assert abs(sum(confidence.WEIGHTS.values()) - 1.0) < 1e-9
    assert "historical_accuracy" not in confidence.WEIGHTS and "measurability" in confidence.WEIGHTS
    assert not hasattr(confidence, "_historical_accuracy")
    assert "held here before" not in _src("intelligence/confidence.py")


# ── calibration (K7) ───────────────────────────────────────────────────────

def test_reliability_and_brier_are_withheld_below_the_floor():
    few = [(72, 1)] * 5
    assert ce.brier(few, 20) is None
    row = ce.reliability(few, 20)[0]
    assert row["range"] == "70-79" and row["n"] == 5 and row["observed_rate"] is None and not row["enough"]
    many = [(72, 1)] * 15 + [(72, 0)] * 5
    row = ce.reliability(many, 20)[0]
    assert row["observed_rate"] == 75.0 and row["low"] < 75 < row["high"]
    assert ce.brier(many, 20) == round((15 * 0.28 ** 2 + 5 * 0.72 ** 2) / 20, 4)
    assert ce.decile(100) == "90-100" and ce.decile(0) == "0-9"


def test_admin_calibration_reads_learned_verdicts_and_the_snapshot(db):
    import admin_ops
    rid = _rid(db)
    conf = ce.assemble(ce.evidence(n=8, kind="reviews"), ce.accuracy(None), ce.freshness([]))
    keys = [f"top_issue:t{i}" for i in range(3)]
    rec_ledger.present_many(rid, [{"key": k, "module": "reviews", "confidence": conf} for k in keys], "home",
                            db_path=db)
    for k in keys:
        rec_ledger.record(rid, k, "accepted", db_path=db)
    rec_ledger.record(rid, keys[0], "outcome", meta={"verdict": "improved"}, db_path=db)
    rec_ledger.record(rid, keys[1], "outcome", meta={"verdict": "worsened"}, db_path=db)
    rec_ledger.record(rid, keys[2], "outcome", meta={"verdict": "improved"}, db_path=db)
    rec_ledger.record(rid, keys[2], "checkin", meta={"did_it": "no"}, db_path=db)     # disowned: not a win
    out = admin_ops.confidence_calibration(days=30)
    assert out["n"] == 2 and out["floor_n"] == admin_ops.RAS_MIN_N
    assert out["bands"][0]["range"] == "70-79" and out["bands"][0]["observed_rate"] is None
    assert out["brier"] is None
    acc = admin_ops.recommendation_acceptance(days=30)
    t = acc["total"]
    assert (t["improved"], t["measured"]) == (1, 2) and t["outcome_rate"] == 0.5
    assert {g["group"] for g in acc["by_confidence"]} == {"medium"}


# ── Ask (K5, E12) ──────────────────────────────────────────────────────────

def test_ask_confidence_is_measured_not_breadth(db, monkeypatch):
    import ask_cavnar
    rid = _rid(db)
    two_live = ["ctx", json.dumps({"is_live": True, "labor_pct": 31}), json.dumps({"reviews": 3})]
    m = ask_cavnar._meta("Labor ran 31% last week.", two_live, ["read_labor", "read_reviews"], [], "standard", rid)
    d = m["confidence_detail"]
    # two modules read no longer means "high": no track record caps it
    assert d["pct"] is not None and d["pct"] <= ce.NO_TRACK_RECORD_CAP and m["confidence"] != "high"
    assert "2 live reads" in d["dimensions"]["evidence"]["basis"]
    sample = ["ctx", json.dumps({"is_live": False, "labor_pct": 31})]
    s = ask_cavnar._meta("Labor ran 31%.", sample, ["read_labor"], [], "standard", rid)["confidence_detail"]
    assert s["dimensions"]["evidence"]["pct"] == 0 and s["band"] == "low"
    bad = ask_cavnar._meta("Labor ran 44% last week.", two_live, ["read_labor"], [], "standard", rid)
    assert bad["unverified_figures"] and bad["confidence_detail"]["dimensions"]["evidence"]["pct"] <= ce.UNVERIFIED_CAP
    assert bad["confidence"] == "low"


# ── diagnoses and actions (E15, E16) ───────────────────────────────────────

def test_a_campaign_read_without_a_holdout_never_reads_high(db, monkeypatch):
    import guest_marketing as gm
    rows = [{"id": i, "sent_count": 40, "clicks": 8 - i, "visits_matched": 6 - i, "segment": "all",
             "segment_label": "Everyone", "created_at": f"2026-09-0{i + 1} 12:00:00", "link_token": f"t{i}"}
            for i in range(6)]
    monkeypatch.setattr(gm, "campaign_history", lambda *a, **k: rows)
    d = gm.diagnose(_rid(db), db_path=db)
    assert d["evidence_input"]["cap"] == gm.CAMPAIGN_NO_HOLDOUT_CAP
    assert d["confidence_detail"]["dimensions"]["evidence"]["pct"] <= 65 and d["confidence"] != "high"


def test_labor_diagnosis_evidence_carries_the_partial_flags():
    import labor
    a = {"total_sales": 50000, "total_labor_cost": 17500, "overall_labor_pct": 35.0, "labor_target": 30,
         "period_days": 28, "date_range": {"days": 28}, "days_missing_sales": [f"d{i}" for i in range(20)],
         "dow_summary": {"Monday": 41.0}, "overtime_risk": [], "role_summary": {}}
    d = labor.diagnose(a)
    assert d["evidence_input"]["coverage"] == pytest.approx(8 / 28)
    assert ce.evidence(**d["evidence_input"])["pct"] <= ce.LOW_COVERAGE_CAP and d["confidence"] == "low"


def test_dsr_action_rank_uses_the_confidence_percentage():
    import home_brief
    base = {"timeframe": "This week", "dollars_monthly": 400, "effort": "low"}
    hi = home_brief.rank_score(dict(base, confidence={"pct": 90, "band": "high", "version": 1}))
    lo = home_brief.rank_score(dict(base, confidence={"pct": 20, "band": "low", "version": 1}))
    none = home_brief.rank_score(dict(base, confidence={"pct": None, "band": "low", "version": 1}))
    assert hi > lo >= none
    assert "action_confidence(a, F, ctx, tctx)" in _src("dsr/narrative.py")


# ── Home (E2, E6–E11, K4) ──────────────────────────────────────────────────

@pytest.fixture
def home(db, monkeypatch):
    import auth
    import client_api
    import home_brief
    import mobile_api
    real = models.get_conn
    for mod in (auth, client_api, mobile_api, home_brief):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db))
    auth.init_auth(db_path=db)
    models.init_email_log(db_path=db)
    import ai_utils
    c = real(db); c.executescript(ai_utils._USAGE_TABLE_SQL); c.commit(); c.close()
    home_brief.invalidate()
    return db


def _user(rid):
    return {"id": 1, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "client",
            "is_admin": 0, "email": "o@x.com"}


def _labor(days=28, missing=0, pct=36.0, dow=None, by_day=None):
    end = datetime(2026, 9, 22).date()
    return {"is_live": True, "overall_labor_pct": pct, "period_days": days,
            "date_range": {"days": days, "start": (end - timedelta(days=days - 1)).isoformat(), "end": end.isoformat()},
            "days_missing_sales": [f"m{i}" for i in range(missing)], "potential_savings_weekly": 120.0,
            "total_labor_cost": 9000, "total_sales": 25000, "dow_summary": dow or {}, "by_day": by_day or {},
            "overtime_risk": [], "week_start_day": 0, "hours_are_estimated": False}


def test_home_cards_and_attention_carry_the_measured_confidence(home, monkeypatch):
    import home_brief
    import labor
    rid = _rid(home, module_inventory=0, module_marketing=0)
    models.update_restaurant(rid, {"labor_target_pct": 30.0}, db_path=home)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: _labor(days=28, missing=20))
    p, st = home_brief.build_home_brief(_user(rid), fresh=True)
    assert st == 200
    att = next(a for a in p["attention"] if a["key"] == "labor_over")
    # E8: coverage in the text and in the evidence — 8 of 28 days carry sales
    assert "8 of 28 days carry sales" in att["evidence"]
    assert att["confidence"]["dimensions"]["evidence"]["pct"] <= ce.LOW_COVERAGE_CAP
    assert att["confidence"]["band"] == "low" and att["dollars_basis"]
    for r in p["recommendations"]:
        assert "strength" not in r and r["confidence"]["version"] == ce.VERSION
    # K4 freshness entries, the stalest date, the honest monitoring count
    f = next(x for x in p["freshness"] if x["module"] == "labor")
    assert {"module", "source", "state", "pct", "as_of", "basis"} <= set(f)
    assert f["as_of"] == "9/22/26"
    assert re.match(r"^\d{1,2}/\d{1,2}/\d{2}$", p["brief"]["data_as_of"] or "")
    assert p["monitoring"]["count_live"] == sum(1 for x in p["freshness"] if x["state"] == "current")


def test_no_labor_over_attention_from_a_cold_start(home, monkeypatch):
    import home_brief
    import labor
    rid = _rid(home, module_inventory=0, module_marketing=0)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: _labor(days=1, pct=48.0))
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    assert not [a for a in p["attention"] if a["key"] == "labor_over"]


def test_a_failing_rpower_sync_is_critical_on_home(home, monkeypatch):
    import home_brief
    import labor
    rid = _rid(home, module_inventory=0, module_marketing=0)
    c = models.get_conn(home)
    c.execute("UPDATE restaurants SET rpower_token='t', rpower_store_mid='m', rpower_sync_error='401 Unauthorized', "
              "rpower_last_synced=? WHERE id=?", ((datetime.utcnow() - timedelta(days=3)).isoformat(), rid))
    c.commit(); c.close()
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {"is_live": False})
    p, _ = home_brief.build_home_brief(_user(rid), fresh=True)
    a = next(a for a in p["attention"] if a["key"] == "pos_sync")
    assert a["severity"] == "critical" and a["title"].startswith("RPOWER")


def test_trim_day_needs_a_spread_based_gap_and_refuses_thin_dollars():
    import home_brief
    # a heavy Friday against steady other days: named
    steady = {"Monday": 28.0, "Tuesday": 28.5, "Wednesday": 27.5, "Thursday": 28.0, "Friday": 36.0}
    by_day = {"2026-09-04": {"sales": 1000, "labor_cost": 360}, "2026-09-11": {"sales": 1000, "labor_cost": 360}}
    t = home_brief.trim_day_read(steady, by_day, 30, 28)
    assert t["day"] == "Friday" and t["monthly"] and t["n_days"] == 2 and "× 52 ÷ 12" in t["basis"]
    # the same gap where weekdays already swing widely: not a finding
    noisy = {"Monday": 20.0, "Tuesday": 34.0, "Wednesday": 22.0, "Thursday": 33.0, "Friday": 36.0}
    assert home_brief.trim_day_read(noisy, by_day, 30, 28) is None
    # one Friday, or a period under the extrapolation floor: no monthly figure
    one = {"2026-09-04": {"sales": 1000, "labor_cost": 360}}
    assert home_brief.trim_day_read(steady, one, 30, 28)["monthly"] is None
    assert home_brief.trim_day_read(steady, by_day, 30, 5)["monthly"] is None
    # a day with no sales figure has no excess to measure
    assert home_brief.trim_day_read(steady, {"2026-09-04": {"sales": None, "labor_cost": 360}}, 30, 28)["n_days"] == 0


def test_the_review_summary_never_invents_a_theme():
    src = _src("home_brief.py")
    assert "else 'Service'" not in src and "if top_issues else 'Service'" not in src
    assert "RATING_MOVE_STARS" in src and "rating_delta >= 0.2" not in src


def test_the_one_thing_carries_its_confidence(home):
    import business_intelligence as bi
    rid = _rid(home)
    c = models.get_conn(home)
    for i in range(4):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                  "fetched_at, response_status, processed) VALUES (?, 'google', ?, 'A', 1, 'bad', date('now','-2 days'), "
                  "datetime('now','-2 days'), 'pending', 1)", (rid, f"x{i}"))
    c.commit(); c.close()
    data = {"reviews": {"brief": {}, "diagnoses": []}, "labor": {}, "food_cost": None}
    first = bi.pick_one_thing(rid, bi.one_thing_candidates(rid, data, [], db_path=home), db_path=home)
    assert first["key"] == "urgent_reviews" and first["confidence"]["version"] == ce.VERSION
    assert first["confidence"]["dimensions"]["evidence"]["pct"] == 100 and first["model_written"] is False


def test_trim_day_tolerates_days_with_no_sales_percentage():
    """Group G writes a day with no sales as labor_pct None / sales NULL:
    such a day is neither the heavy day nor counted as all-excess labor."""
    import home_brief
    dow = {"Monday": 28.0, "Tuesday": None, "Wednesday": 27.5, "Thursday": 28.0, "Friday": 36.0}
    by_day = {"2026-09-04": {"sales": 1000, "labor_cost": 360, "labor_pct": 36.0},
              "2026-09-11": {"sales": 1000, "labor_cost": 360, "labor_pct": 36.0},
              "2026-09-18": {"sales": None, "labor_cost": 900, "labor_pct": None}}
    t = home_brief.trim_day_read(dow, by_day, 30, 28)
    assert t["day"] == "Friday" and t["n_days"] == 2 and t["monthly"] == round(60 * 52 / 12, 2)


def test_places_only_reviews_take_the_coverage_share_when_measured(db, monkeypatch):
    import fetcher
    rid = _rid(db)
    row = {"id": rid, "reviews_live": 1, "google_place_id": "ChIJtest",
           "last_fetched_at": (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")}
    monkeypatch.setattr(fetcher, "places_coverage", lambda r: {"share": 0.4, "sampled": True}, raising=False)
    st = data_freshness.source_state(row, "reviews", db_path=db, now=datetime.now(timezone.utc))
    assert st["sampled"] and st["pct"] <= 40 and "40% of Google's new reviews stored" in st["basis"]


def test_no_waste_win_when_waste_was_not_measured():
    """Group I: a week with no waste logged is `not_measured`, never a win."""
    src = _src("home_brief.py")
    assert 'benchmark_state") != "not_measured"' in src
