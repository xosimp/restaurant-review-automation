"""
jobs_registry.py — the one table of scheduled jobs.

scheduler.scheduler_loop decides WHEN each job runs (its claim_period gates);
this table says WHAT each job is, and everything that used to keep its own
list reads it:

  * ops.EXPECTED_JOBS — the SLA the request-path watchdog checks (every job
    with an `sla_minutes`), so a job that silently stopped pages the
    operator. It listed 14 of the loop's 55 jobs (#31).
  * admin_ops.RUNNABLE_JOBS — what the console's Jobs page lists and what
    "Run now" may start. It listed 27 (#40, #56).
  * the loop's retry of a failed non-sending job (`retry`), the per-job
    runtime watchdog and "stuck" threshold (`max_minutes`), and the lane a
    long network-bound sweep runs on instead of the loop thread (`lane`).

A job's NAME is the name ops.run_job records it under (job_runs.job).
tests/test_fix_d_jobs.py fails when a `run_job("…")` in the loop has no
entry here, or an entry here is never run — the two lists cannot drift
again — and runs every entry's target to hold it to the standard counts.

Fields
------
cadence       when it runs, for people (the loop is the source of truth)
sla_minutes   the most minutes that may pass between SUCCESSFUL runs
              (job_runs.ok 1, or a partial ok 2 that failed under half of
              what it attempted — ops._healthy_run, Audit #25 A07-03) before
              it is overdue; None = no SLA (a quarterly job outlives
              job_runs' 45-day retention)
sends         it can email, text or push a person (an owner, a guest, the
              operator). "Run now" refuses these unless
              scheduler.scheduling_allowed() (#9): a laptop has production's
              Resend and Twilio keys and a stale copy of the claims.
runnable      "Run now" may start it (the console's button)
label         a short name for the console
description   one sentence: what it does
target        (module, function) Run now calls
run_kwargs    keyword arguments Run now passes, or a callable(now) -> dict
              for the ones that depend on the clock (the loop passes the
              same)
max_minutes   the job's own bound plus a margin: a run older than this is
              stuck (the Jobs page) and the watchdog stops vouching for the
              scheduler (status_manager heartbeat)
claim         the claim_period job key, where it differs from the name
retry         a failed run gives its period back for up to
              RETRY_BACKOFF_MINUTES attempts (non-sending daily/weekly jobs
              only: an hourly job's next hour is its retry, and a sending
              job must not be sent twice); a partial run that failed at
              least PARTIAL_RETRY_SHARE of what it attempted, once, after
              PARTIAL_RETRY_MINUTES
lane          run on this bounded worker lane beside the loop, not on the
              loop thread (scheduler._LANES) — the weekly Intel sweeps
              run for hours and blocked briefs, DSR and intraday (#98);
              "ai" holds the long model jobs (auto-draft, weekly plan,
              recipe drafts, weekly digests — AI cost audit 10/7/26 #6;
              the Labor pre-warm, #100); "sweep" the 4-6am restaurant
              sweeps and the diagnoses (#54), each waiting on what it
              reads (scheduler._PulsedOps.settled); "dsr" the DSR sweep,
              submitted by the pulse too while an inline job holds the
              loop, "pos" the 3am POS chain (pos_sync, loss_sync, the DSR
              backfill) and "fetch" the review fetch (scale audit 10/19/26
              INF-1, INF-2, INF-9); "minute" the batch collector, the DSR
              delivery and the staff reminders, "live" live labor and the
              POS schedule push, "intraday" the service slot's nine jobs,
              "brief" the morning briefs (blind re-audit 10/20/26 R1-02,
              R1-11) — submitted by the pulse too. A lane
              run gets the loop's retry when it fails
queue         run as a job_queue row in the queue worker process (the
              "maintenance" group, one at a time), not on the loop or a
              lane: the loop claims its period and enqueues it
              (scheduler.queue_scheduled_job); the worker runs it through
              ops.run_job under this name (scheduler.
              run_queued_scheduled_job), so job_runs, the SLA and Run now
              read it as before. The nightly backup and the restore drill
              (scale re-audit 10/20/26 R8-03, R8-15): out of the web process
              and off the loop. A failed retryable run is retried by the
              queue's own backoff
salary_view   "owner" for a job whose every output goes to the account holder
              alone (or is kept per salary view, keyed as the owner's): it
              runs in models.salary_view(True), all-in labor. Every other job
              runs in salary_view(False) — hourly labor, no salaries — set by
              ops._job_context (blind re-audit 10/26/26 B3 P0: off a request
              every read was the owner's, and the daily issue scan texted a
              manager the all-in labor %). A job that sends to owners and
              managers alike builds each recipient's copy in that
              recipient's view (morning_brief.deliver), never "owner".
              OWNER_SALARY_JOBS lists them; tests hold the two together

Owner-facing SLAs are about 3 h for hourly jobs, 26 h for daily, 8 days for
weekly (#31).
"""

# One stale threshold for the scheduler heartbeat, everywhere (#4): the
# status page, /health, the platform SLA page and the console used 15 and 20.
HEARTBEAT_STALE_MINUTES = 15

# A failed non-sending job (retry=True) waits this long before each retry;
# after the last one the period stays spent and the failure is the digest's
# and the SLA's to report (#56).
RETRY_BACKOFF_MINUTES = (10, 30, 90)
# A PARTIAL run of a retry=True job that failed at least this share of what
# it attempted gives its period back once, this many minutes later (Audit
# #25 A07-12, scheduler._PulsedOps._after): a provider blip that failed a
# third of the nightly learners left those restaurants a period stale.
PARTIAL_RETRY_SHARE = 0.2
PARTIAL_RETRY_MINUTES = 30

# A job with no bound of its own is stuck after this long.
DEFAULT_MAX_MINUTES = 60

_H, _D, _W = 3 * 60, 26 * 60, 8 * 24 * 60      # hourly, daily, weekly SLAs


def _chi_weekday(now):
    return {"weekday": now.weekday()}


def _chi_now_kw(now):
    return {"now": now}


