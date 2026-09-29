# A materialised health row per restaurant (#97)

Plan, 9/29/26 (admin-console fix round, finding #97, left optional by
workstream C). Nothing here is built. Grounded in `admin_ops.py` at
`fixround-0929`.

## The problem

Every fleet read in the admin console — Overview, the rail's badges, the
issue and client lists, integrations, billing, onboarding, adoption, data
sources — is built from one in-memory pass over every restaurant:
`admin_ops._records()` calls `_load_everything()` (grouped queries over the
fleet) and then `location_record(r, d)` for each restaurant, annotates each
issue, applies the resolutions in force and retires the ones whose condition
cleared. The audit measured 28–30 seconds per fleet endpoint at 5,000
synthetic restaurants.

What the fix round did instead (#32, #36, #43, #57): the build is shared — a
45-second memo on request threads only, single-flight with one waiter, dropped
by every admin write and every billing webhook, refused 503 with `Retry-After`
past that (`admin_ops._records_cached`, `AdminBusy`); the rail polls
`/admin/api/badges`, which accepts a memo up to 300 seconds old; the heavy
per-restaurant loops became grouped queries; the value figures moved to a
nightly snapshot (`value_figures_daily`), the business figures to
`business_metrics_daily`, the churn level to `account_risk_state`. That holds
while one build fits comfortably inside a request. It does not remove the
build: the first read after every admin write still pays it, the memo is per
process (a second worker would build its own — `POSTGRES_AND_WORKERS_PLAN.md`),
and the cost grows linearly with the fleet.

## The design

**One row per restaurant, refreshed by a job, read by every fleet page.**

`restaurant_health` (created at boot in `admin_ops.init_admin_ops`):
`restaurant_id` PK, `computed_at`, `generation`, `segment` (customer /
internal), `health` (healthy / warning / critical), the fields the slim list
rows carry today (`admin_ops.clients_page`'s row: name, brand, location
group, owner, billing status and MRR from the mirror, last active, modules,
integration states), `issues_json` (the record's open issues, each with its
`occurrence_at`, severity, category and action — BEFORE resolutions are
applied), `counts_json` (reviews awaiting, stalled, failed jobs 24 h, email
bounces…), and `error` (the build failed for this one restaurant — shown as
unknown, never healthy).

- **Refresh**: a registry job `restaurant_health` (`jobs_registry.JOBS`: every
  5 minutes, SLA ~20 minutes, not sending, runnable) that walks the fleet with
  `scheduler.resumable_sweep` under a time bound and a cursor, oldest
  `computed_at` first, rebuilding each row with the same `location_record`
  code the console uses now — one definition of health, not two. A pass that
  runs out of time resumes where it stopped.
- **Events mark a row stale** so the next pass takes it first: an admin write
  to that restaurant (the after-request hook that drops the memo today), a
  billing webhook (Stripe or DocuSign) for it, a sync result (POS, Google,
  the review fetch, Data Health), a job failure captured with its
  `restaurant_id`. `stale_at` on the row; the refresh orders by
  `stale_at IS NOT NULL DESC, computed_at`.
- **Reads**: `overview`, `badges`, `issues_page`, `clients_page` and the
  integrations / billing / onboarding / adoption / data-source lists read the
  rows (with SQL filters, sorts and paging — `issues/list` and `clients/list`
  already page on the server), then apply the resolutions in force at read
  time (so a Resolve shows at once) and the platform issues that are not per
  restaurant (the scheduler, the error rate, backups, providers). The client
  page (`GET /admin/api/client/<rid>`) keeps building its one record live,
  and writes the row it built, so opening a client refreshes it.
- **Freshness is shown**: every payload's `generated_at` becomes the oldest
  `computed_at` among the rows it read, and `age_seconds` from it; the amber
  "some figures are unknown" line covers rows with `error`.
- **Rollups stay where they are**: `ai_usage_daily`, `ai_validation_daily`,
  `business_metrics_daily`, `value_figures_daily`, `account_risk_state` — the
  health row reads them, it does not replace them.

## Migration

1. Build the table and the job; run it in shadow and compare each row's
   health and issue keys against the live build on the same data (a test over
   the fixture fleets, and a console-only diff view for a week in production).
2. Switch `badges` and the paged lists to the rows; keep `?fresh=1` as "build
   live for this request" for a while.
3. Switch Overview and the remaining lists; retire the fleet memo and its
   busy refusal once nothing reads the live build but the client page.

## What it does not change

The issue model (occurrence-scoped resolve, its history, the self-clearing
issues that cannot be resolved), the support masking (applied to the JSON
answer, whatever it is built from), the single definition of health in
`location_record`, and the rule that a failed read is unknown, never zero.
