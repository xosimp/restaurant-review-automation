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
"""
import json
import logging
import os
import threading
import time

log = logging.getLogger("marker_store")

DEFAULT_PREFIX = "cavnar-markers/"
EXISTS_TTL_SECONDS = 30.0
_UNREADABLE = object()          # exists()'s cached "the store did not answer"
_exists_cache = {}
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("marker_store._exists_cache", _exists_cache)
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


class MarkerUnreadable(RuntimeError):
    """The object store answered with an error other than "not there" (or
    no bucket is configured): the marker's presence is unknown."""


def read(name, strict=False):
    """The marker's JSON (a dict), or None when there is none or it cannot be
    read. `strict` (the guards — blind re-audit 10/27/26 B5 P3): an
    object-storage error other than a 404, or no bucket configured, raises
    MarkerUnreadable instead of reading as "no marker", so the caller can
    fail closed."""
    if backend() == "object":
        cfg = object_config()
        if cfg is None:
            if strict:
                raise MarkerUnreadable(f"marker {name}: no object storage configured (BLOB_S3_* or BACKUP_S3_*)")
            return None
        import offsite_backup
        try:
            data = json.loads(offsite_backup.get_bytes(name, cfg).decode("utf-8"))
        except offsite_backup.OffsiteError as e:
            if "404" not in str(e):
                log.warning("marker %s unreadable: %s", name, e)
                if strict:
                    raise MarkerUnreadable(f"marker {name}: {e}") from e
            return None
        except (ValueError, UnicodeDecodeError) as e:
            log.warning("marker %s unreadable: %s", name, e)
            if strict:
                raise MarkerUnreadable(f"marker {name}: {e}") from e
            return None
        except Exception as e:           # a network error below offsite_backup's own
            if not strict:
                raise
            log.warning("marker %s unreadable: %s", name, e)
            raise MarkerUnreadable(f"marker {name}: {e}") from e
        return data if isinstance(data, dict) else None
    try:
        with open(_path(name), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
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


def exists(name, ttl=EXISTS_TTL_SECONDS, strict=False) -> bool:
    """Whether the marker is there. In object storage, answered from a cache
    at most `ttl` seconds old: config.restore_frozen() asks on every gate
    check. `strict`: an unreadable store raises MarkerUnreadable (and that
    answer is cached for `ttl` too, so a failing store is not asked on every
    gate check)."""
    if backend() != "object":
        return os.path.exists(_path(name))
    now = time.monotonic()
    with _cache_lock:
        hit = _exists_cache.get(name)
        if hit is not None and now - hit[0] < ttl:
            if hit[1] is _UNREADABLE:
                if strict:
                    raise MarkerUnreadable(f"marker {name}: object storage unreadable (cached)")
            else:
                return hit[1]
    try:
        found = read(name, strict=strict) is not None
    except MarkerUnreadable:
        with _cache_lock:
            _exists_cache[name] = (now, _UNREADABLE)
        raise
    with _cache_lock:
        _exists_cache[name] = (now, found)
    return found


def _forget(name):
    with _cache_lock:
        _exists_cache.pop(name, None)
