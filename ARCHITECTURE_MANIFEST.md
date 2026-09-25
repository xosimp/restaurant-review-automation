# Architecture Manifest — Cavnar AI

The map of the repository: what each folder is for, which module owns
what, which modules may import which, where shared code belongs, what is
separate on purpose, and how things are named. It is the document to read
before adding a file, and the one to update in the same commit as any
change it describes. `tests/test_architecture_manifest.py` checks the parts
of it that can be checked: every module is listed, every listed layer is
respected at module scope, every top-level folder is accounted for.

For depth it points elsewhere rather than repeating: how the system runs
(`SYSTEM_ARCHITECTURE.md`), what each module does in detail
(`MODULE_OVERVIEW.md`), tables (`DATABASE_SCHEMA.md`), routes
(`API_REFERENCE.md`), model calls (`PROMPT_LIBRARY.md`), UI
(`DESIGN_SYSTEM.md`), verification (`TESTING.md`), the standing rules
(`CLAUDE.md`).

---

## 1. Top-level folders

| Path | Purpose | Owner |
|---|---|---|
| `/` (root `*.py`) | The Flask backend: one flat package of ~115 modules, layered by import rule (§3). Flat on purpose — every module name is unique and greppable, and the bound-import style makes packages costly. | backend |
| `dsr/` | The nightly End-of-Day Closeout & Daily Sales Report (`docs/plans/DSR_ENGINE_PLAN.md`): `__init__` is the contract (block statuses, stages, block names, the never-fabricate rule), `store` the reports/metrics/budgets/category map, `fiscal` the restaurant's own period calendar, `block_<name>` one collector per block (`collect(ctx) -> block`, no model, never raises for missing data), `common` what the collectors share, `narrative` the one verified AI call, `access` who sees which view, `rollup` the week and period, `memory` the compact reads Ask and the morning brief carry, `history_import` Last Year from old workbooks, `xlsx` the grid's Excel export, `deliver` the email and push that say it is ready. The web screen is `panel-dsr` in `dashboard.html` (`#dsr/YYYY-MM-DD`). One fact snapshot per night; every view renders from it. | backend |
| `intelligence/` | A Python package: the cross-restaurant learning layer. Its `__init__` is the facade the rest of the app imports; submodules import each other relatively and never import the app. | backend, `INTELLIGENCE_ENGINE.md` |
| `templates/` | Jinja templates. `dashboard.html` is the entire client web app (markup + inline CSS + inline ES5 JS); the rest are standalone pages (auth, staff portal, admin, status, audits) and two partials (`_csrf_fetch.html`, `_review_card.html`). | web |
| `static/` | Assets Flask serves at `/static/*`: fonts, brand images, `css/cavnar-buttons.css`, `cavnar-orb.js`, `cavnar-field.js`. No cache-busting — do not add far-future caching until assets are hashed. Everything here is a public URL. | web |
| `public/` | The marketing site (`cavnar.ai`), served by a Cloudflare Worker from this folder only (`wrangler.jsonc`), hand-deployed by Will. Its `static/fonts` is a deliberate byte-copy of `static/fonts` (test-pinned). | site |
| `ios/CavnarAI/` | The SwiftUI app. `project.yml` → `./generate.sh` → gitignored `.xcodeproj`. `CavnarAI/Features/<Module>/` mirrors the product modules; `DesignSystem/` is the one place for tokens, type, motion and shared components. | iOS |
| `tests/` | pytest, flat, one file per concern; names read as sentences. ~140 sites read source files or templates as text — see `TESTING.md` before moving anything they name. | backend |
| `scripts/` | Lints run by CI or by a test (`check_*.py`), runbook tools, one-off builds and seeds. `scripts/README.md` says who runs each. Never imported by the app. | ops |
| `docs/` | `ops/` (RECOVERY, SECURITY, RAILWAY_SCHEDULER_SPLIT, PIN pepper runbook), `plans/` (designs with no code yet), `history/` (superseded material, kept for the record; nothing in it is live), `contracts/`, `integrations/`, `clients/` (a client's own content — e.g. Simple EJ's checklists — kept as source files and loaded onto their account by a seed; never read by the app), `app-store-submission.md`. | ops / planning |
| `brand/` | Brand sources (`assets/`: seal and wordmark SVG/JSON and the build scripts that emit `static/brand/`), the social upload kit (`social/`), the print one-sheet (`print/`). Scratch canvases live in `docs/history/brand-scratch/`. | brand |
| `.github/` | CI: compile every module, colour and silent-handler lints, pytest. No iOS build in CI. | ops |
| `.claude/` | `launch.json` (Browser-pane dev servers) and vendored third-party UI skills. Not part of the product. | tooling |

Root files that are not modules: the ten reference docs and this manifest; `Procfile` (not what Railway runs — `railway.json` is); `wrangler.jsonc`; `requirements*.txt`; `.env.example`; `og-image-v2.png` (the live share image, served by the app); `sms_optin_preview.html` (served by `admin_routes` as the A2P opt-in evidence page); `sample_*.csv` (embedded copies are what the loaders actually parse); `dashboard_template.html` (used only by the standalone `dashboard.py` demo — see §5).

---

## 2. Core services and who owns what

Each service is a set of modules with one owner-module that other code is meant to enter through. "Owns" means: that module holds the invariants, and a change to the service's behaviour starts there.

