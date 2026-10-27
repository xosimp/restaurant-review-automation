"""Audit #25 WS10 — the staging load-test kit (A01-16, A01-19; scripts/loadtest/).

The seeder refuses anything but a keyless staging environment whose database is its own; the world it
plans spreads over five time zones with the 24-hour diner, the 2am bar, the multi-location org, the chain
and the mass-time-off week in it; a small world really seeds (through the app's own writes); and the
results file judges a run against the README's pass line.

Nothing here reaches a provider, starts a server or reads the wall clock for a date.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.loadtest import results as R  # noqa: E402
from scripts.loadtest import seed_staging as S  # noqa: E402


@pytest.fixture
def clean_env(monkeypatch):
    """No deploy environment, no Railway, no provider key."""
    for k in ("CAVNAR_ENV", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT") + S.PROVIDER_KEYS:
        monkeypatch.delenv(k, raising=False)
    import config
    monkeypatch.delattr(config, "deploy_env", raising=False)


@pytest.fixture
def staging(clean_env, monkeypatch):
    monkeypatch.setenv("CAVNAR_ENV", "staging")


# ── the guards ─────────────────────────────────────────────────────────────

def test_a_local_shell_is_refused(clean_env):
    problems = S.check_environment()
    assert len(problems) == 1 and "'local'" in problems[0] and "staging" in problems[0]


def test_production_is_refused(clean_env, monkeypatch):
    monkeypatch.setenv("CAVNAR_ENV", "production")
    assert any("'production'" in p for p in S.check_environment())


def test_a_railway_production_environment_is_refused_whatever_cavnar_env_says(staging, monkeypatch):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    assert any("RAILWAY_ENVIRONMENT_NAME is 'production'" in p for p in S.check_environment())
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert any("RAILWAY_ENVIRONMENT is 'production'" in p for p in S.check_environment())


def test_clean_staging_is_accepted(staging):
    assert S.check_environment() == []


@pytest.mark.parametrize("key", ["ANTHROPIC_API_KEY", "RESEND_API_KEY", "TWILIO_AUTH_TOKEN", "STRIPE_SECRET_KEY",
                                 "GOOGLE_PLACES_API_KEY", "APNS_PRIVATE_KEY", "BACKUP_S3_SECRET_ACCESS_KEY",
                                 "SENTRY_DSN", "OPS_PAGER_ROUTING_KEY", "HEALTHCHECK_PING_URL"])
def test_every_provider_key_is_refused(staging, monkeypatch, key):
    monkeypatch.setenv(key, "x")
    problems = S.check_environment()
    assert len(problems) == 1 and key in problems[0]


def test_an_empty_key_counts_as_unset(staging, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    assert S.check_environment() == []


def test_config_deploy_env_decides_once_it_exists(clean_env, monkeypatch):
    """WS2's contract: config.deploy_env() is the one answer when it is there."""
    import config
    monkeypatch.setattr(config, "deploy_env", lambda: "staging", raising=False)
    assert S.check_environment() == []
    monkeypatch.setenv("CAVNAR_ENV", "staging")
    monkeypatch.setattr(config, "deploy_env", lambda: "production", raising=False)
    assert any("'production'" in p for p in S.check_environment())


def _routed(monkeypatch, db_path):
    """Every module's get_conn (and the default database) is the test's database."""
    import models
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if mod is not None and getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", conn)
        except Exception as e:      # a module that refuses getattr is not one that holds get_conn
            print(f"skip {getattr(mod, '__name__', mod)}: {e}")
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def test_a_database_with_a_real_restaurant_is_refused(staging, db_path, monkeypatch):
    import models
    _routed(monkeypatch, db_path)
    assert S.check_database() == []
    rid = models.create_restaurant(models.Restaurant(name="A real client", owner_email="o@real.example"))
    problems = S.check_database()
    assert len(problems) == 1 and str(rid) in problems[0]
    with pytest.raises(S.SeedRefused):
        S._guard()
    # The kit's own stores (and a demo) are its own.
    models.update_restaurant(rid, {"internal_notes": S.MARK})
    demo = models.create_restaurant(models.Restaurant(name="Showcase", owner_email="d@x.example", is_demo=1))
    assert demo and S.check_database() == []


