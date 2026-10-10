"""The phone's schedule payloads, as the server really sends them
(schedule audit 10/10/26 TRUST-1).

The server sends `narrative` as a list of bullets (labor.py's summary,
schedule_engine's per-part notes); iOS declared `let narrative: String?`
with a synthesized decoder, so every finished generation polled on the
phone failed to decode — the owner waited minutes and got an error instead
of the scorecard. The iOS test that should have caught it decoded a
hand-written fixture with a string in it.

The fixtures the iOS tests decode are now captured here, from a real
generation (the model's call stubbed with a JSON answer, everything after
it — the repair passes, the scorer, the history save, the poll's own
present/attach steps — running for real), polled through
/mobile/api/labor/schedule-status and reopened through
/mobile/api/labor/schedule-history/<id>. This test keeps them honest:

* the payload the phone polls has the shape the fixture has — every key,
  the kind of every value (list, object, string, number, bool), all the
  way down — so a server change that would break the phone's decode fails
  HERE, and the fixture is re-captured and decoded by
  ios/CavnarAI/CavnarAITests/ScheduleStatusContractTests.swift;
* `narrative` is a list of strings — the shape GeneratedSchedule now
  decodes (it also reads an older string);
* every key GeneratedSchedule declares that the server sends is in the
  fixture, so the Swift test decodes every field the phone reads.

Re-capture after an intended payload change:
    CAVNAR_WRITE_IOS_FIXTURES=1 python -m pytest -q tests/test_ios_schedule_payload_contract.py
"""
import datetime as dt
import json
import os
import re
import sys

import pytest

# Every module the generation imports lazily, imported before any fixture
# patches models.get_conn (tests/test_schedule_b2_calls.py).
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_versions, shift_requests, staff_schedule  # noqa: E401,F401
import staff_settings, strategy_jobs, time_off, labor_replacements  # noqa: E401,F401
import analyser, ask_cavnar, benchmark_registry, business_intelligence, clover, cogs  # noqa: E401,F401
import command_center, credentials, data_freshness, data_health, demand, food_cost_intelligence  # noqa: E401,F401
import forecast_log, home_brief, inventory, inventory_ledger, kitchen_stations, marketing  # noqa: E401,F401
import marketing_signals, morning_brief, ordering, owner_memory, permissions, pos, pos_health  # noqa: E401,F401
import rec_trust, reservation_feeds, review_intelligence, rpower, schedule_learning  # noqa: E401,F401
import schedule_optimizer, schedule_solver, square, staffing_curve, staffing_signals, toast  # noqa: E401,F401
import waste_trend, weather  # noqa: E401,F401
from flask import Flask

import auth
import labor
import mobile_api
import models
import ops
import schedule_engine as se
import schedule_prompt
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI")
FIXTURES = os.path.join(IOS, "CavnarAITests", "Fixtures")
STATUS_FIXTURE = os.path.join(FIXTURES, "schedule_status_done.json")
PENDING_FIXTURE = os.path.join(FIXTURES, "schedule_status_pending.json")
DETAIL_FIXTURE = os.path.join(FIXTURES, "schedule_history_detail.json")

# The Halloween week, so the payload carries a holiday (holiday_lift) and the
# clocks going back on Sunday 11/1/26 (a row's dst_hours).
MONDAY = dt.date(2026, 10, 26)
WEEK = [(MONDAY + dt.timedelta(days=i)).isoformat() for i in range(7)]
TEAM = [("Max", "General Manager"), ("Kim", "General Manager"), ("Ana", "Server"), ("Bo", "Server"),
        ("Cara", "Server"), ("Cy", "Line Cook"), ("Di", "Line Cook"), ("Ed", "Bartender")]
RATINGS = {"Ana": 5, "Bo": 2, "Cara": 4, "Cy": 4, "Di": 3, "Ed": 4, "Max": 5, "Kim": 4}
# What the model's answer says about the week — the bullets labor.py keeps
# as `narrative` (its summary, three at most).
BULLETS = ["Kept Saturday night heavy for the Halloween parties on the books.",
           "Ana and Bo split the weekday lunches.", "Ed closes the bar Friday and Saturday."]


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _shifts_history():
    """Four weeks of the team's own shifts before WEEK."""
    out = []
    for w in range(1, 5):
        monday = MONDAY - dt.timedelta(weeks=w)
        for i in range(7):
            d = (monday + dt.timedelta(days=i)).isoformat()
            for name, role in TEAM:
                out.append({"date": d, "employee": name, "role": role, "hours": 6.0})
    return out


