# Restaurant Intelligence Engine

How Cavnar AI gets smarter every week as more restaurants use it — without
one restaurant ever seeing another's data. Written before implementation
(Sep 2026) and kept current with it. The code lives in `intelligence/`.

## The three rules everything below obeys

1. **A restaurant's own data only ever serves that restaurant** (Level 1).
2. **Cross-restaurant learning reads one table** — `intel_features`, which
   holds only ratios, rates and counts per restaurant-week — and every
   figure that leaves it is an aggregate over a cohort of at least
   `MIN_COHORT` (5) restaurants. Below that the answer is "not enough
   similar restaurants", never a number. Demo accounts are never in a
   cohort, a platform rate or the MIN_COHORT count: `jobs.seeded_restaurant_ids`
   (`is_demo`, or de-flagged under `SEEDED_HISTORY_DAYS` ago) is left out by
   `active_restaurants`, `features.latest_by_restaurant`/`weekly_by_restaurant`,
   `scoring.kind_stats` and the confidence log (CA3 F7).
3. **Nothing is generated to fill a gap.** A pattern exists only when a
   permutation test and a false-discovery correction say so; a benchmark
   only when the cohort is large enough; a confidence score only from
   factors that were measured.

## Architecture

```
                 ┌──────────────── Level 1: restaurant memory ───────────────┐
  own tables ──► │ features.py  (one row per restaurant-week, ratios only)   │
  (reviews,      │ memory.py    (busiest days, seasonality, own rec record)  │
   labor,        │ feedback.py  (every recommendation → presented/answered/  │
   waste,        │               measured, from tables that already exist)  │
   campaigns,    └────────────────────────────┬──────────────────────────────┘
   outcomes,                                  │ intel_features / intel_rec_events
   dismissals)                                ▼
                 ┌──────── Level 2 + 3: platform and cohort intelligence ───┐
                 │ categories.py  (which cohort a restaurant compares to)   │
                 │ patterns.py    (hypotheses → permutation test → BH → row)│
                 │ benchmarks.py  (p25/p50/p75 per cohort per metric)       │
                 │ trends.py      (weekly slope per cohort per metric)      │
                 │ scoring.py     (acceptance and success by rec kind)      │
                 │ confidence.py  (seven factors → score, band, caution)    │
                 └────────────────────────────┬──────────────────────────────┘
                                              ▼
        Ask (tools + context)   Home (confidence on each recommendation)
        Admin → Intelligence    Future modules via intelligence.* facade
```

Every module is a plain Python file with pure functions over `db_path`,
independently testable, and `intelligence/__init__.py` is the only import
other modules need.

## Database changes (all in `models.init_db`)

| Table / column | Holds | Privacy |
|---|---|---|
| `restaurants.category` | the owner's or admin's category (taxonomy in `categories.py`); inferred from name/vibe/menu when unset and labelled so | own row |
| `intel_features` | one row per restaurant-week: `features_json` of ratios, rates and counts; `completeness` 0–1 | no names, no dollars, no people; tenant-keyed |
| `intel_rec_events` | one row per recommendation event: kind, action (presented / done / not for us / hidden / snoozed / accepted / tracking / implemented / measured / confirmed / dismissed / auto / ignored), outcome (improved / worsened / no clear change / unknown), days to effect, confidence at the time | kind is a key prefix, never text |
| `intel_patterns` | discovered patterns: cohort, behaviour, outcome, n with / n without, effect, p, q, confidence, sentence, status | counts and effects only |
| `intel_benchmarks` | per cohort × metric × week: n, p25, p50, p75, mean | aggregates over ≥ MIN_COHORT |
| `intel_confidence_log` | per week × cohort × kind: mean confidence, acceptance, success | aggregates |
| `job_cursors['intelligence_features']` | where the nightly feature pass stopped | — |

No existing table changes shape. Feedback is **derived** by a sync, so no
write path changes; new callers may record events directly through
`feedback.record`. Since the ROI audit (Sep 2026) the sync's main input is
**rec_ledger** (`rec_events`), which every surface writes — Home, the brief,
Reviews, Food, Marketing, Intel, the DSR, the schedule, the queue, alerts
and issues — read forward from a cursor
(`job_cursors['intelligence_feedback_ledger']`), bounded per pass. The four
older tables are still read for what predates the ledger:
`home_dismissals`, `recommendation_outcomes` (the source of every measured
verdict and its days to effect), `ask_cavnar_actions` and
`delayed_actions`. Nothing is counted twice: the same answer reaches the
same (restaurant, key, action) row from either path and UNIQUE keeps one;
the ledger's Ask keys and outcome copies are skipped (their sources are
read directly). Ledger mapping: dismissed → `not_for_us` | `hidden`;
completed → `done`; accepted → `tracking` (a tracker named) | `accepted`;
implemented → `implemented`; snoozed → `snoozed` (a "Not today" is not a
no — it used to be learned as `hidden`, and old rows are repaired);
expired → `ignored`. Ask answers are keyed `ask:<proposal id>`, the identity
Ask, the queue, decisions and the ledger share (legacy
`ask:<action>:<summary>` rows are re-keyed).

