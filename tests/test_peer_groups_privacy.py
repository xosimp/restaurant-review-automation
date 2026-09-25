"""Peer groups and privacy (Benchmarking audit 9/24/26, workstream P).

Who a restaurant may be compared with, and what a comparison may reveal:
the owner-confirmed profile and its peer partition, guessed types kept out,
organisation-level floors, the quality gate, disclosure control on what is
published, the peer-assignment ledger and its hysteresis, default targets
and the labor cost basis that say where they came from, the sales audit's
type picker, trends over a steady panel, and the admin/public projections.

Top-50 items #6, #7, #8, #9, #10 (see test_schedule_learning_items), #11,
#13, #14, #15 (see test_sales_audit), #20, #29, #30, #31, #39, #42, #43,
#44, #47."""
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
import thresholds
import benchmark_registry as br
from models import Restaurant, create_restaurant, get_conn, update_restaurant, get_restaurant

import intelligence  # noqa: E402 — imported before any fixture patches get_conn
from intelligence import (benchmarks as bm, categories, confidence, dashboard, engine as eng,  # noqa: E402
                          features as feat, jobs, metrics_registry as reg, patterns, privacy, stats, trends)

LIVE = (date.today() - timedelta(days=120)).isoformat() + "T00:00:00"
WEEK = feat.iso_week(date.today())


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (auth, client_api, mobile_api, intelligence, bm, confidence, dashboard, eng, feat, jobs, patterns,
                trends):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    privacy.invalidate_tenant_names()
    yield
    privacy.invalidate_tenant_names()


def _mk(db_path, name, profile=None, feats=None, completeness=0.9, **cols):
    """A restaurant live 17 weeks on a real labor cost basis; `profile` is
    (service_model, concept) confirmed by the owner; `cols` raw columns."""
    kw = {"created_at": LIVE, "hourly_rate": 18.0}
    for k in ("created_at", "hourly_rate", "location_group"):
        if k in cols:
            kw[k] = cols.pop(k)
    rid = create_restaurant(Restaurant(name=name, owner_email=cols.pop("owner_email", f"{abs(hash(name))}@x.test"),
                                       **kw), db_path=db_path)
    sets = dict(cols)
    if profile:
        sm, concept = profile
        sets.update(service_model=sm, concept=concept, category=concept, profile_source="set",
                    profile_confirmed_at="2026-09-01T00:00:00")
    if sets:
        conn = get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                     (*sets.values(), rid))
        conn.commit()
        conn.close()
    if feats is not None:
        conn = get_conn(db_path)
        conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, completeness) "
                     "VALUES (?,?,?,?)", (rid, WEEK, json.dumps(feats), completeness))
        conn.commit()
        conn.close()
    return rid


def _group(db_path, n, profile=("counter", "pizza"), base=None, step=None, start=0, **cols):
    base = base or {"labor_pct_28d": 28.0, "reply_rate_30d": 0.5, "avg_rating_30d": 4.2}
    out = []
    for i in range(start, start + n):
        f = {k: round(v + i * ((step or {}).get(k) or (0.02 if v < 1 else 0.2)), 3) for k, v in base.items()}
        out.append(_mk(db_path, f"Peer {profile[0]} {i}", profile=profile, feats=f, **cols))
    return out


def _org(db_path, name):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO organizations (name, owner_email) VALUES (?, ?)", (name, f"{name}@x.test"))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _set(db_path, rid, **cols):
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
    conn.commit()
    conn.close()


def _wipe_bands(db_path):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM intel_benchmarks")
    conn.commit()
    conn.close()


# ══ #6: the comparability class is enforced everywhere ═════════════════════

def test_a_type_sensitive_metric_never_falls_back_to_the_all_types_band(db_path):
    _group(db_path, 12)
    viewer = _mk(db_path, "Lone Steak", profile=("full_service", "steakhouse"),
                 feats={"labor_pct_28d": 34.0, "reply_rate_30d": 0.9, "avg_rating_30d": 4.6})
    bm.compute(db_path=db_path)
    stored = {r["metric"] for r in get_conn(db_path).execute(
        "SELECT metric FROM intel_benchmarks WHERE cohort='platform'").fetchall()}
    assert stored and all(reg.platform_allowed(m) for m in stored)          # no all-types labor band exists
    for m in feat.BENCHMARK_KEYS:
        for b in (bm.benchmark(viewer, m, db_path=db_path),
                  bm.benchmark(viewer, m, cohort="steakhouse", cohort_source="set", db_path=db_path)):
            assert b.get("cohort") != "platform" or reg.platform_allowed(m), m
    labor = bm.benchmark(viewer, "labor_pct_28d", db_path=db_path)
    assert labor["available"] is False and labor["reason"].startswith("no like-for-like peers yet")
    reply = bm.benchmark(viewer, "reply_rate_30d", db_path=db_path)
    assert reply["available"] and reply["cohort"] == "platform"
    # A member is compared within its confirmed partition, never all types.
    peers = bm.all_for(1, db_path=db_path)
    lab = next(b for b in peers if b["metric"] == "labor_pct_28d")
    assert lab["available"] and lab["cohort"] == "sm:counter" and lab["n"] == 11