def _answer(dates):
    """The model's JSON answer for `dates`: a manager open to close (Max
    the first four days, Kim the rest), a lunch and two dinner servers, a
    cook each half, the bartender at night — past 2am on Halloween, across
    the clock change."""
    days = []
    for d in dates:
        gm = "Max" if WEEK.index(d) < 4 else "Kim"
        late = d == "2026-10-31"
        days.append({"date": d, "shifts": [
            {"employee": gm, "role": "General Manager", "start": "10:00am", "end": "2:30am" if late else "10:30pm",
             "note": ""},
            {"employee": "Ana", "role": "Server", "start": "11:00am", "end": "4:00pm", "note": ""},
            {"employee": "Bo", "role": "Server", "start": "4:00pm", "end": "10:00pm", "note": ""},
            {"employee": "Cara", "role": "Server", "start": "4:00pm", "end": "10:00pm", "note": ""},
            {"employee": "Cy", "role": "Line Cook", "start": "10:00am", "end": "4:00pm", "note": ""},
            {"employee": "Di", "role": "Line Cook", "start": "4:00pm", "end": "10:00pm", "note": ""},
            {"employee": "Ed", "role": "Bartender", "start": "5:00pm", "end": "2:00am" if late else "11:00pm",
             "note": ""},
        ]})
    return {"days": days, "summary": BULLETS}


def _harness(monkeypatch, db):
    import time_utils
    rid = create_restaurant(Restaurant(name="Contract Grill", owner_email="owner@contract.test"), db_path=db)
    c = models.get_conn(db)
    c.execute("UPDATE restaurants SET module_labor=1, timezone='America/Chicago', hourly_rate=18 WHERE id=?", (rid,))
    c.commit()
    c.close()
    for name, role in TEAM:
        models.add_manual_team_member(rid, name, role=role, db_path=db)
    for name, score in RATINGS.items():
        models.set_capability(rid, name, "overall", score=score, db_path=db,
                              user={"id": 1, "role": "client", "username": "owner"})
    # A standing constraint (staff_constraints) and a request nobody has
    # answered yet (pending_time_off).
    models.save_staff_note(rid, "Cy", "No doubles on school nights", db_path=db,
                           today=MONDAY - dt.timedelta(days=5))
    row, err = time_off.request_time_off(rid, "Bo", WEEK[3], WEEK[3], db_path=db, today=dt.date(2026, 10, 1))
    assert not err, err
    history = _shifts_history()
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda r: history)
    # Sales that buy fewer hours than the draft writes: the budget trim runs.
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda r, **k: {"is_live": True, "blended_rate": 18.0, "overall_labor_pct": 27,
                                        "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                                        "period_days": 28, "total_sales": 60000})
    monkeypatch.setattr(labor, "build_demand_forecast", lambda r: {"ok": False})
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: dt.datetime(2026, 10, 22, 9, 0))
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda *a, **k: [])
    monkeypatch.setattr(se, "_week_monday", lambda today, ws=None, **_k: dt.datetime(2026, 10, 26))

    class _Msg:
        def __init__(self, text):
            self.text, self.stop_reason = text, "end_turn"

    def fake_create(client, **kw):
        dates = schedule_prompt.request_dates(kw["messages"][0]["content"])
        return _Msg(json.dumps(_answer(dates)))
    monkeypatch.setattr(labor, "create_with_retry", fake_create)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(labor, "extract_text", lambda m: m.text)
    monkeypatch.setattr(labor, "model_for", lambda k: "m")
    # The finished poll is the subject; the gate's rewrite of weak days is
    # another model call this harness does not answer.
    monkeypatch.setattr(se, "_quality_gate", lambda result: None)
    return rid


def _client(db, rid):
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    uid = auth.create_user(rid, "owner", "owner@contract.test", "Contract-pass-2026", db_path=db)
    return app.test_client(), {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}


