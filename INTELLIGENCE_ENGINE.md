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
                 │ confidence.py  (seven factors → kind score; admin only)  │
                 └────────────────────────────┬──────────────────────────────┘
                                              ▼
        Ask (tools + context)   rec_trust (Recommendation Confidence, K1)
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
| `confidence.py` | all | `score(rid, rec_kind)` → `{score, band, factors[], caution}` — the kind-level model, read by the admin dashboard; NOT what owners see (see Recommendation Confidence below). `metric` is accepted and not read |
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

- Every recommendation-bearing payload carries the Recommendation
  Confidence object (K1, below) from `rec_trust.assess` — not
  `confidence.score()`, and not `card_confidence` (superseded 9/24/26). Old
  clients read its `score` (always a number), `band`, `label`, `reason`.
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
| Measurability | 0.10 | how READABLE this restaurant's record is: scored forecast error and the share of evaluated outcomes with a clear verdict. A worsened result is as clear as an improved one, so this is never accuracy (it was mislabelled "historical accuracy" until 9/24/26; CA2 #12) |
| Data completeness | 0.15 | latest feature row's completeness |
| Pattern support | 0.10 | pattern support for this kind in the cohort |
| Recent operational changes | 0.10 | the factor is 1 − change: calm reads 1, a schedule edit / price change / POS change in the last 14 days lowers it |

The weights sum to 1.0 (a test holds it). Bands: high ≥ 0.70, medium ≥
0.45, else low. Deterministic: same rows → same score. This model rates a
KIND; it feeds the admin Intelligence page, never an owner's card.

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

**One confidence per card** — since 9/24/26 the measured Recommendation
Confidence (below). The paragraph that follows describes the superseded
`card_confidence` (band logic kept for its tests; a candidate for future
cleanup after additional verification): the card's own evidence
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

## Recommendation Confidence (`confidence_engine`, `rec_trust`, `data_freshness`)

Every recommendation-bearing payload — Home cards and attention items, the
one-thing hero, the review / food / labor / campaign diagnosis blocks, food
cost drivers, Ask answers, DSR actions — carries ONE confidence object
(contract K1). The owner reads percentages (Will, 9/24/26), so every
percentage is computed; below a floor a dimension is `null` with a basis
saying what is needed, never a stand-in.

```
{"pct": 72 | null, "band": "low|medium|high", "label": "72% confidence" | "Confidence not yet measurable",
 "reason": "<the weakest dimension's basis>", "score": 0.72 (0.0 when null), "caution": null | "...",
 "dimensions": {"evidence": {pct, basis, n, kind},
                "accuracy": {pct|null, basis, n, improved, source: own|cohort|none, low, high},
                "freshness": {pct|null, basis, as_of (M/D/YY), as_of_iso, stalest}},
 "version": 1}
```

Where an older client decodes a field named `confidence` as a STRING (the
diagnosis blocks, food drivers, Ask's answer), that field stays the band
word and the object rides beside it as `confidence_detail`.

- **Evidence Strength** = 100 × min(1, n ÷ `N_FULL[kind]`) × coverage ×
  quality. `N_FULL` is one table with a reason per kind (reviews 8,
  weekdays 4, weeks 8, waste weeks 4, price weeks 4, trading days 28,
  posts / campaigns 4, competitors 3, evidence items 3, night facts 3, a
  direct count 1). Coverage is the share of the window measured. Caps:
  coverage under `MIN_COVERAGE` (0.7) → ≤ 49; any partial-data flag
  (days missing sales, estimated hours, gross missing, conflicting sales,
  inferred, sampled, provisional, period too short) → ≤ 74; any unverified
  figure → ≤ 35; a model's own band only lowers it (medium ≤ 65, low ≤ 35);
  sample or demo data → 0 and labelled sample. The owner's "don't trust
  the data" answer (`dont_trust_data`) caps that kind's evidence at 49 for
  30 days.
- **Historical Accuracy** = `rec_learning.kind_record`: this restaurant's
  shown AND taken episodes of the kind, read only through
  `learned_verdict` (disowned, conditions-changed, informational, faded
  and reversed results are never wins), one result per overlapping window,
  improved ÷ measured shrunk toward even (k = 5), with its 90% Wilson
  range. Shown only at `MIN_MEASURED_FOR_RATE` (5) own results; else the
  anonymous cohort's (this restaurant excluded, `cohort_ok`,
  `assert_anonymous`) at `PRIOR_MIN_MEASURED` (10), labelled "At
  restaurants like yours"; else `null`.
- **Data Freshness** = 100 × the minimum over the card's sources of
  recency × completeness (`data_freshness.SOURCES`, one threshold table:
  recency is 1 within `grace` days of the expected lag, then falls to 0
  over `horizon` days; an error — a failing sync, an expired token, two
  missed review fetches — caps it at half; an unknown age is 0, never
  current). Sources are dated by the last day the data COVERS (shifts by
  their last day, counts by the oldest count), not by when a file was
  written.
- **Overall** = the geometric mean of the measured dimensions; no evidence
  → not measurable. No track record here (accuracy null) → at most 70, with
  a caution. Freshness under 50 → at most 49. Band: ≥ 75 high, 50–74
  medium, else low.

