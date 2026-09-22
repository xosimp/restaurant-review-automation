"""Edge cases in the shared AI platform (ai_utils) — the edge-case audit, area AI.

Every model call in the product passes through ai_utils: one client
factory, one retry wrapper, one budget check, one pricing table. These tests
protect the bounds that make a slow or failing provider somebody else's
problem rather than ours:

- the Anthropic client can't hold a request thread for ten minutes (AI-1),
- retries aren't stacked three-deep on top of the SDK's own (AI-13),
- the breaker's "one probe" really is one (AI-13),
- a model override can't be sent a thinking config it rejects (AI-14),
- the ledger prices models at their list price (AI-12),
- the budget cache doesn't grow and get scanned forever (AI-10),
- a refusal isn't mistaken for an answer (AI-24),
- the billing page's Stripe calls are bounded too (AI-33).

Tests marked xfail(strict=True) assert the CORRECT behaviour for a defect
the audit confirmed; each flips to a failure the day the fix lands, so the
marker is removed with the fix.
"""
import importlib.util
import json
import os
import re

import anthropic
import httpx
import pytest

import ai_utils
import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolate(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr("ai_utils.time.sleep", lambda s: None)
    ai_utils._budget_cache.clear()
    ai_utils._clients.clear()
    ai_utils.reset_breaker()
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    yield
    ai_utils._budget_cache.clear()
    ai_utils._clients.clear()
    ai_utils.reset_breaker()


class _Usage:
    input_tokens = 10
    output_tokens = 10
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _Block:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Msg:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Client:
    """A stand-in anthropic client: raises `exc` for the first `failures`
    calls, then returns `reply`. Records every call's kwargs."""

    def __init__(self, failures=0, exc=None, reply=None):
        self.failures, self.exc = failures, exc
        self.reply = reply or _Msg([_Block(type="text", text="ok")])
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise self.exc
        return self.reply


def _rate_limit_error():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError("rate limited", response=httpx.Response(429, request=req), body=None)


def _read_timeout(client):
    t = client.timeout
    return getattr(t, "read", t)


def _load_check_timeouts():
    spec = importlib.util.spec_from_file_location(
        "check_timeouts_edge", os.path.join(ROOT, "scripts", "check_timeouts.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── AI-1: the shared client is bounded ──────────────────────────────────────

def test_the_shared_anthropic_client_gives_up_reading_within_ninety_seconds(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _read_timeout(ai_utils.get_client()) <= 90


def test_the_shared_anthropic_client_leaves_retrying_to_create_with_retry(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert ai_utils.get_client().max_retries == 0


def test_an_explicit_timeout_is_honoured_by_the_shared_client(monkeypatch):
    """The one call site that passes a timeout (inventory's 45s) gets it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert _read_timeout(ai_utils.get_client(timeout=45.0)) == 45.0


def test_the_timeout_lint_flags_an_anthropic_client_built_with_no_timeout(tmp_path, monkeypatch):
    lint = _load_check_timeouts()
    (tmp_path / "some_module.py").write_text(
        "import anthropic\nclient = anthropic.Anthropic(api_key='x')\n")
    monkeypatch.setattr(lint, "ROOT", str(tmp_path))
    assert lint.offenders(), "an unbounded Anthropic client must be flagged"


def test_the_timeout_lint_still_flags_a_bare_requests_call(tmp_path, monkeypatch):
    """Control for the test above: the lint's existing rule works on the
    same scratch tree, so the xfail is about Anthropic, not the harness."""
    lint = _load_check_timeouts()
    (tmp_path / "some_module.py").write_text("import requests\nrequests.get('https://x')\n")
    monkeypatch.setattr(lint, "ROOT", str(tmp_path))
    assert [o[2] for o in lint.offenders()] == ["requests.get"]


def test_concurrent_ask_streams_cannot_take_every_request_thread():
    """Each open Ask stream holds a request thread for its whole tool loop.
    If the Ask ceiling is at or above the thread count, four people asking
    at once leave nothing to serve login or /health."""
    import client_api
    start = json.load(open(os.path.join(ROOT, "railway.json")))["deploy"]["startCommand"]
    threads = int(re.search(r"--threads\s+(\d+)", start).group(1))
    assert client_api.ASK_MAX_CONCURRENT < threads


# ── AI-13: retry amplification and the single probe ─────────────────────────

def test_a_rate_limit_is_attempted_exactly_retries_plus_one_times():
    client = _Client(failures=99, exc=_rate_limit_error())
    with pytest.raises(anthropic.RateLimitError):
        ai_utils.create_with_retry(client, retries=2, model="m", max_tokens=10)
    assert len(client.calls) == 3


def test_only_one_caller_probes_a_breaker_whose_window_just_expired(monkeypatch):
    monkeypatch.setattr(ai_utils, "CB_OPEN_SECONDS", 0)
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        ai_utils._breaker_record("anthropic", False)
    ai_utils._breaker_check("anthropic")          # the probe, allowed
    # A second caller arriving before the probe has reported must be refused.
    with pytest.raises(ai_utils.AIProviderDown):
        ai_utils._breaker_check("anthropic")


def test_an_open_breaker_refuses_without_calling_the_provider():
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        ai_utils._breaker_record("anthropic", False)
    client = _Client()
    with pytest.raises(ai_utils.AIProviderDown):
        ai_utils.create_with_retry(client, model="m", max_tokens=10)
    assert client.calls == []


# ── AI-14: thinking config per model ────────────────────────────────────────

@pytest.mark.parametrize("purpose", sorted(ai_utils.MODELS))
def test_every_default_model_is_sent_a_thinking_config_it_accepts(purpose, monkeypatch):
    """The current defaults (Haiku 4.5, Sonnet 5, Opus 5 at default effort)
    all accept thinking disabled — pin it so a default change is noticed."""
    env, default = ai_utils.MODELS[purpose]
    monkeypatch.delenv(env, raising=False)
    model = ai_utils.model_for(purpose)
    client = _Client()
    ai_utils.create_with_retry(client, model=model, max_tokens=10)
    assert model in (ai_utils.HAIKU, ai_utils.SONNET, ai_utils.OPUS)
    assert client.calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5"])
def test_a_model_that_rejects_disabled_thinking_is_not_sent_it(model, monkeypatch):
    monkeypatch.setenv("ASK_CAVNAR_MODEL", model)
    client = _Client()
    ai_utils.create_with_retry(client, model=ai_utils.model_for("ask_cavnar"), max_tokens=10)
    assert client.calls[0].get("thinking") != {"type": "disabled"}


# ── AI-12: pricing ──────────────────────────────────────────────────────────

def test_sonnet_5_is_priced_at_its_list_price():
    assert ai_utils._estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(12.0)


def test_every_default_model_has_its_own_pricing_row():
    for purpose, (env, default) in ai_utils.MODELS.items():
        assert default in ai_utils._MODEL_PRICING, f"{purpose} default {default} has no price"


def test_a_model_alias_is_priced_like_its_family():
    dated = ai_utils._estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
    assert ai_utils._estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000) == pytest.approx(dated)


def test_the_global_backstop_does_not_scale_without_limit(monkeypatch):
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 100_000)
    assert ai_utils.global_monthly_budget() < 100_000 * ai_utils.AI_GLOBAL_PER_CLIENT_USD


# ── AI-10: the budget cache is pruned ───────────────────────────────────────

def test_the_budget_cache_keeps_only_current_period_keys(db_path):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d 00:00:00")
    month = now.strftime("%Y-%m-01 00:00:00")
    # Three simulated past days of spend across 1,000 restaurants.
    for rid in range(1, 1001):
        for past in ("2020-01-01 00:00:00", "2020-01-02 00:00:00", "2020-01-03 00:00:00"):
            ai_utils._budget_cache[(rid, past)] = (0.0, 0.1)
    ai_utils.ai_budget_status(1)
    ai_utils.log_ai_usage(1, "test", "claude-sonnet-5", 100, 100)
    stale = [k for k in ai_utils._budget_cache if k[1] not in (today, month)]
    assert stale == []


def test_a_call_still_folds_its_cost_into_the_current_cached_totals(db_path):
    ai_utils.ai_budget_status(1)
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d 00:00:00")
    before = ai_utils._budget_cache[(1, day)][1]
    ai_utils.note_ai_spend(0.25, restaurant_id=1)
    assert ai_utils._budget_cache[(1, day)][1] == pytest.approx(before + 0.25)


# ── AI-24: refusals are distinguishable ─────────────────────────────────────

def test_extract_text_returns_empty_on_a_refusal_with_no_text_block():
    """The helper's contract: no text → "". That is why every caller must
    look at stop_reason itself (see the Ask and drafter refusal tests)."""
    assert ai_utils.extract_text(_Msg([], stop_reason="refusal")) == ""


def test_a_refusal_is_still_recorded_in_the_ledger(db_path):
    client = _Client(reply=_Msg([], stop_reason="refusal"))
    ai_utils.create_with_retry(client, model="claude-sonnet-5", max_tokens=10,
                               restaurant_id=1, action="edge_refusal")
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT status FROM ai_usage WHERE action='edge_refusal'").fetchone()
    conn.close()
    assert row and row["status"] == "ok"


# ── AI-33: Stripe calls are bounded ─────────────────────────────────────────

def test_the_stripe_http_client_is_configured_with_a_short_timeout():
    found = []
    for name in os.listdir(ROOT):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(ROOT, name), encoding="utf-8").read()
        for m in re.finditer(r"stripe\.default_http_client\s*=\s*[^\n]*timeout\s*=\s*([\d.]+)", src):
            found.append(float(m.group(1)))
    assert found and max(found) <= 15