| Service | Owner module | Also in the service | Entered through |
|---|---|---|---|
| **Data layer** | `models.py` (schema at boot, the `Restaurant` dataclass, most reads/writes) | `auth.py` (users, sessions, staff portal tables), `credentials.py` (Fernet at rest), `ops.py` (job ledgers, lease, claims), `db_restore.py` (the `RESTORE_FROM` boot-time restore, see `docs/ops/RECOVERY.md`) | `models.get_conn`, `get_restaurant`, `update_restaurant` |
| **AI** | `ai_utils.py` (the only `messages.create`; `MODELS`, `model_for`, `get_client`, budget, breaker, usage) | `ai_guard.py` (figure verification, claim kinds, untrusted-text wrapping) | `create_with_retry(get_client(), model=model_for(...))` |
| **Reviews** | `analyser.py` (scoring) / `drafter.py` (replies) | `fetcher.py`, `gmb.py`, `review_intelligence.py`, `reporter.py` (the digest), `response_templates` in `models` | routes in `client_api` / `mobile_api`; jobs in `scheduler` |
| **Labor** | `labor.py` (shift ingestion, labor %, schedule generation glue) | `shift_quality.py` (pure scoring engine — no I/O, ever), `covers.py`, `time_off.py`, `labor_replacements.py`, `staff_roster.py`, `staff_schedule.py`, `demand.py`, `preshift.py`, `schedule_engine.py` (the deterministic pipeline: inputs, row repair, rule backstops, quality signals, the async job), `schedule_rules.py`, `schedule_requirements.py` (pure: the per-shift requirements and people facts the generation prompt carries), `staffing_curve.py` (pure: half-hour needs from the sales curve and the section cap every requirement is held under), `schedule_learning.py`, `rec_ledger.py` (recommendation identity and acceptance trail), `schedule_optimizer.py` (the Shift Quality score as the objective: bounded legal repair moves, each explained), `schedule_solver.py` (who works each drafted shift, solved over the hard rules and judged by the score), `schedule_experiments.py` (live A/B arms of generation, measured on acceptance and outcomes; admin-only readout), `staff_settings.py`, `demand_signals.py`, `schedule_versions.py`, `shift_requests.py`, `schedule_economics.py`, `schedule_intel.py`, `compliance_packs.py`, `reservation_feeds.py` | routes; `strategy_routes` twins; `strategy_jobs` |
| **Food Cost** | `inventory.py` (waste/overstock/orders) | `inventory_ledger.py` (stock ledger, recipes, margins), `cogs.py`, `waste_trend.py`, `food_cost_intelligence.py` (the CFO layer), `recipes.py`, `invoices.py`, `ordering.py`, `menu_intelligence.py` | routes; `strategy_routes` twins; 5am/6am jobs |
| **Marketing** | `marketing.py` (drafting, content log) | `marketing_drafts.py`, `marketing_publish.py`, `marketing_media.py`, `marketing_links.py`, `marketing_signals.py` (attribution), `marketing_tags.py`, `guest_marketing.py` (text club), `guest_email.py`, `guest_links.py`, `meta_api.py`, `social_routes.py` (Instagram/Facebook OAuth + publish) | routes; `run_due_posts` every tick |
| **Intel** | `competitor.py` | `competitor_intel_format.py`, `weather.py`, the AI-visibility engine currently inside `client_api.py` (`_do_ai_visibility_inner`, Perplexity over REST) | routes; Monday jobs |
| **Ask Cavnar** | `ask_cavnar.py` (context, `ask_with_tools`) | `ask_cavnar_tools.py` (the `TOOLS` registry, kinds read/action/write), `business_intelligence.py` (cross-module), `home_brief.py` (the opening) | routes (SSE stream + non-stream, both surfaces) |
| **The owner's day** | `home_brief.py` (Home) / `morning_brief.py` (the brief) | `action_queue.py`, `command_center.py` (⌘K / the iOS command sheet), `nav.py`, `closeout.py`, `intraday.py`, `first_look.py`, `good_news.py`, `milestones.py`, `decisions.py`, `delayed.py`, `weekly_review.py`, `monthly_review.py`, `review_common.py`, `value_delivered.py`, `promise.py`, `activity.py` | routes; `strategy_routes`; tick jobs |
| **Strategic foundations** | `metrics.py` (the one registry of measurable things) | `outcomes.py`, `goals.py`, `issues.py`, `loss_detection.py`, `strategy_routes.py` (HTTP, both surfaces at once), `strategy_jobs.py` (scheduled half) | `strategy_routes._ROUTES` |
| **Notifications** | `notify.py` (the firing decision: gates, holds, batch, `deliver_alert`) | `push.py` (APNs), `emails.py` (every email; `BRAND`, `sender`, `deliver`), `webhooks.py` + `webhook_routes.py` (outbound deliveries; inbound Stripe/DocuSign/Resend/Twilio) | `notify.raise_alert`, `emails.send_*`, `push.fire_push` |
| **Scheduling (background jobs)** | `scheduler.py` (`scheduler_loop`, the registry in `SYSTEM_ARCHITECTURE.md`) | `strategy_jobs.py`, `intelligence/jobs.py`, `ops.py` (lease, claims, `run_job`), `worker.py` (optional second process) | boot in `hosted_dashboard`; `admin_ops.RUNNABLE_JOBS` for "run now" |
| **Intelligence engine** | `intelligence/__init__.py` facade | `features`, `memory`, `feedback`, `scoring`, `patterns`, `benchmarks`, `trends`, `confidence`, `dashboard`, `jobs`, `privacy`, `categories`, `stats`, `engine`, `metrics_registry`, `dna` (Restaurant DNA), `predict` (neighbour predictions, dormant), `comparison_cache` (materialised comparisons) | `intelligence.<facade function>` only |
| **POS / platform integrations** | `pos.py` (the provider registry and capability dispatch) | `toast.py`, `square.py`, `clover.py`, `rpower.py` (+ each `*_routes.py` for connect/sync), `gmb.py`, `weather.py` | `pos.PROVIDERS`, `pos.supports(rid, cap)`, `pos.fetch_*` |
| **Auth & security** | `auth.py` (sessions, decorators, staff portal identity) | `security.py` (durable throttling, breached-password check, freeze), `permissions.py` (roles, module view gates), `csrf.py`, `security_headers.py`, `guest_links.py`, `credentials.py`, `provisioning.py` (account from a signed contract) | decorators `login_required` / `mobile_login_required` / `admin_required` / `staff_login_required` |
| **HTTP surfaces** | `hosted_dashboard.py` (assembly only: blueprints, boot, error pages, `/`, `/health`) | `client_api.py` (web), `mobile_api.py` (iOS), `strategy_routes.py` (both), `admin_routes.py` + `admin_ops.py` + `admin_events.py`, `auth_routes.py`, `staff_routes.py`, `status_routes.py` + `status_manager.py`, `sales_audit_routes.py` (+ `sales_audit_*.py`, `sales_audits.py`), `http_layer.py` (gzip, cache headers, metrics) | see `API_REFERENCE.md` |
| **Billing & contracts** | `pricing.py` (the one price list) | `docusign_helper.py`, `emails.create_stripe_checkout`, `webhook_routes` (inbound), `provisioning.py` | — |
| **Demo accounts** | `demo_seed.py` | wrappers in `models` for the boot block and tests | `demo_seed.start_background_seed()` once at boot; admin reseed route |
| **Configuration** | `config.py` (env values read in more than one module) | `time_utils.py` (zones and stamps), `pricing.py` | `config.base_url()` etc.; a value read in one module stays in that module |

---

## 3. Layers and allowed dependencies

Modules sit in one of five layers. **At module scope, a module may import only its own layer or a lower one.** Inside a function body a module may import upward (that is what the lazy-import style is for: breaking cycles and keeping optional dependencies off the import path), but it should be rare and deliberate. `tests/test_architecture_manifest.py` enforces the module-scope rule against the table in §4, with the exceptions listed here.