# ══ #7: an owner-confirmed restaurant profile ══════════════════════════════

@pytest.fixture
def client():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    return app.test_client()


def _login(monkeypatch, rid, role="owner"):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0, "username": "o", "role": role})


def test_every_profile_column_has_its_four_touch_points(db_path):
    rid = _mk(db_path, "Columns Co")
    vals = {"service_model": "full_service", "concept": "italian", "bar_led": 1, "ownership": "independent",
            "opened_year": 2011, "profile_source": "set", "profile_confirmed_at": "2026-09-24T10:00:00",
            "exclude_from_learning": 1, "labor_target_source": "seeded", "food_cost_target_source": "default",
            "google_types": '["restaurant", "bar"]', "google_price_level": 3}
    update_restaurant(rid, vals, db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    for k, v in vals.items():
        assert getattr(r, k) == v, k
    cols = {row["name"] for row in get_conn(db_path).execute("PRAGMA table_info(intel_benchmarks)").fetchall()}
    assert {"orgs", "max_org_share", "members_json"} <= cols


def test_the_owner_confirms_the_profile_on_the_web_and_the_mobile_twin_shares_the_body(client, db_path,
                                                                                         monkeypatch):
    rid = _mk(db_path, "Nonna Trattoria")
    _login(monkeypatch, rid)
    got = client.get("/api/account-settings/restaurant-profile").get_json()
    p = got["profile"]
    assert got["ok"] and p["confirmed"] is False
    assert p["suggestion"]["text"] == "We think you're an Italian restaurant — is that right?"
    assert 0 < p["suggestion"]["confidence_pct"] <= 90
    assert {c["value"] for c in p["choices"]["service_model"]} == set(categories.SERVICE_MODELS)
    assert got["targets"]["labor"]["label"] == "Cavnar's starting target"
    bad = client.post("/api/account-settings/restaurant-profile", json={"service_model": "buffet"})
    assert bad.status_code == 400
    ok = client.post("/api/account-settings/restaurant-profile",
                     json={"service_model": "full_service", "concept": "italian", "bar_led": False,
                           "ownership": "independent", "opened_year": "2012"}).get_json()
    assert ok["ok"] and ok["profile"]["confirmed"] and ok["profile"]["suggestion"] is None
    r = get_restaurant(rid, db_path=db_path)
    assert (r.service_model, r.concept, r.ownership, r.opened_year, r.profile_source) == \
        ("full_service", "italian", "independent", 2012, "set")
    assert r.profile_confirmed_at
    # The confirmed concept wins over any guess or category (#7).
    assert categories.category_for(r) == ("italian", "set")
    # Targets not set by the owner are seeded only from a published median
    # measured the way Cavnar measures it (re-audit #3): the NRA labor
    # median includes benefits and the food median counts non-alcohol
    # beverages, so both stay Cavnar's default. This pinned 34.2 / 32.0.
    assert (r.labor_target_pct, r.labor_target_source) == (30.0, "default")
    assert (r.food_cost_target, r.food_cost_target_source) == (30.0, "default")
    # The change is recorded with its old and new values (#29).
    ev = get_conn(db_path).execute("SELECT event_data FROM activity_log WHERE restaurant_id=? AND "
                                   "event_type='profile_changed'", (rid,)).fetchall()
    changes = json.loads(ev[-1]["event_data"])
    assert changes["service_model"] == {"from": None, "to": "full_service"}
    src = open(mobile_api.__file__).read()
    assert '@mobile_bp.route("/account/restaurant-profile", methods=["GET", "POST"])' in src
    assert src.count("_capi._do_restaurant_profile(") == 1 and src.count("_capi._restaurant_profile_payload(") == 1


def test_only_the_owner_changes_the_profile(client, db_path, monkeypatch):
    rid = _mk(db_path, "Managed Place")
    import permissions
    monkeypatch.setattr(permissions, "is_principal", lambda user: user.get("role") == "owner")
    _login(monkeypatch, rid, role="manager")
    r = client.post("/api/account-settings/restaurant-profile", json={"service_model": "counter"})
    assert r.status_code == 403 and r.get_json()["owner_only"] is True
    assert get_restaurant(rid, db_path=db_path).service_model is None


def test_a_set_target_is_never_reseeded_and_an_owner_edit_marks_it_set(db_path):
    rid = _mk(db_path, "Own Target", profile=("full_service", "italian"))
    update_restaurant(rid, {"labor_target_pct": 31.5}, db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert r.labor_target_source == "set" and thresholds.target_label(r, "labor") == "your target"
    seed = thresholds.seeded_targets(r)
    # Re-audit #3: no like-for-like published food figure, so the default
    # (reset to its value) — this pinned the NRA 32.0.
    assert "labor_target_pct" not in seed and seed["food_cost_target"] == 30.0 \
        and seed["food_cost_target_source"] == "default"
    # A form re-sending the unchanged default confirms nothing.
    rid2 = _mk(db_path, "Form Resend")
    update_restaurant(rid2, {"labor_target_pct": 30.0}, db_path=db_path)
    assert thresholds.target_source(get_restaurant(rid2, db_path=db_path), "labor") == "default"
    # A confirmed type with no published median keeps the default, labelled.
    pizza = _mk(db_path, "Counter Pie", profile=("counter", "pizza"))
    assert thresholds.seeded_targets(get_restaurant(pizza, db_path=db_path)) == \
        {"labor_target_pct": 30.0, "labor_target_source": "default",
         "food_cost_target": 30.0, "food_cost_target_source": "default"}
    assert thresholds.seeded_targets(get_restaurant(_mk(db_path, "Nonna Unconfirmed"), db_path=db_path)) == {}


def test_the_copy_points_at_the_real_control():
    import emails
    src = open(emails.__file__).read() + open(bm.__file__).read()
    assert "set it in Settings if it's wrong" not in src
    assert "Account → Restaurant profile" in open(emails.__file__).read()
    from intelligence import staffing
    assert "Set one under Account" not in open(staffing.__file__).read()


# ══ #8: guessed types ════════════════════════════════════════════════════════

@pytest.mark.parametrize("name,menu,vibe,expect", [
    ("Hank's Tavern", "flatbread pizza", "", "bar"),
    ("Casa Lupe", "tacos, mexican pizza", "", "mexican"),
    ("Smokey Joe's", "wood-fired brisket", "", "bbq"),
    ("The Dive", "", "dive bar, brunch on sundays", "bar"),
    ("Chilis Grill & Bar", "wings burgers", "", "bar"),
])
def test_inference_checks_format_before_cuisine_and_reads_the_name_first(name, menu, vibe, expect):
    """BM1-3, BM2-2 probes: a tavern, a taqueria and a BBQ joint all came out
    "pizza"; a dive bar with brunch became "breakfast"."""
    r = SimpleNamespace(name=name, menu_notes=menu, vibe=vibe)
    assert categories.infer(r) == expect
    d = categories.infer_detail(r)
    assert 0 < d["confidence"] <= 0.9 and d["cues"]


def test_google_agreement_raises_confidence_and_disagreement_lowers_it():
    base = SimpleNamespace(name="Harbor Tavern")
    agree = SimpleNamespace(name="Harbor Tavern", google_types='["bar", "restaurant"]')
    clash = SimpleNamespace(name="Harbor Tavern", google_types='["cafe"]')
    c0, c1, c2 = (categories.infer_detail(x)["confidence"] for x in (base, agree, clash))
    assert c1 > c0 > c2


def test_no_published_dollar_figure_on_a_guessed_type(db_path):
    guessed = Restaurant(name="Nonna Trattoria", owner_email="n@x.test")
    assert categories.category_for(guessed) == ("italian", "inferred")
    assert thresholds.labor_industry_benchmark(guessed) is None
    ind = eng.compare(None, "labor_pct_28d", kinds=("industry",), restaurant=guessed, rows=[])["comparisons"][0]
    assert ind["available"] is False and "guessed" in ind["why_not"]
    set_ = Restaurant(name="Nonna", owner_email="n@x.test", category="italian")
    assert thresholds.labor_industry_benchmark(set_)["pct"] == 34.2


def test_the_restaurants_own_google_listing_is_kept(db_path):
    import competitor
    rid = _mk(db_path, "Listed Place", google_place_id="ChIJ-own")
    competitor._remember_own_listing("ChIJ-own", ["bar", "restaurant", "point_of_interest"], 2)
    r = get_restaurant(rid, db_path=db_path)
    assert json.loads(r.google_types) == ["bar", "restaurant", "point_of_interest"] and r.google_price_level == 2


# ══ #9: organisation-level privacy ═════════════════════════════════════════

def test_org_key_is_the_organisation_else_the_group_and_owner_else_the_restaurant():
    assert privacy.org_key({"id": 3, "organization_id": 7}) == "o7"
    assert privacy.org_key({"id": 3, "location_group": " Syrup ", "owner_email": "A@x"}) == "gsyrup|a@x"
    assert privacy.org_key({"id": 3}) == "r3"
    assert privacy.org_counts(["a", "a", "b"]) == (2, 2 / 3)


def test_the_viewers_whole_organisation_is_out_of_the_band_it_sees(db_path):
    org = _org(db_path, "Siblings")
    sibs = _group(db_path, 3, start=0)
    for r in sibs:
        _set(db_path, r, organization_id=org)
    _group(db_path, 8, start=10)
    bm.compute(db_path=db_path)
    b = bm.benchmark(sibs[0], "labor_pct_28d", db_path=db_path)
    assert b["available"] and b["n"] == 8 and b["orgs"] == 8          # both siblings left out too
    single = bm.published("sm:counter", "labor_pct_28d", exclude_value=None, db_path=db_path)
    assert single["n"] == 11 and single["orgs"] == 9


def test_floors_count_organisations_and_no_one_organisation_is_over_a_third(db_path):
    chain = _org(db_path, "Chain")
    ids = _group(db_path, 5, start=0)
    for r in ids:
        _set(db_path, r, organization_id=chain)
    _group(db_path, 5, start=10)                                        # 6 organisations, one of them 5/10
    bm.compute(db_path=db_path)
    p = bm.published("sm:counter", "labor_pct_28d", db_path=db_path)
    assert p["withheld"] and "over a third" in p["reason"]
    _wipe_bands(db_path)
    pairs = _org(db_path, "Pairs")
    few = _group(db_path, 9, profile=("daytime", "cafe"), start=0)       # 9 restaurants, 4 organisations
    for i, r in enumerate(few):
        if i < 6:
            _set(db_path, r, organization_id=pairs + (i // 2))
    for i in (0, 2, 4):
        conn = get_conn(db_path)
        conn.execute("INSERT OR IGNORE INTO organizations (id, name, owner_email) VALUES (?,?,?)",
                     (pairs + i // 2, f"P{i}", f"p{i}@x.test"))
        conn.commit()
        conn.close()
    bm.compute(db_path=db_path)
    row = get_conn(db_path).execute("SELECT n, orgs, max_org_share FROM intel_benchmarks WHERE cohort='sm:daytime' "
                                    "AND metric='labor_pct_28d'").fetchone()
    assert row["n"] == 9 and row["orgs"] == 6 and abs(row["max_org_share"] - 2 / 9) < 0.01
    # A viewer in the first pair sees the other 7 — under the floor of 8 others.
    pair_out = bm.published("sm:daytime", "labor_pct_28d", exclude_org=privacy.org_hash(f"o{pairs}"),
                            db_path=db_path)
    assert pair_out["withheld"] and pair_out["n"] == 7 and "fewer than 8" in pair_out["reason"]


def test_a_band_needs_five_organisations_behind_it(db_path):
    ids = _group(db_path, 10, profile=("bar_led", "bar"))
    orgs = [_org(db_path, f"O{i}") for i in range(4)]
    for i, r in enumerate(ids):
        _set(db_path, r, organization_id=orgs[i % 4])
    bm.compute(db_path=db_path)
    count = "SELECT COUNT(*) FROM intel_benchmarks WHERE cohort='sm:bar_led' AND metric='labor_pct_28d'"
    assert get_conn(db_path).execute(count).fetchone()[0] == 0          # 10 locations, 4 owners
    _set(db_path, ids[0], organization_id=_org(db_path, "Fifth"))
    _set(db_path, ids[4], organization_id=None)
    _wipe_bands(db_path)
    bm.compute(db_path=db_path)
    assert get_conn(db_path).execute(count).fetchone()[0] == 1          # five owners now


def test_duplicate_listings_and_excluded_accounts_are_not_members(db_path):
    ids = _group(db_path, 9)
    _set(db_path, ids[3], exclude_from_learning=1)
    # Two rows for one physical restaurant (the same Google listing — Gia
    # Mia's two rows): counted once. The column is unique today, so the
    # duplicate is handed in as member info.
    members = jobs.member_info(db_path=db_path)
    members[ids[1]]["place_id"] = members[ids[2]]["place_id"] = "ChIJ-dup"
    out = bm.compute(db_path=db_path, members=members)
    assert out["skipped"].get("duplicate listing") == 1
    row = get_conn(db_path).execute("SELECT n FROM intel_benchmarks WHERE cohort='sm:counter' "
                                    "AND metric='labor_pct_28d'").fetchone()
    assert row["n"] == 7                                               # 9 − duplicate − excluded
    assert ids[3] not in jobs.real_restaurant_ids(db_path)


# ══ #11: patterns an owner is shown ═════════════════════════════════════════

def _pattern(db_path, key, orgs=8, means=True):
    ev = {"n": 16, "outcome": "avg_rating_delta", "behaviour": ["response_24h_rate_30d", ">=", 0.5],
          "rec_kinds": ["reply"], "orgs_with": orgs, "orgs_without": orgs}
    if means:
        ev.update(mean_with=0.312, mean_without=-0.041)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO intel_patterns (key, cohort, hypothesis, n_with, n_without, effect, effect_unit, cohen_d, "
                 "p_value, q_value, confidence, sentence, evidence_json, status, last_confirmed) VALUES "
                 "(?,'pizza','reply_fast_rating',8,8,0.35,'★',0.9,0.01,0.05,0.7,'Across 16 pizza, x.',?,'active',"
                 "datetime('now'))", (key, json.dumps(ev)))
    conn.commit()
    conn.close()


def test_an_owner_pattern_carries_no_group_means_and_needs_eight_organisations_a_side(db_path):
    _pattern(db_path, "pizza:a", orgs=8)
    _pattern(db_path, "pizza:b", orgs=7)
    owner = {p["key"]: p for p in patterns.active("pizza", db_path=db_path)}
    assert set(owner) == {"pizza:a"}
    assert "mean_with" not in owner["pizza:a"]["evidence"] and "mean_without" not in owner["pizza:a"]["evidence"]
    admin = {p["key"]: p for p in patterns.active("pizza", db_path=db_path, projection="admin")}
    assert set(admin) == {"pizza:a", "pizza:b"} and admin["pizza:a"]["evidence"]["mean_with"] == 0.312
    assert all("mean_with" not in json.dumps(p) for p in patterns.active(db_path=db_path))


def test_a_patterns_figures_are_frozen_for_the_week(db_path):
    import random
    rng = random.Random(4)
    ids = []
    for i in range(18):
        fast = i % 2 == 0
        f = {"response_24h_rate_30d": 0.8 if fast else 0.1,
             "avg_rating_delta": round((0.35 if fast else -0.05) + rng.uniform(-0.05, 0.05), 3)}
        ids.append(_mk(db_path, f"Freeze {i}", profile=("counter", "pizza"), feats=f))
    cohorts = {r: "pizza" for r in ids}
    patterns.discover(db_path=db_path, cohorts=cohorts, shuffles=300, wall_seconds=60)
    key = "pizza:reply_fast_rating"
    first = get_conn(db_path).execute("SELECT effect, evidence_json FROM intel_patterns WHERE key=?", (key,)).fetchone()
    assert first and json.loads(first["evidence_json"])["week"] == WEEK
    conn = get_conn(db_path)
    for r in ids[::2]:
        conn.execute("UPDATE intel_features SET features_json=? WHERE restaurant_id=?",
                     (json.dumps({"response_24h_rate_30d": 0.8, "avg_rating_delta": 0.6}), r))
    conn.commit()
    conn.close()
    patterns.discover(db_path=db_path, cohorts=cohorts, shuffles=300, wall_seconds=60)
    again = get_conn(db_path).execute("SELECT effect FROM intel_patterns WHERE key=?", (key,)).fetchone()
    assert again["effect"] == first["effect"]                          # same week: not re-figured
    patterns.discover(db_path=db_path, cohorts=cohorts, today=date.today() + timedelta(days=7), shuffles=300,
                      wall_seconds=60)
    later = get_conn(db_path).execute("SELECT effect FROM intel_patterns WHERE key=?", (key,)).fetchone()
    assert later["effect"] != first["effect"]


# ══ #14: the labor cost basis and definitions ═══════════════════════════════

def test_the_labor_cost_basis_is_recorded_and_the_default_is_out_of_labor_bands(db_path):
    assert thresholds.labor_cost_basis({"hourly_rate": 26.0}) == "default"
    assert thresholds.labor_cost_basis({"hourly_rate": 21.5}) == "owner_blended"
    assert thresholds.labor_cost_basis({"hourly_rate": 26.0, "role_rates_json": '{"Server": 9.5}'}) == "role_rates"
    assert thresholds.labor_vs_industry_monthly(28.0, 60000, 30, industry_pct=34.2, cost_basis="default") == 0
    ids = _group(db_path, 9, base={"labor_pct_28d": 28.0, "labor_hours_per_1k_28d": 8.0},
                 step={"labor_pct_28d": 0.2, "labor_hours_per_1k_28d": 0.1})
    _set(db_path, ids[0], hourly_rate=26.0)
    bm.compute(db_path=db_path)
    n = {r["metric"]: r["n"] for r in get_conn(db_path).execute(
        "SELECT metric, n FROM intel_benchmarks WHERE cohort='sm:counter'").fetchall()}
    assert n["labor_pct_28d"] == 8 and n["labor_hours_per_1k_28d"] == 9     # hours are not a cost


def test_the_gap_to_target_opportunity_is_withheld_on_the_default_wage(db_path, monkeypatch):
    import value_delivered
    rid = _mk(db_path, "Default Wage", hourly_rate=26.0)
    _set(db_path, rid, module_labor=1, module_inventory=0)
    lab = {"is_live": True, "potential_savings_monthly": 1200.0,
           "date_range": {"start": (date.today() - timedelta(days=28)).isoformat(),
                          "end": (date.today() - timedelta(days=1)).isoformat(), "days": 28}}
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: lab)
    out = value_delivered.opportunity(rid, db_path=db_path)
    assert out["items"] == [] and out["withheld"][0]["state"] == "default_rate"
    _set(db_path, rid, hourly_rate=19.5)
    assert value_delivered.opportunity(rid, db_path=db_path)["items"][0]["monthly"] == 1200.0


def test_a_published_figure_defined_differently_is_context_not_a_comparison():
    e = br.lookup("labor_pct", "italian")
    assert e["definition"] == "labor_incl_benefits" and "including benefits" in e["median_basis"]
    assert "profitable full-service operators' median" in e["median_basis"]
    assert br.lookup("labor_pct", "italian", definition="wages_from_shifts") is None
    # A rule of thumb that does not say what it counts is not like for like
    # either (re-audit #32, R4-5) — this pinned it as comparable.
    assert br.lookup("labor_pct", "fast_casual", definition="wages_from_shifts") is None
    r = Restaurant(name="Nonna", owner_email="n@x.test", category="italian")
    ind = eng.compare(None, "labor_pct_28d", kinds=("industry",), restaurant=r, rows=[])["comparisons"][0]
    assert ind["available"] and ind["comparable"] is False and "including benefits" in ind["definition_note"]
    assert eng._blend_entry("labor_pct_28d", r) is None                     # never blended toward it


# ══ #20: the ladder ════════════════════════════════════════════════════════

def test_a_guessed_or_unconfirmed_profile_gets_no_peers_and_the_prompt(db_path):
    rid = _mk(db_path, "Tony's Pizzeria", feats={"labor_pct_28d": 27.0})
    peers = eng.compare(rid, "labor_pct_28d", kinds=("peers",), db_path=db_path)["comparisons"][0]
    assert peers["available"] is False and "Restaurant profile" in peers["why_not"]
    assert peers["suggestion"]["text"] == "We think you're a pizzeria — is that right?"


def test_below_its_own_floor_the_restaurant_is_told_how_many_measured_days_are_left(db_path):
    rid = _mk(db_path, "New Counter", profile=("counter", "pizza"))
    conn = get_conn(db_path)
    for k in range(5):
        d = (date.today() - timedelta(days=k + 1)).isoformat()
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct, total_hours) "
                     "VALUES (?,?,?,?,?)", (rid, d, 2000, 28.0, 20))
    conn.commit()
    conn.close()
    peers = eng.compare(rid, "labor_pct_28d", kinds=("peers",), db_path=db_path)["comparisons"][0]
    assert peers["available"] is False and peers["why_not"] == "about 9 more measured days to a comparison"


def test_a_small_group_is_blended_toward_a_like_for_like_published_median_and_says_so():
    band = {"p25": 28.0, "p50": 29.0, "p75": 30.0}
    entry = {"median": 33.0, "short": "Test source", "source_kind": "published", "data_year": 2024}
    out, blend = eng._blend("labor_pct_28d", band, 8, entry)
    assert blend["group_weight_pct"] == 50 and "because the group is small" in blend["text"]
    assert out["p50"] == 31.0 and out["p25"] == 30.0                        # half-way, at the 0.5 step
    big, b2 = eng._blend("labor_pct_28d", band, 72, entry)
    assert b2["group_weight_pct"] == 90 and big["p50"] == 29.5
    assert eng._blend("labor_pct_28d", band, 8, None) == (band, None)


# ══ #29, #30: the ledger, hysteresis and the changed-group signal ════════════

def test_the_ledger_records_rung_partition_and_a_hashed_peer_set(db_path):
    ids = _group(db_path, 10)
    jobs.run_learning(db_path=db_path)
    rows = get_conn(db_path).execute("SELECT * FROM intel_peer_assignments WHERE restaurant_id=? ORDER BY family",
                                     (ids[0],)).fetchall()
    by = {r["family"]: dict(r) for r in rows}
    assert set(by) == {"format", "labor", "food"}
    assert by["labor"]["rung"] == "peers" and by["labor"]["partition_key"] == "sm:counter"
    assert by["labor"]["n"] == 9 and by["labor"]["orgs"] == 9 and len(by["labor"]["peer_set_hash"]) == 16
    assert by["food"]["partition_key"] == "sm:counter|starch" and by["labor"]["profile_source"] == "set"
    assert all(c in "0123456789abcdef" for c in by["labor"]["peer_set_hash"])   # a hash, never the ids
    cols = {r["name"] for r in get_conn(db_path).execute("PRAGMA table_info(intel_peer_assignments)").fetchall()}
    assert not any("member" in c or c.endswith("_ids") for c in cols)


def test_a_changed_group_is_logged_and_lowers_confidence(db_path):
    ids = _group(db_path, 10)
    jobs.run_learning(db_path=db_path, today=date.today() - timedelta(days=7))
    update_restaurant(ids[0], {"service_model": "full_service"}, db_path=db_path)
    out = jobs.run_learning(db_path=db_path)
    assert out["peer_ledger"]["groups_changed"] >= 1
    ev = get_conn(db_path).execute("SELECT event_data FROM activity_log WHERE restaurant_id=? AND "
                                   "event_type='comparison_group_changed'", (ids[0],)).fetchall()
    assert ev and json.loads(ev[0]["event_data"])["to"].startswith("sm:full_service")
    change, note = confidence._recent_changes(ids[0], db_path=db_path)
    assert change > 0 and "comparison group changed" in note


def test_measured_drift_moves_a_partition_only_after_four_weeks(db_path):
    ids = _group(db_path, 3, base={"labor_pct_28d": 28.0, "alcohol_share": 0.6})
    members = jobs.member_info(db_path=db_path)
    latest = feat.latest_by_restaurant(db_path=db_path)
    now = jobs.peer_partitions(members, latest=latest, db_path=db_path)
    assert now[ids[0]]["labor"] == "sm:counter" and now[ids[0]]["_drift"] == "bar_led"
    conn = get_conn(db_path)
    for back in (1, 2, 3):
        wk = feat.iso_week(date.today() - timedelta(weeks=back))
        conn.execute("INSERT INTO intel_peer_assignments (restaurant_id, week, family, rung, partition_key, drift) "
                     "VALUES (?,?,'labor','self','sm:counter','bar_led')", (ids[0], wk))
    conn.commit()
    conn.close()
    moved = jobs.peer_partitions(members, latest=latest, db_path=db_path)
    assert moved[ids[0]]["labor"] == "sm:counter|bar" and moved[ids[0]]["format"] == "sm:counter"
    assert moved[ids[1]]["labor"] == "sm:counter"                          # no history, no move


# ══ #31: the confidence type-match factor uses the published rule ═══════════

def test_type_match_reads_the_published_rule(db_path):
    ids = _group(db_path, 6)                                           # stored (≥5) but never published (<8)
    bm.compute(db_path=db_path)
    r = get_restaurant(ids[0], db_path=db_path)
    tm = confidence._type_match(ids[0], r, "labor_pct_28d", db_path)
    assert tm["value"] == 0.4 and "too few" in tm["note"]
    _wipe_bands(db_path)
    _group(db_path, 6, start=20)
    bm.compute(db_path=db_path)
    tm = confidence._type_match(ids[0], r, "labor_pct_28d", db_path)
    assert tm["value"] == 1.0 and "11 others from 11 owners" in tm["note"]
    unconfirmed = _mk(db_path, "Plain")
    assert confidence._type_match(unconfirmed, get_restaurant(unconfirmed, db_path=db_path), None, db_path) is None


# ══ #39: the quality gate ═════════════════════════════════════════════════════

def test_other_and_untyped_groups_are_never_published(db_path):
    ids = [_mk(db_path, f"Other {i}", feats={"labor_pct_28d": 28 + i * 0.2}) for i in range(10)]
    for r in ids:
        update_restaurant(r, {"category": "other"}, db_path=db_path)
    bm.compute(db_path=db_path, cohorts={r: "other" for r in ids})
    assert bm._row("other", "labor_pct_28d", db_path=db_path) is None
    assert bm.published("other", "labor_pct_28d", db_path=db_path)["withheld"]


def test_a_band_too_spread_to_mean_alike_is_withheld(db_path):
    _group(db_path, 10, base={"labor_pct_28d": 18.0}, step={"labor_pct_28d": 2.5})    # 18% … 40.5%
    bm.compute(db_path=db_path)
    p = bm.published("sm:counter", "labor_pct_28d", db_path=db_path)
    assert p["withheld"] and "too spread out" in p["reason"]
    assert reg.spread_ok("labor_pct_28d", 28, 29, 30) and not reg.spread_ok("labor_pct_28d", 20, 29, 38)


def test_members_need_eight_live_weeks_and_half_their_measures(db_path):
    ids = _group(db_path, 9)
    _set(db_path, ids[0], created_at=(date.today() - timedelta(days=20)).isoformat())
    conn = get_conn(db_path)
    conn.execute("UPDATE intel_features SET completeness=0.3 WHERE restaurant_id=?", (ids[1],))
    conn.commit()
    conn.close()
    out = bm.compute(db_path=db_path)
    assert out["skipped"] == {"fewer than 8 weeks live on Cavnar": 1, "less than half of its measures on file": 1}


# ══ #42: disclosure control on what is published ════════════════════════════

def test_published_quartiles_are_smoothed_coarse_and_frozen_for_the_week(db_path):
    vals = [4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9]
    ids = [_mk(db_path, f"Stars {i}", profile=("counter", "pizza"), feats={"avg_rating_30d": v})
           for i, v in enumerate(vals)]
    viewer = _mk(db_path, "Stars viewer", profile=("counter", "pizza"), feats={"avg_rating_30d": 4.0})
    bm.compute(db_path=db_path)
    p = bm.published("sm:counter", "avg_rating_30d", exclude_org=bm.viewer_org(viewer, db_path=db_path),
                     db_path=db_path)
    for q in ("p25", "p50", "p75"):
        assert (p[q] * 4) == int(p[q] * 4)                              # the 0.25★ step
    assert stats.harrell_davis(vals, 25) not in vals                      # never a member's exact figure
    before = get_conn(db_path).execute("SELECT members_json FROM intel_benchmarks WHERE cohort='sm:counter' "
                                       "AND metric='avg_rating_30d'").fetchone()[0]
    conn = get_conn(db_path)
    conn.execute("UPDATE intel_features SET features_json=? WHERE restaurant_id=?",
                 (json.dumps({"avg_rating_30d": 3.0}), ids[0]))
    conn.commit()
    conn.close()
    bm.compute(db_path=db_path)
    after = get_conn(db_path).execute("SELECT members_json FROM intel_benchmarks WHERE cohort='sm:counter' "
                                      "AND metric='avg_rating_30d'").fetchone()[0]
    assert after == before                                               # this week's band stands
    assert reg.meta("avg_rating_30d")["step"] == 0.25 and bm._STEP["avg_rating_30d"] == 0.25


# ══ #43: the structural block ════════════════════════════════════════════════

def test_the_structural_block_is_bands_and_shares_never_dollars(db_path):
    rid = _mk(db_path, "Structure Co", open_times_json='{"Monday": "11:00am", "Friday": "5:00pm"}',
              close_times_json='{"Monday": "9:00pm", "Friday": "1:00am"}', delivery_pct=15)
    conn = get_conn(db_path)
    for k in range(20):
        d = (date.today() - timedelta(days=k + 1)).isoformat()
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct, total_hours) "
                     "VALUES (?,?,?,?,?)", (rid, d, 4000, 28.0, 40))
        conn.execute("INSERT INTO covers_daily (restaurant_id, date, covers) VALUES (?,?,?)", (rid, d, 160))
        for cat, v in (("Food", 2600), ("Liquor", 900), ("Beer", 500)):
            conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) "
                         "VALUES (?,?,?,?,'final')", (rid, d, f"sales.cat:{cat}", v))
    conn.commit()
    s = feat.structural(conn, rid, date.today())
    conn.close()
    assert s["weekly_open_hours"] == 18.0 and s["delivery_share"] == 0.15
    assert s["ticket_band"] == 2                                          # $25 a cover
    assert s["alcohol_share"] == 0.35
    assert s["urbanity_band"] is None and s["daypart_mix"] is None        # not measured: None, never 0
    privacy.assert_anonymous({k: v for k, v in s.items()}, deny_names=[])
    for k in feat.STRUCTURAL_KEYS:
        assert not any(stem in k for stem in privacy.FORBIDDEN_KEY_STEMS), k


