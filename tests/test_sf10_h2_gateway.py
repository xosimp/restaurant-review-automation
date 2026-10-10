"""Scheduler audit 10/10/26, workstream H2 — the model gateway (SCALE-32):
classes, the organisation's tokens a minute by class, a tenant's tokens a
minute, generation concurrency from the token budget, per-restaurant dollar
ceilings, and breakers shared across processes (SCALE-24)."""
import sys
import time
from types import SimpleNamespace

import pytest

import models
from models import Restaurant, create_restaurant


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
    for k in ("GATEWAY_ORG_OTPM", "GATEWAY_ORG_ITPM", "GATEWAY_TENANT_OTPM"):
        monkeypatch.delenv(k, raising=False)
    import ai_utils
    ai_utils._breaker_synced.clear()
    ai_utils._breakers.clear()
    yield db_path
    ai_utils._breakers.clear()
    ai_utils._breaker_synced.clear()


def _kw(chars=400, max_tokens=4000):
    return {"model": "m", "max_tokens": max_tokens, "messages": [{"role": "user", "content": "x" * chars}]}


def test_the_allocator_is_off_until_a_budget_is_set(db):
    import model_gateway as mg
    assert not mg.enabled() and mg.admit(1, _kw()) is None
    assert mg.max_concurrent_generations() is None


def test_each_class_keeps_its_share_and_bulk_never_takes_the_interactive_reserve(db, monkeypatch):
    import model_gateway as mg
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "10000")
    naps = []
    # Bulk may use its own 20% and the scheduled class's unused 30% — never
    # the interactive 50%: 5,000 tokens at most this minute.
    t = mg.admit(1, _kw(max_tokens=4000), klass="bulk", sleep=naps.append)
    assert t is not None and t.est_out == 4000
    with pytest.raises(mg.GatewayBusy):
        mg.admit(1, _kw(max_tokens=4000), klass="bulk", sleep=naps.append)
    assert sum(naps) >= mg.WAIT_SECONDS["bulk"]
    # An owner's call still has its whole reserve.
    assert mg.admit(2, _kw(max_tokens=5000), klass="interactive", sleep=naps.append).est_out == 5000


def test_an_owner_over_the_allowance_waits_briefly_then_goes(db, monkeypatch):
    import model_gateway as mg
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "1000")
    naps = []
    mg.admit(1, _kw(max_tokens=1000), klass="interactive", sleep=naps.append)
    t = mg.admit(1, _kw(max_tokens=1000), klass="interactive", sleep=naps.append)
    assert t is not None and t.est_out == 0 and t.waited >= mg.WAIT_SECONDS["interactive"]


def test_settle_replaces_the_estimate_with_real_usage(db, monkeypatch):
    import model_gateway as mg
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "100000")
    t = mg.admit(1, _kw(max_tokens=8000), klass="scheduled")
    msg = SimpleNamespace(usage=SimpleNamespace(input_tokens=50, output_tokens=1200, cache_creation_input_tokens=0))
    mg.settle(t, msg)
    mg.settle(t, msg)                                     # once only
    row = mg.usage_now()["classes"]["scheduled"]
    assert row["output"] == 1200 and row["input"] == 50
    t2 = mg.admit(1, _kw(max_tokens=8000), klass="scheduled")
    mg.settle(t2, None)                                   # failed: the reservation is given back
    assert mg.usage_now()["classes"]["scheduled"]["output"] == 1200


def test_one_restaurant_cannot_take_the_organisations_minute(db, monkeypatch):
    import model_gateway as mg
    monkeypatch.setenv("GATEWAY_TENANT_OTPM", "5000")
    mg.admit(7, _kw(max_tokens=4000), klass="bulk", sleep=lambda s: None)
    with pytest.raises(mg.GatewayBusy):
        mg.admit(7, _kw(max_tokens=4000), klass="bulk", sleep=lambda s: None)
    assert mg.admit(8, _kw(max_tokens=4000), klass="bulk", sleep=lambda s: None) is not None


