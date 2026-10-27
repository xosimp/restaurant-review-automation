"""Every module-level cache is registered with process_caches (release-safety
audit 10/26/26, A04-13).

Restaurant ids restart at 1 in every test database, so a module-level cache
keyed by restaurant id hands one test's answer to the next — order-dependent
passes under --dist loadfile. The conftest resets every registered cache
around each test; this file keeps the registry complete: a module-level
container named like `_*cache*` / `_*memo*` must be registered (the very
object, checked by importing its module), and a module-level lru_cache must
be either pure in its arguments (listed below with why) or registered.
"""
import ast
import importlib
import os
import re
import subprocess

import pytest

import process_caches

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = re.compile(r"(?i)^_\w*(cache|memo(?!r))")
CONTAINER_CALLS = {"OrderedDict", "dict", "set", "defaultdict", "_LRU"}

# Containers that match the name but are not reset by the registry.
EXEMPT = {
    "hosted_dashboard._health_cache": "importing hosted_dashboard boots the app (database, demo seed, scheduler), "
                                      "so no test process imports it; a 5-second /health result with no tenant "
                                      "data",
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
    "schedule_rules._weekday_name_of": "date arithmetic",
    "schedule_time._parse": "time parsing",
    "schedule_time._base_date": "date arithmetic",
    "shift_quality._weekday_name": "date arithmetic",
    "shift_quality._iso_datetime": "date parsing",
}


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
