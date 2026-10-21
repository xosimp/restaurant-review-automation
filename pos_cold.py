"""pos_cold — the sale lines' cold tier (RPOWER audit #23 A5-09, F5).

Every RPOWER sale line was kept hot in Postgres for three years
(RETAIN_POS_LINES_DAYS, 1,095): ~0.8 MB a store-day of the ~1.25 MB the
platform writes, 840 GB at 1,500 stores in the first year (MEASURED rates,
10/21/26) — past any single volume and a nightly export that no longer fits
a night. The lines are the one table nothing reads far back: every owner
view of an old month reads its rollups (`pos_item_daily`, kept three years,
the tickets, payments and punches, the DSR's figures), and the line readers
read at most service_performance.MAX_DAYS — inside the 400-day floor.

So, nightly inside the retention pass (ops.prune_ledgers -> `tier`):

  * a restaurant-month of `pos_ticket_lines` wholly older than HOT_DAYS
    (RETAIN_POS_LINES_HOT_DAYS, default 400 — a year and a month, so a
    year-over-year line read still finds last year's month hot) is exported
    to object storage: gzip JSON lines, Fernet-encrypted under
    BACKUP_ENCRYPTION_KEY like every off-site copy, through blob_store's
    bucket (BLOB_STORE=s3: BLOB_S3_*, never the backup bucket whose
    lifecycle expires objects) under
    "pos-cold/pos-lines/<restaurant>/<provider>/<YYYY-MM>-<sha12>.jsonl.gz.enc";
  * the object is read back and its SHA-256 and row count checked, the
    export recorded in `pos_cold_exports` (kept for good: the index of the
    cold tier), and only then the month's lines are deleted — never a line
    whose month has no verified export, never a month whose days are not
    rolled into pos_item_daily yet, never while the month is held by a
    restore;
  * with no object store or no BACKUP_ENCRYPTION_KEY nothing is exported and
    NOTHING is deleted: the lines stay hot (the 1,095-day window still
    applies), and the pass says so.

`restore(restaurant_id, month)` puts a month back (INSERT OR IGNORE, so a
partial restore can be re-run) and holds it hot for RESTORE_HOLD_DAYS.
READERS: a line older than `hot_from(restaurant_id)` is in the cold tier,
not missing — `cold_months` lists what is there; nothing on a request path
reads it (docs/ops/RECOVERY.md, "Cold sale lines").
"""
import gzip
import hashlib
import io
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("pos_cold")

HOT_DAYS = int(os.getenv("RETAIN_POS_LINES_HOT_DAYS", "400") or 0)
HOT_FLOOR_DAYS = 400
RESTORE_HOLD_DAYS = 30
KIND = "pos-cold"
MONTHS_PER_PASS = int(os.getenv("POS_COLD_MONTHS_PER_PASS", "200") or 200)

_DDL = (
    """CREATE TABLE IF NOT EXISTS pos_cold_exports (
        restaurant_id   INTEGER NOT NULL,
        provider        TEXT NOT NULL,
        month           TEXT NOT NULL,
        rows_n          INTEGER NOT NULL,
        bytes           INTEGER NOT NULL DEFAULT 0,
        sha256          TEXT NOT NULL,
        ref             TEXT NOT NULL,
        exported_at     TEXT NOT NULL,
        deleted_at      TEXT,
        held_until      TEXT,
        restored_at     TEXT,
        PRIMARY KEY (restaurant_id, provider, month)
    )""",
)


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def init_pos_cold(db_path=None):
    """The cold tier's index table (boot only, models.init_db)."""
    conn = get_conn(db_path)
    try:
        for stmt in _DDL:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _fernet():
    key = (os.getenv("BACKUP_ENCRYPTION_KEY") or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None


def available() -> str:
    """'' when the cold tier can run, else why not (said in the pass)."""
    import blob_store
    if blob_store.active() == "db":
        return "no object store (BLOB_STORE=s3 with BLOB_S3_*): the lines stay hot"
    if _fernet() is None:
        return "no working BACKUP_ENCRYPTION_KEY: the lines stay hot"
    return ""


def _month_bounds(month):
    y, m = int(month[:4]), int(month[5:7])
    lo = date(y, m, 1)
    hi = date(y + (m == 12), (m % 12) + 1, 1)
    return lo.isoformat(), hi.isoformat()


def cutoff(today=None) -> str:
    """The first day of the month holding today - HOT_DAYS: every month
    before it is wholly past the hot window."""
    today = today or datetime.now(timezone.utc).date()
    edge = today - timedelta(days=HOT_DAYS)
    return edge.replace(day=1).isoformat()


def _rows(conn, rid, provider, month):
    lo, hi = _month_bounds(month)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? AND business_date >= ? "
        "AND business_date < ? ORDER BY business_date, line_id", (int(rid), provider, lo, hi)).fetchall()]


