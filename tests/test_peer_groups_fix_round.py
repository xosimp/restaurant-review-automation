"""Peer groups and privacy — the re-audit fix round (9/24/26, workstream P).

Top-50 items #13 (one organisation identity), #22 (no catch-all food
group), #23 (the tightened spread gate), #34/#36 (the ladder and its finer
coordinates), #35 (a large organisation is held to a third, not a veto),
#37 (staffing's volume-band keying), #40 (the nightly learning pass), #41
(one publishing gate) and #44 (counts after the organisation is out; the
ledger's rungs from published()). #38 (drift as the owner's question) is in
test_peer_groups_privacy."""
import json
from datetime import date, timedelta

import pytest

import auth
import client_api
import mobile_api
import models
from models import Restaurant, create_restaurant, get_conn, get_restaurant

import intelligence  # noqa: E402 — imported before any fixture patches get_conn
from intelligence import (benchmarks as bm, categories, confidence, engine as eng,  # noqa: E402
                          features as feat, jobs, metrics_registry as reg, patterns, privacy, staffing, trends)
import benchmark_views  # noqa: E402

LIVE = (date.today() - timedelta(days=120)).isoformat() + "T00:00:00"
WEEK = feat.iso_week(date.today())


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for mod in (auth, client_api, mobile_api, intelligence, bm, confidence, eng, feat, jobs, patterns, trends,
                staffing):
        monkeypatch.setattr(mod, "get_conn", fake, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    privacy.invalidate_tenant_names()
    yield
    privacy.invalidate_tenant_names()


def _mk(db_path, name, profile=None, feats=None, completeness=0.9, week=WEEK, **cols):
    """A restaurant live 17 weeks on a real labor cost basis, its own owner
    email unless `owner_email` is given; `profile` is (service_model,
    concept) confirmed by the owner."""
    email = cols.pop("owner_email", None) or f"{''.join(c for c in name.lower() if c.isalnum())}@x.test"
    rid = create_restaurant(Restaurant(name=name, owner_email=email, created_at=cols.pop("created_at", LIVE),
                                       hourly_rate=cols.pop("hourly_rate", 18.0)), db_path=db_path)
    sets = dict(cols)
    if profile:
        sm, concept = profile
        sets.update(service_model=sm, concept=concept, category=concept, profile_source="set",
                    profile_confirmed_at="2026-09-01T00:00:00")
    if sets:
        _set(db_path, rid, **sets)
    if feats is not None:
        conn = get_conn(db_path)
        conn.execute("INSERT OR REPLACE INTO intel_features (restaurant_id, week, features_json, completeness) "
                     "VALUES (?,?,?,?)", (rid, week, json.dumps(feats), completeness))
        conn.commit()
        conn.close()
    return rid


def _set(db_path, rid, **cols):
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
    conn.commit()
    conn.close()


def _group(db_path, n, profile=("counter", "pizza"), base=None, start=0, prefix="Peer", **cols):
    base = base or {"labor_pct_28d": 28.0, "reply_rate_30d": 0.5, "avg_rating_30d": 4.2}
    out = []
    for i in range(start, start + n):
        f = {k: (round(v + i * (0.02 if v < 1 else 0.2), 3) if isinstance(v, float) else v) for k, v in base.items()}
        out.append(_mk(db_path, f"{prefix} {profile[0]} {i}", profile=profile, feats=f, **dict(cols)))
    return out


OUTSIDER = privacy.org_hash("r0")


# ══ #13: one organisation identity ═════════════════════════════════════════

def test_two_ungrouped_restaurants_under_one_owner_email_are_one_organisation(db_path):
    assert privacy.org_key({"id": 3, "owner_email": " Owner@X.test "}) == privacy.org_key(
        {"id": 4, "owner_email": "owner@x.test"}) == "eowner@x.test"
    assert privacy.org_key({"id": 3, "organization_id": 7, "owner_email": "a@x"}) == "o7"
    assert privacy.org_key({"id": 3}) == "r3"
    mine = [_mk(db_path, f"Mine {i}", profile=("counter", "pizza"), feats={"labor_pct_28d": 27.0 + i * 0.2},
                owner_email="owner@same.test") for i in range(2)]
    others = _group(db_path, 8, start=10)
    members = jobs.member_info(db_path=db_path)
    assert members[mine[0]]["org_hash"] == members[mine[1]]["org_hash"]
    assert members[others[0]]["org_hash"] != members[others[1]]["org_hash"]
    bm.compute(db_path=db_path)
    # The viewer's sister location is out of the band it sees: 8 others, not 9.
    p = bm.published("sm:counter", "labor_pct_28d", exclude_org=bm.viewer_org(mine[0], db_path=db_path),
                     db_path=db_path)
    assert p["n"] == 8 and p["orgs"] == 8
    row = get_conn(db_path).execute("SELECT orgs FROM intel_benchmarks WHERE cohort='sm:counter' AND "
                                    "metric='labor_pct_28d'").fetchone()
    assert row["orgs"] == 9                                         # 10 restaurants, 9 owners


def test_a_shared_owner_login_and_a_stripe_customer_join_restaurants_transitively(db_path):
    a = _mk(db_path, "Alpha Grill")
    b = _mk(db_path, "Bravo Grill")
    c = _mk(db_path, "Charlie Grill")
    d = _mk(db_path, "Delta Grill")
    auth.init_auth(db_path=db_path)
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO users (restaurant_id, username, email, password_hash, role) "
                       "VALUES (?,?,?,?,?)", (a, "owner1", "owner1@x.test", "x", "owner"))
    conn.execute("INSERT INTO memberships (user_id, restaurant_id, role) VALUES (?,?,?)", (cur.lastrowid, b, "owner"))
    # A manager who works at C and D does not make them one owner.
    cur = conn.execute("INSERT INTO users (restaurant_id, username, email, password_hash, role) "
                       "VALUES (?,?,?,?,?)", (c, "mgr", "mgr@x.test", "x", "manager"))
    conn.execute("INSERT INTO memberships (user_id, restaurant_id, role) VALUES (?,?,?)", (cur.lastrowid, d, "manager"))
    conn.commit()
    conn.close()
    _set(db_path, b, stripe_customer_id="cus_1")
    _set(db_path, c, stripe_customer_id="cus_1")                    # B and C share a Stripe customer
    m = privacy.org_map(db_path=db_path)
    assert m[a] == m[b] == m[c] and m[d] != m[a]
    canon, rows = privacy.org_members(a, db_path=db_path)
    assert canon == m[a] and {r["id"] for r in rows} == {a, b, c}
    assert privacy.org_members(d, db_path=db_path)[0] == m[d]
    # viewer_org also carries the keys a band frozen before the change used.
    assert privacy.org_hash(f"r{a}") in bm.viewer_org(a, db_path=db_path)


