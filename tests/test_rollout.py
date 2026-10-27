"""Cohort rollout flags (rollout.py, audit #25 roadmap P3): a stable hash
bucket per (feature, restaurant), allow and deny lists, the table set from
the console, and an environment override that wins. The console route and
its audit row run through the real app in tests/test_real_app_permissions.py.
"""
import pytest

import rollout


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ROLLOUT_NEW_HOME", "ROLLOUT_OTHER"):
        monkeypatch.delenv(k, raising=False)
    rollout._flag_cache.clear()
    yield
    rollout._flag_cache.clear()


def test_the_table_is_created_at_boot(db_path):
    import models
    conn = models.get_conn(db_path)
    try:
        rows = conn.execute("SELECT feature, percent, allow_json, deny_json, note, updated_by, updated_at "
                            "FROM rollout_flags").fetchall()
    finally:
        conn.close()
    assert rows == []


def test_a_feature_nothing_sets_is_the_default(db_path):
    assert rollout.enabled("new_home", 1, db_path=db_path) is False
    assert rollout.enabled("new_home", 1, default=True, db_path=db_path) is True


def test_the_bucket_is_stable_and_differs_by_feature():
    assert rollout.bucket("new_home", 42) == rollout.bucket("new_home", 42)
    assert 0 <= rollout.bucket("new_home", 42) < 100
    a = [rollout.bucket("new_home", r) for r in range(1, 401)]
    b = [rollout.bucket("other", r) for r in range(1, 401)]
    assert a != b
    # roughly uniform: each quarter of the range holds a fair share of 400
    for q in range(4):
        assert 60 <= sum(1 for x in a if 25 * q <= x < 25 * (q + 1)) <= 140


def test_a_percentage_takes_its_share_and_raising_it_only_adds(db_path):
    rollout.set_flag("new_home", 10, db_path=db_path)
    at10 = {r for r in range(1, 1001) if rollout.enabled("new_home", r, db_path=db_path)}
    rollout.set_flag("new_home", 30, db_path=db_path)
    at30 = {r for r in range(1, 1001) if rollout.enabled("new_home", r, db_path=db_path)}
    assert 50 <= len(at10) <= 150 and 220 <= len(at30) <= 380
    assert at10 <= at30
    rollout.set_flag("new_home", 0, db_path=db_path)
    assert not any(rollout.enabled("new_home", r, db_path=db_path) for r in range(1, 200))
    rollout.set_flag("new_home", 100, db_path=db_path)
    assert all(rollout.enabled("new_home", r, db_path=db_path) for r in range(1, 200))


def test_deny_beats_allow_beats_the_bucket(db_path):
    rollout.set_flag("new_home", 100, deny=[7], db_path=db_path)
    assert rollout.enabled("new_home", 7, db_path=db_path) is False
    rollout.set_flag("new_home", 0, allow=[7, 9], deny=[9], db_path=db_path)
    assert rollout.enabled("new_home", 7, db_path=db_path) is True
    assert rollout.enabled("new_home", 9, db_path=db_path) is False
    assert rollout.enabled("new_home", 8, db_path=db_path) is False


def test_a_platform_call_with_no_restaurant_is_in_only_at_100(db_path):
    rollout.set_flag("new_home", 99, db_path=db_path)
    assert rollout.enabled("new_home", None, db_path=db_path) is False
    rollout.set_flag("new_home", 100, db_path=db_path)
    assert rollout.enabled("new_home", None, db_path=db_path) is True


def test_set_flag_returns_before_and_after_and_drops_the_cache(db_path):
    before, after = rollout.set_flag("new_home", 5, note="first", by="will", db_path=db_path)
    assert before is None and after["percent"] == 5 and after["note"] == "first"
    assert rollout.state("new_home", db_path=db_path)["percent"] == 5          # cached now
    before, after = rollout.set_flag("new_home", 50, db_path=db_path)
    assert before["percent"] == 5 and rollout.state("new_home", db_path=db_path)["percent"] == 50
    [row] = rollout.list_flags(db_path=db_path)
    assert row["feature"] == "new_home" and row["percent"] == 50 and row["env_override"] is None


def test_another_process_change_is_seen_after_the_cache_window(db_path, monkeypatch):
    import time
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    rollout.set_flag("new_home", 5, db_path=db_path)
    assert rollout.state("new_home", db_path=db_path)["percent"] == 5
    import models
    conn = models.get_conn(db_path)
    conn.execute("UPDATE rollout_flags SET percent=60 WHERE feature='new_home'")     # another process's write
    conn.commit()
    conn.close()
    assert rollout.state("new_home", db_path=db_path)["percent"] == 5
    clock[0] += rollout.ROLLOUT_CACHE_SECONDS + 1
    assert rollout.state("new_home", db_path=db_path)["percent"] == 60


@pytest.mark.parametrize("bad", [dict(feature="New Home", percent=5), dict(feature="x", percent=5),
                                 dict(feature="new_home", percent=101), dict(feature="new_home", percent="lots"),
                                 dict(feature="new_home", percent=5, allow=["seven"])])
def test_a_malformed_setting_is_refused_before_anything_is_written(db_path, bad):
    with pytest.raises(rollout.RolloutError):
        rollout.set_flag(db_path=db_path, **bad)
    assert rollout.list_flags(db_path=db_path) == []


@pytest.mark.parametrize("value,expect", [
    ("on", (100, [], [])), ("off", (0, [], [])), ("25", (25, [], [])), ("25%", (25, [], [])),
    ("10;allow=3,5;deny=7", (10, [3, 5], [7])), ("0;allow=4", (0, [4], [])),
])
def test_the_env_grammar(value, expect):
    st = rollout.parse_env(value)
    assert (st["percent"], st["allow"], st["deny"]) == expect and st["source"] == "env"


@pytest.mark.parametrize("value", ["lots", "150", "10;bogus=1", "10;allow=x"])
def test_an_unreadable_env_value_is_off_and_says_so(value):
    st = rollout.parse_env(value)
    assert st["percent"] == 0 and st["allow"] == [] and st.get("error")


def test_the_environment_wins_over_the_table(db_path, monkeypatch):
    rollout.set_flag("new_home", 100, db_path=db_path)
    monkeypatch.setenv("ROLLOUT_NEW_HOME", "off")
    assert rollout.enabled("new_home", 1, db_path=db_path) is False
    assert rollout.list_flags(db_path=db_path)[0]["env_override"]["percent"] == 0
    monkeypatch.setenv("ROLLOUT_NEW_HOME", "0;allow=1")
    assert rollout.enabled("new_home", 1, db_path=db_path) is True
    assert rollout.enabled("new_home", 2, db_path=db_path) is False
    assert rollout.env_name("new_home") == "ROLLOUT_NEW_HOME"


def test_a_failed_read_is_the_default_and_is_reported(db_path, monkeypatch):
    import ops
    seen = []
    monkeypatch.setattr(ops, "capture", lambda exc, job="unknown", **k: seen.append(job))
    monkeypatch.setattr(rollout, "_read_table", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert rollout.enabled("new_home", 1, default=True, db_path=db_path) is True
    assert rollout.enabled("new_home", 1, db_path=db_path) is False
    assert seen == ["rollout_read", "rollout_read"]
