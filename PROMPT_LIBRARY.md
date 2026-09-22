# Prompt Library — Cavnar AI

Every place a model call happens: which provider, which model, what it's asked to do, and what it's forbidden from doing. Every Anthropic call routes through `ai_utils.create_with_retry()` (budget check, retry with backoff, usage logging to `ai_usage`) — nothing calls `messages.create` directly. Perplexity is not an SDK call: the AI-visibility check posts to its REST endpoint with `requests` (`client_api.py`, `_do_ai_visibility_inner`), metered through the same `ai_usage` ledger.

The model for each call comes from one registry, `ai_utils.MODELS` (purpose → env override, default), read through `ai_utils.model_for(purpose)`; the client comes from `ai_utils.get_client()`, built on first use rather than at import. The table below is that registry; when a row changes, change both.

## Providers

| Provider | Used for | Default model | Env override |
|---|---|---|---|
| Anthropic (Claude) | everything except AI-visibility checks | `claude-haiku-4-5-20251001` for the high-volume classifiers (review analysis, competitor menu extraction, email personalisation, marketing insight); `claude-sonnet-5` for everything an owner reads as advice (insights, diagnoses, drafts, schedules, Ask Cavnar); `claude-opus-5` for invoice transcription only | per call site — see the table under *Call sites* |
| Perplexity | Intel's AI-visibility check only | `sonar` | `AI_VISIBILITY_MODEL` |

No OpenAI usage anywhere in this codebase.

## Call sites

| Call | Module | Default model | Env override |
|---|---|---|---|
| Review analysis | `analyser.py` | Haiku | `REVIEW_ANALYSIS_MODEL` |
| Review root-cause diagnosis | `review_intelligence.py` | Sonnet | `CLAUDE_REPORTER_MODEL` |
| Review reply drafting | `drafter.py` | Sonnet | `DRAFTER_MODEL` |
| Review insight (the consultant read) | `client_api.py` `_do_review_insight` | Sonnet | `REVIEW_INSIGHT_MODEL` |
| Food cost insight | `inventory.get_claude_insights` | Sonnet | `INVENTORY_INSIGHT_MODEL` |
| Food cost root-cause diagnosis | `food_cost_intelligence.py` | Sonnet | `CLAUDE_REPORTER_MODEL` |
| Labor insight | `labor.py` (`get_claude_insights`) | Sonnet | `LABOR_INSIGHT_MODEL` |
| Schedule generation | `labor.py` (`generate_schedule`) | Sonnet | `SCHEDULE_MODEL` |
| Weekly plan (the Monday three actions) | `strategy_jobs.py` via `ask_with_tools` | Sonnet | `ASK_CAVNAR_MODEL` |
| Competitor menu extraction (×2) | `competitor.py:119,174` | Haiku | `CLAUDE_MODEL` |
| Weekly competitor insight | `competitor.py:780` | Sonnet | `CLAUDE_REPORTER_MODEL` |
| Marketing post draft, calendar ideas | `marketing.py:452,693` | Sonnet | `MARKETING_MODEL` |
| Marketing insight | `client_api.py` `_do_mkt_insight` | Haiku | `CLAUDE_MODEL` |
| Guest campaign copy | `guest_marketing.py` | Sonnet | `GUEST_MARKETING_MODEL` |
| Recipe drafts, recipe scan (OCR) | `recipes.py` | Sonnet | `RECIPE_MODEL` |
| Invoice extraction | `invoices.py` | Opus | `INVOICE_MODEL` |
| Digest narrative | `reporter.py` | Sonnet | `CLAUDE_REPORTER_MODEL` |
| Onboarding email personalisation | `emails.py:157` | Haiku | `CLAUDE_MODEL` |
| Sales-audit notes | `sales_audit_notes_ai.py` | Sonnet | `SALES_AUDIT_NOTES_MODEL` |
| Ask Cavnar | `ask_cavnar.py` | Sonnet | `ASK_CAVNAR_MODEL` |
| AI visibility | `client_api.py` `AIVIS_MODEL` (Perplexity REST) | `sonar` | `AI_VISIBILITY_MODEL` |