# ══ #22: no catch-all food group ═══════════════════════════════════════════

def test_other_and_unmapped_concepts_get_no_food_group(db_path):
    for concept in ("other", "family", "asian", "sports_bar", "fast_casual"):
        prof = {"confirmed": True, "service_model": "full_service", "concept": concept}
        assert categories.partition_key(prof, "food") is None, concept
        assert categories.partition_key(prof, "labor") == "sm:full_service"
    assert categories.partition_key({"confirmed": True, "service_model": "full_service", "concept": "pizza"},
                                    "food") == "sm:full_service|starch"
    assert not bm.publishable_group("sm:full_service|mixed") and not bm.publishable_group("sm:counter|bar|other")
    assert bm.publishable_group("sm:full_service|protein")
    rid = _mk(db_path, "Noodle Bar Co", profile=("full_service", "asian"), feats={"food_cost_pct_28d": 30.0})
    peers = eng.compare(rid, "food_cost_pct_28d", kinds=("peers",), db_path=db_path)["comparisons"][0]
    assert peers["available"] is False and "asian menus aren't grouped yet" in peers["why_not"]


# ══ #23: the spread gate ═══════════════════════════════════════════════════

def test_the_spread_gate_withholds_a_band_that_is_not_alike():
    assert reg.spread_ok("avg_rating_30d", 4.2, 4.4, 4.6) and not reg.spread_ok("avg_rating_30d", 4.1, 4.4, 4.6)
    assert reg.spread_ok("reply_rate_30d", 0.5, 0.6, 0.8) and not reg.spread_ok("reply_rate_30d", 0.4, 0.6, 0.8)
    assert reg.spread_ok("labor_pct_28d", 27, 30, 33) and not reg.spread_ok("labor_pct_28d", 25, 29, 33)


