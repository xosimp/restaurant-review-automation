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
| **Admin** | Will's console: client health, job runs, contract send, sales-audit tool | `admin_routes.py`, `admin_ops.py` |
| **Ask Cavnar** | Tool-calling AI assistant with live access to every module's data | `ask_cavnar.py`, `ask_cavnar_tools.py` |

---

## 3. Data model conventions and key entities

Full schema: `DATABASE_SCHEMA.md`. Conventions that apply everywhere:

- **`restaurants`** is the tenant root; nearly every other table FKs to `restaurants.id`.
- **A new `restaurants` column needs four touch points, not one**: the `Restaurant` dataclass field (`models.py`), the `ensure_columns()` migration entry, `update_restaurant()`'s allowed-write whitelist, and `get_restaurant()`'s hydration. Miss the whitelist and writes silently no-op; miss hydration and the value is in the DB but invisible to every reader.
- **Employees are identified by name**, not an ID — POS shift data carries no stable identifier. Every staffing feature matches on the name string, case-insensitively.
- **Append-only audit tables never get pruned**: `login_history`, `ask_cavnar_actions`, `alert_log`, `capability_changes`. "Current state" is always computed by querying the latest row; history itself is never mutated or deleted.
- **JSON-in-a-column** is for structured settings read/written as one blob by one piece of code (`quality_weights_json`, `shift_leader_rules_json`, etc.) — not for anything that needs to be queried by an internal field.
- **`is_demo`** on `restaurants` marks an account whose seed data may be reset — never true for a real client, including Simple EJ's now that Erik is paying.

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

Response convention: `jsonify(ok=True, ...)` / `jsonify(ok=False, error=...), 4xx`. Side-effect sends (email/SMS) are best-effort (`try/except` → `ops.capture()`), never blocking the request the user is waiting on. CSRF required on state-changing web POSTs. Ask Cavnar and AI-visibility calls are both rate-limited and budget-gated before any model call fires (`ai_utils`).

---

## 5. AI providers and when each is used

Full detail: `PROMPT_LIBRARY.md`.

- **Anthropic Claude** — everything except AI-visibility. Haiku (`claude-haiku-4-5-20251001`) for high-volume/cheap tasks: review analysis, labor insight, competitor analysis, sales-audit notes. Sonnet (`claude-sonnet-5`) for reasoning/quality-sensitive tasks: review reply drafting, marketing drafts, Ask Cavnar.
- **Perplexity (`sonar`)** — Intel's AI-visibility check only, nowhere else.
- All calls route through `ai_utils.create_with_retry()`: budget check → retry-with-backoff → usage logging. Never call the SDK directly from a route or module.
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
- **Admin**: a wholly separate `admin_required` decorator — no client role, however elevated, reaches `/admin/*`.
- **Assumption an auditor should verify on every new route**: does this handler derive `restaurant_id` from the authenticated user/session, or does it trust a path/query/body parameter? The latter is an IDOR until proven otherwise.
- **Inbound webhooks** (Stripe, Twilio) are signature-verified and event-id de-duplicated (`stripe_events_seen`, Twilio signature check in `notify.py`) before any side effect runs.

---

## 9. Performance expectations

- **Home tab / briefing endpoints are deterministic and cached** (60s per restaurant for `home_brief`) — no model call on a normal page load; a model call on every load of a frequently-hit page is a design smell here, not a norm.
- **Ask Cavnar's opening is instant by construction** — it's arithmetic over already-computed data, not a model call, specifically so the panel never feels slow to open.
- **Background jobs use claim-before-work** (`job_period_claims`, `scheduler_lease`) so a slow run and the next tick can never double-fire the same job — this is a correctness property, not just a performance one (a duplicated job here has produced real incidents: one bad fetch becoming 180 notifications).
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
- **DocuSign/Stripe webhook idempotency** (`docusign_events_seen`, `stripe_events_seen`) exists because these providers retry. Don't remove the de-dup check to "simplify" a handler.

---

## 11. Verification policy (see `TESTING.md` for the full version)

Run only the test file(s) that cover what changed. Full 1,908-test suite is a pre-push or full-audit step, run once — not a per-edit habit. Color lint and ES5 checks only when a template/CSS/JS file actually changed. This is a deliberate, requested change from running the full suite by default — see `feedback_lean_verification` in project memory for the reasoning and date.

---

## Keeping this guide current

When a task changes something this guide states as fact — a new module, a new invariant, a new "don't touch" — update the relevant section (here and in the detailed doc it points to) as part of that task, not as a separate follow-up. A stale architecture doc is worse than no doc, because it gets trusted without being checked.
