# Recovery — Cavnar AI

What to do when production is broken. Written during resiliency audit #21,
which found working, integrity-checked backups and **no procedure anywhere
for using one**. A backup nobody has restored is a hypothesis. Updated after
the admin-console fix round (9/29/26): off-site copies, operator paging, the
`/health` contract and the break-glass variables; the console paths below
are its five areas (Overview, Operations, Customers, Engineering,
Analytics).

Read `SYSTEM_ARCHITECTURE.md` for how the pieces fit, `SECURITY.md` (beside
this file) for the controls. This file is only the emergency path.

---

## First: what kind of broken?

Check `https://dashboard.cavnar.ai/health` before anything else. It is
read-only (it writes nothing and sends nothing) and answers one of three
ways; the body names the problem by code.

| `/health` says | What it means | Go to |
|---|---|---|
| Connection refused / 502 | The process is down, crash-looping, or Railway is down | [Platform down](#platform-down) |
| `500`, `"error": "db_unavailable"` or `"db_unreadable"` | The database file cannot be opened or read | [Database recovery](#database-recovery) |
| `500`, `"error": "schema_mismatch"` (with `missing_count`) | The database lacks tables or columns this code expects — an empty or old restore, a skipped migration. The names are in the deploy log, not the body | [Database recovery](#database-recovery) |
| `500`, `"error": "db_not_writable"` | The database refuses a write (read-only or failing storage, or a full volume) | [Volume full](#volume-full), then [Database recovery](#database-recovery) |
| `500`, `"error": "data_missing"` | The database holds no client restaurants, but the volume's marker says it held some — an emptied or replaced database | [Database recovery](#database-recovery) |
| `200`, `"status": "degraded"`, `problems` has `disk_low` / `disk_critical` | Volume filling or nearly full — **writes fail before reads do** | [Volume full](#volume-full) |
| `200`, `"status": "degraded"`, `scheduler_stale`, `scheduler_wedged`, `scheduler_stalled` or `jobs_overdue` | Web is fine, background jobs have stopped, are stuck in one job, or have fallen behind | [Scheduler stopped](#scheduler-stopped) |
| `200`, `"status": "degraded"`, `backup_stale` or `offsite_backup_stale` | No good backup (or no off-site copy) in 26 hours | [The backup](#the-backup) |
| `200`, `"status": "degraded"`, `db_busy` / `db_not_wal` | A write lock held past two seconds, or the database is not in WAL mode | [Something specific](#something-specific-is-broken) |
| `200`, `"status": "ok"` | The platform is healthy; the problem is narrower | [Something specific](#something-specific-is-broken) |

Railway's deploy check needs only the 200, so a DEGRADED platform still
deploys (failing it would block the deploy that fixes it). The external
uptime monitor (below) asserts the keyword `"status":"ok"`, so degraded and
error both page. The 200 body also carries the write probe, disk (free,
total, database and WAL sizes and both thresholds), the scheduler heartbeat
age, backup and off-site ages, and the overdue jobs; it is cached 5 seconds.

`/status` (public) and `/admin` → Overview carry the same signals with more
detail — Overview's *Needs you* list names the restaurants the review fetch
stopped reaching (`fleet:fetch_coverage`); `/admin` → Operations states each
system from the server's own verdict (`GET /admin/api/ops/state`); and
`/admin` → Engineering → Overview has the System, Scheduler, Providers,
Backups and Boots cards (`GET /admin/api/system`: keys, disk, database,
volume, backup, drill, lease, heartbeat, AI, supervisor, 5xx by route,
boots, providers).

**Reaching the container.** Every command below that touches `/app/data`
runs *inside* the Railway container: `railway ssh -- <command>` (the CLI
must be linked to the project — `railway status` shows it). `railway run`
is the wrong tool — it runs the command on *your laptop* with production's
environment variables, so `railway run sqlite3 /app/data/reviews.db` opens
nothing, or worse, a local file. An earlier version of this file said
`railway run` throughout.

**Logs.** On Railway every line is JSON (`logging_setup`): filter on
`@level:error`, `@logger:ops`, `@request_id:<id>` (every response carries
`X-Request-ID`), `@route:`. The gunicorn access log writes the route RULE
and the request id, never the raw path.

---

## Paging and the external monitors

Three things page, and they fail independently:

1. **The operator page** (`ops.page_operator` → `ops.alert_will`): a text to
   `WILL_PHONE` (the channel that survives a Resend or APNs outage), an
   email, and a push to the admin logins (a push proves nothing, so it does
   not count as delivered). Sent by the web process's supervisor thread
   every five minutes when something needs a person — a stale heartbeat, a
   wedged job or stalled loop, an overdue job, low or critical disk, a
   refused write, a stale / failed / off-site-less backup, a DSR night
   missing past its deadline — at most once an hour per problem, and only
   on Railway. The same unresolved problem repeats hourly until it is
   fixed. Every page is an `operator_alerts` row (Operations → Jobs shows
   the last one and whether it went). Owners are never paged.
2. **The dead-man ping** (`HEALTHCHECK_PING_URL`, e.g. healthchecks.io or
   Better Stack with a period of about 5 minutes and a grace of about 10):
   the scheduler pings at the end of every tick and during a long job that
   is inside its bound; the backup and the digest ping `/fail` when they
   fail. Its silence is what pages when the whole process is gone — nothing
   inside the process can.
3. **The external uptime monitor** on `https://dashboard.cavnar.ai/health`,
   every 1–5 minutes, alerting unless the body contains `"status":"ok"`,
   with SMS contacts. Its hosted status page is the one that survives this
   process; `/status` is served by the same process it reports on.

Also: Railway's own deploy and crash notifications, and Sentry (handled and
unhandled 5xx are reported; the release is the commit).

---

## Platform down

Railway outage or a crashed process.

1. Check Railway's own status, then the service's Deployments tab.
2. If the last deploy is the cause, **roll back in Railway** — do not try to
   fix forward under pressure. The database is on a persistent volume and is
   not affected by a rollback.
3. **With a volume attached, Railway runs one deployment at a time.** The
   old container is stopped before the new one boots, so a boot that fails,
   or a `/health` that does not answer 200 within the 120-second
   healthcheck, is **downtime until a redeploy or a rollback** — the
   previous container does NOT keep serving.
4. If the process is crash-looping, read the deploy logs. Each boot writes a
   `boot_events` row saying how the previous process ended (clean, unclean,
   boot failed); more than 3 boots of one deployment, or more than 3 that followed a crash, in
   an hour pages once. `hosted_dashboard` refuses to boot when:
   - **the volume is missing** (`models.require_volume`): on Railway with no
     `RAILWAY_VOLUME_MOUNT_PATH`, a mount that is missing or not writable,
     or the database not on the mount. Fix the volume; `ALLOW_NO_VOLUME=1`
     overrides, for an emergency only (it would serve an empty platform);
   - **the database was emptied** (`status_manager.assert_platform_not_emptied`):
     the volume's marker (`.cavnar-volume.json`, written once the volume
     holds a client restaurant) says it held clients and the database now
     has none — refused BEFORE `init_db` and the seeds could build an empty
     platform over it. Restore (below); `ALLOW_EMPTY_DATABASE=1` (or deleting
     the marker) is for an emptying that was deliberate;
   - **a migration raised** (`DB init error:` in the log). Since DATA-11
     that includes "database is locked" and any other failure of the boot
     init chain: the process exits rather than serving on a half-migrated
     schema. A lock is usually another process holding the file — an
     overlapped container, `worker.py`, or an open `railway ssh sqlite3`
     session; close it and redeploy.
5. A scheduler thread that dies is restarted by the supervisor — the lease
   keeper and then the loop (`scheduler._run_scheduler_thread`), with
   backoff (30 seconds, doubling to 10 minutes), never beside a live one; a
   thread alive but wedged is left alone and shows as Wedged on Operations →
   Jobs.

**What keeps working:** nothing. This is a full outage.
**Data loss:** none — the volume survives.
**Owners see:** the site not loading. `/status` is served by the same
process; point clients at the external monitor's hosted status page, or
post to them directly.

---

## Database recovery

`/health` returns 500 with a database error code, or SQLite reports corruption.

### 1. Confirm it is really the database

```bash
railway ssh -- sqlite3 /app/data/reviews.db "PRAGMA integrity_check;"
```

`ok` means the file is fine and the problem is elsewhere.

### 2. Find a snapshot

Three copies exist, newest first in the backup ledger
(`SELECT id, finished_at, local_ok, offsite_ok, offsite_target, sha256 FROM backup_runs ORDER BY id DESC LIMIT 5;`,
or `/admin` → Engineering → Overview → Backups, `GET /admin/api/backup`):

- **The local snapshot** on the volume, taken at 2am and kept
  `BACKUP_RETAIN_DAYS` days (14 in production; the code default is 7):

  ```bash
  railway ssh -- ls -lh /app/data/backups/
  ```

  **The local snapshots are complete and directly restorable.** They are not
  redacted — the scrub applies only to copies that leave the server. This
  was fixed in audit #21; snapshots taken *before* that fix have credentials
  stripped, and restoring one means re-authorising every integration by
  hand (see [After any restore](#after-any-restore)).

- **The object-storage copy** (Cloudflare R2 / S3, `BACKUP_S3_*`) —
  `s3://<bucket>/<prefix>cavnar_ai_backup_YYYY-MM-DD.db.enc`, the prefix
  `cavnar-backups/` by default. Use it when the volume itself is gone.
- **The emailed copy** (to Will, when it fits in 25 MB) — the same
  encrypted file as an attachment.

Both off-site copies are **scrubbed** (sessions, 2FA backup codes — an
internal login's own too — trusted devices, device tokens, staff-portal
links, view-as sessions, admin job results (`async_jobs`), the link-signing
secrets in `app_secrets`, and every OAuth / POS / reservation / back-office
credential, webhook secret and Stripe customer id — `DATABASE_SCHEMA.md` →
*What leaves the server in a backup*; the backup email lists what that
night's scrub did) and **Fernet-encrypted** with
`BACKUP_ENCRYPTION_KEY`, one token per 3 MB chunk, one per line.

**Key escrow.** `BACKUP_ENCRYPTION_KEY` must be kept somewhere other than
Railway (a password manager): without it no off-site copy can be read, and a
lost Railway project takes the key with it. Keep `CREDENTIAL_KEY` beside it:
a restored LOCAL snapshot holds credentials encrypted with it.

#### Restoring from the object-storage copy, step by step

1. Find the newest good copy and its checksum:
   `sqlite3 /app/data/reviews.db "SELECT finished_at, offsite_target, sha256 FROM backup_runs WHERE offsite_ok=1 ORDER BY id DESC LIMIT 3;"`
   (if the database is gone, list the bucket: `aws s3 ls s3://<bucket>/cavnar-backups/ --endpoint-url https://<account>.r2.cloudflarestorage.com`).
   Each object also carries its SHA-256 as `x-amz-meta-sha256`
   (`aws s3api head-object --bucket <bucket> --key cavnar-backups/<file> --endpoint-url …`).
2. Download it. Inside the container the credentials are already in the
   environment — `railway ssh`, then from `/app`:
   ```bash
   python3 -c "import offsite_backup; print(offsite_backup.download_file('cavnar_ai_backup_YYYY-MM-DD.db.enc', '/app/data/restore.enc'))"
   ```
   (it prints the SHA-256 it computed and the one stored with the object).
   Off the platform: `aws s3 cp s3://<bucket>/cavnar-backups/cavnar_ai_backup_YYYY-MM-DD.db.enc . --endpoint-url https://<account>.r2.cloudflarestorage.com`.
3. **Check the SHA-256** against `backup_runs.sha256` or `x-amz-meta-sha256`
   (`shasum -a 256 cavnar_ai_backup_YYYY-MM-DD.db.enc`). A mismatch means the
   copy is damaged — take the previous night's.
4. Decrypt it line by line with the escrowed key:
   ```bash
   python3 -c "
   from cryptography.fernet import Fernet
   f = Fernet(open('key.txt').read().strip().encode())
   with open('cavnar_ai_backup_YYYY-MM-DD.db.enc','rb') as enc, open('restore_candidate.db','wb') as out:
       for line in enc:
           if line.strip():
               out.write(f.decrypt(line.strip()))
   "
   ```
   (inside the container, `scheduler._decrypt_file_chunked(src, dest, key)`
   does the same with `BACKUP_ENCRYPTION_KEY`). A copy from before the
   chunked format is a single token and decrypts with the same loop.
5. Verify it (step 3 below) — `integrity_check` first.
6. Put it on the volume (`/app/data/…`) and restore it with `RESTORE_FROM`
   (step 4 below). Then work through [After any restore](#after-any-restore):
   an off-site copy is scrubbed, so expect to redo every integration.

The quarterly drill does steps 2–5 automatically where object storage is
configured, so a failed drill email is the early warning (see [Drill](#drill)).

### 3. Verify the snapshot before trusting it

Never restore a file you have not opened:

```bash
sqlite3 candidate.db "PRAGMA integrity_check;"
sqlite3 candidate.db "SELECT COUNT(*) FROM restaurants;"
sqlite3 candidate.db "SELECT MAX(review_date) FROM reviews;"
sqlite3 candidate.db "SELECT MAX(fired_at) FROM alert_log;"
```

The last two tell you how much time the restore costs. A 2am snapshot loses
everything since 2am.

### 4. Restore

**Never swap the files under a running service.** The web threads and the
scheduler open the database continuously; one connection landing between a
`mv` and a `cp` creates an empty database whose WAL then replays over the
restored file. `integrity_check` still says ok, boot seeds the admin and
demo accounts, and `/health` used to go green on an empty platform.

The restore is a boot step instead (`db_restore.py`):

1. In Railway → web → Variables, set
   `RESTORE_FROM=/app/data/backups/cavnar_ai_backup_YYYY-MM-DD.db` (or the
   decrypted off-site copy's path on the volume).
   Saving it redeploys. The new process swaps the snapshot in before its
   first connection: it checks the snapshot (integrity and a `restaurants`
   table), keeps the old file and its `-wal`/`-shm` as
   `reviews.db.broken-<time>`, copies the snapshot in, and checks the
   restaurant count matches. A snapshot that fails a check fails the deploy
   and changes nothing — and with a volume attached a failed deploy is
   downtime until you roll back or fix the variable.
2. While `RESTORE_FROM` is set **the scheduler does not run**, and no admin
   action sends email or texts (`scheduler.scheduling_allowed()` is false):
   no briefs, digests, alerts or reset links go out of a database nobody has
   looked at yet. A marker beside the database stops a later boot from
   restoring the same snapshot again over new writes.
3. Confirm (step 5), then **delete `RESTORE_FROM`**. That redeploys with the
   scheduler back on.

`init_db()` and `ensure_columns()` run right after the swap and additively
migrate an older snapshot forward — that path is exercised on every deploy.

Manual fallback, only with the service stopped (scale it to 0 in Railway
first, so nothing can open the file):

```bash
railway ssh -- mv /app/data/reviews.db /app/data/reviews.db.broken-$(date +%s)
railway ssh -- rm -f /app/data/reviews.db-wal /app/data/reviews.db-shm
railway ssh -- cp /app/data/backups/cavnar_ai_backup_YYYY-MM-DD.db /app/data/reviews.db
```

### 5. Confirm

- `/health` returns 200 — it checks the schema too, so an empty or
  unmigrated database answers 500 `schema_mismatch` (the missing tables are
  named in the log), and an emptied one on a volume that held clients
  answers 500 `data_missing`
- `/admin` → Overview loads and client count looks right
- One client's dashboard renders
- The scheduler heartbeat goes green within ~5 minutes of removing
  `RESTORE_FROM`

---

## After any restore

Work through this list explicitly. Recovery is not finished when the site
loads.

| Area | State after restore | Action |
|---|---|---|
| Owner sessions | Valid (local snapshot) or gone (off-site copy) | If gone, tell clients to sign in again |
| Push device tokens | Same | They re-register on next app launch; `send-test-push` to confirm |
| Google / Toast / Square / Clover / RPOWER / Instagram / reservations / Back Office | Same (an off-site copy nulls them all) | If nulled, each owner must reconnect in Account → Connections (RPOWER and Back Office are re-entered by Cavnar in the console) |
| Link-signing secrets (`app_secrets`) | Kept (local) or GONE (off-site copy) | From an off-site copy new kept secrets are minted: pay links, table-tent join QR codes, issue links and unsubscribe links issued before the backup no longer verify — resend pay links and reprint QR codes |
| Customer webhook secrets | Kept (local) or blanked (off-site copy) | Owners re-save their outbound webhook |
| Stripe | `stripe_customer_id` nulled in an off-site copy | Re-link it in the console (Customers → the client → Billing → **Attach Stripe customer**, with the `cus_` id from the Stripe dashboard), then run the `stripe_reconcile` job (Operations → Jobs → Run now) so the subscription mirror is refilled |
| Data since the snapshot | Lost | Reviews re-fetch on the next pass (`UNIQUE(restaurant_id, platform, external_id)` makes that safe). Uploaded CSVs and manual edits do not come back |
| Job claims | Restored to snapshot state | Jobs already run that day **may run again**. Check `job_period_claims` before a digest hour |
| Owed billing emails | Restored to snapshot state | Check `owed_sends` for rows the snapshot has as pending that already went (sent after the snapshot) and mark them `cancelled` before you delete `RESTORE_FROM`: the `owed_sends` job drains due rows on every tick once the scheduler is back |

The job-claims row is the one most likely to cause a visible mistake:
restoring a 2am snapshot at 9am can re-send that morning's digests. To
suppress:

```bash
sqlite3 reviews.db "INSERT OR IGNORE INTO job_period_claims (job_key) VALUES ('weekly_digests:YYYY-MM-DD');"
```

---

## Volume full

Writes fail, reads succeed, and **every fail-open guard in the system keeps
failing open**. This looks like a hundred unrelated small errors, not one
cause. `/health` names it directly: `disk_low` below max(250 MB, 4 × the
database and its WAL) or under 10% free, `disk_critical` below max(50 MB,
1.5 ×) or under 2% free, and a 500 `db_not_writable` once a write is refused.

People already signed in keep reading: a session's `last_active` stamp is
best-effort and written at most once a minute, so a refused write no longer
fails the request (a `session_last_active` entry in the error log says the
database is refusing writes). **Nobody can sign in** — a new session is a
write — and every save, draft and send fails until space is freed.

1. `railway ssh -- df -h /app/data`
2. Largest offenders are usually `backups/` and the WAL (`reviews.db-wal`):
   ```bash
   railway ssh -- du -sh /app/data/* | sort -h | tail
   ```
3. Free space: lower `BACKUP_RETAIN_DAYS`, delete the oldest snapshots
   (**never the newest**), or grow the volume in Railway. Once the off-site
   copy is reliably running, fewer local days are needed.
4. `ops.prune_ledgers` trims old ledger rows nightly after each backup
   (chunked, bounded); it can be run early from `/admin` → Operations →
   Jobs → `prune_ledgers`.
5. The backup refuses to START with less than 3.5 × the database free
   (`BACKUP_FREE_SPACE_FACTOR`) — it fails and pages rather than filling the
   volume at 2am. Engineering → Overview → Backups shows growth per day and
   days to full (`ops.storage_trend`, from each night's backup row).

A full volume is the one failure where doing nothing gets worse quietly.

---

## Scheduler stopped

`/health` shows `"scheduler": "stale"`, `"wedged"` or `"stalled"`
(`scheduler_stale`, `scheduler_wedged`, `scheduler_stalled`) or
`jobs_overdue`. The web app is fine; briefs, digests, fetches and scheduled
posts are not running. Stale: no heartbeat for 15 minutes. Wedged: the
loop has been inside one job past that job's own bound
(`jobs_registry.max_minutes`). Stalled: no tick has COMPLETED in 15
minutes and no job is running to explain it — a tick failing part-way.
The web process's supervisor also flips the public `/status` page's
scheduler row to an outage while the heartbeat is stale
(`status_manager.check_scheduler_liveness`); `/health` itself only reads.

1. `/admin` → Operations → Jobs shows every job in the registry with its last runs and
   state (ok / partial / failed / running / stuck / overdue / never), the
   heartbeat (`wedged` — a job past its own bound; `loop_stalled` — no tick
   completed with nothing running; `stale`), the lease, the last operator
   page, missed windows and requests.
2. The lease and the heartbeat are in the database:
   ```bash
   sqlite3 reviews.db "SELECT * FROM scheduler_lease;"
   sqlite3 reviews.db "SELECT * FROM scheduler_heartbeat;"
   ```
   The holder renews its lease every minute (`kept`=1); a kept lease silent
   for 4 minutes is taken over automatically, an unkept one after
   `SCHEDULER_LEASE_STALE_SECONDS` (30 min). A clean exit releases it
   (`ops.shutdown_scheduler`).
3. A dead scheduler thread is restarted by the supervisor, with its lease
   keeper, with backoff (Engineering → Overview → Scheduler shows the
   thread and its restarts). A thread stuck in a
   job is not restarted — a second loop would run every job twice; the
   lease keeper stops renewing once the job is past twice its bound, and a
   restart of the service is the fix.
4. A job that failed: `/admin` → Operations → Jobs, then **Run now** there. On Railway
   the run is handed to the scheduler (a request row it picks up on its
   next tick, under the lease); a request not taken in 30 minutes expires.
   A non-sending job that failed is retried by the loop after 10, 30 and 90
   minutes on its own.

**Never run the scheduler as a separate Railway service.** Volumes are not
shared between services, so it would schedule against an empty database
while the real jobs stopped. See `RAILWAY_SCHEDULER_SPLIT.md` (beside this file).

---

## The backup

Nightly at 2am Central (`scheduler.backup_db`), in order: a free-space check
(3.5 × the database), a consistent snapshot with `integrity_check` on the
volume (unredacted — the restore artifact), then a scrubbed, encrypted copy
to object storage and, when it fits, by email. **The run FAILS** — a failed
job, a page (at most every 12 hours) and a `/fail` ping to the dead-man
monitor — when the snapshot fails, there is no room, `BACKUP_ENCRYPTION_KEY`
is unset, or no off-site copy was made. One `backup_runs` row per run either
way.

- **State**: `/admin` → Engineering → Overview → Backups (`GET /admin/api/backup`; Operations → Jobs carries a summary) —
  `ok` / `stale` (the newest good snapshot is older than 26 hours) /
  `no_offsite` / `failed` / `never`, with the last error, the targets
  configured, the last 30 runs and the storage trend. `/health` reports
  `backup_stale` / `offsite_backup_stale`.
- **Object storage**: set `BACKUP_S3_ENDPOINT` (https), `BACKUP_S3_BUCKET`,
  `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY` (`BACKUP_S3_REGION`
  defaults to `auto` for R2). Give the key read and write on that one bucket
  only (the drill downloads), and add a lifecycle rule expiring objects after
  about 35 days. A single object is at most 5 GB (no multipart yet).
- **Email**: skipped above `BACKUP_EMAIL_MAX_BYTES` (25 MB of encrypted
  file); the skip is in the run's `offsite_error`.
- **Re-run**: `/admin` → Operations → Jobs → `backup_db` → Run now.

---

## Locked out of the console

The admin console needs a password, and — once enrolled or with
`ADMIN_REQUIRE_2FA=1` — the admin's own second factor. In order of reach:

1. **A lockout** (too many wrong passwords): an admin login is locked per
   address, so another network usually works. Otherwise:
   `railway ssh -- python3 scripts/unlock_login.py <username>` (clears the
   account lock and every per-address lock; `--dry-run` first), or set
   `LOGIN_UNLOCK_USERNAMES=<username>` in Railway, redeploy, sign in, **unset
   it**. A lockout expires on its own (5 minutes, 30, then 24 hours; the
   internal logins' daily cap, 60 minutes).
2. **A lost second factor**: use a backup code (they were shown once at
   enrolment). Otherwise set `ADMIN_2FA_RESET_USERNAMES=<username>`,
   redeploy — the login's own 2FA is turned off and its backup codes and
   trusted devices deleted — sign in, enrol again at `/admin/two-factor`,
   then **unset it** (it re-applies on every boot while set). If
   `ADMIN_REQUIRE_2FA=1` is on, the console will send you to enrolment.
3. **No admin login at all**: with no `is_admin` row, the boot seed
   (`auth.ensure_admin_login`) creates one from `ADMIN_USERNAME` (default
   `will`) and `ADMIN_PASSWORD` (it must pass the password policy), on its own
   "Cavnar AI Admin" restaurant row. It never runs while any admin exists,
   so renaming the admin cannot re-arm it.
4. **A session that expired mid-task**: admin sessions last 12 hours; the
   console's next read answers 401 and sends you to sign in. A sensitive
   action asks for the password again (15-minute step-up) and five wrong
   answers end the session.

---

## Something specific is broken

| Symptom | Where to look |
|---|---|
| AI answers failing | `/admin` → Operations → AI. `GET /admin/api/ai/health` shows each provider's breaker (this process's and the last recorded), the last hour's error rate, the last credential failure and the budgets; `ai_health_events` is the history. A 401 or an empty credit balance trips the breaker at once; an open breaker still open after 15 minutes pages. A tripped breaker clears itself; each vendor's **Reset** on that tab (`POST /admin/api/ai/reset-breaker {provider?}`, admins only) closes it now — in this process only. A budget stop (80% warns, 100% stops; trial $5/day and $50/month; Places has its own ceiling) needs the ceiling raised (`AI_*_BUDGET_USD`) |
| No email arriving | `/admin` → Operations → Email & SMS and `GET /admin/api/messaging/health` (problems, Resend/Twilio webhook health, email and SMS stats, outboxes, storm caps, a suppressed operator address). Check Resend's status. A suppressed address: the tab's suppression search (`GET /admin/api/suppressions?q=`) and **Reinstate** (`POST /admin/api/suppressions/reinstate {email, reason}`, audited) — also on the client page's *Suppressed addresses*. Operator addresses are never suppressed. Cavnar AI's own marketing is held until `CAVNAR_POSTAL_ADDRESS` is set |
| Texts not arriving | Operations → Email & SMS, `GET /admin/api/sms?restaurant_id=` — every attempt with its status (the number's last four only); `blocked` means consent or a STOP; account-level Twilio errors are captured once an hour |
| Push not arriving | `POST /api/account/send-test-push` — it reports Apple's own reason string. See the APNs notes in project memory |
| A client's alerts stopped mid-day | An alert storm cap (10 alerts in an hour caps the restaurant until its local midnight; health alerts still go). Lift it on the client page or on Operations → Push & alerts (`POST /admin/api/client/<rid>/storm-cap/lift`) |
| Some restaurants not fetched | `/admin` → Overview's *Needs you* list (`fleet:fetch_coverage`, and a client's `fetch_behind`). A bounded pass resumes from its cursor next slot; **Fetch reviews now** on the client runs one restaurant on the admin pool |
| A job failed | `/admin` → Operations → Jobs, then **Run now** there |
| A billing email never went | `/admin` → Customers → Billing → *Billing health* (`GET /admin/api/billing/health`) lists failed and waiting `owed_sends`; a client's own are on its Billing tab. To retry one after fixing the cause: `UPDATE owed_sends SET status='pending', next_attempt_at=datetime('now'), attempts=0 WHERE id=<id>;` — the `owed_sends` job drains it on its next tick (only on Railway; Run now from Operations → Jobs sends it at once) |
| Stripe and the console disagree | Billing health → *Nightly reconcile*: `billing_reconcile` holds what the `stripe_reconcile` job (3:30am CT, `billing_jobs.reconcile_stripe`) found; it never changes billing status itself — fix the account by hand (change plan, attach customer, lift hold), then run `stripe_reconcile` again from Operations → Jobs |
| A client is paused and cannot resume | A HOLD (`pause_reason` dispute, refund or admin). Only an admin lifts it: the client's Billing tab → **Lift hold** (`POST /admin/api/billing/<rid>/lift-hold {note, status?}`), which lifts every location held for the same reason |
| A console issue keeps coming back | Resolve is per OCCURRENCE: a newer occurrence of the same issue reopens it ("Reopened — … resolved it on M/D/YY"); a condition-level issue stays resolved while the condition lasts and lapses after 30 days; `scheduler`, `platform:error_rate`, `backup` and deletion requests cannot be resolved (409) — they clear themselves, or close another way (a deletion request by withdrawing it or finishing the offboarding). Resolutions written before 9/29/26 carry no occurrence and cover only occurrences that began before them |
| A provider key stopped working | Engineering → Overview → Providers (`provider_health`, probed hourly by the `provider_probes` job): Resend, Twilio, Anthropic, Stripe, Places, APNs. A provider that turns failing pages once |
| Telling clients something is wrong | Engineering → Status page: **Post an incident** naming the affected services (it holds them down on the public `/status`; the scheduler's checks rewrite an un-held service within minutes), post updates, **Resolve**. `/status` is served by this process — in a full outage use the external monitor's hosted page |
| A client is closing their account | Customers → the client → Access & activity → the *Offboarding* card (*Close this account* while a request is open; `GET /admin/api/client/<rid>/offboarding`). Mark each step done, or skipped with a note. Marking an acting step done carries it out, after the step-up: **integrations** clears every stored credential and turns outbound webhooks off; **Stripe** cancels the live subscription (`billing_jobs.cancel_subscription`) — 409 for a location billed under another's subscription (change the plan on the paying location, then skip) or on a server that is not production, 502 when Stripe refuses; **DocuSign** voids the open envelope — 409 on a server that is not production or when the envelope is already signed (skip it with that note), 502 when DocuSign refuses. **Withdraw** the request (409 if there is none, or it changed while you looked) or **Delete** once nothing is outstanding (409 with the steps left, or while an admin or support login calls it home). The checklist and the audit trail outlive the delete |

---

## Drill

The drill is a job: `scheduler.run_restore_drill`, run automatically on
the 2nd of Jan/Apr/Jul/Oct after the 2am backup, and on demand from
`/admin` → Jobs → **restore_drill**. It copies the newest snapshot to a
scratch file beside it and FAILS on a snapshot older than 26 hours, then
runs `integrity_check`, counts restaurants, reads the newest review and
alert, counts Google tokens in the snapshot against production (the
un-redaction proof), runs `init_db()` over the copy the way a real restore
does, and deletes the copy. Where object storage is configured it then
proves the off-site copy too: downloads the newest one recorded in
`backup_runs`, checks it against the SHA-256 it was uploaded with, decrypts
it and runs `integrity_check` and a restaurant count. It emails Will the
result either way. A failure is a failed job in `/admin` → Jobs like any
other. Run it by hand after any change to `backup_db`,
`_write_consistent_snapshot`, `offsite_backup` or the scrub registry.

Record the date of the last successful drill here:

- **Last drill:** 2026-09-20, **production**, by hand over `railway ssh`
  (the job above did not exist yet). Newest snapshot
  `cavnar_ai_backup_2026-09-20.db` (2.03 MB) copied to `/tmp` in the
  container; `PRAGMA integrity_check` → `ok`; 4 restaurants; newest review
  2026-09-20T01:34:54; newest alert 2026-09-16; **110 sessions and 1 device
  token present in the snapshot** — the un-redaction proof, since those are
  what `_redact_snapshot` deletes; Google tokens 0 in the snapshot and 0
  live (no Google connection in production yet, so that column proves
  nothing either way); `init_db()` migrated the copy and integrity was
  `ok` afterwards; scratch file removed. Next: the `restore_drill` job on
  2027-01-02, automatically. The off-site copy has never been drilled — run
  the job once object storage is configured.
- **Earlier:** 2026-09-20, **local** (not production). Newest local
  snapshot `cavnar_ai_backup_2026-09-19.db` (8.3 MB) copied to scratch with
  `-wal`/`-shm` removed; `PRAGMA integrity_check` → `ok`; 16 restaurants;
  `MAX(review_date)` 2026-09-19; `MAX(fired_at)` 2026-09-15; `init_db()` ran
  its additive migration over the snapshot and integrity was `ok` afterwards.
  What this did NOT prove: unredaction of OAuth tokens — the local database
  holds no `gmb_refresh_token` at all, so "0 kept" is not evidence either
  way. Superseded by the production drill above.