# ══ #34/#36: the ladder ════════════════════════════════════════════════════

def test_the_ladder_runs_finest_to_coarsest_and_labels_each_rung():
    prof = {"confirmed": True, "service_model": "full_service", "concept": "steakhouse", "bar_led": True}
    s = {"ticket_band": 3, "volume_band": 2, "urbanity_band": 2}
    assert categories.partition_ladder(prof, "labor", s) == [
        "sm:full_service|bar|t3|v2|u2", "sm:full_service|bar|t3|v2", "sm:full_service|bar|t3",
        "sm:full_service|bar", "sm:full_service"]
    assert categories.partition_ladder(prof, "food", s) == [
        "sm:full_service|bar|protein|t3", "sm:full_service|bar|protein", "sm:full_service|protein"]
    assert categories.partition_ladder(prof, "staff", {"volume_band": 2}) == [
        "sm:full_service|bar|v2", "sm:full_service|bar", "sm:full_service"]
    assert categories.partition_ladder(prof, "format", {}) == ["sm:full_service"]
    assert categories.partition_label("sm:full_service|bar|t3|v2|u2") == (
        "full-service, bar-led restaurants with an average ticket of $35–60 of similar sales volume in urban "
        "areas on Cavnar")
    assert categories.rung_note("sm:full_service", "sm:full_service|bar").startswith("a wider group")
    assert categories.rung_note("sm:full_service|bar|t3", "sm:full_service|bar") is None
    for key in categories.partition_ladder(prof, "labor", s):
        privacy.assert_anonymous({"cohort_label": categories.partition_label(key)}, deny_names=[])


def test_a_bar_led_restaurant_with_too_few_bar_led_peers_reads_the_wider_group_and_says_so(db_path):
    _group(db_path, 9, profile=("counter", "pizza"))
    viewer = _mk(db_path, "Bar Pizza Co", profile=("counter", "pizza"), feats={"labor_pct_28d": 27.0}, bar_led=1)
    jobs.run_learning(db_path=db_path)
    c = eng.compare(viewer, "labor_pct_28d", kinds=("peers",), db_path=db_path)["comparisons"][0]
    assert c["available"] and c["cohort"] == "sm:counter" and c["n"] == 9
    assert c["level"] == 1 and c["levels"] == 2 and c["wider_than_profile"]
    assert "a wider group — too few bar-led ones have this measured yet" in c["cohort_label"]


def test_a_finer_ticket_band_group_is_read_first_once_it_clears_the_floors(db_path):
    base = {"labor_pct_28d": 28.0, "ticket_band": 2}
    _group(db_path, 9, base=base)
    _group(db_path, 9, base={"labor_pct_28d": 29.0, "ticket_band": 1}, start=20, prefix="Cheap")
    viewer = _mk(db_path, "Ticket Two Co", profile=("counter", "pizza"),
                 feats={"labor_pct_28d": 27.5, "ticket_band": 2})
    bm.compute(db_path=db_path, cohorts=jobs.peer_partitions(jobs.member_info(db_path=db_path), db_path=db_path))
    c = eng.compare(viewer, "labor_pct_28d", kinds=("peers",), db_path=db_path)["comparisons"][0]
    assert c["available"] and c["cohort"] == "sm:counter|t2" and c["n"] == 9 and c["level"] == 0
    assert "an average ticket of $20–35" in c["cohort_label"] and "wider" not in c["cohort_label"]
    # The partition itself holds all 18 others, one rung coarser.
    coarse = bm.published("sm:counter", "labor_pct_28d", exclude_org=bm.viewer_org(viewer, db_path=db_path),
                          db_path=db_path)
    assert coarse["n"] == 18


