"""Every name the durable job queue reaches by string resolves (blind re-audit
10/27/26, B4 "job_queue reaches 26 handlers by string with no test resolving
them all").

job_queue runs a row through `HANDLERS[kind]` ("module:function", resolved by
importlib at run time) and tells `EXPIRE_HOOKS[kind]` when a row expired
unstarted. A renamed handler is not an "unknown kind" (that rule keys on
HANDLERS' keys): every row of it raises AttributeError, is retried after 30,
120 and 600 seconds and dead-letters — a fleet-wide night of POS syncs,
auto-drafts or learners gone, with nothing red before the next morning. The
registry jobs are already resolved in the gate (tests/test_fix_d_jobs.py);
this is the same promise for the queue:

- the kinds, the handlers and the expiry hooks name one set of kinds;
- each handler and hook resolves THROUGH job_queue._resolve (the resolver
  run_one and _expired use) to a function that accepts the claimed Job;
- every kind the app enqueues by literal (`job_queue.enqueue("pos_sync", …)`)
  is a kind this build has a handler for;
- every registry job the scheduler queues as a `scheduled_job` row is a
  registry job with a queue lane and a target that resolves.
"""
import ast
import inspect
import os
import subprocess

import pytest

import job_queue
import jobs_registry

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENQUEUE = {"enqueue", "enqueue_many"}


def _app_sources():
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    return [f for f in files if not f.startswith(("tests/", "scripts/"))]


def _calls(predicate):
    """[(first string argument, "file:line")] of every call `predicate(call)`
    accepts across the app's modules."""
    out = []
    for path in _app_sources():
        with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and predicate(node, path) and node.args \
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                out.append((node.args[0].value, f"{path}:{node.lineno}"))
    return out


def _is_enqueue(call, path):
    """job_queue.enqueue(...) / _jq.enqueue_many(...) — any module alias — or
    a bare enqueue(...) inside job_queue itself (billing_jobs has an outbox
    `enqueue` of its own)."""
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in _ENQUEUE and isinstance(f.value, ast.Name):
        return "jq" in f.value.id.lower() or f.value.id == "job_queue"
    return path == "job_queue.py" and isinstance(f, ast.Name) and f.id in _ENQUEUE


def _is_queue_scheduled_job(call, _path):
    f = call.func
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
    return name == "queue_scheduled_job"


def test_the_kinds_the_handlers_and_the_expiry_hooks_name_one_set_of_kinds():
    assert len(job_queue.KINDS) >= 15, "guards the guard: the queue's kinds were not found"
    assert set(job_queue.HANDLERS) == set(job_queue.KINDS), (
        "a kind with no handler is deferred for ever; a handler with no kind runs with the default group "
        f"and class: {sorted(set(job_queue.HANDLERS) ^ set(job_queue.KINDS))}")
    assert set(job_queue.EXPIRE_HOOKS) <= set(job_queue.KINDS), sorted(set(job_queue.EXPIRE_HOOKS) - set(job_queue.KINDS))


def _a_claimed_row(kind):
    return job_queue.Job({"id": 1, "kind": kind, "payload": {}, "checkpoint": {}, "worker_id": "contract-test"})


@pytest.mark.parametrize("kind", sorted(job_queue.HANDLERS))
def test_every_handler_resolves_to_a_function_that_takes_the_claimed_job(kind):
    target = job_queue.HANDLERS[kind]
    try:
        fn = job_queue._resolve(target)
    except (ImportError, AttributeError) as e:
        pytest.fail(f"job_queue.HANDLERS[{kind!r}] = {target!r} does not resolve ({type(e).__name__}: {e}): "
                    "every queued row of this kind would fail, retry and dead-letter")
    assert callable(fn), target
    try:
        inspect.signature(fn).bind(_a_claimed_row(kind))
    except TypeError as e:
        pytest.fail(f"{target} cannot be called with the claimed row the way run_one calls it: {e}")


@pytest.mark.parametrize("kind", sorted(job_queue.EXPIRE_HOOKS))
def test_every_expiry_hook_resolves_to_a_function_that_takes_the_expired_row(kind):
    target = job_queue.EXPIRE_HOOKS[kind]
    try:
        fn = job_queue._resolve(target)
    except (ImportError, AttributeError) as e:
        pytest.fail(f"job_queue.EXPIRE_HOOKS[{kind!r}] = {target!r} does not resolve ({type(e).__name__}: {e}): "
                    "an expired row would hand nothing back")
    inspect.signature(fn).bind(_a_claimed_row(kind))


def test_every_kind_the_app_enqueues_is_a_kind_this_build_runs():
    found = _calls(_is_enqueue)
    # Guards the guard: the literal call sites the scan must see.
    assert len(found) >= 10, found
    assert {"pos_sync", "learner", "schedule_generate", "scheduled_job"} <= {k for k, _ in found}
    unknown = [(k, at) for k, at in found if k not in job_queue.KINDS]
    assert unknown == [], f"enqueued kinds no handler runs (each row would be deferred for ever): {unknown}"


def test_every_job_the_scheduler_queues_is_a_queued_registry_job_whose_target_resolves():
    import importlib
    found = _calls(_is_queue_scheduled_job)
    assert {"backup_db", "restore_drill"} <= {k for k, _ in found}, found
    for name, at in found:
        spec = jobs_registry.spec(name)
        assert spec, f"{at} queues {name!r}, which is not a registry job"
        assert spec.get("queue"), f"{at} queues {name!r}, which has no queue lane: run_queued_scheduled_job refuses it"
        mod, fn = spec["target"]
        assert callable(getattr(importlib.import_module(mod), fn, None)), f"{name}'s target {mod}.{fn} does not resolve"
