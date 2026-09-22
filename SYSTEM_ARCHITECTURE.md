# System Architecture — Cavnar AI

## Folder structure

```
review_automation/
├── hosted_dashboard.py        # Flask app factory / entrypoint — registers every blueprint, boots the scheduler
├── config.py                  # the environment values several modules read (base URL, sender, on-Railway, Places key)
├── demo_seed.py               # the Gia Mia / Simple EJ's demo accounts and their nightly refresh, off the request path
├── main.py                    # CLI entrypoint for one-off fetch/report runs (non-web)
├── models.py                  # (9k lines) schema, migrations, every dataclass, most DB read/write functions
├── auth.py / auth_routes.py   # session/user model, staff portal tables, /auth/* routes (web)
├── security.py / security_headers.py / credentials.py / csrf.py / guest_links.py / http_layer.py
│                              # durable login throttling + freeze, response headers, Fernet at rest, CSRF, signed links, gzip/metrics
├── client_api.py              # (8k lines) web dashboard's API — 194 routes; 58 delegate to mobile_api via _m(), the rest own or share a _do_* body
├── mobile_api.py              # (6k lines) iOS API — 205 routes; the "real" implementation for shared logic
├── strategy_routes.py         # 68 route bodies registered once each at /api/… and /mobile/api/… (the twin pattern to prefer)
├── staff_routes.py / staff_schedule.py / staff_roster.py / time_off.py / labor_replacements.py / preshift.py
│                              # the staff portal: PIN sign-in, today's schedule, availability, time off, pre-shift read
├── admin_routes.py / admin_ops.py / admin_events.py   # /admin console (Will-only)
├── labor.py                   # shift CSV ingestion, labor % math, the one schedule model call
├── schedule_engine.py / schedule_rules.py / staff_settings.py / demand_signals.py / schedule_versions.py / shift_requests.py
│                              # the schedule pipeline: constraints, backstops, rule sweep, versions, roster, dated demand, shift swaps
├── scheduler.py               # background job loop: the registry is in §Scheduling below
├── strategy_jobs.py           # the scheduled half of the strategic features (loss sync, outcomes, weekly plan, ...)
├── delayed.py / decisions.py  # undo-window actions; the owner's decision record
├── milestones.py / good_news.py / first_look.py / monthly_review.py / weekly_review.py / review_common.py
│                              # the moments an owner reads: firsts, wins, the month and week in review (shared sentences in review_common)
├── intelligence/              # the cross-restaurant learning layer (INTELLIGENCE_ENGINE.md)
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
├── pos.py                     # the provider registry every POS feature dispatches through
├── toast.py, square.py, clover.py, rpower.py (+ *_routes.py), gmb.py, meta_api.py, weather.py   # POS / platform integrations
├── sales_audit_*.py, sales_audits.py   # /admin/audits in-person sales tool (audit_app.py is a separate, standalone scorecard app on :9000)
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
├── tests/                     # pytest, one file per concern (see TESTING.md for the count command)
├── docs/ops/                  # RECOVERY, SECURITY, RAILWAY_SCHEDULER_SPLIT, PIN_PEPPER_RUNBOOK
├── docs/plans/                # designs with no code yet
├── docs/history/              # superseded material, kept for the record
├── docs/contracts/            # the contract PDF DocuSign's template is built from
├── public/                    # the Cloudflare-Worker-served marketing site (cavnar.ai) — separate deploy
└── brand/                     # brand sources (assets/), the social upload kit, the print one-sheet
```

## Request flow