def capture(monkeypatch, db):
    """{"pending", "done", "detail"}: a real generation polled while it runs
    and once it is done, then reopened, as the phone does."""
    rid = _harness(monkeypatch, db)
    client, headers = _client(db, rid)
    # A poll while one runs: the job's clock and the days drafted so far.
    ops.claim_async_job("contract-pending", "schedule", rid)
    with se.generation_scope("contract-pending") as clock:
        clock.plan(1)
        clock.plan_days(WEEK)
        clock.days_streamed(WEEK[:3])
        pending = client.get("/mobile/api/labor/schedule-status/contract-pending", headers=headers)
    assert pending.status_code == 200, pending.get_data(as_text=True)
    ops.finish_async_job("contract-pending", "error", {"ok": False, "error": "stopped for the test"})
    ops.start_async_job("contract-job", "schedule", rid)
    se._run_schedule_job("contract-job", rid)
    status = client.get("/mobile/api/labor/schedule-status/contract-job", headers=headers)
    assert status.status_code == 200, status.get_data(as_text=True)
    done = status.get_json()
    assert done["ok"] is True and done["status"] == "done", done.get("error")
    detail = client.get(f"/mobile/api/labor/schedule-history/{done['history_id']}", headers=headers)
    assert detail.status_code == 200, detail.get_data(as_text=True)
    return {"pending": pending.get_json(), "done": done, "detail": detail.get_json()}


# ── the shape of a JSON value ─────────────────────────────────────────────

def _kind(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "list"
    return "object"


def _merged(values):
    """One reference value for several fixture values of one kind: an
    object's keys from all of them (each key's own values merged), a list's
    elements from all of them, a number a float when any was one."""
    values = [v for v in values if v is not None]
    if not values:
        return None
    k = _kind(values[0])
    if k == "object":
        keys = {}
        for v in values:
            for key, val in v.items():
                keys.setdefault(key, []).append(val)
        return {key: _merged(vals) for key, vals in keys.items()}
    if k == "list":
        return [x for v in values for x in v]
    if k in ("int", "float"):
        return next((v for v in values if isinstance(v, float)), values[0])
    return values[0]


def shape_mismatches(fixture, live, path="$"):
    """Where `live` is a different KIND of value than `fixture` — what a
    Swift decoder trips on: a list where a string was (narrative), a string
    where an object was, a fractional number where the fixture only ever
    held whole ones (an Int on the phone). Null on either side matches
    anything (every field the phone reads is optional), and a key only one
    side has is not a mismatch (a new key is the phone's to decode or
    ignore; one no longer sent decodes as nil) — the declared keys are held
    by test_every_field_the_phone_reads_… below. A list's elements are each
    compared with the fixture's elements of that kind merged into one."""
    out = []
    fk, lk = _kind(fixture), _kind(live)
    if "null" in (fk, lk):
        return out
    if {fk, lk} <= {"int", "float"}:
        if fk == "int" and lk == "float" and live != int(live):
            out.append(f"{path}: the fixture holds whole numbers here, the server sent {live!r}")
        return out
    if fk != lk:
        return [f"{path}: the fixture has {fk}, the server sent {lk}"]
    if fk == "list":
        by_kind = {}
        for item in fixture:
            by_kind.setdefault(_kind(item), []).append(item)
        refs = {k: _merged(v) for k, v in by_kind.items() if k != "null"}
        for i, item in enumerate(live):
            k = _kind(item)
            if k == "null":
                continue
            ref = refs.get(k)
            if ref is None and k in ("int", "float"):
                ref = refs.get("float" if k == "int" else "int")
            if ref is None:
                if refs:
                    out.append(f"{path}[{i}]: a {k} where the fixture's list holds {sorted(refs)}")
                continue
            out.extend(shape_mismatches(ref, item, f"{path}[{i}]"))
        return out
    if fk == "object":
        for key in sorted(set(fixture) & set(live)):
            out.extend(shape_mismatches(fixture[key], live[key], f"{path}.{key}"))
    return out


def _swift_keys(struct):
    """The JSON keys a Swift struct's CodingKeys name (raw values, or the
    case name when it has none)."""
    src = open(os.path.join(IOS, "CavnarAI", "Features", "Labor", "LaborViewModel.swift"), encoding="utf-8").read()
    body = src[src.index(f"struct {struct}"):]
    body = body[body.index("enum CodingKeys"):]
    body = body[:body.index("\n    }\n")]
    keys = set()
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("case "):
            continue
        for part in line[5:].split(","):
            part = part.strip()
            m = re.match(r'(\w+)\s*=\s*"([^"]+)"', part)
            keys.add(m.group(2) if m else part)
    return keys


# Volatile values a re-capture always changes: kept out of the files so a
# re-capture is a reviewable diff, not noise.
_VOLATILE = re.compile(r'("(?:created_at|updated_at|generated_at|edited_at|computed_at|as_of|saved_at|'
                       r'capability_version|generation_id|run_id|correlation_id|job_id)"\s*:\s*)"[^"]*"')


def _write(path, payload):
    text = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False)
    text = _VOLATILE.sub(r'\1"<volatile>"', text)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")


