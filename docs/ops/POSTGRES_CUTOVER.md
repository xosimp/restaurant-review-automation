# Postgres cutover — the runbook

**Switched 10/19/26** (scale audit): rehearsed inside Railway (410 tables
verified, 2.3-minute copy, boot 12.7 s), then frozen at 18:43 CT, copied and
verified in 120 s, switched at 18:47 CT (`DATABASE_URL=${{Postgres.DATABASE_URL}}`,
`MAINTENANCE_READ_ONLY=0`). The volume's `reviews.db` and `cutover.db` are
the SQLite database as it stood at the switch. On the Hobby plan the Postgres
volume is capped at 5 GB — move to Pro before the RPOWER rollout.

Built 10/10/26 (scheduler audit, workstream H2: RM-13, RM-14, SCALE-20,
SCALE-21, ARCH-34). Everything up to the switch is in the code; **the switch
itself is Will's step** — it provisions a paid service and moves the
production database. Do it before the RPOWER rollout: the ticket archive
(~0.3 GB a POS store-year, ~60 MB backfill per new store) fills the 5 GB
volume at roughly 15-25 POS stores — the "~300 restaurants" this once said
predates the archive (scale audit 10/19/26, DB-01). Railway's Hobby plan caps
every volume at 5,000 MB, the Postgres service's too: past a few dozen POS
stores the database needs the Pro plan (50 GB, self-serve to 1 TB).

What exists:

| Piece | Where | Off until |
|---|---|---|
| Postgres behind `models.get_conn` (every statement translated; sqlite3's connection surface) | `db_dialect.py` | `DATABASE_URL` names a Postgres |
| The copy, its verification, the Postgres → SQLite export | `pg_copy.py`, `scripts/run_pg_copy.py` | run by hand |
| What still needs a hand port | `scripts/pg_sql_audit.py` (`--database-url` PREPAREs every static statement on a real Postgres) | — (10/14/26: 4,813 statements read, 0 untranslatable, 0 SQLite-only; 3,901 PREPAREd on Postgres 16, 0 failed; 2 built at run time) |
| The boot on Postgres | `models.boot_conn`, `db_dialect.install_compat` / `install_triggers` | automatic: the boot builds or upgrades the Postgres schema itself |
| A bounded connection pool | `db_dialect._Pool` (`PG_POOL_*`, ENVIRONMENT.md) | automatic |
| CI against a real Postgres | `.github/workflows/ci.yml`, job `postgres` | every push |
| The normalized schedule model, shadow-written | `pg_shadow.py`, `migrations/postgres/0001_schedule_model.sql` | `PG_SHADOW_WRITE=1` + `SHADOW_DATABASE_URL` |
| Blobs in object storage | `blob_store.py` | `BLOB_STORE=s3` + `BLOB_S3_*` |
| The write freeze | `http_layer.maintenance_gate`, `scheduler.scheduling_allowed`, `job_queue.claim` | `MAINTENANCE_READ_ONLY=1` |
| The nightly backup on Postgres (an export to SQLite, then the usual scrub, encryption, off-site copy) | `scheduler._write_consistent_snapshot` → `pg_copy.export_to_sqlite` | automatic once `DATABASE_URL` is set |

**Verified against a real Postgres (16) since 10/14/26** (blind re-audit
10/11/26, SCALE-5 … SCALE-12 — before that it had only met a recording
driver, and the first boot would have failed in init_db). CI's `postgres`
job runs, on every push, against a throwaway Postgres 16 with no data and
no keys: the app's boot twice in its own process (building SQLite's schema
column for column, type for type), the copy then the boot (step 4), every
static statement PREPAREd (`pg_sql_audit.py --database-url`), the dialect's
semantics, and 37 database-heavy test files with each test's database on
Postgres (`CAVNAR_TEST_PG_URL`, tests/conftest.py). What it does not prove
is production's data: step 2 below is that rehearsal — do not skip it.