At delivery `rec_ledger.present_many` snapshots it on the episode
(`confidence_pct`, `evidence_pct`, `accuracy_pct`, `accuracy_n`,
`freshness_pct`, `freshness_as_of`, `trust_version`) and on every `shown`
event, noting a move of 10+ points between showings (`confidence_moved`).
A surface that shows no confidence yet still gets accuracy and freshness
snapshotted. `feedback.sync` fills `intel_rec_events.confidence_at` from
the snapshot. The admin calibration view (`/admin/api/calibration`) scores
the stated % against learned verdicts: reliability by decile with Wilson
ranges and a Brier score, per kind and per dimension, each withheld below
its floor (20; 5 per kind).

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
  0.2 × (acceptance − prior) + 1.0 × (success − prior), times the kind's
  dollar calibration (median measured ÷ predicted, ≥ 3 pairs, shrunk toward
  1, held to 0.8–1.2), **bounded below at 0.75 and above by a ceiling that
  scales with measured success**: 1 + 0.25 × (Wilson 90% lower bound of the
  kind's — or its best tag's — own improved ÷ measured, above the prior,
  over the prior's headroom). No measured result, no lift above 1.0; 3 of 3
  allows ≈1.01, 30 of 30 ≈1.21 (CA2 probe E: both used to hit 1.25).
  Acceptance weighs 0.2 (it was 0.5): rank decides exposure and exposure
  drives acceptance, so what the owner likes may nudge rank but never lift
  it on its own.
- The same calibration corrects the dollars a recommendation is SHOWN with:
  `Effectiveness.adjusted_dollars(key, dollars)` → `{dollars,
  dollars_adjusted, calibration_n, calibration_ratio, note}` —
  `dollars_adjusted` is None below 3 measured pairs, otherwise the figure ×
  the ratio with the note "adjusted from N measured results" (the surfaces
  carry it on the rec; Group E/J render it).
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

## What a measured result is allowed to say (confidence audit, 9/24/26)

Every rate the engine learns rests on `recommendation_outcomes`, so the
measurement is held to these rules (outcomes.py, metrics.py; tests in
`tests/test_outcome_calibration.py`):

- **Regression to the mean (CA2 #1).** A triggered tracker — one whose key
  names a recommendation a surface showed, or an alert read — is never
  measured against the window that fired it. `trigger_start`/`trigger_end`
  are the tracker's window length ending the day before its episode chain
  was first shown; `trigger_value` is the metric over it. The baseline is the
  window's MIRROR about the trigger (`outcomes.mirror_window`: same length,
  as far before the trigger as the after-window starts after it), seasonally
  adjusted where a year allows (`baseline_kind` "before the trigger" or
  "same weeks last year"). A number's pull back from a bad stretch is the
  same looking back as forward, so a change that does nothing reads
  improved as often as worsened: CA2 probe R replayed read 57% improved /
  12% worsened against the trigger window and 5% / 6% now. (A trailing
  8–12-week baseline was tried and read worsened twice as often as improved
  — the after-window sits nearer the trigger than most of those weeks.) A
  recommendation taken more than `TRIGGER_MAX_GAP_DAYS` (56) after its
  trigger window ended uses the plain baseline, which can no longer overlap
  it. When the mirror cannot be read, the old baseline is used and a result whose
  baseline overlaps the trigger window is flagged `baseline_overlaps_trigger`:
  shown, never counted — not in learning, not in delivered value.
- **Noise bands per restaurant (CA2 #3).** `metrics.noise_band` estimates
  this restaurant's spread of the metric over its own non-overlapping
  windows of the comparison's length (≥4 windows over up to a year, else ≥6
  whole weeks scaled by √(7/L) for a per-day metric, else the stated band
  alone): band = max(stated, 1.645 × σ_L × √(1 + L/B)), a two-sided 10%
  false-alarm rate that the stated floor only lowers. The tracker stores
  `noise_band`, `noise_sigma`, `false_alarm_rate` and `band_basis` at its
  start and every read of it (evaluation, re-check, accrual, the grade, the
  interim reading) uses it. A 7-day caller passes `window_days=7`.
- **One success definition (CA2 #4).** `rec_learning.learned_verdict` is
  the only mapping; `outcomes.result_counts` is its value twin (not an alert
  read, a routine supplier order or advice not taken; not disowned; no
  "something else changed" check-in; not measured against its trigger
  window) and a result counts in learning exactly when it counts in
  delivered value (the learning == value test). `features.
  outcomes_improved_rate_90d` reads through it — `CLEAR_VERDICTS`
  denominator, one result per number per overlapping window, None below
  `MIN_MEASURED_FOR_RATE` (5); `recs_*_28d` come from the ledger, every
  surface. `scoring.kind_stats` carries `success_enough` and `public()`
  withholds a rate below 5; an `auto` action (delayed.py) is its own bucket,
  neither taken nor declined. `memory.own_record` names a kind as "worked"
  only as `rec_learning.most_effective` would (≥5 measured, success ≥ even,
  ranked by the Wilson lower bound) and says "k of n"; a memory slope is
  said only when the series moved past the metric's stated band over the
  weeks read.
- **Advice not taken (CA2 #11).** `outcomes.observe_untaken` (run by
  `evaluate_due` for each restaurant) gives a recommendation that was shown
  and then dismissed (not "already doing it") or left to expire an
  informational tracker, `observed:untaken:<rec_id>`, on the number it
  carried, measured the same way. `rec_learning.untaken_comparison` (and
  `kind_record["untaken"]`) sets taken beside not-taken — "Compared with
  when you didn't: …" — only over 5 results each side, never as cause.
  feedback.sync skips these trackers.
- **Pattern strength.** A pattern's `confidence` is a display blend of
  effect size, p and n, not the probability it is right; payloads carry it
  as `strength_pct` with its label and basis (`patterns.strength_fields`),
  and Ask's context says "strength … not a probability".

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
