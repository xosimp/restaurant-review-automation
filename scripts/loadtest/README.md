# Load testing (scale audit 10/19/26; release-safety audit #25 WS10)

Never against production, RPOWER, Twilio, Resend, Google or Anthropic. Every script refuses a production
environment, a shell holding provider keys, and a database holding restaurants it did not seed. The POS is
the simulated store (`demo_store`), so RPOWER's real API is never called; model calls are refused without a
key (the narrative-free paths still run end to end).

The kit is ready to run the moment a staging environment exists. It has not been run yet: staging is a
step for Will (below).

## Setup (one staging environment — Will)

1. A Railway environment `staging` cloned from production's service settings, with its OWN Postgres (never
   production's, never a copy of it) and **no provider keys**: none of the names in
   `seed_staging.PROVIDER_KEYS` (Anthropic, Resend, Twilio, Stripe, Google, Perplexity, DocuSign, APNs, Meta,
   the R2/S3 backup and blob keys, and production's side channels — `SENTRY_DSN`, `OPS_PAGER_*`,
   `HEALTHCHECK_PING_URL`). Its own `SECRET_KEY` and `CREDENTIAL_KEY`. Set `CAVNAR_ENV=staging` (WS2's
   `config.deploy_env()`; Railway also names the environment in `RAILWAY_ENVIRONMENT_NAME`). Same instance
   sizes as production, so results transfer.
   With `CAVNAR_ENV=staging` the code never starts the scheduler and refuses every send — email, texts,
   push, live-key Stripe, DocuSign's live account, Google replies, customer webhooks — even if a key was
   left behind (`config.outbound_sends_allowed()`, docs/ops/RECOVERY.md → Deploys). The same environment
   is the one Railway deploys `main` to before a promotion.
2. Seed it from a shell with the staging `DATABASE_URL` and nothing else:
   `CAVNAR_ENV=staging python scripts/loadtest/seed_staging.py --restaurants 100` (then 500, then 1,500 —
   seed each size into a fresh database). `--plan` prints the world and writes nothing. It writes
   `scripts/loadtest/users.csv` — gitignored; keep it out of git.
3. `pip install locust` on the machine driving the test (not in requirements.in — a tool, not the app).

## The world (`seed_staging.plan_world`)

Five time zones, so the morning wave, the nightly close and every restaurant-local "today" arrive at five
different UTC hours: Chicago 30%, New York 25%, Los Angeles 20%, Denver 15%, Honolulu 10%. Per 100 stores:

| Store | Where | Why |
|---|---|---|
| 24-hour diner (`diner_24h`) | New York | three 8-hour shifts, overnight across midnight; the DST week |
| Late bar (`late_bar`) | Los Angeles | 4pm to 2am, 3am Friday and Saturday |
| Mass time off (`mass_time_off`) | Denver | 13 of 22 on approved time off for the week a Generate press drafts |
| Multi-location org (`multi_location_org`) | Chicago | two locations of one owner sharing three people (two restaurants) |
| A chain | one in each zone | one organization, five POS stores |
| The rest | spread to the weights | half on the simulated POS (the sync, archive, DSR, Bar and Food Cost paths), half labor-only stores from the schedule benchmark's archetypes (cafe, bistro, full service, teen QSR, single manager, daypart-limited, zero history) — `--pos-share` |

Every store is a paying client as the scheduler sees one (`billing_status` active — the old kit's
"internal" left the stores out of every served-client job, so a night measured almost nothing; `--billing
internal` brings that back) and marked `LOADTEST` in its notes. The POS stores are demo rows: they all read
the one simulated store, and only a demo may share a POS store (one store, one live restaurant); the
labor-only stores are not. Logins per store: 1
owner, `--managers` (3), `--staff` (2), all `@loadtest.example`. The simulated POS keeps Halsted & Rye's
Chicago clock for its records; each store's own clock (its business day, its 7am brief, its report after
close, its schedule's week) is its own zone.

## Traffic model (`locustfile.py`)

