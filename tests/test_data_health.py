"""The Data Health Engine (data_health.py): the source_health sync ledger,
the registry overlay that turns failures into an error on the source, the
Restaurant Data Health Score and its caps, the Recommendation Confidence
Impact, the readiness gate before a model call, and the route twins.
Data Freshness audit 9/24/26 (DH5 §2)."""
import json
import types
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

import ai_utils
import confidence_engine as ce
import data_freshness as df
import data_health as dh
import models
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _fresh_cache():
    dh.invalidate()
    yield
    dh.invalidate()


def _rid(db_path, **kw):
    kw.setdefault("name", "Health Cafe")
    kw.setdefault("owner_email", "h@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _sales_day(db_path, rid, days_ago, sales=4200.0, labor=1100.0):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    conn = get_conn(db_path)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_cost, sales, total_hours) "
                 "VALUES (?,?,?,?,?)", (rid, d, labor, sales, 80))
    conn.commit()
    conn.close()


# ── the ledger ─────────────────────────────────────────────────────────────

def test_record_attempt_keeps_one_row_with_history_and_failure_run(db_path):
    rid = _rid(db_path)
    dh.record_attempt(rid, "pos", True, provider="toast", db_path=db_path)
    dh.record_attempt(rid, "pos", False, provider="toast", error="HTTP 503 from Toast", db_path=db_path)
    dh.record_attempt(rid, "pos", False, error="HTTP 503 from Toast", db_path=db_path)
    row = dh.health_rows(rid, db_path=db_path)["pos"]
    assert row["recent"] == "100" and row["consecutive_failures"] == 2
    assert row["error_class"] == "provider" and row["last_ok_at"] and row["first_failed_at"]
    assert row["provider"] == "toast"
    first_failed = row["first_failed_at"]
    dh.record_attempt(rid, "pos", False, error="HTTP 401 unauthorized", db_path=db_path)
    row = dh.health_rows(rid, db_path=db_path)["pos"]
    assert row["first_failed_at"] == first_failed and row["error_class"] == "auth"
    dh.record_attempt(rid, "pos", True, db_path=db_path)
    row = dh.health_rows(rid, db_path=db_path)["pos"]
    assert row["consecutive_failures"] == 0 and row["last_error"] is None and row["first_failed_at"] is None


def test_recent_outcomes_are_capped(db_path):
    rid = _rid(db_path)
    for i in range(dh.RECENT_MAX + 7):
        dh.record_attempt(rid, "reviews", i % 2 == 0, error="x", db_path=db_path)
    assert len(dh.health_rows(rid, db_path=db_path)["reviews"]["recent"]) == dh.RECENT_MAX


def test_record_attempt_never_raises_on_a_bad_database(tmp_path):
    dh.record_attempt(1, "pos", True, db_path=str(tmp_path / "nope" / "missing.db"))


@pytest.mark.parametrize("text,cls", [("HTTP 401", "auth"), ("token expired", "auth"), ("429 Too Many Requests", "rate_limit"),
                                      ("read timed out", "timeout"), ("503 Service Unavailable", "provider"),
                                      ("RPOWER truncated page", "provider"), ("KeyError 'x'", "internal")])
def test_errors_are_classified(text, cls):
    assert dh.classify_error(text) == cls


# ── the registry overlay ───────────────────────────────────────────────────

def test_a_failing_sync_is_an_error_on_the_source_even_with_current_data(db_path):
    """Sales through yesterday read current — until the sync that brings
    them has failed: then the source is failing, held under ERROR_CEILING,
    so it trips the stale cap (DH3-13, DH5-1)."""
    rid = _rid(db_path)
    _sales_day(db_path, rid, 1)
    r = models.get_restaurant(rid, db_path=db_path)
    before = df.source_state(r, "sales", db_path=db_path)
    assert before["state"] == "current" and not before.get("error")
    dh.record_attempt(rid, "sales", False, error="HTTP 503", db_path=db_path)
    after = df.source_state(r, "sales", db_path=db_path)
    assert after["error"] and "sync failing" in after["error"]
    assert after["pct"] <= int(df.ERROR_CEILING * 100) < ce.STALE_BELOW
    assert after["consecutive_failures"] == 1 and after["last_attempt_at"]