| Layer | What lives there | May import at module scope |
|---|---|---|
| **L0 foundation** | pure helpers and constants with no data access: `config`, `time_utils`, `pricing`, `permissions`, `csrf`, `security_headers`, `http_layer`, `shift_quality`, `schedule_requirements`, `staffing_curve`, `thresholds`, `ai_guard`, `competitor_intel_format`, `review_common`, `reply_edits`, `pos`, `credentials`, `sales_audit_schema`, `intelligence.stats`, `intelligence.privacy`, `intelligence.categories`, `confidence_engine`, `response_validation`, `benchmark_registry` | stdlib, third-party, L0 |
| **L1 data** | `models`, `auth`, `ops`, `ai_utils`, `security`, `guest_links` | L0, L1 |
| **L2 domain** | every module that computes something for one restaurant (the services in §2), the `intelligence/` package, `emails`, `notify`, `push`, `webhooks`, `admin_ops`, `admin_events`, `status_manager`, `sales_audit_engine/cheatsheet/notes_ai`, `sales_audits` | L0, L1, L2 |
| **L3 HTTP** | `client_api`, `mobile_api`, `strategy_routes`, every `*_routes.py` | L0–L3 |
| **L4 processes** | `hosted_dashboard`, `scheduler`, `strategy_jobs`, `worker`, `demo_seed`; the standalone `main`, `dashboard`, `audit_app` | anything |

Rules that follow from the layers:

- **`models` imports nothing above L1 at module scope.** It reaches `auth`, `labor`, `inventory_ledger`, `demo_seed` only inside functions. Any new write path to `restaurants` outside `update_restaurant` calls `models._invalidate_request_cache`.
- **Route modules never hold business logic that a job also needs.** If the scheduler needs it, it belongs in L2. (One known exception is being worked off: the AI-visibility engine still lives in `client_api.py`; `strategy_jobs` and `scheduler` reach into `client_api` lazily for it. The schedule engine moved to `schedule_engine.py` on 9/22/26; `client_api` re-exports its names.)
- **`client_api` may import `mobile_api` lazily (`_m()`), never at module scope**; `mobile_api` imports `client_api` at module scope as `_capi` for the shared private bodies. That pair is the one intended module-scope edge between the two.
- **The `intelligence` package never imports the app** except `models` (`DB_PATH`, `get_conn`) and `home_brief`'s payload shapes; the app enters through the facade in `intelligence/__init__.py`.
- **`ops` is L1 but imports `emails` at module scope** (for the failure digest). Known exception; do not add a second.
- **`pos` is L0 and reaches its providers lazily by name** (`PROVIDERS` registry). A new provider adds one registry line and implements `PROVIDER_API`; nothing else changes.
- **Third-party SDKs are wrapped once**: Anthropic in `ai_utils`, Resend in `emails.deliver`, Twilio in `notify.send_sms`, APNs in `push`, Stripe in `emails`/`webhook_routes`, DocuSign over REST in `docusign_helper`. A module that needs one calls the wrapper.

Known module-scope exceptions the test tolerates (each named here so a new one is a deliberate act): `ops → emails`; `mobile_api → client_api`; `admin_routes → admin_ops, admin_events` (same layer, listed for clarity); `sales_audit_cheatsheet → sales_audit_engine` (both L2).

---

## 4. Module table

Every root module, its layer and its one-line job. The test fails when a module is missing from this table or when its module-scope imports reach a higher layer than the one listed. (Layers: 0 foundation, 1 data, 2 domain, 3 http, 4 process.)