def _encode(rows) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for r in rows:
            gz.write((json.dumps(r, sort_keys=True, default=str) + "\n").encode("utf-8"))
    return buf.getvalue()


def _decode(blob) -> list:
    raw = gzip.decompress(blob)
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def export_month(rid, provider, month, db_path=None) -> dict:
    """Export one restaurant-month of sale lines (see the module note) and
    record it; the lines are NOT deleted here. Raises when the store refused
    it or the read-back does not match."""
    import blob_store
    f = _fernet()
    if f is None or blob_store.active() == "db":
        raise RuntimeError(available() or "cold tier unavailable")
    conn = get_conn(db_path)
    try:
        rows = _rows(conn, rid, provider, month)
    finally:
        conn.close()
    if not rows:
        return {"rows": 0}
    plain = _encode(rows)
    sealed = f.encrypt(plain)
    digest = hashlib.sha256(sealed).hexdigest()
    key = f"pos-lines/{int(rid)}/{provider}/{month}-{digest[:12]}.jsonl.gz.enc"
    ptr = blob_store.store(KIND, key, sealed)
    if not blob_store.is_pointer(ptr):
        raise RuntimeError(f"the object store did not take {key}")
    back = bytes(blob_store.resolve(ptr))
    if hashlib.sha256(back).hexdigest() != digest or len(_decode(f.decrypt(back))) != len(rows):
        raise RuntimeError(f"{key}: the read-back did not match what was written")
    ref = bytes(ptr).decode("utf-8")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO pos_cold_exports (restaurant_id, provider, month, rows_n, bytes, sha256, ref, exported_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, provider, month) DO UPDATE SET "
            "rows_n=excluded.rows_n, bytes=excluded.bytes, sha256=excluded.sha256, ref=excluded.ref, "
            "exported_at=excluded.exported_at, deleted_at=NULL",
            (int(rid), provider, month, len(rows), len(sealed), digest, ref, now))
        conn.commit()
    finally:
        conn.close()
    return {"rows": len(rows), "bytes": len(sealed), "ref": ref}


def _rolled(conn, rid, provider, month) -> bool:
    """Every archived day of the month rolled into pos_item_daily (its
    owner-facing history) at the current rollup version."""
    import pos_archive
    lo, hi = _month_bounds(month)
    row = conn.execute("SELECT COUNT(*) FROM pos_archive_days WHERE restaurant_id=? AND provider=? "
                       "AND business_date >= ? AND business_date < ? AND COALESCE(item_rollup, 0) <> ?",
                       (int(rid), provider, lo, hi, pos_archive.ITEM_ROLLUP_VERSION)).fetchone()
    return int(row[0] or 0) == 0


def _due_months(conn, edge, limit):
    """(restaurant, provider, month) with lines before `edge`, oldest month
    first per store: one indexed MIN per store (idx_pos_lines_day), never a
    scan of the old range."""
    out = []
    stores = conn.execute("SELECT DISTINCT restaurant_id, provider FROM pos_archive_days").fetchall()
    for rid, prov in stores:
        row = conn.execute("SELECT MIN(business_date) FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? "
                           "AND business_date < ?", (int(rid), prov, edge)).fetchone()
        if row and row[0]:
            out.append((int(rid), str(prov), str(row[0])[:7]))
        if len(out) >= limit:
            break
    return out