`CLAUDE_MODEL` and `CLAUDE_REPORTER_MODEL` are each read by several purposes; `REVIEW_ANALYSIS_MODEL` and `SALES_AUDIT_NOTES_MODEL` are new names for the two sites that had no override (inheriting `CLAUDE_MODEL` would have changed them wherever it is set). Shift Quality has no model call: its "why this schedule" text is the deterministic evaluation rendered as prose. `sales_audit_cheatsheet.py` has none either.

### Review analysis (`analyser.py`)
Model: Haiku. Input: one review's text + rating. Output: sentiment (positive/neutral/negative), category list, one-line summary, urgency (high/normal), **severity tier** (safety/legal/operational/service/minor), **`specific_complaint`** (≤8 words) and **`entities`** (dishes, staff roles, daypart, service mode). Deterministic-feeling by design — same review text should score the same way; the prompt does not ask for creative variation.

Every structured field is validated against an enum and dropped when it misses (`_validate_entities`, `_severity_floor`), for the same reason `categories` always was: an un-enumerated value is a cluster of one that no trend query will ever group with anything, and it would be shown to the owner as if it were one of ours. The entity fields carry explicit EXTRACTION RULES forbidding inference — a dish may not be inferred from a category, a role from a complaint, or a daypart from a rating. An absent field means the review did not say; it is never a gap to fill. `_severity_floor` also stops the tier contradicting the urgency that already fired on the same row.

### Review root-cause diagnosis (`review_intelligence.py`)
Model: Sonnet (`CLAUDE_REPORTER_MODEL`). Runs on a schedule (`scheduler.run_review_diagnoses`, 6am daily), never on a page load. Input: one complaint cluster (its reviews, their concentrations by dish/role/daypart/weekday, the guests' own complaint phrases) + what the other modules recorded over the same period + a fixed CAUSE VOCABULARY. Output: a likely operational cause, an alternative explanation, what would tell them apart, an action, an expected outcome, a confidence, and the review ids it rests on.

This is the module's answer to "why", and its guards are the point of it: `_validate_diagnosis` rejects any citation that was not in the prompt (the same discipline `verify_figures` applies to numbers — a root-cause paragraph is only worth more than a summary because the owner can click through to the reviews behind it), an unsourced figure flags the result and forces confidence to `low`, and an empty cross-module block tells the model in so many words that it has no operational evidence and must cap its confidence. The prompt requires an alternative cause and a tiebreaker on every answer, because a single confident cause with nothing to weigh it against is the shape of a plausible guess.

### Review reply drafting (`drafter.py`)
Model: Sonnet (`DRAFTER_MODEL`). Input: the review + the restaurant's brand-voice fields (`voice_notes`, `sign_off_name`, `never_say`) + recent reply history (avoid repeating the same phrasing). Output: one draft reply. **Never** commits the restaurant to anything not already true (no invented refunds/comps/promises) — this is a hand-reviewed invariant, not an automated guard, so a reviewer of drafter changes should re-check it manually.

### Food cost insight (`inventory.get_claude_insights`)
Model: Sonnet (`INVENTORY_INSIGHT_MODEL`). Framed as a restaurant CFO, not a waste consultant, and handed all five engines: `analyse_inventory`'s waste/overstock/reorder, `cogs.build_food_cost_pct` (food cost % against the restaurant's own target), `food_cost_intelligence`'s ranked cost drivers, the recipe-coverage and counted-vs-inferred trust block, the weekday and year-over-year patterns, what labor/reviews/marketing recorded over the same period, and the stored root-cause read.

The guards are the point of it: `_supported_savings_block` pre-computes every dollar the model may quote, the drivers arrive already ranked and the prompt is told not to re-rank them, a cause may only come from the ROOT-CAUSE READ block, a figure marked not-computable may not be estimated, and `verify_figures` appends an `UNVERIFIED:` marker rendered as a distinct caveat on both clients. The honest-zero path is real: *"If the data does not support a genuine, specific opportunity, say so plainly and write no recommendations at all."*

### Food cost root-cause diagnosis (`food_cost_intelligence.py`)
Model: Sonnet (`CLAUDE_REPORTER_MODEL`). Scheduled (`scheduler.run_food_cost_diagnoses`, 06:00), never on a page load. Input: the ranked drivers with the dollars each carries, the food cost position, the prime-cost projection, the trust block, the weekday/seasonal patterns, the cross-module context, and a fixed CAUSE VOCABULARY. Output: a headline, a cause naming at least one given driver, an alternative explanation, what would tell them apart, a confidence, an action and an expected outcome.

