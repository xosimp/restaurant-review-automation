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

**Files**: `labor.py` (ingestion, aggregation, glue), `shift_quality.py` (pure scoring engine), `scheduler.py`'s job pieces, `models.py`'s staff/schedule tables.

### Data flow
POS shift CSV (`date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes`) → `labor.py` loads and aggregates → labor % vs `labor_target_pct`, overtime detection (1.5× over 40h on the restaurant's own `week_start_day`-anchored workweek), overstaffed-day detection (high labor % on a day, contrasted against sales) → rendered on the Labor tab (web `#panel-labor`, iOS `LaborView`).

### Shift Quality Engine (`shift_quality.py`)
Evaluates a *generated* schedule, not raw historical shifts. `ShiftContext` (per-shift facts: role, flagged constraints, closing, elsewhere-that-day, prior pattern, availability) feeds eleven `DimensionResult`s combined via the `DIMENSIONS` registry into one 0–100 score:

| Dimension | Default weight |
|---|---|
| coverage | 20 |
| operational_strength | 18 |
| leadership | 15 |
| demand_match | 10 |
| labor_efficiency | 10 |
| experience_balance | 8 |
| training_balance | 7 |
| fatigue | 5 |
| fairness | 3 |
| stability | 2 |
| cross_training | 2 |

Weights are per-restaurant editable (`quality_weights_json`). Three invariants hold everywhere:
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

**Files**: `ask_cavnar.py` (context snapshot builder + `ask_with_tools`), `ask_cavnar_tools.py` (the tool registry, 43 tools), `business_intelligence.py` (the cross-module layer), `home_brief.py` (feeds the opening briefing).

**Design stance**: an AI-powered restaurant COO, not a chatbot wrapper. Every question gets a fresh `build_context()` snapshot (identity, sibling locations, alerts, memory, module data the restaurant's tier actually has) plus a filtered tool list (`tool_specs(restaurant)` — a tool tagged with a module the restaurant doesn't have is never offered, so the model can't call it and produce an empty-result apology).

**Tool kinds** (`TOOLS` registry, each entry `{kind, fn, module, spec}`):
- `read` — executes immediately, returns data (e.g. `read_schedule`, `read_team`, `read_alerts`, `remember`, `forget`).
- `action` — executes immediately, no confirmation (reserved for low-stakes/reversible calls).
- `write` — returns a **proposal** (`build_proposal`) the client renders as a confirm card; the actual route it posts to on confirm is the same authenticated endpoint a manual button already uses, so a proposal can never reach anything the owner couldn't do themselves.

**The opening** (`/mobile/api/ask-cavnar/opening`, delegated on web): built from `home_brief`'s attention items + one win, **no model call** — instant, and can't hallucinate since nothing here is generated. Cached with a 5-minute TTL per session so a fixed issue doesn't keep re-warning the owner.

**Memory** (`ask_memory`): the model calls `remember`/`forget` deliberately; nothing lands automatically. Facts are marked "told, not measured" wherever they appear in the snapshot, so the model never presents an owner's stated goal as something it computed.

**Across modules** (`business_intelligence.py`, audit #15): the layer that answers "why did profits drop" rather than six single-module answers. `gather()` collects each module's own executive brief; `correlations()` reports where two of them point at the same day, dish or shift; `money_at_stake()` ranks each module's monthly dollar figure against the others. It invents no thresholds — a link only exists when both sides already cleared their own module's evidence floor — and it never states a co-occurrence as a cause: each link carries `confirm_by` and `alternative`, and the labour link carries `not_a_cause` because this product has no service-time or cover-count data. The money lines are deliberately never summed (a measured cost, a scheduling gap and an elasticity forecast are not addends), and a range stays a range. Reached by the model through `read_business_snapshot`, and summarised into every snapshot by `snapshot_block()`.

**Grounding**: `ai_guard.verify_figures` runs on every answer against the snapshot + every tool payload + the replayed history. An untraceable figure caps confidence at `low` and reaches both clients, which keep the answer and caveat the number.

**Depth** (`_depth_for`): `brief` (Home box, 3 sentences), `standard`, `executive` (what/why/evidence/dollars/action/confidence/what-to-watch). Chosen from the question, deterministically.

**Prompt caching**: `system` is a list of blocks — static rules with a `cache_control` breakpoint, then the live snapshot. ~9,700 tokens cached per turn. Never interpolate per-restaurant data into the static block.

**Don't touch casually**: the module-gating in `tool_specs()`, and the read/write split — a tool that reaches an outside effect (email, public post, scheduled deletion) must be `write`, never `read`/`action`. `auto_approve` and `data_retention` are write tools for exactly this reason. And never call `client_api._do_ai_visibility` from the context path: on a cache miss it fires live Perplexity queries.

---

## Admin (Will-only)

**Files**: `admin_routes.py`, `admin_ops.py` (data layer), `admin_events.py`.

Client health rollup (owner → brand → location), job-run history (`job_runs`/`job_failures`), manual contract send (DocuSign), the sales-audit in-person tool (`sales_audit_*.py`), changelog authoring, status-page incident management. Hash-routed single-page app (`admin.html`), CSRF include required on every mutating call same as the client dashboard.

---

## Account

Profile, Security (2FA + backup codes + trusted devices + sign-in history), Team (owner-only invite/revoke), Connections (Google/Toast real; IG/Square/Clover partial), Billing, Notifications/alert channels, Data export, Help/FAQ. All five iOS sheets share one "identity card" layout (`AccountSheetKit.swift`) — a new sheet reuses it rather than inventing a new chrome.

---

## Auth / Security

See `SYSTEM_ARCHITECTURE.md`'s Auth section for the model. Module-specific note: `_billing_blocked()` and `_module_blocked()` in `auth.py` gate access at the decorator level (a lapsed account or a module the tier doesn't include gets a clear message, not a 500 or a silently empty page).

---

## POS / Platform integrations

`toast.py`/`toast_routes.py` (real, OAuth + shift/sales sync), `square.py`/`square_routes.py`, `clover.py`/`clover_routes.py` — each behind `pos.py`'s `PROVIDER_API` contract: `is_connected`, `sync_to_db`, `build_shifts_csv(restaurant_id, days=60)`. `gmb.py` (Google Business Profile), `meta_api.py` (Instagram), `weather.py` (NWS forecast, cached on the restaurant row for schedule demand-matching).
