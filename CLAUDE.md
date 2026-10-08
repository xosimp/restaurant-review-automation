# Cavnar AI — working instructions

Standing instructions for anyone (human or agent) working in this repo. This
file is loaded automatically at the start of every session.

## Where the architecture lives

These files are the authoritative reference. Read the relevant one before
exploring code:

| File | What it covers |
|---|---|
| `ARCHITECTURE_MANIFEST.md` | The map: folders, services and owners, layers and allowed imports, shared utilities, what stays separate, naming — read before adding a file |
| `README.md` | The way in: the business, running it locally, testing, deploying, how it is built and why (merged from `PROJECT_CONTEXT.md` and `CAVNAR_AI_ENGINEERING_GUIDE.md`, 9/30/26) |
| `MODULE_OVERVIEW.md` | Every module, its files, its design stance |
| `SYSTEM_ARCHITECTURE.md` | Processes, jobs, data flow |
| `DATABASE_SCHEMA.md` | Tables and their invariants |
| `API_REFERENCE.md` | Routes, web and mobile |
| `PROMPT_LIBRARY.md` | Every model call: which model, what input, what guard |
| `DESIGN_SYSTEM.md` | UI/UX: tokens, type, spacing, components, motion |
| `TESTING.md` | How the suite is organised |
| `ROADMAP.md` | Open work, recently shipped, deliberately not doing |
| `INTELLIGENCE_ENGINE.md` | The cross-restaurant learning layer and its privacy rules |
| `docs/ops/SECURITY.md`, `docs/ops/RECOVERY.md`, `docs/ops/ENVIRONMENT.md`, `docs/ops/RAILWAY_SCHEDULER_SPLIT.md`, `docs/ops/PIN_PEPPER_RUNBOOK.md` | Controls, the emergency runbook, every environment variable, the deploy shape, the PIN pepper |
| `docs/plans/` | Designs, each opening with its status. Built: the DSR engine, task sheets. Not yet built: Back Office, Postgres and more workers, the restaurant health row, the review-fetch queue, the nonce CSP |
| `docs/audits/` | Audit registers whose finding IDs tests still cite (the 9/22/26 edge-case audit: 39 strict `xfail`s) |
| `docs/history/` | Superseded material kept for the record; nothing in it is live |
| `docs/clients/`, `docs/integrations/`, `docs/app-store-submission.md` | Per-client config (Simple EJ's checklists and DSR config), vendor API collections (RPOWER), the App Store Connect entries |
| `ios/CavnarAI/README.md`, `scripts/README.md` | The iOS project's setup; who runs each script |

---

## CRITICAL SAFETY REQUIREMENT — code deletion is HIGH RISK

Before recommending the removal, consolidation, or replacement of any code,
service, model, component, API, utility, background job, scheduled task,
prompt, or database object, trace **all ten** of the following:

1. **Every known reference** — direct imports and calls.
2. **Indirect references** — re-exports, aliases, wrappers, base classes.
3. **Dynamic references** — `getattr`, `globals()`, string dispatch tables,
   `importlib`, anything keyed by a name built at runtime.
4. **Dependency injection usage** — registries, factories, provider maps.
5. **Scheduled and background jobs** — `scheduler.py`, cron, queues, threads.
6. **Feature flags** — module flags on `restaurants`, tier gating, env vars.
7. **Routing** — blueprints, URL rules, mobile twins of web routes.
8. **Notifications** — email, SMS, push, digests, alert types.
9. **Tests** — including tests that assert a thing is ABSENT.
10. **SwiftUI previews** — and anything else compiled but never called at
    runtime, which the compiler will still fail on.

**Only recommend removal when HIGH confidence exists that the code is truly
unused or fully superseded.**

If confidence is below High, **do not recommend deletion**. Mark the finding
instead as:

> Candidate for future cleanup after additional verification.

**Never recommend removing code based solely on the absence of obvious
references.** Favour false negatives over false positives: preserving
production stability matters more than maximising cleanup.

### Why this is stricter here than it looks

This codebase is unusually good at hiding a live reference from a grep:

- **Bound imports.** `from models import get_conn` at module top level is a
  reference that the name `models.get_conn` will never match. Most modules
  do this, and it has already caused silent wrong behaviour in tests: a
  patch of `models.get_conn` never reaches a bound copy. `client_api`,
  `mobile_api`, `drafter`, `social_routes` and `demo_seed` therefore define
  a module-level `get_conn` that resolves through `models` at call time —
  copy that pattern rather than the bare import when a module calls
  `get_conn()` with no `db_path`.
- **Lazy imports inside functions.** Most cross-module calls here are
  `import x` *inside* the function body, so a module-level grep misses them.
- **Registry-driven dispatch.** `ask_cavnar_tools.TOOLS`, `pos.PROVIDERS`,
  `ask_cavnar_tools._SETTABLE` and the `_do_*` handler lookups all reach code by
  string, not by symbol.
- **Web/mobile twins.** Almost every route exists twice (`client_api.py` and
  `mobile_api.py`); deleting one half leaves the other calling a shared body.
- **Scheduled-only callers.** A function whose only caller is a 5am job in
  `scheduler.py` looks unused from every screen in the product.
- **Tests that assert absence.** Some tests exist to prove a thing was
  deliberately removed and must stay removed — re-adding or renaming it
  breaks them in a way that reads like an unrelated failure.

### Checklist before proposing any deletion

Run these, and say in the finding what each turned up:

```bash
rg -n "\bNAME\b" --glob '!*.pyc'          # direct, including comments
rg -n "getattr|globals\(\)|importlib"     # dynamic dispatch near the call sites
rg -n "NAME" scheduler.py templates/ ios/ tests/
rg -n "NAME" --glob '*.md'                # documented as an entry point?
```

A deletion is only "verified" when the trace is written down alongside it.

---

## Other standing rules

- **Root-cause first.** Diagnose the real cause; never ship the fastest
  surface patch. Verify live before claiming something is fixed.
- **Push after every completed task.** Don't wait to be asked.
- **Restart the local backend** (`PORT=5050 python3 hosted_dashboard.py`)
  after any backend edit — the user tests against it from a real device over
  ngrok.
- **UI work starts from `DESIGN_SYSTEM.md`.** It is the source of truth for
  colour, type, spacing, cards, buttons, charts, forms, tables, empty states
  and motion, on web and iOS. Do not introduce a new UI pattern unless no
  existing one fits — and if none does, document the new one there in the
  same commit.
- **Dates read `9/21/26`.** M/D/YY, no leading zeros, everywhere an owner
  sees a date — `time_utils.mdy()` in Python, `|format_date` in Jinja,
  `mdy()` in web JS. `DESIGN_SYSTEM.md` → *Dates and times* is the rule;
  an ISO date in owner-facing text is a bug.
- **Email colours come from `emails.BRAND`**, enforced as a ratchet by
  `scripts/check_email_tokens.py`. Email is light-mode only, inline-styled,
  and governed by `DESIGN_SYSTEM.md` → Email. Every sender returns an
  `emails.SendResult` and passes `restaurant_id`; act on `.ok`, and never
  write a `log_email` row for a send that did not happen. One email has one
  sender — the welcome is `emails.send_welcome_with_set_password_link`
  whoever sends it.
- **Value delivered is only what was measured.** `value_delivered.py`
  returns four figures — `delivered` (from `outcomes.py`, before/after,
  caveated), `avoided` (cost avoidance at STATED rates, counted only for
  work that happened), `surfaced` (dollars the alerts carried) and
  `opportunity` (gaps against target) — and **they are never summed into
  each other**. An opportunity figure must never be rendered as delivered
  value: that was the bug, and it made the number rise as the restaurant
  got worse. Any new "value" component states its rate inline and counts
  distinct work, never rows or months-since-signup.
- **Inline JS is ES5 only**, enforced by `tests/test_frontend_rules.py`.
  Colours come from CSS variables only, enforced by `scripts/check_colors.py`.
  Every web button carries `.cbtn`, enforced by `tests/test_button_system.py`.
- **New `restaurants` column = 4 touch points**: the `Restaurant` dataclass,
  a migration entry (either `init_db()`'s ALTER list or `ensure_columns()`'s
  tuple list — both run at boot; pick one), `update_restaurant()`'s `allowed`
  whitelist, and `get_restaurant()`'s hydration. Miss the whitelist and writes
  silently no-op — `tests/test_models.py` now asserts every `al_*`/`alert_*`
  dataclass field is whitelisted.
- **An admin action audits itself through one typed call**,
  `admin_events.record_admin_action` (actor, target, before/after, result) —
  never `admin_events.record("admin", …)` from a route
  (`tests/test_fix_int2_admin.py` holds `admin_routes.py` to it). A sensitive
  one takes `@recent_auth_required()` directly under `@admin_required`, or
  `auth.reauth_refusal(current_user)` for the part of a route that needs it;
  `docs/ops/SECURITY.md` lists them and `tests/test_docs_controls.py` holds
  the list. Admin work that calls a provider or a model runs on the one admin
  job pool (`admin_routes._submit_admin_job`) or `ops.run_admin_task`, never
  a thread per click; a thread a request starts is `ai_utils.attributed(fn)`,
  so its model calls stay the requester's.
- **Targeted tests by default — and before a push too.** The full suite
  (several minutes even in parallel) is NOT a pre-push step. Run it only after an audit or fix
  round, a big code change, a large batch of changes, or when asked — never
  after a few tweaks, never twice "to be sure" (Will, 9/15/26 and again
  9/28/26: "it stalls productivity"). When it does run, in parallel:
  `python3 -m pytest -q -p no:warnings -n auto --dist loadfile`. One suite
  at a time: two at once halve each other.
- **`get_restaurant()` is memoised per Flask request**, and only per request —
  outside one (scheduler, tests, scripts) it is uncached, deliberately, so a
  long-running job sees rows change under it. Any new write path to
  `restaurants` outside `update_restaurant` must call
  `models._invalidate_request_cache(rid)`.
- **Never put schema DDL on a request or per-call path.** Every table is
  created at boot — by `init_db()` (which also calls `ops.init_ops`,
  `billing_jobs.init_billing`, `admin_events.init_admin_events`,
  `offboarding.init_offboarding`, `admin_ops.init_admin_ops` and
  `ai_utils.init_ai_ops`) or by one of the `init_*` functions
  `hosted_dashboard.py` calls right after it (`auth`, `push`, `webhooks`,
  `guest_marketing`, `sales_audits`, `platform_monitor.init_platform_tables`,
  `provider_health.init_provider_health`). `ai_utils._ensure_usage_schema` is
  the pattern where a lazy table is unavoidable: once per database per
  process, with a self-healing retry if a write later fails on a missing
  column.
- **The scheduler only runs on Railway** (`scheduler.scheduling_allowed()`,
  also false while `RESTORE_FROM` is set): a local backend has its own SQLite
  file and lease but production's Resend and Twilio keys, so a local
  scheduler re-sends real briefs, digests, alerts and issue texts. The same
  gate refuses every admin action that emails, texts, or changes Stripe or
  DocuSign (`admin_routes._send_blocked`, `_live_actions_refused`,
  `LOCAL_SENDS_REFUSED`, `billing_jobs._sending_allowed`); a send that is only
  a side effect of an admin action is skipped with a note instead, and a new
  admin send checks the gate too. Deliberately NOT gated: the admin's own 2FA
  code (`/admin/two-factor/send`), the owner and login flows in
  `auth_routes`, and `/api/send-referral` — a local backend with the
  production `.env` still sends those. `ALLOW_LOCAL_SCHEDULER=1` overrides
  both deliberately.
- **Never run a copy of the app from a worktree with Flask's own `.env`
  loading.** `hosted_dashboard.py` loads the `.env` beside it and runs
  `app.run(..., load_dotenv=False)`, because Flask's lookup walks up from the
  working directory — a copy under `.claude/worktrees/` loaded the main
  checkout's `.env` (production's keys). A throwaway server for a UI check
  runs with no provider keys in its environment.
