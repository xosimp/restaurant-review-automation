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
tests/test_fix_d_jobs_registry.py fails when a `run_job("…")` in the loop has
no entry here, or an entry here is never run — the two lists cannot drift
again.

Fields
------
cadence       when it runs, for people (the loop is the source of truth)
sla_minutes   the most minutes that may pass between SUCCESSFUL runs
              (job_runs.ok 1 or 2) before it is overdue; None = no SLA (a
              quarterly job outlives job_runs' 45-day retention)
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
              job must not be sent twice)
lane          run on this bounded worker lane beside the loop, not on the
              loop thread (scheduler._LANES) — the weekly Intel sweeps
              run for hours and blocked briefs, DSR and intraday (#98)

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
        target=("scheduler", "backup_db"), max_minutes=60, retry=True),
    "prune_ledgers": dict(
        cadence="2am CT nightly, after the backup", sla_minutes=_D, sends=False, runnable=True,
        label="Retention",
        description="Delete ledger rows past their retention window, in chunks, then refresh planner statistics; "
                    "soft-delete reviews past each owner's retention",
        target=("scheduler", "run_nightly_retention"), max_minutes=45),
    "restore_drill": dict(
        cadence="2nd of Jan / Apr / Jul / Oct, after the 2am backup", sla_minutes=None, sends=True, runnable=True,
        label="Restore drill",
        description="Restore the newest snapshot to scratch and prove it is fresh, opens, migrates and kept its tokens",
        target=("scheduler", "run_restore_drill"), max_minutes=30),
    "pos_sync": dict(
        cadence="3am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="POS sync", description="Pull yesterday's sales and labor from every connected POS, then inventory systems",
        target=("scheduler", "run_toast_sync"), max_minutes=75, retry=True),
    "pos_retry": dict(
        cadence="hourly, until 11am local", sla_minutes=_H, sends=False, runnable=True,
        label="POS retry", description="Retry failed POS syncs whose retry is due (+1h, +3h, +6h; never an auth failure)",
        target=("scheduler", "run_pos_retry"), max_minutes=25),
    "stripe_reconcile": dict(
        # Reads Stripe; never changes a billing status (billing_jobs, #115).
        cadence="3:30am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="Stripe reconcile",
        description="Refresh the subscription mirror from Stripe and record where Stripe and the local billing state "
                    "disagree (bounded, resumable)",
        target=("billing_jobs", "reconcile_stripe"), max_minutes=20, retry=True),
    "loss_sync": dict(
        cadence="3am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="Loss sync", description="Pull comps, voids and refunds from POSes that report them",
        target=("strategy_jobs", "run_loss_sync"), max_minutes=45, retry=True),
    "intelligence_features": dict(
        cadence="3am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Intelligence features", description="The intelligence engine's per-restaurant feature pass (bounded, resumable)",
        target=("intelligence.jobs", "run_features"), max_minutes=60, retry=True),
    "intelligence_learning": dict(
        cadence="4am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Intelligence learning", description="Learning over the materialized intelligence tables",
        target=("intelligence.jobs", "run_learning"), max_minutes=60, retry=True),
    "marketing_metrics_sync": dict(
        cadence="4am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="Marketing metrics", description="Refresh Instagram / Facebook post metrics",
        target=("scheduler", "run_marketing_metrics_sync"), max_minutes=40, retry=True),
    "schedule_outcomes": dict(
        cadence="Mon 4am CT", sla_minutes=_W, sends=False, runnable=True,
        label="Schedule outcomes", description="Record what each published week actually did, by daypart",
        target=("strategy_jobs", "run_schedule_outcomes"), max_minutes=30, retry=True),
    "inventory_depletion": dict(
        cadence="5am CT nightly", sla_minutes=_D, sends=False, runnable=True,
        label="Depletion", description="Deplete inventory from POS sales",
        target=("scheduler", "run_daily_depletion_sync"), max_minutes=60, retry=True),
    "food_cost_snapshots": dict(
        cadence="5am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Food cost snapshots", description="Write each restaurant's inventory snapshot and score closed forecasts",
        target=("scheduler", "run_food_cost_snapshots"), max_minutes=60, retry=True),
    "forecast_scoring": dict(
        cadence="5am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Forecast scoring", description="Score every frozen forecast whose period has closed",
        target=("scheduler", "run_forecast_scoring"), max_minutes=60, retry=True),
    "reservation_sync": dict(
        cadence="5am CT daily, the day before each draft", sla_minutes=_D, sends=False, runnable=True,
        label="Reservations", description="Reservation feeds into events & reservations (no provider live yet)",
        target=("reservation_feeds", "run_reservation_sync"), run_kwargs=_chi_weekday, max_minutes=30, retry=True),
    "quality_calibration": dict(
        cadence="Sun 5am CT", sla_minutes=_W, sends=False, runnable=True,
        label="Quality calibration", description="Suggest Shift Quality weights (calibrate_weights, what Apply writes)",
        target=("strategy_jobs", "run_quality_calibration"), max_minutes=20, retry=True),
    "data_health_daily": dict(
        cadence="6am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Data health", description="Write each restaurant's Data Health snapshot (data_health_daily)",
        target=("scheduler", "run_data_health_daily"), max_minutes=60, retry=True),
    "review_diagnoses": dict(
        cadence="6am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Review diagnoses", description="Root-cause reads of each restaurant's biggest complaint clusters (bounded, resumable)",
        target=("scheduler", "run_review_diagnoses"), max_minutes=60, retry=True),
    "food_cost_diagnoses": dict(
        cadence="6am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Food cost diagnoses", description="Root-cause reads over each restaurant's ranked cost drivers (bounded, resumable)",
        target=("scheduler", "run_food_cost_diagnoses"), max_minutes=60, retry=True),
    "outcome_evaluations": dict(
        cadence="6am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Outcome evaluations", description="Close outcome trackers whose window ended; mark goals met",
        target=("strategy_jobs", "run_outcome_evaluations"), max_minutes=20, retry=True),
    "outcome_rechecks": dict(
        cadence="6am CT daily", sla_minutes=_D, sends=False, runnable=True,
        label="Outcome rechecks", description="Re-check measured results at 90 days and accrue measured savings day by day",
        target=("strategy_jobs", "run_outcome_rechecks"), max_minutes=20, retry=True),
    "value_figures": dict(
        cadence="6am CT daily, after outcome evaluations (Intel lane)", sla_minutes=_D, sends=False, runnable=True,
        label="Value figures",
        description="Each restaurant's four value figures into value_figures_daily, which the Intelligence page "
                    "sums (bounded, resumable)",
        target=("intelligence.dashboard", "snapshot_value_figures"), max_minutes=55, lane="intel"),
    "competitor_analysis": dict(
        cadence="Mon 6am CT (catch-up to Wed), then a daily retry", sla_minutes=_W, sends=False, runnable=True,
        label="Competitor analysis", description="Weekly competitor analysis for full-tier clients (Places + Claude), on the Intel lane",
        target=("scheduler", "run_weekly_competitor_analysis"), max_minutes=200, lane="intel"),
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
        target=("scheduler", "run_daily_fetch"), max_minutes=200),
    "provider_probes": dict(
        cadence="hourly", sla_minutes=_H, sends=True, runnable=True,
        label="Provider probes",
        description="One authenticated, non-sending call per provider (Resend, Twilio, Anthropic, Stripe, Places, "
                    "APNs); pages Will when one starts failing",
        target=("provider_health", "run_probes"), max_minutes=10),
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
    "auto_draft_schedule": dict(
        cadence="hourly (6am local on each restaurant's draft day)", sla_minutes=_H, sends=True, runnable=True,
        label="Auto-draft", description="Draft next week's schedule for owners who opted in (a draft; nothing reaches staff)",
        target=("strategy_jobs", "run_auto_draft_schedules"), run_kwargs=_chi_now_kw, max_minutes=50),
    "weekly_plan": dict(
        cadence="Mon 7am local (hourly on Mondays)", sla_minutes=_W, sends=False, runnable=True,
        label="Weekly plan", description="The agent files the week's three actions as issues (nobody is texted)",
        target=("strategy_jobs", "run_weekly_plan"), max_minutes=60),
    "recipe_drafts": dict(
        cadence="Tue 5am local (hourly on Tuesdays)", sla_minutes=_W, sends=False, runnable=True,
        label="Recipe drafts", description="Draft recipes for POS dishes that have none",
        target=("strategy_jobs", "run_recipe_drafts"), max_minutes=45),
    "trusted_orders": dict(
        cadence="hourly (8am local on each restaurant's order day)", sla_minutes=_H, sends=True, runnable=True,
        label="Trusted orders", description="Queue supplier orders that have earned it, with an hour to undo",
        target=("strategy_jobs", "run_trusted_orders"), max_minutes=20),
    "auto_publish_schedule": dict(
        cadence="hourly (9am local, the day after the draft)", sla_minutes=_H, sends=True, runnable=True,
        label="Auto-publish", description="Queue the unedited draft to publish with a two-hour undo",
        target=("scheduler", "run_auto_publish_schedules"), max_minutes=20),
    "weekly_digests": dict(
        cadence="hourly (9am local on each client's digest day)", sla_minutes=_H, sends=True, runnable=True,
        label="Weekly digests", description="Email weekly digests",
        target=("scheduler", "run_weekly_digests"), claim="weekly_digest", max_minutes=30),
    "daily_alerts": dict(
        cadence="hourly (10am local)", sla_minutes=_H, sends=True, runnable=True,
        label="Daily alerts", description="No-response, trend, threshold, labor, competitor and data-source alerts, batched",
        target=("scheduler", "run_daily_alert_checks"), max_minutes=30),
    "monthly_summary": dict(
        cadence="hourly (9am local on the 1st)", sla_minutes=_H, sends=True, runnable=True,
        label="Monthly summary", description="The monthly business review email",
        target=("scheduler", "run_monthly_summaries"), max_minutes=30),
    "quarterly_summaries": dict(
        cadence="hourly on the 1st of Jan / Apr / Jul / Oct (9am local)", sla_minutes=None, sends=True, runnable=True,
        label="Quarterly summary", description="The quarter that just ended",
        target=("scheduler", "run_quarterly_summaries"), claim="quarterly_summary", max_minutes=30),
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
        target=("strategy_jobs", "run_intraday_capture"), claim="intraday", max_minutes=15),
    "pre_dinner_pulse": dict(
        cadence="every 20 minutes (4pm local)", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Pre-dinner pulse", description="One push before dinner when the day is materially off",
        target=("strategy_jobs", "run_pre_dinner_pulse"), claim="intraday", max_minutes=10),
    "coverage_check": dict(
        cadence="every 20 minutes while open", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Coverage", description="Scheduled staff who have not clocked in",
        target=("strategy_jobs", "run_coverage_check"), claim="intraday", max_minutes=10),
    "preshift_nudge": dict(
        cadence="every 20 minutes", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Pre-shift nudge", description="The pre-shift notes before service",
        target=("strategy_jobs", "run_preshift_nudge"), claim="intraday", max_minutes=10),
    "closing_summary": dict(
        cadence="every 20 minutes (after close)", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Closing summary", description="How tonight went, once the doors are shut",
        target=("strategy_jobs", "run_closing_summary"), claim="intraday", max_minutes=10),
    "demand_opportunity": dict(
        cadence="every 20 minutes (weekly per restaurant)", sla_minutes=2 * 60, sends=True, runnable=True,
        label="Quiet-night heads-up", description="A quiet night two days out, once a week",
        target=("strategy_jobs", "run_demand_opportunity"), claim="intraday", max_minutes=10),

    # ── the Daily Sales Report, every 10 minutes ─────────────────────────
    "dsr_sweep": dict(
        # Delivery inside is gated on scheduling_allowed (dsr.deliver._allowed).
        cadence="every 10 minutes", sla_minutes=60, sends=False, runnable=True,
        label="DSR sweep", description="Nightly DSR: past each close, poll the POS close, collect, write, finalise",
        target=("dsr.pipeline", "run_sweep"), max_minutes=30),
    "dsr_delivery": dict(
        cadence="every 10 minutes", sla_minutes=60, sends=True, runnable=True,
        label="DSR delivery", description="Send the DSR pushes held through each restaurant's quiet hours, once they end",
        target=("dsr.deliver", "release_held"), max_minutes=10),

    # ── every tick ──────────────────────────────────────────────────────
    "morning_brief": dict(
        cadence="every tick (each restaurant's own brief hour)", sla_minutes=60, sends=True, runnable=True,
        label="Morning brief", description="Each restaurant's brief, once, at or after its own local hour (bounded, resumable)",
        target=("morning_brief", "run_due"), max_minutes=20),
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
