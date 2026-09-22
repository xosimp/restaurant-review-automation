# Project Context — Cavnar AI

## Business

Cavnar AI is a restaurant intelligence SaaS built and operated by Will Cavnar. It gives independent restaurant owners a single dashboard (web + iOS) that reads their POS, review, and inventory data and tells them what to do about it — not a BI tool that shows charts, a decision layer that names the problem, the day it happened, the dollar cost, and the fix.

- **First paying client**: Erik, owner of Simple EJ's. His three stated pain points — labor "through the roof," weak repeat-visit rate, and training — are the product's proof case.
- **Pricing** (`pricing.py`, mirrored on `pricing.html`): per module, one-time setup at checkout + a monthly or annual retainer starting 30 days after setup. 1 module $750/$349mo, 2 modules $1,500/$649mo, 3 modules $2,250/$899mo, 4 modules (full system) $3,000/$1,199mo. Annual is 10× monthly (two months free). 30 days' notice to cancel either direction.
- **Contracting**: DocuSign (`docusign_helper.py`) sends and tracks the service contract; a client's `contract_status` and `docusign_envelope_id` live on their `restaurants` row. An admin can fire a contract manually or it fires automatically on account creation.
- **Billing**: Stripe collects the setup fee at checkout and the recurring retainer afterward (`stripe_customer_id` on `restaurants`, `stripe_events_seen` for webhook idempotency).
- **Deployment**: Railway (`dashboard.cavnar.ai`), `gunicorn --workers 1 --threads 4 --timeout 120` from `railway.json`. The marketing site (`cavnar.ai`) is a separately hand-deployed Cloudflare Worker — a push to `main` does **not** deploy it; Will runs `wrangler` himself.
- **Erik's other systems**: RPOWER (POS; adapter built, token pending), Back Office by Buyers Edge (inventory and invoices; research note in `docs/plans/`), Hostie, Fourth, Tock, Control Play, Fishbowl.
- **Repo**: `xosimp/restaurant-review-automation` on GitHub, `main` branch is production.

## Architecture (one paragraph — see `SYSTEM_ARCHITECTURE.md` for the full picture)

A single Flask app (`hosted_dashboard.py`) with domain logic split across about 115 top-level modules plus the `intelligence/` package and registered as Blueprints (`client_bp` web, `mobile_bp` iOS, `admin_bp`, `auth_bp`, plus one blueprint per POS/social integration). One SQLite database (`reviews.db`, on a Railway volume) is the single source of truth for both surfaces. `client_api.py` routes largely **delegate** to `mobile_api.py` implementations via the `_m(name)` helper, so web and iOS read the same computed payload rather than maintaining two versions of the same logic. A background scheduler (`scheduler.py`) runs the daily/weekly jobs (fetches, digests, alerts, backups) under a leased-claim model so one job never runs twice. The iOS app (`ios/CavnarAI`, SwiftUI) is a thin client over the same JSON APIs `mobile_api.py` serves.

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
| Account | Profile, security (2FA, sessions, sign-in history), team access and grants, automation & trust, billing, notifications |
| Staff portal | PIN sign-in for employees: today's shifts, tasks, availability, time off, the pre-shift read |
| Intelligence engine | `intelligence/`: what the platform learns across restaurants, under a cohort floor of five, and the confidence it puts on each recommendation |

Full detail on each: `MODULE_OVERVIEW.md`.

## Coding standards and conventions

They live in one place: `CLAUDE.md` (the standing rules) and `CAVNAR_AI_ENGINEERING_GUIDE.md` §3–§7 (data model, API patterns, coding standards, UI). This file used to restate them and drifted; it does not any more.

## AI philosophy

Every AI-generated sentence a client sees is **grounded in a real number Cavnar AI actually measured** — never invented. Where a claim can't be verified against the data, it's marked `UNVERIFIED` rather than smoothed over (see the Labor AI insight's `$145 UNVERIFIED` pattern). AI is used to *interpret and phrase* what the deterministic layer computed, not to compute the numbers themselves — Shift Quality's score, Labor's percentages, and Intel's visibility range are all pure Python; only the prose wrapped around them touches a model. See `PROMPT_LIBRARY.md` for where and how each AI call is used, and the "AI providers" section of `CAVNAR_AI_ENGINEERING_GUIDE.md` for the provider/model map.

## Where to look next

This file is deliberately short. The index of every reference document, with what each covers, is the first table in `CLAUDE.md`.
