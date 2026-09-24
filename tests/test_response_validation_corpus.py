"""The Response Validation Layer's golden corpus.

tests/fixtures/validation_corpus/<surface>.jsonl holds every probe from the
"Never Say" audit (NS1–NS6, 9/24/26): each bad output with the rule codes,
verdict and rewrite it must produce, and good outputs that must pass
untouched. contexts.json holds the shared contexts the cases name.

Every case failed before response_validation existed (there was nothing to
produce a verdict), and each rule has at least one bad case here — see
test_every_rule_has_a_golden_case.
"""
import json
import os

import pytest

import response_validation as rv

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "validation_corpus")


def _contexts():
    with open(os.path.join(HERE, "contexts.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _cases():
    out = []
    for name in sorted(os.listdir(HERE)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(json.loads(line))
    return out


CONTEXTS = _contexts()
CASES = _cases()


def build_ctx(case) -> rv.ValidationContext:
    ctx = case["ctx"]
    base = dict(CONTEXTS[ctx]) if isinstance(ctx, str) else dict(ctx)
    base.update(case.get("ctx_patch") or {})
    return rv.ValidationContext(**base)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(case):
    v = rv.validate(case["text"], build_ctx(case))
    exp = case["expect"]
    got = sorted(set(v.codes))
    assert (v.verdict, got) == (exp["verdict"], exp["rules"]), (
        f"{case['src']}: {case['text']!r}\n  → {v.text!r}\n  findings {v.findings}")
    if "text_is" in exp:
        assert v.text == exp["text_is"], case["src"]
    for s in exp.get("contains", []):
        assert s in v.text, f"{case['src']}: {s!r} not in {v.text!r}"
    for s in exp.get("absent", []):
        assert s.lower() not in v.text.lower(), f"{case['src']}: {s!r} still in {v.text!r}"


def test_corpus_covers_every_probe_source():
    """Each audit report contributes cases (NS1–NS6)."""
    srcs = " ".join(c["src"] for c in CASES)
    for report in ("NS1", "NS2", "NS3", "NS4", "NS5", "NS6"):
        assert report in srcs, f"no golden case from {report}"


def test_every_rule_has_a_golden_case():
    fired = set()
    for c in CASES:
        fired |= set(c["expect"]["rules"])
    assert set(rv.RULES) <= fired, f"rules with no golden case: {sorted(set(rv.RULES) - fired)}"


def test_every_surface_file_is_a_surface():
    for c in CASES:
        surface = build_ctx(c).surface
        assert surface in rv.SURFACES


def test_good_cases_pass_untouched_and_bad_cases_do_not():
    goods = [c for c in CASES if c.get("good")]
    bads = [c for c in CASES if not c.get("good")]
    assert len(goods) >= 40 and len(bads) >= 150
    for c in goods:
        v = rv.validate(c["text"], build_ctx(c))
        assert v.verdict == "pass" and not v.findings and v.text == c["text"], c["src"]
    for c in bads:
        assert c["expect"]["rules"], c["src"]
