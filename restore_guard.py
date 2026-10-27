"""restore_guard.py — a database that went back in time stops the platform's
sends until a person says otherwise (scale re-audit 10/20/26 R8-04).

A Railway restore of the Postgres service (a volume backup from the Backups
tab, or point-in-time recovery) swaps the database under a RUNNING web
service. Nothing in the app knew: the scheduler's day claims, the owed
sends, the queued jobs all went back to the restore point, so the morning's
briefs, digests and DSR deliveries went out a second time, owed welcomes
and receipts were drained again, and a queued price push re-applied an older
price to RPOWER. RESTORE_FROM (db_restore) stands the scheduler down, but
that path restores the SQLite file only.

How it notices: the scheduler's tick (only the lease holder ticks) advances
a counter kept IN THE DATABASE (job_cursors 'restore_epoch', "<database
id>:<seq>") and then copies it to a file on the APP volume
(`.restore_epoch` beside models.DB_PATH — the volume survives deploys and is
not part of a database restore). A database whose counter is BEHIND the
file's for the same database id was put back to an earlier point. The guard
then writes `.restore_freeze` beside it, and while that file exists:

  * config.scheduling_allowed() is False — no scheduled job, no admin send,
    no billing send (the same gate RESTORE_FROM and MAINTENANCE_READ_ONLY
    use);
  * job_queue.claim() claims nothing (job_queue.frozen);
  * the operator is paged once an hour (ops.page_operator "restore_detected").

A person reconciles (docs/ops/RECOVERY.md -> Database recovery -> After a
Postgres restore) and then confirms on the server:

    python3 -m restore_guard confirm

which re-baselines the volume's copy to the database's counter and removes
the freeze. The ordering of the two writes in a tick (database first, then
the file) means a crash between them leaves the file BEHIND the database,
which reads as normal — never a false freeze. A database with no counter yet
(this guard's first tick, a fresh test database, a restore to before the
guard existed) is baselined, not frozen; a different database id (a new
database) is baselined too.

Active on Railway with its volume mounted (config.on_railway() and
RAILWAY_VOLUME_MOUNT_PATH), or with RESTORE_GUARD=1; RESTORE_GUARD=0 turns it off (the escape hatch if it ever
misfires — say so in the incident notes). A local run never writes either
file.
"""
import json
import logging
import os
import sys
import time
import uuid

log = logging.getLogger("restore_guard")

CURSOR_KEY = "restore_epoch"
EPOCH_FILE = ".restore_epoch"
FREEZE_FILE = ".restore_freeze"


