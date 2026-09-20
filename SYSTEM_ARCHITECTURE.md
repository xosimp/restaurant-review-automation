# System Architecture — Cavnar AI

## Folder structure

```
review_automation/
├── hosted_dashboard.py        # Flask app factory / entrypoint — registers every blueprint, boots the scheduler
├── main.py                    # CLI entrypoint for one-off fetch/report runs (non-web)
├── models.py                  # (6.8k lines) schema, migrations, every dataclass, most DB read/write functions
├── auth.py / auth_routes.py   # session/user model + /auth/* routes (web) 
├── client_api.py              # (6.7k lines) web dashboard's API — 162 routes, mostly delegate to mobile_api
├── mobile_api.py              # (5.1k lines) iOS API — 176 routes; the "real" implementation for shared logic
├── admin_routes.py / admin_ops.py / admin_events.py   # /admin console (Will-only)
├── labor.py                   # shift CSV ingestion, labor % math, schedule building glue
├── scheduler.py               # background job loop: fetches, digests, alerts, backups, DB snapshot
├── shift_quality.py           # pure scoring engine for a generated schedule (no I/O)
├── ask_cavnar.py / ask_cavnar_tools.py   # the in-app AI assistant: context builder + tool registry
├── home_brief.py              # deterministic Home-tab payload (no AI on load)
├── notify.py / push.py        # email/SMS alert firing, APNs push delivery
├── ai_utils.py                # Anthropic client wrapper: budget guard, retry, usage logging
├── inventory.py / inventory_ledger.py   # Food Cost module
├── marketing*.py              # Marketing module (drafts, publishing, media, links, signals)
├── competitor*.py             # Intel module's competitor snapshots
├── docusign_helper.py         # contract send/status via DocuSign eSignature API
├── pricing.py                 # single source of truth for every dollar figure quoted anywhere
├── webhook_routes.py / webhooks.py       # outbound webhook delivery + inbound Stripe/Twilio webhooks
├── toast.py, square.py, clover.py, gmb.py, meta_api.py, weather.py   # POS / platform integrations
├── sales_audit_*.py           # /admin/audits in-person sales tool
├── emails.py                  # every transactional/marketing email template + send function
├── models.py's ensure_columns()   # additive migration path, runs on every boot
├── templates/
│   └── dashboard.html         # the ENTIRE web app — one Jinja template, one inline <style>/<script>
│                               #   per module panel (#panel-labor, #panel-reviews, ...), ES5-only JS
├── static/                    # fonts, images, cavnar-orb.js (loading animation)
├── ios/CavnarAI/CavnarAI/
│   ├── Core/                  # APIClient, SessionStore, AppEnvironment
│   ├── DesignSystem/          # colors, type, motion (CavnarMotion.swift), AccountSheetKit
│   ├── Features/              # one folder per module — Home, Labor, Reviews, FoodCost, Marketing,
│   │                          #   Intel, AskCavnar, Account, Auth, Notifications, ScheduleHistory, ...
│   ├── Models/                # Codable structs mirroring API JSON shapes
│   └── Push/                  # APNs registration/handling
├── scripts/                   # one-off / CI scripts: check_colors.py, build_contract_pdf.py, ...
├── tests/                     # ~1,900 pytest tests, one file per concern (see TESTING.md)
├── docs/contracts/            # the contract PDF DocuSign's template is built from
├── public/                    # the Cloudflare-Worker-served marketing site (cavnar.ai) — separate deploy
└── design/, brand/            # brand assets, logo source files
```

## Request flow

```
Browser/iOS ──HTTPS──▶ gunicorn (Railway) ──▶ Flask app (hosted_dashboard.py)
                                                  │
                                     Blueprint dispatch (registered in hosted_dashboard.py):
                                     admin_bp · audit_bp · webhook_bp · social_bp · auth_bp ·
                                     client_bp · toast_bp · square_bp · clover_bp · status_bp · mobile_bp
                                                  │
                          ┌───────────────────────┼────────────────────────┐
                          ▼                       ▼                        ▼
                 client_bp route          mobile_bp route           admin_bp route
                 (web, session cookie)    (iOS, Bearer token)       (Will only)
                          │                       │
                          └──────_m("mobile_x")───┘   (client route delegates to the mobile
                                                        implementation so both surfaces compute
                                                        the same payload from one code path)
                                                  │
                                        models.py / labor.py / ask_cavnar.py / ...
                                                  │
                                          get_conn(DB_PATH) → SQLite (reviews.db, Railway volume)
```

