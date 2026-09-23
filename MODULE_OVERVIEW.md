# Module Overview — Cavnar AI

One section per product module: what it does, the key files, the invariants an audit checks, and what NOT to touch casually.

---

## Reviews

**Files**: `fetcher.py` (Google/Yelp pull), `analyser.py` (Claude sentiment/category/summary/severity/entities), `drafter.py` (AI reply drafting), `review_intelligence.py` (the consultant layer), `client_api.py`/`mobile_api.py` review routes.

**Flow**: scheduled fetch → `analyser.py` scores sentiment/urgency/severity and extracts the dish, role and daypart the guest named → `drafter.py` writes a draft reply → owner approves/edits/skips → posted back to the platform (where API access allows) or copy-pasted. Separately, at 6am daily, `scheduler.run_review_diagnoses` clusters the negative reviews and produces a root-cause read per cluster.

**Invariants (audit #10)**: a Google Business location is matched on `google_place_id`, never fuzzy name matching. A review's time for trend/period purposes is `review_date` (when it was posted), not `fetched_at` (when Cavnar AI saw it) — a backfilled batch of old reviews must not appear as "this week's activity." An unanalysed review (fetched but not yet scored) still counts toward totals — it just lacks sentiment/urgency until the analysis pass catches up. A drafted reply is never allowed to invent a commitment ("we'll refund you," "come back for a free X") the restaurant hasn't actually offered.

**Invariants (audit #13 — the consultant layer)**: there is exactly ONE review time axis, `models.REVIEW_TIME_AXIS_BARE`, and every window in every file uses it — there were three answers to "the last 30 days" and the same AI prompt once carried two of them. A pattern claim needs volume behind it on both sides (`MIN_CLUSTER_MENTIONS`, `MIN_TREND_REVIEWS_PER_WEEK`, `MIN_TOPIC_TREND_MENTIONS`), and a "concentration" that holds less than half its cluster is not reported at all. A rating direction carries a real confidence, computed by the same slope-agreement scorer `waste_trend` uses for waste dollars — never a bare monotonic check over three weekly means. A cause is only ever asserted from a stored diagnosis that cited real review ids; with no diagnosis, no cause is stated. The cross-module block distinguishes "no data" from "nothing notable", and sample data is refused rather than reported. A revenue figure is a labelled forecast, a range, shown with its inputs, and absent entirely when either input is missing. And a claim's kind (`ai_guard.CLAIM_KINDS`) travels with it to both clients, so measured, inferred and projected never render alike.

---

## Labor

**Files**: `labor.py` (ingestion, aggregation, the one model call), `schedule_engine.py` (the deterministic pipeline around that call), `schedule_rules.py` (compliance rules, role floors, the `Constraints` object and the violation sweep), `schedule_requirements.py` (pure: the per-shift requirements table and people facts the prompt carries, from the scorer's own inputs), `schedule_learning.py` (retimes, headcount, role changes and leader swaps the manager keeps making; implicit recommendation acceptance; the edit predictor (`predict_row_edits`, smoothed per-feature edit rates over the manager's own finished drafts, measured by `edit_prediction_backtest`); weights fitted to outcomes (`calibrate_weights`), owner-applied; no-shows by weekday and standby days; the overtime forecast), `staff_settings.py` (roster, per-person facts, pairs, reliability), `demand_signals.py` (events and reservations per date), `schedule_versions.py` (versions, diffs, learned edits), `shift_requests.py` (drop / decide / claim), `shift_quality.py` (pure scoring engine), `scheduler.py`/`strategy_jobs.py` job pieces, `models.py`'s staff/schedule tables.

### The schedule pipeline (`schedule_engine._run_schedule_job`)
1. `schedule_rules.build_constraints` gathers everything once: the roster (`staff_settings.roster`, deactivated names excluded), availability, notes, approved time off (blocks the date) and pending time off (a warning), per-person hours envelopes and daypart windows, minors, the rules, the role floors, and the already-published tail of the payroll week here and at sibling sites (hours count toward the ceiling, rows toward rest, a sibling's date blocks the person).
2. The prompt opens with one ranked PRIORITIES list (hard constraints, shift requirements, leadership, the hours ceiling, quality preferences) and carries a SHIFT REQUIREMENTS table (per date × daypart: people per role = max(owner floor, typical headcount), the day's hour target, the demand level and leader need the scorer uses), the experienced and developing staff by name, each person's usual days and dayparts, the rules, dated demand signals, pairings, reliability and what the manager keeps changing (`schedule_requirements`); the model answers a JSON schema (CSV fallback). Rosters over `CHUNK_ROSTER_THRESHOLD` or a truncated answer generate the week in date slices and merge.
3. Deterministic backstops, every one asking `Constraints.can_work` / `max_hours` / `rest_ok` before it adds a row: close times, the server overlap cap, role floors (`_ensure_role_floors`), and a coverage top-up that fills only a day-and-role genuinely thin against its own average — the labor budget is a ceiling, never a quota. Every fill-in and trim pass (role floors, top-up, section cap, stagger, budget trim) chooses among its own legal options the one that costs the week the least Shift Quality, scored with `shift_quality.LocalScorer` — which re-evaluates only the dates a move touches (the week-level measures every time); the pass's own order breaks ties and takes over past its time budget. Arrival times have one legal outcome and are enforced as before.
4. `schedule_rules.violations` sweeps the finished week; `shift_quality.apply_fixes` puts somebody legal on each hard breach; what nobody legal can take is marked `needs_review` and named. A missed run of days off (`days_off`, soft — `schedule_rules.fixable`) is repaired too: the fewest of the person's shifts that give them the rule's run go to legal teammates, the least score cost, kept only when the sweep shows no new hard breach and nobody newly short of their own days off. Rows the engine could not vouch for are never dropped.
5. `shift_quality.score_rows` grades it, with a `why` for every assignment; the summary is `schedule_versions.diff` against the last published week (deterministic — the model's own note is kept separately as `narrative`); the result is persisted with its version, review and timing.
6. Publishing (`client_api._publish_schedule`) refuses with the blockers until a human acknowledges; auto-publish holds. Only a published week reaches the staff portal.

After the backstops and before the sweep, three economic passes (`schedule_economics`): arrival times by role are enforced like close times, identical starts are staggered along the day's measured sales curve, and the week is trimmed back to the hours budget (owner toggle, most discretionary hours first, never below a floor, every removal reported). The week's cost is priced with overtime at 1.5× past the ceiling. The prompt also carries what published weeks actually did by daypart, the rotation ledger, sales per labor hour, what a holiday did here last year, stated and learned staff preferences, who could hold a station, and the cohort's labor-hours-per-$1k ratio when the cohort clears the floor (`schedule_intel`, `intelligence`). A jurisdiction pack (`compliance_packs`) sets rule defaults under the owner's own values.

### Data flow
POS shift CSV (`date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes`) → `labor.py` loads and aggregates → labor % vs `labor_target_pct`, overtime detection (1.5× over 40h on the restaurant's own `week_start_day`-anchored workweek), overstaffed-day detection (high labor % on a day, contrasted against sales) → rendered on the Labor tab (web `#panel-labor`, iOS `LaborView`).

### Shift Quality Engine (`shift_quality.py`)
Evaluates a *generated* schedule, not raw historical shifts. `ShiftContext` (per-shift facts: role, flagged constraints, closing, elsewhere-that-day, prior pattern, availability) feeds sixteen `DimensionResult`s combined via the `DIMENSIONS` registry into one 0–100 score:

| Dimension | Default weight | Critical floor |
|---|---|---|
| coverage | 20 | 70 |
| operational_strength | 18 | 55 |
| leadership | 15 | 60 (owner-written rules only) |
| demand_match | 10 | |
| labor_efficiency | 10 | |
| splh | 5 | |
| coverage_curve | 8 | 60 |
| experience_balance | 8 | |
| training_balance | 7 | |
| fatigue (week level, judged once) | 7 | |
| fairness | 6 | |
| reliability | 5 | |
| pairings | 4 | |
| stability | 2 | |
| cross_training | 2 | |
| preferences | 3 | |

Coverage by the hour also judges every half hour of service against the restaurant's own measured sales curve (three or more same-weekday readings; `staffing_curve.half_hour_needs`): the busiest half hour needs 75% of each role's usual crew and each other half hour that crew scaled by its share of the sales. The capture is hourly, so the half-hour curve is interpolated between hour midpoints and every surface says so; the requirements table the model reads carries the same needs as a compact ramp ("Server 2 from 5:30pm, 3 from 6:30pm"). With no intraday data it is the owner's floors alone, as before. No requirement — whole-shift or half-hour, in the prompt or the score — asks for more front-of-house people at once than the section count (`staffing_curve.cap_requirement`, counting `foh_roles` together; the backstop and the repair loop count the same roles); where the history runs over the cap the review tells the owner the cap is probably wrong (`section_cap_conflicts`). Cross-training is judged role by role against the owner's target for that role (`role_cross_training_json`, set with the other per-role rules on web and iOS), else `CROSS_TRAINING_DEFAULTS`; 0 means the role is not expected to flex. Fatigue is a week-level measure (`WEEK_LEVEL_DIMENSIONS`): judged once per person per week, it stands in for the share of each uncapped shift it used to carry (`week_score_raw`), so a tired person costs the week once and a capped shift stays at its cap; it is shown under "Across the week" (`quality.week_dimensions`) and the repair loop sees it through `raw_score`. Preferences scores the daypart and weekly hours staff said they want, only for people who stated them.

**The score is the objective (`schedule_optimizer.py`).** After the hard-rule fix pass and before the owner sees the draft, a bounded local search (add, retime/extend, replace, swap, trim — generated from the weakest dimensions' facts, capped shifts first) takes each move that raises the week, re-checked against the full rule sweep (never adds a hard breach), the hours budget, the section cap on simultaneous servers and pending time off. Every change is recorded with why (`optimizer.changes`), and what no legal move could fix is said (`optimizer.unresolved`, e.g. only one bartender authorised to close). `/labor/schedule/optimize` runs it on the week on screen without saving. The budget trim picks, among equally discretionary rows, the one the score can best spare. A draft whose busy shifts are still capped by a staffing hole has those days regenerated once with what was wrong named in the prompt (`schedule_engine._quality_gate`); the better draft is kept. The review also names who the draft puts past the payroll-week ceiling, the days worth a standby, and the rows the manager is likely to change (`likely_edits`): first the ones matching an edit they keep making, then every row the predictor puts at 50% or more (and clearly above their overall edit rate), each with its likelihood and the rates behind it, once three finished drafts, 60 rows and 8 changes are on file.

**Who works each shift is solved (`schedule_solver.py`, #47).** Before the repair loop, the draft's shape stays the model's judgment (which shifts exist, when, in which role) and the assignment is re-solved: a unit per drafted row (a drafted double stays one unit; overlapping legs are split), each person's legality from the same `Constraints` the sweep uses, and a complete search — most-constrained unit first, forward checking (conflicts, hours per payroll week, days in a row, a manager on every daypart when asked, a Hall check per role and date), branch-and-bound on an admissible bound, then large-neighbourhood rounds re-solved exactly — within the optimizer's budget (12s). Its cost mirrors the dimensions an assignment can move (rotation-aware fairness included); coverage, coverage by the hour, labor efficiency and SPLH are shape-only and constant to it. `score_rows` over every dimension picks between the draft and the solver's weeks; the solver's week is kept only when it scores higher and adds no hard breach, row by row. Its changes lead `optimizer.changes` ("Put Cat on Saturday dinner Server … instead of Ann — Cat is rated 5 to Ann's 2 …"), its stats ride in `optimizer.solver` (proved optimal or stopped on time, seconds, slots). A shift no legal week can staff — nobody legal, more shifts than legal people that day, more than the role can carry this week, or somebody the draft already overcommitted — is kept as drafted and named with why; the rest is still solved. Whether it runs is the week's arm of the live experiment (`schedule_experiments.py`, #50): a hash of restaurant and week (so a regeneration keeps it), recorded in `schedule_experiment_weeks`, pinned globally by `SCHEDULE_EXPERIMENT_PIN` or per restaurant; the admin console's *Schedule experiments* page compares arms on draft acceptance (`schedule_versions.acceptance`), outcomes and Shift Quality, and names no winner under 20 published weeks from 5 restaurants per arm. Owners never see an arm; an arm never adds a model call.

**What the draft learns (#39, #43, #46, #48).** `calibrate_weights` fits each outcome (coverage and no-show issues — only on nights `schedule_intel.watched_dates` says Cavnar was watching — the day's review rating, labor % against target) on every dimension at once (a standardized ridge fit), needs 8 published weeks, 40 shift outcomes and 20 shifts per dimension per outcome, keeps the fitted weight within 30% of the default, moves a weight at most 10% of its default per Apply from where it is now, and names the outcome that drove each change; the owner applies it (web and iOS). `schedule_intel.rotation_plan` plans the rotation per role from the last 8 published weeks — who is due a weekend off (most weekends in a row), who closes next (fewest closes for their shifts), who rests from closing, who works and who has the next holiday — and the prompt carries it as a preference below the rules; the fairness dimension judges the week against it (a person due a weekend off on a weekend shift while somebody in their role has the weekend free, a resting closer closing while the next closer has none). `schedule_economics.splh_objective` sets a sales-per-labor-hour target for every weekday's lunch and dinner from the restaurant's own record, raised by history labor % ÷ target labor % when it runs over its labor target (never lowered below the record); the prompt gets each shift's target and the hours its usual sales carry, the `splh` dimension scores each shift against it and withdraws without sales, and the review reports the week (`splh_report`). A restaurant with no history of its own (nothing published, under two weeks of shifts) gets a starting headcount borrowed from its category cohort (`intelligence.staffing`: people per role family and daypart per $1k of sales, cohort medians over at least `MIN_COHORT` restaurants, scaled by its own sales), marked "(borrowed)" in the SHIFT REQUIREMENTS table and said in the review; the scorer's typical headcount stays the restaurant's own. Below the floor, with no category or with no sales of its own, nothing is borrowed and the record panel says why.

Weights are per-restaurant editable (`quality_weights_json`); `scripts/schedule_eval.py` replays saved weeks through the current code and is the check before any change to scoring. A shift is one date × daypart; a row counts in every daypart it is on the floor for (`present_dayparts`: its start's daypart, plus the other when it covers an hour of that daypart's core window), while its hours and week assignment count once. A profile's demand is settled per weekday from the restaurant's own sales (`profile_for_shift`), never the busiest of the days it covers. Experience withdraws when the history on file is too short for anybody to reach `EXPERIENCE_SHIFTS` and nobody is marked experienced (`staff_settings.experienced`). Fairness asks whether somebody on a closing, weekend or busy shift has more of that kind than their share among comparable people in their role. Labor efficiency treats under the day's target as fine when every required position (and every half hour, where measured) is covered. Invariants that hold everywhere:
1. **No data → `None`, never `0`** — a dimension with nothing to measure withdraws and the rest renormalize. (A leadership dimension with zero ratings used to score `0` and cap otherwise-great shifts at nothing; fixed.)
2. **A critical dimension under its floor caps the shift at its own score** — never an arbitrary extra penalty stacked on top.
3. **Confidence is tracked separately from score.** A shift graded from 2 rated employees out of 9 says so; it isn't hidden inside the number.

`SUBSTANTIVE_DIMENSIONS` gates against fatigue-alone producing a score. What-if swap evaluation (`_SwapIndex`) only considers same-role swaps, bounded by `MAX_CANDIDATE_EVALUATIONS`, for tractable performance on a real week.

### Operational Score
Per-employee 1–5 rating (`staff_capabilities`), owner-set, keyed by employee **name** (POS data has no stable ID). Feeds `experience_balance` and `leadership` in Shift Quality, and is exposed to Ask Cavnar's `read_team` tool. Dormant until someone is rated — a fresh account has zero effect on scheduling until the owner engages with it. Role matching is case-insensitive (`_same_role`/`_role_lookup`) — this used to silently disable every role-based rule.

### UI note
Web `#panel-labor` header shows Sales/Labor as the two headline numbers, the labor figure carrying its own colored (green under target / red over) percent-of-sales badge; the date range sits above them on its own line; "Generate optimized schedule" / "Upload shifts CSV" live with the Schedule section further down, not in the header.

**Don't touch casually**: the invariants above — they're each the fix for a real production bug found by prior audits (see `git log --grep "Shift Quality"` for the history). Any change to dimension scoring must be re-audited the same way (revert-check: does removing the fix make a specific test fail?).

---

## Food Cost (Inventory)

**Files**: `inventory.py` (waste/overstock/reorder), `inventory_ledger.py` (the stock ledger, recipes, menu margins, portion variance), `cogs.py` (actual food cost %), `waste_trend.py` (the trend engine), `food_cost_intelligence.py` (the CFO layer). Panel id is historically `#panel-inventory` (predates the "Food Cost" rename).

Weekly ingredient counts → waste cost, price-drift detection (this week's unit cost vs last), monthly waste projection, purchase-order drafting. CSV shape: `item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week`.

**The five engines and who reads them.** Four financial engines existed and the AI saw one — it narrated waste and could not say whether food cost was over target or which dish was eating the margin. `food_cost_intelligence` is the layer that joins them: it ranks the drivers, projects prime cost, and feeds all of it into both the insight prompt and the root-cause pass.

**Invariants (audit #14 — the CFO layer)**:
- `inventory_history` is written by a SCHEDULED job (`scheduler.run_food_cost_snapshots`, 05:00), not by a page render. The weekly waste series, the multi-week price trends, the price-spike alert and the opening/closing values behind food cost % all read that table — when its only writer was the AI insight on page view, all four were functions of whether the owner opened the tab, and a week nobody looked at was ABSENT from the series rather than zero in it.
- There is ONE week-over-week implementation, `waste_trend`'s ISO-week series. `week_end` is today's date, so "the previous snapshot row" is whenever the owner last looked — that was being labelled "vs last week" and a dollar forecast was built on it.
- A forecast is only projected when `waste_trend`'s own confidence and anomaly checks allow it, and it is STORED (`forecast_log`) and later scored. A forecast nobody checks costs nothing to get wrong.
- The waste-rate denominator is what was actually received over the same seven days (`cogs.purchases_in_window`), never the sum of each item's last order — that covers a different period per item and usually a longer one than the numerator.
- Two different windows, kept apart: `week_start`/`week_end` label the WASTE period (always the trailing seven days); `counted_to`/`window_age_days`/`stock_basis` say how far the CURRENT STOCK figures can be trusted.
- Every displayed annual figure is the displayed monthly times twelve. Round once, derive the rest.
- Drivers are ranked in Python — dollars, then confidence, then ease — and the prompt is told not to re-rank them. Each carries evidence, a confidence, a difficulty and what happens if it is ignored.
- A cause is only ever stated from a stored diagnosis whose validator rejects any driver it was not given (`_validate_diagnosis`), the same discipline `verify_figures` applies to numbers.
- A projection returns nothing rather than substituting a zero, and says which component is missing. `profitability_projection` is PRIME COST, not net profit, and its basis says the labor share came from the shift analysis's own period.
- The inferred-waste gap is attributed per ingredient and per dish (`inventory_ledger.inferred_variance`) — it is over-portioning, prep loss or shrink, and aggregating it to one restaurant-wide dollar figure produced the one number an owner could not act on.

---

## Marketing

**Files**: `marketing.py`, `marketing_drafts.py`, `marketing_publish.py`, `marketing_media.py`, `marketing_links.py`, `marketing_signals.py`, `guest_marketing.py`, `guest_email.py`.

AI-drafted social posts (brand-voice fields on `restaurants`: `voice_notes`, `vibe`, `known_for`, `sign_off_name`, `never_say`), a content calendar, direct publish to connected accounts (Instagram real; Square/Clover honest "contact us" stubs), and the guest text-club feature (`guest_contacts`/`guest_campaigns`, SMS opt-in via `sms_optin_invites`).

---

## Intel

**Files**: `competitor.py`, `competitor_intel_format.py`, AI-visibility pieces in `admin_routes.py`/`client_api.py`/`mobile_api.py` reading `ai_visibility_runs`.

Two halves:
1. **Competitor snapshots** — nearby restaurants (Google Places), diffed week over week for rating/review-count movement.
2. **AI visibility** — asks Perplexity a fixed panel of realistic customer queries ("best brunch in X neighborhood") and records whether/how the restaurant is mentioned.

**Invariants (audit #11)**: a missing measurement is never `0`. Visibility is reported as a **range over the query sample**, never one falsely precise number. "Presence" (does an LLM mention you) is never blended with "is Cavnar AI set up for this client" — those are separate facts. No claim about AI search indexing is made without a source.

---

## Ask Cavnar

**Files**: `ask_cavnar.py` (context snapshot builder + `ask_with_tools`), `ask_cavnar_tools.py` (the tool registry — `len(TOOLS)` is the count), `business_intelligence.py` (the cross-module layer), `home_brief.py` (feeds the opening briefing).

**Design stance**: an AI-powered restaurant COO, not a chatbot wrapper. Every question gets a fresh `build_context()` snapshot (identity, sibling locations, alerts, memory, module data the restaurant's tier actually has) plus a filtered tool list (`tool_specs(restaurant)` — a tool tagged with a module the restaurant doesn't have is never offered, so the model can't call it and produce an empty-result apology).

**Role scoping**: Ask answers as the asker, not as the restaurant. Every route (web/mobile, stream/non-stream) passes `current_user`; `ask_with_tools(..., user=)` swaps in `ask_cavnar_tools.viewer_restaurant(restaurant, user)` — a copy with every module the role can't read (`permissions.MODULE_VIEW_PERMISSIONS`) switched off — so the snapshot, the cross-module money, the offered tools and every tool call leave it out. `run_read_tool` re-checks `tool_allowed` (the offered list is not the permission check); food-cost metrics in goals/outcomes are filtered via `metric_visible`. The context cache is keyed by (restaurant, denied modules). Home (`home_brief._build`) and the Ask opening built from it filter the same way.

**Roles on a team**: an owner invites or re-roles a login as Co-owner (stored `client` — every owner right), Manager, or Teammate (`member`) — `auth.TEAM_ROLES`, `auth.set_team_role` (writes users.role and the membership together; never your own role; never the last owner — revoke refuses that too). Owner mail that addresses "the owner" goes to every owner: `scheduler.get_owner_emails` (weekly digest); the morning brief already goes to each recipient.

**Owner-granted access**: an owner can open Food cost & margins (`FOOD_COST_VIEW`) and, separately, Comps & voids (`LOSS_VIEW`) to an individual manager at one location — Account → Team, web and iOS. Stored in `permission_grants`, loaded per request by `auth.get_session_user` into `current_user["grants"]`, honoured by `permissions.has_permission` for `GRANTABLE` permissions on `GRANTABLE_ROLES` only. Because every gate goes through `has_permission`, a grant opens the routes, the dashboard tab, Home, Ask and the brief at once; revoking takes effect on the next request.

**Tool kinds** (`TOOLS` registry, each entry `{kind, fn, module, spec}`):
- `read` — executes immediately, returns data (e.g. `read_schedule`, `read_team`, `read_alerts`, `remember`, `forget`).
- `action` — executes immediately, no confirmation (reserved for low-stakes/reversible calls).
- `write` — returns a **proposal** (`build_proposal`) the client renders as a confirm card; the actual route it posts to on confirm is the same authenticated endpoint a manual button already uses, so a proposal can never reach anything the owner couldn't do themselves.

**The opening** (`/mobile/api/ask-cavnar/opening`, delegated on web): built from `home_brief`'s attention items + one win, **no model call** — instant, and can't hallucinate since nothing here is generated. The only cache behind it is `home_brief`'s 60-second per-restaurant memo, so a fixed issue stops being raised within a minute.

**Memory** (`ask_memory`): the model calls `remember`/`forget` deliberately; nothing lands automatically. Facts are marked "told, not measured" wherever they appear in the snapshot, so the model never presents an owner's stated goal as something it computed.

**Across modules** (`business_intelligence.py`, audit #15): the layer that answers "why did profits drop" rather than six single-module answers. `gather()` collects each module's own executive brief; `correlations()` reports where two of them point at the same day, dish or shift; `money_at_stake()` ranks each module's monthly dollar figure against the others. It invents no thresholds — a link only exists when both sides already cleared their own module's evidence floor — and it never states a co-occurrence as a cause: each link carries `confirm_by` and `alternative`, and the labour link carries `not_a_cause` because this product has no service-time or cover-count data. The money lines are deliberately never summed (a measured cost, a scheduling gap and an elasticity forecast are not addends), and a range stays a range. Reached by the model through `read_business_snapshot`, and summarised into every snapshot by `snapshot_block()`.

**Grounding**: `ai_guard.verify_figures` runs on every answer against the snapshot + every tool payload + the replayed history. An untraceable figure caps confidence at `low` and reaches both clients, which keep the answer and caveat the number.

**Depth** (`_depth_for`): `brief` (Home box, 3 sentences), `standard`, `executive` (what/why/evidence/dollars/action/confidence/what-to-watch). Chosen from the question, deterministically.

**Prompt caching**: `system` is a list of blocks — static rules with a `cache_control` breakpoint, then the live snapshot. ~9,700 tokens cached per turn. Never interpolate per-restaurant data into the static block.

**Don't touch casually**: the module-gating in `tool_specs()`, and the read/write split — a tool that reaches an outside effect (email, public post, scheduled deletion) must be `write`, never `read`/`action`. `auto_approve` and `data_retention` are write tools for exactly this reason. And never call `client_api._do_ai_visibility` from the context path: on a cache miss it fires live Perplexity queries.

---

## The owner's day (workflow audit #19)

**Files**: `closeout.py`, `action_queue.py`, `intraday.py`, `monthly_review.py`, `weekly_review.py`, plus the day-shaped parts of `notify.py`, `morning_brief.py`, `issues.py` and `strategy_jobs.py`.

`weekly_review.py` and `monthly_review.py` are the same four questions at two cadences — what moved and what it is worth, what the owner's own changes did, where the goals stand, what is worth their time next — and they deliberately share a voice. The weekly blocks ride above the review digest (`reporter.render_html`, owner view only, since they carry money). `intraday.closing_summary` is the third: how tonight went, sent at close.

**Design stance**: the product is useful before service and after it, and must not interrupt during it.
- **Before opening** — the brief is the one morning surface. It carries yesterday, prime cost, the one thing to do, open issues, results, goals, comps (owner only), last night's close-out, reviews waiting, what's running low, next week's schedule from Thursday, the quiet weekday (Mondays), and today with weather and any holiday. Home and the Ask opening are built from the same lines; the email's lines each link into Ask (`?ask=`).
- **During service** — non-critical alerts raised in a rush (11:30–13:30, 17:30–20:30 local, clipped to opening hours) are held and released when it ends, three per restaurant per pass, dropped if 12h stale (`notify.rush_release_at`, `alert_holds`). Health alerts never wait. `intraday.py` captures net sales hourly where the POS can be read during service (Toast; RPOWER is month-at-a-time and says so), which also builds the hour-level profile that never existed — one push before dinner only when the day is materially off, and only after three same-weekday readings at that hour. A scheduled person not clocked in 15 minutes in becomes the manager's issue.
- **After close** — `closeout.py` is the manager's four-line handoff; it leads the next brief.
- **Still open** — `action_queue.py` gathers issues, replies owed, unanswered Ask proposals, unapplied invoices, dishes priced below cost and next week's schedule, each with what finishes it; "not today" snoozes to tomorrow (`action_snoozes`), nothing dismisses silently.
- **Monthly** — `monthly_review.py` reads the month against the one before, with results, goals and next month's three priorities.

**Timing**: everything an owner reads runs at that hour in the RESTAURANT's timezone (`scheduler.local_due`, `notify._gated_out`), attempted hourly and claimed per restaurant per local day.

## Strategic foundations (audit #18)

**Files**: `metrics.py`, `outcomes.py`, `goals.py`, `menu_intelligence.py`, `demand.py`, `loss_detection.py`, `issues.py`, `invoices.py`, `morning_brief.py`, `preshift.py`, `strategy_routes.py` (HTTP, web + mobile twins + the public `/i/<token>` issue page), `strategy_jobs.py` (scheduled half).

**Design stance**: every number is measured, never generated. `metrics.py` is the one registry of "what can be measured before and after" (unknown is `None`, never 0; each metric has a noise band below which the verdict is `no_clear_change`). `outcomes.py` tracks a committed change against that metric and always carries `CAUSATION_CAVEAT` — before/after, not proven cause. `goals.py` holds one active target per metric.
- **Menu**: `menu_intelligence.dish_scorecard` joins margin (inventory_ledger) to review dish mentions; `reprice_suggestions` refuses sample inventory and only proposes a price that restores the previous food-cost %.
- **Demand**: `demand.py` forecasts from medians of the same weekday; slow days and a prep list follow from it.
- **Loss**: `loss_detection.py` — comps/voids/refunds from POSes that report them (RPOWER); a spike needs 2× baseline AND $75 AND 4 events; approver concentration attributes to the APPROVING manager. Worded "worth reviewing", never an accusation, and **owner-only** everywhere (routes, digest, brief — never a manager).
- **Issues**: `issues.py` — an issue texts a consented alert contact a tokenised link (one link per person, hash-only storage); GET on the link has no side effect (link previews), the assignee taps "I'm on it" / "Mark resolved"; unacknowledged issues escalate once; quiet hours hold the text. Bad reviews open issues automatically only where the owner routed a manager.
- **Invoices**: see `PROMPT_LIBRARY.md` — the model transcribes, Python proposes, the owner confirms.
- **Morning brief**: deterministic lines, pushed (or emailed) at the restaurant's own hour, never after 2pm local; every line carries an Ask prompt.
- **Pre-shift**: `preshift.py` for the staff portal — relative volume, complaint watch, running-low items, holiday, weather. No money, no individuals.

**Permissions**: `/food-cost/*` paths inherit Food Cost gating (a manager never sees invoice prices or margins); goals/outcomes on food-cost metrics are filtered the same way; loss signals, issue routing and the morning brief are principal-only (`TEAM_INVITE` holders).

## Customer value / ROI (audit #20)

**Files**: `value_delivered.py` (the four figures), `outcomes.py`
(`total_value`, `best_ever`, `realised`, `module_of`), `promise.py` (the
sales audit, measured), `metrics.py` (`comp_rate`/`void_rate`),
`models.money_surfaced`.

**Design stance**: the product must be able to say what it has been worth
without saying anything it cannot defend line by line.

- **Four figures, never summed.** `delivered` is measured before/after from
  outcomes and carries `CAUSATION_CAVEAT`. `avoided` is cost avoidance with
  every rate stated in the payload. `surfaced` is what the alerts carried in
  dollars. `opportunity` is the gap against target — money available, not
  banked. Adding any two of them mixes a measurement with an estimate.
- **The denominator travels with the total.** Every surface that prints
  "$X across N changes" also prints how many finished trackers came back
  unmeasurable or flat.
- **A recommendation is trackable when it names a metric.** `home_brief`'s
  `add_rec(metric=)` is what puts a "Track this" control on the card; four
  of the nine carry `None` because no honest metric fits, and they render
  without one.
- **Count work that happened, not time that passed.** The marketing figure
  was `months_since_signup × $1,500` off one post; schedules were counted
  per generated DRAFT (5,672 of them for one restaurant). Distinct months
  with content, distinct weeks scheduled.
- **`promise.compare`** puts the sales audit's own range beside where the
  metric stood then and stands now, and stops. It does not score itself, and
  a category with no metric in this product (bar, waitlist, marketing
  revenue) says so rather than being dropped or zeroed.

**Don't touch casually**: the separation of the four figures, and
`metrics.DAYS_PER_MONTH`/`WEEKS_PER_MONTH` — one calendar, derived from
52/12, matching `inventory.WEEKS_PER_MONTH`.

---

## Admin (Will-only)

**Files**: `admin_routes.py`, `admin_ops.py` (data layer), `admin_events.py`.

Client health rollup (owner → brand → location), job-run history (`job_runs`/`job_failures`), manual contract send (DocuSign), the sales-audit in-person tool (`sales_audit_*.py`), changelog authoring, status-page incident management. Hash-routed single-page app (`admin.html`), CSRF include required on every mutating call same as the client dashboard.

---

## Account

Profile, Security (2FA + backup codes + trusted devices + sign-in history), Team (owner-only invite/revoke/re-role, owner-granted Food-cost and Comps access), Automation & trust (every switch with its record), Connections (Google, Toast, Instagram and RPOWER real; Square/Clover honest "contact us" stubs), Billing, Notifications/alert channels, Data export, Help/FAQ. All five iOS sheets share one "identity card" layout (`AccountSheetKit.swift`) — a new sheet reuses it rather than inventing a new chrome.

---

## Auth / Security

See `SYSTEM_ARCHITECTURE.md`'s Auth section for the model. Module-specific note: `_billing_blocked()` and `_module_blocked()` in `auth.py` gate access at the decorator level (a lapsed account or a module the tier doesn't include gets a clear message, not a 500 or a silently empty page).

---

## POS / Platform integrations

`toast.py`/`toast_routes.py` (OAuth + shift/sales sync), `square.py`/`square_routes.py`, `clover.py`/`clover_routes.py`, and `rpower.py`/`rpower_routes.py` (a static bearer token pasted in Admin → Verify & save, which resolves the store; month-at-a-time, read-only, archived locally before it is parsed — vendor-confirmed Sep 2026; item-level sales feed `menu_item_sales`) — each behind `pos.py`'s `PROVIDER_API` contract (`is_connected`, `test_credentials`, `sync_to_db`, `build_shifts_csv`, and the optional `fetch_order_customers` that guest matching asks for via `pos.supports`). `gmb.py` (Google Business Profile), `meta_api.py` (Instagram), `weather.py` (NWS forecast, cached on the restaurant row for schedule demand-matching).

## Staff portal

**Files**: `staff_routes.py` (the `/staff/*` blueprint), `staff_schedule.py` (the published week as an employee sees it), `staff_roster.py` (names and job roles from the POS or entered by hand), `time_off.py` (requests the owner decides in place), `labor_replacements.py` (who could cover a shift), `preshift.py` (the pre-shift read), and the `memberships`/`staff_portal_tokens` tier in `auth.py` (PIN identity, pepper in `docs/ops/PIN_PEPPER_RUNBOOK.md`).

An employee signs in with a name and PIN at the restaurant's portal link, sees today's shifts, the flat task checklist for their job role, the pre-shift read (relative volume, complaint watch, running-low items, holiday, weather — no money, no individuals), can submit availability and request time off. `docs/plans/TASK_SHEETS_PLAN.md` is the design for replacing the flat checklist with per-shift sheets the owner writes.

## Security infrastructure

**Files**: `security.py` (durable login throttling by account and IP, breached-password check, the freeze), `security_headers.py` (HSTS/CSP/etc. on every response), `credentials.py` (Fernet at rest for POS/OAuth columns), `csrf.py`, `guest_links.py` (signed public tokens), `http_layer.py` (gzip, cache headers, the rolling latency window), `permissions.py` (roles and the per-module view gates), `provisioning.py` (account creation from a signed contract). The controls and the env vars they need: `docs/ops/SECURITY.md`.

## Configuration and the demo accounts

`config.py` holds the environment values more than one module reads — `base_url()`, `from_email()`, `will_email()`, `on_railway()`, `google_places_key()` — each read at call time with one default; a value read in a single module stays in that module. `ai_utils.MODELS` / `model_for()` / `get_client()` are the same idea for the model calls. `demo_seed.py` is the Gia Mia and Simple EJ's seeding and refresh (gated on the account name and `is_demo`), started once at boot by `demo_seed.start_background_seed()`; `models` keeps `_seed_simple_ejs` and `_seed_gia_mia` as wrappers for the tests and boot block that reach them there.

## Automation and moments

**Files**: `delayed.py` (actions queued with an undo window — auto-publish, trusted supplier orders), `decisions.py` (the owner's decision record: what was proposed, what they answered, what was measured after), `milestones.py` (firsts an owner is told about once), `good_news.py` and `first_look.py` (the wins and the first-week read the brief and emails draw on), `covers.py` (covers per day), `promise.py` (the sales audit's promise, measured), `review_common.py` (the sentences the weekly and monthly reviews share). `status_routes.py`/`status_manager.py` are the public status page; `social_routes.py` the Instagram/Facebook OAuth and publishing; `sales_audits.py` the sales-audit store behind `sales_audit_routes.py`. `audit_app.py` is a separate standalone app (the digital audit scorecard), not part of the web process.

## Intelligence engine (`intelligence/`)

The platform's learning layer — see `INTELLIGENCE_ENGINE.md` for the full
design. Level 1 (`features.py`, `memory.py`, `feedback.py`) is each
restaurant's own history and serves only that restaurant. Levels 2 and 3
(`patterns.py`, `benchmarks.py`, `trends.py`, `scoring.py`) read one
materialized table of ratios and answer only over cohorts of at least
`privacy.MIN_COHORT` restaurants. `confidence.py` scores every Home
recommendation; `dashboard.py` builds the admin Intelligence page;
`categories.py` is the cohort taxonomy, `stats.py` the permutation test
and FDR correction, `jobs.py` the two nightly passes (3am features,
4am learning). Stance:
nothing generated fills a gap, and `privacy.assert_anonymous` runs on every
cross-restaurant payload.

## Marketing tags and post attribution (`marketing_tags.py`, `marketing_signals.py`)

Every content-log row carries what the post was about — `menu_item_id`
(only a dish the restaurant has), `occasion` (game day, holiday, event,
offer, weather, weekend) and `post_kind` — inferred from the topic and
caption, correctable by the owner. Attribution reads a post's window
beyond total sales: the promoted dish's own units against the same
weekdays before (`menu_item_sales`), reviews in the next fortnight that
named it, the guest list's move, and engagement. The summary shows what did
not land next to what did, and medians by kind, occasion and dish. A
publish starts the month's observed sales tracker (`post_published`).
