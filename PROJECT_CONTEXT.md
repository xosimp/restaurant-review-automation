# Project Context — Cavnar AI

## Business

Cavnar AI is a restaurant intelligence SaaS built and operated by Will Cavnar. It gives independent restaurant owners a single dashboard (web + iOS) that reads their POS, review, and inventory data and tells them what to do about it — not a BI tool that shows charts, a decision layer that names the problem, the day it happened, the dollar cost, and the fix.

- **First paying client**: Erik, owner of Simple EJ's. His three stated pain points — labor "through the roof," weak repeat-visit rate, and training — are the product's proof case.
- **Pricing** (`pricing.py`, mirrored on `pricing.html`): per module, one-time setup at checkout + a monthly or annual retainer starting 30 days after setup. 1 module $750/$349mo, 2 modules $1,500/$649mo, 3 modules $2,250/$899mo, 4 modules (full system) $3,000/$1,199mo. Annual is 10× monthly (two months free). 30 days' notice to cancel either direction.
- **Contracting**: DocuSign (`docusign_helper.py`) sends and tracks the service contract; a client's `contract_status` and `docusign_envelope_id` live on their `restaurants` row. An admin can fire a contract manually or it fires automatically on account creation.
- **Billing**: Stripe collects the setup fee at checkout and the recurring retainer afterward (`stripe_customer_id` on `restaurants`, `stripe_events_seen` for webhook idempotency).
- **Deployment**: Railway (`dashboard.cavnar.ai`), `gunicorn --workers 1 --threads 4 --timeout 120`. The marketing site (`cavnar.ai`) is a separately hand-deployed Cloudflare Worker — a push to `main` does **not** deploy it; Will runs `wrangler` himself.
- **Repo**: `xosimp/restaurant-review-automation` on GitHub, `main` branch is production.

## Architecture (one paragraph — see `SYSTEM_ARCHITECTURE.md` for the full picture)

A single Flask app (`hosted_dashboard.py`) with domain logic split across ~65 top-level modules and registered as Blueprints (`client_bp` web, `mobile_bp` iOS, `admin_bp`, `auth_bp`, plus one blueprint per POS/social integration). One SQLite database (`reviews.db`, on a Railway volume) is the single source of truth for both surfaces. `client_api.py` routes largely **delegate** to `mobile_api.py` implementations via the `_m(name)` helper, so web and iOS read the same computed payload rather than maintaining two versions of the same logic. A background scheduler (`scheduler.py`) runs the daily/weekly jobs (fetches, digests, alerts, backups) under a leased-claim model so one job never runs twice. The iOS app (`ios/CavnarAI`, SwiftUI) is a thin client over the same JSON APIs `mobile_api.py` serves.

## Modules (what the product actually does)

| Module | One-line job |
|---|---|
| Reviews | Pull Google/Yelp reviews, draft AI replies, alert on urgency, track response rate |
| Labor | Ingest POS shift CSVs, compute labor % vs target, generate and grade AI schedules (Shift Quality Engine), track Operational Score per employee |
| Food Cost (`inventory.py`, panel id `panel-inventory`) | Weekly counts, waste tracking, price drift alerts, purchase orders |
| Marketing | AI-drafted social posts, a content calendar, guest text-club campaigns |
| Intel | Competitor snapshots, AI-visibility ("do LLMs mention us") checks |
| Ask Cavnar | An in-app assistant with tool access to every module's live data — the "restaurant COO" |
| Admin | Will's own console: client health rollup, job runs, manual contract sends, sales-audit tool |
| Account | Profile, security (2FA, sessions, sign-in history), team access, billing, notifications |

Full detail on each: `MODULE_OVERVIEW.md`.

## Coding standards

