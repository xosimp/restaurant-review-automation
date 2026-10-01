# Cavnar AI

Restaurant intelligence SaaS, built and operated by Will Cavnar. Independent
restaurant owners get one dashboard, on the web and an iOS app, that reads
their POS, review and inventory data and tells them what to do about it. It
is a decision layer that names the next move and the dollars behind it, not
a BI tool that shows charts.

This file is the way in. It explains what the product is, how to run it and
how it is built, then points to the reference docs. It replaces
`PROJECT_CONTEXT.md` and `CAVNAR_AI_ENGINEERING_GUIDE.md` (merged here
9/30/26). **Agents:** `CLAUDE.md` holds the standing rules and is loaded
automatically. Where this file and a detailed doc disagree, the detailed doc
is right; fix this one.

---

## The business

- **First paying client.** Erik Baylis, owner of Simple EJ's (St. Charles).
  It is production restaurant 5; restaurant 4 is Will's demo copy. Erik's
  stated pains are labor "through the roof", weak repeat visits and
  training, and they are the product's proof case.
- **Pricing.** `pricing.py`'s `TIERS` is the single source, mirrored on
  `public/pricing.html`. Each module carries a one-time setup fee at
  checkout plus a monthly or annual retainer from day 31:

  | Modules | Setup | Monthly |
  |---|---|---|
  | 1 | $750 | $349 |
  | 2 | $1,500 | $649 |
  | 3 | $2,250 | $899 |
  | 4 (full system) | $3,000 | $1,199 |

  Annual is 10× the monthly.
- **Contracting.** DocuSign (`docusign_helper.py`) sends and tracks the
  service contract. It is live in production, either on account creation or
  sent from the console.
- **Billing.** Stripe collects the setup fee at checkout and the retainer
  after it (`webhook_routes.py`, `billing_jobs.py`).
- **Erik's other systems.**
  - RPOWER, the POS: live since 9/28/26; read-only, one week per request
    (`rpower.py`).
  - Back Office by Buyers Edge, for inventory and invoices: no API yet;
    `docs/plans/BACK_OFFICE_INTEGRATION.md`.
  - Also Hostie, Fourth, Tock, Control Play and Fishbowl.
- **Repository.** `xosimp/restaurant-review-automation`. The `main` branch
  is production.

## Running it locally

```bash
# Python 3.12.7 (.python-version) - the version production and CI run
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt       # the locked app dependencies + test tools
cp .env.example .env                      # then fill in what you need
PORT=5050 python3 hosted_dashboard.py     # http://localhost:5050
```

- **Port.** Use 5050, not the default 5000. The iOS debug build expects
  5050, and macOS's AirPlay Receiver owns 5000.
- **The database.** It is SQLite. Locally it is `reviews.db` beside the
  code; on Railway it is on the volume (`RAILWAY_VOLUME_MOUNT_PATH`). Every
  table is created at boot by `init_db()` and the `init_*` functions
  `hosted_dashboard.py` calls.
- **Environment.** `docs/ops/ENVIRONMENT.md` lists every variable the code
  reads. Unset model and email keys make every AI path and every send fall
  back to offline behaviour.
- **Scheduled jobs.** The scheduler never runs on a local backend
  (`scheduler.scheduling_allowed()`). A laptop with production's keys would
  otherwise re-send real briefs and texts. Never set
  `ALLOW_LOCAL_SCHEDULER=1` on a machine with production keys.
- **Worktrees.** Never run a copy of the app from a `.claude/worktrees/`
  checkout with Flask's own `.env` loading. It would load the main
  checkout's `.env` and so production's keys (`CLAUDE.md`).
- **The iOS app.** See `ios/CavnarAI/README.md`. The project is generated
  (`./generate.sh`), and a physical device reaches your backend through
  `.dev-url` (a LAN address or an ngrok tunnel).

## Testing

`TESTING.md` is the policy. In short:
- Run the test files that cover what you changed.
- Run the full suite only after an audit, a fix round or a large batch, and
  always in parallel:
  `python3 -m pytest -q -p no:warnings -n auto --dist loadfile`.