Built from production's request rollups (7 days to 10/19/26), not a uniform load: a dashboard open is the
page shell plus ~23 API reads at once (measured), and the slow ones (`/api/morning-brief` 13.4 s average,
`/api/good-news` 11.1 s, `/api/labor/publish-review` 10.1 s, `/api/cross-module` 3.3 s) are in the mix on
purpose. User mix: owners 35%, managers 35%, staff app 25%, heavy readers 5%; log-normal think time
(median ~20 s); one `Health` user probes `/health` every 5 s.

Users per restaurant to simulate: 1 owner + 3 managers + 2 staff logins seeded; at peak assume 30-40% of a
restaurant's people are active (ASSUMPTION - replace with the pilot's measured DAU).

## Scenarios and pass/fail

`LOADTEST_SCENARIO` names the run (`morning_burst`, `sustained`, `stress`, `soak`); the results file is
judged against its line.

| Scenario | How | Pass when |
|---|---|---|
| Morning burst | `LOADTEST_SCENARIO=morning_burst` — the `MorningBurst` shape sets the users itself (no `--users`): 40% of every seeded login in 5 min, held 30. `LOADTEST_BURST_MODE=synchronized` (default): every zone at once, the worst case. `rolling`: each zone's 40% in its own wave, New York first, 10 min apart, new users logging in as that zone's stores | p95 < 2 s for every API route except the four named slow ones (< 5 s), 0 5xx, `/health` p95 < 300 ms |
| Sustained | 15% of users for 2 h (`--users N --spawn-rate N/300 --run-time 2h`) | same thresholds, no growth in p95 over the run, worker RSS flat |
| Stress | Ramp +10% users every 5 min until p95 > 5 s or 5xx > 1% | records the breaking point (users, RPS) — no pass/fail, it is the capacity number |
| Soak | 10% of users for 8 h | no memory growth > 10%, no connection-pool exhaustion (`PG_POOL_*`), no error creep |
| Closing crunch | `nightly_sim.py` with every store closing in one window | every stage finishes inside its bound with `hit_bound: false`; DSRs final before 6am local |
| Failure recovery | During the sustained run: restart the web service, then kill `worker.py` | 5xx only during the restart (< 60 s), queued jobs resume from checkpoints, nothing runs twice |
| Schedule rush | 500 managers press Generate in 30 min (a Locust task posting to the generate route at that rate) | every job completes; p95 wait in queue < 10 min; no lease expiry; cost per generation within the ledger's measured average |

Once per release train: the morning burst (rolling, then synchronized) at 100 stores; at 500 before the
RPOWER rollout passes ~100 stores.

    LOADTEST_HOST=https://<staging host> LOADTEST_USERS=scripts/loadtest/users.csv \
    LOADTEST_SCENARIO=morning_burst LOADTEST_BURST_MODE=rolling \
    locust -f scripts/loadtest/locustfile.py --host "$LOADTEST_HOST" --headless

## The results file (`results.py`)

Written when the run stops (on the master in a distributed run): `scripts/loadtest/results/<scenario>-<UTC
start>Z.json` (`LOADTEST_RESULTS_DIR` moves it), format 1 — the scenario and burst mode, the host name
(never a URL with credentials), the build `/health` named, start, end and seconds, the world from
users.csv (stores and logins by zone and kind), the peak users, every route's requests, failures, 5xx,
p50/p95/p99/max/avg ms, requests a second and p95 limit, `/health`'s p95, the totals, and the verdict:
`{"ok": bool, "failed": ["/api/morning-brief p95 6200 ms > 5000 ms", ...]}`. A route with fewer than 20
requests is reported, not judged. `LOADTEST_NOTES` adds a line of context. The console prints PASS or the
first failures and the path. Results hold no customer data; commit the ones worth keeping beside the
capacity model.

## Reading results

- Compare each route's p95 against production's measured average in `prod_metrics.json` - a route that is
  slow at 1 user is a query problem, not a capacity problem; fix it before adding servers.
- Watch Postgres: connections in use vs `PG_POOL_MAX`, locks waiting (`pg_locks` not granted), the slowest
  statements (`pg_stat_statements`). `tests/test_pg_query_plans.py` holds the hottest statements to their
  indexes on a partitioned Postgres (CI's Postgres job).
- The job queue: `job_queue.stats()` (printed by `nightly_sim.py`) - wait p95 per class and depth over time.
- Record every run's numbers next to the capacity model in the scale audit report; replace ASSUMPTIONs with
  them.
