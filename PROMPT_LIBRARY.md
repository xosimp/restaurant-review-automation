# Prompt Library — Cavnar AI

Every place a model call happens: which provider, which model, what it's asked to do, and what it's forbidden from doing. All calls route through `ai_utils.create_with_retry()` (budget check, retry with backoff, usage logging to `ai_usage`) — nothing calls the Anthropic/Perplexity SDK directly.

## Providers

| Provider | Used for | Default model | Env override |
|---|---|---|---|
| Anthropic (Claude) | everything except AI-visibility checks | `claude-haiku-4-5-20251001` (cheap, high-volume: analysis, drafting) or `claude-sonnet-5` (Ask Cavnar, marketing drafting — reasoning/quality-sensitive) | `CLAUDE_MODEL`, `ASK_CAVNAR_MODEL`, `DRAFTER_MODEL` |
| Perplexity | Intel's AI-visibility check only | `sonar` | `AI_VISIBILITY_MODEL` |

No OpenAI usage anywhere in this codebase.

## Call sites

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
Model: Haiku. Input: the period's aggregated labor numbers (by day, by role) — never raw shift rows. Output: 2–4 sentences plus a short recommendations list. Every dollar/percentage figure named must trace back to a number `labor.py` actually computed; where the model states something it can't verify against the passed-in numbers, the response is expected to mark it `UNVERIFIED` rather than assert it plainly (see the real example: *"about $145 over target each day... UNVERIFIED: $145"* when the exact dollar figure wasn't in the aggregate passed to it).

### Shift Quality reasoning ("Why this schedule?")
Model: Haiku/Sonnet (context-dependent). Input: the finished schedule + its `shift_quality.py` evaluation (dimension scores, strengths, weaknesses). Output: plain-English explanation. Cannot name a person or a shift that isn't actually in the generated week — the evaluation it's explaining is itself deterministic Python, so the model is narrating a computed result, not computing one.

### Competitor analysis (`competitor.py`)
Model: Haiku (`CLAUDE_MODEL`). Input: this restaurant's and nearby competitors' Google Places data (rating, review count, category). Output: a short comparative read. Never fabricates a competitor that wasn't actually returned by the Places API — see `project_gia_mia_places_testing` in memory: this codebase has previously fabricated competitor data by accident, and the fix was to make the prompt strictly grounded in the fetched snapshot rows.

### AI-visibility check (`client_api.py`'s `AIVIS_MODEL`, via Perplexity)
Model: `sonar`. Input: a fixed panel of realistic customer-style queries for the restaurant's cuisine/neighborhood. Output, per query: whether the restaurant was mentioned (`appeared`) and whether the query itself got answered at all (`answered`) — these are recorded separately so "the model didn't answer" is never conflated with "the model answered without mentioning us." Rate-limited server-side (see `ai_utils`'s comment: an earlier bug fired nine sonar queries a minute per restaurant, unmetered — fixed with staggered submission + logging).

### Marketing draft (`marketing.py`, `guest_marketing.py`)
Model: Sonnet. Input: brand-voice fields + what's being promoted (a dish, an event, a general "keep guests coming back" prompt) + recent post history (avoid repetition). Output: one social post draft, always presented for the owner to edit/approve — never auto-published without a human step for organic content.

### Ask Cavnar (`ask_cavnar.py`, `ask_cavnar_tools.py`)
Model: Sonnet (`ASK_CAVNAR_MODEL`), tool-calling enabled (`ask_with_tools`), `max_tokens` capped at `_MAX_TOKENS_WITH_TOOLS` (1200). Input: the full context snapshot (`build_context()` — identity, sibling locations, alerts, memory, per-module data the tier grants) + the filtered tool list for this restaurant's modules + conversation history (sanitized via `_sanitize_history`). System framing: an AI restaurant COO with direct data access, not a generic chatbot — encouraged to call tools rather than guess, required to mark anything it can't verify, and required to route any outside-effect action through a `write`-kind tool's proposal rather than claiming to have done it. See `MODULE_OVERVIEW.md`'s Ask Cavnar section for the tool-kind contract.

### Sales-audit notes (`sales_audit_notes_ai.py`, `sales_audit_cheatsheet.py`)
Model: Haiku/Sonnet. Input: what Will observed during an in-person audit visit. Output: structured notes / a cheat-sheet for the pitch. Internal tool, not client-facing — lower stakes on hallucination but still grounded in what was actually entered.

## Guardrails that apply to every call site

- **Budget enforced before the call fires** (`ai_utils.ai_budget_exceeded` / `global_monthly_budget`) — both a global monthly ceiling and a per-restaurant check for unpaid/trial accounts.
- **Retry with backoff, capped** (`create_with_retry`, default 2 retries, 1.5× backoff) — a transient API failure doesn't become a user-facing error immediately, but it also doesn't retry forever.
- **`temperature` is never passed** — the production Anthropic SDK build in use rejects it; `create_with_retry` strips it if present rather than letting the call 400.
- **Every call is logged** (`log_ai_usage` → `ai_usage` table: model, action, input/output tokens, estimated cost) — this is what the admin usage dashboard and the per-restaurant budget check both read.
- **Rate limiting** where a feature could be hammered (`ai_rate_limited`, keyed per restaurant+feature — e.g. Ask Cavnar at 5/min).