- CI (`.github/workflows/ci.yml`) runs on every push to `main`:
  - checks the dependency lock;
  - compiles every module;
  - runs the colour and silent-handler lints;
  - runs the full suite on Python 3.12.7 in **UTC**.

  UTC matters. From 7pm Central, UTC is already tomorrow, so code or a test
  that reads the machine's date instead of the restaurant's fails CI every
  evening (fixed 9/30/26; `time_utils.restaurant_now_by_id` is the
  restaurant's clock).

## Deploying

- **The dashboard** (`dashboard.cavnar.ai`) is on Railway. A push to `main`
  deploys it (`railway.json`: one container,
  `gunicorn --workers 1 --threads 4`, the scheduler thread inside the web
  process). Don't raise `--workers`, and don't split the scheduler into its
  own service. `docs/ops/RAILWAY_SCHEDULER_SPLIT.md` explains why.
- **`/health`** answers 200 for a degraded platform on purpose. A failing
  health check blocks the deploy that would fix it.
- **The marketing site** (`cavnar.ai`) is a separate Cloudflare Worker
  serving `public/`. A push does **not** deploy it; run `npx wrangler deploy`
  after a `public/` change.
- **Dependencies** are locked. Edit `requirements.in`, then re-lock
  `requirements.txt` with `pip-compile` under Python 3.12.
- **Incidents, restores and break-glass:** `docs/ops/RECOVERY.md`. Controls
  and their reasons: `docs/ops/SECURITY.md`.

---

## How it is built

One Flask backend (`hosted_dashboard.py` assembles the blueprints), one
SQLite database, and two clients that read the same computed data through
the same logic: a single-page web dashboard (`templates/dashboard.html`) and
a native SwiftUI app (`ios/CavnarAI`). The full topology, jobs and data flow
are in `SYSTEM_ARCHITECTURE.md`. Where a new file may go is in
`ARCHITECTURE_MANIFEST.md`.

### Principles that shape every design decision

- **One computation, two renderers.** Web and iOS never keep parallel
  implementations of the same business logic. `mobile_api.py` computes it
  once, `client_api.py` delegates via `_m()`, and both clients consume the
  same JSON.
- **Absence is not zero.** Every scoring and aggregation system (Shift
  Quality, Operational Score, Intel visibility, Labor's partial-period math)
  separates "we don't know" from "it's bad". A missing measurement returns
  `None` and the surrounding weights renormalise. It never silently becomes
  a zero that drags a score down.
- **The deterministic layer computes; AI narrates.** Every number a client
  sees (a percentage, a dollar figure, a score) comes from plain Python.
  The model only interprets and phrases what was already computed, and
  every output passes `response_validation` against the facts it was given.
  A claim it can't verify is marked, never smoothed over.
- **Propose, don't perform**, for anything with an effect outside the
  building. Ask Cavnar's write tools return a confirmation card. Nothing
  emails a supplier or posts publicly without the owner confirming through
  the same route a manual button uses.
- **Tenant isolation is server-side and non-negotiable.** Every query
  scopes by the `restaurant_id` of the authenticated session. A code path
  that trusts a client-supplied restaurant id is a P0 finding.
- **Additive schema changes only.** New columns come through guarded
  `ALTER TABLE`; there are no destructive rewrites.

### The modules

Full detail in `MODULE_OVERVIEW.md`.

| Module | Core job | Key files |
|---|---|---|
| **Home** | The day's position: last night, the one thing to do, what needs you. Deterministic, no model call on load | `home_brief.py` |
| **Reports** | The nightly Daily Sales Report: sales, labor (with salaries for the owner), reviews, food, intel and close-out; plus the week, the 4-4-5 period and the report list | `dsr/` (pipeline, `block_*.py`, `scorecard.py`, `narrative.py`, `deliver.py`) |
| **Reviews** | Fetch, then AI sentiment and urgency, then an AI draft reply, then the owner approves and posts | `fetcher.py`, `analyser.py`, `drafter.py`, `review_intelligence.py` |
| **Labor** | Shifts to labor % against target, overtime and overstaffing, the drafted and graded schedule, salaried staff, attendance | `labor.py`, `schedule_engine.py`, `schedule_rules.py`, `shift_quality.py`, `attendance.py` |
| **Food Cost** | Counts, waste, price drift, recipes and purchase orders | `inventory.py`, `inventory_ledger.py`, `ordering.py` |
| **Marketing** | AI-drafted posts, the content calendar, Campaign Studio (text, email and social from one goal), the guest text club | `marketing*.py`, `guest_marketing.py`, `guest_email.py` |
| **Intel** | Competitor snapshots and AI-visibility checks | `competitor.py`, AI-visibility routes |
| **Ask Cavnar** | The tool-calling assistant with live access to every module | `ask_cavnar.py`, `ask_cavnar_tools.py` |
| **POS integrations** | Toast, Square, Clover and RPOWER behind one contract; the ticket archive | `pos.py`, `rpower.py`, `toast.py`, `pos_archive.py` |
| **Staff portal and task sheets** | PIN sign-in for employees; opening and closing sheets by job code | `staff_routes.py`, `task_sheets.py` |
| **Admin** | Will's console: clients, jobs and the backup, billing and contracts, AI operations, messaging, the audit trail, support tools, the sales-audit tool | `admin_routes.py`, `admin_ops.py`, `admin_events.py` |
| **Platform** | The job loop and its registry, the backup, paging, `/health`, telemetry, the supervisor thread | `scheduler.py`, `jobs_registry.py`, `ops.py`, `status_manager.py`, `platform_monitor.py` |
| **Billing** | The Stripe and DocuSign lifecycle, the billing outbox, reconcile | `webhook_routes.py`, `billing_jobs.py`, `pricing.py` |
| **Intelligence engine** | What the platform learns across restaurants, under a cohort floor of five, and the confidence on each recommendation | `intelligence/` (`INTELLIGENCE_ENGINE.md`) |

"Scheduling" means two things here; don't conflate them. Shift Quality
grades a drafted staff schedule. `scheduler.py` runs the background jobs.

### Data model conventions

The full schema is in `DATABASE_SCHEMA.md`. These conventions apply
everywhere.

- **`restaurants` is the tenant root.** Nearly every other table has a
  foreign key to `restaurants.id`.
- **A new `restaurants` column needs four touch points:**
  1. the `Restaurant` dataclass field (`models.py`);
  2. a migration entry (`init_db()`'s ALTER list or `ensure_columns()`;
     both run at boot);
  3. `update_restaurant()`'s allowed-write whitelist;
  4. `get_restaurant()`'s hydration.

  Miss the whitelist and writes silently do nothing. Miss hydration and the
  value is in the database but invisible to every reader.
- **Employees are matched by name**, case- and spacing-insensitively. POS
  shift data carries no stable id for the person. The person record
  (`people.py`, `person_aliases`) ties a POS id to the name where one
  exists.
- **Append-only audit tables are never changed in place:**
  `login_history`, `ask_cavnar_actions`, `alert_log`,
  `capability_changes`, `admin_events`, `support_notes`,
  `billing_status_history`. Current state is always the latest row.
- **Retention has one registry**, `ops._RETENTION_DAYS` /
  `_RETENTION_COLUMN`. Beside each table it records a floor, the rollup run
  before its rows go, and its readers' windows (`_RETENTION_FLOOR_DAYS`,
  `_RETENTION_ROLLUP`, `_RETENTION_READERS`).
  - Tables are pruned nightly after the backup, in chunks under a time
    bound.
  - A window set under its floor is refused and paged, never applied.
  - A new ledger gets an entry there, never a prune loop of its own.
  - A reader that reaches past its table's window reads a summary written
    before the rows go (`history_rollups`), or it fails
    `tests/test_mem_m7_retention.py`.
  - Some rows survive a restaurant's deletion
    (`models._KEEP_ON_RESTAURANT_DELETE`):
    - `admin_events`, `offboarding_steps` and `billing_status_history`;
    - its anonymised learning, kept under a tombstoned id (never a demo's
      or a test account's).
- **An admin action is audited through one typed call**,
  `admin_events.record_admin_action(actor, event_type, restaurant_id=,
  target=, before=, after=, result=, summary=)`. A route never calls
  `admin_events.record("admin", …)`; `tests/test_fix_int2_admin.py`
  enforces it. A billing-state change by an admin runs inside
  `models.billing_context(...)`.
- **JSON in a column** is for structured settings that one piece of code
  reads and writes as one blob (`quality_weights_json`, ...). It is not for
  anything queried by an internal field.
- **`is_demo`** marks an account whose seed data may be reset. It is never
  true for a real client.
- **Every recommendation has one identity and one lifecycle:
  `rec_ledger`** (`rec_instances` + `rec_events`; `DATABASE_SCHEMA.md` →
  Recommendations).
  - A surface that shows one calls `rec_ledger.present()` with a key
    `kind:subject`, where the subject is what it is about
    (`trim_day:Saturday`).
  - Every answer calls `record()`.
  - Every surface checks `silenced_keys()` before speaking.
  - Where a change is actually **made**, call `rec_ledger.implemented()`.
    Never call it from a button that only says yes.
  - How long an answer holds is one rule, `rec_ledger.answer_silence`.
  - What the ledger learns is read through `rec_learning`.
- **Memory reaches a prompt through one assembler**,
  `memory_context.memory_context(rid, surface, viewer=, subjects=)`. It
  returns fenced, dated sections from the `PROVIDERS` registry (dispatched
  by string, so grep the registry, not the symbol), within per-section
  budgets.
  - Never hand-pick memory in a prompt builder.
  - Every line carries an audience, and a viewer never reads a line it may
    not.
  - An output more than one login reads is assembled for the team viewer.
  - A new surface must be classified (`tests/test_mem_int_privacy.py`).
- **A goal is the target a module judges against.** An account holder's
  active goal overrides the module's own setting everywhere a figure is
  judged (`owner_memory.target_for`, read by `thresholds.target_for`). A
  teammate's or an admin's goal waits for an account holder to confirm it.
- **Margins that fire advice are fitted per restaurant**
  (`restaurant_thresholds.margin`, refreshed nightly). The margin is the
  larger of the stated margin and 1.645 σ of the restaurant's own history.
  A new "over target" check reads
  `margin(rid, "labor_over_period", stated=LABOR_OVER_TARGET_PTS)`, not the
  constant.
- **Who teaches a pooled learner is one predicate,
  `models.learning_eligible`.** Who learns for itself is
  `models.learns_for_itself`. Whose answer it is is
  `permissions.answer_authority`; an admin's view-as write teaches nothing.
  Anything pooled across restaurants is de-identified ratios over at least
  five restaurants (`INTELLIGENCE_ENGINE.md` → *Who may teach*).
- **Time.** The server runs on UTC; the restaurant has a timezone.
  - A business day, "today" and "this month" are the restaurant's
    (`time_utils.restaurant_now_by_id`, `local_iso`). Never use the
    machine's `date.today()`.
  - Stored stamps are UTC.
  - Every date an owner sees reads M/D/YY (`time_utils.mdy`).

### API patterns

The full reference is `API_REFERENCE.md`. This is the load-bearing pattern:

```python
# client_api.py
@client_bp.route("/api/ask-cavnar/opening")
@login_required
def ask_cavnar_opening(current_user):
    return _m("mobile_ask_opening")(current_user)
```

`_m(name)` resolves `mobile_api.<name>` and unwraps its auth decorator, so a
web route and its iOS twin run the identical body. Write a shared endpoint
once in `mobile_api.py` and delegate from `client_api.py`.

- **Responses** are `jsonify(ok=True, ...)` or
  `jsonify(ok=False, error=...), 4xx`.
- **Sends.** Every email sender returns an `emails.SendResult`, and one
  email has one sender.
- **Background threads.** Work handed to a thread carries the requester's
  AI attribution (`ai_utils.attributed(fn)`, or the one admin job pool).
- **CSRF** is required on state-changing web requests.
- **Assumed IDOR.** Every new route is an IDOR until proven otherwise: does
  it take `restaurant_id` from the authenticated user, or trust a path,
  query or body value?

### AI providers

The per-call table is in `PROMPT_LIBRARY.md`.

- **Anthropic Claude** is used for everything except AI visibility:
  - Haiku for the high-volume classifiers;
  - Sonnet for anything an owner reads as advice;
  - Opus for invoice transcription.
- **Perplexity (`sonar`)** is used for Intel's AI-visibility check only.
- **Every Anthropic call** routes through `ai_utils.create_with_retry()`.
  It checks the budget and breaker, retries with backoff, and writes the
  `ai_usage` ledger and the redacted `ai_calls` trace. Never call
  `messages.create` directly.
- **Google Places** goes through `ai_utils.places_request`.
- **`temperature`** is never passed (`create_with_retry` strips it).
- **A finding about a model's output** is recorded with
  `ai_utils.record_quality_event`.

### Coding standards

- **Delegate, don't duplicate** (see the API pattern above).
- **Revert-check a bug fix.** Undo the fix, confirm the test written for it
  fails, then restore. A test that passes with the bug back protects
  nothing.
- **Silent `except: pass` around a database write fails lint**
  (`tests/test_silent_handler_lint.py`). Route the error to `ops.capture`.
- **Dashboard JS is ES5 only**, including inside comments
  (`tests/test_frontend_rules.py`).
- **Test names read as sentences** describing the behaviour under test.
- **A test asserted against one rendered payload only covers that
  fixture.** Assert against the source when a rule must hold everywhere.
- **Comments explain why, not what.** This codebase's comments narrate the
  bug or incident the code exists to prevent; match that register.
- **A module that calls `get_conn()` with no `db_path`** defines a
  call-time `get_conn` that resolves through `models` (the bound-import
  hazard in `CLAUDE.md`).

### UI and UX

`DESIGN_SYSTEM.md` is the source of truth. A few rules people trip on:
- **Fonts.** Space Grotesk for every number, Clash Display for headlines,
  Apfel Grotezk for chrome.
- **Colours** are CSS variables per theme (`scripts/check_colors.py`).
- **Buttons** carry `.cbtn` (`tests/test_button_system.py`).
- **Loading.** Use the sliding orange pulse bar, never "..." or a spinner.
- **Motion.** Fifteen approved animations (`CavnarMotion.swift`; `cm-` on
  the web), with an ember-only accent and no bounce.
- **Text size.** One scale across modules; the Account scale is the
  reference.
- **Spacing that wraps.** Check a header fix against text wrapping. Prefer
  `word-spacing` to a margin for space between words, because a margin
  indents a wrapped line.
- **Inline styles.** An inline `style` beats any stylesheet rule short of
  `!important`. Edit the inline style rather than adding a competing rule.

### Security model

The full list is in `docs/ops/SECURITY.md`.

- **Web** uses a session cookie (`sessions`), `login_required` and CSRF.
- **iOS** uses a bearer token on the same `sessions` table, with
  `mobile_login_required`.
- **Second factor.** SMS or email codes plus backup codes.
  - A passkey sign-in counts as the second factor.
  - Admin and support logins have their own authenticator app.
- **Team access.** Several `users` rows per restaurant, with roles in
  `permissions.py`.
- **Admin** is a separate `admin_required` decorator.
  - Support reads the console but gets 403 on every write.
  - Internal logins have a 12-hour session and a request ceiling.
  - A sensitive action needs the password again within 15 minutes
    (`@recent_auth_required()`).
  - View-as lasts 2 hours, shows a banner and records every write.
- **Sends from a local backend.** Anything an admin action would send is
  refused where `scheduler.scheduling_allowed()` is false.
- **Inbound webhooks** are signature-verified and de-duplicated by event
  id. A missing secret fails closed.
- **Outbound calls to an address someone else chose** go through
  `net_safety`: resolved once, private addresses refused, no redirects.

### Performance expectations

- **Home and the briefings** are deterministic and cached. A model call on
  every load of a busy page is a design smell.
- **Ask Cavnar's opening** is arithmetic over computed data, not a model
  call.
- **Background jobs claim before they work** (`job_period_claims`, the
  scheduler lease), so a slow run and the next tick can't double-fire. One
  duplicated job once turned a bad fetch into 180 notifications.
  - Every job is one `jobs_registry.JOBS` entry and returns
    `{attempted, ok, failed, skipped, hit_bound}`.
  - Work over every restaurant is bounded and resumable (a cursor in
    `job_cursors`).
- **AI calls are budget- and rate-gated before they fire.**
- **Request-scoped caching uses Flask's `g`**, never a module global.

### Don't touch without a fresh audit

- **`shift_quality.py`'s three invariants:** no data → `None`; a critical
  dimension's floor caps the score; confidence is tracked apart from the
  score.
- **`pricing.py`'s `TIERS`** is the only place a price is written.
- **The `_m()` delegation chain.** Don't reimplement a `mobile_api.py`
  body in `client_api.py`.
- **`tool_specs(restaurant)`'s module filtering** and the
  read/action/write tool kinds in `ask_cavnar_tools.py`. Anything with an
  effect outside the building is `write` and goes through a proposal.
- **Webhook idempotency** (`stripe_events_seen`, `docusign_events_seen`).
  A billing write that fails raises; it is never answered 200.
- **`/health` is read-only:** no write, no send, no page.
- **A send is reported from its real result**, and no code writes a "sent"
  row for a send that didn't happen.
- **The job registry and the loop stay in step**
  (`tests/test_fix_d_jobs.py`). `tests/test_docs_controls.py` holds
  `docs/ops/` to the code: change a control and its sentence together.

---

## Where the documentation lives

| File | What it covers |
|---|---|
| `CLAUDE.md` | Standing rules, loaded into every agent session |
| `ARCHITECTURE_MANIFEST.md` | Folders, layers, allowed imports, naming — read before adding a file |
| `SYSTEM_ARCHITECTURE.md` | Processes, jobs, data flow, deployment |
| `MODULE_OVERVIEW.md` | Every module, its files, its design stance |
| `API_REFERENCE.md` | Routes, web and mobile |
| `DATABASE_SCHEMA.md` | Tables, invariants, retention, the backup scrub |
| `PROMPT_LIBRARY.md` | Every model call |
| `DESIGN_SYSTEM.md` | UI and email rules |
| `INTELLIGENCE_ENGINE.md` | Cross-restaurant learning and its privacy rules |
| `TESTING.md` | How tests are organised and when to run what |
| `ROADMAP.md` | Open work, recently shipped, deliberately not doing |
| `docs/ops/` | `SECURITY.md`, `RECOVERY.md`, `ENVIRONMENT.md`, `RAILWAY_SCHEDULER_SPLIT.md`, `PIN_PEPPER_RUNBOOK.md` |
| `docs/plans/` | Designs, each opening with its status (built or not yet) |
| `docs/audits/` | Audit registers whose finding IDs tests still cite |
| `docs/history/` | Superseded material kept for the record |
| `docs/clients/` | Per-client configuration (Simple EJ's checklists, DSR config) |
| `docs/integrations/` | Vendor API collections (RPOWER) |
| `docs/app-store-submission.md` | What to enter in App Store Connect |
| `ios/CavnarAI/README.md`, `scripts/README.md` | The iOS project; who runs each script |

A stale doc is worse than none, because it gets trusted without being
checked. When a task changes something a doc states as fact, update the doc
in the same commit. Don't hand-type figures that change, such as test
counts, route counts or column counts. Quote the command that produces them
(`scripts/repo_inventory.py`).
