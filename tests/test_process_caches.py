"""Every module-level cache is registered with process_caches (release-safety
audit 10/26/26, A04-13; blind re-audit 10/27/26 B1 F15, B4 "the process-cache
registry finds caches by name only").

Restaurant ids restart at 1 in every test database, so a module-level cache
keyed by restaurant id hands one test's answer to the next — order-dependent
passes under --dist loadfile. The conftest resets every registered cache
around each test; this file keeps the registry complete: a module-level
container named like `_*cache*` / `_*memo*` must be registered (the very
object, checked by importing its module), and a module-level lru_cache must
be either pure in its arguments (listed below with why) or registered.

A name is not the only sign of state: `_shared_history`, `_studio_live`,
`_live_seen`, `_admin_hits` carried one test's answer to the next under no
`cache`/`memo` name. So any module-level dict / set / OrderedDict /
defaultdict / deque / Counter that a function in its module MUTATES must be
registered too, or listed in MUTATED_EXEMPT with why. The ones that predate
the rule and nobody has reviewed yet are frozen in
tests/ratchets/process_state.json: that list may only shrink (register one,
or exempt it with a reason, and the test says to drop it), and a new
mutated container fails here the day it is written.
"""
import ast
import importlib
import os
import re
import subprocess

import pytest

import process_caches

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = re.compile(r"(?i)^_\w*(cache|memo(?!r))")
CONTAINER_CALLS = {"OrderedDict", "dict", "set", "defaultdict", "_LRU"}

# Containers that match the name but are not reset by the registry.
EXEMPT = {
    "hosted_dashboard._health_cache": "importing hosted_dashboard boots the app (database, demo seed, scheduler), "
                                      "so no test process imports it; a 5-second /health result with no tenant "
                                      "data",
    "process_caches._PROCESS_CACHES": "the registry itself (name -> cache); clearing it would forget every cache",
}
# Module-level functools caches: pure in their arguments (or in files bundled
# with the code), so no test can see another's entry.
LRU_EXEMPT = {
    "ai_guard._prepared_cached": "pure in the text",
    "ai_guard._figures_cached": "pure in the text",
    "bar.build._week_after": "date arithmetic",
    "bar.common._mdy10": "date formatting",
    "bar.common._display_name": "string normalising",
    "bar.common.pos_display_name": "string normalising",
    "bar.prices._family_of": "string classification",
    "bar.prices._day": "date parsing",
    "bar.products._valid_gtin": "checksum",
    "bar.products._parse_pack": "string parsing",
    "confidence_engine._p_greater": "a closed-form probability",
    "demo_store.calendar._catalog": "the season files bundled with the code",
    "demo_store.calendar.demand": "the simulated store's seeded demand",
    "demo_store.day.week": "the simulated store's seeded week",
    "demo_store.day._by_category": "the simulated store's fixed menu",
    "demo_store.day.business_day": "the simulated store's seeded day",
    "demo_store.purchasing._food_items": "the simulated store's fixed menu",
    "demo_store.purchasing.explode": "the simulated store's fixed recipes",
    "demo_store.purchasing._usage_cached": "the simulated store's seeded usage",
    "demo_store.world.employees": "the simulated store's fixed roster",
    "demo_store.world.by_job": "the simulated store's fixed roster",
    "demo_store.world.menu": "the simulated store's fixed menu",
    "demo_store.world.item_by_name": "the simulated store's fixed menu",
    "response_validation._key_family": "pure in the key",
    "response_validation._context_pool_for": "pure in the text",
    "schedule_roles._family_words": "string normalising",
    "schedule_roles._role_words_cached": "string normalising",
    "schedule_roles._fold_table": "pure over its arguments (a set of job codes and the role map, both as tuples)",
    "schedule_rules._weekday_name_of": "date arithmetic",
    "schedule_time._parse": "time parsing",
    "schedule_time._base_date": "date arithmetic",
    "shift_quality._weekday_name": "date arithmetic",
    "shift_quality._iso_datetime": "date parsing",
}


MUTABLE_CALLS = CONTAINER_CALLS | {"deque", "Counter", "WeakValueDictionary", "WeakKeyDictionary", "WeakSet"}
_MUTATORS = {"add", "update", "setdefault", "pop", "popitem", "clear", "discard", "remove", "append", "extend",
             "insert", "appendleft", "popleft", "move_to_end", "difference_update", "intersection_update",
             "__setitem__", "__delitem__"}
