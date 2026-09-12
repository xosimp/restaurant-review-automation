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

## Scheduling / background jobs (`scheduler.py`)

A single `scheduler_loop()` running in a background thread, woken on an interval, that checks a `scheduler_lease` row before doing anything — one worker holds the lease and runs the jobs; the others no-op. Each job type also claims its period in `job_period_claims` before starting (claim-before-work), so a slow run and a subsequent tick can't both fire the same job. Failures are recorded in `job_failures`/`job_runs` rather than silently retried into a notification storm. Key jobs: `run_daily_fetch` (reviews/labor/inventory pull), `run_weekly_digests`, `check_daily_alerts` / `check_extra_daily_alerts`, `run_onboarding_sequence`, `run_monthly_summaries`, `backup_db` (writes a redacted, consistent snapshot and prunes old ones), `run_toast_sync` / `run_daily_depletion_sync`, `run_weekly_competitor_analysis`, `run_weekly_ai_visibility`.

The **shift-scheduling** feature (an owner clicking "Generate optimized schedule") is a separate, synchronous, on-demand flow — see `MODULE_OVERVIEW.md`'s Labor section and `shift_quality.py`'s architecture below. It is not a background job.

### Shift Quality Engine (`shift_quality.py`)

A pure evaluation layer with no I/O: `ShiftContext` (what happened) → per-dimension `DimensionResult` → the `DIMENSIONS` registry combines them into one 0–100 score. Three invariants hold everywhere in this file:
1. A dimension with no data returns `None`, never `0` — weights renormalize across whatever dimensions *do* have data.
2. A critical dimension under its floor **caps the shift at its own score** (never drags it further via an arbitrary penalty).
3. Confidence (how much data backed the score) is tracked separately from the score itself — a high-confidence 60 and a low-confidence 60 are reported differently.
`SUBSTANTIVE_DIMENSIONS` gates the "fatigue alone can't produce a score" rule. The what-if / swap evaluator (`_SwapIndex`) only considers same-role swaps and is bounded (`MAX_CANDIDATE_EVALUATIONS`) so it stays O(1)-ish per legality check rather than re-scanning the whole schedule.

## Notification system

Three delivery channels, one firing decision layer:
- **Email** (`notify.py`, `emails.py`) via Resend.
- **SMS** (`notify.py`) via Twilio, signature-validated on inbound.
- **Push** (`push.py`) via APNs (JWT provider auth, environment-aware host).

Firing goes through `notify.py`'s alert functions (`fire_review_alerts`, `check_daily_alerts`, `check_no_response_alerts`, etc.), each of which checks: is this restaurant over its daily alert ceiling (`_over_alert_ceiling`, `alert_max_per_day`), is this specific alert type already suppressed today (`_daily_alert_suppressed`), is it quiet hours (bypassed for true health alerts via `health_bypasses_quiet_hours`), and does the owner's `alert_contacts` / per-channel opt-in (`al_*_email/sms/push` columns) even want this channel. Every fired alert is logged to `alert_log` — the durable record `ask_cavnar_tools._read_alerts` and the Ask Cavnar snapshot read back from, independent of whatever's currently unread. See `project_job_and_alert_architecture` — the 50/day ceiling and claim-before-work pattern exist specifically to stop one bad fetch from becoming 180 notifications, a real incident this system had.

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
