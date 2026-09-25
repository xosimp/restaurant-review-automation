"""Benchmarking re-audit, workstream T — targets and published figures
(Top-50 items 2, 3, 4, 10, 31, 32, 43, 45; findings R1-03/05/12/16/19,
R2-2/3/4/17/21, R3-5/16/26, R4-1/2/3/5/6/26/31).

Each test here failed before its fix:
  * a published figure measured differently (or not saying what it counts)
    produced a food-cost verdict, dish colours and "$ under industry";
  * confirming a type seeded a 34.2% labor target from a benefits-inclusive
    median, for a counter-service Italian too, and never reset it;
  * Cavnar's 30% default read as the owner's "Over target" in red on Food
    Cost, the digest, Home and the group brief;
  * the waste banner banded dollars as "industry benchmarks";
  * an owner-entered $26 or an explicit save of 30% stayed "default".
"""
import ast
import dataclasses
import inspect
import os
import re
from datetime import date

import pytest

import auth
import benchmark_registry as br
import cogs
import models
import reporter
import thresholds
from models import Restaurant, create_restaurant, get_conn, get_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _mk(db, name, profile=None, **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{abs(hash(name))}@x.test"), db_path=db)
    sets = dict(cols)
    if profile:
        sm, concept = profile
        sets.update(service_model=sm, concept=concept, category=concept, profile_source="set",
                    profile_confirmed_at="2026-09-01T00:00:00")
    if sets:
        conn = get_conn(db)
        conn.execute(f"UPDATE restaurants SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                     (*sets.values(), rid))
        conn.commit()
        conn.close()
    return rid


# ── #32: every entry declares what it measures, and how old it may be ────

def test_every_registry_entry_declares_a_definition_and_an_age_limit():
    for e in br.ENTRIES:
        assert "definition" in e, (e["metric"], e["category"])
        assert e.get("max_age_years"), (e["metric"], e["category"])
    # Bands derived from the NRA food median inherit what it measures.
    nra = br.lookup("food_cost_pct", "italian")["definition"]
    for cat in ("sports_bar", "bar"):
        assert br.lookup("food_cost_pct", cat)["definition"] == nra


def test_unknown_definition_is_not_comparable():
    thumb = br.lookup("labor_pct", "fast_casual")
    assert thumb["definition"] is None
    assert br.definitions_differ(thumb, "wages_from_shifts")
    assert br.lookup("food_cost_pct", "sports_bar", definition="cogs_pct_sales") is None
    assert "does not say what it counts" in br.definition_note(thumb, "Labor %", "wages_from_shifts")


def test_a_figure_past_its_age_limit_is_no_entry():
    e = br.lookup("labor_pct", "italian")
    assert not br.is_stale(e, today=date(2027, 12, 31))
    assert br.is_stale(e, today=date(2028, 1, 1))


# ── #4: published figures apply by service model ─────────────────────────

def test_a_counter_service_italian_never_gets_the_full_service_median():
    assert br.lookup("labor_pct", "italian", published_only=True, service_model="counter") is None
    assert br.lookup("labor_pct", "italian", published_only=True, service_model="full_service")["median"] == 34.2
    counter = Restaurant(name="Slice", owner_email="s@x.test", category="italian", concept="italian",
                         service_model="counter", profile_source="set")
    assert br.for_restaurant("labor_pct", counter) is None
    full = Restaurant(name="Nonna", owner_email="n@x.test", category="italian", concept="italian",
                      service_model="full_service", profile_source="set")
    assert br.for_restaurant("labor_pct", full)["service_model"] == "full_service"


def test_the_engine_ledger_and_audit_pass_the_service_model():
    import intelligence.jobs as jobs
    import sales_audit_engine
    assert "service_model=" in inspect.getsource(jobs.record_assignments)
    assert "service_model=" in inspect.getsource(sales_audit_engine._bench)
    assert "service_model=" in inspect.getsource(thresholds.seeded_targets)


# ── the fact contract (benchmark_registry.facts) ─────────────────────────

def test_registry_facts_carry_the_shared_source_keys():
    f = br.facts(br.lookup("labor_pct", "italian"), own_value=31.0)[0]["source"]
    for k in ("comparable", "definition_note", "inferred", "standing", "metric", "better", "own_value",
              "strength_pct"):
        assert k in f, k
    assert f["comparable"] is False and f["metric"] == "labor_pct_28d" and f["better"] == "lower"
    assert f["own_value"] == 31.0 and "including benefits" in f["definition_note"]
    # A metric the engine has no definition for is not known to be like for like.
    assert br.facts(br.lookup("prime_cost_pct", None))[0]["source"]["comparable"] is False


# ── #3: targets seed only from a like-for-like figure, and reset ─────────

def test_confirming_a_full_service_steakhouse_does_not_seed_the_benefits_median(db):
    rid = _mk(db, "Prime Cut", profile=("full_service", "steakhouse"))
    seed = thresholds.seeded_targets(get_restaurant(rid, db))
    assert seed["labor_target_pct"] == 30.0 and seed["labor_target_source"] == "default"


def test_a_stale_seed_is_reset_to_the_default_value_and_alerts_go_off(db):
    rid = _mk(db, "Old Seed", profile=("full_service", "italian"),
              labor_target_pct=34.2, labor_target_source="seeded",
              food_cost_target=32.0, food_cost_target_source="seeded")
    assert thresholds.target_alerts_allowed(get_restaurant(rid, db), "labor")
    assert models.backfill_seeded_targets(db_path=db) == 1
    r = get_restaurant(rid, db)
    assert (r.labor_target_pct, r.labor_target_source) == (30.0, "default")
    assert (r.food_cost_target, r.food_cost_target_source) == (30.0, "default")
    assert not thresholds.target_alerts_allowed(r, "labor")
    assert models.backfill_seeded_targets(db_path=db) == 0          # idempotent
    # Re-confirming to another type resets too (R3-26).
    rid2 = _mk(db, "Retyped", profile=("counter", "pizza"), labor_target_pct=34.2, labor_target_source="seeded")
    assert thresholds.seeded_targets(get_restaurant(rid2, db))["labor_target_pct"] == 30.0


# ── #10: one target read, and a starting target is never "Over" in red ──

def test_target_for_names_whose_target_it_is():
    d = thresholds.target_for(Restaurant(name="D", owner_email="d@x.test"), "food")
    assert d == {"pct": 30.0, "source": "default", "label": "Cavnar's starting target", "alerts_allowed": False,
                 "phrase": "Cavnar's starting target of 30%"}
    s = thresholds.target_for(Restaurant(name="S", owner_email="s@x.test", food_cost_target=28.0), "food")
    assert s["source"] == "set" and s["label"] == "your target" and s["alerts_allowed"]


def test_food_cost_label_and_dish_colours_on_the_starting_target(monkeypatch):
    monkeypatch.setattr(cogs, "_engine_industry_food_cost", lambda r: None)
    r = Restaurant(name="Steak", owner_email="st@x.test", category="steakhouse")
    assert cogs.band_label(34.0, 30.0, starting=True) == ("Above Cavnar's starting target", "warn")
    ref = cogs.dish_reference(r)
    assert ref["kind"] == "starting_target" and ref["target_source"] == "default"
    assert cogs.dish_tone(45.0, ref) == "warn"            # capped: nobody chose 30
    own = cogs.dish_reference(Restaurant(name="O", owner_email="o@x.test", food_cost_target=28.0))
    assert own["kind"] == "target" and cogs.dish_tone(45.0, own) == "bad"


def test_a_band_measured_differently_is_context_never_a_verdict():
    nra = br.lookup("food_cost_pct", "italian")
    assert cogs.band_label(33.0, bench=dict(nra, comparable=False)) == (None, None)
    assert cogs.band_label(33.0, bench=dict(nra, comparable=True)) == ("Above the industry band (NRA 2025)", "bad")
    italian = Restaurant(name="I", owner_email="i@x.test", category="italian", concept="italian",
                         service_model="full_service", profile_source="set")
    ref = cogs.dish_reference(italian)
    assert ref["kind"] == "starting_target"                # not the NRA band top


def test_the_digest_tag_caps_on_a_starting_target():
    brand = {"good": "g", "warn": "w", "bad": "b"}
    assert reporter.labor_tag(40.0, 30.0, brand, starting=True) == ("w", "Above starting target")
    assert reporter.labor_tag(40.0, 30.0, brand) == ("b", "Over target")
    src = inspect.getsource(reporter)
    assert "target_for(_rest, \"labor\")" in src


def test_home_and_the_group_brief_cap_severity_on_a_starting_target():
    import home_brief
    src = inspect.getsource(home_brief)
    assert '"watch" if not _labor_tgt_for["alerts_allowed"]' in src
    assert '"watch" if not labor.get("target_alerts", True)' in src
    assert "pts over target\", \"module\": \"labor\"" not in src


def test_value_opportunity_names_the_starting_target(db, monkeypatch):
    import value_delivered
    rid = _mk(db, "Opp", module_labor=1, hourly_rate=19.0, hourly_rate_source="set")
    lab = {"is_live": True, "potential_savings_monthly": 900.0}
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: lab)
    monkeypatch.setattr(value_delivered, "_dated", lambda items, w, r, item, *a: items.append(item))
    it = value_delivered.opportunity(rid, db_path=db)["items"][0]
    assert it["label"] == "Scheduling against Cavnar's starting target" and it["target_source"] == "default"


def test_no_new_bare_target_reads():
    """Ratchet (re-audit #10): a surface that judges a figure against a
    labor or food-cost target reads thresholds.target_for, never the bare
    column or notify.labor_target_for. These counts may only fall."""
    ceiling = {"admin_ops.py": 2, "admin_routes.py": 4, "client_api.py": 2, "cogs.py": 2, "demo_seed.py": 3,
               "dsr/block_labor.py": 1, "dsr/memory.py": 1, "food_cost_intelligence.py": 6, "good_news.py": 2,
               "hosted_dashboard.py": 1, "issues.py": 1, "labor.py": 1, "mobile_api.py": 3, "models.py": 14,
               "morning_brief.py": 2, "notify.py": 3, "rec_trust.py": 2, "schedule_economics.py": 11,
               "schedule_engine.py": 2, "thresholds.py": 2}
    pat = re.compile(r"food_cost_target\b|labor_target_pct\b|labor_target_for\(")
    found = {}
    for base, dirs, files in os.walk(ROOT):
        rel_base = os.path.relpath(base, ROOT)
        if rel_base.split(os.sep)[0] in ("tests", "scripts", ".git", ".claude", "docs", "ios", "node_modules",
                                         "venv", ".venv"):
            dirs[:] = []
            continue
        for fn in files:
            if fn.endswith(".py"):
                rel = os.path.normpath(os.path.join(rel_base, fn))
                with open(os.path.join(base, fn), encoding="utf-8") as fh:
                    n = sum(1 for line in fh if pat.search(line))     # lines, as `rg -c` counts
                if n:
                    found[rel] = n
    for f, n in found.items():
        assert n <= ceiling.get(f, 0), f"{f}: {n} bare target reads (ceiling {ceiling.get(f, 0)})"
    for f in ("reporter.py", "home_brief.py", "value_delivered.py"):
        assert f not in found, f


# ── #2: no "$ under industry" from a figure measured differently ─────────

def test_no_dollars_under_industry_from_a_non_comparable_figure():
    import labor
    full = Restaurant(name="Nonna", owner_email="n@x.test", category="italian", hourly_rate=18.0)
    a = {"is_live": True, "overall_labor_pct": 28.0, "total_sales": 60000, "period_days": 30,
         "potential_savings_monthly": 0}
    sb = labor.savings_breakdown(a, restaurant=full)
    assert sb["labor_industry_pct"] == 34.2                  # the context mark stays
    assert sb["labor_vs_industry_monthly"] == 0 and sb["labor_vs_industry_annual"] == 0
    assert sb["labor_industry_comparable"] is False and "benefits" in sb["labor_industry_note"]
    assert thresholds.labor_vs_industry_monthly(28.0, 60000, 30, industry_pct=34.2) == 0


# ── #43: the waste banner has no unsourced dollar "industry" bands ───────

def _banner():
    src = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if (isinstance(node, ast.FunctionDef) and node.name == "inv_banner_gradient") or \
                (isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "_WASTE_BANNER_RED"
                                                      for t in node.targets)):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "hd", "exec"), ns)
    return src, ns["inv_banner_gradient"]


