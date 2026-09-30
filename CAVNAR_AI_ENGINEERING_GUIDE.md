# Cavnar AI Engineering Guide

**This is the authoritative reference for working in this codebase.** Read this before exploring the code directly. It cross-references the other reference docs (`PROJECT_CONTEXT.md`, `SYSTEM_ARCHITECTURE.md`, `DATABASE_SCHEMA.md`, `API_REFERENCE.md`, `MODULE_OVERVIEW.md`, `PROMPT_LIBRARY.md`, `ROADMAP.md`, `TESTING.md`) rather than repeating them — if this guide and one of those disagree, the more detailed file is right and this one is stale; update this one.

**Working rule**: don't rediscover architecture that's already written down here. Grep or read a specific implementation file only when a task actually requires touching it — not to "confirm" something this guide already states. If something here turns out to be wrong or stale, fix the doc as part of the task, don't silently work around it.

---

## 1. Overall architecture and design principles

Cavnar AI is a restaurant intelligence SaaS: one Flask backend, one SQLite database, two clients (a single-page web dashboard and a native iOS app) that read the same computed data through the same logic. Full topology: `SYSTEM_ARCHITECTURE.md`.

**Principles that shape every design decision here**:
- **One computation, two renderers.** Web and iOS never maintain parallel implementations of the same business logic — `mobile_api.py` computes it once, `client_api.py` delegates via `_m()`, and the SwiftUI views consume the same JSON shape the web JS does.
- **Absence is not zero.** Every scoring/aggregation system (Shift Quality, Operational Score, Intel visibility, Labor's partial-period math) distinguishes "we don't know" from "it's bad." A missing measurement returns `None` and the surrounding weights renormalize; it never silently becomes a zero that drags a score down.
- **The deterministic layer computes; AI narrates.** Every number a client sees (a percentage, a dollar figure, a score) comes from plain Python. AI is used to interpret and phrase what was already computed — never to compute it. Where a model states something it can't verify against what was actually passed to it, the expected behavior is to mark it `UNVERIFIED` rather than assert it.
- **Propose, don't perform**, for anything with an effect outside the building. Ask Cavnar's write-kind tools return a confirmation card; nothing emails a supplier or posts publicly without an explicit owner confirmation through the same route a manual button would use.
- **Tenant isolation is server-side and non-negotiable.** Every query scopes by `restaurant_id` taken from the authenticated session/token. A code path that trusts a client-supplied restaurant/tenant id is a P0 finding, full stop.
- **Additive schema changes only.** New columns via guarded `ALTER TABLE`; no destructive rewrites. See §3.

---

## 2. Module summaries

Full detail in `MODULE_OVERVIEW.md`. Condensed here for quick orientation:

| Module | Core job | Key files |
|---|---|---|
| **Reviews** | Fetch → AI sentiment/urgency → AI draft reply → owner approves/posts | `fetcher.py`, `analyser.py`, `drafter.py` |
| **Labor** | Shift CSV → labor % vs target, overtime/overstaffing detection, AI-generated + graded schedules | `labor.py`, `shift_quality.py` |
| **Food Cost** | Weekly counts → waste cost, price-drift alerts, purchase orders | `inventory.py`, `inventory_ledger.py` |
| **Marketing** | AI-drafted posts, content calendar, guest text-club | `marketing*.py`, `guest_marketing.py` |
| **Intel** | Competitor snapshots + AI-visibility ("do LLMs mention us") checks | `competitor.py`, AI-visibility routes |
| **Scheduling** | Shift Quality Engine grades a generated schedule on 11 weighted dimensions; a separate `scheduler.py` runs unrelated background jobs (fetches, digests, alerts) — don't conflate the two "scheduling" meanings | `shift_quality.py` (schedule grading), `scheduler.py` (background jobs) |
| **Admin** | Will's console: client health, jobs and the backup, billing and contracts, AI operations, messaging, the audit trail, support tools and offboarding, the sales-audit tool | `admin_routes.py`, `admin_ops.py`, `admin_events.py` |
| **Platform** | The job loop and its registry, the backup, operator paging, `/health`, request telemetry, the supervisor thread, logging | `scheduler.py`, `jobs_registry.py`, `ops.py`, `status_manager.py`, `platform_monitor.py` |
| **Billing** | Stripe and DocuSign lifecycle, the billing email outbox, the subscription mirror, reconcile | `webhook_routes.py`, `billing_jobs.py`, `pricing.py` |
| **Ask Cavnar** | Tool-calling AI assistant with live access to every module's data | `ask_cavnar.py`, `ask_cavnar_tools.py` |

---

## 3. Data model conventions and key entities

Full schema: `DATABASE_SCHEMA.md`. Conventions that apply everywhere:

- **`restaurants`** is the tenant root; nearly every other table FKs to `restaurants.id`.
- **A new `restaurants` column needs four touch points, not one**: the `Restaurant` dataclass field (`models.py`), a migration entry (`init_db()`'s ALTER list or `ensure_columns()` — both run at boot), `update_restaurant()`'s allowed-write whitelist, and `get_restaurant()`'s hydration. Miss the whitelist and writes silently no-op; miss hydration and the value is in the DB but invisible to every reader.
- **Employees are identified by name**, not an ID — POS shift data carries no stable identifier. Every staffing feature matches on the name string, case-insensitively.
- **Append-only audit tables are never mutated in place**: `login_history`, `ask_cavnar_actions`, `alert_log`, `capability_changes`, `admin_events`, `support_notes`, `billing_status_history`. "Current state" is always computed by querying the latest row. Retention is a separate question, and there is ONE registry for it: `ops._RETENTION_DAYS` / `_RETENTION_COLUMN`, with each table's floor, the rollup run before its rows go and its readers' windows beside them (`_RETENTION_FLOOR_DAYS`, `_RETENTION_ROLLUP`, `_RETENTION_READERS`), pruned nightly after the backup in chunks under a time bound and a per-table, per-pass row cap (`alert_log` after 180 days, `admin_events` 400, `email_log` 365, among others — `DATABASE_SCHEMA.md` → Retention). A window set under its floor is refused and paged, never applied. A new ledger gets an entry there — days, column, floor, and its readers — not a prune loop of its own, and a reader that reaches past its table's window reads a summary written before the rows go (`history_rollups`) or it fails `tests/test_mem_m7_retention.py`. `login_history` is kept 90 days (in the registry; the boot prune, `auth.prune_login_history`, reads the same window); `ask_cavnar_actions`, `ops_markers`, `business_metrics_daily`, the AI daily rollups, `change_log`, `value_snapshots` and the monthly and weekly summaries are never pruned. `admin_events`, `offboarding_steps` and `billing_status_history` survive a restaurant's deletion (`models._KEEP_ON_RESTAURANT_DELETE`), and so does its anonymised learning — its `intel_rec_events` and `intel_features` rows under a tombstoned id, keys hashed, no name (owner decision 9/29/26; never a demo's or a test account's), and `models.delete_restaurant` never deletes an admin or support login — it re-homes one that has another membership, or refuses (`models.InternalLoginHome`).
- **An admin action is audited through ONE typed call**, `admin_events.record_admin_action(actor, event_type, restaurant_id=, target=, before=, after=, result=, summary=)` — never `admin_events.record("admin", …)` from a route (`tests/test_fix_int2_admin.py` holds `admin_routes.py` to it); the summary-only `record` is for events no console login makes (webhooks, provisioning, break-glass and boot events, an owner's close-account request and its `deletion.notice_skipped`). A billing-state change made by an admin runs inside `models.billing_context(source="admin" | "admin_override", actor=, reason=)`.
- **JSON-in-a-column** is for structured settings read/written as one blob by one piece of code (`quality_weights_json`, `shift_leader_rules_json`, etc.) — not for anything that needs to be queried by an internal field.
- **`is_demo`** on `restaurants` marks an account whose seed data may be reset — never true for a real client, including Simple EJ's now that Erik is paying.
- **Every recommendation has one identity and one lifecycle: `rec_ledger`** (`rec_instances` + `rec_events`, `DATABASE_SCHEMA.md` → Recommendations). A surface that shows one calls `rec_ledger.present()` (or `present_many`) with a key `kind:subject` — the subject is what it is about ("trim_day:Saturday"), so a different day or dish is a different recommendation; every answer calls `record()` (with a `reason_code` from `REASON_CODES` when the owner says why not); every surface about to say something checks `silenced_keys()`. The lifecycle is open → accepted | completed | implemented | dismissed | expired (14 days unanswered: ignored, in every denominator) | superseded (replaced by new content before anyone answered: in no denominator). Where a change is actually MADE (a price applied, an order sent, a reply posted, a campaign sent, a schedule edited to match), call `rec_ledger.implemented()` — never from a button that only says yes; inside someone else's write transaction use `implemented_on(conn, …)`. A tracker started for a recommendation is linked to its episode for good (`tracker_id`). What the ledger learns is read through `rec_learning`: the bounded per-restaurant effectiveness weight every ranker applies (Home, the one thing, the DSR — never to a critical item), and the owner's own record (`/recs/summary`, `/recs/timeline`, `/recs/checkin`), redacted per viewer by `rec_learning.viewer_sees`. The intelligence engine reads the same trail (`intelligence/feedback.sync`).
- **How long an answer holds is one rule, `rec_ledger.answer_silence`** (by kind and by the owner's reason; a caller's longer silence is capped by it): "not for us" a year, then the card comes back naming the earlier answer (`previous_answers`); Done a year on a one-off, 60 days on a situational kind — re-armed sooner once its trigger clears and fires again (`reconsider`, never inside 14 days) — and until the next occurrence on a recurring one; "bad timing" 28 days, in no rate; "don't trust the data" until that source is re-verified, 90 days at most; a hide 14 days; a safety kind (stock) 7 days at most, lifted by a recount or delivery. An answered key reopens as a new episode when its figure at least doubles (`reopen_change`). A delegate's answer silences only that login (`rec_silences`) — the episode stays open for the owner — and an admin's view-as answer changes no status or silence. Answer messages say what the answer does (`silence_message`).
- **Memory reaches a prompt through one assembler, `memory_context.memory_context(rid, surface, viewer=, subjects=)`**: fenced, dated (M/D/YY) sections from the `PROVIDERS` registry (string-dispatched — grep the registry, not the symbol), relevance-ordered, within per-section budgets, sizes logged. Never hand-pick memory in a prompt builder. Every line carries an audience (`team` / `principals` / `author`) and a module, and a viewer never reads a line it may not. An output more than one login reads (`memory_context.SHARED_SURFACES`: the schedule draft, the stored reads and diagnoses, the reply drafter, marketing, the nightly narrative, the weekly plan) is assembled for the team viewer, `memory_context.TEAM` / `team_viewer(surface)` — a manager's view, so no owner-only line and no comps and voids — whoever asked. A new surface must be classified (`tests/test_mem_int_privacy.py`).
- **A goal is the target a module judges against.** An account holder's active goal on a metric (`owner_goals`, `goals.set_goal`) overrides the module's own setting everywhere a figure is judged — Labor, Food Cost, the DSR, the alerts (`owner_memory.target_for`, read by `thresholds.target_for`). A teammate's or an admin's goal is stored `proposed` and changes nothing until an account holder confirms it (`goals.confirm_goal`).
- **Margins that fire advice are fitted per restaurant** (`restaurant_thresholds.margin`, refreshed nightly by `learning_memory`): the larger of the stated margin and 1.645 σ of this restaurant's own history. `thresholds.LABOR_OVER_TARGET_PTS` (3 points) is now the FLOOR under each restaurant's fitted margin — a restaurant whose labor % swings on its own is no longer flagged on noise — so a new "over target" check reads `margin(rid, "labor_over_period", stated=LABOR_OVER_TARGET_PTS)`, not the constant.
- **Who teaches a pooled learner is one predicate, `models.learning_eligible`** (no demo, test or internal account — automatically, with the admin's `learning_override` — and nothing a converted demo recorded before its `learning_since`); **who learns for itself is `models.learns_for_itself`** (everyone but a demo or an admin's exclusion — the name rule never switches off a restaurant's own learning, memory re-audit 9/29/26), and whose answer it is is `permissions.answer_authority` (`admin` | `principal` | `delegate`): an admin's view-as write teaches nothing. Anything pooled across restaurants is de-identified ratios over at least five restaurants, with no Google user data (`INTELLIGENCE_ENGINE.md` → *Who may teach*, *Google user data*; `docs/ops/SECURITY.md` lists every learner's marker).

---

## 4. API patterns and service boundaries

Full reference: `API_REFERENCE.md`. The load-bearing pattern:

```python
# client_api.py
@client_bp.route("/api/ask-cavnar/opening")
@login_required
def ask_cavnar_opening(current_user):
    return _m("mobile_ask_opening")(current_user)
```
`_m(name)` resolves `mobile_api.<name>` and unwraps its auth decorator, so a web route and its iOS counterpart run the identical function body. **When implementing a new endpoint that both surfaces need, write it once in `mobile_api.py` and delegate from `client_api.py`** — not the reverse, and not twice.

Response convention: `jsonify(ok=True, ...)` / `jsonify(ok=False, error=...), 4xx`. Side-effect sends (email/SMS) are best-effort (`try/except` → `ops.capture()`), never blocking the request the user is waiting on. One email has one sender: the welcome is `emails.send_welcome_with_set_password_link` whoever sends it (the post-signing outbox, checkout provisioning, the console's Resend welcome) — don't add a second template or link lifetime for the same email. Work a request hands to a thread carries the requester's AI attribution: `ai_utils.attributed(fn)` for a thread, `ops.run_admin_task` or `admin_routes._submit_admin_job` (the one admin job pool) for console work. CSRF required on state-changing web POSTs. Ask Cavnar and AI-visibility calls are both rate-limited and budget-gated before any model call fires (`ai_utils`).

---

## 5. AI providers and when each is used

Full detail: `PROMPT_LIBRARY.md`.

- **Anthropic Claude** — everything except AI-visibility. Haiku for the high-volume classifiers (review analysis, competitor menu extraction, email personalisation, the marketing insight card); Sonnet for anything an owner reads as advice (insights, diagnoses, drafts, schedules, Ask Cavnar); Opus for invoice transcription only. The per-call table in `PROMPT_LIBRARY.md` is the single list — do not restate tiers here.
- **Perplexity (`sonar`)** — Intel's AI-visibility check only, nowhere else; a REST call, not an SDK.
- Every Anthropic call routes through `ai_utils.create_with_retry()`: budget and breaker check → retry-with-backoff → the `ai_usage` ledger (every OUTCOME, a call blocked before it left included) and the redacted `ai_calls` trace. Never call `messages.create` directly from a route or module. Google Places goes through `ai_utils.places_request` (its own ceiling and breaker). A finding about a model's output is an `ai_utils.record_quality_event`, not an `ops.capture` (`PROMPT_LIBRARY.md` → *The AI operations ledger*).
- `temperature` is never passed — the production SDK build rejects it; `create_with_retry` strips it.

---

## 6. Coding standards and naming conventions

- **Delegate, don't duplicate** (§4).
- **Revert-check methodology for bug fixes**: temporarily undo the fix, confirm the specific test written for it actually fails, then restore. A test that still passes with the bug reintroduced protects nothing.
- **Silent `except: pass` around a database write is a lint failure** (`tests/test_silent_handler_lint.py`) — route the error to `ops.capture(e, job=..., context=...)`.
- **Dashboard JS is ES5-only**, enforced including inside comments (`tests/test_frontend_rules.py`) — no `const`/`let`/arrow functions/backticks/`async`/`await`.
- **Test names read as full sentences** describing the behavior under test (`test_a_new_share_gets_an_expiry_about_sixty_days_out`), not identifiers.
- **A test asserted against one rendered payload only covers that fixture's branches** — assert against the source/logic when a rule must hold everywhere (see `feedback_fixture_shaped_tests` in project memory).
- **Comments explain *why*, not *what*** — this codebase's existing comments consistently narrate the bug or incident a piece of code exists to prevent, not a restatement of the line below them. Match that register in new code.

---

## 7. UI/UX guidelines

- **Typography is tokenized**: Space Grotesk for every number (iOS + web, including numeric segments inside otherwise-prose text), Clash Display for headlines, Apfel Grotezk for UI chrome. Never hand-pick a font per element.
- **Color is a CSS variable, always** (`--ink`, `--ink2`, `--ink3`, `--ember`, semantic `--hb-good`/`--hb-warn`/`--hb-bad` per module scope) — redefined per theme (light default `:root`, dark via `[data-theme="dark"]` and the `prefers-color-scheme` media query). A literal hex in a rule that only makes sense in one theme fails `scripts/check_colors.py`.
- **Text-size scale is uniform across modules** — the Account scale (15/16/22) is the reference; don't inflate one module's text independent of the others without deciding that's now the new baseline everywhere.
- **Loading state**: the sliding orange pulse bar for quick async loads — not "..." or a spinner.
- **Motion**: 15 approved animations (`CavnarMotion.swift` on iOS, `cm-` prefixed CSS/JS on web) — ember-only accent color, no bounce easing.
- **A margin/positioning fix on a shared header component should be checked against text wrapping**, not just the common-case rendered width — a `margin-left` on a badge that can wrap to its own line will visibly indent it; prefer `word-spacing` on the container when the goal is "more space between these two words," since trailing whitespace is trimmed at a wrap point and a margin is not.
- **A flex child's inline `style` attribute beats an external stylesheet rule of any specificity** (short of `!important`) — if a shared template sets `style="flex:1"` on an element you need to resize, edit that inline style directly rather than adding a competing CSS rule that will silently never apply.

---

## 8. Security assumptions and authentication model

Full detail: `SYSTEM_ARCHITECTURE.md` §Auth.

- **Web**: session cookie (`sessions` table), `login_required` decorator, CSRF token on mutating POSTs.
- **iOS**: Bearer token, same `sessions` table, `mobile_login_required` decorator.
- **2FA**: SMS/email OTP + hashed single-use backup codes (`two_fa_backup_codes`).
- **Team access**: multiple `users` rows per restaurant; only `role == 'owner'` can invite/revoke teammates.
- **Admin**: a wholly separate `admin_required` decorator — no client role, however elevated, reaches `/admin/*`. The one non-client exception is Cavnar's own `support` role, which reads the console (personal data masked) and opens view-as but gets 403 on every write. An internal login has its own second factor (required once `ADMIN_REQUIRE_2FA=1`), a 12-hour absolute session, a per-session request ceiling, and must type its password again within 15 minutes for a sensitive action (`@recent_auth_required()` under `@admin_required`, or `auth.reauth_refusal(current_user)` inside a route that needs it for only part of what it does). An expired session answers 401 JSON to anything that wants JSON (every `/admin/api/` read included) and a 302 to a page. View-as lasts 2 hours, shows a banner, and records every write against the acting admin. Every admin write is audited after the response. Full list: `docs/ops/SECURITY.md`.
- **Anything sent from an admin action** (email, SMS, a Stripe change, a DocuSign void) is refused on a backend where `scheduler.scheduling_allowed()` is false — a laptop holds production's keys; a send that is only a side effect is skipped with a note instead. The deliberate exceptions (the admin's own 2FA code, the owner and login flows, the referral email) are listed in `docs/ops/SECURITY.md`; a new admin send checks the gate. `hosted_dashboard.py` runs `app.run(..., load_dotenv=False)` so a copy of the app in a worktree never loads the main checkout's `.env`.
- **Assumption an auditor should verify on every new route**: does this handler derive `restaurant_id` from the authenticated user/session, or does it trust a path/query/body parameter? The latter is an IDOR until proven otherwise.
- **Inbound webhooks** (Stripe, DocuSign, Resend, Twilio) are signature-verified and, where the provider retries, event-id de-duplicated (`stripe_events_seen`, `docusign_events_seen`; the Twilio signature check is in `webhook_routes.py`) before any side effect runs. A missing secret fails closed, and every signature failure is counted per provider so a rotated secret shows up in the console.
- **Outbound requests to an address someone else chose** (a customer's webhook, a competitor's page) go through `net_safety` — resolved once, private addresses refused, no redirects.

---

## 9. Performance expectations

- **Home tab / briefing endpoints are deterministic and cached** (60s per restaurant for `home_brief`) — no model call on a normal page load; a model call on every load of a frequently-hit page is a design smell here, not a norm.
- **Ask Cavnar's opening is instant by construction** — it's arithmetic over already-computed data, not a model call, specifically so the panel never feels slow to open.
- **Background jobs use claim-before-work** (`job_period_claims`, `scheduler_lease`) so a slow run and the next tick can never double-fire the same job — this is a correctness property, not just a performance one (a duplicated job here has produced real incidents: one bad fetch becoming 180 notifications). Every job is an entry in `jobs_registry.JOBS` (its SLA, whether it sends, its own time bound) and returns the standard counts `{attempted, ok, failed, skipped, hit_bound}`; a long network-bound sweep runs on a lane beside the loop, not on it.
- **AI calls are budget- and rate-gated before they fire**, not after — a runaway loop should hit a wall in `ai_utils`, not in a monthly bill.
- **Request-scoped caching** uses Flask's `g`, never a module-level global, so it can't leak across requests on a multi-threaded gunicorn worker.

---

## 10. "Don't touch" areas and architectural constraints

- **`shift_quality.py`'s three invariants** (no-data → `None`; a critical-dimension floor caps at its own score; confidence tracked separately from score) are each the fix for a real production bug. Any change to scoring logic needs a fresh audit against these, not just new-feature tests.
- **`pricing.py`'s `TIERS`** is the single source every dollar figure (website, DocuSign contract tabs, Stripe checkout, sales-audit ROI math) reads from. Never hardcode a price anywhere else, including "just for this one email."
- **The `_m()` delegation chain** — don't reimplement a `mobile_api.py` function's logic inside `client_api.py` "because it's simpler for this one route." It isn't, two months later, when they've drifted.
- **`tool_specs(restaurant)`'s module filtering** in `ask_cavnar_tools.py` — a tool must stay excluded for a restaurant whose tier doesn't include that module; don't offer a tool "just in case," since the model calling an unreachable tool and improvising an apology is worse than the tool not existing.
- **The read/action/write tool-kind split** — a tool with any effect outside the building (email, public post, an SMS to a guest) must be `write` and go through the proposal flow. Never promote one to `action` for convenience.
- **The marketing site (`cavnar.ai`) is a separately hand-deployed Cloudflare Worker.** A push to `main` does not deploy it. Don't tell the user their marketing-site change is live just because it's merged — Will runs `wrangler` himself.
- **DocuSign/Stripe webhook idempotency** (`docusign_events_seen`, `stripe_events_seen`) exists because these providers retry. Don't remove the de-dup check to "simplify" a handler. A billing write that fails must RAISE (so the claim is released and the provider redelivers), never be answered 200.
- **`/health` is read-only** — no write, no send, no page (the supervisor thread pages). It answers 200 for a degraded platform on purpose: failing Railway's deploy check would block the deploy that fixes it.
- **A send is reported from its real result.** Every email sender returns a `SendResult`; a route that exists to send answers 502 when the provider refused, and no code writes a "sent" row for a send that did not happen.
- **The job registry and the loop stay in step** (`tests/test_fix_d_jobs.py`; every job returns the standard counts — `ops.run_outcome` reads anything else as partial, never clean), and `tests/test_docs_controls.py` holds `docs/ops/SECURITY.md` / `RECOVERY.md` (and `DATABASE_SCHEMA.md`'s scrub list and retention table) to the code — change a control and its sentence together.

---

## 11. Verification policy (see `TESTING.md` for the full version)

Run only the test file(s) that cover what changed. The full suite (about 11,000 tests, 4-5 minutes in parallel) runs only after an audit or fix round, a big code change or a large batch of changes, or when asked — never as a routine pre-push step after a few tweaks (Will, 9/28/26: it "stalls productivity"). Color lint and ES5 checks only when a template/CSS/JS file actually changed. This is a deliberate, requested change from running the full suite by default — see `feedback_lean_verification` in project memory for the reasoning and date.

---

## Keeping this guide current

When a task changes something this guide states as fact — a new module, a new invariant, a new "don't touch" — update the relevant section (here and in the detailed doc it points to) as part of that task, not as a separate follow-up. A stale architecture doc is worse than no doc, because it gets trusted without being checked.