def tier(deadline=None, db_path=None, today=None) -> dict:
    """The nightly pass (ops.prune_ledgers): each due restaurant-month
    exported, verified and then its lines deleted (the module note). Returns
    {exported, deleted, held, unrolled, failed, months, skipped?}. Bounded by
    `deadline` (time.monotonic()) and MONTHS_PER_PASS; the rest go on the
    following nights. Never deletes a line it has not exported."""
    out = {"exported": 0, "deleted": 0, "held": 0, "unrolled": 0, "failed": 0, "months": []}
    if HOT_DAYS <= 0:
        out["skipped"] = "RETAIN_POS_LINES_HOT_DAYS=0: no cold tier"
        return out
    if HOT_DAYS < HOT_FLOOR_DAYS:
        out["skipped"] = f"RETAIN_POS_LINES_HOT_DAYS={HOT_DAYS} is under its {HOT_FLOOR_DAYS}-day floor"
        return out
    why = available()
    if why:
        out["skipped"] = why
        return out
    edge = cutoff(today)
    now_s = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        due = _due_months(conn, edge, MONTHS_PER_PASS)
    finally:
        conn.close()
    for rid, prov, month in due:
        if deadline is not None and time.monotonic() > deadline:
            out["hit_bound"] = True
            break
        try:
            conn = get_conn(db_path)
            try:
                if not _rolled(conn, rid, prov, month):
                    out["unrolled"] += 1
                    continue
                ex = conn.execute("SELECT rows_n, held_until FROM pos_cold_exports WHERE restaurant_id=? "
                                  "AND provider=? AND month=?", (rid, prov, month)).fetchone()
                lo, hi = _month_bounds(month)
                n = int(conn.execute("SELECT COUNT(*) FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? "
                                     "AND business_date >= ? AND business_date < ?",
                                     (rid, prov, lo, hi)).fetchone()[0] or 0)
            finally:
                conn.close()
            if ex and ex[1] and str(ex[1]) > now_s:
                out["held"] += 1
                continue
            if not ex or int(ex[0] or 0) != n:
                # Never exported, or lines came back since (a restore, a
                # re-archive): exported again, whole, before any delete.
                export_month(rid, prov, month, db_path=db_path)
                out["exported"] += 1
            conn = get_conn(db_path)
            try:
                cur = conn.execute("DELETE FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? "
                                   "AND business_date >= ? AND business_date < ?", (rid, prov, lo, hi))
                conn.execute("UPDATE pos_cold_exports SET deleted_at=? WHERE restaurant_id=? AND provider=? "
                             "AND month=?", (now_s, rid, prov, month))
                conn.commit()
                out["deleted"] += max(0, cur.rowcount or 0)
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()
            out["months"].append(f"{rid}:{prov}:{month}")
        except Exception as e:
            out["failed"] += 1
            log.error("pos_cold: %s %s %s not tiered: %s", rid, prov, month, e)
            try:
                import ops
                ops.capture(e, job="prune_ledgers", context=f"pos_cold {rid} {prov} {month}")
            except Exception:
                pass
    return out


def restore(restaurant_id, month, provider="rpower", db_path=None) -> dict:
    """Put one cold restaurant-month of sale lines back into
    pos_ticket_lines (rows already there are kept), held hot for
    RESTORE_HOLD_DAYS so the next pass does not move it straight back.
    {restored, rows}. Raises when there is no export or it cannot be read.
    On Postgres a month older than the lines' partitions lands in the
    DEFAULT partition (pg_partitions)."""
    import blob_store
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT ref, sha256, rows_n FROM pos_cold_exports WHERE restaurant_id=? AND provider=? "
                           "AND month=?", (int(restaurant_id), provider, str(month)[:7])).fetchone()
    finally:
        conn.close()
    if not row:
        raise LookupError(f"no cold export of {provider} lines for restaurant {restaurant_id} in {month}")
    f = _fernet()
    if f is None:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is needed to open a cold export")
    blob = bytes(blob_store.resolve(str(row[0]).encode("utf-8")))
    if hashlib.sha256(blob).hexdigest() != str(row[1]):
        raise RuntimeError("the cold export does not match its recorded SHA-256")
    rows = _decode(f.decrypt(blob))
    if not rows:
        return {"restored": 0, "rows": 0}
    cols = list(rows[0].keys())
    sql = (f"INSERT OR IGNORE INTO pos_ticket_lines ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' for _ in cols)})")
    held = (datetime.now(timezone.utc) + timedelta(days=RESTORE_HOLD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    lo, hi = _month_bounds(str(month)[:7])
    count = ("SELECT COUNT(*) FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? AND business_date >= ? "
             "AND business_date < ?")
    conn = get_conn(db_path)
    try:
        before = int(conn.execute(count, (int(restaurant_id), provider, lo, hi)).fetchone()[0] or 0)
        conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
        conn.execute("UPDATE pos_cold_exports SET held_until=?, restored_at=? WHERE restaurant_id=? AND provider=? "
                     "AND month=?", (held, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                     int(restaurant_id), provider, str(month)[:7]))
        conn.commit()
        restored = int(conn.execute(count, (int(restaurant_id), provider, lo, hi)).fetchone()[0] or 0) - before
    finally:
        conn.close()
    return {"restored": max(0, int(restored)), "rows": len(rows), "held_until": held}


def cold_months(restaurant_id, db_path=None) -> list:
    """The months of this restaurant's sale lines in the cold tier (deleted
    from the database), oldest first."""
    conn = get_conn(db_path)
    try:
        return [str(r[0]) for r in conn.execute(
            "SELECT month FROM pos_cold_exports WHERE restaurant_id=? AND deleted_at IS NOT NULL ORDER BY month",
            (int(restaurant_id),)).fetchall()]
    finally:
        conn.close()


def hot_from(restaurant_id=None, today=None):
    """The first business date whose sale lines are always hot (None while
    the cold tier is off): older lines may be in cold_months."""
    if HOT_DAYS <= 0 or available():
        return None
    return cutoff(today)
