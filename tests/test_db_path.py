"""Regression test for the 2026-09-07 incident: reviews.db was written to
the container's ephemeral filesystem instead of Railway's persistent
volume, so sessions/login_history silently reset to empty on every deploy
while users/restaurants/reviews looked unchanged (deterministically
reseeded at boot). DB_PATH must resolve under RAILWAY_VOLUME_MOUNT_PATH
when Railway sets it, so the database actually lives on the mounted
volume and survives redeploys.

Run in a subprocess (not importlib.reload) — reloading models in-process
would rebind its dataclasses (Restaurant, etc.) to new class objects while
every other already-imported module keeps its own reference to the old
ones, breaking isinstance checks for the rest of the test session.
"""
import os
import subprocess
import sys

_PROBE = (
    "import status_manager, models; "
    "print(models.DB_PATH); "
    "print(status_manager.DB_PATH)"
)


def _run(volume_mount_path):
    env = dict(os.environ)
    if volume_mount_path is None:
        env.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
    else:
        env["RAILWAY_VOLUME_MOUNT_PATH"] = volume_mount_path
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    models_db_path, status_manager_db_path = result.stdout.strip().splitlines()
    return models_db_path, status_manager_db_path


def test_db_path_uses_railway_volume_mount_when_set():
    models_db_path, status_manager_db_path = _run("/app/data")
    assert models_db_path == os.path.join("/app/data", "reviews.db")
    # status_manager must track models.DB_PATH, not its own copy, or a fix
    # here would silently miss that module the same way it did before.
    assert status_manager_db_path == models_db_path


def test_db_path_falls_back_locally_without_railway_volume():
    models_db_path, status_manager_db_path = _run(None)
    assert models_db_path == os.path.join(".", "reviews.db")
    assert status_manager_db_path == models_db_path


# ── The migration that makes the move above lossless ────────────────────────
#
# Pointing DB_PATH at the volume also points it at a file that has never
# existed. Without adopt_legacy_db the first boot after the fix comes up on
# an empty database, and the boot seed rebuilds admin + demo so convincingly
# that only the real client configuration is missing. These pin the three
# cases that matter.

def _seeded_db(path):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE marker (note TEXT)")
    conn.execute("INSERT INTO marker VALUES ('the real client data')")
    conn.commit()
    conn.close()


def _marker(path):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT note FROM marker").fetchone()[0]
    finally:
        conn.close()


def test_legacy_db_is_adopted_onto_the_volume(tmp_path, monkeypatch):
    import models
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "data"))
    monkeypatch.setattr(models, "DB_PATH", str(tmp_path / "data" / "reviews.db"))
    _seeded_db("reviews.db")
    volume = tmp_path / "data" / "reviews.db"

    assert models.adopt_legacy_db(str(volume)) is True
    assert _marker(str(volume)) == "the real client data"
    # a copy, not a move — the original is still there to fall back on
    assert (tmp_path / "reviews.db").exists()


def test_adoption_never_overwrites_a_volume_that_already_has_data(tmp_path, monkeypatch):
    import models
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path / "data"))
    monkeypatch.setattr(models, "DB_PATH", str(tmp_path / "data" / "reviews.db"))
    _seeded_db("reviews.db")
    volume = tmp_path / "data"
    volume.mkdir()
    conn_path = str(volume / "reviews.db")
    import sqlite3
    conn = sqlite3.connect(conn_path)
    conn.execute("CREATE TABLE marker (note TEXT)")
    conn.execute("INSERT INTO marker VALUES ('newer, already on the volume')")
    conn.commit()
    conn.close()

    assert models.adopt_legacy_db(conn_path) is False
    assert _marker(conn_path) == "newer, already on the volume"


def test_adoption_is_a_noop_when_the_paths_are_the_same(tmp_path, monkeypatch):
    """Local runs and the whole test suite: DB_PATH is ./reviews.db, which is
    the legacy path, so there is nothing to adopt and nothing to clobber."""
    import models
    monkeypatch.chdir(tmp_path)
    _seeded_db("reviews.db")
    monkeypatch.setattr(models, "DB_PATH", "./reviews.db")
    assert models.adopt_legacy_db("./reviews.db") is False
    assert models.adopt_legacy_db("reviews.db") is False
    assert _marker("reviews.db") == "the real client data"


def test_adoption_never_touches_a_test_or_local_database(tmp_path, monkeypatch):
    """The guard that matters most in practice. Off Railway — every local run
    and the whole test suite — adopt_legacy_db must not copy the developer's
    real reviews.db into a database opened at some other path."""
    import models
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    _seeded_db("reviews.db")
    somewhere_else = str(tmp_path / "test_reviews.db")

    assert models.adopt_legacy_db(somewhere_else) is False
    assert not os.path.exists(somewhere_else)