def test_a_behaviour_metric_leads_with_the_all_types_band_when_no_partition_band_exists(db_path):
    _group(db_path, 10, profile=("counter", "pizza"))
    viewer = _mk(db_path, "Lone Steak", profile=("full_service", "steakhouse"),
                 feats={"reply_rate_30d": 0.9, "labor_pct_28d": 30.0})
    bm.compute(db_path=db_path)
    cm = eng.compare(viewer, "reply_rate_30d", db_path=db_path)
    assert cm["headline"]["kind"] == "platform" and "all types" in cm["headline"]["text"]
    row = benchmark_views.metric_row(cm)
    assert row["kind"] == "platform" and row["standing"] and "all types" in row["against"]
    labor = eng.compare(viewer, "labor_pct_28d", db_path=db_path)
    assert labor["headline"]["kind"] != "platform"


# ══ #35: a large organisation is held to a third, not a veto ═══════════════

def test_a_six_location_organisation_is_held_to_a_third_instead_of_withholding_the_band(db_path):
    chain = _group(db_path, 6, owner_email="chain@x.test", prefix="Chain")
    _group(db_path, 10, start=10)
    viewer = _mk(db_path, "Indie Viewer", profile=("counter", "pizza"), feats={"labor_pct_28d": 28.5})
    bm.compute(db_path=db_path)
    ex = bm.viewer_org(viewer, db_path=db_path)
    p = bm.published("sm:counter", "labor_pct_28d", exclude_org=ex, db_path=db_path)
    assert not p.get("withheld") and p["n"] == 15 and p["capped"] == 1 and p["measured"] == 16
    assert p == bm.published("sm:counter", "labor_pct_28d", exclude_org=ex, db_path=db_path)   # fixed per week
    kept, dropped = bm.cap_organisations([(1.0, "a")] * 6 + [(float(i), f"o{i}") for i in range(10)], week="2026-W39")
    counts = {}
    for _v, o in kept:
        counts[o] = counts.get(o, 0) + 1
    assert dropped == 1 and counts["a"] == 5 and counts["a"] / len(kept) <= 1 / 3
    # The chain's own locations see the 10 independents, none of their own.
    mine = bm.published("sm:counter", "labor_pct_28d", exclude_org=bm.viewer_org(chain[0], db_path=db_path),
                        db_path=db_path)
    assert mine["n"] == 11 and mine["capped"] == 0                      # 10 independents + the viewer above


# ══ #37: staffing's keys ═══════════════════════════════════════════════════

def test_staff_bands_are_stored_pooled_and_by_volume_and_a_new_restaurant_borrows_the_pooled_one(db_path):
    ids = []
    for k in range(9):
        ids.append(_mk(db_path, f"Staff Peer {k}", profile=("counter", "pizza"),
                       feats={"staff_per_1k.server.night": 0.5 + 0.01 * k, "volume_band": 2}))
    bm.compute(db_path=db_path, cohorts=jobs.peer_partitions(jobs.member_info(db_path=db_path), db_path=db_path))
    stored = {r["cohort"] for r in get_conn(db_path).execute(
        "SELECT cohort FROM intel_benchmarks WHERE metric='staff_per_1k.server.night'").fetchall()}
    assert {"sm:counter|v2", "sm:counter"} <= stored
    new = _mk(db_path, "Brand New Pizza", profile=("counter", "pizza"))
    conn = get_conn(db_path)
    for k in range(14):
        d = (date.today() - timedelta(days=k + 1)).isoformat()
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_pct, total_hours) "
                     "VALUES (?,?,?,?,?)", (new, d, 8000, 28.0, 60))
    conn.commit()
    conn.close()
    out = staffing.starting_headcount(new, roster_roles={"Ana": "Server"}, shifts=[], db_path=db_path)
    assert out.get("available"), out
    assert out["cohort"] == "sm:counter" and out["n"] == 9
    # The ratio never leaves the server through /api/benchmarks.
    user = {"restaurant_id": ids[0], "is_admin": 1}
    assert eng.payload_for(user, metric="staff_per_1k.server.night", db_path=db_path)["ok"] is False


