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
| `restaurants` profile columns (Benchmarking #7) | `service_model` (counter / full_service / bar_led / daytime), `concept` (a taxonomy value), `bar_led`, `ownership` (independent / franchise / corporate), `opened_year`, `profile_source` (set / inferred), `profile_confirmed_at`; `google_types` + `google_price_level` (the restaurant's own listing, a cross-check for the guess only) | own row; the peer partition is built from the CONFIRMED profile only |
| `restaurants.exclude_from_learning` | a test or internal account: out of every cross-restaurant figure exactly as a demo is (`jobs.REAL_RESTAURANT_SQL`) | — |
| `restaurants.labor_target_source`, `food_cost_target_source` | set / seeded / default — where a target came from (#13) | own row |
| `intel_features` | one row per restaurant-week: `features_json` of ratios, rates and counts; `completeness` 0–1 | no names, no dollars, no people; tenant-keyed |
| `intel_rec_events` | one row per recommendation event: kind, action (presented / done / not for us / hidden / snoozed / accepted / tracking / implemented / measured / confirmed / dismissed / auto / ignored), outcome (improved / worsened / no clear change / unknown), days to effect, confidence at the time | kind is a key prefix, never text |
| `intel_patterns` | discovered patterns: cohort, behaviour, outcome, n with / n without, effect, p, q, confidence, sentence, status | counts and effects only |
| `intel_benchmarks` | per cohort × metric × week: n, p25, p50, p75, mean, `vals_json` (the member values, sorted), `members_json` (each value beside an organisation HASH), `orgs` (distinct organisations) and `max_org_share` — server-side only, so the band shown to a member leaves its whole organisation out. The cohort is a peer PARTITION key (`sm:<service model>[|bar][|protein|starch|mixed][|v<band>]`) or `platform` (behaviour metrics only). Frozen for the ISO week: the first computation of a week stands | stored over ≥ MIN_COHORT members from ≥ `privacy.MIN_ORGS` organisations; SHOWN only through `benchmarks.published()` (viewer's organisation excluded, ≥ 8 others from ≥ 5 organisations, none over ⅓, spread gate, Harrell–Davis quartiles at a coarse step, ≤ 8 weeks old) |
| `intel_peer_assignments` | per restaurant × week × family (format / labor / food): the rung reached (self / published / platform / peers), the partition, a hash of the peer set (never the ids), n and organisations with the viewer's own out, profile source and confirmation date, measured drift (#29) | tenant-keyed; server-side |
| `intel_cohort_series` | per cohort × metric × week: n and the median over the BALANCED panel, with the window's joined / left counts (#44) | aggregates over ≥ MIN_COHORT |
| `intel_confidence_log` | per week × cohort × kind: mean confidence, acceptance, success | aggregates |
| `intel_rec_events` effect columns (boot ALTERs) | `metric`, `effect_pct`, `effect_z` (signed so positive = better), `baseline_kind`, `after_end`, `tags_json` — what a counted result MOVED (BM4-6) | tenant-keyed; filled only for results `rec_learning.learned_verdict` counts |
| `intel_dna` | one row per restaurant-week of its Restaurant DNA: `dims_json` `{dim: {raw, z, n, basis, norm}}`, `coverage`, `version` | ratios, rates, shares and bands only — never dollars; `assert_anonymous` on every row |
| `intel_benchmark_facts` | per restaurant × metric × week: the engine's `compare()` payload (kinds self/peers/platform/industry/market — never the viewer-dependent `location`), `available` | the restaurant's own comparisons only |
| `intel_effects` | per restaurant × kind × metric × week: the neighbour prediction fact (`predict.predict_effect`), `available` | counts, a median and an interval only |
| `staff_first_seen.last_seen` | the newest shift date ever seen per name (`remember_tenure` keeps the MAX) | own row |
| `job_cursors['intelligence_features']` | where the nightly feature pass stopped (the DNA rides the same pass) | — |
| `job_cursors['intelligence_patterns' / 'intelligence_benchmark_facts' / 'intelligence_effects']` | where bounded discovery, the comparison materialisation and the weekly prediction pass stopped | — |

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
| `categories.py` | 3 | taxonomy; `category_for(restaurant)` → (category, source) — the confirmed concept first; `profile_for` / `partition_key(profile, family)` (the hard split: service model; × bar-led for labor and food; × menu family for food cost and waste) / `partition_label`; `infer_detail` (format before cuisine, name before menu, Google types, a confidence) and `suggestion` ("We think you're a pizzeria — is that right?"); `clean_profile` / `profile_payload` for the Account block |
| `features.py` | 1 | `compute(rid, week_end)` from own tables; `store()`; `latest_by_restaurant()` |
| `memory.py` | 1 | `restaurant_memory(rid)`: busiest weekdays, seasonality, own recommendation record by kind, what worked, what was ignored, metric slopes; `lines()` for the prompt |
| `feedback.py` | 1 | `sync()` derives events; `record()` for new callers |
| `scoring.py` | 2 | `kind_stats(kind, cohort)`: acceptance, success, median days; `rank_kinds()` |
| `patterns.py` | 2/3 | declarative `HYPOTHESES`; `discover()` per cohort and platform-wide; `active(cohort)` |
| `benchmarks.py` | 3 | `compute()` weekly over each member's confirmed partition (eligible members only: ≥ 8 live weeks, completeness ≥ 0.5, not excluded, a non-default labor cost basis for labor-cost metrics; one per Google listing); `published(cohort, metric, exclude_org=)` — what may be shown; `benchmark(rid, metric)` — the wrapper older callers keep: the confirmed partition, never an all-types band for a non-behaviour metric ("no like-for-like peers yet"); `context_line(b)`; `cohort_table()` rounded as published (admin) |
| `metrics_registry.py` | 3 | one row per metric: comparability (behaviour / format / economics), `partition_family`, `DEFINITIONS` (what Cavnar's figure measures), the spread ceilings of the quality gate, `module_key` for aliases |
| `engine.py` | 3 | the Benchmark Engine: `compare()` / `compare_all()` / `facts()` / `prompt_lines()` / `payload_for()`; `own_eligible()` (the viewer's figure passes a member's eligibility), `standing()`, `strength()`, `outcome_words()`; the peers kind walks the ladder (below) |
| `trends.py` | 2/3 | weekly medians per cohort × metric over a balanced panel (present ≥ 6 of 8 weeks), joined / left, slope, `emerging()`; `persist()` writes `intel_cohort_series` in the learning pass |
| `confidence.py` | all | `score(rid, rec_kind)` → `{score, band, factors[], caution}` — the kind-level model, read by the admin dashboard; NOT what owners see (see Recommendation Confidence below). `metric` is accepted and not read |
| `dashboard.py` | admin | the Intelligence page payload, passed through `assert_anonymous` |
| `staffing.py` | 1 → 3 | people on the floor per role family and daypart per $1k of sales (`staff_per_1k.<family>.<daypart>` in each feature row); partition bands (with the sales band once the restaurant's own is measured) come from `benchmarks.compute`; `starting_headcount` lends a restaurant with a CONFIRMED profile and no history of its own the PUBLISHED median (≥ 8 others from ≥ 5 organisations, 0.05 step) scaled by ITS OWN sales, labelled borrowed; `payload()` ships the rounded headcount, the group label and n — never `people_per_1k` (#10) |
| `dna.py` | 1 → 3 | Restaurant DNA (BM4 §5, Top-50 #24): `measure`/`compute`/`store` the ~30 dimensions (22+ buildable today; S5 beverage share dormant; B10 retention dormant until `last_seen` fills), `normalise` (stated anchors below `MIN_ROBUST_N` = 30 measuring a dimension, robust z = (x − median) ÷ 1.4826·MAD from 30, clipped ±3; the centre and scale ride with each value), `distance(a, b, weights, norms)` (Gower-style, missing-aware, both rows re-normalised from `raw` under one norm set; None below 60% shared weight or 4 shared structural dimensions — 40% and 3 when size, ticket and service model are all shared), `prediction_weights`, `profile(rid)` — the owner's own read — and `payload_for(user)` |
| `predict.py` | 3 | `predict_effect(rid, kind, metric, tags)` → a fact of kind `prediction` for the P2 rule; `neighbours()` (server-side only); `run_weekly()` into `intel_effects` (DNA, organisations and norms loaded once per pass; bounded and resumable per (kind, metric) pair). Dormant below its floors (every restaurant today) |
| `comparison_cache.py` | 3 | `materialise()` the engine's comparisons nightly into `intel_benchmark_facts` (bounded, cursor-resumable); `read()` — `engine.compare(..., use_cache=True)` serves a fresh row computed from the restaurant's current settings (`inputs_key`); `engine.payload_for` (the card, the Home strip, `/api/benchmarks`) reads it |
| `jobs.py` | — | `run_features()` (bounded, cursor-resumable; writes the DNA row beside the features), `run_learning()` |

## Background jobs

- **`intelligence_features`** nightly at 3am server time: for each active
  restaurant, compute this ISO week's feature row (upsert). Worker pool of
  4, wall-clock bound of 4 minutes, and a cursor in `job_cursors` so the
  next night resumes where this one stopped — `run_daily_fetch`'s pattern.
- **`intelligence_learning`** nightly at 4am: `feedback.sync` →
  `patterns.discover` → `benchmarks.compute` (over `jobs.peer_partitions`)
  → the peer assignment ledger (`jobs.record_assignments`) →
  `trends.persist` (the balanced-panel cohort series, `intel_cohort_series`;
  BM4-17: this line used to say trends were persisted when they were not)
  → confidence log → `comparison_cache.materialise` → `predict.run_weekly`.
  Reads only the materialized tables, so its cost is O(restaurants ×
  hypotheses), not O(rows). Every step that walks cohorts or restaurants is
  bounded and resumable (Benchmarking audit BM4-14): discovery by a wall
  clock (`DISCOVER_WALL_SECONDS`) and a per-cohort cursor, retiring a
  pattern only in a cohort it actually tested, with a Besag–Clifford
  sequential stop (every 200 shuffles, stop once 20 have matched the
  observed difference); the confidence log reads the last 365 days of
  events; the comparison materialisation and the prediction pass by wall
  clock and cursor.
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
   Every ratio has a measured floor before it is non-null (re-audit B3
   #11): a 28-day day-based ratio (labor %, its day-to-day spread, weekend
   share, hours per $1k, food cost %, waste as % of sales) needs
   `features.MIN_MEASURED_DAYS` (14) days carrying its data; a daypart
   ratio 14 schedule-outcome days; a review ratio `MIN_REVIEWS_FOR_RATIO`
   (5) reviews (and timed replies); a campaign rate `MIN_SENT_FOR_RATE` (20)
   messages; a post rate `MIN_POSTS_FOR_RATE` (3) posts. Below it the
   feature is None, so it neither enters benchmarks nor counts in
   completeness (one day at 61% used to publish `labor_pct_28d` = 61.0).
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
  and one short context section (`intelligence.context_bundle`), with counts
  and effects only. Since the Benchmarking audit (9/24/26, workstream V)
  the comparison lines are the Benchmark Engine's (`engine.compare_all` +
  `engine.prompt_lines`): a band of the restaurant's own type, or the
  all-types band ONLY for a behaviour metric — never an all-types labor %,
  food cost % or hours-per-$1k band. Each line names the group exactly as
  the engine does — "12 other Pizza on Cavnar", or "12 other restaurants on
  Cavnar, all types" — never "restaurants like yours" for the all-types
  group; says how the group was chosen ("peer group: restaurants of the
  same type (Pizza), the type set by the owner" / "… inferred from the
  restaurant's name, not set by the owner"), how many measured the figure
  ("measured at k of m in the group": k members measured this metric that
  week, m the most that measured any benchmarked metric, both after the
  viewer's organisation is taken out, so k is the n the line names), the
  as-of date (M/D/YY) and the comparison strength %. Each line opens with
  the restaurant's own figure and which way is better ("this restaurant
  34% (lower is better)") and gives the standing as an outcome ("worse
  than 3 in 4 of the group (higher labor % than 3 in 4)"), never a bare
  "bottom quarter", which on a lower-is-better metric is the highest
  figure (re-audit #17). A published figure the engine marks not
  comparable reads "context only, measured differently". The schedule
  prompt's cohort block (`schedule_engine._cohort_block`) uses the same
  lines.
- **Every benchmark a model is handed is a fact** (`engine.facts`, kind
  "benchmark", with `source_kind` / `engine_kind`, `n`, `min_n`, `as_of`,
  `restaurant_category`, `strength_pct`, `standing`, `comparable`,
  `definition_note`, `inferred`, `metric`, `better` and `own_value` — the
  restaurant's figure only when it may be ranked; the own figure is also a
  "computed" fact `bench.<metric>.own`): Ask's
  snapshot records them beside its text (`ask_cavnar.snapshot_benchmark_facts`)
  and types the `read_platform_intelligence` payload's `comparisons` through
  `engine.facts`; the schedule note's context registers the cohort block's
  facts; the labor and food reads register the registry's published
  figures. The Response Validation Layer's B1 binds every peer or industry
  claim to one of them for the same measure, or the claim is not said
  (response_validation's docstring has the whole rule, and P2 the rule
  and fact shape for "restaurants like yours reduced X by N%").
- **Projected by the login.** The context section, both tools and their
  facts leave out every metric of a module the login may not view
  (`intelligence.visible` / `metric_module` over
  `permissions.MODULE_VIEW_PERMISSIONS`): no labor or food cost figure —
  own, peer or published — for a login denied Labor or Food Cost (BM1-17).
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
  at its re-check is `no_clear_change`, never a win (B9); a result read
  alongside another change, a trend or a level shift is `unknown` (B2 #5).
- A measured result is filed **one per episode** (re-audit B2 #3): the
  `measured` row's key is `<source_key>#o<tracker id>`
  (`feedback.measured_key`), so a key measured three times is three
  results — the unique (restaurant, key, action) row used to be overwritten
  in place, improved then worsened then improved reading as one result
  flipping. On one number (restaurant, metric) only the earliest of
  overlapping after-windows counts; the others are filed `unknown`
  (`feedback._counted_tracker_ids`, the own record's `_one_per_window`).
  Rows written the old way are dropped once
  (`intelligence_feedback_repair:episodes_v1`) and re-derived.
- **No one restaurant carries a cohort figure** (re-audit B2 #3): every
  cross-restaurant summary adds `measured_capped` / `improved_capped` /
  `success_rate_capped` — each restaurant's clear results scaled so it
  holds at most `scoring.MAX_RESTAURANT_SHARE` (1/3) of the capped total
  (`scoring.capped_counts`, the fixed point c = ⅓ × Σ min(n_r, c)). Every
  prior reads the capped counts: `kind_record`'s cohort stand-in needs
  `PRIOR_MIN_MEASURED` (10) CAPPED results (one peer's 8 of 12 now leaves
  6, and the cohort does not stand in), and `Effectiveness.prior` uses
  them.

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
it, and the card names the group the prior was read from ("Pizza on
Cavnar: X of Y measured … improved", `prior_label` = the cohort's label,
else "other restaurants on Cavnar") — counts only (`basis`: own | cohort | platform). It used to be computed
and then ignored.

## Recommendation Confidence (`confidence_engine`, `rec_trust`, `data_freshness`)

**What the percentage means (the owner's decision, 9/24/26 — "support
score").** "72% confidence" is how well SUPPORTED the advice is — evidence ×
track record × freshness, every part measured. It is NOT the chance the
recommendation works, and nothing may present it as one: every K1 object
carries `meaning` ("How well supported this is — not the chance it
works"), and the Why? panel leads with it. Historical Accuracy is shown as
LIFT against doing nothing. The admin view checks that a higher % really
goes with better results (an ORDER check, below) and flags it when it
doesn't. Percentages stay; every % is a computed measure; below a floor a
dimension is `null` ("—") with a basis saying what is needed.

Every recommendation-bearing payload — Home cards and the attention items
that are advice or measured findings, the one-thing hero, the review /
food / labor / campaign diagnosis blocks, food cost drivers, Shift Quality
(its panel and each suggestion), Ask answers, DSR actions — carries ONE
confidence object (contract K1). A FACT carries none (below).

```
{"pct": 72 | null, "band": "low|medium|high", "label": "72% confidence" | "Confidence not yet measurable",
 "reason": "<the cap that set the figure, else the weakest dimension's basis>",
 "score": 0.72 (0.0 when null), "caution": null | "...", "version": 2,
 "meaning": "How well supported this is — not the chance it works",
 "thresholds": {"high": 75, "medium": 50},
 "caps": {"no_track_record": 70, "record_unproven": 70, "record_against": 49, "stale": 49,
          "stale_below": 50, "freshness_unmeasured": 49, "beats_at": 95, "against_below": 50, "max": 99},
 "caps_applied": ["no_track_record", ...], "sample": false,
 "dimensions": {"evidence": {pct, basis, n, n_full, kind, corroborating},
                "accuracy": {pct|null, basis, n, improved, source: own|cohort|none, low, high,
                             p_beats, beats_label, lift: {improved, n, rate, untaken_improved, untaken_n,
                             do_nothing_rate, source}, prior: {source, centre, weight, ...}},
                "freshness": {pct|null, basis, as_of (M/D/YY), as_of_iso, stalest, stalest_basis, errors}}}
```

Every change from version 1 is additive; `version` 2 marks the support-score
meaning, so a snapshot's `trust_version` says which meaning its % had. Where
an older client decodes a field named `confidence` as a STRING (the
diagnosis blocks, food drivers, Ask's answer), that field stays the band
word and the object rides beside it as `confidence_detail`.

- **Evidence Strength** = 100 × min(1, n ÷ `N_FULL[kind]` × corroboration) ×
  coverage × quality. `N_FULL` is one table, each entry the point where one
  more observation stops moving the figure by more than its decision can
  tolerate (B1 M4, B4 M3 — "one row is the whole sample" is gone from every
  recommendation): reviews 8 (one review moves a theme's share 1/n — 12.5
  points at 8); weekdays 4 (a weekday's labor % swings ~3 points night to
  night, so the mean of 4 has a 1.5-point standard error, half the 3-point
  over-target line); weeks 8 (the fewest where a weekly slope's t-test has
  6 degrees of freedom); waste weeks 4 and price weeks 4 (one bad week in 8
  is chance, 4 a pattern; one price reading's error is the whole change);
  trading days 28 (the 28-day mean's standard error is ~0.6 points, inside
  the smallest move the outcome band reads); posts / campaigns 4;
  competitors 3; supplier quotes 3 (a sourcing driver: three quotes before
  a spread is a market price); recounts 4 (a portion driver: one recount's
  gap can be a miscount); evidence items 3 (Ask's distinct live reads that
  back a stated figure, R3); nights 8 (a daily-report action: the nights
  of observation behind its traced figures — tonight is one, a figure read
  against the demand forecast adds the forecast's same-weekday samples,
  one against last week / yesterday / last year one each; 8 is the
  forecast's own window, so one night is an anecdote). `night_facts` 3 and
  `count` 1 remain only for a stored or older caller's input. Menu drivers
  count their reviewed recipe lines out of all of them (`n_full` per dish).
  **Corroboration** (B4 M2): each other module whose verified figure agrees
  adds `CORROBORATION_STEP` 0.25 to the sample factor, for at most
  `CORROBORATION_MAX` 2 modules (×1.5) — bounded because one restaurant's
  modules share its weeks; the "inferred" cap still holds causal wording
  under high. It reaches the food and review diagnoses (distinct verified
  modules — `rec_trust.verified_evidence_count` counts MODULES, not
  entries) and cross-module links (modules − 1). Coverage is the share of
  the window measured. Caps: coverage under `MIN_COVERAGE` (0.7) → ≤ 49;
  any partial-data flag (days missing sales, estimated hours, gross
  missing, conflicting sales, inferred, sampled, provisional, period too
  short) → ≤ 74; any unverified figure → ≤ 35; a model's own band only
  lowers it (medium ≤ 65, low ≤ 35) — and the band read is the validator's
  CAPPED `confidence`, never the raw `model_confidence` (R9); a caller's
  documented `cap` (the Shift Quality read's completeness, a campaign with
  no holdout) with its reason; sample or demo data → 0 and `sample: true`.
  The owner's "don't trust the data" answer (`dont_trust_data`) caps that
  kind's evidence at 49 for 30 days.
- **Historical Accuracy** = how likely this kind of advice beats DOING
  NOTHING here — `pct = round(100 × P(p_taken > p_nothing))`, held to 1–99.
  The record is `rec_learning.kind_record`: this restaurant's shown AND
  taken episodes of the kind, read only through `learned_verdict`
  (disowned, conditions-changed, informational, faded, reversed,
  baseline-overlap and confounded results are never wins), one result per
  overlapping window. `p_taken` ~ Beta(m·k + improved, (1−m)·k + measured −
  improved) with k = `SHRINK_K` 5 and the prior centre m = the do-nothing
  rate. `p_nothing` is the kind's untaken record when it has 5 results
  (`base_rate` from untaken — the untaken share shrunk by 5 toward chance —
  held as Beta(base_rate·(n+5), …)), else the chance rate (half the false-
  alarm rate of this restaurant's own noise bands, else half the stated 10%
  → 5%) held as loosely as `STATED_RATE_K` 20 results: the do-nothing
  improved rates measured end to end ran 3–10% against the stated 5% (B2 p2
  S1/S2, Q's S1/S4), and Beta(1, 19)'s 90% range, 0.3–14%, covers them —
  a stated departure from "compared against the do-nothing rate" as a
  point, because as a point 2 of 5 cleared it (96%) and the owner's rule is
  that 2 of 5 may not lift a card above the no-record cap (B2 #4). The
  integral is numeric (Simpson's rule under x = t^s, within 0.01 of a
  60,000-draw Monte Carlo — `test_confidence_round2_p`). The basis is the
  lift sentence — "improved 4 of 6 times vs 1 of 6 when not acted on", or
  "vs about 5% by chance" with no untaken record — and `beats_label` says
  "93% likely to beat doing nothing"; `low` / `high` stay the 90% Wilson
  range of the improved rate. Shown only at `MIN_MEASURED_FOR_RATE` (5) own
  results. **The cohort** (the anonymous record of restaurants like this
  one, this restaurant excluded, `cohort_ok`, `assert_anonymous`, no one
  restaurant over a third of it — Q) is only the prior's centre: its share,
  shrunk toward the do-nothing rate, replaces m ONLY when it is lower (peers
  saw the kind do worse than chance). It never produces a figure on its
  own and never lifts one (B1 H9, B2 #3: 4 own results all worsened and a
  10-of-12 cohort read 86% high); below the own floor the basis names it
  by its group ("(Pizza on Cavnar: 10 of 12 improved — not counted until
  your own are in)", `prior_label`) and the figure is `null`. `kind_record` fills the cohort's
  `prior_*` counts at any own count for this. Its other additive fields
  (group Q): `base_rate`, `base_rate_source`, `base_rate_n`,
  `base_rate_basis`, `untaken`, `rate_recent` / `rate_recent_n_eff` /
  `recent_half_life_days` (not used by the figure: a recency-weighted rate
  would re-inflate a short recent run the Beta read deliberately holds
  back).
- **Data Freshness** = 100 × the minimum over the card's sources of
  recency × completeness (`data_freshness.SOURCES`, one threshold table:
  recency is 1 within `grace` days of the expected lag, then falls to 0
  over `horizon` days; an error — a failing or empty sync, an expired or
  unreadable token, a connected account whose metrics never synced, two
  missed review fetches, a stale forecast — holds it under
  `ERROR_CEILING` = 45, strictly below the stale threshold, so every
  erroring source trips the stale cap and its caution; an unknown age is
  0, never current; a date or stamp after the restaurant's today + 1 day is
  a typo or clock error and reads unknown). Sources are dated by the last
  day the data COVERS (shifts by their last day, the POS by its last
  business date carrying sales — not the sync stamp — counts by the oldest
  count, the DSR by its last final report; a provisional night is held
  under "aging"), not by when a file was written. A POS with credentials
  that never synced is `unknown` with pct null: no figure rests on it yet.
  Missing sales days count ONCE, in Evidence Strength (coverage and the
  `days_missing_sales` flag), not also as freshness completeness.
  `MODULE_SOURCES` puts `sales` under labor, schedule, food and the DSR,
  and `weather` under the schedule and demand; `TOOL_SOURCES` maps the Ask
  tools whose module names no source. `pos_health`'s current / aging /
  stale is this table's `pos` row (`age_pct`), one POS rule everywhere;
  "connected" is credentials only. `errors` names every source whose sync
  failed.
- **Overall** = the geometric mean of the measured dimensions (any 0 → 0;
  no evidence → not measurable; never above `MAX_OVERALL` 99 — a support
  score never reads certain), with freshness folded in AFTER the record's
  ceiling: the evidence × record part is held to its cap (a/b) first, then
  freshness weighs on the held figure, and the cap holds the result — so
  with no record, freshness 100 / 79 read 70 but 60 reads 65 and 50 reads
  59 (every level from 50 to 100 read the same 70 before, B3 #2). Uncapped
  it is exactly the geometric mean of the three. The caps IN THIS ORDER, each named in
  `caps_applied` and — the last that bound — in the line's `reason` (B1 M5:
  "70% confidence — a count of 3 drafts" hid the cap that set the 70):
  a. **no track record** (accuracy null) → ≤ `NO_TRACK_RECORD_CAP` 70, with
     the no-track-record caution;
  b. **a record that does not clear doing nothing** — under `BEATS_AT` 95%
     likely to beat it, i.e. the lower end of its 90% range does not clear
     it → ≤ `UNPROVEN_RECORD_CAP` 70; and one more likely than not NOT to
     beat it (under `AGAINST_BELOW` 50%) → ≤ `RECORD_AGAINST_CAP` 49 with a
     caution. Applied at ANY record length, not only a short one (a stated
     departure: a long record of advice that does nothing would otherwise
     read ~79%, the do-nothing simulation in `test_confidence_round2_p`);
  c. **stale data** — freshness under `STALE_BELOW` 50 → ≤ `STALE_CAP` 49
     with the out-of-date caution, ALWAYS, after (a)/(b), so a broken POS
     never reads the same as a healthy one (B3 #2); a failing source
     carries a caution even when the cap doesn't bind;
  d. **freshness nothing could date** → ≤ `FRESHNESS_UNMEASURED_CAP` 49,
     with a caution: an unmeasured dimension never raises the figure (B1
     H1b: an undated card read 87% where the same card 60% fresh read 77%).
  Band: ≥ 75 high, 50–74 medium, else low — `thresholds` in every payload,
  the clients read them from there (tests pin the engine constants).
  Measured on the audit's probes: with full, fresh evidence, no record → 70;
  0 of 5 → 49 (was 63); 2 of 5 → 70 (was 77); 3 of 5 → 99; 0 of 10 → 49
  (was 55); B2 p4's expected support over 20 results — do nothing 60, a
  drifting do-nothing kind 68.5, a real −1 pt labor effect 72.5, a kind that
  improves 35% of the time 95.6 (a drifting do-nothing kind outranked the
  real one before).

**Facts carry no confidence** (B4 H5, B1 C1): a setup or health nudge, a
failing sync, reviews or drafts waiting, a response rate, items critically
low or running low, people over 40 hours, the schedule not built, urgent
reviews (`home_brief.HOME_FACT_KEYS`, the one-thing hero's `fact`
candidates, the group view, the morning brief) carry `confidence: null` —
not a "70%" from a default "1 row counted". Only recommendations and
measured findings (a rating slip, a negative-share rise, labor over
target) get one.

**One confidence per key per build** (B1 H3, B4 M1): `rec_trust.assess`
memoises on its `Context` by recommendation key (a key with a subject), so
within one build — Home, the cross-module read (the hero and every What
connects card share one Context), a DSR — the second surface to assess a
key gets the first one's object. Across builds the same diagnosis reads
ONE evidence input: `rec_trust.review_diagnosis_input` (Reviews tab, Home
card, hero: the theme's `mention_count`, the Places "sampled" flag, the
capped band, corroborating modules) and `rec_trust.food_diagnosis_input`
(Food Cost card, Home, hero: weeks of inventory counts in the last 8 plus
corroborating modules). The web hero leaves its own key out of the card
grid.

**Shift Quality** (B4 H3/H4, B1 H2): the read's completeness score
(`shift_quality.confidence`) is a documented Evidence `cap` —
`rec_trust.schedule_evidence`, never `coverage` (which printed a false
"only 62% of the window measured"); the panel carries its own K1 as
`quality.confidence_detail` (key `schedule_quality:read`) beside each
suggestion's, so the pill and the items agree; the sources are everything
a schedule rests on — shifts, the POS, sales and the weather.

At delivery `rec_ledger.present_many` snapshots it on the episode
(`confidence_pct`, `evidence_pct`, `accuracy_pct`, `accuracy_n`,
`freshness_pct`, `freshness_as_of`, `trust_version`) and on every `shown`
event, noting a move of 10+ points between showings (`confidence_moved`).
A surface that shows no confidence yet still gets accuracy and freshness
snapshotted. `feedback.sync` fills `intel_rec_events.confidence_at` from
the snapshot.

**The admin check** (`/admin/api/calibration`, `admin_ops.confidence_
calibration`): because the % is not a probability it is checked for
ORDER, not for "72% comes true 72% of the time" (B2 #1, #9).
`confidence_engine.ordering` puts measured results in support bands
(0–49, 50–74, 75–100) with the improved share and its 90% Wilson range; a
higher band whose range sits wholly below a lower band's is a violation,
and each becomes an admin alert row (`alerts` — never an SMS), overall,
per kind (floor 5) and per dimension (floor 20). Spearman's rho between
the % and the result rides beside it. Only support-score snapshots
(`trust_version` ≥ 2) are judged; older ones measured something else and
are counted apart (`versions`). Each result is scored on the confidence
the owner TOOK it at — the latest showing at or before the first
acceptance — when one carried a snapshot, else the first showing
(`scored_on`); results are counted by the learning rule (`learned_verdict`
with the tracker's `concurrent` and `baseline_overlaps_trigger`, i.e.
`outcomes.result_counts`) and one per overlapping window per restaurant
and kind (`rec_learning._one_per_window`). The decile reliability table
and the Brier score stay, labelled "not the meaning of the %".
`admin_ops.recommendation_calibration` (the dollar pairs) reads verdicts by
the same rule.

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
  `intelligence.recommendation_success`, asserted anonymous; the success
  prior from the CAPPED counts shrunk toward the base rate), otherwise
  even (0.5) for acceptance and the kind's **base rate** for success
  (`rec_learning.base_rate`, re-audit B2 #7: a success prior of 0.5 ranked
  a never-measured kind above one that measurably worked — 2 of 10
  improved weighed 0.867 against 1.0). Measured results are counted one
  per tracker and one per overlapping after-window on a number, the rule
  `kind_record` uses; the worse-result penalties and dollar pairs come
  from the same set. With nothing learned the weight is exactly 1.0. The
  model reads a year of episodes without their `shown` rows (only whether
  each has one — B18).
- weight = 1 + mean over the kind and its tags of
  0.2 × (acceptance − prior) + 1.0 × (success − prior), times the kind's
  dollar calibration (median measured ÷ predicted, ≥ 3 pairs, shrunk toward
  1, held to 0.8–1.2 — from `CALIBRATION_WIDE_PAIRS` (8) pairs to 0–1.2:
  2 of 10 realising $1,000 and 8 realising $0 on $1,000 estimates showed
  $800 under the floor, $231 now, B2 #8), **bounded below at 0.75 and above by a ceiling that
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
  the ratio with the note "adjusted from N measured results".
  `rec_learning.attach_dollar_calibration` puts `dollars_adjusted`,
  `calibration_n`, `calibration_note` and `realised_mean` (the mean monthly
  dollars the kind's measured results actually realised, no clear change =
  $0; None below 3 pairs — shown beside the estimate) on every Home card, the one-thing
  hero and every DSR action; clients show the adjusted figure when it is
  non-null. The raw `dollars_monthly` stays on the item and is what the
  ledger snapshots as `dollar_value` — the ratio is realised ÷ dollar_value,
  so snapshotting the adjusted figure would feed each correction into the
  next ratio.
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

### Every recommendation surface (round 2, Group T)

One K1 object, assessed through `rec_trust.assess`, reaches every surface
that carries advice — never a band word a model wrote, never a figure
re-derived on the client:

- **A model-written line** (the Labor / Food / Marketing reads' numbered
  lines, Intel's "how to improve", an Ask suggestion, the digest's "This
  week's move") takes its Evidence Strength from the data the model was
  handed — `client_api.labor_read_evidence` (days of shifts with sales,
  the labor diagnosis's input), `food_read_evidence` (ISO weeks of counts
  in the last 8), `marketing_read_evidence` (posts with measured
  performance in 8 weeks), the competitors compared, the Ask answer's own
  live reads, the week's reviews — flagged `inferred` (the PARTIAL cap:
  never high on the data alone) and capped low when the read carried a
  figure that did not check out (`client_api.read_line_confidence`).
- **A cross-module link** uses one input on the one-thing card and the
  What connects card (`business_intelligence.link_evidence_input`).
- **A fact** — reviews waiting, a failing sync, the schedule not built,
  unacknowledged issues, items critically low or running low, people over
  40 hours — carries no confidence (B4 H5; group P took the running-low
  brief line and the group view's counts off it too).
- **Outbound** (emails, pushes): one line, `rec_trust.outbound_label` —
  "72% confidence · data through 9/23/26" — the K1 label and the stalest
  source's date.

"Not for us" is read by advice signature (`insight_store.advice_signature`)
on the AI-read lines, Shift Quality items, Ask suggestions, the digest's
move and the quiet-night push, as it already was on Home, the one thing,
the DSR and Reviews' Do today. A whole-schedule recommendation
(`schedule_to_target`, `optimizer`) has the subject `schedule:whole`, so
declining one weekday's trim no longer silences it (B4 L4).

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
  whole weeks for a per-day metric, else the stated band alone): band =
  max(stated, 1.645 × σ_L × √(1 + L/B)), a two-sided 10% false-alarm rate
  that the stated floor only lowers. The weekly path (days 42–111 of
  history) corrects for persistence (re-audit B2 #6): with ρ the weeks'
  bias-corrected lag-1 autocorrelation, held to [`AUTOCORR_FLOOR` 0.5,
  0.95], σ_L = s₇ / √(E[s²]/σ²) × √(VIF(ρ, L/7) / (L/7)) — weeks read as
  independent had 28.7% of do-nothing trackers "move" while stating 10%
  (probe p2 S3); now 11.0% against 10% (`noise_band` also returns `rho`). The tracker stores
  `noise_band`, `noise_sigma`, `false_alarm_rate` and `band_basis` at its
  start and every read of it (evaluation, re-check, accrual, the grade, the
  interim reading) uses it. A 7-day caller passes `window_days=7`.
  The weekly review and the digest's "trending" line read one band,
  `weekly_review.week_band`: max(stated band × √(28/7) — `band_scale`, the
  floor a restaurant with thin history gets — , this restaurant's own
  7-day band). The own band is already measured at seven days, so it is
  never multiplied by the scale again; the stricter of the two stands.
- **One success definition (CA2 #4).** `rec_learning.learned_verdict` is
  the only mapping; `outcomes.result_counts` is its value twin (not an alert
  read, a routine supplier order or advice not taken; not disowned; no
  "something else changed" check-in; not measured against its trigger
  window; not **confounded** — `outcomes.confounded`, re-audit B2 #5: a
  stored `concurrent` entry of any kind, another change on the same
  number, a trend already under way or a level shift) and a result counts
  in learning exactly when it counts in delivered value (the learning ==
  value test; `_COUNTS_SQL` holds the same rule for accrual). Probe p2 S4
  (a drifting restaurant, nothing changed) read 29.7% improved, all
  counted; now 9.8% of what counts.
- **A level shift at the trigger (re-audit B2 #10).** A lasting step inside
  the trigger window (a wage rise) never returns to the mirror baseline, so
  doing nothing read "worsened" 35.5% of the time (probe p2 S7).
  `outcomes.level_shift` — the trigger reading already on the move's side
  of the baseline with at least half the move, and the after-window not
  past the band beyond the trigger reading — adds a `level_shift`
  concurrent entry ("Already at this level before the change started");
  the result is shown and never counted (3.0% now). `features.
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
  as `strength_pct` with its label and basis (`patterns.strength_fields`).
  Ask's context line carries the pattern's MEASURED figures instead — the
  restaurants behind it, Cohen's d and p — "an association, not a cause and
  not a probability" (R9): a composite percentage in a prompt came back as
  the model's own "I'm 62% sure".

## Restaurant DNA, learning effects and prediction (Benchmarking audit, 9/24/26)

**Level 1 — the restaurant's own profile, live now.** `intelligence/dna.py`
measures each restaurant, week by week, on the dimensions its own tables
can measure (BM4 §5.1): sales (volume band from stated edges on mean daily
sales — the band index is stored, never the dollars; Friday-to-Sunday share;
share rung before 4pm from the POS's hourly totals, or for a POS that cannot
be asked during the day (RPOWER) the nightly Daily Sales Report's hourly
split — the schedule_outcomes split is not used, it divides a day by a
stated 0.4 morning share; busiest vs quietest month; the service model, only
from a CONFIRMED profile (S6); the restaurant type, only when the owner SET
it (S7, its own dimension — S6 no longer falls back to it); average-ticket
band, hours open a week and the urbanity band from Intel's competitor
distances (S8–S10, from the features pass's structural block); residual sales
swing; 13-week sales trend; forecast skill vs a naive guess), labor (labor %
and labor % swing only on the owner's own pay rates — blank, "needs your pay
rates", on the assumed $26/hr, as the bands refuse it;
hours per $1k, how closely hours follow sales, overtime share,
weeks published, coverage and no-show issues per 100 shifts, retention,
median tenure), guests (rating, negative share, rating change, wait and
service complaints — the honest proxy for service speed, which has no data
source — reply rate, replies within a day), food and loss (food cost %, its
week-to-week swing, waste % — only when waste is logged in at least 6 of 8
weeks — waste-logging regularity (F3), days between counts, comps and
voids), marketing (posts) and the management loop (recommendations taken,
accepted changes made, measured improvement rate, data completeness ×
Data Health). A dimension below its minimum data is None with what it needs
("needs 28 days of sales in the last 8 weeks (has 9 …)"), never 0. No
"personality" labels: a profile is measured figures. `GET /api/dna` +
`/mobile/api/dna` return `dna.profile` — label, value, display text,
trend against 4 weeks ago, basis, needs — projected by module view
permissions.

**Similarity.** `dna.distance(a, b)` = sqrt(Σ w·δ·(z_a − z_b)² ÷ Σ w·δ)
over the dimensions both measured; a categorical mismatch counts as a
2-SD difference. Both rows are re-normalised from `raw` under ONE norm set
(the caller's `platform_norms()` read once per pass, else the stated
anchors) — never the z each row stored under its own night's norms (R4-17).
Structural weights are STATED (S1 volume 0.20, S2 weekend share 0.15, S3
daypart 0.10, S4 seasonality 0.05, S5 beverage 0.05, S6 service model 0.12,
S7 concept 0.08, S8 ticket band 0.12, S9 open hours 0.08, S10 urbanity 0.05
— they sum to 1, and a test holds it); two profiles are comparable at ≥ 60%
shared weight and ≥ 4 shared structural dimensions, or at ≥ 40% and 3 when
they share size, ticket and service model (so a non-Toast restaurant with
under 12 months of sales can be placed, R4-16). Prediction weights add the
target metric's baseline dimension at 0.25, renormalised. Weights become
fitted only when the admin ordering check can judge them. Similarity reads
only rows at the current `DNA_VERSION`.

**Effect sizes in learning (BM4-6).** `feedback.sync` writes `metric`,
`effect_pct`, `effect_z` (delta ÷ the restaurant's noise sigma), signed so
positive is better, plus `baseline_kind`, `after_end` and the episode's
`tags_json`, onto each `measured` row — only for a result the learning counts
(a clear `learned_verdict`, one per overlapping window). Anything else has
them cleared.

**Waste-logging gate (BM4-16).** `features.cross_restaurant_view` withdraws
`waste_sales_pct_28d` (None, never 0) from every cross-restaurant read —
`latest_by_restaurant`, `weekly_by_restaurant`, so bands, patterns and
trends — unless `waste_log_regularity_8w` ≥ 0.75. The DNA applies the same
gate. The restaurant's own screens still see its waste %.

**Peer priors (BM3-12).** `scoring.kind_stats` takes `window_days`
(priors pass 365) and `half_life_days` (`*_recent` figures). When the cohort
record lowers Historical Accuracy's prior, the basis says so ("— Pizza on
Cavnar saw this rarely help (0 of 20), which lowers it"); it never lifts it.
A kind this restaurant has no record of is RANKED with help from similar
restaurants' results (`scoring.similar_prior`: DNA similarity × recency,
capped per restaurant, over the privacy floors and 10 results), bounded to
[0.9, 1.1] and said: "ranked with help from N similar restaurants' results".

**Borrowed headcount (BM3-13).** A schedule whose slots come from the
borrowed starting headcount caps the Shift Quality Evidence at 49 ("N shifts
use other restaurants' staffing, none of yours yet"), lifting in proportion
as the restaurant's own typical headcount covers the slots
(`rec_trust.borrowed_slots` / `borrowed_cap`).

**Prospective patterns (BM4-5, dormant).** Beside the cross-sectional
hypotheses, `PROSPECTIVE_HYPOTHESES` read the behaviour from a restaurant's
week-t feature row and the outcome as its change to week t + 13, one pair per
restaurant, permuted WITHIN each restaurant type (only types with 5 per side
contribute; a Mantel–Haenszel-style weighted difference), so a type
difference cannot pass as an effect. Every stored pattern's evidence carries
`prospective` and `pooled_types` (true for the all-types group).

**Prediction (BM4 §5.3, dormant).** `predict.predict_effect` — neighbours by
DNA distance outside the viewer's organisation (`privacy.org_key`, the rule
every band uses), within its confirmed service model; a result counts only
when the neighbour STARTED where the viewer is — its target dimension within
1 z of the viewer's, read from its DNA row of the week it took the advice (up
to 3 weeks before), not today's; taken effects weighted by similarity × the
per-restaurant cap; the control arm built the same way — untaken results of
the same kind (read from the key by `feedback.kind_of`, the taken rows'
vocabulary), the same tags, windows overlapping the taken results' (none
without them), weighted and capped alike, from ≥ 5 restaurants and ≥ 3
organisations; each arm a weighted Harrell–Davis median (never one
member's own effect); a restaurant-cluster bootstrap for the 80% interval;
the figure and interval in whole percents; floors ≥ 5 restaurants from ≥ 5
organisations, ≥ 10 capped results, n_eff ≥ 8, ≥ 5 untaken; "mixed results"
when the interval spans 0. Returns `{kind: "prediction", value,
n_restaurants, n_orgs, interval, basis}` — never a neighbour, distance, date
or dollar. Likelihood words never come from it. `run_weekly` loads the DNA,
organisations and norms once per pass and checks its wall clock before every
(kind, metric) pair, its cursor resuming mid-restaurant.

**Peer-benchmark freshness (BM3-9).** `data_freshness.SOURCES["cohort"]`
("Peer benchmarks": lag 7, grace 7, horizon 49 days), dated by the newest
`intel_benchmarks.computed_at` for the restaurant's type (else all types);
`read_platform_intelligence` rests on it; Data Health labels it "Peer
comparison".

## Pattern-discovery architecture

Hypotheses are data (`patterns.HYPOTHESES`), not code: behaviour feature,
split, outcome feature, direction, sentence template. Adding one is one
dict. The test is a seeded two-sided permutation test on the difference of
means (2,000 shuffles; exact enough at cohort sizes and free of SciPy),
with Cohen's d as the effect floor and Benjamini–Hochberg across the
night's hypotheses. Sentences say counts and effects and never a name.
Every hypothesis compares restaurants' latest rows side by side — the
behaviour and the outcome cover the SAME weeks — so a sentence says "at
the same time as", never "over the following" ("Across 14 pizza on
Cavnar, those replying to at least half their reviews within a day saw
their rating move 0.30★ higher than those that did not, measured at the
same time as the replying (not after it)"; BM1-16, BM4-5). A pattern found
across every restaurant on Cavnar pools every type and carries
`pooled_types`; `patterns.pooled_on_economics` keeps such a pattern about
labor, food cost or waste out of `support_for` (the K1 pattern-support
factor) and out of Ask's context, since a type difference would pass as a
behaviour effect.

## Privacy safeguards

- `intel_features` holds ratios, rates and counts. Sales appear only as
  denominators inside ratios; dollars are never stored there.
- Every cross-restaurant answer passes `privacy.cohort_ok(n)`; below the
  floor it returns `{available: False, reason}`.
- `privacy.assert_anonymous(payload)` rejects any payload carrying a key
  from the forbidden list (`restaurant_id`, `name`, `owner_email`,
  `employee`, `phone`, `place_id`, `sales`, `revenue`, …) on its way to a
  cross-restaurant surface. The admin dashboard, patterns, benchmarks and
  Ask context all pass through it, and the tests assert it. It scans string
  VALUES too (NS6 §B finding 2): a tenant's name (every `restaurants.name`
  and `location_name` of four or more characters that is not a generic word,
  cached per process for 60 s and dropped on a restaurant change), a
  restaurant id written out, or an email address raises. `deny_names`
  replaces the name list in a test.
- Effects are rounded (`round_effect`) so a two-member difference cannot be
  reversed into a member's value; below the floor no effect is emitted.
- **Quartiles are not rounding-safe on their own** (NS4 M6, NS6 §B finding
  1): linear-interpolated quartiles of five values ARE the 2nd, 3rd and 4th
  members, so an owner who knew their own figure read three peers' exact
  labor % off the band. What a restaurant is shown goes through
  `benchmarks.published()`: its own row taken out of the band (the member
  values are kept, sorted and unlabelled, in `intel_benchmarks.vals_json`,
  never selected into a payload), at least `MIN_QUARTILE_N` (8) OTHER
  restaurants, and each quartile rounded to a coarse per-metric step (0.5
  points for a %, 0.1★, 0.05 for a rate). Under 8 the band is withheld.
  `benchmarks.band()` stays internal (staffing scales its median to people).
- **Organisations, not locations** (Benchmarking #9, BM1-5, BM2-4, BM4-2):
  every floor counts distinct organisations (`privacy.org_key`:
  `organization_id`, else `location_group|owner_email`, else the restaurant
  alone). A published band needs ≥ 8 others from ≥ `MIN_ORGS` (5)
  organisations, the VIEWER'S WHOLE ORGANISATION is taken out of the band
  it sees (each member value sits beside an org hash in `members_json`), no
  one organisation may be over a third of it, and one Google listing counts
  once. A test or internal account (`exclude_from_learning`) is never a
  member.
- **Disclosure control** (#42): quartiles come from the Harrell–Davis
  estimator (a Beta-weighted average of every member, never one member's
  figure), the rating step is 0.25★, and what is stored for a week is
  frozen for it — a member joining or leaving cannot be differenced out of
  two nights. Owner-facing patterns carry no group means and need ≥ 8
  organisations a side (`patterns.owner_projection`, #11); the admin
  projection keeps the means. The admin cohort table is rounded as
  published and is_admin only (#47); the public status page says "one or
  more locations", never a count.
- **The quality gate** (#39): never an `other` or untyped group; a band whose
  interquartile range is over its metric's ceiling
  (`metrics_registry.spread_ok`) is withheld as "too spread out for a middle
  to mean anything".
- **Nothing stale is served** (NS4 H5): a band older than
  `MAX_BAND_AGE_WEEKS` (8), this restaurant's own figure older than
  `MAX_OWN_AGE_WEEKS` (8), and an active pattern not re-confirmed in
  `MAX_PATTERN_AGE_DAYS` (56) are left out, and every band and pattern
  carries `as_of`. `patterns.discover` retires every pattern whose cohort
  has dropped below the floor, not only those among cohorts tested that
  night.
- **Outside benchmarks** (an NRA median, an operator rule of thumb, Luca's
  revenue per star) are not this engine's: they live in
  `benchmark_registry.py` by restaurant type, and a type with no entry gets
  no industry figure anywhere.
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

## The Benchmark Engine on the owner's screens (Benchmarking audit, workstream O, 9/24/26)

Every comparison an owner sees on a screen now comes from `intelligence.engine`
through `benchmark_views` (L2), which shapes and never compares:

- **How you compare** (`benchmark_views.card`, `/api/benchmarks/card` + mobile
  twin) on Labor, Food Cost, Reviews and Marketing, and a compact Home strip.
  It says who (the engine's headline kind: peers → the restaurant's own
  previous 13 weeks → the published figure), how many, as of when, the
  comparison strength % (only for a band comparison; its Why? rows are the
  engine's four strength dimensions), the standing per metric, and one Ask
  action per metric the restaurant is behind on. Below the minimum it says
  "Not enough restaurants like yours yet — here's how you compare to your own
  last 13 weeks" and carries the engine's `why_not` — the self benchmark is the
  headline until peers clear their floors (#18).
- **Location to location** (`benchmark_views.location_compare`, in the group
  Home and the phone's locations sheet): the engine's `location` kind (the
  siblings are the restaurant's `privacy.org_key` organisation, named by
  `location_name`; a format or economics metric compares only locations in
  the same confirmed partition, and a location on the $26/hr default or
  with an irregular waste log is left out — "your locations serve
  differently" when that leaves fewer than two; closed without a viewer), each
  location first read against its own normal (the engine's `self`), then
  against the median of the owner's other locations, a gap called only when
  it is wider than the location's own noise band and the others' median one
  combined. The group "strongest / weakest" (home_brief's group brief and
  single-location portfolio line, reporter's group digest) is one rule,
  `benchmark_views.rank_by_rating`: the platform's rating floor
  (`thresholds.GROUP_RANK_MIN_REVIEWS` = `RATING_MIN_REVIEWS`), each location's
  own-normal verdict, and a name only for a gap beyond 2 standard errors
  (`thresholds.RATING_SIGMA`).
- **Module helpers read the engine** (#48): `thresholds.labor_industry_benchmark`
  and `cogs`'s food-cost band (`cogs._engine_industry_food_cost`, which also
  feeds the dish colours `cogs.dish_reference`) read the engine's `industry`
  comparison, and obey its `comparable` flag: a band measured differently is
  context beside the label, never the label, a dish colour or a dollar gap
  (re-audit #2); `review_intelligence.competitor_benchmark` reads Intel's market
  definition (`competitor_intel_format`, the engine's `market` kind). The
  waste label (`inventory.analyse_inventory`) is a read against the owner's
  target, not a comparison with other restaurants, so the engine does not
  apply there; it now reads "Under / Near / Over / Well over target". No old
  helper was left without callers.
- **The local market standing** (`competitor_intel_format.market_standing`,
  #38): at least 3 rivals matched on cuisine and price, the widened-radius
  fallback out of the average, one venue capped at 500 reviews of weight, n
  and radius said, and a symmetric neutral tie inside one standard error.


## The engine's own normal, eligibility, strength and serving (re-audit, workstream A, 9/24/26)

- **Your normal** (`engine._self`, Top-50 #1, #39). A feature row is a
  28-day window stored weekly, so neighbouring rows share three of their
  four weeks; the old ±1·(1.4826·MAD) band over them called 45–54% of an
  unchanged restaurant's weeks "better" or "worse than your normal". The
  swing σ is now the pooled variance of rows a whole window apart
  (`window_sigma`: rows grouped by weeks-back mod 4, so no two share a
  day), from at least `SELF_MIN_POINTS` (7) baseline weeks and 3 degrees of
  freedom; a verdict needs |gap| > t₀.₉₇₅(df)·σ + 1.25·σ/√n_eff (n_eff = the
  independent windows the baseline spans), never under the metric's step —
  a steady restaurant is called better/worse in ≤10% of weeks, pinned by a
  simulation test. With a year of history (the same week 51–53 weeks back
  and its own 13-week baseline) the normal is seasonal: the recent median
  moved by how the same weeks moved last year, the threshold ×√2, labelled
  "your own previous 13 weeks, adjusted for this time last year". The
  payload carries `noise_band` (the threshold — `location_compare` reuses
  it), `sigma`, `df` and `basis` (recent | seasonal).
- **Own-figure eligibility** (`engine.own_eligible(restaurant, metric,
  features_row) -> (ok, why_not)`, #11, #12): the restaurant's own figure
  must pass what a band member passes. Labor-cost metrics
  (`LABOR_COST_METRICS`) need a sourced labor cost
  (`engine.labor_cost_sourced`): the owner's blended rate, a role-rate
  `_default` or flat rate of the owner's, or role rates pricing at least
  80% of the last 28 days' hours (`labor_rate_coverage`) — one priced role
  is not enough. Waste % needs `features.waste_logging_regular`. An
  ineligible figure is not ranked by peers, platform or location ("set your
  pay rates to compare labor cost", "waste isn't logged regularly enough to
  compare (2 of 8 weeks)"); the published figure stays as context with
  `own_eligible` False; the labor self comparison stays (its own history is
  on the same assumed wage) and says "on Cavnar's assumed $26/hr, not your
  payroll"; an irregular waste % gets no self verdict, and irregular weeks
  never enter a waste baseline. `compare()` returns `own.eligible` and
  `own.why_not`. Band MEMBERS are still judged by
  `thresholds.labor_cost_basis` (one priced role counts) in
  `jobs.member_info` — a stricter viewer than member until that reads
  `labor_cost_sourced` too.
- **Standing** (#16): the "about the middle" margin is √(se_median² +
  σ_own²) — the band median's uncertainty and the restaurant's own swing
  (`_own_sigma`) — never under the step; a quartile word needs the figure
  past the quartile by the whole margin, and with too little own history to
  know σ_own the word stops at above/below the middle. Every band
  comparison carries `outcome` (`engine.outcome_words`).
- **Comparison strength** (#15): the group's size is a cap — 40% at 8
  others, +3 a restaurant, so the 75% ranking level needs about 20, and
  never above `STRENGTH_MAX` (95). Under the cap, the geometric mean of
  freshness, `own` (THIS metric's own figure: reviews behind a review
  measure against four times its floor, else how many of the previous four
  weeks measured it, times its age), `similarity` (`engine.granularity`:
  all types 0.6, service model 0.75, × bar-led 0.85, × menu family or
  sales band 0.95, −5% a coarser ladder rung), `spread` (the IQR as a share
  of the quality gate's ceiling, `metrics_registry.spread_ratio`) and
  `orgs` (separate owners ÷ 10). A guessed type holds it at 0
  (`INFERRED_TYPE_CAP`; `_peers` refuses a guessed type first, so this is
  the rule restated, not a path taken).
- **Counts** (#44): `measured` and `members` are counted after the viewer's
  organisation is taken out (members from each band's server-side
  `members_json`), so "measured at k of m" agrees with n.
- **Serving** (#28): `payload_for` drops the metrics of modules the login
  may not view before computing anything, reads every kind but `location`
  through `compare(..., use_cache=True)` — a hit only when the row is under
  36 hours old, `ENGINE_VERSION` matches and its `inputs_key` (a hash of
  the profile, pay rates, targets and organisation) matches the
  restaurant's current settings, so a saved profile or pay rate misses at
  once — and computes `location` live once per request from one
  sibling-scoped read (no platform-wide scan per metric). A cache miss
  reads the feature history once for the remaining metrics; the band kinds
  reuse the rows already read and the viewer's organisation hash once per
  request.

## Peer groups: the confirmed profile, the partition and the ladder (Benchmarking audit, workstream P, 9/24/26)

**Who a restaurant is compared with.** The owner confirms a restaurant
profile in Account → Restaurant profile (web block, iOS sheet
`AccountRestaurantProfileSheet`, admin brand modal; routes
`/api/account-settings/restaurant-profile` and its mobile twin, owner-only):
how it serves, its concept, bar-led, ownership and the year it opened. The
save is the confirmation (`profile_source='set'`, `profile_confirmed_at`)
and a `profile_changed` activity event records the old and new values. A
type Cavnar guessed (`categories.infer_detail`: format words before cuisine,
the name before the menu, the restaurant's own Google types, a confidence)
only pre-fills "We think you're X — is that right?": it never joins a
group, never counts toward a floor, and no published dollar figure is
computed on it.

**The partition** (`categories.partition_key`) is the hard split a band is
read from: the service model for every format metric (rating, marketing
rates, and behaviour metrics' like-for-like rung); × bar-led for labor
metrics; × menu family (protein / starch / mixed, from the concept) for food
cost and waste; a staffing ratio also by sales band once the restaurant's
own is measured. A partition change needs the owner's confirmation or
`jobs.DRIFT_WEEKS` (4) consecutive weeks of measured drift (alcohol share
against bar-led); a change is logged as `comparison_group_changed`, which
`confidence._recent_changes` counts.

**The ladder** (`engine.compare`, peers kind): the restaurant's own figure
must clear its floor ("about N more measured days to a comparison"); then
R1 the published figure for the confirmed type (quoted as context with its
definition when it measures something else — the NRA labor median includes
benefits, Cavnar's labor % is wages from shifts, so it is never compared or
blended); R2 all of Cavnar for behaviour metrics only; R3 the confirmed
partition. A small group's band is blended toward a like-for-like published
median by n/(n+8) and says so. The ledger (`intel_peer_assignments`) records
the rung reached each week; reaching a higher rung is logged as
`benchmark_rung_up`.

**Structural features** (`features.STRUCTURAL_KEYS`, #43): `ticket_band`,
`volume_band` and `daypart_mix` (one definition with DNA), `alcohol_share`
(DSR categories, only where mapped), `delivery_share`, `weekly_open_hours`,
`urbanity_band` — band indices and shares, never dollars, None when not
measured. They are peer coordinates and the drift detector's input.

**Targets and the labor cost basis** (#13, #14; re-audit #3, #10, #45): a
target records where it came from (`thresholds.target_source`: set / seeded
/ default), and every surface that judges a figure against one reads
`thresholds.target_for(restaurant, kind)` → `{pct, source, label,
alerts_allowed, phrase}` (kind `labor` | `food`): the Food Cost label and
dish colours (`cogs`), the digest's labor tag (`reporter.labor_tag`), Home's
labor attention item, the group brief's labor issue and the value
opportunity's label. On Cavnar's starting target nothing reads "Over" in
red: tags, dish colours and severities cap at a watch and name the starting
target; no over-target alert or issue fires (`alerts_allowed` False).
`tests/test_targets_published_figures.py` ratchets bare target reads.
Confirming a type seeds a target the owner has not set only from a
PUBLISHED median measured the way Cavnar measures it
(`metrics_registry.definition`) for the confirmed service model — none
exists today (the NRA labor median includes benefits, the NRA food median
counts non-alcohol beverages), so every confirmation keeps, and resets the
value to, Cavnar's default. `models.backfill_seeded_targets` (boot, data
only) puts any seed that no longer qualifies back to the default. An
explicit save of any value — the admin form names the fields it saw
touched — is the owner's, even 30%; an owner-entered $26/hr is theirs too
(`restaurants.hourly_rate_source`). The labor cost basis
(`thresholds.labor_cost_basis`: role_rates / owner_blended / default;
pos_wages reserved) withholds the gap-to-target dollars and a place in
labor-cost bands while it is the $26/hr default. No "$ under industry" is
computed at all (`labor_vs_industry_*` are sent as 0): the only published
labor figure is not like for like.

**Published figures** (`benchmark_registry`, re-audit #4, #32): every
entry declares its `definition` (None when the source does not say what it
counts; bands derived from the NRA food median inherit its definition), the
`service_models` it describes, and a `max_age_years` counted from its data
year. `lookup(metric, concept, published_only, definition, service_model)`
is the one lookup the engine's `industry` kind, the blend, target seeding,
the peer ledger and the sales audit share: an unknown definition is not
comparable, a counter-service restaurant never gets a full-service figure,
and an entry past its age limit is no entry. The sales audit sizes dollars
only against a published figure or the owner's own target; a rule of thumb
and the unsourced blended food + beverage band are shown as context.
`benchmark_registry.facts` carries the shared fact keys (`metric`,
`better`, `comparable`, `definition_note`, `inferred`, `own_value`,
`standing`, `strength_pct`).