FIXTURE_FILES = {"pending": PENDING_FIXTURE, "done": STATUS_FIXTURE, "detail": DETAIL_FIXTURE}


def _fixtures():
    return {k: json.load(open(p, encoding="utf-8")) for k, p in FIXTURE_FILES.items()}


def test_the_phone_payloads_match_the_fixtures_the_ios_tests_decode(db, monkeypatch):
    live = capture(monkeypatch, db)
    if os.environ.get("CAVNAR_WRITE_IOS_FIXTURES") == "1":
        for k, p in FIXTURE_FILES.items():
            _write(p, live[k])
    fixtures = _fixtures()
    for k in FIXTURE_FILES:
        assert shape_mismatches(fixtures[k], live[k]) == [], \
            f"{k}: the server's payload changed shape — re-capture (CAVNAR_WRITE_IOS_FIXTURES=1) and check " \
            "ScheduleStatusContractTests.swift still decodes it"


def test_narrative_is_a_list_of_bullets_on_the_poll_and_in_the_fixture(db, monkeypatch):
    live = capture(monkeypatch, db)
    assert live["done"]["narrative"] == BULLETS
    fixture = _fixtures()["done"]
    assert isinstance(fixture["narrative"], list) and fixture["narrative"]
    assert all(isinstance(b, str) for b in fixture["narrative"])


def test_every_field_the_phone_reads_and_the_server_sends_is_in_the_fixture(db, monkeypatch):
    live = capture(monkeypatch, db)
    declared = _swift_keys("GeneratedSchedule")
    fixtures = _fixtures()
    for k in FIXTURE_FILES:
        assert (declared & set(live[k])) - set(fixtures[k]) == set(), k
    # The captured week carries what a real week does: the bullets, the rows
    # (one across the clock change), the review, the score, a holiday, a
    # staff constraint, a pending request, the trim; a pending poll its clock
    # and its days.
    done = fixtures["done"]
    assert {"narrative", "preview_rows", "review", "quality", "rule_violations"} <= set(done)
    assert any(r.get("dst_hours") for r in done["preview_rows"])
    assert done["holiday_lift"] and done["staff_constraints"] and done["pending_time_off"]
    assert fixtures["pending"]["status"] == "pending" and fixtures["pending"]["progress"]["days_drafted"] == 3
    assert isinstance(fixtures["pending"]["seconds_left"], int)
    assert fixtures["detail"]["preview_rows"]


def test_the_ios_contract_test_decodes_every_captured_fixture():
    src = open(os.path.join(IOS, "CavnarAITests", "ScheduleStatusContractTests.swift"), encoding="utf-8").read()
    for name in ("schedule_status_done", "schedule_status_pending", "schedule_history_detail"):
        assert f'"{name}"' in src, name
    # Nothing it reads was set to nil on the real payloads.
    assert "undecodedKeys" in src
    old = open(os.path.join(IOS, "CavnarAITests", "ScheduleReviewDecodingTests.swift"), encoding="utf-8").read()
    # The hand-written fixture said narrative was a string; the server never did.
    assert '"narrative": "Kept Saturday' not in old


def test_the_shape_check_catches_the_bug_it_exists_for():
    """A list where the fixture had a string — TRUST-1 itself — and an Int
    the server started sending fractional are mismatches; a new key, a
    missing key and a null are not."""
    assert shape_mismatches({"narrative": "a"}, {"narrative": ["a"]}) == [
        "$.narrative: the fixture has string, the server sent list"]
    assert shape_mismatches({"calls": 1}, {"calls": 1.5}) != []
    assert shape_mismatches({"calls": 1}, {"calls": 2.0}) == []
    assert shape_mismatches({"a": [{"x": 1}, {"y": "s"}]}, {"a": [{"x": 2, "y": "t"}], "b": 1}) == []
    assert shape_mismatches({"a": [{"y": "s"}]}, {"a": [{"y": 3}]}) == ["$.a[0].y: the fixture has string, the server sent int"]
    assert shape_mismatches({"a": None}, {"a": {"k": 1}}) == []
