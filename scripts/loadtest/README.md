# Load testing (scale audit 10/19/26)

Never against production, RPOWER, Twilio, Resend, Google or Anthropic. Every script refuses a production host
or a shell holding provider keys. The POS is the simulated store (`demo_store`), so RPOWER's real API is never
called; model calls are refused without a key (the narrative-free paths still run end to end).

## Setup (one staging environment)

1. A Railway environment `staging` cloned from production's service settings, with its OWN Postgres (never
   production's) and **no provider keys**: unset `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, `TWILIO_*`,
   `STRIPE_SECRET_KEY`, `GOOGLE_PLACES_API_KEY`, `PERPLEXITY_API_KEY`, `DOCUSIGN_*`, `APNS_*`.
   Same instance sizes as production, so results transfer. Set `CAVNAR_ENV=staging` (release-safety audit
   10/26/26, A05-06): the code then never starts the scheduler and refuses every send — email, texts,
   push, live-key Stripe, DocuSign's live account, Google replies, customer webhooks — even if a key was
   left behind (`config.outbound_sends_allowed()`, docs/ops/RECOVERY.md → Deploys). The same environment
   is the one Railway deploys `main` to before a promotion.
2. Seed it: `LOADTEST_STAGING=1 python scripts/loadtest/seed_staging.py --restaurants 100` (then 500, 1,500).
   It writes `scripts/loadtest/users.csv` - keep that file out of git.
3. `pip install locust` on the machine driving the test (not in requirements.in - it is a tool, not the app).

## Traffic model (`locustfile.py`)

Built from production's request rollups (7 days to 10/19/26), not a uniform load: a dashboard open is the
page shell plus ~23 API reads at once (measured), and the slow ones (`/api/morning-brief` 13.4 s average,
`/api/good-news` 11.1 s, `/api/labor/publish-review` 10.1 s, `/api/cross-module` 3.3 s) are in the mix on
purpose. User mix: owners 35%, managers 35%, staff app 25%, heavy readers 5%; log-normal think time
(median ~20 s).

Users per restaurant to simulate: 1 owner + 3 managers + 2 staff logins seeded; at peak assume 30-40% of a
restaurant's people are active (ASSUMPTION - replace with the pilot's measured DAU).

## Scenarios and pass/fail

| Scenario | How | Pass when |
|---|---|---|
| Morning burst | Ramp to 40% of all seeded users in 5 min (`--users N --spawn-rate N/300`), hold 30 min | p95 < 2 s for every API route except the four named slow ones (< 5 s), 0 5xx, `/health` p95 < 300 ms |
| Sustained | 15% of users for 2 h | same thresholds, no growth in p95 over the run, worker RSS flat |
| Stress | Ramp +10% users every 5 min until p95 > 5 s or 5xx > 1% | records the breaking point (users, RPS) - no pass/fail, it is the capacity number |
| Soak | 10% of users for 8 h | no memory growth > 10%, no connection-pool exhaustion (`PG_POOL_*`), no error creep |
| Closing crunch | `nightly_sim.py` with every store closing in one window | every stage finishes inside its bound with `hit_bound: false`; DSRs final before 6am local |
| Failure recovery | During the sustained run: restart the web service, then kill `worker.py` | 5xx only during the restart (< 60 s), queued jobs resume from checkpoints, nothing runs twice |
| Schedule rush | 500 managers press Generate in 30 min (a Locust task posting to the generate route at that rate) | every job completes; p95 wait in queue < 10 min; no lease expiry; cost per generation within the ledger's measured average |

## Reading results

- Compare each route's p95 against production's measured average in `prod_metrics.json` - a route that is
  slow at 1 user is a query problem, not a capacity problem; fix it before adding servers.
- Watch Postgres: connections in use vs `PG_POOL_MAX`, locks waiting (`pg_locks` not granted), the slowest
  statements (`pg_stat_statements`).
- The job queue: `job_queue.stats()` (printed by `nightly_sim.py`) - wait p95 per class and depth over time.
- Record every run's numbers next to the capacity model in the scale audit report; replace ASSUMPTIONs with
  them.