# ══ #44: trends over a steady panel ══════════════════════════════════════════

def test_trends_hold_membership_steady_and_are_persisted(db_path):
    steady = [_mk(db_path, f"Steady {i}", profile=("counter", "pizza")) for i in range(6)]
    joiners = [_mk(db_path, f"Joiner {i}", profile=("counter", "pizza")) for i in range(4)]
    conn = get_conn(db_path)
    for w in range(8):
        wk = feat.iso_week(date.today() - timedelta(weeks=7 - w))
        for r in steady:
            conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) "
                         "VALUES (?,?,?,0.9)", (r, wk, json.dumps({"labor_pct_28d": 30.0})))
        if w >= 6:                                                       # two lean joiners, last two weeks
            for r in joiners:
                conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) "
                             "VALUES (?,?,?,0.9)", (r, wk, json.dumps({"labor_pct_28d": 20.0})))
    conn.commit()
    conn.close()
    members = jobs.member_info(db_path=db_path)
    parts = jobs.peer_partitions(members, db_path=db_path)
    s = trends.panel_series(cohorts=parts, db_path=db_path)["sm:counter"]["labor_pct_28d"]
    assert [p["p50"] for p in s["points"]] == [30.0] * len(s["points"])  # joining is not a trend
    assert s["n_panel"] == 6 and s["n_joined"] == 4 and s["n_left"] == 0
    out = trends.persist(cohorts=parts, db_path=db_path)
    assert out["written"] >= 6
    row = get_conn(db_path).execute("SELECT n_joined FROM intel_cohort_series WHERE cohort='sm:counter' "
                                    "AND metric='labor_pct_28d' ORDER BY week DESC LIMIT 1").fetchone()
    assert row["n_joined"] == 4


# ══ #47: admin and public projections ════════════════════════════════════════

def test_the_admin_table_is_rounded_as_published_and_admin_only(db_path):
    _group(db_path, 6, base={"labor_pct_28d": 28.13}, step={"labor_pct_28d": 0.37})
    bm.compute(db_path=db_path)
    rows = bm.cohort_table(db_path=db_path)
    lab = next(r for r in rows if r["metric"] == "labor_pct_28d")
    assert all((lab[q] * 2) == int(lab[q] * 2) for q in ("p25", "p50", "p75"))
    assert lab["cohort_label"] == "counter-service restaurants on Cavnar" and lab["orgs"] == 6
    import admin_routes
    src = open(admin_routes.__file__).read()
    i = src.index("def admin_api_intelligence")
    assert 'if not current_user.get("is_admin")' in src[i:i + 900]
    assert "Cavnar cohorts" in open(admin_routes.__file__.replace("admin_routes.py", "templates/admin.html")).read()


def test_the_public_status_page_never_prints_a_count(monkeypatch):
    import status_manager
    assert status_manager._locations(1) == "one or more locations"
    src = open(status_manager.__file__).read()
    assert "location(s)" not in src and "of {active_with_gmb}" not in src