# ══ #11 (A's rule, applied to members): one priced role is not a labor cost ═

def test_a_member_needs_most_of_its_hours_on_owner_set_rates_to_stand_in_a_labor_cost_band(db_path):
    ids = _group(db_path, 9)
    one_role = ids[0]
    _set(db_path, one_role, hourly_rate=26.0, role_rates_json=json.dumps({"Bartender": 15.0}))
    conn = get_conn(db_path)
    csv_rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"]
    csv_rows += [f"2026-09-{d:02d},Mon,Ana,Server,11:00,19:00,8,8,," for d in range(1, 11)]
    csv_rows += [f"2026-09-{d:02d},Mon,Bo,bartender ,17:00,21:00,4,4,," for d in range(1, 11)]
    conn.execute("INSERT INTO client_data (restaurant_id, shifts_csv) VALUES (?, ?)", (one_role, "\n".join(csv_rows)))
    conn.commit()
    conn.close()
    members = jobs.member_info(db_path=db_path)
    assert members[one_role]["cost_basis"] == "role_rates" and members[one_role]["labor_cost_sourced"] is False
    assert members[ids[1]]["labor_cost_sourced"] is True                  # an owner-set blended rate
    bm.compute(db_path=db_path, members=members)
    row = get_conn(db_path).execute("SELECT n FROM intel_benchmarks WHERE cohort='sm:counter' AND "
                                    "metric='labor_pct_28d'").fetchone()
    assert row["n"] == 8                                                   # one-third of hours priced: out
    # Pricing the server too puts every hour on the owner's rates.
    _set(db_path, one_role, role_rates_json=json.dumps({"Bartender": 15.0, "server": 12.0}))
    assert jobs.member_info(db_path=db_path)[one_role]["labor_cost_sourced"] is True


# ══ #40: the nightly learning pass ═════════════════════════════════════════

def test_a_lapsed_customer_leaves_the_bands_that_night(db_path):
    ids = _group(db_path, 9)
    _set(db_path, ids[0], billing_status="cancelled")
    out = bm.compute(db_path=db_path)
    assert out["skipped"].get("no longer an active customer") == 1
    row = get_conn(db_path).execute("SELECT n FROM intel_benchmarks WHERE cohort='sm:counter' AND "
                                    "metric='labor_pct_28d'").fetchone()
    assert row["n"] == 8


def test_bands_wait_for_a_complete_feature_sweep_and_one_stage_failing_does_not_stop_the_rest(db_path, monkeypatch):
    _group(db_path, 10)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?, '7', datetime('now')) "
                 "ON CONFLICT(key) DO UPDATE SET value='7'", (jobs.CURSOR_KEY,))
    conn.commit()
    conn.close()
    assert jobs.features_sweep_complete(db_path=db_path) is False
    out = jobs.run_learning(db_path=db_path)
    assert out["benchmarks"]["written"] == 0 and "held" in out["benchmarks"]
    assert get_conn(db_path).execute("SELECT COUNT(*) FROM intel_benchmarks").fetchone()[0] == 0
    jobs._cursor_set(db_path, 0)
    assert jobs.features_sweep_complete(db_path=db_path) is True

    def boom(**_k):
        raise RuntimeError("patterns broke")
    monkeypatch.setattr(jobs.patterns, "discover", boom)
    out = jobs.run_learning(db_path=db_path)
    assert out["patterns"] == {"error": "patterns broke"}
    assert out["benchmarks"]["written"] > 0 and out["peer_ledger"]["written"] > 0


# ══ #41: one publishing gate ═══════════════════════════════════════════════