def test_the_waste_banner_reads_the_owners_target_not_dollar_bands():
    src, fn = _banner()
    assert "Industry benchmarks" not in src and "annual_waste < 5000" not in src
    # The same $40k of waste is mild under target and serious well over it.
    assert fn("good", 40000, 0) != fn("bad", 40000, 0)
    assert fn("good", 40000, 0) == fn("good", 1000, 0)


# ── #45: target and wage provenance edge cases ───────────────────────────

def test_an_owner_entered_26_dollar_rate_is_theirs(db):
    assert "hourly_rate_source" in {f.name for f in dataclasses.fields(Restaurant)}
    assert '"hourly_rate_source"' in inspect.getsource(models.update_restaurant)
    rid = _mk(db, "Rate Owner", hourly_rate=21.0)
    update_restaurant(rid, {"hourly_rate": 26.0}, db_path=db)
    r = get_restaurant(rid, db)
    assert r.hourly_rate_source == "set" and thresholds.labor_cost_basis(r) == "owner_blended"
    rid2 = _mk(db, "Rate Default")
    update_restaurant(rid2, {"hourly_rate": 26.0}, db_path=db)          # a form re-send
    assert thresholds.labor_cost_basis(get_restaurant(rid2, db)) == "default"


def test_an_explicit_admin_save_of_the_default_is_the_owners(db, monkeypatch):
    from flask import Flask
    import admin_routes
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _mk(db, "Explicit")
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)

    def save(**body):
        with app.test_request_context(f"/admin/client-settings/{rid}", method="POST",
                                      json=dict({"name": "Explicit", "owner_email": "e@x.test"}, **body)):
            return admin_routes.save_client_settings(rid).get_json()

    assert save(labor_target_pct=30.0, hourly_rate=26.0)["ok"]
    r = get_restaurant(rid, db)
    assert thresholds.target_source(r, "labor") == "default" and thresholds.labor_cost_basis(r) == "default"
    assert save(labor_target_pct=30.0, hourly_rate=26.0, touched=["labor_target_pct", "hourly_rate"])["ok"]
    r = get_restaurant(rid, db)
    assert thresholds.target_source(r, "labor") == "set" and thresholds.target_alerts_allowed(r, "labor")
    assert thresholds.labor_cost_basis(r) == "owner_blended"
    html = open(os.path.join(ROOT, "templates", "client_settings.html"), encoding="utf-8").read()
    assert "touched:         cavTouchedList()," in html
