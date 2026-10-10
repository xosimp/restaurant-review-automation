"""
blob_store.py — large blobs out of the database (scheduler audit 10/10/26:
RM-14, ARCH-34, SCALE-20; scale.md "Model request and answer blobs go to
S3/R2 … the database keeps the pointer").

Every schedule model call keeps its whole request, inputs and answer
(schedule_model_calls request_z / inputs_z / shared_z / answer_z, 90 days),
and every week's Shift Quality evaluation is a 100-440 KB document
(quality_blobs.body). On one SQLite file on a 5 GB volume — where the nightly
backup needs two to three times the database's size free beside it — those
blobs are most of the growth. Here they can live in object storage, the
column keeping a pointer:

  BLOB_STORE=db     (the default) — inline, as before; nothing moves.
  BLOB_STORE=s3     — an S3-compatible bucket of its own (Cloudflare R2, S3,
                      B2, MinIO): BLOB_S3_ENDPOINT (https), BLOB_S3_BUCKET,
                      BLOB_S3_ACCESS_KEY_ID, BLOB_S3_SECRET_ACCESS_KEY,
                      BLOB_S3_REGION ("auto"), BLOB_S3_PREFIX ("cavnar-blobs/").
                      Never the backup bucket: its lifecycle rule expires
                      what it holds. Signed with offsite_backup's SigV4.
  BLOB_STORE=local  — files under BLOB_STORE_DIR (a development fallback; on
                      Railway that is the same volume, and the SQLite backup
                      does not carry them — say so before using it there).

A stored value is the blob itself, or POINTER + "<backend>:<key>" —
resolve() reads either, so every reader works across the move and the
old inline rows. New writes go to the store as they are made
(schedule_output.record_call, schedule_versions.store_quality); offload()
moves old inline rows in bounded chunks (the `blob_offload` job). A
quality blob the prune collects is deleted from the store with it;
model-call blobs are expired by the bucket's own lifecycle rule on
"model-calls/" (set it to the table's retention, 90 days, plus a margin).
"""
import os
import time
import uuid

POINTER = b"cavnar-blob:"
KINDS = ("model-calls", "quality")


def backend() -> str:
    v = (os.getenv("BLOB_STORE") or "db").strip().lower()
    return v if v in ("db", "s3", "local") else "db"


def s3_config():
    """The blob bucket's settings, or None (https only, its own bucket)."""
    cfg = {
        "endpoint": (os.getenv("BLOB_S3_ENDPOINT") or "").strip().rstrip("/"),
        "bucket": (os.getenv("BLOB_S3_BUCKET") or "").strip(),
        "access_key": (os.getenv("BLOB_S3_ACCESS_KEY_ID") or "").strip(),
        "secret_key": (os.getenv("BLOB_S3_SECRET_ACCESS_KEY") or "").strip(),
        "region": (os.getenv("BLOB_S3_REGION") or "auto").strip() or "auto",
        "prefix": os.getenv("BLOB_S3_PREFIX", "cavnar-blobs/"),
    }
    if not all(cfg[k] for k in ("endpoint", "bucket", "access_key", "secret_key")):
        return None
    if not cfg["endpoint"].startswith("https://"):
        return None
    if cfg["bucket"] == (os.getenv("BACKUP_S3_BUCKET") or "").strip():
        return None                     # never the backup bucket (its lifecycle expires objects)
    return cfg


def _local_dir():
    return os.getenv("BLOB_STORE_DIR") or os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "blobs")


def active() -> str:
    """The backend new writes go to: "db" when the configured one cannot be
    used (no bucket settings)."""
    b = backend()
    if b == "s3" and s3_config() is None:
        return "db"
    return b


def is_pointer(value) -> bool:
    return isinstance(value, (bytes, bytearray, memoryview)) and bytes(value[:len(POINTER)]) == POINTER


def store(kind, key, data):
    """Keep `data` (bytes) under `kind`/`key`; returns what the column holds:
    the bytes themselves (BLOB_STORE=db, or the store failed — never lose a
    blob over the store), or the pointer."""
    if data is None:
        return None
    b = active()
    if b == "db":
        return data
    full = f"{kind}/{key}"
    try:
        if b == "s3":
            import offsite_backup
            offsite_backup.put_bytes(full, data, s3_config())
        else:
            path = os.path.join(_local_dir(), full)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + f".{uuid.uuid4().hex[:6]}.partial"
            with open(tmp, "wb") as f:
                f.write(bytes(data))
            os.replace(tmp, path)
        return POINTER + f"{b}:{full}".encode("utf-8")
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="blob_store", context=f"{kind} put kept inline")
        except Exception:
            pass
        return data


def resolve(value):
    """The blob a column holds: itself, or what its pointer names."""
    if not is_pointer(value):
        return value
    ref = bytes(value[len(POINTER):]).decode("utf-8")
    b, _sep, full = ref.partition(":")
    if b == "s3":
        import offsite_backup
        cfg = s3_config()
        if cfg is None:
            raise RuntimeError("a blob is in object storage but BLOB_S3_* is not configured")
        return offsite_backup.get_bytes(full, cfg)
    with open(os.path.join(_local_dir(), full), "rb") as f:
        return f.read()


def delete(value) -> bool:
    """Remove the object a pointer names (an inline value: nothing to do)."""
    if not is_pointer(value):
        return False
    ref = bytes(value[len(POINTER):]).decode("utf-8")
    b, _sep, full = ref.partition(":")
    try:
        if b == "s3":
            import offsite_backup
            cfg = s3_config()
            return bool(cfg) and offsite_backup.delete_object(full, cfg)
        os.unlink(os.path.join(_local_dir(), full))
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


# What offload() moves: (table, key column, blob columns, the kind, the age).
_OFFLOAD = (
    ("schedule_model_calls", "id", ("request_z", "inputs_z", "shared_z", "answer_z"), "model-calls", 1),
    ("quality_blobs", "hash", ("body",), "quality", 0),
)
OFFLOAD_CHUNK = 200


def offload(max_seconds=300, db_path=None) -> dict:
    """Move inline blobs to the store, oldest first, in chunks (the
    `blob_offload` job; nothing while BLOB_STORE=db). Resumable: a row
    already holding a pointer is not read again. Standard counts."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    if active() == "db":
        out["skipped"] = 1
        return out
    import models
    stop = time.monotonic() + float(max_seconds)
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        for table, key_col, cols, kind, age_days in _OFFLOAD:
            for col in cols:
                while time.monotonic() < stop:
                    rows = conn.execute(
                        f"SELECT {key_col} AS k, {col} AS v FROM {table} WHERE {col} IS NOT NULL "
                        f"AND substr({col}, 1, {len(POINTER)}) <> ? AND created_at < datetime('now', ?) LIMIT ?",
                        (POINTER, f"-{int(age_days)} days", OFFLOAD_CHUNK)).fetchall()
                    if not rows:
                        break
                    moved = 0
                    for r in rows:
                        out["attempted"] += 1
                        ptr = store(kind, f"{table}/{r['k']}/{col}", bytes(r["v"]))
                        if is_pointer(ptr):
                            conn.execute(f"UPDATE {table} SET {col}=? WHERE {key_col}=?", (ptr, r["k"]))
                            out["ok"] += 1
                            moved += 1
                        else:
                            out["failed"] += 1
                    conn.commit()
                    if not moved:
                        break
                else:
                    out["hit_bound"] = True
                    return out
        return out
    finally:
        conn.close()