## Services (the `intelligence/` package)

| Module | Level | Responsibility |
|---|---|---|
| `privacy.py` | all | `MIN_COHORT`, the forbidden-key list, `assert_anonymous()` (recursive; used by tests and by the admin payload builder), effect rounding |
| `stats.py` | all | pure-Python mean / sd / Cohen's d / percentiles / least-squares slope / **permutation test** (seeded, two-sided) / **Benjamini–Hochberg** |
| `categories.py` | 3 | taxonomy; `category_for(restaurant)` → (category, source) |
| `features.py` | 1 | `compute(rid, week_end)` from own tables; `store()`; `latest_by_restaurant()` |
| `memory.py` | 1 | `restaurant_memory(rid)`: busiest weekdays, seasonality, own recommendation record by kind, what worked, what was ignored, metric slopes; `lines()` for the prompt |
| `feedback.py` | 1 | `sync()` derives events; `record()` for new callers |
| `scoring.py` | 2 | `kind_stats(kind, cohort)`: acceptance, success, median days; `rank_kinds()` |
| `patterns.py` | 2/3 | declarative `HYPOTHESES`; `discover()` per cohort and platform-wide; `active(cohort)` |
| `benchmarks.py` | 3 | `compute()` weekly; `benchmark(rid, metric)` with percentile and band |
| `trends.py` | 2/3 | weekly medians per cohort × metric, slope, `emerging()` |
| `confidence.py` | all | `score(rid, rec_kind, metric)` → `{score, band, factors[], caution}` |
| `dashboard.py` | admin | the Intelligence page payload, passed through `assert_anonymous` |
| `staffing.py` | 1 → 3 | people on the floor per role family and daypart per $1k of sales (`staff_per_1k.<family>.<daypart>` in each feature row); cohort bands come from `benchmarks.compute`; `starting_headcount` lends a restaurant with no history of its own the cohort median scaled by ITS OWN sales, only over `MIN_COHORT`, through `assert_anonymous`, labelled borrowed |
| `jobs.py` | — | `run_features()` (bounded, cursor-resumable), `run_learning()` |

## Background jobs

- **`intelligence_features`** nightly at 3am server time: for each active
  restaurant, compute this ISO week's feature row (upsert). Worker pool of
  4, wall-clock bound of 4 minutes, and a cursor in `job_cursors` so the
  next night resumes where this one stopped — `run_daily_fetch`'s pattern.
- **`intelligence_learning`** nightly at 4am: `feedback.sync` →
  `patterns.discover` → `benchmarks.compute` → `trends` → confidence log.
  Reads only the materialized tables, so its cost is O(restaurants ×
  hypotheses), not O(rows).
- Both go through `ops.run_job` (lands in `job_runs`) and
  `ops.claim_period` (one runner). Both only run on Railway, like every job.

## The intelligence pipeline

1. **Features** — one row per restaurant-week of ratios: review response
   within 24h rate, reply rate, average rating, labor %, daily labor
   standard deviation (variance), weekend sales share, schedules and edits
   per 28 days, waste as % of sales, food cost %, campaigns per 28 days,
   tap and return rates, posts per 28 days and cadence, specials rotation,
   recommendations presented / done / declined, outcomes improved rate,
   and `completeness` (share of feature keys that could be measured).
2. **Feedback** — every recommendation's life, derived from the tables
   that already record it.
3. **Discovery** — for each cohort (and platform-wide), each hypothesis
   splits restaurants into with / without a behaviour (`response_24h_rate ≥
   0.5`, `schedule_adjust_rate ≥ 0.5`, …) and compares an outcome feature
   (`avg_rating_delta`, `labor_pct_sd`, …). A pattern is written only when
   both groups have ≥ 5 restaurants, Cohen's d ≥ 0.3, the permutation
   p ≤ 0.05 **and** the BH-adjusted q ≤ 0.10 across all hypotheses tested
   that night. A pattern that stops meeting the bar is retired, not deleted.
4. **Benchmarks** — p25/p50/p75 per cohort per metric per week.
5. **Trends** — slope of the weekly cohort median over the last 8 weeks.
6. **Confidence log** — the week's mean confidence and success by kind.

## The recommendation pipeline (what changes for the reader)

- Home's recommendations carry `confidence: {score, band, caution}`. Old
  clients ignore the field; the web and iOS show a low-confidence caption.
- Ask gets two tools (`read_restaurant_memory`, `read_platform_intelligence`)
  and one short context section, present only when the cohort clears the
  floor, phrased as "restaurants like yours" with counts and effects only.