`client_api.py`'s `_m(name)` helper (`getattr(mobile_api, name)`, unwrapping the auth decorator) is the load-bearing pattern that keeps web and iOS from drifting into two different answers to the same question — see `PROJECT_CONTEXT.md`'s "delegation over duplication" rule.

## Database

One SQLite file (`reviews.db`), WAL mode, on a Railway persistent volume. `models.get_conn(db_path)` opens a tracked connection (`_TrackedConnection`, weak-referenced so a leaked connection can be swept by `close_thread_connections()` on request teardown). Schema is created by `init_db()` and additively migrated by `ensure_columns()` — both run on every boot; there is no separate migration-runner or version table, ordinary `ALTER TABLE ADD COLUMN` guarded by `try/except`. Full table list and shapes: `DATABASE_SCHEMA.md`.

## Failure & resiliency (audit #21)

**Bounds, because the platform is one process with four request threads.**

| Bound | Where | Why |
|---|---|---|
| `FETCH_WORKERS` / `FETCH_MAX_SECONDS` + `job_cursors` | `scheduler.py` | A serial pass over every restaurant ran for hours and never reached the tail; the cursor stops the bound from starving the same tail every pass |
| `ASK_MAX_CONCURRENT` | `client_api.py` | Ask spawned an unbounded daemon thread per request |
| `MAX_CSV_ROWS` | `client_api.py` | Parse/analyse/store run synchronously in a request |
| `CB_FAILURE_THRESHOLD` / `CB_OPEN_SECONDS` | `ai_utils.py` | Retry is the wrong answer to a provider outage |
| `timeout=` on every outbound call | enforced by `scripts/check_timeouts.py` | One hung call is 25% of capacity |
| `_claim_fallback` | `ops.py` | `claim_period` and the scheduler lease both fail open on the same dependency |

**Signals.** `/health` reports db, scheduler heartbeat **and disk** (a full
volume fails writes while reads succeed). `status_manager.health_snapshot`
holds the body so it is testable without booting the app.
`http_layer.request_metrics` is a rolling in-process latency/error window.
`admin_ops.overview` raises platform issues for job failures, a stale
scheduler, a 5xx spike, and **fetch coverage** — the one check that can tell
"no new reviews" apart from "never reached".

**Concurrency.** `update_restaurant(..., expected_version=)` is optimistic
locking on the restaurants row; omitting it keeps last-write-wins.

**Recovery.** `RECOVERY.md`.

---

## Scheduling / background jobs (`scheduler.py`)

A single `scheduler_loop()` running in a background thread, woken on an interval, that checks a `scheduler_lease` row before doing anything — one worker holds the lease and runs the jobs; the others no-op. Each job type also claims its period in `job_period_claims` before starting (claim-before-work), so a slow run and a subsequent tick can't both fire the same job. Failures are recorded in `job_failures`/`job_runs` rather than silently retried into a notification storm. Key jobs: `run_daily_fetch` (reviews/labor/inventory pull), `run_weekly_digests`, `check_daily_alerts` / `check_extra_daily_alerts`, `run_onboarding_sequence`, `run_monthly_summaries`, `backup_db` (writes a redacted, consistent snapshot and prunes old ones), `run_toast_sync` / `run_daily_depletion_sync`, `run_weekly_competitor_analysis`, `run_weekly_ai_visibility`.

The **shift-scheduling** feature (an owner clicking "Generate optimized schedule") is a separate, synchronous, on-demand flow — see `MODULE_OVERVIEW.md`'s Labor section and `shift_quality.py`'s architecture below. It is not a background job.

### Shift Quality Engine (`shift_quality.py`)

A pure evaluation layer with no I/O: `ShiftContext` (what happened) → per-dimension `DimensionResult` → the `DIMENSIONS` registry combines them into one 0–100 score. Three invariants hold everywhere in this file:
1. A dimension with no data returns `None`, never `0` — weights renormalize across whatever dimensions *do* have data.
2. A critical dimension under its floor **caps the shift at its own score** (never drags it further via an arbitrary penalty).
3. Confidence (how much data backed the score) is tracked separately from the score itself — a high-confidence 60 and a low-confidence 60 are reported differently.
`SUBSTANTIVE_DIMENSIONS` gates the "fatigue alone can't produce a score" rule. The what-if / swap evaluator (`_SwapIndex`) only considers same-role swaps and is bounded (`MAX_CANDIDATE_EVALUATIONS`) so it stays O(1)-ish per legality check rather than re-scanning the whole schedule.

