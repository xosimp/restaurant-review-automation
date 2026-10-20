"""Scale audit 10/19/26, fix round F5 — AI infrastructure, models and costs.

A4-01 / SEC-06  the interactive lane (ai_lane, Ask polled off the request
                threads) and fair interactive slots per restaurant
A4-02           the gateway per model family, cached prefixes and tools in
                the estimate, a 429 never a breaker failure, retry-after +
                jitter
A4-04 / A4-05   the Places and trial pools grow per client, no absolute
                Places maximum
A4-08           Ask's snapshot breakpoint only where a read is likely; no
                write on the last round
A4-10           the percent canary cohort
A4-11           invoices on Opus 5.5, read at call time
A4-12           a fleet month's spend from the daily rollup
A4-13 / A4-15   competitor check cadence, visibility spread (config)
A4-14           the trace text budget
FE-08           the Food Cost read stale-while-refresh
A5-F8 / A5-F9   the stream cap on model-phase rows; the T3 run cap

Nothing here reaches a provider, the scheduler thread or production.
"""
import json
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import pytest
from flask import Flask

import ai_lane
import ai_utils
import ai_workflows
import auth
import client_api
import model_gateway as mg
import models
from client_api import client_bp
from models import Restaurant, create_restaurant

NS = types.SimpleNamespace


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if mod is not None and getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", conn)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for k in ("GATEWAY_ORG_OTPM", "GATEWAY_ORG_ITPM", "GATEWAY_TENANT_OTPM", "GATEWAY_FAMILY_LIMITS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    ai_utils.reset_process_state(db_path)
    ai_utils.invalidate_budget_memo()
    yield db_path
    ai_utils.reset_process_state(db_path)
    ai_utils.invalidate_budget_memo()


def _rest(db_path, **kw):
    kw.setdefault("name", "F5 Bistro")
    kw.setdefault("owner_email", f"o{time.time_ns()}@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


# ── A4-02: the gateway per model family ────────────────────────────────────

def test_a_models_family_is_its_rate_limit_bucket():
    assert mg.model_family("claude-sonnet-5-5") == "claude-sonnet-5-5"
    assert mg.model_family("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert mg.model_family("claude-opus-4-7") == mg.model_family("claude-opus-4-8") == "claude-opus-4"


def test_one_familys_spent_minute_never_holds_another_family(monkeypatch):
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "10000")
    kw = lambda model, n: {"model": model, "max_tokens": n, "messages": [{"role": "user", "content": "x"}]}  # noqa
    assert mg.admit(1, kw("claude-opus-5-5", 5000), klass="bulk", sleep=lambda s: None) is not None
    with pytest.raises(mg.GatewayBusy):
        mg.admit(1, kw("claude-opus-5-5", 4000), klass="bulk", sleep=lambda s: None)
    # Sonnet's minute is its own.
    t = mg.admit(1, kw("claude-sonnet-5-5", 4000), klass="bulk", sleep=lambda s: None)
    assert t is not None and t.family == "claude-sonnet-5-5"
    fams = mg.usage_now()["families"]
    assert fams["claude-opus-5-5"]["bulk"]["output"] == 5000
    assert fams["claude-sonnet-5-5"]["bulk"]["output"] == 4000


def test_a_family_can_carry_its_own_limits(monkeypatch):
    monkeypatch.setenv("GATEWAY_FAMILY_LIMITS", json.dumps({"claude-haiku-5-5": {"otpm": 500}}))
    assert mg.enabled()
    assert mg.org_limits("claude-haiku-5-5")["otpm"] == 500 and mg.org_limits("claude-sonnet-5-5")["otpm"] == 0


def test_the_estimate_counts_tools_and_leaves_out_the_cached_prefix():
    tools = [{"name": "t", "description": "d" * 4000, "input_schema": {}}]
    sys_ = [{"type": "text", "text": "s" * 8000, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "turn " * 100}]
    msgs = [{"role": "user", "content": "q" * 400}]
    cached_in, _ = mg.estimate({"tools": tools, "system": sys_, "messages": msgs, "max_tokens": 10})
    # Only what follows the last breakpoint: the turn block and the question.
    assert cached_in == int((500 + 400) / 4) + 1
    plain_sys = [dict(b, cache_control=None) for b in sys_]
    full_in, _ = mg.estimate({"tools": tools, "system": plain_sys, "messages": msgs, "max_tokens": 10})
    assert full_in > 3000, "uncached, the tools and the system prompt are counted"


# ── A4-02: a 429 is a throttle, never a breaker failure ────────────────────

def _rate_limit_error(retry_after=None):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return anthropic.RateLimitError("rate limited", response=httpx.Response(429, request=req, headers=headers),
                                    body=None)


class _Failing:
    def __init__(self, exc, failures=99):
        self.messages, self.exc, self.failures, self.calls = self, exc, failures, 0

    def create(self, **kw):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn", model="m", id="msg",
                  usage=NS(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0))