`_validate_diagnosis` rejects a cause naming a driver it was not handed — the same discipline `verify_figures` applies to numbers — and an unsourced figure flags the result and forces confidence to `low`. An empty cross-module block tells the model in so many words that it has no operational evidence and must cap its confidence, because an empty block otherwise reads as "nothing notable happened" rather than "we have no data".

### Labor AI insight (`labor.py` → Claude via `ai_utils`)
Model: Sonnet (`LABOR_INSIGHT_MODEL`). Input: the period's aggregated labor numbers (by day, by role) — never raw shift rows. Output: 2–4 sentences plus a short recommendations list. Every dollar/percentage figure named must trace back to a number `labor.py` actually computed; where the model states something it can't verify against the passed-in numbers, the response is expected to mark it `UNVERIFIED` rather than assert it plainly (see the real example: *"about $145 over target each day... UNVERIFIED: $145"* when the exact dollar figure wasn't in the aggregate passed to it).

### Schedule generation (`labor.generate_schedule`)
Model: Sonnet (`model_for("schedule")`), `max_tokens` 16000, thinking off. Input: the roster grouped by role (active names only, from `staff_settings.roster`), availability and notes, approved and pending time off, the compliance rules and role floors (`schedule_rules.prompt_block`), the owner's dated events and reservations (`demand_signals.prompt_block`), pairings, reliability, what the manager keeps changing (`schedule_versions.prompt_block`), the demand profile and the hours budget (a ceiling, backed by a weekly revenue figure from the restaurant's own median week when it has one), sales per labor hour by daypart, what published weeks actually did by daypart, the rotation ledger, holiday lift measured here, stated and learned staff preferences, who could hold a station, the cohort's hours-per-$1k ratio, and a jurisdiction pack's rules. Output: `output_config` with `labor.SCHEDULE_SCHEMA` (rows plus a short `narrative`), with a CSV fallback when structured output is unavailable. Rosters over `schedule_engine.CHUNK_ROSTER_THRESHOLD`, or a response that stopped on `max_tokens`, are generated in two or three date slices and merged. `schedule_engine._run_schedule_job` then repairs, backstops, sweeps the rules, fixes and grades the week. The "what changed" text the owner reads is `schedule_versions.diff` against the last published week — deterministic; the model's own note is shown separately and never carries a figure the diff does not.

