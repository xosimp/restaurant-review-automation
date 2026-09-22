# Roadmap — Cavnar AI

A living list of what's open, what's next, and what's deliberately on hold. Update this file when a listed item ships or a new one is committed to — it should stay short enough to read in one pass, not become a second changelog.

## Active client

**Simple EJ's (Erik)** — first paying client, currently the primary test account (schema-identical to any other tenant; nothing special-cased). Still being refined on his real usage: labor thresholds per employee category (Erik to specify exact per-role targets); the RPOWER adapter is built and verified against the vendor's Postman collection — what remains is the static bearer token from Justin (paste in Admin → client → RPOWER token → Verify & save → Sync now) and the customer scope for guest matching; Back Office (Buyers Edge) needs sample exports from his team before the CSV import is built (`docs/plans/BACK_OFFICE_INTEGRATION.md`); the per-role task sheets he asked for are designed and waiting on his five answers (`docs/plans/TASK_SHEETS_PLAN.md`).

## Open / in progress

- **App Store readiness** (audit Sep 7–8 2026): privacy manifest and in-app account-deletion request are done. Still open: final App Store Connect listing copy, a demo-login reviewer account, TestFlight build upload.
- **Google OAuth verification**: three code-side issues already fixed; a Cloudflare bot-challenge in front of `cavnar.ai` still blocks Google's verification crawler — needs Will to adjust the Cloudflare rule (not a code fix).
- **Brand identity refresh**: new seal + wordmark shipped across iOS/web/favicons. Still open: refreshed email header logo and Open Graph share image.
- **Task sheets, phase 1** (`docs/plans/TASK_SHEETS_PLAN.md`): starts once Erik answers the open questions at the end of that plan.
- **Back Office CSV import** (`docs/plans/BACK_OFFICE_INTEGRATION.md`): starts once his team sends one export each of items, a count, a recipe and a week of invoices.
- **Repository cleanup, tiers 4–6** (from the Sep 21 architecture audit): the four assets that need an external check before removal, then the architecture items — DDL off request paths, the 34 hand-duplicated web/mobile route pairs, one config module, one model registry, the bound-import migration, the demo-seed and engine extractions.
- **Apollo.io upgrade**: on hold pending the Gia Mia follow-up outcome — upgrade only if that deal falls through, not proactively.

## Recently shipped (most recent first — trim entries older than ~2 months)

- Sep 21: repository audit and cleanup tiers 0–3 — the monthly summary emailing daily (fixed), the Reviews diagnosis card that never rendered, the iOS retry-post route, the shadowed robots.txt, one job_cursors schema, the 3-star whitelist; then 111 unused imports, 17 dead names, 15 dead JS functions, 88 dead CSS rules, 7 unused Swift types, a 10 MB portrait, the unused DocuSign package; then six duplicated helper families folded into one definition each; then every reference doc corrected against the code.
- Sep 21: the Intelligence Engine (`intelligence/`, three learning levels, MIN_COHORT privacy floor, admin Intelligence page, confidence on every Home recommendation); marketing intelligence (post tags, per-dish lift, beyond-sales attribution, the weakest-post read); RPOWER as a full `pos.py` provider (menu discovery, item sales, guest matching via `pos.supports`); Home reordered on web and iOS.
- Sep 19–20: the moat audit's top 25 (decision records, automation with undo windows, trust switches, time off, covers, recipe drafts, the AI-visibility queries); the retention, automation and workflow audits; durable login throttling, account freeze, the support role, encrypted credentials at rest, the restore drill.
- Sep 10–18: Account settings buildout (Face ID lock, sign-in history, 2FA backup codes, team invite/manage/re-role, data export, close-account flow, test digest, marketing opt-out, timezone, Help/FAQ on both platforms); the staff portal with PIN identity; APNs push delivery working in production.
- Ask Cavnar: dynamic opening briefing (no model call), Shift Quality + team roster + alerts exposed to the assistant, durable cross-conversation memory (`remember`/`forget`), sibling-location awareness, data-freshness labeling, role-scoped answers and the cross-module business snapshot.
- Labor module visual pass: removed redundant header text, fixed header-wrap alignment (word-spacing over margin, so a wrap never indents the status badge), widened the squeezed title column, moved schedule/upload actions to sit with the Schedule section, added a colored labor % badge, enlarged body text below the AI insight box, dark-mode small text realigned to iOS's sand tone.
- Shift Quality Engine: built out fully (11 dimensions, confidence tracking, what-if swap evaluation, per-restaurant editable weights), audited twice, all findings from both passes closed.
- Simple EJ's set up as the live test/demo account; manual + automatic contract-send confirmed working.
- Sales-audit tool built for in-person pitches (`/admin/audits`).
- DocuSign contract flow went live in production; full 1–4 module pricing ladder consolidated into `pricing.py` as the single source Stripe/DocuSign/the website all read.
- `cavnar.ai` deploy bug fixed (was serving the source tree, never actually redeploying on push) and the reviews database moved onto the Railway persistent volume.

## Deliberately not doing (yet)

- Self-serve account deletion — service is contract-based (DocuSign), so cancellation goes through Will, not a button.
- A second LLM provider for anything but AI-visibility checks (Perplexity stays scoped to that one job).
- A schema migration/version-table system — additive `ALTER TABLE` has been sufficient at current scale; revisit only if a destructive schema change becomes unavoidable.

## Reference docs this roadmap assumes you've read

The index in `CLAUDE.md`.