def test_a_refused_seed_writes_nothing(clean_env, db_path, monkeypatch):
    import models
    _routed(monkeypatch, db_path)
    with pytest.raises(SystemExit) as e:
        S.seed(S.plan_world(3), out=None)
    assert "refusing to seed" in str(e.value)
    conn = models.get_conn(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_plan_writes_nothing_and_needs_no_staging(clean_env, capsys):
    assert S.main(["--restaurants", "100", "--plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stores"] == 100


# ── the world ──────────────────────────────────────────────────────────────

def test_a_hundred_stores_land_on_the_five_zones_with_every_special():
    world = S.plan_world(100)
    s = S.summarize(world)
    assert s["stores"] == 100
    assert s["by_timezone"] == {"America/Chicago": 30, "America/New_York": 25, "America/Los_Angeles": 20,
                                "America/Denver": 15, "Pacific/Honolulu": 10}
    kinds = s["by_kind"]
    assert kinds["diner_24h"] == 1 and kinds["late_bar"] == 1 and kinds["mass_time_off"] == 1
    assert kinds["multi_location_org"] == 2                     # its two restaurants
    assert 45 <= kinds["pos"] <= 55
    chain = [g for g in s["groups"] if g.startswith("Loadtest Chain")]
    assert chain and len(s["groups"][chain[0]]) == 5           # one organization across every zone
    by_kind_tz = {(st["kind"], st["timezone"]) for st in world}
    assert ("diner_24h", "America/New_York") in by_kind_tz and ("late_bar", "America/Los_Angeles") in by_kind_tz


def test_five_hundred_stores_scale_every_special():
    s = S.summarize(S.plan_world(500))
    assert s["stores"] == 500
    assert s["by_timezone"]["Pacific/Honolulu"] == 50 and s["by_timezone"]["America/Chicago"] == 150
    assert s["by_kind"]["diner_24h"] == 5 and s["by_kind"]["late_bar"] == 5 and s["by_kind"]["mass_time_off"] == 5
    assert len([g for g in s["groups"] if g.startswith("Loadtest Chain")]) == 5


def test_the_world_is_the_same_every_time_and_a_tiny_one_is_still_spread():
    assert S.plan_world(100) == S.plan_world(100)
    tiny = S.summarize(S.plan_world(5))
    assert tiny["stores"] == 5 and len(tiny["by_timezone"]) >= 4 and "diner_24h" not in tiny["by_kind"]


def test_the_specials_are_what_their_names_say():
    import schedule_benchmark as sb
    bar = sb.ARCHETYPES["late_bar"]()
    assert bar["close"]["Tuesday"] == "2:00am" and bar["close"]["Saturday"] == "3:00am"
    diner = sb.ARCHETYPES["diner_24h"]()
    assert diner["open"]["Monday"] == diner["close"]["Monday"] == "12:00am"
    assert len(sb.ARCHETYPES["mass_time_off"]()["time_off"]) == 13
    assert len(sb.ARCHETYPES["multi_location_org"]()["siblings"]) == 1


def test_a_small_world_really_seeds_through_the_apps_own_writes(staging, db_path, monkeypatch, tmp_path):
    """Twelve stores: every special, the chain across five zones, logins in users.csv."""
    import models
    _routed(monkeypatch, db_path)
    world = S.plan_world(12)
    out = str(tmp_path / "users.csv")
    rows = S.seed(world, managers=1, staff=1, history_days=2, out=out, progress=None)
    conn = models.get_conn(db_path)
    try:
        got = conn.execute("SELECT id, timezone, billing_status, is_demo, internal_notes, location_group, "
                           "organization_id, close_times_json FROM restaurants ORDER BY id").fetchall()
    finally:
        conn.close()
    assert len(got) == 12
    assert {r["timezone"] for r in got} == {tz for tz, _w in S.TIMEZONES}
    assert all(r["internal_notes"] == S.MARK and r["billing_status"] == "active" for r in got)
    pos_ids = {r["id"] for r in got if r["is_demo"]}
    assert len(pos_ids) == 6, "the six POS stores share the one simulated store as demos; the rest are not"
    chain = [r for r in got if (r["location_group"] or "").startswith("Loadtest Chain")]
    assert len(chain) == 5 and len({r["organization_id"] for r in chain}) == 1 and chain[0]["organization_id"]
    assert len({r["timezone"] for r in chain}) == 5, "the POS bind's Chicago never overwrites a store's zone"
    shared = [r for r in got if (r["location_group"] or "").startswith("Loadtest Shared")]
    assert len(shared) == 2 and len({r["organization_id"] for r in shared}) == 1
    assert any('"2:00am"' in (r["close_times_json"] or "") for r in got), "the late bar closes at 2am"
    # Logins: three a restaurant, in users.csv with each store's zone and kind; the seeded database passes
    # its own guard again (a second seed or nightly_sim).
    import csv
    with open(out) as f:
        csv_rows = list(csv.DictReader(f))
    assert len(rows) == len(csv_rows) == 12 * 3
    assert {r["timezone"] for r in csv_rows} == {tz for tz, _w in S.TIMEZONES}
    assert all(r["username"] and r["password"] and r["kind"] for r in csv_rows)
    assert S.check_database() == []


# ── the results file and the morning burst ─────────────────────────────────

def _row(name, p95, requests=100, server_errors=0, method="GET"):
    return {"name": name, "method": method, "requests": requests, "failures": server_errors,
            "server_errors": server_errors, "p50_ms": p95 / 3, "p95_ms": p95, "p99_ms": p95 * 1.2,
            "max_ms": p95 * 2, "avg_ms": p95 / 2, "rps": 1.5}


def test_the_verdict_is_the_readmes_pass_line():
    rows = [_row("/api/home/brief", 900), _row("/api/morning-brief", 4800),       # slow route, under 5 s
            _row("/api/labor/bench", 2600),                                       # over 2 s: fails
            _row("/api/dsr", 300, server_errors=2),                               # any 5xx: fails
            _row("/api/rare", 9000, requests=3),                                  # too few to judge
            _row("/health", 450)]                                                 # over 300 ms: fails
    res = R.evaluate(rows, "morning_burst", {"host": "https://u:p@staging-x.up.railway.app/x", "mode": "rolling"})
    assert res["format"] == R.FORMAT and res["host"] == "staging-x.up.railway.app"
    assert res["verdict"]["ok"] is False
    failed = " | ".join(res["verdict"]["failed"])
    assert "/api/labor/bench p95 2600 ms > 2000 ms" in failed and "/api/dsr 2 server error(s)" in failed
    assert "/health p95 450 ms > 300 ms" in failed
    assert "morning-brief" not in failed and "/api/rare" not in failed
    assert res["totals"]["requests"] == 503 and res["totals"]["server_errors"] == 2
    assert res["routes"][0]["name"] == "/api/rare"                                 # slowest first


def test_a_clean_run_passes_and_stress_is_never_judged():
    assert R.evaluate([_row("/api/home/brief", 400), _row("/health", 80)], "sustained")["verdict"] == \
        {"ok": True, "failed": []}
    assert R.evaluate([_row("/api/home/brief", 9000, server_errors=5)], "stress")["verdict"]["ok"] is True
    with pytest.raises(ValueError):
        R.evaluate([], "tuesday")


def test_the_results_file_round_trips(tmp_path):
    res = R.evaluate([_row("/api/home/brief", 400)], "morning_burst",
                     {"started_at": "2026-10-26T13:00:00Z", "world": R.world_of(
                         [{"restaurant_id": "1", "timezone": "America/Chicago", "kind": "pos"},
                          {"restaurant_id": "1", "timezone": "America/Chicago", "kind": "pos"},
                          {"restaurant_id": "2", "timezone": "Pacific/Honolulu", "kind": "late_bar"}])})
    path = R.write(res, str(tmp_path))
    assert os.path.basename(path) == "morning_burst-20261026-130000Z.json"
    back = json.load(open(path))
    assert back == res and back["world"] == {"stores": 2, "logins": 3,
                                             "by_timezone": {"America/Chicago": 1, "Pacific/Honolulu": 1},
                                             "by_kind": {"pos": 1, "late_bar": 1}}


def test_the_synchronized_burst_ramps_holds_and_ends():
    total = 1000
    assert R.burst_users(0, total) == 1
    assert R.burst_users(150, total) == 200
    assert R.burst_users(300, total) == R.burst_users(2000, total) == 400
    assert R.burst_users(R.BURST_RAMP_SECONDS + R.BURST_HOLD_SECONDS, total) == 0


def test_the_rolling_burst_runs_each_zone_east_to_west():
    zones = {"America/Chicago": 100, "America/New_York": 100, "Pacific/Honolulu": 50}
    assert R.active_zones(0, zones) == ["America/New_York"]
    assert R.burst_users(300, 0, "rolling", zones) == 40                       # New York's wave at full
    t = R.WAVE_GAP_SECONDS + R.BURST_RAMP_SECONDS
    assert R.active_zones(t, zones) == ["America/New_York", "America/Chicago"]
    assert R.burst_users(t, 0, "rolling", zones) == 80
    last = 2 * R.WAVE_GAP_SECONDS + R.BURST_RAMP_SECONDS + R.BURST_HOLD_SECONDS
    assert R.active_zones(last - 1, zones) == ["Pacific/Honolulu"]
    assert R.burst_users(last, 0, "rolling", zones) == 0


def test_the_locustfile_parses():
    for name in ("locustfile.py", "seed_staging.py", "nightly_sim.py", "results.py"):
        path = os.path.join(ROOT, "scripts", "loadtest", name)
        compile(open(path, encoding="utf-8").read(), path, "exec")
