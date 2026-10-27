"""Every module-level cache this process keeps, in one registry (release-
safety audit 10/26/26, A04-13).

A module that keeps a cache at module level — a dict, an OrderedDict, a set,
rpower's _LRU — registers it beside its definition:

    _CACHE = {}
    process_caches.register("home_brief._CACHE", _CACHE)

`clear_all()` then puts every registered cache back to how it was registered
(empty, or the fixed shape a `{"at": 0.0, "n": 0}` memo started with), so one
call forgets everything this process remembers. The test suite calls it before
and after every test (tests/conftest.py): restaurant ids restart at 1 in every
test database, so a cache keyed by restaurant id would otherwise hand one
test's answer to the next. tests/test_process_caches.py fails on a module-
level `_*cache*` / `_*memo*` container that is neither registered nor listed
as exempt with a reason.

Production never calls clear_all(); each cache keeps its own TTL and bound.
This module imports no app module, so any module can import it at the top.
"""
import copy
import logging
import threading

log = logging.getLogger("process_caches")

# name -> (the cache object, its reset callable)
_PROCESS_CACHES = {}
_LOCK = threading.Lock()


def register(name, cache, reset=None):
    """Register `cache` under `name` ("module._NAME"). `reset` puts it back to
    its starting state; by default a dict/set/OrderedDict is restored to a
    copy of what it holds now (registration sits right after the definition,
    so that is its starting shape), and anything else with a .clear() is
    cleared. Returns `cache`, so a definition can be wrapped in place."""
    if reset is None:
        if isinstance(cache, (dict, set)) and len(cache):
            initial = copy.deepcopy(cache)

            def reset(c=cache, init=initial):
                c.clear()
                c.update(copy.deepcopy(init))
        elif hasattr(cache, "clear"):
            reset = cache.clear
        else:
            raise TypeError(f"process cache {name!r} has no clear(); pass reset=")
    with _LOCK:
        _PROCESS_CACHES[str(name)] = (cache, reset)
    return cache


def clear_all() -> int:
    """Reset every registered cache; returns how many were reset. A reset
    that fails is logged and the rest still run."""
    with _LOCK:
        entries = list(_PROCESS_CACHES.items())
    n = 0
    for name, (_cache, reset) in entries:
        try:
            reset()
            n += 1
        except Exception as e:
            log.warning("process cache %s could not be reset: %s", name, e)
    return n


def registered() -> dict:
    """{name: cache object} for every registered cache (a copy)."""
    with _LOCK:
        return {name: cache for name, (cache, _reset) in _PROCESS_CACHES.items()}


def holds(cache) -> bool:
    """Whether this very object is registered."""
    with _LOCK:
        return any(c is cache for c, _r in _PROCESS_CACHES.values())