- **Delegation over duplication**: a new `client_api.py` route should call `_m("mobile_x")` rather than reimplement the mobile handler's logic. When both surfaces need the same computed value, add it once in `mobile_api.py` / a shared module and delegate.
- **Four-plus-one touch points for a new `restaurants` column**: the dataclass field, `init_db()`/`ensure_columns()` migration, the `update_restaurant()` allowed-write whitelist, **and** `get_restaurant()` hydration. Missing any one makes the column silently unreadable or unwritable in production even though the schema has it.
- **No-data returns `None`, never `0`.** Every scoring/aggregation engine in this codebase (Shift Quality, Operational Score, Intel visibility, Labor's partial-period math) treats an absent measurement as "unknown," not "zero" or "failing." A `None` withdraws from a weighted average and renormalizes the rest; it never drags a score down.
- **Tenant scoping happens server-side**, never trusted from client input — every query filters by `restaurant_id` taken from the authenticated session/token, and cross-tenant access is a P0 finding whenever an audit finds a code path that skips it.
- **Propose, don't perform**, for anything with an outside effect. Ask Cavnar's tools are split into `read` (executes immediately), `action` (executes immediately, no confirmation — reserved for reversible/low-stakes calls), and `write` (returns a proposal card the owner must confirm before anything fires). A tool that emails a supplier or posts publicly is `write`.
- **Dashboard JS is ES5-only** (`var`/`function`, no backticks/const/let/arrow/async) — enforced by `tests/test_frontend_rules.py`, including inside comments. This is a compatibility constraint for the templated single-page dashboard, not a style preference.
- **Silent `except: pass` around a DB write is a lint failure** (`tests/test_silent_handler_lint.py`). Route unexpected errors to `ops.capture(e, job=..., context=...)` instead — errors must be observable somewhere, even when the code should keep going.
- **Typography is tokenized, not hand-picked per element**: Space Grotesk for every number (iOS and web, including numeric segments inside mixed text), Clash Display for headlines, Apfel Grotezk for chrome/UI text. Colors resolve through CSS custom properties (`--ink`, `--ink2`, `--ink3`, `--ember`, etc.) that redefine per theme — never a literal hex baked into a rule that only makes sense in one theme.

## Conventions specific to this codebase

- Employees are identified by **name**, not a foreign-key id — they originate from POS shift CSVs that carry no stable identifier. Every staffing feature (Operational Score, availability, capability ratings) keys off the name string, matched case-insensitively.
- A restaurant's demo/test account (currently Simple EJ's, Erik) is treated like any other tenant in the schema — no special-cased code path — so nothing breaks when it graduates from "test" to "real client, still testing."
- `_cached_shifts` and similar request-scoped caches live on Flask's `g`, not a module global, to stay correctly scoped per request under a multi-threaded gunicorn worker.
- Revert-check methodology for any bug fix: revert the fix in place, confirm the specific test that should catch the regression actually fails, then restore the fix. A test that still passes with the bug reintroduced is not protecting anything — this has caught several false-positive tests in this codebase's history (see `TESTING.md`).

## AI philosophy

Every AI-generated sentence a client sees is **grounded in a real number Cavnar AI actually measured** — never invented. Where a claim can't be verified against the data, it's marked `UNVERIFIED` rather than smoothed over (see the Labor AI insight's `$145 UNVERIFIED` pattern). AI is used to *interpret and phrase* what the deterministic layer computed, not to compute the numbers themselves — Shift Quality's score, Labor's percentages, and Intel's visibility range are all pure Python; only the prose wrapped around them touches a model. See `PROMPT_LIBRARY.md` for where and how each AI call is used, and the "AI providers" section of `CAVNAR_AI_ENGINEERING_GUIDE.md` for the provider/model map.

## Where to look next

This file is deliberately short. For anything deeper:
- **`CAVNAR_AI_ENGINEERING_GUIDE.md`** — the authoritative day-to-day reference; read this first for any non-trivial task.
- **`SYSTEM_ARCHITECTURE.md`** — folder structure, request flow, scheduling, notifications, auth.
- **`DATABASE_SCHEMA.md`** — every table.
- **`API_REFERENCE.md`** — every route.
- **`MODULE_OVERVIEW.md`** — one section per product module.
- **`PROMPT_LIBRARY.md`** — every AI call site.
- **`ROADMAP.md`** — what's open, what's next.
- **`TESTING.md`** — how to verify a change without re-running everything.
