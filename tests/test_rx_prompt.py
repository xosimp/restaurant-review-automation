"""Schedule re-audit 10/4/26, lens PROMPT: the generation prompt and the
model call (PROMPT-1, 3, 4, 5, 7-13). Each test reproduces the auditor's
finding against the code as it was and holds the fix."""
import json
import re

import pytest

import ai_utils


# ── PROMPT-9: a cache read is priced per model ─────────────────────────────

def test_opus_5_5_cache_reads_are_priced_at_their_list_rate():
    # Opus 5.5: $4 input, cache reads $0.20/MTok — 0.05x, not 0.1x.
    read = ai_utils._estimate_cost("claude-opus-5-5", 0, 0, cache_read_tokens=1_000_000)
    assert read == pytest.approx(0.20)
    # Fable 5.1 (and Mythos 5.1): $0.25 on $10 — 0.025x.
    assert ai_utils._estimate_cost("claude-fable-5-1", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.25)
    assert ai_utils._estimate_cost("claude-mythos-5-1", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.25)
    # Every other model reads at 0.1x: Sonnet 5.5 $0.20 on $2, Fable 5 $1 on $10.
    assert ai_utils._estimate_cost("claude-sonnet-5-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.20)
    assert ai_utils._estimate_cost("claude-fable-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(1.00)
    # A 5-minute cache write stays 1.25x of input.
    assert ai_utils._estimate_cost("claude-opus-5-5", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(5.00)
    assert ai_utils.PRICE_VERSION >= "2026-10-04"