### Competitor analysis (`competitor.py`)
Model: Haiku (`CLAUDE_MODEL`) for the two menu-extraction calls (a competitor's menu page or PDF into structured items); Sonnet (`CLAUDE_REPORTER_MODEL`) for the weekly comparative read. Input: this restaurant's and nearby competitors' Google Places data (rating, review count, category) and the extracted menus. Output: a short comparative read. Never fabricates a competitor that wasn't actually returned by the Places API — see `project_gia_mia_places_testing` in memory: this codebase has previously fabricated competitor data by accident, and the fix was to make the prompt strictly grounded in the fetched snapshot rows.

### AI-visibility check (`client_api.py`'s `AIVIS_MODEL`, via Perplexity)
Model: `sonar`. Input: a fixed panel of realistic customer-style queries for the restaurant's cuisine/neighborhood. Output, per query: whether the restaurant was mentioned (`appeared`) and whether the query itself got answered at all (`answered`) — these are recorded separately so "the model didn't answer" is never conflated with "the model answered without mentioning us." Rate-limited server-side (see `ai_utils`'s comment: an earlier bug fired nine sonar queries a minute per restaurant, unmetered — fixed with staggered submission + logging).

### Marketing draft (`marketing.py`, `guest_marketing.py`)
Model: Sonnet (`MARKETING_MODEL`, `GUEST_MARKETING_MODEL`); the marketing *insight* card (`client_api._do_mkt_insight`) is Haiku (`CLAUDE_MODEL`). Input: brand-voice fields + what's being promoted (a dish, an event, a general "keep guests coming back" prompt) + recent post history (avoid repetition). Output: one social post draft, always presented for the owner to edit/approve — never auto-published without a human step for organic content.

### Ask Cavnar (`ask_cavnar.py`, `ask_cavnar_tools.py`)
Model: Sonnet (`ASK_CAVNAR_MODEL`), tool-calling enabled (`ask_with_tools`), `max_tokens` from the depth contract `_MAX_TOKENS` — brief 400 / standard 1200 / executive 4000, chosen from the question by `_depth_for`. The system prompt is a LIST of content blocks: static rules with a `cache_control` breakpoint, then the live snapshot (never interpolate per-restaurant data into the static block, or the cache stops hitting for everyone). Input: the full context snapshot (`build_context()` — identity, sibling locations, alerts, memory, per-module data the tier grants) + the filtered tool list for this restaurant's modules + conversation history (sanitized via `_sanitize_history`). System framing: an AI restaurant COO with direct data access, not a generic chatbot — encouraged to call tools rather than guess, required to mark anything it can't verify, and required to route any outside-effect action through a `write`-kind tool's proposal rather than claiming to have done it. See `MODULE_OVERVIEW.md`'s Ask Cavnar section for the tool-kind contract.

### Sales-audit notes (`sales_audit_notes_ai.py`)
Model: Sonnet (`SALES_AUDIT_NOTES_MODEL`; `sales_audit_cheatsheet.py` is deterministic and makes no call). Input: what Will observed during an in-person audit visit. Output: structured notes / a cheat-sheet for the pitch. Internal tool, not client-facing — lower stakes on hallucination but still grounded in what was actually entered.

### Invoice extraction (`invoices.py`)
Model: Opus (`INVOICE_MODEL`, default `claude-opus-5`) — the one call site where a misread digit flows straight into every plate cost, so it uses the most capable model; weekly, low volume. Input: one supplier invoice as an `image` block (JPEG/PNG/WebP/GIF) or a `document` block (PDF), ≤4.5 MB. Output: structured JSON via `output_config.format` (`json_schema`: supplier, invoice_date, invoice_total, lines[description, quantity, unit, unit_price, line_total], every numeric field nullable). The prompt forbids estimating a missing number (null instead) and skips non-product lines. **The model only transcribes.** Matching to ingredients, unit conversion, the qty×price=total check, the >40% "unit mix-up" guard and the lines-vs-total check are all Python (`invoices.propose`), and nothing is written until the owner confirms each line (`invoices.apply`, once per import). `stop_reason` `refusal`/`max_tokens` become owner-facing errors. Same file twice (sha256) is never read twice.

### Recipe drafts and recipe scan (`recipes.py`)
Model: Sonnet (`RECIPE_MODEL`). Two calls: a draft recipe (ingredients and quantities) for a dish that has none, accepted line by line by the owner; and OCR of a photographed recipe card into the same shape. Nothing is written until the owner accepts.

### Digest narrative and onboarding emails (`reporter.py`, `emails.py`)
The weekly digest's narrative paragraph is Sonnet (`CLAUDE_REPORTER_MODEL`), grounded in the digest's own figures and checked by `ai_guard`; the day-2/7/30 onboarding emails personalise one paragraph with Haiku (`CLAUDE_MODEL`).

### The weekly plan (`strategy_jobs.run_weekly_plan`)
Monday 7am local: Ask Cavnar's own `ask_with_tools` (Sonnet) files the week's three actions as issues, reading the same snapshot and tools an owner's question would.

## Guardrails that apply to every call site

- **Budget enforced before the call fires** (`ai_utils.ai_budget_exceeded` / `global_monthly_budget`) — both a global monthly ceiling and a per-restaurant check for unpaid/trial accounts.
- **Retry with backoff, capped** (`create_with_retry`, default 2 retries, 1.5× backoff) — a transient API failure doesn't become a user-facing error immediately, but it also doesn't retry forever.
- **`temperature` is never passed** — the production Anthropic SDK build in use rejects it; `create_with_retry` strips it if present rather than letting the call 400.
- **Every call is logged** (`log_ai_usage` → `ai_usage` table: model, action, input/output tokens, estimated cost) — this is what the admin usage dashboard and the per-restaurant budget check both read.
- **Rate limiting** where a feature could be hammered (`ai_rate_limited`, keyed per restaurant+feature — and per USER for Ask Cavnar at 5/min, so an owner and an invited teammate don't throttle each other).
