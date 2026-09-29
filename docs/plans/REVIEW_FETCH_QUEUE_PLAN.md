# The review fetch's AI work on its own queue (#98), and what is left of #131

Plan, 9/29/26 (admin-console fix round, findings #98 and #131). Nothing here
is built. Grounded in `scheduler.py` at `fixround-0929`.

## Where it stands

**The review fetch** (`scheduler.run_daily_fetch`, job `review_fetch`) runs
four times a day (8am, 12pm, 4pm, 8pm Central — `_latest_slot`, the latest
missed slot only) ON THE LOOP THREAD, with a pool of `FETCH_WORKERS` (6)
under a wall-clock bound of `FETCH_MAX_SECONDS` (3 hours) and a per-restaurant
prefix cursor in `job_cursors`. For each restaurant, in one function
(`_process_restaurant`): fetch (Google Business Profile, else Places), save,
**analyse** the new reviews (a model call per review — `analyser.analyse_review`),
**alert** on the urgent ones (the alert needs the analysis: urgency and the
health keywords decide it), **draft** replies (a model call each —
`drafter.draft_response`), stamp `last_fetched_at` only on a fetch that
worked, and record the sync and Data Health. The same code serves the
console's per-client "Fetch now" (`restaurant_ids=[rid]`, on the admin task
pool).

The pass has a fixed coverage ceiling: 6 workers × 3 hours is 64,800 seconds
of per-restaurant work per slot, so roughly 3,000–13,000 restaurants
depending on the seconds `t` one restaurant takes — dominated by the model
calls, not the fetch. `t` has not been measured.

**What the fix round did** (D, #98 / #131): the weekly Intel sweeps
(`competitor_analysis`, `ai_visibility`, bounded at 3 hours each) moved onto
the `intel` lane beside the loop (`scheduler._LANES`); a local-time window
that closed with nothing sent is recorded (`missed_windows`, from
`scheduler.local_due` and `morning_brief.run_due`); the pulse keeps the minute
duties running during a long job; the fetch and the weekly sweeps keep a
per-restaurant cursor and skip restaurants already done this week.

**What is left** (#131): the review fetch (up to ~200 minutes) and the two
diagnoses passes (`DIAGNOSES_MAX_SECONDS`, 40 minutes each) still run on the
loop thread, so at scale the morning brief, the weekly digests, onboarding and
daily alerts (whose windows close at 2pm local), the intraday captures and the
DSR can wait behind them. Intraday (`claim_period("intraday", "<day>-<hour>-<minute//20>")`)
and the DSR sweep (`"<day>-<hour>-<minute//10>"`) claim only the CURRENT slot:
a slot a long pass sat through is neither run late nor recorded.

## Step 0 — measure `t`

Before building anything, from production's own ledger:

```sql
SELECT date(started_at) AS day, COUNT(*) AS runs,
       ROUND(AVG((julianday(finished_at) - julianday(started_at)) * 86400.0
             / NULLIF(json_extract(result_json, '$.attempted'), 0)), 1) AS seconds_per_restaurant,
       MAX(json_extract(result_json, '$.hit_bound')) AS hit_bound
FROM job_runs WHERE job = 'review_fetch' AND finished_at IS NOT NULL
GROUP BY day ORDER BY day DESC LIMIT 30;
```

and the model share of it from `ai_usage` (`action` in the analysis and
drafting actions, `latency_ms`, per restaurant and slot). The Jobs page
(`GET /admin/api/jobs/review_fetch/runs`) shows the same runs. Build the
queue when `seconds_per_restaurant × restaurants ÷ 6` passes about half of the
3-hour bound, or when `hit_bound` appears.

## The design

**Split the pass into a fast fetch and a queue of AI work, keeping the one
invariant that matters: nobody is alerted about a review before it is
analysed.**

1. **Fetch** (`review_fetch`, on a lane, not the loop thread): fetch and save
   new reviews, stamp `last_fetched_at` and record the sync and Data Health —
   network only, seconds per restaurant. Enqueue one work item per restaurant
   that received new reviews.
2. **The AI queue** (`review_ai`, `job_queue` rows — the table
   `POSTGRES_AND_WORKERS_PLAN.md` describes, or its own table first): a
   bounded worker pool with its own time bound and `max_minutes`, claiming
   items oldest first; for each restaurant, analyse its pending reviews, THEN
   fire the urgent alerts (the alert moves here, after the analysis, exactly
   as today's order), THEN draft. The pending reviews are already the natural
   queue (`models.get_pending_analysis`, `get_pending_drafts`; a review past
   `MAX_AI_ATTEMPTS` is stalled and shows on the console with Retry AI), so
   an item is only "this restaurant has work", and a crash loses nothing.
3. **Budget and breakers** stay where they are (`ai_utils`): a queue item
   whose calls are refused by an open breaker or a spent ceiling is left for
   the next pass, not failed.
4. **"Fetch now"** fetches inline on the admin pool and enqueues at the front.

Tests that pin today's single-function order (analyse → alert → claim → draft
in one call) are rewritten against the new contract: after the fetch step no
alert has fired; after the AI step the urgent alert has fired exactly once.

## The rest of #131

- **The diagnoses** (`review_diagnoses`, `food_cost_diagnoses`) move to a
  lane like the Intel sweeps (they are model-bound and not time-critical to
  the minute), or onto the AI queue.
- **Then the loop thread runs only short jobs**, so briefs, digests,
  onboarding, alerts, intraday and the DSR cannot wait behind a long pass.
  The per-job watchdog (`max_minutes`) already tells us if one does.
- **Skipped slots are recorded**: when intraday or the DSR sweep claims a
  slot, it checks the previous slot and records it in `missed_windows` if it
  was never claimed. The DSR needs no catch-up (each night is idempotent and
  the next sweep completes it); a skipped intraday capture is a lost reading,
  shown as a gap, never interpolated.

## Order of work

1. Measure `t` (above) and put the query on the Jobs page.
2. Diagnoses onto a lane (small, no ordering risk).
3. The review fetch onto a lane with the AI queue split out.
4. Record skipped intraday and DSR slots.