def test_exhausted_rate_limits_never_open_the_breaker(monkeypatch):
    monkeypatch.setattr(ai_utils.time, "sleep", lambda s: None)
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD + 2):
        with pytest.raises(anthropic.RateLimitError):
            ai_utils.create_with_retry(_Failing(_rate_limit_error()), retries=1, model="m", max_tokens=5)
    assert ai_utils.breaker_state("anthropic")[0] == "closed"
    # A server error still counts.
    err = anthropic.InternalServerError("boom", response=httpx.Response(
        500, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        with pytest.raises(anthropic.InternalServerError):
            ai_utils.create_with_retry(_Failing(err), retries=0, model="m", max_tokens=5)
    assert ai_utils.breaker_state("anthropic")[0] == "open"
    ai_utils.reset_breaker()


def test_a_retry_waits_the_providers_retry_after_with_jitter(monkeypatch):
    naps = []
    monkeypatch.setattr(ai_utils.time, "sleep", naps.append)
    client = _Failing(_rate_limit_error(retry_after=4), failures=1)
    ai_utils.create_with_retry(client, retries=2, model="m", max_tokens=5)
    assert client.calls == 2 and len(naps) == 1 and 4.0 <= naps[0] <= 5.0


def test_a_long_retry_after_is_not_waited_out_on_an_owners_request(monkeypatch):
    assert ai_utils._retry_delay(_rate_limit_error(retry_after=20), 1, 1.5, interactive=True) is None
    assert ai_utils._retry_delay(_rate_limit_error(retry_after=20), 1, 1.5, interactive=False) >= 20
    steps = [ai_utils._retry_delay(RuntimeError("x"), 2, 1.5) for _ in range(50)]
    assert all(0 < s <= 2.25 for s in steps) and len(set(steps)) > 1, "full jitter"


def test_a_rate_limit_reads_as_busy_to_the_owner():
    msg, status = ai_utils.user_facing_error(_rate_limit_error())
    assert status == 503 and "busy" in msg


# ── A4-04 / A4-05: the pools grow per client ───────────────────────────────

def test_the_places_pool_has_no_absolute_maximum_and_counts_trials(db_path, monkeypatch):
    assert ai_utils.AI_PLACES_GLOBAL_MAX_MONTHLY_USD == 0
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 900)
    monkeypatch.setattr(ai_utils, "_trial_client_count", lambda db_path=None: 100)
    assert ai_utils.places_global_monthly_budget() == 1000 * ai_utils.AI_PLACES_GLOBAL_PER_CLIENT_USD
    monkeypatch.setattr(ai_utils, "AI_PLACES_GLOBAL_MAX_MONTHLY_USD", 3000.0)
    assert ai_utils.places_global_monthly_budget() == 3000.0, "a maximum set on purpose still binds"


def test_the_trial_pool_grows_per_trial_up_to_its_backstop(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "_trial_client_count", lambda db_path=None: 1)
    assert ai_utils.trial_pool_budgets() == (ai_utils.AI_TRIAL_POOL_DAILY_USD, ai_utils.AI_TRIAL_POOL_MONTHLY_USD)
    monkeypatch.setattr(ai_utils, "_trial_client_count", lambda db_path=None: 100)
    day, month = ai_utils.trial_pool_budgets()
    assert month == 100 * ai_utils.AI_TRIAL_POOL_PER_TRIAL_MONTHLY_USD and day == 250.0
    monkeypatch.setattr(ai_utils, "_trial_client_count", lambda db_path=None: 100000)
    assert ai_utils.trial_pool_budgets() == (ai_utils.AI_TRIAL_POOL_MAX_DAILY_USD,
                                             ai_utils.AI_TRIAL_POOL_MAX_MONTHLY_USD)


def test_the_trial_count_leaves_out_demos(db_path):
    _rest(db_path, billing_status="trial")
    _rest(db_path, billing_status="trial", is_demo=1)
    _rest(db_path, billing_status="active")
    ai_utils.invalidate_budget_memo()
    assert ai_utils._trial_client_count() == 1


# ── A4-12: a fleet month from the rollup ───────────────────────────────────

def _usage_on(db_path, rid, day, usd, trigger=None):
    rowid = ai_utils.log_ai_usage(rid, "f5_spend", "claude-sonnet-5-5", 0, 0, cost_usd=usd, trigger=trigger)
    c = models.get_conn(db_path)
    try:
        c.execute("UPDATE ai_usage SET created_at=? WHERE id=?", (f"{day} 12:00:00", rowid))
        c.commit()
    finally:
        c.close()


def test_a_fleet_month_reads_the_rollup_and_the_raw_rows_after_it(db_path, monkeypatch):
    now = datetime.now(timezone.utc)
    if now.day < 3:
        pytest.skip("needs two complete days of this month before today")
    paid = _rest(db_path, billing_status="active")
    trial = _rest(db_path, billing_status="trial")
    first = now.strftime("%Y-%m-01")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    _usage_on(db_path, paid, first, 1.0)
    _usage_on(db_path, trial, first, 0.5)
    _usage_on(db_path, trial, first, 0.25, trigger="admin")
    _usage_on(db_path, None, yday, 2.0)
    month = now.strftime("%Y-%m-01 00:00:00")
    raw_paid = ai_utils._spend_since(month, paid_only=True, _rollup=False)
    raw_trials = ai_utils._spend_since(month, trials_only=True, _rollup=False)
    ai_utils.rollup_usage()
    # Rolled after the days ended: complete.
    assert ai_utils._spend_from_rollup(month, db_path, paid_only=True) is not None
    _usage_on(db_path, paid, today, 4.0)
    assert ai_utils._spend_since(month, paid_only=True) == pytest.approx(raw_paid + 4.0)
    assert ai_utils._spend_since(month, trials_only=True) == pytest.approx(raw_trials)
    assert raw_paid == pytest.approx(1.0 + 0.25 + 2.0), "the paying pool counts an admin's run on a trial"
    assert raw_trials == pytest.approx(0.5)


def test_a_month_with_no_complete_rollup_day_sums_the_ledger(db_path):
    assert ai_utils._spend_from_rollup(datetime.now(timezone.utc).strftime("%Y-%m-01 00:00:00"), db_path) is None


# ── SEC-06: fair interactive slots ─────────────────────────────────────────

def test_a_lone_restaurant_may_use_every_slot():
    s = ai_utils.FairSlots(2)
    assert s.acquire(timeout=0, tenant=1) and s.acquire(timeout=0, tenant=1)
    assert s.acquire(timeout=0, tenant=2) is False
    s.release(tenant=1)
    s.release(tenant=1)
    assert s._value == 2


def test_a_freed_slot_goes_to_the_waiting_restaurant_holding_fewest():
    s = ai_utils.FairSlots(2)
    assert s.acquire(timeout=0, tenant="A") and s.acquire(timeout=0, tenant="A")
    got = []

    def wait(t):
        if s.acquire(timeout=2, tenant=t):
            got.append(t)
    a2 = threading.Thread(target=wait, args=("A",))
    a2.start()
    time.sleep(0.05)
    b = threading.Thread(target=wait, args=("B",))
    b.start()
    time.sleep(0.05)
    s.release(tenant="A")            # A asked first, but holds one already: B is served
    b.join(1)
    assert got == ["B"]
    s.release(tenant="A")
    a2.join(1)
    assert got == ["B", "A"]
    assert s.held_by() == {"A": 1, "B": 1}


def test_a_per_restaurant_ceiling_when_set():
    s = ai_utils.FairSlots(3, per_tenant_max=1)
    assert s.acquire(timeout=0, tenant=1)
    assert s.acquire(timeout=0, tenant=1) is False
    assert s.acquire(timeout=0, tenant=2)


# ── A4-01: Ask on the interactive lane ─────────────────────────────────────

def _app():
    import mobile_api
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _poll(c, url, limit=5.0):
    events, after, deadline = [], 0, time.time() + limit
    while time.time() < deadline:
        r = c.get(f"{url}?after={after}", headers={"Authorization": "Bearer t"})
        d = r.get_json()
        assert r.status_code == 200 and d["ok"]
        events += d["events"]
        after = d["next"]
        if d["done"]:
            return events
        time.sleep(0.05)
    raise AssertionError(f"the turn did not finish: {events}")


def test_a_polled_ask_turn_runs_off_the_request_and_reads_back_its_events(db_path, monkeypatch):
    import ask_cavnar
    rid = _rest(db_path)
    owner = {"id": 7, "restaurant_id": rid, "role": "client", "is_admin": 0, "username": "o", "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: owner)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: owner, raising=False)
    seen = {}

    def fake_ask(restaurant, question, **kw):
        seen["on_request"] = ai_utils.on_request_thread()
        seen["slots_free"] = ai_utils.interactive_slots_free()
        kw["on_progress"]("Reading your reviews", "solving")
        return "Reviews look steady.", False, [], {}
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", fake_ask)
    c = _app().test_client()
    for start, turn in (("/api/ask-cavnar/stream", "/api/ask-cavnar/turn/"),
                        ("/mobile/api/ask-cavnar/stream", "/mobile/api/ask-cavnar/turn/")):
        r = c.post(start, json={"question": "reviews?", "poll": True}, headers={"Authorization": "Bearer t"})
        d = r.get_json()
        assert r.status_code == 200 and d["ok"] and d["turn_id"]
        events = _poll(c, turn + d["turn_id"])
        kinds = [e["type"] for e in events]
        assert kinds[0] == "progress" and kinds[-1] == "answer"
        assert events[-1]["answer"].startswith("Reviews look steady")
    assert seen["on_request"] is False, "the model work never runs on a request thread"
    assert seen["slots_free"] == ai_utils.INTERACTIVE_AI_SLOTS, "and takes no interactive slot"
    # Without `poll` the stream is exactly as before.
    raw = c.post("/api/ask-cavnar/stream", json={"question": "reviews?"}).get_data(as_text=True)
    assert "data: " in raw and '"type": "answer"' in raw


def test_another_login_cannot_read_a_turn(db_path):
    turn = ai_lane.start(1, 7, lambda t: t.put({"type": "answer", "answer": "x"}))
    deadline = time.time() + 2
    while not turn.done and time.time() < deadline:
        time.sleep(0.01)
    assert ai_lane.read(turn.id, 1, 7)[2] is True
    assert ai_lane.read(turn.id, 1, 8) is None and ai_lane.read(turn.id, 2, 7) is None
    payload, status = client_api._do_ask_turn_events(2, 7, turn.id, 0)
    assert status == 404 and not payload["ok"]


def test_the_lane_is_fair_and_bounded(monkeypatch):
    lane = ai_lane._Lane(workers=1, per_tenant=1, queue_max=3)
    gate = threading.Event()
    order = []

    def job(name, wait=False):
        def run(turn):
            if wait:
                gate.wait(2)
            order.append(name)
        return run
    t1 = ai_lane.Turn(1, 1)
    lane.submit(1, t1, job("A1", wait=True))
    time.sleep(0.05)
    lane.submit(1, ai_lane.Turn(1, 1), job("A2"))
    lane.submit(2, ai_lane.Turn(2, 1), job("B1"))
    lane.submit(3, ai_lane.Turn(3, 1), job("C1"))
    with pytest.raises(ai_lane.LaneBusy):
        lane.submit(4, ai_lane.Turn(4, 1), job("D1"))
    gate.set()
    deadline = time.time() + 3
    while len(order) < 4 and time.time() < deadline:
        time.sleep(0.01)
    # Restaurant 1 asked twice first; the restaurants not yet served go ahead
    # of its second turn.
    assert order[0] == "A1" and order.index("A2") > order.index("B1")


def test_a_turn_left_waiting_too_long_is_answered_busy(monkeypatch):
    monkeypatch.setattr(ai_lane, "LANE_QUEUE_WAIT_SECONDS", 0.0)
    lane = ai_lane._Lane(workers=1, per_tenant=1, queue_max=5)
    turn = ai_lane.Turn(1, 1)
    lane._pending.append([1, turn, lambda t: None, 1, time.time() - 1])
    lane.sweep()
    events, _n, done = turn.read(0)
    assert done and events[0]["type"] == "error" and events[0]["error"] == ai_lane.BUSY_MESSAGE


# ── FE-08: the Food Cost read off the request ──────────────────────────────

def test_the_food_cost_read_is_written_off_the_request(db_path, monkeypatch):
    import inventory
    rid = _rest(db_path)
    owner = {"id": 7, "restaurant_id": rid, "role": "client", "is_admin": 0, "username": "o", "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: owner)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: owner, raising=False)
    monkeypatch.setattr(inventory, "analysis_for", lambda rid_, **k: ([], True, {}))
    monkeypatch.setattr(client_api, "insight_rec_items", lambda *a, **k: [])
    monkeypatch.setattr(client_api, "food_read_evidence", lambda *a, **k: None)
    stored = {"text": None}
    on_request = []

    def food_text(rid_, restaurant, items, is_live, analysis):
        if stored["text"] is None:
            ai_utils.refuse_if_stored_only("inventory_insight")     # a model call
            on_request.append(ai_utils.on_request_thread())
            stored["text"] = "Food cost held at 31%."
        return stored["text"]
    monkeypatch.setattr(client_api, "food_insight_text", food_text)
    c = _app().test_client()
    d = c.get("/api/inv-insight?async=1").get_json()
    assert d.get("refresh_job") and d.get("pending"), d
    deadline = time.time() + 5
    while time.time() < deadline:
        d = c.get(f"/api/inv-insight?async=1&refresh_job={d['refresh_job']}").get_json()
        if not d.get("refresh_job"):
            break
        time.sleep(0.05)
    assert "Food cost held at 31%" in d["insight"]
    assert on_request == [False], "the model call ran on the owner AI pool, never on a request thread"
    # Without `async` (the iOS builds), the synchronous read as before.
    assert "Food cost held at 31%" in c.get("/api/inv-insight").get_json()["insight"]


# ── A4-08: Ask's cache breakpoints ─────────────────────────────────────────

def test_the_last_round_reads_its_prefix_but_writes_none():
    import ask_cavnar
    msgs = [{"role": "user", "content": "q"}, {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "r"}]}]
    every = ask_cavnar._cached_messages(msgs, 0)
    last = ask_cavnar._cached_messages(msgs, 0, last=True)
    assert ask_cavnar.cache_breakpoints(messages=every) == 2
    assert ask_cavnar.cache_breakpoints(messages=last) == 1
    assert "cache_control" not in str(last[2]) and "cache_control" in str(last[0])


def test_a_cold_snapshot_keeps_only_the_shared_static_breakpoint():
    import ask_cavnar
    blocks = ask_cavnar._system_blocks("R", "snapshot", "standard")
    cold = ask_cavnar._cold_snapshot(blocks)
    assert blocks[1].get("cache_control") and not cold[1].get("cache_control")
    assert cold[0].get("cache_control"), "the static prefix every restaurant shares stays cached"
    assert not ask_cavnar._snapshot_recently_cached(99, "s")
    ask_cavnar._note_snapshot_cached(99, "s")
    assert ask_cavnar._snapshot_recently_cached(99, "s") and not ask_cavnar._snapshot_recently_cached(99, "t")


# ── A4-10: the percent canary ──────────────────────────────────────────────

def test_the_canary_can_be_a_share_of_real_restaurants(monkeypatch):
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "4")
    monkeypatch.delenv("AI_CANARY_PERCENT", raising=False)
    assert ai_workflows._in_canary(4) and not any(ai_workflows._in_canary(i) for i in range(5, 200))
    monkeypatch.setenv("AI_CANARY_PERCENT", "10")
    share = sum(ai_workflows._in_canary(i) for i in range(1, 2001)) / 2000.0
    assert 0.07 < share < 0.13
    first = [ai_workflows._in_canary(i) for i in range(1, 200)]
    assert first == [ai_workflows._in_canary(i) for i in range(1, 200)], "stable"
    pol = ai_workflows.POLICIES["labor_insight"]
    member = next(i for i in range(5, 2000) if ai_workflows._in_canary(i))
    assert ai_workflows.canary_start(pol, member)[1] is True