```
Browser/iOS ──HTTPS──▶ gunicorn (Railway) ──▶ Flask app (hosted_dashboard.py)
                                                  │
                                     Blueprint dispatch (registered in hosted_dashboard.py):
                                     admin_bp · audit_bp · webhook_bp · social_bp · auth_bp · client_bp ·
                                     toast_bp · square_bp · clover_bp · rpower_bp · status_bp · mobile_bp ·
                                     strategy_bp · strategy_mobile_bp · issue_link_bp · staff_bp
                                     (16; hosted_dashboard refuses to boot on a duplicate (path, method))
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

One SQLite file (`reviews.db`), WAL mode, on a Railway persistent volume. `models.get_conn(db_path)` opens a tracked connection (`_TrackedConnection`, weak-referenced so a leaked connection can be swept by `close_thread_connections()` on request teardown). Schema is created at boot: `init_db()` (most tables, plus its own ALTER list), `ensure_columns()` (a second additive list), then the `init_*` functions `hosted_dashboard.py` calls next (`auth`, `push`, `webhooks`, `guest_marketing`, `sales_audits`, `ops`). There is no migration runner or version table — ordinary `ALTER TABLE ADD COLUMN` guarded by `try/except`. Full table list and shapes: `DATABASE_SCHEMA.md`.

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

**Recovery.** `docs/ops/RECOVERY.md`.

---

## Scheduling / background jobs (`scheduler.py`)

A single `scheduler_loop()` running in a background thread, ticking every five minutes, that checks a `scheduler_lease` row before doing anything — one worker holds the lease and runs the jobs; the others no-op. Each job claims its period in `job_period_claims` before starting (claim-before-work), so a slow run and a subsequent tick can't both fire the same job. Failures are recorded in `job_failures`/`job_runs` rather than silently retried into a notification storm.

**The registry** (gates are Chicago time unless marked *local*; "hourly" jobs are attempted every hour and gate per restaurant inside on its own local hour, claiming once per restaurant per local day — `scheduler.local_due`):

| Claim key | When | Runs |
|---|---|---|
| `backup_db` | 2am | `backup_db` — a consistent, **unredacted** snapshot on the volume (only the emailed copy is redacted), then `prune_ledgers` |
| `restore_drill` | 2nd of Jan/Apr/Jul/Oct | `run_restore_drill` |
| `pos_sync`, `loss_sync` | 3am | `run_toast_sync` (every provider in `pos.PROVIDERS`), `run_loss_sync` |
| `intelligence_features`, `intelligence_learning` | 3am, 4am | `intelligence.jobs.run_features` (bounded, cursor in `job_cursors`), `run_learning` |
| `marketing_metrics_sync` | 4am | `run_marketing_metrics_sync` |
| `inventory_depletion`, `food_cost_snapshots` | 5am | `run_daily_depletion_sync`, `run_food_cost_snapshots` |
| `review_diagnoses`, `food_cost_diagnoses`, `outcome_evaluations` | 6am | the two root-cause passes, then `run_outcome_evaluations` |
| `competitor_analysis`, `ai_visibility` | Mon 6am, Mon 7am | `run_weekly_competitor_analysis`, `run_weekly_ai_visibility` |
| `auto_draft_schedule` | Thu 6am | `run_auto_draft_schedules` |
| `milestones`, `refresh_tokens` | 7am | `run_milestones`, `refresh_expiring_tokens` |
| `weekly_plan` | Mon, hourly → 7am *local* | `run_weekly_plan` |
| `recipe_drafts` | Tue, hourly → 5am *local* | `run_recipe_drafts` |
| `trusted_orders` | Mon, hourly → 8am *local* | `run_trusted_orders` |
| `auto_publish_schedule` | Fri, hourly → 9am *local* | `run_auto_publish_schedules` — held when `client_api.publish_blockers` is non-empty |
| `quality_calibration` | Sun 5am | `strategy_jobs.run_quality_calibration` — nudges a restaurant's quality weights from its own clean vs troubled published weeks; never lowers, never before 8 weeks |
| `review_fetch` | 8am, 12pm, 4pm, 8pm (latest missed slot only) | `run_daily_fetch` — bounded pool, cursor in `job_cursors` |
| `ops_digest` | 8am | `ops.send_failure_digest` (only if something failed) |
| `weekly_digest` | hourly → 9am *local* on the digest day | `run_weekly_digests` |
| `monthly_summary`, `quarterly_summary` | hourly → 9am *local* on the restaurant's own 1st (`local_due(day=1)`) | `run_monthly_summaries`, `run_quarterly_summaries` |
| `daily_alerts` | hourly → 10am *local* (`notify._gated_out`) | `run_daily_alert_checks` (the morning batch) |
| `onboarding` | hourly → 10am *local* | `run_onboarding_sequence` |
| `issue_scan` | hourly → 10am *local* | `run_issue_scan` |
| `review_request_followups` | hourly | `run_review_request_followups` |
| `stale_inventory` | Mon 10am | `check_stale_inventory` |
| `inactive_clients` | Mon 11am | `check_inactive_clients`, `send_while_away_nudges` |
| `optin_invite` | 11am–`OPTIN_INVITE_LATEST_HOUR` | `guest_marketing.run_toast_optin_invites` |
| `campaign_attribution` | noon | `run_campaign_attribution` |
| `intraday` | every 20 min | `run_intraday_capture`, `run_pre_dinner_pulse`, `run_coverage_check`, `run_preshift_nudge`, `run_closing_summary`, `run_demand_opportunity` (each gates itself) |
| `prune_login_attempts` | daily | drops `login_attempts` rows older than two days |
| every tick | — | `notify.release_due_alerts`, `issues.tick`, `morning_brief.run_due`, `delayed.run_due`, `marketing_publish.run_due_posts`, the heartbeat |

`admin_ops.RUNNABLE_JOBS` is the admin console's "run now" map onto the same functions.

The **shift-scheduling** feature (an owner clicking "Generate optimized schedule") is an on-demand async job (`ops.async_jobs`, one per restaurant at a time — `ops.active_job`), run by `schedule_engine._run_schedule_job` — see `MODULE_OVERVIEW.md`'s Labor section. It is not a scheduled job; only the Thursday draft and the Friday publish are.

### Shift Quality Engine (`shift_quality.py`)

A pure evaluation layer with no I/O: `ShiftContext` (what happened) → per-dimension `DimensionResult` → the `DIMENSIONS` registry combines them into one 0–100 score. Three invariants hold everywhere in this file:
1. A dimension with no data returns `None`, never `0` — weights renormalize across whatever dimensions *do* have data.
2. A critical dimension under its floor **caps the shift at its own score** (never drags it further via an arbitrary penalty).
3. Confidence (how much data backed the score) is tracked separately from the score itself — a high-confidence 60 and a low-confidence 60 are reported differently.
`SUBSTANTIVE_DIMENSIONS` gates the "fatigue alone can't produce a score" rule. The what-if / swap evaluator (`_SwapIndex`) only considers same-role swaps and is bounded (`MAX_CANDIDATE_EVALUATIONS`) so it stays O(1)-ish per legality check rather than re-scanning the whole schedule.

## The day's jobs (workflow audit #19)

Owner-facing jobs are attempted **hourly** and gated per restaurant on its own local hour, once per local day: `weekly_digest` (9am), `monthly_summary` (9am on the restaurant's own 1st — the calendar gate is `local_due(day=1)`, lost once in Sep 2026 and pinned by a test since), `onboarding` (10am), the daily alert checks (10am, `notify._gated_out`) and `issue_scan` (10am). Infrastructure jobs keep their Chicago-time daily claims.

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

- **Web**: session cookie, `sessions` table (`auth.create_session`), `auth.login_required` decorator checks `get_current_user()`. CSRF token required on state-changing POSTs (`csrf.py`, `_csrf_fetch.html` include). Login throttling is durable (`security.login_throttled`, `login_attempts` table).
- **iOS**: Bearer token in `Authorization` header, same `sessions` table, `auth.mobile_login_required` decorator.
- **Staff**: PIN identity in the staff portal (`staff_routes.py`, `auth.staff_login_required`, `memberships` + `staff_portal_tokens`; pepper in `docs/ops/PIN_PEPPER_RUNBOOK.md`).
- **2FA**: SMS or email OTP (setup routes in `auth_routes.py`/`mobile_api.py`; `auth._billing_blocked` gates a lapsed account at the decorator), with `two_fa_backup_codes` as a hashed, single-use fallback.
- **Team access**: a restaurant can have multiple `users` rows; `role` distinguishes `owner` from teammate — only `owner` can invite/revoke team members (`auth.invite_team_member` / `revoke_team_member`).
- **Login history**: every successful login writes one `login_history` row (never pruned — independent of `sessions`' hard-deletes on expiry/revoke), giving a real "sign-in activity" audit trail.
- **Device trust / sessions list**: `trusted_devices`, `get_sessions_for_user` (with device de-dup), `revoke_other_sessions`.
- **Admin**: separate `auth.admin_required` decorator. `is_admin` logins write; the `support` role (`permissions.ROLE_SUPPORT`) reads the console and opens view-as but every write returns 403; no client role reaches it.

## Deployment

Railway runs `railway.json`'s `startCommand` (gunicorn, one worker, four threads). The `Procfile` (`web: python hosted_dashboard.py`, the Flask dev server) is not what production runs; it is only picked up by a platform that has no `railway.json`. The marketing site is a hand-deployed Cloudflare Worker (`wrangler.jsonc`, `public/` only). CI (`.github/workflows/ci.yml`) compiles every module, runs the colour and silent-handler lints and pytest; the iOS build is not in CI.

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

## Intelligence engine

Two nightly jobs in the scheduler: `intelligence_features` at 3am (one
feature row per active restaurant per ISO week; worker pool, wall-clock
bound, cursor in `job_cursors`) and `intelligence_learning` at 4am
(feedback sync → pattern discovery → benchmarks → confidence log, reading
only the materialized `intel_*` tables). Request paths read materialized
rows only: Home attaches a confidence band to each recommendation, Ask has
two read tools and one context section, admin has `/admin/api/intelligence`.
Design and privacy rules: `INTELLIGENCE_ENGINE.md`.