What the dialect layer keeps of SQLite's behaviour (db_dialect.py):
- **A failed statement costs only itself.** Inside a transaction every
  statement runs under a savepoint (one round trip, psycopg's pipeline), so
  init_db's already-applied ALTERs, the try-INSERT-then-UPDATE idiom and
  every `except sqlite3.OperationalError: pass` keep the transaction as
  SQLite does — Postgres would otherwise abort it.
- **The boot writes to Postgres.** init_db, init_auth and the init chain
  used `sqlite3.connect` directly: on Postgres their tables would have
  landed in the volume's empty file. They use `models.boot_conn`; the boot
  installs the `sqlite_master` view and the helper functions
  (`0000_sqlite_compat.sql`: `cavnar_ts`, `cav_name_key`,
  `cavnar_json_array`) and the triggers' plpgsql form (the tracker stamp,
  the intel version, the append-only ledgers); a CREATE TABLE's foreign
  keys are added once their target exists; ALTER TABLE ADD COLUMN gets
  CREATE TABLE's types.
- **SQL semantics:** SUM/AVG of a predicate, `COLLATE NOCASE` (as lower()),
  NULL ordering (NULLS FIRST ascending, LAST descending — and indexes built
  the same way), NULL-preserving `MAX(a, b)`, `LIKE … ESCAPE ''`, typed
  `json_extract`, NULL for a date read of `''`, `x IS ?`, a bare
  parameter's type, ambiguous upsert columns, `ROUND(x, n)`, `json_set` /
  `json_each` / `json_valid` (needs Postgres 16+ for `IS JSON`), bare
  columns beside GROUP BY, HAVING aliases, a CASE mixing predicates and
  numbers, `INSERT OR REPLACE` on the unique key the row names, a table's
  `ON CONFLICT REPLACE`, `UPDATE OR IGNORE` (row by row), SUM of integers
  read back as an int (not Decimal), and a stable `rowid` column for tables
  with no integer key (left out of `SELECT *`, as SQLite does).
- **Connections:** one bounded pool a process (`PG_POOL_MAX` 20 +
  `PG_POOL_OVERFLOW` 20 for nested connections, 5 idle, 30-minute
  lifetime); the web process plus `worker.py` stay under Railway's 100.
  PgBouncer in transaction mode works too (the locks are transaction
  scoped) once statements are not prepared server-side: PgBouncer 1.21+
  with `max_prepared_statements`, or `PG_PREPARE_THRESHOLD=off` (scale audit
  10/19/26 DB-07) — not needed at these numbers.
  A database that restarts (10/19/26: enabling PITR redeployed it; every
  checkout waited the 10 s connect timeout for ~3 minutes and the four
  request threads queued) no longer stalls the app (DB-22): a connect gives
  up after `PG_CONNECT_TIMEOUT_S` (5 s); after a failed one, checkouts that
  need a new connection fail at once for `PG_CONNECT_BACKOFF_S` (3 s) with
  the `sqlite3.OperationalError` callers already handle; an idle connection
  is checked before reuse (and asked `SELECT 1` past `PG_POOL_PING_AFTER_S`,
  30 s), so a restart's dead connections are dropped, not handed out.
- **Timeouts:** every connection sets `lock_timeout` 30 s (SQLite's
  busy_timeout), `statement_timeout` 5 min and
  `idle_in_transaction_session_timeout` 5 min (`PG_*_MS`, ENVIRONMENT.md;
  DB-06) — without them one stuck holder of the one-writer advisory lock
  stopped every process.
- **Partitions:** the copy makes them to 3 months ahead; the daily
  `pg_partitions` job keeps them 3 months ahead and fails when a dated row
  sits in a DEFAULT partition (DB-04); the nightly prune drops a month wholly
  past its table's window (DB-03).
- **Savepoints:** every statement inside a transaction is its own savepoint;
  a write loop of more than 64 `execute()` calls in one transaction
  overflows Postgres's subtransaction cache — write loops use `executemany`
  (one savepoint) instead (DB-05; `save_labor_daily_history` since 10/19/26).