- **Deployment shape.** The scheduler runs in the web process by default.
  **Never run it as a separate Railway service**: volumes cannot be shared
  between services, so it would schedule against an empty database while the
  real jobs stopped. `worker.py` is only valid as a second process in the same
  service — see `docs/ops/RAILWAY_SCHEDULER_SPLIT.md`. The single-runner guarantee is
  `ops.acquire_scheduler_lease()`, and the lease lives in the SQLite file:
  the lease keeper renews it every minute, a kept lease silent for 4 minutes
  is taken over, and the web process's supervisor restarts a dead scheduler
  thread together with its keeper (`scheduler._run_scheduler_thread`). With a
  volume attached Railway runs one deployment at a time: a boot that fails,
  or a `/health` that is not 200 within the 120-second healthcheck, is
  downtime until a rollback — `/health` answers 200 for a degraded platform
  on purpose and must stay read-only.
- **Do not raise gunicorn `--workers` past 1** until the process-local
  state that still multiplies with workers is moved into the database:
  `security._admin_hits` (the admin request ceiling), `ai_utils._breakers`
  (per-process AI breakers), the console's fleet memo
  (`admin_ops._fleet_state`) and the bounded pools (`ASK_MAX_CONCURRENT`,
  the two admin pools — `ops.run_admin_task` and
  `admin_routes._submit_admin_job` — and the webhook and push delivery
  pools), the schedule-generation watchers (`schedule_engine._GEN_WATCHERS`:
  a Generate press remembered on one worker pushes nobody when the job
  lands on another) and the schedule Studio's kept inputs
  (`schedule_engine._studio_cache` / `_studio_live` — a cache, safe to keep
  per worker); the inventory and order are in
  `docs/plans/POSTGRES_AND_WORKERS_PLAN.md`. Each worker gets its own copy,
  so two workers silently double every limit. (Already durable: the login
  limiter — `security.login_throttled` on `login_attempts`,
  `auth_routes._login_attempts` only its fail-closed fallback; the
  supplier-email cooldown — `ops.claim_cooldown` rows,
  `client_api._order_send_last` is gone; the AI rate limit —
  `ai_rate_events`, `ai_utils._ai_call_log` only its fallback.) Railway
  usage (Sep 2026) showed ~19 vCPU-minutes of CPU for the whole billing
  period, so there is no load reason to do it yet.
