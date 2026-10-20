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

A local snapshot is gzipped since 10/7/26 (cavnar_ai_backup_<date>.db.gz,
AI cost audit #8): RESTORE_FROM takes it as it is — decompressed beside the
database first, then checked and swapped exactly like a plain one, which
still works for a snapshot from before the change.
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


def _is_gzip(path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


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
    # A restore prepared out of band (prepare(), scripts/prepare_restore.py)
    # was decompressed, decrypted, checked and unsealed while the old
    # process still served: the boot only renames it into place (OPS-3).
    record = _prepared_record(snapshot)
    if record is not None:
        return _swap_prepared(snapshot, record, db_path)
    if not os.path.isfile(snapshot):
        raise RuntimeError(f"RESTORE_FROM={snapshot} does not exist; nothing was changed")
    marker = _marker_path(db_path, snapshot)
    if os.path.exists(marker):
        log.warning("RESTORE_FROM is still set, but %s was already restored; leaving the database alone. "
                    "Remove RESTORE_FROM to restart the scheduler.", snapshot)
        return {"restored": False, "reason": "already restored", "snapshot": snapshot}

    tmp = db_path + ".restoring"
    # A gzipped snapshot is decompressed beside the database first — into
    # the same temporary name the swap below uses — and everything after
    # reads that file. Nothing live is touched until it has passed.
    gz = _is_gzip(snapshot)
    source = snapshot
    if gz:
        import gzip
        with gzip.open(snapshot, "rb") as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out, 3 * 1024 * 1024)
        source = tmp
    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        if ok != "ok" or "restaurants" not in tables:
            raise RuntimeError(f"RESTORE_FROM={snapshot} failed its checks (integrity {ok!r}, "
                               f"restaurants table {'present' if 'restaurants' in tables else 'missing'}); "
                               "nothing was changed")
        expected = _count_restaurants(source)
    except Exception:
        if gz:
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise

    # Every commit in the files themselves before they move: pooled
    # connections (models.get_conn, 10/3/26) keep the write-ahead log open,
    # so a copy of the main file alone could miss the newest writes.
    import models as _models
    if not gz:
        _models.checkpoint(snapshot)
    if os.path.exists(db_path):
        _models.checkpoint(db_path)
    stamp = int(time.time())
    if os.path.exists(db_path):
        # Kept as evidence; it may still be partially readable.
        os.replace(db_path, f"{db_path}.broken-{stamp}")
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.replace(db_path + suffix, f"{db_path}{suffix}.broken-{stamp}")
    if not gz:
        shutil.copyfile(snapshot, tmp)
    # A decrypted off-site copy carries its credentials and link secrets
    # SEALED under CREDENTIAL_KEY (offsite_backup, scale audit OPS-1): opened
    # here, in the file, before anything serves from it. A local snapshot
    # holds none, so this is a few empty reads.
    unsealed = _unseal(tmp)
    os.replace(tmp, db_path)

    got = _count_restaurants(db_path)
    if got < expected:
        raise RuntimeError(f"restore of {snapshot} came back with {got} restaurants, expected {expected}")
    # The marker under the snapshot's name as the NEXT boot will see it too:
    # the checkpoint above can rewrite the snapshot's main file, and a new
    # mtime second gave the next boot a marker name nobody wrote - it
    # restored again over every write since (found 10/20/26: the full
    # suite's load moved the checkpoint across a second).
    open(marker, "w").close()
    if os.path.isfile(snapshot):
        open(_marker_path(db_path, snapshot), "w").close()
    log.warning("Restored %s over %s (%d restaurants). Remove RESTORE_FROM once confirmed.",
                snapshot, db_path, got)
    return {"restored": True, "snapshot": snapshot, "restaurants": got, "unsealed": unsealed}


# ── a restore prepared out of band (scale audit 10/19/26 OPS-3) ─────────────
#
# A RESTORE_FROM boot decompresses, integrity-checks and copies the snapshot
# before gunicorn binds, under Railway's 120-second healthcheck: past a few
# GB that no longer fits, and with a volume attached a failed deploy is
# downtime until a rollback. prepare() does all of it beforehand — over
# `railway ssh` while the old process still serves (it reads the snapshot and
# writes one new file beside the database; it never touches the live one) —
# and leaves a record beside the file. A boot whose RESTORE_FROM names a
# prepared file whose size and mtime still match the record only renames it
# into place: seconds, whatever the size. The monthly drill's stage timings
# (scheduler.run_restore_drill) say when a restore needs this.

_PREPARED_SUFFIX = ".prepared.json"


def _prepared_path(path):
    return path + _PREPARED_SUFFIX


def _stat_tag(path):
    st = os.stat(path)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def prepare(snapshot, out_path, encryption_key=None) -> dict:
    """Make `out_path` a checked, unsealed, ready-to-rename copy of
    `snapshot` (a gzipped local snapshot, a plain .db, or — with
    `encryption_key` — an off-site .enc file). Raises on any failed check and
    leaves nothing behind; returns the record it wrote."""
    import json as _json
    if not os.path.isfile(snapshot):
        raise RuntimeError(f"{snapshot} does not exist")
    if os.path.exists(out_path):
        raise RuntimeError(f"{out_path} already exists; remove it or choose another path")
    work = out_path + ".partial"
    started = time.monotonic()
    stages = {}
    try:
        if encryption_key:
            import scheduler
            scheduler._decrypt_file_chunked(snapshot, work, encryption_key)
        elif _is_gzip(snapshot):
            import gzip
            with gzip.open(snapshot, "rb") as src, open(work, "wb") as out:
                shutil.copyfileobj(src, out, 3 * 1024 * 1024)
        else:
            shutil.copyfile(snapshot, work)
        stages["copy"] = round(time.monotonic() - started, 1)
        conn = sqlite3.connect(f"file:{work}?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        if ok != "ok" or "restaurants" not in tables:
            raise RuntimeError(f"{snapshot} failed its checks (integrity {ok!r}, restaurants table "
                               f"{'present' if 'restaurants' in tables else 'missing'})")
        stages["integrity"] = round(time.monotonic() - started - sum(stages.values()), 1)
        unsealed = _unseal(work)
        os.replace(work, out_path)
    except Exception:
        for p in (work, work + "-journal"):
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    record = dict(_stat_tag(out_path), source=os.path.basename(snapshot), integrity="ok",
                  restaurants=_count_restaurants(out_path), unsealed=unsealed.get("unsealed"),
                  unseal_failed=unsealed.get("failed"), prepared_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                  seconds=round(time.monotonic() - started, 1), stages=stages)
    with open(_prepared_path(out_path), "w") as f:
        _json.dump(record, f)
    return record


def _prepared_record(snapshot):
    """The record prepare() left for this path, or None."""
    import json as _json
    try:
        with open(_prepared_path(snapshot)) as f:
            rec = _json.load(f)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("integrity") == "ok" else None


def _swap_prepared(snapshot, record, db_path) -> dict:
    tag = f"{os.path.basename(snapshot)}-{record.get('mtime_ns')}-{record.get('size')}"
    marker = os.path.join(os.path.dirname(os.path.abspath(db_path)), f".restored-prepared-{tag}")
    if os.path.exists(marker):
        log.warning("RESTORE_FROM is still set, but the prepared %s was already restored; leaving the database "
                    "alone. Remove RESTORE_FROM to restart the scheduler.", snapshot)
        return {"restored": False, "reason": "already restored", "snapshot": snapshot}
    if not os.path.isfile(snapshot):
        raise RuntimeError(f"RESTORE_FROM={snapshot} does not exist; nothing was changed")
    now = _stat_tag(snapshot)
    if now["size"] != record.get("size") or now["mtime_ns"] != record.get("mtime_ns"):
        raise RuntimeError(f"RESTORE_FROM={snapshot} changed after it was prepared (size or mtime differ from "
                           f"{_prepared_path(snapshot)}); prepare it again. Nothing was changed")
    if os.stat(snapshot).st_dev != os.stat(os.path.dirname(os.path.abspath(db_path))).st_dev:
        raise RuntimeError(f"RESTORE_FROM={snapshot} is not on the database's volume; a prepared restore is a "
                           "rename. Prepare it beside the database. Nothing was changed")
    import models as _models
    if os.path.exists(db_path):
        _models.checkpoint(db_path)
    stamp = int(time.time())
    if os.path.exists(db_path):
        os.replace(db_path, f"{db_path}.broken-{stamp}")
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            os.replace(db_path + suffix, f"{db_path}{suffix}.broken-{stamp}")
    os.replace(snapshot, db_path)
    got = _count_restaurants(db_path)
    expected = int(record.get("restaurants") or 0)
    if got < expected:
        raise RuntimeError(f"prepared restore of {snapshot} came back with {got} restaurants, expected {expected}")
    open(marker, "w").close()
    log.warning("Restored the prepared %s over %s by rename (%d restaurants). Remove RESTORE_FROM once confirmed.",
                snapshot, db_path, got)
    return {"restored": True, "snapshot": snapshot, "restaurants": got, "prepared": True}


def _unseal(path) -> dict:
    """offsite_backup.unseal_file, logged; never fails the restore (a value
    it cannot open is blanked — the scrubbed state, re-entered by hand)."""
    try:
        import offsite_backup
        out = offsite_backup.unseal_file(path)
    except Exception as e:
        log.error("restore: unsealing the off-site copy's credentials failed: %s", e)
        return {"unsealed": 0, "failed": None, "error": str(e)[:200]}
    if out.get("unsealed") or out.get("failed"):
        log.warning("restore: unsealed %d value(s) from an off-site copy (%d could not be opened): %s",
                    out["unsealed"], out["failed"], out["columns"])
    return out
