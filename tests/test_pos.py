"""pos.py registry — the contract every POS provider implements, and the
dispatch/isolation behavior the nightly sync depends on."""
import types

import pytest

import pos


def _fake_provider(connected=False, sync_result=None, crash=False):
    mod = types.ModuleType("fakepos")
    mod.is_connected = lambda rid: connected
    mod.build_shifts_csv = lambda rid, days=60: "date,employee\n" if connected else None

    def sync_to_db(rid):
        if crash:
            raise RuntimeError("provider exploded")
        return sync_result or {"ok": True, "rows": 12}

    mod.sync_to_db = sync_to_db
    return mod


@pytest.fixture(autouse=True)
def restore_registry():
    original = pos.PROVIDERS
    yield
    pos.PROVIDERS = original


def test_real_providers_implement_the_contract():
    """toast/square/clover must each expose the full PROVIDER_API — a new
    provider that misses a function fails here, not at 3am."""
    for name, mod in pos._load_providers().items():
        for fn in pos.PROVIDER_API:
            assert callable(getattr(mod, fn, None)), f"{name}.py missing {fn}()"


def test_connected_provider_picks_the_right_one():
    pos.PROVIDERS = {"toast": _fake_provider(False), "square": _fake_provider(True)}
    name, mod = pos.connected_provider(1)
    assert name == "square"


def test_no_provider_connected():
    pos.PROVIDERS = {"toast": _fake_provider(False)}
    assert pos.connected_provider(1) == (None, None)
    assert pos.sync_restaurant(1)["ok"] is False
    assert pos.connection_status(1) == {"connected": False, "provider": None}


def test_sync_result_carries_provider_name():
    pos.PROVIDERS = {"clover": _fake_provider(True, {"ok": True, "rows": 7})}
    result = pos.sync_restaurant(1)
    assert result == {"ok": True, "rows": 7, "provider": "clover"}


def test_provider_crash_is_contained():
    pos.PROVIDERS = {"toast": _fake_provider(True, crash=True)}
    result = pos.sync_restaurant(1)
    assert result["ok"] is False and "exploded" in result["error"]


def test_broken_is_connected_does_not_block_others():
    broken = _fake_provider(True)
    broken.is_connected = lambda rid: (_ for _ in ()).throw(RuntimeError("auth table gone"))
    pos.PROVIDERS = {"toast": broken, "square": _fake_provider(True)}
    name, _ = pos.connected_provider(1)
    assert name == "square"