def test_generation_concurrency_follows_the_token_budget(db, monkeypatch):
    import model_gateway as mg
    import job_queue
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "50")
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "72000")
    # (0.5 + 0.3) x 72,000 / 7,200 a stream = 8 at once.
    assert mg.max_concurrent_generations() == 8
    assert job_queue.group_concurrency("generation") == 8


def test_a_calls_class_is_its_jobs_or_who_asked(db):
    import model_gateway as mg
    import ai_utils
    import job_queue
    with ai_utils.ai_context(trigger="scheduler"):
        assert mg.call_class() == "bulk"
    with ai_utils.ai_context(trigger="owner"):
        assert mg.call_class() == "interactive"
    token = job_queue._CURRENT.set(job_queue.Job(id=1, priority_class="scheduled"))
    try:
        assert mg.call_class() == "scheduled"
        with mg.call_class_scope("bulk"):
            assert mg.call_class() == "bulk"
    finally:
        job_queue._CURRENT.reset(token)
    assert mg.should_batch("labor_schedule", klass="interactive") is False


class _Client:
    def __init__(self):
        self.messages = self
        self.sent = 0

    def create(self, **kw):
        self.sent += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=20,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=0),
                               model="m", id="msg_1")


def test_create_with_retry_passes_the_gateway(db, monkeypatch):
    import ai_utils
    import model_gateway as mg
    import data_health
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "100000")
    client = _Client()
    with mg.call_class_scope("bulk"):
        ai_utils.create_with_retry(client, model="m", max_tokens=500, readiness=data_health.NOT_APPLICABLE,
                                   messages=[{"role": "user", "content": "hi"}])
    assert client.sent == 1 and mg.usage_now()["classes"]["bulk"]["output"] == 20
    # Spent: a bulk call is refused as busy, before anything is sent.
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "10")
    monkeypatch.setitem(mg.WAIT_SECONDS, "bulk", 0)
    with mg.call_class_scope("bulk"), pytest.raises(ai_utils.AIBusy):
        ai_utils.create_with_retry(client, model="m", max_tokens=500, readiness=data_health.NOT_APPLICABLE,
                                   messages=[{"role": "user", "content": "hi"}])
    assert client.sent == 1


def test_one_restaurants_ceiling_can_be_raised_alone(db):
    import ai_utils
    import model_gateway as mg
    rid = create_restaurant(Restaurant(name="Big Roster", owner_email="b@x.com"), db_path=db)
    other = create_restaurant(Restaurant(name="Small Roster", owner_email="s@x.com"), db_path=db)
    base = ai_utils.ai_budget_status(rid)["day"]["budget"]
    mg.set_tenant_budget(rid, daily_usd=60.0, monthly_usd=600.0, reason="250-person roster", set_by="will")
    st = ai_utils.ai_budget_status(rid)
    assert st["day"]["budget"] == 60.0 and st["month"]["budget"] == 600.0
    assert ai_utils.ai_budget_status(other)["day"]["budget"] == base
    mg.set_tenant_budget(rid)                               # removed: the tier's again
    assert ai_utils.ai_budget_status(rid)["day"]["budget"] == base


def test_a_breaker_opened_in_one_process_opens_in_every_process(db):
    import ai_utils
    ai_utils.trip_breaker("anthropic", "auth")               # process A
    assert ai_utils.breaker_state("anthropic")[0] == "open"
    # Process B: its own memory knows nothing yet.
    ai_utils._breakers.clear()
    ai_utils._breaker_synced.clear()
    assert ai_utils.breaker_state("anthropic")[0] == "open"
    with pytest.raises(ai_utils.AIProviderDown):
        ai_utils._breaker_check("anthropic")
    # The console's reset (in A) reaches B on its next read.
    ai_utils.reset_breaker("anthropic")
    ai_utils._breakers["anthropic"] = {"failures": 5, "open_until": time.time() + 60, "opened_at": time.time()}
    ai_utils._breaker_synced.clear()
    assert ai_utils.breaker_state("anthropic")[0] == "closed"
