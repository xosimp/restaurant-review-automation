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
| `500`, `"error": "db_unavailable"` or `"db_unreadable"` | The database (Postgres since 10/19/26) cannot be opened or read | [Database recovery](#database-recovery) |
| `500`, `"error": "schema_mismatch"` (with `missing_count`) | The database lacks a table this code expects, or a column of a sign-in table (`status_manager.STRICT_SCHEMA_TABLES`: users, sessions, memberships, the second factor, staff PIN and portal access, restaurants, the migration ledger) — an empty or old restore, a skipped migration. The names are in the deploy log, not the body | [Database recovery](#database-recovery) |
| `200`, `"status": "degraded"`, `schema_gaps` (with `schema_gaps_count`) | A column this code expects is missing from a table outside that list (drift: a column dropped by hand, a migration that never landed). Serving; the feature that reads it fails. The names are in the log (`health: the database lacks …`) — add the column (the boot's own `ALTER`), or redeploy so the boot does (blind re-audit 10/27/26) | [Something specific](#something-specific-is-broken) |
| `200`, `"status": "degraded"`, `not_production_env` | A Railway service that looks like production (a live Stripe key, `DATABASE_URL` on the production Postgres host, or `CAVNAR_PRODUCTION_SERVICE`) is not named `production` (`CAVNAR_ENV` unset and Railway's environment renamed, or a `CAVNAR_ENV` typo): its scheduler, every send and the paging stand down. The boot log's CRITICAL line says why. Set `CAVNAR_ENV=production` (or `staging` if it is not production) and redeploy | [Deploys](#deploys-the-branch-the-gate-the-window-the-smoke-release-safety-audit-102626) |
| `500`, `"error": "db_not_writable"` | The database refuses a write (read-only or failing storage, or a full volume) | [Volume full](#volume-full), then [Database recovery](#database-recovery) |
| `500`, `"error": "data_missing"` | The database holds no client restaurants, but the volume's marker says it held some — an emptied or replaced database | [Database recovery](#database-recovery) |
| `200`, `"status": "degraded"`, `problems` has `disk_low` / `disk_critical` | Volume filling or nearly full — **writes fail before reads do** | [Volume full](#volume-full) |
| `200`, `"status": "degraded"`, `scheduler_stale`, `scheduler_wedged`, `scheduler_stalled` or `jobs_overdue` | Web is fine, background jobs have stopped, are stuck in one job, or have fallen behind | [Scheduler stopped](#scheduler-stopped) |
| `200`, `"status": "degraded"`, `backup_stale` or `offsite_backup_stale` | No good backup (or no off-site copy) in 26 hours | [The backup](#the-backup) |
| `200`, `"status": "degraded"`, `db_busy` / `db_not_wal` | A write lock held past two seconds, or the database is not in WAL mode | [Something specific](#something-specific-is-broken) |
| `200`, `"status": "degraded"`, `constraint_missing` / `index_missing` (with `indexes_missing`) | The boot could not build a UNIQUE index over duplicate rows, or a new index ran past its build limit — the boot went on | [Deploys](#deploys-the-branch-the-gate-the-window-the-smoke-release-safety-audit-102626) |
| `200`, `"status": "ok"` | The platform is healthy; the problem is narrower | [Something specific](#something-specific-is-broken) |

Railway's deploy check needs only the 200, so a DEGRADED platform still
deploys (failing it would block the deploy that fixes it). The external
uptime monitor (below) asserts the keyword `"status":"ok"`, so degraded and
error both page. The 200 body also carries the write probe, disk (free,
total, database and WAL sizes and both thresholds), the scheduler heartbeat
age, backup and off-site ages, and the overdue jobs; it is cached 5 seconds.
Every answer names `build`: the commit, or `unknown` for a deploy that is not
one (`railway up`) — the post-deploy smoke fails on it.

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
   `WILL_PHONE` and every `OPS_ONCALL_PHONES` number (the channel that
   survives a Resend or APNs outage), an email to `WILL_EMAIL` and every
   `OPS_ONCALL_EMAILS` address, an event to the paging service when one is
   configured (`OPS_PAGER_KIND` / `OPS_PAGER_ROUTING_KEY` /
   `OPS_PAGER_WEBHOOK_URL` — PagerDuty or any incoming webhook; the platform
   check sends each condition as its own event (`sla:<condition>`, e.g.
   `sla:job_overdue:pos_sync`, `sla:disk_critical`) and RESOLVES it when the
   condition clears (re-audit B7, 10/27/26: one constant key, never resolved,
   let an acknowledged incident swallow every later urgent page); other pages
   are deduplicated on their key; acknowledgement and phone escalation live
   there, scale audit 10/19/26 OPS-4), and a push to the admin logins (a push
   proves nothing, so it does not count as delivered). Sent by the web process's supervisor thread
   every five minutes when something needs a person — a stale heartbeat, a
   wedged job or stalled loop, an overdue job, low or critical disk, a
   refused write, a stale / failed / off-site-less backup, a DSR night
   missing past its deadline, a retention window set under its floor
   (`retention_floor`), customer requests failing (a 5xx rate over
   `HTTP_5XX_ALERT_RATE`, 2%, across the last five minutes with at least
   `HTTP_5XX_ALERT_MIN_REQUESTS`, 50; below that floor, 5 SIGNED-IN customer
   5xx on 2 routes from 2 restaurants in ten minutes — unauthenticated traffic
   never pages) — and, as daily warnings, customer
   p95 over `HTTP_P95_SLO_MS` for fifteen minutes, the queue worker process
   silent for `QUEUE_WORKER_ALERT_SECONDS` (the web process has the queue),
   a bulk or POS queue row runnable but waiting past
   `QUEUE_BACKLOG_ALERT_HOURS`, and a job that hit its time bound on each
   of its last `BOUND_ALERT_RUNS` runs (it no longer covers every
   restaurant; scale audit 10/19/26 INF-4/6/7, OPS-7) — at most once an hour per problem, and only
   on Railway. The same unresolved problem repeats hourly until it is
   fixed. Every page is an `operator_alerts` row (Operations → Jobs shows
   the last one and whether it went). Owners are never paged.
2. **The dead-man ping** (`HEALTHCHECK_PING_URL`, e.g. healthchecks.io or
   Better Stack with a period of about 5 minutes and a grace of about 10):
   the scheduler pings at the end of every tick and during a long job that
   is inside its bound; the backup and the digest ping `/fail` when they
   fail. Its silence is what pages when the whole process is gone — nothing
   inside the process can. The cavnar-watchdog Worker (below) alerts on a
   `/fail` at the first check that sees it; until its 10/20/26 change
   (R8-13) the next ordinary ping overwrote the failure seconds later, so a
   `/fail` never alerted from outside.
3. **The external uptime monitor** on `https://dashboard.cavnar.ai/health`,
   every 1–5 minutes, alerting unless the body contains `"status":"ok"`,
   with SMS contacts. Its hosted status page is the one that survives this
   process; `/status` is served by the same process it reports on. The
   cavnar-watchdog Worker now does this too: a body without `"status":"ok"`
   on 5 of its last 7 checks (30 minutes of them — a flapping body too)
   alerts once, naming its `problems` (Audit #25 A07-09; re-audit B7).
4. **A paging service for the in-process pages** (`OPS_PAGER_KIND` with
   `OPS_PAGER_ROUTING_KEY` for PagerDuty, or `OPS_PAGER_WEBHOOK_URL`).
   Every degraded-platform page (overdue or failing jobs, the backup, the
   queue, the AI) goes from inside the web process over the app's own
   Twilio and Resend keys; set, the paging service is tried FIRST on every
   page and needs neither, so a page lands while a key is being rotated.
   Each page records which channels failed (`operator_alerts.channels`).
   **Set it** — until then a revoked Twilio and Resend key leaves only the
   admins' push.

Also: Railway's own deploy and crash notifications, and Sentry (handled and
unhandled 5xx are reported; the release is the commit).

---

## Platform down

Railway outage or a crashed process.

1. Check Railway's own status, then the service's Deployments tab.
2. If the last deploy is the cause, **roll back in Railway** — do not try to
   fix forward under pressure: [Rolling back a release](#rolling-back-a-release).
   The database is Postgres (since 10/19/26), its own Railway service, so a
   rollback changes the code and never the data; the old code runs on the
   newer schema because schema changes are additive (ARCHITECTURE_MANIFEST.md
   → *Release compatibility*). What a rollback does NOT undo is data the bad
   release wrote — that is [Database recovery](#database-recovery).
3. **With a volume attached, Railway runs one deployment at a time.** The
   old container is stopped before the new one boots, so a boot that fails,
   or a `/health` that does not answer 200 within the 120-second
   healthcheck, is **downtime until a redeploy or a rollback** — the
   previous container does NOT keep serving.

   **Every deploy is an outage for every restaurant** (one container, no
   replicas while a volume is attached — scale audit 10/19/26 INF-8), so
   deploys follow the restaurants' day, not ours:
   - **Do not deploy 5pm–2am CT** (service, the closes and the DSR sweep's
     nights: a deploy kills an in-flight sweep pass, whose nights resume on
     the next pass, but an owner mid-shift loses the app) **or 3–6am CT**
     (the POS chain on the "pos" lane and the 4–6am sweeps: an inline or
     lane job killed mid-run keeps its period claimed until
     `CLAIM_RECLAIM_MINUTES`, 120, so a job killed at 3:05 re-runs about
     5:05 — past the briefs' inputs).
   - **Deploy 9am–4pm CT**, after the 8am briefs and review fetch, and not
     within ten minutes of a slot a restaurant is waiting on.
   - Queue rows (generations, learners, the archive) are handed back at
     SIGTERM and resumed from their checkpoints — they are not the risk.
   - Each boot's length is `boot_events.boot_ms` (Engineering → Boots); the
     downtime is roughly the old container's stop plus that.
   - A fix that cannot wait is deployed anyway — the freeze is guidance, not
     a lock. The end of the outage is replicas with rolling deploys, which
     need no volume (docs/plans/POSTGRES_AND_WORKERS_PLAN.md).
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
   - **`DATABASE_URL` is set but unusable** (`config.database_url_problem`,
     blind re-audit 10/27/26): not a Postgres URL (a typo, an unresolved
     `${{Postgres.DATABASE_URL}}`), or a Postgres one in a process on the
     container with Railway's markers lost. It used to open the volume's
     SQLite file instead — the frozen pre-cutover copy. Fix the variable;
     `DB_BACKEND=sqlite` is the deliberate rollback to SQLite;
   - **Postgres held the clients and this boot would serve SQLite**: the
     volume marker says `"backend": "postgres"` and `DATABASE_URL` names no
     Postgres (deleted). Restore `DATABASE_URL`, or `DB_BACKEND=sqlite`;
   - **the marker cannot be read from object storage** (volume-less,
     `VOLUME_OPTIONAL`) **and the database holds no client restaurant** — the
     guard fails closed. Fix object storage; `ALLOW_EMPTY_DATABASE=1` for a
     database meant to be empty;
   - **another boot held the boot DDL lock past `BOOT_LOCK_WAIT_SECONDS`**
     (90): two booters on one Postgres run their DDL one at a time
     (`models.boot_ddl_lock`); a second one gives up rather than race. With no
     volume the old container keeps serving; redeploy once the other boot is
     done;
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
**Data loss:** none — the database is Postgres, a service of its own; the
volume holds the local backup snapshots, the volume marker and the frozen
pre-cutover `reviews.db`.
**Owners see:** the site not loading. `/status` is served by the same
process; point clients at the external monitor's hosted status page, or
post to them directly.

---

## Deploys: the branch, the gate, the window, the smoke (release-safety audit 10/26/26)

**The flow.** Work is pushed to `main`. The Railway `production` service
deploys the `production` branch (once Will points it there — until then it
deploys `main`): a release is a fast-forward of `production` to a `main` SHA
whose CI gate passed (`gate`, `gate-postgres`, `gate-boot`), through the promote
workflow, 9am–3pm CT, after a nightly that ran on that very SHA is green
(scripts/promote.py dispatches one when there is none) and, for a promotion
carrying an `ios/` change, a green iOS run on it (`--emergency` skips the
window, the nightly and the iOS run, never the gate). A Railway `staging`
environment deploys `main` (below). Once production deploys `production`,
the season-refresh bot's commits wait for the next promotion; until then
they deploy at once, which is why the bot runs at 14:00 UTC (9am CT).

**The switch, in order (Will; record the date each was verified here).**
1. Push the merged round to `main` and wait for green `gate`,
   `gate-postgres` and `gate-boot`; run nightly.yml and ios.yml once by
   hand on it.
2. Create `production` at the SHA production runs now
   (`git push origin <deployed sha>:refs/heads/production`).
3. Point Railway's production service at the `production` branch.
4. Protect `main` and `production`: require the `gate`, `gate-postgres` and
   `gate-boot` checks; on `production` let only GitHub Actions push.
5. Set the repository variable `PROMOTION_LIVE=true`: ci.yml's interim
   full-suite jobs on every push to main stop, and the nightly's
   `containment` check turns from a warning into an error.
6. Create the smoke tenant and set the `SMOKE_TOKEN` (and
   `SMOKE_MOBILE_TOKEN`) secret, and `SMOKE_ALERT_URL` (an ntfy topic or a
   paging webhook): a production smoke without the token fails.
Until step 3, every push to `main` deploys at once and ci.yml also runs the
full SQLite and Postgres suites on it.

**Staging.** One Railway environment named `staging` (its own small Postgres,
no provider keys or test keys only, `CAVNAR_ENV=staging`). The code holds it:
`config.deploy_env()` is `staging`, so the scheduler never starts
(`config.scheduling_allowed()`) and every send point refuses
(`config.outbound_sends_allowed()`: `emails.deliver`, the Twilio send, APNs
and Web Push, Stripe with a live key, DocuSign outside its demo account, Google
review replies, their retraction, posts and listing edits, customer webhooks,
the RPOWER and Toast schedule pushes, every Meta Graph write) — whatever keys a
"duplicate environment" copied in. Each refusal is counted (`/health`'s
`sends_refused`), and a production-looking Railway service that is not named
`production` is `/health`'s `not_production_env` problem, so say
`CAVNAR_ENV=staging` on staging (blind re-audit 10/27/26). `ALLOW_NONPROD_SENDS=1` lifts the send gate for a
staging that holds only test keys and test addresses; `ALLOW_LOCAL_SCHEDULER=1`
starts its scheduler. Production needs no new variable: Railway names its
environment `production`.

**Shutdown.** Railway sends SIGTERM and kills the container
`RAILWAY_DEPLOYMENT_DRAINING_SECONDS` later — **and its default is 0**: unset,
the SIGKILL follows the SIGTERM at once. Set it on the web service:
`RAILWAY_DEPLOYMENT_DRAINING_SECONDS=60`. At the SIGTERM the queue worker (and
a scheduler process) start handing their rows back and releasing the lease
AT ONCE, in parallel with the web worker's `graceful_timeout` (30 s) to finish
its requests and release the scheduler lease, and the web worker stops
claiming queue rows (`gunicorn.conf.py`: `_begin_drain`, `post_worker_init`).
The budget is the longer of the two plus a margin — `SHUTDOWN_SECONDS`, 35 s,
inside the 60 (held by `tests/test_boot_ddl_rules.py`; blind re-audit
10/27/26, which also put graceful back to 30 from 20: 30-60 s Ask requests
were killed at every deploy). The boot reads the setting: on Railway a drain
that is unset or under the budget is an ERROR at every boot
(`deploy shutdown budget: …`) and `/health`'s `shutdown {drain_seconds,
needs_seconds, fits}` says so (never a problem — not an outage until a
deploy). A shorter drain kills the hand-back: rows wait out their lease and
the next scheduler waits 4 minutes for the lease.
`boot_events.previous_exit` says whether each exit was clean.

**After every deploy** (`.github/workflows/release.yml`, on Railway's
deployment-success event): `scripts/smoke.py` checks that `/health` names the
deployed SHA (`build`, never `unknown`) and calls itself `production` (`env` —
anything else would mean its scheduler and sends stand down), `/login` renders, its static files
resolve, and — with the `SMOKE_TOKEN` secret, the smoke tenant's session token —
one web read and one mobile read answer. A failure fails the run (GitHub emails
Will) and prints the rollback step. Hourly, the same workflow fails when the
build `/health` names has lagged the deploy branch's head for over an hour.
In the process, `platform_monitor.release_health` compares the new
DEPLOYMENT's first 15 minutes (Railway's `RAILWAY_DEPLOYMENT_ID`, the SHA
alongside — so a redeploy for a variable change and a rollback are checked
too; re-audit B7, 10/27/26) with the previous deployment's last hour (failed
boots, each counted once; customer 5xx and p95; jobs that crashed after a
clean run; each queue kind's failed share; AI provider errors; a scheduler
tick) and pages once per deployment: **roll back** on an urgent row, **hold
the next deploy** on a warning. A rollback is advised only for a regression
that starts with the deployment — a rate against the deployment before it,
with absolute minimums — and never for a provider seen failing at the same
time (`provider_health`: RPOWER for the POS rows, Anthropic for the AI
errors), which reads as a hold: "the provider, not the release". A boot over
60 s, or 50% over the median of the last ten, pages once per build too.

**Emergency deploys come from a pushed commit, never from `railway up`.**
`railway up` uploads the working directory — uncommitted edits included — and
names no commit: `/health` reads `build: "unknown"`, Sentry has no release,
the smoke test fails, and a later rollback to it hung (10/22/26). When the
GitHub integration is down: `git worktree add /tmp/release <pushed-sha>`, set
`GIT_COMMIT=<sha>` on the service, and `railway up` from that clean worktree;
otherwise `railway redeploy` a git-sourced deployment.

**Schema changes at boot** follow the rules in ARCHITECTURE_MANIFEST.md →
*Release compatibility*: additive only; an index on a table production
already has is built first with `scripts/build_index.py` (CONCURRENTLY) and
listed in `migrations/prebuilt_indexes.txt`; a data rewrite runs once
(`data_migrations`). A boot that cannot build a UNIQUE index over duplicate
rows, or a new index past its build limit (`PG_BOOT_INDEX_TIMEOUT_MS`, 60 s),
goes on and `/health` says `constraint_missing` / `index_missing` (degraded,
with `indexes_missing`): dedupe (read-only count first) and redeploy, or build
the index with the script. So does an INVALID index (a failed `CREATE INDEX
CONCURRENTLY` leaves one, and the boot used to count it as built): rebuild it
with `scripts/build_index.py "<the boot's CREATE statement>" --apply
--rebuild-invalid` (drops it CONCURRENTLY, builds it again).

**No `railway ssh` session (psql, a long report) open during a deploy.** A
boot `ALTER` waits for its table's lock 3 s a try, eight tries (about 40 s),
then fails the boot — on purpose for the sign-in tables, whose missing
column would fail every sign-in. A session holding `sessions` or `users`
open is the likeliest way to turn a deploy into an outage.

**Every push boots on the previous release's database** (blind re-audit
10/27/26, B5 P1): `scripts/upgrade_boot_check.py` (the gate runs it) boots the
previous release — `origin/production`, else the merge-base with `main`, else
`HEAD~1` — on a fresh SQLite and a scratch Postgres 16, seeds every table,
boots this code on it twice (`/health` 200, no schema gap, no missing index,
no failed boot statement or run-once migration), then boots the previous
release on the result (the rollback path), and holds the prebuilt-index rule
against that release itself. To run it by hand before a risky deploy:
`python scripts/upgrade_boot_check.py --database-url
postgresql://postgres@127.0.0.1:55433/postgres` (a scratch server on this
machine; never production's). Exit 2 means it could not run (no base commit —
fetch the history — or the previous release itself would not boot here).

### Retiring the volume (Will's step; the code is ready — release-safety audit 10/26/26, P3)

The volume is what makes every deploy an outage: with it attached Railway
runs one deployment at a time. Since the Postgres cutover it holds only the
local backup snapshots, the volume marker, the restore guard's files and the
frozen pre-cutover `reviews.db`. With `VOLUME_OPTIONAL=1` the app runs
without it: the markers live in object storage (`marker_store`, the blob
bucket or else the backup bucket, under `cavnar-markers/`; a read that fails
is never "no marker" — the restore freeze reads as last read, else frozen, and
the restore guard neither compares nor writes an epoch it could not read;
re-audit B7, 10/27/26), the backup's local
snapshot is scratch and the encrypted off-site copy is the backup (the run
still FAILS when no off-site copy was made), and the monthly drill proves the
off-site copy. `models.require_volume` refuses a volume-less boot unless the
database is Postgres and object storage is configured, and never with
`BLOB_STORE=local`.

1. Confirm `/health` shows `backup.offsite` ok and the off-site drill passed
   this month (Engineering → Restore drill). Download the frozen
   `reviews.db` and the newest local snapshot from the volume to encrypted
   storage of your own (the pre-cutover record; nothing reads them).
2. Set `VOLUME_OPTIONAL=1` on the web service and redeploy WITH the volume
   still attached: nothing changes yet (a mounted volume is still used), and
   the boot proves the variable is accepted.
3. Detach the volume from the web service in Railway, and redeploy. The boot
   log says `VOLUME_OPTIONAL: running on Railway with no volume`; once the
   boot is ready `cavnar-markers/.cavnar-volume.json` and, after the first
   scheduler tick, `cavnar-markers/.restore_epoch` appear in the bucket (the
   guard baselines on its first tick, never freezes).
4. **Deploys now overlap — that is Railway's default without a volume**:
   the new container boots, and must pass `/health`, while the old one still
   serves. What makes that safe, all in code (blind re-audit 10/27/26 RF6):
   every boot statement commits on its own (A05-16); no index is built at
   boot on an existing table (A05-04, held per push by the upgrade boot
   check against the previous release); boot DDL runs ONE booter at a time
   (`models.boot_ddl_lock` — a Postgres advisory lock held for the whole
   `boot_schema`; a second booter waits up to `BOOT_LOCK_WAIT_SECONDS`, 90,
   then fails its boot, and the old container keeps serving); and the
   guards that read object storage fail closed (an unreadable restore
   freeze holds scheduling and sends; an unreadable volume marker refuses a
   boot with no client restaurants). What is NOT free: a release that adds a
   column to a hot table (`sessions`, `users`) queues that table's readers
   behind its `ALTER` for up to 3 s a try while the old container serves —
   deploy those in a quiet hour, and keep `/health` 200 through the deploy.
5. Rollback: re-attach the volume (mount path unchanged) and redeploy; unset
   `VOLUME_OPTIONAL`.

Limits: the backup bucket's lifecycle rule expires an object nothing
refreshed — a restore freeze left unconfirmed past it lapses; give the
marker prefix its own rule or use the blob bucket.

---

## Rolling back a release

The way back from a bad release. Safe because the schema is additive and data
formats are read one release before they are written (ARCHITECTURE_MANIFEST.md
→ *Release compatibility*), and every push proves it: the upgrade boot check
boots the previous release on the upgraded database and requires its
`/health` 200. Rehearsed each quarter (below).

1. Railway → the `web` service → **Deployments**. Find the last deployment
   before the bad one that was healthy (its build SHA is in its details; the
   page that paged you names the bad build).
2. Its **⋮ → Redeploy**. Railway builds nothing new: it starts the old
   image. With the volume attached the bad container stops first — a minute
   or two of outage.
3. Watch `/health` until it answers 200 with `build` equal to the old SHA
   (`python scripts/smoke.py https://dashboard.cavnar.ai --expect-sha <old-sha>`).
4. Stop the cause from coming back: move the `production` branch back
   (`git push --force-with-lease origin <old-sha>:production`) or revert on
   `main` and promote, so the next deploy is not the bad one again.
5. If the bad release wrote bad data, the rollback did not undo it:
   [Database recovery](#database-recovery).

A rollback of a `railway up` deployment can hang at "fetching snapshot"
(10/22/26): roll back to a git-sourced deployment instead.

---

## Background work after a restart (the job queue, 10/10/26)

Schedule generations, auto-drafts and the nightly learners' per-restaurant
passes are rows in `jobs` (`job_queue.py`). A deploy hands the running rows
back at SIGTERM and the new process resumes them — a generation that had
saved its draft finishes with it, one that had not re-reads the model
answers it already paid for — so after a restart nothing needs pressing
again. To see them: `SELECT id, kind, restaurant_id, state, attempts, error
FROM jobs WHERE state IN ('queued','running','failed') ORDER BY id DESC`.
A row stuck `running` with a lease in the past is claimed by the next
consumer within a minute (the `job_queue_tick` minute duty). The queue
cannot be turned off any more (`JOB_QUEUE=0` and the in-process paths it
switched back to are gone — blind re-audit 10/11/26 ARCH-10); a queue worker
process that misbehaves is turned off with `QUEUE_WORKER=0` (the web process
consumes the queue again). The queue worker recycles itself after
`QUEUE_WORKER_MAX_JOBS` generations (bulk rows no longer count) or
`QUEUE_WORKER_MAX_RSS_MB`; it tells gunicorn's master when it stops
claiming, and its replacement starts then, beside it, without waiting for a
web boot that already happened (scale audit 10/19/26 INF-4, A5-F3 — until
then a recycled worker never started again and the web process silently
took the queue). If it goes silent anyway the platform check warns
("The queue worker process has not beaten its heartbeat"): read the logs for
`queue worker exited` / `no web process booted`. Per kind, what is queued,
how long the oldest runnable row has waited and what failed or expired in a
day is `job_queue.stats()["kinds"]`. Nightly rows (archive, learners,
diagnoses) expire unstarted after `QUEUE_BULK_DEADLINE_HOURS` (22) instead
of stacking under the next night's. The nightly backup and the monthly
restore drill are queue rows too (`scheduled_job`, the one-at-a-time
`maintenance` group — scale re-audit 10/20/26 R8-03): a deploy mid-backup
hands the row back and the next process starts the backup again from the
beginning (a crashed run's work files are cleared first). The nightly
business metrics (`business_metrics`, 11:50pm CT) are one too since admin
audit 10/27/26 P-16 — a whole-fleet build that ran on the scheduler thread —
and run for the night that was queued (`jobs_registry` `period_kwarg`), a
night the loop queued late included. The queue claims
nothing while `MAINTENANCE_READ_ONLY` or `RESTORE_FROM` is set or the
restore guard holds a restored database (`job_queue.frozen`, R8-05). The
Postgres cutover and its rollback are `docs/ops/POSTGRES_CUTOVER.md`.

## Database recovery

**Production has run on Postgres since 10/19/26** (the Railway service
`Postgres`, Postgres 18, private URL `postgres.railway.internal`, through the
web service's `DATABASE_URL=${{Postgres.DATABASE_URL}}`;
docs/ops/POSTGRES_CUTOVER.md). The volume's `reviews.db` is the SQLite
database as it stood at the switch (18:45 CT) and **never changes**: every
`sqlite3 /app/data/reviews.db` read answers about 10/19/26, not about now —
`integrity_check` on it always says ok. The Postgres path comes first below;
the SQLite steps are kept under [Rolling back to SQLite](#rolling-back-to-sqlite)
(scale re-audit 10/20/26 R8-16).

`/health` returns 500 with a database error code (`db_unavailable`,
`db_unreadable`, `db_not_writable`, `schema_mismatch`, `data_missing`), or
data is wrong (a bad deploy or a bad hand-run statement wrote over it).

### Where the copies are, best restore point first

The workspace is on Railway Pro (10/19/26):

1. **Point-in-time recovery on the `Postgres` service** — the first restore
   path when it is on: Postgres archives its write-ahead log
   (`archive_mode=on`, measured 10/20/26), so a restore can stop at any
   moment — minutes before a bad write, not the night before. **RPO:
   minutes.** Check it is really on before you need it: Railway → Postgres →
   Backups shows point-in-time restore, and `SELECT archived_count,
   last_archived_time, failed_count, last_failed_time FROM pg_stat_archiver;`
   shows WAL leaving the server (a `failed_count` that grows, or a
   `last_archived_time` hours old, is an archiver that has stopped — the WAL
   then stays on the volume, which the Postgres volume check sees growing).
2. **Railway's volume backups on the `Postgres` service** — scheduled on its
   *Backups* tab: **daily, kept 6 days**, and **weekly, kept 1 month**.
   Volume snapshots: a restore comes back as of the snapshot's time.
   **RPO: up to 24 hours.**
3. **The nightly export + the encrypted off-site copy** (`backup_db`, 2am CT;
   *The backup* below) — a SQLite export of every Postgres table on the app
   volume and a sealed, Fernet-encrypted copy in R2 (and by email when it
   fits), kept 35 days. **The one copy that survives losing the Railway
   project or account.** RPO: up to 24 hours. Loaded back into an empty
   Postgres with `scripts/run_pg_copy.py` (the boot then unseals its
   credentials in place).

Paths 1 and 2 live in Railway; neither survives losing the project. POS data
since the restore point re-pulls from RPOWER; owner edits, published
schedules, uploaded invoices, ratings and DSR closeouts since it do not come
back. Pro also lets volumes grow to 1 TB (self-serve resize) — the first fix
for a [Volume full](#volume-full).

### P1. Confirm it is really the database

From your laptop, with the Railway CLI linked: `railway connect Postgres`
opens `psql` on it. Then:

```sql
SELECT 1;
SELECT pg_size_pretty(pg_database_size(current_database()));
SELECT COUNT(*) FROM restaurants;
SELECT MAX(review_date) FROM reviews;
SELECT * FROM pg_stat_archiver;
```

A connection that is refused or a server out of space is the Postgres
service, not the data: Railway → Postgres → Deployments and Metrics (a full
volume: grow it, *Volume full* below) — no restore. Data that is present
but wrong is a restore to just before it went wrong (P2).

### P2. Freeze the platform BEFORE any Postgres restore

A Postgres restore swaps the database under the running web service. Without
a freeze, everything since the restore point runs again: that morning's day
claims (`job_period_claims`) are gone, so the briefs, digests, DSR deliveries
and labor reminders send a second time; `owed_sends` rows pending at the
restore point go out again (welcomes, receipts, dunning); queued `jobs` rows
run again — staff schedule notices, auto-publishes, a Monday bar price push
that puts an older price back on RPOWER (scale re-audit 10/20/26 R8-04).

1. Railway → the web service → Variables: set `MAINTENANCE_READ_ONLY=1` and
   let it redeploy. The scheduler stands down, the job queue claims nothing
   (`job_queue.frozen`), and no admin action sends email or texts
   (`config.scheduling_allowed()` is false). Pages still load.
2. Only then restore (P3).

The backstop if this step is missed: **the restore guard** (`restore_guard.py`).
The scheduler's tick advances a counter in the database (`job_cursors`
`restore_epoch`) and copies it to the app volume (`.restore_epoch`). A
database whose counter is behind the volume's was put back under the running
service: the next tick (within a minute) writes `.restore_freeze` beside the
database, stops scheduling, the queue and admin sends exactly as
`MAINTENANCE_READ_ONLY` does, and pages (`restore_detected`) — until P4's
confirm. It cannot stop what ran between the restore and that tick.

### P3. Restore

- **Point in time:** Railway → Postgres → Backups → restore to a point in
  time, the minute before the damage (`MAX(...)` of the table it hit tells
  you when). Railway restores into the service, or into a new one — if a new
  one, point the web service's `DATABASE_URL` at it (a reference variable,
  `${{<service>.DATABASE_URL}}`) before lifting the freeze. **Verify the exact
  steps at the quarterly game day below** — the UI is Railway's, not ours.
- **A volume backup:** Railway → Postgres → Backups → pick the backup →
  Restore. Railway stages it and the Postgres service redeploys on it.
- **The off-site copy** (the Railway project is gone, or both of the above
  are): download, check and decrypt it ([Restoring from the object-storage
  copy](#restoring-from-the-object-storage-copy-step-by-step)), prepare it
  (`scripts/prepare_restore.py <file> --encrypted` unseals it), create an
  EMPTY Postgres, then
  `python3 scripts/run_pg_copy.py --sqlite <prepared.db> --database-url <new url>`
  (docs/ops/POSTGRES_CUTOVER.md has the copy's flags and its verify report).
  Only ever into an EMPTY database: a second run into a partly filled one
  was never proved safe (a table without a primary key would duplicate).
  Point `DATABASE_URL` at it.

**Never `RESTORE_FROM` on Postgres.** It swaps the SQLite file the app does
not read: it now refuses and fails the deploy (scale re-audit 10/20/26 R8-12)
unless `DB_BACKEND=sqlite` is set too — which is the rollback to SQLite, below.

### P4. After a Postgres restore — reconcile, confirm, unfreeze

With `MAINTENANCE_READ_ONLY=1` still set, in `psql` (`railway connect
Postgres`), with `T` the restore point in UTC:

1. **Day claims** — every job that ran since `T` must not run again today:
   `SELECT job_key FROM job_period_claims ORDER BY claimed_at DESC LIMIT 50;`
   then for each digest, brief or DSR delivery that already went today:
   `INSERT INTO job_period_claims (job_key) VALUES ('weekly_digests:YYYY-MM-DD') ON CONFLICT DO NOTHING;`
2. **Owed sends** — rows the restored database has as pending that went out
   after `T` (Resend's log and `email_log` on the console say which):
   `UPDATE owed_sends SET status='cancelled' WHERE id IN (...);`
3. **Queued jobs** — rows queued before `T` that already ran after it:
   `SELECT id, kind, restaurant_id, enqueued_at FROM jobs WHERE state IN ('queued','running') ORDER BY id;`
   cancel the ones that send or write outward (`schedule_notices`,
   `auto_publish`, `bar_price_push`; a `pos_onboard` or `pos_archive` row
   only re-reads, it may stay):
   `UPDATE jobs SET state='cancelled', error='ran before the restore' WHERE id IN (...);`
4. **Confirm the restore guard** (only if it froze — the console's Engineering
   cards and `/health`'s scheduler say so, or `railway ssh -- python3 -m restore_guard`
   prints `frozen`): `railway ssh -- python3 -m restore_guard confirm`. It
   re-baselines the volume's copy to the restored database's counter.
5. Remove `MAINTENANCE_READ_ONLY`. The next tick runs; then work through
   [After any restore](#after-any-restore).

---

## Rolling back to SQLite

The SQLite steps below are the rollback path (`DB_BACKEND=sqlite`, losing
what Postgres took since — POSTGRES_CUTOVER.md → *Rollback*) and the way an
off-site copy is opened and checked on any backend. Every `sqlite3 /app/data/reviews.db`
read here is about the SQLite file — on Postgres that is the frozen cutover
file, so use `psql` (P1) for anything about production now.

### 1. Confirm it is really the database

```bash
railway ssh -- sqlite3 /app/data/reviews.db "PRAGMA integrity_check;"
```

`ok` means the file is fine and the problem is elsewhere.

### 2. Find a snapshot

Three copies exist, newest first in the backup ledger
(`SELECT id, finished_at, local_ok, offsite_ok, offsite_target, sha256 FROM backup_runs ORDER BY id DESC LIMIT 5;`,
or `/admin` → Engineering → Overview → Backups, `GET /admin/api/backup`):

- **The local snapshot** on the volume, taken at 2am — a `VACUUM INTO`
  copy, integrity-checked, then gzipped and read back:
  `cavnar_ai_backup_YYYY-MM-DD.db.gz`. The newest `BACKUP_RETAIN_COUNT`
  (3) are kept (AI cost audit 10/7/26 #8; it was seven days of
  uncompressed `.db` files — any `.db` still there is from before the
  change, restores the same way and is pruned first):

  ```bash
  railway ssh -- ls -lh /app/data/backups/
  ```

  To open one by hand, decompress it first (`gunzip -k <file>.db.gz`, or
  `scheduler.gunzip_snapshot(src, dest)` in the container); `RESTORE_FROM`
  takes the `.gz` as it is.

  **The local snapshots are complete and directly restorable.** They are not
  redacted — the scrub applies only to copies that leave the server. This
  was fixed in audit #21; snapshots taken *before* that fix have credentials
  stripped, and restoring one means re-authorizing every integration by
  hand (see [After any restore](#after-any-restore)).

- **The object-storage copy** (Cloudflare R2 / S3, `BACKUP_S3_*`) —
  `s3://<bucket>/<prefix>cavnar_ai_backup_YYYY-MM-DD.db.enc`, the prefix
  `cavnar-backups/` by default. Use it when the volume itself is gone.
- **The emailed copy** (to Will, when it fits in 25 MB) — the same
  encrypted file as an attachment.

Both off-site copies are **scrubbed of live access** (sessions, 2FA backup
codes — an internal login's own too — authenticator secrets, trusted
devices, device tokens, staff-portal links, view-as sessions, admin job
results (`async_jobs`), reset tokens) and **keep what a restore needs
sealed under `CREDENTIAL_KEY`** (scale audit 10/19/26 OPS-1): every OAuth /
POS / reservation / back-office credential (already that ciphertext at
rest), the link-signing secrets in `app_secrets`, customer webhook secrets,
the Stripe customer id and each live invoice intake address —
`DATABASE_SCHEMA.md` → *What leaves the server in a backup*; the backup email
lists what that night's scrub emptied, blanked and sealed. The file is then
**Fernet-encrypted** with `BACKUP_ENCRYPTION_KEY`, one token per 3 MB chunk,
one per line — so a reader of an off-site copy needs both keys. A night with
no working `CREDENTIAL_KEY` scrubs the sealed values instead (the old
behaviour), and a restore from that copy means re-entering them by hand.

**Key escrow.** `BACKUP_ENCRYPTION_KEY` must be kept somewhere other than
Railway (a password manager): without it no off-site copy can be read, and a
lost Railway project takes the key with it. Keep `CREDENTIAL_KEY` beside it:
a restored local snapshot holds credentials encrypted with it, and an
off-site copy's sealed values open only with it. **Without the escrowed
`CREDENTIAL_KEY` an off-site restore severs every integration** (each sealed
value is blanked, never served as ciphertext). After a key rotation the previous key is needed too until the boot log says
`unreadable: 0` (`CREDENTIAL_KEY_PREVIOUS`, `docs/ops/SECURITY.md` → Data at
rest): a snapshot taken before the rotation was sealed with the old one.

#### Restoring from the object-storage copy, step by step

1. Find the newest good copy and its checksum — in `psql` on Postgres
   (`railway connect Postgres`; on the SQLite rollback, `sqlite3 /app/data/reviews.db`):
   `SELECT finished_at, offsite_target, sha256 FROM backup_runs WHERE offsite_ok=1 ORDER BY id DESC LIMIT 3;`
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
   (step 4 below), with the escrowed `CREDENTIAL_KEY` set on the service.
   The restore unseals the copy's credentials, link secrets, webhook
   secrets, Stripe customer ids and intake addresses before the swap
   (`db_restore` → `offsite_backup.unseal_file`; the deploy log says how
   many opened and how many could not). Into Postgres instead
   (`scripts/run_pg_copy.py`): the next boot unseals them in place. Then work
   through [After any restore](#after-any-restore): sessions and devices are
   gone (everyone signs in again), integrations are not — unless the log
   says values could not be opened (a wrong or lost `CREDENTIAL_KEY`), in
   which case those are blank and are re-entered by hand as before.

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
   `RESTORE_FROM=/app/data/backups/cavnar_ai_backup_YYYY-MM-DD.db.gz` (or a
   plain `.db` — an older local snapshot, or the decrypted off-site copy's
   path on the volume).
   Saving it redeploys. The new process swaps the snapshot in before its
   first connection: a gzipped one is decompressed beside the database
   first (`reviews.db.restoring` — room for one database is needed); it
   checks the snapshot (integrity and a `restaurants`
   table), keeps the old file and its `-wal`/`-shm` as
   `reviews.db.broken-<time>`, copies the snapshot in, and checks the
   restaurant count matches. A snapshot that fails a check fails the deploy
   and changes nothing — and with a volume attached a failed deploy is
   downtime until you roll back or fix the variable.
2. While `RESTORE_FROM` is set **the scheduler does not run**, the job
   queue claims nothing (R8-05), and no admin action sends email or texts
   (`scheduler.scheduling_allowed()` is false):
   no briefs, digests, alerts or reset links go out of a database nobody has
   looked at yet. A marker beside the database stops a later boot from
   restoring the same snapshot again over new writes.
3. Confirm (step 5), then **delete `RESTORE_FROM`**. That redeploys with the
   scheduler back on.

`init_db()` and `ensure_columns()` run right after the swap and additively
migrate an older snapshot forward — that path is exercised on every deploy.

**A large restore: prepare it out of band first** (scale audit 10/19/26
OPS-3). The steps above run before gunicorn binds, inside the 120-second
healthcheck; the monthly drill times them and says when they no longer fit.
Then, while the old process still serves:

```bash
railway ssh -- python3 scripts/prepare_restore.py /app/data/backups/cavnar_ai_backup_YYYY-MM-DD.db.gz
# an off-site copy, downloaded onto the volume (step 2 above):
railway ssh -- python3 scripts/prepare_restore.py /app/data/restore.enc --encrypted
```

It decompresses or decrypts, integrity-checks and unseals into
`/app/data/restore_prepared.db` (room for one more database is needed) and
writes `restore_prepared.db.prepared.json`; it never touches the live
database. Set `RESTORE_FROM=/app/data/restore_prepared.db`: the boot checks
the file's size and mtime against the record and **renames** it into place —
seconds, whatever the size. A file that changed since it was prepared is
refused and nothing changes.

Manual fallback, only with the service stopped (scale it to 0 in Railway
first, so nothing can open the file):

```bash
railway ssh -- mv /app/data/reviews.db /app/data/reviews.db.broken-$(date +%s)
railway ssh -- rm -f /app/data/reviews.db-wal /app/data/reviews.db-shm
railway ssh -- sh -c 'gunzip -c /app/data/backups/cavnar_ai_backup_YYYY-MM-DD.db.gz > /app/data/reviews.db'
```

(an older plain snapshot: `cp` it instead of `gunzip -c`).

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
| Owner sessions | Valid (local snapshot, Railway volume backup) or gone (off-site copy) | If gone, tell clients to sign in again |
| Push device tokens | Same | They re-register on next app launch; `send-test-push` to confirm |
| Google / Toast / Square / Clover / RPOWER / Instagram / reservations / Back Office | Kept (local, Railway volume backup); kept sealed in an off-site copy and opened on restore with the escrowed `CREDENTIAL_KEY` (OPS-1) | Only if the restore log says values could not be opened (or the copy predates 10/19/26): each owner reconnects in Account → Connections (RPOWER and Back Office are re-entered by Cavnar in the console) |
| Link-signing secrets (`app_secrets`) | Kept (local); kept sealed in an off-site copy since 10/19/26 | Only if they could not be opened: new kept secrets are minted, and pay links, table-tent join QR codes, issue links and unsubscribe links issued before the backup no longer verify — resend pay links and reprint QR codes |
| Customer webhook secrets | Kept (local); kept sealed (off-site) | Only if blanked: owners re-save their outbound webhook |
| Invoice intake addresses | Kept (local); the live route kept sealed (off-site) | Only if blanked: make a new one (Customers → the client → Data & integrations → Invoice intake) and give it to Fintech and any forward |
| Stripe | `stripe_customer_id` kept (local) or sealed (off-site) | Only if blanked: re-link it in the console (Customers → the client → Billing → **Attach Stripe customer**, with the `cus_` id from the Stripe dashboard), then run the `stripe_reconcile` job (Operations → Jobs → Run now) so the subscription mirror is refilled |
| Data since the snapshot | Lost | Reviews re-fetch on the next pass (`UNIQUE(restaurant_id, platform, external_id)` makes that safe). Uploaded CSVs and manual edits do not come back |
| Job claims | Restored to snapshot state | Jobs already run that day **may run again**. Check `job_period_claims` before a digest hour |
| Owed billing emails | Restored to snapshot state | Check `owed_sends` for rows the snapshot has as pending that already went (sent after the snapshot) and mark them `cancelled` before you lift the freeze (`RESTORE_FROM`, `MAINTENANCE_READ_ONLY`): the `owed_sends` job drains due rows on every tick once the scheduler is back |
| Queued jobs | Restored to snapshot state | Rows queued before the restore point that already ran after it run again: cancel the ones that send or write outward (`schedule_notices`, `auto_publish`, `bar_price_push`) — P4 above |
| The restore guard | Frozen, if the database went back under the running service | `railway ssh -- python3 -m restore_guard confirm` once the rows above are reconciled (a `RESTORE_FROM` restore re-baselines it itself) |

The job-claims row is the one most likely to cause a visible mistake:
restoring a 2am snapshot at 9am can re-send that morning's digests. To
suppress, in `psql` on Postgres or `sqlite3` on the SQLite rollback (the
same statement works on both):

```sql
INSERT INTO job_period_claims (job_key) VALUES ('weekly_digests:YYYY-MM-DD') ON CONFLICT DO NOTHING;
```

### Cold sale lines (RPOWER audit #23 A5-09)

Sale lines (`pos_ticket_lines`) older than `RETAIN_POS_LINES_HOT_DAYS` (400)
are not in the database once the cold tier runs (`pos_cold.tier`, inside the
nightly retention pass, only with `BLOB_STORE=s3` and `BACKUP_ENCRYPTION_KEY`
set). Each restaurant-month is one object in the blob bucket,
`pos-cold/pos-lines/<restaurant>/<provider>/<YYYY-MM>-<sha12>.jsonl.gz.enc`
(gzip JSON lines, Fernet under `BACKUP_ENCRYPTION_KEY`), indexed by
`pos_cold_exports` (never pruned; it rides in every backup). A month was
deleted only after its object was read back and its SHA-256 and row count
matched, and after its days were rolled into `pos_item_daily` (which every
owner view of an old month reads). So:

- A database restore loses no cold month: the index comes back with the
  database and the objects never left the bucket. Do not delete the blob
  bucket's `pos-cold/` prefix, and give it no lifecycle rule.
- To read an old month's lines (a dispute, an audit):
  `railway ssh -- python3 -c "import pos_cold; print(pos_cold.restore(<rid>, 'YYYY-MM'))"`
  puts it back (rows already there are kept) and holds it hot for 30 days.
  It needs the same `BACKUP_ENCRYPTION_KEY` the export was sealed with.
- `RETAIN_POS_LINES_HOT_DAYS=0` turns the tier off; nothing already exported
  comes back by itself.

---

## Volume full

Two volumes since 10/19/26 (scale re-audit 10/20/26 R8-06):

- **The Postgres service's volume** holds the database and its write-ahead
  log. Full, Postgres refuses every write on the platform and `/health`
  answers 500 `db_not_writable`. The platform check measures
  `pg_database_size` plus the WAL (`pg_ls_waldir`) against
  `PG_VOLUME_MAX_BYTES` (`status_manager.pg_volume_state`): past 80% a daily
  warning, past 90% an urgent page; unset is itself a daily warning — **set it
  to the volume's size** (Railway → Postgres → Volume). The fix is growing the
  volume in Railway (self-serve to 1 TB on Pro), then raising
  `PG_VOLUME_MAX_BYTES`. A WAL that keeps growing is an archiver that stopped
  (`SELECT * FROM pg_stat_archiver;` — `failed_count`, `last_archived_time`).
- **The app volume** (`/app/data`) holds the local backups, the cutover's
  frozen SQLite file and the logs — the rest of this section.

On the app volume a full disk means the backup stops fitting; on the SQLite
rollback, writes fail, reads succeed, and **every fail-open guard in the
system keeps failing open** — a hundred unrelated small errors, not one
cause. `/health` names it directly: `disk_low` below max(250 MB, 4 × the
database — on Postgres the live database's size, since the backup's export
of it lands here) or under 10% free, `disk_critical` below max(50 MB,
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
3. Free space: lower `BACKUP_RETAIN_COUNT` (3 gzipped snapshots by
   default), delete the oldest snapshots (**never the newest**), or grow
   the volume in Railway. Once the off-site copy is reliably running, fewer
   local copies are needed. A leftover `offsite_work_*.db` is a crashed
   run's scratch copy (possibly already scrubbed — never restore it); the
   next backup deletes it.
4. `ops.prune_ledgers` trims old ledger rows nightly after each backup
   (chunked, bounded); it can be run early from `/admin` → Operations →
   Jobs → `prune_ledgers`. It deletes only when the newest backup wrote its
   local snapshot within the last 26 hours (`ops.prune_backup_gate`): after
   a failed snapshot the prune is held, counted failed and paged
   (`retention_held`), so nothing is deleted that no copy holds — free the
   space, let a backup succeed, then run it.
5. The backup refuses to START with less than 1.5 × the database free
   (`BACKUP_FREE_SPACE_FACTOR`: the uncompressed copy plus its gzip) — it
   fails and pages rather than filling the volume at 2am. The off-site copy
   checks its own room just before it is made (1.4 × the snapshot, for the
   encrypted file; the scrub's compaction is written beside it, inside that
   check — R8-18); short of it, that copy fails and pages and the local
   snapshot stands. One whole run peaks at about 2.75 × the database
   (`BACKUP_PEAK_SPACE_FACTOR`), which is what the backfill's room check and
   the `disk_low` line are judged against. Engineering → Overview → Backups shows growth per day and
   days to full (`ops.storage_trend`, from each night's backup row).

6. New stores' POS history backfill holds itself before it can break the
   backup (scale audit 10/19/26 ONB-1): a store backfills only while it
   holds one of the backfill slots (`onboarding.backfill_cap`, sized from
   the RPOWER fleet budget unless `POS_BACKFILL_MAX_STORES` pins it) AND the
   volume has room for tonight's days plus the backup's whole peak
   (`BACKUP_PEAK_SPACE_FACTOR` x the database) after them
   (`onboarding.backfill_admission`); short of room it waits (last night
   still archives), says why on Customers → Onboarding, and pages once in 12
   hours (`backfill_no_room`). On Postgres the backup's gate reads the live
   database's size (`pg_database_size`), not the SQLite file left by the
   cutover. `PG_VOLUME_MAX_BYTES` adds the Postgres volume's own ceiling.

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

0. **A restored database holds the loop on purpose.** When the restore
   guard found the database behind the app volume's tick counter (a Railway
   Postgres restore under the running service — R8-04), every tick stops
   before any job and the queue claims nothing: the log says `restore guard:`,
   `railway ssh -- python3 -m restore_guard` prints `frozen`, and a
   `restore_detected` page went out. Reconcile ([P4](#p4-after-a-postgres-restore--reconcile-confirm-unfreeze)),
   then `railway ssh -- python3 -m restore_guard confirm`. `RESTORE_GUARD=0`
   turns the guard off if it ever misfires (say so in the incident notes).
1. `/admin` → Operations → Jobs shows every job in the registry with its last runs and
   state (ok / partial / failed / running / stuck / overdue / never), the
   heartbeat (`wedged` — a job past its own bound; `loop_stalled` — no tick
   completed with nothing running; `stale`), the lease, the last operator
   page, missed windows and requests.
2. The lease and the heartbeat are in the database — on Postgres in `psql`
   (`railway connect Postgres`), never the volume's frozen `reviews.db`:
   ```sql
   SELECT * FROM scheduler_lease;
   SELECT * FROM scheduler_heartbeat;
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

Nightly at 2am Central (`scheduler.backup_db`), **as a job-queue row the
queue worker process runs** (`scheduled_job`, the maintenance group — scale
re-audit 10/20/26 R8-03; it ran inline on the scheduler loop, in the web
process, at ~91 s a GB), in order: a free-space check (1.5 × the database;
one run's peak is about 2.75 ×), a consistent snapshot with `integrity_check`
on the volume (unredacted — the restore artifact), then a scrubbed, encrypted
copy to object storage and, when it fits, by email. On Postgres the snapshot
is `pg_copy.export_to_sqlite`: every Postgres table, streamed through a
server-side cursor, each table's count checked in its own snapshot — the
ordinary tables in one consistent snapshot, each partition of a partitioned
fact table in its own short one after it (R8-17: one snapshot for the whole
export held back vacuum for its whole run). The retention prune waits for
the backup's row to finish (by 6am at the latest). **The run FAILS** — a failed
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
  about 35 days. A copy past `BACKUP_MULTIPART_THRESHOLD` (512 MiB) goes up
  in `BACKUP_MULTIPART_PART_BYTES` parts (256 MiB; up to 10,000 parts,
  `offsite_backup.upload_file_multipart`), so the 5 GB single-PUT limit no
  longer caps the copy. The
  rule is checked weekly (`offsite_lifecycle`, `scheduler.run_offsite_lifecycle_check`
  → `offsite_backup.lifecycle_days`, one signed `GET ?lifecycle`): no
  enabled expiration rule covering `BACKUP_S3_PREFIX`, or one longer than
  `BACKUP_OFFSITE_MAX_DAYS` (35), fails the run and pages
  (`backup_lifecycle`, weekly cooldown) (memory re-audit 9/29/26). A key
  that may not read the rule — production's is R2 "Object Read & Write" on
  `cavnar-backups` only, which R2 answers 403 — is checked by the copies
  instead (`offsite_backup.oldest_copy_days`, ListObjectsV2): the oldest
  copy under the prefix older than `BACKUP_OFFSITE_MAX_DAYS` + 1 day, or a
  listing that fails, pages the same way. Production (9/29/26): bucket
  `cavnar-backups`, rule `expire-35d` (35 days, all prefixes), set with
  `wrangler r2 bucket lifecycle add`.
- **Email**: skipped above `BACKUP_EMAIL_MAX_BYTES` (25 MB of encrypted
  file); the skip is in the run's `offsite_error`. An emailed copy has no
  expiry the code can enforce: `BACKUP_EMAIL_MODE=fallback` sends it only
  on a night the object-storage copy was not made (the default, `always`,
  sends it every night beside object storage).
- **Re-run**: `/admin` → Operations → Jobs → `backup_db` → Run now.
- **Durability between backups**: the database runs in WAL with
  `synchronous=NORMAL` (AI cost audit 10/7/26 #92, `models.sqlite_synchronous`).
  A crash, kill, OOM or redeploy of the app loses no committed write and
  never corrupts the file; a host power loss or OS crash can lose the
  writes since the last WAL checkpoint (seconds of them) — the file reads as
  it was a moment earlier, so nothing here needs a restore. Set
  `SQLITE_SYNCHRONOUS=FULL` to fsync every commit again.

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
| AI answers failing | `/admin` → Operations → AI. `GET /admin/api/ai/health` shows each provider's breaker (this process's and the last recorded), the last hour's error rate, the last credential failure and the budgets; `ai_health_events` is the history. A 401 or an empty credit balance trips the breaker at once; an open breaker still open after 15 minutes pages. A tripped breaker clears itself; each vendor's **Reset** on that tab (`POST /admin/api/ai/reset-breaker {provider?}`, admins only) closes it now in this process and, through the shared `ai_breakers` row, in every other process within `AI_BREAKER_SYNC_SECONDS` (5 s; scheduler audit 10/10/26 SCALE-24 — an opening is shared the same way, so a worker that finds the provider down stops the web process calling it too). One restaurant's own ceilings can be set with `model_gateway.set_tenant_budget` (`ai_tenant_budgets`). A budget stop (80% warns, 100% stops; trial $5/day and $50/month; Places has its own ceiling) needs the ceiling raised (`AI_*_BUDGET_USD`) |
| No email arriving | `/admin` → Operations → Email & SMS and `GET /admin/api/messaging/health` (problems, Resend/Twilio webhook health, email and SMS stats, outboxes, storm caps, a suppressed operator address). Check Resend's status. A suppressed address: the tab's suppression search (`GET /admin/api/suppressions?q=`) and **Reinstate** (`POST /admin/api/suppressions/reinstate {email, reason}`, audited) — also on the client page's *Suppressed addresses*. Operator addresses are never suppressed. Cavnar AI's own marketing is held until `CAVNAR_POSTAL_ADDRESS` is set |
| Texts not arriving | Operations → Email & SMS, `GET /admin/api/sms?restaurant_id=` — every attempt with its status (the number's last four only); `blocked` means consent or a STOP; account-level Twilio errors are captured once an hour |
| Push not arriving | `POST /api/account/send-test-push` — it reports Apple's own reason string. See the APNs notes in project memory |
| A client's alerts stopped mid-day | An alert storm cap (10 alerts in an hour caps the restaurant until its local midnight; health alerts still go). Lift it on the client page or on Operations → Push & alerts (`POST /admin/api/client/<rid>/storm-cap/lift`) |
| Some restaurants not fetched | `/admin` → Overview's *Needs you* list (`fleet:fetch_coverage`, and a client's `fetch_behind`). A bounded pass resumes from its cursor next slot; **Fetch reviews now** on the client runs one restaurant on the admin pool |
| A job failed | `/admin` → Operations → Jobs, then **Run now** there |
| A table stopped being pruned (`retention_floor` page) | A `RETAIN_*` variable was set under that table's floor (`ops._RETENTION_FLOOR_DAYS`; the floors exist because a reader looks back that far). `ops.prune_ledgers` refuses the table — nothing is deleted from it — marks the run partial, captures a failure and pages once an hour. Operations → Jobs names each refused table with its window and floor (`retention` on the jobs payload, and `refused` / `capped` kept in the `prune_ledgers` run's result). Fix or remove the variable in Railway; the next night's pass prunes it again. `capped` is not a fault: more rows were past their window than one pass deletes, and the rest go on the following nights |
| A billing email never went | `/admin` → Customers → Billing → *Billing health* (`GET /admin/api/billing/health`) lists failed and waiting `owed_sends`; a client's own are on its Billing tab. To retry one after fixing the cause: `UPDATE owed_sends SET status='pending', next_attempt_at=datetime('now'), attempts=0 WHERE id=<id>;` — the `owed_sends` job drains it on its next tick (only on Railway; Run now from Operations → Jobs sends it at once) |
| Stripe and the console disagree | Billing health → *Nightly reconcile*: `billing_reconcile` holds what the `stripe_reconcile` job (3:30am CT, `billing_jobs.reconcile_stripe`) found; it never changes billing status itself — fix the account by hand (change plan, attach customer, lift hold), then run `stripe_reconcile` again from Operations → Jobs |
| A client is paused and cannot resume | A HOLD (`pause_reason` dispute, refund or admin). Only an admin lifts it: the client's Billing tab → **Lift hold** (`POST /admin/api/billing/<rid>/lift-hold {note, status?}`), which lifts every location held for the same reason |
| A console issue keeps coming back | Resolve is per OCCURRENCE: a newer occurrence of the same issue reopens it ("Reopened — … resolved it on M/D/YY"); a condition-level issue stays resolved while the condition lasts and lapses after 30 days; `scheduler`, `platform:error_rate`, `backup` and deletion requests cannot be resolved (409) — they clear themselves, or close another way (a deletion request by withdrawing it or finishing the offboarding). Resolutions written before 9/29/26 carry no occurrence and cover only occurrences that began before them |
| A provider key stopped working | Engineering → Overview → Providers (`provider_health`, probed hourly by the `provider_probes` job): Resend, Twilio, Anthropic, Stripe, Places, APNs, RPOWER (`store/getbymid` for up to three stores on different tokens, `RPOWER_PROBE_RESTAURANT_ID` first, else the lowest ids; failing only when two or more tokens are refused — one refused token is that store's latch). A provider that turns failing pages once |
| RPOWER down or slow (many stores at once) | Operations → Integrations → *RPOWER traffic* (`GET /admin/api/pos-traffic`: requests, errors and p95 by endpoint and store from `pos_request_ledger`, and the breaker). After a run of timeouts or 5xx answers the shared breaker (`pos_breakers`) stops every RPOWER request — jobs reschedule their queue rows by its `retry_after`, pages show the archive — and one half-open probe (the next request or the hourly probe) closes it; the cooldown doubles each failed probe to 30 minutes, and a breaker open 30 minutes pages once (`pos_io.page_if_long_open`). Many stores failing their nightly sync is one *RPOWER failing for N of M stores* issue (`fleet:pos:rpower`) and one daily page, not a row per store. To stop every RPOWER request by hand: `UPDATE pos_breakers SET open_until=<epoch far ahead> WHERE provider='rpower'`; to resume now: `open_until=0`. `POS_BREAKER=0` turns the breaker off. Since 10/21/26 (audit #23) one store's failures park only that store (`pos_breakers` row `rpower@<restaurant id>`, the console's quarantined list; to release one now: `UPDATE pos_breakers SET open_until=0 WHERE provider='rpower@<id>'`), and the provider's row opens only when a share of distinct stores fails. An answer RPOWER sends in an unknown shape from two stores pauses RPOWER for `POS_SCHEMA_PAUSE_SECONDS` and pages at once (`reason` starts `schema:`; nothing was stored) — check the field named in the page against RPOWER's collection before resuming. A refused token is latched (`pos_auth_latch`, `restaurants.rpower_auth_failed_at`): its requests stop for 15 minutes, then one re-probe; three stores or two tokens refused in an hour pages once (`pos_auth_wave:rpower`) and holds the owners' "hasn't synced" notices; a reconnect clears it, and so does `DELETE FROM pos_auth_latch WHERE provider='rpower'`. A 429 halves the request rate for every process and recovers on its own (`rpower_rate_slots.factor`) |
| One RPOWER store, or a data type across the fleet, is behind | Operations → RPOWER (`GET /admin/api/rpower-health`, Audit #23): one row per store from the 15-minute `rpower_health_rollup` (`rpower_store_health`) — health (token refused / failing / stale / watch / onboarding / OK), each data type's last good read, 90-day coverage, nights missing in 14 days, requests and errors; open a row for its 90-night calendar, its queue rows, 7 days of requests and its findings. The fleet rules firing are at the top (and page from the platform check): a data type failing at more than 5% of stores (and 5) for 30 minutes, more than 10% stale past 48 hours, an auth wave (3 stores or 2 tokens refused within an hour — urgent: the partner token is likely revoked; the owners are not asked to reconnect a shared token), archive gaps at more than 2% of stores, a queue kind's failed and expired rows past 2% in 24 hours, 429s past 1% or budget pauses past 5% in the last hour, requests past 1.5x (page at 2x) the 7-day median. A store whose token RPOWER refused (`restaurants.rpower_auth_failed_at`) is a critical issue and its owner was told once (email and push); it clears when the owner reconnects. If the table looks old ("the rollup is over an hour old"), run `rpower_health_rollup` from Operations → Jobs |
| Telling clients something is wrong | Engineering → Status page: **Post an incident** naming the affected services (it holds them down on the public `/status`; the scheduler's checks rewrite an un-held service within minutes), post updates, **Resolve**. `/status` is served by this process — in a full outage use the external monitor's hosted page |
| A client is closing their account | Customers → the client → Access & activity → the *Offboarding* card (*Close this account* while a request is open; `GET /admin/api/client/<rid>/offboarding`). Mark each step done, or skipped with a note. Marking an acting step done carries it out, after the step-up: **integrations** clears every stored credential and turns outbound webhooks off; **Stripe** cancels the live subscription (`billing_jobs.cancel_subscription`) — 409 for a location billed under another's subscription (change the plan on the paying location, then skip) or on a server that is not production, 502 when Stripe refuses; **DocuSign** voids the open envelope — 409 on a server that is not production or when the envelope is already signed (skip it with that note), 502 when DocuSign refuses. **Withdraw** the request (409 if there is none, or it changed while you looked) or **Delete** once nothing is outstanding (409 with the steps left, or while an admin or support login calls it home). The checklist and the audit trail outlive the delete |

---

## Drill

The drill is a job: `scheduler.run_restore_drill`, a queue row the queue
worker runs on the 2nd of every month once the 2am backup has finished
(quarterly until 10/19/26, OPS-10; on the queue since 10/20/26, R8-15 — it
checks the volume has room for the decompressed copy, and for the off-site
download and its decryption, before writing either), and on demand from
`/admin` → Jobs → **restore_drill**. It copies the newest snapshot to a
scratch file beside it (decompressing a gzipped one — the step
`RESTORE_FROM` takes too) and FAILS on a snapshot older than 26 hours, then
runs `integrity_check`, counts restaurants, reads the newest review and
alert, counts Google tokens in the snapshot against production (the
un-redaction proof), runs `init_db()` over the copy the way a real restore
does, and deletes the copy. Where object storage is configured it then
proves the off-site copy too: downloads the newest one recorded in
`backup_runs`, checks it against the SHA-256 it was uploaded with, decrypts
it and runs `integrity_check` and a restaurant count, then opens its sealed
values with this server's `CREDENTIAL_KEY` — a copy whose seals do not open
fails the drill, since restoring it would sever every integration — and
counts the RPOWER tokens it kept. **It times every stage** (local:
decompress, integrity, checks, migrate; off-site: download, decrypt,
integrity, unseal) and says when the local restore would not fit inside the
120-second healthcheck a `RESTORE_FROM` boot runs under
(`scheduler.RESTORE_HEALTHCHECK_SECONDS`) — the measured RTO, not a guess.
It emails Will the result either way. A failure is a failed job in `/admin` → Jobs like any
other. Run it by hand after any change to `backup_db`,
`_write_consistent_snapshot`, `offsite_backup` or the scrub registry.

Record the date of the last successful drill here:

A yearly game day (an outside step, Will's): a full off-site restore —
download, decrypt, `RESTORE_FROM` — onto a scratch Railway environment with
only the escrowed keys, timed end to end, so the keys in the password
manager are proven to be the right ones. The monthly job proves everything
but the escrow. Railway's own volume backups are proved the same way: once a
quarter restore one into a scratch environment from the Backups tab and load
`/health`.

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
  the 2nd of the next month, automatically. The off-site copy has never been drilled — run
  the job once object storage is configured.
- **Earlier:** 2026-09-20, **local** (not production). Newest local
  snapshot `cavnar_ai_backup_2026-09-19.db` (8.3 MB) copied to scratch with
  `-wal`/`-shm` removed; `PRAGMA integrity_check` → `ok`; 16 restaurants;
  `MAX(review_date)` 2026-09-19; `MAX(fired_at)` 2026-09-15; `init_db()` ran
  its additive migration over the snapshot and integrity was `ok` afterwards.
  What this did NOT prove: unredaction of OAuth tokens — the local database
  holds no `gmb_refresh_token` at all, so "0 kept" is not evidence either
  way. Superseded by the production drill above.

### Release drills (quarterly — release-safety audit 10/26/26)

The monthly job above proves the SQLite export. It does not prove the two
paths a bad release actually needs: a Railway rollback, and a Postgres
point-in-time restore. Rehearse each once a quarter, on a quiet weekday
morning (9–11am CT), timed from the first click to a healthy `/health`, and
record it — the Engineering system card shows the last of each and warns past
100 days (`release_drills`, `platform_monitor.drill_status`).

**Rollback drill** (production, ~10 minutes, a minute or two of outage):
1. Note the current deployment's SHA (`/health` → `build`).
2. Follow [Rolling back a release](#rolling-back-a-release) steps 1–3 to the
   PREVIOUS healthy deployment. Start the clock at the Redeploy click.
3. When `/health` names the old build and `scripts/smoke.py` passes, stop
   the clock. Redeploy the current deployment the same way, smoke it again.
4. Record: `railway ssh "python scripts/record_drill.py rollback --minutes <n> --note '<what was odd>'"`.

**Postgres restore drill** (a scratch service, no outage, ~45 minutes):
1. Railway → the `Postgres` service → Backups / point-in-time recovery →
   restore to a time ~1 hour ago **into a NEW service** (never over
   production's).
2. A throwaway web deploy pointed at it: a new environment (or service) with
   `DATABASE_URL` = the restored service's URL, `CAVNAR_ENV=staging`, and NO
   provider keys (no `RESEND_API_KEY`, `TWILIO_*`, `STRIPE_*`, `DOCUSIGN_*`,
   `ANTHROPIC_API_KEY`, `APNS_*`, `CREDENTIAL_KEY` — it opens every store's
   POS credentials in the copy — nor the RPOWER partner credentials or a Meta
   app secret). The staging gate refuses every send even if one is left behind,
   the RPOWER/Toast schedule pushes and Meta posts the copy's queued rows would
   make included; the scheduler does not start.
3. Wait for `/health` = 200 (`"status"` may be degraded: no scheduler, no
   backups). Count restaurants, `MAX(created_at)` of `reviews`, `alert_log`,
   `job_runs` against production's (read-only `railway ssh` on production).
4. Delete the scratch service and environment. Record:
   `railway ssh "python scripts/record_drill.py pg_restore --minutes <n> --note '<counts matched / not>'"`.
5. Write the steps that differed from these, and the minutes, here.

- **Last rollback drill:** never (as of 10/26/26).
- **Last Postgres restore drill:** never (as of 10/26/26).

## The outside monitor (cavnar-watchdog)

`workers/watchdog/` is a Cloudflare Worker on the same Cloudflare account as cavnar.ai, deployed from that folder with
`npx wrangler deploy` (9/29/26). It is the one check that lives outside Railway:
- **Heartbeat.** `HEALTHCHECK_PING_URL` on Railway is `https://cavnar-watchdog.cavnarai-account.workers.dev/ping/<PING_TOKEN>`;
  the scheduler pings it every tick (`ops.ping_healthcheck`), `/fail` after a failed backup or digest.
- **Uptime.** A cron every 5 minutes fetches `/health` and expects 200 with `"db":"ok"`.
- **Degraded** (Audit #25 A07-09; re-audit B7 10/27/26). A 200 whose body did not say `"status":"ok"` on 5 of the last
  7 checks (`DEGRADED_MIN` of `DEGRADED_WINDOW`: 30 minutes of them, so a flapping body counts too) alerts once —
  "Cavnar AI is degraded", with the body's `problems` — on every configured channel (deduplicated as
  `cavnar-watchdog-degraded` on PagerDuty), and once more after two healthy checks running. Degraded turning DOWN
  resolves the degraded incident and escalates at once: the down alert goes on the first down check, not the second.
  The in-app pages for those problems ride the app's own email and text keys; this copy does not.
- Silence past 20 minutes, or two failing `/health` checks in a row, alerts once, and once more when it recovers. A
  `/fail` alerts on its own at the first check that sees it — kept in its own KV key until a channel took the alert,
  36 hours at most (scale re-audit 10/20/26 R8-13; it used to ride the heartbeat's key and was overwritten by the next
  ping). Every alert goes as an email to every `ALERT_TO` address (comma-separated; Resend, the Worker's own `RESEND_API_KEY` secret),
  and — so a Resend outage cannot silence the one monitor outside Railway (OPS-4) — each channel whose secrets are set:
  `PAGERDUTY_ROUTING_KEY` (a PagerDuty trigger, resolved on recovery), `PAGER_WEBHOOK_URL` (any paging service's
  webhook), and `TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` + `TWILIO_FROM` + `ALERT_SMS_TO` (a text to each on-call
  phone). The alert counts as sent when any channel took it. State is in the Worker's KV namespace. A change to
  `worker.js` goes live only with `npx wrangler deploy` from `workers/watchdog/`.
- To rotate the token: `npx wrangler secret put PING_TOKEN`, then update `HEALTHCHECK_PING_URL` on Railway.