def test_reviews_need_two_failures_before_they_read_failing(db_path):
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    base = {"key": "reviews", "label": "Reviews", "pct": 100, "state": "current", "error": None}
    conn = get_conn(db_path)
    dh.record_attempt(rid, "reviews", False, error="x", db_path=db_path)
    one = df._with_health(dict(base), "reviews", r, conn)
    dh.record_attempt(rid, "reviews", False, error="x", db_path=db_path)
    two = df._with_health(dict(base), "reviews", r, conn)
    conn.close()
    assert not one.get("error") and two.get("error")


def test_no_ledger_row_changes_nothing(db_path):
    rid = _rid(db_path)
    _sales_day(db_path, rid, 1)
    r = models.get_restaurant(rid, db_path=db_path)
    s = df.source_state(r, "sales", db_path=db_path)
    assert "consecutive_failures" not in s and not s.get("error")


# ── the score ──────────────────────────────────────────────────────────────

def _line(key, pct, state=None, error=None, health=None):
    return {"key": key, "label": dh.OWNER_LABEL.get(key, key), "state": state or ce.state(pct), "pct": pct,
            "health_pct": pct if health is None else health, "line": f"{key} line", "error": error,
            "error_class": None}


def test_overall_is_held_by_a_failing_source_so_it_cannot_hide():
    lines = [_line("reviews", 100), _line("competitor", 100), _line("visibility", 100),
             _line("marketing", 45, error="Marketing sync failing")]
    ov = dh.overall(lines, ["reviews", "intel", "visibility", "marketing"])
    assert ov["pct"] <= dh.ANY_ERROR_CAP and "any_error" in ov["caps_applied"]


def test_a_blocking_source_down_holds_the_score_under_half():
    lines = [_line("labor", 0, state="unknown"), _line("pos", 100), _line("sales", 100), _line("reviews", 100)]
    ov = dh.overall(lines, ["labor", "reviews"])
    assert ov["pct"] <= dh.BLOCKING_DOWN_CAP and "blocking_down" in ov["caps_applied"]


def test_not_connected_is_never_scored_and_nothing_connected_is_none():
    ov = dh.overall([_line("reviews", None, state="not_connected", health=None)], ["reviews"])
    assert ov["pct"] is None and ov["state"] == "not_connected"
    two = dh.overall([_line("reviews", 100), {**_line("marketing", None, state="not_connected"), "health_pct": None}],
                     ["reviews", "marketing"])
    assert two["pct"] == 100


def test_snapshot_payload_shape_and_route_twins(db_path, monkeypatch):
    rid = _rid(db_path)
    _sales_day(db_path, rid, 1)
    monkeypatch.setattr(models, "DB_PATH", db_path, raising=False)
    snap = dh.snapshot(rid, restaurant=models.get_restaurant(rid, db_path=db_path), db_path=db_path, use_cache=False)
    assert snap["ok"] and "overall" in snap and isinstance(snap["sources"], list)
    assert {m["module"] for m in snap["modules"]} >= {"labor", "reviews"}
    assert all(s["state"] != "not_connected" for s in snap["sources"])
    assert any(n["key"] == "reviews" for n in snap["not_connected"])
    sales = [s for s in snap["sources"] if s["key"] == "sales"][0]
    assert sales["line"].startswith("Sales through") and not sales["line"].startswith("Sales: Sales") and sales["tone"] == "ok"
    json.dumps(snap)


def test_live_is_said_only_inside_a_sub_hourly_cadence():
    now = datetime(2026, 9, 24, 18, 0, tzinfo=timezone.utc)
    synced = "2026-09-24 17:58:00"
    nightly = dh.source_line({"key": "pos", "label": "POS", "state": "current", "pct": 100,
                              "basis": "Toast sales through 9/23/26", "last_ok_at": synced}, now)
    assert "Live" not in nightly["line"] and "synced 2 min ago" in nightly["line"]


