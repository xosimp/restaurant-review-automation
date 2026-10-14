"""The scheduling code's failures are observable (blind re-audit 10/11/26
ARCH-20): it printed (50 print calls in the engine, 37 in the job, no
logger) and swallowed (195 broad excepts in the engine, many saying
nothing), so a degraded generation — an input it went without, a stage it
skipped — could not be told from the logs. Now every line goes through the
module's logger with the restaurant, the job and the stage bound
(schedule_job), and a failure caught and gone on without is one event,
schedule_context.degraded."""
import ast
import logging
import os

import pytest

import logging_setup
import schedule_context as ctx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The benchmark harness prints its table; it is a script's library, not the app.
PRINT_OK = {"schedule_benchmark.py"}


def _modules():
    return sorted(f for f in os.listdir(ROOT) if f.endswith(".py")
                  and (f.startswith("schedule_") or f in ("labor.py", "owner_rules.py", "shift_quality.py")))


@pytest.mark.parametrize("path", _modules())
def test_no_schedule_module_prints(path):
    if path in PRINT_OK:
        return
    tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    prints = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "print"]
    assert not prints, f"{path} prints at {prints} — use the module logger (log) or schedule_context.degraded"


def _silent_handlers(path):
    """Broad handlers outside a loop that neither raise nor say anything."""
    tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def in_loop(n):
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.For, ast.While, ast.comprehension)):
                return True
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return False
        return False
    out = []
    for h in ast.walk(tree):
        if isinstance(h, ast.ExceptHandler) and isinstance(h.type, ast.Name) and h.type.id == "Exception" \
                and not in_loop(h) and h.body[0].lineno != h.lineno:
            src = ast.unparse(ast.Module(body=h.body, type_ignores=[]))
            if not any(k in src for k in ("degraded(", "_soft_fail(", "capture(", "log.", "record_quality_event")) \
                    and not any(isinstance(x, ast.Raise) for x in ast.walk(h)):
                out.append(h.lineno)
    return out


@pytest.mark.parametrize("path", ["schedule_engine.py", "schedule_job.py", "labor.py"])
def test_no_failure_is_swallowed_without_a_word(path):
    """Outside a loop (a row-by-row parse says nothing per row, on purpose), a
    broad handler that goes on without something says so."""
    assert _silent_handlers(path) == []


def test_a_degradation_is_one_structured_event_with_the_generations_context(caplog):
    caplog.set_level(logging.WARNING)
    with logging_setup.context(restaurant_id=7, job="j-1", stage="signals"):
        ctx.degraded("the weather went missing", ValueError("boom"), restaurant_id=7)
    rec = next(r for r in caplog.records if getattr(r, "event", None) == "schedule_degraded")
    assert rec.levelno == logging.WARNING and rec.getMessage() == "the weather went missing"
    assert rec.where.endswith("test_a_degradation_is_one_structured_event_with_the_generations_context")
    assert "boom" in rec.error


def test_inside_a_generation_each_place_is_recorded_once(monkeypatch):
    seen = []
    import ops
    monkeypatch.setattr(ops, "capture", lambda exc, **k: seen.append((repr(exc), k.get("job"))))
    with ctx.frozen_inputs():
        for i in range(5):
            try:
                raise RuntimeError(f"row {i}")
            except RuntimeError as e:
                ctx.degraded("a row went missing", e, restaurant_id=3)
        assert ctx.degradations() == [f"{__name__}.test_inside_a_generation_each_place_is_recorded_once"]
    assert seen == [("RuntimeError('row 0')", "schedule_degraded")]


def test_outside_a_generation_it_is_only_said(monkeypatch):
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: pytest.fail("recorded outside a generation"))
    ctx.degraded("nothing to record", RuntimeError("x"))
    assert ctx.degradations() == []
