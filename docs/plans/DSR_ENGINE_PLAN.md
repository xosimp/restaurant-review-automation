# End-of-Day Closeout & Daily Sales Report (DSR) Engine — the plan

Status: phases 1–4 built (Sep 23 2026) — `dsr/` runs every night on its own.
Phase 5's core (week/period rollups and routes) and phase 6's backend
(`dsr/memory.py`: Ask's read_dsr / find_days / read_week / read_period and
last night in Ask's context; the morning brief's "yesterday" from the DSR;
`dsr/history_import.py` + `POST /dsr/history/import` for the template
layout — Erik's weekly-grid layout waits for his file; the narrative's
manager-safe `operations_summary`) are built; the screens, email, push and
iOS are in progress. First customer: Simple EJ's
(Erik and Jim), on their **official** account, not the test account.

## 0. What Erik actually runs today

His DSR is an Excel workbook ("Simple EJ's DSR"), one sheet per week:

- **Calendar:** "PERIOD 9 · WEEK 1", weeks run **Wednesday → Tuesday**
  (Wed 8/26/26 – Tue 9/1/26). Period 9 week 1 starting Wed 8/26 fits a
  13-period × 4-week year starting Wed 1/14/26 (32 weeks earlier) — **to confirm with Erik.**
- **Per day:** Weather ("Rain Muggy"), Event or Sport ("NOTHING"),
  Influence/Result (his own read of why the day went how it did).
- **Weekly Sales Breakdown**, a column per day plus Total, three blocks:
  - **Last year:** Liquor, Beer, Wine, Total Alcohol, Food/NA Bev,
    Retail/Rental, Total Gross Sales, Total Net Sales.
  - **Budget:** Total Gross Sales per day (Wed $7,500, Thu $9,500, Fri
    $15,000, Sat $20,000, Sun $11,000, Tue $7,000; week $70,000), Total Net.
  - **Current:** Food, Liquor, Beer (Bottle/Can), Wine, Retail, NA Beverage.
- **Other tabs:** Sales Summary, Petty Cash, Check Requests, CC Register.

So the DSR is, first, **his** sheet filled in automatically, and second, the
executive read on top of it. The owner who has kept this sheet by hand for
years has to recognise it, or the rest is noise to him.

## 1. What already exists (reuse, don't duplicate)

