"""Context re-audit 10/7/26 (restaurant_context, the page reads, the DSR
narrative, Ask) — the fixes, held on their real paths.

  #1  a view-as session (the owner's login dict + acting_admin_id) and the
      owner never share one cached memory section or Ask snapshot: support
      never reads the owner's author-only line, and the owner is never
      served support's copy — in either order.
"""
import sys

import pytest

import models
import owner_memory
import restaurant_context as rc
from models import Restaurant, create_restaurant

OWNER_ID = 11


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    import ask_cavnar
    rc.invalidate()
    ask_cavnar._CONTEXT_CACHE.clear()
    yield
    rc.invalidate()
    ask_cavnar._CONTEXT_CACHE.clear()


def _rid(name="Reaudit Grill"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                        timezone="America/Chicago"))


def _owner(rid):
    return {"id": OWNER_ID, "restaurant_id": rid, "role": "client", "is_admin": 0, "username": "erik"}


def _view_as(rid, admin_id=99):
    return dict(_owner(rid), acting_admin_id=admin_id, acting_admin="sam", acting_admin_role="support",
                device_type="admin-view-as")


PRIVATE = "Remind me to call the landlord about the lease"


def _memory(rid, viewer):
    return rc.section(rid, "memory", viewer=viewer,
                      params={"surface": "ask", "budget_chars": 4000, "whole": True})


@pytest.mark.parametrize("first", ["owner", "view_as"])
def test_view_as_and_the_owner_never_share_a_memory_section(first):
    rid = _rid()
    owner_memory.remember(rid, PRIVATE, audience="author", user=_owner(rid))
    order = [("owner", _owner(rid)), ("view_as", _view_as(rid))]
    if first == "view_as":
        order.reverse()
    seen = {who: _memory(rid, v) for who, v in order}
    assert PRIVATE in seen["owner"].text
    assert PRIVATE not in seen["view_as"].text
    assert seen["owner"].scope != seen["view_as"].scope
    # And again from L1 (the second read of each must hold too).
    assert PRIVATE in _memory(rid, _owner(rid)).text
    assert PRIVATE not in _memory(rid, _view_as(rid)).text
    # A cold process (L1 dropped) reads L2 under each one's own scope.
    rc.invalidate()
    assert PRIVATE in _memory(rid, _owner(rid)).text
    assert PRIVATE not in _memory(rid, _view_as(rid)).text


def test_the_scope_names_the_login_behind_a_view_as_and_two_admins_differ():
    rid = _rid()
    owner = rc.viewer_scope(_owner(rid), "memory")
    sam = rc.viewer_scope(_view_as(rid, 99), "memory")
    jo = rc.viewer_scope(_view_as(rid, 98), "memory")
    assert owner.startswith(f"login:{OWNER_ID}:")
    assert sam.startswith("login:99:") and jo.startswith("login:98:")
    assert len({owner, sam, jo}) == 3


@pytest.mark.parametrize("first", ["owner", "view_as"])
def test_view_as_and_the_owner_never_share_an_ask_snapshot(first):
    import ask_cavnar
    import ask_cavnar_tools
    rid = _rid()
    owner_memory.remember(rid, PRIVATE, audience="author", user=_owner(rid))
    r = models.get_restaurant(rid)
    order = [("owner", _owner(rid)), ("view_as", _view_as(rid))]
    if first == "view_as":
        order.reverse()
    seen = {who: ask_cavnar.build_context(ask_cavnar_tools.viewer_restaurant(r, u)) for who, u in order}
    assert PRIVATE in seen["owner"]
    assert PRIVATE not in seen["view_as"]