# Mutated module-level containers that are deliberately NOT reset between
# tests, and why.
MUTATED_EXEMPT = {
    "process_caches._PROCESS_CACHES": "the registry itself",
    "gunicorn.conf._state": "gunicorn's master process state (the queue worker it supervises); no test process "
                            "imports gunicorn.conf",
}
PROCESS_STATE = os.path.join(ROOT, "tests", "ratchets", "process_state.json")


def _sources():
    files = subprocess.check_output(["git", "ls-files", "*.py"], cwd=ROOT, text=True).split()
    return [f for f in files if not f.startswith(("tests/", "scripts/"))]


def _module(path):
    return path[:-3].replace("/", ".")


def _is_container(v):
    if isinstance(v, (ast.Dict, ast.Set)):
        return True
    if isinstance(v, ast.Call):
        f = v.func
        return (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)) in CONTAINER_CALLS
    return False


def _scan():
    """(module-level caches, module-level functools-cached functions), each
    as "module.name"."""
    caches, lrus = [], []
    for path in _sources():
        with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and NAME.match(tgt.id) and _is_container(node.value):
                        caches.append(f"{_module(path)}.{tgt.id}")
            elif isinstance(node, ast.FunctionDef):
                if any(re.search(r"\blru_cache\b|\bcache\b", ast.unparse(d)) for d in node.decorator_list):
                    lrus.append(f"{_module(path)}.{node.name}")
    return caches, lrus


def _is_mutable_container(v):
    if isinstance(v, (ast.Dict, ast.Set)):
        return True
    if isinstance(v, ast.Call):
        f = v.func
        return (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)) in MUTABLE_CALLS
    return False


_MUTATED = []


def _mutated_scan():
    """Every module-level dict/set/OrderedDict/defaultdict/deque/Counter a
    function of its own module mutates (item assignment or deletion, an
    augmented assignment, or a mutating method), as "module.name" (read
    once per test process)."""
    if not _MUTATED:
        _MUTATED.append(_mutated_scan_now())
    return list(_MUTATED[0])


