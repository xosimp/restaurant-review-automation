# Roadmap — Cavnar AI

A living list of what's open, what's next, and what's deliberately on hold. Update this file when a listed item ships or a new one is committed to — it should stay short enough to read in one pass, not become a second changelog.

## Active client

**Simple EJ's (Erik)** — first paying client, currently the primary test account (schema-identical to any other tenant; nothing special-cased). Onboarding areas still being refined based on his real usage: labor thresholds per employee category (Erik to specify exact per-role targets), RPower POS connection (merchant ID + rep contact pending), Back Office by Buyers Edge Platform sync (confirming exact data export Erik's team can provide).

## Open / in progress

- **App Store readiness** (audit Sep 7–8 2026): privacy manifest and in-app account-deletion request are done. Still open: final App Store Connect listing copy, a demo-login reviewer account, TestFlight build upload.
- **Google OAuth verification**: three code-side issues already fixed; a Cloudflare bot-challenge in front of `cavnar.ai` still blocks Google's verification crawler — needs Will to adjust the Cloudflare rule (not a code fix).
- **Brand identity refresh**: new seal + wordmark shipped across iOS/web/favicons. Still open: refreshed email header logo and Open Graph share image.
- **Account settings buildout** (11-item plan, in progress): Face ID lock toggle, sign-in history view, 2FA backup codes, team invite/manage, data export, delete-account informational flow, test-digest send, marketing opt-out, timezone picker, and a Help/FAQ surface on both iOS and web. Build order and schema in the working plan; phases run with tests + color lint after each, not only at the end.
- **Apollo.io upgrade**: on hold pending the Gia Mia follow-up outcome — upgrade only if that deal falls through, not proactively.

## Recently shipped (most recent first — trim entries older than ~2 months)

- Ask Cavnar: dynamic opening briefing (no model call), Shift Quality + team roster + alerts exposed to the assistant, durable cross-conversation memory (`remember`/`forget`), sibling-location awareness, data-freshness labeling.
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

`PROJECT_CONTEXT.md`, `CAVNAR_AI_ENGINEERING_GUIDE.md`, `MODULE_OVERVIEW.md`.