- **Every outbound HTTP call names a timeout**, enforced by
  `scripts/check_timeouts.py`. With `--workers 1 --threads 4`, one call
  waiting on the OS's TCP behaviour is a quarter of the platform.
- **Dependencies are locked.** Edit `requirements.in` (direct dependencies,
  pinned exactly), then re-lock `requirements.txt` with `pip-compile` under
  Python 3.12 (`.python-version` pins 3.12.7). Railway and CI install the
  lock (CI with `--no-deps`, then `pip check`). Never edit a version in
  `requirements.txt` by hand.
- **Work that iterates every restaurant must be bounded and resumable.**
  `run_daily_fetch` is the pattern: a worker pool, a wall-clock bound, and a
  cursor in `job_cursors` so the next pass starts where the last one
  stopped. A time bound without a cursor is worse than no bound — it starves
  the same tail every pass.
- **A scheduled job is one `jobs_registry.JOBS` entry plus one
  `ops.run_job("<name>", fn)` call in `scheduler_loop`**, returning
  `{attempted, ok, failed, skipped, hit_bound}` or raising
  (`tests/test_fix_d_jobs.py` keeps the two in step; `ops.run_outcome` reads
  any other result as partial, never clean). A growing ledger gets an entry
  in `ops._RETENTION_DAYS` / `_RETENTION_COLUMN`; a finding about a model's
  output is `ai_utils.record_quality_event`, not `ops.capture`.
- **Recovery lives in `docs/ops/RECOVERY.md`.** The local backup snapshot is
  deliberately NOT redacted (it never leaves the volume and is the restore
  artifact); only the off-site copies are — the object-storage and emailed
  copies are scrubbed through `offsite_backup`'s one registry and
  Fernet-encrypted, and the backup fails when no off-site copy was made. Do
  not reintroduce redaction on the local path. `tests/test_docs_controls.py`
  holds `docs/ops/SECURITY.md`, `RECOVERY.md` and `RAILWAY_SCHEDULER_SPLIT.md`
  (and `DATABASE_SCHEMA.md`'s scrub list and retention table) to the code:
  change a control and its sentence together.
- **Static assets have no cache-busting** (`/static/cavnar-orb.js`, bare
  path). Do not add a far-future `max-age` until they are hashed or
  versioned, or a JS fix will be stranded in browser caches.
