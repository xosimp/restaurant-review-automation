# Postgres cutover — the runbook

Built 10/10/26 (scheduler audit, workstream H2: RM-13, RM-14, SCALE-20,
SCALE-21, ARCH-34). Everything up to the switch is in the code; **the switch
itself is Will's step** — it provisions a paid service and moves the
production database. Do it before ~300 restaurants, or sooner when the
volume passes 60% (docs/plans/POSTGRES_AND_WORKERS_PLAN.md, *When*).

What exists:

| Piece | Where | Off until |
|---|---|---|
| Postgres behind `models.get_conn` (every statement translated; sqlite3's connection surface) | `db_dialect.py` | `DATABASE_URL` names a Postgres |
| The copy, its verification, the Postgres → SQLite export | `pg_copy.py`, `scripts/pg_copy.py` | run by hand |
| What still needs a hand port | `scripts/pg_sql_audit.py` | — (10/10/26: 4,550 statements read, 0 untranslatable, 0 SQLite-only; 6 trigger statements ported in `migrations/postgres/0002_triggers.sql`; 2 built at run time) |
| The normalized schedule model, shadow-written | `pg_shadow.py`, `migrations/postgres/0001_schedule_model.sql` | `PG_SHADOW_WRITE=1` + `SHADOW_DATABASE_URL` |
| Blobs in object storage | `blob_store.py` | `BLOB_STORE=s3` + `BLOB_S3_*` |
| The write freeze | `http_layer.maintenance_gate`, `scheduler.scheduling_allowed`, `job_queue.claim` | `MAINTENANCE_READ_ONLY=1` |
| The nightly backup on Postgres (an export to SQLite, then the usual scrub, encryption, off-site copy) | `scheduler._write_consistent_snapshot` → `pg_copy.export_to_sqlite` | automatic once `DATABASE_URL` is set |

**Not verified against a live Postgres in this repo** (none was installed
where it was built): the translation is tested as text, the connection
against a recording driver, the copy and the shadow end to end against
SQLite targets. Step 2 below is that verification — do not skip it.

## 0. Before anything

1. `python scripts/pg_sql_audit.py` on the commit you will deploy. It must say
   `untranslatable: 0` and `sqlite_only: 0`; anything new it lists is ported
   (or `db_dialect.Translator` taught it) before going on.
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
   python scripts/pg_copy.py --sqlite snapshot.db --database-url "$SCRATCH_PG_URL" --report rehearsal.json
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
   `python scripts/pg_copy.py --sqlite snapshot.db --plan > plan.sql` and run
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
   python scripts/pg_copy.py --sqlite $RAILWAY_VOLUME_MOUNT_PATH/cutover.db \
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
   a no-op, every guarded ALTER answered from the catalog); `/health` must
   answer 200 inside the 120-second healthcheck.
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
  `python scripts/pg_copy.py --export $RAILWAY_VOLUME_MOUNT_PATH/from_pg.db --database-url "$PG_PRIVATE_URL"` —
  then restore it as the database the documented way (`RESTORE_FROM`,
  docs/ops/RECOVERY.md → *Database recovery*), set `DB_BACKEND=sqlite`,
  unfreeze.

## 6. After a month on Postgres

- Retire the volume's database files (keep the last snapshot off-site).
- The scheduler and the queue workers can become their own Railway services
  (POSTGRES_AND_WORKERS_PLAN.md step 4) — the lease, the queue and the
  heartbeats live in the networked database now. Not before.
- Retention on the partitioned fact tables can drop whole month partitions
  instead of chunked deletes (`DROP TABLE <table>_p<YYYYMM>`), once a month,
  for months wholly past the table's window.
