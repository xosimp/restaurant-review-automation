"""
marker_store.py — the few small files the platform keeps OUTSIDE its database,
wherever they can survive (release-safety audit 10/26/26, P3 "retire the app
volume").

Two guards depend on a record that a database restore cannot rewind and an
emptied database cannot lose:

  .cavnar-volume.json  status_manager's "this platform has held client data"
                       marker: an empty database is refused at boot (#108).
  .restore_epoch /     restore_guard's copy of the scheduler's tick counter,
  .restore_freeze      and the freeze it writes when the database went back
                       in time (R8-04).

They lived beside the SQLite file on the Railway volume. With
VOLUME_OPTIONAL=1 and no volume mounted (the database is Postgres; the volume
held only these, the local backup snapshots and the frozen pre-cutover
reviews.db), they live in object storage instead: the blob bucket when one is
configured (BLOB_S3_*, which keeps what it holds), else the backup bucket
(BACKUP_S3_*, whose lifecycle rule expires an object no write has refreshed —
the epoch is rewritten every tick and the volume marker hourly; a freeze left
unconfirmed past the rule's days lapses), under MARKER_S3_PREFIX
("cavnar-markers/"). Never Postgres: a point-in-time restore would rewind the
copy with the database it is meant to catch.

    backend()  "volume" (a volume is mounted: as before), "object"
               (VOLUME_OPTIONAL with no volume), or "local" (a laptop, a test:
               the directory beside the database, as before).

Production with its volume attached and no new variable is "volume": nothing
changes until Will detaches it (docs/ops/RECOVERY.md → Retiring the volume).

A read that FAILS is not "no marker" (re-audit B7 P3, 10/27/26: an object-
storage 5xx or timeout read as absent — the restore freeze as lifted, the
epoch as missing). read(name, strict=True) raises MarkerUnreadable for
anything but a clean "there is none" (a 404, a missing file), and each
marker takes its safe side:

  .restore_freeze      unreadable → as last seen in this process; never seen
                       → FROZEN (exists(when_unreadable=True), config.
                       restore_frozen): a send the freeze should have held
                       cannot be taken back, one it held for a minute can.
  .restore_epoch       unreadable → restore_guard neither compares nor writes
                       it that tick: writing over an epoch it could not read
                       could erase the evidence of a restore. An unreadable
                       freeze record holds the tick (state "frozen").
  .cavnar-volume.json  unreadable → treated as present (it says client data
                       existed): an EMPTY database is refused at boot rather
                       than let through; a database holding clients boots as
                       ever, and the marker is not rewritten over a record it
                       could not read (status_manager's guards, read with
                       strict=True).
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("marker_store")

DEFAULT_PREFIX = "cavnar-markers/"
EXISTS_TTL_SECONDS = 30.0
_exists_cache = {}
# The last answer each marker's store gave cleanly in this process: what an
# unreadable read falls back to (module docstring).
_last_known = {}
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("marker_store._exists_cache", _exists_cache)
_process_caches.register("marker_store._last_known", _last_known)


class MarkerUnreadable(RuntimeError):
    """The marker's store answered with something other than "there is
    none": an object-storage error that is not a 404, a timeout, no bucket
    configured where one is needed, a file that cannot be read or parsed."""
_cache_lock = threading.Lock()


def _volume_mount() -> str:
    return (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()


def backend() -> str:
    import config
    if _volume_mount():
        return "volume"
    if config.volume_optional() and config.on_railway():
        return "object"
    return "local"


def durable() -> bool:
    """Whether a marker written now outlives this container."""
    return backend() in ("volume", "object")


def object_config():
    """The bucket markers go to: the blob bucket, else the backup bucket,
    with MARKER_S3_PREFIX. None when neither is configured."""
    import blob_store
    import offsite_backup
    cfg = blob_store.s3_config() or offsite_backup.s3_config()
    if not cfg:
        return None
    return dict(cfg, prefix=os.getenv("MARKER_S3_PREFIX", DEFAULT_PREFIX))


def directory() -> str:
    """The directory a volume or local marker lives in: beside the database."""
    import models
    return os.path.dirname(os.path.abspath(models.DB_PATH)) or "."


def _path(name):
    return os.path.join(directory(), name)


def read(name, strict=False):
    """The marker's JSON (a dict), or None when there is none. A read that
    fails (MarkerUnreadable's cases) is None too — unless `strict`, when it
    raises MarkerUnreadable: a guard reads strictly and takes its marker's
    safe side (module docstring)."""
    def _unreadable(why):
        log.warning("marker %s unreadable: %s", name, why)
        if strict:
            raise MarkerUnreadable(f"marker {name}: {why}")
        return None
    if backend() == "object":
        cfg = object_config()
        if cfg is None:
            return _unreadable("no object storage configured (BLOB_S3_* or BACKUP_S3_*)")
        import offsite_backup
        try:
            data = json.loads(offsite_backup.get_bytes(name, cfg).decode("utf-8"))
        except offsite_backup.OffsiteError as e:
            if "404" in str(e):
                return None
            return _unreadable(e)
        except (ValueError, UnicodeDecodeError) as e:
            return _unreadable(e)
        return data if isinstance(data, dict) else _unreadable("not a JSON object")
    return read_file(_path(name), strict=strict)


def read_file(path, strict=False):
    """A marker file's JSON (a dict); None when the file is not there. A
    file that is there but cannot be read or parsed is None too — unless
    `strict` (MarkerUnreadable)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("marker %s unreadable: %s", path, e)
        if strict:
            raise MarkerUnreadable(f"marker {path}: {e}")
        return None
    if isinstance(data, dict):
        return data
    if strict:
        raise MarkerUnreadable(f"marker {path}: not a JSON object")
    return None


def write(name, data) -> None:
    """Write the marker (atomic on a file system). Raises on failure — each
    caller decides whether a missed write matters."""
    _forget(name)
    if backend() == "object":
        cfg = object_config()
        if cfg is None:
            raise RuntimeError(f"marker {name}: no object storage configured (BLOB_S3_* or BACKUP_S3_*)")
        import offsite_backup
        offsite_backup.put_bytes(name, json.dumps(data).encode("utf-8"), cfg, content_type="application/json")
        return
    path = _path(name)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def remove(name) -> None:
    _forget(name)
    if backend() == "object":
        cfg = object_config()
        if cfg is None:
            return
        import offsite_backup
        offsite_backup.delete_object(name, cfg)
        return
    try:
        os.remove(_path(name))
    except FileNotFoundError:
        pass


def exists(name, ttl=EXISTS_TTL_SECONDS, when_unreadable=True) -> bool:
    """Whether the marker is there. In object storage, answered from a cache
    at most `ttl` seconds old: config.restore_frozen() asks on every gate
    check. A read that fails answers what this process last read cleanly,
    else `when_unreadable` (True: the freeze's safe side — fail closed)."""
    if backend() != "object":
        return os.path.exists(_path(name))
    now = time.monotonic()
    with _cache_lock:
        hit = _exists_cache.get(name)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    try:
        found = read(name, strict=True) is not None
        with _cache_lock:
            _last_known[name] = found
    except MarkerUnreadable as e:
        with _cache_lock:
            last = _last_known.get(name)
        found = last if last is not None else bool(when_unreadable)
        log.error("marker %s unreadable — answering %s (%s): %s", name, found,
                  "as last read" if last is not None else "its safe side", e)
    with _cache_lock:
        _exists_cache[name] = (now, found)
    return found


def _forget(name):
    with _cache_lock:
        _exists_cache.pop(name, None)