- **Locks:** `BEGIN IMMEDIATE` is one advisory lock (SQLite's one writer)
  except on the hot paths, which lock only what they serialise
  (`db_dialect.begin_write`): a job group's claim, one kind's enqueues, one
  gateway minute, one rate key.

## 0. Before anything

1. `python scripts/pg_sql_audit.py --database-url "$SCRATCH_PG_URL"` on the
   commit you will deploy (a scratch Postgres with NO production keys in the
   shell — the boot writes its schema there). It must say
   `untranslatable: 0`, `sqlite_only: 0` and `prepare_failed: 0`; anything
   new it lists is ported (or `db_dialect.Translator` taught it) before
   going on. CI's `postgres` job is the same check; it must be green.
2. The volume has room for one more copy of the database (the snapshot the
   copy reads): `/admin` → Engineering → disk, or `df -h $RAILWAY_VOLUME_MOUNT_PATH`.
3. Optional, recommended first: move the blobs out (step 1b) — the copy and
   every backup after it are that much smaller.

## 1. Provision

a. **Postgres.** Railway → the project → New → Database → PostgreSQL (16 or
   17). Same project and region as the web service (private networking).
   Note its *private* URL (`postgres.railway.internal`) — never the public
   proxy URL for the app. Point-in-time recovery: on Railway's Pro plan turn
   on backups for the service; otherwise the nightly export (above) is the
   only copy — say so in RECOVERY.md's *The backup* if that is the case.
   `CREATE EXTENSION btree_gist` must be allowed (it is on Railway's image;
   the schedule model's exclusion constraint needs it).

b. **Blob bucket** (optional, 1b). A bucket of its own (not the backup
   bucket — its lifecycle rule would expire blobs): R2 or S3. Lifecycle rule
   on the prefix `cavnar-blobs/model-calls/`: expire after 100 days
   (`schedule_model_calls` keeps 90). Set `BLOB_S3_ENDPOINT` (https),
   `BLOB_S3_BUCKET`, `BLOB_S3_ACCESS_KEY_ID`, `BLOB_S3_SECRET_ACCESS_KEY`,
   then `BLOB_STORE=s3`. New calls and evaluations go to the bucket at once;
   the `blob_offload` job (2am) moves the old inline rows, 5 minutes a night.
   Rollback: `BLOB_STORE=db` (new writes inline again; existing pointers
   keep reading from the bucket — never delete the bucket while any row
   points into it).

## 2. Rehearse on a copy (verifies the Postgres path for real)

1. Take the newest local backup snapshot (`backups/cavnar_ai_backup_<date>.db.gz`
   on the volume — `railway ssh`, then `gunzip -k`), or a fresh one:
   `python -c "import models; models.checkpoint()"` then copy `reviews.db`.
2. Copy it into a scratch Postgres (a second Railway Postgres, or one on a
   laptop — with NO production keys in that shell):
   ```
   python scripts/run_pg_copy.py --sqlite snapshot.db --database-url "$SCRATCH_PG_URL" --report rehearsal.json
   ```
   Exit 0 means every table verified (row counts, and the integer key's min,
   max and sum on both sides). Read `rehearsal.json`: `coerced` lists the
   values SQLite held that Postgres's column types refuse (an empty string
   in an INTEGER column becomes NULL, counted); `foreign_keys.refused` lists
   rows that point at nothing. Each is a decision, not a surprise.
3. Run the app against it, locally, with NO provider keys in the environment
   (CLAUDE.md: never a worktree with Flask's `.env`): `DATABASE_URL=$SCRATCH_PG_URL`
   and open the dashboard, Labor, the Studio (generate is safe without keys —
   it refuses), Ask's stored reads, the admin console. Then run the suite's
   database-heavy files against it if time allows. Anything that fails is a
   statement `db_dialect` does not carry: fix the translator (and add a test
   in `tests/test_sf10_h2_postgres.py`), re-run the audit.
4. Time the copy: that is the length of the write freeze in step 4.

## 3. The shadow (weeks before the switch)

1. Apply the schedule model to the production Postgres:
   `python scripts/run_pg_copy.py --sqlite snapshot.db --plan > plan.sql` and run
   the `sched` part (`migrations/postgres/0001_schedule_model.sql`) with
   `psql "$PG_URL" -f migrations/postgres/0001_schedule_model.sql`.
2. On the web service: `SHADOW_DATABASE_URL=<private URL>`, `PG_SHADOW_WRITE=1`.
   Every schedule revision is noted in `pg_shadow_outbox` in its own
   transaction; the `pg_shadow` job writes them every 10 minutes.
3. Backfill the history it should hold:
   `python -c "import pg_shadow; print(pg_shadow.backfill(since_days=180))"`
   (`railway ssh`), repeated until it returns 0.
4. Compare: `python -c "import pg_shadow, db_dialect, os; print(pg_shadow.compare(db_dialect.connect(os.environ['SHADOW_DATABASE_URL'])))"`
   — `ok: True`, `outbox_failed: 0`. A row the shadow refused keeps its error
   in `pg_shadow_outbox.error`; `sched_shifts_no_double_booking` in it means a
   PUBLISHED week double-booked someone — a real finding for the owner.
5. Rollback: unset `PG_SHADOW_WRITE`. Nothing reads the shadow.

## 4. The switch (a quiet window: Tuesday ~3am CT)

1. **Freeze writes.** `MAINTENANCE_READ_ONLY=1` on the web service (Railway
   redeploys): every write request answers 503 with a sentence, the scheduler
   and its sends stop, the job queue claims nothing; reads, sign-in pages and
   `/health` still answer. Wait for running generations to finish (`/admin` →
   Jobs, or `SELECT COUNT(*) FROM jobs WHERE state='running'`) — or accept
   that a running one will resume after the switch from its checkpoint.
2. **Snapshot.** `railway ssh`, then
   `python -c "import models; models.checkpoint()"` and
   `cp $RAILWAY_VOLUME_MOUNT_PATH/reviews.db $RAILWAY_VOLUME_MOUNT_PATH/cutover.db`.
3. **Copy and verify** (from the same shell; the private URL resolves there):
   ```
   python scripts/run_pg_copy.py --sqlite $RAILWAY_VOLUME_MOUNT_PATH/cutover.db \
       --database-url "$PG_PRIVATE_URL" --report $RAILWAY_VOLUME_MOUNT_PATH/cutover.json
   ```
   (Into an empty database, or one holding only the shadow's `sched`
   schema: the schema statements are `IF NOT EXISTS`, but the rows are
   inserted, not merged.) Exit 0 = verified. Anything else: stop,
   unset `MAINTENANCE_READ_ONLY`, nothing has changed.
4. **Switch.** Set `DATABASE_URL=<private URL>` and remove
   `MAINTENANCE_READ_ONLY` in one change (Railway redeploys once). Remove
   `PG_SHADOW_WRITE` / `SHADOW_DATABASE_URL` (the primary is the model now).
   The boot runs `init_db` against Postgres (every `CREATE … IF NOT EXISTS`
   a no-op, every already-applied ALTER refused under its own savepoint and
   tolerated — the transaction goes on; proven in CI by the copy-then-boot
   test, twice); `/health` must answer 200 inside the 120-second
   healthcheck.
5. **Check.** Sign in as the demo restaurant (rid 4); open Labor and save a
   draft edit; `/admin` → Engineering (write probe, jobs); the morning's jobs
   in `/admin` → Jobs the next day; the nightly backup's email (its snapshot
   is now the Postgres export).

## 5. Rollback

- **Within the window** (before step 4): unset `MAINTENANCE_READ_ONLY`.
  SQLite never stopped being the database.
- **After the switch, no writes worth keeping yet:** set `DB_BACKEND=sqlite`
  (the volume's `reviews.db` is still there, as it was at step 2). Writes
  made on Postgres since are lost — so decide within minutes.
- **After the switch, keep what Postgres took:** freeze again
  (`MAINTENANCE_READ_ONLY=1`), export Postgres to SQLite on the volume —
  `python scripts/run_pg_copy.py --export $RAILWAY_VOLUME_MOUNT_PATH/from_pg.db --database-url "$PG_PRIVATE_URL"` —
  then restore it as the database the documented way (`RESTORE_FROM`,
  docs/ops/RECOVERY.md → *Database recovery*), set `DB_BACKEND=sqlite`,
  unfreeze.

## 6. After a month on Postgres

- Retire the volume's database files (keep the last snapshot off-site).
- The scheduler and the queue workers can become their own Railway services
  (POSTGRES_AND_WORKERS_PLAN.md step 4) — the lease, the queue and the
  heartbeats live in the networked database now. Not before.
- Retention on the partitioned fact tables drops whole month partitions
  (built 10/19/26, DB-03): the nightly `prune_ledgers` detaches and drops a
  month once it is wholly past the table's window (`pg_partitions.drop_expired`,
  at most `PARTITION_DROPS_PER_PASS` = 1 a table a night, after the table's
  rollup and with a records hold respected), then deletes in chunks what is
  left (the partial month, the DEFAULT partition). Nothing to do here.