# ── A4-11: invoices ────────────────────────────────────────────────────────

def test_invoices_default_to_opus_55_read_when_the_scan_runs(monkeypatch):
    import invoices
    monkeypatch.delenv("INVOICE_MODEL", raising=False)
    assert invoices.current_model() == ai_utils.OPUS_55
    monkeypatch.setenv("INVOICE_MODEL", "claude-opus-5")
    assert invoices.current_model() == "claude-opus-5", "an override needs no restart"


# ── A4-14: the trace text budget ───────────────────────────────────────────

def test_the_oldest_trace_text_goes_first_past_the_budget(db_path, monkeypatch):
    c = models.get_conn(db_path)
    try:
        for i in range(5):
            c.execute("INSERT INTO ai_calls (call_id, created_at, prompt_z, output_z) VALUES (?,?,?,?)",
                      (f"c{i}", f"2026-10-0{i + 1} 00:00:00", b"x" * 600_000, b"y" * 400_000))
        c.commit()
    finally:
        c.close()
    monkeypatch.setattr(ai_utils, "AI_TRACE_TEXT_MAX_MB", 2.5)
    ai_utils.prune_ai_ops(db_path)
    c = models.get_conn(db_path)
    try:
        kept = [r["call_id"] for r in c.execute("SELECT call_id FROM ai_calls WHERE prompt_z IS NOT NULL "
                                                "ORDER BY call_id").fetchall()]
    finally:
        c.close()
    assert kept == ["c3", "c4"]


