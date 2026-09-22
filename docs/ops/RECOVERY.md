# Recovery — Cavnar AI

What to do when production is broken. Written during resiliency audit #21,
which found working, integrity-checked backups and **no procedure anywhere
for using one**. A backup nobody has restored is a hypothesis.

Read `SYSTEM_ARCHITECTURE.md` for how the pieces fit. This file is only the
emergency path.

---

## First: what kind of broken?

Check `https://dashboard.cavnar.ai/health` before anything else.

| `/health` says | What it means | Go to |
|---|---|---|
| Connection refused / 502 | The process is down or Railway is down | [Platform down](#platform-down) |
| `500` with `"db"` | The database file is unreadable | [Database recovery](#database-recovery) |
| `200`, `"disk": {"state": "critical"}` | Volume nearly full — **writes are failing, reads are not** | [Volume full](#volume-full) |
| `200`, `"scheduler": "stale"` | Web is fine, background jobs have stopped | [Scheduler stopped](#scheduler-stopped) |
| `200`, `"status": "ok"` | The platform is healthy; the problem is narrower | [Something specific](#something-specific-is-broken) |

`/status` (public) and `/admin` → Overview carry the same signals with more
detail, including per-restaurant fetch coverage.

**Reaching the container.** Every command below that touches `/app/data`
runs *inside* the Railway container: `railway ssh -- <command>` (the CLI
must be linked to the project — `railway status` shows it). `railway run`
is the wrong tool — it runs the command on *your laptop* with production's
environment variables, so `railway run sqlite3 /app/data/reviews.db` opens
nothing, or worse, a local file. An earlier version of this file said
`railway run` throughout.

---

## Platform down

Railway outage or a crashed process.

1. Check Railway's own status, then the service's Deployments tab.
2. If the last deploy is the cause, **roll back in Railway** — do not try to
   fix forward under pressure. The database is on a persistent volume and is
   not affected by a rollback.
3. If the process is crash-looping, read the deploy logs. `hosted_dashboard`
   runs `init_db()` and `ensure_columns()` at boot; a migration that raises
   will loop.

**What keeps working:** nothing. This is a full outage.
**Data loss:** none — the volume survives.
**Owners see:** the site not loading. There is no status page to see either,
because it is served by the same process. Post to the clients directly.

---

## Database recovery

`/health` returns 500 with a `db` error, or SQLite reports corruption.

### 1. Confirm it is really the database

```bash
railway ssh -- sqlite3 /app/data/reviews.db "PRAGMA integrity_check;"
```

`ok` means the file is fine and the problem is elsewhere.

### 2. Find a snapshot

Backups run at 2am and are kept `BACKUP_RETAIN_DAYS` days (14 in production; the code default is 7):

```bash
railway ssh -- ls -lh /app/data/backups/
```

**The local snapshots are complete and directly restorable.** They are not
redacted — redaction applies only to the copy that is emailed. This was
fixed in audit #21; snapshots taken *before* that fix have credentials
stripped, and restoring one means re-authorising every integration by hand
(see [After any restore](#after-any-restore)).

If the volume itself is gone, use the encrypted email copy instead:

```bash
python3 -c "
from cryptography.fernet import Fernet
key = open('key.txt').read().strip()
open('reviews.db','wb').write(Fernet(key.encode()).decrypt(open('cavnar_ai_backup_YYYY-MM-DD.db.enc','rb').read()))
"
```

`BACKUP_ENCRYPTION_KEY` is in Railway's variables. **That copy is redacted** —
expect to redo every integration.

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

```bash
# Keep the broken file. It is evidence, and it may still be partially readable.
railway ssh -- mv /app/data/reviews.db /app/data/reviews.db.broken-$(date +%s)
railway ssh -- rm -f /app/data/reviews.db-wal /app/data/reviews.db-shm
railway ssh -- cp /app/data/backups/cavnar_ai_backup_YYYY-MM-DD.db /app/data/reviews.db
```

Removing `-wal` and `-shm` matters: a stale WAL beside a restored database
is its own corruption.

Then restart the service. `init_db()` and `ensure_columns()` run at boot and
will additively migrate an older snapshot forward — that path is exercised
on every deploy.

### 5. Confirm

- `/health` returns 200
- `/admin` → Overview loads and client count looks right
- One client's dashboard renders
- The scheduler heartbeat goes green within ~5 minutes

---

## After any restore

Work through this list explicitly. Recovery is not finished when the site
loads.

| Area | State after restore | Action |
|---|---|---|
| Owner sessions | Valid (unredacted snapshot) or gone (redacted) | If gone, tell clients to sign in again |
| Push device tokens | Same | They re-register on next app launch; `send-test-push` to confirm |
| Google / Toast / Instagram | Same | If nulled, each owner must reconnect in Account → Connections |
| Stripe | `stripe_customer_id` may be nulled | Re-link from the Stripe dashboard before the next billing run |
| Data since the snapshot | Lost | Reviews re-fetch on the next pass (`UNIQUE(restaurant_id, platform, external_id)` makes that safe). Uploaded CSVs and manual edits do not come back |
| Job claims | Restored to snapshot state | Jobs already run that day **may run again**. Check `job_period_claims` before a digest hour |

That last row is the one most likely to cause a visible mistake: restoring a
2am snapshot at 9am can re-send that morning's digests. To suppress:

```bash
sqlite3 reviews.db "INSERT OR IGNORE INTO job_period_claims (job_key) VALUES ('weekly_digests:YYYY-MM-DD');"
```

---

## Volume full

Writes fail, reads succeed, and **every fail-open guard in the system keeps
failing open**. This looks like a hundred unrelated small errors, not one
cause. `/health` names it directly.

People already signed in keep reading: a session's `last_active` stamp is
best-effort and written at most once a minute, so a refused write no longer
fails the request (a `session_last_active` entry in the error log says the
database is refusing writes). **Nobody can sign in** — a new session is a
write — and every save, draft and send fails until space is freed.

1. `railway ssh -- df -h /app/data`
2. Largest offenders are usually `backups/` and the WAL:
   ```bash
   railway ssh -- du -sh /app/data/* | sort -h | tail
   ```
3. Free space: lower `BACKUP_RETAIN_DAYS`, delete the oldest snapshots
   (**never the newest**), or grow the volume in Railway.
4. `ops.prune_ledgers` trims old ledger rows and runs after each backup;
   it can be run early from `/admin` → Jobs.

A full volume is the one failure where doing nothing gets worse quietly.

---

## Scheduler stopped

`/health` shows `"scheduler": "stale"`. The web app is fine; briefs,
digests, fetches and scheduled posts are not running.

1. `/admin` → Jobs shows the last run of each job and any failures.
2. The lease is in the database:
   ```bash
   sqlite3 reviews.db "SELECT * FROM scheduler_lease;"
   ```
   A dead holder's lease is taken over automatically after
   `SCHEDULER_LEASE_STALE_SECONDS` (30 min). To force it sooner, restart the
   service — `release_scheduler_lease` runs on clean exit.
3. If the thread died without the process dying, restart. It is started at
   import in `hosted_dashboard`.

**Never run the scheduler as a separate Railway service.** Volumes are not
shared between services, so it would schedule against an empty database
while the real jobs stopped. See `RAILWAY_SCHEDULER_SPLIT.md` (beside this file).

---

## Something specific is broken

| Symptom | Where to look |
|---|---|
| AI answers failing | `/admin` → AI Ops. A tripped breaker (`ai_utils.breaker_state`) clears itself in 60s; a budget stop needs the ceiling raised |
| No email arriving | `/admin` → Emails. Check Resend's status and the suppression list |
| Push not arriving | `POST /api/account/send-test-push` — it reports Apple's own reason string. See the APNs notes in project memory |
| Some restaurants not fetched | `/admin` → Overview shows fetch coverage. A bounded pass resumes from its cursor next slot |
| A job failed | `/admin` → Jobs, then re-run it there |

---

## Drill

The drill is a job: `scheduler.run_restore_drill`, run automatically on
the 2nd of Jan/Apr/Jul/Oct after the 2am backup, and on demand from
`/admin` → Jobs → **restore_drill**. It copies the newest snapshot to a
scratch file beside it, runs `integrity_check`, counts restaurants, reads
the newest review and alert, counts Google tokens in the snapshot against
production (the un-redaction proof), runs `init_db()` over the copy the
way a real restore does, deletes the copy, and emails Will the result.
A failure is a failed job in `/admin` → Jobs like any other. Run it by
hand after any change to `backup_db`, `_write_consistent_snapshot` or
`_redact_snapshot`.

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
  2027-01-02, automatically.
- **Earlier:** 2026-09-20, **local** (not production). Newest local
  snapshot `cavnar_ai_backup_2026-09-19.db` (8.3 MB) copied to scratch with
  `-wal`/`-shm` removed; `PRAGMA integrity_check` → `ok`; 16 restaurants;
  `MAX(review_date)` 2026-09-19; `MAX(fired_at)` 2026-09-15; `init_db()` ran
  its additive migration over the snapshot and integrity was `ok` afterwards.
  What this did NOT prove: unredaction of OAuth tokens — the local database
  holds no `gmb_refresh_token` at all, so "0 kept" is not evidence either
  way. Superseded by the production drill above.
