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
railway run sqlite3 /app/data/reviews.db "PRAGMA integrity_check;"
```

`ok` means the file is fine and the problem is elsewhere.

### 2. Find a snapshot

Backups run at 2am and are kept `BACKUP_RETAIN_DAYS` (14) days:

```bash
railway run ls -lh /app/data/backups/
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
railway run mv /app/data/reviews.db /app/data/reviews.db.broken-$(date +%s)
railway run rm -f /app/data/reviews.db-wal /app/data/reviews.db-shm
railway run cp /app/data/backups/cavnar_ai_backup_YYYY-MM-DD.db /app/data/reviews.db
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

1. `railway run df -h /app/data`
2. Largest offenders are usually `backups/` and the WAL:
   ```bash
   railway run du -sh /app/data/* | sort -h | tail
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
while the real jobs stopped. See `RAILWAY_SCHEDULER_SPLIT.md`.

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

Restore the newest snapshot into a scratch copy and run the verification
queries. Quarterly, and after any change to `backup_db`,
`_write_consistent_snapshot` or `_redact_snapshot`.

```bash
cp /app/data/backups/$(ls -t /app/data/backups | head -1) /tmp/drill.db
sqlite3 /tmp/drill.db "PRAGMA integrity_check; SELECT COUNT(*) FROM restaurants;"
```

Record the date of the last successful drill here:

- **Last drill:** _(none yet — see audit #21)_