# ── A5-F8: the stream cap holds model-phase rows only ──────────────────────

def test_a_generation_waits_for_a_streaming_place_not_a_cpu_slot(monkeypatch):
    import job_queue
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "9000")        # (0.5 + 0.3) x 9,000 / 7,200 = 1 stream
    assert mg.max_concurrent_generations() == 1
    streaming = {"n": 1}
    monkeypatch.setattr(job_queue, "rows_in_phase", lambda grp, phase, exclude_id=None, db_path=None: streaming["n"])
    naps = []
    token = job_queue._CURRENT.set(job_queue.Job(id=5, grp="generation", priority_class="scheduled"))
    try:
        def nap(s):
            naps.append(s)
            streaming["n"] = 0
        assert mg._stream_slot_wait(10, nap) > 0 and naps
        naps.clear()
        assert mg._stream_slot_wait(10, nap) == 0 and not naps
    finally:
        job_queue._CURRENT.reset(token)
    # Outside a generation row: nothing waits.
    assert mg._stream_slot_wait(10, nap) == 0


# ── A5-F9: the T3 run cap ──────────────────────────────────────────────────

def test_a_run_on_t3_stops_at_its_own_cap_and_t4_keeps_the_policys(monkeypatch):
    import schedule_engine as se
    caps = ai_workflows.POLICIES["labor_schedule"].caps
    monkeypatch.setattr(se, "schedule_route", lambda: ai_workflows.Route("T3", "claude-sonnet-5-5", "medium"))
    assert se.run_cap_usd(caps) == min(caps.usd, se.SCHEDULE_T3_RUN_CAP_USD) == 1.5
    monkeypatch.setattr(se, "schedule_route", lambda: ai_workflows.Route("T4", "claude-opus-5-5", "medium"))
    assert se.run_cap_usd(caps) == caps.usd


# ── A4-13 / A4-15: cadence config ──────────────────────────────────────────

def test_the_competitor_check_waits_its_days_when_set(monkeypatch):
    import competitor
    r = NS(competitor_updated_at=None, timezone="America/Chicago")
    today = datetime.now().strftime("%Y-%m-%d")
    monkeypatch.setattr(competitor, "COMPETITOR_CHECK_EVERY_DAYS", 3)
    assert competitor._check_due(r, {}) is True
    assert competitor._check_due(r, {"ratings_checked_at": today}) is False
    old = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d")
    assert competitor._check_due(r, {"ratings_checked_at": old}) is True


def test_the_visibility_spread_is_off_by_default_and_by_id_when_set(monkeypatch):
    import scheduler
    assert scheduler.AI_VISIBILITY_SPREAD_DAYS == 1 and scheduler._visibility_due_today(6)
    monkeypatch.setattr(scheduler, "AI_VISIBILITY_SPREAD_DAYS", 3)
    monday = datetime(2026, 10, 19, 8, 0)
    assert scheduler._visibility_due_today(3, now=monday) and not scheduler._visibility_due_today(5, now=monday)
    assert scheduler._visibility_due_today(5, now=monday + timedelta(days=2))
