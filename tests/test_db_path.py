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
