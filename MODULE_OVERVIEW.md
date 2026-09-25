# Module Overview — Cavnar AI

One section per product module: what it does, the key files, the invariants an audit checks, and what NOT to touch casually.

---

## Reviews

**Files**: `fetcher.py` (Google/Yelp pull), `analyser.py` (Claude sentiment/category/summary/severity/entities), `drafter.py` (AI reply drafting), `review_intelligence.py` (the consultant layer), `client_api.py`/`mobile_api.py` review routes.

**Web inbox (friction #1/#7/#20, 9/25/26)**: the inbox leads the Reviews tab and the analytics sit under it, collapsed, loading on first open. A drafted reply has Edit beside Approve, and its text opens the editor — editing is never a Skip, so auto-approve trust never counts it as a "no". A filter or search over a partial page asks `/api/reviews/page` and replaces the list; new reviews go in at the top without a reload. `rvFocusReview(id)` (the `review/<id>` nav path, the bell, alert links, the diagnosis chips) opens one review, fetching it by id when it is not loaded.

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
6. Publishing (`client_api._publish_schedule`) refuses with the blockers until a human acknowledges; auto-publish holds. Only a published week reaches the staff portal. Each person is told on their own channel (`people.reach`, Friction #17): the app when their staff login has a device, a text when they opted in themselves (`memberships.schedule_texts_at`, on the staff messaging service only), email as the fallback.

**People** (`people.py`, Friction #25): one person record composed from the stores that own each fact — roster settings (`staff_settings`), the staff login (`memberships`), contacts (`staff_contacts`), ratings (capabilities), portal availability and the per-role rate. It stores nothing; `update_person` writes each field back to its owner. `/api/people[/<key>]` (web + mobile) backs the web person sheet (`openPersonSheet`, opened from the roster, the team list, Account → People and the send drawer) and iOS. **The open draft** (Friction #4): `labor/publish-check` resolves the newest unsent week; Labor reopens it in the editor on open, history rows and Home's "Send now" reach it, and Send is one "Save & send to N staff" button that saves edits first and names rule warnings in its label.

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
- Next week's waste is the mean of the last four ISO weeks with any outlier week left out (`forecast_log.waste_next_week`) — the old latest-week-plus-last-change momentum forecast lost to "same as last week" on pure noise (confidence audit probe W, 9/24/26). It is STORED once per ISO week (`forecast_log.record`, insert-once) and scored only after that week closes, with one error denominator (the actual). The owner is shown the bias-corrected figure when the record has a consistent lean, and nothing at all while the record reads "often wide" (`forecast_log.accuracy(...)["withheld"]`); the raw figure is always what is recorded and scored. A forecast nobody checks costs nothing to get wrong.
- `forecast_log.py` is the one place for every forecast's rules (waste, prime cost, the weekly revenue projection frozen at schedule publish, and the labor / marketing-reach / review-rating forecast lines): insert-once per period, score closed periods only (`scheduler.run_forecast_scoring`, 5am, every restaurant holding a due forecast), accuracy by kind (contract K8).
- The waste-rate denominator is what was actually received over the same seven days (`cogs.purchases_in_window`), never the sum of each item's last order — that covers a different period per item and usually a longer one than the numerator.
- Two different windows, kept apart: `week_start`/`week_end` label the WASTE period (always the trailing seven days); `counted_to`/`window_age_days`/`stock_basis` say how far the CURRENT STOCK figures can be trusted.
- Every displayed annual figure is the displayed monthly times twelve. Round once, derive the rest.
- Drivers are ranked in Python — dollars, then confidence, then ease — and the prompt is told not to re-rank them. Each carries evidence, a confidence, a difficulty and what happens if it is ignored.
- A cause is only ever stated from a stored diagnosis whose validator rejects any driver it was not given (`_validate_diagnosis`), the same discipline `verify_figures` applies to numbers.
- A projection returns nothing rather than substituting a zero, and says which component is missing. `profitability_projection` is PRIME COST, not net profit, and its basis says the labor share came from the shift analysis's own period.
- The inferred-waste gap is attributed per ingredient and per dish (`inventory_ledger.inferred_variance`) — it is over-portioning, prep loss or shrink, and aggregating it to one restaurant-wide dollar figure produced the one number an owner could not act on.

**The work, not just the read (friction audit, 9/25/26, workstream O2)**:
- A delivery goes into stock. "Received as ordered" (or "Some were short" with a quantity per line) posts each PO line with an ingredient through `inventory_ledger.record_receiving` (`client_api._do_receive_po`); the status flip claims the PO first, so a delivery can never post twice. Without this the count sheet's "expected" drifted low and the next count read the gap as unexplained waste.
- One Order card: the week's lines grouped by supplier with editable quantities, loaded when first on screen; the edits ride with the send (`inventory.apply_order_edits`) after the draft hash proves the draft unchanged, and the PO keeps the draft beside what went. Sending asks once, inline, naming the supplier, the item count and the total.
- Pending invoices live on the server (`invoices.pending_imports` — never applied, or a trusted supplier's leftovers) and list at the top of the invoice card; Home's queue item opens the invoice itself (`invoice/<id>`). An unmatched line can become an ingredient in place. Ask can apply only the lines the stored invoice marks checked (`apply_invoice_lines`) and receive the oldest open order as ordered (`receive_purchase_order`).
- Logged waste (`inventory_ledger.record_waste`, source `logged`) is counted waste; only a recount gap is `inferred`.
- Counts without the margins: `permissions.FOOD_COST_ENTER` (every console role, incl. `manager`) opens the count sheet, the PO list (money withheld), receiving and the waste log — and nothing else. A manager's web Food Cost tab is "Counts" (`fc_enter_only`), the count sheet and deliveries only.
- For a ledger account the price monitor is filled from the ingredient list, read-only until "Edit prices"; the typed tracker (`/api/food-cost-quickcount`, `food_cost_json`) is a candidate for future cleanup after additional verification — its `food_cost_data` consumers were not traced end to end.
- Recipes with every line at ≥80% support and clean units accept in one tap; the rest stay one by one.
- Each cost driver and cross-module link carries `act: {label, nav}` (`food_cost_intelligence.driver_action`, `business_intelligence.link_action`) — where the fix is done.

---

## Marketing

**Files**: `marketing.py`, `marketing_drafts.py`, `marketing_publish.py`, `marketing_media.py`, `marketing_links.py`, `marketing_signals.py`, `guest_marketing.py`, `guest_email.py`.

AI-drafted social posts (brand-voice fields on `restaurants`: `voice_notes`, `vibe`, `known_for`, `sign_off_name`, `never_say`), a content calendar, direct publish to connected accounts (Instagram real; Square/Clover honest "contact us" stubs), and the guest text-club feature (`guest_contacts`/`guest_campaigns`, SMS opt-in via `sms_optin_invites`).

**Known limitation (NS5 L16)**: guest texts go out only 8am–9pm in the RESTAURANT's time zone (`guest_marketing.GUEST_SMS_EARLIEST_HOUR`/`LATEST_HOUR`), the closest proxy for the guest's own. A guest whose phone is in another zone, and any state that sets a narrower window for marketing texts, are not modelled — the window is one fixed floor. Widening it per state needs the guest's area code or zone and a checked rule table; nothing here should claim the window satisfies every state's rule.

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

**Screen context** (friction #15, 9/25/26): every question may carry `screen` {panel, entity}; `ask_cavnar.screen_hint` turns it into one server-built line (a review by id from this restaurant, described by rating, platform, date and reply status only), never an instruction — see PROMPT_LIBRARY.md → Ask. The web sends it with every question, and a review card's or an Intel recommendation's "Ask about this" sets the item; the chat id is kept per restaurant in sessionStorage so the panel reopens on it after a reload or a location switch.

**Role scoping**: Ask answers as the asker, not as the restaurant. Every route (web/mobile, stream/non-stream) passes `current_user`; `ask_with_tools(..., user=)` swaps in `ask_cavnar_tools.viewer_restaurant(restaurant, user)` — a copy with every module the role can't read (`permissions.MODULE_VIEW_PERMISSIONS`) switched off — so the snapshot, the cross-module money, the offered tools and every tool call leave it out. `run_read_tool` re-checks `tool_allowed` (the offered list is not the permission check); food-cost metrics in goals/outcomes are filtered via `metric_visible`. The context cache is keyed by (restaurant, denied modules). Home (`home_brief._build`) and the Ask opening built from it filter the same way.

**Roles on a team**: an owner invites or re-roles a login as Co-owner (stored `client` — every owner right), Manager, or Teammate (`member`) — `auth.TEAM_ROLES`, `auth.set_team_role` (writes users.role and the membership together; never your own role; never the last owner — revoke refuses that too). Owner mail that addresses "the owner" goes to every owner: `scheduler.get_owner_emails` (weekly digest); the morning brief already goes to each recipient.

**Owner-granted access**: an owner can open Food cost & margins (`FOOD_COST_VIEW`) and, separately, Comps & voids (`LOSS_VIEW`) to an individual manager at one location — Account → Team, web and iOS. Stored in `permission_grants`, loaded per request by `auth.get_session_user` into `current_user["grants"]`, honoured by `permissions.has_permission` for `GRANTABLE` permissions on `GRANTABLE_ROLES` only. Because every gate goes through `has_permission`, a grant opens the routes, the dashboard tab, Home, Ask and the brief at once; revoking takes effect on the next request.

**Tool kinds** (`TOOLS` registry, each entry `{kind, fn, module, spec}`):
- `read` — executes immediately, returns data (e.g. `read_schedule`, `read_team`, `read_alerts`, `remember`, `forget`).
- `action` — executes immediately, no confirmation (reserved for low-stakes/reversible calls).
- `write` — returns a **proposal** (`build_proposal`) the client renders as a confirm card; the actual route it posts to on confirm is the same authenticated endpoint a manual button already uses, so a proposal can never reach anything the owner couldn't do themselves. `decide_time_off` / `decide_shift_request` (Friction #18, with the read `read_time_off`) put the request id in the path and name the person from the stored PENDING request, never the model's words; a request that is answered or not this restaurant's builds no card.

**The opening** (`/mobile/api/ask-cavnar/opening`, delegated on web): built from `home_brief`'s attention items + one win, **no model call** — instant, and can't hallucinate since nothing here is generated. The only cache behind it is `home_brief`'s 60-second per-restaurant memo, so a fixed issue stops being raised within a minute.

**Memory** (`ask_memory`): the model calls `remember`/`forget` deliberately; nothing lands automatically. Facts are marked "told, not measured" wherever they appear in the snapshot, so the model never presents an owner's stated goal as something it computed.

**Across modules** (`business_intelligence.py`, audit #15): the layer that answers "why did profits drop" rather than six single-module answers. `gather()` collects each module's own executive brief; `correlations()` reports where two of them point at the same day, dish or shift; `money_at_stake()` ranks each module's monthly dollar figure against the others. It invents no thresholds — a link only exists when both sides already cleared their own module's evidence floor — and it never states a co-occurrence as a cause: each link carries `confirm_by` and `alternative`, and the labour link carries `not_a_cause` because this product has no service-time or cover-count data. The money lines are deliberately never summed (a measured cost, a scheduling gap and an elasticity forecast are not addends), and a range stays a range. Reached by the model through `read_business_snapshot`, and summarised into every snapshot by `snapshot_block()`.

**Grounding**: `ai_guard.verify_figures` runs on every answer against the snapshot + every tool payload + the replayed history. An untraceable figure caps confidence at `low` and reaches both clients, which keep the answer and caveat the number.

**Depth** (`_depth_for`): `brief` (Home box, 3 sentences), `standard`, `executive` (what/why/evidence/dollars/action/confidence/what-to-watch). Chosen from the question, deterministically.

**Prompt caching**: `system` is a list of blocks — static rules with a `cache_control` breakpoint, then the live snapshot. ~9,700 tokens cached per turn. Never interpolate per-restaurant data into the static block.

**The nightly DSR** (`dsr/memory.py`): Ask's snapshot carries last night's report (headline figures by fact key, what was missing, the narrative's lead and actions) and four viewer-aware read tools — `read_dsr`, `find_days` (SQL over `dsr_metrics`), `read_week`, `read_period`. Every read goes through `dsr.access` for the asker (`viewer_restaurant` stamps `_ask_dsr_user`), so a manager never reads the budget, prime cost, comps/voids or food cost here either; `find_days` refuses those metrics outright, and the context cache key carries the DSR view.

**Don't touch casually**: the module-gating in `tool_specs()`, and the read/write split — a tool that reaches an outside effect (email, public post, scheduled deletion) must be `write`, never `read`/`action`. `auto_approve` and `data_retention` are write tools for exactly this reason. And never call `client_api._do_ai_visibility` from the context path: on a cache miss it fires live Perplexity queries.

---

## The owner's day (workflow audit #19)

**Files**: `closeout.py`, `action_queue.py`, `command_center.py`, `nav.py`, `intraday.py`, `monthly_review.py`, `weekly_review.py`, plus the day-shaped parts of `notify.py`, `morning_brief.py`, `issues.py` and `strategy_jobs.py`.

`weekly_review.py` and `monthly_review.py` are the same four questions at two cadences — what moved and what it is worth, what the owner's own changes did, where the goals stand, what is worth their time next — and they deliberately share a voice. The weekly blocks ride above the review digest (`reporter.render_html`, owner view only, since they carry money). `intraday.closing_summary` is the third: how tonight went, sent at close — except where the nightly DSR runs (`dsr_enabled` and a POS connected), whose own notice (`dsr.deliver`) replaces it.

**Design stance**: the product is useful before service and after it, and must not interrupt during it.
- **Before opening** — the brief is the one morning surface. It carries yesterday, prime cost, the one thing to do, open issues, results, goals, comps (owner only), last night's close-out, reviews waiting, what's running low, next week's schedule from Thursday, the quiet weekday (Mondays), and today with weather and any holiday. Home and the Ask opening are built from the same lines; the email's lines each link into Ask (`?ask=`).
- **During service** — non-critical alerts raised in a rush (11:30–13:30, 17:30–20:30 local, clipped to opening hours) are held and released when it ends, three per restaurant per pass, dropped if 12h stale (`notify.rush_release_at`, `alert_holds`). Health alerts never wait. `intraday.py` captures net sales hourly where the POS can be read during service (Toast; RPOWER is month-at-a-time and says so), which also builds the hour-level profile that never existed — one push before dinner only when the day is materially off, and only after three same-weekday readings at that hour. A scheduled person not clocked in 15 minutes in becomes the manager's issue.
- **After close** — `closeout.py` is the manager's handoff: four quick lines plus the six fields the nightly DSR reads (equipment, VIP guests, maintenance, shift notes, general notes, and "why the day went how it did"). It leads the next brief (equipment and maintenance included — they are the opener's problem), and `dsr/block_closeout.py` carries it verbatim into the night's report. The night's report itself is the **Daily report** (`dsr/`): Home's "Last night" card opens it at `#dsr/YYYY-MM-DD` — for the owner it opens with **Today's score** (`dsr/scorecard.py`: the verdict, the overall score out of 100, sales vs budget, labor vs target, food cost, guest experience), then the **executive summary**, then **Today's wins** and **Today's risks** (measured signals first, topped up from the narrative's verified lines) — the same order in the email, the push and the iPhone report; the manager's report is operations, not finance — its own **operations summary**, **Today's shift** (employees scheduled, call-offs, late arrivals, overtime, shift quality; break compliance joins when Cavnar has a source) and **Operations** (average ticket, guests, complaints, sales per labor hour; comps and voids only when the owner grants them). Both reports then carry the **key numbers with direction** (`dsr/kpis.py`: vs the same weekday, best/worst in weeks, a trend line, the target, restaurants like yours only when fair), **AI insights**, **Tomorrow's priorities** (up to five actions), **Tomorrow** (`dsr/tomorrow.py`: time off, weather, events, low stock, Cavnar's forecast and an AI confidence %), the blocks, and **How did yesterday turn out?** (`dsr/predictions.py`: yesterday's predictions graded, with the running accuracy); tomorrow's actions (answered on rec_ledger's `dsr` surface), every block with its source or the reason it isn't ready, the close-out verbatim — with Close day (and, for the owner, re-run any of the last seven nights), the weekly grid in the owner's own week with an .xlsx export, and the period.
- **Still open** — `action_queue.py` gathers issues, replies owed, unanswered Ask proposals, unapplied invoices, dishes priced below cost and next week's schedule, each with what finishes it; "not today" snoozes to tomorrow (`action_snoozes`), nothing dismisses silently.
- **The direct action first** (friction #46, 9/25/26) — a brief line with an action of its own carries `action: {label, nav}` (`morning_brief.line_action`: reviews waiting → "Answer them" / "Reply now" on the inbox's filter, running low → the order, next week's schedule → the schedule, open issues → Home), a nav path (`nav.py`). Home renders it as the line's button before "Ask →"; the email links it first (`/?nav=…`) with "Ask about this" after it. The weekly digest's button names the replies still owed and opens the inbox on "To approve" (`reporter.digest_cta`), else the dashboard as before.
- **Home, on the web** (friction #11/#12/#23/#24) — the one thing and Needs attention lead (DESIGN_SYSTEM §11b); the proof is one collapsed Results section. One job appears once (`hbSame`). An answer removes its card and re-reads only `/api/home/brief`; any write elsewhere marks Home stale. A multi-location login opens on all its locations, a group row switches and lands on the item (a nav path carried across the reload), and the bell covers every location (`/api/notifications?scope=group`). The bell is an inbox: each row names the review's words, the location and whether it was handled, is read when opened (not when the bell is), and a drafted reply can be read and posted from the row.
- **Monthly** — `monthly_review.py` reads the month against the one before, with results, goals and next month's three priorities.
- **Getting there** (Friction audit 9/25/26) — every "take me there" carries a `nav` path (`nav.py`): Home's attention items and quick actions, each queue item (a stored Ask proposal reopens from its row with no model call — `command_center.reopen`), each bell row. The web opens it with `cavNav` (`<script id="cav-nav">`, the one router: Back walks tabs and sections, `#labor/schedule` and `#account/notifications` survive a reload, `?nav=` / `?review=` from an email open the item); iOS with `NavPath`.
- **The Command Center** — `command_center.py`: the commands a login may run here (`registry`, Ask's write tools through the same `tool_allowed` test), one permission-filtered search, and `propose` (Ask's confirm card from `build_proposal`, logged `proposed`, settled through Ask's own action route). The web ⌘K palette and the iOS command sheet read the same routes. Deterministic: free text goes to Ask's stream, so Response Validation and the readiness gate stand in front of every answer. Anything that acts follows the confirm/undo policy (`DESIGN_SYSTEM.md` §10): reversible at once with Undo, outward through the confirm card, `confirm()` only for security.

**Timing**: everything an owner reads runs at that hour in the RESTAURANT's timezone (`scheduler.local_due`, `notify._gated_out`), attempted hourly and claimed per restaurant per local day.

## Strategic foundations (audit #18)

**Files**: `metrics.py`, `outcomes.py`, `goals.py`, `menu_intelligence.py`, `demand.py`, `loss_detection.py`, `issues.py`, `invoices.py`, `morning_brief.py`, `preshift.py`, `strategy_routes.py` (HTTP, web + mobile twins + the public `/i/<token>` issue page), `strategy_jobs.py` (scheduled half).

**Design stance**: every number is measured, never generated. `metrics.py` is the one registry of "what can be measured before and after" (unknown is `None`, never 0; each metric has a noise band below which the verdict is `no_clear_change`). `outcomes.py` tracks a committed change against that metric and always carries `CAUSATION_CAVEAT` — before/after, not proven cause. `goals.py` holds one active target per metric.

**Outcomes after the rec-ROI audit** (`outcomes.py`, `metrics.py`; contracts in API_REFERENCE.md → "Outcomes and value"):
- **Metrics**: eleven — `labor_pct`, `overtime_hours` (hours past 40 per whole payroll week from the shifts file; a week with no shifts is unknown; dollars at the overtime premium only, `metrics.overtime_premium_per_hour` = blended wage × (1.5 − 1)), `food_cost_pct`, `weekly_waste`, `sales`, `weekday_sales:<Day>`, `avg_rating`, `complaints:<cat>`, `response_hours` (median review-to-reply hours, ≥5 replies, no dollars), `comp_rate`, `void_rate`. `metrics.FAMILIES` groups the ones that measure the same money or experience (labor cost, food cost, sales, guest rating, reply speed, comps, voids). `compare` returns the `band` it used and the `multiple` of it the move covered; a relative band has a floor (`_NOISE_FLOOR`).
- **One tracker per number.** `outcomes.record(gate=)` refuses a second tracker on the same metric (`"metric"`, an owner-pressed Track and Ask) or anywhere in the family (`"family"`, every automatic start — `AUTOMATIC_SOURCES`: observed actions, reprice, campaigns, schedule accepts — and Accept/Done on `recs/event`, Home Done; re-audit A18). Metric keys are normalised (`metrics.normalize`: `weekday_sales:tuesday` is `weekday_sales:Tuesday`). The gate is checked again inside `BEGIN IMMEDIATE`, so two starts at once are one tracker, never a 500. `observe()` records at most one tracker per metric per calendar month, whatever became of the first. `outcomes.start` turns a refusal (`TrackerRefused`) into the `tracker_refused` reply. An alert-opened tracker is informational and never blocks.
- **Dates** are the restaurant's own (`outcomes.local_today`), for starts, evaluations, re-checks, accrual and campaign keys; the evaluation job runs one restaurant at a time, bounded and resumable (`strategy_jobs._bounded_each`).
- **What a recommendation measures**: `outcomes.metric_for_rec` — a DSR action by its kind (`DSR_ACTION_METRICS`: control_hours/adjust_staffing → labor %, reduce_waste → waste, respond_reviews → reply time, the rest nothing — authoritative), a kind that names its own number (`slow_day:<Day>` → that weekday's sales; `overtime` / `overtime_move:…` → `overtime_hours`; authoritative whatever surface showed it), else the metric it was presented with (`rec_instances.expected_metric`). With a `viewer`, a metric the login may not see is never started from its answer (`metric_visible_to`: food cost needs FOOD_COST_VIEW, comps/voids LOSS_VIEW). Track falls back to the module's natural metric (`strategy_routes.REC_TRACK_METRICS`); Marketing and Intel have none.
- **Credit**: `module` is stored on the row from the recommendation (`resolve_module`: the kind's own (`KIND_MODULE`: a slow day is Marketing) → `rec_instances.module` → DSR block → the caller's → source → the metric's `METRIC_MODULE`, where `sales` is `other`). The caller's module comes after the recommendation's: iOS sends the screen a Track was pressed on.
- **Readings** (`outcomes._measure`): a window counted in trading days (labor %, sales, a weekday's sales, comps, voids) is unknown unless ≥ `MIN_COVERAGE` (70%) of its trading days were measured (`metrics.coverage`: the weekdays traded over the 8 weeks ending with the window, less the owner's stated closures) — baseline, after-window, re-check and each accrual day alike. Comps and voids are read over the days the POS was asked (every asked day has a row, zero included). Waste is priced only when ≥90% of its events carry a unit cost. A move of zero is never a move.
- **Baselines** (`_baseline`): `prior window` for metrics a season does not move; `matched weekdays` for labor %, food cost %, sales (a window a whole number of weeks back); `same weeks last year` when ≥80% of the trading days of both of last year's windows are measured — this year's before-window moved as those weeks moved last year, compared with a band widened by `SEASONAL_BAND_SCALE`. Weekday-sensitive trackers (`metrics.WEEKDAY_MIX_METRICS`) run whole weeks; a legacy window of another length is re-read a whole number of weeks from its start (`_aligned_window`).
- **Evaluation** stores after-value, delta, `delta_pct`, the other changes in the window (`find_concurrent`: trackers and accepted recommendations on the same family, never one the owner disowned; for a labor/food-cost/comp/void share, a sales move past its band (`sales_move`) and any sales tracker; price changes (not the reprice tracker's own), owner events, unbalanced holidays — by NAME against last year for a `same weeks last year` baseline — and closures for trading-volume families), a `trend` entry when the baseline's two halves show the number already moving that way by enough to explain the move (`pre_trend`), and the grade (`grade`, the one rule evaluate, recheck and apply_checkin all use, with the owner's check-in): `none` / `associated` (past the band once, or anything with a concurrent change, an unchecked window, or a check-in saying conditions changed or "no") / `consistent` (≥ `CONSISTENT_MULTIPLE` bands, nothing else changing) / `held` (still past the band at re-check, nothing else changing). No label claims cause.
- **Calibration (confidence audit 9/24/26; INTELLIGENCE_ENGINE.md → "What a measured result is allowed to say")**: a triggered tracker's baseline is the mirror of its after-window about the window that fired the recommendation (`trigger_window_for`, `mirror_window`, `baseline_kind` "before the trigger"), never that window; one that could only be read against it is `baseline_overlaps_trigger` — shown, never counted. Every comparison uses the restaurant's own noise band stored on the tracker (`metrics.noise_band`, `outcomes._cmp`; the stated band is its floor; `false_alarm_rate` stated). `outcomes.result_counts` is the one admission rule value and learning share (the learning == value test): a "something else changed" check-in now stops Delivered and accrual like a "no"; a supplier order is informational. `total_value` / `delivered` / `cumulative.by_grade` report `consistent_monthly` (clear or held) apart from `associated_monthly`; `summarise` and Ask's decision lines carry the grade ("held" only when held). `observe_untaken` measures advice shown and not taken, informationally, as the comparison group (`rec_learning.untaken_comparison`). `goals` "met" needs the reading past the target by the band (else `at_target`). Schedule night recommendations are judged on ≥7 watched nights against the 12 weeks before (`schedule_intel.night_rate_verdict`); a daypart nobody watched has `issues: null` and an `issues_label`.
- **Re-check** (`recheck`, daily job): at `recheck_on` = max(start + 90, evaluate_on + window) the window ending the day before is read against the same baseline — `held` / `faded` / `reversed` / `unknown`. Faded or reversed stops counting and stops accruing. `validated` = improved + held + nothing else on the family + not disowned.
- **Pricing** (`metrics.monthly_dollars(window=)`): a share of sales (labor %, food cost %, comps, voids) and sales itself are priced on the after-window's sales per trading day × the restaurant's trading days a month (`days_per_month`: weekdays traded × 52/12 — 26 for a restaurant closed Mondays), and accrue per trading day on days actually traded and measured (`accrual_days`).
- **Value**: `total_value` keeps one reading per family per overlapping window (`distinct`): a narrower metric overlapping a broader one is the broader one's (overtime inside labor %, waste inside food cost %; `metrics.BREADTH`) — wins and losses netted — and of the rest the non-overlapping set of largest dollars (weighted interval scheduling). A labor/food-cost/comp/void move over the same weeks as a sales move the same way is set aside (`_drop_sales_artifacts`). `monthly` is the SAVINGS that improved, `worsened` `{count, monthly, priced_count}` beside it, `net_monthly` the difference (may be negative, `net_note` says so), `validated_monthly` the part that held; `annual` is a labelled projection. A sales rise is gross revenue and is reported apart in `sales_lift`, never added to the savings (`sales_pricing: "separate"`). Rating and reply-time wins count in `wins_measured` / `wins_by_module` with a `result_line`, never priced. `cumulative` sums `outcome_value_days` in SQL: the after-window's measured days at the evaluated figure's per-day share, then one trailing re-read a day while the move holds (capped at the evaluated figure, up to `ACCRUAL_HORIZON_DAYS`), net of losses — one reading per family and day (the broadest that held, then the largest), disowned trackers out, a cost day on a day sales moved the same way out, sales days summed apart in `cumulative.sales_lift`.
- **Viewers** (`outcomes.visible_to`, `value_delivered.viewer_scope`): a tracker is a login's to see when its metric is and the recommendation behind it is (`rec_learning.viewer_sees` on the linked episode). `/outcomes`, `/value`, the Home value block, the owner report and the win push all drop the rest before anything is summed; abandon leaves an unseen tracker alone with the same `{ok: true}`.
- **Menu**: `menu_intelligence.dish_scorecard` joins margin (inventory_ledger) to review dish mentions; `reprice_suggestions` refuses sample inventory and only proposes a price that restores the previous food-cost %.
- **Demand**: `demand.py` forecasts from medians of the same weekday; slow days and a prep list follow from it.
- **Loss**: `loss_detection.py` — comps/voids/refunds from POSes that report them (RPOWER); a spike needs 2× baseline AND $75 AND 4 events; approver concentration attributes to the APPROVING manager. Worded "worth reviewing", never an accusation, and **owner-only** everywhere (routes, digest, brief — never a manager).
- **Issues**: `issues.py` — an issue texts a consented alert contact a tokenised link (one link per person, hash-only storage); GET on the link has no side effect (link previews), the assignee taps "I'm on it" / "Mark resolved"; unacknowledged issues escalate once; quiet hours hold the text. Bad reviews open issues automatically only where the owner routed a manager.
- **Invoices**: see `PROMPT_LIBRARY.md` — the model transcribes, Python proposes, the owner confirms.
- **Morning brief**: deterministic lines, pushed (or emailed) at the restaurant's own hour, never after 2pm local; every line carries an Ask prompt. Its "yesterday" line reads last night's finished DSR when there is one (`dsr.memory.last_night`: the report's net, its own forecast comparison, labor % where the viewer may read labor, "Provisional" when it was) and falls back to the Labor module's daily history otherwise — the brief never recomputes what the report states.
- **Pre-shift**: `preshift.py` for the staff portal — relative volume, complaint watch, running-low items, holiday, weather. No money, no individuals.

**Permissions**: `/food-cost/*` paths inherit Food Cost gating (a manager never sees invoice prices or margins); goals/outcomes on food-cost metrics are filtered the same way; loss signals, issue routing and the morning brief are principal-only (`TEAM_INVITE` holders).

## Customer value / ROI (audit #20)

**Files**: `value_delivered.py` (the four figures, and `rates()` — every
stated rate in one object the payload carries), `outcomes.py`
(`total_value`, `cumulative`, `best_ever`, `realised`, `module_of_row`), `promise.py` (the
sales audit, measured), `metrics.py` (`comp_rate`/`void_rate`),
`models.money_surfaced`, `owner_report.py` ("What worked for you" —
`what_worked(rid, days, viewer)` behind `GET /recs/what-worked`, Home's card
under the worth section, the Recommendations page and the owner's monthly
email).

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
- **Net, measured, and sales apart (re-audit A6, A29, A36).** Every surface
  that prints delivered carries what got worse beside it: `headline()` and
  `home_block()` give both Homes `net_monthly`, `worsened` and `cumulative`
  (contract K4); the value snapshots and `compute_total_value_delivered` are
  the net; the monthly and lifecycle emails say it through
  `value_delivered.value_lines` — net, the ×12 figure called a projection,
  the sum over measured days, and a sales lift as gross revenue kept apart;
  milestones cross their tiers on dollars actually measured
  (`outcomes.cumulative`), never a run-rate × 12. `headline()` is light: no
  best-ever, no rates.
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

- **"What worked for you" is sentences over stored rows, never a model.**
  `owner_report` joins the ledger summary (acceptance with ignored in the
  denominator), the stored before/after results of recommendations the
  owner took (one per metric per non-overlapping window, no-clear-change
  included so the average is not only the movers), the measured days over
  the window (net) and the most effective tag. Each clause has its own
  minimum and is left out below it; the words are "associated with" and
  "measured", never "caused" or "saved you". The web shows it on Home and on
  the Recommendations page (`#recs` — what you followed by area, the
  check-in, and every recommendation with its answer, reason and result).

**Don't touch casually**: the separation of the four figures, and
`metrics.DAYS_PER_MONTH`/`WEEKS_PER_MONTH` — one calendar, derived from
52/12, matching `inventory.WEEKS_PER_MONTH`.

---

## Admin (Will-only)

**Files**: `admin_routes.py`, `admin_ops.py` (data layer), `admin_events.py`.

Client health rollup (owner → brand → location), job-run history (`job_runs`/`job_failures`), manual contract send (DocuSign), the sales-audit in-person tool (`sales_audit_*.py`), changelog authoring, status-page incident management. Hash-routed single-page app (`admin.html`), CSRF include required on every mutating call same as the client dashboard.

---

## Account

**Hours and closures (friction audit U2-3/U2-19)**: the closures box is the scheduler's closed dates (`schedule_rules.closures`, the same list Labor → Scheduling rules edits), as date chips — it used to write `skip_holidays`, the marketing holiday list, which the scheduler never read. Hours can be filled from Google Business (`gmb.regular_hours`) or copied to every day; neither saves until Save hours. Alert contact 1 starts as the owner's own name and phone when none is saved. The DSR budget editor prefills from last week, last year ± %, or Cavnar's forecast (`dsr.store.budget_prefill`); the covers tile offers the POS guest count for a night nobody entered (`covers.pos_offers`), never writing it without a tap.

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

The iOS staff portal (`Features/Staff/`) has the same requests as the web one (Friction audit #49): a **Requests** tab (time off with withdraw, their own drops and swaps, swaps a colleague asked of them, open shifts to pick up) and a menu on each shift — "Can't work this" / "Swap with a colleague" (`StaffRequestsViews.swift`). It posts the web portal's `/staff/api/*` routes one for one; there are no mobile twins for the staff tier.

## iOS: ways in from outside the app and the command sheet (Friction audit #31, #47)

**Files**: `ios/CavnarAI/CavnarAI/Core/SystemEntry.swift` (quick actions, links, the section hand-off), `Core/SystemSync.swift` (widget snapshot, quick-action count, Live Activity sync, Undo), `Core/CavnarAppIntents.swift` (App Shortcuts / Siri), `Features/Command/` (the command sheet), `Features/People/PersonSheet.swift` (one person record), `ios/CavnarAI/Shared/` (compiled into the app and the extension), `ios/CavnarAI/CavnarWidgets/` (the WidgetKit extension: the "waiting · last night" widget and the auto-publish countdown Live Activity).

**Design stance**: every way in only opens a place. A quick action, a shortcut, a widget or Live Activity tap, a `cavnarai://nav/…` or `dashboard.cavnar.ai` link becomes a nav path posted as `.cavnarOpenNav` — the router owns the navigation, the Face ID lock still stands in front (a destination waits in `SystemEntry` until the scene is active), and nothing outward is sent without the in-app confirm card. The one exception is Undo on a pending send (`delayed.py`), the safe direction, which needs an unlocked device. The widget extension never signs in: the app writes `WidgetSnapshot` into the `group.ai.cavnar.CavnarAI` App Group and clears it on sign-out. The command sheet reads the Command Center's mobile routes (`/mobile/api/command/registry|search|propose`), confirms through Ask's own `ProposalCard` and route, answers time-off and shift requests in place with the Labor decide routes, and falls back to navigation + Ask on a server without those routes. Module screens open the section a path names through `NavSectionInbox` / `.onNavSection(module)` — Labor (`labor/requests`, `request/…`, `labor/schedule`, `person/…`) and Food Cost (`inventory/invoices?scan=camera`, `inventory/order`, `inventory/count`). Universal links need the associated-domains entitlement and an `apple-app-site-association` file on dashboard.cavnar.ai; the in-app handler is built, the entitlement and file are not yet.

## Security infrastructure

**Files**: `security.py` (durable login throttling by account and IP, breached-password check, the freeze), `security_headers.py` (HSTS/CSP/etc. on every response), `credentials.py` (Fernet at rest for POS/OAuth columns), `csrf.py`, `guest_links.py` (signed public tokens), `http_layer.py` (gzip, cache headers, the rolling latency window), `permissions.py` (roles and the per-module view gates), `provisioning.py` (account creation from a signed contract). The controls and the env vars they need: `docs/ops/SECURITY.md`.

## Configuration and the demo accounts

`config.py` holds the environment values more than one module reads — `base_url()`, `from_email()`, `will_email()`, `on_railway()`, `google_places_key()` — each read at call time with one default; a value read in a single module stays in that module. `ai_utils.MODELS` / `model_for()` / `get_client()` are the same idea for the model calls. `demo_seed.py` is the Gia Mia and Simple EJ's seeding and refresh (gated on the account name and `is_demo`), started once at boot by `demo_seed.start_background_seed()`; `models` keeps `_seed_simple_ejs` and `_seed_gia_mia` as wrappers for the tests and boot block that reach them there.

## Automation and moments

**Files**: `delayed.py` (actions queued with an undo window — auto-publish, trusted supplier orders), `decisions.py` (the owner's decision record: what was proposed, what they answered, what was measured after), `milestones.py` (firsts an owner is told about once), `good_news.py` and `first_look.py` (the wins and the first-week read the brief and emails draw on), `covers.py` (covers per day), `promise.py` (the sales audit's promise, measured), `review_common.py` (the sentences the weekly and monthly reviews share), `rec_delivery.py` (a recommendation is recorded as shown when it is DELIVERED: builders stage what they render and the sender presents it only after a successful send; which keys are answerable at all; the rec=/src= links and the open a load from one records), `reply_edits.py` (what the owner changes in a drafted reply, measured, and the drafter's OWNER'S EDITS note). `status_routes.py`/`status_manager.py` are the public status page; `social_routes.py` the Instagram/Facebook OAuth and publishing; `sales_audits.py` the sales-audit store behind `sales_audit_routes.py`. `audit_app.py` is a separate standalone app (the digital audit scorecard), not part of the web process.

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

Bands (confidence audit 9/24/26): a post's lift is a move only past
t(90%, n−1) × the standard error of window-mean minus baseline-mean
(`noise_band_pct`, floor 5%, `false_alarm_rate` 0.10), and only with a
baseline spanning ≥2 different weeks; the promoted dish's unit lift carries
its own band and `item_verdict`; a group median carries `verdicts` and a
`verdict` that is "lifted" only when most of its posts lifted; a
period-on-period change % needs ≥3 posts in each period (`change_note`
says why otherwise).