def _mutated_scan_now():
    out = []
    for path in _sources():
        with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        conts = set()
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else \
                [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None else []
            if targets and _is_mutable_container(node.value):
                conts |= {t.id for t in targets if isinstance(t, ast.Name)}
        if not conts:
            continue
        mutated = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                if isinstance(n, (ast.Assign, ast.AugAssign, ast.Delete)):
                    for t in (n.targets if isinstance(n, (ast.Assign, ast.Delete)) else [n.target]):
                        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in conts:
                            mutated.add(t.value.id)
                        elif isinstance(n, ast.AugAssign) and isinstance(t, ast.Name) and t.id in conts:
                            mutated.add(t.id)
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _MUTATORS \
                        and isinstance(n.func.value, ast.Name) and n.func.value.id in conts:
                    mutated.add(n.func.value.id)
        out += [f"{_module(path)}.{name}" for name in sorted(mutated)]
    return out


def _unreviewed():
    import json
    with open(PROCESS_STATE, encoding="utf-8") as fh:
        return set(json.load(fh)["unreviewed"])


def _registered(full):
    mod_name, attr = full.rsplit(".", 1)
    return process_caches.holds(getattr(importlib.import_module(mod_name), attr))


def test_every_mutated_module_level_container_is_registered_exempt_or_frozen():
    found = _mutated_scan()
    assert len(found) >= 100 and "labor._shared_history" in found and "security._admin_hits" in found, len(found)
    frozen = _unreviewed()
    new = [f for f in found if f not in MUTATED_EXEMPT and f not in EXEMPT and f not in frozen
           and not _registered(f)]
    assert new == [], ("module-level state a function mutates, neither registered nor exempt: register each "
                       "beside its definition (process_caches.register) or add it to MUTATED_EXEMPT with why: "
                       f"{new}")


def test_the_frozen_list_only_shrinks():
    found = set(_mutated_scan())
    frozen = _unreviewed()
    gone = sorted(frozen - found)
    assert gone == [], f"no longer a mutated module-level container — drop from {PROCESS_STATE}: {gone}"
    done = sorted(f for f in frozen if f in MUTATED_EXEMPT or _registered(f))
    assert done == [], f"registered or exempt now — drop from {PROCESS_STATE}: {done}"


def test_the_containers_the_reaudit_named_are_registered():
    for full in ("labor._shared_history", "schedule_engine._studio_live", "rpower._live_seen", "rpower._live_marked",
                 "pos_io._schema_seen", "pos_io._store_runs", "ops._page_memory", "models._HISTORY_COLUMNS_OK",
                 "ai_utils._timeouts_seen", "schedule_state._SEEN", "security._admin_hits", "ai_utils._ai_call_log"):
        assert _registered(full), full


def test_the_scan_finds_the_caches():
    caches, lrus = _scan()
    assert len(caches) >= 60 and "home_brief._CACHE" in caches and "rpower._menu_cache" in caches
    assert len(lrus) >= 20


def test_every_module_level_cache_is_registered_or_exempt():
    caches, _ = _scan()
    missing = []
    for full in caches:
        if full in EXEMPT:
            continue
        mod_name, attr = full.rsplit(".", 1)
        obj = getattr(importlib.import_module(mod_name), attr)
        if not process_caches.holds(obj):
            missing.append(full)
    assert missing == [], ("register these beside their definition (process_caches.register) or add them to "
                           f"EXEMPT with the reason: {missing}")


def test_every_functools_cache_is_exempt_with_a_reason_or_registered():
    _, lrus = _scan()
    held = process_caches.registered()
    unreviewed = [f for f in lrus if f not in LRU_EXEMPT and f not in held]
    assert unreviewed == [], f"a module-level lru_cache must be pure (LRU_EXEMPT, with why) or registered: {unreviewed}"


def test_no_stale_exemption():
    caches, lrus = _scan()
    assert sorted(set(EXEMPT) - set(caches)) == []
    assert sorted(set(LRU_EXEMPT) - set(lrus)) == []


def test_clear_all_resets_a_registered_cache_to_how_it_was_registered():
    plain, shaped = {}, {"at": 0.0, "n": 0}
    process_caches.register("tests.plain", plain)
    process_caches.register("tests.shaped", shaped)
    try:
        plain[1] = "an earlier test's answer"
        shaped.update(at=99.0, n=7, extra=True)
        assert process_caches.clear_all() >= 2
        assert plain == {} and shaped == {"at": 0.0, "n": 0}
    finally:
        with process_caches._LOCK:
            process_caches._PROCESS_CACHES.pop("tests.plain", None)
            process_caches._PROCESS_CACHES.pop("tests.shaped", None)


def test_a_reset_that_fails_does_not_stop_the_others():
    seen = []

    def broken():
        raise RuntimeError("boom")
    process_caches.register("tests.broken", object(), reset=broken)
    process_caches.register("tests.fine", {}, reset=lambda: seen.append(1))
    try:
        process_caches.clear_all()
        assert seen == [1]
    finally:
        with process_caches._LOCK:
            process_caches._PROCESS_CACHES.pop("tests.broken", None)
            process_caches._PROCESS_CACHES.pop("tests.fine", None)


def test_a_cache_with_no_clear_needs_a_reset():
    with pytest.raises(TypeError):
        process_caches.register("tests.bad", object())


def test_a_real_cache_keyed_by_restaurant_id_is_empty_at_the_start_of_a_test():
    """The conftest fixture ran before this test: a restaurant-keyed cache
    another test filled is empty here."""
    import home_brief
    assert home_brief._CACHE == {}
    home_brief._CACHE[1] = ("left behind", 0.0)
    process_caches.clear_all()
    assert home_brief._CACHE == {}


if __name__ == "__main__":
    # python tests/test_process_caches.py --freeze: rewrite the frozen list
    # as what is still unreviewed (it may only shrink — review, don't grow).
    import json
    import sys
    sys.path.insert(0, ROOT)
    if "--freeze" in sys.argv:
        left = sorted(f for f in _mutated_scan() if f not in MUTATED_EXEMPT and f not in EXEMPT
                      and not _registered(f))
        with open(PROCESS_STATE, "w", encoding="utf-8") as fh:
            json.dump({"about": "module-level containers a function mutates that predate the rule and are neither "
                                "registered with process_caches nor exempt with a reason "
                                "(tests/test_process_caches.py); only shrinks",
                       "unreviewed": left}, fh, indent=1)
            fh.write("\n")
        print(f"{len(left)} unreviewed")
