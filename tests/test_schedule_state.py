"""A generation's state declares its keys (blind re-audit 10/11/26 ARCH-5):
the pass's result was an untyped dict ~130 keys wide, so a misspelt key read
None and the safeguard it guarded was skipped without a word.
schedule_state.GenerationResult is still a dict — serialised at the save,
read with .get by the checkpoints and the Studio — but every key is declared
with the stage that writes it, and an undeclared one is caught."""
import ast
import copy
import json
import os
import pickle

import pytest

import schedule_job as sj
import schedule_state as st

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_it_is_still_the_dict_every_reader_and_checkpoint_reads():
    r = st.GenerationResult({"week_dates": ["2026-10-14"], "review": {"lines": []}})
    assert isinstance(r, dict) and r.get("quality") is None and r["week_dates"] == ["2026-10-14"]
    assert json.loads(json.dumps(r)) == {"week_dates": ["2026-10-14"], "review": {"lines": []}}
    for clone in (copy.deepcopy(r), pickle.loads(pickle.dumps(r)), r.copy()):
        assert type(clone) is st.GenerationResult and clone == r
    assert st.GenerationResult.of(r) is r


def test_a_declared_key_reads_as_an_attribute_and_an_unset_one_is_none():
    r = st.GenerationResult()
    assert r.quality is None
    r.quality = {"score": 80}
    assert r["quality"] == {"score": 80} and r.quality["score"] == 80
    with pytest.raises(AttributeError):
        r.not_a_field = 1


def test_an_undeclared_key_fails_the_suite_instead_of_reading_none(monkeypatch):
    monkeypatch.setenv("SCHEDULE_STATE_STRICT", "1")
    r = st.GenerationResult()
    for act in (lambda: r.__setitem__("reviw", {}), lambda: r.get("qualty"), lambda: r.update(revew=1),
                lambda: st.GenerationResult({"stage_secnds": {}})):
        with pytest.raises(KeyError):
            act()


def test_in_production_an_undeclared_key_is_said_not_raised(monkeypatch, caplog):
    monkeypatch.delenv("SCHEDULE_STATE_STRICT", raising=False)
    r = st.GenerationResult()
    r["some_new_key_for_this_test"] = 1
    assert r.get("some_new_key_for_this_test") == 1
    assert "some_new_key_for_this_test" in st.undeclared()
    assert "undeclared key" in caplog.text


def test_every_key_has_one_stage_and_a_type():
    seen = {}
    for stage, keys in st.STAGES.items():
        for k, t in keys.items():
            assert k not in seen, f"{k} declared by {seen.get(k)} and {stage}"
            assert isinstance(t, type), (k, t)
            seen[k] = stage
    assert set(st.FIELDS) == set(seen) and len(st.FIELDS) > 100


def test_a_candidates_result_is_the_typed_state():
    c = sj.Candidate(spec=sj.PassSpec(kind="week"), result={"week_dates": ["2026-10-14"]}, rows=[], payload={},
                     timer=None)
    assert type(c.result) is st.GenerationResult and c.result.week_dates == ["2026-10-14"]


def _literal_keys(path, receivers):
    """String keys written to or read from `receivers` (local names, or any
    `X.result`) in one module: subscripts, .get/.setdefault/.pop, .update."""
    tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    found = set()

    def recv(n):
        return (isinstance(n, ast.Name) and n.id in receivers) or (isinstance(n, ast.Attribute) and n.attr == "result")

    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and recv(n.value) and isinstance(n.slice, ast.Constant) \
                and isinstance(n.slice.value, str):
            found.add((n.slice.value, n.lineno))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and recv(n.func.value):
            if n.func.attr in ("get", "setdefault", "pop") and n.args and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, str):
                found.add((n.args[0].value, n.lineno))
            if n.func.attr == "update":
                found |= {(k.arg, n.lineno) for k in n.keywords if k.arg}
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute) and n.value.attr == "result" \
                and isinstance(n.value.value, ast.Name) and n.value.value.id in ("cand", "c", "winner", "week",
                                                                                  "self", "first"):
            if n.attr not in dir(dict):
                found.add((n.attr, n.lineno))
    return found


def test_every_key_the_job_reads_or_writes_on_a_pass_is_declared():
    """The job's own reads and writes of a pass's state, statically: a
    misspelling fails here before any test runs a generation."""
    keys = _literal_keys("schedule_job.py", {"result"})
    bad = sorted((k, ln) for k, ln in keys if k not in st.FIELDS)
    assert not bad, f"declare these in schedule_state.STAGES (or fix the spelling): {bad}"


def test_every_key_the_draft_merges_is_declared():
    """_generate_in_parts' merged result and the input builder's facts."""
    keys = {k for k, _ln in _literal_keys("schedule_engine.py", {"merged"})}
    tree = ast.parse(open(os.path.join(ROOT, "schedule_engine.py"), encoding="utf-8").read())
    facts = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_result_facts")
    for n in ast.walk(facts):
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "result" \
                and isinstance(n.slice, ast.Constant) and isinstance(n.ctx, ast.Store):
            keys.add(n.slice.value)
    assert keys and not sorted(k for k in keys if k not in st.FIELDS)