# ── confidence impact ──────────────────────────────────────────────────────

def test_impact_replays_freshness_as_current_and_respects_the_record_cap():
    ev, acc = {"pct": 95}, {"pct": 92, "p_beats": 0.95}
    stale = {"pct": 30, "errors": [], "basis": "Counts from 9/1/26", "stalest": "inventory"}
    i = ce.impact(ev, acc, stale)
    assert i["now"] <= ce.STALE_CAP < i["when_current"] and i["delta"] > 0
    held = ce.impact({"pct": 95}, None, stale)          # no track record: held at 70 either way
    assert held["when_current"] <= ce.NO_TRACK_RECORD_CAP
    conf = ce.assemble(ev, acc, stale)
    fi = ce.freshness_impact(conf)
    assert fi["now"] == conf["pct"] and fi["blocked_by"] == "inventory"
    assert ce.freshness_impact(ce.assemble(ev, acc, {"pct": 100, "errors": []}))["blocked_by"] is None


# ── readiness ──────────────────────────────────────────────────────────────

def test_readiness_refuses_unattended_without_its_blocking_source_but_caveats_interactive(db_path):
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    un = dh.readiness(rid, "labor", restaurant=r, db_path=db_path, delivery="unattended")
    it = dh.readiness(rid, "labor", restaurant=r, db_path=db_path, delivery="interactive")
    assert un["decision"] == "refuse" and un["blocking"] == "labor"
    assert it["decision"] == "caveat"


def test_readiness_proceeds_on_current_data_and_carries_the_prompt_block(db_path):
    rid = _rid(db_path)
    _sales_day(db_path, rid, 1)
    r = models.get_restaurant(rid, db_path=db_path)
    out = dh.readiness(rid, "demand", restaurant=r, db_path=db_path, sources=("sales",))
    assert out["decision"] == "proceed"
    assert out["prompt_block"].startswith("DATA STATE") and "Sales through" in out["prompt_block"]
    assert out["data_state"]["stale_sources"] == []


def test_readiness_caveats_stale_data_and_feeds_the_validation_layer(db_path):
    rid = _rid(db_path)
    _sales_day(db_path, rid, 12)
    r = models.get_restaurant(rid, db_path=db_path)
    out = dh.readiness(rid, "demand", restaurant=r, db_path=db_path, sources=("sales",))
    assert out["decision"] in ("caveat", "refuse")
    assert out["data_state"]["stale_sources"] and out["data_state"]["data_age_days"] >= 12
    assert "OUT OF DATE" in out["prompt_block"] or "AGE UNKNOWN" in out["prompt_block"]


def test_readiness_waits_unattended_when_the_blocking_source_has_a_retry_due(db_path):
    rid = _rid(db_path)
    _sales_day(db_path, rid, 1)
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    dh.record_attempt(rid, "sales", False, error="HTTP 503", next_retry_at=later, db_path=db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    out = dh.readiness(rid, "dsr", restaurant=r, db_path=db_path, delivery="unattended")
    assert out["decision"] == "wait" and out["retry_after"] == later
    assert dh.due_retries("sales", db_path=db_path) == []
    assert dh.due_retries("sales", now=datetime.now(timezone.utc) + timedelta(hours=2), db_path=db_path) == [rid]


def test_merge_data_state_keeps_the_older_age():
    out = dh.merge_data_state({"stale_sources": ["Counts: 9/1/26"], "data_age_days": 3, "as_of": "9/21/26"},
                              {"stale_sources": ["Sales: 9/10/26", "Counts: 9/1/26"], "data_age_days": 14,
                               "as_of": "9/10/26"})
    assert out["stale_sources"] == ["Counts: 9/1/26", "Sales: 9/10/26"]
    assert out["data_age_days"] == 14 and out["as_of"] == "9/10/26"


def test_create_with_retry_refuses_before_any_network_call():
    called = []
    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=lambda **k: called.append(k)))
    with pytest.raises(ai_utils.DataNotReady) as e:
        ai_utils.create_with_retry(client, readiness={"decision": "refuse", "reason": "no shifts on file"},
                                   model="claude-sonnet-5", max_tokens=10, messages=[])
    assert not called and "no shifts on file" in str(e.value)
    assert isinstance(e.value, ai_utils.AIRefused)
    with pytest.raises(ai_utils.DataNotReady):
        ai_utils.create_with_retry(client, readiness={"decision": "wait", "reason": "POS retrying"},
                                   model="claude-sonnet-5", max_tokens=10, messages=[])
    assert not called