def test_published_needs_a_viewer_and_never_serves_an_all_types_economics_band(db_path):
    _group(db_path, 10)
    bm.compute(db_path=db_path)
    assert bm.published("sm:counter", "labor_pct_28d", db_path=db_path)["withheld"]
    assert not bm.published("sm:counter", "labor_pct_28d", exclude_org=OUTSIDER, db_path=db_path).get("withheld")
    # A platform labor row written by anything but compute() is still refused.
    conn = get_conn(db_path)
    members = json.dumps([[28.0 + i * 0.1, f"h{i}"] for i in range(10)])
    conn.execute("INSERT INTO intel_benchmarks (cohort, metric, week, n, p25, p50, p75, mean, orgs, max_org_share, "
                 "members_json) VALUES ('platform','labor_pct_28d',?,10,28,28.5,29,28.5,10,0.1,?)", (WEEK, members))
    conn.commit()
    conn.close()
    p = bm.published("platform", "labor_pct_28d", exclude_org=OUTSIDER, db_path=db_path)
    assert p["withheld"] and "all-types" in p["reason"]


def test_a_trend_point_needs_the_bands_floor_and_a_harrell_davis_median(db_path):
    rids = [_mk(db_path, f"Trend {i}", profile=("counter", "pizza")) for i in range(7)]
    conn = get_conn(db_path)
    for w in range(7):
        wk = feat.iso_week(date.today() - timedelta(weeks=6 - w))
        for i, r in enumerate(rids):
            conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) "
                         "VALUES (?,?,?,0.9)", (r, wk, json.dumps({"labor_pct_28d": 26.0 + i})))
    conn.commit()
    conn.close()
    parts = jobs.peer_partitions(jobs.member_info(db_path=db_path), db_path=db_path)
    assert trends.panel_series(cohorts=parts, db_path=db_path) == {}          # 7 < 8: no point at all
    more = [_mk(db_path, f"Trend {i}", profile=("counter", "pizza")) for i in range(7, 9)]
    conn = get_conn(db_path)
    for w in range(7):
        wk = feat.iso_week(date.today() - timedelta(weeks=6 - w))
        for i, r in enumerate(more, start=7):
            conn.execute("INSERT INTO intel_features (restaurant_id, week, features_json, completeness) "
                         "VALUES (?,?,?,0.9)", (r, wk, json.dumps({"labor_pct_28d": 26.0 + i})))
    conn.commit()
    conn.close()
    parts = jobs.peer_partitions(jobs.member_info(db_path=db_path), db_path=db_path)
    pts = trends.panel_series(cohorts=parts, db_path=db_path)["sm:counter"]["labor_pct_28d"]["points"]
    assert pts and all(p["n"] == 9 and p["p50"] == 30.0 for p in pts)      # HD median, 0.5 step
    # One owner's nine locations make no trend.
    for r in rids + more:
        _set(db_path, r, owner_email="one@x.test")
    parts = jobs.peer_partitions(jobs.member_info(db_path=db_path), db_path=db_path)
    assert trends.panel_series(cohorts=parts, db_path=db_path) == {}


# ══ #44: counts, and the ledger's rungs ════════════════════════════════════

def test_measured_and_members_are_counted_with_the_viewers_organisation_out(db_path):
    ids = _group(db_path, 13)
    bm.compute(db_path=db_path)
    c = eng.compare(ids[0], "labor_pct_28d", kinds=("peers",), db_path=db_path)["comparisons"][0]
    assert c["n"] == 12 and c["measured"] == 12 and c["members"] == 12


def test_the_ledger_says_peers_only_when_a_band_is_actually_shown(db_path):
    # Ten members, but spread so wide that no band is ever shown.
    ids = _group(db_path, 10, base={"labor_pct_28d": 18.0})
    for i, r in enumerate(ids):
        conn = get_conn(db_path)
        conn.execute("UPDATE intel_features SET features_json=? WHERE restaurant_id=?",
                     (json.dumps({"labor_pct_28d": 18.0 + 2.5 * i}), r))
        conn.commit()
        conn.close()
    jobs.run_learning(db_path=db_path)
    row = get_conn(db_path).execute("SELECT rung, n FROM intel_peer_assignments WHERE restaurant_id=? AND "
                                    "family='labor'", (ids[0],)).fetchone()
    assert row["rung"] == "self" and row["n"] is None
    # A published figure defined differently from Cavnar's is not a rung.
    assert row["rung"] != "published"
