"""Memory re-audit fix round 9/29/26, workstream R2 — a login's own "not for
us" holds wherever that login is shown the recommendation, not only on Home.

  PEOPLE-4        present_many / the brief / Ask / the queue / the DSR view /
                  the schedule / pushes honour the viewing login's own
                  silences (rec_silences), while the owner is still shown it.
  CROSSMODULE-20  the morning brief's DSR carry passes the viewer.
"""
import ast
import os

import pytest

import models
import rec_ledger as rl
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path):
    return create_restaurant(Restaurant(name="Quiet Co", owner_email="q@x.test"), db_path=db_path)


MANAGER = {"id": 12, "role": "manager", "is_admin": 0, "username": "dana"}


def _managers_no(db_path, rid, key="trim_day:Tuesday"):
    rl.present(rid, key, "labor", "home", db_path=db_path)
    rl.record(rid, key, "dismissed", user_id=12, authority="delegate", meta={"kind": "not_for_us"},
              db_path=db_path)


def test_a_surface_shown_to_the_manager_reads_their_no_as_answered(db_path):
    rid = _rid(db_path)
    _managers_no(db_path, rid)
    assert rl.own_silences(rid, 12, db_path=db_path) == {"trim_day:Tuesday"}
    assert rl.own_silences(rid, MANAGER, db_path=db_path) == {"trim_day:Tuesday"}
    assert rl.own_silences(rid, 11, db_path=db_path) == frozenset()
    item = [{"key": "trim_day:Tuesday", "module": "labor"}, {"key": "trim_day:Friday", "module": "labor"}]
    mine = rl.present_many(rid, item, "brief", user_id=12, db_path=db_path)
    assert mine["trim_day:Tuesday"] is None and mine["trim_day:Friday"]
    owners = rl.present_many(rid, item, "brief", user_id=11, db_path=db_path)
    assert owners["trim_day:Tuesday"], "the owner is still shown what the manager passed on"


def test_a_replacing_batch_never_supersedes_what_the_manager_silenced(db_path):
    rid = _rid(db_path)
    rl.present(rid, "read_line:abc", "reviews", "reviews", kind="read_line", db_path=db_path)
    rl.record(rid, "read_line:abc", "dismissed", user_id=12, authority="delegate", meta={"kind": "not_for_us"},
              db_path=db_path)
    rl.present_many(rid, [{"key": "read_line:abc", "module": "reviews", "kind": "read_line"}], "reviews",
                    user_id=12, replaces=("read_line",), db_path=db_path)
    row = models.get_conn(db_path).execute("SELECT status FROM rec_instances WHERE key='read_line:abc'").fetchone()
    assert row["status"] == "open"


def test_the_brief_drops_what_its_viewer_declined(db_path):
    import morning_brief
    rid = _rid(db_path)
    _managers_no(db_path, rid)
    lines = [{"key": "labor", "rec": "trim_day:Tuesday", "text": "Trim Tuesday"}]
    assert morning_brief._drop_answered(rid, lines, db_path, viewer=MANAGER) == []
    assert morning_brief._drop_answered(rid, lines, db_path) == lines


def test_a_push_skips_a_login_whose_own_no_answers_it(db_path, monkeypatch):
    import notify
    import push
    rid = _rid(db_path)
    _managers_no(db_path, rid)
    recs = [{"key": "trim_day:Tuesday"}]
    assert notify.drop_self_silenced(rid, {11, 12}, recs, alert_type="labor_spike", db_path=db_path) == {11}
    monkeypatch.setattr(push, "get_device_tokens", lambda *a, **k: [{"user_id": 11}, {"user_id": 12}])
    assert notify.drop_self_silenced(rid, None, recs, alert_type="labor_spike", db_path=db_path) == {11}
    # Nobody dropped: the audience is left as it was.
    assert notify.drop_self_silenced(rid, None, [{"key": "trim_day:Friday"}], db_path=db_path) is None
    # Health and safety reach everyone.
    monkeypatch.setattr(notify, "never_silenced", lambda t: True)
    assert notify.drop_self_silenced(rid, {11, 12}, recs, alert_type="health", db_path=db_path) == {11, 12}


# Per-login surfaces: every silenced_keys( call in them names its viewer. A
# call without one reads only the restaurant's answers — right for a shared
# output or a restaurant-level decision (listed with why), wrong for a
# surface one login is looking at.
_PER_LOGIN = ("morning_brief.py", "dsr/memory.py", "ask_cavnar.py", "action_queue.py", "strategy_routes.py",
              "client_api.py", "home_brief.py", "schedule_intel.py")


def test_every_silence_read_on_a_per_login_surface_names_its_viewer():
    missing = []
    for rel in _PER_LOGIN:
        tree = ast.parse(open(os.path.join(ROOT, rel)).read())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) \
                        == "silenced_keys":
                    if "viewer" not in {k.arg for k in node.keywords}:
                        missing.append((rel, fn.name, node.lineno))
    assert not missing, f"per-login silence reads without a viewer: {missing}"


def test_the_dsr_carry_passes_its_viewer_to_the_silence_read(db_path, monkeypatch):
    """CROSSMODULE-20: morning_carry reads the viewer's own silences (a
    finished report is needed to reach the read; the call itself is pinned)."""
    src = open(os.path.join(ROOT, "dsr", "memory.py")).read()
    assert "silenced_keys(restaurant_id, db_path=db_path, viewer=user)" in src