def active() -> bool:
    flag = (os.getenv("RESTORE_GUARD") or "").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return False
    if flag in ("1", "true", "on", "yes"):
        return True
    # Production: Railway, with its volume mounted (models.require_volume
    # refuses a boot without one) — or with no volume on purpose
    # (VOLUME_OPTIONAL: its files live in object storage, marker_store) —
    # never a laptop or a test's process.
    import config
    import marker_store
    return config.on_railway() and (bool((os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip())
                                    or marker_store.backend() == "object")


def _dir():
    import models
    return os.path.dirname(os.path.abspath(models.DB_PATH)) or "."


def epoch_path():
    return os.path.join(_dir(), EPOCH_FILE)


def freeze_path():
    return os.path.join(_dir(), FREEZE_FILE)


def _object_backed():
    """No volume, on purpose: the epoch and the freeze live in object
    storage (marker_store — release-safety audit 10/26/26, P3)."""
    try:
        import marker_store
        return marker_store.backend() == "object"
    except Exception as e:
        log.warning("restore guard: marker backend unknown, using the volume path: %s", e)
        return False


def _remove(path):
    if _object_backed():
        import marker_store
        marker_store.remove(os.path.basename(path))
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _read_json(path):
    if _object_backed():
        import marker_store
        return marker_store.read(os.path.basename(path))
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    if _object_backed():
        import marker_store
        marker_store.write(os.path.basename(path), data)
        return
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _parse(value):
    """(database id, seq) from job_cursors' "<id>:<seq>", or (None, 0)."""
    try:
        db_id, seq = str(value).rsplit(":", 1)
        return (db_id or None), int(seq)
    except (TypeError, ValueError):
        return None, 0


def frozen() -> dict:
    """The freeze record (detected_at, db_seq, volume_seq, db_id), or None."""
    if _object_backed():
        return _read_json(freeze_path())
    return _read_json(freeze_path()) if os.path.exists(freeze_path()) else None


def _db_value(conn):
    row = conn.execute("SELECT value FROM job_cursors WHERE key=?", (CURSOR_KEY,)).fetchone()
    return row[0] if row else None


def check_and_stamp(db_path=None) -> dict:
    """One tick's look: {state: off | frozen | detected | ok, ...}. `detected`
    is the tick that found the database behind the volume (it writes the
    freeze and pages); `frozen` every tick after it until confirm(). Called
    by scheduler_loop at the top of each tick, before any job. Never raises
    for a read or write it could not make (state 'error' — the tick goes on:
    a guard that cannot read is no reason to stop the platform)."""
    if not active():
        return {"state": "off"}
    held = frozen()
    if held is not None:
        return dict(held, state="frozen")
    try:
        from models import get_conn
        conn = get_conn(db_path) if db_path else get_conn()
        try:
            db_id, db_seq = _parse(_db_value(conn))
            mark = _read_json(epoch_path()) or {}
            vol_id, vol_seq = mark.get("db_id"), int(mark.get("seq") or 0)
            if db_id and vol_id == db_id and db_seq < vol_seq:
                record = {"detected_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "db_id": db_id,
                          "db_seq": db_seq, "volume_seq": vol_seq}
                _write_json(freeze_path(), record)
                log.error("restore guard: the database's tick counter (%s) is behind the app volume's (%s) - "
                          "the database was restored to an earlier point. Scheduling and the job queue are "
                          "frozen until `python3 -m restore_guard confirm`.", db_seq, vol_seq)
                _page(record)
                return dict(record, state="detected")
            db_id = db_id or uuid.uuid4().hex[:16]
            new = int(db_seq) + 1
            conn.execute("INSERT INTO job_cursors (key, value, updated_at) VALUES (?,?,datetime('now')) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (CURSOR_KEY, f"{db_id}:{new}"))
            conn.commit()
        finally:
            conn.close()
        _write_json(epoch_path(), {"db_id": db_id, "seq": new, "at": time.time()})
        return {"state": "ok", "db_id": db_id, "seq": new}
    except Exception as e:
        log.warning("restore guard: tick not stamped: %s", e)
        return {"state": "error", "error": str(e)[:200]}


def _page(record):
    try:
        import ops
        ops.page_operator("restore_detected", "Cavnar AI: the database was restored under the running service",
                          [f"The database's tick counter ({record['db_seq']}) is behind the app volume's "
                           f"({record['volume_seq']}): it was put back to an earlier point (a Railway Backups "
                           "restore or point-in-time recovery).",
                           "Scheduled jobs, admin sends and the job queue are frozen so nothing is sent twice.",
                           "Reconcile (docs/ops/RECOVERY.md -> After a Postgres restore), then run "
                           "`python3 -m restore_guard confirm` on the service."],
                          cooldown_minutes=60)
    except Exception as e:
        log.error("restore guard: page failed: %s", e)


def confirm(actor="operator", db_path=None) -> dict:
    """A person reconciled the restored database: the volume's copy of the
    counter is set to the database's and the freeze removed. Returns what
    it did."""
    held = frozen()
    from models import get_conn
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        db_id, db_seq = _parse(_db_value(conn))
    finally:
        conn.close()
    if db_id:
        _write_json(epoch_path(), {"db_id": db_id, "seq": db_seq, "at": time.time(), "confirmed_by": actor})
    else:
        _remove(epoch_path())
    _remove(freeze_path())
    log.warning("restore guard: confirmed by %s (database counter %s); scheduling resumes on the next tick",
                actor, db_seq)
    return {"ok": True, "was_frozen": held is not None, "db_seq": db_seq, "freeze": held}


def forget_baseline() -> None:
    """Drop the volume's copy (db_restore after a RESTORE_FROM swap: that
    path is itself the reviewed restore, so its older counter must not read
    as a second, unreviewed one). The next tick baselines again."""
    for p in (epoch_path(),):
        try:
            _remove(p)
        except OSError as e:
            log.warning("restore guard: %s not removed: %s", p, e)


def status() -> dict:
    """For the console and the runbook: {active, frozen, volume}."""
    return {"active": active(), "frozen": frozen(), "volume": _read_json(epoch_path())}


def _main(argv):
    if len(argv) >= 2 and argv[1] == "confirm":
        sys.stdout.write(json.dumps(confirm(actor=(argv[2] if len(argv) > 2 else os.getenv("USER") or "operator")),
                                    default=str) + "\n")
        return 0
    sys.stdout.write(json.dumps(status(), default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