- Every event that answers a recommendation is already recorded; the sync
  turns it into learning. Future modules call `feedback.record` directly.

## The confidence model

`score = (Σ weight × factor / Σ weight over factors present) × coverage`,
where `coverage` is the share of all weights that could be measured. A
factor that could not be measured does not vote, but it does not vanish
either: one calm factor alone must never read as certainty. Each factor is
0–1:

| Factor | Weight | Source |
|---|---|---|
| Restaurant-specific history | 0.25 | own success rate for this kind (n-shrunk toward 0.5) |
| Cross-restaurant evidence | 0.20 | platform success rate for this kind (n-shrunk) |
| Restaurant-type match | 0.10 | a category and a cohort that clears the floor |
| Historical accuracy | 0.15 | this restaurant's scored forecast error and evaluated-outcome clarity |
| Data completeness | 0.15 | latest feature row's completeness |
| Recommendation success rate | 0.10 | pattern support for this kind in the cohort |
| Recent operational changes | −0.10 | schedule edits, price changes, a new POS in the last 14 days |

Bands: high ≥ 0.70, medium ≥ 0.45, else low. Low confidence carries a
caution sentence the surfaces render. Deterministic: same rows → same score.

**Success and acceptance, as `scoring` counts them** (per recommendation —
restaurant and key — never per event row):

- `success_rate` = improved ÷ (improved + worsened + no clear change). A
  result that could not be measured (`unknown`) is **not a failure**: it is
  reported as `unknown` and kept out of the denominator (it used to count
  as one). `no_clear_change` is reported on its own; `measured` is the
  clear-verdict count.
- `acceptance_rate` = taken ÷ (taken + declined + hidden + ignored). An
  ignored recommendation — shown, never answered, expired — stays in the
  denominator; a snooze is neither. Each recommendation is in **one**
  bucket, the strongest thing that happened to it: taken > declined >
  hidden > ignored (a snooze only when nothing else did) — a card that
  expired and was answered later is the answer, not an answer and an ignore
  (re-audit B8).
- Only answers to a recommendation some surface **showed** are learned: a
  ledger answer on an episode nobody was shown (one an answer opened) is
  skipped by `feedback.sync` (B20). A measured verdict is read through
  `rec_learning.learned_verdict` — the one mapping the engine and the
  owner's record share: the owner saying they did not make the change, or
  that something else changed, is `unknown`; a move that faded or reversed
  at its re-check is `no_clear_change`, never a win (B9).

**The floor, per figure (re-audit B3).** A cross-restaurant rate is a fact
only over `MIN_COHORT` restaurants that contributed **to that rate**:
`answered_restaurants` (restaurants with a settled recommendation of the
kind — taken, declined, hidden or ignored) gate the acceptance rate
(`acceptance_available`), `measured_restaurants` (restaurants with a clear
measured result) gate the success rate (`success_available`); `available`
is either, and `scoring.public()` withholds a rate — and its counts — whose
own population is below the floor. Counting every restaurant with any row
let five restaurants that merely let one card expire stand as the floor for
a 10-of-10 success rate that ONE restaurant measured, and a card then said
"restaurants like yours: 10 of 10 improved". A figure used as one
restaurant's prior (`confidence.score`'s `platform_evidence`,
`rec_learning`'s cohort prior) also leaves that restaurant out
(`exclude_restaurant_id`): its own record is weighed against the prior,
never counted inside it.