| Module | Layer | Job |
|---|---|---|
| `action_queue` | 2 | everything still open for the owner, with what finishes it |
| `activity` | 2 | the Account activity trail and its `build()` |
| `admin_events` | 2 | the admin console's event ledger |
| `admin_ops` | 2 | the admin console's data layer and `RUNNABLE_JOBS` |
| `admin_routes` | 3 | `/admin/*` |
| `ai_guard` | 0 | figure verification, claim kinds, untrusted-text wrapping |
| `ai_utils` | 1 | the one Anthropic call path, `MODELS`, `get_client`, budget, breaker, usage ledger |
| `analyser` | 2 | review scoring (sentiment, severity, entities) |
| `ask_cavnar` | 2 | Ask Cavnar context and `ask_with_tools` |
| `ask_cavnar_tools` | 2 | the `TOOLS` registry (read / action / write) |
| `audit_app` | 4 | standalone digital-audit scorecard app (port 9000); not part of the web process |
| `auth` | 1 | users, sessions, decorators, staff portal identity, 2FA |
| `auth_routes` | 3 | login, reset, 2FA, account-security `/api/*` |
| `benchmark_registry` | 0 | every outside benchmark the product may state, keyed by metric × restaurant type → {low, high, mid, median, source, short, source_kind (published / rule_of_thumb / vendor), year, data_year, applies_to, service_models, definition (None = unstated = not comparable), max_age_years, note}; seeded from `sales_audit_engine.BENCHMARKS`. `lookup(…, definition, service_model)` is the one lookup the engine, target seeding, the ledger and the sales audit share. No entry for a type → no industry figure on any surface; an all-restaurant ("general") entry only where the source is about every restaurant; `published_only` for dollar comparisons; `ABSENT` names what has no source (the 4–5% waste target, the blended wage); `for_restaurant` carries the type's `inferred` flag (a function-scope import of `intelligence.categories`); `line`/`cite` are the one wording; `facts`/`cohort_facts` are the kind-'benchmark' facts `response_validation` reads |
| `benchmark_views` | 2 | the owner screens onto the Benchmark Engine (`intelligence.engine`), shaping only, never a comparison of its own: `card(user, module)` — the "How you compare" card (who, how many, as-of, the comparison-strength % with its four Why? rows, the standing per metric, one Ask action per metric the restaurant is behind on, and the below-the-minimum state that leads with the restaurant's own previous 13 weeks); `module="home"` is Home's strip; `location_compare(user)` — the owner's locations on the engine's `location` kind, each read against its own normal first, a gap called only beyond both locations' own swing; `rank_by_rating(entries)` — the one group strongest/weakest rule (the rating floor, a gap beyond 2 standard errors) home_brief and reporter both read. Routes `/api/benchmarks/card`, `/api/benchmarks/locations` + mobile twins |
| `business_intelligence` | 2 | the cross-module read ("why did profits drop") |
| `client_api` | 3 | the web dashboard's `/api/*` and page routes; the `_do_*` bodies both surfaces share |
| `closeout` | 2 | the manager's handoff: four quick lines, plus the six DSR fields (equipment, VIP guests, maintenance, shift notes, general notes, influence) the nightly report reads verbatim |
| `clover` / `clover_routes` | 2 / 3 | Clover provider / its connect and sync routes |
| `cogs` | 2 | actual food cost % |
| `command_center` | 2 | the Command Center's server half (Friction audit 9/25/26): `registry(user)` — the places, confirmable actions (Ask's write tools through `tool_allowed` + the route's own permission) and Ask a login may use here; `search(user, q)` — one ranked, permission-filtered search (places, locations, reviews, people, issues, schedules, report nights, the caller's own chats), no model; `propose()` — Ask's confirm card from `build_proposal`, logged `proposed`; `reopen()` — a stored proposal rebuilt without the model. Routes in `strategy_routes` (web + mobile twins) |
| `competitor` | 2 | competitor snapshots and the weekly read |
| `competitor_intel_format` | 0 | rendering the competitor read for every surface |
| `confidence_engine` | 0 | the Recommendation Confidence arithmetic, pure (contract K1): Evidence Strength (n ÷ `N_FULL[kind]` × coverage × quality, capped by partial data, unverified figures and a model's own band — which only lowers it), Historical Accuracy (a `kind_record` shrunk toward even, only at `MIN_MEASURED` own or the cohort floor), Data Freshness (the minimum over sources), the overall % (geometric mean; ≤70 with no track record, ≤49 on stale data), the band words derived from it, the K3 snapshot fields, and the admin calibration math (reliability by decile, Brier) |
| `config` | 0 | shared environment values |
| `covers` | 2 | covers per day |
| `credentials` | 0 | Fernet at rest for POS/OAuth columns |
| `csrf` | 0 | CSRF cookie and check |
| `dashboard` | 4 | pre-hosted standalone demo app (port 8080); 0 importers; candidate for `docs/history` |
| `data_freshness` | 2 | how current and complete each data source is (contract K2): one `SOURCES` threshold table and `source_state(restaurant, key)` → `{pct, as_of, as_of_iso, basis, state, error}` for POS (every provider through `pos_health`), shifts and sales (dated by the last day the data covers), reviews (`fetched_at_ct`, missed fetch slots, Places sampled), counts (the oldest `last_recount_at`; the legacy inventory-CSV path is `unknown`, "uploaded M/D, count date unknown", never not connected), deliveries, the waste log (the newest waste event; `ingredients.rollup_at` is its note), prices (the newest applied invoice or priced delivery — read by the price-spike alert and price drivers, in no module's minimum), marketing (token expiry and `scheduler.metrics_sync_state` — a failing or missed nightly metrics sync is an error on the source; a deliberate function-scope L2→L4 import), AI visibility, competitors, weather (`age_hours` and `stale` past `weather.FORECAST_STALE_HOURS`), the DSR (final reports; a provisional night held under aging); an unknown age is never current, a future date is unknown, an erroring source is under `ERROR_CEILING` (below the engine's stale threshold); also `sources_for_tools` (Ask), `review_evidence_flags` (the Places "sampled" flag every review surface reads) and `review_fetch_state` (the reviews recency Home's nudge and portfolio read) |
| `decisions` | 2 | the owner's decision record |
| `delayed` | 2 | actions with an undo window |
| `demand` | 2 | demand forecast from same-weekday medians |
| `demo_seed` | 4 | the Gia Mia / Simple EJ's demo accounts |
| `docusign_helper` | 2 | DocuSign over REST |
| `drafter` | 2 | review reply drafting |
| `emails` | 2 | every email: `BRAND`, `sender`, `deliver`, templates |
| `fetcher` | 2 | review pull (Google Places / CSV) |
| `first_look` | 2 | the first-week read |
| `food_cost_intelligence` | 2 | the CFO layer: drivers, diagnosis, projection |
| `forecast_log` | 2 | every stated forecast frozen once per period, scored only after the period closes, and its accuracy by kind (waste, prime cost, weekly revenue, labor, marketing reach, review rating) |
| `gmb` | 2 | Google Business Profile OAuth and posting |
| `goals` | 2 | one active target per metric |
| `good_news` | 2 | wins the brief and emails draw on |
| `guest_email` | 2 | newsletter and email validation |
| `guest_links` | 1 | signed public tokens |
| `guest_marketing` | 2 | the text club: contacts, campaigns, opt-in, attribution |
| `home_brief` | 2 | the deterministic Home payload |
| `hosted_dashboard` | 4 | app assembly, boot, `/`, `/health`, error pages |
| `http_layer` | 0 | gzip, cache headers, request metrics |
| `intraday` | 2 | in-service capture, the pre-dinner pulse, the closing summary |
| `inventory` | 2 | Food Cost: waste, overstock, reorder, supplier orders |
| `inventory_ledger` | 2 | the stock ledger, recipes, margins, depletion |
| `invoices` | 2 | invoice transcription (model) and the Python proposal/apply |
| `issues` | 2 | issues, assignment, escalation, tokenised links |
| `labor` | 2 | shift ingestion, labor %, schedule generation glue |
| `schedule_engine` | 2 | the deterministic schedule pipeline: inputs, row repair, backstops, quality signals, the async generation job |
| `schedule_rules` | 2 | the rules a schedule is checked against: compliance settings, role floors, the Constraints set, the violation sweep |
| `staff_settings` | 2 | the one roster (history ∪ hand-added − deactivated) and per-person facts: hours limits, daypart windows, minors, pairings, reliability |
| `demand_signals` | 2 | owner-entered events and reservation counts for specific dates |
| `schedule_versions` | 2 | every saved state of a schedule, diffs between them, the edits a manager keeps making |
| `shift_requests` | 2 | staff drop and swap requests, the manager's answer, open shifts and claims |
| `schedule_economics` | 2 | the money side of a week, deterministically: trim to budget, overtime-priced cost, weekly revenue from the restaurant's own pattern, sales per labor hour by daypart, holiday lift, staggered starts, cost of an edit |
| `schedule_intel` | 2 | what the schedule learns from its own record: outcomes per published week, the rotation ledger, behaviour-learned preferences, mentoring, suggested pairs, the recommendation ledger, pattern dismissals, tenure memory |
| `schedule_learning` | 2 | what the schedule learns from the manager and from outcomes: retimes, headcount, role changes and leader swaps the manager keeps making, implicit recommendation acceptance, outcome-calibrated weight suggestions (never applied), no-shows by weekday, standby days, the overtime forecast |
| `schedule_optimizer` | 2 | the Shift Quality score as the schedule's objective: a bounded local search over legal add/retime/replace/swap/trim moves generated from the weakest dimensions, each re-checked against the rule sweep, the hours budget and the section cap, and each explained; what it cannot fix is said |
| `schedule_solver` | 2 | who works each of the draft's shifts, solved: backtracking with forward checking and branch-and-bound over the hard rules, time-limited, proved optimal where it can be; `score_rows` judges the final choice; shifts no legal week can staff are named and kept as drafted. Pure apart from time — no model, no database |
| `schedule_experiments` | 2 | live A/B arms of schedule generation (hash of restaurant and week, env/per-restaurant pins), the week's arm record, and the internal readout of acceptance and outcomes per arm with its minimum-sample rule |
| `insight_store` | 2 | one stored AI read per restaurant and prompt fingerprint (Reviews, Food Cost, Marketing), shared by web and iOS; the recommendation lines inside a read, keyed and presented through rec_ledger |
| `dsr.__init__` | 2 | the DSR contract: block statuses (ready / awaiting / unavailable / not_connected), stages, block names, `block()` which refuses non-numeric metrics and gives every non-ready block an owner-facing reason |
| `dsr.store` | 2 | DSR reports (one row per restaurant, business date and version; stage times as provenance), `dsr_metrics` (the searchable per-night history: `metric_series`, `find_days`), budgets, the POS-department → DSR-category map (unmapped is shown, never guessed) |
| `dsr.narrative` | 2 | the night's one AI call (`narrative.write(ctx, facts)` → `{ok, narrative, reason}`, never raises): refuses without a call unless sales and one more measured block are ready, fences the closeout and every other outside text, holds the answer to a strict JSON shape, drops any line whose figures don't trace to the facts it cites (a failing lead refuses the whole narrative), and ranks actions with `home_brief`'s urgency × dollars × ease rule, keyed `dsr_action:<kind>:<block>[/<entity>]` — presented to `rec_ledger` only where a reader is shown them (the view on `dsr`, a delivered email on `dsr_email`: `ledger_items`), never at generation |
| `dsr.scorecard` | 2 | the Owner DSR's "did we win today?" (9/25/26): the verdict, the overall score out of 100 (sales 40 · labor 25 · food 15 · guests 20, re-weighted over what was measured; sales plus one more or no score), the four components, Today's wins and Today's risks — deterministic from the night's blocks and `dsr_metrics` history, no model, an unmeasured component says why; the narrative's verified lines top the lists up. Owner view only (`access.render`) |
| `dsr.fiscal` | 0 | the restaurant's own fiscal calendar: week start day, Period/Week from a year start, 4x13 or 4-4-5; no year start means no period number, never a guessed one |
| `dsr.pipeline` | 2 | the nightly stage machine: `run_night` (never before close; POS closeday poll; block retries 10/20/40 until the local deadline; only Sales is required — provisional only without it, late sales make a new version, any other late block goes out labelled; the closeout never holds; failed after bounded retries; one claim per restaurant/night/version), `run_sweep` (the scheduler entry, bounded, cursor in `job_cursors`), `start_manual` (Close day). Reaches blocks and the narrative lazily by name |
| `dsr.block_sales` | 2 | the Sales block from `pos.fetch_day_sales`: gross/net/transactions, every comparison None without its baseline, the hourly curve, categories through the owner's map (unmapped listed), top/bottom items |
| `dsr.block_labor` | 2 | the Labor block from what the POS sync archived: labor $/%/hours, overtime, vs `notify.labor_target_for`, coverage issues, the published day's Shift Quality, deterministic observations |
| `dsr.access` | 2 | who sees which DSR: `view_for(user)` (owner / manager / none), redaction by the existing permission rules, narrative filtered by what it cites, the stage checklist |
| `dsr.deliver` | 2 | telling people: `on_terminal` (the pipeline's hook after a version is saved final/provisional — every owner login the Owner DSR, every manager login the Manager DSR, rendered only through `dsr.access.render`; email at once via `emails.send_dsr_email`, push held through quiet hours; one notice per night/person/channel plus one "Updated" after a provisional night goes final; claim-before-send in `dsr_deliveries`; only where `scheduler.scheduling_allowed`), `release_held` (the `dsr_delivery` job), `replaces_closing_summary` |
| `dsr.rollup` | 1 | a week and a fiscal period from the nightly reports (Erik's weekly grid): categories, gross/net, budget, last year (DSR → imported workbook → POS sync), labor, the day's notes; totals over measured days only; `access.redact_grid` drops the budget for a manager |
| `dsr.xlsx` | 0 | Erik's weekly grid as a real .xlsx with no dependency: a small SpreadsheetML writer over `zipfile` (inline strings; styles for bold, `$#,##0`, `0.0%`, a signed dollar variance; column widths, frozen header, merged title) and `week_workbook(grid, name)` in the grid's layout. Lays out only what the (already redacted) grid holds; an unmeasured cell is left empty, never 0 |
| `dsr.common` | 2 | what the collectors share: a business date's window in local and UTC time (a 1am review and a 05:10 UTC reply land on the night they belong to), `M/D/YY · h:mmpm` labels, and `guard` — one failed source becomes a named gap (ops.capture), never an exception |
| `dsr.block_food` | 2 | Food block: estimated food cost (recipes × the night's item sales, labelled an estimate, with coverage), waste logged that night, low / critical stock, recoverable (the deduplicated driver total), variance only where a count exists; `awaiting` while the night's item sales haven't synced |
| `dsr.block_reviews` | 2 | Reviews block: the night's reviews in the local window, rating, stored themes, urgent, drafts owed, replies posted; `awaiting` ("Review sync delayed") when the fetch has missed two slots, with the fetch-dependent counts None |
| `dsr.block_marketing` | 2 | Marketing block: posts published through Cavnar, engagement only where Meta measured it, campaigns and newsletters with clicks / matched visits only where attribution exists, and what's queued next |
| `dsr.block_intel` | 2 | Intel block: the NWS forecast for the night (always labelled a forecast), events and holidays ("Nothing listed"), competitor moves only on a night a check ran, covers vs a typical weekday only where counted |
| `dsr.block_closeout` | 2 | Closeout block: the manager's close-out verbatim, marked manager-written, with who filed it and when; "No manager closeout filed" is `unavailable`, not an error |
| `dsr.memory` | 2 | the reports as the rest of the product remembers them: `compact` (Ask's `read_dsr`), `context_block` (Ask's last-night section), `last_night` (the morning brief) — each a compact read of the stored snapshot through `dsr.access` for the login, never a recomputation |
| `dsr.history_import` | 2 | Last Year from the owner's old workbooks: `.xlsx` read with zipfile + xml.etree (no openpyxl), `.csv`; the template layout today, `LAYOUTS` for Erik's weekly grid when it arrives; a blank is skipped, never a zero |
| `rec_delivery` | 2 | a recommendation is shown when it is DELIVERED: builders `stage()` what they render and the sender flushes the stage to `rec_ledger` only after a successful send (`collect()`); `presentable()` / `answerable()` — which keys are recommendations an owner can answer (`money:*`, `urgent_reviews`, `intraday_pulse:*` are not); `link()` / `ask_url()` — dashboard links that carry `rec=` / `src=`, and `record_link_open()` which turns a load from one into an `opened` |
| `rec_ledger` | 2 | one identity and event trail for every recommendation on every surface: an episode per (restaurant, key), `present()` when shown, `record()` for each answer, `silenced_keys()` so an answer given anywhere holds everywhere; an answer carried in from an older table attaches to the episode current when it was given (never a later one) and is recorded once per key across every episode; an unanswered episode expires 14 days after it was created, one re-shown with a new figure or target (or replaced by a newer key of a one-live-at-a-time kind, or by a new model read) closes as `superseded`; `implemented()` where a change is actually made (price applied, order sent, reply posted, campaign sent, schedule edited to match); subject tags per episode (`tags_for`), the tracker that measures it (`tracker_id`, permanent), check-ins, the structured `REASON_CODES`, and the missed-detection log (`note_problem`); the admin console's acceptance view counts only episodes that were shown and not bookkeeping keys (`restore_kind:`, `calibration:`, `standby:`) |
| `rec_learning` | 2 | what the ledger has learned about ONE restaurant: the bounded per-restaurant effectiveness model the rankers read (Home order, the one thing, the DSR's actions — acceptance × success × dollar calibration by kind and tag, shrunk toward the cohort prior over `MIN_COHORT`, a decaying penalty for a worse result, never applied to a critical item), `kind_record(rid, kind)` (the Historical Accuracy input: shown + taken episodes through `learned_verdict`, one per window, own rate at ≥ `MIN_MEASURED_FOR_RATE`, else the anonymous cohort over `PRIOR_MIN_MEASURED`), and the owner's own record behind `/recs/summary`, `/recs/timeline` and `/recs/checkin`, redacted per viewer (`viewer_sees`) |
| `data_health` | 2 | the Restaurant Data Health Score (the Data Health Engine, built on `data_freshness`): `record_attempt(rid, source, ok, …)` writes the `source_health` sync ledger from every ingest point (never raises; `data_freshness._with_health` turns consecutive failures into an error on the source); `snapshot(rid)` → per-source owner lines, the overall % (weighted mean held by named caps: any failing or stale source ≤74, a module's blocking source down ≤49; not-connected listed apart, never scored), and per module the decision and Recommendation Confidence Impact (`confidence_engine.impact`, freshness replayed as current — never counted twice); `readiness(rid, module, delivery)` → proceed / caveat / wait / refuse plus the DATA STATE `prompt_block` and the validation `data_state`, enforced by `ai_utils.create_with_retry(readiness=)`; `record_daily` → `data_health_daily` for the admin rollup. Owner-surface helpers: `SLOTS`/`next_slot`/`expected_phrase` (when a source next refreshes, in the restaurant's clock — the scheduler's times, held in step by a test), `is_pending`, `counts_as_current` (the cadence rule for "current"/"live"), `connection_lines` (Account → Connections and the Reviews tab), `metrics_line` (Marketing), `affects`/`fix_hint` (the `data_source_down` alert), `compact` (Home's embed) |
| `rec_trust` | 2 | `assess(rid, key, evidence, sources)` → the K1 confidence object every recommendation-bearing payload carries (Home cards, attention, the one thing, diagnoses, drivers, Ask, DSR actions): evidence from the caller, accuracy from `rec_learning.kind_record`, freshness from `data_freshness`, the owner's `dont_trust_data` answer capping a kind's evidence for 30 days; `Context` reads each input once per build; never raises (every dimension null and the low band on failure) |
| `owner_report` | 2 | "What worked for you" (rec-ROI #28): `what_worked(rid, days, viewer)` (days 90 or 180) → `{ok, days, enough, sentences, facts}`, built without a model from `rec_learning.summary` (acceptance by module, ignored in the denominator), the stored before/after results of recommendations the owner took (mean `delta_pct` per metric over non-overlapping results, ≥ 2), `outcomes.cumulative` over the window (net, ≥ 14 measured days) and the most effective tag; each clause only when its minimum is met, "associated with" / "measured", never "caused"; redacted per viewer like `/recs/summary`. Behind `GET /recs/what-worked`, Home's "What worked for you" card and the owner's monthly email |
| `compliance_packs` | 2 | jurisdiction rule packs applied under the owner's own scheduling rules |
| `reservation_feeds` | 2 | the frame for reservation systems writing demand_signals; no provider is live |
| `labor_replacements` | 2 | who could cover a shift |
| `loss_detection` | 2 | comps/voids/refunds signals (owner-only) |
| `main` | 4 | pre-hosted CLI (`--demo`); runs its own `schedule` loop with no lease — do not run beside the real scheduler |
| `marketing` | 2 | post drafting and the content log |
| `marketing_drafts` | 2 | saved drafts and approval |
| `marketing_links` | 2 | tracked short links |
| `marketing_media` | 2 | uploaded images |
| `marketing_publish` | 2 | scheduled and direct publishing |
| `marketing_signals` | 2 | per-post attribution and the summary |
| `marketing_tags` | 2 | dish / occasion / kind tagging of posts |
| `menu_intelligence` | 2 | dish scorecard and repricing |
| `meta_api` | 2 | Instagram/Facebook Graph calls |
| `metrics` | 2 | the registry of measurable things |
| `milestones` | 2 | firsts an owner is told about once |
| `mobile_api` | 3 | the iOS `/mobile/api/*` routes |
| `models` | 1 | schema at boot, `Restaurant`, most reads and writes |
| `monthly_review` | 2 | the month in review |
| `morning_brief` | 2 | the morning brief and its recipients |
| `notify` | 2 | alert firing: gates, holds, batch, delivery |
| `ops` | 1 | job ledgers, lease, claims, `capture`, failure digest |
| `db_restore` | 1 | the `RESTORE_FROM` boot-time restore (`docs/ops/RECOVERY.md`) |
| `ordering` | 2 | supplier order sending |
| `outcomes` | 2 | before/after trackers with the causation caveat |
| `nav` | 0 | the nav path — one address for every place and item an owner is sent to (`<head>[/<rest>][?query]`: modules, sections, item ids, filters); `path()` builds one, `module_of()` names its module. Web opens it with `cavNav`, iOS with `NavPath` (Friction audit 9/25/26) |
| `permissions` | 0 | roles and module view gates |
| `pos` | 0 | provider registry and capability dispatch |
| `pos_health` | 0 | `pos_sync_state(r)`: one provider-agnostic reading of the POS sync (provider, last success, error, age, state) over every provider's `{provider}_last_synced`/`_sync_error` columns — RPOWER included — for Home, the status page, admin, mobile connections and Data Freshness; connected means credentials (never a leftover stamp), the freshest credentialed provider is read, and current / aging / stale is `data_freshness`'s `pos` row (`age_state`: a deliberate function-scope L0→L2 import, so every surface names a POS state by one rule) |
| `preshift` | 2 | the staff pre-shift read |
| `pricing` | 0 | the one price list |
| `promise` | 2 | the sales audit's promise, measured |
| `provisioning` | 2 | account creation from a signed contract |
| `push` | 2 | APNs delivery and token lifecycle |
| `recipes` | 2 | recipe drafts and scan |
| `reporter` | 2 | the weekly digest |
| `review_common` | 0 | sentences the weekly and monthly reviews share, and `impressions()` — the ledger items for what a periodic email rendered (the email stages them; nothing here writes) |
| `reply_edits` | 0 | what the owner changes in a drafted reply, measured: `compare(original, final)` (word edit distance, category, closed-vocabulary signals) and `style_note()` — the drafter's OWNER'S EDITS block; pure |
| `response_validation` | 0 | the Response Validation Layer, pure (no database, clock, model or network; imports only `ai_guard` and `confidence_engine`'s thresholds): `validate(text, ValidationContext) → Verdict` (pass / caveat / withhold / refuse, findings, rewrites, caveats, controls) over typed `Fact`s — the rule codes F1–F8, C1–C2, K1, X1, M1–M2, B1, N1, T1, A1–A2, P1, I1 (PROMPT_LIBRARY.md → Response Validation); `validate_lines` for the digest, DSR and weekly plan; `kind_of_key`, `facts_from_dict`; `mode_for` / `apply` (RESPONSE_VALIDATION_MODE shadow|enforce per surface); `log()` reaches `ai_utils.log_validation` lazily (the one upward call) into `ai_validation_log`. Built, not yet called by any model call site (adoption is workstream A) |
| `review_intelligence` | 2 | the reviews consultant layer and diagnosis |
| `rpower` / `rpower_routes` | 2 / 3 | RPOWER provider / bootstrap and status routes |
| `sales_audit_cheatsheet` | 2 | the in-person pitch cheat-sheet (deterministic) |
| `sales_audit_engine` | 2 | audit scoring and ROI math |
| `sales_audit_notes_ai` | 2 | audit notes (model) |
| `sales_audit_routes` | 3 | `/admin/audits/*` |
| `sales_audit_schema` | 0 | the audit's questions and sections |
| `sales_audits` | 2 | the audit store |
| `scheduler` | 4 | the job loop and every `run_*` job |
| `security` | 1 | durable login throttling, breached-password check, freeze |
| `security_headers` | 0 | response headers |
| `shift_quality` | 0 | the pure schedule scorer — no I/O |
| `staffing_curve` | 0 | how many of each role a shift needs half hour by half hour (the hourly sales curve interpolated to the half hour) and the section cap every requirement is held under — one answer for the scorer, the prompt's requirements table and the passes; pure, no I/O |
| `schedule_requirements` | 0 | what the generation prompt is told from the scorer's own inputs: the SHIFT REQUIREMENTS table, experienced staff, usual patterns, the chunk-seam fairness summary, a regeneration's focus — pure, no I/O |
| `thresholds` | 0 | the numbers that decide when something is a problem, one definition read by every surface (labor over target, a strong day, the age of a reply owed) |
| `social_routes` | 3 | Instagram/Facebook OAuth and publish routes |
| `square` / `square_routes` | 2 / 3 | Square provider / routes |
| `staff_roster` | 2 | names and job roles |
| `staff_routes` | 3 | the staff portal `/staff/*` |
| `staff_schedule` | 2 | the published week as an employee sees it |
| `status_manager` / `status_routes` | 2 / 3 | the public status page |
| `strategy_jobs` | 4 | the scheduled half of the strategic features |
| `strategy_routes` | 3 | one body per route, registered at `/api/…` and `/mobile/api/…` |
| `time_off` | 2 | time-off requests |
| `time_utils` | 0 | zones, local now, stamps |
| `toast` / `toast_routes` | 2 / 3 | Toast provider / routes |
| `value_delivered` | 2 | the four value figures, never summed |
| `waste_trend` | 2 | the waste trend engine and forecasts |
| `weather` | 2 | NWS forecast, cached on the restaurant row |
| `webhook_routes` | 3 | inbound Stripe / DocuSign / Resend / Twilio |
| `webhooks` | 2 | outbound webhook config and delivery |
| `weekly_review` | 2 | the week in review |
| `worker` | 4 | optional second scheduler process in the same Railway service |
| `intelligence.*` | 2 (`stats`, `privacy`, `categories` are 0) | see `INTELLIGENCE_ENGINE.md` |

---

## 5. Shared utilities and where they belong

| Need | Use | Not |
|---|---|---|
| an environment value read in more than one module | `config.py` | a new `os.getenv` with its own default |
| a model or an Anthropic client | `ai_utils.model_for(purpose)`, `ai_utils.get_client()` | a `"claude-…"` literal or `anthropic.Anthropic(...)` |
| "now" or a stamp | `time_utils` (`restaurant_now`, `utc_stamp`, `parse_stored_dt`, `OPERATOR_TZ`) | `datetime.now()` in a module that has a restaurant in hand |
| a database connection in a module that calls `get_conn()` bare | the call-time wrapper pattern (`client_api`, `mobile_api`, `drafter`, `social_routes`, `demo_seed` have it) | `from models import get_conn` at module scope |
| a table | `models.init_db` (or an `init_*` it calls) | `CREATE TABLE` on a request or job path — only `ops.py`'s lease tables, `ai_utils._ensure_usage_schema` and `waste_trend`'s once-per-process `_SCHEMA_ENSURED` guard are allowed to |
| a swallowed error | `ops.capture(e, job=, context=)` | `except Exception: pass` around a write (lint fails) |
| an email | `emails.send_*` / `emails.deliver`, colours from `emails.BRAND` | inline HTML with literal colours (ratchet lint) |
| a figure a model may quote | `ai_guard.verify_figures` and the claim kinds; for a whole output, `response_validation.validate` with typed facts | prose the client cannot trace |
| a claim a model makes about money, cause, certainty or benchmarks | `response_validation.validate` (one context, one verdict) | a per-module guard sequence or a prompt-only rule |
| owner-facing label copy | the banned-label ratchet `scripts/check_owner_copy.py` | "savings", "value delivered", "optimized" over a gap or an estimate |
| a route both surfaces need | a `strategy_routes._ROUTES` entry, else a `_do_*` body, else `_m()` | a second copy of the body |
| review-period sentences | `review_common` | copies in the weekly and monthly modules |
| a colour, font, spacing, component | `DESIGN_SYSTEM.md` tokens and components; iOS `DesignSystem/` | a literal or a feature-local kit |
| a number, table, route or job count in a doc | `python3 scripts/repo_inventory.py` | a typed number |

---

## 6. Intentionally separate

Files that look mergeable and must not be merged, and why.

| Keep separate | Because |
|---|---|
| `client_api.py` and `mobile_api.py` | two auth models (session cookie vs bearer) over one set of bodies; the twin pattern is the design, the duplication is the bug |
| `toast.py`, `square.py`, `clover.py`, `rpower.py` | one provider each behind `pos.PROVIDER_API`; a feature that lands in one must be checked against the registry, not copied |
| `weekly_review.py` and `monthly_review.py` | same four questions at two cadences with different data windows and monthly-only sections (`_plan_score`, `yoy_clause`); the shared sentences already live in `review_common` |
| `shift_quality.py` and `labor.py` | the scorer is pure and I/O-free so it can be audited and tested alone; `labor` is the glue |
| `scheduler.py` and `strategy_jobs.py` / `intelligence/jobs.py` | the loop and its gates vs the job bodies |
| `notify.py`, `emails.py`, `push.py` | the firing decision vs the two channels; nothing may deliver around `deliver_alert` |
| `models.py` and `auth.py` | `auth` owns the identity tables and their pepper rules; `models` owns everything tenant-shaped; each imports the other only lazily |
| `static/fonts/` and `public/static/fonts/` | two deploy targets (Flask vs the Cloudflare Worker); `tests/test_public_site.py` pins them byte-identical |
| `Procfile` and `railway.json` | Railway reads `railway.json`; the Procfile is only for a platform that has neither |
| `init_db()`'s ALTER list and `ensure_columns()` | two migration lists that both run at boot; 57 columns exist only in the second. Pick one for a new column; do not try to merge them in a hurry |
| `ops.py`'s per-call `CREATE TABLE` for the lease and claims, `ai_utils._ensure_usage_schema`, and `waste_trend.load_waste_history`'s once-per-process guard | must work on a fresh volume before boot completes (ops), or run at most once per process (the other two); the documented exceptions to "DDL at boot" |
| `hosted_dashboard`'s local `login_required` / `get_current_user` | a deliberately weaker copy for the one shell route (`/`) that must not be billing- or module-gated |
| `tests/test_push_delivery.py` and `tests/test_webhook_delivery.py` | parallel by design (APNs over httpx vs webhooks over requests), same test names |
| `audit_app.py` | a standalone Flask app Will runs on sales calls; not a blueprint, not in the web process |
| `worker.py` | the documented second-process shape (`docs/ops/RAILWAY_SCHEDULER_SPLIT.md`); not in the start command today |
| `main.py` + `dashboard.py` + `dashboard_template.html` | the pre-hosted demo/CLI; 0 importers; kept until the owner confirms nobody runs them, then `docs/history` |
| `demo_seed.py` | demo data off the request path and out of `models`; gated on `is_demo` and the account name |
| `sample_*.csv` at root | named as fallbacks by the loaders, which actually parse embedded copies; harmless, cheap to keep |

---

## 7. Naming conventions and folder standards

**Python**
- Route modules end in `_routes.py`; the two big surfaces are `client_api.py` and `mobile_api.py`; `strategy_routes.py` registers each `_ROUTES` entry at both prefixes.
- Mobile handlers are `mobile_<thing>`; the shared body a web route delegates to is either `_m("mobile_x")` or a `_do_<thing>(…)` in `client_api`. Web handlers that only delegate carry the docstring `Web twin — the one body is mobile_api.<name>`.
- Scheduled work is `run_<job>` (runs a pass) or `check_<thing>` (evaluates and alerts); every job claims its period with the key used in `scheduler_loop` (`SYSTEM_ARCHITECTURE.md` lists them); owner-facing jobs gate on `local_due` and may pass `day=`.
- Boot-time DDL helpers are `init_<table_group>` and are called from `init_db`.
- Private helpers say what they return when a same-named helper elsewhere returns something else: `_one_dict` / `_one_row` / `_scalar`, `_rows_dict` / `_rows_raw`, `_iso_z` / `_stamp`, `_as_date`, `_normalize_phone_lenient`.
- A new `restaurants` column has four touch points (dataclass, a migration list, `update_restaurant().allowed`, `get_restaurant()` hydration) — `tests/test_models.py` enforces it for `al_*`/`alert_*`.
- Comments explain why, in the register of the incident the code prevents; no restating the line below.

**Web**
- One template, one module per panel: `#panel-home`, `#panel-reviews`, `#panel-labor`, `#panel-inventory` (Food Cost's historical id), `#panel-marketing`, `#panel-competitor` (Intel), `#panel-account`.
- CSS class families by module: `hb-` (Home and the shared brand language), `rv2-` (Reviews), `lb2-` (Labor), `fc2-` (Food Cost), `in2-` (Intel), `ask-` (Ask Cavnar), `ac-` (Account), `cm-` (motion), `cbtn-` (buttons, `static/css/cavnar-buttons.css`). Colours are tokens (`--ink`, `--ember`, `--hb-*`, `--sf-*`, `--elev-*`, `--cb-*`); a new family gets its prefix and a `DESIGN_SYSTEM.md` row.
- JS is ES5 only, including comments; every button carries `.cbtn`; every fetch goes through `_csrf_fetch.html`'s wrapper.

**iOS**
- `Features/<Module>/`: `<Module>View.swift`, `<Module>ViewModel.swift`, `<Module>Models.swift` (or `Models/` for shared shapes), one file per sheet or section. Request and response structs live with the view model that owns the endpoint; the `{ok, error}` envelope is `APIClient.OKResponse`.
- Design-system types are `Cavnar*` (`CavnarCard`, `CavnarTone`, `CavnarMotion`, `Font.cavnarBody/Number/Headline`, `Color.cavnarInk…`); Account sheets use `AccountSheetKit`. No `.font(.system(` on text; symbol sizing only.
- Endpoints are `/mobile/api/...` string literals in the view model; a route with no iOS caller is fine, an iOS caller with no route is a bug (`hosted_dashboard` returns "update the app" for it).

**Tests**
- Files named for the module or concern; test names are sentences. A rule that must hold everywhere is asserted against the source or the registry, not one rendered payload. Tests that patch `models.get_conn` also patch the modules that still bind it.

**Docs**
- The ten reference docs at the root are the canonical homes listed in `CLAUDE.md`; `docs/ops`, `docs/plans`, `docs/history` for the rest. A doc quotes commands for counts. A superseded doc moves to `docs/history/` with a line in its README; it is not deleted.