## The day's jobs (workflow audit #19)

Owner-facing jobs are attempted **hourly** and gated per restaurant on its own local hour, once per local day: `weekly_digest` (9am), `monthly_summary` (9am on the local 1st), `onboarding` (10am), the daily alert checks (10am, `notify._gated_out`) and `issue_signals` (10am). Infrastructure jobs keep their Chicago-time daily claims.

Every tick: `notify.release_due_alerts()` (alerts held through a rush). Every 20 minutes: `intraday_capture` (hourly per restaurant while open), `pre_dinner_pulse` (4pm local, one push when the day is materially off), `coverage_check` (scheduled staff not clocked in), `preshift_nudge` (the hour the owner picked, off by default).

## Strategic jobs (`strategy_jobs.py`, gated in `scheduler.scheduler_loop`)

- `loss_sync` — daily after `pos_sync` (3am CT+): comps/voids/refunds into `pos_loss_daily`; an unsupported POS is normal, not a failure.
- `outcome_evaluations` — daily 6am CT+: closes outcome trackers whose window ended, marks met goals.
- `auto_draft_schedule` — Thursday 6am CT+: drafts next week's schedule for `auto_draft_schedule=1` restaurants with no `external_scheduling_tool` and no schedule in the last 5 days. A draft in Schedule History only; the push is sent only when the job row says `done`.
- `issue_scan` — hourly: bad reviews → issues where a manager is routed.
- `closing_summary` — at each restaurant's own close: tonight against a typical same weekday plus the close-out handover, to the brief's audience. Skipped in quiet hours and when there is nothing to say.
- `demand_opportunity` — weekly (claimed on the ISO week, not the date): a reliably quiet weekday two days out, Marketing module only.
- Every tick: `issues.tick()` (held notifications, one escalation) and `morning_brief.run_due()` (per-restaurant local hour, claimed per restaurant per day). The brief goes to every recipient in `morning_brief.recipients()` — owners and managers by default, per-login opt-out in `login_prefs` — each built from that person's own permissions (built once per distinct view) and pushed to that person's devices only, or emailed.

**Loop hazard (fixed Sep 19 2026):** never assign to a name inside `scheduler_loop` that is also a module helper it calls — Python makes it local for the whole function. `_due = run_due_posts(...)` did exactly that and every tick died on `UnboundLocalError`. `tests/test_scheduler_catchup.py` now drives a real tick.

## Notification system

Three delivery channels, one firing decision layer:
- **Email** (`notify.py`, `emails.py`) via Resend.
- **SMS** (`notify.py`) via Twilio, signature-validated on inbound.
- **Push** (`push.py`) via APNs (JWT provider auth, environment-aware host).

**Two entry points, one delivery path.** `blast()` inside `fire_review_alerts` raises review alerts; `notify.raise_alert()` raises everything else (`check_daily_alerts`, `check_extra_daily_alerts`, `check_no_response_alerts`). Both check the gates — the daily ceiling (`_over_alert_ceiling`, `ALERT_HARD_CEILING_PER_DAY = 50`), the owner's `alert_max_per_day` (`_daily_alert_suppressed`), quiet hours (bypassed for true health alerts via `health_bypasses_quiet_hours`) — then hold through a rush (`rush_release_at` / `hold_alert`, released by `release_due_alerts` every tick) or hand off to `deliver_alert()`, which owns channel selection, logging and the webhook. Nothing may deliver around it.

**The morning batch.** `run_daily_alert_checks` opens `notify.begin_daily_batch()` before the three 10am checks and flushes after. Types in `DAILY_BATCH_TYPES` are collected across the whole pass and sent as ONE notification per restaurant, worst first, when more than one fired — eight separate notifications about the same week of trading is what teaches an owner to stop reading. Each folded type still writes its own `alert_log` row, so the 7-day repeat windows are unchanged; the `daily_briefing` wrapper is in `models.NON_ALERT_TYPES` and does not spend the cap.