# ── routes ─────────────────────────────────────────────────────────────────

def _app(monkeypatch, db_path, rid):
    import auth
    import client_api
    import mobile_api
    monkeypatch.setattr(models, "DB_PATH", db_path, raising=False)
    user = {"id": 1, "restaurant_id": rid, "role": "owner", "email": "h@x.test"}
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    return user


def test_sync_now_is_deduplicated(db_path, monkeypatch):
    import client_api
    import ops
    import pos
    rid = _rid(db_path)
    user = {"restaurant_id": rid}
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("toast", object()))
    ran = []
    monkeypatch.setattr(pos, "sync_restaurant", lambda r, trigger="nightly": ran.append((r, trigger)))
    claims = iter([True, False])
    monkeypatch.setattr(ops, "claim_cooldown", lambda key, minutes: next(claims))
    first, s1 = client_api._do_data_health_sync(user, "pos")
    second, s2 = client_api._do_data_health_sync(user, "pos")
    assert s1 == 200 and first["started"] and s2 == 200 and second["already_syncing"]
    bad, s3 = client_api._do_data_health_sync(user, "weather")
    assert s3 == 400


def test_the_data_health_routes_exist_on_web_and_mobile():
    import client_api
    import mobile_api
    src_web = open(client_api.__file__).read()
    src_mob = open(mobile_api.__file__).read()
    assert '@client_bp.route("/api/data-health")' in src_web
    assert '@client_bp.route("/api/data-health/sync/<source>", methods=["POST"])' in src_web
    assert '@mobile_bp.route("/data-health")' in src_mob
    assert '@mobile_bp.route("/data-health/sync/<source>", methods=["POST"])' in src_mob
    assert "data_health.payload_for(current_user)" in src_web and "data_health.payload_for(current_user)" in src_mob


def test_every_pos_sync_attempt_is_recorded(db_path, monkeypatch):
    import pos
    rid = _rid(db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path, raising=False)
    fake = types.SimpleNamespace(sync_to_db=lambda r: {"ok": False, "error": "HTTP 503"})
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("toast", fake))
    rec = []
    monkeypatch.setattr(dh, "record_attempt", lambda *a, **k: rec.append((a, k)))
    out = pos.sync_restaurant(rid)
    assert out["ok"] is False and rec and rec[0][0][:3] == (rid, "pos", False)
    assert rec[0][1]["provider"] == "toast" and rec[0][1]["error"] == "HTTP 503"
    fake.sync_to_db = lambda r: (_ for _ in ()).throw(RuntimeError("boom"))
    out = pos.sync_restaurant(rid)
    assert out["ok"] is False and rec[-1][0][2] is False


def test_a_never_fetched_source_is_a_missing_input_not_out_of_date_data():
    """Weather with no forecast fetched yet reads `unknown` (DH2-17), but it
    is not old data: the answer checker's missing-input rule holds claims
    about it, and no "out of date (Weather)" caveat is attached."""
    st = {"key": "weather", "label": "Weather", "state": "unknown", "pct": None, "never_fetched": True,
          "basis": "Weather: no forecast fetched yet"}
    out = dh.validation_state([st])
    assert out["stale_sources"] == [] and out["missing_inputs"] == ["weather"]