**One confidence per card** (`card_confidence`): the card's own evidence
sets its band; the kind's record may move it one step. This restaurant's
own measured record of the kind comes first. Where it has none, the
cross-restaurant figure (`platform_evidence` — the cohort's, else the
platform's, without this restaurant) may move it, only when it stands on
at least `MIN_COHORT` restaurants that MEASURED the kind and
`PRIOR_MIN_MEASURED` (10) measured results; the factor is
asserted anonymous where `score()` builds it and again before a card uses
it, and the card says "restaurants like yours: X of Y measured … improved"
— counts only (`basis`: own | cohort | platform). It used to be computed
and then ignored.

## The per-restaurant effectiveness model (`rec_learning`)

What the ledger learned about ONE restaurant, read by every ranker — Home's
card order (`home_brief.order_recommendations`), the cross-module "one
thing" (`business_intelligence.pick_one_thing`) and the DSR's action order
(`dsr/narrative.settle_actions`). Level 1: `WHERE restaurant_id = ?`, over
the last 365 days of episodes.

- Per kind and per subject tag (topic, focus such as "weekend staffing",
  review category, dish, item, daypart): acceptance (taken ÷ settled, the
  ignored included) and success (improved ÷ clear verdicts), each shrunk
  toward its prior by 5 pseudo-observations. The prior is the cohort's own
  shrunk rate for the kind, the cohort **without this restaurant**, each
  rate **only when its own population clears `MIN_COHORT`** (answering
  restaurants for acceptance, measuring restaurants for success — through
  `intelligence.recommendation_success`, asserted anonymous), otherwise
  even (0.5). With nothing learned the weight is exactly 1.0. The model
  reads a year of episodes without their `shown` rows (only whether each
  has one — B18).
- weight = 1 + mean over the kind and its tags of
  0.5 × (acceptance − prior) + 1.0 × (success − prior), times the kind's
  dollar calibration (median measured ÷ predicted, ≥ 3 pairs, shrunk toward
  1, held to 0.8–1.2), **bounded to 0.75–1.25**.
- A worse result downweights that key (0.20 each, capped 0.30) and its kind
  (0.08 each, capped 0.20), halving every 60 days; with the penalty the
  weight never goes below 0.6.
- It reorders only. Nothing is dropped, and nothing learned is applied to a
  critical item: the one thing keeps every critical candidate exactly where
  it was, and reorders only the candidates between them.

The owner's own record of the same trail — what they followed by module,
what worked by tag, the timeline, the check-in — is `rec_learning.summary`,
`timeline` and the `/recs/*` routes (`API_REFERENCE.md`), redacted per
viewer.

## Pattern-discovery architecture

Hypotheses are data (`patterns.HYPOTHESES`), not code: behaviour feature,
split, outcome feature, direction, sentence template. Adding one is one
dict. The test is a seeded two-sided permutation test on the difference of
means (2,000 shuffles; exact enough at cohort sizes and free of SciPy),
with Cohen's d as the effect floor and Benjamini–Hochberg across the
night's hypotheses. Sentences say counts and effects ("Across 14 similar
restaurants, those replying to reviews within a day averaged 0.3★ higher
over the following 90 days") and never a name.

## Privacy safeguards

- `intel_features` holds ratios, rates and counts. Sales appear only as
  denominators inside ratios; dollars are never stored there.
- Every cross-restaurant answer passes `privacy.cohort_ok(n)`; below the
  floor it returns `{available: False, reason}`.
- `privacy.assert_anonymous(payload)` rejects any payload carrying a key
  from the forbidden list (`restaurant_id`, `name`, `owner_email`,
  `employee`, `phone`, `place_id`, `sales`, `revenue`, …) on its way to a
  cross-restaurant surface. The admin dashboard, patterns, benchmarks and
  Ask context all pass through it, and the tests assert it.
- Effects are rounded (`round_effect`) so a two-member difference cannot be
  reversed into a member's value; below the floor no effect is emitted.
- Level 1 reads only `WHERE restaurant_id = ?`. The Ask context names the
  restaurant's own figures only to its own owner.

## Performance impact

Features: ~12 small indexed queries per restaurant per night, bounded and
resumable. Learning: reads one row per restaurant per cohort; ~20
hypotheses × permutation (2,000 shuffles) ≈ 40k list operations per cohort,
milliseconds at present scale, seconds at thousands. Request paths read
only materialized rows (`intel_patterns`, `intel_benchmarks`,
`intel_features` latest) — one indexed query each; `confidence.score` is
four indexed reads and no model call.

## Scalability to 100,000+ restaurants

- Feature rows: 100k × 52 ≈ 5.2M rows/year at ~600 bytes — fine in
  Postgres, and the pass is already resumable, so it can take several
  nights per full sweep and still keep every restaurant within a week.
- Discovery and benchmarks read the latest row per restaurant: 100k rows
  per cohort pass. Cohorts are computed independently, so they shard
  naturally by category, and the permutation count is a constant.
- Nothing here needs a second process. The documented constraints stand:
  SQLite with one gunicorn worker until the process-local dicts move; at
  100k restaurants the platform's own move to Postgres carries this engine
  unchanged because it only ever reads and writes through `get_conn`.

## What this deliberately does not do yet

- Weather and holiday effects: no per-restaurant weather history is stored;
  the hypothesis slots exist and will light when the labor weather feed is
  persisted.
- Employee retention: staff rows have no start/end dates yet.
- Promotion effectiveness beyond campaigns and specials cadence.
- At present the platform has fewer than `MIN_COHORT` live restaurants, so
  every cross-restaurant surface says so honestly. The engine is built for
  the week that changes.

## Posts in the engine (added after the marketing gap review)

Published posts contribute `dish_posts_28d`, `offer_posts_28d`,
`occasion_posts_28d`, `post_lift_median_28d`, `item_lift_median_28d` and
`post_engagement_rate_28d` (from `marketing_tags` and the cached
`marketing_attribution` rows). Four hypotheses read them: dish posts vs
sales lift, occasion posts vs engagement, offer posts vs lift, weekly
cadence vs lift. The former `specials_28d` feature is gone — it matched
content types the generator never wrote.