| Piece | Where | Role in the DSR |
|---|---|---|
| Manager close-out (went well / wrong / 86'd / callouts) | `closeout.py`, `close_outs` | becomes the Manager Closeout block; extend fields, same table |
| "How tonight went" push at close | `intraday.closing_summary`, `strategy_jobs.run_closing_summary` | **replaced** by "Your DSR is ready" — one notification, not two |
| Business date / late close | `time_utils.business_date`, `service_window` | the day a DSR belongs to |
| Intraday captures (hourly sales) | `intraday.py`, `pos_intraday` | the hourly sales curve |
| POS providers | `pos.py` (Toast, Square, Clover, RPower) | sales, tickets, items, labor |
| Labor analysis, overtime, shift quality | `labor.py`, `shift_quality.py`, `schedule_intel` | Labor block |
| Food drivers, waste, low stock | `food_cost_intelligence.py`, `inventory` | Food block |
| Reviews + analyser + drafts | `review_intelligence.py`, `drafter.py` | Reviews block |
| Marketing posts / campaigns | `marketing.py`, `guest_marketing.py` | Marketing block |
| Weather, events, competitors | `marketing_signals.weather_signal`, `demand_signals`, `competitor.py` | Intel block, and Erik's Weather / Event columns |
| Morning brief | `morning_brief.py` | reads the finished DSR instead of recomputing |
| Ask tools | `ask_cavnar_tools.TOOLS` | gains read_dsr / find_days |
| Guard on AI figures | `ai_guard` | every number in the narrative is checked against the facts |
| Recommendation trail | `rec_ledger` | Owner Actions are presented and tracked like every other rec |

RPower (Erik's POS) exposes what his sheet needs: `salesdepartment` /
`salescategory` (his Food/Liquor/Beer/Wine/Retail/NA Bev split),
`ticketsales` and `ticket` (net, gross, transactions), `ticketitem`
(items), `ticketpayment` (the CC Register tab), `payout` (Petty Cash) and
**`closeday`** — the POS's own "day is closed" record, which is the trigger.

## 2. Architecture: one pipeline, one fact snapshot, many views

```
trigger ──► collect FACTS (deterministic, per block, with status + source)
              │  stored once: dsr_reports.facts_json + dsr_metrics rows
              ├──► AI narrative (one call, reads only FACTS, figures verified)
              └──► renderers, all from the same snapshot:
                     Owner DSR (web + iOS) · Manager DSR · Erik's weekly grid
                     (+ .xlsx) · owner email · manager email · push ·
                     morning brief · Ask context · weekly/period rollups
```

- **Facts are the single source.** Every view renders from the same
  immutable snapshot, so the email, the app and Ask can never disagree.
- **Every metric carries a status:** `ready` · `awaiting` (e.g. "Awaiting
  POS synchronization") · `unavailable` ("Inventory data unavailable") ·
  `not_connected`. Nothing is estimated to fill a gap; a block without data
  says so and the narrative is told it is missing.
- **Ask context is the facts JSON plus the narrative**, not a third prose
  document. Questions like "every day labor exceeded 25%" are answered by
  SQL over `dsr_metrics`, not by a model reading old reports.
- **Recipients (decided):** owners get the Owner DSR, managers get the Manager DSR — by the login's role, per location.
- **Owner vs Manager is a permission view, not a second generation.** The
  manager version drops owner-only lines (loss signals, prime cost, budget
  if the owner hides it) and leads with operations and action items.

## 3. Progressive, not static

The DSR is a row with a real stage machine, persisted and shown as it runs:

| Stage | Meaning | Shown as |
|---|---|---|
| `scheduled` | before close | nothing (no noise during service) |
| `awaiting_close` | past close time, POS day not closed | "Closeout in progress — waiting for the POS to close the day" |
| `collecting` | pulling each block | a checklist ticking: Sales ✓ 10:14 · Labor ✓ 10:15 · Reviews ✓ 10:20 … |
| `writing` | the one AI call | "Writing the summary" |
| `final` | done | report + email + push + brief queued |
| `provisional` | hard deadline hit with a block still awaiting | report marked provisional; revised automatically when the data lands |
| `failed` | exhausted retries | Will is alerted (admin console); owner sees what's missing |

Rules: the stage times are the real times each step finished (they stay in
the report as provenance), there is exactly **one** notification (at
`final`), and a late block produces a new **version** (v2) rather than a
silent edit — the owner sees "Updated 7:10am: sales now final".

## 4. When it runs

1. Per restaurant, local time, after `service_window` close (late closes
   handled by the business date).
2. Poll the POS `closeday` for the business date every 10 min from close
   (RPower: `closeday/getbybusinessdate`; Toast: business-day closed flag).
3. When closed: collect. If a required block is still `awaiting` (e.g. the
   POS has not settled tickets), retry with backoff (10, 20, 40 min).
4. **Hard deadline** (default 4:00am local): generate `provisional` with the
   missing blocks labelled; the 3am/next sync completes it into v2.
5. **Manual "Close day"** button (manager/owner) starts it now.
6. Scheduler job is bounded + resumable (cursor), claims per
   (restaurant, business date, version) — never double-sends.

## 5. Data model (all created at boot in `init_db`)

- `dsr_reports` — id, restaurant_id, business_date, version, status, stages_json
  (stage → finished_at), facts_json, narrative_json, provisional, trigger
  (closeday / deadline / manual), generated_at, finalized_at, error,
  UNIQUE(restaurant_id, business_date, version).
- `dsr_metrics` — restaurant_id, business_date, metric, value, source,
  status; indexed (restaurant_id, metric, business_date). The searchable
  history ("labor % > 25", "net sales by weekday").
- `dsr_budgets` — restaurant_id, business_date, gross, net (Erik's Budget
  row; entered per week, carried forward).
- `dsr_category_map` — restaurant_id, pos_department → DSR category (Food,
  Liquor, Beer, Wine, Retail, NA Beverage; Erik's labels, editable).
- `fiscal_calendar` — restaurant columns: fiscal_week_start_dow, fiscal_year_start,
  period_scheme (13×4 / 4-4-5) — drives "Period 9 · Week 1".
- `dsr_history_import` — last-year rows imported from his past DSR workbooks
  (RPower only holds ~1 month for him).
- `close_outs` gains: staff_callouts (exists as callouts), equipment,
  vip_guests, maintenance, shift_notes, general_notes, influence (his
  "Influence/Result").

## 6. Report content (by block; each marked with its source)

- **Sales:** gross, net, transactions, average ticket, vs yesterday, vs same
  day last week, vs budget, vs last year, vs forecast (existing demand
  forecast), hourly curve, category split (his six), top/bottom items.
- **Labor:** labor $ and %, hours, overtime, vs target, coverage (no-shows /
  late clock-ins that day), shift quality of the published day,
  observations (e.g. hours after 6pm vs sales after 6pm).
- **Food:** estimated food cost (theoretical, from recipes × item sales —
  labelled estimated), waste logged, low stock, recoverable, variance only
  where a count exists.
- **Reviews:** received, rating, themes, urgent, drafts ready.
- **Marketing:** posts published, campaign results, what's scheduled.
- **Intel:** weather (actual), events/sports, competitor moves, traffic
  impact only where measured.
- **Operational summary + executive summary + owner actions** — the AI
  narrative, ranked by the existing urgency × dollars rule, presented to
  rec_ledger so answers and outcomes are tracked.
- **Manager closeout** — the manager's own words, never rewritten.

## 7. AI generation

- One call per restaurant-night (Sonnet; ~$0.02–0.05), strict JSON schema:
  executive_summary, went_well[], needs_attention[], biggest_{risk, win,
  financial_opportunity, staffing_concern}, actions_tomorrow[3], each item
  citing the fact keys it rests on.
- Input: the facts snapshot + last 7 DSR summaries + open issues + decisions
  memory ("not for us" etc.). No raw data dumps.
- `ai_guard` verifies every figure against the facts; unverifiable lines are
  dropped, and a narrative with too little data is refused rather than padded
  ("Not enough data tonight for a summary — sales are still syncing").
- No AI call when sales are not ready (the provisional path writes facts only).

## 8. Surfaces

- **Web:** a "Daily report" entry (Home card "Last night" → full report):
  Executive summary, KPI strip, financial performance with charts, labor,
  food, reviews, marketing, intel, manager notes, tomorrow's priorities,
  historical comparison; expandable sections; Erik's weekly grid view with
  **Export to Excel** in his exact layout.
- **iOS:** the same report as expandable cards, readable in under two
  minutes; push "Your DSR is ready" opens it; the progressive checklist
  while it runs.
- **Email:** owners get the Owner DSR (summary, KPIs, wins, risks,
  priorities, "View full report"); managers get the Manager DSR. Light-mode,
  `emails.BRAND`, M/D/YY.
- **Morning brief** reads the final DSR; **weekly and period rollups** read
  `dsr_metrics`.
- **Ask:** tools `read_dsr(date)` and `find_days(metric, op, value, range)`.

## 9. Security & scale

- Every read is restaurant-scoped; managers see only their location and the
  manager view; owners see their authorized locations; group owners get a
  per-location list. Permission tests for each route (web + mobile twins).
- 1 → 1,000 locations: per-restaurant local triggers, bounded resumable
  sweep, one AI call each, facts cached in the row, views render from JSON
  (no recompute on open), history queries hit indexed `dsr_metrics`.

## 10. Phases (each ships and is testable on its own)

1. **Foundation:** tables, fiscal calendar, budgets, category map, facts
   collector for Sales + Labor with statuses, `dsr_metrics`. Tests on fixtures
   for Toast and RPower shapes.
2. **Pipeline:** stage machine, closeday polling, retries, provisional/final
   versions, manual Close Day, scheduler job, admin visibility.
3. **Remaining blocks:** Food, Reviews, Marketing, Intel, extended manager
   closeout.
4. **AI narrative** with guard, refusal and rec_ledger actions.
5. **Views:** Owner + Manager web, Erik's weekly grid + Excel export, email,
   push (replacing the closing-summary push), iOS.
6. **Memory:** morning brief from DSR, Ask tools, weekly/period rollups,
   last-year import from his old workbooks.

## 11. Questions for Erik (block the parts that must match his sheet)

1. Calendar: 13 periods × 4 weeks, weeks Wed–Tue, period 1 starting Wed
   1/14/26? (inferred from Period 9 Week 1 = Wed 8/26/26; or a 4-4-5 year)
2. Gross vs net: what comes off gross — comps, discounts, voids, tax?
3. The six categories: how do his RPower departments map (draft beer under
   Beer? "Retail/Rental" = merch + room rental?).
4. Budget: does he set it weekly per day? Where does it come from?
5. Last year: can he send past DSR workbooks so Last Year fills from day one?
6. ~~Who gets which version~~ **Answered (Will, 9/23/26):** every owner login gets the Owner DSR (Simple EJ's: Erik and Jim); every manager login gets the Manager DSR. Still open: how soon after close.
7. Petty Cash, Check Requests, CC Register tabs — in scope now or later?
8. What goes in "Influence/Result"?

## Not known yet — must not be assumed

- RPower access is still pending — Justin is expected to hand over the token 9/24/26; no live call
  has been made against Erik's store, so department names, closeday timing
  and rate limits are unverified.
- His official Cavnar account does not exist yet.
