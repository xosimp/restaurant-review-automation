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
Model: Haiku. Input: one review's text + rating. Output: sentiment (positive/neutral/negative), category list, one-line summary, urgency (high/normal). Deterministic-feeling by design — same review text should score the same way; the prompt does not ask for creative variation.

### Review reply drafting (`drafter.py`)
Model: Sonnet (`DRAFTER_MODEL`). Input: the review + the restaurant's brand-voice fields (`voice_notes`, `sign_off_name`, `never_say`) + recent reply history (avoid repeating the same phrasing). Output: one draft reply. **Never** commits the restaurant to anything not already true (no invented refunds/comps/promises) — this is a hand-reviewed invariant, not an automated guard, so a reviewer of drafter changes should re-check it manually.

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
