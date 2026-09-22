"""db_restore.py — restoring a database snapshot at boot, before anything
opens the file.

The runbook used to restore by moving and copying reviews.db with `railway
ssh` while the service was running. The web threads and the scheduler open
connections continuously; one landing between the `mv` and the `cp` created
an empty database with its own WAL, which then replayed an empty schema over
the restored file. integrity_check still said ok, boot seeded the admin and
demo accounts, and /health went green on an empty platform (DATA-2).

Now the swap is a boot step. Set RESTORE_FROM to the snapshot's path and
redeploy: hosted_dashboard calls restore_if_requested() before its first
get_conn, the scheduler stays off while the variable is set, and a marker
beside the database stops a later boot from restoring the same snapshot a
second time over newer writes. Remove the variable once the restore is
confirmed.
"""
import logging
import os
import shutil
import sqlite3
import time

log = logging.getLogger(__name__)


def requested() -> str:
    return (os.getenv("RESTORE_FROM") or "").strip()


def _marker_path(db_path, snapshot):
    st = os.stat(snapshot)
    tag = f"{os.path.basename(snapshot)}-{int(st.st_mtime)}-{st.st_size}"
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), f".restored-{tag}")


def _count_restaurants(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
    finally:
        conn.close()


def restore_if_requested(db_path=None) -> dict:
    """Swap the snapshot named by RESTORE_FROM into place, once.

    Refuses (and raises, so the deploy fails rather than boots on the wrong
    file) when the snapshot is missing, fails integrity_check or has no
    restaurants table. Returns what happened."""
    snapshot = requested()
    if not snapshot:
        return {"restored": False, "reason": "not requested"}
    from models import DB_PATH
    db_path = db_path or DB_PATH
    if not os.path.isfile(snapshot):
        raise RuntimeError(f"RESTORE_FROM={snapshot} does not exist; nothing was changed")
    marker = _marker_path(db_path, snapshot)
    if os.path.exists(marker):
        log.warning("RESTORE_FROM is still set, but %s was already restored; leaving the database alone. "
                    "Remove RESTORE_FROM to restart the scheduler.", snapshot)
        return {"restored": False, "reason": "already restored", "snapshot": snapshot}

    conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    if ok != "ok" or "restaurants" not in tables:
        raise RuntimeError(f"RESTORE_FROM={snapshot} failed its checks (integrity {ok!r}, "
                           f"restaurants table {'present' if 'restaurants' in tables else 'missing'}); nothing was changed")
    expected = _count_restaurants(snapshot)

    stamp = int(time.time())
    if os.path.exists(db_path):
        # Kept as evidence; it may still be partially readable.
        os.replace(db_path, f"{db_path}.broken-{stamp}")
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.replace(db_path + suffix, f"{db_path}{suffix}.broken-{stamp}")
    tmp = db_path + ".restoring"
    shutil.copyfile(snapshot, tmp)
    os.replace(tmp, db_path)

    got = _count_restaurants(db_path)
    if got < expected:
        raise RuntimeError(f"restore of {snapshot} came back with {got} restaurants, expected {expected}")
    open(marker, "w").close()
    log.warning("Restored %s over %s (%d restaurants). Remove RESTORE_FROM once confirmed.",
                snapshot, db_path, got)
    return {"restored": True, "snapshot": snapshot, "restaurants": got}