**Priority.** `push.PRIORITY` maps every alert type to P0–P5 and is the single source for three things: whether the phone may break a Focus mode (`interruption-level`, P0/P1 only), how long APNs keeps retrying (`apns-expiration`), and how both notification centers rank and filter. Stored on `alert_log.priority` at write time so a later change to the map cannot rewrite history.

**Push failure classification** (`push._classify`) — the P0 of audit #19. `dead` (Apple says the token is gone) deletes it; `provider` (our key, our topic, a 5xx, a network drop) never touches the failure counter and raises one `ops.capture` per hour; an unclassified 4xx counts, and at `_AUTO_DISABLE_AFTER` the token is PARKED (`disabled_reason`), not deleted. A 403 re-mints the cached JWT. Before this, ten alerts sent during an expired `.p8` deleted every device at every restaurant.

Every fired alert is logged to `alert_log` — the durable record `ask_cavnar_tools._read_alerts` and the Ask Cavnar snapshot read back from — and so are the advisory notifications that push directly (`notify.record_notification`: the brief, the pulse, the closing summary, a drafted schedule, an issue, a coverage gap, an outcome win). Opens are recorded in `notification_opens`, which powers `notify.engagement_report` (a suggestion to the owner, never an automatic downgrade) and admin's open-rate panel. See `project_job_and_alert_architecture` — the 50/day ceiling and claim-before-work pattern exist specifically to stop one bad fetch from becoming 180 notifications, a real incident this system had.

## Auth

- **Web**: session cookie, `sessions` table (`auth.create_session`), `login_required` decorator checks `get_current_user()`. CSRF token required on state-changing POSTs (`csrf.py`, `_csrf_fetch.html` include).
- **iOS**: Bearer token in `Authorization` header, same `sessions` table, `mobile_login_required` decorator (`mobile_api.py`).
- **2FA**: SMS or email OTP (`_billing_blocked`/2FA-setup routes in `auth_routes.py`/`mobile_api.py`), with `two_fa_backup_codes` as a hashed, single-use fallback.
- **Team access**: a restaurant can have multiple `users` rows; `role` distinguishes `owner` from teammate — only `owner` can invite/revoke team members (`auth.invite_team_member` / `revoke_team_member`).
- **Login history**: every successful login writes one `login_history` row (never pruned — independent of `sessions`' hard-deletes on expiry/revoke), giving a real "sign-in activity" audit trail.
- **Device trust / sessions list**: `trusted_devices`, `get_sessions_for_user` (with device de-dup), `revoke_other_sessions`.
- **Admin**: separate `admin_required` decorator; the admin console is Will-only, no client `role` reaches it.

## Payments — Stripe

`stripe_customer_id` lives on `restaurants`. `emails.create_stripe_checkout` builds a Checkout Session for the setup fee + retainer at signup (amounts always read from `pricing.TIERS`, never hardcoded — see `PROJECT_CONTEXT.md`). `webhook_routes.py` verifies inbound Stripe webhooks (`stripe.Webhook.construct_event`) and de-dupes by event id via `stripe_events_seen` before acting, so a Stripe retry can never double-process a payment event.

## Intel module

`competitor.py` / `competitor_intel_format.py` pull nearby competitor data (Google Places) into `competitor_snapshots`, diffed week over week. AI-visibility checking (`ai_visibility_runs` / `ai_visibility_query_runs`) asks Perplexity a fixed set of queries an actual customer might ask and records whether/how the restaurant is mentioned — reported as a **range** over the sample of queries, never a single false-precision number, and never blended with "is Cavnar AI set up for this client" (see `project_intel_module` invariants: a missing measurement is never 0, no unsourced claims about AI indexing).

## Cross-cutting: what to read before touching each area

| Touching... | Read first |
|---|---|
| Any route in `client_api.py` | Check whether `mobile_api.py` already has the logic — delegate via `_m()` |
| A new `restaurants` column | The "four-plus-one touch points" rule in `PROJECT_CONTEXT.md` |
| Labor / scheduling | `MODULE_OVERVIEW.md` Labor section + `shift_quality.py`'s module docstring |
| Ask Cavnar | `ask_cavnar_tools.py`'s `TOOLS` registry (kind: read/action/write) |
| Anything emailed | `emails.py` — ~27 send-sites across 10 files, one `RESEND_API_KEY` pattern to get right |
| Dashboard JS | ES5-only, even in comments — `tests/test_frontend_rules.py` |
| A color | Goes through a CSS variable, never a literal — `scripts/check_colors.py` |