JOBS = {
    # ── nightly infrastructure ───────────────────────────────────────────
    "backup_db": dict(
        cadence="2am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="Nightly backup",
        description="Snapshot the database on the volume, then copy it off-site (object storage and the encrypted email)",
        # Bound: ~91 s a GB measured (10/20/26), so four hours is ~150 GB;
        # half of it pages (scheduler._backup_too_slow). In the queue worker
        # (R8-03) a long run holds nothing else up.
        target=("scheduler", "backup_db"), max_minutes=240, retry=True, queue="maintenance"),
    "prune_ledgers": dict(
        cadence="2am CT nightly, once the backup has finished (6am at the latest)", sla_minutes=_D, sends=False,
        runnable=True, label="Retention",
        description="Delete ledger rows past their retention window, in chunks, then refresh planner statistics; "
                    "soft-delete reviews past each owner's retention",
        target=("scheduler", "run_nightly_retention"), max_minutes=45),
    "offsite_lifecycle": dict(
        cadence="weekly, from 3am CT", sla_minutes=_W, sends=False, runnable=True,
        label="Off-site backup expiry",
        description="Read the object store's lifecycle rule and page when no rule deletes backup copies within "
                    "BACKUP_OFFSITE_MAX_DAYS",
        target=("scheduler", "run_offsite_lifecycle_check"), max_minutes=5),
    "restore_drill": dict(
        cadence="2nd of every month, after the 2am backup", sla_minutes=None, sends=True, runnable=True,
        label="Restore drill",
        description="Restore the newest snapshot and the newest off-site copy to scratch, prove they are fresh, open, "
                    "migrate and kept their tokens (the off-site copy's sealed ones open), and time each stage",
        target=("scheduler", "run_restore_drill"), max_minutes=60, queue="maintenance"),
    "pos_sync": dict(
        cadence="3am CT nightly (POS lane)", sla_minutes=_D, sends=False, runnable=True,
        label="POS sync", description="Queue one sync row per connected store (job_queue kind pos_sync), due at the "
                                     "store's own 3-5am, run by the queue worker: yesterday's sales and labor; then "
                                     "inventory systems",
        target=("scheduler", "run_toast_sync"), max_minutes=75, retry=True, lane="pos"),
    # The RPOWER budget's nightly backpressure check (RPOWER audit #23 A4-12
    # / A5-01): demand against the nightly window's budget, bulk shed and
    # live labor stretched when the night does not fit (pos_capacity).
    "pos_budget_check": dict(
        cadence="2am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="RPOWER budget check",
        description="Tonight's RPOWER demand (stores x requests a store-night) against the nightly window's budget: "
                    "over it, onboarding waits for 7am and live labor reads less often outside decision windows; "
                    "close and the nightly archive are never shed",
        target=("pos_capacity", "budget_check"), max_minutes=5, retry=True),
    # The Labor read written ahead of the morning open (AI cost audit 10/7/26
    # #100), for restaurants whose owner opened it in the last week.
    "labor_prewarm": dict(
        cadence="3am CT daily, after the POS sync (AI lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Labor read pre-warm",
        description="Write the Labor read after the POS sync for restaurants whose Labor read was opened in "
                    "the last 7 days, so the morning open is a stored read (bounded, resumable)",
        target=("scheduler", "run_labor_prewarm"), run_kwargs=_chi_now_kw, max_minutes=25, lane="ai",
        # The owner's Labor read, kept under the owner's key (insight_store.
        # salary_kind): a manager's own hourly read is built on open.
        salary_view="owner"),
    "pos_retry": dict(
        cadence="hourly, until 11am local", sla_minutes=_H, sends=False, runnable=True,
        label="POS retry", description="Retry failed POS syncs whose retry is due (+1h, +3h, +6h; never an auth failure), "
                                       "and sync each store the 3am pass left for its own local hour",
        target=("scheduler", "run_pos_retry"), max_minutes=25),
    "stripe_reconcile": dict(
        # Reads Stripe; never changes a billing status (billing_jobs, #115).
        cadence="3:30am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="Stripe reconcile",
        description="Refresh the subscription mirror from Stripe and record where Stripe and the local billing state "
                    "disagree (bounded, resumable)",
        target=("billing_jobs", "reconcile_stripe"), max_minutes=20, retry=True),
    "loss_sync": dict(
        cadence="3am CT nightly, after the POS sync (POS lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Loss sync", description="Pull comps, voids and refunds from POSes that report them",
        target=("strategy_jobs", "run_loss_sync"), max_minutes=45, retry=True, lane="pos"),
    # Halsted & Rye, the showcase demo (demo_store): what reaches a real
    # store from outside its POS — invoices, reviews, the kitchen count, the
    # coming week's schedule — written for the night, so the demo is current
    # whenever it is opened. The POS side is the ordinary nightly jobs.
    "demo_store_refresh": dict(
        cadence="6am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="Showcase demo upkeep", description="Halsted & Rye (demo_store): the night's distributor and kitchen "
                                                  "invoices, reviews, kitchen count, posts, next week's schedule "
                                                  "and month-end bar count; sends nothing",
        target=("demo_store.build", "run_refresh"), max_minutes=20),
    "guest_followups": dict(
        # The next-day thank-you (guest_hub, 10/20/26): yesterday's guests who
        # hold marketing consent, at restaurants that turned it on. Each guest
        # is claimed before the send, so a retry never texts twice.
        cadence="11am CT daily", sla_minutes=_D, sends=True, runnable=True,
        label="Guest thank-you texts",
        description="Text yesterday's opted-in guests the restaurant's thank-you, once each, inside the guest "
                    "sending hours (bounded, resumable)",
        target=("guest_hub", "run_followups"), max_minutes=10, retry=True),
    "texting_setup_advance": dict(
        # A restaurant's own texting registration (texting_setup, 10/20/26):
        # poll the carriers' review of its brand and campaign, register the
        # campaign once the brand is approved, and switch its guest texts to
        # its own number once the campaign is verified.
        cadence="hourly", sla_minutes=_H, sends=True, runnable=True,
        label="Texting setup",
        description="Move each restaurant's Twilio texting registration on a step: brand review, campaign, "
                    "then its own number live (bounded, resumable)",
        target=("texting_setup", "run_advance"), max_minutes=5, retry=True),
    "guest_pos_match": dict(
        # After the POS archive (4am): RPOWER checks with a customer attached
        # become guest visits and the guest's usual (guest_hub phase 3).
        cadence="5am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Guests from POS checks",
        description="Match yesterday's RPOWER checks that carry a customer to guests: a visit and what they "
                    "ordered; sends nothing",
        target=("guest_hub", "run_pos_guest_match"), max_minutes=10, retry=True),
    "pos_archive": dict(
        cadence="4am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="POS archive", description="Queue one archive row per restaurant whose POS can archive (job_queue "
                                         "kind pos_archive), waiting for its 5am local boundary: the last "
                                         "complete night's tickets, sale lines and punches once the POS has "
                                         "closed it, days the POS restated, a week of backfill",
        target=("strategy_jobs", "run_pos_archive"), max_minutes=30, retry=True),
    # The Daily Sales Report's figures for the nights before it started, its
    # own job (bar audit 10/14/26 RPW-13 / R11): it ran inside the POS sync
    # with every failure swallowed.
    "dsr_backfill": dict(
        cadence="3am CT nightly, after the POS sync", sla_minutes=_D, sends=False, runnable=True,
        label="DSR backfill", description="Fill the Daily Sales Report's figures for nights before it started, "
                                          "from the POS (figures only: no report, email or model): one queue "
                                          "row per restaurant, run by the queue worker (ONB-5)",
        target=("strategy_jobs", "run_dsr_backfill"), max_minutes=40, retry=True, lane="pos"),
    "intelligence_features": dict(
        cadence="3am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Intelligence features", description="The intelligence engine's per-restaurant feature pass (bounded, resumable)",
        target=("intelligence.jobs", "run_features"), max_minutes=60, retry=True),
    "intelligence_learning": dict(
        cadence="4am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Intelligence learning", description="Learning over the materialized intelligence tables",
        target=("intelligence.jobs", "run_learning"), max_minutes=60, retry=True),
    "intelligence_features_backfill": dict(
        # Memory audit 9/29/26 (PLATFORM-5): past weeks from the raw tables.
        cadence="5am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Features backfill",
        description="Compute the past weeks each restaurant has raw history for and no current-version feature "
                    "row (bounded, resumable)",
        target=("intelligence.jobs", "run_features_backfill"), max_minutes=10, retry=True),
    "schedule_experiment_verdicts": dict(
        # Memory audit 9/29/26 (PLATFORM-13): the A/B verdict, stored weekly.
        cadence="Mon 5am CT", sla_minutes=_W, sends=False, runnable=True,
        label="Schedule A/B verdicts",
        description="Store each schedule experiment's clustered verdict for the week",
        target=("schedule_experiments", "record_verdicts"), max_minutes=15, retry=True),
    "marketing_metrics_sync": dict(
        cadence="4am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="Marketing metrics", description="Refresh Instagram / Facebook post metrics",
        target=("scheduler", "run_marketing_metrics_sync"), max_minutes=40, retry=True),
    "schedule_outcomes": dict(
        cadence="4am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Schedule outcomes", description="Record what each published week actually did, by daypart",
        target=("strategy_jobs", "run_schedule_outcomes"), max_minutes=30, retry=True),
    "schedule_scorecard": dict(
        # Schedule audit 10/10/26 RM-4, LEARNING-7: one row per published
        # week, frozen 14 days after it ended, after schedule_outcomes and
        # the memory have read the week.
        cadence="Mon 7am CT", sla_minutes=_W, sends=False, runnable=True,
        label="Schedule Learning Scorecard",
        description="Score each ended published week: the draft's rework rate (unreviewed publishes left out), "
                    "requirement error, labor against the hourly target, overtime, unmanaged minutes, shortfall, "
                    "uncovered no-shows, forecast error, row origins and the learned memory's influence - one "
                    "queue row per restaurant when the job queue is on, else bounded and resumable",
        target=("schedule_scorecard", "run_scorecard"), max_minutes=20, retry=True),
    "schedule_bench": dict(
        # Scheduler audit 10/10/26 MARKET-26, -27, -28: after event_sync (5am)
        # and the memory (6am) - on-call slots settled, published weeks'
        # commitments recorded, events that changed on a published week
        # proposed to the deciders (studio_bench.run_bench_nightly).
        cadence="7am CT daily", sla_minutes=_D, sends=True, runnable=True,
        label="Schedule bench",
        description="Settle last night's on-call slots, record how firm each published shift is, and propose the "
                    "priced change when an event moved on a published week (bounded, resumable)",
        target=("studio_bench", "run_bench_nightly"), max_minutes=20, retry=True),
    "staffing_learning": dict(
        # Scheduler audit 10/10/26 RM-20, LEARNING-6, LEARNING-26: what one
        # more of a role on a meal is worth (no-show natural experiments and
        # within-restaurant fixed effects, with intervals) and each person's
        # measured effect (partial pooling) — read by the requirements and
        # the owner's ratings screen, never computed on a request.
        cadence="Mon 8am CT", sla_minutes=_W, sends=False, runnable=True,
        label="Staffing learning",
        description="Estimate what one more person of each role is worth on each meal (uncovered no-shows; a "
                    "Holm family over the run; a move binds after two agreeing weeks) and each person's measured "
                    "productivity - one queue row per restaurant when the job queue is on, else bounded and "
                    "resumable",
        target=("staffing_elasticity", "run_elasticity"), max_minutes=15, retry=True),
    "schedule_memory": dict(
        # Schedule audit 10/3/26 L-29: the scheduling memory rebuilt from its
        # sources, after people_nightly.
        cadence="6am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Scheduling memory",
        description="Rebuild what each restaurant's scheduling has learned — habits, openers, sections, "
                    "overtime, late closes, teams — with its confidence and status",
        target=("strategy_jobs", "run_schedule_memory"), max_minutes=30, retry=True),
    "people_nightly": dict(
        cadence="5am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="People memory",
        description="Attendance from last night's punches and checks, covers taken, guest mentions, "
                    "standing schedule patterns and the per-person quarterly summaries",
        target=("strategy_jobs", "run_people_nightly"), max_minutes=30, retry=True),
    "inventory_depletion": dict(
        cadence="5am CT nightly (sweep lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Depletion", description="Deplete inventory from POS sales",
        target=("scheduler", "run_daily_depletion_sync"), max_minutes=60, retry=True, lane="sweep"),
    "food_cost_snapshots": dict(
        cadence="5am CT daily, after depletion (sweep lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Food cost snapshots", description="Write each restaurant's inventory snapshot and score closed forecasts",
        target=("scheduler", "run_food_cost_snapshots"), max_minutes=60, retry=True, lane="sweep"),
    "forecast_scoring": dict(
        cadence="5am CT daily, after the snapshots (sweep lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Forecast scoring", description="Score every frozen forecast whose period has closed",
        target=("scheduler", "run_forecast_scoring"), max_minutes=60, retry=True, lane="sweep"),
    "event_sync": dict(
        cadence="5am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Event sync",
        description=("The Event Intelligence catalog (event_intel): each restaurant's follows by distance and its "
                     "copy of the followed games in demand_signals; past game nights measured (bounded, resumable)"),
        target=("event_intel.engine", "run_event_sync"), max_minutes=15, retry=True),
    "event_memory": dict(
        cadence="5am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Event memory",
        description=("Observed weather, and what each finished night's events, holidays, rain, paydays and "
                     "campaigns did to sales (bounded, resumable)"),
        target=("event_memory", "run_event_memory"), max_minutes=30, retry=True),
    "reservation_sync": dict(
        cadence="5am CT daily, the day before each draft", sla_minutes=_D, sends=False, runnable=True,
        label="Reservations", description="Reservation feeds into events & reservations (no provider live yet)",
        target=("reservation_feeds", "run_reservation_sync"), run_kwargs=_chi_weekday, max_minutes=30, retry=True),
    "quality_calibration": dict(
        cadence="Sun 5am CT", sla_minutes=_W, sends=False, runnable=True,
        label="Quality calibration",
        # Blind re-audit 10/11/26 LEARNING-2: it said "suggest" while it
        # also moved the weights. It does both, and says so.
        description=("Suggest Shift Quality weights (calibrate_weights, what Apply writes) AND run the automatic "
                     "calibration: a candidate fitted on older weeks, checked on held-out weeks and then on new "
                     "ones, WRITTEN to the weights only when SCHEDULE_CALIBRATION_AUTOPROMOTE=1 (off by default) - "
                     "one queue row per restaurant when the job queue is on"),
        target=("strategy_jobs", "run_quality_calibration"), max_minutes=20, retry=True),
    "data_health_daily": dict(
        cadence="6am CT daily, after the snapshots (sweep lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Data health", description="Write each restaurant's Data Health snapshot (data_health_daily)",
        target=("scheduler", "run_data_health_daily"), max_minutes=60, retry=True, lane="sweep"),
    "review_diagnoses": dict(
        cadence="6am CT daily, after its 4am batch (sweep lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Review diagnoses",
        description="Root-cause reads of each restaurant's biggest complaint clusters: the 4am batch's answers "
                    "reused, the rest written now (bounded, resumable)",
        target=("scheduler", "run_review_diagnoses"), max_minutes=60, retry=True, lane="sweep"),
    "food_cost_diagnoses": dict(
        cadence="6am CT daily, after the snapshots and its batch (sweep lane)", sla_minutes=_D, sends=False,
        runnable=True, label="Food cost diagnoses",
        description="Root-cause reads over each restaurant's ranked cost drivers: the batch's answers reused, "
                    "the rest written by the queue worker, one row per restaurant (the loop queues them, "
                    "food_cost_diagnoses_pass; Run now runs the bounded, resumable sweep)",
        target=("scheduler", "run_food_cost_diagnoses"), max_minutes=60, retry=True, lane="sweep"),
    # The diagnoses' Message Batches (AI cost audit 10/7/26 #58): the calls
    # the 6am passes would make, sent at half the price ahead of them.
    "review_diagnoses_batch": dict(
        cadence="4am CT daily (sweep lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Review diagnoses batch",
        description="Plan each restaurant's review diagnoses and send the calls they need as one Message Batch "
                    "(half price); the 6am pass writes what has not landed",
        target=("scheduler", "run_review_diagnoses_batch"), run_kwargs=_chi_now_kw, max_minutes=50,
        retry=True, lane="sweep"),
    "food_cost_diagnoses_batch": dict(
        cadence="5am CT daily, after the snapshots, until 5:30 (sweep lane)", sla_minutes=_D, sends=False,
        runnable=True, label="Food cost diagnoses batch",
        description="Plan each restaurant's food cost diagnosis and send the calls as one Message Batch "
                    "(half price); the 6am pass writes what has not landed",
        target=("scheduler", "run_food_cost_diagnoses_batch"), run_kwargs=_chi_now_kw, max_minutes=50,
        retry=True, lane="sweep"),
    "outcome_evaluations": dict(
        cadence="6am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Outcome evaluations", description="Close outcome trackers whose window ended; mark goals met",
        target=("strategy_jobs", "run_outcome_evaluations"), max_minutes=20, retry=True),
    "outcome_rechecks": dict(
        cadence="6am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Outcome rechecks", description="Re-check measured results at 90 days and accrue measured savings day by day",
        target=("strategy_jobs", "run_outcome_rechecks"), max_minutes=20, retry=True),
    "learning_memory": dict(
        cadence="6am CT daily, after the outcome re-checks (sweep lane)", sla_minutes=_D, sends=False,
        runnable=True, label="Learning memory",
        description="The nightly learning pass for every restaurant allowed to teach a learner: score Cavnar "
                    "AI's claims at their horizon, summarise closed quarters of its reads, the what-worked "
                    "record, trackers for changes made, trigger margins, the learning scorecard and the "
                    "cross-module links (bounded, resumable)",
        target=("scheduler", "run_learning_memory"), max_minutes=60, retry=True, lane="sweep"),
    "value_figures": dict(
        cadence="6am CT daily, after outcome evaluations (Intel lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Value figures",
        description="Each restaurant's four value figures into value_figures_daily, which the Intelligence page "
                    "sums (bounded, resumable)",
        target=("intelligence.dashboard", "snapshot_value_figures"), max_minutes=55, lane="intel",
        # Read by the operator's Intelligence page only (an admin sees
        # salaries): the opportunity gap on the owner's all-in basis.
        salary_view="owner"),
    "value_snapshots": dict(
        cadence="6am CT daily, after outcome evaluations", sla_minutes=_D, sends=False, runnable=True,
        label="Value history",
        description="Each restaurant's measured value point for its own day into value_snapshots (the Home "
                    "sparkline), for every restaurant in service — not only the days someone opened Home "
                    "(bounded, resumable)",
        target=("value_delivered", "run_value_snapshots"), max_minutes=25, retry=True),
    "competitor_analysis": dict(
        cadence="Mon 6am CT (catch-up to Wed), then a daily retry", sla_minutes=_W, sends=False, runnable=True,
        label="Competitor analysis", description="Weekly competitor analysis for full-tier clients (Places + Claude), on the Intel lane",
        target=("scheduler", "run_weekly_competitor_analysis"), max_minutes=200, lane="intel"),
    "competitor_daily": dict(
        cadence="daily 8am CT", sla_minutes=26 * 60, sends=False, runnable=True,
        label="Competitor ratings", description="Daily re-read of the tracked competitors' ratings and review counts "
                    "and the restaurant's own (Places details only); the full analysis again when one moved",
        target=("scheduler", "run_daily_competitor_ratings"), max_minutes=25, lane="intel"),
    "web_analytics": dict(
        cadence="daily 7am CT", sla_minutes=26 * 60, sends=False, runnable=True,
        label="Website analytics", description="Each connected website's Google Analytics and Search Console "
                    "figures into web_analytics_daily (read-only; the first read backfills a year)",
        target=("scheduler", "run_daily_web_analytics"), max_minutes=20, lane="intel"),
    "ai_visibility": dict(
        cadence="Mon 7am CT (catch-up to Wed), then a daily retry", sla_minutes=_W, sends=False, runnable=True,
        label="AI visibility", description="Weekly AI visibility checks for full-tier clients (Perplexity), on the Intel lane",
        target=("scheduler", "run_weekly_ai_visibility"), max_minutes=200, lane="intel"),
    "refresh_tokens": dict(
        # 7am to midnight: the overnight gap is eight hours.
        cadence="hourly from 7am CT", sla_minutes=11 * 60, sends=False, runnable=True,
        label="Token refresh", description="Renew expiring Instagram / Facebook tokens (three attempts a restaurant a day)",
        target=("scheduler", "refresh_expiring_tokens"), max_minutes=20),
    "rec_ledger": dict(
        cadence="8am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Recommendation ledger", description="Repair, sync, expire and tag the recommendation trail",
        target=("scheduler", "run_rec_ledger_pass"), max_minutes=30, retry=True),
    "review_fetch": dict(
        # 8am, 12pm, 4pm, 8pm Chicago: the overnight gap is 12 hours, plus a
        # bounded pass of up to three.
        cadence="8am / 12pm / 4pm / 8pm CT", sla_minutes=16 * 60, sends=True, runnable=True,
        label="Review fetch", description="Fetch new reviews, analyse, draft replies, alert owners (bounded, resumable)",
        target=("scheduler", "run_daily_fetch"), max_minutes=200, lane="fetch"),
    "provider_probes": dict(
        cadence="hourly", sla_minutes=_H, sends=True, runnable=True,
        label="Provider probes",
        description="One authenticated, non-sending call per provider (Resend, Twilio, Anthropic, Stripe, Places, "
                    "APNs); pages Will when one starts failing",
        target=("provider_health", "run_probes"), max_minutes=10),
    # The email intake (bar fix round 10/15/26, V13 / V22).
    "intake_reconcile": dict(
        cadence="hourly", sla_minutes=_H, sends=False, runnable=True,
        label="Intake reconcile",
        description="Queue again every received intake email still queued after an hour with no live job "
                    "(marked failed after three jobs)",
        target=("email_intake", "reconcile_stale"), max_minutes=5),
    "invoice_feed_watch": dict(
        cadence="11am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Invoice feed watch",
        description="Record a failure on the invoices source of each restaurant with an active Fintech address "
                    "and no file for two business days (bounded, resumable)",
        target=("email_intake", "watch_feeds"), max_minutes=5, retry=True),
    "business_metrics": dict(
        cadence="11:50pm CT nightly (a missed night is taken before 6am)", sla_minutes=_D, sends=False,
        runnable=True, label="Business metrics",
        description="The day's MRR, accounts, signups, churn, active users and costs into business_metrics_daily "
                    "(never pruned), and each paying account's churn-risk state",
        target=("admin_ops", "snapshot_business_metrics"), run_kwargs=lambda now: {"day": now.date().isoformat()},
        max_minutes=15, retry=True),
    "ops_failure_digest": dict(
        cadence="8am CT daily", sla_minutes=_D, sends=True, runnable=True,
        label="Failure digest", description="Email Will what failed, what is stuck and what is overdue since the last digest",
        target=("ops", "send_failure_digest"), claim="ops_digest", max_minutes=15),
    "operator_weekly_digest": dict(
        cadence="Mon 7am CT", sla_minutes=_W, sends=True, runnable=True,
        label="Operator weekly", description="One Monday email to Will: pipeline, churn risk, onboarding, failures and costs",
        target=("ops", "send_operator_weekly_digest"), max_minutes=20),

    # ── owner-facing, per restaurant at its own local hour ─────────────
    "outcome_wins": dict(
        cadence="hourly (9am local)", sla_minutes=_H, sends=True, runnable=True,
        label="Outcome wins", description="Tell each owner about a result closed since they were last told",
        target=("strategy_jobs", "run_outcome_wins"), max_minutes=20),
    "milestones": dict(
        cadence="hourly (9am local)", sla_minutes=_H, sends=True, runnable=True,
        label="Milestones", description="Fire savings / anniversary / goal milestones (each fires at most once ever)",
        target=("strategy_jobs", "run_milestones"), max_minutes=20),
    "labor_reminders": dict(
        cadence="hourly (9am local)", sla_minutes=_H, sends=True, runnable=True,
        label="Labor reminders", description="What is waiting on each manager before the next shifts",
        target=("strategy_jobs", "run_labor_reminders"), max_minutes=20),
    # The month-end bar count (bar.counts, audit 10/14/26 F1): counted pour
    # cost needs a shelf count at each month's end.
    "bar_count_reminders": dict(
        cadence="hourly (2pm local on each restaurant's last day of the month)", sla_minutes=_H, sends=True,
        runnable=True, label="Bar count reminder",
        description="On the last day of the month, tell each restaurant with distributor invoices to count the bar "
                    "tonight (once a day; bounded, resumable)",
        target=("bar.counts", "run_count_reminders"), max_minutes=10),
    # A bar price increase that held and is worth $25 a month at usual
    # buying (bar_signals, barfix W9 / Decision 7): pushed once per increase.
    "bar_price_push": dict(
        # Runs only on Mondays, so its SLA is the week's: with _H the watch
        # texted "no successful run in 3.1h" every hour from Thursday 10/15/26.
        cadence="hourly on Mondays (9am local)", sla_minutes=_W, sends=True, runnable=True,
        label="Bar price push",
        description="Push each restaurant's biggest bar price increase that held, worth $25 a month or more, once "
                    "per increase (push only): the loop claims each restaurant's window and queues its push "
                    "(strategy_jobs.bar_price_push_pass); Run now runs the bounded, resumable sweep",
        target=("bar_signals", "run_price_push"), max_minutes=10),
    "open_shift_watch": dict(
        cadence="hourly", sla_minutes=_H, sends=True, runnable=True,
        label="Open shifts",
        description="Expire shift requests whose shift has passed; follow the week they are about; tell deciders about open shifts still unclaimed 12 hours before they start; put approved availability changes in force on their date",
        target=("shift_requests", "run_open_shift_watch"), max_minutes=15),
    "auto_draft_schedule": dict(
        cadence="hourly (6am local on each restaurant's draft day)", sla_minutes=_H, sends=True, runnable=True,
        label="Auto-draft", description="Draft next week's schedule for owners who opted in (a draft; nothing reaches staff)",
        target=("strategy_jobs", "run_auto_draft_schedules"), run_kwargs=_chi_now_kw, max_minutes=50, lane="ai"),
    "weekly_plan": dict(
        cadence="Mon 7am local (hourly on Mondays)", sla_minutes=_W, sends=False, runnable=True,
        label="Weekly plan", description="The agent files the week's three actions as issues (nobody is texted) — queued per restaurant",
        target=("strategy_jobs", "fan_out_weekly_plan"), max_minutes=60, lane="ai"),
    "recipe_drafts": dict(
        cadence="Tue 5am local (hourly on Tuesdays)", sla_minutes=_W, sends=False, runnable=True,
        label="Recipe drafts", description="Draft recipes for POS dishes that have none",
        target=("strategy_jobs", "run_recipe_drafts"), max_minutes=45, lane="ai"),
    "trusted_orders": dict(
        cadence="hourly (8am local on each restaurant's order day)", sla_minutes=_H, sends=True, runnable=True,
        label="Trusted orders", description="Queue supplier orders that have earned it, with an hour to undo",
        target=("strategy_jobs", "run_trusted_orders"), max_minutes=20),
    "auto_publish_schedule": dict(
        cadence="hourly (9am local, the day after the draft)", sla_minutes=_H, sends=True, runnable=True,
        label="Auto-publish", description="Queue the unedited draft to publish with a two-hour undo",
        target=("scheduler", "run_auto_publish_schedules"), max_minutes=20),
    "home_precompute": dict(
        # Queues only (one home_snapshot row per restaurant due); the queue
        # worker builds. Sends nothing.
        cadence="hourly (5am-8am local, once a business day)", sla_minutes=_H, sends=False, runnable=True,
        label="Home precompute",
        description="Queue each restaurant's Home blocks to be built before anyone opens Home",
        target=("home_snapshots", "run_precompute"), max_minutes=10),
    "digest_precompute": dict(
        # Submits only where Message Batches may (ai_batches.enabled: the
        # production scheduler, weekly_digest listed). Sends nothing: the
        # 9am weekly_digests pass reads what comes back, or writes it itself.
        cadence="hourly (2am-9am local on each client's digest day)", sla_minutes=_H, sends=False,
        runnable=True, label="Digest precompute",
        description="Send each digest's narrative through Message Batches before the 9am send (half price)",
        target=("reporter", "run_digest_precompute"), max_minutes=20, lane="ai",
        salary_view="owner"),
    "weekly_digests": dict(
        cadence="hourly (9am local on each client's digest day)", sla_minutes=_H, sends=True, runnable=True,
        label="Weekly digests", description="Email weekly digests (bounded, resumable, on the AI lane)",
        target=("scheduler", "run_weekly_digests"), claim="weekly_digest", max_minutes=30, lane="ai",
        # Sent to the owner's addresses only (get_owner_emails).
        salary_view="owner"),
    "daily_alerts": dict(
        cadence="hourly (10am local)", sla_minutes=_H, sends=True, runnable=True,
        label="Daily alerts", description="No-response, trend, threshold, labor, competitor and data-source alerts, batched",
        target=("scheduler", "run_daily_alert_checks"), max_minutes=30),
    "monthly_summary": dict(
        cadence="hourly (9am local on the 1st)", sla_minutes=_H, sends=True, runnable=True,
        label="Monthly summary", description="The monthly business review email",
        target=("scheduler", "run_monthly_summaries"), max_minutes=30,
        # Sent to restaurants.owner_email only.
        salary_view="owner"),
    "quarterly_summaries": dict(
        cadence="hourly on the 1st of Jan / Apr / Jul / Oct (9am local)", sla_minutes=None, sends=True, runnable=True,
        label="Quarterly summary", description="The quarter that just ended",
        target=("scheduler", "run_quarterly_summaries"), claim="quarterly_summary", max_minutes=30,
        salary_view="owner"),
    "onboarding_emails": dict(
        cadence="hourly (10am local)", sla_minutes=_H, sends=True, runnable=True,
        label="Onboarding", description="Day-2 / 7 / 30 onboarding and the 60 / 90 / 180-day lifecycle emails",
        target=("scheduler", "run_onboarding_sequence"), run_kwargs={"local_hour": 10}, claim="onboarding",
        max_minutes=30),
    "onboarding_nudges": dict(
        cadence="hourly (11am local)", sla_minutes=_H, sends=True, runnable=True,
        label="Onboarding nudges",
        description="One email per missing setup step (Google, brand voice, first approval, the app), each step once",
        target=("scheduler", "run_onboarding_nudges"), run_kwargs={"local_hour": 11}, max_minutes=30),
    "contract_chase": dict(
        cadence="10am CT daily", sla_minutes=_D, sends=True, runnable=True,
        label="Contract chase",
        description="Re-send the pay link on days 2, 5 and 9 after signing to a client who has not paid",
        target=("billing_jobs", "run_contract_chase"), max_minutes=5),
    "dunning": dict(
        cadence="hourly", sla_minutes=_H, sends=True, runnable=True,
        label="Dunning",
        description="Owe a dunning email for a failed invoice attempt the Stripe webhook missed, stand down dunning "
                    "for invoices since paid, then send",
        target=("billing_jobs", "run_dunning"), max_minutes=5),
    "stale_inventory": dict(
        cadence="Mon 10am CT", sla_minutes=_W, sends=True, runnable=True,
        label="Stale inventory", description="Email Will the clients whose inventory data is stale",
        target=("scheduler", "check_stale_inventory"), max_minutes=15),
    "inactive_clients": dict(
        cadence="Mon 11am CT", sla_minutes=_W, sends=True, runnable=True,
        label="Inactive clients", description="Email Will the clients who haven't signed in for 14+ days",
        target=("scheduler", "check_inactive_clients"), max_minutes=15),
    "while_away": dict(
        cadence="Mon 11am CT", sla_minutes=_W, sends=True, runnable=True,
        label="While you were away", description="Tell an owner who went quiet what happened while they were away",
        target=("scheduler", "send_while_away_nudges"), claim="inactive_clients", max_minutes=20),
    "toast_optin_invites": dict(
        # 11am to 8pm: the overnight gap is fifteen hours.
        cadence="hourly 11am-8pm CT, each restaurant's last closed day", sla_minutes=18 * 60, sends=True, runnable=True,
        label="Opt-in invites", description="Text opt-in invites to the guests the POS saw on the last closed business day",
        target=("guest_marketing", "run_toast_optin_invites"), claim="optin_invite", max_minutes=30),
    "campaign_attribution": dict(
        cadence="noon CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Campaign attribution", description="Which campaign recipients the POS saw on a later check",
        target=("guest_marketing", "run_campaign_attribution"), max_minutes=45, retry=True),
    "review_request_followups": dict(
        cadence="hourly", sla_minutes=_H, sends=True, runnable=True,
        label="Review requests", description="Text post-visit review requests",
        target=("guest_marketing", "run_review_request_followups"), max_minutes=20),
    "issue_scan": dict(
        cadence="hourly (signals at 10am local)", sla_minutes=_H, sends=True, runnable=True,
        label="Issue scan", description="Open issues from fresh bad reviews, checklists and daily signals",
        target=("strategy_jobs", "run_issue_scan"), run_kwargs={"local_hour": 10}, max_minutes=30),
    "review_request_nudge": dict(
        cadence="hourly (Monday morning local)", sla_minutes=_H, sends=True, runnable=True,
        label="Review-request nudge", description="A weekly nudge with the measured review-request conversion",
        target=("strategy_jobs", "run_review_request_nudge"), max_minutes=15),

    # ── during service, every 20 minutes (one slot claim for six jobs) ──
    "intraday_capture": dict(
        cadence="every 20 minutes while open", sla_minutes=2 * 60, sends=False, runnable=True,
        label="Intraday capture", description="Net sales so far today, once an hour per open restaurant",
        target=("strategy_jobs", "run_intraday_capture"), claim="intraday", max_minutes=15, lane="intraday"),
    "pre_dinner_pulse": dict(
        cadence="every 20 minutes (4pm local)", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Pre-dinner pulse", description="One push before dinner when the day is materially off",
        target=("strategy_jobs", "run_pre_dinner_pulse"), claim="intraday", max_minutes=10, lane="intraday"),
    "coverage_check": dict(
        cadence="every 20 minutes while open", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Coverage", description="Scheduled staff who have not clocked in",
        target=("strategy_jobs", "run_coverage_check"), claim="intraday", max_minutes=10, lane="intraday"),
    "preshift_nudge": dict(
        cadence="every 20 minutes", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Pre-shift nudge", description="The pre-shift notes before service",
        target=("strategy_jobs", "run_preshift_nudge"), claim="intraday", max_minutes=10, lane="intraday"),
    "cert_reminders": dict(
        cadence="every 20 minutes (10am local, once a day)", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Certificate reminders",
        description="Tell each person, and the owner, about a certificate expiring within 30 days",
        target=("staff_knowledge", "run_cert_reminders"), claim="intraday", max_minutes=10, lane="intraday"),
    "task_sheets": dict(
        cadence="every 20 minutes", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Task sheets", description="Issue today's sheets from the published schedule, open critical-miss issues, close ended shifts",
        target=("task_sheets", "run_job"), claim="intraday", max_minutes=10, lane="intraday"),
    "closing_summary": dict(
        cadence="every 20 minutes (after close)", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Closing summary", description="How tonight went, once the doors are shut",
        target=("strategy_jobs", "run_closing_summary"), claim="intraday", max_minutes=10, lane="intraday"),
    "event_push": dict(
        cadence="every 20 minutes (the afternoon before a big game)", sla_minutes=2 * 60, sends=True,
        runnable=True, label="Big-game heads-up",
        description="A push the afternoon before a followed game measured big here, once per game",
        target=("event_intel.gameday", "run_event_push"), claim="intraday", max_minutes=10, lane="intraday"),
    "demand_opportunity": dict(
        cadence="every 20 minutes (weekly per restaurant)", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Quiet-night heads-up", description="A quiet night two days out, once a week",
        target=("strategy_jobs", "run_demand_opportunity"), claim="intraday", max_minutes=10, lane="intraday"),

    # ── the Daily Sales Report, every 10 minutes ─────────────────────────
    "blob_offload": dict(
        # Scheduler audit 10/10/26 RM-14: nothing while BLOB_STORE=db.
        cadence="daily 2am CT while BLOB_STORE is s3 or local", sla_minutes=None, sends=False, runnable=True,
        label="Blob offload",
        description="Move old inline model-call and quality blobs to object storage, leaving a pointer",
        target=("blob_store", "offload"), max_minutes=10),
    "pg_partitions": dict(
        # Scale audit 10/19/26 DB-04: nothing on SQLite.
        cadence="daily 2am CT on Postgres", sla_minutes=None, sends=False, runnable=True,
        label="Postgres partitions",
        description="Keep three months of monthly partitions ahead on the partitioned fact tables; "
                    "report dated rows that fell into a DEFAULT partition",
        target=("pg_partitions", "run"), max_minutes=5),
    "pg_shadow": dict(
        # Scheduler audit 10/10/26 RM-13: off unless PG_SHADOW_WRITE=1.
        cadence="every 10 minutes while PG_SHADOW_WRITE is on", sla_minutes=None, sends=False, runnable=True,
        label="Postgres shadow",
        description="Write the schedule revisions noted in the shadow outbox to the normalized model on Postgres",
        target=("pg_shadow", "run_shadow_pass"), max_minutes=5),
    "dsr_sweep": dict(
        # Delivery inside is gated on scheduling_allowed (dsr.deliver._allowed).
        cadence="every 10 minutes (DSR lane)", sla_minutes=60, sends=False, runnable=True,
        label="DSR sweep", description="Nightly DSR: past each close, poll the POS close, collect, write, finalise "
                                       "(several restaurants at once, earliest deadline first)",
        target=("dsr.pipeline", "run_sweep"), max_minutes=30, lane="dsr"),
    "dsr_delivery": dict(
        cadence="every 10 minutes", sla_minutes=60, sends=True, runnable=True,
        label="DSR delivery", description="Send the DSR pushes held through each restaurant's quiet hours, once they end",
        target=("dsr.deliver", "release_held"), max_minutes=10, lane="minute"),

    # ── Message Batches, every 5 minutes (AI cost audit 10/7/26 #19) ─────
    "ai_batch_collect": dict(
        # Collects only where the scheduler may run (ai_batches.run_collector);
        # its callbacks store results, they send nothing (the DSR sweep does).
        cadence="every 5 minutes", sla_minutes=60, sends=False, runnable=True,
        label="AI batch collector",
        description="Read the Message Batches that have ended, ledger each answer at the batch rate and "
                    "hand it to its workflow (the nightly DSR narrative, the learner's shadow replays)",
        target=("ai_batches", "run_collector"), max_minutes=10, lane="minute"),

    # ── the AI orchestration learner (design 10/7/26, phase 6) ───────────
    "ai_shadow_arms": dict(
        # Submits only where Message Batches may (ai_batches.enabled: the
        # production scheduler, shadow_arms listed); bounded by
        # AI_SHADOW_WEEKLY_USD and 20 replays a workflow. Sends nothing.
        cadence="Sun 4am CT (AI lane)", sla_minutes=_W, sends=False, runnable=True,
        label="AI shadow replays",
        description="Replay each workflow's next cheaper tier on a sample of kept production requests, at the "
                    "batch rate, and score both answers with the same rubric — the evidence a cheaper route needs",
        target=("ai_learning", "run_shadow_arms"), max_minutes=20, lane="ai", retry=True),
    "ai_route_learning": dict(
        # Database reads and writes only: recommendations for the console,
        # and the one automatic step, reverting an override that hurt.
        cadence="Mon 6am CT", sla_minutes=_W, sends=False, runnable=True,
        label="AI route learning",
        description="Rank each workflow's routes by quality per dollar over 28 days of runs, recommend changes "
                    "for the AI routes console, and revert an override owners accept clearly less often",
        target=("ai_learning", "run_learning"), max_minutes=10, retry=True),
    "ai_smoke_eval": dict(
        # Off unless AI_SMOKE_EVAL=1 (a run that attempts nothing). Model
        # calls only, bounded by AI_SMOKE_EVAL_BUDGET_USD a pass; sends
        # nothing. Never retried: a failed pass is next Monday's.
        cadence="Mon 5am CT (AI lane; off unless AI_SMOKE_EVAL=1)", sla_minutes=_W, sends=False, runnable=True,
        label="AI smoke eval",
        description="Replay a few frozen production inputs of each top AI surface on its production model, score "
                    "the answers with the deterministic checks and record how far they are from the baseline "
                    "(scripts/run_ai_eval.py)",
        target=("ai_eval", "run_smoke_eval"), max_minutes=30, lane="ai"),

    # ── staff reminders, every 10 minutes (employee audit H3/M8) ─────────
    "staff_reminders": dict(
        # Sends: gated inside on scheduling_allowed (staff_reminders._allowed).
        cadence="every 10 minutes", sla_minutes=60, sends=True, runnable=True,
        label="Staff reminders",
        description="Push each employee ~1 hour before a published shift and ~15 minutes before a critical "
                    "task line is due (claimed once per person), and send the staff texts held overnight",
        target=("staff_reminders", "run_job"), max_minutes=10, lane="minute"),

    # ── the schedule on the floor (schedule audit 10/10/26 MARKET-7/8/24) ──
    "pos_schedule_push": dict(
        # Writes to a POS, never to a person: held on a local copy inside
        # (pos_schedule_push.run_push checks scheduler.scheduling_allowed).
        cadence="every 10 minutes", sla_minutes=60, sends=False, runnable=True,
        label="POS schedule push",
        description="Write each published week whose owner turned the POS push on to the POS time clock (preview "
                    "first, per-shift status, retries a POS-side failure; bounded, the queue is the cursor)",
        target=("pos_schedule_push", "run_queue"), max_minutes=5, lane="live"),
    # Audit #23 (10/21/26) A6-01 / A4-10: one health row per RPOWER store and
    # the fleet's daily row, which the console, the alert rules, the fleet
    # POS reading and the status page read instead of every restaurant row.
    # Sends: the owner's one RPOWER-reconnect notice on the transition into
    # an auth failure (gated inside on scheduling_allowed).
    "rpower_health_rollup": dict(
        cadence="every 15 minutes", sla_minutes=60, sends=True, runnable=True,
        label="RPOWER health rollup",
        description="Each RPOWER store's health per data type (labor, archive, change read, catalog, prices, live, "
                    "intraday, loss, DSR), coverage, gaps, queue, requests and anomalies, in grouped reads "
                    "(bounded, resumable); tell an owner once when RPOWER refuses their store's token",
        target=("rpower_health", "run_rollup"), max_minutes=5),
    "live_labor": dict(
        cadence="every 15 minutes while open", sla_minutes=60, sends=True, runnable=True,
        label="Live labor",
        description="Labor to date and the day's projected labor % against sales, and the one legal, priced cut, "
                    "extension or call-in offered to the managers (or, earned, sent to the person) — bounded, "
                    "resumable",
        target=("labor_live", "run_live_labor"), max_minutes=5, lane="live"),

    # ── every tick ──────────────────────────────────────────────────────
    "morning_brief": dict(
        cadence="every tick (each restaurant's own brief hour)", sla_minutes=60, sends=True, runnable=True,
        label="Morning brief", description="Each restaurant's brief, once, at or after its own local hour (bounded, resumable)",
        target=("morning_brief", "run_due"), max_minutes=20, lane="brief"),
    "owed_sends": dict(
        cadence="every tick", sla_minutes=60, sends=True, runnable=True,
        label="Owed billing mail",
        description="Send the billing email a signing or a Stripe event owes (receipts, dunning, the set-password "
                    "welcome, pay reminders): each row claimed before its send, retried with backoff",
        target=("billing_jobs", "run_owed_sends"), max_minutes=5),
    "minute_duties": dict(
        cadence="every tick, and every pulse during a long job", sla_minutes=60, sends=True, runnable=False,
        label="Minute duties",
        description="Scheduled posts, delayed actions, issue escalations, held alerts, newsletter and campaign "
                    "sends, and the push and webhook outboxes a restart left behind",
        target=("scheduler", "_minute_duties"), max_minutes=20),
    "prune_login_attempts": dict(
        cadence="daily", sla_minutes=_D, sends=False, runnable=True,
        label="Login-attempt prune", description="Drop login-attempt rows older than two days",
        target=("scheduler", "run_prune_login_attempts"), max_minutes=10),
}


def spec(name):
    """The registry entry for a job run name, or {}."""
    return JOBS.get(name) or {}


# The jobs that read in the owner's salary view (`salary_view="owner"`):
# each one's outputs reach the account holder alone. A new one is added
# here AND on its entry, with the reason beside it (tests/
# test_rf3_salary_view.py holds the two equal).
OWNER_SALARY_JOBS = frozenset({"labor_prewarm", "value_figures", "digest_precompute", "weekly_digests",
                               "monthly_summary", "quarterly_summaries"})


def owner_salary_view(name) -> bool:
    """Whether a run of `name` reads in the owner's salary view (all-in
    labor). False for every other job and for a name the registry does
    not know (an admin task): manager-safe by default."""
    return spec(name).get("salary_view") == "owner"


def max_minutes(name):
    """How long a run of `name` may take before it is stuck."""
    return int(spec(name).get("max_minutes") or DEFAULT_MAX_MINUTES)


def claim_key(name):
    return spec(name).get("claim") or name


def jobs_for_claim(claim):
    """Every run name the loop runs under one claim key."""
    return [n for n, s in JOBS.items() if (s.get("claim") or n) == claim]


def expected_hours():
    """{name: SLA hours} for every job that has an SLA — ops.EXPECTED_JOBS."""
    return {n: round(s["sla_minutes"] / 60.0, 2) if s["sla_minutes"] % 60 else s["sla_minutes"] // 60
            for n, s in JOBS.items() if s.get("sla_minutes")}


def run_kwargs(name, now=None):
    """The keyword arguments Run now passes (the same the loop passes)."""
    kw = spec(name).get("run_kwargs")
    if callable(kw):
        if now is None:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("America/Chicago")).replace(tzinfo=None)
        return dict(kw(now) or {})
    return dict(kw or {})
